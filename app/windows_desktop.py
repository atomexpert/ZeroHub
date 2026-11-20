"""Launch detached processes on a hidden Windows desktop.

An invisible guardian holds the desktop handle until the server exits.
Never switch the input desktop. No credentials or global window station ACLs change.
"""
import ctypes
import os
import subprocess
import sys
import json
import tempfile
from pathlib import Path

CONHOST_CLSID = '{B23D10C0-E52E-411E-9D5B-C09FDF709C7D}'
CONSOLE_KEY = r'Console\%%Startup'


def _backup_path(path):
    return Path(path) if path else Path(__file__).resolve().parents[1] / 'data/windows_console_startup_backup.json'


def _read_backup(path):
    value = json.loads(path.read_text(encoding='utf-8'), object_hook=lambda obj:
        bytes.fromhex(obj['__bytes__']) if set(obj) == {'__bytes__'} else obj)
    if not isinstance(value.get('key_existed'), bool) or not isinstance(value.get('values'), dict):
        raise ValueError('Invalid console rollback snapshot')
    for item in value['values'].values():
        if item is not None and (not isinstance(item, list) or len(item) != 2 or not isinstance(item[1], int)):
            raise ValueError('Invalid console rollback value')
    return value


def ensure_windows_console_host_delegation(backup_path=None):
    if os.name != 'nt' or os.getenv('HUB_KEEP_DEFAULT_TERMINAL') == '1':
        return {'status': 'skipped'}
    import winreg as r
    try:
        values = {}
        exists = True
        try:
            with r.OpenKey(r.HKEY_CURRENT_USER, CONSOLE_KEY) as key:
                for name in ('DelegationConsole', 'DelegationTerminal'):
                    try:
                        values[name] = list(r.QueryValueEx(key, name))
                    except FileNotFoundError:
                        values[name] = None
        except FileNotFoundError:
            exists = False
        if all(values.get(n) == [CONHOST_CLSID, r.REG_SZ] for n in ('DelegationConsole', 'DelegationTerminal')):
            return {'status': 'already_conhost'}
        path = _backup_path(backup_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish a complete snapshot atomically; never replace an earlier backup.
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.console-backup-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'key_existed': exists, 'values': values}, stream,
                          default=lambda value: {'__bytes__': value.hex()})
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            _read_backup(path)  # Fail closed on a damaged backup.
        finally:
            Path(temporary).unlink(missing_ok=True)
        with r.CreateKey(r.HKEY_CURRENT_USER, CONSOLE_KEY) as key:
            for name in ('DelegationConsole', 'DelegationTerminal'):
                r.SetValueEx(key, name, 0, r.REG_SZ, CONHOST_CLSID)
        return {'status': 'configured', 'backup': str(path)}
    except (OSError, ValueError) as exc:
        return {'status': 'error', 'error': str(exc)}


def restore_windows_console_host_delegation(backup_path=None):
    if os.name != 'nt':
        return {'status': 'skipped'}
    import winreg as r
    try:
        saved = _read_backup(_backup_path(backup_path))
        with r.CreateKey(r.HKEY_CURRENT_USER, CONSOLE_KEY) as key:
            for name in ('DelegationConsole', 'DelegationTerminal'):
                value = saved['values'].get(name)
                if value is None:
                    try:
                        r.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    r.SetValueEx(key, name, 0, value[1], value[0])
        if not saved['key_existed']:
            try:
                with r.OpenKey(r.HKEY_CURRENT_USER, CONSOLE_KEY) as key:
                    subkeys, values, _ = r.QueryInfoKey(key)
                if subkeys == 0 and values == 0:
                    r.DeleteKey(r.HKEY_CURRENT_USER, CONSOLE_KEY)
            except OSError:
                pass
        return {'status': 'restored'}
    except (OSError, ValueError) as exc:
        return {'status': 'error', 'error': str(exc)}

HIDDEN_DESKTOP = r"WinSta0\AgyHubHidden"
DEFAULT_DESKTOP = r"WinSta0\Default"


