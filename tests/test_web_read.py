"""Tests for tools/web_read.py: links in Slack requests become local Markdown + images.

Pages are served from a throwaway HTTP server on 127.0.0.1 (WEB_READ_ALLOW_PRIVATE=1 lets the reader use it);
no real website is contacted.

    python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import web_read as w  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "web"


def image_bytes(size=(1536, 1024), fmt="WEBP", colour=(20, 80, 160)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, fmt)
    return buf.getvalue()


class Site(BaseHTTPRequestHandler):
    """Serves the fixtures under /blog/..., images under /wp-content/..., plus redirects, errors and a PDF."""
    routes: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in self.routes:
            status, ctype, body, extra = self.routes[path]
            self.send_response(status)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


class Server:
    def __enter__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def install_routes():
    html = lambda name: (200, "text/html; charset=utf-8", (FIX / name).read_bytes(), None)
    Site.routes = {
        "/blog/aw-2026/": html("builder-post.html"),
        "/plain/": html("article-post.html"),
        "/elementor/": html("elementor.html"),
        "/app/": html("js-only.html"),
        "/old-link/": (301, "text/html", b"", {"Location": "/blog/aw-2026/"}),
        "/secret/": (403, "text/html", b"forbidden", None),
        "/doc.pdf": (200, "application/pdf", b"%PDF-1.4\n%fake\n", None),
        "/photo.png": (200, "image/png", image_bytes((800, 600), "PNG"), None),
        "/notes.txt": (200, "text/plain", b"Plain notes for the robot.", None),
        "/wp-content/uploads/2026/10/aw-share.jpg": (200, "image/jpeg", image_bytes((1200, 630), "JPEG"), None),
        "/wp-content/uploads/2026/10/aw-hero-1536x1024.webp": (200, "image/webp", image_bytes(), None),
    }


class ExtractionTests(unittest.TestCase):
    """read_html on saved pages: no network."""

    def read(self, name, url="https://serpdental.com/blog/advertising-week/"):
        return w.read_html((FIX / name).read_text(), url)

    def test_page_builder_post_without_article_tag(self):
        r = self.read("builder-post.html")
        md, meta = r["markdown"], r["meta"]
        self.assertEqual(meta["author"], "Patrick Hounsell, Co-CEO")            # JSON-LD wins over <meta author>
        self.assertEqual(meta["published"], "2026-10-09")
        self.assertTrue(meta["title"].startswith("3 Lessons from Advertising Week 2026"))
        self.assertEqual(meta["site_name"], "SERP Dental™")
        self.assertEqual(meta["image"], "https://serpdental.com/wp-content/uploads/2026/10/aw-share.jpg")
        for want in ("## Lesson 1: TV is now a local, measurable channel", "## Lesson 2:",
                     "### What is connected TV (CTV) advertising?", "**Key takeaways**",
                     "- Connected TV is now local and measurable.", "> \"Trust is the new targeting.\"",
                     "| Channel | Measurable? |", "1. [Nielsen-style streaming report 2026](https://example.org/ctv-report)",
                     "[Embedded video: https://www.youtube.com/embed/abc123]", "*book*, not just recommend",
                     "*The main stage at Advertising Week 2026.*"):
            self.assertIn(want, md)
        for noise in ("cookies", "Popular posts", "All rights reserved", "Share on Facebook", "tracking",
                      "color:red", "Lesson 1</a>"):                                # banner, sidebar, footer, TOC...
            self.assertNotIn(noise, md)
        # the largest srcset version, absolute; the 1x1 tracking pixel is not an image
        self.assertEqual([i["url"] for i in r["images"]],
                         ["https://serpdental.com/wp-content/uploads/2026/10/aw-hero-1536x1024.webp"])
        self.assertGreater(r["words"], 200)

    def test_old_html_with_unclosed_paragraphs(self):
        r = self.read("article-post.html", "https://clinic.example/blog/reviews/")
        md = r["markdown"]
        self.assertIn("Reviews are the first thing a new patient checks before calling a clinic, and they decide "
                      "who gets the booking.\n\nUnclosed paragraphs", md)
        self.assertIn("[a relative link](https://clinic.example/inner)", md)
        self.assertIn("## Next steps", md)
        self.assertNotIn("Another article teaser", md)
        self.assertNotIn("Site name", md)

    def test_elementor_widgets_are_content_not_noise(self):
        md = self.read("elementor.html")["markdown"]
        self.assertIn("## Implants that last", md)
        self.assertIn("CT scan", md)
        self.assertNotIn("[X]", md)

    def test_google_links_use_the_export(self):
        self.assertEqual(w.google_export("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit?usp=sharing"),
                         ("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/export?format=md", "doc"))
        self.assertEqual(w.google_export("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit#gid=42")[0],
                         "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/export?format=csv&gid=42")
        self.assertIsNone(w.google_export("https://example.com/document/d/x"))

    def test_slug(self):
        self.assertEqual(w.slug_for("https://www.serpdental.com/blog/advertising-week-2026/?utm=x"),
                         "serpdental-com-blog-advertising-week-2026")


class SafetyTests(unittest.TestCase):
    def test_only_public_web_addresses(self):
        with mock.patch.dict(os.environ, {"WEB_READ_ALLOW_PRIVATE": ""}):
            for bad in ("file:///etc/passwd", "ftp://example.com/x", "http://127.0.0.1:8080/", "http://localhost/",
                        "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/", "http://[::1]/"):
                with self.subTest(bad=bad), self.assertRaises(w.FetchError):
                    w.check_url(bad)


class ReadUrlTests(unittest.TestCase):
    """read_url end to end against the local server: files on disk are what the robot hands to Claude."""

    @classmethod
    def setUpClass(cls):
        install_routes()

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"WEB_READ_ALLOW_PRIVATE": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.out, ignore_errors=True)

    def test_blog_post_becomes_markdown_and_local_images(self):
        with Server() as s:
            r = w.read_url(s.base + "/blog/aw-2026/", self.out, render="never")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["author"], "Patrick Hounsell, Co-CEO")
        self.assertEqual(r["images"], ["img-01.jpg", "img-02.webp"])          # share image first, then the article's
        folder = Path(r["dir"])
        md = (folder / "page.md").read_text()
        self.assertTrue(md.startswith("# 3 Lessons from Advertising Week 2026 for Dental and Medical Marketing\n"))
        self.assertIn("Author: Patrick Hounsell, Co-CEO", md)
        self.assertIn("Published: 2026-10-09", md)
        self.assertEqual(md.count("# 3 Lessons from Advertising Week 2026 for Dental and Medical Marketing\n"), 1)
        self.assertIn("_(saved as img-02.webp)_", md)                            # inline image -> local copy
        self.assertIn("- img-01.jpg 1200x630 [share image]", md)
        self.assertIn("- img-02.webp 1536x1024 alt: \"Advertising Week 2026 stage in New York\"", md)
        meta = json.loads((folder / "meta.json").read_text())
        self.assertEqual(meta["published"], "2026-10-09")
        self.assertEqual(len(meta["images"]), 2)
        from PIL import Image
        with Image.open(folder / "img-02.webp") as im:
            self.assertEqual(im.size, (1536, 1024))

    def test_redirect_is_followed(self):
        with Server() as s:
            r = w.read_url(s.base + "/old-link/", self.out, max_images=0, render="never")
        self.assertTrue(r["ok"], r)
        self.assertIn("Lesson 1", (Path(r["dir"]) / "page.md").read_text())

    def test_refused_page_is_a_clear_error_not_a_crash(self):
        with Server() as s:
            r = w.read_url(s.base + "/secret/", self.out, render="never")
            r404 = w.read_url(s.base + "/nope/", self.out, render="never")
        self.assertFalse(r["ok"])
        self.assertIn("refused (403)", r["error"])
        self.assertIn("404", r404["error"])
        self.assertIn("Couldn't read this page", (Path(r["dir"]) / "page.md").read_text())

    def test_pdf_image_and_text_links(self):
        with Server() as s:
            pdf = w.read_url(s.base + "/doc.pdf", self.out)
            img = w.read_url(s.base + "/photo.png", self.out)
            txt = w.read_url(s.base + "/notes.txt", self.out)
        self.assertTrue((Path(pdf["dir"]) / "document.pdf").exists())
        self.assertEqual(pdf["pdf"], "document.pdf")
        self.assertTrue((Path(img["dir"]) / "img-01.png").exists())
        self.assertIn("Plain notes for the robot.", (Path(txt["dir"]) / "page.md").read_text())

    def test_javascript_page_is_rendered_in_a_browser(self):
        rendered = ("<html><head><title>App</title></head><body><main><h1>Rendered later</h1>"
                    + "<p>This text only exists after JavaScript runs, which is why the robot opens the page in "
                      "headless Chromium when the plain download is nearly empty.</p>" * 4 + "</main></body></html>")
        with Server() as s, mock.patch.object(w, "render_with_browser", lambda url: rendered) as _:
            r = w.read_url(s.base + "/app/", self.out, max_images=0)
        self.assertTrue(r["ok"])
        self.assertTrue(r["rendered"])
        self.assertIn("only exists after JavaScript runs", (Path(r["dir"]) / "page.md").read_text())

    def test_cli_prints_one_json_line_per_link(self):
        with Server() as s:
            p = subprocess.run([sys.executable, str(ROOT / "tools/web_read.py"), s.base + "/plain/", s.base + "/secret/",
                                "--out", str(self.out), "--render", "never"],
                               capture_output=True, text=True, env=dict(os.environ, WEB_READ_ALLOW_PRIVATE="1"))
        lines = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([l["ok"] for l in lines], [True, False])
        self.assertEqual(p.returncode, 0)                    # one page read = success; the other is described
        self.assertTrue(Path(lines[0]["md"]).exists())


if __name__ == "__main__":
    unittest.main()
