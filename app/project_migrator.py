"""
Non-destructive Desktop Antigravity -> Antigravity Hub Project & Chat Migrator (v96).

Discovers external projects opened in Desktop Antigravity (`conversation_summaries.db`)
or referenced in `hub.db`, classifies empty skeleton folders vs conflicts, safely copies
project files into `PROJECTS_ROOT/<ProjectName>` via `shutil.copy2` (NEVER deleting or moving
source directories), skips machine-bound virtualenvs/caches, preserves `.env` and real `.git`
history, and auto-imports/relinks Desktop `transcript.jsonl` conversations into `hub.db`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import brain_reader, config, database
from .agy_engine import ensure_isolated_project_git_boundary
from .config import subprocess_hidden_kwargs

SKIP_MIGRATION_DIRS_LOWER: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
    }
)

VENV_DIR_NAMES_LOWER: frozenset[str] = frozenset({".venv", "venv", "env"})

_COMMON_USER_ROOT_FOLDERS_LOWER: frozenset[str] = frozenset(
    {
        "desktop",
        "documents",
        "downloads",
        "onedrive",
        "pictures",
        "music",
        "videos",
        "favorites",
        "links",
        "contacts",
        "searches",
        "saved games",
    }
)


def _norm_path_str(raw: str | Path) -> str:
    s = str(raw or "").strip().replace("\\", "/")
    if re.match(r"^/[A-Za-z]:", s):
        s = s[1:]
    s = re.sub(r"/+", "/", s)
    if len(s) > 3 and s.endswith("/"):
        s = s.rstrip("/")
    if os.name == "nt" or re.match(r"^[A-Za-z]:", s):
        return s.lower()
    return s


def _to_native_path_str(norm_or_raw: str) -> str:
    s = str(norm_or_raw or "").strip()
    if not s:
        return ""
    if os.name == "nt":
        s = s.replace("/", "\\")
        if len(s) >= 2 and s[1] == ":":
            s = s[0].upper() + s[1:]
            if len(s) == 2:
                s += "\\"
    return s


def sanitize_migrated_project_name(raw_name: str) -> str:
    """Produce a safe directory name inside PROJECTS_ROOT."""
    cleaned = str(raw_name or "").strip().strip(". ")
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    if not cleaned or cleaned in {".", ".."}:
        return ""
    if len(cleaned) > 80:
        cleaned = cleaned[:80].rstrip("._ ")
    return cleaned


def _is_same_or_child_norm(cand_norm: str, root_norm: str) -> bool:
    if not cand_norm or not root_norm:
        return False
    r = root_norm.rstrip("/")
    c = cand_norm.rstrip("/")
    return c == r or c.startswith(r + "/")


def _is_Windows_user_profile_or_shell_root(norm_p: str) -> bool:
    """Return True if `norm_p` is `C:/Users`, `C:/Users/<u/>`, or a top-level shell folder
    like `C:/Users/<u/>/Desktop` or `C:/Users/<u/>/OneDrive/Desktop` (while returning False
    for actual project subfolders like `C:/Users/<u/>/Desktop/MyBot`)."""
    parts = [seg for seg in norm_p.split("/") if seg]
    if not parts:
        return True
    # Drive root (`c:`)
    if len(parts) == 1 and parts[0].endswith(":"):
        return True

    # Check against current Path.home()
    try:
        home_norm = _norm_path_str(Path.home().resolve())
        home_parts = [seg for seg in home_norm.split("/") if seg]
    except OSError:
        home_norm = ""
        home_parts = []

    if home_norm:
        if norm_p.rstrip("/") == home_norm.rstrip("/"):
            return True
        if len(parts) == len(home_parts) + 1 and parts[: len(home_parts)] == home_parts:
            if parts[-1].lower() in _COMMON_USER_ROOT_FOLDERS_LOWER:
                return True
        if (
            len(parts) == len(home_parts) + 2
            and parts[: len(home_parts)] == home_parts
            and parts[len(home_parts)].lower() == "onedrive"
            and parts[-1].lower() in _COMMON_USER_ROOT_FOLDERS_LOWER
        ):
            return True

    # Generic `X:/users` or `X:/users/<username>` or `X:/users/<username>/desktop`
    if len(parts) >= 2 and parts[0].endswith(":") and parts[1].lower() == "users":
        if len(parts) == 2:
            return True  # C:/Users
        if len(parts) == 3:
            return True  # C:/Users/<username>
        if len(parts) == 4 and parts[3].lower() in _COMMON_USER_ROOT_FOLDERS_LOWER:
            return True  # C:/Users/<username>/Desktop (root itself)
        if (
            len(parts) == 5
            and parts[3].lower() == "onedrive"
            and parts[4].lower() in _COMMON_USER_ROOT_FOLDERS_LOWER
        ):
            return True  # C:/Users/<username>/OneDrive/Desktop (root itself)

    return False


def is_eligible_project_workspace_path(
    raw_path: str | Path,
    *,
    projects_root: Path,
    base_dir: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    agy_app_dir: Optional[Path] = None,
    agy_cli_dir: Optional[Path] = None,
    allow_temp_subdirs: Optional[bool] = None,
    allow_in_projects_root: bool = True,
) -> bool:
    """Smart path filter for `discover_desktop_migration_plan()`.

    Returns True ONLY for genuine project folders (either inside `projects_root/<Name>` when
    `allow_in_projects_root=True`, or external project folders like `C:\\Projects\\MyBot`,
    `D:\\Projects\\LegacyBot`, etc.).
    Strictly rejects drive roots, `projects_root` itself and its parents, `Antigravity_Hub` (`base_dir`)
    and any internal Hub folders (`users/`, `shared_projects/`, `data/`, `app/`, `tools/`, `tests/`),
    OS system folders, `.gemini`/`.claude`/`.codex`, and bare user/Desktop/Documents/Downloads roots.
    """
    s = str(raw_path or "").strip()
    if not s or "\x00" in s:
        return False
    if s.startswith("\\\\"):
        return False
    norm = _norm_path_str(s)
    if not norm:
        return False

    segments = [seg for seg in norm.split("/") if seg]
    if not segments or ".." in segments:
        return False
    if len(segments) == 1 and (segments[0].endswith(":") or norm == "/"):
        return False

    eff_base = (base_dir or config.BASE_DIR).resolve()
    eff_data = (data_dir or config.DATA_DIR).resolve()
    eff_proj_root = projects_root.resolve()
    eff_agy_app = (agy_app_dir or config.AGY_APP_DATA_DIR).resolve()
    eff_agy_cli = (agy_cli_dir or config.AGY_CLI_DATA_DIR).resolve()

    norm_proj_root = _norm_path_str(eff_proj_root)
    norm_base = _norm_path_str(eff_base)
    norm_data = _norm_path_str(eff_data)
    norm_agy_app = _norm_path_str(eff_agy_app)
    norm_agy_cli = _norm_path_str(eff_agy_cli)

    # Reject `projects_root` itself (`.../users/admin/projects`) and any ancestor (`.../users/admin`, `.../users`, `BASE_DIR`, `D:\`)
    if norm == norm_proj_root or _is_same_or_child_norm(norm_proj_root, norm):
        return False

    is_child_of_proj_root = norm.startswith(norm_proj_root + "/")
    if is_child_of_proj_root and not allow_in_projects_root:
        return False

    # Ironclad recursion guard: reject `BASE_DIR` (`Antigravity_Hub`) and ALL paths inside `BASE_DIR`
    # (except `projects_root/<Proj>` when `allow_in_projects_root=True` for matching already-in-Hub chats).
    for hub_internal in (norm_data, norm_agy_app, norm_agy_cli):
        if _is_same_or_child_norm(norm, hub_internal):
            return False
    if not is_child_of_proj_root and _is_same_or_child_norm(norm, norm_base):
        return False

    # Allow temp subdirectories ONLY when `projects_root` or `base_dir` is inside temp (isolated unit tests)
    # or `allow_temp_subdirs=True` is explicitly passed.
    try:
        temp_norm = _norm_path_str(Path(tempfile.gettempdir()).resolve())
    except OSError:
        temp_norm = ""
    if allow_temp_subdirs is None:
        allow_temp_subdirs = bool(
            temp_norm
            and (
                _is_same_or_child_norm(norm_proj_root, temp_norm)
                or _is_same_or_child_norm(norm_base, temp_norm)
            )
        )

    in_allowed_temp_subdir = bool(
        allow_temp_subdirs
        and temp_norm
        and norm != temp_norm
        and _is_same_or_child_norm(norm, temp_norm)
    )

    if not in_allowed_temp_subdir:
        # Block Windows / POSIX system roots & AppData
        for seg in segments:
            seg_low = seg.lower()
            if seg_low in {
                "appdata",
                ".gemini",
                ".claude",
                ".codex",
                ".ssh",
                ".gnupg",
                ".aws",
                ".kube",
                "$recycle.bin",
                "system volume information",
                "node_modules",
                ".venv",
            }:
                return False

        if len(segments) >= 2 and segments[0].endswith(":"):
            second_low = segments[1].lower()
            if second_low in {
                "windows",
                "program files",
                "program files (x86)",
                "programdata",
                "recovery",
                "perflogs",
            }:
                return False
        elif norm.startswith(("/etc/", "/proc/", "/sys/", "/usr/", "/var/", "/dev/", "/boot/", "/tmp/")):
            return False
        elif norm in {"/etc", "/proc", "/sys", "/usr", "/var", "/dev", "/boot", "/tmp", "/root"}:
            return False

        if _is_Windows_user_profile_or_shell_root(norm):
            return False
    else:
        # Even inside temp test roots, block dot-secret folders
        for seg in segments:
            if seg.lower() in {".gemini", ".claude", ".codex", ".ssh", "node_modules", ".venv"}:
                return False

    # Validate candidate target folder name against reserved Hub core names
    leaf_name = sanitize_migrated_project_name(segments[-1])
    reserved_lows = config.get_reserved_project_names_lower() | {eff_base.name.lower()}
    if not leaf_name or leaf_name.lower() in reserved_lows:
        return False

    return True


_SIBLING_PROJECT_CODE_EXTS: set[str] = {
    ".py",
    ".pyw",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".scss",
    ".vue",
    ".svelte",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".php",
    ".rb",
    ".sh",
    ".bat",
    ".cmd",
    ".ps1",
    ".sql",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".md",
}

_SIBLING_PROJECT_MARKER_NAMES: set[str] = {
    ".git",
    ".env",
    ".env.example",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "cargo.toml",
    "go.mod",
    "dockerfile",
    "docker-compose.yml",
    "makefile",
    "readme.md",
}


def _looks_like_sibling_project_dir(path: Path) -> bool:
    """Return True if `path` is a non-empty folder with at least one code/project marker (skipping media/empty dirs)."""
    if not path.is_dir():
        return False
    try:
        with os.scandir(path) as it:
            for entry in it:
                nl = entry.name.lower()
                if nl in _SIBLING_PROJECT_MARKER_NAMES:
                    return True
                if entry.is_file(follow_symlinks=False):
                    if Path(nl).suffix in _SIBLING_PROJECT_CODE_EXTS:
                        return True
                elif entry.is_dir(follow_symlinks=False) and nl in {"src", "app", "bot", "backend", "frontend", "scripts", "tests"}:
                    return True
    except OSError:
        return False
    return False


def _resolve_safe_legacy_parent_dir(
    *,
    eff_base_dir: Path,
    eff_proj_root: Path,
    allow_temp_subdirs: Optional[bool] = None,
) -> Optional[Path]:
    """Return legacy parent directory (e.g. `D:\\Projects`) containing sibling projects
    around `Antigravity_Hub` if and only if it is safe to scan (never a drive root, user profile,
    Desktop/Documents/Downloads, or OS system root)."""
    cand: Optional[Path] = None
    legacy_cfg = getattr(config, "LEGACY_EXTERNAL_PROJECTS_ROOT", None)
    if isinstance(legacy_cfg, Path) and config._same_fs_path(eff_base_dir, config._BOOT_BASE_DIR):
        try:
            cand = legacy_cfg.resolve()
        except OSError:
            cand = None

    norm_base = _norm_path_str(eff_base_dir)
    norm_proj = _norm_path_str(eff_proj_root)
    if cand is None and _is_same_or_child_norm(norm_proj, norm_base):
        try:
            cand = eff_base_dir.parent.resolve()
        except OSError:
            cand = None

    if cand is None or not cand.is_dir():
        return None

    norm_cand = _norm_path_str(cand)
    if not norm_cand or _is_same_or_child_norm(norm_cand, norm_base):
        return None

    segs = [seg for seg in norm_cand.split("/") if seg]
    if not segs or (len(segs) == 1 and (segs[0].endswith(":") or norm_cand == "/")):
        return None

    if _is_Windows_user_profile_or_shell_root(norm_cand):
        return None

    if len(segs) >= 2 and segs[0].endswith(":"):
        if segs[1].lower() in {
            "windows",
            "program files",
            "program files (x86)",
            "programdata",
            "recovery",
            "perflogs",
            "$recycle.bin",
            "system volume information",
        }:
            return None
    elif norm_cand in {"/", "/etc", "/proc", "/sys", "/usr", "/var", "/dev", "/boot", "/tmp", "/root", "/home"}:
        return None

    try:
        temp_norm = _norm_path_str(Path(tempfile.gettempdir()).resolve())
    except OSError:
        temp_norm = ""
    if temp_norm and norm_cand == temp_norm:
        return None
    if temp_norm and _is_same_or_child_norm(norm_cand, temp_norm):
        eff_allow_tmp = (
            allow_temp_subdirs
            if allow_temp_subdirs is not None
            else _is_same_or_child_norm(norm_base, temp_norm)
        )
        if not eff_allow_tmp:
            return None

    return cand


def _inspect_source_dir_stats(path: Path) -> dict[str, Any]:
    """Scan a project folder quickly without descending into `.venv` / `node_modules` / `.git`."""
    if not path.exists() or not path.is_dir():
        return {
            "exists": False,
            "real_files_count": 0,
            "latest_mtime": 0.0,
            "latest_mtime_iso": "",
            "has_requirements_txt": False,
            "has_pyproject_toml": False,
            "has_package_json": False,
            "has_env_file": False,
            "has_git_dir": False,
            "skipped_venv_detected": False,
            "is_empty_skeleton": False,
        }

    health = brain_reader.inspect_project_dir_health(path)
    has_req = (path / "requirements.txt").is_file()
    has_pyproj = (path / "pyproject.toml").is_file()
    has_pkg = (path / "package.json").is_file()
    has_git = (path / ".git").is_dir()
    has_env = False
    skipped_venv = False
    real_files = 0
    latest_mtime = 0.0
    max_file_cap = 25_000

    stack: list[Path] = [path]
    is_root = True
    while stack and real_files < max_file_cap:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    ename = entry.name
                    elow = ename.lower()
                    if is_root and elow.startswith(".env"):
                        has_env = True
                    try:
                        if config._is_junction_or_symlink(Path(entry.path)):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if elow in VENV_DIR_NAMES_LOWER:
                                skipped_venv = True
                                continue
                            if elow in SKIP_MIGRATION_DIRS_LOWER or elow in {".git", ".agents"}:
                                continue
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if is_root and elow == "readme.md" and health["has_stub_readme"]:
                                continue
                            real_files += 1
                            st = entry.stat(follow_symlinks=False)
                            if st.st_mtime > latest_mtime:
                                latest_mtime = st.st_mtime
                    except OSError:
                        continue
        except OSError:
            pass
        is_root = False

    latest_iso = (
        datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat()
        if latest_mtime > 0
        else ""
    )
    return {
        "exists": True,
        "real_files_count": real_files,
        "latest_mtime": latest_mtime,
        "latest_mtime_iso": latest_iso,
        "has_requirements_txt": has_req,
        "has_pyproject_toml": has_pyproj,
        "has_package_json": has_pkg,
        "has_env_file": has_env,
        "has_git_dir": has_git,
        "skipped_venv_detected": skipped_venv,
        "is_empty_skeleton": bool(health["is_empty_skeleton"]),
    }


def _read_desktop_summaries_full(base_dir: Path) -> list[dict[str, Any]]:
    """Read up to 5000 top-level Desktop Antigravity conversations in strict read-only mode
    (including conversations whose `transcript.jsonl` may or may not exist, flagging `has_transcript`)."""
    db_file = base_dir / "conversation_summaries.db"
    if not db_file.exists():
        return []

    uri = db_file.resolve().as_uri() + "?mode=ro"
    items: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
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
        for r in rows:
            step_cnt = int(r["step_count"] or 0)
            if step_cnt <= 0:
                continue
            ws_paths = brain_reader._parse_workspace_paths(r["workspace_uris"] or "")
            if not ws_paths:
                continue
            cid = str(r["conversation_id"] or "").strip()
            if not cid:
                continue
            transcript_path = (
                base_dir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
            )
            items.append(
                {
                    "conversation_id": cid,
                    "title": (r["title"] or "").strip() or "Диалог без названия",
                    "preview": (r["preview"] or "").strip(),
                    "step_count": step_cnt,
                    "last_modified_time": r["last_modified_time"] or "",
                    "last_user_input_time": r["last_user_input_time"] or "",
                    "workspace_paths": ws_paths,
                    "has_transcript": transcript_path.is_file(),
                }
            )
        conn.close()
    except Exception:
        pass
    return items


def _read_hub_db_snapshot_readonly(hub_db_path: Path, owner_id: str = "admin") -> dict[str, Any]:
    """Read `chats`, `shared_projects`, and `project_memories` from `hub.db` strictly with `?mode=ro`
    so scanning migration plans NEVER mutates `hub.db`."""
    imported_by_desktop_id: dict[str, dict[str, Any]] = {}
    external_hub_chat_paths: list[dict[str, Any]] = []
    migrated_map: dict[str, list[str]] = {}
    shared_names_low: set[str] = set()
    shared_paths_norm: set[str] = set()

    if not hub_db_path.is_file():
        return {
            "imported_by_desktop_id": imported_by_desktop_id,
            "external_hub_chat_paths": external_hub_chat_paths,
            "migrated_map": migrated_map,
            "shared_names_low": shared_names_low,
            "shared_paths_norm": shared_paths_norm,
        }

    uri = hub_db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.5)
        conn.row_factory = sqlite3.Row
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "chats" in tables:
            chat_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(chats)").fetchall()}
            has_owner = "owner_id" in chat_cols
            sql_chats = "SELECT id, title, kind, project_name, project_path, source_desktop_id" + (
                ", owner_id FROM chats" if has_owner else " FROM chats"
            )
            for r in conn.execute(sql_chats).fetchall():
                if has_owner and owner_id and str(r["owner_id"] or "admin") != owner_id:
                    continue
                sdid = str(r["source_desktop_id"] or "").strip()
                c_entry = {
                    "id": str(r["id"] or ""),
                    "title": str(r["title"] or ""),
                    "kind": str(r["kind"] or "chat"),
                    "project_name": str(r["project_name"] or ""),
                    "project_path": str(r["project_path"] or ""),
                    "source_desktop_id": sdid,
                }
                if sdid and sdid not in imported_by_desktop_id:
                    imported_by_desktop_id[sdid] = c_entry
                if c_entry["project_path"]:
                    external_hub_chat_paths.append(c_entry)

        if "project_memories" in tables:
            pm_cols = {
                str(r[1]) for r in conn.execute("PRAGMA table_info(project_memories)").fetchall()
            }
            if "migrated_from_paths_json" in pm_cols:
                for r in conn.execute(
                    "SELECT project_name, migrated_from_paths_json FROM project_memories"
                ).fetchall():
                    pname_low = str(r["project_name"] or "").strip().lower()
                    if not pname_low:
                        continue
                    try:
                        parsed = json.loads(r["migrated_from_paths_json"] or "[]")
                    except Exception:
                        parsed = []
                    if isinstance(parsed, list):
                        existing = migrated_map.setdefault(pname_low, [])
                        for item in parsed:
                            np = _norm_path_str(str(item or ""))
                            if np and np not in existing:
                                existing.append(np)

        if "shared_projects" in tables:
            for r in conn.execute("SELECT name, path FROM shared_projects").fetchall():
                sn = str(r["name"] or "").strip().lower()
                sp = _norm_path_str(str(r["path"] or ""))
                if sn:
                    shared_names_low.add(sn)
                if sp:
                    shared_paths_norm.add(sp)

        conn.close()
    except Exception:
        pass

    return {
        "imported_by_desktop_id": imported_by_desktop_id,
        "external_hub_chat_paths": external_hub_chat_paths,
        "migrated_map": migrated_map,
        "shared_names_low": shared_names_low,
        "shared_paths_norm": shared_paths_norm,
    }


def _collapse_nested_workspace_paths(paths_norm: list[str], *, norm_proj_root: str) -> list[str]:
    """If one Desktop conversation lists both `C:/Dev/MyApp` and `C:/Dev/MyApp/frontend`,
    keep only the top-level project root `C:/Dev/MyApp` (while never collapsing into `PROJECTS_ROOT`!)."""
    unique = []
    for p in paths_norm:
        if p and p not in unique:
            unique.append(p)
    out: list[str] = []
    for p in unique:
        is_sub_of_other = False
        for other in unique:
            if other == p:
                continue
            if other == norm_proj_root:
                continue
            if _is_same_or_child_norm(p, other):
                is_sub_of_other = True
                break
        if not is_sub_of_other:
            out.append(p)
    return out


def discover_desktop_migration_plan(
    *,
    owner_id: str = "admin",
    summaries_base_dir: Optional[Path] = None,
    hub_db_path: Optional[Path] = None,
    projects_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
    allow_temp_subdirs: Optional[bool] = None,
) -> dict[str, Any]:
    """Scan Desktop Antigravity `conversation_summaries.db` + `hub.db` + `PROJECTS_ROOT`
    + legacy sibling folders (`D:\\Projects\\*`) and return a complete migration plan."""
    eff_summaries_dir = (summaries_base_dir or brain_reader._eff_agy_app_data_dir()).resolve()
    eff_hub_db = (hub_db_path or database.DB_PATH).resolve()
    eff_proj_root = (projects_root or brain_reader._eff_projects_root()).resolve()
    eff_base_dir = (base_dir or config.BASE_DIR).resolve()
    eff_data_dir = config.DATA_DIR.resolve()

    norm_proj_root = _norm_path_str(eff_proj_root)
    norm_base_dir = _norm_path_str(eff_base_dir)
    legacy_parent_dir = _resolve_safe_legacy_parent_dir(
        eff_base_dir=eff_base_dir,
        eff_proj_root=eff_proj_root,
        allow_temp_subdirs=allow_temp_subdirs,
    )
    norm_legacy_parent = _norm_path_str(legacy_parent_dir) if legacy_parent_dir else ""

    convs = _read_desktop_summaries_full(eff_summaries_dir)
    hub_snap = _read_hub_db_snapshot_readonly(eff_hub_db, owner_id=owner_id)
    imported_by_did = hub_snap["imported_by_desktop_id"]
    migrated_map = hub_snap["migrated_map"]
    shared_paths_norm = hub_snap["shared_paths_norm"]

    # Map normalized workspace root -> list of Desktop conversation dicts
    convs_by_root: dict[str, list[dict[str, Any]]] = {}
    desktop_roots: set[str] = set()
    legacy_sibling_norms: set[str] = set()
    filtered_out_paths: set[str] = set()

    for conv in convs:
        eligible_norms: list[str] = []
        for raw_wp in conv["workspace_paths"]:
            np = _norm_path_str(raw_wp)
            if not np:
                continue
            if np in shared_paths_norm:
                continue
            # If path is inside `PROJECTS_ROOT/<Proj>/<subdir>`, normalize to `PROJECTS_ROOT/<Proj>`
            if np != norm_proj_root and np.startswith(norm_proj_root + "/"):
                rel_after = np[len(norm_proj_root) + 1 :]
                top_proj_seg = rel_after.split("/", 1)[0]
                np = f"{norm_proj_root}/{top_proj_seg}"
            if is_eligible_project_workspace_path(
                np,
                projects_root=eff_proj_root,
                base_dir=eff_base_dir,
                data_dir=eff_data_dir,
                agy_app_dir=eff_summaries_dir,
                allow_temp_subdirs=allow_temp_subdirs,
                allow_in_projects_root=True,
            ):
                eligible_norms.append(np)
            else:
                filtered_out_paths.add(np)

        collapsed = _collapse_nested_workspace_paths(eligible_norms, norm_proj_root=norm_proj_root)
        for root_np in collapsed:
            convs_by_root.setdefault(root_np, []).append(conv)
            desktop_roots.add(root_np)
            if norm_legacy_parent and root_np.startswith(norm_legacy_parent + "/"):
                rel_sib = root_np[len(norm_legacy_parent) + 1 :]
                if "/" not in rel_sib and root_np != norm_base_dir:
                    legacy_sibling_norms.add(root_np)

    # Also include external `project_path`s referenced in `hub.db` `chats`
    for hc in hub_snap["external_hub_chat_paths"]:
        np = _norm_path_str(hc["project_path"])
        if not np or np in shared_paths_norm:
            continue
        if np != norm_proj_root and np.startswith(norm_proj_root + "/"):
            continue
        if is_eligible_project_workspace_path(
            np,
            projects_root=eff_proj_root,
            base_dir=eff_base_dir,
            data_dir=eff_data_dir,
            agy_app_dir=eff_summaries_dir,
            allow_temp_subdirs=allow_temp_subdirs,
            allow_in_projects_root=False,
        ):
            convs_by_root.setdefault(np, [])
            if norm_legacy_parent and np.startswith(norm_legacy_parent + "/"):
                rel_sib = np[len(norm_legacy_parent) + 1 :]
                if "/" not in rel_sib and np != norm_base_dir:
                    legacy_sibling_norms.add(np)

    # `legacy_parent_scan`: discover sibling folders in `D:\Projects\*` even if created only in Hub
    if legacy_parent_dir is not None and legacy_parent_dir.is_dir():
        try:
            with os.scandir(legacy_parent_dir) as sib_it:
                for entry in sib_it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith("."):
                        continue
                    cand_p = Path(entry.path)
                    if config._is_junction_or_symlink(cand_p):
                        continue
                    try:
                        cand_res = cand_p.resolve()
                    except OSError:
                        continue
                    if cand_res == eff_base_dir or _is_same_or_child_norm(
                        _norm_path_str(cand_res), norm_base_dir
                    ):
                        continue
                    np_sib = _norm_path_str(cand_res)
                    if not np_sib or np_sib in shared_paths_norm:
                        continue
                    if is_eligible_project_workspace_path(
                        cand_res,
                        projects_root=eff_proj_root,
                        base_dir=eff_base_dir,
                        data_dir=eff_data_dir,
                        agy_app_dir=eff_summaries_dir,
                        allow_temp_subdirs=allow_temp_subdirs,
                        allow_in_projects_root=False,
                    ) and _looks_like_sibling_project_dir(cand_res):
                        legacy_sibling_norms.add(np_sib)
                        convs_by_root.setdefault(np_sib, [])
        except OSError:
            pass

    # Scan existing folders in `PROJECTS_ROOT` (`D:\Projects\*`)
    existing_hub_dirs_by_low: dict[str, Path] = {}
    if eff_proj_root.is_dir():
        try:
            with os.scandir(eff_proj_root) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith("."):
                        continue
                    if entry.name.lower() == eff_base_dir.name.lower():
                        continue
                    existing_hub_dirs_by_low[entry.name.lower()] = Path(entry.path).resolve()
        except OSError:
            pass

    # Separate roots already inside `PROJECTS_ROOT` vs external roots
    in_hub_roots: set[str] = set()
    external_roots: list[str] = []
    for root_np in convs_by_root.keys():
        if root_np.startswith(norm_proj_root + "/"):
            in_hub_roots.add(root_np)
        else:
            external_roots.append(root_np)

    # Sort external roots so existing non-empty folders come before missing ones
    def _ext_sort_key(np: str) -> tuple[int, int, str]:
        p_obj = Path(_to_native_path_str(np))
        exists_flag = 1 if p_obj.is_dir() else 0
        chat_cnt = len(convs_by_root.get(np) or [])
        return (-exists_flag, -chat_cnt, np)

    external_roots.sort(key=_ext_sort_key)

    projects_plan: list[dict[str, Any]] = []
    assigned_names_to_ext: dict[str, str] = {}
    handled_hub_lows: set[str] = set()

    def _format_conv_list(raw_convs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
        seen_cids: set[str] = set()
        formatted: list[dict[str, Any]] = []
        unimp = 0
        imp = 0
        for c in raw_convs:
            cid = c["conversation_id"]
            if cid in seen_cids:
                continue
            seen_cids.add(cid)
            imp_info = imported_by_did.get(cid)
            is_imp = imp_info is not None
            if is_imp:
                imp += 1
            elif c.get("has_transcript"):
                unimp += 1
            formatted.append(
                {
                    "conversation_id": cid,
                    "title": c["title"],
                    "step_count": c["step_count"],
                    "last_modified_time": c["last_modified_time"],
                    "has_transcript": bool(c.get("has_transcript")),
                    "already_imported": is_imp,
                    "imported_chat_id": imp_info["id"] if imp_info else "",
                }
            )
        return formatted, unimp, imp

    # 1. Process all external project roots
    for ext_np in external_roots:
        raw_src_candidate = Path(_to_native_path_str(ext_np))
        if raw_src_candidate.exists():
            try:
                src_path = raw_src_candidate.resolve()
            except OSError:
                src_path = raw_src_candidate
            raw_leaf = src_path.name or ext_np.rstrip("/").split("/")[-1]
        else:
            src_path = raw_src_candidate
            raw_leaf = ext_np.rstrip("/").split("/")[-1]
        base_proj_name = sanitize_migrated_project_name(raw_leaf)
        if not base_proj_name:
            continue

        # Check if this exact external path was already linked to an existing Hub project name
        matched_hub_low = ""
        for p_low, mig_norms in migrated_map.items():
            if ext_np in mig_norms and p_low in existing_hub_dirs_by_low:
                matched_hub_low = p_low
                break

        if matched_hub_low:
            hub_dir = existing_hub_dirs_by_low[matched_hub_low]
            proj_name = hub_dir.name
        elif base_proj_name.lower() in existing_hub_dirs_by_low and base_proj_name.lower() not in assigned_names_to_ext:
            hub_dir = existing_hub_dirs_by_low[base_proj_name.lower()]
            proj_name = hub_dir.name
        else:
            cand_name = base_proj_name
            suffix_idx = 2
            while cand_name.lower() in assigned_names_to_ext:
                cand_name = f"{base_proj_name}_{suffix_idx}"
                suffix_idx += 1
            proj_name = cand_name
            hub_dir = (eff_proj_root / proj_name).resolve()

        assigned_names_to_ext[proj_name.lower()] = ext_np
        handled_hub_lows.add(proj_name.lower())

        src_stats = _inspect_source_dir_stats(src_path)
        dest_stats = _inspect_source_dir_stats(hub_dir)

        # Merge Desktop conversations from both `ext_np` and `PROJECTS_ROOT/<proj_name>`
        hub_np = f"{norm_proj_root}/{proj_name.lower()}"
        combined_convs = list(convs_by_root.get(ext_np) or [])
        for in_np in in_hub_roots:
            if in_np.lower() == hub_np.lower():
                combined_convs.extend(convs_by_root.get(in_np) or [])
        conv_items, unimp_cnt, imp_cnt = _format_conv_list(combined_convs)

        already_migrated_here = ext_np in (migrated_map.get(proj_name.lower()) or [])

        if not src_stats["exists"]:
            if dest_stats["exists"] and not dest_stats["is_empty_skeleton"]:
                action = "already_in_hub"
            else:
                action = "missing_source"
        elif src_stats["is_empty_skeleton"] and src_stats["real_files_count"] == 0:
            if dest_stats["exists"] and not dest_stats["is_empty_skeleton"]:
                action = "already_in_hub"
            else:
                action = "copy_to_hub"
        elif not dest_stats["exists"] or dest_stats["is_empty_skeleton"]:
            action = "copy_to_hub"
        elif already_migrated_here:
            action = "already_in_hub"
        else:
            action = "conflict_both_non_empty"

        is_sib = ext_np in legacy_sibling_norms
        has_desk = ext_np in desktop_roots
        source_origin = (
            "both"
            if (is_sib and has_desk)
            else ("legacy_sibling" if is_sib else "desktop")
        )

        projects_plan.append(
            {
                "project_name": proj_name,
                "source_path": str(src_path),
                "dest_path": str(hub_dir),
                "action": action,
                "source_origin": source_origin,
                "is_legacy_sibling": bool(is_sib),
                "source_exists": bool(src_stats["exists"]),
                "real_files_count": int(src_stats["real_files_count"]),
                "source_latest_mtime": src_stats["latest_mtime_iso"],
                "dest_exists": bool(dest_stats["exists"]),
                "dest_is_skeleton": bool(dest_stats["is_empty_skeleton"]),
                "dest_files_count": int(dest_stats["real_files_count"]),
                "dest_latest_mtime": dest_stats["latest_mtime_iso"],
                "has_requirements_txt": bool(
                    src_stats["has_requirements_txt"] or dest_stats["has_requirements_txt"]
                ),
                "has_pyproject_toml": bool(
                    src_stats["has_pyproject_toml"] or dest_stats["has_pyproject_toml"]
                ),
                "has_package_json": bool(
                    src_stats["has_package_json"] or dest_stats["has_package_json"]
                ),
                "has_env_file": bool(src_stats["has_env_file"]),
                "has_git_dir": bool(src_stats["has_git_dir"]),
                "skipped_venv_detected": bool(src_stats["skipped_venv_detected"]),
                "unimported_conversations_count": unimp_cnt,
                "already_imported_count": imp_cnt,
                "total_conversations_count": len(conv_items),
                "conversations": conv_items,
            }
        )

    # 2. Include projects already in `PROJECTS_ROOT` (either with Desktop history or empty skeletons)
    for p_low, hub_dir in sorted(existing_hub_dirs_by_low.items()):
        if p_low in handled_hub_lows:
            continue
        hub_np = _norm_path_str(hub_dir)
        raw_convs: list[dict[str, Any]] = []
        for in_np in in_hub_roots:
            if in_np.lower() == hub_np.lower():
                raw_convs.extend(convs_by_root.get(in_np) or [])
        # Also include conversations matching previously recorded `migrated_from_paths`
        for mig_np in migrated_map.get(p_low) or []:
            raw_convs.extend(convs_by_root.get(mig_np) or [])

        dest_stats = _inspect_source_dir_stats(hub_dir)
        if not raw_convs and not dest_stats["is_empty_skeleton"]:
            #Still list `already_in_hub` if it has Desktop chats or skeleton; skip noisy 0-chat healthy folders?
            # Actually listing `already_in_hub` projects when they have Desktop chats (or skeleton) keeps the table crystal clear!
            continue

        conv_items, unimp_cnt, imp_cnt = _format_conv_list(raw_convs)
        projects_plan.append(
            {
                "project_name": hub_dir.name,
                "source_path": str(hub_dir),
                "dest_path": str(hub_dir),
                "action": "already_in_hub",
                "source_origin": "in_hub",
                "is_legacy_sibling": False,
                "source_exists": True,
                "real_files_count": int(dest_stats["real_files_count"]),
                "source_latest_mtime": dest_stats["latest_mtime_iso"],
                "dest_exists": True,
                "dest_is_skeleton": bool(dest_stats["is_empty_skeleton"]),
                "dest_files_count": int(dest_stats["real_files_count"]),
                "dest_latest_mtime": dest_stats["latest_mtime_iso"],
                "has_requirements_txt": bool(dest_stats["has_requirements_txt"]),
                "has_pyproject_toml": bool(dest_stats["has_pyproject_toml"]),
                "has_package_json": bool(dest_stats["has_package_json"]),
                "has_env_file": bool(dest_stats["has_env_file"]),
                "has_git_dir": bool(dest_stats["has_git_dir"]),
                "skipped_venv_detected": bool(dest_stats["skipped_venv_detected"]),
                "unimported_conversations_count": unimp_cnt,
                "already_imported_count": imp_cnt,
                "total_conversations_count": len(conv_items),
                "conversations": conv_items,
            }
        )

    # Sort plan: `copy_to_hub` first, then `conflict_both_non_empty`, then `already_in_hub` (with unimported chats first), then `missing_source`
    action_rank = {
        "copy_to_hub": 0,
        "conflict_both_non_empty": 1,
        "already_in_hub": 2,
        "missing_source": 3,
    }
    projects_plan.sort(
        key=lambda x: (
            action_rank.get(x["action"], 9),
            -int(x["unimported_conversations_count"]),
            x["project_name"].lower(),
        )
    )

    counts_by_action: dict[str, int] = {
        "copy_to_hub": 0,
        "already_in_hub": 0,
        "conflict_both_non_empty": 0,
        "missing_source": 0,
    }
    total_unimported_chats = 0
    for item in projects_plan:
        act = item["action"]
        counts_by_action[act] = counts_by_action.get(act, 0) + 1
        total_unimported_chats += int(item["unimported_conversations_count"])

    return {
        "ok": True,
        "projects_root": str(eff_proj_root),
        "legacy_parent_root": str(legacy_parent_dir) if legacy_parent_dir else "",
        "summaries_db_found": (eff_summaries_dir / "conversation_summaries.db").is_file(),
        "total_desktop_conversations_scanned": len(convs),
        "filtered_system_paths_count": len(filtered_out_paths),
        "counts_by_action": counts_by_action,
        "total_unimported_conversations": total_unimported_chats,
        "projects": projects_plan,
    }


def _git_dir_has_commits(git_dir: Path) -> bool:
    if not git_dir.is_dir():
        return False
    packed = git_dir / "packed-refs"
    if packed.is_file() and packed.stat().st_size > 0:
        return True
    heads = git_dir / "refs" / "heads"
    if heads.is_dir():
        try:
            for entry in os.scandir(heads):
                if entry.is_file():
                    return True
                if entry.is_dir():
                    for _sub in os.walk(entry.path):
                        if _sub[2]:
                            return True
        except OSError:
            pass
    return False


def _copy_tree_non_destructive_sync(
    source_path: Path,
    dest_path: Path,
    *,
    only_missing: bool = False,
) -> dict[str, Any]:
    """Strictly non-destructive copy (`shutil.copy2` ONLY) from `source_path` into `dest_path`.

    NEVER deletes, moves, or modifies anything in `source_path`.
    Skips `.venv`/`venv`/`env`/`node_modules`/`__pycache__`/caches and symlinks/junctions.
    Preserves `.env*`, SQLite DBs, and real `.git` (replacing commitless skeleton `.git` if needed).
    """
    src_res = source_path.resolve()
    dst_res = dest_path.resolve()
    if src_res == dst_res:
        ensure_isolated_project_git_boundary(dst_res)
        return {
            "copied_files": 0,
            "skipped_existing_files": 0,
            "skipped_dirs": [],
            "skipped_symlinks": 0,
            "git_copied": False,
        }

    # Guard against copying parent into its own subdirectory
    if _is_same_or_child_norm(_norm_path_str(dst_res), _norm_path_str(src_res)):
        raise ValueError(
            f"Целевая папка '{dst_res}' находится внутри исходной папки '{src_res}'"
        )

    dest_health_before = brain_reader.inspect_project_dir_health(dst_res)
    dst_was_skeleton = bool(not dst_res.exists() or dest_health_before["is_empty_skeleton"])
    dst_had_stub_readme = bool(dest_health_before.get("has_stub_readme"))

    dst_res.mkdir(parents=True, exist_ok=True)

    copied_files = 0
    skipped_existing = 0
    skipped_dirs: list[str] = []
    skipped_symlinks = 0
    git_copied = False

    # Copy real `.git` when target has no `.git` or has only an empty skeleton `.git` without commits
    src_git = src_res / ".git"
    dst_git = dst_res / ".git"
    if src_git.is_dir() and not config._is_junction_or_symlink(src_git):
        can_copy_git = (not dst_git.exists()) or (
            dst_was_skeleton and not _git_dir_has_commits(dst_git)
        )
        if can_copy_git:
            for root_s, dirs_s, files_s in os.walk(src_git, followlinks=False):
                cur_src_g = Path(root_s)
                rel_g = cur_src_g.relative_to(src_git)
                # Filter out symlinks inside `.git`
                dirs_s[:] = [
                    d for d in dirs_s if not config._is_junction_or_symlink(cur_src_g / d)
                ]
                cur_dst_g = dst_git / rel_g
                cur_dst_g.mkdir(parents=True, exist_ok=True)
                for fn in files_s:
                    sf = cur_src_g / fn
                    if config._is_junction_or_symlink(sf):
                        continue
                    df = cur_dst_g / fn
                    try:
                        shutil.copy2(sf, df)
                    except PermissionError:
                        # Handle read-only `.git/objects/pack/*` on Windows target if re-copying skeleton
                        try:
                            os.chmod(df, 0o666)
                            shutil.copy2(sf, df)
                        except OSError:
                            pass
            git_copied = True

    # Walk project files skipping virtualenvs/caches/symlinks
    for root_str, dirs, files in os.walk(src_res, followlinks=False):
        cur_src = Path(root_str)
        rel_dir = cur_src.relative_to(src_res)
        is_top_level = rel_dir == Path(".")

        kept_dirs: list[str] = []
        for dname in dirs:
            dpath = cur_src / dname
            dlow = dname.lower()
            if config._is_junction_or_symlink(dpath):
                skipped_symlinks += 1
                continue
            if dlow == ".git":
                continue
            if dlow in SKIP_MIGRATION_DIRS_LOWER:
                rel_skip = str(rel_dir / dname) if not is_top_level else dname
                if len(skipped_dirs) < 40:
                    skipped_dirs.append(rel_skip)
                continue
            kept_dirs.append(dname)
        dirs[:] = kept_dirs

        cur_dst = dst_res / rel_dir
        cur_dst.mkdir(parents=True, exist_ok=True)

        for fname in files:
            src_file = cur_src / fname
            if config._is_junction_or_symlink(src_file):
                skipped_symlinks += 1
                continue
            dst_file = cur_dst / fname
            is_stub_readme_target = (
                is_top_level
                and fname.lower() == "readme.md"
                and dst_was_skeleton
                and dst_had_stub_readme
            )
            if dst_file.exists() and only_missing and not is_stub_readme_target:
                skipped_existing += 1
                continue
            shutil.copy2(src_file, dst_file)
            copied_files += 1

    ensure_isolated_project_git_boundary(dst_res)
    return {
        "copied_files": copied_files,
        "skipped_existing_files": skipped_existing,
        "skipped_dirs": skipped_dirs,
        "skipped_symlinks": skipped_symlinks,
        "git_copied": git_copied,
    }


def _resolve_base_system_python() -> str:
    """Resolve base Python interpreter (`sys.base_prefix`) so `python -m venv .venv` works cleanly."""
    base_pref = Path(getattr(sys, "base_prefix", sys.prefix))
    if os.name == "nt":
        cand = base_pref / "python.exe"
    else:
        cand = base_pref / "bin" / "python3"
        if not cand.exists():
            cand = base_pref / "bin" / "python"
    if cand.is_file():
        return str(cand)
    return sys.executable


def _rebuild_project_venv_sync(dest_path: Path, *, install_requirements: bool = True) -> dict[str, Any]:
    """Create a clean `.venv` inside `dest_path` with `0% GPU` (`CUDA_VISIBLE_DEVICES=""`) and hidden window."""
    req_file = dest_path / "requirements.txt"
    venv_dir = dest_path / ".venv"
    venv_py = (
        venv_dir / "Scripts" / "python.exe"
        if os.name == "nt"
        else venv_dir / "bin" / "python"
    )

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    logs: list[str] = []
    try:
        if not venv_py.is_file():
            base_py = _resolve_base_system_python()
            proc_v = subprocess.run(
                [base_py, "-m", "venv", str(venv_dir)],
                cwd=str(dest_path),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=120,
                **subprocess_hidden_kwargs(),
            )
            out_v = (proc_v.stdout or b"").decode("utf-8", errors="replace").strip()
            if out_v:
                logs.append(out_v[-1200:])
            if proc_v.returncode != 0 or not venv_py.is_file():
                return {
                    "ok": False,
                    "error": f"Не удалось создать .venv (код {proc_v.returncode})",
                    "log": "\n".join(logs),
                }

        if install_requirements and req_file.is_file():
            proc_p = subprocess.run(
                [str(venv_py), "-m", "pip", "install", "-r", str(req_file)],
                cwd=str(dest_path),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=240,
                **subprocess_hidden_kwargs(),
            )
            out_p = (proc_p.stdout or b"").decode("utf-8", errors="replace").strip()
            if out_p:
                logs.append(out_p[-1600:])
            if proc_p.returncode != 0:
                return {
                    "ok": False,
                    "error": f"pip install завершился с кодом {proc_p.returncode}",
                    "log": "\n".join(logs),
                }

        return {"ok": True, "venv_python": str(venv_py), "log": "\n".join(logs)[-2000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Таймаут при пересборке .venv / установке зависимостей", "log": "\n".join(logs)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "log": "\n".join(logs)}


async def _import_and_relink_project_conversations(
    *,
    project_name: str,
    dest_path: Path,
    source_path: Optional[Path],
    conversations: list[dict[str, Any]],
    owner_id: str = "admin",
    default_model: str = "gemini-2.5-pro",
    summaries_base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Auto-import unimported Desktop `transcript.jsonl` chats into `hub.db` and relink
    already-imported chats to `kind='project'`, `project_name`, `project_path=str(dest_path)` without duplicates."""
    imported_count = 0
    relinked_count = 0
    imported_chat_ids: list[str] = []
    dest_str = str(dest_path.resolve())

    for conv in conversations:
        cid = str(conv.get("conversation_id") or "").strip()
        if not cid:
            continue
        existing = await database.find_chat_by_source_desktop_id(cid, owner_id=owner_id)
        if existing:
            needs_relink = (
                existing.get("kind") != "project"
                or existing.get("project_name") != project_name
                or _norm_path_str(existing.get("project_path") or "") != _norm_path_str(dest_str)
            )
            if needs_relink:
                await database.update_chat(
                    existing["id"],
                    kind="project",
                    project_name=project_name,
                    project_path=dest_str,
                    touch_updated_at=False,
                )
                relinked_count += 1
            continue

        if not conv.get("has_transcript", True):
            continue
        messages = await asyncio.to_thread(
            brain_reader.read_transcript_messages,
            cid,
            base_dir=summaries_base_dir,
        )
        if not messages:
            continue

        title = str(conv.get("title") or "").strip() or "Импорт из Antigravity Desktop"
        new_chat = await database.create_chat(
            owner_id=owner_id,
            kind="project",
            project_name=project_name,
            project_path=dest_str,
            title=title,
            model=default_model,
            access_mode="read_only",
            source_desktop_id=cid,
        )
        await database.add_messages_bulk(
            new_chat["id"],
            user_id=owner_id,
            messages=messages,
        )
        imported_count += 1
        imported_chat_ids.append(new_chat["id"])

    # Also relink any existing Hub chats whose `project_path` pointed to the old `source_path`
    if source_path is not None:
        src_norm = _norm_path_str(source_path)
        dst_norm = _norm_path_str(dest_path)
        if src_norm and src_norm != dst_norm:
            all_chats = await database.list_chats(owner_id=owner_id, include_context_pct=False)
            for ch in all_chats:
                ch_p_norm = _norm_path_str(ch.get("project_path") or "")
                if ch_p_norm and ch_p_norm == src_norm:
                    await database.update_chat(
                        ch["id"],
                        kind="project",
                        project_name=project_name,
                        project_path=dest_str,
                        touch_updated_at=False,
                    )
                    relinked_count += 1

    return {
        "imported_chats_count": imported_count,
        "relinked_chats_count": relinked_count,
        "imported_chat_ids": imported_chat_ids,
    }


