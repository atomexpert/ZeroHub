from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

IS_WINDOWS: bool = os.name == "nt"
PLATFORM_LABEL: str = "Windows" if IS_WINDOWS else "Linux"


def subprocess_hidden_kwargs() -> dict[str, Any]:
    """Return cross-platform kwargs to suppress console windows on Windows (under pythonw.exe / Windows Terminal)."""
    if not IS_WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        "startupinfo": si,
    }

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

VERSION_FILE = BASE_DIR / "VERSION"
DEFAULT_GITHUB_OWNER = "atomexpert"
DEFAULT_GITHUB_REPO = "ZeroHub"
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def get_app_version() -> str:
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "1.0.0"
    return version if VERSION_RE.fullmatch(version) else "1.0.0"


def _default_windows_agy_bin() -> str:
    local_appdata = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return str(local_appdata / "agy" / "bin" / "agy.exe")


def _write_first_run_file(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
        file.write(text)


def ensure_first_run_config(base_dir: Path) -> None:
    """Initialize a fresh installation without overwriting a user's configuration."""
    env_path = base_dir / ".env"
    if env_path.exists():
        return
    pin = os.getenv("HUB_INITIAL_PIN", "").strip()
    if len(pin) < 4 or any(c in pin for c in ("\n", "\r", "\x00", "$" "{")):
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        pin = "".join(secrets.choice(alphabet) for _ in range(8))
    default_secret = secrets.token_hex(32)
    default_agy = _default_windows_agy_bin() if IS_WINDOWS else str(Path.home() / ".local" / "bin" / "agy")
    default_appdata = str(Path.home() / ".gemini" / "antigravity")
    try:
        _write_first_run_file(
            env_path,
            "# ZeroHub Configuration\n"
            "HUB_HOST=0.0.0.0\n"
            "HUB_PORT=8765\n"
            "HUB_HTTPS_PORT=8766\n"
            f"HUB_PIN={json.dumps(pin, ensure_ascii=False)}\n"
            f"HUB_SECRET_KEY={default_secret}\n"
            f"AGY_BIN={json.dumps(default_agy, ensure_ascii=False)}\n"
            "HUB_PROXY=auto\n"
            "HUB_VOICE_MODEL=small\n"
            "HUB_VOICE_DEVICE=cpu\n"
            "HUB_DATA_DIR=./data\n"
            "USERS_DIR=./users\n"
            "PROJECTS_ROOT=./users/admin/projects\n"
            "SHARED_PROJECTS_DIR=./shared_projects\n"
            f"AGY_APP_DATA_DIR={json.dumps(default_appdata, ensure_ascii=False)}\n",
        )
    except FileExistsError:
        return
    credentials = base_dir / "data" / "FIRST_RUN_CREDENTIALS.txt"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        _write_first_run_file(credentials, f"ZeroHub first login\nUsername: admin\nPassword: {pin}\n")
    with contextlib.suppress(Exception):
        print(f"ZeroHub first login (admin). Credentials: {credentials}", file=sys.stderr, flush=True)


if "pytest" not in sys.modules:
    ensure_first_run_config(BASE_DIR)

load_dotenv(ENV_PATH, override=False)


def _snapshot_boot_env_proxy() -> str:
    """Capture external process proxy env vars once at boot before any runtime mutation."""
    for _var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        _val = os.environ.get(_var, "").strip()
        if _val:
            return _val if "://" in _val else f"http://{_val}"
    return ""


_BOOT_ENV_PROXY: str = _snapshot_boot_env_proxy()


def _same_fs_path(a: Path, b: Path) -> bool:
    sa = str(a).replace("\\", "/").rstrip("/")
    sb = str(b).replace("\\", "/").rstrip("/")
    if IS_WINDOWS:
        return sa.lower() == sb.lower()
    return sa == sb


def _is_explicit_abs_env_path(raw_str: str) -> bool:
    s = (raw_str or "").strip()
    if not s:
        return False
    return bool(re.match(r"^[a-zA-Z]:[\\/]", s)) or s.startswith("\\\\") or s.startswith("/")


def _update_env_key_in_place(env_path: Path, key: str, new_value: str) -> None:
    """Update a single `KEY=value` line in `.env` while preserving all other lines byte-for-byte."""
    if not env_path or not env_path.is_file():
        return
    if "pytest" in sys.modules and _same_fs_path(env_path, ENV_PATH):
        return
    try:
        raw_bytes = env_path.read_bytes()
    except OSError:
        return

    line_re = re.compile(rb"^(\s*(?:export\s+)?)" + re.escape(key.encode("ascii")) + rb"\s*=.*$", re.DOTALL)
    lines = raw_bytes.splitlines(keepends=True)
    changed = False
    out_lines: list[bytes] = []
    for line in lines:
        if not changed and line_re.match(line):
            ending = b""
            if line.endswith(b"\r\n"):
                ending = b"\r\n"
            elif line.endswith(b"\n"):
                ending = b"\n"
            elif line.endswith(b"\r"):
                ending = b"\r"
            m = re.match(rb"^(\s*(?:export\s+)?)", line)
            prefix = m.group(1) if m else b""
            new_line = prefix + f"{key}={new_value}".encode("utf-8") + ending
            if new_line != line:
                changed = True
            out_lines.append(new_line)
        else:
            out_lines.append(line)

    if not changed:
        return

    updated_bytes = b"".join(out_lines)
    try:
        tmp_env = env_path.parent / f".{env_path.name}.{os.getpid()}.tmp"
        tmp_env.write_bytes(updated_bytes)
        os.replace(tmp_env, env_path)
    except OSError:
        pass


LEGACY_EXTERNAL_PROJECTS_ROOT: Optional[Path] = None


def _resolve_portable_env_path(
    env_key: str,
    default_rel: Path,
    *,
    legacy_parent_migrate: bool = False,
    base_dir: Optional[Path] = None,
    env_path: Optional[Path] = None,
    environ: Optional[dict[str, str]] = None,
) -> Path:
    """Resolve portable Hub directory path relative to `BASE_DIR` and auto-heal legacy `.env` entries."""
    global LEGACY_EXTERNAL_PROJECTS_ROOT

    bdir = (base_dir or BASE_DIR).resolve()
    epath = env_path if env_path is not None else (bdir / ".env" if base_dir is not None else ENV_PATH)
    env_map = environ if environ is not None else os.environ

    raw = str(env_map.get(env_key, "") or "").strip()
    rel_norm = "./" + default_rel.as_posix().lstrip("./")
    target_default = (bdir / default_rel).resolve()

    if not raw:
        return target_default

    if not _is_explicit_abs_env_path(raw):
        return (bdir / Path(raw.replace("\\", "/"))).resolve()

    # Handle cross-OS drive paths cleanly (e.g. Windows E:\OldHub moved to Linux)
    win_drive_on_posix = (not IS_WINDOWS) and bool(re.match(r"^[a-zA-Z]:[\\/]", raw))
    cand_exists = False
    cand_resolved: Optional[Path] = None
    if not win_drive_on_posix:
        try:
            cand_p = Path(raw)
            cand_exists = cand_p.exists()
            cand_resolved = cand_p.resolve()
        except OSError:
            cand_exists = False
            cand_resolved = None

    # 1. Auto-heal legacy `PROJECTS_ROOT = BASE_DIR.parent`
    if legacy_parent_migrate and cand_resolved is not None:
        try:
            bdir_parent = bdir.parent.resolve()
        except OSError:
            bdir_parent = None
        if bdir_parent is not None and _same_fs_path(cand_resolved, bdir_parent):
            LEGACY_EXTERNAL_PROJECTS_ROOT = cand_resolved
            _update_env_key_in_place(epath, env_key, rel_norm)
            env_map[env_key] = rel_norm
            eff_users = USERS_DIR if (base_dir is None and "USERS_DIR" in globals()) else (bdir / "users")
            return (eff_users / "admin" / "projects").resolve()

    # 2. Auto-heal hardcoded absolute path pointing to `BASE_DIR / default_rel` -> portable `./...`
    if cand_resolved is not None and _same_fs_path(cand_resolved, target_default):
        _update_env_key_in_place(epath, env_key, rel_norm)
        env_map[env_key] = rel_norm
        return target_default

    # 3. Auto-heal non-existent `OldBase/...` path after moving `Antigravity_Hub` to another drive/folder
    raw_norm_tail = raw.replace("\\", "/").rstrip("/").lower()
    rel_suffix = "/" + default_rel.as_posix().lstrip("./").lower()
    has_local_rel = target_default.exists() or (
        len(default_rel.parts) > 1 and (bdir / default_rel.parts[0]).exists()
    )
    if not cand_exists and (has_local_rel or raw_norm_tail.endswith(rel_suffix)):
        _update_env_key_in_place(epath, env_key, rel_norm)
        env_map[env_key] = rel_norm
        return target_default

    return cand_resolved if cand_resolved is not None else target_default


def get_pin() -> str:
    # Re-read dynamically or from env so PIN updates take effect cleanly
    load_dotenv(ENV_PATH, override=True)
    return os.getenv("HUB_PIN", "1234").strip()


def get_public_url() -> str:
    """Return configured public HTTPS URL (e.g. https://hub.example.com) if set."""
    load_dotenv(ENV_PATH, override=True)
    return os.getenv("HUB_PUBLIC_URL", "").strip().rstrip("/")


HUB_HOST: str = os.getenv("HUB_HOST", "0.0.0.0").strip()
HUB_PORT: int = int(os.getenv("HUB_PORT", "8765").strip())
HUB_HTTPS_PORT: int = int(os.getenv("HUB_HTTPS_PORT", "8766").strip() or "0")

DATA_DIR: Path = _resolve_portable_env_path("HUB_DATA_DIR", Path("data"))
USERS_DIR: Path = _resolve_portable_env_path("USERS_DIR", Path("users"))
SHARED_PROJECTS_DIR: Path = _resolve_portable_env_path("SHARED_PROJECTS_DIR", Path("shared_projects"))
SHARED_UPLOADS_DIR: Path = SHARED_PROJECTS_DIR / "_uploads"
SHARED_CONTEXT_DIR: Path = SHARED_PROJECTS_DIR / "_context"
PROJECTS_ROOT: Path = _resolve_portable_env_path(
    "PROJECTS_ROOT",
    Path("users/admin/projects"),
    legacy_parent_migrate=True,
)
USER_WORKSPACES_DIR: Path = USERS_DIR

LEGACY_DATA_SHARED_PROJECTS_DIR: Path = DATA_DIR / "shared_projects"
LEGACY_DATA_WORKSPACES_DIR: Path = DATA_DIR / "workspaces"
LEGACY_DATA_UPLOADS_DIR: Path = DATA_DIR / "uploads"
LEGACY_DATA_CHAT_SANDBOX_DIR: Path = DATA_DIR / "chat_sandbox"

DB_PATH = DATA_DIR / "hub.db"
SESSIONS_DIR = DATA_DIR / "sessions"
CHAT_SANDBOX_DIR = LEGACY_DATA_CHAT_SANDBOX_DIR
CLI_HOMES_DIR = DATA_DIR / "cli_homes"
UPLOADS_DIR = LEGACY_DATA_UPLOADS_DIR
SANDBOX_UPLOADS_DIR = DATA_DIR / "sandbox_uploads"
WHISPER_MODELS_DIR = DATA_DIR / "models" / "whisper"
GIGAAM_MODELS_DIR = DATA_DIR / "models" / "gigaam"
CERTS_DIR = DATA_DIR / "certs"
CODEX_TOOLS_DIR = DATA_DIR / "tools" / "codex"
CLAUDE_TOOLS_DIR = DATA_DIR / "tools" / "claude"
XRAY_TOOLS_DIR = DATA_DIR / "tools" / "xray"

_BOOT_BASE_DIR = BASE_DIR
_BOOT_DATA_DIR = DATA_DIR
_BOOT_DB_PATH = DB_PATH
_BOOT_USERS_DIR = USERS_DIR
_BOOT_USER_WORKSPACES_DIR = USER_WORKSPACES_DIR
_BOOT_SHARED_PROJECTS_DIR = SHARED_PROJECTS_DIR
_BOOT_PROJECTS_ROOT = PROJECTS_ROOT
_BOOT_XRAY_TOOLS_DIR = XRAY_TOOLS_DIR

MAX_CUSTOM_RULES_BYTES: int = 24_000
MAX_OPENROUTER_RULES_BYTES: int = 12_000
DOCKER_SANDBOX_IMAGE: str = os.getenv("HUB_DOCKER_IMAGE", "agy-hub-sandbox:latest").strip() or "agy-hub-sandbox:latest"

VOICE_MODEL_DEFAULT: str = os.getenv("HUB_VOICE_MODEL", "small").strip() or "small"
VOICE_DEVICE: str = os.getenv("HUB_VOICE_DEVICE", "cpu").strip() or "cpu"
VOICE_CPU_THREADS: int = max(2, min(12, int(os.getenv("HUB_VOICE_THREADS", "8").strip() or "8")))
VOICE_PRIMARY_ENGINE: str = (os.getenv("HUB_VOICE_ENGINE", "gigaam").strip().lower() or "gigaam")
GROQ_API_KEY: str = os.getenv("HUB_GROQ_API_KEY", os.getenv("GROQ_API_KEY", "")).strip()

AGY_APP_DATA_DIR = Path(
    os.getenv("AGY_APP_DATA_DIR", str(Path.home() / ".gemini" / "antigravity"))
).resolve()
AGY_CLI_DATA_DIR = Path(
    os.getenv("AGY_CLI_DATA_DIR", str(Path.home() / ".gemini" / "antigravity-cli"))
).resolve()

# Ensure persistent directories exist (skip creating live container dirs when imported under pytest)
if "pytest" not in sys.modules:
    for _d in (
        DATA_DIR,
        SESSIONS_DIR,
        CLI_HOMES_DIR,
        USERS_DIR,
        PROJECTS_ROOT,
        SHARED_PROJECTS_DIR,
        SHARED_UPLOADS_DIR,
        SHARED_CONTEXT_DIR,
        UPLOADS_DIR,
        SANDBOX_UPLOADS_DIR,
        WHISPER_MODELS_DIR,
        GIGAAM_MODELS_DIR,
        CERTS_DIR,
        CODEX_TOOLS_DIR,
        CLAUDE_TOOLS_DIR,
    ):
        _d.mkdir(parents=True, exist_ok=True)
    if "unittest" not in sys.modules:
        XRAY_TOOLS_DIR.mkdir(parents=True, exist_ok=True)


_WIN_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

RESERVED_PROJECT_NAMES_LOWER: set[str] = {
    "antigravity_hub",
    BASE_DIR.name.lower(),
    "users",
    "shared_projects",
    "data",
    "app",
    "tools",
    "tests",
    "_uploads",
    "_context",
    ".venv",
    ".git",
}


def get_reserved_project_names_lower() -> set[str]:
    return set(RESERVED_PROJECT_NAMES_LOWER) | {BASE_DIR.name.lower()}


def get_effective_users_dir() -> Path:
    """Return active `users/` root, honoring monkeypatches to either `USERS_DIR` or legacy `USER_WORKSPACES_DIR`."""
    if USERS_DIR != _BOOT_USERS_DIR:
        return USERS_DIR.resolve()
    if USER_WORKSPACES_DIR != _BOOT_USER_WORKSPACES_DIR:
        return USER_WORKSPACES_DIR.resolve()
    return USERS_DIR.resolve()


def get_effective_legacy_uploads_dir() -> Path:
    """Return legacy `data/uploads/` fallback directory, honoring test overrides."""
    boot_uploads = _BOOT_DATA_DIR / "uploads"
    if UPLOADS_DIR != boot_uploads:
        return UPLOADS_DIR.resolve()
    return (DATA_DIR / "uploads").resolve()


def get_hub_core_protected_paths() -> tuple[Path, ...]:
    """Return core Hub server directories forbidden as shared projects or unauthorized extra_dirs."""
    bdir = BASE_DIR.resolve()
    ddir = DATA_DIR.resolve()
    return (
        (bdir / "app").resolve(),
        ddir,
        (bdir / "tools").resolve(),
        (bdir / "tests").resolve(),
        (bdir / ".venv").resolve(),
        (bdir / ".git").resolve(),
    )


def _lookup_username_by_id_readonly(user_id: str) -> str:
    uid = str(user_id or "").strip()
    if not uid or uid in {"admin", "_system"}:
        return "admin" if uid == "admin" else ""
    eff_db = DB_PATH if DB_PATH != _BOOT_DB_PATH else (DATA_DIR / "hub.db")
    if eff_db == _BOOT_DB_PATH and os.environ.get("PYTEST_CURRENT_TEST"):
        return ""
    if not eff_db.is_file():
        return ""
    try:
        import sqlite3
        import urllib.parse

        uri = f"file:{urllib.parse.quote(eff_db.resolve().as_posix(), safe='/:')}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.5) as conn:
            cur = conn.execute("SELECT username FROM users WHERE id = ? LIMIT 1", (uid,))
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0]).strip()
    except Exception:
        pass
    return ""


