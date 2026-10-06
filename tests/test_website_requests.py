"""Tests for tools/website_requests.py (no network: Slack and Claude are faked).

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import website_requests as wr  # noqa: E402

BOT = "UBOT"


def png_bytes(size=(600, 800), colour=(40, 60, 120)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


PNG_BYTES = png_bytes()
NOW = 1_800_000_000.0


class FakeSlack:
    """Answers the handful of Slack Web API methods the robot uses."""

    def __init__(self, history, replies=None, users=None):
        self.history, self.replies, self.users = history, replies or {}, users or {}
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        if method == "auth.test":
            return {"ok": True, "user_id": BOT, "bot_id": "BBOT"}
        if method == "conversations.history":
            oldest = float(p.get("oldest", 0))
            return {"ok": True, "messages": [m for m in self.history if float(m["ts"]) >= oldest]}
        if method == "conversations.replies":
            return {"ok": True, "messages": self.replies.get(p["ts"], [])}
        if method == "users.info":
            return {"ok": True, "user": {"real_name": self.users.get(p["user"], p["user"])}}
        return {"ok": True}

    def download(self, url, dest):
        dest.write_bytes(b"fake")

    def file_url(self, f):
        return f.get("url") or "https://files/x"


def msg(ts, user="UPAT", text="Change the hero headline", **kw):
    m = {"type": "message", "ts": f"{ts:.6f}", "user": user, "text": text}
    m.update(kw)
    return m


class SelectionTests(unittest.TestCase):
    def test_picks_oldest_unhandled_top_level(self):
        h = [msg(NOW - 50, text="second"), msg(NOW - 100, text="first"),
             msg(NOW - 200, text="done", reactions=[{"name": "white_check_mark", "users": [BOT]}])]
        req = wr.find_next(FakeSlack(h, users={"UPAT": "Patrick Hounsell"}), "C1", now=NOW)
        self.assertEqual(req["text"], "first")
        self.assertEqual(req["kind"], "new")
        self.assertEqual(req["requester"], "Patrick Hounsell")

    def test_reaction_by_someone_else_does_not_count_as_handled(self):
        h = [msg(NOW - 10, reactions=[{"name": "eyes", "users": ["USONIA"]}])]
        self.assertIsNotNone(wr.find_next(FakeSlack(h), "C1", now=NOW))

    def test_ignores_bots_joins_chatter_and_old_messages(self):
        h = [msg(NOW - 10, user=BOT, text="✅ Live"),
             msg(NOW - 20, user=None, text="robot", bot_id="BOTHER", subtype="bot_message"),
             msg(NOW - 30, subtype="channel_join", text="joined"),
             msg(NOW - 40, text="// just chatting"),
             msg(NOW - 3 * 24 * 3600, text="too old")]
        self.assertIsNone(wr.find_next(FakeSlack(h), "C1", now=NOW))

    def test_start_variable_hides_history(self):
        h = [msg(NOW - 100)]
        self.assertIsNone(wr.find_next(FakeSlack(h), "C1", now=NOW, start=NOW - 50))

    def test_zapier_email_post_is_a_request(self):
        h = [msg(NOW - 10, user=None, bot_id="BZAP", subtype="bot_message",
                 text="📧 Email request from Sonia Hounsell <sonia@ascendpoint.agency>: Fix the typo on About")]
        req = wr.find_next(FakeSlack(h), "C1", now=NOW)
        self.assertEqual(req["requester"], "Sonia Hounsell <sonia@ascendpoint.agency> (email)")
        self.assertEqual(req["kind"], "blocked")                 # email requests are switched off

    def email_msg(self, ts, address, name="Sonia Hounsell", subject="Fix the typo on About",
                  body="Change 'teh' to 'the' in the first paragraph.", **kw):
        f = {"id": "F1", "mode": "email", "filetype": "email", "title": subject, "subject": subject,
             "from": [{"address": address, "name": name, "original": f"{name} <{address}>"}],
             "plain_text": body, "url_private": "https://files/email"}
        return msg(ts, text="", files=[f], **kw)

    def test_slack_channel_email_is_a_request(self):
        h = [self.email_msg(NOW - 10, "sonia@ascendpoint.agency", subtype="file_share", user="UKYLE")]
        req = wr.find_next(FakeSlack(h), "C1", now=NOW, allowed={"UPAT"})
        self.assertEqual(req["kind"], "blocked")                 # even from a team address: Slack only now
        self.assertEqual(req["requester"], "Sonia Hounsell <sonia@ascendpoint.agency> (email)")
        self.assertTrue(req["text"].startswith("Subject: Fix the typo on About"))
        self.assertIn("'teh' to 'the'", req["text"])
        self.assertEqual(req["files"], [])          # the email itself is not re-downloaded

    def test_slack_channel_email_posted_as_bot(self):
        h = [self.email_msg(NOW - 10, "website@ascendpoint.agency", name="Patrick via Website Requests",
                            user=None, bot_id="BEMAIL", subtype="bot_message")]
        req = wr.find_next(FakeSlack(h), "C1", now=NOW)
        self.assertEqual(req["kind"], "blocked")

    def test_email_from_outside_domain_is_blocked(self):
        h = [self.email_msg(NOW - 10, "someone@gmail.com", name="Spammer")]
        req = wr.find_next(FakeSlack(h), "C1", now=NOW)
        self.assertEqual(req["kind"], "blocked")
        reaction, text = wr.handle(req, FakeSlack([]), "https://x")
        self.assertEqual(reaction, "no_entry_sign")
        self.assertIn("nothing was changed", text)
        self.assertIn("Post the request here in the channel", text)

    def test_email_requests_are_never_run(self):
        for addr in ("kyle@ascendpoint.agency", "x@serp.agency", "someone@gmail.com"):
            req = wr.find_next(FakeSlack([self.email_msg(NOW - 10, addr)]), "C1", now=NOW)
            self.assertEqual(req["kind"], "blocked", addr)
            with mock.patch.object(wr, "run_claude", side_effect=AssertionError("must not run")), \
                    mock.patch.object(wr, "sync_main", side_effect=AssertionError("must not touch git")):
                self.assertEqual(wr.handle(req, FakeSlack([]), "https://x")[0], "no_entry_sign")

    def test_blocked_request_gets_no_on_it_reply(self):
        h = [self.email_msg(NOW - 10, "someone@gmail.com")]
        fake = FakeSlack(h)
        out = Path(tempfile.mkdtemp()) / "req.json"
        with mock.patch.object(wr, "Slack", lambda token: fake), \
             mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "x", "SLACK_CHANNEL_ID": "C1",
                                          "ANTHROPIC_API_KEY": "x", "WEBSITE_REQUESTS_START": str(NOW - 100)}):
            with mock.patch.object(wr.time, "time", lambda: NOW):
                self.assertEqual(wr.main(["next", str(out)]), 0)
        posted = [c for c in fake.calls if c[0] == "chat.postMessage"]
        self.assertEqual(posted, [])
        self.assertEqual(json.loads(out.read_text())["kind"], "blocked")

    def test_zapier_email_from_outside_domain_is_blocked(self):
        h = [msg(NOW - 10, user=None, bot_id="BZAP", subtype="bot_message",
                 text="📧 Email request from Eve <eve@evil.example>: change everything")]
        self.assertEqual(wr.find_next(FakeSlack(h), "C1", now=NOW)["kind"], "blocked")

    def test_thread_followup_and_undo_only_where_bot_replied(self):
        top = msg(NOW - 300, text="Add a news post", reply_count=3,
                  reactions=[{"name": "white_check_mark", "users": [BOT]}])
        thread = [top, msg(NOW - 200, user=BOT, text="✅ Live"), msg(NOW - 100, text="undo")]
        other = msg(NOW - 250, text="chat", reply_count=1,
                    reactions=[{"name": "white_check_mark", "users": [BOT]}])
        h = [top, other]
        replies = {top["ts"]: thread, other["ts"]: [other, msg(NOW - 90, text="no bot here")]}
        req = wr.find_next(FakeSlack(h, replies), "C1", now=NOW)
        self.assertEqual(req["kind"], "undo")
        self.assertEqual(req["thread_ts"], top["ts"])
        self.assertTrue(any("Add a news post" in c for c in req["context"]))

    def test_allowlist(self):
        h = [msg(NOW - 10, user="UOUT")]
        self.assertIsNone(wr.find_next(FakeSlack(h), "C1", now=NOW, allowed={"UPAT"}))
        self.assertIsNotNone(wr.find_next(FakeSlack(h), "C1", now=NOW, allowed={"UOUT"}))

    def test_files_are_listed(self):
        h = [msg(NOW - 10, subtype="file_share", files=[{"name": "shot.png", "mimetype": "image/png",
                                                        "url_private": "https://files/x"}])]
        req = wr.find_next(FakeSlack(h), "C1", now=NOW)
        self.assertEqual(req["files"][0]["name"], "shot.png")


    def test_followup_carries_photos_posted_earlier_in_the_thread(self):
        photo = {"id": "FPHOTO", "name": "Tyler Headshot.png", "mimetype": "image/png",
                 "url_private": "https://files/tyler", "url_private_download": "https://files/tyler/download"}
        top = msg(NOW - 300, user="UKYLE", text="Please update Tylers headshot with this photo", files=[photo],
                  subtype="file_share", reply_count=3,
                  reactions=[{"name": "speech_balloon", "users": [BOT]}])
        bot_file = {"id": "FBOT", "name": "screenshot.png", "url_private": "https://files/bot"}
        thread = [top, msg(NOW - 200, user=BOT, text="💬 I couldn't...", files=[bot_file]),
                  msg(NOW - 100, user="UKYLE", text="retry")]
        req = wr.find_next(FakeSlack([top], {top["ts"]: thread}), "C1", now=NOW)
        self.assertEqual(req["kind"], "followup")
        self.assertEqual([f["id"] for f in req["files"]], ["FPHOTO"])      # the bot's own files never count
        self.assertEqual(req["files"][0]["where"], "earlier in this thread")
        self.assertEqual(req["files"][0]["url"], "https://files/tyler/download")

    def test_new_photo_in_a_reply_comes_first(self):
        old = {"id": "F1", "name": "a.png", "url_private": "https://files/a"}
        new = {"id": "F2", "name": "b.png", "url_private": "https://files/b"}
        top = msg(NOW - 300, text="Swap the hero photo", files=[old], reply_count=2,
                  reactions=[{"name": "white_check_mark", "users": [BOT]}])
        thread = [top, msg(NOW - 200, user=BOT, text="✅ Live"),
                  msg(NOW - 100, text="use this one instead", files=[new], subtype="file_share")]
        req = wr.find_next(FakeSlack([top], {top["ts"]: thread}), "C1", now=NOW)
        self.assertEqual([(f["id"], f["where"]) for f in req["files"]],
                         [("F2", "this message"), ("F1", "earlier in this thread")])

    def test_photo_without_words_is_a_request(self):
        h = [msg(NOW - 10, text="", subtype="file_share", files=[{"id": "F1", "name": "logo.png",
                                                                  "url_private": "https://files/l"}])]
        self.assertIsNotNone(wr.find_next(FakeSlack(h), "C1", now=NOW))


class DownloadTests(unittest.TestCase):
    class Resp:
        def __init__(self, body, ctype):
            self.body, self.headers = body, {"Content-Type": ctype}

        def read(self, n=-1):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def slack(self, body, ctype, seen=None):
        def opener(req, timeout=0):
            if seen is not None:
                seen.append(req)
            return self.Resp(body, ctype)
        return wr.Slack("xoxb-test", opener=opener)

    def test_saves_a_real_file_with_the_bot_token(self):
        seen, dest = [], Path(tempfile.mkdtemp()) / "a.png"
        self.slack(PNG_BYTES, "image/png", seen).download("https://files.slack.com/x", dest)
        self.assertEqual(dest.read_bytes(), PNG_BYTES)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer xoxb-test")

    def test_sign_in_page_is_an_error_not_a_photo(self):
        dest = Path(tempfile.mkdtemp()) / "a.png"
        for body, ctype in ((b"<!DOCTYPE html><html>Sign in</html>", "text/html; charset=utf-8"),
                            (b"<html><body>Sign in</body></html>", "application/octet-stream")):
            with self.assertRaises(wr.FileError) as cm:
                self.slack(body, ctype).download("https://files.slack.com/x", dest)
            self.assertIn("files:read", str(cm.exception))
        self.assertFalse(dest.exists())

    def test_missing_link_is_looked_up(self):
        s = wr.Slack("t")
        s.call = lambda method, **p: {"ok": True, "file": {"url_private_download": f"https://dl/{p['file']}"}}
        self.assertEqual(s.file_url({"id": "F9", "file_access": "check_file_info"}), "https://dl/F9")
        self.assertEqual(s.file_url({"url_private": "https://u"}), "https://u")


class ListenTests(unittest.TestCase):
    def test_listener_picks_up_a_request_that_arrives_mid_poll(self):
        fake = FakeSlack([])
        clock = {"t": NOW}
        polls = {"n": 0}

        def sleep(sec):
            clock["t"] += sec
            polls["n"] += 1
            if polls["n"] == 3:   # someone posts while the listener is waiting
                fake.history.append(msg(clock["t"] - 1, text="Fix the footer phone number"))

        done = []
        with mock.patch.object(wr, "finish", lambda sl, req, live: done.append(req["text"]) or "white_check_mark"), \
             mock.patch.object(wr, "sh", lambda *a, **k: None), \
             mock.patch.object(wr.time, "time", lambda: clock["t"]):
            wr.listen(fake, "C1", "https://x", minutes=1, poll=10, sleep=sleep, clock=lambda: clock["t"])
        self.assertEqual(done, ["Fix the footer phone number"])
        self.assertTrue(any(c[0] == "reactions.add" and c[1]["name"] == "eyes" for c in fake.calls))
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "auth.test"), 1)   # cached

    def test_quiet_threads_are_not_reread(self):
        top = msg(NOW - 300, text="Add a news post", reply_count=1, latest_reply=f"{NOW - 200:.6f}",
                  reactions=[{"name": "white_check_mark", "users": [BOT]}])
        thread = [top, msg(NOW - 200, user=BOT, text="✅ Live")]
        fake = FakeSlack([top], {top["ts"]: thread})
        cache = {}
        for _ in range(3):
            self.assertIsNone(wr.find_next(fake, "C1", now=NOW, cache=cache))
        self.assertEqual(sum(1 for c in fake.calls if c[0] == "conversations.replies"), 1)
        # a new reply changes latest_reply, so the thread is read again
        thread.append(msg(NOW - 100, text="undo"))
        top["latest_reply"] = f"{NOW - 100:.6f}"
        req = wr.find_next(fake, "C1", now=NOW, cache=cache)
        self.assertEqual(req["kind"], "undo")


    def test_restarted_listener_installs_new_packages_first(self):
        clock = {"t": NOW}
        calls = []
        with mock.patch.object(wr, "install_requirements", lambda: calls.append("pip")), \
             mock.patch.object(wr, "sh", lambda *a, **k: None), \
             mock.patch.dict(os.environ, {"LISTEN_UNTIL": str(NOW + 5)}):
            wr.listen(FakeSlack([]), "C1", "https://x", minutes=1, poll=10,
                      sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"])
        self.assertEqual(calls, ["pip"])
        calls.clear()
        with mock.patch.object(wr, "install_requirements", lambda: calls.append("pip")), \
             mock.patch.object(wr, "sh", lambda *a, **k: None):
            os.environ.pop("LISTEN_UNTIL", None)
            wr.listen(FakeSlack([]), "C1", "https://x", minutes=0.1, poll=10,
                      sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"])
        self.assertEqual(calls, [])          # a fresh run: the workflow already installed them

    def test_requirements_cover_photo_tools(self):
        reqs = (ROOT / "tools/requirements-robot.txt").read_text()
        for pkg in ("pillow", "pillow-heif", "opencv-python-headless", "playwright"):
            self.assertIn(pkg, reqs)
        self.assertIn("pip install -r tools/requirements-robot.txt",
                      (ROOT / ".github/workflows/website-requests.yml").read_text())


class WaitLiveTests(unittest.TestCase):
    def test_sends_browser_user_agent_and_detects_new_version(self):
        seen = []
        versions = iter(["oldsha 2026-10-02", "newsha123 2026-10-02"])

        class Resp:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body.encode()

        def opener(req, timeout):
            seen.append(req)
            return Resp(next(versions))

        t = {"now": 0.0}
        ok = wr.wait_live("newsha", "https://ascendpoint.agency", timeout=60, poll=5, opener=opener,
                          sleep=lambda s: t.update(now=t["now"] + s), clock=lambda: t["now"])
        self.assertTrue(ok)
        self.assertEqual(len(seen), 2)
        self.assertIn("Mozilla", seen[0].get_header("User-agent"))
        self.assertIn("/version.txt?v=", seen[0].full_url)

    def test_gives_up_after_timeout(self):
        t = {"now": 0.0}

        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        ok = wr.wait_live("x", "https://x", timeout=30, poll=5, opener=opener,
                          sleep=lambda s: t.update(now=t["now"] + s), clock=lambda: t["now"])
        self.assertFalse(ok)


class ModelRoutingTests(unittest.TestCase):
    def pick(self, text, context=None, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False):
            for k in ("CLAUDE_MODEL", "CLAUDE_MODEL_ADVANCED"):
                if not env or k not in env:
                    os.environ.pop(k, None)
            return wr.choose_model({"text": text, "context": context or []})

    def test_simple_edits_use_sonnet(self):
        for t in ["Change the headline to 'Hello'", "Fix the typo on About", "Remove Kat from the team",
                  "Hey these two sections on the homepage need to get removed", "Update the phone number"]:
            self.assertEqual(self.pick(t), ("sonnet", "standard"), t)

    def test_design_and_motion_use_opus(self):
        for t in ["Add some movement to the homepage hero", "Can the stats count up when you scroll to them?",
                  "Make the brand cards fade in on scroll", "Build a new page for our webinar",
                  "Add a testimonial carousel", "Redesign the contact page layout", "Add hover effects to the cards"]:
            self.assertEqual(self.pick(t), ("opus", "advanced"), t)

    def test_overrides(self):
        self.assertEqual(self.pick("[opus] change the headline"), ("opus", "advanced"))
        self.assertEqual(self.pick("try harder"), ("opus", "advanced"))
        self.assertEqual(self.pick("[sonnet] add a carousel"), ("sonnet", "standard"))
        # a follow-up in an advanced thread stays advanced
        self.assertEqual(self.pick("make it a bit slower", ["Kyle: add a parallax effect to the hero"]),
                         ("opus", "advanced"))

    def test_env_models(self):
        self.assertEqual(self.pick("add a carousel", env={"CLAUDE_MODEL_ADVANCED": "claude-opus-x"})[0], "claude-opus-x")
        self.assertEqual(self.pick("fix a typo", env={"CLAUDE_MODEL": "haiku"})[0], "haiku")


class UrlTests(unittest.TestCase):
    def test_url_for(self):
        self.assertEqual(wr.url_for("site/pages/index.html"), "/")
        self.assertEqual(wr.url_for("site/pages/about.html"), "/about/")
        self.assertEqual(wr.url_for("site/pages/news/foo.html"), "/news/foo/")
        self.assertIsNone(wr.url_for("site/assets/css/site.css"))
        self.assertIsNone(wr.url_for("site/pages/404.html"))


class EndToEndTests(unittest.TestCase):
    """Runs handle() in a throwaway clone with a fake `claude` that edits the About page."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        subprocess.run(["git", "clone", "-q", str(ROOT), str(self.repo)], check=True)
        # include uncommitted robot files so the test runs before they are committed
        for rel in ("tools/website_requests.py", "tools/img_for_web.py", ".gitignore"):
            shutil.copy(ROOT / rel, self.repo / rel)
        shutil.copytree(ROOT / "tools/models", self.repo / "tools/models", dirs_exist_ok=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "robot files", "--allow-empty"], check=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.patches = [mock.patch.object(wr, "ROOT", self.repo),
                        mock.patch.object(wr, "RESULT", self.tmp / "result.json"),
                        mock.patch.object(wr, "FILES_DIR", self.tmp / "files"),
                        mock.patch.object(wr, "wait_live", lambda sha, live, timeout=0: True),
                        mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}",
                                                     "ANTHROPIC_API_KEY": "test"})]
        for p in self.patches:
            p.start()
        self.shipped = []
        self.ship = mock.patch.object(wr, "commit_and_ship", self.fake_ship)
        self.ship.start()

    def tearDown(self):
        self.ship.stop()
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_ship(self, req, summary, extra_trailers=""):
        wr.sh("git", "add", "-A", "site")
        wr.sh("git", "-c", "user.name=AscendPoint AI", "-c", "user.email=b@b", "commit", "-qm",
              f"Website request: {summary}\n\nSlack-Thread: {req['thread_ts']}\n")
        self.shipped.append(summary)
        return "abc"

    def fake_claude(self, script):
        p = self.bin / "claude"
        p.write_text("#!/usr/bin/env python3\n" + script)
        p.chmod(0o755)

    def req(self, text="Change the About headline", kind="new", ts="1.000000", thread="1.000000"):
        return {"channel": "C1", "ts": ts, "thread_ts": thread, "user": "UPAT", "requester": "Patrick",
                "text": text, "files": [], "kind": kind, "context": []}

    def test_change_passes_checks_and_ships(self):
        result = self.tmp / "result.json"
        self.fake_claude(f"""
import json, pathlib, re
p = pathlib.Path('site/pages/about.html'); s = p.read_text()
s = s.replace('Building the leading', 'Building the best', 1); p.write_text(s)
pathlib.Path('{result}').write_text(json.dumps({{"status": "changed",
  "summary": "Changed the About page headline.", "pages": ["/about/"]}}))
print(json.dumps({{"result": "done", "total_cost_usd": 0.12}}))
""")
        reaction, text = wr.handle(self.req(), FakeSlack([]), "https://ascendpoint.agency")
        self.assertEqual(reaction, "white_check_mark", text)
        self.assertIn("https://ascendpoint.agency/about/", text)
        self.assertIn("$0.12", text)
        self.assertEqual(self.shipped, ["Changed the About page headline."])

    def test_question_changes_nothing(self):
        result = self.tmp / "result.json"
        self.fake_claude(f"""
import json, pathlib
pathlib.Path('{result}').write_text(json.dumps({{"status": "question",
  "question": "Which page should the new photo go on?"}}))
""")
        reaction, text = wr.handle(self.req("Add the new photo"), FakeSlack([]), "https://x")
        self.assertEqual(reaction, "speech_balloon")
        self.assertIn("Which page", text)
        self.assertEqual(wr.changed_files(), [])

    def test_failing_checks_block_publish(self):
        result = self.tmp / "result.json"
        self.fake_claude(f"""
import json, pathlib
p = pathlib.Path('site/pages/about.html'); s = p.read_text()
p.write_text(s.replace('</section>', '<a href="/no-such-page/">broken</a></section>', 1))
pathlib.Path('{result}').write_text(json.dumps({{"status": "changed", "summary": "Broke a link."}}))
""")
        reaction, text = wr.handle(self.req(), FakeSlack([]), "https://x")
        self.assertEqual(reaction, "warning", text)
        self.assertEqual(self.shipped, [])
        self.assertEqual(wr.changed_files(), [])

    def test_claude_failure_is_a_warning_not_a_question(self):
        self.fake_claude("""
import json, sys
print(json.dumps({"is_error": True, "result": "API Error: 400 This API key is not scoped to a workspace"}))
sys.exit(1)
""")
        with mock.patch.dict(os.environ, {"WEBSITE_REQUESTS_OWNER": "UKYLE"}):
            reaction, text = wr.handle(self.req(), FakeSlack([]), "https://x")
        self.assertEqual(reaction, "warning")
        self.assertIn("couldn't reach Claude", text)
        self.assertIn("<@UKYLE>", text)
        self.assertIn("not scoped", text)
        self.assertEqual(self.shipped, [])

    def test_workspace_id_is_sent_as_a_header(self):
        out = self.tmp / "env.json"
        self.fake_claude(f"""
import json, os, pathlib
pathlib.Path('{out}').write_text(json.dumps(dict(os.environ)))
print(json.dumps({{"result": "nothing to do", "total_cost_usd": 0.01}}))
""")
        with mock.patch.dict(os.environ, {"ANTHROPIC_WORKSPACE_ID": "wrkspc_TEST"}):
            wr.handle(self.req(), FakeSlack([]), "https://x")
        env = json.loads(out.read_text())
        self.assertEqual(env.get("ANTHROPIC_CUSTOM_HEADERS"), "anthropic-workspace-id: wrkspc_TEST")
        self.assertNotIn("SLACK_BOT_TOKEN", env)          # secrets never reach Claude

    def test_failing_check_is_auto_fixed_by_advanced_model(self):
        result = self.tmp / "result.json"
        calls = self.tmp / "calls.txt"
        self.fake_claude(f"""
import json, pathlib, sys
args = sys.argv[1:]
model = args[args.index("--model") + 1]
prompt = args[args.index("-p") + 1]
pathlib.Path('{calls}').open("a").write(model + "\\n")
p = pathlib.Path('site/pages/about.html'); s = p.read_text()
if "FAILED the site's quality checks" in prompt:
    s = s.replace('<a href="/no-such-page/">broken</a>', '<a href="/contact/">Contact us</a>')
    summary = "Added a contact link to the About page."
else:
    s = s.replace('</section>', '<a href="/no-such-page/">broken</a></section>', 1)
    summary = "Added a link."
p.write_text(s)
pathlib.Path('{result}').write_text(json.dumps({{"status": "changed", "summary": summary, "pages": ["/about/"]}}))
print(json.dumps({{"result": "ok", "total_cost_usd": 0.10}}))
""")
        reaction, text = wr.handle(self.req("Add a contact link to About"), FakeSlack([]), "https://ascendpoint.agency")
        self.assertEqual(reaction, "white_check_mark", text)
        self.assertEqual(calls.read_text().split(), ["sonnet", "opus"])
        self.assertIn("auto-fixed", text)
        self.assertIn("$0.20", text)
        self.assertIn("Added a contact link", text)
        self.assertEqual(self.shipped, ["Added a contact link to the About page."])

    def test_reply_names_the_model(self):
        result = self.tmp / "result.json"
        self.fake_claude(f"""
import json, pathlib
p = pathlib.Path('site/pages/about.html'); p.write_text(p.read_text().replace('Building the leading', 'Building the best', 1))
pathlib.Path('{result}').write_text(json.dumps({{"status": "changed", "summary": "Animated the About hero."}}))
print(json.dumps({{"result": "done", "total_cost_usd": 1.5}}))
""")
        reaction, text = wr.handle(self.req("Add a subtle fade-in animation to the About hero"), FakeSlack([]), "https://x")
        self.assertEqual(reaction, "white_check_mark", text)
        self.assertIn("Claude Opus (advanced request)", text)

    def test_edits_outside_site_are_dropped(self):
        result = self.tmp / "result.json"
        self.fake_claude(f"""
import json, pathlib
pathlib.Path('tools/evil.py').write_text('x')
p = pathlib.Path('README.md'); p.write_text(p.read_text() + 'x')
pathlib.Path('{result}').write_text(json.dumps({{"status": "changed", "summary": "Edited tools."}}))
""")
        reaction, text = wr.handle(self.req(), FakeSlack([]), "https://x")
        self.assertEqual(reaction, "speech_balloon")
        self.assertFalse((self.repo / "tools/evil.py").exists())
        self.assertEqual(self.shipped, [])

    def test_undo_reverts_the_threads_commits(self):
        about = self.repo / "site/pages/about.html"
        before = about.read_text()
        about.write_text(before.replace("Building the leading", "Building the best", 1))
        self.fake_ship(self.req(), "Headline change")
        with mock.patch.object(wr, "sh", self.sh_no_remote):
            reaction, text = wr.handle(self.req("undo", kind="undo", ts="2.000000"), FakeSlack([]), "https://x")
        self.assertEqual(reaction, "leftwards_arrow_with_hook", text)
        self.assertEqual(about.read_text(), before)
        self.assertEqual(wr.thread_commits("1.000000"), [])

    # ---- photos posted in Slack
    def photo_req(self, text="Please update Tylers headshot in the team section with this attached photo",
                  **kw):
        r = self.req(text, **kw)
        r["files"] = [{"id": "FPHOTO", "name": "Tyler Headshot.png", "mimetype": "image/png",
                       "url": "https://files/tyler", "where": "this message"}]
        return r

    class PhotoSlack(FakeSlack):
        def __init__(self, body=None):
            super().__init__([])
            self.body = body if body is not None else (ROOT / "site/img/hs-kyle-sq.webp").read_bytes()

        def download(self, url, dest):
            if self.body is None or self.body.startswith(b"<html"):
                raise wr.FileError("Slack sent a sign-in page instead of the file (the Slack app needs the "
                                   "files:read permission)")
            dest.write_bytes(self.body)

    def test_headshot_from_slack_ships_under_a_new_file_name(self):
        result, prompt_out = self.tmp / "result.json", self.tmp / "prompt.txt"
        files = self.repo / ".request-files"
        self.fake_claude(f"""
import json, pathlib, subprocess, sys
args = sys.argv[1:]
pathlib.Path('{prompt_out}').write_text(json.dumps(args))
src = sorted(p for p in pathlib.Path('.request-files').iterdir() if '.view.' not in p.name)[0]
out = subprocess.run(['python3', 'tools/img_for_web.py', str(src), '--replace', 'site/img/hs-tyler-sq.webp'],
                     capture_output=True, text=True)
assert out.returncode == 0, out.stdout + out.stderr
pathlib.Path('{result}').write_text(json.dumps({{"status": "changed",
  "summary": "Updated Tyler's headshot on the About page.", "pages": ["/about/"]}}))
print(json.dumps({{"result": "done", "total_cost_usd": 0.08}}))
""")
        with mock.patch.object(wr, "FILES_DIR", files):
            reaction, text = wr.handle(self.photo_req(), self.PhotoSlack(), "https://ascendpoint.agency")
        self.assertEqual(reaction, "white_check_mark", text)
        about = (self.repo / "site/pages/about.html").read_text()
        self.assertNotIn('src="/img/hs-tyler-sq.webp"', about)
        self.assertRegex(about, r'src="/img/hs-tyler-sq-[0-9a-f]{6}\.webp"')
        self.assertFalse((self.repo / "site/img/hs-tyler-sq.webp").exists())
        committed = wr.sh("git", "show", "--stat", "HEAD").stdout
        self.assertNotIn(".request-files", committed)            # attachments never get committed
        args = json.loads(prompt_out.read_text())
        prompt = args[args.index("-p") + 1]
        self.assertIn(".request-files/Tyler-Headshot.png: photo/image 320x320", prompt)
        self.assertIn("a face was found", prompt)
        self.assertIn(str(files), args[args.index("--add-dir") + 1])   # Claude may open the attachments
        self.assertIn("Bash(python3 tools/img_for_web.py:*)", args[args.index("--allowedTools") + 1])

    def test_attachment_that_cannot_be_downloaded_is_a_clear_warning(self):
        self.fake_claude("import sys; sys.exit('claude must not run')")
        with mock.patch.dict(os.environ, {"WEBSITE_REQUESTS_OWNER": "UKYLE"}), \
                mock.patch.object(wr, "FILES_DIR", self.repo / ".request-files"):
            reaction, text = wr.handle(self.photo_req(), self.PhotoSlack(b"<html>sign in</html>"), "https://x")
        self.assertEqual(reaction, "warning")
        self.assertIn("couldn't open the file you attached", text)
        self.assertIn("files:read", text)
        self.assertIn("<@UKYLE>", text)
        self.assertEqual(self.shipped, [])

    def test_stale_attachments_from_an_earlier_request_are_cleared(self):
        files = self.repo / ".request-files"
        files.mkdir()
        (files / "old-request.png").write_bytes(PNG_BYTES)
        seen = self.tmp / "seen.txt"
        self.fake_claude(f"""
import pathlib
pathlib.Path('{seen}').write_text(" ".join(sorted(p.name for p in pathlib.Path('.request-files').iterdir())))
""")
        with mock.patch.object(wr, "FILES_DIR", files):
            wr.handle(self.photo_req(), self.PhotoSlack(), "https://x")
        self.assertEqual(seen.read_text(), "Tyler-Headshot.png")

    def test_blocked_tool_is_flagged_to_the_owner(self):
        result = self.tmp / "result.json"
        self.fake_claude(f"""
import json, pathlib
pathlib.Path('{result}').write_text(json.dumps({{"status": "question",
  "question": "I couldn't process the photo."}}))
print(json.dumps({{"result": "x", "permission_denials": [{{"tool_name": "Bash",
  "tool_input": {{"command": "python3 -c 'from PIL import Image'"}}}}]}}))
""")
        with mock.patch.dict(os.environ, {"WEBSITE_REQUESTS_OWNER": "UKYLE"}), \
                mock.patch.object(wr, "FILES_DIR", self.repo / ".request-files"):
            reaction, text = wr.handle(self.photo_req(), self.PhotoSlack(), "https://x")
        self.assertEqual(reaction, "speech_balloon")
        self.assertIn("<@UKYLE>", text)
        self.assertIn("python3 -c", text)

    def sh_no_remote(self, *args, **kw):
        if args[:2] in (("git", "push"), ("gh", "workflow")):
            return subprocess.CompletedProcess(args, 0, "", "")
        return wr.subprocess.run(args, cwd=self.repo, check=kw.get("check", True), text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=kw.get("env"))


if __name__ == "__main__":
    unittest.main()
