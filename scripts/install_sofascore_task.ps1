# Installs a Windows Scheduled Task that runs the SofaScore -> Turso job
# when Windows starts and when the current user logs in. Re-run to overwrite
# an existing task.
#
# Usage (PowerShell, from repo root):
#     powershell -ExecutionPolicy Bypass -File scripts\install_sofascore_task.ps1

$ErrorActionPreference = "Stop"

$repo = (Resolve-Path "$PSScriptRoot\..").Path
$batch = Join-Path $repo "run_sofascore_cloud_once.bat"
$taskName = "TennisStats SofaScore Cloud Scrape"

if (-not (Test-Path $batch)) {
    throw "run_sofascore_cloud_once.bat not found at $batch"
}

$action = New-ScheduledTaskAction -Execute $batch -WorkingDirectory $repo
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn)
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $triggers -Settings $settings `
    -Description "Run SofaScore hybrid scrape to Turso when Windows starts or logs in" `
    -Force | Out-Null

Write-Host "Installed task '$taskName'."
Write-Host "Run now:      Start-ScheduledTask -TaskName '$taskName'"
Write-Host "View / edit:  taskschd.msc"
Write-Host "Remove:       Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"