<#
The laser listener, PowerShell edition, for the show PC (which has no
Python). BENCH TOOL. It only LISTENS on this PC and writes files; it never
sends to BEYOND, the rig, flamesafe or ltcplay.

The show PC has MadMapper but no BEYOND for now, so LTC Player's laser
commands (the conductor's OSC blank and restore, the Morning page's laser
test messages) go to nothing. This listener binds the configured BEYOND
OSC port on 127.0.0.1 (the "beyond" block of ltcplay_fire_ice.json;
default 127.0.0.1:8100) and every other local port the lasers in
ltcplay_morning.json name, decodes every OSC message (bundles flattened),
timestamps it and writes the same files as laser_listener.py, read by
check_witness.py:
  laser.csv    time, mono_ms, source, port, address, types, args, kind
  laser.log    the same in words, plus what it listened to
  laser.json   what it listened to and the laser messages it knew
in the witness run folder (witness-current.txt), else a folder of its own.

It REFUSES, with one sentence and no files, when the port is already in
use (real BEYOND, or ltcplay-soak.exe during the soak) or a BEYOND process
is running. Exit code 3.

  powershell -NoProfile -ExecutionPolicy Bypass -File laser_listener.ps1
  ... -FireIce "C:\path\ltcplay_fire_ice.json" -Morning "C:\path\ltcplay_morning.json"
  ... -Port 8100 -Out "D:\witness\run"
  ... -SelfTest      proves it on a spare port, then exits

Stop it with Ctrl-C, or a file named STOP in its run folder (Stop
witness.cmd puts one there: the witness and this listener stop together).
#>
param(
    [string]$FireIce = "",
    [string]$Morning = "",
    [string]$BindHost = "",
    [int]$Port = 0,
    [double]$BlankValue = 0,
    [string]$Out = "",
    [switch]$Force,
    [switch]$SelfTest,
    [switch]$Version
)

$ErrorActionPreference = "Stop"
$LISTENER_VERSION = "laser_listener.ps1 1.0 (stress-kit)"
$DEFAULT_HOST = "127.0.0.1"
$DEFAULT_PORT = 8100
$BRIGHTNESS_ADDR = "/beyond/master/livecontrol/brightness"
$UNBLANK_VALUE = 100.0
$FORBIDDEN = @("/beyond/general/BlackOut", "/beyond/general/MasterPause")
$TOL = 0.001
$INV = [Globalization.CultureInfo]::InvariantCulture

if ($Version) { Write-Output $LISTENER_VERSION; exit 0 }

function Now-Text { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss.fff") }

function AppData-Dir {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = Join-Path $HOME ".local/state" }
    Join-Path $base "ltcplay"
}

