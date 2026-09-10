"""Direct text review tools backed by ordinary, undoable PDF annotations.

Selection coordinates are captured before acquiring an edit session, because
that operation replaces the document and invalidates the text layer. The review
list is indexed in small Tk batches and never performs a full scan per keystroke.
"""
from __future__ import annotations

import csv
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import dialogs as DLG
from . import docs as D
from .theme import bind_tooltip, toolbar_button

# User-facing defaults and layout settings live together for easy adjustment.
REVIEW_WIDTH = 330
INDEX_BATCH = 12
COLORS = (("Yellow", "#ffd54a"), ("Green", "#8bd450"),
          ("Blue", "#5aa7f0"), ("Pink", "#f28cba"),
          ("Purple", "#b06ad4"))
DEFAULT_OPACITY = 0.35


class ReviewController:
    """One window's selection actions, persistent highlighter and review pane."""

    def __init__(self, edit):
        self.edit = edit
        self.window = edit.window
        self.palette = edit.palette
        self.mode = tk.StringVar(master=self.window, value="")
        self.query = tk.StringVar(master=self.window)
        self.kind = tk.StringVar(master=self.window, value="All")
        self.panel = None
        self.rows = []
        self._items = {}
        self._job = None
        self._generation = 0
        self._buttons = []
        self._restore_key = None

    def build_toolbar(self, parent):
        """Keep the three common review actions beside the existing Edit button."""
        group = ttk.Frame(parent, style="ToolbarGroup.TFrame")
        group.pack(side="left", after=self.edit.edit_button, padx=(0, 8))
        for mode, title, icon, tip in (
                ("highlight", "Highlight", "highlighter",
                 "Highlight selected text, or enable repeated drag highlighting (Ctrl+Alt+H)"),
                ("comment", "Comment", "note",
                 "Attach a comment to selected text (Ctrl+Alt+M)")):
            button = ttk.Checkbutton(
                group, text=title, image=self.window.icons.get(icon),
                compound="left", variable=self.mode, onvalue=mode, offvalue="",
                style="Modal.TCheckbutton", takefocus=False,
                command=lambda m=mode: self.action(m, toggled=True))
            button.pack(side="left")
            bind_tooltip(button, tip, self.palette)
            self._buttons.append(button)
        button = toolbar_button(group, "Review", self.toggle_panel, self.palette,
                                "Search, edit and export all PDF comments (Ctrl+Alt+R)")
        button.pack(side="left")
        self._buttons.append(button)

    def sync(self):
        """Disable PDF-only actions for images and videos."""
        available = self.edit.available()
        for button in self._buttons:
            button.state(["!disabled"] if available else ["disabled"])
        if not available:
            self.mode.set("")
            self.close_panel()

    def cancel_mode(self):
        """Escape exits a sticky review tool without discarding annotations."""
        active = bool(self.mode.get())
        self.mode.set("")
        return active

    def action(self, mode="highlight", *, toggled=False):
        """Apply to the current text selection, or arm a repeatable drag tool."""
        if not self.edit.available():
            return
        if self.window.text.has_selection():
            self.mode.set("")
            self.annotate_selection(mode)
            return
        if not toggled:
            self.mode.set("" if self.mode.get() == mode else mode)
        if not self.mode.get():
            return
        if self.window.snip_mode.get():
            self.window.snip_mode.set(False)
            self.window._snip_mode_changed()
        if self.edit.active:
            self.edit.set_tool("select")
            self.edit.deactivate()
        self.window.canvas.focus_set()
        self.window.toast("Drag across text to " + mode + "; Esc stops. "
                          "Scanned pages: Tools > Recognise Text (OCR).")

    def selection_finished(self):
        """Use reading-order selection instead of a bounding-box sweep."""
        if self.mode.get() and self.window.text.has_selection():
            self.annotate_selection(self.mode.get())

    def annotate_selection(self, mode="highlight"):
        """Create one PDF annotation, including its quote and optional comment."""
        text = self.window.text
        layer = text.layer()
        if layer is None or not text.selected:
            return
        quote = text.selected_text()
        rects = [self.edit._source_rect_to_points(rect)
                 for rect in layer.rects_for(text.selected)]
        comment = ""
        if mode == "comment":
            comment = DLG.ask_text(self.window, self.palette, "Comment on text",
                                   "Selected: " + quote[:220] + "\n\nComment:",
                                   "", multiline=True)
            if comment is None or not comment.strip():
                return
        if not self.edit._ensure_session():
            return
        from .editui import _hex_to_rgb
        defaults = self.edit._defaults()
        try:
            uid = self.edit.session.add_markup(
                self.window.page, [D.fitz.Rect(*r) for r in rects], "highlight",
                color=_hex_to_rgb(defaults.get("highlight_color", COLORS[0][1])),
                opacity=float(defaults.get("markup_opacity", DEFAULT_OPACITY)),
                contents=comment, subject=quote)
        except Exception as exc:
            messagebox.showerror("Annotate text", str(exc), parent=self.window)
            return
        text.clear_selection()
        self.window.toast("Comment added" if comment else "Text highlighted")
        if comment:
            self.open_panel()
            self._restore_key = f"{self.window.page}:{uid}"
        return uid

    def toggle_panel(self):
        """Open or collapse the docked review pane."""
        if self.panel is not None and self.panel.winfo_manager():
            self.close_panel()
        else:
            self.open_panel()

    def open_panel(self):
        """The panel shares the edit session and its save/undo history."""
        if not self.edit._ensure_session():
            return
        if self.panel is None:
            self._build_panel()
        self.panel.grid(row=0, column=1, rowspan=2, sticky="nsew")
        self.refresh()

    def close_panel(self):
        """Cancel indexing when hidden or while changing documents."""
        self._cancel_index()
        if self.panel is not None:
            self.panel.grid_remove()
        self.rows = []
        self._restore_key = None

    def _cancel_index(self):
        """Also used by Destroy: app-wide quit/theme rebuild bypass normal close."""
        self._generation += 1
        if self._job is not None:
            self.window.after_cancel(self._job)
            self._job = None

    def _build_panel(self):
        """A compact searchable list with full quote preview and review actions."""
        p = self.palette
        panel = ttk.Frame(self.window.stage, width=REVIEW_WIDTH,
                          style="Card.TFrame", padding=12)
        panel.grid_propagate(False)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(5, weight=1)
        self.panel = panel
        panel.bind("<Destroy>", lambda event: self._cancel_index()
                   if event.widget is panel else None, add="+")
        header = ttk.Frame(panel)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Text review", font="FVtitle").pack(side="left")
        ttk.Button(header, text="Close", command=self.close_panel).pack(side="right")
        ttk.Label(panel, text="Select text, then Highlight or Comment.",
                  wraplength=REVIEW_WIDTH - 30).grid(row=1, column=0, sticky="w", pady=6)
        settings = ttk.Frame(panel)
        settings.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for name, color in COLORS:
            chip = tk.Canvas(settings, width=22, height=22, background=color,
                             highlightthickness=1, highlightbackground=p.border,
                             cursor="hand2")
            chip.pack(side="left", padx=(0, 5))
            chip.bind("<Button-1>", lambda _e, c=color: self.set_color(c))
            bind_tooltip(chip, name + ": recolor selection and set next highlight color", p)
        self.opacity = tk.StringVar(master=panel, value=str(round(
            float(self.edit._defaults().get("markup_opacity", DEFAULT_OPACITY)) * 100)))
        opacity = ttk.Combobox(settings, textvariable=self.opacity, width=4,
                               values=("20", "35", "50", "75", "100"), state="readonly")
        opacity.pack(side="left")
        opacity.bind("<<ComboboxSelected>>", lambda _e: self.set_opacity())
        bind_tooltip(opacity, "Highlight opacity (%)", p)
        search_row = ttk.Frame(panel)
        search_row.grid(row=3, column=0, sticky="ew")
        ttk.Label(search_row, text="Search").pack(side="left", padx=(0, 6))
        search = ttk.Entry(search_row, textvariable=self.query)
        search.pack(side="left", fill="x", expand=True)
        bind_tooltip(search, "Search quoted text, comments, page or annotation type", p)
        filters = ttk.Frame(panel)
        filters.grid(row=4, column=0, sticky="ew", pady=6)
        for label in ("All", "Comments", "Highlights"):
            ttk.Radiobutton(filters, text=label, value=label, variable=self.kind,
                            command=self._filter).pack(side="left")
        listing = ttk.Frame(panel)
        listing.grid(row=5, column=0, sticky="nsew")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=("page", "text"), show="headings",
                                 selectmode="browse", height=7)
        self.tree.heading("page", text="Page")
        self.tree.heading("text", text="Quote / comment")
        self.tree.column("page", width=52, stretch=False)
        self.tree.column("text", width=220, minwidth=100)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Double-1>", lambda _e: self.edit_comment())
        self.count = ttk.Label(panel, text="")
        self.count.grid(row=6, column=0, sticky="w", pady=6)
        self.preview = tk.Text(panel, height=5, wrap="word", background=p.panel_alt,
                               foreground=p.text, relief="flat", padx=8, pady=8,
                               state="disabled")
        self.preview.grid(row=7, column=0, sticky="ew")
        actions = ttk.Frame(panel)
        actions.grid(row=8, column=0, sticky="ew", pady=8)
        for label, command in (("Edit comment", self.edit_comment),
                               ("Delete", self.delete_selected),
                               ("CSV", self.export_csv)):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 4))
        history = ttk.Frame(panel)
        history.grid(row=9, column=0, sticky="ew")
        for label, command in (("Undo", self.edit.undo), ("Redo", self.edit.redo),
                               ("Save PDF", self.edit.save)):
            ttk.Button(history, text=label, command=command).pack(side="left", padx=(0, 4))
        self.query.trace_add("write", lambda *_: self._filter())

    def refresh(self):
        """Index in cancellable batches, so long documents leave the UI responsive."""
        if self.panel is None or not self.panel.winfo_manager():
            return
        if self._job is not None:
            self.window.after_cancel(self._job)
        self._generation += 1
        generation = self._generation
        session = self.edit.session
        selected = self.selected_info()
        self._restore_key = (f"{selected.page}:{selected.uid}" if selected else None)
        self.rows = []
        self._filter()
        if session is None:
            self._job = None
            return
        self.count.configure(text="Indexing annotations...")

        def batch(page=0):
            """Only the current document/generation may update the visible list."""
            self._job = None
            if generation != self._generation or session is not self.edit.session:
                return
            end = min(page + INDEX_BATCH, session.document.page_count)
            for index in range(page, end):
                self.rows.extend(session.annots_on(index))
            if end < session.document.page_count:
                self._job = self.window.after(1, lambda: batch(end))
            else:
                self._filter()
        self._job = self.window.after_idle(batch)

    def _filter(self):
        """Search the in-memory index without loading PDF pages again."""
        if self.panel is None:
            return
        previous = self.selected_info()
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._items = {}
        query = self.query.get().casefold().strip()
        for info in self.rows:
            if self.kind.get() == "Comments" and not info.contents.strip():
                continue
            if self.kind.get() == "Highlights" and info.kind != "highlight":
                continue
            if query not in f"{info.page + 1} {info.kind} {info.subject} {info.contents}".casefold():
                continue
            key = f"{info.page}:{info.uid}"
            self._items[key] = info
            label = (info.contents or info.subject or info.kind).replace("\n", " ")
            self.tree.insert("", "end", iid=key, values=(info.page + 1, label))
            if ((previous and previous.page == info.page and previous.uid == info.uid)
                    or key == self._restore_key):
                self.tree.selection_set(key)
        self.count.configure(text=f"{len(self._items)} / {len(self.rows)} annotations")
        if self.rows:
            self._restore_key = None
        self._show_preview(self.selected_info())

    def selected_info(self):
        """Return the stable annotation identity selected in the review list."""
        if self.panel is None:
            return None
        selection = self.tree.selection()
        return self._items.get(selection[0]) if selection else None

    def _show_preview(self, info):
        """Show complete text; users may select and copy even in read-only mode."""
        text = "Select an annotation to read or edit its comment."
        if info:
            text = f"Page {info.page + 1} | {info.kind}\n"
            if info.subject:
                text += "\n" + info.subject + "\n"
            text += "\n" + (info.contents or "No comment yet.")
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _selected(self, _event=None):
        """Navigate to and center the annotation without opening edit mode."""
        info = self.selected_info()
        self._show_preview(info)
        if info is None or self.edit.session is None:
            return
        window = self.window
        if window.page != info.page:
            window._set_document_page(info.page)
        self.edit._selected_uid = info.uid
        x0, y0, x1, y1 = self.edit.points_rect_to_canvas(info.rect)
        width, height = window._canvas_size()
        window._pan_by((x0 + x1 - width) / 2, (y0 + y1 - height) / 2)
        window.redraw_now()

    def edit_comment(self):
        """Edit the PDF annotation contents through the shared undo stack."""
        info = self.selected_info()
        if info is None or self.edit.session is None:
            return
        value = DLG.ask_text(self.window, self.palette, "Edit comment", "Comment:",
                             info.contents, multiline=True)
        if value is not None and value != info.contents:
            self.edit.session.set_annot_props(info.page, info.uid, contents=value)

    def delete_selected(self):
        """Delete only the selected annotation; Undo restores it."""
        info = self.selected_info()
        if info is not None and self.edit.session is not None:
            self.edit.session.delete_annot(info.page, info.uid)

    def set_color(self, color):
        """Persist the default and optionally recolor the selected markup."""
        from .editui import MARKUP_TOOLS, _hex_to_rgb
        self.edit.app.settings.setdefault("annot_defaults", {})["highlight_color"] = color
        info = self.selected_info()
        if info and info.kind in MARKUP_TOOLS and self.edit.session is not None:
            self.edit.session.set_annot_props(info.page, info.uid, stroke=_hex_to_rgb(color))
        self.window.toast("Highlight color updated")

    def set_opacity(self):
        """Apply the chosen opacity to subsequent and selected highlights."""
        opacity = float(self.opacity.get()) / 100
        self.edit.app.settings.setdefault("annot_defaults", {})["markup_opacity"] = opacity
        info = self.selected_info()
        if info and info.kind == "highlight" and self.edit.session is not None:
            self.edit.session.set_annot_props(info.page, info.uid, opacity=opacity)

    def export_csv(self):
        """Export the filtered review list with Unicode and spreadsheet-safe cells."""
        if self._job is not None:
            self.window.toast("Indexing annotations; export again when the list is ready")
            return
        path = filedialog.asksaveasfilename(parent=self.window, title="Export review",
                                           defaultextension=".csv",
                                           filetypes=[("CSV", "*.csv")])
        if not path:
            return
        def safe(value):
            """Prevent quoted document content being interpreted as a formula."""
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("page", "type", "quote", "comment"))
                for info in self._items.values():
                    writer.writerow((info.page + 1, info.kind, safe(info.subject),
                                     safe(info.contents)))
        except OSError as exc:
            messagebox.showerror("Export review", str(exc), parent=self.window)
