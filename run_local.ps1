# Runs the scraper on this PC and publishes the results to GitHub.
# Scheduled twice a day by setup_schedule.ps1. Log: data\local-run.log
param([string]$Python = "python")

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8   # read Python's output as UTF-8
New-Item -ItemType Directory -Force "data" | Out-Null
# Telegram settings written by setup_telegram.ps1 (git-ignored).
if (Test-Path ".telegram") {
    Get-Content ".telegram" | ForEach-Object {
        if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { Set-Item -Path "env:$($Matches[1])" -Value $Matches[2] }
    }
}
$log = Join-Path $PSScriptRoot "data\local-run.log"
# "$_" turns PowerShell's stderr error records into plain text lines.
function Log { process { "$_" | Out-File -FilePath $log -Append -Encoding utf8 } }

"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Log
git pull --rebase --autostash 2>&1 | Log

& $Python -m scraper.run 2>&1 | Log
"scraper exit code: $LASTEXITCODE" | Log

git add data/deals.db docs/data.json 2>&1 | Log
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "data: $(Get-Date -Format 'yyyy-MM-dd HH:mm') local run" 2>&1 | Log
    git pull --rebase 2>&1 | Log        # code may have been pushed while we ran
    git push 2>&1 | Log
}
"==== finished $(Get-Date -Format 'HH:mm:ss') ====" | Log
