"""Tests for tools/request_router.py (no network)."""
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import request_router as rr  # noqa: E402

BOT = "UBOT"
CH = "CSERP"
REPO = "ascendpoint/serpdental-site"


class FakeSlack:
    def __init__(self, history=(), replies=None):
        self.history, self.replies = list(history), replies or {}
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        if method == "auth.test":
            return {"ok": True, "user_id": BOT, "bot_id": "BBOT"}
        if method == "conversations.history":
            return {"ok": True, "messages": [m for m in self.history if float(m["ts"]) >= float(p["oldest"])]}
        if method == "conversations.replies":
            return {"ok": True, "messages": self.replies.get(p["ts"], [])}
        return {"ok": True}

    def reacted(self, method):
        return [(p["timestamp"], p["name"]) for m, p in self.calls if m == method]


class FakeResp:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


def msg(ts, user="UPAT", text="Change the footer phone number", **kw):
    m = {"type": "message", "ts": f"{ts:.6f}", "user": user, "text": text}
    m.update(kw)
    return m


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.sent = []

    def opener(self, status=204):
        def _open(req, timeout=30):
            self.sent.append((req.full_url, json.loads(req.data.decode()), req.headers))
            if status >= 400:
                raise OSError("boom")
            return FakeResp(status)
        return _open

    def test_routes_parse(self):
        self.assertEqual(rr.parse_routes("C1=ascendpoint/serpdental-site, junk, C2=bad"),
                         {"C1": "ascendpoint/serpdental-site"})
        self.assertEqual(rr.parse_routes(""), {})

    def test_new_request_is_marked_and_dispatched_to_its_repo(self):
        s = FakeSlack([msg(self.now - 30)])
        n = rr.route_once(s, {CH: REPO}, "tok", opener=self.opener())
        self.assertEqual(n, 1)
        url, body, headers = self.sent[0]
        self.assertEqual(url, f"https://api.github.com/repos/{REPO}/dispatches")
        self.assertEqual(body["event_type"], "website-request")
        self.assertEqual(body["client_payload"], {"channel": CH, "ts": f"{self.now - 30:.6f}", "thread_ts": None})
        self.assertIn((f"{self.now - 30:.6f}", "inbox_tray"), s.reacted("reactions.add"))
        self.assertNotIn("text", json.dumps(body["client_payload"]))   # no message text leaves Slack

    def test_already_queued_or_handled_is_not_sent_again(self):
        s = FakeSlack([msg(self.now - 30, reactions=[{"name": "inbox_tray", "users": [BOT]}]),
                       msg(self.now - 20, reactions=[{"name": "white_check_mark", "users": [BOT]}]),
                       msg(self.now - 10, user=BOT, text="✅ Live")])
        self.assertEqual(rr.route_once(s, {CH: REPO}, "tok", opener=self.opener()), 0)
        self.assertEqual(self.sent, [])

    def test_preview_states_count_as_handled(self):
        s = FakeSlack([msg(self.now - 30, reactions=[{"name": "mag", "users": [BOT]}]),
                       msg(self.now - 20, reactions=[{"name": "wastebasket", "users": [BOT]}])])
        self.assertEqual(rr.route_once(s, {CH: REPO}, "tok", opener=self.opener()), 0)

    def test_failed_dispatch_removes_marker_for_retry(self):
        s = FakeSlack([msg(self.now - 30)])
        self.assertEqual(rr.route_once(s, {CH: REPO}, "tok", opener=self.opener(500)), 0)
        self.assertIn((f"{self.now - 30:.6f}", "inbox_tray"), s.reacted("reactions.remove"))

    def opener_http(self, code):
        import urllib.error

        def _open(req, timeout=30):
            self.sent.append((req.full_url, json.loads(req.data.decode()), req.headers))
            raise urllib.error.HTTPError(req.full_url, code, "Not Found", {}, None)
        return _open

    def posts(self, s):
        return [p for m, p in s.calls if m == "chat.postMessage"]

    def test_stuck_hand_off_explains_itself_once_and_retries_each_minute(self):
        q = msg(self.now - 30, text="How many registrants for tomorrow?")
        s = FakeSlack([q])
        cache = {}
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener_http(404), now=self.now)
        notes = self.posts(s)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["thread_ts"], q["ts"])
        self.assertIn("can't see that repo", notes[0]["text"])
        self.assertIn("HTTP 404", notes[0]["text"])
        self.assertIn("<@U04Q3M29UKE>", notes[0]["text"])
        self.assertIn((q["ts"], "hourglass_flowing_sand"), s.reacted("reactions.add"))
        self.assertIn((q["ts"], "inbox_tray"), s.reacted("reactions.remove"))     # still retried
        # 10 s later: not retried yet (once a minute), and no second notice
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener_http(404), now=self.now + 10)
        self.assertEqual(len(self.sent), 1)
        # 70 s later: retried, still failing, still only one notice
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener_http(404), now=self.now + 70)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.posts(s)), 1)
        # fixed: next retry goes through and the hourglass comes off
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener(), now=self.now + 140)
        self.assertEqual(len(self.sent), 3)
        self.assertIn((q["ts"], "hourglass_flowing_sand"), s.reacted("reactions.remove"))

    def test_stuck_notice_not_repeated_after_router_restart(self):
        q = msg(self.now - 30)
        note = msg(self.now - 20, user=BOT, text=f":warning: I saw this, but {rr.NOTICE_MARK} `x` robot", thread_ts=q["ts"])
        s = FakeSlack([q], replies={q["ts"]: [q, note]})
        rr.route_once(s, {CH: REPO}, "tok", cache={}, opener=self.opener_http(404), now=self.now)
        self.assertEqual(self.posts(s), [])

    def test_hand_off_that_never_starts_is_reported_once(self):
        q = msg(self.now - 30)
        s = FakeSlack([q])
        cache = {}
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener(), now=self.now)
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener(), now=self.now + 100)
        self.assertEqual(self.posts(s), [])                     # still within 5 minutes
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener(), now=self.now + 400)
        notes = self.posts(s)
        self.assertEqual(len(notes), 1)
        self.assertIn("hasn't started", notes[0]["text"])
        self.assertIn("SLACK_BOT_TOKEN", notes[0]["text"])
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener(), now=self.now + 800)
        self.assertEqual(len(self.posts(s)), 1)

    def test_hand_off_that_started_is_quiet(self):
        q = msg(self.now - 30)
        s = FakeSlack([q])
        orig = s.call

        def call(method, **p):
            if method == "reactions.get":
                return {"ok": True, "message": {"reactions": [{"name": "eyes", "users": [BOT]}]}}
            return orig(method, **p)
        s.call = call
        cache = {}
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener(), now=self.now)
        rr.route_once(s, {CH: REPO}, "tok", cache=cache, opener=self.opener(), now=self.now + 400)
        self.assertEqual(self.posts(s), [])

    def test_thread_follow_up_is_sent_with_thread_ts(self):
        top = msg(self.now - 100, reactions=[{"name": "white_check_mark", "users": [BOT]}], reply_count=2,
                  latest_reply=f"{self.now - 10:.6f}")
        bot = msg(self.now - 90, user=BOT, text="✅ Live", thread_ts=top["ts"])
        undo = msg(self.now - 10, text="undo", thread_ts=top["ts"])
        s = FakeSlack([top], replies={top["ts"]: [top, bot, undo]})
        rr.route_once(s, {CH: REPO}, "tok", opener=self.opener())
        self.assertEqual([b["client_payload"] for _, b, _ in self.sent],
                         [{"channel": CH, "ts": undo["ts"], "thread_ts": top["ts"]}])

    def test_any_thread_channel_counts_replies_under_other_bots(self):
        import os
        from unittest import mock
        top = msg(self.now - 100, user=None, bot_id="BZAPIER", text="Daily Funnel Report", reply_count=1,
                  latest_reply=f"{self.now - 10:.6f}")
        q = msg(self.now - 10, text="why is CPL up?", thread_ts=top["ts"])
        chat = msg(self.now - 80, reply_count=1, latest_reply=f"{self.now - 5:.6f}",
                   reactions=[{"name": "white_check_mark", "users": [BOT]}])
        chat_reply = msg(self.now - 5, text="lunch?", thread_ts=chat["ts"])
        s = FakeSlack([top, chat], replies={top["ts"]: [top, q], chat["ts"]: [chat, chat_reply]})
        with mock.patch.dict(os.environ, {"ROUTER_ANY_THREAD": "CADS"}):
            rr.route_once(s, {"CADS": "ascendpoint/ads-assistant"}, "tok", opener=self.opener())
        payloads = [b["client_payload"] for _, b, _ in self.sent]
        self.assertIn({"channel": "CADS", "ts": q["ts"], "thread_ts": top["ts"]}, payloads)
        self.assertNotIn(chat_reply["ts"], [p["ts"] for p in payloads])     # people chatting among themselves
        # without the setting, only threads this bot is in count
        self.sent.clear()
        s2 = FakeSlack([top], replies={top["ts"]: [top, q]})
        with mock.patch.dict(os.environ, {"ROUTER_ANY_THREAD": ""}):
            rr.route_once(s2, {"CADS": "ascendpoint/ads-assistant"}, "tok", opener=self.opener())
        self.assertEqual([b["client_payload"]["ts"] for _, b, _ in self.sent], [])

    def test_wake_channel_only_answers_when_called(self):
        import os
        from unittest import mock
        chat = msg(self.now - 90, text="Patrick, are you on the 2pm?")
        named = msg(self.now - 80, text="how do i activate the agent?")
        mention = msg(self.now - 70, text=f"<@{BOT}> build the deck for Dr. Hayat")
        brand = msg(self.now - 60, text="AscendPoint AI what is on Adam's calendar")
        agency = msg(self.now - 50, text="the ascendpoint agency logo")
        s = FakeSlack([chat, named, mention, brand, agency])
        with mock.patch.dict(os.environ, {"ROUTER_WAKE_CHANNELS": CH}):
            rr.route_once(s, {CH: REPO}, "tok", opener=self.opener())
        self.assertEqual(sorted(b["client_payload"]["ts"] for _, b, _ in self.sent),
                         sorted([named["ts"], mention["ts"], brand["ts"]]))
        # not a wake channel: everything counts, as before
        self.sent.clear()
        s2 = FakeSlack([chat, named])
        with mock.patch.dict(os.environ, {"ROUTER_WAKE_CHANNELS": "COTHER"}):
            rr.route_once(s2, {CH: REPO}, "tok", opener=self.opener())
        self.assertEqual(len(self.sent), 2)

    def test_wake_channel_threads(self):
        import os
        from unittest import mock
        # a teammate's thread: a reply that calls the bot counts, a reply that doesn't, doesn't
        top = msg(self.now - 200, text="Hayat session notes", reply_count=2, latest_reply=f"{self.now - 10:.6f}")
        plain = msg(self.now - 100, text="thanks Adam", thread_ts=top["ts"])
        call = msg(self.now - 10, text=f"<@{BOT}> redo it", thread_ts=top["ts"])
        # a thread the bot is already in: every human reply is a follow-up
        top2 = msg(self.now - 300, text=f"<@{BOT}> deck?", reactions=[{"name": "white_check_mark", "users": [BOT]}],
                   reply_count=2, latest_reply=f"{self.now - 5:.6f}")
        bot2 = msg(self.now - 250, user=BOT, text="done", thread_ts=top2["ts"])
        follow = msg(self.now - 5, text="make it shorter", thread_ts=top2["ts"])
        s = FakeSlack([top, top2], replies={top["ts"]: [top, plain, call], top2["ts"]: [top2, bot2, follow]})
        with mock.patch.dict(os.environ, {"ROUTER_WAKE_CHANNELS": CH}):
            rr.route_once(s, {CH: REPO}, "tok", opener=self.opener())
        self.assertEqual(sorted(b["client_payload"]["ts"] for _, b, _ in self.sent),
                         sorted([call["ts"], follow["ts"]]))

    def test_only_routed_channels_are_read(self):
        s = FakeSlack([msg(self.now - 30)])
        rr.route_once(s, {CH: REPO}, "tok", opener=self.opener())
        channels = {p.get("channel") for m, p in s.calls if m.startswith("conversations.")}
        self.assertEqual(channels, {CH})   # never the AscendPoint #website-requests channel

    def test_listen_restarts_into_new_code(self):
        from unittest import mock
        t = {"now": 0.0}
        s = FakeSlack([])
        with mock.patch.object(rr, "code_changed", return_value=True), \
                mock.patch.object(rr.os, "execv", side_effect=SystemExit("restarted")) as ex, \
                mock.patch.dict(rr.os.environ, {}, clear=False):
            with self.assertRaises(SystemExit):
                rr.listen(s, {CH: REPO}, "tok", minutes=60, poll=400, sleep=lambda x: t.update(now=t["now"] + x),
                          clock=lambda: t["now"], watch_code=True)
            self.assertEqual(ex.call_args.args[1][-1], "listen")
            self.assertEqual(float(rr.os.environ["LISTEN_UNTIL"]), 3600.0)   # keeps the original end time
            rr.os.environ.pop("LISTEN_UNTIL", None)

    def test_not_configured_is_a_no_op(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "", "ROUTES": "", "DISPATCH_TOKEN": ""}):
            self.assertEqual(rr.main(["listen"]), 0)


