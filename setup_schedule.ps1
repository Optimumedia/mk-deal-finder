# Registers the Windows scheduled tasks that run run_local.ps1 (the PC is on 12:00-19:00):
#   - "MK Deal Finder - morning"  12:15 (full sweep)
#   - "MK Deal Finder - evening"  16:45 (quick sweep)
#   - "MK Deal Finder - catch-up" at logon (+5 min) and every hour 12:30-18:30: runs only if one of
#     the two slots above was missed or cut short (PC started late, shut down mid-run, crash).
#   - "MK Deal Finder - bot"      at logon: Telegram listener + private dashboard on 127.0.0.1:8800
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
$runArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Python `"$python`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $runArgs -WorkingDirectory $PSScriptRoot
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

# New-ScheduledTaskTrigger stores the start time with a fixed UTC offset ("synchronize across
# time zones"), so after the clocks went back on 25 Oct 2026 the runs fired at 11:15 and 15:45.
# A start time without an offset follows the PC's local clock, summer and winter.
function Set-LocalStart($trigger, [string]$at) {
    $trigger.StartBoundary = (Get-Date).Date.Add([TimeSpan]::Parse($at)).ToString("yyyy-MM-dd'T'HH:mm:ss")
    $trigger
}

foreach ($t in @(@{Name = "MK Deal Finder - morning"; At = "12:15"}, @{Name = "MK Deal Finder - evening"; At = "16:45"})) {
    $trigger = Set-LocalStart (New-ScheduledTaskTrigger -Daily -At $t.At) $t.At
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings `
        -Description "Scrapes North Macedonian real estate deals and pushes them to GitHub." -Force | Out-Null
    Write-Output "registered: $($t.Name) at $($t.At)"
}

# Catch-up: cheap check (a second or two), and a run only when a slot was missed.
$catchAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "$runArgs -CatchUp" -WorkingDirectory $PSScriptRoot
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$atLogon.Delay = "PT5M"                                  # let Wi-Fi connect first
$hourly = Set-LocalStart (New-ScheduledTaskTrigger -Daily -At "12:30") "12:30"
$hourly.Repetition = (New-ScheduledTaskTrigger -Once -At "12:30" -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 6)).Repetition
Register-ScheduledTask -TaskName "MK Deal Finder - catch-up" -Action $catchAction -Trigger @($atLogon, $hourly) `
    -Settings $settings -Description "Runs MK Deal Finder only if a scheduled run was missed or cut short." -Force | Out-Null
Write-Output "registered: MK Deal Finder - catch-up (at logon + hourly 12:30-18:30, only when a run was missed)"

# Telegram bot listener (answers votes instantly, /top, /status): runs while you're logged in.
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
