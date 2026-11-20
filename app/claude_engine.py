"""
Anthropic Claude Code CLI (`@anthropic-ai/claude-code`) Orchestration Engine for Antigravity Hub (v87).

Provides (in full symmetry with `codex_engine.py`):
- Personal Claude Pro / Max / Team subscriptions ($0 per token) via `claude -p --output-format stream-json`.
- Strict per-user, per-account isolated `CLAUDE_CONFIG_DIR`
  (`data/cli_homes/<safe_uid>/.claude/accounts/<safe_acc_id>/`), never `%USERPROFILE%\\.claude`.
- 0% GPU (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`) and 0 console popups (`subprocess_hidden_kwargs()`).
- 3 login paths: remote OAuth (`claude auth login`, URL + code), `claude setup-token` (`sk-ant-oat...`), `.credentials.json` import.
- Live 5h / 7d subscription quotas (`GET https://api.anthropic.com/api/oauth/usage`).
- Security matrix (`--permission-mode`, `--disallowedTools`, `--allowedTools`) + strict MCP (`--mcp-config ... --strict-mcp-config`).
- Unified Hub stream contract (`claude_session`, `thinking_status`, `thinking_update`, `tool_step`, `text_delta`,
  `claude_failover`, `done`, `cancelled`, `error`), multi-account auto-failover, lost-session recovery and Auto-Continue.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from . import config, database, proxy_router
from .agy_engine import (
    ACTIVE_RUNS,
    AUTO_CONTINUE_PAUSE_SEC,
    CAPACITY_RETRY_BACKOFF_SECS,
    MAX_CONSECUTIVE_AUTO_CONTINUES,
    MAX_TOTAL_AUTO_CONTINUES,
    STREAM_ACTIVE_TOOL_TIMEOUT_SEC,
    STREAM_NETWORK_IDLE_TIMEOUT_SEC,
    STREAM_POST_TEXT_IDLE_TIMEOUT_SEC,
    STREAM_STARTUP_IDLE_TIMEOUT_SEC,
    _augment_prompt_for_mode,
    _build_history_recovery_prompt,
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
from .brain_reader import (
    ensure_isolated_project_git_boundary,
    resolve_effective_project_execution_mode,
)
from .config import (
    CHAT_SANDBOX_DIR,
    CLAUDE_TOOLS_DIR,
    IS_WINDOWS,
    UPLOADS_DIR,
    _find_npm_bin,
    apply_isolated_home_to_env,
    build_claude_exec_argv,
    ensure_user_claude_account_dir,
    ensure_user_claude_home,
    ensure_user_cli_home,
    get_claude_cli_info,
    resolve_claude_bin,
    resolve_outbound_proxy,
    subprocess_hidden_kwargs,
    sync_user_cli_rules,
)

# ---------------------------------------------------------------------------
# 5.1 Model namespace
# ---------------------------------------------------------------------------

CLAUDE_CODE_MODEL_PREFIX = "claude-code:"
VALID_CLAUDE_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
CLAUDE_CODE_CONTEXT_WINDOW = 200_000
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"

DEFAULT_CLAUDE_CODE_MODELS: list[dict[str, Any]] = [
    {"id": "claude-code:sonnet@high", "label": "Claude Sonnet (High)", "family": "claude_code", "context_window": 200_000},
    {"id": "claude-code:sonnet@medium", "label": "Claude Sonnet (Medium)", "family": "claude_code", "context_window": 200_000},
    {"id": "claude-code:sonnet@low", "label": "Claude Sonnet (Low)", "family": "claude_code", "context_window": 200_000},
    {"id": "claude-code:opus@high", "label": "Claude Opus (High)", "family": "claude_code", "context_window": 200_000},
    {"id": "claude-code:opus@medium", "label": "Claude Opus (Medium)", "family": "claude_code", "context_window": 200_000},
    {"id": "claude-code:haiku@low", "label": "Claude Haiku (Low)", "family": "claude_code", "context_window": 200_000},
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_OAUTH_URL_RE = re.compile(
    r"(https://(?:claude\.ai|console\.anthropic\.com)/[^\s\"'<>)\]]+)",
    re.IGNORECASE,
)
_INSTALL_LOCK = asyncio.Lock()
_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-\[\]]{0,79}$")


def is_claude_code_model(model_id: Optional[str]) -> bool:
    return str(model_id or "").strip().lower().startswith(CLAUDE_CODE_MODEL_PREFIX)


def parse_claude_code_model_spec(model_id: str) -> tuple[str, str]:
    """`'claude-code:opus@medium'` -> `('opus', 'medium')`; default effort `high`."""
    raw = str(model_id or "").strip()
    if raw.lower().startswith(CLAUDE_CODE_MODEL_PREFIX):
        raw = raw[len(CLAUDE_CODE_MODEL_PREFIX):].strip()
    if "@" in raw:
        slug, eff = raw.rsplit("@", 1)
    else:
        slug, eff = raw, ""
    slug = slug.strip() or "sonnet"
    if not _SAFE_SLUG_RE.match(slug):
        slug = "sonnet"
    eff = eff.strip().lower()
    if eff not in VALID_CLAUDE_EFFORTS:
        eff = "high"
    return slug, eff


def _humanize_claude_slug(slug: str) -> str:
    low = slug.lower()
    for fam in ("opus", "sonnet", "haiku"):
        if low == fam:
            return f"Claude {fam.capitalize()}"
    if low.startswith("claude-"):
        parts = low.split("-")[1:]
        if parts:
            fam = parts[0].capitalize()
            ver = ".".join(p for p in parts[1:] if p.isdigit() and len(p) <= 2)
            return f"Claude {fam} {ver}".strip()
    return slug


def build_claude_model_entry(model_id: str) -> dict[str, Any]:
    slug, eff = parse_claude_code_model_spec(model_id)
    return {
        "id": f"{CLAUDE_CODE_MODEL_PREFIX}{slug}@{eff}",
        "label": f"{_humanize_claude_slug(slug)} ({eff.capitalize()})",
        "family": "claude_code",
        "context_window": CLAUDE_CODE_CONTEXT_WINDOW,
    }


def get_models_for_claude_account(account: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not account or not account.get("is_enabled", True):
        return []
    cached = account.get("models")
    if isinstance(cached, list):
        valid = [m for m in cached if isinstance(m, dict) and is_claude_code_model(str(m.get("id") or ""))]
        if valid:
            return valid
    return [dict(m) for m in DEFAULT_CLAUDE_CODE_MODELS]


async def get_claude_models_for_user(user_id: str) -> list[dict[str, Any]]:
    """`claude-code:*` entries if the user has at least one enabled Claude account (else `[]`)."""
    uid = str(user_id or "").strip()
    if not uid:
        return []
    try:
        accounts = await database.list_user_claude_accounts(uid)
    except Exception:
        return []
    enabled = [a for a in accounts if a.get("is_enabled")]
    if not enabled:
        return []
    active = next((a for a in enabled if a.get("is_active")), enabled[0])
    return get_models_for_claude_account(active)


def pick_fast_claude_utility_model(account: Optional[dict[str, Any]] = None, preferred: str = "") -> str:
    ids = {str(m.get("id") or "") for m in get_models_for_claude_account(account or {"is_enabled": True})}
    for cand in ("claude-code:haiku@low", "claude-code:sonnet@low"):
        if cand in ids:
            return cand
    if preferred and is_claude_code_model(preferred):
        return preferred
    return "claude-code:sonnet@low"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 5.2 Isolated env / materialization / token back-sync
# ---------------------------------------------------------------------------


_UNSET_PROXY: Any = object()


def build_claude_env(
    user_or_id: Any,
    config_dir: Path,
    *,
    oauth_token: str = "",
    proxy_url: Any = _UNSET_PROXY,
) -> dict[str, str]:
    env = os.environ.copy()
    cli_home = ensure_user_cli_home(user_or_id)
    apply_isolated_home_to_env(env, cli_home)
    env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).resolve())
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    env["BROWSER"] = "echo"
    env["CI"] = "1"
    env["CUDA_VISIBLE_DEVICES"] = ""
    env["ORT_DISABLE_ALL_GPU"] = "1"
    # Guarantee the subscription path ($0), never API balance
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    eff_proxy = (
        proxy_router.resolve_global_proxy_sync()
        if proxy_url is _UNSET_PROXY
        else proxy_url
    )
    proxy_router.apply_proxy_to_subprocess_env(env, eff_proxy)
    return env


def parse_claude_credentials_payload(raw: str) -> dict[str, Any]:
    """Classify stored credentials: `{"kind": "oauth"|"setup_token", "oauth": {...}, "token": str}`."""
    clean = (raw or "").strip()
    if not clean:
        return {"kind": "", "oauth": {}, "token": ""}
    if clean.startswith("sk-ant-oat"):
        return {"kind": "setup_token", "oauth": {}, "token": clean.split()[0]}
    try:
        obj = json.loads(clean)
    except Exception:
        return {"kind": "", "oauth": {}, "token": ""}
    if not isinstance(obj, dict):
        return {"kind": "", "oauth": {}, "token": ""}
    if obj.get("oauthToken") and not (obj.get("claudeAiOauth") or {}).get("refreshToken"):
        return {"kind": "setup_token", "oauth": {}, "token": str(obj["oauthToken"]).strip()}
    oauth = obj.get("claudeAiOauth") if isinstance(obj.get("claudeAiOauth"), dict) else None
    if oauth is None and (obj.get("accessToken") or obj.get("access_token")):
        oauth = {
            "accessToken": obj.get("accessToken") or obj.get("access_token"),
            "refreshToken": obj.get("refreshToken") or obj.get("refresh_token") or "",
            "expiresAt": obj.get("expiresAt") or obj.get("expires_at") or 0,
            "scopes": obj.get("scopes") or [],
            "subscriptionType": obj.get("subscriptionType") or "",
        }
    if isinstance(oauth, dict) and oauth.get("accessToken"):
        return {"kind": "oauth", "oauth": oauth, "token": str(oauth.get("accessToken") or "")}
    return {"kind": "", "oauth": {}, "token": ""}


def _atomic_write_secret(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


_DEFAULT_SETTINGS = {
    "includeCoAuthoredBy": False,
    "env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"},
}


def _ensure_settings_files(acc_dir: Path) -> None:
    settings = acc_dir / "settings.json"
    if not settings.exists():
        settings.write_text(json.dumps(_DEFAULT_SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
    # Skip first-run onboarding/theme prompts
    cfg = acc_dir / ".claude.json"
    if not cfg.exists():
        cfg.write_text(
            json.dumps({"hasCompletedOnboarding": True, "theme": "dark"}, ensure_ascii=False),
            encoding="utf-8",
        )


def _sync_base_claude_assets_into_account(claude_home: Path, acc_dir: Path) -> None:
    """Mirror user's `CLAUDE.md` and `.claude/skills/*/SKILL.md` into `CLAUDE_CONFIG_DIR` (`acc_dir`)."""
    base_rules = claude_home / "CLAUDE.md"
    acc_rules = acc_dir / "CLAUDE.md"
    try:
        if base_rules.is_file():
            txt = base_rules.read_text(encoding="utf-8")
            if not acc_rules.is_file() or acc_rules.read_text(encoding="utf-8") != txt:
                acc_rules.write_text(txt, encoding="utf-8")
        elif acc_rules.is_file():
            acc_rules.unlink(missing_ok=True)
    except OSError:
        pass

    base_skills = claude_home / "skills"
    acc_skills = acc_dir / "skills"
    managed_skills = ("openrouter-bridge", "docker-sandbox", "host-services")
    for sk_name in managed_skills:
        src = base_skills / sk_name / "SKILL.md"
        dst = acc_skills / sk_name / "SKILL.md"
        try:
            if src.is_file():
                s_txt = src.read_text(encoding="utf-8")
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.is_file() or dst.read_text(encoding="utf-8") != s_txt:
                    dst.write_text(s_txt, encoding="utf-8")
            elif dst.is_file():
                dst.unlink(missing_ok=True)
                if dst.parent.is_dir() and not any(dst.parent.iterdir()):
                    dst.parent.rmdir()
        except OSError:
            pass


def materialize_account_claude_home(
    user_id: str,
    account: dict[str, Any],
    *,
    user_or_id: Any = None,
    proxy_url: Any = _UNSET_PROXY,
) -> tuple[Path, dict[str, str]]:
    """Write `.credentials.json` / settings into the isolated account dir and build the CLI env."""
    claude_home = ensure_user_claude_home(user_id)
    acc_dir = ensure_user_claude_account_dir(user_id, str(account["id"]))
    creds = parse_claude_credentials_payload(str(account.get("credentials_json") or ""))
    oauth_token = ""
    if creds["kind"] == "oauth":
        _atomic_write_secret(
            acc_dir / ".credentials.json",
            json.dumps({"claudeAiOauth": creds["oauth"]}, ensure_ascii=False),
        )
    elif creds["kind"] == "setup_token":
        oauth_token = creds["token"]
    _ensure_settings_files(acc_dir)
    _sync_base_claude_assets_into_account(claude_home, acc_dir)
    env = build_claude_env(
        user_or_id if user_or_id is not None else user_id,
        acc_dir,
        oauth_token=oauth_token,
        proxy_url=proxy_url,
    )
    return acc_dir, env


