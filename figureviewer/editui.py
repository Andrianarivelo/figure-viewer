"""Edit-mode controller: the Tk glue between ViewerWindow and the PDF studio.

Everything editing-related that touches Tk lives here or in
:mod:`figureviewer.dialogs`; the headless machinery lives in ``pdfedit``,
``forms``, ``signing``, ``reflow``, ``ocr`` and ``pdfexport``.

Integration contract with the viewer (kept deliberately narrow):

* the viewer calls ``on_press/on_drag/on_release/on_motion`` first and stops
  if the controller returns True, exactly like the snip mode branch;
* ``draw_overlay`` is called at the end of every ``_paint``; all controller
  canvas items carry the tag ``"edit"`` so the scene pass never disturbs them;
* ``attach_if_parked`` runs in the load() done-callback so a dirty session
  survives navigation, window rebuilds and theme toggles;
* ``reload_decision`` gates the two auto-reload sites, so the file watcher
  and an unsaved edit session can never fight over the document.

Edits are document-scoped, not mode-scoped: leaving edit mode keeps the
session (and the title's dirty marker) until the user saves or discards.
"""
from __future__ import annotations

import hashlib
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from PIL import Image, ImageTk

from . import dialogs as DLG
from . import docs as D
from . import pdfedit as PE
from .theme import bind_tooltip, toolbar_button
from .reviewui import ReviewController

HANDLE_PX = 5              # selection handle half-size, in canvas pixels
HIT_SLOP_PT = 3.0          # hit-test tolerance in PDF points
INK_MERGE_MS = 800         # pen strokes closer than this merge into one annot
MIN_DRAG_PX = 4            # below this a "drag" is treated as a click
THUMB_H = 64               # thumb strip page height in pixels
SIGN_DEFAULT_WIDTH_PT = 160.0

#: Palette tools in display order: (name, icon, tooltip, key).
TOOLS = (
    ("select", "select", "Select and move annotations", "V"),
    ("highlight", "highlighter", "Highlight text", "H"),
    ("underline", "underline", "Underline text", "U"),
    ("strikeout", "strikeout", "Strike text through", "K"),
    ("squiggly", "squiggly", "Squiggly underline", "Q"),
    ("text", "textbox", "Add text anywhere", "T"),
    ("note", "note", "Add a sticky note", "N"),
    ("ink", "ink", "Draw freehand", "P"),
    ("rect", "shape", "Rectangle", "R"),
    ("ellipse", "shape", "Ellipse", "O"),
    ("line", "shape", "Line", "L"),
    ("arrow", "arrow", "Arrow", "A"),
    ("redact", "redact", "Mark content for permanent removal", "X"),
)
MARKUP_TOOLS = {"highlight", "underline", "strikeout", "squiggly"}
SHAPE_TOOLS = {"rect", "ellipse", "line", "arrow"}


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = (value or "#000000").lstrip("#")
    try:
        return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:
        return (0.0, 0.0, 0.0)


def _rgb_to_hex(rgb) -> str:
    try:
        r, g, b = rgb
        return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
    except Exception:
        return "#000000"


