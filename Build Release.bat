@echo off
rem Build, test and package the Windows release from this repository.
cd /d "%~dp0"
python build_release.py %*
if errorlevel 1 pause
