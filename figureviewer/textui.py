"""Text selection and image OCR glue for the viewer window.

:class:`TextController` owns, for the page on screen:

* the current :class:`~figureviewer.textlayer.TextLayer` (a PDF page's words,
  or the OCR result of a raster image),
* the selection (a list of word indices plus the anchor/focus carets),
* its drawing on the canvas (tag ``textsel``),
* the clipboard actions (copy selection, select all, copy page text),
* the OCR flow for images (Tools > Recognise Text) and the searchable PDF
  export (File > Save as Searchable PDF).

Pure geometry and selection semantics live in :mod:`figureviewer.textlayer`;
this module is only the Tk glue, mirroring how :mod:`figureviewer.editui`
wraps :mod:`figureviewer.pdfedit`. Gesture precedence in the viewer is
edit tools -> snip -> text selection -> pan, so dragging on blank page area
still pans and every existing gesture keeps working.
"""
from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, Optional

from . import dialogs as DLG
from . import docs as D
from . import winclip
from .textlayer import TextLayer, source_box_to_logical

#: Cursor shown while the pointer is over selectable text.
CURSOR_TEXT = "xterm"
#: Tk modifier bits (Windows and X11 agree on these two).
STATE_SHIFT = 0x0001
STATE_CONTROL = 0x0004
#: Image OCR results kept in memory (keyed by file stamp and frame).
OCR_LAYER_CACHE = 32
#: Original file bytes are embedded as-is in a searchable PDF for these
#: single-frame formats; everything else is re-encoded as PNG.
EMBED_AS_IS = {".png", ".jpg", ".jpeg"}


