<#
Break-it: faults caused on purpose, one at a time, each logged with what
was seen before, what was done, what the programs did, and how long the
recovery took. Safe and reversible: every step has an undo, and the script
takes a backup before it touches a file.

BENCH ONLY. It REFUSES unless the flame hardware is stated disconnected,
and it refuses to break anything while a show is running or starting
(except the two purge steps, which need the purge held on purpose).

  powershell -NoProfile -ExecutionPolicy Bypass -File breakit.ps1 -List
  ... -Steps 1,2,3 -FlameHardwareDisconnected -Confirmation DISCONNECTED
  ... -All ...            every step this install can do (skips the ones
                          that need the Morning page build or hardware)
  ... -Undo               undo whatever is still broken (files, devices,
                          adapter, fill), from the undo list on disk
  ... -DryRun             say what each step would do, change nothing

Options for steps that need a name:
  -Adapter "Ethernet 2"       step 6: the show network adapter to drop.
                              NEVER the one your remote session uses.
  -AudioDevice "Scarlett"     step 5: part of the interface's device name
  -Critical                   step 7: fill to under 1 GB free, not under 5

Logs: %LOCALAPPDATA%\ltcplay\witness\breakit.log (every line) and
breakit-results.csv (one row per step). The expected results are in
BREAK-IT TABLE.md beside this script. Run the wire witness at the same time:
the table says what it must show.
#>
param(
    [int[]]$Steps,
    [switch]$List,
    [switch]$All,
    [switch]$Undo,
    [switch]$DryRun,
    [switch]$FlameHardwareDisconnected,
    [string]$Confirmation = "",
    [string]$Adapter = "",
    [string]$AudioDevice = "",
    [switch]$Critical,
    [int]$Port = 7878,
    [string]$AppDir = "C:\Program Files\LTC Player"
)
$ErrorActionPreference = "Continue"
$ad = Join-Path $env:LOCALAPPDATA "ltcplay"
$wdir = Join-Path $ad "witness"
New-Item -ItemType Directory -Force -Path $wdir | Out-Null
$LOG = Join-Path $wdir "breakit.log"
$RESULTS = Join-Path $wdir "breakit-results.csv"
$UNDO_FILE = Join-Path $wdir "breakit-undo.json"
$BACKUPS = Join-Path $wdir "breakit-backups"
$FILL = Join-Path $wdir "fill"
$BASE = "http://127.0.0.1:$Port"

function Say([string]$m) {
    $line = "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff'))  $m"
    Write-Host $line
    Add-Content -Encoding UTF8 $LOG $line
}

function Result([hashtable]$r) {
    if (-not (Test-Path $RESULTS)) { "step,name,started,action,expected,observed,recovered_s,result" | Set-Content -Encoding UTF8 $RESULTS }
    $q = { param($s) '"' + ([string]$s).Replace('"', '""') + '"' }
    (@($r.step, $r.name, $r.started, $r.action, $r.expected, $r.observed, $r.recovered_s, $r.result) | ForEach-Object { & $q $_ }) -join "," | Add-Content -Encoding UTF8 $RESULTS
}

function Http([string]$method, [string]$path, $body = $null) {
    try {
        $p = @{ Uri = "$BASE$path"; Method = $method; TimeoutSec = 5; UseBasicParsing = $true }
        if ($null -ne $body) { $p["Body"] = ($body | ConvertTo-Json -Compress); $p["ContentType"] = "application/json" }
        $r = Invoke-WebRequest @p
        return @{ code = [int]$r.StatusCode; body = ($r.Content | ConvertFrom-Json) }
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        return @{ code = $code; body = $null; error = $_.Exception.Message }
    }
}

function Alive([string]$name) { [bool](Get-Process -Name $name -ErrorAction SilentlyContinue) }

function Snapshot() {
    $st = Http "GET" "/api/state"
    $rs = Http "GET" "/api/remote/status"
    $alerts = @()
    if ($rs.body -and $rs.body.alerts) { $alerts = @($rs.body.alerts | ForEach-Object { if ($_.text) { $_.text } else { $_ | ConvertTo-Json -Compress } }) }
    $fs = ""
    $fsLog = Join-Path $ad "logs\flamesafe.log"
    if (Test-Path $fsLog) { $fs = (Get-Content $fsLog -Tail 3 -ErrorAction SilentlyContinue) -join " | " }
    $night = ""
    $n = Get-ChildItem (Join-Path $ad "nights") -Filter "night_*.journal.txt" -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
    if ($n) { $night = (Get-Content $n.FullName -Tail 3 -ErrorAction SilentlyContinue) -join " | " }
    @{ engine = ($st.code -eq 200); running = [bool]($st.body -and ($st.body.running -or $st.body.starting))
       flamesafe = (Alive "flamesafe"); deck = (Alive "ltcplay-deck"); supervisor = (Alive "LTC Player")
       alerts = $alerts; fs = $fs; night = $night; served_at = $(if ($rs.body) { $rs.body.served_at } else { $null }) }
}

