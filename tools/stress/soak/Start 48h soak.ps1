<#
Starts the 48 hour unattended soak on the show PC, the way the stress plan
wants it: containment ON, blocks of 110 minutes, the scheduled task
registered for 2 days, and the wire witness beside it in companion mode.

BENCH ONLY. It REFUSES unless the person running it states that the flame
hardware is disconnected:

  powershell -NoProfile -ExecutionPolicy Bypass -File "Start 48h soak.ps1" `
      -FlameHardwareDisconnected -Confirmation DISCONNECTED

Other uses:
  ... -Status        print the soak's status and the last block's verdict
  ... -Stop          stop after the block now running, remove the task
  ... -RestartCmd "..."  the restart command for MadMapper and BEYOND
                     between blocks (only when both are installed on this
                     PC; without it the soak runs in fallback mode and
                     counts their commands itself)
  ... -NoWitness     do not start the companion witness

What it changes: %LOCALAPPDATA%\ltcplay\soak\unattended.json (contain true,
block_minutes 110), removes a stale STOP_UNATTENDED and unattended_state,
registers the task "LTC Player unattended soak" (register_soak_task.ps1,
needs an elevated PowerShell) and starts it. The soak itself stops LTC
Player the safe way first and starts it again at the end. Nothing here
arms or fires anything: the soak's flamesafe copy sends to 127.0.0.1 only.
#>
param(
    [switch]$FlameHardwareDisconnected,
    [string]$Confirmation = "",
    [switch]$Status,
    [switch]$Stop,
    [switch]$NoWitness,
    [string]$RestartCmd = "",
    [double]$Days = 2,
    [string]$AppDir = "C:\Program Files\LTC Player"
)
$ErrorActionPreference = "Stop"
$ad = Join-Path $env:LOCALAPPDATA "ltcplay"
$soak = Join-Path $ad "soak"
$logDir = Join-Path $ad "witness"
New-Item -ItemType Directory -Force -Path $soak, $logDir | Out-Null
$log = Join-Path $logDir "soak-start.log"
$task = "LTC Player unattended soak"
$exe = Join-Path $AppDir "ltcplay-soak.exe"
$reg = Join-Path $AppDir "register_soak_task.ps1"

function Say([string]$m) {
    $line = "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))  $m"
    Write-Host $line
    Add-Content -Encoding UTF8 $log $line
}

function Show-Status {
    $p = Join-Path $soak "status.json"
    if (-not (Test-Path $p)) { Say "No status.json yet in $soak (the soak has not started a block)."; return }
    $s = Get-Content -Raw $p | ConvertFrom-Json
    Say ("Soak status: phase '{0}', block {1} started {2}, last verdict {3}, free disk {4} GB, updated {5}" -f $s.phase, $s.block, $s.block_started, $s.last_verdict, $s.free_disk_gb, $s.updated)
    if ($s.last_failures) { Say ("  last failures: " + ($s.last_failures -join "; ")) }
    if ($s.last_error) { Say ("  last error: " + $s.last_error) }
    if ($s.until) { Say ("  runs until: " + [DateTimeOffset]::FromUnixTimeSeconds([long]$s.until).ToLocalTime().ToString("yyyy-MM-dd HH:mm")) }
    $daily = Get-ChildItem (Join-Path $soak "daily") -Filter *.txt -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
    if ($daily) { Say "  daily file $($daily.FullName):"; Get-Content $daily.FullName | Select-Object -Last 6 | ForEach-Object { Say "    $_" } }
    $t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    if ($t) { Say ("  task '{0}': {1}, last result {2}" -f $task, $t.State, (Get-ScheduledTaskInfo -TaskName $task).LastTaskResult) } else { Say "  task '$task' is not registered" }
}

if ($Status) { Show-Status; exit 0 }

if ($Stop) {
    Say "Stopping the unattended soak after the block now running."
    if (Test-Path $exe) { & $exe --unattended-off | ForEach-Object { Say "  $_" } }
    if (Test-Path $reg) { & powershell -NoProfile -ExecutionPolicy Bypass -File $reg -Remove | ForEach-Object { Say "  $_" } }
    Say "Done. The block now running finishes (up to 110 min), then the soak starts LTC Player again."
    exit 0
}

# ---------------------------------------------------------------- refuse ---
if (-not $FlameHardwareDisconnected -or $Confirmation -ne "DISCONNECTED") {
    Say "REFUSED. This soak arms flame groups and plays fire cues through the real programs."
    Say "It may only run with the flame hardware DISCONNECTED (the data cable to the flame node unplugged, gas off)."
    Say "When that is true, run again with:  -FlameHardwareDisconnected -Confirmation DISCONNECTED"
    exit 3
}
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Say "REFUSED. register_soak_task.ps1 needs an elevated PowerShell (right-click PowerShell, Run as administrator), then run this again."
    exit 4
}
if (-not (Test-Path $exe)) { Say "REFUSED. $exe is not there. Install the bench build first (INSTALL.md)."; exit 5 }
if (-not (Test-Path $reg)) { Say "REFUSED. $reg is not there."; exit 5 }
Say "Flame hardware stated DISCONNECTED by the operator. Starting the $Days day soak."
try { Say ("Program: " + (& $exe --version 2>&1 | Select-Object -First 1)) } catch { Say "Program: --version did not answer ($($_.Exception.Message))" }

# The soak refuses to start while anything holds 127.0.0.1:5568; a witness
# in normal mode would. Say so instead of letting the first block fail.
try {
    $holders = Get-NetUDPEndpoint -LocalPort 5568 -ErrorAction SilentlyContinue | ForEach-Object {
        $pr = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        if ($pr) { "$($pr.ProcessName) (pid $($pr.Id))" } }
    $bad = @($holders | Where-Object { $_ -and $_ -notmatch "^flamesafe" })
    if ($bad.Count -gt 0) {
        Say ("REFUSED. Port 5568 is held by: " + ($bad -join ", ") + ". Stop the witness (Stop witness.cmd) and start it again with -Companion after the soak is up.")
        exit 6
    }
} catch { Say "Could not list port 5568 holders ($($_.Exception.Message)); going on." }

if (Get-Process -Name ltcplay-soak -ErrorAction SilentlyContinue) {
    Say "A soak is already running. Use -Status to read it, or -Stop first."
    exit 7
}

# --------------------------------------------------------------- settings ---
$cfgPath = Join-Path $soak "unattended.json"
$cfg = @{}
if (Test-Path $cfgPath) {
    try { (Get-Content -Raw $cfgPath | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value } } catch { $cfg = @{} }
}
$cfg["block_minutes"] = 110
$cfg["keep_days"] = 7
$cfg["max_soak_gb"] = 20
$cfg["contain"] = $true
if ($RestartCmd) { $cfg["restart_cmd"] = $RestartCmd }
if (-not $cfg.ContainsKey("restart_cmd")) { $cfg["restart_cmd"] = "" }
if (-not $cfg.ContainsKey("restart_timeout_min")) { $cfg["restart_timeout_min"] = 20 }
if (-not $cfg.ContainsKey("restart_tries")) { $cfg["restart_tries"] = 2 }
if (-not $cfg.ContainsKey("app_wait_min")) { $cfg["app_wait_min"] = 15 }
if (-not $cfg.ContainsKey("recover_cmds")) { $cfg["recover_cmds"] = @{ BEYOND = ""; MadMapper = "" } }
if (-not $cfg.ContainsKey("recover_after_s")) { $cfg["recover_after_s"] = 30 }
$cfg["show_seconds"] = $null
$cfg["every_min"] = $null
$cfg | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $cfgPath
Say "Wrote $cfgPath (contain true, blocks of 110 min, restart_cmd '$($cfg['restart_cmd'])')."
Say "NOTE: --contain on the command line is ignored by --days; containment comes from this file."

foreach ($n in @("STOP_UNATTENDED", "unattended_state.json")) {
    $p = Join-Path $soak $n
    if (Test-Path $p) { Remove-Item -Force $p; Say "Removed stale $n so the run starts fresh at block 1." }
}

# --------------------------------------------------------------- register ---
Say "Registering the task: $reg -Days $Days"
& powershell -NoProfile -ExecutionPolicy Bypass -File $reg -Days $Days 2>&1 | ForEach-Object { Say "  $_" }
$t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
if (-not $t) { Say "FAILED: the task '$task' is not registered."; exit 8 }
Start-ScheduledTask -TaskName $task
Say "Started the task. The soak stops LTC Player the safe way, then runs block 1 (it waits for the show audio interface; the Scarlett or PreSonus must be plugged in or every show fails to start)."

if (-not $NoWitness) {
    $w = Join-Path (Split-Path (Split-Path $PSScriptRoot)) "stress\witness\witness.ps1"
    if (-not (Test-Path $w)) { $w = Join-Path (Split-Path $PSScriptRoot) "witness\witness.ps1" }
    if (Test-Path $w) {
        Start-Process powershell -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$w`"", "-Companion") -WindowStyle Minimized
        Say "Started the witness in companion mode (processes and disk every 10 s; the soak owns the flame port during stage 1)."
    } else { Say "witness.ps1 not found beside this kit; start it by hand with -Companion." }
    Say "The laser listener (witness\laser_listener.ps1) is not started here: with no BEYOND installed the soak holds BEYOND's OSC port 127.0.0.1:8100 itself and judges the laser commands in its block report, so the listener would refuse. Start it with the witness for stages 2 to 4 (Start laser listener.cmd)."
}

# ------------------------------------------------------------------- wait ---
$deadline = (Get-Date).AddMinutes(4)
$sp = Join-Path $soak "status.json"
$ok = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    if ((Test-Path $sp) -and ((Get-Item $sp).LastWriteTime -gt (Get-Date).AddMinutes(-3))) { $ok = $true; break }
}
if ($ok) { Show-Status } else { Say "status.json was not written within 4 minutes. Check the task (Task Scheduler, '$task') and the soak window." }
Say "Read it later with: -Status. Summary after 48 h: python soak_summary.py (or Collect witness evidence.cmd and run it where Python is)."
exit 0
