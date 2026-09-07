# FigView 1.0.0

First packaged Windows x64 release, including the latest startup and
multiple-file opening improvements.

## Downloads

- **Recommended:** `FigView-v1.0.0-windows-x64-portable.zip`. Extract everything
  and run `FigView/FigView.exe`, keeping `_internal` beside it.
- `FigView-v1.0.0-windows-x64.exe`: standalone convenience download. Unpacks
  dependencies on every launch, so it starts more slowly.
- `SHA256SUMS.txt`: SHA256 hashes for both downloads.

Python, Pillow, PyMuPDF, OpenCV, Office exporters and certificate-signing
dependencies are included. OCR language packs are downloaded on demand through
the application. Video playback remains silent, as in the source application.
Existing settings and recent-file history are preserved.

## Included improvements

- Batched multi-file opening with one file-list and recent-menu refresh.
- Background thumbnails that do not queue ahead of selected-file loading.
- Coordination between simultaneous file launches and stale-load cancellation.
- Windows shortcuts and file associations that target the packaged executable.
- Writable startup error logs in the existing user settings directory.

## Build and verification

The source includes `FigView.spec`, `build_release.py`, `Build Release.bat` and
the complete pinned build dependency list in `requirements-build.txt`.
The release builder tests both binaries with Python removed from their search
path. The executable's `--self-test report.json` command uses temporary fixtures
to check GUI startup, image/PDF rendering, multiple opening, authenticated IPC,
video decoding, Office/text exports, and PDF signing integrity.

Validation on Windows 11 x64: 171 source tests passed, and both packaged
executables passed all six smoke checks with Python removed from PATH.
Archive integrity and SHA256 checksums were verified after packaging.
Normal command-line launches also passed for both builds: simultaneous starts
created one viewer, all three opened files were preserved, and shutdown was clean.
In this single local check, portable startup/handoff took about 1-2 seconds;
standalone startup/handoff took 19-21 seconds, including dependency extraction.

This is the first public executable version. Earlier development versions in
the source documentation used a separate 3.x numbering sequence.
