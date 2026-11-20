"""Process watcher for diagnosing flashing console windows on Windows 11.

Polls the process table every 150 ms and appends every NEW process to data/proc_watch.log:
    time(ms) | name | PID | parent chain (up to 4 levels, name+PID) | cmdline (<=300 chars)
Console hosts (conhost.exe, OpenConsole.exe, WindowsTerminal.exe, wt.exe) are tagged with
`!!! CONSOLE` and logged with their full parent chain.

Run manually WITHOUT a window:
    .venv\\Scripts\\pythonw.exe tools\\proc_watch.py
Stop it via Task Manager (pythonw.exe with tools\\proc_watch.py in the command line).
Not embedded into the Hub server on purpose.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = BASE_DIR / "data" / "proc_watch.log"
POLL_INTERVAL_S = 0.150
MAX_CMDLINE = 300
PARENT_DEPTH = 4
CONSOLE_NAMES = {"conhost.exe", "openconsole.exe", "windowsterminal.exe", "wt.exe"}

# pid -> (name, ppid, create_time) — kept so parent chains survive short-lived parents.
_known: dict[int, tuple[str, int, float]] = {}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _cmdline_str(cmd: list[str] | None) -> str:
    if not cmd:
        return ""
    s = " ".join(cmd).replace("\r", " ").replace("\n", " ")
    return s if len(s) <= MAX_CMDLINE else s[: MAX_CMDLINE - 1] + "…"


def _parent_chain(ppid: int, child_ctime: float, depth: int | None) -> str:
    parts: list[str] = []
    cur = ppid
    level = 0
    visited: set[int] = set()
    while cur and cur not in visited and (depth is None or level < depth):
        visited.add(cur)
        name = "?"
        next_ppid = 0
        info = _known.get(cur)
        live_ok = False
        try:
            p = psutil.Process(cur)
            # Guard against PID reuse: a real parent must be older than its child.
            if p.create_time() <= child_ctime + 0.5:
                name = p.name()
                next_ppid = p.ppid()
                child_ctime = p.create_time()
                live_ok = True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass
        if not live_ok and info is not None:
            name, next_ppid, child_ctime = info[0], info[1], info[2]
            name = f"{name}(exited)"
        parts.append(f"{name}#{cur}")
        cur = next_ppid
        level += 1
    return " <- ".join(parts) if parts else "-"


def _write(fh, line: str) -> None:
    fh.write(line + "\n")
    fh.flush()


def main() -> int:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    attrs = ["pid", "ppid", "name", "cmdline", "create_time"]
    with open(LOG_FILE, "a", encoding="utf-8", errors="backslashreplace") as fh:
        _write(fh, f"{_ts()} ===== proc_watch started (pid {psutil.Process().pid}, poll {int(POLL_INTERVAL_S * 1000)} ms) =====")

        # Baseline: remember everything that already exists (not logged as new).
        for p in psutil.process_iter(attrs):
            try:
                i = p.info
                _known[i["pid"]] = (i.get("name") or "?", i.get("ppid") or 0, i.get("create_time") or 0.0)
            except Exception:
                continue
        seen: set[tuple[int, float]] = {(pid, v[2]) for pid, v in _known.items()}

        while True:
            try:
                current: set[tuple[int, float]] = set()
                new_items: list[dict] = []
                for p in psutil.process_iter(attrs):
                    try:
                        i = p.info
                    except Exception:
                        continue
                    key = (i["pid"], i.get("create_time") or 0.0)
                    current.add(key)
                    if key not in seen:
                        new_items.append(i)
                        _known[i["pid"]] = (i.get("name") or "?", i.get("ppid") or 0, i.get("create_time") or 0.0)

                new_items.sort(key=lambda d: d.get("create_time") or 0.0)
                for i in new_items:
                    name = i.get("name") or "?"
                    pid = i["pid"]
                    ppid = i.get("ppid") or 0
                    ctime = i.get("create_time") or 0.0
                    is_console = name.lower() in CONSOLE_NAMES
                    chain = _parent_chain(ppid, ctime, None if is_console else PARENT_DEPTH)
                    created = datetime.fromtimestamp(ctime).strftime("%H:%M:%S.%f")[:-3] if ctime else "?"
                    tag = "!!! CONSOLE " if is_console else ""
                    _write(
                        fh,
                        f"{_ts()} {tag}{name} pid={pid} created={created} "
                        f"parents=[{chain}] cmd={_cmdline_str(i.get('cmdline'))}",
                    )

                seen = current
                # Drop exited PIDs from the name cache only after a while (keep chains readable).
                if len(_known) > 4096:
                    alive = {k[0] for k in current}
                    for dead in [pid for pid in _known if pid not in alive][:2048]:
                        _known.pop(dead, None)
            except Exception as exc:  # never die silently in the middle of a diagnosis
                _write(fh, f"{_ts()} [watcher error] {exc!r}")
            time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
