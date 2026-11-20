"""v134: Google AI quota polling/cache fixes and multi-step Gemini High post-tool budget."""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import test_audit_fixes_v128 as isolated


def test_looks_like_completed_response_accepts_closing_task_profile_tag():
    from app.agy_engine import looks_like_completed_response

    spec_body = (
        "### ТЗ v=134: Исправление опроса квот и post-tool шага Gemini High\n\n"
        "1. Развести TelemetryDeferred и реальные ошибки авторизации CLI.\n"
        "2. Выполнять /usage и /credits строго последовательно с таймаутом 30 секунд.\n"
        '<TASK_PROFILE>{"complexity":"medium","type":"backend","files":3,"risk":"medium","needs_terminal":true}</TASK_PROFILE>'
    )
    assert len(spec_body) >= 180
    assert looks_like_completed_response(spec_body) is True
    # Unclosed code fence before </TASK_PROFILE> must still be rejected
    broken_fence = spec_body.replace("1. Развести", "```python\n1. Развести")
    assert looks_like_completed_response(broken_fence) is False


def test_slash_timeout_and_lease_release_on_command_result_and_result(monkeypatch):
    from app import agy_credentials as creds
    from app import agy_engine as engine

    assert engine.SLASH_COMMAND_TIMEOUT_SEC == 30.0

    async def _run():
        monkeypatch.setattr(creds, "_lease_lock", asyncio.Lock())
        for terminal_evt in (
            {"event": "command_result", "command": {"name": "usage", "data": {"groups": []}}},
            {"event": "result", "result": {"status": "SUCCESS", "command": {"data": {"remaining_credits": 0}}}},
        ):
            reader = asyncio.StreamReader()
            reader.feed_data((json.dumps(terminal_evt) + "\n").encode("utf-8"))
            still_running = asyncio.Event()
            proc = MagicMock(stdout=reader, returncode=None)
            proc.wait = AsyncMock(side_effect=still_running.wait)
            restore = MagicMock()
            await creds._lease_lock.acquire()
            leased = creds.LeasedProcess(proc, restore)
            line = await leased.stdout.readline()
            assert line
            # Lease must be released immediately on command_result / result before EOF!
            assert not creds._lease_lock.locked()
            restore.assert_called_once()
            still_running.set()
            await leased.finish_lease()

    asyncio.run(_run())


