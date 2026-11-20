from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import config, database
from .sandbox_service import is_loopback_mcp_request

MCP_HOST_SERVER_NAME = "hub_host"
MCP_PROTOCOL_VERSION = "2024-11-05"
_SUPPORTED_MCP_PROTOCOLS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}

MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB log rotation threshold
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,48}$")
_PROTECTED_ENV_KEYS = {
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "OS",
    "PROCESSOR_ARCHITECTURE",
    "TEMP",
    "TMP",
}

# ---------------------------------------------------------------------------
# 1. Win32 Kernel32 Process Fingerprinting (pid, create_time_ft)
# ---------------------------------------------------------------------------

if os.name == "nt":

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE

    _k32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _k32.GetProcessTimes.restype = wintypes.BOOL

    _k32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL

    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    _get_proc_mem_info_fn = getattr(_k32, "K32GetProcessMemoryInfo", None)
    if _get_proc_mem_info_fn is None:
        try:
            _psapi = ctypes.WinDLL("psapi", use_last_error=True)
            _get_proc_mem_info_fn = getattr(_psapi, "GetProcessMemoryInfo", None)
        except Exception:
            _get_proc_mem_info_fn = None
    if _get_proc_mem_info_fn is not None:
        _get_proc_mem_info_fn.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        _get_proc_mem_info_fn.restype = wintypes.BOOL


def get_process_working_set_mb(pid: int) -> Optional[float]:
    """Return WorkingSetSize in MB (rounded to 0.1 MB) on Windows, or `None` on non-Windows/error."""
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    if _get_proc_mem_info_fn is None:
        return None
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        if not _get_proc_mem_info_fn(handle, ctypes.byref(pmc), pmc.cb):
            return None
        ws_bytes = int(pmc.WorkingSetSize or 0)
        if ws_bytes <= 0:
            return 0.0
        mb = round(ws_bytes / (1024.0 * 1024.0), 1)
        return mb if mb > 0.0 else 0.1
    except Exception:
        return None
    finally:
        _k32.CloseHandle(handle)


def get_process_fingerprint(pid: int) -> tuple[bool, int, Optional[int]]:
    """Return `(is_alive, create_time_ft, exit_code)` without third-party dependencies."""
    if not isinstance(pid, int) or pid <= 0:
        return (False, 0, None)

    if os.name == "nt":
        handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return (False, 0, None)
        try:
            creation = _FILETIME()
            exit_t = _FILETIME()
            kernel_t = _FILETIME()
            user_t = _FILETIME()
            create_ft = 0
            if _k32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_t),
                ctypes.byref(kernel_t),
                ctypes.byref(user_t),
            ):
                create_ft = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)

            exit_code = wintypes.DWORD(0)
            if _k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                code_val = int(exit_code.value)
                if code_val == STILL_ACTIVE:
                    return (True, create_ft, None)
                return (False, create_ft, code_val)
            return (False, create_ft, None)
        finally:
            _k32.CloseHandle(handle)

    # POSIX fallback
    try:
        os.kill(pid, 0)
    except OSError:
        return (False, 0, None)
    start_ticks = 0
    stat_file = Path(f"/proc/{pid}/stat")
    if stat_file.exists():
        try:
            raw = stat_file.read_text(encoding="utf-8", errors="ignore")
            rparen = raw.rfind(")")
            if rparen > 0:
                fields = raw[rparen + 2 :].split()
                if len(fields) > 19:
                    start_ticks = int(fields[19])
        except Exception:
            start_ticks = 0
    return (True, start_ticks, None)


def is_same_alive_process(pid: int, expected_create_time_ft: int) -> bool:
    alive, actual_ft, _ = get_process_fingerprint(pid)
    if not alive:
        return False
    if expected_create_time_ft > 0 and actual_ft > 0 and actual_ft != expected_create_time_ft:
        return False
    return True


def _kill_verified_process_tree(pid: int, expected_create_time_ft: int) -> bool:
    """Safely terminate process tree ONLY if `(pid, create_time_ft)` still matches."""
    if pid <= 0 or pid == os.getpid():
        return False
    if not is_same_alive_process(pid, expected_create_time_ft):
        return False

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8.0,
                check=False,
                **config.subprocess_hidden_kwargs(),
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if not is_same_alive_process(pid, expected_create_time_ft):
            return True
        time.sleep(0.08)

    if os.name != "nt":
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    return not is_same_alive_process(pid, expected_create_time_ft)


# ---------------------------------------------------------------------------
# 2. Port Inspection, .env Parsing & Log Helpers
# ---------------------------------------------------------------------------


def _is_within_dir(child: Path, parent: Path) -> bool:
    try:
        c_res = child.resolve()
        p_res = parent.resolve()
        return c_res == p_res or p_res in c_res.parents
    except Exception:
        return False


def _safe_slug(raw: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ_.\-]+", "_", (raw or "").strip()).strip("._")
    return cleaned[:64] or fallback


def _normalize_service_command(cmd: str) -> str:
    return " ".join((cmd or "").strip().split()).lower()


def _check_tcp_port_listening(port: int, timeout: float = 0.18) -> bool:
    if port <= 0 or port > 65535:
        return False
    for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def _find_listening_pid_on_port(port: int) -> Optional[int]:
    """Inspect `netstat -ano` (hidden) to find which PID owns a listening TCP port."""
    if port <= 0 or port > 65535:
        return None
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3.5,
                check=False,
                **config.subprocess_hidden_kwargs(),
            )
            needle = f":{port}"
            for line in (proc.stdout or "").splitlines():
                parts = line.split()
                if len(parts) < 5:
                    continue
                proto, local_addr, _foreign, state, pid_str = (
                    parts[0],
                    parts[1],
                    parts[2],
                    parts[3],
                    parts[4],
                )
                if proto.upper() != "TCP" or state.upper() != "LISTENING":
                    continue
                if local_addr.endswith(needle):
                    try:
                        found_pid = int(pid_str)
                        if found_pid > 0:
                            return found_pid
                    except ValueError:
                        continue
        except Exception:
            return None
    return None


def _parse_dotenv_file(dotenv_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not dotenv_path.exists() or not dotenv_path.is_file():
        return out
    try:
        raw_text = dotenv_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        k = key.strip()
        if not k or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            continue
        if k.upper() in _PROTECTED_ENV_KEYS:
            continue
        v = val.strip()
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        out[k] = v
    return out


def _rotate_log_if_needed(log_path: Path) -> None:
    try:
        if log_path.exists() and log_path.is_file() and log_path.stat().st_size > MAX_LOG_BYTES:
            rotated = log_path.with_name(log_path.name + ".1")
            try:
                rotated.unlink(missing_ok=True)
            except OSError:
                pass
            os.replace(log_path, rotated)
    except OSError:
        pass


def _read_log_tail(log_path: Path, tail_lines: int = 40, max_chars: int = 16000) -> str:
    clamped = max(5, min(int(tail_lines or 40), 400))
    if not log_path.exists() or not log_path.is_file():
        return ""
    try:
        size = log_path.stat().st_size
        read_bytes = min(size, max(65536, clamped * 512))
        with log_path.open("rb") as fh:
            if size > read_bytes:
                fh.seek(size - read_bytes)
            chunk = fh.read()
        text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines()
        tail = "\n".join(lines[-clamped:]).strip()
        if len(tail) > max_chars:
            tail = "...[лог сокращён]...\n" + tail[-max_chars:]
        return tail
    except OSError:
        return ""


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _rewrite_relocated_path(
    raw_path: str,
    old_base: Path | str,
    new_base: Path | str,
) -> tuple[str, bool]:
    raw = str(raw_path or "")
    old_str = str(old_base or "").strip()
    new_str = str(new_base or "").strip()
    if not raw.strip() or not old_str or not new_str:
        return raw, False

    old_norm = re.sub(r"(?<!^)/+", "/", old_str.replace("\\", "/")).rstrip("/")
    raw_norm = re.sub(r"(?<!^)/+", "/", raw.strip().replace("\\", "/")).rstrip("/")
    if not old_norm or not raw_norm:
        return raw, False

    try:
        new_abs = str(Path(new_str).resolve())
    except OSError:
        new_abs = os.path.normpath(new_str)
    new_slash = new_abs.replace("\\", "/").rstrip("/")

    case_insensitive = os.name == "nt" or bool(re.match(r"^[A-Za-z]:/", old_norm))
    old_cmp = old_norm.lower() if case_insensitive else old_norm
    raw_cmp = raw_norm.lower() if case_insensitive else raw_norm
    use_forward_slashes = ("/" in raw) and ("\\" not in raw)

    if raw_cmp == old_cmp:
        rebuilt = new_slash if use_forward_slashes else new_abs
        return rebuilt, rebuilt != raw

    if raw_cmp.startswith(old_cmp + "/"):
        rel_tail = raw_norm[len(old_norm) :].lstrip("/")
        if use_forward_slashes:
            rebuilt = f"{new_slash}/{rel_tail}" if rel_tail else new_slash
        else:
            rebuilt = str(Path(new_abs) / Path(rel_tail)) if rel_tail else new_abs
        return rebuilt, rebuilt != raw

    return raw, False


HOST_WATCHDOG_INTERVAL_SECONDS = 5.0
_HOST_WATCHDOG_TASK: Optional[asyncio.Task[None]] = None

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "sk-***"),
    (re.compile(r"\bgsk_[A-Za-z0-9_\-]{16,}\b"), "gsk_***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,}\b"), "gh_***"),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}\b"), "hf_***"),
    (re.compile(r"\b(\d{8,12}):[A-Za-z0-9_\-]{28,50}\b"), r"\1:***"),
    (
        re.compile(
            r"((?:api[_-]?key|secret|token|password|authorization|bearer)\s*[:=]\s*[\"']?)([A-Za-z0-9._\-/+=]{12,})([\"']?)",
            re.IGNORECASE,
        ),
        r"\1***\3",
    ),
    (
        re.compile(
            r"\b((?:https?|socks5h?|vless|trojan)://)([^/\s:@]+:[^/\s@]+)@",
            re.IGNORECASE,
        ),
        r"\1***@",
    ),
)
_WIN_ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+")


