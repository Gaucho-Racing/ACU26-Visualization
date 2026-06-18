@echo off
REM Demo mode - animates serial.txt, no hardware needed
cd /d "%~dp0"
python acu_server.py --demo %*
pause
