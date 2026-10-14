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
ROUTER_WAKE_CHANNELS (channels where only a message that calls the bot counts: @mention, "AscendPoint AI/agent",
"the agent"; replies in threads the bot is in always count), ROUTER_OWNER (Slack user @mentioned when a hand-off keeps failing; default Kyle), POLL_SECONDS (default 10),
LISTEN_MINUTES (default 340).

If a hand-off fails (e.g. the dispatch token can't reach the target repo), the router never goes quiet: it
replies once in the thread with ⚠️ and the reason, @mentions the owner, and keeps retrying once a minute, so the
message is answered automatically as soon as the problem is fixed.

Routes (Oct 2026): #serpdental-website-requests -> ascendpoint/serpdental-site (website robot),
#ad-intelligence (was #meta-ads) -> ascendpoint/ads-assistant (questions + approved actions: Meta / Zoom / Typeform / GHL),
#social-requests -> ascendpoint/ads-assistant#social-request (the social calendar robot).
A route may name its event type after '#' (default "website-request"), so one repo can host several robots.

Patrick & Sonia watch (EXEC_ROUTE="ascendpoint/ads-assistant#exec-request", EXEC_PEOPLE="U06D77DKB88,U07TJCUD6JC"):
every other channel the bot is in is polled once a minute (EXEC_POLL_SECONDS) for their messages; a quick model
(ANTHROPIC_API_KEY, EXEC_TRIAGE_MODEL, default claude-haiku-4-5; keyword rules if it can't be reached) decides whether
each is a request we could do, and those are sent to the exec robot. Follow-ups in its threads (⚡ footer) from them or
EXEC_APPROVERS / the owner go straight through. EXEC_SKIP / SITE_CHANNEL: channels to leave alone.

Social clock (SOCIAL_SCHEDULE="owner/repo:path/to/schedule.json"): once a minute the router reads the social robot's
schedule (when a post is due to publish, needs confirming or a retry) and sends that repo a "social-tick" dispatch
the moment something is due, so posts go out on time without a private-repo cron burning Actions minutes. The same
file lists the monthly calendar threads, which the router keeps watching for replies past its usual 2-day window.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import website_requests as wr  # noqa: E402  (Slack client + "is this a request?" rules only)

QUEUED = "inbox_tray"
STUCK = "hourglass_flowing_sand"   # hand-off failing; still retried (not in DONE)
RETRY_SECONDS = 60
STALL_SECONDS = 300                # handed off, but the robot hasn't reacted 👀 within 5 min -> say so
STARTED = {"eyes", "white_check_mark", "warning", "speech_balloon", "leftwards_arrow_with_hook", "mag"}
STALL_MARK = "handed this to the"
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
            if ch and split_route(repo)[0].count("/") == 1:
                routes[ch] = repo
    return routes


def split_route(route: str) -> tuple[str, str]:
    """'owner/repo#social-request' -> ('owner/repo', 'social-request'); no '#' -> 'website-request'."""
    repo, _, event = route.partition("#")
    return repo.strip(), (event.strip() or "website-request")


def done_by_bot(msg: dict, bot_user: str) -> bool:
    return any(r.get("name") in DONE and bot_user in r.get("users", []) for r in msg.get("reactions", []))


def any_thread_channels() -> set[str]:
    """Channels (ROUTER_ANY_THREAD, comma-separated) where a reply counts in any thread that has a bot post in it,
    not only threads this bot is in (e.g. #ad-intelligence: replies under a daily report that Zapier posted)."""
    return {c.strip() for c in (os.environ.get("ROUTER_ANY_THREAD") or "").split(",") if c.strip()}


WAKE_RE = re.compile(r"ascend\s*point\s+(ai|mcp|agent)\b|\b(the|our)\s+(audit\s+)?agent\b", re.I)


def wake_channels() -> set[str]:
    """Channels (ROUTER_WAKE_CHANNELS, comma-separated) where people talk to each other and the bot only answers when
    called: an @mention of the bot, or "AscendPoint AI/MCP/agent", or "the agent". Replies in a thread the bot is in
    always count (follow-ups)."""
    return {c.strip() for c in (os.environ.get("ROUTER_WAKE_CHANNELS") or "").split(",") if c.strip()}


def wakes(msg: dict, bot_user: str) -> bool:
    text = msg.get("text", "") or ""
    return f"<@{bot_user}" in text or bool(WAKE_RE.search(text)) or bool(wr.handoff_from(msg, bot_user))


def pending(slack, channel: str, bot_user: str, bot_id: str | None, start: float = 0.0,
            now: float | None = None, cache: dict | None = None, any_thread: bool = False,
            wake_only: bool = False) -> list[dict]:
    """New requests in `channel`, oldest first: [{"ts", "thread_ts"}]."""
    now = now or time.time()
    oldest = max(start, now - MAX_AGE)
    quiet = cache.setdefault(("quiet", channel), {}) if cache is not None else {}
    hist = slack.call("conversations.history", channel=channel, oldest=f"{oldest:.6f}", limit=200)
    out = []
    for m in hist.get("messages", []):
        if wr.is_request(m, bot_user, bot_id) and not done_by_bot(m, bot_user) \
                and (not wake_only or wakes(m, bot_user)):
            out.append({"ts": m["ts"], "thread_ts": None})
        if m.get("reply_count"):
            marker = m.get("latest_reply") or str(m.get("reply_count"))
            if quiet.get(m["ts"]) == marker:
                continue
            thread = slack.call("conversations.replies", channel=channel, ts=m["ts"], limit=200).get("messages", [])
            bot_in_thread = any(x.get("user") == bot_user for x in thread) or \
                (any_thread and any(x.get("bot_id") for x in thread))
            # in a wake-only channel a reply can also call the bot by name in a thread it hasn't joined yet
            # (a teammate's thread: "@AscendPoint AI build this deck")
            found = False
            for r in thread[1:]:
                if float(r["ts"]) < oldest:
                    continue
                if not (bot_in_thread or (wake_only and wakes(r, bot_user))):
                    continue
                if wr.is_request(r, bot_user, bot_id) and not done_by_bot(r, bot_user):
                    out.append({"ts": r["ts"], "thread_ts": m["ts"]})
                    found = True
            if not found:
                quiet[m["ts"]] = marker
    seen_roots = {m["ts"] for m in hist.get("messages", [])}
    out += watched_replies(slack, channel, bot_user, bot_id, oldest, now, cache, skip=seen_roots)
    return sorted(out, key=lambda x: float(x["ts"]))


WATCH_POLL = 20     # seconds between polls of one watched (older) thread


def watched_replies(slack, channel: str, bot_user: str, bot_id: str | None, oldest: float, now: float,
                    cache: dict | None, skip: set | frozenset = frozenset()) -> list[dict]:
    """Replies in long-lived threads the social robot asked us to watch (its monthly calendar threads), whose root is
    older than the 2-day history window. Only replies newer than `oldest` count, like everywhere else."""
    if cache is None:
        return []
    roots = [t["ts"] for t in cache.get("watch_threads", []) if t.get("channel") == channel and t.get("ts") not in skip]
    last = cache.setdefault("watch_polled", {})
    out = []
    for root in roots:
        if now - last.get(root, 0) < WATCH_POLL:
            continue
        last[root] = now
        try:
            thread = slack.call("conversations.replies", channel=channel, ts=root, oldest=f"{oldest:.6f}",
                                limit=200).get("messages", [])
        except Exception as e:
            print(f"watch {root} failed: {type(e).__name__}", file=sys.stderr, flush=True)
            continue
        for r in thread:
            if r.get("ts") == root or float(r["ts"]) < oldest:
                continue
            if wr.is_request(r, bot_user, bot_id) and not done_by_bot(r, bot_user):
                out.append({"ts": r["ts"], "thread_ts": root})
    return out


# ----------------------------------------------------------------------------- social clock
CLOCK_SECONDS = 60


def social_clock(token: str, cache: dict, opener=None, now: float | None = None) -> int:
    """Read the social robot's schedule.json (ETag-cached) and send one "social-tick" when anything is due.
    Returns the number of due items dispatched."""
    spec = (os.environ.get("SOCIAL_SCHEDULE") or "").strip()
    if not spec or ":" not in spec:
        return 0
    now = now or time.time()
    if now - cache.get("clock_at", 0) < CLOCK_SECONDS:
        return 0
    cache["clock_at"] = now
    repo, path = spec.split(":", 1)
    opener = opener or urllib.request.urlopen
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json",
            "X-GitHub-Api-Version": "2022-11-28"}
    if cache.get("clock_etag"):
        hdrs["If-None-Match"] = cache["clock_etag"]
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/{path}?ref=main", headers=hdrs)
    try:
        with opener(req, timeout=30) as r:
            body = r.read().decode()
            et = (getattr(r, "headers", None) or {}).get("ETag") if hasattr(getattr(r, "headers", None), "get") else None
        sched = json.loads(body)
        cache["clock_schedule"] = sched
        if et:
            cache["clock_etag"] = et
    except Exception as e:
        if int(getattr(e, "code", 0) or 0) != 304:
            print(f"social clock: couldn't read the schedule ({type(e).__name__} {getattr(e, 'code', '')})",
                  file=sys.stderr, flush=True)
    sched = cache.get("clock_schedule") or {}
    cache["watch_threads"] = sched.get("threads") or []
    fired = cache.setdefault("clock_fired", set())
    due = [d["key"] for d in sched.get("due") or [] if d.get("at", 0) <= now and d.get("key") not in fired]
    if not due:
        return 0
    if dispatch(f"{repo}#social-tick", token, {"keys": due[:50]}, opener):
        fired.update(due)
        print(f"social clock: {len(due)} due -> tick", flush=True)
        return len(due)
    return 0


def dispatch(repo: str, token: str, payload: dict, opener=None, status: dict | None = None) -> bool:
    """POST a repository_dispatch. On failure, status["code"] = the HTTP code (0 = network) for the notice."""
    opener = opener or urllib.request.urlopen
    repo, event = split_route(repo)
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/dispatches",
        data=json.dumps({"event_type": event, "client_payload": payload}).encode(),
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
                   text=f":warning: I saw this, but {NOTICE_MARK} `{split_route(repo)[0]}` robot: {why} (HTTP {code or 'network'}). "
                        f"<@{owner}> can fix it; I'll keep retrying every minute and answer here automatically once it works.")
    except Exception as e:
        print(f"notice failed: {type(e).__name__}", file=sys.stderr, flush=True)


