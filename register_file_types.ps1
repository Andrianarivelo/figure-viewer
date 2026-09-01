# Register FigView with Windows as an image, video, and PDF viewer.
#
# What this does, and what it deliberately does not do
# ----------------------------------------------------
# Since Windows 8, a program cannot simply take over a file type. The final
# choice lives in
#
#     HKCU\...\FileExts\<ext>\UserChoice
#
# and is protected by a per-user, per-extension hash that only Explorer and the
# Settings app know how to produce. Anything that writes it directly is either
# reverted by Windows or breaks the association outright. Tools that claim to
# "set the default silently" are forging that hash, and Windows removes them.
#
# So this script does everything that IS legitimate:
#
#   1. a ProgID per extension, with the app's icon, a friendly type name and an
#      open command  ->  FigView appears in "Open with";
#   2. an OpenWithProgids entry on each extension  ->  it stays in that list;
#   3. a RegisteredApplications capability block  ->  FigView appears in
#      Settings > Default apps as a real application, where one click per type
#      (or the "Set default" button on Windows 11) makes it the default.
#
# and then opens that Settings page for the final click.
#
# Everything is written under HKEY_CURRENT_USER: no administrator rights are
# needed and no other account on the machine is touched.
# unregister_file_types.ps1 removes all of it.
#
# Usage
#   powershell -ExecutionPolicy Bypass -File <app folder>\register_file_types.ps1
#
# Options
#   -IncludeVector      also claim .svg .eps .xps .oxps .cbz .epub
#   -Extensions a,b,c   register exactly these extensions instead of the defaults
#   -NoSettings         register only; do not open Settings > Default apps

[CmdletBinding()]
param(
    [switch]$IncludeVector,
    [string[]]$Extensions,
    [switch]$NoSettings
)

$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $AppDir "figure_viewer_app.py"
$Icon   = Join-Path $AppDir "figure-viewer.ico"

if (-not (Test-Path $Script)) { throw "Missing GUI script: $Script" }

. (Join-Path $AppDir "viewer_python.ps1")

# --------------------------------------------------------------- identity
$AppKey       = "FigView"                            # public Windows app name
$LegacyAppKey = "Figure Viewer"                      # removed after migration
# These identifiers deliberately stay stable. Windows UserChoice hashes may
# already reference FigureViewer.pdf, and replacing that ProgID would invalidate
# a user's existing association even though only the public app name changed.
$CapPath      = "Software\FigureViewer\Capabilities"
$ProgIdPrefix = "FigureViewer"

# ------------------------------------------------------------ extensions
# Raster and common video formats, plus PDF. Vector formats are opt-in because a
# browser or Illustrator is usually the better default for .svg and .eps.
$DefaultExtensions = @(".png", ".jpg", ".jpeg", ".jpe", ".tif", ".tiff",
                       ".bmp", ".gif", ".webp", ".jp2", ".ppm", ".pgm", ".pdf",
                       ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv",
                       ".mpg", ".mpeg", ".mpe", ".ogv", ".mts", ".m2ts", ".ts")
$VectorExtensions  = @(".svg", ".eps", ".xps", ".oxps", ".cbz", ".epub")

if ($Extensions -and $Extensions.Count -gt 0) {
    $Targets = $Extensions | ForEach-Object {
        $e = $_.Trim().ToLower()
        if (-not $e.StartsWith(".")) { $e = "." + $e }
        $e
    }
} else {
    $Targets = $DefaultExtensions
    if ($IncludeVector) { $Targets = $Targets + $VectorExtensions }
}

