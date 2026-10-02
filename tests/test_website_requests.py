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
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import website_requests as wr  # noqa: E402

BOT = "UBOT"
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

    def email_msg(self, ts, address, name="Sonia Hounsell", subject="Fix the typo on About",
                  body="Change 'teh' to 'the' in the first paragraph.", **kw):
        f = {"id": "F1", "mode": "email", "filetype": "email", "title": subject, "subject": subject,
             "from": [{"address": address, "name": name, "original": f"{name} <{address}>"}],
             "plain_text": body, "url_private": "https://files/email"}
        return msg(ts, text="", files=[f], **kw)

    def test_slack_channel_email_is_a_request(self):
        h = [self.email_msg(NOW - 10, "sonia@ascendpoint.agency", subtype="file_share", user="UKYLE")]
        req = wr.find_next(FakeSlack(h), "C1", now=NOW, allowed={"UPAT"})   # email bypasses user allowlist
        self.assertEqual(req["kind"], "new")
        self.assertEqual(req["requester"], "Sonia Hounsell <sonia@ascendpoint.agency> (email)")
        self.assertTrue(req["text"].startswith("Subject: Fix the typo on About"))
        self.assertIn("'teh' to 'the'", req["text"])
        self.assertEqual(req["files"], [])          # the email itself is not re-downloaded

    def test_slack_channel_email_posted_as_bot(self):
        h = [self.email_msg(NOW - 10, "website@ascendpoint.agency", name="Patrick via Website Requests",
                            user=None, bot_id="BEMAIL", subtype="bot_message")]
        req = wr.find_next(FakeSlack(h), "C1", now=NOW)
        self.assertEqual(req["kind"], "new")

    def test_email_from_outside_domain_is_blocked(self):
        h = [self.email_msg(NOW - 10, "someone@gmail.com", name="Spammer")]
        req = wr.find_next(FakeSlack(h), "C1", now=NOW)
        self.assertEqual(req["kind"], "blocked")
        reaction, text = wr.handle(req, FakeSlack([]), "https://x")
        self.assertEqual(reaction, "no_entry_sign")
        self.assertIn("Nothing was changed", text)

    def test_email_domains_env(self):
        h = [self.email_msg(NOW - 10, "x@partner.com")]
        with mock.patch.dict(os.environ, {"WEBSITE_REQUESTS_EMAIL_DOMAINS": "partner.com"}):
            self.assertEqual(wr.find_next(FakeSlack(h), "C1", now=NOW)["kind"], "new")

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
        for rel in ("tools/website_requests.py", "tools/img_for_web.py"):
            shutil.copy(ROOT / rel, self.repo / rel)
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

    def sh_no_remote(self, *args, **kw):
        if args[:2] in (("git", "push"), ("gh", "workflow")):
            return subprocess.CompletedProcess(args, 0, "", "")
        return wr.subprocess.run(args, cwd=self.repo, check=kw.get("check", True), text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=kw.get("env"))


if __name__ == "__main__":
    unittest.main()
