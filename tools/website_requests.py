#!/usr/bin/env python3
"""AscendPoint AI website robot: turns Slack requests into live site changes.

Runs in GitHub Actions (.github/workflows/website-requests.yml). A listener job is always running
(restarted every 15 minutes if it isn't), so a request is picked up within ~10 seconds:

    python3 tools/website_requests.py listen   # near-instant: poll every 10 s for ~5.7 h (default)
    python3 tools/website_requests.py next     # one-shot: find the oldest unhandled request -> request.json
    python3 tools/website_requests.py run request.json

Front door: a message in #ascendpoint-website-requests (top-level = new request; reply in the thread =
follow-up). Email requests were switched off on Oct 4: an email that still lands in the channel (Slack
email file, or a "📧 Email request from ..." post) is never acted on; it gets a 🚫 "post it here" reply.

For each request the robot:
  1. reacts 👀 and replies "On it" in the thread,
  2. "undo" in a thread  -> git-reverts the commits that thread produced,
     anything else       -> Claude Code (headless) edits site/ with a tight tool allowlist,
  3. runs every site check (tools/build.py + tools/test_redirects.py); nothing ships if they fail,
  4. commits to main ("AscendPoint AI"), triggers the deploy workflow, waits until
     https://ascendpoint.agency/version.txt shows the new commit,
  5. replies in the thread with what changed + links, and reacts ✅ (or 💬 question / ⚠️ failed).

Preview first ("preview", "send me a mockup", "show me before it goes live", ...): steps 4-5 become
  4. commit on branch preview/<thread ts> (never main),
     publish a staging build to branch kinsta-preview (Kinsta preview site PREVIEW_URL, noindex, no analytics),
  5. reply with the preview link(s) and react 🔍. In that thread: "approve" (ship it, looks good,
     go live...) puts exactly that change live; any other reply updates the preview; "cancel" drops it 🗑️.

Model per request: Haiku for short text-only edits, Sonnet for everyday edits, Opus for design/motion/new
pages (see choose_model); Haiku falls back to Sonnet, and Opus repairs anything that fails the checks.

Attachments (photos, logos, screenshots) are downloaded into .request-files/ (git-ignored), checked and
described to Claude; follow-ups also get the photos posted earlier in the thread. tools/img_for_web.py swaps
or adds images (new file names so caches never show the old picture; face-aware crops for headshots).

State lives in Slack reactions: a message the bot has reacted to is never picked up again.

Environment: SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, ANTHROPIC_API_KEY, GITHUB_TOKEN (Actions),
optional WEBSITE_REQUESTS_START (unix ts; ignore older messages), WEBSITE_REQUESTS_ALLOWED
(comma-separated Slack user IDs; empty = anyone in the channel)
(comma-separated sender domains for email requests; default below), LIVE_URL, CLAUDE_MODEL(_SIMPLE/_ADVANCED),
CLAUDE_MAX_USD(_SIMPLE/_ADVANCED), PREVIEW_URL (the preview site; without it the reply says there's no link yet).
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
# "I don't like this at all, let's keep the site as it is": a cancel unless it also asks for a different change
CANCEL_ANY_RE = re.compile(
    r"\b(keep (the )?(site|website|page|it|everything|things) (as it is|as is|the same|how it is|unchanged)|"
    r"leave (it|the site|the page|everything) (as it is|as is|alone|the same)|"
    r"don'?t (like|want) (this|it|that|these)( (edit|change|one|version))?( at all)?|"
    r"(don'?t|do not|no need to) (make|do|publish|ship|push|go live with) (it|this|that|the change)|"
    r"never ?mind|forget (it|this|that|about it)|no thanks|scrap (it|this|that)|drop (it|this|that)|"
    r"cancel (it|this|that|the preview))\b", re.I)
TWEAK_RE = re.compile(r"\b(instead|rather|but (make|change|try|use)|make it|change it|try|could you|can you|"
                      r"how about|what about|maybe|more|less|bigger|smaller|darker|lighter)\b", re.I)


def wants_cancel(text: str) -> bool:
    """Drop the pending preview? "cancel", or plainly saying to keep the site as it is (and nothing else)."""
    text = text or ""
    return bool(CANCEL_RE.match(text) or (CANCEL_ANY_RE.search(text) and not TWEAK_RE.search(text)))
PREVIEW_PREFIX = "preview/"      # source of a pending preview: branch preview/<thread ts>
PREVIEW_BRANCH = "kinsta-preview"  # built preview site (Kinsta static site PREVIEW_URL deploys this branch)
SITE_DIR = "site/"               # the only place the robot may change anything
GIT_USER = ("-c", "user.name=AscendPoint AI", "-c", "user.email=website-bot@ascendpoint.agency")
IGNORE_RE = re.compile(r"^\s*(//|note:|fyi\b)", re.I)
EMAIL_PREFIX = "📧"
RESULT = Path("/tmp/website_request_result.json")
# Files attached in Slack (photos, logos, screenshots) are downloaded INSIDE the repo (git-ignored) so the
# headless Claude can open them: tools outside its working folder are refused in dontAsk mode.
FILES_DIR = ROOT / ".request-files"
SHOTS_DIR = Path("/tmp/website_request_shots")     # tools/screenshot.py output
MAX_FILES = 10
MAX_FILE_BYTES = 60 * 1024 * 1024
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

    def download(self, url: str, dest: Path):
        """Save a Slack-hosted file. Slack answers a token without files:read (or an expired link) with its
        sign-in WEB PAGE and status 200, so check we really got a file, not HTML."""
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        with self.opener(req, timeout=120) as r:
            ctype = (getattr(r, "headers", None) or {}).get("Content-Type", "") or ""
            data = r.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise FileError(f"{dest.name} is over {MAX_FILE_BYTES // 2**20} MB")
        head = data[:300].lstrip().lower()
        if "text/html" in ctype.lower() or head.startswith((b"<!doctype html", b"<html")):
            raise FileError(f"Slack sent a sign-in page instead of {dest.name} (the Slack app needs the "
                            "files:read permission)")
        if not data:
            raise FileError(f"{dest.name} came through empty")
        dest.write_bytes(data)

    def file_url(self, f: dict) -> str | None:
        """Download URL of a file in a message. Files shared from elsewhere sometimes come without one
        ("file_access": "check_file_info"): ask Slack for it."""
        url = f.get("url_private_download") or f.get("url_private") or f.get("url")
        if url or not f.get("id"):
            return url
        info = self.call("files.info", file=f["id"]).get("file") or {}
        return info.get("url_private_download") or info.get("url_private")


class FileError(RuntimeError):
    """An attachment couldn't be fetched from Slack."""


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


