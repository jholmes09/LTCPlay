<#
The wire witness, PowerShell edition, for the show PC (which has no Python).
BENCH TOOL. It only LISTENS on this PC and writes files; it never sends to
the rig, arms anything, or talks to flamesafe or ltcplay.

Same files as witness.py, read by check_witness.py:
  witness.log   rolling log: stream start and stop, each slot's rise and
                fall, sender changes, gaps, the stream-terminated flag
  nonzero.csv   one row per packet carrying a nonzero slot (every nonzero
                flame channel, as slot=value;slot=value)
  gaps.csv      every gap between packets over -GapMs
  stream.csv    a 10 s summary: packets, worst gap, nonzero packets
  procs.csv     every 10 s: LTC Player programs' memory, handles, CPU
  disk.csv      every 10 s: free disk
  witness.json  what this run listened to

  powershell -NoProfile -ExecutionPolicy Bypass -File witness.ps1
  ... -Config "C:\path\flamesafe.json" -Out "D:\witness"
  ... -Companion     no sACN bind (automatic while ltcplay-soak.exe runs:
                     the soak owns 127.0.0.1:5568 and refuses to start if
                     anything else holds it)
  ... -SelfTest      proves the recorder on a spare port, then exits

Stop it with Ctrl-C, or put a file named STOP in its run folder.
#>
param(
    [string]$Config = "",
    [string]$Out = "",
    [string]$Bind = "0.0.0.0",
    [int]$Port = 0,
    [int]$Universe = 0,
    [double]$GapMs = 100,
    [double]$StallS = 2,
    [switch]$ArtNet,
    [switch]$NoMulticast,
    [switch]$Companion,
    [switch]$Force,
    [switch]$SelfTest,
    [switch]$Version
)

$ErrorActionPreference = "Stop"
$WITNESS_VERSION = "witness.ps1 1.0 (stress-kit)"
$LOG_LIMIT = 20MB
$LOG_KEEP = 5
$PROC_NAMES = @("flamesafe", "ltcplay", "ltcplay-deck", "LTC Player", "ltcplay-soak", "ltcplay-witness")
$E131_ID = [byte[]](0x41,0x53,0x43,0x2D,0x45,0x31,0x2E,0x31,0x37,0x00,0x00,0x00)

if ($Version) { Write-Output $WITNESS_VERSION; exit 0 }

function Now-Text { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss.fff") }

function AppData-Dir {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = Join-Path $HOME ".local/state" }
    Join-Path $base "ltcplay"
}

function Find-Config([string]$given) {
    if ($given) { return @($given, "named on the command line") }
    $ad = AppData-Dir
    $sp = Join-Path $ad "showpc.json"
    if (Test-Path $sp) {
        try {
            $p = (Get-Content -Raw $sp | ConvertFrom-Json).flamesafe_config
            if ($p -and (Test-Path $p)) { return @($p, "showpc.json flamesafe_config") }
        } catch { }
    }
    $p = Join-Path $ad "flamesafe.json"
    if (Test-Path $p) { return @($p, "Settings and logs") }
    $p = "C:\Program Files\LTC Player\flamesafe.example.json"
    if (Test-Path $p) { return @($p, "the EXAMPLE config (no flamesafe.json found)") }
    return @($null, "no config found")
}

function Load-Groups([string]$path) {
    $d = Get-Content -Raw $path | ConvertFrom-Json
    $arm = 0
    if ($null -ne $d.arm_value) { $arm = [int]$d.arm_value }
    $groups = @()
    foreach ($g in @($d.groups)) {
        if ($null -eq $g) { continue }
        $ga = $arm
        if ($null -ne $g.arm_value) { $ga = [int]$g.arm_value }
        $fire = @()
        foreach ($f in @($g.fire)) { $fire += [int]$f }
        $groups += [pscustomobject]@{ name = [string]$g.name; safety = [int]$g.safety; fire = $fire; arm_value = $ga }
    }
    $destIp = "127.0.0.1"; $destPort = 5568
    if ($d.destination) {
        if ($d.destination.ip) { $destIp = [string]$d.destination.ip }
        if ($d.destination.port) { $destPort = [int]$d.destination.port }
    }
    $uni = 1
    if ($null -ne $d.universe) { $uni = [int]$d.universe }
    $confirmed = $false
    if ($null -ne $d.confirmed) { $confirmed = [bool]$d.confirmed }
    [pscustomobject]@{ universe = $uni; dest_ip = $destIp; dest_port = $destPort; arm_value = $arm; groups = $groups; confirmed = $confirmed }
}

