@echo off
rem Lanceur SENTINEL-X : double-clic ou "lancer.bat" (options possibles, ex. lancer.bat --simulateur membre)
chcp 65001 >nul
cd /d "%~dp0"
python lancer.py %*
echo.
pause
