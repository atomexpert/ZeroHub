"""
OpenAI Codex CLI (`@openai/codex`) Orchestration Engine & Multi-Account Manager for Antigravity Hub.

Provides:
- Official `codex app-server` JSON-RPC 2.0 over stdio transport (0 browser scraping, $0 API cost on ChatGPT subscriptions).
- Strict per-user, per-account isolated `CODEX_HOME` (`cli_auth_credentials_store = "file"`, 0 Windows Credential Manager usage).
- 0% GPU / 0 MB VRAM (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`) & 0 console popups (`subprocess_hidden_kwargs()`).
- Remote-friendly OAuth Device Code login (`codex login --device-auth`) + fallback `auth.json` import.
- Live 5-hour (`primary`) and 7-day weekly (`secondary`) subscription quota reader (`account/rateLimits/read`).
- Unified Hub streaming contract (`codex_session`, `thinking_status`, `thinking_update`, `tool_step`, `text_delta`, `done`, `cancelled`, `error`)
  with File Traffic Light (`active_files`), Project Passport (`developerInstructions`), `<PROJECT_PULSE>`, MCP (`hub_openrouter`, `hub_sandbox`),
  and automatic multi-account failover on 5h quota exhaustion.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from . import config, database, proxy_router
from .brain_reader import (
    ensure_isolated_project_git_boundary,
    resolve_effective_project_execution_mode,
)
from .agy_engine import (
    ACTIVE_RUNS,
    AUTO_CONTINUE_PAUSE_SEC,
    CAPACITY_RETRY_BACKOFF_SECS,
    STREAM_ACTIVE_TOOL_TIMEOUT_SEC,
    STREAM_NETWORK_IDLE_TIMEOUT_SEC,
    STREAM_POST_TEXT_IDLE_TIMEOUT_SEC,
    ActiveRun,
    _augment_prompt_for_mode,
    _build_history_recovery_prompt,
    _parse_json_object_loose,
    _sleep_unless_cancelled,
    build_project_pulse_block,
    compute_resume_stitch_prefix,
    check_docker_status,
    extract_writing_tool_paths,
    is_transient_capacity_error,
    is_transient_stream_error,
    looks_like_completed_response,
    normalize_interrupted_tool_calls,
    register_chat_turn,
    resolve_project_dir,
    resolve_turn_execution_contour,
)
from .config import (
    CHAT_SANDBOX_DIR,
    CODEX_TOOLS_DIR,
    IS_WINDOWS,
    UPLOADS_DIR,
    _CODEX_VERSION_CACHE,
    apply_isolated_home_to_env,
    ensure_user_cli_home,
    ensure_user_codex_account_dir,
    ensure_user_codex_home,
    get_codex_cli_info,
    resolve_codex_bin,
    resolve_outbound_proxy,
    subprocess_hidden_kwargs,
    sync_user_cli_rules,
)

CODEX_MODEL_PREFIX = "codex:"
VALID_REASONING_EFFORTS: tuple[str, ...] = ("xhigh", "high", "medium", "low")
_EFFORT_DISPLAY_LABELS: dict[str, str] = {
    "xhigh": "XHigh",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}
_IMAGE_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

DEFAULT_CODEX_MODELS: list[dict[str, Any]] = [
    {"id": "codex:gpt-5.4@xhigh", "label": "GPT-5.4 (XHigh)", "family": "gpt", "context_window": 272_000},
    {"id": "codex:gpt-5.4@high", "label": "GPT-5.4 (High)", "family": "gpt", "context_window": 272_000},
    {"id": "codex:gpt-5.4@medium", "label": "GPT-5.4 (Medium)", "family": "gpt", "context_window": 272_000},
    {"id": "codex:gpt-5.4@low", "label": "GPT-5.4 (Low)", "family": "gpt", "context_window": 272_000},
    {"id": "codex:gpt-5.3-codex@xhigh", "label": "GPT-5.3 Codex (XHigh)", "family": "gpt", "context_window": 272_000},
    {"id": "codex:gpt-5.3-codex@high", "label": "GPT-5.3 Codex (High)", "family": "gpt", "context_window": 272_000},
    {"id": "codex:gpt-5.3-codex@medium", "label": "GPT-5.3 Codex (Medium)", "family": "gpt", "context_window": 272_000},
    {"id": "codex:gpt-5.4-mini@high", "label": "GPT-5.4 Mini (High)", "family": "gpt", "context_window": 272_000},
    {"id": "codex:gpt-5.4-mini@low", "label": "GPT-5.4 Mini (Low)", "family": "gpt", "context_window": 272_000},
]

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]"
)
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,5})\b")
_DEVICE_URL_RE = re.compile(r"(https://auth\.openai\.com[^\s\"'<>)\]]*)", re.IGNORECASE)
_FALSE_POSITIVE_CODES = frozenset({"UTF-8", "UTF-16", "ISO-8859", "HTTP-1", "JSON-RPC", "SHA-256"})

_QUOTA_SEMAPHORE = asyncio.Semaphore(2)
_INSTALL_LOCK = asyncio.Lock()


def is_codex_model(model_id: Optional[str]) -> bool:
    """Return True iff `model_id` targets the personal ChatGPT (`codex:`) orchestrator."""
    return str(model_id or "").strip().lower().startswith(CODEX_MODEL_PREFIX)


def parse_codex_model_id(model_id: str) -> tuple[str, str]:
    """Parse `'codex:gpt-5.4@xhigh'` -> `('gpt-5.4', 'xhigh')`."""
    raw = str(model_id or "").strip()
    if raw.lower().startswith(CODEX_MODEL_PREFIX):
        raw = raw[len(CODEX_MODEL_PREFIX) :].strip()
    if "@" in raw:
        slug_part, effort_part = raw.rsplit("@", 1)
    else:
        slug_part, effort_part = raw, ""
    slug = slug_part.strip() or "gpt-5.4"
    eff = effort_part.strip().lower()
    if eff == "minimal":
        eff = "low"
    if eff not in VALID_REASONING_EFFORTS:
        eff = "low" if "mini" in slug.lower() else "high"
    return slug, eff


def _humanize_codex_slug(slug: str, display_name: str = "") -> str:
    clean_disp = (display_name or "").strip()
    if clean_disp and not clean_disp.islower():
        # Strip any trailing `(High)` if upstream displayName included one
        clean_disp = re.sub(r"\s*\((?:xhigh|high|medium|low|minimal)\)\s*$", "", clean_disp, flags=re.I).strip()
        if clean_disp:
            return clean_disp
    parts = [p for p in slug.strip().split("-") if p]
    if not parts:
        return "GPT-5.4"
    out: list[str] = []
    i = 0
    while i < len(parts):
        low = parts[i].lower()
        if low == "gpt" and i + 1 < len(parts) and re.match(r"^\d+(\.\d+)?$", parts[i + 1]):
            out.append(f"GPT-{parts[i + 1]}")
            i += 2
            continue
        if low == "gpt":
            out.append("GPT")
        elif low == "codex":
            out.append("Codex")
        elif low in ("mini", "pro", "max", "nano"):
            out.append(low.capitalize())
        elif re.match(r"^o\d+$", low):
            out.append(low)
        else:
            out.append(parts[i].capitalize())
        i += 1
    return " ".join(out)


def build_codex_models_from_rpc(raw_items: Any) -> list[dict[str, Any]]:
    """Convert `model/list` JSON-RPC items into Hub model selector entries."""
    if not isinstance(raw_items, list) or not raw_items:
        return [dict(m) for m in DEFAULT_CODEX_MODELS]

    built: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    effort_order = {"xhigh": 0, "high": 1, "medium": 2, "low": 3}

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        if item.get("hidden") is True:
            continue
        slug = str(item.get("model") or item.get("id") or "").strip()
        if not slug:
            continue
        base_title = _humanize_codex_slug(slug, str(item.get("displayName") or ""))
        model_desc = str(item.get("description") or "").strip()[:240]
        ctx_win = int(
            item.get("contextWindow")
            or item.get("modelContextWindow")
            or item.get("context_window")
            or 272_000
        )
        raw_efforts = item.get("supportedReasoningEfforts")
        efforts: list[str] = []
        if isinstance(raw_efforts, list):
            for e_obj in raw_efforts:
                e_val = (
                    str(e_obj.get("reasoningEffort") or "")
                    if isinstance(e_obj, dict)
                    else str(e_obj or "")
                ).strip().lower()
                if e_val == "minimal":
                    e_val = "low"
                if e_val in VALID_REASONING_EFFORTS and e_val not in efforts:
                    efforts.append(e_val)
        if not efforts:
            def_eff = str(item.get("defaultReasoningEffort") or "high").strip().lower()
            if def_eff in VALID_REASONING_EFFORTS:
                efforts = ["high", "low"] if "mini" in slug.lower() else ["xhigh", "high", "medium", "low"]
            else:
                efforts = ["high", "medium", "low"]

        efforts.sort(key=lambda x: effort_order.get(x, 9))
        for eff in efforts:
            mid = f"{CODEX_MODEL_PREFIX}{slug}@{eff}"
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            eff_lbl = _EFFORT_DISPLAY_LABELS.get(eff, eff.capitalize())
            entry_item: dict[str, Any] = {
                "id": mid,
                "label": f"{base_title} ({eff_lbl})",
                "family": "gpt",
                "context_window": ctx_win,
            }
            if model_desc:
                entry_item["description"] = model_desc
            built.append(entry_item)

    return built if built else [dict(m) for m in DEFAULT_CODEX_MODELS]


def get_models_for_chatgpt_account(account: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return active `codex:*` model entries for a user's personal ChatGPT account (or `[]` if paused/none)."""
    if not account or not account.get("is_enabled", True):
        return []
    cached = account.get("models")
    if isinstance(cached, list) and len(cached) > 0:
        valid = [
            m
            for m in cached
            if isinstance(m, dict) and is_codex_model(str(m.get("id") or ""))
        ]
        if valid:
            return valid
    return [dict(m) for m in DEFAULT_CODEX_MODELS]


def _build_codex_exec_argv(bin_path: str, *args: str) -> list[str]:
    """Build subprocess argv supporting direct native binary, `.py` test mocks, and Windows `.cmd` shims."""
    low = bin_path.lower()
    if low.endswith(".py"):
        return [sys.executable, bin_path, *args]
    if IS_WINDOWS and low.endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/s", "/c", bin_path, *args]
    return [bin_path, *args]


_UNSET_PROXY: Any = object()


def _build_codex_subprocess_env(
    *,
    user_or_id: Any,
    codex_home: Path,
    proxy_url: Any = _UNSET_PROXY,
) -> dict[str, str]:
    """Build strictly isolated environment for Codex CLI:
    - `CODEX_HOME` -> isolated account or pending dir
    - `HOME` / `USERPROFILE` -> isolated `data/cli_homes/<safe_uid>`
    - `CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"` (0% GPU / 0 MB VRAM)
    - Context-resolved per-account or global proxy via `apply_proxy_to_subprocess_env`
      (`NO_PROXY="*"` in `direct` mode so Rust `reqwest` ignores Windows `winreg`).
    """
    env = os.environ.copy()
    cli_home = ensure_user_cli_home(user_or_id)
    apply_isolated_home_to_env(env, cli_home)
    env["CODEX_HOME"] = str(codex_home.resolve())
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"
    env["NO_COLOR"] = "1"

    cli_info = get_codex_cli_info(probe_version=False)
    extra_path = str(cli_info.get("extra_path_dir") or "").strip()
    if extra_path:
        cur_path = env.get("PATH") or ""
        env["PATH"] = f"{extra_path}{os.pathsep}{cur_path}" if cur_path else extra_path

    eff_proxy = (
        proxy_router.resolve_global_proxy_sync()
        if proxy_url is _UNSET_PROXY
        else proxy_url
    )
    proxy_router.apply_proxy_to_subprocess_env(env, eff_proxy)
    return env


def resolve_codex_security_matrix(
    *,
    kind: str,
    access_mode: str,
    is_admin: bool,
    force_no_host_shell: Optional[bool] = None,
    allow_hub_openrouter: bool = False,
    allow_hub_sandbox: bool = False,
    allow_hub_host: bool = True,
    workspace_dir: Optional[Path] = None,
    extra_readable_dirs: Optional[list[Path]] = None,
    extra_dirs: Optional[list[Path]] = None,
    sandbox_network: str = "internet",
) -> dict[str, Any]:
    """Compute exact Codex CLI sandbox mode, `shell_tool` flag, MCP permissions, and `turn/start` policy."""
    eff_no_host_shell = (
        (not is_admin) if force_no_host_shell is None else bool(force_no_host_shell)
    )
    eff_sandbox = bool(
        allow_hub_sandbox
        and kind == "project"
        and access_mode == "full_access"
        and eff_no_host_shell
    )
    if kind == "chat":
        sandbox_mode = "read-only"
        shell_tool = False
        eff_sandbox = False
    elif access_mode == "read_only":
        sandbox_mode = "read-only"
        shell_tool = False
        eff_sandbox = False
    elif access_mode == "write_files":
        sandbox_mode = "workspace-write"
        shell_tool = False
        eff_sandbox = False
    elif access_mode == "full_access":
        if eff_no_host_shell:
            sandbox_mode = "workspace-write"
            shell_tool = False
        else:
            sandbox_mode = "danger-full-access"
            shell_tool = True
            eff_sandbox = False
    else:
        sandbox_mode = "read-only"
        shell_tool = False
        eff_sandbox = False

    eff_host_mcp = bool(
        allow_hub_host
        and shell_tool
        and kind == "project"
        and access_mode == "full_access"
        and not eff_no_host_shell
    )

    writable_roots: list[str] = []
    if workspace_dir is not None:
        writable_roots.append(str(workspace_dir.resolve()))
    for ed in extra_dirs or []:
        if ed is not None and sandbox_mode == "workspace-write":
            ed_str = str(Path(ed).resolve())
            if ed_str not in writable_roots:
                writable_roots.append(ed_str)
    for rdir in extra_readable_dirs or []:
        if rdir is not None and sandbox_mode == "workspace-write":
            # Uploads stay readable naturally; we only keep workspace_dir (+ extra_dirs) writable!
            pass

    net_ok = str(sandbox_network or "internet").strip().lower() != "none"
    if sandbox_mode == "danger-full-access":
        turn_sandbox_policy: dict[str, Any] = {"type": "dangerFullAccess"}
    elif sandbox_mode == "workspace-write":
        turn_sandbox_policy = {
            "type": "workspaceWrite",
            "writableRoots": writable_roots,
            "networkAccess": net_ok,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }
    else:
        turn_sandbox_policy = {
            "type": "readOnly",
            "networkAccess": True,
        }

    extra_readable_roots: list[str] = []
    if is_admin and kind == "project":
        if config.PROJECTS_ROOT.exists():
            extra_readable_roots.append(str(config.PROJECTS_ROOT.resolve()))
        if config.SHARED_PROJECTS_DIR.exists():
            sp_root_str = str(config.SHARED_PROJECTS_DIR.resolve())
            if sp_root_str not in extra_readable_roots:
                extra_readable_roots.append(sp_root_str)
    for ed in extra_dirs or []:
        if ed is not None:
            ed_str = str(Path(ed).resolve())
            if ed_str not in extra_readable_roots:
                extra_readable_roots.append(ed_str)

    return {
        "sandbox_mode": sandbox_mode,
        "shell_tool": shell_tool,
        "allow_hub_files": bool(not shell_tool),
        "allow_hub_openrouter": bool(allow_hub_openrouter),
        "allow_hub_sandbox": eff_sandbox,
        "allow_hub_host": eff_host_mcp,
        "turn_sandbox_policy": turn_sandbox_policy,
        "extra_readable_roots": extra_readable_roots,
    }


TRUSTED_HUB_MCP_SERVERS: frozenset[str] = frozenset(
    {"hub_sandbox", "hub_openrouter", "hub_files", "hub_host"}
)
_HUB_OPENROUTER_TOOLS: tuple[str, ...] = (
    "openrouter_chat",
    "openrouter_multi_chat",
    "openrouter_models",
)
_HUB_SANDBOX_TOOLS: tuple[str, ...] = (
    "sandbox_exec",
    "sandbox_service_ctl",
    "sandbox_reset",
)
_HUB_HOST_TOOLS: tuple[str, ...] = (
    "host_service_ctl",
)
_HUB_FILES_TOOLS: tuple[str, ...] = (
    "read_file",
    "list_dir",
    "find_by_name",
    "grep_search",
    "write_file",
    "edit_file",
)
_MCP_APPROVAL_QUESTION_RE = re.compile(
    r"Allow\s+the\s+([A-Za-z0-9_.\-]+)\s+MCP\s+server\s+to\s+run\s+tool",
    re.IGNORECASE,
)


def _toml_quote(val: str) -> str:
    escaped = str(val).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def resolve_codex_approval_policy(
    *,
    sandbox_mode: str,
    openrouter_mcp_url: str = "",
    sandbox_mcp_url: str = "",
    files_mcp_url: str = "",
    host_mcp_url: str = "",
) -> str:
    """Return Codex `approval_policy` / `approvalPolicy`:
    - In `danger-full-access` (or when no MCP servers are attached): `"never"`.
    - In `read-only` / `workspace-write` with trusted Hub MCP servers attached: `"on-request"`,
      paired with per-server `default_tools_approval_mode = "approve"` + Zero-Trust JSON-RPC auto-approver
      so `codex 0.167+` never auto-cancels MCP calls under `approval_policy = "never"`.
    """
    has_any_mcp = bool(
        openrouter_mcp_url.strip()
        or sandbox_mcp_url.strip()
        or files_mcp_url.strip()
        or host_mcp_url.strip()
    )
    if sandbox_mode != "danger-full-access" and has_any_mcp:
        return "on-request"
    return "never"


