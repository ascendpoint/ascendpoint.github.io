#!/usr/bin/env python3
"""Stand-in for `claude` in tests/test_website_requests.py BlogFromLinkEndToEnd: does what the real robot is
told to do with a linked article (read the saved page.md, make images from the saved files with
tools/img_for_web.py, write one new post page). PROMPT_OUT / RESULT_OUT are replaced by the test."""
import json
import pathlib
import re
import subprocess
import sys

args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
pathlib.Path("PROMPT_OUT").write_text(json.dumps(args))
md_path = re.search(r"read: (\S+/page\.md)", prompt).group(1)
md = pathlib.Path(md_path).read_text()
assert "Lesson 1: TV is now a local, measurable channel" in md, md[:500]
folder = pathlib.Path(md_path).parent
slug = "advertising-week-2026-lessons-healthcare-marketing"
for src, dest, opt in ((folder / "img-02.webp", f"site/img/{slug}.webp", ["--width", "1600"]),
                       (folder / "img-01.jpg", f"site/img/og/{slug}.jpg", ["--size", "1200x630"])):
    out = subprocess.run(["python3", "tools/img_for_web.py", str(src), dest, *opt], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
page = """<!--
title: 3 Lessons from Advertising Week 2026 for Healthcare
description: Three lessons from Advertising Week New York 2026 for dental and medical practices: local CTV, trust that books patients, and AI booking.
image: /img/og/SLUG.jpg
type: post
date: 2026-10-17
author: Patrick Hounsell
name: 3 Lessons from Advertising Week 2026 for Dental and Medical Marketing
list_title: 3 Lessons from Advertising Week 2026 for Dental and Medical Marketing
list_date: Oct 17, 2026 · Patrick Hounsell
list_image: /img/SLUG.webp
list_image_alt: Advertising Week 2026 stage in New York
list_summary: Local CTV, trust and agentic AI: what Advertising Week 2026 means for practices.
-->
<section class="hero page"><div class="wrap"><h1 class="h1">3 Lessons from Advertising Week 2026 for Dental and Medical Marketing</h1></div></section>
<section class="sec"><div class="wrap"><figure class="article-feature"><img src="/img/SLUG.webp" alt="Advertising Week 2026 stage in New York"></figure>
<article class="article"><h2>Lesson 1: TV is now a local, measurable channel</h2><p>Streaming TV lets a practice show an ad only to households near its office.</p>
<p><em>Originally published on the <a href="https://serpdental.com/blog/advertising-week-2026-lessons-dental-marketing/">SERP Dental blog</a>.</em></p></article></div></section>
""".replace("SLUG", slug)
pathlib.Path(f"site/pages/{slug}.html").write_text(page)
pathlib.Path("RESULT_OUT").write_text(json.dumps({
    "status": "changed", "summary": "Added the Advertising Week 2026 article to the AscendPoint blog.",
    "pages": [f"/{slug}/", "/blog/"]}))
print(json.dumps({"result": "done", "total_cost_usd": 1.2}))