def _compute_scope_hash(project_dir: Optional[Path | str]) -> str:
    if project_dir is None:
        return ""
    raw = str(project_dir).strip()
    if not raw:
        return ""
    try:
        norm = os.path.normcase(str(Path(raw).resolve()))
    except Exception:
        norm = os.path.normcase(os.path.normpath(raw))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def _compute_duration_sec(started_at: str, end_at: Optional[str] = None) -> Optional[float]:
    raw_start = (started_at or "").strip()
    if not raw_start:
        return None
    try:
        start_dt = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    raw_end = (end_at or "").strip()
    if raw_end:
        try:
            end_dt = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        except Exception:
            end_dt = datetime.now(timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)
    return round(max(0.0, (end_dt - start_dt).total_seconds()), 1)


def _relativize_workdir_for_guest(raw_workdir: str, project_dir: Optional[Path | str] = None) -> str:
    wd = str(raw_workdir or "").strip()
    if not wd or wd == ".":
        return "."
    wd_slash = wd.replace("\\", "/")
    if wd_slash == "/workspace":
        return "."
    if wd_slash.startswith("/workspace/"):
        rel_c = wd_slash[len("/workspace/") :].strip("/")
        return rel_c or "."

    if project_dir is not None and str(project_dir).strip():
        try:
            proj_res = Path(project_dir).resolve()
            wd_res = Path(wd).resolve()
            if wd_res == proj_res:
                return "."
            if _is_within_dir(wd_res, proj_res):
                rel_p = wd_res.relative_to(proj_res).as_posix().strip("/")
                return rel_p or "."
        except Exception:
            pass

    if re.match(r"^[A-Za-z]:[\\/]", wd) or wd_slash.startswith("/"):
        return "."
    return wd_slash.strip("/") or "."


def mask_sensitive_log_text(
    text: str,
    *,
    is_admin: bool = False,
    project_dir: Optional[Path | str] = None,
    max_lines: int = 0,
) -> str:
    if not text:
        return ""
    cleaned = text
    for pat, repl in _SECRET_PATTERNS:
        cleaned = pat.sub(repl, cleaned)

    if not is_admin:
        if project_dir is not None and str(project_dir).strip():
            try:
                proj_str = str(Path(project_dir).resolve())
                for variant in {proj_str, proj_str.replace("\\", "/")}:
                    if variant:
                        cleaned = re.sub(
                            re.escape(variant),
                            ".",
                            cleaned,
                            flags=re.IGNORECASE if os.name == "nt" else 0,
                        )
            except Exception:
                pass
        cleaned = _WIN_ABS_PATH_RE.sub("<host_path>", cleaned)

    if max_lines > 0:
        lines = cleaned.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        cleaned = "\n".join(lines[-max_lines:]).strip()
    return cleaned


