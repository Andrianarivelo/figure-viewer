# Undo register_file_types.ps1: remove every FigView file-type entry.
#
# This deletes only keys this app created, all of them under HKEY_CURRENT_USER:
#
#   HKCU\Software\Classes\FigureViewer.<ext>              the ProgIDs
#   HKCU\Software\Classes\<ext>\OpenWithProgids\...       the "Open with" offers
#   HKCU\Software\FigureViewer                            the capability block
#   HKCU\Software\RegisteredApplications\FigView          the app registration
#
# It never touches another program's ProgID, and it never touches the extension
# keys themselves - only this app's value inside their OpenWithProgids.
#
# If FigView was the *default* for a type, Windows falls back to whatever
# else is registered for it. Windows owns that choice (see the long note in
# register_file_types.ps1), so re-pick a default in
# Settings > Apps > Default apps if a type is left without one.
#
# Usage
#   powershell -ExecutionPolicy Bypass -File <app folder>\unregister_file_types.ps1
#
# Options
#   -WhatIfOnly   list what would be removed and change nothing

[CmdletBinding()]
param(
    [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"

$AppKeys      = @("FigView", "Figure Viewer")
$ProgIdPrefix = "FigureViewer"
$classes      = "HKCU:\Software\Classes"

$removed = 0

function Remove-Thing {
    param([string]$What, [scriptblock]$Action)
    if ($WhatIfOnly) {
        Write-Host "  would remove  $What"
    } else {
        try {
            & $Action
            Write-Host "  removed  $What"
        } catch {
            Write-Warning "could not remove $What : $($_.Exception.Message)"
            return
        }
    }
    $script:removed++
}

# --- the ProgIDs, found by prefix so a changed extension list cannot orphan any
Get-ChildItem -Path $classes -ErrorAction SilentlyContinue |
    Where-Object { $_.PSChildName -like "$ProgIdPrefix.*" } |
    ForEach-Object {
        $path = $_.PSPath
        $name = $_.PSChildName
        Remove-Thing -What "ProgID $name" -Action { Remove-Item -Path $path -Recurse -Force }
    }

# --- the "Open with" offers, leaving each extension key itself intact
Get-ChildItem -Path $classes -ErrorAction SilentlyContinue |
    Where-Object { $_.PSChildName -like ".*" } |
    ForEach-Object {
        $owp = Join-Path $_.PSPath "OpenWithProgids"
        if (-not (Test-Path $owp)) { return }
        $props = Get-ItemProperty -Path $owp -ErrorAction SilentlyContinue
        if ($null -eq $props) { return }
        foreach ($prop in $props.PSObject.Properties) {
            if ($prop.Name -like "$ProgIdPrefix.*") {
                $ext = $_.PSChildName
                $n = $prop.Name
                Remove-Thing -What "OpenWithProgids $ext -> $n" `
                             -Action { Remove-ItemProperty -Path $owp -Name $n -Force }
            }
        }
    }

# --- the "Combine with FigView" verb on PDFs
$combineVerb = Join-Path $classes "SystemFileAssociations\.pdf\shell\FigView.Combine"
if (Test-Path $combineVerb) {
    Remove-Thing -What "verb SystemFileAssociations\.pdf\shell\FigView.Combine" `
                 -Action { Remove-Item -Path $combineVerb -Recurse -Force }
}

# --- the capability block and the app registration
if (Test-Path "HKCU:\Software\FigureViewer") {
    Remove-Thing -What "capabilities HKCU\Software\FigureViewer" `
                 -Action { Remove-Item -Path "HKCU:\Software\FigureViewer" -Recurse -Force }
}

$regApps = "HKCU:\Software\RegisteredApplications"
if (Test-Path $regApps) {
    $props = Get-ItemProperty -Path $regApps -ErrorAction SilentlyContinue
    foreach ($appKey in $AppKeys) {
        if ($null -ne $props -and $null -ne $props.PSObject.Properties[$appKey]) {
            $keyToRemove = $appKey
            Remove-Thing -What "RegisteredApplications\$keyToRemove" `
                         -Action { Remove-ItemProperty -Path $regApps -Name $keyToRemove -Force }
        }
    }
}

# --- refresh Explorer's association cache so icons update immediately
if (-not $WhatIfOnly) {
    try {
        Add-Type -Namespace FVShell -Name Notify -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, uint flags, System.IntPtr item1, System.IntPtr item2);
'@ -ErrorAction Stop
        [FVShell.Notify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
    } catch { }
}

Write-Host ""
if ($removed -eq 0) {
    Write-Host "Nothing to remove: FigView was not registered for any file type."
} elseif ($WhatIfOnly) {
    Write-Host "$removed entries would be removed. Re-run without -WhatIfOnly to do it."
} else {
    Write-Host "$removed entries removed."
}
