#!/usr/bin/env bash
set -euo pipefail
cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")"
export CUDA_VISIBLE_DEVICES="" NVIDIA_VISIBLE_DEVICES=none ORT_DISABLE_ALL_GPU=1 PYTHONIOENCODING=utf-8
umask 077

case "${1:-}" in
    ""|--daemon) ;;
    *) echo "Usage: $0 [--daemon]" >&2; exit 2 ;;
esac
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import uvicorn, fastapi, aiosqlite' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -r requirements.txt
fi

if [[ "${1:-}" != --daemon ]]; then
    exec .venv/bin/python "$PWD/run_server.py"
fi

mkdir -p data
exec 9>data/hub-launch.lock
flock 9
if [[ -f data/hub.pid ]]; then
    read -r pid < data/hub.pid || true
    if [[ "${pid:-}" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/cmdline" ]] &&
        tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fxq -- "$PWD/run_server.py"; then
        echo "ZeroHub is already running (PID $pid)."
        exit 0
    fi
fi
nohup .venv/bin/python "$PWD/run_server.py" </dev/null >>data/hub_server.log 2>&1 9>&- &
pid=$!
printf '%s\n' "$pid" > data/hub.pid
echo "ZeroHub is starting (PID $pid). Logs: data/hub_server.log"
