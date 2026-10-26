$ErrorActionPreference = 'Stop'
$if = 'Ethernet'; $ip = '192.168.110.152'; $gw = '192.168.110.1'

# 0. pastikan .152 tidak dipakai perangkat lain
$used = Test-Connection -ComputerName $ip -Count 2 -Quiet
if ($used) { "BATAL: $ip sudah dipakai perangkat lain"; exit 1 }

# 1. pengaman: kembali ke DHCP dalam 4 menit kalau tidak dibatalkan
$rb = 'Set-NetIPInterface -InterfaceAlias Ethernet -Dhcp Enabled; Set-DnsClientServerAddress -InterfaceAlias Ethernet -ResetServerAddresses; Get-NetIPAddress -InterfaceAlias Ethernet -AddressFamily IPv4 | Remove-NetIPAddress -Confirm:$false -EA SilentlyContinue; Remove-NetRoute -InterfaceAlias Ethernet -DestinationPrefix 0.0.0.0/0 -Confirm:$false -EA SilentlyContinue; ipconfig /renew Ethernet'
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -Command `"$rb`""
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(4)
Register-ScheduledTask -TaskName 'TalaRender-IP-Rollback' -Action $a -Trigger $t -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
"rollback terjadwal"

# 2. IP statis
Set-NetIPInterface -InterfaceAlias $if -Dhcp Disabled
Get-NetIPAddress -InterfaceAlias $if -AddressFamily IPv4 -EA SilentlyContinue | Remove-NetIPAddress -Confirm:$false
Remove-NetRoute -InterfaceAlias $if -DestinationPrefix '0.0.0.0/0' -Confirm:$false -EA SilentlyContinue
New-NetIPAddress -InterfaceAlias $if -IPAddress $ip -PrefixLength 24 -DefaultGateway $gw | Out-Null
Set-DnsClientServerAddress -InterfaceAlias $if -ServerAddresses @($gw, '8.8.8.8')
Start-Sleep 6

# 3. tes
$okGw = Test-Connection -ComputerName $gw -Count 2 -Quiet
$okNet = $false
try { $r = Invoke-WebRequest -Uri 'https://api.telegram.org' -UseBasicParsing -TimeoutSec 10; $okNet = $true } catch { if ($_.Exception.Response) { $okNet = $true } }
"gateway=$okGw internet=$okNet"
if ($okGw -and $okNet) {
    Unregister-ScheduledTask -TaskName 'TalaRender-IP-Rollback' -Confirm:$false
    Set-NetConnectionProfile -InterfaceAlias $if -NetworkCategory Private -EA SilentlyContinue
    "BERHASIL: rollback dibatalkan"
} else {
    "GAGAL: rollback akan berjalan otomatis"
}
Get-NetIPConfiguration -InterfaceAlias $if | % { 'ip=' + $_.IPv4Address.IPAddress + ' gw=' + $_.IPv4DefaultGateway.NextHop + ' dns=' + ($_.DNSServer.ServerAddresses -join ',') }
Get-NetConnectionProfile -InterfaceAlias $if | % { 'profile=' + $_.NetworkCategory }
