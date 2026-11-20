"""v132: proxy/log regressions and rollback of automatic TTFT fallback."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import test_audit_fixes_v128 as isolated


def test_loopback_preserves_user_bypass(monkeypatch, tmp_path):
    from app.proxy_router import apply_proxy_to_subprocess_env
    env = {"NO_PROXY": "example.test", "no_proxy": "internal.test", "ALL_PROXY": "old"}
    apply_proxy_to_subprocess_env(env, "http://127.0.0.1:1234")
    assert env["NO_PROXY"] == env["no_proxy"]
    assert set(env["NO_PROXY"].split(",")) >= {"localhost", "127.0.0.1", "::1", "example.test", "internal.test"}
    assert "ALL_PROXY" not in env
    apply_proxy_to_subprocess_env(env, None)
    assert env["NO_PROXY"] == "*"
    apply_proxy_to_subprocess_env(env, "http://127.0.0.1:4321")
    assert "*" not in env["NO_PROXY"]


def test_log_diagnostics_are_bounded_fresh_and_secret_free(tmp_path, monkeypatch):
    from app.agy_engine import _latest_cli_failure
    p = tmp_path / ".gemini/antigravity-cli/log/cli-test.log"
    p.parent.mkdir(parents=True)
    p.write_text('E1117 22:00:00 503 capacity access_token=secret bearer PRIVATE email@example.test', encoding="utf-8")
    assert _latest_cli_failure(tmp_path, 0) == "Google capacity / 503"
    assert _latest_cli_failure(tmp_path, p.stat().st_mtime + 1) == ""
    p.write_text("E1117 22:00:00 Clearcut responded with HTTP code: 503", encoding="utf-8")
    assert _latest_cli_failure(tmp_path, 0) == ""


class ResponsivenessV132Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolated.AuditFixesV128Tests.setUpClass.__func__)
    tearDownClass = classmethod(isolated.AuditFixesV128Tests.tearDownClass.__func__)
    asyncSetUp = isolated.AuditFixesV128Tests.asyncSetUp
    asyncTearDown = isolated.AuditFixesV128Tests.asyncTearDown
    _assert_live_db_untouched = isolated.AuditFixesV128Tests._assert_live_db_untouched
    _make_agy_account = isolated.AuditFixesV128Tests._make_agy_account

    async def test_selected_high_model_waits_without_injected_retry_notice(self):
        e = self.agy_engine
        account = await self._make_agy_account('rollback@example.test', 'Selected', make_active=True)
        binary = self.tmp_root / 'fake.exe'
        binary.write_text('fake', encoding='utf-8')
        calls = []
        async def spawn(*args, **kwargs):
            calls.append(args)
            proc = MagicMock(returncode=0)
            class Reader(asyncio.StreamReader):
                async def readline(self):
                    pending_reads.append(None)
                    return await super().readline()
            proc.stdout = Reader()
            from pathlib import Path
            log_path = Path(args[args.index("--log-file") + 1])
            transcript = log_path.parent.parent / "brain/waiting/.system_generated/logs/transcript.jsonl"
            proc.stdout.feed_data(b'{"event":"init","conversation_id":"waiting"}\n')
            async def response():
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(json.dumps({
                    "type": "PLANNER_RESPONSE", "source": "MODEL", "thinking": "live reasoning",
                }) + "\n", encoding="utf-8")
                try:
                    await asyncio.wait_for(allow_stdout.wait(), 2)
                except TimeoutError:
                    proc.stdout.feed_eof()
                    raise
                proc.stdout.feed_data(b'{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"OK"}}\n'
                                     b'{"event":"result","result":{"status":"DONE"}}\n')
                proc.stdout.feed_eof()
            response_task = asyncio.create_task(response())
            tasks.append(response_task)
            proc.stderr = asyncio.StreamReader()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=0)
            proc.aclose_lease = AsyncMock()
            return proc
        tasks = []
        pending_reads = []
        allow_stdout = asyncio.Event()
        with (patch.object(e, 'resolve_agy_bin', return_value=str(binary)),
              patch.object(e.proxy_router, 'ensure_proxy_for_context', AsyncMock(return_value=None)),
              patch.object(e, 'create_agy_subprocess_exec', side_effect=spawn),
              patch.object(e, 'STREAM_DISK_POLL_SEC', .01)):
            events = []
            async for event in e.stream_agy_turn(chat_id='rollback132', kind='regular',
                project_name='', model='gemini-4-carbon-high', access_mode='read_only', prompt='hello',
                attachments=[], agy_conversation_id='', history_messages=[], preferred_agy_account_id=account['id']):
                events.append(event)
                if not allow_stdout.is_set() and event.get("thinking") == "live reasoning" and event["type"] == "thinking_update":
                    assert len(pending_reads) == 2
                    allow_stdout.set()
        await asyncio.gather(*tasks)
        assert len(calls) == 1
        assert calls[0][calls[0].index('--model')+1] == 'gemini-4-carbon-high'
        assert events[-1]['type'] == 'done'
        assert events[-1]['content'] == 'OK'
        assert events[-1]['thinking'] == 'live reasoning'
        assert e.STREAM_NETWORK_IDLE_TIMEOUT_SEC == 165


    async def test_empty_capacity_retry_keeps_prompt_and_model(self):
        e = self.agy_engine
        account = await self._make_agy_account("empty@example.test", "Empty", make_active=True)
        binary = self.tmp_root / "fake.exe"
        binary.write_text("fake", encoding="utf-8")
        commands = []

        async def spawn(*args, **kwargs):
            commands.append(args)
            proc = MagicMock(returncode=0)
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            result = {"status": "ERROR", "error": "503 capacity"} if len(commands) == 1 else {
                "status": "DONE", "response": "OK",
            }
            for event in ({"event": "init", "conversation_id": f"empty-{len(commands)}"},
                          {"event": "result", "result": result}):
                proc.stdout.feed_data((json.dumps(event) + "\n").encode())
            proc.stdout.feed_eof()
            proc.stderr.feed_eof()
            proc.wait = AsyncMock(return_value=0)
            proc.aclose_lease = AsyncMock()
            return proc

        with (patch.object(e, "resolve_agy_bin", return_value=str(binary)),
              patch.object(e.proxy_router, "ensure_proxy_for_context", AsyncMock(return_value=None)),
              patch.object(e, "create_agy_subprocess_exec", side_effect=spawn),
              patch.object(e, "CAPACITY_RETRY_BACKOFF_SECS", (0, 0))):
            events = [event async for event in e.stream_agy_turn(
                chat_id="empty133", kind="regular", project_name="", model="gemini-4-carbon-high",
                access_mode="read_only", prompt="Original request", attachments=[],
                agy_conversation_id="", history_messages=[], preferred_agy_account_id=account["id"],
            )]
        first, second = commands
        assert first[first.index("-p") + 1] == second[second.index("-p") + 1]
        assert "Previous step timed out" not in second[second.index("-p") + 1]
        assert second[second.index("--model") + 1] == "gemini-4-carbon-high"
        assert "--conversation" not in second
        assert events[-1]["content"] == "OK"


def test_gemini_startup_log_distinguishes_terminal_error(tmp_path):
    from app.agy_engine import _gemini_attempt_log, _gemini_first_event_timeout
    log = tmp_path / "cli.log"
    log.write_text(
        "OAuth: authenticated successfully\n"
        "SEND_USER_CASCADE_MESSAGE_LATENCY status:OK\n"
        "Clearcut HTTP 503\n"
        "streamGenerateContent status 503 retrying\n", encoding="utf-8",
    )
    assert _gemini_attempt_log(log) == (True, True, "")
    with log.open("a", encoding="utf-8") as stream:
        stream.write("streamGenerateContent retries exhausted status 503\n")
    assert _gemini_attempt_log(log) == (True, True, "Google generation failed: code 503")
    assert _gemini_first_event_timeout("gemini-4-carbon-high") == 360
    assert _gemini_first_event_timeout("gemini-3.8-flash-low") == 180


def test_consumer_oauth_requests_aicode_without_drive():
    from app.agy_accounts import AGY_OAUTH_SCOPES
    assert set(AGY_OAUTH_SCOPES) == {
        "https://www.googleapis.com/auth/" + name
        for name in ("cloud-platform", "userinfo.email", "userinfo.profile", "cclog", "experimentsandconfigs", "aicode")
    }