function Read-Beyond([string]$path) {
    if (-not $path -or -not (Test-Path $path)) { return @($DEFAULT_HOST, $DEFAULT_PORT, "no ltcplay_fire_ice.json: the default 127.0.0.1:8100") }
    try { $d = Get-Content -Raw $path | ConvertFrom-Json } catch { return @($DEFAULT_HOST, $DEFAULT_PORT, "$path could not be read ($($_.Exception.Message)): the default 127.0.0.1:8100") }
    $mode = "osc"
    if ($d.beyond_blank) { $mode = [string]$d.beyond_blank }
    if (-not $d.beyond) {
        return @($DEFAULT_HOST, $DEFAULT_PORT, "$path has no `"beyond`" block (beyond_blank `"$mode`"): the conductor sends its blanks NOWHERE, the engine shows 'Lasers: blanking not set up' (expected while BEYOND is absent); listening on the default 127.0.0.1:8100")
    }
    $h = $DEFAULT_HOST; $p = $DEFAULT_PORT
    if ($d.beyond.host) { $h = ([string]$d.beyond.host).Trim() }
    if ($d.beyond.port) { try { $p = [int]$d.beyond.port } catch { $p = $DEFAULT_PORT } }
    @($h, $p, "$path beyond block (beyond_blank `"$mode`")")
}

function Read-Lasers([string]$path, [string]$defHost, [int]$defPort) {
    $out = @()
    if (-not $path -or -not (Test-Path $path)) { return ,@($out, "no ltcplay_morning.json: no laser test messages known") }
    try { $d = Get-Content -Raw $path | ConvertFrom-Json } catch { return ,@($out, "$path could not be read ($($_.Exception.Message))") }
    if (-not $d.lasers) { return ,@($out, "$path names no lasers") }
    $i = 0
    foreach ($l in @($d.lasers)) {
        $i++
        if ($null -eq $l) { continue }
        $lid = "laser-$i"
        if ($l.id) { $lid = [string]$l.id }
        $name = $lid
        if ($l.name) { $name = [string]$l.name }
        $mech = "none"
        if ($l.mechanism) { $mech = [string]$l.mechanism }
        $h = $defHost; $p = $defPort
        if ($l.host) { $h = [string]$l.host }
        if ($l.port) { try { $p = [int]$l.port } catch { $p = $defPort } }
        $msgs = @{}
        foreach ($k in @("on", "off", "lock")) {
            $lst = @()
            $v = $l.$k
            if ($v) { foreach ($m in @($v)) { if ($m -is [array] -and $m.Count -eq 2 -and ($m[0] -is [string])) { $lst += ,@($m[0], $m[1]) } } }
            $msgs[$k] = $lst
        }
        $out += [pscustomobject]@{ id = $lid; name = $name; mechanism = $mech; host = $h; port = $p; on = $msgs["on"]; off = $msgs["off"]; lock = $msgs["lock"] }
    }
    ,@($out, "${path}: $($out.Count) laser(s)")
}

# ------------------------------------------------------------------ OSC ---
function Read-BE32([byte[]]$b, [int]$i) {
    $c = New-Object byte[] 4
    [Array]::Copy($b, $i, $c, 0, 4)
    [Array]::Reverse($c)
    $c
}
function Read-BE64([byte[]]$b, [int]$i) {
    $c = New-Object byte[] 8
    [Array]::Copy($b, $i, $c, 0, 8)
    [Array]::Reverse($c)
    $c
}
function Padded-String([byte[]]$b, [int]$i) {
    $end = [Array]::IndexOf($b, [byte]0, $i)
    if ($end -lt 0) { throw "unterminated OSC string" }
    $s = [System.Text.Encoding]::UTF8.GetString($b, $i, $end - $i)
    $i = $end + 1
    while ($i % 4 -ne 0) { $i++ }
    @($s, $i)
}

function Decode-Osc([byte[]]$b) {
    # Every message in one datagram, bundles flattened: a list of
    # @{ address; tags; args }. Throws for anything that is not OSC. Written
    # from the OSC 1.0 spec, never from ltcplay's own encoders.
    if ($b.Length -ge 8 -and [System.Text.Encoding]::ASCII.GetString($b, 0, 7) -eq "#bundle" -and $b[7] -eq 0) {
        $out = @()
        $i = 16
        while ($i + 4 -le $b.Length) {
            $size = [BitConverter]::ToInt32((Read-BE32 $b $i), 0)
            $i += 4
            if ($size -lt 0 -or $i + $size -gt $b.Length) { throw "a bundle element runs past the datagram" }
            $el = New-Object byte[] $size
            [Array]::Copy($b, $i, $el, 0, $size)
            $out += @(Decode-Osc $el)
            $i += $size
        }
        return ,$out
    }
    if ($b.Length -lt 1 -or $b[0] -ne 0x2F) { throw "not an OSC message: no leading /" }
    $r = Padded-String $b 0
    $address = $r[0]; $i = $r[1]
    $tags = ""
    $vals = New-Object System.Collections.ArrayList
    if ($i -lt $b.Length -and $b[$i] -eq 0x2C) {
        $r = Padded-String $b $i
        $tags = $r[0].Substring(1); $i = $r[1]
    }
    foreach ($tag in $tags.ToCharArray()) {
        switch -CaseSensitive ([string]$tag) {
            "i" { [void]$vals.Add([BitConverter]::ToInt32((Read-BE32 $b $i), 0)); $i += 4 }
            "f" { [void]$vals.Add([double][BitConverter]::ToSingle((Read-BE32 $b $i), 0)); $i += 4 }
            "s" { $r = Padded-String $b $i; [void]$vals.Add([string]$r[0]); $i = $r[1] }
            "S" { $r = Padded-String $b $i; [void]$vals.Add([string]$r[0]); $i = $r[1] }
            "b" { $n = [BitConverter]::ToInt32((Read-BE32 $b $i), 0); $i += 4; [void]$vals.Add("<blob $n bytes>"); $i += $n; while ($i % 4 -ne 0) { $i++ } }
            "h" { [void]$vals.Add([BitConverter]::ToInt64((Read-BE64 $b $i), 0)); $i += 8 }
            "t" { [void]$vals.Add([BitConverter]::ToUInt64((Read-BE64 $b $i), 0)); $i += 8 }
            "d" { [void]$vals.Add([BitConverter]::ToDouble((Read-BE64 $b $i), 0)); $i += 8 }
            "c" { [void]$vals.Add([BitConverter]::ToString($b, $i, 4).Replace("-", "").ToLower()); $i += 4 }
            "r" { [void]$vals.Add([BitConverter]::ToString($b, $i, 4).Replace("-", "").ToLower()); $i += 4 }
            "m" { [void]$vals.Add([BitConverter]::ToString($b, $i, 4).Replace("-", "").ToLower()); $i += 4 }
            "T" { [void]$vals.Add($true) }
            "F" { [void]$vals.Add($false) }
            "N" { [void]$vals.Add($null) }
            "I" { [void]$vals.Add("impulse") }
            "[" { }
            "]" { }
            default { throw "unknown OSC type tag '$tag'" }
        }
    }
    return ,@(@{ address = $address; tags = $tags; args = @($vals.ToArray()) })
}

function Pad([byte[]]$x) {
    $n = $x.Length + 1
    while ($n % 4 -ne 0) { $n++ }
    $o = New-Object byte[] $n
    [Array]::Copy($x, 0, $o, 0, $x.Length)
    $o
}
function Encode-Osc([string]$address, $value) {
    # Self test only: one argument, a whole number (,i), a decimal (,f) or text (,s).
    $a = Pad ([System.Text.Encoding]::UTF8.GetBytes($address))
    if ($value -is [int]) {
        $v = [BitConverter]::GetBytes([int]$value); [Array]::Reverse($v)
        return $a + (Pad ([System.Text.Encoding]::ASCII.GetBytes(",i"))) + $v
    }
    if ($value -is [double] -or $value -is [single]) {
        $v = [BitConverter]::GetBytes([single]$value); [Array]::Reverse($v)
        return $a + (Pad ([System.Text.Encoding]::ASCII.GetBytes(",f"))) + $v
    }
    $a + (Pad ([System.Text.Encoding]::ASCII.GetBytes(",s"))) + (Pad ([System.Text.Encoding]::UTF8.GetBytes([string]$value)))
}
function Encode-Bundle($messages) {
    $o = [System.Text.Encoding]::ASCII.GetBytes("#bundle") + [byte[]](0, 0, 0, 0, 0, 0, 0, 0, 1)
    foreach ($m in $messages) {
        $n = [BitConverter]::GetBytes([int]$m.Length); [Array]::Reverse($n)
        $o = $o + $n + $m
    }
    $o
}

function Num-Text([double]$v) {
    # Like Python's repr of a float: 0.0, 100.0, 10.5 (six significant digits).
    $s = ([double]::Parse($v.ToString("G6", $INV), $INV)).ToString("R", $INV)
    if ($s -notmatch "[.eE]") { $s += ".0" }
    $s
}
function Args-Text($vals) {
    $parts = @()
    foreach ($a in @($vals)) {
        if ($null -eq $a) { $parts += "nil" }
        elseif ($a -is [bool]) { if ($a) { $parts += "true" } else { $parts += "false" } }
        elseif ($a -is [double] -or $a -is [single]) { $parts += (Num-Text ([double]$a)) }
        else { $parts += ([string]$a).Replace(";", ",") }
    }
    $parts -join ";"
}
function Same-Value($got, $want) {
    if (($got -is [bool]) -or ($want -is [bool])) { return (($got -is [bool]) -and ($want -is [bool]) -and ($got -eq $want)) }
    $gn = ($got -is [int]) -or ($got -is [long]) -or ($got -is [double]) -or ($got -is [single]) -or ($got -is [decimal])
    $wn = ($want -is [int]) -or ($want -is [long]) -or ($want -is [double]) -or ($want -is [single]) -or ($want -is [decimal])
    if ($gn -and $wn) { return ([math]::Abs([double]$got - [double]$want) -le $TOL) }
    return ([string]$got -eq [string]$want)
}

function Classify([string]$address, $vals, $lasers, [double]$blank) {
    # @(kind, laser name or $null). A laser's own messages first and exactly
    # (address AND value), so a test ON at any other value is OTHER.
    if ($FORBIDDEN -contains $address) { return @("FORBIDDEN", $null) }
    $one = $null
    if (@($vals).Count -eq 1) { $one = @($vals)[0] }
    foreach ($l in @($lasers)) {
        foreach ($pair in @(@("on", "TEST ON"), @("off", "TEST OFF"), @("lock", "LOCK"))) {
            foreach ($m in @($l.($pair[0]))) {
                if ($null -eq $m) { continue }
                if ($m[0] -eq $address -and @($vals).Count -eq 1 -and (Same-Value $one $m[1])) { return @($pair[1], $l.name) }
            }
        }
    }
    if ($address -eq $BRIGHTNESS_ADDR -and @($vals).Count -eq 1 -and -not ($one -is [bool]) -and (($one -is [int]) -or ($one -is [double]) -or ($one -is [single]) -or ($one -is [long]))) {
        if ([math]::Abs([double]$one - $blank) -le $TOL) { return @("BLANK", $null) }
        return @("UNBLANK", $null)
    }
    @("OTHER", $null)
}

function Csv-Field([string]$s) {
    if ($s -match '[",\r\n]') { return '"' + $s.Replace('"', '""') + '"' }
    $s
}

# ---------------------------------------------------------------- files ---
class LaserFiles {
    [string]$Dir
    [string]$LogPath
    [System.IO.StreamWriter]$Csv

    LaserFiles([string]$dir) {
        $this.Dir = $dir
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $this.LogPath = Join-Path $dir "laser.log"
        $p = Join-Path $dir "laser.csv"
        $new = -not (Test-Path $p)
        $this.Csv = New-Object System.IO.StreamWriter($p, $true, (New-Object System.Text.UTF8Encoding($false)))
        $this.Csv.AutoFlush = $true
        if ($new) { $this.Csv.WriteLine("time,mono_ms,source,port,address,types,args,kind") }
    }

    [void] Row([string]$line) { $this.Csv.WriteLine($line) }

    [void] Log([string]$kind, [string]$msg) {
        $line = "$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff'))  ${kind}: $msg"
        Write-Host $line
        try {
            if ((Test-Path $this.LogPath) -and ((Get-Item $this.LogPath).Length -gt 20MB)) { $this.Rotate() }
            [System.IO.File]::AppendAllText($this.LogPath, $line + "`n")
        } catch { }
    }

    [void] Rotate() {
        for ($i = 4; $i -ge 1; $i--) {
            $a = "$($this.LogPath).$i"; $b = "$($this.LogPath).$($i + 1)"
            if (Test-Path $a) { Move-Item -Force $a $b }
        }
        Move-Item -Force $this.LogPath "$($this.LogPath).1"
    }

    [void] Close() { try { $this.Csv.Close() } catch { } }
}

