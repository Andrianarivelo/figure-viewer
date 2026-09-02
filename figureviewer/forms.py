"""AcroForm model, identity presets and auto-fill proposals.

Design rules (enforced by tests in ``tests/test_forms.py``)
-----------------------------------------------------------
1. **No tkinter.** This module is fully testable headless; every dialog and
   review bar lives in the Tk glue, which adapts the plain-dependency classes
   defined here (:class:`IdentityStore` takes a settings dict and a save
   callback, never an App reference).
2. **PyMuPDF is reached as ``D.fitz``** through :mod:`figureviewer.docs`, so
   the optional-dependency story stays in one place. A missing PyMuPDF yields
   an actionable error naming the pip command, never an import-time failure.
3. **Page proxy binding.** A PyMuPDF widget or word list is bound to the
   exact ``Page`` object it came from. Every walk below loads one page proxy,
   keeps it referenced for the whole read, and copies what it needs into
   plain dataclasses before the proxy goes away. Reads on an
   :class:`figureviewer.pdfedit.EditableVectorDocument` happen under its
   document lock, so they serialise against worker renders and edits.
4. **Nothing is written without user consent.** :func:`match_identity` is a
   read-only scoring pass; values reach the document only through
   :func:`set_value` / :func:`apply_proposals`, which delegate to the edit
   session's undoable form command. Auto-fill is one undo entry.
5. **Raw files are never touched.** All mutation goes through the session's
   in-memory document; saving is the caller's explicit act.

The matching heuristic is deliberately simple and pinned by tests: names and
tooltips are lowercased, split on camelCase and non-alphanumerics, stripped
of noise tokens, then compared to :data:`FIELD_TOKEN_MAP` (exact token = 2,
substring = 1). Checkboxes, radio buttons and signature widgets are never
proposed; ``full_name`` never steals from split first/last fields on the
same page; date fields near birth/expiry wording are left alone.
"""
from __future__ import annotations

import copy
import re
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from . import docs as D

# ---------------------------------------------------------------------------
# Field model

#: The widget kinds surfaced by :func:`fields`. Pushbuttons are not form
#: data and are skipped entirely.
FIELD_TYPES = ("text", "checkbox", "radio", "combo", "listbox", "signature")

#: Widget kinds that auto-fill must never touch: toggling a checkbox or
#: radio is a decision, and a signature field is a legal act.
NEVER_PROPOSED = ("checkbox", "radio", "signature")


def _require_fitz() -> None:
    if not D.HAVE_FITZ:
        raise RuntimeError(
            "PDF form support requires PyMuPDF.\n\nInstall it with:\n"
            "    pip install pymupdf"
        )


@dataclass(frozen=True)
class FieldInfo:
    """A plain snapshot of one AcroForm widget, safe to hold anywhere.

    ``rect_pts`` is in PDF points, top-left origin. ``value`` is whatever
    PyMuPDF reports (text fields give ``str``, checkboxes give the on/off
    state name). ``max_len`` is ``None`` when the field has no length cap.
    ``options`` holds the export values of combo/list boxes, empty otherwise.
    """

    page: int
    xref: int
    name: str
    ftype: str
    rect_pts: tuple[float, float, float, float]
    value: object
    tooltip: str = ""
    max_len: Optional[int] = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Proposal:
    """One suggested fill: nothing is written until the user confirms."""

    field: FieldInfo
    key: str            # identity key from FIELD_TOKEN_MAP
    value: str
    score: int          # 2 exact token, 1 substring
    source: str         # 'name' | 'tooltip' | 'nearby'


def _resolve(document) -> tuple[object, object]:
    """Return ``(fitz_document, lock_context)`` for any accepted input.

    Accepts a raw ``fitz.Document``, a :class:`figureviewer.docs
    .VectorDocument` (the editable subclass included) or an
    :class:`figureviewer.pdfedit.EditSession`. Wrapper reads take the
    document lock; a bare fitz document gets a null context.
    """
    document = getattr(document, "document", document)   # EditSession
    inner = getattr(document, "_doc", None)
    if inner is not None:
        return inner, getattr(document, "_lock", None) or nullcontext()
    return document, nullcontext()


