@echo off
cd /d "%~dp0"
echo Abriendo http://localhost:5000  (Ctrl+C para detener)
start "" http://localhost:5000
python wsgi.py