def spawn_on_desktop(args, *, cwd=None, desktop=HIDDEN_DESKTOP, creationflags=0x08000200, wait=False, env=None):
    if os.name != 'nt':
        return subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True).pid
    if desktop == HIDDEN_DESKTOP and not wait:
        # Keep the HDESK alive in a detached guardian for the child's lifetime.
        return _helper(args, cwd=cwd, env=env)
    from ctypes import wintypes as w

    class Startup(ctypes.Structure):
        _fields_ = [('cb', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR),
                    ('title', w.LPWSTR), ('x', w.DWORD), ('y', w.DWORD),
                    ('cx', w.DWORD), ('cy', w.DWORD), ('cols', w.DWORD),
                    ('rows', w.DWORD), ('fill', w.DWORD), ('flags', w.DWORD),
                    ('show', w.WORD), ('reserved_size', w.WORD),
                    ('reserved_ptr', ctypes.c_void_p), ('stdin', w.HANDLE),
                    ('stdout', w.HANDLE), ('stderr', w.HANDLE)]

    class Process(ctypes.Structure):
        _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE),
                    ('pid', w.DWORD), ('tid', w.DWORD)]

    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p,
                                   w.DWORD, w.DWORD, ctypes.c_void_p]
    user.CreateDesktopW.restype = w.HANDLE
    user.CloseDesktop.argtypes = [w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p,
        ctypes.c_void_p, w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR,
        ctypes.POINTER(Startup), ctypes.POINTER(Process)]
    handle = None
    if desktop == HIDDEN_DESKTOP:
        handle = user.CreateDesktopW('AgyHubHidden', None, None, 0, 0x01ff, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
    si, pi = Startup(), Process()
    si.cb = ctypes.sizeof(si)
    si.desktop = desktop
    si.flags, si.show = 1, (5 if desktop == DEFAULT_DESKTOP else 0)
    environment = None
    if env is not None:
        environment = ctypes.create_unicode_buffer('\0'.join(
            f'{k}={v}' for k, v in sorted(env.items(), key=lambda item: item[0].upper())) + '\0\0')
        creationflags |= 0x00000400
    try:
        if not kernel.CreateProcessW(None, ctypes.create_unicode_buffer(subprocess.list2cmdline(args)),
                None, None, False, creationflags, environment, str(cwd) if cwd else None,
                ctypes.byref(si), ctypes.byref(pi)):
            raise ctypes.WinError(ctypes.get_last_error())
        if wait:
            kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
            kernel.WaitForSingleObject(pi.process, 0xffffffff)
        return pi.pid
    finally:
        if pi.thread:
            kernel.CloseHandle(pi.thread)
        if pi.process:
            kernel.CloseHandle(pi.process)
        if handle:
            user.CloseDesktop(handle)


def spawn_visible_terminal(args, *, cwd, env):
    if os.name != 'nt':
        return subprocess.Popen(args, cwd=cwd, env=env).pid
    return _helper(['--visible', *args], cwd=cwd, env=env)


def _helper(args, *, cwd=None, env=None):
    from .config import subprocess_hidden_kwargs
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    helper_env = dict(os.environ if env is None else env)
    helper_env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + helper_env.get('PYTHONPATH', '')
    return subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable),
        '-m', 'app.windows_desktop', '--hub-root', str(Path(__file__).resolve().parents[1]), *args], cwd=cwd, env=helper_env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **subprocess_hidden_kwargs()).pid


