from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import uuid
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config
from .config import (
    AGY_APP_DATA_DIR,
    AGY_CLI_DATA_DIR,
    DATA_DIR,
    PROJECTS_ROOT,
    SHARED_PROJECTS_DIR,
    USER_WORKSPACES_DIR,
)

_ORIG_DATA_DIR = DATA_DIR
_ORIG_PROJECTS_ROOT = PROJECTS_ROOT
_ORIG_SHARED_PROJECTS_DIR = SHARED_PROJECTS_DIR
_ORIG_USER_WORKSPACES_DIR = USER_WORKSPACES_DIR
_ORIG_AGY_APP_DATA_DIR = AGY_APP_DATA_DIR
_ORIG_AGY_CLI_DATA_DIR = AGY_CLI_DATA_DIR


def _eff_agy_app_data_dir() -> Path:
    return (
        AGY_APP_DATA_DIR
        if AGY_APP_DATA_DIR != _ORIG_AGY_APP_DATA_DIR
        else config.AGY_APP_DATA_DIR
    )


def _eff_agy_cli_data_dir() -> Path:
    return (
        AGY_CLI_DATA_DIR
        if AGY_CLI_DATA_DIR != _ORIG_AGY_CLI_DATA_DIR
        else config.AGY_CLI_DATA_DIR
    )


def _eff_data_dir() -> Path:
    return DATA_DIR if DATA_DIR != _ORIG_DATA_DIR else config.DATA_DIR


def _eff_projects_root() -> Path:
    return PROJECTS_ROOT if PROJECTS_ROOT != _ORIG_PROJECTS_ROOT else config.PROJECTS_ROOT


def _eff_shared_projects_dir() -> Path:
    return (
        SHARED_PROJECTS_DIR
        if SHARED_PROJECTS_DIR != _ORIG_SHARED_PROJECTS_DIR
        else config.SHARED_PROJECTS_DIR
    )


def _eff_user_workspaces_dir() -> Path:
    if USER_WORKSPACES_DIR != _ORIG_USER_WORKSPACES_DIR:
        return USER_WORKSPACES_DIR.resolve()
    return config.get_effective_users_dir()


def is_self_hub_project_name(name: str) -> bool:
    """Return True if `name` refers to the self-hosted `Antigravity_Hub` core workspace."""
    clean = (name or "").strip().lower()
    if not clean:
        return False
    return clean in {"antigravity_hub", config.BASE_DIR.name.lower()}


def _is_path_within(child: Path, parent: Path) -> bool:
    try:
        c_res = child.resolve()
        p_res = parent.resolve()
        return c_res == p_res or p_res in c_res.parents
    except Exception:
        return False