def check_stalled(slack, bot_user: str, cache: dict | None, now: float):
    """A hand-off that went through but the robot never started (👀) within 5 minutes — usually the robot repo is
    missing its secrets or its workflow is off. Say so once in the thread instead of leaving it silent."""
    if cache is None:
        return
    sent = cache.setdefault("sent_at", {})
    for ts, (channel, thread_ts, repo, t0) in list(sent.items()):
        if now - t0 < STALL_SECONDS:
            continue
        sent.pop(ts)
        cache.setdefault("stall_checked", set()).add(ts)
        try:
            r = slack.call("reactions.get", channel=channel, timestamp=ts)
            names = {x.get("name") for x in (r.get("message") or {}).get("reactions", []) if bot_user in x.get("users", [])}
            if names & STARTED:
                continue
            owner = os.environ.get("ROUTER_OWNER") or DEFAULT_OWNER
            slack.call("chat.postMessage", channel=channel, thread_ts=thread_ts or ts, unfurl_links="false",
                       text=f":warning: I {STALL_MARK} `{split_route(repo)[0]}` robot 5 minutes ago, but it hasn't started. That usually "
                            f"means the robot is missing its GitHub secrets (SLACK_BOT_TOKEN, ANTHROPIC_API_KEY) or its "
                            f"workflow is turned off — see github.com/{split_route(repo)[0]}/actions. <@{owner}>")
        except Exception as e:
            print(f"stall check failed: {type(e).__name__}", file=sys.stderr, flush=True)


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
    wake = wake_channels()
    for channel, repo in routes.items():
        try:
            items = pending(slack, channel, bot_user, bot_id, start=start, now=now, cache=cache,
                            any_thread=channel in anyt, wake_only=channel in wake)
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
                if cache is not None:
                    if it["ts"] not in cache.setdefault("stall_checked", set()):
                        cache.setdefault("sent_at", {}).setdefault(it["ts"], (channel, it["thread_ts"], repo, t_now))
                retry_at.pop(it["ts"], None)
                if cache is not None and it["ts"] in cache.get("noticed", set()):
                    slack.call("reactions.remove", channel=channel, timestamp=it["ts"], name=STUCK)
            else:   # take the marker off so it's retried, and tell the thread why it's stuck
                slack.call("reactions.remove", channel=channel, timestamp=it["ts"], name=QUEUED)
                retry_at[it["ts"]] = t_now + RETRY_SECONDS
                notify_stuck(slack, channel, it, repo, status.get("code", 0), bot_user, cache)
    check_stalled(slack, bot_user, cache, now or time.time())
    return sent