def sanitize_process_view_for_user(
    view: dict[str, Any],
    user: Optional[dict[str, Any]],
    project_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Strip host filesystem paths (`log_path`, `project_dir`, absolute `workdir`) for non-admin users."""
    out = dict(view or {})
    out.pop("scope_hash", None)
    is_admin = bool(user and str(user.get("role") or "").strip().lower() == "admin")
    eff_proj_dir: Optional[Path | str] = project_dir or (out.get("project_dir") or None)
    if is_admin:
        return out

    out["log_path"] = ""
    out["project_dir"] = ""
    out["workdir"] = _relativize_workdir_for_guest(str(out.get("workdir") or "."), eff_proj_dir)

    if isinstance(out.get("command"), str) and out["command"]:
        cmd_clean = out["command"]
        if eff_proj_dir is not None and str(eff_proj_dir).strip():
            try:
                p_abs = str(Path(eff_proj_dir).resolve())
                for variant in {p_abs, p_abs.replace("\\", "/")}:
                    if variant:
                        cmd_clean = re.sub(
                            re.escape(variant),
                            ".",
                            cmd_clean,
                            flags=re.IGNORECASE if os.name == "nt" else 0,
                        )
            except Exception:
                pass
        cmd_clean = _WIN_ABS_PATH_RE.sub("<host_bin>", cmd_clean)
        out["command"] = cmd_clean

    if isinstance(out.get("log_tail"), str) and out["log_tail"]:
        out["log_tail"] = mask_sensitive_log_text(
            out["log_tail"],
            is_admin=False,
            project_dir=eff_proj_dir,
        )
    return out


# ---------------------------------------------------------------------------
# 3. HostServiceManager (Detached Processes + Persistent Registry)
# ---------------------------------------------------------------------------


class HostServiceManager:
    """Manages detached Windows Host background processes across Hub turns & restarts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded_data_dir: Optional[Path] = None
        self._services: dict[str, dict[str, Any]] = {}
        self._live_procs: dict[str, subprocess.Popen[Any]] = {}
        self._pending_transitions: list[dict[str, Any]] = []
        self._change_listeners: list[Callable[..., Awaitable[None]]] = []
        self._transition_listeners: list[Callable[[dict[str, Any]], Awaitable[None]]] = []

    def add_change_listener(self, cb: Callable[..., Awaitable[None]]) -> None:
        if cb not in self._change_listeners:
            self._change_listeners.append(cb)

    def add_transition_listener(self, cb: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        if cb not in self._transition_listeners:
            self._transition_listeners.append(cb)

    def _notify_listeners(self, project_name: str, project_dir: Optional[Path | str] = None) -> None:
        if not project_name or not self._change_listeners:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        proj_path: Optional[Path] = None
        if project_dir is not None and str(project_dir).strip():
            try:
                proj_path = Path(project_dir).resolve()
            except Exception:
                proj_path = None
        for cb in list(self._change_listeners):
            try:
                try:
                    coro = cb(project_name, proj_path)
                except TypeError:
                    coro = cb(project_name)
                loop.create_task(coro)
            except Exception:
                pass

    def _root_dir(self) -> Path:
        return (config.DATA_DIR / "host_services").resolve()

    def _registry_path(self) -> Path:
        return self._root_dir() / "registry.json"

    @staticmethod
    def _make_key(
        project_name: str,
        service_name: str,
        project_dir: Optional[Path | str] = None,
    ) -> str:
        clean_p = (project_name or "").strip().lower()
        clean_s = (service_name or "").strip().lower()
        scope_h = _compute_scope_hash(project_dir)
        if scope_h:
            return f"{scope_h}::{clean_p}::{clean_s}"
        return f"{clean_p}::{clean_s}"

    def _reap_proc_handle_locked(self, key: str) -> Optional[int]:
        proc = self._live_procs.pop(key, None)
        if proc is None:
            return None
        try:
            rc = proc.poll()
            return int(rc) if rc is not None else None
        except Exception:
            return None

    def _clear_pending_transitions_for_key_locked(self, key: str) -> None:
        if not self._pending_transitions:
            return
        self._pending_transitions = [
            ev for ev in self._pending_transitions if ev.get("_key") != key
        ]

    def _queue_transition_locked(
        self,
        key: str,
        entry: dict[str, Any],
        view: dict[str, Any],
        transition: str,
    ) -> None:
        error_tail = ""
        if transition == "crashed":
            log_p = str(entry.get("log_path") or "")
            if log_p:
                raw_tail = _read_log_tail(Path(log_p), tail_lines=25, max_chars=8000)
                error_tail = mask_sensitive_log_text(raw_tail, is_admin=True, max_lines=25)
        ev = {
            "_key": key,
            "project_name": str(entry.get("project_name") or ""),
            "project_dir": str(entry.get("project_dir") or ""),
            "service": dict(view),
            "transition": transition,
            "error_tail": error_tail,
        }
        if len(self._pending_transitions) >= 200:
            self._pending_transitions.pop(0)
        self._pending_transitions.append(ev)

    def _ensure_loaded_locked(self) -> None:
        cur_data_dir = config.DATA_DIR.resolve()
        if self._loaded_data_dir == cur_data_dir:
            return
        self._loaded_data_dir = cur_data_dir
        self._services = {}
        self._live_procs = {}
        self._pending_transitions = []
        reg_file = self._registry_path()
        if not reg_file.exists():
            return
        try:
            raw = json.loads(reg_file.read_text(encoding="utf-8"))
            items = raw.get("services") if isinstance(raw, dict) else []
            if isinstance(items, list):
                for entry in items:
                    if not isinstance(entry, dict):
                        continue
                    pname = str(entry.get("project_name") or "").strip()
                    sname = str(entry.get("name") or "").strip()
                    if not pname or not sname:
                        continue
                    rec = dict(entry)
                    pdir_str = str(rec.get("project_dir") or "").strip()
                    if pdir_str:
                        rec["scope_hash"] = _compute_scope_hash(pdir_str)
                        k = self._make_key(pname, sname, pdir_str)
                    else:
                        k = self._make_key(pname, sname)
                    self._services[k] = rec
        except Exception:
            self._services = {}

    def _entry_matches_project_locked(
        self,
        entry: dict[str, Any],
        clean_proj_low: str,
        target_scope_hash: str,
        target_proj_res: Optional[Path],
    ) -> bool:
        if str(entry.get("project_name") or "").strip().lower() != clean_proj_low:
            return False
        if not target_scope_hash:
            return True
        entry_scope = str(entry.get("scope_hash") or "").strip()
        if not entry_scope and entry.get("project_dir"):
            entry_scope = _compute_scope_hash(str(entry.get("project_dir")))
            entry["scope_hash"] = entry_scope
        if entry_scope:
            return entry_scope == target_scope_hash
        # Legacy entry without `project_dir`: match ONLY if its `workdir` lies inside `target_proj_res`
        if target_proj_res is not None and entry.get("workdir"):
            try:
                if _is_within_dir(Path(str(entry["workdir"])), target_proj_res):
                    entry["project_dir"] = str(target_proj_res)
                    entry["scope_hash"] = target_scope_hash
                    return True
            except Exception:
                pass
        return False

    def _resolve_entry_locked(
        self,
        project_name: str,
        service_name: str,
        project_dir: Optional[Path] = None,
    ) -> tuple[str, Optional[dict[str, Any]]]:
        clean_p = (project_name or "").strip()
        clean_s = (service_name or "").strip()
        if project_dir is not None:
            proj_res = project_dir.resolve()
            scoped_key = self._make_key(clean_p, clean_s, proj_res)
            found = self._services.get(scoped_key)
            if found is not None:
                return scoped_key, found
            # Soft-migrate legacy unscoped key ONLY if its `project_dir` or `workdir` matches `proj_res`
            legacy_key = self._make_key(clean_p, clean_s, None)
            leg = self._services.get(legacy_key)
            if leg is not None and self._entry_matches_project_locked(
                leg, clean_p.lower(), _compute_scope_hash(proj_res), proj_res
            ):
                self._services.pop(legacy_key, None)
                leg["project_dir"] = str(proj_res)
                leg["scope_hash"] = _compute_scope_hash(proj_res)
                self._services[scoped_key] = leg
                if legacy_key in self._live_procs:
                    self._live_procs[scoped_key] = self._live_procs.pop(legacy_key)
                return scoped_key, leg
            return scoped_key, None

        legacy_key = self._make_key(clean_p, clean_s, None)
        if legacy_key in self._services:
            return legacy_key, self._services[legacy_key]
        # Fallback when `project_dir` is omitted (legacy single-project calls): match by `(project_name, name)` if unambiguous
        matches = [
            (k, e)
            for k, e in self._services.items()
            if str(e.get("project_name") or "").strip().lower() == clean_p.lower()
            and str(e.get("name") or "").strip().lower() == clean_s.lower()
        ]
        if len(matches) == 1:
            return matches[0]
        return legacy_key, None

    def _save_locked(self) -> None:
        reg_file = self._registry_path()
        payload = {
            "version": 2,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "services": list(self._services.values()),
        }
        _atomic_write_text(reg_file, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _project_entries(self, project_name: str, project_dir: Optional[Path]):
        with self._lock:
            self._ensure_loaded_locked()
            target = project_dir.resolve() if project_dir else None
            scope = _compute_scope_hash(target) if target else ""
            return [(k, dict(e)) for k, e in self._services.items()
                    if self._entry_matches_project_locked(e, project_name.lower(), scope, target)]

    def stop_all_project_services(self, project_name: str, project_dir: Optional[Path] = None) -> int:
        entries = self._project_entries(project_name, project_dir)
        for _, entry in entries:
            self.control_service(project_name=project_name, project_dir=project_dir,
                                 action="stop", name=entry["name"])
        return len(entries)

    def rename_project_in_registry(self, old_name: str, old_dir: Path, new_name: str, new_dir: Path) -> int:
        with self._lock:
            entries = self._project_entries(old_name, old_dir)
            for key, _ in entries:
                entry = self._services.pop(key)
                entry["project_name"] = new_name
                for field in ("project_dir", "workdir", "log_path"):
                    entry[field] = _rewrite_relocated_path(str(entry.get(field) or ""), old_dir, new_dir)[0]
                command = str(entry.get("command") or "")
                entry["command"] = re.sub(re.escape(str(old_dir)), lambda m: str(new_dir), command,
                                          flags=re.IGNORECASE if os.name == "nt" else 0)
                entry["project_dir"] = str(new_dir)
                entry["scope_hash"] = _compute_scope_hash(new_dir)
                new_key = self._make_key(new_name, entry["name"], new_dir)
                self._services[new_key] = entry
                if key in self._live_procs:
                    self._live_procs[new_key] = self._live_procs.pop(key)
            self._save_locked()
            return len(entries)

    def purge_project_services(self, project_name: str, project_dir: Optional[Path] = None) -> int:
        self.stop_all_project_services(project_name, project_dir)
        with self._lock:
            entries = self._project_entries(project_name, project_dir)
            for key, entry in entries:
                self._services.pop(key, None)
                log = Path(str(entry.get("log_path") or ""))
                root = self._registry_path().parent.resolve()
                if log.is_file() and not log.is_symlink() and root in log.resolve().parents:
                    log.unlink(missing_ok=True)
            self._save_locked()
            return len(entries)

    def relocate_registry_paths(self, old_base: Path | str, new_base: Path | str) -> int:
        """Atomically rewrite `project_dir`, `workdir`, and `log_path` when Hub base folder moves."""
        with self._lock:
            self._loaded_data_dir = None
            self._ensure_loaded_locked()
            if not self._services:
                return 0
            updated_count = 0
            rebuilt: dict[str, dict[str, Any]] = {}
            for old_key, entry in list(self._services.items()):
                entry_changed = False
                for field_name in ("project_dir", "workdir", "log_path"):
                    cur_val = str(entry.get(field_name) or "")
                    if not cur_val:
                        continue
                    new_val, changed = _rewrite_relocated_path(cur_val, old_base, new_base)
                    if changed:
                        entry[field_name] = new_val
                        entry_changed = True
                cmd_val = str(entry.get("command") or "")
                old_str = str(old_base or "").strip()
                new_str = str(new_base or "").strip()
                if cmd_val and old_str and new_str and old_str != new_str:
                    pat = re.compile(re.escape(old_str), re.IGNORECASE if os.name == "nt" else 0)
                    new_cmd = pat.sub(lambda _m: new_str, cmd_val)
                    if new_cmd != cmd_val:
                        entry["command"] = new_cmd
                        entry_changed = True
                pdir_val = str(entry.get("project_dir") or "").strip()
                if pdir_val:
                    new_scope = _compute_scope_hash(pdir_val)
                    if entry.get("scope_hash") != new_scope:
                        entry["scope_hash"] = new_scope
                        entry_changed = True
                    new_key = self._make_key(
                        str(entry.get("project_name") or ""),
                        str(entry.get("name") or ""),
                        pdir_val,
                    )
                else:
                    new_key = old_key
                rebuilt[new_key] = entry
                if entry_changed:
                    updated_count += 1
            self._services = rebuilt
            if updated_count > 0:
                self._save_locked()
            return updated_count

    def _refresh_entry_liveness_locked(
        self,
        entry: dict[str, Any],
        key: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Verify `(pid, create_time_ft)` against OS; returns `(public_view, registry_mutated)`."""
        mutated = False
        pid = int(entry.get("pid") or 0)
        expected_ft = int(entry.get("create_time_ft") or 0)
        alive = False
        exit_code: Optional[int] = entry.get("exit_code")
        transition_to: Optional[str] = None

        if pid > 0:
            proc_alive, actual_ft, proc_exit = get_process_fingerprint(pid)
            held_proc = self._live_procs.get(key) if key else None
            if not proc_alive and proc_exit is None and held_proc is not None:
                try:
                    polled = held_proc.poll()
                    if polled is not None:
                        proc_exit = int(polled)
                except Exception:
                    pass

            if proc_alive and (expected_ft <= 0 or actual_ft <= 0 or actual_ft == expected_ft):
                alive = True
                exit_code = None
                if entry.get("status") != "running":
                    entry["status"] = "running"
                    mutated = True
            else:
                alive = False
                reaped_rc = self._reap_proc_handle_locked(key) if key else None
                if proc_exit is None and reaped_rc is not None:
                    proc_exit = reaped_rc

                if expected_ft > 0 and actual_ft > 0 and actual_ft != expected_ft:
                    # OS reused PID for an unrelated process — clear stale PID in registry
                    prev_st = str(entry.get("status") or "stopped")
                    entry["pid"] = 0
                    if prev_st == "running":
                        now_iso = datetime.now(timezone.utc).isoformat()
                        entry["status"] = "stopped"
                        entry["stopped_at"] = now_iso
                        entry["duration_sec"] = _compute_duration_sec(
                            str(entry.get("started_at") or ""), now_iso
                        )
                        entry["crash_unseen"] = False
                        transition_to = "stopped"
                    mutated = True
                else:
                    if proc_exit is not None and entry.get("exit_code") != proc_exit:
                        entry["exit_code"] = proc_exit
                        exit_code = proc_exit
                        mutated = True
                    prev_status = str(entry.get("status") or "stopped")
                    if prev_status == "running":
                        now_iso = datetime.now(timezone.utc).isoformat()
                        dur = _compute_duration_sec(str(entry.get("started_at") or ""), now_iso)
                        entry["pid"] = 0
                        entry["last_pid"] = pid
                        entry["duration_sec"] = dur
                        if proc_exit == 0:
                            entry["status"] = "completed"
                            entry["finished_at"] = now_iso
                            entry["crash_unseen"] = False
                            transition_to = "completed"
                        elif proc_exit not in (None, 0):
                            entry["status"] = "crashed"
                            entry["finished_at"] = now_iso
                            entry["crash_unseen"] = True
                            transition_to = "crashed"
                        else:
                            entry["status"] = "stopped"
                            entry["stopped_at"] = now_iso
                            entry["crash_unseen"] = False
                            transition_to = "stopped"
                        mutated = True

        port_val = entry.get("port")
        port_int = int(port_val) if isinstance(port_val, int) and port_val > 0 else None
        port_listening = bool(alive and port_int and _check_tcp_port_listening(port_int, timeout=0.08))
        ram_mb = get_process_working_set_mb(pid) if alive and pid > 0 else None

        started_at_str = str(entry.get("started_at") or "")
        finished_at_str = str(entry.get("finished_at") or "")
        stopped_at_str = str(entry.get("stopped_at") or "")
        if alive:
            duration_sec = _compute_duration_sec(started_at_str)
        elif entry.get("duration_sec") is not None:
            duration_sec = float(entry["duration_sec"])
        else:
            duration_sec = _compute_duration_sec(started_at_str, finished_at_str or stopped_at_str)

        eff_status = "running" if alive else str(entry.get("status") or "stopped")
        crash_unseen = bool(entry.get("crash_unseen", False)) if eff_status == "crashed" else False

        view = {
            "runtime": "host",
            "project_name": str(entry.get("project_name") or ""),
            "project_dir": str(entry.get("project_dir") or ""),
            "name": str(entry.get("name") or ""),
            "command": str(entry.get("command") or ""),
            "workdir": str(entry.get("workdir") or ""),
            "pid": pid if alive else int(entry.get("pid") or entry.get("last_pid") or 0),
            "alive": alive,
            "status": eff_status,
            "exit_code": exit_code,
            "ram_mb": ram_mb,
            "duration_sec": duration_sec,
            "port": port_int,
            "port_listening": port_listening,
            "allow_gpu": bool(entry.get("allow_gpu", False)),
            "started_at": started_at_str,
            "finished_at": finished_at_str,
            "stopped_at": stopped_at_str,
            "crash_unseen": crash_unseen,
            "started_by": str(entry.get("started_by") or ""),
            "started_by_id": str(entry.get("started_by_id") or ""),
            "log_path": str(entry.get("log_path") or ""),
        }
        if transition_to in ("completed", "crashed", "stopped") and key:
            self._queue_transition_locked(key, entry, view, transition_to)
        return view, mutated

    def list_project_services(
        self,
        project_name: str,
        *,
        project_dir: Optional[Path] = None,
        include_tail_lines: int = 0,
    ) -> list[dict[str, Any]]:
        clean_proj = (project_name or "").strip().lower()
        if not clean_proj:
            return []
        target_res = project_dir.resolve() if project_dir is not None else None
        target_scope = _compute_scope_hash(target_res) if target_res is not None else ""
        with self._lock:
            self._ensure_loaded_locked()
            out: list[dict[str, Any]] = []
            any_mutated = False
            for k, entry in list(self._services.items()):
                if not self._entry_matches_project_locked(entry, clean_proj, target_scope, target_res):
                    continue
                view, mutated = self._refresh_entry_liveness_locked(entry, key=k)
                if mutated:
                    any_mutated = True
                if include_tail_lines > 0 and view.get("log_path"):
                    view["log_tail"] = _read_log_tail(Path(view["log_path"]), tail_lines=include_tail_lines)
                out.append(view)
            if any_mutated and self._registry_path().exists():
                self._save_locked()
        out.sort(
            key=lambda s: (
                0 if s.get("alive") else (1 if s.get("status") == "crashed" else 2),
                str(s.get("name") or "").lower(),
            )
        )
        return out

    def get_service_logs(
        self,
        project_name: str,
        name: str,
        *,
        project_dir: Optional[Path] = None,
        tail_lines: int = 80,
    ) -> dict[str, Any]:
        clean_name = self._validate_service_name(name)
        target_res = project_dir.resolve() if project_dir is not None else None
        target_scope = _compute_scope_hash(target_res) if target_res is not None else ""
        with self._lock:
            self._ensure_loaded_locked()
            key, entry = self._resolve_entry_locked(project_name, clean_name, project_dir=target_res)
            if not entry:
                if target_scope:
                    log_guess = (
                        self._root_dir()
                        / f"{_safe_slug(project_name, 'proj')}_{target_scope}"
                        / f"{_safe_slug(clean_name, 'app')}.log"
                    )
                else:
                    log_guess = (
                        self._root_dir()
                        / _safe_slug(project_name, "proj")
                        / f"{_safe_slug(clean_name, 'app')}.log"
                    )
                return {
                    "ok": True,
                    "runtime": "host",
                    "name": clean_name,
                    "project_name": project_name,
                    "alive": False,
                    "status": "not_found",
                    "pid": 0,
                    "port": None,
                    "crash_unseen": False,
                    "log_tail": _read_log_tail(log_guess, tail_lines=tail_lines),
                }
            view, mutated = self._refresh_entry_liveness_locked(entry, key=key)
            if mutated and self._registry_path().exists():
                self._save_locked()
            log_file = Path(view["log_path"]) if view.get("log_path") else None
            tail_txt = _read_log_tail(log_file, tail_lines=tail_lines) if log_file else ""
            return {
                "ok": True,
                **view,
                "log_tail": tail_txt,
            }

    def acknowledge_service_alert(
        self,
        project_name: str,
        name: str,
        *,
        project_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        clean_proj = (project_name or "").strip()
        clean_name = self._validate_service_name(name)
        target_res = project_dir.resolve() if project_dir is not None else None
        with self._lock:
            self._ensure_loaded_locked()
            key, entry = self._resolve_entry_locked(clean_proj, clean_name, project_dir=target_res)
            if not entry:
                return {
                    "ok": True,
                    "action": "ack",
                    "runtime": "host",
                    "project_name": clean_proj,
                    "name": clean_name,
                    "alive": False,
                    "status": "not_found",
                    "crash_unseen": False,
                }
            entry["crash_unseen"] = False
            view, _ = self._refresh_entry_liveness_locked(entry, key=key)
            view["crash_unseen"] = False
            self._save_locked()
            eff_dir = entry.get("project_dir") or target_res
        self._notify_listeners(clean_proj, eff_dir)
        return {"ok": True, "action": "ack", **view}

    def clear_inactive_services(
        self,
        project_name: str,
        *,
        project_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Remove strictly inactive (`alive == False`: completed, crashed, stopped) entries & logs.

        Live `running` processes are verified via OS fingerprint and are NEVER stopped or removed.
        """
        clean_proj = (project_name or "").strip()
        if not clean_proj:
            return {
                "ok": True,
                "runtime": "host",
                "project_name": "",
                "cleared_count": 0,
                "cleared_names": [],
                "kept_running_count": 0,
            }
        target_res = project_dir.resolve() if project_dir is not None else None
        target_scope = _compute_scope_hash(target_res) if target_res is not None else ""
        cleared_names: list[str] = []
        kept_running_count = 0
        logs_to_remove: list[str] = []
        eff_dir: Optional[Path | str] = target_res

        with self._lock:
            self._ensure_loaded_locked()
            any_mutated = False
            for k, entry in list(self._services.items()):
                if not self._entry_matches_project_locked(
                    entry, clean_proj.lower(), target_scope, target_res
                ):
                    continue
                view, mutated = self._refresh_entry_liveness_locked(entry, key=k)
                if mutated:
                    any_mutated = True
                if view.get("alive") or view.get("status") == "running":
                    kept_running_count += 1
                    continue

                self._reap_proc_handle_locked(k)
                self._clear_pending_transitions_for_key_locked(k)
                removed = self._services.pop(k, None)
                svc_name = str((removed or entry).get("name") or "")
                if svc_name:
                    cleared_names.append(svc_name)
                if eff_dir is None and (removed or entry).get("project_dir"):
                    eff_dir = str((removed or entry).get("project_dir"))
                log_p = str((removed or entry).get("log_path") or "").strip()
                if log_p:
                    logs_to_remove.append(log_p)

            live_log_paths: set[str] = set()
            for rem_entry in self._services.values():
                rem_lp = str(rem_entry.get("log_path") or "").strip()
                if rem_lp:
                    try:
                        live_log_paths.add(os.path.normcase(str(Path(rem_lp).resolve())))
                    except OSError:
                        live_log_paths.add(os.path.normcase(rem_lp))

            root_dir = self._root_dir()
            touched_dirs: set[Path] = set()
            for raw_lp in logs_to_remove:
                try:
                    lp = Path(raw_lp)
                    lp_res = lp.resolve()
                except OSError:
                    continue
                if os.path.normcase(str(lp_res)) in live_log_paths:
                    continue
                if not _is_within_dir(lp_res, root_dir) or lp_res == root_dir:
                    continue
                for candidate in (lp, lp.with_name(lp.name + ".1")):
                    try:
                        if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
                            candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
                if lp_res.parent != root_dir and _is_within_dir(lp_res.parent, root_dir):
                    touched_dirs.add(lp_res.parent)

            for d_cand in touched_dirs:
                try:
                    if d_cand.is_dir() and not any(d_cand.iterdir()):
                        d_cand.rmdir()
                except OSError:
                    pass

            if cleared_names or (any_mutated and self._registry_path().exists()):
                self._save_locked()

        if cleared_names:
            self._notify_listeners(clean_proj, eff_dir)

        return {
            "ok": True,
            "runtime": "host",
            "project_name": clean_proj,
            "cleared_count": len(cleared_names),
            "cleared_names": cleared_names,
            "kept_running_count": kept_running_count,
        }

    def poll_running_services_transitions(self) -> list[dict[str, Any]]:
        """O(1) when idle; polls running entries and returns state transition events."""
        with self._lock:
            self._ensure_loaded_locked()
            has_running = any(e.get("status") == "running" for e in self._services.values())
            if not has_running and not self._pending_transitions:
                return []

            any_mutated = False
            if has_running:
                for k, entry in list(self._services.items()):
                    if entry.get("status") != "running":
                        continue
                    _, mutated = self._refresh_entry_liveness_locked(entry, key=k)
                    if mutated:
                        any_mutated = True

            if any_mutated:
                self._save_locked()

            events: list[dict[str, Any]] = []
            for raw_ev in self._pending_transitions:
                ev = {k: v for k, v in raw_ev.items() if k != "_key"}
                events.append(ev)
            self._pending_transitions.clear()
            return events

    @staticmethod
    def _validate_service_name(name: str) -> str:
        clean = (name or "app").strip()
        if not clean:
            clean = "app"
        if not _SERVICE_NAME_RE.fullmatch(clean):
            raise ValueError(
                "Имя сервиса должно содержать от 1 до 48 латинских символов, цифр, '.', '_' или '-' (например: 'bot', 'web', 'api')."
            )
        return clean

    @staticmethod
    def _resolve_validated_workdir(
        raw_workdir: str,
        project_dir: Path,
        extra_dirs: Optional[list[Path]] = None,
    ) -> Path:
        proj_res = project_dir.resolve()
        allowed_roots = [proj_res]
        for ed in extra_dirs or []:
            try:
                allowed_roots.append(ed.resolve())
            except OSError:
                pass

        wd_str = (raw_workdir or ".").strip()
        if not wd_str or wd_str == ".":
            target = proj_res
        else:
            if "\x00" in wd_str:
                raise ValueError("Некорректный путь workdir.")
            cand = Path(wd_str)
            target = cand.resolve() if cand.is_absolute() else (proj_res / cand).resolve()

        if not any(_is_within_dir(target, root) for root in allowed_roots):
            raise ValueError(
                f"Рабочая папка '{wd_str}' выходит за пределы папки проекта '{proj_res}' и разрешённых дополнительных папок."
            )
        if not target.exists() or not target.is_dir():
            raise ValueError(f"Рабочая папка '{target}' не существует.")
        if config._is_junction_or_symlink(target):
            raise ValueError("Симлинки и junction-ссылки запрещены для workdir сервиса.")
        return target

    @staticmethod
    def _validate_port(port: Optional[int]) -> Optional[int]:
        if port is None or port == 0 or port == "":
            return None
        p_int = int(port)
        if p_int < 1 or p_int > 65535:
            raise ValueError("Порт должен быть в диапазоне 1..65535.")
        reserved = {int(config.HUB_PORT), int(getattr(config, "HUB_SSL_PORT", 8766)), 8765, 8766}
        if p_int in reserved:
            raise ValueError(f"Порт {p_int} зарезервирован самим сервером Antigravity Hub.")
        return p_int

    def _build_service_env(
        self,
        *,
        project_dir: Path,
        workdir: Path,
        allow_gpu: bool,
        user: Optional[dict[str, Any]],
    ) -> tuple[dict[str, str], bool]:
        env = os.environ.copy()
        # Strip Hub's own internal server config keys that could confuse child web apps (e.g. PORT=8765)
        for hub_key in ("HUB_PIN", "HUB_SECRET_KEY", "GROQ_API_KEY"):
            env.pop(hub_key, None)

        # Load project .env (and subfolder .env if workdir is a subdirectory)
        dotenv_vars = _parse_dotenv_file(project_dir / ".env")
        if workdir != project_dir:
            dotenv_vars.update(_parse_dotenv_file(workdir / ".env"))
        env.update(dotenv_vars)

        # Prepend project .venv/Scripts (or .venv/bin) to PATH when available; keep sys.executable dir as fallback
        venv_bins: list[str] = []
        for base_d in (workdir, project_dir):
            cand_scripts = base_d / ".venv" / ("Scripts" if os.name == "nt" else "bin")
            if cand_scripts.is_dir():
                s_bin = str(cand_scripts.resolve())
                if s_bin not in venv_bins:
                    venv_bins.append(s_bin)
        cur_path = env.get("PATH") or ""
        sys_py_dir = str(Path(sys.executable).resolve().parent)
        path_parts = [*venv_bins]
        if cur_path:
            path_parts.append(cur_path)
        if sys_py_dir and sys_py_dir.lower() not in cur_path.lower():
            path_parts.append(sys_py_dir)
        env["PATH"] = os.pathsep.join(path_parts)

        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        is_admin = bool(user and str(user.get("role") or "").strip().lower() == "admin")
        eff_allow_gpu = bool(allow_gpu and is_admin)
        if not eff_allow_gpu:
            env["CUDA_VISIBLE_DEVICES"] = ""
            env["ORT_DISABLE_ALL_GPU"] = "1"
        else:
            if env.get("CUDA_VISIBLE_DEVICES", None) == "" and "CUDA_VISIBLE_DEVICES" not in dotenv_vars:
                env.pop("CUDA_VISIBLE_DEVICES", None)
            if "ORT_DISABLE_ALL_GPU" not in dotenv_vars:
                env.pop("ORT_DISABLE_ALL_GPU", None)

        return env, eff_allow_gpu

    def control_service(
        self,
        *,
        project_name: str,
        project_dir: Optional[Path] = None,
        action: str,
        name: str = "app",
        command: str = "",
        workdir: str = ".",
        port: Optional[int] = None,
        allow_gpu: bool = False,
        replace: bool = False,
        startup_wait_ms: int = 1500,
        tail_lines: int = 40,
        user: Optional[dict[str, Any]] = None,
        extra_dirs: Optional[list[Path]] = None,
    ) -> dict[str, Any]:
        clean_action = str(action or "").strip().lower()
        if clean_action not in {"start", "stop", "restart", "status", "logs", "delete", "ack"}:
            raise ValueError(
                "action должен быть одним из: 'start', 'stop', 'restart', 'status', 'logs', 'delete', 'ack'."
            )

        clean_proj = (project_name or "").strip()
        if not clean_proj:
            raise ValueError("Не указано имя проекта.")
        clean_name = self._validate_service_name(name)
        proj_res = project_dir.resolve() if project_dir is not None else None

        if clean_action == "logs":
            return self.get_service_logs(
                clean_proj,
                clean_name,
                project_dir=proj_res,
                tail_lines=tail_lines,
            )

        if clean_action == "ack":
            return self.acknowledge_service_alert(
                clean_proj,
                clean_name,
                project_dir=proj_res,
            )

        if clean_action == "status":
            with self._lock:
                self._ensure_loaded_locked()
                key, entry = self._resolve_entry_locked(clean_proj, clean_name, project_dir=proj_res)
                if not entry:
                    all_svcs = self.list_project_services(clean_proj, project_dir=proj_res)
                    return {
                        "ok": True,
                        "action": "status",
                        "runtime": "host",
                        "project_name": clean_proj,
                        "name": clean_name,
                        "alive": False,
                        "status": "not_registered",
                        "pid": 0,
                        "crash_unseen": False,
                        "services": all_svcs,
                    }
                view, mutated = self._refresh_entry_liveness_locked(entry, key=key)
                if mutated and self._registry_path().exists():
                    self._save_locked()
                log_file = Path(view["log_path"]) if view.get("log_path") else None
                view["log_tail"] = _read_log_tail(log_file, tail_lines=tail_lines) if log_file else ""
                return {"ok": True, "action": "status", **view}

        if clean_action in {"stop", "delete"}:
            with self._lock:
                self._ensure_loaded_locked()
                key, entry = self._resolve_entry_locked(clean_proj, clean_name, project_dir=proj_res)
                if not entry:
                    return {
                        "ok": True,
                        "action": clean_action,
                        "runtime": "host",
                        "project_name": clean_proj,
                        "name": clean_name,
                        "alive": False,
                        "status": "stopped",
                        "pid": 0,
                        "crash_unseen": False,
                        "killed": False,
                    }
                old_pid = int(entry.get("pid") or 0)
                old_ft = int(entry.get("create_time_ft") or 0)
                started_at_prev = str(entry.get("started_at") or "")
                eff_dir_prev = entry.get("project_dir") or proj_res

            killed = _kill_verified_process_tree(old_pid, old_ft) if old_pid > 0 else False
            now_iso = datetime.now(timezone.utc).isoformat()
            dur_stop = _compute_duration_sec(started_at_prev, now_iso)
            with self._lock:
                self._ensure_loaded_locked()
                self._reap_proc_handle_locked(key)
                self._clear_pending_transitions_for_key_locked(key)
                entry = self._services.get(key)
                if entry:
                    if clean_action == "delete":
                        self._services.pop(key, None)
                    else:
                        entry["status"] = "stopped"
                        entry["crash_unseen"] = False
                        entry["stopped_at"] = now_iso
                        entry["duration_sec"] = dur_stop
                        entry["pid"] = 0
                        if old_pid > 0:
                            entry["last_pid"] = old_pid
                    self._save_locked()
            self._notify_listeners(clean_proj, eff_dir_prev)
            return {
                "ok": True,
                "action": clean_action,
                "runtime": "host",
                "project_name": clean_proj,
                "name": clean_name,
                "alive": False,
                "status": "deleted" if clean_action == "delete" else "stopped",
                "pid": old_pid,
                "duration_sec": dur_stop,
                "stopped_at": now_iso,
                "crash_unseen": False,
                "killed": killed,
            }

        # Action is 'start' or 'restart'
        if proj_res is None:
            raise ValueError("Не указана директория проекта (project_dir).")
        scope_hash = _compute_scope_hash(proj_res)
        key = self._make_key(clean_proj, clean_name, proj_res)
        is_restart = clean_action == "restart"
        eff_replace = bool(replace or is_restart)

        with self._lock:
            self._ensure_loaded_locked()
            _, existing = self._resolve_entry_locked(clean_proj, clean_name, project_dir=proj_res)
            saved_cmd = str(existing.get("command") or "").strip() if existing else ""
            saved_wd = str(existing.get("workdir") or ".").strip() if existing else "."
            saved_port = existing.get("port") if existing else None
            saved_gpu = bool(existing.get("allow_gpu", False)) if existing else False

        eff_cmd = str(command or "").strip() or (saved_cmd if is_restart else "")
        if not eff_cmd:
            raise ValueError(
                "Для запуска сервиса укажите непустой параметр command (например: 'python -m http.server 8095')."
            )
        if "\x00" in eff_cmd or "\n" in eff_cmd or "\r" in eff_cmd:
            raise ValueError("Команда сервиса должна быть однострочной без управляющих переводов строки.")

        eff_wd_raw = workdir if (workdir and workdir != ".") else (saved_wd if is_restart else workdir)
        resolved_wd = self._resolve_validated_workdir(eff_wd_raw, proj_res, extra_dirs=extra_dirs)
        eff_port = self._validate_port(port if port is not None else (saved_port if is_restart else None))
        eff_req_gpu = bool(allow_gpu or (saved_gpu if is_restart and not command else False))
        norm_cmd = _normalize_service_command(eff_cmd)

        # Anti-duplicate & active liveness check strictly within THIS `project_dir` scope
        pids_to_stop: list[tuple[str, int, int]] = []
        with self._lock:
            self._ensure_loaded_locked()
            for s_key, s_entry in list(self._services.items()):
                if not self._entry_matches_project_locked(
                    s_entry, clean_proj.lower(), scope_hash, proj_res
                ):
                    continue
                s_view, _ = self._refresh_entry_liveness_locked(s_entry, key=s_key)
                if not s_view["alive"]:
                    continue
                same_name = s_key == key
                same_cmd = _normalize_service_command(str(s_entry.get("command") or "")) == norm_cmd
                if same_name and not eff_replace:
                    log_file = Path(s_view["log_path"]) if s_view.get("log_path") else None
                    s_view["log_tail"] = _read_log_tail(log_file, tail_lines=tail_lines) if log_file else ""
                    return {
                        "ok": True,
                        "action": clean_action,
                        "already_running": True,
                        **s_view,
                        "hint": (
                            f"Сервис '{clean_name}' уже запущен (живой PID {s_view['pid']}). "
                            "Дубликат не создавался (защита от 409 Conflict). "
                            "Используйте action='restart' или replace=true для перезапуска."
                        ),
                    }
                if not same_name and same_cmd and not eff_replace:
                    return {
                        "ok": False,
                        "action": clean_action,
                        "runtime": "host",
                        "already_running": True,
                        "duplicate_of": s_view["name"],
                        "pid": s_view["pid"],
                        "status": "running",
                        "project_name": clean_proj,
                        "name": clean_name,
                        "error": (
                            f"Такая же команда уже запущена в этом проекте как сервис "
                            f"'{s_view['name']}' (живой PID {s_view['pid']}). "
                            f"Используйте action='restart' с name='{s_view['name']}' или передайте replace=true."
                        ),
                    }
                if same_name or (same_cmd and eff_replace):
                    pids_to_stop.append(
                        (
                            s_key,
                            int(s_entry.get("pid") or 0),
                            int(s_entry.get("create_time_ft") or 0),
                        )
                    )

        for stop_key, stop_pid, stop_ft in pids_to_stop:
            _kill_verified_process_tree(stop_pid, stop_ft)
            with self._lock:
                self._ensure_loaded_locked()
                self._reap_proc_handle_locked(stop_key)
                self._clear_pending_transitions_for_key_locked(stop_key)
                st_entry = self._services.get(stop_key)
                if st_entry:
                    st_entry["status"] = "stopped"
                    st_entry["crash_unseen"] = False
                    st_entry["pid"] = 0
                    st_entry["stopped_at"] = datetime.now(timezone.utc).isoformat()

        # If we just stopped a process holding `eff_port`, give OS TCP stack up to ~1s to release the socket
        if eff_port is not None and pids_to_stop:
            rel_deadline = time.monotonic() + 1.0
            while time.monotonic() < rel_deadline:
                if not _check_tcp_port_listening(eff_port, timeout=0.08):
                    break
                time.sleep(0.08)

        # Pre-flight port conflict check before spawning
        if eff_port is not None and _check_tcp_port_listening(eff_port, timeout=0.12):
            owner_pid = _find_listening_pid_on_port(eff_port)
            pid_note = f"PID {owner_pid}" if owner_pid else "внешним процессом"
            return {
                "ok": False,
                "action": clean_action,
                "runtime": "host",
                "project_name": clean_proj,
                "name": clean_name,
                "port": eff_port,
                "port_conflict": True,
                "conflicting_pid": owner_pid,
                "status": "port_conflict",
                "alive": False,
                "error": (
                    f"Порт {eff_port} уже занят ({pid_note}). "
                    "Освободите порт или выберите другой свободный порт перед запуском сервиса."
                ),
            }

        log_dir = self._root_dir() / f"{_safe_slug(clean_proj, 'proj')}_{scope_hash}"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{_safe_slug(clean_name, 'app')}.log"
        _rotate_log_if_needed(log_path)

        env, eff_allow_gpu = self._build_service_env(
            project_dir=proj_res,
            workdir=resolved_wd,
            allow_gpu=eff_req_gpu,
            user=user,
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        started_by = str((user or {}).get("username") or (user or {}).get("id") or "admin")
        started_by_id = str((user or {}).get("id") or "admin")
        rel_wd_banner = _relativize_workdir_for_guest(str(resolved_wd), proj_res)

        banner = (
            f"\n=== [{now_iso}] START {clean_proj}/{clean_name} | cmd: {eff_cmd} | "
            f"cwd: {rel_wd_banner} | gpu: {'on' if eff_allow_gpu else 'off'} ===\n"
        )
        popen_kwargs: dict[str, Any] = dict(config.subprocess_hidden_kwargs())
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True

        log_fh = log_path.open("ab")
        try:
            log_fh.write(banner.encode("utf-8", errors="replace"))
            log_fh.flush()
            proc = subprocess.Popen(
                eff_cmd,
                shell=True,
                cwd=str(resolved_wd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                close_fds=True,
                **popen_kwargs,
            )
        finally:
            try:
                log_fh.close()
            except OSError:
                pass

        new_pid = int(proc.pid)
        alive, create_ft, exit_code = get_process_fingerprint(new_pid)

        # Startup probe (0 or 50..10000 ms, default 1500 ms)
        raw_wait = 1500 if startup_wait_ms is None else int(startup_wait_ms)
        wait_ms = 0 if raw_wait <= 0 else max(50, min(raw_wait, 10000))
        probe_deadline = time.monotonic() + (wait_ms / 1000.0)
        port_ready = False

        while wait_ms > 0 and time.monotonic() < probe_deadline:
            alive, cur_ft, exit_code = get_process_fingerprint(new_pid)
            if cur_ft > 0 and create_ft <= 0:
                create_ft = cur_ft
            if not alive:
                break
            if eff_port is not None and _check_tcp_port_listening(eff_port, timeout=0.10):
                port_ready = True
                time.sleep(0.08)
                alive, _, exit_code = get_process_fingerprint(new_pid)
                break
            time.sleep(0.08)

        if alive and eff_port is not None and not port_ready:
            port_ready = _check_tcp_port_listening(eff_port, timeout=0.10)

        if not alive and exit_code is None:
            try:
                polled_rc = proc.poll()
                if polled_rc is not None:
                    exit_code = int(polled_rc)
            except Exception:
                pass

        finished_at_iso = ""
        stopped_at_iso = ""
        crash_unseen = False
        duration_sec: Optional[float]
        ram_mb: Optional[float] = None
        immediate_transition: Optional[str] = None

        if alive:
            status_str = "running"
            is_ok = True
            exit_code = None
            duration_sec = _compute_duration_sec(now_iso)
            ram_mb = get_process_working_set_mb(new_pid)
        else:
            finished_at_iso = datetime.now(timezone.utc).isoformat()
            duration_sec = _compute_duration_sec(now_iso, finished_at_iso)
            if exit_code == 0 and eff_port is None:
                status_str = "completed"
                is_ok = True
                crash_unseen = False
                immediate_transition = "completed"
            else:
                status_str = "crashed"
                is_ok = False
                crash_unseen = True
                immediate_transition = "crashed"

        record: dict[str, Any] = {
            "scope_hash": scope_hash,
            "project_name": clean_proj,
            "project_dir": str(proj_res),
            "name": clean_name,
            "command": eff_cmd,
            "norm_command": norm_cmd,
            "workdir": str(resolved_wd),
            "pid": new_pid if alive else 0,
            "last_pid": new_pid,
            "create_time_ft": create_ft,
            "port": eff_port,
            "allow_gpu": eff_allow_gpu,
            "started_at": now_iso,
            "finished_at": finished_at_iso,
            "stopped_at": stopped_at_iso,
            "duration_sec": duration_sec,
            "crash_unseen": crash_unseen,
            "started_by": started_by,
            "started_by_id": started_by_id,
            "log_path": str(log_path),
            "status": status_str,
            "exit_code": exit_code,
        }

        with self._lock:
            self._ensure_loaded_locked()
            self._reap_proc_handle_locked(key)
            self._clear_pending_transitions_for_key_locked(key)
            if alive:
                self._live_procs[key] = proc
            self._services[key] = record
            view_snapshot, _ = self._refresh_entry_liveness_locked(record, key=key if alive else "")
            if immediate_transition in ("completed", "crashed"):
                self._queue_transition_locked(key, record, view_snapshot, immediate_transition)
            self._save_locked()

        self._notify_listeners(clean_proj, proj_res)
        tail_txt = _read_log_tail(log_path, tail_lines=tail_lines)

        out: dict[str, Any] = {
            "ok": is_ok,
            "action": clean_action,
            "runtime": "host",
            "already_running": False,
            "project_name": clean_proj,
            "project_dir": str(proj_res),
            "name": clean_name,
            "command": eff_cmd,
            "workdir": str(resolved_wd),
            "pid": new_pid,
            "alive": alive,
            "status": status_str,
            "exit_code": exit_code,
            "ram_mb": ram_mb,
            "duration_sec": duration_sec,
            "port": eff_port,
            "port_listening": port_ready,
            "allow_gpu": eff_allow_gpu,
            "started_at": now_iso,
            "finished_at": finished_at_iso,
            "stopped_at": stopped_at_iso,
            "crash_unseen": crash_unseen,
            "started_by": started_by,
            "started_by_id": started_by_id,
            "log_path": str(log_path),
            "log_tail": tail_txt,
        }
        if not is_ok:
            out["error"] = (
                f"Процесс завершился сразу после старта (exit_code={exit_code}). "
                "Проверьте вывод лога ниже."
            )
        return out


HOST_SERVICE_MANAGER = HostServiceManager()


async def _host_services_watchdog_loop(interval_sec: float = HOST_WATCHDOG_INTERVAL_SECONDS) -> None:
    sleep_sec = max(1.0, float(interval_sec or HOST_WATCHDOG_INTERVAL_SECONDS))
    while True:
        await asyncio.sleep(sleep_sec)
        try:
            transitions = await asyncio.to_thread(
                HOST_SERVICE_MANAGER.poll_running_services_transitions
            )
            if not transitions:
                continue
            for ev in transitions:
                pname = str(ev.get("project_name") or "").strip()
                pdir = ev.get("project_dir") or None
                for t_cb in list(HOST_SERVICE_MANAGER._transition_listeners):
                    try:
                        await t_cb(ev)
                    except Exception:
                        pass
                if pname:
                    HOST_SERVICE_MANAGER._notify_listeners(pname, pdir)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def start_host_services_watchdog(interval_sec: float = HOST_WATCHDOG_INTERVAL_SECONDS) -> None:
    global _HOST_WATCHDOG_TASK
    if _HOST_WATCHDOG_TASK is not None and not _HOST_WATCHDOG_TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
        _HOST_WATCHDOG_TASK = loop.create_task(
            _host_services_watchdog_loop(interval_sec),
            name="agy_host_services_watchdog",
        )
    except RuntimeError:
        pass


async def stop_host_services_watchdog() -> None:
    global _HOST_WATCHDOG_TASK
    task = _HOST_WATCHDOG_TASK
    _HOST_WATCHDOG_TASK = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# 4. Per-User HMAC Token, CLI Skill Sync & MCP Streamable HTTP (`hub_host`)
# ---------------------------------------------------------------------------


def is_user_host_eligible(user: Optional[dict[str, Any]]) -> bool:
    if not user or not bool(user.get("is_active", True)):
        return False
    if str(user.get("role") or "").strip().lower() == "admin":
        return True
    return str(user.get("max_access_mode") or "").strip().lower() == "host_full"


def build_host_mcp_token(user_id: str, session_version: int) -> str:
    uid = (user_id or "").strip()
    sv = max(1, int(session_version or 1))
    secret = database.get_or_create_hub_secret().encode("utf-8")
    msg = f"host-mcp:v1:{uid}:{sv}".encode("utf-8")
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return f"hmcp_{uid}_{sv}_{sig[:40]}"


def verify_host_mcp_token(token: str) -> Optional[tuple[str, int]]:
    raw = (token or "").strip()
    if not raw.startswith("hmcp_"):
        return None
    body = raw[len("hmcp_") :]
    parts = body.rsplit("_", 2)
    if len(parts) != 3:
        return None
    uid, sv_str, _sig = parts
    if not uid or not sv_str.isdigit():
        return None
    sv = int(sv_str)
    expected = build_host_mcp_token(uid, sv)
    if not hmac.compare_digest(raw, expected):
        return None
    return (uid, sv)


def build_local_host_mcp_url(user_id: str, session_version: int) -> str:
    tok = build_host_mcp_token(user_id, session_version)
    return f"http://127.0.0.1:{config.HUB_PORT}/mcp/host/{tok}"


def _sync_host_mcp_config_file(cfg_file: Path, *, server_url: Optional[str]) -> None:
    existing_root: dict[str, Any] = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_root = loaded
        except Exception:
            existing_root = {}

    servers = existing_root.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    else:
        servers = dict(servers)

    if server_url:
        servers[MCP_HOST_SERVER_NAME] = {"url": server_url}
        existing_root["mcpServers"] = servers
        _atomic_write_text(cfg_file, json.dumps(existing_root, ensure_ascii=False, indent=2) + "\n")
    else:
        servers.pop(MCP_HOST_SERVER_NAME, None)
        other_root_keys = {k for k in existing_root.keys() if k != "mcpServers"}
        if not servers and not other_root_keys:
            cfg_file.unlink(missing_ok=True)
        else:
            existing_root["mcpServers"] = servers
            _atomic_write_text(cfg_file, json.dumps(existing_root, ensure_ascii=False, indent=2) + "\n")


def _build_host_services_skill_markdown(*, is_admin: bool = False) -> str:
    gpu_line = (
        "- По умолчанию сервисы запускаются с `0% GPU` (`CUDA_VISIBLE_DEVICES=\"\"`, `ORT_DISABLE_ALL_GPU=\"1\"`), "
        "чтобы не мешать фоновым GPU-задачам хоста. Передавай `allow_gpu=true` только если пользователь явно попросил "
        "запустить сервис на видеокарте.\n"
        if is_admin
        else "- Все сервисы запускаются в режиме `0% GPU` (`CUDA_VISIBLE_DEVICES=\"\"`).\n"
    )
    return (
        "---\n"
        "name: host-services\n"
        "description: >-\n"
        "  Управление фоновыми скриптами, процессами, парсерами, ботами, API и веб-серверами на Windows-хосте\n"
        "  (MCP-сервер hub_host, инструмент host_service_ctl): безопасный запуск вне дерева процесса CLI, живой\n"
        "  мониторинг RAM/статуса в UI Hub, защита от дубликатов (409 Conflict), проверка портов, чтение логов,\n"
        "  перезапуск и чистая остановка. Активируй ВСЕГДА, когда в проектах с прямым терминалом ПК нужно запустить,\n"
        "  проверить, перезапустить или остановить рабочий скрипт дольше нескольких секунд или фоновый процесс\n"
        "  (парсер, сборщик данных, воркер, бота, uvicorn/FastAPI, Flask, Node/Vite, http.server).\n"
        "---\n\n"
        "# Фоновые скрипты и сервисы Windows-хоста (`hub_host` → `host_service_ctl`)\n\n"
        "В режиме прямого терминала ПК (`Trusted Host Mode`) чётко разделяй два типа запуска:\n"
        "- **Короткие проверочные команды до 5 секунд** (`pytest`, `py_compile`, `node --check`, `git status`, `pip`, `npm`) — "
        "выполняй напрямую через `run_command`.\n"
        "- **Любые рабочие скрипты (`python`, `node`), парсеры, сборщики данных, воркеры, боты, API и веб-серверы**, "
        "которые работают дольше нескольких секунд или в фоне — запускай **через `host_service_ctl(action=\"start\", name=\"...\", command=\"...\")`**, "
        "чтобы не блокировать ход чата (и не потерять дочерний процесс при закрытии Job Object `agy.exe`) и сразу дать пользователю "
        "живую карточку мониторинга, логи и кнопку «Стоп» в UI Hub.\n\n"
        "## Правила работы с `host_service_ctl`\n"
        "1. **Запуск (`action=\"start\"`):** передавай осмысленное `name` (например `\"parser\"`, `\"bot\"`, `\"api\"`, `\"web\"`), "
        "`command` (например `\".venv\\\\Scripts\\\\python.exe parser.py\"` или `\"python -m uvicorn app.main:app --port 8090\"`) "
        "и `port` (если сервис слушает TCP-порт).\n"
        "2. **Защита от дубликатов (`409 Conflict`):** если процесс с таким `name` или такой же `command` уже запущен, "
        "`start` **не создаёт второй экземпляр**, а возвращает живой `PID`. Чтобы применить изменения в коде, вызывай "
        "`host_service_ctl(action=\"restart\", name=\"...\")`.\n"
        "3. **Диагностика (`status` и `logs`):** проверяй состояние (`running` / `completed` / `crashed` / `stopped`) через "
        "`action=\"status\"` и свежие строки лога через `action=\"logs\"` (`tail_lines=80`). После старта HTTP-сервера можешь "
        "сразу выполнить `curl http://127.0.0.1:<port>` в `run_command` для E2E-проверки.\n"
        "4. **Остановка (`stop`):** если сервис поднимался только на время теста — останови его через "
        "`host_service_ctl(action=\"stop\", name=\"...\")`. Если пользователь попросил «запусти скрипт/бота/сервер» — "
        "оставь его работать: `HostServiceManager` сохранит процесс живым после завершения хода и уведомит пользователя о завершении!\n"
        f"{gpu_line}"
    )


def sync_user_host_cli_files(
    user: Optional[dict[str, Any]],
    *,
    force_disable_user_id: Optional[str] = None,
) -> bool:
    """Idempotently sync or remove `hub_host` in `mcp_config.json` and `host-services/SKILL.md`."""
    uid = str((user or {}).get("id") or force_disable_user_id or "").strip()
    if not uid:
        return False

    home_dir = config.ensure_user_cli_home(uid)
    config_dir = home_dir / ".gemini" / "config"
    mcp_file = config_dir / "mcp_config.json"
    skill_dir = config_dir / "skills" / "host-services"
    skill_file = skill_dir / "SKILL.md"
    agents_skill_dir = home_dir / ".agents" / "skills" / "host-services"
    agents_skill_file = agents_skill_dir / "SKILL.md"
    claude_skill_dir = home_dir / ".claude" / "skills" / "host-services"
    claude_skill_file = claude_skill_dir / "SKILL.md"
    claude_accounts_dir = home_dir / ".claude" / "accounts"

    should_enable = bool(
        user is not None
        and is_user_host_eligible(user)
        and not force_disable_user_id
    )

    if should_enable and user is not None:
        sv = max(1, int(user.get("session_version") or 1))
        local_url = build_local_host_mcp_url(uid, sv)
        _sync_host_mcp_config_file(mcp_file, server_url=local_url)
        is_admin = str(user.get("role") or "").strip().lower() == "admin"
        skill_md = _build_host_services_skill_markdown(is_admin=is_admin)
        _atomic_write_text(skill_file, skill_md)
        _atomic_write_text(agents_skill_file, skill_md)
        _atomic_write_text(claude_skill_file, skill_md)
        if claude_accounts_dir.is_dir():
            for child in claude_accounts_dir.iterdir():
                if child.is_dir() and not child.name.startswith("_pending_"):
                    try:
                        _atomic_write_text(child / "skills" / "host-services" / "SKILL.md", skill_md)
                    except OSError:
                        pass
        return True

    _sync_host_mcp_config_file(mcp_file, server_url=None)
    skill_file.unlink(missing_ok=True)
    agents_skill_file.unlink(missing_ok=True)
    claude_skill_file.unlink(missing_ok=True)
    cleanup_dirs: list[Path] = []
    if claude_accounts_dir.is_dir():
        for child in claude_accounts_dir.iterdir():
            if child.is_dir():
                acc_sk = child / "skills" / "host-services"
                (acc_sk / "SKILL.md").unlink(missing_ok=True)
                cleanup_dirs.append(acc_sk)
    for sdir in (skill_dir, agents_skill_dir, claude_skill_dir, *cleanup_dirs):
        try:
            if sdir.exists() and not any(sdir.iterdir()):
                sdir.rmdir()
        except OSError:
            pass
    return False


async def sync_user_host_cli_files_by_id(user_id: str) -> bool:
    uid = (user_id or "").strip()
    if not uid:
        return False
    user = await database.get_user_by_id(uid)
    return await asyncio.to_thread(sync_user_host_cli_files, user)


async def sync_all_users_host_cli_files() -> None:
    users = await database.list_users()
    for usr in users:
        try:
            await asyncio.to_thread(sync_user_host_cli_files, usr)
        except Exception:
            pass


@dataclass
class ResolvedHostRun:
    user: dict[str, Any]
    chat_id: str
    project_name: str
    project_dir: Path
    extra_dirs: list[Path] = field(default_factory=list)


def resolve_active_host_run(
    user: dict[str, Any],
    *,
    requested_project: str = "",
) -> ResolvedHostRun:
    """Verify caller has an active `full_access` Windows Host project turn in `ACTIVE_RUNS`."""
    from .agy_engine import ACTIVE_RUNS

    if not is_user_host_eligible(user):
        raise PermissionError("У вашей учётной записи нет прав на запуск фоновых сервисов ПК (Host).")

    uid = str(user.get("id") or "").strip()
    candidates = [
        r
        for r in ACTIVE_RUNS.values()
        if not r.cancelled
        and r.user_id == uid
        and getattr(r, "host_allowed", False) is True
        and r.kind == "project"
        and r.access_mode == "full_access"
        and r.project_dir is not None
    ]

    if not candidates:
        raise PermissionError(
            "Управление фоновыми сервисами хоста (hub_host) доступно только во время активного хода "
            "в проектном чате с прямым терминалом ПК в режиме «Терминал» (full_access)."
        )

    clean_req_proj = str(requested_project or "").strip()
    if len(candidates) == 1:
        chosen = candidates[0]
        if clean_req_proj and clean_req_proj.lower() != str(chosen.project_name or "").strip().lower():
            raise PermissionError(
                f"Активный ход идёт в проекте «{chosen.project_name}», а запрошен проект «{clean_req_proj}»."
            )
    else:
        if clean_req_proj:
            matched = [
                r
                for r in candidates
                if str(r.project_name or "").strip().lower() == clean_req_proj.lower()
            ]
            if not matched:
                active_names = sorted({str(r.project_name or "") for r in candidates if r.project_name})
                raise PermissionError(
                    f"Среди активных ходов не найден проект «{clean_req_proj}». "
                    f"Активные проекты: {', '.join(active_names)}."
                )
            matched.sort(
                key=lambda r: float(getattr(r, "started_ts", getattr(r, "started_at", 0.0))),
                reverse=True,
            )
            chosen = matched[0]
        else:
            unique_dirs = {r.project_dir.resolve() for r in candidates if r.project_dir is not None}
            if len(unique_dirs) == 1:
                candidates.sort(
                    key=lambda r: float(getattr(r, "started_ts", getattr(r, "started_at", 0.0))),
                    reverse=True,
                )
                chosen = candidates[0]
            else:
                active_names = sorted({str(r.project_name or "") for r in candidates if r.project_name})
                hint = active_names[0] if active_names else "имя_проекта"
                raise PermissionError(
                    "У вас одновременно запущено несколько параллельных ходов в разных проектах "
                    f"({', '.join(active_names)}). Передайте аргумент project=\"{hint}\"."
                )

    return ResolvedHostRun(
        user=user,
        chat_id=chosen.chat_id,
        project_name=chosen.project_name,
        project_dir=chosen.project_dir.resolve(),
        extra_dirs=list(getattr(chosen, "extra_dirs", None) or []),
    )


def _format_host_service_report(res: dict[str, Any]) -> str:
    action = str(res.get("action") or "status")
    proj = str(res.get("project_name") or "")
    name = str(res.get("name") or "app")
    status = str(res.get("status") or "unknown")
    pid = int(res.get("pid") or 0)
    port = res.get("port")
    port_part = f" · port={port}({'open' if res.get('port_listening') else 'wait'})" if port else ""
    header = f"[hub_host · project={proj} · service={name} · action={action} · status={status} · pid={pid}{port_part}]"
    lines = [header]
    if res.get("hint"):
        lines.append(f"💡 {res['hint']}")
    if res.get("error"):
        lines.append(f"⚠️ {res['error']}")
    if isinstance(res.get("services"), list):
        svcs = res["services"]
        if not svcs:
            lines.append("Зарегистрированных фоновых сервисов в проекте пока нет.")
        else:
            for s in svcs:
                st_icon = "🟢" if s.get("alive") else "🔴"
                p_lbl = f" :{s['port']}" if s.get("port") else ""
                lines.append(
                    f"  {st_icon} {s['name']} (pid={s.get('pid') or 0}{p_lbl}) — {s.get('command') or ''}"
                )
    tail = str(res.get("log_tail") or "").strip()
    if tail:
        lines.append(f"--- log tail ({name}.log) ---\n{tail}")
    return "\n".join(lines)


def _mcp_host_tools_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "host_service_ctl",
            "description": (
                "Безопасное управление рабочими скриптами и фоновыми процессами на Windows-хосте "
                "(парсеры, сборщики данных, воркеры, боты, веб-серверы, API) вне Job Object CLI. "
                "Процесс переживает завершение хода, отображается в живой карточке UI Hub, "
                "защищён от дублирования (anti-409 Conflict), логи пишутся в изолированный лог-файл."
            ),
            "annotations": {
                "title": "Manage Windows Host Background Service",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "stop", "restart", "status", "logs", "delete"],
                        "description": "Действие: 'start', 'stop', 'restart', 'status', 'logs' или 'delete'.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Короткое имя сервиса (например 'bot', 'web', 'api'). По умолчанию 'app'.",
                        "default": "app",
                    },
                    "command": {
                        "type": "string",
                        "description": "Команда запуска (обязательна для первого 'start', при 'restart' можно опустить).",
                        "default": "",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Подпапка внутри проекта или '.' (по умолчанию '.').",
                        "default": ".",
                    },
                    "port": {
                        "type": "integer",
                        "description": "Необязательный TCP-порт сервиса (проверяется на конфликт до старта и готовность после старта).",
                    },
                    "allow_gpu": {
                        "type": "boolean",
                        "description": "Разрешить доступ к GPU (по умолчанию false -> 0% GPU). Только для admin.",
                        "default": False,
                    },
                    "replace": {
                        "type": "boolean",
                        "description": "Если true — при 'start' перезапустить уже работающий экземпляр вместо возврата already_running.",
                        "default": False,
                    },
                    "startup_wait_ms": {
                        "type": "integer",
                        "description": "Ожидание старта и проверки порта в миллисекундах (200..10000, по умолчанию 1500).",
                        "default": 1500,
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "Количество последних строк лога (5..400, по умолчанию 40).",
                        "default": 40,
                    },
                    "project": {
                        "type": "string",
                        "description": "Необязательное имя проекта (нужно только при параллельных ходах в разных проектах).",
                        "default": "",
                    },
                },
                "required": ["action"],
            },
        }
    ]


def _host_tool_error_result(req_id: Any, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "content": [{"type": "text", "text": f"Ошибка hub_host: {message}"}],
            "isError": True,
        },
    }


