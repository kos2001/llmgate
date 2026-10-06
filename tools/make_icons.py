"""Regenerate the raster icons from llmgate/static/favicon.svg (the single source of truth).

    pip install pillow           # and have Chrome/Chromium/Edge installed (set CHROME=... to point at it)
    python tools/make_icons.py

Chrome rasterises the SVG (no native Cairo needed, which is awkward on Windows). Writes next to the SVG:
favicon.ico (16/32/48), icon-192.png, icon-512.png, apple-touch-icon.png (opaque, full-bleed, because iOS applies its
own rounding) and icon-maskable-512.png (full-bleed with the mark inside the central 80%: Android crops it to a circle).
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

STATIC = Path(__file__).resolve().parent.parent / "llmgate" / "static"
SVG = (STATIC / "favicon.svg").read_text(encoding="utf-8")
TILE = '<rect width="64" height="64" rx="15" fill="url(#g)"/>'
CANDIDATES = [
    os.environ.get("CHROME", ""), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "/usr/bin/google-chrome", "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def chrome() -> str:
    for c in CANDIDATES:
        if c and Path(c).exists():
            return c
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        if shutil.which(name):
            return str(shutil.which(name))
    raise SystemExit("Chrome/Chromium/Edge not found; set CHROME=/path/to/browser")


def render(svg: str, size: int) -> Image.Image:
    """Transparent-background PNG of the SVG at ``size`` x ``size``."""
    html = f'<!doctype html><meta charset="utf-8"><style>html,body{{margin:0;background:transparent}}svg{{display:block;width:{size}px;height:{size}px}}</style>{svg}'
    with tempfile.TemporaryDirectory() as tmp:
        page, out = Path(tmp) / "icon.html", Path(tmp) / "icon.png"
        page.write_text(html, encoding="utf-8")
        subprocess.run([chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--default-background-color=00000000",
                        f"--window-size={size},{size}", f"--screenshot={out}", page.as_uri()], check=True, capture_output=True, timeout=60)
        return Image.open(io.BytesIO(out.read_bytes())).convert("RGBA").crop((0, 0, size, size))


def full_bleed(svg: str) -> str:
    return svg.replace('rx="15"', 'rx="0"')


def tile_only(svg: str) -> str:
    """The background tile without the routing mark (strip the connector group and the node circles)."""
    return re.sub(r"<circle[^>]*/>", "", re.sub(r"<g .*?</g>", "", svg, flags=re.S))


def maskable(size: int) -> Image.Image:
    bg = render(full_bleed(tile_only(SVG)), size)  # background only; the mark is added once, smaller, below
    mark = render(SVG.replace(TILE, ""), int(size * 0.8))  # just the routing glyph, no tile
    bg.alpha_composite(mark, ((size - mark.width) // 2, (size - mark.height) // 2))
    return bg


def main() -> None:
    for size, name in ((192, "icon-192.png"), (512, "icon-512.png")):
        render(SVG, size).save(STATIC / name, optimize=True)
    tile = render(full_bleed(SVG), 180)
    flat = Image.new("RGB", tile.size, "white")
    flat.paste(tile, mask=tile.split()[3])
    flat.save(STATIC / "apple-touch-icon.png", optimize=True)
    maskable(512).save(STATIC / "icon-maskable-512.png", optimize=True)
    render(SVG, 256).save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    for p in sorted(STATIC.glob("*icon*")) + sorted(STATIC.glob("favicon*")):
        print(f"{p.name:26} {p.stat().st_size:>7} bytes")


if __name__ == "__main__":
    main()
