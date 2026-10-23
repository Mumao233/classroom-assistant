$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$configPath = Join-Path $PSScriptRoot 'config.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'config.example.json') -Destination $configPath
}
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Output 'Dependencies installed. Place complete local faster-whisper models in models/faster-whisper-small and models/faster-whisper-large-v3-turbo, or edit their paths in config.json. Relative paths are resolved from this application folder. Sign in with codex login. NVIDIA mode also needs compatible CUDA libraries in this virtual environment or cuda_library_dirs.'
