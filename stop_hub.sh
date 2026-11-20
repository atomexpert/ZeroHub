#!/usr/bin/env bash
set -euo pipefail
cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")"

if command -v systemctl >/dev/null && systemctl is-active --quiet zerohub.service; then
    if [[ "$(id -u)" == 0 ]]; then
        exec systemctl stop zerohub.service
    fi
    exec sudo systemctl stop zerohub.service
fi
if [[ ! -f data/hub.pid ]]; then
    echo "No ZeroHub daemon PID file."
    exit 0
fi
exec 9>data/hub-launch.lock
flock 9
read -r pid < data/hub.pid || true
if [[ "${pid:-}" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/cmdline" ]] &&
    tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fxq -- "$PWD/run_server.py"; then
    kill -TERM "$pid"
    for ((attempt = 0; attempt < 100; attempt++)); do
        if ! kill -0 "$pid" 2>/dev/null || ! grep -qv '^State:[[:space:]]*Z' <(grep '^State:' "/proc/$pid/status" 2>/dev/null); then
            break
        fi
        sleep 0.1
    done
    if [[ "$attempt" == 100 ]]; then
        echo "ZeroHub does not yet appear to have stopped (PID $pid)." >&2
        exit 1
    fi
fi
rm -f -- data/hub.pid
