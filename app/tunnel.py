from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from dotenv import dotenv_values

from . import config

logger = logging.getLogger("agy_hub.tunnel")

_tunnel_task: Optional[asyncio.Task] = None
_tunnel_proc: Optional[asyncio.subprocess.Process] = None
_job_handle: Optional[int] = None
_status: dict[str, Any] = {
    "enabled": False,
    "connected": False,
    "target": "",
    "remote_port": 0,
    "last_error": "",
    "connected_since": 0.0,
}


def _ensure_win32_kill_on_close_job() -> Optional[int]:
    """Create a Windows Job Object that terminates child ssh.exe if parent exits abruptly."""
    global _job_handle
    if os.name != "nt":
        return None
    if _job_handle is not None:
        return _job_handle
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        _job_handle = int(job)
        return _job_handle
    except Exception:
        return None


def _attach_pid_to_job(pid: int) -> None:
    if os.name != "nt":
        return
    job = _ensure_win32_kill_on_close_job()
    if not job:
        return
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        h_proc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if h_proc:
            try:
                kernel32.AssignProcessToJobObject(job, h_proc)
            finally:
                kernel32.CloseHandle(h_proc)
    except Exception:
        pass


def _resolve_ssh_bin() -> str:
    if os.name == "nt":
        win_ssh = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
        if win_ssh.exists():
            return str(win_ssh)
    return shutil.which("ssh") or "ssh"


_STDERR_TAIL_MAX_BYTES = 8192
_env_cache_key: Optional[tuple[str, Optional[int], Optional[int]]] = None
_env_cache_values: dict[str, Optional[str]] = {}


