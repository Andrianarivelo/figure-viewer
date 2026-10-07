"""In-place rich text editing for PDF pages (headless, no tkinter).

This module is the engine behind two edit-mode features:

* **Add text**: the user drags a zone on the page and types inside it.
* **Edit text in place**: a double click on existing page text turns that
  paragraph into live, editable text drawn exactly where it was, in its own
  font, size and colour (no opaque editing bar, no shift after the commit).

Design rules
------------
1. **One layout, two consumers.** :func:`layout_rich` computes every glyph
   position from the real font file metrics. The live preview
   (:class:`PreviewRenderer`) and the commit (:func:`write_layout`) both draw
   from that same :class:`Layout`, so what the user sees while typing is what
   ends up in the PDF, pixel for pixel.
2. **Real fonts, never guesses.** Embedded subset fonts cannot be reused for
   new characters (their glyph tables are stripped), so a PDF font name is
   mapped to the matching installed font file (``Calibri`` -> ``calibri.ttf``)
   or to the PDF base-14 font MuPDF itself used to draw it (Helvetica, Times,
   Courier). :meth:`FontRegistry.match_pdf_font` reports whether the match is
   exact so the UI can say so when it is not.
3. **Background is sacred.** Old glyphs are removed with text-only
   redactions whose ``fill`` is disabled, so coloured panels, figures and
   vector art behind the text survive untouched.
4. **One undo entry per gesture.** Commits run inside
   ``session.checkpoint(label)`` and perform only raw page mutations.
5. **The raw file is never written.** Everything happens on the session's
   in-memory document.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from PIL import Image

from . import docs as D

# ---------------------------------------------------------------------------
# Tunables (PDF points unless stated otherwise)

#: Vertical inset of each redacted line box, so neighbouring lines survive.
LINE_INSET_PTS = 0.4
#: Line pitch used when a block has a single line, as a ratio of font size.
DEFAULT_LINE_HEIGHT = 1.2
#: Every non-final line at least this full (relative to the block width)
#: marks the block as a soft-wrapped paragraph that should reflow on edit.
PARAGRAPH_FILL_RATIO = 0.80
#: Slack added to a paragraph's wrap width so unchanged text keeps its breaks.
WRAP_EPSILON_PTS = 0.75
#: Smallest and largest font size the editor accepts.
MIN_FONT_SIZE = 2.0
MAX_FONT_SIZE = 400.0
#: Undo snapshots kept per editing session.
UNDO_LIMIT = 300
#: Version of the on-disk font index; bump when its layout changes.
FONT_CACHE_VERSION = 1
FONT_EXTENSIONS = (".ttf", ".otf", ".ttc")

FLAG_ITALIC = 1 << 1
FLAG_BOLD = 1 << 4
SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")
ALIGNMENTS = ("left", "center", "right", "justify")

#: PDF base-14 families and their MuPDF codes: regular, bold, italic, both.
BASE14_FAMILIES: dict[str, tuple[str, str, str, str]] = {
    "Helvetica": ("helv", "hebo", "heit", "hebi"),
    "Times": ("tiro", "tibo", "tiit", "tibi"),
    "Courier": ("cour", "cobo", "coit", "cobi"),
}

#: Instant Windows font table: family -> (regular, bold, italic, bold-italic)
#: file names. It lets the editor resolve the common Office fonts at once,
#: before the background scan of the font folders has finished.
KNOWN_WINDOWS_FONTS: dict[str, tuple[str, str, str, str]] = {
    "Arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "Calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "Cambria": ("cambria.ttc", "cambriab.ttf", "cambriai.ttf", "cambriaz.ttf"),
    "Times New Roman": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "Georgia": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "Segoe UI": ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    "Verdana": ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
    "Tahoma": ("tahoma.ttf", "tahomabd.ttf", "", ""),
    "Trebuchet MS": ("trebuc.ttf", "trebucbd.ttf", "trebucit.ttf", "trebucbi.ttf"),
    "Consolas": ("consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf"),
    "Courier New": ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"),
    "Palatino Linotype": ("pala.ttf", "palab.ttf", "palai.ttf", "palabi.ttf"),
    "Garamond": ("GARA.TTF", "GARABD.TTF", "GARAIT.TTF", ""),
    "Book Antiqua": ("BKANT.TTF", "ANTQUAB.TTF", "ANTQUAI.TTF", "ANTQUABI.TTF"),
    "Century Gothic": ("GOTHIC.TTF", "GOTHICB.TTF", "GOTHICI.TTF", "GOTHICBI.TTF"),
    "Comic Sans MS": ("comic.ttf", "comicbd.ttf", "comici.ttf", "comicz.ttf"),
}

#: Families tried, in order, for characters the chosen font cannot draw.
GLYPH_FALLBACK_FAMILIES = ("Segoe UI", "Arial", "DejaVu Sans", "Helvetica")

#: Compact PDF names that MuPDF draws with its own base-14 fonts. Mapping
#: them back to the same base-14 font is the only exact visual match.
BASE14_ALIASES = (
    # MuPDF embeds its metric-identical base-14 clones (URW Nimbus) when a
    # base-14 face is written, so text we wrote maps back to the same family.
    ("nimbussans", "Helvetica"),
    ("nimbusroman", "Times"),
    ("nimbusmono", "Courier"),
    ("helvetica", "Helvetica"),
    ("timesroman", "Times"),
    ("times", "Times"),
    ("courier", "Courier"),
)

#: Trailing tokens stripped from compact PDF font names before matching.
_NAME_SUFFIXES = ("bolditalic", "boldoblique", "regular", "italic", "oblique",
                  "bold", "roman", "psmt", "mt", "ps", "std", "pro")
_STYLE_TAIL_RE = re.compile(
    r"\s+(Regular|Bold Italic|Bold Oblique|Bold|Italic|Oblique)$", re.IGNORECASE)


def compact(name: str) -> str:
    """Lowercase alphanumeric form of a font name, used for matching."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    """``'#rrggbb'`` -> ``(r, g, b)`` floats in 0..1 (black on bad input)."""
    value = (value or "#000000").lstrip("#")
    try:
        return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return (0.0, 0.0, 0.0)


def int_to_hex(value: int) -> str:
    """PyMuPDF sRGB integer colour -> ``'#rrggbb'``."""
    return f"#{max(0, min(int(value), 0xFFFFFF)):06x}"


def default_font_dirs() -> list[Path]:
    """Folders that hold installed fonts on this platform."""
    dirs: list[Path] = []
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs.append(Path(windir) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/Library/Fonts"),
                 Path.home() / "Library" / "Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                 Path.home() / ".fonts", Path.home() / ".local/share/fonts"]
    return [d for d in dirs if d.is_dir()]


# ---------------------------------------------------------------------------
# Fonts


@dataclass(eq=False)
class FontFace:
    """One concrete font face: a font file or a base-14 code.

    ``font`` (a :class:`fitz.Font`) is created lazily and cached, because the
    same object must be reused for every write: MuPDF de-duplicates embedded
    fonts by object, so reusing it keeps the PDF from growing on each edit.
    """

    family: str
    bold: bool
    italic: bool
    path: Optional[str] = None
    base14: Optional[str] = None
    _font: object = field(default=None, repr=False)
    _glyphs: dict = field(default_factory=dict, repr=False)

    @property
    def font(self):
        if self._font is None:
            fitz = D.fitz
            if self.path:
                self._font = fitz.Font(fontfile=self.path)
            else:
                self._font = fitz.Font(self.base14 or "helv")
        return self._font

    @property
    def embeds(self) -> bool:
        """True when writing with this face embeds a font file."""
        return self.path is not None

    def has_glyph(self, char: str) -> bool:
        """Whether the face can draw ``char`` (cached per character)."""
        cached = self._glyphs.get(char)
        if cached is None:
            try:
                cached = bool(self.font.has_glyph(ord(char))) or char in " \t"
            except Exception:
                cached = False
            self._glyphs[char] = cached
        return cached

    @property
    def ascender(self) -> float:
        try:
            return float(self.font.ascender) or 0.8
        except Exception:
            return 0.8

    @property
    def descender(self) -> float:
        try:
            return float(self.font.descender) or -0.2
        except Exception:
            return -0.2


class FontRegistry:
    """Installed font families plus the three PDF base-14 families.

    Construction is instant: the base-14 families and the known Windows
    files are available immediately. :meth:`start_scan` indexes every font
    folder in a background thread (about 1.5 s for 300 files on first run,
    milliseconds afterwards thanks to the JSON cache keyed by file mtime).
    """

    def __init__(self, cache_path: Optional[Path] = None,
                 font_dirs: Optional[Sequence[Path]] = None) -> None:
        self.cache_path = Path(cache_path) if cache_path else None
        self.font_dirs = (list(font_dirs) if font_dirs is not None
                          else default_font_dirs())
        self._lock = threading.Lock()
        # display family -> {(bold, italic): path}
        self._families: dict[str, dict[tuple[bool, bool], str]] = {}
        self._faces: dict[tuple[str, bool, bool], FontFace] = {}
        self._scan_thread: Optional[threading.Thread] = None
        self.scanned = False
        self._seed_known_files()

    # -- index building ----------------------------------------------------
    def _seed_known_files(self) -> None:
        """Register the known Windows font files that exist on this machine."""
        for folder in self.font_dirs:
            for family, files in KNOWN_WINDOWS_FONTS.items():
                for slot, name in enumerate(files):
                    if not name:
                        continue
                    path = folder / name
                    if path.is_file():
                        key = (bool(slot & 1), bool(slot & 2))
                        self._families.setdefault(family, {}).setdefault(
                            key, str(path))

    def start_scan(self, on_done: Optional[Callable[[], None]] = None
                   ) -> Optional[threading.Thread]:
        """Index the font folders in a daemon thread (idempotent)."""
        if self.scanned or self._scan_thread is not None:
            return self._scan_thread

        def run() -> None:
            try:
                self.scan()
            finally:
                if on_done is not None:
                    try:
                        on_done()
                    except Exception:
                        pass

        self._scan_thread = threading.Thread(target=run, name="font-scan",
                                             daemon=True)
        self._scan_thread.start()
        return self._scan_thread

    def scan(self) -> None:
        """Blocking scan of every font folder, using the cache when valid."""
        cache = self._read_cache()
        fresh: dict[str, list] = {}
        families: dict[str, dict[tuple[bool, bool], str]] = {}
        fitz = D.fitz
        for folder in self.font_dirs:
            for root, _dirs, files in os.walk(folder):
                for name in files:
                    if not name.lower().endswith(FONT_EXTENSIONS):
                        continue
                    path = os.path.join(root, name)
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    entry = cache.get(path)
                    if not (entry and entry[0] == stat.st_mtime
                            and entry[1] == stat.st_size):
                        try:
                            font = fitz.Font(fontfile=path)
                            entry = [stat.st_mtime, stat.st_size, font.name,
                                     bool(font.is_bold), bool(font.is_italic)]
                        except Exception:
                            entry = [stat.st_mtime, stat.st_size, "", False, False]
                    fresh[path] = entry
                    full_name = str(entry[2] or "").strip()
                    if not full_name:
                        continue
                    family = family_from_full_name(full_name)
                    bold = bool(entry[3]) or "bold" in full_name.lower()
                    italic = bool(entry[4]) or any(
                        token in full_name.lower() for token in ("italic", "oblique"))
                    faces = families.setdefault(family, {})
                    faces.setdefault((bold, italic), path)
        with self._lock:
            # Known files first so their canonical names win on duplicates.
            for family, faces in self._families.items():
                merged = families.setdefault(family, {})
                for key, path in faces.items():
                    merged.setdefault(key, path)
            self._families = families
            self.scanned = True
        self._write_cache(fresh)

    def _read_cache(self) -> dict:
        if self.cache_path is None or not self.cache_path.is_file():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict) or data.get("version") != FONT_CACHE_VERSION:
            return {}
        files = data.get("files")
        return files if isinstance(files, dict) else {}

    def _write_cache(self, files: dict) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": FONT_CACHE_VERSION,
                                       "files": files}), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except Exception:
            pass

    # -- queries -------------------------------------------------------------
    def families(self) -> list[str]:
        """Every selectable family, base-14 first, then alphabetical."""
        with self._lock:
            installed = sorted(self._families, key=str.lower)
        return list(BASE14_FAMILIES) + [f for f in installed
                                        if f not in BASE14_FAMILIES]

    def canonical_family(self, name: str) -> Optional[str]:
        """The registry's spelling of ``name`` (case/space-insensitive)."""
        key = compact(name)
        if not key:
            return None
        for family in BASE14_FAMILIES:
            if compact(family) == key:
                return family
        with self._lock:
            for family in self._families:
                if compact(family) == key:
                    return family
        return None

    def face(self, family: str, bold: bool = False, italic: bool = False) -> FontFace:
        """The best face for a family/style; never raises.

        Unknown families resolve to Helvetica. A missing style (say, no bold
        file) falls back to the nearest existing face of the same family.
        """
        canonical = self.canonical_family(family) or "Helvetica"
        cache_key = (canonical, bool(bold), bool(italic))
        cached = self._faces.get(cache_key)
        if cached is not None:
            return cached
        if canonical in BASE14_FAMILIES:
            code = BASE14_FAMILIES[canonical][int(bool(bold)) + 2 * int(bool(italic))]
            face = FontFace(canonical, bool(bold), bool(italic), base14=code)
        else:
            with self._lock:
                faces = dict(self._families.get(canonical, {}))
            path = None
            for key in ((bool(bold), bool(italic)), (bool(bold), False),
                        (False, bool(italic)), (False, False)):
                if key in faces:
                    path = faces[key]
                    break
            if path is None and faces:
                path = next(iter(faces.values()))
            if path is None:
                return self.face("Helvetica", bold, italic)
            face = FontFace(canonical, bool(bold), bool(italic), path=path)
        self._faces[cache_key] = face
        return face

    def fallback_faces(self, bold: bool, italic: bool) -> list[FontFace]:
        """Faces tried for characters the chosen face cannot draw."""
        faces = []
        for family in GLYPH_FALLBACK_FAMILIES:
            if self.canonical_family(family):
                faces.append(self.face(family, bold, italic))
        return faces

    def match_pdf_font(self, pdf_name: str, flags: int = 0
                       ) -> tuple[str, bool, bool, bool]:
        """Map an embedded PDF font name to ``(family, bold, italic, exact)``.

        ``exact`` is False when no installed family matched and a generic
        serif/sans substitute was chosen; the UI should mention it.
        """
        bare = SUBSET_PREFIX_RE.sub("", pdf_name or "")
        lower = bare.lower()
        bold = bool(flags & FLAG_BOLD) or any(t in lower for t in ("bold", "black", "heavy"))
        italic = bool(flags & FLAG_ITALIC) or any(t in lower for t in ("italic", "oblique"))
        full = compact(re.split(r"[-,]", bare)[0])
        # Progressive suffix stripping, keeping every stage as a candidate:
        # "TimesNewRomanPS" must be tried as "timesnewroman" BEFORE "roman"
        # is stripped too (stripping greedily turned it into "timesnew").
        candidates = [full]
        base = full
        changed = True
        while changed and base:
            changed = False
            for suffix in _NAME_SUFFIXES:
                if base.endswith(suffix) and len(base) > len(suffix):
                    base = base[: -len(suffix)]
                    candidates.append(base)
                    changed = True
                    break
        with self._lock:
            installed = {compact(f): f for f in self._families}
        for candidate in candidates:
            if candidate in installed:
                return installed[candidate], bold, italic, True
        # Longest installed family that prefixes the PDF name (TimesNewRomanPS).
        best = ""
        for key in installed:
            if key and full.startswith(key) and len(key) > len(best):
                best = key
        if best:
            return installed[best], bold, italic, True
        for token, family in BASE14_ALIASES:
            if base.startswith(token):
                return family, bold, italic, True
        if "arial" in base and "Arial" in self._families:
            return "Arial", bold, italic, False
        serif = any(t in lower for t in ("serif", "roman", "times", "garamond",
                                         "georgia", "minion", "book"))
        if "sans" in lower:
            serif = False
        return ("Times" if serif else "Helvetica"), bold, italic, False


