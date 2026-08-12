"""Win32 clipboard helpers.

The stock ``Tk`` clipboard only handles text, and the common "save to BMP and
push CF_DIB" recipe loses transparency. This module opens the clipboard once
and publishes an image in three formats so every consumer gets its best match:

* ``"PNG"`` (registered format)  - lossless with alpha; preferred by Office,
  browsers, Slack, Inkscape, GIMP.
* ``CF_DIBV5``                   - 32-bit BGRA with a real alpha mask.
* ``CF_DIB``                     - 24-bit fallback flattened onto white, for
  older applications that ignore the two formats above.

Also provides file-drop (``CF_HDROP``) publishing so a figure can be pasted
straight into Explorer, an e-mail client or a chat window as a file.
"""
from __future__ import annotations

import ctypes
import io
import struct
from ctypes import wintypes
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image

CF_TEXT = 1
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002

BI_RGB = 0
BI_BITFIELDS = 3
LCS_sRGB = 0x73524742          # 'sRGB' in big-endian ASCII
LCS_GM_IMAGES = 4

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT


class ClipboardError(RuntimeError):
    """Raised when the Windows clipboard cannot be written."""


def _global_from_bytes(payload: bytes) -> wintypes.HGLOBAL:
    """Copy ``payload`` into a moveable global memory block."""
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        raise ClipboardError("GlobalAlloc failed while preparing clipboard data.")
    locked = _kernel32.GlobalLock(handle)
    if not locked:
        _kernel32.GlobalFree(handle)
        raise ClipboardError("GlobalLock failed while preparing clipboard data.")
    try:
        ctypes.memmove(locked, payload, len(payload))
    finally:
        _kernel32.GlobalUnlock(handle)
    return handle


def _dib_payload(image: Image.Image, background: str = "white") -> bytes:
    """24-bit BI_RGB DIB (BITMAPINFOHEADER + pixels), alpha flattened."""
    flat = image
    if flat.mode in ("RGBA", "LA", "PA") or "transparency" in flat.info:
        canvas = Image.new("RGB", flat.size, background)
        rgba = flat.convert("RGBA")
        canvas.paste(rgba, mask=rgba.split()[-1])
        flat = canvas
    buffer = io.BytesIO()
    flat.convert("RGB").save(buffer, "BMP")
    return buffer.getvalue()[14:]  # strip the 14-byte BITMAPFILEHEADER


def _dibv5_payload(image: Image.Image, dpi: tuple[float, float] | None = None) -> bytes:
    """32-bit BITMAPV5HEADER DIB with a genuine alpha channel.

    Rows are stored bottom-up (positive height) in BGRA order, which is what
    every Windows consumer expects for ``CF_DIBV5``.
    """
    rgba = image.convert("RGBA")
    width, height = rgba.size
    # PIL gives RGBA top-down; the DIB wants BGRA bottom-up.
    pixels = rgba.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")

    ppm_x = int(round((dpi[0] if dpi else 96.0) / 0.0254))
    ppm_y = int(round((dpi[1] if dpi else 96.0) / 0.0254))

    header = struct.pack(
        "<IiiHHIIiiII"      # bV5Size .. bV5ClrImportant
        "IIII"              # colour masks
        "I"                 # bV5CSType
        "9i"                # bV5Endpoints (CIEXYZTRIPLE)
        "III"               # gamma
        "IIII",             # intent, profile data, profile size, reserved
        124, width, height, 1, 32, BI_BITFIELDS, len(pixels), ppm_x, ppm_y, 0, 0,
        0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000,
        LCS_sRGB,
        0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0,
        LCS_GM_IMAGES, 0, 0, 0,
    )
    return header + pixels


def _png_payload(image: Image.Image, dpi: tuple[float, float] | None = None) -> bytes:
    """Lossless PNG bytes, optionally tagged with the source DPI."""
    buffer = io.BytesIO()
    kwargs = {"optimize": False, "compress_level": 3}  # speed over size
    if dpi:
        kwargs["dpi"] = dpi
    image.save(buffer, "PNG", **kwargs)
    return buffer.getvalue()


def _hdrop_payload(paths: Sequence[Path]) -> bytes:
    """DROPFILES structure followed by a double-null-terminated path list."""
    names = "\0".join(str(Path(p).resolve()) for p in paths) + "\0\0"
    # DROPFILES: pFiles offset, POINT pt, BOOL fNC, BOOL fWide
    head = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    return head + names.encode("utf-16-le")


def _open_clipboard(retries: int = 8) -> None:
    """Open the clipboard, retrying briefly if another app holds it."""
    import time

    for attempt in range(retries):
        if _user32.OpenClipboard(None):
            return
        time.sleep(0.03 * (attempt + 1))
    raise ClipboardError(
        "Could not open the Windows clipboard. Another application is holding it open."
    )


def _publish(entries: Iterable[tuple[int, bytes]]) -> None:
    """Empty the clipboard and set every ``(format, payload)`` pair."""
    payloads = list(entries)
    if not payloads:
        return
    _open_clipboard()
    handles: list[wintypes.HGLOBAL] = []
    try:
        if not _user32.EmptyClipboard():
            raise ClipboardError("EmptyClipboard failed.")
        for fmt, payload in payloads:
            handle = _global_from_bytes(payload)
            handles.append(handle)
            if not _user32.SetClipboardData(fmt, handle):
                raise ClipboardError(f"SetClipboardData failed for format {fmt}.")
            handles.pop()  # the clipboard now owns this handle
    finally:
        _user32.CloseClipboard()
        for handle in handles:      # only unclaimed handles remain
            _kernel32.GlobalFree(handle)


def copy_image(image: Image.Image, *, dpi: tuple[float, float] | None = None,
               background: str = "white") -> None:
    """Publish ``image`` as PNG + DIBV5 + DIB in a single clipboard update."""
    png_format = _user32.RegisterClipboardFormatW("PNG")
    entries: list[tuple[int, bytes]] = []
    if png_format:
        entries.append((png_format, _png_payload(image, dpi)))
    entries.append((CF_DIBV5, _dibv5_payload(image, dpi)))
    entries.append((CF_DIB, _dib_payload(image, background)))
    _publish(entries)


def copy_text(text: str) -> None:
    """Publish Unicode text."""
    payload = (text + "\0").encode("utf-16-le")
    _publish([(CF_UNICODETEXT, payload)])


def copy_files(paths: Sequence[Path]) -> None:
    """Publish a file list so the files can be pasted into Explorer or e-mail."""
    if not paths:
        return
    _publish([(CF_HDROP, _hdrop_payload(paths)), (CF_UNICODETEXT,
              ("\r\n".join(str(p) for p in paths) + "\0").encode("utf-16-le"))])
