# Stops and removes the background task. Your clips, database, and recordings are not touched.
$TaskName = "KIR Clipper"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed '$TaskName'."
