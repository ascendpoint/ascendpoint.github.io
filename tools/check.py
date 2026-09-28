#!/usr/bin/env python3
"""Quality gate for dist/. Exit 1 on any ERROR (CI will not deploy).

Per page: <title> (10-70 chars), meta description (70-170), exactly one canonical that
matches the page's URL, og:image that exists, exactly one <h1>, valid JSON-LD, balanced
tags, no leftover {{tokens}}, no prototype copy, every internal link/src/srcset resolves
(including #anchors), no .html links, every <img> has alt + width + height, correct
indexing for the environment. Site-wide: unique titles + descriptions, sitemap contains
exactly the indexable pages, robots.txt/feed/llms present.

"Launch blockers" (draft quotes, [confirm] placeholders) are reported as warnings so the
staging preview can still deploy; tools/golive_check.py turns them into errors.
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, unquote

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BANNED = ["This is a prototype", "nothing was sent", "lorem ipsum", "{{", "}}"]
LAUNCH_BLOCKERS = [r"Draft quote", r"pending approval", r"\[[^\]]*confirm[^\]]*\]", r"\[Location\]", r"\bTBD\b", r"\bTODO\b"]


class Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors, self.ids = [], [], set()

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v:
                self.ids.add(v)
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                real = [t for t, _ in self.stack[i + 1:] if t not in {"p", "li", "option", "dt", "dd", "tr", "td", "th"}]
                if real:
                    self.errors.append(f"</{tag}> at line {self.getpos()[0]} closes over unclosed {real}")
                del self.stack[i:]
                return
        self.errors.append(f"stray </{tag}> at line {self.getpos()[0]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["staging", "production"], default="production")
    ap.add_argument("--dist", default=str(Path(__file__).resolve().parent.parent / "dist"))
    ap.add_argument("--base-url", default="https://ascendpoint.agency")
    ap.add_argument("--strict-launch", action="store_true", help="treat launch blockers as errors")
    a = ap.parse_args()
    dist, base = Path(a.dist), a.base_url.rstrip("/")
    errors, warns = [], []
    pages = sorted(dist.rglob("*.html"))
    if not pages:
        print("ERROR no pages in dist/")
        return 1

    def url_of(p: Path) -> str:
        rel = p.relative_to(dist).as_posix()
        if rel == "404.html":
            return "/404.html"
        return "/" if rel == "index.html" else "/" + rel[: -len("index.html")]

    ids_by_url, parsed = {}, {}
    for p in pages:
        pr = Parser()
        pr.feed(p.read_text(encoding="utf-8"))
        ids_by_url[url_of(p)] = pr.ids
        parsed[p] = pr

    def resolve(ref: str):
        """Return (Path or None, fragment). None = external/ignored."""
        u = urlparse(ref)
        if u.scheme in ("http", "https"):
            if u.netloc != urlparse(base).netloc:
                return None, ""
            path = u.path
        elif u.scheme or ref.startswith(("//", "mailto:", "tel:", "javascript:", "data:")):
            return None, ""
        else:
            path = u.path
        path = unquote(path)
        if not path:
            return "SAME", u.fragment
        if path.endswith("/"):
            return dist / path.lstrip("/") / "index.html", u.fragment
        return dist / path.lstrip("/"), u.fragment

    titles, descs, indexable = {}, {}, set()
    for p in pages:
        s = p.read_text(encoding="utf-8")
        url = url_of(p)
        rel = p.relative_to(dist).as_posix()
        def err(msg): errors.append(f"{rel}: {msg}")
        def warn(msg): warns.append(f"{rel}: {msg}")
        t = re.search(r"<title>([^<]*)</title>", s)
        if not t or len(t.group(1)) < 10:
            err("missing/short <title>")
        else:
            if len(t.group(1)) > 70:
                warn(f"title {len(t.group(1))} chars (Google shows ~60)")
            titles.setdefault(t.group(1), []).append(rel)
        noindex = bool(re.search(r'<meta name="robots" content="noindex', s))
        if rel != "404.html":
            d = re.search(r'<meta name="description" content="([^"]*)"', s)
            if not d:
                err("missing meta description")
            else:
                n = len(d.group(1))
                if not 70 <= n <= 170:
                    err(f"meta description {n} chars (want 70-170)")
                descs.setdefault(d.group(1), []).append(rel)
            canon = re.findall(r'<link rel="canonical" href="([^"]+)"', s)
            if canon != [base + url]:
                err(f"canonical {canon} != {base + url}")
            og = re.search(r'<meta property="og:image" content="([^"]+)"', s)
            if not og:
                err("missing og:image")
            else:
                f, _ = resolve(og.group(1))
                if f is None or not Path(f).exists():
                    err(f"og:image missing: {og.group(1)}")
            for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', s, re.S):
                try:
                    json.loads(block)
                except Exception as e:
                    err(f"invalid JSON-LD: {e}")
            if not re.search(r'application/ld\+json', s):
                err("no JSON-LD")
            if not noindex:
                indexable.add(base + url)
        h1 = len(re.findall(r"<h1[\s>]", s))
        if h1 != 1:
            err(f"{h1} <h1> tags (want exactly 1)")
        body = re.sub(r"<script\b.*?</script>", "", s, flags=re.S)
        for b in BANNED:
            if b.lower() in body.lower():
                err(f"contains {b!r}")
        text = re.sub(r"<[^>]+>", " ", body)
        for pat in LAUNCH_BLOCKERS:
            for m in re.findall(pat, text):
                (err if a.strict_launch else warn)(f"LAUNCH BLOCKER {m!r}")
        if a.env == "staging" and not noindex:
            err("staging page is indexable")
        for attr, ref in re.findall(r'\s(href|src)="([^"]+)"', s):
            if ref.startswith("http://") and "localhost" not in ref:
                err(f"insecure http:// {attr}: {ref}")
            if 'rel="preconnect"' in ref:
                continue
            f, frag = resolve(ref)
            if f is None:
                continue
            if f != "SAME" and re.search(r"\.html$", ref.split("#")[0]) and "404.html" not in ref:
                err(f"link uses .html (use the clean /path/ URL): {ref}")
            target = p if f == "SAME" else Path(f)
            if not target.exists():
                err(f"broken {attr}: {ref}")
                continue
            if frag and target.suffix == ".html":
                turl = url_of(target)
                if frag not in ids_by_url.get(turl, set()) and frag != "top":
                    err(f"#anchor not found: {ref}")
        for ss in re.findall(r'srcset="([^"]+)"', s):
            for part in ss.split(","):
                f, _ = resolve(part.strip().split(" ")[0])
                if f not in (None, "SAME") and not Path(f).exists():
                    err(f"broken srcset: {part}")
        for img in re.findall(r"<img\b[^>]*>", s):
            if not re.search(r'\salt="', img):
                err(f"img without alt: {img[:90]}")
            if not (re.search(r"\swidth=", img) and re.search(r"\sheight=", img)):
                err(f"img without width/height (layout shift): {img[:90]}")
        pr = parsed[p]
        errors.extend(f"{rel}: {e}" for e in pr.errors[:5])
        left = [t for t, _ in pr.stack if t not in {"p", "li", "option", "html", "body", "head"}]
        if left:
            err(f"unclosed tags at end: {left[:5]}")

    for t, where in titles.items():
        if len(where) > 1:
            errors.append(f"duplicate <title> {t!r}: {where}")
    for d, where in descs.items():
        if len(where) > 1:
            errors.append(f"duplicate description on {where}")

    sm = dist / "sitemap.xml"
    if not sm.exists():
        errors.append("sitemap.xml missing")
    else:
        locs = set(re.findall(r"<loc>([^<]+)</loc>", sm.read_text()))
        if a.env == "production":
            if locs - indexable:
                errors.append(f"sitemap lists non-indexable/missing pages: {sorted(locs - indexable)}")
            if indexable - locs:
                errors.append(f"indexable pages missing from sitemap: {sorted(indexable - locs)}")
    for f in ["robots.txt", "feed.xml", "llms.txt", "_redirects", "_headers", "favicon.ico", "favicon.svg"]:
        if not (dist / f).exists():
            errors.append(f"{f} missing")
    if a.env == "production" and "Disallow: /\n" in (dist / "robots.txt").read_text():
        errors.append("production robots.txt blocks crawlers")

    for w in warns:
        print("warn ", w)
    for e in errors:
        print("ERROR", e)
    blockers = sum("LAUNCH BLOCKER" in w for w in warns)
    print(f"checked {len(pages)} pages: {len(errors)} errors, {len(warns)} warnings ({blockers} launch blockers)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
