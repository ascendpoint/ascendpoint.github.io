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
