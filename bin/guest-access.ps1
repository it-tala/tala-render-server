$ErrorActionPreference = 'Stop'
$share = 'D:\RenderServer\Share'
Enable-LocalUser -Name 'Guest'
# Everyone (termasuk Guest) boleh baca/tulis di folder Render
& icacls $share /grant '*S-1-1-0:(OI)(CI)M' /T /Q | Out-Null
Grant-SmbShareAccess -Name 'Render' -AccountName 'Everyone' -AccessRight Change -Force | Out-Null
Set-SmbShare -Name 'Render' -FolderEnumerationMode Unrestricted -Force
# Cek: Guest tidak diblokir dari akses jaringan
secedit /export /cfg "$env:TEMP\ur.inf" /areas USER_RIGHTS | Out-Null
"DenyNetwork: " + (((Get-Content "$env:TEMP\ur.inf") | Where-Object { $_ -match '^SeDenyNetworkLogonRight' }) -join '')
Remove-Item "$env:TEMP\ur.inf"
Get-LocalUser Guest | Format-List Name, Enabled | Out-String
Get-SmbShareAccess -Name Render | Format-Table AccountName, AccessRight -AutoSize | Out-String
& icacls $share
Get-Printer | Where-Object Shared | Format-Table Name, ShareName -AutoSize | Out-String
