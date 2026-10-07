"""Tk glue for in-place rich text editing on the page canvas.

:class:`InlineTextEditor` turns a zone of the page into a live text editor:

* the text is drawn by MuPDF itself (:class:`textedit.PreviewRenderer`) on
  a transparent image, so it looks exactly like the final PDF text, in the
  real font, over the real page background (no opaque editing bar);
* when existing text is edited, an "erased" copy of the page region
  (:class:`textedit.ErasedPage`) hides the original glyphs underneath;
* caret, selection, keyboard and mouse handling are implemented here on top
  of the headless :class:`textedit.RichText` model;
* a floating :class:`FormatBar` above the box controls font family, size,
  bold / italic / underline, colour and alignment.

Every canvas item carries the tag :data:`TAG` (never ``"edit"``), so the
edit controller's overlay redraws cannot delete the editor. Keystrokes are
captured with a dedicated bindtag placed first on the canvas, which stops
window shortcuts (arrows, Delete, Ctrl+Z...) from firing while typing.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import colorchooser, ttk
from typing import Callable, Optional, Sequence

from PIL import Image, ImageDraw, ImageTk

from . import textedit as TE
from .theme import bind_tooltip

# ---------------------------------------------------------------------------
# Tunables (canvas pixels unless stated otherwise)

TAG = "fvtext"                 # tag of every editor canvas item
BINDTAG = "FVInlineText"       # bindtag inserted first on the canvas
FRAME_PAD_PX = 4               # gap between the text and its dashed frame
GRIP_PX = 7                    # frame band that moves the box when dragged
HANDLE_PX = 4                  # half-size of the width handle squares
CARET_BLINK_MS = 530
BAR_GAP_PX = 8                 # space between the format bar and the frame
MIN_WRAP_PT = 18.0             # narrowest text box, in PDF points
SIZE_CHOICES = ("6", "7", "8", "9", "10", "10.5", "11", "12", "14", "16",
                "18", "20", "24", "28", "32", "36", "48", "60", "72")
SWATCHES = ("#000000", "#3f3f3f", "#7f7f7f", "#bfbfbf", "#ffffff", "#c00000",
            "#e36c09", "#f2c400", "#00a651", "#0070c0", "#002060", "#7030a0")
ALT_MASK = 0x20000 if sys.platform.startswith("win") else 0x0008
CTRL_MASK = 0x0004
SHIFT_MASK = 0x0001


def _mix(color_a: str, color_b: str, weight: float) -> str:
    """Blend two ``#rrggbb`` colours (weight 0 -> a, 1 -> b)."""
    ra, ga, ba = TE.hex_to_rgb(color_a)
    rb, gb, bb = TE.hex_to_rgb(color_b)
    return "#%02x%02x%02x" % tuple(
        int(round((x + (y - x) * weight) * 255)) for x, y in
        ((ra, rb), (ga, gb), (ba, bb)))