def sanitize_user_folder_slug(user_or_username: Any) -> str:
    """Return canonical lowercase folder slug inside `users/<slug>/` (`admin`, `garik`, `leha`, ...)."""
    if isinstance(user_or_username, dict):
        uid = str(user_or_username.get("id") or "").strip()
        uname = str(user_or_username.get("username") or "").strip()
        role = str(user_or_username.get("role") or "").strip().lower()
        if uid.lower() == "admin" or uname.lower() == "admin" or (role == "admin" and not uname):
            return "admin"
        raw = uname or _lookup_username_by_id_readonly(uid) or uid or "guest"
    else:
        raw = str(user_or_username or "").strip()
        if raw.lower() == "admin":
            return "admin"
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F\-]{20,}", raw):
            looked_up = _lookup_username_by_id_readonly(raw)
            if looked_up:
                raw = looked_up

    slug = re.sub(r"[^a-z0-9_.\-]+", "_", raw.lower()).strip("._-")
    if not slug or slug in {".", ".."}:
        slug = "guest"
    if slug.upper().split(".")[0] in _WIN_RESERVED_NAMES:
        slug = f"u_{slug}"
    return slug[:64]


def ensure_user_space_dirs(user_or_username: Any) -> dict[str, Path]:
    """Idempotently create and return canonical per-user folder hierarchy under `users/<slug>/`."""
    slug = sanitize_user_folder_slug(user_or_username)
    users_root = get_effective_users_dir()
    users_root.mkdir(parents=True, exist_ok=True)
    u_root = (users_root / slug).resolve()
    try:
        u_root.relative_to(users_root)
    except ValueError as exc:
        raise ValueError("Недопустимый путь пользовательского каталога") from exc

    space = {
        "root": u_root,
        "projects": (u_root / "projects").resolve(),
        "chats": (u_root / "chats").resolve(),
        "uploads": (u_root / "uploads").resolve(),
        "context": (u_root / "context").resolve(),
    }
    for p in space.values():
        p.mkdir(parents=True, exist_ok=True)
    return space


def sanitize_chat_folder_id(chat_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "", str(chat_id or "").strip())[:80]
    return safe or "default"