class EditController:
    """Owns edit state and UI for one ViewerWindow."""

    def __init__(self, window) -> None:
        self.window = window
        self.app = window.app
        self.palette = window.palette
        self.session: Optional[PE.EditSession] = None
        self.owned = False
        self.active = False
        self.tool_var = tk.StringVar(value="select")
        self.edit_mode_var = tk.BooleanVar(value=False)

        # Gesture state -------------------------------------------------
        self._gesture: Optional[dict] = None       # in-progress drag
        self._selected_uid: Optional[str] = None
        self._pending_ink: list[list[tuple[float, float]]] = []
        self._ink_job: Optional[str] = None
        self._editor: Optional[dict] = None        # open overlay text editor
        self._placing: Optional[dict] = None       # signature placement
        self._sign_photo: Optional[ImageTk.PhotoImage] = None
        self._find_state: Optional[dict] = None
        self._find_dialog = None
        self._expected_stamp = None                # our own save's stamp
        self._saved_hash: Optional[str] = None
        self._conflict_notified = False

        # Lazy singletons ----------------------------------------------
        self._sig_library = None
        self._identity_store = None
        self._pack_manager = None
        self._save_inflight = False
        self._fields_cache: Optional[tuple[int, list]] = None
        self._ocr_words_cache: dict = {}

        # UI (built lazily on first use) -------------------------------
        self.palette_row: Optional[ttk.Frame] = None
        self.form_bar: Optional[ttk.Frame] = None
        self.thumb_strip: Optional["ThumbStrip"] = None
        self._redact_bar: Optional[ttk.Frame] = None
        self._undo_button = None
        self._redo_button = None
        self._save_button = None
        self._props_popover: Optional[tk.Toplevel] = None
        self.review = ReviewController(self)

    # ------------------------------------------------------------ helpers
    @property
    def dirty(self) -> bool:
        return self.session is not None and self.session.dirty

    def _document(self):
        return self.window.document

    def _page(self) -> int:
        return self.window.page

    def _defaults(self) -> dict:
        return self.app.settings.get("annot_defaults", {})

    def available(self) -> bool:
        """PDF editing applies only to real PDF files with PyMuPDF present."""
        return D.HAVE_FITZ and self.window._is_pdf()

    # -------------------------------------------------- session lifecycle
    def _ensure_session(self) -> bool:
        """Attach (or adopt) the one edit session for the current file."""
        if not self.available() or self.window.current is None:
            return False
        if self.session is not None:
            return True
        try:
            session, owned = self.app.acquire_session(
                self.window, self.window.current,
                base_dpi=float(getattr(self.window.document, "base_dpi",
                                       self.window.pdf_dpi.get())))
        except Exception as exc:
            messagebox.showerror("Edit PDF", str(exc), parent=self.window)
            return False
        if not owned:
            self.window.toast("This PDF is being edited in another window")
            return False
        self.session = session
        self.owned = True
        session.on_change.append(self._on_session_change)
        session.ui_dispatch = self._dispatch
        self._wire_ocr_provider(session)
        self.window.document = session.document
        self.window._minimap_key = None
        self.window._invalidate()
        self.window.redraw_now()
        self._refresh_history_buttons()
        self.window._update_window_title()
        return True

    def _dispatch(self, func) -> None:
        """Run ``func`` on the Tk thread (session change events raised by a
        worker-thread checkpoint arrive here)."""
        if self.window._destroyed:
            return
        try:
            self.window.after(0, func)
        except tk.TclError:
            pass

    def _wire_ocr_provider(self, session: PE.EditSession) -> None:
        """Give the session OCR-backed text for scanned pages, when possible."""
        if session.ocr_text_provider is not None:
            return
        try:
            from . import ocr as ocr_module
            if not ocr_module.ocr_available():
                return
            settings = self.app.settings
            session.ocr_text_provider = ocr_module.make_text_provider(
                tessdata_dir=ocr_module.default_tessdata_root(),
                lang=settings.get("ocr_language", "eng"),
                variant=settings.get("ocr_variant", "fast"),
                preprocess_on=bool(settings.get("ocr_preprocess", True)))
        except Exception:
            pass

    def attach_if_parked(self) -> None:
        """Re-adopt a parked session when its file is loaded again.

        Runs in load()'s done callback. This is what carries dirty state and
        undo history across navigation and the theme toggle's rebuild.
        """
        if self.window.current is None or not D.HAVE_FITZ:
            return
        # Navigating to another file: the previous file's session stays
        # parked (dirty state and history intact) but must stop being THE
        # session of this window, or gestures would edit the invisible old
        # document and Ctrl+S would write it over the new file.
        if (self.session is not None
                and self.session.entry.path != self.window.current.path):
            self.deactivate()
            self._detach_session(close=False)
        parked = self.app.peek_session(self.window.current.path)
        if parked is None:
            return
        if self.session is parked:
            self.window.document = parked.document
            self.window._invalidate()
            self.window.redraw_now()
            return
        session, owned = self.app.acquire_session(
            self.window, self.window.current,
            base_dpi=float(getattr(self.window.document, "base_dpi",
                                   self.window.pdf_dpi.get())))
        if not owned:
            return
        self.session = session
        self.owned = True
        if self._on_session_change not in session.on_change:
            session.on_change.append(self._on_session_change)
        session.ui_dispatch = self._dispatch
        self._wire_ocr_provider(session)
        self.window.document = session.document
        self.window._minimap_key = None
        self.window._invalidate()
        self.window.redraw_now()
        self._refresh_history_buttons()
        self.window._update_window_title()
        self._sync_form_bar()

    def _detach_session(self, *, close: bool) -> None:
        self.review.close_panel()
        self.review.cancel_mode()
        session = self.session
        self.session = None
        self.owned = False
        self._selected_uid = None
        self._fields_cache = None
        if session is not None:
            try:
                session.on_change.remove(self._on_session_change)
            except ValueError:
                pass
            if session.ui_dispatch == self._dispatch:
                session.ui_dispatch = None
            # Release under the SESSION's path: after navigation the
            # window's current file is already a different one.
            self.app.release_session(session.entry.path, close=close)
        self._refresh_history_buttons()
        self.window._update_window_title()

    def discard_session(self) -> None:
        """Drop all edits and reload the pristine document from the cache."""
        current = self.window.current
        # Leave edit mode while the session document is still open: the
        # overlay redraw inside deactivate() reads find matches and
        # selection geometry through it.
        self._find_state = None
        self.deactivate()
        self._detach_session(close=True)
        if current is not None:
            self.window.load(current, keep_view=True, note="Edits discarded")

    def _on_session_change(self, event: PE.ChangeEvent) -> None:
        window = self.window
        if window._destroyed:
            return
        window.document.invalidate_tiles()
        window._minimap_key = None
        window._invalidate()
        window.redraw_now()
        window._update_info()
        window._update_window_title()
        self._refresh_history_buttons()
        if event.structural:
            window._sync_pdf_controls()
            window._update_readouts()
            if self.thumb_strip is not None:
                self.thumb_strip.refresh()
            self._fields_cache = None
            window.index.refresh()      # page ops move bookmark targets
        self._ocr_words_cache.clear()
        self.review.refresh()

    # ------------------------------------------------------------- toggle
    def toggle_mode(self) -> None:
        if self.active:
            self.deactivate()
        else:
            self.activate()

    def activate(self) -> None:
        self.review.cancel_mode()
        if self.active:
            return
        if not self.available():
            self.window.toast("Edit mode is for PDF files")
            self.edit_mode_var.set(False)
            return
        if self.window.snip_mode.get():
            self.window.snip_mode.set(False)
            self.window._snip_mode_changed()
        if not self._ensure_session():
            self.edit_mode_var.set(False)
            return
        self.active = True
        self.edit_mode_var.set(True)
        self.tool_var.set("select")
        self._build_palette()
        self.palette_row.grid()
        self._sync_form_bar()
        self.window.toast("Edit mode: H highlight, T text, P ink, Esc leaves")

    def deactivate(self) -> None:
        if not self.active:
            return
        self.commit_open_editor()
        self._cancel_gesture()
        self._flush_ink(force=True)
        self.active = False
        self.edit_mode_var.set(False)
        self._selected_uid = None
        if self.palette_row is not None:
            self.palette_row.grid_remove()
        if self.thumb_strip is not None:
            self.thumb_strip.grid_remove()
        self._close_props()
        self._redraw_overlay()
        # A dirty session stays parked on purpose: Ctrl+S still saves, and
        # the title keeps its marker until save or discard.

    def detach_for_close(self) -> None:
        """Window teardown: commit editors, cancel timers, release ownership."""
        self.review.close_panel()
        self.commit_open_editor()
        if self._ink_job:
            try:
                self.window.after_cancel(self._ink_job)
            except Exception:
                pass
            self._ink_job = None
        if self._find_dialog is not None:
            try:
                self._find_dialog.destroy()
            except Exception:
                pass
        self._close_props()
        if self.session is not None and self.window.current is not None:
            self.app.release_session(self.window.current.path, close=False)

    def confirm_close(self) -> bool:
        """Window-close gate. True = go ahead and close."""
        if not self.dirty:
            return True
        name = self.window.current.name if self.window.current else "document"
        choice = DLG.ask_save_discard_cancel(self.window, self.palette, name)
        if choice == "cancel":
            return False
        if choice == "save":
            return self.save(synchronous=True)
        self._detach_session(close=True)
        return True

    # ------------------------------------------------------------- saving
    def save(self, *, synchronous: bool = False) -> bool:
        """Atomic save to the original path. Returns True when started/done."""
        session = self.session
        if session is None or self.window.current is None:
            return False
        if session.entry.path != self.window.current.path:
            # Never write one file's edits over another; this is the last
            # line of defence behind attach_if_parked's parking.
            messagebox.showerror(
                "Save PDF", f"The pending edits belong to {session.entry.name}, "
                f"not to {self.window.current.name}. Open that file again to "
                "save them.", parent=self.window)
            return False
        if self._save_inflight:
            self.window.toast("A save is already in progress")
            return False
        self.commit_open_editor()
        if session.document.has_signatures:
            if not messagebox.askokcancel(
                    "Signed document",
                    "This PDF carries digital signatures. Saving edits will "
                    "invalidate them. Save anyway?", parent=self.window):
                return False
        path = self.window.current.path
        try:
            data = session.save_bytes()
        except Exception as exc:
            messagebox.showerror("Save PDF", str(exc), parent=self.window)
            return False
        # The bytes above correspond to THIS history top; an edit made while
        # the write runs must leave the session dirty.
        saved_top = session.history_top()

        def write() -> D.Entry:
            return self._write_atomic(path, data)

        if synchronous:
            try:
                fresh = write()
            except Exception as exc:
                messagebox.showerror("Save PDF", str(exc), parent=self.window)
                return False
            self._after_save(fresh, data, saved_top)
            return True

        def done(fresh: D.Entry) -> None:
            self._save_inflight = False
            self._after_save(fresh, data, saved_top)

        def failed(exc: BaseException) -> None:
            self._save_inflight = False
            messagebox.showerror("Save PDF", str(exc), parent=self.window)

        self._save_inflight = True
        self.window.status_var.set("Saving…")
        self.app.worker.submit(write, done, failed)
        return True

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> D.Entry:
        """temp + fsync + os.replace on the same volume; returns fresh Entry."""
        tmp = path.parent / f".{path.name}.fv-save-{os.getpid()}.tmp"
        # Sweep orphans from crashed saves in this directory while we are here.
        try:
            for stale in path.parent.glob(f".{path.name}.fv-save-*.tmp"):
                stale.unlink(missing_ok=True)
        except Exception:
            pass
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        stat = path.stat()
        return D.Entry(path, stat.st_size, stat.st_mtime, D.KIND_DOC)

    def _after_save(self, fresh: D.Entry, data: bytes, saved_top=None) -> None:
        window = self.window
        if window._destroyed or self.session is None:
            return
        self._expected_stamp = fresh.stamp
        self._saved_hash = hashlib.sha256(data).hexdigest()
        self._conflict_notified = False
        self.session.mark_saved(fresh, saved_top)
        window.current = fresh
        window.entries = [fresh if e.path == fresh.path else e
                          for e in window.entries]
        window.filtered = [fresh if e.path == fresh.path else e
                           for e in window.filtered]
        self.app.cache.evict(fresh.path)
        self._fields_cache = None
        window._update_window_title()
        window.toast(f"Saved {fresh.name}")
        self._refresh_history_buttons()

    def save_as(self) -> None:
        session = self.session
        if self.window.current is None:
            return
        if session is None:
            if not self._ensure_session():
                return
            session = self.session
        target = filedialog.asksaveasfilename(
            parent=self.window, title="Save PDF as", defaultextension=".pdf",
            initialfile=self.window.current.path.stem + "-edited.pdf",
            initialdir=str(self.window.current.path.parent),
            filetypes=[("PDF", "*.pdf")])
        if not target:
            return
        try:
            data = session.save_bytes()
            self._write_atomic(Path(target), data)
        except Exception as exc:
            messagebox.showerror("Save PDF As", str(exc), parent=self.window)
            return
        self.window.toast(f"Saved copy to {Path(target).name}")
        self.window.add_file(Path(target))

    def save_shortcut(self) -> bool:
        """Ctrl+S: session save when one is dirty, else the legacy handler."""
        if self.session is not None and (self.dirty or self.active):
            self.save()
            return True
        return False

    # ------------------------------------------------- watcher reconciliation
    def reload_decision(self, fresh: D.Entry) -> str:
        """'adopt' our own save, 'block' a conflicting reload, or 'reload'."""
        if self.session is None:
            return "reload"
        if self._expected_stamp is not None and fresh.stamp == self._expected_stamp:
            self._expected_stamp = None
            return "adopt"
        if self._saved_hash is not None:
            try:
                if hashlib.sha256(fresh.path.read_bytes()).hexdigest() == self._saved_hash:
                    return "adopt"   # OneDrive/AV double-touch of our own bytes
            except OSError:
                pass
        if self.session.dirty:
            if not self._conflict_notified:
                self._conflict_notified = True
                self.window.toast(
                    "File changed on disk. Your unsaved edits are kept; "
                    "save writes them over it, or discard via Edit menu.")
            return "block"
        # A clean parked session for a file rewritten by someone else has
        # nothing worth keeping: drop it and follow the disk.
        self._detach_session(close=True)
        return "reload"

    def adopt_entry(self, fresh: D.Entry) -> None:
        """Take a stamp change that is our own save into the bookkeeping."""
        self.window.current = fresh
        if self.session is not None:
            self.session.mark_saved(fresh)

    # ----------------------------------------------------------- geometry
    def _factor(self) -> float:
        document = self._document()
        base_dpi = float(getattr(document, "base_dpi", 150.0) or 150.0)
        return base_dpi / 72.0

    def _source_box_to_logical(self, box) -> tuple[float, float, float, float]:
        """Forward transform: source page pixels -> logical (rotated) pixels."""
        window = self.window
        width, height = window.document.page_size(window.page)
        x0, y0, x1, y1 = box
        rotation = window.rotation % 360
        if rotation == 0:
            logical = (x0, y0, x1, y1)
        elif rotation == 90:
            logical = (height - y1, x0, height - y0, x1)
        elif rotation == 180:
            logical = (width - x1, height - y1, width - x0, height - y0)
        else:
            logical = (y0, width - x1, y1, width - x0)
        lw, lh = (height, width) if rotation % 180 else (width, height)
        x0, y0, x1, y1 = logical
        if window.flip_h:
            x0, x1 = lw - x1, lw - x0
        if window.flip_v:
            y0, y1 = lh - y1, lh - y0
        return (x0, y0, x1, y1)

    def canvas_to_points(self, x: float, y: float) -> tuple[float, float]:
        window = self.window
        lx = window.off_x + x / window.scale
        ly = window.off_y + y / window.scale
        box = window._logical_box_to_source((lx, ly, lx, ly))
        rect = self._source_rect_to_points(box)
        return rect[0], rect[1]

    def _source_rect_to_points(self, rect):
        """Rendered source pixels -> unrotated PDF annotation coordinates.

        A PDF's intrinsic /Rotate is separate from the viewer's own rotation.
        Text layers follow the rendered page, while PDF annotation APIs require
        unrotated coordinates. Undoing the native rotation prevents displaced
        highlights and comments on landscape pages.
        """
        factor = self._factor()
        points = D.fitz.Rect(*(value / factor for value in rect))
        document = self._document()
        with document._lock:
            page = document._page(self._page())
            if page.rotation:
                points = points * page.derotation_matrix
        return tuple(points)

    def points_rect_to_canvas(self, rect_pts) -> tuple[float, float, float, float]:
        # Annotation geometry is unrotated; the source image honors /Rotate.
        document = self._document()
        with document._lock:
            page = document._page(self._page())
            if page.rotation:
                rect_pts = tuple(D.fitz.Rect(*rect_pts) * page.rotation_matrix)
        factor = self._factor()
        source = (rect_pts[0] * factor, rect_pts[1] * factor,
                  rect_pts[2] * factor, rect_pts[3] * factor)
        x0, y0, x1, y1 = self._source_box_to_logical(source)
        window = self.window
        return ((x0 - window.off_x) * window.scale,
                (y0 - window.off_y) * window.scale,
                (x1 - window.off_x) * window.scale,
                (y1 - window.off_y) * window.scale)

    def _canvas_rect_to_points(self, x0, y0, x1, y1) -> tuple[float, float, float, float]:
        ax, ay = self.canvas_to_points(x0, y0)
        bx, by = self.canvas_to_points(x1, y1)
        return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))

    # -------------------------------------------------------------- input
    def on_press(self, event) -> bool:
        if self._editor is not None:
            # A click outside an open overlay editor commits it first.
            self.commit_open_editor()
        if self._placing is not None:
            return self._sign_press(event)
        if not self.active:
            return self._form_click(event, require_active=False)
        if self._document() is None:
            return False
        tool = self.tool_var.get()
        point = self.canvas_to_points(event.x, event.y)
        if tool == "select":
            return self._select_press(event, point)
        if tool in MARKUP_TOOLS or tool in SHAPE_TOOLS or tool == "redact":
            self._gesture = {"tool": tool, "start": (event.x, event.y),
                             "now": (event.x, event.y)}
            return True
        if tool == "ink":
            self._gesture = {"tool": "ink", "points": [(event.x, event.y)]}
            return True
        if tool == "text":
            self._gesture = {"tool": "text", "start": (event.x, event.y),
                             "now": (event.x, event.y)}
            return True
        if tool == "note":
            self._add_note_at(point)
            return True
        return False

    def on_drag(self, event) -> bool:
        if self._placing is not None:
            return self._sign_drag(event)
        gesture = self._gesture
        if gesture is None:
            return False
        if gesture["tool"] == "ink":
            gesture["points"].append((event.x, event.y))
        elif gesture["tool"] == "move":
            gesture["now"] = (event.x, event.y)
        elif gesture["tool"] == "resize":
            gesture["now"] = (event.x, event.y)
        else:
            gesture["now"] = (event.x, event.y)
        self._redraw_overlay()
        return True

    def on_release(self, event) -> bool:
        if self._placing is not None:
            return True
        gesture = self._gesture
        if gesture is None:
            return False
        self._gesture = None
        tool = gesture["tool"]
        try:
            if tool == "ink":
                self._finish_ink(gesture)
            elif tool in MARKUP_TOOLS:
                self._finish_markup(tool, gesture, event)
            elif tool in SHAPE_TOOLS:
                self._finish_shape(tool, gesture, event)
            elif tool == "redact":
                self._finish_redact(gesture, event)
            elif tool == "text":
                self._open_text_editor(gesture, event)
            elif tool == "move":
                self._finish_move(gesture, event)
            elif tool == "resize":
                self._finish_resize(gesture, event)
        except Exception as exc:
            messagebox.showerror("Edit PDF", str(exc), parent=self.window)
        self._redraw_overlay()
        return True

    def on_motion(self, event) -> bool:
        if not self.active or self.tool_var.get() != "select":
            return False
        return False   # hover affordances are handled by the cursor only

    def on_delete(self) -> bool:
        if not self.active or self.session is None:
            return False
        if self._selected_uid is not None:
            uid = self._selected_uid
            self._selected_uid = None
            self._close_props()
            self.session.delete_annot(self._page(), uid)
            self.window.toast("Annotation deleted")
            return True
        # In edit mode, a bare Delete must never recycle the file.
        return True

    def on_escape(self) -> bool:
        """One Escape step; True when consumed. Chain: gesture > editor >
        placement > selection > tool > mode."""
        if self._gesture is not None:
            self._cancel_gesture()
            return True
        if self._editor is not None:
            self.cancel_open_editor()
            return True
        if self._placing is not None:
            self._placing = None
            self._redraw_overlay()
            self.window.toast("Signature placement cancelled")
            return True
        if self._selected_uid is not None:
            self._selected_uid = None
            self._close_props()
            self._redraw_overlay()
            return True
        if self.active and self.tool_var.get() != "select":
            self.tool_var.set("select")
            return True
        if self.active:
            self.deactivate()
            return True
        return False

    def _cancel_gesture(self) -> None:
        self._gesture = None
        self._redraw_overlay()

    # ---------------------------------------------------------- selection
    def _select_press(self, event, point) -> bool:
        session = self.session
        if session is None:
            return False
        # Resize handle of the current selection has priority.
        if self._selected_uid is not None:
            handle = self._handle_at(event.x, event.y)
            if handle is not None:
                info = self._selected_info()
                if info is not None:
                    self._gesture = {"tool": "resize", "handle": handle,
                                     "uid": info.uid, "rect": info.rect,
                                     "start": (event.x, event.y),
                                     "now": (event.x, event.y)}
                    return True
        hit = session.hit_test(self._page(), point, tolerance=HIT_SLOP_PT)
        if hit is not None:
            self._selected_uid = hit.uid
            if hit.kind in PE.MARKUP_KINDS:
                # Text markups are anchored to their words: select them
                # (properties, delete) but never start a move gesture.
                self._redraw_overlay()
                return True
            self._gesture = {"tool": "move", "uid": hit.uid, "rect": hit.rect,
                             "start": (event.x, event.y),
                             "now": (event.x, event.y)}
            self._redraw_overlay()
            return True
        if self._selected_uid is not None:
            self._selected_uid = None
            self._close_props()
            self._redraw_overlay()
        if self._form_click(event, require_active=True):
            return True
        return False   # fall through to panning

    def _selected_info(self) -> Optional[PE.AnnotInfo]:
        if self.session is None or self._selected_uid is None:
            return None
        for info in self.session.annots_on(self._page()):
            if info.uid == self._selected_uid:
                return info
        return None

    def _handle_at(self, x: float, y: float) -> Optional[str]:
        info = self._selected_info()
        if info is None or info.kind in PE.MARKUP_KINDS:
            return None
        x0, y0, x1, y1 = self.points_rect_to_canvas(info.rect)
        handles = {"nw": (x0, y0), "ne": (x1, y0), "sw": (x0, y1),
                   "se": (x1, y1)}
        for name, (hx, hy) in handles.items():
            if abs(x - hx) <= HANDLE_PX + 2 and abs(y - hy) <= HANDLE_PX + 2:
                return name
        return None

    def _finish_move(self, gesture, event) -> None:
        session = self.session
        if session is None:
            return
        dx = event.x - gesture["start"][0]
        dy = event.y - gesture["start"][1]
        if abs(dx) < MIN_DRAG_PX and abs(dy) < MIN_DRAG_PX:
            if getattr(event, "_double", False):
                pass
            self._open_props_for_selection()
            return
        rect = gesture["rect"]
        c0 = self.points_rect_to_canvas(rect)
        moved = (c0[0] + dx, c0[1] + dy, c0[2] + dx, c0[3] + dy)
        new_rect = self._canvas_rect_to_points(*moved)
        session.move_annot(self._page(), gesture["uid"], new_rect)

    def _finish_resize(self, gesture, event) -> None:
        session = self.session
        if session is None:
            return
        rect = gesture["rect"]
        x0, y0, x1, y1 = self.points_rect_to_canvas(rect)
        handle = gesture["handle"]
        if "n" in handle:
            y0 = event.y
        if "s" in handle:
            y1 = event.y
        if "w" in handle:
            x0 = event.x
        if "e" in handle:
            x1 = event.x
        if abs(x1 - x0) < 6 or abs(y1 - y0) < 6:
            return
        session.move_annot(self._page(), gesture["uid"],
                           self._canvas_rect_to_points(x0, y0, x1, y1))

    def on_double_click(self, event) -> bool:
        """Double click: edit a free-text annot, or open the reflow editor."""
        if not self.active or self.session is None:
            return False
        point = self.canvas_to_points(event.x, event.y)
        hit = self.session.hit_test(self._page(), point, tolerance=HIT_SLOP_PT)
        if hit is not None and hit.kind in (PE.TYPE_FREETEXT, PE.TYPE_NOTE,
                                            *MARKUP_TOOLS):
            text = DLG.ask_text(self.window, self.palette, "Edit text",
                                "Text:", hit.contents, multiline=True)
            if text is not None and text != hit.contents:
                self.session.set_annot_props(self._page(), hit.uid,
                                             contents=text)
            return True
        if hit is None:
            return self._open_reflow_editor(event, point)
        return True

    # ------------------------------------------------------------ markups
    def _finish_markup(self, tool, gesture, event) -> None:
        session = self.session
        if session is None:
            return
        rect_pts = self._canvas_rect_to_points(*gesture["start"], event.x, event.y)
        words = self._words_in(rect_pts)
        if not words:
            self.window.toast("No text there")
            return
        defaults = self._defaults()
        color = _hex_to_rgb(defaults.get("highlight_color", "#ffd54a")
                            if tool == "highlight"
                            else defaults.get("ink_color", "#d84343"))
        quads = [D.fitz.Rect(*w) for w in words]
        session.add_markup(self._page(), quads, tool, color=color,
                           opacity=float(defaults.get("markup_opacity", 0.35)))

    def _words_in(self, rect_pts) -> list[tuple[float, float, float, float]]:
        """Word boxes intersecting a selection rect, in page points."""
        document = self._document()
        if document is None:
            return []
        x0, y0, x1, y1 = rect_pts
        out = []
        with document._lock:
            page = document._page(self._page())
            words = page.get_text("words")
        for wx0, wy0, wx1, wy1, *_rest in words:
            if wx1 < x0 or wx0 > x1 or wy1 < y0 or wy0 > y1:
                continue
            out.append((wx0, wy0, wx1, wy1))
        return out

    # ------------------------------------------------------------- shapes
    def _finish_shape(self, tool, gesture, event) -> None:
        session = self.session
        if session is None:
            return
        sx, sy = gesture["start"]
        if abs(event.x - sx) < MIN_DRAG_PX and abs(event.y - sy) < MIN_DRAG_PX:
            return
        defaults = self._defaults()
        stroke = _hex_to_rgb(defaults.get("shape_stroke", "#d84343"))
        fill_hex = defaults.get("shape_fill", "")
        fill = _hex_to_rgb(fill_hex) if fill_hex else None
        width = float(defaults.get("shape_width", 1.4))
        if tool in ("rect", "ellipse"):
            rect = self._canvas_rect_to_points(sx, sy, event.x, event.y)
            session.add_shape(self._page(), tool, rect, stroke=stroke,
                              fill=fill, width=width)
        else:
            p1 = self.canvas_to_points(sx, sy)
            p2 = self.canvas_to_points(event.x, event.y)
            session.add_shape(self._page(), tool, (p1, p2), stroke=stroke,
                              width=width)

    # ---------------------------------------------------------------- ink
    def _finish_ink(self, gesture) -> None:
        points = gesture.get("points") or []
        if len(points) < 2:
            return
        stroke_pts = [self.canvas_to_points(x, y) for x, y in points]
        self._pending_ink.append(stroke_pts)
        if self._ink_job:
            try:
                self.window.after_cancel(self._ink_job)
            except Exception:
                pass
        self._ink_job = self.window.after(INK_MERGE_MS,
                                          lambda: self._flush_ink(force=True))
        self._redraw_overlay()

    def _flush_ink(self, *, force: bool = False) -> None:
        self._ink_job = None
        if not self._pending_ink or self.session is None:
            self._pending_ink = []
            return
        defaults = self._defaults()
        strokes, self._pending_ink = self._pending_ink, []
        self.session.add_ink(self._page(), strokes,
                             color=_hex_to_rgb(defaults.get("ink_color", "#d84343")),
                             width=float(defaults.get("ink_width", 1.6)))

    # --------------------------------------------------------------- note
    def _add_note_at(self, point) -> None:
        if self.session is None:
            return
        text = DLG.ask_text(self.window, self.palette, "Sticky note",
                            "Note text:", multiline=True)
        if not text:
            return
        defaults = self._defaults()
        self.session.add_note(self._page(), point, text,
                              icon=str(defaults.get("note_icon", "Comment")))

    # ----------------------------------------------------------- redaction
    def _finish_redact(self, gesture, event) -> None:
        session = self.session
        if session is None:
            return
        rect = self._canvas_rect_to_points(*gesture["start"], event.x, event.y)
        if rect[2] - rect[0] < 2 or rect[3] - rect[1] < 2:
            return
        session.mark_redaction(self._page(), rect)
        self._sync_redact_bar()

    def _pending_redactions(self) -> int:
        """Marks on EVERY page: that is what Apply burns out."""
        if self.session is None:
            return 0
        return self.session.pending_redactions()

    def apply_redactions(self) -> None:
        session = self.session
        if session is None:
            return
        count = self._pending_redactions()
        if count == 0:
            self.window.toast("Mark at least one area first (X, then drag)")
            return
        if not messagebox.askokcancel(
                "Apply redactions",
                f"Permanently remove the content under {count} marked "
                "area(s)?\n\nThis cannot be recovered from the saved file. "
                "Undo works only until you close the document.",
                parent=self.window):
            return
        session.apply_redactions()
        self._sync_redact_bar()
        self.window.toast(f"{count} area(s) redacted")

    def clear_redactions(self) -> None:
        session = self.session
        if session is None:
            return
        pending = [a for a in session.annots_on(self._page())
                   if a.kind == PE.TYPE_REDACT]
        if not pending:
            return
        session.begin_group(f"Clear {len(pending)} redaction marks")
        try:
            for info in pending:
                session.delete_annot(self._page(), info.uid)
        finally:
            session.end_group()
        self._sync_redact_bar()

    def _sync_redact_bar(self) -> None:
        if self._redact_bar is None:
            return
        count = self._pending_redactions()
        if count and self.tool_var.get() == "redact":
            self._redact_count.configure(text=f"{count} marked")
            self._redact_bar.pack(side="left", padx=(10, 0))
        else:
            self._redact_bar.pack_forget()

    # ----------------------------------------------------- overlay editors
    def _open_text_editor(self, gesture, event) -> None:
        """Free-text tool: click or drag opens a real Text widget overlay."""
        sx, sy = gesture["start"]
        dragged = (abs(event.x - sx) >= MIN_DRAG_PX
                   or abs(event.y - sy) >= MIN_DRAG_PX)
        defaults = self._defaults()
        size_pt = float(defaults.get("text_size", 11.0))
        if dragged:
            rect_pts = self._canvas_rect_to_points(sx, sy, event.x, event.y)
        else:
            x0, y0 = self.canvas_to_points(sx, sy)
            rect_pts = (x0, y0, x0 + 180.0, y0 + size_pt * 2.2)
        self._spawn_editor(rect_pts, "", kind="freetext")

    def _spawn_editor(self, rect_pts, initial: str, *, kind: str,
                      payload: Optional[dict] = None) -> None:
        self.commit_open_editor()
        window = self.window
        c0 = self.points_rect_to_canvas(rect_pts)
        width_px = max(90, int(c0[2] - c0[0]))
        height_px = max(26, int(c0[3] - c0[1]))
        p = self.palette
        text = tk.Text(window.canvas, wrap="word",
                       background=p.panel_alt, foreground=p.text,
                       insertbackground=p.text, relief="solid", borderwidth=1,
                       highlightthickness=1, highlightcolor=p.accent,
                       highlightbackground=p.accent, font="FVui",
                       padx=4, pady=2, undo=True)
        text.insert("1.0", initial)
        item = window.canvas.create_window(
            c0[0], c0[1], window=text, anchor="nw",
            width=width_px, height=height_px, tags="edit")
        self._editor = {"widget": text, "item": item, "kind": kind,
                        "rect_pts": rect_pts, "payload": payload or {},
                        "view": (window.scale, window.off_x, window.off_y,
                                 window.page)}
        text.focus_set()
        text.bind("<Control-Return>", lambda _e: (self.commit_open_editor(), "break")[1])
        text.bind("<Escape>", lambda _e: (self.cancel_open_editor(), "break")[1])
        hint = {"freetext": "Ctrl+Enter places the text, Esc cancels",
                "reflow": "Ctrl+Enter rewrites the paragraph, Esc cancels",
                "field": "Ctrl+Enter fills the field, Esc cancels"}[kind]
        window.toast(hint)

    def commit_open_editor(self) -> None:
        editor = self._editor
        if editor is None:
            return
        self._editor = None
        widget = editor["widget"]
        try:
            content = widget.get("1.0", "end-1c")
        except tk.TclError:
            content = ""
        self._destroy_editor_widget(editor)
        if self.session is None:
            return
        try:
            if editor["kind"] == "freetext":
                if content.strip():
                    defaults = self._defaults()
                    self.session.add_free_text(
                        self._page(), editor["rect_pts"], content,
                        fontname=str(defaults.get("text_font", "helv")),
                        fontsize=float(defaults.get("text_size", 11.0)),
                        color=_hex_to_rgb(defaults.get("text_color", "#1a1a1a")))
            elif editor["kind"] == "reflow":
                block = editor["payload"].get("block")
                original = editor["payload"].get("original", "")
                if block is not None and content != original:
                    self._commit_reflow(block, content)
            elif editor["kind"] == "field":
                field = editor["payload"].get("field")
                if field is not None:
                    self.session.set_field_value(
                        field.page, field.xref, content,
                        label=f"Fill {field.name}")
        except Exception as exc:
            messagebox.showerror("Edit PDF", str(exc), parent=self.window)

    def cancel_open_editor(self) -> None:
        editor = self._editor
        if editor is None:
            return
        self._editor = None
        self._destroy_editor_widget(editor)

    def _destroy_editor_widget(self, editor) -> None:
        try:
            self.window.canvas.delete(editor["item"])
        except Exception:
            pass
        try:
            editor["widget"].destroy()
        except Exception:
            pass

    # -------------------------------------------------------------- reflow
    def _open_reflow_editor(self, event, point) -> bool:
        from . import reflow
        document = self._document()
        if document is None or self.session is None:
            return False
        try:
            block = reflow.hit_block(document, self._page(), point)
        except Exception:
            return False
        if block is None:
            reason = reflow.refusal_reason()
            if reason:
                self.window.toast(reason)
            return False
        bbox = tuple(block.bbox) if hasattr(block, "bbox") else None
        if bbox is None:
            return False
        self._spawn_editor(bbox, block.text, kind="reflow",
                           payload={"block": block, "original": block.text})
        return True

    def _commit_reflow(self, block, new_text: str) -> None:
        from . import reflow
        report = reflow.commit_edit(self.session, self._page(), block, new_text)
        scale = getattr(report, "scale", 1.0)
        fallback = getattr(report, "font_fallback", None)
        if scale and scale < 0.999:
            self.window.toast(f"Text shrunk to {scale * 100:.0f}% to fit")
        elif fallback:
            self.window.toast(f"Edited using {fallback}")

    # ------------------------------------------------------- form filling
    def _form_fields(self) -> list:
        from . import forms
        document = self._document()
        if document is None or not D.HAVE_FITZ:
            return []
        version = getattr(document, "content_version", 0)
        if self._fields_cache is not None and self._fields_cache[0] == version:
            return self._fields_cache[1]
        try:
            fields = forms.fields(document) if forms.has_form(document) else []
        except Exception:
            fields = []
        self._fields_cache = (version, fields)
        return fields

    def _form_click(self, event, *, require_active: bool) -> bool:
        """Clicking a form widget edits it, in or out of edit mode."""
        if not self.available():
            return False
        fields = self._form_fields()
        if not fields:
            return False
        point = self.canvas_to_points(event.x, event.y)
        target = None
        for field in fields:
            if field.page != self._page():
                continue
            r = field.rect_pts
            if r[0] - 2 <= point[0] <= r[2] + 2 and r[1] - 2 <= point[1] <= r[3] + 2:
                target = field
                break
        if target is None:
            return False
        if not self._ensure_session():
            return True
        if target.ftype == "checkbox":
            current = bool(target.value)
            self.session.set_field_value(target.page, target.xref, not current,
                                         label=f"Toggle {target.name}")
            self._fields_cache = None
            return True
        if target.ftype == "radio":
            on_value = (target.options[0] if getattr(target, "options", None)
                        else True)
            self.session.set_field_value(target.page, target.xref, on_value,
                                         label=f"Select {target.name}")
            self._fields_cache = None
            return True
        if target.ftype in ("combo", "listbox") and getattr(target, "options", None):
            menu = tk.Menu(self.window)
            from .theme import style_menu
            style_menu(menu, self.palette)
            for option in target.options:
                value = option[0] if isinstance(option, (list, tuple)) else option
                menu.add_command(
                    label=str(value),
                    command=lambda v=value, f=target: (
                        self.session.set_field_value(f.page, f.xref, v,
                                                     label=f"Fill {f.name}"),
                        self._invalidate_fields()))
            menu.tk_popup(event.x_root, event.y_root)
            return True
        if target.ftype == "text":
            self._spawn_editor(target.rect_pts, str(target.value or ""),
                               kind="field", payload={"field": target})
            return True
        return True

    def _invalidate_fields(self) -> None:
        self._fields_cache = None

    def open_identities(self) -> None:
        DLG.IdentityPresetsDialog(self.window, self.palette,
                                  self._identities()).wait()
        self.app.broadcast_identities()

    def _identities(self):
        if self._identity_store is None:
            from . import forms
            from . import app as app_module
            self._identity_store = forms.IdentityStore(
                self.app.settings, app_module.save_settings)
        return self._identity_store

    def auto_fill(self) -> None:
        from . import forms
        fields = self._form_fields()
        if not fields:
            self.window.toast("This document has no form fields")
            return
        store = self._identities()
        presets = store.presets()
        if not presets:
            self.open_identities()
            presets = store.presets()
            if not presets:
                return
        preset = store.active() or presets[0]
        proposals = forms.match_identity(fields, preset, self._document())
        if not proposals:
            self.window.toast("No fields matched this identity preset")
            return
        chosen = DLG.AutoFillReviewDialog(self.window, self.palette,
                                          proposals).wait()
        if not chosen:
            return
        if not self._ensure_session():
            return
        forms.apply_proposals(self.session, chosen)
        self._fields_cache = None
        self.window.toast(f"Filled {len(chosen)} of {len(fields)} fields; "
                          "review before saving")

    def clear_fills(self) -> None:
        fields = [f for f in self._form_fields() if f.ftype in ("text", "checkbox")]
        if not fields or not self._ensure_session():
            return
        self.session.begin_group("Clear form")
        try:
            for field in fields:
                empty = False if field.ftype == "checkbox" else ""
                self.session.set_field_value(field.page, field.xref, empty,
                                             label=f"Clear {field.name}")
        finally:
            self.session.end_group()
        self._fields_cache = None
        self.window.toast(f"Cleared {len(fields)} fields")

    # ----------------------------------------------------------- signatures
    def _signatures(self):
        if self._sig_library is None:
            from . import app as app_module
            from . import signing
            store_dir = app_module.SETTINGS_DIR / "signatures"
            self._sig_library = signing.SignatureLibrary(
                self.app.settings, app_module.save_settings, store_dir)
        return self._sig_library

    def open_sign(self) -> None:
        if not self._ensure_session():
            return
        library = self._signatures()
        while True:
            result = DLG.SignaturePickerDialog(self.window, self.palette,
                                               library).wait()
            if result is None:
                return
            if result.get("changed"):
                self.app.broadcast_signatures()
                continue
            if "new" in result:
                created = self._capture_signature(result["new"])
                if created is None:
                    continue
                result = {"id": created}
            self._start_placement(result["id"])
            return

    def _capture_signature(self, kind: str) -> Optional[str]:
        library = self._signatures()
        try:
            if kind == "drawn":
                strokes = DLG.SignatureCaptureDialog(self.window,
                                                     self.palette).wait()
                if not strokes:
                    return None
                item = library.add_drawn(strokes, color=(0.09, 0.13, 0.23),
                                         width=3.0)
            elif kind == "typed":
                text = DLG.ask_text(self.window, self.palette,
                                    "Typed signature", "Name to sign as:")
                if not text:
                    return None
                item = library.add_typed(text)
            else:
                chosen = filedialog.askopenfilename(
                    parent=self.window, title="Signature image",
                    filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp")])
                if not chosen:
                    return None
                item = library.add_image(Path(chosen))
        except Exception as exc:
            messagebox.showerror("Signature", str(exc), parent=self.window)
            return None
        self.app.broadcast_signatures()
        return item["id"] if isinstance(item, dict) else item

    def _start_placement(self, signature_id: str) -> None:
        library = self._signatures()
        try:
            png = library.png_bytes(signature_id)
        except Exception as exc:
            messagebox.showerror("Signature", str(exc), parent=self.window)
            return
        import io
        image = Image.open(io.BytesIO(png))
        image.load()
        self._placing = {"png": png, "image": image, "rect_pts": None}
        self.app.settings["active_signature"] = signature_id
        self.window.toast("Click where the signature should go; drag to "
                          "move, corner to resize, Enter places it")

    def _sign_press(self, event) -> bool:
        placing = self._placing
        assert placing is not None
        # The box is built in DISPLAY (canvas) space so a signature is
        # always horizontal on screen, whatever the page's /Rotate or the
        # viewer's rotation; _canvas_rect_to_points derotates it into the
        # unrotated annotation frame that stamping expects.
        px_per_pt = self.window.scale * self._factor()
        if placing["rect_pts"] is None:
            image = placing["image"]
            width_px = SIGN_DEFAULT_WIDTH_PT * px_per_pt
            height_px = width_px * image.height / max(image.width, 1)
            placing["rect_pts"] = self._canvas_rect_to_points(
                event.x - width_px / 2, event.y - height_px / 2,
                event.x + width_px / 2, event.y + height_px / 2)
            self._redraw_overlay()
            return True
        c0 = self.points_rect_to_canvas(placing["rect_pts"])
        near_corner = (abs(event.x - c0[2]) < 12 and abs(event.y - c0[3]) < 12)
        placing["drag"] = {"mode": "resize" if near_corner else "move",
                           "start": (event.x, event.y),
                           "canvas_rect": c0}
        return True

    def _sign_drag(self, event) -> bool:
        placing = self._placing
        if placing is None or placing.get("drag") is None:
            return True
        drag = placing["drag"]
        x0, y0, x1, y1 = drag["canvas_rect"]
        dx = event.x - drag["start"][0]
        dy = event.y - drag["start"][1]
        if drag["mode"] == "move":
            moved = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
        else:
            image = placing["image"]
            min_px = 30.0 * self.window.scale * self._factor()
            new_width = max(min_px, (x1 - x0) + dx)
            new_height = new_width * image.height / max(image.width, 1)
            moved = (x0, y0, x0 + new_width, y0 + new_height)
        placing["rect_pts"] = self._canvas_rect_to_points(*moved)
        self._redraw_overlay()
        return True

    def commit_placement(self) -> bool:
        placing = self._placing
        if placing is None or placing["rect_pts"] is None:
            return False
        self._placing = None
        from . import signing
        try:
            signing.stamp_signature(self.session, self._page(),
                                    placing["rect_pts"], placing["png"])
        except Exception as exc:
            messagebox.showerror("Signature", str(exc), parent=self.window)
            return True
        self.window.toast("Signature placed; Ctrl+S saves the document")
        self._redraw_overlay()
        return True

    def sign_with_certificate(self) -> None:
        from . import signing
        if self.window.current is None:
            return
        if not signing.have_pyhanko():
            messagebox.showinfo(
                "Certificate signing",
                "Cryptographic signing needs pyHanko:\n\n"
                "    pip install pyhanko", parent=self.window)
            return
        if self.dirty:
            if not messagebox.askokcancel(
                    "Unsaved edits", "The document must be saved before it "
                    "can be certified. Save now?", parent=self.window):
                return
            if not self.save(synchronous=True):
                return
        path = self.window.current.path
        default_out = path.with_name(path.stem + "-signed.pdf")
        while True:
            result = DLG.CertSignDialog(self.window, self.palette,
                                        self.app.settings,
                                        default_out=default_out,
                                        can_generate=True).wait()
            if result is None:
                return
            if result.get("generate"):
                self._generate_certificate()
                continue
            break
        passphrase = DLG.ask_passphrase(
            self.window, self.palette, "Certificate passphrase",
            f"Passphrase for {result['pfx'].name}:")
        if passphrase is None:
            return
        self.app.settings["sign_certificate_path"] = str(result["pfx"])
        from . import app as app_module
        app_module.save_settings(self.app.settings)

        def job(progress, _cancel):
            progress(0, 1, "Signing…")
            signing.crypto_sign(path, result["out"], result["pfx"], passphrase,
                                reason=result.get("reason", ""))
            progress(1, 1, "Done")
            return result["out"]

        def done(out_path):
            self.window.toast(f"Signed copy written: {Path(out_path).name}")
            self.window.add_file(Path(out_path))

        DLG.run_with_progress(self.window, self.palette, "Certificate signing",
                              job, done, cancellable=False)

    def _generate_certificate(self) -> None:
        from . import app as app_module
        from . import signing
        name = DLG.ask_text(self.window, self.palette, "New certificate",
                            "Name on the certificate:", "")
        if not name:
            return
        passphrase = DLG.ask_passphrase(
            self.window, self.palette, "Protect the certificate",
            "Choose a passphrase for the new certificate:", confirm=True)
        if passphrase is None:
            return
        try:
            pfx = signing.generate_self_signed(
                app_module.SETTINGS_DIR / "certificates", name, passphrase)
        except Exception as exc:
            messagebox.showerror("Certificate", str(exc), parent=self.window)
            return
        self.app.settings["sign_certificate_path"] = str(pfx)
        app_module.save_settings(self.app.settings)
        self.window.toast(f"Certificate created: {Path(pfx).name}")

    # ------------------------------------------------------------- export
    def open_export(self) -> None:
        from . import ocr as ocr_module
        from . import pdfexport
        if self.window.current is None or self._document() is None:
            return
        document = self._document()
        wants_ocr = False
        if ocr_module.ocr_available():
            try:
                wants_ocr = any(ocr_module.needs_ocr(document, i)
                                for i in range(document.page_count))
            except Exception:
                wants_ocr = False
        dialog = DLG.ExportDialog(
            self.window, self.palette, self.app.settings, pdfexport.FORMATS,
            source_name=self.window.current.name,
            page_count=document.page_count, wants_ocr=wants_ocr)
        result = dialog.wait()
        if not result:
            return
        prefs = self.app.settings.setdefault("export_prefs", {})
        prefs["last_format"] = result["format"]
        prefs["range"] = result.get("range_text", "all")
        prefs["ocr_missing_text"] = bool(result["ocr"])
        from . import app as app_module
        app_module.save_settings(self.app.settings)
        self._run_export(result)

    def _export_data(self) -> bytes:
        """A consistent byte snapshot: live session bytes, else the file."""
        if self.session is not None:
            return self.session.document.to_bytes()
        return self.window.current.path.read_bytes()

    def _run_export(self, config: dict) -> None:
        from . import pdfexport
        data = self._export_data()
        name = config["format"]
        exporter = pdfexport.EXPORTERS.get(name)
        if exporter is None:
            return
        entry = self.window.current

        def job(progress, cancel):
            payload = data
            if config.get("ocr"):
                payload = self._ocr_bytes_for_export(payload, progress, cancel)

            def forward(done_count, total):
                progress(done_count, total, f"{done_count}/{total} pages")

            return exporter(payload, config["path"], pages=config["pages"],
                            progress=forward, cancel=cancel)

        def done(report):
            detail = getattr(report, "detail", "") or ""
            self.window.toast(f"Exported to {config['path'].name}"
                              + (f" ({detail})" if detail else ""))

        def failed(exc):
            if isinstance(exc, pdfexport.ExportCancelled):
                self.window.toast("Export cancelled")
            else:
                messagebox.showerror("Export", str(exc), parent=self.window)

        DLG.run_with_progress(self.window, self.palette,
                              f"Exporting {entry.name}", job, done,
                              failed=failed)

    def _ocr_bytes_for_export(self, data: bytes, progress, cancel) -> bytes:
        """Bake an OCR text layer into a throwaway copy for text exporters."""
        from . import ocr as ocr_module
        if not ocr_module.ocr_available():
            return data
        settings = self.app.settings
        import tempfile
        with tempfile.TemporaryDirectory(prefix="figview-export-") as tmp:
            source = Path(tmp) / "source.pdf"
            source.write_bytes(data)
            stat = source.stat()
            entry = D.Entry(source, stat.st_size, stat.st_mtime, D.KIND_DOC)
            scratch = PE.EditSession(entry)
            try:
                ocr_module.make_searchable(
                    scratch, None,
                    lang=settings.get("ocr_language", "eng"),
                    variant=settings.get("ocr_variant", "fast"),
                    preprocess_on=bool(settings.get("ocr_preprocess", True)),
                    tessdata_dir=ocr_module.default_tessdata_root(),
                    progress_cb=lambda done_count, total:
                        progress(done_count, total, "OCR"),
                    cancel=cancel)
                return scratch.document.to_bytes()
            finally:
                scratch.close()

    # ---------------------------------------------------------------- OCR
    def _packs(self):
        if self._pack_manager is None:
            from . import app as app_module
            from . import ocr as ocr_module
            self._pack_manager = ocr_module.PackManager(
                app_module.SETTINGS_DIR / "tessdata")
        return self._pack_manager

    def open_ocr(self, then=None) -> None:
        """Tools > Recognise Text for PDFs; ``then`` runs after a successful
        overlay (used by the one-click OCR-and-copy command)."""
        from . import ocr as ocr_module
        document = self._document()
        if document is None:
            return
        if not ocr_module.ocr_available():
            messagebox.showinfo(
                "OCR", "The integrated OCR engine is unavailable in this "
                "PyMuPDF build.", parent=self.window)
            return
        try:
            scan_pages = sum(1 for i in range(document.page_count)
                             if ocr_module.needs_ocr(document, i))
        except Exception:
            scan_pages = 0
        config = DLG.OcrDialog(self.window, self.palette, self.app.settings,
                               self._packs(), page_count=document.page_count,
                               scan_pages=scan_pages).wait()
        if not config:
            return
        settings = self.app.settings
        settings["ocr_language"] = config["lang"]
        settings["ocr_variant"] = config["variant"]
        settings["ocr_preprocess"] = config["preprocess"]
        from . import app as app_module
        app_module.save_settings(settings)
        if not self._ensure_session():
            return
        pages = [self._page()] if config["scope"] == "page" else None
        self._run_ocr(config, pages, then)

    def _run_ocr(self, config: dict, pages, then=None) -> None:
        from . import ocr as ocr_module
        manager = self._packs()
        session = self.session

        def job(progress, cancel):
            if not manager.has(config["lang"], config["variant"]):
                progress(0, 1, f"Downloading {config['lang']} language pack…")
                manager.download(
                    config["lang"], config["variant"],
                    progress_cb=lambda done_b, total_b:
                        progress(done_b, total_b or max(done_b, 1),
                                 "Downloading language pack"),
                    cancel=cancel)
            return ocr_module.make_searchable(
                session, pages, lang=config["lang"], variant=config["variant"],
                preprocess_on=config["preprocess"],
                tessdata_dir=ocr_module.default_tessdata_root(),
                progress_cb=lambda done_count, total:
                    progress(done_count, total, f"Recognising page "
                                                f"{done_count}/{total}"),
                cancel=cancel)

        def done(report):
            if getattr(report, "cancelled", False):
                self.window.toast("OCR cancelled; the document is unchanged")
                return
            words = getattr(report, "words_added", None)
            pages_done = getattr(report, "pages_done", None)
            if pages_done:
                self.window.toast(
                    f"OCR added {words or 'a'} searchable words on "
                    f"{pages_done} page(s); Ctrl+S saves them into the file")
            else:
                self.window.toast("Nothing to OCR: pages already carry text")
            self._ocr_words_cache.clear()
            if then is not None:
                then()

        def failed(exc):
            messagebox.showerror("OCR", str(exc), parent=self.window)

        DLG.run_with_progress(self.window, self.palette, "Recognising text",
                              job, done, failed=failed)

    # ------------------------------------------------------ find / replace
    def open_find(self) -> None:
        from . import reflow
        if self._document() is None:
            return
        if self._find_dialog is not None:
            try:
                self._find_dialog.lift()
                return
            except Exception:
                self._find_dialog = None
        can_replace = not reflow.is_ocr_only(self._document(), self._page())

        dialog = DLG.FindReplaceDialog(
            self.window, self.palette,
            on_find=self._find_run, on_step=self._find_step,
            on_replace_all=self._replace_all, can_replace=can_replace)
        self._find_dialog = dialog
        dialog.bind("<Destroy>", lambda _e: self._find_closed(), add="+")

    def _find_closed(self) -> None:
        self._find_dialog = None
        self._find_state = None
        self._redraw_overlay()

    def _find_run(self, pattern: str, options: dict) -> int:
        from . import reflow
        document = self._document()
        matches = reflow.find(document, pattern, regex=options["regex"],
                              case=options["case"],
                              whole_word=options["whole_word"])
        self._find_state = {"matches": matches, "index": -1,
                            "pattern": pattern, "options": options}
        self._redraw_overlay()
        return len(matches)

    def _find_step(self, direction: int) -> None:
        state = self._find_state
        if not state or not state["matches"]:
            return
        state["index"] = (state["index"] + direction) % len(state["matches"])
        match = state["matches"][state["index"]]
        page = getattr(match, "page", 0)
        if page != self._page():
            self.window._set_document_page(page)
        self._redraw_overlay()

    def _replace_all(self, pattern: str, replacement: str, options: dict) -> None:
        from . import reflow
        state = self._find_state
        count = len(state["matches"]) if state else 0
        if count == 0:
            return
        if not self._ensure_session():
            return
        if not messagebox.askokcancel(
                "Replace all",
                f"Replace {count} match(es)? One Ctrl+Z undoes all of them.",
                parent=self._find_dialog or self.window):
            return
        session = self.session

        def job(progress, cancel):
            return reflow.replace_all(
                session, pattern, replacement, regex=options["regex"],
                case=options["case"], whole_word=options["whole_word"],
                progress=lambda done_count, total:
                    progress(done_count, total, "Replacing"),
                cancel=cancel)

        def done(report):
            occurrences = getattr(report, "occurrences", count)
            blocks = getattr(report, "blocks_changed", "?")
            self.window.toast(f"Replaced {occurrences} occurrence(s) in "
                              f"{blocks} paragraph(s); one undo reverts all")
            if self._find_dialog is not None:
                try:
                    self._find_dialog._changed()
                except Exception:
                    pass

        DLG.run_with_progress(self.window, self.palette, "Replace all",
                              job, done)

    # ------------------------------------------------------------ page ops
    def _page_op(self, action) -> None:
        if not self._ensure_session():
            return
        try:
            action(self.session)
        except Exception as exc:
            messagebox.showerror("Page operation", str(exc), parent=self.window)
            return
        window = self.window
        # Route through the page setter so the readouts, text layer, index
        # and overlay all follow the (possibly clamped) page index.
        window._set_document_page(
            max(0, min(window.page, self.session.document.page_count - 1)))

    def rotate_page(self, page: int, degrees: int) -> None:
        self._page_op(lambda s: s.rotate_page(page, degrees))

    def duplicate_page(self, page: int) -> None:
        self._page_op(lambda s: s.duplicate_page(page))

    def delete_pages(self, pages: list[int]) -> None:
        self._page_op(lambda s: s.delete_pages(pages))

    def insert_blank_after(self, page: int) -> None:
        if self.session is None and not self._ensure_session():
            return
        with self.session.document._lock:
            width, height = self.session.document.page_size_points(page)
        self._page_op(lambda s: s.insert_blank_page(page + 1, width=width,
                                                    height=height))

    def insert_pdf_after(self, page: int) -> None:
        chosen = filedialog.askopenfilename(
            parent=self.window, title="Insert PDF",
            filetypes=[("PDF", "*.pdf")])
        if chosen:
            self._page_op(lambda s: s.insert_pdf_file(Path(chosen), page + 1))

    def merge_append_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            parent=self.window, title="Merge PDFs at the end",
            filetypes=[("PDF", "*.pdf")])
        if chosen:
            self._page_op(lambda s: s.merge_append([Path(c) for c in chosen]))

    def extract_pages(self, pages: list[int]) -> None:
        if self.session is None and not self._ensure_session():
            return
        stem = self.window.current.path.stem if self.window.current else "pages"
        label = (f"p{pages[0] + 1}" if len(pages) == 1
                 else f"p{pages[0] + 1}-{pages[-1] + 1}")
        target = filedialog.asksaveasfilename(
            parent=self.window, title="Extract pages", defaultextension=".pdf",
            initialfile=f"{stem}_{label}.pdf", filetypes=[("PDF", "*.pdf")])
        if not target:
            return
        try:
            self.session.extract_pages(pages, Path(target))
        except Exception as exc:
            messagebox.showerror("Extract pages", str(exc), parent=self.window)
            return
        self.window.toast(f"Extracted {len(pages)} page(s)")

    # -------------------------------------------------------------- undo UI
    def undo(self) -> None:
        if self.session is not None and self.session.can_undo:
            self.commit_open_editor()
            label = self.session.undo()
            self.window.toast(label or "Undone")
            self._sync_redact_bar()

    def redo(self) -> None:
        if self.session is not None and self.session.can_redo:
            label = self.session.redo()
            self.window.toast(label or "Redone")
            self._sync_redact_bar()

    def _refresh_history_buttons(self) -> None:
        session = self.session
        for button, enabled in ((self._undo_button,
                                 session is not None and session.can_undo),
                                (self._redo_button,
                                 session is not None and session.can_redo)):
            if button is not None:
                button.state(["!disabled"] if enabled else ["disabled"])
        if self._save_button is not None:
            self._save_button.state(["!disabled"] if self.dirty else ["disabled"])
        if hasattr(self.window, "_edit_menu_indices"):
            menu, undo_index, redo_index = self.window._edit_menu_indices
            undo_label = ("Undo " + session.undo_label if session is not None
                          and session.can_undo else "Undo")
            redo_label = ("Redo " + session.redo_label if session is not None
                          and session.can_redo else "Redo")
            try:
                menu.entryconfigure(undo_index, label=undo_label,
                                    state="normal" if session is not None
                                    and session.can_undo else "disabled")
                menu.entryconfigure(redo_index, label=redo_label,
                                    state="normal" if session is not None
                                    and session.can_redo else "disabled")
            except Exception:
                pass

    # ------------------------------------------------------------ overlay
    def _redraw_overlay(self) -> None:
        try:
            self.window.canvas.delete("edit")
        except tk.TclError:
            return
        self.draw_overlay()

    def draw_overlay(self) -> None:
        """Draw controller-owned canvas items; called after every _paint."""
        window = self.window
        canvas = window.canvas
        canvas.delete("edit")
        if window.document is None or self.window._video_session is not None:
            return
        p = self.palette

        # Open overlay editor follows the view; a changed view commits it.
        if self._editor is not None:
            scale, off_x, off_y, page = self._editor["view"]
            if (page != window.page or abs(scale - window.scale) > 1e-9
                    or abs(off_x - window.off_x) > 0.5
                    or abs(off_y - window.off_y) > 0.5):
                self.commit_open_editor()

        # Pending redaction marks stay visible whenever a session exists.
        if self.session is not None:
            for info in self.session.annots_on(window.page):
                if info.kind != PE.TYPE_REDACT:
                    continue
                x0, y0, x1, y1 = self.points_rect_to_canvas(info.rect)
                canvas.create_rectangle(x0, y0, x1, y1, outline=p.danger,
                                        width=2, stipple="gray25",
                                        fill=p.danger, tags="edit")

        # Find matches.
        state = self._find_state
        if state:
            for index, match in enumerate(state["matches"]):
                if getattr(match, "page", 0) != window.page:
                    continue
                current = index == state["index"]
                for rect in getattr(match, "rects_pts", []) or []:
                    x0, y0, x1, y1 = self.points_rect_to_canvas(rect)
                    canvas.create_rectangle(
                        x0 - 1, y0 - 1, x1 + 1, y1 + 1,
                        outline=p.accent, width=3 if current else 1,
                        tags="edit")

        # Selection handles.
        info = self._selected_info()
        if info is not None:
            x0, y0, x1, y1 = self.points_rect_to_canvas(info.rect)
            offset = None
            if self._gesture and self._gesture.get("tool") == "move":
                dx = self._gesture["now"][0] - self._gesture["start"][0]
                dy = self._gesture["now"][1] - self._gesture["start"][1]
                offset = (dx, dy)
                x0, y0, x1, y1 = x0 + dx, y0 + dy, x1 + dx, y1 + dy
            canvas.create_rectangle(x0, y0, x1, y1, outline=p.accent,
                                    width=1, dash=(3, 2), tags="edit")
            for hx, hy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                canvas.create_rectangle(hx - HANDLE_PX, hy - HANDLE_PX,
                                        hx + HANDLE_PX, hy + HANDLE_PX,
                                        fill=p.accent, outline="", tags="edit")
            _ = offset

        # In-progress gesture.
        gesture = self._gesture
        if gesture is not None:
            tool = gesture["tool"]
            if tool == "ink":
                points = gesture.get("points") or []
                if len(points) > 1:
                    flat = [value for point in points for value in point]
                    canvas.create_line(*flat, fill=self._defaults().get(
                        "ink_color", "#d84343"), width=2.4, smooth=True,
                        capstyle="round", tags="edit")
            elif tool in ("move",):
                pass
            elif "start" in gesture and "now" in gesture:
                x0, y0 = gesture["start"]
                x1, y1 = gesture["now"]
                left, right = sorted((x0, x1))
                top, bottom = sorted((y0, y1))
                if tool in MARKUP_TOOLS:
                    rect_pts = self._canvas_rect_to_points(left, top, right,
                                                           bottom)
                    for word in self._words_in(rect_pts):
                        wx0, wy0, wx1, wy1 = self.points_rect_to_canvas(word)
                        canvas.create_rectangle(wx0, wy0, wx1, wy1,
                                                fill=p.accent, outline="",
                                                stipple="gray50", tags="edit")
                elif tool == "redact":
                    canvas.create_rectangle(left, top, right, bottom,
                                            outline=p.danger, width=2,
                                            stipple="gray25", fill=p.danger,
                                            tags="edit")
                elif tool == "ellipse":
                    canvas.create_oval(left, top, right, bottom,
                                       outline=p.accent, width=2, tags="edit")
                elif tool in ("line", "arrow"):
                    canvas.create_line(x0, y0, x1, y1, fill=p.accent, width=2,
                                       arrow="last" if tool == "arrow" else None,
                                       tags="edit")
                else:
                    canvas.create_rectangle(left, top, right, bottom,
                                            outline=p.accent, width=2,
                                            dash=(4, 2), tags="edit")

        # Pending ink strokes not yet flushed into an annotation.
        if self._pending_ink and self.session is not None:
            for stroke in self._pending_ink:
                canvas_points = []
                for px, py in stroke:
                    c = self.points_rect_to_canvas((px, py, px, py))
                    canvas_points.extend((c[0], c[1]))
                if len(canvas_points) >= 4:
                    canvas.create_line(*canvas_points,
                                       fill=self._defaults().get("ink_color",
                                                                 "#d84343"),
                                       width=2.4, smooth=True,
                                       capstyle="round", tags="edit")

        # Signature placement preview.
        placing = self._placing
        if placing is not None and placing["rect_pts"] is not None:
            x0, y0, x1, y1 = self.points_rect_to_canvas(placing["rect_pts"])
            width = max(1, int(x1 - x0))
            height = max(1, int(y1 - y0))
            try:
                preview = placing["image"].resize((width, height),
                                                  Image.Resampling.LANCZOS)
                self._sign_photo = ImageTk.PhotoImage(preview, master=window)
                canvas.create_image(x0, y0, image=self._sign_photo,
                                    anchor="nw", tags="edit")
            except Exception:
                pass
            canvas.create_rectangle(x0, y0, x1, y1, outline=p.accent,
                                    dash=(4, 2), tags="edit")
            canvas.create_rectangle(x1 - 6, y1 - 6, x1 + 6, y1 + 6,
                                    fill=p.accent, outline="", tags="edit")

        # Form field outlines while edit mode is on.
        if self.active:
            for field in self._form_fields():
                if field.page != window.page:
                    continue
                x0, y0, x1, y1 = self.points_rect_to_canvas(field.rect_pts)
                canvas.create_rectangle(x0, y0, x1, y1, outline=p.accent,
                                        width=1, dash=(1, 3), tags="edit")

    # ------------------------------------------------------------- events
    def on_page_changed(self) -> None:
        self.commit_open_editor()
        self._selected_uid = None
        self._close_props()
        self._gesture = None
        self._sync_redact_bar()
        if self.thumb_strip is not None and self.thumb_strip.winfo_ismapped():
            self.thumb_strip.highlight(self._page())

    def wheel_scrolls(self) -> bool:
        """While editing, a plain wheel pans and never flips the page."""
        return self.active and bool(
            self.app.settings.get("edit_wheel_scrolls", True))

    # ---------------------------------------------------------- palette UI
    def build_toolbar_button(self, actionbar) -> None:
        """The Edit toggle beside Snip; called from _build_ui."""
        p = self.palette
        button = ttk.Checkbutton(
            actionbar, text="Edit", image=self.window.icons.get("pen"),
            compound="left", variable=self.edit_mode_var,
            style="Modal.TCheckbutton", takefocus=False,
            command=self.toggle_mode)
        button.pack(side="left", padx=(0, 8), after=self.window.snip_button)
        self.edit_button = button
        bind_tooltip(button, "Edit this PDF: annotate, fill, sign  (E)", p)
        self.review.build_toolbar(actionbar)

    def sync_toolbar(self) -> None:
        """Show the Edit toggle only when it can work."""
        self.review.sync()
        if not hasattr(self, "edit_button"):
            return
        if self.available():
            self.edit_button.state(["!disabled"])
        else:
            self.edit_button.state(["disabled"])
            if self.active:
                self.deactivate()

    def _build_palette(self) -> None:
        if self.palette_row is not None:
            return
        window = self.window
        p = self.palette
        row = ttk.Frame(window.stage, style="Transport.TFrame", padding=(10, 6))
        row.grid(row=2, column=0, sticky="ew")
        row.grid_remove()
        self.palette_row = row

        tools = ttk.Frame(row, style="ToolbarGroup.TFrame", padding=2)
        tools.pack(side="left")
        for name, icon, tip, key in TOOLS:
            button = ttk.Radiobutton(
                tools, image=window.icons.get(icon), value=name,
                variable=self.tool_var, style="Segment.TRadiobutton",
                takefocus=False, command=self._tool_changed)
            button.pack(side="left")
            bind_tooltip(button, f"{tip}  ({key})", p)

        actions = ttk.Frame(row, style="ToolbarGroup.TFrame", padding=2)
        actions.pack(side="left", padx=(8, 0))
        for icon, tip, command in (
                ("signature", "Place a signature", self.open_sign),
                ("pages", "Organise pages", self.toggle_thumb_strip),
                ("ocr", "Recognise text (OCR)", self.open_ocr),
                ("replace", "Find and replace  (Ctrl+H)", self.open_find),
                ("export", "Export to Word, Excel, PowerPoint…  (Ctrl+Shift+E)",
                 self.open_export)):
            toolbar_button(actions, "", command, p, tip, style="Icon.TButton",
                           image=window.icons.get(icon)).pack(side="left")

        history = ttk.Frame(row, style="ToolbarGroup.TFrame", padding=2)
        history.pack(side="left", padx=(8, 0))
        self._undo_button = toolbar_button(
            history, "", self.undo, p, "Undo  (Ctrl+Z)", style="Icon.TButton",
            image=window.icons.get("undo"))
        self._undo_button.pack(side="left")
        self._redo_button = toolbar_button(
            history, "", self.redo, p, "Redo  (Ctrl+Y)", style="Icon.TButton",
            image=window.icons.get("redo"))
        self._redo_button.pack(side="left")

        self._save_button = toolbar_button(
            row, "Save", self.save, p, "Save edits into the PDF  (Ctrl+S)",
            style="Accent.TButton", image=window.icons.get("save"),
            compound="left")
        self._save_button.pack(side="right")

        redact_bar = ttk.Frame(row, style="Transport.TFrame")
        self._redact_count = ttk.Label(redact_bar, text="",
                                       style="Transport.TLabel")
        self._redact_count.pack(side="left", padx=(0, 6))
        ttk.Button(redact_bar, text="Apply", style="Danger.TButton",
                   command=self.apply_redactions).pack(side="left",
                                                       padx=(0, 4))
        ttk.Button(redact_bar, text="Clear",
                   command=self.clear_redactions).pack(side="left")
        self._redact_bar = redact_bar
        self._refresh_history_buttons()

    def _tool_changed(self) -> None:
        self.commit_open_editor()
        self._selected_uid = None
        self._gesture = None
        self._close_props()
        self._sync_redact_bar()
        self._redraw_overlay()
        cursor = {"select": "", "ink": "pencil", "text": "xterm",
                  "note": "hand2"}.get(self.tool_var.get(), "crosshair")
        self.window.canvas.configure(cursor=cursor)

    def set_tool(self, name: str) -> None:
        if self.active:
            self.tool_var.set(name)
            self._tool_changed()

    # ------------------------------------------------------ property popover
    def _open_props_for_selection(self) -> None:
        info = self._selected_info()
        if info is None or info.kind == PE.TYPE_REDACT:
            return
        self._close_props()
        window = self.window
        p = self.palette
        popover = tk.Toplevel(window)
        popover.overrideredirect(True)
        popover.configure(background=p.border)
        frame = ttk.Frame(popover, style="Card.TFrame", padding=8)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        x0, y0, x1, _y1 = self.points_rect_to_canvas(info.rect)
        root_x = window.canvas.winfo_rootx() + int((x0 + x1) / 2) - 100
        root_y = window.canvas.winfo_rooty() + int(y0) - 56
        popover.geometry(f"+{max(0, root_x)}+{max(0, root_y)}")

        swatches = ("#ffd54a", "#8bd450", "#5aa7f0", "#d84343", "#b06ad4",
                    "#1a1a1a")
        recents = [c for c in self.app.settings.get("edit_recent_colors", [])
                   if isinstance(c, str)][:4]
        for value in list(dict.fromkeys(list(swatches) + recents)):
            chip = tk.Canvas(frame, width=18, height=18, background=value,
                             highlightthickness=1, highlightbackground=p.border,
                             cursor="hand2")
            chip.pack(side="left", padx=2)
            chip.bind("<Button-1>",
                      lambda _e, v=value: self._apply_color(info, v))
        custom = ttk.Button(frame, text="…", width=2,
                            command=lambda: self._apply_custom_color(info))
        custom.pack(side="left", padx=(4, 8))
        bind_tooltip(custom, "Custom colour", p)
        remove = ttk.Button(frame, text="Delete", style="Danger.TButton",
                            command=lambda: (self._close_props(),
                                             self.on_delete()))
        remove.pack(side="left")
        popover.bind("<Escape>", lambda _e: self._close_props())
        self._props_popover = popover

    def _apply_color(self, info: PE.AnnotInfo, value: str) -> None:
        if self.session is None:
            return
        rgb = _hex_to_rgb(value)
        if info.kind in (PE.TYPE_HIGHLIGHT, PE.TYPE_UNDERLINE,
                         PE.TYPE_STRIKEOUT, PE.TYPE_SQUIGGLY, PE.TYPE_INK,
                         PE.TYPE_RECT, PE.TYPE_ELLIPSE, PE.TYPE_LINE,
                         PE.TYPE_ARROW):
            self.session.set_annot_props(self._page(), info.uid, stroke=rgb)
        recents = [value] + [c for c in
                             self.app.settings.get("edit_recent_colors", [])
                             if c != value]
        self.app.settings["edit_recent_colors"] = recents[:8]
        defaults = self.app.settings.setdefault("annot_defaults", {})
        if info.kind == PE.TYPE_HIGHLIGHT:
            defaults["highlight_color"] = value
        elif info.kind == PE.TYPE_INK:
            defaults["ink_color"] = value
        elif info.kind in (PE.TYPE_RECT, PE.TYPE_ELLIPSE, PE.TYPE_LINE,
                           PE.TYPE_ARROW):
            defaults["shape_stroke"] = value
        self._close_props()

    def _apply_custom_color(self, info: PE.AnnotInfo) -> None:
        from tkinter import colorchooser
        chosen = colorchooser.askcolor(parent=self.window)[1]
        if chosen:
            self._apply_color(info, chosen)

    def _close_props(self) -> None:
        if self._props_popover is not None:
            try:
                self._props_popover.destroy()
            except Exception:
                pass
            self._props_popover = None

    # --------------------------------------------------------- thumb strip
    def toggle_thumb_strip(self) -> None:
        if not self._ensure_session():
            return
        if self.thumb_strip is None:
            self.thumb_strip = ThumbStrip(self.window.stage, self)
            self.thumb_strip.grid(row=4, column=0, sticky="ew")
        elif self.thumb_strip.winfo_ismapped():
            self.thumb_strip.grid_remove()
            return
        else:
            self.thumb_strip.grid()
        self.thumb_strip.refresh()

    # ------------------------------------------------------------ form bar
    def _build_form_bar(self) -> None:
        if self.form_bar is not None:
            return
        window = self.window
        p = self.palette
        bar = ttk.Frame(window.stage, style="Transport.TFrame", padding=(10, 5))
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_remove()
        self.form_bar = bar
        self._form_label = ttk.Label(bar, text="", style="Transport.TLabel")
        self._form_label.pack(side="left", padx=(0, 10))
        self._preset_var = tk.StringVar()
        self._preset_box = ttk.Combobox(bar, textvariable=self._preset_var,
                                        state="readonly", width=18)
        self._preset_box.pack(side="left", padx=(0, 6))
        self._preset_box.bind("<<ComboboxSelected>>", self._preset_selected)
        fill = ttk.Button(bar, text="Auto-fill", style="Accent.TButton",
                          command=self.auto_fill)
        fill.pack(side="left", padx=(0, 6))
        bind_tooltip(fill, "Fill matching fields from the selected identity; "
                           "you review every value first", p)
        ttk.Button(bar, text="Clear", command=self.clear_fills).pack(
            side="left", padx=(0, 6))
        ttk.Button(bar, text="Identities…", command=self.open_identities).pack(
            side="left")

    def _preset_selected(self, _event=None) -> None:
        store = self._identities()
        label = self._preset_var.get()
        for preset in store.presets():
            if preset.get("label") == label:
                store.set_active(preset.get("id"))
                return

    def _sync_form_bar(self) -> None:
        fields = self._form_fields()
        if not fields:
            if self.form_bar is not None:
                self.form_bar.grid_remove()
            return
        self._build_form_bar()
        count = len(fields)
        self._form_label.configure(
            text=f"{count} form field{'s' if count != 1 else ''}")
        store = self._identities()
        presets = store.presets()
        self._preset_box.configure(
            values=[p.get("label", "Preset") for p in presets])
        current = store.active()
        if current is not None:
            self._preset_var.set(current.get("label", ""))
        elif presets:
            self._preset_var.set(presets[0].get("label", ""))
        else:
            self._preset_var.set("")
        self.form_bar.grid()

    def refresh_signature_ui(self) -> None:
        self._sig_library = None

    def refresh_identity_ui(self) -> None:
        self._identity_store = None
        if self.form_bar is not None and self.form_bar.winfo_ismapped():
            self._sync_form_bar()

    def on_document_loaded(self) -> None:
        """Per-document UI refresh, called from load()'s done callback."""
        self.sync_toolbar()
        self._fields_cache = None
        self._ocr_words_cache.clear()
        self._find_state = None
        self.attach_if_parked()
        self.review.refresh()
        self._sync_form_bar()
        if self.thumb_strip is not None:
            if self.session is None:
                self.thumb_strip.grid_remove()
            else:
                self.thumb_strip.refresh()

    # -------------------------------------------------------- context menu
    def context_items(self, menu: tk.Menu, event) -> None:
        """Editing entries for the canvas context menu."""
        if not self.available():
            return
        menu.add_separator()
        if not self.active:
            menu.add_command(label="Edit PDF", command=self.activate)
        else:
            point = self.canvas_to_points(event.x, event.y)
            hit = (self.session.hit_test(self._page(), point)
                   if self.session is not None else None)
            if hit is not None:
                menu.add_command(
                    label="Annotation properties",
                    command=lambda: (self._set_selection(hit.uid),
                                     self._open_props_for_selection()))
                menu.add_command(
                    label="Delete annotation",
                    command=lambda: (self._set_selection(hit.uid),
                                     self.on_delete()))
            menu.add_command(label="Edit text here…",
                             command=lambda: self.on_double_click(event))
        page = self._page()
        pages_menu = DLG.page_ops_menu(self.window, self.palette, self, page)
        menu.add_cascade(label=f"Page {page + 1}", menu=pages_menu)
        menu.add_command(label="Export To…", command=self.open_export)

    def _set_selection(self, uid: str) -> None:
        self._selected_uid = uid
        self._redraw_overlay()