# ------------------------------------------------------------- listener ---
class LaserListener {
    [LaserFiles]$Files
    $Lasers
    [double]$Blank
    [System.Diagnostics.Stopwatch]$Sw
    [int]$Messages = 0
    [int]$Blanks = 0
    [int]$Unblanks = 0
    [int]$Tests = 0
    [int]$Other = 0
    [int]$Forbidden = 0
    [int]$Bad = 0

    LaserListener([LaserFiles]$files, $lasers, [double]$blank) {
        $this.Files = $files; $this.Lasers = $lasers; $this.Blank = $blank
        $this.Sw = [System.Diagnostics.Stopwatch]::StartNew()
    }

    [void] Packet([byte[]]$data, [System.Net.IPEndPoint]$from, [int]$port) {
        $t = [math]::Round($this.Sw.Elapsed.TotalMilliseconds, 1)
        $src = "$($from.Address):$($from.Port)"
        $msgs = $null
        try { $msgs = Decode-Osc $data }
        catch {
            $this.Bad++
            $this.Files.Log("NOT OSC", "$($data.Length) bytes from $src on port $port that are not OSC ($($_.Exception.Message))")
            return
        }
        foreach ($m in @($msgs)) {
            if ($null -eq $m) { continue }
            try { $this.Message($m, $src, $port, $t) }
            catch { $this.Files.Log("listener", "a message could not be recorded: $($_.Exception.Message)") }
        }
    }

