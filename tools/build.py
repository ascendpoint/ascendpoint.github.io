#!/usr/bin/env python3
"""Build the AscendPoint Agency website: site/ -> dist/.

    python3 tools/build.py                          # production build for https://ascendpoint.agency
    python3 tools/build.py --env staging --base-url https://ascendpoint.github.io

HOW PAGES WORK
  One file per page in site/pages/. The file path IS the URL:
      site/pages/index.html                          -> /
      site/pages/about.html                          -> /about/
      site/pages/capabilities/healthcare-strategy.html -> /capabilities/healthcare-strategy/
      site/pages/404.html                            -> /404.html
  Each file starts with a front-matter comment, then the page content (what goes in <main>):
      <!--
      title: Healthcare Marketing Strategy | AscendPoint Agency      (<title>, ~60 chars)
      description: One sentence, 120-160 chars, for Google + social cards.
      image: /img/og/strategy.jpg        (1200x630 share image; optional)
      name: Strategy                     (short name for breadcrumbs / schema)
      type: page|home|about|contact|collection|service|news|post|landing|404
      date: 2026-03-01                   (news/post publish date)
      author: Sonia Hounsell             (news/post)
      layout: landing                    (optional: logo-only header, disclaimer footer)
      noindex: true                      (optional: keep out of Google + sitemap)
      list_*: ...                        (news/post: how it appears on /news/ or /blog/)
      -->
  Tokens you can use inside content:
      {{news_list}}  {{blog_list}}  {{> partial-name}} (site/_partials/partial-name.html)  {{year}}

WHAT THE BUILD ADDS FOR YOU (never hand-write these)
  canonical URL, Open Graph + Twitter tags, JSON-LD (Organization, WebSite, WebPage,
  BreadcrumbList, Service, NewsArticle, BlogPosting), width/height on every <img>,
  fingerprinted CSS/JS, sitemap.xml with real last-modified dates (git), feed.xml (RSS),
  llms.txt (for AI assistants), robots.txt, _redirects (with trailing-slash twins), _headers,
  Google Tag Manager on the production domain only, noindex on every other host.

Environments
  production  canonical = https://ascendpoint.agency; indexable ONLY when served from that
              host (a tiny inline script adds noindex + skips analytics on *.sevalla.page
              previews), GTM-P2C557NR (same container as the WordPress site, fires GA4).
  staging     noindex everywhere, robots.txt blocks crawlers, no analytics.

The build finishes by running tools/check.py and tools/test_redirects.py; any failure
exits non-zero, so CI never deploys a broken site.
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DIST = Path(os.environ.get("DIST_DIR", ROOT / "dist"))
PROD_HOST = "ascendpoint.agency"
GTM_ID = "GTM-P2C557NR"  # same container the WordPress site used (fires GA4 G-BGK6QEZTTH)

ORG = {
    "name": "AscendPoint Agency",
    "alternateName": ["AscendPoint", "SERP Agency"],
    "description": "AscendPoint builds and operates specialty marketing brands, including SERP Dental and "
                   "Medical Marketing Whiz, that help local healthcare practices acquire, retain and grow patients.",
    "telephone": "+1-213-513-7225",
    "address": {"@type": "PostalAddress", "streetAddress": "222 Purchase St #173", "addressLocality": "Rye",
                "addressRegion": "NY", "postalCode": "10580", "addressCountry": "US"},
    "sameAs": ["https://www.linkedin.com/company/107725025/", "https://www.facebook.com/ascendpointagency"],
    "founder": [{"@type": "Person", "name": "Patrick Hounsell", "jobTitle": "Co-CEO"},
                {"@type": "Person", "name": "Sonia Hounsell", "jobTitle": "Co-CEO"}],
    "subOrganization": [{"@type": "Organization", "name": "SERP Dental", "url": "https://serpdental.com/"},
                        {"@type": "Organization", "name": "Medical Marketing Whiz", "url": "https://medicalmarketingwhiz.com/"}],
    "knowsAbout": ["Healthcare marketing", "Dental marketing", "Medical spa marketing", "Local SEO",
                   "AI search visibility", "Google Ads", "Healthcare website design"],
}
SECTIONS = {  # URL prefix -> (breadcrumb name, URL)
    "/capabilities/": ("Capabilities", "/capabilities/"),
    "/news/": ("News", "/news/"),
}
WEBPAGE_TYPES = {"about": "AboutPage", "contact": "ContactPage", "collection": "CollectionPage"}
DEFAULT_IMAGE = "/img/og/index.jpg"


# ---------------------------------------------------------------- pages
class Page:
    def __init__(self, src: Path):
        self.src = src
        raw = src.read_text(encoding="utf-8")
        m = re.match(r"\s*<!--\n(.*?)\n-->\n?", raw, re.S)
        if not m:
            raise SystemExit(f"{src}: missing front-matter comment at top of file")
        self.fm = {}
        for line in m.group(1).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            k, _, v = line.partition(":")
            self.fm[k.strip()] = v.strip()
        self.content = raw[m.end():].strip("\n")
        rel = src.relative_to(SITE / "pages").with_suffix("").as_posix()
        if rel == "404":
            self.url = "/404.html"
        elif rel == "index":
            self.url = "/"
        else:
            self.url = "/" + (rel[:-6] if rel.endswith("/index") else rel) + "/"
        self.type = self.fm.get("type", "page")
        self.noindex = self.fm.get("noindex", "").lower() == "true" or self.type == "404"

    def get(self, k, d=""):
        return self.fm.get(k, d)

    @property
    def out(self) -> Path:
        if self.url == "/404.html":
            return DIST / "404.html"
        rel = self.url.strip("/")
        return (DIST / rel / "index.html") if rel else (DIST / "index.html")

    @property
    def date(self):
        return self.get("date")


def git_date(path: Path) -> str:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cs", "--", str(path)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out or dt.date.today().isoformat()
    except Exception:
        return dt.date.today().isoformat()


def human_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}"


# ---------------------------------------------------------------- listings
def news_list(pages):
    items = sorted((p for p in pages if p.type == "news"), key=lambda p: p.date, reverse=True)
    out = []
    for p in items:
        out.append(
            f'<article class="news-item has-media" id="{p.get("list_anchor")}"><a class="news-thumb" href="{p.url}" aria-hidden="true" tabindex="-1">'
            f'<img src="{p.get("list_image")}" alt="{p.get("list_image_alt")}" loading="lazy" decoding="async"></a><div class="copy">'
            f'<time>{p.get("list_date") or human_date(p.date)}</time><h2><a href="{p.url}">{p.get("list_title") or p.get("name")}</a></h2>'
            f'{p.get("list_summary")}<a class="arrow" href="{p.url}">Read the full story →</a></div></article>')
    return "".join(out)


def blog_list(pages):
    items = sorted((p for p in pages if p.type == "post"), key=lambda p: p.date, reverse=True)
    out = []
    for p in items:
        d = dt.date.fromisoformat(p.date)
        when = p.get("list_date") or f"{d:%b} {d.day}, {d.year} · {p.get('author')}"
        out.append(
            f'<a class="card post" href="{p.url}"><div class="thumb"><img src="{p.get("list_image")}" alt="{p.get("list_image_alt")}" '
            f'loading="lazy" decoding="async"></div><time>{when}</time><h3 class="h3" style="font-size:22px">{p.get("list_title") or p.get("name")}</h3>'
            f'<p class="small body grow">{p.get("list_summary")}</p><span class="arrow">Read article →</span></a>')
    return "".join(out)


# ---------------------------------------------------------------- structured data
def jsonld(p: Page, base: str, image: str, modified: str) -> str:
    org_id, site_id = f"{base}/#organization", f"{base}/#website"
    url = base + p.url
    graph = [
        {"@type": "Organization", "@id": org_id, "url": base + "/",
         "logo": {"@type": "ImageObject", "@id": f"{base}/#logo", "url": f"{base}/img/ap-logo-color.png",
                  "width": 600, "height": 154, "caption": ORG["name"]},
         "image": {"@id": f"{base}/#logo"}, **ORG},
        {"@type": "WebSite", "@id": site_id, "url": base + "/", "name": ORG["name"],
         "description": "The growth platform for local healthcare", "publisher": {"@id": org_id}, "inLanguage": "en-US"},
    ]
    crumbs = [("Home", "/")]
    for prefix, (n, u) in SECTIONS.items():
        if p.url.startswith(prefix) and p.url != u:
            crumbs.append((n, u))
    if p.type == "post":
        crumbs.append(("Insights", "/blog/"))
    if p.url != "/":
        crumbs.append((p.get("name") or p.get("title"), p.url))
    webpage = {"@type": WEBPAGE_TYPES.get(p.type, "WebPage"), "@id": url + "#webpage", "url": url,
               "name": p.get("title"), "description": p.get("description"), "isPartOf": {"@id": site_id},
               "about": {"@id": org_id}, "inLanguage": "en-US", "dateModified": modified,
               "primaryImageOfPage": {"@type": "ImageObject", "url": image}}
    if len(crumbs) > 1:
        webpage["breadcrumb"] = {"@id": url + "#breadcrumb"}
        graph.append({"@type": "BreadcrumbList", "@id": url + "#breadcrumb", "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": html.unescape(n), "item": base + u}
            for i, (n, u) in enumerate(crumbs)]})
    graph.append(webpage)
    if p.type == "service":
        graph.append({"@type": "Service", "@id": url + "#service", "name": html.unescape(p.get("name")),
                      "serviceType": html.unescape(p.get("name")), "description": p.get("description"),
                      "url": url, "image": image, "provider": {"@id": org_id},
                      "areaServed": {"@type": "Country", "name": "United States"},
                      "audience": {"@type": "BusinessAudience", "audienceType": "Healthcare practices"}})
    if p.type in ("news", "post"):
        author = ({"@id": org_id} if p.get("author") in ("", ORG["name"])
                  else {"@type": "Person", "name": p.get("author"), "worksFor": {"@id": org_id}})
        graph.append({"@type": "NewsArticle" if p.type == "news" else "BlogPosting", "@id": url + "#article",
                      "headline": html.unescape(p.get("name"))[:110], "description": p.get("description"),
                      "image": [image], "datePublished": p.date, "dateModified": max(p.date, modified),
                      "author": author, "publisher": {"@id": org_id}, "mainEntityOfPage": {"@id": url + "#webpage"},
                      "isPartOf": {"@id": site_id}, "inLanguage": "en-US"})
    data = json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, separators=(",", ":"))
    return f'<script type="application/ld+json">{data}</script>\n'


# ---------------------------------------------------------------- html helpers
_img_cache = {}


def img_size(path: Path):
    if path not in _img_cache:
        try:
            from PIL import Image
            with Image.open(path) as im:
                _img_cache[path] = im.size
        except Exception:
            _img_cache[path] = None
    return _img_cache[path]


def add_img_dims(s: str) -> str:
    def fix(m):
        tag = m.group(0)
        if " width=" in tag and " height=" in tag:
            return tag
        src = re.search(r'\ssrc="(/[^"]+)"', tag)
        if not src:
            return tag
        size = img_size(SITE / src.group(1).lstrip("/"))
        if not size:
            return tag
        return tag[:-1].rstrip("/").rstrip() + f' width="{size[0]}" height="{size[1]}">'
    return re.sub(r"<img\b[^>]*>", fix, s)


def mark_nav(header: str, url: str, section: str = "") -> str:
    def cur(m):
        href = m.group(1)
        if href == url:
            return m.group(0).replace("<a ", '<a aria-current="page" ', 1)
        return m.group(0)
    header = re.sub(r'<a (?:class="[^"]*" )?href="([^"#]*)"', cur, header)
    for prefix, label in (("/capabilities/", "Capabilities"), ("/brands/", "Brands")):
        if url.startswith(prefix):
            header = header.replace(f'<div class="dd"><button class="dd-btn" type="button" aria-haspopup="true">{label}',
                                    f'<div class="dd current"><button class="dd-btn" type="button" aria-haspopup="true">{label}')
    for prefix, href in (("/news/", "/news/"),):
        if url.startswith(prefix) and url != href:
            section = href
    if section and section != url:
        header = header.replace(f'<a href="{section}">', f'<a aria-current="true" href="{section}">', 1)
    return header


def fingerprint(rel: str) -> str:
    src = SITE / rel
    h = hashlib.sha256(src.read_bytes()).hexdigest()[:10]
    out = Path(rel).with_name(f"{Path(rel).stem}.{h}{Path(rel).suffix}")
    (DIST / out).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, DIST / out)
    (DIST / rel).unlink(missing_ok=True)
    return "/" + out.as_posix()


def analytics(prod: bool):
    if not prod:
        return '<meta name="robots" content="noindex, nofollow">\n', ""
    head = ("<script>(function(w,d,h){if(!/(^|\\.)" + PROD_HOST.replace(".", "\\.") + "$/.test(h)){"
            "var m=d.createElement('meta');m.name='robots';m.content='noindex, nofollow';d.head.appendChild(m);return;}"
            "w.dataLayer=w.dataLayer||[];w.dataLayer.push({'gtm.start':new Date().getTime(),event:'gtm.js'});"
            "var f=d.getElementsByTagName('script')[0],j=d.createElement('script');j.async=true;"
            f"j.src='https://www.googletagmanager.com/gtm.js?id={GTM_ID}';f.parentNode.insertBefore(j,f);"
            "})(window,document,location.hostname);</script>\n")
    body = (f'<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={GTM_ID}" height="0" width="0" '
            'style="display:none;visibility:hidden" title="Google Tag Manager"></iframe></noscript>\n')
    return head, body


# ---------------------------------------------------------------- generated files
def write_sitemap(pages, base, mods):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in sorted((p for p in pages if not p.noindex), key=lambda p: (p.url != "/", p.url)):
        lines.append(f"  <url><loc>{base}{p.url}</loc><lastmod>{mods[p.url]}</lastmod></url>")
    lines.append("</urlset>")
    (DIST / "sitemap.xml").write_text("\n".join(lines) + "\n")


def write_feed(pages, base):
    items = sorted((p for p in pages if p.type in ("news", "post")), key=lambda p: p.date, reverse=True)
    now = format_datetime(dt.datetime.now(dt.timezone.utc))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>',
           "<title>AscendPoint Agency: News &amp; Insights</title>", f"<link>{base}/</link>",
           f'<atom:link href="{base}/feed.xml" rel="self" type="application/rss+xml"/>',
           "<description>News, launches and practice-growth insights from AscendPoint Agency.</description>",
           "<language>en-us</language>", f"<lastBuildDate>{now}</lastBuildDate>"]
    for p in items:
        d = dt.datetime.fromisoformat(p.date).replace(hour=12, tzinfo=dt.timezone.utc)
        out.append("<item>"
                   f"<title>{html.escape(html.unescape(p.get('name')))}</title><link>{base}{p.url}</link>"
                   f'<guid isPermaLink="true">{base}{p.url}</guid><pubDate>{format_datetime(d)}</pubDate>'
                   f"<category>{'News' if p.type == 'news' else 'Insights'}</category>"
                   f"<description>{html.escape(p.get('description'))}</description></item>")
    out.append("</channel></rss>")
    (DIST / "feed.xml").write_text("\n".join(out) + "\n")


def write_llms(pages, base):
    def row(p):
        return f"- [{html.unescape(p.get('name') or p.get('title'))}]({base}{p.url}): {p.get('description')}"
    idx = [p for p in pages if not p.noindex]
    by = lambda t: sorted((p for p in idx if p.type == t), key=lambda p: p.url)
    core = [p for p in idx if p.type in ("home", "about", "contact", "collection", "page")]
    txt = [f"# {ORG['name']}", "",
           f"> {ORG['description']} Headquarters: 222 Purchase St #173, Rye, NY 10580. Phone: (213) 513-7225.", "",
           "AscendPoint (formerly SERP Agency) is a healthcare marketing platform. Its operating brands are "
           "SERP Dental (https://serpdental.com/, marketing for general, cosmetic, implant, orthodontic and oral surgery "
           "practices) and Medical Marketing Whiz (https://medicalmarketingwhiz.com/, marketing for med spas, plastic "
           "surgery, functional medicine, OB/GYN, weight loss and hormone therapy practices). Co-CEOs: Patrick Hounsell "
           "and Sonia Hounsell.", "",
           "## Company", *[row(p) for p in sorted(core, key=lambda p: p.url)], "",
           "## Capabilities", *[row(p) for p in by("service")], "",
           "## News", *[row(p) for p in sorted(by("news"), key=lambda p: p.date, reverse=True)], "",
           "## Insights", *[row(p) for p in sorted(by("post"), key=lambda p: p.date, reverse=True)], ""]
    (DIST / "llms.txt").write_text("\n".join(txt))


def write_redirects():
    src = (SITE / "_redirects").read_text().splitlines()
    out, seen = [], set()
    for line in src:
        s = line.strip()
        if not s or s.startswith("#"):
            out.append(line)
            continue
        parts = s.split()
        frm = parts[0]
        variants = [frm]
        if "*" not in frm and not frm.startswith("http") and "." not in frm.rsplit("/", 1)[-1]:
            twin = frm[:-1] if frm.endswith("/") and frm != "/" else frm + "/"
            variants.append(twin)
        for v in variants:
            if v in seen:
                continue
            seen.add(v)
            out.append("  ".join([v] + parts[1:]))
    (DIST / "_redirects").write_text("\n".join(out) + "\n")


HEADERS = """# HTTP headers for Kinsta (Sevalla) static hosting. Generated by tools/build.py.
/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  X-Frame-Options: SAMEORIGIN
  Permissions-Policy: camera=(), microphone=(), geolocation=(), browsing-topics=()
  Strict-Transport-Security: max-age=31536000