def _append_trusted_mcp_toml_section(
    lines: list[str],
    *,
    server_name: str,
    url: str,
    tool_names: tuple[str, ...],
    tool_timeout_sec: int,
) -> None:
    lines.extend(
        [
            "",
            f"[mcp_servers.{server_name}]",
            f"url = {_toml_quote(url)}",
            'default_tools_approval_mode = "approve"',
            "startup_timeout_sec = 20",
            f"tool_timeout_sec = {int(tool_timeout_sec)}",
        ]
    )
    for t_name in tool_names:
        lines.extend(
            [
                "",
                f"[mcp_servers.{server_name}.tools.{t_name}]",
                'approval_mode = "approve"',
            ]
        )


def render_codex_config_toml(
    *,
    sandbox_mode: str = "read-only",
    shell_tool: bool = False,
    network_access: bool = True,
    openrouter_mcp_url: str = "",
    sandbox_mcp_url: str = "",
    files_mcp_url: str = "",
    host_mcp_url: str = "",
    approval_policy: Optional[str] = None,
) -> str:
    """Render strict isolated `config.toml` for Codex CLI.
    CRITICAL INVARIANT: `cli_auth_credentials_store = "file"` MUST stay at top-level
    so Codex never touches Windows Credential Manager (`keyring`)."""
    eff_approval = (
        approval_policy.strip()
        if isinstance(approval_policy, str) and approval_policy.strip()
        else resolve_codex_approval_policy(
            sandbox_mode=sandbox_mode,
            openrouter_mcp_url=openrouter_mcp_url,
            sandbox_mcp_url=sandbox_mcp_url,
            files_mcp_url=files_mcp_url,
            host_mcp_url=host_mcp_url,
        )
    )
    web_search_mode = "live" if network_access else "disabled"
    lines: list[str] = [
        'cli_auth_credentials_store = "file"',
        f"approval_policy = {_toml_quote(eff_approval)}",
        f"sandbox_mode = {_toml_quote(sandbox_mode)}",
        f"web_search = {_toml_quote(web_search_mode)}",
        "",
        "[features]",
        f"shell_tool = {'true' if shell_tool else 'false'}",
    ]
    if files_mcp_url.strip():
        _append_trusted_mcp_toml_section(
            lines,
            server_name="hub_files",
            url=files_mcp_url.strip(),
            tool_names=_HUB_FILES_TOOLS,
            tool_timeout_sec=60,
        )
    if openrouter_mcp_url.strip():
        _append_trusted_mcp_toml_section(
            lines,
            server_name="hub_openrouter",
            url=openrouter_mcp_url.strip(),
            tool_names=_HUB_OPENROUTER_TOOLS,
            tool_timeout_sec=200,
        )
    if sandbox_mcp_url.strip():
        _append_trusted_mcp_toml_section(
            lines,
            server_name="hub_sandbox",
            url=sandbox_mcp_url.strip(),
            tool_names=_HUB_SANDBOX_TOOLS,
            tool_timeout_sec=200,
        )
    if host_mcp_url.strip():
        _append_trusted_mcp_toml_section(
            lines,
            server_name="hub_host",
            url=host_mcp_url.strip(),
            tool_names=_HUB_HOST_TOOLS,
            tool_timeout_sec=60,
        )
    return "\n".join(lines) + "\n"


def write_codex_config_toml(
    codex_home: Path,
    *,
    sandbox_mode: str = "read-only",
    shell_tool: bool = False,
    network_access: bool = True,
    openrouter_mcp_url: str = "",
    sandbox_mcp_url: str = "",
    files_mcp_url: str = "",
    host_mcp_url: str = "",
    approval_policy: Optional[str] = None,
) -> Path:
    codex_home.mkdir(parents=True, exist_ok=True)
    cfg_path = codex_home / "config.toml"
    content = render_codex_config_toml(
        sandbox_mode=sandbox_mode,
        shell_tool=shell_tool,
        network_access=network_access,
        openrouter_mcp_url=openrouter_mcp_url,
        sandbox_mcp_url=sandbox_mcp_url,
        files_mcp_url=files_mcp_url,
        host_mcp_url=host_mcp_url,
        approval_policy=approval_policy,
    )
    tmp_path = codex_home / f".config.toml.{os.getpid()}.{secrets.token_hex(3)}.tmp"
    tmp_path.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp_path, cfg_path)
    return cfg_path


def _write_secret_auth_file(codex_home: Path, raw_auth_json: str) -> Path:
    codex_home.mkdir(parents=True, exist_ok=True)
    auth_file = codex_home / "auth.json"
    tmp_file = codex_home / f".auth.json.{os.getpid()}.{secrets.token_hex(3)}.tmp"
    tmp_file.write_text(raw_auth_json.strip() + "\n", encoding="utf-8", newline="\n")
    try:
        os.chmod(tmp_file, 0o600)
    except OSError:
        pass
    os.replace(tmp_file, auth_file)
    try:
        os.chmod(auth_file, 0o600)
    except OSError:
        pass
    return auth_file


def _decode_jwt_payload_unverified(token_str: str) -> dict[str, Any]:
    parts = (token_str or "").strip().split(".")
    if len(parts) < 2:
        return {}
    payload_b64 = parts[1]
    pad = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def extract_public_claims_from_auth_json(raw_auth_json: str) -> dict[str, str]:
    """Extract non-secret public account metadata (`email`, `chatgpt_account_id`, `plan_type`)
    from `auth.json` + OAuth JWT payload claims as a resilient fallback."""
    try:
        data = json.loads(raw_auth_json or "{}")
    except Exception:
        return {"email": "", "chatgpt_account_id": "", "plan_type": ""}
    if not isinstance(data, dict):
        return {"email": "", "chatgpt_account_id": "", "plan_type": ""}

    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    email = str(data.get("email") or tokens.get("email") or "").strip()
    account_id = str(
        tokens.get("account_id")
        or data.get("account_id")
        or data.get("chatgpt_account_id")
        or ""
    ).strip()
    plan_type = str(data.get("plan_type") or tokens.get("plan_type") or "").strip().lower()

    for jwt_key in ("id_token", "access_token"):
        tok = str(tokens.get(jwt_key) or data.get(jwt_key) or "").strip()
        if not tok:
            continue
        claims = _decode_jwt_payload_unverified(tok)
        if not claims:
            continue
        profile_ns = claims.get("https://api.openai.com/profile")
        auth_ns = claims.get("https://api.openai.com/auth")
        if not email:
            email = str(
                claims.get("email")
                or (profile_ns.get("email") if isinstance(profile_ns, dict) else "")
                or ""
            ).strip()
        if not account_id:
            account_id = str(
                (auth_ns.get("chatgpt_account_id") if isinstance(auth_ns, dict) else "")
                or (auth_ns.get("user_id") if isinstance(auth_ns, dict) else "")
                or claims.get("sub")
                or ""
            ).strip()
        if not plan_type and isinstance(auth_ns, dict):
            plan_type = str(
                auth_ns.get("chatgpt_plan_type") or auth_ns.get("plan_type") or ""
            ).strip().lower()

    return {
        "email": email[:160],
        "chatgpt_account_id": account_id[:160],
        "plan_type": plan_type[:40],
    }


async def _sync_back_refreshed_auth_json(
    user_id: str,
    account_id: str,
    codex_home: Path,
    original_raw_auth: str,
) -> None:
    """If Codex CLI refreshed OAuth tokens on disk, re-encrypt `auth.json` back into `hub.db`."""
    auth_file = codex_home / "auth.json"
    if not auth_file.is_file():
        return
    try:
        disk_raw = await asyncio.to_thread(auth_file.read_text, "utf-8")
    except Exception:
        return
    clean_disk = disk_raw.strip()
    if not clean_disk or clean_disk == (original_raw_auth or "").strip():
        return
    await database.sync_user_chatgpt_account_auth_json(user_id, account_id, clean_disk)


async def _terminate_codex_proc_tree(proc: Optional[asyncio.subprocess.Process]) -> None:
    """Guarantee zero zombie `codex` child processes and cleanly close Windows Proactor pipes."""
    if proc is None:
        return
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except Exception:
            pass
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.15)
        except Exception:
            pass
    if proc.returncode is None:
        pid = proc.pid
        if IS_WINDOWS and pid:
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdin=subprocess.DEVNULL,
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
                    proc.kill()
                except Exception:
                    pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.5)
        except Exception:
            pass
    for stream_pipe in (proc.stdout, proc.stderr):
        if stream_pipe is not None:
            try:
                await asyncio.wait_for(stream_pipe.read(), timeout=0.05)
            except Exception:
                pass
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        for fd_num in (0, 1, 2):
            try:
                pt = transport.get_pipe_transport(fd_num)
                if pt is not None:
                    pt.close()
            except Exception:
                pass
        try:
            transport.close()
        except Exception:
            pass
        await asyncio.sleep(0)
        await asyncio.sleep(0)


def _ts_to_iso_utc(raw_ts: Any) -> str:
    if raw_ts is None or raw_ts == "":
        return ""
    if isinstance(raw_ts, (int, float)):
        val = float(raw_ts)
        if val > 1e12:
            val = val / 1000.0
        if val <= 0:
            return ""
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
        except Exception:
            return ""
    s = str(raw_ts).strip()
    if re.match(r"^\d+(\.\d+)?$", s):
        return _ts_to_iso_utc(float(s))
    return s


def _parse_single_rate_window(raw_win: Any, default_mins: int) -> Optional[dict[str, Any]]:
    if not isinstance(raw_win, dict):
        return None
    used_raw = raw_win.get("usedPercent", raw_win.get("used_percent"))
    rem_raw = raw_win.get("remainingPercent", raw_win.get("remaining_percent"))
    if used_raw is None and rem_raw is None:
        return None
    try:
        if used_raw is not None:
            used_pct = round(max(0.0, min(100.0, float(used_raw))), 1)
            rem_pct = round(max(0.0, 100.0 - used_pct), 1)
        else:
            rem_pct = round(max(0.0, min(100.0, float(rem_raw))), 1)
            used_pct = round(max(0.0, 100.0 - rem_pct), 1)
    except (TypeError, ValueError):
        return None

    win_mins = int(
        raw_win.get("windowDurationMins")
        or raw_win.get("window_duration_mins")
        or default_mins
    )
    resets_at_raw = raw_win.get("resetsAt", raw_win.get("resets_at", raw_win.get("reset_time")))
    reset_iso = _ts_to_iso_utc(resets_at_raw)
    return {
        "used_pct": used_pct,
        "remaining_pct": rem_pct,
        "window_mins": win_mins,
        "reset_time": reset_iso,
        "resets_at": resets_at_raw if isinstance(resets_at_raw, (int, float)) else None,
    }


def parse_codex_rate_limits_payload(payload: Any) -> dict[str, Any]:
    """Normalize `account/rateLimits/read` or `account/rateLimits/updated` into Hub's quota schema."""
    if not isinstance(payload, dict):
        return {}
    rl = payload.get("rateLimits") if isinstance(payload.get("rateLimits"), dict) else payload
    ordinary_allowed = payload.get("ordinaryUsageAllowed")
    if ordinary_allowed is None and isinstance(rl, dict):
        ordinary_allowed = rl.get("ordinaryUsageAllowed")

    p_win = _parse_single_rate_window(rl.get("primary") or rl.get("primary_5h"), 300)
    s_win = _parse_single_rate_window(rl.get("secondary") or rl.get("secondary_7d"), 10080)

    # Also inspect `rateLimitsByLimitId` if top-level `rateLimits.primary` is absent
    by_id = payload.get("rateLimitsByLimitId")
    if p_win is None and isinstance(by_id, dict):
        for snap in by_id.values():
            if isinstance(snap, dict):
                p_win = _parse_single_rate_window(snap.get("primary"), 300)
                if s_win is None:
                    s_win = _parse_single_rate_window(snap.get("secondary"), 10080)
                if p_win is not None:
                    break

    if ordinary_allowed is False and p_win is not None and p_win["remaining_pct"] > 0:
        # Check if secondary hit 0% or primary hit 0%
        if s_win is None or s_win["remaining_pct"] > 0:
            p_win["remaining_pct"] = 0.0
            p_win["used_pct"] = 100.0

    plan_type = str(rl.get("planType") or rl.get("plan_type") or "").strip().lower()
    raw_credits = rl.get("credits")
    credits_out: Optional[dict[str, Any]] = None
    if isinstance(raw_credits, dict):
        credits_out = {
            "has_credits": bool(raw_credits.get("hasCredits", raw_credits.get("has_credits", False))),
            "unlimited": bool(raw_credits.get("unlimited", False)),
            "balance": raw_credits.get("balance"),
        }

    return {
        "ok": bool(p_win is not None or s_win is not None),
        "plan_type": plan_type,
        "ordinary_usage_allowed": ordinary_allowed if isinstance(ordinary_allowed, bool) else True,
        "rate_limit_reached_type": str(rl.get("rateLimitReachedType") or ""),
        "primary_5h": p_win,
        "secondary_7d": s_win,
        "credits": credits_out,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _rpc_send_json(proc: asyncio.subprocess.Process, obj: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("stdin подпроцесса codex закрыт")
    raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    proc.stdin.write(raw)
    await proc.stdin.drain()


async def probe_codex_home_session(
    user_id: str,
    codex_home: Path,
    *,
    include_models: bool = True,
    timeout_sec: float = 12.0,
    proxy_url: Any = _UNSET_PROXY,
) -> dict[str, Any]:
    """Run a fast, hidden `codex app-server` probe against `codex_home` to read:
    - `account/read`
    - `account/rateLimits/read`
    - `model/list` (when `include_models=True`)
    """
    codex_bin = resolve_codex_bin()
    if not codex_bin:
        raise RuntimeError(
            "OpenAI Codex CLI (@openai/codex) не найден на сервере. Администратор может установить его в окне ChatGPT."
        )

    eff_proxy = (
        await proxy_router.ensure_proxy_for_context("global")
        if proxy_url is _UNSET_PROXY
        else proxy_url
    )
    CHAT_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    write_codex_config_toml(codex_home, sandbox_mode="read-only", shell_tool=False)
    env = _build_codex_subprocess_env(
        user_or_id=user_id,
        codex_home=codex_home,
        proxy_url=eff_proxy,
    )
    argv = _build_codex_exec_argv(codex_bin, "app-server")

    proc: Optional[asyncio.subprocess.Process] = None
    async with _QUOTA_SEMAPHORE:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(CHAT_SANDBOX_DIR),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=(os.name != "nt"),
                limit=4 * 1024 * 1024,
                **subprocess_hidden_kwargs(),
            )
            assert proc.stdout is not None

            responses: dict[int, dict[str, Any]] = {}
            wanted_ids = {1, 2, 3} | ({4} if include_models else set())

            async def _run_exchange() -> None:
                await _rpc_send_json(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "antigravity_hub", "version": "1.5.0"},
                            "capabilities": None,
                        },
                    },
                )
                while True:
                    raw_line = await proc.stdout.readline()  # type: ignore[union-attr]
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    rid = msg.get("id")
                    if isinstance(rid, int):
                        responses[rid] = msg
                        if rid == 1:
                            await _rpc_send_json(
                                proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}}
                            )
                            await _rpc_send_json(
                                proc,
                                {
                                    "jsonrpc": "2.0",
                                    "id": 2,
                                    "method": "account/read",
                                    "params": {"refreshToken": False},
                                },
                            )
                            await _rpc_send_json(
                                proc,
                                {
                                    "jsonrpc": "2.0",
                                    "id": 3,
                                    "method": "account/rateLimits/read",
                                    "params": {},
                                },
                            )
                            if include_models:
                                await _rpc_send_json(
                                    proc,
                                    {
                                        "jsonrpc": "2.0",
                                        "id": 4,
                                        "method": "model/list",
                                        "params": {"includeHidden": False},
                                    },
                                )
                        if wanted_ids.issubset(responses.keys()):
                            break

            await asyncio.wait_for(_run_exchange(), timeout=timeout_sec)

            acc_res = (responses.get(2) or {}).get("result") or {}
            acc_obj = acc_res.get("account") if isinstance(acc_res, dict) else None
            rl_msg = responses.get(3) or {}
            rl_res = rl_msg.get("result") if isinstance(rl_msg.get("result"), dict) else {}
            rl_err = (rl_msg.get("error") or {}).get("message") if isinstance(rl_msg.get("error"), dict) else ""

            models_list: Optional[list[dict[str, Any]]] = None
            if include_models:
                m_res = (responses.get(4) or {}).get("result") or {}
                raw_data = m_res.get("data") if isinstance(m_res, dict) else None
                if isinstance(raw_data, list) and raw_data:
                    models_list = build_codex_models_from_rpc(raw_data)

            quotas = parse_codex_rate_limits_payload(rl_res) if rl_res else {}
            email = ""
            plan_type = ""
            if isinstance(acc_obj, dict):
                email = str(acc_obj.get("email") or "").strip()
                plan_type = str(acc_obj.get("planType") or acc_obj.get("plan_type") or "").strip().lower()
            if not plan_type and quotas.get("plan_type"):
                plan_type = str(quotas["plan_type"])

            return {
                "authenticated": isinstance(acc_obj, dict),
                "account": acc_obj if isinstance(acc_obj, dict) else {},
                "email": email,
                "plan_type": plan_type,
                "quotas": quotas,
                "models": models_list,
                "rate_limit_error": str(rl_err or ""),
            }
        finally:
            await _terminate_codex_proc_tree(proc)