function Describe($s) {
    "engine answers=$($s.engine) show running=$($s.running) flamesafe=$($s.flamesafe) deck=$($s.deck) supervisor=$($s.supervisor); banners: " +
    $(if ($s.alerts.Count) { $s.alerts -join " || " } else { "none" }) + "; flamesafe tail: $($s.fs); journal tail: $($s.night)"
}

function Wait-Until([scriptblock]$cond, [int]$maxS) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $maxS) {
        if (& $cond) { return [math]::Round($sw.Elapsed.TotalSeconds, 1) }
        Start-Sleep -Milliseconds 500
    }
    return -1
}

function Load-Undo() { if (Test-Path $UNDO_FILE) { try { return @(Get-Content -Raw $UNDO_FILE | ConvertFrom-Json) } catch { } } ; return @() }
function Save-Undo($list) { ConvertTo-Json -Depth 4 @($list) | Set-Content -Encoding UTF8 $UNDO_FILE }
function Add-Undo([hashtable]$u) { $l = @(Load-Undo); $l += [pscustomobject]$u; Save-Undo $l }

function Backup([string]$path) {
    New-Item -ItemType Directory -Force -Path $BACKUPS | Out-Null
    $to = Join-Path $BACKUPS ("{0}.{1}.bak" -f (Split-Path -Leaf $path), (Get-Date).ToString("yyyyMMdd_HHmmss"))
    Copy-Item $path $to -Force
    Add-Undo @{ kind = "file"; path = $path; backup = $to }
    Say "  backed up $path to $to"
    $to
}

function Find-StreamDeck() { if (-not (Get-Command Get-PnpDevice -ErrorAction SilentlyContinue)) { return $null }; Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -match "VID_0FD9&PID_0063" } | Select-Object -First 1 }
function Find-Audio([string]$part) {
    if (-not $part) {
        try {
            $show = (Get-Content -Raw (Join-Path $ad "showpc.json") | ConvertFrom-Json).show_folder
            $f = Get-ChildItem $show -Filter *.json -ErrorAction SilentlyContinue | Select-Object -First 5
            foreach ($x in $f) { $d = Get-Content -Raw $x.FullName | ConvertFrom-Json; if ($d.clock -and $d.clock.audio -and $d.clock.audio.device) { $part = [string]$d.clock.audio.device; break } }
        } catch { }
    }
    if (-not $part) { return $null }
    if (-not (Get-Command Get-PnpDevice -ErrorAction SilentlyContinue)) { return $null }
    $words = ($part -split "[^A-Za-z0-9]+" | Where-Object { $_.Length -ge 4 } | Select-Object -First 1)
    if (-not $words) { $words = $part }
    Get-PnpDevice -PresentOnly -Class MEDIA,USB,AudioEndpoint -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -like "*$words*" } | Select-Object -First 1
}

$morning = Test-Path (Join-Path $ad "ltcplay_morning.json")
$lockout = Join-Path $ad "ltcplay_lockout.json"

