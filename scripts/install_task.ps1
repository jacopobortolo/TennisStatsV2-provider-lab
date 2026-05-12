# Installs a Windows Scheduled Task that runs `python -m tennis_app.cron`
# every hour while the user is logged in.  Re-run to overwrite an existing
# task with the same name.
#
# Usage (PowerShell, from repo root):
#     powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1

$ErrorActionPreference = "Stop"

$repo     = (Resolve-Path "$PSScriptRoot\..").Path
$batch    = Join-Path $repo "scrape.bat"
$taskName = "TennisStats Hourly Scrape"

if (-not (Test-Path $batch)) {
    throw "scrape.bat not found at $batch"
}

# Daily start time = 5 minutes from now, then repeat hourly forever.
$start   = (Get-Date).AddMinutes(5)
$action  = New-ScheduledTaskAction -Execute $batch
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Hourly headless tennis scrape (TennisStatsV2)" `
    -Force | Out-Null

Write-Host "Installed task '$taskName' — first run at $start, then hourly."
Write-Host "View / edit:  taskschd.msc"
Write-Host "Run now:      Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove:       Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
