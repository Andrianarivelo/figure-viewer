@echo off
rem Regenerate the icon assets with the same interpreter as the viewer.
setlocal
cd /d "%~dp0"
rem Development dependencies: python -m pip install -r requirements-icon.txt
python build_icon.py
if errorlevel 1 pause
