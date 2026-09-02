"""Themed dialogs for the PDF studio.

Every dialog here is built on :class:`figureviewer.theme.ThemedDialog` so the
whole editing surface matches the application palette in dark and light. The
rules of the house:

* dialogs collect input and return it from ``wait()``; they never mutate the
  session or the settings themselves (the controller does, so undo grouping
  and persistence stay in one place);
* passphrases are returned as ``bytes``, shown masked, and never stored;
* every long-running action goes through :func:`run_with_progress`, which
  pairs a ThemedProgressDialog with the shared worker pool and a cancel
  event, so the UI thread never blocks.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable, Optional

from .theme import (Palette, ThemedDialog, ThemedProgressDialog, bind_tooltip,
                    style_menu)


# ---------------------------------------------------------------------------
# Small generic dialogs


def ask_save_discard_cancel(parent, palette: Palette, filename: str) -> str:
    """Return ``'save'``, ``'discard'`` or ``'cancel'`` for unsaved edits."""
    dialog = ThemedDialog(parent, palette, "Unsaved changes")
    body = dialog.body
    ttk.Label(body, text=f"{filename} has unsaved edits.",
              style="TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(body, text="Save them before continuing?",
              style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 12))
    row = ttk.Frame(body)
    row.grid(row=2, column=0, sticky="e")

    def finish(choice: str):
        dialog.result = choice
        dialog.destroy()

    ttk.Button(row, text="Save", style="Accent.TButton",
               command=lambda: finish("save")).pack(side="left", padx=(0, 6))
    ttk.Button(row, text="Discard", style="Danger.TButton",
               command=lambda: finish("discard")).pack(side="left", padx=(0, 6))
    ttk.Button(row, text="Cancel",
               command=lambda: finish("cancel")).pack(side="left")
    dialog.bind("<Return>", lambda _e: finish("save"))
    dialog.bind("<Escape>", lambda _e: finish("cancel"))
    result = dialog.wait()
    return result if result in ("save", "discard") else "cancel"


def ask_text(parent, palette: Palette, title: str, prompt: str,
             initial: str = "", *, multiline: bool = False) -> Optional[str]:
    """A themed text prompt; returns None on cancel."""
    dialog = ThemedDialog(parent, palette, title)
    body = dialog.body
    ttk.Label(body, text=prompt).grid(row=0, column=0, sticky="w", pady=(0, 6))
    if multiline:
        widget = tk.Text(body, width=46, height=5, background=palette.panel_alt,
                         foreground=palette.text, insertbackground=palette.text,
                         relief="flat", highlightthickness=1,
                         highlightbackground=palette.border, font="FVui")
        widget.insert("1.0", initial)
        get = lambda: widget.get("1.0", "end-1c")  # noqa: E731
    else:
        var = tk.StringVar(value=initial)
        widget = ttk.Entry(body, textvariable=var, width=46)
        get = var.get
    widget.grid(row=1, column=0, sticky="ew", pady=(0, 12))
    widget.focus_set()

    def accept(_e=None):
        dialog.result = get()
        dialog.destroy()
        return "break"

    row = ttk.Frame(body)
    row.grid(row=2, column=0, sticky="e")
    ttk.Button(row, text="OK", style="Accent.TButton", command=accept).pack(
        side="left", padx=(0, 6))
    ttk.Button(row, text="Cancel", command=dialog.destroy).pack(side="left")
    if multiline:
        widget.bind("<Control-Return>", accept)
        # The base class binds Return to accept-with-True; inside a multiline
        # Text that keystroke must stay a newline.
        dialog.bind("<Return>", lambda _e: None)
    else:
        widget.bind("<Return>", accept)
    return dialog.wait()


def ask_passphrase(parent, palette: Palette, title: str,
                   prompt: str, *, confirm: bool = False) -> Optional[bytes]:
    """Masked passphrase prompt. Returns bytes, or None on cancel."""
    dialog = ThemedDialog(parent, palette, title)
    body = dialog.body
    ttk.Label(body, text=prompt).grid(row=0, column=0, columnspan=2,
                                      sticky="w", pady=(0, 8))
    first = tk.StringVar()
    second = tk.StringVar()
    ttk.Label(body, text="Passphrase", style="Muted.TLabel").grid(
        row=1, column=0, sticky="w", padx=(0, 8))
    entry = ttk.Entry(body, textvariable=first, show="•", width=32)
    entry.grid(row=1, column=1, sticky="ew", pady=2)
    if confirm:
        ttk.Label(body, text="Repeat", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(body, textvariable=second, show="•", width=32).grid(
            row=2, column=1, sticky="ew", pady=2)
    error = ttk.Label(body, text="", style="Muted.TLabel",
                      foreground=palette.danger)
    error.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 8))
    entry.focus_set()

    def accept(_e=None):
        if confirm and first.get() != second.get():
            error.configure(text="The passphrases do not match.")
            return "break"
        if not first.get():
            error.configure(text="A passphrase is required.")
            return "break"
        dialog.result = first.get().encode("utf-8")
        dialog.destroy()
        return "break"

    row = ttk.Frame(body)
    row.grid(row=4, column=0, columnspan=2, sticky="e")
    ttk.Button(row, text="OK", style="Accent.TButton", command=accept).pack(
        side="left", padx=(0, 6))
    ttk.Button(row, text="Cancel", command=dialog.destroy).pack(side="left")
    dialog.bind("<Return>", accept)
    return dialog.wait()


def run_with_progress(window, palette: Palette, title: str,
                      job: Callable, done: Callable, *,
                      cancellable: bool = True,
                      failed: Optional[Callable] = None) -> ThemedProgressDialog:
    """Run ``job(progress_cb, cancel_event)`` on the worker pool with a dialog.

    ``progress_cb(done, total, text)`` may be called from the worker thread;
    it is marshalled onto the UI thread via ``window.after``. The dialog is
    closed before ``done``/``failed`` run.
    """
    dialog = ThemedProgressDialog(window, palette, title, cancellable=cancellable)

    def progress(done_count: int, total: int, text: str = "") -> None:
        try:
            window.after(0, lambda: dialog.update_progress(done_count, total, text))
        except Exception:
            pass

    def work():
        return job(progress, dialog.cancel_event)

    def finish(result):
        dialog.close()
        done(result)

    def fail(exc):
        dialog.close()
        if failed is not None:
            failed(exc)
        else:
            from tkinter import messagebox
            messagebox.showerror(title, str(exc), parent=window)

    window.app.worker.submit(work, finish, fail)
    return dialog


# ---------------------------------------------------------------------------
# Export


class ExportDialog(ThemedDialog):
    """Choose an export format, page range and destination.

    ``formats`` is the pdfexport FORMATS registry; unavailable formats stay
    visible with a muted hint naming the missing package, which is more honest
    than hiding them.
    """

    def __init__(self, parent, palette: Palette, settings: dict, formats: dict,
                 *, source_name: str, page_count: int, wants_ocr: bool) -> None:
        super().__init__(parent, palette, "Export document")
        self.settings = settings
        self.formats = formats
        prefs = settings.get("export_prefs", {})
        body = self.body
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text=f"Export {source_name}",
                  style="TLabel").grid(row=0, column=0, columnspan=2,
                                       sticky="w", pady=(0, 10))

        self.format_var = tk.StringVar(value=prefs.get("last_format", "docx"))
        holder = ttk.Frame(body, style="Card.TFrame", padding=6)
        holder.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        available_names = []
        for row_index, (name, spec) in enumerate(formats.items()):
            ok, hint = spec.probe()
            radio = ttk.Radiobutton(holder, text=spec.label, value=name,
                                    variable=self.format_var, takefocus=False)
            radio.grid(row=row_index, column=0, sticky="w", pady=1)
            if ok:
                available_names.append(name)
            else:
                radio.state(["disabled"])
                ttk.Label(holder, text=hint, style="Muted.TLabel").grid(
                    row=row_index, column=1, sticky="w", padx=(12, 0))
        if self.format_var.get() not in available_names and available_names:
            self.format_var.set(available_names[0])

        ttk.Label(body, text="Pages", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 10))
        self.range_var = tk.StringVar(value=prefs.get("range", "all"))
        range_entry = ttk.Entry(body, textvariable=self.range_var, width=18)
        range_entry.grid(row=2, column=1, sticky="w")
        bind_tooltip(range_entry,
                     f"'all', or ranges like 1-3,7 (1 to {page_count})", palette)

        self.ocr_var = tk.BooleanVar(
            value=bool(prefs.get("ocr_missing_text", True)) and wants_ocr)
        ocr_check = ttk.Checkbutton(
            body, text="OCR pages without a text layer first",
            variable=self.ocr_var, style="Toggle.TCheckbutton", takefocus=False)
        ocr_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        if not wants_ocr:
            ocr_check.state(["disabled"])

        row = ttk.Frame(body)
        row.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(row, text="Export…", style="Accent.TButton",
                   command=self._choose_target).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="left")
        self.source_name = source_name
        self.page_count = page_count

    def accept(self, _event=None) -> None:
        """Return key routes to the primary action instead of a bare close."""
        self._choose_target()

    def _choose_target(self) -> None:
        name = self.format_var.get()
        spec = self.formats.get(name)
        if spec is None:
            return
        stem = Path(self.source_name).stem
        if name == "images":
            target = filedialog.askdirectory(
                parent=self, title="Export page images into folder")
        else:
            target = filedialog.asksaveasfilename(
                parent=self, title=f"Export as {spec.label}",
                initialfile=stem + spec.suffix, defaultextension=spec.suffix,
                filetypes=[(spec.label, "*" + spec.suffix)])
        if not target:
            return
        pages = _parse_range(self.range_var.get(), self.page_count)
        if pages is _RANGE_ERROR:
            from tkinter import messagebox
            messagebox.showerror("Export", "Page range not understood. "
                                 "Use 'all' or e.g. 1-3,7.", parent=self)
            return
        self.result = {"format": name, "path": Path(target), "pages": pages,
                       "ocr": bool(self.ocr_var.get()),
                       "range_text": self.range_var.get().strip() or "all"}
        self.destroy()


_RANGE_ERROR = object()


def _parse_range(text: str, page_count: int):
    """'all' -> None; '1-3,7' -> zero-based sorted list; garbage -> sentinel."""
    text = (text or "").strip().lower()
    if text in ("", "all"):
        return None
    pages: set[int] = set()
    try:
        for chunk in text.split(","):
            chunk = chunk.strip()
            if "-" in chunk:
                start, _, end = chunk.partition("-")
                first, last = int(start), int(end)
                if first > last:
                    first, last = last, first
                pages.update(range(first - 1, last))
            else:
                pages.add(int(chunk) - 1)
    except ValueError:
        return _RANGE_ERROR
    pages = {p for p in pages if 0 <= p < page_count}
    return sorted(pages) if pages else _RANGE_ERROR


# ---------------------------------------------------------------------------
# Signatures


class SignatureCaptureDialog(ThemedDialog):
    """Draw a signature with the mouse; returns a list of strokes.

    Strokes are recorded in a 640 x 220 box and handed to
    ``SignatureLibrary.add_drawn`` untouched; the library supersamples and
    trims, so pixel coordinates are fine here.
    """

    def __init__(self, parent, palette: Palette) -> None:
        super().__init__(parent, palette, "Draw signature")
        body = self.body
        ttk.Label(body, text="Sign below with the mouse or a pen.",
                  style="Muted.TLabel").grid(row=0, column=0, sticky="w",
                                             pady=(0, 6))
        self._canvas = tk.Canvas(body, width=640, height=220,
                                 background="#ffffff", highlightthickness=1,
                                 highlightbackground=palette.border, cursor="pencil")
        self._canvas.grid(row=1, column=0, sticky="ew")
        baseline = 170
        self._canvas.create_line(40, baseline, 600, baseline,
                                 fill="#b9c2cf", dash=(4, 4))
        self._strokes: list[list[tuple[float, float]]] = []
        self._current: Optional[list[tuple[float, float]]] = None
        self._canvas.bind("<ButtonPress-1>", self._start)
        self._canvas.bind("<B1-Motion>", self._extend)
        self._canvas.bind("<ButtonRelease-1>", self._finish)

        row = ttk.Frame(body)
        row.grid(row=2, column=0, sticky="e", pady=(10, 0))
        ttk.Button(row, text="Clear", command=self._clear).pack(
            side="left", padx=(0, 6))
        ttk.Button(row, text="Use signature", style="Accent.TButton",
                   command=self._accept).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="left")

    def accept(self, _event=None) -> None:
        self._accept()

    def _start(self, event) -> None:
        self._current = [(event.x, event.y)]

    def _extend(self, event) -> None:
        if self._current is None:
            return
        last = self._current[-1]
        self._current.append((event.x, event.y))
        self._canvas.create_line(last[0], last[1], event.x, event.y,
                                 fill="#16233b", width=3, capstyle="round",
                                 smooth=True, tags="ink")

    def _finish(self, _event) -> None:
        if self._current and len(self._current) > 1:
            self._strokes.append(self._current)
        self._current = None

    def _clear(self) -> None:
        self._strokes.clear()
        self._current = None
        self._canvas.delete("ink")

    def _accept(self) -> None:
        if not self._strokes:
            return
        self.result = self._strokes
        self.destroy()


class SignaturePickerDialog(ThemedDialog):
    """Pick a stored signature or create a new one.

    Returns ``{"id": ...}`` for a stored pick, or ``{"new": kind}`` where the
    controller then runs the capture flow, or None on cancel.
    """

    def __init__(self, parent, palette: Palette, library) -> None:
        super().__init__(parent, palette, "Signatures")
        body = self.body
        self._thumbs: list = []          # keep PhotoImages alive
        items = library.items()
        if items:
            ttk.Label(body, text="Your signatures",
                      style="Muted.TLabel").grid(row=0, column=0, sticky="w")
            holder = ttk.Frame(body, style="Card.TFrame", padding=6)
            holder.grid(row=1, column=0, sticky="ew", pady=(4, 10))
            for index, item in enumerate(items):
                row = ttk.Frame(holder, style="Card.TFrame")
                row.grid(row=index, column=0, sticky="ew", pady=2)
                photo = None
                try:
                    from PIL import ImageTk
                    photo = ImageTk.PhotoImage(library.thumbnail(item["id"], 34),
                                               master=self)
                    self._thumbs.append(photo)
                except Exception:
                    pass
                button = ttk.Button(
                    row, text=item.get("label", "Signature"), image=photo,
                    compound="left",
                    command=lambda i=item: self._pick(i["id"]))
                button.pack(side="left", fill="x", expand=True)
                remove = ttk.Button(row, text="×", width=2, style="Icon.TButton",
                                    command=lambda i=item: self._remove(library, i["id"]))
                remove.pack(side="left", padx=(6, 0))
                bind_tooltip(remove, "Remove this signature", palette)
        else:
            ttk.Label(body, text="No stored signatures yet.",
                      style="Muted.TLabel").grid(row=0, column=0, sticky="w",
                                                 pady=(0, 8))

        create = ttk.Frame(body)
        create.grid(row=2, column=0, sticky="ew")
        ttk.Label(create, text="New:", style="Muted.TLabel").pack(side="left",
                                                                  padx=(0, 8))
        for label, kind in (("Draw…", "drawn"), ("Type…", "typed"),
                            ("From image…", "image")):
            ttk.Button(create, text=label,
                       command=lambda k=kind: self._new(k)).pack(
                side="left", padx=(0, 6))
        ttk.Button(body, text="Cancel", command=self.destroy).grid(
            row=3, column=0, sticky="e", pady=(12, 0))

    def accept(self, _event=None) -> None:
        """Return alone is ambiguous here; picking needs an explicit click."""

    def _pick(self, signature_id: str) -> None:
        self.result = {"id": signature_id}
        self.destroy()

    def _new(self, kind: str) -> None:
        self.result = {"new": kind}
        self.destroy()

    def _remove(self, library, signature_id: str) -> None:
        library.remove(signature_id)
        self.result = {"changed": True}
        self.destroy()


class CertSignDialog(ThemedDialog):
    """Collect everything needed for a cryptographic (PAdES) signature."""

    def __init__(self, parent, palette: Palette, settings: dict,
                 *, default_out: Path, can_generate: bool) -> None:
        super().__init__(parent, palette, "Sign with certificate")
        body = self.body
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="Digitally sign the saved PDF with a PKCS#12 "
                             "certificate.").grid(row=0, column=0, columnspan=3,
                                                  sticky="w", pady=(0, 10))
        self.cert_var = tk.StringVar(value=settings.get("sign_certificate_path", ""))
        ttk.Label(body, text="Certificate", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(body, textvariable=self.cert_var).grid(row=1, column=1,
                                                         sticky="ew")
        ttk.Button(body, text="Browse…", command=self._browse).grid(
            row=1, column=2, padx=(6, 0))
        self.reason_var = tk.StringVar()
        ttk.Label(body, text="Reason", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Entry(body, textvariable=self.reason_var).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=(6, 0))
        self.out_var = tk.StringVar(value=str(default_out))
        ttk.Label(body, text="Output", style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Entry(body, textvariable=self.out_var).grid(
            row=3, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(body, text="…", width=3, command=self._browse_out).grid(
            row=3, column=2, padx=(6, 0), pady=(6, 0))
        note = ("The signed copy is written separately; the original stays "
                "as saved. Editing the signed copy later breaks its seal.")
        ttk.Label(body, text=note, style="Muted.TLabel", wraplength=420).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        row = ttk.Frame(body)
        row.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        if can_generate:
            ttk.Button(row, text="Create new certificate…",
                       command=self._generate).pack(side="left", padx=(0, 10))
        ttk.Button(row, text="Sign…", style="Accent.TButton",
                   command=self._accept).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="left")

    def accept(self, _event=None) -> None:
        self._accept()

    def _browse(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self, title="Choose a PKCS#12 certificate",
            filetypes=[("PKCS#12", "*.pfx *.p12"), ("All files", "*.*")])
        if chosen:
            self.cert_var.set(chosen)

    def _browse_out(self) -> None:
        chosen = filedialog.asksaveasfilename(
            parent=self, title="Signed copy", defaultextension=".pdf",
            initialfile=Path(self.out_var.get()).name,
            filetypes=[("PDF", "*.pdf")])
        if chosen:
            self.out_var.set(chosen)

    def _generate(self) -> None:
        self.result = {"generate": True}
        self.destroy()

    def _accept(self) -> None:
        cert = self.cert_var.get().strip()
        if not cert or not Path(cert).is_file():
            from tkinter import messagebox
            messagebox.showerror("Sign", "Choose an existing certificate file "
                                 "(.pfx / .p12), or create one.", parent=self)
            return
        self.result = {"pfx": Path(cert), "out": Path(self.out_var.get()),
                       "reason": self.reason_var.get().strip()}
        self.destroy()


# ---------------------------------------------------------------------------
# Identities and auto-fill


class IdentityPresetsDialog(ThemedDialog):
    """Edit the saved identity presets used by form auto-fill."""

    FIELD_ORDER = (
        ("full_name", "Full name"), ("first_name", "First name"),
        ("last_name", "Last name"), ("email", "E-mail"), ("phone", "Phone"),
        ("address1", "Address"), ("address2", "Address 2"), ("city", "City"),
        ("state", "State / region"), ("zip", "Postal code"),
        ("country", "Country"), ("company", "Company"), ("title", "Title"),
    )

    def __init__(self, parent, palette: Palette, store) -> None:
        super().__init__(parent, palette, "Identity presets")
        self.store = store
        self.palette = palette
        body = self.body
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="Preset", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.preset_var = tk.StringVar()
        self.selector = ttk.Combobox(body, textvariable=self.preset_var,
                                     state="readonly", width=28)
        self.selector.grid(row=0, column=1, sticky="ew")
        self.selector.bind("<<ComboboxSelected>>", lambda _e: self._load_selected())
        buttons = ttk.Frame(body)
        buttons.grid(row=0, column=2, padx=(6, 0))
        ttk.Button(buttons, text="New", command=self._new).pack(side="left")
        ttk.Button(buttons, text="Delete", command=self._delete).pack(
            side="left", padx=(4, 0))

        grid = ttk.Frame(body, style="Card.TFrame", padding=10)
        grid.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        grid.columnconfigure(1, weight=1)
        self.vars: dict[str, tk.StringVar] = {}
        for index, (key, label) in enumerate(self.FIELD_ORDER):
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(
                row=index, column=0, sticky="w", padx=(0, 10), pady=1)
            var = tk.StringVar()
            ttk.Entry(grid, textvariable=var).grid(row=index, column=1,
                                                   sticky="ew", pady=1)
            self.vars[key] = var

        note = ("Stored as plain JSON in your Windows profile. Anything "
                "running under your account can read it, so keep passwords "
                "and card numbers out of these fields.")
        ttk.Label(body, text=note, style="Muted.TLabel", wraplength=440).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))

        row = ttk.Frame(body)
        row.grid(row=3, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(row, text="Save preset", style="Accent.TButton",
                   command=self._save).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Close", command=self.destroy).pack(side="left")
        self._current_id: Optional[str] = None
        self._refresh(select_active=True)

    def accept(self, _event=None) -> None:
        self._save()

    def _refresh(self, *, select_active: bool = False) -> None:
        presets = self.store.presets()
        labels = [p.get("label", "Preset") for p in presets]
        self.selector.configure(values=labels)
        target = None
        if select_active:
            target = self.store.active()
        if target is None and presets:
            target = presets[0]
        if target is not None:
            self.preset_var.set(target.get("label", "Preset"))
            self._load(target)
        else:
            self.preset_var.set("")
            self._current_id = None
            for var in self.vars.values():
                var.set("")

    def _selected(self) -> Optional[dict]:
        label = self.preset_var.get()
        return next((p for p in self.store.presets()
                     if p.get("label") == label), None)

    def _load_selected(self) -> None:
        preset = self._selected()
        if preset is not None:
            self._load(preset)

    def _load(self, preset: dict) -> None:
        self._current_id = preset.get("id")
        fields = preset.get("fields", {})
        for key, var in self.vars.items():
            var.set(str(fields.get(key, "")))

    def _new(self) -> None:
        label = ask_text(self, self.palette, "New preset", "Preset name:",
                         "Personal")
        if not label:
            return
        preset_id = self.store.add(label, {})
        self._refresh()
        preset = self.store.get(preset_id)
        if preset is not None:
            self.preset_var.set(preset.get("label", label))
            self._load(preset)

    def _delete(self) -> None:
        if self._current_id:
            self.store.remove(self._current_id)
            self._refresh()

    def _save(self) -> None:
        values = {key: var.get().strip() for key, var in self.vars.items()
                  if var.get().strip()}
        if self._current_id is None:
            self._current_id = self.store.add(
                self.preset_var.get() or "Preset", values)
        else:
            self.store.update(self._current_id, field_values=values)
        self.store.set_active(self._current_id)
        self.result = {"saved": True}
        self.destroy()


class AutoFillReviewDialog(ThemedDialog):
    """Show auto-fill proposals; the user unticks what they do not want."""

    def __init__(self, parent, palette: Palette, proposals: list) -> None:
        super().__init__(parent, palette, "Review auto-fill")
        body = self.body
        ttk.Label(body, text=f"{len(proposals)} field"
                             f"{'s' if len(proposals) != 1 else ''} matched. "
                             "Untick anything wrong, then apply.").grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        holder = ttk.Frame(body, style="Card.TFrame", padding=8)
        holder.grid(row=1, column=0, sticky="ew")
        self._vars: list[tuple[tk.BooleanVar, object]] = []
        for index, proposal in enumerate(proposals):
            var = tk.BooleanVar(value=True)
            field_name = getattr(proposal.field, "name", "?")
            text = f"{field_name}  ←  {proposal.value}"
            check = ttk.Checkbutton(holder, text=text, variable=var,
                                    style="Toggle.TCheckbutton", takefocus=False)
            check.grid(row=index, column=0, sticky="w", pady=1)
            bind_tooltip(check, f"matched via {proposal.source}", palette)
            self._vars.append((var, proposal))
        row = ttk.Frame(body)
        row.grid(row=2, column=0, sticky="e", pady=(12, 0))
        self.bind("<Return>", lambda _e: self._accept())
        ttk.Button(row, text="Apply", style="Accent.TButton",
                   command=self._accept).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="left")

    def _accept(self) -> None:
        self.result = [proposal for var, proposal in self._vars if var.get()]
        self.destroy()


# ---------------------------------------------------------------------------
# Find and replace


class FindReplaceDialog(tk.Toplevel):
    """Non-modal find/replace over the whole document.

    The controller supplies three callbacks: ``on_find(pattern, options)``
    returning a match count, ``on_step(direction)``, and
    ``on_replace_all(pattern, replacement, options)``. The dialog never
    touches the session itself.
    """

    def __init__(self, parent, palette: Palette, *, on_find, on_step,
                 on_replace_all, can_replace: bool) -> None:
        super().__init__(parent)
        self.title("Find and replace")
        self.configure(background=palette.bg)
        self.transient(parent)
        self.resizable(False, False)
        self.palette = palette
        self.on_find = on_find
        self.on_step = on_step
        self.on_replace_all = on_replace_all

        body = ttk.Frame(self, padding=(14, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Find", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.find_var = tk.StringVar()
        find_entry = ttk.Entry(body, textvariable=self.find_var, width=34)
        find_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=2)
        ttk.Label(body, text="Replace", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8))
        self.replace_var = tk.StringVar()
        replace_entry = ttk.Entry(body, textvariable=self.replace_var, width=34)
        replace_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=2)

        options = ttk.Frame(body)
        options.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 2))
        self.regex_var = tk.BooleanVar(value=False)
        self.case_var = tk.BooleanVar(value=False)
        self.word_var = tk.BooleanVar(value=False)
        for text, var, tip in (
                ("Regex", self.regex_var,
                 "Regular expressions, with capture groups \\1 in Replace"),
                ("Case", self.case_var, "Match case exactly"),
                ("Words", self.word_var, "Whole words only")):
            check = ttk.Checkbutton(options, text=text, variable=var,
                                    style="Toggle.TCheckbutton", takefocus=False,
                                    command=self._changed)
            check.pack(side="left", padx=(0, 8))
            bind_tooltip(check, tip, palette)

        self.count_label = ttk.Label(body, text="", style="Muted.TLabel")
        self.count_label.grid(row=3, column=0, columnspan=2, sticky="w")
        self.error_label = ttk.Label(body, text="", style="Muted.TLabel",
                                     foreground=palette.danger, wraplength=320)
        self.error_label.grid(row=4, column=0, columnspan=3, sticky="w")

        row = ttk.Frame(body)
        row.grid(row=5, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(row, text="Previous", command=lambda: on_step(-1)).pack(
            side="left", padx=(0, 4))
        ttk.Button(row, text="Next", command=lambda: on_step(1)).pack(
            side="left", padx=(0, 10))
        self.replace_button = ttk.Button(row, text="Replace all",
                                         style="Accent.TButton",
                                         command=self._replace_all)
        self.replace_button.pack(side="left", padx=(0, 6))
        if not can_replace:
            self.replace_button.state(["disabled"])
            bind_tooltip(self.replace_button,
                         "This page has no text layer to edit; run OCR first. "
                         "Matches are still highlighted.", palette)
        ttk.Button(row, text="Close", command=self.destroy).pack(side="left")

        self.find_var.trace_add("write", lambda *_: self._changed())
        find_entry.bind("<Return>", lambda _e: on_step(1))
        self.bind("<Escape>", lambda _e: self.destroy())
        find_entry.focus_set()

    def options(self) -> dict:
        return {"regex": self.regex_var.get(), "case": self.case_var.get(),
                "whole_word": self.word_var.get()}

    def _changed(self) -> None:
        pattern = self.find_var.get()
        self.error_label.configure(text="")
        if not pattern:
            self.count_label.configure(text="")
            return
        try:
            count = self.on_find(pattern, self.options())
        except Exception as exc:
            self.error_label.configure(text=str(exc))
            self.count_label.configure(text="")
            return
        self.count_label.configure(
            text=f"{count} match{'es' if count != 1 else ''}")

    def _replace_all(self) -> None:
        pattern = self.find_var.get()
        if not pattern:
            return
        try:
            self.on_replace_all(pattern, self.replace_var.get(), self.options())
        except Exception as exc:
            self.error_label.configure(text=str(exc))


# ---------------------------------------------------------------------------
# OCR


class OcrDialog(ThemedDialog):
    """Configure and start OCR: language, variant, preprocessing, scope."""

    def __init__(self, parent, palette: Palette, settings: dict, pack_manager,
                 *, page_count: int, scan_pages: int) -> None:
        super().__init__(parent, palette, "Recognise text (OCR)")
        self.pack_manager = pack_manager
        self.palette = palette
        body = self.body
        body.columnconfigure(1, weight=1)

        summary = (f"{scan_pages} of {page_count} pages look like scans "
                   "with no text layer." if scan_pages else
                   "Every page already has a text layer; OCR would only "
                   "re-read pages that need it.")
        ttk.Label(body, text=summary, wraplength=430).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(body, text="Language", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8))
        self.lang_var = tk.StringVar(value=settings.get("ocr_language", "eng"))
        self.lang_box = ttk.Combobox(body, textvariable=self.lang_var,
                                     state="readonly", width=30)
        self.lang_box.grid(row=1, column=1, sticky="w")
        self._fill_languages()

        self.variant_var = tk.StringVar(value=settings.get("ocr_variant", "fast"))
        variant = ttk.Frame(body, style="Card.TFrame", padding=1)
        variant.grid(row=1, column=2, padx=(8, 0))
        for label, value, tip in (("Fast", "fast", "Smaller download, quick"),
                                  ("Best", "best", "Larger models, highest accuracy")):
            radio = ttk.Radiobutton(variant, text=label, value=value,
                                    variable=self.variant_var,
                                    style="Segment.TRadiobutton", takefocus=False,
                                    command=self._fill_languages)
            radio.pack(side="left")
            bind_tooltip(radio, tip, palette)

        self.preprocess_var = tk.BooleanVar(
            value=bool(settings.get("ocr_preprocess", True)))
        pre = ttk.Checkbutton(
            body, text="Clean up scans first (deskew, denoise, threshold)",
            variable=self.preprocess_var, style="Toggle.TCheckbutton",
            takefocus=False)
        pre.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        bind_tooltip(pre, "Benchmarked accuracy edge on tilted or noisy scans",
                     palette)

        self.scope_var = tk.StringVar(value="document")
        scope = ttk.Frame(body)
        scope.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Radiobutton(scope, text="Whole document", value="document",
                        variable=self.scope_var, takefocus=False).pack(side="left")
        ttk.Radiobutton(scope, text="Current page", value="page",
                        variable=self.scope_var, takefocus=False).pack(
            side="left", padx=(10, 0))

        self.status = ttk.Label(body, text="", style="Muted.TLabel",
                                wraplength=430)
        self.status.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        row = ttk.Frame(body)
        row.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(row, text="Make searchable", style="Accent.TButton",
                   command=self._accept).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="left")

    def accept(self, _event=None) -> None:
        self._accept()

    def _fill_languages(self) -> None:
        manager = self.pack_manager
        variant = self.variant_var.get() if hasattr(self, "variant_var") else "fast"
        installed = set(manager.installed(variant))
        values = []
        for code, meta in manager.KNOWN_LANGS.items():
            name = meta[0] if isinstance(meta, (tuple, list)) else str(meta)
            marker = "" if code in installed else "  (download)"
            values.append(f"{code}  {name}{marker}")
        self.lang_box.configure(values=values)
        current = self.lang_var.get().split()[0]
        for value in values:
            if value.split()[0] == current:
                self.lang_var.set(value)
                break

    def _accept(self) -> None:
        code = self.lang_var.get().split()[0] if self.lang_var.get() else "eng"
        self.result = {
            "lang": code,
            "variant": self.variant_var.get(),
            "preprocess": bool(self.preprocess_var.get()),
            "scope": self.scope_var.get(),
        }
        self.destroy()


# ---------------------------------------------------------------------------
# Page range helper reused by the controller

def parse_page_range(text: str, page_count: int):
    """Public wrapper so the controller can validate ranges the same way."""
    return _parse_range(text, page_count)


def page_ops_menu(parent, palette: Palette, controller, page: int) -> tk.Menu:
    """The shared page-operations context menu (thumb strip and canvas)."""
    menu = tk.Menu(parent)
    style_menu(menu, palette)
    menu.add_command(label="Rotate page right",
                     command=lambda: controller.rotate_page(page, 90))
    menu.add_command(label="Rotate page left",
                     command=lambda: controller.rotate_page(page, -90))
    menu.add_separator()
    menu.add_command(label="Duplicate page",
                     command=lambda: controller.duplicate_page(page))
    menu.add_command(label="Insert blank page after",
                     command=lambda: controller.insert_blank_after(page))
    menu.add_command(label="Insert PDF after…",
                     command=lambda: controller.insert_pdf_after(page))
    menu.add_separator()
    menu.add_command(label="Extract page…",
                     command=lambda: controller.extract_pages([page]))
    menu.add_command(label="Delete page",
                     command=lambda: controller.delete_pages([page]))
    return menu