async def _authenticate_host_user_from_token(mcp_token: str) -> Optional[dict[str, Any]]:
    verified = verify_host_mcp_token(mcp_token)
    if not verified:
        return None
    uid, token_sv = verified
    user = await database.get_user_by_id(uid)
    if not user or not is_user_host_eligible(user):
        return None
    live_sv = max(1, int(user.get("session_version") or 1))
    if live_sv != token_sv:
        return None
    return user


async def _dispatch_host_jsonrpc_message(
    msg: dict[str, Any],
    user: dict[str, Any],
) -> Optional[dict[str, Any]]:
    method = str(msg.get("method") or "").strip()
    has_id = "id" in msg
    req_id = msg.get("id")

    if not has_id or method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        client_proto = str(params.get("protocolVersion") or "").strip()
        negotiated = client_proto if client_proto in _SUPPORTED_MCP_PROTOCOLS else MCP_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": MCP_HOST_SERVER_NAME,
                    "version": "1.0.0",
                },
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _mcp_host_tools_definitions()},
        }

    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        tool_name = str(params.get("name") or "").strip()
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}

        if tool_name != "host_service_ctl":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Неизвестный инструмент MCP: {tool_name}"},
            }

        try:
            run_ctx = resolve_active_host_run(
                user,
                requested_project=str(args.get("project") or ""),
            )
        except PermissionError as perm_err:
            return _host_tool_error_result(req_id, str(perm_err))

        raw_port = args.get("port")
        parsed_port: Optional[int] = int(raw_port) if raw_port not in (None, "", 0) else None
        try:
            res = await asyncio.to_thread(
                HOST_SERVICE_MANAGER.control_service,
                project_name=run_ctx.project_name,
                project_dir=run_ctx.project_dir,
                action=str(args.get("action") or ""),
                name=str(args.get("name") if args.get("name") is not None else "app"),
                command=str(args.get("command") or ""),
                workdir=str(args.get("workdir") if args.get("workdir") is not None else "."),
                port=parsed_port,
                allow_gpu=bool(args.get("allow_gpu", False)),
                replace=bool(args.get("replace", False)),
                startup_wait_ms=int(args.get("startup_wait_ms") if args.get("startup_wait_ms") is not None else 1500),
                tail_lines=int(args.get("tail_lines") if args.get("tail_lines") is not None else 40),
                user=user,
                extra_dirs=run_ctx.extra_dirs,
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": _format_host_service_report(res)}],
                    "isError": not bool(res.get("ok")),
                },
            }
        except (ValueError, RuntimeError, PermissionError) as err:
            return _host_tool_error_result(req_id, str(err))
        except Exception as exc:
            return _host_tool_error_result(req_id, f"Внутренняя ошибка сервиса хоста: {exc}")

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Метод JSON-RPC не поддерживается: {method}"},
    }


