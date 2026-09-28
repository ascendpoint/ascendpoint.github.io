#!/usr/bin/env python3
"""One-time (idempotent) pass that turns Patrick's design prototype into production pages.

What it does to every page in site/:
  * meta description, Open Graph + Twitter tags, favicon/touch icon, theme colour
  * an <!-- ap:env --> marker where tools/build.py injects canonical/robots/analytics per environment
  * JSON-LD (Organization on home, NewsArticle on news, Article on insight articles)
  * per-page 1200x630 share images in site/img/og/ (cover-cropped from the page hero)
  * contact.html: prototype form -> live Typeform embed (the same form the current site uses)
  * copy fixes found in review

Safe to re-run: every step checks whether it has already been applied.
Usage:  python3 tools/productionize.py
"""
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
OG_DIR = SITE / "img" / "og"
BRAND = "AscendPoint Agency"
NAVY = (0, 50, 81)

# Hand-written descriptions where the page has no usable lead paragraph (<=160 chars).
DESCRIPTIONS = {
    "index.html": "AscendPoint builds and operates specialty marketing brands, including SERP Dental and Medical Marketing Whiz, that help local healthcare practices grow.",
    "about.html": "AscendPoint is the healthcare growth platform behind SERP Dental and Medical Marketing Whiz. Meet the leadership team and see how we got here.",
    "contact.html": "Talk to AscendPoint. Tell us about your practice and the right person on our team will reach out within one business day.",
    "results.html": "Real practices, real numbers. See the new-patient and revenue results AscendPoint brands deliver for dental offices, med spas and specialty practices.",
    "news.html": "Acquisitions, launches and milestones from AscendPoint Agency and its brands, SERP Dental and Medical Marketing Whiz.",
    "insights.html": "Research, guides and field notes from the AscendPoint team on growing a healthcare practice in the AI era.",
    "ai-powered-business-solutions.html": "The AI tools reshaping the business side of dentistry in 2026, from practice analytics to AI receptionists, and how growth-focused practices use them.",
    "how-to-show-up-in-ai.html": "How ChatGPT, Gemini and Google AI Overviews choose which businesses to recommend, and the steps that get your practice into the answer.",
    "marketing-for-dentists.html": "A practical guide to dental marketing today: SEO, reviews, ads, websites and patient retention for practices that want steady growth.",
    "what-is-local-seo.html": "What local SEO is, how Google ranks businesses in Maps and local results, and the steps that help your practice dominate your market.",
    "brands.html": "SERP Dental and Medical Marketing Whiz: specialist healthcare marketing brands that keep their names and cultures and run on one AscendPoint platform.",
}

ARTICLES = {"ai-powered-business-solutions.html", "how-to-show-up-in-ai.html",
            "marketing-for-dentists.html", "what-is-local-seo.html"}

TYPEFORM_FORM_ID = "Bs7qxsde"  # "AscendPoint Website – Contact (Founders, Partners & Investors)"; routes dental -> SERP, med spa -> MMW; emails kyle@ascendpoint.agency

COPY_FIXES = [
    # (file glob, old, new)
    ("*", "August 2025, 2025", "August 2025"),
]


def text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def clip(s: str, n: int = 160) -> str:
    if len(s) <= n:
        return s
    cut = s[: n - 1].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"


def first_para_after_h1(s: str) -> str:
    i = s.find("</h1>")
    for p in re.findall(r"<p[^>]*>(.*?)</p>", s[i:], re.S):
        t = text(p)
        if len(t) > 60:
            return t
    return ""


def description_for(name: str, s: str) -> str:
    if name in DESCRIPTIONS:
        return DESCRIPTIONS[name]
    m = re.search(r'<p class="lead[^"]*"[^>]*>(.*?)</p>', s, re.S)
    lead = text(m.group(1)) if m else ""
    if name.startswith("news-") or len(lead) < 80:
        lead = first_para_after_h1(s)
        lead = re.sub(r"^[A-Z][a-z]+ \d{1,2}, \d{4} — |^[A-Z][a-z]+ \d{4} — ", "", lead)
    return clip(lead)


def hero_image(name: str, s: str):
    """Best image to use for the share card: first content image that isn't a logo."""
    for src in re.findall(r'<img[^>]+src="(img/[^"]+)"', s):
        if "logo" in src or src.startswith("img/og/") or src.startswith("img/hs-"):
            continue
        return src
    return None


