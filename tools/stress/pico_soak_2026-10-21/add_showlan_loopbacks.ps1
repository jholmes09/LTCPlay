# Adds two Microsoft KM-TEST Loopback adapters with fixed addresses for show Art-Net.
# Run elevated. Jeff approved 2026-10-21. Log: add_showlan_loopbacks.log beside this file.
$log = Join-Path $PSScriptRoot "add_showlan_loopbacks.log"
Start-Transcript -Path $log -Force | Out-Null
$want = @(
  @{ Name = "ShowNet MadMapper"; IP = "10.250.1.1" },
  @{ Name = "ShowNet BEYOND";    IP = "10.250.2.1" }
)
Add-Type @"
using System; using System.Runtime.InteropServices;
public class Dev {
  [StructLayout(LayoutKind.Sequential)] public struct SP_DEVINFO_DATA { public int cbSize; public Guid ClassGuid; public int DevInst; public IntPtr Reserved; }
  [DllImport("setupapi.dll", SetLastError=true)] public static extern IntPtr SetupDiCreateDeviceInfoList(ref Guid g, IntPtr h);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiCreateDeviceInfo(IntPtr s, string name, ref Guid g, string desc, IntPtr h, int flags, ref SP_DEVINFO_DATA d);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiSetDeviceRegistryProperty(IntPtr s, ref SP_DEVINFO_DATA d, int prop, byte[] buf, int size);
  [DllImport("setupapi.dll", SetLastError=true)] public static extern bool SetupDiCallClassInstaller(int fn, IntPtr s, ref SP_DEVINFO_DATA d);
  [DllImport("setupapi.dll", SetLastError=true)] public static extern bool SetupDiDestroyDeviceInfoList(IntPtr s);
  [DllImport("newdev.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool UpdateDriverForPlugAndPlayDevices(IntPtr h, string hwid, string inf, int flags, out bool reboot);
}
"@
$netGuid = [Guid]"4d36e972-e325-11ce-bfc1-08002be10318"
$hwid = "*MSLOOP"
$inf = "$env:windir\INF\netloop.inf"
foreach ($w in $want) {
  if (Get-NetIPAddress -IPAddress $w.IP -ErrorAction SilentlyContinue) { "already present: $($w.IP)"; continue }
  $before = @(Get-NetAdapter -IncludeHidden | Where-Object InterfaceDescription -like "*KM-TEST Loopback*" | ForEach-Object ifIndex)
  $set = [Dev]::SetupDiCreateDeviceInfoList([ref]$netGuid, [IntPtr]::Zero)
  $d = New-Object Dev+SP_DEVINFO_DATA; $d.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($d)
  if (-not [Dev]::SetupDiCreateDeviceInfo($set, "NET", [ref]$netGuid, $null, [IntPtr]::Zero, 1, [ref]$d)) { throw "CreateDeviceInfo failed $([Runtime.InteropServices.Marshal]::GetLastWin32Error())" }
  $b = [Text.Encoding]::Unicode.GetBytes($hwid + "`0`0")
  if (-not [Dev]::SetupDiSetDeviceRegistryProperty($set, [ref]$d, 1, $b, $b.Length)) { throw "SetHardwareID failed" }
  if (-not [Dev]::SetupDiCallClassInstaller(0x19, $set, [ref]$d)) { throw "RegisterDevice failed $([Runtime.InteropServices.Marshal]::GetLastWin32Error())" }
  [Dev]::SetupDiDestroyDeviceInfoList($set) | Out-Null
  $r = $false
  [Dev]::UpdateDriverForPlugAndPlayDevices([IntPtr]::Zero, $hwid, $inf, 1, [ref]$r) | Out-Null
  $na = $null
  for ($i = 0; $i -lt 30 -and -not $na; $i++) {
    Start-Sleep 1
    $na = Get-NetAdapter | Where-Object { $_.InterfaceDescription -like "*KM-TEST Loopback*" -and $before -notcontains $_.ifIndex } | Select-Object -First 1
  }
  if (-not $na) { throw "new loopback adapter did not appear" }
  Rename-NetAdapter -Name $na.Name -NewName $w.Name
  Set-NetIPInterface -InterfaceAlias $w.Name -Dhcp Disabled
  New-NetIPAddress -InterfaceAlias $w.Name -IPAddress $w.IP -PrefixLength 24 | Out-Null
  Set-DnsClient -InterfaceAlias $w.Name -RegisterThisConnectionsAddress $false
  "added $($w.Name) $($w.IP)"
}
Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -like "10.250.*" | Format-Table InterfaceAlias,IPAddress,PrefixLength -AutoSize | Out-String
Stop-Transcript | Out-Null
