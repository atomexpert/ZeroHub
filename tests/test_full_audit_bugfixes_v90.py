"""v90 regression tests: isolated SQLite, mocked engines, real Node VM, no live server."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update(
    CUDA_VISIBLE_DEVICES="",
    ORT_DISABLE_ALL_GPU="1",
    NVIDIA_VISIBLE_DEVICES="none",
    BROWSER="echo",
    CI="1",
)

from app import agy_engine as agy, claude_engine as claude, codex_engine as codex, config, database as db, main
from tests.test_chatgpt_codex_integration import (
    MOCK_CODEX_CLI_SCRIPT,
    _MiniAsgiClient,
    _make_auth_json_str,
    _unlink_test_junctions,
)

MOCK_CODEX_MULTI_MSG_SCRIPT = r'''# -*- coding: utf-8 -*-
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method", "")
    if method == "initialize":
        send({"id": mid, "result": {"userAgent": "codex-mock/0.118.0"}})
    elif method == "thread/start":
        send({"id": mid, "result": {"thread": {"id": "thr_multi_msg_001"}}})
    elif method == "turn/start":
        send({"id": mid, "result": {"turn": {"id": "turn_001"}}})
        send({"method": "thread/started", "params": {"thread": {"id": "thr_multi_msg_001"}}})
        send({"method": "turn/started", "params": {"turn": {"id": "turn_001"}}})
        send({"method": "item/started", "params": {"item": {"id": "msg_1", "type": "agentMessage"}}})
        send({"method": "item/agentMessage/delta", "params": {"itemId": "msg_1", "delta": "Первая мысль."}})
        send({"method": "item/completed", "params": {"item": {"id": "msg_1", "type": "agentMessage", "text": "Первая мысль."}}})
        send({"method": "item/started", "params": {"item": {"id": "cmd_1", "type": "commandExecution", "command": "pytest -q"}}})
        send({"method": "item/completed", "params": {"item": {"id": "cmd_1", "type": "commandExecution", "command": "pytest -q", "aggregatedOutput": "1 passed", "exitCode": 0}}})
        send({"method": "item/started", "params": {"item": {"id": "msg_2", "type": "agentMessage"}}})
        send({"method": "item/agentMessage/delta", "params": {"itemId": "msg_2", "delta": "Вторая мысль после тула."}})
        send({"method": "item/completed", "params": {"item": {"id": "msg_2", "type": "agentMessage", "text": "Вторая мысль после тула."}}})
        send({"method": "item/completed", "params": {"item": {"id": "msg_3", "type": "agentMessage", "text": "Третья мысль без дельт."}}})
        send({"method": "turn/completed", "params": {"turn": {"id": "turn_001", "status": "completed"}}})
        sys.exit(0)
'''


class TestFullAuditV90(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.live = ROOT / "data/hub.db"
        self.stat = (self.live.stat().st_mtime_ns, self.live.stat().st_size)
        self.tmp = tempfile.TemporaryDirectory(prefix="hub_v90_", ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        for module in (config, db, agy, claude, codex):
            for attr in (
                "DB_PATH",
                "CLI_HOMES_DIR",
                "AGY_CLI_DATA_DIR",
                "UPLOADS_DIR",
                "CHAT_SANDBOX_DIR",
                "USER_WORKSPACES_DIR",
                "SHARED_PROJECTS_DIR",
                "PROJECTS_ROOT",
            ):
                if hasattr(module, attr):
                    target = self.root / ("hub.db" if attr == "DB_PATH" else attr.lower())
                    if attr != "DB_PATH":
                        target.mkdir(exist_ok=True)
                    self.stack.enter_context(patch.object(module, attr, target))
        self.stack.enter_context(
            patch.object(
                main,
                "fetch_available_models",
                AsyncMock(return_value=[{"id": "gemini-4-carbon-medium"}]),
            )
        )
        self.stack.enter_context(patch.object(main, "_refresh_and_broadcast_quotas", AsyncMock()))
        self.stack.enter_context(patch.object(main, "_broadcast_chatgpt_state_to_user", AsyncMock()))
        self.stack.enter_context(patch.object(main, "_broadcast_claude_state_to_user", AsyncMock()))
        self.real_cli_info = {
            "installed": True,
            "path": "D:/secret/host/claude.cmd",
            "cli_js": "D:/secret/host/node_modules/@anthropic-ai/claude-code/cli.js",
            "node_bin": "C:/Program Files/nodejs/node.exe",
            "source": "bundled",
            "version": "2.1.104",
            "npm_available": True,
        }
        self.stack.enter_context(
            patch.object(config, "get_claude_cli_info", side_effect=lambda: dict(self.real_cli_info))
        )
        await db.init_db()
        self.admin = await db.get_user_by_id("admin")
        self.guest = await db.create_user(
            username="v90_guest",
            password="test-password",
            display_name="Guest",
            allowed_models=[" __no_opus__ "],
            daily_token_limit=1,
        )

    async def asyncTearDown(self):
        tasks = list(main.ACTIVE_TURN_TASKS.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        main.ACTIVE_TURN_TASKS.clear()
        main.CHAT_TURN_QUEUES.clear()
        main.CHAT_QUEUE_PAUSED.clear()
        main.COMPACTING_CHATS.clear()
        agy.ACTIVE_RUNS.clear()
        self.stack.close()
        _unlink_test_junctions(self.root)
        self.tmp.cleanup()
        if os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") != "1":
            self.assertEqual(self.stat, (self.live.stat().st_mtime_ns, self.live.stat().st_size))

    async def cg(self, uid="admin", email="one@example.test"):
        return await db.upsert_user_chatgpt_account(
            uid, raw_auth_json=_make_auth_json_str(email, email, "plus"), email=email
        )

    async def cc(self, uid="admin", **kwargs):
        return await db.upsert_user_claude_account(
            uid,
            raw_credentials_json=json.dumps(
                {"claudeAiOauth": {"accessToken": "mock", "refreshToken": "mock"}}
            ),
            **kwargs,
        )

    async def turn(self, chat, stream, user=None):
        with patch.object(main, "stream_codex_turn", side_effect=stream):
            await main._run_turn_task_inner(
                chat_id=chat["id"],
                prompt="continue",
                attachments=[],
                override_model=chat["model"],
                override_access_mode=None,
                trigger_user=user or self.admin,
            )

    async def test_intermediate_codex_session_survives_interrupt_and_explicit_reset(self):
        acc = await self.cg()
        chat = await db.create_chat(model="codex:gpt-5.4-mini@low")

        async def stream(**kwargs):
            yield {"type": "codex_session", "thread_id": "thread-mid", "account_id": acc["id"]}
            saved = await db.get_chat(chat["id"])
            self.assertEqual(
                (saved["codex_thread_id"], saved["codex_account_id"]), ("thread-mid", acc["id"])
            )
            yield {"type": "codex_session"}  # malformed/empty event cannot erase a known thread
            yield {"type": "cancelled", "content": "partial", "thinking": "", "tool_calls": []}

        await self.turn(chat, stream)
        self.assertEqual((await db.get_chat(chat["id"]))["codex_thread_id"], "thread-mid")
        pushed = AsyncMock()

        async def failover(**kwargs):
            yield {
                "type": "codex_session",
                "codex_thread_id": "",
                "codex_account_id": "next",
                "reset_session": True,
            }
            yield {
                "type": "chatgpt_failover",
                "from_label": "one",
                "to_label": "two",
                "to_account_id": "next",
            }
            yield {"type": "cancelled", "content": "", "thinking": "", "tool_calls": []}

        with patch.object(main.ws_manager, "broadcast_user", pushed):
            await self.turn(chat, failover)
        saved = await db.get_chat(chat["id"])
        self.assertEqual((saved["codex_thread_id"], saved["codex_account_id"]), ("", "next"))
        self.assertTrue(
            any(c.args[1].get("type") == "chatgpt_failover" for c in pushed.call_args_list)
        )

    async def test_real_codex_protocol_session_aliases_failover_and_stitch(self):
        first = await self.cg(email="first@example.test")
        second = await self.cg(email="second@example.test")
        await db.activate_user_chatgpt_account("admin", first["id"])
        mock = self.root / "mock_codex.py"
        mock.write_text(MOCK_CODEX_CLI_SCRIPT, encoding="utf-8")
        with patch.dict(
            os.environ, {"HUB_CODEX_BIN": str(mock), "MOCK_EXHAUSTED_ACC_ID": "first@example.test"}
        ):
            evts = [
                e
                async for e in codex.stream_codex_turn(
                    chat_id="mock-turn",
                    kind="chat",
                    project_name="",
                    model="codex:gpt-5.4-mini@low",
                    access_mode="read_only",
                    prompt="hi",
                    attachments=[],
                    user=self.admin,
                    codex_thread_id="",
                    codex_account_id="",
                    history_messages=[],
                    initial_content="Предыдущий текст",
                    is_continue_turn=True,
                )
            ]
        sessions = [e for e in evts if e["type"] == "codex_session"]
        self.assertTrue(sessions, evts)
        for e in sessions:
            self.assertEqual(e["thread_id"], e["codex_thread_id"])
            self.assertEqual(e["account_id"], e["codex_account_id"])
            if e["codex_account_id"] == second["id"] and e["codex_thread_id"]:
                break
        self.assertTrue(any(e.get("reset_session") and not e["codex_thread_id"] for e in sessions), evts)
        text = [e for e in evts if e["type"] == "text_delta"]
        self.assertTrue(text, evts)
        self.assertTrue(text[0]["delta"].startswith((" ", "\n")))

    async def test_codex_multi_agent_message_and_post_tool_paragraph_separation(self):
        await self.cg(email="multi@example.test")
        mock = self.root / "mock_codex_multi.py"
        mock.write_text(MOCK_CODEX_MULTI_MSG_SCRIPT, encoding="utf-8")
        with patch.dict(os.environ, {"HUB_CODEX_BIN": str(mock)}):
            evts = [
                e
                async for e in codex.stream_codex_turn(
                    chat_id="mock-multi",
                    kind="chat",
                    project_name="",
                    model="codex:gpt-5.4-mini@low",
                    access_mode="read_only",
                    prompt="check",
                    attachments=[],
                    user=self.admin,
                    codex_thread_id="",
                    codex_account_id="",
                    history_messages=[],
                )
            ]
        done = next(e for e in evts if e["type"] == "done")
        self.assertEqual(
            done["content"],
            "Первая мысль.\n\nВторая мысль после тула.\n\nТретья мысль без дельт.",
        )
        self.assertEqual(len(done["tool_calls"]), 1)
        self.assertEqual(done["tool_calls"][0]["step_index"], 2)

    async def test_delete_accounts_wipes_disk_and_chat_bindings(self):
        uid = self.guest["id"]
        cc = await self.cc(uid)
        directory = config.ensure_user_claude_account_dir(uid, cc["id"])
        (directory / ".credentials.json").write_text("SECRET", encoding="utf-8")
        del_payload = await main.api_delete_claude_account(cc["id"], self.guest)
        self.assertFalse(directory.exists())
        self.assertEqual(del_payload["cli_info"]["path"], "")
        self.assertEqual(del_payload["cli_info"]["cli_js"], "")
        self.assertEqual(del_payload["cli_info"]["node_bin"], "")
        cg = await self.cg(uid)
        own = await db.create_chat(owner_id=uid)
        other = await db.create_chat()
        for c in (own, other):
            await db.update_chat(c["id"], codex_thread_id="thread", codex_account_id=cg["id"])
        await db.delete_user_chatgpt_account(uid, cg["id"])
        own = await db.get_chat(own["id"])
        self.assertEqual((own["codex_thread_id"], own["codex_account_id"]), ("", ""))
        self.assertEqual((await db.get_chat(other["id"]))["codex_thread_id"], "thread")

    async def test_claude_rbac_stale_quota_setup_token_and_personal_limits(self):
        uid = self.guest["id"]
        acc = await self.cc(
            uid,
            quotas={"updated_at": (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()},
        )
        with patch.object(claude, "refresh_claude_account_quotas", AsyncMock()) as refresh:
            payload = await main.api_get_claude_state(False, self.guest)
            refresh.assert_awaited_once_with(uid, acc["id"])
        # Guest must NOT see real host filesystem paths under any key of get_claude_cli_info()
        for secret_key in ("path", "cli_js", "node_bin", "bin_path"):
            self.assertEqual(payload["cli_info"][secret_key], "")
        self.assertTrue(payload["cli_info"]["installed"])
        self.assertEqual(payload["cli_info"]["version"], "2.1.104")
        # Also verify account mutation endpoints keep paths masked for non-admin
        patched = await main.api_patch_claude_account(
            acc["id"], main.PatchClaudeAccountRequest(label="Work"), self.guest
        )
        self.assertEqual(patched["cli_info"]["path"], "")
        self.assertEqual(patched["cli_info"]["cli_js"], "")
        # Admin MUST still receive full host CLI diagnostics
        admin_payload = await main.api_get_claude_state(False, self.admin)
        self.assertEqual(admin_payload["cli_info"]["path"], self.real_cli_info["path"])
        self.assertEqual(admin_payload["cli_info"]["cli_js"], self.real_cli_info["cli_js"])
        # Whitespace-padded " __no_opus__ " must strip Opus from guest models and block execution
        self.assertFalse(any("opus" in m["id"] for m in payload["models"]))
        with patch.object(db, "get_user_24h_tokens", AsyncMock(return_value=10000)):
            self.assertIsNone(
                await main._enforce_user_turn_quotas(self.guest, "claude-code:sonnet@high")
            )
            self.assertIsNotNone(
                await main._enforce_user_turn_quotas(self.guest, "claude-code:opus@high")
            )
        with (
            patch.object(
                db,
                "get_active_claude_account",
                AsyncMock(return_value={**acc, "auth_method": "setup_token"}),
            ),
            patch.object(claude, "refresh_claude_account_quotas", AsyncMock()) as refresh,
        ):
            await main.api_get_claude_state(False, self.guest)
            refresh.assert_not_awaited()

    async def test_shared_tokens_24h_excludes_personal_providers(self):
        chat = await db.create_chat(owner_id=self.guest["id"])
        for model, provider in [
            ("gemini-test", "agy"),
            ("codex:test", ""),
            ("claude-code:test", ""),
            ("legacy", "chatgpt_codex"),
            ("legacy2", "claude_code"),
        ]:
            await db.add_message(
                chat_id=chat["id"],
                role="assistant",
                content="ok",
                model=model,
                user_id=self.guest["id"],
                usage={"total_tokens": 100, "input_tokens": 100, "provider": provider},
            )
        stats = await db.get_token_usage_stats(include_by_user=True)
        row = next(r for r in stats["by_user"] if r["user_id"] == self.guest["id"])
        self.assertEqual((row["tokens_24h"], row["shared_tokens_24h"]), (500, 100))

    async def test_compaction_cross_engine_fallback_and_agy_family_bucketing(self):
        await self.cc(self.guest["id"])

        async def compact(**kwargs):
            if kwargs["model"].startswith("codex:"):
                raise RuntimeError("429")
            return {"summary": "сводка", "model": kwargs["model"]}

        with (
            patch.object(main, "compact_chat_context", side_effect=compact) as run,
            patch.object(db, "get_user_24h_tokens", AsyncMock(return_value=100)),
        ):
            result = await main._compact_with_fallback(
                chat={},
                messages=[],
                primary_model="codex:test",
                fallback_models=["gemini-test", "claude-code:sonnet@low"],
                user=self.guest,
            )
        self.assertEqual(result["model"], "claude-code:sonnet@low")
        self.assertEqual(run.await_count, 1)  # missing Codex + exhausted shared quota skipped

        await self.cg(self.guest["id"])
        with patch.object(main, "compact_chat_context", side_effect=compact) as run:
            result = await main._compact_with_fallback(
                chat={},
                messages=[],
                primary_model="codex:test",
                fallback_models=["claude-code:sonnet@low"],
                user=self.guest,
            )
        self.assertEqual(run.await_count, 2)

        # Agy family bucketing: failure on Agy Claude must NOT skip Agy Gemini Flash,
        # while a second Agy Claude candidate IS skipped.
        seen_models = []

        async def compact_agy_family(**kwargs):
            m = kwargs["model"]
            seen_models.append(m)
            if "claude" in m:
                raise RuntimeError("529 Claude overloaded")
            return {"summary": "сводка gemini", "model": m}

        with (
            patch.object(main, "compact_chat_context", side_effect=compact_agy_family),
            patch.object(main, "_enforce_user_turn_quotas", AsyncMock(return_value=None)),
        ):
            res_agy = await main._compact_with_fallback(
                chat={},
                messages=[],
                primary_model="claude-sonnet-4-6-thinking",
                fallback_models=["claude-haiku-4-5", "gemini-3-flash"],
                user=self.admin,
            )
        self.assertEqual(res_agy["model"], "gemini-3-flash")
        self.assertEqual(seen_models, ["claude-sonnet-4-6-thinking", "gemini-3-flash"])

        with patch.object(main, "compact_chat_context", AsyncMock(return_value={"summary": ""})):
            with self.assertRaises(main.HTTPException) as caught:
                await main._compact_with_fallback(
                    chat={},
                    messages=[],
                    primary_model="claude-code:sonnet@low",
                    fallback_models=[],
                    user=self.guest,
                )
        self.assertEqual(caught.exception.status_code, 503)

    async def test_execute_compaction_sorts_gemini_flash_before_other_models(self):
        chat = await db.create_chat(model="claude-sonnet-4-6-thinking")
        await db.add_message(chat_id=chat["id"], role="user", content="history")
        captured_fallbacks = []

        async def fake_compact_with_fallback(**kwargs):
            captured_fallbacks.extend(kwargs["fallback_models"])
            return {"summary": "ok", "model": "gemini-3-flash"}

        cached_models = [
            {"id": "gemini-4-argon-low"},
            {"id": "gemini-4-carbon-medium"},
            {"id": "gemini-3-flash"},
        ]
        with (
            patch.object(
                main, "compute_chat_context_stats", return_value={"can_compact": True, "used_tokens": 500}
            ),
            patch.object(main, "fetch_available_models", AsyncMock(return_value=cached_models)),
            patch.object(main, "_compact_with_fallback", side_effect=fake_compact_with_fallback),
        ):
            await main._execute_chat_compaction(chat["id"], self.admin)
        self.assertEqual(
            captured_fallbacks[:3],
            ["gemini-3-flash", "gemini-4-argon-low", "gemini-4-carbon-medium"],
        )

    async def test_continue_turn_attachment_handling_across_resumed_and_fresh_sessions(self):
        cc_acc = await self.cc()
        first_cg = await self.cg(email="att1@example.test")
        await self.cg(email="att2@example.test")
        await db.activate_user_chatgpt_account("admin", first_cg["id"])
        img_path = self.root / "diagram.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        att = [{"name": "diagram.png", "path": str(img_path), "mime_type": "image/png"}]

        # 1) Even if chat has a stale agy_conversation_id, switching/continuing on Claude or Codex
        # passes attachments into the engine so fresh/failover attempts never lose them.
        chat_resumed = await db.create_chat(model="claude-code:opus@high")
        await db.update_chat(
            chat_resumed["id"],
            agy_conversation_id="stale_agy_conv",
            claude_session_id="sess_existing_1",
            claude_account_id=cc_acc["id"],
            last_engine="claude",
        )
        await db.add_message(
            chat_id=chat_resumed["id"], role="user", content="analyze image", attachments=att
        )
        partial_msg = await db.add_message(
            chat_id=chat_resumed["id"],
            role="assistant",
            content="partial answer",
            model="claude-code:opus@high",
            status="cancelled",
        )
        claude_kwargs_seen = []

        async def mock_claude_stream(**kwargs):
            claude_kwargs_seen.append(
                (kwargs.get("claude_session_id"), list(kwargs.get("attachments") or []))
            )
            yield {"type": "cancelled", "content": "partial answer", "thinking": "", "tool_calls": []}

        with patch.object(main, "stream_claude_turn", side_effect=mock_claude_stream):
            await main._run_turn_task_inner(
                chat_id=chat_resumed["id"],
                prompt="Продолжай",
                attachments=[],
                override_model="claude-code:sonnet@high",
                override_access_mode=None,
                trigger_user=self.admin,
                resume_message_id=int(partial_msg["id"]),
            )
        self.assertEqual(claude_kwargs_seen[0][0], "sess_existing_1")
        self.assertEqual(len(claude_kwargs_seen[0][1]), 1)

        # 2) Inside stream_codex_turn on continue_turn: resumed thread omits duplicate attachments,
        # while 429 failover to fresh thread on Account #2 re-attaches them automatically!
        mock = self.root / "mock_codex_att.py"
        mock.write_text(MOCK_CODEX_CLI_SCRIPT, encoding="utf-8")
        seen_input_atts = []
        orig_build_items = codex._build_turn_input_items

        def spy_build_items(prompt_text, atts):
            seen_input_atts.append([a.get("name") for a in (atts or [])])
            return orig_build_items(prompt_text, atts)

        with (
            patch.dict(
                os.environ,
                {"HUB_CODEX_BIN": str(mock), "MOCK_EXHAUSTED_ACC_ID": "att1@example.test"},
            ),
            patch.object(codex, "_build_turn_input_items", side_effect=spy_build_items),
        ):
            _ = [
                e
                async for e in codex.stream_codex_turn(
                    chat_id=chat_resumed["id"],
                    kind="chat",
                    project_name="",
                    model="codex:gpt-5.4-mini@low",
                    access_mode="read_only",
                    prompt="Продолжай",
                    attachments=att,
                    user=self.admin,
                    codex_thread_id="thr_existing_1",
                    codex_account_id=first_cg["id"],
                    history_messages=[],
                    initial_content="partial answer",
                    is_continue_turn=True,
                )
            ]
        # Attempt 1 (resumed thread thr_existing_1) -> [], Attempt 2 (failover fresh thread) -> ['diagram.png']
        self.assertEqual(seen_input_atts, [[], ["diagram.png"]])

    async def test_queue_compact_then_compact_then_voice_preserves_state(self):
        chat = await db.create_chat()
        await db.add_message(chat_id=chat["id"], role="user", content="long history")
        voice = {
            "kind": "message",
            "user_id": "admin",
            "prompt": "voice",
            "live_voice": True,
            "live_session_id": "live",
            "tts_voice": "voice-name",
            "tts_rate": "+10%",
        }
        main.CHAT_TURN_QUEUES[chat["id"]] = [{"kind": "compact", "user_id": "admin"}, voice]
        with (
            patch.object(
                main,
                "compute_chat_context_stats",
                return_value={"can_compact": True, "used_tokens": 100},
            ),
            patch.object(
                main,
                "_compact_with_fallback",
                AsyncMock(return_value={"summary": "summary", "model": "claude-code:haiku@low"}),
            ),
            patch.object(main, "_run_turn_task_inner", AsyncMock()) as run,
        ):
            compacted = await main._execute_chat_compaction(chat["id"], self.admin)
            self.assertEqual(compacted["message"]["model"], "claude-code:haiku@low")
            task = main.ACTIVE_TURN_TASKS[chat["id"]]
            self.assertNotIn(chat["id"], agy.ACTIVE_RUNS)
            await task
            self.assertEqual(run.await_args.kwargs["live_voice"], True)
            self.assertEqual(run.await_args.kwargs["tts_voice"], "voice-name")
            self.assertEqual(run.await_args.kwargs["live_session_id"], "live")
            self.assertEqual(run.await_args.kwargs["tts_rate"], "+10%")
        self.assertNotIn(chat["id"], agy.ACTIVE_RUNS)
        self.assertNotIn(chat["id"], main.ACTIVE_TURN_TASKS)

    async def test_watch_sync_enriches_admin_personal_models_both_branches(self):
        await self.cg()
        await self.cc()
        pushed = AsyncMock()
        ticks = 0

        async def sleep(_):
            nonlocal ticks
            ticks += 1
            if ticks > 2:
                raise asyncio.CancelledError

        clock = iter([0, 1900, 1901])
        with (
            patch.object(main.asyncio, "sleep", side_effect=sleep),
            patch.object(main, "time", SimpleNamespace(monotonic=lambda: next(clock))),
            patch.object(main, "_token_file_signature", return_value=(False, 0, 0)),
            patch.object(main, "get_cli_auth_status", return_value={"ok": False}),
            patch.object(main, "get_cached_models", return_value=[]),
            patch.object(
                main,
                "probe_cli_auth_and_refresh_models",
                AsyncMock(return_value=(True, [{"id": "gemini-test"}], "")),
            ),
            patch.object(main, "fetch_account_quotas", AsyncMock(return_value={})),
            patch.object(main.ws_manager, "broadcast_admins", pushed),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main._watch_account_sync_loop()
        self.assertEqual(pushed.await_count, 2)
        for call in pushed.await_args_list:
            ids = [m["id"] for m in call.args[0]["models"]]
            self.assertTrue(any(m.startswith("codex:") for m in ids), ids)
            self.assertTrue(any(m.startswith("claude-code:") for m in ids), ids)

    async def test_admin_api_media_denylist_and_private_chat_access(self):
        from app import auth

        client = _MiniAsgiClient(main.app)
        headers = {
            "Authorization": f"Bearer {auth.create_user_session_token(user_id=self.guest['id'], session_version=1)}"
        }
        for route in main.app.routes:
            if not route.path.startswith("/api/admin/"):
                continue
            path = route.path.replace("{user_id}", "admin").replace("{sp_id}", "unknown")
            response = await client.request(
                next(iter(route.methods)), path, headers=headers, json_body={}
            )
            self.assertEqual(response.status_code, 403, (path, response.text))

        # Sensitive credential files inside uploads must still be denied by /api/media
        uploads = self.root / "uploads_dir"
        for secret_name in (".credentials.json", "auth.json", ".env"):
            secret_file = uploads / secret_name
            secret_file.write_text("SECRET", encoding="utf-8")
            resp = await client.request(
                "GET", f"/api/media?path={secret_file.as_posix()}", headers=headers
            )
            self.assertEqual(resp.status_code, 403, (secret_name, resp.text))

        foreign = await db.create_chat(kind="project", project_name="private")
        allowed, _ = await main._get_chat_access_context(foreign, self.guest)
        self.assertFalse(allowed)
        tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
        inner = next(
            n
            for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_turn_task_inner"
        )
        loop = next(
            n for n in ast.walk(inner) if isinstance(n, ast.For) and ast.unparse(n.target) == "ref_id"
        )
        body = ast.unparse(loop)
        self.assertLess(body.index("_get_chat_access_context"), body.index("build_referenced_chat_digest"))

    async def test_private_reference_never_reaches_engine_context(self):
        project = self.root / "guest-project"
        project.mkdir()
        uid = self.guest["id"]
        own = await db.create_chat(
            owner_id=uid, kind="project", project_name="audit", project_path=str(project)
        )
        foreign = await db.create_chat(kind="project", project_name="audit", project_path=str(project))
        await db.add_message(chat_id=foreign["id"], role="assistant", content="PRIVATE-SECRET")
        seen = []

        async def stream(**kwargs):
            seen.append(kwargs.get("referenced_chat_blocks"))
            yield {"type": "cancelled", "content": "", "thinking": "", "tool_calls": []}

        with (
            patch.object(
                main.brain_reader, "resolve_user_project", return_value={"name": "audit", "path": project}
            ),
            patch.object(main, "_enforce_user_turn_quotas", AsyncMock(return_value=None)),
            patch.object(main, "stream_agy_turn", side_effect=stream),
            patch.object(db, "build_referenced_chat_digest", AsyncMock()) as digest,
        ):
            await main._run_turn_task_inner(
                chat_id=own["id"],
                prompt="hi",
                attachments=[],
                override_model=None,
                override_access_mode=None,
                trigger_user=self.guest,
                referenced_chat_ids=[foreign["id"]],
            )
        digest.assert_not_awaited()
        self.assertEqual(seen, [[]])

    async def test_run_queued_now_tracks_task_before_execution(self):
        from tests.test_handoff_compact_pipeline import _DummyWS

        chat = await db.create_chat()
        cid = chat["id"]
        main.CHAT_TURN_QUEUES[cid] = [{"id": "compact-now", "kind": "compact", "user_id": "admin"}]
        entered = asyncio.Event()
        release = asyncio.Event()

        async def compact(*args, **kwargs):
            entered.set()
            await release.wait()

        ws = _DummyWS([{"action": "run_queued_now", "chat_id": cid, "queue_id": "compact-now"}])
        with (
            patch.object(main.auth, "authenticate_ws_user", AsyncMock(return_value=self.admin)),
            patch.object(main, "_execute_chat_compaction", side_effect=compact),
        ):
            await main.websocket_endpoint(ws)
            task = main.ACTIVE_TURN_TASKS[cid]
            await entered.wait()
            self.assertNotIn(cid, agy.ACTIVE_RUNS)
            release.set()
            await task
        self.assertNotIn(cid, main.ACTIVE_TURN_TASKS)

    async def test_quota_refresh_pushes_both_personal_summaries(self):
        with (
            patch.object(main, "fetch_account_quotas", AsyncMock(return_value={})),
            patch.object(claude, "refresh_active_claude_quotas", AsyncMock()),
            patch.object(main.ws_manager, "broadcast_user", AsyncMock()) as pushed,
        ):
            await main.api_get_quotas(True, None, self.guest)
        types = {c.args[1].get("type") for c in pushed.await_args_list if c.args[0] == self.guest["id"]}
        self.assertTrue({"claude_accounts_updated", "chatgpt_accounts_updated"}.issubset(types))

    def test_resume_stitch_for_both_stream_contracts(self):
        cases = [
            ("текст", "продолжение", " "),
            ("word", "word", " "),
            ("ok", "## Header", "\n\n"),
            ("ok", "> quote", "\n\n"),
            ("ok", "1. item", "\n\n"),
            ("ok", "| col |", "\n\n"),
            ("| a |", "| b |", "\n"),
            ("ok", ", next", ""),
            ("ok ", "next", ""),
            ("ok", "\nnext", ""),
            ("", "next", ""),
            ("ok", "", ""),
        ]
        for existing, delta, prefix in cases:
            self.assertEqual(agy.compute_resume_stitch_prefix(existing, delta), prefix, (existing, delta))
            parser = claude.ClaudeStreamParser(initial_text=existing)
            out = []
            parser._add_text(delta, out)
            if delta:
                self.assertEqual(out[-1]["delta"], prefix + delta)
                self.assertEqual(parser.text, existing + prefix + delta)
                parser._add_text("tail", out)
                self.assertTrue(parser.text.endswith(delta + "tail"))

    def test_export_idea_and_is_within_dir_reject_symlink_escape(self):
        from app import brain_reader

        project = self.root / "export-project"
        project.mkdir()
        outside = self.root / "private.md"
        outside.write_text("PRIVATE", encoding="utf-8")
        resolve = Path.resolve

        def resolved(path, *args, **kwargs):
            if path == project / "PLAN.md":
                return outside
            return resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolved):
            self.assertFalse(main._is_within_dir(project / "PLAN.md", project))
            with self.assertRaises(ValueError):
                brain_reader.export_idea_to_project(
                    project_name="audit",
                    target_file="PLAN.md",
                    idea_title="test",
                    content="overwrite",
                    resolved_dir=project,
                )
        self.assertEqual(outside.read_text(encoding="utf-8"), "PRIVATE")

    def test_frontend_vm_sw_and_static_invariants(self):
        for f in ("app.js", "index.html", "styles.css", "sw.js"):
            data = (ROOT / "app/static" / f).read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
            data.decode("utf-8")
        css = (ROOT / "app/static/styles.css").read_text(encoding="utf-8")
        sw = (ROOT / "app/static/sw.js").read_text(encoding="utf-8")
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        m_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw)
        self.assertIsNotNone(m_cache)
        ver = int(m_cache.group(1))
        self.assertGreaterEqual(ver, 90)
        for text in (sw, html):
            self.assertIn(f"/static/app.js?v={ver}", text)
            self.assertIn(f"/static/styles.css?v={ver}", text)
        for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if re.search(
                r"(?:main\.workspace|(?<![\w-])\.workspace)(?=[\s,:.#\[]|$)", selector.strip()
            ) and not re.search(r"\.workspace\s+[.#a-z]", selector):
                self.assertFalse(
                    re.search(
                        r"(?m)^\s*(?:transform|filter|backdrop-filter|perspective|contain|container-type)\s*:\s*(?!none)",
                        block,
                    ),
                    selector,
                )
        for js_file in ("app.js", "sw.js"):
            check = subprocess.run(
                ["node", "--check", str(ROOT / "app/static" / js_file)],
                capture_output=True,
                text=True,
                **config.subprocess_hidden_kwargs(),
            )
            self.assertEqual(check.returncode, 0, check.stderr)

        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const app = fs.readFileSync(process.argv[2], 'utf8');
const sw = fs.readFileSync(process.argv[3], 'utf8');

function extractFn(src, name) {
  const re = new RegExp('^  (?:async\\s+)?function\\s+' + name + '\\s*\\(', 'm');
  const m = re.exec(src);
  assert(m, 'Function not found: ' + name);
  let i = m.index;
  let parenDepth = 0, depth = 0, started = false, inStr = '', escaped = false;
  for (; i < src.length; i++) {
    const ch = src[i];
    if (inStr) {
      if (escaped) { escaped = false; continue; }
      if (ch === '\\') { escaped = true; continue; }
      if (ch === inStr) inStr = '';
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') { inStr = ch; continue; }
    if (!started) {
      if (ch === '(') { parenDepth++; continue; }
      if (ch === ')') { parenDepth = Math.max(0, parenDepth - 1); continue; }
      if (ch === '{' && parenDepth === 0) { depth = 1; started = true; }
      continue;
    }
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(m.index, i + 1);
    }
  }
  throw new Error('Unbalanced braces in ' + name);
}

function makeEl(initVal = '') {
  const classes = new Set(['hidden']);
  return {
    value: initVal,
    innerHTML: '',
    textContent: '',
    disabled: false,
    checked: false,
    href: '',
    classList: {
      add: (...c) => c.forEach((x) => classes.add(x)),
      remove: (...c) => c.forEach((x) => classes.delete(x)),
      toggle: (x, force) => {
        const next = force === undefined ? !classes.has(x) : Boolean(force);
        if (next) classes.add(x); else classes.delete(x);
      },
      contains: (x) => classes.has(x),
    },
  };
}

const dom = {
  claudeModal: makeEl(),
  claudeFlowCard: makeEl(),
  claudeStartOAuthBtn: makeEl(),
  claudeFlowOpenLink: makeEl(),
  claudeFlowStatus: makeEl(),
  claudeFlowTimer: makeEl(),
  claudeImportTextarea: makeEl('sk-ant-secret'),
  claudeFlowCodeInput: makeEl('secret-code'),
  claudeNewLabelInput: makeEl('Work'),
  claudeAccountsList: Object.assign(makeEl(), { innerHTML: '<div>stale-claude-acc</div>' }),
};

let timerSeq = 1;
const activeIntervals = new Set();
const ctx = {
  Date, Math, Number, String, Boolean, Array, Object, Set, Map, Promise,
  setInterval: (fn) => { const id = timerSeq++; activeIntervals.add(id); return id; },
  clearInterval: (id) => { activeIntervals.delete(id); },
  escapeHtml: (s) => String(s ?? '').replace(/[&<>"']/g, '_'),
  formatCompactTokens: (n) => String(Number(n) || 0),
  formatFullTokens: (n) => String(Number(n) || 0),
  claudeEl: (id) => dom[id] || null,
  isAdminUser: () => true,
  showToast: () => {},
  loadClaudeState: async () => {},
  renderChatGptModal: () => {},
  renderChatGptEntryBadges: () => {},
  renderClaudeModalCalled: 0,
  renderClaudeModal: () => { ctx.renderClaudeModalCalled++; dom.claudeAccountsList.innerHTML = ''; },
  renderClaudeEntryBadges: () => {},
  renderOpenRouterEntryBadges: () => {},
  stopChatGptDevicePolling: () => {},
  state: {
    currentUser: { id: 'admin', role: 'admin' },
    quotas: { groups: [] },
    tokenStats: {
      window_24h: {}, window_7d: {}, all_time: {},
      by_user: [
        { user_id: 'u1', username: 'guest1', role: 'user', turns: 5, tokens_24h: 500, shared_tokens_24h: 100, daily_token_limit: 1000, total_tokens: 900 },
      ],
    },
    adminOverview: {
      users: [
        { id: 'u1', username: 'guest1', role: 'user', is_active: true, access_mode: 'read_only', allowed_models: '*', daily_token_limit: 1000, tokens_24h: 500, shared_tokens_24h: 100, total_tokens: 900 },
        { id: 'u2', username: 'guest2', role: 'user', is_active: true, access_mode: 'read_only', allowed_models: '*', daily_token_limit: 1000, tokens_24h: 80, shared_tokens_24h: 80, total_tokens: 80 },
      ],
    },
    chatgpt: {
      accounts: [{ id: 'a', is_enabled: false, is_active: true, quotas: {} }],
      summary: { enabled_count: 0 },
    },
    claude: {
      accounts: [{ id: 'c1', email: 'prev@user.test' }],
      summary: { enabled_count: 0 },
      flow: null,
      pollTimer: null,
      countdownTimer: null,
      generation: 1,
    },
  },
  els: new Proxy({
    chatgptModal: makeEl(),
    cgptImportAuthTextarea: makeEl('{"secret":"jwt"}'),
    cgptNewLabelInput: makeEl('Personal'),
    quotaModal: makeEl(),
    tokenUsersWrap: makeEl(),
    tokenUsersBody: makeEl(),
    usersAdminList: makeEl(),
  }, {
    get(target, prop) {
      if (!(prop in target)) target[prop] = makeEl();
      return target[prop];
    },
  }),
};

vm.createContext(ctx);
for (const fnName of [
  'getAdvisorVendorQuota5hPct',
  'stopClaudeFlowPolling',
  'renderClaudeFlowUI',
  'startClaudeFlowPolling',
  'openClaudeModal',
  'resetChatGptStateOnLogout',
  'resetClaudeStateOnLogout',
  'renderQuotaModal',
  'renderAdminUsersList',
]) {
  vm.runInContext(extractFn(app, fnName), ctx);
}

// 1) Disabled accounts never report phantom advisor quota
assert.equal(ctx.getAdvisorVendorQuota5hPct('gpt'), 0);
assert.equal(ctx.getAdvisorVendorQuota5hPct('claude_code'), 0);

// 2) Claude OAuth flow polling stops on modal close without losing deadline and resumes on reopen
ctx.startClaudeFlowPolling({ flow_id: 'fl_1', status: 'waiting_code', expires_in: 300 });
const origDeadline = ctx.state.claude.flow._deadline;
assert(ctx.state.claude.pollTimer !== null);
ctx.stopClaudeFlowPolling(); // simulates Escape / backdrop close
assert.equal(ctx.state.claude.pollTimer, null);
assert.equal(ctx.state.claude.flow.flow_id, 'fl_1');
ctx.openClaudeModal();
assert(ctx.state.claude.pollTimer !== null, 'Reopening modal must resume Claude flow polling');
assert.equal(ctx.state.claude.flow._deadline, origDeadline, 'Reopening modal must preserve original _deadline');

// 3) Admin token breakdown shows shared_tokens_24h (100) next to limit and separates personal (400)
ctx.els.quotaModal.classList.remove('hidden');
ctx.renderQuotaModal();
assert(ctx.els.tokenUsersBody.innerHTML.includes('>100 <span class="nav-item-sub">(+400 личн.)</span></td>'), ctx.els.tokenUsersBody.innerHTML);
ctx.renderAdminUsersList();
assert(ctx.els.usersAdminList.innerHTML.includes('📊 24ч: 100 (всего 500)'), ctx.els.usersAdminList.innerHTML);
assert(ctx.els.usersAdminList.innerHTML.includes('📊 24ч: 80 · Всего: 80 tok'), ctx.els.usersAdminList.innerHTML);

// 4) Logout wipes sensitive inputs and clears stale Claude modal DOM
ctx.resetChatGptStateOnLogout();
assert.equal(ctx.state.chatgpt.accounts.length, 0);
assert.equal(ctx.els.cgptImportAuthTextarea.value, '');
assert.equal(ctx.els.cgptNewLabelInput.value, '');
ctx.resetClaudeStateOnLogout();
assert.equal(ctx.state.claude.accounts.length, 0);
assert.equal(ctx.state.claude.flow, null);
assert.equal(dom.claudeImportTextarea.value, '');
assert.equal(dom.claudeFlowCodeInput.value, '');
assert.equal(dom.claudeNewLabelInput.value, '');
assert.equal(dom.claudeAccountsList.innerHTML, '');
assert(ctx.renderClaudeModalCalled >= 2);

// 5) Service Worker exclusions (/sw.js, /api, cross-origin, POST) + offline navigate fallback to "/"
const handlers = {};
const cachedEntries = new Map([['/', { status: 200, body: 'SHELL' }]]);
const service = {
  URL, Promise,
  fetch: async () => { throw new Error('offline'); },
  caches: {
    open: async () => ({ put: async () => {} }),
    keys: async () => [],
    delete: async () => true,
    match: async (req) => cachedEntries.get(typeof req === 'string' ? req : req.url),
  },
  self: {
    location: { origin: 'https://hub.test' },
    addEventListener: (t, f) => { handlers[t] = f; },
    skipWaiting() {},
    clients: { claim() {} },
  },
};
vm.runInNewContext(sw, service);
for (const [method, url] of [
  ['GET', 'https://fonts.googleapis.com/a'],
  ['POST', 'https://hub.test/'],
  ['GET', 'https://hub.test/api/x'],
  ['GET', 'https://hub.test/sw.js'],
]) {
  handlers.fetch({ request: { method, url }, respondWith() { throw new Error('intercepted excluded: ' + url); } });
}
let navPromise = null;
handlers.fetch({
  request: { method: 'GET', url: 'https://hub.test/?chat=abc', mode: 'navigate' },
  respondWith(p) { navPromise = p; },
});
assert(navPromise, 'navigate request should be handled');
navPromise.then((res) => {
  assert.equal(res && res.body, 'SHELL');
}).catch((err) => {
  console.error(err);
  process.exit(1);
});
'''
        path = self.root / "verify.cjs"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            ["node", str(path), str(ROOT / "app/static/app.js"), str(ROOT / "app/static/sw.js")],
            capture_output=True,
            text=True,
            **config.subprocess_hidden_kwargs(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
