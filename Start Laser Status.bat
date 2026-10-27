@echo off
rem Starts Laser Status without a console window.
cd /d "%~dp0"
start "" pythonw "%~dp0laser_status_app.py"
