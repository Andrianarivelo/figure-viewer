# Shared helper: find a Python interpreter that can actually run Figure Viewer.
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
            Candidates are tried best-first: the FIGURE_VIEWER_PYTHON override,
            then the usual per-user conda installs, then whatever is on PATH.
            If none of them has the dependencies, the first interpreter that
            merely exists is returned with a warning, so the GUI can still start
            and show its own "Pillow is required" dialog.
    #>
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
    throw "No Python interpreter found. Install Python or set FIGURE_VIEWER_PYTHON."
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
