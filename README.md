# FigView 3.5

A fast, themed viewer **and PDF workstation** for images, videos, and PDFs on
Windows. Open one file and only that file enters the list; open more files and
they append to the same session. Folder scanning starts only when you
explicitly choose or pass a folder. Auto reload preserves zoom, pan, playback
position, and list position.

Since 3.5, FigView also **edits PDFs**: annotations, in-place text rewriting
with reflow, page management, redaction, form filling with saved identities,
drawn/typed/image signatures plus real certificate signing, OCR for scanned
documents, and one-click export to Word, Excel, PowerPoint, Markdown, HTML
and text. Everything runs locally; nothing ever leaves the machine.

---

## Launch

- Double-click `FigView.bat`, or the Desktop / Start Menu shortcut.
- Pass a folder **or a file** as the first argument:

```powershell
pythonw.exe D:\Apps\figure-viewer\figure_viewer_app.py "E:\group\1\figures\v3_n6"
pythonw.exe D:\Apps\figure-viewer\figure_viewer_app.py "C:\some\report.pdf"
pythonw.exe D:\Apps\figure-viewer\figure_viewer_app.py "C:\some\simulation.mp4"
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
tkinter, rather than being hardcoded. Override it with `FIGVIEW_PYTHON` if you
need a specific one. The historical `FIGURE_VIEWER_PYTHON` name still works.

### Make it the default PDF reader

```powershell
powershell -ExecutionPolicy Bypass -File D:\Apps\figure-viewer\register_file_types.ps1 -Extensions .pdf
```

or, from inside the app, **Tools > Set as Default PDF Reader**.

The command above registers FigView only for PDF. The same script can register
other supported raster image and video types when explicitly requested. Add
`-IncludeVector` for SVG, EPS, XPS, CBZ, and EPUB, or use
`-Extensions png,pdf,mp4` to choose an exact set. Everything it writes lives
under `HKEY_CURRENT_USER`, so no administrator rights are needed, and
`unregister_file_types.ps1` removes all of it.

**One click is left over, and that is Windows' doing, not an oversight.** Since
Windows 8 the actual default for a file type is stored in a `UserChoice` key
protected by a per-user hash that only Explorer and the Settings app can
produce; programs that write it directly get reverted or break the association.
So the script does everything legitimate - the ProgIDs, the icons, the *Open
with* entries and the app capability block - and then opens
**Settings > Apps > Default apps > FigView**, where *Set default* (or one
click per file type) finishes the job. Per file, the other route is right-click
> *Open with* > *Choose another app* > **FigView** > *Always use this app*.

Once it is the default, double-clicking figures is cheap: a second launch hands
its path to the window that is already open instead of starting another Python
process, so clicking through ten PNGs fills one window rather than ten. Turn it
off with `"single_instance": false` in the settings file.

The **File** menu keeps separate Recent Files and Recent Folders lists. The
small `×` beside the folder path leaves folder mode and clears the active list;
it does not erase either recent-items history.

### Opening performance

File batches append in one operation, select the final file, and refresh the
recent-file menu once. Closely spaced Explorer requests are combined on the
UI thread. Simultaneous cold file launches coordinate through an operating
system lock, which is released automatically if the starting process crashes.

Existing-window handoffs avoid GUI imports. Image startup defers the optional
PDF renderer until the first PDF operation. Thumbnails and speculative file
decodes use a separate background worker, leaving interactive loading workers
available. Changing selection cancels queued stale loads and ignores outdated
results, including errors.

Reproduce the comparison with the pinned previous revision using
`python benchmarks/benchmark_opening.py`. Generated fixtures, raw timings,
paired statistics, measurement limitations, and PDF/PNG/SVG figures are
documented in `benchmarks/results/opening_performance_report.md`.

### Requirements

| Package | Needed for | Install |
|---|---|---|
| Pillow | everything | `pip install pillow` |
| PyMuPDF | PDF viewing, editing, OCR, signatures | `pip install pymupdf` |
| OpenCV | in-app video decoding; OCR scan clean-up | `pip install opencv-python` |
| pdf2docx + python-docx | Export to Word | `pip install pdf2docx python-docx` |
| openpyxl | Export detected tables to Excel | `pip install openpyxl` |
| python-pptx | Export to PowerPoint | `pip install python-pptx` |
| pyHanko | Cryptographic (PAdES) certificate signing | `pip install pyhanko` |

Everything below Pillow is optional. A missing library produces an actionable
hint naming the exact pip command without preventing the app from starting.
OCR additionally needs a Tesseract language pack; FigView downloads it on
request into `%APPDATA%\FigureViewerApp\tessdata` (about 4 MB for English).

OpenCV provides efficient in-app video decoding but does not output audio. The
transport and details panel state this explicitly. For a clip whose soundtrack
matters, use **Open in Default App**.

---

## What is new in 3.5: the PDF studio

Press **E** on any PDF (or the **Edit** toolbar toggle) and the viewer becomes
an editor. Everything below shares one undo history (`Ctrl+Z`), never touches
the file on disk until an explicit `Ctrl+S`, and saves atomically (temp file
plus rename), so a crash can never leave a half-written PDF.

**Annotate.** Highlight, underline, strikeout, squiggly (`H U K Q`), free text
(`T`), sticky notes (`N`), freehand ink (`P`), rectangles, ellipses, lines and
arrows (`R O L A`). Select with `V`: drag to move, corner handles resize,
`Delete` removes, a compact popover changes colours. Defaults follow your last
choices.

**Rewrite text in place.** Double-click a paragraph with the select tool, type,
and `Ctrl+Enter`: the original glyphs are removed via precise line-level
redaction and the new text reflows inside the block, matching the detected
font family, size, colour, alignment and line height. **Find and Replace**
(`Ctrl+H`) works document-wide with regular expressions and capture groups;
one undo reverts a whole replace-all. Acrobat cannot do regex replace at all.

**Organise pages.** The **Pages** strip shows live thumbnails: click to jump,
drag to reorder, right-click to rotate, duplicate, insert (blank or another
PDF), extract or delete pages. Merge whole PDFs from the File menu.

**Redact.** Mark areas with `X`, then apply: content under the marks is
permanently removed from the saved file, not hidden behind a black box.
Images and vector art outside the marks are preserved exactly.

**Fill forms with a saved identity.** A form PDF gets a form bar
automatically: click any field to type into it, and **Auto-fill** matches your
saved identity preset (name, address, e-mail, phone...) against field names,
tooltips and nearby labels, in English, French and German conventions. Every
value is reviewed before it is written, filling is one undo entry, and
identity data stays in a local JSON you can read. Free placement of text and
check marks works on flat, non-form PDFs too.

**Sign.** Draw a signature with the mouse, type one, or import an image whose
background is removed automatically; the library is persistent, and placement
is a live draggable, resizable preview committed with Enter. For legally
meaningful signing, **Certify with Certificate** produces a PAdES digital
signature through pyHanko, with a locally generated PKCS#12 certificate if
you have none; passphrases are asked per use and never stored. Acrobat Reader
cannot create self-managed local certificates without an Adobe account.

**OCR scanned documents.** `Tools > Recognise Text` downloads a Tesseract
language pack on request (20 languages, fast or best variants) and overlays an
invisible, selectable, searchable text layer without re-rasterising the page.
An OpenCV clean-up pass (denoise, automatic deskew, adaptive threshold) runs
first: on generated bad scans it cuts the median character error rate from
3.2% to 0.2% (16x, one-sided paired Wilcoxon `p = 0.0031`, 24 pairs), with
the largest wins at 8-12 degrees of skew where the baseline loses up to 14.5%
of characters. OCR text also feeds search, copy, reflow and every exporter.

**Export.** `Ctrl+Shift+E` exports to Word (.docx, real paragraphs via
pdf2docx), Excel (.xlsx, one sheet per detected table; refuses to write an
empty workbook), PowerPoint (.pptx, full-bleed slides with the page text in
speaker notes), Markdown (headings inferred from font sizes, tables as pipe
tables), styled HTML, plain text, or numbered page images. Scanned pages can
be OCR'd on the fly so a photographed document still exports its text. Adobe
charges a subscription for exactly this; FigView does it locally for free.

Performance on the reference machine: a highlight plus full repaint takes
17 ms median; atomic saves take 38/89/237 ms for 2/10/40-page documents;
export throughput is 684 pages/s (text), 45 (Markdown), 37 (Excel), 19
(Word), 18 (PowerPoint). See the
[validation figure](benchmarks/results/pdf_studio_v35.png),
[raw data](benchmarks/results/pdf_studio_v35_raw.csv),
[statistics](benchmarks/results/pdf_studio_v35_statistics.csv), and
[reproducible benchmark](benchmarks/benchmark_v35.py).

Where Acrobat still wins, stated honestly: FigView's reflow editor works at
paragraph level with substituted fonts (embedded subset fonts cannot legally
gain new glyphs), it has no cloud collaboration, and its OCR has no automatic
language detection. Where FigView wins: local-first speed and privacy, free
export, regex replace, reviewed identity auto-fill, self-managed certificate
signing, honest atomic saves, and one undo history across every kind of edit.

---

## What is new in 3.4

**Focused document view.** The visible **Files** toolbar control and `Ctrl+B`
show or hide the file list so the document can use the full window. Enable
**Hide in 5s** beside it, or **View > Auto-hide File List**, to reveal the list
temporarily and reclaim its width after five seconds. Activity inside the list
restarts the delay.

**Continuous PDF scrolling.** A multi-page PDF now has a visible page bar with
previous/next controls, current page, total pages, and detected paper format.
Ordinary wheel or two-finger scrolling first moves through a page when it is
taller than the viewport. Continuing past the bottom advances to the next page;
scrolling above the top opens the previous page at its bottom. `Ctrl + wheel`
zooms toward the pointer. Images and other formats keep their former wheel zoom.

**Format-aware PDF zoom.** PDF zoom percentages now use the conventional 96 dpi
screen scale rather than the internal render DPI. Portrait A4 opens at 125%,
while A-series, Letter, Legal, Tabloid, landscape, and custom MediaBox sizes are
detected and scaled to a consistent reading width. Very narrow windows cap
horizontal overflow.

The geometry benchmark confirms the intended format-dependent reading sizes at
well below one microsecond per calculation (0.57 µs median on the reference
machine). This is 0.33 µs slower than the former fit-page calculation, a
deliberate but operationally negligible cost. See the
[validation figure](benchmarks/results/pdf_reading_v34.png),
[format measurements](benchmarks/results/pdf_reading_v34_formats.csv),
[statistics](benchmarks/results/pdf_reading_v34_statistics.csv), and
[reproducible benchmark](benchmarks/benchmark_v34.py).

---

## What is new in 3.3

**Explicit document sessions.** Opening a file no longer scans its parent
directory. A single launch shows a single file, and later file opens append in
opening order. This applies equally to the file dialog, command-line launches,
Windows file associations, single-instance handoffs, and Recent Files.

**Folders remain deliberate.** Only Open Folder, a typed directory, a recent
folder, a directory command-line target, or explicit parent navigation starts
a folder scan. Recent files and folders are now independent, and a compact
clear control resets the active folder path.

On generated directories containing 100, 1,000, and 5,000 supported siblings,
the new one-file path admitted exactly one file and took 0.15, 0.28, and 0.35 ms
median, versus 2.18, 17.36, and 96.28 ms for the previous folder scan. All
paired comparisons used a one-sided Wilcoxon test after non-normal difference
checks and gave `p = 0.000031` (15 pairs per size). See the
[validation figure](benchmarks/results/explicit_opening_v33.png),
[statistics](benchmarks/results/explicit_opening_v33_statistics.csv), and
[reproducible benchmark](benchmarks/benchmark_v33.py).

---

## What is new in 3.1

**Exact frame navigation.** While a video is selected, Left and Right pause
playback and move exactly one frame. Up and Down jump 10 frames by default;
change that value under **Tools > Video Frame Jump**. Outside video playback,
the same arrows retain normal previous/next-file navigation. Direct timeline
scrubbing remains available.

**Complete nested discovery by default.** New and upgraded installations scan
all subfolder levels unless a narrower value is deliberately selected under
**Tools > Subfolder Depth**. Existing depth choices and the 20,000-file safety
limit remain available.

In 12 paired runs against committed 3.0, exact forward/back frame commands took
3.95 ms median versus 5.29 ms for the old time seek (25.3% faster, paired
Wilcoxon p = 0.000488). Both were exact on the generated MJPG test clip. The
nested fixture confirms the functional change: 100/100 supported files are
found by default instead of 40/100. See the
[validation figure](benchmarks/results/frame_navigation_v31.png),
[raw results](benchmarks/results/frame_navigation_raw.csv), and
[reproducible benchmark](benchmarks/benchmark_v31.py).

---

## What is new in 3.0

**Fast in-app video.** MP4, MOV, AVI, MKV, WebM, WMV, MPEG, OGV, MTS, M2TS,
and TS files now appear beside figures and PDFs. Version 3.0 introduced play,
pause, direct scrubbing, stop, loop, keyboard control, and copying or saving the
current full-resolution frame.

Playback does not run through the still-image cache. One dedicated thread owns
the OpenCV/FFmpeg decoder, resizes frames to the viewport before handing them to
Tk, drops stale frames when presentation falls behind, and keeps only one
unpainted frame. This bounds memory and latency instead of allowing a playback
queue to grow. OpenCV is imported lazily, so image/PDF startup does not pay the
video backend's import cost.

**A coherent visual system.** The toolbar is now arranged into two clear rows,
with compact raised control groups and deterministic DPI-aware icons rather
than font-dependent symbols. The file and details panels have matching header
icons, videos have their own cyan list identity, and the transport has a themed
timeline. A generated image/video-aware mark supplies the app branding and a
new multi-resolution Windows icon; small functional icons remain code-native
so they stay sharp and consistent.

### Performance validation

Version 3.0 was benchmarked in 16 interleaved paired runs against the committed
2.2 code on the same disk. A paired Wilcoxon signed-rank test was selected
without assuming normally distributed timing differences.

| Existing path | 2.2 median | 3.0 median | Median change | Paired p |
|---|---:|---:|---:|---:|
| Cold document import | 326.0 ms | 325.9 ms | +1.2% paired median | 0.495 |
| 2,400-file folder scan | 119.7 ms | 106.5 ms | -0.9% paired median | 0.706 |

Neither existing path changed significantly. The 1080p video test delivered
its first frame in 65 ms, advanced at 1.015x real time, presented 29.9 frames
per second from a 30 fps source, and dropped no unpainted mailbox frames.
See the [benchmark figure](benchmarks/results/performance_v3.png),
[raw timings](benchmarks/results/performance_raw.csv), and
[reproducible script](benchmarks/benchmark_v3.py).

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
| `↑` `↓` | Previous / next file, or configurable frame jump in video |
| `←` `→` | Previous / next file, or one frame in video |
| `Space` / double click | Play / pause video |
| `PageUp` `PageDown` | Previous / next page |
| Wheel / two-finger scroll | Scroll within a PDF, then cross page boundaries; zoom other formats |
| `Ctrl` + wheel | Zoom towards the pointer |
| Drag / middle-drag | Pan |
| Double click | Toggle fit page / 100 % |
| `S` | Snip a region to the clipboard |
| `Ctrl+C` | Copy the whole page or current video frame |
| `Ctrl+Shift+V` | Copy exactly what is on screen |
| `Ctrl+B` | Show or hide the file list |
| `Ctrl+N` / `Ctrl+O` | New window / open folder |
| `Ctrl+1` `Ctrl+2` `Ctrl+3` `Ctrl+4` | Images / PDFs / videos / everything |
| `Ctrl+Shift+R` | Include subfolders / this folder only |
| Drag the divider | Resize the file list (double-click resets) |
| `Ctrl+B` `Ctrl+I` `Ctrl+T` | Sidebar / details / theme |
| `F5` `F9` `F11` | Rescan / slideshow / full screen |

---

## Layout

```
FigView.bat                canonical double-click launcher
Figure Viewer.bat          compatibility wrapper for the former name
figure_viewer_app.py       stable Python entry point for existing integrations
launch_figure_viewer.py    logging launcher used by the .bat and shortcuts
install_shortcuts.ps1      Desktop / Start Menu shortcuts
register_file_types.ps1    register with Windows as a media and PDF viewer
unregister_file_types.ps1  undo the above
viewer_python.ps1          shared "which Python has Pillow?" probe
figureviewer/
    theme.py               palettes, ttk styling, themed menu bar, tooltips
    icons.py               DPI-aware functional control icons
    winclip.py             Win32 clipboard (PNG + DIBV5 + DIB + HDROP + text)
    docs.py                scanning, document model, caches, worker pool
    video.py               bounded threaded OpenCV playback and thumbnails
    viewer.py              the viewer window: canvas, view transform, commands
    app.py                 Tk root, settings, window registry, single instance
    pdfedit.py             PDF edit sessions: commands, undo/redo, atomic save
    reflow.py              paragraph rewriting with reflow, find and replace
    forms.py               AcroForm model, identity presets, reviewed auto-fill
    signing.py             signature library, stamping, pyHanko certificates
    ocr.py                 integrated-Tesseract OCR, clean-up, language packs
    pdfexport.py           Word / Excel / PowerPoint / Markdown / HTML / text
    editui.py              the edit-mode controller wired into the viewer
    dialogs.py             themed dialogs: export, signatures, identities, OCR
```

Settings remain in `%APPDATA%\FigureViewerApp\settings.json` so the rename does
not discard preferences or break single-instance handoff with an older launcher.

Generated branding lives in `assets/figure-viewer-mark.png`; the 256 px runtime
copy and `figure-viewer.ico` are derived from that master.
