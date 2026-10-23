# Tala Render — web render server (Blender + RTX 5090)

Web app untuk mengirim render Blender ke server RTX 5090 dari browser (LAN kantor).

## Struktur
- `bin/app.py` — server web: login, upload, analisa file, antrian, worker render, statistik GPU
- `bin/inspect_blend.py` — dijalankan di Blender: baca kamera, resolusi, frame, tekstur yang hilang
- `bin/render_job.py` — dijalankan di Blender: setup render per job, penanda progres `@@...@@`
- `bin/relink.py` — cari tekstur/aset yang hilang berdasarkan nama file di dalam upload .zip
- `bin/force_gpu.py` — paksa Cycles memakai GPU (OptiX → CUDA)
- `bin/users.py` — kelola akun (hash PBKDF2-SHA256)
- `bin/run-web.cmd` — supervisor: jalankan ulang app.py bila berhenti
- `bin/install-web.ps1` — pasang Scheduled Task `TalaRender-Web` (LOCAL SERVICE, saat boot) + firewall
- `web/` — `index.html` (aplikasi) dan `login.html`

## Tidak disimpan di git
- `config/users.json` — akun & hash password
- `blender/` — junction ke `C:\Program Files\Blender Foundation\Blender 5.2`
- `tools/` — ffmpeg (binary)
- Data runtime ada di luar folder ini: `D:\RenderServer\data`, `D:\RenderServer\logs`, `D:\RenderServer\Share`

## Perintah
- Akun: `D:\RenderServer\app\blender\5.2\python\bin\python.exe D:\RenderServer\app\bin\users.py list|add|passwd|remove <nama>`
- Restart web: hentikan proses yang memegang port 8080; supervisor menyalakannya lagi dalam 5 detik.
- Log: `D:\RenderServer\logs\web.log`
