#!/usr/bin/env python3
"""Web-ready images for the site (used by the Slack website robot and by hand).

Add a new image:
    python3 tools/img_for_web.py photo.png site/img/team-dinner.webp --width 1200
    python3 tools/img_for_web.py photo.png site/img/hero-office.webp --size 1600x900   # exact size, cropped
    python3 tools/img_for_web.py photo.png site/img/hs-new-person-sq.webp --size 320x320 --headshot

Swap an image that is already on the site (a team headshot, a logo, a photo):
    python3 tools/img_for_web.py new.png --replace site/img/hs-tyler-sq.webp
  * same size and shape as the old image; people photos are framed on the face the way the old one was
  * saved under a NEW file name: the host caches images for 30 days, so reusing the old name would keep
    showing the old picture to visitors
  * every reference under the site folder is updated, WordPress-style size variants used in srcset
    (name-240x300.webp) are regenerated, and the old file(s) are removed

Inspect images (size, orientation, where the face is, which pages use them):
    python3 tools/img_for_web.py --info photo.png site/img/hs-tyler-sq.webp

Check attachments and make viewable copies (the robot runs this on everything posted in Slack):
    python3 tools/img_for_web.py --prepare .request-files

Respects EXIF rotation, reads iPhone HEIC photos (pillow-heif), never upscales, strips metadata.
Face finding uses OpenCV (YuNet) when it is installed and falls back to a sensible upper-centre crop.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageOps

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")   # keep OpenCV's chatter out of the output

try:                                   # iPhone photos (.heic/.heif)
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:                      # optional: everything else still works
    pass

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"                   # the published site: every image reference lives under here
IMG = SITE / "img"                     # images must live here
FACE_MODEL = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"
TEXT_EXT = {".html", ".htm", ".css", ".js", ".json", ".xml", ".txt", ".md", ".svg", ".webmanifest",
            ".yml", ".yaml", ".csv"}
FORMATS = {".webp": "WEBP", ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}
VIEWABLE = {"PNG", "JPEG", "WEBP", "GIF"}        # what Claude's Read tool can look at directly
HEADSHOT_FACE = 0.46                   # face width / photo width in the site's team headshots
HEADSHOT_EYE_Y = 0.42                  # face centre height / photo height in those headshots
HASH_RE = re.compile(r"-(?=[0-9a-f]*\d)[0-9a-f]{6}$")


# ----------------------------------------------------------------------------- basics
def load(path: Path) -> Image.Image:
    with Image.open(path) as im:
        im.load()
        return ImageOps.exif_transpose(im)


def image_format(path: Path) -> str | None:
    with Image.open(path) as im:
        return im.format


def find_face(im: Image.Image) -> tuple[float, float, float, float] | None:
    """(x, y, w, h) in pixels of the most prominent face, or None (no face, or OpenCV not installed).
    Uses OpenCV's YuNet detector (tools/models/, MIT licence from opencv_zoo)."""
    try:
        import cv2
        import numpy as np
        rgb = np.asarray(im.convert("RGB"))
        scale = min(1.0, 1000 / max(rgb.shape[:2]))
        if scale < 1:
            rgb = cv2.resize(rgb, (round(rgb.shape[1] * scale), round(rgb.shape[0] * scale)),
                             interpolation=cv2.INTER_AREA)
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        h, w = bgr.shape[:2]
        det = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), 0.75, 0.3, 50)
        det.setInputSize((w, h))
        _, faces = det.detect(bgr)
    except Exception:
        return None
    if faces is None or len(faces) == 0:
        return None
    small = max(16, min(w, h) * 0.06)           # ignore faces in the background
    faces = [f for f in faces if f[2] >= small]
    if not faces:
        return None
    x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3] * f[-1])[:4]
    return float(x) / scale, float(y) / scale, float(fw) / scale, float(fh) / scale


