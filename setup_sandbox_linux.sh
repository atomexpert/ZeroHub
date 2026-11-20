#!/usr/bin/env bash
# Antigravity Hub — Установка и сборка Docker-песочницы на Linux (Ubuntu / Debian / VPS)
# Использование:
#   chmod +x setup_sandbox_linux.sh
#   ./setup_sandbox_linux.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="${HUB_DOCKER_IMAGE:-agy-hub-sandbox:latest}"
TARGET_USER="${SUDO_USER:-$(id -un)}"

echo "================================================================"
echo "  Antigravity Hub — Настройка Docker-песочницы для Linux"
echo "  Папка проекта: ${SCRIPT_DIR}"
echo "  Сервисный пользователь Hub: ${TARGET_USER}"
echo "================================================================"

run_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "[ОШИБКА] Требуется root или sudo для установки пакетов: $*" >&2
        exit 1
    fi
}

# 1. Install Docker Engine if missing
if ! command -v docker >/dev/null 2>&1; then
    echo "==> Установка Docker Engine..."
    if command -v apt-get >/dev/null 2>&1; then
        run_root apt-get update
        run_root apt-get install -y ca-certificates curl gnupg docker.io
    elif command -v dnf >/dev/null 2>&1; then
        run_root dnf install -y docker
    elif command -v pacman >/dev/null 2>&1; then
        run_root pacman -Sy --noconfirm docker
    else
        curl -fsSL https://get.docker.com | run_root sh
    fi
fi

# 2. Enable and start Docker daemon
if command -v systemctl >/dev/null 2>&1; then
    echo "==> Включение и запуск сервиса docker..."
    run_root systemctl enable --now docker
fi

# 3. Grant Hub user access to /var/run/docker.sock without sudo
if [ "${TARGET_USER}" != "root" ] && getent group docker >/dev/null 2>&1; then
    if ! id -nG "${TARGET_USER}" | tr ' ' '\n' | grep -qx "docker"; then
        echo "==> Добавление пользователя '${TARGET_USER}' в группу 'docker'..."
        run_root usermod -aG docker "${TARGET_USER}"
        echo " [!] После завершения скрипта перезапустите сервис Antigravity Hub (или перелогиньтесь), чтобы группа docker применилась."
    fi
fi

DOCKER_CMD=(docker)
if ! docker info >/dev/null 2>&1; then
    DOCKER_CMD=(sudo docker)
fi

SERVER_VER="$("${DOCKER_CMD[@]}" info --format '{{.ServerVersion}}')"
echo " [OK] Docker Engine ${SERVER_VER} активен"

# 4. Build sandbox image
echo "==> Сборка образа ${IMAGE_TAG}..."
cd "${SCRIPT_DIR}"
"${DOCKER_CMD[@]}" build -t "${IMAGE_TAG}" -f Dockerfile.sandbox .
echo " [OK] Образ ${IMAGE_TAG} собран"

# 5. Smoke-test with actual host non-root UID:GID (verifies Linux bind-mount compatibility!)
TEST_UID="$(id -u "${TARGET_USER}")"
TEST_GID="$(id -g "${TARGET_USER}")"
if [ "${TEST_UID}" -le 0 ]; then
    TEST_UID=1000
    TEST_GID=1000
fi

TMP_WS="$(mktemp -d)"
trap 'rm -rf "${TMP_WS}"' EXIT
chown "${TEST_UID}:${TEST_GID}" "${TMP_WS}" 2>/dev/null || chmod 0777 "${TMP_WS}"

echo "==> Проверка песочницы под UID:GID ${TEST_UID}:${TEST_GID}..."
"${DOCKER_CMD[@]}" run --rm \
    --user "${TEST_UID}:${TEST_GID}" \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    -e HOME=/home/dev \
    -e CUDA_VISIBLE_DEVICES="" \
    -e NVIDIA_VISIBLE_DEVICES=none \
    -v "${TMP_WS}:/workspace" \
    -w /workspace \
    "${IMAGE_TAG}" \
    bash -lc 'id && touch /workspace/.write_ok && rm -f /workspace/.write_ok && node -v && pnpm -v && python3 -c "import openpyxl, requests, bs4, yaml; print(\"py-libs-ok\")" && rustc --version && git --version'

echo " [OK] Linux UID/GID, запись в /workspace и все инструменты проверены"

# 6. Sync Hub CLI profiles if Python venv is present
if [ -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
    echo "==> Синхронизация профилей пользователей Antigravity Hub..."
    "${SCRIPT_DIR}/.venv/bin/python" -c "import asyncio; from app import sandbox_service; asyncio.run(sandbox_service.sync_all_users_sandbox_cli_files(docker_ready=True))" || true
fi

echo "================================================================"
echo " ГОТОВО! Docker-песочница ${IMAGE_TAG} полностью готова на Linux!"
echo "================================================================"
