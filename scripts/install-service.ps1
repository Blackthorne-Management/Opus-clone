# Registers a Task Scheduler task that starts the clipper at login, in the background, and restarts it if it crashes.
# Run:  powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$PyW = Join-Path $Repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $PyW)) { throw "Run scripts\install.ps1 first." }

$TaskName = "KIR Clipper"
$Action = New-ScheduledTaskAction -Execute $PyW -Argument "-m kir_clipper run" -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description "KIR Auto-Clipper: watches the stream folder, renders and schedules clips." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started '$TaskName'. Dashboard: http://127.0.0.1:8765" -ForegroundColor Green
Write-Host "Logs: <data folder>\logs\kir_clipper.log   (data folder is set in config.yaml, default ~\Streams\kir-data)"
