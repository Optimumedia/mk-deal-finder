# Opens the private dashboard (http://localhost:8800). If it isn't running yet
# (e.g. right after the PC starts), starts the bot task that serves it first.
$url = "http://localhost:8800"
function Up { try { (Invoke-WebRequest -UseBasicParsing "$url/api/ping" -TimeoutSec 2).StatusCode -eq 200 } catch { $false } }

if (-not (Up)) {
    Start-ScheduledTask -TaskName "MK Deal Finder - bot" -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20 -and -not (Up); $i++) { Start-Sleep -Milliseconds 500 }
}
if (Up) {
    Start-Process $url
} else {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("The private dashboard didn't start. Run setup_schedule.ps1 in the project folder, then try again.",
        "MK Deal Finder") | Out-Null
}
