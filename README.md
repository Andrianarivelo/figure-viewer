# Figure Viewer 2.2

A fast, themed, folder-based viewer for **figures and PDFs** on Windows.
Point it at a folder, keep it open, and let your analysis scripts overwrite the
files underneath: the viewer reloads them automatically without losing your
zoom, your pan or your place in the list.

---

## Launch

- Double-click `Figure Viewer.bat`, or the Desktop / Start Menu shortcut.
- Pass a folder **or a file** as the first argument:

```powershell
pythonw.exe D:\Apps\figure-viewer\figure_viewer_app.py "E:\group\1\figures\v3_n6"
pythonw.exe D:\Apps\figure-viewer\figure_viewer_app.py "C:\some\report.pdf"
```

Startup failures are written to `figure_viewer_error.log` and shown in a dialog,
so nothing fails silently under `pythonw.exe`.

### After moving the app folder

Windows shortcuts store absolute paths, so a moved folder leaves them pointing
at a directory that no longer exists. Clicking one then does nothing at all: no
window, no error, because `pythonw.exe` has no console to complain to.

Re-create them from the new location:

```powershell
powershell -ExecutionPolicy Bypass -File D:\Apps\figure-viewer\install_shortcuts.ps1
```

Every path that script writes is derived from its own location, so running it
from the folder you moved to is the entire fix. Add `-Folder <path>` to also get
a shortcut that opens straight into one figure directory.

The interpreter is chosen at install time by probing candidates for Pillow and
tkinter, rather than being hardcoded. Override it with the
`FIGURE_VIEWER_PYTHON` environment variable if you need a specific one.

### Make it the default image and PDF viewer

```powershell
powershell -ExecutionPolicy Bypass -File D:\Apps\figure-viewer\register_file_types.ps1
```

or, from inside the app, **Tools > Set as Default Image / PDF Viewer**.

The script registers Figure Viewer with Windows for `.png .jpg .jpeg .jpe .tif
.tiff .bmp .gif .webp .jp2 .ppm .pgm .pdf` (add `-IncludeVector` for `.svg .eps
.xps .oxps .cbz .epub`, or `-Extensions png,pdf` to pick your own). Everything
it writes lives under `HKEY_CURRENT_USER`, so no administrator rights are
needed, and `unregister_file_types.ps1` removes all of it.

**One click is left over, and that is Windows' doing, not an oversight.** Since
Windows 8 the actual default for a file type is stored in a `UserChoice` key
protected by a per-user hash that only Explorer and the Settings app can
produce; programs that write it directly get reverted or break the association.
So the script does everything legitimate - the ProgIDs, the icons, the *Open
with* entries and the app capability block - and then opens
**Settings > Apps > Default apps > Figure Viewer**, where *Set default* (or one
click per file type) finishes the job. Per file, the other route is right-click
> *Open with* > *Choose another app* > **Figure Viewer** > *Always use this app*.

Once it is the default, double-clicking figures is cheap: a second launch hands
its path to the window that is already open instead of starting another Python
process, so clicking through ten PNGs fills one window rather than ten. Turn it
off with `"single_instance": false` in the settings file.

### Requirements

| Package | Needed for | Install |
|---|---|---|
| Pillow | everything | `pip install pillow` |
| PyMuPDF | PDF / SVG / EPS / XPS / CBZ / EPUB | `pip install pymupdf` |

Without PyMuPDF the app still runs; PDFs are listed but show an install hint.

---

## What is new in 2.2

A review pass over the interface, mostly removing things that were wrong rather
than adding new ones.

**The file list is a real tree.** `apply_theme` used to strip the expander
element out of the Treeview layout, on the reasonable grounds that the sidebar
was a flat list. Once 2.1 started grouping files by sub-folder, that left
folders that could not be collapsed. There are now two list styles - flat
without an expander, tree with one - and the sidebar switches between them with
the shape of the listing, so a flat list still has no empty gutter down its left
edge. Folders you collapse **stay** collapsed across the rescans that a running
plotting script triggers.

**The sidebar is resizable.** Drag the divider between the list and the canvas;
double-click it to reset. The width is remembered, and the count beside `FILES`
shortens rather than running off the edge when the panel is narrow.

**Nothing reserves eleven characters any more.** ttk gives `Button` and
`Menubutton` a default `-width` of `-11`, so every toolbar button was 139 px
wide whether it said `Fit` or `Folder…`, and every menu title sat 150 px from
the next. Setting natural widths tightened the menu bar into a menu bar and
handed about 400 px back to the folder-path box.

**Smaller things.** Snip and Auto-reload no longer look alike: a modal state
that changes what dragging does is painted in full accent, a persistent
preference only quietly lit. The details panel scrolls instead of silently
clipping long PDF metadata. The colour swatch appears only while the pointer is
over the image, instead of sitting in the status bar as an empty box. The search
box says what it does. A capped listing keeps saying so.

---

## What is new in 2.1

### Browse several levels of subfolders
**Tools > Subfolder Depth** replaces the old on/off "Include Subfolders":
*This Folder Only*, *+1 / +2 / +3 / +5 Levels* or *All Levels*. `Ctrl+Shift+R`
flips between flat and deep, remembering the depth you last picked.

A deep listing is shown as a **collapsible tree**, one node per subfolder, so
`run01/figures/psth.png` and `run02/figures/psth.png` are finally
distinguishable and the file names are not eaten by a long path. Sorting by
date or size drops back to a flat list showing the relative path on each row,
because "the newest figures anywhere under here" is exactly why you sorted that
way. The search box matches the relative path too, so `run02 psth` narrows to
one file, and the sidebar footer counts the folders involved.

The walk is breadth-first, skips hidden and noise directories (`.git`,
`__pycache__`, `node_modules`, `site-packages`, …), follows no symlink loops,
and stops at 20 000 files with a warning rather than freezing on a drive root.

### Default viewer for images and PDFs
`register_file_types.ps1` registers the app with Windows; see
[Make it the default image and PDF viewer](#make-it-the-default-image-and-pdf-viewer)
above. A second launch hands its file to the window that is already open.

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
checkerboard behind transparent PNGs, thumbnails in the file list (five sizes,
`View > Thumbnails`, plus `View > List Density` for compact / normal /
comfortable rows), an overview
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
| `Ctrl+Shift+R` | Include subfolders / this folder only |
| Drag the divider | Resize the file list (double-click resets) |
| `Ctrl+B` `Ctrl+I` `Ctrl+T` | Sidebar / details / theme |
| `F5` `F9` `F11` | Rescan / slideshow / full screen |

---

## Layout

```
figure_viewer_app.py       entry point (name kept for the existing shortcuts)
launch_figure_viewer.py    logging launcher used by the .bat and shortcuts
install_shortcuts.ps1      Desktop / Start Menu shortcuts
register_file_types.ps1    register with Windows as an image and PDF viewer
unregister_file_types.ps1  undo the above
viewer_python.ps1          shared "which Python has Pillow?" probe
figureviewer/
    theme.py               palettes, ttk styling, themed menu bar, tooltips
    winclip.py             Win32 clipboard (PNG + DIBV5 + DIB + HDROP + text)
    docs.py                scanning, document model, caches, worker pool
    viewer.py              the viewer window: canvas, view transform, commands
    app.py                 Tk root, settings, window registry, single instance
```

Settings live in `%APPDATA%\FigureViewerApp\settings.json`.