class TextController:
    """Selection state and text actions for one :class:`ViewerWindow`."""

    def __init__(self, window) -> None:
        self.window = window
        self.app = window.app
        self.palette = window.palette
        self._layer: Optional[TextLayer] = None
        self._layer_key: Optional[tuple] = None
        # Image OCR results survive navigation: (entry.key, frame) -> data.
        self._ocr_layers: dict[tuple, TextLayer] = {}
        self._ocr_words: dict[tuple, list] = {}
        #: Selected word indices into the current layer, sorted.
        self.selected: list[int] = []
        self.anchor: Optional[int] = None
        self.focus: Optional[int] = None
        self._drag: Optional[dict] = None
        self._cursor_text = False

    # ------------------------------------------------------------ layer
    def _image_key(self, page: int) -> Optional[tuple]:
        current = self.window.current
        if current is None:
            return None
        return current.key + (int(page),)

    def layer(self) -> Optional[TextLayer]:
        """The text layer for the page on screen, or ``None``.

        PDF words are read once per (document, page, content version); an
        edit that bumps the version re-reads them and drops the selection,
        whose indices would otherwise point at the wrong words.
        """
        window = self.window
        document = window.document
        if document is None or window._video_session is not None:
            return None
        if document.kind == D.KIND_DOC:
            key = ("pdf", id(document), window.page,
                   int(getattr(document, "content_version", 0)))
            if key != self._layer_key:
                self._layer_key = key
                self._reset_selection()
                try:
                    raw = document.words(window.page)
                except Exception:
                    raw = []
                self._layer = TextLayer.from_words(raw)
            return self._layer
        key = self._image_key(window.page)
        return self._ocr_layers.get(key) if key is not None else None

    def has_text(self) -> bool:
        layer = self.layer()
        return layer is not None and len(layer) > 0

    def _enabled(self) -> bool:
        """Text gestures are live unless snip mode or an edit tool other
        than 'select' owns the drag."""
        window = self.window
        if window.document is None or window._video_session is not None:
            return False
        if window.snip_mode.get():
            return False
        edit = window.edit
        return not edit.active or edit.tool_var.get() == "select"

    # ------------------------------------------------------- geometry
    def _canvas_to_source(self, x: float, y: float) -> tuple[float, float]:
        window = self.window
        lx = window.off_x + x / window.scale
        ly = window.off_y + y / window.scale
        box = window._logical_box_to_source((lx, ly, lx, ly))
        return box[0], box[1]

    def _source_rect_to_canvas(self, rect) -> tuple[float, float, float, float]:
        window = self.window
        size = window.document.page_size(window.page)
        x0, y0, x1, y1 = source_box_to_logical(rect, size, window.rotation,
                                               window.flip_h, window.flip_v)
        return ((x0 - window.off_x) * window.scale,
                (y0 - window.off_y) * window.scale,
                (x1 - window.off_x) * window.scale,
                (y1 - window.off_y) * window.scale)

    # ------------------------------------------------------ selection
    def has_selection(self) -> bool:
        # Reading the layer can invalidate stale indices after an edit.
        layer = self.layer()
        return layer is not None and bool(self.selected)

    def selected_text(self) -> str:
        layer = self.layer()
        if layer is None or not self.selected:
            return ""
        return layer.text_for(self.selected)

    def _reset_selection(self) -> None:
        self.selected = []
        self.anchor = self.focus = None
        self._drag = None

    def clear_selection(self) -> bool:
        """Drop the selection; True when there was one to drop."""
        had = bool(self.selected)
        self._reset_selection()
        if had:
            self._redraw()
            self.window._update_status("Ready")
        return had

    def select_all(self) -> None:
        layer = self.layer()
        if layer is None or not len(layer):
            self._hint_no_text()
            return
        self.selected = layer.all_indices()
        self.anchor, self.focus = 0, len(layer) - 1
        self._redraw()
        self._announce()

    def select_word_at(self, event) -> bool:
        """Double click: select the word under the pointer."""
        if not self._enabled():
            return False
        layer = self.layer()
        if layer is None:
            return False
        hit = layer.hit(*self._canvas_to_source(event.x, event.y))
        if hit is None:
            return False
        self.selected = [hit]
        self.anchor = self.focus = hit
        self._drag = None
        self._redraw()
        self._announce()
        return True

    def select_line_at(self, event) -> bool:
        """Triple click: select the whole line under the pointer."""
        if not self._enabled():
            return False
        layer = self.layer()
        if layer is None:
            return False
        hit = layer.hit(*self._canvas_to_source(event.x, event.y))
        if hit is None:
            return False
        self.selected = layer.line_indices(hit)
        self.anchor, self.focus = self.selected[0], self.selected[-1]
        self._drag = None
        self._redraw()
        self._announce()
        return True

    def _announce(self) -> None:
        count = len(self.selected)
        if count:
            self.window.status_var.set(
                f"{count} word{'s' if count != 1 else ''} selected  ·  "
                "Ctrl+C copies  ·  Esc clears")

    def _hint_no_text(self) -> None:
        window = self.window
        document = window.document
        if document is None:
            return
        if document.kind == D.KIND_IMAGE:
            window.toast("No text layer yet: Tools > Recognise Text (OCR) "
                         "reads the text in this image")
        else:
            window.toast("No selectable text on this page; Tools > Recognise "
                         "Text (OCR) can read a scanned page")

    # -------------------------------------------------------- gestures
    def on_motion(self, event) -> None:
        """I-beam over text, default elsewhere; never fights a pan or snip."""
        window = self.window
        if window._pan_anchor is not None or self._drag is not None:
            return
        over = False
        if self._enabled():
            layer = self.layer()
            if layer is not None and len(layer):
                over = layer.hit(*self._canvas_to_source(event.x, event.y)) is not None
        if over == self._cursor_text:
            return
        self._cursor_text = over
        cursor = CURSOR_TEXT if over else ("crosshair" if window.snip_mode.get() else "")
        try:
            window.canvas.configure(cursor=cursor)
        except tk.TclError:
            pass

    def cursor_reset(self) -> None:
        """The viewer changed the cursor itself (pan, snip); forget ours so
        the next motion event re-evaluates it."""
        self._cursor_text = False

    def on_press(self, event) -> bool:
        """Start a selection when the press lands on text (or Ctrl is held
        for a rectangular block); otherwise clear and let the viewer pan."""
        if not self._enabled():
            return False
        layer = self.layer()
        if layer is None or not len(layer):
            return False
        sx, sy = self._canvas_to_source(event.x, event.y)
        control = bool(event.state & STATE_CONTROL)
        shift = bool(event.state & STATE_SHIFT)
        hit = layer.hit(sx, sy)
        if hit is None and not control:
            if self.selected:
                self.clear_selection()
            return False
        if shift and self.anchor is not None and not control:
            self.focus = layer.nearest(sx, sy)
            self.selected = layer.range_indices(self.anchor, self.focus)
            self._drag = {"mode": "range", "start": (sx, sy), "moved": True}
            self._redraw()
            self._announce()
            return True
        self.anchor = hit if hit is not None else layer.nearest(sx, sy)
        self.focus = self.anchor
        self._drag = {"mode": "rect" if control else "range",
                      "start": (sx, sy), "moved": False}
        self.selected = [] if control else [self.anchor]
        self._redraw()
        return True

    def on_drag(self, event) -> bool:
        drag = self._drag
        if drag is None:
            return False
        layer = self.layer()
        if layer is None:
            self._drag = None
            return False
        self._autoscroll(event)
        sx, sy = self._canvas_to_source(event.x, event.y)
        drag["moved"] = True
        if drag["mode"] == "rect":
            x0, y0 = drag["start"]
            self.selected = layer.rect_indices((min(x0, sx), min(y0, sy),
                                                max(x0, sx), max(y0, sy)))
        else:
            self.focus = layer.nearest(sx, sy)
            self.selected = layer.range_indices(self.anchor, self.focus)
        self._redraw()
        self._announce()
        return True

    def on_release(self, event) -> bool:
        drag = self._drag
        if drag is None:
            return False
        self._drag = None
        if not drag["moved"]:
            # A plain click on a word places no selection, like any editor.
            self._reset_selection()
            self._redraw()
            self.window._update_status("Ready")
        elif self.selected:
            self._announce()
            self.window.edit.review.selection_finished()
        else:
            self.window._update_status("Ready")
        return True

    def _autoscroll(self, event) -> None:
        """Pan while a drag runs past the canvas edge so long passages can
        be selected without releasing the button."""
        window = self.window
        cw, ch = window._canvas_size()
        dx = event.x if event.x < 0 else (event.x - cw if event.x > cw else 0)
        dy = event.y if event.y < 0 else (event.y - ch if event.y > ch else 0)
        if dx or dy:
            window._pan_by(dx * 0.35, dy * 0.35)

    # --------------------------------------------------------- drawing
    def _redraw(self) -> None:
        try:
            self.window.canvas.delete("textsel")
        except tk.TclError:
            return
        self.draw()

    def draw(self) -> None:
        """Draw the selection ribbon; called after every _paint."""
        window = self.window
        canvas = window.canvas
        canvas.delete("textsel")
        if not self.selected or window.document is None:
            return
        layer = self.layer()
        if layer is None:
            return
        colour = self.palette.accent
        for rect in layer.rects_for(self.selected):
            x0, y0, x1, y1 = self._source_rect_to_canvas(rect)
            # Stipple stands in for alpha (Tk canvas has none): the glyphs
            # stay readable through the accent ribbon, as in snip mode.
            canvas.create_rectangle(x0 - 1, y0 - 1, x1 + 1, y1 + 1,
                                    fill=colour, outline="", stipple="gray50",
                                    tags="textsel")

    # ------------------------------------------------------- lifecycle
    def on_page_changed(self) -> None:
        self.clear_selection()

    def on_document_loaded(self) -> None:
        self._layer = None
        self._layer_key = None
        self._reset_selection()
        self._cursor_text = False

    # ------------------------------------------------------- clipboard
    def copy_selection(self) -> bool:
        """Copy the selected words; False when nothing is selected."""
        text = self.selected_text()
        if not text.strip():
            return False
        try:
            winclip.copy_text(text)
        except Exception as exc:
            messagebox.showerror("Clipboard error", str(exc), parent=self.window)
            return True
        count = len(self.selected)
        self.window.toast(f"Copied {count} word{'s' if count != 1 else ''} "
                          f"({len(text)} characters)")
        return True

    def page_text(self) -> str:
        """All text on the page: the PDF text layer, or an image's OCR."""
        window = self.window
        document = window.document
        if document is None:
            return ""
        if document.kind == D.KIND_DOC:
            return document.text(window.page)
        layer = self.layer()
        return layer.text_for(layer.all_indices()) if layer is not None else ""

    def copy_page_text(self) -> None:
        text = self.page_text()
        if not text.strip():
            self._hint_no_text()
            return
        try:
            winclip.copy_text(text)
        except Exception as exc:
            messagebox.showerror("Clipboard error", str(exc), parent=self.window)
            return
        self.window.toast(f"Copied {len(text)} characters of page text")

    # ------------------------------------------------------ context menu
    def context_items(self, menu: tk.Menu, event) -> None:
        window = self.window
        document = window.document
        if document is None or window._video_session is not None:
            return
        has_text = self.has_text()
        menu.add_separator()
        if self.selected:
            menu.add_command(label="Copy Selected Text", accelerator="Ctrl+C",
                             command=self.copy_selection)
        if has_text:
            menu.add_command(label="Select All Text", accelerator="Ctrl+A",
                             command=self.select_all)
            menu.add_command(label="Copy Page Text", command=self.copy_page_text)
        if document.kind == D.KIND_IMAGE:
            menu.add_command(label="Recognise Text (OCR)…", command=self.open_ocr)
            if has_text:
                menu.add_command(label="Save as Searchable PDF…",
                                 command=self.save_searchable_pdf)

    # ------------------------------------------------------------- OCR
    def open_ocr(self) -> None:
        """Tools > Recognise Text: PDFs go to the edit studio's overlay
        flow, images get a selectable text layer of their own."""
        window = self.window
        document = window.document
        if document is None:
            return
        if window._video_session is not None:
            window.toast("OCR works on still images and PDFs, not videos")
            return
        if document.kind == D.KIND_DOC:
            window.edit.open_ocr()
            return
        self._start_image_ocr()

    def _start_image_ocr(self, then: Optional[Callable[[], None]] = None) -> None:
        from . import app as app_module
        from . import ocr as ocr_module
        window = self.window
        document = window.document
        entry = window.current
        if document is None or entry is None:
            return
        if not ocr_module.ocr_available():
            messagebox.showinfo(
                "OCR", "The integrated OCR engine is unavailable in this "
                "PyMuPDF build.", parent=window)
            return
        width, height = document.page_size(window.page)
        frames = document.page_count
        summary = (f"Read the text in {entry.name} ({width} x {height} px). "
                   "Recognised words become selectable on the image: drag "
                   "to select, Ctrl+A selects all, Ctrl+C copies, and File > "
                   "Save as Searchable PDF keeps the text with the picture.")
        config = DLG.OcrDialog(
            window, self.palette, self.app.settings, window.edit._packs(),
            page_count=frames, scan_pages=frames, summary=summary,
            action="Recognise", scope_labels=("All frames", "Current frame"),
            show_scope=frames > 1).wait()
        if not config:
            return
        settings = self.app.settings
        settings["ocr_language"] = config["lang"]
        settings["ocr_variant"] = config["variant"]
        settings["ocr_preprocess"] = config["preprocess"]
        app_module.save_settings(settings)
        pages = ([window.page] if config["scope"] == "page" or frames < 2
                 else list(range(frames)))
        self._run_image_ocr(config, pages, then)

    def _run_image_ocr(self, config: dict, pages: list[int],
                       then: Optional[Callable[[], None]]) -> None:
        from . import ocr as ocr_module
        window = self.window
        document = window.document
        entry = window.current
        manager = window.edit._packs()
        declared = getattr(document, "dpi", None)

        def job(progress, cancel):
            if not manager.has(config["lang"], config["variant"]):
                progress(0, 1, f"Downloading {config['lang']} language pack…")
                manager.download(
                    config["lang"], config["variant"],
                    progress_cb=lambda done_b, total_b:
                        progress(done_b, total_b or max(done_b, 1),
                                 "Downloading language pack"),
                    cancel=cancel)
            results: dict[int, list] = {}
            for index, page in enumerate(pages):
                if cancel.is_set():
                    break
                progress(index, len(pages),
                         f"Recognising frame {index + 1}/{len(pages)}")
                frame = document.frame(page)
                results[page] = ocr_module.image_ocr(
                    frame, lang=config["lang"], variant=config["variant"],
                    preprocess_on=config["preprocess"],
                    tessdata_dir=ocr_module.default_tessdata_root(),
                    dpi=declared)
            return results

        def done(results: dict):
            if window._destroyed or window.current is not entry:
                return
            total = 0
            for page, words in results.items():
                key = entry.key + (int(page),)
                self._remember(key, words)
                total += len(words)
            if not results:
                window.toast("OCR cancelled")
                return
            self._reset_selection()
            self._redraw()
            if total:
                window.toast(f"Recognised {total} words: drag over the text to "
                             "select it, Ctrl+A selects all, Ctrl+C copies")
            else:
                window.toast("No text recognised in this image")
            window._update_info()
            if then is not None and total:
                then()

        def failed(exc):
            messagebox.showerror("OCR", str(exc), parent=window)

        DLG.run_with_progress(window, self.palette, "Recognising text",
                              job, done, failed=failed)

    def _remember(self, key: tuple, words: list) -> None:
        from .textlayer import Word
        layer = TextLayer(Word(w.rect_px, w.text, w.block, w.line)
                          for w in words)
        self._ocr_words[key] = list(words)
        self._ocr_layers[key] = layer
        while len(self._ocr_layers) > OCR_LAYER_CACHE:
            oldest = next(iter(self._ocr_layers))
            self._ocr_layers.pop(oldest, None)
            self._ocr_words.pop(oldest, None)

    def ocr_word_count(self) -> Optional[int]:
        """Words recognised on the current image frame, for the info panel."""
        window = self.window
        if window.document is None or window.document.kind != D.KIND_IMAGE:
            return None
        key = self._image_key(window.page)
        layer = self._ocr_layers.get(key) if key is not None else None
        return len(layer) if layer is not None else None

    # ------------------------------------------------- searchable PDF
    def save_searchable_pdf(self) -> None:
        """Write the current image plus its OCR text as a one-page PDF."""
        from . import ocr as ocr_module
        window = self.window
        document = window.document
        entry = window.current
        if document is None or entry is None:
            return
        if document.kind != D.KIND_IMAGE:
            window.toast("PDFs already carry their text: Ctrl+S saves them")
            return
        key = self._image_key(window.page)
        if key not in self._ocr_words:
            # Recognise first, then come back here automatically.
            self._start_image_ocr(then=self.save_searchable_pdf)
            return
        target = filedialog.asksaveasfilename(
            parent=window, title="Save as searchable PDF",
            defaultextension=".pdf", initialfile=entry.path.stem + ".pdf",
            initialdir=str(entry.path.parent),
            filetypes=[("PDF document", "*.pdf")])
        if not target:
            return
        words = self._ocr_words[key]
        page = window.page
        frame = document.frame(page)
        dpi = ocr_module.image_ocr_dpi(frame.size, getattr(document, "dpi", None))[0]
        original = None
        if document.page_count == 1 and entry.path.suffix.lower() in EMBED_AS_IS:
            buffer = getattr(document, "_buffer", None)
            original = buffer.getvalue() if buffer is not None else None

        def job(progress, cancel):
            progress(0, 1, "Building the PDF")
            data = ocr_module.searchable_pdf_from_image(
                frame, words, dpi=dpi, image_bytes=original)
            path = Path(target)
            part = path.with_name(path.name + ".part")
            part.write_bytes(data)
            os.replace(part, path)
            return path, len(words)

        def done(result):
            path, count = result
            window.toast(f"Saved {path.name} with {count} searchable words")
            window.rescan()

        DLG.run_with_progress(window, self.palette, "Saving searchable PDF",
                              job, done, cancellable=False)