def ensure_isolated_project_git_boundary(proj_dir: Path) -> None:
    """Idempotently ensure `proj_dir` has its own `.git` boundary when located inside
    `SHARED_PROJECTS_DIR`, `USERS_DIR` (`users/*/projects/*`, `users/*/chats/*`),
    or any parent directory containing `.git`.

    Never touches `config.BASE_DIR` itself (which holds the real `Antigravity_Hub/.git`).
    """
    if not proj_dir or not proj_dir.exists() or not proj_dir.is_dir():
        return
    try:
        resolved = proj_dir.resolve()
        if resolved == config.BASE_DIR.resolve():
            return
    except OSError:
        return

    git_marker = proj_dir / ".git"
    if git_marker.exists():
        return

    needs_boundary = (
        _is_path_within(resolved, _eff_shared_projects_dir())
        or _is_path_within(resolved, _eff_user_workspaces_dir())
        or any((parent / ".git").exists() for parent in resolved.parents)
    )
    if not needs_boundary:
        return

    git_bin = shutil.which("git")
    if git_bin:
        env = os.environ.copy()
        iso_home = str(_eff_data_dir().resolve())
        env["HOME"] = iso_home
        env["USERPROFILE"] = iso_home
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["CUDA_VISIBLE_DEVICES"] = ""
        try:
            subprocess.run(
                [git_bin, "init", "-q"],
                cwd=str(resolved),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
                **config.subprocess_hidden_kwargs(),
            )
        except Exception:
            pass

    if not git_marker.exists():
        try:
            (git_marker / "objects").mkdir(parents=True, exist_ok=True)
            (git_marker / "refs" / "heads").mkdir(parents=True, exist_ok=True)
            (git_marker / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (git_marker / "config").write_text(
                "[core]\n\trepositoryformatversion = 0\n\tfilemode = false\n\tbare = false\n",
                encoding="utf-8",
            )
        except OSError:
            pass


def infer_shared_project_storage_location(path_str: str) -> str:
    raw = (path_str or "").strip()
    if not raw:
        return "shared_dir"
    try:
        resolved = Path(raw).resolve()
        if resolved.parent == _eff_projects_root().resolve() and resolved.parent != _eff_shared_projects_dir().resolve():
            return "projects_root"
    except Exception:
        pass
    return "shared_dir"


def validate_shared_project_path(
    name: str,
    storage_location: str = "shared_dir",
    existing_path: str = "",
) -> Path:
    """Strictly validate and resolve a Shared Project path inside either `SHARED_PROJECTS_DIR`
    or `PROJECTS_ROOT`, blocking `BASE_DIR` (`Antigravity_Hub`), `DATA_DIR`, `CLI_HOMES_DIR`,
    symlinks/junctions, Windows reserved names, and `..` traversal."""
    clean_name = (name or "").strip()
    if (
        not clean_name
        or not _SAFE_FOLDER_RE.fullmatch(clean_name)
        or ".." in clean_name
        or "/" in clean_name
        or "\\" in clean_name
        or clean_name in {".", ".."}
        or clean_name.endswith(".")
        or clean_name.upper().split(".")[0] in config._WIN_RESERVED_NAMES
    ):
        raise ValueError(
            "Название общего проекта должно содержать от 2 до 64 допустимых символов (без слэшей, '..' и системных имён)"
        )

    if clean_name.lower() in config.get_reserved_project_names_lower():
        raise ValueError(
            f"Имя '{clean_name}' зарезервировано системой Antigravity Hub и не может использоваться для проекта"
        )

    loc = (storage_location or "").strip().lower()
    if loc not in {"", "shared_dir", "projects_root"}:
        raise ValueError("Недопустимое расположение папки общего проекта")

    shared_root = _eff_shared_projects_dir().resolve()
    projects_root = _eff_projects_root().resolve()
    users_root = _eff_user_workspaces_dir().resolve()
    base_dir = config.BASE_DIR.resolve()
    data_dir = _eff_data_dir().resolve()
    cli_homes_dir = config.CLI_HOMES_DIR.resolve()

    raw_existing = (existing_path or "").strip()
    if raw_existing and not loc:
        raw_parts = [seg for seg in re.split(r"[\\/]+", raw_existing) if seg]
        if ".." in raw_parts:
            raise ValueError("Недопустимый путь общего проекта ('..')")
        raw_p = Path(raw_existing)
        if (os.path.lexists(str(raw_p)) or raw_p.exists()) and config._is_junction_or_symlink(raw_p):
            raise ValueError("Симлинки и junction-ссылки запрещены для папки общего проекта")
        try:
            resolved = raw_p.resolve()
        except OSError as exc:
            raise ValueError("Некорректный путь общего проекта") from exc
    else:
        eff_loc = loc or "shared_dir"
        target_root = projects_root if eff_loc == "projects_root" else shared_root
        if (os.path.lexists(str(target_root)) or target_root.exists()) and config._is_junction_or_symlink(target_root):
            raise ValueError("Корневой каталог проектов не может быть симлинком")
        raw_p = target_root / clean_name
        if (os.path.lexists(str(raw_p)) or raw_p.exists()) and config._is_junction_or_symlink(raw_p):
            raise ValueError("Симлинки и junction-ссылки запрещены для папки общего проекта")
        try:
            resolved = raw_p.resolve()
        except OSError as exc:
            raise ValueError("Некорректный путь общего проекта") from exc
        if resolved.parent != target_root:
            raise ValueError("Выход за пределы выбранного корня проектов запрещён")

    in_shared = resolved.parent == shared_root
    in_projects = resolved.parent == projects_root
    if not (in_shared or in_projects):
        raise ValueError(
            "Путь общего проекта должен быть прямой подпапкой shared_projects или корня проектов"
        )

    if (
        resolved in (shared_root, projects_root)
        or ".." in resolved.name
        or resolved.name.lower() in config.get_reserved_project_names_lower()
        or resolved.name.startswith("_")
        or resolved.name.upper().split(".")[0] in config._WIN_RESERVED_NAMES
    ):
        raise ValueError("Недопустимое системное имя папки общего проекта")

    if resolved.exists() and not resolved.is_dir():
        raise ValueError("Указанный путь существует, но не является папкой")

    # Hard Zero-Trust Denylist: block BASE_DIR itself (or ancestors), Hub core dirs, and non-admin user dirs
    if resolved == base_dir or _is_path_within(base_dir, resolved):
        raise ValueError(
            "Запрещено указывать корневую директорию сервера Antigravity Hub в качестве общего проекта"
        )

    core_protected = config.get_hub_core_protected_paths()
    if any(
        _is_path_within(resolved, cp) or _is_path_within(cp, resolved)
        for cp in core_protected
        if not (in_shared and _is_path_within(shared_root, data_dir) and cp == data_dir)
    ):
        raise ValueError(
            "Запрещено указывать системные директории ядра Antigravity Hub в качестве общего проекта"
        )

    # Block non-admin user directories `users/<non_admin>/...`
    if _is_path_within(resolved, users_root):
        admin_proj_dir = (users_root / "admin" / "projects").resolve()
        if not (in_projects and (projects_root == admin_proj_dir or resolved.parent == projects_root and not _is_path_within(projects_root, users_root))):
            try:
                rel_u = resolved.relative_to(users_root).parts
            except ValueError:
                rel_u = ()
            if len(rel_u) < 3 or rel_u[0].lower() != "admin" or rel_u[1].lower() != "projects":
                raise ValueError("Запрещено указывать личные каталоги других пользователей для общего проекта")

    forbidden_internal_dirs = (
        cli_homes_dir,
        config.SESSIONS_DIR.resolve(),
        config.CERTS_DIR.resolve(),
        config.CHAT_SANDBOX_DIR.resolve(),
        config.UPLOADS_DIR.resolve(),
        config.SANDBOX_UPLOADS_DIR.resolve(),
        (shared_root / "_uploads").resolve(),
        (shared_root / "_context").resolve(),
    )
    if resolved == data_dir or any(_is_path_within(resolved, fd) for fd in forbidden_internal_dirs):
        raise ValueError("Запрещено указывать служебные директории данных Antigravity Hub")

    if (
        not in_shared
        and not _is_path_within(projects_root, data_dir)
        and _is_path_within(resolved, data_dir)
    ):
        raise ValueError("Запрещено указывать подпапки системного каталога данных Antigravity Hub")

    return resolved


MAX_PROJECT_EXTRA_DIRS = 12


def inspect_project_dir_health(path: Path) -> dict[str, Any]:
    """Inspect whether a project folder is an empty Hub skeleton (only .git/.agents/stub README.md <= 300B)."""
    if not path:
        return {
            "exists": False,
            "is_empty_skeleton": False,
            "real_entries_count": 0,
            "has_stub_readme": False,
        }
    try:
        if not path.exists() or not path.is_dir():
            return {
                "exists": False,
                "is_empty_skeleton": False,
                "real_entries_count": 0,
                "has_stub_readme": False,
            }
        real_entries = 0
        has_stub_readme = False
        with os.scandir(path) as it:
            for entry in it:
                ename_low = entry.name.lower()
                if ename_low in {".git", ".agents"}:
                    continue
                if ename_low == "readme.md":
                    try:
                        if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_size <= 300:
                            has_stub_readme = True
                            continue
                    except OSError:
                        pass
                real_entries += 1
        return {
            "exists": True,
            "is_empty_skeleton": real_entries == 0,
            "real_entries_count": real_entries,
            "has_stub_readme": has_stub_readme,
        }
    except OSError:
        return {
            "exists": False,
            "is_empty_skeleton": False,
            "real_entries_count": 0,
            "has_stub_readme": False,
        }


def validate_project_extra_dirs(
    paths: list[str] | tuple[str, ...] | None,
    *,
    user: Optional[dict[str, Any]],
    exec_mode: str,
    project_dir: Optional[Path] = None,
    strict: bool = True,
) -> list[Path]:
    """Zero-Trust validator for Extra Workspace Directories (--add-dir).

    Allowed strictly for role == 'admin' in exec_mode == 'host'.
    Blocks non-existent dirs, '..' traversal, symlinks/junctions, drive roots,
    system OS directories, and Hub/CLI sensitive vaults.
    """
    if not paths:
        return []

    is_admin = bool(user and str(user.get("role") or "").strip().lower() == "admin")
    eff_mode = str(exec_mode or "").strip().lower()
    if not is_admin or eff_mode != "host":
        if strict:
            raise ValueError(
                "Дополнительные рабочие папки доступны только администратору в контуре прямого терминала ПК (Host)"
            )
        return []

    data_dir = _eff_data_dir().resolve()
    cli_homes_dir = config.CLI_HOMES_DIR.resolve()
    agy_app_dir = _eff_agy_app_data_dir().resolve()
    agy_cli_dir = _eff_agy_cli_data_dir().resolve()
    sessions_dir = config.SESSIONS_DIR.resolve()
    certs_dir = config.CERTS_DIR.resolve()
    base_dir = config.BASE_DIR.resolve()
    home_dir = Path.home().resolve()

    critical_vaults: list[Path] = [
        *config.get_hub_core_protected_paths(),
        data_dir,
        cli_homes_dir,
        agy_app_dir,
        agy_cli_dir,
        sessions_dir,
        certs_dir,
        (_eff_shared_projects_dir().resolve() / "_uploads").resolve(),
        (_eff_shared_projects_dir().resolve() / "_context").resolve(),
    ]
    for dot_secret in (".ssh", ".gnupg", ".aws", ".kube", ".gemini", ".claude", ".codex", ".docker"):
        critical_vaults.append((home_dir / dot_secret).resolve())

    os_forbidden: list[Path] = []
    if os.name == "nt":
        for env_k, fallback in (
            ("SystemRoot", r"C:\Windows"),
            ("ProgramFiles", r"C:\Program Files"),
            ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            ("ProgramData", r"C:\ProgramData"),
        ):
            raw_sys = os.environ.get(env_k) or fallback
            if raw_sys:
                try:
                    os_forbidden.append(Path(raw_sys).resolve())
                except OSError:
                    pass
        appdata_roaming = os.environ.get("APPDATA")
        if appdata_roaming:
            try:
                os_forbidden.append(Path(appdata_roaming).resolve())
            except OSError:
                pass
    else:
        for posix_sys in ("/etc", "/proc", "/sys", "/root", "/boot", "/dev", "/var/run"):
            try:
                os_forbidden.append(Path(posix_sys).resolve())
            except OSError:
                pass

    local_appdata: Optional[Path] = None
    temp_root: Optional[Path] = None
    if os.name == "nt":
        raw_local = os.environ.get("LOCALAPPDATA")
        if raw_local:
            try:
                local_appdata = Path(raw_local).resolve()
            except OSError:
                local_appdata = None
        try:
            temp_root = Path(tempfile.gettempdir()).resolve()
        except OSError:
            temp_root = None

    resolved_proj_dir: Optional[Path] = None
    if project_dir is not None:
        try:
            resolved_proj_dir = project_dir.resolve()
        except OSError:
            resolved_proj_dir = None

    out: list[Path] = []
    seen: set[str] = set()

    def _fail(msg: str) -> None:
        if strict:
            raise ValueError(msg)

    for raw_item in paths:
        raw = str(raw_item or "").strip()
        if not raw:
            continue
        if "\x00" in raw:
            _fail("Некорректный путь дополнительной папки")
            continue
        if raw.startswith("\\\\") or raw.startswith("//"):
            _fail(f"Сетевые UNC-пути запрещены: {raw}")
            continue
        raw_parts = [seg for seg in re.split(r"[\\/]+", raw) if seg]
        if ".." in raw_parts:
            _fail(f"Переходы '..' в пути запрещены: {raw}")
            continue

        raw_p = Path(raw)
        if not raw_p.is_absolute():
            _fail(f"Укажите полный абсолютный путь к папке: {raw}")
            continue

        # Reject symlinks / NTFS junctions anywhere along the path chain
        has_reparse = False
        for check_p in (raw_p, *raw_p.parents):
            try:
                if (os.path.lexists(str(check_p)) or check_p.exists()) and config._is_junction_or_symlink(check_p):
                    has_reparse = True
                    break
            except OSError:
                pass
        if has_reparse:
            _fail(f"Симлинки и junction-ссылки запрещены для дополнительных папок: {raw}")
            continue

        try:
            resolved = raw_p.resolve()
        except OSError:
            _fail(f"Некорректный путь к папке: {raw}")
            continue

        if not resolved.exists() or not resolved.is_dir():
            _fail(f"Папка не существует или недоступна: {raw}")
            continue

        # Block drive roots (C:\, D:\, /) and user home root (C:\Users\<user>)
        if resolved.parent == resolved:
            _fail(f"Запрещено подключать корень диска целиком: {resolved}")
            continue
        if resolved == home_dir or home_dir in (resolved,):
            _fail(f"Запрещено подключать корневой профиль пользователя целиком: {resolved}")
            continue

        if resolved_proj_dir is not None and resolved == resolved_proj_dir:
            _fail("Эта папка уже является основной директорией проекта")
            continue

        # Block BASE_DIR itself (or any parent directory containing BASE_DIR) and top-level container roots,
        # while allowing individual sibling projects inside `users/admin/projects/*` or `shared_projects/*`.
        if (
            resolved == base_dir
            or _is_path_within(base_dir, resolved)
            or resolved in (
                _eff_user_workspaces_dir().resolve(),
                _eff_shared_projects_dir().resolve(),
            )
        ):
            _fail(f"Запрещено подключать корневую или системную директорию контейнера Хаба: {resolved}")
            continue

        # Block critical vaults (both inside vault AND parent folders that would expose a vault)
        vault_hit = False
        for v in critical_vaults:
            if _is_path_within(resolved, v) or _is_path_within(v, resolved):
                vault_hit = True
                break
        if vault_hit:
            _fail(f"Запрещено подключать системные или секретные директории: {resolved}")
            continue

        # Block OS system roots (and parents of OS system roots)
        sys_hit = False
        for sf in os_forbidden:
            if _is_path_within(resolved, sf) or _is_path_within(sf, resolved):
                sys_hit = True
                break
        if sys_hit:
            _fail(f"Запрещено подключать системные каталоги ОС: {resolved}")
            continue

        # Block AppData\Local except isolated subdirectories inside %TEMP%
        if local_appdata is not None:
            if _is_path_within(local_appdata, resolved):
                _fail(f"Запрещено подключать каталог, содержащий AppData: {resolved}")
                continue
            if _is_path_within(resolved, local_appdata):
                allowed_in_temp = (
                    temp_root is not None
                    and resolved != temp_root
                    and _is_path_within(resolved, temp_root)
                )
                if not allowed_in_temp:
                    _fail(f"Запрещено подключать каталоги AppData: {resolved}")
                    continue

        if temp_root is not None and resolved == temp_root:
            _fail(f"Запрещено подключать корневой каталог Temp целиком: {resolved}")
            continue

        norm_key = str(resolved).replace("/", "\\").rstrip("\\").lower()
        if norm_key in seen:
            continue
        seen.add(norm_key)
        out.append(resolved)
        if len(out) > MAX_PROJECT_EXTRA_DIRS:
            _fail(f"Превышен лимит дополнительных папок (максимум {MAX_PROJECT_EXTRA_DIRS})")
            return out[:MAX_PROJECT_EXTRA_DIRS]

    return out

_SAFE_FOLDER_RE = re.compile(r"^[a-zA-Z0-9а-яА-ЯёЁ_.\- ]{2,64}$")

_USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_STRIP_TAGS_RE = re.compile(
    r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>.*?</\1>",
    re.DOTALL,
)


def _normalize_win_path(p: str) -> str:
    """Normalize a Windows path or file:// URI for case-insensitive matching."""
    if not p:
        return ""
    s = p.strip()
    if s.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(s)
        unquoted = urllib.parse.unquote(parsed.path or "")
        # On Windows file:///d:/foo -> /d:/foo
        if len(unquoted) >= 3 and unquoted[0] == "/" and unquoted[2] == ":":
            unquoted = unquoted[1:]
        s = unquoted
    return s.replace("\\", "/").rstrip("/").lower()


def _parse_workspace_paths(workspace_uris_raw: str) -> list[str]:
    if not workspace_uris_raw:
        return []
    try:
        arr = json.loads(workspace_uris_raw)
        if isinstance(arr, list):
            return [_normalize_win_path(str(x)) for x in arr if x]
    except Exception:
        pass
    return [_normalize_win_path(workspace_uris_raw)]


def _clean_user_content(raw: str) -> str:
    if not raw:
        return ""
    m = _USER_REQ_RE.search(raw)
    if m:
        return m.group(1).strip()
    if "<SYSTEM_MESSAGE>" in raw and "<USER_REQUEST>" not in raw:
        return ""
    cleaned = _STRIP_TAGS_RE.sub("", raw).strip()
    return cleaned


_SUMMARIES_CACHE_TTL_S: float = 8.0
# (base_dir, source_label) -> (monotonic_ts, db_signature, results)
_summaries_cache: dict[tuple[str, str], tuple[float, tuple[int, int], list[dict[str, Any]]]] = {}


def _summaries_db_signature(db_file: Path) -> tuple[int, int]:
    """mtime_ns of the summaries DB and its WAL sidecar (WAL-mode writes may not touch the main file)."""
    try:
        main_ns = db_file.stat().st_mtime_ns
    except OSError:
        main_ns = 0
    try:
        wal_ns = db_file.with_name(db_file.name + "-wal").stat().st_mtime_ns
    except OSError:
        wal_ns = 0
    return main_ns, wal_ns


def _copy_summaries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        d = dict(it)
        d["workspace_paths"] = list(it.get("workspace_paths") or [])
        out.append(d)
    return out


def _read_readonly_summaries(base_dir: Path, source_label: str) -> list[dict[str, Any]]:
    """Cached (8 s TTL + mtime invalidation) wrapper around the read-only summaries scan."""
    db_file = base_dir / "conversation_summaries.db"
    if not db_file.exists():
        return []
    key = (str(base_dir), source_label)
    sig = _summaries_db_signature(db_file)
    now_m = time.monotonic()
    cached = _summaries_cache.get(key)
    if cached is not None and cached[1] == sig and (now_m - cached[0]) < _SUMMARIES_CACHE_TTL_S:
        return _copy_summaries(cached[2])
    results = _read_readonly_summaries_uncached(base_dir, source_label)
    _summaries_cache[key] = (now_m, sig, results)
    return _copy_summaries(results)


def _read_readonly_summaries_uncached(base_dir: Path, source_label: str) -> list[dict[str, Any]]:
    db_file = base_dir / "conversation_summaries.db"
    if not db_file.exists():
        return []

    uri = db_file.as_uri() + "?mode=ro"
    results: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT conversation_id, title, preview, step_count,
                   last_modified_time, workspace_uris, status,
                   project_id, nesting_depth, last_user_input_time
            FROM conversation_summaries
            WHERE nesting_depth = 0 AND killed = 0
            ORDER BY last_modified_time DESC
            LIMIT 5000
            """
        ).fetchall()
        norm_data_dir = _normalize_win_path(str(_eff_data_dir()))
        # v110: одна выборка каталога brain/ вместо тысяч `transcript_path.exists()` на NTFS
        brain_dir = base_dir / "brain"
        existing_brain_ids: set[str] = set()
        try:
            with os.scandir(brain_dir) as it_brain:
                for entry in it_brain:
                    try:
                        if entry.is_dir():
                            existing_brain_ids.add(entry.name.lower())
                    except OSError:
                        continue
        except OSError:
            existing_brain_ids = set()
        for r in rows:
            step_cnt = int(r["step_count"] or 0)
            if step_cnt <= 0:
                continue
            ws_paths = [
                wp
                for wp in _parse_workspace_paths(r["workspace_uris"] or "")
                if wp != norm_data_dir and not wp.startswith(norm_data_dir + "/")
            ]
            if not ws_paths:
                continue
            cid = r["conversation_id"]
            has_transcript = False
            if cid and str(cid).lower() in existing_brain_ids:
                transcript_path = (
                    brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
                )
                has_transcript = transcript_path.exists()
            if source_label == "desktop" and not has_transcript:
                continue
            results.append(
                {
                    "conversation_id": cid,
                    "title": (r["title"] or "").strip() or "Диалог без названия",
                    "preview": (r["preview"] or "").strip(),
                    "step_count": step_cnt,
                    "last_modified_time": r["last_modified_time"] or "",
                    "last_user_input_time": r["last_user_input_time"] or "",
                    "workspace_paths": ws_paths,
                    "status": r["status"] or "",
                    "source_app": source_label,
                    "has_transcript": has_transcript,
                }
            )
        conn.close()
    except Exception:
        pass
    return results


def _get_migrated_norm_paths(
    project_name: str,
    migrated_map: Optional[dict[str, list[str]]] = None,
) -> set[str]:
    if migrated_map is not None:
        raw_list = migrated_map.get(str(project_name or "").strip().lower(), [])
        return {_normalize_win_path(p) for p in raw_list if p and _normalize_win_path(p)}
    try:
        from .database import get_project_migrated_paths_sync

        raw_list = get_project_migrated_paths_sync(project_name)
    except Exception:
        return set()
    return {_normalize_win_path(p) for p in raw_list if p and _normalize_win_path(p)}


def _self_hub_excluded_norm_prefixes() -> tuple[str, ...]:
    base_dir = config.BASE_DIR.resolve()
    candidates = (
        _eff_user_workspaces_dir(),
        _eff_shared_projects_dir(),
        _eff_data_dir(),
        base_dir / "users",
        base_dir / "shared_projects",
        base_dir / "data",
    )
    out: list[str] = []
    for cand in candidates:
        np = _normalize_win_path(str(cand))
        if np and np not in out:
            out.append(np)
    return tuple(out)


def _wp_matches_self_hub(wp: str, base_norm: str, mig_norms: set[str], excluded_norms: tuple[str, ...]) -> bool:
    if any(wp == ex or wp.startswith(ex + "/") for ex in excluded_norms):
        return False
    if wp == base_norm or wp.startswith(base_norm + "/"):
        return True
    return any(wp == mp or wp.startswith(mp + "/") for mp in mig_norms)


def _build_self_hub_project_card(desktop_convs: list[dict[str, Any]], *, include_user_fields: bool = False) -> dict[str, Any]:
    base_dir = config.BASE_DIR.resolve()
    base_norm = _normalize_win_path(str(base_dir))
    mig_norms = _get_migrated_norm_paths("Antigravity_Hub")
    excluded_norms = _self_hub_excluded_norm_prefixes()
    matched_desktop = sum(
        1
        for c in desktop_convs
        if any(_wp_matches_self_hub(wp, base_norm, mig_norms, excluded_norms) for wp in c["workspace_paths"])
    )
    health = inspect_project_dir_health(base_dir)
    try:
        mtime = base_dir.stat().st_mtime
        mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except Exception:
        mtime = 0.0
        mtime_iso = ""
    card: dict[str, Any] = {
        "name": "Antigravity_Hub",
        "path": str(base_dir),
        "last_modified": mtime_iso,
        "_mtime": mtime + 5e11,
        "has_context_md": (base_dir / "CONTEXT.md").exists(),
        "has_plan_md": (base_dir / "PLAN.md").exists(),
        "has_readme": (base_dir / "README.md").exists(),
        "desktop_chat_count": matched_desktop,
        "is_empty_skeleton": False,
        "real_entries_count": max(1, health["real_entries_count"]),
        "is_self_hub": True,
    }
    if include_user_fields:
        card.update(
            {
                "is_shared": False,
                "shared_id": "",
                "description": "Системное ядро платформы Antigravity Hub",
                "execution_mode": "host",
                "member_ids": [],
            }
        )
    return card


def list_projects() -> list[dict[str, Any]]:
    """Scan all personal admin project folders in `PROJECTS_ROOT` plus built-in `Antigravity_Hub` (`BASE_DIR`)."""
    proj_root_scan = _eff_projects_root()
    desktop_convs = _read_readonly_summaries(_eff_agy_app_data_dir(), "desktop")
    projects: list[dict[str, Any]] = [_build_self_hub_project_card(desktop_convs, include_user_fields=False)]
    seen_low: set[str] = {"antigravity_hub", config.BASE_DIR.name.lower()}

    if proj_root_scan.exists():
        try:
            with os.scandir(proj_root_scan) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    if entry.name.startswith(".") or entry.name.startswith("$"):
                        continue
                    if entry.name.lower() in seen_low:
                        continue
                    p = Path(entry.path)
                    if p.resolve() == config.BASE_DIR.resolve():
                        continue
                    norm_p = _normalize_win_path(str(p))
                    mig_norms = _get_migrated_norm_paths(entry.name)
                    all_norms = {norm_p, *mig_norms}
                    matched_desktop = sum(
                        1
                        for c in desktop_convs
                        if any(
                            any(wp == np or wp.startswith(np + "/") for np in all_norms)
                            for wp in c["workspace_paths"]
                        )
                    )
                    health = inspect_project_dir_health(p)
                    try:
                        mtime = entry.stat().st_mtime
                        mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                    except Exception:
                        mtime = 0.0
                        mtime_iso = ""

                    seen_low.add(entry.name.lower())
                    projects.append(
                        {
                            "name": entry.name,
                            "path": str(p),
                            "last_modified": mtime_iso,
                            "_mtime": mtime,
                            "has_context_md": (p / "CONTEXT.md").exists(),
                            "has_plan_md": (p / "PLAN.md").exists(),
                            "has_readme": (p / "README.md").exists(),
                            "desktop_chat_count": matched_desktop,
                            "is_empty_skeleton": health["is_empty_skeleton"],
                            "real_entries_count": health["real_entries_count"],
                            "is_self_hub": False,
                        }
                    )
        except Exception:
            pass

    projects.sort(key=lambda x: x["_mtime"], reverse=True)
    for item in projects:
        item.pop("_mtime", None)
    return projects


def resolve_project_dir(project_name: str) -> Optional[Path]:
    """Safely resolve a project folder inside `PROJECTS_ROOT` (or `BASE_DIR` for `Antigravity_Hub`)."""
    clean = (project_name or "").strip()
    if not clean or "/" in clean or "\\" in clean or ".." in clean:
        return None
    if is_self_hub_project_name(clean):
        base_dir = config.BASE_DIR.resolve()
        return base_dir if base_dir.exists() and base_dir.is_dir() else None
    proj_root = _eff_projects_root().resolve()
    candidate = (proj_root / clean).resolve()
    try:
        candidate.relative_to(proj_root)
    except ValueError:
        return None
    if candidate.exists() and candidate.is_dir():
        return candidate
    return None


def get_user_personal_root(user: dict[str, Any]) -> Path:
    """Return `PROJECTS_ROOT` (`users/admin/projects`) for admin or `users/<slug>/projects` for non-admin user."""
    if user.get("role") == "admin":
        root = _eff_projects_root().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root
    slug = config.sanitize_user_folder_slug(user)
    ws = (_eff_user_workspaces_dir() / slug / "projects").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def resolve_effective_project_execution_mode(
    user: Optional[dict[str, Any]],
    project_or_mode: Any = None,
) -> str:
    """Resolve effective project execution contour ('host' | 'docker' | 'sandbox_files') via Dual-Key rule.

     Windows Host terminal ('host') opens ONLY when BOTH keys are enabled:
      1) user has role == 'admin' OR max_access_mode == 'host_full', AND
      2) project is personal OR shared with execution_mode == 'host'.
    """
    if not user:
        return "sandbox_files"

    if isinstance(project_or_mode, dict):
        if bool(project_or_mode.get("is_shared")):
            raw_mode = str(project_or_mode.get("execution_mode") or "docker").strip().lower()
        else:
            raw_mode = "host"
    else:
        raw_mode = str(project_or_mode or "host").strip().lower()

    if raw_mode not in {"host", "docker", "sandbox_files"}:
        raw_mode = "docker"

    if raw_mode == "sandbox_files":
        return "sandbox_files"

    is_admin = str(user.get("role") or "").strip().lower() == "admin"
    max_mode = (
        "host_full"
        if is_admin
        else str(user.get("max_access_mode") or "write_files").strip().lower()
    )

    if max_mode == "host_full":
        return "host" if raw_mode == "host" else "docker"
    if max_mode == "docker_full":
        return "docker"
    return "sandbox_files"


_PROJECTS_LIST_CACHE_TTL_SEC = 3.0
_PROJECTS_LIST_CACHE_LOCK = threading.Lock()
_PROJECTS_LIST_CACHE: dict[tuple[Any, ...], tuple[float, list[dict[str, Any]]]] = {}


def invalidate_projects_list_cache() -> None:
    """Сброс короткого TTL-кэша `list_projects_for_user` (создание/удаление/миграция проекта)."""
    with _PROJECTS_LIST_CACHE_LOCK:
        _PROJECTS_LIST_CACHE.clear()


def _projects_list_cache_key(
    user: dict[str, Any],
    shared_projects: list[dict[str, Any]],
    personal_root: Path,
) -> tuple[Any, ...]:
    try:
        root_mtime = os.stat(personal_root).st_mtime_ns
    except OSError:
        root_mtime = -1
    return (
        str(user.get("id") or ""),
        str(user.get("role") or ""),
        str(user.get("max_access_mode") or ""),
        str(user.get("username") or ""),
        str(user.get("workspace_dir") or ""),
        str(personal_root),
        root_mtime,
        tuple(
            sorted(
                (
                    str(sp.get("name") or ""),
                    str(sp.get("updated_at") or ""),
                    str(sp.get("path") or ""),
                    str(sp.get("execution_mode") or ""),
                )
                for sp in (shared_projects or [])
            )
        ),
    )


def list_projects_for_user(
    user: dict[str, Any],
    shared_projects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """List personal sandboxed projects + authorized Shared Projects for the given user."""
    personal_root = get_user_personal_root(user)
    cache_key = _projects_list_cache_key(user, shared_projects, personal_root)
    now_m = time.monotonic()
    with _PROJECTS_LIST_CACHE_LOCK:
        hit = _PROJECTS_LIST_CACHE.get(cache_key)
        if hit is not None and (now_m - hit[0]) < _PROJECTS_LIST_CACHE_TTL_SEC:
            return copy.deepcopy(hit[1])
    results = _list_projects_for_user_uncached(user, shared_projects, personal_root)
    with _PROJECTS_LIST_CACHE_LOCK:
        if len(_PROJECTS_LIST_CACHE) > 64:
            _PROJECTS_LIST_CACHE.clear()
        _PROJECTS_LIST_CACHE[cache_key] = (now_m, copy.deepcopy(results))
    return results


def _list_projects_for_user_uncached(
    user: dict[str, Any],
    shared_projects: list[dict[str, Any]],
    personal_root: Path,
) -> list[dict[str, Any]]:
    is_admin = user.get("role") == "admin"
    desktop_convs = _read_readonly_summaries(_eff_agy_app_data_dir(), "desktop") if is_admin else []
    migrated_map: Optional[dict[str, list[str]]] = None
    if is_admin and desktop_convs:
        try:
            from .database import get_all_projects_migrated_paths_sync

            migrated_map = get_all_projects_migrated_paths_sync()
        except Exception:
            migrated_map = None
    results: list[dict[str, Any]] = []
    seen_low: set[str] = set()

    # 1. Shared Projects first (pinned collaborative workspaces in SHARED_PROJECTS_DIR or PROJECTS_ROOT)
    for sp in shared_projects:
        sp_name = str(sp.get("name") or "").strip()
        if not sp_name or sp_name.lower() in seen_low:
            continue
        raw_sp_path = str(sp.get("path") or "").strip()
        try:
            p = validate_shared_project_path(sp_name, existing_path=raw_sp_path)
        except ValueError:
            continue
        p.mkdir(parents=True, exist_ok=True)
        matched_desktop = 0
        if is_admin and desktop_convs:
            norm_p = _normalize_win_path(str(p))
            all_norms = {norm_p, *_get_migrated_norm_paths(sp_name, migrated_map)}
            matched_desktop = sum(
                1
                for c in desktop_convs
                if any(
                    any(wp == np or wp.startswith(np + "/") for np in all_norms)
                    for wp in c["workspace_paths"]
                )
            )
        health = inspect_project_dir_health(p)
        try:
            mtime = p.stat().st_mtime
            mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except Exception:
            mtime = 0.0
            mtime_iso = sp.get("updated_at") or ""

        seen_low.add(sp_name.lower())
        results.append(
            {
                "name": sp_name,
                "path": str(p) if is_admin else f"shared/{sp_name}",
                "last_modified": mtime_iso,
                "_mtime": mtime + 1e12,  # Show shared projects prominently at top
                "has_context_md": (p / "CONTEXT.md").exists(),
                "has_plan_md": (p / "PLAN.md").exists(),
                "has_readme": (p / "README.md").exists(),
                "desktop_chat_count": matched_desktop,
                "is_empty_skeleton": health["is_empty_skeleton"],
                "real_entries_count": health["real_entries_count"],
                "is_self_hub": False,
                "is_shared": True,
                "shared_id": sp.get("id", ""),
                "description": sp.get("description", ""),
                "storage_location": infer_shared_project_storage_location(str(p)),
                "raw_execution_mode": str(sp.get("execution_mode") or "docker"),
                "execution_mode": resolve_effective_project_execution_mode(
                    user,
                    {
                        "is_shared": True,
                        "execution_mode": sp.get("execution_mode", "docker"),
                    },
                ),
                "member_ids": sp.get("member_ids", []),
            }
        )

    # 1.5. Built-in self-hosted `Antigravity_Hub` core project strictly for `admin`
    if is_admin:
        hub_card = _build_self_hub_project_card(desktop_convs, include_user_fields=True)
        seen_low.add("antigravity_hub")
        seen_low.add(config.BASE_DIR.name.lower())
        results.append(hub_card)

    # 2. Personal Projects in personal_root (deduplicated against Shared Projects in PROJECTS_ROOT)
    user_slug = config.sanitize_user_folder_slug(user)
    if personal_root.exists():
        try:
            with os.scandir(personal_root) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    if entry.name.startswith(".") or entry.name.startswith("$"):
                        continue
                    if entry.name.lower() in seen_low or is_self_hub_project_name(entry.name):
                        continue
                    p = Path(entry.path)
                    if p.resolve() == config.BASE_DIR.resolve():
                        continue
                    matched_desktop = 0
                    if is_admin and desktop_convs:
                        norm_p = _normalize_win_path(str(p))
                        all_norms = {norm_p, *_get_migrated_norm_paths(entry.name, migrated_map)}
                        matched_desktop = sum(
                            1
                            for c in desktop_convs
                            if any(
                                any(wp == np or wp.startswith(np + "/") for np in all_norms)
                                for wp in c["workspace_paths"]
                            )
                        )
                    health = inspect_project_dir_health(p)
                    try:
                        mtime = entry.stat().st_mtime
                        mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                    except Exception:
                        mtime = 0.0
                        mtime_iso = ""

                    seen_low.add(entry.name.lower())
                    results.append(
                        {
                            "name": entry.name,
                            "path": str(p)
                            if is_admin
                            else f"users/{user_slug}/projects/{entry.name}",
                            "last_modified": mtime_iso,
                            "_mtime": mtime,
                            "has_context_md": (p / "CONTEXT.md").exists(),
                            "has_plan_md": (p / "PLAN.md").exists(),
                            "has_readme": (p / "README.md").exists(),
                            "desktop_chat_count": matched_desktop,
                            "is_empty_skeleton": health["is_empty_skeleton"],
                            "real_entries_count": health["real_entries_count"],
                            "is_self_hub": False,
                            "is_shared": False,
                            "shared_id": "",
                            "description": "",
                            "execution_mode": resolve_effective_project_execution_mode(
                                user, {"is_shared": False}
                            ),
                            "member_ids": [],
                        }
                    )
        except Exception:
            pass

    results.sort(key=lambda x: x["_mtime"], reverse=True)
    for item in results:
        item.pop("_mtime", None)
    return results


def resolve_user_project(
    project_name: str,
    user: dict[str, Any],
    shared_projects: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Safely resolve a project folder for `user` (Shared, built-in `Antigravity_Hub` for admin, or personal sandbox)."""
    clean = (project_name or "").strip()
    if not clean or "/" in clean or "\\" in clean or ".." in clean:
        return None

    # 0. Built-in self-hosted `Antigravity_Hub` core project (strictly admin-only; never touch BASE_DIR/.git)
    if is_self_hub_project_name(clean):
        if str(user.get("role") or "").strip().lower() != "admin":
            return None
        base_dir = config.BASE_DIR.resolve()
        if not base_dir.exists() or not base_dir.is_dir():
            return None
        return {
            "name": "Antigravity_Hub",
            "path": base_dir,
            "is_shared": False,
            "is_self_hub": True,
            "shared_id": "",
            "execution_mode": "host",
            "member_ids": [],
        }

    # 1. Check accessible Shared Projects (in SHARED_PROJECTS_DIR or PROJECTS_ROOT)
    for sp in shared_projects:
        if str(sp.get("name") or "").strip().lower() == clean.lower():
            raw_sp_path = str(sp.get("path") or "").strip()
            try:
                p = validate_shared_project_path(sp["name"], existing_path=raw_sp_path)
            except ValueError:
                return None
            p.mkdir(parents=True, exist_ok=True)
            ensure_isolated_project_git_boundary(p)
            return {
                "name": sp["name"],
                "path": p,
                "is_shared": True,
                "is_self_hub": False,
                "shared_id": sp.get("id", ""),
                "storage_location": infer_shared_project_storage_location(str(p)),
                "raw_execution_mode": str(sp.get("execution_mode") or "docker"),
                "execution_mode": resolve_effective_project_execution_mode(
                    user,
                    {
                        "is_shared": True,
                        "execution_mode": sp.get("execution_mode", "docker"),
                    },
                ),
                "member_ids": sp.get("member_ids", []),
            }

    # 2. Check user's isolated personal root
    personal_root = get_user_personal_root(user)
    candidate = (personal_root / clean).resolve()
    try:
        candidate.relative_to(personal_root)
    except ValueError:
        return None

    if candidate.exists() and candidate.is_dir():
        ensure_isolated_project_git_boundary(candidate)
        return {
            "name": candidate.name,
            "path": candidate,
            "is_shared": False,
            "is_self_hub": False,
            "shared_id": "",
            "execution_mode": resolve_effective_project_execution_mode(
                user, {"is_shared": False}
            ),
            "member_ids": [],
        }
    return None


