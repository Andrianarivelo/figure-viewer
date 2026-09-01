@echo off
rem Console launcher for FigView.
rem
rem This only has to start launch_figure_viewer.py, which uses nothing outside
rem the standard library and then picks a Pillow-capable interpreter for the GUI.
setlocal
cd /d "%~dp0"

rem FIGVIEW_PYTHON is the public override. The historical variable remains a
rem fallback so existing local configurations continue to work after renaming.
if not "%FIGVIEW_PYTHON%"=="" (
    start "" "%FIGVIEW_PYTHON%" "%~dp0launch_figure_viewer.py" %*
    goto :eof
)

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

echo No Python interpreter found. Install Python, or set FIGVIEW_PYTHON
echo to the full path of a python.exe that has Pillow installed.
pause
