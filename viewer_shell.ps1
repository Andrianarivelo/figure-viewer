# Persistent, content-addressed Windows shell icons and conservative link matching.
# Dot-source this helper; importing it performs no writes or shell notifications.

function Resolve-ViewerShellIcon {
    <# Copy the bundled icon outside temporary one-file extraction directories.
       Each content hash has its own stable path, giving Explorer a new cache key.
       Older files remain available to existing shortcuts and associations. #>
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([Parameter(Mandatory = $true)][string]$AppDir)
    $sourceIcon = Join-Path $AppDir 'figure-viewer.ico'
    if (-not (Test-Path -LiteralPath $sourceIcon -PathType Leaf)) { return $null }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA is unavailable; cannot persist the shell icon.'
    }
    $hash = (Get-FileHash -LiteralPath $sourceIcon -Algorithm SHA256).Hash.ToLowerInvariant()
    $iconDirectory = Join-Path $env:LOCALAPPDATA 'FigView\Icons'
    $persistentIcon = Join-Path $iconDirectory ('FigView-' + $hash.Substring(0, 20) + '.ico')
    $needsCopy = -not (Test-Path -LiteralPath $persistentIcon -PathType Leaf)
    if (-not $needsCopy) {
        $needsCopy = (Get-FileHash -LiteralPath $persistentIcon -Algorithm SHA256).Hash -ne $hash
    }
    if ($needsCopy -and $PSCmdlet.ShouldProcess($persistentIcon, 'Store persistent FigView icon')) {
        New-Item -ItemType Directory -Path $iconDirectory -Force | Out-Null
        Copy-Item -LiteralPath $sourceIcon -Destination $persistentIcon -Force
    }
    return $persistentIcon
}

function Test-ViewerShortcut {
    <# Match actual launch identity, never a shortcut's visible name alone. #>
    param([string]$TargetPath, [string]$Arguments,
          [Parameter(Mandatory = $true)][string]$AppDir)
    if ([string]::IsNullOrWhiteSpace($TargetPath)) { return $false }
    $targetName = [IO.Path]::GetFileName($TargetPath)
    if ($targetName -ieq 'FigView.exe') { return $true }
    # Exact standalone release naming contract from FigView.spec. Anchoring
    # rejects lookalike prefixes, suffixes and unrelated executable names.
    if ($targetName -match '^FigView-v\d+\.\d+\.\d+-windows-x64\.exe$') { return $true }
    foreach ($script in @('FigView.bat', 'Figure Viewer.bat')) {
        if ($TargetPath -ieq (Join-Path $AppDir $script)) { return $true }
    }
    if ($targetName -notmatch '^pythonw?(?:\d+(?:\.\d+)*)?\.exe$') { return $false }
    foreach ($script in @('launch_figure_viewer.py', 'figure_viewer_app.py')) {
        $scriptPath = Join-Path $AppDir $script
        # Windows launcher arguments put the script first; allow interpreter
        # flags before it, but require a complete quoted or whitespace token.
        $escaped = [regex]::Escape($scriptPath)
        if ($Arguments -match ('(?i)^\s*(?:-(?:[BuEsSIOqVx]|W\S+|X\S+)\s+)*(?:"' + $escaped + '"|' + $escaped + ')(?=\s|$)')) {
            return $true
        }
    }
    return $false
}

function Send-ViewerShellIconChange {
    <# Use documented shell notifications without restarting Explorer or
       deleting icon caches. Item paths are Unicode SHCNF_PATHW strings. #>
    [CmdletBinding()]
    param([string[]]$Paths = @())
    if (-not ('FigViewShell.IconNotify' -as [type])) {
        Add-Type -Namespace FigViewShell -Name IconNotify -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, uint flags, System.IntPtr item1, System.IntPtr item2);
'@
    }
    # SHCNE_ASSOCCHANGED, SHCNF_IDLIST
    [FigViewShell.IconNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
    foreach ($path in $Paths) {
        $pointer = [Runtime.InteropServices.Marshal]::StringToHGlobalUni($path)
        try {
            # SHCNE_UPDATEITEM, SHCNF_PATHW
            [FigViewShell.IconNotify]::SHChangeNotify(0x00002000, 0x0005, $pointer, [IntPtr]::Zero)
        } finally {
            [Runtime.InteropServices.Marshal]::FreeHGlobal($pointer)
        }
    }
}
