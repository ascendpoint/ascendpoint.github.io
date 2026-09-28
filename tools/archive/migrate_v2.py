#!/usr/bin/env python3
"""ONE-TIME migration (Sep 28 2026): flat prototype pages -> v2 source layout.

  site/*.html (full pages, flat URLs like /seo-ai-ranking)
      -> site/pages/<url path>.html   (front matter + page content only)
         site/_layout/{header,footer}.html, site/assets/{css,js}/...

URLs are chosen to match the live WordPress site exactly wherever a page already
exists there (/about/, /our-results/, /capabilities/healthcare-seo-ai-ranking/, the
four blog posts...), so the move to Kinsta loses no rankings. Kept in the repo as a
record of how the mapping was made; it is not part of the normal build.
"""
import html
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # tools/archive/ -> repo root
OLD = ROOT / "site"
NEW = ROOT / "site_v2"

# flat file -> new URL path (no leading/trailing slash; "" = home)
ROUTES = {
    "index.html": "",
    "about.html": "about",
    "contact.html": "contact",
    "results.html": "our-results",
    "brands.html": "brands",
    "capabilities.html": "capabilities",
    "seo-ai-ranking.html": "capabilities/healthcare-seo-ai-ranking",
    "ppc-ads.html": "capabilities/healthcare-ppc-ads",
    "social-media.html": "capabilities/healthcare-social-media-ads",
    "website-design.html": "capabilities/healthcare-website-design",
    "branding.html": "capabilities/healthcare-branding",
    "video-photography.html": "capabilities/healthcare-video-and-photography",
    "strategy.html": "capabilities/healthcare-strategy",
    "lead-tracking-analytics.html": "capabilities/healthcare-lead-tracking-analytics",
    "email-sms-marketing.html": "capabilities/healthcare-email-sms-marketing",
    "reputation-management.html": "capabilities/healthcare-reputation-management",
    "webinars-events.html": "capabilities/healthcare-webinars-events",
    "podcasting.html": "capabilities/healthcare-podcasting",
    "book-publishing.html": "capabilities/healthcare-book-publishing",
    "ai-conversion-tools.html": "capabilities/healthcare-ai-conversion-tools",
    "ascendvu-ai-visibility.html": "capabilities/ascendvu-ai-visibility",
    "insights.html": "blog",
    "ai-powered-business-solutions.html": "ai-powered-business-solutions",
    "marketing-for-dentists.html": "marketing-for-dentists-building-a-thriving-practice-in-the-digital-age",
    "how-to-show-up-in-ai.html": "how-to-show-up-in-ai-your-business-guide-to-ai-powered-search-visibility",
    "what-is-local-seo.html": "what-is-local-seo-your-complete-guide-to-dominating-your-local-market",
    "news.html": "news",
    "news-fletcher-road-capital-launch.html": "news/fletcher-road-capital-launch",
    "news-fletcher-road-capital-acquires-serp-agency.html": "news/fletcher-road-capital-acquires-serp-agency",
    "news-serp-dental-dental-focus.html": "news/serp-dental-dental-focus",
    "news-serp-agency-becomes-ascendpoint.html": "news/serp-agency-becomes-ascendpoint",
    "news-serp-dental-acquires-sparklyfe.html": "news/serp-dental-acquires-sparklyfe",
    "news-ascendvu-ai-product-suite-launch.html": "news/ascendvu-ai-product-suite-launch",
    "news-ascendpoint-acquires-medical-marketing-whiz.html": "news/ascendpoint-acquires-medical-marketing-whiz",
    "404.html": "404",
}