# ----------------------------------------------------------------------------- Patrick & Sonia ("exec") watch
# Every channel the bot is in that no robot owns (not in ROUTES, not the AscendPoint website channel) is watched for
# messages from EXEC_PEOPLE. A cheap model screens each one ("a request we could do online, for Kyle / the bot / the
# team?"); likely requests are marked 📥 and sent to EXEC_ROUTE (ascendpoint/ads-assistant#exec-request), which answers
# or does it. Replies in a thread that robot answered (its footer carries ⚡) from the people or the approvers go
# straight there (follow-ups, "yes"/"no"). Chatter is never touched: no reaction, no reply. Logs carry only
# timestamps and the verdict (this repo is public).
EXEC_MARK = "⚡"
EXEC_MAX_AGE = 6 * 3600            # never pick up anything older (first start, long gaps)
EXEC_LOOKBACK = 15 * 60            # on (re)start, re-screen only the last 15 minutes
EXEC_CHANNELS_EVERY = 600          # re-list the bot's channels every 10 minutes
TRIAGE_URL = "https://api.anthropic.com/v1/messages"
TRIAGE_SYSTEM = """You screen Slack messages for AscendPoint AI, the assistant Kyle Robins (Growth Director, SERP Dental /
AscendPoint Agency) built so that when Patrick Hounsell (Co-CEO) or Sonia Hounsell (co-owner) ask for something in
Slack, it gets done online with no human: live numbers from Meta ads, Zoom webinars, GoHighLevel CRM (leads, calls,
pipeline, texts/emails to contacts), Typeform; changes in those tools; website edits (serpdental.com,
ascendpoint.agency); social posts; strategy-session prep (prospect audit + deck); research and drafting.

Answer act=true when the message asks for information or an action (explicitly or implicitly, e.g. "what's the count
for today?", "can we get the webinar on the homepage", "send Dr. Lee the replay", "how did the ads do this week",
"we need a post about X") AND it is addressed to Kyle, to the assistant, to the team / nobody in particular.
Answer act=false for: chit-chat, thanks, praise, reactions ("lol", "awesome"), FYIs and status updates, opinions,
questions or tasks clearly aimed at someone other than Kyle (e.g. "@Adam did you call him?"), personal / HR / money
between people, things only a person can do in the physical world, and messages that are just a reply in a human
conversation that doesn't ask for anything.
Reply with JSON only: {"act": true|false}"""