def resolve_user_chat_sandbox_dir(
    user_or_username: Any,
    chat_id: str,
    *,
    create: bool = True,
) -> Path:
    """Return per-user free-chat working directory `users/<slug>/chats/<safe_chat_id>`."""
    slug = sanitize_user_folder_slug(user_or_username)
    chats_root = (get_effective_users_dir() / slug / "chats").resolve()
    safe_cid = sanitize_chat_folder_id(chat_id)
    chat_dir = (chats_root / safe_cid).resolve()
    try:
        chat_dir.relative_to(chats_root)
    except ValueError as exc:
        raise ValueError("Недопустимый путь рабочей папки чата") from exc
    if create:
        chat_dir.mkdir(parents=True, exist_ok=True)
    return chat_dir


def resolve_chat_uploads_dir(
    chat: Optional[dict[str, Any]],
    owner_username: str = "",
    is_shared_project: bool = False,
) -> Path:
    """Resolve canonical upload storage folder for a chat:
    - shared project chat -> `shared_projects/_uploads/<safe_chat_id>`
    - personal/free chat  -> `users/<owner_slug>/uploads/<safe_chat_id>`
    """
    chat_dict = chat or {}
    safe_cid = sanitize_chat_folder_id(str(chat_dict.get("id") or ""))
    if is_shared_project:
        base_up = (SHARED_PROJECTS_DIR.resolve() / "_uploads").resolve()
    else:
        owner_ref = (
            owner_username
            or str(chat_dict.get("owner_username") or "").strip()
            or str(chat_dict.get("user_id") or "").strip()
            or "admin"
        )
        slug = sanitize_user_folder_slug(owner_ref)
        base_up = (get_effective_users_dir() / slug / "uploads").resolve()
    target = (base_up / safe_cid).resolve()
    try:
        target.relative_to(base_up)
    except ValueError as exc:
        raise ValueError("Недопустимый путь вложений чата") from exc
    return target


def resolve_chat_upload_dirs_for_turn(
    chat: Optional[dict[str, Any]],
    *,
    chat_id: str = "",
    owner_username: str = "",
    is_shared_project: bool = False,
) -> list[Path]:
    """Return existing upload directories for a chat (new location first, legacy `data/uploads/<id>` fallback second)."""
    eff_chat = dict(chat or {})
    if chat_id and not eff_chat.get("id"):
        eff_chat["id"] = chat_id
    safe_cid = sanitize_chat_folder_id(str(eff_chat.get("id") or chat_id))
    primary = resolve_chat_uploads_dir(
        eff_chat,
        owner_username=owner_username,
        is_shared_project=is_shared_project,
    )
    legacy = (get_effective_legacy_uploads_dir() / safe_cid).resolve()
    dirs: list[Path] = []
    owner_ref = (owner_username or str(eff_chat.get("owner_username") or "").strip()
                 or str(eff_chat.get("user_id") or "").strip() or "admin")
    alternate = ((get_effective_users_dir() / sanitize_user_folder_slug(owner_ref) / "uploads" / safe_cid).resolve()
                 if is_shared_project else (SHARED_PROJECTS_DIR / "_uploads" / safe_cid).resolve())
    for cand in (primary, legacy, alternate):
        try:
            if cand.exists() and cand.is_dir() and cand not in dirs:
                dirs.append(cand)
        except OSError:
            continue
    return dirs


def find_existing_chat_upload_file(
    chat_id: str,
    filename: str,
    *,
    chat: Optional[dict[str, Any]] = None,
    owner_username: str = "",
    is_shared_project: bool = False,
) -> Optional[Path]:
    """Locate an uploaded file in new `users/.../uploads/<id>` or `shared_projects/_uploads/<id>`,
    falling back transparently to legacy `data/uploads/<id>`."""
    safe_name = Path(filename or "").name
    if not safe_name or safe_name != filename or ".." in safe_name:
        return None
    eff_chat = dict(chat or {})
    if chat_id and not eff_chat.get("id"):
        eff_chat["id"] = chat_id
    safe_cid = sanitize_chat_folder_id(str(eff_chat.get("id") or chat_id))
    primary_dir = resolve_chat_uploads_dir(
        eff_chat,
        owner_username=owner_username,
        is_shared_project=is_shared_project,
    )
    legacy_dir = (get_effective_legacy_uploads_dir() / safe_cid).resolve()

    candidates: list[tuple[Path, Path]] = [
        (primary_dir, (primary_dir / safe_name).resolve()),
        (legacy_dir, (legacy_dir / safe_name).resolve()),
    ]
    # Also handle case where chat became shared or personal after upload
    if is_shared_project:
        owner_ref = (
            owner_username
            or str(eff_chat.get("owner_username") or "").strip()
            or str(eff_chat.get("user_id") or "").strip()
            or "admin"
        )
        alt_user_dir = (get_effective_users_dir() / sanitize_user_folder_slug(owner_ref) / "uploads" / safe_cid).resolve()
        candidates.append((alt_user_dir, (alt_user_dir / safe_name).resolve()))
    else:
        alt_shared_dir = (SHARED_PROJECTS_DIR.resolve() / "_uploads" / safe_cid).resolve()
        candidates.append((alt_shared_dir, (alt_shared_dir / safe_name).resolve()))

    for root_dir, file_p in candidates:
        try:
            file_p.relative_to(root_dir)
            if file_p.exists() and file_p.is_file():
                return file_p
        except (ValueError, OSError):
            continue
    return None


def sanitize_cli_home_id(user_or_id: object) -> str:
    """Return a filesystem-safe directory identifier inside data/cli_homes/."""
    if isinstance(user_or_id, dict):
        raw = str(user_or_id.get("id") or user_or_id.get("username") or "guest").strip()
    else:
        raw = str(user_or_id or "_system").strip()
    safe = re.sub(r"[^a-zA-Z0-9_.\-]+", "_", raw).strip(".-")
    if not safe or safe in {".", ".."}:
        safe = "guest"
    if safe.upper().split(".")[0] in _WIN_RESERVED_NAMES:
        safe = f"u_{safe}"
    return safe[:80]


def _is_junction_or_symlink(p: Path) -> bool:
    if hasattr(os.path, "isjunction") and os.path.isjunction(p):
        return True
    return p.is_symlink()


def _unlink_reparse_point(p: Path) -> None:
    """Safely remove an NTFS junction or symlink WITHOUT touching its target directory."""
    if hasattr(os.path, "isjunction") and os.path.isjunction(p):
        os.rmdir(p)
        return
    if p.is_symlink():
        if IS_WINDOWS and p.is_dir():
            try:
                os.rmdir(p)
                return
            except OSError:
                pass
        p.unlink(missing_ok=True)


def _agy_account_id(account_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", account_id or ""):
        raise ValueError("Некорректный ID Google-аккаунта")
    return account_id


def _assert_agy_local_path(path: Path) -> None:
    root = CLI_HOMES_DIR.absolute()
    path.absolute().relative_to(root)
    current = path.absolute()
    while current != root:
        if _is_junction_or_symlink(current):
            raise ValueError("Неожиданная reparse-точка в пути профиля")
        current = current.parent
    if _is_junction_or_symlink(root):
        raise ValueError("Корень CLI HOME не может быть ссылкой")


def _agy_link(link: Path, target: Path) -> None:
    if _is_junction_or_symlink(link):
        if link.resolve() == target.resolve():
            return
        raise ValueError("Неожиданный маршрут ссылки профиля")
    if link.exists():
        raise ValueError("Путь ссылки профиля уже занят")
    link.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target.resolve())], capture_output=True, timeout=5, **subprocess_hidden_kwargs())
        if result.returncode:
            raise OSError("Не удалось создать junction профиля")
    else:
        link.symlink_to(target.resolve(), target_is_directory=True)


def ensure_agy_account_cli_data_dir(account_id: str) -> Path:
    root = CLI_HOMES_DIR / '_agy_accounts' / _agy_account_id(account_id)
    _assert_agy_local_path(root)
    if _is_junction_or_symlink(root):
        raise ValueError("Каталог аккаунта не может быть ссылкой")
    dest = root / 'antigravity-cli'
    if _is_junction_or_symlink(dest):
        raise ValueError("Каталог данных не может быть ссылкой")
    (dest / 'conversations').mkdir(parents=True, exist_ok=True)
    brain_dir = dest / 'brain'
    if _is_junction_or_symlink(brain_dir):
        _unlink_reparse_point(brain_dir)
    brain_dir.mkdir(parents=True, exist_ok=True)
    settings = AGY_CLI_DATA_DIR / 'settings.json'
    if settings.is_file() and not (dest / settings.name).exists():
        shutil.copy2(settings, dest / settings.name)
    for name in ('builtin', 'mcp'):
        target = AGY_CLI_DATA_DIR / name
        if target.is_dir():
            _agy_link(dest / name, target)
    return dest


def ensure_user_agy_account_home(user_or_id: object, account_id: str) -> Path:
    base = ensure_user_cli_home(user_or_id)
    home = base / '.agy_homes' / _agy_account_id(account_id)
    _assert_agy_local_path(home)
    if _is_junction_or_symlink(home) or _is_junction_or_symlink(home / '.gemini'):
        raise ValueError("Недопустимая ссылка HOME аккаунта")
    gemini = home / '.gemini'
    gemini.mkdir(parents=True, exist_ok=True)
    (base / '.gemini' / 'config').mkdir(parents=True, exist_ok=True)
    _agy_link(gemini / 'config', base / '.gemini' / 'config')
    _agy_link(gemini / 'antigravity-cli', ensure_agy_account_cli_data_dir(account_id))
    rules = base / '.gemini' / 'GEMINI.md'
    if rules.is_file():
        _atomic_sync_text_file(gemini / 'GEMINI.md', rules.read_text(encoding='utf-8'))
    return home