# Names shown by Explorer in the "Type" column, so the entries do not all read
# "FigureViewer.png file".
$TypeNames = @{
    ".png"  = "PNG Image";           ".jpg"  = "JPEG Image"
    ".jpeg" = "JPEG Image";          ".jpe"  = "JPEG Image"
    ".tif"  = "TIFF Image";          ".tiff" = "TIFF Image"
    ".bmp"  = "Bitmap Image";        ".gif"  = "GIF Image"
    ".webp" = "WebP Image";          ".jp2"  = "JPEG 2000 Image"
    ".ppm"  = "Portable Pixmap";     ".pgm"  = "Portable Graymap"
    ".pdf"  = "PDF Document";        ".svg"  = "SVG Document"
    ".eps"  = "EPS Document";        ".xps"  = "XPS Document"
    ".oxps" = "OpenXPS Document";    ".cbz"  = "Comic Book Archive"
    ".epub" = "EPUB Document";      ".mp4"  = "MPEG-4 Video"
    ".m4v"  = "MPEG-4 Video";        ".mov"  = "QuickTime Video"
    ".avi"  = "AVI Video";           ".mkv"  = "Matroska Video"
    ".webm" = "WebM Video";          ".wmv"  = "Windows Media Video"
    ".mpg"  = "MPEG Video";          ".mpeg" = "MPEG Video"
    ".mpe"  = "MPEG Video"
    ".ogv"  = "Ogg Video";           ".mts"  = "AVCHD Video"
    ".m2ts" = "MPEG-2 Transport";    ".ts"   = "MPEG Transport Stream"
}

# ------------------------------------------------------------ interpreter
$Python  = Resolve-ViewerPython
$Pythonw = Get-WindowlessPython -Python $Python
$Command = '"{0}" "{1}" "%1"' -f $Pythonw, $Script

Write-Host "FigView file-type registration"
Write-Host "  app dir  $AppDir"
Write-Host "  python   $Pythonw"
Write-Host "  types    $($Targets -join ' ')"
Write-Host ""

# PDFs need PyMuPDF; say so now rather than after the association is live.
if ($Targets -contains ".pdf") {
    $probe = $Python
    if ([IO.Path]::GetFileName($Python).ToLower() -eq "pythonw.exe") {
        $sibling = Join-Path (Split-Path -Parent $Python) "python.exe"
        if (Test-Path $sibling) { $probe = $sibling }
    }
    & $probe -c "import pymupdf" 2>$null
    if ($LASTEXITCODE -ne 0) {
        & $probe -c "import fitz" 2>$null
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "PyMuPDF is not installed for $probe, so PDFs will open with an install hint instead of a page. Fix with:  $probe -m pip install pymupdf"
    }
}

$VideoExtensions = @(".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv",
                     ".mpg", ".mpeg", ".mpe", ".ogv", ".mts", ".m2ts", ".ts")
if ($Targets | Where-Object { $VideoExtensions -contains $_ }) {
    & $Python -c "import cv2" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "OpenCV is not installed for $Python, so videos will show an install hint. Fix with:  $Python -m pip install opencv-python"
    }
}

# ------------------------------------------------------------- registry
function Set-RegValue {
    <#  Create a key if needed and write one value. "(Default)" writes the
        key's unnamed value, which is what the shell reads for commands.  #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )
    if (-not (Test-Path $Path)) { New-Item -Path $Path -Force | Out-Null }
    New-ItemProperty -Path $Path -Name $Name -Value $Value -PropertyType String -Force | Out-Null
}

$classes = "HKCU:\Software\Classes"

# Capabilities describe the exact set offered by Windows' "Set default" button.
# Rebuild this app-owned subkey on every run so `-Extensions .pdf` cannot leave
# stale image/video values from a previous broader registration behind.
$associationsPath = "HKCU:\$CapPath\FileAssociations"
if (Test-Path $associationsPath) {
    Remove-Item -Path $associationsPath -Recurse -Force
}

