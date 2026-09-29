# ascendpoint.agency

The AscendPoint Agency website: plain HTML pages, built and checked by a small Python script,
hosted on **Kinsta** (Kinsta's static-site hosting, which Kinsta now runs under its Sevalla
dashboard, same login). No WordPress, no plugins, nothing to patch.

| | |
|---|---|
| **Production** | https://ascendpoint.agency (WordPress until go-live, see *Go-live runbook*) |
| **Kinsta preview** | https://ascendpoint-agency-3ueyp.kinsta.page (noindex, no analytics) |
| **Staging (noindex)** | https://ascendpoint.github.io |
| **Repo** | https://github.com/ascendpoint/ascendpoint.github.io (`main` = source, `kinsta` = built site) |
| **Deploy** | push to `main` → GitHub Actions builds + checks → `kinsta` branch → Sevalla API deploy → live check (~2 min) |

## Changing the site: ask Claude

Say what you want, e.g. *"Add a news article about X"*, *"Change the About page headline"*,
*"Add a capability page for Patient Engine"*. Claude edits `site/`, runs the build (which runs
every check), commits and pushes. The change is live about two minutes later. Nobody logs into
anything, and a change that breaks a link, a title or a redirect never deploys.

By hand, the loop is the same:

```bash
python3 tools/build.py          # builds dist/ and runs every check (exit 1 = don't push)
git commit -am "What changed" && git push
```

## Where things live

```
site/
  pages/                 one file per page; the file path IS the URL
    index.html             -> /
    about.html             -> /about/
    capabilities/healthcare-strategy.html -> /capabilities/healthcare-strategy/
    news/<slug>.html       -> /news/<slug>/
    <post-slug>.html       -> /<post-slug>/   (blog posts keep their WordPress URLs)
  _layout/               base.html (the <head>), header/footer (+ landing-page variants)
  _partials/             reusable blocks: {{> funnel-proof}}
  assets/css/site.css    all styles        assets/js/site.js   menu + filters
  assets/fonts/          self-hosted Montserrat + Albert Sans (SIL OFL)
  img/                   images; img/og/ = 1200x630 share images
  _redirects             old URL -> new URL rules (301)
tools/
  build.py               site/ -> dist/   (+ sitemap, RSS, llms.txt, schema, headers)
  check.py               quality gate: SEO tags, links, anchors, images, schema, sitemap
  test_redirects.py      proves every old WordPress URL lands on a real page
  legacy_urls.txt        every URL the WordPress site exposed (Sep 2026 inventory)
  live_check.py          the same checks against the live host, after each deploy
  archive/               one-time migration scripts (kept for the record)
```

### Every page starts with front matter

```html
<!--
title: Healthcare Marketing Strategy | AscendPoint Agency
description: 120–160 characters. Shown in Google and on LinkedIn/Facebook cards.
image: /img/og/strategy.jpg
name: Strategy
type: service
-->
<section class="hero page">…page content…</section>
```

| key | meaning |
|---|---|
| `title` | `<title>` tag, ≤ 60 characters, keyword first |
| `description` | meta description, 70–170 characters (build fails outside that) |
| `image` | 1200×630 share image (defaults to the home page card) |
| `name` | short name for breadcrumbs and schema |
| `type` | `home` `about` `contact` `collection` `service` `news` `post` `page` `landing` `404` |
| `date`, `author` | news + blog posts |
| `list_title`, `list_date`, `list_image`, `list_image_alt`, `list_summary`, `list_anchor` | how a news item / post appears on /news/ or /blog/ |
| `layout: landing` | logo-only header + ad-compliance footer (funnel pages) |
| `noindex: true` | keep out of Google and the sitemap |
| `updated` | override the last-modified date (otherwise taken from git) |

The build generates everything else: canonical, Open Graph/Twitter tags, JSON-LD
(Organization, WebSite, WebPage, BreadcrumbList, Service, NewsArticle, BlogPosting),
`width`/`height` on every image, fingerprinted CSS/JS, `sitemap.xml` with real lastmod
dates, `feed.xml`, `llms.txt`, `robots.txt`, `_headers`, and Google Tag Manager
(`GTM-P2C557NR`, fires GA4 `G-BGK6QEZTTH`) on ascendpoint.agency only.