async def _record_migrated_source_path(
    project_name: str,
    source_path: Path,
    *,
    scope_key: str = "admin",
    owner_id: str = "admin",
) -> list[str]:
    eff_scope = (
        scope_key
        if ":" in (scope_key or "")
        else database.build_project_memory_scope_key(
            project_name, owner_id=owner_id or scope_key or "admin", is_shared=False
        )
    )
    mem = await database.get_project_memory(
        eff_scope, project_name=project_name, owner_id=owner_id
    )
    cur_paths = list(mem.get("migrated_from_paths") or [])
    src_str = str(source_path)
    src_norm = _norm_path_str(src_str)
    if src_norm and all(_norm_path_str(p) != src_norm for p in cur_paths):
        cur_paths.append(src_str)
        saved = await database.save_project_memory(
            eff_scope,
            owner_id=owner_id,
            project_name=project_name,
            migrated_from_paths=cur_paths,
        )
        return list(saved.get("migrated_from_paths") or cur_paths)
    return cur_paths


async def import_folder_into_project(
    *,
    project_name: str,
    dest_path: Path,
    source_path: str | Path,
    scope_key: str = "admin",
    owner_id: str = "admin",
    only_missing_on_non_empty: bool = True,
    import_chats: bool = True,
    rebuild_venv: bool = False,
    default_model: str = "gemini-2.5-pro",
    summaries_base_dir: Optional[Path] = None,
    allow_temp_subdirs: Optional[bool] = None,
) -> dict[str, Any]:
    """Targeted non-destructive folder import for a single project (`POST /api/projects/{name}/import-folder`)."""
    raw_src = str(source_path or "").strip()
    if not raw_src:
        raise ValueError("Укажите путь к исходной папке проекта")
    if "\x00" in raw_src or raw_src.startswith("\\\\"):
        raise ValueError("Сетевые UNC-пути и спецсимволы запрещены")
    raw_parts = [seg for seg in re.split(r"[\\/]+", raw_src) if seg]
    if ".." in raw_parts:
        raise ValueError("Переходы '..' в пути запрещены")

    src_p = Path(raw_src)
    if not src_p.is_absolute():
        raise ValueError("Укажите полный абсолютный путь к исходной папке")
    if config._is_junction_or_symlink(src_p):
        raise ValueError("Симлинки и NTFS-junction не допускаются как источник импорта")
    src_res = src_p.resolve()
    if not src_res.exists() or not src_res.is_dir():
        raise ValueError(f"Исходная папка не найдена: {raw_src}")

    eff_proj_root = brain_reader._eff_projects_root().resolve()
    if not is_eligible_project_workspace_path(
        src_res,
        projects_root=eff_proj_root,
        allow_temp_subdirs=allow_temp_subdirs,
        allow_in_projects_root=False,
    ):
        raise ValueError(
            f"Системная или служебная директория '{src_res}' запрещена для импорта политикой безопасности"
        )

    dst_res = dest_path.resolve()
    dest_health = brain_reader.inspect_project_dir_health(dst_res)
    only_missing = bool(
        only_missing_on_non_empty
        and dest_health["exists"]
        and not dest_health["is_empty_skeleton"]
    )

    copy_res = await asyncio.to_thread(
        _copy_tree_non_destructive_sync,
        src_res,
        dst_res,
        only_missing=only_missing,
    )
    migrated_paths = await _record_migrated_source_path(
        project_name, src_res, scope_key=scope_key, owner_id=owner_id
    )

    chat_res = {"imported_chats_count": 0, "relinked_chats_count": 0, "imported_chat_ids": []}
    if import_chats:
        eff_sum_dir = (summaries_base_dir or brain_reader._eff_agy_app_data_dir()).resolve()
        all_convs = await asyncio.to_thread(_read_desktop_summaries_full, eff_sum_dir)
        src_norm = _norm_path_str(src_res)
        dst_norm = _norm_path_str(dst_res)
        matching_convs = [
            c
            for c in all_convs
            if any(
                wp == src_norm
                or wp.startswith(src_norm + "/")
                or wp == dst_norm
                or wp.startswith(dst_norm + "/")
                for wp in c["workspace_paths"]
            )
        ]
        chat_res = await _import_and_relink_project_conversations(
            project_name=project_name,
            dest_path=dst_res,
            source_path=src_res,
            conversations=matching_convs,
            owner_id=owner_id,
            default_model=default_model,
            summaries_base_dir=eff_sum_dir,
        )

    venv_res = None
    if rebuild_venv and (dst_res / "requirements.txt").is_file():
        venv_res = await asyncio.to_thread(_rebuild_project_venv_sync, dst_res)

    return {
        "ok": True,
        "project_name": project_name,
        "source_path": str(src_res),
        "dest_path": str(dst_res),
        "copied_files": copy_res["copied_files"],
        "skipped_existing_files": copy_res["skipped_existing_files"],
        "skipped_dirs": copy_res["skipped_dirs"],
        "git_copied": copy_res["git_copied"],
        "migrated_from_paths": migrated_paths,
        **chat_res,
        "venv_rebuild": venv_res,
    }