function Slot-Names($cfg) {
    $names = @{}
    foreach ($g in $cfg.groups) {
        $names[[int]$g.safety] = "safety of $($g.name)"
        foreach ($f in $g.fire) { $names[[int]$f] = "fire of $($g.name)" }
    }
    $names
}

function Parse-Sacn([byte[]]$b) {
    if ($b.Length -lt 126) { return $null }
    for ($i = 0; $i -lt 12; $i++) { if ($b[4 + $i] -ne $E131_ID[$i]) { return $null } }
    if ($b[18] -ne 0 -or $b[19] -ne 0 -or $b[20] -ne 0 -or $b[21] -ne 4) { return $null }
    if ($b[40] -ne 0 -or $b[41] -ne 0 -or $b[42] -ne 0 -or $b[43] -ne 2) { return $null }
    $nameLen = 0
    while ($nameLen -lt 64 -and $b[44 + $nameLen] -ne 0) { $nameLen++ }
    $name = [System.Text.Encoding]::UTF8.GetString($b, 44, $nameLen)
    $count = (([int]$b[123] -shl 8) -bor $b[124]) - 1
    if ($count -lt 0) { $count = 0 }
    $avail = $b.Length - 126
    if ($count -gt $avail) { $count = $avail }
    $values = New-Object byte[] $count
    if ($count -gt 0) { [Array]::Copy($b, 126, $values, 0, $count) }
    @{ universe = (([int]$b[113] -shl 8) -bor $b[114]); seq = [int]$b[111]; priority = [int]$b[108]
       terminated = (($b[112] -band 0x40) -ne 0); source = $name; start_code = [int]$b[125]; dmx = $values }
}

function Parse-ArtNet([byte[]]$b) {
    if ($b.Length -lt 18) { return $null }
    $id = [System.Text.Encoding]::ASCII.GetString($b, 0, 7)
    if ($id -ne "Art-Net" -or $b[7] -ne 0) { return $null }
    $op = [int]$b[8] -bor ([int]$b[9] -shl 8)
    if ($op -ne 0x5000) { return $null }
    $len = ([int]$b[16] -shl 8) -bor $b[17]
    $avail = $b.Length - 18
    if ($len -gt $avail) { $len = $avail }
    $values = New-Object byte[] $len
    if ($len -gt 0) { [Array]::Copy($b, 18, $values, 0, $len) }
    @{ universe = ((([int]$b[15] -band 0x7F) -shl 8) -bor $b[14]); seq = [int]$b[12]; priority = ""
       terminated = $false; source = "artnet"; start_code = 0; dmx = $values }
}

function Build-TestPacket([int]$universe, [byte[]]$values, [int]$seq, [bool]$terminated) {
    $total = 126 + $values.Length
    $b = New-Object byte[] $total
    $b[1] = 0x10
    [Array]::Copy($E131_ID, 0, $b, 4, 12)
    $b[16] = 0x70 -bor ((($total - 16) -shr 8) -band 0x0F); $b[17] = ($total - 16) -band 0xFF
    $b[21] = 4
    for ($i = 22; $i -lt 38; $i++) { $b[$i] = 1 }
    $b[38] = 0x70 -bor ((($total - 38) -shr 8) -band 0x0F); $b[39] = ($total - 38) -band 0xFF
    $b[43] = 2
    $src = [System.Text.Encoding]::ASCII.GetBytes("witness-test")
    [Array]::Copy($src, 0, $b, 44, $src.Length)
    $b[108] = 200
    $b[111] = $seq -band 0xFF
    if ($terminated) { $b[112] = 0x40 }
    $b[113] = ($universe -shr 8) -band 0xFF; $b[114] = $universe -band 0xFF
    $b[115] = 0x70 -bor ((($total - 115) -shr 8) -band 0x0F); $b[116] = ($total - 115) -band 0xFF
    $b[117] = 2; $b[118] = 0xA1; $b[122] = 1
    $n = $values.Length + 1
    $b[123] = ($n -shr 8) -band 0xFF; $b[124] = $n -band 0xFF
    [Array]::Copy($values, 0, $b, 126, $values.Length)
    $b
}

