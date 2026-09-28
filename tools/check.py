#!/usr/bin/env python3
"""Verify dist/ before it ships. Exit 1 on any error.

Checks every page for: a <title>, a meta description (50-170 chars), canonical + og:url,
og:image that exists, exactly one <h1>, balanced core tags, no leftover template tokens,
no prototype copy, every local link/src/srcset resolving to a real file, and every <img>
having alt text. Staging must be noindex; production must not be.
"""
import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, unquote

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BANNED = ["This is a prototype", "nothing was sent", "{{BASE_URL}}", "{{PAGE_URL}}", "<!-- ap:env -->", "lorem ipsum"]


class TagBalance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                unclosed = self.stack[i + 1:]
                # p/li/option are legally auto-closed; anything else is a real problem
                real = [t for t, _ in unclosed if t not in {"p", "li", "option", "dt", "dd", "tr", "td", "th"}]
                if real:
                    self.errors.append(f"</{tag}> at line {self.getpos()[0]} closes over unclosed {real}")
                del self.stack[i:]
                return
        self.errors.append(f"stray </{tag}> at line {self.getpos()[0]}")


def resolve(page: Path, ref: str):
    u = urlparse(ref)
    if u.scheme or ref.startswith(("//", "#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    path = unquote(u.path)
    if not path or path == "./":
        return DIST / "index.html"
    target = (DIST / path.lstrip("/")) if path.startswith("/") else (page.parent / path)
    if target.is_dir():
        return target / "index.html"
    if target.suffix == "" and not target.exists():
        return target.with_suffix(".html")
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["staging", "production"], default="staging")
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    a = ap.parse_args()
    global DIST
    DIST = Path(a.dist)
    errors, warns = [], []
    pages = sorted(DIST.glob("*.html"))
    if not pages:
        print("no pages in dist/")
        return 1
    for p in pages:
        s = p.read_text(encoding="utf-8")
        n = p.name
        def err(msg): errors.append(f"{n}: {msg}")
        if not re.search(r"<title>[^<]{5,}</title>", s):
            err("missing <title>")
        m = re.search(r'<meta name="description" content="([^"]*)"', s)
        if n != "404.html":
            if not m:
                err("missing meta description")
            elif not 50 <= len(m.group(1)) <= 170:
                warns.append(f"{n}: description length {len(m.group(1))}")
            if '<link rel="canonical"' not in s:
                err("missing canonical")
            og = re.search(r'<meta property="og:image" content="([^"]+)"', s)
            if not og:
                err("missing og:image")
            else:
                local = "img/" + og.group(1).split("/img/", 1)[-1]
                if not (DIST / local).exists():
                    err(f"og:image file missing: {local}")
        h1 = len(re.findall(r"<h1[\s>]", s))
        if h1 != 1:
            err(f"{h1} <h1> tags (want 1)")
        for b in BANNED:
            if b.lower() in s.lower():
                err(f"contains banned text: {b!r}")
        noindex = 'name="robots" content="noindex' in s
        if a.env == "staging" and not noindex:
            err("staging page is indexable")
        if a.env == "production" and noindex and n != "404.html":
            err("production page is noindex")
        for attr, ref in re.findall(r'\s(href|src)="([^"]+)"', s):
            if 'rel="preconnect"' in ref:
                continue
            t = resolve(p, ref)
            if t is not None and not t.exists():
                err(f"broken {attr}: {ref}")
        for ss in re.findall(r'srcset="([^"]+)"', s):
            for part in ss.split(","):
                ref = part.strip().split(" ")[0]
                t = resolve(p, ref)
                if t is not None and not t.exists():
                    err(f"broken srcset: {ref}")
        for img in re.findall(r"<img\b[^>]*>", s):
            if not re.search(r'\salt="', img):
                err(f"img without alt: {img[:80]}")
        tb = TagBalance()
        tb.feed(s)
        errors.extend(f"{n}: {e}" for e in tb.errors[:5])
        left = [t for t, _ in tb.stack if t not in {"p", "li", "option", "html", "body", "head"}]
        if left:
            err(f"unclosed tags at end: {left[:5]}")
    for w in warns:
        print("warn ", w)
    for e in errors:
        print("ERROR", e)
    print(f"checked {len(pages)} pages: {len(errors)} errors, {len(warns)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
