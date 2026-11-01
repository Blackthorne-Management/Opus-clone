# Auto-Clipper: one-time setup on Windows (also safe to run again after an update).
# Easiest: double-click Install.bat in the app folder. Or in PowerShell, from the app folder:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
# -Quiet skips every question (used by the automatic build check on GitHub).
param([switch]$Quiet)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Ask($question, $default = "y") {
    if ($Quiet) { return $default -eq "y" }
    $answer = Read-Host "$question [$(if ($default -eq 'y') {'Y/n'} else {'y/N'})]"
    if (-not $answer) { $answer = $default }
    return $answer.Trim().ToLower().StartsWith("y")
}

function Ensure($cmd, $wingetId, $label) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return $true }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "Missing: $label. Install it, then run this again." -ForegroundColor Yellow
        return $false
    }
    if (-not (Ask "$label is not installed. Install it now with winget?")) { return $false }
    winget install -e --id $wingetId --accept-package-agreements --accept-source-agreements --silent
    Refresh-Path
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "$label was installed, but Windows needs a new window to see it. Close this window and double-click Install.bat again." -ForegroundColor Yellow
        return $false
    }
    return $true
}

# Python versions every package the app needs has been checked on, best first. "py -3" alone would pick the
# newest Python on the PC, which can be one the transcription libraries don't support yet.
$Supported = @("3.12", "3.13", "3.11")

function Find-Python {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { return $null }
    foreach ($v in $Supported) {
        cmd /c "py -$v -c ""import sys"" >nul 2>&1"
        if ($LASTEXITCODE -eq 0) { return $v }
    }
    return $null
}

Write-Host "== Auto-Clipper setup ==" -ForegroundColor Cyan
$ok = Ensure "ffmpeg" "Gyan.FFmpeg" "FFmpeg (video tools)"
$PyVer = Find-Python
if (-not $PyVer) {
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Python 3.12 (needed by the app) is not installed. Install it now with winget?")) {
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
        Refresh-Path
        $PyVer = Find-Python
    }
    if (-not $PyVer) {
        Write-Host "Python 3.12 is needed (from python.org, tick 'Add to PATH'). If you just installed it, close this window and run Install.bat again." -ForegroundColor Yellow
        $ok = $false
    }
}
if (-not $ok) { if (-not $Quiet) { Read-Host "Press Enter to close" }; exit 1 }

$Py = Join-Path $Repo ".venv\Scripts\python.exe"
if (Test-Path $Py) {
    $venvVer = (& $Py -c "import sys; print('%d.%d' % sys.version_info[:2])") 2>$null
    if ($Supported -notcontains "$venvVer") {
        Write-Host "Rebuilding the app's Python environment (it was made with Python $venvVer)..."
        Remove-Item -Recurse -Force ".venv"
    }
}
if (-not (Test-Path $Py)) {
    Write-Host "Creating the app's private Python environment (Python $PyVer)..."
    py -$PyVer -m venv .venv
}
Write-Host "Installing the app (the first time takes a few minutes)..."
& $Py -m pip install --upgrade pip --quiet
# whisper = local transcription, faces = speaker-aware reframing, research = public Shorts for Niche research
& $Py -m pip install -e ".[whisper,faces,research]" --progress-bar off
if ($LASTEXITCODE -ne 0) { throw "Installing the app failed (the reason is in the lines above). Check your internet connection and run Install.bat again." }

# GPU transcription needs NVIDIA's cuBLAS/cuDNN runtime. Installing via pip is the simplest route.
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Write-Host "NVIDIA graphics card found: adding the libraries for fast transcription..."
    & $Py -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" --quiet
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    if (-not $Quiet) {
        Write-Host ""
        Write-Host "Your Claude API key picks and scores the clips (console.anthropic.com -> API keys)." -ForegroundColor Cyan
        $key = Read-Host "Paste it here and press Enter (or just press Enter to add it later in the .env file)"
        if ($key.Trim()) {
            (Get-Content ".env") -replace '^ANTHROPIC_API_KEY=.*$', "ANTHROPIC_API_KEY=$($key.Trim())" | Set-Content ".env" -Encoding ascii
            Write-Host "Saved to .env" -ForegroundColor Green
        }
    }
}

Write-Host ""
& $Py -m kir_clipper doctor

if (-not $Quiet) {
    Write-Host ""
    if (Ask "Start the clipper now, and automatically every time you log in?") {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install-service.ps1")
        Start-Sleep -Seconds 4
        Start-Process "http://127.0.0.1:8765/#settings"
        Write-Host "The dashboard is open in your browser. Fill in Settings -> Make it yours first." -ForegroundColor Green
    } else {
        Write-Host "To start it later: double-click Start.bat, then open http://127.0.0.1:8765" -ForegroundColor Green
    }
    Read-Host "Setup finished. Press Enter to close"
}
