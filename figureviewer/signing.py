"""Signature capture, storage and cryptographic PDF signing.

Design rules (enforced by tests in ``tests/test_sign.py``)
----------------------------------------------------------
1. **No tkinter.** This module is fully testable headless; every dialog and
   canvas gesture lives in the Tk glue, which hands plain data (stroke point
   lists, text, image bytes) down to this layer.
2. **PNG first, metadata second.** Every library mutation writes or deletes
   the PNG under ``store_dir`` BEFORE touching the settings metadata, and the
   ``save`` callback is invoked immediately after each mutation, so a crash
   can never leave metadata pointing at a file that was never written. A
   load-time sweep drops metadata whose PNG vanished and deletes orphan
   store-named PNGs that no metadata references any more.
3. **Raw user files are never modified.** ``add_image`` reads bytes,
   ``crypto_sign`` always writes to a separate output path, and
   :func:`stamp_signature` mutates only the in-memory edit session (undoable
   via one snapshot checkpoint).
4. **Optional dependencies are probed lazily** (:mod:`importlib.util`
   ``find_spec``, the pattern of ``figureviewer.video``): pyHanko and the
   cryptography package are only imported inside the functions that need
   them, and a missing package raises :class:`SigningError` naming the exact
   ``pip install`` command. OpenCV merely accelerates background removal and
   silently degrades to pure PIL.
5. **Page proxy discipline.** :func:`stamp_signature` performs its one
   ``insert_image`` on the page proxy it loaded itself, under the session
   document lock, inside one ``session.checkpoint('Sign')``.
"""
from __future__ import annotations

import importlib.util
import io
import logging
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence, Union

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import docs as D

# ---------------------------------------------------------------------------
# Tunables. Kept together so the capture look can be adjusted in one place.

#: Drawn strokes are rasterised at this multiple, then LANCZOS-downsampled:
#: the cheapest way to get ink-like antialiased edges out of PIL's aliased
#: line primitive.
SUPERSAMPLE = 4
#: Chaikin corner-cutting passes applied to raw stroke points before drawing.
SMOOTH_PASSES = 2
#: Stored signature PNGs are clamped to this width (store spec, Section D).
MAX_WIDTH_PX = 1200
#: Default placement width in PDF points recorded in each item's metadata.
DEFAULT_WIDTH_PT = 160.0
#: Pixel size typed signatures are rendered at (trimmed and clamped after).
TYPED_POINT_SIZE = 160
#: Channel threshold for "near white" background removal in ``add_image``.
WHITE_THRESHOLD = 235
#: Horizontal shear applied when no script font resolves, so the bundled
#: upright fallback still reads as a signature.
ITALIC_SHEAR = 0.22

#: Script-style fonts tried, in order, by :meth:`SignatureLibrary.add_typed`.
TYPED_FONT_CANDIDATES = ("Segoe Script", "Brush Script MT", "Ink Free",
                         "Gabriola")

#: Known file names for the candidate families inside the Windows font
#: directories. PIL's ``truetype`` resolves file names, not family names, so
#: the family is mapped here first and only then does a directory scan by
#: normalised stem run as a fallback.
_FONT_FILE_HINTS: dict[str, tuple[str, ...]] = {
    "segoe script": ("segoesc.ttf", "segoescb.ttf"),
    "brush script mt": ("BRUSHSCI.TTF", "brushsci.ttf"),
    "ink free": ("Inkfree.ttf",),
    "gabriola": ("Gabriola.ttf",),
}

KIND_DRAWN = "drawn"
KIND_TYPED = "typed"
KIND_IMAGE = "image"


class SigningError(RuntimeError):
    """Raised for missing optional packages and unusable signature data."""


# ---------------------------------------------------------------------------
# Small pure helpers


def _rgb(color: Union[str, Sequence[float]]) -> tuple[int, int, int]:
    """Accept ``#rrggbb`` / ``#rgb`` strings, 0-255 int or 0-1 float tuples."""
    if isinstance(color, str):
        text = color.strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) != 6:
            raise ValueError(f"unrecognised colour {color!r}")
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    channels = tuple(float(c) for c in tuple(color)[:3])
    if len(channels) != 3:
        raise ValueError(f"unrecognised colour {color!r}")
    if max(channels) <= 1.0:
        return tuple(min(255, max(0, round(c * 255))) for c in channels)  # type: ignore[return-value]
    return tuple(min(255, max(0, round(c))) for c in channels)  # type: ignore[return-value]


