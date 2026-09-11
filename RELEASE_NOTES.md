# FigView 1.1.0

A new app icon, direct PDF review tools, and a more comfortable document-reading
workspace. Includes the improvements made since 1.0.0.

## Downloads

- **Recommended:** `FigView-v1.1.0-windows-x64-portable.zip`. Extract the whole
  archive and run `FigView/FigView.exe`, keeping `_internal` beside it.
- `FigView-v1.1.0-windows-x64.exe`: standalone convenience download. It extracts
  dependencies at launch and starts more slowly than the portable app.
- `SHA256SUMS.txt`: checksums for both downloads.

Both builds include Python and the image, PDF, video, export and signing
libraries. Existing settings and recent files are preserved. OCR language
packs download on demand; video playback remains silent.

## New and improved

- A redesigned layered figure icon, with a true SVG master, transparent PNGs
  up to 4096 pixels and a multi-size Windows ICO embedded in both executables.
- Direct text highlighting and anchored comments, five colors, opacity control,
  repeated drag highlighting, undo/redo, searchable review and CSV export.
- Selectable PDF text and OCR-backed image text, including rotated PDF pages.
- A docked document index, resizable right-hand panels, a tidier toolbar and
  automatic opening zoom that uses the available viewing area.
- Clickable PDF hyperlinks and printed web addresses.
- PDF editing fixes and improved layout and navigation behavior.

## Updating Windows icons

New shortcut installation and file-type registration store the icon at a
persistent path keyed by its content, so Windows can distinguish updated artwork.
This also keeps icon files available after standalone executable extraction ends.

For existing Explorer associations and shortcuts, run this from the extracted
portable folder's parent directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\FigView\_internal\refresh_windows_icons.ps1
```

Add `-WhatIf` to preview. The utility changes existing FigView icon references
and notifies the Windows shell. It preserves default-app choices, document
opening commands and shortcut arguments. Restart any already-running older
viewer to load the new taskbar icon. Use **Tools > Set as Default PDF Reader**
from the new executable to change which installation opens PDFs.

## Verification

The source test suite covers editing, selection, shell integration and startup.
Both built executables undergo isolated smoke checks with Python removed from
PATH, covering GUI startup, batched opening, IPC, PDF review and icon resources,
video decoding, Office/text exports and certificate-signature integrity.
Build reports and SHA256 checksums accompany the release downloads.

Rebuild with `python build_release.py` or `Build Release.bat`. Dependencies are
pinned in `requirements-build.txt`; the SVG exporter has its own development-only
dependency file, `requirements-icon.txt`.
