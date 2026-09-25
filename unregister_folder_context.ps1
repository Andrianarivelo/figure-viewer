# Undo register_folder_context.ps1: remove both FigView folder verbs.
#
#   HKCU\Software\Classes\Directory\shell\FigView
#   HKCU\Software\Classes\Directory\Background\shell\FigView
#
# It never touches any other program's verb on Directory or Directory\Background.
#
# Usage
#   powershell -ExecutionPolicy Bypass -File <app folder>\unregister_folder_context.ps1
#
# Options
#   -WhatIfOnly   list what would be removed and change nothing

[CmdletBinding()]
param(
    [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"

$verbs = @(
    "HKCU:\Software\Classes\Directory\shell\FigView",
    "HKCU:\Software\Classes\Directory\Background\shell\FigView"
)

$removed = 0

foreach ($verb in $verbs) {
    if (-not (Test-Path $verb)) { continue }
    if ($WhatIfOnly) {
        Write-Host "  would remove  $verb"
    } else {
        try {
            Remove-Item -Path $verb -Recurse -Force
            Write-Host "  removed  $verb"
        } catch {
            Write-Warning "could not remove $verb : $($_.Exception.Message)"
            continue
        }
    }
    $removed++
}

if (-not $WhatIfOnly -and $removed -gt 0) {
    try {
        Add-Type -Namespace FVShell -Name FolderNotify -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, uint flags, System.IntPtr item1, System.IntPtr item2);
'@ -ErrorAction Stop
        [FVShell.FolderNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
    } catch { }
}

Write-Host ""
if ($removed -eq 0) {
    Write-Host "Nothing to remove: FigView was not registered on the folder context menu."
} elseif ($WhatIfOnly) {
    Write-Host "$removed entries would be removed. Re-run without -WhatIfOnly to do it."
} else {
    Write-Host "$removed entries removed."
}
