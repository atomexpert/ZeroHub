from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import proxy_router
from .config import (
    HUB_PORT,
    HUB_SECRET_KEY,
    ensure_user_cli_home,
    resolve_outbound_proxy,
)
from .database import (
    DEFAULT_OPENROUTER_SMART_TITLES_MODEL,
    add_openrouter_log,
    deduct_user_openrouter_cached_spend,
    extract_outline_title_source_text,
    get_openrouter_sidebar_summary,
    get_openrouter_spend_stats,
    get_user_openrouter_bridge_meta,
    get_user_openrouter_config,
    get_user_openrouter_secret_bundle,
    list_all_openrouter_bridge_metas,
    normalize_legacy_openrouter_rules,
    update_user_openrouter_cached_status,
    utc_now_iso,
)

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_TTL_SEC = 1800.0  # 30 minutes
OPENROUTER_MCP_HARD_DEADLINE_SEC = 162.0  # 18s headroom below agy.exe 3m0s tool timeout
OPENROUTER_RESPONSE_CACHE_TTL_SEC = 600.0  # 10 minutes
OPENROUTER_RESPONSE_CACHE_MAX_ITEMS = 64
MCP_SERVER_NAME = "hub_openrouter"
MCP_PROTOCOL_VERSION = "2025-03-26"
_SUPPORTED_MCP_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

# Allowed local & Docker-bridge networks for `/mcp/openrouter/{mcp_token}`
_ALLOWED_LOCAL_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.65.0/24"),
    ipaddress.ip_network("fd00::/8"),
    ipaddress.ip_network("fe80::/10"),
)

_MODELS_CACHE_LOCK = asyncio.Lock()
_MODELS_CACHE: dict[str, Any] = {
    "fetched_at": 0.0,
    "items": [],
    "by_id": {},
}

OPENROUTER_MIN_REFRESH_INTERVAL_SEC = 30.0

_WS_USER_NOTIFIER: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None
_ACTIVE_TURN_FINDER: Optional[Callable[..., Optional[dict[str, Any]]]] = None
_LIVE_REFRESH_IN_FLIGHT: set[str] = set()
_LAST_LIVE_REFRESH_MONO: dict[str, float] = {}
_BACKGROUND_REFRESH_TASKS: set[asyncio.Task[Any]] = set()
_RECENT_OR_RESPONSES: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

_SECRET_ASSIGN_RE = re.compile(
    r"""(?i)\b(HUB_SECRET_KEY|GITHUB_PUBLISH_TOKEN|OPENROUTER_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY)(\s*[:=]\s*["']?)([^\s"']{8,})(["']?)"""
)
_SECRET_TOKEN_RE = re.compile(
    r"(?:"
    r"sk-or-v1-[A-Za-z0-9_-]{16,}"
    r"|sk-ant-[A-Za-z0-9_-]{16,}"
    r"|sk-(?:proj-|live-|svcacct-)?[A-Za-z0-9_-]{20,}"
    r"|AIzaSy[A-Za-z0-9_-]{25,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r")"
)


