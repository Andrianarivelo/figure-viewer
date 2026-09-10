"""In-memory PDF editing kernel: sessions, commands, undo/redo, saving.

Design rules (enforced by tests in ``tests/test_pdfedit.py``)
-------------------------------------------------------------
1. **The raw file is sacred.** A session is opened from a byte copy of the
   file, exactly like :func:`figureviewer.docs.load_document`. Nothing here
   ever writes to the source path; saving produces *bytes* that the caller
   writes atomically (temp file + ``os.replace``).
2. **One document object for the whole session.** The viewer compares
   document identity to discard stale worker results, so undo never replaces
   the :class:`EditableVectorDocument` wrapper, only the ``fitz`` document
   inside it (:meth:`EditableVectorDocument.swap_doc`).
3. **Every mutation happens under the document lock.** Rendering keeps
   running on worker threads; the shared reentrant lock in
   :class:`figureviewer.docs.VectorDocument` serialises them against edits.
4. **No tkinter.** This module is fully testable headless.

Undo is hybrid, in the style of serious editors:

* cheap, high-frequency operations (add / modify / move / delete a single
  annotation, fill one form field) are **object commands** that know how to
  reverse themselves;
* structural operations (page ops, redactions, reflow commits, OCR overlays,
  signature stamps) are **snapshot commands** holding zlib-compressed copies
  of the whole document before and after, restored via ``swap_doc``.

Annotations are tracked by a session-generated uid stored in the PDF's
standard ``/NM`` (name) entry, never by raw xref: undoing and redoing an
"add" changes the annotation's xref, and a uid survives that while staying
visible to any other standards-compliant PDF tool.
"""
from __future__ import annotations

import threading
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, Sequence

from . import docs as D

# ---------------------------------------------------------------------------
# Budgets for the undo history. Snapshots dominate memory, so they get their
# own caps; when a budget is exceeded the *oldest* history entries are
# dropped and the session records that truncation happened so the UI can say
# "Undo history trimmed to save memory" instead of silently forgetting.
MAX_COMMANDS = 64
MAX_SNAPSHOTS = 12
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024

#: Stands in for a save marker whose command was evicted from the history.
_EVICTED_SAVE_MARKER = object()

#: Annotation type names used in labels and by the properties popover.
TYPE_HIGHLIGHT = "highlight"
TYPE_UNDERLINE = "underline"
TYPE_STRIKEOUT = "strikeout"
TYPE_SQUIGGLY = "squiggly"
TYPE_FREETEXT = "freetext"
TYPE_NOTE = "note"
TYPE_INK = "ink"
TYPE_RECT = "rect"
TYPE_ELLIPSE = "ellipse"
TYPE_LINE = "line"
TYPE_ARROW = "arrow"
TYPE_REDACT = "redact"
TYPE_OTHER = "other"

MARKUP_KINDS = (TYPE_HIGHLIGHT, TYPE_UNDERLINE, TYPE_STRIKEOUT, TYPE_SQUIGGLY)
SHAPE_KINDS = (TYPE_RECT, TYPE_ELLIPSE, TYPE_LINE, TYPE_ARROW)


def _require_fitz() -> None:
    if not D.HAVE_FITZ:
        raise RuntimeError(
            "PDF editing requires PyMuPDF.\n\nInstall it with:\n"
            "    pip install pymupdf"
        )


def _pdf_string(value: str) -> str:
    """A safe PDF literal string for xref_set_key (uids are hex, but be strict)."""
    escaped = value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return f"({escaped})"


@dataclass(frozen=True)
class AnnotInfo:
    """A UI-friendly description of one annotation on a page."""

    page: int
    xref: int
    uid: str
    kind: str
    rect: tuple[float, float, float, float]   # PDF points, top-left origin
    contents: str
    subject: str = ""


