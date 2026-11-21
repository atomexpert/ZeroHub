from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import ipaddress
import time
from typing import Any, Optional

import segno
from fastapi import Depends, HTTPException, Request, WebSocket, status

from . import database
from .config import HUB_SECRET_KEY, get_pin

TOKEN_TTL_SECONDS = 90 * 24 * 3600  # 90 days for seamless mobile PWA usage
COOKIE_NAME = "agy_hub_session"

# Brute-force protection: key (IP, IP:username, or user:username) -> list of failed attempt timestamps
_FAILED_ATTEMPTS: dict[str, list[float]] = {}
MAX_ATTEMPTS_PER_MINUTE = 6
MAX_USER_ATTEMPTS_PER_10M = 20
USER_WINDOW_SECONDS = 600.0
_MAX_TRACKED_KEYS = 4096
_TRUSTED_LOCAL_PROXIES = {"127.0.0.1", "::1"}


def get_request_client_ip(request: Request) -> str:
    """Extract real visitor IP safely when behind a local reverse tunnel/proxy."""
    direct_ip = request.client.host if request.client and request.client.host else "unknown"
    if direct_ip in _TRUSTED_LOCAL_PROXIES:
        for hdr in ("x-real-ip", "cf-connecting-ip", "x-forwarded-for"):
            raw = request.headers.get(hdr, "").strip()
            if not raw:
                continue
            candidate = raw.split(",")[0].strip()
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                continue
    return direct_ip


def is_request_https(request: Request) -> bool:
    """Detect HTTPS either directly or via trusted local reverse tunnel."""
    if request.url.scheme == "https":
        return True
    direct_ip = request.client.host if request.client and request.client.host else ""
    if direct_ip in _TRUSTED_LOCAL_PROXIES:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if proto == "https":
            return True
    return False


def _pin_fingerprint(pin: str) -> str:
    return hashlib.sha256(f"pin:{pin}:{HUB_SECRET_KEY}".encode("utf-8")).hexdigest()[:16]