    [void] Message($m, [string]$src, [int]$port, [double]$t) {
        $c = Classify $m.address $m.args $this.Lasers $this.Blank
        $kind = $c[0]
        $label = $kind
        if ($null -ne $c[1]) { $label = "$kind $($c[1])" }
        $this.Messages++
        switch ($kind) {
            "BLANK" { $this.Blanks++ }
            "UNBLANK" { $this.Unblanks++ }
            "TEST ON" { $this.Tests++ }
            "TEST OFF" { $this.Tests++ }
            "LOCK" { $this.Tests++ }
            "FORBIDDEN" { $this.Forbidden++ }
            default { $this.Other++ }
        }
        $at = Args-Text $m.args
        $this.Files.Row((@((Now-Text), $t, $src, $port, (Csv-Field $m.address), $m.tags, (Csv-Field $at), (Csv-Field $label)) -join ","))
        $this.Files.Log($label, "$($m.address) ,$($m.tags) $at from $src on port $port")
    }

    [string] Summary() {
        return "$($this.Messages) message(s): $($this.Blanks) blank, $($this.Unblanks) unblank, $($this.Tests) laser test, $($this.Other) other, $($this.Forbidden) forbidden; $($this.Bad) datagram(s) that were not OSC"
    }
}

function Pump([System.Net.Sockets.UdpClient]$udp, [LaserListener]$l, [int]$port, [int]$waitUs) {
    $first = $true
    while ($true) {
        $wait = 0
        if ($first) { $wait = $waitUs }
        if (-not $udp.Client.Poll($wait, [System.Net.Sockets.SelectMode]::SelectRead)) { return }
        $first = $false
        $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
        try { $data = $udp.Receive([ref]$ep) } catch { return }
        $l.Packet($data, $ep, $port)
    }
}

