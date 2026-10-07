# Sets up the daily Business Idea Finder. Run once:
#   powershell -ExecutionPolicy Bypass -File setup_ideas.ps1
# Needs: setup_telegram.ps1 done first (ideas arrive in the same Telegram bot),
# and a Claude subscription (Pro or Max). Costs $0 extra: it runs Claude Code
# on this PC, logged in with your subscription. No API key is used.
# Registers a daily task at 07:45. Re-run any time; it replaces the task.
# Remove with:  Unregister-ScheduledTask -TaskName "MK Deal Finder - ideas" -Confirm:$false
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".telegram")) {
    Write-Host "Run setup_telegram.ps1 first: the ideas are delivered through that Telegram bot." -ForegroundColor Red
    exit 1
}
# These would make Claude Code bill an API account; the idea runs never pass them on either.
foreach ($v in @("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")) {
    Remove-Item "env:$v" -ErrorAction SilentlyContinue
}

# Project venv (shared with the deal finder).
$venv = Join-Path $PSScriptRoot ".venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    & (Get-Command python).Source -m venv $venv
}
& "$venv\Scripts\python.exe" -m pip install -q -r (Join-Path $PSScriptRoot "requirements.txt")
$python = "$venv\Scripts\python.exe"

# 1. Claude Code, native build (the npm wrapper can't pass the idea schema safely).
function Find-Claude {
    $native = Join-Path $HOME ".local\bin\claude.exe"
    if (Test-Path $native) { return $native }
    $cmd = Get-Command claude -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -like "*.exe") { return $cmd.Source }
    return $null
}
$claude = Find-Claude
if (-not $claude) {
    Write-Host ""
    Write-Host "Claude Code (native build) isn't installed. It's free with your Claude plan."
    $ok = Read-Host "Install it now from claude.ai? (Y/n)"
    if ($ok -match '^[nN]') { Write-Host "Install it with:  irm https://claude.ai/install.ps1 | iex   then run this again."; exit 1 }
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    $claude = Find-Claude
    if (-not $claude) { Write-Host "Install didn't finish. Open a new PowerShell window and run this script again." -ForegroundColor Red; exit 1 }
}
Write-Host "   Claude Code: $claude" -ForegroundColor Green

# 2. Logged in with your subscription, not an API key.
$status = & $claude auth status --json | ConvertFrom-Json
if (-not $status.loggedIn) {
    Write-Host ""
    Write-Host "Log in with your Claude account (the one with your Pro or Max plan). A browser window opens."
    & $claude auth login
    $status = & $claude auth status --json | ConvertFrom-Json
}
if (-not $status.loggedIn) { Write-Host "Not logged in. Run this script again." -ForegroundColor Red; exit 1 }
if ("$($status.authMethod)" -match 'api') {
    Write-Host "Claude Code is logged in with an API key, which is billed per use." -ForegroundColor Red
    Write-Host "Run:  & `"$claude`" auth logout   then   & `"$claude`" auth login   and pick your Claude subscription."
    exit 1
}
Write-Host "   Logged in ($($status.authMethod))" -ForegroundColor Green

# 3. Quick test.
$reply = "Reply with the single word OK." | & $claude -p --model sonnet
if ("$reply" -notmatch 'OK') {
    Write-Host "Claude Code didn't answer the test: $reply" -ForegroundColor Red
    exit 1
}
Write-Host "   Test answer received" -ForegroundColor Green
"IDEAS_CLAUDE_PATH=$claude" | Set-Content -Path (Join-Path $PSScriptRoot ".ideas") -Encoding ascii

# 4. Daily task at 07:45.
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

# 5. Restart the Telegram bot so it knows the idea buttons and /ideas, /taste.
if (Get-ScheduledTask -TaskName "MK Deal Finder - bot" -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName "MK Deal Finder - bot"
    Start-ScheduledTask -TaskName "MK Deal Finder - bot"
    Write-Output "restarted: MK Deal Finder - bot"
} else {
    Write-Host "The Telegram bot task isn't registered: run setup_schedule.ps1 so the rating buttons work." -ForegroundColor Yellow
}

$now = Read-Host "Run the first idea search now? It takes about 5-15 minutes. (Y/n)"
if ($now -notmatch '^[nN]') {
    Start-ScheduledTask -TaskName "MK Deal Finder - ideas"
    Write-Host "Started. Ideas arrive in Telegram when it finishes; the log is data\ideas-run.log" -ForegroundColor Green
}
