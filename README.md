# ascendpoint.agency

The AscendPoint Agency website: plain HTML pages, built and checked by a small Python script,
hosted on **Kinsta** (Kinsta's static-site hosting, which Kinsta now runs under its Sevalla
dashboard, same login). No WordPress, no plugins, nothing to patch.

| | |
|---|---|
| **Production** | https://ascendpoint.agency (live on Kinsta since Oct 1 2026; www → apex) |
| **Kinsta preview** | https://ascendpoint-agency-3ueyp.kinsta.page (noindex, no analytics) |
| **Staging (noindex)** | https://ascendpoint.github.io |
| **Repo** | https://github.com/ascendpoint/ascendpoint.github.io (`main` = source, `kinsta` = built site) |
| **Deploy** | push to `main` → GitHub Actions builds + checks → `kinsta` branch → Sevalla API deploy → live check (~2 min) |

## Website requests: Slack, no logins (AscendPoint AI robot)

Anyone at AscendPoint can change the site without GitHub, Kinsta or Claude accounts:

- **Slack:** post in **#ascendpoint-website-requests** (renamed from #website-requests on Oct 4) what you want, in plain English (screenshots and photos
  welcome). One request per message.
- **No email.** Email requests were switched off on Oct 4 (anyone can type a From address, so email
  can't be trusted to change the site). Anything that still arrives by email is refused with 🚫.
- **SERP Dental site (serpdental.com)?** Different channel: **#serpdental-website-requests**. Each channel's topic says which site it edits; this robot never touches serpdental.com.

What happens (about 3 to 6 minutes, fully automatic; pickup within ~10 seconds):
1. AscendPoint AI reacts 👀 and replies "On it" in the thread.
2. Claude makes the change on the site's code and runs every quality check (links, titles, images,
   redirects, schema). If anything fails, nothing goes live.
3. It publishes to ascendpoint.agency and replies ✅ with what changed and links to the pages.
4. Reply **undo** in the thread to roll that change back; reply with tweaks to adjust it.
5. If something's unclear, missing (prices, dates, quotes, names are never made up) or risky
   (deleting pages, legal text), it replies 💬 with a question instead. Answer in the thread.

Messages starting with `//`, `note:` or `fyi` are ignored (chat freely that way).

**Photos (headshots, logos, pictures): just attach them in Slack.** Drag the photo into the message, e.g.
"Update Tyler's headshot in the team section with this photo", or post a photo with no words in a thread to
say "use this one". Any format works, including iPhone HEIC. The robot:
- downloads every attachment (and, for "retry" or a follow-up, the photos posted earlier in that thread),
  checks it's a real image and looks at it;
- to **swap** a picture already on the site runs `tools/img_for_web.py <photo> --replace <current image>`:
  same size and shape as before, people are framed on their face the way the old photo was (OpenCV face
  finder), the file gets a **new name** (images are cached for 30 days, so reusing the old name would keep
  showing the old picture), and every page that used it is updated;
- to **add** a picture makes a web-ready .webp (new team member: 320x320 `hs-<firstname>-sq.webp`,
  `--headshot`) and adds it with proper alt text;
- screenshots the page and looks before publishing. Reply **undo** to put the old picture back.
If Slack won't hand over the file (e.g. the app lost `files:read`), it says so with ⚠️ and @mentions the
owner instead of guessing. If Claude is ever blocked from a step, the 💬 reply names the blocked step for
the owner. By hand: `python3 tools/img_for_web.py --help`.

**Three models, picked per request (cheapest that does the job well).** Short text-only edits (a typo, a
phone number, a date, a one-line wording swap) use Claude Haiku: a fraction of Sonnet's cost. Everyday edits
(text, links, photos, removing things) use Claude Sonnet: fast and cheap (typically 20-60 s of work,
$0.05-0.30). Design, motion and interactive work (animation, "movement", on-scroll effects, hover effects,
carousels, new pages or sections, layout/redesign, forms, "this looks bad") uses Claude Opus with a higher cap.
A follow-up in that thread stays on Opus. If Haiku doesn't manage a change, Sonnet redoes it. Force a model by
writing `[opus]`, "use the best model" or "try harder" (or `[sonnet]` / `[haiku]`) in the message. If a change
fails the site checks, Opus gets one automatic repair pass with the exact errors before anything is reported
as failed.

