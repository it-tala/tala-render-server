$ErrorActionPreference = 'Stop'
# Izin: app\ hanya-baca untuk service & user biasa; logs\ bisa ditulis service
& icacls 'D:\RenderServer\app\bin' /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX' '*S-1-5-32-545:(OI)(CI)RX' /T /Q | Out-Null
& icacls 'D:\RenderServer\design' /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX' '*S-1-5-32-545:(OI)(CI)RX' /T /Q | Out-Null
& icacls 'D:\RenderServer\logs' /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)M' /T /Q | Out-Null

# Firewall: port 8080 hanya jaringan Private (kantor)
Get-NetFirewallRule -DisplayName 'Tala Render Web*' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'Tala Render Web (LAN)' -Direction Inbound -Protocol TCP -LocalPort 8080 -Profile Private -Action Allow | Out-Null

# Task: jalan saat boot sebagai LOCAL SERVICE
$p = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\LOCALSERVICE' -LogonType ServiceAccount
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$a = New-ScheduledTaskAction -Execute 'C:\Windows\System32\cmd.exe' -Argument '/c D:\RenderServer\app\bin\run-web.cmd' -WorkingDirectory 'D:\RenderServer\app'
Register-ScheduledTask -TaskName 'TalaRender-Web' -Action $a -Principal $p -Settings $s -Trigger (New-ScheduledTaskTrigger -AtStartup) -Force | Out-Null
Start-ScheduledTask -TaskName 'TalaRender-Web'
Start-Sleep 4
Get-ScheduledTask TalaRender-Web | Format-Table TaskName, State -AutoSize | Out-String
Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | Format-Table LocalAddress, LocalPort, OwningProcess -AutoSize | Out-String