def _safe_remove_agy_tree(root: Path) -> None:
    # Walk without following links; unlink every reparse point before recursive removal.
    root.absolute().relative_to(CLI_HOMES_DIR.absolute())
    _assert_agy_local_path(root.parent)
    if _is_junction_or_symlink(root):
        _unlink_reparse_point(root)
        return
    if not root.exists():
        return
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs) + list(files):
            child = Path(parent) / name
            if _is_junction_or_symlink(child):
                _unlink_reparse_point(child)
                if name in dirs:
                    dirs.remove(name)
    for parent, dirs, files in os.walk(root, followlinks=False):
        if any(_is_junction_or_symlink(Path(parent)/name) for name in dirs + files):
            raise ValueError("Осталась reparse-точка в удаляемом профиле")
    shutil.rmtree(root)


def cleanup_agy_account_dirs(account_id: str) -> None:
    aid = _agy_account_id(account_id)
    if not CLI_HOMES_DIR.exists():
        return
    for user_home in CLI_HOMES_DIR.iterdir():
        if user_home.name != '_agy_accounts' and user_home.is_dir() and not _is_junction_or_symlink(user_home):
            _safe_remove_agy_tree(user_home / '.agy_homes' / aid)
    _safe_remove_agy_tree(CLI_HOMES_DIR / '_agy_accounts' / aid)


def ensure_user_cli_home(user_or_id: object) -> Path:
    """
    Prepare an isolated CLI home directory `data/cli_homes/<safe_user_id>/` on D:\\
    with `.gemini/antigravity-cli` linked (NTFS Directory Junction on Windows,
    symlink on Linux) to `AGY_CLI_DATA_DIR`, excluding desktop `config/` and `GEMINI.md`.
    """
    safe_id = sanitize_cli_home_id(user_or_id)
    homes_root = CLI_HOMES_DIR.resolve()
    homes_root.mkdir(parents=True, exist_ok=True)
    home_dir = (homes_root / safe_id).resolve()
    try:
        home_dir.relative_to(homes_root)
    except ValueError:
        raise ValueError("Недопустимый путь изолированного профиля CLI")

    gemini_dir = home_dir / ".gemini"
    gemini_dir.mkdir(parents=True, exist_ok=True)

    target_cli = AGY_CLI_DATA_DIR.resolve()
    target_cli.mkdir(parents=True, exist_ok=True)
    link_path = gemini_dir / "antigravity-cli"

    need_create = True
    if _is_junction_or_symlink(link_path):
        try:
            if link_path.exists() and link_path.resolve() == target_cli:
                need_create = False
            else:
                _unlink_reparse_point(link_path)
        except OSError:
            try:
                _unlink_reparse_point(link_path)
            except OSError:
                pass
    elif link_path.exists():
        if link_path.is_file():
            link_path.unlink(missing_ok=True)
        elif link_path.is_dir() and not any(link_path.iterdir()):
            link_path.rmdir()
        else:
            need_create = False

    if need_create:
        if IS_WINDOWS:
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(target_cli)],
                capture_output=True,
                timeout=5.0,
                **subprocess_hidden_kwargs(),
            )
            if proc.returncode != 0 and not _is_junction_or_symlink(link_path):
                os.symlink(str(target_cli), str(link_path), target_is_directory=True)
        else:
            os.symlink(str(target_cli), str(link_path), target_is_directory=True)

    return home_dir


def ensure_user_codex_home(user_or_id: object) -> Path:
    """Prepare isolated base Codex directory `data/cli_homes/<safe_user_id>/.codex/`."""
    home_dir = ensure_user_cli_home(user_or_id)
    codex_dir = (home_dir / ".codex").resolve()
    try:
        codex_dir.relative_to(home_dir.resolve())
    except ValueError:
        raise ValueError("Недопустимый путь профиля Codex CLI")
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "accounts").mkdir(parents=True, exist_ok=True)
    return codex_dir


def ensure_user_codex_account_dir(user_or_id: object, account_id: str) -> Path:
    """Prepare isolated `CODEX_HOME` for a specific personal ChatGPT account:
    `data/cli_homes/<safe_user_id>/.codex/accounts/<safe_account_id>/`."""
    codex_dir = ensure_user_codex_home(user_or_id)
    accounts_root = (codex_dir / "accounts").resolve()
    accounts_root.mkdir(parents=True, exist_ok=True)
    safe_acc = re.sub(r"[^a-zA-Z0-9_.\-]+", "_", str(account_id or "").strip()).strip(".-")
    if not safe_acc or safe_acc in {".", ".."}:
        raise ValueError("Некорректный идентификатор аккаунта ChatGPT")
    acc_dir = (accounts_root / safe_acc[:80]).resolve()
    try:
        acc_dir.relative_to(accounts_root)
    except ValueError:
        raise ValueError("Недопустимый путь каталога аккаунта ChatGPT")
    acc_dir.mkdir(parents=True, exist_ok=True)
    try:
        sync_user_cli_skills(user_or_id)
    except Exception:
        pass
    return acc_dir


def ensure_user_claude_home(user_or_id: object) -> Path:
    """Prepare isolated base Claude Code directory `data/cli_homes/<safe_user_id>/.claude/`."""
    home_dir = ensure_user_cli_home(user_or_id)
    claude_dir = (home_dir / ".claude").resolve()
    try:
        claude_dir.relative_to(home_dir.resolve())
    except ValueError:
        raise ValueError("Недопустимый путь профиля Claude Code CLI")
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "accounts").mkdir(parents=True, exist_ok=True)
    return claude_dir


def ensure_user_claude_account_dir(user_or_id: object, account_id: str) -> Path:
    """Prepare isolated `CLAUDE_CONFIG_DIR` for a personal Claude account:
    `data/cli_homes/<safe_user_id>/.claude/accounts/<safe_account_id>/`."""
    claude_dir = ensure_user_claude_home(user_or_id)
    accounts_root = (claude_dir / "accounts").resolve()
    accounts_root.mkdir(parents=True, exist_ok=True)
    safe_acc = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(account_id or "").strip()).strip(".-")
    if not safe_acc or safe_acc in {".", ".."}:
        raise ValueError("Некорректный идентификатор аккаунта Claude")
    acc_dir = (accounts_root / safe_acc[:80]).resolve()
    try:
        acc_dir.relative_to(accounts_root)
    except ValueError:
        raise ValueError("Недопустимый путь каталога аккаунта Claude")
    acc_dir.mkdir(parents=True, exist_ok=True)
    try:
        sync_user_cli_skills(user_or_id)
    except Exception:
        pass
    return acc_dir


def ensure_user_agents_skills_dir(user_or_id: object) -> Path:
    """Prepare `$HOME/.agents/skills/` inside user's isolated CLI home so Codex CLI
    and other standard agents auto-discover personal MCP skills."""
    home_dir = ensure_user_cli_home(user_or_id)
    skills_dir = (home_dir / ".agents" / "skills").resolve()
    try:
        skills_dir.relative_to(home_dir.resolve())
    except ValueError:
        raise ValueError("Недопустимый путь каталога навыков .agents/skills")
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


def _atomic_sync_text_file(target_file: Path, desired_text: str) -> None:
    target_file.parent.mkdir(parents=True, exist_ok=True)
    if not desired_text:
        target_file.unlink(missing_ok=True)
        return
    try:
        if target_file.exists() and target_file.read_text(encoding="utf-8") == desired_text:
            return
    except OSError:
        pass
    tmp_file = target_file.parent / f".{target_file.name}.{os.getpid()}.tmp"
    tmp_file.write_text(desired_text, encoding="utf-8")
    os.replace(tmp_file, target_file)


