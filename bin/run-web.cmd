@echo off
rem Tala Render web (pratinjau) - otomatis jalan ulang kalau berhenti
cd /d D:\RenderServer\app
:loop
echo [%date% %time%] web start >> D:\RenderServer\logs\supervisor.log
D:\RenderServer\app\blender\5.2\python\bin\python.exe -u D:\RenderServer\app\bin\app.py >> D:\RenderServer\logs\web.log 2>&1
echo [%date% %time%] web berhenti (exit %errorlevel%), restart 5 detik >> D:\RenderServer\logs\supervisor.log
timeout /t 5 /nobreak > nul
goto loop
