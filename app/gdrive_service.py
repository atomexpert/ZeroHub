"""Unidirectional Google Drive cloud mirror & encrypted secrets vault service (v151).

Features:
- Proxy-aware stdlib HTTP transport via `proxy_router.build_urllib_proxy_opener(config.resolve_outbound_proxy())`
  with clean injectable transport for offline unit/integration tests.
- Google OAuth 2.0 (PKCE S256, `access_type=offline`, `prompt=consent`, narrow `drive.file openid email` scope),
  supporting both local browser callback and manual redirect URL / code exchange.
- Four-layer project filter (`.gitignore` + runtime/output dirs + secret interception + binary/size/Anti-Dump Guard)
  with strict symlink/reparse-point blocking (`workspace_mcp._is_reparse_or_symlink`).
- Unidirectional mirror layout (`Диалоги/`, `Проекты/<P>/_HUB/`, `Проекты/<P>/code/`, `_restore_vault/`),
  sanitized SQLite hot backup (`sqlite3.Connection.backup()` + `VACUUM` + `gzip`),
  user-passphrase-encrypted `secrets.vault.enc` (`AGY_VAULT_V1`, PBKDF2-HMAC-SHA256 600k + Fernet + zlib),
  incremental `SHA-256` delta sync, and safe Trash (`trashed=true`, never permanent `DELETE`).
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import gzip
import hashlib
import html
import inspect
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import brain_reader, config, database, proxy_router, workspace_mcp

# ---------------------------------------------------------------------------
# Constants & Errors
# ---------------------------------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
GOOGLE_DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file openid email"
GOOGLE_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

VAULT_MAGIC = "AGY_VAULT_V1"
VAULT_KDF = "pbkdf2-sha256"
VAULT_KDF_ITERATIONS = 600_000
VAULT_PAYLOAD_FORMAT = "AGY_VAULT_PAYLOAD_V1"
SYNC_MANIFEST_FORMAT = "AGY_SYNC_MANIFEST_V1"

MAX_CODE_FILE_BYTES = 1_048_576  # 1 MiB
MAX_UI_ASSET_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB
MAX_UI_SOUND_FILE_BYTES = 512 * 1024  # 512 KiB
MAX_VAULT_SECRET_FILE_BYTES = 512 * 1024  # 512 KiB
ANTI_DUMP_MIN_DATA_FILES = 80  # Strictly > 80 data files and 0 code files

SYNC_UPLOAD_CONCURRENCY = 6
SYNC_PROGRESS_THROTTLE_SEC = 0.85
GDRIVE_RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
GDRIVE_RETRY_BACKOFFS_SEC: tuple[float, ...] = (0.6, 1.5)

ALLOWED_BACKUP_TABLES: frozenset[str] = frozenset(
    {
        "chats",
        "messages",
        "chat_folders",
        "project_memories",
        "shared_projects",
        "model_advisor_catalog",
    }
)

# Layer 2: blocked runtime, output, build, cache and browser-profile directories
BLOCKED_PROJECT_DIRS_EXACT: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".cargo",
        ".gradle",
        ".next",
        ".nuxt",
        "target",
        "dist",
        "build",
        "out",
        "prebuilt",
        "data",
        "storage",
        "output",
        "outputs",
        "results",
        "exports",
        "downloads",
        "parsed",
        "dumps",
        "reports",
        "logs",
        "tmp",
        "temp",
        "scratch",
        "scraped",
        "recordings",
        "captures",
        "cache",
        "backups",
        "backup",
        ".backups",
        ".audit-run",
        ".audit-tmp",
        ".test-deps",
        "staged",
    }
)

BLOCKED_PROJECT_DIR_SUBSTRINGS: tuple[str, ...] = (
    "playwright",
    "selenium",
    "chrome_profile",
    "browser_profile",
)

SELF_HUB_BLOCKED_TOP_DIRS: frozenset[str] = frozenset(
    {
        "users",
        "shared_projects",
    }
)

INTERNAL_AUTO_EXCLUDED_CODE_PATHS: frozenset[str] = frozenset(
    {
        ".agents/rules/hub_project.md",
    }
)

# Layer 3: secret interception (never uploaded to plaintext `code/`)
SECRET_DIRS_EXACT: frozenset[str] = frozenset(
    {
        ".secrets",
        "private-profiles",
    }
)

SECRET_ALLOWED_EXACT_NAMES: frozenset[str] = frozenset(
    {
        ".env.example",
    }
)

SECRET_FILE_PATTERNS: tuple[str, ...] = (
    "*secret*.json",
    "*credentials*.json",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.ovpn",
    "*.rsc",
    "*.session*",
)

VAULT_EXCLUDED_FILE_PATTERNS: tuple[str, ...] = (
    "*.session*",
)

# Layer 4: binaries, archives, media, and Anti-Dump Guard extensions
BLOCKED_BINARY_PATTERNS: tuple[str, ...] = (
    "*.db*",
    "*.sqlite*",
    "*.log",
    "*.xlsx",
    "*.xls",
    "*.parquet",
    "*.feather",
    "*.pkl",
    "*.onnx",
    "*.pt",
    "*.pth",
    "*.safetensors",
    "*.bin",
    "*.exe",
    "*.dll",
    "*.so",
    "*.dylib",
    "*.apk",
    "*.aab",
    "*.ipa",
    "*.iso",
    "*.zip",
    "*.7z",
    "*.rar",
    "*.tar*",
    "*.gz",
    "*.zst",
    "*.mp4",
    "*.mkv",
    "*.avi",
    "*.mov",
    "*.mp3",
    "*.wav",
    "*.ogg",
    "*.m4a",
    "*.flac",
    "*.tmp",
    "*.part",
    "*.patch",
)

PROJECT_UI_ASSET_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".svg",
        ".gif",
        ".ico",
        ".avif",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
    }
)

PROJECT_UI_SOUND_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".ogg",
        ".wav",
        ".m4a",
    }
)

PROJECT_UI_SOUND_DIR_SEGMENTS: frozenset[str] = frozenset(
    {
        "static",
        "public",
        "assets",
        "sounds",
        "sfx",
        "audio",
        "res",
        "resources",
    }
)

PROJECT_UI_KEEP_DIR_SEGMENTS: frozenset[str] = frozenset(
    {
        "static",
        "public",
        "assets",
        "icons",
        "images",
        "img",
        "fonts",
        "styles",
        "ui",
        "web",
        "frontend",
        "client",
        "res",
        "resources",
    }
)

ANTI_DUMP_DATA_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".json",
        ".html",
        ".htm",
        ".txt",
        ".csv",
        ".xml",
    }
)

ANTI_DUMP_RASTER_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
    }
)

ANTI_DUMP_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".scss",
        ".vue",
        ".svelte",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".cs",
        ".cpp",
        ".c",
        ".h",
        ".sql",
        ".sh",
        ".ps1",
        ".bat",
    }
)

_WIN_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class GDriveServiceError(ValueError):
    """Structured error raised by `gdrive_service` with an HTTP-friendly status code."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "gdrive_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class GDriveAuthError(GDriveServiceError):
    """Raised when Google OAuth credentials or tokens are missing, revoked, or rejected."""

    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message, status_code=status_code, code="gdrive_auth_error")


class GDriveVaultError(GDriveServiceError):
    """Raised when encrypting or decrypting `secrets.vault.enc` fails."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message, status_code=status_code, code="gdrive_vault_error")


# ---------------------------------------------------------------------------
# Proxy-Aware HTTP Transport (Injectable for Tests)
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        raw = self.text().strip()
        if not raw:
            return {}
        return json.loads(raw)


TransportCallable = Callable[[str, str, dict[str, str], Optional[bytes], float], HttpResponse]

_DEFAULT_TRANSPORT_OVERRIDE: Optional[TransportCallable] = None


def set_default_transport(transport: Optional[TransportCallable]) -> None:
    """Override the module HTTP transport (primarily for offline automated tests)."""
    global _DEFAULT_TRANSPORT_OVERRIDE
    _DEFAULT_TRANSPORT_OVERRIDE = transport


def urllib_proxy_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> HttpResponse:
    """Execute HTTP request via Hub's outbound proxy opener (`proxy_router.build_urllib_proxy_opener`)."""
    opener = proxy_router.build_urllib_proxy_opener(config.resolve_outbound_proxy())
    req = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers or {}),
        method=method.upper(),
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", None) or resp.getcode() or 200)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return HttpResponse(status=status, headers=resp_headers, body=resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read()
        except Exception:
            err_body = b""
        resp_headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        return HttpResponse(status=int(exc.code), headers=resp_headers, body=err_body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GDriveServiceError(
            f"Сетевая ошибка при обращении к Google API: {exc}",
            status_code=502,
            code="network_error",
        ) from exc


def _resolve_transport(transport: Optional[TransportCallable]) -> TransportCallable:
    if transport is not None:
        return transport
    if _DEFAULT_TRANSPORT_OVERRIDE is not None:
        return _DEFAULT_TRANSPORT_OVERRIDE
    return urllib_proxy_transport


# ---------------------------------------------------------------------------
# Per-User Sync Concurrency Locks
# ---------------------------------------------------------------------------

_SYNC_STATE_GUARD = threading.Lock()
_USER_SYNC_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}
_ACTIVE_SYNC_USERS: set[str] = set()
_ACTIVE_SYNC_PROGRESS: dict[str, dict[str, Any]] = {}


def get_user_sync_lock(user_id: str) -> asyncio.Lock:
    clean_uid = str(user_id or "").strip() or "admin"
    loop_id = id(asyncio.get_running_loop())
    key = (clean_uid, loop_id)
    with _SYNC_STATE_GUARD:
        lock = _USER_SYNC_LOCKS.get(key)
        if lock is None:
            if len(_USER_SYNC_LOCKS) > 128:
                stale = [k for k, lk in _USER_SYNC_LOCKS.items() if not lk.locked()]
                for sk in stale[:64]:
                    _USER_SYNC_LOCKS.pop(sk, None)
            lock = asyncio.Lock()
            _USER_SYNC_LOCKS[key] = lock
        return lock


def is_user_sync_in_progress(user_id: str) -> bool:
    clean_uid = str(user_id or "").strip() or "admin"
    with _SYNC_STATE_GUARD:
        return clean_uid in _ACTIVE_SYNC_USERS


def get_user_sync_progress(user_id: str) -> Optional[dict[str, Any]]:
    """Return a snapshot of active sync progress for `user_id`, or `None` when idle."""
    clean_uid = str(user_id or "").strip() or "admin"
    with _SYNC_STATE_GUARD:
        if clean_uid not in _ACTIVE_SYNC_USERS:
            return None
        snap = _ACTIVE_SYNC_PROGRESS.get(clean_uid)
        return dict(snap) if isinstance(snap, dict) else None


def _set_user_sync_progress(user_id: str, progress: Optional[dict[str, Any]]) -> None:
    clean_uid = str(user_id or "").strip() or "admin"
    with _SYNC_STATE_GUARD:
        if progress is None:
            _ACTIVE_SYNC_PROGRESS.pop(clean_uid, None)
        else:
            _ACTIVE_SYNC_PROGRESS[clean_uid] = dict(progress)


def _mark_user_sync_active(user_id: str, active: bool) -> None:
    clean_uid = str(user_id or "").strip() or "admin"
    with _SYNC_STATE_GUARD:
        if active:
            _ACTIVE_SYNC_USERS.add(clean_uid)
        else:
            _ACTIVE_SYNC_USERS.discard(clean_uid)
            _ACTIVE_SYNC_PROGRESS.pop(clean_uid, None)


# ---------------------------------------------------------------------------
# Event-Driven Auto-Sync Dirty Tracker & WebSocket Status Notifier (v152)
# ---------------------------------------------------------------------------

AUTO_SYNC_POLL_INTERVAL_SEC = 25.0
AUTO_SYNC_QUIET_PERIOD_SEC = 45.0
AUTO_SYNC_SAFETY_INTERVAL_SEC = 4.0 * 3600.0
AUTO_SYNC_ERROR_BACKOFF_SEC = 180.0

_AUTO_SYNC_STATE_LOCK = threading.Lock()
_DIRTY_CHANGES: dict[str, dict[str, Any]] = {}
_LAST_AUTO_SYNC_ERROR_MONO: dict[str, float] = {}
_WS_STATUS_NOTIFIER: Optional[Callable[[dict[str, Any]], Any]] = None


def set_gdrive_ws_notifier(notifier: Optional[Callable[[dict[str, Any]], Any]]) -> None:
    """Register an async/sync callback broadcasting `gdrive_sync_status` events to admin WS clients."""
    global _WS_STATUS_NOTIFIER
    _WS_STATUS_NOTIFIER = notifier


async def _emit_gdrive_ws_status(
    payload: dict[str, Any],
    ws_notifier: Optional[Callable[[dict[str, Any]], Any]] = None,
) -> None:
    cb = ws_notifier if ws_notifier is not None else _WS_STATUS_NOTIFIER
    if cb is None:
        return
    try:
        res = cb(payload)
        if inspect.isawaitable(res):
            await res
    except Exception:
        pass


def notify_gdrive_change(
    user_id: str = "admin",
    reason: str = "",
    *,
    now_mono: Optional[float] = None,
) -> None:
    """O(1) non-blocking marker recording that local Hub data changed and awaits quiet-period sync."""
    clean_uid = str(user_id or "").strip() or "admin"
    ts = time.monotonic() if now_mono is None else float(now_mono)
    clean_reason = str(reason or "").strip()[:64]
    with _AUTO_SYNC_STATE_LOCK:
        rec = _DIRTY_CHANGES.get(clean_uid)
        if rec is None:
            reasons: set[str] = {clean_reason} if clean_reason else set()
            _DIRTY_CHANGES[clean_uid] = {
                "last_changed_mono": ts,
                "first_changed_mono": ts,
                "reasons": reasons,
            }
        else:
            rec["last_changed_mono"] = ts
            if clean_reason:
                rs = rec.get("reasons")
                if isinstance(rs, set) and len(rs) < 16:
                    rs.add(clean_reason)
        # Fresh local change cancels stale error backoff so next quiet window can try cleanly
        _LAST_AUTO_SYNC_ERROR_MONO.pop(clean_uid, None)


def get_gdrive_dirty_state(user_id: str = "admin") -> Optional[dict[str, Any]]:
    clean_uid = str(user_id or "").strip() or "admin"
    with _AUTO_SYNC_STATE_LOCK:
        rec = _DIRTY_CHANGES.get(clean_uid)
        if rec is None:
            return None
        return {
            "last_changed_mono": float(rec["last_changed_mono"]),
            "first_changed_mono": float(rec["first_changed_mono"]),
            "reasons": sorted(rec.get("reasons") or ()),
        }


def clear_gdrive_dirty_state(
    user_id: Optional[str] = None,
    *,
    up_to_mono: Optional[float] = None,
) -> None:
    with _AUTO_SYNC_STATE_LOCK:
        if user_id is None:
            _DIRTY_CHANGES.clear()
            _LAST_AUTO_SYNC_ERROR_MONO.clear()
            return
        clean_uid = str(user_id or "").strip() or "admin"
        if up_to_mono is None:
            _DIRTY_CHANGES.pop(clean_uid, None)
            _LAST_AUTO_SYNC_ERROR_MONO.pop(clean_uid, None)
            return
        rec = _DIRTY_CHANGES.get(clean_uid)
        if rec is not None and float(rec.get("last_changed_mono") or 0.0) <= float(up_to_mono):
            _DIRTY_CHANGES.pop(clean_uid, None)


def _public_gdrive_cfg_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in (cfg or {}).items()
        if k not in ("oauth_client", "tokens", "vault_passphrase")
    }


# ---------------------------------------------------------------------------
# OAuth 2.0 (PKCE S256, `drive.file`, Auto-Refresh)
# ---------------------------------------------------------------------------

_OAUTH_FLOWS_LOCK = threading.Lock()
_PENDING_OAUTH_FLOWS: dict[str, dict[str, Any]] = {}
_OAUTH_FLOW_TTL_SEC = 900.0


