"""Build versioned Windows artifacts. Run from an IDE or Build Release.bat."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from figureviewer import APP_VERSION

# Build paths and time budgets are centralized for repeatable local releases.
ROOT = Path(__file__).resolve().parent
ENVIRONMENT = ROOT / ".venv-build"
PYTHON = ENVIRONMENT / "Scripts" / "python.exe"
OUTPUT = ROOT / "release"
SMOKE_TIMEOUT_SECONDS = 120


def checked(arguments: list[str]) -> None:
    """Run a build step in the repository and stop on the first failure."""
    subprocess.run(arguments, cwd=ROOT, check=True)


def main() -> int:
    """Install pinned dependencies, freeze, test both binaries, and package."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-install", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Build Windows releases on a 64-bit Windows machine.")
    if not PYTHON.is_file():
        checked([sys.executable, "-m", "venv", str(ENVIRONMENT)])
    if not args.skip_install:
        checked([str(PYTHON), "-m", "pip", "install", "-r", "requirements-build.txt"])
    checked([str(PYTHON), "-m", "PyInstaller", "--noconfirm", "FigView.spec"])
    OUTPUT.mkdir(exist_ok=True)
    portable = ROOT / "dist" / "FigView"
    standalone = ROOT / "dist" / f"FigView-v{APP_VERSION}-windows-x64.exe"
    for label, binary in (("portable", portable / "FigView.exe"),
                          ("standalone", standalone)):
        report = OUTPUT / f"smoke-{label}.json"
        report.unlink(missing_ok=True)
        # Run with no Python environment injected. The executable must load
        # every dependency from its own bundle, not the developer environment.
        environment = os.environ.copy()
        for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"):
            environment.pop(name, None)
        windows = Path(environment.get("SystemRoot", "C:/Windows"))
        environment["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
        completed = subprocess.run([str(binary), "--self-test", str(report)],
                                   cwd=OUTPUT, env=environment,
                                   timeout=SMOKE_TIMEOUT_SECONDS)
        if completed.returncode or not report.is_file():
            raise RuntimeError(f"{label} executable smoke test failed: {report}")
        result = json.loads(report.read_text(encoding="utf-8"))
        if not (result.get("ok") and result.get("frozen")
                and result.get("version") == APP_VERSION):
            raise RuntimeError(f"{label} executable reported failure: {report}")
    for name in ("README.md", "RELEASE_NOTES.md", "requirements-build.txt"):
        shutil.copy2(ROOT / name, portable / name)
    archive = Path(shutil.make_archive(
        str(OUTPUT / f"FigView-v{APP_VERSION}-windows-x64-portable"), "zip",
        root_dir=portable.parent, base_dir=portable.name))
    executable = OUTPUT / standalone.name
    shutil.copy2(standalone, executable)
    # SHA256 accompanies assets so recipients can verify downloaded bytes.
    manifest = []
    for path in (archive, executable):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest.append(f"{digest}  {path.name}")
    (OUTPUT / "SHA256SUMS.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(f"Release {APP_VERSION} verified and packaged in {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
