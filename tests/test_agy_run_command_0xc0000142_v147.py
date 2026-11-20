"""Windows AGY launch and pre-commit isolation regressions."""
import builtins
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows AGY environment")
def test_agy_normalizes_windows_environment(monkeypatch, tmp_path):
    from app import agy_engine as engine

    windows_dir = os.environ["SystemRoot"]
    path = os.environ["PATH"]
    monkeypatch.setattr(os, "environ", {
        "Path": "obsolete",
        "PATH": path,
        "USERPROFILE": str(tmp_path / "host"),
        "Tmp": str(tmp_path),
    })
    home = tmp_path / "cli"
    env = engine._build_subprocess_env(isolated_home_override=home, proxy_url=None)
    env = engine._apply_agy_proxy_env(env, "http://127.0.0.1:12345")

    assert all(key == key.upper() for key in env)
    assert env["PATH"] == path
    assert Path(env["SYSTEMROOT"]) == Path(env["WINDIR"]) == Path(windows_dir)
    assert env["SYSTEMDRIVE"] == Path(windows_dir).drive
    assert Path(env["COMSPEC"]) == Path(windows_dir) / "System32" / "cmd.exe"
    assert ".EXE" in env["PATHEXT"].split(";")
    assert env["TEMP"] == env["TMP"] == str(tmp_path)
    assert Path(env["APPDATA"]) == tmp_path / "host" / "AppData" / "Roaming"
    assert Path(env["LOCALAPPDATA"]) == tmp_path / "host" / "AppData" / "Local"
    assert Path(env["USERPROFILE"]) == home
    assert env["HTTP_PROXY"] == env["HTTPS_PROXY"] == "http://127.0.0.1:12345"


@pytest.mark.parametrize(("kind", "mode", "force_no_host_shell", "host_shell"), [
    ("project", "read_only", False, False),
    ("project", "write_files", False, False),
    ("project", "full_access", True, False),
    ("project", "full_access", False, True),
    ("chat", "full_access", False, False),
])
def test_only_trusted_full_access_allows_host_shell(monkeypatch, kind, mode, force_no_host_shell, host_shell):
    from app import agy_engine as engine

    monkeypatch.setattr(engine, "IS_WINDOWS", True)
    args = engine._build_permission_args(kind, mode, force_no_host_shell=force_no_host_shell,
                                         resume_conv_id="resumed-plan")
    settings = json.loads(args[args.index("--settings") + 1])
    assert settings["enableTerminalSandbox"] is not host_shell
    assert ("--dangerously-skip-permissions" in args) is host_shell
    assert ("--sandbox=false" in args) is host_shell
    assert ("command(*)" in settings["permissions"]["deny"]) is not host_shell
    if host_shell:
        assert args[args.index("--mode") + 1] == "accept-edits"


@pytest.mark.skipif(os.name != "nt", reason="Win32 process creation")
def test_hidden_launch_flags():
    from app.agy_engine import subprocess_hidden_kwargs

    kwargs = subprocess_hidden_kwargs()
    flags, startup = kwargs["creationflags"], kwargs["startupinfo"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not flags & (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE)
    assert startup.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startup.wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell and pythonw.exe")
def test_hidden_guardian_can_launch_powershell(tmp_path):
    from app.agy_engine import subprocess_hidden_kwargs

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("pythonw.exe is unavailable")
    roots = ("HUB_DATA_DIR", "AGY_CLI_DATA_DIR", "AGY_APP_DATA_DIR", "USERS_DIR",
             "PROJECTS_ROOT", "SHARED_PROJECTS_DIR")
    env = dict(os.environ, **{key: str(tmp_path / key) for key in roots})
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["HUB_KEEP_DEFAULT_TERMINAL"] = "1"
    script = tmp_path / "child.py"
    script.write_text("""
import ctypes
import subprocess
from ctypes import wintypes
from pathlib import Path
from app.agy_engine import _build_subprocess_env, subprocess_hidden_kwargs

assert not ctypes.windll.kernel32.GetConsoleWindow()
user32 = ctypes.windll.user32
user32.GetProcessWindowStation.restype = wintypes.HANDLE
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
]
for handle, expected in (
    (user32.GetProcessWindowStation(), "WinSta0"),
    (user32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId()), "AgyHubHidden"),
):
    name = ctypes.create_unicode_buffer(512)
    assert user32.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), None)
    assert name.value == expected
env = _build_subprocess_env(isolated_home_override=Path.cwd() / "cli", proxy_url=None)
with Path("powershell.out").open("wb") as output:
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "echo AGY_PS_OK"],
        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
        env=env, timeout=10, check=True, **subprocess_hidden_kwargs(),
    )
""", encoding="utf-8")
    subprocess.run(
        [str(pythonw), "-m", "app.windows_desktop", sys.executable, str(script)], cwd=tmp_path, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=True,
        **subprocess_hidden_kwargs(),
    )
    assert (tmp_path / "powershell.out").read_text().strip() == "AGY_PS_OK"


@pytest.mark.parametrize(("staged", "pytest_fails", "status"), [
    (True, False, 0),
    (True, True, 1),
    (False, False, 1),
])
def test_verify_hub_handles_external_db_write(monkeypatch, tmp_path, capsys, staged, pytest_fails, status):
    from tools import verify_hub

    db = tmp_path / "hub.db"
    db.write_bytes(b"before")
    monkeypatch.setattr(verify_hub, "LIVE_DB", db)
    monkeypatch.setattr(sys, "argv", ["verify_hub.py", *(["--staged-guard"] if staged else [])])
    monkeypatch.setenv("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY", "1")
    monkeypatch.setattr(verify_hub, "check_sources", lambda: None)
    staged_calls = []
    monkeypatch.setattr(verify_hub, "staged_guard", lambda: staged_calls.append(True))

    def run(*args, env=None):
        if args[1:3] == ("-m", "pytest"):
            assert env.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") == ("1" if staged else None)
            db.write_bytes(b"external update")
            if pytest_fails:
                raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(verify_hub, "run", run)
    assert verify_hub.main() == status
    assert staged_calls == ([True] if staged else [])
    out = capsys.readouterr().out
    assert "HUB_DB_UNCHANGED: False" in out
    assert ("HUB_DB_EXTERNAL_ACTIVITY" in out) is staged
    assert ("VERIFY_HUB_OK" in out) is (status == 0)


def test_staged_mode_still_guards_production_data_access(monkeypatch, tmp_path):
    import conftest

    monkeypatch.setenv("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY", "1")
    monkeypatch.setattr(conftest, "LIVE_DATA_DIR", tmp_path)
    db = tmp_path / "hub.db"
    with pytest.raises(AssertionError, match="connect SQLite to production data"):
        sqlite3.connect(db)
    with pytest.raises(AssertionError, match="connect SQLite to production data"):
        sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    with pytest.raises(AssertionError, match="open production data"):
        builtins.open(db)
    with pytest.raises(AssertionError, match="open production data"):
        db.write_bytes(b"blocked")
    assert not db.exists()
