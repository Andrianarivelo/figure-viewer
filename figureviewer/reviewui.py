"""Direct text review tools backed by ordinary, undoable PDF annotations.

Selection coordinates are captured before acquiring an edit session, because
that operation replaces the document and invalidates the text layer. The review
list is indexed in small Tk batches and never performs a full scan per keystroke.
"""
from __future__ import annotations

import csv
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import dialogs as DLG
from . import docs as D
from .theme import bind_tooltip, toolbar_button

# User-facing defaults and layout settings live together for easy adjustment.
REVIEW_WIDTH = 410
INDEX_BATCH = 12
COLORS = (("Yellow", "#ffd54a"), ("Green", "#8bd450"),
          ("Blue", "#5aa7f0"), ("Pink", "#f28cba"),
          ("Purple", "#b06ad4"))
DEFAULT_OPACITY = 0.35
PREVIEW_HEIGHT = 8
COMPOSER_COMPACT_HEIGHT = 600


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
        self._press = None
        self._threads = {}
        self._roots = {}
        # App-owned transient drafts survive window/theme rebuilds, but are
        # intentionally never written to settings or the PDF until posted.
        if not hasattr(edit.app, "review_drafts"):
            edit.app.review_drafts = {}
        self._drafts = edit.app.review_drafts
        self._reply_target = None

    def on_press(self, event):
        """Remember a reading click, without taking ownership of text selection."""
        self._press = None
        if (self.edit.available() and not self.edit.active
                and not self.window.snip_mode.get() and not self.mode.get()
                and not event.state & 0x0005):
            self._press = (self.window.document, self.window.page, event.x, event.y)

    def cancel_click(self):
        """Any drag belongs to text selection or pan, even if it returns to its start."""
        self._press = None

    def on_release(self, event):
        """Read an existing PDF comment only after an ordinary stationary click."""
        press, self._press = self._press, None
        if press is None:
            return
        document, page_index, x, y = press
        if (document is not self.window.document or page_index != self.window.page
                or abs(event.x - x) > 4 or abs(event.y - y) > 4):
            return
        point = D.fitz.Point(*self.edit.canvas_to_points(event.x, event.y))
        uid = None
        # Inspect the current PDF before acquiring a private edit session. A
        # click on ordinary text must not copy the PDF or create edit history.
        with document._lock:
            page = document._doc[page_index]
            for annot in page.annots() or ():
                # Replies are represented by the parent passage, never by an
                # extra page icon, including imported PDF-native threads.
                if (document._doc.xref_get_key(annot.xref, "IRT")[0] == "xref"
                        and document._doc.xref_get_key(annot.xref, "RT") != ("name", "/Group")):
                    continue
                if not (annot.info or {}).get("content", "").strip():
                    continue
                if point not in annot.rect:
                    continue
                # Multiline markup rectangles include blank space between
                # lines. Test each text quad so that space remains pannable.
                if annot.type[0] in (D.fitz.PDF_ANNOT_HIGHLIGHT,
                                     D.fitz.PDF_ANNOT_UNDERLINE,
                                     D.fitz.PDF_ANNOT_STRIKE_OUT,
                                     D.fitz.PDF_ANNOT_SQUIGGLY):
                    vertices = annot.vertices or []
                    if not any(point in D.fitz.Quad(vertices[i:i + 4])
                               for i in range(0, len(vertices), 4)):
                        continue
                uid = (annot.info or {}).get("id") or f"xref:{annot.xref}"
        if uid is not None:
            self.query.set("")
            self.kind.set("All")
            self.open_panel()
            self._restore_key = f"{page_index}:{uid}"

    def build_toolbar(self, parent):
        """Keep the three common review actions beside the existing Edit button."""
        # ``parent`` is the viewer's edit group: Edit, Highlight, Comment and
        # Review read as one raised cluster.
        group = parent
        for mode, title, icon, tip in (
                ("highlight", "Highlight", "highlighter",
                 "Highlight selected text, or enable repeated drag highlighting (Ctrl+Alt+H)"),
                ("comment", "Comment", "note",
                 "Attach a comment to selected text (Ctrl+Alt+M)")):
            # Icon-only; the title survives in the tooltip. Only the two
            # modal states of the viewer (Snip, Edit) keep a text label.
            button = ttk.Checkbutton(
                group, image=self.window.icons.get(icon),
                compound="image", variable=self.mode, onvalue=mode, offvalue="",
                style="Group.Modal.TCheckbutton", takefocus=False,
                command=lambda m=mode: self.action(m, toggled=True))
            button.pack(side="left")
            bind_tooltip(button, f"{title}: {tip}", self.palette)
            self._buttons.append(button)
        button = toolbar_button(group, "", self.toggle_panel, self.palette,
                                "Review: search, edit and export all PDF comments "
                                "(Ctrl+Alt+R)", style="Group.Tool.TButton",
                                image=self.window.icons.get("select"), compound="image")
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
        self._store_draft()
        self._reply_target = None
        if self.panel is not None:
            self.composer.grid_remove()
        self._cancel_index()
        self.cancel_click()
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
        """Build an adjustable conversation area and an inline reply composer."""
        p = self.palette
        panel = ttk.Frame(self.window.stage, width=REVIEW_WIDTH,
                          style="Card.TFrame", padding=12)
        panel.grid_propagate(False)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        self.panel = panel
        panel.bind("<Destroy>", lambda event: self._cancel_index()
                   if event.widget is panel else None, add="+")
        panel.bind("<Configure>", lambda _event: self._layout_composer(), add="+")
        header = ttk.Frame(panel)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(header, text="Review", font="FVtitle").pack(side="left")
        self.count = ttk.Label(header, text="", style="Muted.TLabel")
        self.count.pack(side="left", padx=10)
        ttk.Button(header, text="Close", command=self.close_panel).pack(side="right")
        self.search_row = ttk.Frame(panel)
        self.search_row.grid(row=1, column=0, sticky="ew")
        ttk.Label(self.search_row, text="Search", style="Muted.TLabel").pack(
            side="left", padx=(0, 8))
        search = ttk.Entry(self.search_row, textvariable=self.query)
        search.pack(side="left", fill="x", expand=True)
        bind_tooltip(search, "Search comments, replies, authors and quoted text", p)
        filters = ttk.Frame(panel)
        self.filters = filters
        filters.grid(row=2, column=0, sticky="ew", pady=6)
        for label in ("All", "Comments", "Highlights"):
            ttk.Radiobutton(filters, text=label, value=label, variable=self.kind,
                            command=self._filter).pack(side="left")
        # A sash lets each reviewer choose how much room the list and the
        # conversation need. Both areas remain independently scrollable.
        self.split = ttk.Panedwindow(panel, orient="vertical")
        self.split.grid(row=3, column=0, sticky="nsew")
        listing = ttk.Frame(self.split)
        self.listing = listing
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=("page",), show="tree headings",
                                 selectmode="browse", height=3)
        self.tree.heading("#0", text="Comments and replies")
        self.tree.heading("page", text="Page")
        self.tree.column("#0", width=REVIEW_WIDTH - 100, minwidth=130)
        self.tree.column("page", width=48, minwidth=40, stretch=False, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Double-1>", lambda _e: self.edit_comment())
        self.split.add(listing, weight=1)
        preview_frame = ttk.Frame(self.split)
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.preview = tk.Text(preview_frame, height=PREVIEW_HEIGHT, width=1,
                               wrap="word", background=p.panel_alt,
                               foreground=p.text, font="FVui", relief="flat", padx=12, pady=10,
                               state="disabled", cursor="xterm")
        self.preview.grid(row=0, column=0, sticky="nsew")
        preview_scroll = ttk.Scrollbar(preview_frame, command=self.preview.yview)
        preview_scroll.grid(row=0, column=1, sticky="ns")
        self.preview.configure(yscrollcommand=preview_scroll.set)
        self.preview.tag_configure("author", font="FVuiBold", foreground=p.accent,
                                   spacing1=8, spacing3=3)
        self.preview.tag_configure("meta", font="FVsmall", foreground=p.muted, spacing3=5)
        self.preview.tag_configure("body", spacing3=10)
        self.preview.tag_configure("quote", foreground=p.muted, lmargin1=8, lmargin2=8)
        self.preview.tag_configure("chosen", background=p.select)
        self.split.add(preview_frame, weight=3)
        actions = ttk.Frame(panel)
        actions.grid(row=4, column=0, sticky="ew", pady=8)
        self.reply_button = ttk.Button(actions, text="Reply", command=self.start_reply)
        self.reply_button.pack(side="left", padx=(0, 4))
        self.edit_button = ttk.Button(actions, text="Edit", command=self.edit_comment)
        self.edit_button.pack(side="left", padx=(0, 4))
        self.delete_button = ttk.Button(actions, text="Delete", command=self.delete_selected)
        self.delete_button.pack(side="left", padx=(0, 4))
        bind_tooltip(self.delete_button, "Delete this message and its replies; Undo restores them.", p)
        more = ttk.Menubutton(actions, text="More")
        more.pack(side="right")
        menu = tk.Menu(more, tearoff=False)
        from .theme import style_menu
        style_menu(menu, p)
        color_menu = tk.Menu(menu, tearoff=False)
        style_menu(color_menu, p)
        for name, color in COLORS:
            color_menu.add_command(label=name, command=lambda c=color: self.set_color(c))
        menu.add_cascade(label="Highlight color", menu=color_menu)
        self.opacity = tk.StringVar(master=panel, value=str(round(
            float(self.edit._defaults().get("markup_opacity", DEFAULT_OPACITY)) * 100)))
        opacity_menu = tk.Menu(menu, tearoff=False)
        style_menu(opacity_menu, p)
        for value in ("20", "35", "50", "75", "100"):
            opacity_menu.add_radiobutton(label=value + "%", variable=self.opacity,
                                         value=value, command=self.set_opacity)
        menu.add_cascade(label="Highlight opacity", menu=opacity_menu)
        menu.add_separator()
        menu.add_command(label="Export review CSV", command=self.export_csv)
        more.configure(menu=menu)
        self.composer = ttk.Frame(panel, padding=8, style="Card.TFrame")
        self.composer.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        self.composer.columnconfigure(1, weight=1)
        ttk.Label(self.composer, text="Your name", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.author = tk.StringVar(master=panel,
                                   value=self.edit.app.settings.get("review_author", ""))
        ttk.Entry(self.composer, textvariable=self.author).grid(
            row=0, column=1, sticky="ew")
        self.reply_text = tk.Text(self.composer, height=2, width=1, wrap="word",
                                 background=p.panel_alt, foreground=p.text,
                                 insertbackground=p.text, font="FVui", relief="flat",
                                 padx=8, pady=6, undo=True)
        self.reply_text.grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        self.reply_text.bind("<Control-Return>", self.post_reply)
        self.reply_text.bind("<KeyRelease>", lambda _e: self._store_draft())
        posting = ttk.Frame(self.composer)
        posting.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.post_button = ttk.Button(posting, text="Post reply", command=self.post_reply)
        self.post_button.pack(side="left")
        ttk.Button(posting, text="Cancel", command=self.cancel_reply).pack(side="left", padx=5)
        bind_tooltip(self.post_button, "Add a reply to this comment (Ctrl+Enter). Save PDF to persist.", p)
        self.composer.grid_remove()
        history = ttk.Frame(panel)
        history.grid(row=6, column=0, sticky="ew")
        for label, command in (("Undo", self.edit.undo), ("Redo", self.edit.redo),
                               ("Save PDF", self.edit.save)):
            ttk.Button(history, text=label, command=command).pack(side="left", padx=(0, 4))
        self.query.trace_add("write", lambda *_: self._filter())
        self._show_preview(None)

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
        self._threads = {}
        self._roots = {}
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
                self._index_threads()
                self._filter()
        self._job = self.window.after_idle(batch)

    @staticmethod
    def _key(info):
        """PDF annotation identifiers are unique only within their page."""
        return f"{info.page}:{info.uid}"

    def _index_threads(self):
        """Group replies under their root, retaining malformed imports visibly.

        A local parent map makes indexing linear for normal conversations.
        Cyclic imported references become independent visible roots rather than
        disappearing from the list or causing unbounded recursion.
        """
        by_key = {self._key(info): info for info in self.rows}
        roots = {}
        for key in by_key:
            trail, seen = [], set()
            current = key
            while current not in roots:
                if current in seen:
                    for member in trail:
                        roots[member] = member
                    break
                seen.add(current)
                trail.append(current)
                info = by_key[current]
                parent = f"{info.page}:{info.parent_uid}"
                if not info.parent_uid or parent not in by_key:
                    roots[current] = current
                    break
                current = parent
            root = roots.get(current, key)
            for member in reversed(trail):
                roots.setdefault(member, root)
        self._roots = roots
        self._threads = {}
        for key, info in by_key.items():
            self._threads.setdefault(roots[key], []).append(info)
        # Keep the parent first even if a foreign PDF stores replies before it.
        for root, messages in self._threads.items():
            messages.sort(key=lambda info: (self._key(info) != root, info.created, info.xref))

    def _filter(self):
        """Search whole conversations; reply matches retain their parent context."""
        if self.panel is None:
            return
        previous = self.selected_info()
        expanded = {key for key in self.tree.get_children() if self.tree.item(key, "open")}
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._items = {}
        query = self.query.get().casefold().strip()
        visible_threads = 0
        for root, messages in self._threads.items():
            info = messages[0]
            if self.kind.get() == "Comments" and not any(m.contents.strip() for m in messages):
                continue
            if self.kind.get() == "Highlights" and info.kind != "highlight":
                continue
            haystack = " ".join(f"{m.page + 1} {m.kind} {m.author} {m.subject} {m.contents}"
                                for m in messages).casefold()
            if query not in haystack:
                continue
            visible_threads += 1
            for index, message in enumerate(messages):
                key = self._key(message)
                self._items[key] = message
                label = (message.contents or message.subject or message.kind).replace("\n", " ")
                if index == 0 and len(messages) > 1:
                    replies = len(messages) - 1
                    label = f"[{replies} {'reply' if replies == 1 else 'replies'}] " + label
                elif index:
                    label = (message.author or "Reply") + ": " + label
                self.tree.insert("" if index == 0 else root, "end", iid=key,
                                 text=label, values=(message.page + 1,),
                                 open=bool(query) or root in expanded)
                if ((previous and self._key(previous) == key) or key == self._restore_key):
                    self.tree.selection_set(key)
                    if index:
                        self.tree.item(root, open=True)
                    self.tree.see(key)
        total = len(self._threads)
        self.count.configure(text=f"{visible_threads} / {total} threads")
        if self.rows:
            self._restore_key = None
        self._show_preview(self.selected_info())

    def selected_info(self):
        """Return the stable annotation identity selected in the review list."""
        if self.panel is None:
            return None
        selection = self.tree.selection()
        return self._items.get(selection[0]) if selection else None

    @staticmethod
    def _date_label(value):
        """Format a PDF creation date without guessing missing timezone details."""
        try:
            date = datetime.strptime(value.removeprefix("D:")[:12], "%Y%m%d%H%M").strftime(
                "%d %b %Y, %H:%M")
            return date + (" UTC" if value.endswith("Z") else "")
        except ValueError:
            return ""

    def _show_preview(self, info):
        """Render the full conversation with separate author and message styles."""
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        if info is None:
            self.preview.insert("end", "Select a comment to read its conversation.\n\n"
                                "Click Reply to add your response.\nSave PDF to keep your changes.", "meta")
        else:
            root = self._roots.get(self._key(info), self._key(info))
            messages = self._threads.get(root, [info])
            chosen_start = "1.0"
            for index, message in enumerate(messages):
                start = self.preview.index("end-1c")
                heading = "COMMENT" if index == 0 else "REPLY"
                self.preview.insert("end", f"{heading} | By {message.author or 'Unnamed reviewer'}\n", "author")
                date = self._date_label(message.created)
                if date:
                    self.preview.insert("end", date + "\n", "meta")
                self.preview.insert("end", (message.contents or "No comment yet.") + "\n", "body")
                if self._key(message) == self._key(info):
                    chosen_start = start
                    if index:
                        self.preview.tag_add("chosen", start, "end-1c")
            root_info = messages[0]
            self.preview.insert("end", f"\nPage {root_info.page + 1} | {root_info.kind}\n", "meta")
            if root_info.subject:
                self.preview.insert("end", "Quoted text\n", "meta")
                self.preview.insert("end", root_info.subject, "quote")
            self.preview.see(chosen_start)
            self.preview.yview(chosen_start)
        self.preview.configure(state="disabled")
        for button in (self.reply_button, self.edit_button, self.delete_button):
            button.state(["!disabled"] if info is not None else ["disabled"])
        thread = self._threads.get(self._roots.get(self._key(info)), []) if info else []
        self.delete_button.configure(text="Delete thread" if len(thread) > 1
                                     and thread[0].uid == info.uid else "Delete")
        self.post_button.state(["!disabled"] if info is not None else ["disabled"])
        if info is not None and self.composer.winfo_manager():
            self._load_draft(info)

    def _draft_key(self, key):
        """Keep in-memory drafts scoped to their actual file and annotation."""
        return (str(self.window.current.path), key) if self.window.current else None

    def _store_draft(self):
        """Never let an asynchronous refresh erase the text being composed."""
        if self._reply_target is not None:
            self._drafts[self._reply_target] = self.reply_text.get("1.0", "end-1c")

    def _load_draft(self, info):
        """Switch composers safely when the reviewer selects another message."""
        target = self._draft_key(self._key(info))
        if target == self._reply_target:
            return
        self._store_draft()
        self._reply_target = target
        self.reply_text.delete("1.0", "end")
        self.reply_text.insert("1.0", self._drafts.get(target, ""))
        self.reply_text.edit_reset()

    def start_reply(self):
        """Show an inline composer for the selected comment or nested reply."""
        info = self.selected_info()
        if info is None:
            return
        self.composer.grid()
        self._layout_composer()
        self._load_draft(info)
        self.reply_text.focus_set()

    def cancel_reply(self):
        """Collapse the composer, keeping its draft until the document closes."""
        self._store_draft()
        self.composer.grid_remove()
        self._layout_composer()

    def _layout_composer(self):
        """On short windows give the conversation room while writing a reply."""
        if not hasattr(self, "composer"):
            return
        compact = (self.composer.winfo_manager()
                   and self.panel.winfo_height() < COMPOSER_COMPACT_HEIGHT)
        listed = str(self.listing) in self.split.panes()
        for widget in (self.search_row, self.filters):
            if compact:
                widget.grid_remove()
            else:
                widget.grid()
        if compact and listed:
            self.split.forget(self.listing)
        elif not compact and not listed:
            self.split.insert(0, self.listing, weight=1)

    def post_reply(self, _event=None):
        """Add a PDF-native reply in one undo step; keep drafts on any failure."""
        info = self.selected_info()
        if (info is None or self.edit.session is None
                or self._reply_target != self._draft_key(self._key(info))):
            return "break"
        value = self.reply_text.get("1.0", "end-1c").strip()
        if not value:
            self.window.toast("Write a reply before posting")
            return "break"
        try:
            uid = self.edit.session.add_reply(info.page, info.uid, value, self.author.get().strip())
        except Exception as exc:
            messagebox.showerror("Reply to comment", str(exc), parent=self.window)
            return "break"
        self.edit.app.settings["review_author"] = self.author.get().strip()
        self._drafts.pop(self._reply_target, None)
        self._reply_target = None
        self.reply_text.delete("1.0", "end")
        self.composer.grid_remove()
        self._layout_composer()
        self.query.set("")
        self._restore_key = f"{info.page}:{uid}"
        self.window.toast("Reply added. Save PDF to keep it.")
        return "break"

    def _selected(self, _event=None):
        """Navigate to and center the annotation without opening edit mode."""
        info = self.selected_info()
        self._show_preview(info)
        if info is None or self.edit.session is None:
            return
        window = self.window
        root = self._roots.get(self._key(info), self._key(info))
        passage = self._threads.get(root, [info])[0]
        if window.page != passage.page:
            window._set_document_page(passage.page)
        self.edit._selected_uid = passage.uid
        x0, y0, x1, y1 = self.edit.points_rect_to_canvas(passage.rect)
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
            self._store_draft()
            self.composer.grid_remove()
            self._reply_target = None
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
                writer.writerow(("page", "type", "quote", "comment", "author",
                                 "id", "parent_id", "created"))
                for info in self._items.values():
                    writer.writerow((info.page + 1, info.kind, safe(info.subject),
                                     safe(info.contents), safe(info.author),
                                     safe(info.uid), safe(info.parent_uid), safe(info.created)))
        except OSError as exc:
            messagebox.showerror("Export review", str(exc), parent=self.window)