def parse_google_oauth_client_input(
    *,
    oauth_client_json: Any = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> dict[str, Any]:
    """Parse either raw Google `client_secret_*.json` (`installed`/`web`/flat) or explicit `client_id` + `client_secret`."""
    if oauth_client_json is not None and (
        isinstance(oauth_client_json, dict) or str(oauth_client_json).strip()
    ):
        if isinstance(oauth_client_json, dict):
            parsed = oauth_client_json
        else:
            try:
                parsed = json.loads(str(oauth_client_json).strip())
            except json.JSONDecodeError as exc:
                raise GDriveServiceError(
                    f"Некорректный JSON OAuth-клиента Google: {exc}",
                    status_code=400,
                ) from exc
        if not isinstance(parsed, dict):
            raise GDriveServiceError("JSON OAuth-клиента должен быть объектом", status_code=400)

        client_type = "custom"
        section: dict[str, Any] = parsed
        if isinstance(parsed.get("installed"), dict):
            section = parsed["installed"]
            client_type = "installed"
        elif isinstance(parsed.get("web"), dict):
            section = parsed["web"]
            client_type = "web"

        cid = str(section.get("client_id") or "").strip()
        csec = str(section.get("client_secret") or "").strip()
        auth_uri = str(section.get("auth_uri") or GOOGLE_AUTH_URL).strip() or GOOGLE_AUTH_URL
        token_uri = str(section.get("token_uri") or GOOGLE_TOKEN_URL).strip() or GOOGLE_TOKEN_URL
        if not cid or not csec:
            raise GDriveServiceError(
                "В JSON OAuth-клиента отсутствуют обязательные поля client_id и client_secret",
                status_code=400,
            )
        return {
            "client_id": cid,
            "client_secret": csec,
            "auth_uri": auth_uri,
            "token_uri": token_uri,
            "client_type": client_type,
        }

    cid = str(client_id or "").strip()
    csec = str(client_secret or "").strip()
    if not cid or not csec:
        raise GDriveServiceError(
            "Укажите JSON-файл OAuth-клиента либо пару client_id и client_secret",
            status_code=400,
        )
    return {
        "client_id": cid,
        "client_secret": csec,
        "auth_uri": GOOGLE_AUTH_URL,
        "token_uri": GOOGLE_TOKEN_URL,
        "client_type": "manual",
    }


def _compute_pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _prune_expired_oauth_flows_locked(now_mono: Optional[float] = None) -> None:
    now = now_mono if now_mono is not None else time.monotonic()
    expired = [
        st for st, info in _PENDING_OAUTH_FLOWS.items()
        if (now - float(info.get("created_at") or 0.0)) > _OAUTH_FLOW_TTL_SEC
    ]
    for st in expired:
        _PENDING_OAUTH_FLOWS.pop(st, None)


def start_oauth_flow(
    user_id: str,
    *,
    client_id: str,
    redirect_uri: str,
    auth_uri: str = GOOGLE_AUTH_URL,
) -> dict[str, str]:
    """Generate a Google OAuth 2.0 authorization URL with PKCE (`S256`) and offline refresh token."""
    clean_uid = str(user_id or "").strip()
    clean_cid = str(client_id or "").strip()
    clean_redirect = str(redirect_uri or "").strip()
    if not clean_uid or not clean_cid or not clean_redirect:
        raise GDriveServiceError("Недостаточно параметров для запуска OAuth-авторизации", status_code=400)

    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _compute_pkce_challenge(code_verifier)
    now_mono = time.monotonic()

    with _OAUTH_FLOWS_LOCK:
        _prune_expired_oauth_flows_locked(now_mono)
        _PENDING_OAUTH_FLOWS[state] = {
            "user_id": clean_uid,
            "state": state,
            "code_verifier": code_verifier,
            "redirect_uri": clean_redirect,
            "created_at": now_mono,
        }

    params = urllib.parse.urlencode(
        {
            "client_id": clean_cid,
            "redirect_uri": clean_redirect,
            "response_type": "code",
            "scope": GOOGLE_DRIVE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    base_auth = (auth_uri or GOOGLE_AUTH_URL).strip() or GOOGLE_AUTH_URL
    sep = "&" if "?" in base_auth else "?"
    return {
        "auth_url": f"{base_auth}{sep}{params}",
        "state": state,
        "redirect_uri": clean_redirect,
    }


def parse_oauth_exchange_input(
    callback_url_or_code: str,
    state: Optional[str] = None,
) -> tuple[str, str]:
    """Extract `(code, state)` from either a full redirect URL or a raw authorization code."""
    raw = str(callback_url_or_code or "").strip()
    explicit_state = str(state or "").strip()
    if not raw:
        raise GDriveServiceError("Не указан код авторизации или URL редиректа", status_code=400)

    if "://" in raw or "?" in raw or "code=" in raw or "error=" in raw:
        if "://" in raw:
            query_str = urllib.parse.urlsplit(raw).query
        elif "?" in raw:
            query_str = raw.split("?", 1)[1]
        else:
            query_str = raw
        qs = urllib.parse.parse_qs(query_str, keep_blank_values=False)
        if qs.get("error"):
            err_code = str(qs["error"][0])
            err_desc = str((qs.get("error_description") or [""])[0]).strip()
            detail = f"{err_code}: {err_desc}" if err_desc else err_code
            raise GDriveServiceError(f"Google отклонил авторизацию: {detail}", status_code=400)
        code = str((qs.get("code") or [""])[0]).strip()
        url_state = str((qs.get("state") or [""])[0]).strip()
        if not code:
            raise GDriveServiceError("В вставленном URL не найден параметр code", status_code=400)
        return code, (url_state or explicit_state)

    return raw, explicit_state


def _extract_google_error_message(resp: HttpResponse, fallback: str) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or "").strip()
                if msg:
                    return msg
            elif isinstance(err, str) and err.strip():
                desc = str(data.get("error_description") or "").strip()
                return f"{err}: {desc}" if desc else err
    except Exception:
        pass
    txt = resp.text().strip()
    return txt[:300] if txt else fallback


def _exchange_code_for_tokens_sync(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: str,
    token_uri: str,
    transport: TransportCallable,
) -> tuple[dict[str, Any], str]:
    form_body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    resp = transport(
        "POST",
        token_uri or GOOGLE_TOKEN_URL,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        form_body,
        30.0,
    )
    if resp.status != 200:
        msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
        raise GDriveAuthError(f"Ошибка обмена кода OAuth в Google: {msg}", status_code=400)
    data = resp.json()
    if not isinstance(data, dict) or not str(data.get("access_token") or "").strip():
        raise GDriveAuthError("Google не вернул access_token при обмене OAuth-кода", status_code=400)

    access_token = str(data.get("access_token") or "").strip()
    refresh_token = str(data.get("refresh_token") or "").strip()
    expires_in = int(data.get("expires_in") or 3600)
    scope = str(data.get("scope") or GOOGLE_DRIVE_SCOPE).strip()
    tokens = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + max(60, expires_in),
        "scope": scope,
    }

    email = ""
    try:
        ui_resp = transport(
            "GET",
            GOOGLE_USERINFO_URL,
            {"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            None,
            15.0,
        )
        if ui_resp.status == 200:
            ui_data = ui_resp.json()
            if isinstance(ui_data, dict):
                email = str(ui_data.get("email") or "").strip()
    except Exception:
        email = ""
    return tokens, email


async def complete_oauth_exchange(
    user_id: str,
    *,
    code_or_url: str,
    state: Optional[str] = None,
    transport: Optional[TransportCallable] = None,
) -> dict[str, Any]:
    """Exchange Google OAuth authorization code (from callback or pasted redirect URL) for tokens and persist."""
    clean_uid = str(user_id or "").strip()
    code, eff_state = parse_oauth_exchange_input(code_or_url, state=state)

    with _OAUTH_FLOWS_LOCK:
        _prune_expired_oauth_flows_locked()
        flow: Optional[dict[str, Any]] = None
        if eff_state:
            cand = _PENDING_OAUTH_FLOWS.get(eff_state)
            if cand and cand.get("user_id") == clean_uid:
                flow = dict(cand)
        else:
            user_flows = [
                f for f in _PENDING_OAUTH_FLOWS.values()
                if f.get("user_id") == clean_uid
            ]
            if user_flows:
                user_flows.sort(key=lambda f: float(f.get("created_at") or 0.0), reverse=True)
                flow = dict(user_flows[0])

    if not flow:
        raise GDriveServiceError(
            "Сессия OAuth не найдена или истекла (несовпадение state). Запустите авторизацию заново.",
            status_code=400,
        )

    cfg = await database.get_gdrive_config(clean_uid, include_secrets=True)
    oauth_client = cfg.get("oauth_client")
    if not isinstance(oauth_client, dict) or not oauth_client.get("client_id") or not oauth_client.get("client_secret"):
        raise GDriveServiceError("Сначала сохраните OAuth-клиент Google (client_id / client_secret)", status_code=400)

    eff_transport = _resolve_transport(transport)
    tokens, email = await asyncio.to_thread(
        _exchange_code_for_tokens_sync,
        code=code,
        client_id=str(oauth_client["client_id"]),
        client_secret=str(oauth_client["client_secret"]),
        redirect_uri=str(flow["redirect_uri"]),
        code_verifier=str(flow["code_verifier"]),
        token_uri=str(oauth_client.get("token_uri") or GOOGLE_TOKEN_URL),
        transport=eff_transport,
    )

    # Preserve previous refresh_token if Google omitted it on re-consent
    prev_tokens = cfg.get("tokens")
    if not tokens.get("refresh_token") and isinstance(prev_tokens, dict):
        tokens["refresh_token"] = str(prev_tokens.get("refresh_token") or "").strip()

    with _OAUTH_FLOWS_LOCK:
        _PENDING_OAUTH_FLOWS.pop(str(flow["state"]), None)

    return await database.save_gdrive_config(
        clean_uid,
        tokens=tokens,
        account_email=email or str(cfg.get("account_email") or ""),
        is_enabled=True,
    )


def _refresh_access_token_sync(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    token_uri: str,
    transport: TransportCallable,
) -> dict[str, Any]:
    form_body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    resp = transport(
        "POST",
        token_uri or GOOGLE_TOKEN_URL,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        form_body,
        30.0,
    )
    if resp.status != 200:
        msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
        raise GDriveAuthError(
            f"Не удалось обновить access_token Google Drive: {msg}",
            status_code=401,
        )
    data = resp.json()
    if not isinstance(data, dict) or not str(data.get("access_token") or "").strip():
        raise GDriveAuthError("Google не вернул новый access_token при обновлении токена", status_code=401)
    expires_in = int(data.get("expires_in") or 3600)
    return {
        "access_token": str(data.get("access_token") or "").strip(),
        "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
        "expires_at": int(time.time()) + max(60, expires_in),
        "scope": str(data.get("scope") or GOOGLE_DRIVE_SCOPE).strip(),
    }


async def ensure_valid_access_token(
    user_id: str,
    cfg: dict[str, Any],
    *,
    force_refresh: bool = False,
    transport: Optional[TransportCallable] = None,
) -> str:
    """Return a valid Google OAuth `access_token`, auto-refreshing via `refresh_token` when expired."""
    tokens = cfg.get("tokens")
    oauth_client = cfg.get("oauth_client")
    if not isinstance(tokens, dict):
        raise GDriveAuthError("Google Диск не подключён. Выполните OAuth-авторизацию.", status_code=401)

    cur_access = str(tokens.get("access_token") or "").strip()
    expires_at = float(tokens.get("expires_at") or 0.0)
    if not force_refresh and cur_access and expires_at > (time.time() + 60.0):
        return cur_access

    refresh_tok = str(tokens.get("refresh_token") or "").strip()
    if not refresh_tok:
        if not force_refresh and cur_access:
            return cur_access
        raise GDriveAuthError(
            "Срок действия токена Google истёк, а refresh_token отсутствует. Переподключите аккаунт.",
            status_code=401,
        )
    if not isinstance(oauth_client, dict) or not oauth_client.get("client_id") or not oauth_client.get("client_secret"):
        raise GDriveAuthError("Отсутствуют данные OAuth-клиента для обновления токена", status_code=401)

    eff_transport = _resolve_transport(transport)
    new_tokens = await asyncio.to_thread(
        _refresh_access_token_sync,
        refresh_token=refresh_tok,
        client_id=str(oauth_client["client_id"]),
        client_secret=str(oauth_client["client_secret"]),
        token_uri=str(oauth_client.get("token_uri") or GOOGLE_TOKEN_URL),
        transport=eff_transport,
    )
    if not new_tokens.get("scope") and tokens.get("scope"):
        new_tokens["scope"] = str(tokens["scope"])
    cfg["tokens"] = new_tokens
    await database.save_gdrive_config(user_id, tokens=new_tokens)
    return str(new_tokens["access_token"])


def render_oauth_callback_html(*, ok: bool, message: str, account_email: str = "") -> str:
    """Render a clean Russian HTML callback page notifying `window.opener` via `postMessage`."""
    title = "Google Диск подключён" if ok else "Ошибка подключения Google Диска"
    safe_msg = html.escape(message or "")
    safe_email = html.escape(account_email or "")
    payload = json.dumps(
        {
            "type": "agy-gdrive-oauth-done",
            "ok": bool(ok),
            "account_email": account_email or "",
            "error": "" if ok else (message or ""),
        },
        ensure_ascii=False,
    )
    badge = "✅" if ok else "⚠️"
    email_line = f"<p style='color:#94a3b8;margin:8px 0 0'>{safe_email}</p>" if (ok and safe_email) else ""
    return (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)} — ZeroHub</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>"
        "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px;}"
        ".card{background:#1e293b;border:1px solid #334155;border-radius:14px;padding:28px 24px;"
        "max-width:440px;width:100%;text-align:center;box-shadow:0 12px 32px rgba(0,0,0,.35);}"
        "h1{font-size:20px;margin:8px 0 10px;}p{font-size:14px;line-height:1.5;margin:0;color:#cbd5e1;}"
        "button{margin-top:18px;padding:10px 18px;border-radius:8px;border:1px solid #475569;"
        "background:#334155;color:#f8fafc;cursor:pointer;font-size:14px;}"
        "</style></head><body><div class='card'>"
        f"<div style='font-size:34px'>{badge}</div>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p>{safe_msg}</p>{email_line}"
        "<button type='button' onclick='window.close()'>Закрыть окно</button>"
        "</div><script>"
        "(function(){try{"
        f"const msg={payload};"
        "window.opener?.postMessage(msg, window.location.origin);"
        "}catch(e){}"
        + ("setTimeout(function(){try{window.close();}catch(e){}}, 1200);" if ok else "")
        + "})();</script></body></html>"
    )


# ---------------------------------------------------------------------------
# Layer 1: `.gitignore` Matcher
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GitIgnoreRule:
    negated: bool
    dir_only: bool
    anchored: bool
    regex: re.Pattern[str]


def _gitignore_glob_to_regex(pattern: str, *, anchored: bool) -> re.Pattern[str]:
    """Translate a gitignore glob pattern (supporting `*`, `?`, `[...]`, `**`) to a compiled regex."""
    i = 0
    n = len(pattern)
    out: list[str] = []
    if anchored:
        out.append("^")
    else:
        out.append("(?:^|/)")

    while i < n:
        ch = pattern[i]
        if ch == "\\":
            if i + 1 < n:
                out.append(re.escape(pattern[i + 1]))
                i += 2
            else:
                out.append(r"\\")
                i += 1
            continue
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # Handle `**/` or `/**` or standalone `**`
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(r"\[")
                i += 1
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
            continue
        out.append(re.escape(ch))
        i += 1

    out.append("$")
    flags = re.IGNORECASE if config.IS_WINDOWS else 0
    return re.compile("".join(out), flags=flags)


class GitIgnoreMatcher:
    """Root `.gitignore` rule evaluator supporting directories, wildcards, `**`, and `!` negations."""

    def __init__(self, rules: list[_GitIgnoreRule]) -> None:
        self._rules = rules
        self._has_negation = any(r.negated for r in rules)

    @classmethod
    def from_text(cls, text: str) -> "GitIgnoreMatcher":
        rules: list[_GitIgnoreRule] = []
        for raw_line in (text or "").splitlines():
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                continue
            # Strip trailing unescaped spaces
            while line.endswith(" ") and not line.endswith("\\ "):
                line = line[:-1]
            if not line:
                continue
            if line.startswith("#"):
                continue

            negated = False
            if line.startswith("!"):
                negated = True
                line = line[1:]
            elif line.startswith("\\#") or line.startswith("\\!"):
                line = line[1:]

            if not line:
                continue

            dir_only = False
            if line.endswith("/") and not line.endswith("\\/"):
                dir_only = True
                line = line.rstrip("/")
            if not line:
                continue

            anchored = False
            if line.startswith("/"):
                anchored = True
                line = line.lstrip("/")
            elif "/" in line:
                anchored = True

            if not line:
                continue

            try:
                rx = _gitignore_glob_to_regex(line, anchored=anchored)
            except re.error:
                continue
            rules.append(
                _GitIgnoreRule(
                    negated=negated,
                    dir_only=dir_only,
                    anchored=anchored,
                    regex=rx,
                )
            )
        return cls(rules)

    @classmethod
    def from_file(cls, gitignore_path: Path) -> "GitIgnoreMatcher":
        if not gitignore_path.is_file() or workspace_mcp._is_reparse_or_symlink(gitignore_path):
            return cls([])
        try:
            text = gitignore_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return cls([])
        return cls.from_text(text)

    @property
    def has_negation(self) -> bool:
        return self._has_negation

    def is_ignored(self, rel_posix: str, *, is_dir: bool) -> bool:
        norm = str(rel_posix or "").strip().replace("\\", "/").strip("/")
        if not norm or not self._rules:
            return False

        parts = norm.split("/")
        ancestor_prefixes: list[str] = []
        if len(parts) > 1:
            cur = ""
            for seg in parts[:-1]:
                cur = f"{cur}/{seg}" if cur else seg
                ancestor_prefixes.append(cur)

        ignored = False
        for rule in self._rules:
            if rule.dir_only:
                if is_dir and rule.regex.search(norm):
                    ignored = not rule.negated
                elif ancestor_prefixes and any(rule.regex.search(anc) for anc in ancestor_prefixes):
                    ignored = not rule.negated
                continue

            if rule.regex.search(norm):
                ignored = not rule.negated
            elif not rule.negated and ancestor_prefixes and any(rule.regex.search(anc) for anc in ancestor_prefixes):
                ignored = True
        return ignored


# ---------------------------------------------------------------------------
# Layers 2–4: Four-Layer Project Scanner
# ---------------------------------------------------------------------------


def is_blocked_project_dir_name(name: str, *, depth: int, is_self_hub: bool) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return True
    if low in BLOCKED_PROJECT_DIRS_EXACT:
        return True
    if is_self_hub and depth == 0 and low in SELF_HUB_BLOCKED_TOP_DIRS:
        return True
    return any(sub in low for sub in BLOCKED_PROJECT_DIR_SUBSTRINGS)


def is_secret_dir_name(name: str) -> bool:
    return str(name or "").strip().lower() in SECRET_DIRS_EXACT


def is_secret_file_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    if low in SECRET_ALLOWED_EXACT_NAMES:
        return False
    if low == ".env" or low.startswith(".env."):
        return True
    return any(fnmatch.fnmatchcase(low, pat) for pat in SECRET_FILE_PATTERNS)


def is_vault_excluded_secret_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    return any(fnmatch.fnmatchcase(low, pat) for pat in VAULT_EXCLUDED_FILE_PATTERNS)


def is_blocked_binary_file_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return True
    return any(fnmatch.fnmatchcase(low, pat) for pat in BLOCKED_BINARY_PATTERNS)


def _classify_project_asset_or_code_file(rel_posix: str, filename: str, size_bytes: int) -> str:
    """Classify a non-secret, non-gitignored project file: returns `'allow'`, `'binary'`, or `'size_limit'`."""
    ext = Path(filename).suffix.lower()
    dir_segments = {seg.lower() for seg in str(rel_posix or "").replace("\\", "/").split("/")[:-1] if seg}

    # Compact UI sounds (<= 512 KiB) are synced ONLY inside explicit UI/asset directories
    if ext in PROJECT_UI_SOUND_EXTENSIONS:
        if dir_segments & PROJECT_UI_SOUND_DIR_SEGMENTS:
            return "allow" if size_bytes <= MAX_UI_SOUND_FILE_BYTES else "size_limit"
        return "binary"

    # UI graphics, icons, illustrations, and web fonts up to 5 MiB
    if ext in PROJECT_UI_ASSET_EXTENSIONS:
        return "allow" if size_bytes <= MAX_UI_ASSET_FILE_BYTES else "size_limit"

    if is_blocked_binary_file_name(filename):
        return "binary"

    if size_bytes > MAX_CODE_FILE_BYTES:
        return "size_limit"

    return "allow"


def _check_anti_dump_subfolder(dir_path: Path, rel_posix: str = "") -> tuple[bool, int]:
    """Anti-Dump Guard: True if subfolder contains > 80 data files (plus raster images outside UI asset dirs) and 0 source-code files."""
    rel_segments = {seg.lower() for seg in str(rel_posix or "").replace("\\", "/").split("/") if seg}
    root_is_ui_branch = bool(rel_segments & PROJECT_UI_KEEP_DIR_SEGMENTS)

    data_count = 0
    stack: list[tuple[Path, bool]] = [(dir_path, root_is_ui_branch)]
    while stack:
        cur, in_ui_branch = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    p = Path(entry.path)
                    if workspace_mcp._is_reparse_or_symlink(p):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            name_low = entry.name.lower()
                            if (
                                name_low in BLOCKED_PROJECT_DIRS_EXACT
                                or name_low in SECRET_DIRS_EXACT
                                or any(sub in name_low for sub in BLOCKED_PROJECT_DIR_SUBSTRINGS)
                            ):
                                continue
                            stack.append((p, in_ui_branch or (name_low in PROJECT_UI_KEEP_DIR_SEGMENTS)))
                        elif entry.is_file(follow_symlinks=False):
                            ext = Path(entry.name).suffix.lower()
                            if ext in ANTI_DUMP_CODE_EXTENSIONS:
                                return False, data_count
                            if ext in ANTI_DUMP_DATA_EXTENSIONS or (
                                not in_ui_branch and ext in ANTI_DUMP_RASTER_IMAGE_EXTENSIONS
                            ):
                                data_count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return (data_count > ANTI_DUMP_MIN_DATA_FILES), data_count