def _chaikin(points: Sequence[tuple[float, float]],
             passes: int = SMOOTH_PASSES) -> list[tuple[float, float]]:
    """Chaikin corner cutting: fast, stable stroke smoothing that keeps the
    first and last point exactly where the pen went down and lifted."""
    pts = [(float(x), float(y)) for x, y in points]
    for _ in range(max(0, passes)):
        if len(pts) < 3:
            break
        out = [pts[0]]
        for a, b in zip(pts, pts[1:]):
            out.append((0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]))
            out.append((0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]))
        out.append(pts[-1])
        pts = out
    return pts


def _trim_and_clamp(image: Image.Image) -> Image.Image:
    """Crop to the alpha bounding box, then cap the width at MAX_WIDTH_PX."""
    bbox = image.getchannel("A").getbbox()
    if bbox is None:
        raise SigningError("The signature is completely transparent; "
                           "there is nothing to store.")
    image = image.crop(bbox)
    if image.width > MAX_WIDTH_PX:
        height = max(1, round(image.height * MAX_WIDTH_PX / image.width))
        image = image.resize((MAX_WIDTH_PX, height), Image.Resampling.LANCZOS)
    return image


def _have_cv2() -> bool:
    return importlib.util.find_spec("cv2") is not None


def _white_to_alpha(image: Image.Image,
                    threshold: int = WHITE_THRESHOLD) -> Image.Image:
    """Make near-white pixels (all channels >= threshold) fully transparent.

    OpenCV is used when importable (it is optional); the pure-PIL fallback
    computes the identical mask with channel point tables and ImageChops, so
    both paths produce the same alpha.
    """
    image = image.convert("RGBA")
    if _have_cv2():
        import cv2
        import numpy as np

        arr = np.array(image)
        rgb = np.ascontiguousarray(arr[:, :, :3])
        mask = cv2.inRange(rgb, (threshold, threshold, threshold),
                           (255, 255, 255))
        arr[:, :, 3] = np.where(mask > 0, 0, arr[:, :, 3])
        return Image.fromarray(arr, "RGBA")

    def cut(value: int) -> int:
        return 255 if value >= threshold else 0

    r, g, b, a = image.split()
    near_white = ImageChops.multiply(
        ImageChops.multiply(r.point(cut), g.point(cut)), b.point(cut))
    # multiply scales by mask/255: alpha drops to 0 exactly where the mask
    # is 255 (near white) and is untouched everywhere else.
    alpha = ImageChops.multiply(a, ImageChops.invert(near_white))
    return Image.merge("RGBA", (r, g, b, alpha))


def _font_dirs() -> list[Path]:
    """System and per-user Windows font directories that actually exist."""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    candidates = [
        Path(windir) / "Fonts",
        Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts",
    ]
    return [d for d in candidates if d.is_dir()]


def _resolve_font_file(family: str) -> Optional[Path]:
    """Best-effort family name -> font file path, without loading anything.

    Known family-to-file hints are tried first, then each font directory is
    scanned for a file whose normalised stem starts with the normalised
    family name. Returns ``None`` when nothing resolves (non-Windows hosts,
    stripped-down installs), which triggers the bundled fallback font.
    """
    wanted = "".join(ch for ch in family.lower() if ch.isalnum())
    if not wanted:
        return None
    directories = _font_dirs()
    for hint in _FONT_FILE_HINTS.get(family.lower().strip(), ()):
        for directory in directories:
            candidate = directory / hint
            if candidate.is_file():
                return candidate
    for directory in directories:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            stem = "".join(ch for ch in entry.stem.lower() if ch.isalnum())
            if stem == wanted or stem.startswith(wanted):
                return entry
    return None


def _load_typed_font(font_candidates: Sequence[str]
                     ) -> tuple[ImageFont.ImageFont, bool]:
    """Return ``(font, needs_italic_shear)`` for typed signature rendering."""
    for family in font_candidates:
        path = _resolve_font_file(family)
        if path is None:
            continue
        try:
            return ImageFont.truetype(str(path), TYPED_POINT_SIZE), False
        except OSError:
            continue
    # No script font on this machine: fall back to Pillow's embedded free
    # font (Aileron, scalable since Pillow 10.1). It is upright, so a shear
    # is applied after rendering to keep the signature look.
    try:
        return ImageFont.load_default(size=TYPED_POINT_SIZE), True
    except TypeError:  # very old Pillow: fixed-size bitmap font
        return ImageFont.load_default(), True


