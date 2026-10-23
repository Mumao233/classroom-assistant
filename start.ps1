param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$dataRoot = Join-Path $appRoot 'data'
New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
$port = 8766
$url = "http://127.0.0.1:$port"
$runtimePython = Join-Path $appRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) { $runtimePython = $null }
try {
    $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 2
} catch { $health = $null }
if ($health.app -ne 'classroom-assistant') {
    if (-not $runtimePython) {
        throw 'Python environment is missing. Run setup.ps1 first.'
    }
    $runtimePython = (Resolve-Path -LiteralPath $runtimePython).Path
    $appScript = Join-Path $appRoot 'app.py'
    $taskProcess = Start-Process -FilePath $runtimePython -ArgumentList @('"' + $appScript + '"') -WorkingDirectory $appRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataRoot 'server.log') -RedirectStandardError (Join-Path $dataRoot 'server-error.log')
    Set-Content -LiteralPath (Join-Path $dataRoot 'server.pid') -Value $taskProcess.Id -Encoding ascii
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        Start-Sleep -Milliseconds 400
        try { $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 1 } catch { $health = $null }
        if ($health.app -eq 'classroom-assistant') { break }
        if ($taskProcess.HasExited) { throw 'Classroom server exited. See data/server-error.log.' }
    }
    if ($health.app -ne 'classroom-assistant') { throw 'Classroom server did not become ready. See data/server-error.log.' }
}
if (-not $NoBrowser) {
    $edgePath = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
    if (Test-Path -LiteralPath $edgePath) {
        Start-Process -FilePath $edgePath -ArgumentList @("--app=$url")
    } else { Start-Process $url }
}
Write-Output "Classroom assistant ready: $url"