/assets/*
  Cache-Control: public, max-age=31536000, immutable

/img/*
  Cache-Control: public, max-age=2592000

/feed.xml
  Content-Type: application/rss+xml; charset=utf-8

/llms.txt
  Content-Type: text/plain; charset=utf-8
"""


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["staging", "production"], default="production")
    ap.add_argument("--base-url", default=f"https://{PROD_HOST}")
    ap.add_argument("--no-check", action="store_true")
    a = ap.parse_args()
    base, prod = a.base_url.rstrip("/"), a.env == "production"
    year = str(dt.date.today().year)

    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(SITE, DIST, ignore=shutil.ignore_patterns(".DS_Store", "_*", "pages", ".*"))
    css, js = fingerprint("assets/css/site.css"), fingerprint("assets/js/site.js")

    layout = (SITE / "_layout/base.html").read_text()
    parts = {n: (SITE / "_layout" / f"{n}.html").read_text().strip()
             for n in ("header", "footer", "header-landing", "footer-landing")}
    partials = {p.stem: p.read_text().strip() for p in (SITE / "_partials").glob("*.html")}
    pages = [Page(f) for f in sorted((SITE / "pages").rglob("*.html"))]
    urls = [p.url for p in pages]
    dupes = {u for u in urls if urls.count(u) > 1}
    if dupes:
        raise SystemExit(f"two pages map to the same URL: {dupes}")
    a_head, a_body = analytics(prod)
    mods = {}

    for p in pages:
        modified = p.get("updated") or git_date(p.src)
        mods[p.url] = max(modified, p.date) if p.date else modified
        img_rel = p.get("image") or DEFAULT_IMAGE
        img_abs = base + img_rel
        size = img_size(SITE / img_rel.lstrip("/")) or (1200, 630)
        content = p.content
        content = re.sub(r"\{\{>\s*([\w-]+)\s*\}\}", lambda m: partials[m.group(1)], content)
        content = content.replace("{{news_list}}", news_list(pages)).replace("{{blog_list}}", blog_list(pages))
        landing = p.get("layout") == "landing"
        header = parts["header-landing"] if landing else mark_nav(parts["header"], p.url, "/blog/" if p.type == "post" else "")
        footer = parts["footer-landing"] if landing else parts["footer"]
        article_meta = ""
        if p.type in ("news", "post"):
            article_meta = (f'<meta property="article:published_time" content="{p.date}">\n'
                            f'<meta property="article:modified_time" content="{mods[p.url]}">\n')
        robots = ""
        if p.noindex:
            robots = '<meta name="robots" content="noindex, follow">\n'
        elif prod:
            robots = '<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1">\n'
        tokens = {
            "title": html.escape(html.unescape(p.get("title")), quote=False),
            "description": html.escape(html.unescape(p.get("description")), quote=True),
            "og_title": html.escape(html.unescape(p.get("title")), quote=True),
            "og_type": "article" if p.type in ("news", "post") else "website",
            "canonical": base + p.url, "image": img_abs, "image_w": str(size[0]), "image_h": str(size[1]),
            "article_meta": article_meta, "robots": robots, "base": base, "css": css, "js": js,
            "analytics_head": a_head,
            "analytics_body": a_body, "jsonld": "" if p.type == "404" else jsonld(p, base, img_abs, mods[p.url]),
            "head_extra": "", "header": header, "footer": footer, "content": content,
        }
        if not prod:  # staging: static noindex replaces the per-page robots tag
            tokens["robots"] = ""
        s = re.sub(r"\{\{(\w+)\}\}", lambda m: tokens.get(m.group(1), m.group(0)), layout)
        s = s.replace("{{year}}", year)
        s = add_img_dims(s)
        p.out.parent.mkdir(parents=True, exist_ok=True)
        p.out.write_text(s, encoding="utf-8")

    write_sitemap(pages, base, mods)
    write_feed(pages, base)
    write_llms(pages, base)
    write_redirects()
    (DIST / "_headers").write_text(HEADERS)
    if prod:
        (DIST / "robots.txt").write_text(
            "# ascendpoint.agency: every crawler welcome, including AI assistants (GPTBot, ClaudeBot,\n"
            "# PerplexityBot, Google-Extended). Being citable by AI is part of what we sell.\n"
            f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n")
    else:
        (DIST / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    (DIST / ".nojekyll").write_text("")
    sha = os.environ.get("GITHUB_SHA") or subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                                          text=True).stdout.strip() or "local"
    (DIST / "version.txt").write_text(f"{sha} {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}\n")
    print(f"built {len(pages)} pages -> {DIST} ({a.env}, {base})")

    if a.no_check:
        return 0
    rc = subprocess.run([sys.executable, str(ROOT / "tools/check.py"), "--env", a.env, "--dist", str(DIST),
                         "--base-url", base]).returncode
    rc |= subprocess.run([sys.executable, str(ROOT / "tools/test_redirects.py"), "--dist", str(DIST)]).returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