class StationContext:
    """Only used in the disposable guardian, before any GUI initialization."""
    def __init__(self, visible=False):
        from ctypes import wintypes as w
        class Security(ctypes.Structure):
            _fields_ = [('length', w.DWORD), ('descriptor', ctypes.c_void_p), ('inherit', w.BOOL)]
        class Flags(ctypes.Structure):
            _fields_ = [('inherit', w.BOOL), ('reserved', w.BOOL), ('flags', w.DWORD)]
        self.u = u = ctypes.WinDLL('user32', use_last_error=True)
        for name, types in {
            'CreateWindowStationW': [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p],
            'OpenWindowStationW': [w.LPCWSTR, w.BOOL, w.DWORD],
            'CreateDesktopW': [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p],
            'OpenDesktopW': [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD],
            'GetProcessWindowStation': [], 'GetThreadDesktop': [w.DWORD],
        }.items():
            fn = getattr(u, name)
            fn.argtypes, fn.restype = types, w.HANDLE
        for name in ('SetProcessWindowStation', 'SetThreadDesktop', 'CloseWindowStation', 'CloseDesktop'):
            getattr(u, name).argtypes = [w.HANDLE]
        u.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
        self.original_station = u.GetProcessWindowStation()
        self.original_desktop = u.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
        self.station = self.desktop = None
        self.visible = visible
        sa = Security(ctypes.sizeof(Security), None, True)
        try:
            self.station = (u.OpenWindowStationW('WinSta0', False, 0x037f) if visible else
                            u.CreateWindowStationW(None, 0, 0x037f, ctypes.byref(sa)))
            if not self.station:
                raise ctypes.WinError(ctypes.get_last_error())
            flags, needed = Flags(), w.DWORD()
            if not u.GetUserObjectInformationW(self.station, 1, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.flags = flags.flags
            if not visible and self.flags & 1:
                raise OSError('Private window station is visible')
            name = ctypes.create_unicode_buffer(512)
            if not u.GetUserObjectInformationW(self.station, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.name = name.value
            if not u.SetProcessWindowStation(self.station):
                raise ctypes.WinError(ctypes.get_last_error())
            self.desktop = (u.OpenDesktopW('Default', 0, False, 0x01ff) if visible else
                            u.CreateDesktopW('AgyHubHidden', None, None, 0, 0x01ff, ctypes.byref(sa)))
            if not self.desktop or not u.SetThreadDesktop(self.desktop):
                raise ctypes.WinError(ctypes.get_last_error())
            self.path = self.name + ('\\Default' if visible else '\\AgyHubHidden')
        except BaseException:
            self.close()
            raise

    def close(self):
        # The original desktop belongs to the original station: reconnect first.
        if not self.u.SetProcessWindowStation(self.original_station):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.u.SetThreadDesktop(self.original_desktop):
            raise ctypes.WinError(ctypes.get_last_error())
        if self.desktop:
            if not self.u.CloseDesktop(self.desktop):
                raise ctypes.WinError(ctypes.get_last_error())
            self.desktop = None
        if self.station:
            if not self.u.CloseWindowStation(self.station):
                raise ctypes.WinError(ctypes.get_last_error())
            self.station = None


def compatibility_probe():
    """No production credentials, database, or servers are used here."""
    import socket
    from .agy_credentials import WindowsCredentialStore
    from .config import resolve_agy_bin, subprocess_hidden_kwargs
    store = WindowsCredentialStore()
    store.target = 'AgyHub-v131-probe'
    if store.read() is not None:
        raise OSError('Probe credential already exists')
    try:
        store.write({'blob': b'v131-probe'.hex()})
        if bytes.fromhex(store.read()['blob']) != b'v131-probe':
            raise OSError('Credential roundtrip failed')
    finally:
        store.write(None)
    import win32crypt
    encrypted = win32crypt.CryptProtectData(b'v131', None, None, None, None, 0)
    if win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1] != b'v131':
        raise OSError('DPAPI roundtrip failed')
    with socket.socket() as server, socket.socket() as client:
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        client.settimeout(3)
        client.connect(server.getsockname())
        peer, _ = server.accept()
        with peer:
            client.sendall(b'ok')
            if peer.recv(2) != b'ok':
                raise OSError('Loopback roundtrip failed')
    python = Path(__file__).resolve().parents[1] / '.venv/Scripts/python.exe'
    powershell = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', 'exit 0']
    for command in ([str(python), '-c', 'import uvicorn, fastapi, aiosqlite'],
                    [resolve_agy_bin(), '--version'], powershell):
        subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20, check=True, **subprocess_hidden_kwargs())
    # Intentionally no STARTF_USESTDHANDLES / PIPE; exercise terminal handoff.
    subprocess.run(['cmd.exe', '/c', 'ping -n 2 127.0.0.1 >nul'],
        stdin=None, stdout=None, stderr=None, creationflags=0x10, timeout=10, check=True)
    subprocess.run(powershell, stdin=None, stdout=None, stderr=None,
        creationflags=0x10, timeout=10, check=True)
    return {'credentials': True, 'dpapi': True, 'loopback': True,
            'venvlauncher': True, 'agy_version': True, 'new_console_without_pipes': True,
            'powershell': True, 'powershell_without_pipes': True}


def guardian(args, *, visible=False, probe_output=None, force_fallback=False):
    context = None
    report = {}
    try:
        if visible:
            context = StationContext(visible=True)
            return spawn_on_desktop(args, desktop=context.path, creationflags=0x10, wait=True)
        if probe_output and not force_fallback:
            try:
                context = StationContext()
                report = {'mode': 'private', 'station': context.name, 'flags': context.flags}
                report['compatibility'] = compatibility_probe()
            except Exception as exc:
                report = {'fallback_reason': str(exc)}
                if context:
                    context.close()
                    context = None
        if context is None:
            # AGY's run_command shell fails to initialize on a private window station.
            # Attach the guardian to WinSta0 and keep the server on a separate desktop.
            context = StationContext(visible=True)
            report['mode'] = 'fallback' if probe_output else 'hidden'
            report['delegation'] = ensure_windows_console_host_delegation()
        if probe_output:
            Path(probe_output).write_text(json.dumps(report, indent=2), encoding='utf-8')
            if report['mode'] == 'fallback':
                # Validate the same compatibility gate on the fallback desktop in a child.
                spawn_on_desktop([sys.executable, '-m', 'app.windows_desktop', '--compatibility-only', probe_output], wait=True)
            return report
        return spawn_on_desktop(args, desktop=HIDDEN_DESKTOP, wait=True)
    finally:
        if context:
            context.close()


if __name__ == '__main__':
    if sys.argv[1:2] == ['--hub-root']:
        del sys.argv[1:3]
    if '--ensure-console-host' in sys.argv:
        print(json.dumps(ensure_windows_console_host_delegation()))
    elif '--restore-console-host' in sys.argv:
        print(json.dumps(restore_windows_console_host_delegation()))
    elif '--visible' in sys.argv:
        guardian(sys.argv[2:], visible=True)
    elif '--probe' in sys.argv:
        guardian([], probe_output=sys.argv[2])
    elif '--probe-fallback' in sys.argv:
        guardian([], probe_output=sys.argv[2], force_fallback=True)
    elif '--compatibility-only' in sys.argv:
        path = Path(sys.argv[2])
        report = json.loads(path.read_text(encoding='utf-8'))
        report['compatibility'] = compatibility_probe()
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    else:
        guardian(sys.argv[1:])