def family_from_full_name(full_name: str) -> str:
    """``'Calibri Bold Italic'`` -> ``'Calibri'`` (keeps 'Light' etc.)."""
    family = full_name.strip()
    while True:
        stripped = _STYLE_TAIL_RE.sub("", family)
        if stripped == family:
            return family or full_name
        family = stripped


_shared_registry: Optional[FontRegistry] = None


def shared_registry(cache_path: Optional[Path] = None) -> FontRegistry:
    """Process-wide registry, so the font index is built only once."""
    global _shared_registry
    if _shared_registry is None:
        _shared_registry = FontRegistry(cache_path=cache_path)
    return _shared_registry


# ---------------------------------------------------------------------------
# Rich text model


@dataclass(frozen=True)
class CharStyle:
    """Formatting of one character. Immutable so runs can share instances."""

    family: str = "Helvetica"
    size: float = 11.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str = "#000000"


def clamp_size(value: float) -> float:
    return max(MIN_FONT_SIZE, min(float(value), MAX_FONT_SIZE))


class RichText:
    """Editable text with one :class:`CharStyle` per character.

    Holds the caret, the selection anchor and an undo history. Every method
    is pure Python so the editing semantics are unit-testable without Tk.
    Paragraph-level formatting (alignment, line height) lives here too.
    """

    def __init__(self, text: str = "", styles: Optional[Sequence[CharStyle]] = None,
                 *, base_style: Optional[CharStyle] = None, align: str = "left",
                 line_height: float = DEFAULT_LINE_HEIGHT) -> None:
        self.text = text
        self.base_style = base_style or CharStyle()
        self.styles: list[CharStyle] = (list(styles) if styles is not None
                                        else [self.base_style] * len(text))
        if len(self.styles) != len(self.text):
            raise ValueError("one style per character is required")
        self.align = align if align in ALIGNMENTS else "left"
        self.line_height = float(line_height)
        self.caret = len(text)
        self.anchor = len(text)
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._coalesce: Optional[str] = None   # kind of the last edit

    # -- selection ----------------------------------------------------------
    def selection(self) -> tuple[int, int]:
        return (min(self.caret, self.anchor), max(self.caret, self.anchor))

    @property
    def has_selection(self) -> bool:
        return self.caret != self.anchor

    def selected_text(self) -> str:
        a, b = self.selection()
        return self.text[a:b]

    def set_caret(self, index: int, *, extend: bool = False) -> None:
        self.caret = max(0, min(int(index), len(self.text)))
        if not extend:
            self.anchor = self.caret
        self._coalesce = None

    def select_all(self) -> None:
        self.anchor, self.caret = 0, len(self.text)
        self._coalesce = None

    def select_range(self, start: int, end: int) -> None:
        self.anchor = max(0, min(start, len(self.text)))
        self.caret = max(0, min(end, len(self.text)))
        self._coalesce = None

    def word_bounds(self, index: int) -> tuple[int, int]:
        """Bounds of the word (or run of spaces/punctuation) at ``index``."""
        text = self.text
        if not text:
            return (0, 0)
        index = max(0, min(index, len(text) - 1))
        kind = _char_class(text[index])
        start = index
        while start > 0 and _char_class(text[start - 1]) == kind:
            start -= 1
        end = index
        while end < len(text) and _char_class(text[end]) == kind:
            end += 1
        return (start, end)

    def word_left(self, index: int) -> int:
        """Start of the word before ``index`` (Ctrl+Left semantics)."""
        text = self.text
        i = index
        while i > 0 and _char_class(text[i - 1]) == "space":
            i -= 1
        if i > 0:
            kind = _char_class(text[i - 1])
            while i > 0 and _char_class(text[i - 1]) == kind:
                i -= 1
        return i

    def word_right(self, index: int) -> int:
        """End of the word after ``index`` (Ctrl+Right semantics)."""
        text = self.text
        i = index
        if i < len(text):
            kind = _char_class(text[i])
            while i < len(text) and _char_class(text[i]) == kind:
                i += 1
        while i < len(text) and _char_class(text[i]) == "space":
            i += 1
        return i

    # -- styles -------------------------------------------------------------
    def style_at(self, index: int) -> CharStyle:
        """Style a character typed at ``index`` would get.

        Like word processors, typing continues the style of the character
        before the caret; at the very start it takes the following one.
        """
        if self.has_selection:
            a, _b = self.selection()
            return self.styles[a] if a < len(self.styles) else self.base_style
        if index > 0 and index - 1 < len(self.styles):
            return self.styles[index - 1]
        if self.styles:
            return self.styles[0]
        return self.base_style

    def apply_style(self, **changes) -> None:
        """Change formatting of the selection, or of everything if none.

        Applying to the whole box when nothing is selected matches how a
        text box is usually restyled ("make this label bigger"); it also
        updates ``base_style`` so text typed into an empty box follows.
        """
        if not changes:
            return
        if "size" in changes:
            changes["size"] = clamp_size(changes["size"])
        self._push_undo("style")
        a, b = self.selection() if self.has_selection else (0, len(self.text))
        cache: dict[CharStyle, CharStyle] = {}
        for i in range(a, b):
            old = self.styles[i]
            new = cache.get(old)
            if new is None:
                new = cache[old] = replace(old, **changes)
            self.styles[i] = new
        if not self.has_selection:
            self.base_style = replace(self.base_style, **changes)
        self._coalesce = None

    def toggle_flag(self, name: str) -> None:
        """Toggle bold/italic/underline: on unless the target is all on."""
        a, b = self.selection() if self.has_selection else (0, len(self.text))
        styles = self.styles[a:b] or [self.style_at(self.caret)]
        turn_on = not all(getattr(s, name) for s in styles)
        self.apply_style(**{name: turn_on})

    def set_align(self, align: str) -> None:
        if align in ALIGNMENTS and align != self.align:
            self._push_undo("align")
            self.align = align
            self._coalesce = None

    # -- editing ------------------------------------------------------------
    def insert(self, chars: str, style: Optional[CharStyle] = None) -> None:
        """Type or paste ``chars`` at the caret, replacing any selection."""
        chars = normalize_text(chars)
        if not chars and not self.has_selection:
            return
        kind = "type" if len(chars) == 1 and chars not in " \n" else "insert"
        style = style or self.style_at(self.caret)
        self._push_undo(kind)
        a, b = self.selection()
        self.text = self.text[:a] + chars + self.text[b:]
        self.styles[a:b] = [style] * len(chars)
        self.caret = self.anchor = a + len(chars)

    def delete_backward(self, *, word: bool = False) -> None:
        if self.has_selection:
            self._delete_selection()
            return
        if self.caret == 0:
            return
        start = self.word_left(self.caret) if word else self.caret - 1
        self._push_undo("delete")
        self.text = self.text[:start] + self.text[self.caret:]
        del self.styles[start:self.caret]
        self.caret = self.anchor = start

    def delete_forward(self, *, word: bool = False) -> None:
        if self.has_selection:
            self._delete_selection()
            return
        if self.caret >= len(self.text):
            return
        end = self.word_right(self.caret) if word else self.caret + 1
        self._push_undo("delete")
        self.text = self.text[:self.caret] + self.text[end:]
        del self.styles[self.caret:end]
        self.anchor = self.caret

    def _delete_selection(self) -> None:
        a, b = self.selection()
        self._push_undo("cut")
        self.text = self.text[:a] + self.text[b:]
        del self.styles[a:b]
        self.caret = self.anchor = a

    def cut(self) -> str:
        chunk = self.selected_text()
        if chunk:
            self._delete_selection()
        return chunk

    # -- history ------------------------------------------------------------
    def _snapshot(self) -> tuple:
        return (self.text, list(self.styles), self.caret, self.anchor,
                self.align, self.line_height, self.base_style)

    def _restore(self, snap: tuple) -> None:
        (self.text, styles, self.caret, self.anchor, self.align,
         self.line_height, self.base_style) = snap
        self.styles = list(styles)

    def _push_undo(self, kind: str) -> None:
        # Consecutive single-character typing collapses into one undo step.
        if kind == "type" and self._coalesce == "type":
            return
        self._undo.append(self._snapshot())
        if len(self._undo) > UNDO_LIMIT:
            del self._undo[0]
        self._redo.clear()
        self._coalesce = kind

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        self._coalesce = None
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        self._coalesce = None
        return True

    def same_content(self, other_text: str, other_styles: Sequence[CharStyle],
                     other_align: str) -> bool:
        return (self.text == other_text and self.align == other_align
                and list(self.styles) == list(other_styles))


