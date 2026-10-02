#!/usr/bin/env python3
"""Convert any image to a web-ready file for site/img/ (used by the website robot and by hand).

    python3 tools/img_for_web.py input.png site/img/team-photo.webp --width 1200

Respects EXIF rotation, never upscales, strips metadata. Output format follows the extension
(.webp recommended, .jpg/.png also fine).
"""
import argparse
from pathlib import Path
from PIL import Image, ImageOps

ap = argparse.ArgumentParser()
ap.add_argument("src")
ap.add_argument("dest")
ap.add_argument("--width", type=int, default=1600, help="max width in px (default 1600)")
a = ap.parse_args()
dest = Path(a.dest)
if not dest.resolve().is_relative_to(Path(__file__).resolve().parent.parent / "site" / "img"):
    raise SystemExit("destination must be inside site/img/")
im = ImageOps.exif_transpose(Image.open(a.src))
if im.width > a.width:
    im = im.resize((a.width, round(im.height * a.width / im.width)), Image.LANCZOS)
fmt = {".webp": "WEBP", ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}.get(dest.suffix.lower())
if not fmt:
    raise SystemExit("use .webp, .jpg or .png")
if fmt == "JPEG":
    im = im.convert("RGB")
dest.parent.mkdir(parents=True, exist_ok=True)
im.save(dest, fmt, quality=85, optimize=True, **({"method": 6} if fmt == "WEBP" else {}))
print(f"wrote {dest} {im.width}x{im.height} {dest.stat().st_size // 1024} KB")
