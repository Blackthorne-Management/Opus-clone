# KIR Auto-Clipper: one-time setup on Windows.
# Run from the repo folder in PowerShell:   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

function Need($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "Missing: $cmd. $hint" -ForegroundColor Yellow
        return $false
    }
    return $true
}

$ok = Need "py" "Install Python 3.11+ from https://www.python.org/downloads/ (tick 'Add to PATH')."
$ok = (Need "ffmpeg" "Run: winget install Gyan.FFmpeg   (then open a new PowerShell window)") -and $ok
if (-not $ok) { exit 1 }

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    py -3 -m venv .venv
}
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
& $Py -m pip install --upgrade pip
# whisper = local transcription, faces = speaker-aware reframing, research = public Shorts for Niche research
& $Py -m pip install -e ".[whisper,faces,research]"

# GPU transcription needs NVIDIA's cuBLAS/cuDNN runtime. Installing via pip is the simplest route.
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Write-Host "NVIDIA GPU detected: installing CUDA runtime libraries for faster-whisper..."
    & $Py -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env - open it and paste your API keys." -ForegroundColor Cyan
}

& $Py -m kir_clipper doctor
Write-Host ""
Write-Host "Next: edit .env and config.yaml, then run scripts\install-service.ps1 to start it at login." -ForegroundColor Green