def _widget_ftype(widget) -> Optional[str]:
    """Map a PyMuPDF widget type constant to our ftype name, else ``None``."""
    fitz = D.fitz
    mapping = {
        fitz.PDF_WIDGET_TYPE_TEXT: "text",
        fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
        fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
        fitz.PDF_WIDGET_TYPE_COMBOBOX: "combo",
        fitz.PDF_WIDGET_TYPE_LISTBOX: "listbox",
        fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature",
    }
    return mapping.get(widget.field_type)


def _options_of(widget) -> tuple[str, ...]:
    """Normalised export values of a choice widget ([value, display] pairs
    collapse to the export value, which is what ``field_value`` stores)."""
    raw = getattr(widget, "choice_values", None) or ()
    out: list[str] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and item:
            out.append(str(item[0]))
        else:
            out.append(str(item))
    return tuple(out)


def has_form(document) -> bool:
    """True when the document carries fillable AcroForm fields.

    Accepts a raw fitz document, a VectorDocument wrapper or an EditSession;
    anything without pages (a raster image) is simply not a form.
    """
    _require_fitz()
    doc, lock = _resolve(document)
    if not hasattr(doc, "load_page"):
        return False
    with lock:
        try:
            if bool(doc.is_form_pdf):
                return True
        except Exception:
            pass
        # is_form_pdf reads the AcroForm field count; a malformed catalog can
        # under-report, so fall back to an actual widget scan. The page proxy
        # is held in a local for the duration of the probe (binding rule).
        for index in range(doc.page_count):
            page = doc.load_page(index)
            if page.first_widget is not None:
                return True
    return False


def fields(document) -> list[FieldInfo]:
    """Snapshot every fillable widget in the document, in page order.

    One page proxy per page, kept referenced for the whole widget walk;
    everything is copied into plain :class:`FieldInfo` records before the
    proxy is released, so callers can never hold a dangling widget.
    """
    _require_fitz()
    doc, lock = _resolve(document)
    if not hasattr(doc, "load_page"):
        return []
    out: list[FieldInfo] = []
    with lock:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            for widget in page.widgets() or ():
                ftype = _widget_ftype(widget)
                if ftype is None:      # pushbuttons and exotics: not data
                    continue
                rect = widget.rect
                max_len = int(getattr(widget, "text_maxlen", 0) or 0)
                out.append(FieldInfo(
                    page=index,
                    xref=int(widget.xref),
                    name=str(widget.field_name or ""),
                    ftype=ftype,
                    rect_pts=(rect.x0, rect.y0, rect.x1, rect.y1),
                    value=widget.field_value,
                    tooltip=str(widget.field_label or ""),
                    max_len=max_len if max_len > 0 else None,
                    options=_options_of(widget),
                ))
    return out


def set_value(session, field: FieldInfo, value) -> None:
    """Write one field through the session's undoable form command."""
    session.set_field_value(field.page, field.xref, value,
                            label=f"Fill {field.name or 'field'}")


# ---------------------------------------------------------------------------
# Identity matching

#: Identity key -> lowercase tokens that suggest it. Pinned by tests.
#: ``full_name`` is deliberately the LAST entry: iteration order is the
#: tie-break, so the generic "name" token can never outrank a specific match,
#: and a page-level pass additionally drops full_name wherever a split
#: first/last field matched (people design forms both ways).
FIELD_TOKEN_MAP: dict[str, tuple[str, ...]] = {
    "first_name": ("first", "fname", "given", "forename", "prenom", "vorname"),
    "last_name": ("last", "lname", "surname", "family", "nom", "nachname"),
    "email": ("email", "e-mail", "mail", "courriel"),
    "phone": ("phone", "tel", "mobile", "cell"),
    "address1": ("address", "addr", "street", "adresse", "line1"),
    "city": ("city", "town", "ville"),
    "state": ("state", "province", "region"),
    "zip": ("zip", "postal", "postcode", "plz"),
    "country": ("country", "pays", "land"),
    "company": ("company", "organization", "organisation", "employer",
                "institution", "firm"),
    "title": ("title", "position", "role", "job"),
    "date_today": ("date", "dated", "signdate"),
    "full_name": ("name", "fullname", "printname", "applicant"),
}

