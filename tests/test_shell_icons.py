"""Exercise Windows icon helpers without touching user registry or shortcuts."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell is required")
class ShellIconTests(unittest.TestCase):
    """Run helper-only PowerShell in temporary LOCALAPPDATA for every test."""

    def setUp(self):
        """Isolate source data, output icons and subprocess script paths."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "app with spaces"
        self.app.mkdir()
        self.local = self.root / "local"

    def run_shell(self, body):
        """Pass paths through environment variables, never shell interpolation."""
        script = self.root / "test.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n"
                          ". $env:TEST_SHELL_HELPER\n" + body, encoding="utf-8-sig")
        environment = dict(os.environ, LOCALAPPDATA=str(self.local),
                           TEST_APP=str(self.app), TEST_REPO=str(ROOT),
                           TEST_SHELL_HELPER=str(ROOT / "viewer_shell.ps1"))
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive",
                                 "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                env=environment, capture_output=True, text=True,
                                timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_icon_is_content_addressed_stable_and_retains_older_versions(self):
        """Changed icon bytes receive a new path; unchanged bytes reuse the path."""
        source = self.app / "figure-viewer.ico"
        original = (ROOT / "figure-viewer.ico").read_bytes()
        source.write_bytes(original)
        paths = self.run_shell("""
$first = Resolve-ViewerShellIcon -AppDir $env:TEST_APP
$second = Resolve-ViewerShellIcon -AppDir $env:TEST_APP
@($first, $second) | ConvertTo-Json -Compress
""")
        expected = "FigView-" + hashlib.sha256(original).hexdigest()[:20] + ".ico"
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(Path(paths[0]).name, expected)
        self.assertEqual(Path(paths[0]).read_bytes(), original)
        source.write_bytes(original + b"changed")
        changed = self.run_shell("Resolve-ViewerShellIcon -AppDir $env:TEST_APP | ConvertTo-Json -Compress")
        self.assertNotEqual(changed, paths[0])
        self.assertEqual(Path(paths[0]).read_bytes(), original)
        self.assertEqual(Path(changed).read_bytes(), original + b"changed")

    def test_missing_icon_and_whatif_do_not_write(self):
        """Missing source is a graceful fallback; WhatIf creates no directories."""
        missing = self.run_shell("[bool](Resolve-ViewerShellIcon -AppDir $env:TEST_APP) | ConvertTo-Json")
        self.assertFalse(missing)
        (self.app / "figure-viewer.ico").write_bytes(b"synthetic icon")
        destination = self.run_shell("Resolve-ViewerShellIcon -AppDir $env:TEST_APP -WhatIf | ConvertTo-Json -Compress")
        self.assertTrue(destination.endswith(".ico"))
        self.assertFalse(self.local.exists())

    def test_shortcut_matching_uses_launch_identity_not_names_or_substrings(self):
        """Source paths, argument boundaries and interpreter identity all matter."""
        result = self.run_shell(r'''
$source = Join-Path $env:TEST_APP 'launch_figure_viewer.py'
$quoted = '"' + $source + '" "D:\Figures"'
$checks = @(
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Apps\FigView.exe' -Arguments ''),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Python\pythonw.exe' -Arguments $quoted),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Python\python.exe' -Arguments ('-B ' + $quoted)),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Other\viewer.exe' -Arguments $quoted),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Python\python.exe' -Arguments ('"' + $source + '.backup"')),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Python\python.exe' -Arguments ('other.py ' + $quoted)),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Python\python.exe' -Arguments ('-c ' + $quoted)),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Other\NotFigView.exe' -Arguments ''),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Apps\FigView-v1.1.0-windows-x64.exe' -Arguments ''),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Apps\FigView-v1.0.12-windows-x64.exe' -Arguments ''),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Other\NotFigView-v1.1.0-windows-x64.exe' -Arguments ''),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Other\FigView-v1.1.0-windows-x64.exe.backup.exe' -Arguments ''),
 (Test-ViewerShortcut -AppDir $env:TEST_APP -TargetPath 'C:\Other\FigView-v1.1.0-other-x64.exe' -Arguments '')
)
ConvertTo-Json -InputObject $checks -Compress
''')
        self.assertEqual(result, [True, True, True, False, False, False, False, False,
                                  True, True, False, False, False])

    def test_refresh_defaults_to_its_own_folder(self):
        """The advertised no-argument command works in Windows PowerShell 5.1."""
        for name in ("refresh_windows_icons.ps1", "viewer_shell.ps1"):
            shutil.copy2(ROOT / name, self.app / name)
        # No icon in this temporary app folder: the utility should exit before
        # registry/shortcut access, with a useful message instead of an empty
        # AppDir parameter-binding failure.
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.app / "refresh_windows_icons.ps1"), "-WhatIf"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No figure-viewer.ico found", result.stdout)

    def test_powershell_scripts_parse_without_running_registration_or_refresh(self):
        """Parse all changed scripts without executing registry or COM operations."""
        errors = self.run_shell("""
$messages = @()
foreach ($name in @('viewer_shell.ps1', 'refresh_windows_icons.ps1',
                    'register_file_types.ps1', 'install_shortcuts.ps1')) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $env:TEST_REPO $name), [ref]$tokens, [ref]$errors)
    $messages += @($errors | ForEach-Object { $_.Message })
}
ConvertTo-Json -InputObject @($messages) -Compress
""")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