function Port-Holder([int]$port) {
    try {
        $eps = Get-NetUDPEndpoint -LocalPort $port -ErrorAction SilentlyContinue
        foreach ($e in @($eps)) {
            if ($null -eq $e) { continue }
            $pr = Get-Process -Id $e.OwningProcess -ErrorAction SilentlyContinue
            if ($pr) { return "$($pr.ProcessName).exe (pid $($pr.Id))" }
            return "an unknown program (pid $($e.OwningProcess))"
        }
    } catch { }
    ""
}

function Refusal([string]$h, [int]$p, [string]$holder) {
    $low = $holder.ToLower()
    if ($low.Contains("beyond")) { $why = "real BEYOND is running and listening there. This listener only stands in for an ABSENT BEYOND; nothing was started and nothing was written. Judge the lasers at BEYOND itself." }
    elseif ($low.Contains("ltcplay-soak")) { $why = "ltcplay-soak.exe holds BEYOND's port during the soak and judges the laser commands itself (its block report). Nothing was started. Start this listener for stages 2 to 4, once the soak has stopped." }
    elseif ($holder) { $why = "another program holds it (a second listener?). Nothing was started." }
    else { $why = "something already listens there: real BEYOND when it runs, ltcplay-soak.exe during the soak, or a second copy of this listener. Nothing was started and nothing was written." }
    $by = ""
    if ($holder) { $by = " by $holder" }
    "REFUSED: ${h}:${p} is already in use${by}: $why"
}

function Bind-All($targets) {
    # @(sockets, failed target, error) : every (host, port) bound, or none.
    $socks = @()
    foreach ($t in $targets) {
        $udp = New-Object System.Net.Sockets.UdpClient
        try {
            $udp.Client.Bind((New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Parse($t[0]), $t[1])))
        } catch {
            $err = $_.Exception.Message
            try { $udp.Close() } catch { }
            foreach ($s in $socks) { try { $s.udp.Close() } catch { } }
            return @($null, $t, $err)
        }
        $socks += @{ udp = $udp; host = $t[0]; port = $t[1] }
    }
    @($socks, $null, $null)
}

