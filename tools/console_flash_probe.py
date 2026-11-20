"""Interactive desktop/process observer; no production credentials or DB access."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WATCH = {'windowsterminal.exe', 'openconsole.exe', 'conhost.exe', 'cmd.exe',
         'powershell.exe', 'git.exe', 'language_server_windows_x64.exe'}


def observe(command, output, *, seconds=35):
    u = ctypes.WinDLL('user32', use_last_error=True)
    u.GetProcessWindowStation.restype = w.HANDLE
    u.GetUserObjectInformationW.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    station_name, needed = ctypes.create_unicode_buffer(512), w.DWORD()
    if not u.GetUserObjectInformationW(u.GetProcessWindowStation(), 2, station_name,
            ctypes.sizeof(station_name), ctypes.byref(needed)) or station_name.value != 'WinSta0':
        raise OSError('Observer must run on WinSta0')
    u.OpenDesktopW.argtypes = [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD]
    u.OpenDesktopW.restype = w.HANDLE
    u.CloseDesktop.argtypes = [w.HANDLE]
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    u.EnumDesktopWindows.argtypes = [w.HANDLE, callback_type, w.LPARAM]
    u.IsWindowVisible.argtypes = [w.HWND]
    u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    desktop = u.OpenDesktopW('Default', 0, False, 0x0041)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    def process_info(pid):
        try:
            p = psutil.Process(pid)
            return {'pid': pid, 'name': p.name(), 'command': p.cmdline(), 'created': p.create_time()}
        except psutil.Error:
            return {'pid': pid, 'name': 'exited'}
    def windows():
        result = {}
        @callback_type
        def visit(hwnd, param):
            if u.IsWindowVisible(hwnd):
                pid = w.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                result[int(hwnd)] = pid.value
            return True
        if not u.EnumDesktopWindows(desktop, visit, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return result
    baseline_windows = windows()
    baseline_processes = {(p.pid, p.create_time()) for p in psutil.process_iter()}
    report = {'observer_desktop': 'WinSta0\\Default', 'poll_target_ms': 5,
              'visible_windows': [], 'processes': [], 'max_poll_gap_ms': 0, 'samples': 0}
    known_windows, known_processes = set(baseline_windows.items()), set(baseline_processes)
    started = time.perf_counter()
    # No PIPE and no STARTF_USESTDHANDLES in the tested console child.
    from app.config import subprocess_hidden_kwargs
    kwargs = subprocess_hidden_kwargs() if '-m' in command else {'creationflags': 0x10}
    p = subprocess.Popen(command, cwd=ROOT, stdin=None, stdout=None, stderr=None, **kwargs)
    last = time.perf_counter()
    exited_at = None
    try:
        while time.perf_counter()-started < seconds:
            now = time.perf_counter()
            report['max_poll_gap_ms'] = max(report['max_poll_gap_ms'], (now-last)*1000)
            last = now
            for hwnd, pid in windows().items():
                if (hwnd, pid) not in known_windows:
                    known_windows.add((hwnd, pid))
                    report['visible_windows'].append({'hwnd': hwnd, **process_info(pid)})
            for proc in psutil.process_iter(['pid', 'name', 'create_time']):
                stamp = (proc.pid, proc.info['create_time'])
                if stamp not in known_processes and (proc.info['name'] or '').lower() in WATCH:
                    known_processes.add(stamp)
                    report['processes'].append(process_info(proc.pid))
            if p.poll() is not None:
                exited_at = exited_at or now
                if now-exited_at > .4:
                    break
            report['samples'] += 1
            # Spend the remainder of the 5 ms budget sleeping, rather than adding
            # 5 ms on top of process/window enumeration time.
            time.sleep(max(.0001, .005 - (time.perf_counter()-now)))
        report['returncode'] = p.wait(timeout=5)
    finally:
        u.CloseDesktop(desktop)
    report['elapsed_seconds'] = time.perf_counter()-started
    report['mean_poll_gap_ms'] = report['elapsed_seconds']*1000/max(1, report['samples'])
    report['terminal_processes'] = [p for p in report['processes'] if p['name'].lower() in ('windowsterminal.exe','openconsole.exe')]
    Path(output).write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def binaries():
    from app.config import resolve_agy_bin
    binary = Path(resolve_agy_bin())
    result = []
    for p in binary.parent.parent.rglob('*.exe'):
        try:
            with p.open('rb') as f:
                header = f.read(64)
                offset = struct.unpack_from('<I', header, 60)[0]
                f.seek(offset+92)
                subsystem = struct.unpack('<H', f.read(2))[0]
            result.append({'path': str(p), 'subsystem': subsystem})
        except (OSError, struct.error):
            pass
    return result


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv)>1 else 'private'
    if mode == 'baseline':
        command = ['cmd.exe', '/c', 'ping -n 2 127.0.0.1 >nul']
    else:
        command = [sys.executable, '-m', 'app.windows_desktop', '--probe', str(ROOT/'tmp/v131_station.json')]
    report = observe(command, ROOT/f'tmp/v131_{mode}_flash.json')
    report['binaries'] = binaries()
    (ROOT/f'tmp/v131_{mode}_flash.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