def message_files(msg: dict, where: str) -> list[dict]:
    """The files people attached to a Slack message (not an email Slack turned into a file)."""
    out = []
    for f in msg.get("files") or []:
        if email_file({"files": [f]}) or f.get("mode") in ("tombstone", "external") or f.get("is_external"):
            continue
        if not (f.get("url_private") or f.get("url_private_download") or f.get("id")):
            continue
        out.append({"id": f.get("id"), "name": f.get("name") or f.get("title"), "mimetype": f.get("mimetype"),
                    "url": f.get("url_private_download") or f.get("url_private"), "where": where})
    return out


def build_request(slack: Slack, channel: str, msg: dict, thread: list | None, bot_user: str) -> dict:
    thread_ts = msg.get("thread_ts") or msg["ts"]
    context = []
    for t in (thread or []):
        if t["ts"] == msg["ts"]:
            break
        who = BOT_NAME if t.get("user") == bot_user else user_name(slack, t.get("user"))
        context.append(f"{who}: {t.get('text', '')}")
    text = msg.get("text", "")
    files = message_files(msg, "this message")
    if thread_ts != msg["ts"]:
        # A follow-up ("retry", "use the photo above", "make it smaller") still needs the photos posted
        # earlier in the thread by people (not the bot), newest first.
        for t in reversed(thread or []):
            if float(t["ts"]) < float(msg["ts"]) and t.get("user") != bot_user and not t.get("bot_id"):
                files += message_files(t, "earlier in this thread")
    seen, unique = set(), []
    for f in files:
        key = f.get("id") or f.get("url")
        if key not in seen:
            seen.add(key)
            unique.append(f)
    files = unique[:MAX_FILES]
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
        kind = "blocked"      # email requests are switched off (Oct 4): changes only come from the Slack channel
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


TEST_ENV_DROP = ("SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "SEVALLA_API_KEY",
                 "SERPDENTAL_DISPATCH_TOKEN", "DISPATCH_TOKEN")