if __name__ == "__main__":
    unittest.main()


class SocialRouteTests(unittest.TestCase):
    """#social-requests -> ascendpoint/ads-assistant#social-request, the social clock, and watched calendar threads."""

    def setUp(self):
        self.now = time.time()
        self.sent = []

    def opener(self, schedule=None, etag='"e1"', status=204):
        def _open(req, timeout=30):
            if "/contents/" in req.full_url:
                self.sent.append(("GET", req.full_url, dict(req.headers)))
                if schedule is None:
                    import urllib.error
                    raise urllib.error.HTTPError(req.full_url, 304, "not modified", {}, None)
                r = FakeResp(200)
                r.read = lambda: json.dumps(schedule).encode()
                r.headers = {"ETag": etag}
                return r
            self.sent.append(("POST", req.full_url, json.loads(req.data.decode())))
            return FakeResp(status)
        return _open

    def test_route_with_event_type(self):
        routes = rr.parse_routes("C1=ascendpoint/ads-assistant#social-request,C2=ascendpoint/serpdental-site")
        self.assertEqual(routes["C1"], "ascendpoint/ads-assistant#social-request")
        self.assertEqual(rr.split_route(routes["C2"]), ("ascendpoint/serpdental-site", "website-request"))
        s = FakeSlack([msg(self.now - 5, text="approve 1-8")])
        rr.route_once(s, {"C1": routes["C1"]}, "tok", opener=self.opener())
        _, url, body = self.sent[0]
        self.assertEqual(url, "https://api.github.com/repos/ascendpoint/ads-assistant/dispatches")
        self.assertEqual(body["event_type"], "social-request")
        self.assertEqual(body["client_payload"]["channel"], "C1")

    def test_clock_fires_due_items_once(self):
        import os
        os.environ["SOCIAL_SCHEDULE"] = "ascendpoint/ads-assistant:content/social/schedule.json"
        try:
            sched = {"due": [{"at": int(self.now) - 5, "key": "a@1", "why": "publish"},
                             {"at": int(self.now) + 600, "key": "b@2", "why": "confirm"}],
                     "threads": [{"channel": "C1", "ts": "1700000000.000100"}]}
            cache = {}
            self.assertEqual(rr.social_clock("tok", cache, self.opener(sched), now=self.now), 1)
            get = self.sent[0]
            self.assertIn("/repos/ascendpoint/ads-assistant/contents/content/social/schedule.json?ref=main", get[1])
            post = self.sent[1]
            self.assertEqual(post[2]["event_type"], "social-tick")
            self.assertEqual(post[2]["client_payload"]["keys"], ["a@1"])
            self.assertEqual(cache["watch_threads"], sched["threads"])
            # within the minute: nothing; a minute later with 304: same schedule, a@1 not re-sent, b not due yet
            self.assertEqual(rr.social_clock("tok", cache, self.opener(None), now=self.now + 10), 0)
            self.assertEqual(rr.social_clock("tok", cache, self.opener(None), now=self.now + 70), 0)
            self.assertEqual(self.sent[-1][2].get("If-None-match") or self.sent[-1][2].get("If-none-match"), '"e1"')
            # ten minutes later b is due
            self.assertEqual(rr.social_clock("tok", cache, self.opener(None), now=self.now + 700), 1)
            self.assertEqual(self.sent[-1][2]["client_payload"]["keys"], ["b@2"])
        finally:
            os.environ.pop("SOCIAL_SCHEDULE", None)

    def test_clock_off_without_variable(self):
        self.assertEqual(rr.social_clock("tok", {}, self.opener({"due": []}), now=self.now), 0)
        self.assertEqual(self.sent, [])

    def test_failed_tick_dispatch_is_retried(self):
        import os
        os.environ["SOCIAL_SCHEDULE"] = "o/r:s.json"
        try:
            cache = {}
            sched = {"due": [{"at": int(self.now) - 5, "key": "a@1"}]}
            def bad(req, timeout=30):
                if "/contents/" in req.full_url:
                    return self.opener(sched)(req, timeout)
                raise OSError("down")
            self.assertEqual(rr.social_clock("tok", cache, bad, now=self.now), 0)
            self.assertEqual(rr.social_clock("tok", cache, self.opener(None), now=self.now + 61), 1)
        finally:
            os.environ.pop("SOCIAL_SCHEDULE", None)

    def test_watched_calendar_thread_reply_older_than_two_days(self):
        root = f"{self.now - 10 * 86400:.6f}"
        reply = msg(self.now - 20, text="approve 3", thread_ts=root)
        s = FakeSlack(history=[], replies={root: [msg(float(root), user=BOT, text="calendar"), reply]})
        cache = {"auth": {"user_id": BOT, "bot_id": "BBOT"}, "watch_threads": [{"channel": "C1", "ts": root}]}
        n = rr.route_once(s, {"C1": "ascendpoint/ads-assistant#social-request"}, "tok", cache=cache,
                          opener=self.opener())
        self.assertEqual(n, 1)
        self.assertEqual(self.sent[0][2]["client_payload"], {"channel": "C1", "ts": reply["ts"], "thread_ts": root})
        # handled (bot reacted) -> not sent again
        reply["reactions"] = [{"name": "white_check_mark", "users": [BOT]}]
        cache["watch_polled"] = {}
        self.assertEqual(rr.route_once(s, {"C1": "ascendpoint/ads-assistant#social-request"}, "tok", cache=cache,
                                       opener=self.opener()), 0)


