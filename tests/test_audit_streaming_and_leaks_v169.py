"""Audit hardening regression suite (v169 Stage 2):
- Preserves saved `status='done'` answer if post-processing raises (`compute_single_chat_context_stats` / broadcast)
- Throttled intermediate streaming checkpoints in SQLite without bumping `_USAGE_LEDGER_VERSION`
- Per-chat dispatch lock eliminates TOCTOU double-spawn in `continue_turn` and preserves targeted DB lookup
- Identity (`is`) guard on `ACTIVE_RUNS` (agy/claude/codex) and `ACTIVE_TURN_TASKS` cleanup
- Non-blocking parallel `ws_manager.broadcast_for_chat` with per-socket timeout & hung client eviction + RBAC masking
- `thinking_update` coalescing with guaranteed flush before `tool_step` and `turn_done`
- `agy_accounts.refresh_host_agy_token` releases global `_lease_lock` during network OAuth refresh & avoids reviving deleted creds
- Guaranteed `exc.close()` on `urllib.error.HTTPError` across all 6 HTTP clients
- Per-session voice preview cancellation isolation in `voice.py`
- Guaranteed `CloseDesktop`/`CloseWindowStation` in `windows_desktop.StationContext.close()` on `SetProcessWindowStation` error
- Non-mutating cached `.env` reading in `tunnel.py`, cached SSLContext in `push_service.py`, cleanup of dead alias in `sandbox_service.py`

Runs strictly inside temporary directories and temporary SQLite fixtures — never touches live `data/hub.db`.
"""
from __future__ import annotations

import asyncio
import email.message
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

from app import (
    agy_accounts,
    agy_credentials,
    agy_engine,
    auth,
    claude_engine,
    codex_engine,
    config,
    database,
    gdrive_service,
    main,
    openrouter_service,
    proxy_router,
    push_service,
    release_service,
    sandbox_service,
    tunnel,
    voice,
    windows_desktop,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def isolated_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "data" / "hub.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    users_dir = tmp_path / "users"
    uploads_dir = tmp_path / "data" / "uploads"
    shared_dir = tmp_path / "shared_projects"
    cli_homes_dir = tmp_path / "data" / "cli_homes"
    agy_cli_dir = tmp_path / "agy_cli"
    agy_app_dir = tmp_path / "agy_app"
    certs_dir = tmp_path / "data" / "certs"
    sessions_dir = tmp_path / "data" / "sessions"
    projects_root = tmp_path / "projects"
    sandbox_dir = tmp_path / "sandbox"

    for key, path in {
        "BASE_DIR": tmp_path,
        "DATA_DIR": tmp_path / "data",
        "DB_PATH": db_path,
        "UPLOADS_DIR": uploads_dir,
        "USERS_DIR": users_dir,
        "SHARED_PROJECTS_DIR": shared_dir,
        "CLI_HOMES_DIR": cli_homes_dir,
        "AGY_CLI_DATA_DIR": agy_cli_dir,
        "AGY_APP_DATA_DIR": agy_app_dir,
        "CERTS_DIR": certs_dir,
        "SESSIONS_DIR": sessions_dir,
        "PROJECTS_ROOT": projects_root,
        "CHAT_SANDBOX_DIR": sandbox_dir,
        "ENV_PATH": tmp_path / ".env",
        "SECRET_KEY_FILE": tmp_path / "data" / ".secret_key",
    }.items():
        monkeypatch.setattr(config, key, path)

    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "USERS_DIR", users_dir)
    monkeypatch.setattr(database, "SHARED_PROJECTS_DIR", shared_dir)
    monkeypatch.setattr(config, "get_effective_users_dir", lambda: users_dir)
    monkeypatch.setattr(config, "get_effective_legacy_uploads_dir", lambda: uploads_dir)
    monkeypatch.setattr(main, "ws_manager", main.ConnectionManager())
    monkeypatch.setattr(main, "ACTIVE_TURN_TASKS", {})
    monkeypatch.setattr(main, "COMPACTING_CHATS", set())
    monkeypatch.setattr(main, "COMPACTING_OWNERS", {})
    monkeypatch.setattr(main, "COMPACTING_TASKS", {})
    monkeypatch.setattr(main, "CHAT_TURN_QUEUES", {})
    monkeypatch.setattr(main, "CHAT_QUEUE_PAUSED", set())
    monkeypatch.setattr(main, "_CHAT_DISPATCH_LOCKS", {})
    monkeypatch.setattr(agy_engine, "ACTIVE_RUNS", {})
    monkeypatch.setattr(main, "active_generations", agy_engine.ACTIVE_RUNS)
    monkeypatch.setattr(claude_engine, "ACTIVE_RUNS", agy_engine.ACTIVE_RUNS)
    monkeypatch.setattr(codex_engine, "ACTIVE_RUNS", agy_engine.ACTIVE_RUNS)
    monkeypatch.setenv("HUB_SECRET_KEY", "audit-v169-s2-fixture-master-key")

    database._reset_usage_ledger_cache()
    database.invalidate_user_cache()
    database.invalidate_chat_list_cache()
    run(database.init_db())
    yield tmp_path
    run(database.close_db_pool())
    database._reset_usage_ledger_cache()
    database.invalidate_user_cache()
    database.invalidate_chat_list_cache()


