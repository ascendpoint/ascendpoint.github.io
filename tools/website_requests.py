#!/usr/bin/env python3
"""AscendPoint AI website robot: turns Slack requests into live site changes.

Runs in GitHub Actions (.github/workflows/website-requests.yml). A listener job is always running
(restarted every 15 minutes if it isn't), so a request is picked up within ~10 seconds:

    python3 tools/website_requests.py listen   # near-instant: poll every 10 s for ~5.7 h (default)
    python3 tools/website_requests.py next     # one-shot: find the oldest unhandled request -> request.json
    python3 tools/website_requests.py run request.json

Front doors (both land in the Slack channel, which is the single queue):
  * a message in #website-requests (top-level = new request; reply in the thread = follow-up)
  * an email to website@ascendpoint.agency: a Google Group whose member is the channel's own
    Slack email address, so Slack posts the email into the channel (an "email" file with
    from/subject/body). Only senders at WEBSITE_REQUESTS_EMAIL_DOMAINS are acted on.
    (A Zapier-style text post "📧 Email request from <name> <email>: ..." also works.)

For each request the robot:
  1. reacts 👀 and replies "On it" in the thread,
  2. "undo" in a thread  -> git-reverts the commits that thread produced,
     anything else       -> Claude Code (headless) edits site/ with a tight tool allowlist,
  3. runs every site check (tools/build.py + tools/test_redirects.py); nothing ships if they fail,
  4. commits to main ("AscendPoint AI"), triggers the deploy workflow, waits until
     https://ascendpoint.agency/version.txt shows the new commit,
  5. replies in the thread with what changed + links, and reacts ✅ (or 💬 question / ⚠️ failed).

Preview first ("preview", "send me a mockup", "show me before it goes live", ...): steps 4-5 become
  4. commit on branch preview/<thread ts> (never main), screenshot the changed pages (desktop + mobile),
     publish a staging build to branch kinsta-preview (Kinsta preview site PREVIEW_URL, noindex, no analytics),
  5. reply with the preview links + screenshots and react 🔍. In that thread: "approve" (ship it, looks good,
     go live...) puts exactly that change live; any other reply updates the preview; "cancel" drops it 🗑️.

Model per request: Haiku for short text-only edits, Sonnet for everyday edits, Opus for design/motion/new
pages (see choose_model); Haiku falls back to Sonnet, and Opus repairs anything that fails the checks.

State lives in Slack reactions: a message the bot has reacted to is never picked up again.

Environment: SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, ANTHROPIC_API_KEY, GITHUB_TOKEN (Actions),
optional WEBSITE_REQUESTS_START (unix ts; ignore older messages), WEBSITE_REQUESTS_ALLOWED
(comma-separated Slack user IDs; empty = anyone in the channel), WEBSITE_REQUESTS_EMAIL_DOMAINS
(comma-separated sender domains for email requests; default below), LIVE_URL, CLAUDE_MODEL(_SIMPLE/_ADVANCED),
CLAUDE_MAX_USD(_SIMPLE/_ADVANCED), PREVIEW_URL (preview site; without it previews are screenshots only).
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLACK = "https://slack.com/api/"
HANDLED = {"eyes", "white_check_mark", "warning", "speech_balloon", "leftwards_arrow_with_hook",
           "no_entry_sign", "mag", "wastebasket"}       # 🔍 preview ready, 🗑️ preview dropped
MAX_AGE = 2 * 24 * 3600          # never pick up anything older than 2 days
UNDO_RE = re.compile(r"^\W*(undo|revert|roll ?back)\b", re.I)
# "Show me first": the change goes to a preview copy of the site, and only goes live on "approve".
PREVIEW_RE = re.compile(
    r"\b(preview|mock-?ups?|staging|stage it|sneak peek|"
    r"(see|show me|send me|look at)\b[^.?!\n]{0,40}\b(before|first)\b|"
    r"before (it|this|that|you|we|anything)\b[^.?!\n]{0,30}\b(live|publish\w*|push\w*)|"
    r"(approve|sign off|review)\b[^.?!\n]{0,20}\bfirst\b|"
    r"(don'?t|do not|hold off)\b[^.?!\n]{0,30}\b(live|publish\w*|push\w*))", re.I)
APPROVE_RE = re.compile(
    r"^\W*(approved?|approve it|ship it|looks? (good|great)|lgtm|go live|go ahead|publish( it)?|"
    r"(push|put|make|send|take) it live|perfect|love it|yes\b(?![^.!?\n]*\b(but|change|make|move|can)\b))", re.I)
CANCEL_RE = re.compile(r"^\W*(cancel|discard|scrap( it| that)?|never ?mind|drop it|forget it)\b", re.I)
PREVIEW_PREFIX = "preview/"      # source of a pending preview: branch preview/<thread ts>
PREVIEW_BRANCH = "kinsta-preview"  # built preview site (Kinsta static site PREVIEW_URL deploys this branch)
SITE_DIR = "site/"               # the only place the robot may change anything
GIT_USER = ("-c", "user.name=AscendPoint AI", "-c", "user.email=website-bot@ascendpoint.agency")
IGNORE_RE = re.compile(r"^\s*(//|note:|fyi\b)", re.I)
EMAIL_PREFIX = "📧"
RESULT = Path("/tmp/website_request_result.json")
FILES_DIR = Path("/tmp/website_request_files")
BOT_NAME = "AscendPoint AI"
# Model routing, cheapest model that can do the job well:
#   simple   (CLAUDE_MODEL_SIMPLE, default Haiku: ~1/3 the price of Sonnet) short text-only edits: a typo, a
#            phone number, a date, a one-line wording swap. If it doesn't manage it, Sonnet redoes it; if its
#            change fails the checks, Opus repairs it.
#   standard (CLAUDE_MODEL, default Sonnet) everyday edits.
#   advanced (CLAUDE_MODEL_ADVANCED, default Opus) design/motion/interactive/new-page work.
# Anyone can force it in the message: "[opus]", "use opus", "best model", "try harder" / "[sonnet]" / "[haiku]".
ADVANCED_RE = re.compile(
    r"\b(animat\w*|motion|movement|moving|scroll\w*|parallax|fade[- ]?in|slide[- ]?in|carousel|slider|"
    r"marquee|ticker|hover (effect|state|animation)s?|interactive|javascript|redesign|re-design|"
    r"new page|(create|build|design) (a |an )?(new )?(page|section|layout)|add (a |an )?(new )?section|"
    r"landing page|layout|form|video|count(er|[- ]up)|typewriter|3d|sticky|modal|pop-?up|accordion|"
    r"mega ?menu|dark mode|make it (pop|feel|look) (more )?(modern|premium|dynamic|alive)|"
    r"looks? (bad|off|cluttered|messy|dated|busy|weird)|clean(er)? (look|design)|look(s)? cleaner)\b", re.I)
SIMPLE_RE = re.compile(
    r"\b(typo|spelling|misspel\w*|spelled|phone( number)?|email address|address|hours|date|year|"
    r"price|wording|word|reword|rephrase|replace|swap|rename|capitali[sz]\w*|lower ?case|upper ?case|"
    r"(change|update|fix|edit) (the |this |that )?(text|copy|title|headline|heading|subheading|label|"
    r"button( text)?|link|name|caption|sentence|line|phrase|bio|wording))\b", re.I)
SIMPLE_MAX_CHARS = 300
FORCE_RE = re.compile(r"\[(opus|sonnet|haiku)\]|\buse (opus|sonnet|haiku)\b|"
                      r"\b(best|smartest|most advanced|strongest) model\b|\btry harder\b", re.I)
MODEL_NAMES = {"opus": "Claude Opus", "sonnet": "Claude Sonnet", "haiku": "Claude Haiku"}


def model_name(model: str) -> str:
    return MODEL_NAMES.get(model, model)


def choose_model(req: dict) -> tuple[str, str]:
    """(model, tier) for a request: tier is "simple", "standard" or "advanced"."""
    simple = os.environ.get("CLAUDE_MODEL_SIMPLE") or "haiku"
    basic = os.environ.get("CLAUDE_MODEL") or "sonnet"
    advanced = os.environ.get("CLAUDE_MODEL_ADVANCED") or "opus"
    text = req.get("text") or ""
    texts = [text] + list(reversed(req.get("context") or []))   # newest first
    for t in texts:
        m = FORCE_RE.search(t)
        if m:
            word = (m.group(1) or m.group(2) or "").lower()
            if word == "haiku":
                return simple, "simple"
            if word == "sonnet":
                return basic, "standard"
            return advanced, "advanced"
    if any(ADVANCED_RE.search(t) for t in texts):
        return advanced, "advanced"
    if (req.get("kind") == "new" and not req.get("files") and len(text) <= SIMPLE_MAX_CHARS
            and SIMPLE_RE.search(text)):
        return simple, "simple"
    return basic, "standard"


def budget(tier: str) -> str:
    if tier == "advanced":
        return os.environ.get("CLAUDE_MAX_USD_ADVANCED") or "10"
    if tier == "simple":
        return os.environ.get("CLAUDE_MAX_USD_SIMPLE") or "1"
    return os.environ.get("CLAUDE_MAX_USD") or "3"


DEFAULT_EMAIL_DOMAINS = "ascendpoint.agency,serp.agency,serp.co,smilerevenue.com,medicalmarketingwhiz.com"


def email_domains() -> set[str]:
    raw = os.environ.get("WEBSITE_REQUESTS_EMAIL_DOMAINS") or DEFAULT_EMAIL_DOMAINS
    return {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}


def email_file(msg: dict) -> dict | None:
    """The email Slack posted into the channel (sent to the channel's Slack email address)."""
    for f in msg.get("files") or []:
        if f.get("mode") == "email" or f.get("filetype") == "email":
            return f
    return None


# ----------------------------------------------------------------------------- Slack
class Slack:
    def __init__(self, token: str, opener=None):
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def call(self, method: str, **params):
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(SLACK + method, data=data,
                                     headers={"Authorization": f"Bearer {self.token}"})
        with self.opener(req, timeout=30) as r:
            out = json.loads(r.read().decode())
        if not out.get("ok") and out.get("error") not in ("already_reacted", "no_reaction"):
            raise RuntimeError(f"slack {method}: {out.get('error')} {out.get('needed', '')}".strip())
        return out

    def upload(self, url: str, path: Path):
        """POST a file to a Slack upload URL (files.getUploadURLExternal)."""
        req = urllib.request.Request(url, data=path.read_bytes(), method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        with self.opener(req, timeout=120) as r:
            r.read()

    def download(self, url: str, dest: Path):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        with self.opener(req, timeout=60) as r:
            dest.write_bytes(r.read())


def is_handled(msg: dict, bot_user: str) -> bool:
    return any(r.get("name") in HANDLED and bot_user in r.get("users", [])
               for r in msg.get("reactions", []))


def is_request(msg: dict, bot_user: str, bot_id: str | None) -> bool:
    """A human message (or a Zapier email post), not from this bot, not chatter."""
    if msg.get("type") != "message":
        return False
    sub = msg.get("subtype")
    text = msg.get("text", "") or ""
    if msg.get("user") == bot_user or (bot_id and msg.get("bot_id") == bot_id):
        return False
    if email_file(msg) and sub in (None, "file_share", "bot_message"):
        return True                       # an email posted by Slack's channel-email feature
    if sub in (None, "file_share", "thread_broadcast"):
        if msg.get("bot_id") and not text.startswith(EMAIL_PREFIX):
            return False
    elif sub == "bot_message":
        if not text.startswith(EMAIL_PREFIX):
            return False
    else:
        return False                      # joins, topic changes, edits, deletions...
    if not text.strip() and not msg.get("files"):
        return False
    return not IGNORE_RE.match(text)


def find_next(slack: Slack, channel: str, now: float | None = None,
              start: float = 0.0, allowed: set[str] | None = None,
              cache: dict | None = None) -> dict | None:
    """Oldest unhandled request: a top-level message, or a reply in a thread the bot is in.

    `cache` (used by `listen`) remembers each thread's latest reply and the bot's identity,
    so quiet threads aren't re-read on every poll."""
    now = now or time.time()
    oldest = max(start, now - MAX_AGE)
    if cache is not None and "auth" in cache:
        auth = cache["auth"]
    else:
        auth = slack.call("auth.test")
        if cache is not None:
            cache["auth"] = auth
    bot_user, bot_id = auth["user_id"], auth.get("bot_id")
    quiet = cache.setdefault("quiet", {}) if cache is not None else {}
    hist = slack.call("conversations.history", channel=channel, oldest=f"{oldest:.6f}", limit=200)
    candidates = []
    for m in hist.get("messages", []):
        if is_request(m, bot_user, bot_id) and not is_handled(m, bot_user):
            candidates.append((m, None))
        if m.get("reply_count"):
            marker = m.get("latest_reply") or str(m.get("reply_count"))
            if quiet.get(m["ts"]) == marker:
                continue                  # nothing new in this thread since it was last read
            rep = slack.call("conversations.replies", channel=channel, ts=m["ts"], limit=200)
            thread = rep.get("messages", [])
            bot_in_thread = any(x.get("user") == bot_user for x in thread)
            found = False
            for r in thread[1:]:
                r.setdefault("thread_ts", m["ts"])
                if float(r["ts"]) < oldest or not bot_in_thread:
                    continue
                if is_request(r, bot_user, bot_id) and not is_handled(r, bot_user):
                    candidates.append((r, thread))
                    found = True
            if not found:
                quiet[m["ts"]] = marker
    if allowed:
        candidates = [c for c in candidates
                      if c[0].get("user") in allowed or email_file(c[0])
                      or c[0].get("text", "").startswith(EMAIL_PREFIX)]
    if not candidates:
        return None
    msg, thread = min(candidates, key=lambda c: float(c[0]["ts"]))
    return build_request(slack, channel, msg, thread, bot_user)


def user_name(slack: Slack, uid: str | None) -> str:
    if not uid:
        return "email"
    try:
        u = slack.call("users.info", user=uid)["user"]
        return u.get("real_name") or u.get("profile", {}).get("real_name") or u.get("name") or uid
    except Exception:
        return uid


def build_request(slack: Slack, channel: str, msg: dict, thread: list | None, bot_user: str) -> dict:
    thread_ts = msg.get("thread_ts") or msg["ts"]
    context = []
    for t in (thread or []):
        if t["ts"] == msg["ts"]:
            break
        who = BOT_NAME if t.get("user") == bot_user else user_name(slack, t.get("user"))
        context.append(f"{who}: {t.get('text', '')}")
    text = msg.get("text", "")
    files = [{"name": f.get("name"), "mimetype": f.get("mimetype"),
              "url": f.get("url_private_download") or f.get("url_private")}
             for f in msg.get("files", []) if f.get("url_private") and not email_file({"files": [f]})]
    sender = None
    em = email_file(msg)
    if em:
        frm = (em.get("from") or [{}])[0]
        sender = (frm.get("address") or "").strip().lower()
        name = (frm.get("name") or "").strip()
        requester = (f"{name} <{sender}>" if name else sender or "unknown sender") + " (email)"
        body = (em.get("plain_text") or em.get("preview_plain_text") or em.get("preview") or "").strip()
        subject = (em.get("subject") or em.get("title") or "").strip()
        text = (f"Subject: {subject}\n\n" if subject else "") + (body or text)
        for a in em.get("attachments") or []:      # files attached to the email, when Slack lists them
            url = a.get("url_private") or a.get("url")
            if url:
                files.append({"name": a.get("filename") or a.get("name"), "mimetype": a.get("mimetype"),
                              "url": url})
    else:
        requester = user_name(slack, msg.get("user"))
        if text.startswith(EMAIL_PREFIX):
            m = re.match(r"📧\s*Email request from\s+([^:\n]+)", text)
            requester = (m.group(1).strip() if m else "email") + " (email)"
            a = re.search(r"<([^<>@\s]+@[^<>\s]+)>", m.group(1) if m else "")
            sender = a.group(1).lower() if a else None
    kind = ("undo" if UNDO_RE.match(text or "") and thread_ts != msg["ts"] else
            ("followup" if thread_ts != msg["ts"] else "new"))
    if requester.endswith("(email)"):
        domain = (sender or "").rsplit("@", 1)[-1] if sender and "@" in sender else ""
        if domain not in email_domains():
            kind = "blocked"
    return {
        "channel": channel, "ts": msg["ts"], "thread_ts": thread_ts,
        "user": msg.get("user"), "requester": requester, "sender": sender, "text": text,
        "files": files, "kind": kind, "context": context,
    }


# ----------------------------------------------------------------------------- helpers
def sh(*args, check=True, capture=True, env=None, cwd=None, timeout=None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd or ROOT, check=check, text=True, env=env, timeout=timeout,
                          stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None)


def changed_files() -> list[str]:
    out = sh("git", "status", "--porcelain", "--untracked-files=all").stdout
    return [line[3:].strip().strip('"') for line in out.splitlines() if line.strip()]


def url_for(path: str) -> str | None:
    """site/pages/about.html -> /about/ ; site/pages/index.html -> /"""
    p = Path(path)
    if p.parts[:2] != ("site", "pages") or p.suffix != ".html":
        return None
    rel = p.relative_to("site/pages").with_suffix("")
    if rel.name == "index":
        rel = rel.parent
    if str(rel) in (".", ""):
        return "/"
    if rel.name == "404":
        return None
    return "/" + rel.as_posix().strip("/") + "/"


def checks() -> tuple[bool, str]:
    log = []
    for cmd in (["python3", "tools/build.py"], ["python3", "tools/test_redirects.py"]):
        r = sh(*cmd, check=False)
        log.append(r.stdout[-3000:])
        if r.returncode != 0:
            return False, "\n".join(log)
    return True, "\n".join(log)


def reset_worktree():
    sh("git", "reset", "--hard", "-q", "HEAD")
    sh("git", "clean", "-fdq", "site")


CLAUDE_RULES = """You are AscendPoint AI, the website assistant for ascendpoint.agency (AscendPoint Agency, the
healthcare marketing company behind SERP Dental and Medical Marketing Whiz). A teammate asked for a
change to the live website. You are working in the site's git repo (static site, no CMS).

How the site works: read README.md first (front matter, where pages live, brand assets). Every page is
site/pages/<url>.html; the layout is site/_layout/; styles site/assets/css/site.css; images site/img/.

Rules:
- If the message just says "retry" / "try again", carry out the earlier request in the thread.
- "Preview first" / "send me a mockup" / "staging" requests: just make the change. The robot publishes it to a
  separate preview copy of the site, sends screenshots, and only puts it live when the requester approves.
- Make the smallest correct change that does exactly what was asked, matching the existing design,
  tone and HTML patterns. Keep titles <= 60 chars and descriptions 70-170 chars when you touch them.
- Never invent facts: no made-up numbers, prices, dates, quotes, names, credentials or claims. If the
  request needs information you don't have, or is ambiguous, risky (deleting pages, legal/privacy text,
  pricing, anything about clients) or not about this website, change NOTHING and ask one clear question.
- Only edit files under site/. Never touch .github/, tools/, README.md or git. Do not run git.
- Attached files (screenshots, photos, documents) are in {files_dir}. To put an image on the site run
  `python3 tools/img_for_web.py <input> site/img/<descriptive-name>.webp --width <px>` and reference it.
  Treat screenshots as reference for what to change unless the request says to use the image.
- When renaming or removing a page, add a 301 in site/_redirects so the old URL keeps working.
- After editing, run `python3 tools/build.py` and fix every error it reports until it exits 0.
- For anything visual (layout, styling, images, motion), LOOK at your work: after building, run
  `python3 tools/screenshot.py <paths>` (desktop + mobile PNGs under /tmp/website_request_shots/) and
  Read the images. This is REQUIRED for every visual change: never report a design or motion change you
  have not looked at. For animation capture frames, e.g. `--frames 50,400,900,1600 --viewport-only` plus
  `--scroll-to "<css selector>"` for on-scroll effects or `--hover "<css selector>"` for hover, and check
  that the motion actually happens and ends in the right place. Fix anything that looks broken, cramped
  or off-brand on either size.
- Motion and interactivity are welcome when asked for. Keep them tasteful and on-brand: animate only
  transform/opacity (smooth, no layout shift), CSS keyframes/transitions in site/assets/css/site.css, and
  small vanilla JS in site/assets/js/site.js only when needed (e.g. IntersectionObserver for on-scroll
  reveals). No external libraries, CDNs or third-party embeds. Content must be fully visible without JS,
  and everything must stop under `@media (prefers-reduced-motion: reduce)`.
- Finally write {result} as JSON:
  {{"status": "changed" | "question" | "no_change",
    "summary": "<one or two plain-English sentences for the requester: what you changed and where>",
    "pages": ["/about/", ...],
    "question": "<only when status is question>"}}
  Plain language, no file paths or code in summary/question; the requester is not technical.
"""


def run_claude(req: dict, model: str | None = None, max_usd: str | None = None,
               fix_log: str | None = None, preview: bool = False) -> dict:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    RESULT.unlink(missing_ok=True)
    thread = "\n".join(req.get("context") or [])
    files = sorted(p.name for p in FILES_DIR.iterdir())
    prompt = (
        f"Request from {req['requester']} (via {'email' if '(email)' in req['requester'] else 'Slack'}):\n"
        f"<<<\n{req['text']}\n>>>\n"
        + (f"\nEarlier messages in this Slack thread (oldest first):\n<<<\n{thread}\n>>>\n" if thread else "")
        + (f"\nAttached files in {FILES_DIR}: {', '.join(files)}\n" if files else "")
        + "\nDo what the request asks, following the rules."
        + ("\nThis change will be shown to the requester as a PREVIEW (a separate preview copy of the site + "
           "screenshots); nothing goes live until they approve it. So just make the change; never say you "
           "can't do a preview or mockup." if preview else "")
    )
    if fix_log:
        prompt += ("\n\nYour change for this request is already in the working tree, but it FAILED the site's "
                   "quality checks:\n<<<\n" + fix_log + "\n>>>\nFix those problems while keeping what was "
                   "asked for, run `python3 tools/build.py` until it passes, then write the result JSON again.")
    env = {k: os.environ[k] for k in ("PATH", "HOME", "ANTHROPIC_API_KEY", "LANG", "PLAYWRIGHT_BROWSERS_PATH")
           if k in os.environ}
    if os.environ.get("ANTHROPIC_WORKSPACE_ID"):   # org-level keys must name the workspace to bill
        env["ANTHROPIC_CUSTOM_HEADERS"] = f"anthropic-workspace-id: {os.environ['ANTHROPIC_WORKSPACE_ID']}"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    allowed = ",".join([
        "Read", "Edit", "Write", "Glob", "Grep",
        "Bash(python3 tools/build.py)", "Bash(python3 tools/build.py:*)",
        "Bash(python3 tools/img_for_web.py:*)", "Bash(python3 tools/screenshot.py:*)", "Bash(ls:*)",
    ])
    cmd = ["claude", "-p", prompt,
           "--append-system-prompt", CLAUDE_RULES.format(files_dir=FILES_DIR, result=RESULT),
           "--allowedTools", allowed,
           "--disallowedTools", "WebFetch,WebSearch,Bash(git:*),Bash(curl:*)",
           "--permission-mode", "dontAsk",
           "--max-budget-usd", max_usd or budget("standard"),
           "--no-session-persistence",
           "--output-format", "json",
           "--model", model or choose_model(req)[0]]
    r = sh(*cmd, check=False, env=env, timeout=30 * 60)
    meta = {}
    try:
        meta = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        pass
    result = {}
    if RESULT.exists():
        try:
            result = json.loads(RESULT.read_text())
        except Exception:
            result = {}
    result.setdefault("status", "changed" if changed_files() else "no_change")
    result.setdefault("summary", (meta.get("result") or "").strip()[:600])
    result["cost_usd"] = meta.get("total_cost_usd")
    result["claude_exit"] = r.returncode
    if (meta.get("is_error") or r.returncode != 0) and not RESULT.exists():
        result["status"] = "error"     # Claude itself failed (API key, credits, outage...): not a question
        result["error"] = (meta.get("result") or r.stdout or "")[-500:].strip()
    return result


LIVE_UA = "Mozilla/5.0 (compatible; AscendPointAI-website-robot/1.0; +https://ascendpoint.agency)"


def same_commit(version_txt: str, sha: str) -> bool:
    """version.txt holds a (usually short) commit id, maybe followed by a date; sha is a full id."""
    v, sha = (version_txt.split() or [""])[0].strip().lower(), sha.strip().lower()
    return min(len(v), len(sha)) >= 6 and (sha.startswith(v) or v.startswith(sha))


def wait_live(sha: str, live_url: str, timeout: int = 9 * 60, poll: float = 5, opener=None,
              sleep=time.sleep, clock=time.time) -> bool:
    """True once {live_url}/version.txt shows `sha`. The host blocks the default Python
    user agent (403), so send a browser-like one, and bust the edge cache on every poll."""
    opener = opener or urllib.request.urlopen
    end = clock() + timeout
    while clock() < end:
        url = f"{live_url}/version.txt?v={int(clock() * 1000)}"
        req = urllib.request.Request(url, headers={"User-Agent": LIVE_UA, "Cache-Control": "no-cache"})
        try:
            with opener(req, timeout=20) as r:
                if same_commit(r.read().decode(), sha):
                    return True
        except Exception as e:
            print("live check:", e, file=sys.stderr, flush=True)
        sleep(poll)
    return False


def sync_main():
    """Start every request from the latest main (another run, or a person, may have pushed since)."""
    sh("git", "fetch", "-q", "origin", "main", check=False)
    sh("git", "merge", "-q", "--ff-only", "origin/main", check=False)


def push_main():
    """Push HEAD to main, rebasing onto anything pushed in the meantime (up to 3 tries)."""
    for _ in range(3):
        if sh("git", "push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            return
        sh("git", "pull", "-q", "--rebase", "origin", "main")
    raise RuntimeError("push to main failed")


def commit_and_ship(req: dict, summary: str, extra_trailers: str = "") -> str:
    msg = (f"Website request: {summary[:68]}\n\n{summary}\n\n"
           f"Requested-by: {req['requester']}\nSlack-Thread: {req['thread_ts']}\nSlack-Message: {req['ts']}\n"
           f"{extra_trailers}")
    sh("git", "add", "-A", "site")
    sh("git", "-c", "user.name=AscendPoint AI", "-c", "user.email=website-bot@ascendpoint.agency",
       "commit", "-q", "-m", msg)
    push_main()
    sha = sh("git", "rev-parse", "HEAD").stdout.strip()
    sh("gh", "workflow", "run", "deploy.yml", "--ref", "main")
    return sha


def thread_commits(thread_ts: str) -> list[str]:
    out = sh("git", "log", "--format=%H", "--fixed-strings", f"--grep=Slack-Thread: {thread_ts}", "HEAD").stdout.split()
    bodies = sh("git", "log", "--format=%b", "--grep=This reverts commit", "HEAD").stdout
    reverted = set(re.findall(r"This reverts commit ([0-9a-f]{40})", bodies))
    return [c for c in out if c not in reverted]


def links(pages: list[str], live: str) -> str:
    return "\n".join(f"• {live}{p}" for p in pages[:8])


def drop_outside_site() -> list[str]:
    """Claude may only touch site/; silently drop anything else. Returns the remaining changed files."""
    files = changed_files()
    outside = [f for f in files if not f.startswith("site/")]
    for f in outside:
        sh("git", "checkout", "-q", "HEAD", "--", f, check=False)
        sh("git", "clean", "-fdq", "--", f, check=False)
    return changed_files() if outside else files


def wants_preview(req: dict) -> bool:
    """The requester asked to see it before it goes live ("preview first", "send me a mockup", ...)."""
    return bool(PREVIEW_RE.search(req.get("text") or ""))


def preview_branch(thread_ts: str) -> str:
    return PREVIEW_PREFIX + thread_ts


def preview_pending(thread_ts: str) -> bool:
    """A preview for this thread is waiting for approval (its branch exists on origin)."""
    return sh("git", "ls-remote", "--exit-code", "--heads", "origin", preview_branch(thread_ts),
              check=False).returncode == 0


def start_preview_tree(thread_ts: str, pending: bool):
    """Work on the thread's preview branch (or a fresh one from main): nothing here touches main."""
    sh("git", "fetch", "-q", "origin", "main")
    if pending:
        sh("git", "fetch", "-q", "origin", f"{preview_branch(thread_ts)}")
        sh("git", "checkout", "-q", "-B", "preview-work", "FETCH_HEAD")
    else:
        sh("git", "checkout", "-q", "-B", "preview-work", "origin/main")


def back_to_main():
    reset_worktree()
    sh("git", "checkout", "-q", "-f", "main", check=False)
    sh("git", "reset", "-q", "--hard", "origin/main", check=False)


def publish_preview(sha: str) -> str | None:
    """Rebuild with the preview commit and push the built site, as a single orphan commit, to the
    PREVIEW_BRANCH branch (the Kinsta preview site deploys it). Returns the preview base URL, or
    None when PREVIEW_URL isn't configured (the reply then relies on the screenshots)."""
    url = (os.environ.get("PREVIEW_URL") or "").rstrip("/")
    sh("python3", "tools/build.py", "--env", "staging", "--base-url", url or "https://preview.invalid")
    dist = ROOT / "dist"
    shots = Path("/tmp/website_request_preview_shots")
    if shots.exists():                       # screenshots ride along, so links work without Slack uploads
        (dist / "_preview").mkdir(exist_ok=True)
        for p in shots.glob("*.png"):
            shutil.copy2(p, dist / "_preview" / p.name)
    index = Path(tempfile.mkdtemp()) / "index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    sh("git", f"--work-tree={dist}", "add", "-A", "-f", ".", env=env)
    tree = sh("git", "write-tree", env=env).stdout.strip()
    commit = sh("git", *GIT_USER, "commit-tree", tree, "-m", f"Preview build of {sha[:7]}").stdout.strip()
    sh("git", "push", "-q", "-f", "origin", f"{commit}:refs/heads/{PREVIEW_BRANCH}")
    shutil.rmtree(index.parent, ignore_errors=True)
    return url or None


def take_screenshots(pages: list[str]) -> list[Path]:
    """Desktop + mobile shots of up to 3 changed pages, from the local build."""
    out = Path("/tmp/website_request_preview_shots")
    shutil.rmtree(out, ignore_errors=True)
    r = sh("python3", "tools/screenshot.py", *(pages[:3] or ["/"]), "--out", str(out), check=False, timeout=300)
    if r.returncode != 0:
        print("screenshots failed:", r.stdout[-500:], file=sys.stderr, flush=True)
        return []
    return sorted(out.glob("*.png"))


def upload_shots(slack: Slack, req: dict, shots: list[Path]) -> bool:
    """Attach the screenshots in the thread. Needs the Slack app's files:write scope; returns False
    (and the reply links the copies on the preview site instead) when the app doesn't have it."""
    if not shots:
        return False
    try:
        ids = []
        for p in shots:
            up = slack.call("files.getUploadURLExternal", filename=p.name, length=str(p.stat().st_size))
            slack.upload(up["upload_url"], p)
            ids.append({"id": up["file_id"], "title": p.stem.replace("-", " ")})
        slack.call("files.completeUploadExternal", files=json.dumps(ids), channel_id=req["channel"],
                   thread_ts=req["thread_ts"], initial_comment="Preview screenshots (desktop + mobile):")
        return True
    except Exception as e:
        print("screenshot upload skipped:", e, file=sys.stderr, flush=True)
        return False


def approve_preview(req: dict, live: str) -> tuple[str, str]:
    """Put the thread's previewed change live, exactly as previewed."""
    branch = preview_branch(req["thread_ts"])
    sh("git", "checkout", "-q", "-f", "main", check=False)
    sh("git", "fetch", "-q", "origin", "main", branch)
    sh("git", "reset", "-q", "--hard", "origin/main")
    head = sh("git", "rev-parse", f"origin/{branch}", check=False).stdout.strip() or \
        sh("git", "ls-remote", "origin", branch).stdout.split()[0]
    base = sh("git", "merge-base", "origin/main", head).stdout.strip()
    commits = sh("git", "rev-list", "--reverse", f"{base}..{head}").stdout.split()
    if not commits:
        return "speech_balloon", "There's no previewed change in this thread to put live."
    for c in commits:
        if sh("git", *GIT_USER, "cherry-pick", "-x", c, check=False).returncode != 0:
            sh("git", "cherry-pick", "--abort", check=False)
            reset_worktree()
            return "warning", ("⚠️ The live site changed since this preview was made and the two clash, so I left "
                               "the site as it is. Reply with the request again and I'll redo it on the current site.")
    ok, log = checks()
    if not ok:
        back_to_main()
        return "warning", ("⚠️ The previewed change no longer passes the site's checks, so nothing went live. "
                           f"<@{os.environ.get('WEBSITE_REQUESTS_OWNER', '')}> can take a look.")
    push_main()
    sha = sh("git", "rev-parse", "HEAD").stdout.strip()
    sh("gh", "workflow", "run", "deploy.yml", "--ref", "main")
    sh("git", "push", "-q", "origin", "--delete", branch, check=False)
    live_ok = wait_live(sha, live)
    files = sh("git", "diff", "--name-only", f"{base}..HEAD").stdout.split()
    pages = sorted({u for u in map(url_for, files) if u})
    note = "" if live_ok else "\n_(Publishing is taking longer than usual; it should show within a few minutes.)_"
    return "white_check_mark", (f"✅ Live: the previewed change is on the site now.\n{links(pages, live)}{note}\n"
                                "_Reply *undo* here to roll it back._")


def cancel_preview(req: dict) -> tuple[str, str]:
    sh("git", "push", "-q", "origin", "--delete", preview_branch(req["thread_ts"]), check=False)
    return "wastebasket", "🗑️ Dropped the preview. Nothing went live."


def handle(req: dict, slack: Slack, live: str) -> tuple[str, str]:
    """Returns (reaction, reply text)."""
    if req["kind"] == "blocked":
        return "no_entry_sign", ("🚫 I only act on emailed website requests from team addresses "
                                 f"({', '.join(sorted(email_domains()))}). Nothing was changed. "
                                 "Anyone on the team can post the request here instead.")
    sync_main()
    pending = req["kind"] in ("followup", "undo") and preview_pending(req["thread_ts"])
    if pending:
        text = req.get("text") or ""
        if APPROVE_RE.match(text):
            return approve_preview(req, live)
        if CANCEL_RE.match(text) or (req["kind"] == "undo" and not thread_commits(req["thread_ts"])):
            return cancel_preview(req)
    if req["kind"] == "undo":
        commits = thread_commits(req["thread_ts"])
        if not commits:
            return "speech_balloon", "There's nothing from this thread live to undo."
        for c in commits:  # newest first
            sh("git", *GIT_USER, "revert", "--no-edit", c)
        ok, log = checks()
        if not ok:
            reset_worktree()
            return "warning", "I couldn't undo this cleanly, so I left the site as it is. Kyle has been flagged."
        push_main()
        sha = sh("git", "rev-parse", "HEAD").stdout.strip()
        sh("gh", "workflow", "run", "deploy.yml", "--ref", "main")
        live_ok = wait_live(sha, live)
        return ("leftwards_arrow_with_hook",
                "↩️ Undone. The site is back to how it was before this thread's change."
                + ("" if live_ok else " (Publishing is taking longer than usual; it should show within a few minutes.)"))

    preview = pending or wants_preview(req)
    if preview:
        start_preview_tree(req["thread_ts"], pending)
    try:
        return make_change(req, slack, live, preview)
    finally:
        if preview:
            back_to_main()


def make_change(req: dict, slack: Slack, live: str, preview: bool) -> tuple[str, str]:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(req.get("files", [])):
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", f.get("name") or f"file-{i}")
        slack.download(f["url"], FILES_DIR / name)

    model, tier = choose_model(req)
    result = run_claude(req, model, budget(tier), preview=preview)
    spent = [result.get("cost_usd")]
    files = drop_outside_site()
    if tier == "simple" and result.get("status") != "error" and (not files or result.get("status") == "no_change"):
        # the cheap model didn't manage it: try once more with the everyday model
        reset_worktree()
        model, tier = os.environ.get("CLAUDE_MODEL") or "sonnet", "standard"
        result = run_claude(req, model, budget(tier), preview=preview)
        spent.append(result.get("cost_usd"))
        files = drop_outside_site()

    if result.get("status") == "error":
        reset_worktree()
        owner = os.environ.get("WEBSITE_REQUESTS_OWNER", "")
        return "warning", ("⚠️ I couldn't reach Claude, so nothing was changed. "
                           f"{f'<@{owner}> ' if owner else ''}has been flagged; once it's fixed, "
                           f"reply *retry* here.\n`{result.get('error', '')[:300]}`")

    if result.get("status") == "question" or not files:
        reset_worktree()
        q = result.get("question") or result.get("summary") or "I wasn't sure what to change. Can you say a bit more?"
        return "speech_balloon", f"💬 {q}\n_Reply in this thread and I'll pick it up._"

    ok, log = checks()
    fixed_by = None
    if not ok:  # one automatic repair pass with the advanced model, given the exact check failures
        fix_model = os.environ.get("CLAUDE_MODEL_ADVANCED") or "opus"
        fix = run_claude(req, fix_model, budget("advanced"), fix_log="\n".join(log.strip().splitlines()[-40:]),
                         preview=preview)
        spent.append(fix.get("cost_usd"))
        files = drop_outside_site()
        if fix.get("status") != "error" and files:
            ok, log = checks()
            if ok:
                fixed_by = fix_model
                result["summary"] = fix.get("summary") or result.get("summary")
                result["pages"] = fix.get("pages") or result.get("pages")
    if not ok:
        reset_worktree()
        tail = "\n".join(log.strip().splitlines()[-6:])
        return "warning", ("⚠️ I made the change but it failed the site's quality checks, even after an automatic "
                           "fix attempt, so nothing went live. "
                           f"<@{os.environ.get('WEBSITE_REQUESTS_OWNER', '')}> can take a look.\n```{tail[:900]}```")

    summary = result.get("summary") or "Updated the website."
    pages = result.get("pages") or sorted({u for u in map(url_for, files) if u})
    cost = sum(c for c in spent if isinstance(c, (int, float)))
    footer = model_name(model) + {"advanced": " (advanced request)", "simple": " (simple request)"}.get(tier, "")
    if fixed_by:
        footer += f" · {model_name(fixed_by)} auto-fixed a failing check"
    if any(isinstance(c, (int, float)) for c in spent):
        footer += f" · ${cost:.2f}"

    if preview:
        sha = commit_preview(req, summary)
        shots = take_screenshots(pages)
        url = publish_preview(sha)
        preview_ok = wait_live(sha, url, timeout=6 * 60) if url else False
        uploaded = upload_shots(slack, req, shots)
        lines = [f"🔍 Preview ready (NOT live yet): {summary}"]
        if url:
            lines.append(links(pages, url) + ("" if preview_ok else
                         "\n_(The preview site is still publishing; give it a minute if a link shows the old page.)_"))
        if shots and not uploaded:
            repo = os.environ.get("GITHUB_REPOSITORY")
            base = (f"{url}/_preview" if url else
                    f"https://github.com/{repo}/blob/{PREVIEW_BRANCH}/_preview" if repo else None)
            if base:
                lines.append("Screenshots: " + " · ".join(f"<{base}/{p.name}|{p.stem}>" for p in shots))
        elif not shots and not url:
            lines.append("_(I couldn't make screenshots this time; reply *approve* to see it live, or ask again.)_")
        lines.append("_Reply *approve* to put this live, reply with changes to update the preview, "
                     "or *cancel* to drop it._")
        lines.append(f"_{footer}_")
        return "mag", "\n".join(lines)

    sha = commit_and_ship(req, summary)
    live_ok = wait_live(sha, live)
    note = "" if live_ok else "\n_(Publishing is taking longer than usual; it should show within a few minutes.)_"
    return "white_check_mark", (f"✅ Live: {summary}\n{links(pages, live)}{note}\n"
                                f"_Reply *undo* here to roll it back, or reply with tweaks._\n_{footer}_")


def commit_preview(req: dict, summary: str) -> str:
    """Commit on the thread's preview branch (never main) and push it."""
    msg = (f"Website request: {summary[:68]}\n\n{summary}\n\n"
           f"Requested-by: {req['requester']}\nSlack-Thread: {req['thread_ts']}\nSlack-Message: {req['ts']}\n")
    sh("git", "add", "-A", "--", SITE_DIR)
    sh("git", *GIT_USER, "commit", "-q", "-m", msg)
    sh("git", "push", "-q", "-f", "origin", f"HEAD:refs/heads/{preview_branch(req['thread_ts'])}")
    return sh("git", "rev-parse", "HEAD").stdout.strip()


# ----------------------------------------------------------------------------- CLI
def claim(slack: Slack, channel: str, req: dict):
    """Mark a request as taken (👀) so no other run picks it up, and tell the requester."""
    slack.call("reactions.add", channel=channel, timestamp=req["ts"], name="eyes")
    if req["kind"] != "blocked":
        model, tier = choose_model(req)
        text = req.get("text") or ""
        if req["kind"] == "followup" and (APPROVE_RE.match(text) or CANCEL_RE.match(text)):
            return                                     # approve / cancel: the outcome reply is enough
        later = ("I'll reply here with a preview link and screenshots (nothing goes live until you approve)"
                 if wants_preview(req) else "I'll reply here when it's live")
        text = (f"👀 On it. {later} (usually 2 to 4 minutes)." if tier != "advanced" or req["kind"] == "undo" else
                f"👀 On it. This is a design/advanced change, so I'm using {model_name(model)} and checking it "
                f"visually on desktop and mobile. {later} (usually 5 to 15 minutes).")
        slack.call("chat.postMessage", channel=channel, thread_ts=req["thread_ts"], text=text)


def finish(slack: Slack, req: dict, live: str) -> str:
    """Do the request, reply in the thread, swap 👀 for the outcome reaction."""
    try:
        reaction, text = handle(req, slack, live)
    except Exception as e:  # never leave a request silently hanging
        reset_worktree()
        owner = os.environ.get("WEBSITE_REQUESTS_OWNER", "")
        reaction, text = "warning", (f"⚠️ Something went wrong and nothing was changed. "
                                     f"{f'<@{owner}> ' if owner else ''}has been flagged.\n`{str(e)[:300]}`")
        print("ERROR", e, file=sys.stderr)
    slack.call("chat.postMessage", channel=req["channel"], thread_ts=req["thread_ts"], text=text,
               unfurl_links="false")
    slack.call("reactions.remove", channel=req["channel"], timestamp=req["ts"], name="eyes")
    slack.call("reactions.add", channel=req["channel"], timestamp=req["ts"], name=reaction)
    print(reaction, text, flush=True)
    return reaction


def listen(slack: Slack, channel: str, live: str, minutes: float, poll: float,
           start: float = 0.0, allowed: set[str] | None = None, sleep=time.sleep, clock=time.time) -> int:
    """Near-instant mode: poll the channel every `poll` seconds for `minutes`, handling requests
    one at a time as they arrive. The workflow restarts it so one listener is always running."""
    end = float(os.environ.get("LISTEN_UNTIL") or 0) or clock() + minutes * 60
    me = Path(__file__).resolve()
    my_code = me.read_bytes()
    last_pull = clock()
    cache: dict = {}
    seen: set[str] = set()
    handled = 0
    while clock() < end:
        try:
            req = find_next(slack, channel, start=start, allowed=allowed, cache=cache)
        except Exception as e:      # Slack hiccup: wait and try again
            print("poll error", e, file=sys.stderr, flush=True)
            req = None
        if req and req["ts"] in seen:   # Slack hasn't caught up with our reaction yet
            req = None
        if req:
            seen.add(req["ts"])
            print(f"picked {req['kind']} request {req['ts']} from {req['requester']}", flush=True)
            claim(slack, channel, req)
            finish(slack, req, live)
            handled += 1
            last_pull = 0                       # pull now: the change just shipped
        if clock() - last_pull > 300 or not last_pull:
            sh("git", "pull", "-q", "--ff-only", "origin", "main", check=False)   # stay current
            last_pull = clock()
            if me.exists() and me.read_bytes() != my_code:   # robot code changed on main: restart into it
                print("robot code updated on main; restarting listener", flush=True)
                os.environ["LISTEN_UNTIL"] = str(end)
                os.execv(sys.executable, [sys.executable, str(me), "listen"])
        if req:
            continue
        sleep(poll)
    print(f"listener done: {handled} request(s) handled", flush=True)
    return 0


def main(argv: list[str]) -> int:
    token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_CHANNEL_ID")
    if not token or not channel or not os.environ.get("ANTHROPIC_API_KEY"):
        print("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID / ANTHROPIC_API_KEY not configured; nothing to do")
        return 0
    slack = Slack(token)
    live = (os.environ.get("LIVE_URL") or "https://ascendpoint.agency").rstrip("/")

    if argv[:1] == ["next"]:
        allowed = {u.strip() for u in os.environ.get("WEBSITE_REQUESTS_ALLOWED", "").split(",") if u.strip()}
        start = float(os.environ.get("WEBSITE_REQUESTS_START") or 0)
        req = find_next(slack, channel, start=start, allowed=allowed or None)
        out = Path(argv[1] if len(argv) > 1 else "request.json")
        if not req:
            print("no pending requests")
            out.unlink(missing_ok=True)
            return 0
        claim(slack, channel, req)
        out.write_text(json.dumps(req, indent=2))
        print(f"picked {req['kind']} request {req['ts']} from {req['requester']}")
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a") as f:
                f.write("found=true\n")
        return 0

    if argv[:1] == ["run"]:
        req = json.loads(Path(argv[1]).read_text())
        return 0 if finish(slack, req, live) != "warning" else 1

    if argv[:1] == ["listen"]:
        allowed = {u.strip() for u in os.environ.get("WEBSITE_REQUESTS_ALLOWED", "").split(",") if u.strip()}
        return listen(slack, channel, live,
                      minutes=float(os.environ.get("LISTEN_MINUTES") or 340),
                      poll=float(os.environ.get("POLL_SECONDS") or 10),
                      start=float(os.environ.get("WEBSITE_REQUESTS_START") or 0),
                      allowed=allowed or None)

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