def _ids(raw: str | None) -> set[str]:
    return {p.strip().partition("=")[0].strip() for p in (raw or "").split(",") if p.strip().partition("=")[0].strip()}


def exec_config() -> dict | None:
    route = (os.environ.get("EXEC_ROUTE") or "").strip()
    people = _ids(os.environ.get("EXEC_PEOPLE"))
    if not route or not people or split_route(route)[0].count("/") != 1:
        return None
    owner = os.environ.get("ROUTER_OWNER") or DEFAULT_OWNER
    skip = _ids(os.environ.get("EXEC_SKIP")) | _ids(os.environ.get("SITE_CHANNEL"))
    return {"route": route, "people": people, "followers": people | {owner} | _ids(os.environ.get("EXEC_APPROVERS")),
            "skip": skip, "owner": owner, "poll": float(os.environ.get("EXEC_POLL_SECONDS") or 60)}


def exec_channels(slack, routes: dict, cfg: dict, cache: dict, now: float) -> list[str]:
    """Channels (and DMs, when the app may read them) the bot is in, minus the ones a robot already owns."""
    if now - cache.get("exec_listed", 0) < EXEC_CHANNELS_EVERY and "exec_channels" in cache:
        return cache["exec_channels"]
    types = cache.get("exec_types", "public_channel,private_channel,im")
    ids, cursor = [], None
    try:
        while True:
            r = slack.call("users.conversations", types=types, exclude_archived="true", limit=200, cursor=cursor)
            ids += [c["id"] for c in r.get("channels", []) if not c.get("is_archived")]
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        if "missing_scope" in str(e) and "im" in types:      # no im:read yet: watch channels only
            cache["exec_types"] = "public_channel,private_channel"
            return exec_channels(slack, routes, cfg, cache, now)
        print(f"exec: listing channels failed ({type(e).__name__})", file=sys.stderr, flush=True)
        return cache.get("exec_channels", [])
    out = [c for c in ids if c not in routes and c not in cfg["skip"]]
    cache["exec_channels"], cache["exec_listed"] = out, now
    return out