class EditableVectorDocument(D.VectorDocument):
    """A private, mutable PDF document owned by exactly one edit session.

    Never placed in the shared :class:`figureviewer.docs.DocumentCache`; the
    viewer swaps it in as ``self.document`` while a session is attached. The
    object survives every undo snapshot: only the inner ``fitz`` document is
    replaced, so the viewer's ``self.document is document`` staleness guard
    keeps working.
    """

    @classmethod
    def from_entry(cls, entry: D.Entry, *, base_dpi: float = 150.0
                   ) -> "EditableVectorDocument":
        _require_fitz()
        return cls(entry, entry.path.read_bytes(), base_dpi=base_dpi)

    def bump_version(self) -> None:
        """Invalidate every cached rasterisation of this document."""
        with self._lock:
            self.content_version += 1
            self._tile = None

    def refresh_geometry(self) -> None:
        """Re-read page count and format after a structural mutation.

        ``swap_doc`` does this itself, but a first-time snapshot command
        mutates the inner document in place, and the wrapper's cached
        ``page_count`` must follow immediately (the delete-last-page guard
        and the page controls both read it)."""
        with self._lock:
            self.page_count = max(1, self._doc.page_count)
            self.info["Pages"] = str(self.page_count)
            try:
                self.info["Page format"] = self.page_format(0)
            except Exception:
                pass

    def to_bytes(self, *, garbage: bool = False) -> bytes:
        """Serialise the current in-memory state. Never touches the disk.

        Garbage collection runs on a throwaway copy: MuPDF's garbage pass
        renumbers xrefs in the document it serialises, and the undo history
        (ModifyFieldCommand) and cached FieldInfo records hold live xrefs.
        Collecting on the live document made undo of a pre-save form fill
        raise LookupError after a save; a copy keeps the live xrefs stable
        for the whole session.
        """
        with self._lock:
            raw = self._doc.tobytes(deflate=True)
        if not garbage:
            return raw
        scratch = D.fitz.open(stream=raw, filetype="pdf")
        try:
            return scratch.tobytes(garbage=3, deflate=True)
        finally:
            scratch.close()

    def swap_doc(self, new_bytes: bytes) -> None:
        """Replace the inner fitz document, preserving wrapper identity."""
        with self._lock:
            old = self._doc
            self._doc = D.fitz.open(stream=new_bytes, filetype="pdf")
            self.page_count = max(1, self._doc.page_count)
            self.info["Pages"] = str(self.page_count)
            try:
                self.info["Page format"] = self.page_format(0)
            except Exception:
                pass
            self.content_version += 1
            self._tile = None
            try:
                old.close()
            except Exception:
                pass

    @property
    def has_signatures(self) -> bool:
        """True when the file carries digital signature fields.

        Saving such a file with garbage collection rewrites the xref table
        and breaks the signatures, so :meth:`EditSession.save_bytes` degrades
        to a plain deflate save and the UI warns about it.
        """
        with self._lock:
            try:
                return self._doc.get_sigflags() > 0
            except Exception:
                return False


# ---------------------------------------------------------------------------
# Commands


class Command:
    """One reversible unit of work. ``structural`` requests a page-strip
    refresh (page count, order, or rotation changed)."""

    label = "Edit"
    structural = False

    def do(self, session: "EditSession") -> None:
        raise NotImplementedError

    def undo(self, session: "EditSession") -> None:
        raise NotImplementedError

    def snapshot_bytes(self) -> int:
        """Memory charged against the snapshot budget."""
        return 0


class _AnnotCommandBase(Command):
    """Shared uid-based resolution for annotation commands."""

    def __init__(self, page: int, uid: str) -> None:
        self.page = page
        self.uid = uid

    def resolve(self, session: "EditSession"):
        """Return ``(page, annot)``. The page proxy MUST be kept referenced
        while the annot is used: a PyMuPDF annot is bound to the exact page
        object it was found through, and mutating it via a second, freshly
        loaded proxy raises (or, in native code paths, crashes)."""
        page, annot = session._locate(self.page, self.uid)
        if annot is None:
            raise LookupError(f"annotation {self.uid} vanished from page {self.page}")
        return page, annot


class AddAnnotCommand(_AnnotCommandBase):
    """Adds one annotation; construction is delegated back to the session so
    redo rebuilds an identical annotation (same uid, fresh xref)."""

    def __init__(self, page: int, uid: str, label: str,
                 build: Callable[["EditSession"], None]) -> None:
        super().__init__(page, uid)
        self.label = label
        self._build = build

    def do(self, session: "EditSession") -> None:
        self._build(session)

    def undo(self, session: "EditSession") -> None:
        page, annot = self.resolve(session)
        page.delete_annot(annot)


class ModifyAnnotCommand(_AnnotCommandBase):
    """Applies a property dict to an annotation; undo restores the old one."""

    def __init__(self, page: int, uid: str, old: dict, new: dict,
                 label: str = "Edit properties") -> None:
        super().__init__(page, uid)
        self.old = old
        self.new = new
        self.label = label

    def do(self, session: "EditSession") -> None:
        session._apply_annot_props(self.page, self.uid, self.new)

    def undo(self, session: "EditSession") -> None:
        session._apply_annot_props(self.page, self.uid, self.old)


class MoveResizeAnnotCommand(_AnnotCommandBase):
    label = "Move"

    def __init__(self, page: int, uid: str,
                 old_rect: tuple[float, float, float, float],
                 new_rect: tuple[float, float, float, float]) -> None:
        super().__init__(page, uid)
        self.old_rect = old_rect
        self.new_rect = new_rect

    def do(self, session: "EditSession") -> None:
        session._set_annot_rect(self.page, self.uid, self.new_rect)

    def undo(self, session: "EditSession") -> None:
        session._set_annot_rect(self.page, self.uid, self.old_rect)