# ------------------------------------------------------------------ steps ---
$STEP_LIST = @(
    @{ id = 1; name = "Kill the engine (ltcplay.exe, hard)"; needs = ""
       expect = "flamesafe journal 'link: show program stopped answering: every group disarmed' within 0.5 s; its sACN keeps flowing, all zeros (witness: no stall, no nonzero); supervisor log 'ltcplay.exe stopped by itself ... starting it again in 2 s'; engine answers again within 60 s; rack screen reconnects; a new 'ltcplay started' journal line"
       do = { Stop-Process -Name ltcplay -Force; "Stop-Process ltcplay" }
       recover = { (Http "GET" "/api/state").code -eq 200 } },
    @{ id = 2; name = "Kill flamesafe (flamesafe.exe, hard: no safe zeros, the End task hazard)"; needs = ""
       expect = "witness: STREAM STOPPED (the last packet was zeros because nothing was armed; had anything been armed it would read STREAM STOPPED WITH NONZERO ON THE WIRE, which is exactly why hardware is disconnected); supervisor restarts it within 2 to 60 s; witness STREAM BACK with SENDER CHANGED; flamesafe journal 'start'; flames chip or banner red while it was gone; every group disarmed, a fresh arm cycle needed"
       do = { Stop-Process -Name flamesafe -Force; "Stop-Process flamesafe" }
       recover = { Alive "flamesafe" } },
    @{ id = 3; name = "Kill the deck program (ltcplay-deck.exe: the PROCESS, not the USB cable)"; needs = ""
       expect = "flamesafe journal 'arm-input: arm input stale' within 0.5 s and every group disarmed (witness: safety slots fall to 0 if any were armed); supervisor restarts the deck within 2 to 60 s; the Stream Deck's keys redraw; no DECK_LOST banner (that comes from the deck process itself, which is the thing that died)"
       do = { Stop-Process -Name ltcplay-deck -Force; "Stop-Process ltcplay-deck" }
       recover = { Alive "ltcplay-deck" } },
    @{ id = 4; name = "Unplug the Stream Deck (hardware, simulated with pnputil disable)"; needs = "deck-hardware"
       expect = "banner 'The Stream Deck is unplugged or not answering. Every flame group stays as it was. Press Acknowledge once you have seen it, or Disarm all.' (Jeff's ruling: a dying deck never disarms by itself; the deck process keeps the arm link steady); journal fault line action deck; on re-enable: 'The Stream Deck is back...' banner; the deck redraws within 10 s"
       do = {
           $d = Find-StreamDeck
           if (-not $d) { throw "no Stream Deck Mini (VID 0FD9 PID 0063) is plugged in" }
           Add-Undo @{ kind = "pnp"; id = $d.InstanceId; name = $d.FriendlyName }
           & pnputil /disable-device "$($d.InstanceId)" | Out-Null
           "pnputil /disable-device $($d.InstanceId) ($($d.FriendlyName))" }
       recover = { $true } },
    @{ id = 5; name = "Remove the audio interface (pnputil disable)"; needs = "audio-device"
       expect = "within 30 s the 'audio' banner naming the device and the outputs Windows can see; the Audio chip reads 'not attached'; a scheduled show start fails with a sentence (journal 'failed start'); nothing reaches the rig; on re-enable the log says it is back within 30 s, the banner stays until Acknowledge"
       do = {
           $d = Find-Audio $AudioDevice
           if (-not $d) { throw "no audio interface found (give -AudioDevice 'part of its name')" }
           Add-Undo @{ kind = "pnp"; id = $d.InstanceId; name = $d.FriendlyName }
           & pnputil /disable-device "$($d.InstanceId)" | Out-Null
           "pnputil /disable-device $($d.InstanceId) ($($d.FriendlyName))" }
       recover = { $true } },
    @{ id = 6; name = "Drop the show network adapter (Disable-NetAdapter)"; needs = "adapter"
       expect = "the engine, flamesafe and the deck keep running (every link between them is loopback; witness: flamesafe's stream continuous, all zeros); pixel and Art-Net sends to the show network fail: engine send errors climb, 'output' fault lines in the journal; remote pages on the network show their stale banner; on Enable-NetAdapter the sends resume within seconds, no restart needed"
       do = {
           if (-not $Adapter) { throw "give -Adapter 'name' (Get-NetAdapter lists them). NEVER the adapter your remote session uses." }
           $a = Get-NetAdapter -Name $Adapter -ErrorAction Stop
           Add-Undo @{ kind = "adapter"; name = $a.Name }
           Disable-NetAdapter -Name $a.Name -Confirm:$false
           "Disable-NetAdapter '$($a.Name)'" }
       recover = { $true } },
    @{ id = 7; name = "Fill the disk to the threshold (fsutil createnew under Settings and logs)"; needs = ""
       expect = "the rack screen's disk chip turns low ('The disk holding ... has X GB free. Free some space...') under 5 GB, critical under 1 GB (with -Critical); the supervisor log says the same; the journal keeps writing; nothing stops; deleting the fill file clears it within a minute"
       do = {
           New-Item -ItemType Directory -Force -Path $FILL | Out-Null
           $root = [IO.Path]::GetPathRoot($FILL)
           $free = (New-Object IO.DriveInfo($root)).AvailableFreeSpace
           $target = $(if ($Critical) { 0.8GB } else { 4.5GB })
           $size = [long]($free - $target)
           if ($size -le 100MB) { throw "only $([math]::Round($free / 1GB, 1)) GB free already; nothing to fill" }
           $f = Join-Path $FILL "fill-$((Get-Date).ToString('HHmmss')).bin"
           Add-Undo @{ kind = "fill"; path = $f }
           & fsutil file createnew "$f" $size | Out-Null
           "fsutil file createnew $f $([math]::Round($size / 1GB, 1)) GB (free was $([math]::Round($free / 1GB, 1)) GB, target $([math]::Round($target / 1GB, 1)) GB)" }
       recover = { $true } },
    @{ id = 8; name = "Delete the lockout file while a lock is set"; needs = "morning"
       expect = "flamesafe keeps the last locks it confirmed (a deleted file never lifts a lock); the engine shows the lockout file problem on every screen and refuses the next lock, unlock, purge and laser test until it is fixed; journal line names the file; restoring the backup clears it"
       do = {
           if (-not (Test-Path $lockout)) { throw "no ${lockout}: set a lock on the Morning page first" }
           Backup $lockout | Out-Null
           Remove-Item -Force $lockout
           "deleted $lockout" }
       recover = { $true } },
    @{ id = 9; name = "Corrupt the lockout file"; needs = "morning"
       expect = "an unreadable lockout file is refused for every lock, unlock, purge and laser test, shown on every screen until fixed (PR 68 P1-3); flamesafe never lifts a lock because of it; journal line names the file"
       do = {
           if (-not (Test-Path $lockout)) { throw "no ${lockout}: set a lock on the Morning page first" }
           Backup $lockout | Out-Null
           Set-Content -Encoding UTF8 $lockout "{ this is not json"
           "wrote garbage into $lockout" }
       recover = { $true } },
    @{ id = 10; name = "Corrupt the morning config (ltcplay_morning.json)"; needs = "morning"
       expect = "the Morning page refuses with one sentence naming the file; purge and laser tests unavailable; shows and the rest of the rack screen unaffected; restoring the backup brings the page back"
       do = {
           $m = Join-Path $ad "ltcplay_morning.json"
           Backup $m | Out-Null
           Set-Content -Encoding UTF8 $m "{ broken"
           "wrote garbage into $m" }
       recover = { $true } },
    @{ id = 11; name = "Close the page mid-purge (kill the rack screen's browser while the purge is held)"; needs = "morning,operator"
       expect = "the purge stops within 0.25 s of the kill (witness: every fire slot falls to 0 within 0.25 s; the heartbeat gap and the page's hidden state both end it); journal purge released or stopped line; flamesafe's fire_hold_ms stands behind; the page comes back with LTC Player --start"
       do = {
           Read-Host "Hold the purge now (Space on the page or FIRE on the deck). When the witness shows the fire slot RISE, press Enter here"
           $t = (Get-Date).ToString("HH:mm:ss.fff")
           Stop-Process -Name msedge -Force -ErrorAction SilentlyContinue
           Stop-Process -Name chrome -Force -ErrorAction SilentlyContinue
           "killed the browser at $t; compare with the witness fall time" }
       recover = { $true } },
    @{ id = 12; name = "Start a show while a purge is held (Start now from the rack screen route)"; needs = "morning,operator"
       expect = "the purge is cleared the moment the scheduler enters SHOW (witness: the purge fire slot falls to 0 within 0.25 s of the press); the show then fires only its own cues when the timecode moves; journal shows the purge cleared and the Start now, in that order"
       do = {
           Read-Host "Hold the purge now. When the witness shows the fire slot RISE, press Enter here to press Start now"
           $rs = Http "GET" "/api/remote/status"
           $seen = $(if ($rs.body) { $rs.body.served_at } else { 0 })
           $t = (Get-Date).ToString("HH:mm:ss.fff")
           $r = Http "POST" "/api/remote/start-now" @{ confirmed = $true; seen = $seen; who = "Break-it"; screen = "Rack screen" }
           "POST start-now at ${t}: HTTP $($r.code) $($r.body | ConvertTo-Json -Compress)" }
       recover = { $true } }
)

