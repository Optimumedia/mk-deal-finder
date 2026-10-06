# Sets up the daily Business Idea Finder. Run once:
#   powershell -ExecutionPolicy Bypass -File setup_ideas.ps1
# Needs: setup_telegram.ps1 done first (ideas arrive in the same Telegram bot).
# Saves your Anthropic API key to .anthropic (git-ignored, never pushed) and
# registers a daily task at 07:45. Re-run any time; it replaces the task.
# Remove with:  Unregister-ScheduledTask -TaskName "MK Deal Finder - ideas" -Confirm:$false
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location $PSScriptRoot

if (-not (Test-Path ".telegram")) {
    Write-Host "Run setup_telegram.ps1 first: the ideas are delivered through that Telegram bot." -ForegroundColor Red
    exit 1
}

# Project venv (shared with the deal finder), with the anthropic package.
$venv = Join-Path $PSScriptRoot ".venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    & (Get-Command python).Source -m venv $venv
}
& "$venv\Scripts\python.exe" -m pip install -q -r (Join-Path $PSScriptRoot "requirements.txt")
$python = "$venv\Scripts\python.exe"

$file = Join-Path $PSScriptRoot ".anthropic"
if (Test-Path $file) {
    $again = Read-Host "An API key is already saved. Replace it? (y/N)"
} else { $again = "y" }
if ($again -match '^[yY]') {
    Write-Host ""
    Write-Host "1. Open https://console.anthropic.com/settings/keys and create a key (it starts with sk-ant-)."
    Write-Host "   Set a monthly spend limit under Settings > Limits. A run costs roughly 0.30-1.00 USD."
    $secure = Read-Host "2. Paste the API key here" -AsSecureString
    $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
    if ($key -notmatch '^sk-ant-') {
        Write-Host "That doesn't look like an Anthropic API key (sk-ant-...). Nothing saved." -ForegroundColor Red
        exit 1
    }
    try {
        Invoke-RestMethod "https://api.anthropic.com/v1/models?limit=1" -Headers @{ "x-api-key" = $key; "anthropic-version" = "2023-06-01" } | Out-Null
    } catch {
        Write-Host "Anthropic did not accept that key. Check it and run this script again." -ForegroundColor Red
        exit 1
    }
    "ANTHROPIC_API_KEY=$key" | Set-Content -Path $file -Encoding ascii
    Write-Host "   OK - key saved to .anthropic" -ForegroundColor Green
}

$script = Join-Path $PSScriptRoot "run_ideas.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Python `"$python`"" `
    -WorkingDirectory $PSScriptRoot
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "MK Deal Finder - ideas" -Action $action `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "07:45") -Settings $settings `
    -Description "Researches and sends daily business ideas to Telegram for a 1-10 rating." -Force | Out-Null
Write-Output "registered: MK Deal Finder - ideas at 07:45"

# Restart the Telegram bot so it knows the idea buttons and /ideas, /taste.
if (Get-ScheduledTask -TaskName "MK Deal Finder - bot" -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName "MK Deal Finder - bot"
    Start-ScheduledTask -TaskName "MK Deal Finder - bot"
    Write-Output "restarted: MK Deal Finder - bot"
} else {
    Write-Host "The Telegram bot task isn't registered: run setup_schedule.ps1 so the rating buttons work." -ForegroundColor Yellow
}

$now = Read-Host "Run the first idea search now? It takes 3-8 minutes. (Y/n)"
if ($now -notmatch '^[nN]') {
    Start-ScheduledTask -TaskName "MK Deal Finder - ideas"
    Write-Host "Started. Ideas arrive in Telegram when it finishes; the log is data\ideas-run.log" -ForegroundColor Green
}
