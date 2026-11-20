from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from . import config, proxy_router
from .agy_credentials import create_agy_subprocess_exec, wait_for_turn_launches
from .agy_accounts import (parse_agy_import_payload, refresh_and_build_agy_token_payload,
    materialize_agy_account_home, sync_refreshed_agy_credentials_back,
    is_agy_quota_exhausted_error, _pick_next_agy_failover_account, agy_pool_exhausted,
    resolve_agy_account_for_chat_turn)
from .brain_reader import (
    ensure_isolated_project_git_boundary,
    extract_latest_turn_thinking,
    resolve_effective_project_execution_mode,
    resolve_project_dir,
)
from .config import (
    AGY_CLI_DATA_DIR,
    CHAT_SANDBOX_DIR,
    DOCKER_SANDBOX_IMAGE,
    IS_WINDOWS,
    UPLOADS_DIR,
    apply_isolated_home_to_env,
    ensure_user_cli_home,
    resolve_agy_bin,
    resolve_docker_bin,
    resolve_outbound_proxy,
    subprocess_hidden_kwargs,
    sync_user_cli_rules,
)

DEFAULT_MODELS: list[dict[str, str]] = [
    {"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)", "family": "gemini"},
    {"id": "gemini-4-carbon-medium", "label": "Gemini 4 Carbon (Medium)", "family": "gemini"},
    {"id": "gemini-4-carbon-low", "label": "Gemini 4 Carbon (Low)", "family": "gemini"},
    {"id": "gemini-4-neon-xhigh", "label": "Gemini 4 Neon (Extra High)", "family": "gemini"},
    {"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)", "family": "gemini"},
    {"id": "gemini-4-neon-medium", "label": "Gemini 4 Neon (Medium)", "family": "gemini"},
    {"id": "gemini-4-neon-low", "label": "Gemini 4 Neon (Low)", "family": "gemini"},
    {"id": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)", "family": "gemini"},
    {"id": "gemini-4-argon-medium", "label": "Gemini 4 Argon (Medium)", "family": "gemini"},
    {"id": "gemini-4-argon-low", "label": "Gemini 4 Argon (Low)", "family": "gemini"},
    {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)", "family": "gemini"},
    {"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)", "family": "gemini"},
    {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)", "family": "gemini"},
    {"id": "claude-sonnet-5-5-high", "label": "Claude Sonnet 5.5 (High)", "family": "claude"},
    {"id": "claude-sonnet-5-5-medium", "label": "Claude Sonnet 5.5 (Medium)", "family": "claude"},
    {"id": "claude-sonnet-5-5-low", "label": "Claude Sonnet 5.5 (Low)", "family": "claude"},
    {"id": "claude-opus-5-5-high", "label": "Claude Opus 5.5 (High)", "family": "claude"},
    {"id": "claude-opus-5-5-medium", "label": "Claude Opus 5.5 (Medium)", "family": "claude"},
    {"id": "claude-opus-5-5-low", "label": "Claude Opus 5.5 (Low)", "family": "claude"},
    {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)", "family": "gpt"},
]

_cached_models: Optional[list[dict[str, str]]] = None
_cached_models_ts: float = 0.0
_models_lock: Optional[asyncio.Lock] = None
MODELS_CACHE_TTL_SECONDS: float = 1800.0
_cli_auth_ok: bool = True
_cli_auth_detail: str = ""

_cli_version_cache: Optional[dict[str, Any]] = None
_cli_version_mtime_ns: int = -1
_cli_version_bin_path: str = ""

_logger = logging.getLogger("agy_hub.agy_engine")
_cli_update_lock: Optional[asyncio.Lock] = None


def _get_models_lock() -> asyncio.Lock:
    global _models_lock
    if _models_lock is None:
        _models_lock = asyncio.Lock()
    return _models_lock


def _get_cli_update_lock() -> asyncio.Lock:
    global _cli_update_lock
    if _cli_update_lock is None:
        _cli_update_lock = asyncio.Lock()
    return _cli_update_lock


def get_cached_models() -> list[dict[str, str]]:
    return list(_cached_models) if _cached_models else list(DEFAULT_MODELS)


def get_cli_auth_status() -> dict[str, Any]:
    return {
        "ok": _cli_auth_ok,
        "detail": _cli_auth_detail,
        "models_count": len(_cached_models) if _cached_models else len(DEFAULT_MODELS),
    }


MCP_DENY_PATTERNS: tuple[str, ...] = (
    "mcp(*)",
    "call_mcp_tool",
    "call_mcp_tool(*)",
    "mcp_openrouter_*",
    "mcp__*",
    "list_resources(*)",
    "read_resource(*)",
)

MCP_NON_BRIDGE_DENY_PATTERNS: tuple[str, ...] = (
    "mcp_openrouter_*",
    "list_resources(*)",
    "read_resource(*)",
)

HUB_OPENROUTER_ALLOW_PATTERNS: tuple[str, ...] = (
    "call_mcp_tool",
    "call_mcp_tool(*)",
    "call_mcp_tool(hub_openrouter*)",
    "call_mcp_tool(*hub_openrouter*)",
    "mcp(hub_openrouter/*)",
    "mcp(hub_openrouter:*)",
    "mcp(hub_openrouter/openrouter_chat)",
    "mcp(hub_openrouter/openrouter_multi_chat)",
    "mcp(hub_openrouter/openrouter_models)",
    "mcp(hub/openrouter_*)",
    "openrouter_chat",
    "openrouter_multi_chat",
    "openrouter_models",
    "mcp_hub_openrouter_openrouter_chat",
    "mcp_hub_openrouter_openrouter_multi_chat",
    "mcp_hub_openrouter_openrouter_models",
    "mcp__hub_openrouter__openrouter_chat",
    "mcp__hub_openrouter__openrouter_multi_chat",
    "mcp__hub_openrouter__openrouter_models",
)

HUB_OPENROUTER_DENY_PATTERNS: tuple[str, ...] = (
    "mcp(hub_openrouter/*)",
    "mcp(hub_openrouter:*)",
    "call_mcp_tool(hub_openrouter*)",
    "call_mcp_tool(*hub_openrouter*)",
    "mcp_hub_openrouter_*",
    "mcp__hub_openrouter__*",
    "openrouter_chat",
    "openrouter_multi_chat",
    "openrouter_models",
)

HUB_SANDBOX_ALLOW_PATTERNS: tuple[str, ...] = (
    "call_mcp_tool",
    "call_mcp_tool(*)",
    "call_mcp_tool(hub_sandbox*)",
    "call_mcp_tool(*hub_sandbox*)",
    "mcp(hub_sandbox/*)",
    "mcp(hub_sandbox:*)",
    "mcp(hub_sandbox/sandbox_exec)",
    "mcp(hub_sandbox/sandbox_service_ctl)",
    "mcp(hub_sandbox/sandbox_reset)",
    "sandbox_exec",
    "sandbox_service_ctl",
    "sandbox_reset",
    "mcp_hub_sandbox_*",
    "mcp__hub_sandbox__*",
    "mcp_hub_sandbox_sandbox_exec",
    "mcp_hub_sandbox_sandbox_service_ctl",
    "mcp_hub_sandbox_sandbox_reset",
    "mcp__hub_sandbox__sandbox_exec",
    "mcp__hub_sandbox__sandbox_service_ctl",
    "mcp__hub_sandbox__sandbox_reset",
)

HUB_SANDBOX_DENY_PATTERNS: tuple[str, ...] = (
    "mcp(hub_sandbox/*)",
    "mcp(hub_sandbox:*)",
    "call_mcp_tool(hub_sandbox*)",
    "call_mcp_tool(*hub_sandbox*)",
    "mcp_hub_sandbox_*",
    "mcp__hub_sandbox__*",
    "sandbox_exec",
    "sandbox_service_ctl",
    "sandbox_reset",
)

HUB_HOST_ALLOW_PATTERNS: tuple[str, ...] = (
    "call_mcp_tool",
    "call_mcp_tool(*)",
    "call_mcp_tool(hub_host*)",
    "call_mcp_tool(*hub_host*)",
    "mcp(hub_host/*)",
    "mcp(hub_host:*)",
    "mcp(hub_host/host_service_ctl)",
    "host_service_ctl",
    "mcp_hub_host_*",
    "mcp__hub_host__*",
    "mcp_hub_host_host_service_ctl",
    "mcp__hub_host__host_service_ctl",
)

HUB_HOST_DENY_PATTERNS: tuple[str, ...] = (
    "mcp(hub_host/*)",
    "mcp(hub_host:*)",
    "call_mcp_tool(hub_host*)",
    "call_mcp_tool(*hub_host*)",
    "mcp_hub_host_*",
    "mcp__hub_host__*",
    "host_service_ctl",
)

HUB_FILES_READ_ALLOW_PATTERNS: tuple[str, ...] = (
    "call_mcp_tool",
    "call_mcp_tool(*)",
    "call_mcp_tool(hub_files*)",
    "call_mcp_tool(*hub_files*)",
    "mcp(hub_files/read_file)",
    "mcp(hub_files/list_dir)",
    "mcp(hub_files/find_by_name)",
    "mcp(hub_files/grep_search)",
    "mcp(hub_files:read_file)",
    "mcp(hub_files:list_dir)",
    "mcp(hub_files:find_by_name)",
    "mcp(hub_files:grep_search)",
    "mcp_hub_files_read_file",
    "mcp_hub_files_list_dir",
    "mcp_hub_files_find_by_name",
    "mcp_hub_files_grep_search",
    "mcp__hub_files__read_file",
    "mcp__hub_files__list_dir",
    "mcp__hub_files__find_by_name",
    "mcp__hub_files__grep_search",
)

HUB_FILES_WRITE_DENY_PATTERNS: tuple[str, ...] = (
    "mcp(hub_files/write_file)",
    "mcp(hub_files/edit_file)",
    "mcp(hub_files:write_file)",
    "mcp(hub_files:edit_file)",
    "mcp_hub_files_write_file",
    "mcp_hub_files_edit_file",
    "mcp__hub_files__write_file",
    "mcp__hub_files__edit_file",
    "call_mcp_tool(*hub_files*write_file*)",
    "call_mcp_tool(*hub_files*edit_file*)",
)

HUB_FILES_DENY_PATTERNS: tuple[str, ...] = (
    "mcp(hub_files/*)",
    "mcp(hub_files:*)",
    "call_mcp_tool(hub_files*)",
    "call_mcp_tool(*hub_files*)",
    "mcp_hub_files_*",
    "mcp__hub_files__*",
    *HUB_FILES_WRITE_DENY_PATTERNS,
)

HOST_SHELL_DENY_PATTERNS: tuple[str, ...] = (
    "command(*)",
    "run_command",
    "run_command(*)",
)

SECRET_PROFILE_DENY_PATTERNS: tuple[str, ...] = (
    "read_file(*antigravity-oauth-token*)",
    "write_file(*antigravity-oauth-token*)",
    "edit_file(*antigravity-oauth-token*)",
    "read_file(*oauth-token*)",
    "write_file(*oauth-token*)",
    "edit_file(*oauth-token*)",
    "read_file(*mcp_config.json*)",
    "write_file(*mcp_config.json*)",
    "edit_file(*mcp_config.json*)",
    "read_file(*mcp_hub_runtime.json*)",
    "write_file(*mcp_hub_runtime.json*)",
    "edit_file(*mcp_hub_runtime.json*)",
    "read_file(*.credentials.json*)",
    "write_file(*.credentials.json*)",
    "edit_file(*.credentials.json*)",
    "read_file(*cli_homes*auth.json*)",
    "write_file(*cli_homes*auth.json*)",
    "edit_file(*cli_homes*auth.json*)",
    "read_file(*/.codex/*auth.json*)",
    "write_file(*/.codex/*auth.json*)",
    "edit_file(*/.codex/*auth.json*)",
    "read_file(*hub.db*)",
    "write_file(*hub.db*)",
    "edit_file(*hub.db*)",
    "read_file(*.secret_key*)",
    "write_file(*.secret_key*)",
    "edit_file(*.secret_key*)",
)

HEADLESS_ASYNC_DENY_PATTERNS: tuple[str, ...] = (
    "invoke_subagent",
    "invoke_subagent(*)",
    "define_subagent",
    "define_subagent(*)",
    "manage_subagents",
    "manage_subagents(*)",
    "send_message",
    "send_message(*)",
    "swarm",
    "swarm(*)",
    "post_message",
    "post_message(*)",
    "read_board",
    "read_board(*)",
    "swarm_wait",
    "swarm_wait(*)",
    "schedule",
    "schedule(*)",
    "run_workflow",
    "run_workflow(*)",
    "ask_question",
    "ask_question(*)",
)


def _should_use_desktop_globals(user: Optional[dict[str, Any]]) -> bool:
    """v59: 100% of users (including admin) run strictly in isolated `data/cli_homes/<safe_user_id>/`."""
    return False


_UNSET_PROXY: Any = object()


def _windows_agy_env(env: dict[str, str]) -> dict[str, str]:
    env = {key.upper(): value for key, value in env.items()}
    windows_dir = env.get("SYSTEMROOT") or env.get("WINDIR")
    if not windows_dir:
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetSystemWindowsDirectoryW(buffer, len(buffer))
        if not 0 < length < len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        windows_dir = buffer.value

    system32 = os.path.join(windows_dir, "System32")
    temp_dir = env.get("TEMP") or env.get("TMP")
    if not temp_dir:
        profile = env.get("USERPROFILE") or str(Path.home())
        local = env.get("LOCALAPPDATA") or os.path.join(profile, "AppData", "Local")
        temp_dir = os.path.join(local, "Temp")
    defaults = {
        "SYSTEMROOT": windows_dir,
        "WINDIR": windows_dir,
        "SYSTEMDRIVE": os.path.splitdrive(windows_dir)[0],
        "COMSPEC": os.path.join(system32, "cmd.exe"),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": temp_dir,
        "TMP": temp_dir,
    }
    for key, value in defaults.items():
        if not env.get(key):
            env[key] = value
    return env


def _apply_agy_proxy_env(env: dict[str, str], proxy_url: Optional[str]) -> dict[str, str]:
    proxy_router.apply_proxy_to_subprocess_env(env, proxy_url)
    if IS_WINDOWS:
        # The proxy router emits both spellings for case-sensitive platforms.
        return {key.upper(): value for key, value in env.items()}
    return env


def _build_subprocess_env(
    *,
    user: Optional[dict[str, Any]] = None,
    isolated_home_override: Optional[Path] = None,
    proxy_url: Any = _UNSET_PROXY,
) -> dict[str, str]:
    env = os.environ.copy()
    if IS_WINDOWS:
        env = _windows_agy_env(env)
    env.setdefault('GIT_TERMINAL_PROMPT', '0')
    env.setdefault('GCM_INTERACTIVE', 'never')
    eff_proxy = (
        proxy_router.resolve_proxy_sync("agy")
        if proxy_url is _UNSET_PROXY
        else proxy_url
    )
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"
    env["NVIDIA_VISIBLE_DEVICES"] = "none"

    if isolated_home_override is not None:
        apply_isolated_home_to_env(env, isolated_home_override)
    elif user is not None:
        cli_home = ensure_user_cli_home(user)
        sync_user_cli_rules(user, str(user.get("custom_rules") or ""))
        apply_isolated_home_to_env(env, cli_home)
    else:
        sys_home = ensure_user_cli_home("_system")
        apply_isolated_home_to_env(env, sys_home)

    return _apply_agy_proxy_env(env, eff_proxy)


def classify_model_family(model_id: str) -> str:
    m = (model_id or "").strip().lower()
    if m.startswith("claude-code:"):
        return "claude_code"
    if m.startswith("codex:"):
        return "gpt"
    if any(k in m for k in ("gemini", "carbon", "neon", "argon", "hydrogen")):
        return "gemini"
    if "claude" in m:
        return "claude"
    if any(k in m for k in ("gpt", "chatgpt", "openai", "o1-", "o3-", "o4-")) or re.search(
        r"\b(gpt|o1|o3|o4)\b", m
    ):
        return "gpt"
    return "other"


_classify_family = classify_model_family


def _extract_model_version_score(model_id: str) -> float:
    low = (model_id or "").lower()
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", low)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m_cld = re.search(r"claude-(?:opus|sonnet|haiku)-(\d+(?:[.-]\d+)?)", low)
    if m_cld:
        try:
            return float(m_cld.group(1).replace("-", "."))
        except ValueError:
            pass
    m_any = re.search(r"(\d+(?:\.\d+)?)", low)
    if m_any:
        try:
            return float(m_any.group(1))
        except ValueError:
            pass
    return 0.0


def _extract_model_tier(model_id: str) -> str:
    low = (model_id or "").strip().lower()
    if (
        low.endswith("-extra-high")
        or low.endswith("_extra_high")
        or low.endswith(" extra high")
        or low.endswith("@xhigh")
    ):
        return "xhigh"
    for t in (
        "xhigh",
        "high",
        "medium",
        "low",
        "max",
        "pro",
        "ultra",
        "thinking",
        "fast",
        "mini",
        "lite",
        "auto",
        "preview",
        "experimental",
    ):
        if low.endswith(f"-{t}") or low.endswith(f"@{t}"):
            return t
    return "medium"


def pick_default_model_id(models: Optional[list[dict[str, str]]] = None) -> str:
    pool = models if (models is not None and len(models) > 0) else (_cached_models or DEFAULT_MODELS)
    if not pool:
        return "gemini-4-carbon-high"
    ids_in_order = [str(m.get("id") or "").strip() for m in pool if m.get("id")]
    id_set = set(ids_in_order)

    for preferred in ("gemini-4-carbon-high", "gemini-4-neon-high", "gemini-4-argon-high"):
        if preferred in id_set:
            return preferred

    # Look for flagship Gemini *-high (not flash/lite/nano/hydrogen), preferring newest version & Carbon/Neon over Argon
    flagship_gemini_high = [
        mid
        for mid in ids_in_order
        if classify_model_family(mid) == "gemini"
        and _extract_model_tier(mid) == "high"
        and not any(x in mid.lower() for x in ("flash", "lite", "nano", "hydrogen"))
    ]
    if flagship_gemini_high:
        flagship_gemini_high.sort(
            key=lambda mid: (
                _extract_model_version_score(mid),
                2 if "carbon" in mid.lower() else (1 if "neon" in mid.lower() else 0),
            ),
            reverse=True,
        )
        return flagship_gemini_high[0]

    any_gemini_high = [
        mid
        for mid in ids_in_order
        if classify_model_family(mid) == "gemini" and mid.lower().endswith("-high")
    ]
    if any_gemini_high:
        return any_gemini_high[0]

    any_high = [mid for mid in ids_in_order if mid.lower().endswith("-high")]
    if any_high:
        return any_high[0]

    return ids_in_order[0]


def resolve_active_model_id(
    requested_id: str,
    available_models: list[dict[str, str]],
    cli_online: bool = True,
) -> str:
    req = (requested_id or "").strip()
    if not available_models:
        return req if (not cli_online and req) else pick_default_model_id()

    avail_ids = [str(m.get("id") or "").strip() for m in available_models if m.get("id")]
    if req and req in avail_ids:
        return req

    if not req:
        return pick_default_model_id(available_models)

    req_low = req.lower()
    target_fam = classify_model_family(req)
    target_tier = _extract_model_tier(req)
    req_is_flash_or_lite = any(x in req_low for x in ("flash", "lite", "nano", "hydrogen"))
    req_is_neon = "neon" in req_low
    req_is_carbon = "carbon" in req_low

    same_fam = [
        m for m in available_models if classify_model_family(str(m.get("id") or "")) == target_fam
    ]
    if same_fam:
        same_fam_ids = [str(m.get("id") or "").strip() for m in same_fam if m.get("id")]
        # Prefer same sub-class (flagship vs flash/lite/hydrogen) + same tier
        tier_matches = [
            mid for mid in same_fam_ids if _extract_model_tier(mid) == target_tier
        ]
        if target_fam == "gemini":
            preferred_subclass = [
                mid
                for mid in (tier_matches or same_fam_ids)
                if any(x in mid.lower() for x in ("flash", "lite", "nano", "hydrogen")) == req_is_flash_or_lite
            ]
            if preferred_subclass:
                def _gemini_rank(mid: str) -> tuple[float, int, int]:
                    ml = mid.lower()
                    exact_series = 1 if ((req_is_neon and "neon" in ml) or (req_is_carbon and "carbon" in ml)) else 0
                    flagship_bonus = 2 if ("carbon" in ml or "neon" in ml) else 0
                    return (_extract_model_version_score(mid), exact_series, flagship_bonus)

                preferred_subclass.sort(key=_gemini_rank, reverse=True)
                return preferred_subclass[0]
        if tier_matches:
            return tier_matches[0]
        return same_fam_ids[0]

    return pick_default_model_id(available_models)


def _is_cli_auth_error(detail: str) -> bool:
    low = (detail or "").lower()
    return any(
        kw in low
        for kw in (
            "not logged in",
            "login",
            "logged out",
            "auth",
            "credential",
            "token",
            "sign in",
            "401",
            "unauthorized",
            "unauthenticated",
        )
    )


async def probe_cli_auth_and_refresh_models() -> tuple[bool, list[dict[str, str]], str]:
    """Verify official agy.exe CLI session and refresh available models."""
    global _cached_models, _cached_models_ts, _cli_auth_ok, _cli_auth_detail

    agy_bin = resolve_agy_bin()
    if not Path(agy_bin).exists():
        _cached_models = _cached_models or list(DEFAULT_MODELS)
        _cli_auth_ok = False
        _cli_auth_detail = f"Не найден agy.exe: {agy_bin}"
        return False, _cached_models, _cli_auth_detail

    try:
        from . import database
        account = await database.get_active_account_profile()
        home = await materialize_agy_account_home(account)
        proxy_url = await proxy_router.ensure_proxy_for_context("agy", account_id=account['id'] if account else '')
        proc = await create_agy_subprocess_exec(
            agy_bin,
            "models",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_subprocess_env(proxy_url=proxy_url, isolated_home_override=home),
            priority="manual_telemetry",
            **subprocess_hidden_kwargs(),
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=25.0)
        if account and account['has_credentials']:
            await sync_refreshed_agy_credentials_back(account['id'])
        stdout_txt = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        stderr_txt = (stderr_b or b"").decode("utf-8", errors="replace").strip()

        if proc.returncode == 0 and stdout_txt:
            parsed: list[dict[str, str]] = []
            for raw_line in stdout_txt.splitlines():
                line = raw_line.strip()
                if not line or line.lower().startswith("fetching"):
                    continue
                if "\t" in line:
                    mid, mlabel = line.split("\t", 1)
                    mid = mid.strip()
                    mlabel = mlabel.strip()
                    if mid:
                        parsed.append(
                            {
                                "id": mid,
                                "label": mlabel or mid,
                                "family": classify_model_family(mid),
                            }
                        )
            if parsed:
                if account and account['has_credentials']:
                    await database.update_agy_account_telemetry(account['id'], models=parsed)
                _cached_models = parsed
                _cached_models_ts = time.monotonic()
                _cli_auth_ok = True
                _cli_auth_detail = ""
                return True, _cached_models, ""

        detail = stderr_txt or stdout_txt or f"Код выхода agy models: {proc.returncode}"
        if _cached_models and _cli_auth_ok and not _is_cli_auth_error(detail):
            # Temporary network blip: preserve last valid live models list & extend TTL by 300s
            _cached_models_ts = max(
                _cached_models_ts,
                time.monotonic() - MODELS_CACHE_TTL_SECONDS + 300.0,
            )
            return True, _cached_models, ""

        _cached_models = _cached_models or list(DEFAULT_MODELS)
        _cli_auth_ok = False
        _cli_auth_detail = detail
        return False, _cached_models, detail
    except proxy_router.ProxyRouteError as exc:
        detail = str(exc)
        _cached_models = _cached_models or list(DEFAULT_MODELS)
        _cli_auth_ok = False
        _cli_auth_detail = detail
        return False, _cached_models, detail
    except Exception as exc:
        detail = str(exc)
        if _cached_models and _cli_auth_ok and not _is_cli_auth_error(detail):
            _cached_models_ts = max(
                _cached_models_ts,
                time.monotonic() - MODELS_CACHE_TTL_SECONDS + 300.0,
            )
            return True, _cached_models, ""

        _cached_models = _cached_models or list(DEFAULT_MODELS)
        _cli_auth_ok = False
        _cli_auth_detail = detail
        return False, _cached_models, detail


async def fetch_available_models(force_refresh: bool = False) -> list[dict[str, str]]:
    now_mono = time.monotonic()
    if (
        not force_refresh
        and _cached_models is not None
        and len(_cached_models) > 0
        and _cli_auth_ok
        and (now_mono - _cached_models_ts) < MODELS_CACHE_TTL_SECONDS
    ):
        return _cached_models

    async with _get_models_lock():
        now_mono = time.monotonic()
        if (
            not force_refresh
            and _cached_models is not None
            and len(_cached_models) > 0
            and _cli_auth_ok
            and (now_mono - _cached_models_ts) < MODELS_CACHE_TTL_SECONDS
        ):
            return _cached_models
        _, models, _ = await probe_cli_auth_and_refresh_models()
        return models


async def get_cli_version_info(force_refresh: bool = False) -> dict[str, Any]:
    """Return agy.exe binary version and modification timestamp, cached by file mtime_ns."""
    global _cli_version_cache, _cli_version_mtime_ns, _cli_version_bin_path

    agy_bin = resolve_agy_bin()
    bin_path = Path(agy_bin)
    if not bin_path.exists():
        return {
            "bin_path": agy_bin,
            "version": "",
            "updated_at": "",
            "exists": False,
        }

    try:
        st = bin_path.stat()
        mtime_ns = int(st.st_mtime_ns)
        mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        mtime_ns = 0
        mtime_iso = ""

    if (
        not force_refresh
        and _cli_version_cache is not None
        and _cli_version_mtime_ns == mtime_ns
        and _cli_version_bin_path == agy_bin
    ):
        return _cli_version_cache

    ver_str = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            agy_bin,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_subprocess_env(proxy_url=None),
            **subprocess_hidden_kwargs(),
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        raw_out = (
            (stdout_b or b"").decode("utf-8", errors="replace").strip()
            or (stderr_b or b"").decode("utf-8", errors="replace").strip()
        )
        if raw_out:
            first_line = raw_out.splitlines()[0].strip()
            m_ver = re.search(r"v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", first_line)
            ver_str = m_ver.group(1) if m_ver else first_line
    except Exception:
        ver_str = ""

    info = {
        "bin_path": agy_bin,
        "version": ver_str,
        "updated_at": mtime_iso,
        "exists": True,
    }
    _cli_version_cache = info
    _cli_version_mtime_ns = mtime_ns
    _cli_version_bin_path = agy_bin
    return info


async def run_cli_self_update() -> dict[str, Any]:
    """Run official `agy.exe update` safely and refresh cached version & models."""
    async with _get_cli_update_lock():
        before_info = await get_cli_version_info()
        before_ver = str(before_info.get("version") or "")
        agy_bin = resolve_agy_bin()
        if not Path(agy_bin).exists():
            return {
                "ok": False,
                "before_version": before_ver,
                "after_version": before_ver,
                "updated": False,
                "output": f"Бинарник CLI не найден: {agy_bin}",
                "models_count": len(_cached_models or DEFAULT_MODELS),
                "cli_info": before_info,
                "models": get_cached_models(),
            }

        try:
            proxy_url = await proxy_router.ensure_proxy_for_context("agy")
            proc = await asyncio.create_subprocess_exec(
                agy_bin,
                "update",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_build_subprocess_env(proxy_url=proxy_url),
                **subprocess_hidden_kwargs(),
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=60.0)
            stdout_txt = (stdout_b or b"").decode("utf-8", errors="replace").strip()
            stderr_txt = (stderr_b or b"").decode("utf-8", errors="replace").strip()
            rc = proc.returncode or 0
            combined_out = "\n".join(x for x in (stdout_txt, stderr_txt) if x).strip()
        except proxy_router.ProxyRouteError as exc:
            return {
                "ok": False,
                "before_version": before_ver,
                "after_version": before_ver,
                "updated": False,
                "output": str(exc),
                "models_count": len(_cached_models or DEFAULT_MODELS),
                "cli_info": before_info,
                "models": get_cached_models(),
            }
        except asyncio.TimeoutError:
            rc = -1
            combined_out = "Превышено время ожидания (60 сек) при выполнении agy.exe update"
        except Exception as exc:
            rc = -1
            combined_out = f"Ошибка запуска agy.exe update: {exc}"

        after_info = await get_cli_version_info(force_refresh=True)
        after_ver = str(after_info.get("version") or "")
        models = await fetch_available_models(force_refresh=True)
        updated = bool(
            (before_ver and after_ver and before_ver != after_ver)
            or (
                before_info.get("updated_at")
                and after_info.get("updated_at")
                and before_info.get("updated_at") != after_info.get("updated_at")
            )
        )
        return {
            "ok": rc == 0,
            "before_version": before_ver,
            "after_version": after_ver,
            "updated": updated,
            "output": combined_out or ("Обновление завершено." if rc == 0 else f"Код выхода: {rc}"),
            "models_count": len(models),
            "cli_info": after_info,
            "models": models,
        }


_cached_quotas: Optional[dict[str, Any]] = None
_cached_quotas_ts: float = 0.0
_quota_cache_ts: float = 0.0
_quota_fetch_lock: asyncio.Lock = asyncio.Lock()
_quotas_lock: Optional[asyncio.Lock] = _quota_fetch_lock
_quota_cold_init_started: bool = False
_quota_cold_init_task: Optional[asyncio.Task[Any]] = None
QUOTA_CACHE_TTL_SECONDS: float = 900.0


def _get_quotas_lock() -> asyncio.Lock:
    return _quota_fetch_lock


def _parse_agy_iso_utc(raw_ts: Any) -> Optional[datetime]:
    s = str(raw_ts or "").strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _has_expired_quota_bucket(quotas: dict[str, Any]) -> bool:
    now_utc = datetime.now(timezone.utc)
    updated_dt = _parse_agy_iso_utc(quotas.get("updated_at"))
    for grp in quotas.get("groups") or []:
        if not isinstance(grp, dict):
            continue
        for bucket in grp.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            reset_dt = _parse_agy_iso_utc(bucket.get("reset_time"))
            if (
                reset_dt is not None
                and now_utc >= (reset_dt + timedelta(seconds=10))
                and (updated_dt is None or updated_dt < reset_dt)
            ):
                return True
    return False


def _is_fresh_valid_quota_snapshot(
    quotas: Any,
    cached_at: Any,
    max_age_sec: float = QUOTA_CACHE_TTL_SECONDS,
) -> bool:
    if not isinstance(quotas, dict) or quotas.get("ok") is not True:
        return False
    stamp = _parse_agy_iso_utc(cached_at)
    if stamp is None:
        return False
    if (datetime.now(timezone.utc) - stamp).total_seconds() >= max(0.0, float(max_age_sec)):
        return False
    return not _has_expired_quota_bucket(quotas)


