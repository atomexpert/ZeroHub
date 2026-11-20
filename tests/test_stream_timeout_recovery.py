"""
Unit & ASGI API tests for Antigravity Hub stream timeout recovery & context indicators:
1. `is_transient_stream_error` & `looks_like_completed_response` heuristics.
2. Tail `stream idle timeout (5m0s): context deadline exceeded` after a completed report -> clean `done` (no false error).
3. Mid-turn `stream idle timeout (5m0s)` -> seamless `--conversation <id>` Auto-Continue with `<tail>` deduplication hint.
4. Start-of-turn `context deadline exceeded` before any progress -> automatic retry + stale empty `.db` cleanup.
5. Python Stream Watchdog (`proc.stdout.readline()` timeout) -> fast termination of hung post-response sockets without waiting 300s, plus mid-turn watchdog recovery.
6. `GET /api/chats` lightweight `context_pct` / `context_used_tokens` parity with `GET /api/chats/{chat_id}` + strict RBAC user isolation.
Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or `run_server.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _unlink_test_junctions(root: Path) -> None:
    if not root.exists():
        return
    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if hasattr(os.path, "isjunction") and os.path.isjunction(p):
                os.rmdir(p)
            elif p.is_symlink() and p.is_dir():
                os.rmdir(p)
        except Exception:
            pass


class _FakeStreamReader:
    """Mock asyncio StreamReader supporting `.readline()` and `.read()` with optional hang."""

    def __init__(self, lines: list[bytes], *, hang_at_end: bool = False) -> None:
        self._lines = list(lines)
        self._hang_at_end = hang_at_end
        self._closed_event = asyncio.Event()

    def close_on_kill(self) -> None:
        self._hang_at_end = False
        self._closed_event.set()

    async def readline(self) -> bytes:
        if self._lines:
            await asyncio.sleep(0)
            return self._lines.pop(0)
        if self._hang_at_end and not self._closed_event.is_set():
            await self._closed_event.wait()
        return b""

    async def read(self) -> bytes:
        if not self._lines:
            return b""
        out = b"".join(self._lines)
        self._lines.clear()
        return out


class _FakeCliProcess:
    """Mock asyncio.subprocess.Process for deterministic `stream_agy_turn` testing."""

    _next_pid = 41000

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        returncode: int = 0,
        stderr_bytes: bytes = b"",
        hang_after_events: bool = False,
    ) -> None:
        _FakeCliProcess._next_pid += 1
        self.pid = _FakeCliProcess._next_pid
        self._desired_rc = returncode
        self.returncode: int | None = None
        self.killed = False
        encoded_lines = [
            (json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8") for e in events
        ]
        self.stdout = _FakeStreamReader(encoded_lines, hang_at_end=hang_after_events)
        self.stderr = _FakeStreamReader([stderr_bytes] if stderr_bytes else [])

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.close_on_kill()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._desired_rc
        return self.returncode


class _MiniAsgiResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> dict:
        return json.loads(self.text)


class _MiniAsgiClient:
    def __init__(self, app, *, client: tuple[str, int] = ("127.0.0.1", 52700)) -> None:
        self._app = app
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> _MiniAsgiResponse:
        parsed_url = urllib.parse.urlsplit(path)
        raw_path = parsed_url.path or "/"
        qs = parsed_url.query
        if params:
            extra_qs = urllib.parse.urlencode(params)
            qs = f"{qs}&{extra_qs}" if qs else extra_qs

        payload = b""
        req_headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8080")]
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
            req_headers.append((b"content-length", str(len(payload)).encode("ascii")))
        if headers:
            for k, v in headers.items():
                req_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": urllib.parse.unquote(raw_path),
            "raw_path": raw_path.encode("ascii", errors="ignore"),
            "query_string": qs.encode("ascii", errors="ignore"),
            "headers": req_headers,
            "client": self._client,
            "server": ("127.0.0.1", 8080),
        }

        sent_body = False
        resp_status = 500
        resp_headers: dict[str, str] = {}
        resp_chunks: list[bytes] = []

        async def _receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def _send(message):
            nonlocal resp_status, resp_headers
            mtype = message.get("type")
            if mtype == "http.response.start":
                resp_status = int(message.get("status", 200))
                resp_headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in (message.get("headers") or [])
                }
            elif mtype == "http.response.body":
                chunk = message.get("body") or b""
                if chunk:
                    resp_chunks.append(chunk)

        await self._app(scope, _receive, _send)
        return _MiniAsgiResponse(resp_status, resp_headers, b"".join(resp_chunks))


class TestStreamTimeoutRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_test_stream_timeout_", ignore_cleanup_errors=True
        )
        self.tmp_root = Path(self._tmp.name).resolve()

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_agy": config.AGY_CLI_DATA_DIR,
            "eng_agy": agy_engine.AGY_CLI_DATA_DIR,
            "cfg_ws": config.USER_WORKSPACES_DIR,
            "db_ws": database.USER_WORKSPACES_DIR,
            "br_ws": brain_reader.USER_WORKSPACES_DIR,
            "cfg_sh": config.SHARED_PROJECTS_DIR,
            "db_sh": database.SHARED_PROJECTS_DIR,
            "br_sh": brain_reader.SHARED_PROJECTS_DIR,
            "cfg_up": config.UPLOADS_DIR,
            "eng_up": agy_engine.UPLOADS_DIR,
            "cfg_sbx": config.CHAT_SANDBOX_DIR,
            "eng_sbx": agy_engine.CHAT_SANDBOX_DIR,
            "t_active": agy_engine.STREAM_ACTIVE_TOOL_TIMEOUT_SEC,
            "t_post": agy_engine.STREAM_POST_TEXT_IDLE_TIMEOUT_SEC,
            "t_net": agy_engine.STREAM_NETWORK_IDLE_TIMEOUT_SEC,
            "t_cap_backoff": agy_engine.CAPACITY_RETRY_BACKOFF_SECS,
            "t_auto_pause": agy_engine.AUTO_CONTINUE_PAUSE_SEC,
        }

        tmp_db = self.tmp_root / "test_timeout.db"
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = self.tmp_root / "agy_cli_data"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (
            tmp_cli,
            tmp_agy / "conversations",
            tmp_ws,
            tmp_sh,
            tmp_up,
            tmp_sbx,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.fake_agy_bin = self.tmp_root / "fake_agy.exe"
        self.fake_agy_bin.write_bytes(b"MZ")

        config.DB_PATH = tmp_db
        database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_cli
        config.AGY_CLI_DATA_DIR = tmp_agy
        agy_engine.AGY_CLI_DATA_DIR = tmp_agy
        config.USER_WORKSPACES_DIR = tmp_ws
        database.USER_WORKSPACES_DIR = tmp_ws
        brain_reader.USER_WORKSPACES_DIR = tmp_ws
        config.SHARED_PROJECTS_DIR = tmp_sh
        database.SHARED_PROJECTS_DIR = tmp_sh
        brain_reader.SHARED_PROJECTS_DIR = tmp_sh
        config.UPLOADS_DIR = tmp_up
        agy_engine.UPLOADS_DIR = tmp_up
        config.CHAT_SANDBOX_DIR = tmp_sbx
        agy_engine.CHAT_SANDBOX_DIR = tmp_sbx

        # Fast sub-100ms timers in unit tests
        agy_engine.CAPACITY_RETRY_BACKOFF_SECS = (0.02, 0.03)
        agy_engine.AUTO_CONTINUE_PAUSE_SEC = 0.02

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.AGY_CLI_DATA_DIR = self._orig["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = self._orig["eng_agy"]
        config.USER_WORKSPACES_DIR = self._orig["cfg_ws"]
        database.USER_WORKSPACES_DIR = self._orig["db_ws"]
        brain_reader.USER_WORKSPACES_DIR = self._orig["br_ws"]
        config.SHARED_PROJECTS_DIR = self._orig["cfg_sh"]
        database.SHARED_PROJECTS_DIR = self._orig["db_sh"]
        brain_reader.SHARED_PROJECTS_DIR = self._orig["br_sh"]
        config.UPLOADS_DIR = self._orig["cfg_up"]
        agy_engine.UPLOADS_DIR = self._orig["eng_up"]
        config.CHAT_SANDBOX_DIR = self._orig["cfg_sbx"]
        agy_engine.CHAT_SANDBOX_DIR = self._orig["eng_sbx"]
        agy_engine.STREAM_ACTIVE_TOOL_TIMEOUT_SEC = self._orig["t_active"]
        agy_engine.STREAM_POST_TEXT_IDLE_TIMEOUT_SEC = self._orig["t_post"]
        agy_engine.STREAM_NETWORK_IDLE_TIMEOUT_SEC = self._orig["t_net"]
        agy_engine.CAPACITY_RETRY_BACKOFF_SECS = self._orig["t_cap_backoff"]
        agy_engine.AUTO_CONTINUE_PAUSE_SEC = self._orig["t_auto_pause"]

        _unlink_test_junctions(self.tmp_root)
        self._tmp.cleanup()

        if self._live_stat_before is not None and self._live_db.exists():
            stat_after = (
                self._live_db.stat().st_mtime_ns,
                self._live_db.stat().st_size,
            )
            self.assertEqual(
                self._live_stat_before,
                stat_after,
                "CRITICAL INVARIANT VIOLATION: live data/hub.db was modified during tests!",
            )

    def test_1_transient_stream_detector_and_completion_heuristics(self) -> None:
        from app.agy_engine import (
            is_transient_stream_error,
            looks_like_completed_response,
        )

        # Must match real Go / HTTP2 / proxy stream disconnects
        for sample in (
            "API error (attempt 1): request failed: stream idle timeout (5m0s): context deadline exceeded",
            "request failed: context deadline exceeded",
            "Post https://cloudcode-pa.googleapis.com/...: unexpected EOF",
            "read tcp 127.0.0.1:51234->127.0.0.1:10808: wsarecv: An existing connection was forcibly closed by the remote host",
            "read: connection reset by peer",
            "http2: client connection lost",
            "rpc error: code = Unavailable desc = transport is closing",
            "write: broken pipe",
            "The model stream was interrupted. Please continue the task.",
            "stream idle watchdog timeout",
        ):
            self.assertTrue(
                is_transient_stream_error(sample),
                f"Expected transient True for: {sample}",
            )

        # Must reject auth, permission, billing, and quota errors
        for non_transient in (
            "",
            "HTTP 401 Unauthorized: invalid token",
            "Error 403: permission denied for model",
            "Error 429: Resource has been exhausted (check quota)",
            "rate limit exceeded for requests per minute",
            "unauthenticated: please sign in",
            "billing account disabled",
        ):
            self.assertFalse(
                is_transient_stream_error(non_transient),
                f"Expected False for: {non_transient}",
            )

        completed_report = (
            "### Итоговый отчёт о выполнении задания\n"
            "- Все изменения внесены в файлы `app/agy_engine.py`, `app/static/app.js` и проверены автотестами (`0 ошибок`).\n"
            "- Для применения фронтенда достаточно нажать кнопку обновления ↻ в шапке (или Ctrl+F5).*"
        )
        self.assertTrue(looks_like_completed_response(completed_report))

        # Short text (< 180 chars) -> False
        self.assertFalse(looks_like_completed_response("Короткий ответ."))
        # Unclosed code fence -> False
        unclosed_code = completed_report + "\n```python\ndef broken():\n    return 1."
        self.assertFalse(looks_like_completed_response(unclosed_code))
        # Hanging colon / ellipsis / bold colon -> False
        self.assertFalse(looks_like_completed_response("А" * 200 + " Сейчас проверю:"))
        self.assertFalse(looks_like_completed_response("А" * 200 + " **Сейчас проверю:**"))
        self.assertFalse(looks_like_completed_response("А" * 200 + " Подождите немного..."))

    async def test_2_tail_stream_idle_timeout_after_full_report_finishes_cleanly(self) -> None:
        import app.agy_engine as agy_engine

        full_report = (
            "Релиз полностью реализован и проверен автотестами (`0 ошибок`). "
            "Все модули синхронизированы, кэш статики обновлён, живой сервер не перезапускался. "
            "Достаточно нажать кнопку обновления ↻ в шапке (или Ctrl+F5)."
        )
        self.assertGreaterEqual(len(full_report), 180)

        proc1 = _FakeCliProcess(
            [
                {"event": "init", "conversation_id": "conv-v69-tail"},
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-v69-tail",
                        "step_type": "tool",
                        "step_index": 1,
                        "state": "ACTIVE",
                        "tool_name": "edit_file",
                        "tool_info": {"parameters": {"Path": "app/static/app.js"}},
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-v69-tail",
                        "step_type": "tool",
                        "step_index": 1,
                        "state": "DONE",
                        "tool_name": "edit_file",
                        "tool_info": {"parameters": {"Path": "app/static/app.js"}},
                    },
                },
                # Streams complete final report WITHOUT state == "DONE"
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-v69-tail",
                        "step_type": "agent_response",
                        "step_index": 2,
                        "state": "ACTIVE",
                        "text_delta": full_report,
                    },
                },
                {
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "error": "API error (attempt 1): request failed: stream idle timeout (5m0s): context deadline exceeded",
                        "usage": {"input_tokens": 144000, "output_tokens": 1200},
                    },
                },
            ],
            returncode=1,
        )

        spawned_cmds: list[tuple[Any, ...]] = []

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            spawned_cmds.append(cmd)
            return proc1

        events: list[dict[str, Any]] = []
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(agy_engine, "extract_latest_turn_thinking", return_value=""),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            async for evt in agy_engine.stream_agy_turn(
                chat_id="chat-tail-1",
                kind="chat",
                project_name="",
                model="gemini-4-carbon-high",
                access_mode="read_only",
                prompt="Выполни ТЗ",
                attachments=[],
                agy_conversation_id="",
                history_messages=[{"id": 1, "role": "user", "content": "Выполни ТЗ", "status": "done"}],
            ):
                events.append(evt)

        self.assertEqual(len(spawned_cmds), 1, "Should NOT spawn a 2nd CLI process for a completed report")
        types = [e["type"] for e in events]
        self.assertNotIn("error", types)
        self.assertIn("done", types)
        done_evt = next(e for e in events if e["type"] == "done")
        self.assertEqual(done_evt["content"], full_report)
        self.assertEqual(done_evt["agy_conversation_id"], "conv-v69-tail")

    async def test_3_mid_turn_stream_idle_timeout_triggers_seamless_auto_continue(self) -> None:
        import app.agy_engine as agy_engine

        proc1 = _FakeCliProcess(
            [
                {"event": "init", "conversation_id": "conv-mid-77"},
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-mid-77",
                        "step_type": "tool",
                        "step_index": 1,
                        "state": "DONE",
                        "tool_name": "read_file",
                        "tool_info": {"parameters": {"Path": "app/main.py"}},
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-mid-77",
                        "step_type": "agent_response",
                        "step_index": 2,
                        "state": "ACTIVE",
                        "text_delta": "Сейчас проверю:",
                    },
                },
                {
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "error": "API error (attempt 1): request failed: stream idle timeout (5m0s): context deadline exceeded",
                    },
                },
            ],
            returncode=1,
        )
        proc2 = _FakeCliProcess(
            [
                {"event": "init", "conversation_id": "conv-mid-77"},
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-mid-77",
                        "step_type": "agent_response",
                        "step_index": 4,
                        "state": "DONE",
                        "text_delta": " все маршруты проверены и работают штатно.",
                    },
                },
                {
                    "event": "result",
                    "result": {
                        "status": "OK",
                        "usage": {"input_tokens": 22000, "output_tokens": 450},
                    },
                },
            ],
            returncode=0,
        )

        procs = [proc1, proc2]
        spawned_cmds: list[list[str]] = []

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            spawned_cmds.append([str(x) for x in cmd])
            return procs.pop(0)

        events: list[dict[str, Any]] = []
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(agy_engine, "extract_latest_turn_thinking", return_value=""),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            async for evt in agy_engine.stream_agy_turn(
                chat_id="chat-mid-2",
                kind="chat",
                project_name="",
                model="gemini-4-carbon-high",
                access_mode="read_only",
                prompt="Проверь маршруты",
                attachments=[],
                agy_conversation_id="",
                history_messages=[{"id": 1, "role": "user", "content": "Проверь маршруты", "status": "done"}],
            ):
                events.append(evt)

        self.assertEqual(len(spawned_cmds), 2)
        second_cmd = spawned_cmds[1]
        self.assertIn("--conversation", second_cmd)
        conv_arg_idx = second_cmd.index("--conversation") + 1
        self.assertEqual(second_cmd[conv_arg_idx], "conv-mid-77")
        prompt_arg = second_cmd[second_cmd.index("-p") + 1]
        self.assertIn("«...Сейчас проверю:...»", prompt_arg)

        types = [e["type"] for e in events]
        self.assertNotIn("error", types)
        done_evt = next(e for e in events if e["type"] == "done")
        self.assertEqual(
            done_evt["content"],
            "Сейчас проверю: все маршруты проверены и работают штатно.",
        )

    async def test_4_start_of_turn_deadline_exceeded_auto_retries_and_cleans_stale_db(self) -> None:
        import app.agy_engine as agy_engine

        stale_db = agy_engine.AGY_CLI_DATA_DIR / "conversations" / "conv-dead-start.db"
        stale_db.write_bytes(b"empty")

        proc1 = _FakeCliProcess(
            [
                {"event": "init", "conversation_id": "conv-dead-start"},
                {
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "error": "API error (attempt 1): request failed: context deadline exceeded",
                    },
                },
            ],
            returncode=1,
        )
        proc2 = _FakeCliProcess(
            [
                {"event": "init", "conversation_id": "conv-fresh-ok"},
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-fresh-ok",
                        "step_type": "agent_response",
                        "step_index": 1,
                        "state": "DONE",
                        "text_delta": "Запрос успешно выполнен со второй попытки.",
                    },
                },
                {
                    "event": "result",
                    "result": {"status": "OK", "usage": {"input_tokens": 1200, "output_tokens": 80}},
                },
            ],
            returncode=0,
        )

        procs = [proc1, proc2]
        spawned_cmds: list[list[str]] = []

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            spawned_cmds.append([str(x) for x in cmd])
            return procs.pop(0)

        events: list[dict[str, Any]] = []
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(agy_engine, "extract_latest_turn_thinking", return_value=""),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            async for evt in agy_engine.stream_agy_turn(
                chat_id="chat-start-retry",
                kind="chat",
                project_name="",
                model="gemini-4-carbon-high",
                access_mode="read_only",
                prompt="Привет",
                attachments=[],
                agy_conversation_id="",
                history_messages=[{"id": 1, "role": "user", "content": "Привет", "status": "done"}],
            ):
                events.append(evt)

        self.assertEqual(len(spawned_cmds), 2)
        self.assertFalse(stale_db.exists(), "Stale empty conversation .db must be cleaned up on start retry")
        self.assertNotIn("--conversation", spawned_cmds[1])
        done_evt = next(e for e in events if e["type"] == "done")
        self.assertEqual(done_evt["content"], "Запрос успешно выполнен со второй попытки.")
        self.assertEqual(done_evt["agy_conversation_id"], "conv-fresh-ok")

    async def test_5_python_stream_watchdog_closes_hung_post_text_socket_quickly(self) -> None:
        import app.agy_engine as agy_engine

        agy_engine.STREAM_POST_TEXT_IDLE_TIMEOUT_SEC = 0.06
        agy_engine.STREAM_NETWORK_IDLE_TIMEOUT_SEC = 0.50

        full_report = (
            "Полный итоговый отчёт по задаче успешно сформирован и передан клиенту. "
            "Все автотесты пройдены без единой ошибки, структура базы данных не затронута, "
            "зависимости проверены и полностью готовы к эксплуатации."
        )
        hung_proc = _FakeCliProcess(
            [
                {"event": "init", "conversation_id": "conv-watchdog-1"},
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-watchdog-1",
                        "step_type": "agent_response",
                        "step_index": 1,
                        "state": "ACTIVE",
                        "text_delta": full_report,
                    },
                },
                # Socket hangs forever here without `result` or `EOF`!
            ],
            returncode=0,
            hang_after_events=True,
        )

        terminated_pids: list[int] = []

        async def _fake_terminate(proc: Any) -> None:
            terminated_pids.append(proc.pid)
            proc.kill()

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            return hung_proc

        events: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(agy_engine, "extract_latest_turn_thinking", return_value=""),
            patch.object(agy_engine, "_terminate_hung_cli_proc", side_effect=_fake_terminate),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            async for evt in agy_engine.stream_agy_turn(
                chat_id="chat-watchdog-ok",
                kind="chat",
                project_name="",
                model="gemini-4-carbon-high",
                access_mode="read_only",
                prompt="Отчёт",
                attachments=[],
                agy_conversation_id="",
                history_messages=[{"id": 1, "role": "user", "content": "Отчёт", "status": "done"}],
            ):
                events.append(evt)
        elapsed = loop.time() - t0

        self.assertLess(elapsed, 1.0, "Watchdog must close hung socket rapidly without waiting 300s")
        self.assertEqual(terminated_pids, [hung_proc.pid])
        types = [e["type"] for e in events]
        self.assertNotIn("error", types)
        done_evt = next(e for e in events if e["type"] == "done")
        self.assertEqual(done_evt["content"], full_report)

    async def test_6_api_list_chats_context_pct_parity_and_rbac_isolation(self) -> None:
        import app.auth as auth
        import app.database as database
        import app.main as main

        await database.init_db()
        proj_dir = self.tmp_root / "workspaces" / "demo_proj"
        proj_dir.mkdir(parents=True, exist_ok=True)

        admin_user = await database.get_user_by_id("admin")
        assert admin_user is not None
        admin_token = auth.create_user_session_token(
            user_id="admin", session_version=admin_user["session_version"]
        )

        guest_user = await database.create_user(
            username="bob_dev",
            display_name="Bob",
            password="secret5678",
            role="user",
            max_access_mode="write_files",
        )
        guest_token = auth.create_user_session_token(
            user_id=guest_user["id"], session_version=guest_user["session_version"]
        )

        overloaded_chat = await database.create_chat(
            owner_id="admin",
            kind="project",
            project_name="DemoProj",
            project_path=str(proj_dir),
            title="Визуальное оформление",
            model="gemini-4-carbon-high",
            access_mode="full_access",
        )
        await database.update_chat(overloaded_chat["id"], agy_conversation_id="conv-144k")
        await database.add_message(
            overloaded_chat["id"],
            user_id="admin",
            role="user",
            content="Реализуй ТЗ v=69",
            status="done",
        )
        await database.add_message(
            overloaded_chat["id"],
            user_id="admin",
            role="assistant",
            content="Готово.",
            tool_calls=[],
            usage={
                "input_tokens": 24_000,
                "cache_read_tokens": 120_000,
                "output_tokens": 600,
                "thinking_tokens": 200,
            },
            status="done",
        )

        light_chat = await database.create_chat(
            owner_id="admin",
            kind="project",
            project_name="DemoProj",
            project_path=str(proj_dir),
            title="Чистая вкладка",
            model="gemini-4-carbon-high",
            access_mode="write_files",
        )
        await database.update_chat(light_chat["id"], agy_conversation_id="conv-34pct")
        await database.add_message(
            light_chat["id"],
            user_id="admin",
            role="user",
            content="Старт",
            status="done",
        )
        await database.add_message(
            light_chat["id"],
            user_id="admin",
            role="assistant",
            content="Ок.",
            tool_calls=[],
            usage={
                "input_tokens": 8_000,
                "cache_read_tokens": 60_000,
                "output_tokens": 200,
                "thinking_tokens": 100,
            },
            status="done",
        )

        client = _MiniAsgiClient(main.app)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        guest_headers = {"Authorization": f"Bearer {guest_token}"}

        list_resp = await client.request(
            "GET",
            "/api/chats",
            params={"kind": "project", "project_name": "DemoProj"},
            headers=admin_headers,
        )
        self.assertEqual(list_resp.status_code, 200)
        listed = {c["id"]: c for c in list_resp.json()["chats"]}
        self.assertIn(overloaded_chat["id"], listed)
        self.assertIn(light_chat["id"], listed)

        detail_over = await client.request(
            "GET", f"/api/chats/{overloaded_chat['id']}", headers=admin_headers
        )
        detail_light = await client.request(
            "GET", f"/api/chats/{light_chat['id']}", headers=admin_headers
        )
        self.assertEqual(detail_over.status_code, 200)
        self.assertEqual(detail_light.status_code, 200)

        # Exact parity between lightweight GET /api/chats and full GET /api/chats/{id}
        self.assertEqual(
            listed[overloaded_chat["id"]]["context_pct"],
            detail_over.json()["context_stats"]["pct"],
        )
        self.assertGreaterEqual(listed[overloaded_chat["id"]]["context_pct"], 72.0)
        self.assertEqual(
            listed[light_chat["id"]]["context_pct"],
            detail_light.json()["context_stats"]["pct"],
        )
        self.assertAlmostEqual(listed[light_chat["id"]]["context_pct"], 34.1, delta=1.0)

        # RBAC: non-member guest sees 0 chats of Admin's private DemoProj
        guest_resp = await client.request(
            "GET",
            "/api/chats",
            params={"kind": "project", "project_name": "DemoProj"},
            headers=guest_headers,
        )
        self.assertEqual(guest_resp.status_code, 200)
        self.assertEqual(guest_resp.json()["chats"], [])

    async def test_7_mid_turn_watchdog_and_pre_tool_intro_auto_continue(self) -> None:
        import app.agy_engine as agy_engine

        agy_engine.STREAM_POST_TEXT_IDLE_TIMEOUT_SEC = 0.04
        agy_engine.STREAM_NETWORK_IDLE_TIMEOUT_SEC = 0.06

        long_pre_tool_intro = (
            "Сейчас я внимательно прочитаю основные модули серверного ядра и проверю все маршруты API, "
            "чтобы убедиться в корректной обработке входящих параметров, изоляции ролей и отсутствии любых регрессий в коде."
        )
        self.assertTrue(agy_engine.looks_like_completed_response(long_pre_tool_intro))

        # proc1 emits a >=180-char intro ending with '.', THEN runs a tool (`DONE`),
        # and hangs on network BEFORE writing the post-tool answer!
        # Must NOT treat pre-tool intro as final answer — must trigger watchdog + Auto-Continue!
        proc1 = _FakeCliProcess(
            [
                {"event": "init", "conversation_id": "conv-pretool-99"},
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-pretool-99",
                        "step_type": "agent_response",
                        "step_index": 1,
                        "state": "DONE",
                        "text_delta": long_pre_tool_intro,
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-pretool-99",
                        "step_type": "tool",
                        "step_index": 2,
                        "state": "DONE",
                        "tool_name": "read_file",
                        "tool_info": {"parameters": {"Path": "app/agy_engine.py"}},
                    },
                },
            ],
            returncode=0,
            hang_after_events=True,
        )
        proc2 = _FakeCliProcess(
            [
                {"event": "init", "conversation_id": "conv-pretool-99"},
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": "conv-pretool-99",
                        "step_type": "agent_response",
                        "step_index": 4,
                        "state": "DONE",
                        "text_delta": "\n\nПроверка завершена: все обработчики работают штатно.",
                    },
                },
                {"event": "result", "result": {"status": "OK", "usage": {"input_tokens": 5000}}},
            ],
            returncode=0,
        )

        procs = [proc1, proc2]
        spawned_cmds: list[list[str]] = []

        async def _fake_terminate(proc: Any) -> None:
            proc.kill()

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            spawned_cmds.append([str(x) for x in cmd])
            return procs.pop(0)

        events: list[dict[str, Any]] = []
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(agy_engine, "extract_latest_turn_thinking", return_value=""),
            patch.object(agy_engine, "_terminate_hung_cli_proc", side_effect=_fake_terminate),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            async for evt in agy_engine.stream_agy_turn(
                chat_id="chat-pretool-watchdog",
                kind="chat",
                project_name="",
                model="gemini-4-carbon-high",
                access_mode="read_only",
                prompt="Проверь ядро",
                attachments=[],
                agy_conversation_id="",
                history_messages=[{"id": 1, "role": "user", "content": "Проверь ядро", "status": "done"}],
            ):
                events.append(evt)

        self.assertEqual(len(spawned_cmds), 2, "Must Auto-Continue after post-tool network hang")
        done_evt = next(e for e in events if e["type"] == "done")
        self.assertIn("Проверка завершена: все обработчики работают штатно.", done_evt["content"])


if __name__ == "__main__":
    unittest.main()
