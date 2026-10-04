"""Preview-first requests and model tiers for the AscendPoint robot (tools/website_requests.py). No network: a local bare git repo
stands in for GitHub, Slack and Claude are faked.

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

TS = "1791100000.000100"
LIVE = "https://ascendpoint.agency"
PAGE = "site/pages/index.html"
PREVIEW_URL = "https://ascendpoint-preview.kinsta.page"

FAKE_BUILD = '''import shutil, subprocess, sys
from pathlib import Path
root = Path(__file__).resolve().parent.parent
dst = root / "dist"
shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(root / "site" / "pages", dst)
env = sys.argv[sys.argv.index("--env") + 1] if "--env" in sys.argv else "production"
(dst / "env.txt").write_text(env)
(dst / "version.txt").write_text(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True))
'''
FAKE_SHOTS = '''import sys
from pathlib import Path
out = Path(sys.argv[sys.argv.index("--out") + 1]); out.mkdir(parents=True, exist_ok=True)
for dev in ("desktop", "mobile"):
    (out / f"home-{dev}.png").write_bytes(b"png")
'''


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeSlack:
    def __init__(self, files_write=True):
        self.calls, self.uploads, self.files_write = [], [], files_write

    def call(self, method, **p):
        self.calls.append((method, p))
        if method.startswith("files.") and not self.files_write:
            raise RuntimeError("slack files.getUploadURLExternal: missing_scope files:write")
        if method == "files.getUploadURLExternal":
            return {"ok": True, "upload_url": "https://files.slack.test/up", "file_id": f"F{len(self.calls)}"}
        return {"ok": True}

    def upload(self, url, path):
        self.uploads.append(path.name)

    def download(self, url, dest):
        dest.write_bytes(b"x")


class PreviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.work = self.tmp / "work"
        git(self.tmp, "clone", "-q", str(self.origin), str(self.work))
        for rel, text in {"site/pages/index.html": "<h1>Old headline</h1>\n",
                          "site/pages/about.html": "<p>About</p>\n",
                          "tools/build.py": FAKE_BUILD, "tools/screenshot.py": FAKE_SHOTS,
                          ".gitignore": "dist/\n"}.items():
            f = self.work / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            git(self.work, "config", k, v)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "base")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        self.base = git(self.work, "rev-parse", "HEAD")
        self.gh = []
        real_sh = wr.sh

        def sh(*args, **kw):
            if args[0] == "gh":
                self.gh.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_sh(*args, **kw)

        self.patches = [
            mock.patch.object(wr, "ROOT", self.work),
            mock.patch.object(wr, "sh", side_effect=sh),
            mock.patch.object(wr, "wait_live", return_value=True),
            mock.patch.object(wr, "GIT_USER", ("-c", "user.name=t", "-c", "user.email=t@t")),
            mock.patch.object(wr, "checks", side_effect=self.fake_checks),
            mock.patch.dict(os.environ, {"PREVIEW_URL": PREVIEW_URL, "WEBSITE_REQUESTS_OWNER": "UOWN"}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp)

    def fake_checks(self):
        subprocess.run([sys.executable, "tools/build.py"], cwd=self.work, check=True)
        return True, "ok"

    def claude_writes(self, headline, cost=0.02):
        def run(req, model=None, max_usd=None, fix_log=None, preview=False):
            self.claude_calls.append({"model": model, "preview": preview})
            (self.work / "site/pages/index.html").write_text(f"<h1>{headline}</h1>\n")
            return {"status": "changed", "summary": f"Changed the headline to {headline}.", "pages": ["/"],
                    "cost_usd": cost}
        self.claude_calls = []
        return mock.patch.object(wr, "run_claude", side_effect=run)

    def req(self, text, ts=TS, kind=None):
        kind = kind or ("new" if ts == TS else "followup")
        if kind == "followup" and wr.UNDO_RE.match(text):
            kind = "undo"
        return {"channel": "CSERP", "ts": ts, "thread_ts": TS, "user": "UPAT", "requester": "Pat",
                "sender": None, "text": text, "files": [], "kind": kind, "context": []}

    def origin_file(self, ref, rel):
        r = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=self.origin, capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    def branches(self):
        return set(git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())

    # ---------------------------------------------------------------------------------------------------
    def test_preview_request_never_touches_main(self):
        slack = FakeSlack()
        with self.claude_writes("New headline"):
            reaction, text = wr.handle(self.req("Preview first please: change the headline to New headline"),
                                       slack, "https://ascendpoint.agency")
        self.assertEqual(reaction, "mag")
        self.assertTrue(self.claude_calls[0]["preview"])
        self.assertEqual(git(self.origin, "rev-parse", "main"), self.base)                    # main untouched
        self.assertIn("New headline", self.origin_file(f"preview/{TS}", "site/pages/index.html"))
        built = self.origin_file("kinsta-preview", "index.html")                                # preview site
        self.assertIn("New headline", built)
        self.assertEqual(self.origin_file("kinsta-preview", "version.txt").strip(),
                         git(self.origin, "rev-parse", f"preview/{TS}"))
        self.assertEqual(self.origin_file("kinsta-preview", "env.txt"), "staging")              # noindex, no GA
        self.assertIsNotNone(self.origin_file("kinsta-preview", "_preview/home-desktop.png"))
        self.assertIn(f"{PREVIEW_URL}/", text)
        self.assertIn("NOT live", text)
        self.assertIn("*approve*", text)
        self.assertEqual(sorted(slack.uploads), ["home-desktop.png", "home-mobile.png"])
        self.assertEqual(self.gh, [])                                                            # no deploy
        self.assertEqual(git(self.work, "rev-parse", "--abbrev-ref", "HEAD"), "main")           # back on main
        self.assertEqual(git(self.work, "status", "--porcelain"), "")

    def test_screenshots_linked_when_slack_app_cannot_upload(self):
        slack = FakeSlack(files_write=False)
        with self.claude_writes("New headline"):
            reaction, text = wr.handle(self.req("Can I see a mockup before it goes live? Headline: New"),
                                       slack, "https://ascendpoint.agency")
        self.assertEqual(reaction, "mag")
        self.assertIn(f"{PREVIEW_URL}/_preview/home-desktop.png", text)

    def test_tweak_updates_the_preview_then_approve_ships_exactly_it(self):
        slack = FakeSlack()
        with self.claude_writes("First try"):
            wr.handle(self.req("preview first: new headline"), slack, "https://ascendpoint.agency")
        with self.claude_writes("Second try"):
            reaction, _ = wr.handle(self.req("make it say Second try", ts="1791100100.000100"), slack,
                                    "https://ascendpoint.agency")
        self.assertEqual(reaction, "mag")                                    # a tweak updates the preview...
        self.assertTrue(self.claude_calls[0]["preview"])
        self.assertEqual(git(self.origin, "rev-parse", "main"), self.base)   # ...and still nothing is live
        self.assertIn("Second try", self.origin_file("kinsta-preview", "index.html"))

        reaction, text = wr.handle(self.req("approve", ts="1791100200.000100"), slack, "https://ascendpoint.agency")
        self.assertEqual(reaction, "white_check_mark")
        self.assertIn("Second try", self.origin_file("main", "site/pages/index.html"))
        self.assertNotIn(f"preview/{TS}", self.branches())                   # preview cleaned up
        self.assertEqual([a[:3] for a in self.gh], [("gh", "workflow", "run")])
        self.assertIn("https://ascendpoint.agency/", text)

        # and the approved change can be undone like any other
        reaction, _ = wr.handle(self.req("undo", ts="1791100300.000100"), slack, "https://ascendpoint.agency")
        self.assertEqual(reaction, "leftwards_arrow_with_hook")
        self.assertIn("Old headline", self.origin_file("main", "site/pages/index.html"))

    def test_cancel_drops_the_preview(self):
        slack = FakeSlack()
        with self.claude_writes("Nope"):
            wr.handle(self.req("show me a preview: headline Nope"), slack, "https://ascendpoint.agency")
        reaction, text = wr.handle(self.req("cancel", ts="1791100100.000100"), slack, "https://ascendpoint.agency")
        self.assertEqual(reaction, "wastebasket")
        self.assertNotIn(f"preview/{TS}", self.branches())
        self.assertEqual(git(self.origin, "rev-parse", "main"), self.base)
        self.assertEqual(self.gh, [])

    def test_undo_on_an_unapproved_preview_drops_it(self):
        slack = FakeSlack()
        with self.claude_writes("Nope"):
            wr.handle(self.req("preview first: headline Nope"), slack, "https://ascendpoint.agency")
        reaction, _ = wr.handle(self.req("undo", ts="1791100100.000100"), slack, "https://ascendpoint.agency")
        self.assertEqual(reaction, "wastebasket")
        self.assertEqual(git(self.origin, "rev-parse", "main"), self.base)

    def test_ordinary_request_still_goes_straight_live(self):
        slack = FakeSlack()
        with self.claude_writes("Live now"):
            reaction, text = wr.handle(self.req("change the headline to Live now"), slack, "https://ascendpoint.agency")
        self.assertEqual(reaction, "white_check_mark")
        self.assertFalse(self.claude_calls[0]["preview"])
        self.assertIn("Live now", self.origin_file("main", "site/pages/index.html"))
        self.assertNotIn("kinsta-preview", self.branches())


    def test_undo_works_when_main_moved_on(self):
        """Someone else pushed after this thread's change: undo rebases instead of failing."""
        slack = FakeSlack()
        with self.claude_writes("Live now"):
            wr.handle(self.req("change the headline to Live now"), slack, LIVE)
        other = self.tmp / "other"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        for k, v in (("user.name", "o"), ("user.email", "o@o"), ("commit.gpgsign", "false")):
            git(other, "config", k, v)
        (other / "NOTES.md").write_text("pushed by someone else\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "unrelated change")
        git(other, "push", "-q", "origin", "HEAD:main")
        reaction, _ = wr.handle(self.req("undo", ts="1791100300.000100"), slack, LIVE)
        self.assertEqual(reaction, "leftwards_arrow_with_hook")
        self.assertIn("Old headline", self.origin_file("main", PAGE))
        self.assertEqual(self.origin_file("main", "NOTES.md"), "pushed by someone else\n")


class WordingTests(unittest.TestCase):
    def test_preview_phrases(self):
        yes = ["Preview first please", "can you send me a mockup", "put it on staging",
               "show me what it looks like before it goes live", "let me see it first",
               "I want to approve it first", "don't push it live yet", "before you publish, send it over",
               "Can we remove X. But before you do it live on the site - can you send me a mockup or staging "
               "example that I can approve first?"]
        no = ["change the headline to Grow your practice", "fix the typo in the footer",
              "first paragraph should say hello", "add a review section"]
        for t in yes:
            self.assertTrue(wr.wants_preview({"text": t}), t)
        for t in no:
            self.assertFalse(wr.wants_preview({"text": t}), t)

    def test_approve_and_cancel_phrases(self):
        for t in ["approve", "Approved!", "ship it", "looks good", "Looks great, go live", "lgtm", "go ahead",
                  "yes", "Yes please", "perfect", "push it live", "publish"]:
            self.assertTrue(wr.APPROVE_RE.match(t), t)
        for t in ["make the button blue", "yes but make it bigger", "can you change the color", "not yet"]:
            self.assertFalse(wr.APPROVE_RE.match(t), t)
        for t in ["cancel", "never mind", "scrap it", "drop it"]:
            self.assertTrue(wr.CANCEL_RE.match(t), t)
        self.assertFalse(wr.CANCEL_RE.match("make it bigger"))

    def test_model_tiers(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("CLAUDE_MODEL", "CLAUDE_MODEL_SIMPLE", "CLAUDE_MODEL_ADVANCED"):
                os.environ.pop(k, None)
            r = lambda t, **kw: wr.choose_model({"text": t, "kind": "new", **kw})
            self.assertEqual(r("fix the typo in the footer"), ("haiku", "simple"))
            self.assertEqual(r("update the phone number to (512) 555-0100"), ("haiku", "simple"))
            self.assertEqual(r("change the headline to Grow your practice"), ("haiku", "simple"))
            self.assertEqual(r("add our new case study to the results page"), ("sonnet", "standard"))
            self.assertEqual(r("fix the typo", files=[{"name": "shot.png"}]), ("sonnet", "standard"))
            self.assertEqual(r("change the headline. " + "x" * 400), ("sonnet", "standard"))
            self.assertEqual(r("add a fade-in animation to the hero"), ("opus", "advanced"))
            self.assertEqual(r("the contact page looks bad, make it a form"), ("opus", "advanced"))
            self.assertEqual(r("[opus] fix the typo"), ("opus", "advanced"))
            self.assertEqual(r("[sonnet] fix the typo"), ("sonnet", "standard"))
            self.assertEqual(wr.choose_model({"text": "change the text", "kind": "followup"}), ("sonnet", "standard"))
            self.assertEqual(wr.budget("simple"), "1")

    def test_simple_tier_falls_back_to_sonnet(self):
        calls = []

        def run(req, model=None, max_usd=None, fix_log=None, preview=False):
            calls.append(model)
            return {"status": "no_change", "summary": "", "cost_usd": 0.001}
        with mock.patch.object(wr, "run_claude", side_effect=run), \
                mock.patch.object(wr, "drop_outside_site", return_value=[]), \
                mock.patch.object(wr, "reset_worktree"), mock.patch.dict(os.environ, {}, clear=False):
            for k in ("CLAUDE_MODEL", "CLAUDE_MODEL_SIMPLE"):
                os.environ.pop(k, None)
            reaction, _ = wr.make_change({"text": "fix the typo on the about page", "kind": "new", "files": [],
                                          "context": []}, FakeSlack(), "https://ascendpoint.agency", preview=False)
        self.assertEqual(calls, ["haiku", "sonnet"])
        self.assertEqual(reaction, "speech_balloon")

    def test_live_check_matches_short_version_ids(self):
        full = "5fa12b8d0c4e8a1f2b3c4d5e6f708192a3b4c5d6"
        self.assertTrue(wr.same_commit("5fa12b8\n", full))
        self.assertTrue(wr.same_commit(full + " 2026-10-04T04:33Z\n", full))
        self.assertFalse(wr.same_commit("2f20b7e\n", full))
        self.assertFalse(wr.same_commit("", full))
        self.assertFalse(wr.same_commit("5f\n", full))           # too short to trust

        class Resp:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.body
        bodies = iter([b"2f20b7e\n", b"5fa12b8\n"])
        self.assertTrue(wr.wait_live(full, "https://ascendpoint.agency", opener=lambda req, timeout: Resp(next(bodies)),
                                     sleep=lambda s: None))

    def test_preview_states_are_never_picked_up_again(self):
        self.assertTrue({"mag", "wastebasket"} <= wr.HANDLED)

    def test_rules_tell_claude_to_just_make_preview_changes(self):
        self.assertIn('"Preview first"', wr.CLAUDE_RULES)


if __name__ == "__main__":
    unittest.main()