if ($List) {
    foreach ($s in $STEP_LIST) {
        $can = switch -Regex ($s.needs) {
            "morning" { if ($morning) { "yes" } else { "SKIP: needs the Morning page build (ltcplay_morning.json not found)" } }
            "deck-hardware" { if (Find-StreamDeck) { "yes" } else { "SKIP: no Stream Deck Mini plugged in" } }
            "audio-device" { if (Find-Audio $AudioDevice) { "yes" } else { "needs -AudioDevice 'name'" } }
            "adapter" { if ($Adapter) { "yes" } else { "needs -Adapter 'name'" } }
            default { "yes" }
        }
        Write-Host ("{0,2}. {1}`n     can run: {2}`n     expect: {3}`n" -f $s.id, $s.name, ($can | Select-Object -First 1), $s.expect)
    }
    exit 0
}

if ($Undo) {
    $l = @(Load-Undo)
    if (-not $l.Count) { Say "Nothing to undo."; exit 0 }
    $left = @()
    foreach ($u in $l) {
        try {
            switch ($u.kind) {
                "file" { Copy-Item $u.backup $u.path -Force; Say "restored $($u.path) from $($u.backup)" }
                "pnp" { & pnputil /enable-device "$($u.id)" | Out-Null; Say "enabled device $($u.name) ($($u.id))" }
                "adapter" { Enable-NetAdapter -Name $u.name -Confirm:$false; Say "enabled adapter $($u.name)" }
                "fill" { Remove-Item -Force $u.path -ErrorAction SilentlyContinue; Say "deleted $($u.path)" }
                default { Say "unknown undo kind $($u.kind)"; $left += $u }
            }
        } catch { Say "undo of $($u.kind) $($u.path)$($u.name) failed: $($_.Exception.Message)"; $left += $u }
    }
    Save-Undo $left
    Say "Undo done. $($left.Count) item(s) left."
    exit 0
}