@dataclass
class ScannedCodeFile:
    project_name: str
    rel_path: str
    abs_path: Path
    size_bytes: int


@dataclass
class ScannedSecretCandidate:
    project_name: str
    rel_path: str
    abs_path: Path
    size_bytes: int


@dataclass
class ProjectScanResult:
    project_name: str
    project_dir: Path
    is_self_hub: bool
    code_files: list[ScannedCodeFile]
    vault_candidates: list[ScannedSecretCandidate]
    skipped_large_files: list[dict[str, Any]]
    skipped_dump_dirs: list[dict[str, Any]]
    skipped_binary_count: int = 0
    skipped_gitignore_count: int = 0
    skipped_secret_unvaulted_count: int = 0


def _collect_secret_dir_candidates(
    secret_dir: Path,
    *,
    project_root: Path,
    project_name: str,
    vault_candidates: list[ScannedSecretCandidate],
    unvaulted_counter: list[int],
) -> None:
    """Collect compact non-session secret files inside `.secrets/` or `private-profiles/` for vault only."""
    stack: list[Path] = [secret_dir]
    while stack:
        cur = stack.pop()
        try:
            entries = sorted(os.scandir(cur), key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            p = Path(entry.path)
            if workspace_mcp._is_reparse_or_symlink(p):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() in BLOCKED_PROJECT_DIRS_EXACT:
                        continue
                    stack.append(p)
                elif entry.is_file(follow_symlinks=False):
                    size = int(entry.stat(follow_symlinks=False).st_size)
                    if is_vault_excluded_secret_name(entry.name) or size > MAX_VAULT_SECRET_FILE_BYTES:
                        unvaulted_counter[0] += 1
                        continue
                    rel = p.relative_to(project_root).as_posix()
                    vault_candidates.append(
                        ScannedSecretCandidate(
                            project_name=project_name,
                            rel_path=rel,
                            abs_path=p,
                            size_bytes=size,
                        )
                    )
            except OSError:
                continue


def scan_project_for_gdrive(
    project_dir: Path,
    *,
    project_name: str = "",
    is_self_hub: bool = False,
) -> ProjectScanResult:
    """Apply the 4-layer project filter to `project_dir` without modifying files or following symlinks."""
    resolved_root = Path(project_dir).resolve()
    eff_name = (project_name or resolved_root.name).strip() or "Project"
    gitignore = GitIgnoreMatcher.from_file(resolved_root / ".gitignore")

    code_files: list[ScannedCodeFile] = []
    vault_candidates: list[ScannedSecretCandidate] = []
    skipped_large_files: list[dict[str, Any]] = []
    skipped_dump_dirs: list[dict[str, Any]] = []
    skipped_binary_count = 0
    skipped_gitignore_count = 0
    unvaulted_counter = [0]

    if not resolved_root.is_dir() or workspace_mcp._is_reparse_or_symlink(resolved_root):
        return ProjectScanResult(
            project_name=eff_name,
            project_dir=resolved_root,
            is_self_hub=is_self_hub,
            code_files=[],
            vault_candidates=[],
            skipped_large_files=[],
            skipped_dump_dirs=[],
        )

    def _walk(cur_dir: Path, depth: int) -> None:
        nonlocal skipped_binary_count, skipped_gitignore_count
        try:
            with os.scandir(cur_dir) as it:
                entries = sorted(list(it), key=lambda e: e.name.lower())
        except OSError:
            return

        for entry in entries:
            p = Path(entry.path)
            if workspace_mcp._is_reparse_or_symlink(p):
                continue
            try:
                rel_posix = p.relative_to(resolved_root).as_posix()
            except ValueError:
                continue

            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = (not is_dir) and entry.is_file(follow_symlinks=False)
            except OSError:
                continue

            if is_dir:
                # Layer 3: secret directories NEVER enter `code/`, scanned strictly for vault candidates
                if is_secret_dir_name(entry.name):
                    _collect_secret_dir_candidates(
                        p,
                        project_root=resolved_root,
                        project_name=eff_name,
                        vault_candidates=vault_candidates,
                        unvaulted_counter=unvaulted_counter,
                    )
                    continue

                # Layer 2: blocked runtime, output, build, cache, browser profile directories
                if is_blocked_project_dir_name(entry.name, depth=depth, is_self_hub=is_self_hub):
                    continue

                # Layer 4: Anti-Dump Guard on subfolders
                is_dump, dump_data_count = _check_anti_dump_subfolder(p, rel_posix=rel_posix)
                if is_dump:
                    skipped_dump_dirs.append(
                        {
                            "project": eff_name,
                            "path": rel_posix,
                            "data_files_count": dump_data_count,
                            "reason": "anti_dump_guard",
                        }
                    )
                    continue

                # Layer 1: `.gitignore` directory skip (unless `.gitignore` uses `!` negation rules)
                if gitignore.is_ignored(rel_posix, is_dir=True) and not gitignore.has_negation:
                    skipped_gitignore_count += 1
                    continue

                _walk(p, depth + 1)
                continue

            if not is_file:
                continue

            if rel_posix.lower() in INTERNAL_AUTO_EXCLUDED_CODE_PATHS:
                continue

            try:
                size_bytes = int(entry.stat(follow_symlinks=False).st_size)
            except OSError:
                continue

            # Layer 3: secret file interception BEFORE `.gitignore` so `.env` / `*secret*.json` still enter vault
            if is_secret_file_name(entry.name):
                if (
                    not is_vault_excluded_secret_name(entry.name)
                    and size_bytes <= MAX_VAULT_SECRET_FILE_BYTES
                ):
                    vault_candidates.append(
                        ScannedSecretCandidate(
                            project_name=eff_name,
                            rel_path=rel_posix,
                            abs_path=p,
                            size_bytes=size_bytes,
                        )
                    )
                else:
                    unvaulted_counter[0] += 1
                continue

            # Layer 1: root `.gitignore` (supports `!.env.example` negations)
            if gitignore.is_ignored(rel_posix, is_dir=False):
                skipped_gitignore_count += 1
                continue

            # Layer 4: UI assets (<= 5 MiB), UI sounds (<= 512 KiB in asset dirs), blocked binaries & 1 MiB code cap
            policy = _classify_project_asset_or_code_file(rel_posix, entry.name, size_bytes)
            if policy == "binary":
                skipped_binary_count += 1
                continue
            if policy == "size_limit":
                skipped_large_files.append(
                    {
                        "project": eff_name,
                        "path": rel_posix,
                        "size_bytes": size_bytes,
                        "reason": "size_limit",
                    }
                )
                continue

            code_files.append(
                ScannedCodeFile(
                    project_name=eff_name,
                    rel_path=rel_posix,
                    abs_path=p,
                    size_bytes=size_bytes,
                )
            )

    _walk(resolved_root, 0)
    code_files.sort(key=lambda f: f.rel_path.lower())
    vault_candidates.sort(key=lambda f: f.rel_path.lower())
    return ProjectScanResult(
        project_name=eff_name,
        project_dir=resolved_root,
        is_self_hub=is_self_hub,
        code_files=code_files,
        vault_candidates=vault_candidates,
        skipped_large_files=skipped_large_files,
        skipped_dump_dirs=skipped_dump_dirs,
        skipped_binary_count=skipped_binary_count,
        skipped_gitignore_count=skipped_gitignore_count,
        skipped_secret_unvaulted_count=unvaulted_counter[0],
    )


# ---------------------------------------------------------------------------
# Encrypted Secrets Vault (`AGY_VAULT_V1`, PBKDF2-HMAC-SHA256 600k + Fernet)
# ---------------------------------------------------------------------------


def _derive_vault_fernet_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=int(iterations),
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def build_secrets_vault_payload(candidates: list[ScannedSecretCandidate]) -> dict[str, Any]:
    """Read compact secret candidates into a canonical JSON-serializable payload dict."""
    files_map: dict[str, dict[str, Any]] = {}
    for cand in sorted(candidates, key=lambda c: (c.project_name.lower(), c.rel_path.lower())):
        if workspace_mcp._is_reparse_or_symlink(cand.abs_path):
            continue
        try:
            raw = cand.abs_path.read_bytes()
        except OSError:
            continue
        if len(raw) > MAX_VAULT_SECRET_FILE_BYTES:
            continue
        key = f"{cand.project_name}/{cand.rel_path}"
        digest = hashlib.sha256(raw).hexdigest()
        try:
            decoded_text = raw.decode("utf-8")
            encoding = "utf-8"
            content_val = decoded_text
        except UnicodeDecodeError:
            encoding = "base64"
            content_val = base64.b64encode(raw).decode("ascii")
        files_map[key] = {
            "project": cand.project_name,
            "rel_path": cand.rel_path,
            "sha256": digest,
            "size_bytes": len(raw),
            "encoding": encoding,
            "content": content_val,
        }
    return {
        "format": VAULT_PAYLOAD_FORMAT,
        "file_count": len(files_map),
        "files": files_map,
    }


def decode_vault_entry_bytes(entry: dict[str, Any]) -> bytes:
    """Recover exact original file bytes from a decrypted `secrets.vault.enc` entry."""
    enc = str(entry.get("encoding") or "utf-8").lower()
    content = str(entry.get("content") or "")
    if enc == "base64":
        return base64.b64decode(content.encode("ascii"))
    return content.encode("utf-8")


def compute_vault_logical_fingerprint(payload: dict[str, Any], passphrase: str) -> str:
    """Deterministic fingerprint of `(payload, passphrase)` to skip redundant re-encryption/uploads."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    pass_hash = hashlib.sha256(passphrase.encode("utf-8")).digest()
    h = hashlib.sha256()
    h.update(f"{VAULT_MAGIC}:{VAULT_KDF}:{VAULT_KDF_ITERATIONS}:".encode("ascii"))
    h.update(canonical)
    h.update(b":")
    h.update(pass_hash)
    return h.hexdigest()


def encrypt_secrets_vault(
    payload: dict[str, Any],
    passphrase: str,
    *,
    iterations: int = VAULT_KDF_ITERATIONS,
) -> bytes:
    """Encrypt `payload` with the user's passphrase (`AGY_VAULT_V1` JSON envelope over `zlib` + `Fernet`)."""
    if not isinstance(passphrase, str) or not passphrase:
        raise GDriveVaultError("Не задан пароль шифрования сейфа секретов")
    if not isinstance(payload, dict):
        raise GDriveVaultError("Некорректные данные для шифрования сейфа секретов")
    eff_iter = int(iterations)
    if eff_iter < 10_000:
        raise GDriveVaultError("Слишком мало итераций KDF для сейфа секретов")

    raw_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(raw_json, level=6)
    salt = os.urandom(16)
    fernet_key = _derive_vault_fernet_key(passphrase, salt, eff_iter)
    token_str = Fernet(fernet_key).encrypt(compressed).decode("ascii")

    envelope = {
        "magic": VAULT_MAGIC,
        "kdf": VAULT_KDF,
        "iter": eff_iter,
        "salt": base64.b64encode(salt).decode("ascii"),
        "ciphertext": token_str,
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decrypt_secrets_vault(raw_bytes: bytes | str, passphrase: str) -> dict[str, Any]:
    """Decrypt an `AGY_VAULT_V1` `secrets.vault.enc` container using the user's passphrase."""
    if not isinstance(passphrase, str) or not passphrase:
        raise GDriveVaultError("Не указан пароль для расшифровки сейфа")
    try:
        text = raw_bytes.decode("utf-8") if isinstance(raw_bytes, (bytes, bytearray)) else str(raw_bytes)
        envelope = json.loads(text)
    except Exception as exc:
        raise GDriveVaultError("Повреждён формат файла secrets.vault.enc") from exc

    if not isinstance(envelope, dict):
        raise GDriveVaultError("Некорректный заголовок сейфа secrets.vault.enc")
    if envelope.get("magic") != VAULT_MAGIC:
        raise GDriveVaultError("Неподдерживаемая версия сейфа (ожидается AGY_VAULT_V1)")
    if envelope.get("kdf") != VAULT_KDF:
        raise GDriveVaultError("Неподдерживаемый алгоритм KDF в сейфе")

    try:
        iterations = int(envelope.get("iter") or 0)
        salt = base64.b64decode(str(envelope.get("salt") or "").encode("ascii"))
        ciphertext = str(envelope.get("ciphertext") or "").encode("ascii")
    except Exception as exc:
        raise GDriveVaultError("Повреждены криптографические параметры сейфа") from exc
    if iterations < 1 or len(salt) < 8 or not ciphertext:
        raise GDriveVaultError("Некорректные криптографические параметры сейфа")

    fernet_key = _derive_vault_fernet_key(passphrase, salt, iterations)
    try:
        compressed = Fernet(fernet_key).decrypt(ciphertext)
    except InvalidToken as exc:
        raise GDriveVaultError("Неверный пароль сейфа или повреждённые зашифрованные данные") from exc

    try:
        raw_json = zlib.decompress(compressed)
        parsed = json.loads(raw_json.decode("utf-8"))
    except Exception as exc:
        raise GDriveVaultError("Не удалось распаковать содержимое сейфа секретов") from exc
    if not isinstance(parsed, dict):
        raise GDriveVaultError("Некорректная структура расшифрованного сейфа")
    return parsed


# ---------------------------------------------------------------------------
# Sanitized Hot SQLite Backup (`hub_dialogs_and_memory.sqlite.gz`)
# ---------------------------------------------------------------------------


def build_sanitized_sqlite_gz_with_stats(
    source_db_path: Optional[Path] = None,
) -> tuple[bytes, dict[str, int]]:
    """Create a hot read-only SQLite snapshot keeping strictly dialog/memory tables, then `VACUUM` + `gzip`."""
    src_path = Path(source_db_path or database._resolved_db_path()).resolve()
    if not src_path.is_file():
        raise GDriveServiceError("Файл базы данных Hub не найден для резервного копирования", status_code=500)

    stats = {
        "chats_count": 0,
        "messages_count": 0,
        "folders_count": 0,
        "passports_count": 0,
        "advisor_models_count": 0,
    }
    with tempfile.TemporaryDirectory(prefix="agy_gdrive_sqlite_") as tmp_dir:
        tmp_db = Path(tmp_dir) / "hub_dialogs_and_memory.sqlite"
        src_uri = f"file:{urllib.parse.quote(src_path.as_posix(), safe='/:')}?mode=ro"
        src_conn: Optional[sqlite3.Connection] = None
        dst_conn: Optional[sqlite3.Connection] = None
        try:
            src_conn = sqlite3.connect(src_uri, uri=True, timeout=15.0)
            dst_conn = sqlite3.connect(str(tmp_db), timeout=15.0)
            src_conn.backup(dst_conn)
        finally:
            if src_conn is not None:
                try:
                    src_conn.close()
                except Exception:
                    pass

        assert dst_conn is not None
        try:
            dst_conn.execute("PRAGMA foreign_keys = OFF;")
            # Drop views and triggers that might reference dropped auth/token tables
            for obj_type, obj_name in dst_conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view');"
            ).fetchall():
                escaped = str(obj_name).replace('"', '""')
                if obj_type == "trigger":
                    dst_conn.execute(f'DROP TRIGGER IF EXISTS "{escaped}";')
                elif obj_type == "view":
                    dst_conn.execute(f'DROP VIEW IF EXISTS "{escaped}";')

            # Drop every table except the strict allowlist
            tables = [
                str(row[0])
                for row in dst_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
                ).fetchall()
            ]
            kept_low: set[str] = set()
            for tbl in tables:
                tbl_low = tbl.lower()
                if tbl_low not in ALLOWED_BACKUP_TABLES:
                    escaped = tbl.replace('"', '""')
                    dst_conn.execute(f'DROP TABLE IF EXISTS "{escaped}";')
                elif tbl_low == "model_advisor_catalog":
                    escaped = tbl.replace('"', '""')
                    try:
                        mac_cnt = int(dst_conn.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0] or 0)
                    except sqlite3.Error:
                        mac_cnt = 0
                    if mac_cnt > 0:
                        kept_low.add(tbl_low)
                        stats["advisor_models_count"] = mac_cnt
                    else:
                        dst_conn.execute(f'DROP TABLE IF EXISTS "{escaped}";')
                else:
                    kept_low.add(tbl_low)

            if "chats" in kept_low:
                stats["chats_count"] = int(dst_conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0] or 0)
                try:
                    stats["folders_count"] = int(
                        dst_conn.execute(
                            "SELECT COUNT(DISTINCT folder) FROM chats WHERE trim(COALESCE(folder, '')) != ''"
                        ).fetchone()[0]
                        or 0
                    )
                except sqlite3.Error:
                    pass
            if "chat_folders" in kept_low:
                try:
                    cf_cnt = int(dst_conn.execute("SELECT COUNT(*) FROM chat_folders").fetchone()[0] or 0)
                    stats["folders_count"] = max(stats["folders_count"], cf_cnt)
                except sqlite3.Error:
                    pass
            if "messages" in kept_low:
                stats["messages_count"] = int(dst_conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] or 0)
            if "project_memories" in kept_low:
                try:
                    stats["passports_count"] = int(
                        dst_conn.execute(
                            """
                            SELECT COUNT(*) FROM project_memories
                            WHERE trim(COALESCE(passport_overview, '')) != ''
                               OR trim(COALESCE(passport_rules, '')) != ''
                               OR trim(COALESCE(passport_file_map, '')) != ''
                               OR (trim(COALESCE(changelog_json, '')) NOT IN ('', '[]'))
                            """
                        ).fetchone()[0]
                        or 0
                    )
                except sqlite3.Error:
                    pass

            dst_conn.commit()
            dst_conn.execute("PRAGMA journal_mode = DELETE;")
            dst_conn.execute("VACUUM;")
            dst_conn.commit()
        finally:
            dst_conn.close()

        raw_sqlite = tmp_db.read_bytes()
    return gzip.compress(raw_sqlite, compresslevel=6, mtime=0), stats


def build_sanitized_sqlite_gz(source_db_path: Optional[Path] = None) -> bytes:
    """Create a hot read-only SQLite snapshot keeping strictly dialog/memory tables, then `VACUUM` + `gzip`."""
    gz_bytes, _ = build_sanitized_sqlite_gz_with_stats(source_db_path=source_db_path)
    return gz_bytes


# ---------------------------------------------------------------------------
# Human-Readable Markdown Renderers (Dialogs & Project Passports)
# ---------------------------------------------------------------------------


def _safe_drive_segment(name: str, *, fallback: str = "Без_названия", max_len: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    stem_upper = cleaned.split(".")[0].upper()
    if stem_upper in _WIN_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:max_len].strip(" .") or fallback


def _chat_short_id(chat_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "", str(chat_id or ""))
    return (safe[:8] or "00000000")


def _single_line_preview(text: str, max_len: int = 120) -> str:
    flat = re.sub(r"\s+", " ", str(text or "").strip())
    if len(flat) <= max_len:
        return flat
    return flat[: max(1, max_len - 1)].rstrip() + "…"