# ------------------------------------------------------------ self test ---
function Run-SelfTest([string]$dir) {
    $lasers = @([pscustomobject]@{ id = "laser-1"; name = "Laser 1"; mechanism = "osc"; host = "127.0.0.1"; port = 0
        on = @(,@("/beyond/zone/1/brightness", 10.0)); off = @(,@("/beyond/zone/1/brightness", 0.0)); lock = @() })
    $files = [LaserFiles]::new($dir)
    $l = [LaserListener]::new($files, $lasers, 0.0)
    $r = Bind-All @(,@("127.0.0.1", 0))
    $udp = $r[0][0].udp
    $port = ([System.Net.IPEndPoint]$udp.Client.LocalEndPoint).Port
    $out = New-Object System.Net.Sockets.UdpClient
    $pk = Encode-Osc $BRIGHTNESS_ADDR 0.0;  [void]$out.Send($pk, $pk.Length, "127.0.0.1", $port)
    $pk = Encode-Osc $BRIGHTNESS_ADDR 100.0; [void]$out.Send($pk, $pk.Length, "127.0.0.1", $port)
    $pk = Encode-Bundle @((Encode-Osc "/beyond/general/Something" 1), (Encode-Osc "/madmapper/x" "hi")); [void]$out.Send($pk, $pk.Length, "127.0.0.1", $port)
    $pk = Encode-Osc "/beyond/zone/1/brightness" 10.0; [void]$out.Send($pk, $pk.Length, "127.0.0.1", $port)
    $pk = [System.Text.Encoding]::ASCII.GetBytes("not osc at all"); [void]$out.Send($pk, $pk.Length, "127.0.0.1", $port)
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline -and ($l.Messages -lt 5 -or $l.Bad -lt 1)) { Pump $udp $l $port 200000 }
    $udp.Close(); $out.Close(); $files.Close()
    $ok = $true
    $rows = @(Import-Csv (Join-Path $dir "laser.csv"))
    $kinds = @($rows | ForEach-Object { $_.kind }) -join "|"
    if ($kinds -ne "BLANK|UNBLANK|OTHER|OTHER|TEST ON Laser 1") { Write-Host "self-test FAILED: laser.csv kinds are $kinds"; $ok = $false }
    if ($ok -and ($rows[0].address -ne $BRIGHTNESS_ADDR -or $rows[0].args -ne "0.0" -or $rows[1].args -ne "100.0" -or $rows[2].args -ne "1" -or $rows[3].args -ne "hi" -or $rows[3].types -ne "s")) {
        Write-Host "self-test FAILED: laser.csv rows are $($rows | ConvertTo-Json -Compress)"; $ok = $false
    }
    $text = Get-Content -Raw (Join-Path $dir "laser.log")
    foreach ($want in @("BLANK: /beyond/master/livecontrol/brightness ,f 0.0", "UNBLANK: /beyond/master/livecontrol/brightness ,f 100.0", "OTHER: /madmapper/x ,s hi", "TEST ON Laser 1", "NOT OSC")) {
        if (-not $text.Contains($want)) { Write-Host "self-test FAILED: laser.log lacks '$want'"; $ok = $false }
    }
    if ($ok) { Write-Host "self-test passed: a blank, an unblank, a bundle, a laser test and a stranger were heard, decoded and written to laser.csv and laser.log"; return 0 }
    Write-Host "self-test FAILED"
    return 1
}

# ----------------------------------------------------------------- main ---
$ad = AppData-Dir
$root = Join-Path $ad "witness"
$stamp = (Get-Date).ToString("yyyy-MM-dd_HHmmss")
if ($SelfTest) {
    $base = $root
    if ($Out) { $base = $Out }
    exit (Run-SelfTest (Join-Path $base "laser_selftest_$stamp"))
}
$fi = $FireIce
if (-not $fi) { $fi = Join-Path $ad "ltcplay_fire_ice.json" }
$mo = $Morning
if (-not $mo) { $mo = Join-Path $ad "ltcplay_morning.json" }
$b = Read-Beyond $fi
$bHost = $b[0]; $bPort = [int]$b[1]; $how = $b[2]
if ($BindHost) { $bHost = $BindHost }
if ($Port -gt 0) { $bPort = $Port }
$lr = Read-Lasers $mo $bHost $bPort
$lasers = @($lr[0]); $lasersHow = $lr[1]
$targets = @(,@($bHost, $bPort))
$notLocal = @()
foreach ($l in $lasers) {
    if (-not $l.host.StartsWith("127.")) { $notLocal += "$($l.name) at $($l.host):$($l.port)"; continue }
    $dup = $false
    foreach ($t in $targets) { if ($t[0] -eq $l.host -and $t[1] -eq $l.port) { $dup = $true } }
    if (-not $dup) { $targets += ,@($l.host, $l.port) }
}
$badHost = @($targets | Where-Object { -not $_[0].StartsWith("127.") } | ForEach-Object { "$($_[0]):$($_[1])" })
if ($badHost.Count -gt 0) {
    Write-Host "REFUSED: BEYOND's address is $($badHost -join ', '), not this PC. A listener here would hear nothing; nothing was started."
    exit 3
}
if (-not $Force) {
    $bp = Get-Process -Name "beyond*" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($bp) { Write-Host (Refusal $bHost $bPort "$($bp.ProcessName).exe (pid $($bp.Id))"); exit 3 }
}
$r = Bind-All $targets
if ($null -eq $r[0]) {
    Write-Host (Refusal $r[1][0] $r[1][1] (Port-Holder $r[1][1]))
    Write-Host "(the socket said: $($r[2]))"
    exit 3
}
$socks = @($r[0])

