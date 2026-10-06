# FigView 1.1.0

A fast, themed viewer **and PDF workstation** for images, videos, and PDFs on
Windows. Open one file and only that file enters the list; open more files and
they append to the same session. Folder scanning starts only when you
explicitly choose or pass a folder. Auto reload preserves zoom, pan, playback
position, and list position.

FigView also **edits PDFs**: annotations, in-place text rewriting
with reflow, page management, redaction, form filling with saved identities,
drawn/typed/image signatures plus real certificate signing, OCR for scanned
documents and plain images, drag-to-select text copying, and one-click export
to Word, Excel, PowerPoint, Markdown, HTML and text. Everything runs locally;
nothing ever leaves the machine.

### Text highlights and comments

FigView exposes **Highlight**, **Comment**, and **Review** beside
Edit in the main toolbar. Select text across one or more lines, then click
Highlight or Comment. Clicking either tool without a selection enables repeated
drag annotations; press Escape to stop. Scanned pages require OCR first.

**Review** opens a docked, searchable list covering all pages, with filters for
comments and highlights. Select an annotation to jump to its passage, read the
full quote, edit its comment, change its color/opacity, or delete it. Five color
presets and opacity settings also apply to subsequent highlights. Export the
filtered list as a UTF-8 CSV for a review report.

Click a commented highlight or sticky note on the PDF to open its comment
directly in the Review pane. Imported PDF comments are supported, including
the author's name when present. The full comment preview scrolls and supports
selecting and copying text. Dragging across commented text still selects text.

Shortcuts: **Ctrl+Alt+H** highlights, **Ctrl+Alt+M** comments, and
**Ctrl+Alt+R** toggles the review pane. The selection context menu offers the
same actions. **Ctrl+Z / Ctrl+Y** undo and redo, including the entire creation of
a highlight with its comment in one step. **Ctrl+S** or **Save PDF** writes
standard PDF annotations to disk; changes remain unsaved until then. The quoted
passage is stored in the annotation's Subject field and the comment in Contents.

These tools are included in the 1.1.0 Windows downloads and the source launcher.

---

## Linux source installation

The Linux launcher provides smooth Noto Sans interface fonts, native clipboard
copying through X11/XWayland, mouse-wheel scrolling, desktop PDF registration,
and an optional Nautilus folder context menu. Windows launchers are unchanged.

Create an environment with the **Xft** Tk build, which enables antialiased fonts:

```bash
conda create -n figure-viewer -c conda-forge python=3.13 pip 'tk=*=xft_*' xclip
conda activate figure-viewer
python -m pip install pillow pymupdf opencv-python-headless pdf2docx python-docx openpyxl python-pptx pyhanko
python install_linux.py --default-pdf --nautilus
```

Install Noto Sans and DejaVu fonts through your distribution if missing. The
folder menu also requires the distribution's `nautilus-python` integration and
Nautilus 4.0 or 4.1 introspection API. Restart Nautilus after installation, once
any file transfers have finished. Omit `--default-pdf` to preserve the PDF default
or `--nautilus` to omit the folder menu. The installer writes only per-user files
and captures the active Python interpreter; desktop launches do not require
manual environment activation. Re-run it after moving the checkout/environment.

Run `python figview_linux.py /path/to/folder` directly, or choose **Ouvrir avec
FigView** on a selected folder. **Ouvrir ce dossier avec FigView** opens the folder
whose empty background was clicked. External folder requests show only direct
contents, even when an existing window previously used recursive scanning.
Subfolder scanning can still be enabled explicitly from the app's Tools menu.
The Linux launcher stores settings under `$XDG_CONFIG_HOME/FigureViewerApp`
(default `~/.config/FigureViewerApp`). Font family and size are configurable at
the top of `figview_linux.py`. Clipboard support requires X11 or XWayland.

Validate the installation using disposable fixtures (install `xvfb` if needed):

```bash
xvfb-run -a python figview_linux.py --self-test /tmp/figview-selftest.json
xvfb-run -a -s '-screen 0 1280x850x24' python linux/check_linux.py /tmp/figview-linux-checks
```

The Linux check verifies resolved fonts, exact folder scope on startup and real
second-process IPC, exclusion of child/sibling files, and produces a screenshot.

---

## Windows executable release

