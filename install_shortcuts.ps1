# Create (or refresh) the Start Menu and Desktop shortcuts for Figure Viewer.
#
# The shortcuts point straight at pythonw.exe rather than at the .bat file, so
# launching the app never flashes a console window. launch_figure_viewer.py is
# used as the target script because it logs any startup failure to
# figure_viewer_launcher.log instead of failing silently under pythonw.
#
# Usage:   powershell -ExecutionPolicy Bypass -File D:\FigureViewerApp\install_shortcuts.ps1
# Options: -NoDesktop        skip the Desktop shortcut
#          -Folder <path>    also create a shortcut that opens a specific folder

param(
    [switch]$NoDesktop,
    [string]$Folder = ""
)

$ErrorActionPreference = "Stop"

$AppDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $AppDir "launch_figure_viewer.py"
$Icon     = Join-Path $AppDir "figure-viewer.ico"

# Prefer the Anaconda interpreter the app was installed against, then fall back
# to whatever pythonw.exe is on PATH.
$Pythonw = "C:\Users\bellone\anaconda3\pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($null -eq $found) { throw "pythonw.exe not found. Install Python or edit this script." }
    $Pythonw = $found.Source
}
if (-not (Test-Path $Launcher)) { throw "Missing launcher: $Launcher" }

$Shell     = New-Object -ComObject WScript.Shell
$StartMenu = Join-Path $Shell.SpecialFolders("Programs") ""
$Desktop   = $Shell.SpecialFolders("Desktop")

function New-ViewerShortcut {
    param([string]$Path, [string]$Arguments, [string]$Description)

    $link = $Shell.CreateShortcut($Path)
    $link.TargetPath       = $Pythonw
    $link.Arguments        = ('"{0}"{1}' -f $Launcher, $Arguments)
    $link.WorkingDirectory = $AppDir
    $link.IconLocation     = "$Icon,0"
    $link.Description      = $Description
    $link.WindowStyle      = 1
    $link.Save()
    Write-Host "  created  $Path"
}

Write-Host "Figure Viewer shortcuts"
Write-Host "  python   $Pythonw"

New-ViewerShortcut -Path (Join-Path $StartMenu "Figure Viewer.lnk") -Arguments "" `
    -Description "Figure Viewer - browse figures and PDFs in any folder"

if (-not $NoDesktop) {
    New-ViewerShortcut -Path (Join-Path $Desktop "Figure Viewer.lnk") -Arguments "" `
        -Description "Figure Viewer - browse figures and PDFs in any folder"
}

if ($Folder -ne "") {
    $leaf = Split-Path $Folder -Leaf
    New-ViewerShortcut -Path (Join-Path $StartMenu "Figure Viewer - $leaf.lnk") `
        -Arguments (' "{0}"' -f $Folder) `
        -Description "Figure Viewer opened on $Folder"
}

Write-Host "Done. Press the Windows key and type 'Figure Viewer'."
