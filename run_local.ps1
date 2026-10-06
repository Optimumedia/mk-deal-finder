# Fallback runner for Windows (use only if GitHub's servers get blocked).
# Schedule twice a day:
#   schtasks /Create /SC DAILY /ST 06:15 /TN "MKDeals-AM" /TR "powershell -ExecutionPolicy Bypass -File \"$PSScriptRoot\run_local.ps1\""
#   schtasks /Create /SC DAILY /ST 18:15 /TN "MKDeals-PM" /TR "powershell -ExecutionPolicy Bypass -File \"$PSScriptRoot\run_local.ps1\""
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
git pull --rebase --autostash
python -m scraper.run *>> "data\local-run.log"
git add data/deals.db docs/data.json
git commit -m "data: local run $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
git push
