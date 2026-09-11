# Create (or refresh) the Start Menu and Desktop shortcuts for FigView.
#
# Portable shortcuts target FigView.exe. Source checkouts use pythonw.exe and
# launch_figure_viewer.py, which records startup failures without a console.
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
$Icon     = Join-Path $AppDir "figure-viewer.ico"

# Pick an interpreter that can actually run the GUI. Several Pythons are usually
# on PATH and only some have Pillow, so each candidate is probed rather than
# assumed. A hardcoded Anaconda path was what broke this script in the first
# place. The probing itself lives in viewer_python.ps1 so that this script and
# register_file_types.ps1 can never disagree about which Python to use.
. (Join-Path $AppDir "viewer_python.ps1")
. (Join-Path $AppDir "viewer_shell.ps1")

$Launch = Resolve-ViewerLaunch -AppDir $AppDir
$PersistentIcon = Resolve-ViewerShellIcon -AppDir $AppDir
if ($PersistentIcon) { $Icon = $PersistentIcon }
if (-not (Test-Path -LiteralPath $Icon)) { $Icon = $Launch.Target }

$Shell     = New-Object -ComObject WScript.Shell
$StartMenu = Join-Path $Shell.SpecialFolders("Programs") ""
$Desktop   = $Shell.SpecialFolders("Desktop")

function New-ViewerShortcut {
    param([string]$Path, [string]$Arguments, [string]$Description)

    $link = $Shell.CreateShortcut($Path)
    $link.TargetPath       = $Launch.Target
    $link.Arguments        = ($Launch.Arguments + $Arguments).Trim()
    $link.WorkingDirectory = $Launch.WorkingDirectory
    if (Test-Path $Icon) { $link.IconLocation = "$Icon,0" }
    $link.Description      = $Description
    $link.WindowStyle      = 1
    $link.Save()
    Send-ViewerShellIconChange -Paths @($Path)
    Write-Host "  created  $Path"
}

Write-Host "FigView shortcuts"
Write-Host "  app dir  $AppDir"
Write-Host "  target   $($Launch.Target)"

# Rename legacy links in place first, preserving any folder-specific arguments.
foreach ($location in @($StartMenu, $Desktop)) {
    Get-ChildItem -LiteralPath $location -Filter "Figure Viewer*.lnk" `
                  -ErrorAction SilentlyContinue | ForEach-Object {
        $newName = $_.Name -replace '^Figure Viewer', 'FigView'
        $newPath = Join-Path $location $newName
        if (Test-Path -LiteralPath $newPath) {
            Remove-Item -LiteralPath $_.FullName -Force
        } else {
            Move-Item -LiteralPath $_.FullName -Destination $newPath
        }
        Write-Host "  migrated $($_.Name) -> $newName"
    }
}

New-ViewerShortcut -Path (Join-Path $StartMenu "FigView.lnk") -Arguments "" `
    -Description "FigView - browse images, videos and PDFs in any folder"

if (-not $NoDesktop) {
    New-ViewerShortcut -Path (Join-Path $Desktop "FigView.lnk") -Arguments "" `
        -Description "FigView - browse images, videos and PDFs in any folder"
}

if ($Folder -ne "") {
    $leaf = Split-Path $Folder -Leaf
    New-ViewerShortcut -Path (Join-Path $StartMenu "FigView - $leaf.lnk") `
        -Arguments (' "{0}"' -f $Folder) `
        -Description "FigView opened on $Folder"
}

Write-Host "Done. Press the Windows key and type 'FigView'."