def _italicise(image: Image.Image, shear: float = ITALIC_SHEAR) -> Image.Image:
    """Right-leaning shear; the canvas grows so no glyph is clipped."""
    grow = int(round(shear * image.height)) + 1
    # Affine maps output to input: x_in = x_out + shear*y_out - shear*h, so
    # the bottom row stays put and the top shifts right by shear*h.
    matrix = (1.0, shear, -shear * image.height, 0.0, 1.0, 0.0)
    return image.transform((image.width + grow, image.height),
                           Image.Transform.AFFINE, matrix,
                           resample=Image.Resampling.BICUBIC)


# ---------------------------------------------------------------------------
# The signature library


class SignatureLibrary:
    """CRUD over the persisted signature PNG store.

    Takes plain dependencies instead of an App reference so it stays
    headless-testable: the live ``settings`` dict, a ``save`` callback
    invoked immediately after every mutation, and the directory PNGs live
    in (``%APPDATA%/FigureViewerApp/signatures`` in production). The Tk glue
    adapts this to app-wide broadcasting.
    """

    def __init__(self, settings: dict, save: Callable[[dict], None],
                 store_dir: Path) -> None:
        self.settings = settings
        self._save = save
        self.store_dir = Path(store_dir)
        stored = settings.get("signature_library")
        if not isinstance(stored, list):
            stored = []
        # Load-time sweep, both directions: metadata whose PNG vanished is
        # dropped (the item can be neither placed nor thumbnailed), and PNGs
        # that no metadata references any more are deleted. Only files that
        # follow the store's own naming scheme (a 32-hex uid stem) are ever
        # deleted, so a stray user file in the directory is never at risk.
        kept = [dict(item) for item in stored
                if isinstance(item, dict) and item.get("id") and item.get("file")
                and (self.store_dir / str(item["file"])).is_file()]
        changed = kept != stored
        settings["signature_library"] = kept
        self._items: list[dict] = settings["signature_library"]
        self._sweep_orphan_pngs()
        if changed:
            self._save(settings)

    # -- queries -----------------------------------------------------------
    def items(self) -> list[dict]:
        """Metadata copies, in stored order; mutate only through the API."""
        return [dict(item) for item in self._items]

    def item(self, uid: str) -> dict:
        return dict(self._find(uid))

    def png_bytes(self, uid: str) -> bytes:
        meta = self._find(uid)
        path = self.store_dir / str(meta["file"])
        try:
            return path.read_bytes()
        except OSError as exc:
            raise SigningError(
                f"The stored signature image {path.name} could not be read: "
                f"{exc}") from exc

    def thumbnail(self, uid: str, height: int) -> Image.Image:
        """Aspect-preserving RGBA thumbnail for pickers and menus."""
        image = Image.open(io.BytesIO(self.png_bytes(uid))).convert("RGBA")
        target_h = max(1, int(height))
        target_w = max(1, round(image.width * target_h / image.height))
        return image.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # -- mutations ---------------------------------------------------------
    def add_drawn(self, strokes: Sequence[Sequence[tuple[float, float]]],
                  color: Union[str, Sequence[float]] = "#1a1a1a",
                  width: float = 3.0, *, label: str = "Drawn signature") -> str:
        """Render pen strokes (any coordinate space) into a stored PNG."""
        strokes = [list(stroke) for stroke in strokes if len(stroke) > 0]
        if not strokes:
            raise ValueError("at least one stroke point is required")
        width = max(0.5, float(width))
        smoothed = [_chaikin(stroke) for stroke in strokes]
        xs = [p[0] for stroke in smoothed for p in stroke]
        ys = [p[1] for stroke in smoothed for p in stroke]
        pad = width + 2.0
        min_x, min_y = min(xs) - pad, min(ys) - pad
        big_w = max(1, round((max(xs) + pad - min_x) * SUPERSAMPLE))
        big_h = max(1, round((max(ys) + pad - min_y) * SUPERSAMPLE))
        image = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        ink = _rgb(color) + (255,)
        line_w = max(1, round(width * SUPERSAMPLE))
        radius = line_w / 2.0
        for stroke in smoothed:
            scaled = [((x - min_x) * SUPERSAMPLE, (y - min_y) * SUPERSAMPLE)
                      for x, y in stroke]
            if len(scaled) >= 2:
                draw.line(scaled, fill=ink, width=line_w, joint="curve")
            # Round caps: PIL lines are butt-capped, so a dot is drawn at the
            # ends (and a single tap becomes just the dot).
            for x, y in (scaled[0], scaled[-1]):
                draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                             fill=ink)
        small = (max(1, round(big_w / SUPERSAMPLE)),
                 max(1, round(big_h / SUPERSAMPLE)))
        image = image.resize(small, Image.Resampling.LANCZOS)
        return self._commit_new(_trim_and_clamp(image), KIND_DRAWN, label)

    def add_typed(self, text: str,
                  font_candidates: Sequence[str] = TYPED_FONT_CANDIDATES,
                  *, color: Union[str, Sequence[float]] = "#1a1a1a",
                  label: str = "") -> str:
        """Render a typed name in the first resolvable script font."""
        text = str(text).strip()
        if not text:
            raise ValueError("the signature text is empty")
        font, needs_shear = _load_typed_font(font_candidates)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox = probe.textbbox((0, 0), text, font=font)
        pad = max(4, TYPED_POINT_SIZE // 8)
        size = (max(1, bbox[2] - bbox[0] + 2 * pad),
                max(1, bbox[3] - bbox[1] + 2 * pad))
        image = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.text((pad - bbox[0], pad - bbox[1]), text,
                  font=font, fill=_rgb(color) + (255,))
        if needs_shear:
            image = _italicise(image)
        return self._commit_new(_trim_and_clamp(image), KIND_TYPED,
                                label or text)

    def add_image(self, source: Union[str, Path, bytes, bytearray],
                  remove_background: bool = True, *,
                  label: str = "Imported signature") -> str:
        """Import a scan or photo of a signature; near-white becomes alpha."""
        if isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        else:
            # Read the bytes and let go of the file: the source is a raw user
            # file and must never be locked or modified (docs.py rule 1).
            data = Path(source).read_bytes()
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception as exc:
            raise SigningError(f"The signature image could not be decoded: "
                               f"{exc}") from exc
        image = image.convert("RGBA")
        if remove_background:
            image = _white_to_alpha(image)
        return self._commit_new(_trim_and_clamp(image), KIND_IMAGE, label)

    def rename(self, uid: str, label: str) -> None:
        meta = self._find(uid)
        meta["label"] = str(label)
        self._save(self.settings)

    def remove(self, uid: str) -> None:
        meta = self._find(uid)
        path = self.store_dir / str(meta["file"])
        try:
            path.unlink()               # the PNG goes FIRST
        except FileNotFoundError:
            pass
        self._items.remove(meta)        # then the metadata
        self._save(self.settings)

    # -- internals ---------------------------------------------------------
    def _sweep_orphan_pngs(self) -> None:
        """Delete store-named PNGs that no metadata entry references.

        Runs once at load time. A file is deleted only when its stem is a
        32-character hex uid (the store's own naming scheme, so a user file
        parked in the directory is never touched) and no metadata row points
        at it. Deletion failures are ignored: a locked file simply survives
        until the next load."""
        referenced = {str(item.get("file", "")).lower() for item in self._items}
        try:
            candidates = list(self.store_dir.glob("*.png"))
        except OSError:
            return
        for path in candidates:
            stem = path.stem.lower()
            is_store_name = (len(stem) == 32
                             and all(ch in "0123456789abcdef" for ch in stem))
            if is_store_name and path.name.lower() not in referenced:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _find(self, uid: str) -> dict:
        for item in self._items:
            if item.get("id") == uid:
                return item
        raise KeyError(f"no signature with id {uid!r}")

    def _commit_new(self, image: Image.Image, kind: str, label: str) -> str:
        uid = uuid.uuid4().hex
        filename = f"{uid}.png"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        (self.store_dir / filename).write_bytes(buffer.getvalue())  # PNG first
        self._items.append({
            "id": uid,
            "kind": kind,
            "file": filename,
            "label": label,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "default_width_pt": DEFAULT_WIDTH_PT,
        })
        self._save(self.settings)       # metadata second, persisted at once
        return uid


# ---------------------------------------------------------------------------
# Placing a signature onto a page (visual stamp, not cryptographic)


def stamp_signature(session, page: int,
                    rect_pts: tuple[float, float, float, float],
                    png_bytes: bytes) -> None:
    """Draw a signature PNG into the page content, as one undo entry.

    The stamp becomes ordinary page content ("flat" by construction, nothing
    to flatten later), wrapped in ``session.checkpoint('Sign')`` so a single
    Ctrl+Z removes it. The page proxy is loaded and used inside one lock
    acquisition; nothing derived from it escapes.
    """
    if not png_bytes:
        raise ValueError("the signature image is empty")
    x0, y0, x1, y1 = (float(v) for v in rect_pts)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"degenerate signature rectangle {rect_pts!r}")
    with session.checkpoint("Sign"):
        with session.document._lock:
            target = session._page(page)
            target.insert_image(D.fitz.Rect(x0, y0, x1, y1),
                                stream=bytes(png_bytes), keep_proportion=True)


