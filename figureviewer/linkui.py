"""Link hover, click and context-menu glue for the viewer window.

:class:`LinkController` owns, for the page on screen, the
:class:`~figureviewer.links.LinkLayer` (the PDF's own link annotations
plus addresses detected in the text or OCR layer) and:

* the hover feedback: a hand cursor, a soft highlight under the link and
  the destination in the status bar, the way every browser does it;
* the click: a press and release without movement on the same link opens
  it (web addresses, e-mail and DOIs in the default browser or mail client,
  page jumps inside the document); a drag still selects text or pans, so
  nothing about the reading gestures changes;
* the context menu entries "Open Link" and "Copy Link Address".

Only ``http``, ``https``, ``mailto`` and ``ftp`` targets are ever handed to
the operating system (:data:`figureviewer.links.OPENABLE_SCHEMES`); file
launch links are described but never followed.
"""
from __future__ import annotations

import tkinter as tk
import webbrowser
from typing import Optional

from . import docs as D
from . import winclip
from .links import KIND_PAGE, KIND_URI, Link, LinkLayer

#: Cursor shown over a link.
CURSOR_LINK = "hand2"
#: A press/release pair further apart than this (canvas pixels) is a drag,
#: never a click.
CLICK_SLOP_PX = 4
#: Canvas tag of the hover highlight.
TAG_HOVER = "linkhover"


