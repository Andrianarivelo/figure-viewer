"""Exercise portable and source shell launch resolution without registry writes."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
class PackagedShellTests(unittest.TestCase):
    """Keep installed commands independent of Python on portable machines."""

    def resolve(self, directory: Path, *, source: bool = False,
                executable: Path | None = None) -> dict:
        """Run the actual helper with controlled Python discovery functions."""
        def quote(path: Path) -> str:
            return "'" + str(path).replace("'", "''") + "'"

        python_override = (
            "function Resolve-ViewerPython { return 'C:\\Python\\python.exe' }; "
            "function Get-WindowlessPython { param($Python); return 'C:\\Python\\pythonw.exe' }; "
            if source else
            "function Resolve-ViewerPython { throw 'Packaged app attempted Python discovery' }; "
        )
        command = (
            "$ErrorActionPreference = 'Stop'; "
            f". {quote(ROOT / 'viewer_python.ps1')}; "
            + python_override
            + f"$launch = Resolve-ViewerLaunch -AppDir {quote(directory)}"
            + (f" -ExecutablePath {quote(executable)}" if executable else "")
            + '; $launch | ConvertTo-Json -Compress'
        )
        completed = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=True,
        )
        return json.loads(completed.stdout)

    def test_portable_scripts_resolve_executable_without_python(self) -> None:
        """Support scripts beside the executable and inside bundled resources."""
        with tempfile.TemporaryDirectory(prefix="FigView portable ") as folder:
            root = Path(folder)
            executable = root / "FigView.exe"
            executable.touch()
            internal = root / "_internal"
            internal.mkdir()
            for location in (root, internal):
                with self.subTest(location=location):
                    launch = self.resolve(location)
                    self.assertEqual(Path(launch["Target"]), executable)
                    self.assertEqual(Path(launch["WorkingDirectory"]), root)
                    self.assertEqual(launch["Arguments"], "")
                    self.assertTrue(launch["IsFrozen"])
                    self.assertIsNone(launch["Python"])

    def test_source_checkout_keeps_windowless_python_launcher(self) -> None:
        """Preserve the existing quoted source command when no binary exists."""
        with tempfile.TemporaryDirectory(prefix="FigView source ") as folder:
            root = Path(folder)
            launcher = root / "launch_figure_viewer.py"
            launcher.touch()
            launch = self.resolve(root, source=True)
            self.assertEqual(launch["Target"], r"C:\Python\pythonw.exe")
            self.assertEqual(launch["Arguments"], f'"{launcher}"')
            self.assertFalse(launch["IsFrozen"])

    def test_onefile_registration_uses_persistent_executable(self) -> None:
        """An extracted script must register the original downloadable binary."""
        with tempfile.TemporaryDirectory(prefix="FigView user's download ") as folder:
            root = Path(folder)
            executable = root / "FigView-1.0.0.exe"
            executable.touch()
            extracted = root / "_MEI123456"
            extracted.mkdir()
            launch = self.resolve(extracted, executable=executable)
            self.assertEqual(Path(launch["Target"]), executable)
            self.assertEqual(Path(launch["WorkingDirectory"]), root)
            self.assertTrue(launch["IsFrozen"])

    def test_explicit_executable_rejects_non_executable_file(self) -> None:
        """Fail before registration when the supplied target is not an exe."""
        with tempfile.TemporaryDirectory(prefix="FigView invalid ") as folder:
            root = Path(folder)
            invalid = root / "document.pdf"
            invalid.touch()
            with self.assertRaises(subprocess.CalledProcessError):
                self.resolve(root, executable=invalid)

    def test_frozen_gui_passes_executable_as_a_separate_argument(self) -> None:
        """Pass paths intact to PowerShell without constructing shell text."""
        from figureviewer.viewer import ViewerWindow

        executable = r"C:\Users\Example User\Downloads\FigView-1.0.0.exe"
        window = SimpleNamespace(app=SimpleNamespace(app_dir=ROOT), toast=lambda text: None)
        with patch("figureviewer.viewer.sys.frozen", True, create=True), \
                patch("figureviewer.viewer.sys.executable", executable), \
                patch("figureviewer.viewer.messagebox.askokcancel", return_value=True), \
                patch("figureviewer.viewer.subprocess.Popen") as popen:
            ViewerWindow.register_file_types(window)
        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], ["-ExecutablePath", executable])
        self.assertEqual(command[command.index("-Extensions") + 1], ".pdf")


if __name__ == "__main__":
    unittest.main()
