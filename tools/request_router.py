#!/usr/bin/env python3
"""Wake other sites' website robots from Slack (runs free, here, because this repo is public).

The AscendPoint robot (tools/website_requests.py) is untouched by this file: it keeps handling
#ascendpoint-website-requests for ascendpoint.agency. This router only watches OTHER channels and never edits
anything. For each new request in a routed channel it:

  1. reacts 📥 ("queued") so it isn't sent twice, and
  2. sends a repository_dispatch "website-request" {channel, ts, thread_ts} to that site's repo,
     whose own robot (in its own private repo, with a token that can only push there) does the work.

    ROUTES="C0123SERP=ascendpoint/serpdental-site" python3 tools/request_router.py listen

A request is a human top-level message, or a reply in a thread the bot is in (follow-ups, "undo").
Nothing is logged except timestamps: this repo's Actions logs are public.

Environment: SLACK_BOT_TOKEN, ROUTES (comma-separated channel=owner/repo), DISPATCH_TOKEN (fine-grained
token with Contents: read and write on the target repos), optional ROUTER_START (unix ts; ignore older
messages), ROUTER_ANY_THREAD (channels where replies under any bot's post count, e.g. #ad-intelligence),
ROUTER_OWNER (Slack user @mentioned when a hand-off keeps failing; default Kyle), POLL_SECONDS (default 10),
LISTEN_MINUTES (default 340).

If a hand-off fails (e.g. the dispatch token can't reach the target repo), the router never goes quiet: it
replies once in the thread with ⚠️ and the reason, @mentions the owner, and keeps retrying once a minute, so the
message is answered automatically as soon as the problem is fixed.

Routes (Oct 2026): #serpdental-website-requests -> ascendpoint/serpdental-site (website robot),
#ad-intelligence (was #meta-ads) -> ascendpoint/ads-assistant (questions + approved actions: Meta / Zoom / Typeform / GHL).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import website_requests as wr  # noqa: E402  (Slack client + "is this a request?" rules only)

QUEUED = "inbox_tray"
STUCK = "hourglass_flowing_sand"   # hand-off failing; still retried (not in DONE)
RETRY_SECONDS = 60
NOTICE_MARK = "couldn't hand this to the"
DEFAULT_OWNER = "U04Q3M29UKE"      # Kyle Robins
WHY = {401: "the router's GitHub token is invalid or expired",
       403: "the router's GitHub token doesn't have permission on that repo",
       404: "the router's GitHub token can't see that repo (add the repo to the token's Repository access)",
       422: "GitHub rejected the request"}
DONE = wr.HANDLED | {QUEUED}       # anything the bot already reacted to is never sent again
MAX_AGE = wr.MAX_AGE


def parse_routes(raw: str | None) -> dict[str, str]:
    routes = {}
    for part in (raw or "").split(","):
        if "=" in part:
            ch, repo = (x.strip() for x in part.split("=", 1))
            if ch and repo.count("/") == 1:
                routes[ch] = repo
    return routes


def done_by_bot(msg: dict, bot_user: str) -> bool:
    return any(r.get("name") in DONE and bot_user in r.get("users", []) for r in msg.get("reactions", []))


def any_thread_channels() -> set[str]:
    """Channels (ROUTER_ANY_THREAD, comma-separated) where a reply counts in any thread that has a bot post in it,
    not only threads this bot is in (e.g. #ad-intelligence: replies under a daily report that Zapier posted)."""
    return {c.strip() for c in (os.environ.get("ROUTER_ANY_THREAD") or "").split(",") if c.strip()}


def pending(slack, channel: str, bot_user: str, bot_id: str | None, start: float = 0.0,
            now: float | None = None, cache: dict | None = None, any_thread: bool = False) -> list[dict]:
    """New requests in `channel`, oldest first: [{"ts", "thread_ts"}]."""
    now = now or time.time()
    oldest = max(start, now - MAX_AGE)
    quiet = cache.setdefault(("quiet", channel), {}) if cache is not None else {}
    hist = slack.call("conversations.history", channel=channel, oldest=f"{oldest:.6f}", limit=200)
    out = []
    for m in hist.get("messages", []):
        if wr.is_request(m, bot_user, bot_id) and not done_by_bot(m, bot_user):
            out.append({"ts": m["ts"], "thread_ts": None})
        if m.get("reply_count"):
            marker = m.get("latest_reply") or str(m.get("reply_count"))
            if quiet.get(m["ts"]) == marker:
                continue
            thread = slack.call("conversations.replies", channel=channel, ts=m["ts"], limit=200).get("messages", [])
            bot_in_thread = any(x.get("user") == bot_user for x in thread) or \
                (any_thread and any(x.get("bot_id") for x in thread))
            found = False
            for r in thread[1:]:
                if float(r["ts"]) < oldest or not bot_in_thread:
                    continue
                if wr.is_request(r, bot_user, bot_id) and not done_by_bot(r, bot_user):
                    out.append({"ts": r["ts"], "thread_ts": m["ts"]})
                    found = True
            if not found:
                quiet[m["ts"]] = marker
    return sorted(out, key=lambda x: float(x["ts"]))


def dispatch(repo: str, token: str, payload: dict, opener=None, status: dict | None = None) -> bool:
    """POST a repository_dispatch. On failure, status["code"] = the HTTP code (0 = network) for the notice."""
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/dispatches",
        data=json.dumps({"event_type": "website-request", "client_payload": payload}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
        method="POST")
    try:
        with opener(req, timeout=30) as r:
            return 200 <= getattr(r, "status", 204) < 300
    except Exception as e:
        if status is not None:
            status["code"] = int(getattr(e, "code", 0) or 0)
        print(f"dispatch to {repo} failed: {type(e).__name__} {getattr(e, 'code', '')}", file=sys.stderr, flush=True)
        return False


def notify_stuck(slack, channel: str, it: dict, repo: str, code: int, bot_user: str, cache: dict | None):
    """Say once, in the thread, that the hand-off is failing and why (so nobody waits on a silent bot)."""
    noticed = cache.setdefault("noticed", set()) if cache is not None else set()
    if it["ts"] in noticed:
        return
    noticed.add(it["ts"])
    root = it["thread_ts"] or it["ts"]
    try:   # already said so (e.g. before a router restart)? then don't repeat it
        thread = slack.call("conversations.replies", channel=channel, ts=root, limit=200).get("messages", [])
        if any(m.get("user") == bot_user and NOTICE_MARK in (m.get("text") or "") and float(m["ts"]) > float(it["ts"])
               for m in thread):
            return
    except Exception:
        pass
    owner = os.environ.get("ROUTER_OWNER") or DEFAULT_OWNER
    why = WHY.get(code, f"GitHub answered {code}" if code else "GitHub couldn't be reached")
    try:
        slack.call("reactions.add", channel=channel, timestamp=it["ts"], name=STUCK)
        slack.call("chat.postMessage", channel=channel, thread_ts=root, unfurl_links="false",
                   text=f":warning: I saw this, but {NOTICE_MARK} `{repo}` robot: {why} (HTTP {code or 'network'}). "
                        f"<@{owner}> can fix it; I'll keep retrying every minute and answer here automatically once it works.")
    except Exception as e:
        print(f"notice failed: {type(e).__name__}", file=sys.stderr, flush=True)


def route_once(slack, routes: dict[str, str], token: str, start: float = 0.0, cache: dict | None = None,
               opener=None, now: float | None = None) -> int:
    if cache is not None and "auth" in cache:
        auth = cache["auth"]
    else:
        auth = slack.call("auth.test")
        if cache is not None:
            cache["auth"] = auth
    bot_user, bot_id = auth["user_id"], auth.get("bot_id")
    sent = 0
    anyt = any_thread_channels()
    for channel, repo in routes.items():
        try:
            items = pending(slack, channel, bot_user, bot_id, start=start, now=now, cache=cache,
                            any_thread=channel in anyt)
        except Exception as e:
            print(f"poll {channel} failed: {type(e).__name__}", file=sys.stderr, flush=True)
            continue
        retry_at = cache.setdefault("retry_at", {}) if cache is not None else {}
        t_now = now or time.time()
        for it in items:
            if retry_at.get(it["ts"], 0) > t_now:      # a failing hand-off is retried once a minute, not every poll
                continue
            slack.call("reactions.add", channel=channel, timestamp=it["ts"], name=QUEUED)
            status: dict = {}
            if dispatch(repo, token, {"channel": channel, "ts": it["ts"], "thread_ts": it["thread_ts"]}, opener, status):
                print(f"queued {it['ts']} -> {repo}", flush=True)
                sent += 1
                retry_at.pop(it["ts"], None)
                if cache is not None and it["ts"] in cache.get("noticed", set()):
                    slack.call("reactions.remove", channel=channel, timestamp=it["ts"], name=STUCK)
            else:   # take the marker off so it's retried, and tell the thread why it's stuck
                slack.call("reactions.remove", channel=channel, timestamp=it["ts"], name=QUEUED)
                retry_at[it["ts"]] = t_now + RETRY_SECONDS
                notify_stuck(slack, channel, it, repo, status.get("code", 0), bot_user, cache)
    return sent


CODE = [Path(__file__).resolve(), Path(__file__).resolve().parent / "website_requests.py"]


def code_changed(snapshot: list[bytes]) -> bool:
    """Pull main; True when this router (or the robot rules it imports) changed since it started."""
    import subprocess
    subprocess.run(["git", "pull", "-q", "--ff-only", "origin", "main"], cwd=Path(__file__).resolve().parent.parent,
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return [p.read_bytes() if p.exists() else b"" for p in CODE] != snapshot


def listen(slack, routes, token, minutes: float, poll: float, start: float = 0.0,
           sleep=time.sleep, clock=time.time, watch_code: bool = False) -> int:
    end = float(os.environ.get("LISTEN_UNTIL") or 0) or clock() + minutes * 60
    snapshot = [p.read_bytes() if p.exists() else b"" for p in CODE]
    last_pull = clock()
    cache: dict = {}
    total = 0
    while clock() < end:
        try:
            total += route_once(slack, routes, token, start=start, cache=cache)
        except Exception as e:
            print(f"router error: {type(e).__name__}", file=sys.stderr, flush=True)
        if watch_code and clock() - last_pull > 300:
            last_pull = clock()
            if code_changed(snapshot):             # e.g. new "handled" reactions: restart into the new code
                print("router code updated on main; restarting", flush=True)
                os.environ["LISTEN_UNTIL"] = str(end)
                os.execv(sys.executable, [sys.executable, str(CODE[0]), "listen"])
        sleep(poll)
    print(f"router done: {total} request(s) queued", flush=True)
    return 0


def main(argv: list[str]) -> int:
    token = os.environ.get("SLACK_BOT_TOKEN")
    routes = parse_routes(os.environ.get("ROUTES"))
    dtoken = os.environ.get("DISPATCH_TOKEN")
    if not token or not routes or not dtoken:
        print("SLACK_BOT_TOKEN / ROUTES / DISPATCH_TOKEN not configured; nothing to do")
        return 0
    slack = wr.Slack(token)
    start = float(os.environ.get("ROUTER_START") or 0)
    if argv[:1] == ["once"]:
        route_once(slack, routes, dtoken, start=start)
        return 0
    if argv[:1] == ["listen"]:
        return listen(slack, routes, dtoken, minutes=float(os.environ.get("LISTEN_MINUTES") or 340),
                      poll=float(os.environ.get("POLL_SECONDS") or 10), start=start, watch_code=True)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
