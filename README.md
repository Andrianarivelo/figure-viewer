# Figure Viewer 2.0

A fast, themed, folder-based viewer for **figures and PDFs** on Windows.
Point it at a folder, keep it open, and let your analysis scripts overwrite the
files underneath: the viewer reloads them automatically without losing your
zoom, your pan or your place in the list.

---

## Launch

- Double-click `Figure Viewer.bat`, or the Desktop / Start Menu shortcut.
- Pass a folder **or a file** as the first argument:

```powershell
pythonw.exe D:\FigureViewerApp\figure_viewer_app.py "I:\processedData\mPFC-NAc\group\1\figures\v3_n6"
pythonw.exe D:\FigureViewerApp\figure_viewer_app.py "C:\some\report.pdf"
```

Startup failures are written to `figure_viewer_error.log` and shown in a dialog,
so nothing fails silently under `pythonw.exe`.

### Requirements

| Package | Needed for | Install |
|---|---|---|
| Pillow | everything | `pip install pillow` |
| PyMuPDF | PDF / SVG / EPS / XPS / CBZ / EPUB | `pip install pymupdf` |

Without PyMuPDF the app still runs; PDFs are listed but show an install hint.

---

## What is new in 2.0

### Snip: draw a rectangle, get it on the clipboard
Press **S** (or the `✂ Snip` button), drag a rectangle, release. The region is
cropped **from the source at full resolution** - not from the scaled screen
image - and pushed to the clipboard. Hold **Shift** to constrain to a square,
**Esc** to cancel. `Ctrl+Shift+C` re-copies the last snip, `Ctrl+Shift+S` saves
it to a file. For PDFs the crop is re-rasterised at the export DPI (default
300), so a snip of a vector plot is razor sharp whatever zoom you took it at.

### Images or PDFs, one click apart
A segmented switch in the toolbar - **Images | PDF | All** (`Ctrl+1/2/3`) -
filters what the folder shows. PDFs get page navigation (`PageUp`/`PageDown`,
`Ctrl+G`), text extraction (`Edit > Copy Page Text`) and per-page export.
Multi-page TIFFs and animated GIFs use the same page controls.

### File menu and multiple windows
A fully themed menu bar: **File / Edit / View / Go / Tools / Help**.
`Ctrl+N` opens a new window, `Ctrl+Shift+N` duplicates the current one with its
folder and selection. Windows share one document cache, so the same figure open
in two windows is decoded once.

### Much faster
The canvas never scales the whole image. It renders **only the visible region,
at exactly the on-screen size**, so cost is bounded by the window rather than
the file:

| Zoom on an 8000 x 6000 px figure | Old viewer | Figure Viewer 2.0 |
|---|---|---|
| 100 % | 47 ms | 1.3 ms |
| 200 % | 1 147 ms | 7.8 ms |
| 400 % | 3 751 ms | 8.6 ms |
| 800 % | 13 474 ms | 5.9 ms |

Plus: folder scans, decoding and thumbnails run on a worker pool (the UI never
freezes on a network drive); the next and previous files are prefetched so
arrow-key browsing is instant; PDFs keep a padded raster cache so panning costs
~1 ms instead of ~130 ms; every repaint is a fast pass followed by a
high-quality pass once the gesture stops.

### Nicer to look at and to use
Dark and light themes (`Ctrl+T`), DPI-aware fonts, drop shadow and optional
checkerboard behind transparent PNGs, thumbnails in the file list, an overview
map when zoomed in (click it to jump), a live cursor read-out showing image
coordinates and the pixel colour under the pointer, a details panel with file
and PNG-metadata fields, tooltips everywhere, and toast feedback in the status
bar.

### Other additions
Recent folders · parent-folder navigation · recursive scan · sort by
name/date/size/type · search with multiple terms and `*`/`?` wildcards ·
rotate, flip, invert, grayscale · fit page / fit width / 100 % · slideshow
(`F9`) · save page or snip as PNG/JPEG/TIFF/WebP · export all pages · copy the
file itself (paste into Explorer or an e-mail) · reveal in Explorer · delete to
the Recycle Bin · full screen (`F11`) · right-click menus on the canvas and the
file list · `F1` shortcut reference.

### Better clipboard
Images are published in three formats at once: registered **PNG** (lossless,
keeps transparency and DPI - what Office, browsers and Slack prefer),
**CF_DIBV5** (32-bit with a real alpha channel) and **CF_DIB** (24-bit
fallback). The previous version only wrote the 24-bit fallback, which silently
flattened transparency.

---

## Keyboard shortcuts

Press **F1** in the app for the full list. The essentials:

| Key | Action |
|---|---|
| `←` `→` `↑` `↓` | Previous / next file |
| `PageUp` `PageDown` | Previous / next page |
| Wheel | Zoom towards the pointer |
| Drag / middle-drag | Pan |
| Double click | Toggle fit page / 100 % |
| `S` | Snip a region to the clipboard |
| `Ctrl+C` | Copy the whole page |
| `Ctrl+Shift+V` | Copy exactly what is on screen |
| `Ctrl+N` / `Ctrl+O` | New window / open folder |
| `Ctrl+1` `Ctrl+2` `Ctrl+3` | Images / PDFs / everything |
| `Ctrl+B` `Ctrl+I` `Ctrl+T` | Sidebar / details / theme |
| `F5` `F9` `F11` | Rescan / slideshow / full screen |

---

## Layout

```
figure_viewer_app.py      entry point (name kept for the existing shortcuts)
launch_figure_viewer.py   logging launcher used by the .bat and shortcuts
figureviewer/
    theme.py              palettes, ttk styling, themed menu bar, tooltips
    winclip.py            Win32 clipboard (PNG + DIBV5 + DIB + HDROP + text)
    docs.py               scanning, document model, caches, worker pool
    viewer.py             the viewer window: canvas, view transform, commands
    app.py                Tk root, settings, window registry
```

Settings live in `%APPDATA%\FigureViewerApp\settings.json`.