# ---------------------------------------------------------------------------
# Cryptographic signing (pyHanko) and certificate generation (cryptography)


def have_pyhanko() -> bool:
    """Whether pyHanko is installed, without importing it."""
    return importlib.util.find_spec("pyhanko") is not None


def _require_pyhanko() -> None:
    if not have_pyhanko():
        raise SigningError(
            "Digital signing requires pyHanko.\n\nInstall it with:\n"
            "    pip install pyhanko"
        )


def _require_cryptography() -> None:
    if importlib.util.find_spec("cryptography") is None:
        raise SigningError(
            "Creating a certificate requires the cryptography package.\n\n"
            "Install it with:\n    pip install cryptography"
        )


def generate_self_signed(store_dir: Path, common_name: str,
                         passphrase: bytes) -> Path:
    """Create a self-signed PKCS#12 (.pfx) signing identity.

    RSA 2048, valid 10 years, digital-signature key usage, protected by the
    caller-supplied passphrase (which is never stored anywhere). Returns the
    path of the new .pfx under ``store_dir``. Self-signed certificates prove
    document *integrity* to any verifier; they are not trusted identities,
    and verification reports say so honestly.
    """
    common_name = str(common_name).strip()
    if not common_name:
        raise ValueError("a certificate needs a non-empty common name")
    if not passphrase:
        raise ValueError("a passphrase is required to protect the certificate")
    _require_cryptography()
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _dt.datetime.now(_dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=True,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False),
            critical=True)
        .sign(key, hashes.SHA256())
    )
    payload = pkcs12.serialize_key_and_certificates(
        common_name.encode("utf-8", "replace"), key, certificate, None,
        serialization.BestAvailableEncryption(bytes(passphrase)))

    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    stem = "".join(ch if (ch.isalnum() or ch in " -_") else "_"
                   for ch in common_name).strip() or "figview-certificate"
    path = store_dir / f"{stem}.pfx"
    counter = 2
    while path.exists():                # never clobber an existing identity
        path = store_dir / f"{stem}-{counter}.pfx"
        counter += 1
    path.write_bytes(payload)
    return path


