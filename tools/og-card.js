// Branded 1200x630 share card (navy + AscendPoint brand pattern + TM logo + title).
// Used for pages without a photo (e.g. /contact/, /our-results/).
// Usage: node tools/og-card.js "Contact" site/img/og/contact.jpg [--kicker "AscendPoint Agency"]
// Needs Playwright + a Chromium (set CHROMIUM=/path if Playwright's own browser isn't installed).
const path = require('path');
const { chromium } = require('playwright');
const [, , title, out, ...rest] = process.argv;
if (!title || !out) { console.error('usage: node tools/og-card.js "Title" out.jpg [--kicker "text"]'); process.exit(1); }
const ki = rest.indexOf('--kicker'); const kicker = ki >= 0 ? rest[ki + 1] : 'AscendPoint Agency';
const site = path.resolve(__dirname, '..', 'site');
const url = p => 'file://' + path.join(site, p);
const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const html = `<!doctype html><html><head><style>
@font-face{font-family:M;src:url(${url('assets/fonts/montserrat-latin-700-normal.woff2')})}
@font-face{font-family:M6;src:url(${url('assets/fonts/montserrat-latin-600-normal.woff2')})}
html,body{margin:0;width:1200px;height:630px;background:#003251;overflow:hidden}
.p{position:absolute;inset:0 0 0 35%;background:url(${url('img/brand-pattern.svg')}) right top/100px auto repeat;
-webkit-mask-image:linear-gradient(90deg,transparent,#000 50%)}
.c{position:absolute;left:84px;top:84px;right:84px;bottom:84px;display:flex;flex-direction:column}
img{height:88px;width:auto;align-self:flex-start}
.r{width:120px;height:6px;background:#3EC7E3;border-radius:3px;margin:auto 0 30px}
.k{font:600 22px/1 M6;letter-spacing:.14em;text-transform:uppercase;color:#3EC7E3;margin-bottom:16px}
h1{font:700 76px/1.05 M;color:#fff;margin:0;max-width:900px}
</style></head><body><div class="p"></div><div class="c"><img src="${url('img/ap-logo-tm.webp')}">
<div class="r"></div><div class="k">${esc(kicker)}</div><h1>${esc(title)}</h1></div></body></html>`;
(async () => {
  const b = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  const p = await b.newPage({ viewport: { width: 1200, height: 630 } });
  const fs = require('fs'), os = require('os');
  const tmp = path.join(os.tmpdir(), 'og-card-' + process.pid + '.html');
  fs.writeFileSync(tmp, html);
  await p.goto('file://' + tmp, { waitUntil: 'load' });
  fs.unlinkSync(tmp);
  await p.evaluate(() => document.fonts.ready);
  await p.screenshot({ path: out, type: 'jpeg', quality: 88 });
  await b.close();
  console.log('wrote', out);
})();
