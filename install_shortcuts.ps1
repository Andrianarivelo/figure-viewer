# Create (or refresh) the Start Menu and Desktop shortcuts for Figure Viewer.
#
# The shortcuts point straight at pythonw.exe rather than at the .bat file, so
# launching the app never flashes a console window. launch_figure_viewer.py is
# used as the target script because it logs any startup failure to
# figure_viewer_launcher.log instead of failing silently under pythonw.
#
# IMPORTANT: shortcuts store absolute paths. Re-run this script after moving the
# app folder, otherwise the old shortcuts point at a directory that no longer
# exists and clicking them does nothing at all - no error, no window. All paths
# below are derived from this script's own location, so running it from the new
# folder is the whole fix.
#
# Usage:   powershell -ExecutionPolicy Bypass -File <app folder>\install_shortcuts.ps1
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

if (-not (Test-Path $Launcher)) { throw "Missing launcher: $Launcher" }

# Pick an interpreter that can actually run the GUI. Several Pythons are usually
# on PATH and only some have Pillow, so each candidate is probed rather than
# assumed. A hardcoded Anaconda path was what broke this script in the first
# place. The probing itself lives in viewer_python.ps1 so that this script and
# register_file_types.ps1 can never disagree about which Python to use.
. (Join-Path $AppDir "viewer_python.ps1")

$Python = Resolve-ViewerPython

# Use the windowless build for the shortcut so no console flashes on launch.
$Pythonw = Get-WindowlessPython -Python $Python

$Shell     = New-Object -ComObject WScript.Shell
$StartMenu = Join-Path $Shell.SpecialFolders("Programs") ""
$Desktop   = $Shell.SpecialFolders("Desktop")

function New-ViewerShortcut {
    param([string]$Path, [string]$Arguments, [string]$Description)

    $link = $Shell.CreateShortcut($Path)
    $link.TargetPath       = $Pythonw
    $link.Arguments        = ('"{0}"{1}' -f $Launcher, $Arguments)
    $link.WorkingDirectory = $AppDir
    if (Test-Path $Icon) { $link.IconLocation = "$Icon,0" }
    $link.Description      = $Description
    $link.WindowStyle      = 1
    $link.Save()
    Write-Host "  created  $Path"
}

Write-Host "Figure Viewer shortcuts"
Write-Host "  app dir  $AppDir"
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