def _char_class(char: str) -> str:
    if char.isspace():
        return "space"
    if char.isalnum() or char == "_":
        return "word"
    return "punct"


def normalize_text(text: str) -> str:
    """Typed/pasted text -> model text: NBSP to space, tabs to 4 spaces,
    CR/LF variants to ``\\n``, other control characters dropped."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\t", "    ")
    return "".join(c for c in text if c == "\n" or c >= " ")


# ---------------------------------------------------------------------------
# Layout


@dataclass
class Run:
    """A same-face, same-style glyph run placed in layout coordinates."""

    x: float
    baseline: float
    text: str
    face: FontFace
    style: CharStyle
    width: float


@dataclass
class LineLayout:
    """One visual line. ``xs`` holds ``end - start + 1`` caret positions."""

    start: int
    end: int                 # exclusive; excludes the hard newline
    xs: list[float]
    baseline: float
    ascent: float
    descent: float
    hard: bool               # ends with a newline or the end of the text
    content_width: float     # without trailing spaces

    @property
    def top(self) -> float:
        return self.baseline - self.ascent

    @property
    def bottom(self) -> float:
        return self.baseline + self.descent


@dataclass
class Layout:
    """Positions of every character, relative to the first baseline origin.

    ``x = 0`` is the box's left edge, ``y = 0`` the first line's baseline;
    ``top`` is therefore negative (the first line's ascent).
    """

    lines: list[LineLayout]
    width: float
    faces: list[FontFace]
    styles: list[CharStyle]
    text: str
    align: str

    @property
    def top(self) -> float:
        return self.lines[0].top if self.lines else 0.0

    @property
    def bottom(self) -> float:
        return self.lines[-1].bottom if self.lines else 0.0

    @property
    def extent_x(self) -> tuple[float, float]:
        """Horizontal ink extent (at least the box width)."""
        lo = 0.0
        hi = self.width
        for line in self.lines:
            if line.xs:
                lo = min(lo, line.xs[0])
                hi = max(hi, line.xs[-1])
        return lo, hi

    def line_index_of(self, index: int) -> int:
        """Visual line holding caret ``index`` (soft breaks go forward)."""
        for i, line in enumerate(self.lines):
            if index < line.start:
                return max(0, i - 1)
            if line.start <= index <= line.end:
                if index == line.end and not line.hard and i + 1 < len(self.lines):
                    return i + 1
                return i
        return max(0, len(self.lines) - 1)

    def caret_geometry(self, index: int) -> tuple[float, float, float, float]:
        """``(x, baseline, ascent, descent)`` of the caret at ``index``."""
        if not self.lines:
            return (0.0, 0.0, 8.0, 2.0)
        line = self.lines[self.line_index_of(index)]
        offset = max(0, min(index - line.start, len(line.xs) - 1))
        x = line.xs[offset]
        # Caret height follows the character before it (the style being typed).
        ref = index - 1 if index > line.start else index
        if 0 <= ref < len(self.styles) and ref < len(self.faces):
            size = self.styles[ref].size
            face = self.faces[ref]
            return (x, line.baseline, face.ascender * size, -face.descender * size)
        return (x, line.baseline, line.ascent, line.descent)

    def index_at(self, x: float, y: float) -> int:
        """Caret index nearest to a layout point (click hit-testing)."""
        if not self.lines:
            return 0
        line = self.lines[-1]
        for candidate in self.lines:
            if y <= candidate.bottom:
                line = candidate
                break
        return self.index_in_line(line, x)

    def index_in_line(self, line: LineLayout, x: float) -> int:
        best = 0
        best_dist = float("inf")
        for offset, cx in enumerate(line.xs):
            dist = abs(cx - x)
            if dist < best_dist:
                best, best_dist = offset, dist
        return line.start + best

    def selection_rects(self, a: int, b: int) -> list[tuple[float, float, float, float]]:
        """Highlight rectangles for the character range ``[a, b)``."""
        rects = []
        if a >= b:
            return rects
        for line in self.lines:
            lo = max(a, line.start)
            hi = min(b, line.end)
            spans_newline = line.hard and a <= line.end < b and line.end < len(self.text)
            if lo > hi or (lo == hi and not spans_newline):
                continue
            x0 = line.xs[lo - line.start]
            x1 = line.xs[hi - line.start]
            if spans_newline:
                x1 += max(2.0, line.ascent * 0.3)   # show the selected newline
            rects.append((x0, line.top, x1, line.bottom))
        return rects

    def runs(self) -> list[Run]:
        """Glyph runs to draw, split at style/face changes.

        Justified lines are also split at their stretched spaces, so each
        word is placed at its own position (PDF text has no word spacing
        we could rely on across viewers).
        """
        result: list[Run] = []
        text = self.text
        for line in self.lines:
            stretched = self.align == "justify" and not line.hard
            run_start: Optional[int] = None

            def flush(stop: int) -> None:
                chunk = text[run_start:stop].rstrip(" ")
                if chunk.strip():
                    first = run_start - line.start
                    x = line.xs[first]
                    width = line.xs[first + len(chunk)] - x
                    result.append(Run(x, line.baseline, chunk,
                                      self.faces[run_start],
                                      self.styles[run_start], width))

            for i in range(line.start, line.end):
                splits_here = stretched and text[i] == " "
                if run_start is not None and (
                        splits_here or self.faces[i] is not self.faces[run_start]
                        or self.styles[i] != self.styles[run_start]):
                    flush(i)
                    run_start = None
                if run_start is None and not splits_here:
                    run_start = i
            if run_start is not None:
                flush(line.end)
        return result


def layout_rich(text: str, styles: Sequence[CharStyle], registry: FontRegistry,
                *, align: str = "left", line_height: float = DEFAULT_LINE_HEIGHT,
                wrap_width: Optional[float] = None,
                fixed_baselines: Optional[Sequence[float]] = None,
                indents: Optional[Sequence[float]] = None,
                empty_style: Optional[CharStyle] = None) -> Layout:
    """Lay out rich text with real font metrics.

    ``wrap_width`` None means "grow to the longest line" (click-to-type
    labels); otherwise lines wrap greedily at spaces, and words longer than
    the width break between characters. ``fixed_baselines`` pins the first
    lines to measured baselines of the original PDF text (relative to the
    first one), and ``indents`` reproduces per-line left indents, so an
    edited block keeps its exact geometry where its lines are unchanged.
    """
    empty_style = empty_style or (styles[0] if styles else CharStyle())
    faces = _resolve_faces(text, styles, registry)
    advances = _advances(text, styles, faces)

    # 1) break into visual lines: (start, end, hard)
    breaks: list[tuple[int, int, bool]] = []
    paragraph_start = 0
    n = len(text)
    while paragraph_start <= n:
        nl = text.find("\n", paragraph_start)
        paragraph_end = n if nl < 0 else nl
        _wrap_paragraph(paragraph_start, paragraph_end, text, advances,
                        wrap_width, indents, breaks)
        if nl < 0:
            break
        paragraph_start = nl + 1

    # 2) measure, align and place each line
    content_widths = []
    for (start, end, _hard) in breaks:
        stop = end
        while stop > start and text[stop - 1] == " ":
            stop -= 1
        content_widths.append(sum(advances[start:stop]))
    if wrap_width is not None:
        box_width = float(wrap_width)
    else:
        box_width = 0.0
        for k, w in enumerate(content_widths):
            box_width = max(box_width, w + _indent(indents, k))
    lines: list[LineLayout] = []
    baseline = 0.0
    for k, (start, end, hard) in enumerate(breaks):
        sizes = [styles[i].size for i in range(start, end)] or [
            (styles[start - 1].size if 0 < start <= len(styles) else empty_style.size)]
        line_faces = [faces[i] for i in range(start, end)]
        if not line_faces:
            ref = start - 1 if start > 0 else None
            if ref is not None and ref < len(faces):
                line_faces = [faces[ref]]
                sizes = [styles[ref].size]
            else:
                line_faces = [registry.face(empty_style.family, empty_style.bold,
                                            empty_style.italic)]
                sizes = [empty_style.size]
        ascent = max(f.ascender * s for f, s in zip(line_faces, sizes))
        descent = max(-f.descender * s for f, s in zip(line_faces, sizes))
        max_size = max(sizes)
        if k == 0:
            baseline = 0.0
        elif fixed_baselines is not None and k < len(fixed_baselines):
            baseline = float(fixed_baselines[k])
        else:
            # Leading follows the tallest text on the line being placed.
            baseline += line_height * max_size
        indent = _indent(indents, k)
        width_here = content_widths[k]
        free = box_width - indent - width_here
        internal_spaces = [i for i in range(start, end) if text[i] == " "
                           and i < start + _content_len(text, start, end)]
        stretch = 0.0
        if align == "justify" and not hard and internal_spaces and free > 0:
            stretch = free / len(internal_spaces)
            offset = indent
        elif align == "center":
            offset = indent + free / 2.0
        elif align == "right":
            offset = indent + free
        else:
            offset = indent
        xs = [offset]
        x = offset
        for i in range(start, end):
            x += advances[i]
            if stretch and text[i] == " " and i in internal_spaces:
                x += stretch
            xs.append(x)
        lines.append(LineLayout(start, end, xs, baseline, ascent, descent,
                                hard, width_here))
    return Layout(lines, box_width, faces, list(styles), text, align)


def _indent(indents: Optional[Sequence[float]], k: int) -> float:
    if indents is not None and k < len(indents):
        return float(indents[k])
    return 0.0


def _content_len(text: str, start: int, end: int) -> int:
    stop = end
    while stop > start and text[stop - 1] == " ":
        stop -= 1
    return stop - start


def _wrap_paragraph(start: int, end: int, text: str, advances: Sequence[float],
                    wrap_width: Optional[float], indents, out: list) -> None:
    """Greedy wrap of ``text[start:end]`` appended to ``out``."""
    if wrap_width is None or start == end:
        out.append((start, end, True))
        return
    line_start = start
    x = 0.0
    last_break = None            # index just after the last space
    i = start
    while i < end:
        # ``out`` accumulates every line so far, so its length is the
        # global index of the line being filled (indents are per line).
        limit = wrap_width - _indent(indents, len(out))
        char = text[i]
        if char == " ":
            x += advances[i]
            last_break = i + 1
            i += 1
            continue
        if x + advances[i] > limit + 1e-6 and i > line_start:
            if last_break is not None and last_break > line_start:
                cut = last_break
            else:
                cut = i
            out.append((line_start, cut, False))
            line_start = cut
            x = sum(advances[line_start:i])
            last_break = None
            continue
        x += advances[i]
        i += 1
    out.append((line_start, end, True))


def _resolve_faces(text: str, styles: Sequence[CharStyle],
                   registry: FontRegistry) -> list[FontFace]:
    """Per-character face, falling back for glyphs the face cannot draw."""
    faces: list[FontFace] = []
    cache: dict[CharStyle, FontFace] = {}
    for char, style in zip(text, styles):
        face = cache.get(style)
        if face is None:
            face = cache[style] = registry.face(style.family, style.bold, style.italic)
        if char not in "\n " and not face.has_glyph(char):
            for alt in registry.fallback_faces(style.bold, style.italic):
                if alt.has_glyph(char):
                    face = alt
                    break
        faces.append(face)
    return faces


def _advances(text: str, styles: Sequence[CharStyle],
              faces: Sequence[FontFace]) -> list[float]:
    """Advance width of every character, measured in same-face segments."""
    advances = [0.0] * len(text)
    i = 0
    n = len(text)
    while i < n:
        j = i + 1
        while j < n and faces[j] is faces[i] and styles[j].size == styles[i].size:
            j += 1
        segment = text[i:j]
        try:
            widths = faces[i].font.char_lengths(segment.replace("\n", " "),
                                                fontsize=styles[i].size)
        except Exception:
            widths = [styles[i].size * 0.5] * len(segment)
        for k, w in enumerate(widths):
            advances[i + k] = 0.0 if segment[k] == "\n" else float(w)
        i = j
    return advances


# ---------------------------------------------------------------------------
# Drawing (shared by preview and commit)


def write_layout(page, layout: Layout, origin: tuple[float, float],
                 *, opacity: float = 1.0) -> None:
    """Draw ``layout`` on a PyMuPDF page with its first baseline at ``origin``.

    One TextWriter per colour (a TextWriter has a single colour). Coordinates
    are in the page's displayed space; TextWriter handles ``/Rotate`` itself
    (verified on 0/90/180/270), so the text always reads upright on screen.
    """
    fitz = D.fitz
    ox, oy = origin
    writers: dict[str, object] = {}
    underlines: list[tuple[float, float, float, float, str]] = []
    for run in layout.runs():
        writer = writers.get(run.style.color)
        if writer is None:
            writer = writers[run.style.color] = fitz.TextWriter(page.rect)
        writer.append((ox + run.x, oy + run.baseline), run.text,
                      font=run.face.font, fontsize=run.style.size)
        if run.style.underline:
            y = oy + run.baseline + run.style.size * 0.12
            underlines.append((ox + run.x, y, ox + run.x + run.width,
                               max(0.5, run.style.size * 0.06), run.style.color))
    for color, writer in writers.items():
        writer.write_text(page, color=hex_to_rgb(color),
                          opacity=opacity if opacity < 1.0 else -1)
    if underlines:
        # Shapes use unrotated coordinates; map displayed points back.
        matrix = page.derotation_matrix if page.rotation else None
        for x0, y, x1, width, color in underlines:
            p0, p1 = fitz.Point(x0, y), fitz.Point(x1, y)
            if matrix is not None:
                p0, p1 = p0 * matrix, p1 * matrix
            page.draw_line(p0, p1, color=hex_to_rgb(color), width=width)


class PreviewRenderer:
    """Renders a layout to a transparent RGBA image with MuPDF.

    A private scratch document is reused across keystrokes: fonts are
    embedded into it once, and each render only rebuilds one small page.
    About 1-2 ms per render for a paragraph, so it runs on every keystroke.
    """

    def __init__(self) -> None:
        self._doc = D.fitz.open()

    def render(self, layout: Layout, zoom: float, *, pad: float = 2.0
               ) -> tuple[Image.Image, float, float]:
        """Return ``(image, left, top)``: image and its layout-space corner."""
        fitz = D.fitz
        lo, hi = layout.extent_x
        left = lo - pad
        top = layout.top - pad
        width = max(1.0, hi - lo + 2 * pad)
        height = max(1.0, layout.bottom - layout.top + 2 * pad)
        if self._doc.page_count:
            self._doc.delete_page(0)
        page = self._doc.new_page(width=width, height=height)
        write_layout(page, layout, (-left, -top))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True,
                              annots=False)
        # MuPDF samples are premultiplied; "RGBa" tells Pillow exactly that.
        image = Image.frombytes("RGBa", (pix.width, pix.height),
                                pix.samples).convert("RGBA")
        return image, left, top

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:
            pass


class ErasedPage:
    """A one-page scratch copy of a page with some text lines removed.

    Shown under the live editor so the original glyphs disappear while the
    background (fills, figures, rules, annotations) stays exactly as is.
    The redaction runs once when the editor opens; :meth:`render` is then a
    plain clip render (a few ms), cheap enough to follow every pan and zoom.
    The session document itself is never touched.
    """

    def __init__(self, document, page: int, erase_rects: Sequence[tuple]) -> None:
        fitz = D.fitz
        self._doc = fitz.open()
        with document._lock:
            self._doc.insert_pdf(document._doc, from_page=page, to_page=page)
        target = self._doc[0]
        for rect in erase_rects:
            target.add_redact_annot(fitz.Rect(*rect), fill=False)
        if erase_rects:
            target.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                    text=fitz.PDF_REDACT_TEXT_REMOVE)

    def render(self, clip: tuple[float, float, float, float], zoom: float,
               shift: tuple[float, float] = (0.0, 0.0)
               ) -> tuple[Image.Image, int, int]:
        """Render ``clip`` with ``point * zoom + shift`` as the pixel mapping.

        Passing the viewer's own mapping (zoom and canvas offset) makes the
        pixmap land on whole canvas pixels: the returned ``(x, y)`` is its
        top-left canvas pixel, so it overlays the page image seamlessly.
        """
        fitz = D.fitz
        matrix = fitz.Matrix(zoom, 0, 0, zoom, shift[0], shift[1])
        pix = self._doc[0].get_pixmap(matrix=matrix, clip=fitz.Rect(*clip),
                                      alpha=False, colorspace=fitz.csRGB,
                                      annots=True)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return image, pix.x, pix.y

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Seeding from existing page text


@dataclass
class BlockSeed:
    """Everything needed to edit one existing text block in place."""

    text: str
    styles: list[CharStyle]
    align: str
    line_height: float
    origin: tuple[float, float]          # first baseline origin, page points
    wrap_width: Optional[float]          # None: lines kept as typed
    fixed_baselines: list[float]
    indents: list[float]
    erase_rects: list[tuple[float, float, float, float]]
    bbox: tuple[float, float, float, float]
    paragraph: bool
    inexact_fonts: list[str]             # PDF fonts with no installed match
    mixed: bool                          # more than one style in the block


def seed_from_block(block, registry: FontRegistry) -> BlockSeed:
    """Build an editable model from a :class:`reflow.TextBlock`.

    Decides between *paragraph* mode (soft-wrapped lines reflow on edit) and
    *lines* mode (each PDF line is a hard line, e.g. labels and lists), and
    verifies paragraph mode reproduces the original breaks with the matched
    font; if it does not, lines mode is used so unchanged text never moves.
    """
    from . import reflow

    lines = [line for line in block.lines if line.spans]
    if not lines:
        raise ValueError("this text block is empty")
    bx0, by0, bx1, by1 = block.bbox
    block_width = bx1 - bx0
    inexact: set[str] = set()
    style_cache: dict[tuple, CharStyle] = {}

    def style_of(span) -> CharStyle:
        key = (span.font, span.flags, round(span.size, 2), span.color)
        cached = style_cache.get(key)
        if cached is None:
            family, bold, italic, exact = registry.match_pdf_font(span.font, span.flags)
            if not exact:
                inexact.add(SUBSET_PREFIX_RE.sub("", span.font))
            cached = style_cache[key] = CharStyle(
                family=family, size=round(float(span.size), 2) or 11.0,
                bold=bold, italic=italic, color=int_to_hex(span.color))
        return cached

    line_texts: list[str] = []
    line_styles: list[list[CharStyle]] = []
    for line in lines:
        chars: list[str] = []
        sts: list[CharStyle] = []
        for span in line.spans:
            st = style_of(span)
            for char in normalize_text(span.text):
                if char == "\n":
                    continue
                chars.append(char)
                sts.append(st)
        line_texts.append("".join(chars))
        line_styles.append(sts)

    first = lines[0]
    origin = (min(line.spans[0].origin[0] for line in lines), first.baseline)
    baselines = [line.baseline - first.baseline for line in lines]
    indents = [line.spans[0].origin[0] - origin[0] for line in lines]
    sizes = [st.size for sts in line_styles for st in sts] or [11.0]
    dominant = statistics.median(sizes)
    pitches = [b1 - b0 for b0, b1 in zip(baselines, baselines[1:]) if b1 - b0 > 0.1]
    line_height = (statistics.median(pitches) / dominant
                   if pitches and dominant > 0 else DEFAULT_LINE_HEIGHT)
    line_height = max(0.5, min(line_height, 3.0))
    align = reflow._capture_alignment(block)

    erase = []
    block_rect = D.fitz.Rect(*block.bbox)
    for line in block.lines:
        rect = D.fitz.Rect(*line.bbox)
        rect.y0 += LINE_INSET_PTS
        rect.y1 -= LINE_INSET_PTS
        rect.intersect(block_rect)
        if not (rect.is_empty or rect.is_infinite):
            erase.append(tuple(rect))

    all_styles = {st for sts in line_styles for st in sts}
    base = CharStyle() if not all_styles else max(
        all_styles, key=lambda s: sum(sts.count(s) for sts in line_styles))

    def build(paragraph: bool) -> tuple[str, list[CharStyle]]:
        text = ""
        styles: list[CharStyle] = []
        for k, (lt, ls) in enumerate(zip(line_texts, line_styles)):
            if k:
                if paragraph:
                    if not (text.endswith(" ") or text.endswith("-") or lt.startswith(" ")):
                        text += " "
                        styles.append(styles[-1] if styles else base)
                else:
                    text += "\n"
                    styles.append(styles[-1] if styles else base)
            text += lt
            styles.extend(ls)
        return text, styles

    content = [line.bbox[2] - line.bbox[0] for line in lines]
    paragraph = (len(lines) >= 2 and block_width > 0 and align != "center"
                 and all(w >= PARAGRAPH_FILL_RATIO * block_width for w in content[:-1]))
    if paragraph:
        text, styles = build(True)
        wrap = block_width + WRAP_EPSILON_PTS
        layout = layout_rich(text, styles, registry, align=align,
                             line_height=line_height, wrap_width=wrap,
                             fixed_baselines=baselines, indents=indents[:1])
        got = [text[ln.start:ln.end].strip() for ln in layout.lines]
        want = [lt.strip() for lt in line_texts]
        if got != want:
            paragraph = False
    if not paragraph:
        text, styles = build(False)
        wrap = None
        if align == "justify":
            align = "left"
    return BlockSeed(
        text=text, styles=styles, align=align, line_height=line_height,
        origin=origin, wrap_width=wrap, fixed_baselines=baselines,
        indents=indents if not paragraph else indents[:1],
        erase_rects=erase, bbox=tuple(block.bbox), paragraph=paragraph,
        inexact_fonts=sorted(inexact), mixed=len(all_styles) > 1)


# ---------------------------------------------------------------------------
# Commits (raw mutations inside a single session checkpoint)


def _mark_fonts(session, layout: Layout) -> None:
    """Flag the document so saving subsets the newly embedded fonts."""
    if any(face.embeds for face in layout.faces):
        try:
            session.document.fonts_embedded = True
        except Exception:
            pass


def commit_text(session, page: int, layout: Layout, origin: tuple[float, float],
                *, erase_rects: Sequence[tuple] = (), label: str = "Add text") -> None:
    """Write ``layout`` at ``origin``, first removing ``erase_rects`` text.

    One undo entry. With ``erase_rects`` empty this is "Add text"; with the
    original line boxes it is an in-place "Edit text". An empty layout with
    erase rects deletes the block's text.
    """
    fitz = D.fitz
    document = session.document
    with session.checkpoint(label):
        with document._lock:
            target = document._page(page)
            if erase_rects:
                for rect in erase_rects:
                    target.add_redact_annot(fitz.Rect(*rect), fill=False)
                # Images and vector art are preserved; only glyphs go.
                target.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                        text=fitz.PDF_REDACT_TEXT_REMOVE)
                # apply_redactions rebuilt the page: use a fresh proxy.
                target = document._page(page)
            if layout.text.strip():
                write_layout(target, layout, origin)
    _mark_fonts(session, layout)


def anchored_origin(anchor_x: float, baseline: float, layout: Layout,
                    align: str, wrap_width: Optional[float]) -> tuple[float, float]:
    """Box origin so an auto-width box grows around its alignment anchor.

    For left text the anchor is the left edge, for centred text the centre
    and for right-aligned text the right edge: a centred figure title stays
    centred while it is retyped. Fixed-width (wrapping) boxes never move.
    """
    if wrap_width is not None or align in ("left", "justify"):
        return (anchor_x, baseline)
    if align == "center":
        return (anchor_x - layout.width / 2.0, baseline)
    return (anchor_x - layout.width, baseline)


def iter_fonts_used(layout: Layout) -> Iterable[str]:
    seen = set()
    for face in layout.faces:
        if face.family not in seen:
            seen.add(face.family)
            yield face.family