def _patch_minimal_turn_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main,
        "fetch_available_models",
        AsyncMock(return_value=[{"id": "gemini-4-argon-low", "label": "Gemini 4 Argon"}]),
    )
    monkeypatch.setattr(main, "_get_models_for_user", AsyncMock(return_value=[{"id": "gemini-4-argon-low"}]))
    monkeypatch.setattr(main, "get_cli_auth_status", lambda: {"ok": True})
    monkeypatch.setattr(main, "_enforce_user_turn_quotas", AsyncMock(return_value=""))
    monkeypatch.setattr(openrouter_service, "is_user_openrouter_bridge_active", AsyncMock(return_value=False))
    monkeypatch.setattr(sandbox_service, "is_user_sandbox_eligible", lambda _u: False)
    monkeypatch.setattr(main.host_service_manager, "is_user_host_eligible", lambda _u: False)
    monkeypatch.setattr(main, "_dispatch_turn_webpush", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_refresh_and_broadcast_quotas", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_maybe_generate_live_outline_title_task", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_notify_gdrive_change_for_user", lambda *a, **kw: None)


# ---------------------------------------------------------------------------
# 1. Terminal `done` preservation on post-processing error + checkpointing
# ---------------------------------------------------------------------------


def test_done_message_preserved_when_post_processing_fails(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_minimal_turn_helpers(monkeypatch)
    admin_user = {"id": "admin", "username": "admin", "role": "admin", "is_active": True}
    chat = run(database.create_chat(owner_id="admin", kind="chat", title="Post-Error Guard", model="gemini-4-argon-low"))
    chat_id = chat["id"]

    async def fake_stream(**_kwargs):
        yield {"type": "text_delta", "delta": "Готовый финальный ответ.", "seq": 1}
        yield {
            "type": "done",
            "content": "Готовый финальный ответ.",
            "thinking": "Короткий план",
            "tool_calls": [],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }

    monkeypatch.setattr(main, "stream_agy_turn", fake_stream)
    monkeypatch.setattr(
        database,
        "compute_single_chat_context_stats",
        AsyncMock(side_effect=RuntimeError("synthetic stats boom")),
    )

    events_sent: list[dict[str, Any]] = []
    orig_broadcast = main.ws_manager.broadcast_for_chat

    async def capture_broadcast(c, payload, shared_member_ids=None):
        events_sent.append(payload)
        return await orig_broadcast(c, payload, shared_member_ids=shared_member_ids)

    monkeypatch.setattr(main.ws_manager, "broadcast_for_chat", capture_broadcast)

    run(
        main._run_turn_task_inner_body(
            chat_id=chat_id,
            prompt="Сделай расчёт",
            attachments=[],
            override_model="gemini-4-argon-low",
            override_access_mode="read_only",
            trigger_user=admin_user,
        )
    )

    msgs = run(database.get_messages(chat_id, include_thinking=True))
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["status"] == "done"
    assert assistant_msgs[0]["content"] == "Готовый финальный ответ."
    assert assistant_msgs[0]["thinking"] == "Короткий план"
    assert not any(ev.get("type") == "turn_error" for ev in events_sent)


def test_pre_terminal_exception_records_terminal_ts_and_streaming_checkpoint(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_minimal_turn_helpers(monkeypatch)
    monkeypatch.setattr(main, "_STREAM_CHECKPOINT_INTERVAL_SEC", 0.03)
    admin_user = {"id": "admin", "username": "admin", "role": "admin", "is_active": True}
    chat = run(database.create_chat(owner_id="admin", kind="chat", title="Checkpoint Guard", model="gemini-4-argon-low"))
    chat_id = chat["id"]

    checkpoint_snapshot: dict[str, Any] = {}

    async def fake_stream(**_kwargs):
        yield {"type": "text_delta", "delta": "Часть 1. ", "seq": 1}
        await asyncio.sleep(0.05)
        yield {"type": "text_delta", "delta": "Часть 2.", "seq": 2}
        # Capture DB row right after checkpoint before forcing an unexpected failure
        v_before = database._USAGE_LEDGER_VERSION
        rows = await database.get_messages(chat_id, include_thinking=True)
        asst = [m for m in rows if m["role"] == "assistant"][-1]
        checkpoint_snapshot["content"] = asst["content"]
        checkpoint_snapshot["status"] = asst["status"]
        checkpoint_snapshot["version_before_second_cp"] = v_before
        await asyncio.sleep(0.05)
        yield {"type": "text_delta", "delta": " Часть 3.", "seq": 3}
        checkpoint_snapshot["version_after_second_cp"] = database._USAGE_LEDGER_VERSION
        raise RuntimeError("mid-stream failure")

    monkeypatch.setattr(main, "stream_agy_turn", fake_stream)
    main._CHAT_LAST_TERMINAL_TS_MS.pop(chat_id, None)

    run(
        main._run_turn_task_inner_body(
            chat_id=chat_id,
            prompt="Длинный стрим",
            attachments=[],
            override_model="gemini-4-argon-low",
            override_access_mode="read_only",
            trigger_user=admin_user,
        )
    )

    assert checkpoint_snapshot["status"] == "streaming"
    assert checkpoint_snapshot["content"] == "Часть 1. Часть 2."
    assert checkpoint_snapshot["version_after_second_cp"] == checkpoint_snapshot["version_before_second_cp"]
    assert chat_id in main._CHAT_LAST_TERMINAL_TS_MS

    msgs = run(database.get_messages(chat_id, include_thinking=True))
    asst_final = [m for m in msgs if m["role"] == "assistant"][-1]
    assert asst_final["status"] == "error"
    assert "Часть 1. Часть 2. Часть 3." in asst_final["content"]


# ---------------------------------------------------------------------------
# 2. Anti-TOCTOU double-spawn in `continue_turn` & identity pop in `ACTIVE_RUNS`
# ---------------------------------------------------------------------------


def test_continue_turn_dispatch_lock_blocks_concurrent_double_spawn(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_user = {"id": "admin", "username": "admin", "role": "admin", "is_active": True}
    chat = run(database.create_chat(owner_id="admin", kind="chat", title="Continue Race", model="gemini-4-argon-low"))
    chat_id = chat["id"]
    run(database.add_message(chat_id, user_id="admin", role="user", content="Напиши отчёт", status="done"))
    interrupted = run(
        database.add_message(
            chat_id,
            user_id="admin",
            role="assistant",
            content="Начало отчёта...",
            status="cancelled",
        )
    )

    orig_lookup = database.get_continue_turn_messages

    async def delayed_lookup(cid: str):
        await asyncio.sleep(0.05)
        return await orig_lookup(cid)

    monkeypatch.setattr(database, "get_continue_turn_messages", delayed_lookup)
    monkeypatch.setattr(main, "_enforce_user_turn_quotas", AsyncMock(return_value=""))

    spawned_calls: list[dict[str, Any]] = []
    release_turn = asyncio.Event()

    async def fake_run_turn_task(**kwargs):
        spawned_calls.append(kwargs)
        try:
            await asyncio.wait_for(release_turn.wait(), timeout=2.0)
        finally:
            cur_t = asyncio.current_task()
            if main.ACTIVE_TURN_TASKS.get(chat_id) is cur_t:
                main.ACTIVE_TURN_TASKS.pop(chat_id, None)
                agy_engine.clear_chat_turn(chat_id)

    monkeypatch.setattr(main, "_run_turn_task", fake_run_turn_task)
    monkeypatch.setattr(auth, "authenticate_ws_user", AsyncMock(return_value=admin_user))

    class FakeWS:
        def __init__(self, msg: dict[str, Any]) -> None:
            self._msgs = [msg]
            self.sent: list[dict[str, Any]] = []
            self.closed = False

        async def accept(self) -> None:
            pass

        async def receive_json(self) -> dict[str, Any]:
            if self._msgs:
                return self._msgs.pop(0)
            await asyncio.sleep(0.15)
            raise WebSocketDisconnect()

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def close(self, code: int = 1000) -> None:
            self.closed = True

    req = {"action": "continue_turn", "chat_id": chat_id, "message_id": interrupted["id"]}
    ws1 = FakeWS(dict(req))
    ws2 = FakeWS(dict(req))

    async def scenario():
        t1 = asyncio.create_task(main.websocket_endpoint(ws1))
        t2 = asyncio.create_task(main.websocket_endpoint(ws2))
        await asyncio.sleep(0.12)
        release_turn.set()
        await asyncio.gather(t1, t2)
        pending = list(main.ACTIVE_TURN_TASKS.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    run(scenario())

    assert len(spawned_calls) == 1
    assert spawned_calls[0]["resume_message_id"] == interrupted["id"]
    ws_errors = [m for m in (ws1.sent + ws2.sent) if m.get("type") == "ws_error"]
    assert len(ws_errors) == 1
    assert ws_errors[0].get("keep_generating") is True
    assert "уже выполняется" in str(ws_errors[0].get("error") or "")
    assert ws1 not in main.ws_manager.active
    assert ws2 not in main.ws_manager.active


def test_active_runs_identity_pop_preserves_newer_run(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_id = "chat-identity-guard"
    fake_bin = isolated_hub / "fake_cli.exe"
    fake_bin.write_text("ok", encoding="utf-8")

    async def scenario():
        # 1) agy_engine.stream_agy_turn: swap ACTIVE_RUNS[chat_id] inside `try:` before `finally:` runs
        old_run = agy_engine.register_chat_turn(chat_id, user_id="admin")
        new_run = agy_engine.ActiveRun(chat_id=chat_id, user_id="admin", model="newer")

        monkeypatch.setattr(
            agy_engine,
            "resolve_agy_account_for_chat_turn",
            AsyncMock(return_value={"account": None, "target_pool": "gemini", "switched_from": None, "emit_failover": False}),
        )
        monkeypatch.setattr(agy_engine, "materialize_agy_account_home", AsyncMock(return_value=isolated_hub))
        monkeypatch.setattr(agy_engine, "resolve_agy_bin", lambda: str(fake_bin))

        def swap_and_fail_agy(*_a, **_kw):
            agy_engine.ACTIVE_RUNS[chat_id] = new_run
            raise RuntimeError("abort old agy run")

        monkeypatch.setattr(agy_engine, "agy_session_db_exists", swap_and_fail_agy)

        gen = agy_engine.stream_agy_turn(
            chat_id=chat_id,
            kind="chat",
            project_name="",
            model="gemini-4-argon-low",
            access_mode="read_only",
            prompt="hello",
            attachments=[],
            agy_conversation_id="",
            history_messages=[],
            user={"id": "admin", "role": "admin"},
        )
        with pytest.raises(RuntimeError, match="abort old agy run"):
            async for _ in gen:
                pass
        assert agy_engine.ACTIVE_RUNS.get(chat_id) is new_run
        assert agy_engine.ACTIVE_RUNS.get(chat_id) is not old_run

        # 2) claude_engine.stream_claude_turn: identity guard in `finally:`
        agy_engine.ACTIVE_RUNS.clear()
        old_cl_run = agy_engine.register_chat_turn(chat_id, user_id="admin")
        newer_cl_run = agy_engine.ActiveRun(chat_id=chat_id, user_id="admin", engine="claude_code")
        monkeypatch.setattr(claude_engine, "resolve_claude_bin", lambda: str(fake_bin))
        monkeypatch.setattr(
            claude_engine,
            "resolve_claude_account_for_chat_turn",
            AsyncMock(return_value={"id": "acc1", "label": "A1"}),
        )

        async def swap_and_fail_claude(*_a, **_kw):
            agy_engine.ACTIVE_RUNS[chat_id] = newer_cl_run
            raise RuntimeError("abort old claude run")

        monkeypatch.setattr(database, "get_user_claude_account", swap_and_fail_claude)
        cl_gen = claude_engine.stream_claude_turn(
            chat_id=chat_id,
            kind="chat",
            project_name="",
            model="claude-code:sonnet@medium",
            access_mode="read_only",
            prompt="hi",
            attachments=[],
            claude_session_id="",
            claude_account_id="",
            history_messages=[],
            user={"id": "admin", "role": "admin"},
        )
        with pytest.raises(RuntimeError, match="abort old claude run"):
            async for _ in cl_gen:
                pass
        assert agy_engine.ACTIVE_RUNS.get(chat_id) is newer_cl_run
        assert agy_engine.ACTIVE_RUNS.get(chat_id) is not old_cl_run

        # 3) codex_engine.stream_codex_turn: identity guard in `finally:`
        agy_engine.ACTIVE_RUNS.clear()
        old_cx_run = agy_engine.register_chat_turn(chat_id, user_id="admin")
        newer_cx_run = agy_engine.ActiveRun(chat_id=chat_id, user_id="admin", engine="codex")
        monkeypatch.setattr(codex_engine, "resolve_codex_bin", lambda: str(fake_bin))
        monkeypatch.setattr(
            codex_engine,
            "resolve_codex_account_for_chat_turn",
            AsyncMock(return_value={"id": "cx1", "label": "CX1"}),
        )

        async def swap_and_fail_codex(*_a, **_kw):
            agy_engine.ACTIVE_RUNS[chat_id] = newer_cx_run
            raise RuntimeError("abort old codex run")

        monkeypatch.setattr(database, "get_user_chatgpt_account_secret", swap_and_fail_codex)
        cx_gen = codex_engine.stream_codex_turn(
            chat_id=chat_id,
            kind="chat",
            project_name="",
            model="codex:gpt-5.4-mini@low",
            access_mode="read_only",
            prompt="hi",
            attachments=[],
            codex_thread_id="",
            codex_account_id="",
            history_messages=[],
            user={"id": "admin", "role": "admin"},
        )
        with pytest.raises(RuntimeError, match="abort old codex run"):
            async for _ in cx_gen:
                pass
        assert agy_engine.ACTIVE_RUNS.get(chat_id) is newer_cx_run
        assert agy_engine.ACTIVE_RUNS.get(chat_id) is not old_cx_run

    run(scenario())


# ---------------------------------------------------------------------------
# 3. Non-blocking WS broadcast with hung client eviction & RBAC masking
# ---------------------------------------------------------------------------


def test_ws_broadcast_non_blocking_with_hung_socket_eviction() -> None:
    cm = main.ConnectionManager(send_timeout_sec=0.15)

    class LiveSocket:
        def __init__(self) -> None:
            self.received: list[dict[str, Any]] = []
            self.closed = False

        async def accept(self) -> None:
            pass

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.received.append(payload)

        async def close(self, code: int = 1000) -> None:
            self.closed = True

    class HungSocket(LiveSocket):
        async def send_json(self, payload: dict[str, Any]) -> None:
            await asyncio.sleep(5.0)
            self.received.append(payload)

    admin_ws = LiveSocket()
    guest_ws = LiveSocket()
    hung_ws = HungSocket()

    async def scenario() -> float:
        await cm.connect(admin_ws, {"id": "admin", "role": "admin"})
        await cm.connect(guest_ws, {"id": "dev1", "role": "user"})
        await cm.connect(hung_ws, {"id": "admin", "role": "admin"})

        chat = {"id": "c1", "owner_id": "admin", "kind": "project", "project_name": "SharedApp"}
        payload = {
            "type": "turn_done",
            "chat_id": "c1",
            "chat": {"id": "c1", "project_path": "D:/secret/SharedApp"},
        }
        t0 = time.monotonic()
        await cm.broadcast_for_chat(chat, payload, shared_member_ids={"dev1"})
        return time.monotonic() - t0

    elapsed = run(scenario())
    assert elapsed < 1.2
    assert len(admin_ws.received) == 1
    assert admin_ws.received[0]["chat"].get("project_path") == "D:/secret/SharedApp"
    assert len(guest_ws.received) == 1
    assert "project_path" not in guest_ws.received[0]["chat"]
    assert hung_ws.received == []
    assert hung_ws.closed is True
    assert hung_ws not in cm.active
    assert admin_ws in cm.active
    assert guest_ws in cm.active


# ---------------------------------------------------------------------------
# 4. Coalescing `thinking_update` with guaranteed flush before tool_step/done
# ---------------------------------------------------------------------------


def test_thinking_update_coalescing_and_flush_before_tool_and_done(
    isolated_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_minimal_turn_helpers(monkeypatch)
    monkeypatch.setattr(main, "_THINKING_UPDATE_MIN_INTERVAL_SEC", 0.25)
    admin_user = {"id": "admin", "username": "admin", "role": "admin", "is_active": True}
    chat = run(database.create_chat(owner_id="admin", kind="chat", title="Thinking Coalesce", model="gemini-4-argon-low"))
    chat_id = chat["id"]

    async def fake_stream(**_kwargs):
        for i in range(1, 11):
            yield {"type": "thinking_update", "thinking": f"step-A-{i}", "seq": i}
        yield {"type": "tool_step", "step": {"id": "t1", "name": "read_file", "status": "done"}, "seq": 11}
        for i in range(12, 17):
            yield {"type": "thinking_update", "thinking": f"step-B-{i}", "seq": i}
        yield {
            "type": "done",
            "content": "Готово",
            "thinking": "step-B-16",
            "tool_calls": [{"id": "t1", "name": "read_file", "status": "done"}],
            "usage": {},
        }

    monkeypatch.setattr(main, "stream_agy_turn", fake_stream)
    sent_events: list[dict[str, Any]] = []

    async def capture(_chat, payload, shared_member_ids=None):
        sent_events.append(payload)

    monkeypatch.setattr(main.ws_manager, "broadcast_for_chat", capture)

    run(
        main._run_turn_task_inner_body(
            chat_id=chat_id,
            prompt="Подумай",
            attachments=[],
            override_model="gemini-4-argon-low",
            override_access_mode="read_only",
            trigger_user=admin_user,
        )
    )

    thinking_events = [e for e in sent_events if e.get("type") == "thinking_update"]
    # 15 rapid thinking_update events must coalesce down to <= 4 frames
    assert len(thinking_events) <= 4
    assert all(
        e.get("chat_id") == chat_id and isinstance(e.get("thinking"), str)
        for e in thinking_events
    )

    idx_tool = next(i for i, e in enumerate(sent_events) if e.get("type") == "tool_step")
    idx_done = next(i for i, e in enumerate(sent_events) if e.get("type") == "turn_done")
    before_tool = [e["thinking"] for e in sent_events[:idx_tool] if e.get("type") == "thinking_update"]
    between_tool_and_done = [
        e["thinking"] for e in sent_events[idx_tool:idx_done] if e.get("type") == "thinking_update"
    ]
    assert before_tool[-1] == "step-A-10"
    assert between_tool_and_done[-1] == "step-B-16"


# ---------------------------------------------------------------------------
# 5. Global `_lease_lock` released during network OAuth refresh in `agy_accounts`
# ---------------------------------------------------------------------------


def test_refresh_host_agy_token_releases_lease_lock_during_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "cli_home"
    tok_path = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    tok_path.parent.mkdir(parents=True, exist_ok=True)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    initial_payload = {
        "token": {
            "access_token": "old_access",
            "refresh_token": "1//refresh-token-demo",
            "expiry": expired,
        },
        "email": "user@example.com",
    }
    tok_path.write_text(json.dumps(initial_payload), encoding="utf-8")

    # Stub WindowsCredentialStore so this test exercises the isolated temp file deterministically
    class _NullCredStore:
        def read(self):
            return None

        def write(self, _val):
            return None

    monkeypatch.setattr(agy_credentials, "WindowsCredentialStore", _NullCredStore)
    monkeypatch.setattr(agy_accounts, "_ACCOUNT_REFRESH_LOCKS", {})
    new_expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    async def scenario() -> None:
        fresh_lock = asyncio.Lock()
        monkeypatch.setattr(agy_credentials, "_lease_lock", fresh_lock)
        entered_network = asyncio.Event()
        allow_network_finish = asyncio.Event()

        async def slow_network_refresh(_payload, **_kwargs):
            entered_network.set()
            await allow_network_finish.wait()
            return {
                "token": {
                    "access_token": "fresh_access_v2",
                    "refresh_token": "1//refresh-token-demo",
                    "expiry": new_expiry,
                },
                "email": "user@example.com",
            }

        monkeypatch.setattr(agy_accounts, "refresh_and_build_agy_token_payload", slow_network_refresh)

        task = asyncio.create_task(agy_accounts.refresh_host_agy_token(home))
        await asyncio.wait_for(entered_network.wait(), timeout=2.0)

        # Global `_lease_lock` MUST be free while network refresh is in flight
        assert fresh_lock.locked() is False
        await asyncio.wait_for(fresh_lock.acquire(), timeout=0.2)
        fresh_lock.release()

        allow_network_finish.set()
        await asyncio.wait_for(task, timeout=2.0)

        saved = json.loads(tok_path.read_text(encoding="utf-8"))
        assert saved["token"]["access_token"] == "fresh_access_v2"

        # Also verify that if session file is removed during refresh, it is NOT resurrected
        tok_path.write_text(json.dumps(initial_payload), encoding="utf-8")
        entered_network.clear()
        allow_network_finish.clear()

        task2 = asyncio.create_task(agy_accounts.refresh_host_agy_token(home))
        await asyncio.wait_for(entered_network.wait(), timeout=2.0)
        tok_path.unlink()
        allow_network_finish.set()
        await asyncio.wait_for(task2, timeout=2.0)
        assert not tok_path.exists()

    run(scenario())


# ---------------------------------------------------------------------------
# 6. Guaranteed `exc.close()` on `urllib.error.HTTPError` across all 6 handlers
# ---------------------------------------------------------------------------


class _TrackedHTTPError(urllib.error.HTTPError):
    def __init__(self, url: str, code: int, body: bytes = b'{"error":"fail"}') -> None:
        hdrs = email.message.Message()
        hdrs["Content-Type"] = "application/json"
        super().__init__(url, code, "Error", hdrs, io.BytesIO(body))
        self.closed_count = 0

    def close(self) -> None:
        self.closed_count += 1
        super().close()


def test_http_error_close_called_in_all_six_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    def make_opener(err: _TrackedHTTPError):
        class _FakeOpener:
            def open(self, *_a, **_kw):
                raise err

        return _FakeOpener()

    # 1) gdrive_service.urllib_proxy_transport
    err1 = _TrackedHTTPError("https://www.googleapis.com/drive/v3/files", 403)
    monkeypatch.setattr(proxy_router, "build_urllib_proxy_opener", lambda *_a, **_kw: make_opener(err1))
    resp1 = gdrive_service.urllib_proxy_transport("GET", "https://www.googleapis.com/drive/v3/files", {}, None, 5.0)
    assert resp1.status == 403
    assert err1.closed_count == 1

    # 2) release_service._github
    err2 = _TrackedHTTPError("https://api.github.com/repos/a/b/releases/latest", 404)

    def _raise_err2(_req, _timeout):
        raise err2

    code2, data2 = release_service._github("GET", "/repos/a/b/releases/latest", transport=_raise_err2, allowed=(404,))
    assert code2 == 404 and data2 == {}
    assert err2.closed_count == 1

    # 3) openrouter_service._openrouter_http_sync
    err3 = _TrackedHTTPError("https://openrouter.ai/api/v1/models", 429, b'{"error":{"message":"rate"}}')
    monkeypatch.setattr(openrouter_service, "_build_url_opener", lambda *_a, **_kw: make_opener(err3))
    code3, data3 = openrouter_service._openrouter_http_sync("GET", "/models", proxy_url="")
    assert code3 == 429
    assert data3["error"]["message"] == "rate"
    assert err3.closed_count == 1

    # 4) claude_engine._http_get_usage_sync
    err4 = _TrackedHTTPError("https://api.anthropic.com/api/oauth/usage", 500, b"boom")
    monkeypatch.setattr(proxy_router, "build_urllib_proxy_opener", lambda *_a, **_kw: make_opener(err4))
    code4, body4 = claude_engine._http_get_usage_sync("tok", "1.0.0", proxy_url="")
    assert code4 == 500 and "boom" in body4
    assert err4.closed_count == 1

    # 5) agy_accounts.refresh_and_build_agy_token_payload
    err5 = _TrackedHTTPError("https://oauth2.googleapis.com/token", 400)
    monkeypatch.setattr(agy_accounts, "_oauth_pair", lambda _p: ("cid", "csec"))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a, **_kw: make_opener(err5))
    with pytest.raises(ValueError):
        run(agy_accounts.refresh_and_build_agy_token_payload({"refresh_token": "1//x"}, proxy_url=""))
    assert err5.closed_count == 1

    # 6) agy_accounts.exchange_agy_oauth_code_for_token
    err6 = _TrackedHTTPError("https://oauth2.googleapis.com/token", 503)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a, **_kw: make_opener(err6))
    with pytest.raises(agy_accounts.OAuthTemporaryError):
        run(
            agy_accounts.exchange_agy_oauth_code_for_token(
                "code123",
                redirect_uri="http://127.0.0.1:9999/oauth-callback",
                code_verifier="verifier",
                proxy_url="",
            )
        )
    assert err6.closed_count == 1


# ---------------------------------------------------------------------------
# 7. Per-session voice preview isolation, StationContext.close(), tunnel/SSL/sandbox
# ---------------------------------------------------------------------------


def test_voice_preview_cancel_isolated_per_session_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice, "_active_preview_cancels", {})
    monkeypatch.setattr(voice, "_route_needs_outbound_proxy", lambda *_a, **_kw: False)

    user_a_started = threading.Event()
    allow_user_a_finish = threading.Event()
    captured: dict[str, bool] = {}

    def fake_transcribe_sync(
        audio_bytes,
        language,
        engine,
        model_name,
        live_preview,
        offset_sec,
        cancel_event,
        proxy_url,
    ):
        tag = audio_bytes.decode("utf-8")
        if tag == "A1":
            user_a_started.set()
            allow_user_a_finish.wait(timeout=3.0)
            captured["A1_cancelled"] = bool(cancel_event.is_set()) if cancel_event else False
        elif tag == "B1":
            captured["B1_cancelled"] = bool(cancel_event.is_set()) if cancel_event else False
        elif tag == "A2_start":
            user_a_started.set()
            allow_user_a_finish.wait(timeout=3.0)
            captured["A2_cancelled"] = bool(cancel_event.is_set()) if cancel_event else False
        return {"text": tag, "engine": "gigaam", "model": "fast"}

    monkeypatch.setattr(voice, "_transcribe_sync", fake_transcribe_sync)

    async def scenario_cross_user() -> None:
        task_a = asyncio.create_task(
            voice.transcribe_audio(b"A1", live_preview=True, session_key="user_A")
        )
        await asyncio.to_thread(user_a_started.wait, 2.0)
        # Another user's preview MUST NOT cancel user_A
        await voice.transcribe_audio(b"B1", live_preview=True, session_key="user_B")
        allow_user_a_finish.set()
        await task_a

    run(scenario_cross_user())
    assert captured["A1_cancelled"] is False
    assert captured["B1_cancelled"] is False
    assert voice._active_preview_cancels == {}

    # Same user's second preview MUST cancel their own previous preview
    user_a_started.clear()
    allow_user_a_finish.clear()

    async def scenario_same_user() -> None:
        task_a2 = asyncio.create_task(
            voice.transcribe_audio(b"A2_start", live_preview=True, session_key="user_A")
        )
        await asyncio.to_thread(user_a_started.wait, 2.0)
        await voice.transcribe_audio(b"A2_next", live_preview=True, session_key="user_A")
        allow_user_a_finish.set()
        await task_a2

    run(scenario_same_user())
    assert captured["A2_cancelled"] is True
    assert voice._active_preview_cancels == {}


def test_station_context_close_releases_handles_on_set_window_station_failure() -> None:
    calls: list[tuple[str, Any]] = []

    class FakeUser32:
        def SetProcessWindowStation(self, h):
            calls.append(("SetProcessWindowStation", h))
            return 0  # Fail immediately

        def SetThreadDesktop(self, h):
            calls.append(("SetThreadDesktop", h))
            return 1

        def CloseDesktop(self, h):
            calls.append(("CloseDesktop", h))
            return 1

        def CloseWindowStation(self, h):
            calls.append(("CloseWindowStation", h))
            return 1

    ctx = object.__new__(windows_desktop.StationContext)
    ctx.u = FakeUser32()
    ctx.original_station = 10
    ctx.original_desktop = 20
    ctx.desktop = 111
    ctx.station = 222

    with pytest.raises(OSError):
        ctx.close()

    assert ("SetProcessWindowStation", 10) in calls
    assert ("CloseDesktop", 111) in calls
    assert ("CloseWindowStation", 222) in calls
    assert ctx.desktop is None
    assert ctx.station is None


def test_tunnel_dotenv_non_mutating_push_ssl_cache_and_sandbox_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HUB_TUNNEL_ENABLED=1\nHUB_TUNNEL_SSH_TARGET=tunnel@example.com\nHUB_TUNNEL_SSH_PORT=2222\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_PATH", env_file)
    monkeypatch.delenv("HUB_TUNNEL_ENABLED", raising=False)
    monkeypatch.delenv("HUB_TUNNEL_SSH_TARGET", raising=False)
    monkeypatch.setattr(tunnel, "_env_cache_key", None)
    monkeypatch.setattr(tunnel, "_env_cache_values", {})

    cfg = tunnel._load_tunnel_settings()
    assert cfg["enabled"] is True
    assert cfg["target"] == "tunnel@example.com"
    assert cfg["ssh_port"] == 2222
    # Must NOT mutate global os.environ
    assert "HUB_TUNNEL_ENABLED" not in os.environ
    assert "HUB_TUNNEL_SSH_TARGET" not in os.environ

    # Cached SSLContext returns the same immutable context instance
    ctx1 = push_service._get_default_ssl_context()
    ctx2 = push_service._get_default_ssl_context()
    assert ctx1 is ctx2

    # Dead alias removed from sandbox_service
    assert not hasattr(sandbox_service, "cancel_ActiveRun_sandbox_execs")