def _luminance(color: str) -> float:
    r, g, b = TE.hex_to_rgb(color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# ---------------------------------------------------------------------------
# Editor


class InlineTextEditor:
    """One live text box on the canvas.

    Parameters
    ----------
    controller:
        The :class:`figureviewer.editui.EditController` (geometry helpers,
        palette, window, document).
    mode:
        ``"new"`` for a freshly drawn text box, ``"block"`` for existing
        page text being edited in place.
    model:
        The :class:`textedit.RichText` being edited.
    anchor:
        ``(x, baseline)`` in displayed page points: the alignment anchor of
        the box (see :func:`textedit.anchored_origin`) and its first
        baseline.
    on_finish:
        ``callback(editor, commit: bool)`` run exactly once when the editor
        closes; the controller performs the actual PDF commit.
    """

    def __init__(self, controller, *, mode: str, model: TE.RichText, page: int,
                 anchor: tuple[float, float], registry: TE.FontRegistry,
                 on_finish: Callable[["InlineTextEditor", bool], None],
                 wrap_width: Optional[float] = None,
                 fixed_baselines: Optional[Sequence[float]] = None,
                 indents: Optional[Sequence[float]] = None,
                 erase_rects: Sequence[tuple] = (),
                 erase_clip: Optional[tuple] = None,
                 doc_families: Sequence[str] = ()) -> None:
        self.controller = controller
        self.window = controller.window
        self.canvas: tk.Canvas = controller.window.canvas
        self.palette = controller.palette
        self.mode = mode
        self.model = model
        self.page = page
        self.anchor_x, self.baseline = anchor
        self.registry = registry
        self.on_finish = on_finish
        self.wrap_width = wrap_width
        self.fixed_baselines = list(fixed_baselines) if fixed_baselines else None
        self.indents = list(indents) if indents else None
        self.erase_rects = list(erase_rects)
        self.erase_clip = erase_clip
        self.closed = False
        # What the box looked like when it opened, to detect "no change".
        self._initial = (model.text, list(model.styles), model.align,
                         anchor, wrap_width)
        self.layout: TE.Layout = self._compute_layout()
        self._renderer = TE.PreviewRenderer()
        self._erased: Optional[TE.ErasedPage] = None
        if self.erase_rects and erase_clip is not None:
            self._erased = TE.ErasedPage(controller._document(), page,
                                         self.erase_rects)
        self._photos: list[ImageTk.PhotoImage] = []   # keep images alive
        self._caret_visible = True
        self._blink_job: Optional[str] = None
        self._goal_x: Optional[float] = None
        self._gesture: Optional[dict] = None
        self._rich_clip: Optional[tuple[str, list]] = None
        self._sel_fill = _mix(self.palette.accent, "#ffffff", 0.62)
        self.bar = FormatBar(self, doc_families)
        self._install_keys()
        self.refresh()
        self._restart_blink()

    # ------------------------------------------------------------ geometry
    def _zoom(self) -> float:
        """Canvas pixels per PDF point at the current view."""
        return self.controller._factor() * self.window.scale

    def pt_to_canvas(self, x: float, y: float) -> tuple[float, float]:
        window = self.window
        factor = self.controller._factor()
        return ((x * factor - window.off_x) * window.scale,
                (y * factor - window.off_y) * window.scale)

    def canvas_to_pt(self, cx: float, cy: float) -> tuple[float, float]:
        window = self.window
        factor = self.controller._factor()
        return ((window.off_x + cx / window.scale) / factor,
                (window.off_y + cy / window.scale) / factor)

    def origin(self) -> tuple[float, float]:
        """First-baseline origin of the box in page points."""
        return TE.anchored_origin(self.anchor_x, self.baseline, self.layout,
                                  self.model.align, self.wrap_width)

    def box_pts(self) -> tuple[float, float, float, float]:
        """Text box in page points (at least as wide as the wrap width)."""
        ox, oy = self.origin()
        lo, hi = self.layout.extent_x
        return (ox + lo, oy + self.layout.top, ox + hi, oy + self.layout.bottom)

    def box_canvas(self) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.box_pts()
        cx0, cy0 = self.pt_to_canvas(x0, y0)
        cx1, cy1 = self.pt_to_canvas(x1, y1)
        # An empty auto-width box still needs a visible, clickable width.
        if cx1 - cx0 < 12:
            cx1 = cx0 + 12
        return cx0, cy0, cx1, cy1

    def index_at_canvas(self, cx: float, cy: float) -> int:
        x, y = self.canvas_to_pt(cx, cy)
        ox, oy = self.origin()
        return self.layout.index_at(x - ox, y - oy)

    def hit(self, cx: float, cy: float) -> Optional[str]:
        """``'text'``, ``'move'``, ``'handle'`` or None for a canvas point."""
        x0, y0, x1, y1 = self.box_canvas()
        pad = FRAME_PAD_PX
        hx, hy = x1 + pad, (y0 + y1) / 2.0
        if abs(cx - hx) <= HANDLE_PX + 3 and abs(cy - hy) <= HANDLE_PX + 3:
            return "handle"
        if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
            return "text"
        outer = pad + GRIP_PX
        if x0 - outer <= cx <= x1 + outer and y0 - outer <= cy <= y1 + outer:
            return "move"
        return None

    # -------------------------------------------------------------- layout
    def _compute_layout(self) -> TE.Layout:
        model = self.model
        # Per-line indents only make sense for left-aligned text.
        indents = self.indents if model.align == "left" else None
        return TE.layout_rich(model.text, model.styles, self.registry,
                              align=model.align, line_height=model.line_height,
                              wrap_width=self.wrap_width,
                              fixed_baselines=self.fixed_baselines,
                              indents=indents, empty_style=model.base_style)

    def changed(self) -> bool:
        """True when committing would change the PDF."""
        text, styles, align, anchor, wrap = self._initial
        if self.mode == "new":
            return bool(self.model.text.strip())
        return not (self.model.same_content(text, styles, align)
                    and anchor == (self.anchor_x, self.baseline)
                    and wrap == self.wrap_width)

    # ------------------------------------------------------------- drawing
    def refresh(self, *, relayout: bool = True) -> None:
        """Redraw everything; ``relayout`` re-measures the text first."""
        if self.closed:
            return
        if relayout:
            self.layout = self._compute_layout()
        canvas = self.canvas
        canvas.delete(TAG)
        self._photos = []
        zoom = self._zoom()
        window = self.window
        ox, oy = self.origin()

        # 1) the erased page patch hides the original glyphs (block mode)
        if self._erased is not None and self.erase_clip is not None:
            shift = (-window.off_x * window.scale, -window.off_y * window.scale)
            try:
                image, px, py = self._erased.render(self.erase_clip, zoom, shift)
                photo = ImageTk.PhotoImage(image, master=window)
                self._photos.append(photo)
                canvas.create_image(px, py, image=photo, anchor="nw", tags=TAG)
            except Exception:
                pass

        # 2) selection highlight, drawn under the transparent text image
        a, b = self.model.selection()
        for rx0, ry0, rx1, ry1 in self.layout.selection_rects(a, b):
            cx0, cy0 = self.pt_to_canvas(ox + rx0, oy + ry0)
            cx1, cy1 = self.pt_to_canvas(ox + rx1, oy + ry1)
            canvas.create_rectangle(cx0, cy0, cx1, cy1, fill=self._sel_fill,
                                    outline="", tags=TAG)

        # 3) the live text, rendered by MuPDF exactly as it will be saved
        if self.model.text:
            image, left, top = self._renderer.render(self.layout, zoom)
            cx, cy = self.pt_to_canvas(ox + left, oy + top)
            photo = ImageTk.PhotoImage(image, master=window)
            self._photos.append(photo)
            canvas.create_image(round(cx), round(cy), image=photo, anchor="nw",
                                tags=TAG)

        # 4) dashed frame and width handle
        x0, y0, x1, y1 = self.box_canvas()
        pad = FRAME_PAD_PX
        accent = self.palette.accent
        canvas.create_rectangle(x0 - pad, y0 - pad, x1 + pad, y1 + pad,
                                outline=accent, dash=(4, 3), width=1, tags=TAG)
        hx, hy = x1 + pad, (y0 + y1) / 2.0
        canvas.create_rectangle(hx - HANDLE_PX, hy - HANDLE_PX, hx + HANDLE_PX,
                                hy + HANDLE_PX, fill="#ffffff", outline=accent,
                                width=1, tags=TAG)

        # 5) caret (hidden while a selection is shown, like most editors)
        if not self.model.has_selection:
            cxr, base, ascent, descent = self.layout.caret_geometry(self.model.caret)
            top_x, top_y = self.pt_to_canvas(ox + cxr, oy + base - ascent)
            _bx, bot_y = self.pt_to_canvas(ox + cxr, oy + base + descent)
            color = self.model.style_at(self.model.caret).color
            if _luminance(color) > 0.75:
                color = accent      # keep the caret visible for white text
            canvas.create_line(top_x, top_y, top_x, bot_y, fill=color,
                               width=max(1.0, min(2.0, zoom * 0.08)),
                               tags=(TAG, TAG + "_caret"),
                               state="normal" if self._caret_visible else "hidden")
        canvas.tag_raise(TAG)
        self.bar.sync()
        self.bar.place((x0 - pad, y0 - pad, x1 + pad, y1 + pad))

    def _restart_blink(self) -> None:
        if self._blink_job is not None:
            try:
                self.canvas.after_cancel(self._blink_job)
            except Exception:
                pass
        self._caret_visible = True
        self.canvas.itemconfigure(TAG + "_caret", state="normal")
        self._blink_job = self.canvas.after(CARET_BLINK_MS, self._blink)

    def _blink(self) -> None:
        if self.closed:
            return
        self._caret_visible = not self._caret_visible
        self.canvas.itemconfigure(TAG + "_caret",
                                  state="normal" if self._caret_visible else "hidden")
        self._blink_job = self.canvas.after(CARET_BLINK_MS, self._blink)

    def _changed(self, *, keep_goal: bool = False) -> None:
        """Common tail of every model change."""
        if not keep_goal:
            self._goal_x = None
        self.refresh()
        self._restart_blink()

    # ---------------------------------------------------------- formatting
    def apply_style(self, **changes) -> None:
        self.model.apply_style(**changes)
        self._changed()

    def toggle(self, flag: str) -> None:
        self.model.toggle_flag(flag)
        self._changed()

    def set_align(self, align: str) -> None:
        if self.wrap_width is None and align != self.model.align:
            # Keep an auto-width box visually in place when re-anchoring it.
            ox, _oy = self.origin()
            width = self.layout.width
            self.anchor_x = {"left": ox, "justify": ox, "center": ox + width / 2.0,
                             "right": ox + width}[align]
        self.model.set_align(align)
        self._changed()

    def step_size(self, direction: int) -> None:
        """Ctrl+] / Ctrl+[ : next or previous size in the size list."""
        current = self.model.style_at(self.model.caret).size
        sizes = [float(s) for s in SIZE_CHOICES]
        if direction > 0:
            bigger = [s for s in sizes if s > current + 1e-6]
            target = bigger[0] if bigger else current + 12
        else:
            smaller = [s for s in sizes if s < current - 1e-6]
            target = smaller[-1] if smaller else max(TE.MIN_FONT_SIZE, current - 1)
        self.apply_style(size=target)

    # ------------------------------------------------------------ keyboard
    def _install_keys(self) -> None:
        canvas = self.canvas
        canvas.bind_class(BINDTAG, "<KeyPress>", self._on_key)
        tags = list(canvas.bindtags())
        if BINDTAG not in tags:
            canvas.bindtags((BINDTAG, *tags))
        canvas.focus_set()

    def _remove_keys(self) -> None:
        try:
            tags = [t for t in self.canvas.bindtags() if t != BINDTAG]
            self.canvas.bindtags(tuple(tags))
        except tk.TclError:
            pass

    def _on_key(self, event) -> Optional[str]:
        """Every keystroke while the editor is open lands here first."""
        if self.closed:
            return None
        keysym = event.keysym
        state = int(event.state)
        ctrl = bool(state & CTRL_MASK)
        shift = bool(state & SHIFT_MASK)
        alt = bool(state & ALT_MASK)
        model = self.model
        if keysym in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L",
                      "Alt_R", "Caps_Lock", "Num_Lock", "Win_L", "Win_R"):
            return "break"
        if ctrl and not alt:
            key = keysym.lower()
            if key == "return":
                self.finish(commit=True)
                return "break"
            if key == "s":
                # Commit, then let the window's Ctrl+S save the document.
                self.finish(commit=True)
                return None
            handled = self._ctrl_shortcut(key, shift)
            if handled:
                return "break"
        if keysym == "Escape":
            self.finish(commit=False)
            return "break"
        if keysym in ("Left", "Right", "Up", "Down", "Home", "End"):
            self._navigate(keysym, shift=shift, word=ctrl)
            return "break"
        if keysym == "BackSpace":
            model.delete_backward(word=ctrl)
            self._changed()
            return "break"
        if keysym == "Delete":
            model.delete_forward(word=ctrl)
            self._changed()
            return "break"
        if keysym in ("Return", "KP_Enter"):
            model.insert("\n")
            self._changed()
            return "break"
        if keysym == "Tab":
            model.insert("    ")
            self._changed()
            return "break"
        char = event.char
        # AltGr arrives as Ctrl+Alt on Windows (French "@", "€"...): accept it.
        if char and len(char) == 1 and char >= " " and char != "\x7f" and (
                not ctrl or alt):
            model.insert(char)
            self._changed()
        return "break"

    def _ctrl_shortcut(self, key: str, shift: bool) -> bool:
        model = self.model
        if key == "a":
            model.select_all()
        elif key in ("c", "insert"):
            self._copy()
            return True
        elif key == "x":
            self._copy()
            model.cut()
        elif key == "v":
            self._paste()
        elif key == "z" and not shift:
            model.undo()
        elif key == "y" or (key == "z" and shift):
            model.redo()
        elif key == "b":
            model.toggle_flag("bold")
        elif key == "i":
            model.toggle_flag("italic")
        elif key == "u":
            model.toggle_flag("underline")
        elif key in ("l", "e", "r", "j"):
            self.set_align({"l": "left", "e": "center", "r": "right",
                            "j": "justify"}[key])
            return True
        elif key in ("bracketright", "greater", "plus", "equal"):
            self.step_size(+1)
            return True
        elif key in ("bracketleft", "less", "minus"):
            self.step_size(-1)
            return True
        elif key in ("left", "right", "home", "end", "backspace", "delete"):
            return False       # word / document variants handled below
        else:
            return True        # swallow other window shortcuts while typing
        self._changed()
        return True

    def _copy(self) -> None:
        chunk = self.model.selected_text()
        if not chunk:
            return
        a, b = self.model.selection()
        # Keep the formatting for pastes back into the editor.
        self._rich_clip = (chunk, list(self.model.styles[a:b]))
        try:
            self.canvas.clipboard_clear()
            self.canvas.clipboard_append(chunk)
        except tk.TclError:
            pass

    def _paste(self) -> None:
        try:
            chunk = self.canvas.clipboard_get()
        except tk.TclError:
            return
        chunk = TE.normalize_text(chunk)
        if not chunk:
            return
        rich = self._rich_clip
        if rich is not None and rich[0] == chunk:
            # Formatted paste of text copied from this editor.
            model = self.model
            model.insert(chunk)
            start = model.caret - len(chunk)
            model.styles[start:model.caret] = list(rich[1])
        else:
            self.model.insert(chunk)

    def _navigate(self, keysym: str, *, shift: bool, word: bool) -> None:
        model = self.model
        layout = self.layout
        caret = model.caret
        keep_goal = False
        if keysym in ("Left", "Right") and model.has_selection and not shift:
            # Collapse the selection towards the arrow, like every editor.
            a, b = model.selection()
            model.set_caret(a if keysym == "Left" else b)
            self._changed()
            return
        if keysym == "Left":
            target = model.word_left(caret) if word else caret - 1
        elif keysym == "Right":
            target = model.word_right(caret) if word else caret + 1
        elif keysym in ("Up", "Down"):
            keep_goal = True
            index = layout.line_index_of(caret)
            if self._goal_x is None:
                self._goal_x = layout.caret_geometry(caret)[0]
            step = -1 if keysym == "Up" else 1
            if 0 <= index + step < len(layout.lines):
                target = layout.index_in_line(layout.lines[index + step], self._goal_x)
            else:
                target = 0 if step < 0 else len(model.text)
        elif keysym == "Home":
            if word:
                target = 0
            else:
                target = layout.lines[layout.line_index_of(caret)].start
        else:  # End
            if word:
                target = len(model.text)
            else:
                line = layout.lines[layout.line_index_of(caret)]
                target = line.end
                if not line.hard and target > line.start and model.text[target - 1] == " ":
                    target -= 1   # stay on this visual line
        model.set_caret(target, extend=shift)
        self._changed(keep_goal=keep_goal)

    # --------------------------------------------------------------- mouse
    def press(self, event) -> bool:
        """Canvas press; False when it landed outside the editor."""
        zone = self.hit(event.x, event.y)
        if zone is None:
            return False
        self.canvas.focus_set()
        if zone == "handle":
            current = self.wrap_width if self.wrap_width is not None else self.layout.width
            self._gesture = {"kind": "resize", "x": event.x, "width": current,
                             "origin": self.origin()}
        elif zone == "move":
            self._gesture = {"kind": "move", "x": event.x, "y": event.y,
                             "anchor": (self.anchor_x, self.baseline)}
        else:
            shift = bool(int(event.state) & SHIFT_MASK)
            self.model.set_caret(self.index_at_canvas(event.x, event.y), extend=shift)
            self._gesture = {"kind": "select"}
            self._changed()
        return True

    def drag(self, event) -> bool:
        gesture = self._gesture
        if gesture is None:
            return False
        zoom = self._zoom()
        if gesture["kind"] == "select":
            self.model.set_caret(self.index_at_canvas(event.x, event.y), extend=True)
            self._changed()
        elif gesture["kind"] == "move":
            ax, ay = gesture["anchor"]
            self.anchor_x = ax + (event.x - gesture["x"]) / zoom
            self.baseline = ay + (event.y - gesture["y"]) / zoom
            self.refresh(relayout=False)
        elif gesture["kind"] == "resize":
            width = max(MIN_WRAP_PT, gesture["width"] + (event.x - gesture["x"]) / zoom)
            if self.wrap_width is None:
                # Switching an auto-width box to wrapping: pin its left edge.
                self.anchor_x = gesture["origin"][0]
            self.wrap_width = width
            if self.model.align in ("center", "right"):
                self.anchor_x = gesture["origin"][0]
            self.refresh()
        return True

    def release(self, _event) -> bool:
        if self._gesture is None:
            return False
        self._gesture = None
        return True

    def double_click(self, event) -> bool:
        if self.hit(event.x, event.y) != "text":
            return False
        index = self.index_at_canvas(event.x, event.y)
        start, end = self.model.word_bounds(index)
        self.model.select_range(start, end)
        self._changed()
        return True

    def triple_click(self, event) -> bool:
        if self.hit(event.x, event.y) != "text":
            return False
        text = self.model.text
        index = self.index_at_canvas(event.x, event.y)
        start = text.rfind("\n", 0, index) + 1
        end = text.find("\n", index)
        self.model.select_range(start, len(text) if end < 0 else end)
        self._changed()
        return True

    def cursor_for(self, cx: float, cy: float) -> Optional[str]:
        zone = self.hit(cx, cy)
        return {"text": "xterm", "move": "fleur",
                "handle": "sb_h_double_arrow"}.get(zone or "")

    # ------------------------------------------------------------- closing
    def finish(self, *, commit: bool) -> None:
        """Close the editor (idempotent) and hand over to the controller."""
        if self.closed:
            return
        self.closed = True
        if self._blink_job is not None:
            try:
                self.canvas.after_cancel(self._blink_job)
            except Exception:
                pass
        self._remove_keys()
        self.bar.destroy()
        try:
            self.canvas.delete(TAG)
        except tk.TclError:
            pass
        self._photos = []
        self._renderer.close()
        if self._erased is not None:
            self._erased.close()
        self.on_finish(self, commit)