def parse_size(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[x×:]\s*(\d+)\s*", s or "")
    if not m or not int(m.group(1)) or not int(m.group(2)):
        raise SystemExit(f"bad size {s!r}: use WIDTHxHEIGHT, e.g. 320x320 (or W:H for --crop)")
    return int(m.group(1)), int(m.group(2))


def crop_box(im: Image.Image, aspect: float, focus: str = "auto", headshot: bool | float = False,
             face_y: float = HEADSHOT_EYE_Y) -> tuple[int, int, int, int]:
    """Box (left, top, right, bottom) with width/height == aspect.

    headshot: frame head-and-shoulders like a team photo (True = the site's usual framing, or a float =
    the face width as a share of the photo width, copied from the photo being replaced).
    focus: "auto"/"face" (centre on the face if there is one), "center", "top", or "X,Y" (0-1 shares)."""
    iw, ih = im.size
    face = find_face(im) if focus in ("auto", "face") or headshot else None
    if face:
        fx, fy = face[0] + face[2] / 2, face[1] + face[3] / 2
        vpos = face_y
    elif focus not in ("auto", "face", "center", "top") and "," in focus:
        a, b = (float(v) for v in focus.split(",", 1))
        fx, fy, vpos = a * iw, b * ih, 0.5
    else:
        fx = iw / 2
        top_bias = focus == "top" or (focus in ("auto", "face") and ih > iw)   # portraits: keep the head in
        fy, vpos = (ih * 0.30, 0.30) if top_bias else (ih / 2, 0.5)
    cw = min(iw, ih * aspect)                   # largest box of this shape that fits
    if face and headshot:
        share = HEADSHOT_FACE if headshot is True else float(headshot)
        cw = min(cw, max(face[2] / share, face[2] * 1.2))
    ch = cw / aspect
    left = min(max(fx - cw / 2, 0), iw - cw)
    top = min(max(fy - ch * vpos, 0), ih - ch)
    return round(left), round(top), round(left + cw), round(top + ch)


def render(im: Image.Image, size: tuple[int, int] | None = None, width: int | None = None,
           height: int | None = None, crop: tuple[int, int] | None = None, focus: str = "auto",
           headshot: bool | float = False, face_y: float = HEADSHOT_EYE_Y) -> Image.Image:
    """Crop (if a shape is asked for) and shrink. Never upscales: a small source keeps its own size."""
    shape = size or crop
    if shape:
        im = im.crop(crop_box(im, shape[0] / shape[1], focus, headshot, face_y))
    if size:
        if im.width > size[0]:
            im = im.resize(size, Image.LANCZOS)
        return im
    w = im.width
    if width and w > width:
        w = width
    if height and im.height * w / im.width > height:
        w = im.width * height / im.height
    if w < im.width:
        im = im.resize((max(1, round(w)), max(1, round(im.height * w / im.width))), Image.LANCZOS)
    return im


def encode(im: Image.Image, suffix: str) -> bytes:
    fmt = FORMATS.get(suffix.lower())
    if not fmt:
        raise SystemExit("use .webp, .jpg or .png")
    if fmt == "JPEG" and im.mode != "RGB":
        bg = Image.new("RGB", im.size, "white")
        rgba = im.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    elif im.mode not in ("RGB", "RGBA", "L", "LA"):
        im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
    buf = io.BytesIO()
    im.save(buf, fmt, quality=85, optimize=True, **({"method": 6} if fmt == "WEBP" else {}))
    return buf.getvalue()


def inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def site_rel(path: Path) -> str:
    return path.resolve().relative_to(SITE.resolve()).as_posix()


def text_files():
    for p in sorted(SITE.rglob("*")):
        if p.is_file() and p.suffix.lower() in TEXT_EXT:
            yield p


def ref_re(rel: str) -> re.Pattern:
    """A reference to site-relative path `rel` ("img/a.webp"), as /img/a.webp, img/a.webp or a full URL;
    not a longer name that merely ends the same way (img/a.webp vs img/a-240x300.webp or xa.webp)."""
    return re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(rel) + r"(?![A-Za-z0-9_-])")


