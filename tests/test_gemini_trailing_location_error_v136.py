# -*- coding: utf-8 -*-
"""v136 + v138: Suppress trailing/sticky FAILED_PRECONDITION (400) & teardown CLI errors after any
fresh Gemini response text (including short `< 180` char replies without `state == "DONE"`), preserve
honest startup/post-tool `0`-text geo-errors with a Russian proxy-region hint, hide false trailing
location banners on already-saved non-empty assistant cards without touching `hub.db`, and keep
`syncActiveChatHeaderStatus()` (`#activeChatStatePill`) strictly tied to the latest turn."""
from __future__ import annotations

import ast
import asyncio
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_audit_fixes_v128 as isolated
import test_smart_hq_return_v115 as hq_iso

ROOT_DIR = Path(__file__).resolve().parents[1]
LOC_ERR = "FAILED_PRECONDITION (code 400): User location is not supported for the API use."


class GeminiTrailingLocationErrorV136Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolated.AuditFixesV128Tests.setUpClass.__func__)
    tearDownClass = classmethod(isolated.AuditFixesV128Tests.tearDownClass.__func__)
    asyncSetUp = isolated.AuditFixesV128Tests.asyncSetUp
    asyncTearDown = isolated.AuditFixesV128Tests.asyncTearDown
    _assert_live_db_untouched = isolated.AuditFixesV128Tests._assert_live_db_untouched
    _make_agy_account = isolated.AuditFixesV128Tests._make_agy_account

    async def test_01_completed_response_suppresses_trailing_location_error_and_keeps_queue_unpaused(self):
        e = self.agy_engine
        account = await self._make_agy_account("hq136@example.test", "HQ136", make_active=True)
        binary = self.tmp_root / "fake_agy_v136.exe"
        binary.write_text("fake", encoding="utf-8")

        calls: list[tuple] = []

        async def _spawn(*args, **kwargs):
            calls.append(args)
            proc = MagicMock(returncode=1)
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            for ev in (
                {"event": "init", "conversation_id": "conv-hq-v136"},
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "step_type": "agent_response",
                        "state": "DONE",
                        "text_delta": "Полный ответ.",
                    },
                },
                {
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "error": LOC_ERR,
                        "usage": {"input_tokens": 210, "output_tokens": 42},
                    },
                },
            ):
                proc.stdout.feed_data((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=1)
            proc.aclose_lease = AsyncMock()
            return proc

        # 1) Direct stream_agy_turn contract
        with (
            patch.object(e, "resolve_agy_bin", return_value=str(binary)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value="http://127.0.0.1:10808")),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn),
            self.assertLogs("agy_hub.agy_engine", level="WARNING") as log_cm,
        ):
            events = [
                evt
                async for evt in e.stream_agy_turn(
                    chat_id="chat-done-136",
                    kind="project",
                    project_name="Antigravity_Hub",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Проверь скриншот",
                    attachments=[],
                    agy_conversation_id="conv-hq-v136",
                    history_messages=[],
                    preferred_agy_account_id=account["id"],
                )
            ]

        self.assertEqual(len(calls), 1, "Completed turn must not spawn extra retry processes")
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "gemini-4-carbon-high")
        self.assertFalse(any(ev.get("type") == "error" for ev in events), events)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["content"], "Полный ответ.")
        self.assertNotIn("Ошибка", events[-1]["content"])
        self.assertTrue(
            any("Suppressed trailing post-response CLI error" in ln for ln in log_cm.output),
            log_cm.output,
        )

        # 2) End-to-end main._run_turn_task_inner_body: message is `done` without error banner & queue stays unpaused
        admin_user = await self.database.get_user_by_id("admin")
        chat = await self.database.create_chat(
            owner_id="admin",
            kind="chat",
            title="Штаб v136",
            model="gemini-4-carbon-high",
            access_mode="read_only",
            preferred_agy_account_id=account["id"],
        )
        cid = chat["id"]
        self.main_mod.CHAT_TURN_QUEUES[cid] = [{"id": "q1", "chat_id": cid, "prompt": "Следующий вопрос"}]
        self.main_mod.CHAT_QUEUE_PAUSED.discard(cid)
        ws_events: list[dict] = []

        async def _capture_ws(_chat, payload, shared_member_ids=None):
            ws_events.append(payload)

        try:
            with (
                patch.object(e, "resolve_agy_bin", return_value=str(binary)),
                patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value="http://127.0.0.1:10808")),
                patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn),
                patch.object(self.main_mod, "fetch_available_models", AsyncMock(return_value=[{"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"}])),
                patch.object(self.main_mod, "_spawn_telemetry_task", lambda coro: coro.close()),
                patch.object(self.main_mod.ws_manager, "broadcast_for_chat", side_effect=_capture_ws),
            ):
                await self.main_mod._run_turn_task_inner_body(
                    chat_id=cid,
                    prompt="Проверь скриншот",
                    attachments=[],
                    override_model="gemini-4-carbon-high",
                    override_access_mode="read_only",
                    trigger_user=admin_user,
                )
            msgs = await self.database.get_messages(cid)
            assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
            self.assertEqual(len(assistant_msgs), 1)
            saved_msg = assistant_msgs[0]
            self.assertEqual(saved_msg["status"], "done")
            self.assertEqual(saved_msg["content"], "Полный ответ.")
            self.assertNotIn("FAILED_PRECONDITION", saved_msg["content"])
            self.assertNotIn(cid, self.main_mod.CHAT_QUEUE_PAUSED)
            self.assertTrue(any(p.get("type") == "turn_done" for p in ws_events))
            self.assertFalse(any(p.get("type") == "turn_error" for p in ws_events))
        finally:
            self.main_mod.CHAT_TURN_QUEUES.pop(cid, None)
            self.main_mod.CHAT_QUEUE_PAUSED.discard(cid)

    async def test_01b_short_response_without_done_suppresses_sticky_location_error_v138(self):
        """v138: Short reply (`"Ок."`, < 180 chars) with `state="IN_PROGRESS"` (no `state="DONE"`)
        followed by sticky `result(status="ERROR", error=LOC_ERR)` MUST complete as `done`."""
        e = self.agy_engine
        account = await self._make_agy_account("short138@example.test", "Short138", make_active=True)
        binary = self.tmp_root / "fake_agy_short138.exe"
        binary.write_text("fake", encoding="utf-8")

        async def _spawn_short(*args, **kwargs):
            proc = MagicMock(returncode=1)
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            for ev in (
                {"event": "init", "conversation_id": "conv-short-v138"},
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "step_type": "agent_response",
                        "state": "IN_PROGRESS",
                        "text_delta": "Ок.",
                    },
                },
                {
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "error": LOC_ERR,
                        "usage": {"input_tokens": 95, "output_tokens": 3},
                    },
                },
            ):
                proc.stdout.feed_data((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=1)
            proc.aclose_lease = AsyncMock()
            return proc

        admin_user = await self.database.get_user_by_id("admin")
        chat = await self.database.create_chat(
            owner_id="admin",
            kind="chat",
            title="Короткий ответ v138",
            model="gemini-4-carbon-high",
            access_mode="read_only",
            preferred_agy_account_id=account["id"],
        )
        cid = chat["id"]
        self.main_mod.CHAT_TURN_QUEUES[cid] = [{"id": "q_short", "chat_id": cid, "prompt": "Дальше"}]
        self.main_mod.CHAT_QUEUE_PAUSED.discard(cid)
        try:
            with (
                patch.object(e, "resolve_agy_bin", return_value=str(binary)),
                patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value="http://127.0.0.1:10808")),
                patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_short),
                patch.object(self.main_mod, "fetch_available_models", AsyncMock(return_value=[{"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"}])),
                patch.object(self.main_mod, "_spawn_telemetry_task", lambda coro: coro.close()),
                patch.object(self.main_mod.ws_manager, "broadcast_for_chat", AsyncMock()),
            ):
                await self.main_mod._run_turn_task_inner_body(
                    chat_id=cid,
                    prompt="Коротко подтверди",
                    attachments=[],
                    override_model="gemini-4-carbon-high",
                    override_access_mode="read_only",
                    trigger_user=admin_user,
                )
            msgs = await self.database.get_messages(cid)
            assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
            self.assertEqual(len(assistant_msgs), 1)
            self.assertEqual(assistant_msgs[0]["status"], "done")
            self.assertEqual(assistant_msgs[0]["content"], "Ок.")
            self.assertNotIn("FAILED_PRECONDITION", assistant_msgs[0]["content"])
            self.assertNotIn(cid, self.main_mod.CHAT_QUEUE_PAUSED)
        finally:
            self.main_mod.CHAT_TURN_QUEUES.pop(cid, None)
            self.main_mod.CHAT_QUEUE_PAUSED.discard(cid)

        # Also verify multi-step turn: completed tool -> short final text ("Готово") without DONE -> suppressed!
        async def _spawn_tool_then_short(*args, **kwargs):
            proc = MagicMock(returncode=1)
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            for ev in (
                {"event": "init", "conversation_id": "conv-tool-short-v138"},
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "step_type": "tool",
                        "state": "DONE",
                        "tool_name": "read_file",
                        "tool_info": {"name": "read_file", "parameters": {"Path": "README.md"}, "output": "ok"},
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 2,
                        "step_type": "agent_response",
                        "state": "IN_PROGRESS",
                        "text_delta": "Готово",
                    },
                },
                {"event": "result", "result": {"status": "ERROR", "error": LOC_ERR}},
            ):
                proc.stdout.feed_data((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=1)
            proc.aclose_lease = AsyncMock()
            return proc

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(binary)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value="http://127.0.0.1:10808")),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_tool_then_short),
        ):
            evts_ts = [
                evt
                async for evt in e.stream_agy_turn(
                    chat_id="chat-tool-short-138",
                    kind="project",
                    project_name="Antigravity_Hub",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Проверь",
                    attachments=[],
                    agy_conversation_id="conv-tool-short-v138",
                    history_messages=[],
                    preferred_agy_account_id=account["id"],
                )
            ]
        self.assertEqual(evts_ts[-1]["type"], "done")
        self.assertEqual(evts_ts[-1]["content"], "Готово")

    async def test_02_startup_and_post_tool_location_error_not_suppressed_and_formats_russian_hint(self):
        e = self.agy_engine
        account = await self._make_agy_account("err136@example.test", "Err136", make_active=True)
        binary = self.tmp_root / "fake_agy_err136.exe"
        binary.write_text("fake", encoding="utf-8")

        # 2a) Empty-text startup failure -> honest `error` + Russian geo-route hint
        async def _spawn_empty(*args, **kwargs):
            proc = MagicMock(returncode=1)
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            for ev in (
                {"event": "init", "conversation_id": "conv-err-empty"},
                {"event": "result", "result": {"status": "ERROR", "error": LOC_ERR}},
            ):
                proc.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=1)
            proc.aclose_lease = AsyncMock()
            return proc

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(binary)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_empty),
        ):
            evts_empty = [
                evt
                async for evt in e.stream_agy_turn(
                    chat_id="chat-err-empty",
                    kind="chat",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Привет",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    preferred_agy_account_id=account["id"],
                )
            ]

        self.assertEqual(evts_empty[-1]["type"], "error")
        self.assertIn(LOC_ERR, evts_empty[-1]["error"])
        self.assertEqual(evts_empty[-1]["content"], "")

        # Verify `--continue` turn with prior text (`>= 200` chars) but `0` fresh text (`not got_any_text_in_proc`) is NOT suppressed!
        prior_long_text = "Предварительный абзац ответа, сохранённый до перезапуска VPN-туннеля. " * 4
        with (
            patch.object(e, "resolve_agy_bin", return_value=str(binary)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_empty),
        ):
            evts_cont_err = [
                evt
                async for evt in e.stream_agy_turn(
                    chat_id="chat-err-continue",
                    kind="chat",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Продолжи",
                    attachments=[],
                    agy_conversation_id="conv-err-empty",
                    history_messages=[],
                    initial_content=prior_long_text,
                    is_continue_turn=True,
                    preferred_agy_account_id=account["id"],
                )
            ]
        self.assertEqual(evts_cont_err[-1]["type"], "error")
        self.assertIn(LOC_ERR, evts_cont_err[-1]["error"])

        # Verify main.py end-to-end Russian hint & queue pause on genuine startup error
        admin_user = await self.database.get_user_by_id("admin")
        chat = await self.database.create_chat(
            owner_id="admin",
            kind="chat",
            title="Geo Error Chat",
            model="gemini-4-carbon-high",
            access_mode="read_only",
            preferred_agy_account_id=account["id"],
        )
        cid = chat["id"]
        self.main_mod.CHAT_TURN_QUEUES[cid] = [{"id": "q2", "chat_id": cid, "prompt": "В очереди"}]
        self.main_mod.CHAT_QUEUE_PAUSED.discard(cid)
        try:
            with (
                patch.object(e, "resolve_agy_bin", return_value=str(binary)),
                patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
                patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_empty),
                patch.object(self.main_mod, "fetch_available_models", AsyncMock(return_value=[{"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"}])),
                patch.object(self.main_mod, "_spawn_telemetry_task", lambda coro: coro.close()),
                patch.object(self.main_mod.ws_manager, "broadcast_for_chat", AsyncMock()),
            ):
                await self.main_mod._run_turn_task_inner_body(
                    chat_id=cid,
                    prompt="Привет",
                    attachments=[],
                    override_model="gemini-4-carbon-high",
                    override_access_mode="read_only",
                    trigger_user=admin_user,
                )
            msgs = await self.database.get_messages(cid)
            assistant_msg = [m for m in msgs if m["role"] == "assistant"][0]
            self.assertEqual(assistant_msg["status"], "error")
            self.assertIn(LOC_ERR, assistant_msg["content"])
            self.assertIn(
                "Регион текущего IP-маршрута не поддерживается Google Gemini API (код 400)",
                assistant_msg["content"],
            )
            self.assertIn("зарубежный узел (США/Европа), а не прямой выход или узел РФ", assistant_msg["content"])
            self.assertIn(cid, self.main_mod.CHAT_QUEUE_PAUSED)
        finally:
            self.main_mod.CHAT_TURN_QUEUES.pop(cid, None)
            self.main_mod.CHAT_QUEUE_PAUSED.discard(cid)

        # 2b) Mid-turn VPN drop after a completed tool (`got_text_since_last_tool == False`) MUST NOT be suppressed
        async def _spawn_post_tool_drop(*args, **kwargs):
            proc = MagicMock(returncode=1)
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            for ev in (
                {"event": "init", "conversation_id": "conv-err-partial"},
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "step_type": "agent_response",
                        "state": "DONE",
                        "text_delta": "Начинаю проверять конфигурацию:",
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 2,
                        "step_type": "tool",
                        "state": "DONE",
                        "tool_name": "read_file",
                        "tool_info": {"name": "read_file", "parameters": {"Path": "config.py"}, "output": "ok"},
                    },
                },
                {"event": "result", "result": {"status": "ERROR", "error": LOC_ERR}},
            ):
                proc.stdout.feed_data((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=1)
            proc.aclose_lease = AsyncMock()
            return proc

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(binary)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_post_tool_drop),
        ):
            evts_partial = [
                evt
                async for evt in e.stream_agy_turn(
                    chat_id="chat-err-partial",
                    kind="chat",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Проверь",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    preferred_agy_account_id=account["id"],
                )
            ]

        self.assertEqual(evts_partial[-1]["type"], "error")
        self.assertEqual(evts_partial[-1]["content"], "Начинаю проверять конфигурацию:")
        self.assertIn(LOC_ERR, evts_partial[-1]["error"])

        # 2c) Active tool at exit MUST NOT be suppressed even if prior text existed
        async def _spawn_active_tool(*args, **kwargs):
            proc = MagicMock(returncode=1)
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            for ev in (
                {"event": "init", "conversation_id": "conv-err-tool"},
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "step_type": "agent_response",
                        "state": "DONE",
                        "text_delta": "Сейчас прочитаю файл.",
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 2,
                        "step_type": "tool",
                        "state": "ACTIVE",
                        "tool_name": "read_file",
                        "tool_info": {"name": "read_file", "parameters": {"Path": "README.md"}},
                    },
                },
                {"event": "result", "result": {"status": "ERROR", "error": LOC_ERR}},
            ):
                proc.stdout.feed_data((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=1)
            proc.aclose_lease = AsyncMock()
            return proc

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(binary)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_active_tool),
        ):
            evts_tool = [
                evt
                async for evt in e.stream_agy_turn(
                    chat_id="chat-err-tool",
                    kind="chat",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Проверь файл",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    preferred_agy_account_id=account["id"],
                )
            ]

        self.assertEqual(evts_tool[-1]["type"], "error")
        self.assertEqual(evts_tool[-1]["tool_calls"][0]["state"], "INTERRUPTED")

    def test_03_ui_strips_trailing_location_error_banner_card_render_and_header_pill_v138(self):
        static_dir = ROOT_DIR / "app" / "static"
        index_html = (static_dir / "index.html").read_text(encoding="utf-8")
        sw_js = (static_dir / "sw.js").read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_sw)
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 138)
        self.assertIn(f'/static/styles.css?v={ver}"', index_html)
        self.assertIn(f'/static/app.js?v={ver}"', index_html)
        self.assertIn(f'"/static/styles.css?v={ver}"', sw_js)
        self.assertIn(f'"/static/app.js?v={ver}"', sw_js)

        chk = subprocess.run(
            ["node", "--check", str(static_dir / "app.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            **hq_iso._subprocess_hidden_kwargs(),
        )
        self.assertEqual(chk.returncode, 0, chk.stderr)

        tree = ast.parse(Path(hq_iso.__file__).read_text(encoding="utf-8"))
        harness = next(
            n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "node_harness" for t in n.targets)
        )
        harness += r"""
const _origCreateEl = sandbox.document.createElement.bind(sandbox.document);
sandbox.document.createElement = (tag) => {
  const n = _origCreateEl(tag);
  if (!n.insertBefore) {
    n.insertBefore = function(ch) { this.children.unshift(ch); return ch; };
  }
  if (!n.remove) n.remove = () => {};
  if (!n.replaceWith) n.replaceWith = () => {};
  return n;
};

const cleanPlan = [
  "### План работ",
  "",
  "---",
  "",
  "1. Проверить `app/agy_engine.py` и `app/main.py`.",
  "2. Запустить `pytest` и убедиться в `UNCHANGED: True`.",
].join("\n");

const locBanner =
  "\n\n---\n⚠️ **Ошибка:** FAILED_PRECONDITION (code 400): User location is not supported for the API use.\n\n💡 *Регион текущего IP-маршрута не поддерживается Google Gemini API (код 400).*";
const poisonedContent = cleanPlan + locBanner;
const poisonedCrlf = poisonedContent.replace(/\n/g, "\r\n");

// 1) Direct stripInterruptBannerFromContent preserves earlier `---` rules & strips trailing error banner (LF + CRLF)
assert.equal(api.stripInterruptBannerFromContent(poisonedContent), cleanPlan);
assert.equal(
  api.stripInterruptBannerFromContent(poisonedCrlf).replace(/\r\n/g, "\n"),
  cleanPlan
);

const assistantMsg = {
  id: 42,
  role: "assistant",
  status: "error",
  model: "gemini-4-carbon-high",
  content: poisonedContent,
  tool_calls: [],
};

// 2) Reply quote, Forward, Handoff, Return to HQ & Pin Note excerpt strip trailing error banner
assert.equal(api.extractCleanMessageTextForReply(assistantMsg), cleanPlan);
assert.equal(api.extractCleanMessageTextForForward(assistantMsg), cleanPlan);
const handoffPrompt = api.buildCodeHandoffPrompt(poisonedContent, "ШТАБ", {
  orReview: true,
  orCouncil: false,
});
assert.ok(handoffPrompt.includes("1. Проверить `app/agy_engine.py`"));
assert.ok(!handoffPrompt.includes("FAILED_PRECONDITION"), "Handoff prompt leaked trailing error banner!");
assert.ok(!handoffPrompt.includes("⚠️ **Ошибка:**"), "Handoff prompt leaked error marker!");
const hqPrompt = api.buildReturnToHqPrompt(assistantMsg, { title: "Ядро", model: "gemini-4-carbon-high" });
assert.ok(!hqPrompt.includes("FAILED_PRECONDITION"));
const pinExcerpt = api.extractPinExcerptFromMessage(assistantMsg, "");
assert.ok(!pinExcerpt.includes("FAILED_PRECONDITION"));

// 3) v138 Card Render: non-empty saved message hides false trailing FAILED_PRECONDITION banner & bogus Continue bar
api.state.activeChatId = "chat-hq-138";
api.state.viewingDesktopConv = null;
api.state.activeChat = {
  id: "chat-hq-138",
  kind: "project",
  project_name: "Antigravity_Hub",
  title: "ШТАБ",
  is_generating: false,
  last_assistant_status: "error",
};
api.state.messages = [assistantMsg];

const renderedNonEmptyHtml = api.buildAssistantInnerHtml(assistantMsg);
assert.ok(renderedNonEmptyHtml.includes("Проверить"), "Answer body must render");
assert.ok(
  !renderedNonEmptyHtml.includes("FAILED_PRECONDITION"),
  "False trailing FAILED_PRECONDITION banner must be hidden when non-empty answer precedes it!"
);
assert.ok(
  !renderedNonEmptyHtml.includes("data-continue-msg"),
  "Completed answer with false sticky error must not render Continue button!"
);
assert.equal(api.isResumableAssistantMessage(assistantMsg, api.state.messages), false);

// 3b) Code-fence protection: example inside ``` block stays intact while real trailing banner is stripped
const docWithFenceExample = [
  "Пример формата ложного хвоста:",
  "```markdown",
  "Текст",
  "",
  "---",
  "⚠️ **Ошибка:** FAILED_PRECONDITION (code 400): User location is not supported for the API use.",
  "```",
  "Конец примера.",
].join("\n");
assert.equal(api.stripFalseTrailingLocationBanner(docWithFenceExample), docWithFenceExample);
assert.equal(
  api.stripFalseTrailingLocationBanner(docWithFenceExample + locBanner),
  docWithFenceExample
);

// 3c) Empty-answer location error (and non-location 503 error / interrupted tool) MUST keep error banner visible!
const emptyAnswerLocErrMsg = {
  id: 43,
  role: "assistant",
  status: "error",
  model: "gemini-4-carbon-high",
  content:
    "⚠️ **Ошибка:** FAILED_PRECONDITION (code 400): User location is not supported for the API use.\n\n💡 *Регион текущего IP-маршрута не поддерживается Google Gemini API (код 400).*",
  tool_calls: [],
};
api.state.messages = [emptyAnswerLocErrMsg];
const renderedEmptyErrHtml = api.buildAssistantInnerHtml(emptyAnswerLocErrMsg);
assert.ok(
  renderedEmptyErrHtml.includes("FAILED_PRECONDITION"),
  "Standalone startup location error without answer text MUST remain visible!"
);
assert.equal(api.isResumableAssistantMessage(emptyAnswerLocErrMsg, api.state.messages), true);

const whitespaceDividerLocMsg = {
  id: 44,
  role: "assistant",
  status: "error",
  model: "gemini-4-carbon-high",
  content: "   \n\n---\n⚠️ **Ошибка:** FAILED_PRECONDITION (code 400): User location is not supported for the API use.",
  tool_calls: [],
};
assert.ok(api.buildAssistantInnerHtml(whitespaceDividerLocMsg).includes("FAILED_PRECONDITION"));

const interruptedToolLocMsg = {
  id: 45,
  role: "assistant",
  status: "error",
  model: "gemini-4-carbon-high",
  content: "Читаю конфиг:" + locBanner,
  tool_calls: [{ step_index: 1, name: "read_file", state: "INTERRUPTED" }],
};
assert.ok(api.buildAssistantInnerHtml(interruptedToolLocMsg).includes("FAILED_PRECONDITION"));

const real503PartialMsg = {
  id: 46,
  role: "assistant",
  status: "error",
  model: "gemini-4-carbon-high",
  content: "Начало ответа\n\n---\n⚠️ **Ошибка:** 503 Service Unavailable",
  tool_calls: [],
};
assert.ok(api.buildAssistantInnerHtml(real503PartialMsg).includes("503 Service Unavailable"));

// 4) v138 Header Status Pill (`syncActiveChatHeaderStatus` / `els.activeChatStatePill`):
const pill = api.els.activeChatStatePill;

// 4a) Genuine error on last turn -> `is-error` (`! Ошибка`)
api.state.messages = [emptyAnswerLocErrMsg];
api.syncActiveChatHeaderStatus();
assert.equal(pill.classList.contains("is-error"), true, "Genuine error must show (!)");
assert.equal(pill.classList.contains("hidden"), false);

// 4b) Saved message with false trailing FAILED_PRECONDITION after non-empty answer -> `is-done` (NOT `is-error`!)
api.state.messages = [assistantMsg];
api.syncActiveChatHeaderStatus();
assert.equal(pill.classList.contains("is-error"), false, "False sticky error must NOT show (!)");
assert.equal(pill.classList.contains("is-done"), true, "Completed answer must show Done");

// 4c) Starting a new user turn after a genuine error IMMEDIATELY clears `(!)` (`is-error`)
api.state.messages = [emptyAnswerLocErrMsg];
api.state.chats = [Object.assign({}, api.state.activeChat, { last_assistant_status: "error" })];
api.state.handoffTargetOutcomes.set("chat-hq-138", { status: "error", ts: Date.now() - 5000 });
api.syncActiveChatHeaderStatus();
assert.equal(pill.classList.contains("is-error"), true);

api.handleWsEvent({
  type: "user_message",
  chat_id: "chat-hq-138",
  message: {
    id: 47,
    role: "user",
    content: "Новый вопрос после ошибки",
    created_at: new Date().toISOString(),
  },
});
assert.equal(
  pill.classList.contains("is-error"),
  false,
  "New user_message must immediately clear stale (!) from previous error turn!"
);

// Optimistic user message also shows `is-running` and never `is-error`
api.state.messages = [
  emptyAnswerLocErrMsg,
  { id: "__opt_1", role: "user", content: "Ещё вопрос", _optimistic: true },
];
api.syncActiveChatHeaderStatus();
assert.equal(pill.classList.contains("is-running"), true);
assert.equal(pill.classList.contains("is-error"), false);

// 4d) `assistant_start` -> `is-running`, then `turn_done` -> `is-done` (never reverting to stale `last_assistant_status: "error"`)
api.handleWsEvent({
  type: "assistant_start",
  chat_id: "chat-hq-138",
  message: { id: 48, role: "assistant", status: "streaming", content: "" },
});
assert.equal(pill.classList.contains("is-running"), true);
assert.equal(pill.classList.contains("is-error"), false);

api.handleWsEvent({
  type: "turn_done",
  chat_id: "chat-hq-138",
  chat: { id: "chat-hq-138", kind: "project", project_name: "Antigravity_Hub", title: "ШТАБ", is_generating: false },
  message: { id: 48, role: "assistant", status: "done", content: "Ок." },
});
assert.equal(pill.classList.contains("is-done"), true, "turn_done must set header pill to is-done");
assert.equal(pill.classList.contains("is-error"), false, "turn_done must not keep is-error");
assert.equal(
  api.state.chats.find((c) => c.id === "chat-hq-138").last_assistant_status,
  "done",
  "Cached chat last_assistant_status must update to done on turn_done"
);

console.log("V136_V138_UI_STRIP_OK");
process.exit(0);
"""
        vm_script = self.tmp_root / "v136_vm.js"
        vm_script.write_text(harness, encoding="utf-8")
        vm_res = subprocess.run(
            ["node", str(vm_script), str(static_dir / "app.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
            **hq_iso._subprocess_hidden_kwargs(),
        )
        self.assertEqual(vm_res.returncode, 0, f"STDOUT:\n{vm_res.stdout}\nSTDERR:\n{vm_res.stderr}")
        self.assertIn("V136_V138_UI_STRIP_OK", vm_res.stdout)
        self._assert_live_db_untouched()


if __name__ == "__main__":
    unittest.main()
