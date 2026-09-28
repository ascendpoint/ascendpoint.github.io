#!/usr/bin/env python3
"""Build site/ -> dist/ for one environment, then verify the result.

  python3 tools/build.py --env staging    --base-url https://ascendpoint.github.io
  python3 tools/build.py --env production --base-url https://ascendpoint.agency

staging     noindex everywhere, robots.txt blocks crawlers, no analytics
production  canonical URLs, GTM/GA4 analytics, sitemap.xml, crawlable robots.txt

Both: clean URLs (about.html is linked as "about"), absolute share-image URLs,
_redirects kept for Cloudflare Pages. The build fails (exit 1) if any check in
tools/check.py fails, so a broken page can never be deployed.
"""
import argparse
import os
import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DIST = Path(os.environ.get("DIST_DIR", ROOT / "dist"))

GTM_ID = "GTM-P2C557NR"  # the container the current WordPress site already uses (fires GA4 G-BGK6QEZTTH)

GTM_HEAD = f"""<script>(function(w,d,s,l,i){{w[l]=w[l]||[];w[l].push({{'gtm.start':
new Date().getTime(),event:'gtm.js'}});var f=d.getElementsByTagName(s)[0],
j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=
'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);
}})(window,document,'script','dataLayer','{GTM_ID}');</script>"""
GTM_BODY = (f'<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={GTM_ID}" '
            'height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>')


def page_path(name: str) -> str:
    """URL path for a page file: index.html -> /, about.html -> /about"""
    return "/" if name == "index.html" else "/" + name[:-5]


def clean_links(s: str) -> str:
    # href="about.html#x" -> href="about#x";  href="index.html" -> href="./"
    s = re.sub(r'href="index\.html(#[^"]*)?"', lambda m: f'href="./{m.group(1) or ""}"', s)
    s = re.sub(r'href="([a-z0-9][a-z0-9-]*)\.html(#[^"]*)?"',
               lambda m: f'href="{m.group(1)}{m.group(2) or ""}"', s)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["staging", "production"], default="staging")
    ap.add_argument("--base-url", default="https://ascendpoint.github.io")
    ap.add_argument("--no-check", action="store_true")
    a = ap.parse_args()
    base = a.base_url.rstrip("/")
    prod = a.env == "production"

    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(SITE, DIST, ignore=shutil.ignore_patterns(".DS_Store", "brand"))

    pages = sorted(p for p in DIST.glob("*.html"))
    for p in pages:
        s = p.read_text(encoding="utf-8")
        url = base + page_path(p.name)
        env = [f'<link rel="canonical" href="{url}">', f'<meta property="og:url" content="{url}">']
        if prod:
            env.append(GTM_HEAD)
        else:
            env.append('<meta name="robots" content="noindex, nofollow">')
        if p.name == "404.html":
            env = ['<meta name="robots" content="noindex">']
        s = s.replace("<!-- ap:env -->", "\n".join(env))
        s = s.replace("{{BASE_URL}}", base).replace("{{PAGE_URL}}", url)
        if prod and GTM_BODY not in s:
            s = re.sub(r"(<body[^>]*>)", r"\1" + GTM_BODY, s, count=1)
        s = clean_links(s)
        p.write_text(s, encoding="utf-8")

    today = dt.date.today().isoformat()
    if prod:
        (DIST / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n")
    else:
        (DIST / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    urls = [p for p in pages if p.name != "404.html"]
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for p in urls:
        pr = "1.0" if p.name == "index.html" else ("0.6" if p.name.startswith("news-") else "0.8")
        sm.append(f"  <url><loc>{base}{page_path(p.name)}</loc><lastmod>{today}</lastmod><priority>{pr}</priority></url>")
    sm.append("</urlset>")
    (DIST / "sitemap.xml").write_text("\n".join(sm) + "\n")
    (DIST / ".nojekyll").write_text("")  # GitHub Pages: serve files as-is

    print(f"built {len(pages)} pages -> dist/ ({a.env}, {base})")
    if not a.no_check:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "check.py"), "--env", a.env, "--dist", str(DIST)])
        sys.exit(r.returncode)


if __name__ == "__main__":
    main()
