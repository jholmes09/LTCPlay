# Starts MadMapper (saved project) and BEYOND (saved lasers timeline) after sign-in, once the ShowNet adapters are up.
# Jeff approved 2026-10-21. Log: start_show_apps.log beside this file.
$log = Join-Path $PSScriptRoot "start_show_apps.log"
function Log($m) { "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m | Out-File $log -Append -Encoding utf8 }
Log "sign-in task started"
$mad = "C:\Program Files\MadMapper 6.1.8\MadMapper.exe"
$proj = "C:\Users\VIOSO\Documents\LTC Shows\video_stress_show\FireIce_video_stress.madproject\FireIce_video_stress.mad"
$bey = "C:\BEYOND55\BEYOND.exe"
# BEYOND reopens its last workspace (BEYOND.ini LastWorkspaceFileName2 = FireIce_lasers.BeyondWorkspace), which brings back the lasers timeline tab; it ignores a file argument.
# wait for both ShowNet addresses
for ($i = 0; $i -lt 60; $i++) {
  $a = Get-NetIPAddress -IPAddress 10.250.1.1, 10.250.2.1 -ErrorAction SilentlyContinue
  if (@($a).Count -ge 2) { break }
  Start-Sleep 2
}
Log ("ShowNet addresses up: " + (@($a).Count))
Start-Sleep 20
if (-not (Get-Process MadMapper -ErrorAction SilentlyContinue)) { Start-Process $mad -ArgumentList ('"' + $proj + '"'); Log "started MadMapper" } else { Log "MadMapper already running" }
Start-Sleep 40
if (-not (Get-Process BEYOND -ErrorAction SilentlyContinue)) { Start-Process $bey; Log "started BEYOND" } else { Log "BEYOND already running" }
Start-Sleep 60
Log ("MadMapper running: " + [bool](Get-Process MadMapper -ErrorAction SilentlyContinue) + "; BEYOND running: " + [bool](Get-Process BEYOND -ErrorAction SilentlyContinue))