if (-not $FlameHardwareDisconnected -or $Confirmation -ne "DISCONNECTED") {
    Say "REFUSED. Break-it kills the flame safety program on purpose. Run it only with the flame hardware DISCONNECTED, then add: -FlameHardwareDisconnected -Confirmation DISCONNECTED"
    exit 3
}
if (-not $Steps -and -not $All) { Say "Give -Steps 1,2,3 or -All (or -List to read them)."; exit 2 }
$chosen = $STEP_LIST | Where-Object { $All -or ($Steps -contains $_.id) }
Say "Break-it started. Flame hardware stated DISCONNECTED. Steps: $(($chosen | ForEach-Object { $_.id }) -join ', '). DryRun=$DryRun"
foreach ($s in $chosen) {
    $skip = ""
    if ($s.needs -match "morning" -and -not $morning) { $skip = "needs the Morning page build (no ltcplay_morning.json yet)" }
    elseif ($s.needs -match "deck-hardware" -and -not (Find-StreamDeck)) { $skip = "no Stream Deck Mini plugged in" }
    elseif ($s.needs -match "adapter" -and -not $Adapter) { $skip = "needs -Adapter" }
    elseif ($s.needs -match "audio-device" -and -not (Find-Audio $AudioDevice)) { $skip = "no audio interface found; give -AudioDevice" }
    if ($skip) { Say "step $($s.id) SKIPPED: $skip"; Result @{ step = $s.id; name = $s.name; started = (Get-Date).ToString("s"); action = "skipped"; expected = $s.expect; observed = $skip; recovered_s = ""; result = "SKIPPED" }; continue }
    $before = Snapshot
    if ($before.running -and $s.needs -notmatch "operator") {
        Say "step $($s.id) REFUSED: a show is running or starting. Nothing was changed."
        Result @{ step = $s.id; name = $s.name; started = (Get-Date).ToString("s"); action = "refused"; expected = $s.expect; observed = "show running"; recovered_s = ""; result = "REFUSED" }
        continue
    }
    Say "step $($s.id): $($s.name)"
    Say "  before: $(Describe $before)"
    Say "  expected: $($s.expect)"
    if ($DryRun) { Say "  dry run: not done"; continue }
    $started = Get-Date
    try { $action = & $s.do } catch { Say "  step failed to run: $($_.Exception.Message)"; Result @{ step = $s.id; name = $s.name; started = $started.ToString("s"); action = "error: $($_.Exception.Message)"; expected = $s.expect; observed = ""; recovered_s = ""; result = "ERROR" }; continue }
    Say "  did: $action"
    Start-Sleep -Seconds 3
    $after = Snapshot
    Say "  after 3 s: $(Describe $after)"
    $rec = Wait-Until $s.recover 120
    if ($rec -ge 0) { Say "  recovered in $rec s" } else { Say "  NOT recovered within 120 s" }
    Start-Sleep -Seconds 5
    $later = Snapshot
    Say "  after recovery: $(Describe $later)"
    $obs = "3 s: $(Describe $after) ||| later: $(Describe $later)"
    Result @{ step = $s.id; name = $s.name; started = $started.ToString("s"); action = $action; expected = $s.expect; observed = $obs; recovered_s = $rec; result = $(if ($rec -ge 0) { "OBSERVED, judge against the table" } else { "NOT RECOVERED" }) }
    if ($s.id -in 4, 5, 6, 7, 8, 9, 10) { Say "  this step leaves a change in place; run -Undo when you have read the screen (or now, to go on)." }
}
Say "Break-it finished. Results: $RESULTS. Undo list: $UNDO_FILE (run -Undo)."
exit 0
