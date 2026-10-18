<#
Dress rehearsals (stage 4): 20 or more real shows back to back from the
installed engine's own scheduler, with random Aborts (and Resets) pressed
through the same screen routes a person presses, while the wire witness
records flamesafe's output. Optionally arms every group before each show
through the deck's own arm-hold route (the exerciser's path).

BENCH ONLY. Flame hardware DISCONNECTED; it refuses otherwise. It never
talks to flamesafe: every press goes to the engine's rack screen routes on
127.0.0.1, and the deck process and flamesafe keep every one of their rules.

  powershell -NoProfile -ExecutionPolicy Bypass -File rehearsals.ps1 `
      -Shows 22 -EveryMin 10 -ShowLenS 444 -AbortChance 0.4 -Arm `
      -FlameHardwareDisconnected -Confirmation DISCONNECTED
  ... -Restore      put the operator's schedule file back (also done at the end)

What it changes: %LOCALAPPDATA%\ltcplay\ltcplay_schedule.json (backed up
first, restored at the end or with -Restore) so the scheduler starts a show
every -EveryMin minutes from 3 minutes after the start. The engine must be
running under LTC Player with ltcplay_fire_ice.json saying
"scheduler_performs": true and "auto_start": "when_run_pressed", and a
person must press Run on the page (nothing reaches the rig before that;
this script never presses Run). ShowLenS must be at least the show audio's
length or the engine refuses to start.

Logs: %LOCALAPPDATA%\ltcplay\witness\rehearsals.log and rehearsals.csv (one
row per show: number, start, abort time, reset time, HTTP answers).
#>
param(
    [int]$Shows = 22,
    [double]$EveryMin = 10,
    [int]$ShowLenS = 444,
    [double]$AbortChance = 0.4,
    [switch]$Arm,
    [switch]$Restore,
    [switch]$FlameHardwareDisconnected,
    [string]$Confirmation = "",
    [string]$Operator = "Andy",
    [int]$Port = 7878
)
$ErrorActionPreference = "Continue"
$ad = Join-Path $env:LOCALAPPDATA "ltcplay"
$wdir = Join-Path $ad "witness"
New-Item -ItemType Directory -Force -Path $wdir | Out-Null
$LOG = Join-Path $wdir "rehearsals.log"
$CSV = Join-Path $wdir "rehearsals.csv"
$SCHED = Join-Path $ad "ltcplay_schedule.json"
$BACKUP = Join-Path $wdir "ltcplay_schedule.json.before-rehearsals"
$BASE = "http://127.0.0.1:$Port"
$rnd = New-Object System.Random

function Say([string]$m) { $l = "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff'))  $m"; Write-Host $l; Add-Content -Encoding UTF8 $LOG $l }
function Http([string]$method, [string]$path, $body = $null) {
    try {
        $p = @{ Uri = "$BASE$path"; Method = $method; TimeoutSec = 5; UseBasicParsing = $true }
        if ($null -ne $body) { $p["Body"] = ($body | ConvertTo-Json -Compress); $p["ContentType"] = "application/json" }
        $r = Invoke-WebRequest @p
        return @{ code = [int]$r.StatusCode; body = ($r.Content | ConvertFrom-Json) }
    } catch {
        $code = 0; $b = $null
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        try { $b = ($_.ErrorDetails.Message | ConvertFrom-Json) } catch { }
        return @{ code = $code; body = $b; error = $_.Exception.Message }
    }
}
function Seen() { $r = Http "GET" "/api/remote/status"; if ($r.body -and $r.body.served_at) { return $r.body.served_at } ; return 0 }
function Press([string]$route, [hashtable]$extra) {
    $body = @{ who = "Rehearsal"; screen = "Rack screen"; seen = (Seen) }
    foreach ($k in $extra.Keys) { $body[$k] = $extra[$k] }
    $r = Http "POST" "/api/remote/$route" $body
    $txt = ""
    if ($r.body) { if ($r.body.text) { $txt = $r.body.text } elseif ($r.body.error) { $txt = $r.body.error } }
    Say ("  POST {0}: HTTP {1} {2}" -f $route, $r.code, $txt)
    $r
}
function SchedState() { $r = Http "GET" "/api/schedule/state"; if ($r.body) { return $r.body } ; return $null }
function Restart-LTCPlayer() {
    # The scheduler reads its rule file only when the engine starts, so the
    # safe way to pick up a new schedule is LTC Player's own stop (refused
    # while a show runs; flamesafe sends its safe zeros) and start.
    $sup = "C:\Program Files\LTC Player\LTC Player.exe"
    if (-not (Test-Path $sup)) { Say "  $sup not found; restart LTC Player by hand (Start menu, Stop LTC Player, then LTC Player)"; return }
    Say "  stopping LTC Player the safe way, then starting it, so the engine reads the schedule"
    & $sup --stop --quiet | Out-Null
    Start-Sleep -Seconds 5
    & $sup --start --quiet | Out-Null
    $t0 = Get-Date
    while (((Get-Date) - $t0).TotalSeconds -lt 90) { Start-Sleep -Seconds 2; if ((Http "GET" "/api/state").code -eq 200) { break } }
    Say ("  engine answering again: " + ((Http "GET" "/api/state").code -eq 200))
}
function Row($vals) {
    if (-not (Test-Path $CSV)) { "show,scheduled,started_seen,abort_planned_s,abort_at,abort_http,reset_at,reset_http,ended_seen,note" | Set-Content -Encoding UTF8 $CSV }
    ($vals | ForEach-Object { '"' + ([string]$_).Replace('"', '""') + '"' }) -join "," | Add-Content -Encoding UTF8 $CSV
}

