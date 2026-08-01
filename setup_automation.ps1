# Setup script for the automated daily routine.
# ================================================
# Registers two Windows scheduled tasks:
#   1. CryptoDailyCheck   — runs daily_check.py at 00:05 UTC every day
#   2. CryptoUniverseRefresh — runs dynamic_universe.py weekly (Sun 00:00 UTC)
#
# Run this script ONCE from PowerShell. After it completes, the automation
# runs forever without your intervention.
#
# To unregister later:
#   Unregister-ScheduledTask -TaskName "CryptoDailyCheck" -Confirm:$false
#   Unregister-ScheduledTask -TaskName "CryptoUniverseRefresh" -Confirm:$false
#
# To verify what's registered:
#   Get-ScheduledTask -TaskName "Crypto*"
#
# To test-run a task without waiting for the schedule:
#   Start-ScheduledTask -TaskName "CryptoDailyCheck"
#   Get-Content automation.log -Tail 30
#
# IMPORTANT: this script must be run from the Crypto project directory.

$ErrorActionPreference = "Stop"

# ============================================================================
# Auto-detect paths
# ============================================================================

$here = $PSScriptRoot
if (-not $here) { $here = (Get-Location).Path }
Write-Host "Project directory: $here" -ForegroundColor Cyan

# Find the venv python.exe. Adjust the path below if your venv is elsewhere.
$venvCandidates = @(
    (Join-Path $here ".venv\Scripts\python.exe"),
    "C:\Users\Admin\Documents\Claude\Projects\Trading Strategies\.venv\Scripts\python.exe",
    (Join-Path $here "..\Trading Strategies\.venv\Scripts\python.exe")
)
$venvPython = $null
foreach ($p in $venvCandidates) {
    if (Test-Path $p) { $venvPython = (Resolve-Path $p).Path; break }
}
if (-not $venvPython) {
    Write-Host "ERROR: could not auto-detect venv python.exe." -ForegroundColor Red
    Write-Host "Tried:" -ForegroundColor Red
    foreach ($p in $venvCandidates) { Write-Host "  $p" -ForegroundColor Red }
    Write-Host "Edit setup_automation.ps1 and set venvPython manually." -ForegroundColor Yellow
    exit 1
}
Write-Host "Python interpreter:  $venvPython" -ForegroundColor Cyan

$dailyScript = Join-Path $here "daily_check.py"
$weeklyScript = Join-Path $here "dynamic_universe.py"
foreach ($s in @($dailyScript, $weeklyScript)) {
    if (-not (Test-Path $s)) {
        Write-Host "ERROR: missing script: $s" -ForegroundColor Red
        exit 1
    }
}

# ============================================================================
# Common task settings
# ============================================================================

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# ============================================================================
# Task 1: Daily check (alerter + dashboard)
# ============================================================================

$dailyAction = New-ScheduledTaskAction `
    -Execute $venvPython `
    -Argument "`"$dailyScript`"" `
    -WorkingDirectory $here

# 00:05 UTC daily — adjust StartBoundary for your local timezone if you want
# the task to fire at a specific local time. PowerShell uses local time;
# 00:05 UTC = depends on your zone. Most users want it after the daily close.
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "00:05"

# Unregister if exists, then register fresh
Get-ScheduledTask -TaskName "CryptoDailyCheck" -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

Register-ScheduledTask `
    -TaskName "CryptoDailyCheck" `
    -Action $dailyAction `
    -Trigger $dailyTrigger `
    -Settings $settings `
    -Description "Runs alerter + dashboard for the Donchian crypto strategy" `
    | Out-Null

Write-Host "[OK] Registered CryptoDailyCheck (daily at 00:05)" -ForegroundColor Green

# ============================================================================
# Task 2: Weekly universe refresh (Sunday 00:00)
# ============================================================================

$weeklyAction = New-ScheduledTaskAction `
    -Execute $venvPython `
    -Argument "`"$weeklyScript`"" `
    -WorkingDirectory $here

$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "00:00"

Get-ScheduledTask -TaskName "CryptoUniverseRefresh" -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

Register-ScheduledTask `
    -TaskName "CryptoUniverseRefresh" `
    -Action $weeklyAction `
    -Trigger $weeklyTrigger `
    -Settings $settings `
    -Description "Refreshes active_universe.json based on Binance volume rankings" `
    | Out-Null

Write-Host "[OK] Registered CryptoUniverseRefresh (Sunday at 00:00)" -ForegroundColor Green

# ============================================================================
# Summary
# ============================================================================

Write-Host ""
Write-Host "=" * 70
Write-Host "Automation setup complete." -ForegroundColor Cyan
Write-Host ""
Write-Host "Scheduled tasks registered:"
Write-Host "  CryptoDailyCheck       runs daily at 00:05 (local time)"
Write-Host "    -> python daily_check.py"
Write-Host "    -> runs alerter + dashboard"
Write-Host ""
Write-Host "  CryptoUniverseRefresh  runs weekly Sundays at 00:00 (local time)"
Write-Host "    -> python dynamic_universe.py"
Write-Host "    -> refreshes active_universe.json"
Write-Host ""
Write-Host "To test-run NOW without waiting:" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName 'CryptoDailyCheck'" -ForegroundColor Yellow
Write-Host "  Get-Content automation.log -Tail 50" -ForegroundColor Yellow
Write-Host ""
Write-Host "To see the latest dashboard, open in browser:" -ForegroundColor Yellow
Write-Host "  file:///$($here.Replace('\','/'))/dashboard.html" -ForegroundColor Yellow
Write-Host ""
Write-Host "Reminder: order placement remains MANUAL." -ForegroundColor Yellow
Write-Host "When the alerter sends a Discord/email notification, you place the" -ForegroundColor Yellow
Write-Host "order on Binance manually and update live_trades.csv." -ForegroundColor Yellow
