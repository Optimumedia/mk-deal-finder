# Connects Telegram alerts. Run once:  powershell -ExecutionPolicy Bypass -File setup_telegram.ps1
# Saves the bot token + chat id to .telegram (git-ignored, never pushed).
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$file = Join-Path $PSScriptRoot ".telegram"

Write-Host ""
Write-Host "1. In Telegram, open @BotFather, send /newbot and follow the steps."
Write-Host "   It replies with a token like 123456789:AAH...  - copy it."
$secure = Read-Host "2. Paste the bot token here" -AsSecureString
$token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()

try {
    $me = Invoke-RestMethod "https://api.telegram.org/bot$token/getMe"
} catch {
    Write-Host "That token was not accepted by Telegram. Check it and run this script again." -ForegroundColor Red
    exit 1
}
$bot = $me.result.username
Write-Host "   OK - bot @$bot" -ForegroundColor Green

Write-Host ""
Write-Host "3. Open https://t.me/$bot , press START (or send it any message)."
Read-Host "   Then press Enter here"

$chat = $null
for ($i = 0; $i -lt 6 -and -not $chat; $i++) {
    $updates = Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates"
    $msg = $updates.result | Where-Object { $_.message } | Select-Object -Last 1
    if ($msg) { $chat = $msg.message.chat.id } else { Start-Sleep -Seconds 5 }
}
if (-not $chat) {
    Write-Host "No message reached the bot yet. Send it a message and run this script again." -ForegroundColor Red
    exit 1
}

@(
    "TELEGRAM_BOT_TOKEN=$token"
    "TELEGRAM_CHAT_ID=$chat"
    "DASHBOARD_URL=https://optimumedia.github.io/mk-deal-finder/"
) | Set-Content -Path $file -Encoding ascii

$text = "MK Deal Finder is connected. You'll get the best new deals here after each run (06:15 and 18:15)."
Invoke-RestMethod "https://api.telegram.org/bot$token/sendMessage" -Method Post -Body @{ chat_id = $chat; text = $text } | Out-Null
Write-Host ""
Write-Host "Done - check Telegram for the test message. Settings saved to .telegram" -ForegroundColor Green