def checks() -> tuple[bool, str]:
    """Everything CI runs before a deploy: the robot's own tests too, so a change can never land on main
    and then sit there undeployed because CI refused it (tests run with every secret removed)."""
    log = []
    test_env = {k: v for k, v in os.environ.items() if k not in TEST_ENV_DROP}
    test_env["ROBOT_TESTS_RUNNING"] = "1"            # the tests call checks() too: don't recurse
    steps = [(["python3", "tools/build.py"], None), (["python3", "tools/test_redirects.py"], None)]
    if not os.environ.get("ROBOT_TESTS_RUNNING"):
        steps.append((["python3", "-m", "unittest", "discover", "-s", "tests", "-q"], test_env))
    for cmd, env in steps:
        r = sh(*cmd, check=False, env=env, timeout=900)
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
  separate preview copy of the site, sends the requester a link, and only puts it live when the requester approves.
- Make the smallest correct change that does exactly what was asked, matching the existing design,
  tone and HTML patterns. Keep titles <= 60 chars and descriptions 70-170 chars when you touch them.
- Never invent facts: no made-up numbers, prices, dates, quotes, names, credentials or claims. If the
  request needs information you don't have, or is ambiguous, risky (deleting pages, legal/privacy text,
  pricing, anything about clients) or not about this website, change NOTHING and ask one clear question.
- Only edit files under site/. Never touch .github/, tools/, README.md or git. Do not run git.
- Photos and files attached in Slack are in {files_dir}/ (the request lists each one: size, whether a face was
  found, and a .view.jpg copy to look at when the original can't be opened). Look at them with Read. USE them:
  never ask for a file that is already attached, and never say you can't process images.
- SWAP a picture that is already on the site (a team headshot, a logo, a photo): find the <img> on that page,
  then run `python3 tools/img_for_web.py {files_dir}/<file> --replace site/img/<file the page uses now>`.
  It matches the old image's size and shape, frames people on their face the way the old photo was,
  saves it under a NEW file name and updates every page that uses it. Never overwrite an image file in place
  and don't edit those references yourself (visitors' browsers and the host cache images for 30 days, so
  the old picture would keep showing).