def render_chat_markdown(chat: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    title = str(chat.get("title") or "Без названия").strip() or "Без названия"
    chat_id = str(chat.get("id") or "")
    kind = str(chat.get("kind") or "chat")
    folder = str(chat.get("folder") or "").strip()
    project_name = str(chat.get("project_name") or "").strip()
    is_archived = bool(chat.get("is_archived"))
    model = str(chat.get("model") or "").strip()
    created_at = str(chat.get("created_at") or "").strip()
    updated_at = str(chat.get("updated_at") or "").strip()

    lines: list[str] = [f"# {title}", ""]
    meta_parts: list[str] = [f"- **ID:** `{chat_id}`"]
    if kind == "project" and project_name:
        meta_parts.append(f"- **Проект:** `{project_name}`")
    elif folder:
        meta_parts.append(f"- **Папка:** {folder}")
    if is_archived:
        meta_parts.append("- **Статус:** Архив")
    if model:
        meta_parts.append(f"- **Модель:** `{model}`")
    if created_at:
        meta_parts.append(f"- **Создан:** {created_at}")
    if updated_at:
        meta_parts.append(f"- **Обновлён:** {updated_at}")
    lines.extend(meta_parts)
    lines.append("")

    bookmarked_msgs = [m for m in messages if bool(m.get("bookmarked"))]
    if bookmarked_msgs:
        lines.append("## 🔖 Закладки")
        lines.append("")
        for bm in bookmarked_msgs:
            mid = bm.get("id")
            role = str(bm.get("role") or "")
            role_lbl = "Пользователь" if role == "user" else ("Ассистент" if role == "assistant" else role)
            ts = str(bm.get("created_at") or "")[:19].replace("T", " ")
            label = (
                str(bm.get("custom_title") or "").strip()
                or str(bm.get("auto_title") or "").strip()
                or _single_line_preview(str(bm.get("content") or ""), 120)
                or "(пустое сообщение)"
            )
            lines.append(f"- 🔖 **[#{mid} · {role_lbl} · {ts}]** {label}")
        lines.append("")

    compacted = str(chat.get("compacted_summary") or "").strip()
    if compacted:
        lines.append("## Сводка сжатого контекста")
        lines.append("")
        lines.append(compacted)
        lines.append("")

    lines.append("## Сообщения")
    lines.append("")
    if not messages:
        lines.append("_(Сообщений пока нет.)_")
        lines.append("")
    else:
        for msg in messages:
            mid = msg.get("id")
            role = str(msg.get("role") or "")
            if role == "user":
                role_hdr = "👤 Пользователь"
            elif role == "assistant":
                role_hdr = "🤖 Ассистент"
            else:
                role_hdr = f"⚙️ {role or 'Система'}"
            ts = str(msg.get("created_at") or "")[:19].replace("T", " ")
            bm_mark = " 🔖" if bool(msg.get("bookmarked")) else ""
            msg_model = str(msg.get("model") or "").strip()
            model_tail = f" · `{msg_model}`" if (role == "assistant" and msg_model) else ""
            lines.append(f"### {role_hdr} (#{mid}) · {ts}{model_tail}{bm_mark}")
            title_note = str(msg.get("custom_title") or msg.get("auto_title") or "").strip()
            if title_note:
                lines.append(f"_Тема: {title_note}_")
            lines.append("")
            content = str(msg.get("content") or "").strip()
            lines.append(content if content else "_(пусто)_")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_project_passport_markdown(
    project_name: str,
    memory: dict[str, Any],
    *,
    is_shared: bool = False,
) -> str:
    overview = str(memory.get("passport_overview") or "").strip()
    rules = str(memory.get("passport_rules") or "").strip()
    file_map = str(memory.get("passport_file_map") or "").strip()
    changelog = list(memory.get("changelog") or [])
    updated_at = str(memory.get("updated_at") or "").strip()

    sections: list[str] = [
        f"# Паспорт проекта: {project_name}",
        f"- **Тип:** {'Общий проект' if is_shared else 'Личный проект'}"
        + (f"\n- **Обновлён:** {updated_at}" if updated_at else ""),
    ]
    if overview:
        sections.append(f"## Обзор проекта\n\n{overview}")
    if rules:
        sections.append(f"## Правила и ограничения\n\n{rules}")
    if file_map:
        sections.append(f"## Карта файлов и модулей\n\n{file_map}")
    if changelog:
        cl_lines = ["## Журнал изменений (Пульс)"]
        for item in changelog:
            if not isinstance(item, dict):
                continue
            ts_s = str(item.get("ts") or "")[:19].replace("T", " ")
            ctitle = str(item.get("chat_title") or "Проект").strip()
            pin = "📌 " if item.get("pinned") else ""
            summ = str(item.get("summary") or "").strip()
            flist = [str(f) for f in (item.get("files") or []) if str(f).strip()]
            files_tail = f" _(файлы: `{', '.join(flist)}`)_" if flist else ""
            cl_lines.append(f"- **[{ts_s}] {pin}{ctitle}:** {summ}{files_tail}")
        sections.append("\n".join(cl_lines))
    if not (overview or rules or file_map or changelog):
        sections.append("_(Паспорт проекта пока не заполнен.)_")
    return "\n\n".join(sections).strip() + "\n"


# ---------------------------------------------------------------------------
# Google Drive API v3 Client
# ---------------------------------------------------------------------------


def _escape_drive_query_literal(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def _guess_mime_type(filename: str) -> str:
    low = str(filename or "").lower()
    if low.endswith(".md"):
        return "text/markdown"
    if low.endswith(".json"):
        return "application/json"
    if low.endswith(".gz"):
        return "application/gzip"
    if low.endswith(".enc"):
        return "application/octet-stream"
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


class GoogleDriveClient:
    """Thread-safe Google Drive REST v3 client (folders, multipart upload, media PATCH, retries, safe Trash)."""

    def __init__(
        self,
        access_token: str,
        *,
        transport: TransportCallable,
        refresh_token_fn: Optional[Callable[[], str]] = None,
        retry_delays: Optional[tuple[float, ...]] = None,
    ) -> None:
        self._access_token = access_token
        self._transport = transport
        self._refresh_token_fn = refresh_token_fn
        self._retry_delays: tuple[float, ...] = (
            tuple(retry_delays) if retry_delays is not None else GDRIVE_RETRY_BACKOFFS_SEC
        )
        self._token_lock = threading.RLock()
        self._folder_lock = threading.RLock()
        self._folder_cache: dict[tuple[str, str], str] = {}
        self._folder_inflight_locks: dict[tuple[str, str], threading.Lock] = {}

    def _get_access_token(self) -> str:
        with self._token_lock:
            return self._access_token

    def _refresh_access_token(self, failed_token: str) -> str:
        with self._token_lock:
            if self._access_token != failed_token and self._access_token:
                return self._access_token
            if self._refresh_token_fn is None:
                return self._access_token
            new_tok = self._refresh_token_fn()
            if new_tok:
                self._access_token = new_tok
            return self._access_token

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 60.0,
    ) -> HttpResponse:
        refreshed_once = False
        delays = self._retry_delays
        max_attempts = len(delays) + 1
        last_resp: Optional[HttpResponse] = None

        for attempt in range(max_attempts):
            used_token = self._get_access_token()
            req_headers = dict(headers or {})
            req_headers["Authorization"] = f"Bearer {used_token}"
            try:
                resp = self._transport(method, url, req_headers, body, timeout)
            except GDriveServiceError as exc:
                if exc.code == "network_error" and attempt < len(delays):
                    time.sleep(delays[attempt])
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                    continue
                raise GDriveServiceError(
                    f"Сетевая ошибка при обращении к Google API: {exc}",
                    status_code=502,
                    code="network_error",
                ) from exc

            if resp.status == 401 and self._refresh_token_fn is not None and not refreshed_once:
                refreshed_once = True
                new_token = self._refresh_access_token(used_token)
                req_headers["Authorization"] = f"Bearer {new_token}"
                try:
                    resp = self._transport(method, url, req_headers, body, timeout)
                except GDriveServiceError as exc:
                    if exc.code == "network_error" and attempt < len(delays):
                        time.sleep(delays[attempt])
                        continue
                    raise
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    if attempt < len(delays):
                        time.sleep(delays[attempt])
                        continue
                    raise GDriveServiceError(
                        f"Сетевая ошибка при обращении к Google API: {exc}",
                        status_code=502,
                        code="network_error",
                    ) from exc

            last_resp = resp
            if resp.status in GDRIVE_RETRY_STATUS_CODES and attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            return resp

        assert last_resp is not None
        return last_resp

    def rename_folder(self, folder_id: str, new_name: str) -> str:
        """Rename an existing Google Drive folder in place (`PATCH`) without changing its ID or children."""
        clean_id = str(folder_id or "").strip()
        clean_name = str(new_name or "").strip() or database.DEFAULT_GDRIVE_ROOT_FOLDER_NAME
        url = f"{GOOGLE_DRIVE_API_BASE}/files/{urllib.parse.quote(clean_id, safe='')}?fields=id,name"
        body = json.dumps({"name": clean_name}, ensure_ascii=False).encode("utf-8")
        resp = self._request(
            "PATCH",
            url,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            body=body,
        )
        if resp.status not in (200, 201):
            msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
            raise GDriveServiceError(
                f"Ошибка переименования папки '{clean_id}' в '{clean_name}' в Google Drive: {msg}",
                status_code=502,
            )
        return clean_id

    def find_root_folder(
        self,
        root_folder_id: str,
        root_folder_name: str,
        *,
        rename_if_needed: bool = False,
    ) -> Optional[str]:
        """Find the existing root mirror folder without creating a new one (optionally renaming in place)."""
        clean_id = str(root_folder_id or "").strip()
        clean_name = str(root_folder_name or "").strip() or database.DEFAULT_GDRIVE_ROOT_FOLDER_NAME
        if clean_id:
            url = f"{GOOGLE_DRIVE_API_BASE}/files/{urllib.parse.quote(clean_id, safe='')}?fields=id,name,mimeType,trashed"
            resp = self._request("GET", url)
            if resp.status == 200:
                meta = resp.json()
                if (
                    isinstance(meta, dict)
                    and meta.get("id")
                    and meta.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME
                    and not bool(meta.get("trashed"))
                ):
                    found_id = str(meta["id"])
                    current_name = str(meta.get("name") or "").strip()
                    if rename_if_needed and current_name != clean_name:
                        self.rename_folder(found_id, clean_name)
                    return found_id
        return self._find_child_folder("root", clean_name)

    def ensure_root_folder(self, root_folder_id: str, root_folder_name: str) -> str:
        existing_id = self.find_root_folder(
            root_folder_id,
            root_folder_name,
            rename_if_needed=True,
        )
        if existing_id:
            return existing_id
        clean_name = str(root_folder_name or "").strip() or database.DEFAULT_GDRIVE_ROOT_FOLDER_NAME
        return self._create_folder(clean_name, parent_id=None)

    def list_children(
        self,
        parent_id: str,
        *,
        folders_only: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """List all non-trashed direct children inside `parent_id` with full pagination."""
        escaped_parent = _escape_drive_query_literal(parent_id)
        q_parts = [f"'{escaped_parent}' in parents", "trashed = false"]
        if folders_only is True:
            q_parts.append(f"mimeType = '{GOOGLE_DRIVE_FOLDER_MIME}'")
        elif folders_only is False:
            q_parts.append(f"mimeType != '{GOOGLE_DRIVE_FOLDER_MIME}'")
        q = " and ".join(q_parts)

        results: list[dict[str, Any]] = []
        page_token: Optional[str] = None
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, str] = {
                "q": q,
                "spaces": "drive",
                "pageSize": "1000",
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,trashed)",
            }
            if page_token:
                params["pageToken"] = page_token
            qs = urllib.parse.urlencode(params)
            resp = self._request("GET", f"{GOOGLE_DRIVE_API_BASE}/files?{qs}")
            if resp.status != 200:
                msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
                raise GDriveServiceError(f"Ошибка чтения списка файлов в Google Drive: {msg}", status_code=502)
            data = resp.json()
            files = data.get("files") if isinstance(data, dict) else None
            if isinstance(files, list):
                for entry in files:
                    if isinstance(entry, dict) and entry.get("id") and not bool(entry.get("trashed")):
                        results.append(entry)
            next_tok = str((data or {}).get("nextPageToken") or "").strip() if isinstance(data, dict) else ""
            if not next_tok or next_tok in seen_tokens:
                break
            seen_tokens.add(next_tok)
            page_token = next_tok
        return results

    def find_child_entry(
        self,
        parent_id: str,
        name: str,
        *,
        is_folder: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        escaped_name = _escape_drive_query_literal(name)
        escaped_parent = _escape_drive_query_literal(parent_id)
        q_parts = [
            f"name = '{escaped_name}'",
            f"'{escaped_parent}' in parents",
            "trashed = false",
        ]
        if is_folder is True:
            q_parts.append(f"mimeType = '{GOOGLE_DRIVE_FOLDER_MIME}'")
        elif is_folder is False:
            q_parts.append(f"mimeType != '{GOOGLE_DRIVE_FOLDER_MIME}'")
        qs = urllib.parse.urlencode(
            {
                "q": " and ".join(q_parts),
                "spaces": "drive",
                "pageSize": "1",
                "fields": "files(id,name,mimeType,size,modifiedTime,trashed)",
            }
        )
        resp = self._request("GET", f"{GOOGLE_DRIVE_API_BASE}/files?{qs}")
        if resp.status != 200:
            msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
            raise GDriveServiceError(f"Ошибка поиска объекта '{name}' в Google Drive: {msg}", status_code=502)
        data = resp.json()
        files = data.get("files") if isinstance(data, dict) else None
        if isinstance(files, list) and files:
            first = files[0]
            if isinstance(first, dict) and first.get("id") and not bool(first.get("trashed")):
                return first
        return None

    def download_file_bytes(self, file_id: str) -> bytes:
        url = f"{GOOGLE_DRIVE_API_BASE}/files/{urllib.parse.quote(file_id, safe='')}?alt=media"
        resp = self._request("GET", url, timeout=120.0)
        if resp.status != 200:
            msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
            raise GDriveServiceError(f"Ошибка скачивания файла '{file_id}' с Google Диска: {msg}", status_code=502)
        return bytes(resp.body)

    def walk_folder_files(
        self,
        folder_id: str,
        prefix: str = "",
        *,
        _visited: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        visited = _visited if _visited is not None else set()
        if folder_id in visited:
            return []
        visited.add(folder_id)
        out: list[dict[str, Any]] = []
        children = self.list_children(folder_id)
        for child in sorted(children, key=lambda c: str(c.get("name") or "").lower()):
            cname = str(child.get("name") or "")
            cid = str(child.get("id") or "")
            if not cname or not cid:
                continue
            rel = f"{prefix}/{cname}" if prefix else cname
            if child.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME:
                out.extend(self.walk_folder_files(cid, prefix=rel, _visited=visited))
            else:
                out.append(
                    {
                        "id": cid,
                        "name": cname,
                        "rel_path": rel,
                        "size": int(child.get("size") or 0),
                        "modifiedTime": str(child.get("modifiedTime") or ""),
                    }
                )
        return out

    def _find_child_folder(self, parent_id: str, folder_name: str) -> Optional[str]:
        escaped_name = _escape_drive_query_literal(folder_name)
        escaped_parent = _escape_drive_query_literal(parent_id)
        q = (
            f"name = '{escaped_name}' and '{escaped_parent}' in parents "
            f"and mimeType = '{GOOGLE_DRIVE_FOLDER_MIME}' and trashed = false"
        )
        qs = urllib.parse.urlencode(
            {
                "q": q,
                "spaces": "drive",
                "pageSize": "1",
                "fields": "files(id,name,trashed)",
            }
        )
        resp = self._request("GET", f"{GOOGLE_DRIVE_API_BASE}/files?{qs}")
        if resp.status != 200:
            msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
            raise GDriveServiceError(f"Ошибка поиска папки в Google Drive: {msg}", status_code=502)
        data = resp.json()
        files = data.get("files") if isinstance(data, dict) else None
        if isinstance(files, list) and files:
            first = files[0]
            if isinstance(first, dict) and first.get("id") and not bool(first.get("trashed")):
                return str(first["id"])
        return None

    def _create_folder(self, folder_name: str, *, parent_id: Optional[str]) -> str:
        meta: dict[str, Any] = {
            "name": folder_name,
            "mimeType": GOOGLE_DRIVE_FOLDER_MIME,
        }
        if parent_id:
            meta["parents"] = [parent_id]
        body = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        resp = self._request(
            "POST",
            f"{GOOGLE_DRIVE_API_BASE}/files?fields=id,name,parents",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            body=body,
        )
        if resp.status not in (200, 201):
            msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
            raise GDriveServiceError(f"Ошибка создания папки '{folder_name}' в Google Drive: {msg}", status_code=502)
        data = resp.json()
        fid = str((data or {}).get("id") or "").strip() if isinstance(data, dict) else ""
        if not fid:
            raise GDriveServiceError(f"Google Drive не вернул id новой папки '{folder_name}'", status_code=502)
        return fid

    def ensure_folder_path(self, root_folder_id: str, folder_segments: list[str]) -> str:
        parent_id = root_folder_id
        for seg in folder_segments:
            clean_seg = str(seg or "").strip()
            if not clean_seg:
                continue
            cache_key = (parent_id, clean_seg)
            with self._folder_lock:
                cached = self._folder_cache.get(cache_key)
                if cached:
                    parent_id = cached
                    continue
                seg_lock = self._folder_inflight_locks.get(cache_key)
                if seg_lock is None:
                    seg_lock = threading.Lock()
                    self._folder_inflight_locks[cache_key] = seg_lock
            with seg_lock:
                with self._folder_lock:
                    cached = self._folder_cache.get(cache_key)
                if cached:
                    parent_id = cached
                    continue
                found = self._find_child_folder(parent_id, clean_seg)
                fid = found or self._create_folder(clean_seg, parent_id=parent_id)
                with self._folder_lock:
                    self._folder_cache[cache_key] = fid
                parent_id = fid
        return parent_id

    def upload_new_file(
        self,
        *,
        name: str,
        parent_id: str,
        content: bytes,
        mime_type: str,
    ) -> str:
        boundary = f"agy_hub_boundary_{secrets.token_hex(12)}"
        metadata_json = json.dumps(
            {"name": name, "parents": [parent_id]},
            ensure_ascii=False,
        ).encode("utf-8")
        multipart_body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + metadata_json + (
            f"\r\n--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

        resp = self._request(
            "POST",
            f"{GOOGLE_DRIVE_UPLOAD_BASE}/files?uploadType=multipart&fields=id,parents",
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            body=multipart_body,
            timeout=120.0,
        )
        if resp.status not in (200, 201):
            msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
            raise GDriveServiceError(f"Ошибка загрузки файла '{name}' на Google Диск: {msg}", status_code=502)
        data = resp.json()
        fid = str((data or {}).get("id") or "").strip() if isinstance(data, dict) else ""
        if not fid:
            raise GDriveServiceError(f"Google Drive не вернул id загруженного файла '{name}'", status_code=502)
        return fid

    def update_file_media(
        self,
        *,
        file_id: str,
        content: bytes,
        mime_type: str,
    ) -> Optional[str]:
        url = f"{GOOGLE_DRIVE_UPLOAD_BASE}/files/{urllib.parse.quote(file_id, safe='')}?uploadType=media&fields=id"
        resp = self._request(
            "PATCH",
            url,
            headers={"Content-Type": mime_type},
            body=content,
            timeout=120.0,
        )
        if resp.status == 404:
            return None
        if resp.status not in (200, 201):
            msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
            raise GDriveServiceError(f"Ошибка обновления файла '{file_id}' на Google Диске: {msg}", status_code=502)
        data = resp.json()
        fid = str((data or {}).get("id") or file_id).strip() if isinstance(data, dict) else file_id
        return fid or file_id

    def trash_file(self, file_id: str) -> bool:
        """Move file to Trash (`PATCH {"trashed": true}`). Never calls permanent `DELETE`."""
        url = f"{GOOGLE_DRIVE_API_BASE}/files/{urllib.parse.quote(file_id, safe='')}?fields=id,trashed"
        body = json.dumps({"trashed": True}).encode("utf-8")
        resp = self._request(
            "PATCH",
            url,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            body=body,
        )
        if resp.status in (200, 204, 404):
            return True
        msg = _extract_google_error_message(resp, f"HTTP {resp.status}")
        raise GDriveServiceError(f"Ошибка перемещения файла '{file_id}' в Корзину: {msg}", status_code=502)


# ---------------------------------------------------------------------------
# Dry-Run Preview & Full Unidirectional Sync Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class _DesiredSyncItem:
    logical_path: str
    item_kind: str
    sha256: str
    size_bytes: int
    mime_type: str
    content_bytes: Optional[bytes] = None
    content_loader: Optional[Callable[[], bytes]] = None

    def read_bytes(self) -> bytes:
        if self.content_bytes is not None:
            return self.content_bytes
        if self.content_loader is not None:
            return self.content_loader()
        return b""


def _sha256_file_streaming(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def _collect_projects_and_chats(
    user: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[ProjectScanResult], dict[str, list[dict[str, Any]]]]:
    """Collect regular chats, included projects, their local scans, and per-project chats."""
    user_id = str(user.get("id") or "admin")
    is_admin = user.get("role") == "admin"
    shared_projs = await database.list_shared_projects(for_user_id=user_id, is_admin=is_admin)
    all_projects = await asyncio.to_thread(brain_reader.list_projects_for_user, user, shared_projs)

    excluded_low = {str(p).strip().lower() for p in (cfg.get("excluded_projects") or []) if str(p).strip()}
    sync_self_hub = bool(cfg.get("sync_self_hub", True))

    included_projects: list[dict[str, Any]] = []
    for proj in all_projects:
        pname = str(proj.get("name") or "").strip()
        if not pname:
            continue
        if bool(proj.get("is_self_hub")) and not sync_self_hub:
            continue
        if pname.lower() in excluded_low:
            continue
        included_projects.append(proj)

    regular_chats = await database.list_chats(
        kind="chat",
        owner_id=user_id,
        include_context_pct=False,
        include_archived=True,
    )

    shared_names = [str(sp.get("name") or "") for sp in shared_projs if str(sp.get("name") or "").strip()]
    all_project_chats = await database.list_chats(
        kind="project",
        owner_id=user_id,
        shared_project_names=shared_names,
        include_context_pct=False,
        include_archived=True,
    )
    chats_by_project_low: dict[str, list[dict[str, Any]]] = {}
    for ch in all_project_chats:
        pl = str(ch.get("project_name") or "").strip().lower()
        if pl:
            chats_by_project_low.setdefault(pl, []).append(ch)

    def _scan_all() -> list[ProjectScanResult]:
        scans: list[ProjectScanResult] = []
        for proj in included_projects:
            pname = str(proj["name"])
            raw_path = str(proj.get("path") or "").strip()
            p_dir = Path(raw_path).resolve() if raw_path else None
            if p_dir is None or not p_dir.is_dir():
                continue
            scans.append(
                scan_project_for_gdrive(
                    p_dir,
                    project_name=pname,
                    is_self_hub=bool(proj.get("is_self_hub")),
                )
            )
        return scans

    scans = await asyncio.to_thread(_scan_all)
    return regular_chats, included_projects, shared_projs, scans, chats_by_project_low


async def preview_user_gdrive_sync(user: dict[str, Any]) -> dict[str, Any]:
    """Fast local dry-run scan without any network calls."""
    user_id = str(user.get("id") or "admin")
    cfg = await database.get_gdrive_config(user_id, include_secrets=False)
    regular_chats, included_projects, _shared_projs, scans, chats_by_proj = await _collect_projects_and_chats(
        user, cfg
    )

    scan_by_low = {s.project_name.lower(): s for s in scans}
    projects_summary: list[dict[str, Any]] = []
    total_code_files = 0
    total_code_bytes = 0
    total_vault_candidates = 0
    total_vault_bytes = 0
    total_project_chats = 0
    skipped_large_files: list[dict[str, Any]] = []
    skipped_dump_dirs: list[dict[str, Any]] = []

    for proj in included_projects:
        pname = str(proj["name"])
        plow = pname.lower()
        sc = scan_by_low.get(plow)
        p_chats = chats_by_proj.get(plow, [])
        p_code_files = len(sc.code_files) if sc else 0
        p_code_bytes = sum(f.size_bytes for f in sc.code_files) if sc else 0
        p_vault_count = len(sc.vault_candidates) if sc else 0
        p_vault_bytes = sum(f.size_bytes for f in sc.vault_candidates) if sc else 0
        p_large = sc.skipped_large_files if sc else []
        p_dumps = sc.skipped_dump_dirs if sc else []

        total_code_files += p_code_files
        total_code_bytes += p_code_bytes
        total_vault_candidates += p_vault_count
        total_vault_bytes += p_vault_bytes
        total_project_chats += len(p_chats)
        skipped_large_files.extend(p_large)
        skipped_dump_dirs.extend(p_dumps)

        projects_summary.append(
            {
                "name": pname,
                "is_self_hub": bool(proj.get("is_self_hub")),
                "is_shared": bool(proj.get("is_shared")),
                "chats_count": len(p_chats),
                "code_files_count": p_code_files,
                "code_bytes": p_code_bytes,
                "vault_candidates_count": p_vault_count,
                "skipped_large_count": len(p_large),
                "skipped_dump_dirs_count": len(p_dumps),
            }
        )

    skipped_objects = [*skipped_dump_dirs[:100], *skipped_large_files[:100]]
    return {
        "regular_chats_count": len(regular_chats),
        "project_chats_count": total_project_chats,
        "projects_count": len(included_projects),
        "projects": projects_summary,
        "code_files_count": total_code_files,
        "code_bytes": total_code_bytes,
        "vault_candidates_count": total_vault_candidates,
        "vault_candidates_bytes": total_vault_bytes,
        "include_env_vault": bool(cfg.get("include_env_vault")),
        "has_vault_passphrase": bool(cfg.get("has_vault_passphrase")),
        "sync_self_hub": bool(cfg.get("sync_self_hub", True)),
        "excluded_projects": list(cfg.get("excluded_projects") or []),
        "skipped_large_files": skipped_large_files[:100],
        "skipped_large_files_total": len(skipped_large_files),
        "skipped_dump_dirs": skipped_dump_dirs[:100],
        "skipped_dump_dirs_total": len(skipped_dump_dirs),
        "skipped_objects": skipped_objects,
    }


async def sync_user_to_gdrive(
    user: dict[str, Any],
    *,
    transport: Optional[TransportCallable] = None,
    trigger: str = "manual",
    ws_notifier: Optional[Callable[[dict[str, Any]], Any]] = None,
    concurrency: int = SYNC_UPLOAD_CONCURRENCY,
    progress_throttle_sec: float = SYNC_PROGRESS_THROTTLE_SEC,
) -> dict[str, Any]:
    """Run incremental unidirectional sync to Google Drive (dialogs, project Hub/code, sanitized SQLite, optional vault)."""
    user_id = str(user.get("id") or "admin")
    cfg = await database.get_gdrive_config(user_id, include_secrets=True)
    if not cfg.get("connected"):
        raise GDriveAuthError("Google Диск не подключён. Выполните OAuth-авторизацию.", status_code=400)

    eff_transport = _resolve_transport(transport)
    access_token = await ensure_valid_access_token(user_id, cfg, transport=eff_transport)

    loop = asyncio.get_running_loop()

    def _sync_refresh_cb() -> str:
        fut = asyncio.run_coroutine_threadsafe(
            ensure_valid_access_token(user_id, cfg, force_refresh=True, transport=eff_transport),
            loop,
        )
        return fut.result(timeout=35.0)

    client = GoogleDriveClient(
        access_token,
        transport=eff_transport,
        refresh_token_fn=_sync_refresh_cb,
    )

    dirty_before = get_gdrive_dirty_state(user_id)
    dirty_snapshot_mono = float(dirty_before["last_changed_mono"]) if dirty_before else None

    eff_concurrency = max(1, min(16, int(concurrency or SYNC_UPLOAD_CONCURRENCY)))
    eff_throttle = max(0.0, float(progress_throttle_sec))
    sync_start_mono = time.monotonic()
    last_ws_emit_mono = 0.0
    last_ws_phase = ""

    uploaded_count = 0
    updated_count = 0
    skipped_unchanged_count = 0
    uploaded_bytes = 0

    _mark_user_sync_active(user_id, True)
    started_at = datetime.now(timezone.utc).isoformat()
    pub_cfg = _public_gdrive_cfg_snapshot(cfg)

    async def _publish_progress(
        phase: str,
        *,
        processed: int,
        total: int,
        current_item: str = "",
        force: bool = False,
        phase_start_mono: Optional[float] = None,
        calc_eta: bool = False,
    ) -> None:
        nonlocal last_ws_emit_mono, last_ws_phase
        now_m = time.monotonic()
        elapsed_sec = round(max(0.0, now_m - sync_start_mono), 1)
        safe_total = max(0, int(total))
        safe_processed = max(0, min(int(processed), safe_total)) if safe_total > 0 else max(0, int(processed))
        percent = (
            max(0, min(100, int(round((safe_processed / safe_total) * 100))))
            if safe_total > 0
            else 0
        )
        eta_sec: Optional[int] = None
        if calc_eta and safe_total > 0 and safe_processed > 0:
            if safe_processed >= safe_total:
                eta_sec = 0
            else:
                ref_start = phase_start_mono if phase_start_mono is not None else sync_start_mono
                dt = max(0.001, now_m - ref_start)
                rate = safe_processed / dt
                if rate > 0:
                    eta_sec = max(1, int(round((safe_total - safe_processed) / rate)))

        snap = {
            "phase": str(phase),
            "processed": safe_processed,
            "total": safe_total,
            "uploaded": int(uploaded_count),
            "updated": int(updated_count),
            "skipped_unchanged": int(skipped_unchanged_count),
            "percent": percent,
            "current_item": str(current_item or "")[:240],
            "elapsed_sec": elapsed_sec,
            "eta_sec": eta_sec,
        }
        _set_user_sync_progress(user_id, snap)

        phase_changed = phase != last_ws_phase
        if force or phase_changed or (now_m - last_ws_emit_mono) >= eff_throttle:
            last_ws_emit_mono = now_m
            last_ws_phase = phase
            await _emit_gdrive_ws_status(
                {
                    "type": "gdrive_sync_status",
                    "user_id": user_id,
                    "state": "running",
                    "sync_in_progress": True,
                    "trigger": str(trigger or "manual"),
                    "started_at": started_at,
                    "progress": snap,
                    "status": {**pub_cfg, "sync_in_progress": True, "sync_progress": snap},
                },
                ws_notifier=ws_notifier,
            )

    await _publish_progress(
        "scanning",
        processed=0,
        total=0,
        current_item="Сканирование диалогов и проектов…",
        force=True,
    )
    try:
        # 1. Collect local mirror dataset
        regular_chats, included_projects, _shared_projs, scans, chats_by_proj = await _collect_projects_and_chats(
            user, cfg
        )
        scan_by_low = {s.project_name.lower(): s for s in scans}
        existing_items = await database.list_gdrive_sync_items(user_id)

        desired_items: dict[str, _DesiredSyncItem] = {}

        # 1a. Regular dialogs: `Диалоги/<Папка_или_Без_папки>/<Безопасное_имя>__<id8>.md` (archived -> `Диалоги/_Архив/`)
        for chat in regular_chats:
            cid = str(chat["id"])
            full_chat = await database.get_chat(cid) or chat
            msgs = await database.get_messages(cid, include_thinking=False)
            md_bytes = render_chat_markdown(full_chat, msgs).encode("utf-8")
            sha = hashlib.sha256(md_bytes).hexdigest()
            safe_title = _safe_drive_segment(str(full_chat.get("title") or "Диалог"), fallback="Диалог")
            id8 = _chat_short_id(cid)
            if bool(full_chat.get("is_archived")):
                folder_seg = "_Архив"
            else:
                raw_folder = str(full_chat.get("folder") or "").strip()
                folder_seg = _safe_drive_segment(raw_folder, fallback="Без_папки") if raw_folder else "Без_папки"
            logical_path = f"Диалоги/{folder_seg}/{safe_title}__{id8}.md"
            desired_items[logical_path] = _DesiredSyncItem(
                logical_path=logical_path,
                item_kind="chat_md",
                sha256=sha,
                size_bytes=len(md_bytes),
                mime_type="text/markdown",
                content_bytes=md_bytes,
            )

        # 1b. Projects: `_HUB/PASSPORT.md`, `_HUB/chats/*.md`, `code/<rel_path>`
        all_vault_candidates: list[ScannedSecretCandidate] = []
        manifest_projects: list[dict[str, Any]] = []
        total_project_chats_count = 0

        for proj in sorted(included_projects, key=lambda p: str(p.get("name") or "").lower()):
            pname = str(proj["name"])
            plow = pname.lower()
            is_shared = bool(proj.get("is_shared"))
            safe_proj = _safe_drive_segment(pname, fallback="Project")

            # PASSPORT.md
            scope_key = database.build_project_memory_scope_key(
                pname,
                owner_id=user_id,
                is_shared=is_shared,
            )
            memory = await database.get_project_memory(scope_key, project_name=pname, owner_id=user_id)
            passport_bytes = render_project_passport_markdown(pname, memory, is_shared=is_shared).encode("utf-8")
            passport_path = f"Проекты/{safe_proj}/_HUB/PASSPORT.md"
            desired_items[passport_path] = _DesiredSyncItem(
                logical_path=passport_path,
                item_kind="project_passport",
                sha256=hashlib.sha256(passport_bytes).hexdigest(),
                size_bytes=len(passport_bytes),
                mime_type="text/markdown",
                content_bytes=passport_bytes,
            )

            # Project chats
            p_chats = chats_by_proj.get(plow, [])
            total_project_chats_count += len(p_chats)
            for pch in p_chats:
                pcid = str(pch["id"])
                full_pch = await database.get_chat(pcid) or pch
                pmsgs = await database.get_messages(pcid, include_thinking=False)
                pmd_bytes = render_chat_markdown(full_pch, pmsgs).encode("utf-8")
                psafe_title = _safe_drive_segment(str(full_pch.get("title") or "Диалог"), fallback="Диалог")
                pid8 = _chat_short_id(pcid)
                pchat_path = f"Проекты/{safe_proj}/_HUB/chats/{psafe_title}__{pid8}.md"
                desired_items[pchat_path] = _DesiredSyncItem(
                    logical_path=pchat_path,
                    item_kind="project_chat_md",
                    sha256=hashlib.sha256(pmd_bytes).hexdigest(),
                    size_bytes=len(pmd_bytes),
                    mime_type="text/markdown",
                    content_bytes=pmd_bytes,
                )

            # Project clean code files
            sc = scan_by_low.get(plow)
            code_count = 0
            code_bytes_total = 0
            if sc is not None:
                all_vault_candidates.extend(sc.vault_candidates)
                for cf in sc.code_files:
                    file_sha = await asyncio.to_thread(_sha256_file_streaming, cf.abs_path)
                    code_logical_path = f"Проекты/{safe_proj}/code/{cf.rel_path}"
                    desired_items[code_logical_path] = _DesiredSyncItem(
                        logical_path=code_logical_path,
                        item_kind="project_code",
                        sha256=file_sha,
                        size_bytes=cf.size_bytes,
                        mime_type=_guess_mime_type(cf.abs_path.name),
                        content_loader=cf.abs_path.read_bytes,
                    )
                    code_count += 1
                    code_bytes_total += cf.size_bytes

            manifest_projects.append(
                {
                    "name": pname,
                    "drive_folder_name": safe_proj,
                    "is_shared": is_shared,
                    "is_self_hub": bool(proj.get("is_self_hub")),
                    "chats_count": len(p_chats),
                    "code_files_count": code_count,
                    "code_bytes": code_bytes_total,
                }
            )

        # 1c. Sanitized hot SQLite backup (`_restore_vault/hub_dialogs_and_memory.sqlite.gz`)
        db_gz_bytes, db_stats = await asyncio.to_thread(build_sanitized_sqlite_gz_with_stats)
        db_gz_path = "_restore_vault/hub_dialogs_and_memory.sqlite.gz"
        desired_items[db_gz_path] = _DesiredSyncItem(
            logical_path=db_gz_path,
            item_kind="vault_db",
            sha256=hashlib.sha256(db_gz_bytes).hexdigest(),
            size_bytes=len(db_gz_bytes),
            mime_type="application/gzip",
            content_bytes=db_gz_bytes,
        )

        # 1d. Optional encrypted secrets vault (`_restore_vault/secrets.vault.enc`)
        include_env_vault = bool(cfg.get("include_env_vault"))
        vault_passphrase = str(cfg.get("vault_passphrase") or "")
        vault_uploaded_active = False
        vault_files_count = 0
        if include_env_vault and vault_passphrase:
            vault_payload = await asyncio.to_thread(build_secrets_vault_payload, all_vault_candidates)
            vault_files_count = int(vault_payload.get("file_count") or 0)
            vault_fp = compute_vault_logical_fingerprint(vault_payload, vault_passphrase)
            vault_path = "_restore_vault/secrets.vault.enc"
            prev_vault = existing_items.get(vault_path)
            if prev_vault and prev_vault.get("sha256") == vault_fp and prev_vault.get("drive_file_id"):
                # Unchanged secrets & passphrase: skip expensive 600k PBKDF2 and upload
                desired_items[vault_path] = _DesiredSyncItem(
                    logical_path=vault_path,
                    item_kind="vault_secrets",
                    sha256=vault_fp,
                    size_bytes=int(prev_vault.get("size_bytes") or 0),
                    mime_type="application/json",
                    content_bytes=None,
                )
            else:
                vault_bytes = await asyncio.to_thread(
                    encrypt_secrets_vault, vault_payload, vault_passphrase
                )
                desired_items[vault_path] = _DesiredSyncItem(
                    logical_path=vault_path,
                    item_kind="vault_secrets",
                    sha256=vault_fp,
                    size_bytes=len(vault_bytes),
                    mime_type="application/json",
                    content_bytes=vault_bytes,
                )
            vault_uploaded_active = True

        # 1e. Deterministic Sync Manifest (`_restore_vault/sync_manifest.json`)
        manifest_obj = {
            "format": SYNC_MANIFEST_FORMAT,
            "root_folder_name": str(cfg.get("root_folder_name") or database.DEFAULT_GDRIVE_ROOT_FOLDER_NAME),
            "sync_self_hub": bool(cfg.get("sync_self_hub", True)),
            "include_env_vault": vault_uploaded_active,
            "vault_files_count": vault_files_count,
            "regular_chats_count": len(regular_chats),
            "project_chats_count": total_project_chats_count,
            "chats_count": int(db_stats.get("chats_count") or 0),
            "messages_count": int(db_stats.get("messages_count") or 0),
            "folders_count": int(db_stats.get("folders_count") or 0),
            "passports_count": int(db_stats.get("passports_count") or 0),
            "projects_count": len(manifest_projects),
            "projects": manifest_projects,
            "items_count": len(desired_items) + 1,
            "items_hashes": {
                lp: item.sha256
                for lp, item in sorted(desired_items.items(), key=lambda kv: kv[0])
            },
        }
        manifest_bytes = json.dumps(manifest_obj, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        manifest_path = "_restore_vault/sync_manifest.json"
        desired_items[manifest_path] = _DesiredSyncItem(
            logical_path=manifest_path,
            item_kind="vault_manifest",
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            size_bytes=len(manifest_bytes),
            mime_type="application/json",
            content_bytes=manifest_bytes,
        )

        # 2. Ensure Google Drive root folder exists (recreate if deleted/trashed)
        old_root_folder_id = str(cfg.get("root_folder_id") or "").strip()
        root_folder_name = str(cfg.get("root_folder_name") or database.DEFAULT_GDRIVE_ROOT_FOLDER_NAME)
        root_folder_id = await asyncio.to_thread(
            client.ensure_root_folder,
            old_root_folder_id,
            root_folder_name,
        )
        if root_folder_id != old_root_folder_id:
            await database.save_gdrive_config(user_id, root_folder_id=root_folder_id)
            if old_root_folder_id:
                # Root folder was recreated after deletion/trashing: rebuild clean index in the new root
                await database.clear_gdrive_sync_items(user_id)
                existing_items = {}

        # 3. Partition unchanged vs to-transfer items, pre-warm folders, and run concurrent uploads
        to_transfer: list[tuple[str, _DesiredSyncItem, Optional[dict[str, Any]]]] = []
        for logical_path in sorted(desired_items.keys()):
            item = desired_items[logical_path]
            prev = existing_items.get(logical_path)
            if (
                prev is not None
                and prev.get("sha256") == item.sha256
                and str(prev.get("drive_file_id") or "").strip()
            ):
                skipped_unchanged_count += 1
                continue
            to_transfer.append((logical_path, item, prev))

        # 3a. Pre-create unique folder chains by depth level so parallel workers never race on folders
        folder_prefixes_by_depth: dict[int, set[tuple[str, ...]]] = {}
        for lp, _it, _pr in to_transfer:
            segs = [seg for seg in lp.split("/")[:-1] if seg]
            for depth in range(1, len(segs) + 1):
                folder_prefixes_by_depth.setdefault(depth, set()).add(tuple(segs[:depth]))

        total_folders_to_ensure = sum(len(s) for s in folder_prefixes_by_depth.values())
        if total_folders_to_ensure > 0:
            folders_start_mono = time.monotonic()
            ensured_folders_count = 0
            await _publish_progress(
                "folders",
                processed=0,
                total=total_folders_to_ensure,
                current_item="Подготовка структуры папок…",
                force=True,
                phase_start_mono=folders_start_mono,
                calc_eta=True,
            )
            folder_sem = asyncio.Semaphore(eff_concurrency)
            folder_error: Optional[BaseException] = None

            async def _ensure_prefix(prefix_segs: tuple[str, ...]) -> None:
                nonlocal ensured_folders_count, folder_error
                async with folder_sem:
                    if folder_error is not None:
                        return
                    try:
                        await asyncio.to_thread(client.ensure_folder_path, root_folder_id, list(prefix_segs))
                    except BaseException as exc:
                        if folder_error is None:
                            folder_error = exc
                        return
                    ensured_folders_count += 1
                    await _publish_progress(
                        "folders",
                        processed=ensured_folders_count,
                        total=total_folders_to_ensure,
                        current_item="/".join(prefix_segs),
                        force=False,
                        phase_start_mono=folders_start_mono,
                        calc_eta=True,
                    )

            for depth in sorted(folder_prefixes_by_depth.keys()):
                level_prefixes = sorted(folder_prefixes_by_depth[depth])
                await asyncio.gather(*(_ensure_prefix(p) for p in level_prefixes))
                if folder_error is not None:
                    raise folder_error

        # 3b. Concurrent upload / update with bounded worker pool (`SYNC_UPLOAD_CONCURRENCY = 6`)
        upload_total = len(to_transfer)
        if upload_total > 0:
            upload_start_mono = time.monotonic()
            processed_uploads = 0
            await _publish_progress(
                "uploading",
                processed=0,
                total=upload_total,
                current_item=to_transfer[0][0],
                force=True,
                phase_start_mono=upload_start_mono,
                calc_eta=True,
            )

            upload_sem = asyncio.Semaphore(eff_concurrency)
            db_write_lock = asyncio.Lock()
            transfer_error: Optional[BaseException] = None

            async def _transfer_one(
                logical_path: str,
                item: _DesiredSyncItem,
                prev: Optional[dict[str, Any]],
            ) -> None:
                nonlocal uploaded_count, updated_count, uploaded_bytes, processed_uploads, transfer_error
                async with upload_sem:
                    if transfer_error is not None:
                        return
                    try:
                        parts = [seg for seg in logical_path.split("/") if seg]
                        folder_segs = parts[:-1]
                        filename = parts[-1]
                        raw_bytes = await asyncio.to_thread(item.read_bytes)

                        prev_fid = str(prev.get("drive_file_id") or "").strip() if prev else ""
                        prev_parent_id = str(prev.get("drive_parent_id") or "").strip() if prev else ""

                        drive_file_id: Optional[str] = None
                        drive_parent_id = prev_parent_id
                        was_updated = False
                        if prev_fid:
                            drive_file_id = await asyncio.to_thread(
                                client.update_file_media,
                                file_id=prev_fid,
                                content=raw_bytes,
                                mime_type=item.mime_type,
                            )
                            if drive_file_id:
                                was_updated = True
                                if not drive_parent_id:
                                    drive_parent_id = await asyncio.to_thread(
                                        client.ensure_folder_path, root_folder_id, folder_segs
                                    )

                        if not drive_file_id:
                            drive_parent_id = await asyncio.to_thread(
                                client.ensure_folder_path, root_folder_id, folder_segs
                            )
                            drive_file_id = await asyncio.to_thread(
                                client.upload_new_file,
                                name=filename,
                                parent_id=drive_parent_id,
                                content=raw_bytes,
                                mime_type=item.mime_type,
                            )

                        async with db_write_lock:
                            await database.upsert_gdrive_sync_item(
                                user_id,
                                logical_path=logical_path,
                                item_kind=item.item_kind,
                                sha256=item.sha256,
                                size_bytes=len(raw_bytes),
                                drive_file_id=drive_file_id,
                                drive_parent_id=drive_parent_id,
                            )
                            if was_updated:
                                updated_count += 1
                            else:
                                uploaded_count += 1
                            uploaded_bytes += len(raw_bytes)
                            processed_uploads += 1

                        await _publish_progress(
                            "uploading",
                            processed=processed_uploads,
                            total=upload_total,
                            current_item=logical_path,
                            force=False,
                            phase_start_mono=upload_start_mono,
                            calc_eta=True,
                        )
                    except BaseException as exc:
                        if transfer_error is None:
                            transfer_error = exc

            await asyncio.gather(*(_transfer_one(lp, it, pr) for lp, it, pr in to_transfer))
            if transfer_error is not None:
                raise transfer_error

        # 4. Trash sweep for items removed locally (including `secrets.vault.enc` when checkbox disabled)
        stale_paths = sorted(lp for lp in existing_items.keys() if lp not in desired_items)
        trashed_count = 0
        if stale_paths:
            trash_start_mono = time.monotonic()
            await _publish_progress(
                "trashing",
                processed=0,
                total=len(stale_paths),
                current_item=stale_paths[0],
                force=True,
                phase_start_mono=trash_start_mono,
                calc_eta=True,
            )
            for stale_lp in stale_paths:
                stale_row = existing_items[stale_lp]
                stale_fid = str(stale_row.get("drive_file_id") or "").strip()
                if stale_fid:
                    await asyncio.to_thread(client.trash_file, stale_fid)
                trashed_count += 1
                await _publish_progress(
                    "trashing",
                    processed=trashed_count,
                    total=len(stale_paths),
                    current_item=stale_lp,
                    force=False,
                    phase_start_mono=trash_start_mono,
                    calc_eta=True,
                )
            await database.delete_gdrive_sync_items(user_id, stale_paths)

        finished_at = datetime.now(timezone.utc).isoformat()
        summary = {
            "started_at": started_at,
            "finished_at": finished_at,
            "trigger": str(trigger or "manual"),
            "root_folder_id": root_folder_id,
            "root_folder_name": root_folder_name,
            "total_items": len(desired_items),
            "uploaded": uploaded_count,
            "updated": updated_count,
            "skipped_unchanged": skipped_unchanged_count,
            "trashed": trashed_count,
            "uploaded_bytes": uploaded_bytes,
            "regular_chats_count": len(regular_chats),
            "projects_count": len(manifest_projects),
            "vault_included": vault_uploaded_active,
        }
        updated_status = await database.update_gdrive_sync_status(
            user_id,
            status="ok",
            error="",
            summary=summary,
            root_folder_id=root_folder_id,
            last_sync_at=finished_at,
        )
        if dirty_snapshot_mono is not None:
            clear_gdrive_dirty_state(user_id, up_to_mono=dirty_snapshot_mono)
        with _AUTO_SYNC_STATE_LOCK:
            _LAST_AUTO_SYNC_ERROR_MONO.pop(user_id, None)
        _mark_user_sync_active(user_id, False)
        await _emit_gdrive_ws_status(
            {
                "type": "gdrive_sync_status",
                "user_id": user_id,
                "state": "ok",
                "sync_in_progress": False,
                "trigger": str(trigger or "manual"),
                "progress": None,
                "summary": summary,
                "status": {**updated_status, "sync_in_progress": False, "sync_progress": None},
            },
            ws_notifier=ws_notifier,
        )
        return {
            "summary": summary,
            "status": updated_status,
        }
    except Exception as exc:
        err_text = str(exc).strip() or exc.__class__.__name__
        err_status: Optional[dict[str, Any]] = None
        try:
            err_status = await database.update_gdrive_sync_status(
                user_id,
                status="error",
                error=err_text,
                last_sync_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception:
            err_status = None
        _mark_user_sync_active(user_id, False)
        await _emit_gdrive_ws_status(
            {
                "type": "gdrive_sync_status",
                "user_id": user_id,
                "state": "error",
                "sync_in_progress": False,
                "trigger": str(trigger or "manual"),
                "progress": None,
                "error": err_text,
                "status": {
                    **(err_status or _public_gdrive_cfg_snapshot(cfg)),
                    "sync_in_progress": False,
                    "sync_progress": None,
                },
            },
            ws_notifier=ws_notifier,
        )
        raise
    finally:
        _mark_user_sync_active(user_id, False)


# ---------------------------------------------------------------------------
# Background Event-Driven Auto-Sync Tick (0% CPU when idle, v152)
# ---------------------------------------------------------------------------


def _default_has_active_ai_runs() -> bool:
    try:
        from . import agy_engine

        return bool(agy_engine.ACTIVE_RUNS)
    except Exception:
        return False


def _parse_iso_utc(raw: str) -> Optional[datetime]:
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


async def run_gdrive_auto_sync_tick(
    *,
    quiet_period_sec: float = AUTO_SYNC_QUIET_PERIOD_SEC,
    safety_interval_sec: float = AUTO_SYNC_SAFETY_INTERVAL_SEC,
    now_mono: Optional[float] = None,
    now_utc: Optional[datetime] = None,
    has_active_runs_fn: Optional[Callable[[], bool]] = None,
    transport: Optional[TransportCallable] = None,
    ws_notifier: Optional[Callable[[dict[str, Any]], Any]] = None,
) -> dict[str, Any]:
    """Single deterministic check step for the quiet-period & 4-hour periodic auto-sync scheduler."""
    active_checker = has_active_runs_fn if has_active_runs_fn is not None else _default_has_active_ai_runs
    if active_checker():
        return {
            "ran": False,
            "skipped_reason": "active_runs",
            "synced_users": [],
            "skipped_users": {},
            "results": [],
        }

    enabled_configs = await database.list_enabled_gdrive_configs(include_secrets=False)
    if not enabled_configs:
        return {
            "ran": False,
            "skipped_reason": "no_enabled_configs",
            "synced_users": [],
            "skipped_users": {},
            "results": [],
        }

    cur_mono = time.monotonic() if now_mono is None else float(now_mono)
    cur_utc = (now_utc if now_utc is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)

    synced_users: list[str] = []
    skipped_users: dict[str, str] = {}
    results: list[dict[str, Any]] = []

    for cfg in enabled_configs:
        uid = str(cfg.get("user_id") or "").strip()
        if not uid or not cfg.get("is_enabled") or not cfg.get("connected"):
            continue

        with _AUTO_SYNC_STATE_LOCK:
            last_err_mono = _LAST_AUTO_SYNC_ERROR_MONO.get(uid)
        if last_err_mono is not None and (cur_mono - last_err_mono) < AUTO_SYNC_ERROR_BACKOFF_SEC:
            skipped_users[uid] = "error_backoff"
            continue

        dirty = get_gdrive_dirty_state(uid)
        trigger: Optional[str] = None
        if dirty is not None:
            elapsed = cur_mono - float(dirty["last_changed_mono"])
            if elapsed >= float(quiet_period_sec):
                trigger = "auto_quiet"
            else:
                skipped_users[uid] = "quiet_period_pending"
                continue
        else:
            last_sync_dt = _parse_iso_utc(str(cfg.get("last_sync_at") or ""))
            if last_sync_dt is None or (cur_utc - last_sync_dt).total_seconds() >= float(safety_interval_sec):
                trigger = "auto_periodic"
            else:
                skipped_users[uid] = "idle"
                continue

        lock = get_user_sync_lock(uid)
        if lock.locked() or is_user_sync_in_progress(uid):
            skipped_users[uid] = "sync_in_progress"
            continue

        async with lock:
            # Re-verify no AI generation started while waiting for lock/DB
            if active_checker():
                skipped_users[uid] = "active_runs"
                break
            user = await database.get_user_by_id(uid)
            if not user or str(user.get("role") or "").lower() != "admin":
                skipped_users[uid] = "not_admin"
                continue
            try:
                res = await sync_user_to_gdrive(
                    user,
                    transport=transport,
                    trigger=trigger,
                    ws_notifier=ws_notifier,
                )
                synced_users.append(uid)
                results.append({"user_id": uid, "trigger": trigger, **res})
            except Exception as exc:
                with _AUTO_SYNC_STATE_LOCK:
                    _LAST_AUTO_SYNC_ERROR_MONO[uid] = cur_mono
                skipped_users[uid] = f"error:{exc.__class__.__name__}"

    return {
        "ran": bool(synced_users),
        "skipped_reason": "" if synced_users else (next(iter(skipped_users.values()), "idle")),
        "synced_users": synced_users,
        "skipped_users": skipped_users,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Non-Destructive Restore Catalog & Safe Restore Executor (v152)
# ---------------------------------------------------------------------------


def _build_authorized_drive_client(
    user_id: str,
    cfg: dict[str, Any],
    access_token: str,
    eff_transport: TransportCallable,
) -> GoogleDriveClient:
    loop = asyncio.get_running_loop()

    def _sync_refresh_cb() -> str:
        fut = asyncio.run_coroutine_threadsafe(
            ensure_valid_access_token(user_id, cfg, force_refresh=True, transport=eff_transport),
            loop,
        )
        return fut.result(timeout=35.0)

    return GoogleDriveClient(
        access_token,
        transport=eff_transport,
        refresh_token_fn=_sync_refresh_cb,
    )


def _validate_restore_project_name(raw_name: str) -> Optional[str]:
    clean = str(raw_name or "").strip()
    if not clean or len(clean) > 120:
        return None
    if "\x00" in clean or "/" in clean or "\\" in clean or ".." in clean or ":" in clean:
        return None
    if clean in (".", "..") or clean.startswith(".") or clean.startswith("_"):
        return None
    stem_upper = clean.split(".")[0].upper()
    if stem_upper in _WIN_RESERVED_NAMES:
        return None
    return clean


def _validate_restore_rel_path(
    raw_rel: str,
    *,
    allow_secrets: bool = False,
    is_self_hub: bool = False,
) -> Optional[str]:
    """Strictly validate a cloud relative path against traversal, absolute paths, `.git/`, and Hub secrets."""
    if not isinstance(raw_rel, str):
        return None
    if "\x00" in raw_rel:
        return None
    norm = raw_rel.strip()
    if not norm:
        return None
    if norm.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", norm):
        return None
    if "\\" in norm:
        return None
    parts = norm.split("/")
    if not parts:
        return None

    low_parts: list[str] = []
    for idx, seg in enumerate(parts):
        if not seg or seg in (".", "..") or ":" in seg or any(ord(c) < 32 for c in seg):
            return None
        stem_upper = seg.split(".")[0].upper()
        if stem_upper in _WIN_RESERVED_NAMES:
            return None
        seg_low = seg.lower()
        # Never allow `.git` or Hub runtime/database directories under any restore mode
        if seg_low == ".git":
            return None
        if idx < len(parts) - 1:
            if allow_secrets:
                if seg_low in ("data", "users", "shared_projects", ".venv", "venv", "node_modules"):
                    return None
            else:
                if is_blocked_project_dir_name(seg, depth=idx, is_self_hub=is_self_hub):
                    return None
                if is_secret_dir_name(seg):
                    return None
        low_parts.append(seg_low)

    filename = parts[-1]
    fname_low = low_parts[-1]
    rel_posix = "/".join(parts)
    rel_low = "/".join(low_parts)

    if rel_low in INTERNAL_AUTO_EXCLUDED_CODE_PATHS:
        return None
    if is_vault_excluded_secret_name(filename):
        return None
    if fnmatch.fnmatchcase(fname_low, "*.db*") or fnmatch.fnmatchcase(fname_low, "*.sqlite*"):
        return None
    if fname_low.startswith("hub_secret") or fname_low == "hub.db":
        return None

    if allow_secrets:
        in_secret_dir = any(p in SECRET_DIRS_EXACT for p in low_parts[:-1])
        if not (in_secret_dir or is_secret_file_name(filename)):
            return None
    else:
        if is_secret_file_name(filename):
            return None
        if is_self_hub and low_parts[0] in SELF_HUB_BLOCKED_TOP_DIRS:
            return None

    return rel_posix


def _is_safe_path_within_root(root_dir: Path, rel_posix: str) -> Optional[Path]:
    """Verify `root_dir / rel_posix` stays strictly inside `root_dir` and traverses zero symlinks/reparse points."""
    root_resolved = root_dir.resolve()
    if workspace_mcp._is_reparse_or_symlink(root_dir):
        return None
    cur = root_dir
    for seg in rel_posix.split("/"):
        cur = cur / seg
        if cur.exists() or cur.is_symlink():
            if workspace_mcp._is_reparse_or_symlink(cur):
                return None
    try:
        resolved_target = cur.resolve()
        resolved_target.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    return cur


def _resolve_local_project_dir_for_restore(
    project_name: str,
    *,
    user: dict[str, Any],
    shared_by_low: dict[str, dict[str, Any]],
) -> tuple[Path, bool, bool]:
    """Return `(target_dir, is_self_hub, is_shared)` bound to the current PC's paths."""
    if brain_reader.is_self_hub_project_name(project_name):
        return config.BASE_DIR.resolve(), True, False
    plow = project_name.lower()
    sp = shared_by_low.get(plow)
    if sp is not None:
        sp_name = str(sp.get("name") or project_name).strip() or project_name
        raw_sp_path = str(sp.get("path") or "").strip()
        try:
            validated = brain_reader.validate_shared_project_path(sp_name, existing_path=raw_sp_path)
            return validated.resolve(), False, True
        except ValueError:
            personal_cand = (brain_reader.get_user_personal_root(user) / sp_name).resolve()
            if personal_cand.is_dir():
                return personal_cand, False, True
            return (config.SHARED_PROJECTS_DIR / sp_name).resolve(), False, True
    personal_root = brain_reader.get_user_personal_root(user).resolve()
    return (personal_root / project_name).resolve(), False, False


def _is_passport_row_empty(row: dict[str, Any]) -> bool:
    overview = str(row.get("passport_overview") or "").strip()
    rules = str(row.get("passport_rules") or "").strip()
    file_map = str(row.get("passport_file_map") or "").strip()
    cl_raw = str(row.get("changelog_json") or "").strip()
    has_cl = bool(cl_raw and cl_raw != "[]")
    return not (overview or rules or file_map or has_cl)


async def get_gdrive_restore_catalog(
    user: dict[str, Any],
    *,
    transport: Optional[TransportCallable] = None,
) -> dict[str, Any]:
    """Inspect the existing Google Drive backup (`_restore_vault/` + `Проекты/`) without modifying anything."""
    user_id = str(user.get("id") or "admin")
    cfg = await database.get_gdrive_config(user_id, include_secrets=True)
    if not cfg.get("connected"):
        raise GDriveAuthError("Google Диск не подключён. Выполните OAuth-авторизацию.", status_code=400)

    eff_transport = _resolve_transport(transport)
    access_token = await ensure_valid_access_token(user_id, cfg, transport=eff_transport)
    client = _build_authorized_drive_client(user_id, cfg, access_token, eff_transport)

    root_folder_name = str(cfg.get("root_folder_name") or database.DEFAULT_GDRIVE_ROOT_FOLDER_NAME)
    root_folder_id = await asyncio.to_thread(
        client.find_root_folder,
        str(cfg.get("root_folder_id") or ""),
        root_folder_name,
    )

    empty_dialogs = {
        "exists": False,
        "file_id": "",
        "size_bytes": 0,
        "modified_at": "",
        "chats_count": 0,
        "messages_count": 0,
        "folders_count": 0,
        "passports_count": 0,
    }
    empty_vault = {
        "exists": False,
        "file_id": "",
        "size_bytes": 0,
        "modified_at": "",
        "files_count": 0,
    }
    if not root_folder_id:
        return {
            "has_backup": False,
            "root_folder_name": root_folder_name,
            "root_folder_id": "",
            "manifest_found": False,
            "dialogs_backup": empty_dialogs,
            "secrets_vault": empty_vault,
            "projects": [],
        }

    is_admin = str(user.get("role") or "").lower() == "admin"
    shared_projs = await database.list_shared_projects(for_user_id=user_id, is_admin=is_admin)
    local_projects = await asyncio.to_thread(brain_reader.list_projects_for_user, user, shared_projs)
    local_proj_low = {str(p.get("name") or "").strip().lower() for p in local_projects if p.get("name")}
    personal_root = brain_reader.get_user_personal_root(user).resolve()

    def _inspect_remote_sync() -> dict[str, Any]:
        vault_dir = client.find_child_entry(root_folder_id, "_restore_vault", is_folder=True)
        vault_files_by_name: dict[str, dict[str, Any]] = {}
        if vault_dir and vault_dir.get("id"):
            for entry in client.list_children(str(vault_dir["id"]), folders_only=False):
                ename = str(entry.get("name") or "")
                if ename:
                    vault_files_by_name[ename] = entry

        manifest: dict[str, Any] = {}
        manifest_entry = vault_files_by_name.get("sync_manifest.json")
        if manifest_entry and manifest_entry.get("id"):
            try:
                raw_manifest = client.download_file_bytes(str(manifest_entry["id"]))
                parsed = json.loads(raw_manifest.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    manifest = parsed
            except Exception:
                manifest = {}

        db_entry = vault_files_by_name.get("hub_dialogs_and_memory.sqlite.gz")
        sec_entry = vault_files_by_name.get("secrets.vault.enc")

        projs_dir = client.find_child_entry(root_folder_id, "Проекты", is_folder=True)
        remote_proj_folders: dict[str, dict[str, Any]] = {}
        if projs_dir and projs_dir.get("id"):
            for entry in client.list_children(str(projs_dir["id"]), folders_only=True):
                ename = str(entry.get("name") or "").strip()
                if ename:
                    remote_proj_folders[ename.lower()] = entry

        manifest_projs_raw = manifest.get("projects") if isinstance(manifest.get("projects"), list) else []
        manifest_by_folder_low: dict[str, dict[str, Any]] = {}
        manifest_by_name_low: dict[str, dict[str, Any]] = {}
        for mp in manifest_projs_raw:
            if not isinstance(mp, dict):
                continue
            mname = str(mp.get("name") or "").strip()
            if not mname:
                continue
            dfname = str(mp.get("drive_folder_name") or _safe_drive_segment(mname, fallback="Project")).strip()
            manifest_by_name_low[mname.lower()] = {**mp, "drive_folder_name": dfname}
            manifest_by_folder_low[dfname.lower()] = {**mp, "drive_folder_name": dfname}

        projects_out: list[dict[str, Any]] = []
        seen_proj_low: set[str] = set()

        def _append_project(
            pname: str,
            drive_folder_name: str,
            mp_info: Optional[dict[str, Any]],
            folder_entry: Optional[dict[str, Any]],
        ) -> None:
            plow = pname.lower()
            if plow in seen_proj_low:
                return
            seen_proj_low.add(plow)
            is_self = bool((mp_info or {}).get("is_self_hub")) or brain_reader.is_self_hub_project_name(pname)
            exists_loc = (
                is_self
                or (plow in local_proj_low)
                or (personal_root / pname).is_dir()
            )
            code_cnt = int((mp_info or {}).get("code_files_count") or 0)
            code_bytes = int((mp_info or {}).get("code_bytes") or 0)
            if mp_info is None and folder_entry and folder_entry.get("id"):
                code_sub = client.find_child_entry(str(folder_entry["id"]), "code", is_folder=True)
                if code_sub and code_sub.get("id"):
                    rfiles = client.walk_folder_files(str(code_sub["id"]))
                    code_cnt = len(rfiles)
                    code_bytes = sum(int(f.get("size") or 0) for f in rfiles)
            projects_out.append(
                {
                    "name": pname,
                    "drive_folder_name": drive_folder_name,
                    "is_shared": bool((mp_info or {}).get("is_shared")),
                    "is_self_hub": is_self,
                    "chats_count": int((mp_info or {}).get("chats_count") or 0),
                    "code_files_count": code_cnt,
                    "code_bytes": code_bytes,
                    "exists_locally": exists_loc,
                    "has_drive_folder": folder_entry is not None,
                }
            )

        for mp in manifest_projs_raw:
            if not isinstance(mp, dict):
                continue
            mname = str(mp.get("name") or "").strip()
            if not mname:
                continue
            dfname = str(mp.get("drive_folder_name") or _safe_drive_segment(mname, fallback="Project")).strip()
            _append_project(mname, dfname, mp, remote_proj_folders.get(dfname.lower()))

        for flow, fentry in sorted(remote_proj_folders.items(), key=lambda kv: kv[0]):
            if flow in manifest_by_folder_low:
                continue
            fname = str(fentry.get("name") or "").strip()
            if not fname:
                continue
            _append_project(fname, fname, manifest_by_name_low.get(fname.lower()), fentry)

        projects_out.sort(key=lambda item: str(item["name"]).lower())

        fallback_chats_cnt = int(manifest.get("regular_chats_count") or 0) + sum(
            int(p.get("chats_count") or 0) for p in projects_out
        )
        dialogs_backup = {
            "exists": db_entry is not None,
            "file_id": str(db_entry.get("id") or "") if db_entry else "",
            "size_bytes": int(db_entry.get("size") or 0) if db_entry else 0,
            "modified_at": str(db_entry.get("modifiedTime") or "") if db_entry else "",
            "chats_count": int(manifest.get("chats_count") if manifest.get("chats_count") is not None else fallback_chats_cnt),
            "messages_count": int(manifest.get("messages_count") or 0),
            "folders_count": int(manifest.get("folders_count") or 0),
            "passports_count": int(manifest.get("passports_count") or 0),
        }
        secrets_vault = {
            "exists": sec_entry is not None,
            "file_id": str(sec_entry.get("id") or "") if sec_entry else "",
            "size_bytes": int(sec_entry.get("size") or 0) if sec_entry else 0,
            "modified_at": str(sec_entry.get("modifiedTime") or "") if sec_entry else "",
            "files_count": int(manifest.get("vault_files_count") or 0),
        }
        return {
            "has_backup": bool(db_entry or sec_entry or projects_out),
            "root_folder_name": root_folder_name,
            "root_folder_id": root_folder_id,
            "manifest_found": bool(manifest),
            "manifest_modified_at": str(manifest_entry.get("modifiedTime") or "") if manifest_entry else "",
            "dialogs_backup": dialogs_backup,
            "secrets_vault": secrets_vault,
            "projects": projects_out,
        }

    return await asyncio.to_thread(_inspect_remote_sync)


async def _merge_sqlite_backup_into_live_db(
    backup_sqlite_gz: bytes,
    *,
    user: dict[str, Any],
    overwrite_existing: bool,
) -> dict[str, Any]:
    """Decompress, integrity-check, and safely merge backup SQLite into live Hub DB with local path rebinding."""
    try:
        raw_sqlite = await asyncio.to_thread(gzip.decompress, backup_sqlite_gz)
    except Exception as exc:
        raise GDriveServiceError(
            "Не удалось распаковать архив hub_dialogs_and_memory.sqlite.gz",
            status_code=400,
        ) from exc

    def _read_and_validate_backup_sync() -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="agy_gdrive_restore_db_") as tmp_dir:
            tmp_db = Path(tmp_dir) / "backup.sqlite"
            tmp_db.write_bytes(raw_sqlite)
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = sqlite3.connect(str(tmp_db), timeout=15.0)
                conn.row_factory = sqlite3.Row
                ic_rows = conn.execute("PRAGMA integrity_check;").fetchall()
                if not ic_rows or len(ic_rows) != 1 or str(ic_rows[0][0]).lower() != "ok":
                    raise GDriveServiceError(
                        "Резервная копия SQLite повреждена (PRAGMA integrity_check не пройден)",
                        status_code=400,
                    )
                existing_tables = {
                    str(r[0]).lower(): str(r[0])
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
                    ).fetchall()
                }
                extracted: dict[str, dict[str, Any]] = {}
                for tbl_low in (
                    "chat_folders",
                    "shared_projects",
                    "chats",
                    "messages",
                    "project_memories",
                    "model_advisor_catalog",
                ):
                    real_name = existing_tables.get(tbl_low)
                    if not real_name:
                        continue
                    escaped = real_name.replace('"', '""')
                    cols = [str(c[1]) for c in conn.execute(f'PRAGMA table_info("{escaped}");').fetchall()]
                    order_sql = " ORDER BY id ASC" if tbl_low == "messages" and "id" in cols else ""
                    rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{escaped}"{order_sql};').fetchall()]
                    extracted[tbl_low] = {"columns": cols, "rows": rows}
                return extracted
            except sqlite3.DatabaseError as exc:
                raise GDriveServiceError(
                    f"Резервная копия SQLite не читается: {exc}",
                    status_code=400,
                ) from exc
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    backup_data = await asyncio.to_thread(_read_and_validate_backup_sync)

    stats = {
        "requested": True,
        "chats_added": 0,
        "chats_updated": 0,
        "chats_skipped": 0,
        "messages_restored": 0,
        "folders_restored": 0,
        "passports_restored": 0,
        "passports_updated": 0,
        "passports_skipped": 0,
        "shared_projects_added": 0,
        "rebound_project_paths": 0,
        "advisor_models_restored": 0,
    }

    personal_root = brain_reader.get_user_personal_root(user).resolve()
    restored_folders_set: set[str] = set()

    async with database.connect_db() as db:
        db.row_factory = aiosqlite.Row
        cur_t = await db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
        )
        live_tables = {str(r[0]).lower(): str(r[0]) for r in await cur_t.fetchall()}

        async def _live_cols(tbl_low: str) -> list[str]:
            real = live_tables.get(tbl_low)
            if not real:
                return []
            esc = real.replace('"', '""')
            c = await db.execute(f'PRAGMA table_info("{esc}");')
            return [str(r[1]) for r in await c.fetchall()]

        # 1. Optional `chat_folders` table (if present in both backup and live DB)
        if "chat_folders" in backup_data and "chat_folders" in live_tables:
            live_cf_cols = await _live_cols("chat_folders")
            common_cf = [c for c in backup_data["chat_folders"]["columns"] if c in live_cf_cols]
            if common_cf:
                verb = "INSERT OR REPLACE" if overwrite_existing else "INSERT OR IGNORE"
                cols_sql = ", ".join(f'"{c}"' for c in common_cf)
                ph_sql = ", ".join("?" for _ in common_cf)
                for row in backup_data["chat_folders"]["rows"]:
                    cur = await db.execute(
                        f'{verb} INTO chat_folders ({cols_sql}) VALUES ({ph_sql})',
                        [row.get(c) for c in common_cf],
                    )
                    if int(cur.rowcount or 0) > 0:
                        stats["folders_restored"] += 1

        # 2. `shared_projects` table (rebind stale paths from another PC/drive)
        shared_by_low: dict[str, dict[str, Any]] = {}
        if "shared_projects" in live_tables:
            cur_sp = await db.execute("SELECT * FROM shared_projects")
            for sp_row in await cur_sp.fetchall():
                d = dict(sp_row)
                nm = str(d.get("name") or "").strip().lower()
                if nm:
                    shared_by_low[nm] = d

        if "shared_projects" in backup_data and "shared_projects" in live_tables:
            live_sp_cols = await _live_cols("shared_projects")
            common_sp = [c for c in backup_data["shared_projects"]["columns"] if c in live_sp_cols]
            for sp_row in backup_data["shared_projects"]["rows"]:
                sp_name = str(sp_row.get("name") or "").strip()
                if not sp_name or not _validate_restore_project_name(sp_name):
                    continue
                row_copy = dict(sp_row)
                raw_path = str(row_copy.get("path") or "").strip()
                try:
                    valid_p = brain_reader.validate_shared_project_path(sp_name, existing_path=raw_path)
                    row_copy["path"] = str(valid_p.resolve())
                except ValueError:
                    row_copy["path"] = str((config.SHARED_PROJECTS_DIR / sp_name).resolve())

                existing_sp = shared_by_low.get(sp_name.lower())
                if existing_sp is None:
                    cols_sql = ", ".join(f'"{c}"' for c in common_sp)
                    ph_sql = ", ".join("?" for _ in common_sp)
                    cur = await db.execute(
                        f"INSERT OR IGNORE INTO shared_projects ({cols_sql}) VALUES ({ph_sql})",
                        [row_copy.get(c) for c in common_sp],
                    )
                    if int(cur.rowcount or 0) > 0:
                        stats["shared_projects_added"] += 1
                        shared_by_low[sp_name.lower()] = row_copy
                elif overwrite_existing:
                    upd_cols = [c for c in common_sp if c not in ("id", "name")]
                    if upd_cols:
                        set_sql = ", ".join(f'"{c}" = ?' for c in upd_cols)
                        await db.execute(
                            f"UPDATE shared_projects SET {set_sql} WHERE LOWER(name) = LOWER(?)",
                            [*[row_copy.get(c) for c in upd_cols], sp_name],
                        )
                        shared_by_low[sp_name.lower()] = {**existing_sp, **row_copy}

        # 3. `chats` table with automatic local `project_path` rebinding
        added_chat_ids: set[str] = set()
        updated_chat_ids: set[str] = set()
        if "chats" in backup_data and "chats" in live_tables:
            live_chat_cols = await _live_cols("chats")
            common_chat_cols = [c for c in backup_data["chats"]["columns"] if c in live_chat_cols]
            cur_ex_chats = await db.execute("SELECT id FROM chats")
            existing_chat_ids = {str(r[0]) for r in await cur_ex_chats.fetchall()}

            for ch_row in backup_data["chats"]["rows"]:
                cid = str(ch_row.get("id") or "").strip()
                if not cid:
                    continue
                row_copy = dict(ch_row)
                if str(row_copy.get("kind") or "") == "project":
                    pname = str(row_copy.get("project_name") or "").strip()
                    if pname:
                        target_dir, _, _ = _resolve_local_project_dir_for_restore(
                            pname,
                            user=user,
                            shared_by_low=shared_by_low,
                        )
                        new_proj_path = str(target_dir)
                        if str(row_copy.get("project_path") or "") != new_proj_path:
                            stats["rebound_project_paths"] += 1
                        row_copy["project_path"] = new_proj_path

                folder_val = str(row_copy.get("folder") or "").strip()
                if cid not in existing_chat_ids:
                    cols_sql = ", ".join(f'"{c}"' for c in common_chat_cols)
                    ph_sql = ", ".join("?" for _ in common_chat_cols)
                    await db.execute(
                        f"INSERT INTO chats ({cols_sql}) VALUES ({ph_sql})",
                        [row_copy.get(c) for c in common_chat_cols],
                    )
                    existing_chat_ids.add(cid)
                    added_chat_ids.add(cid)
                    stats["chats_added"] += 1
                    if folder_val:
                        restored_folders_set.add(folder_val)
                elif overwrite_existing:
                    upd_cols = [c for c in common_chat_cols if c != "id"]
                    if upd_cols:
                        set_sql = ", ".join(f'"{c}" = ?' for c in upd_cols)
                        await db.execute(
                            f"UPDATE chats SET {set_sql} WHERE id = ?",
                            [*[row_copy.get(c) for c in upd_cols], cid],
                        )
                    updated_chat_ids.add(cid)
                    stats["chats_updated"] += 1
                    if folder_val:
                        restored_folders_set.add(folder_val)
                else:
                    stats["chats_skipped"] += 1

        # 4. `messages` table (safe PK collision avoidance when merging into non-empty DB)
        if "messages" in backup_data and "messages" in live_tables:
            live_msg_cols = await _live_cols("messages")
            common_msg_cols = [c for c in backup_data["messages"]["columns"] if c in live_msg_cols]
            common_msg_no_id = [c for c in common_msg_cols if c != "id"]

            for cid in updated_chat_ids:
                await db.execute("DELETE FROM messages WHERE chat_id = ?", (cid,))

            cur_ex_mids = await db.execute("SELECT id FROM messages")
            used_msg_ids = {int(r[0]) for r in await cur_ex_mids.fetchall() if r[0] is not None}

            cols_with_id_sql = ", ".join(f'"{c}"' for c in common_msg_cols)
            ph_with_id_sql = ", ".join("?" for _ in common_msg_cols)
            cols_no_id_sql = ", ".join(f'"{c}"' for c in common_msg_no_id)
            ph_no_id_sql = ", ".join("?" for _ in common_msg_no_id)

            eligible_chats = added_chat_ids | updated_chat_ids
            for msg_row in backup_data["messages"]["rows"]:
                mcid = str(msg_row.get("chat_id") or "").strip()
                if mcid not in eligible_chats:
                    continue
                raw_mid = msg_row.get("id")
                can_keep_id = (
                    "id" in common_msg_cols
                    and raw_mid is not None
                    and int(raw_mid) not in used_msg_ids
                )
                if can_keep_id:
                    await db.execute(
                        f"INSERT INTO messages ({cols_with_id_sql}) VALUES ({ph_with_id_sql})",
                        [msg_row.get(c) for c in common_msg_cols],
                    )
                    used_msg_ids.add(int(raw_mid))
                else:
                    await db.execute(
                        f"INSERT INTO messages ({cols_no_id_sql}) VALUES ({ph_no_id_sql})",
                        [msg_row.get(c) for c in common_msg_no_id],
                    )
                stats["messages_restored"] += 1

        # 5. `project_memories` table (restore missing or empty local Passports; overwrite only if requested)
        if "project_memories" in backup_data and "project_memories" in live_tables:
            live_pm_cols = await _live_cols("project_memories")
            common_pm_cols = [c for c in backup_data["project_memories"]["columns"] if c in live_pm_cols]
            cur_ex_pm = await db.execute("SELECT * FROM project_memories")
            existing_pm = {str(r["scope_key"]): dict(r) for r in await cur_ex_pm.fetchall()}

            for pm_row in backup_data["project_memories"]["rows"]:
                skey = str(pm_row.get("scope_key") or "").strip()
                if not skey:
                    continue
                if _is_passport_row_empty(pm_row):
                    continue
                live_pm = existing_pm.get(skey)
                if live_pm is None:
                    cols_sql = ", ".join(f'"{c}"' for c in common_pm_cols)
                    ph_sql = ", ".join("?" for _ in common_pm_cols)
                    await db.execute(
                        f"INSERT INTO project_memories ({cols_sql}) VALUES ({ph_sql})",
                        [pm_row.get(c) for c in common_pm_cols],
                    )
                    stats["passports_restored"] += 1
                elif _is_passport_row_empty(live_pm):
                    upd_cols = [c for c in common_pm_cols if c != "scope_key"]
                    if upd_cols:
                        set_sql = ", ".join(f'"{c}" = ?' for c in upd_cols)
                        await db.execute(
                            f"UPDATE project_memories SET {set_sql} WHERE scope_key = ?",
                            [*[pm_row.get(c) for c in upd_cols], skey],
                        )
                    stats["passports_restored"] += 1
                elif overwrite_existing:
                    upd_cols = [c for c in common_pm_cols if c != "scope_key"]
                    if upd_cols:
                        set_sql = ", ".join(f'"{c}" = ?' for c in upd_cols)
                        await db.execute(
                            f"UPDATE project_memories SET {set_sql} WHERE scope_key = ?",
                            [*[pm_row.get(c) for c in upd_cols], skey],
                        )
                    stats["passports_updated"] += 1
                else:
                    stats["passports_skipped"] += 1

        # 6. `model_advisor_catalog` table (restore missing `series_key`; update existing when `overwrite_existing=True`)
        if "model_advisor_catalog" in backup_data and "model_advisor_catalog" in live_tables:
            live_mac_cols = await _live_cols("model_advisor_catalog")
            common_mac_cols = [
                c for c in backup_data["model_advisor_catalog"]["columns"] if c in live_mac_cols
            ]
            if common_mac_cols and "series_key" in common_mac_cols:
                cur_ex_mac = await db.execute("SELECT series_key FROM model_advisor_catalog")
                existing_mac_keys = {str(r[0]) for r in await cur_ex_mac.fetchall() if r[0]}
                cols_sql = ", ".join(f'"{c}"' for c in common_mac_cols)
                ph_sql = ", ".join("?" for _ in common_mac_cols)
                upd_cols = [c for c in common_mac_cols if c != "series_key"]

                for mac_row in backup_data["model_advisor_catalog"]["rows"]:
                    skey = str(mac_row.get("series_key") or "").strip()
                    if not skey:
                        continue
                    if skey not in existing_mac_keys:
                        await db.execute(
                            f"INSERT INTO model_advisor_catalog ({cols_sql}) VALUES ({ph_sql})",
                            [mac_row.get(c) for c in common_mac_cols],
                        )
                        existing_mac_keys.add(skey)
                        stats["advisor_models_restored"] += 1
                    elif overwrite_existing and upd_cols:
                        set_sql = ", ".join(f'"{c}" = ?' for c in upd_cols)
                        await db.execute(
                            f"UPDATE model_advisor_catalog SET {set_sql} WHERE series_key = ?",
                            [*[mac_row.get(c) for c in upd_cols], skey],
                        )
                        stats["advisor_models_restored"] += 1

        await db.commit()

    if restored_folders_set:
        stats["folders_restored"] = max(stats["folders_restored"], len(restored_folders_set))

    database.invalidate_chat_list_cache(None)
    brain_reader.invalidate_projects_list_cache()
    _ = personal_root
    return stats


async def execute_gdrive_restore(
    user: dict[str, Any],
    *,
    restore_dialogs: bool = False,
    restore_code: bool = False,
    selected_projects: Optional[list[str]] = None,
    restore_env_vault: bool = False,
    vault_passphrase: str = "",
    overwrite_existing: bool = False,
    transport: Optional[TransportCallable] = None,
) -> dict[str, Any]:
    """Safely restore dialogs/passports, clean source code, and/or encrypted `.env` vault from Google Drive."""
    if not (restore_dialogs or restore_code or restore_env_vault):
        raise GDriveServiceError("Выберите хотя бы один компонент для восстановления", status_code=400)

    user_id = str(user.get("id") or "admin")
    cfg = await database.get_gdrive_config(user_id, include_secrets=True)
    if not cfg.get("connected"):
        raise GDriveAuthError("Google Диск не подключён. Выполните OAuth-авторизацию.", status_code=400)

    eff_transport = _resolve_transport(transport)
    access_token = await ensure_valid_access_token(user_id, cfg, transport=eff_transport)
    client = _build_authorized_drive_client(user_id, cfg, access_token, eff_transport)

    root_folder_name = str(cfg.get("root_folder_name") or database.DEFAULT_GDRIVE_ROOT_FOLDER_NAME)
    root_folder_id = await asyncio.to_thread(
        client.find_root_folder,
        str(cfg.get("root_folder_id") or ""),
        root_folder_name,
    )
    if not root_folder_id:
        raise GDriveServiceError(
            f"Папка резервной копии «{root_folder_name}» не найдена на Google Диске",
            status_code=404,
        )

    # Locate `_restore_vault/` entries upfront
    def _locate_vault_entries_sync() -> dict[str, dict[str, Any]]:
        vdir = client.find_child_entry(root_folder_id, "_restore_vault", is_folder=True)
        if not vdir or not vdir.get("id"):
            return {}
        entries: dict[str, dict[str, Any]] = {}
        for item in client.list_children(str(vdir["id"]), folders_only=False):
            nm = str(item.get("name") or "")
            if nm:
                entries[nm] = item
        return entries

    vault_entries = await asyncio.to_thread(_locate_vault_entries_sync)

    # PRE-FLIGHT 1: Decrypt `secrets.vault.enc` strictly in memory BEFORE touching any file or DB row!
    decrypted_vault: Optional[dict[str, Any]] = None
    if restore_env_vault:
        if not str(vault_passphrase or ""):
            raise GDriveVaultError(
                "Укажите пароль для расшифровки сейфа секретов (secrets.vault.enc)",
                status_code=400,
            )
        sec_entry = vault_entries.get("secrets.vault.enc")
        if not sec_entry or not sec_entry.get("id"):
            raise GDriveServiceError(
                "Зашифрованный сейф secrets.vault.enc не найден в облачной копии",
                status_code=404,
            )
        raw_vault_bytes = await asyncio.to_thread(client.download_file_bytes, str(sec_entry["id"]))
        decrypted_vault = await asyncio.to_thread(decrypt_secrets_vault, raw_vault_bytes, vault_passphrase)

    warnings: list[str] = []
    blocked_paths: list[str] = []
    self_hub_overwrite_blocked = False
    self_hub_protected_files = 0

    def _note_self_hub_shield() -> None:
        nonlocal self_hub_overwrite_blocked
        if not self_hub_overwrite_blocked:
            self_hub_overwrite_blocked = True
            warnings.append(
                "Защита самопроекта Antigravity_Hub: перезапись существующих файлов "
                "принудительно отключена (overwrite_existing проигнорирован)."
            )

    # STEP 1: Restore Dialogs & Passports (`hub_dialogs_and_memory.sqlite.gz`)
    dialogs_report: dict[str, Any] = {
        "requested": bool(restore_dialogs),
        "chats_added": 0,
        "chats_updated": 0,
        "chats_skipped": 0,
        "messages_restored": 0,
        "folders_restored": 0,
        "passports_restored": 0,
        "passports_updated": 0,
        "passports_skipped": 0,
        "shared_projects_added": 0,
        "rebound_project_paths": 0,
        "advisor_models_restored": 0,
    }
    if restore_dialogs:
        db_entry = vault_entries.get("hub_dialogs_and_memory.sqlite.gz")
        if not db_entry or not db_entry.get("id"):
            raise GDriveServiceError(
                "Файл резервной копии диалогов hub_dialogs_and_memory.sqlite.gz не найден в облаке",
                status_code=404,
            )
        db_gz_bytes = await asyncio.to_thread(client.download_file_bytes, str(db_entry["id"]))
        dialogs_report = await _merge_sqlite_backup_into_live_db(
            db_gz_bytes,
            user=user,
            overwrite_existing=bool(overwrite_existing),
        )

    # Refresh shared projects after optional dialog/shared_projects restore
    is_admin = str(user.get("role") or "").lower() == "admin"
    shared_projs = await database.list_shared_projects(for_user_id=user_id, is_admin=is_admin)
    shared_by_low = {str(sp.get("name") or "").strip().lower(): sp for sp in shared_projs if sp.get("name")}
    selected_low = {str(p).strip().lower() for p in (selected_projects or []) if str(p).strip()}

    # Parse manifest to map Drive folder names back to original project names
    manifest_name_by_folder_low: dict[str, str] = {}
    manifest_entry = vault_entries.get("sync_manifest.json")
    if manifest_entry and manifest_entry.get("id"):
        try:
            raw_m = await asyncio.to_thread(client.download_file_bytes, str(manifest_entry["id"]))
            parsed_m = json.loads(raw_m.decode("utf-8", errors="replace"))
            if isinstance(parsed_m, dict) and isinstance(parsed_m.get("projects"), list):
                for mp in parsed_m["projects"]:
                    if not isinstance(mp, dict):
                        continue
                    mname = str(mp.get("name") or "").strip()
                    if not mname:
                        continue
                    dfname = str(mp.get("drive_folder_name") or _safe_drive_segment(mname, fallback="Project")).strip()
                    manifest_name_by_folder_low[dfname.lower()] = mname
        except Exception:
            pass

    # STEP 2: Restore Clean Source Code (`Проекты/<Проект>/code/<rel_path>`)
    code_report: dict[str, Any] = {
        "requested": bool(restore_code),
        "projects_processed": [],
        "files_restored": 0,
        "files_overwritten": 0,
        "files_skipped": 0,
    }
    if restore_code:
        def _restore_code_sync() -> None:
            nonlocal self_hub_protected_files
            projs_dir = client.find_child_entry(root_folder_id, "Проекты", is_folder=True)
            if not projs_dir or not projs_dir.get("id"):
                return
            remote_folders = client.list_children(str(projs_dir["id"]), folders_only=True)
            for fentry in sorted(remote_folders, key=lambda c: str(c.get("name") or "").lower()):
                drive_fname = str(fentry.get("name") or "").strip()
                fid = str(fentry.get("id") or "")
                if not drive_fname or not fid:
                    continue
                proj_name_raw = manifest_name_by_folder_low.get(drive_fname.lower(), drive_fname)
                valid_proj = _validate_restore_project_name(proj_name_raw)
                if not valid_proj:
                    blocked_paths.append(f"Проекты/{drive_fname}")
                    continue
                if selected_low and valid_proj.lower() not in selected_low and drive_fname.lower() not in selected_low:
                    continue

                target_dir, is_self_hub, _ = _resolve_local_project_dir_for_restore(
                    valid_proj,
                    user=user,
                    shared_by_low=shared_by_low,
                )
                if target_dir.resolve() == config.BASE_DIR.resolve():
                    is_self_hub = True

                eff_overwrite = False if is_self_hub else bool(overwrite_existing)
                if is_self_hub and overwrite_existing:
                    _note_self_hub_shield()

                code_sub = client.find_child_entry(fid, "code", is_folder=True)
                if not code_sub or not code_sub.get("id"):
                    continue

                if not is_self_hub:
                    target_dir.mkdir(parents=True, exist_ok=True)
                elif not target_dir.is_dir():
                    continue

                code_report["projects_processed"].append(valid_proj)
                remote_files = client.walk_folder_files(str(code_sub["id"]))
                for rfile in remote_files:
                    raw_rel = str(rfile.get("rel_path") or "")
                    safe_rel = _validate_restore_rel_path(
                        raw_rel,
                        allow_secrets=False,
                        is_self_hub=is_self_hub,
                    )
                    if not safe_rel:
                        blocked_paths.append(f"{valid_proj}/{raw_rel}")
                        continue

                    dest = _is_safe_path_within_root(target_dir, safe_rel)
                    if dest is None:
                        blocked_paths.append(f"{valid_proj}/{raw_rel}")
                        continue

                    existed = dest.exists()
                    if existed and not eff_overwrite:
                        code_report["files_skipped"] += 1
                        if is_self_hub:
                            self_hub_protected_files += 1
                            if overwrite_existing:
                                _note_self_hub_shield()
                        continue

                    file_bytes = client.download_file_bytes(str(rfile["id"]))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # Re-check symlink safety right before writing
                    if _is_safe_path_within_root(target_dir, safe_rel) is None:
                        blocked_paths.append(f"{valid_proj}/{raw_rel}")
                        continue
                    dest.write_bytes(file_bytes)
                    if existed:
                        code_report["files_overwritten"] += 1
                    else:
                        code_report["files_restored"] += 1

        await asyncio.to_thread(_restore_code_sync)

    # STEP 3: Restore Encrypted Secrets Vault (`secrets.vault.enc`)
    vault_report: dict[str, Any] = {
        "requested": bool(restore_env_vault),
        "files_restored": 0,
        "files_skipped": 0,
    }
    if restore_env_vault and isinstance(decrypted_vault, dict):
        def _restore_vault_sync() -> None:
            nonlocal self_hub_protected_files
            files_map = decrypted_vault.get("files")
            if not isinstance(files_map, dict):
                return
            for key in sorted(files_map.keys()):
                entry = files_map.get(key)
                if not isinstance(entry, dict):
                    continue
                raw_proj = str(entry.get("project") or "").strip()
                raw_rel = str(entry.get("rel_path") or "").strip()
                valid_proj = _validate_restore_project_name(raw_proj)
                if not valid_proj:
                    blocked_paths.append(f"vault:{key}")
                    continue
                if selected_low and valid_proj.lower() not in selected_low:
                    continue

                target_dir, is_self_hub, _ = _resolve_local_project_dir_for_restore(
                    valid_proj,
                    user=user,
                    shared_by_low=shared_by_low,
                )
                if target_dir.resolve() == config.BASE_DIR.resolve():
                    is_self_hub = True

                safe_rel = _validate_restore_rel_path(
                    raw_rel,
                    allow_secrets=True,
                    is_self_hub=is_self_hub,
                )
                if not safe_rel:
                    blocked_paths.append(f"vault:{valid_proj}/{raw_rel}")
                    continue

                eff_overwrite = False if is_self_hub else bool(overwrite_existing)
                if is_self_hub and overwrite_existing:
                    _note_self_hub_shield()

                if not is_self_hub:
                    target_dir.mkdir(parents=True, exist_ok=True)
                elif not target_dir.is_dir():
                    continue

                dest = _is_safe_path_within_root(target_dir, safe_rel)
                if dest is None:
                    blocked_paths.append(f"vault:{valid_proj}/{raw_rel}")
                    continue

                if dest.exists() and not eff_overwrite:
                    fname_low = dest.name.lower()
                    is_env_file = fname_low == ".env" or fname_low.startswith(".env.")
                    try:
                        existing_size = int(dest.stat().st_size)
                    except OSError:
                        existing_size = 1
                    # Never overwrite existing non-empty `.env` (or any existing secret file) unless overwrite_existing=True
                    if (not is_env_file) or existing_size > 0 or is_self_hub:
                        vault_report["files_skipped"] += 1
                        if is_self_hub:
                            self_hub_protected_files += 1
                        continue

                raw_bytes = decode_vault_entry_bytes(entry)
                expected_sha = str(entry.get("sha256") or "").strip().lower()
                if expected_sha and hashlib.sha256(raw_bytes).hexdigest().lower() != expected_sha:
                    blocked_paths.append(f"vault_sha_mismatch:{valid_proj}/{raw_rel}")
                    continue

                dest.parent.mkdir(parents=True, exist_ok=True)
                if _is_safe_path_within_root(target_dir, safe_rel) is None:
                    blocked_paths.append(f"vault:{valid_proj}/{raw_rel}")
                    continue
                dest.write_bytes(raw_bytes)
                vault_report["files_restored"] += 1

        await asyncio.to_thread(_restore_vault_sync)

    database.invalidate_chat_list_cache(None)
    brain_reader.invalidate_projects_list_cache()

    return {
        "dialogs": dialogs_report,
        "code": code_report,
        "vault": vault_report,
        "self_hub_overwrite_blocked": self_hub_overwrite_blocked,
        "self_hub_protected_files": self_hub_protected_files,
        "blocked_paths": blocked_paths,
        "warnings": warnings,
    }
