<#
Starts the 48 hour unattended soak on the show PC, the way the stress plan
wants it: containment ON, blocks of 110 minutes, the scheduled task
registered for 2 days, and the wire witness beside it in companion mode.

BENCH ONLY. It REFUSES unless the person running it states that the flame
hardware is disconnected:

  powershell -NoProfile -ExecutionPolicy Bypass -File "Start 48h soak.ps1" `
      -FlameHardwareDisconnected -Confirmation DISCONNECTED

It also REFUSES (not warns) when:
  - no show audio interface is attached: a soak with no interface plays no
    show and tests almost nothing (2026-10-18: 48 hours, 0 shows). Attach
    the Scarlett or PreSonus, or pass -FakeAudio ON PURPOSE: the engine then
    plays on its stand-in device, which proves the engine, the scheduler,
    the flame link, the timecode, the Art-Net outputs and the witness, NOT
    the real audio path; every status line and the summary then say AUDIO
    IS FAKE, so such a run is never mistaken for a real one;
  - unattended.json carries a restart_cmd whose target file is missing or
    older than the installed MadMapper (a stale click wrapper): it is
    blanked and said so, unless -KeepRestartCmd or a new -RestartCmd "...";
  - this elevated PowerShell is not the signed-in user's (the task and the
    settings folder would be another account's);
  - a stale STOP_UNATTENDED could not be removed (the soak would stop at
    once); every removal is checked, not assumed.

Other uses:
  ... -Status        print the soak's status and the last block's verdict
  ... -Stop          stop after the block now running, remove the task
  ... -AudioDevice "Focusrite USB ASIO"  the interface's exact name
  ... -FakeAudio     run with the engine's stand-in device (see above)
  ... -RestartCmd "..."  the restart command for MadMapper and BEYOND
                     between blocks (only when both are installed on this
                     PC; without it the soak runs in fallback mode and
                     counts their commands itself)
  ... -KeepRestartCmd    keep a restart_cmd this script calls stale
  ... -NoWitness     do not start the companion witness
  ... -SelfTest      prove the checks (audio, restart_cmd, stale files) on
                     a temp folder, then exit

What it changes: <signed-in user>\AppData\Local\ltcplay\soak\unattended.json
(contain true, block_minutes 110, fake_audio, audio_device, restart_cmd),
removes a stale STOP_UNATTENDED and unattended_state, registers the task
"LTC Player unattended soak" (register_soak_task.ps1, needs an elevated
PowerShell) and starts it. The soak itself stops LTC Player the safe way
first and starts it again at the end. Nothing here arms or fires anything:
the soak's flamesafe copy sends to 127.0.0.1 only.
#>
param(
    [switch]$FlameHardwareDisconnected,
    [string]$Confirmation = "",
    [switch]$Status,
    [switch]$Stop,
    [switch]$NoWitness,
    [string]$RestartCmd = "",
    [switch]$KeepRestartCmd,
    [string]$AudioDevice = "",
    [switch]$FakeAudio,
    [switch]$SelfTest,
    [double]$Days = 2,
    [string]$AppDir = "C:\Program Files\LTC Player"
)
$ErrorActionPreference = "Stop"
$task = "LTC Player unattended soak"
# [IO.Path]::Combine, not Join-Path: the self test runs where there is no C: drive.
$exe = [IO.Path]::Combine($AppDir, "ltcplay-soak.exe")
$reg = [IO.Path]::Combine($AppDir, "register_soak_task.ps1")
$script:log = $null

function Say([string]$m) {
    $line = "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))  $m"
    Write-Host $line
    if ($script:log) { try { Add-Content -Encoding UTF8 $script:log $line } catch { } }
}

# ---------------------------------------------------------- the checks ---
# Pure functions first, so -SelfTest can prove them without a show PC.

function Audio-Check([string]$wanted, [string[]]$named, [string[]]$present) {
    # @(ok, sentence). $wanted: -AudioDevice. $named: the names the show
    # file and LTC Player's saved input give. $present: what Windows sees.
    $seen = @($present | Where-Object { $_ })
    $list = "none"
    if ($seen.Count -gt 0) { $list = ($seen -join "; ") }
    if ($wanted) {
        $hit = @($seen | Where-Object { $_.ToLower().Contains($wanted.ToLower()) })
        if ($hit.Count -gt 0) { return @($true, "audio interface '$wanted' is present (Windows sees: $($hit -join '; '))") }
        return @($false, "audio interface '$wanted' (-AudioDevice) is NOT attached. Windows sees: $list")
    }
    foreach ($n in @($named | Where-Object { $_ })) {
        $word = ($n -split "[^A-Za-z0-9]+" | Where-Object { $_.Length -ge 4 } | Select-Object -First 1)
        if (-not $word) { $word = $n }
        $hit = @($seen | Where-Object { $_.ToLower().Contains($word.ToLower()) })
        if ($hit.Count -gt 0) { return @($true, "the show's audio interface '$n' is present (Windows sees: $($hit -join '; '))") }
    }
    $hit = @($seen | Where-Object { $_ -match "(?i)focusrite|scarlett|presonus" })
    if ($hit.Count -gt 0) { return @($true, "a show audio interface is present: $($hit -join '; ')") }
    $want = "none named"
    if (@($named | Where-Object { $_ }).Count -gt 0) { $want = (@($named | Where-Object { $_ }) -join "; ") }
    return @($false, "NO show audio interface is attached (looked for: $want; a Focusrite, Scarlett or PreSonus). Windows sees: $list")
}

function Restart-Target([string]$cmd) {
    # The script or program a restart_cmd runs: the first path ending in
    # .ps1, .cmd, .bat or .exe, quoted or not; "" when there is none.
    if (-not $cmd) { return "" }
    $m = [regex]::Match($cmd, '"([^"]+\.(ps1|cmd|bat|exe))"')
    if ($m.Success) { return $m.Groups[1].Value }
    $m = [regex]::Match($cmd, "'([^']+\.(ps1|cmd|bat|exe))'")
    if ($m.Success) { return $m.Groups[1].Value }
    $m = [regex]::Match($cmd, '(?i)((?:[A-Za-z]:\\|/)[^\s"]+\.(ps1|cmd|bat|exe))')
    if ($m.Success) { return $m.Groups[1].Value }
    ""
}

function RestartCmd-Stale([string]$cmd, [string]$target, [string]$mmVersion, $mmTime) {
    # Why a restart_cmd is stale, as a list of sentences; empty when it is
    # not. $mmVersion and $mmTime: the installed MadMapper's version and
    # file time ("" / $null when MadMapper is not installed).
    $why = @()
    if (-not $cmd) { return $why }
    if (-not $target) { return @("its target file could not be read out of the command '$cmd'") }
    if (-not (Test-Path $target)) { return @("its target $target does not exist") }
    $text = ""
    try { $text = Get-Content -Raw $target -ErrorAction Stop } catch { $text = "" }
    if ($mmVersion) {
        $vers = @([regex]::Matches($text, '\b\d+\.\d+\.\d+\b') | ForEach-Object { $_.Value } | Sort-Object -Unique)
        $other = @($vers | Where-Object { $_ -ne $mmVersion })
        if ($other.Count -gt 0 -and -not ($vers -contains $mmVersion)) {
            $why += "it names version $($other -join ', ') and the installed MadMapper is $mmVersion"
        }
    }
    if ($null -ne $mmTime) {
        $t = (Get-Item $target).LastWriteTime
        if ($t -lt $mmTime) { $why += "it was written $($t.ToString('yyyy-MM-dd')) and the installed MadMapper is newer ($($mmTime.ToString('yyyy-MM-dd')))" }
    }
    $why
}

function Remove-StaleFiles([string]$soakDir) {
    # STOP_UNATTENDED and unattended_state.json gone, CHECKED: a STOP file
    # that survives makes the soak stop at once (one from Oct 8 did).
    # @(ok, sentences).
    $out = @()
    $ok = $true
    foreach ($n in @("STOP_UNATTENDED", "unattended_state.json")) {
        $p = Join-Path $soakDir $n
        if (-not (Test-Path $p)) { continue }
        try {
            $f = Get-Item $p -Force
            if ($f.Attributes -band [IO.FileAttributes]::ReadOnly) { $f.Attributes = $f.Attributes -bxor [IO.FileAttributes]::ReadOnly }
            Remove-Item -Force -LiteralPath $p -ErrorAction Stop
        } catch {
            $out += "COULD NOT remove $p ($($_.Exception.Message))"
        }
        if (Test-Path $p) {
            $ok = $false
            $owner = "?"
            try { $owner = (Get-Acl $p).Owner } catch { }
            $out += "stale $n is STILL THERE at $p (owner $owner, written $((Get-Item $p -Force).LastWriteTime)); the soak would stop at once"
        } else {
            $out += "removed stale $n ($p)"
        }
    }
    @($ok, $out)
}

function MadMapper-Installed {
    # @(version, exe time) of the installed MadMapper, or @("", $null).
    try {
        $exe = Get-ChildItem "C:\Program Files\MadMapper*" -Filter "MadMapper*.exe" -Recurse -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($exe) {
            $v = $exe.VersionInfo.ProductVersion
            if (-not $v) { $v = $exe.VersionInfo.FileVersion }
            $m = [regex]::Match([string]$v, '\d+\.\d+\.\d+')
            if ($m.Success) { $v = $m.Value }
            return @([string]$v, $exe.LastWriteTime)
        }
    } catch { }
    try {
        foreach ($k in @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*", "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*")) {
            $e = Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "MadMapper*" } | Select-Object -First 1
            if ($e) {
                $t = $null
                if ($e.InstallDate) { try { $t = [datetime]::ParseExact([string]$e.InstallDate, "yyyyMMdd", $null) } catch { } }
                return @([string]$e.DisplayVersion, $t)
            }
        }
    } catch { }
    @("", $null)
}

function Present-AudioOutputs {
    $names = @()
    try { $names += @(Get-PnpDevice -Class AudioEndpoint -PresentOnly -Status OK -ErrorAction SilentlyContinue | ForEach-Object { $_.FriendlyName }) } catch { }
    try { $names += @(Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq "OK" } | ForEach-Object { $_.Name }) } catch { }
    @($names | Where-Object { $_ } | Sort-Object -Unique)
}

function Named-AudioDevices([string]$ad) {
    # The show file's clock.audio.device and LTC Player's saved input.
    $names = @()
    try {
        $show = (Get-Content -Raw (Join-Path $ad "showpc.json") | ConvertFrom-Json).show_folder
        foreach ($x in @(Get-ChildItem $show -Filter *.json -ErrorAction SilentlyContinue | Select-Object -First 8)) {
            try { $d = Get-Content -Raw $x.FullName | ConvertFrom-Json; if ($d.clock -and $d.clock.audio -and $d.clock.audio.device) { $names += [string]$d.clock.audio.device } } catch { }
        }
    } catch { }
    try { $d = Get-Content -Raw (Join-Path $ad "ltcplay_input.json") | ConvertFrom-Json; if ($d.device) { $names += [string]$d.device } } catch { }
    @($names | Where-Object { $_ } | Sort-Object -Unique)
}

function Console-User {
    # DOMAIN\user signed in at the console, "" when unknown.
    try { $u = (Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).UserName; if ($u) { return [string]$u } } catch { }
    ""
}

function Profile-LocalAppData([string]$user) {
    # The LocalAppData folder of DOMAIN\user, "" when it cannot be found.
    $name = ($user -split "\\")[-1]
    try {
        foreach ($k in @(Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList" -ErrorAction SilentlyContinue)) {
            $p = (Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue).ProfileImagePath
            if ($p -and ((Split-Path -Leaf $p) -ieq $name)) { return (Join-Path $p "AppData\Local") }
        }
    } catch { }
    ""
}

# ------------------------------------------------------------ self test ---
function Run-SelfTest {
    $d = Join-Path ([IO.Path]::GetTempPath()) ("soak-start-selftest-" + (Get-Date).ToString("HHmmssfff"))
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    $fails = @()
    try {
        # stale files: present, then gone, and said so
        Set-Content (Join-Path $d "STOP_UNATTENDED") "stop"
        Set-Content (Join-Path $d "unattended_state.json") "{}"
        $r = Remove-StaleFiles $d
        if (-not $r[0] -or (Test-Path (Join-Path $d "STOP_UNATTENDED")) -or (Test-Path (Join-Path $d "unattended_state.json"))) { $fails += "stale files not removed: $($r[1] -join ' | ')" }
        if (-not (($r[1] -join " ") -like "*removed stale STOP_UNATTENDED*")) { $fails += "removal not reported: $($r[1] -join ' | ')" }
        $r = Remove-StaleFiles $d
        if (-not $r[0] -or @($r[1]).Count -ne 0) { $fails += "a clean folder is not clean: $($r[1] -join ' | ')" }
        # audio
        $r = Audio-Check "" @("Focusrite USB ASIO") @("Speakers (Realtek(R) Audio)", "Focusrite USB (Focusrite USB Audio)")
        if (-not $r[0]) { $fails += "the show's interface present was refused: $($r[1])" }
        $r = Audio-Check "" @("Focusrite USB ASIO") @("Speakers (Realtek(R) Audio)")
        if ($r[0]) { $fails += "no interface was accepted: $($r[1])" }
        if (-not ($r[1] -like "NO show audio interface*")) { $fails += "refusal wording: $($r[1])" }
        $r = Audio-Check "" @() @("Speakers (Realtek(R) Audio)", "PreSonus AudioBox")
        if (-not $r[0]) { $fails += "a PreSonus present was refused: $($r[1])" }
        $r = Audio-Check "Scarlett" @() @("Speakers (Realtek(R) Audio)")
        if ($r[0]) { $fails += "-AudioDevice not attached was accepted" }
        $r = Audio-Check "Scarlett" @() @("Scarlett 2i2 USB")
        if (-not $r[0]) { $fails += "-AudioDevice attached was refused" }
        $r = Audio-Check "" @() @()
        if ($r[0]) { $fails += "an empty device list was accepted" }
        # restart_cmd
        $w = Join-Path $d "restart_apps.ps1"
        Set-Content $w "# restarts MadMapper Demo 6.1.5 and BEYOND`nStart-Process 'C:\Program Files\MadMapper 6.1.5\MadMapper.exe'"
        (Get-Item $w).LastWriteTime = (Get-Date).AddDays(-30)
        $cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$w`""
        if ((Restart-Target $cmd) -ne $w) { $fails += "target not read from a quoted command: '$(Restart-Target $cmd)'" }
        if ((Restart-Target "powershell -File $w") -ne $w) { $fails += "target not read from a bare command" }
        $why = @(RestartCmd-Stale $cmd $w "6.1.8" (Get-Date))
        if ($why.Count -lt 2) { $fails += "an old wrapper naming 6.1.5 against 6.1.8 was not called stale: $($why -join ' | ')" }
        $why = @(RestartCmd-Stale $cmd (Join-Path $d "missing.ps1") "6.1.8" (Get-Date))
        if ($why.Count -ne 1 -or -not ($why[0] -like "*does not exist*")) { $fails += "a missing target was not called stale: $($why -join ' | ')" }
        Set-Content $w "# restarts MadMapper 6.1.8 and BEYOND"
        $why = @(RestartCmd-Stale $cmd $w "6.1.8" (Get-Date).AddDays(-1))
        if ($why.Count -ne 0) { $fails += "a current wrapper was called stale: $($why -join ' | ')" }
        $why = @(RestartCmd-Stale "" "" "6.1.8" (Get-Date))
        if ($why.Count -ne 0) { $fails += "an empty restart_cmd was called stale" }
        $why = @(RestartCmd-Stale $cmd $w "" $null)
        if ($why.Count -ne 0) { $fails += "with no MadMapper installed a present wrapper was called stale: $($why -join ' | ')" }
    } finally {
        Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue
    }
    if ($fails.Count -eq 0) { Write-Host "self-test passed: stale files are removed and checked, a missing interface refuses, -AudioDevice and a present interface pass, a stale or missing restart_cmd is caught"; return 0 }
    foreach ($f in $fails) { Write-Host "self-test FAILED: $f" }
    return 1
}

if ($SelfTest) { exit (Run-SelfTest) }

# ---------------------------------------------------- the settings folder ---
# The task runs as the SIGNED-IN user and the soak reads that user's
# AppData\Local\ltcplay\soak. An elevated PowerShell opened as another
# account would write another folder and register the task for the wrong
# account (a stale STOP_UNATTENDED from Oct 8 survived exactly such a run).
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$console = Console-User
$ad = Join-Path $env:LOCALAPPDATA "ltcplay"
if ($console -and ($console -ne $me)) {
    $other = Profile-LocalAppData $console
    Write-Host "This PowerShell runs as $me but the signed-in user is $console."
    if (-not $Status) {
        Write-Host "REFUSED. The soak task runs as the signed-in user and reads $other\ltcplay\soak, not $ad\soak. Open PowerShell as $console (right-click, Run as administrator, in that user's own session) and run this again."
        exit 10
    }
    if ($other) { $ad = Join-Path $other "ltcplay" }
}
$soak = Join-Path $ad "soak"
$logDir = Join-Path $ad "witness"
New-Item -ItemType Directory -Force -Path $soak, $logDir | Out-Null
$script:log = Join-Path $logDir "soak-start.log"

function Show-Status {
    $p = Join-Path $soak "status.json"
    if (-not (Test-Path $p)) { Say "No status.json yet in $soak (the soak has not started a block)."; return }
    $s = Get-Content -Raw $p | ConvertFrom-Json
    Say ("Soak status: phase '{0}', block {1} started {2}, last verdict {3}, free disk {4} GB, updated {5}" -f $s.phase, $s.block, $s.block_started, $s.last_verdict, $s.free_disk_gb, $s.updated)
    if ($s.audio) { Say ("  " + $s.audio) } else { Say "  audio: not stated by this soak build (older than the fake_audio setting)" }
    if ($s.last_failures) { Say ("  last failures: " + ($s.last_failures -join "; ")) }
    if ($s.last_error) { Say ("  last error: " + $s.last_error) }
    if ($s.until) { Say ("  runs until: " + [DateTimeOffset]::FromUnixTimeSeconds([long]$s.until).ToLocalTime().ToString("yyyy-MM-dd HH:mm")) }
    $daily = Get-ChildItem (Join-Path $soak "daily") -Filter *.txt -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
    if ($daily) { Say "  daily file $($daily.FullName):"; Get-Content $daily.FullName | Select-Object -Last 6 | ForEach-Object { Say "    $_" } }
    $t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    if ($t) { Say ("  task '{0}': {1}, last result {2}" -f $task, $t.State, (Get-ScheduledTaskInfo -TaskName $task).LastTaskResult) } else { Say "  task '$task' is not registered" }
    $cfgP = Join-Path $soak "unattended.json"
    if (Test-Path $cfgP) {
        try { $c = Get-Content -Raw $cfgP | ConvertFrom-Json; if ($c.fake_audio) { Say "  unattended.json: fake_audio TRUE. AUDIO IS FAKE: this run does not test the real audio path." } } catch { }
    }
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
Say "Flame hardware stated DISCONNECTED by the operator. Starting the $Days day soak as $me (signed in: $console)."
try { Say ("Program: " + (& $exe --version 2>&1 | Select-Object -First 1)) } catch { Say "Program: --version did not answer ($($_.Exception.Message))" }

# ---------------------------------------------------------------- audio ---
# A soak with no show audio interface plays no show (2026-10-18: 48 hours,
# 0 shows, 'Show audio: NO interface found'). Refuse, unless fake audio is
# asked for on purpose.
$present = Present-AudioOutputs
$named = Named-AudioDevices $ad
$ac = Audio-Check $AudioDevice $named $present
if ($ac[0]) {
    Say ("Audio: " + $ac[1])
    if ($FakeAudio) { Say "AUDIO IS FAKE anyway (-FakeAudio): the engine plays the shows on its stand-in device, NOT the interface. This run proves the engine, the scheduler, the flame link, the timecode, the Art-Net outputs and the witness, not the real audio path." }
} elseif ($FakeAudio) {
    Say ("Audio: " + $ac[1])
    Say "AUDIO IS FAKE (-FakeAudio): the engine plays the shows on its stand-in device. This run proves the engine, the scheduler, the flame link, the timecode, the Art-Net outputs and the witness, NOT the real audio path. The status, the daily lines and the summary all say so; it is not a show-night soak."
} else {
    Say ("REFUSED. " + $ac[1])
    Say "A soak with no interface plays no show and tests almost nothing. Attach the Scarlett or PreSonus and run again (name it with -AudioDevice 'part of its name' if it is not found), or, ON PURPOSE and knowing the limit, add -FakeAudio: the engine's stand-in device then plays the shows, which proves the engine, the scheduler, the flame link, the timecode, the Art-Net outputs and the witness, NOT the real audio path."
    exit 11
}

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
$cfg["fake_audio"] = [bool]$FakeAudio
$cfg["audio_device"] = $AudioDevice

# restart_cmd: a kept one is checked against the installed MadMapper. A
# stale click wrapper (MadMapper Demo 6.1.5 while 6.1.8 licensed is
# installed) is blanked, loudly, unless told otherwise.
$mm = MadMapper-Installed
if ($mm[0]) { Say "MadMapper installed: version $($mm[0]) ($($mm[1]))" } else { Say "MadMapper: not found under C:\Program Files (fallback mode: the soak counts its commands itself)" }
$kept = ""
if ($cfg.ContainsKey("restart_cmd") -and $cfg["restart_cmd"]) { $kept = [string]$cfg["restart_cmd"] }
if ($RestartCmd) {
    $why = @(RestartCmd-Stale $RestartCmd (Restart-Target $RestartCmd) $mm[0] $mm[1])
    if ($why.Count -gt 0) { Say ("WARNING: the -RestartCmd given looks stale: " + ($why -join "; ") + ". Used anyway because you named it.") }
    $cfg["restart_cmd"] = $RestartCmd
    Say "restart_cmd: '$RestartCmd' (from -RestartCmd)"
} elseif ($kept) {
    $why = @(RestartCmd-Stale $kept (Restart-Target $kept) $mm[0] $mm[1])
    if ($why.Count -gt 0) {
        Say "WARNING: unattended.json carries a STALE restart_cmd: '$kept'"
        foreach ($w in $why) { Say "  because $w" }
        if ($KeepRestartCmd) {
            Say "  kept anyway (-KeepRestartCmd). If it starts the wrong program the block waits $($cfg['restart_timeout_min']) min and says so."
            $cfg["restart_cmd"] = $kept
        } else {
            Say "  BLANKED: this script writes its own unattended.json, so the soak runs in fallback mode (it counts MadMapper's and BEYOND's commands itself, no restarts between blocks). To keep it, run again with -KeepRestartCmd; for a new one, -RestartCmd '<command>'."
            $cfg["restart_cmd"] = ""
        }
    } else {
        Say "restart_cmd kept from unattended.json: '$kept' (its target is present and matches the installed MadMapper)"
        $cfg["restart_cmd"] = $kept
    }
} else {
    $cfg["restart_cmd"] = ""
    Say "restart_cmd: none (fallback mode: no MadMapper or BEYOND restarts between blocks; the soak counts their commands itself)"
}
if (-not $cfg.ContainsKey("restart_timeout_min")) { $cfg["restart_timeout_min"] = 20 }
if (-not $cfg.ContainsKey("restart_tries")) { $cfg["restart_tries"] = 2 }
if (-not $cfg.ContainsKey("app_wait_min")) { $cfg["app_wait_min"] = 15 }
if (-not $cfg.ContainsKey("recover_cmds")) { $cfg["recover_cmds"] = @{ BEYOND = ""; MadMapper = "" } }
if (-not $cfg.ContainsKey("recover_after_s")) { $cfg["recover_after_s"] = 30 }
$cfg["show_seconds"] = $null
$cfg["every_min"] = $null
$cfg | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $cfgPath
Say "Wrote $cfgPath (contain true, blocks of 110 min, fake_audio $($cfg['fake_audio']), audio_device '$AudioDevice', restart_cmd '$($cfg['restart_cmd'])')."
Say "NOTE: --contain on the command line is ignored by --days; containment comes from this file."

# ------------------------------------------------------------ stale files ---
$rm = Remove-StaleFiles $soak
foreach ($s in @($rm[1])) { Say $s }
if (-not $rm[0]) {
    Say "REFUSED. A stale STOP_UNATTENDED (or state file) could not be removed from $soak, so the soak would stop at once (or resume an old run). Delete it by hand, from the signed-in user's own elevated PowerShell, and run again."
    exit 9
}

# --------------------------------------------------------------- register ---
$regArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $reg, "-Days", $Days)
if ($FakeAudio) { $regArgs += "-FakeAudio" }
if ($AudioDevice) { $regArgs += @("-AudioDevice", $AudioDevice) }
Say "Registering the task: $reg -Days $Days$(if ($FakeAudio) { ' -FakeAudio' })$(if ($AudioDevice) { " -AudioDevice '$AudioDevice'" })"
& powershell @regArgs 2>&1 | ForEach-Object { Say "  $_" }
$t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
if (-not $t) { Say "FAILED: the task '$task' is not registered."; exit 8 }
if (Test-Path (Join-Path $soak "STOP_UNATTENDED")) { Say "FAILED: STOP_UNATTENDED is back in $soak after registering; the soak would stop at once. Nothing was started."; exit 9 }
Start-ScheduledTask -TaskName $task
Say "Started the task. The soak stops LTC Player the safe way, then runs block 1 (it waits for the show audio interface; the Scarlett or PreSonus must be plugged in or every show fails to start)."
if ($FakeAudio) { Say "Remember: AUDIO IS FAKE for this run. Its status, daily lines and summary say so; it is not a show-night soak." }

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