def _deferred_quota_fallback(account: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    acc_q = account.get("quotas") if isinstance(account, dict) else None
    if isinstance(acc_q, dict) and acc_q.get("ok") is True:
        return acc_q
    if not account and isinstance(_cached_quotas, dict) and _cached_quotas.get("ok") is True:
        return _cached_quotas
    return {
        "ok": False,
        "deferred": True,
        "error": "",
        "groups": [],
        "remaining_credits": 0,
        "gemini_5h_pct": None,
        "gemini_weekly_pct": None,
        "claude_5h_pct": None,
        "claude_weekly_pct": None,
        "tier_summary": infer_account_tier(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def is_agy_quota_cache_stale(max_age_sec: float = 300.0) -> bool:
    """Non-blocking check: True if Google AI / Antigravity CLI quota cache is empty, failed (`ok != True`),
    older than `max_age_sec`, or any cached `gemini`/`claude` bucket has passed `reset_time + 10s`."""
    if not isinstance(_cached_quotas, dict) or _cached_quotas.get("ok") is not True:
        return True
    ts_val = _cached_quotas_ts or _quota_cache_ts
    if ts_val <= 0.0 or (time.monotonic() - ts_val) >= max(0.0, float(max_age_sec)):
        return True
    return _has_expired_quota_bucket(_cached_quotas)


def get_cached_account_quotas_fast() -> Optional[dict[str, Any]]:
    """Return last in-memory Google AI quota snapshot immediately (`0` blocking CLI calls).
    On cold start (`_cached_quotas is None`), schedules a single background warm-up if CLI binary exists."""
    global _quota_cold_init_started, _quota_cold_init_task
    if _cached_quotas is not None:
        return _cached_quotas
    if not _quota_cold_init_started:
        try:
            loop = asyncio.get_running_loop()
            if Path(resolve_agy_bin()).exists():
                _quota_cold_init_started = True
                _quota_cold_init_task = loop.create_task(fetch_account_quotas(force_refresh=False))
        except Exception:
            pass
    return _cached_quotas


def _build_quota_models_hint(
    models: Optional[list[dict[str, str]]] = None,
    pool: str = "gemini",
) -> str:
    m_list = models if (models is not None and len(models) > 0) else (_cached_models or DEFAULT_MODELS)
    series: list[str] = []
    for m in m_list:
        mid = str(m.get("id") or "")
        fam = classify_model_family(mid)
        if pool == "gemini" and fam != "gemini":
            continue
        if pool == "claude_gpt" and fam not in ("claude", "gpt"):
            continue
        raw_label = str(m.get("label") or mid).strip()
        base_name = re.sub(r"\s*\([^)]+\)\s*$", "", raw_label).strip()
        if base_name and base_name not in series:
            series.append(base_name)
    if series:
        return " · ".join(series[:4])
    if pool == "gemini":
        return "Gemini 4 Carbon · Gemini 4 Argon · Gemini 3.8 Flash"
    return "Claude Opus 5.5 · Sonnet 5.5 · GPT-OSS"


def infer_account_tier(models: Optional[list[dict[str, str]]] = None) -> str:
    if not _cli_auth_ok and models is None:
        return "Требуется вход"
    m_list = models if (models is not None and len(models) > 0) else (_cached_models or DEFAULT_MODELS)
    gem_versions: list[tuple[float, str]] = []
    cld_versions: list[tuple[float, str]] = []
    for m in m_list:
        mid = str(m.get("id") or "").lower()
        mlabel = str(m.get("label") or "")
        if "gemini" in mid:
            mg = re.search(r"gemini[- ](\d+(?:\.\d+)?)", mlabel, re.IGNORECASE) or re.search(
                r"gemini-(\d+(?:\.\d+)?)", mid
            )
            if mg:
                v_str = mg.group(1)
                try:
                    gem_versions.append((float(v_str), v_str))
                except ValueError:
                    pass
        elif "claude" in mid:
            mc = re.search(
                r"claude\s+(?:opus|sonnet|haiku)\s+(\d+(?:\.\d+)?)", mlabel, re.IGNORECASE
            ) or re.search(r"claude-(?:opus|sonnet|haiku)-(\d+(?:[.-]\d+)?)", mid)
            if mc:
                v_str = mc.group(1).replace("-", ".")
                try:
                    cld_versions.append((float(v_str), v_str))
                except ValueError:
                    pass

    best_gem = max(gem_versions, key=lambda x: x[0])[1] if gem_versions else ""
    best_cld = max(cld_versions, key=lambda x: x[0])[1] if cld_versions else ""

    if best_gem and best_cld:
        return f"Google AI · Gemini {best_gem} + Claude {best_cld}"
    if best_gem:
        return f"Google AI · Gemini {best_gem}"
    return f"Antigravity ({len(m_list)} мод.)"


def invalidate_agy_route_caches() -> None:
    """Immediately invalidate cached Google AI / Antigravity models & quotas when active profile or proxy route changes."""
    global _cached_models_ts, _cached_quotas, _cached_quotas_ts, _quota_cache_ts
    _cached_models_ts = 0.0
    _cached_quotas = None
    _cached_quotas_ts = 0.0
    _quota_cache_ts = 0.0


SLASH_COMMAND_TIMEOUT_SEC: float = 30.0


def _extract_slash_command_data(raw_text: str) -> Optional[dict[str, Any]]:
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("event") == "command_result":
            cmd_obj = evt.get("command") or {}
            if isinstance(cmd_obj.get("data"), dict):
                return cmd_obj["data"]
        if evt.get("event") == "result":
            res_obj = evt.get("result") or {}
            cmd_obj = res_obj.get("command") or {}
            if isinstance(cmd_obj.get("data"), dict):
                return cmd_obj["data"]
    return None


async def _run_slash_json_command(
    agy_bin: str,
    slash_cmd: str,
    *,
    proxy_url: Any = _UNSET_PROXY,
    cli_home=None,
    priority: str = "telemetry",
    timeout: float = SLASH_COMMAND_TIMEOUT_SEC,
    raise_on_deferred: bool = False,
) -> Optional[dict[str, Any]]:
    """Run a zero-token official agy slash command (/usage, /credits) in print stream-json mode."""
    from .agy_credentials import TelemetryDeferred, active_working_turns

    proc = None
    try:
        if priority == "telemetry":
            if active_working_turns:
                if raise_on_deferred:
                    raise TelemetryDeferred()
                return None
            await wait_for_turn_launches()
            if active_working_turns:
                if raise_on_deferred:
                    raise TelemetryDeferred()
                return None
        elif priority == "manual_telemetry":
            await wait_for_turn_launches()

        eff_proxy = (
            await proxy_router.ensure_proxy_for_context("agy")
            if proxy_url is _UNSET_PROXY
            else proxy_url
        )
        proc = await create_agy_subprocess_exec(
            agy_bin,
            "-p",
            slash_cmd,
            "--output-format",
            "stream-json",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_subprocess_env(proxy_url=eff_proxy, isolated_home_override=cli_home),
            priority=priority,
            **subprocess_hidden_kwargs(),
        )
        deadline = time.monotonic() + float(timeout)
        parsed_data: Optional[dict[str, Any]] = None

        if proc.stdout is not None and inspect.iscoroutinefunction(getattr(proc.stdout, "readline", None)):
            while True:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise asyncio.TimeoutError()
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=rem)
                if not raw_line:
                    break
                line_txt = (
                    raw_line
                    if isinstance(raw_line, str)
                    else raw_line.decode("utf-8", errors="replace")
                )
                parsed_data = _extract_slash_command_data(line_txt)
                if parsed_data is not None:
                    break
            if getattr(proc, "returncode", None) is None and inspect.iscoroutinefunction(getattr(proc, "wait", None)):
                try:
                    await asyncio.wait_for(
                        proc.wait(),
                        timeout=min(2.0, max(0.2, deadline - time.monotonic())),
                    )
                except Exception:
                    pass
        else:
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
            stdout_txt = (stdout_b or b"").decode("utf-8", errors="replace").strip()
            parsed_data = _extract_slash_command_data(stdout_txt)

        return parsed_data
    except TelemetryDeferred:
        if raise_on_deferred:
            raise
        return None
    except Exception:
        return None
    finally:
        if proc is not None:
            if getattr(proc, "returncode", None) is None:
                try:
                    await _terminate_hung_cli_proc(proc)
                except Exception:
                    pass
            if inspect.iscoroutinefunction(getattr(proc, "aclose_lease", None)):
                try:
                    await proc.aclose_lease()
                except Exception:
                    pass


async def fetch_account_quotas(force_refresh=False, account_id=None):
    global _cached_quotas, _cached_quotas_ts, _quota_cache_ts
    from . import database
    from . import agy_credentials
    eff_priority = "manual_telemetry" if force_refresh else "telemetry"
    account = await database.get_account_profile_by_id(account_id) if account_id else await database.get_active_account_profile()
    if not force_refresh and agy_credentials.active_working_turns:
        return _deferred_quota_fallback(account)
    if not account or not account.get('has_credentials'):
        return await _fetch_account_quotas(force_refresh, priority=eff_priority)
    acc_lock = _account_quota_locks.setdefault(str(account['id']), asyncio.Lock())
    async with acc_lock:
        fresh_acc = await database.get_account_profile_by_id(account['id']) or account
        if not force_refresh:
            if agy_credentials.active_working_turns:
                return _deferred_quota_fallback(fresh_acc)
            cached_q = fresh_acc.get('quotas')
            if _is_fresh_valid_quota_snapshot(cached_q, fresh_acc.get('cached_at')):
                current = await database.get_active_account_profile()
                if current and current['id'] == fresh_acc['id']:
                    _cached_quotas = cached_q
                    _cached_quotas_ts = _quota_cache_ts = time.monotonic()
                return cached_q
        account = fresh_acc
        home = await materialize_agy_account_home(account)
        try:
            try:
                snapshot = await _fetch_account_quotas(
                    True,
                    account=account,
                    cli_home=home,
                    _lock_held=True,
                    priority=eff_priority,
                    _raise_deferred=(not force_refresh),
                )
            except agy_credentials.TelemetryDeferred:
                return _deferred_quota_fallback(account)
            if snapshot.get('deferred'):
                return _deferred_quota_fallback(account)
            await database.update_agy_account_telemetry(account['id'], quotas=snapshot, last_error=snapshot.get('error', ''))
            current = await database.get_active_account_profile()
            if current and current['id'] == account['id']:
                _cached_quotas = snapshot
                _cached_quotas_ts = _quota_cache_ts = time.monotonic()
            return snapshot
        finally:
            await sync_refreshed_agy_credentials_back(account['id'])


async def refresh_all_agy_accounts_quotas(force_refresh: bool = True, *, fetch_fn=None):
    from . import database
    eff_fetch = fetch_fn or fetch_account_quotas
    semaphore = asyncio.Semaphore(1)
    refreshed_results: dict[str, dict[str, Any]] = {}

    async def refresh(account):
        async with semaphore:
            await wait_for_turn_launches()
            try:
                res = await fetch_account_quotas(force_refresh, account['id'])
                if isinstance(res, dict):
                    refreshed_results[str(account['id'])] = res
                return res
            except Exception:
                await database.update_agy_account_telemetry(account['id'], last_error='Не удалось обновить квоты')
                return None

    accounts = [a for a in await database.list_account_profiles() if a['is_enabled'] and a['has_credentials']]
    for acc in accounts:
        await refresh(acc)
    active = await database.get_active_account_profile()
    if active and str(active.get('id') or '') in refreshed_results:
        return refreshed_results[str(active['id'])]
    return await eff_fetch(force_refresh)


def get_cached_quotas() -> Optional[dict[str, Any]]:
    return _cached_quotas


_account_quota_locks: dict[str, asyncio.Lock] = {}


from contextlib import nullcontext


async def _fetch_account_quotas(
    force_refresh: bool = False,
    account=None,
    cli_home=None,
    *,
    _lock_held: bool = False,
    priority: Optional[str] = None,
    _raise_deferred: bool = False,
) -> dict[str, Any]:
    """Fetch live remaining model quotas (/usage) and G1 credits (/credits) via official agy CLI."""
    global _cached_quotas, _cached_quotas_ts, _quota_cache_ts
    from . import agy_credentials

    eff_priority = priority or ("manual_telemetry" if force_refresh else "telemetry")
    now_mono = time.monotonic()
    if (
        not account and not force_refresh
        and isinstance(_cached_quotas, dict)
        and _cached_quotas.get("ok") is True
        and (now_mono - _cached_quotas_ts) < QUOTA_CACHE_TTL_SECONDS
        and not is_agy_quota_cache_stale(QUOTA_CACHE_TTL_SECONDS)
    ):
        return _cached_quotas

    target_lock = (
        nullcontext()
        if _lock_held
        else (
            _account_quota_locks.setdefault(str(account["id"]), asyncio.Lock())
            if (account and account.get("id"))
            else _quota_fetch_lock
        )
    )
    async with target_lock:
        now_mono = time.monotonic()
        if (
            not account and not force_refresh
            and isinstance(_cached_quotas, dict)
            and _cached_quotas.get("ok") is True
            and (now_mono - _cached_quotas_ts) < QUOTA_CACHE_TTL_SECONDS
            and not is_agy_quota_cache_stale(QUOTA_CACHE_TTL_SECONDS)
        ):
            return _cached_quotas
        if account and account.get("id") and not force_refresh:
            from . import database
            fresh_acc = await database.get_account_profile_by_id(account["id"])
            if fresh_acc and _is_fresh_valid_quota_snapshot(fresh_acc.get("quotas"), fresh_acc.get("cached_at")):
                return fresh_acc["quotas"]

        if eff_priority == "telemetry" and agy_credentials.active_working_turns:
            if _raise_deferred:
                raise agy_credentials.TelemetryDeferred()
            return _deferred_quota_fallback(account)

        agy_bin = resolve_agy_bin()
        if not Path(agy_bin).exists():
            fallback = {
                "ok": False,
                "error": f"Не найден бинарник CLI: {agy_bin}",
                "groups": [],
                "remaining_credits": 0,
                "gemini_5h_pct": None,
                "gemini_weekly_pct": None,
                "claude_5h_pct": None,
                "claude_weekly_pct": None,
                "tier_summary": infer_account_tier(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if not account:
                _cached_quotas = fallback
            if not account:
                _cached_quotas_ts = now_mono
            if not account:
                _quota_cache_ts = now_mono
            return fallback

        try:
            proxy_url = await proxy_router.ensure_proxy_for_context("agy", account_id=account["id"] if account else "")
        except proxy_router.ProxyRouteError as exc:
            err_snap = {
                "ok": False,
                "error": str(exc),
                "groups": [],
                "remaining_credits": 0,
                "gemini_5h_pct": None,
                "gemini_weekly_pct": None,
                "claude_5h_pct": None,
                "claude_weekly_pct": None,
                "tier_summary": infer_account_tier(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if not account:
                _cached_quotas = err_snap
            if not account:
                _cached_quotas_ts = now_mono
            if not account:
                _quota_cache_ts = now_mono
            return err_snap

        try:
            usage_data = await _run_slash_json_command(
                agy_bin,
                "/usage",
                proxy_url=proxy_url,
                cli_home=cli_home,
                priority=eff_priority,
                raise_on_deferred=(eff_priority == "telemetry"),
            )
            credits_data = await _run_slash_json_command(
                agy_bin,
                "/credits",
                proxy_url=proxy_url,
                cli_home=cli_home,
                priority=eff_priority,
                raise_on_deferred=(eff_priority == "telemetry"),
            )
        except agy_credentials.TelemetryDeferred:
            if _raise_deferred:
                raise
            return _deferred_quota_fallback(account)

        groups_out: list[dict[str, Any]] = []
        gemini_5h_pct: Optional[float] = None
        gemini_weekly_pct: Optional[float] = None
        claude_5h_pct: Optional[float] = None
        claude_weekly_pct: Optional[float] = None

        if isinstance(usage_data, dict):
            for g in usage_data.get("groups") or []:
                raw_name = str(g.get("name") or "").strip()
                low_name = raw_name.lower()
                if "gemini" in low_name:
                    gid = "gemini"
                    title = "Модели Gemini"
                    models_hint = _build_quota_models_hint(pool="gemini")
                elif "claude" in low_name or "gpt" in low_name:
                    gid = "claude_gpt"
                    title = "Claude и GPT"
                    models_hint = _build_quota_models_hint(pool="claude_gpt")
                else:
                    gid = low_name.replace(" ", "_") or "other"
                    title = raw_name or "Другие модели"
                    models_hint = str(g.get("description") or "")

                raw_buckets = list(g.get("buckets") or [])
                # Show 5h active window first, weekly second
                raw_buckets.sort(key=lambda b: 0 if str(b.get("window")) == "5h" else 1)

                buckets_out: list[dict[str, Any]] = []
                for b in raw_buckets:
                    win = str(b.get("window") or "")
                    frac_raw = b.get("remaining_fraction")
                    try:
                        frac = max(0.0, min(1.0, float(frac_raw)))
                    except (TypeError, ValueError):
                        frac = 0.0
                    rem_pct = round(frac * 100.0, 1)
                    used_pct = round((1.0 - frac) * 100.0, 1)
                    label = (
                        "Лимит 5 часов"
                        if win == "5h"
                        else ("Недельный лимит" if win == "weekly" else str(b.get("name") or win))
                    )
                    reset_time = str(b.get("reset_time") or "")
                    buckets_out.append(
                        {
                            "id": str(b.get("id") or f"{gid}-{win}"),
                            "window": win,
                            "label": label,
                            "remaining_fraction": frac,
                            "remaining_pct": rem_pct,
                            "used_pct": used_pct,
                            "reset_time": reset_time,
                        }
                    )
                    if gid == "gemini":
                        if win == "5h":
                            gemini_5h_pct = rem_pct
                        elif win == "weekly":
                            gemini_weekly_pct = rem_pct
                    elif gid == "claude_gpt":
                        if win == "5h":
                            claude_5h_pct = rem_pct
                        elif win == "weekly":
                            claude_weekly_pct = rem_pct

                groups_out.append(
                    {
                        "id": gid,
                        "raw_name": raw_name,
                        "title": title,
                        "models_hint": models_hint,
                        "buckets": buckets_out,
                    }
                )

        rem_credits = 0
        if isinstance(credits_data, dict):
            try:
                rem_credits = int(credits_data.get("remaining_credits") or 0)
            except (TypeError, ValueError):
                rem_credits = 0

        ok = len(groups_out) > 0
        snapshot = {
            "ok": ok,
            "error": "" if ok else "Не удалось получить квоты (проверьте вход в аккаунт CLI)",
            "groups": groups_out,
            "remaining_credits": rem_credits,
            "gemini_5h_pct": gemini_5h_pct,
            "gemini_weekly_pct": gemini_weekly_pct,
            "claude_5h_pct": claude_5h_pct,
            "claude_weekly_pct": claude_weekly_pct,
            "tier_summary": infer_account_tier(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if not account:
            _cached_quotas = snapshot
        if not account:
            _cached_quotas_ts = time.monotonic()
        if not account:
            _quota_cache_ts = _cached_quotas_ts
        return snapshot


@dataclass
class ActiveRun:
    chat_id: str
    process: Optional[asyncio.subprocess.Process] = None
    cancelled: bool = False
    thinking_active: bool = True
    seq: int = 0
    accumulated_text: str = ""
    accumulated_thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    user_id: str = ""
    kind: str = "chat"
    project_name: str = ""
    project_dir: Optional[Path] = None
    workspace_dir: Optional[Path] = None
    chat_title: str = ""
    access_mode: str = "read_only"
    sandbox_allowed: bool = False
    host_allowed: bool = False
    sandbox_network: str = "internet"
    extra_dirs: list[Path] = field(default_factory=list)
    scope_key: str = ""
    started_ts: float = field(default_factory=time.monotonic)
    active_files: set[str] = field(default_factory=set)
    model: str = ""
    engine: str = ""


ACTIVE_RUNS: dict[str, ActiveRun] = {}

_WRITING_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "edit_file",
        "write_file",
        "create_file",
        "replace_file_content",
        "multi_replace_file_content",
        "notebookedit",
        "write_to_file",
        "apply_patch",
        "delete_file",
        "mcp_hub_files_write_file",
        "mcp_hub_files_edit_file",
        "mcp__hub_files__write_file",
        "mcp__hub_files__edit_file",
    }
)

_MODIFYING_OR_EXEC_TOOL_NAMES: frozenset[str] = _WRITING_TOOL_NAMES | frozenset(
    {
        "run_command",
        "bash",
        "execute_command",
        "command",
        "sandbox_exec",
        "mcp_hub_sandbox_sandbox_exec",
        "mcp__hub_sandbox__sandbox_exec",
        "host_service_ctl",
        "mcp_hub_host_host_service_ctl",
        "mcp__hub_host__host_service_ctl",
    }
)

_MODE_SHORT_LABELS_RU: dict[str, str] = {
    "read_only": "План",
    "write_files": "Код",
    "full_access": "Терминал",
}


def register_chat_turn(
    chat_id: str,
    *,
    user_id: str = "",
    project_name: str = "",
    chat_title: str = "",
    access_mode: str = "read_only",
    scope_key: str = "",
    model: str = "",
    engine: str = "",
) -> ActiveRun:
    run = ActiveRun(
        chat_id=chat_id,
        thinking_active=True,
        user_id=user_id,
        project_name=project_name,
        chat_title=chat_title,
        access_mode=access_mode,
        scope_key=scope_key,
        model=model,
        engine=engine,
    )
    ACTIVE_RUNS[chat_id] = run
    return run


def find_active_turn_for_user(user_id: str, chat_id: str = "") -> Optional[dict[str, Any]]:
    """Return active turn context (`chat_id`, `chat_title`, `project_name`, `caller_model`, `caller_engine`, `caller_family`) for `user_id`."""
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return None
    clean_cid = (chat_id or "").strip()
    best: Optional[ActiveRun] = None
    if clean_cid:
        exact = ACTIVE_RUNS.get(clean_cid)
        if (
            exact is not None
            and not exact.cancelled
            and (exact.user_id or "").strip() == clean_uid
        ):
            best = exact
    if best is None:
        for run in list(ACTIVE_RUNS.values()):
            if run.cancelled:
                continue
            if (run.user_id or "").strip() != clean_uid:
                continue
            if best is None or run.started_ts >= best.started_ts:
                best = run
    if best is None:
        return None
    raw_mod = (best.model or "").strip()
    raw_mod_low = raw_mod.lower()
    raw_eng = (best.engine or "").strip().lower()
    if raw_mod_low.startswith("claude-code:") or raw_eng == "claude_code":
        caller_family = "claude"
    elif raw_mod_low.startswith("codex:") or raw_eng == "codex":
        caller_family = "gpt"
    else:
        fam = classify_model_family(raw_mod)
        caller_family = "claude" if fam == "claude_code" else fam
    return {
        "chat_id": best.chat_id,
        "chat_title": best.chat_title,
        "project_name": best.project_name,
        "caller_model": best.model,
        "caller_engine": best.engine,
        "caller_family": caller_family,
    }


def format_rel_project_path(raw_path: str, proj_dir: Optional[Path] = None) -> str:
    clean = str(raw_path or "").strip().strip("\"'")
    if not clean:
        return ""
    try:
        p = Path(clean)
        if proj_dir is not None and p.is_absolute():
            rel = p.resolve().relative_to(proj_dir.resolve()).as_posix()
            if rel and rel != ".":
                return rel[:120]
        if not p.is_absolute():
            norm = clean.replace("\\", "/").lstrip("./")
            if norm:
                return norm[:120]
        return p.name[:120]
    except Exception:
        norm = clean.replace("\\", "/")
        parts = [seg for seg in norm.split("/") if seg and seg != "."]
        return "/".join(parts[-2:])[:120] if parts else ""


def extract_writing_tool_paths(
    tool_name: str,
    parameters: Any,
    proj_dir: Optional[Path] = None,
) -> list[str]:
    t_low = (tool_name or "").strip().lower()
    is_write = t_low in _WRITING_TOOL_NAMES or "write_file" in t_low or "edit_file" in t_low
    if not is_write or not isinstance(parameters, dict):
        return []
    out: list[str] = []
    sources: list[dict[str, Any]] = [parameters]
    nested_args = parameters.get("Arguments") or parameters.get("arguments")
    if isinstance(nested_args, dict):
        sources.append(nested_args)
    for src in sources:
        for key in ("Path", "TargetFile", "file_path", "path", "target_file", "NotebookPath"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                rel = format_rel_project_path(val, proj_dir)
                if rel and rel not in out:
                    out.append(rel)
    return out


def get_parallel_project_runs(
    *,
    project_name: str,
    scope_key: str = "",
    exclude_chat_id: str = "",
) -> list[dict[str, Any]]:
    clean_proj = (project_name or "").strip().lower()
    clean_scope = (scope_key or "").strip()
    if not clean_proj and not clean_scope:
        return []
    out: list[dict[str, Any]] = []
    for cid, run in list(ACTIVE_RUNS.items()):
        if cid == exclude_chat_id or run.cancelled:
            continue
        if clean_scope and run.scope_key:
            if run.scope_key != clean_scope:
                continue
        elif (run.project_name or "").strip().lower() != clean_proj:
            continue
        out.append(
            {
                "chat_id": cid,
                "chat_title": (run.chat_title or "Параллельная вкладка").strip(),
                "access_mode": run.access_mode or "read_only",
                "active_files": sorted(run.active_files)[:8],
            }
        )
    return out


def build_project_pulse_block(
    *,
    project_name: str,
    chat_id: str,
    is_first_turn: bool,
    scope_key: str = "",
    changelog: Optional[list[dict[str, Any]]] = None,
    recent_chats: Optional[list[dict[str, Any]]] = None,
    prev_chat_updated_at: str = "",
) -> str:
    """
    Build a compact (<PROJECT_PULSE>, ~200-450 tokens max) situational awareness block
    for `kind == 'project'` with smart delta suppression:
      - First turn of a CLI session (`is_first_turn == True`): full pulse.
      - Subsequent turns of a live CLI session: emitted ONLY if a parallel project tab
        is active right now OR a newer changelog entry appeared since `prev_chat_updated_at`.
        Otherwise returns "" (0 extra tokens).
    """
    clean_proj = (project_name or "").strip()
    if not clean_proj:
        return ""

    parallel_runs = get_parallel_project_runs(
        project_name=clean_proj,
        scope_key=scope_key,
        exclude_chat_id=chat_id,
    )
    cl_items = list(changelog or [])
    sibling_chats = [
        c for c in (recent_chats or []) if str(c.get("id") or "") != chat_id
    ][:6]

    newer_changelog: list[dict[str, Any]] = []
    if prev_chat_updated_at:
        for entry in cl_items:
            e_ts = str(entry.get("ts") or "")
            e_cid = str(entry.get("chat_id") or "")
            is_other_or_pinned = (e_cid != chat_id) or bool(entry.get("pinned"))
            if is_other_or_pinned and e_ts > prev_chat_updated_at:
                newer_changelog.append(entry)

    if not is_first_turn:
        if not parallel_runs and not newer_changelog:
            return ""

    if is_first_turn and not parallel_runs and not cl_items and not sibling_chats:
        return ""

    lines: list[str] = [f'<PROJECT_PULSE project="{clean_proj}">']

    if parallel_runs:
        lines.append("Активные параллельные вкладки этого проекта прямо сейчас:")
        for pr in parallel_runs[:5]:
            m_lbl = _MODE_SHORT_LABELS_RU.get(pr["access_mode"], pr["access_mode"])
            title_s = pr["chat_title"]
            afiles = pr.get("active_files") or []
            if afiles:
                files_str = ", ".join(afiles)
                lines.append(
                    f'- ⚠️ СВЕТОФОР ФАЙЛОВ: параллельная вкладка "{title_s}" ({m_lbl}) '
                    f"прямо сейчас редактирует файлы: {files_str}. "
                    "НЕ перезаписывай эти файлы одновременно с ней без согласования!"
                )
            else:
                lines.append(f'- Вкладка "{title_s}" ({m_lbl}) сейчас выполняет запрос.')

    entries_to_show = cl_items[:6] if is_first_turn else (newer_changelog[:5] or cl_items[:3])
    if entries_to_show:
        hdr = (
            "Последние изменения и решения в проекте:"
            if is_first_turn
            else "Новые изменения в проекте с момента прошлого хода:"
        )
        lines.append(hdr)
        for ce in entries_to_show:
            ts_raw = str(ce.get("ts") or "")
            ts_short = ts_raw[5:16].replace("T", " ") if len(ts_raw) >= 16 else ts_raw[:16]
            c_title = str(ce.get("chat_title") or "Проект")[:48]
            pin_mark = "📌 " if ce.get("pinned") else ""
            c_files = ce.get("files") or []
            files_part = f" [{', '.join(c_files[:4])}]" if c_files else ""
            c_sum = str(ce.get("summary") or "").strip()[:150]
            lines.append(f"- ({ts_short} · {c_title}){files_part} {pin_mark}{c_sum}")

    if is_first_turn and sibling_chats:
        lines.append("Соседние вкладки этого проекта:")
        for sc in sibling_chats[:6]:
            sc_title = str(sc.get("title") or "Чат").strip()[:56]
            sc_mode = _MODE_SHORT_LABELS_RU.get(
                str(sc.get("access_mode") or "read_only"), "План"
            )
            bm_cnt = int(sc.get("bookmark_count") or 0)
            bm_part = f" · 🔖×{bm_cnt}" if bm_cnt > 0 else ""
            lines.append(f'- "{sc_title}" ({sc_mode}{bm_part})')

    lines.append("</PROJECT_PULSE>")
    block = "\n".join(lines)
    if len(block) > 1850:
        block = block[:1820].rstrip() + "\n…</PROJECT_PULSE>"
    return block


_INTERIM_WAIT_PHRASES: tuple[str, ...] = (
    "ожидаю завершения",
    "ожидаю результата",
    "ожидаю окончания",
    "ожидаю ответа",
    "ожидаю субагент",
    "дожидаюсь завершения",
    "дожидаюсь окончания",
    "дожидаюсь результата",
    "ожидаем завершения",
    "жду завершения",
    "жду результата",
    "жду окончания",
    "жду ответа",
    "жду их завершения",
    "запустил субагент",
    "делегировал субагент",
    "раздал задач",
    "как только они закончат",
    "как только субагент",
    "фоновая задача запущена",
    "waiting for it to finish",
    "will wait for it to finish",
    "waiting for the command",
    "waiting for completion",
    "waiting for subagent",
    "waiting for background",
    "waiting for worker",
    "waiting for them to finish",
    "once they complete",
    "will report back once",
)


def _is_interim_wait_status_line(line: str) -> bool:
    """Return True if `line` is an interim CLI status notice emitted while waiting on a background task."""
    clean = re.sub(r"^[#>*\-\d.)\s]+", "", line or "").strip()
    clean = re.sub(r"[*_`]+", "", clean).strip().lower()
    if not clean:
        return False
    norm = re.sub(r"\s+", " ", clean)
    return any(p in norm for p in _INTERIM_WAIT_PHRASES)


def build_auto_changelog_entry_from_turn(
    *,
    chat_id: str,
    chat_title: str,
    access_mode: str,
    user_prompt: str,
    assistant_text: str,
    tool_calls: list[dict[str, Any]],
    proj_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """
    Pure Python (0 ms, 0 LLM calls) filter & builder for project auto-changelog:
      - `read_only` (`План`) NEVER writes to auto-changelog.
      - Requires `write_files` or `full_access` AND at least one real modifying/command tool call.
    """
    if access_mode not in ("write_files", "full_access"):
        return None
    if not tool_calls:
        return None

    has_modifying_action = False
    touched_files: list[str] = []
    seen_files: set[str] = set()

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        tname = str(tc.get("name") or "").strip()
        t_low = tname.lower()
        params = tc.get("parameters") if isinstance(tc.get("parameters"), dict) else {}

        lazy_tool_low = str(
            params.get("ToolName") or params.get("tool_name") or ""
        ).strip().lower()
        is_mod = (
            t_low in _MODIFYING_OR_EXEC_TOOL_NAMES
            or "write_file" in t_low
            or "edit_file" in t_low
            or "sandbox_exec" in t_low
            or (t_low == "call_mcp_tool" and lazy_tool_low == "sandbox_exec")
        )
        if not is_mod:
            continue
        has_modifying_action = True

        for rel_p in extract_writing_tool_paths(tname, params, proj_dir):
            p_low = rel_p.lower()
            if p_low not in seen_files:
                seen_files.add(p_low)
                touched_files.append(rel_p)
                if len(touched_files) >= 6:
                    break

    if not has_modifying_action:
        return None

    # Extract concise <=160 char 1-line summary from prompt / first clean conclusion thesis
    clean_prompt = re.sub(
        r"<(?:CONTEXT_SUMMARY|PROJECT_PULSE|REFERENCED_CHAT)\b[^>]*>[\s\S]*?</(?:CONTEXT_SUMMARY|PROJECT_PULSE|REFERENCED_CHAT)>",
        "",
        user_prompt or "",
        flags=re.IGNORECASE,
    )
    clean_prompt = re.sub(r"```[\s\S]*?```", " ", clean_prompt)
    clean_prompt = re.sub(r"[#>*`_]+", " ", clean_prompt)
    clean_prompt = re.sub(r"\s+", " ", clean_prompt).strip()

    first_thesis = ""
    if assistant_text:
        no_code = re.sub(r"```[\s\S]*?```", "", assistant_text)
        for raw_ln in no_code.splitlines():
            ln = re.sub(r"^[#>*\-\d.)\s]+", "", raw_ln).strip()
            ln = re.sub(r"[*_`]+", "", ln).strip()
            if _is_interim_wait_status_line(ln):
                continue
            if len(ln) >= 18 and not ln.endswith(":"):
                first_thesis = re.sub(r"\s+", " ", ln)
                break

    summary_src = first_thesis or clean_prompt or "Выполнены изменения в проекте"
    if len(summary_src) > 160:
        summary_src = summary_src[:159].rstrip() + "…"

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "chat_title": (chat_title or "Код").strip()[:80],
        "files": touched_files[:6],
        "summary": summary_src,
    }


async def _terminate_hung_cli_proc(proc: asyncio.subprocess.Process) -> None:
    """Safely terminate a CLI process that already emitted `result` but failed to close pipes, and close its transport."""
    try:
        if proc.returncode is None:
            pid = proc.pid
            try:
                proc.kill()
            except Exception:
                pass
            waited_ok = False
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
                waited_ok = True
            except Exception:
                pass
            if not waited_ok and os.name == "nt" and pid:
                try:
                    await asyncio.to_thread(
                        subprocess.run,
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2.5,
                        **subprocess_hidden_kwargs(),
                    )
                except Exception:
                    pass
    finally:
        raw_proc = getattr(proc, "proc", proc)
        transport = getattr(raw_proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
            await asyncio.sleep(0)


def agy_session_db_exists(
    conv_id: str,
    cli_home: Optional[Path] = None,
    account_id: str = "",
) -> bool:
    """Return True if `conv_id.db` exists in the exact conversation directory for this turn.
    Isolated account turns NEVER inspect the global `AGY_CLI_DATA_DIR/conversations` folder."""
    clean_cid = str(conv_id or "").strip()
    if not clean_cid or ".." in clean_cid or not re.fullmatch(r"[0-9a-zA-Z_.-]{1,120}", clean_cid):
        return False
    clean_acc = str(account_id or "").strip()
    is_isolated = bool(clean_acc) or (
        cli_home is not None and ".agy_homes" in Path(cli_home).parts
    )
    if is_isolated:
        cand_files: list[Path] = []
        if clean_acc:
            try:
                acc_data = config.ensure_agy_account_cli_data_dir(clean_acc)
                cand_files.append(acc_data / "conversations" / f"{clean_cid}.db")
            except Exception:
                pass
        if cli_home is not None:
            cand_files.append(
                Path(cli_home) / ".gemini" / "antigravity-cli" / "conversations" / f"{clean_cid}.db"
            )
        return any(p.is_file() for p in cand_files)

    if cli_home is not None:
        if (Path(cli_home) / ".gemini" / "antigravity-cli" / "conversations" / f"{clean_cid}.db").is_file():
            return True
    return (AGY_CLI_DATA_DIR / "conversations" / f"{clean_cid}.db").is_file()


def clear_chat_turn(chat_id: str) -> None:
    ACTIVE_RUNS.pop(chat_id, None)


def is_chat_generating(chat_id: str) -> bool:
    return chat_id in ACTIVE_RUNS


def get_active_run_snapshot(chat_id: str) -> Optional[dict[str, Any]]:
    run = ACTIVE_RUNS.get(chat_id)
    if not run:
        return None
    return {
        "content": run.accumulated_text,
        "thinking": run.accumulated_thinking,
        "tool_calls": [dict(tc) for tc in run.tool_calls],
        "thinking_active": bool(run.thinking_active and not run.accumulated_text),
        "stream_seq": int(run.seq),
    }


async def cancel_chat_generation(chat_id: str) -> bool:
    run = ACTIVE_RUNS.get(chat_id)
    if not run:
        return False
    run.cancelled = True
    try:
        from .sandbox_service import cancel_chat_sandbox_execs

        await cancel_chat_sandbox_execs(chat_id)
    except Exception:
        pass
    proc = run.process
    if proc and proc.returncode is None:
        pid = proc.pid
        if os.name == "nt" and pid:
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                    **subprocess_hidden_kwargs(),
                )
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        elif pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
    return True


_docker_cache: Optional[dict[str, Any]] = None
_docker_cache_ts: float = 0.0
_last_synced_sandbox_ready: Optional[bool] = None
DOCKER_STATUS_CACHE_TTL_SECONDS: float = 60.0
DOCKER_STATUS_NEGATIVE_CACHE_TTL_SECONDS: float = 10.0


def _schedule_sandbox_cli_resync_if_changed(sandbox_ready: bool) -> None:
    global _last_synced_sandbox_ready
    if _last_synced_sandbox_ready is sandbox_ready:
        return
    _last_synced_sandbox_ready = sandbox_ready
    try:
        from . import sandbox_service

        loop = asyncio.get_running_loop()
        loop.create_task(
            sandbox_service.sync_all_users_sandbox_cli_files(docker_ready=sandbox_ready)
        )
    except Exception:
        pass


async def check_docker_status(force_refresh: bool = False) -> dict[str, Any]:
    """Detect Docker Engine and sandbox image readiness for Split-Plane MCP `hub_sandbox`."""
    global _docker_cache, _docker_cache_ts
    now_m = time.monotonic()
    if not force_refresh and _docker_cache is not None:
        ttl = (
            DOCKER_STATUS_CACHE_TTL_SECONDS
            if _docker_cache.get("sandbox_mcp_ready")
            else DOCKER_STATUS_NEGATIVE_CACHE_TTL_SECONDS
        )
        if (now_m - _docker_cache_ts) < ttl:
            return _docker_cache

    docker_bin = resolve_docker_bin()
    build_cmd = f"docker build -f Dockerfile.sandbox -t {DOCKER_SANDBOX_IMAGE} ."

    if not docker_bin:
        setup_hint = "setup_sandbox.bat" if IS_WINDOWS else "./setup_sandbox_linux.sh"
        status_obj = {
            "installed": False,
            "daemon_running": False,
            "image_ready": False,
            "sandbox_mcp_ready": False,
            "ready_for_container_cli": False,
            "image": DOCKER_SANDBOX_IMAGE,
            "server_version": "",
            "build_cmd": build_cmd,
            "summary": (
                f"Docker не установлен · запустите {setup_hint} на сервере "
                "(включена безопасная файловая изоляция «Код»)"
            ),
        }
        _docker_cache = status_obj
        _docker_cache_ts = now_m
        _schedule_sandbox_cli_resync_if_changed(False)
        return status_obj

    daemon_ok = False
    server_ver = ""
    daemon_err = ""
    image_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_bin,
            "info",
            "--format",
            "{{.ServerVersion}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_hidden_kwargs(),
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        if proc.returncode == 0:
            daemon_ok = True
            server_ver = (out_b or b"").decode("utf-8", errors="replace").strip()
        else:
            daemon_err = (err_b or b"").decode("utf-8", errors="replace").strip().lower()
    except Exception as exc:
        daemon_ok = False
        daemon_err = str(exc).lower()

    if daemon_ok:
        try:
            proc_img = await asyncio.create_subprocess_exec(
                docker_bin,
                "image",
                "inspect",
                DOCKER_SANDBOX_IMAGE,
                "--format",
                "{{.Id}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **subprocess_hidden_kwargs(),
            )
            await asyncio.wait_for(proc_img.communicate(), timeout=3.0)
            image_ok = proc_img.returncode == 0
        except Exception:
            image_ok = False

    sandbox_ready = bool(daemon_ok and image_ok)
    if sandbox_ready:
        summary = (
            f"Docker {server_ver} активен · образ {DOCKER_SANDBOX_IMAGE} готов "
            "(MCP-песочница без утечки OAuth)"
        )
    elif daemon_ok and not image_ok:
        summary = f"Docker {server_ver} запущен · соберите образ ({build_cmd})"
    elif not IS_WINDOWS and ("permission denied" in daemon_err or "docker.sock" in daemon_err):
        summary = (
            "Нет доступа к /var/run/docker.sock · выполните на Linux: "
            "sudo usermod -aG docker $USER и перезапустите сервис Hub"
        )
    else:
        summary = "Docker установлен, но демон не запущен · включена безопасная файловая изоляция"

    status_obj = {
        "installed": True,
        "daemon_running": daemon_ok,
        "image_ready": image_ok,
        "sandbox_mcp_ready": sandbox_ready,
        "ready_for_container_cli": sandbox_ready,
        "image": DOCKER_SANDBOX_IMAGE,
        "server_version": server_ver,
        "build_cmd": build_cmd,
        "summary": summary,
    }
    _docker_cache = status_obj
    _docker_cache_ts = now_m
    _schedule_sandbox_cli_resync_if_changed(sandbox_ready)
    return status_obj


def _dedupe_patterns(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _collect_safe_cli_readonly_dirs(cli_home_dir: Optional[Path] = None) -> list[Path]:
    """Collect safe read-only MCP schema and skill directories for sandboxed `agy.exe` turns.

    Returns both un-followed `abspath` (matching NTFS junction paths in `<skills>` / `<mcp_servers>`)
    and `resolve()` target paths so `allowNonWorkspaceAccess=False` never blocks reading `SKILL.md` or
    lazy MCP tool schemas (`mcp/hub_sandbox/*.json`, `mcp/hub_openrouter/*.json`).
    Never includes CLI root folders containing OAuth tokens, `mcp_config.json`, or `conversations/*.db`.
    """
    candidates: list[Path] = []
    for base_agy_data in (AGY_CLI_DATA_DIR, config.AGY_CLI_DATA_DIR):
        if base_agy_data is not None:
            candidates.append(Path(base_agy_data) / "mcp")
            candidates.append(Path(base_agy_data) / "builtin" / "skills")

    if cli_home_dir is not None:
        ch = Path(cli_home_dir)
        candidates.extend(
            [
                ch / ".gemini" / "antigravity-cli" / "mcp",
                ch / ".gemini" / "antigravity-cli" / "builtin" / "skills",
                ch / ".gemini" / "config" / "skills",
                ch / ".agents" / "skills",
            ]
        )

    seen_norm: set[str] = set()
    out: list[Path] = []
    for cand in candidates:
        try:
            if not cand.exists() or not cand.is_dir():
                continue
        except OSError:
            continue
        variants: list[Path] = [Path(os.path.abspath(str(cand)))]
        try:
            variants.append(cand.resolve())
        except OSError:
            pass
        for v in variants:
            key = v.as_posix().rstrip("/").lower() if IS_WINDOWS else v.as_posix().rstrip("/")
            if "_agy_accounts" in v.parts or "oauth-token" in v.name:
                continue
            if key and key not in seen_norm:
                seen_norm.add(key)
                out.append(v)
    return out


def _posix_path_variants(p: Path) -> list[str]:
    """Return deduplicated POSIX representations (un-followed `abspath` + `resolve()`)."""
    variants: list[str] = []
    seen: set[str] = set()
    raw_candidates: list[Path] = [Path(os.path.abspath(str(p)))]
    try:
        raw_candidates.append(p.resolve())
    except OSError:
        pass
    for item in raw_candidates:
        px = item.as_posix().rstrip("/")
        key = px.lower() if IS_WINDOWS else px
        if px and key not in seen:
            seen.add(key)
            variants.append(px)
    return variants


def _build_permission_args(
    kind: str,
    access_mode: str,
    allowed_upload_dir: Optional[Path] = None,
    *,
    allowed_upload_dirs: Optional[list[Path]] = None,
    is_admin: bool = True,
    allow_hub_openrouter: bool = False,
    allow_hub_sandbox: bool = False,
    allow_hub_host: bool = True,
    allow_hub_files: Optional[bool] = None,
    cli_home_dir: Optional[Path] = None,
    project_dir: Optional[Path] = None,
    extra_dirs: Optional[list[Path]] = None,
    force_no_host_shell: Optional[bool] = None,
    resume_conv_id: str = "",
    sandbox_network: str = "internet",
) -> list[str]:
    """Build official agy CLI flags enforcing Read-Only, Write-Files, Sandboxed Access, and granular MCP isolation."""
    eff_no_host_shell = (
        (not is_admin) if force_no_host_shell is None else bool(force_no_host_shell)
    )
    eff_sandbox = bool(
        allow_hub_sandbox
        and kind == "project"
        and access_mode == "full_access"
        and eff_no_host_shell
    )
    eff_host_mcp = bool(
        allow_hub_host
        and kind == "project"
        and access_mode == "full_access"
        and not eff_no_host_shell
    )
    eff_files_mcp = bool(
        kind == "project"
        and (
            (access_mode in ("read_only", "write_files") or eff_no_host_shell)
            if allow_hub_files is None
            else allow_hub_files
        )
    )
    if not allow_hub_openrouter and not eff_sandbox and not eff_host_mcp and not eff_files_mcp:
        mcp_allow: list[str] = []
        mcp_deny: list[str] = _dedupe_patterns(
            [
                *MCP_DENY_PATTERNS,
                *HUB_OPENROUTER_DENY_PATTERNS,
                *HUB_SANDBOX_DENY_PATTERNS,
                *HUB_HOST_DENY_PATTERNS,
                *HUB_FILES_DENY_PATTERNS,
            ]
        )
    else:
        raw_allow: list[str] = []
        raw_deny: list[str] = list(MCP_NON_BRIDGE_DENY_PATTERNS)
        if allow_hub_openrouter:
            raw_allow.extend(HUB_OPENROUTER_ALLOW_PATTERNS)
        else:
            raw_deny.extend(HUB_OPENROUTER_DENY_PATTERNS)
        if eff_sandbox:
            raw_allow.extend(HUB_SANDBOX_ALLOW_PATTERNS)
        else:
            raw_deny.extend(HUB_SANDBOX_DENY_PATTERNS)
        if eff_host_mcp:
            raw_allow.extend(HUB_HOST_ALLOW_PATTERNS)
        else:
            raw_deny.extend(HUB_HOST_DENY_PATTERNS)
        if eff_files_mcp:
            raw_allow.extend(HUB_FILES_READ_ALLOW_PATTERNS)
            raw_deny.extend(HUB_FILES_WRITE_DENY_PATTERNS)
        else:
            raw_deny.extend(HUB_FILES_DENY_PATTERNS)
        mcp_allow = _dedupe_patterns(raw_allow)
        mcp_deny = _dedupe_patterns(raw_deny)

    net_disabled = str(sandbox_network or "internet").strip().lower() == "none"
    web_allow_globs: list[str] = (
        [] if net_disabled else ["read_url(*)", "search_web(*)", "search_web"]
    )
    web_deny_globs: list[str] = (
        ["read_url(*)", "search_web(*)", "search_web"] if net_disabled else []
    )

    cli_media_globs: list[str] = []
    cli_media_deny_globs: list[str] = []

    junc_brain_root = (
        f"{Path(os.path.abspath(str(cli_home_dir))).as_posix().rstrip('/')}/.gemini/antigravity-cli/brain"
        if cli_home_dir is not None
        else ""
    )
    resolved_home_brain_root = ""
    if cli_home_dir is not None:
        try:
            resolved_home_brain_root = (
                (Path(cli_home_dir) / ".gemini" / "antigravity-cli" / "brain")
                .resolve()
                .as_posix()
                .rstrip("/")
            )
        except OSError:
            resolved_home_brain_root = ""

    if is_admin:
        cli_brain_posix = (AGY_CLI_DATA_DIR / "brain").resolve().as_posix().rstrip("/")
        for b_root in dict.fromkeys([cli_brain_posix, junc_brain_root, resolved_home_brain_root]):
            if b_root:
                cli_media_globs.extend([f"read_file({b_root}/*)", f"read_file({b_root}/**)"])
    elif resume_conv_id:
        own_brain_posix = (
            (AGY_CLI_DATA_DIR / "brain" / resume_conv_id).resolve().as_posix().rstrip("/")
        )
        own_roots = [own_brain_posix]
        if junc_brain_root:
            own_roots.append(f"{junc_brain_root}/{resume_conv_id}")
        if resolved_home_brain_root:
            own_roots.append(f"{resolved_home_brain_root}/{resume_conv_id}")
        for own_b in dict.fromkeys(own_roots):
            if not own_b:
                continue
            cli_media_globs.extend([f"read_file({own_b}/*)", f"read_file({own_b}/**)"])
            cli_media_deny_globs.extend(
                [
                    f"read_file({own_b}/.system_generated/*)",
                    f"read_file({own_b}/.system_generated/**)",
                ]
            )

    safe_cli_ro_allow_globs: list[str] = []
    safe_cli_ro_deny_globs: list[str] = []
    for ro_dir in _collect_safe_cli_readonly_dirs(cli_home_dir):
        d_posix = ro_dir.as_posix().rstrip("/")
        safe_cli_ro_allow_globs.extend(
            [
                f"read_file({d_posix}/*)",
                f"read_file({d_posix}/**)",
            ]
        )
        safe_cli_ro_deny_globs.extend(
            [
                f"write_file({d_posix}/*)",
                f"write_file({d_posix}/**)",
                f"edit_file({d_posix}/*)",
                f"edit_file({d_posix}/**)",
            ]
        )

    upload_allow_globs: list[str] = []
    upload_deny_globs: list[str] = []
    combined_up_dirs: list[Path] = []
    for cand_ud in [allowed_upload_dir, *(allowed_upload_dirs or [])]:
        if cand_ud is not None and cand_ud not in combined_up_dirs:
            combined_up_dirs.append(cand_ud)
    for ud_item in combined_up_dirs:
        for u_posix in _posix_path_variants(ud_item):
            upload_allow_globs.extend(
                [
                    f"read_file({u_posix}/*)",
                    f"read_file({u_posix}/**)",
                ]
            )
            upload_deny_globs.extend(
                [
                    f"write_file({u_posix}/*)",
                    f"write_file({u_posix}/**)",
                    f"edit_file({u_posix}/*)",
                    f"edit_file({u_posix}/**)",
                ]
            )

    if kind == "chat":
        # Strict conversational isolation:
        # - Never allow terminal commands, file writes, or unauthorized host MCP tools.
        # - Block non-workspace PC files via allowNonWorkspaceAccess=False, while allowing
        #   this chat's uploads directory, safe MCP/skill schemas, CLI media, and personal `hub_openrouter` MCP.
        chat_allow_globs: list[str] = _dedupe_patterns(
            [
                *upload_allow_globs,
                *cli_media_globs,
                *safe_cli_ro_allow_globs,
                *web_allow_globs,
                *mcp_allow,
            ]
        )

        settings: dict[str, Any] = {
            "enableTerminalSandbox": True,
            "allowNonWorkspaceAccess": False,
            "permissions": {
                "allow": chat_allow_globs,
                "deny": _dedupe_patterns(
                    [
                        *cli_media_deny_globs,
                        *safe_cli_ro_deny_globs,
                        *upload_deny_globs,
                        *SECRET_PROFILE_DENY_PATTERNS,
                        *web_deny_globs,
                        *mcp_deny,
                        "write_file(*)",
                        "edit_file(*)",
                        *HOST_SHELL_DENY_PATTERNS,
                        *HEADLESS_ASYNC_DENY_PATTERNS,
                    ]
                ),
            },
        }
        return ["--disable-slash-commands", "--settings", json.dumps(settings, separators=(",", ":"))]

    admin_projects_read_globs: list[str] = []
    if is_admin and project_dir is not None:
        admin_read_roots: list[Path] = []
        if config.PROJECTS_ROOT.exists():
            admin_read_roots.append(config.PROJECTS_ROOT)
        if config.SHARED_PROJECTS_DIR.exists():
            admin_read_roots.append(config.SHARED_PROJECTS_DIR)
        try:
            if project_dir.resolve() == config.BASE_DIR.resolve() and config.BASE_DIR.exists():
                admin_read_roots.append(config.BASE_DIR)
        except OSError:
            pass
        for ar_root in admin_read_roots:
            for pr_posix in _posix_path_variants(ar_root):
                admin_projects_read_globs.extend(
                    [
                        f"read_file({pr_posix}/*)",
                        f"read_file({pr_posix}/**)",
                        f"list_dir({pr_posix})",
                        f"list_dir({pr_posix}/*)",
                        f"list_dir({pr_posix}/**)",
                        f"find_by_name({pr_posix})",
                        f"find_by_name({pr_posix}/*)",
                        f"find_by_name({pr_posix}/**)",
                        f"grep_search({pr_posix})",
                        f"grep_search({pr_posix}/*)",
                        f"grep_search({pr_posix}/**)",
                    ]
                )

    extra_read_globs: list[str] = []
    extra_write_globs: list[str] = []
    for ed in extra_dirs or []:
        if ed is None:
            continue
        for ed_posix in _posix_path_variants(ed):
            extra_read_globs.extend(
                [
                    f"read_file({ed_posix}/*)",
                    f"read_file({ed_posix}/**)",
                    f"list_dir({ed_posix})",
                    f"list_dir({ed_posix}/*)",
                    f"list_dir({ed_posix}/**)",
                    f"find_by_name({ed_posix})",
                    f"find_by_name({ed_posix}/*)",
                    f"find_by_name({ed_posix}/**)",
                    f"grep_search({ed_posix})",
                    f"grep_search({ed_posix}/*)",
                    f"grep_search({ed_posix}/**)",
                    f"view_file_outline({ed_posix}/*)",
                    f"view_file_outline({ed_posix}/**)",
                    f"view_code_item({ed_posix}/*)",
                    f"view_code_item({ed_posix}/**)",
                ]
            )
            extra_write_globs.extend(
                [
                    f"write_file({ed_posix}/*)",
                    f"write_file({ed_posix}/**)",
                    f"edit_file({ed_posix}/*)",
                    f"edit_file({ed_posix}/**)",
                ]
            )

    # Sandboxed contour when host shell is forbidden (or non-admin in read_only / write_files):
    # strictly lock file globs to `project_dir` and NEVER permit host shell execution (`command(*)`)!
    if eff_no_host_shell or (not is_admin and access_mode != "full_access"):
        read_globs: list[str] = [*cli_media_globs, *safe_cli_ro_allow_globs]
        write_globs: list[str] = []
        if project_dir is not None:
            for p_posix in _posix_path_variants(project_dir):
                read_globs.extend(
                    [
                        f"read_file({p_posix}/*)",
                        f"read_file({p_posix}/**)",
                        f"list_dir({p_posix})",
                        f"list_dir({p_posix}/*)",
                        f"list_dir({p_posix}/**)",
                        f"find_by_name({p_posix})",
                        f"find_by_name({p_posix}/*)",
                        f"find_by_name({p_posix}/**)",
                        f"grep_search({p_posix})",
                        f"grep_search({p_posix}/*)",
                        f"grep_search({p_posix}/**)",
                        f"view_file_outline({p_posix}/*)",
                        f"view_file_outline({p_posix}/**)",
                        f"view_code_item({p_posix}/*)",
                        f"view_code_item({p_posix}/**)",
                    ]
                )
                write_globs.extend(
                    [
                        f"write_file({p_posix}/*)",
                        f"write_file({p_posix}/**)",
                        f"edit_file({p_posix}/*)",
                        f"edit_file({p_posix}/**)",
                    ]
                )
            read_globs.extend(
                [
                    "list_dir",
                    "find_by_name",
                    "grep_search",
                    "view_file_outline",
                    "view_code_item",
                ]
            )
        if upload_allow_globs:
            read_globs.extend(upload_allow_globs)
        if admin_projects_read_globs:
            read_globs.extend(admin_projects_read_globs)

        if access_mode == "read_only":
            settings = {
                "enableTerminalSandbox": True,
                "allowNonWorkspaceAccess": False,
                "permissions": {
                    "allow": _dedupe_patterns([*read_globs, *web_allow_globs, *mcp_allow]),
                    "deny": _dedupe_patterns(
                        [
                            *cli_media_deny_globs,
                            *safe_cli_ro_deny_globs,
                            *upload_deny_globs,
                            *SECRET_PROFILE_DENY_PATTERNS,
                            *web_deny_globs,
                            *mcp_deny,
                            "write_file(*)",
                            "edit_file(*)",
                            *HOST_SHELL_DENY_PATTERNS,
                            *HEADLESS_ASYNC_DENY_PATTERNS,
                        ]
                    ),
                },
            }
            return ["--disable-slash-commands", "--settings", json.dumps(settings, separators=(",", ":"))]

        # write_files (or Docker / sandboxed full_access when host shell is forbidden)
        settings = {
            "enableTerminalSandbox": True,
            "allowNonWorkspaceAccess": False,
            "permissions": {
                "allow": _dedupe_patterns(
                    [*read_globs, *write_globs, *web_allow_globs, *mcp_allow]
                ),
                "deny": _dedupe_patterns(
                    [
                        *cli_media_deny_globs,
                        *safe_cli_ro_deny_globs,
                        *upload_deny_globs,
                        *SECRET_PROFILE_DENY_PATTERNS,
                        *web_deny_globs,
                        *mcp_deny,
                        *HOST_SHELL_DENY_PATTERNS,
                        *HEADLESS_ASYNC_DENY_PATTERNS,
                    ]
                ),
            },
        }
        return [
            "--disable-slash-commands",
            "--mode",
            "accept-edits",
            "--settings",
            json.dumps(settings, separators=(",", ":")),
        ]

    # Dual-Key Trusted Host contour (Admin OR trusted host_full user on a host project):
    # AGY auto mode enables the terminal sandbox even with enableTerminalSandbox=false.
    if access_mode == "full_access":
        perms_full: dict[str, Any] = {}
        full_allow = _dedupe_patterns(
            [*extra_read_globs, *extra_write_globs, *mcp_allow]
        )
        if full_allow:
            perms_full["allow"] = full_allow
        full_deny = _dedupe_patterns(
            [
                *cli_media_deny_globs,
                *SECRET_PROFILE_DENY_PATTERNS,
                *web_deny_globs,
                *mcp_deny,
                *HEADLESS_ASYNC_DENY_PATTERNS,
            ]
        )
        if full_deny:
            perms_full["deny"] = full_deny
        settings = {
            "enableTerminalSandbox": False,
            "toolPermission": "always-proceed",
            "allowNonWorkspaceAccess": True,
            "permissions": perms_full,
        }
        return [
            "--disable-slash-commands",
            "--mode",
            "accept-edits" if IS_WINDOWS else "auto",
            *(["--sandbox=false"] if IS_WINDOWS else []),
            "--dangerously-skip-permissions",
            "--settings",
            json.dumps(settings, separators=(",", ":")),
        ]
    elif access_mode == "write_files":
        settings = {
            "enableTerminalSandbox": True,
            "allowNonWorkspaceAccess": False,
            "permissions": {
                "allow": _dedupe_patterns(
                    [
                        "read_file(*)",
                        "list_dir(*)",
                        "find_by_name(*)",
                        "grep_search(*)",
                        *extra_read_globs,
                        "write_file(*)",
                        "edit_file(*)",
                        *extra_write_globs,
                        *web_allow_globs,
                        *mcp_allow,
                    ]
                ),
                "deny": _dedupe_patterns(
                    [
                        *cli_media_deny_globs,
                        *SECRET_PROFILE_DENY_PATTERNS,
                        *web_deny_globs,
                        *mcp_deny,
                        *HOST_SHELL_DENY_PATTERNS,
                        *HEADLESS_ASYNC_DENY_PATTERNS,
                    ]
                ),
            },
        }
        return [
            "--disable-slash-commands",
            "--mode",
            "accept-edits",
            "--settings",
            json.dumps(settings, separators=(",", ":")),
        ]
    else:
        # Default: strict Read-Only (safe from mobile!)
        settings = {
            "enableTerminalSandbox": True,
            "allowNonWorkspaceAccess": False,
            "permissions": {
                "allow": _dedupe_patterns(
                    [
                        "read_file(*)",
                        "list_dir(*)",
                        "find_by_name(*)",
                        "grep_search(*)",
                        *extra_read_globs,
                        *web_allow_globs,
                        *mcp_allow,
                    ]
                ),
                "deny": _dedupe_patterns(
                    [
                        *cli_media_deny_globs,
                        *SECRET_PROFILE_DENY_PATTERNS,
                        *web_deny_globs,
                        *mcp_deny,
                        "write_file(*)",
                        "edit_file(*)",
                        *HOST_SHELL_DENY_PATTERNS,
                        *HEADLESS_ASYNC_DENY_PATTERNS,
                    ]
                ),
            },
        }
        return ["--disable-slash-commands", "--settings", json.dumps(settings, separators=(",", ":"))]


def get_model_context_window(model_id: str) -> dict[str, Any]:
    """
    Return context window parameters and warning thresholds for a given model ID
    based on classify_model_family(model_id).
    """
    raw_low = str(model_id or "").strip().lower()
    if raw_low.startswith("claude-code:"):
        return {
            "family": "claude_code",
            "max_tokens": 200_000,
            "hard_max_tokens": 200_000,
            "warn_pct": 65.0,
            "high_pct": 78.0,
            "window_note": "Рабочее окно Claude Code (Подписка): 200k токенов",
        }
    if raw_low.startswith("codex:"):
        return {
            "family": "gpt",
            "max_tokens": 272_000,
            "hard_max_tokens": 272_000,
            "warn_pct": 75.0,
            "high_pct": 88.0,
            "window_note": "Рабочее окно ChatGPT (Codex): 272k токенов",
        }
    fam = classify_model_family(model_id)
    if fam == "gpt":
        return {
            "family": "gpt",
            "max_tokens": 128_000,
            "hard_max_tokens": 128_000,
            "warn_pct": 70.0,
            "high_pct": 85.0,
            "window_note": "Лимит контекста GPT: 128k токенов",
        }
    if fam == "claude":
        return {
            "family": "claude",
            "max_tokens": 200_000,
            "hard_max_tokens": 200_000,
            "warn_pct": 65.0,
            "high_pct": 78.0,
            "window_note": "Жёсткий лимит окна Claude: 200k токенов",
        }
    return {
        "family": fam if fam != "other" else "gemini",
        "max_tokens": 200_000,
        "hard_max_tokens": 1_000_000,
        "warn_pct": 78.0,
        "high_pct": 90.0,
        "window_note": "Оптимальное рабочее окно Antigravity: 200k токенов (макс. буфер Gemini — 1M, но свыше ~200k растёт расход квоты и падает точность)",
    }


def resolve_model_context_limits(model_id: str) -> tuple[int, int, str]:
    """
    Return (working_max_tokens, hard_max_tokens, human_note_ru) for a given model ID.
    We use 200k as the working gauge for Gemini & Claude (128k for GPT-OSS) because:
      - Claude Sonnet / Opus have a strict 200k context limit;
      - Antigravity auto-compacts Gemini trajectories around ~200k tokens to prevent
        context degradation ("context rot") and excessive 5h quota drain.
    """
    win = get_model_context_window(model_id)
    return (
        int(win["max_tokens"]),
        int(win["hard_max_tokens"]),
        str(win["window_note"]),
    )


def estimate_text_tokens(text: str) -> int:
    """Script-aware token estimator tuned for Russian Cyrillic + code/Markdown."""
    if not text:
        return 0
    cyr = sum(1 for ch in text if "\u0400" <= ch <= "\u04ff")
    other = len(text) - cyr
    return max(1, int(round(cyr / 2.5 + other / 3.9)))


def _count_turn_llm_calls(tool_calls: Any) -> int:
    """
    Infer exact number of internal LLM API passes inside a single CLI turn.
    Parallel tool calls share consecutive step_index values (e.g. 18, 19), while
    sequential tool rounds have step_index gaps > 1 (e.g. 19 -> 21 due to agent step 20).
    """
    if not isinstance(tool_calls, list) or not tool_calls:
        return 1
    steps = sorted(
        {
            int(tc.get("step_index"))
            for tc in tool_calls
            if isinstance(tc, dict) and tc.get("step_index") is not None
        }
    )
    if not steps:
        return 1 + len(tool_calls)
    batches = 1 + sum(1 for i in range(1, len(steps)) if steps[i] - steps[i - 1] > 1)
    return 1 + batches


def compute_chat_context_stats(
    chat: dict[str, Any],
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Compute accurate per-chat context window occupancy:
      1. Exact API telemetry mode (`source="telemetry"`): uses cumulative trajectory deltas
         from `agy.exe` (`input_tokens + cache_read_tokens` normalized by internal tool-call
         passes + visible output tokens) when an active `agy_conversation_id` has telemetry.
      2. Post-compaction / recovery estimation (`source="compacted"` or `"estimated"`):
         combines CLI system/tool prompt overhead with script-aware token count of
         `compacted_summary` + active post-compaction messages.
    """
    model_id = str(chat.get("model") or pick_default_model_id())
    win = get_model_context_window(model_id)
    max_tokens = int(win["max_tokens"])
    hard_max_tokens = int(win["hard_max_tokens"])
    window_note = str(win["window_note"])
    warn_pct = float(win["warn_pct"])
    high_pct = float(win["high_pct"])

    compacted_up_to = int(chat.get("compacted_up_to_msg_id") or 0)
    compacted_summary = str(chat.get("compacted_summary") or "").strip()
    compacted_count = int(chat.get("compacted_count") or 0)
    is_generating = bool(chat.get("is_generating"))

    visible_msgs = [
        m
        for m in messages
        if m.get("role") != "system" and m.get("status") != "compacted"
    ]
    active_msgs = [
        m for m in visible_msgs if int(m.get("id") or 0) > compacted_up_to
    ]

    if not visible_msgs and not compacted_summary:
        return {
            "used_tokens": 0,
            "max_tokens": max_tokens,
            "hard_max_tokens": hard_max_tokens,
            "pct": 0.0,
            "level": "ok",
            "source": "empty",
            "window_note": window_note,
            "can_compact": False,
            "compacted_count": compacted_count,
            "active_messages_count": 0,
            "total_messages_count": 0,
        }

    agy_conv_id = str(chat.get("agy_conversation_id") or "").strip()
    codex_thread_id = str(chat.get("codex_thread_id") or "").strip()
    claude_session_id = str(chat.get("claude_session_id") or "").strip()
    last_engine = str(chat.get("last_engine") or "").strip().lower()
    model_low = model_id.strip().lower()
    use_claude_telem = bool(claude_session_id and last_engine == "claude")
    use_codex_telem = bool(
        not use_claude_telem and codex_thread_id and last_engine == "codex"
    )
    claude_telem_msg: Optional[dict[str, Any]] = None
    if use_claude_telem:
        for m in active_msgs:
            if m.get("role") != "assistant" or m.get("status") != "done":
                continue
            u = m.get("usage")
            if (
                isinstance(u, dict)
                and u.get("provider") == "claude_code"
                and int(u.get("input_tokens") or u.get("context_input_tokens") or 0) > 0
            ):
                claude_telem_msg = m

    codex_telem_msg: Optional[dict[str, Any]] = None
    if use_codex_telem:
        for m in active_msgs:
            if m.get("role") != "assistant" or m.get("status") != "done":
                continue
            u = m.get("usage")
            if (
                isinstance(u, dict)
                and u.get("provider") == "chatgpt_codex"
                and (
                    int(u.get("last_run_input_tokens") or 0)
                    + int(u.get("context_input_tokens") or 0)
                    + int(u.get("input_tokens") or 0)
                    + int(u.get("cache_read_tokens") or 0)
                )
                > 0
            ):
                codex_telem_msg = m

    telem_msgs: list[dict[str, Any]] = []
    if (
        not claude_telem_msg
        and not codex_telem_msg
        and agy_conv_id
        and last_engine not in ("codex", "claude")
    ):
        for m in active_msgs:
            if m.get("role") != "assistant":
                continue
            u = m.get("usage")
            if not isinstance(u, dict):
                continue
            prov = str(u.get("provider") or "").strip()
            if prov in ("chatgpt_codex", "claude_code"):
                # Drop any legacy un-tagged agy messages collected prior to another provider's turn
                telem_msgs = [
                    tm
                    for tm in telem_msgs
                    if str((tm.get("usage") or {}).get("agy_conversation_id") or "").strip() == agy_conv_id
                ]
                continue
            if m.get("status") != "done":
                continue
            if (
                int(u.get("input_tokens") or 0)
                + int(u.get("cache_read_tokens") or 0)
                + int(u.get("last_run_input_tokens") or 0)
                + int(u.get("last_run_cache_tokens") or 0)
            ) <= 0:
                continue
            msg_conv = str(u.get("agy_conversation_id") or "").strip()
            if msg_conv:
                if msg_conv == agy_conv_id:
                    telem_msgs.append(m)
            else:
                telem_msgs.append(m)

    if claude_telem_msg is not None:
        u_claude = claude_telem_msg.get("usage") or {}
        dyn_win = int(u_claude.get("model_context_window") or 0)
        if dyn_win >= 32_000 and model_low.startswith("claude-code:"):
            max_tokens = dyn_win
            hard_max_tokens = dyn_win
        # In Claude Code telemetry, `input_tokens` already includes `cache_read` + `cache_creation`,
        # and `context_input_tokens` holds the last step's exact context size in multi-step tool turns.
        ctx_in = int(u_claude.get("context_input_tokens") or u_claude.get("input_tokens") or 0)
        c_out = int(u_claude.get("output_tokens") or 0)
        c_think = int(u_claude.get("thinking_tokens") or 0)
        visible_reply_tokens = max(
            estimate_text_tokens(str(claude_telem_msg.get("content") or "")),
            max(60, c_out - c_think),
        )
        used_tokens = ctx_in + visible_reply_tokens
        last_a_id = int(claude_telem_msg.get("id") or 0)
        for trailing in active_msgs:
            if int(trailing.get("id") or 0) > last_a_id:
                used_tokens += estimate_text_tokens(str(trailing.get("content") or "")) + 45
        source = "telemetry"
    elif codex_telem_msg is not None:
        u_codex = codex_telem_msg.get("usage") or {}
        dyn_win = int(u_codex.get("model_context_window") or 0)
        if dyn_win >= 32_000 and model_low.startswith("codex:"):
            max_tokens = dyn_win
            hard_max_tokens = dyn_win
        c_in = int(
            u_codex.get("last_run_input_tokens")
            or u_codex.get("context_input_tokens")
            or u_codex.get("input_tokens")
            or 0
        )
        c_out = int(
            u_codex.get("last_run_output_tokens")
            if "last_run_output_tokens" in u_codex
            else (u_codex.get("output_tokens") or 0)
        )
        c_think = int(
            u_codex.get("last_run_thinking_tokens")
            if "last_run_thinking_tokens" in u_codex
            else (u_codex.get("thinking_tokens") or 0)
        )
        visible_reply_tokens = max(
            estimate_text_tokens(str(codex_telem_msg.get("content") or "")),
            max(60, c_out - c_think),
        )
        # In Codex telemetry, cachedInputTokens is already included inside inputTokens
        used_tokens = c_in + visible_reply_tokens
        last_a_id = int(codex_telem_msg.get("id") or 0)
        for trailing in active_msgs:
            if int(trailing.get("id") or 0) > last_a_id:
                used_tokens += estimate_text_tokens(str(trailing.get("content") or "")) + 45
        source = "telemetry"
    elif telem_msgs:
        last_a = telem_msgs[-1]
        u_curr = last_a.get("usage") or {}
        c_in = int(u_curr.get("input_tokens") or 0)
        c_cache = int(u_curr.get("cache_read_tokens") or 0)
        c_out = int(u_curr.get("output_tokens") or 0)
        c_think = int(u_curr.get("thinking_tokens") or 0)
        proc_runs = max(1, int(u_curr.get("proc_runs") or 1))
        has_last_run = (
            "last_run_input_tokens" in u_curr or "last_run_cache_tokens" in u_curr
        )

        if has_last_run:
            lr_in = int(u_curr.get("last_run_input_tokens") or 0)
            lr_cache = int(u_curr.get("last_run_cache_tokens") or 0)
            lr_out = int(u_curr.get("last_run_output_tokens") or c_out)
            lr_think = int(u_curr.get("last_run_thinking_tokens") or c_think)
            u_prev = telem_msgs[-2].get("usage") or {} if len(telem_msgs) >= 2 else {}
            same_traj = (
                len(telem_msgs) >= 2
                and proc_runs == 1
                and u_prev.get("agy_conversation_id") == u_curr.get("agy_conversation_id")
            )
            if same_traj:
                p_in = int(u_prev.get("last_run_input_tokens") if "last_run_input_tokens" in u_prev else (u_prev.get("input_tokens") or 0))
                p_cache = int(u_prev.get("last_run_cache_tokens") if "last_run_cache_tokens" in u_prev else (u_prev.get("cache_read_tokens") or 0))
                p_out = int(u_prev.get("output_tokens") or 0)
                p_think = int(u_prev.get("thinking_tokens") or 0)
                if (
                    lr_in >= p_in
                    and lr_cache >= p_cache
                    and c_out >= p_out
                    and (lr_in + lr_cache) > (p_in + p_cache)
                ):
                    d_in = lr_in - p_in
                    d_cache = lr_cache - p_cache
                    d_out = max(0, c_out - p_out)
                    d_think = max(0, c_think - p_think)
                else:
                    d_in, d_cache, d_out, d_think = lr_in, lr_cache, lr_out, lr_think
            else:
                d_in, d_cache, d_out, d_think = lr_in, lr_cache, lr_out, lr_think
            eff_llm_calls = max(
                1,
                int(
                    u_curr.get("last_run_llm_calls")
                    or _count_turn_llm_calls(last_a.get("tool_calls"))
                ),
            )
            prompt_ctx = int(round((d_in + d_cache) / eff_llm_calls))
        else:
            u_prev = telem_msgs[-2].get("usage") or {} if len(telem_msgs) >= 2 else {}
            same_traj = (
                len(telem_msgs) >= 2
                and proc_runs == 1
                and u_prev.get("agy_conversation_id") == u_curr.get("agy_conversation_id")
            )
            if same_traj:
                p_in = int(u_prev.get("input_tokens") or 0)
                p_cache = int(u_prev.get("cache_read_tokens") or 0)
                p_out = int(u_prev.get("output_tokens") or 0)
                p_think = int(u_prev.get("thinking_tokens") or 0)
                if (
                    c_in >= p_in
                    and c_cache >= p_cache
                    and c_out >= p_out
                    and (c_in + c_cache) > (p_in + p_cache)
                ):
                    d_in = c_in - p_in
                    d_cache = c_cache - p_cache
                    d_out = c_out - p_out
                    d_think = max(0, c_think - p_think)
                else:
                    d_in, d_cache, d_out, d_think = c_in, c_cache, c_out, c_think
            else:
                d_in, d_cache, d_out, d_think = c_in, c_cache, c_out, c_think

            llm_calls = _count_turn_llm_calls(last_a.get("tool_calls"))
            prompt_ctx = int(round((d_in + d_cache) / max(1, llm_calls, proc_runs)))
            base_overhead = 16_500 if chat.get("kind") == "project" else 9_500
            summary_tokens = (
                estimate_text_tokens(compacted_summary) + 120 if compacted_summary else 0
            )
            est_msg_tokens = sum(
                estimate_text_tokens(str(m.get("content") or "")) + 38 for m in active_msgs
            )
            tool_cnt = len(last_a.get("tool_calls") or []) if isinstance(last_a.get("tool_calls"), list) else 0
            safety_cap = (base_overhead + summary_tokens + est_msg_tokens) * 2 + 24_000 + tool_cnt * 12_000
            if (proc_runs > 1 or compacted_count > 0) and prompt_ctx > safety_cap:
                prompt_ctx = safety_cap

        visible_reply_tokens = max(
            estimate_text_tokens(str(last_a.get("content") or "")),
            max(60, d_out - d_think),
        )
        used_tokens = prompt_ctx + visible_reply_tokens

        last_a_id = int(last_a.get("id") or 0)
        for trailing in active_msgs:
            if int(trailing.get("id") or 0) > last_a_id:
                used_tokens += estimate_text_tokens(str(trailing.get("content") or "")) + 45

        source = "telemetry"
    else:
        base_overhead = 16_500 if chat.get("kind") == "project" else 9_500
        summary_tokens = (
            estimate_text_tokens(compacted_summary) + 120 if compacted_summary else 0
        )
        msg_tokens = 0
        for m in active_msgs:
            msg_tokens += estimate_text_tokens(str(m.get("content") or "")) + 38
            tcs = m.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                msg_tokens += len(tcs) * 180
        used_tokens = base_overhead + summary_tokens + msg_tokens
        source = "compacted" if (compacted_summary and not active_msgs) else "estimated"

    pct = round((used_tokens / max(1, max_tokens)) * 100.0, 1)
    if pct >= high_pct:
        level = "high"
    elif pct >= warn_pct:
        level = "warn"
    else:
        level = "ok"

    can_compact = (
        len(active_msgs) >= 2
        or (len(active_msgs) >= 1 and (used_tokens >= 14_000 or is_generating))
    )

    return {
        "used_tokens": int(used_tokens),
        "max_tokens": int(max_tokens),
        "hard_max_tokens": int(hard_max_tokens),
        "pct": pct,
        "level": level,
        "source": source,
        "window_note": window_note,
        "can_compact": bool(can_compact),
        "compacted_count": compacted_count,
        "active_messages_count": len(active_msgs),
        "total_messages_count": len(visible_msgs),
    }


_NON_TERMINAL_TOOL_STATES: tuple[str, ...] = ("ACTIVE", "RUNNING", "PENDING")
_TERMINAL_TOOL_STATES: frozenset[str] = frozenset(
    {"DONE", "COMPLETED", "SUCCESS", "ERROR", "FAILED"}
)


def normalize_interrupted_tool_calls(
    tool_calls: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Normalize any unfinished tool steps (`ACTIVE`/`RUNNING`/`PENDING`) to `INTERRUPTED`."""
    if not tool_calls:
        return []
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(tool_calls):
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        st = str(entry.get("state") or "").strip().upper()
        if st in _NON_TERMINAL_TOOL_STATES:
            entry["state"] = "INTERRUPTED"
        if entry.get("step_index") is None:
            entry["step_index"] = idx
        out.append(entry)
    return out


def _summarize_tool_step_for_recovery(tc: dict[str, Any], fallback_idx: int) -> str:
    name = str(tc.get("name") or "tool").strip() or "tool"
    state = str(tc.get("state") or "DONE").strip() or "DONE"
    raw_idx = tc.get("step_index", fallback_idx)
    params = tc.get("parameters") if isinstance(tc.get("parameters"), dict) else {}
    target = ""
    for k in (
        "Path",
        "path",
        "TargetFile",
        "CommandLine",
        "command",
        "DirectoryPath",
        "SearchPath",
        "Query",
        "query",
        "Url",
        "url",
        "Name",
    ):
        val = params.get(k)
        if val:
            target = re.sub(r"\s+", " ", str(val)).strip()
            break
    if len(target) > 140:
        target = target[:137] + "..."
    target_part = f" ({target})" if target else ""
    return f"- [#{raw_idx}] {name}{target_part} -> {state}"


_TRANSIENT_PROJECT_PULSE_RE = re.compile(
    r"<PROJECT_PULSE\b[^>]*>[\s\S]*?</PROJECT_PULSE>",
    re.IGNORECASE,
)


def strip_transient_turn_wrappers(text: str) -> str:
    """Remove transient per-turn `<PROJECT_PULSE>` wrappers from historical messages
    and collapse leading blank lines so stale traffic-light warnings never pollute recovery/compaction."""
    if not text:
        return ""
    cleaned = _TRANSIENT_PROJECT_PULSE_RE.sub("", str(text))
    cleaned = re.sub(r"^(?:[ \t]*\r?\n)+", "", cleaned)
    return cleaned


def _clean_recovery_message_text(text: str) -> str:
    if not text:
        return ""
    from . import database
    stripped = strip_transient_turn_wrappers(str(text))
    return database._strip_injected_prompt_tags(stripped)


def _clip_recovery_text_head_tail(text: str, max_chars: int) -> str:
    """Keep ~60% head and ~40% tail on line boundaries so FINAL_DECISION/conclusions are never lost."""
    clean = str(text or "")
    if max_chars <= 0 or len(clean) <= max_chars:
        return clean
    marker = "\n...[пропущена середина сообщения]...\n"
    if max_chars <= len(marker) + 40:
        return clean[:max_chars]
    avail = max_chars - len(marker)
    head_budget = int(avail * 0.6)
    tail_budget = max(1, avail - head_budget)

    head_raw = clean[:head_budget]
    nl_h = head_raw.rfind("\n")
    head_part = (
        head_raw[:nl_h].rstrip("\r\n")
        if nl_h >= int(head_budget * 0.65)
        else head_raw
    )

    tail_raw = clean[-tail_budget:]
    nl_t = tail_raw.find("\n")
    tail_part = (
        tail_raw[nl_t + 1 :].lstrip("\r\n")
        if 0 <= nl_t <= int(tail_budget * 0.35)
        else tail_raw
    )
    return f"{head_part}{marker}{tail_part}"


def _format_recovery_attachments_manifest(attachments: Any) -> str:
    if not isinstance(attachments, list) or not attachments:
        return ""
    items: list[str] = []
    for att in attachments[:12]:
        if not isinstance(att, dict):
            continue
        dp = str(att.get("disk_path") or att.get("url") or "").strip()
        nm = str(att.get("name") or att.get("filename") or "").strip()
        if not nm and dp:
            nm = Path(dp).name
        if not nm and not dp:
            continue
        items.append(f"{nm} ({dp})" if dp else nm)
    if not items:
        return ""
    return f"[Вложения сообщения: {', '.join(items)}]"


def _format_recovery_assistant_tool_facts(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""
    touched_paths: list[str] = []
    seen_paths: set[str] = set()
    cmds: list[str] = []

    def _add_p(raw_p: Any) -> None:
        if not isinstance(raw_p, str):
            return
        s = raw_p.strip()
        if not s or len(touched_paths) >= 10:
            return
        label = Path(s.replace("\\", "/")).name or s
        if len(s) <= 64:
            label = s
        if label not in seen_paths:
            seen_paths.add(label)
            touched_paths.append(label)

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        tname = str(tc.get("name") or "").strip()
        params = tc.get("parameters") if isinstance(tc.get("parameters"), dict) else {}
        for pk in ("Path", "TargetFile", "file_path", "path", "notebook_path"):
            if params.get(pk):
                _add_p(params.get(pk))
        if isinstance(params.get("paths"), list):
            for p_item in params["paths"]:
                _add_p(p_item)
        if isinstance(params.get("renames"), list):
            for r_item in params["renames"]:
                if isinstance(r_item, dict):
                    _add_p(r_item.get("from"))
                    _add_p(r_item.get("to"))
        raw_cmd = str(
            params.get("CommandLine") or params.get("command") or params.get("cmd") or ""
        ).strip()
        if raw_cmd or tname in ("run_command", "Bash", "commandExecution"):
            short_c = re.sub(r"\s+", " ", raw_cmd)[:72]
            raw_st = str(tc.get("state") or tc.get("status") or "DONE").strip().upper()
            st = "ERROR" if ("ERR" in raw_st or raw_st == "FAILED") else ("INTERRUPTED" if "INTERRUPT" in raw_st else "DONE")
            if short_c:
                cmds.append(f"{short_c} -> {st}")

    fact_lines: list[str] = []
    if touched_paths:
        fact_lines.append("[Факты инструментов — файлы: " + ", ".join(touched_paths[:8]) + "]")
    if cmds:
        fact_lines.append("[Факты инструментов — команды: " + "; ".join(cmds[-3:]) + "]")
    if not fact_lines:
        return ""
    out = "\n".join(fact_lines[:3])
    if len(out) > 420:
        out = out[:416].rstrip() + "...]"
    return out


def _build_history_recovery_prompt(
    history_messages: list[dict[str, Any]],
    new_user_prompt: str,
    *,
    compacted_summary: str = "",
    compacted_up_to_msg_id: int = 0,
    resume_tool_calls: Optional[list[dict[str, Any]]] = None,
    partial_assistant_text: str = "",
    is_continue_turn: bool = False,
) -> str:
    """Build a compact context block from D:\\ hub.db after compaction, account/model switch, or In-Place Resume."""
    clean_summary = (compacted_summary or "").strip()
    clean_partial = (partial_assistant_text or "").rstrip()
    clean_tools = [tc for tc in (resume_tool_calls or []) if isinstance(tc, dict)]
    has_resume_progress = bool(clean_tools or clean_partial or is_continue_turn)

    filtered: list[dict[str, Any]] = []
    for m in history_messages[:-1]:
        if int(m.get("id") or 0) <= int(compacted_up_to_msg_id or 0):
            continue
        if m.get("role") == "system" or m.get("status") == "compacted":
            continue
        has_txt = bool(_clean_recovery_message_text(str(m.get("content") or "")).strip())
        has_att = bool(_format_recovery_attachments_manifest(m.get("attachments")))
        if has_txt or has_att:
            filtered.append(m)

    recent_limit = 6 if clean_summary else 12
    recent = filtered[-recent_limit:]
    if not clean_summary and not recent and not (clean_tools or clean_partial):
        return new_user_prompt

    lines: list[str] = []
    if clean_summary:
        lines.append(
            "<CONTEXT_SUMMARY>\n"
            "[Сжатая сводка предыдущего контекста диалога — все ключевые выводы, решения, пути и параметры сохранены:]\n\n"
            f"{clean_summary}\n"
            "</CONTEXT_SUMMARY>"
        )

    if recent:
        lines.append(
            "\n[Сообщения диалога после сводки (или из локальной базы Antigravity Hub):]"
        )
        last_user_idx = -1
        last_done_asst_idx = -1
        for idx_r, mr in enumerate(recent):
            if mr.get("role") == "user":
                last_user_idx = idx_r
            elif mr.get("role") == "assistant" and mr.get("status", "done") == "done":
                last_done_asst_idx = idx_r

        for i, m in enumerate(recent):
            is_last_user_before_resume = (
                has_resume_progress
                and i == len(recent) - 1
                and m.get("role") == "user"
            )
            is_priority_tail_msg = i in (last_user_idx, last_done_asst_idx)
            if is_last_user_before_resume:
                max_msg_chars = 8000 if clean_summary else 12000
            elif is_priority_tail_msg:
                max_msg_chars = 5000 if clean_summary else 7500
            else:
                max_msg_chars = 1800 if clean_summary else 2400

            role_label = "Пользователь" if m.get("role") == "user" else "Ассистент"
            txt = _clean_recovery_message_text(str(m.get("content") or "")).strip()
            if len(txt) > max_msg_chars:
                txt = _clip_recovery_text_head_tail(txt, max_msg_chars)
            att_manifest = _format_recovery_attachments_manifest(m.get("attachments"))
            tool_facts = (
                _format_recovery_assistant_tool_facts(m.get("tool_calls"))
                if m.get("role") == "assistant"
                else ""
            )
            msg_body_parts = [p for p in (txt, att_manifest, tool_facts) if p]
            lines.append(f"\n### {role_label}:\n" + "\n".join(msg_body_parts))

    if clean_tools:
        tail_tools = clean_tools[-25:]
        header_note = (
            f" (последние {len(tail_tools)} из {len(clean_tools)} шагов)"
            if len(clean_tools) > len(tail_tools)
            else f" ({len(tail_tools)} шаг.)"
        )
        lines.append(
            f"\n[Инструменты, уже выполненные в текущем прерванном ходе{header_note} — не дублируй уже сохранённые правки, при необходимости проверь состояние файлов на диске:]"
        )
        for idx, tc in enumerate(tail_tools):
            lines.append(_summarize_tool_step_for_recovery(tc, idx))

    if clean_partial:
        partial_snippet = (
            ("...[начало ответа уже сохранено]...\n" + clean_partial[-2000:])
            if len(clean_partial) > 2000
            else clean_partial
        )
        lines.append(
            "\n[Уже выведенная и видимая пользователю часть ответа в этой карточке (НЕ повторяй её):]\n"
            f"«{partial_snippet}»"
        )

    if has_resume_progress:
        lines.append(
            "\n---\n[Инструкция продолжения прерванного хода — продолжи выполнение задачи ровно с места остановки:]\n"
        )
    else:
        lines.append(
            "\n---\n[Новое сообщение пользователя — ответь на него естественно и по существу, опираясь на контекст выше:]\n"
        )
    lines.append(new_user_prompt)
    return "\n".join(lines)


LIVE_VOICE_MODE_INSTRUCTION = (
    "[РЕЖИМ ЖИВОЙ ГОЛОСОВОЙ БЕСЕДЫ (LIVE VOICE): Пользователь общается с тобой голосом в реальном времени (hands-free). "
    "Отвечай естественно, живо, лаконично и разговорно (обычно 1–4 коротких предложения по сути), без списков из 15 пунктов, "
    "без Markdown-таблиц и без озвучивания длинных путей к файлам. Если просят написать код или выполнить правку — "
    "коротко скажи словами суть решения, а сам код/правку оформи стандартно.]\n"
)


PLAN_TASK_PROFILE_INSTRUCTION = (
    "[Режим планирования («План»): если в ответе ты формируешь готовое ТЗ, спецификацию или план реализации "
    "для передачи в разработку («🚀 В Код»), учитывай, что рабочие вкладки Hub работают в однопроцессном режиме "
    "без внутренних субагентов `invoke_subagent` и `swarm` (предписывай последовательное выполнение всех шагов "
    "в одном процессе), а в самом конце ответа на отдельной строке (без обрамления в code-fence) "
    "допиши скрытый однострочный JSON-тег:\n"
    '<TASK_PROFILE>{"complexity":"trivial|light|medium|heavy|critical","type":"ui|backend|fullstack|refactor|debug|tests|docs","files":3,"risk":"low|medium|high","needs_terminal":true}</TASK_PROFILE>\n'
    "Для обычных ответов, обсуждений и уточняющих вопросов без готового ТЗ этот тег не добавляй.]\n"
)

SINGLE_TAB_READ_ONLY_INSTRUCTION = (
    "По известному пути сразу вызывай нативный read_file с диапазоном строк; не читай JSON-схемы hub_files повторно. "
    "Поиск hub_files группируй в компактные серии; избегай 15–20 мелких вызовов. "
    "[Контур выполнения Antigravity Hub (📋 Режим «План» — строго только чтение): "
    "эта вкладка работает в однопроцессном аналитическом режиме только для чтения. "
    "Выполнение терминальных команд, создание или модификация файлов на диске, а также фоновые субагенты "
    "(`invoke_subagent`, `define_subagent`), рой (`swarm`), таймеры (`schedule`) и модальные опросы (`ask_question`) "
    "запрещены политикой безопасности — НЕ пытайся их вызывать и НЕ завершай ход в ожидании уведомлений. "
    "Для быстрого поиска по коду и обзора структуры папок используй поисковый MCP `hub_files` "
    "(`call_mcp_tool` с `ServerName='hub_files'` для `grep_search`, `find_by_name`, `list_dir`), "
    "а штатный `read_file` вызывай точечно по нужным диапазонам строк (`StartLine`/`EndLine`) вместо сплошного чтения файлов по 800 строк.]\n"
)

SINGLE_TAB_EXECUTION_INSTRUCTION = (
    "[Контур выполнения Antigravity Hub: эта вкладка работает в однопроцессном headless-режиме (`agy -p`), "
    "поэтому фоновые субагенты (`invoke_subagent`, `define_subagent`), рой (`swarm`), таймеры (`schedule`) "
    "и модальные опросы (`ask_question`) отключены политикой безопасности. "
    "НЕ пытайся их вызывать и НЕ завершай ход в ожидании фоновых уведомлений.]\n"
)

SINGLE_TAB_HOST_FULL_EXECUTION_INSTRUCTION = (
    "[Контур выполнения Antigravity Hub (💻 Прямой терминал ПК — Trusted Host Mode): "
    "эта вкладка работает в однопроцессном headless-режиме (`agy -p`) без фоновых субагентов (`invoke_subagent`, "
    "`define_subagent`), роя (`swarm`), таймеров (`schedule`) и `ask_question` — НЕ вызывай их. "
    "Инструмент `run_command` выполняется НАПРЯМУЮ на Windows-хосте с реальным выходом в сеть, доступом к локальным "
    "портам `127.0.0.1` и установленным интерпретаторам (`python`, `node`, `npm`, `git`) без ограничения песочницей терминала "
    "(при вызове `run_command` передавай `WaitMsBeforeAsync=10000`). "
    "ВАЖНО для фоновых сервисов: долгоживущие процессы (Telegram/Discord-боты, `uvicorn`/FastAPI, Flask, Vite, `http.server`, воркеры) "
    "НЕ запускай напрямую через `run_command` (иначе они заблокируют ход или будут убиты по окончании хода). "
    "Управляй ими через MCP-сервер `hub_host` → `host_service_ctl` (`call_mcp_tool` с `ServerName='hub_host'`, `ToolName='host_service_ctl'`, "
    "`action='start'|'restart'|'status'|'logs'|'stop'`): он запускает процесс в отвязанном режиме с защитой от дубликатов (`409 Conflict`) и логами.]\n"
)

SINGLE_TAB_DOCKER_EXECUTION_INSTRUCTION = (
    "[Контур выполнения Antigravity Hub (🐳 Docker-песочница): эта вкладка работает в однопроцессном "
    "headless-режиме (`agy -p`) без фоновых субагентов (`invoke_subagent`, `define_subagent`), роя (`swarm`), "
    "таймеров (`schedule`) и модальных опросы (`ask_question`) — НЕ вызывай их и НЕ завершай ход в ожидании уведомлений. "
    "Прямой хостовый терминал ПК отключён политикой безопасности. "
    "Чтение, поиск и редактирование файлов в папке проекта открыты напрямую (`read_file`, `edit_file`, "
    "`write_file`, а также MCP `hub_files`: `list_dir`, `find_by_name`, `grep_search`). "
    "Все команды сборки, тестов, git, npm, python и фоновых веб-серверов выполняй через MCP-сервер `hub_sandbox` "
    "(`call_mcp_tool` с `ServerName='hub_sandbox'` для `sandbox_exec`, `sandbox_service_ctl`, `sandbox_reset`; "
    "папка проекта смонтирована в `/workspace`).]\n"
)

SINGLE_TAB_SANDBOX_FILES_INSTRUCTION = (
    "[Контур выполнения Antigravity Hub (✏️ Файловый режим «Код» без терминала): "
    "эта вкладка работает в однопроцессном headless-режиме без фоновых субагентов (`invoke_subagent`, `define_subagent`), "
    "роя (`swarm`), таймеров (`schedule`) и `ask_question` — НЕ вызывай их и НЕ завершай ход в ожидании уведомлений. "
    "Терминал в текущем режиме отключён политикой безопасности. "
    "Выполняй все шаги самостоятельно и последовательно через файловые инструменты "
    "(`read_file`, `edit_file`, `write_file`) и поисковый MCP `hub_files` "
    "(`call_mcp_tool` с `ServerName='hub_files'` для `list_dir`, `find_by_name`, `grep_search`).]\n"
)


def _build_docker_unavailable_instruction(reason: str = "") -> str:
    clean_reason = (
        reason or "Docker Desktop не запущен или образ песочницы `agy-hub-sandbox:latest` ещё не собран"
    ).strip()
    return (
        "[Контур выполнения Antigravity Hub (⚠️ Режим правки файлов — Docker-терминал недоступен): "
        "эта вкладка работает в однопроцессном headless-режиме без фоновых субагентов (`invoke_subagent`, `define_subagent`), "
        "роя (`swarm`), таймеров (`schedule`) и `ask_question` — НЕ вызывай их. "
        f"Прямой хостовый терминал ПК (`run_command` / Bash) запрещён политикой безопасности, а Docker-песочница сейчас недоступна ({clean_reason}). "
        "НИКОГДА не вызывай `run_command`. "
        "Чтение, поиск, создание и редактирование файлов в папке проекта полностью открыты напрямую "
        "(`read_file`, `edit_file`, `write_file`, `list_dir`, `find_by_name`, `grep_search`). "
        "Выполни все файловые изменения и анализ кода напрямую, а если для задачи требуется запуск команд "
        f"в терминале — явно и понятно сообщи пользователю точную причину ({clean_reason}: запустить Docker Desktop, "
        "собрать образ `agy-hub-sandbox:latest` через `setup_sandbox.bat` или включить право «🐳 Docker-терминал»).]\n"
    )


def resolve_turn_execution_contour(
    *,
    kind: str,
    requested_access_mode: str,
    eff_exec_mode: str,
    force_no_host_shell: bool,
    allow_hub_sandbox: bool,
    user: Optional[dict[str, Any]] = None,
    docker_info: Optional[dict[str, Any]] = None,
) -> tuple[str, str]:
    """Return `(execution_contour, docker_unavailable_reason)` for prompt & developer instructions."""
    is_admin = bool(user is None or str((user or {}).get("role") or "") == "admin")
    max_mode = (
        "host_full"
        if is_admin
        else str((user or {}).get("max_access_mode") or "write_files").strip().lower()
    )

    if kind == "project" and eff_exec_mode == "docker":
        if allow_hub_sandbox:
            return "docker", ""
        if requested_access_mode == "full_access":
            if not is_admin and max_mode not in ("docker_full", "host_full"):
                return (
                    "docker_unavailable",
                    "у вашей учётной записи не включено право «🐳 Docker-терминал» в настройках пользователя",
                )
            if not docker_info or not docker_info.get("installed"):
                return "docker_unavailable", "Docker не установлен на сервере"
            if not docker_info.get("daemon_running"):
                return "docker_unavailable", "Docker Desktop (демон Docker) не запущен на сервере"
            if not docker_info.get("image_ready"):
                return (
                    "docker_unavailable",
                    f"не собран образ песочницы `{DOCKER_SANDBOX_IMAGE}` (`{docker_info.get('build_cmd') or 'setup_sandbox.bat'}`)",
                )
            return (
                "docker_unavailable",
                str(docker_info.get("summary") or "Docker-песочница сейчас недоступна"),
            )
        return "sandbox_files", ""

    if force_no_host_shell:
        if kind == "project" and requested_access_mode == "full_access":
            if not is_admin and max_mode not in ("docker_full", "host_full"):
                return (
                    "docker_unavailable",
                    "у вашей учётной записи установлен потолок прав «Только код без терминала» — попросите администратора включить «🐳 Docker-терминал»",
                )
            return "docker_unavailable", "терминал отключён в настройках этого проекта"
        return "sandbox_files", ""

    return "host", ""


def _augment_prompt_for_mode(
    *,
    kind: str,
    prompt: str,
    attachments: list[dict[str, Any]],
    is_first_turn: bool,
    access_mode: str = "read_only",
    inline_hub_rules: str = "",
    project_pulse_block: str = "",
    referenced_chat_blocks: Optional[list[str]] = None,
    live_voice: bool = False,
    execution_contour: str = "host",
    docker_unavailable_reason: str = "",
    extra_dirs: Optional[list[Path]] = None,
    project_dir: Optional[Path] = None,
    disable_default_guardrails: bool = False,
    project_memory: Optional[dict[str, Any]] = None,
) -> str:
    parts: list[str] = []
    if kind == "chat" and is_first_turn:
        parts.append(
            "[Режим: обычный разговорный чат без привязки к кодовой базе. "
            "Отвечай естественно, структурированно и по делу, как в веб-чате Gemini.]\n"
        )
    elif kind == "project" and access_mode == "read_only" and is_first_turn and not live_voice:
        parts.append(PLAN_TASK_PROFILE_INSTRUCTION)
    if not live_voice and (
        is_first_turn
        or (kind == "project" and access_mode in ("write_files", "full_access"))
    ):
        if kind == "project" and access_mode == "read_only":
            parts.append(SINGLE_TAB_READ_ONLY_INSTRUCTION)
        elif execution_contour == "docker":
            parts.append(SINGLE_TAB_DOCKER_EXECUTION_INSTRUCTION)
        elif execution_contour == "docker_unavailable":
            parts.append(_build_docker_unavailable_instruction(docker_unavailable_reason))
        elif kind == "project" and access_mode == "write_files":
            parts.append(SINGLE_TAB_SANDBOX_FILES_INSTRUCTION)
        elif execution_contour in ("sandbox_files", "sandbox_readonly"):
            parts.append(SINGLE_TAB_SANDBOX_FILES_INSTRUCTION)
        elif kind == "project" and access_mode == "full_access" and execution_contour == "host":
            parts.append(SINGLE_TAB_HOST_FULL_EXECUTION_INSTRUCTION)
        else:
            parts.append(SINGLE_TAB_EXECUTION_INSTRUCTION)
    if not live_voice and kind == "project" and access_mode in ("write_files", "full_access"):
        default_guardrails = config.get_default_project_guardrails_block(
            project_dir,
            disable_default_guardrails=disable_default_guardrails,
            project_memory=project_memory,
        )
        if default_guardrails:
            parts.append(default_guardrails + "\n")
    if kind == "project" and extra_dirs and (is_first_turn or access_mode in ("write_files", "full_access")):
        ed_list = ", ".join(f"`{d}`" for d in extra_dirs)
        parts.append(
            f"[Дополнительные рабочие папки проекта, подключённые через `--add-dir`: {ed_list}]\n"
        )
    if inline_hub_rules.strip() and is_first_turn:
        parts.append(
            f"[Личные глобальные правила пользователя Antigravity Hub:]\n{inline_hub_rules.strip()}\n"
        )
    if project_pulse_block.strip():
        parts.append(project_pulse_block.strip() + "\n")
    if referenced_chat_blocks:
        clean_refs = [b.strip() for b in referenced_chat_blocks if b and b.strip()]
        if clean_refs:
            ref_intro = (
                "[Прикреплённый контекст соседних вкладок этого проекта (@чат) — опирайся на эти выводы и закладки:]\n"
                if kind == "project"
                else "[Прикреплённый контекст связанных диалогов (@чат) — опирайся на эти выводы и данные:]\n"
            )
            parts.append(ref_intro + "\n\n".join(clean_refs) + "\n")
    if attachments:
        att_lines = ["Прикреплённые пользователем файлы (прочитай/просмотри их через read_file):"]
        for att in attachments:
            if att.get("disk_path"):
                att_lines.append(f"- {att['disk_path']} ({att.get('name', 'file')})")
        parts.append("\n".join(att_lines) + "\n")
    if live_voice:
        parts.append(LIVE_VOICE_MODE_INSTRUCTION)
    parts.append(prompt)
    return "\n".join(parts)


def is_transient_capacity_error(err_text: str) -> bool:
    """Return True if the CLI error is a transient upstream 503/529 capacity overload (not 401/403/429 quota)."""
    low = (err_text or "").lower()
    if not low:
        return False
    if any(q in low for q in ("quota", "429", "rate limit", "too many requests", "billing")):
        return False
    return any(
        kw in low
        for kw in (
            "503 capacity",
            "google capacity / 503",
            "no capacity available",
            "experiencing high traffic",
            "unavailable (code 503)",
            "code 503",
            "503 service unavailable",
            "status 503",
            "overloaded_error",
            "temporarily overloaded",
            "server is overloaded",
        )
    )


STREAM_STARTUP_IDLE_TIMEOUT_SEC: float = 65.0
STREAM_RESUME_STARTUP_IDLE_TIMEOUT_SEC: float = 45.0
STREAM_ACTIVE_TOOL_TIMEOUT_SEC: float = 600.0
STREAM_POST_TEXT_IDLE_TIMEOUT_SEC: float = 65.0
STREAM_NETWORK_IDLE_TIMEOUT_SEC: float = 165.0
STREAM_DISK_POLL_SEC = 2.5
STREAM_GEMINI_HIGH_FIRST_EVENT_SEC = 360.0
STREAM_GEMINI_FIRST_EVENT_SEC = 180.0


def _gemini_first_event_timeout(model: str) -> float:
    if model.lower().endswith(("-high", "-xhigh", "-max")):
        return STREAM_GEMINI_HIGH_FIRST_EVENT_SEC
    return STREAM_GEMINI_FIRST_EVENT_SEC


def _gemini_attempt_log(log_path: Path) -> tuple[bool, bool, str]:
    """Read only the bounded tail of the log assigned to this subprocess."""
    try:
        with log_path.open("rb") as stream:
            stream.seek(max(0, log_path.stat().st_size - 65536))
            tail = stream.read(65536).decode("utf-8", errors="replace")
    except OSError:
        return False, False, ""
    authenticated = "OAuth: authenticated successfully" in tail
    submitted = any(
        "SEND_USER_CASCADE_MESSAGE_LATENCY" in line and "status:OK" in line
        for line in tail.splitlines()
    )
    for line in reversed(tail.splitlines()):
        body = line.split("] ", 1)[-1].lower()
        if "clearcut" in body or not re.search(
            r"generation failed|failed to generate|streamgeneratecontent.*(?:giving up|retries exhausted)",
            body,
        ):
            continue
        if re.search(r"unauthenticated|(?:http|code|status)[: =]+401", body):
            return authenticated, submitted, "Google generation failed: UNAUTHENTICATED (401)"
        if re.search(r"resource_exhausted|(?:http|code|status)[: =]+429", body):
            return authenticated, submitted, "Google generation failed: RESOURCE_EXHAUSTED (429)"
        if re.search(r"deadline_exceeded|deadline exceeded", body):
            return authenticated, submitted, "Google generation failed: DEADLINE_EXCEEDED"
        if re.search(r"\b503\b", body):
            return authenticated, submitted, "Google generation failed: code 503"
    return authenticated, submitted, ""


def _gemini_disk_progress(root: Path, cid: str) -> tuple[tuple, tuple]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cid):
        return (), ()
    logs = root / "brain" / cid / ".system_generated" / "logs"

    def stamps(paths):
        result = []
        for path in paths:
            try:
                st = path.stat()
                result.append((path.name, st.st_size, st.st_mtime_ns))
            except OSError:
                continue
        return tuple(result)

    return (
        stamps(logs / name for name in ("transcript.jsonl", "transcript_full.jsonl")),
        stamps(root / "conversations" / f"{cid}{suffix}" for suffix in (".db", ".db-wal", ".db-journal", ".pb")),
    )


def _latest_cli_failure(home: Path, since: float, attempt_log: Optional[Path] = None) -> str:
    """Bounded, redacted diagnostics from this launch only; never expose raw log bodies."""
    root = Path(home) / ".gemini" / "antigravity-cli"
    try:
        paths = [attempt_log] if attempt_log is not None else [p for folder in ("log", "logs") for p in (root / folder).glob("cli-*.log")
                 if p.stat().st_mtime >= since]
        if not paths:
            return ""
        path = max(paths, key=lambda p: p.stat().st_mtime)
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 32768))
            lines = stream.read(32768).decode("utf-8", errors="replace").splitlines()
        # Whitelist status descriptions, rather than copying arbitrary prompt/token-bearing lines.
        statuses = []
        for line in lines:
            if "clearcut" in line.lower():
                continue  # Telemetry failure is not a model capacity error.
            line = line.split("] ", 1)[-1]
            for pattern, label in (
                (r"\b503\b|capacity|RESOURCE_EXHAUSTED", "Google capacity / 503"),
                (r"\b429\b|quota exhausted", "Google quota / 429"),
                (r"DEADLINE_EXCEEDED|TLS handshake timeout|connection reset|i/o timeout", "Google transport timeout"),
                (r"UNAUTHENTICATED|\b401\b", "Google authentication / 401"),
                (r"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "AICode scope insufficient (CCPA fallback)"),
            ):
                if re.search(pattern, line, re.I):
                    statuses.append(label)
        return "; ".join(dict.fromkeys(statuses))
    except OSError:
        return ""


MAX_CONSECUTIVE_AUTO_CONTINUES: int = 3
MAX_TOTAL_AUTO_CONTINUES: int = 10
MAX_PARKED_TURN_RESUMES: int = 2
MAX_EMPTY_TURN_RESUMES: int = 2
CAPACITY_RETRY_BACKOFF_SECS: tuple[float, float] = (2.0, 4.0)
AUTO_CONTINUE_PAUSE_SEC: float = 1.5


def _spill_prompt_to_upload_file(prompt_text: str, upload_dir: Path) -> tuple[Path, str]:
    """Write a large/quote-heavy prompt to `_large_prompt_input_<uid>.md` and return `(file_path, stub_prompt)`.
    Emits explicit pagination instructions whenever UTF-8 bytes > 45_000 or lines > 780."""
    upload_dir.mkdir(parents=True, exist_ok=True)
    large_prompt_file = upload_dir / f"_large_prompt_input_{uuid.uuid4().hex[:8]}.md"
    raw_bytes = prompt_text.encode("utf-8")
    large_prompt_file.write_bytes(raw_bytes)
    total_bytes = len(raw_bytes)
    total_lines = prompt_text.count("\n") + 1
    if total_bytes > 45_000 or total_lines > 780:
        stub = (
            f"Полный текст запроса сохранён в файле {large_prompt_file} ({total_lines} строк, {total_bytes} байт UTF-8). "
            "Внимание: объём файла превышает разовый лимит read_file (800 строк / 46080 байт) — обязательно прочитай файл целиком "
            "последовательными вызовами read_file через StartLine/EndLine или ByteOffset до самого конца перед выполнением задачи."
        )
    else:
        stub = (
            f"Полный текст запроса пользователя сохранён в файле {large_prompt_file} ({total_lines} строк, {total_bytes} байт UTF-8 — "
            "полностью помещается в один вызов read_file). Сначала полностью прочитай этот файл через read_file и подробно ответь на запрос."
        )
    return large_prompt_file, stub


def is_transient_stream_error(err_text: str) -> bool:
    """Return True if the CLI error is a transient HTTP/2 stream, proxy, or idle timeout disconnect (not 401/403/429)."""
    low = (err_text or "").lower().replace("deadline_exceeded", "deadline exceeded")
    if not low:
        return False
    if any(
        q in low
        for q in (
            "401",
            "403",
            "429",
            "unauthorized",
            "unauthenticated",
            "quota",
            "rate limit",
            "too many requests",
            "billing",
            "permission denied",
        )
    ):
        return False
    return any(
        kw in low
        for kw in (
            "stream was interrupted",
            "please continue the task",
            "stream idle timeout",
            "stream idle watchdog timeout",
            "context deadline exceeded",
            "deadline exceeded",
            "i/o timeout",
            "tls handshake timeout",
            "unexpected eof",
            "request failed: eof",
            ": eof",
            "connection reset",
            "forcibly closed",
            "wsarecv",
            "broken pipe",
            "client connection lost",
            "http2:",
            "stream error",
            "transport is closing",
            "there was a network issue",
            "network issue connecting",
            "network error",
            "connection error",
            "failed to connect to the server",
            "temporary network",
        )
    )


def is_gemini_location_unsupported_error(err_text: str) -> bool:
    """Return True if upstream Google/Gemini API rejected the current outbound IP region (HTTP 400 FAILED_PRECONDITION / 403 location not eligible)."""
    low = (err_text or "").lower()
    if not low:
        return False
    return (
        "user location is not supported" in low
        or "location is not supported for the api use" in low
        or "your location is not eligible for this service" in low
        or ("failed_precondition" in low and "location" in low)
    )


def _trailing_active_lines(text: str, limit: int) -> list[str]:
    """
    Extract up to `limit` trailing non-empty lines from the final active paragraph block.
    Avoids false-positive matches on superseded interim notices located before a `\\n\\n`
    wakeup paragraph break when the model has already appended a final prose conclusion.
    """
    raw = (text or "").rstrip()
    if not raw:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    if not paragraphs:
        return []
    last_lines = [ln.strip() for ln in paragraphs[-1].splitlines() if ln.strip()]
    if len(paragraphs) >= 2 and last_lines:
        prev_para = paragraphs[-2].rstrip()
        is_bullet_continuation = prev_para.endswith(":") or all(
            bool(re.match(r"^(?:[-*•]|\d+[.)])\s+", ln)) for ln in last_lines
        )
        if is_bullet_continuation:
            prev_lines = [ln.strip() for ln in prev_para.splitlines() if ln.strip()]
            return (prev_lines + last_lines)[-limit:]
    return last_lines[-limit:]


def looks_like_completed_response(text: str) -> bool:
    """Return True if accumulated assistant text looks like a complete final report with closed code fences."""
    t = (text or "").rstrip()
    if len(t) < 180:
        return False
    if t.count("```") % 2 != 0:
        return False
    tail_lines = _trailing_active_lines(t, 3)
    if tail_lines and any(_is_interim_wait_status_line(ln) for ln in tail_lines):
        return False
    if t.upper().endswith("</TASK_PROFILE>"):
        return True
    if t.endswith((":", ",", "—", "–", "-", "…", "...")):
        return False
    core = t.rstrip("*_").rstrip()
    if core.endswith((":", ",", "—", "–", "-", "…", "...")):
        return False
    return t.endswith((".", "!", "?", ")", "]", "`", '"', "»", "*"))


def _update_bg_task_state_on_tool(
    active_bg_tasks: set[str],
    entry: dict[str, Any],
) -> bool:
    """
    Update `active_bg_tasks` in place for a `step_type == 'tool'` update and return
    the resulting `waiting_for_bg_task` boolean (`bool(active_bg_tasks)`).
    """
    tname_low = str(entry.get("name") or "").strip().lower()
    state_upper = str(entry.get("state") or "").strip().upper()
    out_low = str(entry.get("output") or "").lower()
    eff_step_idx = entry.get("step_index", 0)

    is_denied_or_failed = (
        state_upper in ("ERROR", "FAILED")
        or "permission denied" in out_low
        or "matches user-configured deny rule" in out_low
    )
    has_bg_output_marker = (
        "tool is running as a background task" in out_low
        or "running in the background" in out_low
        or "task id:" in out_low
    )
    is_bg_tool_launch = (not is_denied_or_failed) and (
        has_bg_output_marker
        or (
            tname_low in ("invoke_subagent", "schedule", "swarm_wait", "run_workflow")
            and state_upper in ("DONE", "COMPLETED", "SUCCESS")
        )
    )
    if is_bg_tool_launch:
        m_tid = re.search(r"task\s*id\s*[:=#]?\s*([a-z0-9_.\-]+)", out_low)
        bg_tid = m_tid.group(1).strip(".") if m_tid else f"step_{eff_step_idx}"
        if not bg_tid:
            bg_tid = f"step_{eff_step_idx}"
        active_bg_tasks.add(bg_tid)
        return True

    if tname_low == "manage_task" and state_upper in _TERMINAL_TOOL_STATES:
        params = (
            entry.get("parameters")
            if isinstance(entry.get("parameters"), dict)
            else {}
        )
        action_low = str(params.get("Action") or params.get("action") or "").strip().lower()
        target_tid = (
            str(params.get("TaskId") or params.get("task_id") or "")
            .strip()
            .lower()
            .strip(".")
        )
        if action_low == "kill":
            if target_tid and target_tid in active_bg_tasks:
                active_bg_tasks.discard(target_tid)
            else:
                active_bg_tasks.clear()
        elif action_low == "list":
            if any(
                m in out_low
                for m in ("no running", "0 running", "no active", "none", "empty")
            ):
                active_bg_tasks.clear()
        elif action_low == "status":
            if any(
                m in out_low
                for m in (
                    "exited",
                    "completed",
                    "finished",
                    "terminated",
                    "killed",
                    "not found",
                    "exit code",
                    "status: done",
                    "status: failed",
                )
            ):
                if target_tid and target_tid in active_bg_tasks:
                    active_bg_tasks.discard(target_tid)
                elif not target_tid or len(active_bg_tasks) == 1:
                    active_bg_tasks.clear()

    return bool(active_bg_tasks)


def is_parked_async_turn(
    text: str,
    proc_tool_calls: list[dict[str, Any]],
    *,
    waiting_for_bg_task: bool = False,
) -> bool:
    """
    Detect when headless `agy -p` exited (`rc == 0`) while parking the turn to wait for
    a background command or subagent/swarm/timer notification.
    Guaranteed 0% false positives on normal Q&A / planning text when no real async tool ran.
    """
    if waiting_for_bg_task:
        return True
    has_real_async_tool = False
    for tc in proc_tool_calls or []:
        if not isinstance(tc, dict):
            continue
        st_up = str(tc.get("state") or "").strip().upper()
        out_low = str(tc.get("output") or "").lower()
        if (
            st_up in ("ERROR", "FAILED")
            or "permission denied" in out_low
            or "matches user-configured deny rule" in out_low
        ):
            continue
        tname_low = str(tc.get("name") or "").strip().lower()
        has_bg_marker = (
            "tool is running as a background task" in out_low
            or "running in the background" in out_low
            or "task id:" in out_low
        )
        is_bg_cmd = (
            tname_low in ("run_command", "command", "bash", "execute_command")
            or "run_command" in tname_low
        ) and has_bg_marker
        is_async_delegate = tname_low in (
            "invoke_subagent",
            "swarm",
            "swarm_wait",
            "schedule",
            "run_workflow",
        )
        if is_bg_cmd or is_async_delegate:
            has_real_async_tool = True
            break

    if not has_real_async_tool:
        return False

    for ln in _trailing_active_lines(text, 4):
        if _is_interim_wait_status_line(ln) or "conversation://" in ln.lower():
            return True
    return False


def _build_parked_turn_resume_prompt(
    accumulated_text: str,
    *,
    execution_contour: str = "host",
) -> str:
    partial = (accumulated_text or "").rstrip()
    tail_note = ""
    if partial:
        tail = partial[-600:]
        tail_note = (
            "\nПользователь уже видит в этой карточке ответа текст, заканчивающийся на:\n"
            f"«{tail}»\n"
            "Продолжай ответ сразу со следующего абзаца после этого фрагмента — "
            "БЕЗ повторов уже выведенного текста, извинений и лишних вступлений."
        )
    if execution_contour == "docker":
        sync_hint = (
            "команды сборки и тестов выполняй через MCP `hub_sandbox` / `sandbox_exec`, "
            "НЕ вызывая хостовый `run_command`"
        )
    elif execution_contour != "host":
        sync_hint = "используй файловые инструменты, НЕ вызывая хостовый `run_command`"
    else:
        sync_hint = "при вызове `run_command` передавай `WaitMsBeforeAsync=10000`"
    return (
        "[СИСТЕМНОЕ ПРОДОЛЖЕНИЕ ХОДА: Однопроцессный headless-вызов `agy -p` завершился при попытке "
        "запарковать ход в ожидании фонового события — реактивные уведомления между вызовами недоступны. "
        "НЕ вызывай `invoke_subagent`, `swarm` или `schedule` и НЕ завершай ход на промежуточном ожидании. "
        "Проверь текущее состояние файлов и процессов напрямую и доведи до конца все оставшиеся шаги задачи "
        f"пользователя самостоятельно и синхронно ({sync_hint})."
        f"{tail_note}]"
    )


def _build_empty_turn_resume_prompt(
    proc_tools: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Build a continuation prompt when `agy -p` exits cleanly (`rc == 0`) with tools/thinking but no visible answer."""
    _ = proc_tools
    return (
        "[СИСТЕМНОЕ ПРОДОЛЖЕНИЕ ХОДА: Предыдущий шаг завершился после чтения инструментов/рассуждений "
        "без текстового ответа пользователю. НЕ перечитывай уже прочитанные файлы и схемы заново — "
        "сразу выполни запланированные действия (включая вызовы MCP/поиска, если они требуются) "
        "и выдай полный итоговый ответ пользователю.]"
    )


def _has_real_file_write_tools(tool_calls: Optional[list[dict[str, Any]]]) -> bool:
    """Return True if any non-failed/non-denied tool call in the turn performed a real file write/edit."""
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        st_up = str(tc.get("state") or "").strip().upper()
        out_low = str(tc.get("output") or "").lower()
        if (
            st_up in ("ERROR", "FAILED", "INTERRUPTED", "CANCELLED")
            or "permission denied" in out_low
            or "matches user-configured deny rule" in out_low
        ):
            continue
        t_low = str(tc.get("name") or "").strip().lower()
        if (
            t_low in _WRITING_TOOL_NAMES
            or "write_file" in t_low
            or "edit_file" in t_low
            or "replace_file_content" in t_low
        ):
            return True
    return False


_STALLED_LARGE_FILE_HINT: str = (
    "Previous step timed out while streaming a very large response or single monolithic file tool call. "
    "Continue immediately from the current workspace state, check already modified files first, and if creating "
    "or editing a large file (>350 lines), write a clean skeleton/first part via `write_file` and add the remaining "
    "sections via smaller sequential `edit_file` or `write_file(Append=True)` calls so no single tool call stalls the stream."
)


def _build_auto_continue_prompt(
    accumulated_text: str,
    *,
    stalled_without_progress: bool = False,
) -> str:
    partial = (accumulated_text or "").rstrip()
    if partial:
        tail = partial[-180:]
        base = (
            "The upstream network stream timed out mid-turn. Continue the task from where it stopped "
            "without repeating completed tool steps. Note: the user already sees text ending with: "
            f"«...{tail}...» — continue seamlessly right after that point without duplicating earlier paragraphs."
        )
    else:
        base = (
            "The upstream network stream timed out mid-turn. Continue the task from where it stopped "
            "without repeating completed tool steps."
        )
    if stalled_without_progress:
        return f"{base} {_STALLED_LARGE_FILE_HINT}"
    return base


def _build_continue_turn_prompt(
    partial_assistant_text: str,
    *,
    was_cancelled: bool = False,
) -> str:
    reason_ru = (
        "остановлен пользователем"
        if was_cancelled
        else "прерван по сетевому таймауту / обрыву соединения"
    )
    partial = (partial_assistant_text or "").rstrip()
    chunk_hint = (
        "Если требуется создать или изменить крупный файл (>350 строк), сначала запиши компактный каркас/первую часть "
        "через `write_file`, а остальные секции дописывай последовательными компактными вызовами `edit_file` или "
        "`write_file(Append=True)`, чтобы ни один одиночный вызов не зависал при стриминге."
    )
    if partial:
        tail = partial[-1200:]
        return (
            f"[СИСТЕМНОЕ ПРОДОЛЖЕНИЕ ХОДА: Предыдущий ход был {reason_ru}. "
            "При необходимости сначала проверь актуальное состояние уже изменённых файлов в рабочей папке, "
            "не повторяй уже выполненные шаги и продолжи выполнение задачи ровно с места остановки — "
            "БЕЗ извинений, вступлений и дублирования уже выведенного текста. "
            f"{chunk_hint}\n"
            "Пользователь уже видит в этой же карточке текст, заканчивающийся на:\n"
            f"«{tail}»\n"
            "Продолжай ответ непосредственно со следующего символа или абзаца после этого фрагмента.]"
        )
    return (
        f"[СИСТЕМНОЕ ПРОДОЛЖЕНИЕ ХОДА: Предыдущий ход был {reason_ru}. "
        "При необходимости сначала проверь актуальное состояние уже изменённых файлов в рабочей папке, "
        "не повторяй уже выполненные шаги и продолжи выполнение задачи ровно с места остановки — "
        f"БЕЗ извинений и лишних вступлений. {chunk_hint}]"
    )


async def _sleep_unless_cancelled(seconds: float, run_state: ActiveRun) -> bool:
    """Sleep up to `seconds` in small steps; return False immediately if `run_state.cancelled` becomes True."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if run_state.cancelled:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(0.25, remaining))


def _is_agy_auth_error(text):
    return bool(re.search(r'UNAUTHENTICATED|\b401\b|unauthorized|invalid[_ ]credentials', str(text), re.I))


def _working_turn(func):
    from functools import wraps
    @wraps(func)
    async def tracked(**kwargs):
        from . import agy_credentials
        agy_credentials.active_working_turns += 1
        stream = func(**kwargs)
        try:
            async for event in stream:
                yield event
        finally:
            try:
                await stream.aclose()
            finally:
                agy_credentials.active_working_turns -= 1
    return tracked


@_working_turn
async def stream_agy_turn(
    *,
    chat_id: str,
    kind: str,
    project_name: str,
    model: str,
    access_mode: str,
    prompt: str,
    attachments: list[dict[str, Any]],
    agy_conversation_id: str,
    history_messages: list[dict[str, Any]],
    compacted_summary: str = "",
    compacted_up_to_msg_id: int = 0,
    user: Optional[dict[str, Any]] = None,
    resolved_project_dir: Optional[Path] = None,
    extra_dirs: Optional[list[Path]] = None,
    execution_mode: str = "host",
    chat_title: str = "",
    project_scope_key: str = "",
    project_changelog: Optional[list[dict[str, Any]]] = None,
    recent_project_chats: Optional[list[dict[str, Any]]] = None,
    prev_chat_updated_at: str = "",
    referenced_chat_blocks: Optional[list[str]] = None,
    allow_hub_openrouter: bool = False,
    live_voice: bool = False,
    initial_content: str = "",
    initial_thinking: str = "",
    initial_tool_calls: Optional[list[dict[str, Any]]] = None,
    is_continue_turn: bool = False,
    agy_account_id: str = "",
    preferred_agy_account_id: str = "",
    disable_default_guardrails: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """
    Execute a turn via official agy CLI --output-format stream-json and yield live events:
      - {"type": "agy_session", "agy_conversation_id": "..."}
      - {"type": "thinking_status", "active": bool}
      - {"type": "thinking_update", "thinking": "..."}
      - {"type": "tool_step", "step": {...}}
      - {"type": "text_delta", "delta": "..."}
      - {"type": "done", "content": "...", "thinking": "...", "tool_calls": [...], "usage": {...}}
      - {"type": "cancelled", ...}
      - {"type": "error", "error": "..."}
    """
    auth_retry_used = False
    normalized_initial_tools = normalize_interrupted_tool_calls(initial_tool_calls)
    agy_bin = resolve_agy_bin()
    if not Path(agy_bin).exists():
        yield {
            "type": "error",
            "error": f"Не найден официальный клиент Antigravity CLI по пути: {agy_bin}",
            "content": initial_content or "",
            "thinking": initial_thinking or "",
            "tool_calls": normalized_initial_tools,
        }
        return

    is_admin = bool(user is None or user.get("role") == "admin")
    user_custom_rules = str((user or {}).get("custom_rules") or "").strip()
    user_cli_target: Any = user if user is not None else "admin"

    from . import database
    resolved_agy = await resolve_agy_account_for_chat_turn(
        model,
        preferred_account_id=preferred_agy_account_id,
        current_chat_account_id=agy_account_id,
    )
    active_account = resolved_agy["account"]
    target_pool = resolved_agy["target_pool"]
    switched_from = resolved_agy.get("switched_from")
    tried_accounts = {active_account["id"]} if active_account else set()
    if switched_from and switched_from.get("id"):
        tried_accounts.add(switched_from["id"])
    if agy_conversation_id and active_account:
        act_acc_id_str = str(active_account["id"])
        if (agy_account_id and act_acc_id_str != str(agy_account_id)) or (
            not agy_account_id
            and not agy_session_db_exists(agy_conversation_id, account_id=act_acc_id_str)
        ):
            agy_conversation_id = ""
    if resolved_agy.get("emit_failover") and switched_from and active_account:
        await proxy_router.reload_routes_from_db()
        yield {
            "type": "agy_failover",
            "from_label": switched_from.get("label") or switched_from.get("email"),
            "to_label": active_account.get("label") or active_account.get("email"),
            "from_account_id": switched_from["id"],
            "account_id": active_account["id"],
        }
        agy_conversation_id = ""

    if active_account and not active_account['is_enabled']:
        yield {'type':'error','error':'Все доступные Google-аккаунты на паузе.'}
        return

    def _prep_user_home() -> tuple[Path, Path]:
        h = ensure_user_cli_home(user_cli_target)
        rf = sync_user_cli_rules(user_cli_target, user_custom_rules)
        return h, rf

    active_cli_home, synced_rules_file = await asyncio.to_thread(_prep_user_home)
    agy_proxy_url: Optional[str] = None
    while True:
        try:
            agy_proxy_url = await proxy_router.ensure_proxy_for_context(
                "agy", account_id=active_account["id"] if active_account else ""
            )
            active_cli_home = await materialize_agy_account_home(active_account, user_cli_target)
            break
        except (proxy_router.ProxyRouteError, ValueError, OSError) as exc:
            err_str = str(exc)
            if active_account and active_account.get("id"):
                failed_acc_id = str(active_account["id"])
                tried_accounts.add(failed_acc_id)
                try:
                    await database.update_agy_account_telemetry(failed_acc_id, last_error=err_str[:300])
                except Exception:
                    pass
                can_failover = (
                    not str(preferred_agy_account_id or "").strip()
                    and bool(active_account.get("auto_failover", True))
                )
                nxt_cand = (
                    await _pick_next_agy_failover_account(tried_accounts, target_pool)
                    if can_failover
                    else None
                )
                if nxt_cand:
                    prev_acc = active_account
                    active_account = nxt_cand
                    tried_accounts.add(str(nxt_cand["id"]))
                    agy_conversation_id = ""
                    await proxy_router.reload_routes_from_db()
                    yield {
                        "type": "agy_failover",
                        "from_label": prev_acc.get("label") or prev_acc.get("email"),
                        "to_label": nxt_cand.get("label") or nxt_cand.get("email"),
                        "from_account_id": prev_acc["id"],
                        "account_id": nxt_cand["id"],
                    }
                    continue
            yield {
                "type": "error",
                "error": err_str,
                "content": initial_content or "",
                "thinking": initial_thinking or "",
                "tool_calls": normalized_initial_tools,
            }
            return

    turn_env = _build_subprocess_env(
        isolated_home_override=active_cli_home,
        proxy_url=agy_proxy_url,
    )

    proj_dir: Optional[Path] = None

    if kind == "project" and project_name:
        proj_dir = resolved_project_dir or (resolve_project_dir(project_name) if is_admin else None)
        if not proj_dir or not proj_dir.exists():
            yield {
                "type": "error",
                "error": f"Папка проекта '{project_name}' не найдена или доступ запрещён.",
                "content": initial_content or "",
                "thinking": initial_thinking or "",
                "tool_calls": normalized_initial_tools,
            }
            return
        await asyncio.to_thread(ensure_isolated_project_git_boundary, proj_dir)
        cwd = proj_dir
    else:
        eff_chat_user = user if user is not None else {"id": "admin", "username": "admin", "role": "admin"}
        cwd = await asyncio.to_thread(
            config.resolve_user_chat_sandbox_dir, eff_chat_user, chat_id, create=True
        )
        await asyncio.to_thread(ensure_isolated_project_git_boundary, cwd.parent)

    eff_exec_mode = (
        resolve_effective_project_execution_mode(
            user if user is not None else {"role": "admin"},
            execution_mode,
        )
        if kind == "project"
        else ("host" if is_admin else "sandbox_files")
    )
    can_host_shell = bool(
        kind == "project"
        and (is_admin or str((user or {}).get("max_access_mode") or "") == "host_full")
    )
    use_docker_contour = bool(
        kind == "project"
        and proj_dir is not None
        and eff_exec_mode == "docker"
    )
    docker_info = await check_docker_status() if use_docker_contour else None
    # Whenever host contour is not explicitly opened by Dual-Key ('host_full'/admin + 'host' project),
    # strictly block host Windows shell commands (`command(*)`)!
    force_no_host_shell = bool(
        (kind != "project" and not is_admin)
        or eff_exec_mode != "host"
        or not can_host_shell
    )
    allow_hub_sandbox = bool(
        use_docker_contour
        and access_mode == "full_access"
        and docker_info
        and docker_info.get("sandbox_mcp_ready")
    )
    effective_access_mode = access_mode
    if use_docker_contour and access_mode == "full_access" and not allow_hub_sandbox:
        effective_access_mode = "write_files"
    if force_no_host_shell and effective_access_mode == "full_access" and not allow_hub_sandbox:
        effective_access_mode = "write_files"

    exec_contour, docker_unavail_reason = resolve_turn_execution_contour(
        kind=kind,
        requested_access_mode=access_mode,
        eff_exec_mode=eff_exec_mode,
        force_no_host_shell=force_no_host_shell,
        allow_hub_sandbox=allow_hub_sandbox,
        user=user,
        docker_info=docker_info,
    )

    allow_hub_host = bool(
        kind == "project"
        and not force_no_host_shell
        and effective_access_mode == "full_access"
    )
    clean_extra_dirs: list[Path] = []
    if kind == "project" and extra_dirs:
        for ed in extra_dirs:
            if ed is not None:
                try:
                    clean_extra_dirs.append(Path(ed).resolve())
                except OSError:
                    pass

    run_state = ACTIVE_RUNS.get(chat_id) or ActiveRun(chat_id=chat_id)
    run_state.user_id = str((user or {}).get("id") or run_state.user_id or "admin")
    run_state.kind = kind
    run_state.project_dir = proj_dir.resolve() if (kind == "project" and proj_dir is not None) else None
    run_state.workspace_dir = cwd.resolve() if cwd is not None else None
    run_state.chat_title = chat_title or run_state.chat_title
    run_state.access_mode = effective_access_mode or run_state.access_mode
    run_state.sandbox_allowed = allow_hub_sandbox
    run_state.host_allowed = allow_hub_host
    run_state.extra_dirs = list(clean_extra_dirs)
    run_state.sandbox_network = str((user or {}).get("sandbox_network") or "internet")
    run_state.model = str(model or "")
    run_state.engine = "agy"
    if kind == "project":
        run_state.project_name = project_name or run_state.project_name
        run_state.scope_key = project_scope_key or run_state.scope_key
    ACTIVE_RUNS[chat_id] = run_state

    created_large_prompt_files: list[Path] = []
    created_upload_dir_for_prompt: Optional[Path] = None
    stream_proc_runs = 0
    accum_stream_usage: dict[str, Any] = {}

    def _record_subprocess_usage(
        raw_usage: Optional[dict[str, Any]],
        proc_tools_list: list[dict[str, Any]],
        conv_id_tag: str = "",
    ) -> dict[str, Any]:
        nonlocal stream_proc_runs, accum_stream_usage
        inc = dict(raw_usage) if isinstance(raw_usage, dict) else {}
        inc_in = int(inc.get("input_tokens") or 0)
        inc_cache = int(inc.get("cache_read_tokens") or 0)
        inc_out = int(inc.get("output_tokens") or 0)
        inc_think = int(inc.get("thinking_tokens") or 0)
        if (inc_in + inc_cache + inc_out + inc_think) <= 0 and not inc:
            return dict(accum_stream_usage)
        stream_proc_runs += 1
        merged_u = dict(accum_stream_usage)
        for k, v in inc.items():
            if k in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens") and isinstance(v, (int, float)) and not isinstance(v, bool):
                old_v = merged_u.get(k, 0)
                merged_u[k] = (old_v + v) if isinstance(old_v, (int, float)) and not isinstance(old_v, bool) else v
            else:
                merged_u[k] = v
        merged_u["provider"] = "agy"
        if conv_id_tag:
            merged_u["agy_conversation_id"] = conv_id_tag
        merged_u["proc_runs"] = stream_proc_runs
        if (inc_in + inc_cache) > 0:
            merged_u["last_run_input_tokens"] = inc_in
            merged_u["last_run_cache_tokens"] = inc_cache
            merged_u["last_run_output_tokens"] = inc_out
            merged_u["last_run_thinking_tokens"] = inc_think
            merged_u["last_run_llm_calls"] = _count_turn_llm_calls(proc_tools_list)
        accum_stream_usage = merged_u
        return dict(accum_stream_usage)

    try:
        active_prior_msgs = [
            m
            for m in history_messages[:-1]
            if int(m.get("id") or 0) > int(compacted_up_to_msg_id or 0)
            and m.get("role") != "system"
            and m.get("status") != "compacted"
            and (m.get("content", "").strip() or bool(m.get("attachments")))
        ]
        has_prior_history = bool(
            (compacted_summary or "").strip()
            or active_prior_msgs
            or normalized_initial_tools
            or (initial_content or "").strip()
        )
        cli_session_exists = agy_session_db_exists(
            agy_conversation_id,
            cli_home=active_cli_home,
            account_id=(str(active_account["id"]) if active_account else ""),
        )

        # If local session exists, try resuming it first (with __RECOVERY__ fallback).
        # If local session was reset by Compaction or missing on disk, bootstrap a fresh
        # lightweight CLI trajectory seeded with compacted_summary + active D:\ history!
        if agy_conversation_id and cli_session_exists:
            attempt_conv_ids = [agy_conversation_id, "__RECOVERY__"]
        elif has_prior_history:
            attempt_conv_ids = ["__RECOVERY__"]
        else:
            attempt_conv_ids = [""]

        def _seed_initial_turn_state() -> tuple[dict[int, dict[str, Any]], str]:
            seeded_map: dict[int, dict[str, Any]] = {}
            for idx, raw_tc in enumerate(normalized_initial_tools):
                tc_copy = dict(raw_tc)
                raw_sidx = tc_copy.get("step_index")
                try:
                    sidx = int(raw_sidx) if raw_sidx is not None else idx
                except (TypeError, ValueError):
                    sidx = idx
                if sidx in seeded_map:
                    sidx = (max(seeded_map.keys()) + 1) if seeded_map else 0
                tc_copy["step_index"] = sidx
                seeded_map[sidx] = tc_copy
                if kind == "project":
                    for w_rel in extract_writing_tool_paths(
                        str(tc_copy.get("name") or ""),
                        tc_copy.get("parameters") or {},
                        proj_dir,
                    ):
                        if len(run_state.active_files) < 12:
                            run_state.active_files.add(w_rel)
            run_state.accumulated_text = initial_content or ""
            run_state.accumulated_thinking = initial_thinking or ""
            run_state.tool_calls = [seeded_map[k] for k in sorted(seeded_map.keys())]
            return seeded_map, (initial_thinking or "")

        for attempt_marker in attempt_conv_ids:
            is_recovery = attempt_marker == "__RECOVERY__"
            resume_id = "" if is_recovery else attempt_marker
            is_first_turn = not bool(resume_id)

            tool_map, base_thinking_prefix = _seed_initial_turn_state()

            pulse_block = ""
            if kind == "project" and project_name:
                pulse_block = build_project_pulse_block(
                    project_name=project_name,
                    chat_id=chat_id,
                    is_first_turn=is_first_turn,
                    scope_key=project_scope_key,
                    changelog=project_changelog,
                    recent_chats=recent_project_chats,
                    prev_chat_updated_at=prev_chat_updated_at,
                )

            effective_prompt = _augment_prompt_for_mode(
                kind=kind,
                prompt=prompt,
                attachments=[] if (is_continue_turn and bool(resume_id) and not is_recovery) else attachments,
                is_first_turn=is_first_turn,
                access_mode=effective_access_mode,
                inline_hub_rules="",
                project_pulse_block=pulse_block,
                referenced_chat_blocks=referenced_chat_blocks,
                live_voice=live_voice,
                execution_contour=exec_contour,
                docker_unavailable_reason=docker_unavail_reason,
                extra_dirs=clean_extra_dirs,
                project_dir=proj_dir,
                disable_default_guardrails=disable_default_guardrails,
            )
            if is_recovery:
                effective_prompt = _build_history_recovery_prompt(
                    history_messages,
                    effective_prompt,
                    compacted_summary=compacted_summary,
                    compacted_up_to_msg_id=compacted_up_to_msg_id,
                    resume_tool_calls=(
                        run_state.tool_calls
                        if (is_continue_turn or normalized_initial_tools)
                        else None
                    ),
                    partial_assistant_text=(
                        run_state.accumulated_text
                        if (is_continue_turn or (initial_content or "").strip())
                        else ""
                    ),
                    is_continue_turn=is_continue_turn,
                )

            is_shared_turn = bool(
                kind == "project"
                and (
                    str(project_scope_key or "").startswith("shared:")
                    or (
                        proj_dir is not None
                        and proj_dir.resolve().parent == config.SHARED_PROJECTS_DIR.resolve()
                    )
                )
            )
            turn_chat_ref = {
                "id": chat_id,
                "user_id": str((user or {}).get("id") or "admin"),
            }
            owner_uname = str((user or {}).get("username") or "")
            primary_upload_dir = config.resolve_chat_uploads_dir(
                turn_chat_ref,
                owner_username=owner_uname,
                is_shared_project=is_shared_turn,
            )

            extra_upload_dir: Optional[Path] = None
            if len(effective_prompt) > 28500:
                # Step 1 guard against Windows CreateProcessW 32,767-char CLI argument limit
                extra_upload_dir = primary_upload_dir
                if not extra_upload_dir.exists():
                    created_upload_dir_for_prompt = extra_upload_dir
                large_prompt_file, effective_prompt = _spill_prompt_to_upload_file(
                    effective_prompt, extra_upload_dir
                )
                created_large_prompt_files.append(large_prompt_file)

            existing_upload_dirs = config.resolve_chat_upload_dirs_for_turn(
                turn_chat_ref,
                chat_id=chat_id,
                owner_username=owner_uname,
                is_shared_project=is_shared_turn,
            )
            turn_upload_dirs: list[Path] = []
            for cand_ud in existing_upload_dirs:
                try:
                    if not cand_ud.exists():
                        continue
                    has_real_files = any(
                        f.name != "large_prompt_input.md"
                        and not f.name.startswith("_large_prompt_input_")
                        for f in cand_ud.iterdir()
                    )
                    if (
                        bool(attachments)
                        or (extra_upload_dir is not None and cand_ud == extra_upload_dir)
                        or has_real_files
                    ):
                        turn_upload_dirs.append(cand_ud)
                except OSError:
                    continue
            scoped_upload_dir: Optional[Path] = turn_upload_dirs[0] if turn_upload_dirs else None
            allow_hub_files_turn = bool(
                kind == "project"
                and (effective_access_mode in ("read_only", "write_files") or force_no_host_shell)
            )

            active_conv_id = resume_id
            consecutive_auto_continues = 0
            total_auto_continues = 0
            parked_resume_attempts = 0
            empty_turn_resumes = 0
            capacity_retry_attempts = 0
            current_prompt = effective_prompt
            current_resume_id = resume_id
            recover_next_attempt = False
            got_text_since_last_tool = False
            got_any_output_in_attempt = False
            needs_bg_wakeup_break = False
            needs_resume_stitch_check = bool(
                is_continue_turn and (initial_content or "").strip()
            )

            def _merge_thinking(latest_th: str) -> str:
                if not latest_th:
                    return run_state.accumulated_thinking
                if base_thinking_prefix and not latest_th.startswith(base_thinking_prefix):
                    return f"{base_thinking_prefix}\n\n---\n\n{latest_th}"
                return latest_th

            def _assemble_agy_cmd(prompt_arg: str, res_cid: str) -> list[str]:
                built = [
                    agy_bin,
                    "-p",
                    prompt_arg,
                    "--output-format",
                    "stream-json",
                    "--model",
                    model,
                ]
                if res_cid:
                    built.extend(["--conversation", res_cid])
                if kind == "project" and proj_dir is not None:
                    built.extend(["--add-dir", str(proj_dir)])
                    for ed in clean_extra_dirs:
                        built.extend(["--add-dir", str(ed)])
                for ud_item in turn_upload_dirs:
                    built.extend(["--add-dir", str(ud_item)])
                if (
                    kind == "project"
                    and (force_no_host_shell
                    or not is_admin
                    or effective_access_mode != "full_access")
                    or allow_hub_openrouter
                ):
                    for ro_dir in _collect_safe_cli_readonly_dirs(active_cli_home):
                        built.extend(["--add-dir", str(ro_dir)])
                built.extend(
                    _build_permission_args(
                        kind,
                        effective_access_mode,
                        scoped_upload_dir,
                        allowed_upload_dirs=turn_upload_dirs,
                        is_admin=is_admin,
                        allow_hub_openrouter=allow_hub_openrouter,
                        allow_hub_sandbox=allow_hub_sandbox,
                        allow_hub_host=allow_hub_host,
                        allow_hub_files=allow_hub_files_turn,
                        cli_home_dir=active_cli_home,
                        project_dir=proj_dir,
                        extra_dirs=clean_extra_dirs,
                        force_no_host_shell=force_no_host_shell,
                        resume_conv_id=res_cid,
                        sandbox_network=run_state.sandbox_network,
                    )
                )
                return built

            while True:
                prior_tool_keys: set[int] = set(tool_map.keys())
                proc_step_remap: dict[int, int] = {}

                gemini = classify_model_family(model) == "gemini"
                cli_root = active_cli_home / ".gemini" / "antigravity-cli"
                attempt_log = None
                log_args = []
                if gemini:
                    log_dir = cli_root / "log"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    attempt_log = log_dir / f"cli-hub-{time.time_ns()}-{uuid.uuid4().hex[:8]}.log"
                    log_args = ["--log-file", str(attempt_log)]

                cmd = _assemble_agy_cmd(current_prompt, current_resume_id) + log_args
                cmd_too_long = (
                    len(subprocess.list2cmdline(cmd)) > 32_400
                    if IS_WINDOWS
                    else sum(len(str(a).encode("utf-8")) for a in cmd) > 100_000
                )
                if cmd_too_long and "_large_prompt_input_" not in current_prompt:
                    if not primary_upload_dir.exists() and created_upload_dir_for_prompt is None:
                        created_upload_dir_for_prompt = primary_upload_dir
                    spilled_file, spilled_stub = _spill_prompt_to_upload_file(
                        current_prompt, primary_upload_dir
                    )
                    created_large_prompt_files.append(spilled_file)
                    if primary_upload_dir not in turn_upload_dirs:
                        turn_upload_dirs.insert(0, primary_upload_dir)
                    scoped_upload_dir = turn_upload_dirs[0] if turn_upload_dirs else None
                    if current_prompt == effective_prompt:
                        effective_prompt = spilled_stub
                    current_prompt = spilled_stub
                    cmd = _assemble_agy_cmd(current_prompt, current_resume_id) + log_args

                if run_state.cancelled:
                    yield {
                        "type": "cancelled",
                        "content": run_state.accumulated_text,
                        "thinking": run_state.accumulated_thinking,
                        "tool_calls": run_state.tool_calls,
                    }
                    return

                try:
                    attempt_proxy_url = await proxy_router.ensure_proxy_for_context("agy", account_id=active_account["id"] if active_account else "")
                    turn_env = _apply_agy_proxy_env(turn_env, attempt_proxy_url)
                except proxy_router.ProxyRouteError as exc:
                    yield {
                        "type": "error",
                        "error": str(exc),
                        "content": run_state.accumulated_text,
                        "thinking": run_state.accumulated_thinking,
                        "tool_calls": run_state.tool_calls,
                    }
                    return

                launch_wall_time = time.time()
                disk_state = ((), ())
                if gemini and current_resume_id:
                    disk_state = await asyncio.to_thread(_gemini_disk_progress, cli_root, current_resume_id)
                disk_cid = current_resume_id
                authenticated = submitted = False
                model_wait_deadline: Optional[float] = None
                startup_started = time.monotonic()
                proc = await create_agy_subprocess_exec(
                    *cmd,
                    cwd=str(cwd),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=turn_env,
                    start_new_session=(os.name != "nt"),
                    limit=4 * 1024 * 1024,  # 4 MB line buffer for large JSON events
                    **subprocess_hidden_kwargs(),
                )
                run_state.process = proc
                assert proc.stdout is not None
                assert proc.stderr is not None

                got_init_in_proc = False
                got_any_output = False
                got_any_text_in_proc = False
                completed_tools_in_proc = 0
                agent_response_completed = False
                waiting_for_bg_task = False
                active_bg_tasks: set[str] = set()
                bg_wait_resp_step_idx: Optional[int] = None
                got_result_event = False
                watchdog_post_text_idle = False
                watchdog_network_idle = False
                result_error: Optional[str] = None
                final_usage: dict[str, Any] = {}
                final_response_fallback = ""

                if not run_state.accumulated_text:
                    run_state.thinking_active = True
                    run_state.seq += 1
                    yield {"type": "thinking_status", "active": True, "seq": run_state.seq}

                while True:
                    if run_state.cancelled:
                        break

                    has_active_tool_now = any(
                        tc.get("state") == "ACTIVE" for tc in run_state.tool_calls
                    )
                    if has_active_tool_now or waiting_for_bg_task:
                        read_timeout = STREAM_ACTIVE_TOOL_TIMEOUT_SEC
                        post_text_idle_candidate = False
                    elif (
                        got_any_text_in_proc
                        and got_text_since_last_tool
                        and looks_like_completed_response(run_state.accumulated_text)
                    ):
                        read_timeout = STREAM_POST_TEXT_IDLE_TIMEOUT_SEC
                        post_text_idle_candidate = True
                    elif not got_init_in_proc:
                        startup_cap = (
                            STREAM_RESUME_STARTUP_IDLE_TIMEOUT_SEC
                            if current_resume_id
                            else STREAM_STARTUP_IDLE_TIMEOUT_SEC
                        )
                        read_timeout = min(
                            startup_cap,
                            STREAM_NETWORK_IDLE_TIMEOUT_SEC,
                        )
                        post_text_idle_candidate = False
                    else:
                        read_timeout = STREAM_NETWORK_IDLE_TIMEOUT_SEC
                        post_text_idle_candidate = False

                    if gemini and not (has_active_tool_now or waiting_for_bg_task) and not post_text_idle_candidate:
                        if got_init_in_proc or submitted:
                            if model_wait_deadline is None:
                                model_wait_deadline = time.monotonic() + _gemini_first_event_timeout(model)
                            read_timeout = max(0.0, model_wait_deadline - time.monotonic())
                        else:
                            read_timeout = max(0.0, read_timeout - (time.monotonic() - startup_started))

                    readline_task = asyncio.create_task(proc.stdout.readline())
                    raw_line = b""
                    timed_out = False
                    deadline = time.monotonic() + read_timeout
                    try:
                        while not run_state.cancelled:
                            remaining = deadline - time.monotonic()
                            done, _ = await asyncio.wait(
                                {readline_task}, timeout=max(0, min(STREAM_DISK_POLL_SEC, remaining))
                            )
                            if done:
                                raw_line = readline_task.result()
                                break
                            if run_state.cancelled:
                                break
                            if gemini:
                                oauth_ok, sent, fatal = await asyncio.to_thread(_gemini_attempt_log, attempt_log)
                                if oauth_ok and not authenticated:
                                    authenticated = True
                                    _logger.debug("Gemini CLI OAuth completed for chat %s", chat_id)
                                if sent and not submitted:
                                    submitted = True
                                    if (
                                        not got_init_in_proc
                                        and not (has_active_tool_now or waiting_for_bg_task)
                                        and not post_text_idle_candidate
                                    ):
                                        deadline = model_wait_deadline = time.monotonic() + _gemini_first_event_timeout(model)
                                if readline_task.done():
                                    raw_line = readline_task.result()
                                    break
                                if fatal:
                                    result_error = fatal
                                    break
                                if active_conv_id and (got_init_in_proc or submitted):
                                    state = await asyncio.to_thread(_gemini_disk_progress, cli_root, active_conv_id)
                                    previous = disk_state if disk_cid == active_conv_id else ((), ())
                                    disk_cid, disk_state = active_conv_id, state
                                    if state != previous and any(state):
                                        idle_budget = read_timeout
                                        if not (has_active_tool_now or waiting_for_bg_task) and not post_text_idle_candidate:
                                            idle_budget = _gemini_first_event_timeout(model)
                                        deadline = time.monotonic() + idle_budget
                                        if not (has_active_tool_now or waiting_for_bg_task) and not post_text_idle_candidate:
                                            model_wait_deadline = deadline
                                        if state[0] and state[0] != previous[0]:
                                            latest = await asyncio.to_thread(
                                                extract_latest_turn_thinking, active_conv_id, cli_root,
                                                current_turn_only=True,
                                            )
                                            if latest:
                                                merged = _merge_thinking(latest)
                                                if merged != run_state.accumulated_thinking:
                                                    run_state.accumulated_thinking = merged
                                                    run_state.seq += 1
                                                    yield {"type": "thinking_update", "thinking": merged, "seq": run_state.seq}
                            if time.monotonic() >= deadline:
                                timed_out = True
                                break
                    finally:
                        if not readline_task.done():
                            readline_task.cancel()
                            await asyncio.gather(readline_task, return_exceptions=True)

                    if timed_out or result_error:
                        await _terminate_hung_cli_proc(proc)
                        if timed_out:
                            if post_text_idle_candidate:
                                watchdog_post_text_idle = True
                            else:
                                watchdog_network_idle = True
                                result_error = "stream idle watchdog timeout"
                        break

                    if not raw_line:
                        break

                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except Exception:
                        continue

                    etype = evt.get("event")
                    if etype in ("init", "step_update", "message"):
                        got_init_in_proc = True
                    if gemini:
                        model_wait_deadline = time.monotonic() + _gemini_first_event_timeout(model)

                    if etype == "init":
                        cid = evt.get("conversation_id", "")
                        if cid and cid != active_conv_id:
                            active_conv_id = cid
                            yield {"type": "agy_session", "agy_conversation_id": cid, "agy_account_id": active_account["id"] if active_account else ""}

                    elif etype == "step_update":
                        su = evt.get("step_update") or {}
                        cid = su.get("conversation_id") or active_conv_id
                        if cid and cid != active_conv_id:
                            active_conv_id = cid
                            yield {"type": "agy_session", "agy_conversation_id": cid, "agy_account_id": active_account["id"] if active_account else ""}

                        step_type = su.get("step_type", "")
                        state = su.get("state", "")
                        raw_step_idx = int(su.get("step_index", 0))

                        # Direct thinking delta if emitted by future CLI versions
                        th_delta = su.get("thinking_delta", "")
                        if th_delta:
                            run_state.accumulated_thinking += th_delta
                            run_state.seq += 1
                            yield {
                                "type": "thinking_update",
                                "thinking": run_state.accumulated_thinking,
                                "seq": run_state.seq,
                            }

                        if step_type == "tool":
                            got_any_output = True
                            got_any_output_in_attempt = True
                            agent_response_completed = False
                            got_text_since_last_tool = False

                            if raw_step_idx in proc_step_remap:
                                eff_step_idx = proc_step_remap[raw_step_idx]
                            elif raw_step_idx in prior_tool_keys:
                                eff_step_idx = (max(tool_map.keys()) + 1) if tool_map else 0
                                proc_step_remap[raw_step_idx] = eff_step_idx
                            else:
                                eff_step_idx = raw_step_idx
                                proc_step_remap[raw_step_idx] = raw_step_idx

                            prev_state_upper = (
                                str((tool_map.get(eff_step_idx) or {}).get("state") or "")
                                .strip()
                                .upper()
                            )
                            state_upper = str(state or "").strip().upper()
                            if (
                                state_upper in _TERMINAL_TOOL_STATES
                                and prev_state_upper not in _TERMINAL_TOOL_STATES
                            ):
                                completed_tools_in_proc += 1

                            tinfo = su.get("tool_info") or {}
                            tname = su.get("tool_name") or tinfo.get("name") or "tool"
                            entry = {
                                "step_index": eff_step_idx,
                                "state": state,
                                "name": tname,
                                "parameters": tinfo.get("parameters") or {},
                                "output": tinfo.get("output") or "",
                                "duration": su.get("duration_seconds"),
                            }
                            tool_map[eff_step_idx] = entry
                            run_state.tool_calls = [
                                tool_map[k] for k in sorted(tool_map.keys())
                            ]
                            was_waiting_bg = waiting_for_bg_task
                            bg_wait_resp_step_idx = None
                            waiting_for_bg_task = _update_bg_task_state_on_tool(
                                active_bg_tasks, entry
                            )
                            if (
                                was_waiting_bg
                                and not waiting_for_bg_task
                                and run_state.accumulated_text
                                and not run_state.accumulated_text.endswith("\n")
                            ):
                                needs_bg_wakeup_break = True
                            if kind == "project":
                                for w_rel in extract_writing_tool_paths(
                                    tname, entry["parameters"], proj_dir
                                ):
                                    if len(run_state.active_files) < 12:
                                        run_state.active_files.add(w_rel)
                            # Refresh thinking written before the tool call!
                            if active_conv_id and state == "ACTIVE":
                                latest_th = await asyncio.to_thread(
                                    extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                                )
                                if latest_th:
                                    merged_th = _merge_thinking(latest_th)
                                    if merged_th != run_state.accumulated_thinking:
                                        run_state.accumulated_thinking = merged_th
                                        run_state.seq += 1
                                        yield {
                                            "type": "thinking_update",
                                            "thinking": merged_th,
                                            "seq": run_state.seq,
                                        }

                            run_state.seq += 1
                            yield {"type": "tool_step", "step": entry, "seq": run_state.seq}

                        elif step_type == "agent_response":
                            tdelta = su.get("text_delta", "")
                            if tdelta:
                                got_any_output = True
                                got_any_output_in_attempt = True
                                got_any_text_in_proc = True
                                got_text_since_last_tool = True
                                if waiting_for_bg_task:
                                    if agent_response_completed or (
                                        bg_wait_resp_step_idx is not None
                                        and raw_step_idx != bg_wait_resp_step_idx
                                    ):
                                        active_bg_tasks.clear()
                                        waiting_for_bg_task = False
                                        agent_response_completed = False
                                        bg_wait_resp_step_idx = None
                                        needs_bg_wakeup_break = True
                                    elif bg_wait_resp_step_idx is None:
                                        bg_wait_resp_step_idx = raw_step_idx
                                if needs_bg_wakeup_break:
                                    needs_bg_wakeup_break = False
                                    prev_bg_txt = run_state.accumulated_text
                                    if (
                                        prev_bg_txt
                                        and not prev_bg_txt.endswith("\n")
                                        and not tdelta.startswith("\n")
                                    ):
                                        tdelta = "\n\n" + tdelta.lstrip(" ")
                                if needs_resume_stitch_check:
                                    needs_resume_stitch_check = False
                                    tdelta = compute_resume_stitch_prefix(
                                        run_state.accumulated_text, tdelta
                                    ) + tdelta
                                if run_state.thinking_active:
                                    run_state.thinking_active = False
                                    run_state.seq += 1
                                    yield {"type": "thinking_status", "active": False, "seq": run_state.seq}
                                run_state.accumulated_text += tdelta
                                run_state.seq += 1
                                yield {"type": "text_delta", "delta": tdelta, "seq": run_state.seq}

                            if state == "DONE":
                                if got_any_text_in_proc or bool(run_state.accumulated_text.strip()):
                                    agent_response_completed = True
                                if active_conv_id:
                                    latest_th = await asyncio.to_thread(
                                        extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                                    )
                                    if latest_th:
                                        merged_th = _merge_thinking(latest_th)
                                        if merged_th != run_state.accumulated_thinking:
                                            run_state.accumulated_thinking = merged_th
                                            run_state.seq += 1
                                            yield {
                                                "type": "thinking_update",
                                                "thinking": merged_th,
                                                "seq": run_state.seq,
                                            }

                    elif etype == "result":
                        res = evt.get("result") or {}
                        final_usage = res.get("usage") or {}
                        final_response_fallback = res.get("response") or ""
                        res_status_upper = str(res.get("status") or "").strip().upper()
                        if res_status_upper == "ERROR":
                            result_error = res.get("error") or "Ошибка выполнения запроса в Antigravity CLI"
                        elif (
                            res_status_upper in ("FAILED", "ABORTED", "CANCELLED", "INTERRUPTED")
                            and not (run_state.accumulated_text or final_response_fallback).strip()
                        ):
                            result_error = (
                                res.get("error")
                                or f"Запрос в Antigravity CLI завершился со статусом {res_status_upper}"
                            )
                        got_result_event = True
                        break

                async def _drain_stderr_and_wait() -> tuple[bytes, int]:
                    sb = await proc.stderr.read()  # type: ignore[union-attr]
                    exit_code = await proc.wait()
                    return sb, exit_code

                drain_timeout = (
                    0.6 if (watchdog_post_text_idle or watchdog_network_idle) else 3.5
                )
                try:
                    stderr_bytes, rc = await asyncio.wait_for(
                        _drain_stderr_and_wait(), timeout=drain_timeout
                    )
                    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                except asyncio.TimeoutError:
                    stderr_text = ""
                    await _terminate_hung_cli_proc(proc)
                    rc = 0 if (got_result_event and not result_error and not watchdog_network_idle) else -9
                else:
                    if got_result_event and not result_error and rc != 0:
                        rc = 0

                if hasattr(proc, "aclose_lease"):
                    await proc.aclose_lease()

                if run_state.cancelled:
                    if active_conv_id:
                        latest_th = await asyncio.to_thread(
                            extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                        )
                        if latest_th:
                            run_state.accumulated_thinking = _merge_thinking(latest_th)
                    cancel_usage = {"provider": "agy", **final_usage}
                    if active_conv_id:
                        cancel_usage["agy_conversation_id"] = active_conv_id
                    yield {
                        "type": "cancelled",
                        "content": run_state.accumulated_text,
                        "thinking": run_state.accumulated_thinking,
                        "tool_calls": run_state.tool_calls,
                        "usage": cancel_usage,
                    }
                    return

                log_status = ""
                if (rc != 0 or result_error) and not stderr_text:
                    log_status = await asyncio.to_thread(_latest_cli_failure, active_cli_home, launch_wall_time, attempt_log)
                err_combined_raw = f"{result_error or ''} {stderr_text}".strip()
                err_combined_low = err_combined_raw.lower()
                is_stream_interrupted = (
                    "stream was interrupted" in err_combined_low
                    or "please continue the task" in err_combined_low
                )
                is_capacity_overload = is_transient_capacity_error(err_combined_raw)
                is_idle_stream_err = (
                    is_stream_interrupted
                    or is_transient_stream_error(err_combined_raw)
                )
                has_active_tools = any(
                    tc.get("state") == "ACTIVE" for tc in run_state.tool_calls
                )
                has_prior_turn_progress = bool(
                    got_any_output_in_attempt
                    or run_state.accumulated_text.strip()
                    or run_state.tool_calls
                )
                can_use_auto_continue_session = bool(
                    active_conv_id
                    and (
                        has_prior_turn_progress
                        or is_continue_turn
                        or (watchdog_network_idle and got_init_in_proc)
                    )
                )
                is_location_unsupported_err = is_gemini_location_unsupported_error(err_combined_raw)
                has_fresh_post_tool_text = bool(
                    got_any_text_in_proc
                    and got_text_since_last_tool
                    and bool(run_state.accumulated_text.strip())
                )
                is_sticky_post_text_location_err = bool(
                    is_location_unsupported_err
                    and has_fresh_post_tool_text
                    and not has_active_tools
                    and not waiting_for_bg_task
                )
                response_looks_done = (
                    False
                    if waiting_for_bg_task
                    else bool(
                        agent_response_completed
                        or is_sticky_post_text_location_err
                        or (
                            got_any_text_in_proc
                            and got_text_since_last_tool
                            and looks_like_completed_response(run_state.accumulated_text)
                        )
                    )
                )

                # Guard against false-positive post-stream timeout/interruption, trailing 503 capacity blip,
                # or sticky historical / post-response CLI errors (e.g. FAILED_PRECONDITION 400 echoed in
                # `result` from conversations/*.db or non-zero teardown exit code) after the response already
                # completed with fresh text, no active tools, and no background task wait.
                is_suppressed_post_done_err = (
                    is_idle_stream_err
                    or is_capacity_overload
                    or watchdog_post_text_idle
                    or is_sticky_post_text_location_err
                    or (
                        agent_response_completed
                        and got_any_text_in_proc
                        and not _is_agy_auth_error(err_combined_raw)
                        and not is_location_unsupported_err
                    )
                    or (rc != 0 and not result_error and not _is_agy_auth_error(err_combined_raw))
                )
                if (
                    (rc != 0 or result_error)
                    and is_suppressed_post_done_err
                    and response_looks_done
                    and bool(run_state.accumulated_text.strip())
                    and not has_active_tools
                    and not waiting_for_bg_task
                ):
                    if is_idle_stream_err or is_capacity_overload or watchdog_post_text_idle:
                        _logger.info(
                            "Suppressed trailing transient CLI error after completed response (chat=%s, rc=%s): %s",
                            chat_id,
                            rc,
                            (err_combined_raw or "")[:240],
                        )
                    else:
                        _logger.warning(
                            "Suppressed trailing post-response CLI error after completed response (chat=%s, rc=%s, agent_done=%s): %s",
                            chat_id,
                            rc,
                            agent_response_completed,
                            (err_combined_raw or "")[:240],
                        )
                    result_error = None
                    rc = 0
                    err_combined_raw = ""
                    err_combined_low = ""
                    is_capacity_overload = False
                    is_stream_interrupted = False
                    is_idle_stream_err = False
                    is_location_unsupported_err = False

                proc_tools_this_run = [
                    tool_map[idx]
                    for idx in sorted(set(proc_step_remap.values()))
                    if idx in tool_map
                ]
                if final_usage:
                    final_usage = _record_subprocess_usage(
                        final_usage, proc_tools_this_run, conv_id_tag=active_conv_id
                    )
                elif accum_stream_usage:
                    final_usage = dict(accum_stream_usage)
                    final_usage["provider"] = "agy"
                    if active_conv_id:
                        final_usage["agy_conversation_id"] = active_conv_id

                if active_account and active_account['has_credentials']:
                    try:
                        await sync_refreshed_agy_credentials_back(active_account['id'])
                    except (OSError, ValueError):
                        pass
                if (rc != 0 or result_error) and is_agy_quota_exhausted_error(err_combined_raw) and not run_state.accumulated_text.strip() and not run_state.tool_calls:
                    if active_account:
                        quotas = dict(active_account.get('quotas') or {})
                        quotas[('claude' if target_pool == 'claude_gpt' else 'gemini') + '_5h_pct'] = 0.0
                        groups = json.loads(json.dumps(quotas.get('groups') or []))
                        group = next((g for g in groups if g.get('id') == target_pool), None)
                        if group is None:
                            group = {'id':target_pool, 'buckets':[]}
                            groups.append(group)
                        bucket = next((b for b in group['buckets'] if b.get('window') == '5h'), None)
                        if bucket is None:
                            bucket = {'window':'5h'}
                            group['buckets'].append(bucket)
                        bucket['remaining_pct'] = 0.0
                        bucket['remaining_fraction'] = 0.0
                        if not _parse_agy_iso_utc(bucket.get('reset_time')) or _parse_agy_iso_utc(bucket['reset_time']) <= datetime.now(timezone.utc):
                            bucket['reset_time'] = (datetime.now(timezone.utc)+timedelta(hours=5)).isoformat()
                        quotas['groups'] = groups
                        await database.update_agy_account_telemetry(active_account['id'], quotas=quotas, last_error='Исчерпан лимит ' + target_pool)
                    can_mid_failover = bool(
                        active_account
                        and active_account.get('auto_failover', True)
                        and not str(preferred_agy_account_id or '').strip()
                    )
                    switched_ok = False
                    last_failover_err = ''
                    while can_mid_failover:
                        nxt = await _pick_next_agy_failover_account(tried_accounts, target_pool)
                        if not nxt:
                            break
                        prev_acc = active_account
                        tried_accounts.add(nxt['id'])
                        await proxy_router.reload_routes_from_db()
                        yield {
                            'type': 'agy_failover',
                            'from_label': prev_acc.get('label') or prev_acc.get('email'),
                            'to_label': nxt.get('label') or nxt.get('email'),
                            'from_account_id': prev_acc['id'],
                            'account_id': nxt['id'],
                        }
                        active_account = nxt
                        try:
                            nxt_proxy_url = await proxy_router.ensure_proxy_for_context('agy', account_id=nxt['id'])
                            nxt_cli_home = (
                                await materialize_agy_account_home(nxt, user_cli_target)
                                if nxt.get('has_credentials')
                                else active_cli_home
                            )
                        except (proxy_router.ProxyRouteError, ValueError, OSError) as f_exc:
                            last_failover_err = str(f_exc)
                            try:
                                await database.update_agy_account_telemetry(nxt['id'], last_error=last_failover_err[:300])
                            except Exception:
                                pass
                            continue
                        active_cli_home = nxt_cli_home
                        agy_proxy_url = nxt_proxy_url
                        turn_env = _build_subprocess_env(isolated_home_override=active_cli_home, proxy_url=agy_proxy_url)
                        current_resume_id = ''
                        resume_id = ''
                        recover_next_attempt = True
                        if '__RECOVERY__' not in attempt_conv_ids[attempt_conv_ids.index(attempt_marker) + 1 :]:
                            attempt_conv_ids.append('__RECOVERY__')
                        switched_ok = True
                        break
                    if switched_ok:
                        break
                    if last_failover_err:
                        err_usage = {'provider': 'agy', **final_usage}
                        if active_conv_id:
                            err_usage['agy_conversation_id'] = active_conv_id
                        yield {
                            'type': 'error',
                            'error': last_failover_err,
                            'content': run_state.accumulated_text,
                            'thinking': run_state.accumulated_thinking,
                            'tool_calls': run_state.tool_calls,
                            'usage': err_usage,
                        }
                        return

                if (rc != 0 or result_error) and _is_agy_auth_error(err_combined_raw) and not auth_retry_used:
                    auth_retry_used = True
                    try:
                        active_cli_home = await materialize_agy_account_home(
                            active_account, user_cli_target, force_refresh=True)
                    except (ValueError, OSError, proxy_router.ProxyRouteError) as refresh_error:
                        yield {'type': 'error', 'error': str(refresh_error),
                               'content': run_state.accumulated_text,
                               'thinking': run_state.accumulated_thinking,
                               'tool_calls': run_state.tool_calls,
                               'usage': {'provider': 'agy', **final_usage}}
                        return
                    else:
                        turn_env = _build_subprocess_env(isolated_home_override=active_cli_home, proxy_url=agy_proxy_url)
                        current_resume_id = active_conv_id or current_resume_id
                        if current_resume_id and (run_state.accumulated_text or run_state.tool_calls):
                            current_prompt = _build_auto_continue_prompt(run_state.accumulated_text)
                            needs_resume_stitch_check = bool(run_state.accumulated_text)
                        # Keep all accumulated text, thinking and tool state across retry.
                        continue

                if (rc != 0 or result_error) and _is_agy_auth_error(err_combined_raw):
                    # A repeated 401 must not enter generic broken-session recovery.
                    yield {'type': 'error', 'error': result_error or stderr_text or err_combined_raw,
                           'content': run_state.accumulated_text,
                           'thinking': run_state.accumulated_thinking,
                           'tool_calls': run_state.tool_calls,
                           'usage': {'provider': 'agy', **final_usage}}
                    return

                # Fast fallback from dead/broken/hung `resume_id` straight to `__RECOVERY__`
                # without waiting 3 x 120s or aborting the turn!
                if (
                    (rc != 0 or result_error)
                    and not run_state.cancelled
                    and resume_id
                    and attempt_marker != "__RECOVERY__"
                    and "__RECOVERY__" in attempt_conv_ids
                    and total_auto_continues == 0
                    and not got_any_output_in_attempt
                ):
                    stalled_before_init = (
                        not got_init_in_proc
                        and not (is_capacity_overload and capacity_retry_attempts < 1)
                    )
                    broken_session_kw = (
                        not is_capacity_overload
                        and not is_idle_stream_err
                        and any(
                            kw in err_combined_low
                            for kw in (
                                "not found",
                                "conversation",
                                "invalid",
                                "permission",
                                "unauthorized",
                                "unauthenticated",
                                "access denied",
                                "account",
                                "session",
                                "401",
                                "403",
                                "404",
                            )
                        )
                    )
                    if stalled_before_init or broken_session_kw:
                        bad_cid = str(resume_id or "").strip()
                        if bad_cid and ".." not in bad_cid and re.fullmatch(r"[0-9a-zA-Z_.-]{1,120}", bad_cid):
                            conv_dirs_to_clean: list[Path] = [
                                active_cli_home / ".gemini" / "antigravity-cli" / "conversations"
                            ]
                            if active_account and active_account.get("id"):
                                try:
                                    conv_dirs_to_clean.append(
                                        config.ensure_agy_account_cli_data_dir(str(active_account["id"])) / "conversations"
                                    )
                                except Exception:
                                    pass
                            elif ".agy_homes" not in active_cli_home.parts:
                                conv_dirs_to_clean.append(AGY_CLI_DATA_DIR / "conversations")
                            for c_dir in conv_dirs_to_clean:
                                for sfx in (".db", ".db-wal", ".db-shm"):
                                    try:
                                        (c_dir / f"{bad_cid}{sfx}").unlink(missing_ok=True)
                                    except OSError:
                                        pass
                        yield {"type": "account_session_recovered"}
                        recover_next_attempt = True
                        break

                # Auto-retry up to 2 times (2s, 4s backoff) on transient start-of-turn timeout/network break
                # or upstream 503/529 capacity overload.
                if (
                    (rc != 0 or result_error)
                    and not run_state.cancelled
                    and capacity_retry_attempts < 2
                    and (
                        (
                            not can_use_auto_continue_session
                            and (is_capacity_overload or is_idle_stream_err)
                        )
                        or (can_use_auto_continue_session and is_capacity_overload)
                    )
                ):
                    backoff_sec = (
                        CAPACITY_RETRY_BACKOFF_SECS[0]
                        if capacity_retry_attempts == 0
                        else CAPACITY_RETRY_BACKOFF_SECS[1]
                    )
                    capacity_retry_attempts += 1
                    if not await _sleep_unless_cancelled(backoff_sec, run_state):
                        if active_conv_id:
                            latest_th = await asyncio.to_thread(
                                extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                            )
                            if latest_th:
                                run_state.accumulated_thinking = _merge_thinking(latest_th)
                        cancel_u2 = {"provider": "agy", **final_usage}
                        if active_conv_id:
                            cancel_u2["agy_conversation_id"] = active_conv_id
                        yield {
                            "type": "cancelled",
                            "content": run_state.accumulated_text,
                            "thinking": run_state.accumulated_thinking,
                            "tool_calls": run_state.tool_calls,
                            "usage": cancel_u2,
                        }
                        return

                    if (run_state.accumulated_text or run_state.tool_calls) and active_conv_id:
                        for k_idx in sorted(tool_map.keys()):
                            if str(tool_map[k_idx].get("state") or "").upper() in _NON_TERMINAL_TOOL_STATES:
                                tool_map[k_idx] = {**tool_map[k_idx], "state": "INTERRUPTED"}
                                run_state.tool_calls = [
                                    tool_map[k] for k in sorted(tool_map.keys())
                                ]
                                run_state.seq += 1
                                yield {
                                    "type": "tool_step",
                                    "step": tool_map[k_idx],
                                    "seq": run_state.seq,
                                }
                        latest_th = await asyncio.to_thread(
                            extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                        )
                        if latest_th:
                            merged_th = _merge_thinking(latest_th)
                            if merged_th != run_state.accumulated_thinking:
                                run_state.accumulated_thinking = merged_th
                                run_state.seq += 1
                                yield {
                                    "type": "thinking_update",
                                    "thinking": merged_th,
                                    "seq": run_state.seq,
                                }
                        base_thinking_prefix = run_state.accumulated_thinking
                        needs_bg_wakeup_break = False
                        current_resume_id = active_conv_id
                        current_prompt = _build_auto_continue_prompt(run_state.accumulated_text)
                        continue

                    if not resume_id and active_conv_id:
                        stale_cid = active_conv_id
                        active_conv_id = ""
                        current_resume_id = ""
                        is_isolated_home = bool(active_account) or (
                            ".agy_homes" in Path(active_cli_home).parts
                        )
                        conv_roots = [active_cli_home / ".gemini" / "antigravity-cli" / "conversations"]
                        if not is_isolated_home:
                            conv_roots.append(AGY_CLI_DATA_DIR / "conversations")
                        for ext in (".db", ".db-wal", ".db-shm"):
                            for conv_root in conv_roots:
                                try:
                                    (conv_root / f"{stale_cid}{ext}").unlink(missing_ok=True)
                                except Exception:
                                    pass
                    else:
                        current_resume_id = resume_id
                    current_prompt = effective_prompt
                    continue

                # Adaptive Auto-Continue on transient stream / idle watchdog disconnect:
                # reset consecutive failure counter whenever the subprocess achieved real progress!
                if (
                    (rc != 0 or result_error)
                    and is_idle_stream_err
                    and not run_state.cancelled
                    and can_use_auto_continue_session
                ):
                    had_progress_in_proc = (
                        completed_tools_in_proc > 0 or got_any_text_in_proc
                    )
                    next_consecutive = (
                        0 if had_progress_in_proc else (consecutive_auto_continues + 1)
                    )
                    next_total = total_auto_continues + 1
                    if (
                        next_consecutive <= MAX_CONSECUTIVE_AUTO_CONTINUES
                        and next_total <= MAX_TOTAL_AUTO_CONTINUES
                    ):
                        consecutive_auto_continues = next_consecutive
                        total_auto_continues = next_total
                        if not await _sleep_unless_cancelled(AUTO_CONTINUE_PAUSE_SEC, run_state):
                            if active_conv_id:
                                latest_th = await asyncio.to_thread(
                                    extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                                )
                                if latest_th:
                                    run_state.accumulated_thinking = _merge_thinking(latest_th)
                            yield {
                                "type": "cancelled",
                                "content": run_state.accumulated_text,
                                "thinking": run_state.accumulated_thinking,
                                "tool_calls": run_state.tool_calls,
                            }
                            return

                        for k_idx in sorted(tool_map.keys()):
                            if str(tool_map[k_idx].get("state") or "").upper() in _NON_TERMINAL_TOOL_STATES:
                                tool_map[k_idx] = {**tool_map[k_idx], "state": "INTERRUPTED"}
                                run_state.tool_calls = [
                                    tool_map[k] for k in sorted(tool_map.keys())
                                ]
                                run_state.seq += 1
                                yield {
                                    "type": "tool_step",
                                    "step": tool_map[k_idx],
                                    "seq": run_state.seq,
                                }

                        latest_th = await asyncio.to_thread(
                            extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                        )
                        if latest_th:
                            merged_th = _merge_thinking(latest_th)
                            if merged_th != run_state.accumulated_thinking:
                                run_state.accumulated_thinking = merged_th
                                run_state.seq += 1
                                yield {
                                    "type": "thinking_update",
                                    "thinking": merged_th,
                                    "seq": run_state.seq,
                                }
                        base_thinking_prefix = run_state.accumulated_thinking
                        needs_bg_wakeup_break = False
                        current_resume_id = active_conv_id if (run_state.accumulated_text or run_state.tool_calls) else resume_id
                        current_prompt = _build_auto_continue_prompt(
                            run_state.accumulated_text,
                            stalled_without_progress=(not had_progress_in_proc),
                        ) if (run_state.accumulated_text or run_state.tool_calls) else effective_prompt
                        continue

                if rc != 0 or result_error:
                    for k_idx in sorted(tool_map.keys()):
                        if str(tool_map[k_idx].get("state") or "").upper() in _NON_TERMINAL_TOOL_STATES:
                            tool_map[k_idx] = {**tool_map[k_idx], "state": "INTERRUPTED"}
                            run_state.tool_calls = [
                                tool_map[k] for k in sorted(tool_map.keys())
                            ]
                    if active_conv_id:
                        latest_th = await asyncio.to_thread(
                            extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                        )
                        if latest_th:
                            merged_th = _merge_thinking(latest_th)
                            if merged_th != run_state.accumulated_thinking:
                                run_state.accumulated_thinking = merged_th
                                run_state.seq += 1
                                yield {
                                    "type": "thinking_update",
                                    "thinking": merged_th,
                                    "seq": run_state.seq,
                                }
                    err_msg = result_error or stderr_text or f"Код выхода agy.exe: {rc}"
                    if log_status:
                        err_msg += f"; CLI log: {log_status}"
                    out_err_usage = {"provider": "agy", **final_usage}
                    if active_conv_id:
                        out_err_usage["agy_conversation_id"] = active_conv_id
                    yield {
                        "type": "error",
                        "error": err_msg,
                        "content": run_state.accumulated_text,
                        "thinking": run_state.accumulated_thinking,
                        "tool_calls": run_state.tool_calls,
                        "usage": out_err_usage,
                    }
                    return

                proc_tools = [
                    tool_map[idx]
                    for idx in sorted(set(proc_step_remap.values()))
                    if idx in tool_map
                ]
                candidate_text = run_state.accumulated_text or final_response_fallback
                if (
                    active_conv_id
                    and parked_resume_attempts < MAX_PARKED_TURN_RESUMES
                    and total_auto_continues < MAX_TOTAL_AUTO_CONTINUES
                    and is_parked_async_turn(
                        candidate_text,
                        proc_tools,
                        waiting_for_bg_task=waiting_for_bg_task,
                    )
                ):
                    parked_resume_attempts += 1
                    total_auto_continues += 1
                    if not got_any_text_in_proc and final_response_fallback:
                        fb_delta = (
                            "\n\n" + final_response_fallback
                            if (
                                run_state.accumulated_text
                                and not run_state.accumulated_text.endswith("\n")
                            )
                            else final_response_fallback
                        )
                        if run_state.thinking_active:
                            run_state.thinking_active = False
                            run_state.seq += 1
                            yield {
                                "type": "thinking_status",
                                "active": False,
                                "seq": run_state.seq,
                            }
                        run_state.accumulated_text += fb_delta
                        run_state.seq += 1
                        yield {"type": "text_delta", "delta": fb_delta, "seq": run_state.seq}

                    for k_idx in sorted(tool_map.keys()):
                        if (
                            str(tool_map[k_idx].get("state") or "").upper()
                            in _NON_TERMINAL_TOOL_STATES
                        ):
                            tool_map[k_idx] = {**tool_map[k_idx], "state": "INTERRUPTED"}
                            run_state.tool_calls = [
                                tool_map[k] for k in sorted(tool_map.keys())
                            ]
                            run_state.seq += 1
                            yield {
                                "type": "tool_step",
                                "step": tool_map[k_idx],
                                "seq": run_state.seq,
                            }

                    if not await _sleep_unless_cancelled(AUTO_CONTINUE_PAUSE_SEC, run_state):
                        if active_conv_id:
                            latest_th = await asyncio.to_thread(
                                extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                            )
                            if latest_th:
                                run_state.accumulated_thinking = _merge_thinking(latest_th)
                        yield {
                            "type": "cancelled",
                            "content": run_state.accumulated_text,
                            "thinking": run_state.accumulated_thinking,
                            "tool_calls": run_state.tool_calls,
                        }
                        return

                    latest_th = await asyncio.to_thread(
                        extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                    )
                    if latest_th:
                        merged_th = _merge_thinking(latest_th)
                        if merged_th != run_state.accumulated_thinking:
                            run_state.accumulated_thinking = merged_th
                            run_state.seq += 1
                            yield {
                                "type": "thinking_update",
                                "thinking": merged_th,
                                "seq": run_state.seq,
                            }
                    base_thinking_prefix = run_state.accumulated_thinking
                    needs_bg_wakeup_break = True
                    current_resume_id = active_conv_id
                    current_prompt = _build_parked_turn_resume_prompt(
                        run_state.accumulated_text,
                        execution_contour=exec_contour,
                    )
                    continue

                if (
                    rc == 0
                    and not result_error
                    and not run_state.cancelled
                    and not (run_state.accumulated_text or final_response_fallback).strip()
                    and not run_state.accumulated_thinking.strip()
                    and active_conv_id
                ):
                    latest_th = await asyncio.to_thread(
                        extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                    )
                    if latest_th:
                        merged_th = _merge_thinking(latest_th)
                        if merged_th != run_state.accumulated_thinking:
                            run_state.accumulated_thinking = merged_th
                            run_state.seq += 1
                            yield {
                                "type": "thinking_update",
                                "thinking": merged_th,
                                "seq": run_state.seq,
                            }

                if (
                    rc == 0
                    and not result_error
                    and not run_state.cancelled
                    and not (run_state.accumulated_text or final_response_fallback).strip()
                    and (
                        len(proc_tools) > 0
                        or bool(run_state.tool_calls)
                        or bool(run_state.accumulated_thinking.strip())
                    )
                    and active_conv_id
                    and empty_turn_resumes < MAX_EMPTY_TURN_RESUMES
                    and total_auto_continues < MAX_TOTAL_AUTO_CONTINUES
                ):
                    empty_turn_resumes += 1
                    total_auto_continues += 1
                    _logger.warning(
                        "Silent empty turn detected after tools/thinking (chat=%s, conv=%s, tools=%d, resume=%d/%d) — auto-resuming.",
                        chat_id,
                        active_conv_id,
                        len(proc_tools),
                        empty_turn_resumes,
                        MAX_EMPTY_TURN_RESUMES,
                    )
                    for k_idx in sorted(tool_map.keys()):
                        if (
                            str(tool_map[k_idx].get("state") or "").upper()
                            in _NON_TERMINAL_TOOL_STATES
                        ):
                            tool_map[k_idx] = {**tool_map[k_idx], "state": "INTERRUPTED"}
                            run_state.tool_calls = [
                                tool_map[k] for k in sorted(tool_map.keys())
                            ]
                            run_state.seq += 1
                            yield {
                                "type": "tool_step",
                                "step": tool_map[k_idx],
                                "seq": run_state.seq,
                            }

                    if not await _sleep_unless_cancelled(AUTO_CONTINUE_PAUSE_SEC, run_state):
                        if active_conv_id:
                            latest_th = await asyncio.to_thread(
                                extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                            )
                            if latest_th:
                                run_state.accumulated_thinking = _merge_thinking(latest_th)
                        cancel_empty_usage = {"provider": "agy", **final_usage}
                        if active_conv_id:
                            cancel_empty_usage["agy_conversation_id"] = active_conv_id
                        yield {
                            "type": "cancelled",
                            "content": run_state.accumulated_text,
                            "thinking": run_state.accumulated_thinking,
                            "tool_calls": run_state.tool_calls,
                            "usage": cancel_empty_usage,
                        }
                        return

                    latest_th = await asyncio.to_thread(
                        extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                    )
                    if latest_th:
                        merged_th = _merge_thinking(latest_th)
                        if merged_th != run_state.accumulated_thinking:
                            run_state.accumulated_thinking = merged_th
                            run_state.seq += 1
                            yield {
                                "type": "thinking_update",
                                "thinking": merged_th,
                                "seq": run_state.seq,
                            }
                    base_thinking_prefix = run_state.accumulated_thinking
                    needs_bg_wakeup_break = False
                    current_resume_id = active_conv_id
                    current_prompt = _build_empty_turn_resume_prompt(proc_tools)
                    continue

                # Finalize thinking from transcript.jsonl
                if active_conv_id:
                    latest_th = await asyncio.to_thread(
                        extract_latest_turn_thinking, active_conv_id, active_cli_home / ".gemini" / "antigravity-cli"
                    )
                    if latest_th:
                        merged_th = _merge_thinking(latest_th)
                        run_state.accumulated_thinking = merged_th
                        yield {"type": "thinking_update", "thinking": merged_th}

                if not got_any_text_in_proc and final_response_fallback:
                    if run_state.accumulated_text:
                        run_state.accumulated_text += "\n\n" + final_response_fallback
                    else:
                        run_state.accumulated_text = final_response_fallback

                final_text = run_state.accumulated_text or final_response_fallback
                out_done_usage = {"provider": "agy", **final_usage}
                if active_conv_id:
                    out_done_usage["agy_conversation_id"] = active_conv_id
                if not final_text.strip() and not _has_real_file_write_tools(run_state.tool_calls):
                    yield {
                        "type": "error",
                        "error": "Модель завершила шаг чтения/рассуждений без текстового ответа. Нажмите «Продолжить» или «Повторить».",
                        "content": "",
                        "thinking": run_state.accumulated_thinking,
                        "tool_calls": run_state.tool_calls,
                        "usage": out_done_usage,
                    }
                    return
                yield {
                    "type": "done",
                    "content": final_text,
                    "thinking": run_state.accumulated_thinking,
                    "tool_calls": run_state.tool_calls,
                    "usage": out_done_usage,
                    "agy_conversation_id": active_conv_id,
                    "agy_account_id": active_account["id"] if active_account else "",
                }
                return

            if recover_next_attempt:
                continue
    finally:
        try:
            from .sandbox_service import cancel_chat_sandbox_execs

            await cancel_chat_sandbox_execs(chat_id)
        except Exception:
            pass
        ACTIVE_RUNS.pop(chat_id, None)
        if 'proc' in locals() and hasattr(proc, 'aclose_lease'):
            await proc.aclose_lease()
        for tmp_pf in created_large_prompt_files:
            try:
                tmp_pf.unlink(missing_ok=True)
            except OSError:
                pass
        if created_upload_dir_for_prompt is not None:
            try:
                if created_upload_dir_for_prompt.is_dir() and not any(created_upload_dir_for_prompt.iterdir()):
                    created_upload_dir_for_prompt.rmdir()
            except OSError:
                pass
        if active_account and active_account.get('has_credentials'):
            try:
                await sync_refreshed_agy_credentials_back(active_account['id'])
            except (OSError, ValueError):
                pass


def compute_resume_stitch_prefix(existing_text: str, first_delta: str) -> str:
    """Separate resumed prose/Markdown without changing either source chunk."""
    if not existing_text or not first_delta:
        return ""
    if existing_text[-1].isspace() or first_delta[0].isspace():
        return ""
    if first_delta.startswith("|"):
        return "\n" if existing_text.rstrip().endswith("|") else "\n\n"
    if (
        first_delta.startswith(("#", "```", "~~~", "---", "- ", "* ", "+ ", ">"))
        or re.match(r"\d+[.)]\s", first_delta)
        or existing_text.rstrip().endswith(("```", "~~~"))
    ):
        return "\n\n"
    if first_delta[0] not in ".,;:!?)]}»\"'":
        return " "
    return ""


class AgyClientUnavailableError(RuntimeError):
    """Raised when the local Antigravity CLI binary is missing or unavailable."""


async def compact_chat_context(
    *,
    chat: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    user_id: str = "",
) -> dict[str, Any]:
    """
    Distill the active chat context into a structured, lossless Russian summary via `agy.exe`
    (or personal OpenAI Codex CLI when `model` is `codex:*` or `agy.exe` is absent),
    discarding raw tool-output bloat, internal thinking logs, and conversational noise.
    """
    use_claude_model = str(model or "").strip().lower().startswith("claude-code:")
    use_codex_model = str(model or "").strip().lower().startswith("codex:")
    agy_bin = resolve_agy_bin()
    if not use_codex_model and not use_claude_model and not Path(agy_bin).exists():
        raise AgyClientUnavailableError("Официальный клиент Antigravity CLI недоступен")

    compacted_up_to = int(chat.get("compacted_up_to_msg_id") or 0)
    prev_summary = str(chat.get("compacted_summary") or "").strip()
    active_msgs = [
        m
        for m in messages
        if int(m.get("id") or 0) > compacted_up_to
        and m.get("role") in ("user", "assistant")
        and m.get("status") != "compacted"
        and (strip_transient_turn_wrappers(str(m.get("content") or "")).strip() or m.get("tool_calls"))
    ]
    if not active_msgs:
        raise ValueError("Нет новых сообщений для сжатия контекста")

    transcript_parts: list[str] = []
    if prev_summary:
        transcript_parts.append(
            "=== ПРЕДЫДУЩАЯ СВОДКА КОНТЕКСТА (обязательно сохрани важные факты из неё) ===\n"
            + prev_summary
        )

    transcript_parts.append("=== НОВЫЕ СООБЩЕНИЯ ДИАЛОГА ДЛЯ СЖАТИЯ ===")
    for idx, m in enumerate(active_msgs, start=1):
        role_title = "ПОЛЬЗОВАТЕЛЬ" if m.get("role") == "user" else "АССИСТЕНТ"
        tcs = m.get("tool_calls") or []
        tool_brief = ""
        if isinstance(tcs, list) and tcs:
            names: list[str] = []
            for tc in tcs[:12]:
                if not isinstance(tc, dict):
                    continue
                tname = str(tc.get("name") or "tool")
                params = tc.get("parameters") or {}
                hint = (
                    params.get("Path")
                    or params.get("CommandLine")
                    or params.get("query")
                    or ""
                )
                short_h = str(hint).strip().replace("\n", " ")[:70]
                names.append(f"{tname}({short_h})" if short_h else tname)
            if names:
                tool_brief = f"\n[Выполненные инструменты: {', '.join(names)}]"

        txt = strip_transient_turn_wrappers(str(m.get("content") or "")).strip()
        # Keep newer turns more detailed if a single turn has a huge raw log paste
        per_msg_cap = 9000 if idx > len(active_msgs) - 4 else 4500
        if len(txt) > per_msg_cap:
            head = txt[: int(per_msg_cap * 0.65)]
            tail = txt[-int(per_msg_cap * 0.30) :]
            txt = f"{head}\n...[пропущена середина длинного вывода]...\n{tail}"

        transcript_parts.append(f"\n--- [{idx}] {role_title} ---{tool_brief}\n{txt}")

    full_transcript = "\n".join(transcript_parts)

    system_instructions = (
        "Ты — модуль интеллектуального анти-галлюцинационного сжатия контекста Antigravity Hub.\n"
        "Твоя задача: сжать приведённую историю диалога в плотную, структурированную сводку на РУССКОМ языке "
        "БЕЗ ПОТЕРИ СМЫСЛА И ТЕХНИЧЕСКИХ ДЕТАЛЕЙ, удалив словесный мусор, воду, повторы и промежуточные тупиковые рассуждения.\n\n"
        "СТРОГИЕ ПРАВИЛА:\n"
        "1. Сохрани 100% точных технических реквизитов: точные пути к файлам, имена функций/классов/переменных, номера строк, "
        "конкретные цифры/расчёты/формулы, IP/домены/порты, названия моделей/железа/библиотек, выполненные команды и ограничения пользователя.\n"
        "2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдавать предположения, планы или идеи из обсуждения за уже выполненные правки! "
        "Строго разделяй проверенные на диске/в логе инструментов действия и неподтверждённые догадки.\n"
        "3. Полностью выброси приветствия, общие фразы, повторные объяснения и устаревшие черновые варианты, от которых отказались.\n"
        "4. Не используй никаких инструментов — сразу выдай только готовую сводку в Markdown строго по 5 компактным разделам:\n"
        "   ### 1. Цель и задача\n"
        "   ### 2. Подтверждённые факты и выполненные изменения\n"
        "   ### 3. Непроверенные гипотезы / Открытые вопросы\n"
        "   ### 4. Точные пути к файлам, функции, строки и параметры\n"
        "   ### 5. Текущий статус и следующие шаги\n"
        "5. Объём сводки: максимально ёмко (обычно 250–900 слов в зависимости от насыщенности диалога), чёткими маркированными списками.\n\n"
    )

    extra_upload_dir: Optional[Path] = None
    temp_input_file: Optional[Path] = None
    full_prompt = f"{system_instructions}{full_transcript}"

    if use_claude_model:
        from . import claude_engine

        eff_uid_c = str(user_id or chat.get("owner_id") or "admin").strip() or "admin"
        res_cl = await claude_engine.run_one_shot_claude_prompt(
            eff_uid_c,
            full_prompt,
            model_id=model if claude_engine.is_claude_code_model(model) else "",
            timeout_sec=120.0,
        )
        summary_cl = str(res_cl.get("text") or "").strip()
        if not summary_cl:
            raise RuntimeError("Модель Claude вернула пустую сводку контекста")
        return {"summary": summary_cl, "usage": res_cl.get("usage") or {}, "model": str(res_cl.get("model") or model)}

    if use_codex_model:
        from . import codex_engine

        eff_uid = str(user_id or chat.get("owner_id") or "admin").strip() or "admin"
        eff_model = (
            model
            if codex_engine.is_codex_model(model)
            else "codex:gpt-5.4-mini@low"
        )
        res_codex = await codex_engine.run_one_shot_codex_prompt(
            user_id=eff_uid,
            prompt=full_prompt,
            model_id=eff_model,
            timeout_sec=120.0,
        )
        summary_out = str(res_codex.get("text") or "").strip()
        if not summary_out:
            raise RuntimeError("Модель ChatGPT вернула пустую сводку контекста")
        return {
            "summary": summary_out,
            "usage": res_codex.get("usage") or {},
            "model": str(res_codex.get("model") or eff_model),
        }

    if len(full_prompt) > 22000:
        extra_upload_dir = config.resolve_chat_uploads_dir(chat)
        extra_upload_dir.mkdir(parents=True, exist_ok=True)
        temp_input_file = extra_upload_dir / "_compact_context_source.md"
        temp_input_file.write_text(full_transcript, encoding="utf-8")
        up_posix = extra_upload_dir.resolve().as_posix().rstrip("/")
        settings = {
            "allowNonWorkspaceAccess": False,
            "permissions": {
                "allow": [f"read_file({up_posix}/*)"],
                "deny": _dedupe_patterns(
                    [
                        *MCP_DENY_PATTERNS,
                        "write_file(*)",
                        "command(*)",
                        "read_url(*)",
                        *HEADLESS_ASYNC_DENY_PATTERNS,
                    ]
                ),
            },
        }
        effective_prompt = (
            f"{system_instructions}"
            f"Полная стенограмма диалога сохранена в файле `{temp_input_file}`. "
            "Прочитай его через `read_file` и сразу выведи итоговую сжатую сводку по 4 разделам."
        )
    else:
        settings = {
            "allowNonWorkspaceAccess": False,
            "permissions": {
                "allow": [],
                "deny": _dedupe_patterns(
                    [
                        *MCP_DENY_PATTERNS,
                        "read_file(*)",
                        "write_file(*)",
                        "command(*)",
                        "read_url(*)",
                        *HEADLESS_ASYNC_DENY_PATTERNS,
                    ]
                ),
            },
        }
        effective_prompt = full_prompt

    cmd = [
        agy_bin,
        "-p",
        effective_prompt,
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--disable-slash-commands",
        "--settings",
        json.dumps(settings),
    ]
    if extra_upload_dir is not None:
        cmd.extend(["--add-dir", str(extra_upload_dir)])

    from . import database
    resolved_compact_acc = await resolve_agy_account_for_chat_turn(
        model,
        preferred_account_id=str(chat.get("preferred_agy_account_id") or ""),
        current_chat_account_id=str(chat.get("agy_account_id") or ""),
    )
    auxiliary_account = resolved_compact_acc["account"]
    compact_proxy_url = await proxy_router.ensure_proxy_for_context(
        "agy",
        account_id=auxiliary_account["id"] if auxiliary_account else "",
    )
    compact_cwd = await asyncio.to_thread(
        config.resolve_user_chat_sandbox_dir,
        chat.get("user_id") or "admin",
        str(chat["id"]),
        create=True,
    )
    sys_cli_home = await materialize_agy_account_home(auxiliary_account, "_system")
    compact_env = _build_subprocess_env(
        isolated_home_override=sys_cli_home,
        proxy_url=compact_proxy_url,
    )
    tmp_conv_id = ""
    accumulated_text = ""
    fallback_response = ""
    result_error: Optional[str] = None
    usage_data: dict[str, Any] = {}

    try:
        proc = await create_agy_subprocess_exec(
            *cmd,
            cwd=str(compact_cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=compact_env,
            start_new_session=(os.name != "nt"),
            limit=4 * 1024 * 1024,
            **subprocess_hidden_kwargs(),
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        agent_done = False

        async def _consume() -> tuple[int, str]:
            nonlocal tmp_conv_id, accumulated_text, fallback_response, result_error, usage_data, agent_done
            got_res = False
            async for raw_line in proc.stdout:  # type: ignore[union-attr]
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                etype = evt.get("event")
                if etype == "init":
                    tmp_conv_id = evt.get("conversation_id") or tmp_conv_id
                elif etype == "step_update":
                    su = evt.get("step_update") or {}
                    tmp_conv_id = su.get("conversation_id") or tmp_conv_id
                    if su.get("step_type") == "tool":
                        agent_done = False
                    elif su.get("step_type") == "agent_response":
                        td = su.get("text_delta") or ""
                        if td:
                            accumulated_text += td
                        if su.get("state") == "DONE" and accumulated_text.strip():
                            agent_done = True
                elif etype == "result":
                    res = evt.get("result") or {}
                    usage_data = res.get("usage") or {}
                    fallback_response = res.get("response") or ""
                    if res.get("status") == "ERROR":
                        result_error = res.get("error") or "Ошибка генерации сводки"
                    got_res = True
                    break

            async def _drain() -> tuple[bytes, int]:
                sb = await proc.stderr.read()  # type: ignore[union-attr]
                ec = await proc.wait()
                return sb, ec

            try:
                stderr_b, rc_val = await asyncio.wait_for(_drain(), timeout=3.5)
                stderr_s = stderr_b.decode("utf-8", errors="replace").strip()
            except asyncio.TimeoutError:
                stderr_s = ""
                await _terminate_hung_cli_proc(proc)
                rc_val = 0 if (got_res and not result_error) else -9
            else:
                if got_res and not result_error and rc_val != 0:
                    rc_val = 0
            return rc_val, stderr_s

        rc, stderr_txt = await asyncio.wait_for(_consume(), timeout=150.0)
        if (rc != 0 or result_error) and agent_done and accumulated_text.strip():
            err_combo = f"{result_error or ''} {stderr_txt}"
            err_low = err_combo.lower()
            if (
                "stream was interrupted" in err_low
                or "please continue the task" in err_low
                or is_transient_stream_error(err_combo)
                or is_transient_capacity_error(err_combo)
            ):
                rc = 0
                result_error = None
        if rc != 0 or result_error:
            raise RuntimeError(result_error or stderr_txt or f"Код выхода agy.exe: {rc}")

        summary_out = (accumulated_text or fallback_response).strip()
        if not summary_out:
            raise RuntimeError("Модель вернула пустую сводку контекста")

        return {
            "summary": summary_out,
            "usage": usage_data,
            "model": model,
        }
    finally:
        if 'proc' in locals() and hasattr(proc, "aclose_lease"):
            await proc.aclose_lease()
        if auxiliary_account and auxiliary_account.get("has_credentials"):
            await sync_refreshed_agy_credentials_back(auxiliary_account["id"])
        if temp_input_file is not None:
            try:
                temp_input_file.unlink(missing_ok=True)
            except Exception:
                pass
        if tmp_conv_id:
            for ext in (".db", ".db-wal", ".db-shm"):
                for conv_root in (
                    AGY_CLI_DATA_DIR / "conversations",
                    sys_cli_home / ".gemini" / "antigravity-cli" / "conversations",
                ):
                    try:
                        (conv_root / f"{tmp_conv_id}{ext}").unlink(missing_ok=True)
                    except Exception:
                        pass


_PASSPORT_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "data",
        ".agents",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "coverage",
        "logs",
        "tmp",
        "temp",
    }
)


def _collect_project_disk_snapshot(project_dir: Path) -> str:
    """Fast pure-Python 2-level directory & manifest snapshot (0 external processes)."""
    lines: list[str] = [f"Папка проекта: {project_dir.name}"]
    tree_lines: list[str] = []
    try:
        top_entries = sorted(
            list(project_dir.iterdir()),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except Exception:
        top_entries = []

    for entry in top_entries[:50]:
        name = entry.name
        if name.lower() in _PASSPORT_SKIP_DIRS:
            continue
        if entry.is_dir():
            tree_lines.append(f"📁 {name}/")
            try:
                sub_entries = sorted(
                    list(entry.iterdir()),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except Exception:
                sub_entries = []
            shown = 0
            for sub in sub_entries:
                if sub.name.lower() in _PASSPORT_SKIP_DIRS:
                    continue
                prefix = "  📁 " if sub.is_dir() else "  📄 "
                suffix = "/" if sub.is_dir() else ""
                tree_lines.append(f"{prefix}{name}/{sub.name}{suffix}")
                shown += 1
                if shown >= 22:
                    if len(sub_entries) > shown:
                        tree_lines.append(f"  … (+ещё {len(sub_entries) - shown})")
                    break
        else:
            tree_lines.append(f"📄 {name}")
        if len(tree_lines) >= 120:
            tree_lines.append("…")
            break

    if tree_lines:
        lines.append("\n### Структура верхних 2 уровней:")
        lines.append("\n".join(tree_lines))

    manifest_candidates: list[Path] = []
    seen_names: set[str] = set()
    try:
        for p in sorted(project_dir.glob("README*")):
            if p.is_file() and p.name.lower() not in seen_names:
                seen_names.add(p.name.lower())
                manifest_candidates.append(p)
    except Exception:
        pass

    for fname in (
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Dockerfile",
        "Dockerfile.sandbox",
        "docker-compose.yml",
        ".env.example",
    ):
        cand = project_dir / fname
        if cand.is_file() and fname.lower() not in seen_names:
            seen_names.add(fname.lower())
            manifest_candidates.append(cand)

    total_snippet_chars = 0
    for mf in manifest_candidates[:6]:
        if total_snippet_chars >= 6000:
            break
        try:
            raw = mf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        head_lines = raw.splitlines()[:40]
        snippet = "\n".join(head_lines).strip()[:1450]
        if not snippet:
            continue
        total_snippet_chars += len(snippet)
        lines.append(f"\n### Файл `{mf.name}` (начало):\n{snippet}")

    return "\n".join(lines)[:11500]


def _pick_fast_passport_model(preferred_model: str = "") -> str:
    available = [str(m.get("id") or "") for m in get_cached_models() if m.get("id")]
    avail_set = set(available)
    for fast_id in (
        "gemini-4-carbon-low",
        "gemini-3.8-flash-low",
        "gemini-4-argon-low",
        "gemini-4-carbon-medium",
        "gemini-3.8-flash-medium",
    ):
        if fast_id in avail_set:
            return fast_id
    if preferred_model and preferred_model in avail_set:
        return preferred_model
    return available[0] if available else "gemini-4-carbon-low"


def _parse_json_object_loose(raw_text: str) -> dict[str, Any]:
    txt = (raw_text or "").strip()
    if not txt:
        raise ValueError("Пустой ответ от модели")
    # Strip markdown code fences if present
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", txt, re.IGNORECASE)
    if fenced:
        txt = fenced.group(1).strip()
    else:
        first_brace = txt.find("{")
        last_brace = txt.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            txt = txt[first_brace : last_brace + 1]
    parsed = json.loads(txt)
    if not isinstance(parsed, dict):
        raise ValueError("Ответ модели не является JSON-объектом")
    return parsed


async def generate_project_passport_via_cli(
    *,
    project_name: str,
    project_dir: Path,
    recent_chats: list[dict[str, Any]],
    need_starter_changelog: bool = False,
    preferred_model: str = "",
    user_id: str = "",
) -> dict[str, Any]:
    """
    Scan an existing or new project folder (pure Python 2-level snapshot + recent chat
    summaries) and perform a single fast read-only `agy.exe` (or personal `codex app-server`)
    call returning structured JSON for `passport_overview`, `passport_rules`, `passport_file_map`,
    and optional `starter_changelog`.
    """
    use_claude_passport = str(preferred_model or "").strip().lower().startswith("claude-code:")
    use_codex_passport = use_claude_passport or str(preferred_model or "").strip().lower().startswith("codex:")
    agy_bin = resolve_agy_bin()
    if not use_codex_passport and not Path(agy_bin).exists():
        if user_id:
            try:
                from . import database as _db_pass

                if await _db_pass.get_active_claude_account(user_id, require_enabled=True):
                    use_claude_passport = True
                    use_codex_passport = True
                else:
                    use_codex_passport = True
            except Exception:
                use_codex_passport = True
        else:
            raise RuntimeError(f"Не найден официальный клиент Antigravity CLI: {agy_bin}")

    disk_snapshot = await asyncio.to_thread(_collect_project_disk_snapshot, project_dir)

    chat_blocks: list[str] = []
    for idx, ch in enumerate(recent_chats[:5], start=1):
        c_title = str(ch.get("title") or "Чат")
        c_mode = str(ch.get("access_mode") or "read_only")
        c_sum = str(ch.get("compacted_summary") or "").strip()[:1200]
        c_tail = str(ch.get("tail_excerpt") or "").strip()[:550]
        body = c_sum or c_tail or "(без сводки)"
        chat_blocks.append(f"{idx}. Вкладка «{c_title}» ({c_mode}):\n{body}")

    chats_section = (
        "\n\n".join(chat_blocks)
        if chat_blocks
        else "(История чатов проекта в базе пока отсутствует — опирайся на структуру файлов.)"
    )

    changelog_instruction = (
        'Также заполни массив `"starter_changelog"` (от 1 до 3 кратких объектов '
        '`{"summary": "до 140 симв.", "chat_title": "название вкладки", "files": []}` '
        "по реальным выводам из истории чатов; если истории чатов нет — верни `[]`)."
        if need_starter_changelog
        else 'Поле `"starter_changelog"` верни пустым массивом `[]`.'
    )

    prompt = (
        f"Ты составляешь ультра-компактный «Паспорт и правила проекта» для `{project_name}`.\n"
        "НЕ вызывай никаких инструментов — все данные уже собраны ниже.\n"
        "Верни СТРОГО валидный JSON-объект (без обёрток и вступлений) следующей структуры:\n"
        "{\n"
        '  "passport_overview": "Суть проекта, основной стек, архитектура и как запускается (маркированный список на русском, 6–12 строк, до 1600 симв.)",\n'
        '  "passport_rules": "Жёсткие технические правила, ограничения, запреты и договорённости из кода и прошлых чатов (маркированный список, 5–12 строк, до 1800 симв.)",\n'
        '  "passport_file_map": "Карта ключевых папок, точек входа и модулей проекта с кратким назначением (маркированный список, 8–16 строк, до 2000 симв.)",\n'
        '  "starter_changelog": []\n'
        "}\n"
        f"{changelog_instruction}\n"
        "Суммарный объём всех трёх текстовых полей должен быть лаконичным (~30–45 строк тезисно, без воды).\n\n"
        f"=== СЛЕПОК ФАЙЛОВ ПРОЕКТА ===\n{disk_snapshot}\n\n"
        f"=== ИСТОРИЯ ПОСЛЕДНИХ ЧАТОВ ПРОЕКТА ===\n{chats_section}\n"
    )

    if use_codex_passport:
        from . import codex_engine

        eff_uid = str(user_id or "admin").strip() or "admin"
        eff_model = (
            preferred_model
            if codex_engine.is_codex_model(preferred_model)
            else "codex:gpt-5.4-mini@low"
        )
        from . import claude_engine as _claude_eng

        res_codex = (
            await codex_engine.run_one_shot_codex_prompt(
                user_id=eff_uid,
                prompt=prompt,
                model_id=eff_model,
                timeout_sec=60.0,
            )
            if not use_claude_passport
            else await _claude_eng.run_one_shot_claude_prompt(
                eff_uid, prompt, model_id=preferred_model, timeout_sec=90.0
            )
        )
        parsed = _parse_json_object_loose(str(res_codex.get("text") or ""))

        def _norm_c_field(val: Any, cap: int) -> str:
            if isinstance(val, list):
                return "\n".join(str(x).strip() for x in val if str(x).strip())[:cap]
            return str(val or "").strip()[:cap]

        starter_cl_c: list[dict[str, Any]] = []
        raw_cl_c = parsed.get("starter_changelog")
        if need_starter_changelog and isinstance(raw_cl_c, list):
            now_iso_c = datetime.now(timezone.utc).isoformat()
            for item in raw_cl_c[:3]:
                if isinstance(item, dict) and str(item.get("summary") or "").strip():
                    starter_cl_c.append(
                        {
                            "ts": now_iso_c,
                            "chat_id": "",
                            "chat_title": str(item.get("chat_title") or "История чатов")[:80],
                            "files": [
                                str(f)[:80]
                                for f in (item.get("files") or [])
                                if isinstance(f, str) and f.strip()
                            ][:4],
                            "summary": str(item.get("summary") or "").strip()[:160],
                        }
                    )
        return {
            "passport_overview": _norm_c_field(parsed.get("passport_overview"), 2500),
            "passport_rules": _norm_c_field(parsed.get("passport_rules"), 3000),
            "passport_file_map": _norm_c_field(parsed.get("passport_file_map"), 3000),
            "starter_changelog": starter_cl_c,
            "model": str(res_codex.get("model") or eff_model),
        }

    model_id = _pick_fast_passport_model(preferred_model)
    settings = {
        "allowNonWorkspaceAccess": False,
        "permissions": {
            "allow": [],
            "deny": _dedupe_patterns(
                [
                    *MCP_DENY_PATTERNS,
                    "read_file(*)",
                    "write_file(*)",
                    "command(*)",
                    "read_url(*)",
                    *HEADLESS_ASYNC_DENY_PATTERNS,
                ]
            ),
        },
    }
    cmd = [
        agy_bin,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--model",
        model_id,
        "--disable-slash-commands",
        "--settings",
        json.dumps(settings),
    ]

    passport_proxy_url = await proxy_router.ensure_proxy_for_context("agy")
    CHAT_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    from . import database
    auxiliary_account = await database.get_active_account_profile()
    sys_cli_home = await materialize_agy_account_home(auxiliary_account, "_system")
    sub_env = _build_subprocess_env(
        isolated_home_override=sys_cli_home,
        proxy_url=passport_proxy_url,
    )
    tmp_conv_id = ""
    accumulated_text = ""
    fallback_response = ""
    result_error: Optional[str] = None
    proc: Optional[asyncio.subprocess.Process] = None

    try:
        proc = await create_agy_subprocess_exec(
            *cmd,
            cwd=str(CHAT_SANDBOX_DIR),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sub_env,
            start_new_session=(os.name != "nt"),
            limit=4 * 1024 * 1024,
            **subprocess_hidden_kwargs(),
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        async def _consume_passport() -> tuple[int, str]:
            nonlocal tmp_conv_id, accumulated_text, fallback_response, result_error
            got_res = False
            async for raw_line in proc.stdout:  # type: ignore[union-attr]
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                etype = evt.get("event")
                if etype == "init":
                    tmp_conv_id = evt.get("conversation_id") or tmp_conv_id
                elif etype == "step_update":
                    su = evt.get("step_update") or {}
                    tmp_conv_id = su.get("conversation_id") or tmp_conv_id
                    if su.get("step_type") == "agent_response":
                        td = su.get("text_delta") or ""
                        if td:
                            accumulated_text += td
                elif etype == "result":
                    res = evt.get("result") or {}
                    fallback_response = res.get("response") or ""
                    if res.get("status") == "ERROR":
                        result_error = res.get("error") or "Ошибка автосборки паспорта"
                    got_res = True
                    break

            async def _drain() -> tuple[bytes, int]:
                sb = await proc.stderr.read()  # type: ignore[union-attr]
                ec = await proc.wait()
                return sb, ec

            try:
                stderr_b, rc_val = await asyncio.wait_for(_drain(), timeout=3.5)
                stderr_s = stderr_b.decode("utf-8", errors="replace").strip()
            except asyncio.TimeoutError:
                stderr_s = ""
                await _terminate_hung_cli_proc(proc)
                rc_val = 0 if (got_res and not result_error) else -9
            else:
                if got_res and not result_error and rc_val != 0:
                    rc_val = 0
            return rc_val, stderr_s

        try:
            rc, stderr_txt = await asyncio.wait_for(_consume_passport(), timeout=45.0)
        except asyncio.TimeoutError as exc:
            await _terminate_hung_cli_proc(proc)
            raise RuntimeError("Превышен таймаут автосборки паспорта проекта (45 с)") from exc

        if rc != 0 or result_error:
            raise RuntimeError(result_error or stderr_txt or f"Код выхода agy.exe: {rc}")

        raw_out = (accumulated_text or fallback_response).strip()
        parsed = _parse_json_object_loose(raw_out)

        def _norm_field(val: Any, cap: int) -> str:
            if isinstance(val, list):
                return "\n".join(str(x).strip() for x in val if str(x).strip())[:cap]
            return str(val or "").strip()[:cap]

        starter_cl: list[dict[str, Any]] = []
        raw_cl = parsed.get("starter_changelog")
        if need_starter_changelog and isinstance(raw_cl, list):
            now_iso = datetime.now(timezone.utc).isoformat()
            for item in raw_cl[:3]:
                if isinstance(item, dict) and str(item.get("summary") or "").strip():
                    starter_cl.append(
                        {
                            "ts": now_iso,
                            "chat_id": "",
                            "chat_title": str(item.get("chat_title") or "История чатов")[:80],
                            "files": [
                                str(f)[:80]
                                for f in (item.get("files") or [])
                                if isinstance(f, str) and f.strip()
                            ][:4],
                            "summary": str(item.get("summary") or "").strip()[:160],
                        }
                    )

        return {
            "passport_overview": _norm_field(parsed.get("passport_overview"), 2500),
            "passport_rules": _norm_field(parsed.get("passport_rules"), 3000),
            "passport_file_map": _norm_field(parsed.get("passport_file_map"), 3000),
            "starter_changelog": starter_cl,
            "model": model_id,
        }
    finally:
        if proc is not None and proc.returncode is None:
            await _terminate_hung_cli_proc(proc)
        if hasattr(proc, "aclose_lease"):
            await proc.aclose_lease()
        if auxiliary_account and auxiliary_account.get("has_credentials"):
            await sync_refreshed_agy_credentials_back(auxiliary_account["id"])
        if tmp_conv_id:
            for ext in (".db", ".db-wal", ".db-shm"):
                for conv_root in (
                    AGY_CLI_DATA_DIR / "conversations",
                    sys_cli_home / ".gemini" / "antigravity-cli" / "conversations",
                ):
                    try:
                        (conv_root / f"{tmp_conv_id}{ext}").unlink(missing_ok=True)
                    except Exception:
                        pass


async def probe_unknown_models_passport_via_cli(
    unknown_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Perform a single low-priority (`telemetry`) headless CLI probe with `search_web` allowed
    to calibrate unknown LLM series against ZeroHub reference capability/burn anchors.
    """
    from .agy_credentials import TelemetryDeferred

    if not unknown_items:
        return {"series": {}}
    if ACTIVE_RUNS:
        raise TelemetryDeferred()

    agy_bin = resolve_agy_bin()
    if not Path(agy_bin).exists():
        raise RuntimeError(f"Не найден официальный клиент Antigravity CLI: {agy_bin}")

    compact_items = [
        {
            "series_key": str(it.get("series_key") or ""),
            "display_name": str(it.get("display_name") or ""),
            "vendor": str(it.get("vendor") or ""),
            "efforts": list(it.get("efforts") or []),
            "context_window": int(it.get("context_window") or 0),
            "description": str(it.get("description") or "")[:200],
            "openrouter_hint": it.get("openrouter_hint"),
        }
        for it in unknown_items[:12]
    ]

    prompt = (
        "Ты системный калибратор Советника моделей ZeroHub. Оцени следующие новые серии ИИ-моделей "
        "относительно нашей шкалы эталонов (при необходимости используй search_web по свежим релиз-ноутам/бенчмаркам):\n"
        "- GPT-6 Astra (XHigh): capability=97, burn_class=5, burn_score=5.0\n"
        "- Claude Opus (High): capability=95, burn_class=4, burn_score=4.3\n"
        "- Gemini 4 Neon (High): capability=93, burn_class=2, burn_score=1.65 (кодерский sweet-spot)\n"
        "- Gemini 4 Carbon (High): capability=92, burn_class=2, burn_score=2.4\n"
        "- Gemini 4 Argon (High): capability=86, burn_class=1, burn_score=1.25\n"
        "- Gemini Flash: capability=74, burn_class=1, burn_score=1.0\n\n"
        f"Список серий для паспортизации:\n{json.dumps(compact_items, ensure_ascii=False)}\n\n"
        "Верни СТРОГО один JSON-объект без лишнего текста:\n"
        '{"series": {"<series_key>": {"tiers": {"low": {"capability": 85, "burn_class": 1, "burn_score": 1.0}, '
        '"medium": {"capability": 89, "burn_class": 1, "burn_score": 1.3}, '
        '"high": {"capability": 92, "burn_class": 2, "burn_score": 2.0}, '
        '"xhigh": {"capability": 94, "burn_class": 3, "burn_score": 3.2}}, '
        '"coding": 0.90, "debug": 0.88, "ui": 0.85, "docs": 0.82, '
        '"role_ru": "Краткая роль на русском до 70 символов"}}}'
    )

    model_id = _pick_fast_passport_model()
    settings = {
        "allowNonWorkspaceAccess": False,
        "permissions": {
            "allow": ["search_web", "search_web(*)"],
            "deny": _dedupe_patterns(
                [
                    *MCP_DENY_PATTERNS,
                    "read_file(*)",
                    "write_file(*)",
                    "edit_file(*)",
                    "command(*)",
                    "run_command(*)",
                    *HEADLESS_ASYNC_DENY_PATTERNS,
                ]
            ),
        },
    }
    cmd = [
        agy_bin,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--model",
        model_id,
        "--disable-slash-commands",
        "--settings",
        json.dumps(settings),
    ]

    probe_proxy_url = await proxy_router.ensure_proxy_for_context("agy")
    CHAT_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    from . import database

    auxiliary_account = await database.get_active_account_profile()
    sys_cli_home = await materialize_agy_account_home(auxiliary_account, "_system")
    sub_env = _build_subprocess_env(
        isolated_home_override=sys_cli_home,
        proxy_url=probe_proxy_url,
    )
    tmp_conv_id = ""
    accumulated_text = ""
    fallback_response = ""
    result_error: Optional[str] = None
    proc: Optional[asyncio.subprocess.Process] = None

    try:
        proc = await create_agy_subprocess_exec(
            *cmd,
            cwd=str(CHAT_SANDBOX_DIR),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sub_env,
            priority="telemetry",
            start_new_session=(os.name != "nt"),
            limit=4 * 1024 * 1024,
            **subprocess_hidden_kwargs(),
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        async def _consume_probe() -> tuple[int, str]:
            nonlocal tmp_conv_id, accumulated_text, fallback_response, result_error
            got_res = False
            async for raw_line in proc.stdout:  # type: ignore[union-attr]
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                etype = evt.get("event")
                if etype == "init":
                    tmp_conv_id = evt.get("conversation_id") or tmp_conv_id
                elif etype == "step_update":
                    su = evt.get("step_update") or {}
                    tmp_conv_id = su.get("conversation_id") or tmp_conv_id
                    if su.get("step_type") == "agent_response":
                        td = su.get("text_delta") or ""
                        if td:
                            accumulated_text += td
                elif etype == "result":
                    res = evt.get("result") or {}
                    fallback_response = res.get("response") or ""
                    if res.get("status") == "ERROR":
                        result_error = res.get("error") or "Ошибка авто-паспортизации моделей"
                    got_res = True
                    break

            async def _drain() -> tuple[bytes, int]:
                sb = await proc.stderr.read()  # type: ignore[union-attr]
                ec = await proc.wait()
                return sb, ec

            try:
                stderr_b, rc_val = await asyncio.wait_for(_drain(), timeout=3.5)
                stderr_s = stderr_b.decode("utf-8", errors="replace").strip()
            except asyncio.TimeoutError:
                stderr_s = ""
                await _terminate_hung_cli_proc(proc)
                rc_val = 0 if (got_res and not result_error) else -9
            else:
                if got_res and not result_error and rc_val != 0:
                    rc_val = 0
            return rc_val, stderr_s

        try:
            rc, stderr_txt = await asyncio.wait_for(_consume_probe(), timeout=45.0)
        except asyncio.TimeoutError as exc:
            await _terminate_hung_cli_proc(proc)
            raise RuntimeError("Превышен таймаут авто-паспортизации моделей (45 с)") from exc

        if rc != 0 or result_error:
            raise RuntimeError(result_error or stderr_txt or f"Код выхода agy.exe: {rc}")

        raw_out = (accumulated_text or fallback_response).strip()
        return _parse_json_object_loose(raw_out)
    finally:
        if proc is not None and proc.returncode is None:
            await _terminate_hung_cli_proc(proc)
        if hasattr(proc, "aclose_lease"):
            await proc.aclose_lease()
        if auxiliary_account and auxiliary_account.get("has_credentials"):
            await sync_refreshed_agy_credentials_back(auxiliary_account["id"])
        if tmp_conv_id:
            for ext in (".db", ".db-wal", ".db-shm"):
                for conv_root in (
                    AGY_CLI_DATA_DIR / "conversations",
                    sys_cli_home / ".gemini" / "antigravity-cli" / "conversations",
                ):
                    try:
                        (conv_root / f"{tmp_conv_id}{ext}").unlink(missing_ok=True)
                    except Exception:
                        pass
