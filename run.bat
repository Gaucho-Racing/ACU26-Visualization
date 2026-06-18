@echo off
REM Live ACU monitor on COM5 @ 2,000,000 baud
cd /d "%~dp0"
python acu_server.py %*
pause