# ---------------------------------------------------------------- files ---
class Files {
    [string]$Dir
    [string]$LogPath
    [System.IO.StreamWriter]$Nonzero
    [System.IO.StreamWriter]$Gaps
    [System.IO.StreamWriter]$Stream
    [System.IO.StreamWriter]$Procs
    [System.IO.StreamWriter]$Disk

    Files([string]$dir) {
        $this.Dir = $dir
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $this.LogPath = Join-Path $dir "witness.log"
        $this.Nonzero = $this.Open("nonzero.csv", "time,mono_ms,source,universe,seq,priority,n_nonzero,slots")
        $this.Gaps = $this.Open("gaps.csv", "time,mono_ms,gap_ms,last_seq,next_seq,last_nonzero")
        $this.Stream = $this.Open("stream.csv", "time,packets,max_gap_ms,nonzero_packets,last_seq,bytes,other_universe,multicast")
        $this.Procs = $this.Open("procs.csv", "time,name,pid,ws_mb,handles,cpu_s")
        $this.Disk = $this.Open("disk.csv", "time,free_gb")
    }

    [System.IO.StreamWriter] Open([string]$name, [string]$header) {
        $p = Join-Path $this.Dir $name
        $new = -not (Test-Path $p)
        $w = New-Object System.IO.StreamWriter($p, $true, (New-Object System.Text.UTF8Encoding($false)))
        $w.AutoFlush = $true
        if ($new) { $w.WriteLine($header) }
        return $w
    }

    [void] Row([System.IO.StreamWriter]$w, [string]$line) { $w.WriteLine($line) }

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

    [void] Close() {
        foreach ($w in @($this.Nonzero, $this.Gaps, $this.Stream, $this.Procs, $this.Disk)) { try { $w.Close() } catch { } }
    }
}

# -------------------------------------------------------------- witness ---
class Witness {
    $Cfg
    [Files]$Files
    [double]$GapMs
    [double]$StallS
    [System.Diagnostics.Stopwatch]$Sw
    [hashtable]$Names
    [double]$LastT = -1
    [int]$LastSeq = -1
    [int]$LastNonzero = 0
    [byte[]]$LastValues
    [string]$Sender = ""
    [bool]$Live = $false
    [int]$Packets = 0
    [int]$NonzeroPackets = 0
    [long]$Bytes = 0
    [int]$OtherUniverse = 0
    [int]$MulticastSeen = 0
    [double]$MaxGap = 0
    [int]$TerminatedSeen = 0

    Witness($cfg, [Files]$files, [double]$gapMs, [double]$stallS) {
        $this.Cfg = $cfg; $this.Files = $files; $this.GapMs = $gapMs; $this.StallS = $stallS
        $this.Sw = [System.Diagnostics.Stopwatch]::StartNew()
        $this.Names = Slot-Names $cfg
        $this.LastValues = New-Object byte[] 0
    }

    [double] MonoMs() { return [math]::Round($this.Sw.Elapsed.TotalMilliseconds, 1) }

    [void] Packet([byte[]]$data, [System.Net.IPEndPoint]$from, [bool]$multicast) {
        try { $this.PacketInner($data, $from, $multicast) }
        catch { $this.Files.Log("witness", "a packet could not be recorded: $($_.Exception.Message)") }
    }