def make_og(name: str, src, title: str) -> str:
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    OG_DIR.mkdir(parents=True, exist_ok=True)
    out = OG_DIR / (name.replace(".html", "") + ".jpg")
    if src and (SITE / src).exists():
        im = Image.open(SITE / src).convert("RGB")
        im = ImageOps.fit(im, (1200, 630), Image.LANCZOS, centering=(0.5, 0.45))
    else:
        # Branded fallback: navy card, logo, page title.
        im = Image.new("RGB", (1200, 630), NAVY)
        d = ImageDraw.Draw(im)
        logo = Image.open(SITE / "img" / "ap-logo.png").convert("RGBA")
        logo.thumbnail((420, 110))
        im.paste(logo, (80, 80), logo)
        d.rectangle([80, 250, 200, 256], fill=(62, 199, 227))
        font = None
        for cand in ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]:
            if Path(cand).exists():
                font = ImageFont.truetype(cand, 56)
                break
        font = font or ImageFont.load_default()
        words, lines, line = title.split(), [], ""
        for w in words:
            t = (line + " " + w).strip()
            if d.textlength(t, font=font) > 1040:
                lines.append(line)
                line = w
            else:
                line = t
        lines.append(line)
        y = 290
        for ln in lines[:3]:
            d.text((80, y), ln, font=font, fill=(255, 255, 255))
            y += 72
    im.save(out, "JPEG", quality=85, optimize=True, progressive=True)
    return f"img/og/{out.name}"


def jsonld(name: str, s: str, title: str, desc: str, og: str) -> str:
    if name == "index.html":
        data = {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": BRAND,
            "alternateName": ["AscendPoint", "SERP Agency"],
            "url": "{{BASE_URL}}/",
            "logo": "{{BASE_URL}}/img/ap-logo.png",
            "description": desc,
            "address": {"@type": "PostalAddress", "streetAddress": "222 Purchase St #173",
                        "addressLocality": "Rye", "addressRegion": "NY", "postalCode": "10580",
                        "addressCountry": "US"},
            "telephone": "+1-213-513-7225",
            "subOrganization": [
                {"@type": "Organization", "name": "SERP Dental", "url": "https://serpdental.com/"},
                {"@type": "Organization", "name": "Medical Marketing Whiz", "url": "https://medicalmarketingwhiz.com/"},
            ],
        }
    elif name.startswith("news-") or name in ARTICLES:
        m = re.search(r'datetime="([0-9-]+)"', s)
        data = {
            "@context": "https://schema.org",
            "@type": "NewsArticle" if name.startswith("news-") else "Article",
            "headline": title[:110],
            "description": desc,
            "image": ["{{BASE_URL}}/" + og],
            "publisher": {"@type": "Organization", "name": BRAND,
                          "logo": {"@type": "ImageObject", "url": "{{BASE_URL}}/img/ap-logo.png"}},
            "mainEntityOfPage": "{{PAGE_URL}}",
        }
        if m:
            data["datePublished"] = m.group(1)
    else:
        return ""
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>\n"


def contact_embed(s: str) -> str:
    if "data-tf-widget" in s or "data-tf-live" in s:
        return s
    start = s.find('<form class="form" id="contact-form"')
    end_thanks = s.find('<div class="thanks" id="thanks"')
    if start == -1 or end_thanks == -1:
        raise SystemExit("contact.html: could not find the prototype form")
    # end of the thanks block = the </div> that closes it (thanks div contains nested divs; find the button then its closing div)
    btn = s.find('id="again"', end_thanks)
    close = s.find("</div>", s.find("</button>", btn)) + len("</div>")
    embed = (
        '<div class="tf-wrap" style="border-radius:14px;overflow:hidden;border:1px solid var(--border);background:#fff">\n'
        f'<div data-tf-widget="{TYPEFORM_FORM_ID}" data-tf-opacity="100" data-tf-inline-on-mobile data-tf-medium="snippet" '
        'data-tf-transitive-search-params="utm_source,utm_medium,utm_campaign,utm_content,utm_term" '
        'data-tf-iframe-props="title=Contact AscendPoint" style="width:100%;height:600px"></div>\n'
        "</div>\n"
        '<p class="body small muted" style="margin-top:14px">Prefer to talk? Call '
        '<a href="tel:+12135137225">(213) 513-7225</a>.</p>\n'
        '<script>(function(){var w=document.querySelector("[data-tf-widget]");if(w)w.setAttribute("data-tf-hidden","landing_page="+encodeURIComponent(location.pathname));})();</script>\n'
        '<script src="https://embed.typeform.com/next/embed.js" async></script>'
    )
    return s[:start] + embed + s[close:]