if ($Restore) {
    if (Test-Path $BACKUP) { Copy-Item $BACKUP $SCHED -Force; Say "Restored $SCHED from $BACKUP."; Restart-LTCPlayer }
    else { Say "No backup at $BACKUP; nothing to restore." }
    exit 0
}
if (-not $FlameHardwareDisconnected -or $Confirmation -ne "DISCONNECTED") {
    Say "REFUSED. The rehearsals arm groups and play real shows with fire cues. Run only with the flame hardware DISCONNECTED, then add: -FlameHardwareDisconnected -Confirmation DISCONNECTED"
    exit 3
}
if ($Shows -lt 1 -or $EveryMin * 60 -lt $ShowLenS + 60) { Say "REFUSED. EveryMin must leave at least 60 s after each show (ShowLenS $ShowLenS)."; exit 2 }
$st = Http "GET" "/api/state"
if ($st.code -ne 200) { Say "REFUSED. The engine does not answer on $BASE. Start LTC Player first."; exit 4 }
if ($st.body.running -or $st.body.starting) { Say "REFUSED. A show is running or starting now."; exit 5 }

# ------------------------------------------------------------ schedule ---
if (Test-Path $SCHED) { Copy-Item $SCHED $BACKUP -Force; Say "Backed up $SCHED to $BACKUP" }
$utc = (Get-Date).ToUniversalTime()
$offH = [math]::Round(((Get-Date) - $utc).TotalHours)
$tz = "UTC"
if ($offH -ne 0) { $tz = "Etc/GMT" + $(if ($offH -gt 0) { "-$offH" } else { "+$([math]::Abs($offH))" }) }
$local = $utc.AddHours($offH)
$first = $local.AddMinutes(3)
$first = $first.AddSeconds(-$first.Second)
$lastEnd = $first.AddMinutes($EveryMin * $Shows + 5)
if ($lastEnd.Date -ne $first.Date) { $lastEnd = $first.Date.AddDays(1).AddSeconds(-60) }
$night = @{ first_start = $first.ToString("HH:mm"); interval_min = $EveryMin; last_end = $lastEnd.ToString("HH:mm") }
$rule = @{ timezone = $tz
           season = @{ first_date = $local.Date.AddDays(-1).ToString("yyyy-MM-dd"); last_date = $local.Date.AddDays(2).ToString("yyyy-MM-dd") }
           weekly = @{ mon = $night; tue = $night; wed = $night; thu = $night; fri = $night; sat = $night; sun = $night }
           exceptions = @{}; show_len_s = $ShowLenS; guard_s = 30; late_grace_s = 15 }
