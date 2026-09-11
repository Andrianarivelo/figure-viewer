# Refresh existing FigView shell icon references. Use -WhatIf to preview.
# Existing associations, launch commands and shortcut properties are preserved.
[CmdletBinding(SupportsShouldProcess = $true)]
param([string]$AppDir = '')
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($AppDir)) { $AppDir = $PSScriptRoot }
. (Join-Path $PSScriptRoot 'viewer_shell.ps1')

$icon = Resolve-ViewerShellIcon -AppDir $AppDir
if (-not $icon) {
    Write-Warning 'No figure-viewer.ico found; existing shell icons were left unchanged.'
    return
}
$iconLocation = "$icon,0"
$changedPaths = New-Object System.Collections.Generic.List[string]

# Open only existing per-user application keys. No extension or UserChoice writes.
$classes = 'HKCU:\Software\Classes'
Get-ChildItem -LiteralPath $classes -ErrorAction SilentlyContinue |
    Where-Object { $_.PSChildName -like 'FigureViewer.*' } | ForEach-Object {
        $iconKey = Join-Path $_.PSPath 'DefaultIcon'
        if ((Test-Path -LiteralPath $iconKey) -and
                $PSCmdlet.ShouldProcess($iconKey, "Update existing icon to $iconLocation")) {
            Set-Item -LiteralPath $iconKey -Value $iconLocation
        }
    }
$capabilities = 'HKCU:\Software\FigureViewer\Capabilities'
if (Test-Path -LiteralPath $capabilities) {
    $key = Get-Item -LiteralPath $capabilities
    if (($key.GetValueNames() -contains 'ApplicationIcon') -and
            $PSCmdlet.ShouldProcess($capabilities, 'Update existing ApplicationIcon')) {
        Set-ItemProperty -LiteralPath $capabilities -Name 'ApplicationIcon' -Value $iconLocation
    }
}

$shell = New-Object -ComObject WScript.Shell
try {
    $locations = @($shell.SpecialFolders('Desktop'), $shell.SpecialFolders('Programs'),
                  (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'))
    foreach ($location in ($locations | Select-Object -Unique)) {
        if (-not $location -or -not (Test-Path -LiteralPath $location)) { continue }
        Get-ChildItem -LiteralPath $location -Filter '*.lnk' -File -Recurse |
            ForEach-Object {
                $linkPath = $_.FullName
                $link = $null
                try {
                    $link = $shell.CreateShortcut($linkPath)
                    if ((Test-ViewerShortcut -TargetPath $link.TargetPath -Arguments $link.Arguments -AppDir $AppDir) -and
                            $PSCmdlet.ShouldProcess($linkPath, 'Update existing FigView shortcut icon')) {
                        $link.IconLocation = $iconLocation
                        $link.Save()
                        $changedPaths.Add($linkPath)
                    }
                } catch {
                    Write-Warning "Cannot refresh shortcut '$linkPath': $_"
                } finally {
                    if ($null -ne $link) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($link) }
                }
            }
    }
} finally {
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}
if ($PSCmdlet.ShouldProcess('Windows shell', 'Notify icon and association changes')) {
    Send-ViewerShellIconChange -Paths $changedPaths.ToArray()
}