### Common jobs

- **New news article:** copy any file in `site/pages/news/`, rename it to the new slug, update
  front matter (`date`, `list_*`) and the body. It appears on /news/, in the sitemap and in the
  RSS feed automatically, newest first.
- **New blog post:** same, with a file in `site/pages/` and `type: post`. It appears on /blog/.
- **New capability page:** copy a file in `site/pages/capabilities/`, then add it to the menu in
  `site/_layout/header.html` (and the footer if wanted) and to the capabilities page.
- **Rename or remove a page:** move/delete the file, then add a 301 in `site/_redirects`.
  `tools/test_redirects.py` fails the build if an old URL stops resolving.

## URLs and redirects

Every page that existed on the WordPress site kept its exact URL (/, /about/, /contact/,
/our-results/, /blog/, /capabilities/healthcare-…/, the four blog posts, and the ad-funnel
pages /strategy-session/, /schedule-a-call/, /call-confirmed/), so rankings, backlinks, ads and
emails keep working with no redirect hop. Everything else the old site exposed (legal-marketing
pages, /other-industries/, /solutions/*, category, author, event, feed and sitemap URLs) 301s
to the closest page. The full inventory is `tools/legacy_urls.txt`; the build proves every one.

## Kinsta settings (Sevalla → company "SERP Agency" → Static sites → ascendpoint-agency)

| Setting | Value |
|---|---|
| Preview address | https://ascendpoint-agency-3ueyp.kinsta.page |
| Source | public repo `https://github.com/ascendpoint/ascendpoint.github.io`, branch **`kinsta`** |
| Build site before publishing | **off** (GitHub Actions already built and checked it) |
| Publish directory | *(blank = repo root)* |
| Deploys | GitHub Actions calls the Sevalla API (`POST /v3/static-sites/{id}/deployments`) after each publish. Needs repo secret `SEVALLA_API_KEY` + variable `SEVALLA_SITE_ID`. Without them, click **Deploy now** in Sevalla. |
| Live verification | repo variable `LIVE_URL` (preview address now, `https://ascendpoint.agency` after go-live) |

`_redirects` and `_headers` sit at the root of the `kinsta` branch, which is where Kinsta reads them.
Kinsta quirk (verified live): a splat rule `/x/*` does not match the bare `/x/`, so every splat
has an exact twin; `tools/test_redirects.py` simulates that behaviour.

## Go-live runbook (WordPress → Kinsta)

1. `python3 tools/check.py --strict-launch` passes (no draft quotes / `[confirm]` placeholders).
2. Sevalla → Domains → add `ascendpoint.agency` and `www.ascendpoint.agency`; add the TXT
   verification records it shows in Cloudflare as **DNS only** (grey cloud).
3. In Cloudflare, replace the apex + www records with the A records Sevalla shows (DNS only).
   Leave MX, SPF, DKIM, DMARC and every other record alone (email keeps working).
4. When Sevalla shows both domains active with SSL: set the repo variable `LIVE_URL` to
   `https://ascendpoint.agency`, re-run the workflow, and `tools/live_check.py` confirms every
   page and every old URL on the real domain.
5. Google Search Console: submit `https://ascendpoint.agency/sitemap.xml`; URL-inspect the home
   page. GA4: confirm real-time traffic from the new site.
6. Add this rule at the top of `site/_redirects` so the preview address stops competing with
   the real domain: `https://ascendpoint-agency-3ueyp.kinsta.page/*  https://ascendpoint.agency/:splat  301!` (plus the bare `/` twin)
7. Keep the WordPress install 30 days as a rollback (rollback = point DNS back), then cancel it.

## Access

Pushes use a fine-grained GitHub token scoped to this repository only. It lives on Kyle's Mac
at `Websites/.secrets/github-token` (never committed). Kinsta access is through Kyle's
MyKinsta login (Sevalla dashboard).