    [void] PacketInner([byte[]]$data, [System.Net.IPEndPoint]$from, [bool]$multicast) {
        $t = $this.Sw.Elapsed.TotalMilliseconds
        $this.Bytes += $data.Length
        $p = Parse-Sacn $data
        if ($null -eq $p) { $p = Parse-ArtNet $data }
        $addr = "$($from.Address):$($from.Port)"
        if ($null -eq $p) {
            $this.Files.Log("other", "$($data.Length) bytes from $addr that are not sACN or Art-Net")
            return
        }
        if ($p.universe -ne $this.Cfg.universe) {
            $this.OtherUniverse++
            if ($this.OtherUniverse -le 20) {
                $nzo = 0
                foreach ($v in $p.dmx) { if ($v -ne 0) { $nzo++ } }
                if ($nzo -gt 0) { $this.Files.Log("other-universe", "universe $($p.universe) from $addr ($($p.source)) carries $nzo nonzero slot(s)") }
            }
            return
        }
        if ($multicast) {
            $this.MulticastSeen++
            if ($this.MulticastSeen -le 20) { $this.Files.Log("MULTICAST", "the flame universe $($p.universe) arrived by multicast from $addr; flamesafe never multicasts, so another program is sending it") }
        }
        if ($p.start_code -ne 0) { return }
        $this.Packets++
        if ($this.LastT -ge 0) {
            $gap = $t - $this.LastT
            if ($gap -gt $this.MaxGap) { $this.MaxGap = $gap }
            if ($gap -ge $this.StallS * 1000) {
                $this.Files.Log("STREAM BACK", ("after {0:N1} s of silence; the last packet before it carried {1} nonzero slot(s)" -f ($gap / 1000), $this.LastNonzero))
            }
            if ($gap -gt $this.GapMs) {
                $this.Files.Row($this.Files.Gaps, ("{0},{1},{2},{3},{4},{5}" -f (Now-Text), [math]::Round($t, 1), [math]::Round($gap, 1), $this.LastSeq, $p.seq, $this.LastNonzero))
                if ($gap -le $this.StallS * 1000) { $this.Files.Log("gap", ("{0:N0} ms between seq {1} and {2}" -f $gap, $this.LastSeq, $p.seq)) }
            }
        } else {
            $this.Files.Log("STREAM START", "first packet of universe $($p.universe) from $addr ($($p.source), priority $($p.priority))")
        }
        if ($this.Sender -ne $addr) {
            if ($this.Sender -ne "") { $this.Files.Log("SENDER CHANGED", "now $addr ($($p.source)), was $($this.Sender)") }
            $this.Sender = $addr
        }
        if ($p.priority -ne "" -and $p.priority -ne 200 -and $this.Packets -le 3) { $this.Files.Log("priority", "sACN priority is $($p.priority), flamesafe sends 200") }
        if ($p.terminated) {
            $this.TerminatedSeen++
            if ($this.TerminatedSeen -le 6) { $this.Files.Log("TERMINATED", "stream-terminated flag on seq $($p.seq) (a clean stop sends six)") }
        }
        $this.Edges($p.dmx)
        $sb = New-Object System.Text.StringBuilder
        $n = 0
        for ($i = 0; $i -lt $p.dmx.Length; $i++) {
            $v = $p.dmx[$i]
            if ($v -ne 0) {
                if ($n -gt 0) { [void]$sb.Append(";") }
                [void]$sb.Append($i + 1).Append("=").Append([int]$v)
                $n++
            }
        }
        if ($n -gt 0) {
            $this.NonzeroPackets++
            $this.Files.Row($this.Files.Nonzero, ("{0},{1},{2},{3},{4},{5},{6},{7}" -f (Now-Text), [math]::Round($t, 1), $addr, $p.universe, $p.seq, $p.priority, $n, $sb.ToString()))
        }
        $this.LastT = $t
        $this.LastSeq = $p.seq
        $this.LastNonzero = $n
        $this.LastValues = $p.dmx
        $this.Live = $true
    }