def _human(m: dict) -> bool:
    return m.get("type") == "message" and m.get("subtype") in (None, "file_share", "thread_broadcast") \
        and bool(m.get("user")) and not m.get("bot_id") and bool((m.get("text") or "").strip() or m.get("files"))


THANKS_RE = re.compile(r"^\W*(thanks?( you)?|thx|ty|great|awesome|perfect|nice|cool|got it|love it|lol|"
                       r"amazing|beautiful|sweet|:[a-z0-9_+-]+:)\W*$", re.I)


def exec_thread(thread: list[dict], bot_user: str) -> bool:
    """A thread the exec robot answered in (its footer carries ⚡)."""
    return any(x.get("user") == bot_user and EXEC_MARK in (x.get("text") or "") for x in thread)


def exec_pending(slack, channel: str, cfg: dict, bot_user: str, oldest: float, cache: dict) -> list[dict]:
    """New messages from the watched people (to screen) and follow-ups in exec threads (sent as-is), oldest first."""
    quiet = cache.setdefault(("exec_quiet", channel), {})
    seen = cache.setdefault("exec_seen", set())
    hist = slack.call("conversations.history", channel=channel, oldest=f"{oldest:.6f}", limit=200)
    out = []
    for m in hist.get("messages", []):
        if _human(m) and m.get("user") in cfg["people"] and m["ts"] not in seen and not done_by_bot(m, bot_user) \
                and (not m.get("thread_ts") or m.get("thread_ts") == m["ts"]):
            out.append({"ts": m["ts"], "thread_ts": None, "screen": True, "msg": m})
        if m.get("reply_count"):
            marker = m.get("latest_reply") or str(m.get("reply_count"))
            if quiet.get(m["ts"]) == marker:
                continue
            thread = slack.call("conversations.replies", channel=channel, ts=m["ts"], limit=200).get("messages", [])
            ours = exec_thread(thread, bot_user)
            for r in thread[1:]:
                if float(r["ts"]) < oldest or r["ts"] in seen or not _human(r) or done_by_bot(r, bot_user):
                    continue
                if ours and r.get("user") in cfg["followers"]:
                    if THANKS_RE.match(r.get("text") or "") and not r.get("files"):
                        seen.add(r["ts"])            # "thanks!" under our answer needs nothing
                        continue
                    out.append({"ts": r["ts"], "thread_ts": m["ts"], "screen": False, "msg": r})
                elif r.get("user") in cfg["people"]:
                    out.append({"ts": r["ts"], "thread_ts": m["ts"], "screen": True, "msg": r, "thread": thread})
            quiet[m["ts"]] = marker
    return sorted(out, key=lambda x: float(x["ts"]))