BRAND = "AscendPoint Agency"
# SEO titles (<= ~60 chars, keyword first). Everything else comes from the page.
TITLES = {
    "": "AscendPoint Agency | The Growth Platform for Local Healthcare",
    "about": "About AscendPoint Agency | Healthcare Marketing Leadership",
    "contact": "Contact AscendPoint Agency | Healthcare Marketing",
    "our-results": "Healthcare Marketing Results & Case Studies | AscendPoint",
    "brands": "Our Brands: SERP Dental & Medical Marketing Whiz | AscendPoint",
    "capabilities": "Healthcare Marketing Services & Capabilities | AscendPoint",
    "capabilities/healthcare-seo-ai-ranking": "Healthcare SEO & AI Search Ranking | AscendPoint Agency",
    "capabilities/healthcare-ppc-ads": "Healthcare PPC & Google Ads Management | AscendPoint Agency",
    "capabilities/healthcare-social-media-ads": "Healthcare Social Media Advertising | AscendPoint Agency",
    "capabilities/healthcare-website-design": "Healthcare Website Design That Books Patients | AscendPoint",
    "capabilities/healthcare-branding": "Healthcare Practice Branding | AscendPoint Agency",
    "capabilities/healthcare-video-and-photography": "Healthcare Video & Photography | AscendPoint Agency",
    "capabilities/healthcare-strategy": "Healthcare Marketing Strategy | AscendPoint Agency",
    "capabilities/healthcare-lead-tracking-analytics": "Healthcare Lead Tracking & Analytics | AscendPoint Agency",
    "capabilities/healthcare-email-sms-marketing": "Patient Email & SMS Marketing | AscendPoint Agency",
    "capabilities/healthcare-reputation-management": "Healthcare Reputation Management | AscendPoint Agency",
    "capabilities/healthcare-webinars-events": "Healthcare Webinars & Patient Events | AscendPoint Agency",
    "capabilities/healthcare-podcasting": "Healthcare Podcast Production | AscendPoint Agency",
    "capabilities/healthcare-book-publishing": "Book Publishing for Doctors & Dentists | AscendPoint Agency",
    "capabilities/healthcare-ai-conversion-tools": "AI Receptionist & Conversion Tools for Practices | AscendPoint",
    "capabilities/ascendvu-ai-visibility": "AscendVu: AI Visibility for Healthcare Practices | AscendPoint",
    "blog": "Healthcare Marketing Insights & Guides | AscendPoint Agency",
    "news": "News & Press Releases | AscendPoint Agency",
    "news/fletcher-road-capital-launch": "Patrick Hounsell Launches Fletcher Road Capital | AscendPoint",
    "news/serp-agency-becomes-ascendpoint": "SERP Agency Rebrands as AscendPoint Agency | AscendPoint News",
    "404": "Page Not Found | AscendPoint Agency",
}
# Short names used in breadcrumbs, menus and Service schema.
NAMES = {
    "": "Home", "about": "About", "contact": "Contact", "our-results": "Results", "brands": "Brands",
    "capabilities": "Capabilities", "blog": "Insights", "news": "News", "404": "Page not found",
    "capabilities/healthcare-seo-ai-ranking": "SEO + AI Ranking",
    "capabilities/healthcare-ppc-ads": "PPC Ads",
    "capabilities/healthcare-social-media-ads": "Social Media",
    "capabilities/healthcare-website-design": "Website Design",
    "capabilities/healthcare-branding": "Branding",
    "capabilities/healthcare-video-and-photography": "Video & Photography",
    "capabilities/healthcare-strategy": "Strategy",
    "capabilities/healthcare-lead-tracking-analytics": "Lead Tracking + Analytics",
    "capabilities/healthcare-email-sms-marketing": "Email & SMS Marketing",
    "capabilities/healthcare-reputation-management": "Reputation Management",
    "capabilities/healthcare-webinars-events": "Webinars & Events",
    "capabilities/healthcare-podcasting": "Podcasting",
    "capabilities/healthcare-book-publishing": "Book Publishing",
    "capabilities/healthcare-ai-conversion-tools": "AI Conversion Tools",
    "capabilities/ascendvu-ai-visibility": "AscendVu AI Visibility",
}
BLOG_POSTS = {
    "ai-powered-business-solutions": ("2026-03-02", "Sonia Hounsell"),
    "marketing-for-dentists-building-a-thriving-practice-in-the-digital-age": ("2025-05-15", "Kelsey Hagberg"),
    "how-to-show-up-in-ai-your-business-guide-to-ai-powered-search-visibility": ("2025-05-03", "Kelsey Hagberg"),
    "what-is-local-seo-your-complete-guide-to-dominating-your-local-market": ("2025-06-02", "Kelsey Hagberg"),
}

