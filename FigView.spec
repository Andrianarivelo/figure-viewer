# -*- mode: python ; coding: utf-8 -*-
"""Build a fast portable folder and a standalone Windows executable."""
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH)
VERSION = "1.0.0"
# A venv over conda can expose older MinGW OpenSSL DLLs earlier on PATH.
# Pin Python's matching native SSL pair explicitly when building over conda.
binaries = []
native = Path(sys.base_prefix) / "Library" / "bin"
for name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
    if (native / name).is_file():
        binaries.append((str(native / name), "."))
datas = [(str(ROOT / "assets"), "assets"),
         (str(ROOT / "figure-viewer.ico"), ".")]
for name in ("register_file_types.ps1", "unregister_file_types.ps1",
             "install_shortcuts.ps1", "viewer_python.ps1"):
    datas.append((str(ROOT / name), "."))
# Preserve templates, trust resources and license/metadata files that dynamic
# imports cannot discover by following Python bytecode alone.
for package in ("docx", "pptx", "pyhanko", "pyhanko_certvalidator", "tzdata"):
    datas += collect_data_files(package)
for distribution in ("Pillow", "PyMuPDF", "opencv-python-headless", "numpy",
                     "pdf2docx", "python-docx", "openpyxl", "python-pptx", "pyHanko"):
    datas += copy_metadata(distribution, recursive=True)

version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=(1, 0, 0, 0), prodvers=(1, 0, 0, 0),
                      mask=0x3F, flags=0, OS=0x40004, fileType=1,
                      subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable("040904B0", [
        StringStruct("FileDescription", "FigView image, video and PDF viewer"),
        StringStruct("FileVersion", VERSION),
        StringStruct("ProductName", "FigView"),
        StringStruct("ProductVersion", VERSION),
        StringStruct("OriginalFilename", "FigView.exe"),
    ])]), VarFileInfo([VarStruct("Translation", [1033, 1200])])],
)
a = Analysis(
    [str(ROOT / "figure_viewer_app.py")], pathex=[str(ROOT)],
    binaries=binaries, datas=datas,
    hiddenimports=["pymupdf", "fitz", "cv2", "pdf2docx", "docx", "openpyxl",
                   "pptx", "pyhanko.sign.signers", "pyhanko.sign.validation",
                   "pyhanko_certvalidator", "tzdata"],
    hookspath=[], runtime_hooks=[], excludes=["matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="FigView",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=str(ROOT / "figure-viewer.ico"), version=version)
portable = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="FigView")
# One-file convenience download. The portable folder avoids unpacking these
# dependencies on each cold start and remains the recommended fast launcher.
standalone = EXE(pyz, a.scripts, a.binaries, a.datas, [],
                 name=f"FigView-v{VERSION}-windows-x64", debug=False,
                 bootloader_ignore_signals=False, strip=False, upx=False,
                 console=False, icon=str(ROOT / "figure-viewer.ico"), version=version)
