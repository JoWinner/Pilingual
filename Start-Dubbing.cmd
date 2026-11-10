@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" airpods_call.py %*
pause