class ModifyFieldCommand(Command):
    """Sets one AcroForm field value; undo restores the previous value."""

    def __init__(self, page: int, field_xref: int, old_value, new_value,
                 label: str = "Fill field") -> None:
        self.page = page
        self.field_xref = field_xref
        self.old_value = old_value
        self.new_value = new_value
        self.label = label

    def _apply(self, session: "EditSession", value) -> None:
        page, widget = session._locate_widget(self.page, self.field_xref)
        if widget is None:
            raise LookupError(f"form field xref {self.field_xref} not found")
        if value in ("", None):
            # PyMuPDF quirk: assigning an empty value and calling update()
            # silently keeps the old text. Clearing /V at the xref level and
            # regenerating the appearance from the reloaded widget works.
            session.document._doc.xref_set_key(self.field_xref, "V", "()")
            page, fresh = session._locate_widget(self.page, self.field_xref)
            if fresh is not None:
                fresh.update()
        else:
            widget.field_value = value
            widget.update()
        assert page is not None   # the proxy must outlive widget.update()

    def do(self, session: "EditSession") -> None:
        self._apply(session, self.new_value)

    def undo(self, session: "EditSession") -> None:
        self._apply(session, self.old_value)


class SnapshotCommand(Command):
    """Whole-document before/after snapshot, compressed.

    Two lifecycles:

    * ``SnapshotCommand(label, mutate=...)`` runs ``mutate`` on first
      :meth:`do`, capturing bytes around it;
    * :meth:`EditSession.checkpoint` builds one in *pre-applied* form for
      mutations executed by other modules (reflow, OCR, signing).

    Redo swaps to the after-bytes, so it is deterministic even when the
    original mutation had side effects that are not replayable.
    """

    def __init__(self, label: str, mutate: Optional[Callable[[], None]] = None,
                 *, structural: bool = False) -> None:
        self.label = label
        self._mutate = mutate
        self.structural = structural
        self._before: Optional[bytes] = None
        self._after: Optional[bytes] = None

    @classmethod
    def pre_applied(cls, label: str, before: bytes, after: bytes,
                    *, structural: bool = False) -> "SnapshotCommand":
        cmd = cls(label, structural=structural)
        cmd._before = zlib.compress(before)
        cmd._after = zlib.compress(after)
        return cmd

    def do(self, session: "EditSession") -> None:
        if self._after is not None:
            session.document.swap_doc(zlib.decompress(self._after))
            return
        self._before = zlib.compress(session.document.to_bytes())
        assert self._mutate is not None
        try:
            self._mutate()
        except BaseException:
            # A mutation that fails half-way (second merge file broken, one
            # page's redaction failing) must not leave a changed document
            # that no history entry knows about: restore the before state,
            # then let the error reach the UI.
            session.document.swap_doc(zlib.decompress(self._before))
            self._before = None
            raise
        self._after = zlib.compress(session.document.to_bytes())

    def undo(self, session: "EditSession") -> None:
        assert self._before is not None
        session.document.swap_doc(zlib.decompress(self._before))

    def snapshot_bytes(self) -> int:
        return len(self._before or b"") + len(self._after or b"")


class GroupCommand(Command):
    """Several commands undone and redone as one history entry."""

    def __init__(self, label: str, commands: list[Command]) -> None:
        self.label = label
        self.commands = commands
        self.structural = any(c.structural for c in commands)

    def do(self, session: "EditSession") -> None:
        for command in self.commands:
            command.do(session)

    def undo(self, session: "EditSession") -> None:
        for command in reversed(self.commands):
            command.undo(session)

    def snapshot_bytes(self) -> int:
        return sum(c.snapshot_bytes() for c in self.commands)


@dataclass
class ChangeEvent:
    """Passed to on_change listeners after every history movement."""

    label: str
    dirty: bool
    structural: bool
    can_undo: bool
    can_redo: bool


# ---------------------------------------------------------------------------
# The session


@dataclass
class _History:
    undo: list[Command] = field(default_factory=list)
    redo: list[Command] = field(default_factory=list)
    truncated: bool = False

    def snapshot_count(self) -> int:
        return sum(1 for c in self.undo if c.snapshot_bytes() > 0)

    def snapshot_size(self) -> int:
        return sum(c.snapshot_bytes() for c in self.undo)


