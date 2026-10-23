$ErrorActionPreference = 'Continue'
# Tutup share folder lain (isi folder TIDAK dihapus)
foreach ($s in '2026', 'TES KONEKSI', 'Users') {
    if (Get-SmbShare -Name $s -ErrorAction SilentlyContinue) {
        Get-SmbOpenFile | Where-Object ShareRelativePath -ne $null | Out-Null
        Remove-SmbShare -Name $s -Force; "tutup share: $s"
    }
}
# Berhenti share printer (printer tetap bisa dipakai di PC ini)
Get-Printer | Where-Object Shared | ForEach-Object { Set-Printer -Name $_.Name -Shared $false; "stop share printer: $($_.Name)" }
# Matikan Guest
Disable-LocalUser -Name 'Guest'; "Guest dimatikan"
'--- share tersisa'
Get-SmbShare | Format-Table Name, Path, Special -AutoSize | Out-String
"Folder masih ada: D:\2026=$(Test-Path 'D:\2026')  D:\TES KONEKSI=$(Test-Path 'D:\TES KONEKSI')"
Get-LocalUser Guest | Format-List Name, Enabled | Out-String
