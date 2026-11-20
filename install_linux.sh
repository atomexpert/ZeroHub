#!/usr/bin/env bash
set -euo pipefail
umask 077
export CUDA_VISIBLE_DEVICES="" NVIDIA_VISIBLE_DEVICES=none ORT_DISABLE_ALL_GPU=1 PYTHONIOENCODING=utf-8

use_systemd=false
install_dir="$HOME/ZeroHub"
while (($#)); do
    case "$1" in
        --systemd) use_systemd=true; shift ;;
        --dir) install_dir="${2:?Missing installation directory}"; shift 2 ;;
        *) echo "Usage: $0 [--systemd] [--dir PATH]" >&2; exit 2 ;;
    esac
done
if [[ "$(id -u)" == 0 ]]; then
    echo "Run this installer as a regular user with sudo access." >&2
    exit 1
fi
if ! command -v python3 >/dev/null || ! python3 -c 'import venv, ensurepip' 2>/dev/null ||
    ! command -v git >/dev/null || ! command -v curl >/dev/null; then
    if command -v apt-get >/dev/null; then
        sudo apt-get update
        sudo apt-get install -y python3 python3-venv git curl
    elif command -v dnf >/dev/null; then
        sudo dnf install -y python3 git curl
    elif command -v pacman >/dev/null; then
        sudo pacman -S --needed python git curl
    else
        echo "Install Python 3.11+ with venv, Git and curl, then rerun." >&2
        exit 1
    fi
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
    echo "Python 3.11 or newer is required." >&2; exit 1;
}
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$script_dir/run_server.py" && -f "$script_dir/requirements.txt" ]]; then
    install_dir="$script_dir"
elif [[ ! -d "$install_dir/.git" ]]; then
    git clone https://github.com/atomexpert/ZeroHub.git "$install_dir"
fi
cd -- "$install_dir"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'from app import config'

if "$use_systemd"; then
    command -v systemctl >/dev/null || { echo "systemd is unavailable." >&2; exit 1; }
    unit_dir=$(.venv/bin/python -c 'import os; print(os.getcwd().replace("\\", "\\\\").replace("\"", "\\\"").replace("%", "%%").replace("\n", "\\n"))')
    sudo tee /etc/systemd/system/zerohub.service >/dev/null <<UNIT
[Unit]
Description=ZeroHub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory="$unit_dir"
ExecStart=/usr/bin/bash "$unit_dir/start_hub.sh"
Environment=ZEROHUB_SYSTEMD=1
Restart=always
RestartSec=2
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now zerohub.service
else
    bash ./start_hub.sh --daemon
fi

.venv/bin/python -c 'from app import config; print(f"ZeroHub: http://127.0.0.1:{config.HUB_PORT}")'
if [[ -r data/FIRST_RUN_CREDENTIALS.txt ]]; then
    cat data/FIRST_RUN_CREDENTIALS.txt
fi