def sanitize_outbound_openrouter_text(text: str) -> tuple[str, int]:
    """Mask accidentally embedded secrets/tokens in outbound OpenRouter prompts."""
    if not text:
        return "", 0
    redactions = 0

    def _mask_assign(m: re.Match[str]) -> str:
        nonlocal redactions
        val = m.group(3)
        if val == "[REDACTED_SECRET]":
            return m.group(0)
        redactions += 1
        return f"{m.group(1)}{m.group(2)}[REDACTED_SECRET]{m.group(4)}"

    def _mask_token(_m: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return "[REDACTED_SECRET]"

    out = _SECRET_ASSIGN_RE.sub(_mask_assign, str(text))
    out = _SECRET_TOKEN_RE.sub(_mask_token, out)
    return out, redactions


def _build_openrouter_response_cache_key(
    *,
    user_id: str,
    chosen_model: str,
    sanitized_prompt: str,
    sanitized_sys: str,
    reasoning_effort: str,
    max_tokens: Optional[int],
    temperature: Optional[float],
) -> str:
    raw_tuple = (
        str(user_id or "").strip(),
        str(chosen_model or "").strip().lower(),
        sanitized_prompt,
        sanitized_sys,
        str(reasoning_effort or "").strip().lower(),
        int(max_tokens) if max_tokens is not None else None,
        round(float(temperature), 4) if temperature is not None else None,
    )
    return hashlib.sha256(
        json.dumps(raw_tuple, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _get_cached_openrouter_response(cache_key: str) -> Optional[dict[str, Any]]:
    now = time.monotonic()
    expired = [
        k
        for k, (ts, _) in _RECENT_OR_RESPONSES.items()
        if (now - ts) > OPENROUTER_RESPONSE_CACHE_TTL_SEC
    ]
    for k in expired:
        _RECENT_OR_RESPONSES.pop(k, None)
    hit = _RECENT_OR_RESPONSES.get(cache_key)
    if hit is None:
        return None
    ts, saved = hit
    if (now - ts) > OPENROUTER_RESPONSE_CACHE_TTL_SEC:
        _RECENT_OR_RESPONSES.pop(cache_key, None)
        return None
    _RECENT_OR_RESPONSES.move_to_end(cache_key)
    cloned = dict(saved)
    cloned["cost_usd"] = 0.0
    cloned["latency_ms"] = 0
    cloned["cached_retry"] = True
    return cloned


def _put_cached_openrouter_response(cache_key: str, res: dict[str, Any]) -> None:
    now = time.monotonic()
    _RECENT_OR_RESPONSES[cache_key] = (now, dict(res))
    _RECENT_OR_RESPONSES.move_to_end(cache_key)
    while len(_RECENT_OR_RESPONSES) > OPENROUTER_RESPONSE_CACHE_MAX_ITEMS:
        _RECENT_OR_RESPONSES.popitem(last=False)


def set_openrouter_ws_notifier(
    fn: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> None:
    global _WS_USER_NOTIFIER
    _WS_USER_NOTIFIER = fn


def set_openrouter_active_turn_finder(
    fn: Callable[[str], Optional[dict[str, str]]],
) -> None:
    global _ACTIVE_TURN_FINDER
    _ACTIVE_TURN_FINDER = fn


async def notify_openrouter_user(
    user_id: str,
    *,
    log_entry: Optional[dict[str, Any]] = None,
) -> None:
    if not _WS_USER_NOTIFIER or not user_id:
        return
    try:
        summary = await get_openrouter_sidebar_summary(user_id)
        payload: dict[str, Any] = {
            "type": "openrouter_updated",
            "summary": summary,
        }
        if log_entry is not None:
            payload["log"] = log_entry
        await _WS_USER_NOTIFIER(user_id, payload)
    except Exception:
        pass


def _resolve_active_turn_context(user_id: str, chat_id: str = "") -> dict[str, str]:
    finder = _ACTIVE_TURN_FINDER
    if finder is None:
        try:
            from .agy_engine import find_active_turn_for_user as finder
        except Exception:
            finder = None
    if finder and user_id:
        try:
            clean_cid = (chat_id or "").strip()
            try:
                ctx = finder(user_id, clean_cid) if clean_cid else finder(user_id)
            except TypeError:
                ctx = finder(user_id)
            if isinstance(ctx, dict):
                return {
                    "chat_id": str(ctx.get("chat_id") or ""),
                    "chat_title": str(ctx.get("chat_title") or ""),
                    "project_name": str(ctx.get("project_name") or ""),
                    "caller_model": str(ctx.get("caller_model") or ""),
                    "caller_engine": str(ctx.get("caller_engine") or ""),
                    "caller_family": str(ctx.get("caller_family") or ""),
                }
        except Exception:
            pass
    return {
        "chat_id": "",
        "chat_title": "",
        "project_name": "",
        "caller_model": "",
        "caller_engine": "",
        "caller_family": "",
    }


# ---------------------------------------------------------------------------
# HMAC-SHA256 MCP Bridge Tokens & Local-Only Network Verification
# ---------------------------------------------------------------------------


def _compute_mcp_signature(user_id: str, key_nonce: str) -> str:
    msg = f"openrouter-mcp:v1:{user_id}:{key_nonce}".encode("utf-8")
    return hmac.new(HUB_SECRET_KEY.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def build_openrouter_mcp_token(user_id: str, key_nonce: str) -> str:
    clean_uid = (user_id or "").strip()
    clean_nonce = (key_nonce or "").strip()
    if not clean_uid or not clean_nonce:
        raise ValueError("user_id и key_nonce обязательны для выпуска MCP-токена")
    uid_b64 = base64.urlsafe_b64encode(clean_uid.encode("utf-8")).decode("ascii").rstrip("=")
    sig = _compute_mcp_signature(clean_uid, clean_nonce)
    return f"{uid_b64}.{sig}"


def parse_openrouter_mcp_token_user(mcp_token: str) -> Optional[tuple[str, str]]:
    raw = (mcp_token or "").strip()
    if "." not in raw or len(raw) > 256:
        return None
    uid_b64, sig = raw.split(".", 1)
    if not uid_b64 or len(sig) != 64:
        return None
    pad = "=" * ((4 - len(uid_b64) % 4) % 4)
    try:
        user_id = base64.urlsafe_b64decode((uid_b64 + pad).encode("ascii")).decode("utf-8").strip()
    except Exception:
        return None
    if not user_id:
        return None
    return user_id, sig


async def verify_openrouter_mcp_token(mcp_token: str) -> Optional[dict[str, Any]]:
    """Verify HMAC-SHA256 token, user activity, key_nonce match, is_enabled, and decrypted key."""
    parsed = parse_openrouter_mcp_token_user(mcp_token)
    if not parsed:
        return None
    user_id, candidate_sig = parsed
    bundle = await get_user_openrouter_secret_bundle(user_id)
    if not bundle:
        return None
    if not bundle.get("user_is_active") or not bundle.get("has_key") or not bundle.get("is_enabled"):
        return None
    nonce = str(bundle.get("key_nonce") or "").strip()
    api_key = str(bundle.get("api_key") or "").strip()
    if not nonce or not api_key:
        return None
    expected_sig = _compute_mcp_signature(user_id, nonce)
    if not hmac.compare_digest(expected_sig, candidate_sig):
        return None
    return bundle


def is_local_mcp_ip(host: str) -> bool:
    raw = (host or "").strip()
    if not raw:
        return False
    if raw.lower() == "localhost":
        return True
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _ALLOWED_LOCAL_NETWORKS)


def is_local_mcp_request(request: Request) -> bool:
    """Reject reverse-proxied external traffic and allow only loopback & local Docker bridge IPs."""
    for proxy_hdr in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "true-client-ip"):
        if (request.headers.get(proxy_hdr) or "").strip():
            return False
    client_host = request.client.host if request.client else ""
    return is_local_mcp_ip(client_host)


# ---------------------------------------------------------------------------
# Contour 1: Progressive CLI Sync (`mcp_config.json` + `openrouter-bridge/SKILL.md`)
# ---------------------------------------------------------------------------


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists() and target.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    tmp = target.parent / f".{target.name}.{os.getpid()}.tmp"
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, target)


def _sync_single_mcp_config_file(
    cfg_file: Path,
    *,
    server_url: Optional[str],
) -> None:
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
        servers[MCP_SERVER_NAME] = {"url": server_url}
        existing_root["mcpServers"] = servers
        rendered = json.dumps(existing_root, ensure_ascii=False, indent=2) + "\n"
        _atomic_write_text(cfg_file, rendered)
    else:
        servers.pop(MCP_SERVER_NAME, None)
        other_root_keys = {k for k in existing_root.keys() if k != "mcpServers"}
        if not servers and not other_root_keys:
            cfg_file.unlink(missing_ok=True)
        else:
            existing_root["mcpServers"] = servers
            rendered = json.dumps(existing_root, ensure_ascii=False, indent=2) + "\n"
            _atomic_write_text(cfg_file, rendered)


def _build_openrouter_skill_markdown(meta: dict[str, Any]) -> str:
    default_model = str(meta.get("default_model") or "").strip()
    max_price = float(meta.get("max_price_per_m") or 0.0)
    daily_limit = float(meta.get("daily_spend_limit_usd") or 0.0)
    custom_rules = normalize_legacy_openrouter_rules(str(meta.get("custom_rules") or "").strip())

    model_line = (
        f"`{default_model}` (используется автоматически, если не передать `model` и не совпадает с твоим вендором)"
        if default_model
        else "`openrouter/auto` (сервер автоматически подберёт свежую модель другого семейства в рамках бюджета)"
    )
    guard_lines: list[str] = []
    if max_price > 0:
        guard_lines.append(
            f"- Потолок цены модели: не дороже **${max_price:g} / 1M** входных или выходных токенов."
        )
    if daily_limit > 0:
        guard_lines.append(
            f"- Суточный лимит трат ключа в Hub: **${daily_limit:g} / день (UTC)**."
        )
    if not guard_lines:
        guard_lines.append("- Ценовые и суточные ограничения в Hub: без искусственных лимитов (при автовыборе без `max_price_per_m` используются только `:free` модели).")

    rules_block = ""
    if custom_rules:
        rules_block = (
            "\n## Персональные правила пользователя для OpenRouter\n"
            "Эти правила также автоматически инжектируются сервером в `system`-промпт каждого вызова:\n\n"
            f"{custom_rules}\n"
        )

    return (
        "---\n"
        "name: openrouter-bridge\n"
        "description: >-\n"
        "  Персональный шлюз OpenRouter (MCP-сервер hub_openrouter): вызов любых внешних LLM\n"
        "  через openrouter_chat, параллельный консилиум 2–4 моделей через openrouter_multi_chat\n"
        "  и поиск актуальных ID моделей и цен $/1M через openrouter_models. Активируй, когда\n"
        "  пользователь просит обратиться к OpenRouter, внешней модели, получить второе мнение\n"
        "  архитектора или сравнить ответы нескольких нейросетей.\n"
        "---\n\n"
        "# Шлюз OpenRouter (`hub_openrouter`)\n\n"
        "У пользователя подключён личный изолированный мост к **OpenRouter API**.\n\n"
        "## Доступные MCP-инструменты\n"
        "1. **`openrouter_chat`** — одиночный запрос к внешней модели (кросс-вендорное второе мнение).\n"
        "2. **`openrouter_multi_chat`** — параллельный опрос **2..4 моделей** за один шаг («консилиум / второе мнение»).\n"
        "3. **`openrouter_models`** — быстрый поиск актуальных свежих `id` моделей, размера контекста и цен `$/1M`.\n\n"
        "## Текущие настройки профиля\n"
        f"- Модель по умолчанию: {model_line}\n"
        + "\n".join(guard_lines)
        + "\n\n"
        "## Рекомендации по вызову\n"
        "- Вызовы OpenRouter **не видят локальные файлы проекта сами по себе**: всегда передавай в `prompt` "
        "необходимый фрагмент кода, стек ошибки или ТЗ целиком.\n"
        "- **Кросс-вендорный выбор (`explicit_user_choice`):** при самостоятельном поиске второго мнения передавай "
        "`explicit_user_choice=false` (по умолчанию) и выбирай модель **другого** семейства (не вызывай устаревшие поколения "
        "или `:batch` очереди). Устанавливай `explicit_user_choice=true` ТОЛЬКО если человек в сообщении явно назвал конкретную модель.\n"
        "- **Бюджет времени (`162 с`):** для тяжёлых рассуждающих моделей ставь `reasoning_effort=\"medium\"`, "
        "чтобы гарантированно уложиться в дедлайн `162 с` MCP-шлюза.\n"
        "- Если не уверен в точном slug модели — сначала вызови `openrouter_models(search=...)`.\n"
        "- Для сложной архитектуры, ревью безопасности или спорного бага используй `openrouter_multi_chat` "
        "с 2–3 разными семействами моделей и синтезируй итоговый вывод для пользователя.\n"
        + rules_block
    )


def sync_user_openrouter_cli_files_from_meta(meta: dict[str, Any]) -> None:
    """Sync or clean `mcp_config.json` and `openrouter-bridge/SKILL.md`."""
    user_id = str(meta.get("user_id") or "").strip()
    if not user_id:
        return
    home_dir = ensure_user_cli_home(user_id)
    config_dir = home_dir / ".gemini" / "config"
    mcp_file = config_dir / "mcp_config.json"
    mcp_docker_file = config_dir / "mcp_config.docker.json"
    skill_dir = config_dir / "skills" / "openrouter-bridge"
    skill_file = skill_dir / "SKILL.md"
    agents_skill_dir = home_dir / ".agents" / "skills" / "openrouter-bridge"
    agents_skill_file = agents_skill_dir / "SKILL.md"
    claude_skill_dir = home_dir / ".claude" / "skills" / "openrouter-bridge"
    claude_skill_file = claude_skill_dir / "SKILL.md"
    claude_accounts_dir = home_dir / ".claude" / "accounts"

    # Legacy cleanup: agy.exe always runs on host; mcp_config.docker.json is no longer used.
    mcp_docker_file.unlink(missing_ok=True)

    is_active_bridge = bool(
        meta.get("has_key")
        and meta.get("is_enabled")
        and str(meta.get("key_nonce") or "").strip()
    )

    if is_active_bridge:
        token = build_openrouter_mcp_token(user_id, str(meta["key_nonce"]))
        local_url = f"http://127.0.0.1:{HUB_PORT}/mcp/openrouter/{token}"
        _sync_single_mcp_config_file(mcp_file, server_url=local_url)
        skill_md = _build_openrouter_skill_markdown(meta)
        _atomic_write_text(skill_file, skill_md)
        _atomic_write_text(agents_skill_file, skill_md)
        _atomic_write_text(claude_skill_file, skill_md)
        if claude_accounts_dir.is_dir():
            for child in claude_accounts_dir.iterdir():
                if child.is_dir() and not child.name.startswith("_pending_"):
                    try:
                        _atomic_write_text(child / "skills" / "openrouter-bridge" / "SKILL.md", skill_md)
                    except OSError:
                        pass
    else:
        _sync_single_mcp_config_file(mcp_file, server_url=None)
        skill_file.unlink(missing_ok=True)
        agents_skill_file.unlink(missing_ok=True)
        claude_skill_file.unlink(missing_ok=True)
        extra_dirs: list[Path] = []
        if claude_accounts_dir.is_dir():
            for child in claude_accounts_dir.iterdir():
                if child.is_dir():
                    acc_sk = child / "skills" / "openrouter-bridge"
                    (acc_sk / "SKILL.md").unlink(missing_ok=True)
                    extra_dirs.append(acc_sk)
        for sdir in (skill_dir, agents_skill_dir, claude_skill_dir, *extra_dirs):
            try:
                if sdir.exists() and not any(sdir.iterdir()):
                    sdir.rmdir()
            except OSError:
                pass


async def sync_user_openrouter_cli_files(user_id: str) -> dict[str, Any]:
    meta = await get_user_openrouter_bridge_meta(user_id)
    sync_user_openrouter_cli_files_from_meta(meta)
    return meta


async def sync_all_users_openrouter_cli_files() -> None:
    metas = await list_all_openrouter_bridge_metas()
    for meta in metas:
        try:
            sync_user_openrouter_cli_files_from_meta(meta)
        except Exception:
            pass


async def is_user_openrouter_bridge_active(user_id: str) -> bool:
    if not user_id:
        return False
    try:
        meta = await get_user_openrouter_bridge_meta(user_id)
        return bool(meta.get("has_key") and meta.get("is_enabled") and meta.get("key_nonce"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Outbound OpenRouter HTTP Client (0% GPU, stdlib urllib + outbound proxy)
# ---------------------------------------------------------------------------


_UNSET_PROXY = object()


def _build_url_opener(proxy_url: Any = _UNSET_PROXY) -> urllib.request.OpenerDirector:
    eff_proxy = resolve_outbound_proxy() if proxy_url is _UNSET_PROXY else proxy_url
    return proxy_router.build_urllib_proxy_opener(eff_proxy)


def _openrouter_http_sync(
    method: str,
    path: str,
    *,
    api_key: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    timeout: float = 90.0,
    deadline_mono: Optional[float] = None,
    proxy_url: Any = _UNSET_PROXY,
) -> tuple[int, dict[str, Any]]:
    if deadline_mono is not None and time.monotonic() > deadline_mono:
        raise TimeoutError("Превышен дедлайн MCP-шлюза OpenRouter (162 с)")
    sock_timeout = (
        min(float(timeout), max(3.0, deadline_mono - time.monotonic()))
        if deadline_mono is not None
        else float(timeout)
    )
    url = path if path.startswith("http") else f"{OPENROUTER_API_BASE}{path}"
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "AntigravityHub/91",
        "HTTP-Referer": f"http://127.0.0.1:{HUB_PORT}",
        "X-Title": "Antigravity Hub",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body_bytes: Optional[bytes] = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
    opener = _build_url_opener(proxy_url)
    try:
        with opener.open(req, timeout=sock_timeout) as resp:
            status_code = int(getattr(resp, "status", 200) or 200)
            chunks: list[bytes] = []
            while True:
                if deadline_mono is not None and time.monotonic() > deadline_mono:
                    raise TimeoutError("Превышен дедлайн MCP-шлюза OpenRouter при чтении ответа (162 с)")
                chunk = resp.read(16384)
                if not chunk:
                    break
                chunks.append(chunk)
            if deadline_mono is not None and time.monotonic() > deadline_mono:
                raise TimeoutError("Превышен дедлайн MCP-шлюза OpenRouter (162 с)")
            raw = b"".join(chunks).decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw.strip() else {}
            return status_code, parsed if isinstance(parsed, dict) else {"data": parsed}
    except urllib.error.HTTPError as exc:
        try:
            status_code = int(exc.code or 500)
            raw_err = ""
            try:
                raw_err = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw_err = str(exc.reason or "")
            try:
                parsed_err = json.loads(raw_err) if raw_err.strip() else {}
                if not isinstance(parsed_err, dict):
                    parsed_err = {"error": {"message": str(parsed_err)}}
            except Exception:
                parsed_err = {"error": {"message": raw_err.strip() or f"HTTP {status_code}"}}
            return status_code, parsed_err
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise TimeoutError(str(exc.reason)) from exc
        raise


def _extract_openrouter_error_message(status_code: int, data: dict[str, Any]) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        msg = str(err.get("message") or "").strip()
        meta = err.get("metadata")
        raw_provider = ""
        if isinstance(meta, dict):
            raw_provider = str(meta.get("raw") or meta.get("provider_name") or "").strip()
        if msg and raw_provider and raw_provider not in msg:
            return f"{msg} ({raw_provider[:180]})"
        if msg:
            return msg
    elif isinstance(err, str) and err.strip():
        return err.strip()
    return f"Ошибка OpenRouter HTTP {status_code}"


def _opt_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return round(float(val), 6)
    except (TypeError, ValueError):
        return None


async def fetch_openrouter_key_and_credits(api_key: str) -> dict[str, Any]:
    """Validate key via `GET /api/v1/key` and enrich wallet balance via parallel `GET /api/v1/credits`."""
    clean_key = (api_key or "").strip()
    if not clean_key:
        raise ValueError("API-ключ OpenRouter пуст")

    try:
        proxy_url = await proxy_router.ensure_proxy_for_context("global")
    except proxy_router.ProxyRouteError as exc:
        raise ValueError(str(exc)) from exc

    key_task = asyncio.to_thread(
        _openrouter_http_sync,
        "GET",
        "/key",
        api_key=clean_key,
        timeout=18.0,
        proxy_url=proxy_url,
    )
    credits_task = asyncio.to_thread(
        _openrouter_http_sync,
        "GET",
        "/credits",
        api_key=clean_key,
        timeout=15.0,
        proxy_url=proxy_url,
    )
    results = await asyncio.gather(key_task, credits_task, return_exceptions=True)

    key_res = results[0]
    if isinstance(key_res, Exception):
        raise ValueError(f"Не удалось связаться с OpenRouter API: {key_res}")

    key_status, key_payload = key_res
    if key_status in (401, 403):
        raise ValueError("Ключ OpenRouter отклонён (401/403). Проверьте корректность sk-or-v1-...")
    if key_status >= 400:
        raise ValueError(_extract_openrouter_error_message(key_status, key_payload))

    key_data = key_payload.get("data") if isinstance(key_payload.get("data"), dict) else key_payload
    if not isinstance(key_data, dict):
        key_data = {}

    total_credits: Optional[float] = None
    total_credits_usage: Optional[float] = None
    credits_remaining: Optional[float] = None

    credits_res = results[1]
    if not isinstance(credits_res, Exception):
        c_status, c_payload = credits_res
        if 200 <= c_status < 300 and isinstance(c_payload, dict):
            c_data = c_payload.get("data") if isinstance(c_payload.get("data"), dict) else c_payload
            if isinstance(c_data, dict) and c_data.get("total_credits") is not None:
                total_credits = _opt_float(c_data.get("total_credits"))
                total_credits_usage = _opt_float(c_data.get("total_usage")) or 0.0
                if total_credits is not None:
                    credits_remaining = round(max(0.0, total_credits - total_credits_usage), 6)

    return {
        "label": str(key_data.get("label") or ""),
        "limit": _opt_float(key_data.get("limit")),
        "limit_remaining": _opt_float(key_data.get("limit_remaining")),
        "limit_reset": str(key_data.get("limit_reset") or ""),
        "include_byok_in_limit": bool(key_data.get("include_byok_in_limit", False)),
        "usage": _opt_float(key_data.get("usage")) or 0.0,
        "usage_daily": _opt_float(key_data.get("usage_daily")) or 0.0,
        "usage_weekly": _opt_float(key_data.get("usage_weekly")) or 0.0,
        "usage_monthly": _opt_float(key_data.get("usage_monthly")) or 0.0,
        "byok_usage": _opt_float(key_data.get("byok_usage")) or 0.0,
        "is_free_tier": bool(key_data.get("is_free_tier", False)),
        "is_provisioning_key": bool(key_data.get("is_provisioning_key", False)),
        "total_credits": total_credits,
        "total_credits_usage": total_credits_usage,
        "credits_remaining": credits_remaining,
        "checked_at": utc_now_iso(),
    }


def _cached_status_age_sec(cached_at_iso: str) -> Optional[float]:
    raw = (cached_at_iso or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None


async def refresh_user_openrouter_live_status(
    user_id: str,
    *,
    force: bool = False,
    max_age_sec: float = 180.0,
) -> Optional[dict[str, Any]]:
    """Fetch fresh `/key` + `/credits` for `user_id` if stale (or forced), persist & push WS update."""
    clean_uid = (user_id or "").strip()
    if not clean_uid or clean_uid in _LIVE_REFRESH_IN_FLIGHT:
        return None

    now_mono = time.monotonic()
    last_mono = _LAST_LIVE_REFRESH_MONO.get(clean_uid)
    if last_mono is not None and (now_mono - last_mono) < OPENROUTER_MIN_REFRESH_INTERVAL_SEC:
        return None

    try:
        cfg = await get_user_openrouter_config(clean_uid)
    except Exception:
        return None
    if not cfg.get("has_key"):
        return None

    if not force:
        age_sec = _cached_status_age_sec(str(cfg.get("cached_at") or ""))
        if age_sec is not None and age_sec < float(max_age_sec):
            return None

    if clean_uid in _LIVE_REFRESH_IN_FLIGHT:
        return None
    last_mono = _LAST_LIVE_REFRESH_MONO.get(clean_uid)
    if last_mono is not None and (time.monotonic() - last_mono) < OPENROUTER_MIN_REFRESH_INTERVAL_SEC:
        return None

    _LIVE_REFRESH_IN_FLIGHT.add(clean_uid)
    try:
        bundle = await get_user_openrouter_secret_bundle(clean_uid)
        if not bundle or not bundle.get("api_key"):
            return None
        _LAST_LIVE_REFRESH_MONO[clean_uid] = time.monotonic()
        live_status = await fetch_openrouter_key_and_credits(str(bundle["api_key"]))
        await update_user_openrouter_cached_status(clean_uid, live_status)
        await notify_openrouter_user(clean_uid)
        return live_status
    except Exception:
        return None
    finally:
        _LIVE_REFRESH_IN_FLIGHT.discard(clean_uid)


def schedule_openrouter_live_refresh(
    user_id: str,
    *,
    force: bool = False,
    max_age_sec: float = 180.0,
) -> bool:
    """Fire-and-forget non-blocking live OpenRouter status refresh inside a running event loop."""
    clean_uid = (user_id or "").strip()
    if not clean_uid or clean_uid in _LIVE_REFRESH_IN_FLIGHT:
        return False
    last_mono = _LAST_LIVE_REFRESH_MONO.get(clean_uid)
    if last_mono is not None and (time.monotonic() - last_mono) < OPENROUTER_MIN_REFRESH_INTERVAL_SEC:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = loop.create_task(
        refresh_user_openrouter_live_status(
            clean_uid,
            force=force,
            max_age_sec=max_age_sec,
        )
    )
    _BACKGROUND_REFRESH_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_REFRESH_TASKS.discard)
    return True


# ---------------------------------------------------------------------------
# 30-Minute Cached Models Catalog (`GET /api/v1/models`)
# ---------------------------------------------------------------------------


_DEPRECATED_OPENROUTER_MODEL_PATTERNS: tuple[str, ...] = (
    "openai/gpt-3.5",
    "openai/gpt-4-0",
    "openai/gpt-4-turbo",
    "openai/gpt-4o-2024",
    "openai/gpt-4o-mini-2024",
    "openai/o1-preview",
    "openai/o1-mini",
    "anthropic/claude-2",
    "anthropic/claude-instant",
    "anthropic/claude-3-",
    "anthropic/claude-3.5",
    "google/gemini-pro",
    "google/gemini-1.",
    "google/gemini-2.0",
    "meta-llama/llama-2",
    "meta-llama/llama-3-",
    "mistralai/mistral-7b",
)


def is_deprecated_openrouter_model(
    model_id: str,
    name: str = "",
    created: int = 0,
) -> bool:
    """Return True if `model_id` / `name` belongs to an obsolete/deprecated generation."""
    _ = created
    mid_l = str(model_id or "").strip().lower()
    name_l = str(name or "").strip().lower()
    if "deprecated" in name_l or "obsolete" in name_l:
        return True
    if not mid_l:
        return False
    return any(mid_l.startswith(pat) or pat in mid_l for pat in _DEPRECATED_OPENROUTER_MODEL_PATTERNS)


def _map_openrouter_vendor_to_family(model_id: str) -> str:
    mid_l = str(model_id or "").strip().lower()
    if "/" not in mid_l:
        return "other"
    vendor = mid_l.split("/", 1)[0].strip()
    if vendor == "openai":
        return "gpt"
    if vendor == "google":
        return "gemini"
    if vendor == "anthropic":
        return "claude"
    return vendor or "other"


def _normalize_catalog_model(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    model_id = str(raw.get("id") or "").strip()
    if not model_id:
        return None
    name = str(raw.get("name") or model_id).strip()
    try:
        created = int(raw.get("created") or 0)
    except (TypeError, ValueError):
        created = 0
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    raw_p = pricing.get("prompt")
    raw_c = pricing.get("completion")
    is_dynamic = False
    try:
        p_per_tok = float(raw_p or 0.0)
        if p_per_tok < 0:
            is_dynamic = True
            p_per_tok = 0.0
    except (TypeError, ValueError):
        p_per_tok = 0.0
    try:
        c_per_tok = float(raw_c or 0.0)
        if c_per_tok < 0:
            is_dynamic = True
            c_per_tok = 0.0
    except (TypeError, ValueError):
        c_per_tok = 0.0

    prompt_per_m = round(p_per_tok * 1_000_000.0, 4)
    comp_per_m = round(c_per_tok * 1_000_000.0, 4)
    top_provider = raw.get("top_provider") if isinstance(raw.get("top_provider"), dict) else {}
    arch = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    ctx_len = int(raw.get("context_length") or top_provider.get("context_length") or 0)
    max_comp = int(top_provider.get("max_completion_tokens") or 0)
    supported_params = raw.get("supported_parameters")
    if not isinstance(supported_params, list):
        supported_params = []

    is_batch_only = model_id.lower().endswith(":batch")
    is_deprecated = is_deprecated_openrouter_model(model_id, name, created)

    return {
        "id": model_id,
        "name": name,
        "created": created,
        "is_batch_only": is_batch_only,
        "is_deprecated": is_deprecated,
        "context_length": ctx_len,
        "max_completion_tokens": max_comp,
        "prompt_price_per_m": prompt_per_m,
        "completion_price_per_m": comp_per_m,
        "max_side_price_per_m": max(prompt_per_m, comp_per_m),
        "is_free": (not is_dynamic and prompt_per_m == 0.0 and comp_per_m == 0.0),
        "is_dynamic_pricing": is_dynamic,
        "modality": str(arch.get("modality") or ""),
        "supports_reasoning": ("reasoning" in supported_params or "include_reasoning" in supported_params),
    }


async def get_openrouter_models_catalog(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    now_mono = time.monotonic()
    if (
        not force_refresh
        and _MODELS_CACHE["items"]
        and (now_mono - float(_MODELS_CACHE["fetched_at"])) < OPENROUTER_MODELS_TTL_SEC
    ):
        return _MODELS_CACHE["items"]

    async with _MODELS_CACHE_LOCK:
        now_mono = time.monotonic()
        if (
            not force_refresh
            and _MODELS_CACHE["items"]
            and (now_mono - float(_MODELS_CACHE["fetched_at"])) < OPENROUTER_MODELS_TTL_SEC
        ):
            return _MODELS_CACHE["items"]

        try:
            proxy_url = await proxy_router.ensure_proxy_for_context("global")
            status_code, payload = await asyncio.to_thread(
                _openrouter_http_sync,
                "GET",
                "/models",
                timeout=20.0,
                proxy_url=proxy_url,
            )
            raw_list = payload.get("data") if isinstance(payload, dict) else []
            if 200 <= status_code < 300 and isinstance(raw_list, list):
                items: list[dict[str, Any]] = []
                by_id: dict[str, dict[str, Any]] = {}
                for raw_item in raw_list:
                    if not isinstance(raw_item, dict):
                        continue
                    norm = _normalize_catalog_model(raw_item)
                    if norm:
                        items.append(norm)
                        by_id[norm["id"].lower()] = norm
                if items:
                    _MODELS_CACHE["items"] = items
                    _MODELS_CACHE["by_id"] = by_id
                    _MODELS_CACHE["fetched_at"] = time.monotonic()
        except Exception:
            pass

        return _MODELS_CACHE["items"]


async def lookup_catalog_model(model_id: str) -> Optional[dict[str, Any]]:
    clean = (model_id or "").strip().lower()
    if not clean:
        return None
    items = await get_openrouter_models_catalog()
    by_id: dict[str, dict[str, Any]] = dict(_MODELS_CACHE.get("by_id") or {})
    if items and clean not in by_id:
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                by_id.setdefault(str(item["id"]).lower(), item)
    if clean in by_id:
        return by_id[clean]
    if ":" in clean:
        base, suffix = clean.split(":", 1)
        if suffix == "free":
            base_info = by_id.get(base)
            if base_info:
                patched = dict(base_info)
                patched["id"] = model_id.strip()
                patched["prompt_price_per_m"] = 0.0
                patched["completion_price_per_m"] = 0.0
                patched["max_side_price_per_m"] = 0.0
                patched["is_free"] = True
                return patched
            return {
                "id": model_id.strip(),
                "name": model_id.strip(),
                "created": 0,
                "is_batch_only": False,
                "is_deprecated": is_deprecated_openrouter_model(model_id.strip()),
                "context_length": 0,
                "max_completion_tokens": 0,
                "prompt_price_per_m": 0.0,
                "completion_price_per_m": 0.0,
                "max_side_price_per_m": 0.0,
                "is_free": True,
                "is_dynamic_pricing": False,
                "modality": "",
                "supports_reasoning": False,
            }
        if suffix == "batch" and base in by_id:
            patched = dict(by_id[base])
            patched["id"] = model_id.strip()
            patched["is_batch_only"] = True
            return patched
        if base in by_id:
            return by_id[base]
    return None


def _is_catalog_item_excluded_fresh(m: dict[str, Any]) -> bool:
    mid = str(m.get("id") or "").strip()
    if not mid:
        return True
    if bool(m.get("is_batch_only")) or mid.lower().endswith(":batch"):
        return True
    if bool(m.get("is_deprecated")) or is_deprecated_openrouter_model(
        mid,
        str(m.get("name") or ""),
        int(m.get("created") or 0),
    ):
        return True
    return False


def _select_fresh_cross_vendor_candidates(
    items: list[dict[str, Any]],
    *,
    caller_family: str,
    max_price_limit: float,
    require_free: bool = False,
    limit: int = 3,
) -> list[dict[str, Any]]:
    popular_prefixes = (
        "anthropic/claude-",
        "openai/gpt-",
        "openai/o3",
        "openai/o4",
        "google/gemini-",
        "deepseek/deepseek-",
        "qwen/qwen",
        "x-ai/grok-",
    )
    scored: list[tuple[int, int, str, dict[str, Any]]] = []
    for m in items:
        if not isinstance(m, dict) or _is_catalog_item_excluded_fresh(m):
            continue
        mid = str(m.get("id") or "").strip()
        mid_l = mid.lower()
        if mid_l == "openrouter/auto":
            continue
        vendor_fam = _map_openrouter_vendor_to_family(mid)
        if caller_family in ("gemini", "gpt", "claude") and vendor_fam == caller_family:
            continue
        is_free = bool(
            m.get("is_free")
            or (
                not m.get("is_dynamic_pricing")
                and float(m.get("prompt_price_per_m") or 0.0) == 0.0
                and float(m.get("completion_price_per_m") or 0.0) == 0.0
            )
        )
        if require_free:
            if not is_free:
                continue
        elif max_price_limit > 0.0:
            if bool(m.get("is_dynamic_pricing")):
                continue
            if (
                float(m.get("prompt_price_per_m") or 0.0) > max_price_limit
                or float(m.get("completion_price_per_m") or 0.0) > max_price_limit
            ):
                continue
        score = 0
        if any(mid_l.startswith(pref) for pref in popular_prefixes):
            score += 100
        if int(m.get("context_length") or 0) >= 128_000:
            score += 25
        if bool(m.get("supports_reasoning")):
            score += 15
        scored.append((score, int(m.get("created") or 0), vendor_fam, m))

    scored.sort(key=lambda row: (-row[0], -row[1], str(row[3].get("id") or "")))
    out: list[dict[str, Any]] = []
    seen_vendors: set[str] = set()
    # First pass: 1 per vendor family for diversity
    for _, _, vfam, item in scored:
        if vfam not in seen_vendors:
            seen_vendors.add(vfam)
            out.append(item)
            if len(out) >= limit:
                return out
    # Second pass: fill up to limit
    seen_ids = {str(x["id"]).lower() for x in out}
    for _, _, _, item in scored:
        if str(item["id"]).lower() not in seen_ids:
            seen_ids.add(str(item["id"]).lower())
            out.append(item)
            if len(out) >= limit:
                break
    return out


async def search_openrouter_models(
    *,
    query: str = "",
    max_price_per_m: float = 0.0,
    limit: int = 30,
    include_deprecated: bool = False,
) -> list[dict[str, Any]]:
    items = await get_openrouter_models_catalog()
    q = (query or "").strip().lower()
    tokens = [t for t in q.split() if t]
    cap = max(1, min(int(limit or 30), 80))
    price_cap = max(0.0, float(max_price_per_m or 0.0))

    scored: list[tuple[int, dict[str, Any]]] = []
    popular_prefixes = (
        "anthropic/claude-",
        "openai/gpt-",
        "openai/o3",
        "openai/o4",
        "google/gemini-",
        "deepseek/deepseek-",
        "qwen/qwen",
        "x-ai/grok-",
        "mistralai/",
        "meta-llama/",
    )

    for m in items:
        if not include_deprecated and _is_catalog_item_excluded_fresh(m):
            continue
        if price_cap > 0.0:
            if m.get("is_dynamic_pricing"):
                continue
            if float(m.get("prompt_price_per_m") or 0.0) > price_cap or float(m.get("completion_price_per_m") or 0.0) > price_cap:
                continue

        mid_l = m["id"].lower()
        name_l = m["name"].lower()
        hay = f"{mid_l} {name_l}"
        if tokens and not all(tok in hay for tok in tokens):
            continue

        score = 0
        if q:
            if mid_l == q:
                score += 1000
            elif mid_l.startswith(q):
                score += 400
            elif f"/{q}" in mid_l:
                score += 300
            elif q in mid_l:
                score += 200
            elif q in name_l:
                score += 100
        if any(mid_l.startswith(pref) for pref in popular_prefixes):
            score += 50
        if m["context_length"] >= 128_000:
            score += 10
        scored.append((score, m))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return [pair[1] for pair in scored[:cap]]


# ---------------------------------------------------------------------------
# Contour 2: Server-Side Guardrails, Rules Injection & Call Execution
# ---------------------------------------------------------------------------


def _extract_assistant_content(choice_msg: dict[str, Any]) -> str:
    raw_content = choice_msg.get("content")
    text_out = ""
    if isinstance(raw_content, str):
        text_out = raw_content
    elif isinstance(raw_content, list):
        parts: list[str] = []
        for item in raw_content:
            if isinstance(item, dict) and item.get("type") in ("text", "output_text", None):
                txt = str(item.get("text") or item.get("content") or "")
                if txt:
                    parts.append(txt)
            elif isinstance(item, str):
                parts.append(item)
        text_out = "\n".join(parts)
    return text_out.strip()


async def _compute_effective_today_spend_usd(bundle: dict[str, Any]) -> float:
    user_id = str(bundle.get("user_id") or "")
    stats = await get_openrouter_spend_stats(user_id)
    local_today = float(stats.get("today_usd") or 0.0)
    cached = bundle.get("cached_status") if isinstance(bundle.get("cached_status"), dict) else {}
    cached_at = str(bundle.get("cached_at") or "")
    today_start = (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    upstream_today = (
        float(cached.get("usage_daily") or 0.0)
        if cached_at >= today_start and cached.get("usage_daily") is not None
        else 0.0
    )
    return max(local_today, upstream_today)


async def execute_openrouter_chat(
    bundle: dict[str, Any],
    *,
    prompt: str,
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    explicit_user_choice: bool = False,
    chat_id: str = "",
    deadline_mono: Optional[float] = None,
    skip_daily_check: bool = False,
    purpose: str = "mcp_chat",
) -> dict[str, Any]:
    """Execute one OpenRouter chat completion with server-enforced rules, guardrails & telemetry."""
    user_id = str(bundle.get("user_id") or "").strip()
    api_key = str(bundle.get("api_key") or "").strip()
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return {
            "ok": False,
            "status": "error",
            "error": "Параметр `prompt` не может быть пустым.",
        }

    # Unconditional secret sanitization BEFORE any outbound network call or DB log
    sanitized_prompt, _ = sanitize_outbound_openrouter_text(clean_prompt)
    sanitized_extra_sys, _ = sanitize_outbound_openrouter_text((system_prompt or "").strip())

    turn_ctx = _resolve_active_turn_context(user_id, chat_id=chat_id)
    caller_family = str(turn_ctx.get("caller_family") or "").strip().lower()
    requested_model = (model or "").strip()
    default_model = str(bundle.get("default_model") or "").strip()
    max_price_limit = max(0.0, float(bundle.get("max_price_per_m") or 0.0))
    daily_limit = max(0.0, float(bundle.get("daily_spend_limit_usd") or 0.0))
    eff_deadline = (
        float(deadline_mono)
        if deadline_mono is not None
        else (time.monotonic() + OPENROUTER_MCP_HARD_DEADLINE_SEC)
    )

    # 0a) Block asynchronous `:batch` queue models in synchronous MCP ($0, <5ms)
    raw_batch_probe = requested_model or (default_model if explicit_user_choice else "")
    if raw_batch_probe.lower().endswith(":batch"):
        sync_id = raw_batch_probe[:-6]
        err_msg = (
            f"Модель `{raw_batch_probe}` относится к асинхронной очереди OpenRouter (`:batch`) "
            f"и не может вызываться синхронно через MCP. Используйте синхронный ID без `:batch`: `{sync_id}`."
        )
        log_row = await add_openrouter_log(
            user_id,
            chat_id=turn_ctx["chat_id"],
            chat_title=turn_ctx["chat_title"],
            project_name=turn_ctx["project_name"],
            requested_model=raw_batch_probe,
            resolved_model="",
            status="blocked",
            finish_reason="guardrail_batch_only",
            prompt_preview=sanitized_prompt,
            error_text=err_msg,
            purpose=purpose,
            items_count=0,
        )
        await notify_openrouter_user(user_id, log_entry=log_row)
        return {
            "ok": False,
            "status": "blocked",
            "model": raw_batch_probe,
            "error": err_msg,
            "log": log_row,
        }

    # 0b) Caller-Aware & Fresh-Only Guardrail when `explicit_user_choice == False`
    if not explicit_user_choice:
        if not requested_model or requested_model.lower() == "openrouter/auto":
            def_cat = await lookup_catalog_model(default_model) if default_model else None
            def_fam = _map_openrouter_vendor_to_family(default_model) if default_model else ""
            def_usable = bool(
                default_model
                and default_model.lower() != "openrouter/auto"
                and not default_model.lower().endswith(":batch")
                and not is_deprecated_openrouter_model(
                    default_model,
                    str((def_cat or {}).get("name") or ""),
                    int((def_cat or {}).get("created") or 0),
                )
                and not bool((def_cat or {}).get("is_deprecated"))
                and not bool((def_cat or {}).get("is_batch_only"))
                and not (caller_family in ("gemini", "gpt", "claude") and def_fam == caller_family)
            )
            if def_usable:
                effective_model = default_model
            else:
                catalog_items = await get_openrouter_models_catalog()
                require_free = (max_price_limit <= 0.0) or default_model.lower().endswith(":free")
                auto_cands = _select_fresh_cross_vendor_candidates(
                    catalog_items,
                    caller_family=caller_family,
                    max_price_limit=max_price_limit,
                    require_free=require_free,
                    limit=3,
                )
                if auto_cands:
                    effective_model = str(auto_cands[0]["id"])
                elif not caller_family and not catalog_items:
                    effective_model = requested_model or default_model or "openrouter/auto"
                else:
                    budget_lbl = "FREE ($0/1M)" if require_free else f"<=${max_price_limit:g}/1M"
                    err_msg = (
                        f"Автовыбор кросс-вендорной модели отклонён: в каталоге OpenRouter не найдено "
                        f"свежих моделей другого семейства в рамках бюджета `{budget_lbl}` "
                        f"(скрытое переключение с бесплатной модели на платную запрещено). "
                        f"Вызовите `openrouter_models` для подбора модели или настройте `max_price_per_m`."
                    )
                    log_row = await add_openrouter_log(
                        user_id,
                        chat_id=turn_ctx["chat_id"],
                        chat_title=turn_ctx["chat_title"],
                        project_name=turn_ctx["project_name"],
                        requested_model=requested_model or default_model or "openrouter/auto",
                        resolved_model="",
                        status="blocked",
                        finish_reason="guardrail_auto_budget",
                        prompt_preview=sanitized_prompt,
                        error_text=err_msg,
                        purpose=purpose,
                        items_count=0,
                    )
                    await notify_openrouter_user(user_id, log_entry=log_row)
                    return {
                        "ok": False,
                        "status": "blocked",
                        "model": requested_model or default_model or "openrouter/auto",
                        "error": err_msg,
                        "log": log_row,
                    }
        else:
            effective_model = requested_model
            req_cat = await lookup_catalog_model(effective_model)
            is_dep = bool(
                (req_cat and req_cat.get("is_deprecated"))
                or is_deprecated_openrouter_model(
                    effective_model,
                    str((req_cat or {}).get("name") or ""),
                    int((req_cat or {}).get("created") or 0),
                )
            )
            vendor_fam = _map_openrouter_vendor_to_family(effective_model)
            is_same_vendor = bool(
                caller_family in ("gemini", "gpt", "claude") and vendor_fam == caller_family
            )
            if is_dep or is_same_vendor:
                catalog_items = await get_openrouter_models_catalog()
                suggestions = _select_fresh_cross_vendor_candidates(
                    catalog_items,
                    caller_family=caller_family,
                    max_price_limit=max_price_limit,
                    require_free=(max_price_limit <= 0.0),
                    limit=3,
                )
                sug_lines = []
                for s in suggestions:
                    if s.get("is_free"):
                        p_str = "FREE ($0/1M)"
                    else:
                        p_str = f"in ${s['prompt_price_per_m']:g}/1M · out ${s['completion_price_per_m']:g}/1M"
                    sug_lines.append(f"- `{s['id']}` ({p_str})")
                sug_block = (
                    "Рекомендуемые свежие кросс-вендорные модели:\n" + "\n".join(sug_lines)
                    if sug_lines
                    else "Вызовите `openrouter_models` для просмотра актуальных кросс-вендорных моделей."
                )
                if is_same_vendor:
                    reason_hdr = (
                        f"Вызов `{effective_model}` отклонён защитой кросс-вендорного консилиума ($0): "
                        f"вызывающий агент уже принадлежит семейству `{caller_family}`, поэтому для независимого "
                        f"второго мнения нужно выбрать модель другого вендора."
                    )
                    fin_reason = "guardrail_same_vendor"
                else:
                    reason_hdr = (
                        f"Вызов `{effective_model}` отклонён защитой актуальности ($0): "
                        f"запрошенная модель относится к устаревшему поколению."
                    )
                    fin_reason = "guardrail_deprecated_model"
                err_msg = (
                    f"{reason_hdr}\n{sug_block}\n"
                    f"Передавайте `explicit_user_choice=true` ТОЛЬКО в том случае, если именно эту модель "
                    f"прямо назвал и попросил сам пользователь в своём сообщении."
                )
                log_row = await add_openrouter_log(
                    user_id,
                    chat_id=turn_ctx["chat_id"],
                    chat_title=turn_ctx["chat_title"],
                    project_name=turn_ctx["project_name"],
                    requested_model=effective_model,
                    resolved_model="",
                    status="blocked",
                    finish_reason=fin_reason,
                    prompt_preview=sanitized_prompt,
                    error_text=err_msg,
                    purpose=purpose,
                    items_count=0,
                )
                await notify_openrouter_user(user_id, log_entry=log_row)
                return {
                    "ok": False,
                    "status": "blocked",
                    "model": effective_model,
                    "suggestions": [str(s["id"]) for s in suggestions],
                    "error": err_msg,
                    "log": log_row,
                }
    else:
        effective_model = requested_model or default_model or "openrouter/auto"

    # 1) Pre-flight Daily Spend Limit check (Contour 2)
    if daily_limit > 0.0 and not skip_daily_check:
        today_spend = await _compute_effective_today_spend_usd(bundle)
        if today_spend >= daily_limit:
            err_msg = (
                f"Вызов заблокирован защитой Antigravity Hub: достигнут суточный лимит расходов "
                f"OpenRouter (${today_spend:.4f} из ${daily_limit:g} за сегодня UTC). "
                f"Увеличьте или отключите суточный потолок в окне OpenRouter."
            )
            log_row = await add_openrouter_log(
                user_id,
                chat_id=turn_ctx["chat_id"],
                chat_title=turn_ctx["chat_title"],
                project_name=turn_ctx["project_name"],
                requested_model=effective_model,
                resolved_model="",
                status="blocked",
                finish_reason="guardrail_daily_limit",
                prompt_preview=sanitized_prompt,
                error_text=err_msg,
                purpose=purpose,
                items_count=0,
            )
            await notify_openrouter_user(user_id, log_entry=log_row)
            return {
                "ok": False,
                "status": "blocked",
                "model": effective_model,
                "error": err_msg,
                "log": log_row,
            }

    # 2) Pre-flight Max Price per 1M Tokens check (Contour 2)
    catalog_info = await lookup_catalog_model(effective_model)
    if max_price_limit > 0.0 and catalog_info is not None:
        in_p = float(catalog_info.get("prompt_price_per_m") or 0.0)
        out_p = float(catalog_info.get("completion_price_per_m") or 0.0)
        if in_p > max_price_limit or out_p > max_price_limit:
            err_msg = (
                f"Модель `{effective_model}` заблокирована ценовым фильтром пользователя: "
                f"вход ${in_p:g}/1M, выход ${out_p:g}/1M при установленном потолке "
                f"${max_price_limit:g}/1M. Вызовите `openrouter_models(max_price_per_m={max_price_limit:g})` "
                f"для выбора более доступной модели."
            )
            log_row = await add_openrouter_log(
                user_id,
                chat_id=turn_ctx["chat_id"],
                chat_title=turn_ctx["chat_title"],
                project_name=turn_ctx["project_name"],
                requested_model=effective_model,
                resolved_model=effective_model,
                status="blocked",
                finish_reason="guardrail_max_price",
                prompt_preview=sanitized_prompt,
                error_text=err_msg,
                purpose=purpose,
                items_count=0,
            )
            await notify_openrouter_user(user_id, log_entry=log_row)
            return {
                "ok": False,
                "status": "blocked",
                "model": effective_model,
                "error": err_msg,
                "log": log_row,
            }

    # 3) Server-side mandatory `custom_rules` injection into `role: "system"`
    raw_user_rules = normalize_legacy_openrouter_rules(str(bundle.get("custom_rules") or "").strip())
    user_rules, _ = sanitize_outbound_openrouter_text(raw_user_rules)
    sys_parts: list[str] = []
    if user_rules:
        sys_parts.append(f"[Персональные правила пользователя OpenRouter]:\n{user_rules}")
    if sanitized_extra_sys:
        sys_parts.append(sanitized_extra_sys)
    combined_sanitized_sys = "\n\n".join(sys_parts)

    messages: list[dict[str, str]] = []
    if combined_sanitized_sys:
        messages.append({"role": "system", "content": combined_sanitized_sys})
    messages.append({"role": "user", "content": sanitized_prompt})

    req_body: dict[str, Any] = {
        "model": effective_model,
        "messages": messages,
        "usage": {"include": True},
    }
    if max_price_limit > 0.0:
        req_body["provider"] = {
            "max_price": {
                "prompt": max_price_limit,
                "completion": max_price_limit,
            }
        }
    norm_temp: Optional[float] = None
    if temperature is not None:
        try:
            norm_temp = max(0.0, min(2.0, float(temperature)))
            req_body["temperature"] = norm_temp
        except (TypeError, ValueError):
            norm_temp = None
    norm_max_tokens: Optional[int] = None
    if max_tokens is not None:
        try:
            mt = int(max_tokens)
            if mt > 0:
                norm_max_tokens = max(16, min(65_536, mt))
                req_body["max_tokens"] = norm_max_tokens
        except (TypeError, ValueError):
            norm_max_tokens = None
    clean_effort = (reasoning_effort or "").strip().lower()
    if clean_effort in ("low", "medium", "high"):
        req_body["reasoning"] = {"effort": clean_effort}
    else:
        clean_effort = ""

    # 4) In-memory LRU/TTL retry cache (`_RECENT_OR_RESPONSES`, 600s, max 64)
    cache_key = _build_openrouter_response_cache_key(
        user_id=user_id,
        chosen_model=effective_model,
        sanitized_prompt=sanitized_prompt,
        sanitized_sys=combined_sanitized_sys,
        reasoning_effort=clean_effort,
        max_tokens=norm_max_tokens,
        temperature=norm_temp,
    )
    cached_hit = _get_cached_openrouter_response(cache_key)
    if cached_hit is not None:
        return cached_hit

    t0 = time.perf_counter()
    rem_budget = eff_deadline - time.monotonic()
    if rem_budget <= 0.5:
        err_msg = (
            f"Превышен бюджет времени MCP-шлюза OpenRouter ({int(OPENROUTER_MCP_HARD_DEADLINE_SEC)} с) "
            f"для модели `{effective_model}`. Повторите вызов со сниженным `reasoning_effort=\"medium\"` "
            f"или уменьшите `max_tokens`."
        )
        log_row = await add_openrouter_log(
            user_id,
            chat_id=turn_ctx["chat_id"],
            chat_title=turn_ctx["chat_title"],
            project_name=turn_ctx["project_name"],
            requested_model=effective_model,
            latency_ms=0,
            status="error",
            finish_reason="deadline_timeout",
            prompt_preview=sanitized_prompt,
            error_text=err_msg,
            purpose=purpose,
            items_count=0,
        )
        await notify_openrouter_user(user_id, log_entry=log_row)
        return {
            "ok": False,
            "status": "timeout",
            "model": effective_model,
            "latency_ms": 0,
            "error": err_msg,
            "log": log_row,
        }

    try:
        proxy_url = await proxy_router.ensure_proxy_for_context("global")
        status_code, resp_data = await asyncio.wait_for(
            asyncio.to_thread(
                _openrouter_http_sync,
                "POST",
                "/chat/completions",
                api_key=api_key,
                payload=req_body,
                timeout=min(155.0, max(3.0, rem_budget)),
                deadline_mono=eff_deadline,
                proxy_url=proxy_url,
            ),
            timeout=max(1.0, rem_budget),
        )
    except proxy_router.ProxyRouteError as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        err_msg = str(exc)
        log_row = await add_openrouter_log(
            user_id,
            chat_id=turn_ctx["chat_id"],
            chat_title=turn_ctx["chat_title"],
            project_name=turn_ctx["project_name"],
            requested_model=effective_model,
            latency_ms=latency_ms,
            status="error",
            finish_reason="proxy_route_error",
            prompt_preview=sanitized_prompt,
            error_text=err_msg,
            purpose=purpose,
            items_count=0,
        )
        await notify_openrouter_user(user_id, log_entry=log_row)
        return {
            "ok": False,
            "status": "error",
            "model": effective_model,
            "latency_ms": latency_ms,
            "error": err_msg,
            "log": log_row,
        }
    except (TimeoutError, asyncio.TimeoutError):
        latency_ms = int((time.perf_counter() - t0) * 1000)
        err_msg = (
            f"Превышен лимит ожидания ответа OpenRouter ({int(OPENROUTER_MCP_HARD_DEADLINE_SEC)} с) "
            f"для модели `{effective_model}`. Рекомендуется снизить `reasoning_effort` до `\"medium\"` "
            f"или уменьшить `max_tokens`."
        )
        log_row = await add_openrouter_log(
            user_id,
            chat_id=turn_ctx["chat_id"],
            chat_title=turn_ctx["chat_title"],
            project_name=turn_ctx["project_name"],
            requested_model=effective_model,
            latency_ms=latency_ms,
            status="error",
            finish_reason="deadline_timeout",
            prompt_preview=sanitized_prompt,
            error_text=err_msg,
            purpose=purpose,
            items_count=0,
        )
        await notify_openrouter_user(user_id, log_entry=log_row)
        return {
            "ok": False,
            "status": "timeout",
            "model": effective_model,
            "latency_ms": latency_ms,
            "error": err_msg,
            "log": log_row,
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        err_msg = f"Сетевая ошибка при обращении к OpenRouter: {exc}"
        log_row = await add_openrouter_log(
            user_id,
            chat_id=turn_ctx["chat_id"],
            chat_title=turn_ctx["chat_title"],
            project_name=turn_ctx["project_name"],
            requested_model=effective_model,
            latency_ms=latency_ms,
            status="error",
            finish_reason="network_error",
            prompt_preview=sanitized_prompt,
            error_text=err_msg,
            purpose=purpose,
            items_count=0,
        )
        await notify_openrouter_user(user_id, log_entry=log_row)
        return {
            "ok": False,
            "status": "error",
            "model": effective_model,
            "latency_ms": latency_ms,
            "error": err_msg,
            "log": log_row,
        }

    latency_ms = int((time.perf_counter() - t0) * 1000)

    if status_code >= 400 or (isinstance(resp_data, dict) and resp_data.get("error") and not resp_data.get("choices")):
        err_msg = _extract_openrouter_error_message(status_code, resp_data)
        log_row = await add_openrouter_log(
            user_id,
            chat_id=turn_ctx["chat_id"],
            chat_title=turn_ctx["chat_title"],
            project_name=turn_ctx["project_name"],
            requested_model=effective_model,
            resolved_model=str(resp_data.get("model") or ""),
            provider_name=str(resp_data.get("provider") or ""),
            generation_id=str(resp_data.get("id") or ""),
            latency_ms=latency_ms,
            status="error",
            finish_reason=f"http_{status_code}",
            prompt_preview=sanitized_prompt,
            error_text=err_msg,
            purpose=purpose,
            items_count=0,
        )
        await notify_openrouter_user(user_id, log_entry=log_row)
        if status_code in (402, 403):
            schedule_openrouter_live_refresh(user_id, force=True, max_age_sec=30.0)
        return {
            "ok": False,
            "status": "error",
            "model": effective_model,
            "latency_ms": latency_ms,
            "error": err_msg,
            "log": log_row,
        }

    choices = resp_data.get("choices") if isinstance(resp_data.get("choices"), list) else []
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    choice_msg = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
    if isinstance(first_choice.get("error"), dict):
        err_msg = _extract_openrouter_error_message(502, {"error": first_choice["error"]})
        log_row = await add_openrouter_log(
            user_id,
            chat_id=turn_ctx["chat_id"],
            chat_title=turn_ctx["chat_title"],
            project_name=turn_ctx["project_name"],
            requested_model=effective_model,
            resolved_model=str(resp_data.get("model") or ""),
            provider_name=str(resp_data.get("provider") or ""),
            generation_id=str(resp_data.get("id") or ""),
            latency_ms=latency_ms,
            status="error",
            finish_reason="upstream_error",
            prompt_preview=sanitized_prompt,
            error_text=err_msg,
            purpose=purpose,
            items_count=0,
        )
        await notify_openrouter_user(user_id, log_entry=log_row)
        return {
            "ok": False,
            "status": "error",
            "model": effective_model,
            "latency_ms": latency_ms,
            "error": err_msg,
            "log": log_row,
        }

    answer_text = _extract_assistant_content(choice_msg)
    finish_reason = str(
        first_choice.get("native_finish_reason")
        or first_choice.get("finish_reason")
        or "stop"
    )
    resolved_model = str(resp_data.get("model") or effective_model)
    provider_name = str(resp_data.get("provider") or "")
    generation_id = str(resp_data.get("id") or "")

    usage = resp_data.get("usage") if isinstance(resp_data.get("usage"), dict) else {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    p_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    c_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    cached_tokens = int(p_details.get("cached_tokens") or usage.get("cached_tokens") or 0)
    reasoning_tokens = int(c_details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0)

    raw_cost = usage.get("cost")
    if raw_cost is None:
        raw_cost = usage.get("total_cost")
    if raw_cost is not None:
        try:
            cost_usd = max(0.0, float(raw_cost))
        except (TypeError, ValueError):
            cost_usd = 0.0
    else:
        resolved_cat = await lookup_catalog_model(resolved_model) or catalog_info
        if resolved_cat:
            in_rate = float(resolved_cat.get("prompt_price_per_m") or 0.0) / 1_000_000.0
            out_rate = float(resolved_cat.get("completion_price_per_m") or 0.0) / 1_000_000.0
            cost_usd = max(0.0, prompt_tokens * in_rate + completion_tokens * out_rate)
        else:
            cost_usd = 0.0

    log_row = await add_openrouter_log(
        user_id,
        chat_id=turn_ctx["chat_id"],
        chat_title=turn_ctx["chat_title"],
        project_name=turn_ctx["project_name"],
        requested_model=effective_model,
        resolved_model=resolved_model,
        provider_name=provider_name,
        generation_id=generation_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        status="ok",
        finish_reason=finish_reason,
        prompt_preview=sanitized_prompt,
        response_preview=answer_text,
        purpose=purpose,
        items_count=1,
    )
    if cost_usd > 0.0:
        await deduct_user_openrouter_cached_spend(user_id, cost_usd)
    await notify_openrouter_user(user_id, log_entry=log_row)
    if cost_usd > 0.0:
        schedule_openrouter_live_refresh(user_id, force=False, max_age_sec=45.0)

    res_payload = {
        "ok": True,
        "status": "ok",
        "requested_model": effective_model,
        "resolved_model": resolved_model,
        "provider_name": provider_name,
        "generation_id": generation_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "finish_reason": finish_reason,
        "content": answer_text,
        "log": log_row,
    }
    _put_cached_openrouter_response(cache_key, res_payload)
    return res_payload


async def execute_openrouter_multi_chat(
    bundle: dict[str, Any],
    *,
    prompt: str,
    models: list[str],
    system_prompt: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    explicit_user_choice: bool = False,
    chat_id: str = "",
    deadline_mono: Optional[float] = None,
) -> dict[str, Any]:
    """Parallel `asyncio.gather` consultation across 2..4 OpenRouter models."""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        return {"ok": False, "error": "Параметр `prompt` не может быть пустым."}

    sanitized_prompt, _ = sanitize_outbound_openrouter_text(clean_prompt)

    clean_models: list[str] = []
    seen: set[str] = set()
    for m in models or []:
        s = str(m or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            clean_models.append(s)

    if len(clean_models) < 2 or len(clean_models) > 4:
        return {
            "ok": False,
            "error": "Для `openrouter_multi_chat` укажите от 2 до 4 различных ID моделей в массиве `models`.",
        }

    eff_deadline = (
        float(deadline_mono)
        if deadline_mono is not None
        else (time.monotonic() + OPENROUTER_MCP_HARD_DEADLINE_SEC)
    )

    daily_limit = max(0.0, float(bundle.get("daily_spend_limit_usd") or 0.0))
    if daily_limit > 0.0:
        today_spend = await _compute_effective_today_spend_usd(bundle)
        if today_spend >= daily_limit:
            user_id = str(bundle.get("user_id") or "")
            turn_ctx = _resolve_active_turn_context(user_id, chat_id=chat_id)
            err_msg = (
                f"Консилиум заблокирован защитой Antigravity Hub: достигнут суточный лимит расходов "
                f"OpenRouter (${today_spend:.4f} из ${daily_limit:g} за сегодня UTC)."
            )
            log_row = await add_openrouter_log(
                user_id,
                chat_id=turn_ctx["chat_id"],
                chat_title=turn_ctx["chat_title"],
                project_name=turn_ctx["project_name"],
                requested_model=", ".join(clean_models),
                status="blocked",
                finish_reason="guardrail_daily_limit",
                prompt_preview=sanitized_prompt,
                error_text=err_msg,
                purpose="mcp_council",
                items_count=0,
            )
            await notify_openrouter_user(user_id, log_entry=log_row)
            return {"ok": False, "status": "blocked", "error": err_msg}

    results = await asyncio.gather(
        *(
            execute_openrouter_chat(
                bundle,
                prompt=clean_prompt,
                model=m,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                explicit_user_choice=explicit_user_choice,
                chat_id=chat_id,
                deadline_mono=eff_deadline,
                skip_daily_check=True,
                purpose="mcp_council",
            )
            for m in clean_models
        )
    )

    ok_any = any(bool(r.get("ok")) for r in results)
    return {
        "ok": ok_any,
        "models": clean_models,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Hub Micro-Generations: Hybrid Smart Outline Titles via OpenRouter (v101)
# ---------------------------------------------------------------------------

OUTLINE_TITLE_BATCH_SIZE = 25

SMART_TITLE_MODEL_PRESETS: list[dict[str, Any]] = [
    {
        "id": "openai/gpt-4.1-mini",
        "label": "GPT-4.1 Mini · Рекомендуемая ($0.40/$1.60, 0 мыслей)",
        "short_label": "GPT-4.1 Mini · реком. ($0.40)",
        "prompt_price_per_m": 0.40,
        "completion_price_per_m": 1.60,
        "recommended": True,
    },
    {
        "id": "qwen/qwen3-235b-a22b-2507",
        "label": "Qwen3 235B · Мощная и дешёвая ($0.14/$0.80)",
        "short_label": "Qwen3 235B ($0.14)",
        "prompt_price_per_m": 0.14,
        "completion_price_per_m": 0.80,
        "recommended": False,
    },
    {
        "id": "google/gemini-2.5-flash-lite",
        "label": "Gemini 2.5 Flash Lite · Ультра-эконом ($0.10/$0.40)",
        "short_label": "Gemini 2.5 Flash Lite ($0.10)",
        "prompt_price_per_m": 0.10,
        "completion_price_per_m": 0.40,
        "recommended": False,
    },
    {
        "id": "google/gemini-3.1-flash-lite-preview",
        "label": "Gemini 3.1 Flash Lite ($0.25/$1.50)",
        "short_label": "Gemini 3.1 Flash Lite ($0.25)",
        "prompt_price_per_m": 0.25,
        "completion_price_per_m": 1.50,
        "recommended": False,
    },
]

_KNOWN_UTILITY_MODEL_SPECS: dict[str, dict[str, Any]] = {
    "openai/gpt-4.1-mini": {
        "prompt_price_per_m": 0.40,
        "completion_price_per_m": 1.60,
        "supports_reasoning": False,
    },
    "qwen/qwen3-235b-a22b-2507": {
        "prompt_price_per_m": 0.14,
        "completion_price_per_m": 0.80,
        "supports_reasoning": False,
    },
    "google/gemini-2.5-flash-lite": {
        "prompt_price_per_m": 0.10,
        "completion_price_per_m": 0.40,
        "supports_reasoning": True,
    },
    "google/gemini-3.1-flash-lite-preview": {
        "prompt_price_per_m": 0.25,
        "completion_price_per_m": 1.50,
        "supports_reasoning": True,
    },
}

_SINGLE_OUTLINE_TITLE_SYS_PROMPT = (
    "Ты генератор компактных заголовков для оглавления технического чата Antigravity Hub.\n"
    "Правила:\n"
    "1. Пиши на языке сообщения пользователя (по умолчанию русский).\n"
    "2. Длина строго 35–82 символа (максимум 95), ровно одна строка без переносов, "
    "без кавычек вокруг всей строки, без Markdown (**, #, `) и без вводных клише "
    "(«Пользователь спрашивает…», «Вопрос про…», «Просьба…»).\n"
    "3. Отражай И предмет/контекст проблемы, И цель/вопрос, склеивая разорванные голосовой диктовкой фразы.\n"
    "Пример:\n"
    "Вход: Не работает режим терминал, на клиентском ПК. Через докер, можешь проверить в чём дело? И предложить решение проблемы\n"
    "Выход: ❓ Не работает терминал у клиента через Docker — как решить проблему?\n"
    "4. Эмодзи в самом начале ставь ТОЛЬКО по делу: `❓ ` если в сообщении есть вопрос или разбор причины, "
    "`🐛 ` если это отчёт об ошибке/крэше без вопроса, иначе без эмодзи.\n"
    "Верни ТОЛЬКО готовый заголовок."
)

_BATCH_OUTLINE_TITLES_SYS_PROMPT = (
    "Ты пакетный генератор компактных заголовков для оглавления технического чата Antigravity Hub.\n"
    "На входе JSON-массив элементов `{\"id\": <int>, \"text\": \"<string>\"}`.\n"
    "Для каждого элемента сформулируй заголовок:\n"
    "- На языке сообщения (по умолчанию русский), длина 35–82 символа (максимум 95), одна строка.\n"
    "- Без обёрточных кавычек, без Markdown (**, #, `) и без слов «Пользователь просит / Вопрос о».\n"
    "- Отражай И суть/контекст проблемы, И цель/вопрос, склеивая рваные фразы диктовки "
    "(пример: «Не работает режим терминал, на клиентском ПК. Через докер, можешь проверить в чём дело? "
    "И предложить решение проблемы» -> «❓ Не работает терминал у клиента через Docker — как решить проблему?»).\n"
    "- Эмодзи в начале ТОЛЬКО по делу: `❓ ` для вопросов/разбора причины, `🐛 ` для багов/крэшей без вопроса, иначе без эмодзи.\n"
    "Верни СТРОГО валидный JSON-объект вида: {\"titles\": [{\"id\": <int>, \"title\": \"<string>\"}]} "
    "без пояснений и без блока markdown."
)


async def _lookup_utility_model_spec(
    model_id: str,
    *,
    allow_network: bool = False,
) -> Optional[dict[str, Any]]:
    clean = (model_id or "").strip().lower()
    if not clean:
        return None
    by_id: dict[str, dict[str, Any]] = dict(_MODELS_CACHE.get("by_id") or {})
    if clean in by_id:
        return by_id[clean]
    base = clean.split(":", 1)[0]
    if clean.endswith(":free"):
        return {
            "id": model_id.strip(),
            "prompt_price_per_m": 0.0,
            "completion_price_per_m": 0.0,
            "supports_reasoning": bool(
                (by_id.get(base) or _KNOWN_UTILITY_MODEL_SPECS.get(base) or {}).get("supports_reasoning")
            ),
        }
    if base in by_id:
        return by_id[base]
    if base in _KNOWN_UTILITY_MODEL_SPECS:
        return dict(_KNOWN_UTILITY_MODEL_SPECS[base])
    if allow_network:
        return await lookup_catalog_model(model_id)
    return None


def _should_suppress_utility_reasoning(
    model_id: str,
    spec: Optional[dict[str, Any]] = None,
) -> bool:
    base = (model_id or "").strip().lower().split(":", 1)[0]
    if not base:
        return False
    if base in ("openai/gpt-4.1-mini", "qwen/qwen3-235b-a22b-2507"):
        return False
    if base.startswith(("openai/gpt-4.1", "openai/gpt-4o")):
        return False
    if base.startswith(
        (
            "google/gemini-2.5",
            "google/gemini-3",
            "openai/gpt-5",
            "qwen/qwen3.5",
            "openai/o1",
            "openai/o3",
            "openai/o4",
            "deepseek/deepseek-r1",
        )
    ):
        return True
    if spec and bool(spec.get("supports_reasoning")):
        return True
    return False


async def _build_utility_fallback_chain(
    chosen_model: str,
    max_price_limit: float,
) -> tuple[list[str], Optional[tuple[float, float]]]:
    primary = (chosen_model or DEFAULT_OPENROUTER_SMART_TITLES_MODEL).strip() or DEFAULT_OPENROUTER_SMART_TITLES_MODEL
    raw_candidates = [primary, "openai/gpt-4.1-mini", "google/gemini-2.5-flash-lite"]
    deduped: list[str] = []
    seen: set[str] = set()
    for cand in raw_candidates:
        c = cand.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            deduped.append(c)

    if max_price_limit <= 0.0:
        return deduped, None

    allowed: list[str] = []
    primary_blocked_prices: Optional[tuple[float, float]] = None
    for idx, cand in enumerate(deduped):
        spec = await _lookup_utility_model_spec(cand, allow_network=(idx == 0))
        if spec is not None:
            in_p = float(spec.get("prompt_price_per_m") or 0.0)
            out_p = float(spec.get("completion_price_per_m") or 0.0)
            if in_p > max_price_limit or out_p > max_price_limit:
                if idx == 0:
                    primary_blocked_prices = (in_p, out_p)
                continue
        allowed.append(cand)
    return allowed, primary_blocked_prices


async def execute_openrouter_utility_call(
    user_id: str,
    *,
    bundle: Optional[dict[str, Any]] = None,
    system_prompt: str,
    user_prompt: str,
    chosen_model: Optional[str] = None,
    max_tokens: int = 90,
    temperature: float = 0.2,
    purpose: str = "outline_title",
    items_count: int = 1,
    chat_id: str = "",
    chat_title: str = "",
    project_name: str = "",
    timeout_sec: float = 35.0,
    parse_items_fn: Optional[Callable[[str], tuple[Any, int]]] = None,
) -> dict[str, Any]:
    """Lightweight internal OpenRouter utility call (no custom_rules, no cross-vendor guardrail)."""
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return {"ok": False, "status": "error", "error": "Не указан пользователь OpenRouter."}

    secret_bundle = bundle if bundle is not None else await get_user_openrouter_secret_bundle(clean_uid)
    if (
        not secret_bundle
        or not bool(secret_bundle.get("has_key"))
        or not bool(secret_bundle.get("is_enabled"))
    ):
        return {
            "ok": False,
            "status": "disabled",
            "error": "Персональный шлюз OpenRouter не подключён или поставлен на паузу.",
        }

    api_key = str(secret_bundle.get("api_key") or "").strip()
    if not api_key:
        return {
            "ok": False,
            "status": "disabled",
            "error": "API-ключ OpenRouter недоступен.",
        }

    sanitized_sys, _ = sanitize_outbound_openrouter_text((system_prompt or "").strip())
    sanitized_user, _ = sanitize_outbound_openrouter_text((user_prompt or "").strip())
    if not sanitized_user:
        return {"ok": False, "status": "error", "error": "Пустой входной текст для генерации."}

    req_model = (
        str(chosen_model or secret_bundle.get("smart_titles_model") or DEFAULT_OPENROUTER_SMART_TITLES_MODEL).strip()
        or DEFAULT_OPENROUTER_SMART_TITLES_MODEL
    )
    max_price_limit = max(0.0, float(secret_bundle.get("max_price_per_m") or 0.0))
    daily_limit = max(0.0, float(secret_bundle.get("daily_spend_limit_usd") or 0.0))

    if daily_limit > 0.0:
        today_spend = await _compute_effective_today_spend_usd(secret_bundle)
        if today_spend >= daily_limit:
            err_msg = (
                f"Микро-генерация остановлена: достигнут суточный лимит расходов OpenRouter "
                f"(${today_spend:.4f} из ${daily_limit:g} за сегодня UTC)."
            )
            log_row = await add_openrouter_log(
                clean_uid,
                chat_id=chat_id,
                chat_title=chat_title,
                project_name=project_name,
                requested_model=req_model,
                resolved_model="",
                status="blocked",
                finish_reason="guardrail_daily_limit",
                prompt_preview=sanitized_user,
                error_text=err_msg,
                purpose=purpose,
                items_count=0,
            )
            await notify_openrouter_user(clean_uid, log_entry=log_row)
            return {
                "ok": False,
                "status": "blocked",
                "finish_reason": "guardrail_daily_limit",
                "stop_reason": "daily_spend_limit",
                "error": err_msg,
                "log": log_row,
            }

    candidate_chain, blocked_prices = await _build_utility_fallback_chain(req_model, max_price_limit)
    if not candidate_chain:
        in_p, out_p = blocked_prices or (0.0, 0.0)
        err_msg = (
            f"Модель `{req_model}` (${in_p:g}/${out_p:g} за 1M) и резервные модели превышают "
            f"установленный потолок цены ${max_price_limit:g}/1M."
        )
        log_row = await add_openrouter_log(
            clean_uid,
            chat_id=chat_id,
            chat_title=chat_title,
            project_name=project_name,
            requested_model=req_model,
            resolved_model=req_model,
            status="blocked",
            finish_reason="guardrail_max_price",
            prompt_preview=sanitized_user,
            error_text=err_msg,
            purpose=purpose,
            items_count=0,
        )
        await notify_openrouter_user(clean_uid, log_entry=log_row)
        return {
            "ok": False,
            "status": "blocked",
            "finish_reason": "guardrail_max_price",
            "stop_reason": "max_price_limit",
            "error": err_msg,
            "log": log_row,
        }

    messages: list[dict[str, str]] = []
    if sanitized_sys:
        messages.append({"role": "system", "content": sanitized_sys})
    messages.append({"role": "user", "content": sanitized_user})

    norm_max_tokens = max(16, min(8192, int(max_tokens or 90)))
    norm_temp = max(0.0, min(1.5, float(temperature if temperature is not None else 0.2)))

    last_err_msg = "Не удалось выполнить микро-генерацию OpenRouter."
    last_status_code = 0
    last_finish_reason = "error"
    last_resp_data: dict[str, Any] = {}
    last_model = candidate_chain[0]
    total_t0 = time.perf_counter()

    try:
        proxy_url = await proxy_router.ensure_proxy_for_context("global")
    except proxy_router.ProxyRouteError as exc:
        err_msg = str(exc)
        log_row = await add_openrouter_log(
            clean_uid,
            chat_id=chat_id,
            chat_title=chat_title,
            project_name=project_name,
            requested_model=req_model,
            resolved_model=last_model,
            latency_ms=int((time.perf_counter() - total_t0) * 1000),
            status="error",
            finish_reason="proxy_route_error",
            prompt_preview=sanitized_user,
            error_text=err_msg,
            purpose=purpose,
            items_count=0,
        )
        await notify_openrouter_user(clean_uid, log_entry=log_row)
        return {
            "ok": False,
            "status": "error",
            "http_status": 0,
            "stop_reason": "proxy_route_error",
            "error": err_msg,
            "log": log_row,
        }

    for candidate_model in candidate_chain:
        last_model = candidate_model
        cand_spec = await _lookup_utility_model_spec(candidate_model, allow_network=False)
        req_body: dict[str, Any] = {
            "model": candidate_model,
            "messages": messages,
            "max_tokens": norm_max_tokens,
            "temperature": norm_temp,
            "usage": {"include": True},
        }
        if max_price_limit > 0.0:
            req_body["provider"] = {
                "max_price": {
                    "prompt": max_price_limit,
                    "completion": max_price_limit,
                }
            }
        added_reasoning = _should_suppress_utility_reasoning(candidate_model, cand_spec)
        if added_reasoning:
            req_body["reasoning"] = {"effort": "low", "exclude": True}

        t_call = time.perf_counter()
        try:
            status_code, resp_data = await asyncio.to_thread(
                _openrouter_http_sync,
                "POST",
                "/chat/completions",
                api_key=api_key,
                payload=req_body,
                timeout=float(timeout_sec),
                proxy_url=proxy_url,
            )
            if status_code == 400 and added_reasoning:
                retry_body = {k: v for k, v in req_body.items() if k != "reasoning"}
                status_code, resp_data = await asyncio.to_thread(
                    _openrouter_http_sync,
                    "POST",
                    "/chat/completions",
                    api_key=api_key,
                    payload=retry_body,
                    timeout=float(timeout_sec),
                    proxy_url=proxy_url,
                )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            last_status_code = 408
            last_finish_reason = "timeout"
            last_err_msg = f"Таймаут OpenRouter для `{candidate_model}`: {exc}"
            last_resp_data = {}
            continue
        except Exception as exc:
            last_status_code = 0
            last_finish_reason = "network_error"
            last_err_msg = f"Сетевая ошибка OpenRouter для `{candidate_model}`: {exc}"
            last_resp_data = {}
            continue

        latency_ms = int((time.perf_counter() - t_call) * 1000)
        if not isinstance(resp_data, dict):
            resp_data = {}

        if status_code >= 400 or (resp_data.get("error") and not resp_data.get("choices")):
            last_status_code = status_code
            last_finish_reason = f"http_{status_code}"
            last_err_msg = _extract_openrouter_error_message(status_code, resp_data)
            last_resp_data = resp_data
            if status_code in (401, 402, 403):
                break
            if status_code in (404, 408, 429) or status_code >= 500:
                continue
            break

        choices = resp_data.get("choices") if isinstance(resp_data.get("choices"), list) else []
        first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        if isinstance(first_choice.get("error"), dict):
            last_status_code = 502
            last_finish_reason = "upstream_error"
            last_err_msg = _extract_openrouter_error_message(502, {"error": first_choice["error"]})
            last_resp_data = resp_data
            continue

        choice_msg = first_choice.get("message") if isinstance(first_choice.get("message"), dict) else {}
        answer_text = _extract_assistant_content(choice_msg)
        finish_reason = str(
            first_choice.get("native_finish_reason")
            or first_choice.get("finish_reason")
            or "stop"
        )
        resolved_model = str(resp_data.get("model") or candidate_model)
        provider_name = str(resp_data.get("provider") or "")
        generation_id = str(resp_data.get("id") or "")

        usage = resp_data.get("usage") if isinstance(resp_data.get("usage"), dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        p_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        c_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        cached_tokens = int(p_details.get("cached_tokens") or usage.get("cached_tokens") or 0)
        reasoning_tokens = int(c_details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0)

        raw_cost = usage.get("cost")
        if raw_cost is None:
            raw_cost = usage.get("total_cost")
        if raw_cost is not None:
            try:
                cost_usd = max(0.0, float(raw_cost))
            except (TypeError, ValueError):
                cost_usd = 0.0
        else:
            resolved_spec = (
                await _lookup_utility_model_spec(resolved_model, allow_network=False)
                or cand_spec
            )
            if resolved_spec:
                in_rate = float(resolved_spec.get("prompt_price_per_m") or 0.0) / 1_000_000.0
                out_rate = float(resolved_spec.get("completion_price_per_m") or 0.0) / 1_000_000.0
                cost_usd = max(0.0, prompt_tokens * in_rate + completion_tokens * out_rate)
            else:
                cost_usd = 0.0

        parsed_obj: Any = None
        effective_items_count = max(0, int(items_count or 0))
        if parse_items_fn is not None:
            try:
                parsed_obj, effective_items_count = parse_items_fn(answer_text)
            except Exception:
                parsed_obj = None
                effective_items_count = 0

        log_row = await add_openrouter_log(
            clean_uid,
            chat_id=chat_id,
            chat_title=chat_title,
            project_name=project_name,
            requested_model=req_model,
            resolved_model=resolved_model,
            provider_name=provider_name,
            generation_id=generation_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            cached_tokens=cached_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status="ok",
            finish_reason=finish_reason,
            prompt_preview=sanitized_user,
            response_preview=answer_text,
            purpose=purpose,
            items_count=effective_items_count,
        )
        if cost_usd > 0.0:
            await deduct_user_openrouter_cached_spend(clean_uid, cost_usd)
        await notify_openrouter_user(clean_uid, log_entry=log_row)

        return {
            "ok": True,
            "status": "ok",
            "requested_model": req_model,
            "resolved_model": resolved_model,
            "provider_name": provider_name,
            "generation_id": generation_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cached_tokens": cached_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "finish_reason": finish_reason,
            "content": answer_text,
            "parsed": parsed_obj,
            "items_count": effective_items_count,
            "log": log_row,
        }

    total_latency_ms = int((time.perf_counter() - total_t0) * 1000)
    stop_reason = ""
    if last_status_code in (401, 402):
        stop_reason = f"http_{last_status_code}"
    log_row = await add_openrouter_log(
        clean_uid,
        chat_id=chat_id,
        chat_title=chat_title,
        project_name=project_name,
        requested_model=req_model,
        resolved_model=str(last_resp_data.get("model") or last_model),
        provider_name=str(last_resp_data.get("provider") or ""),
        generation_id=str(last_resp_data.get("id") or ""),
        latency_ms=total_latency_ms,
        status="error",
        finish_reason=last_finish_reason,
        prompt_preview=sanitized_user,
        error_text=last_err_msg,
        purpose=purpose,
        items_count=0,
    )
    await notify_openrouter_user(clean_uid, log_entry=log_row)
    if last_status_code in (402, 403):
        schedule_openrouter_live_refresh(clean_uid, force=True, max_age_sec=30.0)
    return {
        "ok": False,
        "status": "error",
        "http_status": last_status_code,
        "stop_reason": stop_reason,
        "error": last_err_msg,
        "log": log_row,
    }


_WRAPPING_QUOTES_PAIRS = (
    ("«", "»"),
    ('"', '"'),
    ("'", "'"),
    ("„", "“"),
    ("“", "”"),
    ("‘", "’"),
)


def clean_generated_outline_title(raw_reply: str) -> str:
    """Clean and bound a model-generated outline title (single line, no Markdown/quotes, <= 110 chars)."""
    if not raw_reply:
        return ""
    lines = [ln.strip() for ln in str(raw_reply).replace("\r", "\n").split("\n") if ln.strip()]
    if not lines:
        return ""
    s = lines[0]
    s = re.sub(r"^(?:заголовок|выход|ответ|title|output)\s*:\s*", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^#{1,6}\s+", "", s)
    s = re.sub(r"[*`#~]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()

    for _ in range(3):
        changed = False
        for open_q, close_q in _WRAPPING_QUOTES_PAIRS:
            if len(s) >= 2 and s.startswith(open_q) and s.endswith(close_q):
                s = s[len(open_q) : len(s) - len(close_q)].strip()
                changed = True
        if not changed:
            break

    s = re.sub(r"\s+", " ", s).strip()
    if s.endswith(".") and not s.endswith("..."):
        s = s[:-1].rstrip()

    if len(s) > 110:
        cut = s[:109]
        last_sp = cut.rfind(" ")
        base = cut[:last_sp] if last_sp >= 72 else cut
        s = base.rstrip(" ,;:-–—.") + "…"
    return s


def _parse_batch_outline_titles_reply(
    raw_reply: str,
    valid_ids: set[int],
) -> dict[int, str]:
    if not raw_reply or not valid_ids:
        return {}
    text = str(raw_reply).strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()

    parsed_json: Any = None
    for candidate in (text,):
        try:
            parsed_json = json.loads(candidate)
        except Exception:
            parsed_json = None

    if parsed_json is None:
        obj_start = text.find("{")
        obj_end = text.rfind("}")
        arr_start = text.find("[")
        arr_end = text.rfind("]")
        slices: list[str] = []
        if obj_start != -1 and obj_end > obj_start:
            slices.append(text[obj_start : obj_end + 1])
        if arr_start != -1 and arr_end > arr_start:
            slices.append(text[arr_start : arr_end + 1])
        for sl in slices:
            try:
                parsed_json = json.loads(sl)
                break
            except Exception:
                continue

    raw_list: list[Any] = []
    if isinstance(parsed_json, dict):
        for key in ("titles", "items", "results", "data"):
            val = parsed_json.get(key)
            if isinstance(val, list):
                raw_list = val
                break
        if not raw_list:
            out_map: dict[int, str] = {}
            for k, v in parsed_json.items():
                if isinstance(v, str):
                    try:
                        mid = int(str(k).strip())
                    except (TypeError, ValueError):
                        continue
                    if mid in valid_ids:
                        cleaned = clean_generated_outline_title(v)
                        if len(cleaned) >= 6:
                            out_map[mid] = cleaned
            return out_map
    elif isinstance(parsed_json, list):
        raw_list = parsed_json

    out: dict[int, str] = {}
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        if raw_id is None:
            raw_id = entry.get("message_id")
        try:
            mid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if mid not in valid_ids:
            continue
        raw_title = entry.get("title")
        if not isinstance(raw_title, str):
            continue
        cleaned = clean_generated_outline_title(raw_title)
        if len(cleaned) >= 6:
            out[mid] = cleaned
    return out


async def generate_single_outline_title(
    user_id: str,
    *,
    chat_id: str,
    chat_title: str = "",
    project_name: str = "",
    raw_content: str,
) -> Optional[str]:
    """Generate a single AI outline title for a newly sent user turn (returns None if skipped/failed)."""
    snippet = extract_outline_title_source_text(raw_content)
    if not snippet:
        return None

    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return None
    bundle = await get_user_openrouter_secret_bundle(clean_uid)
    if (
        not bundle
        or not bool(bundle.get("has_key"))
        or not bool(bundle.get("is_enabled"))
        or not bool(bundle.get("smart_titles_enabled"))
    ):
        return None

    sanitized_snippet, _ = sanitize_outbound_openrouter_text(snippet)
    if not sanitized_snippet:
        return None

    chosen_model = (
        str(bundle.get("smart_titles_model") or DEFAULT_OPENROUTER_SMART_TITLES_MODEL).strip()
        or DEFAULT_OPENROUTER_SMART_TITLES_MODEL
    )

    def _parse_single(reply_txt: str) -> tuple[str, int]:
        cleaned = clean_generated_outline_title(reply_txt)
        if len(cleaned) >= 6:
            return cleaned, 1
        return "", 0

    res = await execute_openrouter_utility_call(
        clean_uid,
        bundle=bundle,
        system_prompt=_SINGLE_OUTLINE_TITLE_SYS_PROMPT,
        user_prompt=sanitized_snippet,
        chosen_model=chosen_model,
        max_tokens=90,
        temperature=0.2,
        purpose="outline_title",
        items_count=1,
        chat_id=chat_id,
        chat_title=chat_title,
        project_name=project_name,
        timeout_sec=35.0,
        parse_items_fn=_parse_single,
    )
    if not res.get("ok"):
        return None
    cleaned_title = str(res.get("parsed") or "")
    return cleaned_title if len(cleaned_title) >= 6 else None


async def generate_outline_titles_batch_chunk(
    user_id: str,
    items: list[dict[str, Any]],
    *,
    bundle: Optional[dict[str, Any]] = None,
    meta_out: Optional[dict[str, Any]] = None,
) -> dict[int, str]:
    """Generate up to `OUTLINE_TITLE_BATCH_SIZE` (25) outline titles in a single JSON request."""
    if not items:
        return {}
    chunk = list(items[:OUTLINE_TITLE_BATCH_SIZE])
    payload_items: list[dict[str, Any]] = []
    valid_ids: set[int] = set()
    first_chat_id = ""
    first_chat_title = ""
    first_project_name = ""

    for it in chunk:
        if not isinstance(it, dict):
            continue
        try:
            mid = int(it["id"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_snip = str(it.get("snippet") or "").strip()
        if not raw_snip and it.get("content"):
            raw_snip = extract_outline_title_source_text(str(it.get("content") or ""))
        if not raw_snip:
            continue
        sanitized_snip, _ = sanitize_outbound_openrouter_text(raw_snip)
        if not sanitized_snip:
            continue
        valid_ids.add(mid)
        payload_items.append({"id": mid, "text": sanitized_snip})
        if not first_chat_id and it.get("chat_id"):
            first_chat_id = str(it.get("chat_id") or "")
            first_chat_title = str(it.get("chat_title") or "")
            first_project_name = str(it.get("project_name") or "")

    if not payload_items:
        return {}

    clean_uid = (user_id or "").strip()
    eff_bundle = bundle if bundle is not None else await get_user_openrouter_secret_bundle(clean_uid)
    if not eff_bundle:
        if meta_out is not None:
            meta_out["stop_reason"] = "disabled"
        return {}

    chosen_model = (
        str(eff_bundle.get("smart_titles_model") or DEFAULT_OPENROUTER_SMART_TITLES_MODEL).strip()
        or DEFAULT_OPENROUTER_SMART_TITLES_MODEL
    )
    user_json_str = json.dumps(payload_items, ensure_ascii=False)
    max_tok = min(2600, 52 * len(payload_items) + 140)

    def _parse_chunk(reply_txt: str) -> tuple[dict[int, str], int]:
        parsed_map = _parse_batch_outline_titles_reply(reply_txt, valid_ids)
        return parsed_map, len(parsed_map)

    res = await execute_openrouter_utility_call(
        clean_uid,
        bundle=eff_bundle,
        system_prompt=_BATCH_OUTLINE_TITLES_SYS_PROMPT,
        user_prompt=user_json_str,
        chosen_model=chosen_model,
        max_tokens=max_tok,
        temperature=0.2,
        purpose="outline_title_batch",
        items_count=len(payload_items),
        chat_id=first_chat_id,
        chat_title=first_chat_title if len(payload_items) == 1 else f"Пакет заголовков ({len(payload_items)} шт.)",
        project_name=first_project_name,
        timeout_sec=45.0,
        parse_items_fn=_parse_chunk,
    )
    if meta_out is not None:
        meta_out["ok"] = bool(res.get("ok"))
        meta_out["status"] = str(res.get("status") or "")
        meta_out["stop_reason"] = str(res.get("stop_reason") or "")
        meta_out["error"] = str(res.get("error") or "")
        meta_out["cost_usd"] = float(res.get("cost_usd") or 0.0)
    if not res.get("ok"):
        return {}
    parsed = res.get("parsed")
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Streamable HTTP MCP Endpoint (`2025-03-26` JSON-RPC 2.0)
# ---------------------------------------------------------------------------


def _build_mcp_tools_schema(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    default_model = str(bundle.get("default_model") or "").strip()
    max_price = float(bundle.get("max_price_per_m") or 0.0)
    daily_limit = float(bundle.get("daily_spend_limit_usd") or 0.0)
    rules = str(bundle.get("custom_rules") or "").strip()

    desc_parts = [
        "Отправить запрос к внешней LLM через персональный шлюз OpenRouter.",
        f"Модель по умолчанию: {default_model or 'openrouter/auto'}.",
    ]
    if max_price > 0:
        desc_parts.append(f"Макс. цена модели: ${max_price:g}/1M токенов.")
    if daily_limit > 0:
        desc_parts.append(f"Суточный лимит: ${daily_limit:g}.")
    if rules:
        compact_rules = " ".join(rules.split())[:260]
        if len(rules) > 260:
            compact_rules += "…"
        desc_parts.append(f"Правила пользователя: {compact_rules}")

    return [
        {
            "name": "openrouter_chat",
            "description": " ".join(desc_parts),
            "annotations": {
                "title": "OpenRouter Chat",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Полный самодостаточный текст запроса (включая нужные фрагменты кода и контекст).",
                    },
                    "model": {
                        "type": "string",
                        "description": "ID модели OpenRouter (например `anthropic/claude-sonnet-4`, `openai/gpt-4.1`, `deepseek/deepseek-r1`). Если не указан — используется модель по умолчанию пользователя.",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Дополнительная системная роль для конкретной задачи (добавляется после личных правил пользователя).",
                    },
                    "temperature": {
                        "type": "number",
                        "description": "Температура генерации от 0.0 до 2.0.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Максимум выходных токенов (например 4096).",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Уровень рассуждений для поддерживающих моделей (рекомендуется 'medium' для соблюдения дедлайна 162 с).",
                    },
                    "explicit_user_choice": {
                        "type": "boolean",
                        "description": (
                            "Установи true ТОЛЬКО если человек в своём сообщении явно назвал конкретную модель "
                            "(например, 'спроси у Astra 6.1'). При самостоятельном выборе агентом всегда false."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "openrouter_multi_chat",
            "description": (
                "Параллельный опрос сразу 2..4 моделей OpenRouter за один шаг "
                "(«второе мнение / архитектурный консилиум» через asyncio.gather)."
            ),
            "annotations": {
                "title": "OpenRouter Multi-Model Council",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Общий самодостаточный запрос для всех опрашиваемых моделей.",
                    },
                    "models": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 4,
                        "description": "Список из 2–4 ID моделей OpenRouter для параллельного сравнения.",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Дополнительная системная инструкция для консилиума.",
                    },
                    "temperature": {
                        "type": "number",
                        "description": "Температура генерации от 0.0 до 2.0.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Максимум выходных токенов на каждую модель.",
                    },
                    "explicit_user_choice": {
                        "type": "boolean",
                        "description": (
                            "Установи true ТОЛЬКО если человек в своём сообщении явно назвал конкретную модель "
                            "(например, 'спроси у Astra 6.1'). При самостоятельном выборе агентом всегда false."
                        ),
                    },
                },
                "required": ["prompt", "models"],
            },
        },
        {
            "name": "openrouter_models",
            "description": (
                "Поиск актуальных ID моделей OpenRouter, размеров контекста и цен $/1M токенов "
                "по кэшируемому каталогу OpenRouter."
            ),
            "annotations": {
                "title": "Search OpenRouter Models",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Поисковая строка по ID или названию модели (например `claude`, `gpt`, `deepseek`, `qwen`, `coder`).",
                    },
                    "max_price_per_m": {
                        "type": "number",
                        "description": "Фильтр максимальной цены за 1M токенов в $ (0 = без фильтра).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимум результатов (по умолчанию 20, макс. 60).",
                    },
                },
            },
        },
    ]


def _format_single_chat_mcp_text(res: dict[str, Any]) -> str:
    if not res.get("ok"):
        return f"❌ OpenRouter ({res.get('model') or 'error'}): {res.get('error') or 'Неизвестная ошибка'}"
    meta_bits = [f"model: `{res.get('resolved_model') or res.get('requested_model')}`"]
    if res.get("provider_name"):
        meta_bits.append(f"provider: `{res['provider_name']}`")
    tok_info = f"{res.get('prompt_tokens', 0)}→{res.get('completion_tokens', 0)} tok"
    if res.get("reasoning_tokens"):
        tok_info += f" (reasoning {res['reasoning_tokens']})"
    meta_bits.append(tok_info)
    meta_bits.append(f"${float(res.get('cost_usd') or 0.0):.5f}")
    meta_bits.append(f"{res.get('latency_ms', 0)} ms")
    header = " · ".join(meta_bits)
    body = str(res.get("content") or "").strip() or "(Пустой текстовый ответ модели)"
    return f"[{header}]\n\n{body}"


def _format_multi_chat_mcp_text(res: dict[str, Any]) -> str:
    if "results" not in res:
        return f"❌ OpenRouter Multi-Chat: {res.get('error') or 'Ошибка запроса'}"
    sections: list[str] = []
    total_cost = 0.0
    for idx, item in enumerate(res["results"], start=1):
        model_lbl = item.get("resolved_model") or item.get("requested_model") or item.get("model") or f"Model #{idx}"
        if item.get("ok"):
            cost = float(item.get("cost_usd") or 0.0)
            total_cost += cost
            prov = f" ({item['provider_name']})" if item.get("provider_name") else ""
            hdr = (
                f"### {idx}. `{model_lbl}`{prov} — "
                f"{item.get('prompt_tokens', 0)}→{item.get('completion_tokens', 0)} tok · "
                f"${cost:.5f} · {item.get('latency_ms', 0)} ms"
            )
            sections.append(f"{hdr}\n\n{item.get('content') or '(Пустой ответ)'}")
        else:
            sections.append(f"### {idx}. `{model_lbl}` — ❌ {item.get('status', 'error')}\n\n{item.get('error')}")
    summary_hdr = f"Консилиум OpenRouter ({len(res['results'])} мод., суммарно ${total_cost:.5f}):"
    return summary_hdr + "\n\n" + "\n\n---\n\n".join(sections)


def _format_models_mcp_text(models: list[dict[str, Any]], *, query: str, price_cap: float) -> str:
    if not models:
        return (
            f"По запросу `{query or '*'}` (макс. цена: "
            f"{f'${price_cap:g}/1M' if price_cap > 0 else 'без лимита'}) модели не найдены."
        )
    lines = [
        f"Найдено моделей OpenRouter: {len(models)} "
        f"(запрос: `{query or '*'}`, фильтр цены: {f'<=${price_cap:g}/1M' if price_cap > 0 else 'нет'}):",
        "",
    ]
    for m in models:
        ctx_k = f"{int(round(m['context_length'] / 1000))}k" if m.get("context_length") else "?"
        if m.get("is_dynamic_pricing"):
            price_s = "dynamic $/1M"
        elif m.get("is_free"):
            price_s = "FREE ($0/1M)"
        else:
            price_s = f"in ${m['prompt_price_per_m']:g}/1M · out ${m['completion_price_per_m']:g}/1M"
        r_tag = " · reasoning" if m.get("supports_reasoning") else ""
        lines.append(f"- `{m['id']}` — {m['name']} | ctx {ctx_k} | {price_s}{r_tag}")
    return "\n".join(lines)


async def _dispatch_jsonrpc_message(
    msg: dict[str, Any],
    bundle: dict[str, Any],
    *,
    chat_id: str = "",
) -> Optional[dict[str, Any]]:
    req_id = msg.get("id")
    is_notification = "id" not in msg
    method = str(msg.get("method") or "").strip()
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if is_notification or method.startswith("notifications/"):
        return None

    if method == "initialize":
        client_proto = str(params.get("protocolVersion") or "").strip()
        negotiated = client_proto if client_proto in _SUPPORTED_MCP_PROTOCOLS else MCP_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": MCP_SERVER_NAME,
                    "version": "91.0.0",
                },
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _build_mcp_tools_schema(bundle)},
        }

    if method == "tools/call":
        tool_name = str(params.get("name") or "").strip()
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        call_deadline = time.monotonic() + OPENROUTER_MCP_HARD_DEADLINE_SEC

        try:
            if tool_name == "openrouter_chat":
                res = await execute_openrouter_chat(
                    bundle,
                    prompt=str(args.get("prompt") or ""),
                    model=args.get("model"),
                    system_prompt=args.get("system_prompt"),
                    temperature=args.get("temperature"),
                    max_tokens=args.get("max_tokens"),
                    reasoning_effort=args.get("reasoning_effort"),
                    explicit_user_choice=bool(args.get("explicit_user_choice", False)),
                    chat_id=chat_id,
                    deadline_mono=call_deadline,
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": _format_single_chat_mcp_text(res)}],
                        "isError": not bool(res.get("ok")),
                    },
                }

            if tool_name == "openrouter_multi_chat":
                raw_models = args.get("models") if isinstance(args.get("models"), list) else []
                res = await execute_openrouter_multi_chat(
                    bundle,
                    prompt=str(args.get("prompt") or ""),
                    models=[str(x) for x in raw_models],
                    system_prompt=args.get("system_prompt"),
                    temperature=args.get("temperature"),
                    max_tokens=args.get("max_tokens"),
                    explicit_user_choice=bool(args.get("explicit_user_choice", False)),
                    chat_id=chat_id,
                    deadline_mono=call_deadline,
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": _format_multi_chat_mcp_text(res)}],
                        "isError": not bool(res.get("ok")),
                    },
                }

            if tool_name == "openrouter_models":
                user_max = float(bundle.get("max_price_per_m") or 0.0)
                arg_max = args.get("max_price_per_m")
                eff_max = float(arg_max) if arg_max is not None else user_max
                lim = int(args.get("limit") or 20)
                q_str = str(args.get("search") or "")
                found = await search_openrouter_models(
                    query=q_str,
                    max_price_per_m=max(0.0, eff_max),
                    limit=max(1, min(lim, 60)),
                    include_deprecated=bool(args.get("include_deprecated", False)),
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": _format_models_mcp_text(found, query=q_str, price_cap=max(0.0, eff_max)),
                            }
                        ],
                        "isError": False,
                    },
                }
        except (TimeoutError, asyncio.TimeoutError):
            timeout_txt = (
                f"❌ OpenRouter (timeout): превышен лимит времени ожидания ответа "
                f"({int(OPENROUTER_MCP_HARD_DEADLINE_SEC)} с). Повторите вызов с "
                f"`reasoning_effort=\"medium\"` или уменьшите `max_tokens`."
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": timeout_txt}],
                    "isError": True,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Неизвестный инструмент MCP: {tool_name}"},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Метод JSON-RPC не поддерживается: {method}"},
    }


async def handle_mcp_openrouter_http(
    request: Request,
    mcp_token: str,
    *,
    chat_id: str = "",
) -> Response:
    """FastAPI Streamable HTTP MCP handler for `POST`, `GET`, `DELETE` `/mcp/openrouter/{mcp_token}`."""
    if not is_local_mcp_request(request):
        return JSONResponse(
            status_code=403,
            content={"error": "Доступ к локальному MCP-шлюзу разрешён только с 127.0.0.1 / Docker-моста."},
        )

    bundle = await verify_openrouter_mcp_token(mcp_token)
    if not bundle:
        return JSONResponse(
            status_code=403,
            content={"error": "MCP-токен OpenRouter недействителен, отозван или поставлен на паузу."},
        )

    eff_chat_id = (chat_id or str(request.query_params.get("chat_id") or "")).strip()

    method = request.method.upper()
    if method == "GET":
        # Per MCP 2025-03-26 Streamable HTTP spec: return 405 when standalone server-push SSE is not offered.
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
                "error": {"code": -32700, "message": "Некорректный JSON в запросе MCP"},
            },
        )

    if isinstance(payload, list):
        if not payload:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Пустой batch-массив JSON-RPC"},
                },
            )
        responses: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            resp_obj = await _dispatch_jsonrpc_message(item, bundle, chat_id=eff_chat_id)
            if resp_obj is not None:
                responses.append(resp_obj)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(status_code=200, content=responses)

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Ожидался JSON-RPC объект"},
            },
        )

    resp_obj = await _dispatch_jsonrpc_message(payload, bundle, chat_id=eff_chat_id)
    if resp_obj is None:
        return Response(status_code=202)
    return JSONResponse(status_code=200, content=resp_obj)
