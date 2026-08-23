"""DPI-aware line icons for Figure Viewer's native Tk controls.

Toolbar glyphs used to be font characters. Their shape and even availability
changed with the active font, and a few Windows configurations displayed empty
boxes. These icons are drawn from simple geometry into oversampled Pillow
images, then reduced once for clean antialiasing. They remain deterministic,
lightweight, theme-aware, and sharp at high DPI.

The generated application mark is intentionally separate. It provides the
brand asset, while small functional controls stay code-native because that is
the reliable way to make a coherent scalable icon set.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageTk


class IconFactory:
    """Create and retain themed ``PhotoImage`` objects for one Tk window."""

    def __init__(self, master, palette, scale: float = 1.0) -> None:
        self.master = master
        self.palette = palette
        self.scale = max(1.0, float(scale))
        self._cache: dict[tuple, ImageTk.PhotoImage] = {}

    def get(self, name: str, size: int = 16, tone: str = "text") -> ImageTk.PhotoImage:
        """Return one cached icon, coloured from a named palette role."""
        pixels = max(12, int(round(size * min(self.scale, 1.75))))
        colour = getattr(self.palette, tone, self.palette.text)
        key = (name, pixels, colour, self.palette.accent)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        image = _draw_icon(name, pixels, colour, self.palette.accent)
        photo = ImageTk.PhotoImage(image, master=self.master)
        self._cache[key] = photo
        return photo

    def brand(self, path: Path, size: int = 22) -> Optional[ImageTk.PhotoImage]:
        """Load the generated master mark and reduce it for the menu bar."""
        pixels = max(16, int(round(size * min(self.scale, 1.5))))
        key = ("brand", str(path), pixels)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            with Image.open(path) as source:
                image = source.convert("RGBA")
                image.thumbnail((pixels, pixels), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image, master=self.master)
        except Exception:
            return None
        self._cache[key] = photo
        return photo


def _draw_icon(name: str, size: int, colour: str, accent: str) -> Image.Image:
    """Draw one icon in a 24-unit coordinate system with 4x antialiasing."""
    oversample = 4
    edge = size * oversample
    image = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = edge / 24.0
    stroke = max(4, int(round(unit * 1.8)))

    def pt(x: float, y: float) -> tuple[int, int]:
        return int(round(x * unit)), int(round(y * unit))

    def line(points, *, fill=colour, width=stroke) -> None:
        draw.line([pt(x, y) for x, y in points], fill=fill, width=width,
                  joint="curve")

    def polygon(points, *, fill=colour) -> None:
        draw.polygon([pt(x, y) for x, y in points], fill=fill)

    def rectangle(box, *, outline=colour, fill=None, width=stroke, radius=0) -> None:
        coords = (*pt(box[0], box[1]), *pt(box[2], box[3]))
        if radius:
            draw.rounded_rectangle(coords, radius=int(radius * unit), outline=outline,
                                   fill=fill, width=width)
        else:
            draw.rectangle(coords, outline=outline, fill=fill, width=width)

    if name == "previous":
        line(((15, 5), (8, 12), (15, 19)))
    elif name == "next":
        line(((9, 5), (16, 12), (9, 19)))
    elif name == "back5":
        line(((13, 6), (7, 12), (13, 18)))
        line(((18, 6), (12, 12), (18, 18)))
    elif name == "forward5":
        line(((6, 6), (12, 12), (6, 18)))
        line(((11, 6), (17, 12), (11, 18)))
    elif name == "play":
        polygon(((8, 5), (19, 12), (8, 19)), fill=accent)
    elif name == "pause":
        rectangle((7, 5, 10, 19), outline=None, fill=accent, width=0, radius=1)
        rectangle((14, 5, 17, 19), outline=None, fill=accent, width=0, radius=1)
    elif name == "stop":
        rectangle((7, 7, 17, 17), outline=None, fill=colour, width=0, radius=1.5)
    elif name in ("reload", "auto", "loop"):
        draw.arc((*pt(5, 5), *pt(19, 19)), start=30, end=315, fill=colour, width=stroke)
        polygon(((17.2, 4.2), (20.0, 8.2), (15.2, 8.5)))
        if name == "auto":
            draw.ellipse((*pt(10.2, 10.2), *pt(13.8, 13.8)), fill=accent)
        elif name == "loop":
            line(((5, 8), (5, 5), (9, 5)), width=max(3, stroke - 2))
    elif name == "folder":
        polygon(((3, 7), (9, 7), (11, 9), (21, 9), (19, 19), (3, 19)), fill=accent)
        rectangle((3, 6, 10, 10), outline=accent, fill=accent, width=0, radius=1.2)
    elif name == "up":
        line(((12, 19), (12, 6)))
        line(((6, 12), (12, 6), (18, 12)))
    elif name in ("zoom_in", "zoom_out"):
        draw.ellipse((*pt(4, 4), *pt(15, 15)), outline=colour, width=stroke)
        line(((14, 14), (20, 20)))
        line(((7, 9.5), (12, 9.5)), width=max(3, stroke - 1))
        if name == "zoom_in":
            line(((9.5, 7), (9.5, 12)), width=max(3, stroke - 1))
    elif name == "fit":
        line(((9, 4), (4, 4), (4, 9)))
        line(((15, 4), (20, 4), (20, 9)))
        line(((4, 15), (4, 20), (9, 20)))
        line(((20, 15), (20, 20), (15, 20)))
    elif name == "actual":
        rectangle((5, 5, 19, 19), radius=1.5)
        draw.ellipse((*pt(10, 10), *pt(14, 14)), fill=accent)
    elif name in ("rotate_left", "rotate_right"):
        if name == "rotate_right":
            draw.arc((*pt(5, 5), *pt(19, 19)), start=205, end=525, fill=colour, width=stroke)
            polygon(((18, 5), (21, 9), (16, 9)))
        else:
            draw.arc((*pt(5, 5), *pt(19, 19)), start=15, end=335, fill=colour, width=stroke)
            polygon(((6, 5), (3, 9), (8, 9)))
    elif name == "snip":
        draw.ellipse((*pt(3, 4), *pt(9, 10)), outline=colour, width=stroke)
        draw.ellipse((*pt(3, 14), *pt(9, 20)), outline=colour, width=stroke)
        line(((8, 9), (20, 18)))
        line(((8, 15), (20, 6)))
    elif name == "files":
        rectangle((5, 4, 17, 18), radius=1.5)
        line(((8, 8), (14, 8)), width=max(3, stroke - 1))
        line(((8, 12), (14, 12)), width=max(3, stroke - 1))
        line(((8, 16), (13, 16)), width=max(3, stroke - 1))
        line(((17, 8), (20, 8), (20, 21), (8, 21)), fill=accent)
    elif name == "info":
        draw.ellipse((*pt(4, 4), *pt(20, 20)), outline=colour, width=stroke)
        draw.ellipse((*pt(11, 7), *pt(13, 9)), fill=accent)
        line(((12, 11), (12, 17)), fill=accent)
    elif name == "copy":
        rectangle((8, 8, 20, 20), radius=1.4)
        rectangle((4, 4, 16, 16), outline=accent, radius=1.4)
    elif name == "path":
        draw.ellipse((*pt(3, 8), *pt(13, 16)), outline=colour, width=stroke)
        draw.ellipse((*pt(11, 8), *pt(21, 16)), outline=accent, width=stroke)
        line(((9, 12), (15, 12)))
    elif name == "external":
        rectangle((4, 7, 17, 20), radius=1.5)
        line(((11, 13), (20, 4)))
        line(((14, 4), (20, 4), (20, 10)))
    elif name == "video":
        rectangle((3, 5, 21, 19), radius=2)
        polygon(((10, 8), (17, 12), (10, 16)), fill=accent)
    else:
        draw.ellipse((*pt(6, 6), *pt(18, 18)), outline=colour, width=stroke)

    return image.resize((size, size), Image.Resampling.LANCZOS)
