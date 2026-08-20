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
# place.
function Resolve-ViewerPython {
    $candidates = New-Object System.Collections.Generic.List[string]

    if ($env:FIGURE_VIEWER_PYTHON) { $candidates.Add($env:FIGURE_VIEWER_PYTHON) }
    foreach ($root in @("$env:USERPROFILE\miniconda3", "$env:USERPROFILE\anaconda3",
                        "$env:LOCALAPPDATA\miniconda3", "$env:LOCALAPPDATA\anaconda3")) {
        $candidates.Add((Join-Path $root "python.exe"))
    }
    foreach ($name in @("python.exe", "pythonw.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $cmd) { $candidates.Add($cmd.Source) }
    }

    $firstExisting = $null
    foreach ($c in $candidates) {
        if (-not (Test-Path $c)) { continue }
        # Probe with the console build so an exit code comes back.
        $probe = $c
        if ([IO.Path]::GetFileName($c).ToLower() -eq "pythonw.exe") {
            $sibling = Join-Path (Split-Path -Parent $c) "python.exe"
            if (Test-Path $sibling) { $probe = $sibling }
        }
        if ($null -eq $firstExisting) { $firstExisting = $c }
        & $probe -c "import PIL, tkinter" 2>$null
        if ($LASTEXITCODE -eq 0) { return $c }
        Write-Host "  skipped (no Pillow/tkinter)  $probe"
    }
    if ($null -ne $firstExisting) {
        Write-Warning "No interpreter with Pillow found; using $firstExisting anyway."
        return $firstExisting
    }
    throw "No Python interpreter found. Install Python or set FIGURE_VIEWER_PYTHON."
}

$Python = Resolve-ViewerPython

# Use the windowless build for the shortcut so no console flashes on launch.
$Pythonw = $Python
if ([IO.Path]::GetFileName($Python).ToLower() -eq "python.exe") {
    $sibling = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
    if (Test-Path $sibling) { $Pythonw = $sibling }
}

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
