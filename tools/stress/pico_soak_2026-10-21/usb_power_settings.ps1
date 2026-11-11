# Jeff approved 2026-10-21: High performance plan, USB selective suspend off, "allow turn off to save power" off on USB root hubs and the Stream Deck.
$log = Join-Path $PSScriptRoot "usb_power_settings.log"
Start-Transcript -Path $log -Force | Out-Null
powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c
powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setactive SCHEME_CURRENT
Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable | Where-Object { $_.InstanceName -match 'ROOT_HUB|VID_0FD9|VID_194F' } | ForEach-Object {
  $_.Enable = $false; Set-CimInstance -InputObject $_; "power-save off: $($_.InstanceName)"
}
Get-CimInstance -Namespace root\wmi -ClassName MSPower_DeviceEnable | Where-Object { $_.InstanceName -match 'ROOT_HUB|VID_0FD9|VID_194F' } | Format-Table InstanceName,Enable -AutoSize | Out-String
powercfg /query SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 | Select-String "Current"
Stop-Transcript | Out-Null
