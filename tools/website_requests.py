#!/usr/bin/env python3
"""AscendPoint AI website robot: turns Slack requests into live site changes.

Runs in GitHub Actions (.github/workflows/website-requests.yml) every 5 minutes:

    python3 tools/website_requests.py next   # find the oldest unhandled request -> request.json
    python3 tools/website_requests.py run request.json

Front doors (both land in the Slack channel, which is the single queue):
  * a message in #website-requests (top-level = new request; reply in the thread = follow-up)
  * an email to website@ascendpoint.agency, which Zapier posts into the channel as
    "📧 Email request from <name> <email>: ..."

For each request the robot:
  1. reacts 👀 and replies "On it" in the thread,
  2. "undo" in a thread  -> git-reverts the commits that thread produced,
     anything else       -> Claude Code (headless) edits site/ with a tight tool allowlist,
  3. runs every site check (tools/build.py + tools/test_redirects.py); nothing ships if they fail,
  4. commits to main ("AscendPoint AI"), triggers the deploy workflow, waits until
     https://ascendpoint.agency/version.txt shows the new commit,
  5. replies in the thread with what changed + links, and reacts ✅ (or 💬 question / ⚠️ failed).

State lives in Slack reactions: a message the bot has reacted to is never picked up again.

Environment: SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, ANTHROPIC_API_KEY, GITHUB_TOKEN (Actions),
optional WEBSITE_REQUESTS_START (unix ts; ignore older messages), WEBSITE_REQUESTS_ALLOWED
(comma-separated Slack user IDs; empty = anyone in the channel), LIVE_URL, CLAUDE_MODEL.
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLACK = "https://slack.com/api/"
HANDLED = {"eyes", "white_check_mark", "warning", "speech_balloon", "leftwards_arrow_with_hook"}
MAX_AGE = 2 * 24 * 3600          # never pick up anything older than 2 days
UNDO_RE = re.compile(r"^\W*(undo|revert|roll ?back)\b", re.I)
IGNORE_RE = re.compile(r"^\s*(//|note:|fyi\b)", re.I)
EMAIL_PREFIX = "📧"
RESULT = Path("/tmp/website_request_result.json")
FILES_DIR = Path("/tmp/website_request_files")
BOT_NAME = "AscendPoint AI"


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
              start: float = 0.0, allowed: set[str] | None = None) -> dict | None:
    """Oldest unhandled request: a top-level message, or a reply in a thread the bot is in."""
    now = now or time.time()
    oldest = max(start, now - MAX_AGE)
    auth = slack.call("auth.test")
    bot_user, bot_id = auth["user_id"], auth.get("bot_id")
    hist = slack.call("conversations.history", channel=channel, oldest=f"{oldest:.6f}", limit=200)
    candidates = []
    for m in hist.get("messages", []):
        if is_request(m, bot_user, bot_id) and not is_handled(m, bot_user):
            candidates.append((m, None))
        if m.get("reply_count"):
            rep = slack.call("conversations.replies", channel=channel, ts=m["ts"], limit=200)
            thread = rep.get("messages", [])
            bot_in_thread = any(x.get("user") == bot_user for x in thread)
            for r in thread[1:]:
                r.setdefault("thread_ts", m["ts"])
                if float(r["ts"]) < oldest or not bot_in_thread:
                    continue
                if is_request(r, bot_user, bot_id) and not is_handled(r, bot_user):
                    candidates.append((r, thread))
    if allowed:
        candidates = [c for c in candidates
                      if c[0].get("user") in allowed or (c[0].get("text", "").startswith(EMAIL_PREFIX))]
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
    requester = user_name(slack, msg.get("user"))
    if text.startswith(EMAIL_PREFIX):
        m = re.match(r"📧\s*Email request from\s+([^:\n]+)", text)
        requester = (m.group(1).strip() if m else "email") + " (email)"
    return {
        "channel": channel, "ts": msg["ts"], "thread_ts": thread_ts,
        "user": msg.get("user"), "requester": requester, "text": text,
        "files": [{"name": f.get("name"), "mimetype": f.get("mimetype"),
                   "url": f.get("url_private_download") or f.get("url_private")}
                  for f in msg.get("files", []) if f.get("url_private")],
        "kind": "undo" if UNDO_RE.match(text or "") and thread_ts != msg["ts"] else
                ("followup" if thread_ts != msg["ts"] else "new"),
        "context": context,
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
- Finally write {result} as JSON:
  {{"status": "changed" | "question" | "no_change",
    "summary": "<one or two plain-English sentences for the requester: what you changed and where>",
    "pages": ["/about/", ...],
    "question": "<only when status is question>"}}
  Plain language, no file paths or code in summary/question; the requester is not technical.
"""


