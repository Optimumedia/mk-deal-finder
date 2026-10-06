# Daily business-idea run on this PC. Scheduled by setup_ideas.ps1. Log: data\ideas-run.log
# Ideas and ratings stay on this PC (data\ideas.db, git-ignored); nothing is pushed.
# Uses Claude Code with your Claude subscription: $0 extra, no API key.
param([string]$Python = "python")

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
New-Item -ItemType Directory -Force "data" | Out-Null
# Telegram settings (setup_telegram.ps1) and this PC's Claude Code path (setup_ideas.ps1), both git-ignored.
foreach ($f in @(".telegram", ".ideas")) {
    if (Test-Path $f) {
        Get-Content $f | ForEach-Object {
            if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { Set-Item -Path "env:$($Matches[1])" -Value $Matches[2] }
        }
    }
}
$log = Join-Path $PSScriptRoot "data\ideas-run.log"
function Log { process { "$_" | Out-File -FilePath $log -Append -Encoding utf8 } }

"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Log
git pull --rebase --autostash 2>&1 | Log        # pick up code and ideas.toml changes

& $Python -m ideas.run 2>&1 | Log
$code = $LASTEXITCODE
"ideas exit code: $code" | Log
# 0 = ok, 2 = failed (Python already sent its own warning), anything else = crash.
if ($code -ne 0 -and $code -ne 2 -and $env:TELEGRAM_BOT_TOKEN) {
    try {
        Invoke-RestMethod "https://api.telegram.org/bot$($env:TELEGRAM_BOT_TOKEN)/sendMessage" -Method Post `
            -Body @{ chat_id = $env:TELEGRAM_CHAT_ID; text = "WARNING - Idea Finder crashed (exit code $code). See data\ideas-run.log on the PC." } | Out-Null
    } catch { "telegram alert failed: $_" | Log }
}
"==== finished $(Get-Date -Format 'HH:mm:ss') ====" | Log
