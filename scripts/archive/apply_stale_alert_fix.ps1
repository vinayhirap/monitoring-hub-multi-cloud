# apply_stale_alert_fix.ps1
# Run this from the root of monitoring-hub-V4-cost-optimized (same folder as app/, db/)
# It merges the fixed files into place and runs the one-time DB cleanup.

$ErrorActionPreference = "Stop"

Write-Host "==> Merging fixed files into project..." -ForegroundColor Cyan

$scriptDir = $PSScriptRoot
robocopy "$scriptDir\app" "app" /E
robocopy "$scriptDir\db"  "db"  /E

Write-Host "==> Files merged." -ForegroundColor Green

Write-Host "==> Running one-time DB cleanup (purges stale account data)..." -ForegroundColor Cyan
Write-Host "    You will be prompted for the MySQL root/monitor password if required."

mysql -umonitor -proot123 monitoring_hub -e "source db/migrations/007_purge_stale_account_data.sql"

Write-Host "==> Done. Restart your backend for the alert_evaluator/alerts.py changes to take effect." -ForegroundColor Green