class LinkController:
    """Link state and actions for one :class:`ViewerWindow`."""

    def __init__(self, window) -> None:
        self.window = window
        self.app = window.app
        self.palette = window.palette
        self._layer: Optional[LinkLayer] = None
        self._layer_key: Optional[tuple] = None
        self._hover: Optional[Link] = None
        self._press: Optional[tuple[Link, int, int]] = None
        self._status_owned = False

    # ------------------------------------------------------------ layer
    def layer(self) -> Optional[LinkLayer]:
        """Links of the page on screen, rebuilt when the document, page,
        content version or text layer (an image's fresh OCR) changes."""
        window = self.window
        document = window.document
        if document is None or window._video_session is not None:
            return None
        text_layer = window.text.layer()
        key = (id(document), window.page,
               int(getattr(document, "content_version", 0)), id(text_layer))
        if key != self._layer_key:
            self._layer_key = key
            try:
                explicit = document.links(window.page)
            except Exception:
                explicit = []
            self._layer = LinkLayer.build(explicit, text_layer)
        return self._layer

    def count(self) -> int:
        layer = self.layer()
        return len(layer) if layer is not None else 0

    def _enabled(self) -> bool:
        """Links respond whenever text selection would: not in snip mode
        and not while an edit tool other than 'select' owns the pointer."""
        return self.window.text._enabled()

    def link_at(self, x: float, y: float) -> Optional[Link]:
        """The link under canvas point ``(x, y)`` or ``None``."""
        if not self._enabled():
            return None
        layer = self.layer()
        if layer is None or not len(layer):
            return None
        return layer.hit(*self.window.text._canvas_to_source(x, y))

    # -------------------------------------------------------- gestures
    def on_motion(self, event) -> bool:
        """Hand cursor and status over a link. Returns True while the
        pointer is on a link, in which case the text controller must not
        touch the cursor; on leaving a link the cursor is reset so the
        text controller re-evaluates it from scratch."""
        window = self.window
        link = None
        if window._pan_anchor is None and window.text._drag is None:
            link = self.link_at(event.x, event.y)
        if link is self._hover:
            return link is not None
        self._hover = link
        self._draw_hover()
        canvas = window.canvas
        if link is not None:
            try:
                canvas.configure(cursor=CURSOR_LINK)
            except tk.TclError:
                pass
            self._status_owned = True
            verb = "click to follow" if link.kind == KIND_PAGE else "click to open"
            window.status_var.set(
                f"{link.describe()}  ·  {verb}" if link.openable() else link.describe())
            return True
        try:
            canvas.configure(cursor="crosshair" if window.snip_mode.get() else "")
        except tk.TclError:
            pass
        window.text.cursor_reset()
        self._restore_status()
        return False

    def _restore_status(self) -> None:
        if not self._status_owned:
            return
        self._status_owned = False
        window = self.window
        if window.text.selected:
            window.text._announce()
        else:
            window._update_status("Ready")

    def on_press(self, event) -> None:
        """Remember the link under a press; never consumes the event, so a
        drag that starts on a link still selects text or pans."""
        self._press = None
        link = self.link_at(event.x, event.y)
        if link is not None:
            self._press = (link, int(event.x), int(event.y))

    def on_release(self, event) -> Optional[Link]:
        """The link to open: press and release on the same link, with no
        drag in between. ``None`` otherwise."""
        press = self._press
        self._press = None
        if press is None:
            return None
        link, x, y = press
        if abs(event.x - x) > CLICK_SLOP_PX or abs(event.y - y) > CLICK_SLOP_PX:
            return None
        return link if self.link_at(event.x, event.y) is link else None

    # --------------------------------------------------------- actions
    def open(self, link: Link) -> bool:
        """Follow a link; False when it cannot be followed (a toast says why)."""
        window = self.window
        if link.kind == KIND_PAGE:
            document = window.document
            if document is None or not (0 <= link.page < document.page_count):
                window.toast("This link points outside the document")
                return False
            return window.index.navigate(
                D.OutlineEntry(1, "Link", link.page, link.y_pts))
        if link.kind == KIND_URI:
            if not link.openable():
                window.toast(f"Link type not opened for safety: {link.target}")
                return False
            try:
                opened = webbrowser.open(link.target)
            except Exception as exc:
                window.toast(f"Could not open {link.target}: {exc}")
                return False
            if not opened:
                window.toast(f"No application registered to open {link.target}")
                return False
            window.toast(f"Opened {link.target}")
            return True
        window.toast(f"{link.describe()}  ·  file links are not followed")
        return False

    def copy_address(self, link: Link) -> None:
        text = link.target if link.kind != KIND_PAGE else f"page {link.page + 1}"
        if not text:
            return
        try:
            winclip.copy_text(text)
        except Exception as exc:
            window = self.window
            from tkinter import messagebox
            messagebox.showerror("Clipboard error", str(exc), parent=window)
            return
        self.window.toast(f"Copied {text}")

    # ------------------------------------------------------ context menu
    def context_items(self, menu: tk.Menu, event) -> None:
        link = self.link_at(event.x, event.y)
        if link is None:
            return
        menu.add_separator()
        if link.kind == KIND_PAGE:
            label = f"Go to Page {link.page + 1}" if link.page >= 0 else "Broken Link"
        else:
            label = "Open Link"
        menu.add_command(label=label, command=lambda: self.open(link),
                         state="normal" if link.openable() else "disabled")
        if link.kind != KIND_PAGE and link.target:
            menu.add_command(label="Copy Link Address",
                             command=lambda: self.copy_address(link))

    # --------------------------------------------------------- drawing
    def _draw_hover(self) -> None:
        canvas = self.window.canvas
        try:
            canvas.delete(TAG_HOVER)
        except tk.TclError:
            return
        self.draw()

    def draw(self) -> None:
        """Highlight the hovered link; called after every _paint too, so a
        pan or zoom keeps the highlight under the link."""
        link = self._hover
        window = self.window
        if link is None or window.document is None:
            return
        canvas = window.canvas
        canvas.delete(TAG_HOVER)
        x0, y0, x1, y1 = window.text._source_rect_to_canvas(link.rect)
        colour = self.palette.accent
        canvas.create_rectangle(x0 - 2, y0 - 2, x1 + 2, y1 + 2, fill=colour,
                                outline="", stipple="gray25", tags=TAG_HOVER)
        canvas.create_line(x0 - 1, y1 + 2, x1 + 1, y1 + 2, fill=colour,
                            width=2, tags=TAG_HOVER)

    # ------------------------------------------------------- lifecycle
    def on_page_changed(self) -> None:
        self._reset()

    def on_document_loaded(self) -> None:
        self._layer = None
        self._layer_key = None
        self._reset()

    def _reset(self) -> None:
        self._hover = None
        self._press = None
        self._status_owned = False
        try:
            self.window.canvas.delete(TAG_HOVER)
        except tk.TclError:
            pass