def references(rel: str) -> dict[Path, int]:
    pat = ref_re(rel)
    out = {}
    for p in text_files():
        n = len(pat.findall(p.read_text(errors="ignore")))
        if n:
            out[p] = n
    return out


def describe(path: Path) -> dict:
    im = load(path)
    info = {"file": str(path), "width": im.width, "height": im.height,
            "orientation": "portrait" if im.height > im.width * 1.05 else
                           "landscape" if im.width > im.height * 1.05 else "square",
            "format": image_format(path) or "?"}
    face = find_face(im)
    if face:
        x, y, w, h = face
        info["face"] = {"left": round(x / im.width, 3), "top": round(y / im.height, 3),
                        "width": round(w / im.width, 3), "height": round(h / im.height, 3)}
    if inside(path, SITE):
        info["used_in"] = sorted(str(p.relative_to(ROOT)) for p in references(site_rel(path)))
    return info


# ----------------------------------------------------------------------------- commands
def cmd_new(a) -> int:
    dest = Path(a.dest)
    if not inside(dest, IMG):
        raise SystemExit(f"destination must be inside {IMG.relative_to(ROOT)}/")
    if dest.exists() and not a.force:
        raise SystemExit(f"{dest} already exists. To swap a picture that is on the site use "
                         f"--replace {dest} (it gets a fresh file name so visitors see the new one).")
    im = render(load(Path(a.src)), size=parse_size(a.size) if a.size else None, width=a.width,
                height=a.height, crop=parse_size(a.crop) if a.crop else None, focus=a.focus,
                headshot=a.headshot)
    data = encode(im, dest.suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"wrote {dest} {im.width}x{im.height} {len(data) // 1024} KB")
    return 0


def new_name(old: Path, data: bytes) -> str:
    stem = HASH_RE.sub("", old.stem)
    return f"{stem}-{hashlib.sha1(data).hexdigest()[:6]}"


def cmd_replace(a) -> int:
    old = Path(a.replace).resolve()
    if not old.is_file():
        raise SystemExit(f"{old} doesn't exist. Use --info on the page's image to find the right file.")
    if not inside(old, IMG):
        raise SystemExit(f"--replace only works for images inside {IMG.relative_to(ROOT)}/")
    rel = site_rel(old)
    refs = references(rel)
    if not refs:
        raise SystemExit(f"No page uses {rel}, so replacing it would change nothing. Find the file the page "
                         "actually shows (the <img src=...> in the page) and replace that one.")
    src = load(Path(a.src))
    old_im = load(old)
    W, H = parse_size(a.size) if a.size else old_im.size
    # People photos: copy the old picture's framing (how big the face is, where it sits).
    headshot, face_y = a.headshot, HEADSHOT_EYE_Y
    old_face = find_face(old_im)
    if old_face and not a.no_headshot:
        share = old_face[2] / old_im.width
        if share >= 0.18:                             # a portrait, not a crowd or an office shot
            headshot = min(max(share, 0.25), 0.7)
            face_y = min(max((old_face[1] + old_face[3] / 2) / old_im.height, 0.25), 0.6)
    out = render(src, size=(W, H), focus=a.focus, headshot=headshot, face_y=face_y)
    ext = old.suffix if old.suffix.lower() in FORMATS else ".webp"
    data = encode(out, ext)
    stem = new_name(old, data)
    new = old.with_name(stem + ext)
    jobs = [(old, new, data, out.size)]
    variant_re = re.compile(re.escape(old.stem) + r"-(\d+)x(\d+)" + re.escape(old.suffix))
    for sib in sorted(old.parent.iterdir()):
        m = variant_re.fullmatch(sib.name)
        if m:
            vw, vh = int(m.group(1)), int(m.group(2))
            v = render(src, size=(vw, vh), focus=a.focus, headshot=headshot, face_y=face_y)
            jobs.append((sib, old.with_name(f"{stem}-{v.width}x{v.height}{ext}"), encode(v, ext), v.size))
    changed: dict[Path, int] = {}
    for p in text_files():
        text = p.read_text(errors="ignore")
        n_total = 0
        for o, n, _, _ in jobs:
            text, n_sub = ref_re(site_rel(o)).subn(site_rel(n), text)
            n_total += n_sub
        if n_total:
            p.write_text(text)
            changed[p] = n_total
    for o, n, d, _ in jobs:
        n.write_bytes(d)
        if o.resolve() != n.resolve():
            o.unlink()
    left = [site_rel(o) for o, _, _, _ in jobs if references(site_rel(o))]
    framing = ("framed on the face like the old photo" if headshot and find_face(src) else
               "no face found, cropped from the upper centre" if headshot else "cropped to the same shape")
    print(f"replaced {old.relative_to(ROOT)} -> {new.relative_to(ROOT)} ({out.width}x{out.height}, {framing})")
    for o, n, _, size in jobs[1:]:
        print(f"  size variant {o.name} -> {n.name} ({size[0]}x{size[1]})")
    print(f"updated {sum(changed.values())} reference(s) in: "
          + ", ".join(str(p.relative_to(ROOT)) for p in changed))
    if left:
        print("WARNING still referenced (fix these by hand): " + ", ".join(left))
        return 1
    return 0