Download version 1.1.0 from the [GitHub release](https://github.com/Andrianarivelo/figure-viewer/releases/tag/v1.1.0):

- **Recommended for faster startup:** extract the entire portable ZIP and run `FigView/FigView.exe`.
  Keep its `_internal` folder beside the executable.
- **Standalone convenience:** run `FigView-v1.1.0-windows-x64.exe` directly.
  It unpacks dependencies on every launch. A single local release check took
  about 19-21 seconds, versus 1-2 seconds for the portable version.

Both builds include Python and the image, PDF, video, Office export, and signing
libraries. No Python installation is required. OCR language packs are downloaded
when requested from the app. Existing settings and file histories are preserved.
Version 1.0.0 is the first packaged release; older README development milestones
refer to the earlier source-only versions.

For a portable desktop shortcut, run
`powershell -ExecutionPolicy Bypass -File .\FigView\_internal\install_shortcuts.ps1`.
Use **Tools > Set as Default PDF Reader** for Windows file associations.

If Explorer or existing shortcuts still show the previous icon, run
`powershell -ExecutionPolicy Bypass -File .\FigView\_internal\refresh_windows_icons.ps1`.
For a source checkout, run the same script from the project folder. Add `-WhatIf`
to preview. It updates existing FigView icon references using a persistent,
versioned icon path and notifies Windows. File-opening commands and your chosen
default apps are preserved. Restart an already-running viewer to reload its
taskbar icon. Fresh shortcut installation and file-type registration use the
versioned icon automatically.

### Build from source

On 64-bit Windows with Python 3.13, double-click `Build Release.bat` or run
`python build_release.py`. The build creates an isolated `.venv-build`, installs
the pinned `requirements-build.txt`, builds both executables, checks them with
Python removed from their search path, and writes the ZIP, EXE, test reports,
and SHA256 checksums into `release/`.

The build uses [PyInstaller specification files](https://pyinstaller.org/en/stable/spec-files.html)
and bundles resources at their [runtime paths](https://pyinstaller.org/en/stable/runtime-information.html).
The built-in `--self-test report.json` command verifies disposable image, PDF,
video, export, signature, and multiple-opening workflows without using your files.

## Launch from source

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

### Open a folder from Explorer

```powershell
powershell -ExecutionPolicy Bypass -File D:\Apps\figure-viewer\register_folder_context.ps1
```

Adds **Open with FigView** to a folder's right-click menu, and to the
right-click menu on empty space inside a folder (opens the folder you are
in). Neither changes what double-clicking a folder does. Everything it
writes lives under `HKEY_CURRENT_USER`, so no administrator rights are
needed, and `unregister_folder_context.ps1` removes both entries.

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

**Select and copy text.** Drag over the text of any PDF page to select it,
word by word in reading order, exactly as in a desktop reader: dragging past
the end of a line takes the whole line, `Ctrl`+drag selects a rectangular
block (tables, columns), double and triple click select a word or a line,
`Shift`+click extends, `Ctrl+A` selects the page and `Esc` clears. `Ctrl+C`
copies the selection as text and falls back to the image copy when nothing
is selected, so the old shortcut keeps working. Dragging on blank page area
still pans. Selections live in page coordinates, so rotating or flipping the
view keeps them.

**Optimal zoom on open.** Files now open at the zoom that fills the canvas
width (the **Auto** toolbar button or `Ctrl+Shift+9` returns to it any time):
a landscape scan in a wide window fills it instead of sitting at a fixed
reading width, while a portrait page on an ultra-wide screen stops at 200 %
and an image never grows past twice its pixels. `View > Zoom on Open` keeps
the older behaviours available: fit width, fit page, or the A4 reading width.

**Index panel.** A PDF that carries bookmarks gets an **Index** panel on the
right (toolbar button or `Ctrl+Shift+I`): the outline as a tree with page
numbers in their own column, a filter box, expand and collapse buttons, and
the bookmark covering the page on screen highlighted as you read. Clicking an
entry jumps to its page and lands on the bookmark's target point, rotated
pages included. The panel appears only when the document has an outline and
shares the right dock with the details panel; drag the dock's edge to resize
it (double-click resets), and the width is remembered.

**Reader-first layout.** The action bar is grouped by purpose: zoom and
rotation, capture (Snip, OCR), editing (Edit, Highlight, Comment, Review),
and, at the far right where the panels themselves live, the Files, Index and
Details toggles with the auto-reload and auto-hide preferences beside them.
Only the two modal states (Snip, Edit) carry a text label; everything else
is an icon with a tooltip. When files were opened directly there is no folder
to type, so the path box gives way to a chip naming the file's folder (click
it to browse that folder), and a single opened file starts with the file list
collapsed until a second file or a folder arrives. The Images / PDF / Video /
All filter sits in the Files panel header, next to the list it filters. The
overview map appears only while the page is larger than the viewport and
fades after the view has been still for a moment. The canvas is a mid-dark
neutral rather than black, the status bar shows the file name and its
position in the list (pixel size, paper and dates live in Details), and the
file list opens at the normal density.

**Clickable links.** Links open in the default browser with one click. A PDF's
own link annotations are honoured (web addresses, jumps to another page or a
named destination), and addresses that are merely printed in the text are
detected as well: `https://...` and `www.` URLs, e-mail addresses (opened in
the mail client) and DOIs (opened at doi.org), with sentence punctuation and
brackets stripped. After OCR the same detection runs over an image, so the
links in a screenshot become clickable. Hovering shows a hand cursor, a
highlight and the destination in the status bar; a click without drag
follows it, while dragging still selects text or pans. The context menu
offers **Open Link** and **Copy Link Address**, and the Details panel counts
the links on the page. Only `http`, `https`, `mailto` and `ftp` targets are
ever handed to the system; file launch links are shown but never run.

**Combine PDFs.** `File > Combine PDFs…` (`Ctrl+Shift+M`, also in the file
list's context menu) opens a Combine window in the Adobe style: the files
are listed top to bottom in the order they will be joined, and you change
that order by dragging a row, with Move up / Move down (`Alt+Up` /
`Alt+Down`) or with **A to Z**. Add files, remove them, pick the destination
name, and press Combine; the result opens in the viewer. Files opened
explicitly are offered automatically, in folder mode the current PDF is.
Every page is copied as-is, the combined file gets one bookmark per source
with the source's own bookmarks nested under it, and the write is atomic, so
a source may even be chosen as the destination. Locked (password) files are
flagged and must be unlocked first.

From Explorer, select several PDFs, right-click, **Combine with FigView**:
the files arrive in the same window already in Explorer's alphabetical order
(`chapter2` before `chapter10`, like the folder listing), so one click
combines them, and the order can still be changed first. The entry is
registered by `register_file_types.ps1` on the PDF type itself, so it is
there whatever app owns the double-click; `unregister_file_types.ps1`
removes it.

**Redaction burns content out.** Applying redactions removes the text, blanks
the covered pixels of images (the rest of the picture survives) and drops
vector art fully inside a mark, so nothing recoverable stays under the black
box; the confirmation counts marks on every page, which is what Apply
removes. Edits made to one PDF stay parked with that file when you navigate
to another, and can never be saved over a different document.

**One-click OCR copy.** The **OCR** toolbar button (`Ctrl+Shift+T`, also
`Edit > Copy Image Text`) reads the text of the current image or scanned PDF
page and puts all of it on the clipboard in one go; images that were already
recognised and PDF pages with a text layer skip straight to the copy.

**OCR images too.** `Tools > Recognise Text` on a PNG, JPEG, TIFF or any other
picture reads its text with the same engine and clean-up pass; the result is a
selectable text layer on top of the image (same gestures, `Edit > Copy Page
Text` for everything at once), and `File > Save Image as Searchable PDF`
wraps the untouched picture and an invisible text layer into a one-page PDF
that any reader can search. Declared image resolution is honoured; images
without one are treated as 96 dpi screen captures and upsampled for
recognition, with the working bitmap capped at 4500 px on its long side.

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
`Ctrl+G`), text selection by dragging, text extraction (`Edit > Copy Page
Text`) and per-page export.
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
| Click a link | Open a web address, e-mail or DOI in the default browser; page links jump |
| `Ctrl+Shift+M` | Combine PDFs into one file, in the order you choose |
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
register_folder_context.ps1    "Open with FigView" on the folder right-click menu
unregister_folder_context.ps1  undo the above
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
    textlayer.py           selectable text layer: words, carets, ranges, ribbons
    textui.py              text selection, image OCR and searchable PDF glue
    indexui.py             docked outline (bookmarks) panel with navigation
    dialogs.py             themed dialogs: export, signatures, identities, OCR
```

Settings remain in `%APPDATA%\FigureViewerApp\settings.json` so the rename does
not discard preferences or break single-instance handoff with an older launcher.

The editable app icon is `assets/figure-viewer-mark.svg`, made entirely from
vector paths and gradients. Its layered figure sheets, cyan signal and amber
focal point are exported with transparent corners at 256, 512, 1024, 2048 and
4096 pixels. `assets/figure-viewer-mark.png` is the 4096 px export; the viewer
loads the 256 px copy. `figure-viewer.ico` includes Windows sizes from 16 to
256 px for the app, shortcuts and file associations.

To regenerate the assets, install the development-only dependencies with
`python -m pip install -r requirements-icon.txt`, then run `python build_icon.py`
or double-click `Build Icon.bat`. The source SVG is the single editable master.
The 1.1.0 executables embed this new icon; older executables retain their original
embedded artwork. The source viewer loads updated PNG and ICO assets on restart.