**Preview before it goes live.** Say so in the request ("preview first", "send me a mockup", "show me before
it goes live", "staging"). The robot makes the change on branch `preview/<thread>` (never `main`), publishes a
staging build (noindex, no analytics) to branch `kinsta-preview`, which the Kinsta preview site (`PREVIEW_URL`)
serves, and replies 🔍 with the preview link (no screenshots). In that thread: **approve** (or
"ship it", "looks good", "go live", "yes") puts exactly that change live; any other reply updates the preview;
**cancel** drops it 🗑️. The preview site (https://ascendpoint-preview-nwyxk.kinsta.page, Sevalla static site
`ascendpoint-preview`) always shows the most recent preview.
No preview is made unless the request asks for one: everything else goes straight live as before. If a thread's first message asked
for a preview, later replies in it stay previews until something from the thread is approved.

For anything visual the robot builds the site and screenshots it on desktop and mobile
(`tools/screenshot.py`, headless Chromium; frames over time for animation, hover and on-scroll states) and
must look at the result before it publishes. The ✅ reply names the model used and the cost.

How it works: `.github/workflows/website-requests.yml` keeps one listener running in GitHub Actions
(polls Slack every 10 s for ~5.7 h, then starts its successor; the 15-minute cron is a backup; free on a public repo; no one's
computer needs to be on). `tools/website_requests.py` does the work: Claude Code in
headless mode with a tight tool allowlist (edit `site/` only, run the build, the image tool and the
screenshot tool; no git, no web, no other shell; Slack attachments are downloaded into the git-ignored
`.request-files/`), packages from `tools/requirements-robot.txt`,
a $3 cap per request, then the normal checks, a commit to `main` by "AscendPoint AI" with
`Requested-by` / `Slack-Thread` trailers (so undo finds it), the deploy workflow, and a live check of
`/version.txt`. Slack reactions are the queue state (👀 working, ✅ live, 🔍 preview waiting for approval, 🗑️ preview
dropped, 💬 question, ⚠️ failed, ↩️ undone, 🚫 email refused). Tests: `python3 -m unittest discover -s tests` (also run by CI on every push).

Settings (GitHub → Settings → Secrets and variables → Actions): secrets `ANTHROPIC_API_KEY` (pay-as-you-go
key from the Claude Console, ideally in its own workspace "AscendPoint website robot" so Usage/Cost shows
exactly what the robot spends; typically $0.20 to $1 per request, also printed in each ✅ reply) and `SLACK_BOT_TOKEN` (the AscendPoint AI
Slack app's bot token: scopes `channels:history`, `chat:write`, `reactions:read`, `reactions:write`,
`users:read`, `files:read`; `files:write` is granted but no longer used; the app must be in the channel); variables `SLACK_CHANNEL_ID`,
`WEBSITE_REQUESTS_START` (ignore messages before this unix time), optional `WEBSITE_REQUESTS_ALLOWED`
(comma-separated Slack user IDs; empty = anyone in the channel), `WEBSITE_REQUESTS_OWNER` (Slack user ID
to @mention on failures), `CLAUDE_MODEL` (default `sonnet`), `CLAUDE_MAX_USD` (default 3), `CLAUDE_MODEL_ADVANCED` (default `opus`),
`CLAUDE_MAX_USD_ADVANCED` (default 10), `CLAUDE_MODEL_SIMPLE` (default `haiku`), `CLAUDE_MAX_USD_SIMPLE` (default 1),
`PREVIEW_URL` (the Kinsta preview site serving branch `kinsta-preview`),
`ANTHROPIC_WORKSPACE_ID` (Claude Console workspace to bill; required when the API key is organization-level,
not created inside a workspace: Oct 2 2026 = Default workspace wrkspc_01AphpFe2Yxf9FqUWDUssPN6 of the
Claude Console org Kyle created for the robot).

Current setup (Oct 4 2026): Slack workspace AscendPoint Agency, channel **#ascendpoint-website-requests**
(`C0C67AHG5T8`, public); bot = Slack app A0BB2TCVB9P "AscendPoint AI" (bot user U0BAXPR7CH3, shown as
"AscendPoint MCP" until the app is reinstalled; it also powers Kyle's local slack-ascendpoint MCP);
`WEBSITE_REQUESTS_OWNER` = Kyle (U04Q3M29UKE). Email path: retired Oct 4 (Slack only). The channel's Slack email address was deleted and the Google Group
website@ascendpoint.agency no longer forwards to Slack; the robot refuses any email that still lands (🚫).
To pause the robot: Actions → "Website requests" → ⋯ → Disable workflow.
If the repo is ever made private, the always-on listener would use ~43,000 Actions minutes/month
(over the free tier): switch the cron to `*/5` with `LISTEN_MINUTES: "4"` or move to Slack Events first.

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

- **Swap a photo (e.g. a team headshot):** `python3 tools/img_for_web.py new.jpg --replace site/img/hs-tyler-sq.webp`
  (new file name, same size, face-framed, all references updated). Add one: `... new.jpg site/img/name.webp --width 1200`.
- **New news article:** copy any file in `site/pages/news/`, rename it to the new slug, update
  front matter (`date`, `list_*`) and the body. It appears on /news/, in the sitemap and in the
  RSS feed automatically, newest first.
- **New blog post:** same, with a file in `site/pages/` and `type: post`. It appears on /blog/.
- **New capability page:** copy a file in `site/pages/capabilities/`, then add it to the menu in
  `site/_layout/header.html` (and the footer if wanted) and to the capabilities page.
- **Rename or remove a page:** move/delete the file, then add a 301 in `site/_redirects`.
  `tools/test_redirects.py` fails the build if an old URL stops resolving.

### Brand assets (source: Drive "AscendPoint Brand Assets", Sonia)

- **Logos (™ versions):** `img/ap-logo-tm.webp` = AscendPoint-Agency-Logo-T02 (sky mark + white
  type) for the dark header/footer; `img/ap-logo-tm-color.png` = AscendPoint-Agency-Logo-TM (sky
  mark + navy type) for light backgrounds and the Organization logo in JSON-LD. Images are cached
  30 days, so a changed logo gets a **new file name** (update `_layout/*.html` and `tools/build.py`).
- **Pattern:** `img/brand-pattern.svg` is one repeat tile of "AscendPoint Agency - Pattern-01"
  (vector, taken from Pattern.ai): sky #3EC7E3 at 16% on Prussian #003251. Used by `.hero::before`
  (right side, fading out to the left) and `.cta-band::before` in `assets/css/site.css`.
- **Share cards for pages without a photo:** `node tools/og-card.js "Title" site/img/og/x.jpg
  --kicker "Label"` renders a 1200x630 navy card with the pattern + ™ logo (needs Playwright).

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
| Deploys | **Automatic** (since Oct 2 2026): GitHub Actions calls the Sevalla API (`POST https://api.sevalla.com/v3/static-sites/{id}/deployments`, body `{"branch":"kinsta"}`) after each publish. Repo secret `SEVALLA_API_KEY` = Sevalla key `github-actions-ascendpoint-deploy` (static-site create/read/update, scoped to this site only, no expiry; rotate in Sevalla → Integration → API keys, then update the secret). Repo variable `SEVALLA_SITE_ID` = `25a02981-5228-4223-8331-d965ed6e420f`. Manual fallback: **Deploy now** in Sevalla. |
| Domains | `ascendpoint.agency` (**primary**) + `www.ascendpoint.agency` (301 → apex); preview address still answers, noindex |
| Error file | *(blank on purpose: setting it to 404.html serves that page with status 200, a soft 404; Kinsta's own 404 page keeps the real 404 status)* |
| Live verification | repo variable `LIVE_URL` = `https://ascendpoint.agency` |

`_redirects` and `_headers` sit at the root of the `kinsta` branch, which is where Kinsta reads them.
Kinsta quirk (verified live): a splat rule `/x/*` does not match the bare `/x/`, so every splat
has an exact twin; `tools/test_redirects.py` simulates that behaviour.

## Go-live record (done Oct 1 2026) and rollback

Cut over on Oct 1 2026 (approved by Kyle):

- Sevalla → Domains: `ascendpoint.agency` (primary) and `www.ascendpoint.agency` added, both Active with SSL.
- Cloudflare zone `ascendpoint.agency` (account websites@serp.co), all **DNS only** (grey cloud):
  - `ascendpoint.agency` CNAME `to.kinsta.page` (was `ascendpointmarketing.hosting.kinsta.cloud`, proxied)
  - `www` CNAME `to.kinsta.page` (was the same WordPress target, proxied)
  - `_acme-challenge` and `_acme-challenge.www` TXT = Sevalla SSL tokens (replaced the old WordPress
    `_acme-challenge` CNAME to `ascendpoint.agency.kinstavalidation.app`)
  - MX, SPF, DKIM (ctct, smtp2go), Google site verification and `analytics.` untouched.
- Repo variable `LIVE_URL` = `https://ascendpoint.agency`; `tools/live_check.py` passes on the real domain
  (35 sitemap pages, 51 legacy URLs).

**Rollback** (WordPress is still installed on Kinsta as `ascendpointmarketing`): point the apex and
www CNAMEs back to `ascendpointmarketing.hosting.kinsta.cloud` (proxied) and restore the
`_acme-challenge` CNAME above. Keep WordPress ~30 days, then cancel it.

Still to do: submit `https://ascendpoint.agency/sitemap.xml` in Google Search Console (the property
belongs to a Google account other than kyle@ascendpoint.agency); confirm GA4 real-time traffic.

## Access

Pushes use a fine-grained GitHub token scoped to this repository only. It lives on Kyle's Mac
at `Websites/.secrets/github-token` (never committed). Kinsta access is through Kyle's
MyKinsta login (Sevalla dashboard).