def _read_dotenv_cached_sync(env_path: Path) -> dict[str, Optional[str]]:
    global _env_cache_key, _env_cache_values
    try:
        st = env_path.stat()
        key = (str(env_path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(env_path), None, None)
    if _env_cache_key == key:
        return _env_cache_values
    vals = dict(dotenv_values(env_path)) if key[1] is not None else {}
    _env_cache_key = key
    _env_cache_values = vals
    return vals


def _load_tunnel_settings() -> dict[str, Any]:
    file_vals = _read_dotenv_cached_sync(config.ENV_PATH)

    def _get_val(name: str, default: str = "") -> str:
        v = file_vals.get(name)
        if v is not None:
            return str(v)
        return os.getenv(name, default)

    enabled_raw = _get_val("HUB_TUNNEL_ENABLED", "0").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    target = _get_val("HUB_TUNNEL_SSH_TARGET", "").strip()
    ssh_port = int(_get_val("HUB_TUNNEL_SSH_PORT", "22").strip() or "22")
    remote_port = int(_get_val("HUB_TUNNEL_REMOTE_PORT", "18765").strip() or "18765")
    default_key = config.DATA_DIR / "ssh" / "hub_tunnel_ed25519"
    key_path = Path(_get_val("HUB_TUNNEL_SSH_KEY", str(default_key)).strip() or str(default_key))
    return {
        "enabled": enabled and bool(target),
        "target": target,
        "ssh_port": ssh_port,
        "remote_port": remote_port,
        "key_path": key_path,
    }


def get_tunnel_status() -> dict[str, Any]:
    return dict(_status)


async def _drain_stderr_tail(
    stream: Optional[asyncio.StreamReader],
    tail: deque[bytes],
    max_bytes: int = _STDERR_TAIL_MAX_BYTES,
) -> None:
    if stream is None:
        return
    total = 0
    try:
        while True:
            chunk = await stream.read(2048)
            if not chunk:
                break
            if len(chunk) > max_bytes:
                chunk = chunk[-max_bytes:]
            tail.append(chunk)
            total += len(chunk)
            while total > max_bytes and tail:
                removed = tail.popleft()
                total -= len(removed)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _terminate_tunnel_proc(proc: Optional[asyncio.subprocess.Process]) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
        return
    except Exception:
        pass
    if proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass


async def _supervise_tunnel_loop() -> None:
    global _tunnel_proc
    while True:
        cfg = await asyncio.to_thread(_load_tunnel_settings)
        _status["enabled"] = cfg["enabled"]
        _status["target"] = cfg["target"]
        _status["remote_port"] = cfg["remote_port"]

        if not cfg["enabled"]:
            _status["connected"] = False
            await asyncio.sleep(10.0)
            continue

        key_path: Path = cfg["key_path"]
        if not key_path.exists():
            _status["connected"] = False
            _status["last_error"] = f"SSH key missing: {key_path}"
            logger.warning("Tunnel SSH key not found at %s", key_path)
            await asyncio.sleep(10.0)
            continue

        ssh_bin = _resolve_ssh_bin()
        forward_spec = f"127.0.0.1:{cfg['remote_port']}:127.0.0.1:{config.HUB_PORT}"
        args = [
            ssh_bin,
            "-N",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=20",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-i",
            str(key_path),
            "-p",
            str(cfg["ssh_port"]),
            "-R",
            forward_spec,
            cfg["target"],
        ]

        stderr_tail: deque[bytes] = deque()
        drain_task: Optional[asyncio.Task[None]] = None
        try:
            logger.info("Connecting reverse SSH tunnel -> %s (%s)", cfg["target"], forward_spec)
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **config.subprocess_hidden_kwargs(),
            )
            _tunnel_proc = proc
            if proc.pid:
                _attach_pid_to_job(proc.pid)
            drain_task = asyncio.create_task(_drain_stderr_tail(proc.stderr, stderr_tail))

            # Wait briefly to verify ExitOnForwardFailure didn't abort immediately
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.5)
                try:
                    await asyncio.wait_for(drain_task, timeout=1.0)
                except Exception:
                    pass
                err_msg = b"".join(stderr_tail).decode("utf-8", errors="replace").strip()
                _status["connected"] = False
                _status["last_error"] = err_msg or f"ssh exited with code {proc.returncode}"
                logger.warning("Reverse SSH tunnel exited early (%s): %s", proc.returncode, _status["last_error"])
            except asyncio.TimeoutError:
                # Tunnel stayed alive past handshake & remote port bind!
                _status["connected"] = True
                _status["connected_since"] = time.time()
                _status["last_error"] = ""
                logger.info("Public HTTPS reverse tunnel active via %s", config.get_public_url() or cfg["target"])
                await proc.wait()
                try:
                    await asyncio.wait_for(drain_task, timeout=1.0)
                except Exception:
                    pass
                err_msg = b"".join(stderr_tail).decode("utf-8", errors="replace").strip()
                _status["connected"] = False
                _status["last_error"] = err_msg or f"disconnected (code {proc.returncode})"
                logger.warning("Reverse SSH tunnel disconnected (%s); reconnecting soon...", _status["last_error"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _status["connected"] = False
            _status["last_error"] = str(exc)
            logger.warning("Reverse SSH tunnel error: %s", exc)
        finally:
            proc_to_stop = _tunnel_proc
            _tunnel_proc = None
            await _terminate_tunnel_proc(proc_to_stop)
            if drain_task is not None and not drain_task.done():
                drain_task.cancel()
                try:
                    await drain_task
                except (asyncio.CancelledError, Exception):
                    pass

        await asyncio.sleep(4.0)


def start_tunnel_supervisor() -> None:
    global _tunnel_task
    if _tunnel_task is None or _tunnel_task.done():
        _tunnel_task = asyncio.create_task(_supervise_tunnel_loop())


async def stop_tunnel_supervisor() -> None:
    global _tunnel_task, _tunnel_proc
    proc = _tunnel_proc
    if proc and proc.returncode is None:
        await _terminate_tunnel_proc(proc)
    if _tunnel_task and not _tunnel_task.done():
        _tunnel_task.cancel()
        try:
            await _tunnel_task
        except asyncio.CancelledError:
            pass
    _tunnel_task = None
    _tunnel_proc = None
    _status["connected"] = False
