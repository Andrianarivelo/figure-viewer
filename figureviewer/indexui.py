"""Index panel: the document's outline (bookmarks) as a navigable tree.

:class:`IndexController` owns the panel that docks on the right of the
viewer whenever the current document carries an outline. It refreshes on
document load and after structural edits (page operations rewrite bookmark
targets), follows the page on screen by highlighting the deepest bookmark
that starts at or before it, and navigates on click: page first, then a
vertical landing that honours the bookmark's target point through the same
source-to-logical transform text selection uses, so rotated pages land
where the author intended.

Outline extraction itself is headless (:meth:`Document.outline`); this
module is Tk glue only.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional

from . import docs as D
from .textlayer import source_box_to_logical
from .theme import LIST_TREE, bind_tooltip, toolbar_button

#: Bookmark levels opened by default; deeper ones start collapsed.
EXPAND_LEVELS = 2
#: Pixels of breathing room above a bookmark's landing point.
LANDING_MARGIN_PX = 12
#: Default panel width in pixels (the details panel is 32 characters).
INDEX_WIDTH = 260


class IndexController:
    """Outline tree for one :class:`ViewerWindow`."""

    def __init__(self, window) -> None:
        self.window = window
        self.app = window.app
        self.palette = window.palette
        self.show_index = tk.BooleanVar(
            value=bool(self.app.settings.get("show_index", True)))
        self.entries: list[D.OutlineEntry] = []
        self.panel: Optional[ttk.Frame] = None
        self.tree: Optional[ttk.Treeview] = None
        self.filter_var = tk.StringVar()
        self.count_var = tk.StringVar(value="")
        self._suppress = False
        self._current_iid: Optional[str] = None

    # ----------------------------------------------------------- build
    def build(self, master: tk.Misc) -> ttk.Frame:
        p = self.palette
        frame = ttk.Frame(master, style="Panel.TFrame", padding=(12, 10, 8, 8))
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)
        header = ttk.Frame(frame, style="Panel.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="INDEX", image=self.window.icons.get("index", tone="muted"),
                  compound="left", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.count_var,
                  style="Muted.TLabel").grid(row=0, column=1, sticky="e")

        search = ttk.Entry(frame, textvariable=self.filter_var)
        search.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self.filter_var.trace_add("write", lambda *_: self._populate())
        bind_tooltip(search, "Filter bookmarks by title", p)
        self.window._install_placeholder(search, self.filter_var, "Search index…")

        holder = ttk.Frame(frame, style="Panel.TFrame")
        holder.grid(row=2, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        tree = ttk.Treeview(holder, columns=("page",), show="tree", height=8,
                            selectmode="browse", takefocus=True, style=LIST_TREE)
        tree.grid(row=0, column=0, sticky="nsew")
        tree.column("#0", width=INDEX_WIDTH - 60, stretch=True, anchor="w")
        tree.column("page", width=0, stretch=False)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview,
                               style="Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        tree.tag_configure("current", foreground=p.accent, font="FVuiBold")
        tree.tag_configure("nopage", foreground=p.muted)
        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Return>", lambda _e: self._on_select())
        self.tree = tree

        actions = ttk.Frame(frame, style="Panel.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        for index in range(2):
            actions.columnconfigure(index, weight=1)
        toolbar_button(actions, "Expand", lambda: self._set_open(True), p,
                       "Open every bookmark level").grid(row=0, column=0, sticky="ew",
                                                         padx=(0, 3))
        toolbar_button(actions, "Collapse", lambda: self._set_open(False), p,
                       "Show top-level bookmarks only").grid(row=0, column=1,
                                                             sticky="ew", padx=(3, 0))
        self.panel = frame
        return frame

    # --------------------------------------------------------- content
    @property
    def available(self) -> bool:
        return bool(self.entries)

    def refresh(self) -> None:
        """Re-read the outline from the current document and relayout."""
        document = self.window.document
        entries: list[D.OutlineEntry] = []
        if document is not None and self.window._video_session is None:
            try:
                entries = list(document.outline())
            except Exception:
                entries = []
        self.entries = entries
        self._populate()
        self.window._apply_layout()

    def _populate(self) -> None:
        tree = self.tree
        if tree is None:
            return
        self._suppress = True
        try:
            tree.delete(*tree.get_children(""))
            needle = self.filter_var.get().strip().lower()
            parents: dict[int, str] = {0: ""}
            shown = 0
            for index, entry in enumerate(self.entries):
                if needle and needle not in entry.title.lower():
                    continue
                # Filtering flattens the tree: matches are easier to scan
                # than a forest of half-opened branches.
                parent = "" if needle else parents.get(entry.level - 1, "")
                label = entry.title
                if entry.page >= 0:
                    label = f"{entry.title}    ·  {entry.page + 1}"
                tags = ("nopage",) if entry.page < 0 else ()
                iid = tree.insert(parent, "end", iid=f"o{index}", text=label,
                                  values=(entry.page,), tags=tags,
                                  open=entry.level < EXPAND_LEVELS)
                if not needle:
                    parents[entry.level] = iid
                    # A skipped level (1 -> 3) still nests under the nearest
                    # shallower ancestor.
                    for deeper in [k for k in parents if k > entry.level]:
                        del parents[deeper]
                shown += 1
            total = len(self.entries)
            self.count_var.set(f"{shown} of {total}" if needle else f"{total}")
        finally:
            self._suppress = False
        self._current_iid = None
        self.sync_page(self.window.page)

    def _set_open(self, state: bool) -> None:
        tree = self.tree
        if tree is None:
            return

        def walk(parent: str) -> None:
            for iid in tree.get_children(parent):
                tree.item(iid, open=state)
                walk(iid)
        walk("")

    # ------------------------------------------------------ navigation
    def current_entry_index(self, page: int) -> Optional[int]:
        """Index of the deepest bookmark starting at or before ``page``."""
        best = None
        for index, entry in enumerate(self.entries):
            if entry.page < 0 or entry.page > page:
                continue
            if best is None or entry.page >= self.entries[best].page:
                best = index
        return best

    def sync_page(self, page: int) -> None:
        """Highlight the bookmark that covers ``page`` without navigating."""
        tree = self.tree
        if tree is None or not self.entries:
            return
        index = self.current_entry_index(page)
        iid = f"o{index}" if index is not None else None
        if iid == self._current_iid:
            return
        if self._current_iid is not None and tree.exists(self._current_iid):
            tags = tuple(t for t in tree.item(self._current_iid, "tags") if t != "current")
            tree.item(self._current_iid, tags=tags)
        self._current_iid = iid
        if iid is None or not tree.exists(iid):
            return
        tree.item(iid, tags=tuple(tree.item(iid, "tags")) + ("current",))
        # Reveal it: open ancestors and scroll, but leave the selection to
        # the user so a page flip never re-triggers navigation.
        parent = tree.parent(iid)
        while parent:
            tree.item(parent, open=True)
            parent = tree.parent(parent)
        tree.see(iid)

    def _on_select(self, _event=None) -> None:
        if self._suppress or self.tree is None:
            return
        selection = self.tree.selection()
        if not selection:
            return
        try:
            index = int(selection[0][1:])
        except ValueError:
            return
        if 0 <= index < len(self.entries):
            self.navigate(self.entries[index])

    def navigate(self, entry: D.OutlineEntry) -> bool:
        """Jump to a bookmark: page, then its vertical target."""
        window = self.window
        document = window.document
        if document is None or entry.page < 0 or entry.page >= document.page_count:
            window.toast("This bookmark has no page target")
            return False
        if entry.page != window.page:
            window._set_document_page(entry.page)
        if entry.y_pts is not None:
            factor = float(getattr(document, "base_dpi", 72.0)) / 72.0
            size = document.page_size(entry.page)
            y_source = entry.y_pts * factor
            _x0, y0, _x1, _y1 = source_box_to_logical(
                (0.0, y_source, float(size[0]), y_source), size,
                window.rotation, window.flip_h, window.flip_v)
            _width, height = window.logical_size()
            _cw, ch = window._canvas_size()
            top = y0 - LANDING_MARGIN_PX / window.scale
            window.off_y = max(0.0, min(top, height - ch / window.scale))
            window._invalidate()
            window.redraw_now()
        self.sync_page(entry.page)
        window.toast(f"{entry.title}  ·  page {entry.page + 1}")
        return True

    # ---------------------------------------------------------- toggle
    def toggle(self) -> None:
        self.show_index.set(not self.show_index.get())
        self.window._apply_layout()

    def visible(self) -> bool:
        """Whether the panel should be on screen right now."""
        return bool(self.show_index.get() and self.available)