def create_personal_project_folder(
    *,
    project_name: str,
    user: dict[str, Any],
    description: str = "",
) -> dict[str, Any]:
    """Create a new project folder strictly inside the user's personal sandbox root."""
    clean = (project_name or "").strip()
    if (
        not _SAFE_FOLDER_RE.fullmatch(clean)
        or ".." in clean
        or "/" in clean
        or "\\" in clean
    ):
        raise ValueError(
            "Название папки проекта должно содержать от 2 до 64 символов (без слэшей и '..')"
        )
    if clean.lower() in config.get_reserved_project_names_lower() or clean.startswith("_"):
        raise ValueError(
            f"Имя '{clean}' зарезервировано системой Antigravity Hub и не может использоваться для проекта"
        )

    personal_root = get_user_personal_root(user)
    dest = (personal_root / clean).resolve()
    try:
        dest.relative_to(personal_root)
    except ValueError:
        raise ValueError("Выход за пределы рабочей папки запрещён")

    if dest.exists():
        raise ValueError(f"Папка проекта '{clean}' уже существует")

    dest.mkdir(parents=True, exist_ok=False)
    readme = dest / "README.md"
    readme.write_text(
        f"# {clean}\n\n{description.strip() or 'Проект создан в Antigravity Hub.'}\n",
        encoding="utf-8",
    )
    ensure_isolated_project_git_boundary(dest)
    health = inspect_project_dir_health(dest)
    is_admin = user.get("role") == "admin"
    user_slug = config.sanitize_user_folder_slug(user)
    return {
        "name": dest.name,
        "path": str(dest)
        if is_admin
        else f"users/{user_slug}/projects/{dest.name}",
        "is_shared": False,
        "is_self_hub": False,
        "is_empty_skeleton": health["is_empty_skeleton"],
        "real_entries_count": health["real_entries_count"],
        "execution_mode": resolve_effective_project_execution_mode(
            user, {"is_shared": False}
        ),
    }