def process(path: Path):
    name = path.name
    s = path.read_text(encoding="utf-8")
    orig = s

    for fname, old, new in COPY_FIXES:
        if fname == "*" or name == fname:
            s = s.replace(old, new)

    if name == "contact.html":
        s = contact_embed(s)

    if "<!-- ap:env -->" not in s:
        existing = re.search(r'<meta name="description" content="([^"]*)">', s)
        title_full = html.unescape(re.search(r"<title>(.*?)</title>", s, re.S).group(1)).strip()
        title = re.sub(r"\s*·\s*AscendPoint$", "", title_full)
        if name == "index.html":
            title = "AscendPoint · The growth platform for local healthcare"
        desc = html.unescape(existing.group(1)) if existing else description_for(name, s)
        og = make_og(name, hero_image(name, s), title)
        otype = "article" if (name.startswith("news-") or name in ARTICLES) else "website"
        e = html.escape
        head = (
            ("" if existing else f'\n<meta name="description" content="{e(desc)}">') + "\n"

            f'<meta property="og:site_name" content="{BRAND}">\n'
            f'<meta property="og:type" content="{otype}">\n'
            f'<meta property="og:title" content="{e(title_full)}">\n'
            f'<meta property="og:description" content="{e(desc)}">\n'
            f'<meta property="og:image" content="{{{{BASE_URL}}}}/{og}">\n'
            '<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">\n'
            '<meta name="twitter:card" content="summary_large_image">\n'
            '<link rel="icon" type="image/png" sizes="32x32" href="favicon-32.png">\n'
            '<link rel="icon" type="image/png" sizes="192x192" href="favicon-192.png">\n'
            '<link rel="apple-touch-icon" href="apple-touch-icon.png">\n'
            '<meta name="theme-color" content="#003251">\n'
            "<!-- ap:env -->\n"
            + jsonld(name, s, title, desc, og)
        )
        s = re.sub(r"(</title>)", r"\1" + head.replace("\\", "\\\\"), s, count=1)

    if s != orig:
        path.write_text(s, encoding="utf-8")
        print("updated", name)
    else:
        print("ok     ", name)


def icons():
    from PIL import Image

    src = SITE / "img" / "brand" / "logomark-cyan.png"
    if not src.exists():
        print("skip icons: site/img/brand/logomark-cyan.png missing")
        return
    mark = Image.open(src).convert("RGBA")
    bbox = mark.getbbox()
    mark = mark.crop(bbox)
    for size, name in [(32, "favicon-32.png"), (192, "favicon-192.png")]:
        m = mark.copy()
        m.thumbnail((size, size), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(m, ((size - m.width) // 2, (size - m.height) // 2), m)
        canvas.save(SITE / name)
    # iOS: solid navy tile, mark at ~62%
    tile = Image.new("RGBA", (180, 180), NAVY + (255,))
    m = mark.copy()
    m.thumbnail((112, 112), Image.LANCZOS)
    tile.paste(m, ((180 - m.width) // 2, (180 - m.height) // 2), m)
    tile.convert("RGB").save(SITE / "apple-touch-icon.png")
    # favicon.ico for browsers that ask for /favicon.ico directly
    ico = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    m = mark.copy()
    m.thumbnail((64, 64), Image.LANCZOS)
    ico.paste(m, ((64 - m.width) // 2, (64 - m.height) // 2), m)
    ico.save(SITE / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    print("icons  favicon-32/192, apple-touch-icon, favicon.ico")


def make_404():
    """404 page built from the home page's header/footer so it always matches the site chrome."""
    out = SITE / "404.html"
    s = (SITE / "index.html").read_text(encoding="utf-8")
    s = re.sub(r"<title>.*?</title>", "<title>Page not found · AscendPoint</title>", s, count=1, flags=re.S)
    s = re.sub(r'<script type="application/ld\+json">.*?</script>\n?', "", s, flags=re.S)
    s = re.sub(r'<meta (name="description"|property="og:[^"]+"|name="twitter:card")[^>]*>\n?', "", s)
    s = s.replace("<head>", '<head><base href="/">', 1)  # 404 is served at any depth; keep relative links working
    main = (
        '<main id="main">\n<section class="sec"><div class="wrap stack" style="max-width:760px;text-align:left">\n'
        '<p class="kicker">Error 404</p>\n'
        '<h1 class="h1">We couldn\'t find that page.</h1>\n'
        '<p class="lead">It may have moved when we rebuilt the site. These are the pages most people are looking for:</p>\n'
        '<div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:8px">'
        '<a class="btn btn-dark" href="index.html">Home</a>'
        '<a class="btn btn-line" href="capabilities.html">Capabilities</a>'
        '<a class="btn btn-line" href="brands.html">Our Brands</a>'
        '<a class="btn btn-line" href="news.html">News</a>'
        '<a class="btn btn-line" href="contact.html">Contact</a></div>\n'
        "</div></section>\n</main>"
    )
    s2, n = re.subn(r"<main\b.*?</main>", main, s, count=1, flags=re.S)
    if n != 1:
        raise SystemExit("404: could not find <main> in index.html")
    out.write_text(s2, encoding="utf-8")
    print("wrote  404.html")


if __name__ == "__main__":
    icons()
    for p in sorted(SITE.glob("*.html")):
        if p.name == "404.html":
            continue
        process(p)
    make_404()
