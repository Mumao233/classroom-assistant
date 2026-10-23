$ErrorActionPreference = 'Stop'
$pidPath = Join-Path $PSScriptRoot 'data\server.pid'
if (-not (Test-Path -LiteralPath $pidPath)) { exit 0 }
$serverPid = [int](Get-Content -LiteralPath $pidPath -Raw)
$ownedProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid" -ErrorAction SilentlyContinue
$expectedApp = Join-Path $PSScriptRoot 'app.py'
if ($ownedProcess -and $ownedProcess.CommandLine.Contains($expectedApp)) {
    Stop-Process -Id $serverPid
    Write-Output 'Stopped the classroom assistant. Saved recordings and notes are retained.'
} elseif ($ownedProcess) {
    throw 'PID now belongs to another program; refusing to stop it.'
}