# ----------------------------------------------------------------------------- Patrick & Sonia watch
import os  # noqa: E402
from unittest import mock  # noqa: E402

PAT, SONIA, KYLE, ADAM = "U06D77DKB88", "U07TJCUD6JC", "U04Q3M29UKE", "UADAM"
LEADS = "CLEADS"
EXEC_ENV = {"EXEC_ROUTE": "ascendpoint/ads-assistant#exec-request", "EXEC_PEOPLE": f"{PAT}=Patrick,{SONIA}",
            "ROUTER_OWNER": KYLE, "EXEC_SKIP": "CSKIP", "SITE_CHANNEL": "CSITE", "EXEC_POLL_SECONDS": "60",
            "ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_WORKSPACE_ID": "wrkspc_1"}


class ExecSlack(FakeSlack):
    def __init__(self, history=(), replies=None, channels=(LEADS,), im_scope=True):
        super().__init__(history, replies)
        self.channels, self.im_scope = list(channels), im_scope
        self.names = {PAT: "Patrick Hounsell", SONIA: "Sonia Hounsell", KYLE: "Kyle Robins", ADAM: "Adam Callahan"}

    def call(self, method, **p):
        if method == "users.conversations":
            self.calls.append((method, p))
            if "im" in p["types"] and not self.im_scope:
                raise RuntimeError("slack users.conversations: missing_scope im:read")
            return {"ok": True, "channels": [{"id": c} for c in self.channels]}
        if method == "users.info":
            self.calls.append((method, p))
            return {"ok": True, "user": {"real_name": self.names.get(p["user"], p["user"])}}
        if method == "conversations.history" and "latest" in p:
            self.calls.append((method, p))
            msgs = [m for m in self.history if float(m["ts"]) < float(p["latest"])]
            return {"ok": True, "messages": sorted(msgs, key=lambda m: -float(m["ts"]))[:int(p.get("limit", 100))]}
        return super().call(method, **p)


class ExecWatchTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.sent, self.screens = [], []
        self.verdict = True
        self.env = mock.patch.dict(os.environ, EXEC_ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def opener(self, status=204):
        def _open(req, timeout=30):
            self.sent.append((req.full_url, json.loads(req.data.decode()), req.headers))
            if status >= 400:
                import urllib.error
                raise urllib.error.HTTPError(req.full_url, status, "err", {}, None)
            return FakeResp(status)
        return _open

    def screen_opener(self, req, timeout=30):
        body = json.loads(req.data.decode())
        self.screens.append((body, dict(req.headers)))
        if self.verdict is None:
            raise OSError("api down")

        class R(FakeResp):
            def read(s):
                return json.dumps({"content": [{"type": "text", "text": json.dumps({"act": self.verdict})}]}).encode()
        return R(200)

    def run_once(self, slack, routes=None, cache=None, now=None, status=204):
        cache = cache if cache is not None else {}
        n = rr.exec_once(slack, routes or {CH: REPO}, "tok", cache, BOT, opener=self.opener(status),
                         screen_opener=self.screen_opener, now=now or self.now)
        return n, cache

    def test_config_needs_route_and_people(self):
        self.assertIsNotNone(rr.exec_config())
        cfg = rr.exec_config()
        self.assertEqual(cfg["people"], {PAT, SONIA})
        self.assertEqual(cfg["followers"], {PAT, SONIA, KYLE})
        self.assertEqual(cfg["skip"], {"CSKIP", "CSITE"})
        with mock.patch.dict(os.environ, {"EXEC_ROUTE": ""}):
            self.assertIsNone(rr.exec_config())
        s = ExecSlack([msg(self.now - 30, user=PAT, text="what's the count?")])
        with mock.patch.dict(os.environ, {"EXEC_PEOPLE": ""}):
            self.assertEqual(self.run_once(s)[0], 0)
        self.assertEqual(self.sent, [])

    def test_patrick_question_is_screened_marked_and_sent_to_the_exec_robot(self):
        q = msg(self.now - 30, user=PAT, text=f"<@{KYLE}> what's the count for today?")
        zap = msg(self.now - 120, user=None, text="New Webinar Registrant: Gen Mann", bot_id="BZAP", username="Zapier")
        s = ExecSlack([zap, q])
        n, cache = self.run_once(s)
        self.assertEqual(n, 1)
        url, body, _ = self.sent[0]
        self.assertEqual(url, "https://api.github.com/repos/ascendpoint/ads-assistant/dispatches")
        self.assertEqual(body, {"event_type": "exec-request",
                                "client_payload": {"channel": LEADS, "ts": q["ts"], "thread_ts": None}})
        self.assertEqual(s.reacted("reactions.add"), [(q["ts"], "inbox_tray")])
        screen, hdrs = self.screens[0]
        self.assertEqual(screen["model"], "claude-haiku-4-5")
        text = screen["messages"][0]["content"]
        self.assertIn("from Patrick Hounsell", text)
        self.assertIn("@Kyle Robins what's the count for today?", text)        # mentions resolved to names
        self.assertIn("Zapier: New Webinar Registrant", text)                  # what came before
        self.assertEqual(hdrs.get("Anthropic-workspace-id"), "wrkspc_1")
        self.assertEqual(hdrs.get("X-api-key"), "sk-test")
        # next minute: nothing new, nothing re-screened or re-sent
        self.run_once(s, cache=cache, now=self.now + 61)
        self.assertEqual((len(self.sent), len(self.screens)), (1, 1))

    def test_chatter_is_left_alone_and_not_rescreened(self):
        self.verdict = False
        s = ExecSlack([msg(self.now - 30, user=SONIA, text="that's where i put it")])
        n, cache = self.run_once(s)
        self.assertEqual((n, self.sent, s.reacted("reactions.add")), (0, [], []))
        self.run_once(s, cache=cache, now=self.now + 61)
        self.assertEqual(len(self.screens), 1)

    def test_other_people_and_bots_are_never_screened(self):
        s = ExecSlack([msg(self.now - 30, user=ADAM, text="can you pull the count?"),
                       msg(self.now - 20, user=None, text="can you pull the count?", bot_id="BZAP"),
                       msg(self.now - 10, user=PAT, subtype="channel_join", text="joined")])
        self.assertEqual(self.run_once(s)[0], 0)
        self.assertEqual(self.screens, [])

    def test_poll_interval_and_lookback(self):
        old = msg(self.now - 3600, user=PAT, text="what's the count?")       # before this router started
        s = ExecSlack([old])
        n, cache = self.run_once(s)
        self.assertEqual(n, 0)
        s.history.append(msg(self.now + 5, user=PAT, text="and yesterday's?"))
        self.assertEqual(self.run_once(s, cache=cache, now=self.now + 10)[0], 0)    # < 60 s since the last poll
        self.assertEqual(self.run_once(s, cache=cache, now=self.now + 61)[0], 1)

    def test_routed_skipped_and_dm_channels(self):
        s = ExecSlack([], channels=[LEADS, CH, "CSKIP", "CSITE", "DPAT"], im_scope=False)
        cfg = rr.exec_config()
        self.assertEqual(rr.exec_channels(s, {CH: REPO}, cfg, {}, self.now), [LEADS, "DPAT"])
        types = [p["types"] for m, p in s.calls if m == "users.conversations"]
        self.assertEqual(types, ["public_channel,private_channel,im", "public_channel,private_channel"])

    def test_follow_ups_in_our_thread_skip_the_screen_and_thanks_is_ignored(self):
        top = msg(self.now - 300, user=PAT, text="what's the count?",
                  reactions=[{"name": "white_check_mark", "users": [BOT]}], reply_count=4, latest_reply="x")
        ans = msg(self.now - 280, user=BOT, text=f"*41 registered*\n_{rr.EXEC_MARK} AscendPoint AI · Sonnet · 9s_",
                  thread_ts=top["ts"])
        yes = msg(self.now - 60, user=KYLE, text="yes", thread_ts=top["ts"])
        thx = msg(self.now - 50, user=PAT, text="thanks!", thread_ts=top["ts"])
        adam = msg(self.now - 40, user=ADAM, text="can you also pull last week?", thread_ts=top["ts"])
        s = ExecSlack([top], replies={top["ts"]: [top, ans, yes, thx, adam]})
        n, _ = self.run_once(s)
        self.assertEqual(n, 1)
        self.assertEqual(self.sent[0][1]["client_payload"], {"channel": LEADS, "ts": yes["ts"], "thread_ts": top["ts"]})
        self.assertEqual(self.screens, [])

    def test_patrick_reply_in_a_human_thread_is_screened(self):
        top = msg(self.now - 300, user=ADAM, text="Dr. Lee booked", reply_count=1, latest_reply="x")
        r = msg(self.now - 30, user=PAT, text="can we text him the replay link?", thread_ts=top["ts"])
        s = ExecSlack([top], replies={top["ts"]: [top, r]})
        self.assertEqual(self.run_once(s)[0], 1)
        self.assertEqual(self.sent[0][1]["client_payload"]["thread_ts"], top["ts"])
        self.assertIn("Adam Callahan: Dr. Lee booked", self.screens[0][0]["messages"][0]["content"])

    def test_voice_message_goes_through_unscreened(self):
        self.verdict = False
        clip = {"id": "F1", "mimetype": "audio/mp4", "subtype": "slack_audio"}
        s = ExecSlack([msg(self.now - 30, user=PAT, text="", files=[clip])])
        self.assertEqual(self.run_once(s)[0], 1)
        self.assertEqual(self.screens, [])

    def test_model_down_falls_back_to_rules(self):
        self.verdict = None
        s = ExecSlack([msg(self.now - 30, user=PAT, text="what's the count for today?"),
                       msg(self.now - 20, user=SONIA, text=f"<@{ADAM}> did you call him?"),
                       msg(self.now - 10, user=SONIA, text="love this deck")])
        self.assertEqual(self.run_once(s)[0], 1)
        self.assertTrue(rr.heuristic(f"<@{KYLE}> can you pull it", BOT, KYLE))
        self.assertTrue(rr.heuristic("please add the banner", BOT, KYLE))
        self.assertFalse(rr.heuristic("awesome week team", BOT, KYLE))

    def test_failed_hand_off_tells_owner_privately_once_and_retries(self):
        s = ExecSlack([msg(self.now - 30, user=PAT, text="what's the count?"),
                       msg(self.now - 20, user=SONIA, text="how many booked calls today?")])
        n, cache = self.run_once(s, status=404)
        self.assertEqual(n, 0)
        dms = [p for m, p in s.calls if m == "chat.postMessage"]
        self.assertEqual(len(dms), 1)
        self.assertEqual(dms[0]["channel"], KYLE)
        self.assertIn("can't see that repo", dms[0]["text"])
        self.assertEqual(s.reacted("reactions.remove"), [(s.history[0]["ts"], "inbox_tray"), (s.history[1]["ts"], "inbox_tray")])
        n, _ = self.run_once(s, cache=cache, now=self.now + 61)
        self.assertEqual(n, 2)


class HandoffRoutingTests(unittest.TestCase):
    HEAD = "\U0001f4e8 *Request from Patrick Hounsell* (<@UPAT>, via AscendPoint AI)"

    def test_handoff_post_in_a_routed_channel_is_dispatched(self):
        now = time.time()
        sent = []
        def opener(req, timeout=30):
            sent.append(json.loads(req.data.decode()))
            return FakeResp()
        s = FakeSlack([msg(now - 20, user=BOT, text=self.HEAD + "\nChange the hero headline"),
                       msg(now - 10, user=BOT, text="✅ Live")])
        self.assertEqual(rr.route_once(s, {CH: REPO}, "tok", opener=opener), 1)
        self.assertEqual(sent[0]["client_payload"]["ts"], f"{now - 20:.6f}")

    def test_handoff_wakes_a_wake_only_channel(self):
        self.assertTrue(rr.wakes(msg(1.0, user=BOT, text=self.HEAD + "\nbuild the deck for Dr. Lee"), BOT))
        self.assertFalse(rr.wakes(msg(1.0, user=BOT, text="\u2705 Deck is ready"), BOT))
        # a bot reply that quotes the header is not a request at all (is_request runs before wakes)
        self.assertFalse(rr.wr.is_request(msg(1.0, user=BOT, text=self.HEAD + "\nx", thread_ts="0.5"), BOT, "BBOT"))