def _name(slack, uid: str | None, cache: dict) -> str:
    names = cache.setdefault("exec_names", {})
    if uid and uid not in names:
        try:
            u = slack.call("users.info", user=uid)["user"]
            names[uid] = u.get("real_name") or (u.get("profile") or {}).get("real_name") or u.get("name") or uid
        except Exception:
            names[uid] = uid
    return names.get(uid or "", "someone")


def _plain(slack, text: str, cache: dict, bot_user: str) -> str:
    text = (text or "").replace(f"<@{bot_user}>", "@AscendPoint AI")
    return re.sub(r"<@([UW][A-Z0-9]+)>", lambda m: "@" + _name(slack, m.group(1), cache), text)


ASK_RE = re.compile(r"\?|^\s*(can|could|would|will|pls|please|let'?s|need|we need|pull|send|post|add|remove|change|"
                    r"update|make|create|build|set up|setup|book|schedule|text|email|find|check|get|give|show|what|how|"
                    r"when|who|where|which|is there|are there|do we|did we)\b", re.I)


def heuristic(text: str, bot_user: str, owner: str) -> bool:
    """Fallback screen when the model can't be reached: a question or an ask, addressed to Kyle / the bot / nobody."""
    others = set(re.findall(r"<@([UW][A-Z0-9]+)>", text or "")) - {bot_user, owner}
    if others and f"<@{owner}>" not in text and f"<@{bot_user}>" not in text:
        return False
    return bool(ASK_RE.search(re.sub(r"<@[UW][A-Z0-9]+>[,:]?\s*", "", text or "")))