def _sync_skill_folder_contents(src_dir: Path, dst_dir: Path) -> None:
    """Mirror files from a user skill folder into `dst_dir` without touching symlinks/junctions."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        name = item.name
        if name.startswith(".") or name == "__pycache__" or name.endswith(".tmp"):
            continue
        if _is_junction_or_symlink(item):
            continue
        dst_item = dst_dir / name
        if item.is_dir():
            _sync_skill_folder_contents(item, dst_item)
        elif item.is_file():
            try:
                raw = item.read_bytes()
                if dst_item.is_file() and dst_item.read_bytes() == raw:
                    continue
                dst_item.parent.mkdir(parents=True, exist_ok=True)
                tmp_item = dst_item.parent / f".{name}.{os.getpid()}.tmp"
                tmp_item.write_bytes(raw)
                os.replace(tmp_item, dst_item)
            except OSError:
                continue


def sync_user_cli_skills(user_or_id: Any) -> None:
    """Mirror user skill folders from `<cli_home>/.gemini/config/skills/*` and `<cli_home>/.agents/skills/*`
    into Codex (`.codex/skills/*`, `.codex/accounts/*/skills/*`) and Claude (`.claude/skills/*`, `.claude/accounts/*/skills/*`),
    skipping built-in system skills (`builtin`)."""
    home_dir = ensure_user_cli_home(user_or_id)
    builtin_names: set[str] = {"builtin"}
    builtin_root = AGY_CLI_DATA_DIR / "builtin" / "skills"
    if builtin_root.is_dir():
        try:
            for bchild in builtin_root.iterdir():
                if bchild.is_dir():
                    builtin_names.add(bchild.name.lower())
        except OSError:
            pass

    discovered: dict[str, Path] = {}
    for src_root in (home_dir / ".agents" / "skills", home_dir / ".gemini" / "config" / "skills"):
        if not src_root.is_dir() or _is_junction_or_symlink(src_root):
            continue
        try:
            for child in src_root.iterdir():
                if not child.is_dir() or _is_junction_or_symlink(child):
                    continue
                sname = child.name.strip()
                if (
                    not sname
                    or sname.startswith(".")
                    or sname.startswith("_")
                    or sname.lower() in builtin_names
                ):
                    continue
                if (child / "SKILL.md").is_file() or any(child.iterdir()):
                    discovered[sname] = child
        except OSError:
            continue

    if not discovered:
        return

    target_roots: list[Path] = [
        home_dir / ".codex" / "skills",
        home_dir / ".claude" / "skills",
    ]
    for acc_parent in (home_dir / ".codex" / "accounts", home_dir / ".claude" / "accounts"):
        if acc_parent.is_dir():
            try:
                for acc_child in acc_parent.iterdir():
                    if (
                        acc_child.is_dir()
                        and not acc_child.name.startswith(".")
                        and not acc_child.name.startswith("_pending_")
                        and not _is_junction_or_symlink(acc_child)
                    ):
                        target_roots.append(acc_child / "skills")
            except OSError:
                pass

    for dst_root in target_roots:
        for sname, src_folder in discovered.items():
            try:
                _sync_skill_folder_contents(src_folder, dst_root / sname)
            except OSError:
                continue


def sync_user_cli_rules(user_or_id: object, custom_rules: str, *, username: str = "") -> Path:
    """Sync user's custom AI rules simultaneously into:
    - `data/cli_homes/<safe_user_id>/.gemini/GEMINI.md` (Google Antigravity CLI)
    - `data/cli_homes/<safe_user_id>/.codex/AGENTS.md` (OpenAI Codex CLI base)
    - `data/cli_homes/<safe_user_id>/.codex/accounts/*/AGENTS.md` (existing ChatGPT accounts)
    - `users/<user_slug>/context/RULES.md` (human-readable context mirror)
    """
    home_dir = ensure_user_cli_home(user_or_id)
    gemini_dir = home_dir / ".gemini"
    rules_file = gemini_dir / "GEMINI.md"
    clean = (custom_rules or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    desired = (clean + "\n") if clean else ""

    _atomic_sync_text_file(rules_file, desired)
    for account_rules in (home_dir / '.agy_homes').glob('*/.gemini/GEMINI.md'):
        _atomic_sync_text_file(account_rules, desired)

    codex_dir = home_dir / ".codex"
    if clean or codex_dir.exists():
        codex_dir.mkdir(parents=True, exist_ok=True)
        _atomic_sync_text_file(codex_dir / "AGENTS.md", desired)
        accounts_dir = codex_dir / "accounts"
        if accounts_dir.is_dir():
            for child in accounts_dir.iterdir():
                if child.is_dir():
                    try:
                        _atomic_sync_text_file(child / "AGENTS.md", desired)
                    except OSError:
                        pass

    claude_dir = home_dir / ".claude"
    if clean or claude_dir.exists():
        claude_dir.mkdir(parents=True, exist_ok=True)
        _atomic_sync_text_file(claude_dir / "CLAUDE.md", desired)
        c_accounts = claude_dir / "accounts"
        if c_accounts.is_dir():
            for child in c_accounts.iterdir():
                if child.is_dir():
                    try:
                        _atomic_sync_text_file(child / "CLAUDE.md", desired)
                    except OSError:
                        pass

    # Human-readable mirror in `users/<user_slug>/context/RULES.md` (skip internal `_system`)
    safe_cli_id = sanitize_cli_home_id(user_or_id)
    if safe_cli_id != "_system":
        try:
            slug_source: Any = username if username else user_or_id
            space = ensure_user_space_dirs(slug_source)
            mirror_file = space["context"] / "RULES.md"
            mirror_content = desired if clean else "# Глобальные правила ИИ\n\n_(Пользовательские правила не заданы.)_\n"
            _atomic_sync_text_file(mirror_file, mirror_content)
        except Exception:
            pass

    try:
        sync_user_cli_skills(user_or_id)
    except Exception:
        pass

    return rules_file


DEFAULT_PROJECT_GUARDRAILS: str = (
    "[Базовый инженерный стандарт проекта (защита от регрессий):]\n"
    "- **Сохранность существующей логики:** перед переименованием, удалением или изменением сигнатуры "
    "функции, класса, маршрута API, схемы БД или DOM `id` проверь все обращения через `grep_search`, "
    "чтобы не оставлять оборванных импортов и вызовов. Не удаляй рабочие ветки кода без задачи пользователя.\n"
    "- **Самопроверка до завершения хода:** после правок проверь синтаксис изменённых файлов "
    "(`py_compile` для `.py`, `node --check` для `.js`, `cargo check`, `tsc --noEmit`) и запусти "
    "проектный скрипт (`tools/verify_hub.py`, `tools/verify_project.py`, `pytest` / `npm test`), если доступен.\n"
    "- **Тесты и изоляция данных:** при фиксе бага или новой ключевой логике добавляй изолированный "
    "регрессионный тест (`tmp_path` / `tempfile`); запрещено менять в тестах живые `.env`, боевые "
    "`*.db` / `*.sqlite`, `*.session` и останавливать живые фоновые сервисы без поручения.\n"
    "- **Секреты и структура данных:** храни пароли, токены, API-ключи и прокси в корневом `.env` с "
    "комментариями над ключами; свободно создавай и дополняй `.env` и `.env.example` по задаче, но в "
    "существующий `.env` дописывай новые переменные бережно, никогда не перезатирая и не обнуляя "
    "заполненные пользователем ключи/пароли, и не выводи значения боевых секретов из `.env` открытым "
    "текстом в чат без прямого запроса; файловые ключи храни в `.secrets/`, а БД, кэши, логи, сборки "
    "и выгрузки — в `data/`, `output/`, `dist/`, `target/`.\n"
    "- **Чистота кода и Git:** избегай дублей и `!important`, сохраняй UTF-8 без BOM и окончания строк; "
    "коммить только целевые пути явно (без `git add .` / `-A`, секретов, сессий и БД)."
)

DEFAULT_PROJECT_GUARDRAILS_DISABLE_MARKERS: tuple[str, ...] = (
    "hub:disable-default-guardrails",
    "disable_default_guardrails: true",
    "default_guardrails: off",
)


def has_explicit_project_guardrails_file(proj_dir: Optional[Path]) -> bool:
    """Return True if `<proj_dir>/.agents/rules/engineering_guardrails.md` exists and is non-empty."""
    if proj_dir is None:
        return False
    try:
        rules_dir = Path(proj_dir) / ".agents" / "rules"
        if not rules_dir.is_dir():
            return False
        for item in rules_dir.iterdir():
            if item.is_file() and item.name.lower() == "engineering_guardrails.md":
                if item.read_text(encoding="utf-8", errors="replace").strip():
                    return True
    except OSError:
        return False
    return False


def get_default_project_guardrails_block(
    proj_dir: Optional[Path] = None,
    *,
    disable_default_guardrails: bool = False,
    project_memory: Optional[dict[str, Any]] = None,
    existing_rules_text: str = "",
) -> str:
    """Return `DEFAULT_PROJECT_GUARDRAILS` unless explicitly disabled or already covered by `engineering_guardrails.md`."""
    if disable_default_guardrails:
        return ""
    if isinstance(project_memory, dict):
        if bool(project_memory.get("disable_default_guardrails")):
            return ""
        if project_memory.get("default_guardrails_enabled") is False:
            return ""
        mem_rules = str(project_memory.get("passport_rules") or "").lower()
        if any(marker in mem_rules for marker in DEFAULT_PROJECT_GUARDRAILS_DISABLE_MARKERS):
            return ""
    if existing_rules_text:
        low_existing = existing_rules_text.lower()
        if any(marker in low_existing for marker in DEFAULT_PROJECT_GUARDRAILS_DISABLE_MARKERS):
            return ""
    if proj_dir is not None:
        if has_explicit_project_guardrails_file(proj_dir):
            return ""
        if not existing_rules_text:
            try:
                passport_md = Path(proj_dir) / ".agents" / "rules" / "hub_project.md"
                if passport_md.is_file():
                    low_md = passport_md.read_text(encoding="utf-8", errors="replace").lower()
                    if any(marker in low_md for marker in DEFAULT_PROJECT_GUARDRAILS_DISABLE_MARKERS):
                        return ""
            except OSError:
                pass
    return DEFAULT_PROJECT_GUARDRAILS


def read_project_hub_rules(
    proj_dir: Optional[Path],
    *,
    access_mode: str = "write_files",
    disable_default_guardrails: bool = False,
    project_memory: Optional[dict[str, Any]] = None,
) -> str:
    """Read `<project_dir>/.agents/rules/hub_project.md` (if present) plus every other `*.md`
    from the same folder in deterministic alphabetical order, and append `DEFAULT_PROJECT_GUARDRAILS`
    for active edit/terminal project turns when `engineering_guardrails.md` is not already present.
    The combined text keeps the safe 26000-char cap."""
    if proj_dir is None:
        return ""
    try:
        p_dir = Path(proj_dir)
        if not p_dir.is_dir():
            return ""
    except Exception:
        return ""

    rules_dir = p_dir / ".agents" / "rules"
    files: list[Path] = []
    try:
        if rules_dir.is_dir():
            primary = rules_dir / "hub_project.md"
            if primary.is_file():
                files.append(primary)
            files += sorted(
                (p for p in rules_dir.glob("*.md") if p.is_file() and p.name.lower() != "hub_project.md"),
                key=lambda p: (p.name.lower(), p.name),
            )
    except Exception:
        files = []

    parts: list[str] = []
    has_explicit_guardrails = False
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if text:
            if path.name.lower() == "engineering_guardrails.md":
                has_explicit_guardrails = True
            parts.append(text)

    if not has_explicit_guardrails and access_mode in ("write_files", "full_access"):
        default_block = get_default_project_guardrails_block(
            p_dir,
            disable_default_guardrails=disable_default_guardrails,
            project_memory=project_memory,
            existing_rules_text="\n\n".join(parts),
        )
        if default_block:
            parts.append(default_block)

    return "\n\n".join(parts)[:26000]


def cleanup_user_cli_home(user_or_id: object) -> None:
    """Safely remove a deleted user's isolated CLI home without following the Junction."""
    safe_id = sanitize_cli_home_id(user_or_id)
    if safe_id in {"admin", "_system"}:
        return
    homes_root = CLI_HOMES_DIR.resolve()
    home_dir = (homes_root / safe_id).resolve()
    try:
        home_dir.relative_to(homes_root)
    except ValueError:
        return
    if not home_dir.exists():
        return

    gemini_dir = home_dir / ".gemini"
    link_path = gemini_dir / "antigravity-cli"
    try:
        if _is_junction_or_symlink(link_path):
            _unlink_reparse_point(link_path)
    except OSError:
        return

    # Hard safety check: never rmtree if the Junction reparse point still exists
    if _is_junction_or_symlink(link_path) or link_path.exists():
        return

    _safe_remove_agy_tree(home_dir)


