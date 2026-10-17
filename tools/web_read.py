#!/usr/bin/env python3
"""Read web pages for the website robot: a link in a Slack request becomes a clean, local copy.

    python3 tools/web_read.py https://serpdental.com/blog/some-post/
    python3 tools/web_read.py URL [URL ...] --out .request-files/web [--max-images 12] [--render auto|always|never]

For every URL it writes a folder under --out (default .request-files/web/<slug>/):
    page.md      the article as Markdown: title, author, date, description, then the body with headings,
                 paragraphs, lists, quotes, tables, links (absolute) and images, then a list of the images saved
    meta.json    title, author, published date, description, share image, canonical, site name, word count
    img-01.webp  the share image and the article's own images (largest srcset version), ready for
    img-02.jpg   tools/img_for_web.py (never hot-link another site's images)
    ...
and prints one JSON line per URL: {"url", "ok", "dir", "md", "title", "words", "images", "error"}.

What it understands
  * normal web pages and blog posts (finds the article body even on page builders with no <article> tag:
    the block holding the most paragraph text wins; nav, cookie banners, sidebars, footers and forms are dropped)
  * pages that only render with JavaScript: when the plain download has almost no text it opens the page in
    headless Chromium (Playwright, installed for tools/screenshot.py) and reads that instead
  * Google Docs / Sheets / Slides links shared "anyone with the link": read through Google's export
  * PDFs (saved as document.pdf + text in page.md), images (saved as img-01.*), plain text / Markdown / JSON

Safety: http(s) only, never private / loopback / link-local addresses (checked again on every redirect),
10 MB per page, 10 MB per image, 30 s timeouts, a browser-like user agent (some hosts refuse Python's).
Standard library only; Pillow (image sizes), pypdf (PDF text) and Playwright are used when installed.
"""
from __future__ import annotations

import argparse
import html as htmllib
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 AscendPointAI-robot/1.0 (+https://ascendpoint.agency)")
MAX_PAGE = 10 * 1024 * 1024
MAX_IMAGE = 10 * 1024 * 1024
TIMEOUT = 30
RENDER_BELOW_WORDS = 120          # fewer words than this from the plain download -> try headless Chromium
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif", "image/avif": ".avif", "image/svg+xml": ".svg", "image/heic": ".heic"}

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track",
        "wbr"}
SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "form", "button", "input", "select",
        "textarea", "nav", "aside", "footer", "dialog", "object", "head", "title", "meta", "link"}
BLOCK = {"p", "div", "section", "article", "main", "header", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5",
         "h6", "blockquote", "pre", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "figure", "figcaption",
         "hr", "dl", "dt", "dd", "details", "summary", "address", "center", "body", "html", "picture", "video",
         "iframe", "caption"}
# a <p> (or <li>, heading...) implicitly ends an open <p>
P_CLOSERS = {"p", "div", "ul", "ol", "table", "blockquote", "pre", "figure", "section", "article", "header",
             "h1", "h2", "h3", "h4", "h5", "h6", "hr", "dl", "address", "main", "aside", "nav", "footer", "form"}
# class/id words that mark page furniture, not the article (whole word, or the start of one: "share-buttons")
NOISE_RE = re.compile(r"^(cookie|consent|gdpr|newsletter|subscribe|popup|modal|share|sharing|sharedaddy|social|"
                      r"related|breadcrumbs?|comments?|sidebar|advert|ads?-|promo|skip-link|screen-reader|"
                      r"sr-only|visually-hidden)", re.I)


class FetchError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- fetching
def _allow_private() -> bool:
    return os.environ.get("WEB_READ_ALLOW_PRIVATE") == "1"     # tests serve pages from 127.0.0.1