    [void] Edges([byte[]]$values) {
        $old = $this.LastValues
        $n = [math]::Max($old.Length, $values.Length)
        for ($i = 0; $i -lt $n; $i++) {
            $a = 0; $b = 0
            if ($i -lt $old.Length) { $a = [int]$old[$i] }
            if ($i -lt $values.Length) { $b = [int]$values[$i] }
            if ($a -eq $b) { continue }
            $slot = $i + 1
            $what = "NOT IN ANY GROUP"
            if ($this.Names.ContainsKey($slot)) { $what = $this.Names[$slot] }
            if ($a -eq 0) { $this.Files.Log("RISE", "slot $slot -> $b ($what)") }
            elseif ($b -eq 0) { $this.Files.Log("fall", "slot $slot -> 0 ($what)") }
            else { $this.Files.Log("change", "slot $slot $a -> $b ($what)") }
        }
    }

    [void] Tick() {
        if ($this.Live -and $this.LastT -ge 0) {
            $silent = ($this.Sw.Elapsed.TotalMilliseconds - $this.LastT) / 1000
            if ($silent -ge $this.StallS) {
                $this.Live = $false
                $kind = "STREAM STOPPED"
                $tail = ""
                if ($this.LastNonzero -ne 0) { $kind = "STREAM STOPPED WITH NONZERO ON THE WIRE"; $tail = ": the node is holding those values" }
                $this.Files.Log($kind, ("no packet for {0:N1} s; the last one (seq {1}) carried {2} nonzero slot(s){3}" -f $silent, $this.LastSeq, $this.LastNonzero, $tail))
            }
        }
    }

    [void] SummaryRow() {
        $this.Files.Row($this.Files.Stream, ("{0},{1},{2},{3},{4},{5},{6},{7}" -f (Now-Text), $this.Packets, [math]::Round($this.MaxGap, 1), $this.NonzeroPackets, $this.LastSeq, $this.Bytes, $this.OtherUniverse, $this.MulticastSeen))
        $this.Packets = 0; $this.NonzeroPackets = 0; $this.Bytes = 0; $this.OtherUniverse = 0; $this.MulticastSeen = 0; $this.MaxGap = 0
    }
}

function Sample-Procs([Files]$files, [string]$folder) {
    try {
        foreach ($p in (Get-Process -Name $PROC_NAMES -ErrorAction SilentlyContinue)) {
            $cpu = ""
            try { $cpu = [math]::Round($p.TotalProcessorTime.TotalSeconds, 1) } catch { }
            $files.Row($files.Procs, ("{0},{1},{2},{3},{4},{5}" -f (Now-Text), $p.ProcessName, $p.Id, [math]::Round($p.WorkingSet64 / 1MB, 1), $p.HandleCount, $cpu))
        }
    } catch { }
    try {
        $root = [System.IO.Path]::GetPathRoot((Resolve-Path $folder).Path)
        $di = New-Object System.IO.DriveInfo($root)
        $files.Row($files.Disk, ("{0},{1}" -f (Now-Text), [math]::Round($di.AvailableFreeSpace / 1GB, 1)))
    } catch { }
}

function Open-Udp([string]$bind, [int]$port, [int]$universe, [bool]$multicast) {
    $udp = New-Object System.Net.Sockets.UdpClient
    $udp.Client.ReceiveBufferSize = 4MB
    $udp.Client.Bind((New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Parse($bind), $port)))
    $joined = ""
    if ($multicast) {
        try {
            $grp = "239.255.$(($universe -shr 8) -band 0xFF).$($universe -band 0xFF)"
            $udp.JoinMulticastGroup([System.Net.IPAddress]::Parse($grp))
            $joined = $grp
        } catch { $joined = "" }
    }
    @{ udp = $udp; joined = $joined }
}

function Pump([System.Net.Sockets.UdpClient]$udp, [Witness]$w, [int]$waitUs) {
    # Every datagram waiting on $udp, then back; waits at most $waitUs for the first.
    $first = $true
    while ($true) {
        $wait = 0
        if ($first) { $wait = $waitUs }
        if (-not $udp.Client.Poll($wait, [System.Net.Sockets.SelectMode]::SelectRead)) { return }
        $first = $false
        $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
        try { $data = $udp.Receive([ref]$ep) } catch { return }
        $w.Packet($data, $ep, $false)
    }
}