def _move_legacy_sibling_to_hub_sync(
    source_path: Path,
    dest_path: Path,
) -> dict[str, Any]:
    """Consolidate a legacy sibling project folder (`D:\\Projects\\<Proj>`)
    directly into `users/admin/projects/<Proj>`.

    Removes empty target skeleton if present, moves source folder, strips path-bound `.venv`
    launchers, and on Windows `PermissionError` safely falls back to `shutil.copy2` with a clear warning.
    """
    src_res = source_path.resolve()
    dst_res = dest_path.resolve()
    if _is_same_or_child_norm(_norm_path_str(dst_res), _norm_path_str(src_res)):
        raise ValueError(f"Целевая папка '{dst_res}' находится внутри исходной папки '{src_res}'")

    src_stats = _inspect_source_dir_stats(src_res)
    if dst_res.exists():
        dest_health = brain_reader.inspect_project_dir_health(dst_res)
        if not dest_health["is_empty_skeleton"]:
            copy_res = _copy_tree_non_destructive_sync(src_res, dst_res, only_missing=True)
            return {
                **copy_res,
                "moved": False,
                "warning": "",
            }
        shutil.rmtree(dst_res, ignore_errors=True)

    dst_res.parent.mkdir(parents=True, exist_ok=True)
    if not dst_res.exists():
        try:
            shutil.move(str(src_res), str(dst_res))
            skipped_dirs: list[str] = []
            for vname in VENV_DIR_NAMES_LOWER:
                cand_v = dst_res / vname
                if cand_v.is_dir() and not config._is_junction_or_symlink(cand_v):
                    shutil.rmtree(cand_v, ignore_errors=True)
                    skipped_dirs.append(vname)
            ensure_isolated_project_git_boundary(dst_res)
            return {
                "copied_files": int(src_stats["real_files_count"]),
                "skipped_existing_files": 0,
                "skipped_dirs": skipped_dirs,
                "skipped_symlinks": 0,
                "git_copied": bool(src_stats["has_git_dir"]),
                "moved": True,
                "warning": "",
            }
        except (PermissionError, OSError):
            pass

    # Fallback when Windows process holds a lock on the old folder
    copy_res = _copy_tree_non_destructive_sync(src_res, dst_res, only_missing=False)
    try:
        shutil.rmtree(src_res)
    except (PermissionError, OSError):
        pass
    still_locked = src_res.exists()
    return {
        **copy_res,
        "moved": not still_locked,
        "warning": (
            "Скопировано; закройте процесс в старой папке, чтобы удалить её"
            if still_locked
            else ""
        ),
    }


