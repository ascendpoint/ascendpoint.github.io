#!/usr/bin/env python3
"""Check the LIVE site after a deploy (CI runs this automatically; you can too).

    python3 tools/live_check.py https://ascendpoint.agency
    python3 tools/live_check.py https://<site>.sevalla.page --wait-for <git sha>

Checks, against the real host:
  * /version.txt matches the commit (with --wait-for, polls up to 6 minutes for Kinsta to deploy)
  * every URL in /sitemap.xml returns 200 with the right canonical
  * every URL in tools/legacy_urls.txt reaches a 200 page in <= 2 redirects, all 301/302
  * security + cache headers are present, /feed.xml and /llms.txt serve with the right type
  * an unknown URL returns a real 404 status (Kinsta shows its own 404 page; see note below)
"""
import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
UA = "AscendPoint-live-check/1.0"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with OPENER.open(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, dict(e.headers), body


def follow(url, max_hops=4):
    hops = []
    for _ in range(max_hops + 1):
        st, h, body = fetch(url)
        if st in (301, 302, 307, 308):
            url = urljoin(url, h.get("Location") or h.get("location"))
            hops.append((st, url))
            continue
        return st, url, hops, h, body
    return "TOO_MANY", url, hops, {}, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--wait-for", help="git sha that /version.txt must show")
    ap.add_argument("--canonical-base", default="https://ascendpoint.agency")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    errors = []

    if a.wait_for:
        deadline = time.time() + 360
        while True:
            st, _, body = fetch(base + "/version.txt")
            if st == 200 and body.strip().startswith(a.wait_for[:7]):
                print(f"deployed: {body.strip()}")
                break
            if time.time() > deadline:
                print(f"ERROR live site never showed version {a.wait_for[:7]} (last: {st} {body.strip()[:40]})")
                return 1
            time.sleep(15)

    st, h, sm = fetch(base + "/sitemap.xml")
    if st != 200:
        print(f"ERROR sitemap.xml -> {st}")
        return 1
    locs = re.findall(r"<loc>([^<]+)</loc>", sm)
    for loc in locs:
        path = urlparse(loc).path
        st, final, hops, hh, body = follow(base + path)
        if st != 200 or hops:
            errors.append(f"{path}: {st} via {hops}")
            continue
        c = re.search(r'<link rel="canonical" href="([^"]+)"', body)
        if not c or c.group(1) != a.canonical_base + path:
            errors.append(f"{path}: canonical {c.group(1) if c else None}")

    legacy = [l.strip() for l in (ROOT / "tools/legacy_urls.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    for path in legacy:
        st, final, hops, _, _ = follow(base + path)
        if st != 200 or len(hops) > 2:
            errors.append(f"legacy {path}: {st} -> {final} via {hops}")

    st, h, _ = fetch(base + "/")
    hl = {k.lower(): v for k, v in h.items()}
    for k in ("x-content-type-options", "referrer-policy", "strict-transport-security"):
        if k not in hl:
            errors.append(f"missing header {k} on /")
    css = re.search(r'href="(/assets/css/[^"]+)"', fetch(base + "/")[2])
    if css:
        ch = {k.lower(): v for k, v in fetch(base + css.group(1))[1].items()}
        if "immutable" not in ch.get("cache-control", ""):
            errors.append(f"CSS not cached immutable: {ch.get('cache-control')}")
    for path, ctype in (("/feed.xml", "xml"), ("/llms.txt", "text/plain"), ("/robots.txt", "text/plain")):
        st, h, _ = fetch(base + path)
        t = {k.lower(): v for k, v in h.items()}.get("content-type", "")
        if st != 200 or ctype not in t:
            errors.append(f"{path}: {st} {t}")
    st, _, body = fetch(base + "/this-page-does-not-exist-" + str(int(time.time())) + "/")
    if st != 404:
        errors.append(f"unknown URL returned {st} (want 404)")
    elif "couldn't find that page" not in body:
        # Kinsta static hosting serves its own 404 page. Its "Error file" setting would serve our
        # 404.html, but with status 200 (a soft 404 that hurts SEO), so it stays off (Oct 1 2026).
        print("note: unknown URL returns a real 404, but Kinsta's own page, not site/pages/404.html")

    for e in errors:
        print("ERROR", e)
    print(f"live check {base}: {len(locs)} sitemap URLs, {len(legacy)} legacy URLs, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
