# Runs the scraper on this PC and publishes the results to GitHub.
# Scheduled by setup_schedule.ps1 (12:15 and 16:45, plus a catch-up check at logon
# and every hour while the PC is on). Log: data\local-run.log (rotated at 1 MB, 3 old copies kept)
#   -CatchUp   only run if a scheduled run was missed or cut short (the catch-up task uses this)
param([string]$Python = "python", [switch]$CatchUp)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8   # read Python's output as UTF-8
New-Item -ItemType Directory -Force "data" | Out-Null
$log = Join-Path $PSScriptRoot "data\local-run.log"
# "$_" turns PowerShell's stderr error records into plain text lines.
function Log { process { "$_" | Out-File -FilePath $log -Append -Encoding utf8 } }

# One run at a time, including git. When the PC starts after both slots were missed,
# Windows fires both tasks at once; before this lock they ran side by side and failed
# each other ("database is locked", "Cannot rebase onto multiple branches").
# Windows frees the mutex by itself if this script dies, so it can never get stuck.
$mutex = New-Object System.Threading.Mutex($false, "Global\MKDealFinderRun")
$haveLock = $false
try { $haveLock = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $haveLock = $true }
if (-not $haveLock) {
    if (-not $CatchUp) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') another run is still going; this one is skipped" | Log }
    exit 0
}
if ($CatchUp) {
    $why = & $Python -m scraper.run --is-due 2>&1
    if ($LASTEXITCODE -ne 0) {                   # nothing was missed: stay quiet
        $mutex.ReleaseMutex(); $mutex.Dispose()
        exit 0
    }
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $why" | Log
}

try {
    # Log rotation: local-run.log -> .1 -> .2 -> .3 once it passes 1 MB.
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) {
        for ($i = 2; $i -ge 1; $i--) {
            $src = Join-Path $PSScriptRoot "data\local-run.$i.log"
            if (Test-Path $src) { Move-Item $src (Join-Path $PSScriptRoot "data\local-run.$($i + 1).log") -Force }
        }
        Move-Item $log (Join-Path $PSScriptRoot "data\local-run.1.log") -Force
    }

    # Telegram settings written by setup_telegram.ps1 (git-ignored).
    if (Test-Path ".telegram") {
        Get-Content ".telegram" | ForEach-Object {
            if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { Set-Item -Path "env:$($Matches[1])" -Value $Matches[2] }
        }
    }
    # Telegram warning for failures Python can't report itself (crash on start, failed push).
    function Alert([string]$text) {
        if (-not $env:TELEGRAM_BOT_TOKEN) { return }
        try {
            Invoke-RestMethod "https://api.telegram.org/bot$($env:TELEGRAM_BOT_TOKEN)/sendMessage" -Method Post `
                -Body @{ chat_id = $env:TELEGRAM_CHAT_ID; text = "WARNING - MK Deal Finder: $text" } | Out-Null
        } catch { "telegram alert failed: $_" | Log }
    }

    "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====" | Log
    git pull --rebase --autostash 2>&1 | Log

    & $Python -m scraper.run 2>&1 | Log
    $code = $LASTEXITCODE
    "scraper exit code: $code" | Log
    # 0 = ok, 2 = blocked/error (Python already sent its own alert), 3 = another run held the lock
    # (e.g. one started by hand), anything else = crash.
    if ($code -ne 0 -and $code -ne 2 -and $code -ne 3) {
        Alert "the scraper crashed (exit code $code) and published nothing. See data\local-run.log on the PC."
    }

    # The database stays on this PC (35 MB, twice a day would bloat the public repo); weekly backup
    # instead, taken whenever the newest one is a week old (so a PC that is off on Sundays still gets one).
    $backups = Join-Path $PSScriptRoot "data\backups"
    New-Item -ItemType Directory -Force $backups | Out-Null
    $newest = Get-ChildItem (Join-Path $backups "deals-*.db") -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($code -ne 3 -and (Test-Path "data\deals.db") -and (-not $newest -or $newest.LastWriteTime -lt (Get-Date).AddDays(-7))) {
        Copy-Item "data\deals.db" (Join-Path $backups ("deals-{0}.db" -f (Get-Date -Format 'yyyy-MM-dd'))) -Force
        Get-ChildItem (Join-Path $backups "deals-*.db") | Sort-Object Name -Descending | Select-Object -Skip 4 | Remove-Item -Force
        "weekly database backup written to data\backups" | Log
    }

    git add docs/data.json 2>&1 | Log
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -m "data: $(Get-Date -Format 'yyyy-MM-dd HH:mm') local run" 2>&1 | Log
        git pull --rebase 2>&1 | Log        # code may have been pushed while we ran
        git push 2>&1 | Log
        if ($LASTEXITCODE -ne 0) { Alert "results could not be pushed to GitHub, so the dashboard is not updated. See data\local-run.log." }
    }
    "==== finished $(Get-Date -Format 'HH:mm:ss') ====" | Log
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
