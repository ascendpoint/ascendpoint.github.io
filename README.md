# ascendpoint.agency — website source

Static site for AscendPoint Agency, built from Patrick's Claude-made design (Sep 2026).
No WordPress, no page builder: every page is a plain HTML file in `site/`, and every
push to `main` rebuilds, checks and publishes the site automatically.

| | |
|---|---|
| **Live preview (staging)** | https://ascendpoint.github.io |
| **Repo** | https://github.com/ascendpoint/ascendpoint.github.io |
| **Production domain** | ascendpoint.agency (still on WordPress; see *Going live* below) |
| **Deploys** | GitHub Actions → GitHub Pages, ~1 minute after each push |

## How to change the site (the normal way: ask Claude)

Tell Claude what to change, e.g. *"Update the MMW news article's quote"* or *"Add a
capability page for Patient Engine"*. Claude clones this repo, edits the HTML in `site/`,
runs the build + checks locally, commits and pushes. The Action redeploys; the change is
live in about a minute. Nobody logs into anything.

If you edit by hand: change files in `site/`, then

```bash
python3 tools/build.py            # builds dist/ and runs every check
git commit -am "What changed" && git push
```

## Layout

```
site/                 the website. One .html file per page, images in site/img/
  img/og/             1200x630 share images (one per page, used by LinkedIn/Facebook/Slack)
  _redirects          old WordPress URLs → new pages (used at production cutover)
  404.html            generated from the home page chrome
tools/build.py        site/ → dist/ for an environment (staging | production)
tools/check.py        fails the build on broken links/images, missing titles/descriptions,
                      missing share images, duplicate <h1>, unclosed tags, prototype copy,
                      or wrong indexing for the environment
tools/productionize.py  one-time pass that turned the prototype into production pages
                      (meta, Open Graph, JSON-LD, favicons, Typeform contact embed). Idempotent.
.github/workflows/deploy.yml  build → check → deploy on every push to main
```

## Environments

Set with two repository **variables** (Settings → Secrets and variables → Actions → Variables):

| Variable | staging (default) | production |
|---|---|---|
| `SITE_ENV` | `staging` | `production` |
| `SITE_URL` | `https://ascendpoint.github.io` | `https://ascendpoint.agency` |

- **staging**: every page `noindex, nofollow`, `robots.txt` blocks crawlers, no analytics.
  Safe to share; Google won't index it and it won't compete with the live site.
- **production**: canonical URLs on the real domain, Google Tag Manager `GTM-P2C557NR`
  (the same container the WordPress site uses, which fires GA4 `G-BGK6QEZTTH`),
  crawlable `robots.txt`, `sitemap.xml`.

Pages use clean URLs (`/about`, not `/about.html`); `build.py` rewrites internal links.

## Contact form

Patrick's contact page routes dental practices to SERP Dental and med spas to MMW with the
three cards; the form underneath is for everyone else (agency founders, investors, partners,
job seekers). It's a Typeform built for this page:

- **Form:** *AscendPoint Website – Contact (Founders, Partners & Investors)*, id `Bs7qxsde`,
  theme *AscendPoint Website* (`G1U3NOCg`, navy/cyan, Montserrat), workspace *SERP Agency*.
- **Questions:** who you are → name → email → phone (optional) → company (optional) → message.
- **Routing:** anyone who picks *Dental practice* ends on a "Book with SERP Dental" screen;
  *Med spa / aesthetics / women's health* ends on "Meet with Lori" (MMW). Everyone else
  gets the standard thank-you. All responses are kept in Typeform either way.
- **Notification:** every submission emails **kyle@ascendpoint.agency** with all answers.
  Add Patrick/Sonia in Typeform → Connect → Email notifications.
- **Attribution:** `utm_*` parameters on the page URL pass through to hidden fields, plus
  `landing_page`.

## Going live on ascendpoint.agency (not done yet — on purpose)

The WordPress site is untouched. When the team signs off:

1. **Content sign-off.** Open items from the review: news quotes marked *Draft quote ·
   pending approval*, SparkLyfe exact date, public email, Results page `[Location]`.
2. **Pick the host** (either works with this repo as-is):
   - *GitHub Pages*: Settings → Pages → Custom domain `ascendpoint.agency`, then in
     Cloudflare DNS point `ascendpoint.agency` + `www` at GitHub Pages (A records
     185.199.108-111.153 / CNAME `ascendpoint.github.io`). `_redirects` is not supported
     here; old WordPress URLs would 404 unless rules are added in Cloudflare.
   - *Cloudflare Pages* (recommended — DNS is already in Cloudflare, and it honours
     `_redirects`): Workers & Pages → Create → Connect to Git → this repo, build command
     `pip install pillow && python tools/build.py --env production --base-url https://ascendpoint.agency`,
     output `dist`. Add custom domain `ascendpoint.agency`.
3. Set `SITE_ENV=production`, `SITE_URL=https://ascendpoint.agency`, push, and submit
   `https://ascendpoint.agency/sitemap.xml` in Google Search Console.
4. Keep the WordPress install for 30 days as a rollback, then cancel hosting.

## Credentials

Pushes use a fine-grained GitHub token scoped to the `ascendpoint` org only
(contents, pages, workflows, administration on its repos). It lives on Kyle's Mac in the
project folder at `Websites/.secrets/github-token` and is never committed.