async def fetch_chatgpt_account_quotas(
    user_id: str,
    account_id: str,
    *,
    include_models: bool = False,
) -> dict[str, Any]:
    """Materialize `auth.json` for `(user_id, account_id)`, query live 5h & 7d quotas via `codex app-server`,
    sync refreshed tokens back to Fernet storage, and update `user_chatgpt_accounts` cache."""
    secret_acc = await database.get_user_chatgpt_account_secret(user_id, account_id)
    if not secret_acc:
        raise ValueError("Аккаунт ChatGPT не найден или недоступен")

    raw_auth = str(secret_acc["auth_json"])
    acc_dir = await asyncio.to_thread(ensure_user_codex_account_dir, user_id, account_id)
    await asyncio.to_thread(_write_secret_auth_file, acc_dir, raw_auth)

    claims = extract_public_claims_from_auth_json(raw_auth)
    try:
        acc_proxy_url = await proxy_router.ensure_proxy_for_context(
            "chatgpt",
            user_id=user_id,
            account_id=account_id,
        )
        probe = await probe_codex_home_session(
            user_id,
            acc_dir,
            include_models=include_models or not bool(secret_acc.get("models")),
            timeout_sec=12.0,
            proxy_url=acc_proxy_url,
        )
        await _sync_back_refreshed_auth_json(user_id, account_id, acc_dir, raw_auth)

        if not probe.get("authenticated") and not claims.get("email"):
            err_msg = "Сессия ChatGPT истекла — войдите повторно"
            updated = await database.update_user_chatgpt_account_cache(
                user_id, account_id, last_error=err_msg
            )
            return updated or secret_acc

        quotas = probe.get("quotas") or secret_acc.get("quotas") or {}
        plan_type = str(
            probe.get("plan_type") or claims.get("plan_type") or secret_acc.get("plan_type") or ""
        ).strip().lower()
        if plan_type and isinstance(quotas, dict) and not quotas.get("plan_type"):
            quotas = {**quotas, "plan_type": plan_type}

        err_note = str(probe.get("rate_limit_error") or "").strip()
        updated = await database.update_user_chatgpt_account_cache(
            user_id,
            account_id,
            quotas=quotas if isinstance(quotas, dict) and quotas else None,
            models=probe.get("models"),
            plan_type=plan_type or None,
            email=str(probe.get("email") or claims.get("email") or ""),
            chatgpt_account_id=str(claims.get("chatgpt_account_id") or ""),
            last_error=err_note if (err_note and not quotas) else "",
        )
        return updated or (await database.get_user_chatgpt_account(user_id, account_id)) or {}
    except Exception as exc:
        err_text = str(exc).strip()[:300] or "Ошибка чтения лимитов Codex CLI"
        updated = await database.update_user_chatgpt_account_cache(
            user_id, account_id, last_error=err_text
        )
        return updated or (await database.get_user_chatgpt_account(user_id, account_id)) or {}


def _parse_iso_utc(raw_ts: Any) -> Optional[datetime]:
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


async def _read_rate_limits_from_open_proc(
    proc: Optional[asyncio.subprocess.Process],
    timeout_sec: float = 2.5,
) -> Optional[dict[str, Any]]:
    """Read live 5h/7d rate limits inside an already running `codex app-server` process (`0` extra processes)."""
    if proc is None or proc.returncode is not None or proc.stdin is None or proc.stdout is None:
        return None

    async def _exchange() -> Optional[dict[str, Any]]:
        await _rpc_send_json(
            proc,
            {
                "jsonrpc": "2.0",
                "id": "post_turn_rl",
                "method": "account/rateLimits/read",
                "params": {},
            },
        )
        while True:
            raw_line = await proc.stdout.readline()  # type: ignore[union-attr]
            if not raw_line:
                return None
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == "post_turn_rl":
                rl_res = msg.get("result")
                if isinstance(rl_res, dict):
                    parsed = parse_codex_rate_limits_payload(rl_res)
                    if parsed.get("ok"):
                        return parsed
                return None
            if msg.get("method") == "account/rateLimits/updated":
                params = msg.get("params")
                if isinstance(params, dict):
                    parsed = parse_codex_rate_limits_payload(params)
                    if parsed.get("ok"):
                        return parsed

    try:
        return await asyncio.wait_for(_exchange(), timeout=max(0.2, float(timeout_sec)))
    except Exception:
        return None


