# BENCH ONLY. Registers the unattended soak test as a scheduled task, once.
#
# Run it as the signed-in user, NOT as administrator (2026-11-09: an
# elevated task started an elevated soak, whose deck the user could not end,
# and the task could not be removed without admin):
#   powershell -ExecutionPolicy Bypass -File "C:\Program Files\LTC Player\register_soak_task.ps1"
# Options:
#   -Days 5     soak for five days (the default runs until stopped)
#   -FakeAudio  run with the engine's stand-in audio device (no interface
#               attached): proves the engine, scheduler, flame link,
#               timecode, Art-Net and the witness, NOT the real audio path;
#               every status and report then says AUDIO IS FAKE
#   -AudioDevice "StudioLive"   the interface: resolved to the ONE exact
#               output name the engine opens (ltcplay-soak.exe
#               --resolve-audio), refused when none or two or more match
#   -Remove     remove the task again
#   -StopNow    stop the soak at once (its engine, flamesafe and deck too)
#               and remove the task
#
# The task runs for the signed-in user only while that user is signed in
# (MadMapper, BEYOND, the Scarlett and the Stream Deck need an unlocked
# desktop, so the PC signs in by itself), NOT elevated, so the soak and its
# programs are that user's and that user can end them and remove it. It
# starts at sign-in and again within a minute if the soak program stops
# with an error, up to 999 times. A second copy is never started.
#
# To stop the soak at once: Start menu, "Stop soak now" (or this script
# with -StopNow). "Stop the unattended soak" stops after the block now
# running; then run this script with -Remove.

param([double]$Days = 0, [switch]$Remove, [switch]$StopNow, [switch]$FakeAudio, [string]$AudioDevice = "")

$Name = "LTC Player unattended soak"
$Exe = Join-Path $PSScriptRoot "ltcplay-soak.exe"
if ($StopNow) {
    & $Exe --stop-now --confirm STOP
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    exit 0
}
if ($Remove) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the task '$Name'."
    exit 0
}
if (-not (Test-Path $Exe)) {
    Write-Host "ltcplay-soak.exe is not beside this script ($PSScriptRoot). Nothing was changed."
    exit 1
}
$SoakArgs = if ($Days -gt 0) { "--days $Days" } else { "--forever" }
if ($FakeAudio) { $SoakArgs += " --fake-audio" }
if ($AudioDevice) {
    # The ONE exact name the engine opens, from the soak program itself, so
    # this script and the engine agree (a part of a name matching two
    # outputs, or none, is refused).
    $said = @(& $Exe --resolve-audio $AudioDevice)
    $exact = ($said | Where-Object { $_ -like "EXACT: *" } | Select-Object -First 1)
    if (-not $exact) {
        Write-Host ($said -join " ")
        Write-Host "Nothing was registered."
        exit 2
    }
    $AudioDevice = $exact.Substring(7)
    Write-Host ($said | Where-Object { $_ -notlike "EXACT: *" }) 
    $SoakArgs += " --audio-device `"$AudioDevice`""
}
$User = "$env:USERDOMAIN\$env:USERNAME"
$Action = New-ScheduledTaskAction -Execute $Exe -Argument $SoakArgs -WorkingDirectory $PSScriptRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
$Stop = Join-Path $env:LOCALAPPDATA "ltcplay\soak\STOP_UNATTENDED"
Remove-Item $Stop -ErrorAction SilentlyContinue
if (Test-Path $Stop) { Write-Host "WARNING: $Stop is still there (not removable from this account?); the soak would stop at once. Delete it by hand." }
if ($FakeAudio) { Write-Host "AUDIO IS FAKE for this task: the engine's stand-in device plays the shows. This proves the engine, scheduler, flame link, timecode, Art-Net and the witness, NOT the real audio path." }
Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Principal $Principal `
    -Settings $Settings -Force | Out-Null
Write-Host "Registered '$Name': $Exe $SoakArgs, at sign-in of $User, not elevated (that user can end it and remove it), restarted within a minute after a crash."
Write-Host "Settings: $env:LOCALAPPDATA\ltcplay\soak\unattended.json (written on the first run). Status: $env:LOCALAPPDATA\ltcplay\soak\status.json"
