# Runs the scraper on this PC and publishes the results to GitHub.
# Scheduled twice a day by setup_schedule.ps1. Log: data\local-run.log
param([string]$Python = "python")

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force "data" | Out-Null
$log = Join-Path $PSScriptRoot "data\local-run.log"
function Log { process { $_ | Out-File -FilePath $log -Append -Encoding utf8 } }

"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Log
git pull --rebase --autostash 2>&1 | Log

& $Python -m scraper.run 2>&1 | Log
"scraper exit code: $LASTEXITCODE" | Log

git add data/deals.db docs/data.json 2>&1 | Log
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "data: $(Get-Date -Format 'yyyy-MM-dd HH:mm') local run" 2>&1 | Log
    git push 2>&1 | Log
}
"==== finished $(Get-Date -Format 'HH:mm:ss') ====" | Log
