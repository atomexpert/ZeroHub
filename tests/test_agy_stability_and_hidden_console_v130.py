"""v130: isolated credential, telemetry and stream recovery regressions."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_lease_init_then_model_event(monkeypatch, tmp_path):
    from app import agy_credentials as c
    async def run():
        monkeypatch.setattr(c, '_lease_lock', asyncio.Lock())
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"event":"init"}\n{"event":"message","text":"ok"}\n')
        proc = MagicMock(stdout=reader, returncode=None)
        proc.wait = AsyncMock(side_effect=asyncio.Event().wait)
        restore = MagicMock()
        await c._lease_lock.acquire()
        leased = c.LeasedProcess(proc, restore)
        await leased.stdout.readline()
        assert c._lease_lock.locked()
        restore.assert_not_called()
        await leased.stdout.readline()
        assert not c._lease_lock.locked()
        restore.assert_called_once()
        await leased.finish_lease()
        restore.assert_called_once()
    asyncio.run(run())


def test_telemetry_never_launches_during_turn(monkeypatch, tmp_path):
    from app import agy_credentials as c, agy_engine as e
    async def run():
        monkeypatch.setattr(c, 'active_working_turns', 1)
        spawn = AsyncMock()
        monkeypatch.setattr(c.asyncio, 'create_subprocess_exec', spawn)
        for command in ('/usage', '/credits'):
            assert await e._run_slash_json_command('fake', command) is None
            with pytest.raises(c.TelemetryDeferred):
                await c.create_agy_subprocess_exec('fake', '-p', command, priority='telemetry')
        spawn.assert_not_called()
    asyncio.run(run())


def test_host_preflight_refresh_and_force(monkeypatch, tmp_path):
    from app import agy_accounts as a, agy_credentials as c
    async def run():
        monkeypatch.setattr(c, '_lease_lock', asyncio.Lock())
        path = tmp_path / '.gemini/antigravity-cli/antigravity-oauth-token'
        path.parent.mkdir(parents=True)
        old = {'token': {'access_token': 'old', 'refresh_token': 'fake',
            'expiry': (datetime.now(timezone.utc) + timedelta(seconds=100)).isoformat()}}
        fresh = {'token': {**old['token'], 'access_token': 'fresh',
            'expiry': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}
        path.write_text(json.dumps(old), encoding='utf-8')
        store = MagicMock()
        store.read.return_value = None
        monkeypatch.setattr(c, 'WindowsCredentialStore', lambda: store)
        refresh = AsyncMock(return_value=fresh)
        monkeypatch.setattr(a, 'refresh_and_build_agy_token_payload', refresh)
        await a.refresh_host_agy_token(tmp_path)
        assert json.loads(path.read_text(encoding='utf-8')) == fresh
        refresh.assert_awaited_once()
        await a.refresh_host_agy_token(tmp_path)
        refresh.assert_awaited_once()
        await a.refresh_host_agy_token(tmp_path, force=True)
        assert refresh.await_count == 2
    asyncio.run(run())


def test_working_turn_counter_releases_on_close(monkeypatch, tmp_path):
    from app import agy_engine as e, agy_credentials as c
    async def run():
        monkeypatch.setattr(c, 'active_working_turns', 0)
        @e._working_turn
        async def sample():
            yield 1
            yield 2
        stream = sample()
        assert await anext(stream) == 1
        assert c.active_working_turns == 1
        await stream.aclose()
        assert c.active_working_turns == 0
    asyncio.run(run())


def test_desktop_and_watchdog_contract():
    from app import windows_desktop as w, agy_engine as e
    assert w.HIDDEN_DESKTOP == r'WinSta0\AgyHubHidden'
    assert w.DEFAULT_DESKTOP == r'WinSta0\Default'
    assert e.STREAM_STARTUP_IDLE_TIMEOUT_SEC == 65
    assert e.STREAM_NETWORK_IDLE_TIMEOUT_SEC == 165
    assert e.STREAM_ACTIVE_TOOL_TIMEOUT_SEC == 600
    assert e._is_agy_auth_error('UNAUTHENTICATED (code 401)')
    assert not e._is_agy_auth_error('503 capacity')


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Win32 ABI')
def test_win32_desktop_flags_and_handle_cleanup(monkeypatch, tmp_path):
    from app import windows_desktop as w
    import ctypes
    user, kernel = MagicMock(), MagicMock()
    user.CreateDesktopW.return_value = 123
    recorded = []
    def create(app, command, pa, ta, inherit, flags, env, cwd, startup, process):
        recorded.append((startup._obj.desktop, flags, inherit))
        process._obj.process, process._obj.thread, process._obj.pid = 456, 789, 42
        return 1
    kernel.CreateProcessW.side_effect = create
    monkeypatch.setattr(ctypes, 'WinDLL', lambda name, **kw: user if name == 'user32' else kernel)
    assert w.spawn_on_desktop(['fake'], cwd=tmp_path, wait=True) == 42
    assert recorded[-1] == (w.HIDDEN_DESKTOP, 0x08000200, False)
    kernel.WaitForSingleObject.assert_called_once()
    user.CloseDesktop.assert_called_once_with(123)
    helper = MagicMock(return_value=43)
    monkeypatch.setattr(w, '_helper', helper)
    assert w.spawn_visible_terminal(['fake'], cwd=tmp_path, env={'TEST': 'ok'}) == 43
    helper.assert_called_once_with(['--visible', 'fake'], cwd=tmp_path, env={'TEST': 'ok'})
    assert kernel.CloseHandle.call_count == 2


# Reuse the established temporary database/home fixture, without inheriting its tests.
import unittest
import test_audit_fixes_v128 as isolated
from unittest.mock import patch


class StreamRecoveryV130Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolated.AuditFixesV128Tests.setUpClass.__func__)
    tearDownClass = classmethod(isolated.AuditFixesV128Tests.tearDownClass.__func__)
    asyncSetUp = isolated.AuditFixesV128Tests.asyncSetUp
    asyncTearDown = isolated.AuditFixesV128Tests.asyncTearDown
    _assert_live_db_untouched = isolated.AuditFixesV128Tests._assert_live_db_untouched
    _make_agy_account = isolated.AuditFixesV128Tests._make_agy_account

    async def _recover(self, repeat_401=False, reject_refresh=False, no_progress=False):
        from app import agy_accounts as accounts
        e = self.agy_engine
        account = await self._make_agy_account('retry@example.test', 'Retry', make_active=True)
        binary = self.tmp_root / 'fake.exe'
        binary.write_text('fake', encoding='utf-8')
        calls = []
        async def spawn(*args, **kwargs):
            calls.append(args)
            p = MagicMock(returncode=0)
            p.stdout = asyncio.StreamReader()
            events = [{'event': 'init', 'conversation_id': 'retry-session'},
                {'event': 'step_update', 'step_update': {'step_type': 'agent_response',
                    'text_delta': 'before ' if len(calls) == 1 else 'after'}}]
            if no_progress:
                events = events[:1]
            events.append({'event': 'result', 'result':
                {'status': 'ERROR', 'error': 'UNAUTHENTICATED (code 401)'}
                if len(calls) == 1 or repeat_401 else {'status': 'DONE'}})
            p.stdout.feed_data(('\n'.join(map(json.dumps, events))+'\n').encode())
            p.stdout.feed_eof()
            p.stderr = asyncio.StreamReader()
            p.stderr.feed_eof()
            p.wait = AsyncMock(return_value=0)
            p.aclose_lease = AsyncMock()
            return p
        fresh = {'token': {'access_token': 'fresh', 'refresh_token': 'fake',
            'expiry': (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}}
        refresh = AsyncMock(side_effect=ValueError('refresh rejected') if reject_refresh else None,
                            return_value=fresh)
        with (patch.object(e, 'resolve_agy_bin', return_value=str(binary)),
              patch.object(e.proxy_router, 'ensure_proxy_for_context', AsyncMock(return_value=None)),
              patch.object(e, 'create_agy_subprocess_exec', side_effect=spawn),
              patch.object(accounts, 'refresh_and_build_agy_token_payload', refresh)):
            events = [evt async for evt in e.stream_agy_turn(chat_id='retry130', kind='regular',
                project_name='', model='gemini-4-carbon-high', access_mode='read_only',
                prompt='hello', attachments=[], agy_conversation_id='', history_messages=[],
                preferred_agy_account_id=account['id'])]
        refresh.assert_awaited_once()
        assert len(calls) == (1 if reject_refresh else 2)
        assert events[-1]['type'] == ('error' if repeat_401 or reject_refresh else 'done')
        assert events[-1]['content'].startswith('before ') if not no_progress else events[-1]['content'] == ''
        if not reject_refresh:
            assert '--conversation' in calls[1]
            assert events[-1]['content'] == ('before after' if not no_progress else '')
        if not repeat_401 and not reject_refresh:
            assert not any(evt['type'] == 'error' for evt in events)

    async def test_mid_turn_401_retains_text_and_resumes_once(self):
        await self._recover()

    async def test_second_401_surfaces_error_without_third_launch(self):
        await self._recover(repeat_401=True)

    async def test_rejected_refresh_surfaces_error_without_retry(self):
        await self._recover(reject_refresh=True)

    async def test_second_401_without_progress_does_not_enter_generic_recovery(self):
        await self._recover(repeat_401=True, no_progress=True)
