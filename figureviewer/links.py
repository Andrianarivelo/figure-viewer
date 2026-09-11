"""Clickable links: PDF link annotations and addresses printed in the text.

Two sources feed one :class:`LinkLayer` per page:

* **Annotation links** the PDF itself carries (``/Link`` annotations): web
  addresses, jumps to another page (plain or named destinations) and
  references to external files. :meth:`~figureviewer.docs.Document.links`
  extracts them.
* **Text links**: words that *look* like an address. Instruction sheets,
  papers and slide decks very often print a URL, an e-mail address or a
  DOI without wrapping it in a link annotation; the "Rendez-vous sur :
  https://..." sheet that motivated this feature is the typical case. The
  same detection runs over an image's OCR layer, so a screenshot's links
  become clickable once its text is recognised.

Rectangles live in **source pixels**, the frame of :mod:`textlayer`, so a
rotated or flipped view never invalidates them. Headless by design (no
tkinter); ``tests/test_links.py`` covers it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .textlayer import Rect, TextLayer

#: Link kinds.
KIND_URI = "uri"        # web address, e-mail, DOI: opened in the browser
KIND_PAGE = "page"      # jump inside the same document
KIND_OTHER = "other"    # external file or unsupported action: shown, not opened

#: Schemes the viewer hands to the default browser or mail client. Anything
#: else (``file:``, ``javascript:``, custom protocols) is refused: a PDF must
#: not be able to launch arbitrary programs with one click.
OPENABLE_SCHEMES = frozenset({"http", "https", "mailto", "ftp"})

#: Address patterns tried in order on every word. The scheme form goes
#: first so that ``https://doi.org/10...`` is a URL, not a bare DOI.
_ADDRESS_RE = re.compile(r"""
    (?P<url>(?:https?|ftp)://[^\s<>"']+)
  | (?P<mail>(?:mailto:)?[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)
  | (?P<doi>(?:doi:)?10\.\d{4,9}/[^\s<>"']+)
  | (?P<www>www\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/[^\s<>"']*)?)
""", re.VERBOSE | re.IGNORECASE)

#: Punctuation that ends a sentence rather than an address.
_TRAILING = ".,;:!?'\"»’"
_BRACKETS = (("(", ")"), ("[", "]"), ("{", "}"))


@dataclass(frozen=True)
class Link:
    """One clickable region of a page.

    ``rect`` is in source pixels. For :data:`KIND_URI` ``target`` is the
    address to open; for :data:`KIND_PAGE` ``page`` (0-based) and ``y_pts``
    (rendered page points from the top, or ``None`` for the page top)
    locate the destination; for :data:`KIND_OTHER` ``target`` is only shown.
    ``text`` is what the reader sees (the printed word, or the address).
    """

    rect: Rect
    kind: str
    target: str = ""
    page: int = -1
    y_pts: Optional[float] = None
    text: str = ""
    origin: str = "annotation"

    def describe(self) -> str:
        """Short human-readable description for the status bar."""
        if self.kind == KIND_PAGE:
            return f"Go to page {self.page + 1}" if self.page >= 0 else "Broken link"
        if self.kind == KIND_URI:
            return self.target
        return f"External file: {self.target}" if self.target else "Unsupported link"

    def openable(self) -> bool:
        """True when :meth:`describe` names something the viewer can follow."""
        if self.kind == KIND_PAGE:
            return self.page >= 0
        if self.kind == KIND_URI:
            return urlsplit(self.target).scheme.lower() in OPENABLE_SCHEMES
        return False


def trim_address(text: str) -> str:
    """Strip sentence punctuation and unbalanced closing brackets from the
    end of a matched address (``https://x.org/a).`` -> ``https://x.org/a``).
    A ``)`` that closes a ``(`` inside the address (Wikipedia style) stays."""
    while True:
        before = text
        text = text.rstrip(_TRAILING)
        for opener, closer in _BRACKETS:
            while text.endswith(closer) and text.count(opener) < text.count(closer):
                text = text[:-1]
        if text == before:
            return text


def normalise_address(match: "re.Match[str]") -> Optional[str]:
    """Turn a regex match into an absolute address the browser understands,
    or ``None`` when the trimmed remainder is too short to be one."""
    kind = match.lastgroup
    raw = trim_address(match.group(kind))
    if kind == "url":
        parts = urlsplit(raw)
        host = parts.netloc.lower()
        return raw if ("." in host or host == "localhost") else None
    if kind == "mail":
        address = raw[7:] if raw.lower().startswith("mailto:") else raw
        return f"mailto:{address}" if "@" in address else None
    if kind == "doi":
        doi = raw[4:] if raw.lower().startswith("doi:") else raw
        return f"https://doi.org/{doi}" if "/" in doi and len(doi) > 7 else None
    if kind == "www":
        return f"https://{raw}"
    return None


def address_in(text: str) -> Optional[str]:
    """The absolute address printed in ``text`` (one word), or ``None``."""
    match = _ADDRESS_RE.search(text)
    if match is None:
        return None
    return normalise_address(match)


def text_links(layer: TextLayer) -> list[Link]:
    """Detect addresses in every word of a text layer."""
    found: list[Link] = []
    for word in layer.words:
        address = address_in(word.text)
        if address is None:
            continue
        found.append(Link(word.rect, KIND_URI, address, text=word.text,
                          origin="text"))
    return found


class LinkLayer:
    """The links of one page, annotation links first so that an explicit
    link wins over an address detected in the same words."""

    def __init__(self, links: Iterable[Link]) -> None:
        self.links: tuple[Link, ...] = tuple(
            link for link in links
            if link.rect[2] > link.rect[0] and link.rect[3] > link.rect[1])

    def __len__(self) -> int:
        return len(self.links)

    def __iter__(self):
        return iter(self.links)

    def hit(self, x: float, y: float, pad: float = 1.5) -> Optional[Link]:
        """The link under ``(x, y)`` in source pixels, or ``None``."""
        for link in self.links:
            x0, y0, x1, y1 = link.rect
            if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
                return link
        return None

    @classmethod
    def build(cls, annotation_links: Iterable[Link],
              text_layer: Optional[TextLayer]) -> "LinkLayer":
        """Merge the document's own links with the addresses in its text.
        A text address whose word already sits inside an annotation link is
        dropped: the annotation is the authoritative target."""
        explicit = list(annotation_links)
        detected = text_links(text_layer) if text_layer is not None else []
        kept = []
        for link in detected:
            cx = (link.rect[0] + link.rect[2]) / 2.0
            cy = (link.rect[1] + link.rect[3]) / 2.0
            if any(e.rect[0] <= cx <= e.rect[2] and e.rect[1] <= cy <= e.rect[3]
                   for e in explicit):
                continue
            kept.append(link)
        return cls(explicit + kept)