def _clamp_exhausted_chatgpt_quotas(
    existing_quotas: Optional[dict[str, Any]],
    err_text: str = "",
    err_code: str = "",
    *,
    live_quotas: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Guarantee exhausted ChatGPT window (`primary_5h` or `secondary_7d`) is clamped to `0%` remaining."""
    if isinstance(live_quotas, dict) and live_quotas.get("ok"):
        lp5 = live_quotas.get("primary_5h") if isinstance(live_quotas.get("primary_5h"), dict) else None
        ls7 = live_quotas.get("secondary_7d") if isinstance(live_quotas.get("secondary_7d"), dict) else None
        p5_zero = lp5 is not None and float(lp5.get("remaining_pct", 100.0)) <= 0.0
        s7_zero = ls7 is not None and float(ls7.get("remaining_pct", 100.0)) <= 0.0
        if p5_zero or s7_zero:
            out_live = dict(live_quotas)
            out_live["ok"] = True
            out_live["ordinary_usage_allowed"] = False
            out_live["updated_at"] = datetime.now(timezone.utc).isoformat()
            return out_live

    src = (
        dict(live_quotas)
        if isinstance(live_quotas, dict) and live_quotas
        else (dict(existing_quotas) if isinstance(existing_quotas, dict) else {})
    )
    combined_low = f"{err_text or ''} {err_code or ''}".lower()
    is_weekly = any(
        kw in combined_low
        for kw in ("7d", "7-day", "7 day", "weekly", "seven_day", "secondary")
    )

    if is_weekly:
        s7 = dict(src.get("secondary_7d") or {}) if isinstance(src.get("secondary_7d"), dict) else {}
        s7["used_pct"] = 100.0
        s7["remaining_pct"] = 0.0
        s7["window_mins"] = int(s7.get("window_mins") or 10080)
        s7.setdefault("reset_time", "")
        s7.setdefault("resets_at", None)
        src["secondary_7d"] = s7
        src.setdefault("rate_limit_reached_type", "secondary")
    else:
        p5 = dict(src.get("primary_5h") or {}) if isinstance(src.get("primary_5h"), dict) else {}
        p5["used_pct"] = 100.0
        p5["remaining_pct"] = 0.0
        p5["window_mins"] = int(p5.get("window_mins") or 300)
        p5.setdefault("reset_time", "")
        p5.setdefault("resets_at", None)
        src["primary_5h"] = p5
        src.setdefault("rate_limit_reached_type", "primary")

    src["ok"] = True
    src["ordinary_usage_allowed"] = False
    src["updated_at"] = datetime.now(timezone.utc).isoformat()
    return src


CHATGPT_DEFAULT_STALE_SEC = 300.0
CHATGPT_MIN_REFRESH_INTERVAL_SEC = 90.0
CHATGPT_FORCE_MIN_INTERVAL_SEC = 20.0
CHATGPT_ERROR_BACKOFF_SEC = 180.0

_CHATGPT_REFRESH_IN_FLIGHT: set[str] = set()
_CHATGPT_LAST_PROBE_MONO: dict[str, float] = {}
_CHATGPT_BACKOFF_UNTIL_MONO: dict[str, float] = {}
_CHATGPT_LAST_ACTIVE_ACC: dict[str, str] = {}
_CHATGPT_BG_TASKS: set[asyncio.Task[Any]] = set()


def is_chatgpt_quota_cache_stale(
    account: Optional[dict[str, Any]],
    max_age_sec: float = CHATGPT_DEFAULT_STALE_SEC,
) -> bool:
    """Return True if account quota cache is missing, older than `max_age_sec`, or past `reset_time + 10s`."""
    if not isinstance(account, dict) or not account:
        return True
    cached_dt = _parse_iso_utc(account.get("cached_at"))
    if cached_dt is None:
        return True
    now_utc = datetime.now(timezone.utc)
    if (now_utc - cached_dt).total_seconds() >= max(0.0, float(max_age_sec)):
        return True

    quotas = account.get("quotas")
    if isinstance(quotas, dict):
        for win_key in ("primary_5h", "secondary_7d"):
            win = quotas.get(win_key)
            if not isinstance(win, dict):
                continue
            reset_dt = _parse_iso_utc(win.get("reset_time"))
            if (
                reset_dt is not None
                and now_utc >= (reset_dt + timedelta(seconds=10))
                and cached_dt < reset_dt
            ):
                return True
    return False


def schedule_chatgpt_live_refresh(
    user_id: str,
    *,
    force: bool = False,
    max_age_sec: float = CHATGPT_DEFAULT_STALE_SEC,
    min_interval_sec: float = CHATGPT_MIN_REFRESH_INTERVAL_SEC,
    on_updated: Optional[Callable[[str], Awaitable[Any]]] = None,
    account_id: Optional[str] = None,
) -> bool:
    """Non-blocking background ChatGPT quota refresh with strict in-flight lock, cooldown, and 180s error backoff."""
    clean_uid = (user_id or "").strip()
    if not clean_uid or not resolve_codex_bin():
        return False

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    now_mono = time.monotonic()
    effective_min_interval = (
        CHATGPT_FORCE_MIN_INTERVAL_SEC
        if force
        else max(0.0, float(min_interval_sec))
    )
    prefix = f"{clean_uid}:"
    target_acc_id = (account_id or "").strip() or _CHATGPT_LAST_ACTIVE_ACC.get(clean_uid, "")

    if target_acc_id:
        known_key = f"{clean_uid}:{target_acc_id}"
        if known_key in _CHATGPT_REFRESH_IN_FLIGHT or f"{clean_uid}:*" in _CHATGPT_REFRESH_IN_FLIGHT:
            return False
        if not force and now_mono < _CHATGPT_BACKOFF_UNTIL_MONO.get(known_key, 0.0):
            return False
        last_mono = _CHATGPT_LAST_PROBE_MONO.get(known_key)
        if last_mono is not None and (now_mono - last_mono) < effective_min_interval:
            return False
    else:
        if any(k.startswith(prefix) for k in _CHATGPT_REFRESH_IN_FLIGHT):
            return False
        matching_backoffs = [v for k, v in _CHATGPT_BACKOFF_UNTIL_MONO.items() if k.startswith(prefix)]
        if not force and matching_backoffs and now_mono < max(matching_backoffs):
            return False
        matching_last = [v for k, v in _CHATGPT_LAST_PROBE_MONO.items() if k.startswith(prefix)]
        if matching_last and (now_mono - max(matching_last)) < effective_min_interval:
            return False

    pending_key = f"{clean_uid}:{target_acc_id}" if target_acc_id else f"{clean_uid}:*"
    _CHATGPT_REFRESH_IN_FLIGHT.add(pending_key)

    async def _runner() -> None:
        resolved_key = pending_key
        try:
            accounts = await database.list_user_chatgpt_accounts(clean_uid)
            if not accounts:
                return
            chosen: Optional[dict[str, Any]] = None
            if account_id:
                chosen = next((a for a in accounts if str(a.get("id")) == account_id.strip()), None)
            if chosen is None:
                chosen = next(
                    (a for a in accounts if a.get("is_active") and a.get("is_enabled")),
                    None,
                )
            if chosen is None:
                chosen = next((a for a in accounts if a.get("is_enabled")), None)
            if chosen is None:
                return

            acc_id = str(chosen["id"])
            _CHATGPT_LAST_ACTIVE_ACC[clean_uid] = acc_id
            resolved_key = f"{clean_uid}:{acc_id}"
            if resolved_key != pending_key:
                if resolved_key in _CHATGPT_REFRESH_IN_FLIGHT:
                    return
                _CHATGPT_REFRESH_IN_FLIGHT.add(resolved_key)

            cur_mono = time.monotonic()
            if not force and cur_mono < _CHATGPT_BACKOFF_UNTIL_MONO.get(resolved_key, 0.0):
                return
            prev_mono = _CHATGPT_LAST_PROBE_MONO.get(resolved_key)
            if prev_mono is not None and (cur_mono - prev_mono) < effective_min_interval:
                return
            if not force and not is_chatgpt_quota_cache_stale(chosen, max_age_sec=max_age_sec):
                return

            _CHATGPT_LAST_PROBE_MONO[resolved_key] = cur_mono
            updated = await fetch_chatgpt_account_quotas(clean_uid, acc_id)
            err_after = str((updated or {}).get("last_error") or "").strip()
            if err_after:
                _CHATGPT_BACKOFF_UNTIL_MONO[resolved_key] = (
                    time.monotonic() + CHATGPT_ERROR_BACKOFF_SEC
                )
            else:
                _CHATGPT_BACKOFF_UNTIL_MONO.pop(resolved_key, None)

            if on_updated is not None:
                await on_updated(clean_uid)
        except Exception:
            if resolved_key and not resolved_key.endswith(":*"):
                _CHATGPT_BACKOFF_UNTIL_MONO[resolved_key] = (
                    time.monotonic() + CHATGPT_ERROR_BACKOFF_SEC
                )
        finally:
            _CHATGPT_REFRESH_IN_FLIGHT.discard(pending_key)
            _CHATGPT_REFRESH_IN_FLIGHT.discard(resolved_key)

    task = loop.create_task(_runner())
    _CHATGPT_BG_TASKS.add(task)
    task.add_done_callback(_CHATGPT_BG_TASKS.discard)
    return True


def strip_ansi_codes(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def parse_device_auth_output(raw_text: str) -> tuple[str, str]:
    """Extract `(verification_url, user_code)` from `codex login --device-auth` stdout/stderr."""
    clean = strip_ansi_codes(raw_text)
    url_match = _DEVICE_URL_RE.search(clean)
    verification_url = (
        url_match.group(1).rstrip(".,;:")
        if url_match
        else "https://auth.openai.com/codex/device"
    )
    user_code = ""
    for m in _DEVICE_CODE_RE.finditer(clean):
        cand = m.group(1).upper()
        if cand in _FALSE_POSITIVE_CODES:
            continue
        # OpenAI device codes have uppercase letters/digits e.g. ABCD-EFGHI or 4F9K-2M8PQ
        user_code = cand
        break
    return verification_url, user_code


@dataclass
class DeviceAuthFlow:
    flow_id: str
    user_id: str
    label: str
    pending_dir: Path
    verification_url: str = "https://auth.openai.com/codex/device"
    user_code: str = ""
    status: str = "pending"  # pending | completed | expired | error | cancelled
    error: str = ""
    account: Optional[dict[str, Any]] = None
    proxy_mode: str = "global"
    proxy_spec: str = ""
    proxy_label: str = ""
    pending_owner: str = ""
    proxy_url: Optional[str] = None
    created_mono: float = field(default_factory=time.monotonic)
    expires_in: int = 600
    process: Optional[asyncio.subprocess.Process] = None
    wait_task: Optional[asyncio.Task[Any]] = None

    def to_public_dict(self) -> dict[str, Any]:
        elapsed = max(0, int(time.monotonic() - self.created_mono))
        rem = max(0, self.expires_in - elapsed) if self.status == "pending" else 0
        return {
            "flow_id": self.flow_id,
            "status": self.status,
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "expires_in": self.expires_in,
            "remaining_sec": rem,
            "error": self.error,
            "account": self.account,
            "proxy_mode": self.proxy_mode,
            "proxy_label": self.proxy_label,
        }


_DEVICE_AUTH_FLOWS: dict[str, DeviceAuthFlow] = {}


def _cleanup_expired_device_flows() -> None:
    now_m = time.monotonic()
    stale = [
        fid
        for fid, fl in list(_DEVICE_AUTH_FLOWS.items())
        if fl.status != "pending" and (now_m - fl.created_mono) > 900.0
    ]
    for fid in stale:
        _DEVICE_AUTH_FLOWS.pop(fid, None)


async def _resolve_user_is_admin(user_id: str, explicit: Optional[bool] = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    if (user_id or "").strip() == "admin":
        return True
    try:
        u = await database.get_user_by_id(user_id)
        return bool(u and str(u.get("role") or "").strip().lower() == "admin")
    except Exception:
        return False


def _prepare_pending_account_proxy(
    proxy_mode: Optional[str],
    proxy_spec: Optional[str],
    *,
    is_admin: bool,
) -> tuple[Optional[str], str, str]:
    """Validate and normalize `(eff_mode, clean_spec, safe_label)` for account connect/import."""
    clean_spec = str(proxy_spec or "").strip() if proxy_spec is not None else ""
    if proxy_mode is None and not clean_spec:
        return None, "", ""
    eff_mode = "custom" if str(proxy_mode or ("custom" if clean_spec else "global")).strip().lower() == "custom" else "global"
    if eff_mode == "global":
        return "global", "", ""
    if not clean_spec:
        raise ValueError("Укажите адрес прокси или ссылку Xray для режима «Свой прокси»")
    try:
        parsed = proxy_router.parse_proxy_spec(clean_spec)
        proxy_router.validate_proxy_target_security(
            parsed.host,
            parsed.port,
            is_admin=is_admin,
            resolve_dns=True,
        )
    except proxy_router.ProxyConfigError as exc:
        raise ValueError(str(exc)) from exc
    return "custom", clean_spec, parsed.safe_label


async def start_chatgpt_device_auth(
    user_id: str,
    *,
    label: str = "",
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    is_admin: Optional[bool] = None,
    on_complete: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """Start `codex login --device-auth` in an isolated `_pending_<flow_id>` directory
    strictly routed through the selected global or custom account proxy/Xray tunnel."""
    _cleanup_expired_device_flows()
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        raise ValueError("Не указан пользователь")

    eff_is_admin = await _resolve_user_is_admin(clean_uid, is_admin)
    p_mode_opt, clean_p_spec, p_safe_label = await asyncio.to_thread(
        _prepare_pending_account_proxy,
        proxy_mode if proxy_mode is not None else "global",
        proxy_spec,
        is_admin=eff_is_admin,
    )
    eff_p_mode = p_mode_opt or "global"

    codex_bin = resolve_codex_bin()
    if not codex_bin:
        raise RuntimeError(
            "На сервере не установлен OpenAI Codex CLI (@openai/codex). Откройте спойлер ниже для установки или импорта."
        )

    # Cancel any previous pending device flow of the same user so processes don't pile up
    for old_fid, old_fl in list(_DEVICE_AUTH_FLOWS.items()):
        if old_fl.user_id == clean_uid and old_fl.status == "pending":
            await cancel_chatgpt_device_auth_flow(clean_uid, old_fid)

    flow_id = secrets.token_hex(8)
    pending_owner = f"pending:cgpt:{flow_id}"
    flow_proxy_url = await proxy_router.ensure_proxy_for_context(
        "chatgpt",
        user_id=clean_uid,
        pending_owner=pending_owner,
        pending_mode=eff_p_mode,
        pending_spec=clean_p_spec,
        is_admin=eff_is_admin,
    )

    codex_base = await asyncio.to_thread(ensure_user_codex_home, clean_uid)
    pending_dir = (codex_base / f"_pending_{flow_id}").resolve()
    pending_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        write_codex_config_toml, pending_dir, sandbox_mode="read-only", shell_tool=False
    )

    env = _build_codex_subprocess_env(
        user_or_id=clean_uid,
        codex_home=pending_dir,
        proxy_url=flow_proxy_url,
    )
    argv = _build_codex_exec_argv(codex_bin, "login", "--device-auth")

    CHAT_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(CHAT_SANDBOX_DIR),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=(os.name != "nt"),
            **subprocess_hidden_kwargs(),
        )
    except Exception:
        await asyncio.to_thread(proxy_router.release_owner, pending_owner)
        shutil.rmtree(pending_dir, ignore_errors=True)
        raise

    flow = DeviceAuthFlow(
        flow_id=flow_id,
        user_id=clean_uid,
        label=(label or "").strip()[:80],
        pending_dir=pending_dir,
        proxy_mode=eff_p_mode,
        proxy_spec=clean_p_spec,
        proxy_label=p_safe_label,
        pending_owner=pending_owner,
        proxy_url=flow_proxy_url,
        process=proc,
    )
    _DEVICE_AUTH_FLOWS[flow_id] = flow

    captured_chunks: list[str] = []
    code_ready_evt = asyncio.Event()

    async def _pump_stream(stream: Optional[asyncio.StreamReader]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            txt = chunk.decode("utf-8", errors="replace")
            captured_chunks.append(txt)
            joined = "".join(captured_chunks)
            v_url, u_code = parse_device_auth_output(joined)
            if v_url:
                flow.verification_url = v_url
            if u_code:
                flow.user_code = u_code
                code_ready_evt.set()

    out_pump = asyncio.create_task(_pump_stream(proc.stdout))
    err_pump = asyncio.create_task(_pump_stream(proc.stderr))

    try:
        done_wait, _ = await asyncio.wait(
            [asyncio.create_task(code_ready_evt.wait()), asyncio.create_task(proc.wait())],
            timeout=15.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not flow.user_code:
            # Give pipe pumps a brief moment to flush
            await asyncio.sleep(0.15)
            v_url, u_code = parse_device_auth_output("".join(captured_chunks))
            if v_url:
                flow.verification_url = v_url
            if u_code:
                flow.user_code = u_code
    except Exception:
        pass

    if not flow.user_code:
        await _terminate_codex_proc_tree(proc)
        await asyncio.to_thread(proxy_router.release_owner, pending_owner)
        shutil.rmtree(pending_dir, ignore_errors=True)
        flow.status = "error"
        raw_err = strip_ansi_codes("".join(captured_chunks)).strip()[:320]
        flow.error = raw_err or "Не удалось получить код авторизации от `codex login --device-auth`"
        raise RuntimeError(flow.error)

    async def _await_completion() -> None:
        try:
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=float(flow.expires_in))
            except asyncio.TimeoutError:
                if flow.status == "pending":
                    flow.status = "expired"
                    flow.error = "Время ожидания кода истекло (10 минут)"
                await _terminate_codex_proc_tree(proc)
                return

            await asyncio.gather(out_pump, err_pump, return_exceptions=True)
            if flow.status == "cancelled":
                return

            auth_file = pending_dir / "auth.json"
            if rc != 0 or not auth_file.is_file():
                flow.status = "error"
                tail = strip_ansi_codes("".join(captured_chunks)).strip()[-300:]
                flow.error = tail or f"Процесс входа завершился с кодом {rc}"
                return

            raw_auth = await asyncio.to_thread(auth_file.read_text, "utf-8")
            claims = extract_public_claims_from_auth_json(raw_auth)
            probe: dict[str, Any] = {}
            try:
                probe = await probe_codex_home_session(
                    clean_uid,
                    pending_dir,
                    include_models=True,
                    timeout_sec=12.0,
                    proxy_url=flow.proxy_url,
                )
                # Re-read auth.json in case `account/read` normalized/refreshed it
                if auth_file.is_file():
                    raw_auth = await asyncio.to_thread(auth_file.read_text, "utf-8")
            except Exception:
                probe = {}

            email = str(probe.get("email") or claims.get("email") or "").strip()
            plan_type = str(probe.get("plan_type") or claims.get("plan_type") or "").strip().lower()
            quotas = probe.get("quotas") if isinstance(probe.get("quotas"), dict) else {}
            if plan_type and isinstance(quotas, dict) and not quotas.get("plan_type"):
                quotas = {**quotas, "plan_type": plan_type}
            models = probe.get("models") if isinstance(probe.get("models"), list) else [dict(m) for m in DEFAULT_CODEX_MODELS]

            saved_acc = await database.upsert_user_chatgpt_account(
                clean_uid,
                raw_auth_json=raw_auth,
                chatgpt_account_id=str(claims.get("chatgpt_account_id") or ""),
                email=email,
                label=flow.label,
                plan_type=plan_type,
                quotas=quotas,
                models=models,
                make_active=True,
                proxy_mode=flow.proxy_mode,
                proxy_spec=flow.proxy_spec if flow.proxy_mode == "custom" else None,
                proxy_label=flow.proxy_label,
            )
            await proxy_router.reload_routes_from_db()
            acc_dir = await asyncio.to_thread(
                ensure_user_codex_account_dir, clean_uid, str(saved_acc["id"])
            )
            await asyncio.to_thread(_write_secret_auth_file, acc_dir, raw_auth)

            flow.account = saved_acc
            flow.status = "completed"
            if on_complete is not None:
                try:
                    await on_complete(clean_uid, saved_acc)
                except Exception:
                    pass
        except Exception as exc:
            if flow.status == "pending":
                flow.status = "error"
                flow.error = str(exc)[:300]
        finally:
            await _terminate_codex_proc_tree(proc)
            await asyncio.to_thread(proxy_router.release_owner, pending_owner)
            shutil.rmtree(pending_dir, ignore_errors=True)

    flow.wait_task = asyncio.create_task(_await_completion())
    return flow.to_public_dict()


def get_chatgpt_device_auth_flow(user_id: str, flow_id: str) -> Optional[dict[str, Any]]:
    _cleanup_expired_device_flows()
    fl = _DEVICE_AUTH_FLOWS.get((flow_id or "").strip())
    if not fl or fl.user_id != (user_id or "").strip():
        return None
    return fl.to_public_dict()


async def cancel_chatgpt_device_auth_flow(user_id: str, flow_id: str) -> bool:
    fl = _DEVICE_AUTH_FLOWS.get((flow_id or "").strip())
    if not fl or fl.user_id != (user_id or "").strip():
        return False
    fl.status = "cancelled"
    await _terminate_codex_proc_tree(fl.process)
    if fl.wait_task and not fl.wait_task.done():
        fl.wait_task.cancel()
    if fl.pending_owner:
        await asyncio.to_thread(proxy_router.release_owner, fl.pending_owner)
    shutil.rmtree(fl.pending_dir, ignore_errors=True)
    return True


async def import_chatgpt_auth_json(
    user_id: str,
    *,
    raw_auth_json: str,
    label: str = "",
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    is_admin: Optional[bool] = None,
) -> dict[str, Any]:
    """Validate and import a personal `~/.codex/auth.json` under `user_id`."""
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        raise ValueError("Не указан пользователь")
    clean_raw = (raw_auth_json or "").strip()
    if not clean_raw:
        raise ValueError("Вставьте содержимое файла auth.json")
    try:
        parsed = json.loads(clean_raw)
    except Exception as exc:
        raise ValueError(f"Некорректный формат JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Содержимое auth.json должно быть JSON-объектом")

    tokens_obj = parsed.get("tokens") if isinstance(parsed.get("tokens"), dict) else {}
    has_token_material = bool(
        tokens_obj.get("access_token")
        or tokens_obj.get("refresh_token")
        or tokens_obj.get("id_token")
        or parsed.get("access_token")
        or parsed.get("OPENAI_API_KEY")
    )
    if not has_token_material:
        raise ValueError("В auth.json не найдены токены авторизации ChatGPT (`tokens.access_token`)")

    eff_is_admin = await _resolve_user_is_admin(clean_uid, is_admin)
    p_mode_opt, clean_p_spec, p_safe_label = await asyncio.to_thread(
        _prepare_pending_account_proxy,
        proxy_mode,
        proxy_spec,
        is_admin=eff_is_admin,
    )

    claims = extract_public_claims_from_auth_json(clean_raw)
    codex_base = await asyncio.to_thread(ensure_user_codex_home, clean_uid)
    imp_hex = secrets.token_hex(6)
    tmp_dir = (codex_base / f"_import_{imp_hex}").resolve()
    imp_owner = f"pending:cgpt_imp:{imp_hex}"
    probe: dict[str, Any] = {}
    try:
        imp_proxy_url = await proxy_router.ensure_proxy_for_context(
            "chatgpt",
            user_id=clean_uid,
            pending_owner=imp_owner,
            pending_mode=(p_mode_opt or "global"),
            pending_spec=clean_p_spec,
            is_admin=eff_is_admin,
        )
        await asyncio.to_thread(_write_secret_auth_file, tmp_dir, clean_raw)
        if resolve_codex_bin():
            probe = await probe_codex_home_session(
                clean_uid,
                tmp_dir,
                include_models=True,
                timeout_sec=12.0,
                proxy_url=imp_proxy_url,
            )
            if not probe.get("authenticated") and not claims.get("email"):
                raise ValueError("Сессия в auth.json недействительна или истекла")
            auth_after = tmp_dir / "auth.json"
            if auth_after.is_file():
                clean_raw = (await asyncio.to_thread(auth_after.read_text, "utf-8")).strip()
    finally:
        await asyncio.to_thread(proxy_router.release_owner, imp_owner)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    email = str(probe.get("email") or claims.get("email") or "").strip()
    plan_type = str(probe.get("plan_type") or claims.get("plan_type") or "").strip().lower()
    quotas = probe.get("quotas") if isinstance(probe.get("quotas"), dict) else {}
    if plan_type and isinstance(quotas, dict) and not quotas.get("plan_type"):
        quotas = {**quotas, "plan_type": plan_type}
    models = (
        probe.get("models")
        if isinstance(probe.get("models"), list) and probe.get("models")
        else [dict(m) for m in DEFAULT_CODEX_MODELS]
    )

    saved = await database.upsert_user_chatgpt_account(
        clean_uid,
        raw_auth_json=clean_raw,
        chatgpt_account_id=str(claims.get("chatgpt_account_id") or ""),
        email=email,
        label=(label or "").strip()[:80],
        plan_type=plan_type,
        quotas=quotas,
        models=models,
        make_active=True,
        proxy_mode=p_mode_opt,
        proxy_spec=clean_p_spec if p_mode_opt == "custom" else None,
        proxy_label=p_safe_label if p_mode_opt == "custom" else None,
    )
    await proxy_router.reload_routes_from_db()
    acc_dir = await asyncio.to_thread(ensure_user_codex_account_dir, clean_uid, str(saved["id"]))
    await asyncio.to_thread(_write_secret_auth_file, acc_dir, clean_raw)
    return saved


async def install_or_update_codex_cli() -> dict[str, Any]:
    """Install or update `@openai/codex@latest` locally into `data/tools/codex` (admin-only action,
    0 OS admin rights required, hidden console window, full outbound proxy support)."""
    async with _INSTALL_LOCK:
        npm_bin = (
            shutil.which("npm.cmd")
            or shutil.which("npm")
            or (r"C:\Program Files\nodejs\npm.cmd" if IS_WINDOWS else "/usr/bin/npm")
        )
        if not npm_bin or not Path(npm_bin).exists():
            raise RuntimeError("На сервере не найден Node.js / npm для установки @openai/codex")

        proxy = await proxy_router.ensure_proxy_for_context("global")
        CODEX_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        proxy_router.apply_proxy_to_subprocess_env(env, proxy, include_npm=True)
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["ORT_DISABLE_ALL_GPU"] = "1"

        cmd = (
            [
                "cmd.exe",
                "/d",
                "/s",
                "/c",
                npm_bin,
                "install",
                "--prefix",
                str(CODEX_TOOLS_DIR),
                "@openai/codex@latest",
                "--no-audit",
                "--no-fund",
            ]
            if IS_WINDOWS and npm_bin.lower().endswith((".cmd", ".bat"))
            else [
                npm_bin,
                "install",
                "--prefix",
                str(CODEX_TOOLS_DIR),
                "@openai/codex@latest",
                "--no-audit",
                "--no-fund",
            ]
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(CODEX_TOOLS_DIR),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **subprocess_hidden_kwargs(),
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=240.0)
        except asyncio.TimeoutError as exc:
            await _terminate_codex_proc_tree(proc)
            raise RuntimeError("Превышен таймаут установки @openai/codex (240 с)") from exc

        if proc.returncode != 0:
            err_txt = (err_b or out_b or b"").decode("utf-8", errors="replace").strip()[-400:]
            raise RuntimeError(f"Ошибка npm install @openai/codex: {err_txt}")

        _CODEX_VERSION_CACHE.clear()
        return get_codex_cli_info(probe_version=True)


# ---------------------------------------------------------------------------
# Streaming Turn Orchestrator, Tool Normalization, Auto-Failover & Helpers
# ---------------------------------------------------------------------------


def _map_codex_item_state(raw_status: str, event_method: str = "") -> str:
    low = (raw_status or "").strip().lower()
    if event_method == "item/started" or low in ("inprogress", "in_progress", "running", "pending"):
        return "ACTIVE"
    if low in ("failed", "declined", "error"):
        return "ERROR"
    if low in ("interrupted", "cancelled", "canceled"):
        return "INTERRUPTED"
    return "DONE"


def _normalize_codex_tool_item(
    item: dict[str, Any],
    *,
    step_index: int,
    event_method: str = "",
) -> Optional[list[dict[str, Any]]]:
    """Normalize a Codex `ThreadItem` (`commandExecution`, `fileChange`, `mcpToolCall`, `webSearch`)
    into one or more Hub `tool_calls` entries compatible with UI spoilers, File Traffic Light, and Auto-Changelog."""
    itype = str(item.get("type") or "").strip()
    if not itype or itype in ("userMessage", "agentMessage", "reasoning", "plan"):
        return None

    state = _map_codex_item_state(str(item.get("status") or ""), event_method)

    if itype == "commandExecution":
        cmd_str = item.get("command")
        if isinstance(cmd_str, list):
            cmd_str = " ".join(str(x) for x in cmd_str)
        cmd_clean = str(cmd_str or "").strip()
        params: dict[str, Any] = {"CommandLine": cmd_clean}
        if item.get("cwd"):
            params["Cwd"] = str(item["cwd"])
        if item.get("exitCode") is not None:
            params["ExitCode"] = item.get("exitCode")
        return [
            {
                "step_index": step_index,
                "name": "run_command",
                "state": state,
                "parameters": params,
            }
        ]

    if itype == "fileChange":
        changes = item.get("changes")
        entries: list[dict[str, Any]] = []
        if isinstance(changes, list) and changes:
            for sub_idx, ch in enumerate(changes):
                if not isinstance(ch, dict):
                    continue
                p_val = str(ch.get("path") or ch.get("file_path") or "").strip()
                kind_obj = ch.get("kind")
                kind_type = (
                    str(kind_obj.get("type") or "")
                    if isinstance(kind_obj, dict)
                    else str(kind_obj or "")
                ).lower()
                tool_name = "write_file" if kind_type in ("add", "create", "new") else "edit_file"
                entries.append(
                    {
                        "step_index": step_index + sub_idx,
                        "name": tool_name,
                        "state": state,
                        "parameters": {"Path": p_val} if p_val else {},
                    }
                )
        else:
            p_val = str(item.get("path") or item.get("file_path") or "").strip()
            entries.append(
                {
                    "step_index": step_index,
                    "name": "edit_file",
                    "state": state,
                    "parameters": {"Path": p_val} if p_val else {},
                }
            )
        return entries

    if itype == "mcpToolCall":
        srv = str(item.get("server") or "mcp").strip()
        tl = str(item.get("tool") or "call").strip()
        args = item.get("arguments")
        if not isinstance(args, dict):
            args = {"value": args} if args is not None else {}
        extra_params: dict[str, Any] = (
            {k: v for k, v in args.items() if isinstance(k, str)} if len(args) <= 8 else {}
        )
        if srv == "hub_files":
            p_cand = args.get("path") or args.get("Path")
            if isinstance(p_cand, str) and p_cand.strip():
                extra_params.setdefault("Path", p_cand.strip())
            d_cand = args.get("directory_path") or args.get("DirectoryPath")
            if isinstance(d_cand, str) and d_cand.strip():
                extra_params.setdefault("DirectoryPath", d_cand.strip())
            q_cand = args.get("query") or args.get("Query")
            if isinstance(q_cand, str) and q_cand.strip():
                extra_params.setdefault("Query", q_cand.strip())
            pat_cand = args.get("pattern") or args.get("Pattern")
            if isinstance(pat_cand, str) and pat_cand.strip():
                extra_params.setdefault("Pattern", pat_cand.strip())
        return [
            {
                "step_index": step_index,
                "name": f"mcp_{srv}_{tl}",
                "state": state,
                "parameters": {
                    "ServerName": srv,
                    "ToolName": tl,
                    "Arguments": args,
                    **extra_params,
                },
            }
        ]

    if itype in ("webSearch", "web_search"):
        q = str(item.get("query") or "").strip()
        return [
            {
                "step_index": step_index,
                "name": "search_web",
                "state": state,
                "parameters": {"query": q},
            }
        ]

    return None


def _is_chatgpt_quota_exhausted_error(err_text: str, error_code_info: str = "") -> bool:
    code_low = (error_code_info or "").strip().lower()
    if code_low in (
        "usagelimitedexceeded",
        "usagelimitexceeded",
        "usage_limit_exceeded",
        "usage_limit_reached",
        "ratelimitexceeded",
        "rate_limit_exceeded",
    ):
        return True
    low = (err_text or "").lower()
    if not low:
        return False
    return any(
        kw in low
        for kw in (
            "usage_limit_reached",
            "usage limit",
            "usagelimitexceeded",
            "rate_limit_exceeded",
            "rate limit reached",
            "too many requests",
            "429",
            "5-hour",
            "5 hour",
            "weekly limit",
            "quota exceeded",
            "hit your chatgpt",
            "you've hit your",
        )
    )


def _extract_token_usage_dict(raw_usage: Any) -> dict[str, int]:
    """Extract normalized Hub usage counters from `thread/tokenUsage/updated` or `turn/tokenUsage/updated`."""
    if not isinstance(raw_usage, dict):
        return {}
    tu = raw_usage.get("tokenUsage") if isinstance(raw_usage.get("tokenUsage"), dict) else raw_usage
    # Prefer `last` (per-turn breakdown) when non-zero, otherwise `total`
    breakdown = tu.get("last") if isinstance(tu.get("last"), dict) else None
    total_bd = tu.get("total") if isinstance(tu.get("total"), dict) else None
    has_last = bool(
        breakdown
        and any(
            int(breakdown.get(k) or 0) > 0
            for k in (
                "totalTokens",
                "total_tokens",
                "inputTokens",
                "input_tokens",
                "outputTokens",
                "output_tokens",
            )
        )
    )
    src = breakdown if has_last else (total_bd or tu)
    if not isinstance(src, dict):
        return {}

    inp = int(src.get("inputTokens") or src.get("input_tokens") or 0)
    cached = int(
        src.get("cachedInputTokens")
        or src.get("cached_input_tokens")
        or src.get("cache_read_tokens")
        or 0
    )
    out = int(src.get("outputTokens") or src.get("output_tokens") or 0)
    reas = int(
        src.get("reasoningOutputTokens")
        or src.get("reasoning_output_tokens")
        or src.get("thinking_tokens")
        or 0
    )
    tot = int(src.get("totalTokens") or src.get("total_tokens") or 0) or (inp + out)
    ctx_win = int(
        tu.get("modelContextWindow")
        or tu.get("model_context_window")
        or src.get("modelContextWindow")
        or 0
    )
    res: dict[str, int] = {
        "input_tokens": inp,
        "context_input_tokens": inp,
        "cache_read_tokens": cached,
        "output_tokens": out,
        "thinking_tokens": reas,
        "total_tokens": tot,
    }
    if ctx_win > 0:
        res["model_context_window"] = ctx_win
    return res


def _read_project_hub_rules(
    proj_dir: Optional[Path],
    *,
    access_mode: str = "write_files",
    disable_default_guardrails: bool = False,
    project_memory: Optional[dict[str, Any]] = None,
) -> str:
    """Read `<project_dir>/.agents/rules/hub_project.md` (if present) plus every other `*.md`
    from the same folder in deterministic alphabetical order, and append `DEFAULT_PROJECT_GUARDRAILS`
    when `engineering_guardrails.md` is absent. Keeps the safe 26000-char cap."""
    return config.read_project_hub_rules(
        proj_dir,
        access_mode=access_mode,
        disable_default_guardrails=disable_default_guardrails,
        project_memory=project_memory,
    )


def _build_turn_input_items(prompt_text: str, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {"type": "text", "text": prompt_text, "text_elements": []},
    ]
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        dpath = str(att.get("disk_path") or "").strip()
        if not dpath:
            continue
        p = Path(dpath)
        if p.suffix.lower() in _IMAGE_EXTS and p.is_file():
            items.append({"type": "localImage", "path": str(p.resolve())})
    return items


def is_codex_account_exhausted(acc: dict[str, Any]) -> bool:
    q = acc.get("quotas") or {}
    if not isinstance(q, dict):
        return False
    for key in ("primary_5h", "secondary_7d"):
        bucket = q.get(key)
        if not isinstance(bucket, dict):
            continue
        rem = bucket.get("remaining_pct")
        if rem is None or float(rem) > 0.0:
            continue
        rst_raw = str(bucket.get("reset_time") or bucket.get("resets_at") or bucket.get("reset_at") or "").strip()
        if rst_raw:
            try:
                dt = datetime.fromisoformat(rst_raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt <= datetime.now(timezone.utc):
                    continue
            except (ValueError, TypeError):
                pass
        return True
    return False


async def _pick_next_failover_account(
    user_id: str,
    tried_account_ids: set[str],
) -> Optional[dict[str, Any]]:
    accounts = await database.list_user_chatgpt_accounts(user_id)
    candidates: list[dict[str, Any]] = []
    for acc in accounts:
        aid = str(acc.get("id") or "")
        if not aid or aid in tried_account_ids:
            continue
        if not acc.get("is_enabled", True):
            continue
        if is_codex_account_exhausted(acc):
            continue
        candidates.append(acc)
    candidates.sort(
        key=lambda a: -float(
            (((a.get("quotas") or {}).get("primary_5h") or {}).get("remaining_pct"))
            if isinstance((a.get("quotas") or {}).get("primary_5h"), dict)
            and ((a.get("quotas") or {}).get("primary_5h") or {}).get("remaining_pct") is not None
            else 101.0
        )
    )
    return candidates[0] if candidates else None


async def resolve_codex_account_for_chat_turn(
    user_id: str,
    *,
    preferred_account_id: str = "",
    current_chat_account_id: str = "",
) -> Optional[dict[str, Any]]:
    accounts = await database.list_user_chatgpt_accounts(user_id)
    enabled = [a for a in accounts if a.get("is_enabled", True)]
    if not enabled:
        return None
    by_id = {str(a["id"]): a for a in enabled if a.get("id")}
    pref_id = str(preferred_account_id or "").strip()
    cur_id = str(current_chat_account_id or "").strip()

    if pref_id and pref_id in by_id:
        return by_id[pref_id]

    if cur_id and cur_id in by_id:
        bound = by_id[cur_id]
        if not is_codex_account_exhausted(bound) or not bound.get("auto_failover", True):
            return bound

    active = next((a for a in enabled if a.get("is_active")), enabled[0])
    if not is_codex_account_exhausted(active) or not active.get("auto_failover", True):
        return active

    tried = {str(active["id"])}
    if cur_id:
        tried.add(cur_id)
    nxt = await _pick_next_failover_account(user_id, tried)
    return nxt or active


async def _build_user_mcp_urls(
    user: Optional[dict[str, Any]],
    *,
    allow_hub_openrouter: bool,
    allow_hub_sandbox: bool,
    chat_id: str = "",
) -> tuple[str, str]:
    uid = str((user or {}).get("id") or "").strip()
    if not uid:
        return "", ""
    clean_cid = (chat_id or "").strip()
    or_url = ""
    sbx_url = ""
    if allow_hub_openrouter:
        try:
            from .openrouter_service import build_openrouter_mcp_token

            meta = await database.get_user_openrouter_bridge_meta(uid)
            if meta.get("has_key") and meta.get("is_enabled") and meta.get("key_nonce"):
                tok = build_openrouter_mcp_token(uid, str(meta["key_nonce"]))
                or_url = f"http://127.0.0.1:{config.HUB_PORT}/mcp/openrouter/{tok}"
                if clean_cid:
                    or_url = f"{or_url}?chat_id={urllib.parse.quote(clean_cid, safe='')}"
        except Exception:
            or_url = ""
    if allow_hub_sandbox and user is not None:
        try:
            from .sandbox_service import build_sandbox_mcp_token, is_user_sandbox_eligible

            if is_user_sandbox_eligible(user):
                sv = max(1, int(user.get("session_version") or 1))
                tok = build_sandbox_mcp_token(uid, sv)
                sbx_url = f"http://127.0.0.1:{config.HUB_PORT}/mcp/sandbox/{tok}"
        except Exception:
            sbx_url = ""
    return or_url, sbx_url


async def _build_files_mcp_url(
    user: Optional[dict[str, Any]],
    *,
    user_id: str,
    allow_hub_files: bool,
    chat_id: str,
) -> str:
    if not allow_hub_files:
        return ""
    clean_uid = (user_id or str((user or {}).get("id") or "")).strip()
    clean_cid = (chat_id or "").strip()
    if not clean_uid or not clean_cid:
        return ""
    try:
        from .workspace_mcp import build_files_mcp_token

        eff_user = user
        if eff_user is None or eff_user.get("session_version") is None:
            eff_user = await database.get_user_by_id(clean_uid) or eff_user or {}
        sv = max(1, int((eff_user or {}).get("session_version") or 1))
        tok = build_files_mcp_token(clean_uid, sv, clean_cid)
        return f"http://127.0.0.1:{config.HUB_PORT}/mcp/files/{tok}"
    except Exception:
        return ""


async def _build_host_mcp_url(
    user: Optional[dict[str, Any]],
    *,
    user_id: str,
    allow_hub_host: bool,
) -> str:
    if not allow_hub_host:
        return ""
    clean_uid = (user_id or str((user or {}).get("id") or "")).strip()
    if not clean_uid:
        return ""
    try:
        from .host_service_manager import build_local_host_mcp_url, is_user_host_eligible

        eff_user = user
        if eff_user is None or eff_user.get("session_version") is None:
            eff_user = await database.get_user_by_id(clean_uid) or eff_user or {"id": clean_uid, "role": "admin"}
        if not is_user_host_eligible(eff_user):
            return ""
        sv = max(1, int((eff_user or {}).get("session_version") or 1))
        return build_local_host_mcp_url(clean_uid, sv)
    except Exception:
        return ""


def _extract_file_change_item_paths(item_obj: dict[str, Any]) -> list[str]:
    out: list[str] = []
    changes = item_obj.get("changes")
    if isinstance(changes, list):
        for ch in changes:
            if isinstance(ch, dict):
                p = str(ch.get("path") or ch.get("file_path") or "").strip()
                if p and p not in out:
                    out.append(p)
    p_direct = str(item_obj.get("path") or item_obj.get("file_path") or "").strip()
    if p_direct and p_direct not in out:
        out.append(p_direct)
    return out


def evaluate_codex_server_request(
    msg: dict[str, Any],
    *,
    trusted_mcp_servers: set[str] | frozenset[str],
    shell_tool: bool,
    effective_access_mode: str,
    kind: str,
    workspace_dir: Path,
    known_file_changes: Optional[dict[str, list[str]]] = None,
    extra_dirs: Optional[list[Path]] = None,
) -> dict[str, Any]:
    """Zero-Trust JSON-RPC response builder for Server-to-Client requests from `codex app-server`:
    - Auto-approves MCP tool elicitations/approvals strictly for `trusted_mcp_servers`
      (`hub_sandbox`, `hub_openrouter`, `hub_files`, `hub_host`).
    - Auto-approves `fileChange` / `applyPatch` strictly when `kind == "project"` and
      `effective_access_mode in ("write_files", "full_access")` and all paths stay inside `workspace_dir` (or `extra_dirs`).
    - Strictly declines host `commandExecution` / `execCommandApproval` when `shell_tool == False`,
      `fileChange` in `read_only` or outside `workspace_dir`, sandbox permission escalations, and untrusted MCPs.
    """
    from .workspace_mcp import is_safe_workspace_write_target

    srv_req_id = msg.get("id")
    srv_method = str(msg.get("method") or "").strip()
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    # 1. MCP Elicitation approval (`mcpServer/elicitation/request`)
    if srv_method == "mcpServer/elicitation/request":
        srv_name = str(
            params.get("serverName") or params.get("server_name") or params.get("server") or ""
        ).strip()
        elicit_mode = str(params.get("mode") or "form").strip().lower()
        if srv_name and srv_name in trusted_mcp_servers and elicit_mode != "url":
            return {
                "jsonrpc": "2.0",
                "id": srv_req_id,
                "result": {"action": "accept", "content": {}},
            }
        return {
            "jsonrpc": "2.0",
            "id": srv_req_id,
            "result": {"action": "decline"},
        }

    # 2. Fallback MCP approval via `item/tool/requestUserInput`
    if srv_method == "item/tool/requestUserInput":
        questions = params.get("questions") if isinstance(params.get("questions"), list) else []
        answers: dict[str, Any] = {}
        top_srv = str(params.get("serverName") or params.get("server") or "").strip()
        for q in questions:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id") or "").strip()
            if not qid:
                continue
            qtext = str(q.get("question") or "")
            q_srv = str(q.get("serverName") or q.get("server") or top_srv).strip()
            if not q_srv:
                m_srv = _MCP_APPROVAL_QUESTION_RE.search(qtext)
                if m_srv:
                    q_srv = m_srv.group(1).strip()
            opts_raw = q.get("options") if isinstance(q.get("options"), list) else []
            opt_labels = [
                str(o.get("label") or "")
                for o in opts_raw
                if isinstance(o, dict) and str(o.get("label") or "")
            ]
            is_mcp_approval = qid.startswith("mcp_tool_call_approval") or (
                "mcp server to run tool" in qtext.lower()
            )
            if is_mcp_approval and q_srv and q_srv in trusted_mcp_servers:
                chosen = (
                    "Allow for this session"
                    if "Allow for this session" in opt_labels
                    else ("Allow" if "Allow" in opt_labels else (opt_labels[0] if opt_labels else "Allow"))
                )
                answers[qid] = {"answers": [chosen]}
            else:
                cancel_opt = ["Cancel"] if "Cancel" in opt_labels else []
                answers[qid] = {"answers": cancel_opt}
        return {
            "jsonrpc": "2.0",
            "id": srv_req_id,
            "result": {"answers": answers},
        }

    # 3. Direct MCP tool approval variants (`item/tool/requestApproval`, `mcpServer/tool/requestApproval`)
    if srv_method in (
        "item/tool/requestApproval",
        "mcpServer/requestApproval",
        "mcpServer/tool/requestApproval",
        "item/mcpToolCall/requestApproval",
    ):
        item_sub = params.get("item") if isinstance(params.get("item"), dict) else {}
        srv_name = str(
            params.get("serverName")
            or params.get("server_name")
            or params.get("server")
            or item_sub.get("server")
            or item_sub.get("serverName")
            or ""
        ).strip()
        decision = "accept" if (srv_name and srv_name in trusted_mcp_servers) else "decline"
        return {
            "jsonrpc": "2.0",
            "id": srv_req_id,
            "result": {"decision": decision},
        }

    # 4. File modification approval (`item/fileChange/requestApproval`)
    if srv_method == "item/fileChange/requestApproval":
        can_write_here = bool(
            kind == "project" and effective_access_mode in ("write_files", "full_access")
        )
        if not can_write_here:
            return {"jsonrpc": "2.0", "id": srv_req_id, "result": {"decision": "decline"}}

        grant_root = params.get("grantRoot")
        has_verified_root = False
        if isinstance(grant_root, str) and grant_root.strip():
            if not is_safe_workspace_write_target(
                grant_root.strip(), workspace_dir, allow_root_itself=True, extra_dirs=extra_dirs
            ):
                return {"jsonrpc": "2.0", "id": srv_req_id, "result": {"decision": "decline"}}
            has_verified_root = True

        candidate_paths: list[str] = []
        item_id = str(params.get("itemId") or "").strip()
        if item_id and known_file_changes and item_id in known_file_changes:
            candidate_paths.extend(known_file_changes[item_id])
        for direct_p_key in ("path", "file_path", "targetPath"):
            dp = params.get(direct_p_key)
            if isinstance(dp, str) and dp.strip():
                candidate_paths.append(dp.strip())
        raw_changes = params.get("changes")
        if isinstance(raw_changes, list):
            for ch in raw_changes:
                if isinstance(ch, dict):
                    cp = str(ch.get("path") or ch.get("file_path") or "").strip()
                    if cp:
                        candidate_paths.append(cp)
        raw_fc_map = params.get("fileChanges")
        if isinstance(raw_fc_map, dict):
            for fp_k in raw_fc_map.keys():
                if isinstance(fp_k, str) and fp_k.strip():
                    candidate_paths.append(fp_k.strip())

        if not candidate_paths and not has_verified_root:
            return {"jsonrpc": "2.0", "id": srv_req_id, "result": {"decision": "decline"}}
        for cp in candidate_paths:
            if not is_safe_workspace_write_target(
                cp, workspace_dir, allow_root_itself=False, extra_dirs=extra_dirs
            ):
                return {"jsonrpc": "2.0", "id": srv_req_id, "result": {"decision": "decline"}}
        return {"jsonrpc": "2.0", "id": srv_req_id, "result": {"decision": "accept"}}

    # 5. Legacy `applyPatchApproval` (`ReviewDecision`: `"approved"` | `{"denied": {"rejection": ...}}`)
    if srv_method == "applyPatchApproval":
        can_write_here = bool(
            kind == "project" and effective_access_mode in ("write_files", "full_access")
        )
        if not can_write_here:
            return {
                "jsonrpc": "2.0",
                "id": srv_req_id,
                "result": {"decision": {"denied": {"rejection": "Read-only mode"}}},
            }
        grant_root = params.get("grantRoot")
        if isinstance(grant_root, str) and grant_root.strip():
            if not is_safe_workspace_write_target(
                grant_root.strip(), workspace_dir, allow_root_itself=True, extra_dirs=extra_dirs
            ):
                return {
                    "jsonrpc": "2.0",
                    "id": srv_req_id,
                    "result": {"decision": {"denied": {"rejection": "Outside workspace root"}}},
                }
        fc_map = params.get("fileChanges")
        patch_paths = (
            [str(k).strip() for k in fc_map.keys() if str(k).strip()]
            if isinstance(fc_map, dict)
            else []
        )
        if not patch_paths or not all(
            is_safe_workspace_write_target(
                p, workspace_dir, allow_root_itself=False, extra_dirs=extra_dirs
            )
            for p in patch_paths
        ):
            return {
                "jsonrpc": "2.0",
                "id": srv_req_id,
                "result": {"decision": {"denied": {"rejection": "Path rejected by Zero-Trust guard"}}},
            }
        return {"jsonrpc": "2.0", "id": srv_req_id, "result": {"decision": "approved"}}

    # 6. Host shell execution approvals (`item/commandExecution/requestApproval` & legacy `execCommandApproval`)
    if srv_method == "item/commandExecution/requestApproval":
        decision = "accept" if shell_tool else "decline"
        return {"jsonrpc": "2.0", "id": srv_req_id, "result": {"decision": decision}}

    if srv_method == "execCommandApproval":
        if shell_tool:
            return {"jsonrpc": "2.0", "id": srv_req_id, "result": {"decision": "approved"}}
        return {
            "jsonrpc": "2.0",
            "id": srv_req_id,
            "result": {
                "decision": {
                    "denied": {
                        "rejection": "Host shell execution is disabled by Antigravity Hub security policy."
                    }
                }
            },
        }

    # 7. Additional sandbox permission escalation requests (`item/permissions/requestApproval`)
    if srv_method == "item/permissions/requestApproval":
        return {
            "jsonrpc": "2.0",
            "id": srv_req_id,
            "result": {"permissions": {}, "scope": "turn"},
        }

    # 8. Unknown approval / client requests: safe fail-closed decline
    if "requestApproval" in srv_method:
        return {
            "jsonrpc": "2.0",
            "id": srv_req_id,
            "result": {"decision": "decline"},
        }
    return {
        "jsonrpc": "2.0",
        "id": srv_req_id,
        "error": {"code": -32601, "message": f"Unsupported client request: {srv_method}"},
    }


async def stream_codex_turn(
    *,
    chat_id: str,
    kind: str,
    project_name: str,
    model: str,
    access_mode: str,
    prompt: str,
    attachments: list[dict[str, Any]],
    codex_thread_id: str,
    codex_account_id: str,
    history_messages: list[dict[str, Any]],
    compacted_summary: str = "",
    compacted_up_to_msg_id: int = 0,
    user: Optional[dict[str, Any]] = None,
    resolved_project_dir: Optional[Path] = None,
    execution_mode: str = "host",
    chat_title: str = "",
    project_scope_key: str = "",
    project_changelog: Optional[list[dict[str, Any]]] = None,
    recent_project_chats: Optional[list[dict[str, Any]]] = None,
    prev_chat_updated_at: str = "",
    referenced_chat_blocks: Optional[list[str]] = None,
    allow_hub_openrouter: bool = False,
    extra_dirs: Optional[list[Path]] = None,
    live_voice: bool = False,
    initial_content: str = "",
    initial_thinking: str = "",
    initial_tool_calls: Optional[list[dict[str, Any]]] = None,
    is_continue_turn: bool = False,
    preferred_codex_account_id: str = "",
    disable_default_guardrails: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Execute a turn via OpenAI `codex app-server` (JSON-RPC 2.0 over stdio) and yield Hub stream events."""
    normalized_initial_tools = normalize_interrupted_tool_calls(initial_tool_calls)
    codex_bin = resolve_codex_bin()
    if not codex_bin:
        yield {
            "type": "error",
            "error": (
                "На сервере не найден бинарник OpenAI Codex CLI (@openai/codex). "
                "Откройте карточку ChatGPT в левой панели для установки или проверки."
            ),
            "content": initial_content or "",
            "thinking": initial_thinking or "",
            "tool_calls": normalized_initial_tools,
        }
        return

    user_id = str((user or {}).get("id") or "admin").strip() or "admin"
    is_admin = bool(user is None or user.get("role") == "admin")
    user_custom_rules = str((user or {}).get("custom_rules") or "").strip()

    active_pub_acc = await resolve_codex_account_for_chat_turn(
        user_id,
        preferred_account_id=preferred_codex_account_id,
        current_chat_account_id=codex_account_id,
    )
    if not active_pub_acc:
        yield {
            "type": "error",
            "error": "Личный аккаунт ChatGPT не подключён или поставлен на паузу. Проверьте настройки ChatGPT.",
            "content": initial_content or "",
            "thinking": initial_thinking or "",
            "tool_calls": normalized_initial_tools,
        }
        return

    # Sync user's custom rules to `.gemini/GEMINI.md` + `.codex/AGENTS.md`
    await asyncio.to_thread(sync_user_cli_rules, user if user is not None else user_id, user_custom_rules)

    proj_dir: Optional[Path] = None
    if kind == "project" and project_name:
        if resolved_project_dir is not None and resolved_project_dir.is_dir():
            proj_dir = resolved_project_dir.resolve()
        elif is_admin:
            proj_dir = resolve_project_dir(project_name)
        if not proj_dir or not proj_dir.is_dir():
            yield {
                "type": "error",
                "error": f"Папка проекта '{project_name}' не найдена или недоступна",
                "content": initial_content or "",
                "thinking": initial_thinking or "",
                "tool_calls": normalized_initial_tools,
            }
            return
        await asyncio.to_thread(ensure_isolated_project_git_boundary, proj_dir)
        workspace_dir = proj_dir
    else:
        eff_chat_user = user if user is not None else {"id": user_id or "admin", "username": "admin" if is_admin else "", "role": "admin" if is_admin else "user"}
        workspace_dir = await asyncio.to_thread(
            config.resolve_user_chat_sandbox_dir, eff_chat_user, chat_id, create=True
        )
        await asyncio.to_thread(ensure_isolated_project_git_boundary, workspace_dir.parent)

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
    force_no_host_shell = bool(
        (kind != "project" and not is_admin)
        or eff_exec_mode != "host"
        or not can_host_shell
    )
    allow_hub_sandbox = False
    docker_info: Optional[dict[str, Any]] = None
    effective_access_mode = access_mode
    if use_docker_contour and access_mode == "full_access":
        docker_info = await check_docker_status()
        if docker_info.get("sandbox_mcp_ready"):
            allow_hub_sandbox = True
        else:
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
    turn_chat_ref = {"id": chat_id, "user_id": user_id}
    owner_uname = str((user or {}).get("username") or "")
    if attachments:
        primary_up = config.resolve_chat_uploads_dir(
            turn_chat_ref,
            owner_username=owner_uname,
            is_shared_project=is_shared_turn,
        )
        primary_up.mkdir(parents=True, exist_ok=True)
    turn_upload_dirs = config.resolve_chat_upload_dirs_for_turn(
        turn_chat_ref,
        chat_id=chat_id,
        owner_username=owner_uname,
        is_shared_project=is_shared_turn,
    )

    validated_extra_dirs: list[Path] = [
        Path(ed).resolve() for ed in (extra_dirs or []) if ed is not None and Path(ed).is_dir()
    ]
    allow_hub_host = bool(
        kind == "project"
        and eff_exec_mode == "host"
        and can_host_shell
        and effective_access_mode == "full_access"
        and not force_no_host_shell
    )
    sbx_net = str((user or {}).get("sandbox_network") or "internet").strip().lower()
    sec = resolve_codex_security_matrix(
        kind=kind,
        access_mode=effective_access_mode,
        is_admin=is_admin,
        force_no_host_shell=force_no_host_shell,
        allow_hub_openrouter=allow_hub_openrouter,
        allow_hub_sandbox=allow_hub_sandbox,
        allow_hub_host=allow_hub_host,
        workspace_dir=workspace_dir,
        extra_readable_dirs=turn_upload_dirs or None,
        extra_dirs=validated_extra_dirs or None,
        sandbox_network=sbx_net,
    )
    or_mcp_url, sbx_mcp_url = await _build_user_mcp_urls(
        user,
        allow_hub_openrouter=bool(sec["allow_hub_openrouter"]),
        allow_hub_sandbox=bool(sec["allow_hub_sandbox"]),
        chat_id=chat_id,
    )
    files_mcp_url = await _build_files_mcp_url(
        user,
        user_id=user_id,
        allow_hub_files=bool(sec.get("allow_hub_files", not sec["shell_tool"])),
        chat_id=chat_id,
    )
    host_mcp_url = await _build_host_mcp_url(
        user,
        user_id=user_id,
        allow_hub_host=bool(sec.get("allow_hub_host", False)),
    )
    eff_approval_policy = resolve_codex_approval_policy(
        sandbox_mode=str(sec["sandbox_mode"]),
        openrouter_mcp_url=or_mcp_url,
        sandbox_mcp_url=sbx_mcp_url,
        files_mcp_url=files_mcp_url,
        host_mcp_url=host_mcp_url,
    )
    trusted_turn_mcps: set[str] = set()
    if files_mcp_url:
        trusted_turn_mcps.add("hub_files")
    if or_mcp_url:
        trusted_turn_mcps.add("hub_openrouter")
    if sbx_mcp_url:
        trusted_turn_mcps.add("hub_sandbox")
    if host_mcp_url:
        trusted_turn_mcps.add("hub_host")

    dev_parts: list[str] = []
    if kind == "project":
        proj_rules = await asyncio.to_thread(
            lambda: _read_project_hub_rules(
                proj_dir,
                access_mode=effective_access_mode,
                disable_default_guardrails=disable_default_guardrails,
            )
        )
        if proj_rules:
            dev_parts.append(proj_rules)
        if validated_extra_dirs:
            ed_str = ", ".join(f"`{d}`" for d in validated_extra_dirs)
            dev_parts.append(
                f"К этому проекту подключены дополнительные рабочие директории (`Extra Workspace Directories`): {ed_str}."
            )
        if is_admin and sec.get("extra_readable_roots"):
            roots_str = ", ".join(sec["extra_readable_roots"])
            dev_parts.append(
                f"Каталог соседних проектов администратора `{roots_str}` доступен ТОЛЬКО для справочного чтения; "
                f"создание и изменение файлов строго ограничено текущей рабочей директорией проекта `{workspace_dir}`"
                + (" и подключёнными `Extra Workspace Directories`." if validated_extra_dirs else ".")
            )
    if host_mcp_url and sec.get("shell_tool"):
        dev_parts.append(
            "Долгоживущие фоновые сервисы Windows-хоста (Telegram/Discord-боты, `uvicorn`/FastAPI, Flask, Vite, `http.server`, воркеры) "
            "НЕ запускай напрямую блокирующим shell-вызовом — управляй ими через MCP-сервер `hub_host` (`host_service_ctl` с `action='start'|'restart'|'status'|'logs'|'stop'`)."
        )
    if sec["allow_hub_sandbox"]:
        dev_parts.append(
            "Хостовый терминал ПК (`shell` / `exec_command`) отключён политикой безопасности. "
            "Для быстрого чтения файлов, обзора папок, поиска по именам/содержимому и редактирования кода проекта используй MCP-сервер "
            "`hub_files` (`read_file`, `list_dir`, `find_by_name`, `grep_search`, `write_file`, `edit_file`) и штатный `apply_patch`, "
            "а все команды сборки, тестов, git, npm, python и фоновых серверов выполняй строго через MCP-сервер "
            "`hub_sandbox` (`sandbox_exec`, `sandbox_service_ctl`, `sandbox_reset`; проект смонтирован в `/workspace`)."
        )
    elif exec_contour == "docker_unavailable":
        dev_parts.append(
            f"Хостовый терминал ПК отключён политикой безопасности, а Docker-песочница сейчас недоступна ({docker_unavail_reason}). "
            "Чтение, обзор структуры папок, поиск и редактирование файлов в папке проекта открыты через MCP-сервер "
            "`hub_files` (`read_file`, `list_dir`, `find_by_name`, `grep_search`, `write_file`, `edit_file`) и штатный `apply_patch`; "
            "если требуется терминал, явно сообщи пользователю причину."
        )
    elif not sec["shell_tool"]:
        if kind == "project" and effective_access_mode == "write_files":
            dev_parts.append(
                "Хостовый терминал ПК (`shell` / `exec_command`) в режиме «Код» отключён политикой безопасности. "
                "Для чтения файлов, обзора папок, поиска и редактирования кода проекта используй MCP-сервер "
                "`hub_files` (`read_file`, `list_dir`, `find_by_name`, `grep_search`, `write_file`, `edit_file`) и штатный `apply_patch`."
            )
        else:
            dev_parts.append(
                "Режим «План» (только чтение, терминал отключён политикой безопасности): "
                "для чтения файлов, вложений диалога, обзора папок и поиска по коду используй инструменты MCP-сервера "
                "`hub_files` (`read_file`, `list_dir`, `find_by_name`, `grep_search`). Запись и модификация файлов отключены."
            )
    developer_instructions = "\n\n".join(dev_parts)

    run_state = ACTIVE_RUNS.get(chat_id)
    if run_state is None:
        run_state = register_chat_turn(
            chat_id,
            user_id=user_id,
            project_name=project_name if kind == "project" else "",
            chat_title=chat_title,
            access_mode=effective_access_mode,
            scope_key=project_scope_key if kind == "project" else "",
        )
    else:
        run_state.user_id = user_id
        run_state.project_name = project_name if kind == "project" else ""
        run_state.chat_title = chat_title
        run_state.access_mode = effective_access_mode
        run_state.scope_key = project_scope_key if kind == "project" else ""

    run_state.kind = kind
    run_state.project_dir = proj_dir
    run_state.workspace_dir = workspace_dir
    run_state.sandbox_allowed = bool(sec["allow_hub_sandbox"])
    run_state.host_allowed = bool(sec.get("allow_hub_host", False) and host_mcp_url)
    run_state.extra_dirs = list(validated_extra_dirs)
    run_state.sandbox_network = sbx_net
    run_state.model = str(model or "")
    run_state.engine = "codex"

    model_slug, reasoning_effort = parse_codex_model_id(model)
    current_account_id = str(active_pub_acc["id"])
    can_resume_existing = bool(
        codex_thread_id
        and (not codex_account_id or codex_account_id == current_account_id)
    )
    active_thread_id = codex_thread_id if can_resume_existing else ""

    tool_by_step: dict[int, dict[str, Any]] = {}
    for idx_tc, raw_tc in enumerate(normalized_initial_tools):
        tc_copy = dict(raw_tc)
        raw_sidx = tc_copy.get("step_index")
        try:
            sidx = int(raw_sidx) if raw_sidx is not None else idx_tc
        except (TypeError, ValueError):
            sidx = idx_tc
        if sidx in tool_by_step:
            sidx = (max(tool_by_step.keys()) + 1) if tool_by_step else 0
        tc_copy["step_index"] = sidx
        tool_by_step[sidx] = tc_copy
    run_state.accumulated_text = initial_content or ""
    run_state.accumulated_thinking = initial_thinking or ""
    run_state.tool_calls = [tool_by_step[k] for k in sorted(tool_by_step.keys())]
    step_counter = (max(tool_by_step.keys()) + 1) if tool_by_step else 0

    def _build_turn_prompt(*, is_fresh_thread: bool) -> str:
        pulse_block = ""
        if kind == "project" and project_name:
            pulse_block = build_project_pulse_block(
                project_name=project_name,
                chat_id=chat_id,
                is_first_turn=is_fresh_thread,
                scope_key=project_scope_key,
                changelog=project_changelog,
                recent_chats=recent_project_chats,
                prev_chat_updated_at=prev_chat_updated_at,
            )
        aug_prompt = _augment_prompt_for_mode(
            kind=kind,
            prompt=prompt,
            attachments=[] if (is_continue_turn and not is_fresh_thread) else attachments,
            is_first_turn=is_fresh_thread,
            access_mode=effective_access_mode,
            inline_hub_rules="",  # Rules already synced into `<CODEX_HOME>/AGENTS.md`
            project_pulse_block=pulse_block,
            referenced_chat_blocks=referenced_chat_blocks,
            live_voice=live_voice,
            execution_contour=exec_contour,
            docker_unavailable_reason=docker_unavail_reason,
            extra_dirs=validated_extra_dirs or None,
            project_dir=proj_dir,
            disable_default_guardrails=True,
        )
        if is_fresh_thread:
            return _build_history_recovery_prompt(
                history_messages,
                aug_prompt,
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
        return aug_prompt

    tried_accounts: set[str] = {current_account_id}
    auto_continue_attempts = 0
    capacity_retry_attempts = 0
    item_step_map: dict[str, int] = {}
    override_prompt_for_retry: Optional[str] = None

    try:
        while True:
            if run_state.cancelled:
                yield {
                    "type": "cancelled",
                    "content": run_state.accumulated_text,
                    "thinking": run_state.accumulated_thinking,
                    "tool_calls": run_state.tool_calls,
                }
                return

            secret_acc = await database.get_user_chatgpt_account_secret(user_id, current_account_id)
            if not secret_acc:
                yield {
                    "type": "error",
                    "error": "Не удалось расшифровать токен выбранного аккаунта ChatGPT. Переподключите аккаунт.",
                    "content": run_state.accumulated_text,
                    "thinking": run_state.accumulated_thinking,
                    "tool_calls": run_state.tool_calls,
                }
                return

            raw_auth = str(secret_acc["auth_json"])
            acc_dir = await asyncio.to_thread(
                ensure_user_codex_account_dir, user_id, current_account_id
            )

            def _prep_account_files() -> None:
                _write_secret_auth_file(acc_dir, raw_auth)
                write_codex_config_toml(
                    acc_dir,
                    sandbox_mode=str(sec["sandbox_mode"]),
                    shell_tool=bool(sec["shell_tool"]),
                    network_access=(sbx_net != "none"),
                    openrouter_mcp_url=or_mcp_url,
                    sandbox_mcp_url=sbx_mcp_url,
                    files_mcp_url=files_mcp_url,
                    host_mcp_url=host_mcp_url,
                    approval_policy=eff_approval_policy,
                )
                # Ensure personal rules file is also directly inside `<CODEX_HOME>/AGENTS.md`
                desired_rules = (
                    user_custom_rules.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
                    if user_custom_rules.strip()
                    else ""
                )
                config._atomic_sync_text_file(acc_dir / "AGENTS.md", desired_rules)

            try:
                account_proxy_url = await proxy_router.ensure_proxy_for_context(
                    "chatgpt",
                    user_id=user_id,
                    account_id=current_account_id,
                    is_admin=is_admin,
                )
            except proxy_router.ProxyRouteError as exc:
                yield {
                    "type": "error",
                    "error": str(exc),
                    "content": run_state.accumulated_text,
                    "thinking": run_state.accumulated_thinking,
                    "tool_calls": run_state.tool_calls,
                }
                return

            await asyncio.to_thread(_prep_account_files)
            turn_env = _build_codex_subprocess_env(
                user_or_id=user if user is not None else user_id,
                codex_home=acc_dir,
                proxy_url=account_proxy_url,
            )
            argv = _build_codex_exec_argv(codex_bin, "app-server")

            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workspace_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=turn_env,
                start_new_session=(os.name != "nt"),
                limit=4 * 1024 * 1024,
                **subprocess_hidden_kwargs(),
            )
            run_state.process = proc
            assert proc.stdout is not None
            assert proc.stderr is not None

            if not run_state.accumulated_text:
                run_state.thinking_active = True
                run_state.seq += 1
                yield {"type": "thinking_status", "active": True, "seq": run_state.seq}

            rpc_id_seq = 10
            pending_req_methods: dict[int, str] = {}
            current_turn_id = ""
            got_any_text_in_proc = False
            text_seen_this_attempt = False
            got_text_since_last_tool = False
            streamed_agent_msg_ids: set[str] = set()
            active_agent_msg_id = ""
            turn_completed = False
            turn_status = ""
            turn_error_msg: Optional[str] = None
            turn_error_code = ""
            watchdog_network_idle = False
            watchdog_post_text_idle = False
            final_usage: dict[str, Any] = {}
            live_quotas_pushed: Optional[dict[str, Any]] = None
            resume_fallback_triggered = False

            known_file_changes: dict[str, list[str]] = {}

            def _next_rpc_id(method_name: str) -> int:
                nonlocal rpc_id_seq
                rpc_id_seq += 1
                pending_req_methods[rpc_id_seq] = method_name
                return rpc_id_seq

            async def _start_new_thread_req() -> None:
                req_id = _next_rpc_id("thread/start")
                params_ts: dict[str, Any] = {
                    "model": model_slug,
                    "cwd": str(workspace_dir),
                    "approvalPolicy": eff_approval_policy,
                    "sandbox": str(sec["sandbox_mode"]),
                }
                if developer_instructions:
                    params_ts["developerInstructions"] = developer_instructions
                await _rpc_send_json(
                    proc,
                    {"jsonrpc": "2.0", "id": req_id, "method": "thread/start", "params": params_ts},
                )

            async def _start_turn_req(t_id: str, is_fresh_thread: bool) -> None:
                req_id = _next_rpc_id("turn/start")
                prompt_to_send = (
                    override_prompt_for_retry
                    if override_prompt_for_retry is not None
                    else _build_turn_prompt(is_fresh_thread=is_fresh_thread)
                )
                input_items = _build_turn_input_items(
                    prompt_to_send,
                    []
                    if (
                        override_prompt_for_retry is not None
                        or (is_continue_turn and not is_fresh_thread)
                    )
                    else attachments,
                )
                params_turn: dict[str, Any] = {
                    "threadId": t_id,
                    "model": model_slug,
                    "effort": reasoning_effort,
                    "cwd": str(workspace_dir),
                    "approvalPolicy": eff_approval_policy,
                    "sandboxPolicy": sec["turn_sandbox_policy"],
                    "input": input_items,
                }
                await _rpc_send_json(
                    proc,
                    {"jsonrpc": "2.0", "id": req_id, "method": "turn/start", "params": params_turn},
                )

            try:
                init_id = _next_rpc_id("initialize")
                await _rpc_send_json(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": init_id,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "antigravity_hub", "version": "1.5.0"},
                            "capabilities": {"experimentalApi": True},
                        },
                    },
                )

                while True:
                    if run_state.cancelled:
                        if current_turn_id and active_thread_id and proc.returncode is None:
                            try:
                                await _rpc_send_json(
                                    proc,
                                    {
                                        "jsonrpc": "2.0",
                                        "id": _next_rpc_id("turn/interrupt"),
                                        "method": "turn/interrupt",
                                        "params": {
                                            "threadId": active_thread_id,
                                            "turnId": current_turn_id,
                                        },
                                    },
                                )
                                await asyncio.sleep(0.35)
                            except Exception:
                                pass
                        break

                    has_active_tool_now = any(
                        tc.get("state") == "ACTIVE" for tc in run_state.tool_calls
                    )
                    if has_active_tool_now:
                        read_timeout = STREAM_ACTIVE_TOOL_TIMEOUT_SEC
                        post_text_idle_candidate = False
                    elif (
                        got_any_text_in_proc
                        and got_text_since_last_tool
                        and looks_like_completed_response(run_state.accumulated_text)
                    ):
                        read_timeout = STREAM_POST_TEXT_IDLE_TIMEOUT_SEC
                        post_text_idle_candidate = True
                    else:
                        read_timeout = STREAM_NETWORK_IDLE_TIMEOUT_SEC
                        post_text_idle_candidate = False

                    try:
                        raw_line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=read_timeout
                        )
                    except asyncio.TimeoutError:
                        if post_text_idle_candidate:
                            watchdog_post_text_idle = True
                        else:
                            watchdog_network_idle = True
                            turn_error_msg = "stream idle watchdog timeout"
                        break

                    if not raw_line:
                        break

                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(msg, dict):
                        continue

                    # 1. Server-to-Client JSON-RPC Request: Zero-Trust policy evaluation
                    if "id" in msg and "method" in msg:
                        rpc_reply = evaluate_codex_server_request(
                            msg,
                            trusted_mcp_servers=trusted_turn_mcps,
                            shell_tool=bool(sec["shell_tool"]),
                            effective_access_mode=effective_access_mode,
                            kind=kind,
                            workspace_dir=workspace_dir,
                            known_file_changes=known_file_changes,
                            extra_dirs=validated_extra_dirs or None,
                        )
                        await _rpc_send_json(proc, rpc_reply)
                        continue

                    # 2. JSON-RPC Response to our request
                    if "id" in msg and ("result" in msg or "error" in msg):
                        rid = msg.get("id")
                        req_method = pending_req_methods.pop(rid, "") if isinstance(rid, int) else ""
                        err_obj = msg.get("error") if isinstance(msg.get("error"), dict) else None

                        if req_method == "initialize":
                            if err_obj:
                                turn_error_msg = str(err_obj.get("message") or "Ошибка initialize codex app-server")
                                break
                            await _rpc_send_json(
                                proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}}
                            )
                            if active_thread_id:
                                res_id = _next_rpc_id("thread/resume")
                                params_tr: dict[str, Any] = {
                                    "threadId": active_thread_id,
                                    "model": model_slug,
                                    "cwd": str(workspace_dir),
                                    "approvalPolicy": eff_approval_policy,
                                    "sandbox": str(sec["sandbox_mode"]),
                                    "excludeTurns": True,
                                }
                                if developer_instructions:
                                    params_tr["developerInstructions"] = developer_instructions
                                await _rpc_send_json(
                                    proc,
                                    {
                                        "jsonrpc": "2.0",
                                        "id": res_id,
                                        "method": "thread/resume",
                                        "params": params_tr,
                                    },
                                )
                            else:
                                await _start_new_thread_req()

                        elif req_method == "thread/resume":
                            if err_obj:
                                # Transparently fall back to `thread/start` with `_build_history_recovery_prompt`!
                                active_thread_id = ""
                                resume_fallback_triggered = True
                                yield {"type": "account_session_recovered"}
                                await _start_new_thread_req()
                            else:
                                res_obj = msg.get("result") or {}
                                th_obj = res_obj.get("thread") if isinstance(res_obj, dict) else {}
                                tid = str((th_obj or {}).get("id") or active_thread_id).strip()
                                active_thread_id = tid
                                yield {
                                    "type": "codex_session",
                                    "thread_id": active_thread_id,
                                    "codex_thread_id": active_thread_id,
                                    "account_id": current_account_id,
                                    "codex_account_id": current_account_id,
                                }
                                await _start_turn_req(active_thread_id, is_fresh_thread=False)

                        elif req_method == "thread/start":
                            if err_obj:
                                turn_error_msg = str(err_obj.get("message") or "Ошибка thread/start в Codex CLI")
                                break
                            res_obj = msg.get("result") or {}
                            th_obj = res_obj.get("thread") if isinstance(res_obj, dict) else {}
                            tid = str((th_obj or {}).get("id") or "").strip()
                            if not tid:
                                turn_error_msg = "Codex app-server не вернул thread.id"
                                break
                            active_thread_id = tid
                            yield {
                                "type": "codex_session",
                                "thread_id": active_thread_id,
                                "codex_thread_id": active_thread_id,
                                "account_id": current_account_id,
                                "codex_account_id": current_account_id,
                            }
                            await _start_turn_req(
                                active_thread_id,
                                is_fresh_thread=True,
                            )

                        elif req_method == "turn/start":
                            if err_obj:
                                turn_error_msg = str(err_obj.get("message") or "Ошибка запуска хода Codex CLI")
                                data_obj = err_obj.get("data")
                                if isinstance(data_obj, dict):
                                    turn_error_code = str(data_obj.get("codexErrorInfo") or "")
                                break
                            res_obj = msg.get("result") or {}
                            turn_obj = res_obj.get("turn") if isinstance(res_obj, dict) else {}
                            current_turn_id = str((turn_obj or {}).get("id") or "").strip()
                        continue

                    # 3. JSON-RPC Notifications
                    method = str(msg.get("method") or "")
                    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

                    if method == "turn/started":
                        t_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                        if t_obj.get("id"):
                            current_turn_id = str(t_obj["id"])

                    elif method == "item/reasoning/summaryPartAdded":
                        if run_state.accumulated_thinking and not run_state.accumulated_thinking.endswith("\n\n"):
                            run_state.accumulated_thinking += "\n\n"

                    elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
                        r_delta = str(params.get("delta") or "")
                        if r_delta:
                            if not run_state.thinking_active and not got_any_text_in_proc:
                                run_state.thinking_active = True
                                run_state.seq += 1
                                yield {"type": "thinking_status", "active": True, "seq": run_state.seq}
                            run_state.accumulated_thinking += r_delta
                            run_state.seq += 1
                            yield {
                                "type": "thinking_update",
                                "thinking": run_state.accumulated_thinking,
                                "seq": run_state.seq,
                            }

                    elif method in ("item/started", "item/updated", "item/completed"):
                        item_obj = params.get("item") if isinstance(params.get("item"), dict) else {}
                        item_id = str(item_obj.get("id") or f"anon_{step_counter + 1}")
                        itype = str(item_obj.get("type") or "")
                        if itype == "fileChange":
                            fc_paths = _extract_file_change_item_paths(item_obj)
                            if fc_paths:
                                known_file_changes[item_id] = fc_paths

                        if itype == "agentMessage":
                            if method == "item/completed":
                                full_msg_txt = str(item_obj.get("text") or "")
                                if full_msg_txt and item_id not in streamed_agent_msg_ids and (
                                    not got_any_text_in_proc
                                    or not got_text_since_last_tool
                                    or (active_agent_msg_id and item_id != active_agent_msg_id)
                                ):
                                    if run_state.thinking_active:
                                        run_state.thinking_active = False
                                        run_state.seq += 1
                                        yield {"type": "thinking_status", "active": False, "seq": run_state.seq}
                                    if not text_seen_this_attempt:
                                        full_msg_txt = (
                                            compute_resume_stitch_prefix(run_state.accumulated_text, full_msg_txt)
                                            + full_msg_txt
                                        )
                                    elif (
                                        run_state.accumulated_text
                                        and not run_state.accumulated_text.endswith("\n")
                                        and not full_msg_txt.startswith("\n")
                                    ):
                                        full_msg_txt = "\n\n" + full_msg_txt.lstrip(" ")
                                    streamed_agent_msg_ids.add(item_id)
                                    active_agent_msg_id = item_id
                                    got_any_text_in_proc = True
                                    text_seen_this_attempt = True
                                    got_text_since_last_tool = True
                                    run_state.accumulated_text += full_msg_txt
                                    run_state.seq += 1
                                    yield {
                                        "type": "text_delta",
                                        "delta": full_msg_txt,
                                        "content": run_state.accumulated_text,
                                        "seq": run_state.seq,
                                    }
                            continue

                        if itype == "reasoning":
                            if method == "item/completed" and not run_state.accumulated_thinking:
                                summary_arr = item_obj.get("summary")
                                if isinstance(summary_arr, list) and summary_arr:
                                    joined_s = "\n\n".join(str(x).strip() for x in summary_arr if str(x).strip())
                                    if joined_s:
                                        run_state.accumulated_thinking = joined_s
                                        run_state.seq += 1
                                        yield {
                                            "type": "thinking_update",
                                            "thinking": joined_s,
                                            "seq": run_state.seq,
                                        }
                            continue

                        if item_id not in item_step_map:
                            step_counter += 2  # Gap > 1 reflects sequential tool pass
                            item_step_map[item_id] = step_counter
                        base_step = item_step_map[item_id]

                        norm_list = _normalize_codex_tool_item(
                            item_obj, step_index=base_step, event_method=method
                        )
                        if norm_list:
                            got_text_since_last_tool = False
                            for entry in norm_list:
                                s_idx = int(entry["step_index"])
                                tool_by_step[s_idx] = entry
                                run_state.tool_calls = [
                                    tool_by_step[k] for k in sorted(tool_by_step.keys())
                                ]
                                touched_paths = extract_writing_tool_paths(
                                    entry["name"], entry.get("parameters"), proj_dir
                                )
                                for rel_p in touched_paths:
                                    run_state.active_files.add(rel_p)
                                run_state.seq += 1
                                yield {
                                    "type": "tool_step",
                                    "step": entry,
                                    "tool_calls": list(run_state.tool_calls),
                                    "seq": run_state.seq,
                                }

                    elif method == "item/agentMessage/delta":
                        tdelta = str(params.get("delta") or "")
                        delta_item_id = str(params.get("itemId") or params.get("id") or "")
                        if tdelta:
                            new_msg_item = bool(
                                delta_item_id and active_agent_msg_id and delta_item_id != active_agent_msg_id
                            )
                            if run_state.thinking_active:
                                run_state.thinking_active = False
                                run_state.seq += 1
                                yield {"type": "thinking_status", "active": False, "seq": run_state.seq}
                            if not text_seen_this_attempt:
                                tdelta = compute_resume_stitch_prefix(run_state.accumulated_text, tdelta) + tdelta
                            elif (not got_text_since_last_tool or new_msg_item) and (
                                run_state.accumulated_text
                                and not run_state.accumulated_text.endswith("\n")
                                and not tdelta.startswith("\n")
                            ):
                                tdelta = "\n\n" + tdelta.lstrip(" ")
                            if delta_item_id:
                                streamed_agent_msg_ids.add(delta_item_id)
                                active_agent_msg_id = delta_item_id
                            got_any_text_in_proc = True
                            got_text_since_last_tool = True
                            text_seen_this_attempt = True
                            run_state.accumulated_text += tdelta
                            run_state.seq += 1
                            yield {
                                "type": "text_delta",
                                "delta": tdelta,
                                "content": run_state.accumulated_text,
                                "seq": run_state.seq,
                            }

                    elif method in ("thread/tokenUsage/updated", "turn/tokenUsage/updated"):
                        parsed_u = _extract_token_usage_dict(params)
                        if parsed_u:
                            final_usage = parsed_u

                    elif method == "account/rateLimits/updated":
                        parsed_rl = parse_codex_rate_limits_payload(params)
                        if parsed_rl:
                            live_quotas_pushed = parsed_rl

                    elif method == "error":
                        err_p = params.get("error") if isinstance(params.get("error"), dict) else {}
                        will_retry = bool(params.get("willRetry"))
                        if not will_retry:
                            turn_error_msg = str(
                                err_p.get("message") or params.get("message") or "Ошибка Codex CLI"
                            )
                            info_raw = err_p.get("codexErrorInfo")
                            if isinstance(info_raw, str):
                                turn_error_code = info_raw
                            elif isinstance(info_raw, dict) and info_raw:
                                turn_error_code = str(next(iter(info_raw.keys())))

                    elif method == "turn/completed":
                        t_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                        turn_status = str(t_obj.get("status") or "completed").strip().lower()
                        t_err = t_obj.get("error") if isinstance(t_obj.get("error"), dict) else None
                        if t_err:
                            turn_error_msg = str(t_err.get("message") or turn_error_msg or "Ошибка выполнения хода")
                            info_raw = t_err.get("codexErrorInfo")
                            if isinstance(info_raw, str):
                                turn_error_code = info_raw
                            elif isinstance(info_raw, dict) and info_raw:
                                turn_error_code = str(next(iter(info_raw.keys())))
                        turn_completed = True
                        break

                if (
                    live_quotas_pushed is None
                    and not run_state.cancelled
                    and not watchdog_network_idle
                    and proc is not None
                    and proc.returncode is None
                ):
                    live_quotas_pushed = await _read_rate_limits_from_open_proc(
                        proc, timeout_sec=2.5
                    )

                stderr_text = ""
                if not turn_completed and not run_state.cancelled and not watchdog_network_idle and not watchdog_post_text_idle:
                    try:
                        sb = await asyncio.wait_for(proc.stderr.read(), timeout=1.5)
                        stderr_text = sb.decode("utf-8", errors="replace").strip()
                    except Exception:
                        stderr_text = ""
            finally:
                await _terminate_codex_proc_tree(proc)
                await _sync_back_refreshed_auth_json(user_id, current_account_id, acc_dir, raw_auth)
                if live_quotas_pushed:
                    turn_failed_in_proc = bool(
                        turn_status == "failed"
                        or (not turn_completed and not watchdog_post_text_idle)
                        or turn_error_msg
                    )
                    if watchdog_post_text_idle and run_state.accumulated_text.strip():
                        turn_failed_in_proc = False
                    try:
                        await database.update_user_chatgpt_account_cache(
                            user_id,
                            current_account_id,
                            quotas=live_quotas_pushed,
                            plan_type=str(live_quotas_pushed.get("plan_type") or "") or None,
                            last_error=None if turn_failed_in_proc else "",
                        )
                    except Exception:
                        pass

            if run_state.cancelled or turn_status == "interrupted":
                yield {
                    "type": "cancelled",
                    "content": run_state.accumulated_text,
                    "thinking": run_state.accumulated_thinking,
                    "tool_calls": run_state.tool_calls,
                }
                return

            err_combined = f"{turn_error_msg or ''} {stderr_text}".strip()
            has_failed = bool(
                turn_status == "failed"
                or (not turn_completed and not watchdog_post_text_idle)
                or turn_error_msg
            )

            if watchdog_post_text_idle and run_state.accumulated_text.strip():
                has_failed = False

            is_quota_err = bool(
                has_failed and _is_chatgpt_quota_exhausted_error(err_combined, turn_error_code)
            )
            if is_quota_err:
                exhausted_quotas = _clamp_exhausted_chatgpt_quotas(
                    secret_acc.get("quotas") or {},
                    err_combined,
                    turn_error_code,
                    live_quotas=live_quotas_pushed,
                )
                can_failover = (
                    not got_any_text_in_proc
                    and len(run_state.tool_calls) == len(normalized_initial_tools)
                    and bool(secret_acc.get("auto_failover", True))
                )
                next_acc = (
                    await _pick_next_failover_account(user_id, tried_accounts)
                    if can_failover
                    else None
                )
                quota_err_note = (
                    "Лимит окна ChatGPT исчерпан (выполнено автопереключение)"
                    if next_acc is not None
                    else (turn_error_msg or stderr_text or "Лимит окна ChatGPT исчерпан").strip()[:300]
                )
                await database.update_user_chatgpt_account_cache(
                    user_id,
                    current_account_id,
                    quotas=exhausted_quotas,
                    last_error=quota_err_note,
                )
                if next_acc is not None:
                    prev_id = current_account_id
                    next_id = str(next_acc["id"])
                    tried_accounts.add(next_id)
                    current_account_id = next_id
                    active_thread_id = ""
                    override_prompt_for_retry = None
                    yield {"type": "codex_session", "thread_id": "", "codex_thread_id": "",
                           "account_id": current_account_id, "codex_account_id": current_account_id,
                           "reset_session": True}
                    yield {
                        "type": "chatgpt_failover",
                        "from_label": str(secret_acc.get("label") or secret_acc.get("email") or prev_id),
                        "from_account_id": prev_id,
                        "to_account_id": next_id,
                        "to_label": str(next_acc.get("label") or next_acc.get("email") or next_id),
                    }
                    continue

            # Transient capacity / network stream interruption Auto-Continue
            is_cap = is_transient_capacity_error(err_combined)
            is_trans = watchdog_network_idle or is_transient_stream_error(err_combined)
            if has_failed and not is_quota_err and (is_cap or is_trans):
                if not run_state.accumulated_text.strip() and capacity_retry_attempts < 2:
                    backoff = (
                        CAPACITY_RETRY_BACKOFF_SECS[0]
                        if capacity_retry_attempts == 0
                        else CAPACITY_RETRY_BACKOFF_SECS[1]
                    )
                    capacity_retry_attempts += 1
                    if not await _sleep_unless_cancelled(backoff, run_state):
                        yield {
                            "type": "cancelled",
                            "content": run_state.accumulated_text,
                            "thinking": run_state.accumulated_thinking,
                            "tool_calls": run_state.tool_calls,
                        }
                        return
                    continue

                if active_thread_id and auto_continue_attempts < 2:
                    auto_continue_attempts += 1
                    for k_idx in sorted(tool_by_step.keys()):
                        if tool_by_step[k_idx].get("state") == "ACTIVE":
                            tool_by_step[k_idx] = {**tool_by_step[k_idx], "state": "INTERRUPTED"}
                    run_state.tool_calls = [tool_by_step[k] for k in sorted(tool_by_step.keys())]
                    if not await _sleep_unless_cancelled(AUTO_CONTINUE_PAUSE_SEC, run_state):
                        yield {
                            "type": "cancelled",
                            "content": run_state.accumulated_text,
                            "thinking": run_state.accumulated_thinking,
                            "tool_calls": run_state.tool_calls,
                        }
                        return
                    tail = run_state.accumulated_text.strip()[-180:]
                    override_prompt_for_retry = (
                        "The upstream network stream timed out mid-turn. Continue the task from where it stopped "
                        "without repeating completed tool steps."
                        + (
                            f" Note: the user already sees text ending with: «...{tail}...» — continue seamlessly right after that point."
                            if tail
                            else ""
                        )
                    )
                    continue

            if has_failed:
                clean_err = (turn_error_msg or stderr_text or "Ошибка выполнения запроса в Codex CLI").strip()
                if not is_quota_err:
                    await database.update_user_chatgpt_account_cache(
                        user_id, current_account_id, last_error=clean_err[:300]
                    )
                err_evt: dict[str, Any] = {
                    "type": "error",
                    "error": clean_err,
                    "content": run_state.accumulated_text,
                    "thinking": run_state.accumulated_thinking,
                    "tool_calls": run_state.tool_calls,
                }
                if is_quota_err:
                    err_evt["quota_exhausted"] = True
                yield err_evt
                return

            # Turn succeeded! Clear any stale last_error if we didn't already update quotas
            if not live_quotas_pushed and secret_acc.get("last_error"):
                await database.update_user_chatgpt_account_cache(
                    user_id, current_account_id, last_error=""
                )

            if not final_usage:
                est_in = max(1, len(prompt) // 3)
                est_out = max(1, len(run_state.accumulated_text) // 3)
                final_usage = {
                    "input_tokens": est_in,
                    "cache_read_tokens": 0,
                    "output_tokens": est_out,
                    "thinking_tokens": 0,
                    "total_tokens": est_in + est_out,
                    "model_context_window": 272_000,
                }
            final_usage["provider"] = "chatgpt_codex"
            final_usage["codex_account_id"] = current_account_id
            if "model_context_window" not in final_usage:
                final_usage["model_context_window"] = 272_000

            yield {
                "type": "done",
                "content": run_state.accumulated_text,
                "thinking": run_state.accumulated_thinking,
                "tool_calls": run_state.tool_calls,
                "usage": final_usage,
                "codex_thread_id": active_thread_id,
                "codex_account_id": current_account_id,
            }
            return
    finally:
        try:
            from .sandbox_service import cancel_chat_sandbox_execs

            await cancel_chat_sandbox_execs(chat_id)
        except Exception:
            pass
        ACTIVE_RUNS.pop(chat_id, None)


async def run_one_shot_codex_prompt(
    *,
    user_id: str,
    prompt: str,
    model_id: str = "codex:gpt-5.4-mini@low",
    timeout_sec: float = 90.0,
) -> dict[str, Any]:
    """Run a fast, strictly read-only, tool-disabled single turn on `user_id`'s active ChatGPT account.
    Used for `/compact` summarization and `✨ Собрать автоматически` project passport generation."""
    codex_bin = resolve_codex_bin()
    if not codex_bin:
        raise RuntimeError("На сервере не установлен OpenAI Codex CLI (@openai/codex)")

    active_pub = await database.get_active_user_chatgpt_account(user_id, require_enabled=True)
    if not active_pub:
        raise RuntimeError("У пользователя нет активного аккаунта ChatGPT")

    acc_id = str(active_pub["id"])
    secret_acc = await database.get_user_chatgpt_account_secret(user_id, acc_id)
    if not secret_acc:
        raise RuntimeError("Не удалось расшифровать авторизацию ChatGPT")

    raw_auth = str(secret_acc["auth_json"])
    acc_dir = await asyncio.to_thread(ensure_user_codex_account_dir, user_id, acc_id)
    await asyncio.to_thread(_write_secret_auth_file, acc_dir, raw_auth)
    await asyncio.to_thread(
        write_codex_config_toml, acc_dir, sandbox_mode="read-only", shell_tool=False
    )

    acc_proxy_url = await proxy_router.ensure_proxy_for_context(
        "chatgpt",
        user_id=user_id,
        account_id=acc_id,
    )
    CHAT_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    env = _build_codex_subprocess_env(
        user_or_id=user_id,
        codex_home=acc_dir,
        proxy_url=acc_proxy_url,
    )
    argv = _build_codex_exec_argv(codex_bin, "app-server")
    slug, effort = parse_codex_model_id(model_id)

    proc: Optional[asyncio.subprocess.Process] = None
    accumulated = ""
    usage_out: dict[str, Any] = {}
    err_msg = ""

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(CHAT_SANDBOX_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=(os.name != "nt"),
            limit=4 * 1024 * 1024,
            **subprocess_hidden_kwargs(),
        )
        assert proc.stdout is not None

        async def _consume() -> None:
            nonlocal accumulated, usage_out, err_msg
            await _rpc_send_json(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "antigravity_hub", "version": "1.5.0"}, "capabilities": None},
                },
            )
            while True:
                raw_line = await proc.stdout.readline()  # type: ignore[union-attr]
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                if rid == 1:
                    await _rpc_send_json(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
                    await _rpc_send_json(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "thread/start",
                            "params": {
                                "model": slug,
                                "cwd": str(CHAT_SANDBOX_DIR),
                                "approvalPolicy": "never",
                                "sandbox": "read-only",
                            },
                        },
                    )
                elif rid == 2:
                    if isinstance(msg.get("error"), dict):
                        err_msg = str(msg["error"].get("message") or "Ошибка thread/start")
                        break
                    th = (msg.get("result") or {}).get("thread") or {}
                    tid = str(th.get("id") or "")
                    await _rpc_send_json(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "turn/start",
                            "params": {
                                "threadId": tid,
                                "model": slug,
                                "effort": effort,
                                "cwd": str(CHAT_SANDBOX_DIR),
                                "approvalPolicy": "never",
                                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                            },
                        },
                    )
                elif rid == 3 and isinstance(msg.get("error"), dict):
                    err_msg = str(msg["error"].get("message") or "Ошибка turn/start")
                    break
                else:
                    method = str(msg.get("method") or "")
                    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                    if method == "item/agentMessage/delta":
                        accumulated += str(params.get("delta") or "")
                    elif method == "item/completed":
                        it = params.get("item") if isinstance(params.get("item"), dict) else {}
                        if it.get("type") == "agentMessage" and not accumulated:
                            accumulated = str(it.get("text") or "")
                    elif method in ("thread/tokenUsage/updated", "turn/tokenUsage/updated"):
                        u = _extract_token_usage_dict(params)
                        if u:
                            usage_out = u
                    elif method == "turn/completed":
                        t_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                        if isinstance(t_obj.get("error"), dict):
                            err_msg = str(t_obj["error"].get("message") or "Ошибка выполнения")
                        break

        await asyncio.wait_for(_consume(), timeout=timeout_sec)
    finally:
        await _terminate_codex_proc_tree(proc)
        await _sync_back_refreshed_auth_json(user_id, acc_id, acc_dir, raw_auth)

    if err_msg and not accumulated.strip():
        raise RuntimeError(err_msg)
    if not accumulated.strip():
        raise RuntimeError("Модель ChatGPT вернула пустой ответ")
    usage_out["provider"] = "chatgpt_codex"
    usage_out["codex_account_id"] = acc_id
    return {
        "text": accumulated.strip(),
        "usage": usage_out,
        "model": model_id,
    }


def pick_fast_codex_utility_model(account: Optional[dict[str, Any]], preferred: str = "") -> str:
    models = get_models_for_chatgpt_account(account)
    ids = [str(m.get("id") or "") for m in models if m.get("id")]
    id_set = set(ids)
    for cand in (
        "codex:gpt-5.4-mini@low",
        "codex:gpt-5.4@low",
        "codex:gpt-5.4-mini@high",
        "codex:gpt-5.4@medium",
        "codex:gpt-5.3-codex@medium",
    ):
        if cand in id_set:
            return cand
    if preferred and is_codex_model(preferred) and preferred in id_set:
        return preferred
    return ids[-1] if ids else "codex:gpt-5.4@low"