def screen(slack, item: dict, channel: str, bot_user: str, cfg: dict, cache: dict, opener=None) -> bool:
    """True when the message looks like a request we could do. Model first (EXEC_TRIAGE_MODEL), heuristic fallback."""
    m = item["msg"]
    if any((f.get("mimetype") or "").startswith(("audio/", "video/")) or f.get("subtype") in ("slack_audio", "slack_video")
           for f in m.get("files") or []):
        return True          # a voice message can't be screened as text: the robot transcribes it and decides
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return heuristic(m.get("text", ""), bot_user, cfg["owner"])
    try:
        if item.get("thread"):
            before = [x for x in item["thread"] if float(x["ts"]) < float(m["ts"])][-5:]
        else:
            before = list(reversed(slack.call("conversations.history", channel=channel, latest=m["ts"],
                                              inclusive="false", limit=4).get("messages", [])))
        ctx = "\n".join(f"{'AscendPoint AI' if x.get('user') == bot_user else (x.get('username') or _name(slack, x.get('user'), cache))}: "
                        f"{_plain(slack, x.get('text', ''), cache, bot_user)[:300]}" for x in before)
        who = _name(slack, m.get("user"), cache)
        user = (f"Earlier messages:\n{ctx or '(none)'}\n\nMessage to screen, from {who}"
                f"{' (has attachments)' if m.get('files') else ''}:\n{_plain(slack, m.get('text', ''), cache, bot_user)[:2000]}")
        body = {"model": os.environ.get("EXEC_TRIAGE_MODEL") or "claude-haiku-4-5", "max_tokens": 20,
                "system": TRIAGE_SYSTEM, "messages": [{"role": "user", "content": user}]}
        hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        if os.environ.get("ANTHROPIC_WORKSPACE_ID"):
            hdrs["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
        req = urllib.request.Request(TRIAGE_URL, data=json.dumps(body).encode(), headers=hdrs, method="POST")
        with (opener or urllib.request.urlopen)(req, timeout=30) as r:
            out = json.loads(r.read().decode())
        text = "".join(c.get("text", "") for c in out.get("content", []) if c.get("type") == "text")
        hit = re.search(r'"act"\s*:\s*(true|false)', text, re.I)
        if hit:
            return hit.group(1).lower() == "true"
        raise ValueError("no verdict")
    except Exception as e:
        print(f"exec screen fell back to rules ({type(e).__name__} {getattr(e, 'code', '')})", file=sys.stderr, flush=True)
        return heuristic(m.get("text", ""), bot_user, cfg["owner"])


def exec_once(slack, routes: dict, token: str, cache: dict, bot_user: str, opener=None, screen_opener=None,
              now: float | None = None, start: float = 0.0) -> int:
    cfg = exec_config()
    if not cfg:
        return 0
    now = now or time.time()
    if now - cache.get("exec_polled", 0) < cfg["poll"]:
        return 0
    cache["exec_polled"] = now
    oldest = max(start, cache.setdefault("exec_start", now - EXEC_LOOKBACK), now - EXEC_MAX_AGE)
    seen = cache.setdefault("exec_seen", set())
    retry_at = cache.setdefault("exec_retry", {})
    sent = 0
    for channel in exec_channels(slack, routes, cfg, cache, now):
        try:
            items = exec_pending(slack, channel, cfg, bot_user, oldest, cache)
        except Exception as e:
            print(f"exec poll failed ({type(e).__name__})", file=sys.stderr, flush=True)
            continue
        for it in items:
            if retry_at.get(it["ts"], 0) > now:
                continue
            if it["screen"]:
                act = screen(slack, it, channel, bot_user, cfg, cache, screen_opener)
                print(f"exec {it['ts']}: {'request' if act else 'skip'}", flush=True)
                if not act:
                    seen.add(it["ts"])
                    continue
            slack.call("reactions.add", channel=channel, timestamp=it["ts"], name=QUEUED)
            status: dict = {}
            if dispatch(cfg["route"], token, {"channel": channel, "ts": it["ts"], "thread_ts": it["thread_ts"]},
                        opener, status):
                seen.add(it["ts"])
                sent += 1
                retry_at.pop(it["ts"], None)
                print(f"queued {it['ts']} -> exec", flush=True)
            else:
                slack.call("reactions.remove", channel=channel, timestamp=it["ts"], name=QUEUED)
                retry_at[it["ts"]] = now + RETRY_SECONDS
                if it["thread_ts"]:          # re-read that thread next time
                    cache.setdefault(("exec_quiet", channel), {}).pop(it["thread_ts"], None)
                told = cache.setdefault("exec_told", set())
                if not told:      # once per router run: tell the owner privately, never in their thread
                    told.add(it["ts"])
                    why = WHY.get(status.get("code", 0), "GitHub couldn't be reached")
                    try:
                        slack.call("chat.postMessage", channel=cfg["owner"], unfurl_links="false",
                                   text=f":warning: Patrick/Sonia requests can't reach `{split_route(cfg['route'])[0]}`: "
                                        f"{why}. I'll keep retrying every minute.")
                    except Exception:
                        pass
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
            social_clock(token, cache)
        except Exception as e:
            print(f"social clock error: {type(e).__name__}", file=sys.stderr, flush=True)
        try:
            total += route_once(slack, routes, token, start=start, cache=cache)
        except Exception as e:
            print(f"router error: {type(e).__name__}", file=sys.stderr, flush=True)
        try:
            if "auth" in cache:
                total += exec_once(slack, routes, token, cache, cache["auth"]["user_id"], start=start)
        except Exception as e:
            print(f"exec error: {type(e).__name__}", file=sys.stderr, flush=True)
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