#: Tokens that promote an address1 match to address2.
_ADDRESS2_TOKENS = frozenset({"2", "line2", "apt", "suite", "address2", "addr2"})

#: Tokens carrying no meaning in field names, dropped before matching.
_NOISE_TOKENS = frozenset({"field", "txt", "input", "form"})

_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> tuple[str, ...]:
    """Normalise a field name, tooltip or nearby label into match tokens."""
    if not text:
        return ()
    spaced = _CAMEL_SPLIT.sub(" ", text)
    parts = _NON_ALNUM.split(spaced.lower())
    return tuple(part for part in parts if part and part not in _NOISE_TOKENS)


def _score_key(tokens: tuple[str, ...], key: str) -> int:
    """2 for an exact token hit, 1 for a substring hit, 0 for none."""
    best = 0
    for map_token in FIELD_TOKEN_MAP[key]:
        if map_token in tokens:
            return 2
        if best == 0 and any(map_token in token for token in tokens):
            best = 1
    return best


def _best_key(tokens: tuple[str, ...]) -> tuple[Optional[str], int]:
    """The best-scoring identity key for one token set, with the guards:
    fax is never a phone, birth/expiry dates are never today's date, and
    'title' yields to any name-ish wording (a 'name title' is an honorific,
    not a job)."""
    if not tokens:
        return None, 0
    has_fax = any("fax" in token for token in tokens)
    date_blocked = any("birth" in token or token == "dob" or "expir" in token
                       for token in tokens)
    name_hit = any(_score_key(tokens, key) > 0
                   for key in ("first_name", "last_name", "full_name"))
    best_key: Optional[str] = None
    best_score = 0
    for key in FIELD_TOKEN_MAP:
        if key == "phone" and has_fax:
            continue
        if key == "date_today" and date_blocked:
            continue
        if key == "title" and name_hit:
            continue
        score = _score_key(tokens, key)
        if score > best_score:
            best_key, best_score = key, score
    if best_key == "address1" and any(t in _ADDRESS2_TOKENS for t in tokens):
        best_key = "address2"
    return best_key, best_score