def crypto_sign(saved_path: Path, out_path: Path, pkcs12_path: Path,
                passphrase: bytes, field_name: str = "FigViewSignature",
                reason: str = "") -> Path:
    """Digitally sign a saved PDF into a NEW file (never in place).

    ``saved_path`` must be a document already saved to disk; the signed copy
    is written to ``out_path``. Signing in place is refused because a failed
    write must never damage the input.
    """
    saved_path = Path(saved_path)
    out_path = Path(out_path)
    if os.path.normcase(str(saved_path.resolve())) == os.path.normcase(
            str(out_path.resolve() if out_path.exists()
                else out_path.absolute())):
        raise SigningError("Refusing to sign in place: choose a different "
                           "output file so the original stays intact.")
    _require_pyhanko()
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers

    try:
        signer = signers.SimpleSigner.load_pkcs12(
            pfx_file=str(pkcs12_path), passphrase=bytes(passphrase))
    except Exception as exc:
        raise SigningError(f"The certificate {Path(pkcs12_path).name} could "
                           f"not be loaded: {exc}") from exc
    if signer is None:
        raise SigningError(
            f"The certificate {Path(pkcs12_path).name} could not be loaded. "
            "Check the file and the passphrase.")

    metadata = signers.PdfSignatureMetadata(field_name=field_name,
                                            reason=reason or None)
    try:
        with saved_path.open("rb") as source:
            writer = IncrementalPdfFileWriter(source)
            with out_path.open("wb") as output:
                signers.sign_pdf(writer, metadata, signer=signer,
                                 output=output)
    except SigningError:
        raise
    except Exception as exc:
        try:                            # do not leave a half-written output
            out_path.unlink()
        except OSError:
            pass
        raise SigningError(f"Signing failed: {exc}") from exc
    return out_path


