"""
Isolated automated verification suite for Release v=103:
Dynamic Real-Time Telemetry Sync (Google AI, ChatGPT, Claude Code, OpenRouter) with Anti-Ban Protection.

Covers all HQ specification acceptance criteria:
1. Test 1 (`_clamp_exhausted_chatgpt_quotas` & `_clamp_exhausted_claude_quotas` + single-account turn exhaustion):
   - Clamping `primary_5h` to `0%` on 5-hour limit errors and `secondary_7d` to `0%` on weekly (`7-day`) limit errors.
   - Single-account `stream_codex_turn` (`auto_failover=False`, partial text already emitted) and `stream_claude_turn`
     (single account, partial text already emitted) clamp SQLite `quotas` to `0%`, preserve `last_error`, skip
     redundant capacity retries (`1` spawn), and emit `"quota_exhausted": True`.
2. Test 2 (`_read_rate_limits_from_open_proc` over mock stdio JSON-RPC + post-turn in-session read):
   - Extracts `primary_5h` and `secondary_7d` via `account/rateLimits/read` (`id == "post_turn_rl"`) and
     `account/rateLimits/updated` with `0` extra subprocesses, and exits gracefully on timeout (`None`, `0` exceptions).
   - Verifies `stream_codex_turn` persists fresh post-turn rate limits from the open `codex app-server` session.
3. Test 3 (Anti-Ban deduplication, cooldowns, `reset_time + 10s` staleness, `429` `300s` backoff, OpenRouter `30s` guard + `402`):
   - `is_chatgpt_quota_cache_stale`, `is_claude_quota_cache_stale`, `is_agy_quota_cache_stale` (including `reset_time + 10s`).
   - `schedule_chatgpt_live_refresh` & `schedule_claude_live_refresh` in-flight deduplication and cooldowns.
   - `refresh_claude_account_quotas` HTTP `429` sets `300s` Anti-Ban backoff (`0` retry loops).
   - `openrouter_service`: `30s` burst guard (`_LAST_LIVE_REFRESH_MONO`) + HTTP `402` triggers live balance refresh to `$0.00`.
4. Test 4 (`GET /api/quotas?refresh=false` Stale-While-Revalidate + `_run_turn_task_inner` WS broadcasts on `etype == "error"`):
   - Instant cached response on `GET /api/quotas?refresh=false` + background revalidation pushing `chatgpt_accounts_updated`
     (with `summary` and `accounts`) and `claude_accounts_updated` over WebSocket; `20s` manual cooldown on `refresh=true`.
   - `_run_turn_task_inner` on ChatGPT & Claude quota errors immediately broadcasts `0%` state via WebSocket.
5. Test 5 (Frontend JS syntax & VM coordinator checks, `v=103` cache sync, UTF-8 without BOM, `0% GPU`, `main.workspace` CSS invariant).

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or live server ports.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"
_INITIAL_LIVE_DB_SIG: tuple[int, int] | None = None
if LIVE_HUB_DB.exists():
    _st0 = LIVE_HUB_DB.stat()
    _INITIAL_LIVE_DB_SIG = (_st0.st_mtime_ns, _st0.st_size)


class _MiniAsgiResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> dict[str, Any]:
        return json.loads(self.text)


class _MiniAsgiClient:
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 53301)) -> None:
        self._app = app
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _MiniAsgiResponse:
        parsed_url = urllib.parse.urlsplit(path)
        raw_path = parsed_url.path or "/"
        qs = parsed_url.query
        payload = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
        hdr_list: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8765")]
        if json_body is not None:
            hdr_list.append((b"content-type", b"application/json"))
            hdr_list.append((b"content-length", str(len(payload)).encode("ascii")))
        for k, v in (headers or {}).items():
            hdr_list.append((k.lower().encode("latin-1"), str(v).encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": urllib.parse.unquote(raw_path),
            "raw_path": raw_path.encode("ascii", errors="ignore"),
            "query_string": qs.encode("ascii"),
            "headers": hdr_list,
            "client": self._client,
            "server": ("127.0.0.1", 8765),
        }

        sent_body = False
        resp_status = 500
        resp_headers: dict[str, str] = {}
        resp_chunks: list[bytes] = []

        async def _receive() -> dict[str, Any]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        async def _send(message: dict[str, Any]) -> None:
            nonlocal resp_status, resp_headers
            mtype = message.get("type")
            if mtype == "http.response.start":
                resp_status = int(message.get("status", 200))
                resp_headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in (message.get("headers") or [])
                }
            elif mtype == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    resp_chunks.append(chunk)

        await self._app(scope, _receive, _send)
        return _MiniAsgiResponse(resp_status, resp_headers, b"".join(resp_chunks))


class _FakeStdin:
    def __init__(self, on_json_line: Optional[Callable[[dict[str, Any]], None]] = None) -> None:
        self._buf = bytearray()
        self._on_json_line = on_json_line
        self.closed = False

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        while b"\n" in self._buf:
            raw_line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            txt = raw_line.decode("utf-8", errors="replace").strip()
            if txt and self._on_json_line is not None:
                try:
                    parsed = json.loads(txt)
                    if isinstance(parsed, dict):
                        self._on_json_line(parsed)
                except Exception:
                    pass

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, *, rpc_handler: Optional[Callable[[dict[str, Any], "_FakeProcess"], None]] = None) -> None:
        self.pid = 999123
        self.returncode: Optional[int] = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._done_evt = asyncio.Event()
        self.stdin = _FakeStdin(
            on_json_line=(lambda obj: rpc_handler(obj, self)) if rpc_handler is not None else None
        )

    def push_stdout_json(self, obj: dict[str, Any]) -> None:
        raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.stdout.feed_data(raw)

    def finish(self, rc: int = 0) -> None:
        if self.returncode is None:
            self.returncode = rc
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done_evt.set()

    async def wait(self) -> int:
        await self._done_evt.wait()
        return int(self.returncode or 0)

    def kill(self) -> None:
        self.finish(-9)

    def terminate(self) -> None:
        self.finish(-15)


class TestDynamicTelemetrySyncV103(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        if _INITIAL_LIVE_DB_SIG is not None:
            assert LIVE_HUB_DB.exists(), "Live data/hub.db disappeared!"
            st_now = LIVE_HUB_DB.stat()
            assert (st_now.st_mtime_ns, st_now.st_size) == _INITIAL_LIVE_DB_SIG, (
                f"Live data/hub.db was modified! before={_INITIAL_LIVE_DB_SIG}, "
                f"after={(st_now.st_mtime_ns, st_now.st_size)}"
            )

    def _assert_live_db_untouched(self) -> None:
        if _INITIAL_LIVE_DB_SIG is not None:
            self.assertTrue(LIVE_HUB_DB.exists())
            st = LIVE_HUB_DB.stat()
            self.assertEqual(
                (st.st_mtime_ns, st.st_size),
                _INITIAL_LIVE_DB_SIG,
                "Live production data/hub.db must remain untouched!",
            )

    async def asyncSetUp(self) -> None:
        self._db_sig_before_test = (
            (LIVE_HUB_DB.stat().st_mtime_ns, LIVE_HUB_DB.stat().st_size)
            if LIVE_HUB_DB.exists()
            else None
        )
        from app import (
            agy_engine,
            brain_reader,
            claude_engine,
            codex_engine,
            config,
            database,
            host_service_manager,
            main as main_mod,
            openrouter_service,
        )

        self.agy_engine = agy_engine
        self.brain_reader = brain_reader
        self.claude_engine = claude_engine
        self.codex_engine = codex_engine
        self.config = config
        self.database = database
        self.main_mod = main_mod
        self.or_service = openrouter_service

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v103_telemetry_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.hub_base = self.tmp_root / "Antigravity_Hub"
        data_dir = self.hub_base / "data"
        users_dir = self.hub_base / "users"
        projects_root = users_dir / "admin" / "projects"
        shared_dir = self.hub_base / "shared_projects"
        shared_uploads = shared_dir / "_uploads"
        shared_ctx = shared_dir / "_context"
        cli_homes = data_dir / "cli_homes"
        uploads_dir = data_dir / "uploads"
        chat_sandbox_dir = data_dir / "chat_sandbox"
        self.db_path = data_dir / "hub.db"

        for d in (
            self.hub_base,
            self.hub_base / "app",
            self.hub_base / ".venv",
            self.hub_base / ".git",
            data_dir,
            users_dir,
            projects_root,
            shared_dir,
            shared_uploads,
            shared_ctx,
            cli_homes,
            uploads_dir,
            chat_sandbox_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        (self.hub_base / "run_server.py").write_text("# test\n", encoding="utf-8")
        (self.hub_base / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        patch_map = {
            "BASE_DIR": self.hub_base,
            "DATA_DIR": data_dir,
            "DB_PATH": self.db_path,
            "USERS_DIR": users_dir,
            "USER_WORKSPACES_DIR": users_dir,
            "PROJECTS_ROOT": projects_root,
            "SHARED_PROJECTS_DIR": shared_dir,
            "SHARED_UPLOADS_DIR": shared_uploads,
            "SHARED_CONTEXT_DIR": shared_ctx,
            "CLI_HOMES_DIR": cli_homes,
            "UPLOADS_DIR": uploads_dir,
            "CHAT_SANDBOX_DIR": chat_sandbox_dir,
        }
        self._patches = []
        for mod in (
            config,
            database,
            brain_reader,
            agy_engine,
            codex_engine,
            claude_engine,
            main_mod,
            openrouter_service,
        ):
            for attr, val in patch_map.items():
                if hasattr(mod, attr):
                    p = patch.object(mod, attr, val)
                    p.start()
                    self._patches.append(p)

        hsm_inst = host_service_manager.HostServiceManager()
        for mod in (host_service_manager, database, main_mod):
            if hasattr(mod, "HOST_SERVICE_MANAGER"):
                p = patch.object(mod, "HOST_SERVICE_MANAGER", hsm_inst)
                p.start()
                self._patches.append(p)

        self._saved_agy_quotas = agy_engine._cached_quotas
        self._saved_agy_quotas_ts = agy_engine._cached_quotas_ts
        self._saved_agy_models = agy_engine._cached_models
        self._saved_agy_models_ts = agy_engine._cached_models_ts
        self._saved_or_ws_notifier = openrouter_service._WS_USER_NOTIFIER
        self._reset_telemetry_Coordinators()

    def _reset_telemetry_Coordinators(self) -> None:
        self.codex_engine._CHATGPT_REFRESH_IN_FLIGHT.clear()
        self.codex_engine._CHATGPT_LAST_PROBE_MONO.clear()
        self.codex_engine._CHATGPT_BACKOFF_UNTIL_MONO.clear()
        self.codex_engine._CHATGPT_LAST_ACTIVE_ACC.clear()

        self.claude_engine._CLAUDE_REFRESH_IN_FLIGHT.clear()
        self.claude_engine._CLAUDE_LAST_PROBE_MONO.clear()
        self.claude_engine._CLAUDE_BACKOFF_UNTIL_MONO.clear()
        self.claude_engine._CLAUDE_LAST_ACTIVE_ACC.clear()

        self.or_service._LIVE_REFRESH_IN_FLIGHT.clear()
        self.or_service._LAST_LIVE_REFRESH_MONO.clear()

        self.main_mod._last_turn_quota_refresh_mono = -1000.0
        self.main_mod._last_manual_quota_refresh_mono.clear()
        self.agy_engine._quota_warmup_scheduled = False

    async def asyncTearDown(self) -> None:
        for task_set in (
            self.codex_engine._CHATGPT_BG_TASKS,
            self.claude_engine._CLAUDE_BG_TASKS,
            self.main_mod._TELEMETRY_BG_TASKS,
            self.or_service._BACKGROUND_REFRESH_TASKS,
        ):
            for t in list(task_set):
                if not t.done():
                    t.cancel()
            if task_set:
                await asyncio.gather(*list(task_set), return_exceptions=True)
            task_set.clear()

        self._reset_telemetry_Coordinators()
        self.agy_engine._cached_quotas = self._saved_agy_quotas
        self.agy_engine._cached_quotas_ts = self._saved_agy_quotas_ts
        self.agy_engine._cached_models = self._saved_agy_models
        self.agy_engine._cached_models_ts = self._saved_agy_models_ts
        self.or_service._WS_USER_NOTIFIER = self._saved_or_ws_notifier

        for p in reversed(getattr(self, "_patches", [])):
            p.stop()
        self._tmp.cleanup()
        if self._db_sig_before_test is not None:
            st_after = LIVE_HUB_DB.stat()
            self.assertEqual(
                (st_after.st_mtime_ns, st_after.st_size),
                self._db_sig_before_test,
                "Live data/hub.db changed during test!",
            )
        self._assert_live_db_untouched()

    async def test_01_clamp_exhausted_quotas_and_single_account_turns(self) -> None:
        """Test 1: `0%` clamping for 5h & 7d limits + single-account ChatGPT & Claude turns with partial text."""
        reset_5h = "2026-11-15T16:00:00+00:00"
        reset_7d = "2026-11-20T00:00:00+00:00"
        initial_cgpt_q = {
            "ok": True,
            "plan_type": "plus",
            "ordinary_usage_allowed": True,
            "primary_5h": {"used_pct": 58.0, "remaining_pct": 42.0, "window_mins": 300, "reset_time": reset_5h},
            "secondary_7d": {"used_pct": 25.0, "remaining_pct": 75.0, "window_mins": 10080, "reset_time": reset_7d},
        }

        clamped_5h = self.codex_engine._clamp_exhausted_chatgpt_quotas(
            initial_cgpt_q,
            "You've hit your usage limit. Try again in 2 hours.",
            "usage_limit_reached",
        )
        self.assertEqual(clamped_5h["primary_5h"]["remaining_pct"], 0.0)
        self.assertEqual(clamped_5h["primary_5h"]["used_pct"], 100.0)
        self.assertEqual(clamped_5h["primary_5h"]["reset_time"], reset_5h)
        self.assertEqual(clamped_5h["secondary_7d"]["remaining_pct"], 75.0)
        self.assertFalse(clamped_5h["ordinary_usage_allowed"])

        clamped_7d = self.codex_engine._clamp_exhausted_chatgpt_quotas(
            initial_cgpt_q,
            "Weekly 7-day usage limit reached for ChatGPT Plus",
            "usage_limit_reached",
        )
        self.assertEqual(clamped_7d["secondary_7d"]["remaining_pct"], 0.0)
        self.assertEqual(clamped_7d["secondary_7d"]["used_pct"], 100.0)
        self.assertEqual(clamped_7d["secondary_7d"]["reset_time"], reset_7d)
        self.assertEqual(clamped_7d["primary_5h"]["remaining_pct"], 42.0)

        cl_clamped_5h = self.claude_engine._clamp_exhausted_claude_quotas(
            {
                "ok": True,
                "primary_5h": {"used_pct": 69.0, "remaining_pct": 31.0, "window_mins": 300, "reset_time": reset_5h},
                "secondary_7d": {"used_pct": 20.0, "remaining_pct": 80.0, "window_mins": 10080, "reset_time": reset_7d},
            },
            "Claude AI usage limit reached|1799999999",
        )
        self.assertEqual(cl_clamped_5h["primary_5h"]["remaining_pct"], 0.0)
        self.assertEqual(cl_clamped_5h["secondary_7d"]["remaining_pct"], 80.0)

        cl_clamped_7d = self.claude_engine._clamp_exhausted_claude_quotas(
            {
                "ok": True,
                "primary_5h": {"used_pct": 69.0, "remaining_pct": 31.0, "window_mins": 300, "reset_time": reset_5h},
                "secondary_7d": {"used_pct": 20.0, "remaining_pct": 80.0, "window_mins": 10080, "reset_time": reset_7d},
            },
            "You have reached your weekly 7-day Claude limit",
        )
        self.assertEqual(cl_clamped_7d["secondary_7d"]["remaining_pct"], 0.0)
        self.assertEqual(cl_clamped_7d["primary_5h"]["remaining_pct"], 31.0)

        # --- Full single-account `stream_codex_turn` with `auto_failover=False` & partial text ---
        await self.database.init_db()
        alice = await self.database.create_user(
            username="alice_quota",
            password="Password123!",
            display_name="Alice Quota",
            role="user",
            max_access_mode="read_only",
        )
        cgpt_acc = await self.database.upsert_user_chatgpt_account(
            alice["id"],
            raw_auth_json=json.dumps({"tokens": {"access_token": "tok_test_cgpt"}}),
            chatgpt_account_id="cgpt_single_1",
            email="alice@example.com",
            label="Single Plus",
            plan_type="plus",
            quotas=initial_cgpt_q,
            models=[dict(m) for m in self.codex_engine.DEFAULT_CODEX_MODELS],
            make_active=True,
        )
        async with self.database.aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE user_chatgpt_accounts SET auto_failover = 0 WHERE id = ?",
                (cgpt_acc["id"],),
            )
            await db.commit()

        codex_spawns = 0

        async def _fake_codex_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
            nonlocal codex_spawns
            codex_spawns += 1

            def _on_rpc(req: dict[str, Any], proc: _FakeProcess) -> None:
                rid = req.get("id")
                method = req.get("method")
                if method == "initialize":
                    proc.push_stdout_json({"jsonrpc": "2.0", "id": rid, "result": {"serverInfo": {"name": "mock"}}})
                elif method == "thread/start":
                    proc.push_stdout_json({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": "th_single_1"}}})
                elif method == "turn/start":
                    proc.push_stdout_json({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": "turn_1"}}})
                    # Emit partial text BEFORE hitting rate limit error!
                    proc.push_stdout_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {
                                "item": {
                                    "id": "msg_partial_1",
                                    "type": "agentMessage",
                                    "text": "Частичный ответ до исчерпания 5ч окна...",
                                }
                            },
                        }
                    )
                    proc.push_stdout_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {
                                "turn": {
                                    "id": "turn_1",
                                    "status": "failed",
                                    "error": {
                                        "message": "You've hit your usage limit. Upgrade to Pro or wait for reset.",
                                        "codexErrorInfo": "usage_limit_reached",
                                    },
                                }
                            },
                        }
                    )
                elif method == "account/rateLimits/read" and rid == "post_turn_rl":
                    proc.push_stdout_json({"jsonrpc": "2.0", "id": rid, "error": {"message": "unavailable"}})
                    proc.finish(0)

            return _FakeProcess(rpc_handler=_on_rpc)

        fake_codex_bin = self.tmp_root / "codex_mock.exe"
        fake_codex_bin.write_text("mock", encoding="utf-8")
        with (
            patch.object(self.codex_engine, "resolve_codex_bin", return_value=str(fake_codex_bin)),
            patch.object(self.codex_engine, "_terminate_codex_proc_tree", new=AsyncMock()),
            patch.object(self.codex_engine.asyncio, "create_subprocess_exec", side_effect=_fake_codex_exec),
        ):
            events_cgpt = [
                ev
                async for ev in self.codex_engine.stream_codex_turn(
                    chat_id="chat_cgpt_limit",
                    kind="chat",
                    project_name="",
                    model="codex::gpt-5.4",
                    access_mode="read_only",
                    prompt="Проверь лимит",
                    attachments=[],
                    codex_thread_id="",
                    codex_account_id="",
                    history_messages=[],
                    user=alice,
                )
            ]

        self.assertEqual(codex_spawns, 1, "Must NOT run capacity retries on quota exhaustion")
        err_evts = [e for e in events_cgpt if e.get("type") == "error"]
        self.assertEqual(len(err_evts), 1)
        self.assertTrue(err_evts[0].get("quota_exhausted"))
        self.assertIn("Частичный ответ", err_evts[0].get("content", ""))

        fresh_cgpt_acc = await self.database.get_user_chatgpt_account(alice["id"], cgpt_acc["id"])
        assert fresh_cgpt_acc is not None
        self.assertEqual(fresh_cgpt_acc["quotas"]["primary_5h"]["remaining_pct"], 0.0)
        self.assertEqual(fresh_cgpt_acc["quotas"]["secondary_7d"]["remaining_pct"], 75.0)
        self.assertTrue(bool(fresh_cgpt_acc.get("last_error")), "last_error must not be wiped in finally:")

        # --- Full single-account `stream_claude_turn` with partial text & weekly limit error ---
        claude_acc = await self.database.upsert_user_claude_account(
            alice["id"],
            raw_credentials_json=json.dumps(
                {"claudeAiOauth": {"accessToken": "\x73k-ant-oat01-testtoken1234567890", "subscriptionType": "pro"}}
            ),
            claude_account_id="claude_single_1",
            email="alice@claude.ai",
            label="Single Claude Pro",
            plan_type="pro",
            auth_method="oauth",
            quotas={
                "ok": True,
                "primary_5h": {"used_pct": 30.0, "remaining_pct": 70.0, "window_mins": 300, "reset_time": reset_5h},
                "secondary_7d": {"used_pct": 92.0, "remaining_pct": 8.0, "window_mins": 10080, "reset_time": reset_7d},
            },
            models=[dict(m) for m in self.claude_engine.DEFAULT_CLAUDE_CODE_MODELS],
            make_active=True,
        )

        claude_spawns = 0

        async def _fake_claude_spawn(argv: list[str], env: dict[str, str], cwd: Path) -> _FakeProcess:
            nonlocal claude_spawns
            claude_spawns += 1
            fp = _FakeProcess()
            fp.push_stdout_json(
                {
                    "type": "assistant",
                    "session_id": "sess_cl_1",
                    "message": {"content": [{"type": "text", "text": "Частичный текст Claude..."}]},
                }
            )
            fp.push_stdout_json(
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "session_id": "sess_cl_1",
                    "result": "Claude AI usage limit reached (weekly 7-day window)",
                }
            )
            fp.finish(1)
            return fp

        fake_claude_bin = self.tmp_root / "claude_mock.exe"
        fake_claude_bin.write_text("mock", encoding="utf-8")
        with (
            patch.object(self.claude_engine, "resolve_claude_bin", return_value=str(fake_claude_bin)),
            patch.object(self.claude_engine, "_spawn", side_effect=_fake_claude_spawn),
            patch.object(self.claude_engine, "_terminate_proc_tree", new=AsyncMock()),
        ):
            events_cl = [
                ev
                async for ev in self.claude_engine.stream_claude_turn(
                    chat_id="chat_cl_limit",
                    kind="chat",
                    project_name="",
                    model="claude-code::sonnet",
                    access_mode="read_only",
                    prompt="Проверь недельный лимит",
                    attachments=[],
                    claude_session_id="",
                    claude_account_id="",
                    history_messages=[],
                    user=alice,
                )
            ]

        self.assertEqual(claude_spawns, 1, "Must NOT run capacity retries on Claude quota exhaustion")
        cl_errs = [e for e in events_cl if e.get("type") == "error"]
        self.assertEqual(len(cl_errs), 1)
        self.assertTrue(cl_errs[0].get("quota_exhausted"))

        fresh_cl_acc = await self.database.get_user_claude_account(alice["id"], claude_acc["id"])
        assert fresh_cl_acc is not None
        self.assertEqual(fresh_cl_acc["quotas"]["secondary_7d"]["remaining_pct"], 0.0)
        self.assertEqual(fresh_cl_acc["quotas"]["primary_5h"]["remaining_pct"], 70.0)

    async def test_02_read_rate_limits_from_open_proc_and_post_turn_persist(self) -> None:
        """Test 2: In-session `account/rateLimits/read` (`0` extra processes) & graceful timeout."""
        # Case A: Direct response with `id == "post_turn_rl"`
        def _rpc_ok(req: dict[str, Any], proc: _FakeProcess) -> None:
            if req.get("method") == "account/rateLimits/read" and req.get("id") == "post_turn_rl":
                proc.push_stdout_json(
                    {
                        "jsonrpc": "2.0",
                        "id": "post_turn_rl",
                        "result": {
                            "rateLimits": {
                                "planType": "plus",
                                "ordinaryUsageAllowed": True,
                                "primary": {"usedPercent": 36.0, "windowDurationMins": 300, "resetsAt": 1800000000},
                                "secondary": {"usedPercent": 12.0, "windowDurationMins": 10080, "resetsAt": 1800500000},
                            }
                        },
                    }
                )

        proc_ok = _FakeProcess(rpc_handler=_rpc_ok)
        with patch.object(self.codex_engine.asyncio, "create_subprocess_exec", side_effect=AssertionError("No spawn!")):
            parsed = await self.codex_engine._read_rate_limits_from_open_proc(proc_ok, timeout_sec=1.5)

        assert parsed is not None
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["primary_5h"]["remaining_pct"], 64.0)
        self.assertEqual(parsed["secondary_7d"]["remaining_pct"], 88.0)

        # Case B: Hung stdout -> soft timeout without raising
        proc_hung = _FakeProcess()
        t0 = time.monotonic()
        timed_out = await self.codex_engine._read_rate_limits_from_open_proc(proc_hung, timeout_sec=0.22)
        elapsed = time.monotonic() - t0
        self.assertIsNone(timed_out)
        self.assertLess(elapsed, 1.5)

        # Case C: Successful `stream_codex_turn` reads post-turn rate limits from the SAME open process
        await self.database.init_db()
        bob = await self.database.create_user(
            username="bob_postturn",
            password="Password123!",
            display_name="Bob",
            role="user",
            max_access_mode="read_only",
        )
        bob_acc = await self.database.upsert_user_chatgpt_account(
            bob["id"],
            raw_auth_json=json.dumps({"tokens": {"access_token": "tok_bob"}}),
            chatgpt_account_id="cgpt_bob_1",
            email="bob@example.com",
            label="Bob Plus",
            plan_type="plus",
            quotas={
                "ok": True,
                "primary_5h": {"used_pct": 1.0, "remaining_pct": 99.0, "window_mins": 300, "reset_time": ""},
                "secondary_7d": {"used_pct": 1.0, "remaining_pct": 99.0, "window_mins": 10080, "reset_time": ""},
            },
            models=[dict(m) for m in self.codex_engine.DEFAULT_CODEX_MODELS],
            make_active=True,
        )

        spawns_bob = 0

        async def _fake_bob_turn_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
            nonlocal spawns_bob
            spawns_bob += 1

            def _on_rpc(req: dict[str, Any], proc: _FakeProcess) -> None:
                rid = req.get("id")
                method = req.get("method")
                if method == "initialize":
                    proc.push_stdout_json({"jsonrpc": "2.0", "id": rid, "result": {"serverInfo": {"name": "mock"}}})
                elif method == "thread/start":
                    proc.push_stdout_json({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": "th_bob_1"}}})
                elif method == "turn/start":
                    proc.push_stdout_json({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": "turn_bob_1"}}})
                    proc.push_stdout_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "item/completed",
                            "params": {"item": {"id": "m1", "type": "agentMessage", "text": "Готово!"}},
                        }
                    )
                    proc.push_stdout_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {"turn": {"id": "turn_bob_1", "status": "completed"}},
                        }
                    )
                elif method == "account/rateLimits/read" and rid == "post_turn_rl":
                    proc.push_stdout_json(
                        {
                            "jsonrpc": "2.0",
                            "id": "post_turn_rl",
                            "result": {
                                "rateLimits": {
                                    "planType": "plus",
                                    "ordinaryUsageAllowed": True,
                                    "primary": {"usedPercent": 49.0, "windowDurationMins": 300, "resetsAt": 1800001234},
                                    "secondary": {"usedPercent": 19.0, "windowDurationMins": 10080, "resetsAt": 1800601234},
                                }
                            },
                        }
                    )
                    proc.finish(0)

            return _FakeProcess(rpc_handler=_on_rpc)

        fake_codex_bin = self.tmp_root / "codex_bob.exe"
        fake_codex_bin.write_text("mock", encoding="utf-8")
        with (
            patch.object(self.codex_engine, "resolve_codex_bin", return_value=str(fake_codex_bin)),
            patch.object(self.codex_engine, "_terminate_codex_proc_tree", new=AsyncMock()),
            patch.object(self.codex_engine.asyncio, "create_subprocess_exec", side_effect=_fake_bob_turn_exec),
        ):
            bob_evts = [
                ev
                async for ev in self.codex_engine.stream_codex_turn(
                    chat_id="chat_bob_ok",
                    kind="chat",
                    project_name="",
                    model="codex::gpt-5.4",
                    access_mode="read_only",
                    prompt="Скажи привет",
                    attachments=[],
                    codex_thread_id="",
                    codex_account_id="",
                    history_messages=[],
                    user=bob,
                )
            ]

        self.assertEqual(spawns_bob, 1, "Post-turn rateLimits must reuse the open codex app-server process!")
        self.assertTrue(any(e.get("type") == "done" for e in bob_evts))
        updated_bob_acc = await self.database.get_user_chatgpt_account(bob["id"], bob_acc["id"])
        assert updated_bob_acc is not None
        self.assertEqual(updated_bob_acc["quotas"]["primary_5h"]["remaining_pct"], 51.0)
        self.assertEqual(updated_bob_acc["quotas"]["secondary_7d"]["remaining_pct"], 81.0)

    async def test_03_antiban_dedup_cooldowns_reset_expiry_429_backoff_and_openrouter_402(self) -> None:
        """Test 3: Staleness (`reset_time + 10s`), in-flight dedup, cooldowns, Claude `429` 300s backoff, OR `402`."""
        now_utc = datetime.now(timezone.utc)
        fresh_cached_iso = (now_utc - timedelta(seconds=35)).isoformat()
        old_cached_iso = (now_utc - timedelta(seconds=340)).isoformat()
        future_reset_iso = (now_utc + timedelta(minutes=45)).isoformat()
        past_reset_iso = (now_utc - timedelta(seconds=15)).isoformat()

        # 1. ChatGPT & Claude & AGY staleness including `reset_time + 10s`
        fresh_acc = {
            "cached_at": fresh_cached_iso,
            "quotas": {"primary_5h": {"remaining_pct": 0.0, "reset_time": future_reset_iso}},
        }
        self.assertFalse(self.codex_engine.is_chatgpt_quota_cache_stale(fresh_acc, max_age_sec=300.0))
        self.assertFalse(self.claude_engine.is_claude_quota_cache_stale(fresh_acc, max_age_sec=300.0))

        age_stale_acc = {
            "cached_at": old_cached_iso,
            "quotas": {"primary_5h": {"remaining_pct": 80.0, "reset_time": future_reset_iso}},
        }
        self.assertTrue(self.codex_engine.is_chatgpt_quota_cache_stale(age_stale_acc, max_age_sec=300.0))
        self.assertTrue(self.claude_engine.is_claude_quota_cache_stale(age_stale_acc, max_age_sec=300.0))

        # Cached 35s ago (< 300s), BEFORE `past_reset_iso` (15s ago), and `now >= reset_time + 10s` -> MUST be stale!
        reset_expired_acc = {
            "cached_at": fresh_cached_iso,
            "quotas": {"primary_5h": {"remaining_pct": 0.0, "reset_time": past_reset_iso}},
        }
        self.assertTrue(self.codex_engine.is_chatgpt_quota_cache_stale(reset_expired_acc, max_age_sec=300.0))
        self.assertTrue(self.claude_engine.is_claude_quota_cache_stale(reset_expired_acc, max_age_sec=300.0))

        self.agy_engine._cached_quotas = {
            "ok": True,
            "updated_at": fresh_cached_iso,
            "groups": [{"id": "gemini", "buckets": [{"window": "5h", "remaining_pct": 0.0, "reset_time": past_reset_iso}]}],
        }
        self.agy_engine._cached_quotas_ts = time.monotonic() - 35.0
        self.assertTrue(self.agy_engine.is_agy_quota_cache_stale(300.0))

        # 2. ChatGPT Anti-Ban in-flight deduplication + 90s cooldown
        await self.database.init_db()
        u1 = await self.database.create_user(
            username="antiban_user",
            password="Password123!",
            display_name="AntiBan",
            role="user",
            max_access_mode="read_only",
        )
        cg_acc = await self.database.upsert_user_chatgpt_account(
            u1["id"],
            raw_auth_json=json.dumps({"tokens": {"access_token": "tok_ab"}}),
            chatgpt_account_id="cg_ab_1",
            email="ab@example.com",
            label="AB Plus",
            plan_type="plus",
            quotas={"ok": True, "primary_5h": {"remaining_pct": 0.0, "used_pct": 100.0, "reset_time": past_reset_iso}},
            models=[dict(m) for m in self.codex_engine.DEFAULT_CODEX_MODELS],
            make_active=True,
        )
        # Ensure `cached_at` is strictly before `past_reset_iso`
        await self.database.update_user_chatgpt_account_cache(u1["id"], cg_acc["id"], last_error="")
        async with self.database.aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE user_chatgpt_accounts SET cached_at = ? WHERE id = ?",
                (fresh_cached_iso, cg_acc["id"]),
            )
            await db.commit()

        cgpt_probe_gate = asyncio.Event()
        cgpt_probe_calls = 0
        cgpt_ws_callbacks: list[str] = []

        async def _mock_fetch_cgpt(uid: str, acc_id: str, *, include_models: bool = False) -> dict[str, Any]:
            nonlocal cgpt_probe_calls
            cgpt_probe_calls += 1
            await cgpt_probe_gate.wait()
            return (
                await self.database.update_user_chatgpt_account_cache(
                    uid,
                    acc_id,
                    quotas={
                        "ok": True,
                        "primary_5h": {"remaining_pct": 100.0, "used_pct": 0.0, "reset_time": future_reset_iso},
                    },
                    last_error="",
                )
                or {}
            )

        async def _on_cgpt_updated(uid: str) -> None:
            cgpt_ws_callbacks.append(uid)

        with (
            patch.object(self.codex_engine, "resolve_codex_bin", return_value="codex"),
            patch.object(self.codex_engine, "fetch_chatgpt_account_quotas", side_effect=_mock_fetch_cgpt),
        ):
            ok1 = self.codex_engine.schedule_chatgpt_live_refresh(
                u1["id"], max_age_sec=300.0, min_interval_sec=90.0, on_updated=_on_cgpt_updated
            )
            ok2_dup = self.codex_engine.schedule_chatgpt_live_refresh(
                u1["id"], max_age_sec=300.0, min_interval_sec=90.0, on_updated=_on_cgpt_updated
            )
            self.assertTrue(ok1)
            self.assertFalse(ok2_dup, "Concurrent in-flight refresh must be deduplicated!")

            cgpt_probe_gate.set()
            await asyncio.gather(*list(self.codex_engine._CHATGPT_BG_TASKS))
            self.assertEqual(cgpt_probe_calls, 1)
            self.assertEqual(cgpt_ws_callbacks, [u1["id"]])

            ok3_cooldown = self.codex_engine.schedule_chatgpt_live_refresh(
                u1["id"], max_age_sec=0.0, min_interval_sec=90.0, on_updated=_on_cgpt_updated
            )
            self.assertFalse(ok3_cooldown, "Repeat refresh within 90s cooldown must be blocked!")
            self.assertEqual(cgpt_probe_calls, 1)

        # 3. Claude HTTP `429` -> 0 retry loops + `300s` Anti-Ban backoff
        cl_acc = await self.database.upsert_user_claude_account(
            u1["id"],
            raw_credentials_json=json.dumps(
                {"claudeAiOauth": {"accessToken": "sk-ant-oat01-429testtoken", "subscriptionType": "pro"}}
            ),
            claude_account_id="cl_ab_1",
            email="ab@claude.ai",
            label="AB Claude",
            plan_type="pro",
            auth_method="oauth",
            quotas={"ok": True, "primary_5h": {"remaining_pct": 0.0, "used_pct": 100.0, "reset_time": past_reset_iso}},
            models=[dict(m) for m in self.claude_engine.DEFAULT_CLAUDE_CODE_MODELS],
            make_active=True,
        )
        async with self.database.aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE user_claude_accounts SET cached_at = ? WHERE id = ?",
                (fresh_cached_iso, cl_acc["id"]),
            )
            await db.commit()

        http_429_calls = 0

        def _mock_claude_429(access_token: str, cli_version: str) -> tuple[int, Any]:
            nonlocal http_429_calls
            http_429_calls += 1
            return 429, "Rate limit exceeded"

        with patch.object(self.claude_engine, "_http_get_usage_sync", side_effect=_mock_claude_429):
            sched_cl_1 = self.claude_engine.schedule_claude_live_refresh(
                u1["id"], max_age_sec=300.0, min_interval_sec=0.0
            )
            self.assertTrue(sched_cl_1)
            await asyncio.gather(*list(self.claude_engine._CLAUDE_BG_TASKS))
            self.assertEqual(http_429_calls, 1, "HTTP 429 must NOT trigger retry loops!")

            backoff_key = f"{u1['id']}:{cl_acc['id']}"
            backoff_until = self.claude_engine._CLAUDE_BACKOFF_UNTIL_MONO.get(backoff_key, 0.0)
            self.assertGreater(backoff_until, time.monotonic() + 290.0)

            # Even with `min_interval_sec=0.0`, non-forced schedule must respect the 300s 429 backoff!
            sched_cl_blocked = self.claude_engine.schedule_claude_live_refresh(
                u1["id"], force=False, max_age_sec=0.0, min_interval_sec=0.0
            )
            self.assertFalse(sched_cl_blocked, "300s Anti-Ban backoff must suppress background probes after 429!")

        # 4. OpenRouter 30s burst guard + HTTP 402 Insufficient Credits auto-refresh to $0.00
        await self.database.upsert_user_openrouter_config(
            u1["id"],
            new_api_key="\x73k-or-v1-99998888777766665555444433332222",
            is_enabled=True,
            cached_status={
                "label": "AB Key",
                "is_free_tier": False,
                "credits_remaining": 4.25,
                "total_credits": 10.0,
                "total_credits_usage": 5.75,
            },
        )

        or_live_fetches = 0
        or_ws_events: list[dict[str, Any]] = []

        async def _mock_or_key_credits(api_key: str) -> dict[str, Any]:
            nonlocal or_live_fetches
            or_live_fetches += 1
            return {
                "label": "AB Key",
                "limit": None,
                "limit_remaining": None,
                "limit_reset": "",
                "include_byok_in_limit": False,
                "usage": 10.0,
                "usage_daily": 4.25,
                "usage_weekly": 10.0,
                "usage_monthly": 10.0,
                "byok_usage": 0.0,
                "is_free_tier": False,
                "is_provisioning_key": False,
                "total_credits": 10.0,
                "total_credits_usage": 10.0,
                "credits_remaining": 0.0,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        async def _capture_or_ws(uid: str, payload: dict[str, Any]) -> None:
            or_ws_events.append(payload)

        self.or_service.set_openrouter_ws_notifier(_capture_or_ws)
        with patch.object(self.or_service, "fetch_openrouter_key_and_credits", side_effect=_mock_or_key_credits):
            res_or_1 = await self.or_service.refresh_user_openrouter_live_status(u1["id"], force=True)
            res_or_2 = await self.or_service.refresh_user_openrouter_live_status(u1["id"], force=True)
            self.assertIsNotNone(res_or_1)
            self.assertIsNone(res_or_2, "30s burst guard must block back-to-back OpenRouter refreshes!")
            self.assertEqual(or_live_fetches, 1)

            # Now simulate elapsed > 30s and an HTTP 402 Insufficient Credits error during `execute_openrouter_chat`
            await self.database.upsert_user_openrouter_config(
                u1["id"],
                max_price_per_m=20.0,
                daily_spend_limit_usd=50.0,
                cached_status={
                    "label": "AB Key",
                    "is_free_tier": False,
                    "credits_remaining": 4.25,
                    "total_credits": 10.0,
                    "total_credits_usage": 5.75,
                },
            )
            self.or_service._LAST_LIVE_REFRESH_MONO.clear()
            or_ws_events.clear()

            def _mock_or_402(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
                return 402, {"error": {"message": "Insufficient credits. Add more credits."}}

            or_bundle = await self.or_service.get_user_openrouter_secret_bundle(u1["id"])
            assert or_bundle is not None
            with patch.object(self.or_service, "_openrouter_http_sync", side_effect=_mock_or_402):
                res_402 = await self.or_service.execute_openrouter_chat(
                    or_bundle,
                    model="openai/gpt-4.1-mini",
                    prompt="Привет",
                    explicit_user_choice=True,
                )
                self.assertFalse(res_402.get("ok", True))
                if self.or_service._BACKGROUND_REFRESH_TASKS:
                    await asyncio.gather(*list(self.or_service._BACKGROUND_REFRESH_TASKS), return_exceptions=True)

            self.assertEqual(or_live_fetches, 2, "HTTP 402 must trigger background OpenRouter balance refresh!")
            self.assertTrue(any(ev.get("type") == "openrouter_updated" for ev in or_ws_events))
            or_summary = await self.database.get_openrouter_sidebar_summary(u1["id"])
            self.assertEqual(or_summary["credits_remaining"], 0.0)

    async def test_04_swr_api_quotas_and_post_turn_error_ws_broadcasts(self) -> None:
        """Test 4: Instant `GET /api/quotas?refresh=false` SWR + immediate WS broadcasts on turn quota error."""
        from app import auth

        await self.database.init_db()
        admin_user = await self.database.get_user_by_id("admin")
        assert admin_user is not None
        admin_tok = auth.create_user_session_token("admin", int(admin_user.get("session_version") or 1))
        admin_hdrs = {"Authorization": f"Bearer {admin_tok}"}

        old_iso = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        future_reset = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()

        cgpt_acc = await self.database.upsert_user_chatgpt_account(
            "admin",
            raw_auth_json=json.dumps({"tokens": {"access_token": "tok_swr"}}),
            chatgpt_account_id="cgpt_swr_1",
            email="admin@openai.com",
            label="Admin Plus",
            plan_type="plus",
            quotas={"ok": True, "primary_5h": {"remaining_pct": 62.0, "used_pct": 38.0, "reset_time": future_reset}},
            models=[dict(m) for m in self.codex_engine.DEFAULT_CODEX_MODELS],
            make_active=True,
        )
        cl_acc = await self.database.upsert_user_claude_account(
            "admin",
            raw_credentials_json=json.dumps(
                {"claudeAiOauth": {"accessToken": "sk-ant-oat01-swrtoken", "subscriptionType": "max"}}
            ),
            claude_account_id="cl_swr_1",
            email="admin@claude.ai",
            label="Admin Claude Max",
            plan_type="max",
            auth_method="oauth",
            quotas={"ok": True, "primary_5h": {"remaining_pct": 55.0, "used_pct": 45.0, "reset_time": future_reset}},
            models=[dict(m) for m in self.claude_engine.DEFAULT_CLAUDE_CODE_MODELS],
            make_active=True,
        )
        async with self.database.aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE user_chatgpt_accounts SET cached_at = ? WHERE id = ?", (old_iso, cgpt_acc["id"]))
            await db.execute("UPDATE user_claude_accounts SET cached_at = ? WHERE id = ?", (old_iso, cl_acc["id"]))
            await db.commit()

        self.agy_engine._cached_quotas = {
            "ok": True,
            "groups": [{"id": "gemini", "title": "Модели Gemini", "buckets": [{"window": "5h", "remaining_pct": 88.0, "reset_time": future_reset}]}],
            "remaining_credits": 500,
            "gemini_5h_pct": 88.0,
            "gemini_weekly_pct": 92.0,
            "claude_5h_pct": 75.0,
            "claude_weekly_pct": 80.0,
            "tier_summary": "Google AI Pro",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.agy_engine._cached_quotas_ts = time.monotonic()
        self.agy_engine._cached_models = [
            {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "vendor": "Google"},
        ]
        self.agy_engine._cached_models_ts = time.monotonic()

        ws_user_events: list[tuple[str, dict[str, Any]]] = []

        async def _record_ws_user(uid: str, payload: dict[str, Any]) -> None:
            ws_user_events.append((uid, payload))

        async def _mock_bg_cgpt(uid: str, acc_id: str, *, include_models: bool = False) -> dict[str, Any]:
            return (
                await self.database.update_user_chatgpt_account_cache(
                    uid,
                    acc_id,
                    quotas={"ok": True, "primary_5h": {"remaining_pct": 91.0, "used_pct": 9.0, "reset_time": future_reset}},
                    last_error="",
                )
                or {}
            )

        async def _mock_bg_claude(uid: str, acc_id: str) -> dict[str, Any]:
            q = {"ok": True, "primary_5h": {"remaining_pct": 94.0, "used_pct": 6.0, "reset_time": future_reset}}
            await self.database.update_claude_account_telemetry(uid, acc_id, quotas=q, last_error="")
            return q

        client = _MiniAsgiClient(self.main_mod.app)
        with (
            patch.object(self.codex_engine, "resolve_codex_bin", return_value="codex"),
            patch.object(self.codex_engine, "fetch_chatgpt_account_quotas", side_effect=_mock_bg_cgpt),
            patch.object(self.claude_engine, "refresh_claude_account_quotas", side_effect=_mock_bg_claude),
            patch.object(self.main_mod.ws_manager, "broadcast_user", side_effect=_record_ws_user),
            patch.object(self.main_mod.ws_manager, "broadcast_admins", new=AsyncMock()),
            patch.object(self.agy_engine, "fetch_account_quotas", side_effect=AssertionError("refresh=false must NOT block on agy CLI!")),
        ):
            resp = await client.request("GET", "/api/quotas?refresh=false", headers=admin_hdrs)
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["quotas"]["gemini_5h_pct"], 88.0)
            self.assertEqual(body["chatgpt_summary"]["quotas"]["primary_5h"]["remaining_pct"], 62.0)
            self.assertEqual(body["claude_summary"]["quotas"]["primary_5h"]["remaining_pct"], 55.0)

            # Wait for non-blocking SWR background tasks to finish and push WebSocket events
            await asyncio.gather(
                *list(self.codex_engine._CHATGPT_BG_TASKS),
                *list(self.claude_engine._CLAUDE_BG_TASKS),
                *list(self.main_mod._TELEMETRY_BG_TASKS),
            )

        cgpt_ws = [p for _, p in ws_user_events if p.get("type") == "chatgpt_accounts_updated"]
        cl_ws = [p for _, p in ws_user_events if p.get("type") == "claude_accounts_updated"]
        self.assertGreaterEqual(len(cgpt_ws), 1)
        self.assertGreaterEqual(len(cl_ws), 1)
        self.assertEqual(cgpt_ws[-1]["summary"]["quotas"]["primary_5h"]["remaining_pct"], 91.0)
        self.assertIsInstance(cgpt_ws[-1].get("accounts"), list, "chatgpt_accounts_updated must include `accounts`!")
        self.assertEqual(cl_ws[-1]["summary"]["quotas"]["primary_5h"]["remaining_pct"], 94.0)

        # Manual `GET /api/quotas?refresh=true` enforces 20s anti-spam cooldown
        manual_force_flags: list[bool] = []

        async def _mock_manual_quotas(force_refresh: bool = False) -> dict[str, Any]:
            manual_force_flags.append(bool(force_refresh))
            return self.agy_engine._cached_quotas or {"ok": True, "groups": []}

        with (
            patch.object(self.main_mod, "fetch_account_quotas", side_effect=_mock_manual_quotas),
            patch.object(self.main_mod.ws_manager, "broadcast_user", new=AsyncMock()),
            patch.object(self.main_mod.ws_manager, "broadcast_admins", new=AsyncMock()),
        ):
            r_man_1 = await client.request("GET", "/api/quotas?refresh=true", headers=admin_hdrs)
            r_man_2 = await client.request("GET", "/api/quotas?refresh=true", headers=admin_hdrs)
            self.assertEqual(r_man_1.status_code, 200)
            self.assertEqual(r_man_2.status_code, 200)
            self.assertEqual(manual_force_flags, [True, False], "Second manual refresh within 20s must set force_refresh=False")

        # --- `_run_turn_task_inner` on `etype == "error"` immediately broadcasts `0%` over WebSocket ---
        ws_user_events.clear()
        chat_cgpt = await self.database.create_chat(
            title="ChatGPT Limit Chat",
            kind="chat",
            model="codex::gpt-5.4",
            access_mode="read_only",
            owner_id="admin",
        )

        async def _fake_stream_codex_err(**kwargs: Any):
            await self.database.update_user_chatgpt_account_cache(
                "admin",
                cgpt_acc["id"],
                quotas={"ok": True, "primary_5h": {"remaining_pct": 0.0, "used_pct": 100.0, "reset_time": future_reset}},
                last_error="usage_limit_reached",
            )
            yield {
                "type": "error",
                "error": "Лимит запросов ChatGPT (5ч) исчерпан",
                "content": "",
                "thinking": "",
                "tool_calls": [],
                "quota_exhausted": True,
            }

        async def _fake_stream_claude_err(**kwargs: Any):
            await self.database.update_claude_account_telemetry(
                "admin",
                cl_acc["id"],
                quotas={"ok": True, "primary_5h": {"remaining_pct": 0.0, "used_pct": 100.0, "reset_time": future_reset}},
                last_error="Claude AI usage limit reached",
            )
            yield {
                "type": "error",
                "error": "Лимит Claude Code исчерпан",
                "content": "",
                "thinking": "",
                "tool_calls": [],
                "quota_exhausted": True,
            }

        with (
            patch.object(self.main_mod, "stream_codex_turn", side_effect=_fake_stream_codex_err),
            patch.object(self.main_mod, "stream_claude_turn", side_effect=_fake_stream_claude_err),
            patch.object(self.main_mod, "fetch_available_models", new=AsyncMock(return_value=self.agy_engine._cached_models)),
            patch.object(self.codex_engine, "schedule_chatgpt_live_refresh", return_value=True) as mock_sched_cg,
            patch.object(self.claude_engine, "schedule_claude_live_refresh", return_value=True) as mock_sched_cl,
            patch.object(self.main_mod.ws_manager, "broadcast_user", side_effect=_record_ws_user),
            patch.object(self.main_mod.ws_manager, "broadcast_for_chat", new=AsyncMock()),
            patch.object(self.main_mod.ws_manager, "broadcast_admins", new=AsyncMock()),
        ):
            await self.main_mod._run_turn_task_inner(
                chat_id=chat_cgpt["id"],
                prompt="Тест ошибки квоты ChatGPT",
                attachments=[],
                override_model="codex::gpt-5.4",
                override_access_mode="read_only",
                trigger_user=admin_user,
            )
            if self.main_mod._TELEMETRY_BG_TASKS:
                await asyncio.gather(*list(self.main_mod._TELEMETRY_BG_TASKS))
            mock_sched_cg.assert_called_once()

            await self.main_mod._run_turn_task_inner(
                chat_id=chat_cgpt["id"],
                prompt="Тест ошибки квоты Claude",
                attachments=[],
                override_model="claude-code::sonnet",
                override_access_mode="read_only",
                trigger_user=admin_user,
            )
            if self.main_mod._TELEMETRY_BG_TASKS:
                await asyncio.gather(*list(self.main_mod._TELEMETRY_BG_TASKS))
            mock_sched_cl.assert_called_once()

        err_cgpt_ws = [p for _, p in ws_user_events if p.get("type") == "chatgpt_accounts_updated"]
        err_cl_ws = [p for _, p in ws_user_events if p.get("type") == "claude_accounts_updated"]
        self.assertGreaterEqual(len(err_cgpt_ws), 1)
        self.assertGreaterEqual(len(err_cl_ws), 1)
        self.assertEqual(err_cgpt_ws[-1]["summary"]["quotas"]["primary_5h"]["remaining_pct"], 0.0)
        self.assertEqual(err_cl_ws[-1]["summary"]["quotas"]["primary_5h"]["remaining_pct"], 0.0)

    def test_05_frontend_js_vm_coordinator_version_sync_and_css_invariants(self) -> None:
        """Test 5: `node --check app/static/app.js`, JS coordinator invariants, `v=103`, UTF-8 no BOM, `0% GPU`."""
        from app.config import subprocess_hidden_kwargs

        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["ORT_DISABLE_ALL_GPU"] = "1"

        app_js_path = ROOT_DIR / "app" / "static" / "app.js"
        res_check = subprocess.run(
            ["node", "--check", str(app_js_path)],
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            **subprocess_hidden_kwargs(),
        )
        self.assertEqual(
            res_check.returncode,
            0,
            f"`node --check app/static/app.js` failed:\n{res_check.stderr}",
        )

        app_js = app_js_path.read_text(encoding="utf-8")
        # Verify no legacy rigid 3-minute interval remains
        self.assertFalse(
            "setInterval(() => {\n    if (!document.hidden) {\n      loadQuotasAndTokens(false);\n    }\n  }, 180000)" in app_js,
            "Legacy rigid 180000ms interval must be replaced by startTelemetryCoordinator()",
        )
        for required_snippet in (
            'TELEMETRY_POLL_STORAGE_KEY = "agy_hub_last_telemetry_poll_ts"',
            "TELEMETRY_POLL_BASE_MS = 240000",
            "TELEMETRY_POLL_JITTER_MS = 15000",
            "TELEMETRY_CROSS_TAB_DEDUP_MS = 90000",
            "TELEMETRY_RESET_GRACE_MS = 12000",
            "function startTelemetryCoordinator()",
            "function runLocalTelemetryCountdownTick()",
            "function collectActiveProviderResetTimestamps()",
            "function markTelemetrySyncedNow()",
            "refreshOpenHandoffModalLiveState()",
            "processedQuotaResetTimestamps",
            "rMs + TELEMETRY_RESET_GRACE_MS",
            "renderChatGptModal()",
            "renderClaudeModal()",
            "renderOpenRouterModal({ syncInputs: false })",
            "mobClaudePillBtn",
        ):
            self.assertTrue(
                required_snippet in app_js,
                f"Missing required telemetry reactivity snippet: {required_snippet}",
            )

        # Verify `sw.js` and `index.html` static cache version sync (`v >= 103`)
        sw_txt = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        idx_txt = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")

        m_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_txt)
        self.assertIsNotNone(m_cache, "CACHE_NAME missing in sw.js")
        v_num = int(m_cache.group(1))
        self.assertGreaterEqual(v_num, 103, f"Expected static release >= 103, got v={v_num}")

        for asset in (f"/static/styles.css?v={v_num}", f"/static/app.js?v={v_num}"):
            self.assertIn(asset, sw_txt, f"{asset} missing in sw.js SHELL_ASSETS")
            self.assertIn(asset, idx_txt, f"{asset} missing in index.html")

        # Verify UTF-8 without BOM across all modified/created files
        checked_files = (
            ROOT_DIR / "app" / "codex_engine.py",
            ROOT_DIR / "app" / "claude_engine.py",
            ROOT_DIR / "app" / "agy_engine.py",
            ROOT_DIR / "app" / "openrouter_service.py",
            ROOT_DIR / "app" / "main.py",
            ROOT_DIR / "app" / "static" / "app.js",
            ROOT_DIR / "app" / "static" / "sw.js",
            ROOT_DIR / "app" / "static" / "index.html",
            Path(__file__).resolve(),
        )
        for fp in checked_files:
            raw_b = fp.read_bytes()
            self.assertFalse(raw_b.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM detected in {fp}")
            raw_b.decode("utf-8")

        # Verify `main.workspace` CSS invariant remains untouched
        raw_css = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        clean_css = re.sub(r"/\*[\s\S]*?\*/", "", raw_css)
        forbidden_props = (
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain",
            "container-type",
        )
        for m in re.finditer(r"main\.workspace\s*\{([^}]*)\}", clean_css, flags=re.DOTALL):
            block = m.group(1)
            for prop in forbidden_props:
                self.assertIsNone(
                    re.search(rf"(?<![\w-]){re.escape(prop)}\s*:", block),
                    f"Forbidden CSS property `{prop}` found inside `main.workspace`",
                )


if __name__ == "__main__":
    unittest.main()