def check_url(url: str) -> str:
    """Only public http(s) addresses. Returns the URL, raises FetchError otherwise."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError(f"not a web address: {url}")
    if _allow_private():
        return url
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError(f"can't find {u.hostname} ({e.strerror or e})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            raise FetchError(f"{u.hostname} is a private/internal address; refusing to read it")
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect())


def fetch(url: str, limit: int = MAX_PAGE, accept: str = "*/*", timeout: int = TIMEOUT):
    """(final url, content type, bytes). Raises FetchError with a plain-English reason."""
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept,
                                               "Accept-Language": "en-US,en;q=0.9"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            data = r.read(limit + 1)
            ctype = (r.headers.get("Content-Type") or "").lower()
            final = r.geturl()
    except urllib.error.HTTPError as e:
        reason = {401: "it needs a login", 403: "the site refused (403)", 404: "page not found (404)",
                  410: "page removed (410)", 429: "the site is rate-limiting (429)"}.get(e.code, f"HTTP {e.code}")
        raise FetchError(reason)
    except FetchError:
        raise
    except Exception as e:                            # DNS, TLS, timeout...
        raise FetchError(f"couldn't connect ({getattr(e, 'reason', e)})")
    if len(data) > limit:
        raise FetchError(f"over {limit // 2**20} MB")
    return final, ctype, data


def decode(data: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype)
    enc = m.group(1) if m else None
    if not enc:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", data[:4096], re.I)
        enc = m.group(1).decode() if m else "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------- a tiny DOM
class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.children, self.parent = tag, attrs or {}, [], parent

    def text(self) -> str:
        out = []
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag not in SKIP:
                out.append(c.text())
        return "".join(out)

    def iter(self, tag=None):
        for c in self.children:
            if isinstance(c, Node):
                if tag is None or c.tag == tag:
                    yield c
                yield from c.iter(tag)

    def get(self, k, d=""):
        return self.attrs.get(k, d) or d


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in P_CLOSERS or tag == "li":
            self._close_open({"p"} if tag != "li" else {"p", "li"}, stop={"ul", "ol", "div", "td", "blockquote"})
        node = Node(tag, a, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v or "") for k, v in attrs}, self.cur))

    def _close_open(self, tags, stop):
        n = self.cur
        while n is not None and n.tag != "#root":
            if n.tag in tags:
                self.cur = n.parent
                return
            if n.tag in stop:
                return
            n = n.parent

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != "#root":
            if n.tag == tag:
                self.cur = n.parent
                return
            n = n.parent                                  # stray end tag: ignore

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html: str) -> Node:
    tb = TreeBuilder()
    tb.feed(html)
    tb.close()
    return tb.root


# ----------------------------------------------------------------------------- metadata
def _meta(root: Node) -> dict:
    out = {}
    for m in root.iter("meta"):
        k = (m.get("property") or m.get("name") or m.get("itemprop")).lower()
        if k and m.get("content") and k not in out:
            out[k] = m.get("content").strip()
    return out


def _jsonld(root: Node) -> list[dict]:
    items = []
    for s in root.iter("script"):
        if "ld+json" not in s.get("type").lower():
            continue
        try:
            data = json.loads("".join(c for c in s.children if isinstance(c, str)))
        except Exception:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                items.append(d)
                if "@graph" in d:
                    stack.append(d["@graph"])
    return items


def _name(v) -> str:
    if isinstance(v, list):
        return ", ".join(filter(None, (_name(x) for x in v)))
    if isinstance(v, dict):
        return str(v.get("name") or "")
    return str(v or "")


def _img(v) -> str:
    if isinstance(v, list):
        return _img(v[0]) if v else ""
    if isinstance(v, dict):
        return str(v.get("url") or v.get("contentUrl") or "")
    return str(v or "")


def metadata(root: Node, url: str) -> dict:
    m = _meta(root)
    art = next((d for d in _jsonld(root)
                if any(t in str(d.get("@type")) for t in ("Article", "BlogPosting", "NewsArticle", "Report"))), {})
    title_tag = next((t.text().strip() for t in root.iter("title")), "")
    h1 = next((re.sub(r"\s+", " ", h.text()).strip() for h in root.iter("h1")), "")
    canonical = next((l.get("href") for l in root.iter("link") if "canonical" in l.get("rel").lower()), "")
    time_tag = next((t.get("datetime") for t in root.iter("time") if t.get("datetime")), "")
    author = _name(art.get("author")) or m.get("author") or ""
    if not author and m.get("article:author", "").strip() and not m["article:author"].startswith("http"):
        author = m["article:author"]
    image = m.get("og:image") or m.get("twitter:image") or _img(art.get("image"))
    html_tag = next(root.iter("html"), None)
    return {
        "title": (art.get("headline") or m.get("og:title") or h1 or title_tag or "").strip(),
        "h1": h1,
        "page_title": title_tag,
        "description": (m.get("description") or m.get("og:description") or art.get("description") or "").strip(),
        "author": author.strip(),
        "published": str(art.get("datePublished") or m.get("article:published_time") or time_tag or "")[:25],
        "modified": str(art.get("dateModified") or m.get("article:modified_time") or "")[:25],
        "site_name": m.get("og:site_name") or _name(art.get("publisher")) or "",
        "image": urllib.parse.urljoin(url, image) if image else "",
        "image_alt": m.get("og:image:alt") or m.get("twitter:image:alt") or "",
        "canonical": urllib.parse.urljoin(url, canonical) if canonical else "",
        "lang": html_tag.get("lang") if html_tag else "",
        "keywords": [v for k, v in _meta_all(root) if k in ("article:tag", "keywords")],
    }


def _meta_all(root: Node):
    for m in root.iter("meta"):
        k = (m.get("property") or m.get("name")).lower()
        if k and m.get("content"):
            yield k, m.get("content").strip()


# ----------------------------------------------------------------------------- finding the article
def _hidden(n: Node) -> bool:
    if "hidden" in n.attrs or n.get("aria-hidden") == "true":
        return True
    style = n.get("style").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    if n.tag in ("main", "article", "body", "html"):
        return False
    words = (n.get("class") + " " + n.get("id")).split()
    return any(NOISE_RE.match(w) for w in words) or n.get("role") in ("navigation", "complementary", "banner",
                                                                       "contentinfo", "dialog")


def _plen(n: Node) -> int:
    return len(re.sub(r"\s+", " ", n.text()).strip())


def find_content(root: Node) -> Node:
    """The element holding the article: <article> or <main> with real text, else the block with the most
    paragraph text directly inside it (or one level down)."""
    body = next(root.iter("body"), root)
    scores: dict[int, list] = {}

    def walk(n: Node):
        for c in n.children:
            if not isinstance(c, Node) or c.tag in SKIP or _hidden(c):
                continue
            if c.tag in ("p", "li", "h2", "h3", "blockquote", "pre", "td", "dd"):
                ln = _plen(c)
                if ln >= 25 or c.tag in ("h2", "h3"):
                    for depth, anc in enumerate((c.parent, c.parent.parent if c.parent else None)):
                        if anc is not None:
                            s = scores.setdefault(id(anc), [anc, 0])
                            s[1] += ln if depth == 0 else ln / 2
            walk(c)
    walk(body)
    best = max(scores.values(), key=lambda s: s[1], default=[None, 0])[0]
    for tag in ("article", "main"):
        cands = [a for a in body.iter(tag) if not _hidden(a)]
        if cands:
            big = max(cands, key=_plen)
            if best is None or _contains(big, best):
                best_text = _plen(best) if best is not None else 0
                if _plen(big) >= best_text * 0.9 and _plen(big) < max(best_text, 1) * 3:
                    return big
    return best or body


def _contains(a: Node, b: Node) -> bool:
    while b is not None:
        if b is a:
            return True
        b = b.parent
    return False


# ----------------------------------------------------------------------------- Markdown
def _img_src(n: Node, base: str) -> str:
    best, best_w = "", -1
    for attr in ("srcset", "data-srcset", "data-lazy-srcset"):
        for part in n.get(attr).split(","):
            bits = part.strip().split()
            if not bits:
                continue
            w = int(re.sub(r"\D", "", bits[1]) or 0) if len(bits) > 1 and bits[1].endswith("w") else 0
            if w > best_w:
                best, best_w = bits[0], w
    src = best or n.get("data-src") or n.get("data-lazy-src") or n.get("data-original") or n.get("src")
    if not src or src.startswith("data:"):
        return ""
    return urllib.parse.urljoin(base, htmllib.unescape(src))


class Markdown:
    def __init__(self, base: str):
        self.base = base
        self.images: list[dict] = []          # {"url", "alt"} in order of appearance
        self.links: list[dict] = []

    def image(self, n: Node) -> str:
        src = _img_src(n, self.base)
        if not src:
            return ""
        for dim in ("width", "height"):
            v = re.sub(r"\D", "", n.get(dim))
            if v and int(v) < 40:
                return ""                     # icons, tracking pixels
        alt = re.sub(r"\s+", " ", n.get("alt")).strip()
        if not any(i["url"] == src for i in self.images):
            self.images.append({"url": src, "alt": alt})
        return f"![{alt}]({src})"

    def inline(self, n) -> str:
        if isinstance(n, str):
            return re.sub(r"\s+", " ", n)
        if n.tag in SKIP or _hidden(n):
            return ""
        if n.tag == "br":
            return "  \n"
        if n.tag == "img":
            return self.image(n)
        inner = "".join(self.inline(c) for c in n.children)
        if n.tag == "a":
            href = n.get("href")
            text = inner.strip()
            if href and not href.startswith(("#", "javascript:")) and text:
                href = urllib.parse.urljoin(self.base, href)
                self.links.append({"url": href, "text": text})
                return f"[{text}]({href})"
            return inner
        if n.tag in ("strong", "b") and inner.strip():
            return f"**{inner.strip()}**" + (" " if inner.endswith(" ") else "")
        if n.tag in ("em", "i") and inner.strip():
            return f"*{inner.strip()}*" + (" " if inner.endswith(" ") else "")
        if n.tag == "code":
            return f"`{inner.strip()}`"
        if n.tag == "sup":
            return f"^{inner.strip()}"
        return inner

    def blocks(self, n: Node, depth: int = 0) -> list[str]:
        out, run = [], []

        def flush():
            t = re.sub(r"[ \t]+", " ", "".join(run)).strip()
            t = re.sub(r" *\n *", "\n", t)
            if t:
                out.append(t)
            run.clear()

        for c in n.children:
            if isinstance(c, str) or (c.tag not in BLOCK and c.tag not in SKIP):
                if isinstance(c, Node) and _hidden(c):
                    continue
                if isinstance(c, Node) and c.tag == "span" and any(
                        isinstance(g, Node) and g.tag in BLOCK for g in c.children):
                    flush()                   # page builders wrap whole articles in <span>
                    out.extend(self.blocks(c, depth))
                    continue
                run.append(self.inline(c))
                continue
            flush()
            if c.tag in SKIP or _hidden(c):
                if c.tag == "iframe":
                    out.extend(self.iframe(c))
                continue
            out.extend(self.block(c, depth))
        flush()
        return out

    def iframe(self, n: Node) -> list[str]:
        src = n.get("src") or n.get("data-src")
        if src and re.search(r"youtube|vimeo|wistia|loom", src):
            return [f"[Embedded video: {urllib.parse.urljoin(self.base, src)}]"]
        return []

    def block(self, c: Node, depth: int) -> list[str]:
        t = c.tag
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = re.sub(r"\s+", " ", "".join(self.inline(x) for x in c.children)).strip()
            return [f"{'#' * int(t[1])} {text}"] if text else []
        if t in ("ul", "ol"):
            return [self.list_md(c, depth)]
        if t == "blockquote":
            inner = "\n\n".join(self.blocks(c, depth))
            return ["\n".join("> " + line if line else ">" for line in inner.splitlines())] if inner else []
        if t == "pre":
            return ["```\n" + c.text().strip("\n") + "\n```"]
        if t == "table":
            return [self.table(c)]
        if t == "hr":
            return ["---"]
        if t == "iframe":
            return self.iframe(c)
        if t == "video":
            src = c.get("src") or next((s.get("src") for s in c.iter("source") if s.get("src")), "")
            return [f"[Video: {urllib.parse.urljoin(self.base, src)}]"] if src else []
        if t == "figure":
            parts = self.blocks(c, depth)
            return parts
        if t == "figcaption":
            text = re.sub(r"\s+", " ", "".join(self.inline(x) for x in c.children)).strip()
            return [f"*{text}*"] if text else []
        if t in ("details",):
            return self.blocks(c, depth)
        if t == "summary":
            text = re.sub(r"\s+", " ", "".join(self.inline(x) for x in c.children)).strip()
            return [f"**{text}**"] if text else []
        if t == "dt":
            text = re.sub(r"\s+", " ", "".join(self.inline(x) for x in c.children)).strip()
            return [f"**{text}**"] if text else []
        return self.blocks(c, depth)

    def list_md(self, n: Node, depth: int) -> str:
        lines, i = [], 0
        for li in n.children:
            if not isinstance(li, Node) or li.tag != "li" or _hidden(li):
                continue
            i += 1
            marker = f"{i}." if n.tag == "ol" else "-"
            own, nested = [], []
            for c in li.children:
                if isinstance(c, Node) and c.tag in ("ul", "ol"):
                    nested.append(self.list_md(c, depth + 1))
                elif isinstance(c, Node) and c.tag in BLOCK:
                    own.append(" " + " ".join(self.blocks(c, depth + 1)) + " ")
                else:
                    own.append(self.inline(c))
            text = re.sub(r"\s+", " ", "".join(own)).strip()
            lines.append("  " * depth + f"{marker} {text}")
            lines.extend(nested)
        return "\n".join(lines)

    def table(self, n: Node) -> str:
        rows = []
        for tr in n.iter("tr"):
            cells = [re.sub(r"\s+", " ", "".join(self.inline(x) for x in td.children)).strip().replace("|", "\\|")
                     for td in tr.children if isinstance(td, Node) and td.tag in ("td", "th")]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(out)


def to_markdown(content: Node, base: str) -> tuple[str, list[dict], list[dict]]:
    md = Markdown(base)
    blocks = md.blocks(content) if content.tag not in ("ul", "ol", "table") else md.block(content, 0)
    text = "\n\n".join(b for b in blocks if b.strip())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, md.images, md.links


def words(text: str) -> int:
    plain = re.sub(r"!\[[^\]]*\]\([^)]*\)|\]\([^)]*\)|[#>*`|_-]", " ", text)
    return len(re.findall(r"\w+", plain))


# ----------------------------------------------------------------------------- whole pages
def read_html(html: str, url: str) -> dict:
    root = parse(html)
    meta = metadata(root, url)
    content = find_content(root)
    body, images, links = to_markdown(content, url)
    # the page's H1 often sits outside the article block (a hero): keep the title at the top either way
    return {"meta": meta, "markdown": body, "images": images, "links": links, "words": words(body)}


def render_with_browser(url: str) -> str | None:
    """Full HTML after JavaScript ran (headless Chromium), or None when Playwright isn't available."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(user_agent=UA)
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(800)
            html = page.content()
            b.close()
            return html
    except Exception as e:
        print(f"render failed for {url}: {e}", file=sys.stderr)
        return None