def run_claude(req: dict) -> dict:
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
    )
    env = {k: os.environ[k] for k in ("PATH", "HOME", "ANTHROPIC_API_KEY", "LANG") if k in os.environ}
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    allowed = ",".join([
        "Read", "Edit", "Write", "Glob", "Grep",
        "Bash(python3 tools/build.py)", "Bash(python3 tools/build.py:*)",
        "Bash(python3 tools/img_for_web.py:*)", "Bash(ls:*)",
    ])
    cmd = ["claude", "-p", prompt,
           "--append-system-prompt", CLAUDE_RULES.format(files_dir=FILES_DIR, result=RESULT),
           "--allowedTools", allowed,
           "--disallowedTools", "WebFetch,WebSearch,Bash(git:*),Bash(curl:*)",
           "--permission-mode", "dontAsk",
           "--max-budget-usd", os.environ.get("CLAUDE_MAX_USD") or "3",
           "--no-session-persistence",
           "--output-format", "json",
           "--model", os.environ.get("CLAUDE_MODEL") or "sonnet"]
    r = sh(*cmd, check=False, env=env, timeout=20 * 60)
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
    return result


def wait_live(sha: str, live_url: str, timeout: int = 9 * 60) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{live_url}/version.txt?v={int(time.time())}", timeout=20) as r:
                if r.read().decode().strip().startswith(sha):
                    return True
        except Exception:
            pass
        time.sleep(20)
    return False


def commit_and_ship(req: dict, summary: str, extra_trailers: str = "") -> str:
    msg = (f"Website request: {summary[:68]}\n\n{summary}\n\n"
           f"Requested-by: {req['requester']}\nSlack-Thread: {req['thread_ts']}\nSlack-Message: {req['ts']}\n"
           f"{extra_trailers}")
    sh("git", "add", "-A", "site")
    sh("git", "-c", "user.name=AscendPoint AI", "-c", "user.email=website-bot@ascendpoint.agency",
       "commit", "-q", "-m", msg)
    for _ in range(3):
        if sh("git", "push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            break
        sh("git", "pull", "-q", "--rebase", "origin", "main")
    else:
        raise RuntimeError("push to main failed")
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


def handle(req: dict, slack: Slack, live: str) -> tuple[str, str]:
    """Returns (reaction, reply text)."""
    if req["kind"] == "undo":
        commits = thread_commits(req["thread_ts"])
        if not commits:
            return "speech_balloon", "There's nothing from this thread live to undo."
        for c in commits:  # newest first
            sh("git", "-c", "user.name=AscendPoint AI", "-c", "user.email=website-bot@ascendpoint.agency",
               "revert", "--no-edit", c)
        ok, log = checks()
        if not ok:
            reset_worktree()
            return "warning", "I couldn't undo this cleanly, so I left the site as it is. Kyle has been flagged."
        sh("git", "push", "-q", "origin", "HEAD:main")
        sha = sh("git", "rev-parse", "HEAD").stdout.strip()
        sh("gh", "workflow", "run", "deploy.yml", "--ref", "main")
        live_ok = wait_live(sha, live)
        return ("leftwards_arrow_with_hook",
                "↩️ Undone. The site is back to how it was before this thread's change."
                + ("" if live_ok else " (Publishing is taking longer than usual; it should show within a few minutes.)"))

    FILES_DIR.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(req.get("files", [])):
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", f.get("name") or f"file-{i}")
        slack.download(f["url"], FILES_DIR / name)

    result = run_claude(req)
    files = changed_files()
    outside = [f for f in files if not f.startswith("site/")]
    if outside:  # Claude may only touch site/; silently drop anything else
        for f in outside:
            sh("git", "checkout", "-q", "HEAD", "--", f, check=False)
            sh("git", "clean", "-fdq", "--", f, check=False)
        files = changed_files()

    if result.get("status") == "question" or not files:
        reset_worktree()
        q = result.get("question") or result.get("summary") or "I wasn't sure what to change. Can you say a bit more?"
        return "speech_balloon", f"💬 {q}\n_Reply in this thread and I'll pick it up._"

    ok, log = checks()
    if not ok:
        reset_worktree()
        tail = "\n".join(log.strip().splitlines()[-6:])
        return "warning", ("⚠️ I made the change but it failed the site's quality checks, so nothing went live. "
                           f"<@{os.environ.get('WEBSITE_REQUESTS_OWNER', '')}> can take a look.\n```{tail[:900]}```")

    summary = result.get("summary") or "Updated the website."
    pages = result.get("pages") or sorted({u for u in map(url_for, files) if u})
    sha = commit_and_ship(req, summary)
    live_ok = wait_live(sha, live)
    cost = result.get("cost_usd")
    note = "" if live_ok else "\n_(Publishing is taking longer than usual; it should show within a few minutes.)_"
    return "white_check_mark", (f"✅ Live: {summary}\n{links(pages, live)}{note}\n"
                                f"_Reply *undo* here to roll it back, or reply with tweaks._"
                                + (f"\n_cost ${cost:.2f}_" if isinstance(cost, (int, float)) else ""))


# ----------------------------------------------------------------------------- CLI
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
        slack.call("reactions.add", channel=channel, timestamp=req["ts"], name="eyes")
        slack.call("chat.postMessage", channel=channel, thread_ts=req["thread_ts"],
                   text="👀 On it. I'll reply here when it's live (usually 3 to 6 minutes).")
        out.write_text(json.dumps(req, indent=2))
        print(f"picked {req['kind']} request {req['ts']} from {req['requester']}")
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a") as f:
                f.write("found=true\n")
        return 0

    if argv[:1] == ["run"]:
        req = json.loads(Path(argv[1]).read_text())
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
        print(reaction, text)
        return 0 if reaction != "warning" else 1

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