async def _relink_project_chats_path_in_db(
    project_name: str,
    dest_path: Path,
    *,
    source_path: Optional[Path] = None,
    hub_db_path: Optional[Path] = None,
) -> int:
    """Ensure `chats.project_path` points to `dest_path` for all project chats of `project_name`."""
    import aiosqlite

    eff_db = (hub_db_path or database.DB_PATH).resolve()
    if not eff_db.is_file():
        return 0
    dest_str = str(dest_path.resolve())
    src_str = str(source_path.resolve()) if source_path is not None else ""
    async with aiosqlite.connect(eff_db, timeout=15.0) as db:
        cur = await db.execute(
            """
            UPDATE chats
            SET project_path = ?
            WHERE kind = 'project'
              AND project_path != ?
              AND (LOWER(project_name) = LOWER(?) OR (? != '' AND LOWER(project_path) = LOWER(?)));
            """,
            (dest_str, dest_str, project_name, src_str, src_str),
        )
        await db.commit()
        return int(cur.rowcount or 0)


async def execute_desktop_migration(
    *,
    selected_projects: Optional[list[str]] = None,
    import_chats: bool = True,
    merge_missing_on_conflict: bool = False,
    rebuild_venv: bool = False,
    move_legacy_siblings: bool = False,
    owner_id: str = "admin",
    default_model: str = "gemini-2.5-pro",
    summaries_base_dir: Optional[Path] = None,
    hub_db_path: Optional[Path] = None,
    projects_root: Optional[Path] = None,
    base_dir: Optional[Path] = None,
    allow_temp_subdirs: Optional[bool] = None,
) -> dict[str, Any]:
    """Execute Desktop & sibling project migration into `users/admin/projects`."""
    eff_base_dir = (base_dir or config.BASE_DIR).resolve()
    plan = await asyncio.to_thread(
        discover_desktop_migration_plan,
        owner_id=owner_id,
        summaries_base_dir=summaries_base_dir,
        hub_db_path=hub_db_path,
        projects_root=projects_root,
        base_dir=eff_base_dir,
        allow_temp_subdirs=allow_temp_subdirs,
    )
    legacy_parent_str = str(plan.get("legacy_parent_root") or "").strip()
    norm_legacy_parent = _norm_path_str(legacy_parent_str) if legacy_parent_str else ""
    norm_base_dir = _norm_path_str(eff_base_dir)

    selected_low: Optional[set[str]] = None
    if selected_projects is not None:
        selected_low = {str(p or "").strip().lower() for p in selected_projects if str(p or "").strip()}

    results: list[dict[str, Any]] = []
    total_copied_files = 0
    total_imported_chats = 0
    total_relinked_chats = 0
    migrated_projects_count = 0

    for item in plan["projects"]:
        pname = item["project_name"]
        action = item["action"]
        if selected_low is not None:
            if pname.lower() not in selected_low:
                continue
        else:
            # By default when `selected_projects` is None, run all `copy_to_hub` + chat imports on `already_in_hub`
            if action == "conflict_both_non_empty" and not merge_missing_on_conflict:
                results.append(
                    {
                        "project_name": pname,
                        "action": action,
                        "status": "skipped_conflict",
                        "reason": "Обе папки содержат файлы — включите слияние недостающих файлов для переноса",
                        "copied_files": 0,
                        "imported_chats_count": 0,
                        "relinked_chats_count": 0,
                    }
                )
                continue
            if action == "missing_source" and (
                not import_chats or item["unimported_conversations_count"] == 0
            ):
                continue

        src_path = Path(item["source_path"])
        dst_path = Path(item["dest_path"])
        copied_files = 0
        skipped_existing = 0
        skipped_dirs: list[str] = []
        git_copied = False
        moved_flag = False
        warning_msg = ""
        db_relinked_direct = 0
        status_label = "ok"

        if action == "copy_to_hub" and item["source_exists"]:
            src_resolved = src_path.resolve()
            src_parent_norm = _norm_path_str(src_resolved.parent)
            can_move_sibling = bool(
                move_legacy_siblings
                and item.get("is_legacy_sibling")
                and norm_legacy_parent
                and src_parent_norm == norm_legacy_parent
                and _norm_path_str(src_resolved) != norm_base_dir
            )
            if can_move_sibling:
                copy_res = await asyncio.to_thread(
                    _move_legacy_sibling_to_hub_sync,
                    src_path,
                    dst_path,
                )
                moved_flag = bool(copy_res.get("moved"))
                warning_msg = str(copy_res.get("warning") or "")
                status_label = "moved" if moved_flag else "copied"
            else:
                copy_res = await asyncio.to_thread(
                    _copy_tree_non_destructive_sync,
                    src_path,
                    dst_path,
                    only_missing=False,
                )
                status_label = "copied"

            copied_files = copy_res["copied_files"]
            skipped_existing = copy_res["skipped_existing_files"]
            skipped_dirs = copy_res["skipped_dirs"]
            git_copied = copy_res["git_copied"]
            await asyncio.to_thread(ensure_isolated_project_git_boundary, dst_path)
            db_relinked_direct = await _relink_project_chats_path_in_db(
                pname, dst_path, source_path=src_path, hub_db_path=hub_db_path
            )
            await _record_migrated_source_path(
                pname, src_path, scope_key=owner_id, owner_id=owner_id
            )
            migrated_projects_count += 1
        elif action == "conflict_both_non_empty":
            if not merge_missing_on_conflict:
                results.append(
                    {
                        "project_name": pname,
                        "action": action,
                        "source_path": str(src_path),
                        "dest_path": str(dst_path),
                        "status": "skipped_conflict",
                        "reason": "Конфликт непустых папок (пропущено без перезаписи)",
                        "copied_files": 0,
                        "imported_chats_count": 0,
                        "relinked_chats_count": 0,
                    }
                )
                continue
            copy_res = await asyncio.to_thread(
                _copy_tree_non_destructive_sync,
                src_path,
                dst_path,
                only_missing=True,
            )
            copied_files = copy_res["copied_files"]
            skipped_existing = copy_res["skipped_existing_files"]
            skipped_dirs = copy_res["skipped_dirs"]
            git_copied = copy_res["git_copied"]
            await asyncio.to_thread(ensure_isolated_project_git_boundary, dst_path)
            db_relinked_direct = await _relink_project_chats_path_in_db(
                pname, dst_path, source_path=src_path, hub_db_path=hub_db_path
            )
            await _record_migrated_source_path(
                pname, src_path, scope_key=owner_id, owner_id=owner_id
            )
            migrated_projects_count += 1
            status_label = "merged_missing"
        elif action == "missing_source":
            # Record historical path so Desktop chats match if folder is created for history archive
            if import_chats and (item["unimported_conversations_count"] > 0 or item["already_imported_count"] > 0):
                dst_path.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(ensure_isolated_project_git_boundary, dst_path)
                await _record_migrated_source_path(
                    pname, src_path, scope_key=owner_id, owner_id=owner_id
                )
                status_label = "chats_only"
            else:
                status_label = "missing_source"
        else:
            status_label = "already_in_hub"

        chat_res = {"imported_chats_count": 0, "relinked_chats_count": 0, "imported_chat_ids": []}
        if import_chats and dst_path.exists():
            chat_res = await _import_and_relink_project_conversations(
                project_name=pname,
                dest_path=dst_path,
                source_path=src_path,
                conversations=item["conversations"],
                owner_id=owner_id,
                default_model=default_model,
                summaries_base_dir=summaries_base_dir,
            )

        venv_res = None
        if (
            rebuild_venv
            and status_label in ("copied", "moved", "merged_missing")
            and (dst_path / "requirements.txt").is_file()
        ):
            venv_res = await asyncio.to_thread(_rebuild_project_venv_sync, dst_path)

        proj_relinked_total = int(chat_res["relinked_chats_count"]) + int(db_relinked_direct)
        total_copied_files += copied_files
        total_imported_chats += chat_res["imported_chats_count"]
        total_relinked_chats += proj_relinked_total

        res_item: dict[str, Any] = {
            "project_name": pname,
            "action": action,
            "status": status_label,
            "moved": moved_flag,
            "source_origin": item.get("source_origin", "desktop"),
            "source_path": str(src_path),
            "dest_path": str(dst_path),
            "copied_files": copied_files,
            "skipped_existing_files": skipped_existing,
            "skipped_dirs": skipped_dirs,
            "git_copied": git_copied,
            "imported_chats_count": chat_res["imported_chats_count"],
            "relinked_chats_count": proj_relinked_total,
            "venv_rebuild": venv_res,
        }
        if warning_msg:
            res_item["warning"] = warning_msg
        results.append(res_item)

    # Clear cached summaries so project list immediately reflects updated Desktop chat counts
    brain_reader._summaries_cache.clear()

    return {
        "ok": True,
        "projects_root": plan["projects_root"],
        "migrated_projects_count": migrated_projects_count,
        "moved_legacy_siblings_count": sum(1 for r in results if r.get("moved")),
        "total_copied_files": total_copied_files,
        "total_imported_chats": total_imported_chats,
        "total_relinked_chats": total_relinked_chats,
        "results": results,
    }
