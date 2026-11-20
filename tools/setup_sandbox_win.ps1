param(
    [string]$UserHome = $env:USERPROFILE,
    [switch]$Elevated
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$HubRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ImageTag = "agy-hub-sandbox:latest"
$DockerfilePath = Join-Path $HubRoot "Dockerfile.sandbox"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host " [OK] $Text" -ForegroundColor Green
}

function Write-Warn([string]$Text) {
    Write-Host " [!] $Text" -ForegroundColor Yellow
}

function Find-DockerExe {
    $cmd = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path $cmd.Source)) { return $cmd.Source }
    $candidates = @(
        "$env:ProgramW6432\Docker\Docker\resources\bin\docker.exe",
        "C:\Program Files\Docker\Docker\resources\bin\docker.exe",
        "$UserHome\AppData\Local\Programs\Docker\Docker\resources\bin\docker.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

function Find-DockerDesktopApp {
    $candidates = @(
        "$env:ProgramW6432\Docker\Docker\Docker Desktop.exe",
        "C:\Program Files\Docker\Docker\Docker Desktop.exe",
        "$UserHome\AppData\Local\Programs\Docker\Docker\Docker Desktop.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

Write-Host "================================================================" -ForegroundColor DarkCyan
Write-Host "   Antigravity Hub — Установка и сборка Docker-песочницы" -ForegroundColor White
Write-Host "   Защита GPU/майнера: 0% GPU / 0 MB VRAM (gpuSupport=false)" -ForegroundColor Gray
Write-Host "================================================================" -ForegroundColor DarkCyan

# 1. Ensure .wslconfig protects GPU/VRAM and caps WSL2 memory
Write-Step "Проверка защиты видеокарты и памяти WSL 2 (.wslconfig)..."
$wslCfgPath = Join-Path $UserHome ".wslconfig"
if (-not (Test-Path $wslCfgPath)) {
    $wslCfgContent = @(
        "# Antigravity Hub — WSL2 & Docker Desktop Resource Guard",
        "# Protects NVIDIA GPU / miner (0% GPU, 0 MB VRAM) and bounds WSL2 RAM usage",
        "[wsl2]",
        "memory=8GB",
        "processors=6",
        "swap=2GB",
        "gpuSupport=false",
        ""
    ) -join "`r`n"
    [System.IO.File]::WriteAllText($wslCfgPath, $wslCfgContent, [System.Text.Encoding]::UTF8)
    Write-Ok "Создан $wslCfgPath (gpuSupport=false, memory=8GB)"
} else {
    $existingCfg = Get-Content -Path $wslCfgPath -Raw -ErrorAction SilentlyContinue
    if ($existingCfg -notmatch "gpuSupport\s*=") {
        Add-Content -Path $wslCfgPath -Value "`r`ngpuSupport=false" -Encoding UTF8
        Write-Ok "В $wslCfgPath добавлен параметр gpuSupport=false (0% GPU для WSL 2)"
    } else {
        Write-Ok "Файл $wslCfgPath уже настроен"
    }
}

# 2. Check if WSL 2 and Docker Desktop are installed
$dockerExe = Find-DockerExe
$wslReady = $false
try {
    $null = & wsl.exe --status 2>$null
    if ($LASTEXITCODE -eq 0) { $wslReady = $true }
} catch {
    $wslReady = $false
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if ((-not $wslReady) -or (-not $dockerExe)) {
    if (-not $isAdmin) {
        Write-Warn "Для первой установки WSL 2 / Docker Desktop нужны права Администратора (UAC)."
        Write-Host "Сейчас откроется стандартное окно подтверждения Windows..." -ForegroundColor Gray
        $argList = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$PSCommandPath`"",
            "-UserHome", "`"$UserHome`"",
            "-Elevated"
        )
        $p = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -Verb RunAs -Wait -PassThru
        exit $p.ExitCode
    }

    $needsReboot = $false

    if (-not $wslReady) {
        Write-Step "Установка ядра WSL 2 (без лишнего дистрибутива Linux)..."
        & wsl.exe --install --no-distribution
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Включаю компоненты Windows (VirtualMachinePlatform + WSL) через DISM..."
            & dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
            & dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
            $needsReboot = $true
        } else {
            try {
                $null = & wsl.exe --status 2>$null
                if ($LASTEXITCODE -ne 0) { $needsReboot = $true }
            } catch {
                $needsReboot = $true
            }
        }
        Write-Ok "Компоненты WSL 2 подготовлены"
    }

    $dockerExe = Find-DockerExe
    if (-not $dockerExe) {
        Write-Step "Установка Docker Desktop для Windows (WSL 2 backend)..."
        $installedViaWinget = $false
        $wingetCmd = Get-Command winget.exe -ErrorAction SilentlyContinue
        if ($wingetCmd) {
            try {
                & winget.exe install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements --disable-interactivity
                $dockerExe = Find-DockerExe
                if ($dockerExe) { $installedViaWinget = $true }
            } catch {
                $installedViaWinget = $false
            }
        }

        if (-not $installedViaWinget) {
            Write-Warn "Скачиваю официальный установщик Docker Desktop напрямую на диск D:..."
            $cacheDir = Join-Path $HubRoot "data\cache"
            if (-not (Test-Path $cacheDir)) { New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null }
            $installerPath = Join-Path $cacheDir "DockerDesktopInstaller.exe"
            & curl.exe -fL --retry 3 -o $installerPath "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
            if (-not (Test-Path $installerPath)) {
                Write-Host "[ОШИБКА] Не удалось скачать Docker Desktop Installer." -ForegroundColor Red
                Read-Host "Нажмите Enter для выхода"
                exit 1
            }
            Write-Step "Запуск тихой установки Docker Desktop..."
            $instProc = Start-Process -FilePath $installerPath -ArgumentList "install --quiet --accept-license --backend=wsl-2" -Wait -PassThru
            if ($instProc.ExitCode -ne 0 -and $instProc.ExitCode -ne 3010) {
                Write-Host "[ОШИБКА] Установщик Docker Desktop завершился с кодом $($instProc.ExitCode)" -ForegroundColor Red
                Read-Host "Нажмите Enter для выхода"
                exit 1
            }
            if ($instProc.ExitCode -eq 3010) { $needsReboot = $true }
            $dockerExe = Find-DockerExe
        }
        Write-Ok "Docker Desktop установлен"
    }

    if ($needsReboot) {
        Write-Host ""
        Write-Host "================================================================" -ForegroundColor Yellow
        Write-Host " ТРЕБУЕТСЯ 1 ПЕРЕЗАГРУЗКА WINDOWS (авторебут НЕ выполнялся, чтобы не прерывать майнер)" -ForegroundColor Yellow
        Write-Host " После перезагрузки ПК просто ещё раз запустите setup_sandbox.bat —" -ForegroundColor White
        Write-Host " он соберёт образ agy-hub-sandbox:latest за 1–2 минуты." -ForegroundColor White
        Write-Host "================================================================" -ForegroundColor Yellow
        Read-Host "Нажмите Enter для закрытия окна"
        exit 0
    }
}

$dockerExe = Find-DockerExe
if (-not $dockerExe) {
    Write-Host "[ОШИБКА] docker.exe не найден после установки. Перезагрузите Windows и запустите setup_sandbox.bat снова." -ForegroundColor Red
    Read-Host "Нажмите Enter для выхода"
    exit 1
}

# 3. Ensure Docker Desktop daemon is running
Write-Step "Проверка демона Docker Engine..."
$daemonUp = $false
try {
    $null = & $dockerExe info --format "{{.ServerVersion}}" 2>$null
    if ($LASTEXITCODE -eq 0) { $daemonUp = $true }
} catch {
    $daemonUp = $false
}

if (-not $daemonUp) {
    $desktopApp = Find-DockerDesktopApp
    if ($desktopApp) {
        Write-Host " Запускаю Docker Desktop в фоне и жду готовности демона (до 90 сек)..." -ForegroundColor Gray
        Start-Process -FilePath $desktopApp
        for ($i = 0; $i -lt 45; $i++) {
            Start-Sleep -Seconds 2
            try {
                $null = & $dockerExe info --format "{{.ServerVersion}}" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $daemonUp = $true
                    break
                }
            } catch {}
            Write-Host -NoNewline "."
        }
        Write-Host ""
    }
}

if (-not $daemonUp) {
    Write-Warn "Демон Docker ещё не ответил. Если Windows ещё не перезагружалась после установки WSL 2 — перезагрузите ПК, откройте Docker Desktop (примите стартовое окно при первом входе) и запустите setup_sandbox.bat повторно."
    Read-Host "Нажмите Enter для выхода"
    exit 1
}

$ver = (& $dockerExe info --format "{{.ServerVersion}}" 2>$null).Trim()
Write-Ok "Docker Engine $ver запущен"

# 4. Build agy-hub-sandbox:latest
Write-Step "Сборка изолированного образа $ImageTag (Node.js 22 + Python 3 + Rust + Git)..."
Push-Location $HubRoot
try {
    & $dockerExe build -t $ImageTag -f $DockerfilePath .
    if ($LASTEXITCODE -ne 0) {
        throw "docker build завершился с кодом $LASTEXITCODE"
    }
} finally {
    Pop-Location
}
Write-Ok "Образ $ImageTag успешно собран"

# 5. Smoke-test container
Write-Step "Проверка контейнера песочницы (UID, языки, 0% GPU)..."
$smokeCmd = 'id && node -v && pnpm -v && python3 -c ''import sys, openpyxl, requests, bs4, yaml; print(sys.version_info[:2])'' && rustc --version && git --version && echo GPU=$NVIDIA_VISIBLE_DEVICES'
& $dockerExe run --rm --user 1000:1000 --cap-drop=ALL --security-opt=no-new-privileges -e NVIDIA_VISIBLE_DEVICES=none -e CUDA_VISIBLE_DEVICES= $ImageTag bash -lc $smokeCmd
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ОШИБКА] Проверочный запуск контейнера не прошёл!" -ForegroundColor Red
    Read-Host "Нажмите Enter для выхода"
    exit 1
}
Write-Ok "Контейнер прошёл все проверки безопасности и окружения"

# 6. Sync CLI sandbox profiles immediately (no server restart needed)
$venvPy = Join-Path $HubRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    Write-Step "Синхронизация профилей пользователей Antigravity Hub..."
    Push-Location $HubRoot
    try {
        & $venvPy -c "import asyncio; from app import sandbox_service; asyncio.run(sandbox_service.sync_all_users_sandbox_cli_files(docker_ready=True))"
        Write-Ok "Профили пользователей (@graf, @alex и др.) синхронизированы"
    } catch {
        Write-Warn "Сервер обновит статус Docker автоматически в течение 10 секунд"
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " ГОТОВО! Изолированный режим «🐳 Терминал» полностью активен!" -ForegroundColor Green
Write-Host " Перезапускать сервер Antigravity Hub НЕ требуется." -ForegroundColor White
Write-Host "================================================================" -ForegroundColor Green
Read-Host "Нажмите Enter для закрытия окна"
exit 0
