"""Export FigView's vector master to transparent PNGs and a Windows ICO.

Run from an IDE, with ``python build_icon.py``, or using Build Icon.bat.
Only the Python build environment needs resvg-py and Pillow. The application
continues to load the ready-made PNG and ICO with no new runtime dependency.
"""
from __future__ import annotations

import io
from pathlib import Path

import resvg_py
from PIL import Image

# Export sizes and canonical asset paths are centralized for easy customization.
ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
SVG_PATH = ASSETS / "figure-viewer-mark.svg"
MASTER_SIZE = 4096
PNG_SIZES = (256, 512, 1024, 2048)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_svg(size: int) -> Image.Image:
    """Rasterize the vector paths directly, preserving transparent tile corners."""
    # resvg preserves SVG gradients and alpha consistently, without a browser,
    # installed fonts, or external resources. Decode PNG to straight RGBA.
    data = resvg_py.svg_to_bytes(svg_path=str(SVG_PATH), width=size, height=size,
                                skip_system_fonts=True)
    with Image.open(io.BytesIO(data)) as source:
        return source.convert("RGBA")


def main() -> None:
    """Regenerate every shipping bitmap from the editable SVG master."""
    master = render_svg(MASTER_SIZE)
    master.save(ASSETS / "figure-viewer-mark.png", optimize=True)
    for size in PNG_SIZES:
        # A single high-resolution rendering followed by a Lanczos reduction
        # preserves smooth curves and consistent color at every desktop DPI.
        master.resize((size, size), Image.Resampling.LANCZOS).save(
            ASSETS / f"figure-viewer-mark-{size}.png", optimize=True)
    master.save(ROOT / "figure-viewer.ico", format="ICO",
                sizes=[(size, size) for size in ICO_SIZES])
    print(f"Exported SVG to {MASTER_SIZE}px master, {PNG_SIZES} PNGs and Windows ICO.")


if __name__ == "__main__":
    main()
