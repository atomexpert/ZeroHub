# -*- coding: utf-8 -*-
"""Isolated regression suite for v=157:
1. Silent Empty-Turn Auto-Resume: 4x `read_file` + thinking -> `rc=0`, `result.response=""`
   automatically resumes `--conversation <cid>` with `_build_empty_turn_resume_prompt`,
   executes `call_mcp_tool(hub_openrouter)` and yields a single `done` event with full text
   and all 5 `tool_calls` (no model downgrade).
2. Exhausted `MAX_EMPTY_TURN_RESUMES = 2` with empty text and no real file writes emits
   a clear Russian `type="error"` event (with preserved thinking, tool_calls, and
   `agy_conversation_id`) instead of a silent empty `type="done"`.
3. Empty text WITH real file writes (`write_file` / `edit_file`) still completes as `done`
   after exhausting resumes, while failed/denied `write_file` does not count as a write.
4. Non-OK `result.status` (`FAILED`, `ABORTED`, `CANCELLED`, `INTERRUPTED`) without text
   sets `result_error` and is never masked as `rc = 0`.
5. `run_workflow` & `run_workflow(*)` are denied in `HEADLESS_ASYNC_DENY_PATTERNS` across
   all contours (including `kind == "chat"`) and caught by `_update_bg_task_state_on_tool`
   and `is_parked_async_turn`.
6. Disk-only `transcript.jsonl` thinking (without streamed `thinking_delta` or tools)
   still triggers Silent Empty-Turn Auto-Resume.

Strict invariants:
- `0% GPU / 0 MB VRAM` (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`).
- `0` network calls, `0` touches to live `data/hub.db` or `users/`.
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
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_ALL_GPU"] = "1"

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
        except OSError:
            pass


def _make_fake_proc(
    events: list[dict[str, Any]],
    *,
    returncode: int = 0,
    stderr_text: str = "",
) -> MagicMock:
    proc = MagicMock(returncode=returncode)
    proc.stdout = asyncio.StreamReader()
    for ev in events:
        proc.stdout.feed_data((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdout.feed_eof()
    proc.stderr = asyncio.StreamReader()
    if stderr_text:
        proc.stderr.feed_data(stderr_text.encode("utf-8"))
    proc.stderr.feed_eof()
    proc.wait = AsyncMock(return_value=returncode)
    proc.aclose_lease = AsyncMock()
    return proc


class SilentEmptyTurnRecoveryV157Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        self.agy_engine = agy_engine
        self.config = config
        self.database = database

        self._live_db = (ROOT_DIR / "data" / "hub.db").resolve()
        self._live_db_before = (
            (self._live_db.stat().st_size, self._live_db.stat().st_mtime_ns)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_v157_silent_empty_turn_", ignore_cleanup_errors=True
        )
        self.tmp_root = Path(self._tmp.name).resolve()

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_users": config.USERS_DIR,
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
            "t_auto_pause": agy_engine.AUTO_CONTINUE_PAUSE_SEC,
        }

        tmp_db = (self.tmp_root / "hub_v157_test.db").resolve()
        self.tmp_users = self.tmp_root / "users"
        self.tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = self.tmp_cli / "admin" / ".gemini" / "antigravity-cli"
        tmp_proj = self.tmp_root / "projects_root"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (
            self.tmp_users,
            tmp_agy / "conversations",
            tmp_agy / "brain",
            tmp_proj / "demo_proj",
            tmp_ws,
            tmp_sh,
            tmp_up,
            tmp_sbx,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.proj_dir = tmp_proj / "demo_proj"
        self.fake_agy_bin = self.tmp_root / "fake_agy_v157.exe"
        self.fake_agy_bin.write_bytes(b"MZ")

        await database.close_db_pool()
        config.DB_PATH = database.DB_PATH = tmp_db
        config.USERS_DIR = self.tmp_users
        config.CLI_HOMES_DIR = self.tmp_cli
        config.AGY_CLI_DATA_DIR = agy_engine.AGY_CLI_DATA_DIR = tmp_agy
        config.PROJECTS_ROOT = database.PROJECTS_ROOT = brain_reader.PROJECTS_ROOT = tmp_proj
        config.USER_WORKSPACES_DIR = database.USER_WORKSPACES_DIR = brain_reader.USER_WORKSPACES_DIR = tmp_ws
        config.SHARED_PROJECTS_DIR = database.SHARED_PROJECTS_DIR = brain_reader.SHARED_PROJECTS_DIR = tmp_sh
        config.UPLOADS_DIR = agy_engine.UPLOADS_DIR = tmp_up
        config.CHAT_SANDBOX_DIR = agy_engine.CHAT_SANDBOX_DIR = tmp_sbx
        agy_engine.AUTO_CONTINUE_PAUSE_SEC = 0.01
        agy_engine.ACTIVE_RUNS.clear()

        self._users_dir_patch = patch.object(
            config, "get_effective_users_dir", return_value=self.tmp_users
        )
        self._users_dir_patch.start()

        await database.init_db()
        self.assertNotEqual(database.DB_PATH.resolve(), self._live_db)
        self.assertIn(self.tmp_root, database.DB_PATH.resolve().parents)

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        agy_engine.ACTIVE_RUNS.clear()
        await database.close_db_pool()
        self._users_dir_patch.stop()

        s = self._orig
        config.DB_PATH = s["cfg_db"]
        database.DB_PATH = s["db_db"]
        config.USERS_DIR = s["cfg_users"]
        config.CLI_HOMES_DIR = s["cfg_cli"]
        config.AGY_CLI_DATA_DIR = s["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = s["eng_agy"]
        config.PROJECTS_ROOT = s["cfg_proj"]
        database.PROJECTS_ROOT = s["db_proj"]
        brain_reader.PROJECTS_ROOT = s["br_proj"]
        config.USER_WORKSPACES_DIR = s["cfg_ws"]
        database.USER_WORKSPACES_DIR = s["db_ws"]
        brain_reader.USER_WORKSPACES_DIR = s["br_ws"]
        config.SHARED_PROJECTS_DIR = s["cfg_sh"]
        database.SHARED_PROJECTS_DIR = s["db_sh"]
        brain_reader.SHARED_PROJECTS_DIR = s["br_sh"]
        config.UPLOADS_DIR = s["cfg_up"]
        agy_engine.UPLOADS_DIR = s["eng_up"]
        config.CHAT_SANDBOX_DIR = s["cfg_sbx"]
        agy_engine.CHAT_SANDBOX_DIR = s["eng_sbx"]
        agy_engine.AUTO_CONTINUE_PAUSE_SEC = s["t_auto_pause"]

        _unlink_test_junctions(self.tmp_root)
        self._tmp.cleanup()

        if self._live_db_before is not None and self._live_db.exists():
            live_after = (self._live_db.stat().st_size, self._live_db.stat().st_mtime_ns)
            self.assertEqual(
                self._live_db_before,
                live_after,
                "CRITICAL: live data/hub.db must remain completely untouched!",
            )

    async def test_01_screenshot_scenario_four_reads_thinking_silent_exit_auto_resumes_openrouter(self) -> None:
        e = self.agy_engine
        spawned_cmds: list[list[str]] = []

        read_targets = (
            "openrouter-bridge/SKILL.md",
            "mcp/hub_openrouter/openrouter_models.json",
            "mcp/hub_openrouter/openrouter_multi_chat.json",
            "mcp/hub_openrouter/openrouter_chat.json",
        )
        attempt1_events: list[dict[str, Any]] = [
            {"event": "init", "conversation_id": "conv-openrouter-1615"},
        ]
        for idx, rel_path in enumerate(read_targets, start=1):
            attempt1_events.append(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": idx,
                        "step_type": "tool",
                        "state": "DONE",
                        "tool_name": "read_file",
                        "tool_info": {
                            "name": "read_file",
                            "parameters": {"Path": rel_path},
                            "output": f"content of {rel_path}",
                        },
                        "thinking_delta": "Готовлю вызов openrouter_multi_chat на 4 модели.\n" if idx == 4 else "",
                    },
                }
            )
        attempt1_events.append(
            {
                "event": "result",
                "result": {
                    "status": "DONE",
                    "response": "",
                    "usage": {"input_tokens": 1400, "output_tokens": 220},
                },
            }
        )

        attempt2_events: list[dict[str, Any]] = [
            {"event": "init", "conversation_id": "conv-openrouter-1615"},
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 1,
                    "step_type": "tool",
                    "state": "DONE",
                    "tool_name": "call_mcp_tool",
                    "tool_info": {
                        "name": "call_mcp_tool",
                        "parameters": {
                            "ServerName": "hub_openrouter",
                            "ToolName": "openrouter_multi_chat",
                            "Arguments": {"models": ["kimi", "chatgpt", "deepseek", "grok"]},
                        },
                        "output": '{"ok": true, "responses": 4}',
                    },
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 2,
                    "step_type": "agent_response",
                    "state": "DONE",
                    "text_delta": "Консилиум 4 моделей через OpenRouter завершён: Kimi, ChatGPT, DeepSeek и Grok дали единый вывод.",
                },
            },
            {
                "event": "result",
                "result": {
                    "status": "DONE",
                    "response": "",
                    "usage": {"input_tokens": 2100, "output_tokens": 340},
                },
            },
        ]

        procs = [_make_fake_proc(attempt1_events), _make_fake_proc(attempt2_events)]

        async def _fake_spawn(*cmd: Any, **kwargs: Any) -> MagicMock:
            spawned_cmds.append([str(c) for c in cmd])
            return procs.pop(0)

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_fake_spawn),
        ):
            events = [
                ev
                async for ev in e.stream_agy_turn(
                    chat_id="chat_openrouter_1615",
                    kind="chat",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Опроси через OpenRouter 4 модели: Kimi, ChatGPT, DeepSeek и Grok.",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    allow_hub_openrouter=True,
                )
            ]

        self.assertEqual(len(spawned_cmds), 2, "Must auto-resume once after silent empty exit")
        self.assertNotIn("--conversation", spawned_cmds[0])
        self.assertIn("--conversation", spawned_cmds[1])
        conv_idx = spawned_cmds[1].index("--conversation")
        self.assertEqual(spawned_cmds[1][conv_idx + 1], "conv-openrouter-1615")

        resumed_prompt = spawned_cmds[1][spawned_cmds[1].index("-p") + 1]
        self.assertEqual(resumed_prompt, e._build_empty_turn_resume_prompt([]))
        self.assertIn("НЕ перечитывай уже прочитанные файлы", resumed_prompt)
        self.assertEqual(
            spawned_cmds[1][spawned_cmds[1].index("--model") + 1],
            "gemini-4-carbon-high",
            "Model must never be downgraded during empty-turn auto-resume",
        )

        self.assertFalse(any(ev.get("type") == "error" for ev in events), events)
        done_events = [ev for ev in events if ev.get("type") == "done"]
        self.assertEqual(len(done_events), 1)
        done_ev = done_events[0]
        self.assertIn("Консилиум 4 моделей через OpenRouter завершён", done_ev["content"])
        self.assertEqual(len(done_ev["tool_calls"]), 5)
        self.assertEqual(
            [tc["name"] for tc in done_ev["tool_calls"]],
            ["read_file", "read_file", "read_file", "read_file", "call_mcp_tool"],
        )
        self.assertIn("openrouter_multi_chat", done_ev["thinking"])
        self.assertEqual(done_ev["agy_conversation_id"], "conv-openrouter-1615")

    async def test_02_exhausted_empty_turn_resumes_emits_russian_error_instead_of_empty_done(self) -> None:
        e = self.agy_engine
        self.assertEqual(e.MAX_EMPTY_TURN_RESUMES, 2)
        spawned_cmds: list[list[str]] = []

        async def _always_empty_spawn(*cmd: Any, **kwargs: Any) -> MagicMock:
            spawned_cmds.append([str(c) for c in cmd])
            attempt_no = len(spawned_cmds)
            evts: list[dict[str, Any]] = [
                {"event": "init", "conversation_id": "conv-always-silent"},
            ]
            if attempt_no == 1:
                evts.append(
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 1,
                            "step_type": "tool",
                            "state": "DONE",
                            "tool_name": "read_file",
                            "tool_info": {
                                "name": "read_file",
                                "parameters": {"Path": "SKILL.md"},
                                "output": "ok",
                            },
                            "thinking_delta": "Анализирую схему инструмента...",
                        },
                    }
                )
            evts.append(
                {
                    "event": "result",
                    "result": {
                        "status": "DONE",
                        "response": "",
                        "usage": {"input_tokens": 500, "output_tokens": 10},
                    },
                }
            )
            return _make_fake_proc(evts)

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_always_empty_spawn),
        ):
            events = [
                ev
                async for ev in e.stream_agy_turn(
                    chat_id="chat_silent_cap",
                    kind="project",
                    project_name="demo_proj",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Проанализируй проект",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    resolved_project_dir=self.proj_dir,
                )
            ]

        self.assertEqual(len(spawned_cmds), 1 + e.MAX_EMPTY_TURN_RESUMES)
        self.assertFalse(any(ev.get("type") == "done" for ev in events), "Must NOT emit empty `done`!")
        err_events = [ev for ev in events if ev.get("type") == "error"]
        self.assertEqual(len(err_events), 1)
        err_ev = err_events[0]
        self.assertEqual(
            err_ev["error"],
            "Модель завершила шаг чтения/рассуждений без текстового ответа. Нажмите «Продолжить» или «Повторить».",
        )
        self.assertEqual(err_ev["content"], "")
        self.assertIn("Анализирую схему", err_ev["thinking"])
        self.assertEqual(len(err_ev["tool_calls"]), 1)
        self.assertEqual(err_ev["usage"].get("agy_conversation_id"), "conv-always-silent")

    async def test_03_empty_text_with_real_file_writes_allows_done_while_denied_write_does_not(self) -> None:
        e = self.agy_engine

        # Helper unit checks
        self.assertFalse(e._has_real_file_write_tools([{"name": "read_file", "state": "DONE", "output": "x"}]))
        self.assertFalse(
            e._has_real_file_write_tools(
                [
                    {
                        "name": "write_file",
                        "state": "ERROR",
                        "output": "Permission denied: matches user-configured deny rule",
                    }
                ]
            )
        )
        self.assertTrue(
            e._has_real_file_write_tools([{"name": "edit_file", "state": "DONE", "output": "updated"}])
        )
        self.assertTrue(
            e._has_real_file_write_tools(
                [{"name": "replace_file_content", "state": "DONE", "output": "updated"}]
            )
        )

        # End-to-end: successful write_file + empty text after resumes still emits `done`
        spawns = 0

        async def _spawn_write_no_text(*cmd: Any, **kwargs: Any) -> MagicMock:
            nonlocal spawns
            spawns += 1
            evts: list[dict[str, Any]] = [{"event": "init", "conversation_id": "conv-write-only"}]
            if spawns == 1:
                evts.append(
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 1,
                            "step_type": "tool",
                            "state": "DONE",
                            "tool_name": "write_file",
                            "tool_info": {
                                "name": "write_file",
                                "parameters": {"Path": "notes.md"},
                                "output": "wrote 128 bytes",
                            },
                        },
                    }
                )
            evts.append({"event": "result", "result": {"status": "DONE", "response": ""}})
            return _make_fake_proc(evts)

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_write_no_text),
        ):
            events = [
                ev
                async for ev in e.stream_agy_turn(
                    chat_id="chat_write_only",
                    kind="project",
                    project_name="demo_proj",
                    model="gemini-4-carbon-high",
                    access_mode="write_files",
                    prompt="Запиши notes.md",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    resolved_project_dir=self.proj_dir,
                )
            ]

        self.assertEqual(spawns, 1 + e.MAX_EMPTY_TURN_RESUMES)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(len(events[-1]["tool_calls"]), 1)

    async def test_04_unsuccessful_result_statuses_not_masked_as_rc0(self) -> None:
        e = self.agy_engine
        for bad_status in ("FAILED", "ABORTED", "CANCELLED", "INTERRUPTED"):
            with self.subTest(status=bad_status):
                spawns = 0

                async def _spawn_bad(*cmd: Any, _st: str = bad_status, **kwargs: Any) -> MagicMock:
                    nonlocal spawns
                    spawns += 1
                    return _make_fake_proc(
                        [
                            {"event": "init", "conversation_id": f"conv-bad-{_st.lower()}"},
                            {
                                "event": "step_update",
                                "step_update": {
                                    "step_index": 1,
                                    "step_type": "tool",
                                    "state": "DONE",
                                    "tool_name": "read_file",
                                    "tool_info": {"name": "read_file", "parameters": {}, "output": "ok"},
                                },
                            },
                            {"event": "result", "result": {"status": _st, "response": ""}},
                        ],
                        returncode=1,
                    )

                with (
                    patch.object(e, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
                    patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
                    patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_bad),
                ):
                    events = [
                        ev
                        async for ev in e.stream_agy_turn(
                            chat_id=f"chat_bad_{bad_status}",
                            kind="chat",
                            project_name="",
                            model="gemini-4-carbon-high",
                            access_mode="read_only",
                            prompt="Тест неуспешного статуса",
                            attachments=[],
                            agy_conversation_id="",
                            history_messages=[],
                        )
                    ]

                self.assertEqual(spawns, 1)
                self.assertEqual(events[-1]["type"], "error")
                self.assertIn(bad_status, events[-1]["error"])

    def test_05_run_workflow_denied_in_headless_matrix_and_parked_turn_guard(self) -> None:
        e = self.agy_engine
        self.assertIn("run_workflow", e.HEADLESS_ASYNC_DENY_PATTERNS)
        self.assertIn("run_workflow(*)", e.HEADLESS_ASYNC_DENY_PATTERNS)

        cases = (
            ("chat", "read_only", True, False),
            ("chat", "read_only", False, True),
            ("project", "read_only", True, False),
            ("project", "write_files", True, False),
            ("project", "full_access", True, False),
            ("project", "full_access", False, True),
        )
        for kind, access_mode, is_admin, force_no_host in cases:
            with self.subTest(kind=kind, access_mode=access_mode, is_admin=is_admin):
                args = e._build_permission_args(
                    kind,
                    access_mode,
                    is_admin=is_admin,
                    project_dir=self.proj_dir if kind == "project" else None,
                    force_no_host_shell=force_no_host,
                )
                settings = json.loads(args[args.index("--settings") + 1])
                deny_set = set(settings["permissions"]["deny"])
                self.assertIn("run_workflow", deny_set)
                self.assertIn("run_workflow(*)", deny_set)

        # `_update_bg_task_state_on_tool` catches `run_workflow` unless denied/failed
        bg_tasks: set[str] = set()
        self.assertFalse(
            e._update_bg_task_state_on_tool(
                bg_tasks,
                {
                    "step_index": 1,
                    "name": "run_workflow",
                    "state": "ERROR",
                    "output": "Permission denied: matches user-configured deny rule",
                },
            )
        )
        self.assertTrue(
            e._update_bg_task_state_on_tool(
                bg_tasks,
                {
                    "step_index": 2,
                    "name": "run_workflow",
                    "state": "DONE",
                    "output": "Workflow started",
                },
            )
        )
        self.assertTrue(
            e.is_parked_async_turn(
                "Запустил workflow, ожидаю завершения.",
                [{"name": "run_workflow", "state": "DONE", "output": "Workflow started"}],
                waiting_for_bg_task=False,
            )
        )

    async def test_06_disk_only_thinking_without_tools_triggers_silent_empty_turn_resume(self) -> None:
        e = self.agy_engine
        conv_id = "conv-disk-thinking-only"
        spawned_cmds: list[list[str]] = []

        async def _spawn_disk_think(*cmd: Any, **kwargs: Any) -> MagicMock:
            spawned_cmds.append([str(c) for c in cmd])
            if len(spawned_cmds) == 1:
                transcript = (
                    self.tmp_cli
                    / "admin"
                    / ".gemini"
                    / "antigravity-cli"
                    / "brain"
                    / conv_id
                    / ".system_generated"
                    / "logs"
                    / "transcript.jsonl"
                )
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(
                    json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Ответь"}) + "\n"
                    + json.dumps(
                        {
                            "type": "PLANNER_RESPONSE",
                            "source": "MODEL",
                            "thinking": "Глубокое рассуждение на диске перед пустым STOP.",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return _make_fake_proc(
                    [
                        {"event": "init", "conversation_id": conv_id},
                        {"event": "result", "result": {"status": "DONE", "response": ""}},
                    ]
                )
            return _make_fake_proc(
                [
                    {"event": "init", "conversation_id": conv_id},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 1,
                            "step_type": "agent_response",
                            "state": "DONE",
                            "text_delta": "Итоговый ответ после восстановления с диска.",
                        },
                    },
                    {"event": "result", "result": {"status": "DONE", "response": ""}},
                ]
            )

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_disk_think),
        ):
            events = [
                ev
                async for ev in e.stream_agy_turn(
                    chat_id="chat_disk_think_only",
                    kind="chat",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Ответь подробно",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                )
            ]

        self.assertEqual(len(spawned_cmds), 2)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["content"], "Итоговый ответ после восстановления с диска.")
        self.assertIn("Глубокое рассуждение на диске", events[-1]["thinking"])


if __name__ == "__main__":
    unittest.main()
