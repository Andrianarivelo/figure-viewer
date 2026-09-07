# Shared helper: find a Python interpreter that can actually run FigView.
#
# Dot-source it, do not run it:
#
#     . (Join-Path $PSScriptRoot "viewer_python.ps1")
#     $python = Resolve-ViewerPython
#
# Several Pythons are usually on PATH and only some of them have Pillow and
# tkinter, so every candidate is *probed* rather than assumed. Hardcoding an
# Anaconda path is what broke the shortcuts in the first place, which is why
# nothing here is hardcoded.

function Resolve-ViewerPython {
    <#
        .SYNOPSIS
            Return the path of a python.exe that can import PIL and tkinter.
        .DESCRIPTION
            Candidates are tried best-first: the FIGVIEW_PYTHON override, its
            historical FIGURE_VIEWER_PYTHON alias, the usual per-user conda
            installs, then whatever is on PATH.
            If none of them has the dependencies, the first interpreter that
            merely exists is returned with a warning, so the GUI can still start
            and show its own "Pillow is required" dialog.
    #>
    $candidates = New-Object System.Collections.Generic.List[string]

    if ($env:FIGVIEW_PYTHON) { $candidates.Add($env:FIGVIEW_PYTHON) }
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
        if ($null -eq $firstExisting) { $firstExisting = $c }

        # Probe with the console build so an exit code comes back.
        $probe = $c
        if ([IO.Path]::GetFileName($c).ToLower() -eq "pythonw.exe") {
            $sibling = Join-Path (Split-Path -Parent $c) "python.exe"
            if (Test-Path $sibling) { $probe = $sibling }
        }
        & $probe -c "import PIL, tkinter" 2>$null
        if ($LASTEXITCODE -eq 0) { return $c }
        Write-Host "  skipped (no Pillow/tkinter)  $probe"
    }
    if ($null -ne $firstExisting) {
        Write-Warning "No interpreter with Pillow found; using $firstExisting anyway."
        return $firstExisting
    }
    throw "No Python interpreter found. Install Python or set FIGVIEW_PYTHON."
}

function Get-WindowlessPython {
    <#
        .SYNOPSIS
            The pythonw.exe next to a python.exe, so no console flashes.
    #>
    param([Parameter(Mandatory = $true)][string]$Python)

    if ([IO.Path]::GetFileName($Python).ToLower() -eq "python.exe") {
        $sibling = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
        if (Test-Path $sibling) { return $sibling }
    }
    return $Python
}

function Resolve-ViewerLaunch {
    <#
        .SYNOPSIS
            Resolve a portable executable or the source checkout launcher.
        .DESCRIPTION
            Portable scripts may sit beside FigView.exe or in PyInstaller's
            _internal resource directory. A packaged app never probes Python.
            Arguments contains only the optional quoted source script; callers
            append their own file or folder argument.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$AppDir,
        [string]$SourceScript = "launch_figure_viewer.py",
        [string]$ExecutablePath = ""
    )

    $appRoot = [IO.Path]::GetFullPath($AppDir)
    $exe = Join-Path $appRoot "FigView.exe"
    # One-file builds extract scripts to a temporary resource directory. The
    # running app supplies its persistent executable for Windows registration.
    if ($ExecutablePath) {
        $exe = [IO.Path]::GetFullPath($ExecutablePath)
        if ([IO.Path]::GetExtension($exe) -ine ".exe" -or
                -not (Test-Path -LiteralPath $exe -PathType Leaf)) {
            throw "ExecutablePath must reference an existing .exe file: $exe"
        }
    }
    if (-not $ExecutablePath -and -not (Test-Path -LiteralPath $exe -PathType Leaf) -and
            (Split-Path $appRoot -Leaf) -eq "_internal") {
        $exe = Join-Path (Split-Path $appRoot -Parent) "FigView.exe"
    }
    if (Test-Path -LiteralPath $exe -PathType Leaf) {
        return [pscustomobject]@{
            Target = $exe
            Arguments = ""
            WorkingDirectory = Split-Path $exe -Parent
            Python = $null
            IsFrozen = $true
        }
    }

    $launcher = Join-Path $appRoot $SourceScript
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "Missing FigView.exe or source launcher: $launcher"
    }
    $python = Resolve-ViewerPython
    return [pscustomobject]@{
        Target = Get-WindowlessPython -Python $python
        Arguments = ('"{0}"' -f $launcher)
        WorkingDirectory = $appRoot
        Python = $python
        IsFrozen = $false
    }
}
