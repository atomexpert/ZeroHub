"""
Zero-Trust In-Process Workspace Filesystem MCP Gateway (`hub_files`) for Antigravity Hub (v94).

Provides pure-Python (`pathlib` / `os.scandir`, 0 OS child processes) file reading, directory listing,
file/content search, and mode-gated atomic file editing strictly scoped to the active turn's workspace
(`ACTIVE_RUNS[chat_id]`) over loopback Streamable HTTP (`/mcp/files/{mcp_token}`).

Used primarily by OpenAI Codex CLI (`@openai/codex`) turns where `shell_tool = false` disables
host shell execution (`read_only` / `write_files` / `docker` contours).
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import config, database
from .agy_engine import ACTIVE_RUNS, ActiveRun, format_rel_project_path
from .sandbox_service import is_loopback_mcp_request

MCP_FILES_SERVER_NAME = "hub_files"
USER_SCOPED_FILES_CHAT_ID = "_user"
MCP_PROTOCOL_VERSION = "2025-03-26"
_SUPPORTED_MCP_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

MCP_FILES_READ_TOOLS: tuple[str, ...] = (
    "read_file",
    "list_dir",
    "find_by_name",
    "grep_search",
)
MCP_FILES_WRITE_TOOLS: tuple[str, ...] = (
    "write_file",
    "edit_file",
)
MCP_FILES_ALL_TOOLS: tuple[str, ...] = MCP_FILES_READ_TOOLS + MCP_FILES_WRITE_TOOLS

_MAX_READ_LINES = 800
_MAX_READ_CHARS = 48_000
_MAX_GREP_FILE_BYTES = 8 * 1024 * 1024
_MAX_WRITE_CHARS = 4 * 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_SKIP_SEARCH_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        "dist",
        "build",
        "target",
    }
)

_SELF_HUB_SKIP_TOP_DIRS: frozenset[str] = frozenset(
    {
        "users",
        "shared_projects",
        "data",
        "scratch",
    }
)


def _should_skip_self_hub_subdir(parent_dir: Path, entry_name: str, ws_root: Path) -> bool:
    """When working on `Antigravity_Hub` (`BASE_DIR`) itself, skip nested user/shared/data/scratch containers."""
    if entry_name.lower() not in _SELF_HUB_SKIP_TOP_DIRS:
        return False
    try:
        base_res = config.BASE_DIR.resolve(strict=False)
        return ws_root.resolve(strict=False) == base_res and parent_dir.resolve(strict=False) == base_res
    except Exception:
        return False

_SECRET_BASENAMES_LOWER: frozenset[str] = frozenset(
    {
        "hub.db",
        "hub.db-wal",
        "hub.db-shm",
        "hub.db-journal",
        "auth.json",
        ".credentials.json",
        "credentials.json",
        "mcp_config.json",
        "mcp_config.docker.json",
        "mcp_hub_runtime.json",
        "antigravity-oauth-token",
    }
)

_GIT_SHORTNAME_RE = re.compile(r"^git~\d+$", re.IGNORECASE)


@dataclass
class ResolvedFilesRun:
    user: dict[str, Any]
    chat_id: str
    run_state: ActiveRun
    workspace_dir: Path
    project_dir: Optional[Path]
    uploads_dir: Optional[Path]
    can_write: bool
    upload_dirs: tuple[Path, ...] = ()


# ---------------------------------------------------------------------------
# 1. HMAC-SHA256 Token (`user_id` + `session_version` + `chat_id`)
# ---------------------------------------------------------------------------


def _compute_files_mcp_signature(user_id: str, session_version: int, chat_id: str) -> str:
    msg = f"files-mcp:v1:{user_id}:{int(session_version)}:{chat_id}".encode("utf-8")
    return hmac.new(config.HUB_SECRET_KEY.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def build_files_mcp_token(user_id: str, session_version: int, chat_id: str) -> str:
    clean_uid = (user_id or "").strip()
    clean_cid = (chat_id or "").strip()
    sv_int = int(session_version or 0)
    if not clean_uid or not clean_cid or sv_int < 1:
        raise ValueError("user_id, chat_id и положительный session_version обязательны для токена hub_files")
    payload_json = json.dumps(
        [clean_uid, sv_int, clean_cid], separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    claim_b64 = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")
    sig = _compute_files_mcp_signature(clean_uid, sv_int, clean_cid)
    return f"{claim_b64}.{sig}"


def verify_files_mcp_token(token: str) -> Optional[tuple[str, int, str]]:
    """Verify HMAC-SHA256 signature and return `(user_id, session_version, chat_id)`."""
    raw = (token or "").strip()
    if "." not in raw or len(raw) > 512:
        return None
    claim_b64, candidate_sig = raw.split(".", 1)
    if not claim_b64 or len(candidate_sig) != 64:
        return None
    pad = "=" * ((4 - len(claim_b64) % 4) % 4)
    try:
        decoded_bytes = base64.urlsafe_b64decode((claim_b64 + pad).encode("ascii"))
        arr = json.loads(decoded_bytes.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(arr, list) or len(arr) != 3:
        return None
    user_id = str(arr[0] or "").strip()
    chat_id = str(arr[2] or "").strip()
    try:
        sv_int = int(arr[1])
    except (TypeError, ValueError):
        return None
    if not user_id or not chat_id or sv_int < 1:
        return None
    expected_sig = _compute_files_mcp_signature(user_id, sv_int, chat_id)
    if not hmac.compare_digest(expected_sig, candidate_sig):
        return None
    return (user_id, sv_int, chat_id)


async def _authenticate_files_user_from_token(
    mcp_token: str,
) -> Optional[tuple[dict[str, Any], str]]:
    parsed = verify_files_mcp_token(mcp_token)
    if not parsed:
        return None
    user_id, token_sv, chat_id = parsed
    user = await database.get_user_by_id(user_id)
    if not user:
        return None
    if not bool(user.get("is_active", True)):
        return None
    if int(user.get("session_version") or 1) != token_sv:
        return None
    return (user, chat_id)


# ---------------------------------------------------------------------------
# 2. Zero-Trust Path & Reparse/Symlink Guards
# ---------------------------------------------------------------------------


def _is_within_dir(child: Path, parent: Path) -> bool:
    try:
        c_res = child.resolve(strict=False)
        p_res = parent.resolve(strict=False)
        return c_res == p_res or p_res in c_res.parents
    except Exception:
        return False


def _is_reparse_or_symlink(p: Path) -> bool:
    try:
        if p.is_symlink():
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(p):
            return True
        st = os.lstat(p)
        file_attrs = int(getattr(st, "st_file_attributes", 0) or 0)
        if file_attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    except OSError:
        return False
    return False


def _has_reparse_in_chain(target: Path, allowed_root: Path) -> bool:
    """Return True if `allowed_root` or any existing segment down to `target` is a symlink/junction."""
    if _is_reparse_or_symlink(allowed_root):
        return True
    try:
        rel = target.relative_to(allowed_root)
    except ValueError:
        return True
    cur = allowed_root
    for part in rel.parts:
        cur = cur / part
        if cur.exists() or _is_reparse_or_symlink(cur):
            if _is_reparse_or_symlink(cur):
                return True
    return False


def _is_forbidden_secret_path(resolved: Path, *, workspace_root: Path) -> bool:
    name_low = resolved.name.lower()
    if name_low in _SECRET_BASENAMES_LOWER or "oauth-token" in name_low:
        return True
    if name_low.startswith("hub.db"):
        return True
    try:
        cli_homes_res = config.CLI_HOMES_DIR.resolve(strict=False)
        if _is_within_dir(resolved, cli_homes_res):
            return True
    except Exception:
        pass
    try:
        data_res = config.DATA_DIR.resolve(strict=False)
        base_res = config.BASE_DIR.resolve(strict=False)
        hub_db_res = (data_res / "hub.db").resolve(strict=False)
        if resolved == hub_db_res or str(resolved).lower().startswith(str(hub_db_res).lower()):
            return True
        # Block access inside DATA_DIR unless workspace_root itself is an isolated subdir inside DATA_DIR
        # (never when workspace_root == BASE_DIR!).
        ws_inside_data = workspace_root != base_res and workspace_root != data_res and _is_within_dir(workspace_root, data_res)
        if _is_within_dir(resolved, data_res) and not (ws_inside_data and _is_within_dir(resolved, workspace_root)):
            return True
    except Exception:
        pass
    try:
        base_res = config.BASE_DIR.resolve(strict=False)
        if workspace_root == base_res and name_low.startswith(".env"):
            return True
    except Exception:
        pass
    return False


def _has_git_component(rel_parts: tuple[str, ...]) -> bool:
    for part in rel_parts:
        low = part.lower().rstrip(". ")
        if low == ".git" or _GIT_SHORTNAME_RE.match(low):
            return True
    return False


def resolve_safe_workspace_path(
    raw_path: str,
    run_ctx: ResolvedFilesRun,
    *,
    for_write: bool = False,
    allow_chat_uploads: bool = False,
    allow_root_itself: bool = False,
) -> Path:
    """Validate and resolve `raw_path` strictly inside `run_ctx.workspace_dir` (or `uploads_dir` for read)."""
    if raw_path is None:
        raw_clean = "." if allow_root_itself else ""
    else:
        raw_clean = str(raw_path).strip()

    if "\x00" in raw_clean:
        raise PermissionError("Недопустимый нулевой байт в пути файла.")

    if not raw_clean:
        if allow_root_itself:
            raw_clean = "."
        else:
            raise ValueError("Не указан путь к файлу (`path`).")

    # Normalize container aliases (`/workspace/...` -> relative to workspace_dir)
    posix_view = raw_clean.replace("\\", "/")
    if posix_view == "/workspace" or posix_view == "/workspace/":
        raw_clean = "."
        posix_view = "."
    elif posix_view.startswith("/workspace/"):
        raw_clean = posix_view[len("/workspace/") :]
        posix_view = raw_clean
    elif (
        allow_chat_uploads
        and not for_write
        and (run_ctx.upload_dirs or run_ctx.uploads_dir is not None)
        and posix_view.startswith(f"/uploads/{run_ctx.chat_id}/")
    ):
        sub_up = posix_view[len(f"/uploads/{run_ctx.chat_id}/") :]
        cand_up_dirs = list(run_ctx.upload_dirs) if run_ctx.upload_dirs else ([run_ctx.uploads_dir] if run_ctx.uploads_dir else [])
        chosen_up = cand_up_dirs[0]
        for ud in cand_up_dirs:
            if ud is not None and (ud / sub_up).exists():
                chosen_up = ud
                break
        if chosen_up is not None:
            raw_clean = str(chosen_up / sub_up)

    # Reject explicit `..` traversal tokens immediately before normalization
    raw_segments = [seg for seg in re.split(r"[\\/]+", raw_clean) if seg]
    if ".." in raw_segments:
        raise PermissionError("Выход за пределы рабочей директории через '..' запрещён.")

    ws_root = run_ctx.project_dir.resolve() if for_write and run_ctx.project_dir else run_ctx.workspace_dir.resolve()
    if _is_reparse_or_symlink(ws_root):
        raise PermissionError("Рабочая директория содержит недопустимую символическую ссылку.")

    p_in = Path(raw_clean)
    allowed_roots: list[Path] = [ws_root]
    for extra_d in getattr(run_ctx.run_state, "extra_dirs", None) or []:
        if extra_d is not None:
            ed_res = Path(extra_d).resolve(strict=False)
            if ed_res.is_dir() and not _is_reparse_or_symlink(ed_res) and ed_res not in allowed_roots:
                allowed_roots.append(ed_res)
    if allow_chat_uploads and not for_write:
        cand_up_dirs = list(run_ctx.upload_dirs) if run_ctx.upload_dirs else ([run_ctx.uploads_dir] if run_ctx.uploads_dir else [])
        for ud in cand_up_dirs:
            if ud is not None:
                up_root = ud.resolve()
                if not _is_reparse_or_symlink(up_root) and up_root not in allowed_roots:
                    allowed_roots.append(up_root)

    if p_in.is_absolute():
        matched_root: Optional[Path] = None
        for root_cand in allowed_roots:
            # Check raw lexical containment first so symlinks inside root are inspected segment-by-segment
            try:
                p_in.relative_to(root_cand)
                matched_root = root_cand
                candidate = p_in
                break
            except ValueError:
                if _is_within_dir(p_in, root_cand):
                    matched_root = root_cand
                    candidate = root_cand / p_in.resolve(strict=False).relative_to(root_cand)
                    break
        if matched_root is None:
            raise PermissionError(
                "Доступ к путям за пределами рабочей папки проекта запрещён политикой Zero-Trust."
            )
    else:
        matched_root = ws_root
        candidate = ws_root / p_in

    if _has_reparse_in_chain(candidate, matched_root):
        raise PermissionError("Доступ через символические ссылки или NTFS-junction запрещён.")

    resolved = candidate.resolve(strict=False)
    if not _is_within_dir(resolved, matched_root):
        raise PermissionError("Путь выходит за пределы разрешённой рабочей директории.")

    if not allow_root_itself and resolved == matched_root:
        raise ValueError("Укажите путь к конкретному файлу внутри рабочей папки, а не саму корневую папку.")

    if _is_forbidden_secret_path(resolved, workspace_root=matched_root):
        raise PermissionError("Доступ к защищённым системным/секретным файлам запрещён политикой безопасности.")

    rel_parts = resolved.relative_to(matched_root).parts
    if for_write and _has_git_component(rel_parts):
        raise PermissionError("Прямая модификация служебной директории .git через файловый инструмент запрещена.")

    return resolved


def is_safe_workspace_write_target(
    raw_path: str,
    workspace_dir: Path,
    *,
    allow_root_itself: bool = False,
    extra_dirs: Optional[list[Path]] = None,
) -> bool:
    """Boolean Zero-Trust write guard reusable by Codex `fileChange`/`applyPatch` approval checks."""
    try:
        ws_res = workspace_dir.resolve()
        dummy_run = ActiveRun(chat_id="_guard", kind="project", extra_dirs=list(extra_dirs or []))
        dummy_ctx = ResolvedFilesRun(
            user={"id": "_guard", "role": "admin"},
            chat_id="_guard",
            run_state=dummy_run,
            workspace_dir=ws_res,
            project_dir=ws_res,
            uploads_dir=None,
            can_write=True,
        )
        resolve_safe_workspace_path(
            raw_path,
            dummy_ctx,
            for_write=True,
            allow_chat_uploads=False,
            allow_root_itself=allow_root_itself,
        )
        return True
    except Exception:
        return False


def _atomic_write_cli_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists() and target.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    tmp = target.parent / f".{target.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, target)


def _sync_files_mcp_config_file(cfg_file: Path, *, server_url: Optional[str]) -> None:
    existing_root: dict[str, Any] = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_root = loaded
        except Exception:
            existing_root = {}

    servers = existing_root.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    else:
        servers = dict(servers)

    if server_url:
        servers[MCP_FILES_SERVER_NAME] = {"url": server_url}
        existing_root["mcpServers"] = servers
        rendered = json.dumps(existing_root, ensure_ascii=False, indent=2) + "\n"
        _atomic_write_cli_text(cfg_file, rendered)
    else:
        servers.pop(MCP_FILES_SERVER_NAME, None)
        other_root_keys = {k for k in existing_root.keys() if k != "mcpServers"}
        if not servers and not other_root_keys:
            cfg_file.unlink(missing_ok=True)
        else:
            existing_root["mcpServers"] = servers
            rendered = json.dumps(existing_root, ensure_ascii=False, indent=2) + "\n"
            _atomic_write_cli_text(cfg_file, rendered)


_HUB_FILES_INSTRUCTIONS_MD = (
    "# Поисковый файловый шлюз проекта (`hub_files`, Read-Only)\n\n"
    "По известным путям сразу используй нативный read_file. Схемы не перечитывай; поиск группируй.\n"
    "Быстрые in-process инструменты обзора и поиска по рабочей папке проекта без терминала:\n"
    "- `grep_search`: поиск подстроки или регулярного выражения по файлам проекта с номерами строк (`путь:строка: фрагмент`).\n"
    "- `find_by_name`: рекурсивный поиск файлов по имени или glob-шаблону (`*.py`, `*auth*`, `app/**/*.js`).\n"
    "- `list_dir`: просмотр дерева подпапок и файлов с размерами.\n"
    "- `read_file`: точечное чтение диапазона строк (`start_line`..`end_line`).\n\n"
    "Если параллельно запущены ходы в нескольких разных проектах, передавай необязательный параметр `project` с именем текущего проекта.\n"
)


def sync_user_files_cli_files(user: dict[str, Any]) -> None:
    """Idempotently register read-only `hub_files` MCP server in `<cli_home>/.gemini/config/mcp_config.json`
    and sync read-only lazy tool schemas (`list_dir.json`, `find_by_name.json`, `grep_search.json`, `read_file.json`,
    `instructions.md`) for `agy` turns where host shell is disabled."""
    uid = str((user or {}).get("id") or "").strip()
    if not uid:
        return
    sv = max(1, int((user or {}).get("session_version") or 1))
    token = build_files_mcp_token(uid, sv, USER_SCOPED_FILES_CHAT_ID)
    local_url = f"http://127.0.0.1:{config.HUB_PORT}/mcp/files/{token}"

    home_dir = config.ensure_user_cli_home(uid)
    mcp_cfg = home_dir / ".gemini" / "config" / "mcp_config.json"
    _sync_files_mcp_config_file(mcp_cfg, server_url=local_url)

    ro_tools = _build_files_mcp_tools_schema(include_write_tools=False)
    schema_dirs: list[Path] = []
    seen_resolved: set[Path] = set()
    for base_cli_data in (
        config.AGY_CLI_DATA_DIR,
        home_dir / ".gemini" / "antigravity-cli",
    ):
        if base_cli_data is None:
            continue
        cand_mcp_parent = Path(base_cli_data) / "mcp"
        try:
            res_parent = cand_mcp_parent.resolve()
        except OSError:
            continue
        if res_parent in seen_resolved:
            continue
        seen_resolved.add(res_parent)
        schema_dirs.append(res_parent / MCP_FILES_SERVER_NAME)

    for sdir in schema_dirs:
        try:
            sdir.mkdir(parents=True, exist_ok=True)
            for wt in MCP_FILES_WRITE_TOOLS:
                (sdir / f"{wt}.json").unlink(missing_ok=True)
            for tool in ro_tools:
                tname = str(tool["name"])
                lazy_doc = {
                    "name": tname,
                    "description": tool["description"],
                    "parameters": tool["inputSchema"],
                }
                _atomic_write_cli_text(
                    sdir / f"{tname}.json",
                    json.dumps(lazy_doc, ensure_ascii=False),
                )
            _atomic_write_cli_text(sdir / "instructions.md", _HUB_FILES_INSTRUCTIONS_MD)
        except OSError:
            continue


def resolve_active_files_run(
    user: dict[str, Any],
    chat_id: str,
    *,
    requested_project: str = "",
    hint_path: str = "",
) -> ResolvedFilesRun:
    clean_cid = (chat_id or "").strip()
    uid = str(user.get("id") or "").strip()
    if not clean_cid or not uid:
        raise PermissionError("Некорректный контекст сессии hub_files.")

    is_user_scoped = clean_cid == USER_SCOPED_FILES_CHAT_ID
    if is_user_scoped:
        candidates = [
            r
            for r in list(ACTIVE_RUNS.values())
            if not r.cancelled
            and str(r.user_id or "").strip() == uid
            and r.kind == "project"
            and r.project_dir is not None
            and r.project_dir.is_dir()
        ]
        if not candidates:
            raise PermissionError("Нет активного проектного хода для этого пользователя в ACTIVE_RUNS.")

        chosen_run: Optional[ActiveRun] = None
        req_proj = str(requested_project or "").strip().lower()
        if req_proj:
            proj_matches = [
                r
                for r in candidates
                if str(r.project_name or "").strip().lower() == req_proj
                or (r.project_dir is not None and r.project_dir.name.lower() == req_proj)
                or str(r.scope_key or "").strip().lower().endswith(f":{req_proj}")
            ]
            if not proj_matches:
                raise PermissionError(f"Нет активного хода для проекта '{requested_project}'.")
            uniq_dirs = {r.project_dir.resolve() for r in proj_matches if r.project_dir is not None}
            if len(uniq_dirs) == 1:
                chosen_run = proj_matches[0]
            else:
                candidates = proj_matches

        if chosen_run is None and hint_path:
            hp_clean = str(hint_path or "").strip()
            if hp_clean and not hp_clean.startswith("/workspace") and not hp_clean.startswith("/uploads/"):
                p_hint = Path(hp_clean)
                if p_hint.is_absolute():
                    path_matches = [
                        r
                        for r in candidates
                        if (r.project_dir is not None and _is_within_dir(p_hint, r.project_dir.resolve()))
                        or any(
                            ed is not None and _is_within_dir(p_hint, Path(ed).resolve())
                            for ed in (getattr(r, "extra_dirs", None) or [])
                        )
                    ]
                    uniq_pdirs = {r.project_dir.resolve() for r in path_matches if r.project_dir is not None}
                    if len(uniq_pdirs) == 1:
                        chosen_run = path_matches[0]

        if chosen_run is None:
            agy_cands = [r for r in candidates if str(getattr(r, "engine", "") or "") == "agy"]
            agy_dirs = {r.project_dir.resolve() for r in agy_cands if r.project_dir is not None}
            all_dirs = {r.project_dir.resolve() for r in candidates if r.project_dir is not None}
            if len(agy_dirs) == 1 and agy_cands:
                chosen_run = agy_cands[0]
            elif len(all_dirs) == 1:
                chosen_run = candidates[0]
            else:
                raise PermissionError(
                    "Параллельно запущены ходы в нескольких проектах — укажите параметр `project` в вызове hub_files."
                )

        run = chosen_run
        effective_cid = str(run.chat_id or "").strip() or clean_cid
    else:
        run = ACTIVE_RUNS.get(clean_cid)
        if run is None or run.cancelled:
            raise PermissionError("Нет активного хода для этого диалога в ACTIVE_RUNS.")
        if (run.user_id or "").strip() != uid:
            raise PermissionError("Активный ход принадлежит другому пользователю.")
        effective_cid = clean_cid

    proj_dir: Optional[Path] = None
    if run.kind == "project":
        if run.project_dir is None or not run.project_dir.is_dir():
            raise PermissionError("Рабочая директория проекта не найдена.")
        proj_dir = run.project_dir.resolve()
        ws_dir = proj_dir
    else:
        ws_cand = getattr(run, "workspace_dir", None) or run.project_dir
        if ws_cand is None or not Path(ws_cand).is_dir():
            ws_dir = config.resolve_user_chat_sandbox_dir(user, effective_cid, create=True)
        else:
            ws_dir = Path(ws_cand).resolve()

    is_admin = str(user.get("role") or "").strip().lower() == "admin"
    max_mode = "host_full" if is_admin else str(user.get("max_access_mode") or "write_files").strip().lower()
    if is_user_scoped:
        can_write = False
    else:
        can_write = bool(
            run.kind == "project"
            and proj_dir is not None
            and run.access_mode in ("write_files", "full_access")
            and (is_admin or max_mode in ("write_files", "docker_full", "host_full"))
        )

    safe_cid = config.sanitize_chat_folder_id(effective_cid)
    is_shared_proj = bool(
        proj_dir is not None
        and _is_within_dir(proj_dir, config.SHARED_PROJECTS_DIR.resolve(strict=False))
    )
    cand_uploads = config.resolve_chat_upload_dirs_for_turn(
        {"id": effective_cid, "user_id": uid},
        chat_id=effective_cid,
        owner_username=str(user.get("username") or ""),
        is_shared_project=is_shared_proj,
    )
    shared_up_cand = (config.SHARED_PROJECTS_DIR.resolve(strict=False) / "_uploads" / safe_cid).resolve(strict=False)
    if shared_up_cand.is_dir() and shared_up_cand not in cand_uploads:
        cand_uploads.insert(0 if is_shared_proj else len(cand_uploads), shared_up_cand)
    up_dir = cand_uploads[0] if cand_uploads else None

    return ResolvedFilesRun(
        user=user,
        chat_id=effective_cid,
        run_state=run,
        workspace_dir=ws_dir,
        project_dir=proj_dir,
        uploads_dir=up_dir,
        can_write=can_write,
        upload_dirs=tuple(cand_uploads),
    )


# ---------------------------------------------------------------------------
# 3. Pure-Python File & Search Operations (0 OS Subprocesses)
# ---------------------------------------------------------------------------


def _display_rel(target: Path, root: Path) -> str:
    try:
        rel = target.relative_to(root).as_posix()
        return rel if rel else "."
    except ValueError:
        return target.name


def _sync_read_file(
    run_ctx: ResolvedFilesRun,
    *,
    path: str,
    start_line: int = 1,
    end_line: int = 400,
) -> str:
    target = resolve_safe_workspace_path(
        path,
        run_ctx,
        for_write=False,
        allow_chat_uploads=True,
        allow_root_itself=False,
    )
    if not target.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    if not target.is_file():
        raise ValueError(f"Путь указывает на папку, а не на файл (используйте list_dir): {path}")

    st_size = target.stat().st_size
    with target.open("rb") as fb:
        head = fb.read(4096)
    if b"\x00" in head:
        rel_lbl = _display_rel(target, run_ctx.workspace_dir)
        return f"[Бинарный файл `{rel_lbl}` ({st_size} байт) — текстовое чтение пропущено]"

    s_line = max(1, int(start_line or 1))
    e_line = max(s_line, int(end_line or (s_line + 399)))
    if e_line - s_line + 1 > _MAX_READ_LINES:
        e_line = s_line + _MAX_READ_LINES - 1

    selected: list[str] = []
    total_lines = 0
    used_chars = 0
    truncated_by_chars = False

    with target.open("r", encoding="utf-8", errors="replace") as ft:
        for idx, raw_ln in enumerate(ft, start=1):
            total_lines = idx
            if s_line <= idx <= e_line and not truncated_by_chars:
                clean_ln = raw_ln.rstrip("\r\n")
                formatted = f"{idx}: {clean_ln}"
                if used_chars + len(formatted) + 1 > _MAX_READ_CHARS and selected:
                    truncated_by_chars = True
                else:
                    selected.append(formatted)
                    used_chars += len(formatted) + 1

    rel_lbl = _display_rel(target, run_ctx.workspace_dir)
    if total_lines == 0:
        return f"[Файл `{rel_lbl}` пуст (0 байт)]"
    actual_end = min(total_lines, s_line + len(selected) - 1) if selected else min(total_lines, e_line)
    hdr = f"Файл `{rel_lbl}` — строки {s_line}..{actual_end} из {total_lines}"
    if truncated_by_chars:
        hdr += " (обрезано по лимиту символов; запросите следующий диапазон start_line)"
    return hdr + "\n" + "\n".join(selected)


def _sync_list_dir(
    run_ctx: ResolvedFilesRun,
    *,
    directory_path: str = ".",
    recursive: bool = False,
) -> str:
    target = resolve_safe_workspace_path(
        directory_path or ".",
        run_ctx,
        for_write=False,
        allow_chat_uploads=False,
        allow_root_itself=True,
    )
    if not target.exists():
        raise FileNotFoundError(f"Папка не найдена: {directory_path}")
    if not target.is_dir():
        raise ValueError(f"Путь указывает на файл, а не на папку: {directory_path}")

    ws_root = run_ctx.workspace_dir.resolve()
    rel_lbl = _display_rel(target, ws_root)

    if recursive:
        items_out: list[str] = []
        for root_str, dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
            root_p = Path(root_str)
            kept_dirs: list[str] = []
            for d in sorted(dirnames):
                if d in _SKIP_SEARCH_DIRS or _should_skip_self_hub_subdir(root_p, d, ws_root):
                    continue
                dp = root_p / d
                if _is_reparse_or_symlink(dp) or not _is_within_dir(dp, ws_root):
                    continue
                if _is_forbidden_secret_path(dp.resolve(strict=False), workspace_root=ws_root):
                    continue
                kept_dirs.append(d)
                items_out.append(f"[DIR]  {_display_rel(dp.resolve(strict=False), ws_root)}/")
            dirnames[:] = kept_dirs
            for fn in sorted(filenames):
                fp = root_p / fn
                if _is_reparse_or_symlink(fp):
                    continue
                res_fp = fp.resolve(strict=False)
                if not _is_within_dir(res_fp, ws_root) or _is_forbidden_secret_path(res_fp, workspace_root=ws_root):
                    continue
                try:
                    sz = fp.stat(follow_symlinks=False).st_size
                except OSError:
                    sz = 0
                items_out.append(f"[FILE] {_display_rel(res_fp, ws_root)} ({sz} B)")
                if len(items_out) >= 350:
                    break
            if len(items_out) >= 350:
                break
        if not items_out:
            return f"Папка `{rel_lbl}` пуста."
        capped = items_out[:300]
        extra = f"\n... и ещё {len(items_out) - 300} элем." if len(items_out) > 300 else ""
        return f"Рекурсивное содержимое `{rel_lbl}` ({len(items_out)} элем.):\n" + "\n".join(capped) + extra

    dirs_out: list[str] = []
    files_out: list[str] = []
    with os.scandir(target) as it:
        entries = sorted(it, key=lambda e: e.name.lower())
    for entry in entries:
        ep = Path(entry.path)
        if _is_reparse_or_symlink(ep):
            continue
        if entry.is_dir(follow_symlinks=False) and _should_skip_self_hub_subdir(target, entry.name, ws_root):
            continue
        if _is_forbidden_secret_path(ep.resolve(strict=False), workspace_root=ws_root):
            continue
        if entry.is_dir(follow_symlinks=False):
            dirs_out.append(f"[DIR]  {entry.name}/")
        elif entry.is_file(follow_symlinks=False):
            try:
                sz = entry.stat(follow_symlinks=False).st_size
            except OSError:
                sz = 0
            files_out.append(f"[FILE] {entry.name} ({sz} B)")

    combined = dirs_out + files_out
    if not combined:
        return f"Папка `{rel_lbl}` пуста."
    capped = combined[:300]
    extra = f"\n... и ещё {len(combined) - 300} элем." if len(combined) > 300 else ""
    return f"Содержимое `{rel_lbl}` ({len(dirs_out)} пап., {len(files_out)} файл.):\n" + "\n".join(capped) + extra


def _sync_find_by_name(
    run_ctx: ResolvedFilesRun,
    *,
    pattern: str = "*",
    directory_path: str = ".",
    max_results: int = 100,
) -> str:
    base_dir = resolve_safe_workspace_path(
        directory_path or ".",
        run_ctx,
        for_write=False,
        allow_chat_uploads=False,
        allow_root_itself=True,
    )
    if not base_dir.is_dir():
        raise FileNotFoundError(f"Папка для поиска не найдена: {directory_path}")

    clean_pat = (pattern or "*").strip() or "*"
    norm_pat = clean_pat.replace("\\", "/").lower()
    if norm_pat.startswith("**/"):
        norm_pat_short = norm_pat[3:]
    else:
        norm_pat_short = norm_pat
    has_slash = "/" in norm_pat_short
    cap = max(1, min(250, int(max_results or 100)))

    matches: list[str] = []
    ws_root = run_ctx.workspace_dir.resolve()

    for root_str, dirnames, filenames in os.walk(base_dir, topdown=True, followlinks=False):
        root_p = Path(root_str)
        # Prune skipped, nested self-hub container, secret, or symlink/junction directories in-place
        kept_dirs: list[str] = []
        for d in sorted(dirnames):
            if d in _SKIP_SEARCH_DIRS or _should_skip_self_hub_subdir(root_p, d, ws_root):
                continue
            dp = root_p / d
            if _is_reparse_or_symlink(dp):
                continue
            if not _is_within_dir(dp, ws_root):
                continue
            if _is_forbidden_secret_path(dp.resolve(strict=False), workspace_root=ws_root):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fn in sorted(filenames):
            fp = root_p / fn
            if _is_reparse_or_symlink(fp):
                continue
            res_fp = fp.resolve(strict=False)
            if not _is_within_dir(res_fp, ws_root):
                continue
            if _is_forbidden_secret_path(res_fp, workspace_root=ws_root):
                continue
            rel_posix = res_fp.relative_to(ws_root).as_posix()
            rel_low = rel_posix.lower()
            fn_low = fn.lower()
            matched = (
                fnmatch.fnmatchcase(rel_low, norm_pat)
                or fnmatch.fnmatchcase(rel_low, norm_pat_short)
                or (not has_slash and (fnmatch.fnmatchcase(fn_low, norm_pat_short) or norm_pat_short in fn_low))
            )
            if matched:
                matches.append(rel_posix)
                if len(matches) >= cap:
                    break
        if len(matches) >= cap:
            break

    if not matches:
        return f"По шаблону `{clean_pat}` в `{_display_rel(base_dir, ws_root)}` файлы не найдены."
    return f"Найдено файлов ({len(matches)}):\n" + "\n".join(matches)


def _sync_grep_search(
    run_ctx: ResolvedFilesRun,
    *,
    query: str,
    path: str = ".",
    is_regex: bool = False,
    case_insensitive: bool = False,
    include_glob: str = "",
    max_results: int = 80,
) -> str:
    clean_q = str(query or "")
    if not clean_q:
        raise ValueError("Поисковый запрос `query` не может быть пустым.")
    if len(clean_q) > 500:
        raise ValueError("Поисковый запрос `query` слишком длинный (макс. 500 символов).")

    target = resolve_safe_workspace_path(
        path or ".",
        run_ctx,
        for_write=False,
        allow_chat_uploads=True,
        allow_root_itself=True,
    )
    if not target.exists():
        raise FileNotFoundError(f"Путь для поиска не найден: {path}")

    cap = max(1, min(200, int(max_results or 80)))
    inc_pat = (include_glob or "").strip().replace("\\", "/").lower()
    if inc_pat.startswith("**/"):
        inc_pat = inc_pat[3:]

    regex_obj: Optional[re.Pattern[str]] = None
    if is_regex:
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex_obj = re.compile(clean_q, flags)
        except re.error as exc:
            raise ValueError(f"Некорректное регулярное выражение: {exc}") from exc
    else:
        needle = clean_q.lower() if case_insensitive else clean_q

    ws_root = run_ctx.workspace_dir.resolve()
    candidate_files: list[Path] = []
    if target.is_file():
        candidate_files.append(target)
    else:
        for root_str, dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
            root_p = Path(root_str)
            dirnames[:] = [
                d
                for d in sorted(dirnames)
                if d not in _SKIP_SEARCH_DIRS
                and not _should_skip_self_hub_subdir(root_p, d, ws_root)
                and not _is_reparse_or_symlink(root_p / d)
                and _is_within_dir(root_p / d, ws_root)
                and not _is_forbidden_secret_path((root_p / d).resolve(strict=False), workspace_root=ws_root)
            ]
            for fn in sorted(filenames):
                fp = root_p / fn
                if _is_reparse_or_symlink(fp):
                    continue
                res_fp = fp.resolve(strict=False)
                if not _is_within_dir(res_fp, ws_root):
                    continue
                if _is_forbidden_secret_path(res_fp, workspace_root=ws_root):
                    continue
                if inc_pat:
                    rel_low = res_fp.relative_to(ws_root).as_posix().lower()
                    if not (
                        fnmatch.fnmatchcase(fn.lower(), inc_pat)
                        or fnmatch.fnmatchcase(rel_low, inc_pat)
                    ):
                        continue
                candidate_files.append(res_fp)
                if len(candidate_files) >= 3000:
                    break
            if len(candidate_files) >= 3000:
                break

    hits: list[str] = []
    skipped_large = 0
    for fp in candidate_files:
        try:
            st_sz = fp.stat().st_size
            if st_sz == 0:
                continue
            if st_sz > _MAX_GREP_FILE_BYTES:
                skipped_large += 1
                continue
            with fp.open("rb") as fb:
                sample = fb.read(2048)
            if b"\x00" in sample:
                continue
            rel_lbl = _display_rel(fp, ws_root)
            with fp.open("r", encoding="utf-8", errors="replace") as ft:
                for ln_no, raw_ln in enumerate(ft, start=1):
                    line_txt = raw_ln.rstrip("\r\n")
                    hay = line_txt[:4000]
                    if regex_obj is not None:
                        ok = bool(regex_obj.search(hay))
                    else:
                        ok = (needle in hay.lower()) if case_insensitive else (needle in hay)
                    if ok:
                        snippet = line_txt.strip()
                        if len(snippet) > 220:
                            snippet = snippet[:220] + "…"
                        hits.append(f"{rel_lbl}:{ln_no}: {snippet}")
                        if len(hits) >= cap:
                            break
        except OSError:
            continue
        if len(hits) >= cap:
            break

    if not hits:
        if skipped_large > 0:
            return (
                f"Совпадений по запросу `{clean_q}` не найдено "
                f"(пропущено файлов > 8 МиБ: {skipped_large})."
            )
        return f"Совпадений по запросу `{clean_q}` не найдено."
    return f"Найдено совпадений ({len(hits)}):\n" + "\n".join(hits)


def _atomic_write_project_file(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as fw:
            fw.write(content)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _record_active_file(run_ctx: ResolvedFilesRun, target: Path) -> str:
    base = run_ctx.project_dir or run_ctx.workspace_dir
    rel = format_rel_project_path(str(target), base)
    if rel:
        run_ctx.run_state.active_files.add(rel)
    return rel or _display_rel(target, base)


def _sync_write_file(
    run_ctx: ResolvedFilesRun,
    *,
    path: str,
    content: str,
    overwrite: bool = True,
) -> str:
    if not run_ctx.can_write or run_ctx.project_dir is None:
        raise PermissionError(
            "Запись файлов запрещена в текущем режиме доступа (`read_only` / вне проекта). "
            "Переключите вкладку в режим «Код» или «Терминал»."
        )
    if len(content) > _MAX_WRITE_CHARS:
        raise ValueError(f"Содержимое файла превышает лимит ({_MAX_WRITE_CHARS} символов).")

    target = resolve_safe_workspace_path(
        path,
        run_ctx,
        for_write=True,
        allow_chat_uploads=False,
        allow_root_itself=False,
    )
    if target.exists():
        if target.is_dir():
            raise ValueError(f"Невозможно перезаписать директорию файлом: {path}")
        if not overwrite:
            raise ValueError(f"Файл `{path}` уже существует (передайте overwrite=true для перезаписи).")

    _atomic_write_project_file(target, content)
    rel = _record_active_file(run_ctx, target)
    line_cnt = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return f"Файл `{rel}` успешно записан ({len(content.encode('utf-8'))} байт, {line_cnt} стр.)."


def _sync_edit_file(
    run_ctx: ResolvedFilesRun,
    *,
    path: str,
    target: str,
    replacement: str,
    replace_all: bool = False,
) -> str:
    if not run_ctx.can_write or run_ctx.project_dir is None:
        raise PermissionError(
            "Редактирование файлов запрещено в текущем режиме доступа (`read_only` / вне проекта). "
            "Переключите вкладку в режим «Код» или «Терминал»."
        )
    if not target:
        raise ValueError("Параметр `target` (искомый фрагмент для замены) не может быть пустым.")

    file_path = resolve_safe_workspace_path(
        path,
        run_ctx,
        for_write=True,
        allow_chat_uploads=False,
        allow_root_itself=False,
    )
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"Файл для редактирования не найден: {path}")

    raw_text = file_path.read_bytes().decode("utf-8")
    had_crlf = "\r\n" in raw_text
    exact_count = raw_text.count(target)
    if exact_count > 0:
        if not replace_all and exact_count > 1:
            raise ValueError(
                f"Фрагмент `target` встречается в файле `{path}` {exact_count} раз(а). "
                "Передайте больше соседних строк для однозначного совпадения или установите replace_all=true."
            )
        eff_repl = (
            replacement.replace("\r\n", "\n").replace("\n", "\r\n")
            if had_crlf
            else replacement
        )
        new_text = raw_text.replace(target, eff_repl) if replace_all else raw_text.replace(target, eff_repl, 1)
        replaced_n = exact_count if replace_all else 1
    else:
        # Resilient CRLF <-> LF matching while preserving the file's dominant line ending
        norm_text = raw_text.replace("\r\n", "\n")
        norm_target = target.replace("\r\n", "\n")
        norm_repl = replacement.replace("\r\n", "\n")
        norm_count = norm_text.count(norm_target)
        if norm_count == 0:
            raise ValueError(
                f"Фрагмент `target` не найден в `{path}`. Прочитайте актуальные строки через `read_file`."
            )
        if not replace_all and norm_count > 1:
            raise ValueError(
                f"Фрагмент `target` встречается в файле `{path}` {norm_count} раз(а). "
                "Передайте больше соседних строк для однозначного совпадения или установите replace_all=true."
            )
        updated_norm = (
            norm_text.replace(norm_target, norm_repl)
            if replace_all
            else norm_text.replace(norm_target, norm_repl, 1)
        )
        new_text = updated_norm.replace("\n", "\r\n") if had_crlf else updated_norm
        replaced_n = norm_count if replace_all else 1

    if len(new_text) > _MAX_WRITE_CHARS:
        raise ValueError(f"Результирующий файл превышает лимит ({_MAX_WRITE_CHARS} символов).")

    _atomic_write_project_file(file_path, new_text)
    rel = _record_active_file(run_ctx, file_path)
    return f"Файл `{rel}` обновлён (заменено вхождений: {replaced_n})."


# ---------------------------------------------------------------------------
# 4. MCP Tool Schemas (`annotations` included) & JSON-RPC 2.0 Dispatcher
# ---------------------------------------------------------------------------


def _build_files_mcp_tools_schema(*, include_write_tools: bool) -> list[dict[str, Any]]:
    read_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    project_prop = {
        "type": "string",
        "description": "Необязательное имя проекта (нужно только если параллельно открыты ходы в разных проектах).",
        "default": "",
    }
    tools: list[dict[str, Any]] = [
        {
            "name": "read_file",
            "description": (
                "Прочитать текстовый файл UTF-8 с номерами строк в рабочей папке проекта "
                "(или прикреплённый файл текущего диалога). За один вызов возвращает до 800 строк."
            ),
            "annotations": {**read_annotations, "title": "Read Workspace File"},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Относительный путь к файлу внутри проекта (или путь к вложению чата).",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Начальная строка (с 1, включительно). По умолчанию 1.",
                        "default": 1,
                        "minimum": 1,
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Конечная строка (включительно). По умолчанию 400.",
                        "default": 400,
                        "minimum": 1,
                    },
                    "project": dict(project_prop),
                },
                "required": ["path"],
            },
        },
        {
            "name": "list_dir",
            "description": "Показать список подпапок и файлов (с размерами) в указанной директории проекта.",
            "annotations": {**read_annotations, "title": "List Workspace Directory"},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "directory_path": {
                        "type": "string",
                        "description": "Относительный путь к папке внутри рабочей директории (по умолчанию '.').",
                        "default": ".",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Рекурсивно обойти вложенные подпапки (по умолчанию false).",
                        "default": False,
                    },
                    "project": dict(project_prop),
                },
            },
        },
        {
            "name": "find_by_name",
            "description": (
                "Быстрый рекурсивный поиск файлов по имени или glob-шаблону (например `*.py`, `*auth*`, `src/**/*.ts`) "
                "внутри рабочей папки проекта."
            ),
            "annotations": {**read_annotations, "title": "Find Files by Name"},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Подстрока имени файла или glob-шаблон (по умолчанию '*').",
                        "default": "*",
                    },
                    "directory_path": {
                        "type": "string",
                        "description": "Относительная подпапка для поиска (по умолчанию '.').",
                        "default": ".",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Максимум результатов (1..250, по умолчанию 100).",
                        "default": 100,
                        "minimum": 1,
                        "maximum": 250,
                    },
                    "project": dict(project_prop),
                },
            },
        },
        {
            "name": "grep_search",
            "description": (
                "Поиск точной подстроки или регулярного выражения по содержимому файлов рабочей папки "
                "с возвратом `путь:строка: фрагмент`."
            ),
            "annotations": {**read_annotations, "title": "Search File Contents"},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Искомая строка или регулярное выражение.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Относительный путь к папке или конкретному файлу (по умолчанию '.').",
                        "default": ".",
                    },
                    "is_regex": {
                        "type": "boolean",
                        "description": "Интерпретировать query как регулярное выражение Python (по умолчанию false).",
                        "default": False,
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "Поиск без учёта регистра (по умолчанию false).",
                        "default": False,
                    },
                    "include_glob": {
                        "type": "string",
                        "description": "Необязательный glob-фильтр имён файлов (например `*.py` или `*.js`).",
                        "default": "",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Максимум совпадений (1..200, по умолчанию 80).",
                        "default": 80,
                        "minimum": 1,
                        "maximum": 200,
                    },
                    "project": dict(project_prop),
                },
                "required": ["query"],
            },
        },
    ]

    if include_write_tools:
        tools.extend(
            [
                {
                    "name": "write_file",
                    "description": (
                        "Создать новый файл или атомарно перезаписать существующий текстовый файл UTF-8 "
                        "строго внутри папки текущего проекта."
                    ),
                    "annotations": {
                        "title": "Write Project File",
                        "readOnlyHint": False,
                        "destructiveHint": False,
                        "idempotentHint": True,
                        "openWorldHint": False,
                    },
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Относительный путь к файлу внутри папки проекта.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Полное текстовое содержимое файла (UTF-8).",
                            },
                            "overwrite": {
                                "type": "boolean",
                                "description": "Разрешить перезапись существующего файла (по умолчанию true).",
                                "default": True,
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
                {
                    "name": "edit_file",
                    "description": (
                        "Точечно заменить фрагмент `target` на `replacement` в существующем файле проекта "
                        "(атомарная запись с сохранением кодировки UTF-8 и переводов строк)."
                    ),
                    "annotations": {
                        "title": "Edit Project File",
                        "readOnlyHint": False,
                        "destructiveHint": False,
                        "idempotentHint": False,
                        "openWorldHint": False,
                    },
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Относительный путь к изменяемому файлу внутри папки проекта.",
                            },
                            "target": {
                                "type": "string",
                                "description": "Точный существующий фрагмент текста (включая отступы) для замены.",
                            },
                            "replacement": {
                                "type": "string",
                                "description": "Новый текст, подставляемый вместо target.",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "Заменить все вхождения target в файле (по умолчанию false — только уникальное).",
                                "default": False,
                            },
                        },
                        "required": ["path", "target", "replacement"],
                    },
                },
            ]
        )
    return tools


def _files_tool_error(req_id: Any, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": f"Ошибка hub_files: {message}"}],
            "isError": True,
        },
    }


async def _dispatch_files_jsonrpc_message(
    msg: dict[str, Any],
    user: dict[str, Any],
    chat_id: str,
) -> Optional[dict[str, Any]]:
    method = str(msg.get("method") or "").strip()
    has_id = "id" in msg
    req_id = msg.get("id")

    if not has_id or method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        client_proto = str(params.get("protocolVersion") or "").strip()
        negotiated = client_proto if client_proto in _SUPPORTED_MCP_PROTOCOLS else MCP_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": MCP_FILES_SERVER_NAME,
                    "version": "94.0.0",
                },
            },
        }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    if method == "tools/list":
        can_write = False
        if chat_id != USER_SCOPED_FILES_CHAT_ID:
            try:
                run_ctx = resolve_active_files_run(user, chat_id)
                can_write = run_ctx.can_write
            except PermissionError:
                can_write = False
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _build_files_mcp_tools_schema(include_write_tools=can_write)},
        }

    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        tool_name = str(params.get("name") or "").strip()
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}

        if tool_name not in MCP_FILES_ALL_TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Неизвестный инструмент hub_files: {tool_name}"},
            }

        if chat_id == USER_SCOPED_FILES_CHAT_ID and tool_name not in MCP_FILES_READ_TOOLS:
            return _files_tool_error(
                req_id,
                "Пользовательский токен hub_files (`_user`) работает строго в режиме только чтения (read-only); запись и редактирование файлов запрещены.",
            )

        req_project = str(args.get("project") or args.get("Project") or "").strip()
        hint_path = str(
            args.get("path")
            or args.get("Path")
            or args.get("directory_path")
            or args.get("DirectoryPath")
            or ""
        ).strip()
        try:
            run_ctx = resolve_active_files_run(
                user,
                chat_id,
                requested_project=req_project,
                hint_path=hint_path,
            )
        except PermissionError as perm_err:
            return _files_tool_error(req_id, str(perm_err))

        try:
            if tool_name == "read_file":
                out_text = await asyncio.to_thread(
                    _sync_read_file,
                    run_ctx,
                    path=str(args.get("path") or args.get("Path") or ""),
                    start_line=int(args.get("start_line") if args.get("start_line") is not None else 1),
                    end_line=int(args.get("end_line") if args.get("end_line") is not None else 400),
                )
            elif tool_name == "list_dir":
                out_text = await asyncio.to_thread(
                    _sync_list_dir,
                    run_ctx,
                    directory_path=str(
                        args.get("directory_path")
                        if args.get("directory_path") is not None
                        else (args.get("path") if args.get("path") is not None else ".")
                    ),
                    recursive=bool(args.get("recursive", False)),
                )
            elif tool_name == "find_by_name":
                out_text = await asyncio.to_thread(
                    _sync_find_by_name,
                    run_ctx,
                    pattern=str(args.get("pattern") if args.get("pattern") is not None else "*"),
                    directory_path=str(
                        args.get("directory_path")
                        if args.get("directory_path") is not None
                        else (args.get("path") if args.get("path") is not None else ".")
                    ),
                    max_results=int(args.get("max_results") if args.get("max_results") is not None else 100),
                )
            elif tool_name == "grep_search":
                out_text = await asyncio.to_thread(
                    _sync_grep_search,
                    run_ctx,
                    query=str(args.get("query") if args.get("query") is not None else ""),
                    path=str(args.get("path") if args.get("path") is not None else "."),
                    is_regex=bool(args.get("is_regex", False)),
                    case_insensitive=bool(args.get("case_insensitive", False)),
                    include_glob=str(args.get("include_glob") or ""),
                    max_results=int(args.get("max_results") if args.get("max_results") is not None else 80),
                )
            elif tool_name == "write_file":
                if "content" not in args and "Content" not in args:
                    raise ValueError("Обязательный строковый параметр `content` не передан.")
                raw_content = args.get("content") if "content" in args else args.get("Content")
                out_text = await asyncio.to_thread(
                    _sync_write_file,
                    run_ctx,
                    path=str(args.get("path") or args.get("Path") or ""),
                    content=str(raw_content if raw_content is not None else ""),
                    overwrite=bool(args.get("overwrite", True)),
                )
            elif tool_name == "edit_file":
                out_text = await asyncio.to_thread(
                    _sync_edit_file,
                    run_ctx,
                    path=str(args.get("path") or args.get("Path") or ""),
                    target=str(args.get("target") if args.get("target") is not None else (args.get("Target") or "")),
                    replacement=str(
                        args.get("replacement")
                        if args.get("replacement") is not None
                        else (args.get("Replacement") or "")
                    ),
                    replace_all=bool(args.get("replace_all", False)),
                )
            else:
                return _files_tool_error(req_id, f"Инструмент не поддерживается: {tool_name}")

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": out_text}],
                    "isError": False,
                },
            }
        except (PermissionError, FileNotFoundError, ValueError) as user_err:
            return _files_tool_error(req_id, str(user_err))
        except Exception as exc:
            return _files_tool_error(req_id, f"Внутренняя ошибка файлового шлюза: {exc}")

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Метод JSON-RPC не поддерживается: {method}"},
    }


async def handle_mcp_files_http(request: Request, mcp_token: str) -> Response:
    """FastAPI Streamable HTTP MCP handler for `/mcp/files/{mcp_token}`."""
    if not is_loopback_mcp_request(request):
        return JSONResponse(
            status_code=403,
            content={"error": "Доступ к MCP файловой системы разрешён строго с loopback хоста (127.0.0.1 / ::1)."},
        )

    auth_pair = await _authenticate_files_user_from_token(mcp_token)
    if not auth_pair:
        return JSONResponse(
            status_code=403,
            content={"error": "MCP-токен hub_files недействителен, отозван или сессия завершена."},
        )
    user, token_chat_id = auth_pair

    qs_chat_id = str(request.query_params.get("chat_id") or "").strip()
    if qs_chat_id and qs_chat_id != token_chat_id:
        return JSONResponse(
            status_code=403,
            content={"error": "Несовпадение chat_id с подписью MCP-токена hub_files."},
        )

    method = request.method.upper()
    if method == "GET":
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})
    if method == "DELETE":
        return Response(status_code=200)
    if method != "POST":
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    try:
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Некорректный JSON в запросе MCP hub_files"},
            },
        )

    if isinstance(payload, list):
        if not payload:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Пустой пакет JSON-RPC"},
                },
            )
        responses: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, dict):
                resp_item = await _dispatch_files_jsonrpc_message(item, user, token_chat_id)
                if resp_item is not None:
                    responses.append(resp_item)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(status_code=200, content=responses)

    if isinstance(payload, dict):
        single = await _dispatch_files_jsonrpc_message(payload, user, token_chat_id)
        if single is None:
            return Response(status_code=202)
        return JSONResponse(status_code=200, content=single)

    return JSONResponse(
        status_code=400,
        content={
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Ожидался JSON-объект или массив JSON-RPC"},
        },
    )
