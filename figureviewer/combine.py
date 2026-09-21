"""Combine several PDFs into one file (headless core).

Two entry points share this module:

* **In the app**: File > Combine PDFs opens :class:`~figureviewer.dialogs.CombineDialog`,
  where the user adds files and puts them in order (drag, arrows, or A to Z),
  then :func:`combine_pdfs` writes the result.
* **From Explorer**: the "Combine with FigView" verb registered by
  ``register_file_types.ps1`` on every PDF launches the app once per selected
  file with ``--combine``; the launches are funnelled to one running instance
  (:mod:`figureviewer.ipc`), collected for a short settle time and then shown
  in the same dialog, already sorted with :func:`sort_alphabetical`, so the
  order is the one Explorer displays and one click confirms it.

Ordering rule
-------------
:func:`natural_key` mimics Explorer's ``StrCmpLogicalW``: case-insensitive
and with digit runs compared as numbers, so ``chapter2.pdf`` sorts before
``chapter10.pdf``. A plain byte-wise sort would put 10 before 2, which no
user calls "alphabetical".

Output
------
The combined file keeps every page as-is (``Document.insert_pdf``, no
re-rendering) and gains an outline: one top-level bookmark per source file
(its file name) with the source's own bookmarks nested underneath, shifted
to the new page numbers. Writing goes through a ``.part`` file and an
atomic replace, so an interrupted run never leaves a half-written PDF, and
a source may even be chosen as the destination: every source is read into
memory before the destination is touched.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import docs as D

#: Milliseconds the app waits for further ``--combine`` launches before it
#: shows the dialog: Explorer starts one process per selected file.
SHELL_SETTLE_MS = 900
#: Digit runs for the natural sort key.
_DIGITS = re.compile(r"(\d+)")

ProgressFn = Callable[[int, int, str], None]


class CombineError(Exception):
    """A source cannot be combined (missing, not a PDF, encrypted, damaged)."""


@dataclass(frozen=True)
class SourceInfo:
    """What the dialog shows for one source file."""

    path: Path
    pages: int
    size: int
    encrypted: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.encrypted


def natural_key(path: Path | str) -> tuple:
    """Explorer-like sort key: case-insensitive, digit runs as numbers.

    Only the file name takes part, the way a folder listing sorts; ties
    (same name in different folders) fall back to the full path so the
    order is total and stable.
    """
    name = Path(path).name
    parts = _DIGITS.split(name.casefold())
    # Character by character, a digit run counting as one element that
    # ranks where a "0" would: "A.pdf" precedes "a10.pdf" ('.' < '0') and
    # 2 precedes 10 numerically.
    key: list[tuple] = []
    for part in parts:
        if part.isdigit():
            key.append(("0", 1, int(part)))
        else:
            key.extend((char, 0) for char in part)
    return (tuple(key), str(path).casefold())


def sort_alphabetical(paths: Sequence[Path]) -> list[Path]:
    """The paths in Explorer's alphabetical (natural) order, duplicates
    removed while keeping the first occurrence."""
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        identity = os.path.normcase(str(path))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(path)
    return sorted(unique, key=natural_key)


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def inspect(path: Path) -> SourceInfo:
    """Page count, size and lock state of a source, never raising."""
    if not is_pdf(path):
        return SourceInfo(path, 0, 0, error="Not a PDF")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return SourceInfo(path, 0, 0, error=f"Cannot read: {exc.strerror or exc}")
    if not D.HAVE_FITZ:
        return SourceInfo(path, 0, size, error="PyMuPDF is not installed")
    try:
        doc = D.fitz.open(str(path))
    except Exception as exc:
        return SourceInfo(path, 0, size, error=f"Damaged: {exc}")
    try:
        if doc.needs_pass:
            return SourceInfo(path, 0, size, encrypted=True)
        return SourceInfo(path, int(doc.page_count), size)
    finally:
        doc.close()


def default_target(sources: Sequence[Path]) -> Path:
    """A destination beside the first source that does not exist yet:
    ``report + 2 more.pdf``, then ``report + 2 more (2).pdf`` and so on."""
    first = sources[0]
    others = len(sources) - 1
    stem = f"{first.stem} + {others} more" if others else f"{first.stem} combined"
    return unique_path(first.parent / f"{stem}.pdf")


def unique_path(path: Path) -> Path:
    """``path`` itself when free, else ``name (2).pdf``, ``name (3).pdf``."""
    if not path.exists():
        return path
    for number in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def format_size(size: int) -> str:
    return D.format_size(size)


def combine_pdfs(sources: Sequence[Path], target: Path, *,
                 bookmarks: bool = True,
                 progress: Optional[ProgressFn] = None,
                 cancel: Optional[threading.Event] = None) -> int:
    """Write ``sources`` in order into ``target``; return the page count.

    Raises :class:`CombineError` for fewer than two sources, a missing,
    locked or damaged source, or when cancelled (nothing is written then).
    """
    sources = [Path(source) for source in sources]
    if len(sources) < 2:
        raise CombineError("Choose at least two PDF files to combine.")
    if not D.HAVE_FITZ:
        raise CombineError("PyMuPDF is required to combine PDFs.")
    target = Path(target)
    if target.is_dir():
        raise CombineError(f"{target} is a folder.")
    fitz = D.fitz

    # Every source is loaded into memory first: the destination may be one
    # of them, and a failure on file 7 must not leave a truncated result.
    loaded: list[tuple[Path, bytes]] = []
    for index, source in enumerate(sources):
        if cancel is not None and cancel.is_set():
            raise CombineError("Cancelled")
        if progress is not None:
            progress(index, len(sources) * 2, f"Reading {source.name}")
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise CombineError(f"Cannot read {source.name}: {exc.strerror or exc}") from exc
        loaded.append((source, data))

    out = fitz.open()
    toc: list = []
    total = 0
    try:
        for index, (source, data) in enumerate(loaded):
            if cancel is not None and cancel.is_set():
                raise CombineError("Cancelled")
            if progress is not None:
                progress(len(sources) + index, len(sources) * 2, f"Adding {source.name}")
            try:
                doc = fitz.open(stream=data, filetype="pdf")
            except Exception as exc:
                raise CombineError(f"{source.name} is not a readable PDF: {exc}") from exc
            try:
                if doc.needs_pass:
                    raise CombineError(f"{source.name} is password protected; "
                                       "unlock it first.")
                if doc.page_count == 0:
                    continue
                offset = out.page_count
                out.insert_pdf(doc)
                if bookmarks:
                    toc.append([1, source.stem, offset + 1])
                    for level, title, page, *rest in doc.get_toc(simple=True):
                        if page < 1:
                            continue
                        toc.append([int(level) + 1, str(title), offset + int(page)])
            finally:
                doc.close()
        total = out.page_count
        if total == 0:
            raise CombineError("None of the files has any pages.")
        if bookmarks and toc:
            try:
                out.set_toc(_sane_levels(toc))
            except Exception:
                pass                    # an outline is a bonus, never a blocker
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        try:
            out.save(str(part), garbage=3, deflate=True)
            os.replace(part, target)
        except Exception as exc:
            try:
                part.unlink()
            except OSError:
                pass
            raise CombineError(f"Cannot write {target.name}: {exc}") from exc
    finally:
        out.close()
    if progress is not None:
        progress(len(sources) * 2, len(sources) * 2, "Done")
    return total


def _sane_levels(toc: list) -> list:
    """PyMuPDF refuses an outline whose level jumps by more than one; clamp
    each entry to at most one deeper than its predecessor."""
    fixed = []
    previous = 0
    for level, title, page in toc:
        level = max(1, min(int(level), previous + 1))
        fixed.append([level, title, page])
        previous = level
    return fixed
