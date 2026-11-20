"""
Isolated automated tests for Antigravity Hub v=79:
1. `strip_interrupt_banner_from_content` across all 6+ error/cancel/restart banner variants.
2. Adaptive Watchdog (`420s`) + `consecutive_auto_continues` reset on progress + stalled large-file hint
   + `initial_tool_calls` `ACTIVE -> INTERRUPTED` normalization & `step_index = 0` collision remapping.
3. Auto-pausing non-empty `CHAT_TURN_QUEUES` on `etype == "error"` + `usage["last_error"]` persistence.
4. In-Place Resume (`continue_turn` / `resume_message_id`): updates the exact same `messages.id` row,
   strips banner, stitches text + tool_calls, sums `usage` while preserving `code_handoffs`, records
   combined files in Project Memory changelog, unpauses & drains queued turns, and enforces RBAC.
Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or `run_server.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
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
    _next_pid = 52000

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


class TestContinueTurnAndWatchdog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database
        import app.main as main_mod

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(prefix="agy_hub_v79_test_")
        self.tmp_root = Path(self._tmp.name)

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_agy": config.AGY_CLI_DATA_DIR,
            "eng_agy": agy_engine.AGY_CLI_DATA_DIR,
            "cfg_proj": config.PROJECTS_ROOT,
            "db_proj": database.PROJECTS_ROOT,
            "br_proj": brain_reader.PROJECTS_ROOT,
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
            "t_startup": agy_engine.STREAM_STARTUP_IDLE_TIMEOUT_SEC,
            "t_active": agy_engine.STREAM_ACTIVE_TOOL_TIMEOUT_SEC,
            "t_post": agy_engine.STREAM_POST_TEXT_IDLE_TIMEOUT_SEC,
            "t_net": agy_engine.STREAM_NETWORK_IDLE_TIMEOUT_SEC,
            "t_cap_backoff": agy_engine.CAPACITY_RETRY_BACKOFF_SECS,
            "t_auto_pause": agy_engine.AUTO_CONTINUE_PAUSE_SEC,
        }

        tmp_db = self.tmp_root / "hub_test.db"
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = tmp_cli / "admin" / ".gemini" / "antigravity-cli"
        tmp_proj = self.tmp_root / "projects_root"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (
            tmp_agy / "conversations",
            tmp_proj,
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
        config.PROJECTS_ROOT = tmp_proj
        database.PROJECTS_ROOT = tmp_proj
        brain_reader.PROJECTS_ROOT = tmp_proj
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

        agy_engine.CAPACITY_RETRY_BACKOFF_SECS = (0.01, 0.02)
        agy_engine.AUTO_CONTINUE_PAUSE_SEC = 0.01

        main_mod.CHAT_TURN_QUEUES.clear()
        main_mod.CHAT_QUEUE_PAUSED.clear()
        main_mod.COMPACTING_CHATS.clear()
        agy_engine.ACTIVE_RUNS.clear()

        await database.init_db()

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database
        import app.main as main_mod

        main_mod.CHAT_TURN_QUEUES.clear()
        main_mod.CHAT_QUEUE_PAUSED.clear()
        main_mod.COMPACTING_CHATS.clear()
        agy_engine.ACTIVE_RUNS.clear()

        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.AGY_CLI_DATA_DIR = self._orig["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = self._orig["eng_agy"]
        config.PROJECTS_ROOT = self._orig["cfg_proj"]
        database.PROJECTS_ROOT = self._orig["db_proj"]
        brain_reader.PROJECTS_ROOT = self._orig["br_proj"]
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
        agy_engine.STREAM_STARTUP_IDLE_TIMEOUT_SEC = self._orig["t_startup"]
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
                "CRITICAL INVARIANT VIOLATION: live data/hub.db was touched!",
            )

    def test_1_strip_interrupt_banner_all_variants(self) -> None:
        from app.main import strip_interrupt_banner_from_content

        real_md = (
            "## Архитектура модуля\n\n"
            "Разделитель ниже в тексте сохраняется:\n\n---\n\n"
            "```python\ndef compute(x: int) -> int:\n    return x * 2\n```"
        )

        # 1. Standard error banner with real text + hint
        with_err = (
            f"{real_md}\n\n---\n⚠️ **Ошибка:** stream idle watchdog timeout\n\n"
            "💡 *Сетевой поток оборвался по таймауту даже после авто-продолжения.*"
        )
        self.assertEqual(strip_interrupt_banner_from_content(with_err), real_md)

        # 2. Standalone error banner (no text before error)
        standalone_err = (
            "⚠️ **Ошибка:** stream idle watchdog timeout\n\n"
            "💡 *Сетевой поток оборвался по таймауту.*"
        )
        self.assertEqual(strip_interrupt_banner_from_content(standalone_err), "")

        # 3. Internal server error with text & standalone
        with_srv_err = f"{real_md}\n\n---\n⚠️ **Внутренняя ошибка сервера:** `RuntimeError('boom')`"
        self.assertEqual(strip_interrupt_banner_from_content(with_srv_err), real_md)
        self.assertEqual(
            strip_interrupt_banner_from_content(
                "⚠️ **Внутренняя ошибка сервера:** `RuntimeError('boom')`"
            ),
            "",
        )

        # 4. Cancelled notes (trailing & standalone)
        self.assertEqual(
            strip_interrupt_banner_from_content(f"{real_md}\n\n*(Генерация остановлена)*"),
            real_md,
        )
        self.assertEqual(
            strip_interrupt_banner_from_content(
                f"{real_md}\n\n*(Генерация остановлена пользователем)*"
            ),
            real_md,
        )
        self.assertEqual(
            strip_interrupt_banner_from_content("*(Генерация остановлена пользователем)*"),
            "",
        )
        self.assertEqual(
            strip_interrupt_banner_from_content("*(Генерация остановлена)*"),
            "",
        )

        # 5. Server restart stale streaming notes (trailing & standalone)
        self.assertEqual(
            strip_interrupt_banner_from_content(
                f"{real_md}\n\n*(Генерация прервана при перезапуске сервера)*"
            ),
            real_md,
        )
        self.assertEqual(
            strip_interrupt_banner_from_content(
                "⚠️ *Генерация ответа была прервана при перезапуске сервера.*"
            ),
            "",
        )

        # 6. Edge case: model text legitimately starts with "⚠️ **Ошибка:**" before a real crash tail
        tricky_body = "⚠️ **Ошибка:** в вашей функции отсутствует `await`."
        self.assertEqual(
            strip_interrupt_banner_from_content(
                f"{tricky_body}\n\n---\n⚠️ **Ошибка:** stream idle watchdog timeout"
            ),
            tricky_body,
        )

    async def test_2_watchdog_consecutive_reset_stalled_hint_and_step_index_remap(self) -> None:
        import app.agy_engine as agy_engine

        # Verify split watchdog constants & auto-continue caps
        self.assertEqual(self._orig["t_startup"], 65.0)
        self.assertEqual(self._orig["t_net"], 165.0)
        self.assertEqual(self._orig["t_active"], 600.0)
        self.assertEqual(self._orig["t_post"], 65.0)
        self.assertEqual(agy_engine.MAX_CONSECUTIVE_AUTO_CONTINUES, 3)
        self.assertEqual(agy_engine.MAX_TOTAL_AUTO_CONTINUES, 10)

        # Verify stalled_without_progress large-file decomposition hint
        normal_prompt = agy_engine._build_auto_continue_prompt(
            "Часть текста", stalled_without_progress=False
        )
        stalled_prompt = agy_engine._build_auto_continue_prompt(
            "Часть текста", stalled_without_progress=True
        )
        self.assertNotIn("write_file(Append=True)", normal_prompt)
        self.assertIn(">350 lines", stalled_prompt)
        self.assertIn("edit_file", stalled_prompt)
        self.assertIn("write_file(Append=True)", stalled_prompt)

        # Speed up network idle timeout for deterministic watchdog trigger
        agy_engine.STREAM_STARTUP_IDLE_TIMEOUT_SEC = 0.04
        agy_engine.STREAM_NETWORK_IDLE_TIMEOUT_SEC = 0.04
        agy_engine.STREAM_POST_TEXT_IDLE_TIMEOUT_SEC = 0.25

        conv_id = "11111111-2222-3333-4444-555555555555"
        (agy_engine.AGY_CLI_DATA_DIR / "conversations" / f"{conv_id}.db").write_bytes(b"SQLite")

        # Seed 2 initial tool steps (step 0 DONE, step 1 ACTIVE -> must become INTERRUPTED)
        seeded_tools = [
            {
                "step_index": 0,
                "state": "DONE",
                "name": "read_file",
                "parameters": {"Path": "a.py"},
                "output": "ok",
            },
            {
                "step_index": 1,
                "state": "ACTIVE",
                "name": "write_file",
                "parameters": {"Path": "big.py"},
                "output": "",
            },
        ]

        # 4 subprocess invocations: runs 1..3 each make progress (tool DONE or text_delta)
        # and then hang -> watchdog kills -> consecutive_auto_continues resets to 0 every time!
        # Also each new subprocess restarts its own step_index at 0 -> must remap without overwriting!
        spawned_cmds: list[list[str]] = []
        runs = [
            _FakeCliProcess(
                [
                    {"event": "init", "conversation_id": conv_id},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 0,
                            "step_type": "tool",
                            "state": "DONE",
                            "tool_name": "edit_file",
                            "tool_info": {"parameters": {"Path": "part1.py"}},
                        },
                    },
                ],
                hang_after_events=True,
            ),
            _FakeCliProcess(
                [
                    {"event": "init", "conversation_id": conv_id},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 0,
                            "step_type": "tool",
                            "state": "DONE",
                            "tool_name": "edit_file",
                            "tool_info": {"parameters": {"Path": "part2.py"}},
                        },
                    },
                ],
                hang_after_events=True,
            ),
            _FakeCliProcess(
                [
                    {"event": "init", "conversation_id": conv_id},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 0,
                            "step_type": "agent_response",
                            "state": "ACTIVE",
                            "text_delta": "Продолжение расчёта.",
                        },
                    },
                ],
                hang_after_events=True,
            ),
            _FakeCliProcess(
                [
                    {"event": "init", "conversation_id": conv_id},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 0,
                            "step_type": "agent_response",
                            "state": "DONE",
                            "text_delta": " Финал успешно готов.",
                        },
                    },
                    {
                        "event": "result",
                        "result": {
                            "status": "OK",
                            "usage": {"input_tokens": 900, "output_tokens": 180, "total_tokens": 1080},
                        },
                    },
                ],
                returncode=0,
            ),
        ]

        async def _fake_exec(*cmd: str, **_kwargs: Any) -> _FakeCliProcess:
            spawned_cmds.append(list(cmd))
            return runs.pop(0)

        async def _fake_kill(p: Any) -> None:
            p.kill()

        collected: list[dict[str, Any]] = []
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(agy_engine, "extract_latest_turn_thinking", return_value=""),
            patch.object(agy_engine, "_terminate_hung_cli_proc", side_effect=_fake_kill),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        ):
            async for ev in agy_engine.stream_agy_turn(
                chat_id="chat-wd-test",
                kind="chat",
                project_name="",
                model="claude-opus-4-6-thinking",
                access_mode="read_only",
                prompt=agy_engine._build_continue_turn_prompt("Начало ответа"),
                attachments=[],
                agy_conversation_id=conv_id,
                history_messages=[{"id": 1, "role": "user", "content": "Сделай рефакторинг", "status": "done"}],
                initial_content="Начало ответа",
                initial_thinking="Первичные мысли",
                initial_tool_calls=seeded_tools,
                is_continue_turn=True,
            ):
                collected.append(ev)

        self.assertEqual(len(spawned_cmds), 4, "Should survive 3 watchdog restarts with progress and finish on #4")
        done_evts = [e for e in collected if e.get("type") == "done"]
        self.assertEqual(len(done_evts), 1)
        done = done_evts[0]
        self.assertEqual(done["content"], "Начало ответа Продолжение расчёта. Финал успешно готов.")

        tcs = done["tool_calls"]
        self.assertEqual(len(tcs), 4, "2 seeded tools + 2 new tools with colliding step_index=0 must all be preserved")
        self.assertEqual(tcs[0]["step_index"], 0)
        self.assertEqual(tcs[0]["name"], "read_file")
        self.assertEqual(tcs[1]["step_index"], 1)
        self.assertEqual(tcs[1]["state"], "INTERRUPTED")
        self.assertEqual(tcs[2]["step_index"], 2)
        self.assertEqual(tcs[2]["parameters"]["Path"], "part1.py")
        self.assertEqual(tcs[3]["step_index"], 3)
        self.assertEqual(tcs[3]["parameters"]["Path"], "part2.py")

    async def test_3_error_pauses_queue_and_inplace_resume_stitches_same_row_and_usage(self) -> None:
        import app.database as database
        import app.main as main_mod

        admin = await database.get_user_by_id("admin")
        assert admin is not None

        proj_dir = database.PROJECTS_ROOT / "DemoProj"
        proj_dir.mkdir(parents=True, exist_ok=True)

        chat = await database.create_chat(
            owner_id="admin",
            kind="project",
            project_name="DemoProj",
            project_path=str(proj_dir),
            title="Интеграция In-Place Resume",
            model="claude-opus-4-6-thinking",
            access_mode="write_files",
        )
        cid = chat["id"]

        # Enqueue a follow-up item while turn #1 will run and fail
        main_mod.CHAT_TURN_QUEUES[cid] = [
            {
                "id": "q_follow_1",
                "chat_id": cid,
                "kind": "message",
                "prompt": "Второй шаг из очереди после продолжения",
                "attachments": [],
                "model": "claude-opus-4-6-thinking",
                "access_mode": "write_files",
                "user_id": "admin",
                "author_name": "Администратор",
                "created_at": "2026-11-12T10:00:00Z",
            }
        ]

        file_a = str(proj_dir / "part_a.py")
        file_b = str(proj_dir / "part_b.py")

        async def _stream_fail_first(**_kwargs: Any):
            yield {"type": "agy_session", "agy_conversation_id": "conv-demo-1"}
            yield {"type": "thinking_update", "thinking": "Мысль части 1", "seq": 1}
            yield {
                "type": "tool_step",
                "step": {
                    "step_index": 0,
                    "state": "DONE",
                    "name": "write_file",
                    "parameters": {"Path": file_a},
                    "output": "written",
                },
                "seq": 2,
            }
            yield {"type": "text_delta", "delta": "Первая половина ответа.", "seq": 3}
            yield {
                "type": "error",
                "error": "stream idle watchdog timeout",
                "content": "Первая половина ответа.",
                "thinking": "Мысль части 1",
                "tool_calls": [
                    {
                        "step_index": 0,
                        "state": "DONE",
                        "name": "write_file",
                        "parameters": {"Path": file_a},
                        "output": "written",
                    },
                    {
                        "step_index": 1,
                        "state": "ACTIVE",
                        "name": "edit_file",
                        "parameters": {"Path": file_b},
                        "output": "",
                    },
                ],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "thought_tokens": 50,
                    "cached_tokens": 400,
                    "total_tokens": 1250,
                    "elapsed_ms": 3100,
                },
            }

        async def _noop_quotas(_uid: str) -> None:
            return None

        with (
            patch.object(main_mod, "stream_agy_turn", side_effect=_stream_fail_first),
            patch.object(main_mod, "_refresh_and_broadcast_quotas", side_effect=_noop_quotas),
            patch.object(
                main_mod,
                "check_docker_status",
                return_value={"installed": False, "sandbox_mcp_ready": False},
            ),
            patch.object(
                main_mod,
                "fetch_available_models",
                return_value=[{"id": "claude-opus-4-6-thinking", "label": "Opus 4.6"}],
            ),
        ):
            await main_mod._run_turn_task(
                chat_id=cid,
                prompt="Собери оба модуля проекта DemoProj",
                attachments=[],
                override_model="claude-opus-4-6-thinking",
                override_access_mode="write_files",
                trigger_user=admin,
            )

        # 1. Queue MUST be auto-paused and NOT drained on top of the failed turn!
        self.assertIn(cid, main_mod.CHAT_QUEUE_PAUSED)
        self.assertEqual(len(main_mod.CHAT_TURN_QUEUES.get(cid) or []), 1)

        msgs_after_err = await database.get_messages(cid, include_thinking=True)
        self.assertEqual(len(msgs_after_err), 2)
        err_msg = msgs_after_err[-1]
        err_msg_id = int(err_msg["id"])
        self.assertEqual(err_msg["status"], "error")
        self.assertIn("⚠️ **Ошибка:** stream idle watchdog timeout", err_msg["content"])
        self.assertEqual(err_msg["usage"].get("last_error"), "stream idle watchdog timeout")
        self.assertEqual(err_msg["tool_calls"][1]["state"], "INTERRUPTED")

        # Attach a code_handoffs receipt to verify In-Place Resume preserves it
        await database.append_message_code_handoff(
            cid,
            err_msg_id,
            {
                "target_chat_id": "handoff-target-99",
                "target_title": "Рабочая вкладка",
                "access_mode": "write_files",
                "project_name": "DemoProj",
            },
        )

        # Clear queue temporarily so we inspect exact message count right after In-Place Resume
        saved_queue_item = main_mod.CHAT_TURN_QUEUES.pop(cid)[0]

        captured_resume_kwargs: dict[str, Any] = {}

        async def _stream_resume_ok(**kwargs: Any):
            captured_resume_kwargs.update(kwargs)
            seeded_txt = kwargs["initial_content"]
            seeded_tools = list(kwargs["initial_tool_calls"])
            stitched = seeded_txt + " Вторая половина завершена."
            new_step = {
                "step_index": 2,
                "state": "DONE",
                "name": "edit_file",
                "parameters": {"Path": file_b},
                "output": "patched",
            }
            yield {"type": "text_delta", "delta": " Вторая половина завершена.", "seq": 1}
            yield {
                "type": "done",
                "content": stitched,
                "thinking": kwargs["initial_thinking"] + "\n\n---\n\nМысль части 2",
                "tool_calls": seeded_tools + [new_step],
                "usage": {
                    "input_tokens": 600,
                    "output_tokens": 150,
                    "thought_tokens": 30,
                    "cached_tokens": 250,
                    "total_tokens": 780,
                    "elapsed_ms": 1900,
                },
            }

        # Simulate WebSocket `continue_turn` unpausing queue & launching `_run_turn_task(resume_message_id=...)`
        main_mod.CHAT_QUEUE_PAUSED.discard(cid)
        orig_cl_builder = main_mod.build_auto_changelog_entry_from_turn
        captured_cl_prompts: list[str] = []

        def _spy_cl_builder(**cl_kw: Any) -> Any:
            captured_cl_prompts.append(str(cl_kw.get("user_prompt") or ""))
            return orig_cl_builder(**cl_kw)

        with (
            patch.object(main_mod, "stream_agy_turn", side_effect=_stream_resume_ok),
            patch.object(main_mod, "_refresh_and_broadcast_quotas", side_effect=_noop_quotas),
            patch.object(
                main_mod,
                "check_docker_status",
                return_value={"installed": False, "sandbox_mcp_ready": False},
            ),
            patch.object(
                main_mod,
                "build_auto_changelog_entry_from_turn",
                side_effect=_spy_cl_builder,
            ),
            patch.object(
                main_mod,
                "fetch_available_models",
                return_value=[{"id": "claude-opus-4-6-thinking", "label": "Opus 4.6"}],
            ),
        ):
            await main_mod._run_turn_task(
                chat_id=cid,
                prompt="Собери оба модуля проекта DemoProj",
                attachments=[],
                override_model="claude-opus-4-6-thinking",
                override_access_mode="write_files",
                trigger_user=admin,
                resume_message_id=err_msg_id,
            )

        self.assertTrue(captured_resume_kwargs.get("is_continue_turn"))
        self.assertEqual(captured_resume_kwargs.get("initial_content"), "Первая половина ответа.")
        self.assertNotIn(cid, main_mod.CHAT_QUEUE_PAUSED)

        msgs_after_resume = await database.get_messages(cid, include_thinking=True)
        self.assertEqual(
            len(msgs_after_resume),
            2,
            "In-Place Resume MUST NOT create duplicate user or assistant rows!",
        )
        resumed = msgs_after_resume[-1]
        self.assertEqual(int(resumed["id"]), err_msg_id)
        self.assertEqual(resumed["status"], "done")
        self.assertEqual(
            resumed["content"],
            "Первая половина ответа. Вторая половина завершена.",
        )
        self.assertNotIn("⚠️ **Ошибка:**", resumed["content"])
        self.assertEqual(len(resumed["tool_calls"]), 3)

        usage = resumed["usage"]
        self.assertNotIn("last_error", usage)
        self.assertEqual(usage["input_tokens"], 1600)
        self.assertEqual(usage["output_tokens"], 350)
        self.assertEqual(usage["thought_tokens"], 80)
        self.assertEqual(usage["cached_tokens"], 650)
        self.assertEqual(usage["total_tokens"], 2030)
        self.assertEqual(usage["elapsed_ms"], 5000)
        self.assertEqual(len(usage.get("code_handoffs") or []), 1)
        self.assertEqual(usage["code_handoffs"][0]["target_chat_id"], "handoff-target-99")

        # Verify Project Memory auto-changelog recorded BOTH files from part 1 + part 2!
        scope_key = database.build_project_memory_scope_key("DemoProj", "admin", is_shared=False)
        mem = await database.get_project_memory(scope_key, project_name="DemoProj", owner_id="admin")
        cl = mem.get("changelog") or []
        self.assertEqual(len(cl), 1)
        self.assertIn("part_a.py", cl[0].get("files") or [])
        self.assertIn("part_b.py", cl[0].get("files") or [])
        self.assertEqual(captured_cl_prompts, ["Собери оба модуля проекта DemoProj"])
        self.assertEqual(
            cl[0].get("summary"),
            "Первая половина ответа. Вторая половина завершена.",
        )

        # Also verify WebSocket `continue_turn` RBAC & queue drain end-to-end
        guest = await database.create_user(
            username="dev_guest",
            password="guest_password_123",
            display_name="Guest Dev",
            max_access_mode="write_files",
        )
        await database.update_message(err_msg_id, status="cancelled", content="Часть до стопа\n\n*(Генерация остановлена)*")
        main_mod.CHAT_TURN_QUEUES[cid] = [saved_queue_item]
        main_mod.CHAT_QUEUE_PAUSED.add(cid)

        class _DummyWS:
            def __init__(self, payloads: list[dict[str, Any]]) -> None:
                self._payloads = list(payloads)
                self.sent: list[dict[str, Any]] = []
                self.accepted = False

            async def accept(self) -> None:
                self.accepted = True

            async def receive_json(self) -> dict[str, Any]:
                if self._payloads:
                    return self._payloads.pop(0)
                raise main_mod.WebSocketDisconnect()

            async def send_json(self, data: dict[str, Any]) -> None:
                self.sent.append(data)

            async def close(self, code: int = 1000) -> None:
                pass

        # Guest MUST be denied access to admin's private project chat
        guest_ws = _DummyWS([{"action": "continue_turn", "chat_id": cid, "message_id": err_msg_id}])
        with patch.object(main_mod.auth, "authenticate_ws_user", return_value=guest):
            await main_mod.websocket_endpoint(guest_ws)  # type: ignore[arg-type]
        self.assertTrue(
            any(
                m.get("type") == "ws_error" and "запрещён" in str(m.get("error") or "")
                for m in guest_ws.sent
            )
        )

        # Admin WebSocket `continue_turn` unpauses queue, resumes `err_msg_id`, AND drains queued item!
        ws_calls = 0

        async def _stream_ws_resume_and_queue(**kwargs: Any):
            nonlocal ws_calls
            ws_calls += 1
            if ws_calls == 1:
                yield {
                    "type": "done",
                    "content": kwargs["initial_content"] + " и дописано.",
                    "thinking": "",
                    "tool_calls": [],
                    "usage": {"total_tokens": 100},
                }
            else:
                yield {
                    "type": "done",
                    "content": "Ответ на второй шаг из очереди.",
                    "thinking": "",
                    "tool_calls": [],
                    "usage": {"total_tokens": 90},
                }

        async def _noop_quotas(_uid: str) -> None:
            return None

        admin_ws = _DummyWS([{"action": "continue_turn", "chat_id": cid, "message_id": err_msg_id}])
        with (
            patch.object(main_mod.auth, "authenticate_ws_user", return_value=admin),
            patch.object(main_mod, "stream_agy_turn", side_effect=_stream_ws_resume_and_queue),
            patch.object(main_mod, "_refresh_and_broadcast_quotas", side_effect=_noop_quotas),
            patch.object(
                main_mod,
                "check_docker_status",
                return_value={"installed": False, "sandbox_mcp_ready": False},
            ),
            patch.object(
                main_mod,
                "fetch_available_models",
                return_value=[{"id": "claude-opus-4-6-thinking", "label": "Opus 4.6"}],
            ),
        ):
            await main_mod.websocket_endpoint(admin_ws)  # type: ignore[arg-type]
            await asyncio.sleep(0)
            task = main_mod.ACTIVE_TURN_TASKS.get(cid)
            if task is not None:
                await asyncio.wait_for(task, timeout=5.0)

        self.assertNotIn(cid, main_mod.CHAT_QUEUE_PAUSED)
        self.assertFalse(main_mod.CHAT_TURN_QUEUES.get(cid))
        final_msgs = await database.get_messages(cid, include_thinking=False)
        # 2 original messages (resumed in-place!) + 2 messages from the queued follow-up turn = 4 total
        self.assertEqual(len(final_msgs), 4)
        self.assertEqual(final_msgs[1]["id"], err_msg_id)
        self.assertEqual(final_msgs[1]["content"], "Часть до стопа и дописано.")
        self.assertEqual(final_msgs[3]["content"], "Ответ на второй шаг из очереди.")

    def test_4_frontend_v80_anchors_and_pwa_cache(self) -> None:
        index_html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        sw_js = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        app_js = (ROOT_DIR / "app" / "static" / "app.js").read_text(encoding="utf-8")
        styles_css = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")

        import re

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_sw)
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 80)
        self.assertIn(f'/static/styles.css?v={ver}"', index_html)
        self.assertIn(f'/static/app.js?v={ver}"', index_html)
        self.assertIn(f'"/static/styles.css?v={ver}"', sw_js)
        self.assertIn(f'"/static/app.js?v={ver}"', sw_js)

        for anchor in (
            "function stripInterruptBannerFromContent(",
            "function isResumableAssistantMessage(",
            "function continueInterruptedTurn(",
            'action: "continue_turn"',
            'data-continue-msg=',
            '".msg-continue-bar"',
            "Продолжить с места остановки",
        ):
            self.assertIn(anchor, app_js)

        for css_sel in (".msg-continue-bar", ".msg-continue-btn", ".msg-continue-hint"):
            self.assertIn(css_sel, styles_css)

    async def test_5_bg_task_blocks_false_post_text_idle_and_filters_changelog(self) -> None:
        import app.agy_engine as agy_engine
        import app.database as database

        interim_two_lines = (
            "Запустил первый прогон автотестов визуального обновления и регрессионные проверки — дожидаюсь завершения прогона.\n"
            "Запущен повторный прогон изолированных автотестов pytest — ожидаю завершения выполнения задачи."
        )
        self.assertGreater(len(interim_two_lines), 180)
        self.assertTrue(interim_two_lines.endswith("."))
        # Interim wait line MUST NOT be mistaken for a completed final report!
        self.assertFalse(agy_engine.looks_like_completed_response(interim_two_lines))

        final_line = "Все 12 изолированных автотестов успешно пройдены, регрессий не обнаружено."

        class _DelayedBgStreamReader:
            def __init__(self, pre_events: list[dict[str, Any]], delay_sec: float, post_events: list[dict[str, Any]]) -> None:
                self._items: list[tuple[float, bytes]] = [
                    (0.0, (json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8"))
                    for e in pre_events
                ] + [
                    (
                        delay_sec if i == 0 else 0.0,
                        (json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8"),
                    )
                    for i, e in enumerate(post_events)
                ]
                self._closed = False

            def close_on_kill(self) -> None:
                self._closed = True

            async def readline(self) -> bytes:
                if self._closed or not self._items:
                    return b""
                wait_s, payload = self._items.pop(0)
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                if self._closed:
                    return b""
                return payload

        pre_events = [
            {"event": "init", "conversation_id": "conv-bg-pytest-01"},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 1,
                    "tool_name": "run_command",
                    "state": "DONE",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "pytest -q"},
                        "output": "Tool is running as a background task (task ID: task-1).",
                    },
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 2,
                    "state": "ACTIVE",
                    "text_delta": interim_two_lines,
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 2,
                    "state": "DONE",
                },
            },
        ]
        post_events = [
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 4,
                    "state": "ACTIVE",
                    "text_delta": final_line,
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 4,
                    "state": "DONE",
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "DONE",
                    "usage": {"input_tokens": 210, "output_tokens": 65},
                },
            },
        ]

        fake_proc = _FakeCliProcess([], returncode=0)
        # Background pause (0.16s) is >3x longer than STREAM_POST_TEXT_IDLE_TIMEOUT_SEC (0.04s)
        fake_proc.stdout = _DelayedBgStreamReader(pre_events, 0.16, post_events)  # type: ignore[assignment]

        chat = await database.create_chat(
            title="Проверка фонового pytest",
            kind="chat",
            access_mode="full",
            model="claude-opus-4-6-thinking",
            owner_id="admin",
        )

        streamed_events: list[dict[str, Any]] = []

        async def _fake_spawn(*args: Any, **kwargs: Any) -> _FakeCliProcess:
            return fake_proc

        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(agy_engine, "STREAM_POST_TEXT_IDLE_TIMEOUT_SEC", 0.04),
            patch.object(agy_engine, "STREAM_ACTIVE_TOOL_TIMEOUT_SEC", 2.0),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_spawn),
        ):
            async for ev in agy_engine.stream_agy_turn(
                chat_id=chat["id"],
                prompt="Прогони pytest",
                attachments=[],
                kind="chat",
                project_name="",
                access_mode="full",
                model="claude-opus-4-6-thinking",
                agy_conversation_id="",
                history_messages=[],
            ):
                streamed_events.append(ev)

        self.assertFalse(fake_proc.killed, "Background task wait must NOT trigger post_text_idle kill!")
        done_ev = next((e for e in streamed_events if e.get("type") == "done"), None)
        self.assertIsNotNone(done_ev, streamed_events)
        assert done_ev is not None
        full_txt = done_ev["content"]
        self.assertIn(interim_two_lines + "\n\n" + final_line, full_txt)
        self.assertTrue(agy_engine.looks_like_completed_response(full_txt))

        # Auto-changelog must ignore interim wait lines and capture the real engineering thesis!
        proj_dir = self.tmp_root / "proj_changelog_check"
        proj_dir.mkdir(parents=True, exist_ok=True)
        changelog_entry = agy_engine.build_auto_changelog_entry_from_turn(
            chat_id=chat["id"],
            chat_title="Проверка фонового pytest",
            access_mode="full_access",
            user_prompt="Прогони pytest",
            assistant_text=full_txt,
            tool_calls=[
                {
                    "name": "edit_file",
                    "state": "DONE",
                    "parameters": {"Path": str(proj_dir / "app" / "service.py")},
                }
            ],
            proj_dir=proj_dir,
        )
        self.assertIsNotNone(changelog_entry)
        assert changelog_entry is not None
        self.assertEqual(changelog_entry["summary"], final_line)


if __name__ == "__main__":
    unittest.main()