def _boxes_overlap(a: tuple[float, float, float, float],
                   b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def nearby_label(document, field: FieldInfo) -> str:
    """Visible text just left of and just above a widget, reading order.

    Forms routinely name their fields ``Text3``; the printed label beside the
    box is then the only hint. The strips are narrow on purpose so a label
    from the next column or the previous row cannot bleed in.
    """
    doc, lock = _resolve(document)
    if not hasattr(doc, "load_page"):
        return ""
    x0, y0, x1, y1 = field.rect_pts
    left_strip = (x0 - 120.0, y0 - 4.0, x0, y1 + 4.0)
    above_strip = (x0 - 4.0, y0 - 28.0, x1 + 4.0, y0 + 2.0)
    with lock:
        # get_text builds and disposes its own textpage against this proxy;
        # the returned word tuples are plain data, safe past the lock.
        page = doc.load_page(field.page)
        words = page.get_text("words")
    picked = [(wy0, wx0, text) for wx0, wy0, wx1, wy1, text, *_ in words
              if _boxes_overlap((wx0, wy0, wx1, wy1), left_strip)
              or _boxes_overlap((wx0, wy0, wx1, wy1), above_strip)]
    picked.sort()
    return " ".join(text for _, _, text in picked)


def _preset_values(preset: dict) -> dict:
    """Accept a full preset record or just its ``fields`` mapping."""
    inner = preset.get("fields")
    return inner if isinstance(inner, dict) else preset


def _value_for(key: str, values: dict, date_format: str) -> str:
    if key == "date_today":
        try:
            return date.today().strftime(date_format)
        except (ValueError, TypeError):
            return date.today().strftime("%Y-%m-%d")
    return str(values.get(key) or "").strip()


def _canonical_option(value: str, options: tuple[str, ...]) -> Optional[str]:
    """The export value matching ``value`` case-insensitively, else None."""
    lowered = value.strip().lower()
    for option in options:
        if option.strip().lower() == lowered:
            return option
    return None


def match_identity(field_list: list[FieldInfo], preset: dict,
                   document=None) -> list[Proposal]:
    """Score every fillable field against an identity preset.

    Pure given its inputs: nothing is written to the document. ``document``
    is only needed for the nearby-label fallback (fields whose name and
    tooltip say nothing); omit it and that fallback is skipped. Checkboxes,
    radio buttons and signature widgets are never proposed. Choice widgets
    are proposed only when the preset value is actually one of the options.
    """
    values = _preset_values(preset)
    date_format = str(values.get("date_format") or "%Y-%m-%d")

    matched: list[tuple[FieldInfo, str, int, str]] = []
    for field in field_list:
        if field.ftype in NEVER_PROPOSED:
            continue
        key: Optional[str] = None
        score = 0
        source = ""
        for src, text in (("name", field.name), ("tooltip", field.tooltip)):
            key, score = _best_key(_tokens(text))
            if key is not None:
                source = src
                break
        if key is None and document is not None:
            key, score = _best_key(_tokens(nearby_label(document, field)))
            source = "nearby"
        if key is None:
            continue
        matched.append((field, key, score, source))

    # Page rule: a page with dedicated first/last fields treats any bare
    # "name" field as decoration (a header, a printed-name line), never as a
    # second target for the same data.
    split_pages = {field.page for field, key, _, _ in matched
                   if key in ("first_name", "last_name")}
    proposals: list[Proposal] = []
    for field, key, score, source in matched:
        if key == "full_name" and field.page in split_pages:
            continue
        value = _value_for(key, values, date_format)
        if not value:
            continue
        if field.ftype in ("combo", "listbox"):
            canonical = _canonical_option(value, field.options)
            if canonical is None:
                continue
            value = canonical
        proposals.append(Proposal(field=field, key=key, value=value,
                                  score=score, source=source))
    return proposals


def apply_proposals(session, proposals: list[Proposal]) -> int:
    """Write the confirmed proposals as ONE undo entry. Returns the count."""
    if not proposals:
        return 0
    session.begin_group(f"Auto-fill ({len(proposals)} fields)")
    try:
        for proposal in proposals:
            set_value(session, proposal.field, proposal.value)
    finally:
        session.end_group()
    return len(proposals)


# ---------------------------------------------------------------------------
# Identity presets

class IdentityStore:
    """CRUD over the identity presets stored in the settings dict.

    Takes plain dependencies (the live settings mapping and a save callback)
    so it is testable headless; the Tk glue wires ``save`` to the app's
    failure-safe ``save_settings`` and re-broadcasts after each call. Every
    mutation persists IMMEDIATELY: identity data must survive a crash.

    Privacy note surfaced by the preset dialog: presets are plain JSON in
    the settings file, readable by anything running as the user.
    """

    def __init__(self, settings: dict, save: Callable[[dict], None]) -> None:
        self._settings = settings
        self._save = save
        # Sanitise hand-edited JSON; sanitation is not a user mutation, so
        # it does not trigger a save.
        if not isinstance(settings.get("identity_presets"), list):
            settings["identity_presets"] = []
        if not isinstance(settings.get("active_identity"), str):
            settings["active_identity"] = ""

    # -- reads -------------------------------------------------------------
    def presets(self) -> list[dict]:
        return copy.deepcopy(self._settings["identity_presets"])

    def get(self, preset_id: str) -> Optional[dict]:
        for preset in self._settings["identity_presets"]:
            if isinstance(preset, dict) and preset.get("id") == preset_id:
                return copy.deepcopy(preset)
        return None

    def active(self) -> Optional[dict]:
        return self.get(self._settings["active_identity"])

    # -- mutations (each one calls save exactly once) ----------------------
    def add(self, label: str, field_values: dict) -> str:
        """Create a preset, make it active, persist, return its id."""
        preset = {
            "id": uuid.uuid4().hex,
            "label": str(label),
            "fields": {str(k): str(v) for k, v in dict(field_values).items()},
        }
        self._settings["identity_presets"].append(preset)
        self._settings["active_identity"] = preset["id"]
        self._save(self._settings)
        return preset["id"]

    def update(self, preset_id: str, *, label: Optional[str] = None,
               field_values: Optional[dict] = None) -> bool:
        for preset in self._settings["identity_presets"]:
            if isinstance(preset, dict) and preset.get("id") == preset_id:
                if label is not None:
                    preset["label"] = str(label)
                if field_values is not None:
                    preset["fields"] = {str(k): str(v)
                                        for k, v in dict(field_values).items()}
                self._save(self._settings)
                return True
        return False

    def remove(self, preset_id: str) -> bool:
        presets = self._settings["identity_presets"]
        kept = [p for p in presets
                if not (isinstance(p, dict) and p.get("id") == preset_id)]
        if len(kept) == len(presets):
            return False
        self._settings["identity_presets"] = kept
        if self._settings["active_identity"] == preset_id:
            self._settings["active_identity"] = ""
        self._save(self._settings)
        return True

    def set_active(self, preset_id: str) -> bool:
        """Select a preset by id ('' clears the selection)."""
        if preset_id and self.get(preset_id) is None:
            return False
        self._settings["active_identity"] = preset_id
        self._save(self._settings)
        return True


# ---------------------------------------------------------------------------
# Fill & Sign helpers for flat (form-less) PDFs

#: Ink glyphs in a unit box (x right, y down), one tuple of strokes each.
#: Hardcoded so the rendering is deterministic and pinnable by tests.
CHECK_GLYPHS: dict[str, tuple[tuple[tuple[float, float], ...], ...]] = {
    "check": (
        ((0.10, 0.55), (0.40, 0.85), (0.90, 0.15)),
    ),
    "cross": (
        ((0.12, 0.12), (0.88, 0.88)),
        ((0.88, 0.12), (0.12, 0.88)),
    ),
    "dot": (
        ((0.82, 0.50), (0.777, 0.66), (0.66, 0.777), (0.50, 0.82),
         (0.34, 0.777), (0.223, 0.66), (0.18, 0.50), (0.223, 0.34),
         (0.34, 0.223), (0.50, 0.18), (0.66, 0.223), (0.777, 0.34),
         (0.82, 0.50)),
    ),
}


def place_text(session, page: int, point_pts: tuple[float, float], text: str,
               size: float = 11.0,
               color: tuple[float, float, float] = (0.1, 0.1, 0.1)) -> str:
    """Drop borderless free text at a point (Fill&Sign on a flat PDF).

    The rect is sized to the text so the annotation never clips; delegates
    to the session's undoable FreeText command and returns its uid.
    """
    _require_fitz()
    lines = (text or " ").split("\n")
    width = 0.0
    for line in lines:
        try:
            width = max(width, D.fitz.get_text_length(
                line or " ", fontname="helv", fontsize=size))
        except Exception:
            width = max(width, len(line or " ") * size * 0.6)
    x, y = point_pts
    pad = size * 0.35
    rect = (x, y, x + width + 2.0 * pad + 2.0, y + size * 1.55 * len(lines))
    return session.add_free_text(page, rect, text, fontname="helv",
                                 fontsize=size, color=color)


def add_check(session, page: int, point_pts: tuple[float, float],
              kind: str = "check", size: float = 14.0,
              color: tuple[float, float, float] = (0.1, 0.1, 0.1)) -> str:
    """Draw a small check / cross / dot glyph centred on a point.

    Rendered as an ink annotation from :data:`CHECK_GLYPHS`, scaled to
    ``size`` points; one undoable command, uid returned.
    """
    if kind not in CHECK_GLYPHS:
        raise ValueError(f"unknown check kind {kind!r}; "
                         f"expected one of {sorted(CHECK_GLYPHS)}")
    cx, cy = point_pts
    strokes = [[(cx + (u - 0.5) * size, cy + (v - 0.5) * size)
                for u, v in stroke]
               for stroke in CHECK_GLYPHS[kind]]
    return session.add_ink(page, strokes, color=color,
                           width=max(0.8, size * 0.09))
