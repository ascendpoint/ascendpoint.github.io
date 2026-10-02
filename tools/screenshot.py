#!/usr/bin/env python3
"""Screenshot built pages so the website robot (or a person) can SEE a change, including motion.

    python3 tools/build.py                       # build dist/ first
    python3 tools/screenshot.py /                # desktop + mobile shots of the home page
    python3 tools/screenshot.py /about/ --frames 0,400,1200 --scroll 900
    python3 tools/screenshot.py / --scroll-to ".grid.g4" --frames 50,400,900,1600 --viewport-only
    python3 tools/screenshot.py / --selector ".hero" --hover ".btn"

Serves dist/ on a private localhost port, opens it in headless Chromium (Playwright) and writes PNGs to
/tmp/website_request_shots/ (or --out). Prints one path per line; open them with an image viewer, or
Claude's Read tool. --frames captures the viewport N ms after load (and after --scroll), which is how
animations are checked. Needs `pip install playwright` + `python -m playwright install chromium`.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import re
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
SIZES = {"desktop": (1440, 900), "mobile": (390, 844)}


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_head(self):  # pretty URLs: /about/ -> /about/index.html, /x -> /x.html or /x/index.html
        path = self.translate_path(self.path)
        p = Path(path)
        if not p.exists() and Path(path + ".html").exists():
            self.path = self.path.split("?")[0] + ".html"
        return super().send_head()


def serve(directory: Path) -> tuple[socketserver.TCPServer, int]:
    handler = functools.partial(_Quiet, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "home"


def ensure_playwright():
    """In CI, install Playwright + Chromium on first use if the runner doesn't have them yet."""
    try:
        import playwright  # noqa: F401
        return
    except ImportError:
        if not os.environ.get("CI"):
            sys.exit("Playwright is not installed: pip install playwright && python -m playwright install chromium")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "playwright"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


def shoot(paths, out: Path, devices, frames, scroll, selector, hover, full_page, scroll_to=None) -> list[Path]:
    ensure_playwright()
    from playwright.sync_api import sync_playwright

    if not (DIST / "index.html").exists():
        sys.exit("dist/ is empty: run python3 tools/build.py first")
    out.mkdir(parents=True, exist_ok=True)
    httpd, port = serve(DIST)
    written = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for dev in devices:
                w, h = SIZES[dev]
                ctx = browser.new_context(viewport={"width": w, "height": h}, reduced_motion="no-preference",
                                          device_scale_factor=1)
                page = ctx.new_page()
                for path in paths:
                    url = f"http://127.0.0.1:{port}{path if path.startswith('/') else '/' + path}"
                    page.goto(url, wait_until="networkidle")
                    if scroll_to:   # jump (no smooth scrolling) so on-scroll effects start at frame 0
                        page.locator(scroll_to).first.evaluate(
                            "el => { document.documentElement.style.scrollBehavior = 'auto';"
                            " el.scrollIntoView({block: 'center', behavior: 'instant'}); }")
                    elif scroll:
                        page.evaluate(f"document.documentElement.style.scrollBehavior='auto'; window.scrollBy(0, {scroll})")
                    if hover:
                        page.hover(hover)
                    last = 0
                    for ms in frames:
                        page.wait_for_timeout(max(0, ms - last))
                        last = ms
                        name = f"{slug(path)}-{dev}" + (f"-{ms}ms" if len(frames) > 1 or ms else "") + ".png"
                        target = out / name
                        if selector:
                            page.locator(selector).first.screenshot(path=str(target))
                        else:
                            page.screenshot(path=str(target), full_page=full_page and len(frames) == 1)
                        written.append(target)
                ctx.close()
            browser.close()
    finally:
        httpd.shutdown()
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="site paths like / or /about/")
    ap.add_argument("--out", default="/tmp/website_request_shots")
    ap.add_argument("--desktop", action="store_true", help="desktop only")
    ap.add_argument("--mobile", action="store_true", help="mobile only")
    ap.add_argument("--frames", default="0", help="comma-separated ms after load, e.g. 0,400,1200")
    ap.add_argument("--scroll", type=int, default=0, help="scroll down this many px before capturing")
    ap.add_argument("--scroll-to", help="scroll this element (CSS selector) to the middle of the screen first")
    ap.add_argument("--selector", help="capture just this element")
    ap.add_argument("--hover", help="hover this element before capturing")
    ap.add_argument("--viewport-only", action="store_true", help="no full-page capture")
    a = ap.parse_args(argv)
    devices = ["desktop"] if a.desktop else ["mobile"] if a.mobile else ["desktop", "mobile"]
    frames = sorted(int(x) for x in a.frames.split(",") if x.strip())
    for p in shoot(a.paths, Path(a.out), devices, frames, a.scroll, a.selector, a.hover, not a.viewport_only,
                   a.scroll_to):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
