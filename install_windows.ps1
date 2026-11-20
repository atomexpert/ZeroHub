param([string]$InstallDir = (Join-Path $HOME "ZeroHub"))
$ErrorActionPreference = 'Stop'
Get-Command git -ErrorAction Stop | Out-Null
$python = Get-Command python -CommandType Application -ErrorAction Stop
& $python.Source -c 'import sys; sys.exit(sys.version_info < (3, 11))'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }

if ((Test-Path (Join-Path $PSScriptRoot "run_server.py")) -and
    (Test-Path (Join-Path $PSScriptRoot "requirements.txt"))) {
    $InstallDir = $PSScriptRoot
} elseif (-not (Test-Path (Join-Path $InstallDir ".git"))) {
    & git clone https://github.com/atomexpert/ZeroHub.git $InstallDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not clone ZeroHub.' }
}
$InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
$launcher = Join-Path $InstallDir "start_hub.bat"
if (-not (Test-Path -LiteralPath $launcher)) { throw 'ZeroHub launcher is missing.' }

$desktop = [Environment]::GetFolderPath('DesktopDirectory')
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $desktop 'ZeroHub.lnk'))
$shortcut.TargetPath = (Join-Path $env:SystemRoot 'System32\wscript.exe')
$shortcut.Arguments = '"' + (Join-Path $InstallDir 'start_hub_silent.vbs') + '"'
$shortcut.WorkingDirectory = $InstallDir
$shortcut.Save()

Start-Process -FilePath $launcher -WorkingDirectory $InstallDir -WindowStyle Hidden
Write-Host 'ZeroHub is starting. First login credentials will appear in data\FIRST_RUN_CREDENTIALS.txt.'
