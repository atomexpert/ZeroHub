"""
Isolated automated test suite for `agy.exe` Subagent / Swarm / Background-Task Guard:
1. `_build_permission_args` full security matrix (`kind="chat"`, `kind="project"` x
   `read_only` / `write_files` / `full_access` x `is_admin=True/False` x
   `force_no_host_shell=True/False`) + service CLI calls (`compact_chat_context`,
   `generate_project_passport_via_cli`) + `_run_slash_json_command` preservation.
2. `_augment_prompt_for_mode` injection of `SINGLE_TAB_EXECUTION_INSTRUCTION` and
   updated `PLAN_TASK_PROFILE_INSTRUCTION` (`live_voice=True` excluded).
3. `active_bg_tasks` / `_update_bg_task_state_on_tool` / `waiting_for_bg_task` state
   machine resilience across intermediate `read_file` & `manage_task(status=running)`
   calls (protecting live `agy.exe` from false 65s `post_text_idle` kills).
4. `is_parked_async_turn` & `looks_like_completed_response` precision (`0%` false
   positives on Q&A / specs mentioning `invoke_subagent`, `swarm`, `conversation://`,
   or `Ожидаю завершения` without real async tool launches).
5. End-to-end `stream_agy_turn` seamless parked-turn auto-resume (`--conversation <id>`)
   and strict `MAX_PARKED_TURN_RESUMES = 2` cap.

Strict invariants:
- `0% GPU / 0 MB VRAM` (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`).
- Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db`
  or `run_server.py`.
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
        except Exception:
            pass


class _FakeStreamReader:
    def __init__(
        self,
        items: list[tuple[float, bytes]],
        *,
        hang_at_end: bool = False,
    ) -> None:
        self._items = list(items)
        self._hang_at_end = hang_at_end
        self._closed_event = asyncio.Event()

    @classmethod
    def from_events(
        cls,
        events: list[dict[str, Any]],
        *,
        hang_at_end: bool = False,
    ) -> "_FakeStreamReader":
        encoded = [
            (0.0, (json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8"))
            for e in events
        ]
        return cls(encoded, hang_at_end=hang_at_end)

    def close_on_kill(self) -> None:
        self._hang_at_end = False
        self._closed_event.set()

    async def readline(self) -> bytes:
        if self._closed_event.is_set():
            return b""
        if self._items:
            delay_s, line_b = self._items.pop(0)
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            if self._closed_event.is_set():
                return b""
            return line_b
        if self._hang_at_end and not self._closed_event.is_set():
            await self._closed_event.wait()
        return b""

    async def read(self) -> bytes:
        if not self._items:
            return b""
        out = b"".join(b for _, b in self._items)
        self._items.clear()
        return out

    def __aiter__(self) -> "_FakeStreamReader":
        return self

    async def __anext__(self) -> bytes:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line


class _FakeCliProcess:
    _next_pid = 73000

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        returncode: int = 0,
        stderr_bytes: bytes = b"",
        delayed_items: list[tuple[float, bytes]] | None = None,
    ) -> None:
        _FakeCliProcess._next_pid += 1
        self.pid = _FakeCliProcess._next_pid
        self._desired_rc = returncode
        self.returncode: int | None = None
        self.killed = False
        if delayed_items is not None:
            self.stdout = _FakeStreamReader(delayed_items)
        else:
            self.stdout = _FakeStreamReader.from_events(events)
        self.stderr = _FakeStreamReader(
            [(0.0, stderr_bytes)] if stderr_bytes else []
        )

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.close_on_kill()

    async def communicate(self) -> tuple[bytes, bytes]:
        out_b = await self.stdout.read()
        err_b = await self.stderr.read()
        await self.wait()
        return out_b, err_b

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = self._desired_rc
        return self.returncode


class TestAgySubagentGuard(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        self._live_db = (ROOT_DIR / "data" / "hub.db").resolve()

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_subagent_guard_test_", ignore_cleanup_errors=True
        )
        self.tmp_root = Path(self._tmp.name).resolve()

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
            "t_post": agy_engine.STREAM_POST_TEXT_IDLE_TIMEOUT_SEC,
            "t_active": agy_engine.STREAM_ACTIVE_TOOL_TIMEOUT_SEC,
            "t_auto_pause": agy_engine.AUTO_CONTINUE_PAUSE_SEC,
        }

        tmp_db = (self.tmp_root / "hub_guard_test.db").resolve()
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = self.tmp_root / "agy_cli_data"
        tmp_proj = self.tmp_root / "projects_root"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (
            tmp_cli,
            tmp_agy / "conversations",
            tmp_agy / "brain",
            tmp_proj,
            tmp_ws,
            tmp_sh,
            tmp_up,
            tmp_sbx,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.fake_agy_bin = self.tmp_root / "fake_agy.exe"
        self.fake_agy_bin.write_bytes(b"MZ")

        config.DB_PATH = database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_cli
        config.AGY_CLI_DATA_DIR = agy_engine.AGY_CLI_DATA_DIR = tmp_agy
        config.PROJECTS_ROOT = database.PROJECTS_ROOT = brain_reader.PROJECTS_ROOT = tmp_proj
        config.USER_WORKSPACES_DIR = database.USER_WORKSPACES_DIR = brain_reader.USER_WORKSPACES_DIR = tmp_ws
        config.SHARED_PROJECTS_DIR = database.SHARED_PROJECTS_DIR = brain_reader.SHARED_PROJECTS_DIR = tmp_sh
        config.UPLOADS_DIR = agy_engine.UPLOADS_DIR = tmp_up
        config.CHAT_SANDBOX_DIR = agy_engine.CHAT_SANDBOX_DIR = tmp_sbx
        agy_engine.AUTO_CONTINUE_PAUSE_SEC = 0.01
        agy_engine.ACTIVE_RUNS.clear()
        self.assertNotEqual(database.DB_PATH.resolve(), self._live_db)
        self.assertIn(self.tmp_root, database.DB_PATH.resolve().parents)

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        agy_engine.ACTIVE_RUNS.clear()
        s = self._orig
        config.DB_PATH = s["cfg_db"]
        database.DB_PATH = s["db_db"]
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
        agy_engine.STREAM_POST_TEXT_IDLE_TIMEOUT_SEC = s["t_post"]
        agy_engine.STREAM_ACTIVE_TOOL_TIMEOUT_SEC = s["t_active"]
        agy_engine.AUTO_CONTINUE_PAUSE_SEC = s["t_auto_pause"]

        _unlink_test_junctions(self.tmp_root)
        self._tmp.cleanup()

    async def test_1_permission_args_matrix_and_service_calls(self) -> None:
        import app.agy_engine as agy_engine

        proj_dir = self.tmp_root / "projects_root" / "demo_proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        up_dir = self.tmp_root / "uploads" / "chat_demo"
        up_dir.mkdir(parents=True, exist_ok=True)
        cli_home = self.tmp_root / "cli_homes" / "u1"
        cli_home.mkdir(parents=True, exist_ok=True)

        required_headless_denies = set(agy_engine.HEADLESS_ASYNC_DENY_PATTERNS)
        for core_tool in (
            "invoke_subagent",
            "invoke_subagent(*)",
            "define_subagent",
            "manage_subagents",
            "send_message",
            "swarm",
            "swarm(*)",
            "post_message",
            "read_board",
            "swarm_wait",
            "schedule",
            "schedule(*)",
            "ask_question",
            "ask_question(*)",
        ):
            self.assertIn(core_tool, required_headless_denies)
        self.assertNotIn("manage_task", required_headless_denies)
        self.assertNotIn("manage_task(*)", required_headless_denies)

        matrix_cases: list[dict[str, Any]] = [
            # 1) Plain chat (admin & non-admin)
            {
                "label": "chat_admin",
                "kind": "chat",
                "access_mode": "read_only",
                "is_admin": True,
                "force_no_host_shell": False,
                "allow_hub_openrouter": True,
                "allow_hub_sandbox": False,
            },
            {
                "label": "chat_user",
                "kind": "chat",
                "access_mode": "full_access",
                "is_admin": False,
                "force_no_host_shell": True,
                "allow_hub_openrouter": False,
                "allow_hub_sandbox": True,
            },
            # 2) Sandboxed project contours (force_no_host_shell=True): read_only / write_files / full_access(docker)
            {
                "label": "sandbox_read_only",
                "kind": "project",
                "access_mode": "read_only",
                "is_admin": False,
                "force_no_host_shell": True,
                "allow_hub_openrouter": False,
                "allow_hub_sandbox": False,
            },
            {
                "label": "sandbox_write_files",
                "kind": "project",
                "access_mode": "write_files",
                "is_admin": False,
                "force_no_host_shell": True,
                "allow_hub_openrouter": True,
                "allow_hub_sandbox": False,
            },
            {
                "label": "sandbox_docker_full",
                "kind": "project",
                "access_mode": "full_access",
                "is_admin": False,
                "force_no_host_shell": True,
                "allow_hub_openrouter": True,
                "allow_hub_sandbox": True,
            },
            # 3) Trusted host contours (force_no_host_shell=False): read_only / write_files / full_access
            {
                "label": "host_read_only",
                "kind": "project",
                "access_mode": "read_only",
                "is_admin": True,
                "force_no_host_shell": False,
                "allow_hub_openrouter": False,
                "allow_hub_sandbox": False,
            },
            {
                "label": "host_write_files",
                "kind": "project",
                "access_mode": "write_files",
                "is_admin": True,
                "force_no_host_shell": False,
                "allow_hub_openrouter": True,
                "allow_hub_sandbox": False,
            },
            {
                "label": "host_full_admin",
                "kind": "project",
                "access_mode": "full_access",
                "is_admin": True,
                "force_no_host_shell": False,
                "allow_hub_openrouter": False,
                "allow_hub_sandbox": False,
            },
            {
                "label": "host_full_dual_key_user",
                "kind": "project",
                "access_mode": "full_access",
                "is_admin": False,
                "force_no_host_shell": False,
                "allow_hub_openrouter": True,
                "allow_hub_sandbox": True,
            },
        ]

        for case in matrix_cases:
            with self.subTest(case=case["label"]):
                args = agy_engine._build_permission_args(
                    case["kind"],
                    case["access_mode"],
                    up_dir,
                    is_admin=case["is_admin"],
                    allow_hub_openrouter=case["allow_hub_openrouter"],
                    allow_hub_sandbox=case["allow_hub_sandbox"],
                    cli_home_dir=cli_home,
                    project_dir=proj_dir,
                    force_no_host_shell=case["force_no_host_shell"],
                    resume_conv_id="conv-matrix-01",
                )
                self.assertIn("--disable-slash-commands", args)
                self.assertIn("--settings", args)
                settings = json.loads(args[args.index("--settings") + 1])
                perms = settings.get("permissions") or {}
                deny_set = set(perms.get("deny") or [])
                allow_set = set(perms.get("allow") or [])

                # Every headless async/subagent/swarm/schedule/ask_question pattern MUST be denied
                self.assertTrue(
                    required_headless_denies.issubset(deny_set),
                    f"Missing headless deny patterns in {case['label']}: {required_headless_denies - deny_set}",
                )
                # Standard synchronous tools MUST NOT be blocked globally
                for sync_tool in ("manage_task", "manage_task(*)", "read_file(*)"):
                    self.assertNotIn(sync_tool, deny_set)

                # Write permissions preserved in write_files / full_access
                if case["kind"] == "project" and case["access_mode"] in ("write_files", "full_access"):
                    self.assertNotIn("write_file(*)", deny_set)
                    self.assertNotIn("edit_file(*)", deny_set)
                else:
                    self.assertIn("write_file(*)", deny_set)
                    self.assertIn("edit_file(*)", deny_set)

                # Host shell RBAC preserved
                if (
                    case["kind"] == "project"
                    and case["access_mode"] == "full_access"
                    and not case["force_no_host_shell"]
                ):
                    self.assertIn("--dangerously-skip-permissions", args)
                    self.assertNotIn("command(*)", deny_set)
                else:
                    self.assertNotIn("--dangerously-skip-permissions", args)
                    self.assertIn("command(*)", deny_set)
                    if case["kind"] == "chat" or case["force_no_host_shell"] or not case["is_admin"]:
                        self.assertFalse(settings.get("allowNonWorkspaceAccess", True))

                # MCP OpenRouter & Sandbox preservation
                if case["allow_hub_openrouter"]:
                    self.assertIn("openrouter_chat", allow_set)
                    self.assertNotIn("openrouter_chat", deny_set)
                else:
                    self.assertIn("openrouter_chat", deny_set)

                expect_sandbox = (
                    case["allow_hub_sandbox"]
                    and case["kind"] == "project"
                    and case["access_mode"] == "full_access"
                    and case["force_no_host_shell"]
                )
                if expect_sandbox:
                    self.assertIn("sandbox_exec", allow_set)
                    self.assertNotIn("sandbox_exec", deny_set)
                else:
                    self.assertIn("sandbox_exec", deny_set)

        # Service CLI calls: compact_chat_context & generate_project_passport_via_cli
        captured_service_cmds: list[list[str]] = []

        async def _fake_service_exec(*cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            captured_service_cmds.append([str(c) for c in cmd])
            is_passport = "passport_overview" in str(cmd[2])
            resp_text = (
                json.dumps(
                    {
                        "passport_overview": "- Обзор",
                        "passport_rules": "- Правила",
                        "passport_file_map": "- Файлы",
                        "starter_changelog": [],
                    },
                    ensure_ascii=False,
                )
                if is_passport
                else "### 1. Цель\nСводка готова."
            )
            return _FakeCliProcess(
                [
                    {"event": "init", "conversation_id": "tmp-srv-conv"},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "agent_response",
                            "state": "DONE",
                            "text_delta": resp_text,
                        },
                    },
                    {"event": "result", "result": {"status": "DONE", "usage": {}}},
                ]
            )

        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_service_exec),
        ):
            await agy_engine.compact_chat_context(
                chat={"id": "c_cmp", "kind": "project", "project_name": "demo_proj"},
                messages=[
                    {"id": 1, "role": "user", "content": "Привет"},
                    {"id": 2, "role": "assistant", "content": "Ответ"},
                ],
                model="gemini-4-carbon-low",
            )
            await agy_engine.generate_project_passport_via_cli(
                project_name="demo_proj",
                project_dir=proj_dir,
                recent_chats=[],
            )

        self.assertEqual(len(captured_service_cmds), 2)
        for srv_cmd in captured_service_cmds:
            self.assertIn("--disable-slash-commands", srv_cmd)
            self.assertIn("--settings", srv_cmd)
            srv_settings = json.loads(srv_cmd[srv_cmd.index("--settings") + 1])
            srv_deny = set(srv_settings["permissions"]["deny"])
            self.assertTrue(required_headless_denies.issubset(srv_deny))

        # Verify `_run_slash_json_command` (/usage, /credits) does NOT pass `--disable-slash-commands`
        slash_cmds: list[list[str]] = []

        async def _fake_slash_exec(*cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            slash_cmds.append([str(c) for c in cmd])
            return _FakeCliProcess(
                [{"event": "command_result", "command": {"data": {"remaining_credits": 42}}}]
            )

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_slash_exec):
            slash_res = await agy_engine._run_slash_json_command(str(self.fake_agy_bin), "/credits")
        self.assertEqual(slash_res, {"remaining_credits": 42})
        self.assertEqual(len(slash_cmds), 1)
        self.assertNotIn("--disable-slash-commands", slash_cmds[0])

    def test_2_single_tab_execution_instruction_injection(self) -> None:
        import app.agy_engine as agy_engine

        # First turn of regular chat -> injected
        p_chat_first = agy_engine._augment_prompt_for_mode(
            kind="chat",
            prompt="Объясни алгоритм",
            attachments=[],
            is_first_turn=True,
            access_mode="read_only",
            live_voice=False,
        )
        self.assertIn(agy_engine.SINGLE_TAB_EXECUTION_INSTRUCTION.strip(), p_chat_first)

        # Subsequent turn of regular chat -> NOT injected (preserves zero-overhead plain chat)
        p_chat_next = agy_engine._augment_prompt_for_mode(
            kind="chat",
            prompt="Спасибо",
            attachments=[],
            is_first_turn=False,
            access_mode="read_only",
            live_voice=False,
        )
        self.assertEqual(p_chat_next, "Спасибо")

        # First turn of project Plan (`read_only`) -> both PLAN_TASK_PROFILE and SINGLE_TAB injected
        p_plan_first = agy_engine._augment_prompt_for_mode(
            kind="project",
            prompt="Составь ТЗ",
            attachments=[],
            is_first_turn=True,
            access_mode="read_only",
            live_voice=False,
        )
        self.assertIn("<TASK_PROFILE>", p_plan_first)
        self.assertIn("invoke_subagent", agy_engine.PLAN_TASK_PROFILE_INSTRUCTION)
        self.assertIn(agy_engine.SINGLE_TAB_EXECUTION_INSTRUCTION.strip(), p_plan_first)

        # Subsequent turns (`is_first_turn=False`) of project `write_files` and `full_access` -> MUST be injected!
        for mode in ("write_files", "full_access"):
            p_proj_exec = agy_engine._augment_prompt_for_mode(
                kind="project",
                prompt="Выполни ТЗ",
                attachments=[],
                is_first_turn=False,
                access_mode=mode,
                live_voice=False,
            )
            self.assertIn(agy_engine.SINGLE_TAB_EXECUTION_INSTRUCTION.strip(), p_proj_exec)
            self.assertNotIn("<TASK_PROFILE>", p_proj_exec)

        # `live_voice=True` -> NEVER injects `SINGLE_TAB_EXECUTION_INSTRUCTION`
        for is_first in (True, False):
            p_voice = agy_engine._augment_prompt_for_mode(
                kind="project",
                prompt="Проверь сборку",
                attachments=[],
                is_first_turn=is_first,
                access_mode="full_access",
                live_voice=True,
            )
            self.assertNotIn(agy_engine.SINGLE_TAB_EXECUTION_INSTRUCTION.strip(), p_voice)
            self.assertIn(agy_engine.LIVE_VOICE_MODE_INSTRUCTION.strip(), p_voice)

    async def test_3_active_bg_tasks_resilience_and_watchdog_protection(self) -> None:
        import app.agy_engine as agy_engine

        active_bg: set[str] = set()

        # 3a. Policy-denied `invoke_subagent` (`ERROR` / `Permission denied`) MUST NOT arm `waiting_for_bg_task`
        self.assertFalse(
            agy_engine._update_bg_task_state_on_tool(
                active_bg,
                {
                    "step_index": 1,
                    "name": "invoke_subagent",
                    "state": "ERROR",
                    "parameters": {"TypeName": "research"},
                    "output": "Permission denied: tool invoke_subagent matches user-configured deny rule.",
                },
            )
        )
        self.assertEqual(active_bg, set())

        # 3b. Background `run_command` arms `waiting_for_bg_task = True`
        self.assertTrue(
            agy_engine._update_bg_task_state_on_tool(
                active_bg,
                {
                    "step_index": 2,
                    "name": "run_command",
                    "state": "DONE",
                    "parameters": {"CommandLine": "npm run build"},
                    "output": "Command is running in the background. Task ID: task_1.",
                },
            )
        )
        self.assertEqual(active_bg, {"task_1"})

        # 3c. Intermediate `read_file` and `manage_task(Action="status")` with "status: running" MUST NOT clear wait!
        self.assertTrue(
            agy_engine._update_bg_task_state_on_tool(
                active_bg,
                {
                    "step_index": 3,
                    "name": "read_file",
                    "state": "DONE",
                    "parameters": {"Path": "package.json"},
                    "output": '{"name": "app"}',
                },
            )
        )
        self.assertTrue(
            agy_engine._update_bg_task_state_on_tool(
                active_bg,
                {
                    "step_index": 4,
                    "name": "manage_task",
                    "state": "DONE",
                    "parameters": {"Action": "status", "TaskId": "task_1"},
                    "output": "Task task_1 status: running\nCompiling modules...",
                },
            )
        )
        self.assertEqual(active_bg, {"task_1"})

        # 3d. `manage_task(Action="status")` reporting completion clears `task_1`
        self.assertFalse(
            agy_engine._update_bg_task_state_on_tool(
                active_bg,
                {
                    "step_index": 5,
                    "name": "manage_task",
                    "state": "DONE",
                    "parameters": {"Action": "status", "TaskId": "task_1"},
                    "output": "Task task_1 status: completed (exit code: 0)",
                },
            )
        )
        self.assertEqual(active_bg, set())

        # 3e. `manage_task(Action="kill")` also clears background wait
        agy_engine._update_bg_task_state_on_tool(
            active_bg,
            {
                "step_index": 6,
                "name": "run_command",
                "state": "DONE",
                "parameters": {"CommandLine": "python watcher.py"},
                "output": "Tool is running as a background task (Task ID: task_2)",
            },
        )
        self.assertEqual(active_bg, {"task_2"})
        self.assertFalse(
            agy_engine._update_bg_task_state_on_tool(
                active_bg,
                {
                    "step_index": 7,
                    "name": "manage_task",
                    "state": "DONE",
                    "parameters": {"Action": "kill", "TaskId": "task_2"},
                    "output": "Killed task_2",
                },
            )
        )
        self.assertEqual(active_bg, set())

        # 3f. Live `stream_agy_turn` check: bg `run_command` -> `read_file` -> `manage_task(running)`
        #     -> long completed-looking text -> pause 4x longer than `STREAM_POST_TEXT_IDLE_TIMEOUT_SEC`
        #     MUST NOT kill `agy.exe`!
        long_text_after_read = (
            "Проверил конфигурацию сборщика в файле package.json и текущий статус фонового процесса сборки — "
            "все зависимости пакета корректны, компиляция модулей продолжается в штатном режиме без каких-либо ошибок."
        )
        self.assertGreaterEqual(len(long_text_after_read), 180)
        self.assertTrue(agy_engine.looks_like_completed_response(long_text_after_read))

        pre_ev = [
            {"event": "init", "conversation_id": "conv-bg-mid-read"},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 1,
                    "tool_name": "run_command",
                    "state": "DONE",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "pytest"},
                        "output": "Command is running in the background. Task ID: task_1",
                    },
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 2,
                    "tool_name": "read_file",
                    "state": "DONE",
                    "tool_info": {
                        "name": "read_file",
                        "parameters": {"Path": "README.md"},
                        "output": "# Demo",
                    },
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 3,
                    "tool_name": "manage_task",
                    "state": "DONE",
                    "tool_info": {
                        "name": "manage_task",
                        "parameters": {"Action": "status", "TaskId": "task_1"},
                        "output": "status: running",
                    },
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 4,
                    "state": "DONE",
                    "text_delta": long_text_after_read,
                },
            },
        ]
        post_ev = [
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 6,
                    "state": "DONE",
                    "text_delta": "Фоновый прогон pytest успешно завершён: 24 passed.",
                },
            },
            {"event": "result", "result": {"status": "DONE", "usage": {"input_tokens": 100}}},
        ]
        delayed_lines = [
            (0.0, (json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8"))
            for e in pre_ev
        ] + [
            (
                0.16 if idx == 0 else 0.0,
                (json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8"),
            )
            for idx, e in enumerate(post_ev)
        ]
        proc_mid_read = _FakeCliProcess([], delayed_items=delayed_lines)

        async def _spawn_mid_read(*args: Any, **kwargs: Any) -> _FakeCliProcess:
            return proc_mid_read

        events_out: list[dict[str, Any]] = []
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch.object(agy_engine, "STREAM_POST_TEXT_IDLE_TIMEOUT_SEC", 0.04),
            patch.object(agy_engine, "STREAM_ACTIVE_TOOL_TIMEOUT_SEC", 2.0),
            patch("asyncio.create_subprocess_exec", side_effect=_spawn_mid_read),
        ):
            async for ev in agy_engine.stream_agy_turn(
                chat_id="chat_bg_mid_read",
                kind="chat",
                project_name="",
                model="claude-opus-5-5-high",
                access_mode="read_only",
                prompt="Запусти тесты",
                attachments=[],
                agy_conversation_id="",
                history_messages=[],
            ):
                events_out.append(ev)

        self.assertFalse(
            proc_mid_read.killed,
            "Intermediate read_file / manage_task(running) MUST NOT drop waiting_for_bg_task!",
        )
        done_ev = next((e for e in events_out if e.get("type") == "done"), None)
        self.assertIsNotNone(done_ev)
        assert done_ev is not None
        self.assertIn("24 passed.", done_ev["content"])

    def test_4_is_parked_async_turn_zero_false_positives(self) -> None:
        import app.agy_engine as agy_engine

        # Q&A / Architectural spec mentioning all forbidden words & wait phrases, with NO real async tool
        spec_text = (
            "## Разбор проблемы\n"
            "Нельзя вызывать `invoke_subagent`, `swarm` или ссылки [Worker](conversation://abc-123).\n"
            "Запустил субагентов и ожидаю завершения.\n"
            "Жду их завершения."
        )
        self.assertFalse(
            agy_engine.is_parked_async_turn(spec_text, [], waiting_for_bg_task=False)
        )
        self.assertFalse(
            agy_engine.is_parked_async_turn(
                spec_text,
                [
                    {"name": "read_file", "state": "DONE", "output": "content"},
                    {
                        "name": "invoke_subagent",
                        "state": "ERROR",
                        "output": "Permission denied: invoke_subagent matches user-configured deny rule",
                    },
                ],
                waiting_for_bg_task=False,
            )
        )

        # `looks_like_completed_response` checks last 3 non-empty lines (including bullet list continuation)
        three_line_tail_wait = (
            "А" * 200
            + "\nЗапустил субагентов для проверки модулей.\n"
            + "- Бэкенд\n"
            + "- Фронтенд."
        )
        self.assertFalse(agy_engine.looks_like_completed_response(three_line_tail_wait))
        colon_bullet_tail_wait = (
            "А" * 200
            + "\nЗапустил субагентов и жду их завершения:\n\n"
            + "- Бэкенд\n"
            + "- Фронтенд."
        )
        self.assertFalse(agy_engine.looks_like_completed_response(colon_bullet_tail_wait))
        self.assertTrue(
            agy_engine.is_parked_async_turn(
                colon_bullet_tail_wait,
                [{"name": "invoke_subagent", "state": "DONE", "output": "ok"}],
                waiting_for_bg_task=False,
            )
        )

        # Superseded interim lines BEFORE `\n\n` followed by a completed prose conclusion -> completed (`True`) & NOT parked (`False`)!
        superseded_after_wakeup = (
            "Запустил первый прогон автотестов визуального обновления и регрессионные проверки — дожидаюсь завершения прогона.\n"
            "Запущен повторный прогон изолированных автотестов pytest — ожидаю завершения выполнения задачи.\n\n"
            "Все 12 изолированных автотестов успешно пройдены, регрессий не обнаружено."
        )
        self.assertTrue(agy_engine.looks_like_completed_response(superseded_after_wakeup))
        self.assertFalse(
            agy_engine.is_parked_async_turn(
                superseded_after_wakeup,
                [
                    {
                        "name": "run_command",
                        "state": "DONE",
                        "output": "Tool is running as a background task (task ID: task-1).",
                    }
                ],
                waiting_for_bg_task=False,
            )
        )

        # Real background `run_command` + waiting_for_bg_task=True -> True
        self.assertTrue(
            agy_engine.is_parked_async_turn(
                "Запустил сборку в фоне, ожидаю завершения.",
                [
                    {
                        "name": "run_command",
                        "state": "DONE",
                        "output": "Command is running in the background. Task ID: task_9",
                    }
                ],
                waiting_for_bg_task=True,
            )
        )

        # Real `invoke_subagent` (DONE) + `conversation://` link or wait phrase -> True
        self.assertTrue(
            agy_engine.is_parked_async_turn(
                "Делегировал задачу исследователю: [Researcher](conversation://sub-01).",
                [{"name": "invoke_subagent", "state": "DONE", "output": "conversationId: sub-01"}],
                waiting_for_bg_task=False,
            )
        )

        # Real background `run_command` earlier in turn, but turn already completed with normal report -> False
        self.assertFalse(
            agy_engine.is_parked_async_turn(
                "Сборка завершена.\nВсе 18 автотестов успешно пройдены, ошибок нет.",
                [
                    {
                        "name": "run_command",
                        "state": "DONE",
                        "output": "Command is running in the background. Task ID: task_9",
                    },
                    {
                        "name": "manage_task",
                        "state": "DONE",
                        "output": "status: completed (exit code: 0)",
                    },
                ],
                waiting_for_bg_task=False,
            )
        )

    async def test_5_e2e_stream_agy_turn_auto_resume_and_max_cap(self) -> None:
        import app.agy_engine as agy_engine

        proj_dir = self.tmp_root / "projects_root" / "guard_e2e"
        proj_dir.mkdir(parents=True, exist_ok=True)

        # Scenario A: 1st subprocess parks on background task & exits (`rc=0`);
        # `stream_agy_turn` MUST NOT emit premature `done`, must resume `--conversation conv-park-1`,
        # and emit a single final `done` after the 2nd subprocess completes!
        spawned_cmds_a: list[list[str]] = []
        procs_a = [
            _FakeCliProcess(
                [
                    {"event": "init", "conversation_id": "conv-park-1"},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "tool",
                            "step_index": 1,
                            "tool_name": "run_command",
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {"CommandLine": "npm run build"},
                                "output": "Command is running in the background. Task ID: build_1",
                            },
                        },
                    },
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "agent_response",
                            "step_index": 2,
                            "state": "DONE",
                            "text_delta": "Запустил сборку в фоне, ожидаю завершения.",
                        },
                    },
                    {
                        "event": "result",
                        "result": {"status": "DONE", "usage": {"input_tokens": 120, "output_tokens": 20}},
                    },
                ]
            ),
            _FakeCliProcess(
                [
                    {"event": "init", "conversation_id": "conv-park-1"},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "tool",
                            "step_index": 1,
                            "tool_name": "read_file",
                            "state": "DONE",
                            "tool_info": {
                                "name": "read_file",
                                "parameters": {"Path": "dist/index.js"},
                                "output": "// bundle ready",
                            },
                        },
                    },
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "agent_response",
                            "step_index": 2,
                            "state": "DONE",
                            "text_delta": "Сборка успешно завершена, бандл dist/index.js проверен.",
                        },
                    },
                    {
                        "event": "result",
                        "result": {"status": "DONE", "usage": {"input_tokens": 240, "output_tokens": 55}},
                    },
                ]
            ),
        ]

        async def _fake_spawn_a(*cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            spawned_cmds_a.append([str(c) for c in cmd])
            return procs_a.pop(0)

        events_a: list[dict[str, Any]] = []
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_spawn_a),
        ):
            async for ev in agy_engine.stream_agy_turn(
                chat_id="chat_park_e2e",
                kind="project",
                project_name="guard_e2e",
                model="claude-opus-5-5-high",
                access_mode="full_access",
                prompt="Собери фронтенд и проверь артефакт",
                attachments=[],
                agy_conversation_id="",
                history_messages=[],
                resolved_project_dir=proj_dir,
            ):
                events_a.append(ev)

        self.assertEqual(len(spawned_cmds_a), 2)
        self.assertNotIn("--conversation", spawned_cmds_a[0])
        self.assertIn("--conversation", spawned_cmds_a[1])
        conv_idx = spawned_cmds_a[1].index("--conversation")
        self.assertEqual(spawned_cmds_a[1][conv_idx + 1], "conv-park-1")
        self.assertIn("WaitMsBeforeAsync=10000", spawned_cmds_a[1][2])

        done_events_a = [e for e in events_a if e.get("type") == "done"]
        self.assertEqual(len(done_events_a), 1, "Must emit `done` ONLY ONCE after auto-resume completes!")
        final_content_a = done_events_a[0]["content"]
        self.assertIn(
            "Запустил сборку в фоне, ожидаю завершения.\n\nСборка успешно завершена, бандл dist/index.js проверен.",
            final_content_a,
        )
        self.assertEqual(len(done_events_a[0]["tool_calls"]), 2)

        # Scenario B: Respects upper cap `MAX_PARKED_TURN_RESUMES = 2` (1 initial + 2 resumes = 3 spawns max)
        spawned_count_b = 0

        async def _fake_spawn_always_parked(*cmd: Any, **kwargs: Any) -> _FakeCliProcess:
            nonlocal spawned_count_b
            spawned_count_b += 1
            return _FakeCliProcess(
                [
                    {"event": "init", "conversation_id": "conv-park-cap"},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "tool",
                            "step_index": 1,
                            "tool_name": "run_command",
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {"CommandLine": "sleep 30"},
                                "output": f"Command is running in the background. Task ID: t_{spawned_count_b}",
                            },
                        },
                    },
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_type": "agent_response",
                            "step_index": 2,
                            "state": "DONE",
                            "text_delta": f"Попытка #{spawned_count_b}: жду завершения.",
                        },
                    },
                    {"event": "result", "result": {"status": "DONE", "usage": {}}},
                ]
            )

        events_b: list[dict[str, Any]] = []
        with (
            patch.object(agy_engine, "resolve_agy_bin", return_value=str(self.fake_agy_bin)),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_spawn_always_parked),
        ):
            async for ev in agy_engine.stream_agy_turn(
                chat_id="chat_park_cap",
                kind="project",
                project_name="guard_e2e",
                model="claude-opus-5-5-high",
                access_mode="full_access",
                prompt="Тест лимита дожима",
                attachments=[],
                agy_conversation_id="",
                history_messages=[],
                resolved_project_dir=proj_dir,
            ):
                events_b.append(ev)

        self.assertEqual(spawned_count_b, 1 + agy_engine.MAX_PARKED_TURN_RESUMES)
        self.assertEqual(agy_engine.MAX_PARKED_TURN_RESUMES, 2)
        done_events_b = [e for e in events_b if e.get("type") == "done"]
        self.assertEqual(len(done_events_b), 1)


if __name__ == "__main__":
    unittest.main()
