@echo off
cd /d "%~dp0"
echo Respaldo completo y solo de la app (se guardan en la carpeta respaldos)
python respaldo.py exportar
python respaldo.py exportar --solo-app
pause
