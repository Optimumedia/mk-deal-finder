# Opens the private dashboard (http://localhost:8800). If it isn't running yet
# (e.g. right after the PC starts), starts the bot task that serves it first.
# The health check uses 127.0.0.1: "localhost" can resolve to IPv6 first and
# stall for seconds.
$url = "http://localhost:8800"
function Up {
    try { (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8800/api/ping" -TimeoutSec 5).StatusCode -eq 200 }
    catch { $false }
}

if (-not (Up)) {
    Start-ScheduledTask -TaskName "MK Deal Finder - bot" -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 30 -and -not (Up); $i++) { Start-Sleep -Seconds 1 }
}
if (Up) {
    Start-Process $url
} else {
    Add-Type -AssemblyName System.Windows.Forms
    # TopMost owner window so the message is never hidden behind other windows.
    $owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true; ShowInTaskbar = $false }
    [System.Windows.Forms.MessageBox]::Show($owner,
        "The private dashboard didn't start.`n`nOpen the project folder and run setup_schedule.ps1, then try again.",
        "MK Deal Finder", "OK", "Warning") | Out-Null
}
