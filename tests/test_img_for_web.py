"""Tests for tools/img_for_web.py: adding, swapping (headshots) and checking images.

    python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import img_for_web as iw  # noqa: E402

# Fixed test photos (copies of two team headshots), so the tests never depend on what the live site
# currently calls its images: the robot renames them every time someone swaps a photo.
FIX = ROOT / "tests" / "fixtures"

try:
    import cv2  # noqa: F401
    HAVE_CV = iw.find_face(Image.open(FIX / "headshot-b.webp")) is not None
except Exception:
    HAVE_CV = False


class Site:
    """A throwaway site/ folder with an About page that shows two headshots."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp())
        self.site = self.root / "site"
        self.img = self.site / "img"
        (self.site / "pages").mkdir(parents=True)
        self.img.mkdir()
        shutil.copy(FIX / "headshot-a.webp", self.img / "hs-tyler-sq.webp")
        shutil.copy(FIX / "headshot-b.webp", self.img / "hs-adam-sq.webp")
        Image.new("RGB", (50, 50), "red").save(self.img / "xhs-tyler-sq.webp")    # look-alike name
        self.about = self.site / "pages/about.html"
        self.about.write_text(
            "<!--\nimage: img/hs-tyler-sq.webp\n-->\n"
            '<img src="/img/hs-tyler-sq.webp" width="150" height="150">'
            '<img src="/img/hs-adam-sq.webp"><img src="/img/xhs-tyler-sq.webp">'
            '<meta property="og:image" content="https://ascendpoint.agency/img/hs-tyler-sq.webp">')
        self.patch = mock.patch.multiple(iw, ROOT=self.root, SITE=self.site, IMG=self.img)

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *a):
        self.patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def run(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = iw.main([str(a) for a in argv])
            except SystemExit as e:
                code, msg = (e.code if isinstance(e.code, int) else 2), e.code
                if not isinstance(msg, int):
                    print(msg)
        return code, out.getvalue()


def size_of(path) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size


def photo(path: Path, size=(1122, 1402), colour=(90, 110, 140)) -> Path:
    Image.new("RGB", size, colour).save(path)
    return path


class ReplaceTests(unittest.TestCase):
    def test_swaps_to_a_new_name_and_updates_every_reference(self):
        with Site() as s:
            src = photo(s.root / "Tyler Headshot.png")
            code, out = s.run(src, "--replace", s.img / "hs-tyler-sq.webp")
            self.assertEqual(code, 0, out)
            new = [p for p in s.img.glob("hs-tyler-sq-*.webp")]
            self.assertEqual(len(new), 1)
            self.assertRegex(new[0].name, r"^hs-tyler-sq-[0-9a-f]{6}\.webp$")
            self.assertEqual(size_of(new[0]), (320, 320))        # same size + shape as before
            self.assertFalse((s.img / "hs-tyler-sq.webp").exists())
            html = s.about.read_text()
            self.assertEqual(html.count(new[0].name), 3)                  # src, front matter, full URL
            self.assertNotIn("/img/hs-tyler-sq.webp", html)
            self.assertIn('src="/img/xhs-tyler-sq.webp"', html)          # look-alike untouched
            self.assertIn('src="/img/hs-adam-sq.webp"', html)
            self.assertTrue((s.img / "xhs-tyler-sq.webp").exists())
            self.assertIn("updated 3 reference(s)", out)

    def test_replacing_again_drops_the_old_hash(self):
        with Site() as s:
            s.run(photo(s.root / "a.png"), "--replace", s.img / "hs-tyler-sq.webp")
            first = next(s.img.glob("hs-tyler-sq-*.webp"))
            code, out = s.run(photo(s.root / "b.png", colour=(10, 200, 10)), "--replace", first)
            self.assertEqual(code, 0, out)
            second = next(s.img.glob("hs-tyler-sq-*.webp"))
            self.assertNotEqual(first.name, second.name)
            self.assertRegex(second.name, r"^hs-tyler-sq-[0-9a-f]{6}\.webp$")    # not ...-abc123-def456
            self.assertIn(second.name, s.about.read_text())

    def test_wordpress_size_variants_are_regenerated(self):
        with Site() as s:
            (s.img / "up").mkdir()
            Image.new("RGB", (600, 750), "grey").save(s.img / "up/sonia.webp")
            Image.new("RGB", (240, 300), "grey").save(s.img / "up/sonia-240x300.webp")
            s.about.write_text('<img src="/img/up/sonia.webp" srcset="/img/up/sonia.webp 600w, '
                               '/img/up/sonia-240x300.webp 240w">')
            code, out = s.run(photo(s.root / "new.png", (2000, 2500)), "--replace", s.img / "up/sonia.webp")
            self.assertEqual(code, 0, out)
            html = s.about.read_text()
            main = next(p for p in (s.img / "up").iterdir() if not p.stem.endswith("240x300"))
            variant = next(p for p in (s.img / "up").iterdir() if p.stem.endswith("240x300"))
            self.assertEqual(size_of(main), (600, 750))
            self.assertEqual(size_of(variant), (240, 300))
            self.assertEqual(variant.name, main.stem + "-240x300.webp")
            self.assertIn(f"/img/up/{main.name} 600w", html)
            self.assertIn(f"/img/up/{variant.name} 240w", html)
            self.assertNotIn("sonia-240x300.webp", html)

    def test_refuses_an_image_no_page_uses(self):
        with Site() as s:
            Image.new("RGB", (100, 100)).save(s.img / "unused.webp")
            code, out = s.run(photo(s.root / "a.png"), "--replace", s.img / "unused.webp")
            self.assertNotEqual(code, 0)
            self.assertIn("No page uses", out)
            self.assertTrue((s.img / "unused.webp").exists())

    def test_refuses_files_outside_the_image_folder(self):
        with Site() as s:
            code, out = s.run(photo(s.root / "a.png"), "--replace", s.site / "pages/about.html")
            self.assertNotEqual(code, 0)

    def test_small_photo_is_not_upscaled(self):
        with Site() as s:
            code, out = s.run(photo(s.root / "tiny.png", (200, 260)), "--replace", s.img / "hs-tyler-sq.webp")
            self.assertEqual(code, 0, out)
            self.assertEqual(Image.open(next(s.img.glob("hs-tyler-sq-*.webp"))).size, (200, 200))

    @unittest.skipUnless(HAVE_CV, "OpenCV not installed")
    def test_headshot_is_framed_on_the_face_like_the_old_one(self):
        with Site() as s:
            # a "new photo": a known face placed off-centre, small, in a big portrait frame
            face = Image.open(FIX / "headshot-b.webp").convert("RGB")
            big = Image.new("RGB", (1500, 1900), (200, 200, 205))
            big.paste(face, (900, 250))
            src = s.root / "new.png"
            big.save(src)
            old_face = iw.find_face(Image.open(s.img / "hs-tyler-sq.webp"))
            code, out = s.run(src, "--replace", s.img / "hs-tyler-sq.webp")
            self.assertEqual(code, 0, out)
            self.assertIn("framed on the face", out)
            result = Image.open(next(s.img.glob("hs-tyler-sq-*.webp")))
            x, y, w, h = iw.find_face(result)
            self.assertAlmostEqual((x + w / 2) / 320, 0.5, delta=0.08)                  # centred
            self.assertAlmostEqual(w / 320, old_face[2] / 320, delta=0.1)              # same size face
            self.assertAlmostEqual((y + h / 2) / 320, (old_face[1] + old_face[3] / 2) / 320, delta=0.1)


class NewImageTests(unittest.TestCase):
    def test_exact_size_crop(self):
        with Site() as s:
            code, out = s.run(photo(s.root / "a.png", (3000, 2000)), s.img / "hero.webp", "--size", "1600x900")
            self.assertEqual(code, 0, out)
            self.assertEqual(size_of(s.img / "hero.webp"), (1600, 900))

    def test_width_only_keeps_shape_and_never_upscales(self):
        with Site() as s:
            s.run(photo(s.root / "a.png", (3000, 2000)), s.img / "a.webp", "--width", "1200")
            self.assertEqual(size_of(s.img / "a.webp"), (1200, 800))
            s.run(photo(s.root / "b.png", (400, 300)), s.img / "b.webp", "--width", "1200")
            self.assertEqual(size_of(s.img / "b.webp"), (400, 300))

    def test_will_not_overwrite_a_picture_in_place(self):
        with Site() as s:
            before = (s.img / "hs-adam-sq.webp").read_bytes()
            code, out = s.run(photo(s.root / "a.png"), s.img / "hs-adam-sq.webp")
            self.assertNotEqual(code, 0)
            self.assertIn("--replace", out)
            self.assertEqual((s.img / "hs-adam-sq.webp").read_bytes(), before)

    def test_destination_must_be_in_the_image_folder(self):
        with Site() as s:
            code, out = s.run(photo(s.root / "a.png"), s.site / "pages/x.webp")
            self.assertNotEqual(code, 0)
            self.assertFalse((s.site / "pages/x.webp").exists())

    def test_png_with_transparency_to_jpg(self):
        with Site() as s:
            Image.new("RGBA", (100, 100), (255, 0, 0, 0)).save(s.root / "logo.png")
            code, out = s.run(s.root / "logo.png", s.img / "logo.jpg")
            self.assertEqual(code, 0, out)
            self.assertEqual(Image.open(s.img / "logo.jpg").getpixel((5, 5)), (255, 255, 255))

    def test_exif_rotation_is_respected(self):
        with Site() as s:
            im = Image.new("RGB", (400, 200), "blue")
            exif = im.getexif()
            exif[0x0112] = 6                      # "rotate 90": a phone photo taken upright
            im.save(s.root / "phone.jpg", exif=exif)
            s.run(s.root / "phone.jpg", s.img / "phone.webp")
            self.assertEqual(size_of(s.img / "phone.webp"), (200, 400))


class InfoAndPrepareTests(unittest.TestCase):
    def test_info_lists_pages_using_an_image(self):
        with Site() as s:
            code, out = s.run("--info", s.img / "hs-tyler-sq.webp")
            d = json.loads(out.splitlines()[0])
            self.assertEqual((d["width"], d["height"], d["orientation"]), (320, 320, "square"))
            self.assertEqual(d["used_in"], ["site/pages/about.html"])

    def test_prepare_flags_a_sign_in_page_and_makes_viewable_copies(self):
        with Site() as s:
            d = s.root / "files"
            d.mkdir()
            Image.new("RGB", (5000, 3000), "white").save(d / "huge.png")
            Image.new("RGB", (600, 800), "white").save(d / "photo.png")
            Image.new("RGB", (300, 300), "white").save(d / "scan.tiff")
            (d / "Tyler.png").write_bytes(b"<!DOCTYPE html><html><title>Slack</title></html>")
            (d / "notes.pdf").write_bytes(b"%PDF-1.4 ...")
            code, out = s.run("--prepare", d)
            facts = {r["file"]: r for r in map(json.loads, out.splitlines())}
            self.assertIn("web page", facts["Tyler.png"]["kind"])
            self.assertEqual(facts["notes.pdf"]["kind"], "not an image")
            self.assertEqual((facts["photo.png"]["width"], facts["photo.png"]["height"]), (600, 800))
            self.assertNotIn("view", facts["photo.png"])
            self.assertEqual(facts["huge.png"]["view"], "huge.view.jpg")         # too big to look at
            self.assertEqual(facts["scan.tiff"]["view"], "scan.view.jpg")        # format Claude can't open
            self.assertLessEqual(max(size_of(d / "huge.view.jpg")), 1600)


def _heif_ok():
    try:
        import pillow_heif  # noqa: F401
        return True
    except Exception:
        return False


class HeicTests(unittest.TestCase):
    @unittest.skipUnless(_heif_ok(), "pillow-heif not installed")
    def test_iphone_heic_photo_is_converted(self):
        with Site() as s:
            Image.new("RGB", (800, 1000), (120, 80, 60)).save(s.root / "IMG_0001.HEIC", format="HEIF")
            code, out = s.run(s.root / "IMG_0001.HEIC", s.img / "from-phone.webp")
            self.assertEqual(code, 0, out)
            self.assertEqual(size_of(s.img / "from-phone.webp"), (800, 1000))


if __name__ == "__main__":
    unittest.main()