foreach ($ext in $Targets) {
    $progId = "$ProgIdPrefix$ext"                      # e.g. FigureViewer.png
    $label  = $TypeNames[$ext]
    if ([string]::IsNullOrEmpty($label)) { $label = "$($ext.TrimStart('.').ToUpper()) File" }

    $progPath = Join-Path $classes $progId

    # 1. the ProgID itself: what Explorer shows and what double-clicking runs
    Set-RegValue -Path $progPath -Name "(Default)"       -Value $label
    Set-RegValue -Path $progPath -Name "FriendlyTypeName" -Value $label
    if (Test-Path $Icon) {
        Set-RegValue -Path (Join-Path $progPath "DefaultIcon") -Name "(Default)" -Value "$Icon,0"
    }
    Set-RegValue -Path (Join-Path $progPath "shell") -Name "(Default)" -Value "open"

    $openPath = Join-Path $progPath "shell\open"
    Set-RegValue -Path $openPath -Name "(Default)"       -Value "&Open with FigView"
    # Without this the "Open with" list would say "pythonw", the interpreter.
    Set-RegValue -Path $openPath -Name "FriendlyAppName" -Value "FigView"
    Set-RegValue -Path (Join-Path $openPath "command") -Name "(Default)" -Value $Command

    # 2. offer the app for this extension without disturbing the current default
    Set-RegValue -Path (Join-Path (Join-Path $classes $ext) "OpenWithProgids") `
                 -Name $progId -Value ""

    # 3. declare the association as an app capability, for Settings > Default apps
    Set-RegValue -Path "HKCU:\$CapPath\FileAssociations" -Name $ext -Value $progId

    Write-Host ("  registered  {0,-6}  ->  {1}" -f $ext, $progId)
}

# Existing raster ProgIDs may remain outside `$Targets` during a PDF-only run.
# Refresh their visible verb/app labels too, without deleting or replacing the
# stable IDs that a protected UserChoice may already reference.
Get-ChildItem -Path $classes -ErrorAction SilentlyContinue |
    Where-Object { $_.PSChildName -like "$ProgIdPrefix.*" } |
    ForEach-Object {
        $openPath = Join-Path $_.PSPath "shell\open"
        if (Test-Path $openPath) {
            Set-RegValue -Path $openPath -Name "(Default)" -Value "&Open with FigView"
            Set-RegValue -Path $openPath -Name "FriendlyAppName" -Value "FigView"
        }
    }

Set-RegValue -Path "HKCU:\$CapPath" -Name "ApplicationName" -Value "FigView"
Set-RegValue -Path "HKCU:\$CapPath" -Name "ApplicationDescription" `
             -Value "Fast folder-based viewer for images, videos and PDFs, with live reload, snipping and multi-level subfolder browsing."
if (Test-Path $Icon) {
    Set-RegValue -Path "HKCU:\$CapPath" -Name "ApplicationIcon" -Value "$Icon,0"
}
Set-RegValue -Path "HKCU:\Software\RegisteredApplications" -Name $AppKey -Value $CapPath

# Avoid presenting both the old and new public names in Default apps. The
# capability path and ProgIDs remain in place, so this does not break existing
# associations.
$registeredApps = "HKCU:\Software\RegisteredApplications"
$registered = Get-ItemProperty -Path $registeredApps -ErrorAction SilentlyContinue
if ($null -ne $registered -and
        $null -ne $registered.PSObject.Properties[$LegacyAppKey]) {
    Remove-ItemProperty -Path $registeredApps -Name $LegacyAppKey -Force
}

# ------------------------------------------------- tell the shell to refresh
# Without this, Explorer keeps showing the old icons and the old "Open with"
# list until the next sign-in.
try {
    Add-Type -Namespace FVShell -Name Notify -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, uint flags, System.IntPtr item1, System.IntPtr item2);
'@ -ErrorAction Stop
    # SHCNE_ASSOCCHANGED (0x08000000), SHCNF_IDLIST (0x0000)
    [FVShell.Notify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
} catch {
    Write-Host "  (could not refresh the shell association cache; sign out and in if icons look stale)"
}

Write-Host ""
Write-Host "Registered under HKEY_CURRENT_USER. No administrator rights were used."
Write-Host ""
Write-Host "One step is left, and only Windows itself is allowed to do it:"
Write-Host "  Settings > Apps > Default apps > FigView,"
Write-Host "  then press 'Set default', or pick a file type and choose FigView."
Write-Host ""
Write-Host "Per file: right-click any PNG or PDF > Open with > Choose another app >"
Write-Host "  FigView > 'Always use this app'."

if (-not $NoSettings) {
    try {
        # Windows 11 deep-links straight to this app's page; older builds fall
        # back to the Default apps list, which is still the right place.
        Start-Process ("ms-settings:defaultapps?registeredAppUser=" + [Uri]::EscapeDataString($AppKey))
    } catch {
        try { Start-Process "ms-settings:defaultapps" } catch { }
    }
}