def apply_isolated_home_to_env(env: dict[str, str], cli_home: Path) -> dict[str, str]:
    """
    Point USERPROFILE, HOME, HOMEDRIVE, and HOMEPATH to the isolated `cli_home`
    while preserving real system LOCALAPPDATA, APPDATA, SystemRoot, TEMP, TMP, and PATH.
    """
    if IS_WINDOWS:
        orig_profile = env.get("USERPROFILE") or str(Path.home())
        if not env.get("LOCALAPPDATA"):
            env["LOCALAPPDATA"] = str(Path(orig_profile) / "AppData" / "Local")
        if not env.get("APPDATA"):
            env["APPDATA"] = str(Path(orig_profile) / "AppData" / "Roaming")

    home_str = str(cli_home.resolve())
    env["USERPROFILE"] = home_str
    env["HOME"] = home_str
    if IS_WINDOWS:
        drive, tail = os.path.splitdrive(home_str)
        if drive:
            env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = tail or "\\"
    # Strict system invariant: 0% GPU / 0 MB VRAM for all CLI subprocesses
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"
    return env

SECRET_KEY_FILE = DATA_DIR / ".secret_key"
_env_secret = os.getenv("HUB_SECRET_KEY", "").strip()
if _env_secret:
    HUB_SECRET_KEY = _env_secret
elif SECRET_KEY_FILE.exists():
    HUB_SECRET_KEY = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
else:
    HUB_SECRET_KEY = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(HUB_SECRET_KEY, encoding="utf-8")


def get_or_create_hub_secret() -> str:
    """Return the persistent Hub master secret used for per-user PBKDF2-Fernet derivation."""
    env_sec = os.getenv("HUB_SECRET_KEY", "").strip()
    if env_sec:
        return env_sec
    if SECRET_KEY_FILE.exists():
        try:
            val = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
            if val:
                return val
        except OSError:
            pass
    if HUB_SECRET_KEY:
        return HUB_SECRET_KEY
    generated = secrets.token_hex(32)
    try:
        SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_KEY_FILE.write_text(generated, encoding="utf-8")
    except OSError:
        pass
    return generated


def resolve_agy_bin() -> str:
    """Locate the official Google Antigravity CLI executable (Windows + Linux)."""
    configured = os.getenv("AGY_BIN", "").strip()
    if configured and Path(configured).exists():
        return str(Path(configured))

    if IS_WINDOWS:
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            candidate = Path(local_app_data) / "agy" / "bin" / "agy.exe"
            if candidate.exists():
                return str(candidate)
        which_agy = shutil.which("agy") or shutil.which("agy.exe")
        if which_agy:
            return which_agy
        return _default_windows_agy_bin()

    # Linux / POSIX lookup
    which_agy = shutil.which("agy")
    if which_agy:
        return which_agy
    for posix_candidate in (
        Path.home() / ".local" / "bin" / "agy",
        Path("/usr/local/bin/agy"),
        Path("/usr/bin/agy"),
    ):
        if posix_candidate.exists():
            return str(posix_candidate)
    return "agy"


_CODEX_VERSION_CACHE: dict[str, tuple[float, str]] = {}


def _resolve_native_codex_from_node_modules(
    nm_dir: Path,
) -> Optional[tuple[Path, Optional[Path], str]]:
    """Locate the direct native Rust `codex` binary inside an npm `node_modules` tree.
    Returns `(binary_path, extra_path_dir, pkg_version)` or `None`."""
    if not nm_dir.is_dir():
        return None

    pkg_version = ""
    main_pkg = nm_dir / "@openai" / "codex" / "package.json"
    if main_pkg.is_file():
        try:
            pkg_data = json.loads(main_pkg.read_text(encoding="utf-8"))
            pkg_version = str(pkg_data.get("version") or "").strip()
        except Exception:
            pkg_version = ""

    arch = platform.machine().lower()
    is_arm = arch in ("arm64", "aarch64")
    exe_name = "codex.exe" if IS_WINDOWS else "codex"

    if IS_WINDOWS:
        pkg_triples = (
            [
                ("@openai/codex-win32-arm64", "aarch64-pc-windows-msvc"),
                ("@openai/codex-win32-x64", "x86_64-pc-windows-msvc"),
            ]
            if is_arm
            else [
                ("@openai/codex-win32-x64", "x86_64-pc-windows-msvc"),
                ("@openai/codex-win32-arm64", "aarch64-pc-windows-msvc"),
            ]
        )
    else:
        sys_name = platform.system().lower()
        if sys_name == "darwin":
            pkg_triples = (
                [
                    ("@openai/codex-darwin-arm64", "aarch64-apple-darwin"),
                    ("@openai/codex-darwin-x64", "x86_64-apple-darwin"),
                ]
                if is_arm
                else [
                    ("@openai/codex-darwin-x64", "x86_64-apple-darwin"),
                    ("@openai/codex-darwin-arm64", "aarch64-apple-darwin"),
                ]
            )
        else:
            pkg_triples = (
                [
                    ("@openai/codex-linux-arm64", "aarch64-unknown-linux-musl"),
                    ("@openai/codex-linux-x64", "x86_64-unknown-linux-musl"),
                ]
                if is_arm
                else [
                    ("@openai/codex-linux-x64", "x86_64-unknown-linux-musl"),
                    ("@openai/codex-linux-arm64", "aarch64-unknown-linux-musl"),
                ]
            )

    candidate_roots: list[tuple[Path, str]] = []
    for sub_pkg, triple in pkg_triples:
        candidate_roots.append((nm_dir / sub_pkg, triple))
        candidate_roots.append((nm_dir / "@openai" / "codex" / "node_modules" / sub_pkg, triple))
        candidate_roots.append((nm_dir / "@openai" / "codex", triple))

    for root_dir, triple in candidate_roots:
        vendor_triple = root_dir / "vendor" / triple
        bin_cand = vendor_triple / "bin" / exe_name
        if bin_cand.is_file():
            extra_path = vendor_triple / "codex-path"
            return (
                bin_cand.resolve(),
                extra_path.resolve() if extra_path.is_dir() else None,
                pkg_version,
            )

    return None


def _unwrap_npm_shim_to_native_codex(shim_path: Path) -> Optional[tuple[Path, Optional[Path], str]]:
    """Given an npm shim (`.../node_modules/.bin/codex.cmd` or `%APPDATA%/npm/codex.cmd`),
    try to unwrap it to the direct native `codex.exe` binary."""
    parent = shim_path.parent
    nm_candidates = [
        parent / "node_modules",
        parent.parent if parent.name == ".bin" else parent / "lib" / "node_modules",
    ]
    for nm in nm_candidates:
        found = _resolve_native_codex_from_node_modules(nm)
        if found is not None:
            return found
    return None