# ---------------------------------------------------------------------------
# Format bar


def _align_icon(kind: str, color: str, size: int = 16) -> Image.Image:
    """A crisp 16 px alignment glyph: four bars aligned like the text."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rgb = tuple(int(v * 255) for v in TE.hex_to_rgb(color))
    widths = (14, 9, 14, 7) if kind != "justify" else (14, 14, 14, 9)
    for row, width in enumerate(widths):
        y = 2 + row * 4
        if kind == "center":
            x0 = (size - width) // 2
        elif kind == "right":
            x0 = size - 1 - width
        else:
            x0 = 1
        draw.rectangle((x0, y, x0 + width - 1, y + 1), fill=rgb + (255,))
    return image


class FormatBar:
    """Floating formatting toolbar shown just above the text box."""

    def __init__(self, editor: InlineTextEditor, doc_families: Sequence[str]) -> None:
        self.editor = editor
        canvas = editor.canvas
        p = editor.palette
        self.palette = p
        self._syncing = False
        self._place_pending = False
        self._popover: Optional[tk.Toplevel] = None
        self.frame = tk.Frame(canvas, background=p.panel, highlightthickness=1,
                              highlightbackground=p.border, padx=4, pady=3)
        self.item = canvas.create_window(0, 0, window=self.frame, anchor="sw",
                                         tags=(TAG + "_bar",))
        self._images: list[ImageTk.PhotoImage] = []

        # Font family: fonts used in this document first, then every family.
        families = editor.registry.families()
        ordered = [f for f in doc_families if f in families]
        ordered += [f for f in families if f not in ordered]
        self._all_families = ordered
        self.family_var = tk.StringVar(master=canvas)
        self.family = ttk.Combobox(self.frame, textvariable=self.family_var,
                                   values=ordered, width=18, height=18)
        self.family.pack(side="left", padx=(0, 4))
        self.family.bind("<<ComboboxSelected>>", lambda _e: self._apply_family())
        self.family.bind("<Return>", lambda _e: (self._apply_family(), "break")[1])
        self.family.bind("<Escape>", lambda _e: (self._back_to_text(), "break")[1])
        self.family.bind("<KeyRelease>", self._filter_families)
        bind_tooltip(self.family, "Font", p)

        self.size_var = tk.StringVar(master=canvas)
        self.size = ttk.Combobox(self.frame, textvariable=self.size_var,
                                 values=SIZE_CHOICES, width=5, height=19)
        self.size.pack(side="left", padx=(0, 6))
        self.size.bind("<<ComboboxSelected>>", lambda _e: self._apply_size())
        self.size.bind("<Return>", lambda _e: (self._apply_size(), "break")[1])
        self.size.bind("<Escape>", lambda _e: (self._back_to_text(), "break")[1])
        self.size.bind("<MouseWheel>", self._wheel_size)
        bind_tooltip(self.size, "Font size (Ctrl+] / Ctrl+[)", p)

        self._sep()
        self.bold = self._toggle("B", ("Segoe UI", 10, "bold"),
                                 lambda: editor.toggle("bold"), "Bold (Ctrl+B)")
        self.italic = self._toggle("I", ("Georgia", 10, "italic"),
                                   lambda: editor.toggle("italic"), "Italic (Ctrl+I)")
        self.underline = self._toggle("U", ("Segoe UI", 10, "underline"),
                                      lambda: editor.toggle("underline"),
                                      "Underline (Ctrl+U)")
        self._sep()
        self.color = tk.Canvas(self.frame, width=26, height=24, highlightthickness=0,
                               background=p.panel, cursor="hand2")
        self.color.pack(side="left", padx=1)
        self.color.bind("<Button-1>", lambda _e: self._open_swatches())
        bind_tooltip(self.color, "Text colour", p)
        self._sep()
        self.aligns: dict[str, tk.Label] = {}
        for kind, tip in (("left", "Align left (Ctrl+L)"),
                          ("center", "Centre (Ctrl+E)"),
                          ("right", "Align right (Ctrl+R)"),
                          ("justify", "Justify (Ctrl+J)")):
            photo = ImageTk.PhotoImage(_align_icon(kind, p.text), master=canvas)
            self._images.append(photo)
            label = self._button(image=photo,
                                 command=lambda k=kind: editor.set_align(k), tip=tip)
            self.aligns[kind] = label
        self._sep()
        self.done = self._button(text="Done", command=lambda: editor.finish(commit=True),
                                 tip="Place the text (Ctrl+Enter or click outside)",
                                 accent=True)
        self.cancel = self._button(text="✕", command=lambda: editor.finish(commit=False),
                                   tip="Discard changes (Esc)")

    # -- widgets -------------------------------------------------------------
    def _sep(self) -> None:
        tk.Frame(self.frame, width=1, background=self.palette.border).pack(
            side="left", fill="y", padx=4, pady=2)

    def _button(self, *, text: str = "", image=None, command, tip: str,
                font=None, accent: bool = False) -> tk.Label:
        p = self.palette
        bg = p.accent if accent else p.panel
        fg = p.accent_text if accent else p.text
        label = tk.Label(self.frame, text=text, image=image, background=bg,
                         foreground=fg, font=font or "FVui", padx=6 if text else 4,
                         pady=2, cursor="hand2", borderwidth=0)
        label.pack(side="left", padx=1)
        label._fv_bg = bg          # remembered so hover/toggle can restore it
        label._fv_on = False
        label.bind("<Button-1>", lambda _e: (command(), "break")[1])
        label.bind("<Enter>", lambda _e: label.configure(
            background=p.hover if not accent and not label._fv_on else label["background"]))
        label.bind("<Leave>", lambda _e: label.configure(
            background=label._fv_bg if not label._fv_on else p.select))
        bind_tooltip(label, tip, p)
        return label

    def _toggle(self, text: str, font, command, tip: str) -> tk.Label:
        return self._button(text=text, font=font, command=command, tip=tip)

    def _set_on(self, label: tk.Label, on: bool) -> None:
        label._fv_on = on
        label.configure(background=self.palette.select if on else label._fv_bg)

    # -- state ---------------------------------------------------------------
    def sync(self) -> None:
        """Reflect the style at the caret (or selection start)."""
        editor = self.editor
        model = editor.model
        style = model.style_at(model.caret)
        self._syncing = True
        try:
            try:
                focused = self.frame.focus_get()
            except KeyError:   # focus inside a combobox drop-down list
                focused = self.family
            if focused not in (self.family, self.size):
                self.family_var.set(style.family)
                size = style.size
                self.size_var.set(f"{size:g}")
        finally:
            self._syncing = False
        a, b = model.selection()
        styles = model.styles[a:b] if model.has_selection else [style]
        self._set_on(self.bold, all(s.bold for s in styles))
        self._set_on(self.italic, all(s.italic for s in styles))
        self._set_on(self.underline, all(s.underline for s in styles))
        for kind, label in self.aligns.items():
            self._set_on(label, model.align == kind)
        self.color.delete("all")
        p = self.palette
        self.color.create_text(13, 10, text="A", fill=p.text, font=("Segoe UI", 10, "bold"))
        self.color.create_rectangle(4, 18, 22, 22, fill=style.color, outline=p.border)

    def place(self, frame_box: tuple[float, float, float, float]) -> None:
        """Float above the text frame; flip below it near the canvas top."""
        canvas = self.editor.canvas
        x0, y0, _x1, y1 = frame_box
        if not canvas.type(self.item):
            # The viewer cleared the canvas (document reload): recreate.
            self.item = canvas.create_window(0, 0, window=self.frame,
                                             anchor="sw", tags=(TAG + "_bar",))
        # Requested size is known once Tk has computed the geometry; no
        # update_idletasks here (this runs inside the viewer's paint).
        bar_w = self.frame.winfo_reqwidth()
        bar_h = self.frame.winfo_reqheight()
        if bar_w <= 1 and not self._place_pending:
            self._place_pending = True
            canvas.after_idle(self._deferred_place)
        canvas_w = max(1, canvas.winfo_width())
        x = min(max(4, x0), max(4, canvas_w - bar_w - 4))
        if y0 - BAR_GAP_PX - bar_h >= 2:
            canvas.coords(self.item, x, y0 - BAR_GAP_PX)
            canvas.itemconfigure(self.item, anchor="sw")
        else:
            canvas.coords(self.item, x, y1 + BAR_GAP_PX)
            canvas.itemconfigure(self.item, anchor="nw")

    def _deferred_place(self) -> None:
        self._place_pending = False
        if not self.editor.closed:
            self.editor.refresh(relayout=False)

    # -- actions ---------------------------------------------------------------
    def _back_to_text(self) -> None:
        self.editor.canvas.focus_set()
        self.sync()

    def _apply_family(self) -> None:
        if self._syncing:
            return
        name = self.family_var.get().strip()
        canonical = self.editor.registry.canonical_family(name)
        if canonical is None:
            matches = [f for f in self._all_families if f.lower().startswith(name.lower())]
            canonical = matches[0] if matches and name else None
        if canonical is None:
            self.editor.window.toast(f"Font “{name}” is not installed")
        else:
            self.editor.apply_style(family=canonical)
        self.family.configure(values=self._all_families)
        self._back_to_text()

    def _filter_families(self, event) -> None:
        if event.keysym in ("Return", "Escape", "Up", "Down", "Tab"):
            return
        typed = self.family_var.get().strip().lower()
        values = ([f for f in self._all_families if typed in f.lower()]
                  if typed else self._all_families)
        self.family.configure(values=values or self._all_families)

    def _apply_size(self) -> None:
        if self._syncing:
            return
        try:
            value = float(self.size_var.get().replace(",", "."))
        except ValueError:
            self.editor.window.toast("Font size must be a number")
            self._back_to_text()
            return
        self.editor.apply_style(size=TE.clamp_size(value))
        self._back_to_text()

    def _wheel_size(self, event) -> str:
        self.editor.step_size(+1 if event.delta > 0 else -1)
        return "break"

    def _open_swatches(self) -> None:
        self._close_popover()
        p = self.palette
        top = tk.Toplevel(self.frame)
        top.overrideredirect(True)
        top.configure(background=p.border)
        body = tk.Frame(top, background=p.panel, padx=6, pady=6)
        body.pack(padx=1, pady=1)
        for index, color in enumerate(SWATCHES):
            cell = tk.Canvas(body, width=20, height=20, highlightthickness=1,
                             highlightbackground=p.border, background=color,
                             cursor="hand2")
            cell.grid(row=index // 6, column=index % 6, padx=2, pady=2)
            cell.bind("<Button-1>", lambda _e, c=color: self._pick_color(c))
        more = tk.Label(body, text="More colours…", background=p.panel,
                        foreground=p.accent, cursor="hand2", font="FVsmall")
        more.grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 0))
        more.bind("<Button-1>", lambda _e: self._custom_color())
        x = self.color.winfo_rootx()
        y = self.color.winfo_rooty() + self.color.winfo_height() + 2
        top.geometry(f"+{x}+{y}")
        top.bind("<FocusOut>", lambda _e: top.after(30, self._popover_focus_check))
        top.bind("<Escape>", lambda _e: self._close_popover())
        top.focus_force()
        self._popover = top

    def _popover_focus_check(self) -> None:
        """Close the swatch popover once focus has really left it."""
        top = self._popover
        if top is None:
            return
        try:
            focused = top.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None or not str(focused).startswith(str(top)):
            self._close_popover()

    def _close_popover(self) -> None:
        if self._popover is not None:
            try:
                self._popover.destroy()
            except tk.TclError:
                pass
            self._popover = None

    def _pick_color(self, color: str) -> None:
        self._close_popover()
        self.editor.apply_style(color=color)
        self._back_to_text()

    def _custom_color(self) -> None:
        self._close_popover()
        current = self.editor.model.style_at(self.editor.model.caret).color
        result = colorchooser.askcolor(color=current, parent=self.editor.window,
                                       title="Text colour")
        if result and result[1]:
            self.editor.apply_style(color=result[1])
        self._back_to_text()

    def destroy(self) -> None:
        self._close_popover()
        try:
            self.editor.canvas.delete(self.item)
        except tk.TclError:
            pass
        try:
            self.frame.destroy()
        except tk.TclError:
            pass
