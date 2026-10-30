# Zips the evidence the checker (check_witness.py) and the soak summary
# (soak_summary.py) read, onto the Desktop. BENCH TOOL: copies and zips,
# changes nothing. Left out on purpose: show files, media, the soak's
# generated shows (show\ in each block), keys, PIN files.
$ErrorActionPreference = "Stop"
$ad = Join-Path $env:LOCALAPPDATA "ltcplay"
$stamp = (Get-Date).ToString("yyyy-MM-dd_HHmm")
$desk = [Environment]::GetFolderPath("Desktop")
$out = Join-Path $desk "LTC Player witness evidence $stamp.zip"
$stage = Join-Path $env:TEMP "ltcplay-evidence-$stamp"
New-Item -ItemType Directory -Force -Path $stage | Out-Null

function Take([string]$rel, [string[]]$include) {
    $src = Join-Path $ad $rel
    if (-not (Test-Path $src)) { return }
    $dst = Join-Path $stage $rel
    if (Test-Path $src -PathType Leaf) {
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
        Copy-Item $src $dst
        return
    }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    $files = Get-ChildItem -Path $src -Recurse -File -ErrorAction SilentlyContinue
    foreach ($f in $files) {
        $r = $f.FullName.Substring($src.Length).TrimStart("\")
        if ($r -match "(^|\\)show(\\|$)") { continue }          # generated shows
        if ($r -match "\.(wav|mp3|aiff|flac|fseq|xsq|mp4|mov|key|pem)$") { continue }
        if ($include -and -not ($include | Where-Object { $r -like $_ })) { continue }
        $to = Join-Path $dst $r
        New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
        Copy-Item $f.FullName $to
    }
}

Take "witness" $null
Take "nights" @("*.jsonl", "*.journal.txt", "*.summary.md")
Take "logs" @("*.log", "*.log.*")
Take "soak" @("status.json", "unattended.json", "unattended_state.json", "daily\*",
              "*report*.txt", "*flame-violations.txt", "*samples.csv", "*cpu1s.csv",
              "*engine-stalls.json", "*engine-send-gaps.json", "*flamesafe-journal\*",
              "*flamesafe.log", "*engine.log", "*deck.log", "*flamesafe-soak*.json")
foreach ($n in @("flamesafe.json", "showpc.json", "ltcplay_fire_ice.json", "ltcplay_schedule.json",
                 "ltcplay_lockout.json", "ltcplay_morning.json", "ltcplay_abort_latch.json")) { Take $n $null }
# flamesafe.json carries the link key: blank it in the copy.
$fs = Join-Path $stage "flamesafe.json"
if (Test-Path $fs) {
    try {
        $doc = Get-Content -Raw $fs | ConvertFrom-Json
        if ($doc.link -and $doc.link.key) { $doc.link.key = "REMOVED-FROM-THE-COPY" }
        $doc | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $fs
    } catch { }
}
$n = (Get-ChildItem -Recurse -File $stage).Count
if ($n -eq 0) { Write-Host "Nothing found under $ad"; exit 1 }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $out -CompressionLevel Optimal -Force
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
Write-Host "Written: $out ($n files, $([math]::Round((Get-Item $out).Length / 1MB, 1)) MB)"
Write-Host "Send this file to the session that runs the checker, or copy it to a machine with Python."
exit 0
