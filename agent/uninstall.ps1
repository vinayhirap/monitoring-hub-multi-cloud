#requires -Version 5.1
<#
.SYNOPSIS
  Remove the CloudOps host agent from Windows. -Purge also deletes config/token, queue and logs.
  This does NOT revoke the host on the hub; do that in the CloudOps UI (hostagents.manage).
#>
[CmdletBinding()]
param([switch]$Purge)
$ErrorActionPreference = 'Stop'
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host 'run in an elevated PowerShell (Run as administrator)' -ForegroundColor Red; exit 1 }
$TaskName = 'CloudOpsAgent'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Get-Process -Name 'cloudops-agent' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Remove-Item -LiteralPath (Join-Path $env:ProgramFiles 'CloudOpsAgent') -Recurse -Force -ErrorAction SilentlyContinue
$DataRoot = Join-Path $env:ProgramData 'CloudOpsAgent'
if ($Purge) {
  Remove-Item -LiteralPath $DataRoot -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host 'purged config, token, queue and logs.'
} else {
  Write-Host "kept $DataRoot (token, queue, log). Use -Purge to delete it."
}
Write-Host 'Reminder: revoke this host on the hub so the token cannot be reused.'