$rule | ConvertTo-Json -Depth 5 | ForEach-Object { [IO.File]::WriteAllText($SCHED, $_, (New-Object Text.UTF8Encoding($false))) }   # no BOM
Say "Wrote a rehearsal schedule: $Shows shows, the first at $($night.first_start) ($tz), every $EveryMin min, show_len_s $ShowLenS, until $($night.last_end)."
Restart-LTCPlayer
Say "PRESS RUN ON THE PAGE NOW. The scheduler starts nothing before Run is pressed. Random Aborts: chance $AbortChance per show."
if ($Arm) {
    $op = Http "POST" "/api/schedule/operator" @{ who = $Operator; screen = "Rack screen" }
    Say "Chose $Operator as the Stream Deck's operator: HTTP $($op.code)"
}

function ArmAll() {
    $rs = Http "GET" "/api/remote/status"
    $n = 3
    if ($rs.body -and $rs.body.flames -and $rs.body.flames.groups) { $n = @($rs.body.flames.groups).Count }
    for ($i = 0; $i -lt $n; $i++) {
        $holdId = $null; $t0 = Get-Date; $ok = $false
        while (((Get-Date) - $t0).TotalSeconds -lt 4) {
            $body = @{ group = $i; seen = (Seen) }
            if ($holdId) { $body["hold_id"] = $holdId }
            $r = Http "POST" "/api/remote/arm-hold" $body
            if ($r.code -ne 200) { Say "  arm-hold group $i refused: $($r.body.error)"; break }
            if ($r.body.hold_id) { $holdId = $r.body.hold_id }
            if ($r.body.armed -eq $true) { $ok = $true; break }
            Start-Sleep -Milliseconds 100
        }
        $null = Http "POST" "/api/remote/arm-release" @{ group = $i }
        Say "  group $i arm-hold done (armed reported: $ok); flamesafe reports the truth in the witness"
    }
}

# ---------------------------------------------------------------- shows ---
$done = 0
$deadline = (Get-Date).AddMinutes($EveryMin * ($Shows + 1) + 10)
$lastShow = $null
while ($done -lt $Shows -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $s = SchedState
    if (-not $s) { continue }
    if ($s.state -eq "SHOW" -and $s.running -and $s.running -ne $lastShow) {
        $lastShow = $s.running
        $done++
        $startedAt = Get-Date
        Say "show $($s.running) started (rehearsal $done of $Shows)"
        if ($Arm) { ArmAll }
        $abortAt = -1; $abortHttp = ""; $resetHttp = ""; $abortStamp = ""; $resetStamp = ""
        if ($rnd.NextDouble() -lt $AbortChance) {
            $abortAt = 20 + $rnd.Next(0, [math]::Max(1, $ShowLenS - 60))
            Say "  this show gets an Abort at +$abortAt s"
        }
        $ended = $false
        while (-not $ended) {
            Start-Sleep -Milliseconds 500
            $el = ((Get-Date) - $startedAt).TotalSeconds
            if ($abortAt -ge 0 -and $el -ge $abortAt) {
                $abortStamp = (Get-Date).ToString("HH:mm:ss.fff")
                $r = Press "abort" @{ confirmed = $true }
                $abortHttp = $r.code
                Start-Sleep -Seconds (10 + $rnd.Next(0, 10))
                $resetStamp = (Get-Date).ToString("HH:mm:ss.fff")
                $r2 = Press "reset" @{}
                $resetHttp = $r2.code
                if ($r2.code -ne 200) { Start-Sleep -Seconds 3; $r2 = Press "reset" @{}; $resetHttp = "$resetHttp/$($r2.code)" }
                $abortAt = -1
            }
            $s2 = SchedState
            if ($s2 -and ($s2.state -ne "SHOW" -and $s2.state -ne "PAUSED")) { $ended = $true }
            if ($el -gt $ShowLenS + 90) { $ended = $true }
        }
        Say "  show $lastShow over at $((Get-Date).ToString('HH:mm:ss')) (state $($s2.state))"
        Row @($lastShow, $startedAt.ToString("s"), $startedAt.ToString("HH:mm:ss"), $(if ($abortStamp) { "planned" } else { "" }), $abortStamp, $abortHttp, $resetStamp, $resetHttp, (Get-Date).ToString("HH:mm:ss"), $s2.state)
    }
}
Say "Rehearsals finished: $done show(s) seen. Restoring the schedule."
if (Test-Path $BACKUP) { Copy-Item $BACKUP $SCHED -Force; Say "Restored $SCHED."; Restart-LTCPlayer }
Say "Now run the checker on the witness run: it must show fire only in SHOW, nothing while an Abort stood, and every armed slot backed by a press."
exit 0