URLMAP = {k: ("/" + v + "/" if v else "/") for k, v in ROUTES.items()}
URLMAP["404.html"] = "/404.html"


def fix_links(s: str) -> str:
    def href(m):
        name, frag = m.group(1) + ".html", m.group(2) or ""
        if name in URLMAP:
            return f'href="{URLMAP[name]}{frag}"'
        return m.group(0)
    s = re.sub(r'href="([a-z0-9][a-z0-9-]*)\.html(#[^"]*)?"', href, s)
    s = re.sub(r'(src|href)="img/', r'\1="/img/', s)
    s = re.sub(r'srcset="img/', 'srcset="/img/', s)
    s = s.replace('href="favicon', 'href="/favicon').replace('href="apple-touch', 'href="/apple-touch')
    return s


def attr(s, pat):
    m = re.search(pat, s, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def main():
    if NEW.exists():
        shutil.rmtree(NEW)
    (NEW / "pages").mkdir(parents=True)
    (NEW / "_layout").mkdir()
    (NEW / "assets" / "css").mkdir(parents=True)
    (NEW / "assets" / "js").mkdir(parents=True)

    home = (OLD / "index.html").read_text()
    # shared CSS: the article-page variant is a strict superset of the other one
    styles = re.findall(r"<style>(.*?)</style>", home, re.S)
    (NEW / "assets/css/site.css").write_text(
        "/* AscendPoint Agency — site styles. Edit here; every page uses this one file. */\n"
        + styles[0].strip() + "\n\n" + styles[1].strip() + "\n")
    js = re.findall(r"<script>\s*(\(function\(\)\{\s*var h=document.*?)</script>", home, re.S)[0]
    (NEW / "assets/js/site.js").write_text("/* AscendPoint Agency — site behaviour (menu, filters). */\n" + js.strip() + "\n")

    hdr = re.search(r'<header class="site-header".*?</header>', home, re.S).group(0)
    hdr = re.sub(r' aria-current="page"', "", hdr).replace('class="dd current"', 'class="dd"')
    ftr = re.search(r'<footer class="site-footer".*?</footer>', home, re.S).group(0)
    (NEW / "_layout/header.html").write_text(fix_links(hdr) + "\n")
    (NEW / "_layout/footer.html").write_text(fix_links(ftr) + "\n")

    for name, path in ROUTES.items():
        s = (OLD / name).read_text()
        main_m = re.search(r"<main[^>]*>(.*)</main>", s, re.S)
        body = main_m.group(1).strip("\n")
        # page-specific scripts that sit after </footer> (none today besides shared JS)
        desc = attr(s, r'<meta name="description" content="([^"]*)"')
        og = attr(s, r'<meta property="og:image" content="\{\{BASE_URL\}\}/([^"]+)"')
        old_title = attr(s, r"<title>(.*?)</title>").replace(" · AscendPoint", "")
        title = TITLES.get(path) or f"{old_title} | AscendPoint"
        fm = {"title": title, "description": desc, "image": "/" + og if og else ""}
        if path in NAMES:
            fm["name"] = NAMES[path]
        if path.startswith("capabilities/"):
            fm["type"] = "service"
        elif path.startswith("news/"):
            fm["type"] = "news"
            ld = re.search(r'"datePublished": "([^"]+)"', s)
            fm["date"] = ld.group(1)
            fm["name"] = old_title
            fm["author"] = BRAND
        elif path in BLOG_POSTS:
            fm["type"] = "post"
            fm["date"], fm["author"] = BLOG_POSTS[path]
            fm["name"] = old_title
        elif path in ("news", "blog", "capabilities", "brands", "our-results"):
            fm["type"] = "collection"
        elif path == "about":
            fm["type"] = "about"
        elif path == "contact":
            fm["type"] = "contact"
        elif path == "404":
            fm["type"] = "404"
            fm["noindex"] = "true"
        else:
            fm["type"] = "page"
        if path == "":
            fm["type"] = "home"
        body = fix_links(body)
        # results page: fill the open location on the CR Smiles case study
        body = body.replace('<span class="muted" style="font-size:14px">[Location]</span>',
                            '<span class="muted" style="font-size:14px">Katy, TX</span>')
        out = NEW / "pages" / ((path or "index") + ".html")
        out.parent.mkdir(parents=True, exist_ok=True)
        front = "\n".join(f"{k}: {v}" for k, v in fm.items() if v)
        out.write_text(f"<!--\n{front}\n-->\n{body}\n")
        print(f"{name:55} -> {URLMAP[name]}")

    for f in ["favicon.ico", "favicon-32.png", "favicon-192.png", "apple-touch-icon.png"]:
        shutil.copy(OLD / f, NEW / f)
    shutil.copytree(OLD / "img", NEW / "img")


if __name__ == "__main__":
    main()


# ---- step 2: move the hand-written News / Insights listing copy into each article's
# front matter, and replace the lists with {{news_list}} / {{blog_list}} tokens that
# build.py expands. build.py's self-test proves the expansion is byte-identical.
def extract_lists():
    pages = NEW / "pages"
    news = (pages / "news.html").read_text()
    items = re.findall(r'<article class="news-item has-media" id="([^"]+)"><a class="news-thumb" href="([^"]+)" aria-hidden="true" tabindex="-1">'
                       r'<img src="([^"]+)" alt="([^"]*)" loading="lazy" decoding="async"></a><div class="copy"><time>([^<]+)</time>'
                       r'<h2><a href="[^"]+">(.*?)</a></h2>(.*?)<a class="arrow" href="[^"]+">Read the full story →</a></div></article>', news, re.S)
    assert len(items) == 7, len(items)
    for anchor, href, img, alt, when, title, summary in items:
        f = pages / (href.strip("/") + ".html")
        s = f.read_text()
        extra = (f"list_anchor: {anchor}\nlist_title: {title}\nlist_date: {when}\n"
                 f"list_image: {img}\nlist_image_alt: {alt}\nlist_summary: {summary}\n")
        f.write_text(s.replace("\n-->\n", "\n" + extra.rstrip("\n") + "\n-->\n", 1))
    block = re.search(r'<div class="news-list">.*?</div></div></section>', news, re.S).group(0)
    (NEW / ".news_list_expected.html").write_text(block)
    news = news.replace(block, '<div class="news-list">{{news_list}}</div></div></section>')
    (pages / "news.html").write_text(news)

    blog = (pages / "blog.html").read_text()
    cards = re.findall(r'<a class="card post" href="([^"]+)"><div class="thumb"><img src="([^"]+)" alt="([^"]*)" loading="lazy" decoding="async"></div>'
                       r'<time>([^<]+)</time><h3 class="h3" style="font-size:22px">(.*?)</h3><p class="small body grow">(.*?)</p><span class="arrow">Read article →</span></a>', blog, re.S)
    assert len(cards) == 4, len(cards)
    for href, img, alt, when, title, blurb in cards:
        f = pages / (href.strip("/") + ".html")
        s = f.read_text()
        extra = f"list_title: {title}\nlist_date: {when}\nlist_image: {img}\nlist_image_alt: {alt}\nlist_summary: {blurb}\n"
        f.write_text(s.replace("\n-->\n", "\n" + extra.rstrip("\n") + "\n-->\n", 1))
    block = re.search(r'<div class="grid g2"><a class="card post".*?</a></div>', blog, re.S).group(0)
    (NEW / ".blog_list_expected.html").write_text(block)
    blog = blog.replace(block, '<div class="grid g2">{{blog_list}}</div>')
    (pages / "blog.html").write_text(blog)
    print("lists extracted: 7 news, 4 posts")