@dataclass(frozen=True)
class SignatureReport:
    """Plain-language verification result for one embedded signature."""

    field_name: str
    signer: str
    signed_at: str
    intact: bool
    trusted: bool
    coverage: str
    ok: bool
    status: str


@contextmanager
def _quiet_pyhanko_logs() -> Iterator[None]:
    """Silence pyHanko's expected-failure log spam (untrusted self-signed
    certificates log full tracebacks) for the duration of a validation."""
    names = ("pyhanko", "pyhanko_certvalidator")
    loggers = [logging.getLogger(name) for name in names]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels):
            logger.setLevel(level)


def verify_signatures(path: Path) -> list[SignatureReport]:
    """Validate every embedded signature in ``path``.

    Returns one :class:`SignatureReport` per signature (an empty list for an
    unsigned document). ``ok`` is True only when the cryptographic digest is
    intact AND the signature covers the entire file: a byte appended after
    signing drops coverage below ``ENTIRE_FILE`` and is reported as a
    post-signing change. Trust is reported but does not gate ``ok``, because
    self-signed certificates (the ones this app generates) are inherently
    untrusted while still proving integrity. An unreadable file yields a
    single not-ok report instead of raising.
    """
    _require_pyhanko()
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko.sign.validation.status import SignatureCoverageLevel
    from pyhanko_certvalidator import ValidationContext

    reports: list[SignatureReport] = []
    try:
        with Path(path).open("rb") as handle:
            reader = PdfFileReader(handle, strict=False)
            embedded = list(reader.embedded_signatures)
            context = ValidationContext(trust_roots=[], allow_fetching=False)
            for signature in embedded:
                field = str(getattr(signature, "field_name", "") or "")
                try:
                    with _quiet_pyhanko_logs():
                        status = validate_pdf_signature(
                            signature, signer_validation_context=context)
                except Exception as exc:
                    reports.append(SignatureReport(
                        field_name=field, signer="", signed_at="",
                        intact=False, trusted=False, coverage="unknown",
                        ok=False,
                        status=f"Verification failed for this signature: "
                               f"{exc}"))
                    continue
                try:
                    signer_name = status.signing_cert.subject.human_friendly
                except Exception:
                    signer_name = ""
                signed_at = ""
                reported = getattr(status, "signer_reported_dt", None)
                if reported is not None:
                    signed_at = reported.isoformat(sep=" ", timespec="seconds")
                intact = bool(status.intact and status.valid)
                trusted = bool(status.trusted)
                whole_file = status.coverage == SignatureCoverageLevel.ENTIRE_FILE
                ok = intact and whole_file
                if not intact:
                    text = ("Broken: the document content no longer matches "
                            "this signature. It was altered after signing.")
                elif not whole_file:
                    text = ("The signature itself is intact, but the file "
                            "was changed after it was applied.")
                elif trusted:
                    text = ("Valid: the signature is intact, covers the "
                            "whole file, and the certificate is trusted.")
                else:
                    text = ("Intact: the document is unchanged since "
                            "signing. The certificate is self-signed or not "
                            "in the trust store, so the signer's identity "
                            "is not independently verified.")
                reports.append(SignatureReport(
                    field_name=field, signer=signer_name, signed_at=signed_at,
                    intact=intact, trusted=trusted,
                    coverage=status.coverage.name.lower(), ok=ok, status=text))
    except OSError as exc:
        raise SigningError(f"The file could not be read: {exc}") from exc
    except Exception as exc:
        reports.append(SignatureReport(
            field_name="", signer="", signed_at="", intact=False,
            trusted=False, coverage="unreadable", ok=False,
            status=f"Verification failed: the file could not be parsed as a "
                   f"signed PDF ({exc})."))
    return reports