def create_user_session_token(user_id: str, session_version: int = 1) -> str:
    issued = int(time.time())
    ver = int(session_version or 1)
    payload = f"u2.{user_id}.{issued}.{ver}"
    sig = hmac.new(
        HUB_SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{sig}"


def _verify_legacy_v1_token(token: str) -> bool:
    """Verify legacy 3-part admin session token for seamless upgrade without tab logout."""
    if not token or token.count(".") != 2:
        return False
    issued_s, fp, sig = token.split(".", 2)
    try:
        issued = int(issued_s)
    except ValueError:
        return False
    now = int(time.time())
    if now - issued > TOKEN_TTL_SECONDS or issued > now + 300:
        return False

    expected_payload = f"{issued_s}.{fp}"
    expected_sig = hmac.new(
        HUB_SECRET_KEY.encode("utf-8"),
        expected_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False

    current_fp = _pin_fingerprint(get_pin())
    return hmac.compare_digest(fp, current_fp)


async def resolve_token_user(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    clean = token.strip()
    if not clean:
        return None

    # v2 multi-user signed token: u2.{user_id}.{issued}.{session_version}.{sig}
    if clean.startswith("u2."):
        parts = clean.split(".")
        if len(parts) != 5:
            return None
        _, user_id, issued_s, ver_s, sig = parts
        try:
            issued = int(issued_s)
            ver = int(ver_s)
        except ValueError:
            return None

        now = int(time.time())
        if now - issued > TOKEN_TTL_SECONDS or issued > now + 300:
            return None

        expected_payload = f"u2.{user_id}.{issued_s}.{ver_s}"
        expected_sig = hmac.new(
            HUB_SECRET_KEY.encode("utf-8"),
            expected_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None

        user = await database.get_user_by_id(user_id)
        if not user or not user.get("is_active", False):
            return None
        if int(user.get("session_version") or 1) != ver:
            return None
        return user

    # Legacy v1 token -> map seamlessly to built-in admin if still valid
    if _verify_legacy_v1_token(clean):
        admin_user = await database.get_user_by_id("admin")
        if admin_user and admin_user.get("is_active", False):
            return admin_user
    return None


def _prune_failed_attempts(now: float) -> None:
    if len(_FAILED_ATTEMPTS) <= _MAX_TRACKED_KEYS:
        return
    for key in list(_FAILED_ATTEMPTS.keys()):
        window = USER_WINDOW_SECONDS if key.startswith("user:") else 60.0
        recent = [t for t in _FAILED_ATTEMPTS.get(key, ()) if now - t < window]
        if recent:
            _FAILED_ATTEMPTS[key] = recent
        else:
            _FAILED_ATTEMPTS.pop(key, None)
    while len(_FAILED_ATTEMPTS) > _MAX_TRACKED_KEYS:
        oldest_key = min(
            _FAILED_ATTEMPTS.keys(),
            key=lambda k: _FAILED_ATTEMPTS[k][-1] if _FAILED_ATTEMPTS[k] else 0.0,
        )
        _FAILED_ATTEMPTS.pop(oldest_key, None)


def check_rate_limit(client_ip: str, username: str = "") -> None:
    now = time.time()
    uname = (username or "").strip().lower()
    keys = [client_ip]
    if uname:
        keys.append(f"{client_ip}:{uname}")
    for k in keys:
        existing = _FAILED_ATTEMPTS.get(k)
        if not existing:
            _FAILED_ATTEMPTS.pop(k, None)
            continue
        recent = [t for t in existing if now - t < 60.0]
        if recent:
            _FAILED_ATTEMPTS[k] = recent
        else:
            _FAILED_ATTEMPTS.pop(k, None)
        if len(recent) >= MAX_ATTEMPTS_PER_MINUTE:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Слишком много неверных попыток входа. Подождите 1 минуту.",
            )
    if uname:
        u_key = f"user:{uname}"
        existing_u = _FAILED_ATTEMPTS.get(u_key)
        if not existing_u:
            _FAILED_ATTEMPTS.pop(u_key, None)
        else:
            recent_u = [t for t in existing_u if now - t < USER_WINDOW_SECONDS]
            if recent_u:
                _FAILED_ATTEMPTS[u_key] = recent_u
            else:
                _FAILED_ATTEMPTS.pop(u_key, None)
            if len(recent_u) >= MAX_USER_ATTEMPTS_PER_10M:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Слишком много неверных попыток входа. Подождите несколько минут.",
                )


def record_failed_attempt(client_ip: str, username: str = "") -> None:
    now = time.time()
    uname = (username or "").strip().lower()
    _FAILED_ATTEMPTS.setdefault(client_ip, []).append(now)
    if uname:
        _FAILED_ATTEMPTS.setdefault(f"{client_ip}:{uname}", []).append(now)
        _FAILED_ATTEMPTS.setdefault(f"user:{uname}", []).append(now)
    _prune_failed_attempts(now)


def clear_failed_attempts(client_ip: str, username: str = "") -> None:
    _FAILED_ATTEMPTS.pop(client_ip, None)
    uname = (username or "").strip().lower()
    if uname:
        _FAILED_ATTEMPTS.pop(f"{client_ip}:{uname}", None)
        _FAILED_ATTEMPTS.pop(f"user:{uname}", None)


async def authenticate_credentials(
    username: str,
    secret_input: str,
    client_ip: str,
) -> Optional[dict[str, Any]]:
    clean_user = (username or "").strip() or "admin"
    clean_secret = (secret_input or "").strip()
    check_rate_limit(client_ip, clean_user)

    if not clean_secret:
        record_failed_attempt(client_ip, clean_user)
        return None

    auth_row = await database.get_user_auth_by_username(clean_user)
    if not auth_row:
        # Constant-ish work dummy hash to prevent username timing enumeration
        await asyncio.to_thread(database.hash_secret, "dummy_anti_timing_probe")
        record_failed_attempt(client_ip, clean_user)
        return None

    if not auth_row.get("is_active", False):
        record_failed_attempt(client_ip, clean_user)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Эта учётная запись временно заблокирована администратором",
        )

    matched = await asyncio.to_thread(
        database.verify_secret, clean_secret, auth_row.get("password_hash", "")
    )

    # Local emergency recovery for built-in admin if owner updated HUB_PIN directly in .env
    if not matched and auth_row["id"] == "admin":
        env_pin = get_pin()
        if env_pin and hmac.compare_digest(clean_secret, env_pin):
            updated_admin = await database.update_user("admin", password=env_pin)
            if updated_admin:
                clear_failed_attempts(client_ip, clean_user)
                return updated_admin

    if not matched:
        record_failed_attempt(client_ip, clean_user)
        return None

    clear_failed_attempts(client_ip, clean_user)
    auth_row.pop("password_hash", None)
    return auth_row


def extract_request_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    cookie_tok = request.cookies.get(COOKIE_NAME)
    if cookie_tok:
        return cookie_tok.strip()
    return None


async def get_current_user(request: Request) -> dict[str, Any]:
    tok = extract_request_token(request)
    user = await resolve_token_user(tok)
    if not user:
        cookie_tok = request.cookies.get(COOKIE_NAME)
        if cookie_tok and cookie_tok.strip() != tok:
            user = await resolve_token_user(cookie_tok.strip())
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация (логин и пароль / ПИН-код)",
        )
    return user


async def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ разрешён только администратору",
        )
    return user


async def authenticate_ws_user(websocket: WebSocket) -> Optional[dict[str, Any]]:
    tok = websocket.query_params.get("token") or websocket.cookies.get(COOKIE_NAME)
    return await resolve_token_user(tok)


def generate_qr_svg(url: str) -> str:
    qr = segno.make(url, micro=False, error="m")
    buf = io.BytesIO()
    qr.save(
        buf,
        kind="svg",
        scale=6,
        border=2,
        dark="#09090b",
        light="#ffffff",
        xmldecl=False,
        omitsize=True,
    )
    return buf.getvalue().decode("utf-8")