- ADD a new picture: `python3 tools/img_for_web.py {files_dir}/<file> site/img/<descriptive-name>.webp`
  with `--width <px>`, or `--size WxH` for an exact size (cropped around the face / centre). A new team
  member's headshot matches the others: `--size 320x320 --headshot`, named hs-<firstname>-sq.webp. Then add
  the <img> following the existing markup, with width/height and real alt text (the person's name).
- `python3 tools/img_for_web.py --info <images>` shows size, orientation, where the face is and which pages
  use an image. After any image change, screenshot the page and LOOK: if a head is cut off or off-centre,
  redo it with `--focus X,Y` (0-1 shares of the photo's width/height).
- Screenshots of the site are reference for what to change, unless the request says to put the image up.
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


def files_rel() -> str:
    """FILES_DIR as Claude should write it: relative to the repo when it's inside it."""
    try:
        return FILES_DIR.relative_to(ROOT).as_posix()
    except ValueError:
        return str(FILES_DIR)


def denied_tools(meta: dict) -> list[str]:
    """Short descriptions of the tool calls Claude Code refused (its JSON result lists them)."""
    out = []
    for d in meta.get("permission_denials") or []:
        inp = d.get("tool_input") or {}
        what = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""
        out.append(f"{d.get('tool_name', '?')} {str(what)[:160]}".strip())
    return out


def prepare_files(req: dict, slack: Slack) -> list[str]:
    """Download the request's attachments into FILES_DIR (emptied first) and describe each one for Claude.
    Raises FileError when an attachment of THIS message can't be fetched (older thread files are optional)."""
    shutil.rmtree(FILES_DIR, ignore_errors=True)
    FILES_DIR.mkdir(parents=True)
    names: dict[str, str] = {}
    for i, f in enumerate(req.get("files") or []):
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", f.get("name") or f"file-{i + 1}").strip("-.") or f"file-{i + 1}"
        name, n = base, 2
        while name in names.values():
            stem, dot, ext = base.rpartition(".")
            name = f"{stem}-{n}.{ext}" if dot else f"{base}-{n}"
            n += 1
        try:
            url = slack.file_url(f)
            if not url:
                raise FileError(f"Slack didn't give a download link for {base}")
            slack.download(url, FILES_DIR / name)
        except Exception as e:
            if f.get("where") == "this message":
                raise e if isinstance(e, FileError) else FileError(f"couldn't download {base}: {e}")
            print(f"skipped earlier thread file {base}: {e}", file=sys.stderr, flush=True)
            continue
        names[name] = f.get("where") or "this message"
    if not names:
        return []
    facts = {}
    r = sh("python3", "tools/img_for_web.py", "--prepare", str(FILES_DIR), check=False)
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
            facts[d["file"]] = d
        except Exception:
            pass
    out = []
    for name, where in names.items():
        d = facts.get(name, {})
        if d.get("kind") == "image":
            desc = (f"{files_rel()}/{name}: photo/image {d['width']}x{d['height']} "
                    f"({'a face was found' if d.get('face') else 'no face found'})")
            if d.get("view"):
                desc += f"; look at {files_rel()}/{d['view']} (a viewable copy)"
        elif str(d.get("kind", "")).startswith("broken"):
            if where == "this message":
                raise FileError(f"{name}: {d['kind']}")
            (FILES_DIR / name).unlink(missing_ok=True)
            continue
        else:
            desc = f"{files_rel()}/{name}: {d.get('kind') or 'file'}"
        out.append(f"{desc} [posted {where}]")
    return out


def run_claude(req: dict, model: str | None = None, max_usd: str | None = None,
               fix_log: str | None = None, preview: bool = False) -> dict:
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULT.unlink(missing_ok=True)
    thread = "\n".join(req.get("context") or [])
    attached = req.get("attached") or []
    text = req["text"] if (req.get("text") or "").strip() else "(no words, just the attached file(s))"
    prompt = (
        f"Request from {req['requester']} (via {'email' if '(email)' in req['requester'] else 'Slack'}):\n"
        f"<<<\n{text}\n>>>\n"
        + (f"\nEarlier messages in this Slack thread (oldest first):\n<<<\n{thread}\n>>>\n" if thread else "")
        + (f"\nAttached files, in {files_rel()}/:\n" + "\n".join(f"- {a}" for a in attached) + "\n"
           if attached else "")
        + "\nDo what the request asks, following the rules."
        + ("\nThis change will be shown to the requester as a PREVIEW (a link to a separate preview copy of "
           "the site); nothing goes live until they approve it. So just make the change; never say you "
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
        "Bash(python3 tools/img_for_web.py:*)", "Bash(python3 ./tools/img_for_web.py:*)",
        "Bash(python3 tools/screenshot.py:*)", "Bash(python3 ./tools/screenshot.py:*)", "Bash(ls:*)",
    ])
    cmd = ["claude", "-p", prompt,
           "--append-system-prompt", CLAUDE_RULES.format(files_dir=files_rel(), result=RESULT),
           "--add-dir", str(FILES_DIR), "--add-dir", str(SHOTS_DIR),
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
    result["denied"] = denied_tools(meta)
    if result["denied"]:     # a blocked tool is a robot problem, not the requester's: log it for the owner
        print("claude was blocked from: " + " | ".join(result["denied"]), file=sys.stderr, flush=True)
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


def deploy_failed(sha: str) -> str | None:
    """The site didn't show `sha` in time: if the deploy workflow refused it (failed/cancelled), return
    that run's URL so the reply can say so instead of "taking longer than usual"."""
    r = sh("gh", "run", "list", "--workflow", "deploy.yml", "--commit", sha, "--limit", "5",
           "--json", "status,conclusion,url", check=False)
    try:
        runs = json.loads(r.stdout or "[]")
    except Exception:
        return None
    for run in runs:
        if run.get("status") == "completed" and run.get("conclusion") in ("failure", "cancelled", "timed_out",
                                                                          "startup_failure"):
            return run.get("url") or "the deploy run"
    return None


def not_live_note(sha: str) -> tuple[bool, str]:
    """(deploy failed?, note for the reply) when the live check timed out."""
    url = deploy_failed(sha)
    if url:
        owner = os.environ.get("WEBSITE_REQUESTS_OWNER", "")
        return True, (f"\n⚠️ _Saved, but publishing failed, so the live site doesn't show it yet. "
                      f"{f'<@{owner}> ' if owner else ''}has been flagged: {url}_")
    return False, "\n_(Publishing is taking longer than usual; it should show within a few minutes.)_"


def links(pages: list[str], live: str, version: str = "") -> str:
    """Page links for a reply. `version` (a short commit id) is added as ?v=..., so each link is a URL no
    browser or CDN has cached before: the page opens fresh without clearing the cache."""
    q = lambda p: (("&" if "?" in p else "?") + f"v={version}") if version else ""
    return "\n".join(f"• {live}{p}{q(p)}" for p in pages[:8])


def drop_outside_site() -> list[str]:
    """Claude may only touch site/; silently drop anything else. Returns the remaining changed files."""
    files = changed_files()
    outside = [f for f in files if not f.startswith("site/")]
    for f in outside:
        sh("git", "checkout", "-q", "HEAD", "--", f, check=False)
        sh("git", "clean", "-fdq", "--", f, check=False)
    return changed_files() if outside else files


def wants_preview(req: dict) -> bool:
    """The requester asked to see it before it goes live ("preview first", "send me a mockup", ...).

    A follow-up counts too when the thread's opening request asked for a preview and nothing from the
    thread is live yet ("Can we remove X? Send me a mockup first" ... "remove this bit here")."""
    if PREVIEW_RE.search(req.get("text") or ""):
        return True
    ctx = req.get("context") or []
    if req.get("kind") == "followup" and ctx:
        opener = ctx[0].split(": ", 1)[-1]            # context lines are "Name: text"; [0] is the thread's opener
        if PREVIEW_RE.search(opener):
            try:
                return not thread_commits(req["thread_ts"])
            except Exception:
                return True                              # can't tell: the safe choice is not to go live
    return False


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


def no_cache(headers: Path):
    """Preview site only: every response is `Cache-Control: no-store`, so neither the browser nor the
    host's CDN keeps an old preview (the live site's 30-day edge caching would otherwise show a stale page)."""
    text = headers.read_text() if headers.exists() else ""
    out, block = [], []
    def flush():
        if any(l.strip() and l[0].isspace() for l in block[1:]):   # keep a path only if it still has headers
            out.extend(block)
    for line in text.splitlines():
        if re.match(r"\s+Cache-Control:", line, re.I):
            continue
        if line and not line[0].isspace() and not line.startswith("#"):
            flush()
            block = [line]
        elif block:
            block.append(line)
        else:
            out.append(line)
    flush()
    body = "\n".join(l for l in out if l.strip())
    headers.write_text("/*\n  Cache-Control: no-store, max-age=0\n" + (body + "\n" if body else ""))


def publish_preview(sha: str) -> str | None:
    """Rebuild with the preview commit and push the built site, as a single orphan commit, to the
    PREVIEW_BRANCH branch (the Kinsta preview site deploys it). Returns the preview base URL, or
    None when PREVIEW_URL isn't configured (the reply then says there's no link yet)."""
    url = (os.environ.get("PREVIEW_URL") or "").rstrip("/")
    # build.py stamps version.txt with $GITHUB_SHA when set, which inside the listener is the commit the
    # listener started from, not this preview: override it so the wait below can see the preview land.
    sh("python3", "tools/build.py", "--env", "staging", "--base-url", url or "https://preview.invalid",
       env=dict(os.environ, GITHUB_SHA=sha))
    dist = ROOT / "dist"
    no_cache(dist / "_headers")
    index = Path(tempfile.mkdtemp()) / "index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    sh("git", f"--work-tree={dist}", "add", "-A", "-f", ".", env=env)
    tree = sh("git", "write-tree", env=env).stdout.strip()
    commit = sh("git", *GIT_USER, "commit-tree", tree, "-m", f"Preview build of {sha[:7]}").stdout.strip()
    sh("git", "push", "-q", "-f", "origin", f"{commit}:refs/heads/{PREVIEW_BRANCH}")
    shutil.rmtree(index.parent, ignore_errors=True)
    return url or None



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
    failed, note = (False, "") if live_ok else not_live_note(sha)
    if failed:
        return "warning", f"⚠️ Approved and saved, but NOT live yet.{note}"
    return "white_check_mark", (f"✅ Live: the previewed change is on the site now.\n{links(pages, live, sha[:7])}{note}\n"
                                "_Reply *undo* here to roll it back._")


def cancel_preview(req: dict) -> tuple[str, str]:
    sh("git", "push", "-q", "origin", "--delete", preview_branch(req["thread_ts"]), check=False)
    return "wastebasket", "🗑️ Dropped the preview. Nothing went live."


def handle(req: dict, slack: Slack, live: str) -> tuple[str, str]:
    """Returns (reaction, reply text)."""
    if req["kind"] == "blocked":
        return "no_entry_sign", ("🚫 Website changes by email are switched off, so nothing was changed. "
                                 "Post the request here in the channel instead.")
    sync_main()
    pending = req["kind"] in ("followup", "undo") and preview_pending(req["thread_ts"])
    if pending:
        text = req.get("text") or ""
        if APPROVE_RE.match(text):
            return approve_preview(req, live)
        if wants_cancel(text) or (req["kind"] == "undo" and not thread_commits(req["thread_ts"])):
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
        failed, note = (False, "") if live_ok else not_live_note(sha)
        if failed:
            return "warning", f"⚠️ Undo saved, but NOT live yet.{note}"
        return ("leftwards_arrow_with_hook",
                "↩️ Undone. The site is back to how it was before this thread's change." + note)

    preview = pending or wants_preview(req)
    if preview:
        start_preview_tree(req["thread_ts"], pending)
    try:
        return make_change(req, slack, live, preview)
    finally:
        if preview:
            back_to_main()


def make_change(req: dict, slack: Slack, live: str, preview: bool) -> tuple[str, str]:
    try:
        req["attached"] = prepare_files(req, slack)
    except FileError as e:
        owner = os.environ.get("WEBSITE_REQUESTS_OWNER", "")
        return "warning", ("⚠️ I couldn't open the file you attached, so nothing was changed. "
                           f"{f'<@{owner}> ' if owner else ''}has been flagged; once it's fixed, reply *retry* "
                           f"here (or post the file again).\n`{str(e)[:300]}`")

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
        blocked = result.get("denied") or []
        owner = os.environ.get("WEBSITE_REQUESTS_OWNER", "")
        note = (f"\n_Robot note for <@{owner}>: a step was blocked (`{blocked[0][:120]}`)._"
                if blocked and owner else "")
        return "speech_balloon", f"💬 {q}\n_Reply in this thread and I'll pick it up._{note}"

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
        url = publish_preview(sha)
        preview_ok = wait_live(sha, url, timeout=6 * 60) if url else False
        lines = [f"🔍 Preview ready (NOT live yet): {summary}"]
        if url:
            lines.append(links(pages, url, sha[:7]) + ("" if preview_ok else
                         "\n_(The preview site is still publishing; give it a minute if a link shows the old page.)_"))
        else:
            lines.append("_(The preview site isn't set up, so there's no link to share yet.)_")
        lines.append("_Reply *approve* to put this live, reply with changes to update the preview, "
                     "or *cancel* to drop it._")
        lines.append(f"_{footer}_")
        return "mag", "\n".join(lines)

    sha = commit_and_ship(req, summary)
    live_ok = wait_live(sha, live)
    failed, note = (False, "") if live_ok else not_live_note(sha)
    if failed:
        return "warning", (f"⚠️ Saved but NOT live yet: {summary}{note}\n"
                           f"_Reply *undo* here to drop it._\n_{footer}_")
    return "white_check_mark", (f"✅ Live: {summary}\n{links(pages, live, sha[:7])}{note}\n"
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
        if req["kind"] == "followup" and (APPROVE_RE.match(text) or wants_cancel(text)):
            return                                     # approve / cancel: the outcome reply is enough
        mins = "2 to 4" if tier != "advanced" or req["kind"] == "undo" else "5 to 15"
        later = (f"I'll reply here with a preview link in about {mins} minutes. Nothing goes live "
                 "until you approve it." if wants_preview(req) else
                 f"I'll reply here when it's live (usually {mins} minutes).")
        text = (f"👀 On it. {later}" if tier != "advanced" or req["kind"] == "undo" else
                f"👀 On it. This is a design/advanced change, so I'm using {model_name(model)} and checking it "
                f"visually on desktop and mobile. {later}")
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


def install_requirements():
    """pip install tools/requirements-robot.txt (the workflow does this at start; a listener that restarts
    into newer robot code does it again so new packages, e.g. for face-aware photo crops, are there)."""
    reqs = ROOT / "tools" / "requirements-robot.txt"
    if reqs.exists():
        r = sh(sys.executable, "-m", "pip", "install", "-q", "-r", str(reqs), check=False, timeout=900)
        print("requirements:", "ok" if r.returncode == 0 else r.stdout[-400:], flush=True)


def listen(slack: Slack, channel: str, live: str, minutes: float, poll: float,
           start: float = 0.0, allowed: set[str] | None = None, sleep=time.sleep, clock=time.time) -> int:
    """Near-instant mode: poll the channel every `poll` seconds for `minutes`, handling requests
    one at a time as they arrive. The workflow restarts it so one listener is always running."""
    end = float(os.environ.get("LISTEN_UNTIL") or 0) or clock() + minutes * 60
    if os.environ.get("LISTEN_UNTIL"):
        install_requirements()     # restarted into new robot code: it may need new packages
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