def list_desktop_conversations(
    *,
    project_name: Optional[str] = None,
    query: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List read-only Desktop Antigravity conversations, optionally filtered by project (including migrated_from_paths)."""
    convs = _read_readonly_summaries(_eff_agy_app_data_dir(), "desktop")

    if project_name:
        if is_self_hub_project_name(project_name):
            base_norm = _normalize_win_path(str(config.BASE_DIR.resolve()))
            mig_norms = _get_migrated_norm_paths("Antigravity_Hub")
            excluded_norms = _self_hub_excluded_norm_prefixes()
            convs = [
                c
                for c in convs
                if any(_wp_matches_self_hub(wp, base_norm, mig_norms, excluded_norms) for wp in c["workspace_paths"])
            ]
        else:
            allowed_norms: set[str] = set()
            proj_dir = resolve_project_dir(project_name)
            if proj_dir:
                allowed_norms.add(_normalize_win_path(str(proj_dir)))
            else:
                clean_proj = (project_name or "").strip()
                if clean_proj and "/" not in clean_proj and "\\" not in clean_proj and ".." not in clean_proj:
                    sp_cand = (_eff_shared_projects_dir() / clean_proj).resolve()
                    if sp_cand.exists() and sp_cand.is_dir():
                        allowed_norms.add(_normalize_win_path(str(sp_cand)))
            allowed_norms.update(_get_migrated_norm_paths(project_name))
            if not allowed_norms:
                return []
            convs = [
                c
                for c in convs
                if any(
                    any(wp == np or wp.startswith(np + "/") for np in allowed_norms)
                    for wp in c["workspace_paths"]
                )
            ]

    if query and query.strip():
        q = query.strip().lower()
        convs = [
            c
            for c in convs
            if q in c["title"].lower() or q in c["preview"].lower()
        ]

    return convs


def read_transcript_messages(
    conversation_id: str,
    *,
    base_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Parse transcript.jsonl (with fallback to transcript_full.jsonl on truncated lines)."""
    # Sanitize UUID/ID to prevent directory traversal
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", conversation_id or ""):
        return []

    if base_dir:
        roots = [base_dir]
    else:
        roots = [_eff_agy_app_data_dir(), _eff_agy_cli_data_dir()]
        agy_accs_root = config.CLI_HOMES_DIR / "_agy_accounts"
        if agy_accs_root.is_dir():
            roots.extend(sorted(agy_accs_root.glob("*/antigravity-cli")))
    logs_dir: Optional[Path] = None
    for root in roots:
        if root is None:
            continue
        candidate = root / "brain" / conversation_id / ".system_generated" / "logs"
        if (candidate / "transcript.jsonl").exists():
            logs_dir = candidate
            break

    if not logs_dir:
        return []

    compact_path = logs_dir / "transcript.jsonl"
    full_path = logs_dir / "transcript_full.jsonl"

    try:
        compact_lines = compact_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    # Lazy-load full transcript lines only if needed for truncated user/model content
    full_lines: Optional[list[str]] = None

    messages: list[dict[str, Any]] = []
    current_assistant: Optional[dict[str, Any]] = None

    def flush_assistant() -> None:
        nonlocal current_assistant
        if current_assistant is not None:
            if (
                current_assistant["content"].strip()
                or current_assistant["thinking"].strip()
                or current_assistant["tool_calls"]
            ):
                messages.append(current_assistant)
            current_assistant = None

    for idx, raw_line in enumerate(compact_lines):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            step = json.loads(raw_line)
        except Exception:
            continue

        stype = step.get("type", "")
        source = step.get("source", "")
        truncated = step.get("truncated_fields") or []

        # Upgrade from transcript_full.jsonl only for user/model steps with truncated content/thinking
        if (
            stype in ("USER_INPUT", "PLANNER_RESPONSE")
            and ("content" in truncated or "thinking" in truncated)
            and full_path.exists()
        ):
            if full_lines is None:
                try:
                    full_lines = full_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except Exception:
                    full_lines = []
            if full_lines and idx < len(full_lines):
                try:
                    step = json.loads(full_lines[idx])
                except Exception:
                    pass

        if stype == "USER_INPUT" and source == "USER_EXPLICIT":
            text = _clean_user_content(step.get("content", ""))
            media = step.get("media") or []
            if not text and not media:
                continue
            flush_assistant()
            messages.append(
                {
                    "id": f"step-{step.get('step_index', idx)}",
                    "role": "user",
                    "content": text,
                    "thinking": "",
                    "tool_calls": [],
                    "attachments": [
                        {"mime_type": m.get("mime_type", ""), "name": Path(m.get("uri", "")).name}
                        for m in media
                        if isinstance(m, dict)
                    ],
                    "created_at": step.get("created_at", ""),
                    "status": "done",
                }
            )

        elif stype == "PLANNER_RESPONSE" and source == "MODEL":
            if current_assistant is None:
                current_assistant = {
                    "id": f"step-{step.get('step_index', idx)}",
                    "role": "assistant",
                    "content": "",
                    "thinking": "",
                    "tool_calls": [],
                    "attachments": [],
                    "created_at": step.get("created_at", ""),
                    "status": "done",
                }
            th = (step.get("thinking") or "").strip()
            if th:
                if current_assistant["thinking"]:
                    current_assistant["thinking"] += "\n\n---\n\n" + th
                else:
                    current_assistant["thinking"] = th

            for tc in step.get("tool_calls") or []:
                if isinstance(tc, dict):
                    current_assistant["tool_calls"].append(
                        {
                            "name": tc.get("name", "tool"),
                            "parameters": tc.get("args", {}),
                        }
                    )

            cnt = (step.get("content") or "").strip()
            if cnt:
                if current_assistant["content"]:
                    current_assistant["content"] += "\n\n" + cnt
                else:
                    current_assistant["content"] = cnt

    flush_assistant()
    return messages


def extract_latest_turn_thinking(conversation_id: str, base_dir=None, *, current_turn_only=False) -> str:
    """Read the thinking blocks from the most recent turn in agy-cli's transcript.jsonl."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", conversation_id or ""):
        return ""
    msgs = read_transcript_messages(conversation_id, base_dir=base_dir or AGY_CLI_DATA_DIR)
    if not msgs and base_dir and not current_turn_only:
        msgs = read_transcript_messages(conversation_id, base_dir=AGY_CLI_DATA_DIR)
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            return m.get("thinking", "")
        if current_turn_only and m.get("role") == "user":
            break
    return ""


def export_idea_to_project(
    *,
    project_name: str,
    target_file: str,
    idea_title: str,
    content: str,
    append: bool = True,
    resolved_dir: Optional[Path] = None,
    hide_host_path: bool = False,
) -> dict[str, Any]:
    """Save a brainstormed idea/summary into PLAN.md, CONTEXT.md, or IDEAS.md of a chosen project."""
    proj_dir = resolved_dir or resolve_project_dir(project_name)
    if not proj_dir:
        raise ValueError(f"Проект '{project_name}' не найден или нет доступа")

    allowed_targets = {"PLAN.md", "CONTEXT.md", "IDEAS.md"}
    fname = target_file.strip()
    if fname not in allowed_targets:
        raise ValueError(f"Допустимые целевые файлы: {', '.join(sorted(allowed_targets))}")

    project_root = Path(proj_dir).resolve()
    dest = (project_root / fname).resolve()
    if not dest.is_relative_to(project_root):
        raise ValueError("Целевой файл выходит за пределы проекта")
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M")
    header_title = idea_title.strip() or "Идея из Antigravity Hub"

    block = (
        f"\n\n---\n\n"
        f"## 💡 {header_title} *(из Antigravity Hub — {now_local})*\n\n"
        f"{content.strip()}\n"
    )

    if append and dest.exists():
        existing = dest.read_text(encoding="utf-8", errors="replace").rstrip()
        dest.write_text(existing + block, encoding="utf-8")
    else:
        initial = f"# {project_name} — {fname.replace('.md', '')}\n" if not append or not dest.exists() else ""
        dest.write_text(
            (initial + block.lstrip("\n")) if initial else content.strip() + "\n",
            encoding="utf-8",
        )

    return {
        "ok": True,
        "project_name": project_name,
        "target_file": fname,
        "full_path": f"{project_name}/{fname}" if hide_host_path else str(dest),
    }


# Managed project mutations: never accept an arbitrary filesystem target.
def validate_new_project_name(new_name: str) -> str:
    clean = (new_name or "").strip()
    if (not _SAFE_FOLDER_RE.fullmatch(clean) or ".." in clean
        or clean.startswith((".", "_")) or clean.endswith(".")
        or clean.upper().split(".")[0] in config._WIN_RESERVED_NAMES
        or clean.lower() in config.get_reserved_project_names_lower()
        or is_self_hub_project_name(clean)):
        raise ValueError("Недопустимое или зарезервированное название проекта (2–64 символа)")
    return clean


def assert_safe_managed_project_dir(proj_dir: Path, *, allowed_parent: Path) -> Path:
    proj_dir, allowed_parent = Path(proj_dir), Path(allowed_parent)
    # Check every ancestor too: a junction above the immediate parent is equally unsafe.
    for path in (proj_dir, allowed_parent, *allowed_parent.parents):
        if config._is_junction_or_symlink(path):
            raise ValueError("Ссылки и junction в пути проекта запрещены")
    resolved = proj_dir.resolve()
    base = config.BASE_DIR.resolve()
    if (resolved.parent != allowed_parent.resolve() or is_self_hub_project_name(proj_dir.name)
        or resolved == base or resolved in base.parents):
        raise ValueError("Системный проект или небезопасный путь")
    for protected in (_eff_data_dir(), config.CLI_HOMES_DIR,
                      _eff_shared_projects_dir() / "_uploads", _eff_shared_projects_dir() / "_context"):
        target = protected.resolve()
        if resolved == target or target in resolved.parents or resolved in target.parents:
            raise ValueError("Путь пересекается с системным хранилищем")
    if not resolved.is_dir():
        raise ValueError("Папка проекта не найдена")
    return resolved


def rename_project_directory_on_disk(old_dir: Path, new_name: str, *, allowed_parent: Path) -> Path:
    old = assert_safe_managed_project_dir(old_dir, allowed_parent=allowed_parent)
    clean = validate_new_project_name(new_name)
    new = allowed_parent.resolve() / clean
    if old.name == clean:
        return old
    if new.exists() and (old.name.lower() != clean.lower() or not old.samefile(new)):
        raise ValueError(f"Папка проекта '{clean}' уже существует")
    temp = old.with_name(f".__agy_ren_{uuid.uuid4().hex[:8]}")
    try:
        if old.name.lower() == clean.lower():
            old.rename(temp)
            try:
                temp.rename(new)
            except OSError:
                temp.rename(old)
                raise
        else:
            old.rename(new)
    except OSError as exc:
        raise RuntimeError("Папка проекта занята другой программой на ПК (закройте терминал или проводник в этой папке и повторите)") from exc
    readme = new / "README.md"
    if readme.is_file() and not config._is_junction_or_symlink(readme) and readme.stat().st_size <= 300:
        try:
            text = readme.read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            if lines and lines[0].rstrip() == f"# {old.name}":
                lines[0] = f"# {clean}\n"
                readme.write_text("".join(lines), encoding="utf-8")
        except (OSError, UnicodeError):
            pass
    return new


def relocate_project_directory_on_disk(src_dir: Path, dst_dir: Path, *,
        allowed_src_parent: Path, allowed_dst_parent: Path) -> Path:
    src = assert_safe_managed_project_dir(src_dir, allowed_parent=allowed_src_parent)
    dst_dir, parent = Path(dst_dir), Path(allowed_dst_parent)
    validate_new_project_name(dst_dir.name)
    for path in (dst_dir, parent, *parent.parents):
        if config._is_junction_or_symlink(path):
            raise ValueError("Ссылки и junction в пути проекта запрещены")
    dst = dst_dir.resolve()
    base = config.BASE_DIR.resolve()
    if dst.parent != parent.resolve() or dst == base or dst in base.parents:
        raise ValueError("Системный проект или небезопасный путь")
    for protected in (_eff_data_dir(), config.CLI_HOMES_DIR,
                      _eff_shared_projects_dir() / "_uploads", _eff_shared_projects_dir() / "_context"):
        target = protected.resolve()
        if dst == target or target in dst.parents or dst in target.parents:
            raise ValueError("Путь пересекается с системным хранилищем")
    if src == dst:
        ensure_isolated_project_git_boundary(src)
        return src
    if os.path.lexists(dst):
        raise ValueError(f"Целевая папка проекта '{dst_dir.name}' уже существует")
    parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except OSError as exc:
        raise RuntimeError("Папка проекта занята другой программой на ПК (закройте терминал или проводник в этой папке и повторите)") from exc
    try:
        ensure_isolated_project_git_boundary(dst)
    except Exception:
        shutil.move(str(dst), str(src))
        raise
    return dst


def _rmtree_force_readonly(resolved: Path) -> None:
    def retry(func, path, exc_info):
        if config._is_junction_or_symlink(Path(path)):
            raise OSError("Отказ удаления ссылки")
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    # shutil.rmtree does not traverse directory symlinks or Windows junctions.
    shutil.rmtree(resolved, onerror=retry)


def remove_or_archive_project_directory(proj_dir: Path, *, allowed_parent: Path,
        delete_files: bool, archive_owner_slug: str = "admin") -> Optional[Path]:
    resolved = assert_safe_managed_project_dir(proj_dir, allowed_parent=allowed_parent)
    if delete_files:
        _rmtree_force_readonly(resolved)
        return None
    slug = config.sanitize_user_folder_slug(archive_owner_slug)
    archive = _eff_user_workspaces_dir() / slug / "archived_projects"
    for parent in (archive, *archive.parents):
        if config._is_junction_or_symlink(parent):
            raise ValueError("Архив не может быть ссылкой")
    archive.mkdir(parents=True, exist_ok=True)
    dest = archive / resolved.name
    if dest.exists():
        dest = archive / (resolved.name + datetime.now().strftime("_%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    shutil.move(str(resolved), str(dest))
    return dest