def reorder_plan(source: int, target: int, page_count: int
                 ) -> Optional[tuple[int, int]]:
    """Translate a thumb-strip drop into a ``Document.move_page`` call.

    ``target`` is the slot the page was dropped before (``page_count`` means
    "after the last page"). PyMuPDF's ``move_page(pno, to)`` inserts the page
    *before* ``to`` (``-1`` appends), so dropping one slot to the right needs
    ``to = target`` and not the final index, which used to make a one-slot
    rightward drag a silent no-op. Returns ``(insert_before, final_index)``
    or ``None`` when the drop changes nothing.
    """
    if target in (source, source + 1):
        return None
    insert_before = target if target < page_count else -1
    destination = target if target < source else target - 1
    return insert_before, destination


class ThumbStrip(ttk.Frame):
    """Horizontal page-thumbnail strip with click, context menu and reorder."""

    def __init__(self, master, controller: EditController) -> None:
        super().__init__(master, style="Transport.TFrame", padding=(10, 6))
        self.controller = controller
        self.window = controller.window
        p = controller.palette
        self.canvas = tk.Canvas(self, height=THUMB_H + 22,
                                background=p.panel, highlightthickness=0)
        self.canvas.pack(fill="x", expand=True)
        scroll = ttk.Scrollbar(self, orient="horizontal",
                               command=self.canvas.xview,
                               style="Horizontal.TScrollbar")
        scroll.pack(fill="x")
        self.canvas.configure(xscrollcommand=scroll.set)
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        self._slots: list[tuple[int, int]] = []      # (x0, x1) per page
        self._drag: Optional[dict] = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._context)
        self.canvas.bind("<MouseWheel>",
                         lambda e: self.canvas.xview_scroll(
                             -1 if e.delta > 0 else 1, "units"))

    def refresh(self) -> None:
        session = self.controller.session
        if session is None:
            return
        document = session.document
        version = document.content_version
        canvas = self.canvas
        canvas.delete("all")
        self._slots = []
        p = self.controller.palette
        x = 8
        pad = 8
        for page in range(document.page_count):
            try:
                width_px, height_px = document.page_size(page)
            except Exception:
                continue
            thumb_w = max(18, int(THUMB_H * width_px / max(height_px, 1)))
            key = (page, version, thumb_w)
            photo = self._photos.get(key)
            slot_x0 = x
            if photo is None:
                self._render_thumb(page, version, thumb_w, slot_x0)
            else:
                self._place_thumb(photo, page, slot_x0)
            self._slots.append((slot_x0, slot_x0 + thumb_w))
            x = slot_x0 + thumb_w + pad
        canvas.configure(scrollregion=(0, 0, x, THUMB_H + 22))
        # Drop stale photos so an edited page cannot show its old pixels.
        self._photos = {key: photo for key, photo in self._photos.items()
                        if key[1] == version}
        self.highlight(self.window.page)
        _ = p

    def _render_thumb(self, page: int, version: int, thumb_w: int,
                      slot_x0: int) -> None:
        session = self.controller.session
        if session is None:
            return
        document = session.document

        def job():
            width_px, height_px = document.page_size(page)
            return document.render_region(page, (0, 0, width_px, height_px),
                                          (thumb_w, THUMB_H), fast=True)

        def done(image) -> None:
            if (self.controller.session is not session
                    or document.content_version != version
                    or not self.winfo_exists()):
                return
            photo = ImageTk.PhotoImage(image, master=self)
            self._photos[(page, version, thumb_w)] = photo
            self._place_thumb(photo, page, slot_x0)
            self.highlight(self.window.page)

        self.window.app.worker.submit(job, done, lambda _exc: None)

    def _place_thumb(self, photo, page: int, x0: int) -> None:
        canvas = self.canvas
        canvas.delete(f"pg{page}")
        canvas.create_image(x0, 4, image=photo, anchor="nw",
                            tags=(f"pg{page}", "thumb"))
        canvas.create_text(x0 + photo.width() / 2, THUMB_H + 12,
                           text=str(page + 1), fill=self.controller.palette.muted,
                           font="FVsmall", tags=(f"pg{page}",))

    def highlight(self, page: int) -> None:
        canvas = self.canvas
        canvas.delete("hl")
        if 0 <= page < len(self._slots):
            x0, x1 = self._slots[page]
            canvas.create_rectangle(x0 - 3, 1, x1 + 3, THUMB_H + 7,
                                    outline=self.controller.palette.accent,
                                    width=2, tags="hl")

    def _page_at(self, x: float) -> Optional[int]:
        cx = self.canvas.canvasx(x)
        for page, (x0, x1) in enumerate(self._slots):
            if x0 - 4 <= cx <= x1 + 4:
                return page
        return None

    def _press(self, event) -> None:
        page = self._page_at(event.x)
        if page is None:
            return
        self._drag = {"page": page, "x": event.x, "moved": False}
        if page != self.window.page:
            self.window._set_document_page(page)

    def _motion(self, event) -> None:
        drag = self._drag
        if drag is None:
            return
        if abs(event.x - drag["x"]) > 12:
            drag["moved"] = True
        if not drag["moved"]:
            return
        canvas = self.canvas
        canvas.delete("caret")
        target = self._insert_index(event.x)
        if target is None:
            return
        x = (self._slots[target][0] - 4 if target < len(self._slots)
             else self._slots[-1][1] + 4)
        canvas.create_line(x, 2, x, THUMB_H + 8,
                           fill=self.controller.palette.accent, width=3,
                           tags="caret")

    def _insert_index(self, x: float) -> Optional[int]:
        if not self._slots:
            return None
        cx = self.canvas.canvasx(x)
        for page, (x0, x1) in enumerate(self._slots):
            if cx < (x0 + x1) / 2:
                return page
        return len(self._slots)

    def _release(self, event) -> None:
        drag = self._drag
        self._drag = None
        self.canvas.delete("caret")
        if drag is None or not drag["moved"]:
            return
        target = self._insert_index(event.x)
        source = drag["page"]
        session = self.controller.session
        if target is None or session is None:
            return
        plan = reorder_plan(source, target, session.document.page_count)
        if plan is None:
            return
        insert_before, destination = plan
        session.move_page(source, insert_before)
        self.window._set_document_page(
            max(0, min(destination, session.document.page_count - 1)))

    def _context(self, event) -> None:
        page = self._page_at(event.x)
        if page is None:
            return
        menu = DLG.page_ops_menu(self.window, self.controller.palette,
                                 self.controller, page)
        menu.tk_popup(event.x_root, event.y_root)