GOOGLE_RE = re.compile(r"https://docs\.google\.com/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]{20,})")


def google_export(url: str) -> tuple[str, str] | None:
    """(export URL, kind) for a Google Docs/Sheets/Slides link; works when it is shared 'anyone with the link'."""
    m = GOOGLE_RE.match(url)
    if not m:
        return None
    kind, fid = m.groups()
    if kind == "document":
        return f"https://docs.google.com/document/d/{fid}/export?format=md", "doc"
    if kind == "spreadsheets":
        gid = re.search(r"[#&?]gid=(\d+)", url)
        return (f"https://docs.google.com/spreadsheets/d/{fid}/export?format=csv"
                + (f"&gid={gid.group(1)}" if gid else "")), "sheet"
    return f"https://docs.google.com/presentation/d/{fid}/export/txt", "slides"


def slug_for(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    s = (u.hostname or "page").removeprefix("www.") + "-" + u.path.strip("/").replace("/", "-")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:70].strip("-") or "page"


def save_images(images: list[dict], folder: Path, max_images: int) -> list[dict]:
    saved, total = [], 0
    for i, img in enumerate(images[:max_images], 1):
        try:
            final, ctype, data = fetch(img["url"], limit=MAX_IMAGE, accept="image/avif,image/webp,image/*,*/*")
        except FetchError as e:
            saved.append(dict(img, error=str(e)))
            continue
        ctype = ctype.split(";")[0].strip()
        ext = IMAGE_TYPES.get(ctype) or Path(urllib.parse.urlsplit(final).path).suffix.lower() or ".img"
        if ctype and not ctype.startswith("image/") and ext not in IMAGE_TYPES.values():
            saved.append(dict(img, error=f"not an image ({ctype})"))
            continue
        total += len(data)
        name = f"img-{i:02d}{ext}"
        (folder / name).write_bytes(data)
        entry = dict(img, file=name, bytes=len(data))
        try:
            from PIL import Image
            with Image.open(folder / name) as im:
                entry["width"], entry["height"] = im.size
        except Exception:
            pass
        saved.append(entry)
    return saved