def cmd_info(paths: list[str]) -> int:
    for p in paths:
        print(json.dumps(describe(Path(p))))
    return 0


def cmd_prepare(folder: str) -> int:
    """For every attachment: check it's a real image, write a viewable JPEG copy when Claude can't read the
    original (HEIC, TIFF, huge files), and print one JSON line of facts per file."""
    for p in sorted(Path(folder).iterdir()):
        if not p.is_file() or ".view." in p.name:
            continue
        rec = {"file": p.name, "bytes": p.stat().st_size}
        try:
            fmt = image_format(p)
            im = load(p)
        except Exception:
            head = p.read_bytes()[:200].lstrip().lower()
            rec["kind"] = ("broken: Slack sent a web page instead of the file"
                           if head.startswith((b"<!doctype", b"<html")) else "not an image")
            print(json.dumps(rec))
            continue
        rec.update(kind="image", format=fmt, width=im.width, height=im.height)
        rec["face"] = bool(find_face(im))
        if fmt not in VIEWABLE or p.stat().st_size > 3_500_000 or max(im.size) > 4000:
            view = p.with_name(p.stem + ".view.jpg")
            view.write_bytes(encode(render(im, width=1600, height=1600), ".jpg"))
            rec["view"] = view.name
        print(json.dumps(rec))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="?")
    ap.add_argument("dest", nargs="?")
    ap.add_argument("--replace", metavar="OLD", help="swap this image (inside the site's image folder)")
    ap.add_argument("--info", nargs="+", metavar="FILE", help="print facts about images and exit")
    ap.add_argument("--prepare", metavar="DIR", help="check + make viewable copies of attachments")
    ap.add_argument("--width", type=int, default=1600, help="max width in px (default 1600)")
    ap.add_argument("--height", type=int, help="max height in px")
    ap.add_argument("--size", help="exact output size WxH (cropped to fit, never upscaled)")
    ap.add_argument("--crop", help="crop to this shape W:H (e.g. 1:1, 16:9) before sizing")
    ap.add_argument("--focus", default="auto", help="auto/face (default), center, top, or X,Y shares 0-1")
    ap.add_argument("--headshot", action="store_true", help="frame head-and-shoulders like a team photo")
    ap.add_argument("--no-headshot", action="store_true", help="--replace: don't reframe on the face")
    ap.add_argument("--force", action="store_true", help="overwrite an existing file (avoid: caching)")
    a = ap.parse_args(argv)
    if a.info:
        return cmd_info(a.info + [x for x in (a.src, a.dest) if x])
    if a.prepare:
        return cmd_prepare(a.prepare)
    if not a.src:
        ap.error("need an input image")
    if a.replace:
        return cmd_replace(a)
    if not a.dest:
        ap.error("need a destination (or --replace OLD)")
    return cmd_new(a)


if __name__ == "__main__":
    sys.exit(main())