def _resolve_codex_details() -> dict[str, Any]:
    """Internal resolver returning path, source, native flag, extra PATH dir, and package version."""
    # 1. Explicit env override
    configured = os.environ.get("HUB_CODEX_BIN", "").strip()
    if configured:
        cfg_path = Path(configured)
        if cfg_path.exists() and cfg_path.is_file():
            if cfg_path.suffix.lower() in (".cmd", ".ps1", ""):
                unwrapped = _unwrap_npm_shim_to_native_codex(cfg_path)
                if unwrapped is not None:
                    b_path, extra_dir, pkg_ver = unwrapped
                    return {
                        "installed": True,
                        "bin_path": str(b_path),
                        "source": "env",
                        "native_binary": True,
                        "extra_path_dir": str(extra_dir) if extra_dir else "",
                        "version": pkg_ver,
                    }
            return {
                "installed": True,
                "bin_path": str(cfg_path.resolve()),
                "source": "env",
                "native_binary": cfg_path.suffix.lower() not in (".cmd", ".bat"),
                "extra_path_dir": "",
                "version": "",
            }
        which_cfg = shutil.which(configured)
        if which_cfg:
            return {
                "installed": True,
                "bin_path": str(Path(which_cfg).resolve()),
                "source": "env",
                "native_binary": Path(which_cfg).suffix.lower() not in (".cmd", ".bat"),
                "extra_path_dir": "",
                "version": "",
            }

    # 2. Local Hub-managed installation (`data/tools/codex`)
    local_nm = CODEX_TOOLS_DIR / "node_modules"
    local_native = _resolve_native_codex_from_node_modules(local_nm)
    if local_native is not None:
        b_path, extra_dir, pkg_ver = local_native
        return {
            "installed": True,
            "bin_path": str(b_path),
            "source": "hub_tools",
            "native_binary": True,
            "extra_path_dir": str(extra_dir) if extra_dir else "",
            "version": pkg_ver,
        }
    local_shim = local_nm / ".bin" / ("codex.cmd" if IS_WINDOWS else "codex")
    if local_shim.is_file():
        return {
            "installed": True,
            "bin_path": str(local_shim.resolve()),
            "source": "hub_tools",
            "native_binary": not IS_WINDOWS,
            "extra_path_dir": "",
            "version": "",
        }

    # 3. Global npm (`%APPDATA%\npm\codex.cmd`), Cargo (`~/.cargo/bin/codex`), and system PATH
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            npm_dir = Path(appdata) / "npm"
            npm_native = _resolve_native_codex_from_node_modules(npm_dir / "node_modules")
            if npm_native is not None:
                b_path, extra_dir, pkg_ver = npm_native
                return {
                    "installed": True,
                    "bin_path": str(b_path),
                    "source": "npm_global",
                    "native_binary": True,
                    "extra_path_dir": str(extra_dir) if extra_dir else "",
                    "version": pkg_ver,
                }
            npm_cmd = npm_dir / "codex.cmd"
            if npm_cmd.is_file():
                return {
                    "installed": True,
                    "bin_path": str(npm_cmd.resolve()),
                    "source": "npm_global",
                    "native_binary": False,
                    "extra_path_dir": "",
                    "version": "",
                }

    real_home = Path.home()
    cargo_bin = real_home / ".cargo" / "bin" / ("codex.exe" if IS_WINDOWS else "codex")
    if cargo_bin.is_file():
        return {
            "installed": True,
            "bin_path": str(cargo_bin.resolve()),
            "source": "cargo",
            "native_binary": True,
            "extra_path_dir": "",
            "version": "",
        }

    which_codex = (
        shutil.which("codex.exe")
        or shutil.which("codex")
        or (shutil.which("codex.cmd") if IS_WINDOWS else None)
    )
    if which_codex:
        w_path = Path(which_codex)
        unwrapped = _unwrap_npm_shim_to_native_codex(w_path)
        if unwrapped is not None:
            b_path, extra_dir, pkg_ver = unwrapped
            return {
                "installed": True,
                "bin_path": str(b_path),
                "source": "path",
                "native_binary": True,
                "extra_path_dir": str(extra_dir) if extra_dir else "",
                "version": pkg_ver,
            }
        return {
            "installed": True,
            "bin_path": str(w_path.resolve()),
            "source": "path",
            "native_binary": w_path.suffix.lower() not in (".cmd", ".bat"),
            "extra_path_dir": "",
            "version": "",
        }

    return {
        "installed": False,
        "bin_path": "",
        "source": "missing",
        "native_binary": False,
        "extra_path_dir": "",
        "version": "",
    }


def resolve_codex_bin() -> Optional[str]:
    """Locate the official OpenAI Codex CLI executable, preferring the direct native binary."""
    details = _resolve_codex_details()
    if details.get("installed") and details.get("bin_path"):
        return str(details["bin_path"])
    return None


def get_codex_cli_info(*, probe_version: bool = False) -> dict[str, Any]:
    """Return status metadata about the installed OpenAI Codex CLI binary."""
    details = _resolve_codex_details()
    if not details.get("installed"):
        return details

    bin_path = str(details.get("bin_path") or "")
    ver = str(details.get("version") or "").strip()
    if not ver and bin_path:
        cached = _CODEX_VERSION_CACHE.get(bin_path)
        now_m = time.monotonic()
        if cached and (now_m - cached[0]) < 600.0:
            ver = cached[1]
        elif probe_version:
            try:
                cmd = (
                    ["cmd.exe", "/d", "/s", "/c", bin_path, "--version"]
                    if IS_WINDOWS and bin_path.lower().endswith((".cmd", ".bat"))
                    else [bin_path, "--version"]
                )
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=4.0,
                    **subprocess_hidden_kwargs(),
                )
                raw_out = (proc.stdout or proc.stderr or "").strip().splitlines()
                if raw_out:
                    ver = raw_out[0].strip()[:80]
                    _CODEX_VERSION_CACHE[bin_path] = (now_m, ver)
            except Exception:
                pass
    details["version"] = ver
    return details


_CLAUDE_INFO_CACHE: dict[str, Any] = {}
_CLAUDE_INFO_TTL_SEC: float = 30.0


def _find_node_bin() -> str:
    found = shutil.which("node.exe") or shutil.which("node")
    if found:
        return found
    if IS_WINDOWS:
        cand = Path(r"C:\Program Files\nodejs\node.exe")
        if cand.exists():
            return str(cand)
    return ""


def _find_npm_bin() -> str:
    found = shutil.which("npm.cmd") or shutil.which("npm")
    if found:
        return found
    if IS_WINDOWS:
        cand = Path(r"C:\Program Files\nodejs\npm.cmd")
        if cand.exists():
            return str(cand)
    return ""


def _unwrap_npm_shim_to_claude_js(shim_path: Path) -> str:
    """Given `.../node_modules/.bin/claude.cmd` or `%APPDATA%/npm/claude.cmd`, return `cli.js` if present."""
    if not _find_node_bin():
        return ""
    parent = shim_path.parent
    nm_candidates = [
        parent / "node_modules",
        parent.parent if parent.name == ".bin" else parent / "lib" / "node_modules",
    ]
    for nm in nm_candidates:
        cand = nm / "@anthropic-ai" / "claude-code" / "cli.js"
        try:
            if cand.is_file():
                return str(cand.resolve())
        except OSError:
            continue
    return ""


def _read_claude_pkg_version(cli_js_path: str) -> str:
    if not cli_js_path:
        return ""
    try:
        pkg = Path(cli_js_path).parent / "package.json"
        if pkg.is_file():
            data = json.loads(pkg.read_text(encoding="utf-8"))
            return str(data.get("version") or "").strip()[:80]
    except Exception:
        pass
    return ""


def _resolve_claude_details() -> tuple[str, str, str]:
    """Return `(path, cli_js, source)` for the Claude Code CLI ('' if not found).
    `cli_js` is set when the CLI should be run as `node <cli.js>` (no cmd.exe shim)."""
    def _finalize_candidate(cand_path: Path, source: str) -> tuple[str, str, str]:
        low_name = cand_path.name.lower()
        if low_name.endswith((".js", ".mjs", ".cjs")):
            s = str(cand_path)
            return s, s, source
        if low_name.endswith((".cmd", ".bat")):
            js_unwrapped = _unwrap_npm_shim_to_claude_js(cand_path)
            if js_unwrapped:
                return js_unwrapped, js_unwrapped, source
        return str(cand_path), "", source

    configured = os.environ.get("HUB_CLAUDE_BIN", "").strip()
    if configured:
        cfg = Path(configured)
        if cfg.is_file():
            return _finalize_candidate(cfg, "env")
        w = shutil.which(configured)
        if w:
            return _finalize_candidate(Path(w), "env")

    local_nm = CLAUDE_TOOLS_DIR / "node_modules"
    local_js = local_nm / "@anthropic-ai" / "claude-code" / "cli.js"
    if local_js.is_file() and _find_node_bin():
        return str(local_js), str(local_js), "local"
    local_shim = local_nm / ".bin" / ("claude.cmd" if IS_WINDOWS else "claude")
    if local_shim.is_file():
        return _finalize_candidate(local_shim, "local")

    real_home = Path(os.environ.get("HUB_REAL_HOME") or Path.home())
    cands: list[tuple[Path, str]] = []
    if IS_WINDOWS:
        cands.append((real_home / ".local" / "bin" / "claude.exe", "user"))
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            npm_js = Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
            if npm_js.is_file() and _find_node_bin():
                return str(npm_js), str(npm_js), "global_npm"
            cands.append((Path(appdata) / "npm" / "claude.cmd", "global_npm"))
    else:
        cands.append((real_home / ".local" / "bin" / "claude", "user"))
    for c, c_src in cands:
        try:
            if c.is_file():
                return _finalize_candidate(c, c_src)
        except OSError:
            continue

    w = shutil.which("claude") or (shutil.which("claude.cmd") if IS_WINDOWS else None)
    if w:
        return _finalize_candidate(Path(w), "system")
    return "", "", ""


def resolve_claude_bin() -> str:
    """Locate the Claude Code CLI entry (path to `cli.js`, native exe, `.cmd` shim or `.py` mock)."""
    return _resolve_claude_details()[0]


def build_claude_exec_argv(bin_path: str, *args: str) -> list[str]:
    """Build argv for Claude Code CLI: `node cli.js`, Python test mocks, `.cmd` shims or native exe."""
    low = (bin_path or "").lower()
    if low.endswith(".py"):
        return [sys.executable, bin_path, *args]
    if low.endswith((".js", ".mjs", ".cjs")):
        return [_find_node_bin() or "node", bin_path, *args]
    if IS_WINDOWS and low.endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/s", "/c", bin_path, *args]
    return [bin_path, *args]