async def handle_mcp_host_http(request: Request, mcp_token: str) -> Response:
    """FastAPI Streamable HTTP MCP handler for `POST`, `GET`, `DELETE` `/mcp/host/{mcp_token}`."""
    if not is_loopback_mcp_request(request):
        return JSONResponse(
            status_code=403,
            content={"error": "Доступ к MCP hub_host разрешён строго с loopback хоста (127.0.0.1 / ::1)."},
        )

    user = await _authenticate_host_user_from_token(mcp_token)
    if not user:
        return JSONResponse(
            status_code=403,
            content={"error": "MCP-токен hub_host недействителен, отозван или доступ отключён."},
        )

    method = request.method.upper()
    if method == "GET":
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})
    if method == "DELETE":
        return Response(status_code=200)
    if method != "POST":
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    try:
        raw_body = await request.body()
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else None
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Некорректный JSON в запросе MCP"},
            },
        )

    if isinstance(payload, list):
        if not payload:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Пустой batch-массив JSON-RPC"},
                },
            )
        responses: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            resp_obj = await _dispatch_host_jsonrpc_message(item, user)
            if resp_obj is not None:
                responses.append(resp_obj)
        if not responses:
            return Response(status_code=202)
        return JSONResponse(status_code=200, content=responses)

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Ожидался JSON-RPC объект"},
            },
        )

    resp_obj = await _dispatch_host_jsonrpc_message(payload, user)
    if resp_obj is None:
        return Response(status_code=202)
    return JSONResponse(status_code=200, content=resp_obj)
