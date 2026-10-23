$ErrorActionPreference = 'Stop'
$share = 'D:\RenderServer\Share'

# 1. Akun bersama "Render" (password diset sendiri oleh pemilik PC)
if (-not (Get-LocalUser -Name 'Render' -ErrorAction SilentlyContinue)) {
    New-LocalUser -Name 'Render' -NoPassword -FullName 'Render (akun bersama)' -Description 'Akses share Render' | Out-Null
}
Set-LocalUser -Name 'Render' -PasswordNeverExpires $true -UserMayChangePassword $false
# akun ini hanya untuk akses jaringan, bukan untuk login di PC server
Remove-LocalGroupMember -Group 'Users' -Member 'Render' -ErrorAction SilentlyContinue

# 2. Izin folder: hanya admin, SYSTEM, LOCAL SERVICE (watcher nanti), dan Render
& icacls $share /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)M' 'Render:(OI)(CI)M' /T /Q | Out-Null

# 3. Share \\SERVER\Render
if (Get-SmbShare -Name 'Render' -ErrorAction SilentlyContinue) { Remove-SmbShare -Name 'Render' -Force }
New-SmbShare -Name 'Render' -Path $share -FullAccess 'Administrators' -ChangeAccess 'Render' `
    -FolderEnumerationMode AccessBased -CachingMode None -Description 'Render otomatis RTX 5090' | Out-Null

Get-SmbShare -Name Render | Format-List Name, Path | Out-String
Get-SmbShareAccess -Name Render | Format-Table AccountName, AccessRight -AutoSize | Out-String
& icacls $share
Get-LocalUser Render | Format-List Name, Enabled, PasswordRequired, PasswordLastSet | Out-String