def get_claude_cli_info(force_refresh: bool = False) -> dict[str, Any]:
    """Return Claude Code CLI status (cached for 30 s)."""
    now_m = time.monotonic()
    env_sig = os.environ.get("HUB_CLAUDE_BIN", "").strip()
    cached = _CLAUDE_INFO_CACHE.get("info")
    if (
        not force_refresh
        and isinstance(cached, dict)
        and _CLAUDE_INFO_CACHE.get("env_sig") == env_sig
        and (now_m - float(_CLAUDE_INFO_CACHE.get("ts") or 0.0)) < _CLAUDE_INFO_TTL_SEC
    ):
        return dict(cached)
    path, cli_js, source = _resolve_claude_details()
    npm_ok = bool(_find_npm_bin())
    pkg_ver = _read_claude_pkg_version(cli_js)
    info: dict[str, Any] = {
        "installed": bool(path),
        "path": path,
        "cli_js": cli_js,
        "node_bin": _find_node_bin(),
        "source": source,
        "version": pkg_ver,
        "npm_available": npm_ok,
    }
    if path and not info["version"]:
        try:
            env = apply_isolated_home_to_env(os.environ.copy(), ensure_user_cli_home("_system"))
            env["CLAUDE_CONFIG_DIR"] = str(ensure_user_claude_home("_system"))
            env["DISABLE_AUTOUPDATER"] = "1"
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
            env["DISABLE_TELEMETRY"] = "1"
            env["BROWSER"] = "echo"
            proc = subprocess.run(
                build_claude_exec_argv(path, "--version"),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5.0,
                env=env,
                stdin=subprocess.DEVNULL,
                **subprocess_hidden_kwargs(),
            )
            lines = (proc.stdout or proc.stderr or "").strip().splitlines()
            if lines:
                m = re.search(r"\d+\.\d+\.\d+[\w.\-]*", lines[0])
                info["version"] = (m.group(0) if m else lines[0].strip())[:80]
        except Exception:
            pass
    _CLAUDE_INFO_CACHE["info"] = dict(info)
    _CLAUDE_INFO_CACHE["env_sig"] = env_sig
    _CLAUDE_INFO_CACHE["ts"] = now_m
    return info


def resolve_docker_bin() -> Optional[str]:
    """Locate Docker CLI executable across Windows (Docker Desktop) and Linux (APT/CE/Snap),
    even when Docker was installed after the Hub server process started (stale PATH)."""
    configured = os.getenv("HUB_DOCKER_BIN", "").strip()
    if configured:
        cfg_path = Path(configured)
        if cfg_path.exists() and cfg_path.is_file():
            return str(cfg_path)
        which_cfg = shutil.which(configured)
        if which_cfg:
            return which_cfg

    which_docker = shutil.which("docker") or (shutil.which("docker.exe") if IS_WINDOWS else None)
    if which_docker:
        return which_docker

    if IS_WINDOWS:
        win_candidates: list[Path] = []
        for env_key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            prog_dir = os.environ.get(env_key, "").strip()
            if prog_dir:
                win_candidates.append(Path(prog_dir) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe")
        win_candidates.append(Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"))
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            win_candidates.append(
                Path(local_app) / "Programs" / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
            )
        for cand in win_candidates:
            try:
                if cand.exists() and cand.is_file():
                    return str(cand)
            except OSError:
                continue
        return None

    for posix_cand in (
        Path("/usr/bin/docker"),
        Path("/usr/local/bin/docker"),
        Path("/snap/bin/docker"),
    ):
        try:
            if posix_cand.exists() and posix_cand.is_file():
                return str(posix_cand)
        except OSError:
            continue
    return None


def detect_system_proxy() -> Optional[str]:
    """Pure detector for external boot env proxy or Windows Internet Settings (`ProxyEnable` / `ProxyServer`)."""
    if _BOOT_ENV_PROXY:
        return _BOOT_ENV_PROXY

    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if int(enabled) == 1:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    server = str(server).strip()
                    if server:
                        if "=" in server:
                            parts: dict[str, str] = {}
                            for item in server.split(";"):
                                if "=" in item:
                                    k, v = item.split("=", 1)
                                    parts[k.strip().lower()] = v.strip()
                            if parts.get("https"):
                                chosen = parts["https"]
                                return chosen if "://" in chosen else f"http://{chosen}"
                            if parts.get("http"):
                                chosen = parts["http"]
                                return chosen if "://" in chosen else f"http://{chosen}"
                            if parts.get("socks") or parts.get("socks5"):
                                chosen = parts.get("socks5") or parts.get("socks") or ""
                                if chosen:
                                    return chosen if "://" in chosen else f"socks5://{chosen}"
                            chosen = next(iter(parts.values()), "")
                        else:
                            chosen = server
                        if chosen:
                            return chosen if "://" in chosen else f"http://{chosen}"
        except Exception:
            pass
    return None


def _legacy_resolve_outbound_proxy() -> Optional[str]:
    """Startup fallback before `proxy_router` initializes from `hub_meta`."""
    mode = os.getenv("HUB_PROXY", "auto").strip()
    if not mode or mode.lower() in ("none", "off", "false", "direct"):
        return None
    if mode.lower() != "auto":
        if "://" not in mode:
            return f"http://{mode}"
        return mode
    return detect_system_proxy()


def resolve_outbound_proxy() -> Optional[str]:
    """Compatible wrapper over `proxy_router.resolve_global_proxy_sync()` with boot fallback."""
    router_mod = sys.modules.get("app.proxy_router")
    if router_mod is not None and getattr(router_mod, "is_initialized", lambda: False)():
        try:
            return router_mod.resolve_global_proxy_sync()
        except Exception:
            return None
    return _legacy_resolve_outbound_proxy()


_TAILSCALE_CLI_CACHE_TTL_S: float = 300.0
_tailscale_cli_cache: Optional[tuple[float, list[str]]] = None


def _tailscale_cli_ips_cached() -> list[str]:
    """Return IPv4 addresses from `tailscale ip -4`, cached in memory for 300 s."""
    global _tailscale_cli_cache
    now_m = time.monotonic()
    if _tailscale_cli_cache is not None and (now_m - _tailscale_cli_cache[0]) < _TAILSCALE_CLI_CACHE_TTL_S:
        return list(_tailscale_cli_cache[1])

    ips: list[str] = []
    ts_fallback = (
        r"C:\Program Files\Tailscale\tailscale.exe"
        if IS_WINDOWS
        else "/usr/bin/tailscale"
    )
    ts_cli = shutil.which("tailscale") or ts_fallback
    if ts_cli and Path(ts_cli).exists():
        try:
            out = subprocess.check_output(
                [ts_cli, "ip", "-4"],
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=1.5,
                text=True,
                **subprocess_hidden_kwargs(),
            ).strip()
            for line in out.splitlines():
                ip_str = line.strip()
                if ip_str:
                    ips.append(ip_str)
        except Exception:
            pass
    _tailscale_cli_cache = (now_m, ips)
    return list(ips)


def get_network_endpoints(
    port: Optional[int] = None,
    https_port: Optional[int] = None,
) -> dict:
    """Detect Localhost, Home LAN Wi-Fi, and Tailscale P2P URLs (HTTP + HTTPS)."""
    p = port or HUB_PORT
    hp = HUB_HTTPS_PORT if https_port is None else https_port
    tailscale_ips: list[str] = []
    lan_ips: list[str] = []

    ts_net = ipaddress.ip_network("100.64.0.0/10")

    # Gather all IPv4 addresses on host
    seen: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip_str = info[4][0]
            if ip_str and ip_str not in seen and not ip_str.startswith("127."):
                seen.add(ip_str)
    except Exception:
        pass

    # Fallback UDP probe for primary LAN route
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            primary_ip = s.getsockname()[0]
            if primary_ip and primary_ip not in seen and not primary_ip.startswith("127."):
                seen.add(primary_ip)
    except Exception:
        pass

    # Also check Tailscale CLI if installed — but only when sockets did not already reveal a
    # 100.64.0.0/10 address, and cache the CLI result for 5 minutes (fewer console spawns).
    def _in_ts_net(ip_s: str) -> bool:
        try:
            return ipaddress.ip_address(ip_s) in ts_net
        except ValueError:
            return False

    if not any(_in_ts_net(ip_s) for ip_s in seen):
        seen.update(_tailscale_cli_ips_cached())

    for ip_str in sorted(seen):
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if ip_obj in ts_net:
                tailscale_ips.append(ip_str)
            elif ip_obj.is_private and not ip_str.startswith("169.254."):
                lan_ips.append(ip_str)
        except ValueError:
            continue

    tailscale_urls = [f"http://{ip}:{p}" for ip in tailscale_ips]
    lan_urls = [f"http://{ip}:{p}" for ip in lan_ips]
    primary_mobile_url = (
        tailscale_urls[0]
        if tailscale_urls
        else (lan_urls[0] if lan_urls else f"http://127.0.0.1:{p}")
    )

    https_enabled = hp > 0
    https_tailscale_urls = [f"https://{ip}:{hp}" for ip in tailscale_ips] if https_enabled else []
    https_lan_urls = [f"https://{ip}:{hp}" for ip in lan_ips] if https_enabled else []
    primary_mobile_https_url = ""
    if https_enabled:
        primary_mobile_https_url = (
            https_tailscale_urls[0]
            if https_tailscale_urls
            else (https_lan_urls[0] if https_lan_urls else f"https://127.0.0.1:{hp}")
        )

    public_url = get_public_url()

    return {
        "port": p,
        "https_port": hp,
        "https_enabled": https_enabled,
        "public_url": public_url,
        "localhost_url": f"http://127.0.0.1:{p}",
        "tailscale_ips": tailscale_ips,
        "lan_ips": lan_ips,
        "tailscale_urls": tailscale_urls,
        "lan_urls": lan_urls,
        "primary_mobile_url": public_url or primary_mobile_url,
        "https_tailscale_urls": https_tailscale_urls,
        "https_lan_urls": https_lan_urls,
        "primary_mobile_https_url": public_url or primary_mobile_https_url,
        "tailscale_active": len(tailscale_ips) > 0,
    }
