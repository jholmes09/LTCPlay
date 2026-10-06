# BENCH ONLY. Registers the unattended soak test as a scheduled task, once.
#
# Run it as administrator (right-click, Run with PowerShell as
# administrator, or from an elevated PowerShell):
#   powershell -ExecutionPolicy Bypass -File "C:\Program Files\LTC Player\register_soak_task.ps1"
# Options:
#   -Days 5     soak for five days (the default runs until stopped)
#   -Remove     remove the task again
#
# The task runs for the signed-in user only while that user is signed in
# (MadMapper, BEYOND, the Scarlett and the Stream Deck need an unlocked
# desktop, so the PC signs in by itself), with highest privileges. It
# starts at sign-in and again within a minute if the soak program stops
# with an error, up to 999 times. A second copy is never started.
#
# To stop the soak: Start menu, "Stop the unattended soak" (it stops after
# the block now running), then run this script with -Remove.

param([double]$Days = 0, [switch]$Remove)

$Name = "LTC Player unattended soak"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the task '$Name'."
    exit 0
}
$Exe = Join-Path $PSScriptRoot "ltcplay-soak.exe"
if (-not (Test-Path $Exe)) {
    Write-Host "ltcplay-soak.exe is not beside this script ($PSScriptRoot). Nothing was changed."
    exit 1
}
$SoakArgs = if ($Days -gt 0) { "--days $Days" } else { "--forever" }
$User = "$env:USERDOMAIN\$env:USERNAME"
$Action = New-ScheduledTaskAction -Execute $Exe -Argument $SoakArgs -WorkingDirectory $PSScriptRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
$Stop = Join-Path $env:LOCALAPPDATA "ltcplay\soak\STOP_UNATTENDED"
Remove-Item $Stop -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Principal $Principal `
    -Settings $Settings -Force | Out-Null
Write-Host "Registered '$Name': $Exe $SoakArgs, at sign-in of $User, highest privileges, restarted within a minute after a crash."
Write-Host "Settings: $env:LOCALAPPDATA\ltcplay\soak\unattended.json (written on the first run). Status: $env:LOCALAPPDATA\ltcplay\soak\status.json"
