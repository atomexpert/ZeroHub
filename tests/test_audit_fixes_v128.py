"""v128: Comprehensive regression & security suite for all 21 dual-audit fixes."""
import ast
import asyncio
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_rename_delete as mutations
import test_smart_hq_return_v115 as isolation


class AuditFixesV128Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(mutations.ProjectMutationTests.setUpClass.__func__)
    tearDownClass = classmethod(mutations.ProjectMutationTests.tearDownClass.__func__)
    _assert_live_db_untouched = mutations.ProjectMutationTests._assert_live_db_untouched

    async def asyncSetUp(self):
        await mutations.ProjectMutationTests.asyncSetUp(self)
        self.local_cli = self.tmp_root / "global_cli_v128"
        self.local_cli.mkdir(parents=True, exist_ok=True)
        for sub in ("builtin", "mcp", "brain", "conversations"):
            (self.local_cli / sub).mkdir(exist_ok=True)
        for mod in (self.config, self.agy_engine):
            p = patch.object(mod, "AGY_CLI_DATA_DIR", self.local_cli)
            p.start()
            self._patches.append(p)
        from app import workspace_mcp
        p_wmcp = patch.object(workspace_mcp.config, "AGY_CLI_DATA_DIR", self.local_cli)
        p_wmcp.start()
        self._patches.append(p_wmcp)
        p_sec = patch.object(
            self.database, "get_or_create_hub_secret", return_value="v128-isolated-fernet-secret"
        )
        p_sec.start()
        self._patches.append(p_sec)
        await self.database.init_db()

    async def asyncTearDown(self):
        from app import agy_accounts, claude_engine
        for fid in list(agy_accounts._AGY_OAUTH_FLOWS.keys()):
            await agy_accounts.cancel_agy_oauth_flow(fid)
        for fid, fl in list(claude_engine._OAUTH_FLOWS.items()):
            if fl.expiry_task and not fl.expiry_task.done():
                fl.expiry_task.cancel()
        claude_engine._OAUTH_FLOWS.clear()
        self.agy_engine.ACTIVE_RUNS.clear()
        self.main_mod.COMPACTING_CHATS.clear()
        self.main_mod.COMPACTING_OWNERS.clear()
        self.main_mod.COMPACTING_TASKS.clear()
        self.main_mod.ACTIVE_TURN_TASKS.clear()
        self.main_mod.CHAT_TURN_QUEUES.clear()
        self.main_mod._INFLIGHT_HQ_RETURN_UIDS.clear()
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
            "claude_5h_pct": cld_pct,
            "groups": [
                {"id": "gemini", "buckets": [{"window": "5h", "remaining_pct": gem_pct, "reset_time": future}]},
                {"id": "claude_gpt", "buckets": [{"window": "5h", "remaining_pct": cld_pct, "reset_time": future}]},
            ],
        }
        await self.database.update_agy_account_telemetry(acc["id"], quotas=quotas)
        return await self.database.get_account_profile_by_id(acc["id"])

    # ------------------------------------------------------------------
    # STAGE 1: Keyring _lease_lock, restore(), quota lock, transport, OAuth TTL, maintenance
    # ------------------------------------------------------------------
    async def test_stage1_lease_lock_no_deadlock_and_bulletproof_restore(self):
        if os.name != "nt":
            self.skipTest("Windows keyring lease test")
        from app import agy_credentials as creds

        acc = await self._make_agy_account("lease128@gmail.com", "LeaseAcc", make_active=True)
        home = await self.agy_engine.materialize_agy_account_home(acc)
        kwargs = {"env": {"USERPROFILE": str(home)}}

        # 1) Failure in store.read() BEFORE LeasedProcess creation MUST release _lease_lock
        bad_store = MagicMock()
        bad_store.read.side_effect = ValueError("Unsupported CredReadW attributes")
        with patch.object(creds, "WindowsCredentialStore", return_value=bad_store):
            with self.assertRaises(ValueError):
                await creds.create_agy_subprocess_exec("fake.exe", **kwargs)
        self.assertFalse(creds._lease_lock.locked())

        # 2) UTF-16LE refreshed blob is saved back & original restored; OSError on write never crashes stdout
        utf16_tok = {
            "token": {
                "refresh_token": "1//rt_lease128@gmail.com",
                "access_token": "ya29.utf16_refreshed",
                "expiry": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            }
        }
        original = {"blob": b"host-orig".hex(), "persist": 2, "username": "antigravity"}
        state = {"val": original, "fail_restore_write": True}
        store = MagicMock()
        store.read.side_effect = lambda: state["val"]

        def _write(entry):
            if entry == original and state["fail_restore_write"]:
                raise OSError("Simulated CredWriteW lock contention")
            state["val"] = entry

        store.write.side_effect = _write
        proc = MagicMock(returncode=0)
        proc.wait = AsyncMock(return_value=0)
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(b'{"event":"init"}\n')
        proc.stdout.feed_eof()

        with (
            patch.object(creds, "WindowsCredentialStore", return_value=store),
            patch.object(creds.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
        ):
            leased = await creds.create_agy_subprocess_exec("fake.exe", **kwargs)
            # Simulate CLI refreshing keyring in UTF-16LE before first stdout line
            state["val"] = {
                "blob": json.dumps(utf16_tok).encode("utf-16-le").hex(),
                "persist": 2,
                "username": "antigravity",
            }
            line = await leased.stdout.readline()
            self.assertEqual(line, b'{"event":"init"}\n')
            self.assertFalse(creds._lease_lock.locked())
            saved_disk = json.loads(
                (home / ".gemini/antigravity-cli/antigravity-oauth-token").read_text(encoding="utf-8")
            )
            self.assertEqual(saved_disk["token"]["access_token"], "ya29.utf16_refreshed")
            await leased.aclose_lease()

        # 3) Non-JSON external binary blob during lease sets restore_original=False (does not overwrite external session)
        ext_entry = {"blob": b"\xff\xfe\x00\x01binary-external".hex(), "persist": 2, "username": "ext"}
        state2 = {"val": original}
        store2 = MagicMock()
        store2.read.side_effect = lambda: state2["val"]
        store2.write.side_effect = lambda v: state2.update(val=v)
        proc2 = MagicMock(returncode=0)
        proc2.wait = AsyncMock(return_value=0)
        proc2.stdout = asyncio.StreamReader()
        proc2.stdout.feed_data(b"ok\n")
        proc2.stdout.feed_eof()
        with (
            patch.object(creds, "WindowsCredentialStore", return_value=store2),
            patch.object(creds.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc2)),
        ):
            leased2 = await creds.create_agy_subprocess_exec("fake.exe", **kwargs)
            state2["val"] = ext_entry
            self.assertEqual(await leased2.stdout.readline(), b"ok\n")
            self.assertEqual(state2["val"], ext_entry)
            self.assertFalse(creds._lease_lock.locked())

    async def test_stage1_quota_lock_transport_oauth_ttl_and_maintenance(self):
        from app import agy_accounts, claude_engine, storage_maintenance

        # 1) Per-account quota lock deduplicates parallel _fetch_account_quotas calls
        acc = await self._make_agy_account("qlock@gmail.com", "QLock", make_active=True)
        fake_bin = self.tmp_root / "fake_agy_q.exe"
        fake_bin.write_text("bin", encoding="utf-8")
        exec_calls = 0

        async def _slow_quota_proc(*_a, **_kw):
            nonlocal exec_calls
            exec_calls += 1
            await asyncio.sleep(0.05)
            p = MagicMock(returncode=0)
            p.communicate = AsyncMock(
                return_value=(
                    json.dumps(
                        {
                            "event": "command_result",
                            "command": {
                                "data": {
                                    "groups": [
                                        {
                                            "name": "Gemini",
                                            "buckets": [{"window": "5h", "remaining_fraction": 0.91}],
                                        }
                                    ],
                                    "remaining_credits": 500,
                                }
                            },
                        }
                    ).encode("utf-8"),
                    b"",
                )
            )
            p.aclose_lease = AsyncMock()
            return p

        # Clear cached_at so first caller fetches and updates DB cache
        async with self.database.connect_db() as conn:
            await conn.execute("UPDATE account_profiles SET cached_at = '' WHERE id = ?", (acc["id"],))
            await conn.commit()
        fresh_acc = await self.database.get_account_profile_by_id(acc["id"])

        with (
            patch.object(self.agy_engine, "resolve_agy_bin", return_value=str(fake_bin)),
            patch.object(self.agy_engine.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(self.agy_engine, "create_agy_subprocess_exec", side_effect=_slow_quota_proc),
            patch.object(self.agy_engine, "sync_refreshed_agy_credentials_back", AsyncMock()),
        ):
            r1, r2 = await asyncio.gather(
                self.agy_engine.fetch_account_quotas(False, acc["id"]),
                self.agy_engine.fetch_account_quotas(False, acc["id"]),
            )
        # Only caller #1 runs /usage + /credits (2 CLI calls); caller #2 hits fresh DB cache under per-account lock!
        self.assertEqual(exec_calls, 2)
        self.assertTrue(r1["ok"] and r2["ok"])

        # 2) _terminate_hung_cli_proc closes _transport
        raw_p = MagicMock(returncode=0, pid=None)
        raw_p._transport = MagicMock()
        wrapped_p = MagicMock(proc=raw_p, returncode=0, pid=None)
        await self.agy_engine._terminate_hung_cli_proc(wrapped_p)
        raw_p._transport.close.assert_called_once()

        # 3) Google & Claude OAuth flows auto-expire via background TTL task
        with (
            patch.object(agy_accounts, "AGY_OAUTH_FLOW_TTL_SEC", 0.06),
            patch.object(agy_accounts, "_oauth_pair", return_value=("cid", "sec")),
            patch.object(agy_accounts.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
        ):
            g_started = await agy_accounts.start_agy_oauth_flow(label="ShortTTL")
            await asyncio.sleep(0.14)
            g_st = await agy_accounts.get_agy_oauth_flow_status(g_started["flow_id"])
            self.assertEqual(g_st["status"], "expired")
            fl_obj = agy_accounts._AGY_OAUTH_FLOWS[g_started["flow_id"]]
            self.assertIsNone(fl_obj.server)

        fake_cl_proc = MagicMock(returncode=None, pid=None)
        fake_cl_proc.terminate = MagicMock()
        fake_cl_proc.wait = AsyncMock(return_value=0)
        fake_cl_flow = claude_engine.ClaudeOAuthFlow(
            flow_id="cl_ttl_test",
            user_id="admin",
            env={},
            created_ts=time.monotonic() - 9999,
            pending_dir=self.tmp_root / "cl_pend",
            label="CL",
            proxy_mode="global",
            proxy_label="",
            proc=fake_cl_proc,
        )
        claude_engine._OAUTH_FLOWS["cl_ttl_test"] = fake_cl_flow
        cl_pub = claude_engine.get_claude_oauth_flow("admin", "cl_ttl_test")
        self.assertEqual(cl_pub["status"], "expired")
        self.assertEqual(cl_pub["expires_in"], 0)
        await asyncio.sleep(0.02)

        # 4) storage_maintenance cleans empty sandbox dir & stale large prompt files (skipping ACTIVE_RUNS)
        sbx_dir = self.config.resolve_user_chat_sandbox_dir("admin", "del-chat-128", create=True)
        self.assertTrue(sbx_dir.is_dir())
        await storage_maintenance.cleanup_deleted_chat({"id": "del-chat-128", "user_id": "admin"}, "admin")
        self.assertFalse(sbx_dir.exists())

        up_stale = self.config.resolve_chat_uploads_dir({"id": "stale-128", "user_id": "admin"}, owner_username="admin")
        up_active = self.config.resolve_chat_uploads_dir({"id": "active-128", "user_id": "admin"}, owner_username="admin")
        up_stale.mkdir(parents=True, exist_ok=True)
        up_active.mkdir(parents=True, exist_ok=True)
        f_stale = up_stale / "_large_prompt_input_deadbeef.md"
        f_active = up_active / "_large_prompt_input_cafebabe.md"
        f_stale.write_text("old", encoding="utf-8")
        f_active.write_text("live", encoding="utf-8")
        old_ts = time.time() - 9000
        os.utime(f_stale, (old_ts, old_ts))
        os.utime(f_active, (old_ts, old_ts))
        self.agy_engine.register_chat_turn("active-128", user_id="admin")
        try:
            removed = storage_maintenance.cleanup_stale_large_prompt_files(ttl_sec=3600.0)
            self.assertEqual(removed, 1)
            self.assertFalse(f_stale.exists())
            self.assertFalse(up_stale.exists())
            self.assertTrue(f_active.exists())
        finally:
            self.agy_engine.clear_chat_turn("active-128")

    # ------------------------------------------------------------------
    # STAGE 2: Failover safety, session isolation, 35s resume recovery, telemetry/context, large prompt
    # ------------------------------------------------------------------
    async def test_stage2_failover_resilience_session_isolation_and_resume_recovery(self):
        eng, db, main = self.agy_engine, self.database, self.main_mod
        fake_bin = self.tmp_root / "fake_agy_s2.exe"
        fake_bin.write_text("bin", encoding="utf-8")

        acc1 = await self._make_agy_account("s2_1@gmail.com", "Acc1", make_active=True)
        acc2 = await self._make_agy_account("s2_2@gmail.com", "Acc2", make_active=False)
        acc3 = await self._make_agy_account("s2_3@gmail.com", "Acc3", make_active=False)

        # 1) agy_session_db_exists strictly isolates per-account conversations from global AGY_CLI_DATA_DIR
        (self.local_cli / "conversations" / "global_only_cid.db").write_text("sqlite", encoding="utf-8")
        self.assertTrue(eng.agy_session_db_exists("global_only_cid"))
        self.assertFalse(eng.agy_session_db_exists("global_only_cid", account_id=acc1["id"]))

        # Legacy tab with agy_account_id="" & global_only_cid resets conv_id in _run_turn_task_inner
        legacy_chat = await db.create_chat(
            kind="project", project_name="AlphaProj", owner_id="admin", title="LegacyTab"
        )
        await db.update_chat(legacy_chat["id"], agy_conversation_id="global_only_cid", agy_account_id="")
        seen_in_stream = []

        async def _check_legacy_stream(**kw):
            seen_in_stream.append(kw["agy_conversation_id"])
            yield {"type": "done", "content": "ok", "thinking": "", "tool_calls": [], "usage": {}}

        admin = await db.get_user_by_id("admin")
        with (
            patch.object(main, "fetch_available_models", AsyncMock(return_value=eng.DEFAULT_MODELS)),
            patch.object(main, "get_cli_auth_status", return_value={"ok": True}),
            patch.object(main.openrouter_service, "is_user_openrouter_bridge_active", AsyncMock(return_value=False)),
            patch.object(main.sandbox_service, "is_user_sandbox_eligible", return_value=False),
            patch.object(main.host_service_manager, "is_user_host_eligible", return_value=False),
            patch.object(main, "_refresh_and_broadcast_quotas", AsyncMock()),
            patch.object(main.ws_manager, "broadcast_for_chat", AsyncMock()),
            patch.object(main, "stream_agy_turn", side_effect=_check_legacy_stream),
        ):
            await main._run_turn_task_inner(
                chat_id=legacy_chat["id"],
                prompt="Проверка legacy",
                attachments=[],
                override_model="gemini-4-carbon-high",
                override_access_mode="read_only",
                trigger_user=admin,
            )
        self.assertEqual(seen_in_stream[-1], "")

        # 2) Startup ProxyRouteError on acc1 & acc2 seamlessly fails over to acc3; pinned acc1 yields structured error
        async def _proxy_side_effect(_ctx, account_id=""):
            if account_id in (acc1["id"], acc2["id"]):
                raise eng.proxy_router.ProxyRouteError(f"proxy-down-{account_id}")
            return None

        async def _ok_proc(*_a, **_kw):
            p = MagicMock(returncode=0)
            p.stdout = asyncio.StreamReader()
            p.stdout.feed_data(
                b'{"event":"init","conversation_id":"conv-acc3"}\n'
                b'{"event":"result","result":{"status":"DONE","response":"survived","usage":{"input_tokens":50,"output_tokens":10}}}\n'
            )
            p.stdout.feed_eof()
            p.stderr = asyncio.StreamReader()
            p.stderr.feed_eof()
            p.wait = AsyncMock(return_value=0)
            p.aclose_lease = AsyncMock()
            return p

        with (
            patch.object(eng, "resolve_agy_bin", return_value=str(fake_bin)),
            patch.object(eng.proxy_router, "ensure_proxy_for_context", AsyncMock(side_effect=_proxy_side_effect)),
            patch.object(eng.proxy_router, "reload_routes_from_db", AsyncMock()),
            patch.object(eng, "create_agy_subprocess_exec", side_effect=_ok_proc),
        ):
            evts = [
                e
                async for e in eng.stream_agy_turn(
                    chat_id="fo-128",
                    kind="regular",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Привет",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                )
            ]
            self.assertEqual(evts[-1]["type"], "done")
            self.assertEqual(evts[-1]["agy_account_id"], acc3["id"])

            # Pinned acc1 does NOT failover and yields structured error event cleanly
            pin_evts = [
                e
                async for e in eng.stream_agy_turn(
                    chat_id="fo-pin-128",
                    kind="regular",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Привет",
                    attachments=[],
                    agy_conversation_id="",
                    preferred_agy_account_id=acc1["id"],
                    history_messages=[],
                )
            ]
            self.assertEqual(pin_evts[-1]["type"], "error")
            self.assertIn("proxy-down", pin_evts[-1]["error"])

        # 3) Hung --resume before init times out cleanly and immediately recovers via __RECOVERY__ in same turn
        acc3_conv_dir = self.config.ensure_agy_account_cli_data_dir(acc3["id"]) / "conversations"
        acc3_conv_dir.mkdir(parents=True, exist_ok=True)
        (acc3_conv_dir / "corrupt-cid.db").write_text("corrupt", encoding="utf-8")

        launch_cmds = []

        async def _hung_then_ok_proc(*cmd_args, **_kw):
            launch_cmds.append(cmd_args)
            p = MagicMock(returncode=0, pid=None)
            p.stdout = asyncio.StreamReader()
            p.stderr = asyncio.StreamReader()
            p.stderr.feed_eof()
            p.wait = AsyncMock(return_value=0)
            p.aclose_lease = AsyncMock()
            if "--conversation" in cmd_args:
                # Never emit init -> triggers STREAM_RESUME_STARTUP_IDLE_TIMEOUT_SEC
                return p
            p.stdout.feed_data(
                b'{"event":"init","conversation_id":"fresh-recovered-cid"}\n'
                b'{"event":"result","result":{"status":"DONE","response":"Recovered OK","usage":{"input_tokens":80,"output_tokens":12}}}\n'
            )
            p.stdout.feed_eof()
            return p

        with (
            patch.object(eng, "resolve_agy_bin", return_value=str(fake_bin)),
            patch.object(eng, "STREAM_RESUME_STARTUP_IDLE_TIMEOUT_SEC", 0.08),
            patch.object(eng.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(eng, "create_agy_subprocess_exec", side_effect=_hung_then_ok_proc),
        ):
            rec_evts = [
                e
                async for e in eng.stream_agy_turn(
                    chat_id="resume-rec-128",
                    kind="regular",
                    project_name="",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Продолжаем работу",
                    attachments=[],
                    agy_conversation_id="corrupt-cid",
                    agy_account_id=acc3["id"],
                    preferred_agy_account_id=acc3["id"],
                    history_messages=[
                        {"id": 1, "role": "user", "status": "done", "content": "Старый вопрос"},
                        {"id": 2, "role": "assistant", "status": "done", "content": "Старый ответ"},
                        {"id": 3, "role": "user", "status": "done", "content": "Продолжаем работу"},
                    ],
                )
            ]
        self.assertEqual(len(launch_cmds), 2)
        self.assertIn("--conversation", launch_cmds[0])
        self.assertNotIn("--conversation", launch_cmds[1])
        self.assertTrue(any(e.get("type") == "account_session_recovered" for e in rec_evts))
        self.assertEqual(rec_evts[-1]["type"], "done")
        self.assertEqual(rec_evts[-1]["agy_conversation_id"], "fresh-recovered-cid")
        self.assertFalse((acc3_conv_dir / "corrupt-cid.db").exists())

    async def test_stage2_context_meter_codex_tokens_recovery_head_tail_and_post_build_spill(self):
        eng, main = self.agy_engine, self.main_mod
        from app import codex_engine

        # 1) Codex token extraction does NOT double-count cachedInputTokens
        codex_u = codex_engine._extract_token_usage_dict(
            {"last": {"inputTokens": 40000, "cachedInputTokens": 35000, "outputTokens": 1200}}
        )
        self.assertEqual(codex_u["input_tokens"], 40000)
        self.assertEqual(codex_u["context_input_tokens"], 40000)
        self.assertEqual(codex_u["cache_read_tokens"], 35000)
        self.assertEqual(codex_u["total_tokens"], 41200)

        codex_stats = eng.compute_chat_context_stats(
            {"id": "cx-1", "model": "codex:gpt-5.4@high", "last_engine": "codex", "codex_thread_id": "th-1"},
            [
                {"id": 1, "role": "user", "status": "done", "content": "Hi"},
                {
                    "id": 2,
                    "role": "assistant",
                    "status": "done",
                    "content": "Done",
                    "usage": {"provider": "chatgpt_codex", **codex_u},
                },
            ],
        )
        self.assertLess(codex_stats["used_tokens"], 45000)

        # 2) UI model switch BEFORE turn (`last_engine == 'agy'`, `model == 'claude-code:...'`) does NOT misread agy usage as claude or apply foreign dyn_win
        mixed_stats = eng.compute_chat_context_stats(
            {
                "id": "mix-1",
                "model": "claude-code:claude-sonnet-4-6@high",
                "last_engine": "agy",
                "agy_conversation_id": "c-agy-1",
            },
            [
                {"id": 1, "role": "user", "status": "done", "content": "Q"},
                {
                    "id": 2,
                    "role": "assistant",
                    "status": "done",
                    "content": "A",
                    "usage": {
                        "provider": "agy",
                        "agy_conversation_id": "c-agy-1",
                        "model_context_window": 50000,
                        "input_tokens": 25000,
                        "cache_read_tokens": 15000,
                        "last_run_input_tokens": 25000,
                        "last_run_cache_tokens": 15000,
                        "output_tokens": 500,
                    },
                },
            ],
        )
        self.assertGreaterEqual(mixed_stats["used_tokens"], 25000)
        self.assertNotEqual(mixed_stats["max_tokens"], 50000)

        # 3) Different agy_conversation_id trajectories after compaction do NOT subtract u_prev across sessions
        traj_stats = eng.compute_chat_context_stats(
            {
                "id": "traj-1",
                "model": "gemini-4-carbon-high",
                "last_engine": "agy",
                "agy_conversation_id": "conv-B",
                "compacted_up_to_msg_id": 5,
                "compacted_summary": "Summary",
            },
            [
                {"id": 6, "role": "user", "status": "done", "content": "Step 1"},
                {
                    "id": 7,
                    "role": "assistant",
                    "status": "done",
                    "content": "R1",
                    "usage": {"provider": "agy", "agy_conversation_id": "conv-A", "input_tokens": 90000, "output_tokens": 1000},
                },
                {"id": 8, "role": "user", "status": "done", "content": "Step 2"},
                {
                    "id": 9,
                    "role": "assistant",
                    "status": "done",
                    "content": "R2",
                    "usage": {
                        "provider": "agy",
                        "agy_conversation_id": "conv-B",
                        "last_run_input_tokens": 15000,
                        "last_run_cache_tokens": 0,
                        "output_tokens": 400,
                    },
                },
            ],
        )
        self.assertGreaterEqual(traj_stats["used_tokens"], 15000)
        self.assertLess(traj_stats["used_tokens"], 25000)

        # 4) merge_assistant_usage cleans foreign provider keys on cross-engine continue_turn
        merged_cross = main.merge_assistant_usage(
            {"provider": "claude_code", "context_window": 200000, "cache_creation_tokens": 5000, "input_tokens": 1000},
            {"provider": "agy", "agy_conversation_id": "c-new", "input_tokens": 2000, "output_tokens": 100},
        )
        self.assertEqual(merged_cross["provider"], "agy")
        self.assertNotIn("context_window", merged_cross)
        self.assertNotIn("cache_creation_tokens", merged_cross)

        # 5) _build_history_recovery_prompt preserves head + FINAL_DECISION tail, attachments & tool facts
        long_asst = "HEAD_ANCHOR_START\n" + ("mid line content\n" * 600) + "FINAL_DECISION_TAIL_OK"
        rec_txt = eng._build_history_recovery_prompt(
            [
                {
                    "id": 1,
                    "role": "user",
                    "status": "done",
                    "content": "Проверь макет",
                    "attachments": [{"name": "mockup.png", "disk_path": "D:/uploads/mockup.png"}],
                },
                {
                    "id": 2,
                    "role": "assistant",
                    "status": "done",
                    "content": long_asst,
                    "tool_calls": [
                        {"name": "edit_file", "state": "DONE", "parameters": {"Path": "app/main.py"}},
                        {"name": "run_command", "state": "DONE", "parameters": {"CommandLine": "pytest -q"}},
                    ],
                },
                {"id": 3, "role": "user", "status": "done", "content": "Дальше"},
            ],
            "Дальше",
        )
        self.assertIn("HEAD_ANCHOR_START", rec_txt)
        self.assertIn("FINAL_DECISION_TAIL_OK", rec_txt)
        self.assertIn("mockup.png", rec_txt)
        self.assertIn("app/main.py", rec_txt)
        self.assertIn("pytest -q -> DONE", rec_txt)

        # 6) Post-build cmdline >30k spill + pagination notice for >780 lines
        fake_bin = self.tmp_root / "fake_agy_spill.exe"
        fake_bin.write_text("bin", encoding="utf-8")
        captured_p = []

        async def _capture_spill_proc(*cmd_args, **_kw):
            captured_p.append(cmd_args[cmd_args.index("-p") + 1])
            p = MagicMock(returncode=0)
            p.stdout = asyncio.StreamReader()
            p.stdout.feed_data(
                b'{"event":"init","conversation_id":"c-sp"}\n'
                b'{"event":"result","result":{"status":"DONE","response":"ok","usage":{}}}\n'
            )
            p.stdout.feed_eof()
            p.stderr = asyncio.StreamReader()
            p.stderr.feed_eof()
            p.wait = AsyncMock(return_value=0)
            p.aclose_lease = AsyncMock()
            return p

        extra_dirs = [self.tmp_root / f"very_long_extra_workspace_directory_number_{i:02d}_padding_xxx" for i in range(45)]
        for d in extra_dirs:
            d.mkdir(exist_ok=True)
        # 26,000 chars < 28,500 pre-check, but with 45 --add-dir + --settings flags list2cmdline > 32,400!
        multiline_26k = "\n".join([f"Line {i}: " + ("x" * 20) for i in range(900)])
        self.assertLess(len(multiline_26k), 28000)

        with (
            patch.object(eng, "resolve_agy_bin", return_value=str(fake_bin)),
            patch.object(eng.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(eng, "create_agy_subprocess_exec", side_effect=_capture_spill_proc),
        ):
            _ = [
                e
                async for e in eng.stream_agy_turn(
                    chat_id="post-spill-128",
                    kind="project",
                    project_name="AlphaProj",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt=multiline_26k,
                    attachments=[],
                    agy_conversation_id="c-existing",
                    history_messages=[],
                    resolved_project_dir=self.root / "AlphaProj",
                    extra_dirs=extra_dirs,
                )
            ]
        self.assertIn("_large_prompt_input_", captured_p[-1])
        self.assertIn("StartLine/EndLine", captured_p[-1])

    # ------------------------------------------------------------------
    # STAGE 3: Compaction & Queue atomicity, deletion safety, RBAC
    # ------------------------------------------------------------------
    async def test_stage3_compaction_atomicity_delete_cancel_and_queue_rbac(self):
        main, db = self.main_mod, self.database
        admin = await db.get_user_by_id("admin")
        guest = await db.create_user(
            username="guest128",
            password="pw128",
            display_name="Guest 128",
            role="user",
            max_access_mode="write_files",
        )
        chat = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="QueueRBAC")
        cid = chat["id"]

        # 1) api_compact_chat queues compaction (never racing) when ACTIVE_TURN_TASKS is active, and raises 409 when COMPACTING_CHATS is active
        blocker = asyncio.Event()
        live_t = asyncio.create_task(blocker.wait())
        main.ACTIVE_TURN_TASKS[cid] = live_t
        try:
            with patch.object(main, "_broadcast_chat_queue", AsyncMock()):
                queued_res = await main.api_compact_chat(cid, current_user=admin)
            self.assertTrue(queued_res["queued"])
            main.CHAT_TURN_QUEUES.pop(cid, None)
        finally:
            main.ACTIVE_TURN_TASKS.pop(cid, None)
            live_t.cancel()
            await asyncio.gather(live_t, return_exceptions=True)

        main.COMPACTING_CHATS.add(cid)
        try:
            with self.assertRaises(main.HTTPException) as ctx409:
                await main.api_compact_chat(cid, current_user=admin)
            self.assertEqual(ctx409.exception.status_code, 409)
        finally:
            main.COMPACTING_CHATS.discard(cid)

        # 2) Deleting chat during compaction cancels COMPACTING_TASKS and does not resurrect messages
        for i in range(4):
            await db.add_message(
                cid,
                role="user" if i % 2 == 0 else "assistant",
                content=f"Msg {i} " * 40,
                usage={"provider": "agy", "input_tokens": 60000, "output_tokens": 1500} if i % 2 == 1 else {},
            )

        started_cmp = asyncio.Event()

        async def _slow_compact(**_kw):
            started_cmp.set()
            await asyncio.sleep(5.0)
            return {"summary": "Summary", "usage": {}, "model": "gemini-4-carbon-high"}

        with (
            patch.object(main, "fetch_available_models", AsyncMock(return_value=self.agy_engine.DEFAULT_MODELS)),
            patch.object(main, "compact_chat_context", side_effect=_slow_compact),
            patch.object(main.ws_manager, "broadcast_for_chat", AsyncMock()),
            patch.object(main.ws_manager, "broadcast_user", AsyncMock()),
        ):
            cmp_task = asyncio.create_task(main._execute_chat_compaction(cid, admin))
            await asyncio.wait_for(started_cmp.wait(), timeout=2.0)
            self.assertIn(cid, main.COMPACTING_CHATS)
            await main.api_delete_chat(cid, current_user=admin)
            with self.assertRaises((asyncio.CancelledError, main.HTTPException)):
                await cmp_task
            self.assertNotIn(cid, main.COMPACTING_CHATS)
            self.assertIsNone(await db.get_chat(cid))

        # 3) Queue drain in _run_turn_task drops blocked/unauthorized guest item WITHOUT running as admin!
        chat2 = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="PrivateAdminTab")
        cid2 = chat2["id"]
        main.CHAT_TURN_QUEUES[cid2] = [
            {
                "id": "q_bad_guest",
                "chat_id": cid2,
                "kind": "message",
                "prompt": "Unauthorized queued turn",
                "user_id": guest["id"],
            }
        ]
        executed_users = []

        async def _fake_inner(**kw):
            executed_users.append((kw["trigger_user"]["id"], kw["prompt"]))

        with (
            patch.object(main, "_run_turn_task_inner", side_effect=_fake_inner),
            patch.object(main, "_broadcast_chat_queue", AsyncMock()),
        ):
            await main._run_turn_task(
                chat_id=cid2,
                prompt="Admin turn",
                attachments=[],
                override_model="gemini-4-carbon-high",
                override_access_mode="read_only",
                trigger_user=admin,
            )
        # Only admin's initial turn ran; guest's unauthorized item was rejected and NOT run as admin!
        self.assertEqual(executed_users, [("admin", "Admin turn")])
        self.assertFalse(main.CHAT_TURN_QUEUES.get(cid2))

        # 4) continue_turn restores referenced_chats from orig_user_msg + dynamic budget + immediate last_engine update
        ref_src = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="RefNeighbor")
        await db.add_message(ref_src["id"], role="user", content="Контекст соседнего чата " + ("Р" * 6000))
        await db.add_message(ref_src["id"], role="assistant", content="Ответ соседнего чата " + ("О" * 6000))

        resume_chat = await db.create_chat(
            kind="project", project_name="AlphaProj", owner_id="admin", title="ResumeTab", model="gemini-4-carbon-high"
        )
        await db.add_message(
            resume_chat["id"],
            role="user",
            content="Задача с @упоминанием",
            usage={"referenced_chats": [{"id": ref_src["id"], "scope": "full"}]},
        )
        interrupted_asst = await db.add_message(
            resume_chat["id"],
            role="assistant",
            content="Частичный ответ",
            status="cancelled",
            model="gemini-4-carbon-high",
        )

        seen_ref_blocks = []

        async def _err_codex_stream(**kw):
            seen_ref_blocks.append(kw.get("referenced_chat_blocks") or [])
            yield {"type": "error", "error": "simulated codex network drop"}

        with (
            patch.object(main, "fetch_available_models", AsyncMock(return_value=self.agy_engine.DEFAULT_MODELS + [{"id": "codex:gpt-5.4@high", "label": "GPT-5.4"}])),
            patch.object(main, "get_cli_auth_status", return_value={"ok": True}),
            patch.object(main.openrouter_service, "is_user_openrouter_bridge_active", AsyncMock(return_value=False)),
            patch.object(main.sandbox_service, "is_user_sandbox_eligible", return_value=False),
            patch.object(main.host_service_manager, "is_user_host_eligible", return_value=False),
            patch.object(main.ws_manager, "broadcast_for_chat", AsyncMock()),
            patch.object(main, "_broadcast_chat_event", AsyncMock()),
            patch.object(
                main,
                "compute_chat_context_stats",
                return_value={"max_tokens": 200000, "used_tokens": 186000, "pct": 93.0, "source": "telemetry"},
            ),
            patch.object(main, "_enforce_user_turn_quotas", AsyncMock(return_value=None)),
            patch.object(main, "stream_codex_turn", side_effect=_err_codex_stream),
        ):
            await main._run_turn_task_inner(
                chat_id=resume_chat["id"],
                prompt="Задача с @упоминанием",
                attachments=[],
                override_model="codex:gpt-5.4@high",
                override_access_mode="read_only",
                trigger_user=admin,
                resume_message_id=interrupted_asst["id"],
            )
        self.assertEqual(len(seen_ref_blocks), 1)
        self.assertEqual(len(seen_ref_blocks[0]), 1)
        # Near-full context (186k / 200k) dynamically caps referenced chat digest (~3600 chars + XML wrapper < 4500)
        self.assertLessEqual(len(seen_ref_blocks[0][0]), 6000)
        # Even though stream_codex_turn errored out, last_engine was persisted to 'codex' immediately!
        self.assertEqual((await db.get_chat(resume_chat["id"]))["last_engine"], "codex")

    # ------------------------------------------------------------------
    # STAGE 4: Claude Windows .cmd injection/length safety & read_only hub_files MCP
    # ------------------------------------------------------------------
    async def test_stage4_claude_cmd_shim_security_and_readonly_hub_files_mcp(self):
        from app import claude_engine, workspace_mcp

        # 1) build_claude_turn_args places security flags FIRST and --append-system-prompt LAST
        sec = {
            "permission_mode": "plan",
            "allowed_tools": ["Read", "Glob"],
            "disallowed_tools": ["Bash", "Write"],
        }
        args = claude_engine.build_claude_turn_args(
            model_slug="claude-sonnet-4-6",
            effort="high",
            sec=sec,
            mcp_config_path=Path("D:/tmp/mcp.json"),
            system_prompt="SYS_PROMPT_TAIL",
        )
        self.assertLess(args.index("--permission-mode"), args.index("--append-system-prompt"))
        self.assertLess(args.index("--strict-mcp-config"), args.index("--append-system-prompt"))
        self.assertLess(args.index("--disallowedTools"), args.index("--append-system-prompt"))
        self.assertEqual(args[-2:], ["--append-system-prompt", "SYS_PROMPT_TAIL"])

        # 2) Windows .cmd shim neutralizes %VAR% / metachars / CRLF and spills >7200 cmdline into stdin
        huge_sys = "Line1\r\n%USERNAME% & calc.exe | echo \"hi\" ^ < >\n" + ("S" * 8000)
        argv, stdin_txt = claude_engine.prepare_claude_turn_argv_and_stdin(
            bin_path=r"C:\Users\admin\AppData\Roaming\npm\claude.cmd",
            model_slug="claude-sonnet-4-6",
            effort="high",
            sec=sec,
            mcp_config_path=Path("D:/tmp/mcp.json"),
            resume_session_id="",
            system_prompt=huge_sys,
            add_dirs=None,
            prompt_text="User question",
        )
        self.assertLessEqual(len(subprocess.list2cmdline(argv)), 7200)
        sys_arg_val = argv[argv.index("--append-system-prompt") + 1]
        self.assertNotIn("\n", sys_arg_val)
        self.assertNotIn("\r", sys_arg_val)
        self.assertNotIn("%USERNAME%", sys_arg_val)
        self.assertIn("User question", stdin_txt)
        self.assertIn("SSSSSSSSSS", stdin_txt)

        # 3) SINGLE_TAB_READ_ONLY_INSTRUCTION has zero toxic mentions; read_only permissions allow hub_files read only
        ro_instr = self.agy_engine.SINGLE_TAB_READ_ONLY_INSTRUCTION
        for banned in ("run_command", "edit_file", "write_file"):
            self.assertNotIn(banned, ro_instr)
        perm_args = self.agy_engine._build_permission_args(
            "project", "read_only", project_dir=self.root / "AlphaProj"
        )
        settings_obj = json.loads(perm_args[perm_args.index("--settings") + 1])
        allows = settings_obj["permissions"]["allow"]
        denies = settings_obj["permissions"]["deny"]
        self.assertIn("mcp_hub_files_read_file", allows)
        self.assertIn("mcp_hub_files_write_file", denies)
        self.assertIn("mcp_hub_files_edit_file", denies)

        # 4) workspace_mcp _user endpoint syncs config/schemas, resolves project run, and blocks write_file even in full_access
        admin = await self.database.get_user_by_id("admin")
        workspace_mcp.sync_user_files_cli_files(admin)
        cli_home = self.config.ensure_user_cli_home(admin)
        mcp_cfg = json.loads((cli_home / ".gemini/config/mcp_config.json").read_text(encoding="utf-8"))
        self.assertIn("hub_files", mcp_cfg["mcpServers"])
        self.assertTrue((self.local_cli / "mcp/hub_files/read_file.json").is_file())
        self.assertFalse((self.local_cli / "mcp/hub_files/write_file.json").exists())

        proj_dir = self.root / "AlphaProj"
        (proj_dir / "hello.txt").write_text("AlphaContent128", encoding="utf-8")
        run_alpha = self.agy_engine.register_chat_turn(
            "run-alpha-128",
            user_id="admin",
            project_name="AlphaProj",
            access_mode="full_access",
        )
        run_alpha.kind = "project"
        run_alpha.project_dir = proj_dir
        run_alpha.engine = "agy"
        try:
            list_resp = await workspace_mcp._dispatch_files_jsonrpc_message(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                admin,
                workspace_mcp.USER_SCOPED_FILES_CHAT_ID,
            )
            tool_names = {t["name"] for t in list_resp["result"]["tools"]}
            self.assertEqual(tool_names, {"read_file", "list_dir", "find_by_name", "grep_search"})

            read_resp = await workspace_mcp._dispatch_files_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "read_file", "arguments": {"path": "hello.txt"}},
                },
                admin,
                workspace_mcp.USER_SCOPED_FILES_CHAT_ID,
            )
            self.assertFalse(read_resp["result"].get("isError", False))
            self.assertIn("AlphaContent128", read_resp["result"]["content"][0]["text"])

            write_resp = await workspace_mcp._dispatch_files_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "write_file", "arguments": {"path": "hack.txt", "content": "bad"}},
                },
                admin,
                workspace_mcp.USER_SCOPED_FILES_CHAT_ID,
            )
            self.assertTrue(write_resp["result"]["isError"])
            self.assertFalse((proj_dir / "hack.txt").exists())
        finally:
            self.agy_engine.clear_chat_turn("run-alpha-128")

    # ------------------------------------------------------------------
    # STAGE 5: Handoff preferred_accounts RBAC, unborn HEAD diff, adaptive HQ directive, idempotency, XML/tags
    # ------------------------------------------------------------------
    async def test_stage5_hq_diff_unborn_head_idempotency_rbac_and_tag_sanitizers(self):
        main, db = self.main_mod, self.database
        admin = await db.get_user_by_id("admin")
        guest = await db.create_user(
            username="guest_s5",
            password="pw128",
            display_name="Guest S5",
            role="user",
            max_access_mode="write_files",
        )
        acc_agy = await self._make_agy_account("pin_s5@gmail.com", "PinS5", make_active=True)

        # 1) compact_tool_calls_payload preserves paths & Move to: renames from apply_patch before stripping patch
        compacted_tc = db.compact_tool_calls_payload(
            [
                {
                    "name": "apply_patch",
                    "state": "DONE",
                    "parameters": {
                        "patch": "*** Begin Patch\n*** Update File: src/old_name.py\n*** Move to: src/new_name.py\n@@\n-a\n+b\n*** End Patch"
                    },
                }
            ]
        )
        self.assertNotIn("patch", compacted_tc[0]["parameters"])
        self.assertEqual(compacted_tc[0]["parameters"]["paths"], ["src/old_name.py", "src/new_name.py"])
        self.assertEqual(
            compacted_tc[0]["parameters"]["renames"],
            [{"from": "src/old_name.py", "to": "src/new_name.py"}],
        )

        # 2) Unborn HEAD git repo + no_git status
        no_git_dir = self.tmp_root / "NoGitProj"
        no_git_dir.mkdir(exist_ok=True)
        (no_git_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
        blk_ng, st_ng = await main._build_hq_return_git_diff_with_status(
            no_git_dir, {"tool_calls": [{"name": "write_file", "parameters": {"Path": "a.py"}}]}
        )
        self.assertEqual(blk_ng, "")
        self.assertEqual(st_ng, "no_git")

        unborn_dir = self.tmp_root / "UnbornGitProj"
        unborn_dir.mkdir(exist_ok=True)
        subprocess.run(["git", "-C", str(unborn_dir), "init"], check=True, capture_output=True, **isolation._subprocess_hidden_kwargs())
        (unborn_dir / "staged.py").write_text("print('staged unborn')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(unborn_dir), "add", "staged.py"], check=True, capture_output=True, **isolation._subprocess_hidden_kwargs())
        blk_ub, st_ub = await main._build_hq_return_git_diff_with_status(
            unborn_dir, {"tool_calls": [{"name": "write_file", "parameters": {"paths": ["staged.py"]}}]}
        )
        self.assertEqual(st_ub, "attached")
        self.assertIn("staged unborn", blk_ub)

        # 3) HQ Return idempotency (even after optimistic client append_message_hq_return with target_msg_id=None!) + adaptive directive
        worker = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="WorkerS5")
        hq = await db.create_chat(kind="project", project_name="AlphaProj", owner_id="admin", title="HQS5")
        w_msg = await db.add_message(
            worker["id"],
            role="assistant",
            content="Реализовано без запуска терминала",
            tool_calls=[{"name": "edit_file", "state": "DONE", "parameters": {"Path": "x.py"}}],
        )
        # Simulate optimistic client REST call BEFORE WS turn runs (target_msg_id=None)
        await db.append_message_hq_return(
            worker["id"],
            w_msg["id"],
            {"target_chat_id": hq["id"], "target_title": "HQS5", "return_uid": "hqr_idem_128"},
        )
        self.assertFalse(await db.has_message_hq_return_uid(worker["id"], w_msg["id"], hq["id"], "hqr_idem_128"))

        seen_prompts = []

        async def _hq_stream(**kw):
            seen_prompts.append(kw["prompt"])
            yield {"type": "done", "content": "Принято", "thinking": "", "tool_calls": [], "usage": {}}

        with (
            patch.object(main, "fetch_available_models", AsyncMock(return_value=self.agy_engine.DEFAULT_MODELS)),
            patch.object(main, "get_cli_auth_status", return_value={"ok": True}),
            patch.object(main.openrouter_service, "is_user_openrouter_bridge_active", AsyncMock(return_value=False)),
            patch.object(main.sandbox_service, "is_user_sandbox_eligible", return_value=False),
            patch.object(main.host_service_manager, "is_user_host_eligible", return_value=False),
            patch.object(main, "_refresh_and_broadcast_quotas", AsyncMock()),
            patch.object(main.ws_manager, "broadcast_for_chat", AsyncMock()),
            patch.object(main, "stream_agy_turn", side_effect=_hq_stream),
        ):
            hq_src = {"chat_id": worker["id"], "message_id": w_msg["id"], "review_mode": "fast", "return_uid": "hqr_idem_128"}
            await main._run_turn_task_inner(
                chat_id=hq["id"],
                prompt="Чистый текст отчёта",
                attachments=[],
                override_model="gemini-4-carbon-high",
                override_access_mode="read_only",
                trigger_user=admin,
                hq_return_source=hq_src,
            )
            # Duplicate replay with same return_uid MUST be ignored!
            await main._run_turn_task_inner(
                chat_id=hq["id"],
                prompt="Чистый текст отчёта",
                attachments=[],
                override_model="gemini-4-carbon-high",
                override_access_mode="read_only",
                trigger_user=admin,
                hq_return_source=hq_src,
            )

        self.assertEqual(len(seen_prompts), 1)
        self.assertIn("терминальные команды и автотесты в рабочей вкладке не запускались", seen_prompts[0])
        hq_user_msgs = [m for m in await db.get_messages(hq["id"]) if m["role"] == "user"]
        self.assertEqual(len(hq_user_msgs), 1)
        self.assertEqual(hq_user_msgs[0]["content"], "Чистый текст отчёта")

        # 4) _apply_turn_preferred_accounts RBAC: guest cannot pin admin Google account or foreign Codex account
        guest_chat = await db.create_chat(kind="regular", owner_id=guest["id"], title="GuestTab")
        updated_guest_chat = await main._apply_turn_preferred_accounts(
            guest_chat,
            {
                "preferred_agy_account_id": acc_agy["id"],
                "preferred_codex_account_id": "cgpt_foreign_nonexistent",
                "preferred_claude_account_id": "claude_foreign_nonexistent",
            },
            guest,
            None,
        )
        self.assertEqual(updated_guest_chat["preferred_agy_account_id"], "")
        self.assertEqual(updated_guest_chat["preferred_codex_account_id"], "")
        self.assertEqual(updated_guest_chat["preferred_claude_account_id"], "")

        # 5) Balanced FORWARDED_MESSAGES & _strip_injected_prompt_tags
        nested_fwd = (
            'Комментарий пользователя\n\n'
            '<FORWARDED_MESSAGES source="Чат А" count="2">\n'
            '[Сообщение 1 · Пользователь]:\n'
            'Пример тега: </FORWARDED_MESSAGES> внутри кода\n\n---\n\n'
            '[Сообщение 2 · Ассистент]:\n'
            'Финальный вывод пересылки\n'
            '</FORWARDED_MESSAGES>'
        )
        blocks = db.find_forwarded_messages_blocks(nested_fwd)
        self.assertEqual(len(blocks), 1)
        self.assertIn("Финальный вывод пересылки", blocks[0]["inner"])
        self.assertEqual(main._make_auto_title(nested_fwd), "Комментарий пользователя")

        dirty_prompt = (
            '<REPLY_QUOTE>[Контекст ответа: Ассистент]\nВажная строка цитаты</REPLY_QUOTE>\n'
            'Основной вопрос\n\n'
            '🔍 [Режим код-ревью Штаба: служебная директива]\n\n'
            '<HQ_RETURN_GIT_DIFF files="1">\n+secret diff\n</HQ_RETURN_GIT_DIFF>'
        )
        cleaned_prompt = db._strip_injected_prompt_tags(dirty_prompt)
        self.assertIn("> Важная строка цитаты", cleaned_prompt)
        self.assertIn("Основной вопрос", cleaned_prompt)
        self.assertNotIn("HQ_RETURN_GIT_DIFF", cleaned_prompt)
        self.assertNotIn("Режим код-ревью Штаба", cleaned_prompt)

    def test_stage5_and_stage3_frontend_node_vm_v128(self):
        static_dir = isolation.ROOT_DIR / "app" / "static"
        html_txt = (static_dir / "index.html").read_text(encoding="utf-8")
        sw_txt = (static_dir / "sw.js").read_text(encoding="utf-8")
        import re
        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_txt)
        self.assertIsNotNone(m_sw)
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 129)
        self.assertIn(f"styles.css?v={ver}", html_txt)
        self.assertIn(f"app.js?v={ver}", html_txt)

        tree = ast.parse(Path(isolation.__file__).read_text(encoding="utf-8"))
        harness = next(
            n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "node_harness" for t in n.targets)
        )
        harness += r"""
// 1) Unified <TASK_PROFILE> extraction & stripping with outer ```json fence and >800 chars
const padNotes = "x".repeat(880);
const fencedTp = `Готовый план работ.\n\n\`\`\`json\n<TASK_PROFILE>\n{"complexity":"heavy","type":"backend","files":6,"risk":"high","needs_terminal":true,"notes":"${padNotes}"}\n</TASK_PROFILE>\n\`\`\``;
const prof = api.extractTaskProfileFromText(fencedTp);
assert.ok(prof, "extractTaskProfileFromText must parse fenced >800 char profile");
assert.equal(prof.complexity, "heavy");
assert.equal(prof.files, 6);
const strippedTp = api.stripTaskProfileFromText(fencedTp);
assert.equal(strippedTp, "Готовый план работ.");
assert.ok(!strippedTp.includes("TASK_PROFILE"));
assert.ok(!strippedTp.includes("```"));

// 2) Move to: & params.renames in buildExecutionFactSummaryFromMsg + clean buildReturnToHqPrompt
const moveMsg = {
  content: "Переименовал модуль",
  status: "done",
  tool_calls: [
    { name: "apply_patch", state: "DONE", parameters: { patch: "*** Update File: a.py\n*** Move to: b.py" } },
    { name: "apply_patch", state: "DONE", parameters: { renames: [{ from: "c.py", to: "d.py" }] } }
  ]
};
const moveSum = api.buildExecutionFactSummaryFromMsg(moveMsg, { title: "CodeTab", model: "gemini-4-carbon-high" });
assert.ok(moveSum.files.includes("a.py") && moveSum.files.includes("b.py"));
assert.ok(moveSum.files.includes("c.py") && moveSum.files.includes("d.py"));
const cleanHqBody = api.buildReturnToHqPrompt(moveMsg, { title: "CodeTab", model: "gemini-4-carbon-high" });
assert.ok(!cleanHqBody.includes("[Режим быстрой приёмки Штаба"));

// 3) Nested <FORWARDED_MESSAGES> escaping + balanced parser + strict resolveLiveSourceChatId (no false title fallback)
const xmlBlock = api.buildForwardedMessagesXmlBlock({
  sourceTitle: "Чат Источник",
  items: [
    { role: "user", authorLabel: "Админ", text: "Код с </FORWARDED_MESSAGES> и <FORWARDED_MESSAGES source=\"inner\">внутри</FORWARDED_MESSAGES>" },
    { role: "assistant", model: "Gemini", text: "Второй пункт без обрезки" }
  ]
});
assert.equal((xmlBlock.match(/<\/FORWARDED_MESSAGES>/gi) || []).length, 1);

api.state.chats = [{ id: "live-same-title", title: "Удалённый чат", project_name: "AlphaProj" }];
api.state.deletedChatIds.add("del-src-id");

const envDeleted = api.parseForwardedEnvelope({
  role: "user",
  content: "Заметка\n\n" + xmlBlock,
  usage: { handoff_meta: { kind: "forwarded_messages", source_chat_id: "del-src-id", source_title: "Удалённый чат", messages_count: 2 } }
}, "AlphaProj");
assert.equal(envDeleted.sourceChatId, "", "Deleted source_chat_id must NOT fall back to live chat with same title!");
assert.ok(envDeleted.bodyMarkdown.includes("Второй пункт без обрезки"));

const envDesktop = api.parseForwardedEnvelope({
  role: "user",
  content: "Заметка\n\n" + xmlBlock,
  usage: { handoff_meta: { kind: "forwarded_messages", source_chat_id: "desktop:conv123", source_title: "Удалённый чат", messages_count: 2 } }
}, "AlphaProj");
assert.equal(envDesktop.sourceChatId, "", "desktop:* source_chat_id must NOT fall back to Hub chat by title!");

// 4) chat_compacting updates state.activeChat.is_compacting and tabViewCache entry
api.state.activeChatId = "cmp-live-128";
api.state.activeChat = { id: "cmp-live-128", kind: "project", is_generating: false, is_compacting: false };
api.tabViewCache.set("cmp-live-128", { chat: { id: "cmp-live-128", is_compacting: false }, messages: [] });
api.applyLiveChatEventToCachedTab("cmp-live-128", { type: "chat_compacting", chat_id: "cmp-live-128", active: true });
assert.equal(api.tabViewCache.get("cmp-live-128").chat.is_compacting, true);
api.applyLiveChatEventToCurrentState({ type: "chat_compacting", chat_id: "cmp-live-128", active: true }, { skipDom: true });
assert.equal(api.state.activeChat.is_compacting, true);
api.applyLiveChatEventToCurrentState({ type: "chat_compacting", chat_id: "cmp-live-128", active: false }, { skipDom: true });
assert.equal(api.state.activeChat.is_compacting, false);
assert.equal(api.tabViewCache.get("cmp-live-128").chat.is_compacting, false);

// 5) executeSendToExistingCodeTab does NOT fire premature PATCH on busy/compactFirst tab and passes preferred_accounts in WS payload
(async () => {
  const fetchCalls = [];
  const wsSent = [];
  sandbox.WebSocket = { OPEN: 1 };
  sandbox.fetch = async (url, init) => {
    fetchCalls.push({ url: String(url), method: (init && init.method) || "GET", body: init && init.body });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  api.state.ws = { readyState: 1, send: (raw) => wsSent.push(JSON.parse(raw)) };
  api.state.currentUser = { id: "admin", role: "admin", max_access_mode: "host_full" };
  const busyTarget = { id: "busy-code-128", title: "Код", kind: "project", project_name: "AlphaProj", model: "gemini-4-carbon-high", access_mode: "write_files", preferred_agy_account_id: "agy_old", is_generating: true };
  api.state.generatingChats.add("busy-code-128");
  await api.executeSendToExistingCodeTab(busyTarget, "План работ", "ШТАБ", {
    model: "gemini-4-carbon-high",
    accessMode: "write_files",
    preferredAccounts: { agy: "agy_new_pin" },
    compactFirst: true
  });
  assert.equal(busyTarget.preferred_agy_account_id, "agy_old", "Busy tab must not mutate preferred_agy_account_id before queue turn starts!");
  assert.equal(fetchCalls.filter((c) => c.method === "PATCH").length, 0, "Busy tab must not fire premature PATCH!");
  assert.equal(wsSent.length, 1);
  assert.deepEqual(wsSent[0].preferred_accounts, { preferred_agy_account_id: "agy_new_pin" });
  console.log("V128_NODE_VM_OK");
  process.exit(0);
})().catch((err) => { console.error(err); process.exit(1); });
"""
        vm_script = self.tmp_root / "v128_vm.js"
        vm_script.write_text(harness, encoding="utf-8")
        vm_res = subprocess.run(
            ["node", str(vm_script), str(static_dir / "app.js")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
            **isolation._subprocess_hidden_kwargs(),
        )
        self.assertEqual(vm_res.returncode, 0, vm_res.stderr)
        self.assertIn("V128_NODE_VM_OK", vm_res.stdout)


if __name__ == "__main__":
    unittest.main()
