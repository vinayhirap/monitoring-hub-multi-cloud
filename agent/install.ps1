#requires -Version 5.1
<#
.SYNOPSIS
  CloudOps host agent installer for Windows (Windows Server 2016+/Windows 10+, amd64 or arm64).
.EXAMPLE
  # from the folder that holds the binaries and SHA256SUMS (what `make dist` produces):
  .\install.ps1 -Binary .\cloudops-agent-windows-amd64.exe -Sums .\SHA256SUMS -Url https://hub.example.com -Key cak_...
.EXAMPLE
  .\install.ps1 -BaseUrl https://files.example.com/cloudops-agent -Url https://hub.example.com -Key cak_...
.NOTES
  Must run in an elevated (Administrator) PowerShell. The binary is ALWAYS verified against SHA256SUMS.
  Runs as a scheduled task under SYSTEM (starts at boot, restarts every minute on failure, no time limit); data and
  the token live in %ProgramData%\CloudOpsAgent, ACL'd to SYSTEM + Administrators only.
#>
[CmdletBinding()]
param(
  [string]$BaseUrl = $env:CLOUDOPS_AGENT_BASE_URL,
  [string]$Binary,
  [string]$Sums,
  [string]$Url,
  [string]$Key = $env:CLOUDOPS_AGENT_ENROLL_KEY,
  [switch]$InsecureDev,
  [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
function Die([string]$m) { Write-Host "install.ps1: $m" -ForegroundColor Red; exit 1 }
function Info([string]$m) { Write-Host "==> $m" }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Die 'run in an elevated PowerShell (Run as administrator)' }

$archRaw = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
switch ($archRaw) {
  'AMD64' { $arch = 'amd64' }
  'ARM64' { $arch = 'arm64' }
  default { Die "unsupported architecture: $archRaw" }
}
$Name = "cloudops-agent-windows-$arch.exe"
$TaskName = 'CloudOpsAgent'
$BinDir   = Join-Path $env:ProgramFiles 'CloudOpsAgent'
$DataRoot = Join-Path $env:ProgramData 'CloudOpsAgent'
$DataDir  = Join-Path $DataRoot 'data'
$Exe      = Join-Path $BinDir 'cloudops-agent.exe'
$Cfg      = Join-Path $DataRoot 'agent.json'
$LogFile  = Join-Path $DataDir 'agent.log'

$Tmp = Join-Path ([IO.Path]::GetTempPath()) ("cloudops-agent-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Tmp | Out-Null
try {
  $TmpBin = Join-Path $Tmp $Name
  $TmpSums = Join-Path $Tmp 'SHA256SUMS'
  if ($Binary) {
    if (-not (Test-Path -LiteralPath $Binary)) { Die "no such file: $Binary" }
    if (-not $Sums -or -not (Test-Path -LiteralPath $Sums)) { Die '-Binary also needs -Sums <SHA256SUMS>' }
    Copy-Item -LiteralPath $Binary -Destination $TmpBin
    Copy-Item -LiteralPath $Sums -Destination $TmpSums
  } else {
    if (-not $BaseUrl) { Die 'pass -BaseUrl (or CLOUDOPS_AGENT_BASE_URL), or -Binary/-Sums for a local file' }
    $BaseUrl = $BaseUrl.TrimEnd('/')
    if ($BaseUrl -like 'http://*' -and -not $InsecureDev) { Die 'refusing http:// download; use https or -InsecureDev' }
    if ($BaseUrl -notlike 'http://*' -and $BaseUrl -notlike 'https://*') { Die 'bad -BaseUrl' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Info "downloading $Name"
    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/$Name" -OutFile $TmpBin -TimeoutSec 120
    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/SHA256SUMS" -OutFile $TmpSums -TimeoutSec 30
  }

  Info 'verifying sha256'
  $pattern = '^\s*([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($Name) + '\s*$'
  $expected = $null
  foreach ($line in Get-Content -LiteralPath $TmpSums) { if ($line -match $pattern) { $expected = $Matches[1]; break } }
  if (-not $expected) { Die "$Name not listed in SHA256SUMS" }
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $TmpBin).Hash
  if ($expected -ne $actual) { Die "CHECKSUM MISMATCH for $Name (expected $expected, got $actual); nothing installed" }
  & $TmpBin version | Out-Null
  if ($LASTEXITCODE -ne 0) { Die 'downloaded binary does not run on this host' }

  # Stop a previous install before replacing the binary.
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-Process -Name 'cloudops-agent' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
  }

  Info "installing $Exe"
  # SYSTEM (S-1-5-18) and Administrators (S-1-5-32-544) by SID, so this works on any display language.
  foreach ($d in @($BinDir, $DataRoot, $DataDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    & icacls.exe $d /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { Die "could not restrict permissions on $d" }
  }
  Copy-Item -LiteralPath $TmpBin -Destination $Exe -Force

  if ($Url) {
    if (-not $Key) { Die '-Url given but no -Key (or CLOUDOPS_AGENT_ENROLL_KEY)' }
    Info "enrolling with $Url"
    $env:CLOUDOPS_AGENT_ENROLL_KEY = $Key
    $enrollArgs = @('enroll', '--url', $Url, '--config', $Cfg)
    if ($InsecureDev) { $enrollArgs += '--insecure-dev' }
    & $Exe @enrollArgs
    $code = $LASTEXITCODE
    Remove-Item Env:\CLOUDOPS_AGENT_ENROLL_KEY -ErrorAction SilentlyContinue
    if ($code -ne 0) { Die "enroll failed (exit $code)" }
    & icacls.exe $Cfg /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
  }

  $runArgs = "run --log-file `"$LogFile`""
  if ($InsecureDev) { $runArgs += ' --insecure-dev' }
  $action    = New-ScheduledTaskAction -Execute $Exe -Argument $runArgs
  $trigger   = New-ScheduledTaskTrigger -AtStartup
  $principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
  # A revoked host exits with code 3; Task Scheduler cannot exclude one exit code, so it retries once a minute
  # (one cheap 401 each time) until the agent is uninstalled.
  $settings  = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
               -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew `
               -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'CloudOps host agent' -Force | Out-Null

  if (Test-Path -LiteralPath $Cfg) {
    if (-not $NoStart) {
      Info "starting $TaskName"
      Start-ScheduledTask -TaskName $TaskName
      Start-Sleep -Seconds 3
      $state = (Get-ScheduledTask -TaskName $TaskName).State
      if ($state -ne 'Running') {
        if (Test-Path -LiteralPath $LogFile) { Get-Content -LiteralPath $LogFile -Tail 20 }
        Die "task is '$state', not Running (see $LogFile)"
      }
      Info 'running'
    }
  } else {
    Info 'installed. Not started: this host is not enrolled yet. Run (elevated):'
    Write-Host "    `$env:CLOUDOPS_AGENT_ENROLL_KEY='cak_...'; & '$Exe' enroll --url https://<hub> --config '$Cfg'; Start-ScheduledTask -TaskName $TaskName"
  }
  Info ("done: " + (& $Exe version))
} finally {
  Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue
}
