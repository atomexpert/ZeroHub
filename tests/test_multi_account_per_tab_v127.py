"""v127: Per-tab multi-account bucket routing, Google OAuth PKCE, HQ stability & UI invariants."""
import ast
import asyncio
import io
import json
import os
import re
import subprocess
import sys
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_rename_delete as mutations
import test_smart_hq_return_v115 as isolation


class MultiAccountPerTabV127Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(mutations.ProjectMutationTests.setUpClass.__func__)
    tearDownClass = classmethod(mutations.ProjectMutationTests.tearDownClass.__func__)
    _assert_live_db_untouched = mutations.ProjectMutationTests._assert_live_db_untouched

    async def asyncSetUp(self):
        await mutations.ProjectMutationTests.asyncSetUp(self)
        self.local_cli = self.tmp_root / "global_cli_v127"
        self.local_cli.mkdir(parents=True, exist_ok=True)
        for sub in ("builtin", "mcp", "brain", "conversations"):
            (self.local_cli / sub).mkdir(exist_ok=True)
            (self.local_cli / sub / "shared_keep.txt").write_text("keep", encoding="utf-8")
        for mod in (self.config, self.agy_engine):
            p = patch.object(mod, "AGY_CLI_DATA_DIR", self.local_cli)
            p.start()
            self._patches.append(p)
        p_sec = patch.object(
            self.database, "get_or_create_hub_secret", return_value="v127-isolated-fernet-secret"
        )
        p_sec.start()
        self._patches.append(p_sec)
        await self.database.init_db()

    async def asyncTearDown(self):
        from app import agy_accounts
        for fid in list(agy_accounts._AGY_OAUTH_FLOWS.keys()):
            await agy_accounts.cancel_agy_oauth_flow(fid)
        for acc in await self.database.list_account_profiles():
            self.config.cleanup_agy_account_dirs(acc["id"])
        await mutations.ProjectMutationTests.asyncTearDown(self)

    async def _make_agy_account(self, email: str, label: str, *, gem_pct=100, cld_pct=100, make_active=False):
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        payload = {
            "token": {
                "refresh_token": f"1//rt_{email}",
                "access_token": f"ya29.{email}",
                "expiry": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            },
            "email": email,
        }
        acc = await self.database.upsert_agy_account_from_credentials(
            json.dumps(payload), email=email, label=label, make_active=make_active
        )
        quotas = {
            "ok": True,
            "gemini_5h_pct": gem_pct,
            "gemini_weekly_pct": 95,
            "claude_5h_pct": cld_pct,
            "claude_weekly_pct": 90,
            "groups": [
                {"id": "gemini", "buckets": [{"window": "5h", "remaining_pct": gem_pct, "reset_time": future}]},
                {"id": "claude_gpt", "buckets": [{"window": "5h", "remaining_pct": cld_pct, "reset_time": future}]},
            ],
        }
        await self.database.update_agy_account_telemetry(acc["id"], quotas=quotas)
        return await self.database.get_account_profile_by_id(acc["id"])

    async def test_is_compacting_separated_from_is_generating(self):
        main, db = self.main_mod, self.database
        admin = await db.get_user_by_id("admin")
        chat = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="ШТАБ")
        cid = chat["id"]

        main.COMPACTING_CHATS.add(cid)
        try:
            listed = await main.api_list_chats(
                kind="project", project_name="AlphaProj", query=None, recent=False, limit=None, include_archived=True, current_user=admin
            )
            row = next(c for c in listed["chats"] if c["id"] == cid)
            self.assertTrue(row["is_compacting"])
            self.assertFalse(row["is_generating"])

            detail = await main.api_get_chat(cid, include_thinking=False, current_user=admin)
            self.assertTrue(detail["chat"]["is_compacting"])
            self.assertFalse(detail["chat"]["is_generating"])

            patched = await main.api_update_chat(
                cid, main.UpdateChatRequest(title="ШТАБ · План"), current_user=admin
            )
            self.assertTrue(patched["chat"]["is_compacting"])
            self.assertFalse(patched["chat"]["is_generating"])
        finally:
            main.COMPACTING_CHATS.discard(cid)

        with patch.object(main, "is_chat_generating", side_effect=lambda x: x == cid):
            detail_gen = await main.api_get_chat(cid, include_thinking=False, current_user=admin)
            self.assertTrue(detail_gen["chat"]["is_generating"])
            self.assertFalse(detail_gen["chat"]["is_compacting"])

    async def test_context_stats_merge_usage_recovery_trim_large_prompt_cleanup_and_priority(self):
        main, eng = self.main_mod, self.agy_engine

        # 1. merge_assistant_usage tracks proc_runs & last_run_*; compute_chat_context_stats uses last run
        u1 = main.merge_assistant_usage({}, {"input_tokens": 12000, "cache_read_tokens": 55000, "output_tokens": 400})
        u2 = main.merge_assistant_usage(u1, {"input_tokens": 14000, "cache_read_tokens": 60000, "output_tokens": 500})
        u3 = main.merge_assistant_usage(
            u2,
            {
                "input_tokens": 5000,
                "cache_read_tokens": 18000,
                "output_tokens": 300,
                "last_run_input_tokens": 5000,
                "last_run_cache_tokens": 18000,
                "last_run_llm_calls": 1,
            },
        )
        self.assertEqual(u3["proc_runs"], 3)
        self.assertEqual(u3["last_run_input_tokens"], 5000)
        self.assertEqual(u3["last_run_cache_tokens"], 18000)

        chat_obj = {
            "id": "ctx-v127",
            "kind": "project",
            "model": "gemini-4-carbon-high",
            "agy_conversation_id": "conv-127",
            "last_engine": "agy",
            "compacted_count": 1,
            "compacted_up_to_msg_id": 10,
            "compacted_summary": "Краткая сводка",
        }
        msgs = [
            {"id": 11, "role": "user", "status": "done", "content": "Проверь статус"},
            {"id": 12, "role": "assistant", "status": "done", "content": "Готово", "usage": u3, "tool_calls": []},
        ]
        stats = eng.compute_chat_context_stats(chat_obj, msgs)
        self.assertEqual(stats["source"], "telemetry")
        self.assertLess(stats["used_tokens"], 30000)

        # Legacy post-compaction record without last_run_* must also be capped below 100k
        legacy_msgs = [
            {"id": 11, "role": "user", "status": "done", "content": "Короткий вопрос"},
            {
                "id": 12,
                "role": "assistant",
                "status": "done",
                "content": "Ответ",
                "usage": {"input_tokens": 45000, "cache_read_tokens": 195000, "output_tokens": 300},
                "tool_calls": [],
            },
        ]
        legacy_stats = eng.compute_chat_context_stats(chat_obj, legacy_msgs)
        self.assertLess(legacy_stats["used_tokens"], 90000)

        # 2. _build_history_recovery_prompt trims to 6 messages of <=1800 chars & strips nested wrappers
        hist = [
            {
                "id": 20 + i,
                "role": "user" if i % 2 == 0 else "assistant",
                "status": "done",
                "content": (
                    "<PROJECT_PULSE>pulse</PROJECT_PULSE>\n"
                    "<CONTEXT_SUMMARY>nested</CONTEXT_SUMMARY>\n"
                    "[СИСТЕМНОЕ ПРОДОЛЖЕНИЕ ХОДА: старый баннер]\n"
                    f"MSG_{i}_" + ("x" * 2300)
                ),
            }
            for i in range(10)
        ] + [{"id": 99, "role": "user", "status": "done", "content": "Новый запрос"}]
        rec = eng._build_history_recovery_prompt(
            hist, "Новый запрос", compacted_summary="Сводка Штаба", compacted_up_to_msg_id=15
        )
        self.assertEqual(rec.count("<CONTEXT_SUMMARY>"), 1)
        self.assertNotIn("<PROJECT_PULSE>", rec)
        self.assertNotIn("СИСТЕМНОЕ ПРОДОЛЖЕНИЕ ХОДА", rec)
        self.assertNotIn("MSG_2_", rec)
        self.assertIn("MSG_4_", rec)
        self.assertIn("MSG_9_", rec)

        # 3. Large prompt (>29000 chars) writes temp file, deletes it in finally:, and empty/stale uploads dir is not mounted
        fake_bin = self.tmp_root / "fake_agy.exe"
        fake_bin.write_text("bin", encoding="utf-8")
        captured_cmds = []

        async def _fake_exec(*cmd_args, **kwargs):
            captured_cmds.append(cmd_args)
            # While running, temp prompt file must exist on disk for >29000 prompt
            p_arg = cmd_args[cmd_args.index("-p") + 1]
            if "_large_prompt_input_" in p_arg:
                m_f = re.search(r"([^\s]+_large_prompt_input_[a-f0-9]+\.md)", p_arg)
                self.assertIsNotNone(m_f)
                self.assertTrue(Path(m_f.group(1)).is_file())
            proc = MagicMock(returncode=0)
            proc.stdout = asyncio.StreamReader()
            proc.stdout.feed_data(
                b'{"event":"init","conversation_id":"c-big"}\n'
                b'{"event":"result","result":{"status":"DONE","response":"ok","usage":{"input_tokens":100,"output_tokens":10}}}\n'
            )
            proc.stdout.feed_eof()
            proc.stderr = asyncio.StreamReader()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=0)
            proc.aclose_lease = AsyncMock()
            return proc

        with (
            patch.object(eng, "resolve_agy_bin", return_value=str(fake_bin)),
            patch.object(eng.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(eng, "create_agy_subprocess_exec", side_effect=_fake_exec),
        ):
            big_events = [
                e
                async for e in eng.stream_agy_turn(
                    chat_id="big-chat-127",
                    kind="project",
                    project_name="AlphaProj",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="План " + ("А" * 30500),
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    resolved_project_dir=self.root / "AlphaProj",
                )
            ]
            self.assertEqual(big_events[-1]["type"], "done")
            up_dir = self.config.resolve_chat_uploads_dir({"id": "big-chat-127", "user_id": "admin"}, owner_username="admin")
            self.assertFalse(any(up_dir.glob("*large_prompt_input*")) if up_dir.exists() else False)

            # Even if a legacy stale large_prompt_input.md sits in uploads/<chat_id>, normal prompt <=29000 does NOT mount uploads dir
            up_dir.mkdir(parents=True, exist_ok=True)
            (up_dir / "large_prompt_input.md").write_text("stale", encoding="utf-8")
            _ = [
                e
                async for e in eng.stream_agy_turn(
                    chat_id="big-chat-127",
                    kind="project",
                    project_name="AlphaProj",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Обычный запрос длиной 26000 " + ("Б" * 26000),
                    attachments=[],
                    agy_conversation_id="c-big",
                    history_messages=[],
                    resolved_project_dir=self.root / "AlphaProj",
                )
            ]
            second_cmd = [str(x) for x in captured_cmds[-1]]
            self.assertNotIn(str(up_dir), second_cmd)

        # 4. Priority queue: 'turn' preempts 'telemetry' on _lease_lock
        from app import agy_credentials as creds
        order = []
        await creds._lease_lock.acquire()

        async def _telem():
            await creds._acquire_lease_with_priority("telemetry")
            order.append("telemetry")
            creds._lease_lock.release()

        async def _turn():
            await creds._acquire_lease_with_priority("turn")
            order.append("turn")
            creds._lease_lock.release()

        t_telem = asyncio.create_task(_telem())
        await asyncio.sleep(0.01)
        t_turn = asyncio.create_task(_turn())
        await asyncio.sleep(0.01)
        creds._lease_lock.release()
        await asyncio.gather(t_telem, t_turn)
        self.assertEqual(order, ["turn", "telemetry"])

    async def test_parallel_tabs_bucket_routing_brain_isolation_and_explicit_pins(self):
        main, db, eng = self.main_mod, self.database, self.agy_engine
        admin = await db.get_user_by_id("admin")

        acc1 = await self._make_agy_account("acc1@gmail.com", "Google #1", gem_pct=95, cld_pct=0, make_active=True)
        acc2 = await self._make_agy_account("acc2@gmail.com", "Google #2", gem_pct=100, cld_pct=90, make_active=False)

        # Verify legacy junction migration + real isolated brain directory per account
        dest1 = self.config.CLI_HOMES_DIR / "_agy_accounts" / acc1["id"] / "antigravity-cli"
        dest1.mkdir(parents=True, exist_ok=True)
        self.config._agy_link(dest1 / "brain", self.local_cli / "brain")
        self.assertTrue(self.config._is_junction_or_symlink(dest1 / "brain"))

        home1 = await eng.materialize_agy_account_home(acc1)
        home2 = await eng.materialize_agy_account_home(acc2)
        b1 = (home1 / ".gemini" / "antigravity-cli" / "brain").resolve()
        b2 = (home2 / ".gemini" / "antigravity-cli" / "brain").resolve()
        self.assertFalse(self.config._is_junction_or_symlink(dest1 / "brain"))
        self.assertNotEqual(b1, b2)
        self.assertNotEqual(b1, (self.local_cli / "brain").resolve())
        self.assertEqual((self.local_cli / "brain" / "shared_keep.txt").read_text(encoding="utf-8"), "keep")

        # Parallel tabs: Tab A (Gemini on Acc #1) and Tab B (Claude auto-routed to Acc #2 without resetting Tab A)
        tab_a = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="Tab A", model="gemini-4-carbon-high")
        tab_b = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="Tab B", model="claude-sonnet-5-5-high")

        seen_calls = []

        async def _mock_stream(**kwargs):
            resolved = await eng.resolve_agy_account_for_chat_turn(
                kwargs["model"],
                preferred_account_id=kwargs.get("preferred_agy_account_id", ""),
                current_chat_account_id=kwargs.get("agy_account_id", ""),
            )
            chosen_acc = resolved["account"]
            seen_calls.append(
                {
                    "chat_id": kwargs["chat_id"],
                    "model": kwargs["model"],
                    "incoming_conv_id": kwargs["agy_conversation_id"],
                    "resolved_acc_id": chosen_acc["id"],
                }
            )
            if resolved.get("emit_failover") and resolved.get("switched_from"):
                yield {
                    "type": "agy_failover",
                    "from_label": resolved["switched_from"]["label"],
                    "to_label": chosen_acc["label"],
                    "from_account_id": resolved["switched_from"]["id"],
                    "account_id": chosen_acc["id"],
                }
            conv_id = kwargs["agy_conversation_id"] or f"conv-{kwargs['chat_id'][:6]}-{chosen_acc['id'][-4:]}"
            yield {"type": "agy_session", "agy_conversation_id": conv_id, "agy_account_id": chosen_acc["id"]}
            yield {
                "type": "done",
                "content": "Ответ готов",
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 500, "output_tokens": 50},
                "agy_conversation_id": conv_id,
                "agy_account_id": chosen_acc["id"],
            }

        with (
            patch.object(main, "fetch_available_models", AsyncMock(return_value=eng.DEFAULT_MODELS)),
            patch.object(main, "get_cli_auth_status", return_value={"ok": True}),
            patch.object(main.openrouter_service, "is_user_openrouter_bridge_active", AsyncMock(return_value=False)),
            patch.object(main.sandbox_service, "is_user_sandbox_eligible", return_value=False),
            patch.object(main.host_service_manager, "is_user_host_eligible", return_value=False),
            patch.object(main, "_refresh_and_broadcast_quotas", AsyncMock()),
            patch.object(main.ws_manager, "broadcast_for_chat", AsyncMock()),
            patch.object(main.ws_manager, "broadcast_user", AsyncMock()),
            patch.object(main.ws_manager, "broadcast_admins", AsyncMock()),
            patch.object(main, "stream_agy_turn", side_effect=_mock_stream),
        ):
            # 1) Tab A turn 1 on Gemini -> uses Acc #1
            await main._run_turn_task_inner(
                chat_id=tab_a["id"], prompt="Шаг 1 в Gemini", attachments=[], override_model="gemini-4-carbon-high", override_access_mode="read_only", trigger_user=admin
            )
            a_after_1 = await db.get_chat(tab_a["id"])
            self.assertEqual(a_after_1["agy_account_id"], acc1["id"])
            conv_a = a_after_1["agy_conversation_id"]
            self.assertTrue(conv_a)

            # 2) Tab B turn 1 on Claude -> Acc #1 has claude=0%, auto-routes Tab B to Acc #2 WITHOUT changing global active!
            await main._run_turn_task_inner(
                chat_id=tab_b["id"], prompt="Параллельная задача на Claude", attachments=[], override_model="claude-sonnet-5-5-high", override_access_mode="read_only", trigger_user=admin
            )
            b_after_1 = await db.get_chat(tab_b["id"])
            self.assertEqual(b_after_1["agy_account_id"], acc2["id"])
            self.assertEqual((await db.get_active_account_profile())["id"], acc1["id"])

            # 3) Tab A turn 2 on Gemini -> MUST preserve conv_a and continue on Acc #1!
            await main._run_turn_task_inner(
                chat_id=tab_a["id"], prompt="Шаг 2 в Gemini без потери сессии", attachments=[], override_model="gemini-4-carbon-high", override_access_mode="read_only", trigger_user=admin
            )
            self.assertEqual(seen_calls[-1]["incoming_conv_id"], conv_a)
            self.assertEqual(seen_calls[-1]["resolved_acc_id"], acc1["id"])

            # 4) Explicit pin on Tab A -> preferred_agy_account_id = acc2 resets Tab A conv_id only for Tab A
            await main.api_update_chat(
                tab_a["id"],
                main.UpdateChatRequest(
                    preferred_agy_account_id=acc2["id"],
                    preferred_codex_account_id="cgpt_pin_2",
                    preferred_claude_account_id="claude_pin_2",
                ),
                current_user=admin,
            )
            pinned_a = await db.get_chat(tab_a["id"])
            self.assertEqual(pinned_a["preferred_agy_account_id"], acc2["id"])
            self.assertEqual(pinned_a["preferred_codex_account_id"], "cgpt_pin_2")
            self.assertEqual(pinned_a["preferred_claude_account_id"], "claude_pin_2")

            await main._run_turn_task_inner(
                chat_id=tab_a["id"], prompt="Ход на закреплённом #2", attachments=[], override_model="gemini-4-carbon-high", override_access_mode="read_only", trigger_user=admin
            )
            self.assertEqual(seen_calls[-1]["incoming_conv_id"], "")
            self.assertEqual(seen_calls[-1]["resolved_acc_id"], acc2["id"])

        # Deleting acc2 resets preferred_agy_account_id back to '' (Auto)
        await db.delete_account_profile(acc2["id"])
        after_del = await db.get_chat(tab_a["id"])
        self.assertEqual(after_del["preferred_agy_account_id"], "")

    async def test_google_oauth_pkce_loopback_remote_url_fernet_and_fail_closed_proxy(self):
        from app import agy_account_api, agy_accounts
        db = self.database
        admin = {"id": "admin", "username": "admin", "role": "admin"}

        # 1. Fail-closed proxy check on oauth/start
        with (
            patch.object(
                self.main_mod,
                "_validate_account_proxy_input",
                AsyncMock(return_value=("custom", "socks5://127.0.0.1:19999", "CustomNode")),
            ),
            patch.object(
                agy_accounts.proxy_router,
                "ensure_proxy_for_context",
                AsyncMock(side_effect=agy_accounts.proxy_router.ProxyRouteError("proxy unreachable")),
            ),
        ):
            with self.assertRaises(agy_account_api.HTTPException) as ctx_err:
                await agy_account_api.start_oauth(
                    agy_account_api.StartOAuthRequest(label="Blocked", proxy_mode="custom", proxy_spec="socks5://127.0.0.1:19999"),
                    _admin=admin,
                )
            self.assertEqual(ctx_err.exception.status_code, 502)

        def _make_mock_opener():
            op = MagicMock()
            op.open.side_effect = [
                io.BytesIO(b'{"access_token":"ya29.pkce_acc","refresh_token":"1//pkce_secret_rt","expires_in":3600}'),
                io.BytesIO(b'{"email":"pkce_loopback@gmail.com"}'),
            ]
            return op

        # 2. Loopback PC callback flow
        with (
            patch.object(agy_accounts, "_oauth_pair", return_value=("mock-cid.apps.googleusercontent.com", "GOCSPX-mocksecret")),
            patch.object(agy_accounts.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value="http://127.0.0.1:10808")),
            patch.object(self.agy_engine, "fetch_account_quotas", AsyncMock(return_value={"ok": True, "gemini_5h_pct": 100})),
            patch.object(self.main_mod.ws_manager, "broadcast_admins", AsyncMock()),
            patch("urllib.request.build_opener", side_effect=lambda *_a, **_k: _make_mock_opener()),
        ):
            started = await agy_account_api.start_oauth(
                agy_account_api.StartOAuthRequest(label="Ultra Loopback"),
                _admin=admin,
            )
            self.assertNotIn("GOCSPX", json.dumps(started))
            self.assertNotIn("code_verifier", json.dumps(started))
            parsed_auth = urllib.parse.urlsplit(started["auth_url"])
            auth_qs = urllib.parse.parse_qs(parsed_auth.query)
            self.assertEqual(auth_qs["code_challenge_method"], ["S256"])
            state_tok = auth_qs["state"][0]

            red_split = urllib.parse.urlsplit(started["redirect_uri"])
            reader, writer = await asyncio.open_connection("127.0.0.1", red_split.port)
            req_path = f"{red_split.path}?code=4/0A_loopback_code&state={urllib.parse.quote(state_tok)}"
            writer.write(f"GET {req_path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode("ascii"))
            await writer.drain()
            http_resp = (await reader.read()).decode("utf-8", errors="replace")
            writer.close()
            await writer.wait_closed()
            self.assertIn("200 OK", http_resp)

            st_after = await agy_account_api.get_oauth_status(started["flow_id"], _admin=admin)
            self.assertEqual(st_after["status"], "completed")
            self.assertEqual(st_after["account"]["email"], "pkce_loopback@gmail.com")
            self.assertNotIn("1//pkce_secret_rt", json.dumps(st_after))

            # 3. Remote smartphone URL paste flow + state mismatch guard
            op_phone = MagicMock()
            op_phone.open.side_effect = [
                io.BytesIO(b'{"access_token":"ya29.phone","refresh_token":"1//phone_secret_rt","expires_in":3600}'),
                io.BytesIO(b'{"email":"pkce_phone@gmail.com"}'),
            ]
            with patch("urllib.request.build_opener", return_value=op_phone):
                started2 = await agy_account_api.start_oauth(
                    agy_account_api.StartOAuthRequest(label="Phone Pro"),
                    _admin=admin,
                )
                state2 = urllib.parse.parse_qs(urllib.parse.urlsplit(started2["auth_url"]).query)["state"][0]

                with self.assertRaises(agy_account_api.HTTPException) as bad_state:
                    await agy_account_api.complete_oauth(
                        started2["flow_id"],
                        agy_account_api.CompleteOAuthRequest(
                            code_or_url=f"{started2['redirect_uri']}?code=4/0A_phone&state=WRONG_STATE"
                        ),
                        _admin=admin,
                    )
                self.assertEqual(bad_state.exception.status_code, 400)

                done_phone = await agy_account_api.complete_oauth(
                    started2["flow_id"],
                    agy_account_api.CompleteOAuthRequest(
                        code_or_url=f"{started2['redirect_uri']}?code=4/0A_phone_ok&state={urllib.parse.quote(state2)}"
                    ),
                    _admin=admin,
                )
                self.assertEqual(done_phone["account"]["email"], "pkce_phone@gmail.com")
                self.assertNotIn("1//phone_secret_rt", json.dumps(done_phone))

        # Verify Fernet encryption at rest in SQLite
        async with db.connect_db() as conn:
            rows = await (await conn.execute("SELECT credentials_json_encrypted FROM account_profiles")).fetchall()
        raw_cipher = " ".join(r[0] for r in rows)
        self.assertNotIn("1//pkce_secret_rt", raw_cipher)
        self.assertNotIn("1//phone_secret_rt", raw_cipher)

    def test_frontend_v127_static_invariants_and_node_vm(self):
        static_dir = isolation.ROOT_DIR / "app" / "static"
        html_txt = (static_dir / "index.html").read_text(encoding="utf-8")
        sw_txt = (static_dir / "sw.js").read_text(encoding="utf-8")
        css_txt = (static_dir / "styles.css").read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_txt)
        self.assertIsNotNone(m_sw)
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 129)
        self.assertIn(f"styles.css?v={ver}", html_txt)
        self.assertIn(f"app.js?v={ver}", html_txt)
        self.assertIn("data-provider-hub-nav", html_txt)
        self.assertIn('id="agyStartOAuthBtn"', html_txt)
        self.assertIn('id="modelPickerAccBadge"', html_txt)
        self.assertNotIn("status-green", css_txt)
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", css_txt.split("/* v125:", 1)[1]))

        chk = subprocess.run(
            ["node", "--check", str(static_dir / "app.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
            **isolation._subprocess_hidden_kwargs(),
        )
        self.assertEqual(chk.returncode, 0, chk.stderr)

        tree = ast.parse(Path(isolation.__file__).read_text(encoding="utf-8"))
        harness = next(
            n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "node_harness" for t in n.targets)
        )
        harness += r"""
// 1. Deduplicated Google AI identity (no 'Ultra Ultra')
const id1 = api.formatAgyAccountIdentity({label: "Ultra", email: "Ultra", plan_note: "Ultra"}, {tier_summary: "Google AI Ultra"});
assert.equal(id1.titleTxt, "Ultra");
assert.equal(id1.emailSub, "");
assert.equal(id1.planBadge, "");

const id2 = api.formatAgyAccountIdentity({label: "Основной", email: "user@gmail.com", plan_note: "Ultra"}, {});
assert.equal(id2.titleTxt, "Основной");
assert.equal(id2.emailSub, "user@gmail.com");
assert.equal(id2.planBadge, "Ultra");

// 2. Per-tab account routing badge & pills (Auto bucket routing vs Manual Pin)
api.state.currentUser = {id: "admin", role: "admin"};
api.state.accountProfiles = [
  {id: "agy_1", label: "Main", email: "a1@gmail.com", is_enabled: true, is_active: true, quotas: {gemini_5h_pct: 95, claude_5h_pct: 0}},
  {id: "agy_2", label: "Reserve", email: "a2@gmail.com", is_enabled: true, is_active: false, quotas: {gemini_5h_pct: 100, claude_5h_pct: 88}}
];
const gemInfo = api.computeTabAccountDisplayInfo("gemini-4-carbon-high", {agy_account_id: "agy_1", preferred_agy_account_id: ""});
assert.equal(gemInfo.badgeText, "⚡ #1");
const cldInfo = api.computeTabAccountDisplayInfo("claude-sonnet-5-5-high", {agy_account_id: "", preferred_agy_account_id: ""});
assert.equal(cldInfo.badgeText, "⚡ #2");
const pinInfo = api.computeTabAccountDisplayInfo("gemini-4-carbon-high", {agy_account_id: "agy_1", preferred_agy_account_id: "agy_2"});
assert.equal(pinInfo.badgeText, "📌 #2");
assert.equal(pinInfo.isPinned, true);

const selectorHtml = api.renderTabAccountSelectorBoxHtml("claude-sonnet-5-5-high", "chat");
assert.ok(selectorHtml.includes("Аккаунт для этой вкладки"));
assert.ok(selectorHtml.includes("Авто (по лимитам)"));
assert.ok(selectorHtml.includes("Gem 95% / Cld 0%"));
assert.ok(selectorHtml.includes("Gem 100% / Cld 88%"));

// 3. >4 accounts filter & sort
const fiveAccs = [1,2,3,4,5].map(i => ({id: "a"+i, label: "Acc "+i, email: `u${i}@gmail.com`, is_enabled: true, is_active: i===3, quotas: {gemini_5h_pct: i*15, claude_5h_pct: i*10}}));
api.state.accountFilterQueries = {agy: "u5@"};
const filtered = api.sortAndFilterAccountsList("agy", fiveAccs);
assert.equal(filtered.length, 1);
assert.equal(filtered[0].id, "a5");

// 4. is_compacting === true & is_generating === false hides #stopBtn and shows 'Сжимаем…'
api.state.activeChatId = "hq-chat-127";
api.state.activeChat = {id: "hq-chat-127", kind: "project", model: "gemini-4-carbon-high", is_generating: false, is_compacting: true};
api.state.messages = [{id: 1, role: "user", content: "План"}, {id: 2, role: "assistant", content: "Ответ"}];
api.state.generatingChats.clear();
api.syncChatCompactingFromPayload(api.state.activeChat);
assert.ok(api.state.compactingChats.has("hq-chat-127"));
api.syncComposerState();
assert.ok(api.els.stopBtn.classList.contains("hidden"));
assert.equal(api.els.compactContextLabel.textContent, "Сжимаем…");
assert.equal(api.els.compactContextBtn.disabled, true);
console.log("V127_FRONTEND_OK");
"""
        vm_script = self.tmp_root / "v127_vm.js"
        vm_script.write_text(harness, encoding="utf-8")
        vm_res = subprocess.run(
            ["node", str(vm_script), str(static_dir / "app.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=25,
            **isolation._subprocess_hidden_kwargs(),
        )
        self.assertEqual(vm_res.returncode, 0, vm_res.stderr)
        self.assertIn("V127_FRONTEND_OK", vm_res.stdout)


if __name__ == "__main__":
    unittest.main()