async def sync_refreshed_credentials_back(user_id: str, account_id: str, acc_dir: Path) -> bool:
    """Re-encrypt `.credentials.json` into SQLite if Claude Code refreshed the OAuth tokens."""
    cred_file = Path(acc_dir) / ".credentials.json"
    if not cred_file.is_file():
        return False
    try:
        disk = (await asyncio.to_thread(cred_file.read_text, "utf-8")).strip()
        disk_obj = json.loads(disk)
    except Exception:
        return False
    disk_oauth = disk_obj.get("claudeAiOauth") if isinstance(disk_obj, dict) else None
    if not isinstance(disk_oauth, dict) or not disk_oauth.get("accessToken"):
        return False
    acc = await database.get_user_claude_account(user_id, account_id, include_decrypted_credentials=True)
    if not acc:
        return False
    cur = parse_claude_credentials_payload(str(acc.get("credentials_json") or ""))
    old = cur.get("oauth") or {}
    if all(old.get(k) == disk_oauth.get(k) for k in ("accessToken", "refreshToken", "expiresAt")):
        return False
    return await database.sync_user_claude_credentials_json(
        user_id, account_id, json.dumps({"claudeAiOauth": disk_oauth}, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


async def _terminate_proc_tree(proc: Optional[asyncio.subprocess.Process]) -> None:
    if proc is None:
        return
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except Exception:
            pass
    if proc.returncode is None:
        pid = proc.pid
        if IS_WINDOWS and pid:
            try:
                kill_env = apply_isolated_home_to_env(os.environ.copy(), ensure_user_cli_home("_system"))
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=kill_env,
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
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            transport.close()
        except Exception:
            pass
        await asyncio.sleep(0)


async def _spawn(argv: list[str], env: dict[str, str], cwd: Optional[Path] = None) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=(os.name != "nt"),
        limit=8 * 1024 * 1024,
        **subprocess_hidden_kwargs(),
    )


async def _run_cli_capture(
    args: list[str],
    env: dict[str, str],
    *,
    timeout: float = 20.0,
    stdin_text: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> tuple[int, str, str]:
    bin_path = resolve_claude_bin()
    if not bin_path:
        return 127, "", "Claude Code CLI не установлен"
    proc = await _spawn(build_claude_exec_argv(bin_path, *args), env, cwd)
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate((stdin_text or "").encode("utf-8") if stdin_text is not None else b""),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await _terminate_proc_tree(proc)
        return 124, "", "timeout"
    return (
        int(proc.returncode or 0),
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )


async def claude_auth_status(env: dict[str, str]) -> dict[str, Any]:
    """`claude auth status --json` -> `{logged_in, email, plan_type, auth_method, account_uuid, org_id}`."""
    rc, out, err = await _run_cli_capture(["auth", "status", "--json"], env, timeout=20.0)
    data: dict[str, Any] = {}
    txt = strip_ansi(out).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    acct = data.get("account") if isinstance(data.get("account"), dict) else {}
    email = str(data.get("email") or data.get("emailAddress") or acct.get("email") or acct.get("email_address") or "")
    plan = str(
        data.get("subscriptionType") or data.get("subscription_type") or data.get("plan") or acct.get("subscriptionType") or ""
    ).strip().lower()
    logged = data.get("loggedIn")
    if logged is None:
        logged = data.get("logged_in")
    if logged is None:
        logged = bool(email) and rc == 0
    return {
        "ok": rc == 0,
        "logged_in": bool(logged),
        "email": email,
        "plan_type": plan,
        "auth_method": str(data.get("authMethod") or data.get("auth_method") or ""),
        "account_uuid": str(data.get("accountUuid") or data.get("account_uuid") or acct.get("uuid") or ""),
        "org_id": str(data.get("orgId") or data.get("org_id") or data.get("organizationUuid") or ""),
        "raw_error": strip_ansi(err).strip()[:400] if rc != 0 else "",
    }


# ---------------------------------------------------------------------------
# 5.4 Live quotas
# ---------------------------------------------------------------------------


def _pct(val: Any) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return 0.0
    if 0.0 < f <= 1.0 and not float(f).is_integer():
        f *= 100.0
    return max(0.0, min(100.0, f))


def _window(obj: Any, mins: int) -> Optional[dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    used = _pct(obj.get("utilization", obj.get("used_pct", 0)))
    return {
        "used_pct": round(used, 1),
        "remaining_pct": round(max(0.0, 100.0 - used), 1),
        "window_mins": mins,
        "reset_time": str(obj.get("resets_at") or obj.get("reset_time") or ""),
    }


def parse_claude_usage_payload(payload: Any, plan_type: str = "") -> dict[str, Any]:
    """Normalize `/api/oauth/usage` (`five_hour`, `seven_day`, ...) into Hub quota format."""
    if not isinstance(payload, dict):
        return {"ok": False, "note": "Пустой ответ телеметрии Claude", "updated_at": _now_iso()}
    p5 = _window(payload.get("five_hour"), 300) or {
        "used_pct": 0.0, "remaining_pct": 100.0, "window_mins": 300, "reset_time": "",
    }
    s7 = _window(payload.get("seven_day"), 10080) or {
        "used_pct": 0.0, "remaining_pct": 100.0, "window_mins": 10080, "reset_time": "",
    }
    out: dict[str, Any] = {
        "ok": True,
        "plan_type": (plan_type or "").strip().lower(),
        "primary_5h": p5,
        "secondary_7d": s7,
        "updated_at": _now_iso(),
    }
    for k in ("seven_day_sonnet", "seven_day_opus"):
        w = _window(payload.get(k), 10080)
        if w:
            out[k] = w
    return out


def _http_get_usage_sync(
    access_token: str,
    cli_version: str,
    proxy_url: Any = _UNSET_PROXY,
) -> tuple[int, Any]:
    req = urllib.request.Request(
        CLAUDE_USAGE_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": CLAUDE_OAUTH_BETA,
            "User-Agent": f"claude-code/{cli_version or '2.1.0'}",
            "Accept": "application/json",
        },
    )
    eff_proxy = (
        proxy_router.resolve_global_proxy_sync()
        if proxy_url is _UNSET_PROXY
        else proxy_url
    )
    opener = proxy_router.build_urllib_proxy_opener(eff_proxy)
    try:
        with opener.open(req, timeout=12.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return int(exc.code), body[:300]
    except Exception as exc:  # network / proxy / JSON
        return 0, str(exc)[:300]


def _invoke_http_get_usage_sync(
    access_token: str,
    cli_version: str,
    proxy_url: Any,
) -> tuple[int, Any]:
    try:
        return _http_get_usage_sync(access_token, cli_version, proxy_url)
    except TypeError:
        return _http_get_usage_sync(access_token, cli_version)


async def fetch_claude_oauth_quotas(
    access_token: str,
    plan_type: str = "",
    *,
    proxy_url: Any = _UNSET_PROXY,
) -> tuple[int, dict[str, Any]]:
    if not access_token:
        return 0, {"ok": False, "note": "Нет OAuth-токена", "updated_at": _now_iso()}
    eff_proxy = (
        await proxy_router.ensure_proxy_for_context("global")
        if proxy_url is _UNSET_PROXY
        else proxy_url
    )
    ver = str(get_claude_cli_info().get("version") or "")
    status, body = await asyncio.to_thread(_invoke_http_get_usage_sync, access_token, ver, eff_proxy)
    if status == 200 and isinstance(body, dict):
        return status, parse_claude_usage_payload(body, plan_type)
    return status, {"ok": False, "status": status, "note": str(body)[:200], "updated_at": _now_iso()}


SETUP_TOKEN_QUOTA_NOTE = "Токен активен (телеметрия % доступна при полном OAuth-входе)"


async def refresh_claude_account_quotas(user_id: str, account_id: str) -> dict[str, Any]:
    """Read live 5h/7d quotas for one account, auto-refreshing the OAuth token once on 401."""
    acc = await database.get_user_claude_account(user_id, account_id, include_decrypted_credentials=True)
    if not acc:
        raise ValueError("Аккаунт Claude не найден")
    try:
        acc_proxy_url = await proxy_router.ensure_proxy_for_context(
            "claude",
            user_id=user_id,
            account_id=account_id,
        )
    except proxy_router.ProxyRouteError as exc:
        err_route = str(exc)[:300]
        await database.update_claude_account_telemetry(user_id, account_id, last_error=err_route)
        return {"ok": False, "status": 0, "note": err_route, "updated_at": _now_iso()}

    creds = parse_claude_credentials_payload(str(acc.get("credentials_json") or ""))
    plan = str(acc.get("plan_type") or "")
    acc_dir, env = await asyncio.to_thread(
        materialize_account_claude_home,
        user_id,
        acc,
        proxy_url=acc_proxy_url,
    )

    if creds["kind"] == "setup_token":
        status, quotas = await fetch_claude_oauth_quotas(creds["token"], plan, proxy_url=acc_proxy_url)
        if not quotas.get("ok"):
            st = await claude_auth_status(env) if resolve_claude_bin() else {"logged_in": True}
            if st.get("logged_in"):
                quotas = {"ok": False, "note": SETUP_TOKEN_QUOTA_NOTE, "plan_type": plan, "updated_at": _now_iso()}
                await database.update_claude_account_telemetry(user_id, account_id, quotas=quotas, last_error="")
            else:
                await database.update_claude_account_telemetry(
                    user_id, account_id, last_error="Токен setup-token недействителен или отозван"
                )
            return quotas
        await database.update_claude_account_telemetry(user_id, account_id, quotas=quotas, last_error="")
        return quotas

    status, quotas = await fetch_claude_oauth_quotas(creds["token"], plan, proxy_url=acc_proxy_url)
    if status == 401 and creds["oauth"].get("refreshToken") and resolve_claude_bin():
        await claude_auth_status(env)  # lets the CLI refresh the token on disk
        if await sync_refreshed_credentials_back(user_id, account_id, acc_dir):
            acc2 = await database.get_user_claude_account(user_id, account_id, include_decrypted_credentials=True)
            creds = parse_claude_credentials_payload(str((acc2 or {}).get("credentials_json") or ""))
            status, quotas = await fetch_claude_oauth_quotas(creds["token"], plan, proxy_url=acc_proxy_url)
    else:
        await sync_refreshed_credentials_back(user_id, account_id, acc_dir)

    if quotas.get("ok"):
        _CLAUDE_BACKOFF_UNTIL_MONO.pop(f"{user_id.strip()}:{account_id.strip()}", None)
        await database.update_claude_account_telemetry(
            user_id, account_id, quotas=quotas, plan_type=plan or None, last_error=""
        )
    else:
        if status in (0, 429):
            _CLAUDE_BACKOFF_UNTIL_MONO[f"{user_id.strip()}:{account_id.strip()}"] = (
                time.monotonic() + CLAUDE_RATE_LIMIT_BACKOFF_SEC
            )
        err = "Сессия Claude истекла — войдите заново" if status == 401 else f"Не удалось получить квоты Claude ({status})"
        await database.update_claude_account_telemetry(user_id, account_id, last_error=err)
    return quotas


def _parse_claude_iso_utc(raw_ts: Any) -> Optional[datetime]:
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


def _clamp_exhausted_claude_quotas(
    existing_quotas: Optional[dict[str, Any]],
    err_text: str = "",
) -> dict[str, Any]:
    """Clamp `secondary_7d` (weekly) or `primary_5h` (5-hour) window to `0%` remaining on limit error."""
    out: dict[str, Any] = dict(existing_quotas) if isinstance(existing_quotas, dict) else {}
    low = (err_text or "").lower()
    is_weekly = any(
        kw in low
        for kw in ("weekly", "7-day", "7 day", "seven_day", "7d", "secondary")
    )
    if is_weekly:
        s7 = dict(out.get("secondary_7d") or {}) if isinstance(out.get("secondary_7d"), dict) else {}
        s7["used_pct"] = 100.0
        s7["remaining_pct"] = 0.0
        s7["window_mins"] = int(s7.get("window_mins") or 10080)
        s7.setdefault("reset_time", "")
        out["secondary_7d"] = s7
        if not isinstance(out.get("primary_5h"), dict):
            out["primary_5h"] = {
                "used_pct": 0.0,
                "remaining_pct": 100.0,
                "window_mins": 300,
                "reset_time": "",
            }
    else:
        p5 = dict(out.get("primary_5h") or {}) if isinstance(out.get("primary_5h"), dict) else {}
        p5["used_pct"] = 100.0
        p5["remaining_pct"] = 0.0
        p5["window_mins"] = int(p5.get("window_mins") or 300)
        p5.setdefault("reset_time", "")
        out["primary_5h"] = p5

    out.pop("note", None)
    out["ok"] = True
    out["updated_at"] = _now_iso()
    return out


CLAUDE_DEFAULT_STALE_SEC = 300.0
CLAUDE_MIN_REFRESH_INTERVAL_SEC = 90.0
CLAUDE_FORCE_MIN_INTERVAL_SEC = 20.0
CLAUDE_RATE_LIMIT_BACKOFF_SEC = 300.0

_CLAUDE_REFRESH_IN_FLIGHT: set[str] = set()
_CLAUDE_LAST_PROBE_MONO: dict[str, float] = {}
_CLAUDE_BACKOFF_UNTIL_MONO: dict[str, float] = {}
_CLAUDE_LAST_ACTIVE_ACC: dict[str, str] = {}
_CLAUDE_BG_TASKS: set[asyncio.Task[Any]] = set()


def is_claude_quota_cache_stale(
    account: Optional[dict[str, Any]],
    max_age_sec: float = CLAUDE_DEFAULT_STALE_SEC,
) -> bool:
    """Return True if Claude account telemetry cache is missing, older than `max_age_sec`, or past `reset_time + 10s`."""
    if not isinstance(account, dict) or not account:
        return True
    cached_dt = _parse_claude_iso_utc(account.get("cached_at"))
    if cached_dt is None:
        return True
    now_utc = datetime.now(timezone.utc)
    if (now_utc - cached_dt).total_seconds() >= max(0.0, float(max_age_sec)):
        return True

    quotas = account.get("quotas")
    if isinstance(quotas, dict):
        for win_key in ("primary_5h", "secondary_7d", "seven_day_sonnet", "seven_day_opus"):
            win = quotas.get(win_key)
            if not isinstance(win, dict):
                continue
            reset_dt = _parse_claude_iso_utc(win.get("reset_time"))
            if (
                reset_dt is not None
                and now_utc >= (reset_dt + timedelta(seconds=10))
                and cached_dt < reset_dt
            ):
                return True
    return False


def schedule_claude_live_refresh(
    user_id: str,
    *,
    force: bool = False,
    max_age_sec: float = CLAUDE_DEFAULT_STALE_SEC,
    min_interval_sec: float = CLAUDE_MIN_REFRESH_INTERVAL_SEC,
    on_updated: Optional[Callable[[str], Awaitable[Any]]] = None,
    account_id: Optional[str] = None,
) -> bool:
    """Non-blocking background Claude quota refresh with in-flight lock, cooldown, and 300s backoff on 429/timeout."""
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return False

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    now_mono = time.monotonic()
    effective_min_interval = (
        CLAUDE_FORCE_MIN_INTERVAL_SEC
        if force
        else max(0.0, float(min_interval_sec))
    )
    prefix = f"{clean_uid}:"
    target_acc_id = (account_id or "").strip() or _CLAUDE_LAST_ACTIVE_ACC.get(clean_uid, "")

    if target_acc_id:
        known_key = f"{clean_uid}:{target_acc_id}"
        if known_key in _CLAUDE_REFRESH_IN_FLIGHT or f"{clean_uid}:*" in _CLAUDE_REFRESH_IN_FLIGHT:
            return False
        if not force and now_mono < _CLAUDE_BACKOFF_UNTIL_MONO.get(known_key, 0.0):
            return False
        last_mono = _CLAUDE_LAST_PROBE_MONO.get(known_key)
        if last_mono is not None and (now_mono - last_mono) < effective_min_interval:
            return False
    else:
        if any(k.startswith(prefix) for k in _CLAUDE_REFRESH_IN_FLIGHT):
            return False
        matching_backoffs = [v for k, v in _CLAUDE_BACKOFF_UNTIL_MONO.items() if k.startswith(prefix)]
        if not force and matching_backoffs and now_mono < max(matching_backoffs):
            return False
        matching_last = [v for k, v in _CLAUDE_LAST_PROBE_MONO.items() if k.startswith(prefix)]
        if matching_last and (now_mono - max(matching_last)) < effective_min_interval:
            return False

    pending_key = f"{clean_uid}:{target_acc_id}" if target_acc_id else f"{clean_uid}:*"
    _CLAUDE_REFRESH_IN_FLIGHT.add(pending_key)

    async def _runner() -> None:
        resolved_key = pending_key
        try:
            chosen: Optional[dict[str, Any]] = None
            if account_id:
                chosen = await database.get_user_claude_account(clean_uid, account_id.strip())
            if chosen is None:
                chosen = await database.get_active_claude_account(clean_uid, require_enabled=True)
            if chosen is None:
                return

            acc_id = str(chosen["id"])
            _CLAUDE_LAST_ACTIVE_ACC[clean_uid] = acc_id
            resolved_key = f"{clean_uid}:{acc_id}"
            if resolved_key != pending_key:
                if resolved_key in _CLAUDE_REFRESH_IN_FLIGHT:
                    return
                _CLAUDE_REFRESH_IN_FLIGHT.add(resolved_key)

            cur_mono = time.monotonic()
            if not force and cur_mono < _CLAUDE_BACKOFF_UNTIL_MONO.get(resolved_key, 0.0):
                return
            prev_mono = _CLAUDE_LAST_PROBE_MONO.get(resolved_key)
            if prev_mono is not None and (cur_mono - prev_mono) < effective_min_interval:
                return
            if not force and not is_claude_quota_cache_stale(chosen, max_age_sec=max_age_sec):
                return

            _CLAUDE_LAST_PROBE_MONO[resolved_key] = cur_mono
            quotas_res = await refresh_claude_account_quotas(clean_uid, acc_id)
            if isinstance(quotas_res, dict) and quotas_res.get("status") in (0, 429):
                _CLAUDE_BACKOFF_UNTIL_MONO[resolved_key] = (
                    time.monotonic() + CLAUDE_RATE_LIMIT_BACKOFF_SEC
                )

            if on_updated is not None:
                await on_updated(clean_uid)
        except Exception:
            if resolved_key and not resolved_key.endswith(":*"):
                _CLAUDE_BACKOFF_UNTIL_MONO[resolved_key] = (
                    time.monotonic() + CLAUDE_RATE_LIMIT_BACKOFF_SEC
                )
        finally:
            _CLAUDE_REFRESH_IN_FLIGHT.discard(pending_key)
            _CLAUDE_REFRESH_IN_FLIGHT.discard(resolved_key)

    task = loop.create_task(_runner())
    _CLAUDE_BG_TASKS.add(task)
    task.add_done_callback(_CLAUDE_BG_TASKS.discard)
    return True


async def refresh_active_claude_quotas(user_id: str) -> Optional[dict[str, Any]]:
    acc = await database.get_active_claude_account(user_id)
    if not acc:
        return None
    try:
        return await refresh_claude_account_quotas(user_id, str(acc["id"]))
    except Exception:
        return None


async def refresh_all_user_claude_quotas(user_id: str) -> list[dict[str, Any]]:
    """Refresh live 5h/7d quotas across all enabled personal Claude accounts of `user_id`."""
    accounts = await database.list_user_claude_accounts(user_id)
    enabled = [a for a in accounts if a.get("is_enabled")]
    if not enabled:
        return []
    sem = asyncio.Semaphore(3)

    async def _one(aid: str) -> Optional[dict[str, Any]]:
        async with sem:
            try:
                return await refresh_claude_account_quotas(user_id, aid)
            except Exception:
                return None

    res = await asyncio.gather(*(_one(str(a["id"])) for a in enabled), return_exceptions=True)
    return [r for r in res if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# 5.3 Install + auth flows
# ---------------------------------------------------------------------------


async def install_or_update_claude_cli() -> dict[str, Any]:
    """`npm install --prefix data/tools/claude @anthropic-ai/claude-code@latest` (admin only — checked in route)."""
    async with _INSTALL_LOCK:
        npm = _find_npm_bin()
        if not npm:
            return {"ok": False, "error": "npm не найден на сервере (установите Node.js LTS)"}
        try:
            proxy = await proxy_router.ensure_proxy_for_context("global")
        except proxy_router.ProxyRouteError as exc:
            return {"ok": False, "error": str(exc)}
        CLAUDE_TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        env = apply_isolated_home_to_env(os.environ.copy(), ensure_user_cli_home("_system"))
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["ORT_DISABLE_ALL_GPU"] = "1"
        env["npm_config_update_notifier"] = "false"
        env["BROWSER"] = "echo"
        proxy_router.apply_proxy_to_subprocess_env(env, proxy, include_npm=True)
        args = ["install", "--prefix", str(CLAUDE_TOOLS_DIR), "@anthropic-ai/claude-code@latest", "--no-audit", "--no-fund"]
        argv = ["cmd.exe", "/d", "/s", "/c", npm, *args] if IS_WINDOWS and npm.lower().endswith(".cmd") else [npm, *args]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **subprocess_hidden_kwargs(),
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=600.0)
        except asyncio.TimeoutError:
            await _terminate_proc_tree(proc)
            return {"ok": False, "error": "Таймаут установки Claude Code CLI"}
        info = get_claude_cli_info(force_refresh=True)
        ok = proc.returncode == 0 and bool(info.get("installed"))
        return {
            "ok": ok,
            "cli_info": info,
            "log": strip_ansi((out_b or b"").decode("utf-8", "replace") + (err_b or b"").decode("utf-8", "replace"))[-4000:],
            **({} if ok else {"error": "npm install завершился с ошибкой"}),
        }


@dataclass
class ClaudeOAuthFlow:
    flow_id: str
    user_id: str
    label: str
    pending_dir: Path
    env: dict[str, str]
    proxy_mode: str = "global"
    proxy_spec: str = ""
    proxy_label: str = ""
    pending_owner: str = ""
    proxy_url: Optional[str] = None
    created_ts: float = field(default_factory=time.monotonic)
    status: str = "waiting_url"
    verification_url: str = ""
    error: str = ""
    output: str = ""
    account: Optional[dict[str, Any]] = None
    proc: Optional[asyncio.subprocess.Process] = None
    reader_task: Optional[asyncio.Task] = None
    expiry_task: Optional[asyncio.Task] = None
    url_event: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "status": self.status,
            "verification_url": self.verification_url,
            "label": self.label,
            "error": self.error,
            "account": self.account,
            "proxy_mode": self.proxy_mode,
            "proxy_label": self.proxy_label,
            "expires_in": max(0, int(FLOW_TTL_SEC - (time.monotonic() - self.created_ts))),
        }


_OAUTH_FLOWS: dict[str, ClaudeOAuthFlow] = {}
_CLAUDE_OAUTH_EXPIRY_TASKS: set[asyncio.Task] = set()
FLOW_TTL_SEC = 600.0


async def _resolve_claude_user_is_admin(user_id: str, explicit: Optional[bool] = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    if (user_id or "").strip() == "admin":
        return True
    try:
        u = await database.get_user_by_id(user_id)
        return bool(u and str(u.get("role") or "").strip().lower() == "admin")
    except Exception:
        return False


def _prepare_pending_claude_proxy(
    proxy_mode: Optional[str],
    proxy_spec: Optional[str],
    *,
    is_admin: bool,
) -> tuple[Optional[str], str, str]:
    clean_spec = str(proxy_spec or "").strip() if proxy_spec is not None else ""
    if proxy_mode is None and not clean_spec:
        return None, "", ""
    eff_mode = (
        "custom"
        if str(proxy_mode or ("custom" if clean_spec else "global")).strip().lower() == "custom"
        else "global"
    )
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


def extract_oauth_url(text: str) -> str:
    clean = strip_ansi(text or "")
    # Re-join URLs wrapped across terminal lines
    joined = re.sub(r"(https://\S+)\r?\n(\S+)", lambda m: m.group(1) + m.group(2) if "=" in m.group(2) or "&" in m.group(2) else m.group(0), clean)
    for cand in _OAUTH_URL_RE.findall(joined):
        if "oauth" in cand.lower() or "authorize" in cand.lower():
            return cand.rstrip(".,;")
    found = _OAUTH_URL_RE.findall(joined)
    return found[0].rstrip(".,;") if found else ""


def _cleanup_pending_dir(p: Path) -> None:
    try:
        rp = Path(p).resolve()
        rp.relative_to(config.CLI_HOMES_DIR.resolve())
        if rp.name.startswith("_pending_") and rp.parent.name == "accounts" and rp.exists():
            shutil.rmtree(rp, ignore_errors=True)
    except Exception:
        pass


async def _expire_flows() -> None:
    now = time.monotonic()
    for fid, fl in list(_OAUTH_FLOWS.items()):
        if now - fl.created_ts > FLOW_TTL_SEC and fl.status not in ("completed",):
            await _abort_flow(fl, "Время ожидания входа истекло")
            fl.status = "expired"
            _OAUTH_FLOWS.pop(fid, None)


async def _abort_flow(fl: ClaudeOAuthFlow, err: str = "") -> None:
    exp_t, fl.expiry_task = fl.expiry_task, None
    if exp_t is not None and not exp_t.done() and exp_t is not asyncio.current_task():
        exp_t.cancel()
    if fl.reader_task is not None and not fl.reader_task.done():
        fl.reader_task.cancel()
    await _terminate_proc_tree(fl.proc)
    if fl.pending_owner:
        await asyncio.to_thread(proxy_router.release_owner, fl.pending_owner)
    await asyncio.to_thread(_cleanup_pending_dir, fl.pending_dir)
    if err and fl.status != "completed":
        fl.status = "error"
        fl.error = err


async def _auto_expire_claude_oauth_flow(flow_id: str) -> None:
    try:
        await asyncio.sleep(max(0.0, float(FLOW_TTL_SEC)))
    except asyncio.CancelledError:
        return
    fl = _OAUTH_FLOWS.get(flow_id)
    if not fl or fl.status in ("completed", "cancelled"):
        return
    await _abort_flow(fl, "Время ожидания входа истекло")
    fl.status = "expired"
    _OAUTH_FLOWS.pop(flow_id, None)


async def start_claude_oauth_flow(
    user_id: str,
    label: str = "",
    *,
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    is_admin: Optional[bool] = None,
) -> dict[str, Any]:
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("Не указан пользователь")
    eff_is_admin = await _resolve_claude_user_is_admin(uid, is_admin)
    p_mode_opt, clean_p_spec, p_safe_label = await asyncio.to_thread(
        _prepare_pending_claude_proxy,
        proxy_mode if proxy_mode is not None else "global",
        proxy_spec,
        is_admin=eff_is_admin,
    )
    eff_p_mode = p_mode_opt or "global"

    if not resolve_claude_bin():
        raise RuntimeError("Claude Code CLI не установлен на сервере. Попросите администратора установить его.")
    await _expire_flows()
    for fid, fl in list(_OAUTH_FLOWS.items()):
        if fl.user_id == uid and fl.status in ("waiting_url", "waiting_code", "verifying"):
            await _abort_flow(fl)
            _OAUTH_FLOWS.pop(fid, None)

    flow_id = secrets.token_hex(8)
    pending_owner = f"pending:cld:{flow_id}"
    flow_proxy_url = await proxy_router.ensure_proxy_for_context(
        "claude",
        user_id=uid,
        pending_owner=pending_owner,
        pending_mode=eff_p_mode,
        pending_spec=clean_p_spec,
        is_admin=eff_is_admin,
    )

    pending = ensure_user_claude_account_dir(uid, f"_pending_{flow_id}")
    await asyncio.to_thread(_ensure_settings_files, pending)
    env = build_claude_env(uid, pending, proxy_url=flow_proxy_url)
    fl = ClaudeOAuthFlow(
        flow_id=flow_id,
        user_id=uid,
        label=(label or "").strip()[:80],
        pending_dir=pending,
        env=env,
        proxy_mode=eff_p_mode,
        proxy_spec=clean_p_spec,
        proxy_label=p_safe_label,
        pending_owner=pending_owner,
        proxy_url=flow_proxy_url,
    )
    bin_path = resolve_claude_bin()
    try:
        fl.proc = await _spawn(build_claude_exec_argv(bin_path, "auth", "login"), env, pending)
    except Exception:
        await asyncio.to_thread(proxy_router.release_owner, pending_owner)
        await asyncio.to_thread(_cleanup_pending_dir, pending)
        raise

    async def _reader(stream: Optional[asyncio.StreamReader]) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            fl.output = (fl.output + chunk.decode("utf-8", errors="replace"))[-20000:]
            if not fl.verification_url:
                url = extract_oauth_url(fl.output)
                if url:
                    fl.verification_url = url
                    if fl.status == "waiting_url":
                        fl.status = "waiting_code"
                    fl.url_event.set()

    async def _read_both() -> None:
        await asyncio.gather(_reader(fl.proc.stdout), _reader(fl.proc.stderr), return_exceptions=True)
        fl.url_event.set()

    fl.reader_task = asyncio.create_task(_read_both())
    _OAUTH_FLOWS[flow_id] = fl
    exp_task = asyncio.create_task(_auto_expire_claude_oauth_flow(flow_id))
    _CLAUDE_OAUTH_EXPIRY_TASKS.add(exp_task)
    exp_task.add_done_callback(_CLAUDE_OAUTH_EXPIRY_TASKS.discard)
    fl.expiry_task = exp_task
    try:
        await asyncio.wait_for(fl.url_event.wait(), timeout=25.0)
    except asyncio.TimeoutError:
        pass
    if not fl.verification_url:
        tail = strip_ansi(fl.output).strip()[-300:]
        await _abort_flow(fl, "Claude Code CLI не выдал ссылку авторизации" + (f": {tail}" if tail else ""))
        _OAUTH_FLOWS.pop(flow_id, None)
        raise RuntimeError(fl.error)
    return fl.public()


def get_claude_oauth_flow(user_id: str, flow_id: str) -> Optional[dict[str, Any]]:
    fl = _OAUTH_FLOWS.get(str(flow_id or ""))
    if not fl or fl.user_id != str(user_id or ""):
        return None
    if (time.monotonic() - fl.created_ts) > FLOW_TTL_SEC and fl.status not in ("completed", "cancelled"):
        fl.status = "expired"
        fl.error = fl.error or "Время ожидания входа истекло"
        try:
            loop = asyncio.get_running_loop()
            t = loop.create_task(_expire_flows())
            _CLAUDE_OAUTH_EXPIRY_TASKS.add(t)
            t.add_done_callback(_CLAUDE_OAUTH_EXPIRY_TASKS.discard)
        except RuntimeError:
            pass
        pub = fl.public()
        pub["expires_in"] = 0
        return pub
    return fl.public()


async def cancel_claude_oauth_flow(user_id: str, flow_id: str) -> bool:
    fl = _OAUTH_FLOWS.get(str(flow_id or ""))
    if not fl or fl.user_id != str(user_id or ""):
        return False
    await _abort_flow(fl)
    fl.status = "cancelled"
    _OAUTH_FLOWS.pop(fl.flow_id, None)
    return True


def _read_pending_credentials(pending: Path) -> str:
    f = pending / ".credentials.json"
    if f.is_file():
        try:
            txt = f.read_text(encoding="utf-8").strip()
            if txt and parse_claude_credentials_payload(txt)["kind"] == "oauth":
                return txt
        except OSError:
            return ""
    return ""


async def _finalize_account(
    uid: str,
    *,
    raw_credentials: str,
    label: str,
    auth_method: str,
    status: dict[str, Any],
    source_dir: Optional[Path] = None,
    proxy_url: Any = _UNSET_PROXY,
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    proxy_label: Optional[str] = None,
) -> dict[str, Any]:
    creds = parse_claude_credentials_payload(raw_credentials)
    plan = str(status.get("plan_type") or creds["oauth"].get("subscriptionType") or "").lower()
    quotas: Optional[dict[str, Any]] = None
    if creds["token"]:
        _st, q = await fetch_claude_oauth_quotas(creds["token"], plan, proxy_url=proxy_url)
        if q.get("ok"):
            quotas = q
        elif auth_method == "setup_token":
            quotas = {"ok": False, "note": SETUP_TOKEN_QUOTA_NOTE, "plan_type": plan, "updated_at": _now_iso()}
    stored = raw_credentials if creds["kind"] == "oauth" else json.dumps({"oauthToken": creds["token"]})
    acc = await database.upsert_user_claude_account(
        uid,
        raw_credentials_json=stored,
        claude_account_id=str(status.get("account_uuid") or ""),
        email=str(status.get("email") or ""),
        label=label,
        plan_type=plan,
        auth_method=auth_method,
        quotas=quotas,
        models=[dict(m) for m in DEFAULT_CLAUDE_CODE_MODELS],
        make_active=True,
        proxy_mode=proxy_mode,
        proxy_spec=proxy_spec,
        proxy_label=proxy_label,
    )
    await proxy_router.reload_routes_from_db()
    if source_dir is not None:

        def _move() -> None:
            dest = ensure_user_claude_account_dir(uid, acc["id"])
            for child in source_dir.iterdir():
                target = dest / child.name
                try:
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink()
                    shutil.move(str(child), str(target))
                except Exception:
                    pass
            _cleanup_pending_dir(source_dir)

        await asyncio.to_thread(_move)
    return acc


async def submit_claude_oauth_code(user_id: str, flow_id: str, code: str) -> dict[str, Any]:
    fl = _OAUTH_FLOWS.get(str(flow_id or ""))
    if not fl or fl.user_id != str(user_id or ""):
        raise ValueError("Сессия входа не найдена или истекла")
    clean_code = re.sub(r"\s+", "", str(code or ""))
    if not clean_code:
        raise ValueError("Введите код подтверждения с экрана браузера")
    if fl.proc is None or fl.proc.stdin is None:
        raise RuntimeError("Процесс авторизации уже завершён")
    fl.status = "verifying"
    try:
        fl.proc.stdin.write((clean_code + "\n").encode("utf-8"))
        await fl.proc.stdin.drain()
    except Exception as exc:
        fl.status = "error"
        fl.error = f"Не удалось передать код: {exc}"
        raise RuntimeError(fl.error)

    raw = ""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        raw = await asyncio.to_thread(_read_pending_credentials, fl.pending_dir)
        if raw or (fl.proc.returncode is not None):
            break
        await asyncio.sleep(0.4)
    if not raw:
        raw = await asyncio.to_thread(_read_pending_credentials, fl.pending_dir)
    if not raw:
        tail = strip_ansi(fl.output).strip()[-300:]
        await _abort_flow(fl, "Код не принят Claude" + (f": {tail}" if tail else ""))
        _OAUTH_FLOWS.pop(fl.flow_id, None)
        raise RuntimeError(fl.error)

    await _terminate_proc_tree(fl.proc)
    try:
        status = await claude_auth_status(fl.env)
        acc = await _finalize_account(
            fl.user_id,
            raw_credentials=raw,
            label=fl.label,
            auth_method="oauth",
            status=status,
            source_dir=fl.pending_dir,
            proxy_url=fl.proxy_url,
            proxy_mode=fl.proxy_mode,
            proxy_spec=fl.proxy_spec if fl.proxy_mode == "custom" else None,
            proxy_label=fl.proxy_label,
        )
        fl.status = "completed"
        fl.account = acc
        if fl.expiry_task is not None and not fl.expiry_task.done():
            fl.expiry_task.cancel()
        fl.expiry_task = None
        _OAUTH_FLOWS.pop(fl.flow_id, None)
        return fl.public()
    finally:
        if fl.pending_owner:
            await asyncio.to_thread(proxy_router.release_owner, fl.pending_owner)


async def import_claude_credentials(
    user_id: str,
    raw_payload: str,
    label: str = "",
    *,
    proxy_mode: Optional[str] = None,
    proxy_spec: Optional[str] = None,
    is_admin: Optional[bool] = None,
) -> dict[str, Any]:
    """Import `sk-ant-oat...` (setup-token) or `.credentials.json` contents."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("Не указан пользователь")
    payload = (raw_payload or "").strip()
    creds = parse_claude_credentials_payload(payload)
    if creds["kind"] == "":
        raise ValueError(
            "Не распознан формат: вставьте токен `sk-ant-oat...` из `claude setup-token` "
            "или содержимое `.credentials.json` (с полем accessToken)"
        )
    eff_is_admin = await _resolve_claude_user_is_admin(uid, is_admin)
    p_mode_opt, clean_p_spec, p_safe_label = await asyncio.to_thread(
        _prepare_pending_claude_proxy,
        proxy_mode,
        proxy_spec,
        is_admin=eff_is_admin,
    )

    method = "setup_token" if creds["kind"] == "setup_token" else "credentials_json"
    imp_hex = secrets.token_hex(4)
    tmp_dir = ensure_user_claude_account_dir(uid, f"_pending_imp_{imp_hex}")
    imp_owner = f"pending:cld_imp:{imp_hex}"
    try:
        imp_proxy_url = await proxy_router.ensure_proxy_for_context(
            "claude",
            user_id=uid,
            pending_owner=imp_owner,
            pending_mode=(p_mode_opt or "global"),
            pending_spec=clean_p_spec,
            is_admin=eff_is_admin,
        )
        if creds["kind"] == "oauth":
            raw_store = json.dumps({"claudeAiOauth": creds["oauth"]}, ensure_ascii=False)
            await asyncio.to_thread(_atomic_write_secret, tmp_dir / ".credentials.json", raw_store)
            env = build_claude_env(uid, tmp_dir, proxy_url=imp_proxy_url)
        else:
            raw_store = creds["token"]
            env = build_claude_env(uid, tmp_dir, oauth_token=creds["token"], proxy_url=imp_proxy_url)
        await asyncio.to_thread(_ensure_settings_files, tmp_dir)
        status: dict[str, Any] = {"logged_in": True}
        if resolve_claude_bin():
            status = await claude_auth_status(env)
            if not status.get("logged_in") and creds["kind"] == "setup_token" and status.get("raw_error"):
                raise ValueError(f"Токен не принят Claude Code: {status['raw_error'][:200]}")
            if creds["kind"] == "oauth":
                refreshed = await asyncio.to_thread(_read_pending_credentials, tmp_dir)
                if refreshed:
                    raw_store = refreshed
        return await _finalize_account(
            uid,
            raw_credentials=raw_store,
            label=label,
            auth_method=method,
            status=status,
            source_dir=tmp_dir,
            proxy_url=imp_proxy_url,
            proxy_mode=p_mode_opt,
            proxy_spec=clean_p_spec if p_mode_opt == "custom" else None,
            proxy_label=p_safe_label if p_mode_opt == "custom" else None,
        )
    finally:
        await asyncio.to_thread(proxy_router.release_owner, imp_owner)
        await asyncio.to_thread(_cleanup_pending_dir, tmp_dir)


# ---------------------------------------------------------------------------
# 5.5 Security matrix + MCP config
# ---------------------------------------------------------------------------

READ_ONLY_DISALLOWED = ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"]


def resolve_claude_security_matrix(
    *,
    kind: str,
    access_mode: str,
    is_admin: bool,
    force_no_host_shell: Optional[bool] = None,
    allow_hub_openrouter: bool = False,
    allow_hub_sandbox: bool = False,
    allow_hub_host: bool = True,
    sandbox_network: str = "internet",
) -> dict[str, Any]:
    no_shell = (not is_admin) if force_no_host_shell is None else bool(force_no_host_shell)
    allowed: list[str] = []
    eff_sandbox = False
    if kind != "project" or access_mode == "read_only":
        perm, disallowed, tier = "plan", list(READ_ONLY_DISALLOWED), "read_only"
    elif access_mode == "write_files":
        perm, disallowed, tier = "acceptEdits", ["Bash"], "write_files"
    elif access_mode == "full_access" and no_shell:
        perm, disallowed = "acceptEdits", ["Bash"]
        eff_sandbox = bool(allow_hub_sandbox)
        tier = "docker" if eff_sandbox else "write_files"
    elif access_mode == "full_access":
        perm, disallowed, tier = "bypassPermissions", [], "host_full"
    else:
        perm, disallowed, tier = "plan", list(READ_ONLY_DISALLOWED), "read_only"
    eff_host_mcp = bool(allow_hub_host and tier == "host_full")
    if not eff_host_mcp:
        disallowed = [*disallowed, "mcp__hub_host__*"]
    if perm == "plan":
        allowed.extend(["Read", "Glob", "Grep", "LS"])
    elif perm == "acceptEdits":
        allowed.extend(["Read", "Glob", "Grep", "LS", "Write", "Edit", "MultiEdit"])
    net_ok = str(sandbox_network or "internet").strip().lower() != "none"
    if net_ok and perm in ("plan", "acceptEdits"):
        allowed.extend(["WebSearch", "WebFetch"])
    if allow_hub_openrouter:
        allowed.append("mcp__hub_openrouter__*")
    if eff_sandbox:
        allowed.append("mcp__hub_sandbox__*")
    return {
        "permission_mode": perm,
        "disallowed_tools": disallowed,
        "allowed_tools": allowed,
        "allow_hub_openrouter": bool(allow_hub_openrouter),
        "allow_hub_sandbox": eff_sandbox,
        "allow_hub_host": eff_host_mcp,
        "tier": tier,
    }


def write_claude_mcp_config(
    acc_dir: Path,
    *,
    openrouter_url: str = "",
    sandbox_url: str = "",
    host_url: str = "",
) -> Path:
    servers: dict[str, Any] = {}
    if openrouter_url:
        servers["hub_openrouter"] = {"type": "http", "url": openrouter_url}
    if sandbox_url:
        servers["hub_sandbox"] = {"type": "http", "url": sandbox_url}
    if host_url:
        servers["hub_host"] = {"type": "http", "url": host_url}
    path = Path(acc_dir) / "mcp_hub_runtime.json"
    _atomic_write_secret(path, json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2))
    return path


HOST_SERVICES_PROMPT_NOTE = (
    "Долгоживущие фоновые сервисы Windows-хоста (Telegram/Discord-боты, `uvicorn`/FastAPI, Flask, Vite, `http.server`, воркеры) "
    "НЕ запускай напрямую блокирующим `Bash` — управляй ими через MCP `hub_host` (`mcp__hub_host__host_service_ctl` "
    "с `action='start'|'restart'|'status'|'logs'|'stop'`)."
)


SANDBOX_PROMPT_NOTE = (
    "Хостовый Bash заблокирован политикой безопасности. Чтение, поиск и редактирование файлов в папке проекта "
    "открыты напрямую (`Read`, `Edit`, `Write`, `Glob`, `Grep`), а все команды сборки, тестов, git, npm, python "
    "и фоновых серверов выполняй строго через MCP `hub_sandbox` (`mcp__hub_sandbox__sandbox_exec`, "
    "`mcp__hub_sandbox__sandbox_service_ctl`, `mcp__hub_sandbox__sandbox_reset`; проект смонтирован в `/workspace`)."
)


def sanitize_claude_cmd_shim_text(text: str) -> str:
    """Sanitize a string argument for `cmd.exe /d /s /c <.cmd>` so newlines and metacharacters never break argv."""
    s = re.sub(r"(?:\r\n|\r|\n)+", " | ", str(text or ""))
    s = s.replace("\x00", "")
    # Replace double quotes so list2cmdline wraps the entire value in one unbroken "..."
    # without `\"` sequences that cmd.exe treats as closing quotes.
    s = s.replace('"', "'")
    # Neutralize %VAR% expansion inside cmd.exe /c
    s = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*|[0-9*])%?", r"pct_\1", s)
    s = s.strip()
    if s and any(ch in s for ch in "&|<>^()!;%") and " " not in s:
        s = s + " "
    return s


def build_claude_turn_args(
    *,
    model_slug: str,
    effort: str,
    sec: dict[str, Any],
    mcp_config_path: Path,
    resume_session_id: str = "",
    system_prompt: str = "",
    add_dirs: Optional[list[Path]] = None,
    bin_path: str = "",
) -> list[str]:
    is_win_cmd = bool(IS_WINDOWS and str(bin_path or "").lower().endswith((".cmd", ".bat")))
    args = [
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model", model_slug,
    ]
    if effort:
        args += ["--effort", effort]
    # Security & MCP isolation flags ALWAYS come first before any user/project text!
    args += ["--permission-mode", str(sec["permission_mode"])]
    args += ["--strict-mcp-config", "--mcp-config", str(mcp_config_path)]
    if sec.get("disallowed_tools"):
        args += ["--disallowedTools", ",".join(sec["disallowed_tools"])]
    if sec.get("allowed_tools"):
        args += ["--allowedTools", ",".join(sec["allowed_tools"])]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    for extra_d in add_dirs or []:
        if extra_d is not None:
            args += ["--add-dir", str(extra_d)]
    eff_sys_prompt = sanitize_claude_cmd_shim_text(system_prompt) if (is_win_cmd and system_prompt) else system_prompt
    if eff_sys_prompt:
        args += ["--append-system-prompt", eff_sys_prompt]
    if is_win_cmd:
        args = [re.sub(r"(?:\r\n|\r|\n)+", " ", str(a)) for a in args]
    return args


def prepare_claude_turn_argv_and_stdin(
    bin_path: str,
    *,
    prompt_text: str,
    model_slug: str,
    effort: str,
    sec: dict[str, Any],
    mcp_config_path: Path,
    resume_session_id: str = "",
    system_prompt: str = "",
    add_dirs: Optional[list[Path]] = None,
    max_cmd_chars: int = 7200,
) -> tuple[list[str], str]:
    """Build safe Claude CLI argv and stdin prompt, spilling excess `system_prompt` into stdin on Windows `.cmd` shims."""
    args = build_claude_turn_args(
        model_slug=model_slug,
        effort=effort,
        sec=sec,
        mcp_config_path=mcp_config_path,
        resume_session_id=resume_session_id,
        system_prompt=system_prompt,
        add_dirs=add_dirs,
        bin_path=bin_path,
    )
    argv = build_claude_exec_argv(bin_path, *args)
    is_win_cmd = bool(IS_WINDOWS and str(bin_path or "").lower().endswith((".cmd", ".bat")))
    if not is_win_cmd or not system_prompt or len(subprocess.list2cmdline(argv)) <= max_cmd_chars:
        return argv, prompt_text

    base_args = build_claude_turn_args(
        model_slug=model_slug,
        effort=effort,
        sec=sec,
        mcp_config_path=mcp_config_path,
        resume_session_id=resume_session_id,
        system_prompt="",
        add_dirs=add_dirs,
        bin_path=bin_path,
    )
    base_cmd_len = len(subprocess.list2cmdline(build_claude_exec_argv(bin_path, *base_args)))
    clean_sys = sanitize_claude_cmd_shim_text(system_prompt)
    avail = max(0, max_cmd_chars - base_cmd_len - len(' --append-system-prompt ""') - 32)
    if avail >= 200:
        cut = clean_sys[:avail]
        sep_idx = cut.rfind(" | ")
        if sep_idx >= int(avail * 0.5):
            head_sys = cut[:sep_idx].strip()
        else:
            sp_idx = cut.rfind(" ")
            head_sys = cut[:sp_idx].strip() if sp_idx >= int(avail * 0.5) else cut.strip()
    else:
        head_sys = ""

    args_fitted = build_claude_turn_args(
        model_slug=model_slug,
        effort=effort,
        sec=sec,
        mcp_config_path=mcp_config_path,
        resume_session_id=resume_session_id,
        system_prompt=head_sys,
        add_dirs=add_dirs,
        bin_path=bin_path,
    )
    argv_fitted = build_claude_exec_argv(bin_path, *args_fitted)
    while head_sys and len(subprocess.list2cmdline(argv_fitted)) > max_cmd_chars:
        head_sys = head_sys[: max(0, len(head_sys) - 250)].rstrip()
        args_fitted = build_claude_turn_args(
            model_slug=model_slug,
            effort=effort,
            sec=sec,
            mcp_config_path=mcp_config_path,
            resume_session_id=resume_session_id,
            system_prompt=head_sys,
            add_dirs=add_dirs,
            bin_path=bin_path,
        )
        argv_fitted = build_claude_exec_argv(bin_path, *args_fitted)

    overflow_text = system_prompt.strip()
    if head_sys and len(clean_sys) > len(head_sys):
        # Preserve exact remaining suffix from sanitized or raw system_prompt
        overflow_raw = clean_sys[len(head_sys) :].lstrip(" |")
        overflow_text = overflow_raw or overflow_text

    eff_stdin = (
        f"[Системные правила контура выполнения (перенесены из CLI-аргументов):]\n{overflow_text}\n\n{prompt_text}"
        if overflow_text
        else prompt_text
    )
    return argv_fitted, eff_stdin


# ---------------------------------------------------------------------------
# 5.6 Stream parsing & tool normalization
# ---------------------------------------------------------------------------

_SIMPLE_TOOL_MAP = {
    "Glob": "find_by_name",
    "Grep": "grep_search",
    "LS": "list_dir",
    "WebSearch": "search_web",
    "WebFetch": "read_url_content",
    "TodoWrite": "todo_write",
    "Task": "task",
}


def normalize_claude_tool(name: str, inp: Any) -> tuple[str, dict[str, Any]]:
    inp = inp if isinstance(inp, dict) else {}
    path = str(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "")
    if name == "Bash":
        return "run_command", {"CommandLine": str(inp.get("command") or ""), "toolSummary": str(inp.get("description") or "")}
    if name == "Write":
        return "write_file", {"Path": str(inp.get("file_path") or inp.get("path") or "")}
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "edit_file", {"Path": path}
    if name == "Read":
        return "read_file", {"Path": str(inp.get("file_path") or inp.get("path") or "")}
    if name.startswith("mcp__"):
        parts = name.split("__")
        srv = parts[1] if len(parts) > 1 else "mcp"
        tool = "__".join(parts[2:]) if len(parts) > 2 else "call"
        return f"mcp_{srv}_{tool}", dict(inp)
    if name in _SIMPLE_TOOL_MAP:
        return _SIMPLE_TOOL_MAP[name], dict(inp)
    return (name or "tool").lower(), dict(inp)


_LIMIT_KEYWORDS = (
    "429", "rate_limit_error", "rate limit", "5-hour limit", "5 hour limit", "usage limit",
    "weekly limit", "7-day limit", "7 day limit", "seven_day",
    "quota", "resets at", "limit reached", "claude ai usage limit",
)
_MISSING_SESSION_KEYWORDS = ("no conversation found", "session not found", "could not find session", "no session found")
_OPUS_UNAVAILABLE_KEYWORDS = ("opus", "not available", "not_found_error", "does not have access", "model not found")


def is_claude_limit_error(text: str) -> bool:
    low = (text or "").lower()
    return bool(low) and any(k in low for k in _LIMIT_KEYWORDS)


def is_missing_session_error(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in _MISSING_SESSION_KEYWORDS)


def humanize_claude_error(text: str, model_slug: str = "") -> str:
    low = (text or "").lower()
    if "opus" in (model_slug or "").lower() and any(k in low for k in _OPUS_UNAVAILABLE_KEYWORDS[1:]):
        return (
            "Модель Claude Opus недоступна на этом тарифе (Pro). Выберите Claude Sonnet "
            "или подключите аккаунт Max / Team."
        )
    if "invalid api key" in low or "please run /login" in low or "oauth token has expired" in low or "401" in low:
        return "Сессия Claude Code истекла или отозвана. Переподключите аккаунт в карточке Claude."
    if is_claude_limit_error(text):
        return "Достигнут лимит подписки Claude (окно 5 часов / неделя). Дождитесь сброса или подключите другой аккаунт."
    return (text or "Ошибка Claude Code CLI").strip()[:1200]


class ClaudeStreamParser:
    """Stateful NDJSON (`stream-json`) parser producing Hub stream events."""

    def __init__(
        self,
        *,
        account_id: str = "",
        proj_dir: Optional[Path] = None,
        step_offset: int = 0,
        initial_text: str = "",
        initial_thinking: str = "",
        initial_tools: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self.account_id = account_id
        self.proj_dir = proj_dir
        self.session_id = ""
        self.text = initial_text
        self.got_text_before_resume = False
        self.thinking = initial_thinking
        self.thinking_active = False
        self.got_text = False
        self.got_text_since_tool = False
        self.seq = 0
        self.tools: dict[str, dict[str, Any]] = {}
        self.tool_order: list[str] = []
        self.initial_tools = list(initial_tools or [])
        self.step_counter = step_offset
        self.usage: dict[str, Any] = {}
        self.result_seen = False
        self.result_is_error = False
        self.error_text = ""
        self.active_files: set[str] = set()
        self._block_tool_ids: dict[int, str] = {}
        self._block_json: dict[int, str] = {}
        self._got_text_delta_in_msg = False
        self._got_thinking_delta_in_msg = False
        self._orig_names: dict[str, str] = {}
        self._last_context_input_tokens: int = 0
        self._seen_assistant_blocks: set[tuple[str, int, str]] = set()

    def _record_step_usage(self, mu: Any) -> None:
        if not isinstance(mu, dict):
            return
        step_inp = (
            int(mu.get("input_tokens") or 0)
            + int(mu.get("cache_read_input_tokens") or 0)
            + int(mu.get("cache_creation_input_tokens") or 0)
        )
        if step_inp > 0:
            self._last_context_input_tokens = step_inp

    # -- helpers --
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.initial_tools + [self.tools[k] for k in self.tool_order]

    def has_active_tool(self) -> bool:
        return any(t.get("state") == "ACTIVE" for t in self.tools.values())

    def _ev(self, d: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        d["seq"] = self.seq
        return d

    def _thinking_on(self, out: list[dict[str, Any]]) -> None:
        if not self.thinking_active and not self.got_text:
            self.thinking_active = True
            out.append(self._ev({"type": "thinking_status", "active": True}))

    def _thinking_off(self, out: list[dict[str, Any]]) -> None:
        if self.thinking_active:
            self.thinking_active = False
            out.append(self._ev({"type": "thinking_status", "active": False}))

    def _add_text(self, delta: str, out: list[dict[str, Any]]) -> None:
        if not delta:
            return
        self._thinking_off(out)
        self.got_text = True
        self.got_text_since_tool = True
        if not self.got_text_before_resume:
            delta = compute_resume_stitch_prefix(self.text, delta) + delta
            self.got_text_before_resume = True
        self.text += delta
        out.append(self._ev({"type": "text_delta", "delta": delta, "content": self.text}))

    def _add_thinking(self, delta: str, out: list[dict[str, Any]]) -> None:
        if not delta:
            return
        self._thinking_on(out)
        self.thinking += delta
        out.append(self._ev({"type": "thinking_update", "thinking": self.thinking}))

    def _upsert_tool(self, tool_id: str, name: str, inp: Any, state: str, out: list[dict[str, Any]]) -> None:
        hub_name, params = normalize_claude_tool(name, inp)
        entry = self.tools.get(tool_id)
        if entry is None:
            self.step_counter += 2
            entry = {"step_index": self.step_counter, "name": hub_name, "state": state, "parameters": params, "tool_use_id": tool_id}
            self.tools[tool_id] = entry
            self.tool_order.append(tool_id)
        else:
            entry["name"] = hub_name
            if params:
                entry["parameters"] = params
            entry["state"] = state
        self.got_text_since_tool = False
        for rel in extract_writing_tool_paths(entry["name"], entry.get("parameters"), self.proj_dir):
            self.active_files.add(rel)
        out.append(self._ev({"type": "tool_step", "step": dict(entry), "tool_calls": self.tool_calls()}))

    # -- main entry --
    def feed(self, obj: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(obj, dict):
            return out
        t = obj.get("type")
        if t == "system":
            if obj.get("subtype") == "init" and obj.get("session_id"):
                self.session_id = str(obj["session_id"])
                out.append({"type": "claude_session", "claude_session_id": self.session_id, "claude_account_id": self.account_id})
            return out
        if obj.get("session_id") and not self.session_id:
            self.session_id = str(obj["session_id"])
        if t == "stream_event":
            ev = obj.get("event") if isinstance(obj.get("event"), dict) else {}
            et = ev.get("type")
            idx = int(ev.get("index") or 0)
            if et == "message_start":
                self._got_text_delta_in_msg = False
                self._got_thinking_delta_in_msg = False
                m_obj = ev.get("message") if isinstance(ev.get("message"), dict) else {}
                self._record_step_usage(m_obj.get("usage"))
            elif et == "content_block_start":
                cb = ev.get("content_block") if isinstance(ev.get("content_block"), dict) else {}
                if cb.get("type") == "tool_use":
                    tid = str(cb.get("id") or f"tool_{idx}_{self.step_counter}")
                    self._block_tool_ids[idx] = tid
                    self._block_json[idx] = ""
                    self._orig_names[tid] = str(cb.get("name") or "")
                    self._thinking_off(out)
                    self._upsert_tool(tid, str(cb.get("name") or ""), cb.get("input") or {}, "ACTIVE", out)
                elif cb.get("type") == "thinking" and self.thinking and not self.thinking.endswith("\n"):
                    self.thinking += "\n\n"
                elif cb.get("type") == "text" and self.text and not self.text.endswith("\n") and self.got_text and not self.got_text_since_tool:
                    self._add_text("\n\n", out)
            elif et == "content_block_delta":
                d = ev.get("delta") if isinstance(ev.get("delta"), dict) else {}
                dt = d.get("type")
                if dt == "thinking_delta":
                    self._got_thinking_delta_in_msg = True
                    self._add_thinking(str(d.get("thinking") or ""), out)
                elif dt == "text_delta":
                    self._got_text_delta_in_msg = True
                    self._add_text(str(d.get("text") or ""), out)
                elif dt == "input_json_delta" and idx in self._block_json:
                    self._block_json[idx] += str(d.get("partial_json") or "")
            elif et == "content_block_stop" and idx in self._block_tool_ids:
                raw = self._block_json.pop(idx, "")
                tid = self._block_tool_ids.pop(idx)
                if raw:
                    try:
                        inp = json.loads(raw)
                        ent = self.tools.get(tid)
                        if ent is not None and isinstance(inp, dict):
                            self._upsert_tool(tid, self._orig_name(ent), inp, ent.get("state") or "ACTIVE", out)
                    except Exception:
                        pass
            return out
        if t == "assistant":
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            msg_id = str(msg.get("id") or "")
            self._record_step_usage(msg.get("usage"))
            for b_idx, blk in enumerate(msg.get("content") or []):
                if not isinstance(blk, dict):
                    continue
                bt = str(blk.get("type") or "")
                if bt == "tool_use":
                    tid = str(blk.get("id") or "")
                    st = (self.tools.get(tid) or {}).get("state") or "ACTIVE"
                    self._upsert_tool(tid, str(blk.get("name") or ""), blk.get("input") or {}, st, out)
                    self._orig_names[tid] = str(blk.get("name") or "")
                elif bt == "text" and not self._got_text_delta_in_msg:
                    sig = (msg_id, b_idx, "text")
                    if not msg_id or sig not in self._seen_assistant_blocks:
                        if msg_id:
                            self._seen_assistant_blocks.add(sig)
                        self._add_text(str(blk.get("text") or ""), out)
                elif bt == "thinking" and not self._got_thinking_delta_in_msg:
                    sig_t = (msg_id, b_idx, "thinking")
                    if not msg_id or sig_t not in self._seen_assistant_blocks:
                        if msg_id:
                            self._seen_assistant_blocks.add(sig_t)
                        self._add_thinking(str(blk.get("thinking") or ""), out)
            return out
        if t == "user":
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = msg.get("content")
            for blk in content if isinstance(content, list) else []:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    tid = str(blk.get("tool_use_id") or "")
                    ent = self.tools.get(tid)
                    if ent is not None:
                        ent["state"] = "ERROR" if blk.get("is_error") else "DONE"
                        out.append(self._ev({"type": "tool_step", "step": dict(ent), "tool_calls": self.tool_calls()}))
            self._got_text_delta_in_msg = False
            self._got_thinking_delta_in_msg = False
            return out
        if t == "result":
            self.result_seen = True
            self.result_is_error = bool(obj.get("is_error")) or str(obj.get("subtype") or "success") not in ("success",)
            if self.result_is_error:
                errs = obj.get("errors")
                self.error_text = str(obj.get("result") or (" ".join(map(str, errs)) if isinstance(errs, list) else "") or obj.get("subtype") or "")
            elif not self.text and obj.get("result"):
                self._add_text(str(obj.get("result")), out)
            u = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
            cache_read = int(u.get("cache_read_input_tokens") or 0)
            inp = int(u.get("input_tokens") or 0) + cache_read + int(u.get("cache_creation_input_tokens") or 0)
            outp = int(u.get("output_tokens") or 0)
            self.usage = {
                "input_tokens": inp,
                "output_tokens": outp,
                "cached_tokens": cache_read,
                "cache_read_tokens": cache_read,
                "thinking_tokens": 0,
                "total_tokens": inp + outp,
            }
            if self._last_context_input_tokens > 0:
                self.usage["context_input_tokens"] = self._last_context_input_tokens
            if obj.get("total_cost_usd") is not None:
                self.usage["api_equiv_cost_usd"] = float(obj.get("total_cost_usd") or 0.0)
            self._thinking_off(out)
            return out
        return out

    def _orig_name(self, ent: dict[str, Any]) -> str:
        return self._orig_names.get(str(ent.get("tool_use_id") or ""), "") or _reverse_hub_name(ent.get("name") or "")


def _reverse_hub_name(hub: str) -> str:
    rev = {"run_command": "Bash", "write_file": "Write", "edit_file": "Edit", "read_file": "Read"}
    for k, v in _SIMPLE_TOOL_MAP.items():
        rev[v] = k
    if hub in rev:
        return rev[hub]
    if hub.startswith("mcp_"):
        rest = hub[4:]
        for srv in ("hub_openrouter", "hub_sandbox", "hub_host"):
            if rest.startswith(srv + "_"):
                return f"mcp__{srv}__{rest[len(srv) + 1:]}"
    return hub


# ---------------------------------------------------------------------------
# MCP URLs (re-use the same token scheme as Codex)
# ---------------------------------------------------------------------------


async def _build_user_mcp_urls(
    user: Optional[dict[str, Any]],
    *,
    allow_hub_openrouter: bool,
    allow_hub_sandbox: bool,
    chat_id: str = "",
) -> tuple[str, str]:
    from .codex_engine import _build_user_mcp_urls as _codex_urls

    return await _codex_urls(
        user,
        allow_hub_openrouter=allow_hub_openrouter,
        allow_hub_sandbox=allow_hub_sandbox,
        chat_id=chat_id,
    )


def is_claude_account_exhausted(acc: dict[str, Any]) -> bool:
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


async def _pick_next_failover_account(user_id: str, tried: set[str]) -> Optional[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for acc in await database.list_user_claude_accounts(user_id):
        aid = str(acc.get("id") or "")
        if not aid or aid in tried or not acc.get("is_enabled"):
            continue
        if is_claude_account_exhausted(acc):
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


async def resolve_claude_account_for_chat_turn(
    user_id: str,
    *,
    preferred_account_id: str = "",
    current_chat_account_id: str = "",
) -> Optional[dict[str, Any]]:
    accounts = await database.list_user_claude_accounts(user_id)
    enabled = [a for a in accounts if a.get("is_enabled")]
    if not enabled:
        return None
    by_id = {str(a["id"]): a for a in enabled if a.get("id")}
    pref_id = str(preferred_account_id or "").strip()
    cur_id = str(current_chat_account_id or "").strip()

    if pref_id and pref_id in by_id:
        return by_id[pref_id]

    if cur_id and cur_id in by_id:
        bound = by_id[cur_id]
        if not is_claude_account_exhausted(bound) or not bound.get("auto_failover", True):
            return bound

    active = next((a for a in enabled if a.get("is_active")), enabled[0])
    if not is_claude_account_exhausted(active) or not active.get("auto_failover", True):
        return active

    tried = {str(active["id"])}
    if cur_id:
        tried.add(cur_id)
    nxt = await _pick_next_failover_account(user_id, tried)
    return nxt or active


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


# ---------------------------------------------------------------------------
# Streaming turn
# ---------------------------------------------------------------------------


async def stream_claude_turn(
    *,
    chat_id: str,
    kind: str,
    project_name: str,
    model: str,
    access_mode: str,
    prompt: str,
    attachments: list[dict[str, Any]],
    claude_session_id: str,
    claude_account_id: str,
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
    preferred_claude_account_id: str = "",
    disable_default_guardrails: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Execute a turn via `claude -p --output-format stream-json` and yield Hub stream events."""
    init_tools = normalize_interrupted_tool_calls(initial_tool_calls)

    def _err(
        msg: str,
        text: str = "",
        thinking: str = "",
        tools: Optional[list] = None,
        *,
        quota_exhausted: bool = False,
    ) -> dict[str, Any]:
        evt_out: dict[str, Any] = {
            "type": "error",
            "error": msg,
            "content": text or initial_content or "",
            "thinking": thinking or initial_thinking or "",
            "tool_calls": tools if tools is not None else init_tools,
        }
        if quota_exhausted:
            evt_out["quota_exhausted"] = True
        return evt_out

    bin_path = resolve_claude_bin()
    if not bin_path:
        yield _err("На сервере не найден Claude Code CLI (@anthropic-ai/claude-code). Откройте карточку Claude для установки.")
        return

    user_id = str((user or {}).get("id") or "admin").strip() or "admin"
    is_admin = bool(user is None or user.get("role") == "admin")
    custom_rules = str((user or {}).get("custom_rules") or "").strip()

    active = await resolve_claude_account_for_chat_turn(
        user_id,
        preferred_account_id=preferred_claude_account_id,
        current_chat_account_id=claude_account_id,
    )
    if not active:
        yield _err("Личный аккаунт Claude не подключён или поставлен на паузу. Откройте карточку Claude в левой панели.")
        return

    await asyncio.to_thread(sync_user_cli_rules, user if user is not None else user_id, custom_rules)

    safe_cid = re.sub(r"[^a-zA-Z0-9_.\-]+", "_", chat_id or "chat")
    proj_dir: Optional[Path] = None
    if kind == "project" and project_name:
        if resolved_project_dir is not None and resolved_project_dir.is_dir():
            proj_dir = resolved_project_dir.resolve()
        elif is_admin:
            proj_dir = resolve_project_dir(project_name)
        if not proj_dir or not proj_dir.is_dir():
            yield _err(f"Папка проекта '{project_name}' не найдена или недоступна")
            return
        await asyncio.to_thread(ensure_isolated_project_git_boundary, proj_dir)
        workspace_dir = proj_dir
    else:
        eff_chat_user = user if user is not None else {"id": user_id or "admin", "username": "admin" if is_admin else "", "role": "admin" if is_admin else "user"}
        workspace_dir = await asyncio.to_thread(
            config.resolve_user_chat_sandbox_dir, eff_chat_user, safe_cid, create=True
        )
        await asyncio.to_thread(ensure_isolated_project_git_boundary, workspace_dir.parent)

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
    turn_chat_ref = {"id": safe_cid, "user_id": user_id}
    owner_uname = str((user or {}).get("username") or "")
    turn_upload_dirs: list[Path] = []
    try:
        if attachments:
            primary_up = config.resolve_chat_uploads_dir(
                turn_chat_ref,
                owner_username=owner_uname,
                is_shared_project=is_shared_turn,
            )
            primary_up.mkdir(parents=True, exist_ok=True)
        turn_upload_dirs = config.resolve_chat_upload_dirs_for_turn(
            turn_chat_ref,
            chat_id=safe_cid,
            owner_username=owner_uname,
            is_shared_project=is_shared_turn,
        )
    except Exception:
        turn_upload_dirs = []

    eff_exec_mode = (
        resolve_effective_project_execution_mode(user if user is not None else {"role": "admin"}, execution_mode)
        if kind == "project"
        else ("host" if is_admin else "sandbox_files")
    )
    can_host_shell = bool(kind == "project" and (is_admin or str((user or {}).get("max_access_mode") or "") == "host_full"))
    force_no_host_shell = bool(kind != "project" or eff_exec_mode != "host" or not can_host_shell)
    allow_hub_sandbox = False
    docker_info: Optional[dict[str, Any]] = None
    eff_access = access_mode
    if kind == "project" and proj_dir is not None and eff_exec_mode == "docker" and access_mode == "full_access":
        try:
            docker_info = await check_docker_status()
        except Exception:
            docker_info = {}
        if docker_info.get("sandbox_mcp_ready"):
            allow_hub_sandbox = True
        else:
            eff_access = "write_files"
    if force_no_host_shell and eff_access == "full_access" and not allow_hub_sandbox:
        eff_access = "write_files"

    exec_contour, docker_unavail_reason = resolve_turn_execution_contour(
        kind=kind,
        requested_access_mode=access_mode,
        eff_exec_mode=eff_exec_mode,
        force_no_host_shell=force_no_host_shell,
        allow_hub_sandbox=allow_hub_sandbox,
        user=user,
        docker_info=docker_info,
    )

    validated_extra_dirs: list[Path] = [
        Path(ed).resolve() for ed in (extra_dirs or []) if ed is not None and Path(ed).is_dir()
    ]
    allow_hub_host = bool(
        kind == "project"
        and eff_exec_mode == "host"
        and can_host_shell
        and eff_access == "full_access"
        and not force_no_host_shell
    )
    sbx_net = str((user or {}).get("sandbox_network") or "internet").strip().lower() or "internet"
    sec = resolve_claude_security_matrix(
        kind=kind,
        access_mode=eff_access,
        is_admin=is_admin,
        force_no_host_shell=force_no_host_shell,
        allow_hub_openrouter=allow_hub_openrouter,
        allow_hub_sandbox=allow_hub_sandbox,
        allow_hub_host=allow_hub_host,
        sandbox_network=sbx_net,
    )
    or_url, sbx_url = await _build_user_mcp_urls(
        user,
        allow_hub_openrouter=bool(sec["allow_hub_openrouter"]),
        allow_hub_sandbox=bool(sec["allow_hub_sandbox"]),
        chat_id=chat_id,
    )
    from .codex_engine import _build_host_mcp_url

    host_url = await _build_host_mcp_url(
        user,
        user_id=user_id,
        allow_hub_host=bool(sec.get("allow_hub_host", False)),
    )
    if not or_url and "mcp__hub_openrouter__*" in sec["allowed_tools"]:
        sec["allowed_tools"].remove("mcp__hub_openrouter__*")

    extra_add_dirs: list[Path] = []
    if kind == "project" and proj_dir is not None:
        extra_add_dirs.append(proj_dir)
    for ed in validated_extra_dirs:
        if ed not in extra_add_dirs:
            extra_add_dirs.append(ed)
    for ud_item in turn_upload_dirs:
        if ud_item not in extra_add_dirs:
            extra_add_dirs.append(ud_item)
    if is_admin and kind == "project":
        if config.PROJECTS_ROOT.exists():
            extra_add_dirs.append(config.PROJECTS_ROOT.resolve())
        if config.SHARED_PROJECTS_DIR.exists():
            extra_add_dirs.append(config.SHARED_PROJECTS_DIR.resolve())

    sys_parts: list[str] = []
    if kind == "project":
        rules = await asyncio.to_thread(
            lambda: _read_project_hub_rules(
                proj_dir,
                access_mode=eff_access,
                disable_default_guardrails=disable_default_guardrails,
            )
        )
        if rules:
            sys_parts.append(rules)
        if validated_extra_dirs:
            ed_str = ", ".join(f"`{d}`" for d in validated_extra_dirs)
            sys_parts.append(
                f"К этому проекту подключены дополнительные рабочие директории (`Extra Workspace Directories`): {ed_str}."
            )
        if is_admin and config.PROJECTS_ROOT.exists() and eff_access != "full_access":
            sys_parts.append(
                f"Каталог соседних проектов `{config.PROJECTS_ROOT.resolve()}` открыт ТОЛЬКО для справочного чтения; "
                f"создание и редактирование файлов разрешено строго внутри текущего проекта `{workspace_dir}`"
                + (" и подключённых `Extra Workspace Directories`." if validated_extra_dirs else ".")
            )
    if custom_rules:
        sys_parts.append(custom_rules)
    if host_url and sec.get("allow_hub_host"):
        sys_parts.append(HOST_SERVICES_PROMPT_NOTE)
    if sec["allow_hub_sandbox"]:
        sys_parts.append(SANDBOX_PROMPT_NOTE)
    elif exec_contour == "docker_unavailable":
        sys_parts.append(
            f"Хостовый Bash заблокирован политикой безопасности, а Docker-песочница сейчас недоступна ({docker_unavail_reason}). "
            "Чтение и редактирование файлов в папке проекта открыты напрямую; если требуется терминал, явно сообщи пользователю причину."
        )
    system_prompt = "\n\n".join(sys_parts)[:26000]

    run_state = ACTIVE_RUNS.get(chat_id)
    if run_state is None:
        run_state = register_chat_turn(
            chat_id,
            user_id=user_id,
            project_name=project_name if kind == "project" else "",
            chat_title=chat_title,
            access_mode=eff_access,
            scope_key=project_scope_key if kind == "project" else "",
        )
    else:
        run_state.user_id = user_id
        run_state.project_name = project_name if kind == "project" else ""
        run_state.chat_title = chat_title
        run_state.scope_key = project_scope_key if kind == "project" else ""
    run_state.kind = kind
    run_state.project_dir = proj_dir
    run_state.sandbox_allowed = bool(sec["allow_hub_sandbox"])
    run_state.host_allowed = bool(sec.get("allow_hub_host", False) and host_url)
    run_state.extra_dirs = list(validated_extra_dirs)
    run_state.access_mode = eff_access
    run_state.model = str(model or "")
    run_state.engine = "claude_code"
    run_state.accumulated_text = initial_content or ""
    run_state.accumulated_thinking = initial_thinking or ""
    run_state.tool_calls = list(init_tools)

    model_slug, effort = parse_claude_code_model_spec(model)
    current_acc_id = str(active["id"])
    active_session = claude_session_id if (claude_session_id and (not claude_account_id or claude_account_id == current_acc_id)) else ""

    def _build_prompt(fresh: bool) -> str:
        pulse = ""
        if kind == "project" and project_name:
            pulse = build_project_pulse_block(
                project_name=project_name,
                chat_id=chat_id,
                is_first_turn=fresh,
                scope_key=project_scope_key,
                changelog=project_changelog,
                recent_chats=recent_project_chats,
                prev_chat_updated_at=prev_chat_updated_at,
            )
        aug = _augment_prompt_for_mode(
            kind=kind,
            prompt=prompt,
            attachments=[] if (is_continue_turn and not fresh) else attachments,
            is_first_turn=fresh,
            access_mode=eff_access,
            inline_hub_rules="",
            project_pulse_block=pulse,
            referenced_chat_blocks=referenced_chat_blocks,
            live_voice=live_voice,
            execution_contour=exec_contour,
            docker_unavailable_reason=docker_unavail_reason,
            extra_dirs=validated_extra_dirs or None,
            project_dir=proj_dir,
            disable_default_guardrails=True,
        )
        if fresh:
            return _build_history_recovery_prompt(
                history_messages,
                aug,
                compacted_summary=compacted_summary,
                compacted_up_to_msg_id=compacted_up_to_msg_id,
                resume_tool_calls=run_state.tool_calls if (is_continue_turn or init_tools) else None,
                partial_assistant_text=run_state.accumulated_text if (is_continue_turn or (initial_content or "").strip()) else "",
                is_continue_turn=is_continue_turn,
            )
        return aug

    tried: set[str] = {current_acc_id}
    override_prompt: Optional[str] = None
    consecutive_ac = 0
    total_ac = 0
    capacity_retries = 0
    session_reset_done = False
    use_effort_flag = True
    step_offset = max([int(t.get("step_index") or 0) for t in init_tools] or [0])
    final_usage: dict[str, Any] = {}

    async def _drain_stderr_buf(stream: Optional[asyncio.StreamReader], buf: list[str]) -> None:
        if stream is None:
            return
        acc_s = ""
        try:
            while True:
                ch = await stream.read(4096)
                if not ch:
                    break
                acc_s = (acc_s + ch.decode("utf-8", errors="replace"))[-16000:]
                buf[0] = acc_s
        except Exception:
            pass

    try:
        while True:
            if run_state.cancelled:
                yield {"type": "cancelled", "content": run_state.accumulated_text, "thinking": run_state.accumulated_thinking, "tool_calls": run_state.tool_calls}
                return

            acc = await database.get_user_claude_account(user_id, current_acc_id, include_decrypted_credentials=True)
            if not acc or not acc.get("credentials_json"):
                yield _err("Не удалось расшифровать учётные данные Claude. Переподключите аккаунт.", run_state.accumulated_text, run_state.accumulated_thinking, run_state.tool_calls)
                return
            try:
                acc_proxy_url = await proxy_router.ensure_proxy_for_context(
                    "claude",
                    user_id=user_id,
                    account_id=current_acc_id,
                    is_admin=is_admin,
                )
            except proxy_router.ProxyRouteError as exc:
                yield _err(str(exc), run_state.accumulated_text, run_state.accumulated_thinking, run_state.tool_calls)
                return
            acc_dir, env = await asyncio.to_thread(
                materialize_account_claude_home,
                user_id,
                acc,
                user_or_id=user if user is not None else user_id,
                proxy_url=acc_proxy_url,
            )
            mcp_path = await asyncio.to_thread(
                write_claude_mcp_config,
                acc_dir,
                openrouter_url=or_url,
                sandbox_url=sbx_url,
                host_url=host_url,
            )
            prompt_text = override_prompt if override_prompt is not None else _build_prompt(not active_session)
            turn_argv, prompt_text = prepare_claude_turn_argv_and_stdin(
                bin_path,
                prompt_text=prompt_text,
                model_slug=model_slug,
                effort=effort if use_effort_flag else "",
                sec=sec,
                mcp_config_path=mcp_path,
                resume_session_id=active_session,
                system_prompt=system_prompt,
                add_dirs=extra_add_dirs or None,
            )
            proc = await _spawn(turn_argv, env, workspace_dir)
            run_state.process = proc
            stderr_holder = [""]
            stderr_task = asyncio.create_task(_drain_stderr_buf(proc.stderr, stderr_holder))
            parser = ClaudeStreamParser(
                account_id=current_acc_id,
                proj_dir=proj_dir,
                step_offset=step_offset,
                initial_text=run_state.accumulated_text,
                initial_thinking=run_state.accumulated_thinking,
                initial_tools=run_state.tool_calls,
            )
            if not run_state.accumulated_text:
                run_state.thinking_active = True
                parser.thinking_active = True
                run_state.seq += 1
                yield {"type": "thinking_status", "active": True, "seq": run_state.seq}

            watchdog_network = False
            watchdog_post_text = False
            got_any_line = False
            stderr_text = ""
            try:
                assert proc.stdin is not None and proc.stdout is not None
                try:
                    proc.stdin.write(prompt_text.encode("utf-8"))
                    await proc.stdin.drain()
                except Exception:
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

                while True:
                    if run_state.cancelled:
                        break
                    if not got_any_line:
                        timeout, post_cand = STREAM_STARTUP_IDLE_TIMEOUT_SEC, False
                    elif parser.has_active_tool():
                        timeout, post_cand = STREAM_ACTIVE_TOOL_TIMEOUT_SEC, False
                    elif parser.got_text_since_tool and looks_like_completed_response(parser.text):
                        timeout, post_cand = STREAM_POST_TEXT_IDLE_TIMEOUT_SEC, True
                    else:
                        timeout, post_cand = STREAM_NETWORK_IDLE_TIMEOUT_SEC, False
                    try:
                        raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                    except asyncio.TimeoutError:
                        if post_cand:
                            watchdog_post_text = True
                        else:
                            watchdog_network = True
                        break
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    got_any_line = True
                    for ev in parser.feed(obj):
                        if "seq" in ev:
                            run_state.seq += 1
                            ev["seq"] = run_state.seq
                        if ev["type"] == "claude_session":
                            active_session = parser.session_id
                        run_state.accumulated_text = parser.text
                        run_state.accumulated_thinking = parser.thinking
                        run_state.tool_calls = parser.tool_calls()
                        run_state.thinking_active = parser.thinking_active
                        run_state.active_files |= parser.active_files
                        yield ev
                    if parser.result_seen:
                        break
            finally:
                await _terminate_proc_tree(proc)
                try:
                    await asyncio.wait_for(stderr_task, timeout=0.8)
                except Exception:
                    stderr_task.cancel()
                stderr_text = strip_ansi(stderr_holder[0]).strip()
                await sync_refreshed_credentials_back(user_id, current_acc_id, acc_dir)

            step_offset = parser.step_counter
            if run_state.cancelled:
                yield {"type": "cancelled", "content": run_state.accumulated_text, "thinking": run_state.accumulated_thinking, "tool_calls": run_state.tool_calls}
                return

            err_combined = f"{parser.error_text} {stderr_text}".strip()
            failed = bool(parser.result_is_error or (not parser.result_seen and not watchdog_post_text))
            if watchdog_post_text and run_state.accumulated_text.strip():
                failed = False
            produced = bool(parser.text.strip() != (initial_content or "").strip() or parser.tools)

            # Older CLI version without `--effort` flag -> transparent fallback without `--effort`
            err_low = err_combined.lower()
            if (
                failed
                and use_effort_flag
                and effort
                and not produced
                and "--effort" in err_low
                and ("unknown option" in err_low or "unrecognized" in err_low or "invalid" in err_low)
            ):
                use_effort_flag = False
                continue

            # Lost --resume session -> rebuild from SQLite history
            if failed and active_session and not session_reset_done and is_missing_session_error(err_combined):
                session_reset_done = True
                active_session = ""
                override_prompt = None
                yield {"type": "claude_session", "claude_session_id": "", "claude_account_id": current_acc_id}
                continue

            # Subscription limit -> always clamp exhausted window (5h / 7d) + optional auto-failover
            is_limit_err = bool(failed and is_claude_limit_error(err_combined))
            if is_limit_err:
                exhausted = _clamp_exhausted_claude_quotas(acc.get("quotas"), err_combined)
                can_failover = not produced and bool(acc.get("auto_failover", True))
                nxt = await _pick_next_failover_account(user_id, tried) if can_failover else None
                limit_note = (
                    "Лимит подписки Claude исчерпан (автопереключение)"
                    if nxt is not None
                    else humanize_claude_error(err_combined or "Лимит подписки Claude исчерпан", model_slug)[:300]
                )
                await database.update_claude_account_telemetry(
                    user_id,
                    current_acc_id,
                    quotas=exhausted,
                    last_error=limit_note,
                )
                if nxt is not None:
                    prev_label = str(acc.get("label") or acc.get("email") or current_acc_id)
                    current_acc_id = str(nxt["id"])
                    tried.add(current_acc_id)
                    active_session = ""
                    override_prompt = None
                    yield {"type": "claude_session", "claude_session_id": "", "claude_account_id": current_acc_id}
                    yield {
                        "type": "claude_failover",
                        "from_label": prev_label,
                        "to_label": str(nxt.get("label") or nxt.get("email") or current_acc_id),
                        "account_id": current_acc_id,
                        "from_account_id": str(acc["id"]),
                    }
                    continue

            is_cap = is_transient_capacity_error(err_combined)
            is_trans = watchdog_network or is_transient_stream_error(err_combined)
            if failed and not is_limit_err and (is_cap or is_trans):
                if not run_state.accumulated_text.strip() and capacity_retries < 2:
                    capacity_retries += 1
                    if not await _sleep_unless_cancelled(CAPACITY_RETRY_BACKOFF_SECS[min(capacity_retries - 1, 1)], run_state):
                        yield {"type": "cancelled", "content": run_state.accumulated_text, "thinking": run_state.accumulated_thinking, "tool_calls": run_state.tool_calls}
                        return
                    continue
                if active_session and consecutive_ac < MAX_CONSECUTIVE_AUTO_CONTINUES and total_ac < MAX_TOTAL_AUTO_CONTINUES:
                    consecutive_ac = consecutive_ac + 1 if not produced else 1
                    total_ac += 1
                    for t in run_state.tool_calls:
                        if t.get("state") == "ACTIVE":
                            t["state"] = "INTERRUPTED"
                    if not await _sleep_unless_cancelled(AUTO_CONTINUE_PAUSE_SEC, run_state):
                        yield {"type": "cancelled", "content": run_state.accumulated_text, "thinking": run_state.accumulated_thinking, "tool_calls": run_state.tool_calls}
                        return
                    tail = run_state.accumulated_text.strip()[-180:]
                    override_prompt = (
                        "The upstream stream was interrupted mid-turn. Continue the task from where it stopped "
                        "without repeating completed tool steps."
                        + (f" The user already sees text ending with: «...{tail}...» — continue right after it." if tail else "")
                    )
                    continue

            if failed:
                clean = humanize_claude_error(err_combined or "Claude Code CLI завершился без результата", model_slug)
                if not is_limit_err:
                    await database.update_claude_account_telemetry(user_id, current_acc_id, last_error=clean[:300])
                yield _err(
                    clean,
                    run_state.accumulated_text,
                    run_state.accumulated_thinking,
                    run_state.tool_calls,
                    quota_exhausted=is_limit_err,
                )
                return

            if acc.get("last_error"):
                await database.update_claude_account_telemetry(user_id, current_acc_id, last_error="")
            final_usage = dict(parser.usage) or {
                "input_tokens": max(1, len(prompt) // 3),
                "output_tokens": max(1, len(run_state.accumulated_text) // 3),
                "cache_read_tokens": 0,
                "thinking_tokens": 0,
            }
            final_usage.setdefault("total_tokens", int(final_usage.get("input_tokens", 0)) + int(final_usage.get("output_tokens", 0)))
            final_usage["provider"] = "claude_code"
            final_usage["claude_account_id"] = current_acc_id
            final_usage["model_context_window"] = CLAUDE_CODE_CONTEXT_WINDOW
            for t in run_state.tool_calls:
                if t.get("state") == "ACTIVE":
                    t["state"] = "DONE"
            yield {
                "type": "done",
                "content": run_state.accumulated_text,
                "thinking": run_state.accumulated_thinking,
                "tool_calls": run_state.tool_calls,
                "usage": final_usage,
                "claude_session_id": active_session,
                "claude_account_id": current_acc_id,
            }
            return
    finally:
        try:
            from .sandbox_service import cancel_chat_sandbox_execs

            await cancel_chat_sandbox_execs(chat_id)
        except Exception:
            pass
        ACTIVE_RUNS.pop(chat_id, None)


async def run_one_shot_claude_prompt(
    user_id: str,
    prompt: str,
    model_id: str = "",
    timeout_sec: float = 120.0,
) -> dict[str, Any]:
    """Fast read-only, tool-less single turn (used by `/compact` and project passport generation)."""
    bin_path = resolve_claude_bin()
    if not bin_path:
        raise RuntimeError("На сервере не установлен Claude Code CLI")
    active = await database.get_active_claude_account(user_id, include_decrypted_credentials=True, require_enabled=True)
    if not active:
        raise RuntimeError("У пользователя нет активного аккаунта Claude")
    acc_proxy_url = await proxy_router.ensure_proxy_for_context(
        "claude",
        user_id=user_id,
        account_id=str(active["id"]),
    )
    mid = model_id if is_claude_code_model(model_id) else pick_fast_claude_utility_model(active)
    slug, effort = parse_claude_code_model_spec(mid)
    acc_dir, env = await asyncio.to_thread(
        materialize_account_claude_home,
        user_id,
        active,
        proxy_url=acc_proxy_url,
    )
    mcp_path = await asyncio.to_thread(write_claude_mcp_config, acc_dir)
    sec = {"permission_mode": "plan", "disallowed_tools": READ_ONLY_DISALLOWED + ["WebFetch", "WebSearch"], "allowed_tools": []}
    work = (CHAT_SANDBOX_DIR / "_oneshot").resolve()
    work.mkdir(parents=True, exist_ok=True)

    async def _run_once(eff_arg: str) -> tuple[ClaudeStreamParser, str]:
        args = build_claude_turn_args(model_slug=slug, effort=eff_arg, sec=sec, mcp_config_path=mcp_path)
        proc = await _spawn(build_claude_exec_argv(bin_path, *args), env, work)
        p = ClaudeStreamParser(account_id=str(active["id"]))
        err_holder = [""]

        async def _drain_err() -> None:
            if proc.stderr is None:
                return
            buf = ""
            try:
                while True:
                    ch = await proc.stderr.read(4096)
                    if not ch:
                        break
                    buf = (buf + ch.decode("utf-8", errors="replace"))[-8000:]
                    err_holder[0] = buf
            except Exception:
                pass

        err_task = asyncio.create_task(_drain_err())

        async def _consume() -> None:
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace").strip() or "null")
                except Exception:
                    continue
                p.feed(obj)
                if p.result_seen:
                    break

        try:
            await asyncio.wait_for(_consume(), timeout=timeout_sec)
        finally:
            await _terminate_proc_tree(proc)
            try:
                await asyncio.wait_for(err_task, timeout=0.5)
            except Exception:
                err_task.cancel()
            await sync_refreshed_credentials_back(user_id, str(active["id"]), acc_dir)
        return p, strip_ansi(err_holder[0]).strip()

    parser, stderr_out = await _run_once(effort)
    err_comb = f"{parser.error_text} {stderr_out}".strip()
    if (
        not parser.text.strip()
        and effort
        and "--effort" in err_comb.lower()
        and ("unknown option" in err_comb.lower() or "unrecognized" in err_comb.lower())
    ):
        parser, stderr_out = await _run_once("")
        err_comb = f"{parser.error_text} {stderr_out}".strip()
    if parser.result_is_error and not parser.text.strip():
        raise RuntimeError(humanize_claude_error(err_comb or parser.error_text, slug))
    if not parser.text.strip():
        raise RuntimeError(humanize_claude_error(err_comb, slug) if err_comb else "Модель Claude вернула пустой ответ")
    usage = dict(parser.usage)
    usage["provider"] = "claude_code"
    usage["claude_account_id"] = str(active["id"])
    return {"text": parser.text.strip(), "usage": usage, "model": mid}