class EditSession:
    """All edit state for one PDF file: document, history, dirty tracking.

    Owned by the App (one session per resolved path); windows attach to it.
    Every public mutator runs on the Tk thread, takes the document lock,
    pushes exactly one history entry (or joins an open group), bumps the
    document's content version and notifies listeners.
    """

    def __init__(self, entry: D.Entry, *, base_dpi: float = 150.0) -> None:
        _require_fitz()
        self.entry = entry
        self.document = EditableVectorDocument.from_entry(entry, base_dpi=base_dpi)
        self._history = _History()
        self._group: Optional[list[Command]] = None
        self._group_label = ""
        self._saved_top: Optional[Command] = None   # top of undo stack at save
        self.saved_stamp = entry.stamp
        self.on_change: list[Callable[[ChangeEvent], None]] = []
        #: Installed by the UI: ``ui_dispatch(callable)`` runs the callable
        #: on the Tk thread. Mutations wrapped in :meth:`checkpoint` may run
        #: on a worker (reflow's replace-all, OCR overlays), and listeners
        #: touch widgets, so :meth:`_fire` marshals when it is off-thread.
        self.ui_dispatch: Optional[Callable[[Callable[[], None]], None]] = None
        self._ui_thread = threading.current_thread()
        #: Optional OCR text provider installed by figureviewer.ocr:
        #: Callable[(document, page index), str].
        self.ocr_text_provider: Optional[Callable[[object, int], str]] = None

    # -- state ------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        top = self._history.undo[-1] if self._history.undo else None
        return top is not self._saved_top

    @property
    def can_undo(self) -> bool:
        return bool(self._history.undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._history.redo)

    @property
    def undo_label(self) -> str:
        return self._history.undo[-1].label if self._history.undo else ""

    @property
    def redo_label(self) -> str:
        return self._history.redo[-1].label if self._history.redo else ""

    @property
    def history_truncated(self) -> bool:
        return self._history.truncated

    @property
    def content_version(self) -> int:
        return self.document.content_version

    # -- plumbing ---------------------------------------------------------
    def _page(self, page: int):
        return self.document._page(page)

    def _fire(self, label: str, structural: bool) -> None:
        event = ChangeEvent(label=label, dirty=self.dirty, structural=structural,
                            can_undo=self.can_undo, can_redo=self.can_redo)
        if (self.ui_dispatch is not None
                and threading.current_thread() is not self._ui_thread):
            self.ui_dispatch(lambda: self._deliver(event))
            return
        self._deliver(event)

    def _deliver(self, event: ChangeEvent) -> None:
        for listener in list(self.on_change):
            try:
                listener(event)
            except Exception:
                pass

    def _touch(self, label: str, structural: bool) -> None:
        if structural:
            self.document.refresh_geometry()
        self.document.bump_version()
        self._fire(label, structural)

    def _push(self, command: Command) -> None:
        if self._group is not None:
            self._group.append(command)
            return
        self._history.undo.append(command)
        self._history.redo.clear()
        self._enforce_budget()

    def _enforce_budget(self) -> None:
        history = self._history
        while history.undo and (
            len(history.undo) > MAX_COMMANDS
            or history.snapshot_count() > MAX_SNAPSHOTS
            or history.snapshot_size() > MAX_SNAPSHOT_BYTES
        ):
            dropped = history.undo.pop(0)
            history.truncated = True
            # The save marker may point at a command that just left history,
            # or at "nothing" for a never-saved session whose first edit just
            # left it. Either way the document can never be undone back to
            # its on-disk state, so dirty must stay True until the next
            # explicit save: point the marker at a sentinel no stack top can
            # ever be again.
            if dropped is self._saved_top or self._saved_top is None:
                self._saved_top = _EVICTED_SAVE_MARKER

    def _execute(self, command: Command) -> None:
        with self.document._lock:
            command.do(self)
        self._push(command)
        self._touch(command.label, command.structural)

    def undo(self) -> str:
        if not self._history.undo:
            return ""
        command = self._history.undo.pop()
        with self.document._lock:
            command.undo(self)
        self._history.redo.append(command)
        self._touch(f"Undid {command.label}", command.structural)
        return command.label

    def redo(self) -> str:
        if not self._history.redo:
            return ""
        command = self._history.redo.pop()
        with self.document._lock:
            command.do(self)
        self._history.undo.append(command)
        self._touch(f"Redid {command.label}", command.structural)
        return command.label

    def begin_group(self, label: str) -> None:
        if self._group is not None:
            raise RuntimeError("nested edit groups are not supported")
        self._group = []
        self._group_label = label

    def end_group(self) -> None:
        commands, self._group = self._group, None
        if commands:
            group = GroupCommand(self._group_label, commands)
            self._history.undo.append(group)
            self._history.redo.clear()
            self._enforce_budget()
            self._fire(group.label, group.structural)

    @contextmanager
    def checkpoint(self, label: str, *, structural: bool = False) -> Iterator[None]:
        """Wrap an external mutation in a single snapshot undo entry.

        Used by reflow, OCR and signing: any mutation of ``session.document``
        inside the ``with`` body becomes one undoable command. On an
        exception the document is restored to the before state and the error
        propagates.
        """
        before = self.document.to_bytes()
        try:
            yield
        except Exception:
            self.document.swap_doc(before)
            self._fire(f"Failed {label}", structural)
            raise
        after = self.document.to_bytes()
        command = SnapshotCommand.pre_applied(label, before, after,
                                              structural=structural)
        self._push(command)
        self._touch(label, structural)

    # -- annotation helpers ----------------------------------------------
    def _tag_annot(self, annot, uid: str) -> None:
        doc = self.document._doc
        doc.xref_set_key(annot.xref, "NM", _pdf_string(uid))

    def _annot_uid(self, annot) -> str:
        """Identity of an annotation: its /NM name, else its xref.

        Foreign annotations frequently carry no /NM; without the fallback
        they all collapsed to the empty name and a delete or move resolved
        to the first unnamed one. Xrefs stay stable for the session because
        garbage collection only ever runs on a throwaway copy."""
        name = (annot.info or {}).get("id") or ""
        return name or f"xref:{annot.xref}"

    def _locate(self, page: int, uid: str):
        """Return ``(page_proxy, annot_or_None)`` from ONE page load.

        Callers must keep the returned page referenced for as long as they
        touch the annot (see :meth:`_AnnotCommandBase.resolve`)."""
        target = self._page(page)
        for annot in target.annots() or ():
            if self._annot_uid(annot) == uid:
                return target, annot
        return target, None

    def _locate_widget(self, page: int, xref: int):
        """Return ``(page_proxy, widget_or_None)`` from one page load; the
        proxy must stay referenced while the widget is used (same binding
        rule as annotations)."""
        target = self._page(page)
        for widget in target.widgets() or ():
            if widget.xref == xref:
                return target, widget
        return target, None

    @staticmethod
    def _annot_kind(annot) -> str:
        code = annot.type[0]
        fitz = D.fitz
        mapping = {
            fitz.PDF_ANNOT_HIGHLIGHT: TYPE_HIGHLIGHT,
            fitz.PDF_ANNOT_UNDERLINE: TYPE_UNDERLINE,
            fitz.PDF_ANNOT_STRIKE_OUT: TYPE_STRIKEOUT,
            fitz.PDF_ANNOT_SQUIGGLY: TYPE_SQUIGGLY,
            fitz.PDF_ANNOT_FREE_TEXT: TYPE_FREETEXT,
            fitz.PDF_ANNOT_TEXT: TYPE_NOTE,
            fitz.PDF_ANNOT_INK: TYPE_INK,
            fitz.PDF_ANNOT_SQUARE: TYPE_RECT,
            fitz.PDF_ANNOT_CIRCLE: TYPE_ELLIPSE,
            fitz.PDF_ANNOT_REDACT: TYPE_REDACT,
        }
        if code == fitz.PDF_ANNOT_LINE:
            ends = getattr(annot, "line_ends", None)
            if ends and any(ends):
                return TYPE_ARROW
            return TYPE_LINE
        return mapping.get(code, TYPE_OTHER)

    def _info(self, page: int, annot) -> AnnotInfo:
        rect = annot.rect
        metadata = annot.info or {}
        return AnnotInfo(
            page=page, xref=annot.xref, uid=self._annot_uid(annot),
            kind=self._annot_kind(annot),
            rect=(rect.x0, rect.y0, rect.x1, rect.y1),
            contents=metadata.get("content", ""),
            subject=metadata.get("subject", ""),
        )

    def annots_on(self, page: int) -> list[AnnotInfo]:
        with self.document._lock:
            return [self._info(page, a) for a in self._page(page).annots() or ()]

    def hit_test(self, page: int, point_pts: tuple[float, float],
                 tolerance: float = 3.0) -> Optional[AnnotInfo]:
        """Topmost annotation under a page point, in PDF points."""
        x, y = point_pts
        with self.document._lock:
            hits = []
            for annot in self._page(page).annots() or ():
                r = annot.rect
                if (r.x0 - tolerance <= x <= r.x1 + tolerance
                        and r.y0 - tolerance <= y <= r.y1 + tolerance):
                    hits.append(self._info(page, annot))
            return hits[-1] if hits else None

    # -- annotation mutators ----------------------------------------------
    def add_markup(self, page: int, quads: Sequence, kind: str,
                   color: tuple[float, float, float], opacity: float, *,
                   contents: str = "", subject: str = "") -> str:
        """Create text markup and its comment as one reversible operation.

        Contents and subject use standard PDF annotation metadata, so other
        readers can display the comment and redo restores it with the markup.
        """
        if kind not in MARKUP_KINDS:
            raise ValueError(f"unknown markup kind {kind!r}")
        uid = uuid.uuid4().hex
        quads = list(quads)

        def build(session: "EditSession") -> None:
            target = session._page(page)
            method = {
                TYPE_HIGHLIGHT: target.add_highlight_annot,
                TYPE_UNDERLINE: target.add_underline_annot,
                TYPE_STRIKEOUT: target.add_strikeout_annot,
                TYPE_SQUIGGLY: target.add_squiggly_annot,
            }[kind]
            annot = method(quads=quads)
            annot.set_colors(stroke=color)
            annot.set_opacity(opacity)
            if contents or subject:
                annot.set_info(content=contents, subject=subject)
            annot.update()
            session._tag_annot(annot, uid)

        self._execute(AddAnnotCommand(page, uid, f"Add {kind}", build))
        return uid

    def add_free_text(self, page: int, rect_pts: tuple[float, float, float, float],
                      text: str, *, fontname: str = "helv", fontsize: float = 11.0,
                      color: tuple[float, float, float] = (0.1, 0.1, 0.1),
                      align: int = 0) -> str:
        uid = uuid.uuid4().hex

        def build(session: "EditSession") -> None:
            target = session._page(page)
            # rotate follows /Rotate so the text reads upright on a page
            # that is stored sideways.
            annot = target.add_freetext_annot(
                D.fitz.Rect(*rect_pts), text, fontsize=fontsize,
                fontname=fontname, text_color=color, fill_color=None,
                align=align, rotate=target.rotation)
            annot.set_border(width=0)
            annot.update()
            session._tag_annot(annot, uid)

        self._execute(AddAnnotCommand(page, uid, "Add text", build))
        return uid

    def add_note(self, page: int, point_pts: tuple[float, float], text: str,
                 icon: str = "Comment") -> str:
        uid = uuid.uuid4().hex

        def build(session: "EditSession") -> None:
            target = session._page(page)
            annot = target.add_text_annot(D.fitz.Point(*point_pts), text, icon=icon)
            annot.update()
            session._tag_annot(annot, uid)

        self._execute(AddAnnotCommand(page, uid, "Add note", build))
        return uid

    def add_ink(self, page: int, strokes: Sequence[Sequence[tuple[float, float]]],
                color: tuple[float, float, float], width: float) -> str:
        uid = uuid.uuid4().hex
        strokes = [list(map(tuple, stroke)) for stroke in strokes]

        def build(session: "EditSession") -> None:
            target = session._page(page)
            annot = target.add_ink_annot(strokes)
            annot.set_border(width=width)
            annot.set_colors(stroke=color)
            annot.update()
            session._tag_annot(annot, uid)

        self._execute(AddAnnotCommand(page, uid, "Draw ink", build))
        return uid

    def add_shape(self, page: int, kind: str, geom, *,
                  stroke: tuple[float, float, float],
                  fill: Optional[tuple[float, float, float]] = None,
                  width: float = 1.4) -> str:
        if kind not in SHAPE_KINDS:
            raise ValueError(f"unknown shape kind {kind!r}")
        uid = uuid.uuid4().hex

        def build(session: "EditSession") -> None:
            target = session._page(page)
            fitz = D.fitz
            if kind in (TYPE_RECT, TYPE_ELLIPSE):
                rect = fitz.Rect(*geom)
                annot = (target.add_rect_annot(rect) if kind == TYPE_RECT
                         else target.add_circle_annot(rect))
                annot.set_colors(stroke=stroke, fill=fill)
            else:
                (x0, y0), (x1, y1) = geom
                annot = target.add_line_annot(fitz.Point(x0, y0), fitz.Point(x1, y1))
                annot.set_colors(stroke=stroke)
                if kind == TYPE_ARROW:
                    annot.set_line_ends(fitz.PDF_ANNOT_LE_NONE,
                                        fitz.PDF_ANNOT_LE_OPEN_ARROW)
            annot.set_border(width=width)
            annot.update()
            session._tag_annot(annot, uid)

        self._execute(AddAnnotCommand(page, uid, f"Add {kind}", build))
        return uid

    # -- annotation property editing --------------------------------------
    _PROP_KEYS = ("stroke", "fill", "opacity", "width", "contents", "fontsize",
                  "text_color")

    def _read_props(self, annot) -> dict:
        colors = annot.colors or {}
        border = annot.border or {}
        return {
            "stroke": tuple(colors.get("stroke") or ()) or None,
            "fill": tuple(colors.get("fill") or ()) or None,
            "opacity": annot.opacity if annot.opacity is not None else 1.0,
            "width": border.get("width", 1.0),
            "contents": (annot.info or {}).get("content", ""),
        }

    def _apply_annot_props(self, page: int, uid: str, props: dict) -> None:
        # page_proxy is unused by name, but it must stay referenced until the
        # last annot call below; the annot is bound to exactly this proxy.
        page_proxy, annot = self._locate(page, uid)
        if annot is None:
            raise LookupError(f"annotation {uid} vanished from page {page}")
        if "stroke" in props or "fill" in props:
            annot.set_colors(stroke=props.get("stroke"), fill=props.get("fill"))
        if props.get("width") is not None:
            annot.set_border(width=float(props["width"]))
        if props.get("opacity") is not None:
            annot.set_opacity(float(props["opacity"]))
        if props.get("contents") is not None:
            annot.set_info(content=props["contents"])
        kwargs = {}
        if props.get("fontsize") is not None:
            kwargs["fontsize"] = float(props["fontsize"])
        annot.update(**kwargs)
        assert page_proxy is not None

    def set_annot_props(self, page: int, uid: str, **props) -> None:
        unknown = set(props) - set(self._PROP_KEYS)
        if unknown:
            raise ValueError(f"unknown annotation properties: {sorted(unknown)}")
        with self.document._lock:
            page_proxy, annot = self._locate(page, uid)
            if annot is None:
                raise LookupError(f"annotation {uid} vanished from page {page}")
            old = self._read_props(annot)
            del annot, page_proxy
        old = {k: old.get(k) for k in props}
        self._execute(ModifyAnnotCommand(page, uid, old, dict(props)))

    def _set_annot_rect(self, page: int, uid: str,
                        rect_pts: tuple[float, float, float, float]) -> None:
        # One page proxy for the whole operation: find, delete and re-add all
        # go through the same object (see _locate).
        target, annot = self._locate(page, uid)
        if annot is None:
            raise LookupError(f"annotation {uid} vanished from page {page}")
        kind = self._annot_kind(annot)
        if kind in MARKUP_KINDS:
            # Text markups are anchored to their quads, not to /Rect; MuPDF
            # ignores set_rect on them, so a "move" would push a history
            # entry that changes nothing.
            raise ValueError("Text markups follow their text and cannot be "
                             "moved or resized")
        fitz = D.fitz
        new_rect = fitz.Rect(*rect_pts)
        if kind in (TYPE_LINE, TYPE_ARROW, TYPE_INK):
            # Geometry annotations: scale their vertices into the new rect;
            # set_rect alone would stretch the appearance without moving the
            # underlying points. Recreating with transformed geometry keeps
            # the appearance stream exact; the uid is re-applied so history
            # entries pointing at this annotation stay valid.
            old_rect = annot.rect
            sx = new_rect.width / max(old_rect.width, 1e-6)
            sy = new_rect.height / max(old_rect.height, 1e-6)

            def remap(pt) -> tuple[float, float]:
                return (new_rect.x0 + (pt[0] - old_rect.x0) * sx,
                        new_rect.y0 + (pt[1] - old_rect.y0) * sy)

            info = self._read_props(annot)
            meta = dict(annot.info or {})
            if kind == TYPE_INK:
                # annot.vertices for ink is a list of stroke point lists.
                strokes = [[remap(p) for p in stroke] for stroke in annot.vertices]
                target.delete_annot(annot)
                fresh = target.add_ink_annot(strokes)
                if info.get("stroke"):
                    fresh.set_colors(stroke=info["stroke"])
                fresh.set_border(width=info.get("width", 1.0))
            else:
                p1, p2 = annot.vertices[0], annot.vertices[-1]
                ends = annot.line_ends or (0, 0)
                target.delete_annot(annot)
                fresh = target.add_line_annot(fitz.Point(*remap(p1)),
                                              fitz.Point(*remap(p2)))
                if info.get("stroke"):
                    fresh.set_colors(stroke=info["stroke"])
                fresh.set_line_ends(*ends)
                fresh.set_border(width=info.get("width", 1.0))
            # A recreated annotation must keep what the user cannot see in
            # its geometry: opacity, comment, author, subject and dates.
            self._copy_meta(fresh, info, meta)
            fresh.update()
            self._tag_annot(fresh, uid)
        else:
            annot.set_rect(new_rect)
            annot.update()

    @staticmethod
    def _copy_meta(fresh, props: dict, meta: dict) -> None:
        opacity = props.get("opacity")
        if opacity is not None and 0.0 <= float(opacity) < 1.0:
            fresh.set_opacity(float(opacity))
        keys = {"content": meta.get("content"), "title": meta.get("title"),
                "subject": meta.get("subject"),
                "creationDate": meta.get("creationDate"),
                "modDate": meta.get("modDate")}
        keys = {k: v for k, v in keys.items() if v}
        if keys:
            try:
                fresh.set_info(**keys)
            except Exception:
                pass

    def move_annot(self, page: int, uid: str,
                   new_rect_pts: tuple[float, float, float, float]) -> None:
        with self.document._lock:
            page_proxy, annot = self._locate(page, uid)
            if annot is None:
                raise LookupError(f"annotation {uid} vanished from page {page}")
            if self._annot_kind(annot) in MARKUP_KINDS:
                raise ValueError("Text markups follow their text and cannot "
                                 "be moved or resized")
            rect = annot.rect
            old = (rect.x0, rect.y0, rect.x1, rect.y1)
            del annot, page_proxy
        self._execute(MoveResizeAnnotCommand(page, uid, old, tuple(new_rect_pts)))

    def delete_annot(self, page: int, uid: str) -> None:
        """Snapshot-based delete: byte-perfect undo for every annot type."""
        def mutate() -> None:
            target, annot = self._locate(page, uid)
            if annot is None:
                raise LookupError(f"annotation {uid} vanished from page {page}")
            target.delete_annot(annot)

        self._execute(SnapshotCommand("Delete annotation", mutate))

    # -- forms -------------------------------------------------------------
    def set_field_value(self, page: int, field_xref: int, value,
                        label: str = "Fill field") -> None:
        with self.document._lock:
            page_proxy, widget = self._locate_widget(page, field_xref)
            if widget is None:
                raise LookupError(f"form field xref {field_xref} not found")
            old = widget.field_value
            del widget, page_proxy
        self._execute(ModifyFieldCommand(page, field_xref, old, value, label))

    # -- redaction ---------------------------------------------------------
    def mark_redaction(self, page: int,
                       rect_pts: tuple[float, float, float, float]) -> str:
        uid = uuid.uuid4().hex

        def build(session: "EditSession") -> None:
            target = session._page(page)
            annot = target.add_redact_annot(D.fitz.Rect(*rect_pts),
                                            fill=(0.0, 0.0, 0.0))
            session._tag_annot(annot, uid)

        self._execute(AddAnnotCommand(page, uid, "Mark redaction", build))
        return uid

    def apply_redactions(self, pages: Optional[Sequence[int]] = None,
                         *, redact_images: bool = False) -> None:
        """Burn the marked areas out of the content.

        Text under a mark is removed, covered image pixels are blanked (the
        rest of the picture survives; ``redact_images`` removes the whole
        image instead) and vector art fully inside a mark is dropped. The
        earlier "images NONE / line art NONE" flags left the original pixels
        and shapes in the saved file under a black box, which is not a
        redaction at all.
        """
        fitz = D.fitz
        image_flag = (fitz.PDF_REDACT_IMAGE_REMOVE if redact_images
                      else fitz.PDF_REDACT_IMAGE_PIXELS)

        def mutate() -> None:
            doc = self.document._doc
            targets = range(doc.page_count) if pages is None else pages
            for index in targets:
                doc[index].apply_redactions(
                    images=image_flag,
                    graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=fitz.PDF_REDACT_TEXT_REMOVE,
                )

        self._execute(SnapshotCommand("Apply redactions", mutate))

    def pending_redactions(self) -> int:
        """Redaction marks on every page, which is what apply removes."""
        with self.document._lock:
            return sum(1 for page in range(self.document.page_count)
                       for info in self.annots_on(page)
                       if info.kind == TYPE_REDACT)

    # -- page operations ----------------------------------------------------
    def rotate_page(self, page: int, degrees: int) -> None:
        def mutate() -> None:
            target = self._page(page)
            target.set_rotation((target.rotation + degrees) % 360)

        self._execute(SnapshotCommand("Rotate page", mutate, structural=True))

    def delete_pages(self, indices: Sequence[int]) -> None:
        indices = sorted(set(indices))
        if len(indices) >= self.document.page_count:
            raise ValueError("cannot delete every page of a document")

        def mutate() -> None:
            self.document._doc.delete_pages(indices)

        label = "Delete page" if len(indices) == 1 else f"Delete {len(indices)} pages"
        self._execute(SnapshotCommand(label, mutate, structural=True))

    def move_page(self, src: int, dst: int) -> None:
        def mutate() -> None:
            self.document._doc.move_page(src, dst)

        self._execute(SnapshotCommand("Move page", mutate, structural=True))

    def duplicate_page(self, page: int) -> None:
        def mutate() -> None:
            doc = self.document._doc
            doc.fullcopy_page(page)
            doc.move_page(doc.page_count - 1, page + 1)

        self._execute(SnapshotCommand("Duplicate page", mutate, structural=True))

    def insert_blank_page(self, at: int, *, width: float = 595.0,
                          height: float = 842.0) -> None:
        def mutate() -> None:
            self.document._doc.new_page(pno=at, width=width, height=height)

        self._execute(SnapshotCommand("Insert blank page", mutate, structural=True))

    def insert_pdf_file(self, path, at: int) -> None:
        def mutate() -> None:
            other = D.fitz.open(str(path))
            try:
                self.document._doc.insert_pdf(other, start_at=at)
            finally:
                other.close()

        self._execute(SnapshotCommand("Insert PDF", mutate, structural=True))

    def merge_append(self, paths: Sequence) -> None:
        def mutate() -> None:
            for path in paths:
                other = D.fitz.open(str(path))
                try:
                    self.document._doc.insert_pdf(other)
                finally:
                    other.close()

        self._execute(SnapshotCommand("Merge PDFs", mutate, structural=True))

    def extract_pages(self, indices: Sequence[int], out_path) -> None:
        """Write selected pages to a new file. Not an edit: no history entry."""
        with self.document._lock:
            out = D.fitz.open()
            try:
                for index in sorted(set(indices)):
                    out.insert_pdf(self.document._doc, from_page=index, to_page=index)
                out.save(str(out_path), garbage=3, deflate=True)
            finally:
                out.close()

    # -- text ---------------------------------------------------------------
    def page_text(self, page: int) -> str:
        """Native text, falling back to OCR for pages with no text layer."""
        native = self.document.text(page)
        if len(native.split()) >= 3 or self.ocr_text_provider is None:
            return native
        try:
            return self.ocr_text_provider(self.document, page) or native
        except Exception:
            return native

    # -- saving -------------------------------------------------------------
    def save_bytes(self) -> bytes:
        """Serialise for an atomic save. Garbage collection is skipped when
        the document carries digital signatures, because rewriting the xref
        table would break them; the UI warns in that case."""
        return self.document.to_bytes(garbage=not self.document.has_signatures)

    def history_top(self) -> Optional[Command]:
        """The command the current document state corresponds to; capture it
        when serialising for a save and hand it to :meth:`mark_saved`."""
        return self._history.undo[-1] if self._history.undo else None

    def mark_saved(self, entry: D.Entry, saved_top=_EVICTED_SAVE_MARKER) -> None:
        """Record a completed save. ``saved_top`` is the history top captured
        by :meth:`history_top` when the bytes were produced; an edit made
        while the write was in flight therefore keeps the session dirty.
        Without it (legacy callers) the current top is assumed."""
        self.entry = entry
        self.document.entry = entry
        self.saved_stamp = entry.stamp
        if saved_top is _EVICTED_SAVE_MARKER:
            saved_top = self.history_top()
        self._saved_top = saved_top
        self._fire("Saved", False)

    def close(self) -> None:
        self._history.undo.clear()
        self._history.redo.clear()
        self.on_change.clear()
        self.document.close()
