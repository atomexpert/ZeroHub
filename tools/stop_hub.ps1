param([Parameter(Mandatory=$true)][string]$RepositoryRoot)

$repoPath = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\') + '\'
$listeners = Get-NetTCPConnection -LocalPort 8765,8766 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
$scopedProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -in @('python.exe', 'pythonw.exe') -and $_.CommandLine -and
        $_.CommandLine.IndexOf($repoPath, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $_.CommandLine -match 'run_server\.py|-m\s+app\.windows_desktop') -or
    ($_.Name -eq 'ssh.exe' -and $_.CommandLine -match 'hub_tunnel_ed25519')
}
foreach ($process in $scopedProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
foreach ($listenerId in $listeners) {
    if ($listenerId -gt 0) {
        Stop-Process -Id $listenerId -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 1
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host '[WARN] Port 8765 is still in use (run stop_hub.bat as Administrator)'
    exit 1
}
Write-Host 'Done.'
exit 0
