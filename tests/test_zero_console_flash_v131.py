"""Registry is always fake here; no production credentials or database access."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app import windows_desktop as desktop


class FakeRegistry:
    HKEY_CURRENT_USER, REG_SZ = 1, 1
    def __init__(self, exists=False, values=None):
        self.exists = exists
        self.values = dict(values or {})
        self.writes = 0
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def OpenKey(self, *args):
        if not self.exists:
            raise FileNotFoundError()
        return self
    def CreateKey(self, *args):
        self.exists = True
        return self
    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError()
        return self.values[name]
    def SetValueEx(self, key, name, reserved, kind, value):
        self.writes += 1
        self.values[name] = (value, kind)
    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError()
        del self.values[name]
    def QueryInfoKey(self, key):
        return 0, len(self.values), 0
    def DeleteKey(self, *args):
        self.exists = False


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setitem(sys.modules, 'winreg', fake)
    # Patch this module's os binding only, never global os.name (pathlib depends on it).
    monkeypatch.setattr(desktop, 'os', SimpleNamespace(name='nt', getenv=os.getenv,
        fdopen=os.fdopen, fsync=os.fsync, link=os.link))
    monkeypatch.delenv('HUB_KEEP_DEFAULT_TERMINAL', raising=False)
    return fake


@pytest.mark.parametrize('exists,values', [(False, {}), (True, {}),
    (True, {'DelegationConsole': ('old-console', 1), 'DelegationTerminal': ('old-terminal', 1)}),
    (True, {'DelegationConsole': (b'original', 3)})])
def test_registry_roundtrip_idempotence(registry, tmp_path, exists, values):
    registry.exists, registry.values = exists, dict(values)
    path = tmp_path / 'backup.json'
    assert desktop.ensure_windows_console_host_delegation(path)['status'] == 'configured'
    original = path.read_bytes()
    assert registry.values['DelegationTerminal'] == (desktop.CONHOST_CLSID, 1)
    assert desktop.ensure_windows_console_host_delegation(path)['status'] == 'already_conhost'
    assert registry.writes == 2
    assert path.read_bytes() == original
    assert desktop.restore_windows_console_host_delegation(path)['status'] == 'restored'
    assert registry.values == values
    assert registry.exists == exists


def test_opt_out_and_permission_error(registry, monkeypatch, tmp_path):
    path = tmp_path/'backup.json'
    monkeypatch.setenv('HUB_KEEP_DEFAULT_TERMINAL', '1')
    assert desktop.ensure_windows_console_host_delegation(path)['status'] == 'skipped'
    assert not path.exists()
    monkeypatch.delenv('HUB_KEEP_DEFAULT_TERMINAL')
    monkeypatch.setattr(registry, 'CreateKey', MagicMock(side_effect=PermissionError('denied')))
    assert desktop.ensure_windows_console_host_delegation(path)['status'] == 'error'
    assert path.exists()


def test_invalid_backup_fails_closed(registry, tmp_path):
    path = tmp_path/'backup.json'
    path.write_text('{}', encoding='utf-8')
    assert desktop.ensure_windows_console_host_delegation(path)['status'] == 'error'
    assert registry.writes == 0


def test_original_backup_is_never_replaced(registry, tmp_path):
    path = tmp_path/'backup.json'
    desktop.ensure_windows_console_host_delegation(path)
    original = path.read_bytes()
    registry.values['DelegationTerminal'] = ('external-change', 1)
    assert desktop.ensure_windows_console_host_delegation(path)['status'] == 'configured'
    assert path.read_bytes() == original
    desktop.restore_windows_console_host_delegation(path)
    assert registry.values == {}


def test_visible_guardian_reconnects_to_interactive_station(monkeypatch, tmp_path):
    context = MagicMock(path=desktop.DEFAULT_DESKTOP)
    station = MagicMock(return_value=context)
    spawn = MagicMock(return_value=9)
    monkeypatch.setattr(desktop, 'StationContext', station)
    monkeypatch.setattr(desktop, 'spawn_on_desktop', spawn)
    assert desktop.guardian(['fake'], visible=True) == 9
    station.assert_called_once_with(visible=True)
    spawn.assert_called_once_with(['fake'], desktop=desktop.DEFAULT_DESKTOP, creationflags=0x10, wait=True)
    context.close.assert_called_once()


def test_restore_preserves_unrelated_new_values(registry, tmp_path):
    path = tmp_path/'backup.json'
    desktop.ensure_windows_console_host_delegation(path)
    registry.values['unrelated'] = ('keep', 1)
    desktop.restore_windows_console_host_delegation(path)
    assert registry.values == {'unrelated': ('keep', 1)}


def test_visible_helper_route(monkeypatch, tmp_path):
    helper = MagicMock(return_value=42)
    monkeypatch.setattr(desktop, '_helper', helper)
    assert desktop.spawn_visible_terminal(['agy.exe'], cwd=tmp_path, env={'ROUTE':'test'}) == 42
    helper.assert_called_once_with(['--visible', 'agy.exe'], cwd=tmp_path, env={'ROUTE':'test'})


def test_git_prompt_environment(monkeypatch, tmp_path):
    from app import agy_engine as engine
    monkeypatch.delenv('GIT_TERMINAL_PROMPT', raising=False)
    monkeypatch.delenv('GCM_INTERACTIVE', raising=False)
    env = engine._build_subprocess_env(proxy_url=None, isolated_home_override=tmp_path)
    assert env['GIT_TERMINAL_PROMPT'] == '0'
    assert env['GCM_INTERACTIVE'] == 'never'


def test_guardian_prefers_hidden_desktop_for_server(monkeypatch):
    context = MagicMock()
    station = MagicMock(return_value=context)
    monkeypatch.setattr(desktop, 'StationContext', station)
    ensure = MagicMock(return_value={'status': 'already_conhost'})
    monkeypatch.setattr(desktop, 'ensure_windows_console_host_delegation', ensure)
    spawn = MagicMock(return_value=1)
    monkeypatch.setattr(desktop, 'spawn_on_desktop', spawn)
    assert desktop.guardian(['fake']) == 1
    station.assert_called_once_with(visible=True)
    ensure.assert_called_once_with()
    spawn.assert_called_once_with(['fake'], desktop=desktop.HIDDEN_DESKTOP, wait=True)
    context.close.assert_called_once_with()


def test_guardian_fallback_probe(monkeypatch, tmp_path):
    context = MagicMock(path='private\\AgyHubHidden', name='private', flags=0)
    fallback = MagicMock()
    monkeypatch.setattr(desktop, 'StationContext', MagicMock(side_effect=[context, fallback]))
    monkeypatch.setattr(desktop, 'compatibility_probe', MagicMock(side_effect=OSError('probe failed')))
    ensure = MagicMock(return_value={'status':'already_conhost'})
    monkeypatch.setattr(desktop, 'ensure_windows_console_host_delegation', ensure)
    spawn = MagicMock(return_value=1)
    monkeypatch.setattr(desktop, 'spawn_on_desktop', spawn)
    output = tmp_path/'station.json'
    report = desktop.guardian([], probe_output=str(output))
    assert report['mode'] == 'fallback'
    assert report['fallback_reason'] == 'probe failed'
    context.close.assert_called_once()
    fallback.close.assert_called_once()
    ensure.assert_called_once()
    spawn.assert_called_once_with(
        [sys.executable, '-m', 'app.windows_desktop', '--compatibility-only', str(output)], wait=True)


@pytest.mark.skipif(os.name != 'nt', reason='Windows integration')
def test_hidden_desktop_without_pipes_under_live_observer(monkeypatch, tmp_path):
    # Keep even fallback from changing HKCU in a test process.
    monkeypatch.setenv('HUB_KEEP_DEFAULT_TERMINAL', '1')
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('flash_probe', root/'tools/console_flash_probe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    station_report = tmp_path/'station.json'
    context = desktop.StationContext(visible=True)
    try:
        report = probe.observe([sys.executable, '-m', 'app.windows_desktop', '--probe-fallback',
            str(station_report)], tmp_path/'flash.json')
    finally:
        context.close()
    assert report['returncode'] == 0
    station = json.loads(station_report.read_text(encoding='utf-8'))
    assert station['mode'] == 'fallback', station
    assert all(station['compatibility'].values())
    assert report['visible_windows'] == []
    assert report['terminal_processes'] == []