# ------------------------------------------------------------ self test ---
function Run-SelfTest([string]$dir) {
    $cfg = [pscustomobject]@{ universe = 1; dest_ip = "127.0.0.1"; dest_port = 0; arm_value = 78; confirmed = $false
        groups = @([pscustomobject]@{ name = "front row"; safety = 401; fire = @(411, 412); arm_value = 78 }) }
    $files = [Files]::new($dir)
    $w = [Witness]::new($cfg, $files, 100, 2)
    $o = Open-Udp "127.0.0.1" 0 1 $false
    $udp = $o.udp
    $port = ([System.Net.IPEndPoint]$udp.Client.LocalEndPoint).Port
    $out = New-Object System.Net.Sockets.UdpClient
    $vals = New-Object byte[] 512
    $pk = Build-TestPacket 1 $vals 1 $false
    [void]$out.Send($pk, $pk.Length, "127.0.0.1", $port)
    $vals[400] = 78; $vals[410] = 255
    $pk = Build-TestPacket 1 $vals 2 $false
    [void]$out.Send($pk, $pk.Length, "127.0.0.1", $port)
    $vals = New-Object byte[] 512
    $pk = Build-TestPacket 1 $vals 3 $true
    [void]$out.Send($pk, $pk.Length, "127.0.0.1", $port)
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline -and $w.TerminatedSeen -lt 1) { Pump $udp $w 200000 }
    $udp.Close(); $out.Close(); $files.Close()
    $ok = $true
    $rows = Import-Csv (Join-Path $dir "nonzero.csv")
    if (@($rows).Count -ne 1 -or $rows[0].slots -ne "401=78;411=255") { Write-Host "self-test FAILED: nonzero.csv has $($rows | ConvertTo-Json -Compress)"; $ok = $false }
    $text = Get-Content -Raw (Join-Path $dir "witness.log")
    foreach ($want in @("STREAM START", "slot 401 -> 78 (safety of front row)", "slot 411 -> 255 (fire of front row)", "slot 411 -> 0", "TERMINATED")) {
        if (-not $text.Contains($want)) { Write-Host "self-test FAILED: witness.log lacks '$want'"; $ok = $false }
    }
    if ($ok) { Write-Host "self-test passed: a crafted packet was heard, parsed, and written to nonzero.csv and witness.log"; return 0 }
    Write-Host "self-test FAILED"
    return 1
}

# ----------------------------------------------------------------- main ---
$root = Join-Path (AppData-Dir) "witness"
$stamp = (Get-Date).ToString("yyyy-MM-dd_HHmmss")
if ($SelfTest) {
    $base = $root
    if ($Out) { $base = $Out }
    exit (Run-SelfTest (Join-Path $base "selftest_$stamp"))
}
$outDir = $Out
if (-not $outDir) { $outDir = Join-Path $root $stamp }
$files = [Files]::new($outDir)
$files.Log("witness", "$WITNESS_VERSION started, pid $PID, folder $outDir")
$found = Find-Config $Config
$cfgPath = $found[0]; $how = $found[1]
$cfg = $null
if ($cfgPath) {
    try {
        $cfg = Load-Groups $cfgPath
        $gl = ($cfg.groups | ForEach-Object { "$($_.name) safety $($_.safety) fire [$($_.fire -join ', ')]" }) -join "; "
        $files.Log("config", "$cfgPath ($how): universe $($cfg.universe), destination $($cfg.dest_ip):$($cfg.dest_port), arm value $($cfg.arm_value), confirmed $($cfg.confirmed), groups $gl")
    } catch {
        $files.Log("config", "$cfgPath could not be read ($($_.Exception.Message)); slots will be recorded without group names")
    }
}
if ($null -eq $cfg) {
    $cfg = [pscustomobject]@{ universe = 1; dest_ip = "127.0.0.1"; dest_port = 5568; arm_value = 0; groups = @(); confirmed = $false }
    $files.Log("config", "$how; using universe 1, port 5568, no groups")
}
if ($Universe -gt 0) { $cfg.universe = $Universe }
$port = $Port
if ($port -le 0) { $port = $cfg.dest_port }
@{ version = $WITNESS_VERSION; started = (Now-Text); config = $cfgPath; config_how = $how; universe = $cfg.universe; port = $port; bind = $Bind
   arm_value = $cfg.arm_value; groups = $cfg.groups; gap_ms = $GapMs; stall_s = $StallS; companion = [bool]$Companion } |
    ConvertTo-Json -Depth 5 | ForEach-Object { [IO.File]::WriteAllText((Join-Path $outDir "witness.json"), $_, (New-Object Text.UTF8Encoding($false))) }