class QuotasAndMultistepHighV134Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolated.AuditFixesV128Tests.setUpClass.__func__)
    tearDownClass = classmethod(isolated.AuditFixesV128Tests.tearDownClass.__func__)
    asyncSetUp = isolated.AuditFixesV128Tests.asyncSetUp
    asyncTearDown = isolated.AuditFixesV128Tests.asyncTearDown
    _assert_live_db_untouched = isolated.AuditFixesV128Tests._assert_live_db_untouched
    _make_agy_account = isolated.AuditFixesV128Tests._make_agy_account

    @staticmethod
    def _make_stream_proc_for_slash(cmd_args: tuple, *, on_read=None):
        slash = cmd_args[cmd_args.index("-p") + 1]
        proc = MagicMock(returncode=0)
        reader = asyncio.StreamReader()
        if slash == "/usage":
            payload = {
                "event": "command_result",
                "command": {
                    "name": "usage",
                    "data": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": "gemini-5h",
                                        "window": "5h",
                                        "remaining_fraction": 0.973,
                                        "reset_time": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
                                    },
                                    {
                                        "id": "gemini-weekly",
                                        "window": "weekly",
                                        "remaining_fraction": 0.989,
                                        "reset_time": (datetime.now(timezone.utc) + timedelta(days=6)).isoformat(),
                                    },
                                ],
                            }
                        ]
                    },
                },
            }
        else:
            payload = {
                "event": "command_result",
                "command": {"name": "credits", "data": {"remaining_credits": 250}},
            }
        reader.feed_data((json.dumps(payload) + "\n").encode("utf-8"))
        reader.feed_eof()

        if on_read is not None:
            orig_readline = reader.readline

            async def _tracked_readline():
                on_read(slash)
                return await orig_readline()

            reader.readline = _tracked_readline  # type: ignore[method-assign]

        proc.stdout = reader
        proc.stderr = asyncio.StreamReader()
        proc.stderr.feed_eof()
        proc.wait = AsyncMock(return_value=0)
        proc.aclose_lease = AsyncMock()
        return proc

    async def test_refresh_all_quotas_overwrites_cached_error_and_runs_sequentially(self):
        e = self.agy_engine
        acc = await self._make_agy_account("o01036269952@example.test", "Main", make_active=True)
        fake_bin = self.tmp_root / "agy_v134.exe"
        fake_bin.write_text("fake", encoding="utf-8")

        # Seed a fresh (< 15m) failed quota snapshot in DB to reproduce sticky ok:False bug
        stale_err = {
            "ok": False,
            "error": "Не удалось получить квоты (проверьте вход в аккаунт CLI)",
            "groups": [],
            "remaining_credits": 0,
        }
        await self.database.update_agy_account_telemetry(
            acc["id"], quotas=stale_err, last_error=stale_err["error"]
        )

        slash_order: list[str] = []
        priorities: list[str] = []
        readline_hits: list[str] = []
        in_flight = 0
        max_in_flight = 0

        async def _spawn(*args, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.02)
                slash = args[args.index("-p") + 1]
                slash_order.append(slash)
                priorities.append(str(kwargs.get("priority") or ""))
                return self._make_stream_proc_for_slash(args, on_read=readline_hits.append)
            finally:
                in_flight -= 1

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(fake_bin)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn),
            patch.object(e, "sync_refreshed_agy_credentials_back", AsyncMock()),
        ):
            # 1) Even non-forced fetch must NOT treat `ok: False` as a fresh 15m cache
            non_forced = await e.fetch_account_quotas(False, acc["id"])
            self.assertTrue(non_forced["ok"])
            self.assertEqual(slash_order, ["/usage", "/credits"])
            self.assertEqual(max_in_flight, 1)
            self.assertEqual(readline_hits, ["/usage", "/credits"])

            # Put ok: False back again and test manual button `refresh_all_agy_accounts_quotas()`
            await self.database.update_agy_account_telemetry(
                acc["id"], quotas=stale_err, last_error=stale_err["error"]
            )
            slash_order.clear()
            priorities.clear()
            refreshed = await e.refresh_all_agy_accounts_quotas()

        self.assertTrue(refreshed["ok"])
        self.assertEqual(refreshed["gemini_5h_pct"], 97.3)
        self.assertEqual(refreshed["remaining_credits"], 250)
        # Active account was refreshed once (/usage then /credits), not polled twice!
        self.assertEqual(slash_order, ["/usage", "/credits"])
        self.assertEqual(priorities, ["manual_telemetry", "manual_telemetry"])
        self.assertEqual(max_in_flight, 1)
        saved_acc = await self.database.get_account_profile_by_id(acc["id"])
        self.assertTrue(saved_acc["quotas"]["ok"])
        self.assertEqual(saved_acc["last_error"], "")

    async def test_deferred_telemetry_preserves_db_while_manual_refresh_works_during_turn(self):
        from app import agy_credentials as creds

        e = self.agy_engine
        acc = await self._make_agy_account("active-turn@example.test", "Working", make_active=True)
        acc_empty = await self._make_agy_account("empty-turn@example.test", "EmptyAcc", make_active=False)
        fake_bin = self.tmp_root / "agy_turn_v134.exe"
        fake_bin.write_text("fake", encoding="utf-8")

        good_quotas = {
            "ok": True,
            "error": "",
            "groups": [{"id": "gemini", "buckets": [{"window": "5h", "remaining_pct": 84.0}]}],
            "remaining_credits": 100,
            "gemini_5h_pct": 84.0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.database.update_agy_account_telemetry(acc["id"], quotas=good_quotas, last_error="")
        await self.database.update_agy_account_telemetry(acc_empty["id"], quotas={}, last_error="")
        # Force cached_at to look expired (> 15m) so background poll tries to refresh
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        async with self.database.connect_db() as conn:
            await conn.execute("UPDATE account_profiles SET cached_at = ?", (old_ts,))
            await conn.commit()

        spawn_calls: list[tuple] = []

        async def _spawn_if_allowed(*args, **kwargs):
            if kwargs.get("priority") == "telemetry" and creds.active_working_turns:
                raise creds.TelemetryDeferred()
            spawn_calls.append((args, kwargs.get("priority")))
            return self._make_stream_proc_for_slash(args)

        prev_turns = creds.active_working_turns
        try:
            # Case A: active_working_turns == 1 from the start
            creds.active_working_turns = 1
            with (
                patch.object(e, "resolve_agy_bin", return_value=str(fake_bin)),
                patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
                patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn_if_allowed),
                patch.object(e, "sync_refreshed_agy_credentials_back", AsyncMock()),
            ):
                bg_res = await e.fetch_account_quotas(False, acc["id"])
                self.assertEqual(bg_res["gemini_5h_pct"], 84.0)
                self.assertEqual(spawn_calls, [])

                bg_empty = await e.fetch_account_quotas(False, acc_empty["id"])
                self.assertFalse(bg_empty["ok"])
                self.assertTrue(bg_empty.get("deferred"))
                self.assertEqual(bg_empty["error"], "")

                # Case B: Race where active_working_turns flips 0 -> 1 inside _fetch_account_quotas
                creds.active_working_turns = 0
                orig_mat = e.materialize_agy_account_home

                async def _mat_then_turn_starts(*m_args, **m_kwargs):
                    home_p = await orig_mat(*m_args, **m_kwargs)
                    creds.active_working_turns = 1
                    return home_p

                with patch.object(e, "materialize_agy_account_home", side_effect=_mat_then_turn_starts):
                    race_res = await e.fetch_account_quotas(False, acc["id"])
                self.assertEqual(race_res["gemini_5h_pct"], 84.0)
                self.assertEqual(spawn_calls, [])

                # Verify DB was NOT overwritten with fake CLI auth error!
                db_acc = await self.database.get_account_profile_by_id(acc["id"])
                self.assertTrue(db_acc["quotas"]["ok"])
                self.assertEqual(db_acc["quotas"]["gemini_5h_pct"], 84.0)
                self.assertEqual(db_acc["last_error"], "")

                db_empty = await self.database.get_account_profile_by_id(acc_empty["id"])
                self.assertEqual(db_empty["last_error"], "")

                # Case C: Manual refresh (`force_refresh=True`) while active_working_turns == 1 succeeds!
                manual_res = await e.fetch_account_quotas(True, acc["id"])
                self.assertTrue(manual_res["ok"])
                self.assertEqual(manual_res["gemini_5h_pct"], 97.3)
                self.assertEqual([p for _, p in spawn_calls], ["manual_telemetry", "manual_telemetry"])
        finally:
            creds.active_working_turns = prev_turns

    async def test_admin_manual_quotas_api_serializes_quotas_before_models(self):
        order: list[str] = []
        active_agy = 0
        overlap_detected = False

        async def _fake_refresh_all(*, force_refresh: bool = True, fetch_fn=None):
            nonlocal active_agy, overlap_detected
            assert force_refresh is True
            active_agy += 1
            if active_agy > 1:
                overlap_detected = True
            order.append("quotas_start")
            await asyncio.sleep(0.02)
            order.append("quotas_end")
            active_agy -= 1
            return {"ok": True, "gemini_5h_pct": 95.0, "groups": []}

        async def _fake_models(force_refresh: bool = False):
            nonlocal active_agy, overlap_detected
            if force_refresh:
                active_agy += 1
                if active_agy > 1:
                    overlap_detected = True
                order.append("models_start")
                await asyncio.sleep(0.01)
                order.append("models_end")
                active_agy -= 1
            return [{"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)", "family": "gemini"}]

        self.main_mod._last_manual_quota_refresh_mono.clear()
        with (
            patch.object(self.agy_engine, "refresh_all_agy_accounts_quotas", side_effect=_fake_refresh_all),
            patch.object(self.main_mod, "fetch_available_models", side_effect=_fake_models),
            patch.object(self.main_mod.ws_manager, "broadcast_models_per_user", AsyncMock()),
            patch.object(self.main_mod.ws_manager, "broadcast_admins", AsyncMock()),
        ):
            resp = await self.main_mod.api_get_quotas(
                refresh=True,
                chat_id=None,
                current_user={"id": "admin", "username": "admin", "role": "admin"},
            )
        self.assertFalse(overlap_detected)
        self.assertEqual(order, ["quotas_start", "quotas_end", "models_start", "models_end"])
        self.assertEqual(resp["quotas"]["gemini_5h_pct"], 95.0)

    async def test_multistep_gemini_high_survives_over_170s_post_tool_thinking_pause(self):
        import time as real_time_mod
        from types import SimpleNamespace

        e = self.agy_engine
        account = await self._make_agy_account("multistep@example.test", "MultiStep", make_active=True)
        binary = self.tmp_root / "fake_multistep.exe"
        binary.write_text("fake", encoding="utf-8")

        clock_offset = [0.0]
        fake_time_ns = SimpleNamespace(
            monotonic=lambda: real_time_mod.monotonic() + clock_offset[0],
            time=real_time_mod.time,
            time_ns=real_time_mod.time_ns,
            sleep=real_time_mod.sleep,
        )

        calls: list[tuple] = []
        step2_released = asyncio.Event()
        bg_tasks: list[asyncio.Task] = []

        async def _spawn(*args, **kwargs):
            calls.append(args)
            proc = MagicMock(returncode=0)
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=0)
            proc.aclose_lease = AsyncMock()

            log_path = Path(args[args.index("--log-file") + 1])
            transcript = log_path.parent.parent / "brain/ms-conv/.system_generated/logs/transcript.jsonl"
            transcript.parent.mkdir(parents=True, exist_ok=True)

            # Step 1: init -> read_file ACTIVE -> read_file DONE
            for ev in (
                {"event": "init", "conversation_id": "ms-conv"},
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "step_type": "tool",
                        "state": "ACTIVE",
                        "tool_name": "read_file",
                        "tool_info": {"name": "read_file", "parameters": {"Path": "a.py"}},
                    },
                },
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "step_type": "tool",
                        "state": "DONE",
                        "tool_name": "read_file",
                        "tool_info": {"name": "read_file", "parameters": {"Path": "a.py"}, "output": "ok"},
                    },
                },
            ):
                proc.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))

            async def _step2_after_200s():
                # Let stream_agy_turn enter post-tool readline wait
                await asyncio.sleep(0.02)
                # Advance engine monotonic clock by +205s (> 165s STREAM_NETWORK_IDLE_TIMEOUT_SEC, < 360s High budget!)
                clock_offset[0] += 205.0
                transcript.write_text(
                    json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "check"}) + "\n"
                    + json.dumps({"type": "PLANNER_RESPONSE", "source": "MODEL", "thinking": "step 1 thought"}) + "\n"
                    + json.dumps({"type": "PLANNER_RESPONSE", "source": "MODEL", "thinking": "step 2 deep high thought"}) + "\n",
                    encoding="utf-8",
                )
                await asyncio.wait_for(step2_released.wait(), timeout=2.0)
                for ev2 in (
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 2,
                            "step_type": "agent_response",
                            "state": "DONE",
                            "text_delta": "Final multi-step answer.",
                        },
                    },
                    {"event": "result", "result": {"status": "DONE"}},
                ):
                    proc.stdout.feed_data((json.dumps(ev2) + "\n").encode("utf-8"))
                proc.stdout.feed_eof()

            bg_tasks.append(asyncio.create_task(_step2_after_200s()))
            return proc

        events: list[dict] = []
        with (
            patch.object(e, "resolve_agy_bin", return_value=str(binary)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn),
            patch.object(e, "STREAM_DISK_POLL_SEC", 0.01),
            patch.object(e, "time", fake_time_ns),
        ):
            async for evt in e.stream_agy_turn(
                chat_id="multistep134",
                kind="regular",
                project_name="",
                model="gemini-4-carbon-high",
                access_mode="read_only",
                prompt="Analyze files",
                attachments=[],
                agy_conversation_id="",
                history_messages=[],
                preferred_agy_account_id=account["id"],
            ):
                events.append(evt)
                if (
                    evt.get("type") == "thinking_update"
                    and "step 2 deep high thought" in str(evt.get("thinking") or "")
                    and not step2_released.is_set()
                ):
                    step2_released.set()

        await asyncio.gather(*bg_tasks)
        self.assertEqual(len(calls), 1, "Must NOT kill and restart CLI after 205s post-tool High thinking!")
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "gemini-4-carbon-high")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["content"], "Final multi-step answer.")
        self.assertIn("step 2 deep high thought", events[-1]["thinking"])

    async def test_delayed_result_after_task_profile_completes_via_post_text_idle(self):
        e = self.agy_engine
        account = await self._make_agy_account("plan134@example.test", "PlanTab", make_active=True)
        binary = self.tmp_root / "fake_plan.exe"
        binary.write_text("fake", encoding="utf-8")

        plan_text = (
            "### Готовое ТЗ v=134\n\n"
            "Выполнить последовательно в одном процессе исправление квот и бюджета второго шага Gemini High.\n"
            "Проверить неизменность боевой базы данных и прогнать все регрессионные тесты.\n"
            '<TASK_PROFILE>{"complexity":"medium","type":"backend","files":3,"risk":"low","needs_terminal":true}</TASK_PROFILE>'
        )
        calls: list[tuple] = []

        async def _spawn(*args, **kwargs):
            calls.append(args)
            proc = MagicMock(returncode=None)
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            proc.stderr.feed_eof()
            for ev in (
                {"event": "init", "conversation_id": "plan-conv"},
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 1,
                        "step_type": "agent_response",
                        "text_delta": plan_text,
                    },
                },
            ):
                proc.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
            # Intentionally omit {"event": "result"} and EOF so post_text_idle fires!
            proc.wait = AsyncMock(return_value=0)
            proc.aclose_lease = AsyncMock()
            return proc

        with (
            patch.object(e, "resolve_agy_bin", return_value=str(binary)),
            patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
            patch.object(e, "create_agy_subprocess_exec", side_effect=_spawn),
            patch.object(e, "_terminate_hung_cli_proc", AsyncMock()),
            patch.object(e, "STREAM_POST_TEXT_IDLE_TIMEOUT_SEC", 0.03),
            patch.object(e, "STREAM_DISK_POLL_SEC", 0.01),
        ):
            events = [
                evt
                async for evt in e.stream_agy_turn(
                    chat_id="plan134",
                    kind="project",
                    project_name="Antigravity_Hub",
                    model="gemini-4-carbon-high",
                    access_mode="read_only",
                    prompt="Сформируй ТЗ",
                    attachments=[],
                    agy_conversation_id="",
                    history_messages=[],
                    preferred_agy_account_id=account["id"],
                )
            ]

        self.assertEqual(len(calls), 1, "Must finish via post_text_idle, NOT auto-continue!")
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(events[-1]["content"].endswith("</TASK_PROFILE>"))
