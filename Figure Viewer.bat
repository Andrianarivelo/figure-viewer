@echo off
rem Console launcher for Figure Viewer.
rem
rem This only has to start launch_figure_viewer.py, which uses nothing outside
rem the standard library and then picks a Pillow-capable interpreter for the GUI
rem itself. So any Python found here is good enough.
rem
rem It used to hardcode C:\Users\bellone\anaconda3\pythonw.exe, which exists on
rem one machine and one account, and hard-failed everywhere else.
setlocal
cd /d "%~dp0"

if not "%FIGURE_VIEWER_PYTHON%"=="" (
    start "" "%FIGURE_VIEWER_PYTHON%" "%~dp0launch_figure_viewer.py" %*
    goto :eof
)

if exist "%USERPROFILE%\miniconda3\pythonw.exe" (
    start "" "%USERPROFILE%\miniconda3\pythonw.exe" "%~dp0launch_figure_viewer.py" %*
    goto :eof
)

if exist "%USERPROFILE%\anaconda3\pythonw.exe" (
    start "" "%USERPROFILE%\anaconda3\pythonw.exe" "%~dp0launch_figure_viewer.py" %*
    goto :eof
)

where pythonw.exe >nul 2>&1
if %ERRORLEVEL%==0 (
    start "" pythonw.exe "%~dp0launch_figure_viewer.py" %*
    goto :eof
)

where python.exe >nul 2>&1
if %ERRORLEVEL%==0 (
    python.exe "%~dp0launch_figure_viewer.py" %*
    goto :eof
)

echo No Python interpreter found. Install Python, or set FIGURE_VIEWER_PYTHON
echo to the full path of a python.exe that has Pillow installed.
pause