# No BOM: the .cmd launchers read this path with set /p, and a BOM would be part of it.
try { [IO.File]::WriteAllText((Join-Path $root "witness-current.txt"), $outDir + "`r`n", (New-Object Text.UTF8Encoding($false))) } catch { }

$w = [Witness]::new($cfg, $files, $GapMs, $StallS)
$companion = [bool]$Companion
if (-not $companion -and -not $Force -and (Get-Process -Name "ltcplay-soak" -ErrorAction SilentlyContinue)) {
    $companion = $true
    $files.Log("COMPANION", "ltcplay-soak.exe is running: it owns 127.0.0.1:5568 and refuses to start if anything else holds it, so this witness records processes and disk only; the soak's own listener judges flamesafe's output (flame-violations.txt in the block folder)")
}
$udp = $null; $art = $null
if (-not $companion) {
    if (-not $cfg.dest_ip.StartsWith("127.") -and $Bind -eq "0.0.0.0") {
        $files.Log("note", "the config's destination is $($cfg.dest_ip); this witness hears it only if that is one of this PC's own addresses. For a bench run with hardware disconnected, set destination to 127.0.0.1 (back up the file first) so the wire ends here.")
    }
    try {
        $o = Open-Udp $Bind $port $cfg.universe (-not $NoMulticast)
        $udp = $o.udp
        $mc = ", no multicast"
        if ($o.joined) { $mc = ", multicast $($o.joined) joined" }
        $files.Log("listen", "sACN on ${Bind}:${port}$mc")
    } catch {
        $files.Log("CANNOT LISTEN", "${Bind}:${port}: $($_.Exception.Message). Another program holds the flame port (the soak, or a second witness). Nothing is recorded from the wire.")
    }
    if ($ArtNet) {
        try {
            $o = Open-Udp $Bind 6454 $cfg.universe $false
            $art = $o.udp
            $files.Log("listen", "Art-Net on ${Bind}:6454")
        } catch { $files.Log("artnet", "not listening on 6454: $($_.Exception.Message)") }
    }
}
$stopFile = Join-Path $outDir "STOP"
$lastTick = Get-Date; $lastSummary = Get-Date; $lastSample = Get-Date
try {
    while ($true) {
        if ($null -ne $udp) { Pump $udp $w 100000 } else { Start-Sleep -Milliseconds 200 }
        if ($null -ne $art) { Pump $art $w 0 }
        $now = Get-Date
        if (($now - $lastTick).TotalSeconds -ge 1) { $w.Tick(); $lastTick = $now; if (Test-Path $stopFile) { $files.Log("witness", "STOP file found; stopping"); break } }
        if (($now - $lastSummary).TotalSeconds -ge 10) { $w.SummaryRow(); $lastSummary = $now }
        if (($now - $lastSample).TotalSeconds -ge 10) { Sample-Procs $files $outDir; $lastSample = $now }
    }
} finally {
    if ($null -ne $udp) { try { $udp.Close() } catch { } }
    if ($null -ne $art) { try { $art.Close() } catch { } }
    $w.SummaryRow()
    $files.Log("witness", "stopped")
    $files.Close()
}
exit 0
