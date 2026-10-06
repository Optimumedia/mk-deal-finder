# Registers two Windows scheduled tasks (06:15 and 18:15) that run run_local.ps1.
# Re-run this script any time; it replaces the existing tasks.
# Remove with:  Unregister-ScheduledTask -TaskName "MK Deal Finder*" -Confirm:$false
# A project venv: scheduled tasks don't always see per-user site-packages.
$venv = Join-Path $PSScriptRoot ".venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    & (Get-Command python).Source -m venv $venv
}
& "$venv\Scripts\python.exe" -m pip install -q -r (Join-Path $PSScriptRoot "requirements.txt")
$python = "$venv\Scripts\python.exe"
$script = Join-Path $PSScriptRoot "run_local.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Python `"$python`"" `
    -WorkingDirectory $PSScriptRoot
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

foreach ($t in @(@{Name = "MK Deal Finder - morning"; At = "06:15"}, @{Name = "MK Deal Finder - evening"; At = "18:15"})) {
    $trigger = New-ScheduledTaskTrigger -Daily -At $t.At
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings `
        -Description "Scrapes North Macedonian real estate deals and pushes them to GitHub." -Force | Out-Null
    Write-Output "registered: $($t.Name) at $($t.At)"
}

# Telegram bot listener (answers 👍/👎 instantly, /top, /status): runs while you're logged in.
$botAction = New-ScheduledTaskAction -Execute "$venv\Scripts\pythonw.exe" -Argument "-m scraper.bot" `
    -WorkingDirectory $PSScriptRoot
$botSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "MK Deal Finder - bot" -Action $botAction `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings $botSettings `
    -Description "Telegram bot for MK Deal Finder: records your deal votes." -Force | Out-Null
Start-ScheduledTask -TaskName "MK Deal Finder - bot"
Write-Output "registered + started: MK Deal Finder - bot (at logon)"
