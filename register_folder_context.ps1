# Add "Open with FigView" to a folder's right-click menu, and to the
# right-click menu on empty space inside a folder (opens the folder you are
# in). Neither touches the double-click action: browsing folders in Explorer
# is unchanged, this only adds an extra verb.
#
# What gets written
# ------------------
#   HKCU\Software\Classes\Directory\shell\FigView             right-click a folder
#   HKCU\Software\Classes\Directory\Background\shell\FigView  right-click empty space
#
# Everything is written under HKEY_CURRENT_USER: no administrator rights are
# needed and no other account on the machine is touched.
# unregister_folder_context.ps1 removes both verbs.
#
# Usage
#   powershell -ExecutionPolicy Bypass -File <app folder>\register_folder_context.ps1

[CmdletBinding()]
param(
    [string]$ExecutablePath = ""
)

$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Icon   = Join-Path $AppDir "figure-viewer.ico"

. (Join-Path $AppDir "viewer_python.ps1")
. (Join-Path $AppDir "viewer_shell.ps1")

$Launch = Resolve-ViewerLaunch -AppDir $AppDir -SourceScript "figure_viewer_app.py" -ExecutablePath $ExecutablePath

# %1 is the clicked folder itself; %V is the current folder for a background
# click, where there is no selected item to substitute.
$FolderCommand     = ('"{0}" {1} "%1"' -f $Launch.Target, $Launch.Arguments)
$BackgroundCommand = ('"{0}" {1} "%V"' -f $Launch.Target, $Launch.Arguments)
if ($Launch.IsFrozen) {
    $FolderCommand     = '"{0}" "%1"' -f $Launch.Target
    $BackgroundCommand = '"{0}" "%V"' -f $Launch.Target
}

$PersistentIcon = Resolve-ViewerShellIcon -AppDir $AppDir
if ($PersistentIcon) { $Icon = $PersistentIcon }
elseif ($Launch.IsFrozen -or -not (Test-Path -LiteralPath $Icon)) { $Icon = $Launch.Target }

Write-Host "FigView folder context menu registration"
Write-Host "  app dir  $AppDir"
Write-Host "  target   $($Launch.Target)"
Write-Host ""

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

$folderVerb     = "HKCU:\Software\Classes\Directory\shell\FigView"
$backgroundVerb = "HKCU:\Software\Classes\Directory\Background\shell\FigView"

Set-RegValue -Path $folderVerb -Name "(Default)" -Value "Open with FigView"
if (Test-Path $Icon) { Set-RegValue -Path $folderVerb -Name "Icon" -Value "$Icon,0" }
Set-RegValue -Path (Join-Path $folderVerb "command") -Name "(Default)" -Value $FolderCommand
Write-Host "  registered  Directory\shell               -> right-click a folder"

Set-RegValue -Path $backgroundVerb -Name "(Default)" -Value "Open with FigView"
if (Test-Path $Icon) { Set-RegValue -Path $backgroundVerb -Name "Icon" -Value "$Icon,0" }
Set-RegValue -Path (Join-Path $backgroundVerb "command") -Name "(Default)" -Value $BackgroundCommand
Write-Host "  registered  Directory\Background\shell    -> right-click empty space in a folder"

try {
    Send-ViewerShellIconChange
} catch {
    Write-Host "  (could not refresh the shell association cache; sign out and in if the menu looks stale)"
}

Write-Host ""
Write-Host "Registered under HKEY_CURRENT_USER. No administrator rights were used."
Write-Host "Right-click any folder, or empty space inside one, for 'Open with FigView'."