def _header(meta: dict, url: str) -> str:
    lines = [f"# {meta.get('h1') or meta.get('title') or url}", "", f"Source: {url}"]
    facts = [("Site", meta.get("site_name")), ("Author", meta.get("author")),
             ("Published", meta.get("published")), ("Updated", meta.get("modified"))]
    line = " · ".join(f"{k}: {v}" for k, v in facts if v)
    if line:
        lines.append(line)
    if meta.get("title") and meta.get("title") != meta.get("h1"):
        lines.append(f"Headline (share/SEO title): {meta['title']}")
    if meta.get("description"):
        lines.append(f"Description: {meta['description']}")
    if meta.get("canonical") and meta["canonical"].rstrip("/") != url.rstrip("/"):
        lines.append(f"Canonical: {meta['canonical']}")
    return "\n".join(lines)


def read_url(url: str, out: Path, max_images: int = 12, render: str = "auto") -> dict:
    url = url.strip()
    folder = out / slug_for(url)
    folder.mkdir(parents=True, exist_ok=True)
    result = {"url": url, "ok": False, "dir": folder.as_posix(), "md": (folder / "page.md").as_posix()}
    try:
        g = google_export(url)
        if g:
            final, ctype, data = fetch(g[0])
            if "text/html" in ctype:
                raise FetchError("the Google file isn't shared 'anyone with the link', so it can't be read")
            text = decode(data, ctype)
            body = text if g[1] != "sheet" else "```csv\n" + text.strip() + "\n```"
            meta = {"title": f"Google {g[1]} {url}", "kind": f"google-{g[1]}"}
            (folder / "page.md").write_text(f"# Google {g[1]}\n\nSource: {url}\n\n---\n\n{body}\n")
            (folder / "meta.json").write_text(json.dumps(meta, indent=2))
            return dict(result, ok=True, title=meta["title"], words=words(body), images=[])

        final, ctype, data = fetch(url, accept="text/html,application/xhtml+xml,*/*;q=0.8")
        kind = ctype.split(";")[0].strip()
        if kind in IMAGE_TYPES or kind.startswith("image/"):
            name = "img-01" + (IMAGE_TYPES.get(kind) or ".img")
            (folder / name).write_bytes(data)
            (folder / "page.md").write_text(f"# Image\n\nSource: {url}\nSaved as {name} in this folder.\n")
            return dict(result, ok=True, title="image", words=0, images=[{"url": url, "file": name}])
        if kind == "application/pdf" or data[:5] == b"%PDF-":
            (folder / "document.pdf").write_bytes(data)
            text = ""
            try:
                import logging
                logging.getLogger("pypdf").setLevel(logging.CRITICAL)
                from pypdf import PdfReader
                text = "\n\n".join((p.extract_text() or "") for p in PdfReader(str(folder / "document.pdf")).pages)
            except Exception:
                pass
            (folder / "page.md").write_text(f"# PDF\n\nSource: {url}\nSaved as document.pdf in this folder "
                                            f"(open it with Read to see pages and images).\n\n---\n\n{text.strip()}\n")
            return dict(result, ok=True, title="PDF", words=words(text), images=[], pdf="document.pdf")
        if kind and "html" not in kind and "xml" not in kind:
            text = decode(data, ctype)[:2_000_000]
            (folder / "page.md").write_text(f"# {Path(urllib.parse.urlsplit(url).path).name or url}\n\n"
                                            f"Source: {url}\n\n---\n\n{text}\n")
            return dict(result, ok=True, title=url, words=words(text), images=[])

        html = decode(data, ctype)
        page = read_html(html, final)
        rendered = False
        if render == "always" or (render == "auto" and page["words"] < RENDER_BELOW_WORDS):
            full = render_with_browser(final)
            if full:
                again = read_html(full, final)
                if again["words"] > page["words"]:
                    page, rendered = again, True
        meta = page["meta"]
        images = list(page["images"])
        if meta.get("image") and not any(i["url"] == meta["image"] for i in images):
            images.insert(0, {"url": meta["image"], "alt": meta.get("image_alt", ""), "share_image": True})
        else:
            for i in images:
                if i["url"] == meta.get("image"):
                    i["share_image"] = True
        saved = save_images(images, folder, max_images) if max_images else []
        body = page["markdown"]
        first, _, rest = body.partition("\n")
        if first.startswith("# ") and first[2:].strip() in (meta.get("h1"), meta.get("title")):
            body = rest.strip()                        # the header above already shows the title
        for s in saved:                                # point Claude at the local copy of each image
            if s.get("file"):
                body = body.replace(f"]({s['url']})", f"]({s['url']}) _(saved as {s['file']})_", 1)
        lines = [_header(meta, final), "", "---", "", body or "(no article text found on this page)", ""]
        if saved:
            lines += ["---", "", "Images (saved in this folder; use these files, never link to the other site):"]
            for s in saved:
                if s.get("file"):
                    size = f" {s['width']}x{s['height']}" if s.get("width") else ""
                    tag = " [share image]" if s.get("share_image") else ""
                    lines.append(f"- {s['file']}{size}{tag} alt: \"{s.get('alt', '')}\" from {s['url']}")
                else:
                    lines.append(f"- (not saved: {s.get('error')}) {s['url']}")
        (folder / "page.md").write_text("\n".join(lines).rstrip() + "\n")
        meta.update(words=page["words"], final_url=final, rendered=rendered,
                    images=[{k: v for k, v in s.items()} for s in saved])
        (folder / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        title = meta.get("h1") or meta.get("title") or final
        if page["words"] < 40:
            return dict(result, ok=True, title=title, words=page["words"], warning="almost no text on this page",
                        images=[s.get("file") for s in saved if s.get("file")])
        return dict(result, ok=True, title=title, words=page["words"], rendered=rendered,
                    author=meta.get("author"), published=meta.get("published"),
                    images=[s.get("file") for s in saved if s.get("file")])
    except FetchError as e:
        (folder / "page.md").write_text(f"# Couldn't read this page\n\nSource: {url}\nReason: {e}\n")
        return dict(result, error=str(e))
    except Exception as e:                               # never crash the robot over one page
        (folder / "page.md").write_text(f"# Couldn't read this page\n\nSource: {url}\nReason: {e}\n")
        return dict(result, error=f"unexpected error: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--out", default=".request-files/web")
    ap.add_argument("--max-images", type=int, default=12)
    ap.add_argument("--render", choices=["auto", "always", "never"], default="auto")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    for url in a.urls[:10]:
        r = read_url(url, out, a.max_images, a.render)
        print(json.dumps(r, ensure_ascii=False), flush=True)
        bad += not r.get("ok")
    return 1 if bad == len(a.urls[:10]) else 0


if __name__ == "__main__":
    sys.exit(main())
