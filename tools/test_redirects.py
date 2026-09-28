#!/usr/bin/env python3
"""Prove the move from WordPress loses no URL.

Simulates the host's _redirects engine (first match wins, `*` splat -> :splat, exact
paths otherwise) plus static-file serving (/x/ -> x/index.html, /x -> 301 to /x/), then
checks that:
  1. every URL in tools/legacy_urls.txt (everything the old WordPress site exposed)
     ends on a real page in at most 2 hops, with no loops;
  2. every redirect rule's destination is a real page;
  3. no rule shadows a real page (a redirect on a URL that has its own page).
"""
import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
HOST = "ascendpoint.agency"


def load_rules(dist: Path):
    rules = []
    for line in (dist / "_redirects").read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        rules.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "301"))
    return rules


def match(rule_from: str, url: str, host: str):
    if rule_from.startswith("http"):
        u = urlparse(rule_from)
        if u.netloc != host:
            return None
        rule_from = u.path
    if rule_from.endswith("*"):
        prefix = rule_from[:-1]
        if url.startswith(prefix):
            return url[len(prefix):]
        return None
    return "" if url == rule_from else None


def serve(dist: Path, path: str):
    """What the static host does with no redirect rule: (status, location|file)."""
    if path.endswith("/"):
        f = dist / path.lstrip("/") / "index.html"
        return (200, f) if f.exists() else (404, None)
    f = dist / path.lstrip("/")
    if f.is_file():
        return 200, f
    if (dist / path.lstrip("/") / "index.html").exists():
        return 301, path + "/"  # pretty URLs: /about -> /about/
    return 404, None


def resolve(dist, rules, url, host=HOST, max_hops=3):
    hops, seen = [], set()
    cur = url
    while True:
        if (host, cur) in seen:
            return "LOOP", hops
        seen.add((host, cur))
        forced = False
        status, target = None, None
        st, f = serve(dist, cur)
        for frm, to, code in rules:
            sp = match(frm, cur, host)
            if sp is None:
                continue
            force = code.endswith("!")
            if st == 200 and not force:
                break  # real file wins over a non-forced rule
            status, target = int(code.rstrip("!")), to.replace(":splat", sp)
            break
        if status is None:
            if st == 200:
                return "OK", hops
            if st == 301:
                status, target = 301, f
            else:
                return "404", hops
        if target.startswith("http"):
            u = urlparse(target)
            host, target = u.netloc, u.path
        hops.append((status, target))
        if len(hops) > max_hops:
            return "TOO_MANY_HOPS", hops
        cur = target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    a = ap.parse_args()
    dist = Path(a.dist)
    rules = load_rules(dist)
    errors = []
    legacy = [l.strip() for l in (ROOT / "tools/legacy_urls.txt").read_text().splitlines()
              if l.strip() and not l.startswith("#")]
    for url in legacy:
        res, hops = resolve(dist, rules, url)
        if res != "OK" or len(hops) > 2:
            errors.append(f"legacy {url}: {res} via {hops}")
    for frm, to, code in rules:
        if frm.startswith("http"):
            continue
        if not frm.endswith("*"):
            st, _ = serve(dist, frm)
            if st == 200 and not code.endswith("!"):
                errors.append(f"rule {frm} is shadowed by a real page (remove it)")
        dest = to.replace(":splat", "")
        if dest.startswith("http"):
            continue
        if ":splat" in to:
            continue
        res, hops = resolve(dist, rules, dest)
        if res != "OK" or hops:
            errors.append(f"rule {frm} -> {to}: destination is not a page ({res} {hops})")
    # www -> apex
    res, hops = resolve(dist, rules, "/about/", host="www." + HOST)
    if res != "OK" or hops[:1] != [(301, "/about/")]:
        errors.append(f"www.{HOST}/about/ does not 301 to apex: {res} {hops}")
    for e in errors:
        print("ERROR", e)
    print(f"redirects: {len(legacy)} legacy URLs + {len(rules)} rules checked, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