$outDir = $Out
if (-not $outDir) {
    $cur = Join-Path $root "witness-current.txt"
    if (Test-Path $cur) {
        try { $c = (Get-Content -Raw $cur).Trim(); if ($c -and (Test-Path $c)) { $outDir = $c } } catch { }
    }
}
if (-not $outDir) { $outDir = Join-Path $root $stamp }
$files = [LaserFiles]::new($outDir)
$files.Log("listener", "$LISTENER_VERSION started, pid $PID, folder $outDir")
$files.Log("config", "BEYOND OSC: $how")
$ll = ""
if ($lasers.Count -gt 0) { $ll = "; " + (($lasers | ForEach-Object { "$($_.name) ($($_.mechanism)) on $(($_.on | ForEach-Object { '[' + ($_ -join ', ') + ']' }) -join ' ') off $(($_.off | ForEach-Object { '[' + ($_ -join ', ') + ']' }) -join ' ') lock $(($_.lock | ForEach-Object { '[' + ($_ -join ', ') + ']' }) -join ' ')" }) -join "; ") }
$files.Log("config", "lasers: $lasersHow$ll")
foreach ($n in $notLocal) { $files.Log("note", "$n is not this PC: its test messages cannot be heard here") }
foreach ($s in $socks) { $files.Log("listen", "OSC on $($s.host):$($s.port)") }
$fiOut = $null; if (Test-Path $fi) { $fiOut = $fi }
$moOut = $null; if (Test-Path $mo) { $moOut = $mo }
@{ version = $LISTENER_VERSION; started = (Now-Text); host = $bHost; port = $bPort; ports = @($socks | ForEach-Object { ,@($_.host, $_.port) })
   fire_ice = $fiOut; fire_ice_how = $how; morning = $moOut; brightness_addr = $BRIGHTNESS_ADDR; blank_value = $BlankValue
   unblank_value = $UNBLANK_VALUE; lasers = $lasers } |
    ConvertTo-Json -Depth 6 | ForEach-Object { [IO.File]::WriteAllText((Join-Path $outDir "laser.json"), $_, (New-Object Text.UTF8Encoding($false))) }
try { [IO.File]::WriteAllText((Join-Path $root "laser-current.txt"), $outDir + "`r`n", (New-Object Text.UTF8Encoding($false))) } catch { }

$lst = [LaserListener]::new($files, $lasers, $BlankValue)
$stopFile = Join-Path $outDir "STOP"
$lastTick = Get-Date; $lastSummary = Get-Date
try {
    while ($true) {
        $first = $true
        foreach ($s in $socks) {
            $wait = 0
            if ($first) { $wait = 100000 }
            Pump $s.udp $lst $s.port $wait
            $first = $false
        }
        $now = Get-Date
        if (($now - $lastTick).TotalSeconds -ge 1) { $lastTick = $now; if (Test-Path $stopFile) { $files.Log("listener", "STOP file found; stopping"); break } }
        if (($now - $lastSummary).TotalMinutes -ge 10) { $files.Log("summary", $lst.Summary()); $lastSummary = $now }
    }
} finally {
    foreach ($s in $socks) { try { $s.udp.Close() } catch { } }
    $files.Log("summary", $lst.Summary())
    $files.Log("listener", "stopped")
    $files.Close()
}
exit 0
