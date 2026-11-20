from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Literal

import numpy as np

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import (
    auth,
    brain_reader,
    certs,
    claude_engine,
    codex_engine,
    config,
    database,
    env_service,
    gdrive_service,
    git_service,
    host_service_manager,
    model_advisor_service,
    openrouter_service,
    project_migrator,
    proxy_router,
    release_service,
    sandbox_service,
    tts_service,
    tunnel,
    voice,
    workspace_mcp,
)
from .claude_engine import is_claude_code_model, stream_claude_turn
from .codex_engine import is_codex_model, stream_codex_turn
from .agy_engine import (
    ACTIVE_RUNS as active_generations,
    AgyClientUnavailableError,
    _build_continue_turn_prompt,
    _build_history_recovery_prompt,
    build_auto_changelog_entry_from_turn,
    cancel_chat_generation,
    check_docker_status,
    classify_model_family,
    clear_chat_turn,
    compact_chat_context,
    compute_chat_context_stats,
    fetch_account_quotas,
    fetch_available_models,
    find_active_turn_for_user,
    generate_project_passport_via_cli,
    get_active_run_snapshot,
    get_cached_account_quotas_fast,
    get_cached_models,
    get_cached_quotas,
    get_cli_auth_status,
    get_cli_version_info,
    get_parallel_project_runs,
    infer_account_tier,
    invalidate_agy_route_caches,
    is_agy_quota_cache_stale,
    is_chat_generating,
    is_gemini_location_unsupported_error,
    is_transient_capacity_error,
    is_transient_stream_error,
    normalize_interrupted_tool_calls,
    pick_default_model_id,
    probe_cli_auth_and_refresh_models,
    register_chat_turn,
    resolve_active_model_id,
    run_cli_self_update,
    stream_agy_turn,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
COMPACTING_CHATS: set[str] = set()
COMPACTING_OWNERS: dict[str, str] = {}
COMPACTING_TASKS: dict[str, asyncio.Task] = {}
ACTIVE_TURN_TASKS: dict[str, asyncio.Task] = {}
CHAT_TURN_QUEUES: dict[str, list[dict[str, Any]]] = {}
CHAT_QUEUE_PAUSED: set[str] = set()
_INFLIGHT_HQ_RETURN_UIDS: set[tuple[str, str]] = set()


def _try_acquire_compaction(
    chat_id: str,
    *,
    allow_current_turn_task: bool = False,
) -> Optional[str]:
    """Synchronously reserve `chat_id` for compaction (0 awaits) if no other compaction or turn is active."""
    if not chat_id or chat_id in COMPACTING_CHATS or is_chat_generating(chat_id):
        return None
    active_t = ACTIVE_TURN_TASKS.get(chat_id)
    cur_t = asyncio.current_task()
    if active_t is not None and not active_t.done():
        if not (allow_current_turn_task and active_t is cur_t):
            return None
    token = uuid.uuid4().hex
    COMPACTING_CHATS.add(chat_id)
    COMPACTING_OWNERS[chat_id] = token
    if cur_t is not None and active_t is not cur_t:
        COMPACTING_TASKS[chat_id] = cur_t
    return token


def _release_compaction(chat_id: str, token: str) -> bool:
    """Release compaction ownership only if `token` matches the active reservation."""
    if not token or COMPACTING_OWNERS.get(chat_id) != token:
        return False
    COMPACTING_OWNERS.pop(chat_id, None)
    COMPACTING_CHATS.discard(chat_id)
    cur_t = asyncio.current_task()
    if COMPACTING_TASKS.get(chat_id) is cur_t or (
        COMPACTING_TASKS.get(chat_id) is not None and COMPACTING_TASKS[chat_id].done()
    ):
        COMPACTING_TASKS.pop(chat_id, None)
    return True

_INTERRUPT_DIVIDER_MARKERS = (
    "\n\n---\n⚠️ **Ошибка:**",
    "\n\n---\n⚠️ **Внутренняя ошибка сервера:**",
)
_INTERRUPT_TRAILING_NOTES = (
    "\n\n*(Генерация прервана при перезапуске сервера)*",
    "\n\n*(Генерация остановлена пользователем)*",
    "\n\n*(Генерация остановлена)*",
    "\n\n⚠️ *Генерация ответа была прервана при перезапуске сервера.*",
)
_INTERRUPT_STANDALONE_EXACT = {
    "*(Генерация остановлена)*",
    "*(Генерация остановлена пользователем)*",
    "*(Генерация прервана при перезапуске сервера)*",
    "⚠️ *Генерация ответа была прервана при перезапуске сервера.*",
}
_INTERRUPT_STANDALONE_PREFIXES = (
    "⚠️ **Ошибка:**",
    "⚠️ **Внутренняя ошибка сервера:**",
)
_SUMMABLE_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "thought_tokens",
    "cache_read_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "total_tokens",
    "elapsed_ms",
    "duration_ms",
)


def strip_interrupt_banner_from_content(content: str) -> str:
    """Safely strip Hub error/cancel/restart banners without touching model Markdown before them."""
    if not content:
        return ""
    text = str(content)
    stripped_any = False

    best_idx = -1
    for marker in _INTERRUPT_DIVIDER_MARKERS:
        idx = text.rfind(marker)
        if idx > best_idx:
            best_idx = idx
    if best_idx >= 0:
        text = text[:best_idx]
        stripped_any = True

    while True:
        trimmed_right = text.rstrip()
        matched_suffix = False
        for note in _INTERRUPT_TRAILING_NOTES:
            if trimmed_right.endswith(note):
                text = trimmed_right[: -len(note)]
                stripped_any = True
                matched_suffix = True
                break
        if not matched_suffix:
            break

    probe = text.strip()
    if not probe:
        return ""
    if probe in _INTERRUPT_STANDALONE_EXACT:
        return ""
    if best_idx < 0 and any(probe.startswith(pref) for pref in _INTERRUPT_STANDALONE_PREFIXES):
        return ""

    return text.rstrip() if stripped_any else text


def merge_assistant_usage(
    prev_usage: Optional[dict[str, Any]],
    new_usage: Optional[dict[str, Any]] = None,
    *,
    clear_last_error: bool = False,
    last_error: Optional[str] = None,
) -> dict[str, Any]:
    """Merge token/time telemetry across resumed runs while preserving `code_handoffs` and tracking `proc_runs` + `last_run_*`."""
    prev = dict(prev_usage) if isinstance(prev_usage, dict) else {}
    incoming = dict(new_usage) if isinstance(new_usage, dict) else {}
    merged: dict[str, Any] = dict(prev)

    prev_prov = str(prev.get("provider") or "").strip()
    inc_prov = str(incoming.get("provider") or "").strip()
    if prev_prov and inc_prov and prev_prov != inc_prov:
        merged["provider"] = inc_prov
        for stale_k in (
            "context_input_tokens",
            "model_context_window",
            "context_window",
            "cache_creation_tokens",
            "cache_creation_input_tokens",
            "agy_conversation_id",
        ):
            if stale_k not in incoming:
                merged.pop(stale_k, None)

    prev_tok_sum = int(prev.get("input_tokens") or 0) + int(prev.get("cache_read_tokens") or 0)
    inc_tok_sum = int(incoming.get("input_tokens") or 0) + int(incoming.get("cache_read_tokens") or 0)
    prev_runs = int(prev.get("proc_runs") or (1 if prev_tok_sum > 0 else 0))
    inc_runs = int(incoming.get("proc_runs") or (1 if inc_tok_sum > 0 else 0))

    _LAST_RUN_KEYS = (
        "last_run_input_tokens",
        "last_run_cache_tokens",
        "last_run_output_tokens",
        "last_run_thinking_tokens",
        "last_run_llm_calls",
        "proc_runs",
    )

    for k, v in incoming.items():
        if k == "code_handoffs" or k in _LAST_RUN_KEYS:
            continue
        if k in _SUMMABLE_USAGE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
            old_val = prev.get(k, 0)
            if isinstance(old_val, (int, float)) and not isinstance(old_val, bool):
                merged[k] = old_val + v
            else:
                merged[k] = v
        else:
            merged[k] = v

    if prev_runs + inc_runs > 0:
        merged["proc_runs"] = prev_runs + inc_runs

    if "last_run_input_tokens" in incoming or "last_run_cache_tokens" in incoming:
        merged["last_run_input_tokens"] = int(incoming.get("last_run_input_tokens") or 0)
        merged["last_run_cache_tokens"] = int(incoming.get("last_run_cache_tokens") or 0)
        if "last_run_output_tokens" in incoming:
            merged["last_run_output_tokens"] = int(incoming.get("last_run_output_tokens") or 0)
        if "last_run_thinking_tokens" in incoming:
            merged["last_run_thinking_tokens"] = int(incoming.get("last_run_thinking_tokens") or 0)
        if "last_run_llm_calls" in incoming:
            merged["last_run_llm_calls"] = max(1, int(incoming.get("last_run_llm_calls") or 1))
    elif inc_tok_sum > 0:
        merged["last_run_input_tokens"] = int(incoming.get("input_tokens") or 0)
        merged["last_run_cache_tokens"] = int(incoming.get("cache_read_tokens") or 0)
        merged["last_run_output_tokens"] = int(incoming.get("output_tokens") or 0)
        merged["last_run_thinking_tokens"] = int(incoming.get("thinking_tokens") or 0)
        if "last_run_llm_calls" in incoming:
            merged["last_run_llm_calls"] = max(1, int(incoming.get("last_run_llm_calls") or 1))

    if "code_handoffs" in prev and isinstance(prev["code_handoffs"], list):
        merged["code_handoffs"] = list(prev["code_handoffs"])
    elif "code_handoffs" in incoming and isinstance(incoming["code_handoffs"], list):
        merged["code_handoffs"] = list(incoming["code_handoffs"])

    if clear_last_error:
        merged.pop("last_error", None)
    if last_error is not None:
        merged["last_error"] = str(last_error)

    return merged


_VALID_REF_CHAT_SCOPES = {"full", "last", "summary"}


def _sanitize_referenced_chat_specs(
    raw_specs: Any,
    fallback_ids: Any = None,
    *,
    exclude_chat_id: str = "",
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    excl = str(exclude_chat_id or "").strip()

    if isinstance(raw_specs, list):
        for item in raw_specs:
            if isinstance(item, dict):
                cid = str(item.get("id") or item.get("chat_id") or "").strip()
                raw_sc = str(item.get("scope") or "full").strip().lower()
                scope = raw_sc if raw_sc in _VALID_REF_CHAT_SCOPES else "full"
            else:
                cid = str(item or "").strip()
                scope = "full"
            if not cid or cid.startswith("desktop:") or cid == excl or cid in seen:
                continue
            seen.add(cid)
            out.append({"id": cid, "scope": scope})
            if len(out) >= 3:
                return out

    if isinstance(fallback_ids, list) and len(out) < 3:
        for item in fallback_ids:
            if isinstance(item, dict):
                cid = str(item.get("id") or item.get("chat_id") or "").strip()
                raw_sc = str(item.get("scope") or "full").strip().lower()
                scope = raw_sc if raw_sc in _VALID_REF_CHAT_SCOPES else "full"
            else:
                cid = str(item or "").strip()
                scope = "full"
            if not cid or cid.startswith("desktop:") or cid == excl or cid in seen:
                continue
            seen.add(cid)
            out.append({"id": cid, "scope": scope})
            if len(out) >= 3:
                break

    return out


def _sanitize_referenced_chat_ids(
    raw_ids: Any,
    *,
    exclude_chat_id: str = "",
) -> list[str]:
    return [
        s["id"]
        for s in _sanitize_referenced_chat_specs(
            None, raw_ids, exclude_chat_id=exclude_chat_id
        )
    ]


def _sanitize_handoff_meta(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    if kind not in ("code_spec", "hq_report", "forwarded_messages"):
        return None

    def _non_neg_int(val: Any) -> Optional[int]:
        if val is None or isinstance(val, bool):
            return None
        try:
            n = int(val)
            return n if n >= 0 else None
        except Exception:
            return None

    src_mid = _non_neg_int(raw.get("source_msg_id"))
    if src_mid is not None and src_mid <= 0:
        src_mid = None

    out: dict[str, Any] = {
        "kind": kind,
        "source_chat_id": str(raw.get("source_chat_id") or "").strip()[:120],
        "source_msg_id": src_mid,
        "source_title": str(raw.get("source_title") or "").strip()[:160],
        "source_icon": str(raw.get("source_icon") or "").strip()[:16],
    }
    if kind == "code_spec":
        out["or_review"] = bool(raw.get("or_review", False))
        out["or_council"] = bool(raw.get("or_council", False))
    elif kind == "forwarded_messages":
        raw_mc = _non_neg_int(raw.get("messages_count"))
        out["messages_count"] = max(1, min(raw_mc if raw_mc is not None and raw_mc > 0 else 1, 50))
        out["user_note"] = (
            str(raw.get("user_note") or "").replace("\r\n", "\n").strip()[:4000]
        )
    else:
        out["model_label"] = str(
            raw.get("model_label") or raw.get("source_model") or ""
        ).strip()[:80]
        out["review_mode"] = "review" if raw.get("review_mode") == "review" else "fast"
        out["user_note"] = (
            str(raw.get("user_note") or "").replace("\r\n", "\n").strip()[:4000]
        )
        for count_key in ("files_count", "commands_count", "tool_steps_count"):
            cnt = _non_neg_int(raw.get(count_key))
            if cnt is not None:
                out[count_key] = min(cnt, 99999)
    if kind == "hq_report":
        out["sources"] = _sanitize_hq_return_sources({"hq_return_sources": raw.get("sources")})
    return out


def _sanitize_hq_return_source(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    src_chat_id = str(raw.get("chat_id") or "").strip()[:120]
    if not src_chat_id:
        return None
    raw_mid = raw.get("message_id")
    if raw_mid is None or isinstance(raw_mid, bool):
        return None
    try:
        mid = int(raw_mid)
    except Exception:
        return None
    if mid <= 0:
        return None
    return {
        "chat_id": src_chat_id,
        "message_id": mid,
        "return_uid": str(raw.get("return_uid") or "").strip()[:64],
        "review_mode": "review" if raw.get("review_mode") == "review" else "fast",
    }


def _sanitize_hq_return_sources(raw_data: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_data, dict):
        return []
    raw = raw_data.get("hq_return_sources")
    if not isinstance(raw, list) or not raw:
        raw = [raw_data.get("hq_return_source")]
    result = []
    seen = set()
    for item in raw:
        src = _sanitize_hq_return_source(item)
        if src and (src["chat_id"], src["message_id"]) not in seen:
            seen.add((src["chat_id"], src["message_id"]))
            result.append(src)
        if len(result) == 4:
            break
    return result


_TURN_PREFERRED_ACCOUNT_KEYS: tuple[str, ...] = (
    "preferred_agy_account_id",
    "preferred_codex_account_id",
    "preferred_claude_account_id",
)


def _sanitize_turn_preferred_accounts(raw_data: Any) -> dict[str, str]:
    if not isinstance(raw_data, dict):
        return {}
    src = (
        raw_data.get("preferred_accounts")
        if isinstance(raw_data.get("preferred_accounts"), dict)
        else raw_data
    )
    out: dict[str, str] = {}
    for key in _TURN_PREFERRED_ACCOUNT_KEYS:
        if key in src and src[key] is not None:
            val = str(src[key]).strip()[:80]
            if not val or re.fullmatch(r"[A-Za-z0-9_.\-]{1,80}", val):
                out[key] = val
    return out


async def _build_hq_return_git_diff_with_status(
    resolved_proj_dir: Path,
    src_msg: dict[str, Any],
    max_chars: int = 14000,
) -> tuple[str, str]:
    """Build a bounded review attachment and explicit diff status (`attached`, `no_paths`, `no_git`, `clean`, `error`)."""
    def build() -> tuple[str, str]:
        import fnmatch
        import tempfile
        root = resolved_proj_dir.resolve()
        paths = set()
        forbidden = ('.git', '.env*', '*oauth-token*', '*.key', '*.pem', '*.pfx', '*.db*', '.secret_key', 'credentials.json', 'auth.json')

        def add(raw):
            if not isinstance(raw, str) or not raw.strip():
                return
            try:
                target = Path(raw.strip().replace('\\', '/'))
                target = (target if target.is_absolute() else root / target).resolve()
                if not _is_within_dir(target, root):
                    return
                rel = target.relative_to(root)
                if any(fnmatch.fnmatch(part.lower(), pat) for part in rel.parts for pat in forbidden):
                    return
                paths.add(rel.as_posix())
            except (ValueError, OSError):
                pass

        def collect(obj):
            if isinstance(obj, str):
                try:
                    collect(json.loads(obj))
                except ValueError:
                    for match in re.finditer(r'^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)$', obj, re.M):
                        add(match[1])
                    for match in re.finditer(r'^\*\*\*\s+Move to:\s*(.+)$', obj, re.M):
                        add(match[1])
            elif isinstance(obj, dict):
                for key, value in obj.items():
                    if key in ('Path', 'TargetFile', 'file_path', 'path', 'notebook_path', 'move_path'):
                        add(value)
                    elif key == 'paths' and isinstance(value, list):
                        for item in value:
                            add(item)
                    elif key == 'renames' and isinstance(value, list):
                        for r_item in value:
                            if isinstance(r_item, dict):
                                add(r_item.get('from'))
                                add(r_item.get('to'))
                    elif isinstance(value, (dict, list, str)):
                        collect(value)
            elif isinstance(obj, list):
                for item in obj:
                    collect(item)

        for tc in src_msg.get('tool_calls') or []:
            if not isinstance(tc, dict):
                continue
            name = str(tc.get('name') or '')
            params = tc.get('parameters') or tc.get('input') or tc.get('args') or {}
            lazy_write = name == 'call_mcp_tool' and isinstance(params, dict) and params.get('ServerName') == 'hub_files' and params.get('ToolName') in ('write_file', 'edit_file')
            if name in ('write_file', 'edit_file', 'apply_patch', 'multi_edit', 'Write', 'Edit', 'MultiEdit', 'fileChange') or ('hub_files' in name and ('write' in name.lower() or 'edit' in name.lower())) or lazy_write:
                collect(params)
        if not paths:
            return '', 'no_paths'

        def git(*args):
            return subprocess.run(
                ['git', '-C', str(root), *args],
                capture_output=True,
                timeout=4.0,
                **config.subprocess_hidden_kwargs(),
            )

        def _read_git_diff_cmd(*diff_args: str) -> tuple[int, str]:
            with tempfile.TemporaryFile() as output:
                res = subprocess.run(
                    ['git', '-C', str(root), *diff_args],
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=4.0,
                    **config.subprocess_hidden_kwargs(),
                )
                output.seek(0)
                txt = output.read(56001).decode('utf-8', errors='replace')
                return res.returncode, txt

        try:
            if git('rev-parse', '--show-toplevel').returncode != 0:
                return '', 'no_git'
            sorted_paths = sorted(paths)
            has_head = git('rev-parse', '--verify', 'HEAD').returncode == 0
            body = ''
            if has_head:
                rc_h, body_h = _read_git_diff_cmd('diff', 'HEAD', '-U3', '--no-color', '--', *sorted_paths)
                if rc_h == 0:
                    body = body_h
                else:
                    _, b_cached = _read_git_diff_cmd('diff', '--cached', '-U3', '--no-color', '--', *sorted_paths)
                    _, b_wt = _read_git_diff_cmd('diff', '-U3', '--no-color', '--', *sorted_paths)
                    body = (b_cached + '\n' + b_wt).strip()
            else:
                _, b_cached = _read_git_diff_cmd('diff', '--cached', '-U3', '--no-color', '--', *sorted_paths)
                body = b_cached

            for rel in sorted_paths:
                if len(body) >= max_chars:
                    break
                if git('ls-files', '--error-unmatch', '--', rel).returncode == 0:
                    continue
                target = (root / rel).resolve()
                if not _is_within_dir(target, root) or not target.is_file() or target.stat().st_size > 32768:
                    continue
                raw = target.read_bytes()
                if b'\0' in raw:
                    continue
                try:
                    content = raw.decode('utf-8')
                except UnicodeError:
                    continue
                body += '\n--- ' + rel + ' (new untracked file)\n' + '\n'.join('+' + ln for ln in content.splitlines()[:120]) + '\n'
            if not body.strip():
                return '', 'clean'
            limit = max(0, min(int(max_chars), 14000))
            prefix = f'<HQ_RETURN_GIT_DIFF files="{len(paths)}">\n'
            suffix = '\n</HQ_RETURN_GIT_DIFF>'
            marker = '\n[... diff обрезан по лимиту 14 КБ ...]'
            available = max(0, limit - len(prefix) - len(suffix))
            if len(body) > available:
                body = body[:max(0, available - len(marker))] + marker
            return (prefix + body + suffix)[:limit], 'attached'
        except (OSError, subprocess.TimeoutExpired):
            return '', 'error'

    return await asyncio.to_thread(build)


async def _build_hq_return_git_diff_block(resolved_proj_dir: Path, src_msg: dict[str, Any], max_chars: int = 14000) -> str:
    """Backward-compatible wrapper returning only the diff block string."""
    blk, _ = await _build_hq_return_git_diff_with_status(resolved_proj_dir, src_msg, max_chars=max_chars)
    return blk


_DEFAULT_BUILD_HQ_DIFF_BLOCK = _build_hq_return_git_diff_block
_CMD_TOOL_NAMES_SET = frozenset(
    {
        "run_command",
        "command",
        "execute_command",
        "bash",
        "shell",
        "shell_command",
        "exec_command",
        "commandexecution",
    }
)


def _count_executed_commands_in_msg(src_msg: Optional[dict[str, Any]]) -> int:
    if not isinstance(src_msg, dict):
        return 0
    cnt = 0
    for tc in src_msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        name = str(tc.get("name") or "").strip()
        low = name.lower()
        params = tc.get("parameters") if isinstance(tc.get("parameters"), dict) else {}
        is_host_svc = (
            low.endswith("__host_service_ctl")
            or low.endswith("_host_service_ctl")
            or (low == "call_mcp_tool" and str(params.get("ServerName") or params.get("server_name") or "").lower() == "hub_host")
        )
        is_sbx_exec = (
            low.endswith("__sandbox_exec")
            or low.endswith("_sandbox_exec")
            or (low == "call_mcp_tool" and str(params.get("ServerName") or params.get("server_name") or "").lower() == "hub_sandbox")
        )
        if low in _CMD_TOOL_NAMES_SET or is_host_svc or is_sbx_exec:
            cnt += 1
    return cnt


def _build_hq_acceptance_directive(
    review_mode: str,
    *,
    commands_count: int,
    diff_status: str = "",
    diff_block: str = "",
) -> str:
    if review_mode == "review":
        if diff_status == "attached" and diff_block:
            return (
                "\n\n🔍 [Режим код-ревью Штаба: проверь корректность и полноту реализации по отчёту, сводке "
                "и приложенному ниже серверному git diff затронутых файлов (вызывай read_file только точечно "
                "при явной нехватке контекста вокруг правок) и зафиксируй итог или замечания.]\n\n"
                + diff_block
            )
        reason_map = {
            "no_git": "нет Git-репозитория в папке проекта",
            "no_paths": "пути правок не зафиксированы в tool_calls",
            "clean": "нет незакоммиченных изменений в git diff",
            "error": "не удалось сформировать git diff",
        }
        reason_txt = reason_map.get(diff_status, "серверный git diff недоступен")
        return (
            f"\n\n🔍 [Режим код-ревью Штаба: серверный git diff не приложен ({reason_txt}) — "
            "сопоставь отчёт и сводку с ТЗ, выполни не более 3–5 точечных вызовов read_file только по самым "
            "рискованным строкам (НЕ перечитывай все затронутые файлы подряд) и сразу зафиксируй итог или замечания.]"
        )
    if commands_count > 0:
        return (
            "\n\n⚡ [Режим быстрой приёмки Штаба: команды и автотесты в рабочей вкладке выполнены без ошибок. "
            "НЕ вызывай read_file и НЕ перечитывай файлы проекта с диска — за один ответ сопоставь этот отчёт "
            "и сводку инструментов с пунктами исходного ТЗ, зафиксируй приёмку и предложи следующий шаг.]"
        )
    return (
        "\n\n⚡ [Режим быстрой приёмки Штаба: терминальные команды и автотесты в рабочей вкладке не запускались, "
        "ошибок инструментов не зафиксировано — сопоставь отчёт и сводку файлов с ТЗ (при необходимости проверки "
        "критичных мест допустимо точечное чтение) и зафиксируй приёмку.]"
    )


def _get_public_queue(chat_id: str) -> list[dict[str, Any]]:
    items = CHAT_TURN_QUEUES.get(chat_id) or []
    return [
        {
            "id": str(it.get("id") or ""),
            "chat_id": str(it.get("chat_id") or chat_id),
            "kind": str(it.get("kind") or "message"),
            "prompt": str(it.get("prompt") or ""),
            "attachments": list(it.get("attachments") or []),
            "referenced_chat_ids": list(it.get("referenced_chat_ids") or []),
            "referenced_chats": list(it.get("referenced_chats") or []),
            "handoff_meta": _sanitize_handoff_meta(it.get("handoff_meta")),
            "preferred_accounts": _sanitize_turn_preferred_accounts(it),
            "model": it.get("model"),
            "access_mode": it.get("access_mode"),
            "author_name": str(it.get("author_name") or ""),
            "created_at": str(it.get("created_at") or ""),
        }
        for it in items
    ]


async def _broadcast_chat_queue(
    chat_id: str,
    chat_obj: Optional[dict[str, Any]] = None,
) -> None:
    chat = chat_obj or await database.get_chat(chat_id)
    if not chat:
        return
    shared_members: Optional[set[str]] = None
    if chat.get("kind") == "project" and chat.get("project_name"):
        sp = await database.get_shared_project_by_name(chat["project_name"])
        if sp:
            shared_members = set(sp.get("member_ids") or [])
    await ws_manager.broadcast_for_chat(
        chat,
        {
            "type": "queue_updated",
            "chat_id": chat_id,
            "queue": _get_public_queue(chat_id),
            "paused": chat_id in CHAT_QUEUE_PAUSED,
        },
        shared_member_ids=shared_members,
    )


async def _broadcast_chat_event(
    chat_id: str,
    chat: dict[str, Any],
    payload: dict[str, Any],
    *,
    shared_member_ids: Optional[set[str]] = None,
) -> None:
    eff_shared = shared_member_ids
    if eff_shared is None and chat.get("kind") == "project" and chat.get("project_name"):
        sp = await database.get_shared_project_by_name(chat["project_name"])
        if sp:
            eff_shared = set(sp.get("member_ids") or [])
    out_payload = dict(payload)
    out_payload.setdefault("chat_id", chat_id)
    await ws_manager.broadcast_for_chat(chat, out_payload, shared_member_ids=eff_shared)


class ConnectionManager:
    """Broadcasts live events across connected devices with strict per-user RBAC scoping."""

    def __init__(self) -> None:
        self.active: dict[WebSocket, dict[str, Any]] = {}

    async def connect(self, ws: WebSocket, user: dict[str, Any]) -> None:
        await ws.accept()
        self.active[ws] = user

    def disconnect(self, ws: WebSocket) -> None:
        self.active.pop(ws, None)

    async def disconnect_user(self, user_id: str) -> None:
        targets = [ws for ws, u in list(self.active.items()) if u.get("id") == user_id]
        for ws in targets:
            self.active.pop(ws, None)
            try:
                await ws.close(code=4401)
            except Exception:
                pass

    async def _send_to_matching(
        self,
        payload: dict[str, Any],
        predicate,
    ) -> None:
        dead: list[WebSocket] = []
        for ws, u in list(self.active.items()):
            if not predicate(u):
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.pop(ws, None)

    async def broadcast_all(self, payload: dict[str, Any]) -> None:
        await self._send_to_matching(payload, lambda _u: True)

    async def broadcast_admins(self, payload: dict[str, Any]) -> None:
        await self._send_to_matching(payload, lambda u: u.get("role") == "admin")

    async def broadcast_user(self, user_id: str, payload: dict[str, Any]) -> None:
        await self._send_to_matching(payload, lambda u: u.get("id") == user_id)

    broadcast_to_user = broadcast_user

    async def broadcast_for_chat(
        self,
        chat: dict[str, Any],
        payload: dict[str, Any],
        shared_member_ids: Optional[set[str]] = None,
    ) -> None:
        owner_id = chat.get("owner_id") or "admin"
        guest_payload = payload
        if isinstance(payload.get("chat"), dict) and "project_path" in payload["chat"]:
            masked_chat = dict(payload["chat"])
            masked_chat.pop("project_path", None)
            guest_payload = {**payload, "chat": masked_chat}

        if chat.get("kind") == "project" and shared_member_ids is not None:
            await self._send_to_matching(payload, lambda u: u.get("role") == "admin")
            await self._send_to_matching(
                guest_payload,
                lambda u: u.get("role") != "admin" and u.get("id") in shared_member_ids,
            )
        else:
            await self._send_to_matching(
                payload,
                lambda u: u.get("role") == "admin" and u.get("id") == owner_id,
            )
            await self._send_to_matching(
                guest_payload,
                lambda u: u.get("role") != "admin" and u.get("id") == owner_id,
            )

    async def broadcast_models_per_user(
        self,
        all_models: list[dict[str, str]],
        tier_summary: str,
    ) -> None:
        user_ids = {
            str(u.get("id"))
            for u in list(self.active.values())
            if u.get("id")
        }
        for uid in user_ids:
            fresh_u = await database.get_user_by_id(uid)
            if not fresh_u or not fresh_u.get("is_active", False):
                continue
            is_adm = fresh_u.get("role") == "admin"
            user_models = await _get_models_for_user(all_models, fresh_u)
            await self.broadcast_user(
                uid,
                {
                    "type": "models_updated",
                    "models": user_models,
                    "tier_summary": (
                        tier_summary
                        if is_adm
                        else "Рабочее пространство разработчика"
                    ),
                },
            )


ws_manager = ConnectionManager()
openrouter_service.set_openrouter_ws_notifier(ws_manager.broadcast_user)


async def _broadcast_gdrive_sync_status(payload: dict[str, Any]) -> None:
    await ws_manager.broadcast_admins(payload)


gdrive_service.set_gdrive_ws_notifier(_broadcast_gdrive_sync_status)


def _notify_gdrive_change_for_user(
    user: Optional[dict[str, Any]],
    reason: str = "",
    *,
    is_shared: bool = False,
) -> None:
    """O(1) non-blocking dirty-flag hook for Google Drive event-driven auto-sync."""
    try:
        uid = str((user or {}).get("id") or "").strip() if isinstance(user, dict) else ""
        is_admin = bool(isinstance(user, dict) and str(user.get("role") or "").lower() == "admin")
        if is_admin and uid:
            gdrive_service.notify_gdrive_change(uid, reason=reason)
        elif is_shared or not uid:
            gdrive_service.notify_gdrive_change("admin", reason=reason)
    except Exception:
        pass


def _is_any_ai_generation_active() -> bool:
    return (
        bool(active_generations)
        or bool(COMPACTING_CHATS)
        or any(not t.done() for t in ACTIVE_TURN_TASKS.values())
    )


async def _gdrive_auto_sync_loop() -> None:
    while True:
        await asyncio.sleep(gdrive_service.AUTO_SYNC_POLL_INTERVAL_SEC)
        try:
            await gdrive_service.run_gdrive_auto_sync_tick(
                has_active_runs_fn=_is_any_ai_generation_active,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def _broadcast_advisor_models_updated() -> None:
    all_models = get_cached_models()
    tier_summary = infer_account_tier(all_models)
    await ws_manager.broadcast_models_per_user(all_models, tier_summary)


model_advisor_service.set_advisor_ws_notifier(_broadcast_advisor_models_updated)


async def _advisor_auto_calibration_loop() -> None:
    await model_advisor_service.run_advisor_auto_calibration_loop(
        has_active_runs_fn=_is_any_ai_generation_active,
    )
openrouter_service.set_openrouter_active_turn_finder(find_active_turn_for_user)

# v137: Lightweight in-memory cross-device read state sync (PC + Smartphone, zero DB schema churn)
_USER_CHAT_READ_TS: dict[str, dict[str, float]] = {}
_USER_READ_ALL_TS: dict[str, float] = {}
_CHAT_LAST_TERMINAL_TS_MS: dict[str, float] = {}
_LAST_READ_SYNC_CLOCK_MS: float = 0.0
_MAX_USER_READ_ENTRIES = 500
_MAX_CHAT_TERMINAL_ENTRIES = 1000


def _next_read_sync_ts_ms(hint_ms: Optional[float] = None) -> int:
    global _LAST_READ_SYNC_CLOCK_MS
    now_ms = time.time() * 1000.0
    cand_ms = now_ms
    if hint_ms is not None:
        try:
            parsed = float(hint_ms)
            if parsed > 0:
                cand_ms = min(max(now_ms, parsed), now_ms + 2000.0)
        except (TypeError, ValueError):
            pass
    _LAST_READ_SYNC_CLOCK_MS = max(cand_ms, _LAST_READ_SYNC_CLOCK_MS + 1.0)
    return int(_LAST_READ_SYNC_CLOCK_MS)


def record_user_chat_read(
    user_id: str,
    chat_id: str,
    read_ts_ms: Optional[float] = None,
) -> int:
    uid = str(user_id or "").strip()
    cid = str(chat_id or "").strip()[:120]
    ts_ms = _next_read_sync_ts_ms(read_ts_ms)
    if not uid or not cid:
        return ts_ms
    bucket = _USER_CHAT_READ_TS.setdefault(uid, {})
    bucket[cid] = max(float(bucket.get(cid, 0.0)), float(ts_ms))
    if len(bucket) > _MAX_USER_READ_ENTRIES:
        sorted_items = sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)[
            :_MAX_USER_READ_ENTRIES
        ]
        _USER_CHAT_READ_TS[uid] = dict(sorted_items)
    return int(_USER_CHAT_READ_TS[uid][cid])


def record_user_all_chats_read(
    user_id: str,
    read_ts_ms: Optional[float] = None,
) -> int:
    uid = str(user_id or "").strip()
    ts_ms = _next_read_sync_ts_ms(read_ts_ms)
    if not uid:
        return ts_ms
    prev = float(_USER_READ_ALL_TS.get(uid, 0.0))
    eff = max(prev, float(ts_ms))
    _USER_READ_ALL_TS[uid] = eff
    return int(eff)


def record_chat_terminal_ts(chat_id: str) -> int:
    cid = str(chat_id or "").strip()[:120]
    ts_ms = _next_read_sync_ts_ms()
    if not cid:
        return ts_ms
    _CHAT_LAST_TERMINAL_TS_MS[cid] = float(ts_ms)
    if len(_CHAT_LAST_TERMINAL_TS_MS) > _MAX_CHAT_TERMINAL_ENTRIES:
        sorted_items = sorted(
            _CHAT_LAST_TERMINAL_TS_MS.items(), key=lambda kv: kv[1], reverse=True
        )[:_MAX_CHAT_TERMINAL_ENTRIES]
        _CHAT_LAST_TERMINAL_TS_MS.clear()
        _CHAT_LAST_TERMINAL_TS_MS.update(sorted_items)
    return ts_ms


def get_user_chat_last_read_ts_ms(user_id: str, chat_id: str) -> Optional[int]:
    uid = str(user_id or "").strip()
    cid = str(chat_id or "").strip()
    if not uid or not cid:
        return None
    per_chat = float((_USER_CHAT_READ_TS.get(uid) or {}).get(cid, 0.0))
    read_all = float(_USER_READ_ALL_TS.get(uid, 0.0))
    best = max(per_chat, read_all)
    return int(best) if best > 0 else None


def purge_deleted_chat_read_ts(chat_id: str) -> None:
    cid = str(chat_id or "").strip()
    if not cid:
        return
    _CHAT_LAST_TERMINAL_TS_MS.pop(cid, None)
    for bucket in _USER_CHAT_READ_TS.values():
        bucket.pop(cid, None)


async def sync_and_broadcast_user_chat_read(
    user_id: str,
    *,
    chat_id: str = "",
    chat_ids: Optional[list[str]] = None,
    read_all: bool = False,
    read_ts_ms: Optional[float] = None,
) -> dict[str, Any]:
    uid = str(user_id or "").strip()
    if not uid:
        return {"chat_id": "", "read_all": bool(read_all), "read_ts_ms": 0}
    if read_all:
        ts_ms = record_user_all_chats_read(uid, read_ts_ms)
        payload: dict[str, Any] = {
            "type": "chat_read_synced",
            "chat_id": "",
            "read_all": True,
            "read_ts_ms": ts_ms,
        }
        await ws_manager.broadcast_to_user(uid, payload)
        return payload

    clean_ids: list[str] = []
    seen: set[str] = set()
    for raw in [chat_id, *(chat_ids or [])]:
        s = str(raw or "").strip()[:120]
        if s and s not in seen:
            seen.add(s)
            clean_ids.append(s)
            if len(clean_ids) >= 100:
                break

    last_ts = _next_read_sync_ts_ms(read_ts_ms)
    for cid in clean_ids:
        last_ts = record_user_chat_read(uid, cid, last_ts)
        await ws_manager.broadcast_to_user(
            uid,
            {
                "type": "chat_read_synced",
                "chat_id": cid,
                "read_all": False,
                "read_ts_ms": last_ts,
            },
        )
    return {
        "type": "chat_read_synced",
        "chat_id": clean_ids[0] if len(clean_ids) == 1 else "",
        "chat_ids": clean_ids,
        "read_all": False,
        "read_ts_ms": last_ts,
    }

_account_sync_lock = asyncio.Lock()
_last_token_sig: tuple[bool, int, int] = (False, 0, 0)
_last_cli_bin_sig: tuple[str, int] = ("", 0)
_SMART_TITLE_REGEN_ACTIVE_USERS: set[str] = set()
_LIVE_OUTLINE_TITLE_TASKS: set[asyncio.Task[Any]] = set()

_ACCESS_RANK = {"read_only": 0, "write_files": 1, "full_access": 2}


def _filter_models_for_user(
    models: list[dict[str, str]],
    user: dict[str, Any],
) -> list[dict[str, str]]:
    if user.get("role") == "admin":
        return models
    allowed = {m.strip() for m in (user.get("allowed_models") or []) if m.strip()}
    if not allowed:
        return models
    if "__no_opus__" in allowed:
        filtered = [
            m
            for m in models
            if "opus" not in str(m.get("id") or "").lower() or m.get("id") in allowed
        ]
        return filtered if filtered else models[:1]
    filtered = [m for m in models if m.get("id") in allowed]
    return filtered if filtered else models[:1]


async def _get_models_for_user(
    all_models: list[dict[str, Any]],
    user: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Combine admin-governed Antigravity CLI models with the user's personal active ChatGPT (`codex:*`) models."""
    if not user:
        base_models = list(all_models)
        model_advisor_service.notify_models_discovered(base_models)
        return await model_advisor_service.enrich_models_with_advisor(base_models)
    base_models = list(_filter_models_for_user(all_models, user))
    uid = str(user.get("id") or "").strip()
    if not uid:
        model_advisor_service.notify_models_discovered(base_models)
        return await model_advisor_service.enrich_models_with_advisor(base_models)
    active_cgpt = await database.get_active_user_chatgpt_account(uid, require_enabled=True)
    seen_ids = {str(m.get("id") or "") for m in base_models if m.get("id")}
    if active_cgpt:
        for cm in codex_engine.get_models_for_chatgpt_account(active_cgpt):
            cm_id = str(cm.get("id") or "")
            if cm_id and cm_id not in seen_ids:
                seen_ids.add(cm_id)
                base_models.append(cm)
    # Personal Claude Code subscription models (`claude-code:*`, vendor `claude_code`)
    try:
        claude_models = await claude_engine.get_claude_models_for_user(uid)
    except Exception:
        claude_models = []
    if claude_models:
        allowed_set = {str(m).strip() for m in (user.get("allowed_models") or []) if str(m).strip()}
        no_opus = user.get("role") != "admin" and "__no_opus__" in allowed_set
        for cm in claude_models:
            cm_id = str(cm.get("id") or "")
            if not cm_id or cm_id in seen_ids:
                continue
            if no_opus and "opus" in cm_id.lower() and cm_id not in allowed_set:
                continue
            seen_ids.add(cm_id)
            base_models.append(cm)
    model_advisor_service.notify_models_discovered(base_models)
    return await model_advisor_service.enrich_models_with_advisor(base_models)


async def _broadcast_claude_state_to_user(user_id: str) -> dict[str, Any]:
    """Push personal `claude_accounts_updated` + `models_updated` events strictly to `user_id`."""
    summary, accounts, fresh_u = await asyncio.gather(
        database.get_claude_sidebar_summary(user_id),
        database.list_user_claude_accounts(user_id),
        database.get_user_by_id(user_id),
    )
    user_models: list[dict[str, Any]] = []
    all_models: list[dict[str, Any]] = []
    if fresh_u and fresh_u.get("is_active", False):
        all_models = get_cached_models()
        user_models = await _get_models_for_user(all_models, fresh_u)
    await ws_manager.broadcast_user(
        user_id,
        {
            "type": "claude_accounts_updated",
            "summary": summary,
            "accounts": accounts,
            "models": [m for m in user_models if is_claude_code_model(str(m.get("id") or ""))],
        },
    )
    if fresh_u and fresh_u.get("is_active", False):
        await ws_manager.broadcast_user(
            user_id,
            {
                "type": "models_updated",
                "models": user_models,
                "tier_summary": (
                    infer_account_tier(all_models)
                    if fresh_u.get("role") == "admin"
                    else "Рабочее пространство разработчика"
                ),
            },
        )
    return summary


async def _broadcast_chatgpt_state_to_user(user_id: str) -> dict[str, Any]:
    """Push personal `chatgpt_accounts_updated` + `models_updated` events strictly to `user_id`."""
    summary, accounts, fresh_u = await asyncio.gather(
        database.get_chatgpt_sidebar_summary(user_id),
        database.list_user_chatgpt_accounts(user_id),
        database.get_user_by_id(user_id),
    )
    await ws_manager.broadcast_user(
        user_id,
        {
            "type": "chatgpt_accounts_updated",
            "summary": summary,
            "accounts": accounts,
        },
    )
    if fresh_u and fresh_u.get("is_active", False):
        all_models = get_cached_models()
        user_models = await _get_models_for_user(all_models, fresh_u)
        await ws_manager.broadcast_user(
            user_id,
            {
                "type": "models_updated",
                "models": user_models,
                "tier_summary": (
                    infer_account_tier(all_models)
                    if fresh_u.get("role") == "admin"
                    else "Рабочее пространство разработчика"
                ),
            },
        )
    return summary


def _max_rank_for_user(user: dict[str, Any]) -> int:
    if user.get("role") == "admin":
        return 2
    max_m = str(user.get("max_access_mode") or "write_files")
    if max_m in ("docker_full", "host_full"):
        return 2
    if max_m == "write_files":
        return 1
    return 0


def _cap_access_mode_for_user(
    requested_mode: Optional[str],
    kind: str,
    user: dict[str, Any],
) -> str:
    if kind != "project":
        return "read_only"
    req = requested_mode if requested_mode in _ACCESS_RANK else "read_only"
    max_r = _max_rank_for_user(user)
    req_r = _ACCESS_RANK[req]
    if req_r <= max_r:
        return req
    for name, rnk in _ACCESS_RANK.items():
        if rnk == max_r:
            return name
    return "read_only"


async def _get_chat_access_context(
    chat: dict[str, Any],
    user: dict[str, Any],
    *,
    allow_admin_override: bool = True,
) -> tuple[bool, Optional[dict[str, Any]]]:
    """Return (is_allowed, shared_project_or_None)."""
    if chat.get("kind") == "project" and chat.get("project_name"):
        sp = await database.get_shared_project_by_name(chat["project_name"])
        if sp:
            allowed = (
                user.get("role") == "admin"
                or user.get("id") in (sp.get("member_ids") or [])
            )
            return allowed, sp

    if chat.get("owner_id", "admin") == user.get("id"):
        return True, None
    if allow_admin_override and user.get("role") == "admin":
        return True, None
    return False, None


async def _enforce_user_turn_quotas(user: dict[str, Any], model_id: str) -> Optional[str]:
    """Return Russian error reason if non-admin user hits model restriction, daily limit, or reserve."""
    if is_codex_model(model_id):
        active_cgpt = await database.get_active_user_chatgpt_account(
            str(user.get("id") or "admin"),
            require_enabled=True,
        )
        if not active_cgpt:
            return (
                "Личный аккаунт ChatGPT не подключён или поставлен на паузу. "
                "Откройте карточку ChatGPT в левой панели, чтобы войти или включить аккаунт."
            )
        return None

    if is_claude_code_model(model_id):
        uid_c = str(user.get("id") or "admin")
        if not await database.get_active_claude_account(uid_c, require_enabled=True):
            return (
                "Личный аккаунт Claude не подключён или поставлен на паузу. "
                "Откройте карточку Claude в левой панели, чтобы войти или включить аккаунт."
            )
        if user.get("role") == "admin":
            return None
        allowed_c = {m.strip() for m in (user.get("allowed_models") or []) if m.strip()}
        if "__no_opus__" in allowed_c and "opus" in model_id.lower() and model_id not in allowed_c:
            return (
                f"Модель `{model_id}` ограничена администратором для вашей учётной записи. "
                "Выберите Claude Sonnet или Haiku."
            )
        # Personal Anthropic subscription: owner's Antigravity quota reserve is NOT applied
        return None

    if user.get("role") == "admin":
        return None

    allowed_set = {m.strip() for m in (user.get("allowed_models") or []) if m.strip()}
    if allowed_set:
        if "__no_opus__" in allowed_set:
            is_allowed = ("opus" not in (model_id or "").lower()) or (model_id in allowed_set)
        else:
            is_allowed = model_id in allowed_set
        if not is_allowed:
            return (
                f"Модель `{model_id}` ограничена администратором для вашей учётной записи. "
                "Выберите доступную модель в шапке чата."
            )

    daily_limit = int(user.get("daily_token_limit") or 0)
    if daily_limit > 0:
        used_24h = await database.get_user_24h_tokens(user["id"])
        if used_24h >= daily_limit:
            return (
                f"Достигнут суточный лимит токенов для вашего аккаунта "
                f"({used_24h:,} из {daily_limit:,} за 24ч). Попробуйте позже или обратитесь к администратору."
            ).replace(",", " ")

    reserve_pct = int(user.get("quota_reserve_pct") or 0)
    if reserve_pct > 0:
        quotas = await fetch_account_quotas(force_refresh=False)
        if quotas and quotas.get("ok"):
            is_claude_gpt = classify_model_family(model_id) in ("claude", "gpt")
            rem_pct = (
                quotas.get("claude_5h_pct")
                if is_claude_gpt
                else quotas.get("gemini_5h_pct")
            )
            family_label = "Claude / GPT" if is_claude_gpt else "Gemini"
            if rem_pct is not None and float(rem_pct) <= float(reserve_pct):
                return (
                    f"Сработал защитный резерв квоты администратора по линейке {family_label}: "
                    f"осталось {rem_pct}% (резерв владельца {reserve_pct}%). "
                    "Переключитесь на другую линейку моделей или дождитесь обновления 5ч окна."
                )
    return None


def _token_file_signature() -> tuple[bool, int, int]:
    """Inspect ONLY filesystem stat metadata (never file contents) to detect CLI account changes."""
    tok_path = config.AGY_CLI_DATA_DIR / "antigravity-oauth-token"
    try:
        st = tok_path.stat()
        return (True, st.st_mtime_ns, st.st_size)
    except OSError:
        return (False, 0, 0)


def _cli_bin_signature() -> tuple[str, int]:
    """Inspect agy.exe path & mtime_ns to detect automatic CLI binary updates."""
    agy_bin = config.resolve_agy_bin()
    try:
        st = Path(agy_bin).stat()
        return (agy_bin, int(st.st_mtime_ns))
    except OSError:
        return (agy_bin, 0)


def _snapshot_cli_auth_mtime() -> tuple[tuple[bool, int, int], str, int]:
    tok_sig = _token_file_signature()
    bin_path, bin_mtime_ns = _cli_bin_signature()
    return (tok_sig, bin_path, bin_mtime_ns)


def _is_auth_missing_error(detail: str) -> bool:
    low = (detail or "").lower()
    return any(
        kw in low
        for kw in (
            "login",
            "logged",
            "auth",
            "credential",
            "token",
            "sign in",
            "signin",
            "401",
            "unauthorized",
            "unauthenticated",
        )
    )


def build_turn_error_extra_hint(err_txt: str, *, is_admin: bool = False) -> str:
    """Return localized Russian diagnostic hint for known upstream CLI errors."""
    if is_gemini_location_unsupported_error(err_txt):
        return (
            "\n\n💡 *Регион текущего IP-маршрута не поддерживается Google Gemini API (код 400). "
            "Проверьте в настройках маршрута/прокси, что для Google AI выбран рабочий зарубежный узел "
            "(США/Европа), а не прямой выход или узел РФ.*"
        )
    if _is_auth_missing_error(err_txt):
        if is_admin:
            return (
                "\n\n💡 *Подсказка: откройте «Настройки» (⚙️) → «Аккаунт Antigravity» "
                "и нажмите «Обновить сессию», чтобы войти через браузер без команд в терминале.*"
            )
        return ""
    if is_transient_capacity_error(err_txt):
        return (
            "\n\n💡 *Серверы Google для выбранной модели сейчас перегружены (код 503 даже после авто-повтора). "
            "Попробуйте через минуту или переключите модель в селекторе вверху чата.*"
        )
    if is_transient_stream_error(err_txt):
        return (
            "\n\n💡 *Сетевой поток оборвался по таймауту даже после авто-продолжения. "
            "Если контекст вкладки превысил ~70%, сожмите его или отправьте задачу в новую чистую вкладку.*"
        )
    return ""


_UNSET_WIN_PROXY = object()


def _launch_cli_interactive_window(proxy_url: Any = _UNSET_WIN_PROXY) -> bool:
    """Launch official agy CLI in a host console window so browser OAuth can run without terminal commands."""
    agy_bin = config.resolve_agy_bin()
    if not Path(agy_bin).exists():
        return False
    env = os.environ.copy()
    try:
        eff_proxy = config.resolve_outbound_proxy() if proxy_url is _UNSET_WIN_PROXY else proxy_url
    except proxy_router.ProxyRouteError:
        return False
    proxy_router.apply_proxy_to_subprocess_env(env, eff_proxy)
    try:
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        # Manual terminal is an intentional visible user action.
        if os.name == "nt":
            # Preserve the account/proxy environment for the explicit terminal.
            from .windows_desktop import spawn_visible_terminal
            spawn_visible_terminal([agy_bin], cwd=config.CHAT_SANDBOX_DIR, env=env)
            return True
        subprocess.Popen(
            [agy_bin],
            cwd=str(config.CHAT_SANDBOX_DIR),
            env=env,
            creationflags=flags,
        )
        return True
    except Exception:
        return False


def _session_fingerprint() -> str:
    """Compute a short non-secret fingerprint of token file stat metadata."""
    exists, mtime_ns, size_b = _token_file_signature()
    if not exists:
        return ""
    raw = f"{size_b}:{mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


_TURN_QUOTA_REFRESH_MIN_INTERVAL_S: float = 90.0
_QUOTA_ERROR_REFRESH_MIN_INTERVAL_S: float = 45.0
_MANUAL_QUOTA_REFRESH_MIN_INTERVAL_S: float = 20.0
_last_turn_quota_refresh_mono: float = -1000.0
_last_manual_quota_refresh_mono: dict[str, float] = {}
_TELEMETRY_BG_TASKS: set[asyncio.Task[Any]] = set()


def _spawn_telemetry_task(coro: Any) -> asyncio.Task[Any]:
    t = asyncio.create_task(coro)
    _TELEMETRY_BG_TASKS.add(t)
    t.add_done_callback(_TELEMETRY_BG_TASKS.discard)
    return t


async def _refresh_and_broadcast_quotas(
    trigger_user_id: Optional[str] = None,
    *,
    force_on_quota_error: bool = False,
    max_age_sec: Optional[float] = None,
) -> None:
    global _last_turn_quota_refresh_mono
    try:
        now_mono = time.monotonic()
        elapsed = now_mono - _last_turn_quota_refresh_mono
        if max_age_sec is not None:
            if not is_agy_quota_cache_stale(max_age_sec) or elapsed < _QUOTA_ERROR_REFRESH_MIN_INTERVAL_S:
                return
            force = True
        else:
            min_interval = (
                _QUOTA_ERROR_REFRESH_MIN_INTERVAL_S
                if force_on_quota_error
                else _TURN_QUOTA_REFRESH_MIN_INTERVAL_S
            )
            force = elapsed >= min_interval or (
                is_agy_quota_cache_stale(300.0) and elapsed >= _QUOTA_ERROR_REFRESH_MIN_INTERVAL_S
            )

        if force:
            _last_turn_quota_refresh_mono = now_mono

        quotas, admin_stats = await asyncio.gather(
            fetch_account_quotas(force_refresh=force),
            database.get_token_usage_stats(include_by_user=True),
        )
        if isinstance(quotas, dict) and quotas.get("deferred"):
            return
        await ws_manager.broadcast_admins(
            {
                "type": "quotas_updated",
                "account_profiles": await database.list_account_profiles(),
                "agy_summary": await database.get_agy_accounts_summary(),
                "quotas": quotas,
                "token_stats": admin_stats,
            }
        )

        target_guest_uids: set[str] = set()
        if trigger_user_id and trigger_user_id != "admin":
            target_guest_uids.add(str(trigger_user_id))
        if force:
            for u_conn in list(ws_manager.active.values()):
                uid_conn = str(u_conn.get("id") or "").strip()
                if uid_conn and u_conn.get("role") != "admin":
                    target_guest_uids.add(uid_conn)

        for guest_uid in target_guest_uids:
            user_stats = await database.get_token_usage_stats(filter_user_id=guest_uid)
            await ws_manager.broadcast_user(
                guest_uid,
                {
                    "type": "quotas_updated",
                    "quotas": quotas,
                    "token_stats": user_stats,
                },
            )
    except Exception:
        pass


def schedule_stale_telemetry_revalidation(
    user_id: str,
    *,
    max_age_sec: float = 300.0,
) -> None:
    """Non-blocking Stale-While-Revalidate coordinator for Google AI, ChatGPT, Claude Code, and OpenRouter."""
    clean_uid = (user_id or "").strip()
    if not clean_uid:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    if is_agy_quota_cache_stale(max_age_sec):
        t_agy = loop.create_task(
            _refresh_and_broadcast_quotas(clean_uid, max_age_sec=max_age_sec)
        )
        _TELEMETRY_BG_TASKS.add(t_agy)
        t_agy.add_done_callback(_TELEMETRY_BG_TASKS.discard)

    codex_engine.schedule_chatgpt_live_refresh(
        clean_uid,
        max_age_sec=max_age_sec,
        on_updated=_broadcast_chatgpt_state_to_user,
    )

    claude_engine.schedule_claude_live_refresh(
        clean_uid,
        max_age_sec=max_age_sec,
        on_updated=_broadcast_claude_state_to_user,
    )

    openrouter_service.schedule_openrouter_live_refresh(
        clean_uid,
        max_age_sec=max_age_sec,
    )


async def _perform_account_sync(
    *,
    auto: bool,
    open_login: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    global _last_token_sig, _last_cli_bin_sig
    async with _account_sync_lock:
        ok, models, detail = await probe_cli_auth_and_refresh_models()
        _last_token_sig = _token_file_signature()
        _last_cli_bin_sig = _cli_bin_signature()

        launched_login = False
        if open_login or (not auto and not ok and _is_auth_missing_error(detail)):
            try:
                agy_proxy = await proxy_router.ensure_proxy_for_context("agy")
                launched_login = _launch_cli_interactive_window(proxy_url=agy_proxy)
            except proxy_router.ProxyRouteError:
                launched_login = False

        quotas, active_profile, all_profiles, cli_info = await asyncio.gather(
            fetch_account_quotas(force_refresh=True),
            database.get_active_account_profile(),
            database.list_account_profiles(),
            get_cli_version_info(force_refresh=(reason == "cli_binary_updated")),
        )

        cli_auth = get_cli_auth_status()
        tier_summary = infer_account_tier(models)
        admin_user_obj = await database.get_user_by_id("admin")
        admin_models = await _get_models_for_user(models, admin_user_obj)
        await ws_manager.broadcast_admins(
            {
                "type": "account_synced",
                "models": admin_models,
                "auto": auto,
                "ok": ok,
                "reason": reason,
                "cli_auth": cli_auth,
                "cli_info": cli_info,
                "quotas": quotas,
                "account_profile": active_profile,
                "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
            }
        )
        await ws_manager.broadcast_models_per_user(models, tier_summary)
        return {
            "ok": ok,
            "models": admin_models,
            "launched_login": launched_login,
            "reason": reason,
            "cli_auth": cli_auth,
            "cli_info": cli_info,
            "quotas": quotas,
            "account_profile": active_profile,
            "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
            "voice": voice.get_voice_status(),
        }


async def _watch_account_sync_loop() -> None:
    """Automatically detect Desktop Antigravity / CLI account switches & binary updates with zero manual intervention."""
    global _last_token_sig, _last_cli_bin_sig
    _last_token_sig = _token_file_signature()
    _last_cli_bin_sig = _cli_bin_signature()
    unauth_ticks = 0
    last_periodic_models_check = time.monotonic()

    while True:
        await asyncio.sleep(5.0)
        sig = _token_file_signature()
        bin_sig = _cli_bin_signature()

        if bin_sig != _last_cli_bin_sig:
            await asyncio.sleep(0.6)
            _last_cli_bin_sig = _cli_bin_signature()
            _last_token_sig = _token_file_signature()
            unauth_ticks = 0
            last_periodic_models_check = time.monotonic()
            try:
                await get_cli_version_info(force_refresh=True)
                await _perform_account_sync(
                    auto=True,
                    open_login=False,
                    reason="cli_binary_updated",
                )
            except Exception:
                pass
            continue

        if sig != _last_token_sig:
            await asyncio.sleep(0.6)
            _last_token_sig = _token_file_signature()
            unauth_ticks = 0
            last_periodic_models_check = time.monotonic()
            try:
                await _perform_account_sync(
                    auto=True,
                    open_login=False,
                    reason="token_changed",
                )
            except Exception:
                pass
            continue

        now_mono = time.monotonic()
        if (now_mono - last_periodic_models_check) >= 1800.0:
            last_periodic_models_check = now_mono
            try:
                prev_ids = [m.get("id") for m in get_cached_models()]
                new_models = await fetch_available_models(force_refresh=True)
                new_ids = [m.get("id") for m in new_models]
                if new_ids != prev_ids:
                    tier = infer_account_tier(new_models)
                    admin_user_obj = await database.get_user_by_id("admin")
                    admin_models = await _get_models_for_user(new_models, admin_user_obj) if admin_user_obj else new_models
                    await ws_manager.broadcast_models_per_user(new_models, tier)
                    await ws_manager.broadcast_admins(
                        {
                            "type": "account_synced",
                            "models": admin_models,
                            "auto": True,
                            "ok": get_cli_auth_status().get("ok", True),
                            "reason": "periodic_models_refresh",
                            "cli_auth": get_cli_auth_status(),
                            "quotas": get_cached_quotas(),
                        }
                    )
            except Exception:
                pass

        if not get_cli_auth_status().get("ok", True):
            unauth_ticks += 1
            if unauth_ticks >= 2:
                unauth_ticks = 0
                try:
                    ok, models, _ = await probe_cli_auth_and_refresh_models()
                    _last_token_sig = _token_file_signature()
                    if ok:
                        quotas = await fetch_account_quotas(force_refresh=True)
                        tier = infer_account_tier(models)
                        admin_user_obj = await database.get_user_by_id("admin")
                        admin_models = await _get_models_for_user(models, admin_user_obj) if admin_user_obj else models
                        await ws_manager.broadcast_admins(
                            {
                                "type": "account_synced",
                                "models": admin_models,
                                "auto": True,
                                "ok": True,
                                "cli_auth": get_cli_auth_status(),
                                "quotas": quotas,
                            }
                        )
                        await ws_manager.broadcast_models_per_user(models, tier)
                except Exception:
                    pass
        else:
            unauth_ticks = 0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await database.init_db()
    await proxy_router.initialize_from_db()
    await database.cleanup_stale_streaming_messages()
    host_service_manager.HOST_SERVICE_MANAGER.add_change_listener(
        _broadcast_project_host_services_update
    )
    host_service_manager.HOST_SERVICE_MANAGER.add_transition_listener(
        _broadcast_host_process_transition
    )
    sandbox_service.set_sandbox_processes_notifier(_broadcast_sandbox_processes_update)
    await openrouter_service.sync_all_users_openrouter_cli_files()
    await sandbox_service.sync_all_users_sandbox_cli_files()
    await host_service_manager.sync_all_users_host_cli_files()
    sandbox_service.start_sandbox_reaper()
    host_service_manager.start_host_services_watchdog()
    from .storage_maintenance import cleanup_stale_large_prompt_files
    asyncio.create_task(asyncio.to_thread(cleanup_stale_large_prompt_files))
    asyncio.create_task(fetch_available_models())
    asyncio.create_task(fetch_account_quotas())
    asyncio.create_task(check_docker_status())
    asyncio.create_task(voice.preload_default_model())
    tunnel.start_tunnel_supervisor()
    watch_task = asyncio.create_task(_watch_account_sync_loop())
    gdrive_sync_task: Optional[asyncio.Task] = None
    advisor_cal_task: Optional[asyncio.Task] = None
    if not (os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules):
        gdrive_sync_task = asyncio.create_task(_gdrive_auto_sync_loop())
        advisor_cal_task = asyncio.create_task(_advisor_auto_calibration_loop())
    try:
        yield
    finally:
        if advisor_cal_task is not None:
            advisor_cal_task.cancel()
            try:
                await advisor_cal_task
            except asyncio.CancelledError:
                pass
        if gdrive_sync_task is not None:
            gdrive_sync_task.cancel()
            try:
                await gdrive_sync_task
            except asyncio.CancelledError:
                pass
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        await host_service_manager.stop_host_services_watchdog()
        await sandbox_service.stop_sandbox_reaper()
        await tunnel.stop_tunnel_supervisor()
        await proxy_router.shutdown_all_tunnels()
        await database.close_db_pool()


app = FastAPI(
    title="Antigravity Hub",
    version=config.get_app_version(),
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


class _ProjectsListCacheInvalidationMiddleware:
    """v110: чистый ASGI-middleware — любая мутация `/api/projects*` или общих проектов
    сбрасывает 3-секундный TTL-кэш `brain_reader.list_projects_for_user` ДО отправки ответа."""

    _PREFIXES = ("/api/projects", "/api/admin/shared-projects", "/api/chats")

    def __init__(self, asgi_app: Any) -> None:
        self.app = asgi_app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") in ("GET", "HEAD", "OPTIONS")
            or not str(scope.get("path") or "").startswith(self._PREFIXES)
        ):
            await self.app(scope, receive, send)
            return

        async def _send(message: Any) -> None:
            if message.get("type") == "http.response.start":
                try:
                    brain_reader.invalidate_projects_list_cache()
                except Exception:
                    pass
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            try:
                brain_reader.invalidate_projects_list_cache()
            except Exception:
                pass


app.add_middleware(_ProjectsListCacheInvalidationMiddleware)


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = "admin"
    password: str = ""
    pin: str = ""


class ChangePinRequest(BaseModel):
    current_pin: str
    new_pin: str


class CreateChatRequest(BaseModel):
    kind: str = "chat"
    project_name: str = ""
    title: str = "Новый диалог"
    model: str = "gemini-4-carbon-high"
    access_mode: str = "read_only"
    icon: str = ""
    folder: str = ""
    preferred_agy_account_id: str = ""
    preferred_codex_account_id: str = ""
    preferred_claude_account_id: str = ""


class UpdateChatRequest(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None
    access_mode: Optional[str] = None
    pinned: Optional[bool] = None
    icon: Optional[str] = None
    folder: Optional[str] = None
    sort_order: Optional[int] = None
    kind: Optional[str] = None
    project_name: Optional[str] = None
    is_archived: Optional[bool] = None
    preferred_agy_account_id: Optional[str] = None
    preferred_codex_account_id: Optional[str] = None
    preferred_claude_account_id: Optional[str] = None


class ReorderChatsRequest(BaseModel):
    chat_ids: list[str]


class MarkChatsReadRequest(BaseModel):
    chat_id: Optional[str] = None
    chat_ids: Optional[list[str]] = None
    read_all: bool = False
    read_ts_ms: Optional[float] = None


class BookmarkMessageRequest(BaseModel):
    bookmarked: bool


class UpdateMessageTitleRequest(BaseModel):
    custom_title: str = ""


class RecordMessageHandoffRequest(BaseModel):
    target_chat_id: str
    target_title: str = ""
    target_icon: str = ""
    access_mode: str = "full_access"
    project_name: str = ""
    sent_at: str = ""


class RecordMessageHqReturnRequest(BaseModel):
    target_chat_id: str
    target_title: str = "Штаб"
    target_icon: str = "🏠"
    project_name: str = ""
    target_msg_id: Optional[int] = None
    sent_at: str = ""
    return_uid: str = ""
    increment_count: bool = True


class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""
    is_shared: bool = False
    execution_mode: str = "docker"
    member_ids: list[str] = Field(default_factory=list)
    storage_location: Optional[str] = None
    upgrade_members_docker: bool = False


class AdminCreateUserRequest(BaseModel):
    username: str
    display_name: str = ""
    password: str
    max_access_mode: str = "write_files"
    sandbox_network: str = "internet"
    allowed_models: list[str] = Field(default_factory=list)
    daily_token_limit: int = 0
    quota_reserve_pct: int = 15


class AdminUpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    max_access_mode: Optional[str] = None
    sandbox_network: Optional[str] = None
    allowed_models: Optional[list[str]] = None
    daily_token_limit: Optional[int] = None
    quota_reserve_pct: Optional[int] = None


class AdminCreateSharedProjectRequest(BaseModel):
    name: str
    description: str = ""
    execution_mode: str = "docker"
    member_ids: list[str] = Field(default_factory=list)
    storage_location: Optional[str] = None
    upgrade_members_docker: bool = False


class AdminUpdateSharedProjectRequest(BaseModel):
    description: Optional[str] = None
    execution_mode: Optional[str] = None
    member_ids: Optional[list[str]] = None
    storage_location: Optional[str] = None
    upgrade_members_docker: bool = False


class UpdateAdminProxyRequest(BaseModel):
    mode: str = "system_auto"
    proxy_spec: Optional[str] = None


class ProbeProxyRouteRequest(BaseModel):
    mode: Optional[str] = None
    proxy_spec: Optional[str] = None
    provider: Optional[str] = None
    account_id: Optional[str] = None


class SaveAccountProfileRequest(BaseModel):
    id: Optional[str] = None
    profile_id: Optional[str] = None
    email: Optional[str] = None
    label: Optional[str] = None
    plan_note: Optional[str] = None
    make_active: Optional[bool] = True
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None


class ExportIdeaRequest(BaseModel):
    project_name: str
    target_file: str = "PLAN.md"
    idea_title: str = ""
    content: str
    append: bool = True


class ImportDesktopChatRequest(BaseModel):
    project_name: str = ""
    title: str = ""
    model: str = "gemini-4-carbon-high"
    force_copy: bool = False


class SyncAccountRequest(BaseModel):
    open_login: bool = False


class UpdateAccountRulesRequest(BaseModel):
    custom_rules: str = ""


class UpdateAdvisorPrefsRequest(BaseModel):
    enabled: Optional[bool] = None
    strategy: Optional[str] = None
    auto_pick_new_tab: Optional[bool] = None
    composer_hints: Optional[bool] = None


class UpdateOpenRouterConfigRequest(BaseModel):
    api_key: Optional[str] = None
    is_enabled: Optional[bool] = None
    default_model: Optional[str] = None
    custom_rules: Optional[str] = None
    max_price_per_m: Optional[float] = None
    daily_spend_limit_usd: Optional[float] = None
    smart_titles_enabled: Optional[bool] = None
    smart_titles_model: Optional[str] = Field(default=None, max_length=160)


class RegenerateOutlineTitlesRequest(BaseModel):
    rebuild_existing_ai: bool = False


class StartChatGptDeviceAuthRequest(BaseModel):
    label: str = ""
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None


class ImportChatGptAuthRequest(BaseModel):
    auth_json: str
    label: str = ""
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None


class PatchChatGptAccountRequest(BaseModel):
    label: Optional[str] = None
    is_enabled: Optional[bool] = None
    auto_failover: Optional[bool] = None
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None


class StartClaudeAuthRequest(BaseModel):
    label: str = ""
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None


class SubmitClaudeCodeRequest(BaseModel):
    code: str


class ImportClaudeCredentialsRequest(BaseModel):
    payload: str
    label: str = ""
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None


class PatchClaudeAccountRequest(BaseModel):
    label: Optional[str] = None
    is_enabled: Optional[bool] = None
    auto_failover: Optional[bool] = None
    proxy_mode: Optional[str] = None
    proxy_spec: Optional[str] = None


class VoiceFormatRequest(BaseModel):
    text: str
    language: str = "ru"
    is_partial: bool = False
    native_punct: bool = False
    project_name: str = ""


class LiveTtsRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    rate: Optional[str] = "+15%"


class SaveProjectMemoryRequest(BaseModel):
    passport_overview: Optional[str] = None
    passport_rules: Optional[str] = None
    passport_file_map: Optional[str] = None
    changelog: Optional[list[dict[str, Any]]] = None
    disable_default_guardrails: Optional[bool] = None


class GenerateProjectMemoryRequest(BaseModel):
    preferred_model: str = ""


class PinProjectMemoryNoteRequest(BaseModel):
    note: str
    target: str = "changelog"
    chat_id: str = ""
    chat_title: str = ""


class ReorderProjectsRequest(BaseModel):
    project_names: list[str] = Field(default_factory=list)


class UpdateProjectSettingsRequest(BaseModel):
    extra_dirs: list[str] = Field(default_factory=list)
    disable_default_guardrails: Optional[bool] = None


class ProjectEnvEntryPayload(BaseModel):
    key: str
    value: str = ""
    comment: Optional[str] = None


class UpdateProjectEnvRequest(BaseModel):
    file: str = ".env"
    mode: Literal["entries", "raw"] = "entries"
    entries: list[ProjectEnvEntryPayload] = Field(default_factory=list)
    raw_content: Optional[str] = None
    ensure_gitignore: bool = True


class SyncProjectEnvExampleRequest(BaseModel):
    file: str = ".env"
    direction: Literal["import_missing", "export_template"]


class GitCheckpointRequest(BaseModel):
    message: str = ""
    paths: Optional[list[str]] = None
    run_verify: bool = True


class GitRollbackRequest(BaseModel):
    mode: str
    target_sha: str = ""


class UpdateGDriveConfigRequest(BaseModel):
    oauth_client_json: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    clear_oauth_client: bool = False
    root_folder_name: Optional[str] = None
    is_enabled: Optional[bool] = None
    sync_self_hub: Optional[bool] = None
    excluded_projects: Optional[list[str]] = None
    include_env_vault: Optional[bool] = None
    vault_passphrase: Optional[str] = None
    clear_vault_passphrase: bool = False


class GDriveOAuthStartRequest(BaseModel):
    redirect_uri: Optional[str] = None


class GDriveOAuthExchangeRequest(BaseModel):
    callback_url: Optional[str] = None
    code: Optional[str] = None
    state: Optional[str] = None


class GDriveDisconnectRequest(BaseModel):
    clear_index: bool = False
    clear_root_folder: bool = False


class GDriveRestoreExecuteRequest(BaseModel):
    restore_dialogs: bool = False
    restore_code: bool = False
    selected_projects: list[str] = Field(default_factory=list)
    restore_env_vault: bool = False
    vault_passphrase: str = ""
    overwrite_existing: bool = False


class HostServiceActionRequest(BaseModel):
    command: str = ""
    workdir: str = "."
    port: Optional[int] = None
    allow_gpu: bool = False
    replace: bool = False
    startup_wait_ms: int = 1500


class ExecuteDesktopMigrationRequest(BaseModel):
    selected_projects: Optional[list[str]] = None
    import_chats: bool = True
    merge_missing_on_conflict: bool = False
    rebuild_venv: bool = False
    move_legacy_siblings: bool = False


class ImportProjectFolderRequest(BaseModel):
    source_path: str
    only_missing_on_non_empty: bool = True
    import_chats: bool = True
    rebuild_venv: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FWD_BLOCK_TITLE_RE = re.compile(
    r"<FORWARDED_MESSAGES\b[^>]*>([\s\S]*?)</FORWARDED_MESSAGES>",
    re.IGNORECASE,
)
_FWD_AUTO_FRAME_TITLE_RE = re.compile(
    r"^(?:Ознакомься\s+с\s+пересланными\s+сообщениями\s+и\s+продолжи\s+задачу\s*/\s*дай\s+ответ\s*:|Пользователь\s+переслал\s+(?:сообщения|сообщение|контекст)\b[^\n]*|Прикреплённые\s+сообщения\s+из\s+диалога[^\n]*)\s*$",
    re.IGNORECASE,
)
_FWD_ITEM_HEADER_TITLE_RE = re.compile(
    r"^\s*\[Сообщение\s+\d+\s*·[^\]]*\]\s*:?\s*$",
    re.IGNORECASE,
)


def _make_auto_title(prompt: str) -> str:
    raw = (prompt or "").strip()
    fwd_blocks = database.find_forwarded_messages_blocks(raw)
    if fwd_blocks:
        first_blk = fwd_blocks[0]
        last_blk = fwd_blocks[-1]
        before_fwd = raw[: first_blk["start"]].strip()
        after_fwd = raw[last_blk["end"] :].strip()
        user_note_cand = (
            before_fwd
            if (before_fwd and not _FWD_AUTO_FRAME_TITLE_RE.match(before_fwd))
            else after_fwd
        )
        if user_note_cand:
            first_note_ln = next(
                (ln.strip() for ln in user_note_cand.splitlines() if ln.strip()),
                user_note_cand,
            )
            raw = first_note_ln
        else:
            inner_raw = first_blk["inner"] or ""
            in_fence = False
            chosen_line = ""
            for ln in inner_raw.splitlines():
                s = ln.strip()
                if s.startswith("```") or s.startswith("~~~"):
                    in_fence = not in_fence
                    continue
                if in_fence or not s or s == "---":
                    continue
                if _FWD_ITEM_HEADER_TITLE_RE.match(s):
                    continue
                cand = re.sub(r"^[#*>\-\s`]+", "", s).strip()
                cand = re.sub(r"(\*\*|__|\*|_|~~|`)", "", cand).strip()
                if cand:
                    chosen_line = cand
                    break
            raw = chosen_line or "Пересланные сообщения"

    clean = re.sub(r"\s+", " ", raw.strip())
    clean = re.sub(r"^[#*>\-\s]+", "", clean)
    if not clean:
        return "Новый диалог"
    if len(clean) <= 54:
        return clean
    cut = clean[:54]
    if " " in cut[30:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(".,;:!?-") + "…"


def _update_env_pin(new_pin: str) -> None:
    env_file = config.ENV_PATH
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    replaced = False
    out: list[str] = []
    for line in lines:
        if line.strip().startswith("HUB_PIN="):
            out.append(f"HUB_PIN={new_pin}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"HUB_PIN={new_pin}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Auth Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/auth/status")
async def auth_status(request: Request):
    tok = auth.extract_request_token(request)
    user = await auth.resolve_token_user(tok)
    return {"authenticated": bool(user), "user": user}


@app.get("/api/auth/me")
async def auth_me(current_user: dict[str, Any] = Depends(auth.get_current_user)):
    return {
        "ok": True,
        "user": current_user,
        "advisor_prefs": database.normalize_advisor_prefs(current_user.get("advisor_prefs")),
    }


@app.post("/api/auth/login")
async def auth_login(body: LoginRequest, request: Request, response: Response):
    client_ip = auth.get_request_client_ip(request)
    secret = body.password if body.password else body.pin
    uname = (body.username or "admin").strip() or "admin"

    user = await auth.authenticate_credentials(uname, secret, client_ip)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Неверный логин или пароль / ПИН-код",
        )

    token = auth.create_user_session_token(user["id"], user.get("session_version", 1))
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        max_age=auth.TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=auth.is_request_https(request),
    )
    return {"ok": True, "token": token, "user": user}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.post("/api/auth/change-pin")
async def auth_change_pin(
    body: ChangePinRequest,
    request: Request,
    response: Response,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    client_ip = auth.get_request_client_ip(request)
    verified = await auth.authenticate_credentials(
        current_user["username"], body.current_pin, client_ip
    )
    if not verified:
        raise HTTPException(status_code=401, detail="Текущий пароль / ПИН-код указан неверно")

    clean_new = body.new_pin.strip()
    if len(clean_new) < 4:
        raise HTTPException(
            status_code=400,
            detail="Новый пароль / ПИН-код должен содержать минимум 4 символа",
        )

    updated_user = await database.update_user(current_user["id"], password=clean_new)
    if not updated_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if current_user["id"] == "admin":
        _update_env_pin(clean_new)

    await openrouter_service.sync_user_openrouter_cli_files(updated_user["id"])
    await sandbox_service.sync_user_sandbox_cli_files_by_id(updated_user["id"])
    await host_service_manager.sync_user_host_cli_files_by_id(updated_user["id"])

    new_token = auth.create_user_session_token(
        updated_user["id"], updated_user.get("session_version", 1)
    )
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=new_token,
        max_age=auth.TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=auth.is_request_https(request),
    )
    return {"ok": True, "token": new_token, "user": updated_user}


# ---------------------------------------------------------------------------
# System, Models, Quotas & QR Code
# ---------------------------------------------------------------------------


@app.get("/api/system/info")
async def system_info(current_user: dict[str, Any] = Depends(auth.get_current_user)):
    uid = str(current_user["id"])
    is_admin = current_user.get("role") == "admin"
    all_models = get_cached_models()
    if getattr(sys.modules.get("app.agy_engine"), "_cached_models", None) is None:
        async def _warm_models_bg() -> None:
            try:
                if Path(config.resolve_agy_bin()).exists():
                    live_m = await fetch_available_models(force_refresh=False)
                    await ws_manager.broadcast_models_per_user(live_m, infer_account_tier(live_m))
            except Exception:
                pass
        t_m = asyncio.create_task(_warm_models_bg())
        _TELEMETRY_BG_TASKS.add(t_m)
        t_m.add_done_callback(_TELEMETRY_BG_TASKS.discard)

    docker_status, openrouter_summary, chatgpt_summary = await asyncio.gather(
        check_docker_status(),
        database.get_openrouter_sidebar_summary(uid),
        database.get_chatgpt_sidebar_summary(uid),
    )
    try:
        claude_summary = await database.get_claude_sidebar_summary(uid)
    except Exception:
        claude_summary = {"has_account": False}

    schedule_stale_telemetry_revalidation(uid, max_age_sec=300.0)

    models = await _get_models_for_user(all_models, current_user)
    net = config.get_network_endpoints()
    net["tunnel"] = tunnel.get_tunnel_status()
    fast_quotas = get_cached_account_quotas_fast()
    proxy_status = await asyncio.to_thread(
        proxy_router.get_proxy_status_payload, is_admin=is_admin
    )

    if is_admin:
        active_profile, all_profiles, users_list, cli_info = await asyncio.gather(
            database.get_active_account_profile(),
            database.list_account_profiles(),
            database.list_users(),
            get_cli_version_info(),
        )
        return {
            "version": config.get_app_version(),
            "platform": config.PLATFORM_LABEL,
            "user": current_user,
            "models": models,
            "all_models": all_models,
            "cli_auth": get_cli_auth_status(),
            "cli_info": cli_info,
            "tier_summary": infer_account_tier(all_models),
            "account_profile": active_profile,
            "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
            "quotas": fast_quotas,
            "openrouter_summary": openrouter_summary,
            "chatgpt_summary": chatgpt_summary,
            "claude_summary": claude_summary,
            "docker_status": docker_status,
            "users_count": len(users_list),
            "network": net,
            "proxy": str(proxy_status.get("effective_label") or "direct"),
            "proxy_settings": proxy_status,
            "base_dir": str(config.BASE_DIR),
            "users_dir": str(config.USERS_DIR),
            "data_dir": str(config.DATA_DIR),
            "projects_root": str(config.PROJECTS_ROOT),
            "shared_projects_dir": str(config.SHARED_PROJECTS_DIR),
            "workspace_label": f"users/{config.sanitize_user_folder_slug(current_user)}/projects",
            "agy_bin": config.resolve_agy_bin(),
            "voice": voice.get_voice_status(),
        }

    # Sandboxed non-admin view (never leaks admin Google email, host paths, or agy_bin)
    return {
        "version": config.get_app_version(),
        "platform": config.PLATFORM_LABEL,
        "user": current_user,
        "models": models,
        "cli_auth": {"ok": get_cli_auth_status().get("ok", True), "detail": ""},
        "tier_summary": "Рабочее пространство разработчика",
        "account_profile": None,
        "account_profiles": [],
        "quotas": fast_quotas,
        "openrouter_summary": openrouter_summary,
        "chatgpt_summary": chatgpt_summary,
        "claude_summary": claude_summary,
        "docker_status": docker_status,
        "network": net,
        "proxy_settings": proxy_status,
        "workspace_label": f"users/{config.sanitize_user_folder_slug(current_user)}/projects",
        "voice": voice.get_voice_status(),
    }


@app.post("/api/system/sync-account")
async def api_sync_account(
    body: Optional[SyncAccountRequest] = None,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    open_login = bool(body.open_login) if body else False
    return await _perform_account_sync(auto=False, open_login=open_login)


@app.post("/api/system/update-cli")
async def api_update_cli(_admin: dict[str, Any] = Depends(auth.require_admin)):
    has_active_turn = (
        bool(active_generations)
        or bool(COMPACTING_CHATS)
        or any(not t.done() for t in ACTIVE_TURN_TASKS.values())
    )
    if has_active_turn:
        raise HTTPException(
            status_code=409,
            detail="Сейчас выполняется ответ ИИ. Дождитесь завершения генерации перед обновлением движка CLI.",
        )

    update_res = await run_cli_self_update()
    sync_res = await _perform_account_sync(
        auto=False,
        open_login=False,
        reason="cli_self_update",
    )
    models = sync_res.get("models") or update_res.get("models") or []
    return {
        **update_res,
        "models": models,
        "models_count": len(models),
        "cli_auth": sync_res.get("cli_auth"),
        "cli_info": update_res.get("cli_info") or sync_res.get("cli_info"),
        "quotas": sync_res.get("quotas"),
        "tier_summary": infer_account_tier(models),
    }


@app.get("/api/quotas")
async def api_get_quotas(
    refresh: bool = Query(False),
    chat_id: Optional[str] = Query(None),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    global _last_turn_quota_refresh_mono
    uid = str(current_user["id"])
    is_admin = current_user.get("role") == "admin"

    # Fast Stale-While-Revalidate path (`refresh=false`): 0 blocking CLI spawns
    if not refresh:
        fast_quotas = get_cached_account_quotas_fast()
        all_models = get_cached_models()
        schedule_stale_telemetry_revalidation(uid, max_age_sec=300.0)

        if is_admin:
            (
                token_stats,
                active_profile,
                all_profiles,
                openrouter_summary,
                chatgpt_summary,
                claude_summary,
                user_models,
            ) = await asyncio.gather(
                database.get_token_usage_stats(active_chat_id=chat_id, include_by_user=True),
                database.get_active_account_profile(),
                database.list_account_profiles(),
                database.get_openrouter_sidebar_summary(uid),
                database.get_chatgpt_sidebar_summary(uid),
                database.get_claude_sidebar_summary(uid),
                _get_models_for_user(all_models, current_user),
            )
            if (
                fast_quotas is None
                and active_profile
                and isinstance(active_profile.get("quotas"), dict)
                and active_profile["quotas"].get("ok") is True
            ):
                fast_quotas = active_profile["quotas"]
            return {
                "quotas": fast_quotas,
                "token_stats": token_stats,
                "models": user_models,
                "account_profile": active_profile,
                "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
                "cli_auth": get_cli_auth_status(),
                "tier_summary": infer_account_tier(all_models),
                "openrouter_summary": openrouter_summary,
                "chatgpt_summary": chatgpt_summary,
                "claude_summary": claude_summary,
            }

        (
            token_stats,
            openrouter_summary,
            chatgpt_summary,
            claude_summary,
            user_models,
        ) = await asyncio.gather(
            database.get_token_usage_stats(active_chat_id=chat_id, filter_user_id=uid),
            database.get_openrouter_sidebar_summary(uid),
            database.get_chatgpt_sidebar_summary(uid),
            database.get_claude_sidebar_summary(uid),
            _get_models_for_user(all_models, current_user),
        )
        return {
            "quotas": fast_quotas,
            "token_stats": token_stats,
            "models": user_models,
            "account_profile": None,
            "account_profiles": [],
            "cli_auth": {"ok": get_cli_auth_status().get("ok", True), "detail": ""},
            "tier_summary": "Ваша квота и личный расход токенов",
            "openrouter_summary": openrouter_summary,
            "chatgpt_summary": chatgpt_summary,
            "claude_summary": claude_summary,
        }

    # Explicit manual `refresh=true`: synchronous forced refresh with >= 20s Anti-Ban cooldown
    now_mono = time.monotonic()
    last_manual = _last_manual_quota_refresh_mono.get(uid, -1000.0)
    allow_force_live = (now_mono - last_manual) >= _MANUAL_QUOTA_REFRESH_MIN_INTERVAL_S
    if allow_force_live:
        _last_manual_quota_refresh_mono[uid] = now_mono

    async def _maybe_refresh_active_chatgpt() -> None:
        if not allow_force_live or not config.resolve_codex_bin():
            return
        act_c = await database.get_active_user_chatgpt_account(uid, require_enabled=True)
        if act_c and act_c.get("id"):
            try:
                acc_key = f"{uid}:{act_c['id']}"
                codex_engine._CHATGPT_LAST_PROBE_MONO[acc_key] = time.monotonic()
                await codex_engine.fetch_chatgpt_account_quotas(uid, str(act_c["id"]))
            except Exception:
                pass

    async def _maybe_refresh_active_claude() -> None:
        if not allow_force_live:
            return
        try:
            act_cl = await database.get_active_claude_account(uid, require_enabled=True)
            if act_cl and act_cl.get("id"):
                cl_key = f"{uid}:{act_cl['id']}"
                claude_engine._CLAUDE_LAST_PROBE_MONO[cl_key] = time.monotonic()
            await asyncio.wait_for(claude_engine.refresh_active_claude_quotas(uid), timeout=15.0)
        except Exception:
            pass

    if is_admin:
        async def _refresh_agy_quotas_and_models() -> tuple[dict[str, Any], list[dict[str, str]]]:
            if allow_force_live:
                q_live = await agy_engine.refresh_all_agy_accounts_quotas(
                    force_refresh=True,
                    fetch_fn=fetch_account_quotas,
                )
                m_live = await fetch_available_models(force_refresh=True)
                return q_live, m_live
            return await asyncio.gather(
                fetch_account_quotas(False),
                fetch_available_models(force_refresh=False),
            )

        gather_tasks: list[Any] = [
            _refresh_agy_quotas_and_models(),
            database.get_token_usage_stats(active_chat_id=chat_id, include_by_user=True),
            database.get_active_account_profile(),
            database.list_account_profiles(),
            _maybe_refresh_active_chatgpt(),
            _maybe_refresh_active_claude(),
        ]
        if allow_force_live:
            _last_turn_quota_refresh_mono = now_mono
            gather_tasks.append(
                openrouter_service.refresh_user_openrouter_live_status(uid, force=True)
            )
        gathered = await asyncio.gather(*gather_tasks)
        (quotas, all_models), token_stats, active_profile, all_profiles = gathered[:4]
        active_profile = await database.get_active_account_profile()
        all_profiles = await database.list_account_profiles()
        openrouter_summary, chatgpt_summary, claude_summary, user_models = await asyncio.gather(
            database.get_openrouter_sidebar_summary(uid),
            database.get_chatgpt_sidebar_summary(uid),
            database.get_claude_sidebar_summary(uid),
            _get_models_for_user(all_models, current_user),
        )
        tier_summary = infer_account_tier(all_models)
        if allow_force_live:
            await ws_manager.broadcast_models_per_user(all_models, tier_summary)
            await ws_manager.broadcast_admins(
                {
                    "type": "quotas_updated",
                    "quotas": quotas,
                    "token_stats": token_stats,
                }
            )
            await _broadcast_chatgpt_state_to_user(uid)
            await _broadcast_claude_state_to_user(uid)
        return {
            "quotas": quotas,
            "token_stats": token_stats,
            "models": user_models,
            "account_profile": active_profile,
            "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
            "cli_auth": get_cli_auth_status(),
            "tier_summary": tier_summary,
            "openrouter_summary": openrouter_summary,
            "chatgpt_summary": chatgpt_summary,
            "claude_summary": claude_summary,
        }

    user_tasks: list[Any] = [
        fetch_account_quotas(force_refresh=False),
        database.get_token_usage_stats(active_chat_id=chat_id, filter_user_id=uid),
        fetch_available_models(force_refresh=False),
        _maybe_refresh_active_chatgpt(),
        _maybe_refresh_active_claude(),
    ]
    if allow_force_live:
        user_tasks.append(
            openrouter_service.refresh_user_openrouter_live_status(uid, force=True)
        )
    user_gathered = await asyncio.gather(*user_tasks)
    quotas, token_stats, all_models = user_gathered[:3]
    openrouter_summary, chatgpt_summary, claude_summary, user_models = await asyncio.gather(
        database.get_openrouter_sidebar_summary(uid),
        database.get_chatgpt_sidebar_summary(uid),
        database.get_claude_sidebar_summary(uid),
        _get_models_for_user(all_models, current_user),
    )
    if allow_force_live:
        await _broadcast_chatgpt_state_to_user(uid)
        await _broadcast_claude_state_to_user(uid)
    return {
        "quotas": quotas,
        "token_stats": token_stats,
        "models": user_models,
        "account_profile": None,
        "account_profiles": [],
        "cli_auth": {"ok": get_cli_auth_status().get("ok", True), "detail": ""},
        "tier_summary": "Ваша квота и личный расход токенов",
        "openrouter_summary": openrouter_summary,
        "chatgpt_summary": chatgpt_summary,
        "claude_summary": claude_summary,
    }


async def _validate_account_proxy_input(
    *,
    proxy_mode: Optional[str],
    proxy_spec: Optional[str],
    is_admin: bool,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if proxy_mode is None and proxy_spec is None:
        return (None, None, None)

    if proxy_mode is not None:
        pm = proxy_mode.strip().lower()
        if pm not in ("global", "custom"):
            raise HTTPException(
                status_code=400,
                detail="Недопустимый режим маршрута аккаунта (разрешены 'global' или 'custom')",
            )
    else:
        pm = "custom" if (proxy_spec and proxy_spec.strip()) else None

    if pm == "global":
        return ("global", "", "")

    clean_spec = (proxy_spec or "").strip() if proxy_spec is not None else ""
    if clean_spec:
        try:
            parsed = proxy_router.parse_proxy_spec(clean_spec)
            await asyncio.to_thread(
                proxy_router.validate_proxy_target_security,
                parsed.host,
                parsed.port,
                is_admin=is_admin,
                resolve_dns=True,
            )
        except proxy_router.ProxyConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return ("custom", clean_spec, parsed.safe_label)

    return (pm, None, None)


async def _refresh_agy_after_route_change() -> None:
    try:
        if not Path(config.resolve_agy_bin()).exists():
            return
        quotas = await fetch_account_quotas(force_refresh=True)
        all_models, admin_stats = await asyncio.gather(
            fetch_available_models(force_refresh=True),
            database.get_token_usage_stats(include_by_user=True),
        )
        tier = infer_account_tier(all_models)
        await ws_manager.broadcast_models_per_user(all_models, tier)
        await ws_manager.broadcast_admins(
            {
                "type": "quotas_updated",
                "account_profiles": await database.list_account_profiles(),
                "agy_summary": await database.get_agy_accounts_summary(),
                "quotas": quotas,
                "token_stats": admin_stats,
            }
        )
    except Exception:
        pass


@app.get("/api/proxy/status")
async def api_get_proxy_status(
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    return await asyncio.to_thread(
        proxy_router.get_proxy_status_payload, is_admin=is_admin
    )


@app.put("/api/admin/proxy")
async def api_put_admin_proxy(
    body: UpdateAdminProxyRequest,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    mode = (body.mode or "").strip().lower()
    if mode not in ("system_auto", "custom", "direct"):
        raise HTTPException(
            status_code=400,
            detail="Недопустимый режим глобального прокси (разрешены 'system_auto', 'custom', 'direct')",
        )

    clean_spec = (body.proxy_spec or "").strip() if body.proxy_spec is not None else ""
    safe_label: Optional[str] = None
    requires_xray = False

    if mode == "custom" and clean_spec:
        try:
            parsed = proxy_router.parse_proxy_spec(clean_spec)
            await asyncio.to_thread(
                proxy_router.validate_proxy_target_security,
                parsed.host,
                parsed.port,
                is_admin=True,
                resolve_dns=True,
            )
            safe_label = parsed.safe_label
            requires_xray = parsed.requires_xray
        except proxy_router.ProxyConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    try:
        await database.save_global_proxy_settings(
            mode=mode,
            proxy_spec=clean_spec if clean_spec else None,
            proxy_label=safe_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await proxy_router.reload_routes_from_db()
    invalidate_agy_route_caches()
    status_payload = await asyncio.to_thread(
        proxy_router.get_proxy_status_payload, is_admin=True
    )
    await ws_manager.broadcast_admins(
        {
            "type": "proxy_settings_updated",
            "proxy_settings": status_payload,
        }
    )
    _spawn_telemetry_task(_refresh_agy_after_route_change())
    return {
        "ok": True,
        "proxy_settings": status_payload,
        "xray_required": bool(requires_xray and not status_payload.get("xray", {}).get("available")),
    }


@app.post("/api/admin/xray/install")
async def api_admin_install_xray(
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    try:
        xray_info = await proxy_router.install_or_update_xray_core()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    await proxy_router.reload_routes_from_db()
    invalidate_agy_route_caches()
    status_payload = await asyncio.to_thread(
        proxy_router.get_proxy_status_payload, is_admin=True
    )
    await ws_manager.broadcast_admins(
        {
            "type": "proxy_settings_updated",
            "proxy_settings": status_payload,
        }
    )
    return {
        "ok": True,
        "xray": xray_info,
        "proxy_settings": status_payload,
    }


@app.post("/api/proxy/test")
async def api_test_proxy_route(
    body: Optional[ProbeProxyRouteRequest] = None,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    req = body or ProbeProxyRouteRequest()
    try:
        return await proxy_router.probe_proxy_route(
            current_user=current_user,
            mode=req.mode,
            proxy_spec=req.proxy_spec,
            provider=req.provider,
            account_id=req.account_id,
        )
    except proxy_router.ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


from . import agy_engine
from .agy_account_api import router as agy_account_router
app.include_router(agy_account_router)


@app.get("/api/account/profiles")
async def api_list_account_profiles(_admin: dict[str, Any] = Depends(auth.require_admin)):
    active_profile, all_profiles = await asyncio.gather(
        database.get_active_account_profile(),
        database.list_account_profiles(),
    )
    return {"account_profile": active_profile, "account_profiles": all_profiles}


@app.post("/api/account/profiles")
async def api_save_account_profile(
    body: SaveAccountProfileRequest,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    target_pid = (body.id or body.profile_id or "").strip() or None
    clean_email = body.email.strip() if body.email is not None else None
    clean_label = body.label.strip() if body.label is not None else None
    clean_plan = body.plan_note.strip() if body.plan_note is not None else None

    if not target_pid and not (clean_email or clean_label):
        raise HTTPException(status_code=400, detail="Укажите почту Google или название учётки")

    p_mode, p_spec, p_lbl = await _validate_account_proxy_input(
        proxy_mode=body.proxy_mode,
        proxy_spec=body.proxy_spec,
        is_admin=True,
    )

    try:
        saved = await database.save_account_profile(
            profile_id=target_pid,
            email=clean_email,
            label=clean_label,
            plan_note=clean_plan,
            fingerprint=_session_fingerprint() if not target_pid else None,
            make_active=body.make_active,
            proxy_mode=p_mode,
            proxy_spec=p_spec,
            proxy_label=p_lbl,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await proxy_router.reload_routes_from_db()
    invalidate_agy_route_caches()
    active_profile = await database.get_active_account_profile()
    all_profiles = await database.list_account_profiles()
    status_payload = await asyncio.to_thread(
        proxy_router.get_proxy_status_payload, is_admin=True
    )
    await ws_manager.broadcast_admins(
        {
            "type": "account_profile_updated",
            "account_profile": active_profile,
            "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
            "proxy_settings": status_payload,
        }
    )
    if saved.get("is_active"):
        _spawn_telemetry_task(_refresh_agy_after_route_change())
    return {
        "ok": True,
        "saved": saved,
        "account_profile": active_profile,
        "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
        "proxy_settings": status_payload,
    }


@app.post("/api/account/profiles/{profile_id}/activate")
async def api_activate_account_profile(
    profile_id: str,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    activated = await database.activate_account_profile(
        profile_id, fingerprint=_session_fingerprint()
    )
    if not activated:
        raise HTTPException(status_code=404, detail="Профиль аккаунта не найден")
    await proxy_router.reload_routes_from_db()
    invalidate_agy_route_caches()
    all_profiles = await database.list_account_profiles()
    status_payload = await asyncio.to_thread(
        proxy_router.get_proxy_status_payload, is_admin=True
    )
    await ws_manager.broadcast_admins(
        {
            "type": "account_profile_updated",
            "account_profile": activated,
            "quotas": activated.get('quotas') or {},
            "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
            "proxy_settings": status_payload,
        }
    )
    _spawn_telemetry_task(_refresh_agy_after_route_change())
    return {
        "ok": True,
        "account_profile": activated,
        "quotas": activated.get('quotas') or {},
        "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
        "proxy_settings": status_payload,
    }


@app.delete("/api/account/profiles/{profile_id}")
async def api_delete_account_profile(
    profile_id: str,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    ok = await database.delete_account_profile(profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Профиль аккаунта не найден")
    await proxy_router.reload_routes_from_db()
    invalidate_agy_route_caches()
    active_profile = await database.get_active_account_profile()
    all_profiles = await database.list_account_profiles()
    status_payload = await asyncio.to_thread(
        proxy_router.get_proxy_status_payload, is_admin=True
    )
    await ws_manager.broadcast_admins(
        {
            "type": "account_profile_updated",
            "account_profile": active_profile,
            "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
            "proxy_settings": status_payload,
        }
    )
    return {
        "ok": True,
        "account_profile": active_profile,
        "account_profiles": all_profiles,
                "agy_summary": await database.get_agy_accounts_summary(),
        "proxy_settings": status_payload,
    }


@app.get("/api/account/rules")
async def api_get_account_rules(
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    rules_text = str(current_user.get("custom_rules") or "")
    return {
        "custom_rules": rules_text,
        "bytes_used": len(rules_text.encode("utf-8")),
        "max_bytes": config.MAX_CUSTOM_RULES_BYTES,
    }


@app.put("/api/account/rules")
async def api_update_account_rules(
    body: UpdateAccountRulesRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    try:
        updated_user = await database.update_user_rules(
            current_user["id"],
            custom_rules=body.custom_rules or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not updated_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    rules_text = str(updated_user.get("custom_rules") or "")
    await asyncio.to_thread(config.sync_user_cli_rules, updated_user, rules_text)

    await ws_manager.broadcast_user(
        current_user["id"],
        {
            "type": "user_rules_updated",
            "custom_rules": rules_text,
            "bytes_used": len(rules_text.encode("utf-8")),
            "max_bytes": config.MAX_CUSTOM_RULES_BYTES,
        },
    )
    return {
        "ok": True,
        "custom_rules": rules_text,
        "bytes_used": len(rules_text.encode("utf-8")),
        "max_bytes": config.MAX_CUSTOM_RULES_BYTES,
    }


@app.get("/api/account/advisor-prefs")
async def api_get_advisor_prefs(
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    return {
        "ok": True,
        "advisor_prefs": database.normalize_advisor_prefs(current_user.get("advisor_prefs")),
    }


@app.put("/api/account/advisor-prefs")
async def api_update_advisor_prefs(
    body: UpdateAdvisorPrefsRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    patch: dict[str, Any] = {}
    if body.enabled is not None:
        patch["enabled"] = body.enabled
    if body.strategy is not None:
        patch["strategy"] = body.strategy
    if body.auto_pick_new_tab is not None:
        patch["auto_pick_new_tab"] = body.auto_pick_new_tab
    if body.composer_hints is not None:
        patch["composer_hints"] = body.composer_hints

    try:
        saved = await database.update_user_advisor_prefs(current_user["id"], patch)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await ws_manager.broadcast_user(
        current_user["id"],
        {
            "type": "advisor_prefs_updated",
            "advisor_prefs": saved,
        },
    )
    return {"ok": True, "advisor_prefs": saved}


# ---------------------------------------------------------------------------
# Personal OpenRouter Bridge, Rules, Telemetry & In-Process MCP Gateway (v59)
# ---------------------------------------------------------------------------


@app.api_route("/mcp/openrouter/{mcp_token}", methods=["POST", "GET", "DELETE"])
async def mcp_openrouter_gateway(request: Request, mcp_token: str):
    return await openrouter_service.handle_mcp_openrouter_http(
        request,
        mcp_token,
        chat_id=str(request.query_params.get("chat_id") or ""),
    )


@app.api_route(
    "/mcp/sandbox/{mcp_token}",
    methods=["POST", "GET", "DELETE"],
    include_in_schema=False,
)
async def mcp_sandbox_gateway(request: Request, mcp_token: str):
    return await sandbox_service.handle_mcp_sandbox_http(request, mcp_token)


@app.api_route(
    "/mcp/files/{mcp_token}",
    methods=["POST", "GET", "DELETE"],
    include_in_schema=False,
)
async def mcp_files_gateway(request: Request, mcp_token: str):
    return await workspace_mcp.handle_mcp_files_http(request, mcp_token)


@app.api_route(
    "/mcp/host/{mcp_token}",
    methods=["POST", "GET", "DELETE"],
    include_in_schema=False,
)
async def mcp_host_gateway(request: Request, mcp_token: str):
    return await host_service_manager.handle_mcp_host_http(request, mcp_token)


def _compact_smart_titles_eligibility(raw_cands: dict[str, Any]) -> dict[str, int]:
    return {
        "eligible_count": int(raw_cands.get("eligible_count") or 0),
        "existing_ai_count": int(raw_cands.get("existing_ai_count") or 0),
        "manual_custom_count": int(raw_cands.get("manual_custom_count") or 0),
    }


async def _build_full_openrouter_state_payload(
    user_id: str,
    *,
    refresh_live: bool = False,
) -> dict[str, Any]:
    uid = str(user_id or "").strip()
    cfg = await database.get_user_openrouter_config(uid)
    live_error = ""

    need_live = False
    if cfg.get("has_key"):
        if bool(refresh_live) or not cfg.get("cached_at"):
            need_live = True
        else:
            try:
                cached_dt = datetime.fromisoformat(str(cfg["cached_at"]))
                age_sec = (datetime.now(timezone.utc) - cached_dt).total_seconds()
                if age_sec >= 120.0:
                    need_live = True
            except Exception:
                need_live = True

    if need_live:
        bundle = await database.get_user_openrouter_secret_bundle(uid)
        if bundle and bundle.get("api_key"):
            try:
                live_status = await openrouter_service.fetch_openrouter_key_and_credits(
                    bundle["api_key"]
                )
                cfg = await database.update_user_openrouter_cached_status(uid, live_status)
            except Exception as exc:
                live_error = str(exc)

    spend_stats, models_summary, logs, summary, raw_cands = await asyncio.gather(
        database.get_openrouter_spend_stats(uid),
        database.get_openrouter_models_summary(uid),
        database.list_openrouter_logs(uid, limit=60),
        database.get_openrouter_sidebar_summary(uid),
        database.list_user_outline_title_candidates(uid, include_existing_ai=False),
    )

    if need_live and not live_error:
        await ws_manager.broadcast_user(
            uid,
            {"type": "openrouter_updated", "summary": summary},
        )

    return {
        "config": cfg,
        "status": cfg.get("cached_status") or {},
        "spend_stats": spend_stats,
        "models_summary": models_summary,
        "logs": logs,
        "summary": summary,
        "max_rules_bytes": config.MAX_OPENROUTER_RULES_BYTES,
        "live_error": live_error,
        "smart_titles_eligibility": _compact_smart_titles_eligibility(raw_cands),
        "smart_title_presets": openrouter_service.SMART_TITLE_MODEL_PRESETS,
        "smart_titles_regen_running": bool(uid in _SMART_TITLE_REGEN_ACTIVE_USERS),
    }


@app.get("/api/openrouter/state")
async def api_get_openrouter_state(
    refresh: int = Query(0),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    return await _build_full_openrouter_state_payload(
        current_user["id"],
        refresh_live=bool(refresh),
    )


@app.put("/api/openrouter/config")
async def api_put_openrouter_config(
    body: UpdateOpenRouterConfigRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    new_key: Optional[str] = None
    live_status: Optional[dict[str, Any]] = None

    if body.api_key is not None and body.api_key.strip():
        new_key = body.api_key.strip()
        try:
            live_status = await openrouter_service.fetch_openrouter_key_and_credits(new_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Не удалось проверить ключ через OpenRouter API: {exc}",
            )

    try:
        cfg = await database.upsert_user_openrouter_config(
            uid,
            new_api_key=new_key,
            is_enabled=body.is_enabled,
            default_model=body.default_model,
            custom_rules=body.custom_rules,
            max_price_per_m=body.max_price_per_m,
            daily_spend_limit_usd=body.daily_spend_limit_usd,
            smart_titles_enabled=body.smart_titles_enabled,
            smart_titles_model=body.smart_titles_model,
            cached_status=live_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await openrouter_service.sync_user_openrouter_cli_files(uid)
    spend_stats, summary, raw_cands = await asyncio.gather(
        database.get_openrouter_spend_stats(uid),
        database.get_openrouter_sidebar_summary(uid),
        database.list_user_outline_title_candidates(uid, include_existing_ai=False),
    )
    await ws_manager.broadcast_user(
        uid,
        {"type": "openrouter_updated", "summary": summary},
    )
    return {
        "ok": True,
        "config": cfg,
        "status": cfg.get("cached_status") or {},
        "spend_stats": spend_stats,
        "summary": summary,
        "max_rules_bytes": config.MAX_OPENROUTER_RULES_BYTES,
        "smart_titles_eligibility": _compact_smart_titles_eligibility(raw_cands),
        "smart_title_presets": openrouter_service.SMART_TITLE_MODEL_PRESETS,
        "smart_titles_regen_running": bool(uid in _SMART_TITLE_REGEN_ACTIVE_USERS),
    }


@app.post("/api/openrouter/regenerate-titles")
async def api_regenerate_openrouter_outline_titles(
    req: RegenerateOutlineTitlesRequest = RegenerateOutlineTitlesRequest(),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    if uid in _SMART_TITLE_REGEN_ACTIVE_USERS:
        raise HTTPException(
            status_code=409,
            detail="Перегенерация заголовков уже выполняется",
        )

    bundle = await database.get_user_openrouter_secret_bundle(uid)
    if not bundle or not bundle.get("has_key") or not bundle.get("is_enabled"):
        raise HTTPException(
            status_code=400,
            detail="Подключите и активируйте персональный ключ OpenRouter для перегенерации заголовков",
        )
    if not bundle.get("smart_titles_enabled"):
        raise HTTPException(
            status_code=400,
            detail="Включите переключатель умных заголовков «Содержания» перед запуском перегенерации",
        )

    _SMART_TITLE_REGEN_ACTIVE_USERS.add(uid)
    updated_total = 0
    chunks_count = 0
    run_cost_usd = 0.0
    stopped_reason = ""
    try:
        cand_data = await database.list_user_outline_title_candidates(
            uid,
            include_existing_ai=bool(req.rebuild_existing_ai),
        )
        items = list(cand_data.get("items") or [])
        total_candidates = len(items)
        processed_so_far = 0
        batch_sz = openrouter_service.OUTLINE_TITLE_BATCH_SIZE

        chat_cache: dict[str, Optional[dict[str, Any]]] = {}
        sp_members_cache: dict[str, Optional[set[str]]] = {}

        for start_idx in range(0, total_candidates, batch_sz):
            chunk = items[start_idx : start_idx + batch_sz]
            chunk_meta: dict[str, Any] = {}
            titles_by_id = await openrouter_service.generate_outline_titles_batch_chunk(
                uid,
                chunk,
                bundle=bundle,
                meta_out=chunk_meta,
            )
            chunks_count += 1
            run_cost_usd += max(0.0, float(chunk_meta.get("cost_usd") or 0.0))

            bulk_updates: list[tuple[str, int, str]] = []
            for it in chunk:
                mid = int(it["id"])
                if mid in titles_by_id:
                    bulk_updates.append((str(it["chat_id"]), mid, titles_by_id[mid]))

            applied = (
                await database.set_messages_auto_titles_bulk(bulk_updates)
                if bulk_updates
                else []
            )
            processed_so_far += len(chunk)
            updated_total += len(applied)

            await ws_manager.broadcast_to_user(
                uid,
                {
                    "type": "outline_titles_regen_progress",
                    "processed": processed_so_far,
                    "total": total_candidates,
                    "updated": updated_total,
                },
            )

            if applied:
                ws_updates = [
                    {
                        "chat_id": str(u["chat_id"]),
                        "message_id": int(u["message_id"]),
                        "auto_title": str(u["auto_title"]),
                    }
                    for u in applied
                ]
                await ws_manager.broadcast_to_user(
                    uid,
                    {
                        "type": "outline_titles_batch_updated",
                        "updates": ws_updates,
                    },
                )

                by_chat: dict[str, list[dict[str, Any]]] = {}
                for upd_item in ws_updates:
                    by_chat.setdefault(upd_item["chat_id"], []).append(upd_item)

                for cid, chat_upd_list in by_chat.items():
                    if cid not in chat_cache:
                        chat_cache[cid] = await database.get_chat(cid)
                    chat_obj = chat_cache[cid]
                    if not chat_obj or chat_obj.get("kind") != "project" or not chat_obj.get("project_name"):
                        continue
                    pname = str(chat_obj["project_name"])
                    if pname not in sp_members_cache:
                        sp_obj = await database.get_shared_project_by_name(pname)
                        sp_members_cache[pname] = (
                            set(sp_obj.get("member_ids") or []) if sp_obj else None
                        )
                    sp_members = sp_members_cache[pname]
                    if sp_members is not None:
                        await ws_manager.broadcast_for_chat(
                            chat_obj,
                            {
                                "type": "outline_titles_batch_updated",
                                "updates": chat_upd_list,
                            },
                            shared_member_ids=sp_members,
                        )

            stop_code = str(chunk_meta.get("stop_reason") or "")
            if stop_code in ("daily_spend_limit", "http_401", "http_402", "max_price_limit"):
                stopped_reason = stop_code
                break
    finally:
        _SMART_TITLE_REGEN_ACTIVE_USERS.discard(uid)

    state_payload = await _build_full_openrouter_state_payload(uid, refresh_live=False)
    await ws_manager.broadcast_user(
        uid,
        {"type": "openrouter_updated", "summary": state_payload["summary"]},
    )
    return {
        "ok": True,
        **state_payload,
        "regenerated_count": updated_total,
        "updated_titles": updated_total,
        "cost_usd": round(run_cost_usd, 6),
        "chunks_count": chunks_count,
        "stopped_reason": stopped_reason,
        "stop_reason": stopped_reason,
    }


@app.delete("/api/openrouter/key")
async def api_delete_openrouter_key(
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    cfg = await database.delete_user_openrouter_key(uid)
    await openrouter_service.sync_user_openrouter_cli_files(uid)
    summary, raw_cands = await asyncio.gather(
        database.get_openrouter_sidebar_summary(uid),
        database.list_user_outline_title_candidates(uid, include_existing_ai=False),
    )
    await ws_manager.broadcast_user(
        uid,
        {"type": "openrouter_updated", "summary": summary},
    )
    return {
        "ok": True,
        "config": cfg,
        "status": {},
        "summary": summary,
        "smart_titles_eligibility": _compact_smart_titles_eligibility(raw_cands),
        "smart_title_presets": openrouter_service.SMART_TITLE_MODEL_PRESETS,
        "smart_titles_regen_running": bool(uid in _SMART_TITLE_REGEN_ACTIVE_USERS),
    }


@app.delete("/api/openrouter/logs")
async def api_clear_openrouter_logs(
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    deleted = await database.clear_openrouter_logs(uid)
    spend_stats, summary = await asyncio.gather(
        database.get_openrouter_spend_stats(uid),
        database.get_openrouter_sidebar_summary(uid),
    )
    await ws_manager.broadcast_user(
        uid,
        {"type": "openrouter_updated", "summary": summary},
    )
    return {
        "ok": True,
        "deleted": deleted,
        "spend_stats": spend_stats,
        "models_summary": [],
        "summary": summary,
    }


@app.get("/api/openrouter/models")
async def api_search_openrouter_models(
    q: str = Query(""),
    max_price_per_m: float = Query(0.0),
    limit: int = Query(35),
    refresh: bool = Query(False),
    _current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    if refresh:
        await openrouter_service.get_openrouter_models_catalog(force_refresh=True)
    items = await openrouter_service.search_openrouter_models(
        query=q,
        max_price_per_m=max(0.0, float(max_price_per_m or 0.0)),
        limit=max(1, min(int(limit or 35), 80)),
    )
    return {"models": items}


# ---------------------------------------------------------------------------
# Personal ChatGPT Accounts, Device Code OAuth & Codex CLI Quotas (v80)
# ---------------------------------------------------------------------------


@app.get("/api/chatgpt/state")
async def api_get_chatgpt_state(
    refresh: int = Query(0),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    is_admin = current_user.get("role") == "admin"
    accounts = await database.list_user_chatgpt_accounts(uid)
    cli_info = await asyncio.to_thread(
        lambda: config.get_codex_cli_info(probe_version=False)
    )
    if not is_admin:
        cli_info = {
            "installed": bool(cli_info.get("installed")),
            "version": str(cli_info.get("version") or ""),
            "native_binary": bool(cli_info.get("native_binary")),
        }

    refreshed_any = False
    if cli_info.get("installed") and accounts:
        if bool(refresh):
            enabled_accs = [a for a in accounts if a.get("is_enabled", True)]
            for acc in enabled_accs:
                try:
                    await codex_engine.fetch_chatgpt_account_quotas(
                        uid,
                        str(acc["id"]),
                        include_models=bool(acc.get("is_active")),
                    )
                    refreshed_any = True
                except Exception:
                    pass
        else:
            active_acc = next(
                (a for a in accounts if a.get("is_active") and a.get("is_enabled", True)),
                None,
            )
            if active_acc is not None:
                need_live = not bool(active_acc.get("cached_at"))
                if not need_live:
                    try:
                        c_dt = datetime.fromisoformat(str(active_acc["cached_at"]))
                        if (datetime.now(timezone.utc) - c_dt).total_seconds() >= 180.0:
                            need_live = True
                    except Exception:
                        need_live = True
                if need_live:
                    try:
                        await codex_engine.fetch_chatgpt_account_quotas(
                            uid,
                            str(active_acc["id"]),
                            include_models=not bool(active_acc.get("models")),
                        )
                        refreshed_any = True
                    except Exception:
                        pass

    if refreshed_any:
        accounts = await database.list_user_chatgpt_accounts(uid)
        summary = await _broadcast_chatgpt_state_to_user(uid)
    else:
        summary = await database.get_chatgpt_sidebar_summary(uid)

    token_stats = await database.get_chatgpt_token_usage_stats(uid)
    active_obj = next((a for a in accounts if a.get("is_active")), None)
    return {
        "accounts": accounts,
        "active_account_id": str(active_obj["id"]) if active_obj else "",
        "summary": summary,
        "token_stats": token_stats,
        "cli_info": cli_info,
    }


@app.post("/api/chatgpt/auth/device/start")
@app.post("/api/chatgpt/device-auth/start")
async def api_start_chatgpt_device_auth(
    body: Optional[StartChatGptDeviceAuthRequest] = None,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    is_admin = current_user.get("role") == "admin"
    label = (body.label if body else "") or ""

    async def _on_device_login_complete(completed_uid: str, _acc: dict[str, Any]) -> None:
        await _broadcast_chatgpt_state_to_user(completed_uid)

    try:
        flow = await codex_engine.start_chatgpt_device_auth(
            uid,
            label=label,
            proxy_mode=body.proxy_mode if body else None,
            proxy_spec=body.proxy_spec if body else None,
            is_admin=is_admin,
            on_complete=_on_device_login_complete,
        )
    except (ValueError, proxy_router.ProxyConfigError, proxy_router.ProxyRouteError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return flow


@app.get("/api/chatgpt/auth/device/{flow_id}")
async def api_get_chatgpt_device_auth_flow(
    flow_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    flow = codex_engine.get_chatgpt_device_auth_flow(current_user["id"], flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Сессия авторизации не найдена")
    return flow


@app.delete("/api/chatgpt/auth/device/{flow_id}")
async def api_cancel_chatgpt_device_auth_flow(
    flow_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    ok = await codex_engine.cancel_chatgpt_device_auth_flow(current_user["id"], flow_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Сессия авторизации не найдена")
    return {"ok": True}


@app.post("/api/chatgpt/accounts/import")
@app.post("/api/chatgpt/import-auth")
async def api_import_chatgpt_account(
    body: ImportChatGptAuthRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    is_admin = current_user.get("role") == "admin"
    try:
        saved = await codex_engine.import_chatgpt_auth_json(
            uid,
            raw_auth_json=body.auth_json,
            label=body.label,
            proxy_mode=body.proxy_mode,
            proxy_spec=body.proxy_spec,
            is_admin=is_admin,
        )
    except (ValueError, proxy_router.ProxyConfigError, proxy_router.ProxyRouteError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка импорта auth.json: {exc}")

    summary = await _broadcast_chatgpt_state_to_user(uid)
    accounts = await database.list_user_chatgpt_accounts(uid)
    return {
        "ok": True,
        "account": saved,
        "accounts": accounts,
        "summary": summary,
    }


@app.post("/api/chatgpt/accounts/{account_id}/activate")
async def api_activate_chatgpt_account(
    account_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    activated = await database.activate_user_chatgpt_account(uid, account_id)
    if not activated:
        raise HTTPException(status_code=404, detail="Аккаунт ChatGPT не найден")

    await proxy_router.reload_routes_from_db()

    if not activated.get("cached_at") and config.resolve_codex_bin():
        try:
            activated = await codex_engine.fetch_chatgpt_account_quotas(
                uid, account_id, include_models=True
            )
        except Exception:
            pass

    summary = await _broadcast_chatgpt_state_to_user(uid)
    accounts = await database.list_user_chatgpt_accounts(uid)
    return {
        "ok": True,
        "account": activated,
        "accounts": accounts,
        "summary": summary,
    }


@app.patch("/api/chatgpt/accounts/{account_id}")
async def api_patch_chatgpt_account(
    account_id: str,
    body: PatchChatGptAccountRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    is_admin = current_user.get("role") == "admin"
    p_mode, p_spec, p_lbl = await _validate_account_proxy_input(
        proxy_mode=body.proxy_mode,
        proxy_spec=body.proxy_spec,
        is_admin=is_admin,
    )
    try:
        updated = await database.patch_user_chatgpt_account(
            uid,
            account_id,
            label=body.label,
            is_enabled=body.is_enabled,
            auto_failover=body.auto_failover,
            proxy_mode=p_mode,
            proxy_spec=p_spec,
            proxy_label=p_lbl,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="Аккаунт ChatGPT не найден")

    await proxy_router.reload_routes_from_db()
    summary = await _broadcast_chatgpt_state_to_user(uid)
    accounts = await database.list_user_chatgpt_accounts(uid)
    return {
        "ok": True,
        "account": updated,
        "accounts": accounts,
        "summary": summary,
    }


@app.delete("/api/chatgpt/accounts/{account_id}")
async def api_delete_chatgpt_account(
    account_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    ok = await database.delete_user_chatgpt_account(uid, account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Аккаунт ChatGPT не найден")

    await proxy_router.reload_routes_from_db()

    # Remove materialized `<CODEX_HOME>` directory on disk as well
    try:
        acc_dir = config.ensure_user_codex_account_dir(uid, account_id)
        await asyncio.to_thread(lambda: __import__("shutil").rmtree(acc_dir, ignore_errors=True))
    except Exception:
        pass

    summary = await _broadcast_chatgpt_state_to_user(uid)
    accounts = await database.list_user_chatgpt_accounts(uid)
    return {
        "ok": True,
        "accounts": accounts,
        "summary": summary,
    }


@app.post("/api/admin/codex/install")
async def api_admin_install_codex_cli(
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    try:
        cli_info = await codex_engine.install_or_update_codex_cli()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    summary = await _broadcast_chatgpt_state_to_user(admin_user["id"])
    return {
        "ok": True,
        "cli_info": cli_info,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Personal Claude Code (Pro / Max / Team) Accounts, OAuth & Live Quotas (v87)
# ---------------------------------------------------------------------------


async def _claude_state_payload(uid: str, user: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    accounts, summary, token_stats = await asyncio.gather(
        database.list_user_claude_accounts(uid),
        database.get_claude_sidebar_summary(uid),
        database.get_claude_token_usage_stats(uid),
    )
    user = user or await database.get_user_by_id(uid) or {}
    cli_info = dict(config.get_claude_cli_info())
    models = [dict(m) for m in claude_engine.DEFAULT_CLAUDE_CODE_MODELS]
    if user.get("role") != "admin":
        cli_info = {
            "installed": bool(cli_info.get("installed")),
            "version": str(cli_info.get("version") or ""),
            "source": str(cli_info.get("source") or ""),
            "npm_available": bool(cli_info.get("npm_available")),
            "path": "",
            "cli_js": "",
            "node_bin": "",
            "bin_path": "",
        }
        allowed = {str(m).strip() for m in (user.get("allowed_models") or []) if str(m).strip()}
        if "__no_opus__" in allowed:
            models = [m for m in models if "opus" not in str(m.get("id") or "").lower() or m.get("id") in allowed]
    return {
        "accounts": accounts,
        "active_account_id": str(summary.get("active_account_id") or ""),
        "summary": summary,
        "token_stats": token_stats,
        "cli_info": cli_info,
        "models": models,
    }


@app.get("/api/claude/state")
async def api_get_claude_state(
    refresh: bool = Query(False),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    if refresh:
        try:
            await asyncio.wait_for(claude_engine.refresh_all_user_claude_quotas(uid), timeout=25.0)
        except Exception:
            pass
        await _broadcast_claude_state_to_user(uid)
    else:
        active = await database.get_active_claude_account(uid, require_enabled=True)
        if active and active.get("auth_method") != "setup_token":
            stamp = (active.get("quotas") or {}).get("updated_at")
            try:
                dt = datetime.fromisoformat(str(stamp)) if stamp else None
                if dt is not None and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                stale = dt is None or (datetime.now(timezone.utc) - dt).total_seconds() >= 180
            except (ValueError, TypeError):
                stale = True
            if stale:
                try:
                    await asyncio.wait_for(claude_engine.refresh_claude_account_quotas(uid, active["id"]), timeout=25.0)
                    await _broadcast_claude_state_to_user(uid)
                except Exception:
                    pass
    return await _claude_state_payload(uid, current_user)


@app.post("/api/claude/auth/flow/start")
@app.post("/api/claude/oauth/start")
async def api_start_claude_auth_flow(
    body: Optional[StartClaudeAuthRequest] = None,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    try:
        flow = await claude_engine.start_claude_oauth_flow(
            current_user["id"],
            (body.label if body else "") or "",
            proxy_mode=body.proxy_mode if body else None,
            proxy_spec=body.proxy_spec if body else None,
            is_admin=is_admin,
        )
    except (ValueError, proxy_router.ProxyConfigError, proxy_router.ProxyRouteError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return flow


@app.get("/api/claude/auth/flow/{flow_id}")
async def api_get_claude_auth_flow(
    flow_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    flow = claude_engine.get_claude_oauth_flow(current_user["id"], flow_id)
    if not flow:
        raise HTTPException(status_code=404, detail="Сессия входа не найдена или истекла")
    return flow


@app.post("/api/claude/auth/flow/{flow_id}/code")
async def api_submit_claude_auth_code(
    flow_id: str,
    body: SubmitClaudeCodeRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    try:
        flow = await claude_engine.submit_claude_oauth_code(uid, flow_id, body.code)
    except (ValueError, proxy_router.ProxyConfigError, proxy_router.ProxyRouteError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    await _broadcast_claude_state_to_user(uid)
    return {**flow, **(await _claude_state_payload(uid, current_user))}


@app.delete("/api/claude/auth/flow/{flow_id}")
async def api_cancel_claude_auth_flow(
    flow_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    ok = await claude_engine.cancel_claude_oauth_flow(current_user["id"], flow_id)
    return {"ok": ok}


@app.post("/api/claude/accounts/import")
@app.post("/api/claude/import")
async def api_import_claude_account(
    body: ImportClaudeCredentialsRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    is_admin = current_user.get("role") == "admin"
    try:
        saved = await claude_engine.import_claude_credentials(
            uid,
            body.payload,
            body.label,
            proxy_mode=body.proxy_mode,
            proxy_spec=body.proxy_spec,
            is_admin=is_admin,
        )
    except (ValueError, proxy_router.ProxyConfigError, proxy_router.ProxyRouteError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    await _broadcast_claude_state_to_user(uid)
    return {"ok": True, "account": saved, **(await _claude_state_payload(uid, current_user))}


@app.post("/api/claude/accounts/{account_id}/activate")
async def api_activate_claude_account(
    account_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    activated = await database.activate_user_claude_account(uid, account_id)
    if not activated:
        raise HTTPException(status_code=404, detail="Аккаунт Claude не найден")
    await proxy_router.reload_routes_from_db()
    if not activated.get("cached_at") and config.resolve_claude_bin():
        try:
            await asyncio.wait_for(
                claude_engine.refresh_claude_account_quotas(uid, account_id), timeout=12.0
            )
            activated = await database.get_user_claude_account(uid, account_id) or activated
        except Exception:
            pass
    await _broadcast_claude_state_to_user(uid)
    return {"ok": True, "account": activated, **(await _claude_state_payload(uid, current_user))}


@app.patch("/api/claude/accounts/{account_id}")
async def api_patch_claude_account(
    account_id: str,
    body: PatchClaudeAccountRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    is_admin = current_user.get("role") == "admin"
    p_mode, p_spec, p_lbl = await _validate_account_proxy_input(
        proxy_mode=body.proxy_mode,
        proxy_spec=body.proxy_spec,
        is_admin=is_admin,
    )
    try:
        updated = await database.patch_user_claude_account(
            uid,
            account_id,
            label=body.label,
            is_enabled=body.is_enabled,
            auto_failover=body.auto_failover,
            proxy_mode=p_mode,
            proxy_spec=p_spec,
            proxy_label=p_lbl,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="Аккаунт Claude не найден")
    await proxy_router.reload_routes_from_db()
    await _broadcast_claude_state_to_user(uid)
    return {"ok": True, "account": updated, **(await _claude_state_payload(uid, current_user))}


@app.delete("/api/claude/accounts/{account_id}")
async def api_delete_claude_account(
    account_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    uid = current_user["id"]
    ok = await database.delete_user_claude_account(uid, account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Аккаунт Claude не найден")
    await proxy_router.reload_routes_from_db()
    await _broadcast_claude_state_to_user(uid)
    return {"ok": True, **(await _claude_state_payload(uid, current_user))}


@app.post("/api/admin/claude/install")
async def api_admin_install_claude_cli(
    current_user: dict[str, Any] = Depends(auth.require_admin),
):
    res = await claude_engine.install_or_update_claude_cli()
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=str(res.get("error") or "Ошибка установки"))
    await _broadcast_claude_state_to_user(current_user["id"])
    return {**res, **(await _claude_state_payload(current_user["id"], current_user))}


# ---------------------------------------------------------------------------
# Admin Multi-User & Shared Projects Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/admin/overview")
async def api_admin_overview(_admin: dict[str, Any] = Depends(auth.require_admin)):
    users, shared_projects, token_stats, all_models, docker_status = await asyncio.gather(
        database.list_users(),
        database.list_shared_projects(is_admin=True),
        database.get_token_usage_stats(include_by_user=True),
        fetch_available_models(),
        check_docker_status(),
    )
    by_user_map = {
        u["user_id"]: u for u in (token_stats.get("by_user") or [])
    }
    for usr in users:
        st = by_user_map.get(usr["id"]) or {}
        usr["tokens_24h"] = int(st.get("tokens_24h") or 0)
        usr["shared_tokens_24h"] = int(st.get("shared_tokens_24h") or 0)
        usr["tokens_7d"] = int(st.get("tokens_7d") or 0)
        usr["total_tokens"] = int(st.get("total_tokens") or 0)
        usr["turns"] = int(st.get("turns") or 0)

    return {
        "users": users,
        "shared_projects": shared_projects,
        "models": all_models,
        "docker_status": docker_status,
    }


@app.post("/api/admin/users")
async def api_admin_create_user(
    body: AdminCreateUserRequest,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    try:
        created = await database.create_user(
            username=body.username,
            display_name=body.display_name,
            password=body.password,
            role="user",
            max_access_mode=body.max_access_mode,
            sandbox_network=body.sandbox_network,
            allowed_models=body.allowed_models,
            daily_token_limit=body.daily_token_limit,
            quota_reserve_pct=body.quota_reserve_pct,
        )
        await sandbox_service.sync_user_sandbox_cli_files_by_id(created["id"])
        await host_service_manager.sync_user_host_cli_files_by_id(created["id"])
        return {"ok": True, "user": created}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/admin/users/{user_id}")
@app.put("/api/admin/users/{user_id}")
async def api_admin_update_user(
    user_id: str,
    body: AdminUpdateUserRequest,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    prev_user = await database.get_user_by_id(user_id)
    try:
        updated = await database.update_user(
            user_id,
            display_name=body.display_name,
            password=body.password,
            is_active=body.is_active,
            max_access_mode=body.max_access_mode,
            sandbox_network=body.sandbox_network,
            allowed_models=body.allowed_models,
            daily_token_limit=body.daily_token_limit,
            quota_reserve_pct=body.quota_reserve_pct,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not updated:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if user_id == "admin" and body.password and body.password.strip():
        _update_env_pin(body.password.strip())

    await openrouter_service.sync_user_openrouter_cli_files(user_id)
    await sandbox_service.sync_user_sandbox_cli_files_by_id(user_id)
    await host_service_manager.sync_user_host_cli_files_by_id(user_id)

    net_changed = bool(
        prev_user and prev_user.get("sandbox_network") != updated.get("sandbox_network")
    )
    lost_sandbox = not sandbox_service.is_user_sandbox_eligible(updated)
    if lost_sandbox or net_changed:
        await sandbox_service.stop_user_sandboxes(user_id)

    # Immediately revoke live WebSockets if blocked or password reset (except self-edit)
    if (body.is_active is False) or (body.password and body.password.strip() and user_id != "admin"):
        await ws_manager.disconnect_user(user_id)
    else:
        all_models = await fetch_available_models(force_refresh=False)
        await ws_manager.broadcast_models_per_user(
            all_models, infer_account_tier(all_models)
        )

    return {"ok": True, "user": updated}


@app.delete("/api/admin/users/{user_id}")
async def api_admin_delete_user(
    user_id: str,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    try:
        await ws_manager.disconnect_user(user_id)
        await sandbox_service.stop_user_sandboxes(user_id)
        await asyncio.to_thread(
            sandbox_service.sync_user_sandbox_cli_files,
            None,
            force_disable_user_id=user_id,
        )
        await asyncio.to_thread(
            host_service_manager.sync_user_host_cli_files,
            None,
            force_disable_user_id=user_id,
        )
        ok = await database.delete_user(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"ok": True}


async def _maybe_upgrade_shared_project_members_docker(
    member_ids: list[str],
    execution_mode: str,
    upgrade: bool,
) -> list[str]:
    if not upgrade or str(execution_mode or "docker").strip().lower() != "docker":
        return []
    upgraded: list[str] = []
    for uid in member_ids or []:
        clean_uid = str(uid or "").strip()
        if not clean_uid or clean_uid == "admin":
            continue
        usr = await database.get_user_by_id(clean_uid)
        if (
            usr
            and usr.get("role") != "admin"
            and str(usr.get("max_access_mode") or "write_files").strip().lower()
            in ("read_only", "write_files")
        ):
            await database.update_user(clean_uid, max_access_mode="docker_full")
            upgraded.append(clean_uid)
    return upgraded


@app.get("/api/admin/shared-projects")
async def api_admin_list_shared_projects(_admin: dict[str, Any] = Depends(auth.require_admin)):
    items = await database.list_shared_projects(is_admin=True)
    return {"shared_projects": items}


@app.post("/api/admin/shared-projects")
async def api_admin_create_shared_project(
    body: AdminCreateSharedProjectRequest,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    try:
        sp = await database.create_shared_project(
            name=body.name,
            description=body.description,
            execution_mode=body.execution_mode or "docker",
            member_ids=body.member_ids,
            created_by=admin_user["id"],
            storage_location=body.storage_location,
        )
        upgraded_ids = await _maybe_upgrade_shared_project_members_docker(
            sp.get("member_ids") or [],
            sp.get("execution_mode", "docker"),
            body.upgrade_members_docker,
        )
        await sandbox_service.sync_all_users_sandbox_cli_files()
        return {"ok": True, "shared_project": sp, "upgraded_member_ids": upgraded_ids}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/api/admin/shared-projects/{sp_id}")
async def api_admin_update_shared_project(
    sp_id: str,
    body: AdminUpdateSharedProjectRequest,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    sp = await database.get_shared_project(sp_id)
    if not sp:
        raise HTTPException(404, "Общий проект не найден")
    request = ShareProjectRequest(
        description=body.description if body.description is not None else sp["description"],
        execution_mode=body.execution_mode if body.execution_mode is not None else sp["execution_mode"],
        member_ids=body.member_ids if body.member_ids is not None else sp["member_ids"],
        storage_location=body.storage_location if body.storage_location is not None else sp["storage_location"],
        upgrade_members_docker=body.upgrade_members_docker)
    return await api_share_project(sp["name"],request,_admin)


@app.delete("/api/admin/shared-projects/{sp_id}")
async def api_admin_delete_shared_project(
    sp_id: str,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    sp = await database.get_shared_project(sp_id)
    if not sp:
        raise HTTPException(404, "Общий проект не найден")
    return await api_unshare_project(sp["name"],_admin)


# ---------------------------------------------------------------------------
# Certs, Voice, Models & QR
# ---------------------------------------------------------------------------


@app.get("/api/system/ca.crt")
async def download_local_ca():
    """Serve public local Root CA cert so smartphones can trust LAN HTTPS (:8766) & PWA mic."""
    ca_bytes = certs.get_ca_cert_bytes()
    return Response(
        content=ca_bytes,
        media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="AntigravityHubCA.crt"'},
    )


async def _collect_user_voice_context_terms(
    user: dict[str, Any],
    active_project: str = "",
) -> list[str]:
    terms: list[str] = []
    act = (active_project or "").strip()
    if act:
        terms.append(act)
    try:
        is_admin = user.get("role") == "admin"
        shared_projs = await database.list_shared_projects(
            for_user_id=user["id"], is_admin=is_admin
        )
        for sp in shared_projs:
            nm = (sp.get("name") or "").strip()
            if nm and nm not in terms:
                terms.append(nm)
        projs = await asyncio.to_thread(
            brain_reader.list_projects_for_user, user, shared_projs
        )
        for p in projs:
            nm = (p.get("name") or "").strip()
            if nm and nm not in terms:
                terms.append(nm)
    except Exception:
        pass
    return terms[:40]


@app.get("/api/voice/status")
async def api_voice_status(_u: dict[str, Any] = Depends(auth.get_current_user)):
    return voice.get_voice_status()


@app.get("/api/voice/live-capabilities")
async def api_voice_live_capabilities(
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    all_models = await fetch_available_models()
    user_models = await _get_models_for_user(all_models, current_user)
    user_model_ids = {m["id"] for m in user_models}
    turbo_models = [
        c for c in tts_service.LIVE_TURBO_MODEL_CANDIDATES if c["id"] in user_model_ids
    ]
    if not turbo_models and user_models:
        turbo_models = [
            {"id": user_models[0]["id"], "label": user_models[0].get("name") or user_models[0]["id"]}
        ]
    default_turbo = (
        turbo_models[0]["id"]
        if turbo_models
        else "gemini-3.8-flash-low"
    )
    return {
        "live_voice_supported": True,
        "tts_available": tts_service.is_edge_tts_available(),
        "default_voice": tts_service.DEFAULT_TTS_VOICE,
        "voices": tts_service.get_allowed_tts_voices_list(),
        "default_rate": "+15%",
        "default_turbo_model": default_turbo,
        "turbo_models": turbo_models,
        "stt_status": voice.get_voice_status(),
    }


@app.post("/api/voice/tts")
async def api_voice_tts(
    body: LiveTtsRequest,
    _u: dict[str, Any] = Depends(auth.get_current_user),
):
    raw_text = str(body.text or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="Пустой текст для озвучивания")
    if len(raw_text) > 8000:
        raise HTTPException(status_code=413, detail="Текст для озвучивания слишком длинный")
    clean_text = tts_service.sanitize_text_for_tts(raw_text)
    if not clean_text:
        raise HTTPException(status_code=400, detail="Нет текста для озвучивания после очистки")
    resolved_voice = tts_service.resolve_tts_voice(body.voice)
    norm_rate = tts_service.normalize_tts_rate(body.rate)
    try:
        mp3_bytes = await tts_service.synthesize_tts_mp3(
            clean_text,
            voice=resolved_voice,
            rate=norm_rate,
            already_sanitized=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка синтеза речи: {exc}")
    if not mp3_bytes:
        raise HTTPException(status_code=502, detail="Пустой аудиопоток TTS")
    return Response(
        content=mp3_bytes,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/voice/transcribe")
async def api_voice_transcribe(
    audio: UploadFile = File(...),
    language: str = Form("ru"),
    engine: str = Form("gigaam"),
    model: str = Form("accurate"),
    smart_format: str = Form("true"),
    live_preview: str = Form("false"),
    offset_sec: float = Form(0.0),
    project_name: str = Form(""),
    _u: dict[str, Any] = Depends(auth.get_current_user),
):
    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Пустой аудиофайл")
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Аудиофайл превышает 25 МБ")
    try:
        is_live = live_preview.strip().lower() in ("1", "true", "yes", "on")
        res = await voice.transcribe_audio(
            raw,
            language=language,
            engine=engine,
            model_name=model,
            live_preview=is_live,
            offset_sec=max(0.0, float(offset_sec or 0.0)),
        )
        use_smart = smart_format.strip().lower() in ("1", "true", "yes", "on")
        if use_smart and res.get("text"):
            ctx_terms = await _collect_user_voice_context_terms(
                _u, active_project=project_name
            )
            native_punct = bool(res.get("native_punct", False))
            fmt = await voice.format_speech_text(
                res["text"],
                language=language,
                is_partial=is_live,
                native_punct=native_punct,
                context_terms=ctx_terms,
            )
            res["raw_text"] = res["text"]
            res["text"] = fmt["text"]
            res["smart_formatted"] = fmt["smart_formatted"]
            res["format_latency_ms"] = fmt["latency_ms"]
            if res.get("commit_text"):
                fmt_commit = await voice.format_speech_text(
                    res["commit_text"],
                    language=language,
                    is_partial=True,
                    native_punct=native_punct,
                    context_terms=ctx_terms,
                )
                res["commit_text"] = fmt_commit["text"] or res["commit_text"]
            if res.get("tail_text"):
                fmt_tail = await voice.format_speech_text(
                    res["tail_text"],
                    language=language,
                    is_partial=True,
                    native_punct=native_punct,
                    context_terms=ctx_terms,
                )
                res["tail_text"] = fmt_tail["text"] or res["tail_text"]
        return res
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ошибка распознавания речи: {exc}")


@app.post("/api/voice/format")
async def api_voice_format(
    body: VoiceFormatRequest,
    _u: dict[str, Any] = Depends(auth.get_current_user),
):
    if len(body.text) > 12000:
        raise HTTPException(status_code=413, detail="Текст диктовки слишком длинный")
    ctx_terms = await _collect_user_voice_context_terms(
        _u, active_project=body.project_name
    )
    return await voice.format_speech_text(
        body.text,
        language=body.language,
        is_partial=body.is_partial,
        native_punct=body.native_punct,
        context_terms=ctx_terms,
    )


@app.get("/api/models")
async def get_models(
    refresh: bool = Query(False),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    models = await fetch_available_models(force_refresh=(refresh and is_admin))
    return {"models": await _get_models_for_user(models, current_user)}


@app.get("/api/advisor/catalog")
async def api_get_advisor_catalog(
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    all_models = get_cached_models()
    user_models = await _get_models_for_user(all_models, current_user)
    return await model_advisor_service.build_catalog_view(user_models)


@app.post("/api/advisor/recalibrate")
async def api_recalibrate_advisor_catalog(
    current_user: dict[str, Any] = Depends(auth.require_admin),
):
    all_models = get_cached_models()
    user_models = await _get_models_for_user(all_models, current_user)
    model_advisor_service.notify_models_discovered(user_models)

    async def _run_bg_recalibrate() -> None:
        try:
            await model_advisor_service.run_auto_calibration_once(
                models=user_models,
                force=True,
                has_active_runs_fn=_is_any_ai_generation_active,
            )
        except Exception:
            pass

    _spawn_telemetry_task(_run_bg_recalibrate())
    catalog = await model_advisor_service.build_catalog_view(user_models)
    catalog["triggered"] = True
    return catalog


@app.get("/api/system/qr")
async def system_qr(
    url: Optional[str] = Query(None),
    _u: dict[str, Any] = Depends(auth.get_current_user),
):
    target_url = (url or "").strip()
    if not target_url:
        net = config.get_network_endpoints()
        target_url = net["primary_mobile_url"]
    svg_str = auth.generate_qr_svg(target_url)
    return Response(content=svg_str, media_type="image/svg+xml")


def _spawn_detached_server_restart() -> None:
    """Spawn a detached background helper that waits 1s, stops the current server PID and
    starts `python.exe run_server.py` with CREATE_NO_WINDOW (headless invisible console).

    The new server owns a hidden console, so every console grandchild (agy.exe, git, node …)
    inherits it instead of allocating a fresh one that Windows 11 would hand to Windows Terminal.
    The helper itself still runs under pythonw.exe (it never spawns console children visibly).
    """
    base_dir = config.BASE_DIR.resolve()
    run_script = base_dir / "run_server.py"
    old_pid = os.getpid()
    if os.name == "nt":
        scripts_dir = base_dir / ".venv" / "Scripts"
        pythonw_bin = scripts_dir / "pythonw.exe"
        python_bin = scripts_dir / "python.exe"
        cur_exe = Path(sys.executable)
        py_exe = str(pythonw_bin if pythonw_bin.exists() else cur_exe)
        if python_bin.exists():
            server_exe = str(python_bin)
        else:
            alt = cur_exe.with_name("python.exe")
            server_exe = str(alt if alt.exists() else cur_exe)
        # NB: no DETACHED_PROCESS (0x08) for the server — it would cancel CREATE_NO_WINDOW.
        helper_code = (
            "import subprocess, time\n"
            "time.sleep(1.0)\n"
            f"subprocess.run(['taskkill', '/F', '/PID', '{old_pid}'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)\n"
            "time.sleep(0.6)\n"
            "from app.windows_desktop import spawn_on_desktop, ensure_windows_console_host_delegation\n"
            "ensure_windows_console_host_delegation()\n"
            f"spawn_on_desktop([{server_exe!r}, {str(run_script)!r}], cwd={str(base_dir)!r})\n"
        )
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        subprocess.Popen(
            [py_exe, "-c", helper_code],
            cwd=str(base_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
    else:
        py_exe = sys.executable
        systemd_managed = os.environ.get("ZEROHUB_SYSTEMD") == "1" or bool(os.environ.get("INVOCATION_ID"))
        restart_delay = 0.8 if systemd_managed else 1.0
        helper_code = (
            "import os, signal, subprocess, time\nfrom pathlib import Path\n"
            f"time.sleep({restart_delay})\n"
            f"try:\n    os.kill({old_pid}, signal.SIGTERM)\nexcept ProcessLookupError:\n    pass\n"
        )
        if not systemd_managed:
            helper_code += (
                "time.sleep(0.6)\n"
                f"server = subprocess.Popen([{py_exe!r}, {str(run_script)!r}], "
                f"cwd={str(base_dir)!r}, start_new_session=True, close_fds=True)\n"
                f"pid_file = Path({str(base_dir / 'data' / 'hub.pid')!r})\n"
                f"if pid_file.is_file() and pid_file.read_text().strip() == {str(old_pid)!r}:\n"
                "    pid_temp = pid_file.with_suffix('.pid.tmp')\n"
                "    pid_temp.write_text(str(server.pid) + '\\n')\n"
                "    pid_temp.replace(pid_file)\n"
            )
        subprocess.Popen(
            [py_exe, "-c", helper_code],
            cwd=str(base_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )


class UpdateApplyRequest(BaseModel):
    target_version: Optional[str] = Field(None, max_length=48)
    force: bool = False


class PublisherConfigRequest(BaseModel):
    owner: str = Field(max_length=100)
    repo: str = Field(max_length=100)
    new_token: Optional[str] = Field(None, max_length=512)
    clear_token: bool = False


class PublishReleaseRequest(BaseModel):
    version: str = Field(max_length=48)
    title: str = Field(max_length=250)
    release_notes: str = Field(max_length=125_000)


async def _run_release_operation(operation, **kwargs):
    try:
        return await asyncio.to_thread(operation, **kwargs)
    except release_service.ReleaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except OSError:
        raise HTTPException(status_code=500, detail="ZeroHub release files are unavailable.") from None


_UPDATE_BOOT_ID = uuid.uuid4().hex


@app.get("/api/health")
async def api_health():
    return {"ok": True, "version": config.get_app_version(), "platform": config.PLATFORM_LABEL,
            "boot_id": _UPDATE_BOOT_ID}


@app.get("/api/updates/status")
async def api_updates_status(user: dict[str, Any] = Depends(auth.get_current_user)):
    settings = await _run_release_operation(release_service.get_publisher_settings)
    slug = f"{settings['owner']}/{settings['repo']}"
    current = config.get_app_version()
    latest = release_service.get_cached_update()
    if latest and (latest["repo_slug"] != slug or latest["current_version"] != current):
        latest = None
    pending = {cid for cid, task in ACTIVE_TURN_TASKS.items() if not task.done()}
    active_count = len(set(active_generations) | COMPACTING_CHATS | pending)
    response = {"current_version": current, "repo_slug": slug, "latest": latest,
                "active_generations": active_count}
    if user.get("role") == "admin":
        settings["draft_notes"] = await _run_release_operation(release_service.build_draft_release_notes)
        response["publisher"] = settings
    return response


@app.post("/api/updates/check")
async def api_updates_check(_admin: dict[str, Any] = Depends(auth.require_admin)):
    return await _run_release_operation(release_service.check_for_updates, force=True)


@app.post("/api/updates/apply")
async def api_updates_apply(
    body: UpdateApplyRequest = UpdateApplyRequest(),
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    if not body.force and (
        active_generations or COMPACTING_CHATS or any(not task.done() for task in ACTIVE_TURN_TASKS.values())
    ):
        raise HTTPException(status_code=409, detail="Wait for active generations to finish before updating.")
    result = await _run_release_operation(release_service.apply_one_click_update, target_version=body.target_version)
    asyncio.get_running_loop().call_later(0.6, _spawn_detached_server_restart)
    return {**result, "restarting": True, "previous_boot_id": _UPDATE_BOOT_ID}


@app.put("/api/updates/publisher-config")
async def api_updates_publisher_config(
    body: Any = Body(),
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    try:
        request = PublisherConfigRequest.model_validate(body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Invalid publisher configuration.") from None
    return await _run_release_operation(release_service.save_publisher_settings, **request.model_dump())


@app.post("/api/updates/publish")
async def api_updates_publish(
    body: PublishReleaseRequest,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    return await _run_release_operation(release_service.publish_release_to_github, **body.model_dump())


@app.post("/api/system/restart")
async def api_system_restart(
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    active_count = len(active_generations)
    asyncio.get_running_loop().call_later(0.4, _spawn_detached_server_restart)
    return {
        "ok": True,
        "restarting": True,
        "active_generations": active_count,
    }


# ---------------------------------------------------------------------------
# Chats & Uploads (Scoped per User + Shared Projects)
# ---------------------------------------------------------------------------


@app.get("/api/chats")
async def api_list_chats(
    kind: Optional[str] = Query(None),
    project_name: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    recent: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=30),
    include_archived: bool = Query(True),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    shared_projs = await database.list_shared_projects(
        for_user_id=current_user["id"], is_admin=is_admin
    )
    shared_names = [sp["name"] for sp in shared_projs]
    if recent:
        chats = await database.list_chats(
            kind=kind,
            project_name=project_name,
            query=query,
            owner_id=current_user["id"],
            shared_project_names=shared_names,
            recent_only=True,
            limit=limit,
            include_archived=include_archived,
        )
        folders: list[str] = []
    else:
        chats, folders = await asyncio.gather(
            database.list_chats(
                kind=kind,
                project_name=project_name,
                query=query,
                owner_id=current_user["id"],
                shared_project_names=shared_names,
                recent_only=False,
                limit=limit,
                include_archived=include_archived,
            ),
            database.list_chat_folders(owner_id=current_user["id"]),
        )
    shared_low_set = {n.lower() for n in shared_names}
    uid = str(current_user["id"])
    for c in chats:
        cid = c["id"]
        c["is_generating"] = is_chat_generating(cid)
        c["is_compacting"] = cid in COMPACTING_CHATS
        c["queue_count"] = len(CHAT_TURN_QUEUES.get(cid) or [])
        c["queue_paused"] = cid in CHAT_QUEUE_PAUSED
        c["is_shared_project"] = bool(
            c.get("kind") == "project"
            and (c.get("project_name") or "").lower() in shared_low_set
        )
        last_read_ms = get_user_chat_last_read_ts_ms(uid, cid)
        if last_read_ms is not None:
            c["last_read_ts_ms"] = last_read_ms
        if cid in _CHAT_LAST_TERMINAL_TS_MS:
            c["last_terminal_ts_ms"] = int(_CHAT_LAST_TERMINAL_TS_MS[cid])
        if not is_admin:
            c.pop("project_path", None)
    return {"chats": chats, "folders": folders}


@app.post("/api/chats")
async def api_create_chat(
    body: CreateChatRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    kind = "project" if body.kind == "project" else "chat"
    proj_path = ""
    proj_name = ""
    shared_members: Optional[set[str]] = None

    if kind == "project":
        shared_projs = await database.list_shared_projects(
            for_user_id=current_user["id"], is_admin=is_admin
        )
        resolved = brain_reader.resolve_user_project(
            body.project_name, current_user, shared_projs
        )
        if not resolved:
            raise HTTPException(
                status_code=404,
                detail="Папка проекта не найдена или доступ запрещён",
            )
        proj_path = str(resolved["path"])
        proj_name = resolved["name"]
        if resolved.get("is_shared"):
            shared_members = set(resolved.get("member_ids") or [])

    all_models = await fetch_available_models()
    allowed_models = await _get_models_for_user(all_models, current_user)
    allowed_ids = {m["id"] for m in allowed_models}
    cli_ok = bool(get_cli_auth_status().get("ok", True))
    req_model = (body.model or "").strip()
    if req_model and req_model in allowed_ids and (is_codex_model(req_model) or is_claude_code_model(req_model)):
        chosen_model = req_model
    else:
        chosen_model = resolve_active_model_id(
            req_model or pick_default_model_id(allowed_models),
            allowed_models,
            cli_online=cli_ok,
        )

    chosen_access = _cap_access_mode_for_user(body.access_mode, kind, current_user)

    chat = await database.create_chat(
        owner_id=current_user["id"],
        kind=kind,
        project_name=proj_name,
        project_path=proj_path,
        title=body.title or "Новый диалог",
        model=chosen_model,
        access_mode=chosen_access,
        icon=body.icon or "",
        folder=body.folder if kind == "chat" else "",
        preferred_agy_account_id=(body.preferred_agy_account_id if is_admin else ""),
        preferred_codex_account_id=body.preferred_codex_account_id,
        preferred_claude_account_id=body.preferred_claude_account_id,
    )
    chat["is_generating"] = False
    chat["is_compacting"] = False
    chat["is_shared_project"] = shared_members is not None
    await ws_manager.broadcast_for_chat(
        chat, {"type": "chat_created", "chat": chat}, shared_member_ids=shared_members
    )
    if not is_admin:
        chat = dict(chat)
        chat.pop("project_path", None)
    return {"chat": chat}


@app.post("/api/chats/reorder")
async def api_reorder_chats(
    body: ReorderChatsRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    clean_ids = [str(cid).strip() for cid in (body.chat_ids or []) if str(cid).strip()]
    if clean_ids:
        await database.reorder_chats(clean_ids, owner_id=current_user["id"])
        await ws_manager.broadcast_user(
            current_user["id"],
            {"type": "chats_reordered", "chat_ids": clean_ids},
        )
    return {"ok": True}


@app.post("/api/chats/mark-read")
async def api_mark_chats_read(
    body: MarkChatsReadRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    synced = await sync_and_broadcast_user_chat_read(
        current_user["id"],
        chat_id=str(body.chat_id or ""),
        chat_ids=body.chat_ids,
        read_all=bool(body.read_all),
        read_ts_ms=body.read_ts_ms,
    )
    return {"ok": True, **synced}


@app.get("/api/chats/{chat_id}/mention-stats")
async def api_chat_mention_stats(chat_id: str, current_user: dict[str, Any] = Depends(auth.get_current_user)):
    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    allowed, _ = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Access denied")
    result = {}
    for scope in ("full", "last", "summary"):
        digest = await database.build_referenced_chat_digest(chat_id, scope=scope)
        result[scope] = {"tokens": agy_engine.estimate_text_tokens(digest["block"]) if digest else 0,
                         "chars": digest["chars"] if digest else 0,
                         "truncated": bool(digest and digest["truncated"])}
    return result


@app.get("/api/chats/{chat_id}")
async def api_get_chat(
    chat_id: str,
    include_thinking: bool = Query(True),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, sp = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

    chat["is_generating"] = is_chat_generating(chat_id)
    chat["is_compacting"] = chat_id in COMPACTING_CHATS
    chat["is_shared_project"] = sp is not None
    last_read_ms = get_user_chat_last_read_ts_ms(current_user["id"], chat_id)
    if last_read_ms is not None:
        chat["last_read_ts_ms"] = last_read_ms
    if chat_id in _CHAT_LAST_TERMINAL_TS_MS:
        chat["last_terminal_ts_ms"] = int(_CHAT_LAST_TERMINAL_TS_MS[chat_id])
    messages = await database.get_messages(chat_id, include_thinking=include_thinking)
    snap = get_active_run_snapshot(chat_id)
    if snap and messages:
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("status") == "streaming":
                if snap["content"]:
                    m["content"] = snap["content"]
                if snap["thinking"]:
                    m["thinking"] = snap["thinking"]
                    m["has_thinking"] = True
                if snap["tool_calls"]:
                    m["tool_calls"] = database.compact_tool_calls_payload(snap["tool_calls"])
                m["thinking_active"] = snap["thinking_active"]
                m["stream_seq"] = snap["stream_seq"]
                break
    context_stats = compute_chat_context_stats(chat, messages)
    chat["context_stats"] = context_stats
    chat["context_pct"] = float(context_stats.get("pct") or 0.0)
    chat["context_used_tokens"] = int(context_stats.get("used_tokens") or 0)
    chat.pop("compacted_summary", None)
    if current_user.get("role") != "admin":
        chat.pop("project_path", None)
    return {
        "chat": chat,
        "messages": messages,
        "context_stats": context_stats,
        "queued_messages": _get_public_queue(chat_id),
        "queue_paused": chat_id in CHAT_QUEUE_PAUSED,
    }


@app.get("/api/chats/{chat_id}/messages/{message_id}/thinking")
async def api_get_message_thinking(
    chat_id: str,
    message_id: int,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, _ = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

    msg_th = await database.get_message_thinking(chat_id, message_id)
    if not msg_th:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")

    thinking_text = msg_th["thinking"]
    if msg_th.get("status") == "streaming":
        snap = get_active_run_snapshot(chat_id)
        if snap and snap.get("thinking"):
            thinking_text = snap["thinking"]

    return {"id": int(message_id), "thinking": thinking_text}


@app.patch("/api/chats/{chat_id}/messages/{message_id}/bookmark")
async def api_bookmark_message(
    chat_id: str,
    message_id: int,
    body: BookmarkMessageRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, sp = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

    updated_msg = await database.set_message_bookmarked(
        chat_id, int(message_id), bool(body.bookmarked)
    )
    if not updated_msg:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")

    shared_members = set(sp.get("member_ids") or []) if sp else None
    await ws_manager.broadcast_for_chat(
        chat,
        {
            "type": "message_bookmarked",
            "chat_id": chat_id,
            "message_id": int(message_id),
            "bookmarked": bool(updated_msg.get("bookmarked")),
        },
        shared_member_ids=shared_members,
    )
    _notify_gdrive_change_for_user(current_user, "bookmark", is_shared=bool(sp))
    return {
        "ok": True,
        "chat_id": chat_id,
        "message_id": int(message_id),
        "bookmarked": bool(updated_msg.get("bookmarked")),
    }


@app.patch("/api/chats/{chat_id}/messages/{message_id}/title")
async def api_update_message_title(
    chat_id: str,
    message_id: int,
    req: UpdateMessageTitleRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, sp = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

    updated = await database.set_message_custom_title(
        chat_id, int(message_id), req.custom_title
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")

    shared_members = set(sp.get("member_ids") or []) if sp else None
    await ws_manager.broadcast_for_chat(
        chat,
        {
            "type": "message_title_updated",
            "chat_id": chat_id,
            "message_id": int(message_id),
            "custom_title": updated["custom_title"],
            "auto_title": updated.get("auto_title", ""),
        },
        shared_member_ids=shared_members,
    )
    return {
        "status": "ok",
        "ok": True,
        "chat_id": chat_id,
        "message_id": int(message_id),
        "custom_title": updated["custom_title"],
        "auto_title": updated.get("auto_title", ""),
    }


@app.post("/api/chats/{chat_id}/messages/{message_id}/handoff")
async def api_record_message_handoff(
    chat_id: str,
    message_id: int,
    req: RecordMessageHandoffRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, sp = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

    if not str(req.target_chat_id or "").strip():
        raise HTTPException(status_code=400, detail="Не указан целевой чат")

    handoffs = await database.append_message_code_handoff(
        chat_id, int(message_id), req.model_dump()
    )
    if handoffs is None:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")

    shared_members = set(sp.get("member_ids") or []) if sp else None
    await ws_manager.broadcast_for_chat(
        chat,
        {
            "type": "message_handoff_updated",
            "chat_id": chat_id,
            "message_id": int(message_id),
            "code_handoffs": handoffs,
        },
        shared_member_ids=shared_members,
    )
    return {
        "ok": True,
        "chat_id": chat_id,
        "message_id": int(message_id),
        "code_handoffs": handoffs,
    }


@app.post("/api/chats/{chat_id}/messages/{message_id}/hq-return")
async def api_record_message_hq_return(
    chat_id: str,
    message_id: int,
    req: RecordMessageHqReturnRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, sp = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

    clean_target_id = str(req.target_chat_id or "").strip()
    if not clean_target_id:
        raise HTTPException(status_code=400, detail="Не указан целевой чат Штаба")

    if not clean_target_id.startswith("desktop:"):
        target_chat = await database.get_chat(clean_target_id)
        if not target_chat:
            raise HTTPException(status_code=404, detail="Целевой чат Штаба не найден")
        target_allowed, _ = await _get_chat_access_context(target_chat, current_user)
        if not target_allowed:
            raise HTTPException(
                status_code=403,
                detail="Нет доступа к целевому диалогу Штаба",
            )

    hq_returns = await database.append_message_hq_return(
        chat_id, int(message_id), req.model_dump()
    )
    if hq_returns is None:
        raise HTTPException(status_code=404, detail="Сообщение не найдено")

    shared_members = set(sp.get("member_ids") or []) if sp else None
    await ws_manager.broadcast_for_chat(
        chat,
        {
            "type": "message_hq_return_updated",
            "chat_id": chat_id,
            "message_id": int(message_id),
            "hq_returns": hq_returns,
        },
        shared_member_ids=shared_members,
    )
    return {
        "ok": True,
        "chat_id": chat_id,
        "message_id": int(message_id),
        "hq_returns": hq_returns,
    }


@app.patch("/api/chats/{chat_id}")
async def api_update_chat(
    chat_id: str,
    body: UpdateChatRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    existing = await database.get_chat(chat_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Чат не найден")

    allowed, existing_sp = await _get_chat_access_context(existing, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

    new_kind: Optional[str] = None
    new_proj_name: Optional[str] = None
    new_proj_path: Optional[str] = None
    new_conv_id: Optional[str] = None
    new_folder: Optional[str] = body.folder
    effective_kind = existing["kind"]
    shared_members: Optional[set[str]] = (
        set(existing_sp.get("member_ids") or []) if existing_sp else None
    )

    if body.kind is not None or body.project_name is not None:
        target_kind = (
            "project"
            if (body.kind if body.kind is not None else existing["kind"]) == "project"
            else "chat"
        )
        effective_kind = target_kind
        if target_kind == "project":
            proj_candidate = (
                body.project_name if body.project_name is not None else existing["project_name"]
            ).strip()
            shared_projs = await database.list_shared_projects(
                for_user_id=current_user["id"], is_admin=is_admin
            )
            resolved = brain_reader.resolve_user_project(
                proj_candidate, current_user, shared_projs
            )
            if not resolved:
                raise HTTPException(
                    status_code=404,
                    detail="Папка проекта не найдена или доступ запрещён",
                )
            new_kind = "project"
            new_proj_name = resolved["name"]
            new_proj_path = str(resolved["path"])
            new_folder = ""
            shared_members = (
                set(resolved.get("member_ids") or []) if resolved.get("is_shared") else None
            )
            if (
                existing["kind"] != "project"
                or (existing["project_name"] or "").lower() != resolved["name"].lower()
            ):
                new_conv_id = ""
        else:
            new_kind = "chat"
            new_proj_name = ""
            new_proj_path = ""
            shared_members = None
            if existing["kind"] != "chat":
                new_conv_id = ""

    new_access: Optional[str] = None
    if body.access_mode is not None or effective_kind != existing["kind"]:
        candidate_access = (
            body.access_mode if body.access_mode is not None else existing["access_mode"]
        )
        new_access = _cap_access_mode_for_user(candidate_access, effective_kind, current_user)

    new_model = body.model
    if new_model is not None and not is_admin:
        all_models = await fetch_available_models()
        allowed_ids = {m["id"] for m in await _get_models_for_user(all_models, current_user)}
        if allowed_ids and new_model not in allowed_ids:
            raise HTTPException(
                status_code=403,
                detail="Выбранная модель не разрешена для вашего аккаунта",
            )

    eff_is_archived: Optional[bool] = body.is_archived
    if eff_is_archived is None and bool(existing.get("is_archived")):
        moving_kind = body.kind is not None
        moving_proj = (
            body.project_name is not None
            and (new_proj_name or "").strip().lower()
            != str(existing.get("project_name") or "").strip().lower()
        )
        moving_folder = (
            body.folder is not None
            and str(body.folder or "").strip() != str(existing.get("folder") or "").strip()
        )
        if moving_kind or moving_proj or moving_folder:
            eff_is_archived = False

    updated = await database.update_chat(
        chat_id,
        title=body.title,
        model=new_model,
        access_mode=new_access,
        pinned=body.pinned,
        icon=body.icon,
        folder=new_folder,
        sort_order=body.sort_order,
        kind=new_kind,
        project_name=new_proj_name,
        project_path=new_proj_path,
        agy_conversation_id=new_conv_id,
        codex_thread_id=new_conv_id,
        codex_account_id=new_conv_id,
        claude_session_id=new_conv_id,
        claude_account_id=new_conv_id,
        preferred_agy_account_id=(body.preferred_agy_account_id if is_admin else None),
        preferred_codex_account_id=body.preferred_codex_account_id,
        preferred_claude_account_id=body.preferred_claude_account_id,
        is_archived=eff_is_archived,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Чат не найден")
    updated["is_generating"] = is_chat_generating(chat_id)
    updated["is_compacting"] = chat_id in COMPACTING_CHATS
    updated["queue_count"] = len(CHAT_TURN_QUEUES.get(chat_id) or [])
    updated["queue_paused"] = chat_id in CHAT_QUEUE_PAUSED
    updated["is_shared_project"] = shared_members is not None
    upd_stats = await database.compute_single_chat_context_stats(updated)
    updated["context_stats"] = upd_stats
    updated["context_pct"] = float(upd_stats.get("pct") or 0.0)
    updated["context_used_tokens"] = int(upd_stats.get("used_tokens") or 0)
    updated.pop("compacted_summary", None)
    await ws_manager.broadcast_for_chat(
        updated, {"type": "chat_updated", "chat": updated}, shared_member_ids=shared_members
    )
    if bool(updated.get("is_archived")):
        await sync_and_broadcast_user_chat_read(current_user["id"], chat_id=chat_id)
    _notify_gdrive_change_for_user(
        current_user,
        "chat_archived" if body.is_archived is not None else "chat_updated",
        is_shared=(shared_members is not None),
    )
    if not is_admin:
        updated = dict(updated)
        updated.pop("project_path", None)
    return {"chat": updated, "context_stats": updated["context_stats"]}


@app.get("/api/system/storage-health")
async def api_storage_health(current_user: dict[str, Any] = Depends(auth.get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    from .storage_maintenance import storage_health
    return await storage_health()


@app.post("/api/system/maintenance")
async def api_system_maintenance(current_user: dict[str, Any] = Depends(auth.get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    from .storage_maintenance import run_maintenance
    return await run_maintenance(lambda: not active_generations and not COMPACTING_CHATS
                                 and not any(not t.done() for t in ACTIVE_TURN_TASKS.values()))


@app.delete("/api/chats/{chat_id}")
async def api_delete_chat(
    chat_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    existing = await database.get_chat(chat_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, sp = await _get_chat_access_context(existing, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к удалению этого диалога")
    # In shared projects, only the chat creator or Admin can delete a thread
    if sp and current_user.get("role") != "admin" and existing.get("owner_id") != current_user["id"]:
        raise HTTPException(
            status_code=403,
            detail="В общем проекте удалять чужие ветки может только администратор",
        )

    CHAT_QUEUE_PAUSED.add(chat_id)
    CHAT_TURN_QUEUES.pop(chat_id, None)
    await cancel_chat_generation(chat_id)
    t_turn = ACTIVE_TURN_TASKS.pop(chat_id, None)
    t_cmp = COMPACTING_TASKS.pop(chat_id, None)
    tasks_to_cancel = [
        t
        for t in (t_turn, t_cmp)
        if t is not None and not t.done() and t is not asyncio.current_task()
    ]
    for t in tasks_to_cancel:
        t.cancel()
    if tasks_to_cancel:
        try:
            await asyncio.wait(tasks_to_cancel, timeout=2.0)
        except Exception:
            pass
    COMPACTING_CHATS.discard(chat_id)
    COMPACTING_OWNERS.pop(chat_id, None)
    CHAT_QUEUE_PAUSED.discard(chat_id)
    ok = await database.delete_chat(chat_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Чат не найден")

    from .storage_maintenance import cleanup_deleted_chat
    await cleanup_deleted_chat(existing, shared=bool(sp))

    purge_deleted_chat_read_ts(chat_id)
    shared_members = set(sp.get("member_ids") or []) if sp else None
    await ws_manager.broadcast_for_chat(
        existing,
        {"type": "chat_deleted", "chat_id": chat_id},
        shared_member_ids=shared_members,
    )
    await ws_manager.broadcast_to_user(
        current_user["id"],
        {
            "type": "chat_read_synced",
            "chat_id": chat_id,
            "read_all": False,
            "read_ts_ms": _next_read_sync_ts_ms(),
        },
    )
    _notify_gdrive_change_for_user(current_user, "chat_deleted", is_shared=bool(sp))
    return {"ok": True}


@app.post("/api/chats/{chat_id}/cancel")
async def api_cancel_chat(
    chat_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    existing = await database.get_chat(chat_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, _ = await _get_chat_access_context(existing, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")
    CHAT_QUEUE_PAUSED.add(chat_id)
    await _broadcast_chat_queue(chat_id, existing)
    cancelled = await cancel_chat_generation(chat_id)
    return {"ok": True, "cancelled": cancelled}


async def _compact_with_fallback(*, chat: dict[str, Any], messages: list[dict[str, Any]],
                                 primary_model: str, fallback_models: Optional[list[str]] = None,
                                 user: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    tried_buckets: set[str] = set()
    for candidate in dict.fromkeys([primary_model, *(fallback_models or [])]):
        if not candidate:
            continue
        if is_claude_code_model(candidate):
            engine, bucket = "claude", "claude"
        elif is_codex_model(candidate):
            engine, bucket = "codex", "codex"
        else:
            engine = "agy"
            bucket = f"agy:{classify_model_family(candidate)}"
        if bucket in tried_buckets or (engine == "agy" and "agy:*" in tried_buckets):
            continue
        blocked = await _enforce_user_turn_quotas(user, candidate)
        if blocked:
            reasons.append(blocked)
            continue
        tried_buckets.add(bucket)
        try:
            result = await compact_chat_context(chat=chat, messages=messages, model=candidate, user_id=user["id"])
            if not isinstance(result, dict) or not str(result.get("summary") or "").strip():
                raise ValueError("Пустая сводка")
            return {**result, "model": str(result.get("model") or candidate)}
        except Exception as exc:
            if engine == "agy" and (
                isinstance(exc, AgyClientUnavailableError) or "недоступен" in str(exc).lower()
            ):
                tried_buckets.add("agy:*")
            # Do not expose CLI output, local paths or OAuth credentials in API errors.
            reasons.append(f"{engine}: клиент недоступен, лимит подписки или пустой ответ")
    raise HTTPException(status_code=503, detail="Сжатие пока недоступно. " + "; ".join(dict.fromkeys(reasons)))


async def _execute_chat_compaction(
    chat_id: str,
    current_user: dict[str, Any],
    *,
    from_queue: bool = False,
) -> dict[str, Any]:
    comp_token = _try_acquire_compaction(chat_id, allow_current_turn_task=from_queue)
    if not comp_token:
        raise HTTPException(
            status_code=409,
            detail="Сжатие или генерация в этом диалоге уже выполняется",
        )

    chat: Optional[dict[str, Any]] = None
    shared_members: Optional[set[str]] = None
    broadcasted_active = False
    try:
        chat = await database.get_chat(chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Чат не найден")
        allowed, sp = await _get_chat_access_context(chat, current_user)
        if not allowed:
            raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

        messages = await database.get_messages(chat_id, include_thinking=False)
        chat["is_generating"] = False
        before_stats = compute_chat_context_stats(chat, messages)
        if not before_stats.get("can_compact"):
            raise HTTPException(
                status_code=400,
                detail="Контекст уже сжат или диалог ещё слишком короткий для сжатия",
            )

        is_admin = current_user.get("role") == "admin"
        all_models = await fetch_available_models()
        allowed_models = await _get_models_for_user(all_models, current_user)
        allowed_ids = {m["id"] for m in allowed_models}
        active_cgpt = await database.get_active_user_chatgpt_account(
            current_user["id"], require_enabled=True
        )
        chat_model_str = str(chat.get("model") or "")

        comp_model = ""
        active_claude_c = await database.get_active_claude_account(current_user["id"], require_enabled=True)
        if is_claude_code_model(chat_model_str) and active_claude_c:
            comp_model = claude_engine.pick_fast_claude_utility_model(active_claude_c, chat_model_str)
        elif is_codex_model(chat_model_str) and active_cgpt:
            comp_model = codex_engine.pick_fast_codex_utility_model(active_cgpt, chat_model_str)
        else:
            preferred_summarizers = [
                "gemini-4-carbon-medium",
                "gemini-4-argon-medium",
                "gemini-3.8-flash-high",
                "gemini-4-carbon-low",
                "gemini-4-argon-low",
                "gemini-3.8-flash-medium",
                "claude-sonnet-5-5-medium",
            ]
            comp_model = next(
                (mid for mid in preferred_summarizers if not allowed_ids or mid in allowed_ids),
                "",
            )
            if not comp_model:
                dyn_candidates = [
                    str(m.get("id") or "")
                    for m in allowed_models
                    if classify_model_family(str(m.get("id") or "")) == "gemini"
                    and any(
                        k in str(m.get("id") or "").lower()
                        for k in ("medium", "low", "flash")
                    )
                ]
                comp_model = (
                    dyn_candidates[0]
                    if dyn_candidates
                    else resolve_active_model_id(
                        chat_model_str,
                        allowed_models,
                        cli_online=bool(get_cli_auth_status().get("ok", True)),
                    )
                )

        fallback_models = [str(m.get("id") or "") for m in allowed_models
                           if classify_model_family(str(m.get("id") or "")) == "gemini"
                           and not is_codex_model(str(m.get("id") or ""))
                           and not is_claude_code_model(str(m.get("id") or ""))]
        fallback_models.sort(
            key=lambda mid: (
                0 if "flash" in mid.lower() else 1 if "low" in mid.lower() else 2 if "medium" in mid.lower() else 3,
                mid,
            )
        )
        if active_claude_c:
            fallback_models.append(claude_engine.pick_fast_claude_utility_model(active_claude_c, "claude-code:haiku@low"))
        if active_cgpt:
            fallback_models.append(codex_engine.pick_fast_codex_utility_model(active_cgpt, "codex:gpt-5.4-mini@low"))

        shared_members = set(sp.get("member_ids") or []) if sp else None
        broadcasted_active = True
        await ws_manager.broadcast_for_chat(
            chat,
            {"type": "chat_compacting", "chat_id": chat_id, "active": True},
            shared_member_ids=shared_members,
        )

        comp_res = await _compact_with_fallback(
            chat=chat, messages=messages, primary_model=comp_model,
            fallback_models=fallback_models, user=current_user,
        )
        summary_text = comp_res["summary"]

        # Preview post-compaction stats so checkpoint badge records exact before -> after numbers
        preview_chat = {
            **chat,
            "agy_conversation_id": "",
            "codex_thread_id": "",
            "codex_account_id": "",
            "claude_session_id": "",
            "claude_account_id": "",
            "compacted_summary": summary_text,
            "compacted_up_to_msg_id": 10**12,
            "compacted_count": int(chat.get("compacted_count") or 0) + 1,
            "is_generating": False,
        }
        after_preview = compute_chat_context_stats(preview_chat, messages)
        before_tok = int(before_stats["used_tokens"])
        after_tok = int(after_preview["used_tokens"])
        saved_pct = (
            max(0, int(round((before_tok - after_tok) / max(1, before_tok) * 100)))
            if before_tok > after_tok
            else 0
        )

        if not await database.get_chat(chat_id):
            raise HTTPException(status_code=404, detail="Чат не найден")

        checkpoint_msg = await database.add_message(
            chat_id,
            user_id=current_user["id"],
            role="system",
            content=summary_text,
            model=str(comp_res.get("model") or comp_model),
            usage={
                "before_tokens": before_tok,
                "after_tokens": after_tok,
                "saved_pct": saved_pct,
                "compacted_messages": int(before_stats.get("active_messages_count") or 0),
            },
            status="compacted",
        )

        updated_chat = await database.save_chat_compaction(
            chat_id,
            compacted_summary=summary_text,
            compacted_up_to_msg_id=int(checkpoint_msg["id"]),
            compacted_count=int(chat.get("compacted_count") or 0) + 1,
        )
        if not updated_chat:
            raise HTTPException(status_code=404, detail="Чат не найден")

        all_msgs = messages + [checkpoint_msg]
        updated_chat["is_generating"] = False
        updated_chat["is_compacting"] = False
        updated_chat["is_shared_project"] = sp is not None
        final_stats = compute_chat_context_stats(updated_chat, all_msgs)
        updated_chat["context_stats"] = final_stats
        updated_chat["context_pct"] = float(final_stats.get("pct") or 0.0)
        updated_chat["context_used_tokens"] = int(final_stats.get("used_tokens") or 0)
        updated_chat.pop("compacted_summary", None)

        await ws_manager.broadcast_for_chat(
            updated_chat,
            {
                "type": "chat_compacted",
                "chat_id": chat_id,
                "chat": updated_chat,
                "message": checkpoint_msg,
                "context_stats": final_stats,
            },
            shared_member_ids=shared_members,
        )

        out_chat = dict(updated_chat)
        if not is_admin:
            out_chat.pop("project_path", None)
        return {
            "ok": True,
            "queued": False,
            "chat": out_chat,
            "message": checkpoint_msg,
            "context_stats": final_stats,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось сжать контекст: {exc}",
        )
    finally:
        released = _release_compaction(chat_id, comp_token)
        live_chat_after = await database.get_chat(chat_id) if chat is not None else None
        if released and live_chat_after is not None and broadcasted_active:
            await ws_manager.broadcast_for_chat(
                live_chat_after,
                {"type": "chat_compacting", "chat_id": chat_id, "active": False},
                shared_member_ids=shared_members,
            )
        if (
            released
            and live_chat_after is not None
            and not from_queue
            and not is_chat_generating(chat_id)
            and chat_id not in CHAT_QUEUE_PAUSED
            and CHAT_TURN_QUEUES.get(chat_id)
        ):
            q = CHAT_TURN_QUEUES.get(chat_id) or []
            chosen_item: Optional[dict[str, Any]] = None
            chosen_user: Optional[dict[str, Any]] = None
            while q:
                cand_item = q.pop(0)
                cand_uid = str(cand_item.get("user_id") or "").strip()
                cand_u = await database.get_user_by_id(cand_uid) if cand_uid else None
                if cand_u and cand_u.get("is_active", False):
                    allowed_q, _ = await _get_chat_access_context(live_chat_after, cand_u)
                    if allowed_q:
                        chosen_item = cand_item
                        chosen_user = cand_u
                        break
            if not q:
                CHAT_TURN_QUEUES.pop(chat_id, None)
            await _broadcast_chat_queue(chat_id, live_chat_after)
            if chosen_item is not None and chosen_user is not None:
                if chosen_item.get("kind") != "compact":
                    register_chat_turn(chat_id, user_id=chosen_user["id"])
                task = asyncio.create_task(
                    _run_turn_task(
                        chat_id=chat_id,
                        prompt=str(
                            chosen_item.get("prompt")
                            or "Проанализируй прикреплённые файлы."
                        ),
                        attachments=list(chosen_item.get("attachments") or []),
                        override_model=chosen_item.get("model"),
                        override_access_mode=chosen_item.get("access_mode"),
                        trigger_user=chosen_user,
                        initial_kind=str(chosen_item.get("kind") or "message"),
                        referenced_chat_ids=list(
                            chosen_item.get("referenced_chat_ids") or []
                        ),
                        referenced_chats=list(
                            chosen_item.get("referenced_chats") or []
                        ),
                        live_voice=bool(chosen_item.get("live_voice", False)),
                        live_session_id=str(chosen_item.get("live_session_id") or ""),
                        tts_voice=str(chosen_item.get("tts_voice") or ""),
                        tts_rate=str(chosen_item.get("tts_rate") or ""),
                        handoff_meta=_sanitize_handoff_meta(
                            chosen_item.get("handoff_meta")
                        ),
                        hq_return_source=_sanitize_hq_return_source(
                            chosen_item.get("hq_return_source")
                        ),
                        hq_return_sources=_sanitize_hq_return_sources(chosen_item),
                        preferred_accounts=_sanitize_turn_preferred_accounts(
                            chosen_item
                        ),
                    )
                )
                ACTIVE_TURN_TASKS[chat_id] = task


@app.post("/api/chats/{chat_id}/compact")
async def api_compact_chat(
    chat_id: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, _ = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

    if chat_id in COMPACTING_CHATS:
        raise HTTPException(
            status_code=409,
            detail="Сжатие контекста уже выполняется",
        )

    active_t = ACTIVE_TURN_TASKS.get(chat_id)
    if is_chat_generating(chat_id) or (active_t is not None and not active_t.done()):
        q = CHAT_TURN_QUEUES.setdefault(chat_id, [])
        existing = next((it for it in q if it.get("kind") == "compact"), None)
        if not existing:
            existing = {
                "id": uuid.uuid4().hex[:10],
                "chat_id": chat_id,
                "kind": "compact",
                "prompt": "Сжатие контекста без потери смысла",
                "attachments": [],
                "model": chat.get("model"),
                "access_mode": chat.get("access_mode"),
                "user_id": current_user["id"],
                "author_name": (
                    current_user.get("display_name")
                    or current_user.get("username")
                    or ""
                ),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            q.append(existing)
        CHAT_QUEUE_PAUSED.discard(chat_id)
        await _broadcast_chat_queue(chat_id, chat)
        return {"ok": True, "queued": True, "queue_item": existing}

    return await _execute_chat_compaction(chat_id, current_user, from_queue=False)


@app.post("/api/chats/{chat_id}/upload")
async def api_upload_attachment(
    chat_id: str,
    file: UploadFile = File(...),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    allowed, shared_proj = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")

    raw_name = Path(file.filename or "attachment").name
    safe_name = re.sub(r"[^\w.\-а-яА-ЯёЁ ]+", "_", raw_name).strip() or "file"
    unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"

    owner_uname = ""
    if shared_proj is None:
        owner_id = str(chat.get("owner_id") or chat.get("user_id") or current_user["id"])
        if owner_id == str(current_user.get("id") or ""):
            owner_uname = str(current_user.get("username") or "admin")
        elif owner_id == "admin":
            owner_uname = "admin"
        else:
            owner_u = await database.get_user_by_id(owner_id)
            owner_uname = str((owner_u or {}).get("username") or "admin")

    dest_dir = config.resolve_chat_uploads_dir(
        chat,
        owner_username=owner_uname,
        is_shared_project=bool(shared_proj is not None),
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / unique_name

    data = await file.read()
    if len(data) > 35 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Файл превышает лимит 35 МБ")
    dest_path.write_bytes(data)

    mime = file.content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    return {
        "attachment": {
            "name": safe_name,
            "stored_name": unique_name,
            "mime_type": mime,
            "size": len(data),
            "url": f"/api/uploads/{chat_id}/{unique_name}",
            "disk_path": str(dest_path),
        }
    }


_EXT_MIME_OVERRIDES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".pdf": "application/pdf",
}


def _is_within_dir(child: Path, parent: Path) -> bool:
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


async def _resolve_media_request_user(
    request: Request,
    token: Optional[str] = None,
) -> dict[str, Any]:
    tok = auth.extract_request_token(request) or (token or "").strip()
    user = await auth.resolve_token_user(tok)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user


@app.get("/api/uploads/{chat_id}/{filename}")
async def api_serve_upload(
    chat_id: str,
    filename: str,
    request: Request,
    token: Optional[str] = Query(None),
    download: int = Query(0),
):
    current_user = await _resolve_media_request_user(request, token)
    if "/" in chat_id or "\\" in chat_id or ".." in chat_id:
        raise HTTPException(status_code=400, detail="Некорректный ID")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Некорректное имя файла")

    chat = await database.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    allowed, shared_proj = await _get_chat_access_context(chat, current_user)
    if not allowed:
        raise HTTPException(status_code=403, detail="Доступ к вложению запрещён")

    owner_uname = ""
    owner_id = str(chat.get("owner_id") or chat.get("user_id") or "")
    if owner_id == str(current_user.get("id") or ""):
        owner_uname = str(current_user.get("username") or "")
    elif owner_id == "admin":
        owner_uname = "admin"
    elif owner_id:
        owner_u = await database.get_user_by_id(owner_id)
        owner_uname = str((owner_u or {}).get("username") or "")

    target = config.find_existing_chat_upload_file(
        chat_id,
        filename,
        chat=chat,
        owner_username=owner_uname,
        is_shared_project=bool(shared_proj is not None),
    )
    if target is None or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    if download:
        return FileResponse(
            target,
            filename=target.name,
            content_disposition_type="attachment",
        )
    # Upload filenames are unique (uuid[:8]_name) and never rewritten in place,
    # so the browser may cache inline previews forever (instant tab re-open).
    return FileResponse(
        target,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/media")
async def api_serve_generated_media(
    request: Request,
    path: str = Query(..., min_length=1, max_length=2048),
    chat_id: Optional[str] = Query(None, max_length=128),
    download: int = Query(0),
    token: Optional[str] = Query(None),
):
    """Securely proxy AI-generated media & workspace artifacts (images, video, audio, PDF) with RBAC."""
    current_user = await _resolve_media_request_user(request, token)
    is_admin = current_user.get("role") == "admin"

    raw = urllib.parse.unquote(path.strip())
    if "\x00" in raw:
        raise HTTPException(status_code=400, detail="Некорректный путь")

    # Strip anchor (#L10-L20, etc.) and file:// query remnants
    raw = raw.split("#", 1)[0].strip()
    if raw.lower().startswith("file:"):
        raw = raw.split("?", 1)[0].strip()

    if raw.lower().startswith("file://"):
        raw = raw[7:]
        if re.match(r"^/+[a-zA-Z]:[\\/]", raw):
            raw = raw.lstrip("/")
    elif re.match(r"^/+[a-zA-Z]:[\\/]", raw):
        raw = raw.lstrip("/")

    if not raw or raw.startswith("\\\\") or raw.startswith("//"):
        raise HTTPException(status_code=400, detail="Сетевые и пустые пути запрещены")

    chat_obj: Optional[dict[str, Any]] = None
    resolved_chat_proj_dir: Optional[Path] = None
    if chat_id:
        chat_obj = await database.get_chat(chat_id)
        if chat_obj:
            allowed_chat, _ = await _get_chat_access_context(chat_obj, current_user)
            if not allowed_chat:
                raise HTTPException(status_code=403, detail="Нет доступа к этому диалогу")
            if chat_obj.get("kind") == "project" and chat_obj.get("project_name"):
                shared_projs = await database.list_shared_projects(
                    for_user_id=current_user["id"], is_admin=is_admin
                )
                proj_info = brain_reader.resolve_user_project(
                    chat_obj["project_name"], current_user, shared_projs
                )
                if proj_info and proj_info.get("path"):
                    resolved_chat_proj_dir = Path(proj_info["path"]).resolve()

    # Translate sandbox virtual /workspace/... paths to the active project or user workspace
    norm_slashes = raw.replace("\\", "/")
    if norm_slashes == "/workspace" or norm_slashes.startswith("/workspace/"):
        rel_ws = norm_slashes[len("/workspace") :].lstrip("/")
        base_ws = resolved_chat_proj_dir
        if base_ws is None and current_user.get("workspace_dir"):
            base_ws = Path(current_user["workspace_dir"]).resolve()
        if base_ws is None:
            raise HTTPException(status_code=404, detail="Рабочая папка не найдена")
        target = (base_ws / rel_ws).resolve()
        if not _is_within_dir(target, base_ws):
            raise HTTPException(
                status_code=403,
                detail="Выход за пределы рабочей папки запрещён",
            )
    else:
        try:
            target = Path(raw).resolve()
        except OSError:
            raise HTTPException(status_code=400, detail="Некорректный путь к файлу")

    # 1. Hard Sensitive Denylist (always enforced, even for Admin)
    forbidden_dirs = (
        (config.DATA_DIR / "ssh").resolve(),
        config.CERTS_DIR.resolve(),
        config.SESSIONS_DIR.resolve(),
        config.CLI_HOMES_DIR.resolve(),
        (config.DATA_DIR / "host_services").resolve(),
        (config.DATA_DIR / "tools").resolve(),
        (config.DATA_DIR / "models").resolve(),
    )
    if any(_is_within_dir(target, fd) for fd in forbidden_dirs):
        raise HTTPException(status_code=403, detail="Доступ к системным каталогам запрещён")

    name_low = target.name.lower()
    suffix_low = target.suffix.lower()
    parts_low = {p.lower() for p in target.parts}
    if (
        name_low == ".env"
        or name_low.startswith(".env.")
        or name_low == ".secret_key"
        or name_low in {
            ".credentials.json",
            "credentials.json",
            "auth.json",
            "mcp_config.json",
            "mcp_config.docker.json",
            "mcp_hub_runtime.json",
        }
        or suffix_low
        in {
            ".db",
            ".sqlite",
            ".sqlite3",
            ".db-wal",
            ".db-shm",
            ".pem",
            ".key",
            ".pfx",
            ".p12",
            ".kdbx",
        }
        or "oauth-token" in name_low
        or "hub_tunnel" in name_low
        or "id_rsa" in name_low
        or "id_ed25519" in name_low
        or ".git" in parts_low
    ):
        raise HTTPException(
            status_code=403,
            detail="Доступ к секретным и системным файлам запрещён",
        )

    # 2. Strict RBAC Allowed-Roots Enforcement
    cli_brain_root = (config.AGY_CLI_DATA_DIR / "brain").resolve()
    cli_brain_roots: list[Path] = [cli_brain_root]
    agy_accs_dir = config.CLI_HOMES_DIR / "_agy_accounts"
    if agy_accs_dir.is_dir():
        for acc_b in agy_accs_dir.glob("*/antigravity-cli/brain"):
            try:
                cli_brain_roots.append(acc_b.resolve())
            except OSError:
                pass
    app_brain_root = (config.AGY_APP_DATA_DIR / "brain").resolve()
    legacy_uploads_root = config.get_effective_legacy_uploads_dir()
    shared_uploads_root = (config.SHARED_PROJECTS_DIR.resolve() / "_uploads").resolve()
    shared_context_root = (config.SHARED_PROJECTS_DIR.resolve() / "_context").resolve()
    eff_users_root = config.get_effective_users_dir()
    base_dir_res = config.BASE_DIR.resolve()
    data_dir_res = config.DATA_DIR.resolve()

    def _extract_upload_cid(cand_target: Path) -> Optional[str]:
        if _is_within_dir(cand_target, legacy_uploads_root):
            rp = cand_target.relative_to(legacy_uploads_root).parts
            return rp[0] if len(rp) >= 2 else ""
        if _is_within_dir(cand_target, shared_uploads_root):
            rp = cand_target.relative_to(shared_uploads_root).parts
            return rp[0] if len(rp) >= 2 else ""
        if _is_within_dir(cand_target, eff_users_root):
            rp = cand_target.relative_to(eff_users_root).parts
            if len(rp) >= 4 and rp[1].lower() == "uploads":
                return rp[2]
            if len(rp) == 2 and rp[1].lower() == "uploads":
                return ""
        return None

    detected_up_cid = _extract_upload_cid(target)
    matched_cli_brain = next((br for br in cli_brain_roots if _is_within_dir(target, br)), None)

    if matched_cli_brain is not None or _is_within_dir(target, app_brain_root):
        in_cli_brain = matched_cli_brain is not None
        active_brain = matched_cli_brain if matched_cli_brain is not None else app_brain_root
        rel_parts = target.relative_to(active_brain).parts
        if len(rel_parts) < 2:
            raise HTTPException(status_code=403, detail="Доступ запрещён")
        if "logs" in {p.lower() for p in rel_parts} and suffix_low in {".jsonl", ".log"}:
            raise HTTPException(status_code=403, detail="Доступ к сырым логам запрещён")

        if not is_admin:
            if not in_cli_brain or ".system_generated" in rel_parts or not chat_obj:
                raise HTTPException(status_code=403, detail="Доступ к артефакту запрещён")
            brain_cid = rel_parts[0]
            direct_match = brain_cid and brain_cid in {
                chat_obj.get("agy_conversation_id"),
                chat_obj.get("source_desktop_id"),
            }
            if not direct_match:
                ref_ok = await database.chat_references_brain_id(chat_obj["id"], brain_cid)
                if not ref_ok:
                    raise HTTPException(
                        status_code=403,
                        detail="Медиафайл не принадлежит этому диалогу",
                    )

    elif detected_up_cid is not None:
        if not detected_up_cid:
            raise HTTPException(status_code=403, detail="Доступ запрещён")
        up_chat = await database.get_chat(detected_up_cid)
        if not up_chat:
            raise HTTPException(status_code=404, detail="Диалог вложения не найден")
        allowed_up, up_sp = await _get_chat_access_context(up_chat, current_user)
        if not allowed_up:
            raise HTTPException(status_code=403, detail="Доступ к вложению запрещён")
        if not target.exists():
            alt_target = config.find_existing_chat_upload_file(
                detected_up_cid,
                target.name,
                chat=up_chat,
                is_shared_project=bool(up_sp is not None),
            )
            if alt_target is not None:
                target = alt_target

    else:
        # Hard block `DATA_DIR` (except legacy test roots when tests place workspaces/shared_projects inside tmp data_dir)
        legacy_test_safe_roots = tuple(
            r
            for r in (
                config.CHAT_SANDBOX_DIR.resolve(),
                eff_users_root,
                config.SHARED_PROJECTS_DIR.resolve(),
            )
            if r != data_dir_res
        )
        if _is_within_dir(target, data_dir_res):
            in_legacy_test_ws = any(
                _is_within_dir(sr, data_dir_res) and _is_within_dir(target, sr)
                for sr in legacy_test_safe_roots
            )
            if not in_legacy_test_ws and not (
                resolved_chat_proj_dir is not None
                and resolved_chat_proj_dir != data_dir_res
                and _is_within_dir(resolved_chat_proj_dir, data_dir_res)
                and _is_within_dir(target, resolved_chat_proj_dir)
            ):
                raise HTTPException(status_code=403, detail="Доступ к данным сервера запрещён")

        if is_admin:
            allowed_admin_roots = (
                base_dir_res,
                config.PROJECTS_ROOT.resolve(),
                config.SHARED_PROJECTS_DIR.resolve(),
                eff_users_root,
                config.CHAT_SANDBOX_DIR.resolve(),
            )
            allowed_admin = any(_is_within_dir(target, ar) for ar in allowed_admin_roots)
            if not allowed_admin and resolved_chat_proj_dir is not None:
                allowed_admin = _is_within_dir(target, resolved_chat_proj_dir)
            if not allowed_admin:
                raise HTTPException(
                    status_code=403,
                    detail="Путь находится вне разрешённых рабочих директорий",
                )
        else:
            if _is_within_dir(target, shared_context_root):
                raise HTTPException(status_code=403, detail="Доступ к служебному контексту запрещён")

            own_slug = config.sanitize_user_folder_slug(current_user)
            own_user_root = (eff_users_root / own_slug).resolve()
            allowed_guest_roots: list[Path] = [
                (own_user_root / "projects").resolve(),
                (own_user_root / "chats").resolve(),
                (own_user_root / "uploads").resolve(),
            ]
            if current_user.get("workspace_dir"):
                ws_cfg = Path(current_user["workspace_dir"]).resolve()
                if ws_cfg != eff_users_root and ws_cfg != base_dir_res:
                    allowed_guest_roots.append(ws_cfg)
            uname_safe = re.sub(
                r"[^a-zA-Z0-9_\-]+", "_", str(current_user.get("username") or "guest")
            )
            allowed_guest_roots.append((config.CHAT_SANDBOX_DIR / uname_safe).resolve())
            if resolved_chat_proj_dir is not None:
                allowed_guest_roots.append(resolved_chat_proj_dir)
            else:
                guest_sps = await database.list_shared_projects(
                    for_user_id=current_user["id"], is_admin=False
                )
                for gsp in guest_sps:
                    try:
                        gsp_p = brain_reader.validate_shared_project_path(
                            gsp["name"], existing_path=str(gsp.get("path") or "")
                        )
                        allowed_guest_roots.append(gsp_p)
                    except ValueError:
                        continue

            if not any(_is_within_dir(target, gr) for gr in allowed_guest_roots):
                raise HTTPException(status_code=403, detail="Доступ к файлу запрещён")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Медиафайл не найден на диске")

    mime = (
        _EXT_MIME_OVERRIDES.get(suffix_low)
        or mimetypes.guess_type(target.name)[0]
        or "application/octet-stream"
    )
    headers: dict[str, str] = {
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }

    force_attachment = bool(download)
    if suffix_low in {".html", ".htm", ".xhtml", ".xml"}:
        # Never execute arbitrary HTML documents inline on Hub's origin
        force_attachment = True
    elif suffix_low == ".svg":
        headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"
        )

    if force_attachment:
        return FileResponse(
            target,
            media_type=mime,
            filename=target.name,
            content_disposition_type="attachment",
            headers=headers,
        )
    return FileResponse(target, media_type=mime, headers=headers)


# ---------------------------------------------------------------------------
# Projects & Desktop Antigravity Brain Integration
# ---------------------------------------------------------------------------


async def _resolve_project_memory_target(
    project_name: str,
    current_user: dict[str, Any],
) -> tuple[dict[str, Any], str, bool]:
    is_admin = current_user.get("role") == "admin"
    shared_projs = await database.list_shared_projects(
        for_user_id=current_user["id"], is_admin=is_admin
    )
    resolved = brain_reader.resolve_user_project(project_name, current_user, shared_projs)
    if not resolved:
        raise HTTPException(
            status_code=404,
            detail="Папка проекта не найдена или доступ запрещён",
        )
    is_shared = bool(resolved.get("is_shared"))
    scope_key = database.build_project_memory_scope_key(
        resolved["name"],
        current_user["id"],
        is_shared=is_shared,
    )
    return resolved, scope_key, is_shared


async def _broadcast_project_memory_update(
    resolved_proj: dict[str, Any],
    owner_id: str,
    memory: dict[str, Any],
) -> None:
    payload = {
        "type": "project_memory_updated",
        "project_name": resolved_proj["name"],
        "has_passport": bool(memory.get("has_passport")),
        "changelog_count": int(memory.get("changelog_count") or 0),
        "disable_default_guardrails": bool(memory.get("disable_default_guardrails")),
        "updated_at": str(memory.get("updated_at") or ""),
    }
    if resolved_proj.get("is_shared"):
        member_ids = set(resolved_proj.get("member_ids") or [])
        await ws_manager._send_to_matching(
            payload,
            lambda u: u.get("role") == "admin" or u.get("id") in member_ids,
        )
    else:
        await ws_manager.broadcast_user(owner_id, payload)


def _empty_processes_summary() -> dict[str, Any]:
    return {
        "total": 0,
        "running_count": 0,
        "crashed_count": 0,
        "unseen_crash_count": 0,
        "completed_count": 0,
        "stopped_count": 0,
        "ports": [],
    }


def _summarize_processes_list(services: list[dict[str, Any]]) -> dict[str, Any]:
    items = services or []
    running = [s for s in items if s.get("alive") or s.get("status") == "running"]
    crashed = [s for s in items if not s.get("alive") and s.get("status") == "crashed"]
    unseen_crashed = [s for s in crashed if bool(s.get("crash_unseen"))]
    completed = [s for s in items if not s.get("alive") and s.get("status") == "completed"]
    stopped = [
        s
        for s in items
        if not s.get("alive") and s.get("status") not in ("running", "crashed", "completed")
    ]
    ports = [
        int(s["port"])
        for s in running
        if isinstance(s.get("port"), int) and int(s["port"]) > 0
    ]
    return {
        "total": len(items),
        "running_count": len(running),
        "crashed_count": len(crashed),
        "unseen_crash_count": len(unseen_crashed),
        "completed_count": len(completed),
        "stopped_count": len(stopped),
        "ports": ports,
    }


_summarize_host_services_list = _summarize_processes_list


async def broadcast_project_processes_update(
    project_name: str,
    project_dir: Optional[Path | str] = None,
    *,
    transition_event: Optional[dict[str, Any]] = None,
    docker_user_id: Optional[str] = None,
    send_list_update: bool = True,
) -> None:
    clean_proj = (project_name or "").strip()
    if not clean_proj or not ws_manager.active:
        return

    eff_dir: Optional[Path] = None
    if project_dir is not None and str(project_dir).strip():
        try:
            eff_dir = Path(project_dir).resolve()
        except Exception:
            eff_dir = None

    connected_by_uid: dict[str, dict[str, Any]] = {}
    for _ws, u_conn in list(ws_manager.active.items()):
        uid = str(u_conn.get("id") or "").strip()
        if uid and uid not in connected_by_uid:
            connected_by_uid[uid] = u_conn
    if not connected_by_uid:
        return

    if docker_user_id:
        uid_target = str(docker_user_id).strip()
        u_conn = connected_by_uid.get(uid_target)
        if not u_conn or eff_dir is None:
            return
        if not sandbox_service.is_user_sandbox_eligible(u_conn):
            return
        try:
            u_shared = await database.list_shared_projects(
                for_user_id=uid_target, is_admin=(u_conn.get("role") == "admin")
            )
            resolved_u = brain_reader.resolve_user_project(clean_proj, u_conn, u_shared)
        except Exception:
            resolved_u = None
        if not resolved_u:
            return
        u_dir = Path(resolved_u["path"]).resolve()
        if os.path.normcase(str(u_dir)) != os.path.normcase(str(eff_dir)):
            return
        if send_list_update:
            raw_svcs = await sandbox_service.list_project_sandbox_services(
                uid_target, u_dir, resolved_u["name"]
            )
            sanitized = [
                host_service_manager.sanitize_process_view_for_user(
                    s, u_conn, project_dir=u_dir
                )
                for s in raw_svcs
            ]
            summary = _summarize_processes_list(sanitized)
            await ws_manager.broadcast_user(
                uid_target,
                {
                    "type": "project_processes_updated",
                    "project_name": resolved_u["name"],
                    "execution_mode": "docker",
                    "processes": sanitized,
                    "services": sanitized,
                    "summary": summary,
                },
            )
        if transition_event and transition_event.get("transition") in ("completed", "crashed"):
            raw_svc = transition_event.get("service") or {}
            sanitized_svc = host_service_manager.sanitize_process_view_for_user(
                raw_svc, u_conn, project_dir=u_dir
            )
            safe_tail = host_service_manager.mask_sensitive_log_text(
                str(transition_event.get("error_tail") or ""),
                is_admin=(u_conn.get("role") == "admin"),
                project_dir=u_dir,
                max_lines=25,
            )
            await ws_manager.broadcast_user(
                uid_target,
                {
                    "type": "process_state_changed",
                    "project_name": resolved_u["name"],
                    "service": sanitized_svc,
                    "transition": str(transition_event["transition"]),
                    "error_tail": safe_tail,
                },
            )
        return

    # Host contour: verify per-recipient project access & exact `project_dir` match
    raw_services: list[dict[str, Any]] = []
    if send_list_update:
        raw_services = await asyncio.to_thread(
            host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
            clean_proj,
            project_dir=eff_dir,
        )
    try:
        all_shared = await database.list_shared_projects(is_admin=True)
    except Exception:
        all_shared = []

    target_norm = os.path.normcase(str(eff_dir)) if eff_dir is not None else ""

    for uid, u_conn in connected_by_uid.items():
        if not host_service_manager.is_user_host_eligible(u_conn):
            continue
        is_adm = u_conn.get("role") == "admin"
        u_shared = (
            all_shared
            if is_adm
            else [sp for sp in all_shared if uid in (sp.get("member_ids") or [])]
        )
        resolved_u = brain_reader.resolve_user_project(clean_proj, u_conn, u_shared)
        if not resolved_u:
            continue
        if brain_reader.resolve_effective_project_execution_mode(u_conn, resolved_u) != "host":
            continue
        u_dir = Path(resolved_u["path"]).resolve()
        if target_norm and os.path.normcase(str(u_dir)) != target_norm:
            continue

        if send_list_update:
            source_list = (
                raw_services
                if eff_dir is not None
                else await asyncio.to_thread(
                    host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
                    resolved_u["name"],
                    project_dir=u_dir,
                )
            )
            sanitized = [
                host_service_manager.sanitize_process_view_for_user(
                    s, u_conn, project_dir=u_dir
                )
                for s in source_list
            ]
            summary = _summarize_processes_list(sanitized)
            await ws_manager.broadcast_user(
                uid,
                {
                    "type": "project_host_services_updated",
                    "project_name": resolved_u["name"],
                    "services": sanitized,
                    "summary": summary,
                },
            )
            await ws_manager.broadcast_user(
                uid,
                {
                    "type": "project_processes_updated",
                    "project_name": resolved_u["name"],
                    "execution_mode": "host",
                    "processes": sanitized,
                    "services": sanitized,
                    "summary": summary,
                },
            )

        if transition_event and transition_event.get("transition") in ("completed", "crashed"):
            raw_svc = transition_event.get("service") or {}
            sanitized_svc = host_service_manager.sanitize_process_view_for_user(
                raw_svc, u_conn, project_dir=u_dir
            )
            safe_tail = host_service_manager.mask_sensitive_log_text(
                str(transition_event.get("error_tail") or ""),
                is_admin=is_adm,
                project_dir=u_dir,
                max_lines=25,
            )
            await ws_manager.broadcast_user(
                uid,
                {
                    "type": "process_state_changed",
                    "project_name": resolved_u["name"],
                    "service": sanitized_svc,
                    "transition": str(transition_event["transition"]),
                    "error_tail": safe_tail,
                },
            )


async def _broadcast_project_host_services_update(
    project_name: str,
    project_dir: Optional[Path] = None,
) -> None:
    await broadcast_project_processes_update(project_name, project_dir)


async def _broadcast_host_process_transition(event: dict[str, Any]) -> None:
    pname = str(event.get("project_name") or "").strip()
    if not pname:
        return
    await broadcast_project_processes_update(
        pname,
        event.get("project_dir"),
        transition_event=event,
        send_list_update=False,
    )


async def _broadcast_sandbox_processes_update(
    project_name: str,
    project_dir: Path,
    user_id: str,
) -> None:
    await broadcast_project_processes_update(
        project_name,
        project_dir,
        docker_user_id=user_id,
    )


sandbox_service.set_sandbox_processes_notifier(_broadcast_sandbox_processes_update)


@app.get("/api/projects")
async def api_list_projects(current_user: dict[str, Any] = Depends(auth.get_current_user)):
    is_admin = current_user.get("role") == "admin"
    can_host = host_service_manager.is_user_host_eligible(current_user)
    can_sandbox = sandbox_service.is_user_sandbox_eligible(current_user)
    shared_projs = await database.list_shared_projects(
        for_user_id=current_user["id"], is_admin=is_admin
    )
    shared_names = [sp["name"] for sp in shared_projs]

    projects, hub_project_chats, docker_status, saved_order = await asyncio.gather(
        asyncio.to_thread(brain_reader.list_projects_for_user, current_user, shared_projs),
        database.list_chats(
            kind="project",
            owner_id=current_user["id"],
            shared_project_names=shared_names,
            include_context_pct=False,
        ),
        check_docker_status(),
        database.get_user_projects_order(current_user["id"]),
    )
    counts: dict[str, int] = {}
    archived_counts: dict[str, int] = {}
    for ch in hub_project_chats:
        pname = (ch.get("project_name") or "").lower()
        if ch.get("is_archived"):
            archived_counts[pname] = archived_counts.get(pname, 0) + 1
        else:
            counts[pname] = counts.get(pname, 0) + 1

    scope_keys = [
        database.build_project_memory_scope_key(
            p["name"], current_user["id"], is_shared=bool(p.get("is_shared"))
        )
        for p in projects
    ]
    mem_map = await database.get_project_memories_summary_map(scope_keys)

    has_any_docker_proj = can_sandbox and bool(docker_status.get("running")) and any(
        brain_reader.resolve_effective_project_execution_mode(current_user, p) == "docker"
        for p in projects
    )
    running_containers: set[str] = (
        await sandbox_service.list_running_managed_container_names()
        if has_any_docker_proj
        else set()
    )

    # v110: резолв путей проектов и Win32-опрос host-сервисов (OpenProcess /
    # GetExitCodeProcess) выполняются ОДНИМ вызовом в пуле потоков, а не на Event Loop.
    def _resolve_projects_and_poll_host_services() -> tuple[
        dict[int, tuple[str, Optional[Path]]], dict[int, list[dict[str, Any]]]
    ]:
        resolved_map: dict[int, tuple[str, Optional[Path]]] = {}
        host_svcs: dict[int, list[dict[str, Any]]] = {}
        for idx, proj in enumerate(projects):
            mode = brain_reader.resolve_effective_project_execution_mode(current_user, proj)
            res_p = brain_reader.resolve_user_project(proj["name"], current_user, shared_projs)
            real_dir = Path(res_p["path"]).resolve() if res_p else None
            resolved_map[idx] = (mode, real_dir)
            if can_host and mode == "host" and real_dir is not None:
                host_svcs[idx] = host_service_manager.HOST_SERVICE_MANAGER.list_project_services(
                    proj["name"],
                    project_dir=real_dir,
                )
        return resolved_map, host_svcs

    resolved_by_idx, host_svcs_by_idx = await asyncio.to_thread(
        _resolve_projects_and_poll_host_services
    )

    for p_idx, p in enumerate(projects):
        p["hub_chat_count"] = counts.get(p["name"].lower(), 0)
        p["archived_chat_count"] = archived_counts.get(p["name"].lower(), 0)
        sk = database.build_project_memory_scope_key(
            p["name"], current_user["id"], is_shared=bool(p.get("is_shared"))
        )
        minfo = mem_map.get(sk) or {}
        p["has_passport"] = bool(minfo.get("has_passport", False))
        p["changelog_count"] = int(minfo.get("changelog_count") or 0)
        p["extra_dirs_count"] = int(minfo.get("extra_dirs_count") or 0) if is_admin else 0
        eff_p_mode, real_proj_dir = resolved_by_idx.get(p_idx, ("", None))
        if p_idx in host_svcs_by_idx:
            svcs = host_svcs_by_idx[p_idx]
            proc_summary = _summarize_processes_list(svcs)
        elif can_sandbox and eff_p_mode == "docker" and real_proj_dir is not None and running_containers:
            svcs = await sandbox_service.list_project_sandbox_services(
                str(current_user["id"]),
                real_proj_dir,
                p["name"],
                running_containers=running_containers,
            )
            proc_summary = _summarize_processes_list(svcs)
        else:
            proc_summary = _empty_processes_summary()
        p["processes_summary"] = proc_summary
        p["host_services_summary"] = proc_summary

    if saved_order:
        order_rank = {name.lower(): idx for idx, name in enumerate(saved_order)}
        fallback_base = len(saved_order)
        projects = [
            proj
            for _, proj in sorted(
                enumerate(projects),
                key=lambda pair: (
                    order_rank.get(pair[1]["name"].lower(), fallback_base + pair[0]),
                    pair[0],
                ),
            )
        ]

    return {
        "projects": projects,
        "docker_status": docker_status,
        "projects_order": saved_order,
        "workspace_label": f"users/{config.sanitize_user_folder_slug(current_user)}/projects",
    }


@app.post("/api/projects/reorder")
async def api_reorder_projects(
    body: ReorderProjectsRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    clean_order = await database.set_user_projects_order(
        current_user["id"], body.project_names
    )
    await ws_manager.broadcast_user(
        current_user["id"],
        {"type": "projects_reordered", "project_names": clean_order},
    )
    return {"ok": True, "project_names": clean_order}


@app.get("/api/projects/{project_name}/memory")
async def api_get_project_memory(
    project_name: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, scope_key, _ = await _resolve_project_memory_target(
        project_name, current_user
    )
    memory = await database.get_project_memory(
        scope_key,
        project_name=resolved["name"],
        owner_id=current_user["id"],
    )
    if memory.get("has_passport"):
        await asyncio.to_thread(
            database.sync_project_rules_file, resolved["path"], memory
        )
    return {
        "ok": True,
        "memory": memory,
        "parallel_runs": get_parallel_project_runs(
            project_name=resolved["name"], scope_key=scope_key
        ),
    }


@app.put("/api/projects/{project_name}/memory")
async def api_save_project_memory(
    project_name: str,
    body: SaveProjectMemoryRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, scope_key, _ = await _resolve_project_memory_target(
        project_name, current_user
    )
    memory = await database.save_project_memory(
        scope_key,
        owner_id=current_user["id"],
        project_name=resolved["name"],
        passport_overview=body.passport_overview,
        passport_rules=body.passport_rules,
        passport_file_map=body.passport_file_map,
        changelog=body.changelog,
        disable_default_guardrails=body.disable_default_guardrails,
    )
    await asyncio.to_thread(
        database.sync_project_rules_file, resolved["path"], memory
    )
    await _broadcast_project_memory_update(resolved, current_user["id"], memory)
    _notify_gdrive_change_for_user(
        current_user, "project_memory", is_shared=bool(resolved.get("is_shared"))
    )
    return {"ok": True, "memory": memory}


@app.post("/api/projects/{project_name}/memory/generate")
async def api_generate_project_memory(
    project_name: str,
    body: Optional[GenerateProjectMemoryRequest] = None,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, scope_key, is_shared = await _resolve_project_memory_target(
        project_name, current_user
    )
    pref_model = (body.preferred_model if body else "") or ""
    active_cgpt = await database.get_active_user_chatgpt_account(
        current_user["id"], require_enabled=True
    )
    active_claude = await database.get_active_claude_account(
        current_user["id"], require_enabled=True
    )
    check_model = pref_model or "gemini-4-carbon-low"
    quota_block = await _enforce_user_turn_quotas(current_user, check_model)
    if quota_block and active_cgpt and not is_codex_model(check_model) and not is_claude_code_model(check_model):
        pref_model = codex_engine.pick_fast_codex_utility_model(active_cgpt, pref_model)
        quota_block = await _enforce_user_turn_quotas(current_user, pref_model)
    if quota_block and active_claude and not is_claude_code_model(pref_model or check_model):
        pref_model = claude_engine.pick_fast_claude_utility_model(active_claude, pref_model)
        quota_block = await _enforce_user_turn_quotas(current_user, pref_model)
    if quota_block:
        raise HTTPException(status_code=429, detail=quota_block)

    cur_mem = await database.get_project_memory(
        scope_key,
        project_name=resolved["name"],
        owner_id=current_user["id"],
    )
    need_starter_cl = len(cur_mem.get("changelog") or []) == 0
    recent_chats = await database.get_recent_project_chats_for_passport(
        resolved["name"],
        owner_id=current_user["id"],
        is_shared=is_shared,
        limit=5,
    )

    try:
        gen = await generate_project_passport_via_cli(
            project_name=resolved["name"],
            project_dir=resolved["path"],
            recent_chats=recent_chats,
            need_starter_changelog=need_starter_cl,
            preferred_model=pref_model,
            user_id=current_user["id"],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось автоматически собрать паспорт проекта: {exc}",
        )

    new_cl = gen.get("starter_changelog") if need_starter_cl else None
    saved_mem = await database.save_project_memory(
        scope_key,
        owner_id=current_user["id"],
        project_name=resolved["name"],
        passport_overview=gen.get("passport_overview", ""),
        passport_rules=gen.get("passport_rules", ""),
        passport_file_map=gen.get("passport_file_map", ""),
        changelog=new_cl if new_cl else None,
    )
    await asyncio.to_thread(
        database.sync_project_rules_file, resolved["path"], saved_mem
    )
    await _broadcast_project_memory_update(resolved, current_user["id"], saved_mem)
    _notify_gdrive_change_for_user(
        current_user, "project_memory_generate", is_shared=bool(resolved.get("is_shared"))
    )
    return {"ok": True, "memory": saved_mem, "model": gen.get("model", "")}


@app.post("/api/projects/{project_name}/memory/pin-note")
async def api_pin_project_memory_note(
    project_name: str,
    body: PinProjectMemoryNoteRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    clean_note = (body.note or "").strip()
    if not clean_note:
        raise HTTPException(status_code=400, detail="Текст заметки не может быть пустым")
    resolved, scope_key, _ = await _resolve_project_memory_target(
        project_name, current_user
    )
    target = "rules" if body.target == "rules" else "changelog"
    saved_mem = await database.pin_project_memory_note(
        scope_key,
        owner_id=current_user["id"],
        project_name=resolved["name"],
        note=clean_note,
        target=target,
        chat_id=body.chat_id,
        chat_title=body.chat_title,
    )
    if target == "rules":
        await asyncio.to_thread(
            database.sync_project_rules_file, resolved["path"], saved_mem
        )
    await _broadcast_project_memory_update(resolved, current_user["id"], saved_mem)
    _notify_gdrive_change_for_user(
        current_user, "project_memory_pin", is_shared=bool(resolved.get("is_shared"))
    )
    return {"ok": True, "memory": saved_mem, "target": target}


@app.get("/api/projects/{project_name}/sandbox/status")
async def api_get_project_sandbox_status(
    project_name: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    shared_projs = await database.list_shared_projects(
        for_user_id=current_user["id"], is_admin=is_admin
    )
    resolved = brain_reader.resolve_user_project(project_name, current_user, shared_projs)
    if not resolved:
        raise HTTPException(
            status_code=404,
            detail="Папка проекта не найдена или доступ запрещён",
        )

    eff_exec_mode = brain_reader.resolve_effective_project_execution_mode(
        current_user, resolved
    )
    is_docker_contour = eff_exec_mode == "docker"
    status = await sandbox_service.get_project_sandbox_status(
        current_user["id"],
        Path(resolved["path"]),
        resolved["name"],
    )
    return {
        "ok": True,
        "project_name": resolved["name"],
        "docker_contour": is_docker_contour,
        **status,
    }


@app.post("/api/projects/{project_name}/sandbox/reset")
async def api_reset_project_sandbox(
    project_name: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    shared_projs = await database.list_shared_projects(
        for_user_id=current_user["id"], is_admin=is_admin
    )
    resolved = brain_reader.resolve_user_project(project_name, current_user, shared_projs)
    if not resolved:
        raise HTTPException(
            status_code=404,
            detail="Папка проекта не найдена или доступ запрещён",
        )

    if not sandbox_service.is_user_sandbox_eligible(current_user):
        raise HTTPException(
            status_code=403,
            detail="Docker-песочница недоступна для вашей роли или выбранного проекта",
        )

    eff_exec_mode = brain_reader.resolve_effective_project_execution_mode(
        current_user, resolved
    )
    if eff_exec_mode == "host":
        raise HTTPException(
            status_code=400,
            detail="В этом проекте используется прямой терминал ПК (Windows Host), а не Docker-песочница",
        )
    if eff_exec_mode != "docker":
        raise HTTPException(
            status_code=403,
            detail="Docker-песочница отключена для выбранного проекта",
        )

    docker_info = await check_docker_status()
    if not docker_info.get("sandbox_mcp_ready"):
        raise HTTPException(
            status_code=503,
            detail=docker_info.get("summary") or "Docker-образ песочницы не готов",
        )

    try:
        res = await sandbox_service.reset_project_sandbox(
            current_user["id"],
            Path(resolved["path"]),
            resolved["name"],
            sandbox_network=str(current_user.get("sandbox_network") or "internet"),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось перезапустить Docker-песочницу: {exc}",
        )
    await broadcast_project_processes_update(
        resolved["name"],
        Path(resolved["path"]).resolve(),
        docker_user_id=str(current_user["id"]),
    )
    return {"ok": True, **res}


async def _resolve_host_project_for_user(
    project_name: str,
    current_user: dict[str, Any],
) -> tuple[dict[str, Any], str, bool]:
    if not host_service_manager.is_user_host_eligible(current_user):
        raise HTTPException(
            status_code=403,
            detail="Управление фоновыми сервисами ПК доступно только при включённом праве прямого терминала (Host)",
        )
    resolved, scope_key, is_shared = await _resolve_project_memory_target(
        project_name, current_user
    )
    eff_exec_mode = brain_reader.resolve_effective_project_execution_mode(
        current_user, resolved
    )
    if eff_exec_mode != "host":
        raise HTTPException(
            status_code=400,
            detail="Этот проект работает в изолированном Docker-контуре, а не на прямом терминале ПК (Host)",
        )
    return resolved, scope_key, is_shared


@app.get("/api/projects/{project_name}/settings")
async def api_get_project_settings(
    project_name: str,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    resolved, scope_key, _ = await _resolve_project_memory_target(
        project_name, admin_user
    )
    eff_exec_mode = brain_reader.resolve_effective_project_execution_mode(
        admin_user, resolved
    )
    mem = await database.get_project_memory(
        scope_key, project_name=resolved["name"], owner_id=admin_user["id"]
    )
    proj_dir = Path(resolved["path"]).resolve()
    health = await asyncio.to_thread(brain_reader.inspect_project_dir_health, proj_dir)
    services = (
        await asyncio.to_thread(
            host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
            resolved["name"],
            project_dir=proj_dir,
        )
        if eff_exec_mode == "host"
        else []
    )
    return {
        "ok": True,
        "project_name": resolved["name"],
        "project_path": str(proj_dir),
        "execution_mode": eff_exec_mode,
        "is_empty_skeleton": bool(health["is_empty_skeleton"]),
        "real_entries_count": int(health["real_entries_count"]),
        "extra_dirs": list(mem.get("extra_dirs") or []),
        "migrated_from_paths": list(mem.get("migrated_from_paths") or []),
        "disable_default_guardrails": bool(mem.get("disable_default_guardrails")),
        "host_services": services,
        "host_services_summary": _summarize_host_services_list(services),
    }


@app.put("/api/projects/{project_name}/settings")
async def api_update_project_settings(
    project_name: str,
    body: UpdateProjectSettingsRequest,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    resolved, scope_key, _ = await _resolve_project_memory_target(
        project_name, admin_user
    )
    eff_exec_mode = brain_reader.resolve_effective_project_execution_mode(
        admin_user, resolved
    )
    proj_dir = Path(resolved["path"])
    try:
        validated_dirs = await asyncio.to_thread(
            brain_reader.validate_project_extra_dirs,
            body.extra_dirs,
            user=admin_user,
            exec_mode=eff_exec_mode,
            project_dir=proj_dir,
            strict=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    saved_mem = await database.save_project_memory(
        scope_key,
        owner_id=admin_user["id"],
        project_name=resolved["name"],
        extra_dirs=[str(p) for p in validated_dirs],
        disable_default_guardrails=body.disable_default_guardrails,
    )
    await _broadcast_project_memory_update(resolved, admin_user["id"], saved_mem)
    return {
        "ok": True,
        "project_name": resolved["name"],
        "extra_dirs": list(saved_mem.get("extra_dirs") or []),
        "migrated_from_paths": list(saved_mem.get("migrated_from_paths") or []),
        "disable_default_guardrails": bool(saved_mem.get("disable_default_guardrails")),
    }


async def _is_gdrive_env_vault_active_for_user(user_id: str) -> bool:
    try:
        gd_cfg = await database.get_gdrive_config(user_id, include_secrets=False)
        if not isinstance(gd_cfg, dict):
            return False
        connected = bool(gd_cfg.get("connected") or gd_cfg.get("is_connected"))
        return bool(connected and gd_cfg.get("include_env_vault"))
    except Exception:
        return False


async def _build_project_env_response(
    resolved: dict[str, Any],
    admin_user: dict[str, Any],
    rel_file: str = ".env",
) -> dict[str, Any]:
    proj_dir = Path(resolved["path"]).resolve()
    vault_active = await _is_gdrive_env_vault_active_for_user(admin_user["id"])
    try:
        env_state = await asyncio.to_thread(
            env_service.read_project_env_state,
            proj_dir,
            rel_file or ".env",
            gdrive_vault_active=vault_active,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "project_name": resolved["name"],
        **env_state,
    }


@app.get("/api/projects/{project_name}/env")
async def api_get_project_env(
    project_name: str,
    file: str = Query(default=".env"),
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    resolved, _, _ = await _resolve_project_memory_target(project_name, admin_user)
    eff_file = file if isinstance(file, str) and file.strip() else ".env"
    return await _build_project_env_response(resolved, admin_user, eff_file)


@app.put("/api/projects/{project_name}/env")
async def api_update_project_env(
    project_name: str,
    body: UpdateProjectEnvRequest,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    resolved, _, is_shared = await _resolve_project_memory_target(project_name, admin_user)
    proj_dir = Path(resolved["path"]).resolve()
    try:
        saved = await asyncio.to_thread(
            env_service.save_project_env_file,
            proj_dir,
            rel_file=body.file,
            mode=body.mode,
            entries=[e.model_dump() for e in body.entries],
            raw_content=body.raw_content,
            ensure_gitignore=body.ensure_gitignore,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _notify_gdrive_change_for_user(
        admin_user,
        f"project_env:{resolved['name']}",
        is_shared=is_shared,
    )
    resp = await _build_project_env_response(resolved, admin_user, saved["active_file"])
    resp["gitignore_updated"] = bool(saved.get("gitignore_updated"))
    return resp


@app.post("/api/projects/{project_name}/env/sync-example")
async def api_sync_project_env_example(
    project_name: str,
    body: SyncProjectEnvExampleRequest,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    resolved, _, is_shared = await _resolve_project_memory_target(project_name, admin_user)
    proj_dir = Path(resolved["path"]).resolve()
    try:
        sync_res = await asyncio.to_thread(
            env_service.sync_env_example,
            proj_dir,
            rel_file=body.file,
            direction=body.direction,
            ensure_gitignore=True,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if body.direction == "import_missing":
        _notify_gdrive_change_for_user(
            admin_user,
            f"project_env:{resolved['name']}",
            is_shared=is_shared,
        )
    resp = await _build_project_env_response(resolved, admin_user, sync_res["active_file"])
    resp["sync_result"] = sync_res
    return resp


async def _resolve_project_git_target(
    project_name: str,
    current_user: dict[str, Any],
    *,
    require_write: bool = False,
) -> tuple[dict[str, Any], Path]:
    is_admin = current_user.get("role") == "admin"
    shared_projs = await database.list_shared_projects(
        for_user_id=current_user["id"], is_admin=is_admin
    )
    resolved = await asyncio.to_thread(
        brain_reader.resolve_user_project, project_name, current_user, shared_projs
    )
    if not resolved:
        raise HTTPException(
            status_code=404,
            detail="Папка проекта не найдена или доступ запрещён",
        )
    if require_write and not is_admin:
        if _max_rank_for_user(current_user) < 1:
            raise HTTPException(
                status_code=403,
                detail="Недостаточно прав: для вашего профиля установлен режим «Только чтение»",
            )
        if bool(resolved.get("is_shared")):
            raise HTTPException(
                status_code=403,
                detail="Управлять Git-точками сохранения общего проекта может только администратор",
            )
    return resolved, Path(resolved["path"]).resolve()


@app.get("/api/projects/{project_name}/git/status")
async def api_get_project_git_status(
    project_name: str,
    limit: int = Query(default=15, ge=1, le=100),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, proj_dir = await _resolve_project_git_target(
        project_name, current_user, require_write=False
    )
    eff_limit = limit if isinstance(limit, int) else 15
    try:
        status = await asyncio.to_thread(
            git_service.get_project_git_status, proj_dir, limit=eff_limit
        )
    except git_service.GitServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {
        "ok": True,
        "project_name": resolved["name"],
        "is_self_hub": bool(resolved.get("is_self_hub")),
        **status,
    }


@app.get("/api/projects/{project_name}/git/diff")
async def api_get_project_git_diff(
    project_name: str,
    commit: Optional[str] = Query(default=None),
    path: Optional[str] = Query(default=None),
    max_chars: int = Query(default=48000, ge=512, le=250000),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, proj_dir = await _resolve_project_git_target(
        project_name, current_user, require_write=False
    )
    eff_commit = commit if isinstance(commit, str) else None
    eff_path = path if isinstance(path, str) else None
    eff_max_chars = max_chars if isinstance(max_chars, int) else 48000
    try:
        diff_res = await asyncio.to_thread(
            git_service.get_project_git_diff,
            proj_dir,
            commit=eff_commit,
            path=eff_path,
            max_chars=eff_max_chars,
        )
    except git_service.GitServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {
        "ok": True,
        "project_name": resolved["name"],
        **diff_res,
    }


@app.post("/api/projects/{project_name}/git/init")
async def api_init_project_git(
    project_name: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, proj_dir = await _resolve_project_git_target(
        project_name, current_user, require_write=True
    )
    try:
        init_res = await asyncio.to_thread(
            git_service.ensure_project_git_ready, proj_dir
        )
        status = await asyncio.to_thread(
            git_service.get_project_git_status, proj_dir
        )
    except git_service.GitServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {
        "ok": True,
        "project_name": resolved["name"],
        **init_res,
        "status": status,
    }


@app.post("/api/projects/{project_name}/git/checkpoint")
async def api_create_project_git_checkpoint(
    project_name: str,
    body: GitCheckpointRequest = Body(default=GitCheckpointRequest()),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, proj_dir = await _resolve_project_git_target(
        project_name, current_user, require_write=True
    )
    eff_body = body if isinstance(body, GitCheckpointRequest) else GitCheckpointRequest()
    try:
        res = await asyncio.to_thread(
            git_service.create_project_checkpoint,
            proj_dir,
            message=eff_body.message,
            paths=eff_body.paths,
            run_verify=eff_body.run_verify,
        )
    except git_service.GitServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    _notify_gdrive_change_for_user(
        current_user, "git_checkpoint", is_shared=bool(resolved.get("is_shared"))
    )
    return {
        "ok": True,
        "project_name": resolved["name"],
        **res,
    }


@app.post("/api/projects/{project_name}/git/rollback")
async def api_rollback_project_git(
    project_name: str,
    body: GitRollbackRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, proj_dir = await _resolve_project_git_target(
        project_name, current_user, require_write=True
    )
    try:
        res = await asyncio.to_thread(
            git_service.rollback_project_to_checkpoint,
            proj_dir,
            mode=body.mode,
            target_sha=body.target_sha,
        )
    except git_service.GitServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    _notify_gdrive_change_for_user(
        current_user, "git_rollback", is_shared=bool(resolved.get("is_shared"))
    )
    return {
        "ok": True,
        "project_name": resolved["name"],
        **res,
    }


@app.post("/api/projects/{project_name}/git/install-shield")
async def api_install_project_git_shield(
    project_name: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, proj_dir = await _resolve_project_git_target(
        project_name, current_user, require_write=True
    )
    try:
        res = await asyncio.to_thread(
            git_service.install_project_shield, proj_dir
        )
    except git_service.GitServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {
        "ok": True,
        "project_name": resolved["name"],
        **res,
    }


# ---------------------------------------------------------------------------
# Google Drive Unidirectional Cloud Mirror REST API (v151, Admin-Only)
# ---------------------------------------------------------------------------


@app.get("/api/gdrive/status")
async def api_get_gdrive_status(
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    uid = admin_user["id"]
    cfg = await database.get_gdrive_config(uid, include_secrets=False)
    sync_in_progress = gdrive_service.is_user_sync_in_progress(uid)
    return {
        "ok": True,
        **cfg,
        "sync_in_progress": sync_in_progress,
        "sync_progress": gdrive_service.get_user_sync_progress(uid) if sync_in_progress else None,
    }


@app.post("/api/gdrive/config")
async def api_save_gdrive_config(
    body: UpdateGDriveConfigRequest,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    uid = admin_user["id"]
    parsed_oauth_client: Optional[dict[str, Any]] = None
    has_oauth_input = (
        (body.oauth_client_json is not None and str(body.oauth_client_json).strip() != "")
        or (body.client_id is not None and str(body.client_id).strip() != "")
        or (body.client_secret is not None and str(body.client_secret).strip() != "")
    )
    if has_oauth_input and not body.clear_oauth_client:
        try:
            parsed_oauth_client = gdrive_service.parse_google_oauth_client_input(
                oauth_client_json=body.oauth_client_json,
                client_id=body.client_id,
                client_secret=body.client_secret,
            )
        except gdrive_service.GDriveServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))

    saved = await database.save_gdrive_config(
        uid,
        oauth_client=parsed_oauth_client,
        clear_oauth_client=bool(body.clear_oauth_client),
        root_folder_name=body.root_folder_name,
        is_enabled=body.is_enabled,
        sync_self_hub=body.sync_self_hub,
        excluded_projects=body.excluded_projects,
        include_env_vault=body.include_env_vault,
        vault_passphrase=body.vault_passphrase,
        clear_vault_passphrase=bool(body.clear_vault_passphrase),
    )
    sync_in_progress = gdrive_service.is_user_sync_in_progress(uid)
    return {
        "ok": True,
        **saved,
        "sync_in_progress": sync_in_progress,
        "sync_progress": gdrive_service.get_user_sync_progress(uid) if sync_in_progress else None,
    }


@app.post("/api/gdrive/oauth/start")
async def api_gdrive_oauth_start(
    request: Request,
    body: GDriveOAuthStartRequest = Body(default=GDriveOAuthStartRequest()),
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    uid = admin_user["id"]
    cfg = await database.get_gdrive_config(uid, include_secrets=True)
    oauth_client = cfg.get("oauth_client")
    if not isinstance(oauth_client, dict) or not oauth_client.get("client_id"):
        raise HTTPException(
            status_code=400,
            detail="Сначала сохраните JSON OAuth-клиента Google (client_id и client_secret)",
        )
    eff_body = body if isinstance(body, GDriveOAuthStartRequest) else GDriveOAuthStartRequest()
    redirect_uri = str(eff_body.redirect_uri or "").strip()
    if not redirect_uri:
        base = str(request.base_url).rstrip("/")
        redirect_uri = f"{base}/api/gdrive/oauth/callback"
    try:
        flow = gdrive_service.start_oauth_flow(
            uid,
            client_id=str(oauth_client["client_id"]),
            redirect_uri=redirect_uri,
            auth_uri=str(oauth_client.get("auth_uri") or gdrive_service.GOOGLE_AUTH_URL),
        )
    except gdrive_service.GDriveServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {"ok": True, **flow}


@app.get("/api/gdrive/oauth/callback")
async def api_gdrive_oauth_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    if error:
        desc = f"{error}: {error_description}" if error_description else str(error)
        return HTMLResponse(
            content=gdrive_service.render_oauth_callback_html(
                ok=False,
                message=f"Google отклонил доступ: {desc}",
            ),
            status_code=400,
        )
    if not code:
        return HTMLResponse(
            content=gdrive_service.render_oauth_callback_html(
                ok=False,
                message="В ответе Google отсутствует параметр code.",
            ),
            status_code=400,
        )
    try:
        saved = await gdrive_service.complete_oauth_exchange(
            admin_user["id"],
            code_or_url=code,
            state=state,
        )
    except gdrive_service.GDriveServiceError as exc:
        return HTMLResponse(
            content=gdrive_service.render_oauth_callback_html(
                ok=False,
                message=str(exc),
            ),
            status_code=exc.status_code if 400 <= exc.status_code < 500 else 400,
        )
    return HTMLResponse(
        content=gdrive_service.render_oauth_callback_html(
            ok=True,
            message="Авторизация успешно завершена. Можно вернуться в ZeroHub.",
            account_email=str(saved.get("account_email") or ""),
        ),
        status_code=200,
    )


@app.post("/api/gdrive/oauth/exchange")
async def api_gdrive_oauth_exchange(
    body: GDriveOAuthExchangeRequest,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    raw_input = str(body.callback_url or "").strip() or str(body.code or "").strip()
    try:
        saved = await gdrive_service.complete_oauth_exchange(
            admin_user["id"],
            code_or_url=raw_input,
            state=body.state,
        )
    except gdrive_service.GDriveServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {
        "ok": True,
        **saved,
        "sync_in_progress": gdrive_service.is_user_sync_in_progress(admin_user["id"]),
    }


@app.post("/api/gdrive/disconnect")
async def api_gdrive_disconnect(
    body: GDriveDisconnectRequest = Body(default=GDriveDisconnectRequest()),
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    uid = admin_user["id"]
    eff_body = body if isinstance(body, GDriveDisconnectRequest) else GDriveDisconnectRequest()
    saved = await database.disconnect_gdrive_account(
        uid,
        clear_index=bool(eff_body.clear_index),
        clear_root_folder=bool(eff_body.clear_root_folder),
    )
    sync_in_progress = gdrive_service.is_user_sync_in_progress(uid)
    return {
        "ok": True,
        **saved,
        "sync_in_progress": sync_in_progress,
        "sync_progress": gdrive_service.get_user_sync_progress(uid) if sync_in_progress else None,
    }


@app.get("/api/gdrive/preview")
async def api_gdrive_preview(
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    try:
        preview = await gdrive_service.preview_user_gdrive_sync(admin_user)
    except gdrive_service.GDriveServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {"ok": True, **preview}


@app.post("/api/gdrive/sync")
async def api_gdrive_sync(
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    uid = admin_user["id"]
    lock = gdrive_service.get_user_sync_lock(uid)
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Синхронизация с Google Диском уже выполняется. Дождитесь её завершения.",
        )
    async with lock:
        try:
            res = await gdrive_service.sync_user_to_gdrive(admin_user, trigger="manual")
        except gdrive_service.GDriveServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {"ok": True, **res}


@app.get("/api/gdrive/restore/catalog")
async def api_gdrive_restore_catalog(
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    try:
        catalog = await gdrive_service.get_gdrive_restore_catalog(admin_user)
    except gdrive_service.GDriveServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {"ok": True, **catalog}


@app.post("/api/gdrive/restore/execute")
async def api_gdrive_restore_execute(
    body: GDriveRestoreExecuteRequest,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    uid = admin_user["id"]
    lock = gdrive_service.get_user_sync_lock(uid)
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Операция синхронизации или восстановления Google Диска уже выполняется. Дождитесь её завершения.",
        )
    async with lock:
        try:
            res = await gdrive_service.execute_gdrive_restore(
                admin_user,
                restore_dialogs=bool(body.restore_dialogs),
                restore_code=bool(body.restore_code),
                selected_projects=list(body.selected_projects or []),
                restore_env_vault=bool(body.restore_env_vault),
                vault_passphrase=str(body.vault_passphrase or ""),
                overwrite_existing=bool(body.overwrite_existing),
            )
        except gdrive_service.GDriveServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return {"ok": True, **res}


async def _resolve_process_project_for_user(
    project_name: str,
    current_user: dict[str, Any],
) -> tuple[dict[str, Any], str, bool, str, Path]:
    resolved, scope_key, is_shared = await _resolve_project_memory_target(
        project_name, current_user
    )
    eff_exec_mode = brain_reader.resolve_effective_project_execution_mode(
        current_user, resolved
    )
    proj_dir = Path(resolved["path"]).resolve()
    if eff_exec_mode == "host":
        if not host_service_manager.is_user_host_eligible(current_user):
            raise HTTPException(
                status_code=403,
                detail="Управление фоновыми сервисами ПК доступно только при включённом праве прямого терминала (Host)",
            )
    elif eff_exec_mode == "docker":
        if not sandbox_service.is_user_sandbox_eligible(current_user):
            raise HTTPException(
                status_code=403,
                detail="Docker-песочница недоступна для вашей роли или выбранного проекта",
            )
    else:
        raise HTTPException(
            status_code=403,
            detail="Выполнение фоновых процессов отключено для выбранного проекта",
        )
    return resolved, scope_key, is_shared, eff_exec_mode, proj_dir


@app.get("/api/processes")
async def api_list_all_processes(
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    can_host = host_service_manager.is_user_host_eligible(current_user)
    can_sandbox = sandbox_service.is_user_sandbox_eligible(current_user)
    shared_projs = await database.list_shared_projects(
        for_user_id=current_user["id"], is_admin=is_admin
    )
    projects, docker_status = await asyncio.gather(
        asyncio.to_thread(brain_reader.list_projects_for_user, current_user, shared_projs),
        check_docker_status(),
    )

    has_any_docker_proj = can_sandbox and bool(docker_status.get("running")) and any(
        brain_reader.resolve_effective_project_execution_mode(current_user, p) == "docker"
        for p in projects
    )
    running_containers: set[str] = (
        await sandbox_service.list_running_managed_container_names()
        if has_any_docker_proj
        else set()
    )

    all_processes: list[dict[str, Any]] = []
    by_project: dict[str, list[dict[str, Any]]] = {}

    for p in projects:
        resolved_p = brain_reader.resolve_user_project(p["name"], current_user, shared_projs)
        if not resolved_p:
            continue
        proj_dir = Path(resolved_p["path"]).resolve()
        eff_mode = brain_reader.resolve_effective_project_execution_mode(current_user, resolved_p)
        raw_svcs: list[dict[str, Any]] = []
        if can_host and eff_mode == "host":
            raw_svcs = await asyncio.to_thread(
                host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
                resolved_p["name"],
                project_dir=proj_dir,
            )
        elif can_sandbox and eff_mode == "docker" and running_containers:
            raw_svcs = await sandbox_service.list_project_sandbox_services(
                str(current_user["id"]),
                proj_dir,
                resolved_p["name"],
                running_containers=running_containers,
            )

        if raw_svcs:
            sanitized = [
                host_service_manager.sanitize_process_view_for_user(
                    s, current_user, project_dir=proj_dir
                )
                for s in raw_svcs
            ]
            by_project[resolved_p["name"]] = sanitized
            all_processes.extend(sanitized)

    all_processes.sort(
        key=lambda s: (
            0 if s.get("alive") else (1 if s.get("status") == "crashed" else 2),
            str(s.get("project_name") or "").lower(),
            str(s.get("name") or "").lower(),
        )
    )
    return {
        "ok": True,
        "processes": all_processes,
        "by_project": by_project,
        "global_summary": _summarize_processes_list(all_processes),
    }


@app.post("/api/processes/clear")
async def api_clear_all_processes(
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    can_host = host_service_manager.is_user_host_eligible(current_user)
    can_sandbox = sandbox_service.is_user_sandbox_eligible(current_user)
    shared_projs = await database.list_shared_projects(
        for_user_id=current_user["id"], is_admin=is_admin
    )
    projects, docker_status = await asyncio.gather(
        asyncio.to_thread(brain_reader.list_projects_for_user, current_user, shared_projs),
        check_docker_status(),
    )

    has_any_docker_proj = can_sandbox and bool(docker_status.get("running")) and any(
        brain_reader.resolve_effective_project_execution_mode(current_user, p) == "docker"
        for p in projects
    )
    running_containers: set[str] = (
        await sandbox_service.list_running_managed_container_names()
        if has_any_docker_proj
        else set()
    )

    all_processes: list[dict[str, Any]] = []
    by_project: dict[str, list[dict[str, Any]]] = {}
    summaries_by_project: dict[str, dict[str, Any]] = {}
    cleared_by_project: dict[str, int] = {}
    affected_projects: list[tuple[str, Path, Optional[str]]] = []
    total_cleared = 0

    for p in projects:
        resolved_p = brain_reader.resolve_user_project(p["name"], current_user, shared_projs)
        if not resolved_p:
            continue
        proj_dir = Path(resolved_p["path"]).resolve()
        eff_mode = brain_reader.resolve_effective_project_execution_mode(current_user, resolved_p)
        raw_svcs: list[dict[str, Any]] = []
        cleared_cnt = 0
        docker_uid: Optional[str] = None

        if can_host and eff_mode == "host":
            clear_res = await asyncio.to_thread(
                host_service_manager.HOST_SERVICE_MANAGER.clear_inactive_services,
                resolved_p["name"],
                project_dir=proj_dir,
            )
            cleared_cnt = int(clear_res.get("cleared_count") or 0)
            raw_svcs = await asyncio.to_thread(
                host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
                resolved_p["name"],
                project_dir=proj_dir,
            )
        elif can_sandbox and eff_mode == "docker" and running_containers:
            docker_uid = str(current_user["id"])
            clear_res = await sandbox_service.clear_inactive_services(
                docker_uid,
                proj_dir,
                resolved_p["name"],
                running_containers=running_containers,
            )
            cleared_cnt = int(clear_res.get("cleared_count") or 0)
            raw_svcs = await sandbox_service.list_project_sandbox_services(
                docker_uid,
                proj_dir,
                resolved_p["name"],
                running_containers=running_containers,
            )

        if cleared_cnt > 0:
            total_cleared += cleared_cnt
            cleared_by_project[resolved_p["name"]] = cleared_cnt
            affected_projects.append((resolved_p["name"], proj_dir, docker_uid))

        sanitized = [
            host_service_manager.sanitize_process_view_for_user(
                s, current_user, project_dir=proj_dir
            )
            for s in raw_svcs
        ]
        summaries_by_project[resolved_p["name"]] = _summarize_processes_list(sanitized)
        if sanitized:
            by_project[resolved_p["name"]] = sanitized
            all_processes.extend(sanitized)

    for aff_name, aff_dir, aff_docker_uid in affected_projects:
        await broadcast_project_processes_update(
            aff_name,
            aff_dir,
            docker_user_id=aff_docker_uid,
        )

    all_processes.sort(
        key=lambda s: (
            0 if s.get("alive") else (1 if s.get("status") == "crashed" else 2),
            str(s.get("project_name") or "").lower(),
            str(s.get("name") or "").lower(),
        )
    )
    return {
        "ok": True,
        "cleared_count": total_cleared,
        "cleared_by_project": cleared_by_project,
        "affected_projects": [x[0] for x in affected_projects],
        "processes": all_processes,
        "by_project": by_project,
        "summaries_by_project": summaries_by_project,
        "global_summary": _summarize_processes_list(all_processes),
    }


@app.get("/api/projects/{project_name}/processes")
async def api_list_project_processes(
    project_name: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, _, _, eff_exec_mode, proj_dir = await _resolve_process_project_for_user(
        project_name, current_user
    )
    if eff_exec_mode == "host":
        raw_svcs = await asyncio.to_thread(
            host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
            resolved["name"],
            project_dir=proj_dir,
        )
    else:
        raw_svcs = await sandbox_service.list_project_sandbox_services(
            str(current_user["id"]),
            proj_dir,
            resolved["name"],
        )
    processes = [
        host_service_manager.sanitize_process_view_for_user(
            s, current_user, project_dir=proj_dir
        )
        for s in raw_svcs
    ]
    return {
        "ok": True,
        "project_name": resolved["name"],
        "execution_mode": eff_exec_mode,
        "processes": processes,
        "services": processes,
        "summary": _summarize_processes_list(processes),
    }


@app.get("/api/projects/{project_name}/processes/{service_name}/logs")
async def api_get_project_process_logs(
    project_name: str,
    service_name: str,
    tail_lines: int = Query(120, ge=10, le=500),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, _, _, eff_exec_mode, proj_dir = await _resolve_process_project_for_user(
        project_name, current_user
    )
    try:
        if eff_exec_mode == "host":
            raw_res = await asyncio.to_thread(
                host_service_manager.HOST_SERVICE_MANAGER.get_service_logs,
                resolved["name"],
                service_name,
                project_dir=proj_dir,
                tail_lines=tail_lines,
            )
        else:
            raw_res = await sandbox_service.get_project_sandbox_service_logs(
                str(current_user["id"]),
                proj_dir,
                resolved["name"],
                service_name,
                tail_lines=tail_lines,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sanitized = host_service_manager.sanitize_process_view_for_user(
        raw_res, current_user, project_dir=proj_dir
    )
    if current_user.get("role") != "admin" and sanitized.get("log_tail"):
        sanitized["log_tail"] = host_service_manager.mask_sensitive_log_text(
            str(sanitized["log_tail"]),
            is_admin=False,
            project_dir=proj_dir,
        )
    return sanitized


@app.post("/api/projects/{project_name}/processes/clear")
async def api_clear_project_processes(
    project_name: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, _, _, eff_exec_mode, proj_dir = await _resolve_process_project_for_user(
        project_name, current_user
    )
    if eff_exec_mode == "host":
        clear_res = await asyncio.to_thread(
            host_service_manager.HOST_SERVICE_MANAGER.clear_inactive_services,
            resolved["name"],
            project_dir=proj_dir,
        )
        raw_svcs = await asyncio.to_thread(
            host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
            resolved["name"],
            project_dir=proj_dir,
        )
        await broadcast_project_processes_update(resolved["name"], proj_dir)
    else:
        docker_uid = str(current_user["id"])
        clear_res = await sandbox_service.clear_inactive_services(
            docker_uid,
            proj_dir,
            resolved["name"],
        )
        raw_svcs = await sandbox_service.list_project_sandbox_services(
            docker_uid,
            proj_dir,
            resolved["name"],
        )
        await broadcast_project_processes_update(
            resolved["name"],
            proj_dir,
            docker_user_id=docker_uid,
        )

    sanitized_svcs = [
        host_service_manager.sanitize_process_view_for_user(
            s, current_user, project_dir=proj_dir
        )
        for s in raw_svcs
    ]
    return {
        "ok": True,
        "project_name": resolved["name"],
        "execution_mode": eff_exec_mode,
        "cleared_count": int(clear_res.get("cleared_count") or 0),
        "cleared_names": list(clear_res.get("cleared_names") or []),
        "kept_running_count": int(clear_res.get("kept_running_count") or 0),
        "processes": sanitized_svcs,
        "services": sanitized_svcs,
        "summary": _summarize_processes_list(sanitized_svcs),
    }


@app.post("/api/projects/{project_name}/processes/{service_name}/{action}")
async def api_control_project_process(
    project_name: str,
    service_name: str,
    action: str,
    body: Optional[HostServiceActionRequest] = None,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    clean_action = (action or "").strip().lower()
    if clean_action == "remove":
        clean_action = "delete"
    if clean_action not in {"start", "stop", "restart", "delete", "ack"}:
        raise HTTPException(
            status_code=400,
            detail="Допустимые действия: start, stop, restart, delete, ack",
        )
    resolved, scope_key, _, eff_exec_mode, proj_dir = await _resolve_process_project_for_user(
        project_name, current_user
    )
    req = body or HostServiceActionRequest()

    if eff_exec_mode == "host":
        extra_dirs: list[Path] = []
        if clean_action in {"start", "restart"}:
            mem = await database.get_project_memory(
                scope_key, project_name=resolved["name"], owner_id=current_user["id"]
            )
            extra_dirs = await asyncio.to_thread(
                brain_reader.validate_project_extra_dirs,
                mem.get("extra_dirs") or [],
                user=current_user if current_user.get("role") == "admin" else {"role": "admin"},
                exec_mode="host",
                project_dir=proj_dir,
                strict=False,
            )
        try:
            ctl_res = await asyncio.to_thread(
                host_service_manager.HOST_SERVICE_MANAGER.control_service,
                project_name=resolved["name"],
                project_dir=proj_dir,
                action=clean_action,
                name=service_name,
                command=req.command,
                workdir=req.workdir,
                port=req.port,
                allow_gpu=bool(req.allow_gpu and current_user.get("role") == "admin"),
                replace=req.replace,
                startup_wait_ms=req.startup_wait_ms,
                user=current_user,
                extra_dirs=extra_dirs,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        raw_svcs = await asyncio.to_thread(
            host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
            resolved["name"],
            project_dir=proj_dir,
        )
        await broadcast_project_processes_update(resolved["name"], proj_dir)
    else:
        try:
            ctl_res = await sandbox_service.control_project_sandbox_service(
                str(current_user["id"]),
                proj_dir,
                resolved["name"],
                action=clean_action,
                name=service_name,
                command=req.command,
                workdir=req.workdir,
                port=req.port,
                sandbox_network=str(current_user.get("sandbox_network") or "internet"),
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        raw_svcs = await sandbox_service.list_project_sandbox_services(
            str(current_user["id"]),
            proj_dir,
            resolved["name"],
        )
        await broadcast_project_processes_update(
            resolved["name"],
            proj_dir,
            docker_user_id=str(current_user["id"]),
        )

    sanitized_ctl = host_service_manager.sanitize_process_view_for_user(
        ctl_res, current_user, project_dir=proj_dir
    )
    sanitized_svcs = [
        host_service_manager.sanitize_process_view_for_user(
            s, current_user, project_dir=proj_dir
        )
        for s in raw_svcs
    ]
    return {
        **sanitized_ctl,
        "execution_mode": eff_exec_mode,
        "processes": sanitized_svcs,
        "services": sanitized_svcs,
        "summary": _summarize_processes_list(sanitized_svcs),
    }


@app.get("/api/projects/{project_name}/host-services")
async def api_list_project_host_services(
    project_name: str,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, _, _ = await _resolve_host_project_for_user(project_name, current_user)
    proj_dir = Path(resolved["path"]).resolve()
    raw_services = await asyncio.to_thread(
        host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
        resolved["name"],
        project_dir=proj_dir,
    )
    services = [
        host_service_manager.sanitize_process_view_for_user(
            s, current_user, project_dir=proj_dir
        )
        for s in raw_services
    ]
    return {
        "ok": True,
        "project_name": resolved["name"],
        "services": services,
        "summary": _summarize_host_services_list(services),
    }


@app.get("/api/projects/{project_name}/host-services/{service_name}/logs")
async def api_get_project_host_service_logs(
    project_name: str,
    service_name: str,
    tail_lines: int = Query(100, ge=10, le=500),
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    resolved, _, _ = await _resolve_host_project_for_user(project_name, current_user)
    proj_dir = Path(resolved["path"]).resolve()
    try:
        raw_res = await asyncio.to_thread(
            host_service_manager.HOST_SERVICE_MANAGER.get_service_logs,
            resolved["name"],
            service_name,
            project_dir=proj_dir,
            tail_lines=tail_lines,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sanitized = host_service_manager.sanitize_process_view_for_user(
        raw_res, current_user, project_dir=proj_dir
    )
    if current_user.get("role") != "admin" and sanitized.get("log_tail"):
        sanitized["log_tail"] = host_service_manager.mask_sensitive_log_text(
            str(sanitized["log_tail"]),
            is_admin=False,
            project_dir=proj_dir,
        )
    return sanitized


@app.post("/api/projects/{project_name}/host-services/{service_name}/{action}")
async def api_control_project_host_service(
    project_name: str,
    service_name: str,
    action: str,
    body: Optional[HostServiceActionRequest] = None,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    clean_action = (action or "").strip().lower()
    if clean_action == "remove":
        clean_action = "delete"
    if clean_action not in {"start", "stop", "restart", "delete", "ack"}:
        raise HTTPException(
            status_code=400,
            detail="Допустимые действия: start, stop, restart, delete, ack",
        )
    resolved, scope_key, _ = await _resolve_host_project_for_user(
        project_name, current_user
    )
    proj_dir = Path(resolved["path"]).resolve()
    extra_dirs: list[Path] = []
    if clean_action in {"start", "restart"}:
        mem = await database.get_project_memory(
            scope_key, project_name=resolved["name"], owner_id=current_user["id"]
        )
        extra_dirs = await asyncio.to_thread(
            brain_reader.validate_project_extra_dirs,
            mem.get("extra_dirs") or [],
            user=current_user if current_user.get("role") == "admin" else {"role": "admin"},
            exec_mode="host",
            project_dir=proj_dir,
            strict=False,
        )
    req = body or HostServiceActionRequest()
    try:
        ctl_res = await asyncio.to_thread(
            host_service_manager.HOST_SERVICE_MANAGER.control_service,
            project_name=resolved["name"],
            project_dir=proj_dir,
            action=clean_action,
            name=service_name,
            command=req.command,
            workdir=req.workdir,
            port=req.port,
            allow_gpu=bool(req.allow_gpu and current_user.get("role") == "admin"),
            replace=req.replace,
            startup_wait_ms=req.startup_wait_ms,
            user=current_user,
            extra_dirs=extra_dirs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    raw_services = await asyncio.to_thread(
        host_service_manager.HOST_SERVICE_MANAGER.list_project_services,
        resolved["name"],
        project_dir=proj_dir,
    )
    await broadcast_project_processes_update(resolved["name"], proj_dir)
    sanitized_ctl = host_service_manager.sanitize_process_view_for_user(
        ctl_res, current_user, project_dir=proj_dir
    )
    services = [
        host_service_manager.sanitize_process_view_for_user(
            s, current_user, project_dir=proj_dir
        )
        for s in raw_services
    ]
    return {
        **sanitized_ctl,
        "services": services,
        "summary": _summarize_host_services_list(services),
    }


@app.get("/api/admin/projects/migration-plan")
async def api_admin_get_migration_plan(
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    return await asyncio.to_thread(
        project_migrator.discover_desktop_migration_plan,
        owner_id=admin_user["id"],
    )


@app.post("/api/admin/projects/execute-migration")
async def api_admin_execute_migration(
    body: Optional[ExecuteDesktopMigrationRequest] = None,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    req = body or ExecuteDesktopMigrationRequest()
    all_models = await fetch_available_models()
    chosen_model = resolve_active_model_id(
        pick_default_model_id(all_models),
        all_models,
        cli_online=bool(get_cli_auth_status().get("ok", True)),
    )
    try:
        report = await project_migrator.execute_desktop_migration(
            selected_projects=req.selected_projects,
            import_chats=req.import_chats,
            merge_missing_on_conflict=req.merge_missing_on_conflict,
            rebuild_venv=req.rebuild_venv,
            move_legacy_siblings=req.move_legacy_siblings,
            owner_id=admin_user["id"],
            default_model=chosen_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await ws_manager.broadcast_admins(
        {"type": "projects_migrated", "report": report}
    )
    return report


@app.post("/api/projects/{project_name}/import-folder")
async def api_import_folder_into_project(
    project_name: str,
    body: ImportProjectFolderRequest,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    resolved, scope_key, _ = await _resolve_project_memory_target(
        project_name, admin_user
    )
    all_models = await fetch_available_models()
    chosen_model = resolve_active_model_id(
        pick_default_model_id(all_models),
        all_models,
        cli_online=bool(get_cli_auth_status().get("ok", True)),
    )
    try:
        res = await project_migrator.import_folder_into_project(
            project_name=resolved["name"],
            dest_path=Path(resolved["path"]),
            source_path=body.source_path,
            scope_key=scope_key,
            owner_id=admin_user["id"],
            only_missing_on_non_empty=body.only_missing_on_non_empty,
            import_chats=body.import_chats,
            rebuild_venv=body.rebuild_venv,
            default_model=chosen_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await ws_manager.broadcast_admins(
        {
            "type": "projects_migrated",
            "project_name": resolved["name"],
            "report": res,
        }
    )
    return res


class RenameProjectRequest(BaseModel):
    new_name: str


class DeleteProjectRequest(BaseModel):
    chats_action: Literal["delete", "move_to_regular"] = "delete"
    delete_files: bool = False
    confirm_name: str = ""


_PROJECT_MUTATION_LOCK = asyncio.Lock()


class ShareProjectRequest(BaseModel):
    description: str = ""
    execution_mode: Literal["docker", "sandbox_files", "host"] = "docker"
    storage_location: Literal["shared_dir", "projects_root"] = "shared_dir"
    member_ids: list[str] = Field(default_factory=list)
    upgrade_members_docker: bool = True
    chats_action: Literal["keep_in_project", "move_to_regular"] = "keep_in_project"


async def _notify_project_sharing(name, current_user, prev_members, members, payload):
    event = {"type": "project_sharing_updated", "project_name": name, **payload}
    await ws_manager.broadcast_user(current_user["id"], event)
    users = await database.list_users()
    for user in users:
        uid = user["id"]
        if uid == current_user["id"]:
            continue
        if user.get("role") == "admin":
            await ws_manager.broadcast_user(uid, event)
        elif uid in set(prev_members) | set(members):
            await ws_manager.broadcast_user(uid, {"type": event["type"], "project_name": name,
                "is_shared": event["is_shared"], "access_revoked": uid not in members})


async def _relocate_sharing_directory(src, dst):
    return await asyncio.to_thread(brain_reader.relocate_project_directory_on_disk,src,dst,
        allowed_src_parent=src.parent,allowed_dst_parent=dst.parent)


@app.post("/api/projects/{project_name}/share")
async def api_share_project(project_name: str, body: ShareProjectRequest,
        current_user: dict[str, Any] = Depends(auth.get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(403, "Управлять общим доступом может только администратор")
    async with _PROJECT_MUTATION_LOCK:
        resolved = await _managed_project_guard(project_name,current_user)
        name = resolved["name"]
        previous = await database.get_shared_project_by_name(name) if resolved["is_shared"] else None
        prev_members = (previous or {}).get("member_ids", [])
        members = sorted({m.strip() for m in body.member_ids if m.strip() and m.strip() != "admin"})
        for uid in members:
            user = await database.get_user_by_id(uid)
            if not user:
                raise HTTPException(400, "Выбранный пользователь не найден")
            if user.get("role") != "admin":
                root = brain_reader.get_user_personal_root(user)
                if root.exists() and any(p.name.lower() == name.lower() for p in root.iterdir()):
                    raise HTTPException(409, f"У пользователя @{user['username']} уже есть личный проект «{name}» — переименуйте проект перед открытием общего доступа")
        old_dir = Path(resolved["path"]).resolve()
        try:
            target = brain_reader.validate_shared_project_path(name,storage_location=body.storage_location)
            if target != old_dir or not previous or previous["execution_mode"] != body.execution_mode or set(prev_members) != set(members):
                await sandbox_service.stop_project_sandboxes(project_dir=old_dir,project_name=name)
                await asyncio.to_thread(host_service_manager.HOST_SERVICE_MANAGER.stop_all_project_services,name,old_dir)
            if target != old_dir:
                await _relocate_sharing_directory(old_dir,target)
            try:
                result = await database.promote_or_update_shared_project_records(project_name=name,old_dir=old_dir,new_dir=target,
                    owner_id=current_user["id"],was_shared=resolved["is_shared"],shared_id=(previous or {}).get("id", ""),
                    description=body.description,execution_mode=body.execution_mode,member_ids=members,chats_action=body.chats_action)
            except BaseException:
                if target != old_dir:
                    await _relocate_sharing_directory(target,old_dir)
                raise
        except ValueError as exc:
            raise HTTPException(400,str(exc))
        except (RuntimeError,OSError) as exc:
            raise HTTPException(409,str(exc))
        if old_dir != target:
            await asyncio.to_thread(host_service_manager.HOST_SERVICE_MANAGER.rename_project_in_registry,name,old_dir,name,target)
        upgraded = await _maybe_upgrade_shared_project_members_docker(members,body.execution_mode,body.upgrade_members_docker)
        await sandbox_service.sync_all_users_sandbox_cli_files()
        brain_reader.invalidate_projects_list_cache()
        payload = {"is_shared": True,"storage_location":body.storage_location,
                   "moved_chat_ids":result["moved_chat_ids"],"folder":result["folder"]}
        await _notify_project_sharing(name,current_user,prev_members,members,payload)
        return {"ok":True,"type":"project_sharing_updated","project_name":name,**payload,**result,"upgraded_member_ids":upgraded}


@app.post("/api/projects/{project_name}/unshare")
async def api_unshare_project(project_name: str,
        current_user: dict[str, Any] = Depends(auth.get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(403, "Управлять общим доступом может только администратор")
    async with _PROJECT_MUTATION_LOCK:
        resolved = await _managed_project_guard(project_name,current_user)
        if not resolved["is_shared"]:
            raise HTTPException(400, "Проект уже является личным")
        name = resolved["name"]
        previous = await database.get_shared_project_by_name(name)
        old_dir = Path(resolved["path"]).resolve()
        target = brain_reader.get_user_personal_root(current_user) / name
        await sandbox_service.stop_project_sandboxes(project_dir=old_dir,project_name=name)
        await asyncio.to_thread(host_service_manager.HOST_SERVICE_MANAGER.stop_all_project_services,name,old_dir)
        try:
            if old_dir != target:
                await _relocate_sharing_directory(old_dir,target)
            try:
                result = await database.demote_shared_project_records(project_name=name,shared_id=previous["id"],
                    old_dir=old_dir,new_dir=target,target_owner_id=current_user["id"])
            except BaseException:
                if old_dir != target:
                    await _relocate_sharing_directory(target,old_dir)
                raise
        except ValueError as exc:
            raise HTTPException(400,str(exc))
        except (RuntimeError,OSError) as exc:
            raise HTTPException(409,str(exc))
        if old_dir != target:
            await asyncio.to_thread(host_service_manager.HOST_SERVICE_MANAGER.rename_project_in_registry,name,old_dir,name,target)
        await sandbox_service.sync_all_users_sandbox_cli_files()
        brain_reader.invalidate_projects_list_cache()
        payload = {"is_shared":False,**result}
        await _notify_project_sharing(name,current_user,previous["member_ids"],[],payload)
        return {"ok":True,"type":"project_sharing_updated","project_name":name,**payload}


async def _managed_project_guard(project_name, current_user):
    if brain_reader.is_self_hub_project_name(project_name):
        raise HTTPException(403, "Системный проект Antigravity_Hub нельзя переименовать или удалить")
    shared = await database.list_shared_projects(for_user_id=current_user["id"], is_admin=current_user.get("role")=="admin")
    shared_match = next((p for p in shared if p["name"].lower() == project_name.strip().lower()), None)
    if shared_match is None:
        personal_root = brain_reader.get_user_personal_root(current_user)
        raw_dir = personal_root / project_name.strip()
        if raw_dir.exists():
            try:
                brain_reader.assert_safe_managed_project_dir(raw_dir, allowed_parent=personal_root)
            except ValueError as exc:
                raise HTTPException(403, str(exc))
    resolved = await asyncio.to_thread(brain_reader.resolve_user_project,project_name,current_user,shared)
    if not resolved:
        raise HTTPException(404, "Проект не найден")
    if Path(resolved["path"]).resolve() == config.BASE_DIR.resolve():
        raise HTTPException(403, "Системный проект Antigravity_Hub нельзя переименовать или удалить")
    if resolved["is_shared"] and current_user.get("role") != "admin":
        raise HTTPException(403, "Управлять общим проектом может только администратор")
    async with database.connect_db() as db:
        db.row_factory = database.aiosqlite.Row
        chats = await database._managed_project_chats(db,resolved["name"],current_user["id"],resolved["is_shared"])
    if any(c["id"] in active_generations or c["id"] in ACTIVE_TURN_TASKS or c["id"] in COMPACTING_CHATS for c in chats):
        raise HTTPException(409, "В проекте сейчас выполняется активная генерация — дождитесь завершения или остановите её")
    try:
        brain_reader.assert_safe_managed_project_dir(Path(resolved["path"]),allowed_parent=Path(resolved["path"]).parent)
    except ValueError as exc:
        raise HTTPException(403,str(exc))
    return resolved


async def _broadcast_project_mutation(payload, current_user, is_shared, member_ids=()):
    users = await database.list_users() if is_shared else [current_user]
    for user in users:
        if not is_shared or user.get("role") == "admin" or user["id"] in member_ids:
            await ws_manager.broadcast_user(user["id"],payload)


@app.post("/api/projects/{project_name}/rename")
@app.patch("/api/projects/{project_name}")
async def api_rename_project(project_name: str, body: RenameProjectRequest,
        current_user: dict[str, Any] = Depends(auth.get_current_user)):
    async with _PROJECT_MUTATION_LOCK:
        resolved = await _managed_project_guard(project_name,current_user)
        old_name, old_dir = resolved["name"], Path(resolved["path"])
        try:
            clean = brain_reader.validate_new_project_name(body.new_name)
            shared = await database.list_shared_projects(is_admin=True)
            available = await asyncio.to_thread(brain_reader.list_projects_for_user,current_user,shared)
            if clean.lower()!=old_name.lower() and any(p["name"].lower()==clean.lower() for p in available+shared):
                raise ValueError("Проект с таким именем уже существует")
            await sandbox_service.stop_project_sandboxes(project_dir=old_dir,project_name=old_name)
            await asyncio.to_thread(host_service_manager.HOST_SERVICE_MANAGER.stop_all_project_services,old_name,old_dir)
            new_dir = await asyncio.to_thread(brain_reader.rename_project_directory_on_disk,old_dir,clean,allowed_parent=old_dir.parent)
            try:
                result = await database.rename_project_records(old_name=old_name,new_name=clean,old_dir=old_dir,new_dir=new_dir,
                    owner_id=current_user["id"],is_shared=resolved["is_shared"],shared_id=resolved.get("shared_id", ""))
            except Exception:
                await asyncio.to_thread(brain_reader.rename_project_directory_on_disk,new_dir,old_name,allowed_parent=old_dir.parent)
                raise
            await asyncio.to_thread(host_service_manager.HOST_SERVICE_MANAGER.rename_project_in_registry,old_name,old_dir,clean,new_dir)
        except ValueError as exc:
            raise HTTPException(400,str(exc))
        except (RuntimeError, OSError) as exc:
            raise HTTPException(409,str(exc))
        if resolved["is_shared"]:
            await sandbox_service.sync_all_users_sandbox_cli_files()
        brain_reader.invalidate_projects_list_cache()
        payload = {"type":"project_renamed","old_name":old_name,"new_name":clean,"is_shared":resolved["is_shared"]}
        await _broadcast_project_mutation(payload,current_user,resolved["is_shared"],resolved.get("member_ids", []))
        return {"ok":True,**payload,**result}


@app.post("/api/projects/{project_name}/delete")
@app.delete("/api/projects/{project_name}")
async def api_delete_project(project_name: str, body: DeleteProjectRequest = Body(default=DeleteProjectRequest()),
        current_user: dict[str, Any] = Depends(auth.get_current_user)):
    async with _PROJECT_MUTATION_LOCK:
        resolved = await _managed_project_guard(project_name,current_user)
        name, proj_dir = resolved["name"], Path(resolved["path"])
        health = await asyncio.to_thread(brain_reader.inspect_project_dir_health,proj_dir)
        if body.delete_files and not health["is_empty_skeleton"] and health["real_entries_count"] > 0 and body.confirm_name.strip()!=name:
            raise HTTPException(400,"Введите точное название проекта для подтверждения удаления файлов с диска")
        await sandbox_service.stop_project_sandboxes(project_dir=proj_dir,project_name=name)
        await asyncio.to_thread(host_service_manager.HOST_SERVICE_MANAGER.purge_project_services,name,proj_dir)
        try:
            archived = await asyncio.to_thread(brain_reader.remove_or_archive_project_directory,proj_dir,
                allowed_parent=proj_dir.parent,delete_files=body.delete_files,
                archive_owner_slug=config.sanitize_user_folder_slug(current_user))
        except (OSError, RuntimeError) as exc:
            raise HTTPException(409,str(exc))
        result = await database.delete_project_records(project_name=name,owner_id=current_user["id"],
            is_shared=resolved["is_shared"],shared_id=resolved.get("shared_id", ""),chats_action=body.chats_action)
        deleted_chats = result.pop("deleted_chats")
        deleted_ids = [c["id"] for c in deleted_chats]
        # Cleanup uses original chat metadata, including protected Desktop source IDs.
        if deleted_ids:
            from .storage_maintenance import cleanup_deleted_chat
            for c in deleted_chats:
                cid = c["id"]
                CHAT_TURN_QUEUES.pop(cid,None)
                CHAT_QUEUE_PAUSED.discard(cid)
                await cleanup_deleted_chat(c,shared=resolved["is_shared"])
        if resolved["is_shared"]:
            await sandbox_service.sync_all_users_sandbox_cli_files()
        brain_reader.invalidate_projects_list_cache()
        payload = {"type":"project_deleted","project_name":name,"chats_action":body.chats_action,"deleted_chat_ids":deleted_ids,**result}
        await _broadcast_project_mutation(payload,current_user,resolved["is_shared"],resolved.get("member_ids", []))
        return {"ok":True,**payload,"archived_disk_path":str(archived) if archived and current_user.get("role")=="admin" else ""}


@app.post("/api/projects/create")
async def api_create_project(
    body: CreateProjectRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    is_admin = current_user.get("role") == "admin"
    if body.is_shared:
        if not is_admin:
            raise HTTPException(
                status_code=403,
                detail="Создавать общие проекты может только администратор",
            )
        try:
            sp = await database.create_shared_project(
                name=body.name,
                description=body.description,
                execution_mode=body.execution_mode or "docker",
                member_ids=body.member_ids,
                created_by=current_user["id"],
                storage_location=body.storage_location,
            )
            upgraded_ids = await _maybe_upgrade_shared_project_members_docker(
                sp.get("member_ids") or [],
                sp.get("execution_mode", "docker"),
                body.upgrade_members_docker,
            )
            await sandbox_service.sync_all_users_sandbox_cli_files()
            return {
                "ok": True,
                "project": {**sp, "is_shared": True},
                "upgraded_member_ids": upgraded_ids,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    try:
        proj = await asyncio.to_thread(
            brain_reader.create_personal_project_folder,
            project_name=body.name,
            user=current_user,
            description=body.description,
        )
        return {"ok": True, "project": proj}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/desktop/conversations")
async def api_desktop_conversations(
    project_name: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    convs, imported_map = await asyncio.gather(
        asyncio.to_thread(
            brain_reader.list_desktop_conversations,
            project_name=project_name,
            query=query,
        ),
        database.get_imported_desktop_map(owner_id=_admin["id"]),
    )
    for c in convs:
        imp = imported_map.get(c.get("conversation_id", ""))
        c["imported_chat_id"] = imp["id"] if imp else ""
        c["imported_chat_title"] = imp["title"] if imp else ""
    return {"conversations": convs}


@app.get("/api/desktop/conversations/{conversation_id}")
async def api_desktop_transcript(
    conversation_id: str,
    _admin: dict[str, Any] = Depends(auth.require_admin),
):
    messages, existing = await asyncio.gather(
        asyncio.to_thread(brain_reader.read_transcript_messages, conversation_id),
        database.find_chat_by_source_desktop_id(conversation_id, owner_id=_admin["id"]),
    )
    return {
        "conversation_id": conversation_id,
        "messages": messages,
        "imported_chat_id": existing["id"] if existing else "",
    }


@app.post("/api/desktop/conversations/{conversation_id}/import")
async def api_import_desktop_conversation(
    conversation_id: str,
    body: ImportDesktopChatRequest,
    admin_user: dict[str, Any] = Depends(auth.require_admin),
):
    if not body.force_copy:
        existing = await database.find_chat_by_source_desktop_id(
            conversation_id, owner_id=admin_user["id"]
        )
        if existing:
            existing["is_generating"] = existing["id"] in active_generations
            return {"chat": existing, "already_imported": True}

    messages = await asyncio.to_thread(
        brain_reader.read_transcript_messages, conversation_id
    )
    if not messages:
        raise HTTPException(status_code=404, detail="История диалога пуста или не найдена")

    kind = "project" if body.project_name else "chat"
    proj_path = ""
    if kind == "project":
        proj_dir = brain_reader.resolve_project_dir(body.project_name)
        if proj_dir:
            proj_path = str(proj_dir)

    all_models = await fetch_available_models()
    chosen_model = resolve_active_model_id(
        body.model or pick_default_model_id(all_models),
        all_models,
        cli_online=bool(get_cli_auth_status().get("ok", True)),
    )
    title = body.title.strip() or "Импорт из Antigravity Desktop"
    new_chat = await database.create_chat(
        owner_id=admin_user["id"],
        kind=kind,
        project_name=body.project_name if kind == "project" else "",
        project_path=proj_path,
        title=title,
        model=chosen_model,
        access_mode="read_only",
        source_desktop_id=conversation_id,
    )
    await database.add_messages_bulk(
        new_chat["id"],
        user_id=admin_user["id"],
        messages=messages,
    )
    refreshed = await database.get_chat(new_chat["id"]) or new_chat
    refreshed["is_generating"] = False
    await ws_manager.broadcast_for_chat(refreshed, {"type": "chat_created", "chat": refreshed})
    return {"chat": refreshed, "already_imported": False}


@app.post("/api/projects/export-idea")
async def api_export_idea(
    body: ExportIdeaRequest,
    current_user: dict[str, Any] = Depends(auth.get_current_user),
):
    if not body.content.strip():
        raise HTTPException(status_code=400, detail="Текст идеи не может быть пустым")
    is_admin = current_user.get("role") == "admin"
    shared_projs = await database.list_shared_projects(
        for_user_id=current_user["id"], is_admin=is_admin
    )
    resolved = brain_reader.resolve_user_project(
        body.project_name, current_user, shared_projs
    )
    if not resolved:
        raise HTTPException(status_code=404, detail="Проект не найден или доступ запрещён")

    try:
        res = await asyncio.to_thread(
            brain_reader.export_idea_to_project,
            project_name=resolved["name"],
            target_file=body.target_file,
            idea_title=body.idea_title,
            content=body.content,
            append=body.append,
            resolved_dir=resolved["path"],
            hide_host_path=not is_admin,
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# WebSocket Live Streaming & Multi-Device Sync
# ---------------------------------------------------------------------------


async def _resolve_allowed_referenced_chats(
    target_chat: dict[str, Any],
    trigger_user: dict[str, Any],
    ref_specs: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Enforce Zero-Trust RBAC and strict contour isolation (project <-> chat) for @chat mentions."""
    if not target_chat or not trigger_user or not ref_specs:
        return []
    target_id = str(target_chat.get("id") or "").strip()
    target_kind = str(target_chat.get("kind") or "chat").strip()
    target_proj = str(target_chat.get("project_name") or "").strip().lower()
    user_id = str(trigger_user.get("id") or "").strip()

    allowed_list: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for spec in ref_specs:
        ref_id = str(spec.get("id") or "").strip()
        raw_sc = str(spec.get("scope") or "full").strip().lower()
        scope = raw_sc if raw_sc in _VALID_REF_CHAT_SCOPES else "full"
        if not ref_id or ref_id == target_id or ref_id in seen_ids:
            continue
        ref_chat = await database.get_chat(ref_id)
        if not ref_chat:
            continue
        ref_kind = str(ref_chat.get("kind") or "chat").strip()
        if target_kind == "project":
            if ref_kind != "project":
                continue
            if (
                not target_proj
                or str(ref_chat.get("project_name") or "").strip().lower() != target_proj
            ):
                continue
        elif target_kind == "chat":
            if ref_kind != "chat":
                continue
            if not user_id or str(ref_chat.get("owner_id") or "").strip() != user_id:
                continue
        else:
            continue
        ref_allowed, _ = await _get_chat_access_context(ref_chat, trigger_user)
        if not ref_allowed:
            continue
        seen_ids.add(ref_id)
        allowed_list.append(
            {
                "id": str(ref_chat["id"]),
                "title": str(ref_chat.get("title") or "Без названия"),
                "access_mode": str(ref_chat.get("access_mode") or "read_only"),
                "icon": str(ref_chat.get("icon") or ""),
                "scope": scope,
            }
        )
        if len(allowed_list) >= 3:
            break
    return allowed_list


async def _is_duplicate_hq_return(
    target_chat_id: str,
    hq_src: Optional[dict[str, Any]],
    *,
    check_queue: bool = True,
) -> bool:
    if not hq_src:
        return False
    ruid = str(hq_src.get("return_uid") or "").strip()[:64]
    if not ruid or not target_chat_id:
        return False
    if (target_chat_id, ruid) in _INFLIGHT_HQ_RETURN_UIDS:
        return True
    if check_queue:
        for q_it in CHAT_TURN_QUEUES.get(target_chat_id) or []:
            if any(str(q_hq.get("return_uid") or "").strip()[:64] == ruid for q_hq in _sanitize_hq_return_sources(q_it)):
                return True
    return await database.has_message_hq_return_uid(
        str(hq_src.get("chat_id") or ""),
        int(hq_src.get("message_id") or 0),
        target_chat_id,
        ruid,
    )


async def _apply_turn_preferred_accounts(
    chat: dict[str, Any],
    preferred_accounts: Optional[dict[str, str]],
    trigger_user: dict[str, Any],
    shared_members: Optional[set[str]],
) -> dict[str, Any]:
    clean_prefs = _sanitize_turn_preferred_accounts(preferred_accounts)
    if not clean_prefs:
        return chat
    is_admin = trigger_user.get("role") == "admin"
    uid = str(trigger_user.get("id") or "")
    updates: dict[str, str] = {}

    if "preferred_agy_account_id" in clean_prefs and is_admin:
        val = clean_prefs["preferred_agy_account_id"]
        if val:
            prof = await database.get_account_profile_by_id(val)
            val = val if prof else ""
        if val != str(chat.get("preferred_agy_account_id") or ""):
            updates["preferred_agy_account_id"] = val

    if "preferred_codex_account_id" in clean_prefs and uid:
        val = clean_prefs["preferred_codex_account_id"]
        if val:
            acc = await database.get_user_chatgpt_account(uid, val)
            val = val if acc else ""
        if val != str(chat.get("preferred_codex_account_id") or ""):
            updates["preferred_codex_account_id"] = val

    if "preferred_claude_account_id" in clean_prefs and uid:
        val = clean_prefs["preferred_claude_account_id"]
        if val:
            acc = await database.get_user_claude_account(uid, val)
            val = val if acc else ""
        if val != str(chat.get("preferred_claude_account_id") or ""):
            updates["preferred_claude_account_id"] = val

    if not updates:
        return chat

    updated = await database.update_chat(str(chat["id"]), **updates)
    if updated:
        chat = updated
        await ws_manager.broadcast_for_chat(
            chat,
            {"type": "chat_updated", "chat": chat},
            shared_member_ids=shared_members,
        )
    return chat


async def _run_turn_task(
    *,
    chat_id: str,
    prompt: str,
    attachments: list[dict[str, Any]],
    override_model: Optional[str],
    override_access_mode: Optional[str],
    trigger_user: dict[str, Any],
    initial_kind: str = "message",
    referenced_chat_ids: Optional[list[str]] = None,
    referenced_chats: Optional[list[dict[str, Any]]] = None,
    live_voice: bool = False,
    live_session_id: str = "",
    tts_voice: str = "",
    tts_rate: str = "",
    resume_message_id: Optional[int] = None,
    handoff_meta: Optional[dict[str, Any]] = None,
    hq_return_source: Optional[dict[str, Any]] = None,
    hq_return_sources: Optional[list[dict[str, Any]]] = None,
    preferred_accounts: Optional[dict[str, str]] = None,
) -> None:
    cur_hq_return_sources = hq_return_sources
    cur_kind = initial_kind or "message"
    cur_prompt = prompt
    cur_attachments = attachments
    cur_model = override_model
    cur_access_mode = override_access_mode
    cur_user = trigger_user
    cur_ref_ids = list(referenced_chat_ids or [])
    cur_ref_specs = _sanitize_referenced_chat_specs(
        referenced_chats, cur_ref_ids, exclude_chat_id=chat_id
    )
    cur_live_voice = bool(live_voice)
    cur_live_session_id = str(live_session_id or "")
    cur_tts_voice = str(tts_voice or "")
    cur_tts_rate = str(tts_rate or "")
    cur_resume_msg_id: Optional[int] = (
        int(resume_message_id) if resume_message_id is not None else None
    )
    cur_handoff_meta: Optional[dict[str, Any]] = _sanitize_handoff_meta(handoff_meta)
    cur_hq_return_source: Optional[dict[str, Any]] = _sanitize_hq_return_source(
        hq_return_source
    )
    cur_pref_accounts: dict[str, str] = _sanitize_turn_preferred_accounts(
        preferred_accounts
    )
    cur_task = asyncio.current_task()
    if cur_task is not None:
        ACTIVE_TURN_TASKS[chat_id] = cur_task
    try:
        while True:
            CHAT_QUEUE_PAUSED.discard(chat_id)
            if cur_kind == "compact":
                clear_chat_turn(chat_id)
                try:
                    await _execute_chat_compaction(
                        chat_id, cur_user, from_queue=True
                    )
                except HTTPException as hexc:
                    if not (
                        hexc.status_code == 400
                        and bool(CHAT_TURN_QUEUES.get(chat_id))
                    ):
                        await ws_manager.broadcast_user(
                            cur_user["id"],
                            {
                                "type": "ws_error",
                                "chat_id": chat_id,
                                "error": str(hexc.detail),
                            },
                        )
                except Exception as exc:
                    await ws_manager.broadcast_user(
                        cur_user["id"],
                        {
                            "type": "ws_error",
                            "chat_id": chat_id,
                            "error": f"Ошибка сжатия из очереди: {exc}",
                        },
                    )
            else:
                register_chat_turn(chat_id, user_id=cur_user["id"])
                await _run_turn_task_inner(
                    chat_id=chat_id,
                    prompt=cur_prompt,
                    attachments=cur_attachments,
                    override_model=cur_model,
                    override_access_mode=cur_access_mode,
                    trigger_user=cur_user,
                    referenced_chat_ids=cur_ref_ids,
                    referenced_chats=cur_ref_specs,
                    live_voice=cur_live_voice,
                    live_session_id=cur_live_session_id,
                    tts_voice=cur_tts_voice,
                    tts_rate=cur_tts_rate,
                    resume_message_id=cur_resume_msg_id,
                    handoff_meta=cur_handoff_meta,
                    hq_return_source=cur_hq_return_source,
                    hq_return_sources=cur_hq_return_sources,
                    preferred_accounts=cur_pref_accounts,
                )
            cur_resume_msg_id = None
            cur_handoff_meta = None
            cur_hq_return_source = None
            cur_hq_return_sources = None
            cur_pref_accounts = {}

            next_item: Optional[dict[str, Any]] = None
            next_user: Optional[dict[str, Any]] = None
            while True:
                if chat_id in CHAT_QUEUE_PAUSED or chat_id in COMPACTING_CHATS:
                    break
                q = CHAT_TURN_QUEUES.get(chat_id)
                if not q:
                    CHAT_TURN_QUEUES.pop(chat_id, None)
                    break
                # Keep chat marked as generating across the await gap before the next turn starts
                register_chat_turn(chat_id, user_id=cur_user["id"])
                cand = q.pop(0)
                if not q:
                    CHAT_TURN_QUEUES.pop(chat_id, None)
                await _broadcast_chat_queue(chat_id)
                cand_uid = str(cand.get("user_id") or "").strip()
                u = await database.get_user_by_id(cand_uid) if cand_uid else None
                if u and u.get("is_active", False):
                    chk_chat = await database.get_chat(chat_id)
                    if not chk_chat:
                        CHAT_TURN_QUEUES.pop(chat_id, None)
                        break
                    allowed_u, _ = await _get_chat_access_context(chk_chat, u)
                    if allowed_u:
                        next_item = cand
                        next_user = u
                        break

            if not next_item or not next_user:
                break

            cur_kind = str(next_item.get("kind") or "message")
            cur_prompt = str(next_item.get("prompt") or "Проанализируй прикреплённые файлы.")
            cur_attachments = list(next_item.get("attachments") or [])
            cur_model = next_item.get("model")
            cur_access_mode = next_item.get("access_mode")
            cur_ref_ids = list(next_item.get("referenced_chat_ids") or [])
            cur_ref_specs = _sanitize_referenced_chat_specs(
                next_item.get("referenced_chats"), cur_ref_ids, exclude_chat_id=chat_id
            )
            cur_live_voice = bool(next_item.get("live_voice", False))
            cur_live_session_id = str(next_item.get("live_session_id") or "")
            cur_tts_voice = str(next_item.get("tts_voice") or "")
            cur_tts_rate = str(next_item.get("tts_rate") or "")
            cur_handoff_meta = _sanitize_handoff_meta(next_item.get("handoff_meta"))
            cur_hq_return_source = _sanitize_hq_return_source(
                next_item.get("hq_return_source")
            )
            cur_hq_return_sources = _sanitize_hq_return_sources(next_item)
            cur_pref_accounts = _sanitize_turn_preferred_accounts(next_item)
            cur_user = next_user
    finally:
        ACTIVE_TURN_TASKS.pop(chat_id, None)
        clear_chat_turn(chat_id)


async def _maybe_generate_live_outline_title_task(
    *,
    user_id: str,
    chat_id: str,
    message_id: int,
    raw_content: str,
    chat_title: str = "",
    project_name: str = "",
) -> None:
    """Fire-and-forget Stage 2 AI outline title generation via OpenRouter (never affects main turn)."""
    try:
        if not database.extract_outline_title_source_text(raw_content):
            return
        cfg = await database.get_user_openrouter_config(user_id)
        if not (
            bool(cfg.get("has_key"))
            and bool(cfg.get("is_enabled"))
            and bool(cfg.get("smart_titles_enabled"))
        ):
            return
        ai_title = await openrouter_service.generate_single_outline_title(
            user_id,
            chat_id=chat_id,
            chat_title=chat_title,
            project_name=project_name,
            raw_content=raw_content,
        )
        if not ai_title:
            return
        updated = await database.set_message_auto_title(
            chat_id,
            int(message_id),
            ai_title,
            respect_custom=True,
        )
        if updated is None:
            return
        fresh_chat = await database.get_chat(chat_id)
        if not fresh_chat:
            return
        sp_members: Optional[set[str]] = None
        if fresh_chat.get("kind") == "project" and fresh_chat.get("project_name"):
            sp_obj = await database.get_shared_project_by_name(str(fresh_chat["project_name"]))
            if sp_obj:
                sp_members = set(sp_obj.get("member_ids") or [])
        await ws_manager.broadcast_for_chat(
            fresh_chat,
            {
                "type": "message_title_updated",
                "chat_id": chat_id,
                "message_id": int(message_id),
                "custom_title": updated["custom_title"],
                "auto_title": updated["auto_title"],
            },
            shared_member_ids=sp_members,
        )
    except Exception:
        pass


async def _run_turn_task_inner(
    *,
    chat_id: str,
    prompt: str,
    attachments: list[dict[str, Any]],
    override_model: Optional[str],
    override_access_mode: Optional[str],
    trigger_user: dict[str, Any],
    referenced_chat_ids: Optional[list[str]] = None,
    referenced_chats: Optional[list[dict[str, Any]]] = None,
    live_voice: bool = False,
    live_session_id: str = "",
    tts_voice: str = "",
    tts_rate: str = "",
    resume_message_id: Optional[int] = None,
    handoff_meta: Optional[dict[str, Any]] = None,
    hq_return_source: Optional[dict[str, Any]] = None,
    hq_return_sources: Optional[list[dict[str, Any]]] = None,
    preferred_accounts: Optional[dict[str, str]] = None,
) -> None:
    sources = _sanitize_hq_return_sources({"hq_return_source": hq_return_source, "hq_return_sources": hq_return_sources})
    clean_hq_src = sources[0] if sources else None
    tracked_hq_keys = []
    if resume_message_id is None:
        for src in sources:
            if await _is_duplicate_hq_return(chat_id, src, check_queue=False):
                return
        if any((chat_id, src["return_uid"]) in _INFLIGHT_HQ_RETURN_UIDS
               for src in sources if src.get("return_uid")):
            return
        for src in sources:
            if src.get("return_uid"):
                key = (chat_id, src["return_uid"])
                tracked_hq_keys.append(key)
                _INFLIGHT_HQ_RETURN_UIDS.add(key)
    try:
        await _run_turn_task_inner_body(
            chat_id=chat_id,
            prompt=prompt,
            attachments=attachments,
            override_model=override_model,
            override_access_mode=override_access_mode,
            trigger_user=trigger_user,
            referenced_chat_ids=referenced_chat_ids,
            referenced_chats=referenced_chats,
            live_voice=live_voice,
            live_session_id=live_session_id,
            tts_voice=tts_voice,
            tts_rate=tts_rate,
            resume_message_id=resume_message_id,
            handoff_meta=handoff_meta,
            clean_hq_src=clean_hq_src,
            clean_hq_sources=sources,
            preferred_accounts=preferred_accounts,
        )
    finally:
        for key in tracked_hq_keys:
            _INFLIGHT_HQ_RETURN_UIDS.discard(key)


def _expand_reply_quote_for_turn_prompt(prompt, existing_msgs, chat, target_engine, free_tokens):
    tail = [m for m in existing_msgs if m.get("role") != "system" and m.get("status") != "compacted"][-2:]
    session_key = {"agy": "agy_conversation_id", "codex": "codex_thread_id", "claude": "claude_session_id"}[target_engine]
    # A changed account cannot reuse the old native session.
    account_key = {"agy": "agy", "codex": "codex", "claude": "claude"}[target_engine]
    changed_account = bool(chat.get(f"preferred_{account_key}_account_id") and
                           chat.get(f"preferred_{account_key}_account_id") != chat.get(f"{account_key}_account_id"))
    def expand(match):
        attrs = match.group(1)
        if not re.search(r'\bmode="message"', attrs):
            return match.group(0)
        mid = re.search(r'\bmsg_id="(\d+)"', attrs)
        if not mid:
            return match.group(0)
        mid = int(mid.group(1))
        source = next((m for m in existing_msgs if int(m.get("id") or 0) == mid), None)
        if not source:
            return match.group(0)
        already_live_in_tail = (chat.get(session_key) and chat.get("last_engine") == target_engine
            and not changed_account and mid > int(chat.get("compacted_up_to_msg_id") or 0)
            and any(int(m.get("id") or 0) == mid for m in tail))
        if already_live_in_tail:
            return match.group(0)
        clean = database._strip_injected_prompt_tags(str(source.get("content") or ""))
        cap = min(80_000, max(4_000, int(free_tokens * 2.5)))
        clean = clean[:cap].replace("</REPLY_QUOTE>", "[/REPLY_QUOTE]")
        return f"<REPLY_QUOTE{attrs}>\n{clean}\n</REPLY_QUOTE>"
    return re.sub(r"<REPLY_QUOTE\b([^>]*)>[\s\S]*?</REPLY_QUOTE>", expand, prompt, flags=re.I)


async def _run_turn_task_inner_body(
    *,
    chat_id: str,
    prompt: str,
    attachments: list[dict[str, Any]],
    override_model: Optional[str],
    override_access_mode: Optional[str],
    trigger_user: dict[str, Any],
    referenced_chat_ids: Optional[list[str]] = None,
    referenced_chats: Optional[list[dict[str, Any]]] = None,
    live_voice: bool = False,
    live_session_id: str = "",
    tts_voice: str = "",
    tts_rate: str = "",
    resume_message_id: Optional[int] = None,
    handoff_meta: Optional[dict[str, Any]] = None,
    clean_hq_src: Optional[dict[str, Any]] = None,
    clean_hq_sources: Optional[list[dict[str, Any]]] = None,
    preferred_accounts: Optional[dict[str, str]] = None,
) -> None:
    chat = await database.get_chat(chat_id)
    if not chat:
        return

    clean_hq_sources = clean_hq_sources or ([clean_hq_src] if clean_hq_src else [])
    prev_chat_updated_at = str(chat.get("updated_at") or "")
    prev_chat_model = str(chat.get("model") or "")
    is_admin = trigger_user.get("role") == "admin"
    allowed, sp = await _get_chat_access_context(chat, trigger_user)
    if not allowed:
        return
    shared_members: Optional[set[str]] = set(sp.get("member_ids") or []) if sp else None
    if preferred_accounts:
        chat = await _apply_turn_preferred_accounts(
            chat, preferred_accounts, trigger_user, shared_members
        )

    resolved_proj_dir: Optional[Path] = None
    resolved_proj_obj: Optional[dict[str, Any]] = None
    proj_scope_key = ""
    proj_owner_id = str(chat.get("owner_id") or trigger_user["id"] or "admin")
    proj_changelog: list[dict[str, Any]] = []
    turn_extra_dirs: list[Path] = []
    turn_disable_default_guardrails = False
    recent_proj_chats: list[dict[str, Any]] = []
    ref_blocks: list[str] = []
    ref_meta_chips: list[dict[str, str]] = []

    can_user_host = bool(
        is_admin or str(trigger_user.get("max_access_mode") or "") == "host_full"
    )
    exec_mode = "host" if can_user_host else "sandbox_files"
    if chat["kind"] == "project" and chat["project_name"]:
        shared_projs = await database.list_shared_projects(
            for_user_id=trigger_user["id"], is_admin=is_admin
        )
        resolved = brain_reader.resolve_user_project(
            chat["project_name"], trigger_user, shared_projs
        )
        if not resolved:
            await ws_manager.broadcast_user(
                trigger_user["id"],
                {
                    "type": "ws_error",
                    "chat_id": chat_id,
                    "error": f"Папка проекта '{chat['project_name']}' недоступна.",
                },
            )
            return
        resolved_proj_obj = resolved
        resolved_proj_dir = resolved["path"]
        exec_mode = brain_reader.resolve_effective_project_execution_mode(
            trigger_user, resolved
        )
        is_shared_proj = bool(resolved.get("is_shared"))
        proj_scope_key = database.build_project_memory_scope_key(
            resolved["name"],
            proj_owner_id,
            is_shared=is_shared_proj,
        )
        proj_memory, recent_proj_chats = await asyncio.gather(
            database.get_project_memory(
                proj_scope_key,
                project_name=resolved["name"],
                owner_id=proj_owner_id,
            ),
            database.list_chats(
                kind="project",
                project_name=resolved["name"],
                owner_id=trigger_user["id"],
                shared_project_names=[s["name"] for s in shared_projs],
                recent_only=True,
                limit=7,
                include_context_pct=False,
                include_archived=False,
            ),
        )
        proj_changelog = list(proj_memory.get("changelog") or [])
        turn_disable_default_guardrails = bool(proj_memory.get("disable_default_guardrails"))
        raw_turn_extra_dirs = list(proj_memory.get("extra_dirs") or [])
        if raw_turn_extra_dirs:
            turn_extra_dirs = await asyncio.to_thread(
                brain_reader.validate_project_extra_dirs,
                raw_turn_extra_dirs,
                user=trigger_user,
                exec_mode=exec_mode,
                project_dir=resolved_proj_dir,
                strict=False,
            )
        if proj_memory.get("has_passport"):
            await asyncio.to_thread(
                database.sync_project_rules_file, resolved_proj_dir, proj_memory
            )

    clean_ref_specs = _sanitize_referenced_chat_specs(
        referenced_chats, referenced_chat_ids, exclude_chat_id=chat_id
    )

    all_models = await fetch_available_models()
    user_models = await _get_models_for_user(all_models, trigger_user)
    user_model_ids = {m["id"] for m in user_models}
    all_model_ids = {m["id"] for m in all_models}
    cli_ok = bool(get_cli_auth_status().get("ok", True))

    raw_model = (override_model or chat["model"] or pick_default_model_id(user_models)).strip()
    if (
        not is_admin
        and sp is not None
        and not override_model
        and user_model_ids
        and raw_model not in user_model_ids
    ):
        # In shared project, if admin left an Opus/restricted or personal Codex model on the chat
        # and guest didn't override, softly fall back to the guest's closest allowed model.
        raw_model = resolve_active_model_id(raw_model, user_models, cli_online=cli_ok)

    if is_codex_model(raw_model) or is_claude_code_model(raw_model):
        model = raw_model
    elif cli_ok and all_model_ids and raw_model not in all_model_ids:
        # Deprecated / removed model ID in DB: automatically resolve to closest active model
        model = resolve_active_model_id(raw_model, user_models, cli_online=True)
    else:
        model = raw_model

    access_mode = _cap_access_mode_for_user(
        override_access_mode or chat["access_mode"] or "read_only",
        chat["kind"],
        trigger_user,
    )

    quota_block_reason = await _enforce_user_turn_quotas(trigger_user, model)
    if quota_block_reason:
        await ws_manager.broadcast_user(
            trigger_user["id"],
            {"type": "ws_error", "chat_id": chat_id, "error": quota_block_reason},
        )
        return

    # Persist model / access_mode if changed in header.
    # CRITICAL (v=70): When live_voice=True, `override_model` is an ephemeral turbo override
    # for this spoken turn ONLY — NEVER overwrite `chat["model"]` in SQLite!
    if live_voice:
        if access_mode != chat["access_mode"]:
            chat = (
                await database.update_chat(chat_id, access_mode=access_mode)
                or chat
            )
            await ws_manager.broadcast_for_chat(
                chat,
                {"type": "chat_updated", "chat": chat},
                shared_member_ids=shared_members,
            )
    elif model != chat["model"] or access_mode != chat["access_mode"]:
        chat = (
            await database.update_chat(chat_id, model=model, access_mode=access_mode)
            or chat
        )
        await ws_manager.broadcast_for_chat(
            chat,
            {"type": "chat_updated", "chat": chat},
            shared_member_ids=shared_members,
        )

    existing_msgs = await database.get_messages(chat_id, include_thinking=False)
    if resume_message_id is not None and not clean_ref_specs:
        non_sys_for_refs = [m for m in existing_msgs if m.get("role") != "system"]
        orig_u_for_refs = next(
            (
                m
                for m in reversed(non_sys_for_refs[:-1])
                if m.get("role") == "user"
            ),
            None,
        )
        if orig_u_for_refs and isinstance(orig_u_for_refs.get("usage"), dict):
            saved_refs = orig_u_for_refs["usage"].get("referenced_chats")
            if isinstance(saved_refs, list) and saved_refs:
                clean_ref_specs = _sanitize_referenced_chat_specs(
                    saved_refs, None, exclude_chat_id=chat_id
                )

    if clean_ref_specs:
        allowed_refs = await _resolve_allowed_referenced_chats(
            chat, trigger_user, clean_ref_specs
        )
        if allowed_refs:
            ctx_now = compute_chat_context_stats(
                {**chat, "model": model, "is_generating": False}, existing_msgs
            )
            max_ctx_tok = int(ctx_now.get("max_tokens") or 200_000)
            used_ctx_tok = int(ctx_now.get("used_tokens") or 0)
            free_tokens = max(
                0,
                max_ctx_tok
                - used_ctx_tok
                - agy_engine.estimate_text_tokens(prompt)
                - 12_000,
            )
            total_ref_char_budget = max(6_000, min(180_000, int(free_tokens * 2.8)))
            requests = []
            for info in allowed_refs:
                source = next((src for src in clean_hq_sources if src["chat_id"] == info["id"]), None)
                requests.append((info, source))
            async def build_ref(info, source, cap):
                return await database.build_referenced_chat_digest(
                    info["id"], scope=info["scope"], max_chars=min(cap, 16_000) if source else cap,
                    exclude_message_id=source["message_id"] if source else None,
                    focus_latest_task_segment=bool(source))
            digests = [await build_ref(info, src, min(100_000, total_ref_char_budget)) for info, src in requests]
            lengths = [len(d["block"]) if d else 0 for d in digests]
            if sum(lengths) > total_ref_char_budget:
                caps = [0] * len(lengths)
                remaining = total_ref_char_budget
                while remaining > 0:
                    needy = [i for i, n in enumerate(lengths) if caps[i] < n]
                    if not needy:
                        break
                    share = max(1, remaining // len(needy))
                    for i in needy:
                        take = min(share, remaining, lengths[i] - caps[i])
                        caps[i] += take
                        remaining -= take
                digests = [await build_ref(info, src, caps[i]) for i, (info, src) in enumerate(requests)]
            for digest in digests:
                if digest and digest.get("block"):
                    ref_blocks.append(digest["block"])
                    ref_meta_chips.append({k: digest[k] for k in ("id", "title", "access_mode", "scope")})

    changelog_user_prompt = prompt
    turn_prompt = prompt
    turn_attachments = attachments
    effective_agy_conv_id = str(chat.get("agy_conversation_id") or "")
    saved_agy_acc_id = str(chat.get("agy_account_id") or "")
    pref_agy_acc_id = str(chat.get("preferred_agy_account_id") or "")
    resolved_agy_turn = await agy_engine.resolve_agy_account_for_chat_turn(
        model,
        preferred_account_id=pref_agy_acc_id,
        current_chat_account_id=saved_agy_acc_id,
    )
    active_agy_acc_id = str(((resolved_agy_turn.get("account") or {}).get("id")) or "")
    if (
        effective_agy_conv_id
        and active_agy_acc_id
        and (
            (saved_agy_acc_id and active_agy_acc_id != saved_agy_acc_id)
            or (
                not saved_agy_acc_id
                and not agy_engine.agy_session_db_exists(
                    effective_agy_conv_id, account_id=active_agy_acc_id
                )
            )
        )
    ):
        effective_agy_conv_id = ""
        await database.update_chat(chat_id, agy_conversation_id="")
    seed_initial_content = ""
    seed_initial_thinking = ""
    seed_initial_tools: list[dict[str, Any]] = []
    is_continue_turn = False

    if resume_message_id is not None:
        target_msg = await database.get_message_by_id(
            chat_id, int(resume_message_id), include_thinking=True
        )
        non_sys_existing = [m for m in existing_msgs if m.get("role") != "system"]
        if (
            not target_msg
            or target_msg.get("role") != "assistant"
            or target_msg.get("status") not in ("error", "cancelled")
            or not non_sys_existing
            or int(non_sys_existing[-1]["id"]) != int(resume_message_id)
        ):
            await ws_manager.broadcast_user(
                trigger_user["id"],
                {
                    "type": "ws_error",
                    "chat_id": chat_id,
                    "error": "Продолжить можно только последнее прерванное сообщение ассистента",
                },
            )
            return

        orig_user_msg = next(
            (
                m
                for m in reversed(non_sys_existing[:-1])
                if m.get("role") == "user"
            ),
            None,
        )
        if orig_user_msg:
            changelog_user_prompt = str(orig_user_msg.get("content") or prompt or "")
            if not turn_attachments:
                turn_attachments = list(orig_user_msg.get("attachments") or [])

        prev_status = str(target_msg.get("status") or "error")
        prev_msg_model = str(target_msg.get("model") or prev_chat_model or "")
        seed_initial_content = strip_interrupt_banner_from_content(
            str(target_msg.get("content") or "")
        )
        seed_initial_thinking = str(target_msg.get("thinking") or "")
        seed_initial_tools = normalize_interrupted_tool_calls(
            target_msg.get("tool_calls")
        )
        is_continue_turn = True

        updated_target = await database.update_message(
            int(resume_message_id),
            content=seed_initial_content,
            thinking=seed_initial_thinking,
            tool_calls=seed_initial_tools,
            model=model,
            status="streaming",
        )
        if not updated_target:
            return
        assistant_msg = updated_target
        msg_id = int(assistant_msg["id"])
        await _broadcast_chat_event(
            chat_id,
            chat,
            {
                "type": "assistant_start",
                "chat_id": chat_id,
                "resumed": True,
                "message": assistant_msg,
            },
            shared_member_ids=shared_members,
        )

        all_history = [
            m for m in existing_msgs if int(m.get("id") or 0) <= msg_id
        ]
        turn_prompt = _build_continue_turn_prompt(
            seed_initial_content,
            was_cancelled=(prev_status == "cancelled"),
        )
        model_family_changed = bool(
            prev_msg_model
            and (
                classify_model_family(prev_msg_model) != classify_model_family(model)
                or is_codex_model(prev_msg_model) != is_codex_model(model)
                or is_claude_code_model(prev_msg_model) != is_claude_code_model(model)
            )
        )
        if model_family_changed or not effective_agy_conv_id:
            effective_agy_conv_id = ""
    else:
        # Auto-title new chat on first prompt
        if not existing_msgs and chat["title"] in ("Новый диалог", "Без названия", ""):
            new_title = _make_auto_title(prompt or "Вложение")
            chat = await database.update_chat(chat_id, title=new_title) or chat
            await ws_manager.broadcast_for_chat(
                chat,
                {"type": "chat_updated", "chat": chat},
                shared_member_ids=shared_members,
            )

        user_usage: dict[str, Any] = (
            {"referenced_chats": ref_meta_chips} if ref_meta_chips else {}
        )
        clean_handoff_meta = _sanitize_handoff_meta(handoff_meta)
        if clean_handoff_meta:
            meta_src_cid = str(clean_handoff_meta.get("source_chat_id") or "").strip()
            if meta_src_cid and not meta_src_cid.startswith("desktop:"):
                meta_src_chat = await database.get_chat(meta_src_cid)
                meta_src_ok = False
                if meta_src_chat:
                    meta_src_ok, _ = await _get_chat_access_context(
                        meta_src_chat, trigger_user
                    )
                if not meta_src_ok:
                    clean_handoff_meta = {
                        **clean_handoff_meta,
                        "source_chat_id": "",
                        "source_msg_id": None,
                    }
            user_usage["handoff_meta"] = clean_handoff_meta
        if clean_hq_src:
            user_usage["hq_return_source"] = clean_hq_src
            user_usage["hq_return_sources"] = clean_hq_sources

        user_msg = await database.add_message(
            chat_id,
            user_id=trigger_user["id"],
            role="user",
            content=prompt,
            attachments=attachments,
            model=model,
            usage=user_usage,
            status="done",
        )
        await ws_manager.broadcast_for_chat(
            chat,
            {"type": "user_message", "chat_id": chat_id, "message": user_msg},
            shared_member_ids=shared_members,
        )
        await sync_and_broadcast_user_chat_read(
            str(trigger_user["id"]), chat_id=chat_id
        )

        try:
            live_title_task = asyncio.create_task(
                _maybe_generate_live_outline_title_task(
                    user_id=str(trigger_user["id"]),
                    chat_id=chat_id,
                    message_id=int(user_msg["id"]),
                    raw_content=prompt,
                    chat_title=str(chat.get("title") or ""),
                    project_name=str(chat.get("project_name") or ""),
                )
            )
            _LIVE_OUTLINE_TITLE_TASKS.add(live_title_task)
            live_title_task.add_done_callback(_LIVE_OUTLINE_TITLE_TASKS.discard)
        except Exception:
            pass

        if clean_hq_sources:
            accessible = []
            for source in clean_hq_sources:
                cid = source["chat_id"]
                if cid == chat_id or cid.startswith("desktop:"):
                    continue
                src_chat = await database.get_chat(cid)
                if not src_chat:
                    continue
                ok, src_sp = await _get_chat_access_context(src_chat, trigger_user)
                if not ok:
                    continue
                src_msg = await database.get_message_by_id(cid, source["message_id"])
                if src_msg:
                    accessible.append((source, src_chat, src_sp, src_msg))
            commands_count = sum(_count_executed_commands_in_msg(x[3]) for x in accessible)
            if any(src["chat_id"].startswith("desktop:") for src in clean_hq_sources):
                commands_count += int((clean_handoff_meta or {}).get("commands_count") or 0)
            review_mode = clean_hq_sources[0].get("review_mode", "fast")
            diff_block, diff_status = "", "no_paths"
            if review_mode == "review" and resolved_proj_dir and accessible:
                union_msg = {"tool_calls": [tc for x in accessible for tc in (x[3].get("tool_calls") or [])]}
                if _build_hq_return_git_diff_block is not _DEFAULT_BUILD_HQ_DIFF_BLOCK:
                    diff_block = await _build_hq_return_git_diff_block(Path(resolved_proj_dir), union_msg)
                    diff_status = "attached" if diff_block else "clean"
                else:
                    diff_block, diff_status = await _build_hq_return_git_diff_with_status(Path(resolved_proj_dir), union_msg)
            if not is_continue_turn:
                turn_prompt += _build_hq_acceptance_directive(review_mode, commands_count=commands_count,
                                                            diff_status=diff_status, diff_block=diff_block)
            for source, src_chat, src_sp, src_msg in accessible:
                hq_returns = await database.append_message_hq_return(
                    source["chat_id"], source["message_id"],
                    {"target_chat_id": chat_id, "target_title": chat.get("title") or "Штаб",
                     "target_icon": chat.get("icon") or "🏠", "project_name": chat.get("project_name") or "",
                     "target_msg_id": int(user_msg["id"]), "return_uid": source.get("return_uid") or ""})
                if hq_returns is not None:
                    await ws_manager.broadcast_for_chat(src_chat,
                        {"type": "message_hq_return_updated", "chat_id": source["chat_id"],
                         "message_id": source["message_id"], "hq_returns": hq_returns},
                        shared_member_ids=set(src_sp.get("member_ids") or []) if src_sp else None)

        assistant_msg = await database.add_message(
            chat_id,
            user_id=trigger_user["id"],
            role="assistant",
            content="",
            thinking="",
            tool_calls=[],
            model=model,
            status="streaming",
        )
        msg_id = assistant_msg["id"]
        await ws_manager.broadcast_for_chat(
            chat,
            {"type": "assistant_start", "chat_id": chat_id, "message": assistant_msg},
            shared_member_ids=shared_members,
        )
        all_history = existing_msgs + [user_msg]
    allow_hub_or = await openrouter_service.is_user_openrouter_bridge_active(trigger_user["id"])
    if allow_hub_or:
        await openrouter_service.sync_user_openrouter_cli_files(trigger_user["id"])
    if sandbox_service.is_user_sandbox_eligible(trigger_user):
        turn_docker_status = await check_docker_status()
        turn_sbx_ready = bool(turn_docker_status.get("sandbox_mcp_ready"))
        turn_admin_has_docker = bool(exec_mode == "docker")
        if not turn_admin_has_docker and trigger_user.get("role") == "admin":
            try:
                all_sp = await database.list_shared_projects(is_admin=True)
                turn_admin_has_docker = any(
                    str(s.get("execution_mode") or "docker").strip().lower() == "docker"
                    for s in all_sp
                )
            except Exception:
                turn_admin_has_docker = False
        await asyncio.to_thread(
            sandbox_service.sync_user_sandbox_cli_files,
            trigger_user,
            sandbox_ready=turn_sbx_ready,
            admin_has_docker_projects=turn_admin_has_docker,
        )
    if host_service_manager.is_user_host_eligible(trigger_user):
        await asyncio.to_thread(
            host_service_manager.sync_user_host_cli_files,
            trigger_user,
        )
    if (
        not is_claude_code_model(model)
        and not is_codex_model(model)
        and chat["kind"] == "project"
        and (
            access_mode in ("read_only", "write_files")
            or exec_mode != "host"
            or not can_user_host
        )
    ):
        await asyncio.to_thread(
            workspace_mcp.sync_user_files_cli_files,
            trigger_user,
        )

    tts_chunker: Optional[tts_service.LiveSentenceChunker] = None
    tts_queue: Optional[asyncio.Queue[Optional[str]]] = None
    tts_worker_task: Optional[asyncio.Task[int]] = None
    tts_cancelled_notified = False
    eff_tts_voice = tts_service.resolve_tts_voice(tts_voice)
    eff_tts_rate = tts_service.normalize_tts_rate(tts_rate or "+15%")

    if live_voice:
        tts_chunker = tts_service.LiveSentenceChunker(max_total_chars=750)
        tts_queue = asyncio.Queue()

        async def _tts_sender_worker() -> int:
            assert tts_queue is not None
            seq = 0
            while True:
                spoken_piece = await tts_queue.get()
                if spoken_piece is None:
                    break
                piece = str(spoken_piece or "").strip()
                if not piece:
                    continue
                try:
                    mp3_bytes = await tts_service.synthesize_tts_mp3(
                        piece,
                        voice=eff_tts_voice,
                        rate=eff_tts_rate,
                        already_sanitized=True,
                    )
                    if not mp3_bytes:
                        continue
                    audio_b64 = base64.b64encode(mp3_bytes).decode("ascii")
                    await ws_manager.broadcast_user(
                        trigger_user["id"],
                        {
                            "type": "live_tts_chunk",
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "live_session_id": live_session_id,
                            "seq": seq,
                            "mime": "audio/mpeg",
                            "audio_b64": audio_b64,
                            "spoken_text": piece,
                        },
                    )
                    seq += 1
                except asyncio.CancelledError:
                    raise
                except Exception as tts_exc:
                    voice.logger.debug("[live-tts] chunk synthesis skipped: %s", tts_exc)
            return seq

        tts_worker_task = asyncio.create_task(_tts_sender_worker())

    async def _abort_live_tts() -> None:
        nonlocal tts_cancelled_notified
        if not live_voice or tts_cancelled_notified:
            return
        tts_cancelled_notified = True
        if tts_worker_task is not None and not tts_worker_task.done():
            tts_worker_task.cancel()
            try:
                await tts_worker_task
            except (asyncio.CancelledError, Exception):
                pass
        await ws_manager.broadcast_user(
            trigger_user["id"],
            {
                "type": "live_tts_cancelled",
                "chat_id": chat_id,
                "message_id": msg_id,
                "live_session_id": live_session_id,
            },
        )

    async def _load_current_msg_usage() -> dict[str, Any]:
        cur_m = await database.get_message_by_id(
            chat_id, int(msg_id), include_thinking=False
        )
        return dict((cur_m or {}).get("usage") or {})

    use_claude_turn = is_claude_code_model(model)
    use_codex_turn = is_codex_model(model) and not use_claude_turn
    target_engine = "claude" if use_claude_turn else ("codex" if use_codex_turn else "agy")
    prev_last_engine = str(chat.get("last_engine") or "agy").strip().lower()
    effective_codex_thread_id = ""
    effective_codex_account_id = ""
    effective_claude_session_id = ""
    effective_claude_account_id = ""

    pref_claude_acc_id = str(chat.get("preferred_claude_account_id") or "")
    pref_codex_acc_id = str(chat.get("preferred_codex_account_id") or "")

    if use_claude_turn:
        # Three-way session bridge: resume native Claude session if this tab's resolved Claude account matches.
        saved_cl_sid = str(chat.get("claude_session_id") or "")
        saved_cl_aid = str(chat.get("claude_account_id") or "")
        active_cl_for_turn = await claude_engine.resolve_claude_account_for_chat_turn(
            trigger_user["id"],
            preferred_account_id=pref_claude_acc_id,
            current_chat_account_id=saved_cl_aid,
        )
        act_cl_id = str((active_cl_for_turn or {}).get("id") or "")
        if saved_cl_sid and prev_last_engine == "claude" and act_cl_id and saved_cl_aid == act_cl_id:
            effective_claude_session_id = saved_cl_sid
            effective_claude_account_id = saved_cl_aid
        else:
            effective_claude_account_id = act_cl_id
            if saved_cl_sid:
                await database.update_chat(chat_id, claude_session_id="", claude_account_id="")
    elif use_codex_turn:
        saved_c_tid = str(chat.get("codex_thread_id") or "")
        saved_c_aid = str(chat.get("codex_account_id") or "")
        active_cgpt_for_turn = await codex_engine.resolve_codex_account_for_chat_turn(
            trigger_user["id"],
            preferred_account_id=pref_codex_acc_id,
            current_chat_account_id=saved_c_aid,
        )
        act_cgpt_id = str((active_cgpt_for_turn or {}).get("id") or "")
        if (
            saved_c_tid
            and prev_last_engine == "codex"
            and act_cgpt_id
            and saved_c_aid == act_cgpt_id
            and not (is_continue_turn and not effective_agy_conv_id and prev_last_engine != "codex")
        ):
            effective_codex_thread_id = saved_c_tid
            effective_codex_account_id = saved_c_aid
        else:
            effective_codex_account_id = act_cgpt_id
            if saved_c_tid:
                await database.update_chat(chat_id, codex_thread_id="", codex_account_id="")
    else:
        # Bidirectional recovery: if the previous turn in this chat ran on ChatGPT (Codex) or Claude Code,
        # force a fresh Antigravity CLI trajectory seeded with full `hub.db` history!
        if prev_last_engine in ("codex", "claude") and effective_agy_conv_id:
            effective_agy_conv_id = ""
            await database.update_chat(chat_id, agy_conversation_id="")
        elif is_continue_turn and effective_agy_conv_id:
            turn_attachments = []

    if not is_continue_turn:
        reply_ctx = compute_chat_context_stats({**chat, "model": model, "is_generating": False}, existing_msgs)
        reply_free = max(0, int(reply_ctx.get("max_tokens") or 200_000) - int(reply_ctx.get("used_tokens") or 0)
                         - agy_engine.estimate_text_tokens(turn_prompt) - 12_000)
        reply_chat = {**chat, "last_engine": prev_last_engine,
                      "agy_conversation_id": effective_agy_conv_id,
                      "codex_thread_id": effective_codex_thread_id,
                      "claude_session_id": effective_claude_session_id}
        if model != prev_chat_model:
            reply_chat["last_engine"] = ""
        turn_prompt = _expand_reply_quote_for_turn_prompt(turn_prompt, existing_msgs, reply_chat, target_engine, reply_free)

    if prev_last_engine != target_engine:
        chat = await database.update_chat(chat_id, last_engine=target_engine) or {
            **chat,
            "last_engine": target_engine,
        }

    try:
        turn_stream = (
            stream_claude_turn(
                chat_id=chat_id,
                kind=chat["kind"],
                project_name=chat["project_name"],
                model=model,
                access_mode=access_mode,
                prompt=turn_prompt,
                attachments=turn_attachments,
                claude_session_id=effective_claude_session_id,
                claude_account_id=effective_claude_account_id,
                preferred_claude_account_id=pref_claude_acc_id,
                history_messages=all_history,
                compacted_summary=chat.get("compacted_summary", ""),
                compacted_up_to_msg_id=int(chat.get("compacted_up_to_msg_id") or 0),
                user=trigger_user,
                resolved_project_dir=resolved_proj_dir,
                execution_mode=exec_mode,
                chat_title=chat.get("title", ""),
                project_scope_key=proj_scope_key,
                project_changelog=proj_changelog,
                recent_project_chats=recent_proj_chats,
                prev_chat_updated_at=prev_chat_updated_at,
                referenced_chat_blocks=ref_blocks,
                allow_hub_openrouter=allow_hub_or,
                live_voice=live_voice,
                initial_content=seed_initial_content,
                initial_thinking=seed_initial_thinking,
                initial_tool_calls=seed_initial_tools,
                is_continue_turn=is_continue_turn,
                extra_dirs=turn_extra_dirs or None,
                **({"disable_default_guardrails": True} if turn_disable_default_guardrails else {}),
            )
            if use_claude_turn
            else stream_codex_turn(
                chat_id=chat_id,
                kind=chat["kind"],
                project_name=chat["project_name"],
                model=model,
                access_mode=access_mode,
                prompt=turn_prompt,
                attachments=turn_attachments,
                codex_thread_id=effective_codex_thread_id,
                codex_account_id=effective_codex_account_id,
                preferred_codex_account_id=pref_codex_acc_id,
                history_messages=all_history,
                compacted_summary=chat.get("compacted_summary", ""),
                compacted_up_to_msg_id=int(chat.get("compacted_up_to_msg_id") or 0),
                user=trigger_user,
                resolved_project_dir=resolved_proj_dir,
                execution_mode=exec_mode,
                chat_title=chat.get("title", ""),
                project_scope_key=proj_scope_key,
                project_changelog=proj_changelog,
                recent_project_chats=recent_proj_chats,
                prev_chat_updated_at=prev_chat_updated_at,
                referenced_chat_blocks=ref_blocks,
                allow_hub_openrouter=allow_hub_or,
                live_voice=live_voice,
                initial_content=seed_initial_content,
                initial_thinking=seed_initial_thinking,
                initial_tool_calls=seed_initial_tools,
                is_continue_turn=is_continue_turn,
                extra_dirs=turn_extra_dirs or None,
                **({"disable_default_guardrails": True} if turn_disable_default_guardrails else {}),
            )
            if use_codex_turn
            else stream_agy_turn(
                chat_id=chat_id,
                kind=chat["kind"],
                project_name=chat["project_name"],
                model=model,
                access_mode=access_mode,
                prompt=turn_prompt,
                attachments=turn_attachments,
                agy_conversation_id=effective_agy_conv_id,
                agy_account_id=saved_agy_acc_id,
                preferred_agy_account_id=pref_agy_acc_id,
                history_messages=all_history,
                compacted_summary=chat.get("compacted_summary", ""),
                compacted_up_to_msg_id=int(chat.get("compacted_up_to_msg_id") or 0),
                user=trigger_user,
                resolved_project_dir=resolved_proj_dir,
                execution_mode=exec_mode,
                chat_title=chat.get("title", ""),
                project_scope_key=proj_scope_key,
                project_changelog=proj_changelog,
                recent_project_chats=recent_proj_chats,
                prev_chat_updated_at=prev_chat_updated_at,
                referenced_chat_blocks=ref_blocks,
                allow_hub_openrouter=allow_hub_or,
                live_voice=live_voice,
                initial_content=seed_initial_content,
                initial_thinking=seed_initial_thinking,
                initial_tool_calls=seed_initial_tools,
                is_continue_turn=is_continue_turn,
                extra_dirs=turn_extra_dirs or None,
                **({"disable_default_guardrails": True} if turn_disable_default_guardrails else {}),
            )
        )
        async for evt in turn_stream:
            etype = evt.get("type")
            if etype == "agy_session":
                new_cid = evt["agy_conversation_id"]
                await database.update_chat(
                    chat_id, agy_conversation_id=new_cid, agy_account_id=evt.get("agy_account_id", active_agy_acc_id), last_engine="agy"
                )

            elif etype == "codex_session":
                session_fields: dict[str, Any] = {"last_engine": "codex"}
                thread_id = str(evt.get("codex_thread_id") or evt.get("thread_id") or "")
                account_id = str(evt.get("codex_account_id") or evt.get("account_id") or "")
                if thread_id or evt.get("reset_session"):
                    session_fields["codex_thread_id"] = thread_id
                if account_id:
                    session_fields["codex_account_id"] = account_id
                await database.update_chat(chat_id, **session_fields)

            elif etype == 'agy_failover':
                active_agy_acc_id = evt['account_id']
                await database.update_chat(chat_id, agy_conversation_id='', agy_account_id=active_agy_acc_id)
                await ws_manager.broadcast_user(trigger_user['id'], {**evt, 'chat_id':chat_id})
                from .agy_account_api import pool_state
                await pool_state()

            elif etype == "chatgpt_failover":
                await ws_manager.broadcast_user(trigger_user["id"], {
                    **evt, "type": "chatgpt_failover", "chat_id": chat_id,
                })
                asyncio.create_task(_broadcast_chatgpt_state_to_user(trigger_user["id"]))

            elif etype == "claude_session":
                await database.update_chat(
                    chat_id,
                    claude_session_id=str(evt.get("claude_session_id") or ""),
                    claude_account_id=str(evt.get("claude_account_id") or ""),
                    last_engine="claude",
                )

            elif etype == "claude_failover":
                await ws_manager.broadcast_user(
                    trigger_user["id"],
                    {
                        "type": "claude_failover",
                        "chat_id": chat_id,
                        "from_label": evt.get("from_label", ""),
                        "to_label": evt.get("to_label", ""),
                        "account_id": evt.get("account_id", ""),
                    },
                )
                asyncio.create_task(_broadcast_claude_state_to_user(trigger_user["id"]))

            elif etype == "account_session_recovered":
                asyncio.create_task(_perform_account_sync(auto=True, open_login=False))

            elif etype == "thinking_status":
                await ws_manager.broadcast_for_chat(
                    chat,
                    {
                        "type": "thinking_status",
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "active": evt["active"],
                        "seq": evt.get("seq", 0),
                    },
                    shared_member_ids=shared_members,
                )

            elif etype == "thinking_update":
                await ws_manager.broadcast_for_chat(
                    chat,
                    {
                        "type": "thinking_update",
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "thinking": evt["thinking"],
                        "seq": evt.get("seq", 0),
                    },
                    shared_member_ids=shared_members,
                )

            elif etype == "tool_step":
                await ws_manager.broadcast_for_chat(
                    chat,
                    {
                        "type": "tool_step",
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "step": database.compact_tool_calls_payload([evt["step"]])[0],
                        "seq": evt.get("seq", 0),
                    },
                    shared_member_ids=shared_members,
                )

            elif etype == "text_delta":
                delta_str = evt["delta"]
                if live_voice and tts_chunker is not None and tts_queue is not None:
                    for seg in tts_chunker.push(delta_str):
                        tts_queue.put_nowait(seg)
                await ws_manager.broadcast_for_chat(
                    chat,
                    {
                        "type": "text_delta",
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "delta": delta_str,
                        "seq": evt.get("seq", 0),
                    },
                    shared_member_ids=shared_members,
                )

            elif etype == "done":
                model_advisor_service.record_model_turn_health(model, ok=True)
                if evt.get('agy_conversation_id') and evt.get('agy_account_id'):
                    await database.update_chat(chat_id, agy_conversation_id=evt['agy_conversation_id'], agy_account_id=evt['agy_account_id'])
                if live_voice and tts_chunker is not None and tts_queue is not None:
                    # Fallback if stream produced final content without text_delta events
                    if tts_chunker.chunk_index == 0 and not tts_chunker._text_buf and evt.get("content"):
                        for seg in tts_chunker.push(str(evt["content"])):
                            tts_queue.put_nowait(seg)
                    for seg in tts_chunker.flush():
                        tts_queue.put_nowait(seg)
                    tts_queue.put_nowait(None)

                prev_db_usage = await _load_current_msg_usage()
                merged_done_usage = merge_assistant_usage(
                    prev_db_usage,
                    evt.get("usage") or {},
                    clear_last_error=True,
                )
                saved = await database.update_message(
                    msg_id,
                    content=evt["content"],
                    thinking=evt["thinking"],
                    tool_calls=evt["tool_calls"],
                    usage=merged_done_usage,
                    model=model,
                    status="done",
                )
                chat_done_kwargs: dict[str, Any] = {
                    "last_engine": "claude" if use_claude_turn else ("codex" if use_codex_turn else "agy"),
                    "touch_updated_at": True,
                }
                if use_claude_turn and evt.get("claude_session_id"):
                    chat_done_kwargs["claude_session_id"] = str(evt["claude_session_id"])
                    if evt.get("claude_account_id"):
                        chat_done_kwargs["claude_account_id"] = str(evt["claude_account_id"])
                if use_codex_turn and evt.get("codex_thread_id"):
                    chat_done_kwargs["codex_thread_id"] = str(evt["codex_thread_id"])
                    if evt.get("codex_account_id"):
                        chat_done_kwargs["codex_account_id"] = str(evt["codex_account_id"])
                updated_chat = await database.update_chat(chat_id, **chat_done_kwargs)
                ctx_stats = (
                    await database.compute_single_chat_context_stats(updated_chat)
                    if updated_chat
                    else None
                )
                if updated_chat:
                    updated_chat["is_generating"] = False
                    updated_chat["is_compacting"] = chat_id in COMPACTING_CHATS
                    updated_chat["is_shared_project"] = shared_members is not None
                    updated_chat["context_stats"] = ctx_stats
                    if ctx_stats:
                        updated_chat["context_pct"] = float(ctx_stats.get("pct") or 0.0)
                        updated_chat["context_used_tokens"] = int(
                            ctx_stats.get("used_tokens") or 0
                        )
                    updated_chat.pop("compacted_summary", None)
                term_ts = record_chat_terminal_ts(chat_id)
                if isinstance(updated_chat, dict):
                    updated_chat["last_terminal_ts_ms"] = term_ts
                turn_done_payload: dict[str, Any] = {
                    "type": "turn_done",
                    "chat_id": chat_id,
                    "message": saved,
                    "chat": updated_chat,
                    "context_stats": ctx_stats,
                }
                if live_voice:
                    turn_done_payload["live_tts_pending"] = True
                await ws_manager.broadcast_for_chat(
                    chat,
                    turn_done_payload,
                    shared_member_ids=shared_members,
                )
                if live_voice and tts_worker_task is not None:
                    sent_tts_chunks = 0
                    try:
                        sent_tts_chunks = await asyncio.wait_for(tts_worker_task, timeout=20.0)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if not tts_worker_task.done():
                            tts_worker_task.cancel()
                    await ws_manager.broadcast_user(
                        trigger_user["id"],
                        {
                            "type": "live_tts_done",
                            "chat_id": chat_id,
                            "message_id": msg_id,
                            "live_session_id": live_session_id,
                            "total_chunks": sent_tts_chunks,
                        },
                    )
                if (
                    chat.get("kind") == "project"
                    and proj_scope_key
                    and resolved_proj_obj is not None
                    and access_mode in ("write_files", "full_access")
                ):
                    auto_cl_entry = build_auto_changelog_entry_from_turn(
                        chat_id=chat_id,
                        chat_title=(updated_chat or chat).get("title") or chat["title"],
                        access_mode=access_mode,
                        user_prompt=changelog_user_prompt,
                        assistant_text=evt.get("content", ""),
                        tool_calls=evt.get("tool_calls") or [],
                        proj_dir=resolved_proj_dir,
                    )
                    if auto_cl_entry is not None:
                        try:
                            updated_mem = await database.append_project_changelog_entry(
                                proj_scope_key,
                                owner_id=proj_owner_id,
                                project_name=resolved_proj_obj["name"],
                                entry=auto_cl_entry,
                            )
                            await _broadcast_project_memory_update(
                                resolved_proj_obj, proj_owner_id, updated_mem
                            )
                        except Exception:
                            pass
                turn_uid = str(trigger_user["id"])
                _notify_gdrive_change_for_user(
                    trigger_user,
                    "turn_done",
                    is_shared=(shared_members is not None),
                )
                if use_claude_turn:
                    async def _post_claude_turn_done(uid_val: str) -> None:
                        try:
                            await _broadcast_claude_state_to_user(uid_val)
                            claude_engine.schedule_claude_live_refresh(
                                uid_val,
                                max_age_sec=60.0,
                                min_interval_sec=90.0,
                                on_updated=_broadcast_claude_state_to_user,
                            )
                        except Exception:
                            pass

                    _spawn_telemetry_task(_post_claude_turn_done(turn_uid))
                elif use_codex_turn:
                    async def _post_codex_turn_done(uid_val: str) -> None:
                        try:
                            await _broadcast_chatgpt_state_to_user(uid_val)
                            codex_engine.schedule_chatgpt_live_refresh(
                                uid_val,
                                max_age_sec=90.0,
                                min_interval_sec=90.0,
                                on_updated=_broadcast_chatgpt_state_to_user,
                            )
                        except Exception:
                            pass

                    _spawn_telemetry_task(_post_codex_turn_done(turn_uid))
                else:
                    _spawn_telemetry_task(_refresh_and_broadcast_quotas(turn_uid))

            elif etype == "cancelled":
                await _abort_live_tts()
                prev_db_usage = await _load_current_msg_usage()
                merged_cancel_usage = merge_assistant_usage(
                    prev_db_usage,
                    evt.get("usage") or {},
                )
                saved = await database.update_message(
                    msg_id,
                    content=evt.get("content", ""),
                    thinking=evt.get("thinking", ""),
                    tool_calls=normalize_interrupted_tool_calls(evt.get("tool_calls")),
                    usage=merged_cancel_usage,
                    status="cancelled",
                )
                record_chat_terminal_ts(chat_id)
                await ws_manager.broadcast_for_chat(
                    chat,
                    {
                        "type": "turn_cancelled",
                        "context_stats": await database.compute_single_chat_context_stats({**chat, "is_generating": False}),
                        "chat_id": chat_id,
                        "message": saved,
                    },
                    shared_member_ids=shared_members,
                )

            elif etype == "error":
                await _abort_live_tts()
                err_txt = evt.get("error", "Неизвестная ошибка")
                model_advisor_service.record_model_turn_health(model, ok=False, error_text=err_txt)
                if _is_auth_missing_error(err_txt) and not is_gemini_location_unsupported_error(err_txt):
                    asyncio.create_task(_perform_account_sync(auto=True, open_login=False))
                extra_hint = build_turn_error_extra_hint(err_txt, is_admin=is_admin)
                partial_content = str(evt.get("content") or "").rstrip()
                err_block = f"⚠️ **Ошибка:** {err_txt}{extra_hint}"
                final_err_content = (
                    f"{partial_content}\n\n---\n{err_block}"
                    if partial_content
                    else err_block
                )
                prev_db_usage = await _load_current_msg_usage()
                merged_err_usage = merge_assistant_usage(
                    prev_db_usage,
                    evt.get("usage") or {},
                    last_error=err_txt,
                )
                saved = await database.update_message(
                    msg_id,
                    content=final_err_content,
                    thinking=evt.get("thinking", ""),
                    tool_calls=normalize_interrupted_tool_calls(evt.get("tool_calls")),
                    usage=merged_err_usage,
                    status="error",
                )
                if CHAT_TURN_QUEUES.get(chat_id):
                    CHAT_QUEUE_PAUSED.add(chat_id)
                    await _broadcast_chat_queue(chat_id, chat)
                record_chat_terminal_ts(chat_id)
                await ws_manager.broadcast_for_chat(
                    chat,
                    {
                        "type": "turn_error",
                        "context_stats": await database.compute_single_chat_context_stats({**chat, "is_generating": False}),
                        "chat_id": chat_id,
                        "message": saved,
                        "error": err_txt,
                    },
                    shared_member_ids=shared_members,
                )
                err_uid = str(trigger_user["id"])
                if use_codex_turn:
                    _spawn_telemetry_task(_broadcast_chatgpt_state_to_user(err_uid))
                    if (
                        evt.get("quota_exhausted")
                        or codex_engine._is_chatgpt_quota_exhausted_error(err_txt)
                    ):
                        codex_engine.schedule_chatgpt_live_refresh(
                            err_uid,
                            max_age_sec=30.0,
                            min_interval_sec=30.0,
                            on_updated=_broadcast_chatgpt_state_to_user,
                        )
                elif use_claude_turn:
                    _spawn_telemetry_task(_broadcast_claude_state_to_user(err_uid))
                    if evt.get("quota_exhausted") or claude_engine.is_claude_limit_error(err_txt):
                        claude_engine.schedule_claude_live_refresh(
                            err_uid,
                            max_age_sec=30.0,
                            min_interval_sec=30.0,
                            on_updated=_broadcast_claude_state_to_user,
                        )
                else:
                    err_low = (err_txt or "").lower()
                    is_agy_quota_or_cap = (
                        bool(evt.get("quota_exhausted"))
                        or is_transient_capacity_error(err_txt)
                        or any(
                            kw in err_low
                            for kw in ("quota", "exhausted", "429", "rate limit", "resource_exhausted")
                        )
                    )
                    if is_agy_quota_or_cap:
                        _spawn_telemetry_task(
                            _refresh_and_broadcast_quotas(err_uid, force_on_quota_error=True)
                        )
    except asyncio.CancelledError:
        await _abort_live_tts()
        raise
    except Exception as exc:
        await _abort_live_tts()
        model_advisor_service.record_model_turn_health(model, ok=False, error_text=str(exc))
        snap = get_active_run_snapshot(chat_id) or {}
        partial_content = str(snap.get("content") or "").rstrip()
        err_block = f"⚠️ **Внутренняя ошибка сервера:** `{exc}`"
        final_err_content = (
            f"{partial_content}\n\n---\n{err_block}"
            if partial_content
            else err_block
        )
        prev_db_usage = await _load_current_msg_usage()
        merged_exc_usage = merge_assistant_usage(
            prev_db_usage,
            None,
            last_error=str(exc),
        )
        saved = await database.update_message(
            msg_id,
            content=final_err_content,
            thinking=snap.get("thinking", ""),
            tool_calls=normalize_interrupted_tool_calls(snap.get("tool_calls")),
            usage=merged_exc_usage,
            status="error",
        )
        if CHAT_TURN_QUEUES.get(chat_id):
            CHAT_QUEUE_PAUSED.add(chat_id)
            await _broadcast_chat_queue(chat_id, chat)
        await ws_manager.broadcast_for_chat(
            chat,
            {
                "type": "turn_error",
                "context_stats": await database.compute_single_chat_context_stats({**chat, "is_generating": False}),
                "chat_id": chat_id,
                "message": saved,
                "error": str(exc),
            },
            shared_member_ids=shared_members,
        )
    finally:
        if tts_worker_task is not None and not tts_worker_task.done():
            tts_worker_task.cancel()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ws_user = await auth.authenticate_ws_user(websocket)
    if not ws_user:
        await websocket.close(code=4401)
        return

    await ws_manager.connect(websocket, ws_user)
    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            action = data.get("action") or data.get("type")
            if action == "ping":
                await websocket.send_json({"type": "pong"})

            elif action == "mark_chat_read":
                raw_ids = data.get("chat_ids")
                await sync_and_broadcast_user_chat_read(
                    ws_user["id"],
                    chat_id=str(data.get("chat_id") or ""),
                    chat_ids=raw_ids if isinstance(raw_ids, list) else None,
                    read_all=bool(data.get("read_all", False)),
                    read_ts_ms=data.get("read_ts_ms"),
                )

            elif action == "mark_all_chats_read":
                await sync_and_broadcast_user_chat_read(
                    ws_user["id"],
                    read_all=True,
                    read_ts_ms=data.get("read_ts_ms"),
                )

            elif action in ("send_message", "send"):
                # Re-fetch fresh user record so live model/limit/block edits apply immediately!
                fresh_user = await database.get_user_by_id(ws_user["id"])
                if not fresh_user or not fresh_user.get("is_active", False):
                    await websocket.close(code=4401)
                    return

                chat_id = (data.get("chat_id") or "").strip()
                prompt = (data.get("prompt") or "").strip()
                attachments = data.get("attachments") or []
                referenced_chat_specs = _sanitize_referenced_chat_specs(
                    data.get("referenced_chats"),
                    data.get("referenced_chat_ids"),
                    exclude_chat_id=chat_id,
                )
                referenced_chat_ids = [s["id"] for s in referenced_chat_specs]
                model = data.get("model")
                access_mode = data.get("access_mode")
                compact_first = bool(data.get("compact_first", False))
                live_voice = bool(data.get("live_voice", False))
                live_session_id = str(data.get("live_session_id") or "").strip()[:64]
                tts_voice = str(data.get("tts_voice") or "").strip()[:64]
                tts_rate = str(data.get("tts_rate") or "").strip()[:24]
                handoff_meta = _sanitize_handoff_meta(data.get("handoff_meta"))
                hq_return_source = _sanitize_hq_return_source(
                    data.get("hq_return_source")
                )
                hq_return_sources = _sanitize_hq_return_sources(data)
                pref_accounts = _sanitize_turn_preferred_accounts(data)

                if not chat_id or (not prompt and not attachments and not referenced_chat_ids):
                    await websocket.send_json(
                        {"type": "ws_error", "chat_id": chat_id, "error": "Пустой запрос"}
                    )
                    continue

                chat_obj = await database.get_chat(chat_id)
                if not chat_obj:
                    await websocket.send_json(
                        {"type": "ws_error", "chat_id": chat_id, "error": "Диалог не найден"}
                    )
                    continue
                allowed, _ = await _get_chat_access_context(chat_obj, fresh_user)
                if not allowed:
                    await websocket.send_json(
                        {"type": "ws_error", "chat_id": chat_id, "error": "Доступ к этому диалогу запрещён"}
                    )
                    continue

                duplicate_return = False
                for source in hq_return_sources:
                    if await _is_duplicate_hq_return(chat_id, source, check_queue=True):
                        duplicate_return = True
                if duplicate_return:
                    continue

                effective_user_prompt = prompt or (
                    "Ознакомься с прикреплённым контекстом соседнего чата и продолжи задачу."
                    if referenced_chat_ids and not attachments
                    else "Проанализируй прикреплённые файлы."
                )

                queued_ref_chats = await _resolve_allowed_referenced_chats(
                    chat_obj, fresh_user, referenced_chat_specs
                )
                queued_ref_ids = [rc["id"] for rc in queued_ref_chats]
                author_label = (
                    fresh_user.get("display_name")
                    or fresh_user.get("username")
                    or ""
                )
                item = {
                    "id": uuid.uuid4().hex[:10],
                    "chat_id": chat_id,
                    "kind": "message",
                    "prompt": effective_user_prompt,
                    "attachments": attachments,
                    "referenced_chat_ids": queued_ref_ids,
                    "referenced_chats": queued_ref_chats,
                    "handoff_meta": handoff_meta,
                    "hq_return_source": hq_return_source,
                    "hq_return_sources": hq_return_sources,
                    "preferred_accounts": pref_accounts,
                    "model": model,
                    "access_mode": access_mode,
                    "live_voice": live_voice,
                    "live_session_id": live_session_id,
                    "tts_voice": tts_voice,
                    "tts_rate": tts_rate,
                    "user_id": fresh_user["id"],
                    "author_name": author_label,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

                def _enqueue_with_optional_compact() -> None:
                    q = CHAT_TURN_QUEUES.setdefault(chat_id, [])
                    if compact_first and not live_voice:
                        already_cmp_before = (
                            chat_id in COMPACTING_CHATS and len(q) == 0
                        ) or (len(q) > 0 and q[-1].get("kind") == "compact")
                        if not already_cmp_before:
                            q.append(
                                {
                                    "id": f"cmp_{uuid.uuid4().hex[:8]}",
                                    "chat_id": chat_id,
                                    "kind": "compact",
                                    "prompt": "Сжатие контекста без потери смысла",
                                    "attachments": [],
                                    "referenced_chat_ids": [],
                                    "referenced_chats": [],
                                    "model": None,
                                    "access_mode": None,
                                    "user_id": fresh_user["id"],
                                    "author_name": author_label,
                                    "created_at": datetime.now(timezone.utc).isoformat(),
                                }
                            )
                    q.append(item)
                    CHAT_QUEUE_PAUSED.discard(chat_id)

                active_turn_task = ACTIVE_TURN_TASKS.get(chat_id)
                if (
                    is_chat_generating(chat_id)
                    or chat_id in COMPACTING_CHATS
                    or (active_turn_task is not None and not active_turn_task.done())
                ):
                    _enqueue_with_optional_compact()
                    await _broadcast_chat_queue(chat_id, chat_obj)
                    await sync_and_broadcast_user_chat_read(
                        fresh_user["id"], chat_id=chat_id
                    )
                    continue

                if compact_first and not live_voice:
                    msgs_for_cmp = await database.get_messages(
                        chat_id, include_thinking=False
                    )
                    active_turn_task = ACTIVE_TURN_TASKS.get(chat_id)
                    if (
                        is_chat_generating(chat_id)
                        or chat_id in COMPACTING_CHATS
                        or (active_turn_task is not None and not active_turn_task.done())
                    ):
                        _enqueue_with_optional_compact()
                        await _broadcast_chat_queue(chat_id, chat_obj)
                        continue

                    can_cmp = bool(
                        compute_chat_context_stats(
                            {**chat_obj, "is_generating": False}, msgs_for_cmp
                        ).get("can_compact")
                    )
                    if can_cmp:
                        CHAT_TURN_QUEUES.setdefault(chat_id, []).append(item)
                        CHAT_QUEUE_PAUSED.discard(chat_id)
                        await _broadcast_chat_queue(chat_id, chat_obj)
                        t_cmp = asyncio.create_task(
                            _run_turn_task(
                                chat_id=chat_id,
                                prompt="Сжатие контекста без потери смысла",
                                attachments=[],
                                override_model=None,
                                override_access_mode=None,
                                trigger_user=fresh_user,
                                initial_kind="compact",
                            )
                        )
                        ACTIVE_TURN_TASKS[chat_id] = t_cmp
                        continue

                CHAT_QUEUE_PAUSED.discard(chat_id)
                register_chat_turn(
                    chat_id,
                    user_id=fresh_user["id"],
                    project_name=chat_obj.get("project_name", ""),
                    chat_title=chat_obj.get("title", ""),
                    access_mode=access_mode or chat_obj.get("access_mode", "read_only"),
                )
                t_turn = asyncio.create_task(
                    _run_turn_task(
                        chat_id=chat_id,
                        prompt=effective_user_prompt,
                        attachments=attachments,
                        override_model=model,
                        override_access_mode=access_mode,
                        trigger_user=fresh_user,
                        referenced_chat_ids=queued_ref_ids,
                        referenced_chats=queued_ref_chats,
                        live_voice=live_voice,
                        live_session_id=live_session_id,
                        tts_voice=tts_voice,
                        tts_rate=tts_rate,
                        handoff_meta=handoff_meta,
                        hq_return_source=hq_return_source,
                        hq_return_sources=hq_return_sources,
                        preferred_accounts=pref_accounts,
                    )
                )
                ACTIVE_TURN_TASKS[chat_id] = t_turn

            elif action == "run_queued_now":
                fresh_user = await database.get_user_by_id(ws_user["id"])
                if not fresh_user or not fresh_user.get("is_active", False):
                    await websocket.close(code=4401)
                    return

                chat_id = (data.get("chat_id") or "").strip()
                queue_id = (data.get("queue_id") or "").strip()
                if not chat_id or not queue_id:
                    continue

                chat_obj = await database.get_chat(chat_id)
                if not chat_obj:
                    continue
                allowed, _ = await _get_chat_access_context(chat_obj, fresh_user)
                if not allowed:
                    continue

                q = CHAT_TURN_QUEUES.get(chat_id) or []
                target_idx = next(
                    (i for i, it in enumerate(q) if str(it.get("id")) == queue_id),
                    -1,
                )
                if target_idx < 0:
                    continue
                item = q.pop(target_idx)
                CHAT_QUEUE_PAUSED.discard(chat_id)

                cand_uid = str(item.get("user_id") or "").strip()
                cand_user = await database.get_user_by_id(cand_uid) if cand_uid else None
                cand_allowed = False
                if cand_user and cand_user.get("is_active", False):
                    cand_allowed, _ = await _get_chat_access_context(chat_obj, cand_user)
                if not cand_user or not cand_allowed:
                    if not q:
                        CHAT_TURN_QUEUES.pop(chat_id, None)
                    await _broadcast_chat_queue(chat_id, chat_obj)
                    await websocket.send_json(
                        {
                            "type": "ws_error",
                            "chat_id": chat_id,
                            "error": "Автор элемента очереди заблокирован или потерял доступ к диалогу",
                        }
                    )
                    continue

                busy_generating = is_chat_generating(chat_id)
                busy_compacting = chat_id in COMPACTING_CHATS
                active_t = ACTIVE_TURN_TASKS.get(chat_id)
                busy_task = active_t is not None and not active_t.done()

                if busy_generating or busy_compacting or busy_task:
                    q.insert(0, item)
                    CHAT_TURN_QUEUES[chat_id] = q
                    await _broadcast_chat_queue(chat_id, chat_obj)
                    if busy_generating:
                        await cancel_chat_generation(chat_id)
                else:
                    if not q:
                        CHAT_TURN_QUEUES.pop(chat_id, None)
                    await _broadcast_chat_queue(chat_id, chat_obj)
                    if item.get("kind") != "compact":
                        register_chat_turn(
                            chat_id,
                            user_id=cand_user["id"],
                            project_name=chat_obj.get("project_name", ""),
                            chat_title=chat_obj.get("title", ""),
                            access_mode=item.get("access_mode")
                            or chat_obj.get("access_mode", "read_only"),
                        )
                    task = asyncio.create_task(
                        _run_turn_task(
                            chat_id=chat_id,
                            prompt=str(
                                item.get("prompt")
                                or "Проанализируй прикреплённые файлы."
                            ),
                            attachments=list(item.get("attachments") or []),
                            override_model=item.get("model"),
                            override_access_mode=item.get("access_mode"),
                            trigger_user=cand_user,
                            initial_kind=str(item.get("kind") or "message"),
                            referenced_chat_ids=list(
                                item.get("referenced_chat_ids") or []
                            ),
                            referenced_chats=list(
                                item.get("referenced_chats") or []
                            ),
                            live_voice=bool(item.get("live_voice", False)),
                            live_session_id=str(item.get("live_session_id") or ""),
                            tts_voice=str(item.get("tts_voice") or ""),
                            tts_rate=str(item.get("tts_rate") or ""),
                            handoff_meta=_sanitize_handoff_meta(
                                item.get("handoff_meta")
                            ),
                            hq_return_source=_sanitize_hq_return_source(
                                item.get("hq_return_source")
                            ),
                            hq_return_sources=_sanitize_hq_return_sources(item),
                            preferred_accounts=_sanitize_turn_preferred_accounts(
                                item
                            ),
                        )
                    )
                    ACTIVE_TURN_TASKS[chat_id] = task

            elif action == "remove_queued_message":
                chat_id = (data.get("chat_id") or "").strip()
                queue_id = (data.get("queue_id") or "").strip()
                if not chat_id or not queue_id:
                    continue
                chat_obj = await database.get_chat(chat_id)
                if not chat_obj:
                    continue
                allowed, _ = await _get_chat_access_context(chat_obj, ws_user)
                if not allowed:
                    continue
                q = CHAT_TURN_QUEUES.get(chat_id)
                if q:
                    CHAT_TURN_QUEUES[chat_id] = [
                        it for it in q if str(it.get("id")) != queue_id
                    ]
                    if not CHAT_TURN_QUEUES[chat_id]:
                        CHAT_TURN_QUEUES.pop(chat_id, None)
                await _broadcast_chat_queue(chat_id, chat_obj)

            elif action == "clear_queue":
                chat_id = (data.get("chat_id") or "").strip()
                if not chat_id:
                    continue
                chat_obj = await database.get_chat(chat_id)
                if not chat_obj:
                    continue
                allowed, _ = await _get_chat_access_context(chat_obj, ws_user)
                if not allowed:
                    continue
                CHAT_TURN_QUEUES.pop(chat_id, None)
                CHAT_QUEUE_PAUSED.discard(chat_id)
                await _broadcast_chat_queue(chat_id, chat_obj)

            elif action == "continue_turn":
                fresh_user = await database.get_user_by_id(ws_user["id"])
                if not fresh_user or not fresh_user.get("is_active", False):
                    await websocket.close(code=4401)
                    return

                chat_id = (data.get("chat_id") or "").strip()
                raw_msg_id = data.get("message_id")
                req_model = data.get("model")
                req_access_mode = data.get("access_mode")
                req_pref_accounts = _sanitize_turn_preferred_accounts(data)
                if not chat_id:
                    await websocket.send_json(
                        {"type": "ws_error", "chat_id": chat_id, "error": "Диалог не указан"}
                    )
                    continue

                chat_obj = await database.get_chat(chat_id)
                if not chat_obj:
                    await websocket.send_json(
                        {"type": "ws_error", "chat_id": chat_id, "error": "Диалог не найден"}
                    )
                    continue
                allowed, _ = await _get_chat_access_context(chat_obj, fresh_user)
                if not allowed:
                    await websocket.send_json(
                        {
                            "type": "ws_error",
                            "chat_id": chat_id,
                            "error": "Доступ к этому диалогу запрещён",
                        }
                    )
                    continue

                active_t = ACTIVE_TURN_TASKS.get(chat_id)
                if (
                    is_chat_generating(chat_id)
                    or chat_id in COMPACTING_CHATS
                    or (active_t is not None and not active_t.done())
                ):
                    await websocket.send_json(
                        {
                            "type": "ws_error",
                            "chat_id": chat_id,
                            "error": "Диалог уже выполняется",
                            "keep_generating": True,
                        }
                    )
                    continue

                eff_access_mode = _cap_access_mode_for_user(
                    req_access_mode or chat_obj.get("access_mode") or "read_only",
                    chat_obj["kind"],
                    fresh_user,
                )
                check_model = str(req_model or chat_obj.get("model") or "").strip()
                if check_model:
                    quota_err = await _enforce_user_turn_quotas(fresh_user, check_model)
                    if quota_err:
                        await websocket.send_json(
                            {"type": "ws_error", "chat_id": chat_id, "error": quota_err}
                        )
                        continue

                msgs = await database.get_messages(chat_id, include_thinking=False)
                non_sys = [m for m in msgs if m.get("role") != "system"]
                if not non_sys:
                    await websocket.send_json(
                        {
                            "type": "ws_error",
                            "chat_id": chat_id,
                            "error": "Нет прерванного сообщения для продолжения",
                        }
                    )
                    continue

                target_msg = non_sys[-1]
                req_msg_id_int: Optional[int] = None
                if raw_msg_id is not None and str(raw_msg_id).strip() != "":
                    try:
                        req_msg_id_int = int(raw_msg_id)
                    except (TypeError, ValueError):
                        await websocket.send_json(
                            {
                                "type": "ws_error",
                                "chat_id": chat_id,
                                "error": "Некорректный идентификатор сообщения",
                            }
                        )
                        continue

                if (
                    target_msg.get("role") != "assistant"
                    or target_msg.get("status") not in ("error", "cancelled")
                    or (
                        req_msg_id_int is not None
                        and int(target_msg["id"]) != req_msg_id_int
                    )
                ):
                    await websocket.send_json(
                        {
                            "type": "ws_error",
                            "chat_id": chat_id,
                            "error": "Продолжить можно только последнее прерванное сообщение ассистента",
                        }
                    )
                    continue

                orig_user_msg = next(
                    (
                        m
                        for m in reversed(non_sys[:-1])
                        if m.get("role") == "user"
                    ),
                    None,
                )
                if not orig_user_msg:
                    await websocket.send_json(
                        {
                            "type": "ws_error",
                            "chat_id": chat_id,
                            "error": "Не найдено исходное сообщение пользователя для продолжения",
                        }
                    )
                    continue

                orig_refs_raw = (
                    orig_user_msg["usage"].get("referenced_chats")
                    if isinstance(orig_user_msg.get("usage"), dict)
                    else None
                )
                orig_refs = _sanitize_referenced_chat_specs(
                    orig_refs_raw, None, exclude_chat_id=chat_id
                )

                CHAT_QUEUE_PAUSED.discard(chat_id)
                await _broadcast_chat_queue(chat_id, chat_obj)
                register_chat_turn(
                    chat_id,
                    user_id=fresh_user["id"],
                    project_name=chat_obj.get("project_name", ""),
                    chat_title=chat_obj.get("title", ""),
                    access_mode=eff_access_mode,
                )
                t_resume = asyncio.create_task(
                    _run_turn_task(
                        chat_id=chat_id,
                        prompt=str(orig_user_msg.get("content") or ""),
                        attachments=list(orig_user_msg.get("attachments") or []),
                        override_model=req_model,
                        override_access_mode=eff_access_mode,
                        trigger_user=fresh_user,
                        referenced_chat_ids=[r["id"] for r in orig_refs],
                        referenced_chats=orig_refs,
                        resume_message_id=int(target_msg["id"]),
                        preferred_accounts=req_pref_accounts,
                    )
                )
                ACTIVE_TURN_TASKS[chat_id] = t_resume

            elif action == "cancel_generation":
                chat_id = (data.get("chat_id") or "").strip()
                if chat_id:
                    chat_obj = await database.get_chat(chat_id)
                    if chat_obj:
                        allowed, _ = await _get_chat_access_context(chat_obj, ws_user)
                        if allowed:
                            CHAT_QUEUE_PAUSED.add(chat_id)
                            await _broadcast_chat_queue(chat_id, chat_obj)
                            await cancel_chat_generation(chat_id)

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


@app.websocket("/ws/voice")
async def voice_websocket_endpoint(websocket: WebSocket):
    """Dedicated binary PCM 16 kHz streaming voice endpoint with dual-contour live preview + pause commits."""
    ws_user = await auth.authenticate_ws_user(websocket)
    if not ws_user:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    context_terms = await _collect_user_voice_context_terms(ws_user)

    sample_rate = 16000
    language = "ru"
    engine = "gigaam"
    model_name = "accurate"
    smart_format = True
    live_call = False

    max_session_samples = 10 * 60 * 16000  # 10 minutes safety cap (extended to 60 min in live_call)
    total_received_samples = 0
    samples_since_last_preview = 0
    last_preview_mono = 0.0

    active_chunks: list[np.ndarray] = []
    active_samples = 0
    active_peak_rms = 0.0
    trailing_silent_samples = 0

    groq_chunks: list[np.ndarray] = []
    committed_segments: list[str] = []
    commit_epoch = 0
    commit_in_progress = False
    finishing = False
    ws_closed = False

    used_native_punct = True
    last_model_used = voice.GIGAAM_ACCURATE_MODEL

    ws_send_lock = asyncio.Lock()
    commit_lock = asyncio.Lock()
    bg_tasks: set[asyncio.Task] = set()

    async def safe_send_json(payload: dict[str, Any]) -> None:
        nonlocal ws_closed
        if ws_closed:
            return
        async with ws_send_lock:
            if ws_closed:
                return
            try:
                await websocket.send_json(payload)
            except Exception:
                ws_closed = True

    def _snapshot_active_pcm() -> np.ndarray:
        if not active_chunks:
            return np.zeros(0, dtype=np.float32)
        if len(active_chunks) == 1:
            return active_chunks[0]
        return np.concatenate(active_chunks)

    async def _commit_utterance(seg_pcm: np.ndarray) -> None:
        nonlocal commit_epoch, commit_in_progress, used_native_punct, last_model_used
        try:
            async with commit_lock:
                commit_epoch += 1
                local_engine = "gigaam" if engine == "groq" else engine
                res = await voice.transcribe_pcm_window(
                    seg_pcm,
                    language=language,
                    engine=local_engine,
                    model_name=model_name,
                    is_live=False,
                    allow_groq=False,
                )
                seg_txt = str(res.get("text") or "").strip()
                nat_p = bool(res.get("native_punct", False))
                used_native_punct = nat_p
                if res.get("model"):
                    last_model_used = str(res["model"])
                if seg_txt:
                    if smart_format:
                        seg_txt = voice._format_speech_text_sync(
                            seg_txt,
                            language=language,
                            is_partial=True,
                            native_punct=nat_p,
                            context_terms=context_terms,
                        )
                    if seg_txt:
                        committed_segments.append(seg_txt)
                commit_epoch += 1
                commit_in_progress = False
                if not finishing and not ws_closed:
                    committed_str = voice.join_speech_segments(committed_segments)
                    await safe_send_json(
                        {
                            "type": "partial",
                            "committed": committed_str,
                            "unstable": "",
                            "full_text": committed_str,
                            "engine": last_model_used,
                            "latency_ms": int(res.get("latency_ms") or 0),
                            "committed_event": True,
                        }
                    )
        except Exception as exc:
            commit_in_progress = False
            voice.logger.warning("[voice-ws] utterance commit error: %s", exc)

    async def _emit_live_utterance(
        seg_pcm: np.ndarray,
        prior_segs: list[str],
        *,
        forced: bool = False,
    ) -> None:
        nonlocal commit_epoch, commit_in_progress, used_native_punct, last_model_used
        t_utt = time.perf_counter()
        try:
            async with commit_lock:
                commit_epoch += 1
                seg_txt = ""
                nat_p = used_native_punct
                dur_sec = float(seg_pcm.shape[0] / 16000.0) if seg_pcm.size else 0.0
                seg_rms = (
                    float(np.sqrt(np.mean(np.square(seg_pcm))))
                    if seg_pcm.size
                    else 0.0
                )
                min_dur = 0.16 if forced else 0.30
                min_rms = 0.0012 if forced else 0.0025
                if dur_sec >= min_dur and seg_rms >= min_rms:
                    local_engine = "gigaam" if engine == "groq" else engine
                    res = await voice.transcribe_pcm_window(
                        seg_pcm,
                        language=language,
                        engine=local_engine,
                        model_name=model_name,
                        is_live=False,
                        allow_groq=False,
                    )
                    seg_txt = str(res.get("text") or "").strip()
                    nat_p = bool(res.get("native_punct", nat_p))
                    used_native_punct = nat_p
                    if res.get("model"):
                        last_model_used = str(res["model"])

                raw_joined = voice.join_speech_segments(prior_segs, seg_txt)
                if raw_joined and smart_format:
                    final_txt = voice._format_speech_text_sync(
                        raw_joined,
                        language=language,
                        is_partial=False,
                        native_punct=nat_p,
                        context_terms=context_terms,
                    )
                else:
                    final_txt = raw_joined.strip()

                commit_epoch += 1
                commit_in_progress = False

                alnum_chars = re.findall(r"[A-Za-zА-Яа-яЁё0-9]", final_txt)
                if not finishing and not ws_closed and len(alnum_chars) >= 2:
                    lat_ms = round((time.perf_counter() - t_utt) * 1000)
                    await safe_send_json(
                        {
                            "type": "live_utterance",
                            "utterance_id": uuid.uuid4().hex[:8],
                            "text": final_txt,
                            "duration_sec": round(dur_sec, 2),
                            "latency_ms": lat_ms,
                            "model": last_model_used,
                        }
                    )
        except Exception as exc:
            commit_in_progress = False
            voice.logger.warning("[voice-ws] live_utterance error: %s", exc)

    async def _preview_loop() -> None:
        nonlocal samples_since_last_preview, last_preview_mono
        while not finishing and not ws_closed:
            await asyncio.sleep(0.10)
            if finishing or ws_closed or commit_in_progress:
                continue
            now_m = time.monotonic()
            if samples_since_last_preview < int(0.35 * 16000):
                continue
            if (now_m - last_preview_mono) < 0.30:
                continue
            if active_samples < int(0.30 * 16000):
                continue

            snap_epoch = commit_epoch
            pcm_cur = _snapshot_active_pcm()
            samples_since_last_preview = 0
            last_preview_mono = now_m

            # Cap live preview window to latest 16 seconds
            max_prev_samples = 16 * 16000
            if pcm_cur.shape[0] > max_prev_samples:
                pcm_cur = pcm_cur[-max_prev_samples:]

            local_engine = "gigaam" if engine == "groq" else engine
            try:
                res = await voice.transcribe_pcm_window(
                    pcm_cur,
                    language=language,
                    engine=local_engine,
                    model_name=model_name,
                    is_live=True,
                    allow_groq=False,
                )
            except Exception as exc:
                voice.logger.debug("[voice-ws] preview skipped: %s", exc)
                continue

            if finishing or ws_closed or commit_in_progress or snap_epoch != commit_epoch:
                continue

            unstable = str(res.get("text") or "").strip()
            if unstable and smart_format:
                unstable = voice._format_speech_text_sync(
                    unstable,
                    language=language,
                    is_partial=True,
                    native_punct=bool(res.get("native_punct", False)),
                    context_terms=context_terms,
                )
            committed_str = voice.join_speech_segments(committed_segments)
            full_text = voice.join_speech_segments(committed_segments, unstable)
            await safe_send_json(
                {
                    "type": "partial",
                    "committed": committed_str,
                    "unstable": unstable,
                    "full_text": full_text,
                    "engine": str(res.get("model") or res.get("engine") or "gigaam-v3-e2e-ctc"),
                    "latency_ms": int(res.get("latency_ms") or 0),
                }
            )

    preview_task = asyncio.create_task(_preview_loop())
    try:
        while not ws_closed:
            msg = await websocket.receive()
            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                ws_closed = True
                finishing = True
                break

            raw_text = msg.get("text")
            if raw_text is not None:
                try:
                    data = json.loads(raw_text)
                except Exception:
                    continue
                ev_type = str(data.get("action") or data.get("type") or "").strip().lower()
                if ev_type == "start":
                    sr_in = int(data.get("sample_rate") or 16000)
                    sample_rate = sr_in if 8000 <= sr_in <= 96000 else 16000
                    language = str(data.get("language") or "ru").strip().lower() or "ru"
                    engine = str(data.get("engine") or "gigaam").strip().lower() or "gigaam"
                    model_name = str(data.get("model") or "accurate").strip().lower() or "accurate"
                    smart_format = bool(data.get("smart_format", True))
                    live_call = bool(data.get("live_call", False))
                    if live_call:
                        max_session_samples = 60 * 60 * 16000  # 60 minutes continuous call cap
                    proj_name = str(data.get("project_name") or "").strip()
                    if proj_name:
                        context_terms = [proj_name] + [
                            t for t in context_terms if t.lower() != proj_name.lower()
                        ]
                    eff_e, eff_m = voice.resolve_voice_route(
                        engine=engine,
                        language=language,
                        model_name=model_name,
                        is_live=False,
                        allow_groq=(engine == "groq"),
                    )
                    last_model_used = eff_m
                    used_native_punct = eff_e in ("gigaam", "groq")
                    await safe_send_json(
                        {"type": "ready", "engine": eff_e, "model": eff_m, "live_call": live_call}
                    )
                    if live_call:
                        await safe_send_json(
                            {"type": "live_call_ready", "engine": eff_e, "model": eff_m}
                        )

                elif ev_type == "reset_buffer":
                    commit_epoch += 1
                    active_chunks.clear()
                    groq_chunks.clear()
                    committed_segments.clear()
                    active_samples = 0
                    active_peak_rms = 0.0
                    trailing_silent_samples = 0
                    samples_since_last_preview = 0
                    await safe_send_json({"type": "buffer_reset"})

                elif ev_type == "flush_utterance":
                    seg_pcm_now = _snapshot_active_pcm()
                    prior_now = list(committed_segments)
                    active_chunks.clear()
                    groq_chunks.clear()
                    committed_segments.clear()
                    active_samples = 0
                    active_peak_rms = 0.0
                    trailing_silent_samples = 0
                    samples_since_last_preview = 0
                    commit_epoch += 1
                    if seg_pcm_now.size > 0 or prior_now:
                        commit_in_progress = True
                        t_fl = asyncio.create_task(
                            _emit_live_utterance(seg_pcm_now, prior_now, forced=True)
                        )
                        bg_tasks.add(t_fl)
                        t_fl.add_done_callback(bg_tasks.discard)

                elif ev_type == "cancel":
                    finishing = True
                    break

                elif ev_type == "finish":
                    finishing = True
                    t_fin = time.perf_counter()
                    async with commit_lock:
                        tail_pcm = _snapshot_active_pcm()
                        tail_dur = float(tail_pcm.shape[0] / 16000.0) if tail_pcm.size else 0.0
                        final_model = last_model_used
                        raw_joined = ""
                        groq_done = False

                        # Optional Groq Cloud final pass (with instant local fallback on any error)
                        if (
                            engine == "groq"
                            and config.GROQ_API_KEY
                            and groq_chunks
                            and total_received_samples >= 2400
                        ):
                            full_groq_pcm = np.concatenate(groq_chunks)
                            g_res = await voice.transcribe_pcm_window(
                                full_groq_pcm,
                                language=language,
                                engine="groq",
                                model_name=model_name,
                                is_live=False,
                                allow_groq=True,
                            )
                            if g_res.get("engine") == "groq" and g_res.get("text"):
                                raw_joined = str(g_res["text"]).strip()
                                final_model = "whisper-large-v3-turbo"
                                used_native_punct = True
                                groq_done = True

                        if not groq_done:
                            tail_txt = ""
                            tail_rms = (
                                float(np.sqrt(np.mean(np.square(tail_pcm))))
                                if tail_pcm.size
                                else 0.0
                            )
                            if tail_dur >= 0.16 and tail_rms >= 0.0010:
                                local_eng = "gigaam" if engine == "groq" else engine
                                t_res = await voice.transcribe_pcm_window(
                                    tail_pcm,
                                    language=language,
                                    engine=local_eng,
                                    model_name=model_name,
                                    is_live=False,
                                    allow_groq=False,
                                )
                                tail_txt = str(t_res.get("text") or "").strip()
                                used_native_punct = bool(
                                    t_res.get("native_punct", used_native_punct)
                                )
                                if t_res.get("model"):
                                    final_model = str(t_res["model"])
                                if tail_txt and smart_format:
                                    tail_txt = voice._format_speech_text_sync(
                                        tail_txt,
                                        language=language,
                                        is_partial=True,
                                        native_punct=used_native_punct,
                                        context_terms=context_terms,
                                    )
                            raw_joined = voice.join_speech_segments(
                                committed_segments, tail_txt
                            )

                        if raw_joined and smart_format:
                            final_text = voice._format_speech_text_sync(
                                raw_joined,
                                language=language,
                                is_partial=False,
                                native_punct=used_native_punct,
                                context_terms=context_terms,
                            )
                        else:
                            final_text = raw_joined.strip()

                        finalize_ms = round((time.perf_counter() - t_fin) * 1000)
                        total_dur = round(total_received_samples / 16000.0, 2)
                        voice.logger.info(
                            "[voice-ws] final: total_dur=%.2fs tail_dur=%.2fs finalize_ms=%dms model=%s len=%d",
                            total_dur,
                            tail_dur,
                            finalize_ms,
                            final_model,
                            len(final_text),
                        )
                        await safe_send_json(
                            {
                                "type": "final",
                                "text": final_text,
                                "duration_sec": total_dur,
                                "latency_ms": finalize_ms,
                                "model": final_model,
                            }
                        )
                    await asyncio.sleep(0.05)
                    break
                continue

            raw_bytes = msg.get("bytes")
            if not raw_bytes or finishing:
                continue
            even_len = len(raw_bytes) - (len(raw_bytes) % 2)
            if even_len < 2:
                continue
            if total_received_samples >= max_session_samples:
                continue

            chunk = (
                np.frombuffer(raw_bytes[:even_len], dtype=np.int16).astype(np.float32)
                / 32768.0
            )
            if sample_rate != 16000 and chunk.size > 1:
                out_len = max(1, int(round(chunk.size * 16000.0 / sample_rate)))
                x_old = np.linspace(0.0, 1.0, num=chunk.size, endpoint=False)
                x_new = np.linspace(0.0, 1.0, num=out_len, endpoint=False)
                chunk = np.interp(x_new, x_old, chunk).astype(np.float32)

            remain_cap = max_session_samples - total_received_samples
            if chunk.shape[0] > remain_cap:
                chunk = chunk[:remain_cap]
            if chunk.size == 0:
                continue

            total_received_samples += chunk.shape[0]
            samples_since_last_preview += chunk.shape[0]
            active_chunks.append(chunk)
            active_samples += chunk.shape[0]
            if engine == "groq" and config.GROQ_API_KEY:
                groq_chunks.append(chunk)

            # Track chunk RMS and consecutive pause duration
            step = 1600  # 100 ms sub-frames
            for offset in range(0, chunk.shape[0], step):
                sub = chunk[offset : offset + step]
                if sub.size == 0:
                    continue
                sub_rms = float(np.sqrt(np.mean(np.square(sub))))
                if sub_rms > active_peak_rms:
                    active_peak_rms = sub_rms
                if sub_rms < 0.007:
                    trailing_silent_samples += sub.shape[0]
                else:
                    trailing_silent_samples = 0

            # Contour B: Pause-based Utterance Commit / Continuous Live Call Turn-Taking VAD
            if not commit_in_progress and not finishing:
                active_dur = active_samples / 16000.0
                if live_call:
                    # Continuous hands-free turn-taking VAD:
                    # >= 0.42s speech + >= 0.78s silence (0.62s if speech >= 2.2s) OR >= 24.0s auto-cut
                    req_silence_samples = 9920 if active_dur >= 2.2 else 12480
                    live_pause_ready = (
                        active_peak_rms >= 0.015
                        and active_dur >= 0.42
                        and trailing_silent_samples >= req_silence_samples
                    )
                    live_overflow_ready = (
                        active_peak_rms >= 0.015 and active_dur >= 24.0
                    )
                    if live_pause_ready or live_overflow_ready:
                        full_act = _snapshot_active_pcm()
                        if live_pause_ready:
                            # Trim excess trailing silence while keeping ~250ms natural tail
                            trim_samples = max(0, trailing_silent_samples - 4000)
                            cut_end = max(3200, full_act.shape[0] - trim_samples)
                            seg_pcm = full_act[:cut_end].copy()
                        else:
                            seg_pcm = full_act.copy()

                        prior_segs = list(committed_segments)
                        committed_segments.clear()
                        groq_chunks.clear()
                        active_chunks = []
                        active_samples = 0
                        active_peak_rms = 0.0
                        trailing_silent_samples = 0
                        samples_since_last_preview = 0
                        commit_in_progress = True
                        t_lu = asyncio.create_task(
                            _emit_live_utterance(seg_pcm, prior_segs, forced=False)
                        )
                        bg_tasks.add(t_lu)
                        t_lu.add_done_callback(bg_tasks.discard)
                else:
                    pause_ready = (
                        active_peak_rms >= 0.018
                        and active_dur >= 2.0
                        and trailing_silent_samples >= 8800  # 550 ms at 16 kHz
                    )
                    overflow_ready = active_dur > 14.5
                    if pause_ready or overflow_ready:
                        full_act = _snapshot_active_pcm()
                        if pause_ready:
                            # Keep ~250 ms of trailing silence on the utterance and 100 ms silence tail for next word
                            cut_end = max(3200, full_act.shape[0] - 4800)
                            seg_pcm = full_act[:cut_end].copy()
                            tail_keep = full_act[-1600:].copy()
                        else:
                            # Cut at minimum-energy point in the last 2 seconds
                            search_from = max(32000, full_act.shape[0] - 32000)
                            best_cut = full_act.shape[0] - 1600
                            best_r = 999.0
                            for pos in range(search_from, full_act.shape[0] - 1600, 800):
                                w = full_act[pos : pos + 800]
                                r = float(np.sqrt(np.mean(np.square(w))))
                                if r < best_r:
                                    best_r = r
                                    best_cut = pos + 400
                            seg_pcm = full_act[:best_cut].copy()
                            tail_keep = full_act[max(0, best_cut - 1600) :].copy()

                        active_chunks = [tail_keep] if tail_keep.size else []
                        active_samples = int(tail_keep.shape[0])
                        active_peak_rms = 0.0
                        trailing_silent_samples = 0
                        samples_since_last_preview = 0
                        commit_in_progress = True
                        t_c = asyncio.create_task(_commit_utterance(seg_pcm))
                        bg_tasks.add(t_c)
                        t_c.add_done_callback(bg_tasks.discard)

    except WebSocketDisconnect:
        ws_closed = True
    except Exception as exc:
        ws_closed = True
        voice.logger.debug("[voice-ws] session ended: %s", exc)
    finally:
        finishing = True
        ws_closed = True
        preview_task.cancel()
        for t in list(bg_tasks):
            t.cancel()


# ---------------------------------------------------------------------------
# Static PWA Frontend Mount
# ---------------------------------------------------------------------------


@app.get("/")
async def serve_index():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/manifest.json")
async def serve_manifest():
    return FileResponse(
        STATIC_DIR / "manifest.json",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sw.js")
async def serve_sw():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


class _HubStaticFiles(StaticFiles):
    """v110: `/static/vendor/*` и бандлы с `?v=N` — immutable на год (версия меняется при релизе)."""

    async def get_response(self, path: str, scope: Any):  # type: ignore[override]
        response = await super().get_response(path, scope)
        try:
            if response.status_code in (200, 304):
                norm = str(path or "").replace("\\", "/").lstrip("/")
                qs = urllib.parse.parse_qs((scope.get("query_string") or b"").decode("latin-1"))
                if norm.startswith("vendor/") or qs.get("v"):
                    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        except Exception:
            pass
        return response


app.mount("/static", _HubStaticFiles(directory=str(STATIC_DIR)), name="static")
