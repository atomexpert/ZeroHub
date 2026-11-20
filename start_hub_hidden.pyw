"""Antigravity Hub — silent launcher (run via pythonw.exe).

Starts the server on WinSta0\\AgyHubHidden through a guardian with console-host delegation. Console
children and grandchildren inherit this non-interactive desktop even when they
request their own console. The launcher opens the browser on the user's desktop.

NB: DETACHED_PROCESS is intentionally NOT used: it cancels CREATE_NO_WINDOW and leaves the
server without any console (the old pythonw.exe behaviour that caused the flashes).
AllocConsole() is not used either: with Windows Terminal as default host it opens a window.

Usage:
    .venv\\Scripts\\pythonw.exe start_hub_hidden.pyw            # start server only
    .venv\\Scripts\\pythonw.exe start_hub_hidden.pyw --open     # start (if needed) + open browser
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from app.windows_desktop import spawn_on_desktop, ensure_windows_console_host_delegation

BASE_DIR = Path(__file__).resolve().parent
HUB_URL = "http://127.0.0.1:8765"
HUB_PORT = 8765

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _python_runs_cleanly(py_exe: Path) -> bool:
    if not py_exe.is_file():
        return False
    try:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.run(
            [str(py_exe), "-c", "import sys"],
            cwd=str(BASE_DIR),
            creationflags=CREATE_NO_WINDOW,
            startupinfo=startup,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.5,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _server_python() -> str:
    venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists() and _python_runs_cleanly(venv_python):
        return str(venv_python)
    cur = Path(sys.executable)
    alt = cur.with_name("python.exe")
    if alt.exists() and alt.resolve() != venv_python.resolve() and _python_runs_cleanly(alt):
        return str(alt)
    sys_py = shutil.which("python")
    if sys_py:
        return sys_py
    return str(alt if alt.exists() else cur)


def main() -> int:
    ensure_windows_console_host_delegation()
    open_browser = "--open" in sys.argv[1:]
    started = False
    if not _port_listening(HUB_PORT):
        spawn_on_desktop([_server_python(), str(BASE_DIR / "run_server.py")], cwd=BASE_DIR)
        started = True

    if open_browser:
        if started:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and not _port_listening(HUB_PORT):
                time.sleep(0.3)
        webbrowser.open(HUB_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
