"""Tests for the mandatory part: displaying metadata (scorpion.py)."""

import contextlib
import io
import os
import tempfile
import unittest

from tests import helpers as h
import scorpion


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = scorpion.main(list(argv))
        except SystemExit as exit_:
            status = exit_.code
    return status, out.getvalue(), err.getvalue()


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path


class TestParsers(unittest.TestCase):
    def test_tiff_little_endian(self):
        sections = scorpion.parse_tiff(h.build_tiff("<"))
        self.assertEqual(sections["IFD0"]["Make"], "Canon")
        self.assertEqual(sections["IFD0"]["Model"], "EOS 5D")
        self.assertEqual(sections["Exif"]["DateTimeOriginal"], "2020:01:02 03:04:05")
        self.assertEqual(sections["GPS"]["GPSPosition (decimal)"], "48.858333, 2.350000")

    def test_tiff_big_endian(self):
        sections = scorpion.parse_tiff(h.build_tiff(">"))
        self.assertEqual(sections["IFD0"]["Make"], "Canon")
        self.assertEqual(sections["GPS"]["GPSLatitudeRef"], "N")

    def test_tiff_garbage_returns_empty(self):
        self.assertEqual(scorpion.parse_tiff(b""), {})
        self.assertEqual(scorpion.parse_tiff(b"XX\x00\x00\x00\x00\x00\x00"), {})
        self.assertEqual(scorpion.parse_tiff(b"II*\x00\xff\xff\xff\x7f"), {})

    def test_jpeg(self):
        info, sections = scorpion.read_jpeg(
            h.make_jpeg(exif=h.build_tiff(), comment=b"hi", xmp=True))
        self.assertEqual(info["Format"], "JPEG")
        self.assertEqual(info["Dimensions"], "40 x 30")
        self.assertEqual(info["JFIF version"], "1.01")
        self.assertEqual(info["Comment"], "hi")
        self.assertEqual(sections["IFD0"]["Make"], "Canon")
        self.assertIn("XMP", sections)

    def test_jpeg_without_exif(self):
        _, sections = scorpion.read_jpeg(h.make_jpeg())
        self.assertEqual(sections, {})

    def test_png(self):
        info, sections = scorpion.read_png(
            h.make_png(text={"Author": "Bob"}, exif=h.build_tiff(), time=True))
        self.assertEqual(info["Dimensions"], "4 x 3")
        self.assertEqual(info["Color type"], "RGB")
        self.assertEqual(info["Last modification (tIME)"], "2021-05-06 07:08:09")
        self.assertEqual(sections["Text chunks"], {"Author": "Bob"})
        self.assertEqual(sections["IFD0"]["Model"], "EOS 5D")

    def test_png_long_text_is_truncated(self):
        _, sections = scorpion.read_png(h.make_png(text={"K": "x" * 500}))
        self.assertTrue(sections["Text chunks"]["K"].endswith("..."))

    def test_gif(self):
        info, _ = scorpion.read_gif(h.make_gif(comment=b"yo", loop=True))
        self.assertEqual(info["Format"], "GIF")
        self.assertEqual(info["Dimensions"], "3 x 2")
        self.assertEqual(info["Frames"], "1")
        self.assertEqual(info["Loop count"], "infinite")
        self.assertEqual(info["Comment"], "yo")

    def test_bmp(self):
        info, _ = scorpion.read_bmp(h.make_bmp(5, 7))
        self.assertEqual(info["Dimensions"], "5 x 7")
        self.assertEqual(info["Bits per pixel"], "24")
        self.assertEqual(info["Compression"], "none")

    def test_truncated_headers_raise(self):
        with self.assertRaises(scorpion.MetadataError):
            scorpion.read_bmp(b"BM" + b"\x00" * 10)
        with self.assertRaises(scorpion.MetadataError):
            scorpion.read_gif(b"GIF89a\x00")

    def test_detect_reader(self):
        self.assertIs(scorpion.detect_reader(h.make_jpeg()), scorpion.read_jpeg)
        self.assertIs(scorpion.detect_reader(h.make_png()), scorpion.read_png)
        self.assertIs(scorpion.detect_reader(h.make_gif()), scorpion.read_gif)
        self.assertIs(scorpion.detect_reader(h.make_bmp()), scorpion.read_bmp)
        self.assertIsNone(scorpion.detect_reader(b"hello world"))


class TestCLI(TempDirCase):
    def test_displays_exif_for_each_format(self):
        files = {
            "a.jpg": h.make_jpeg(exif=h.build_tiff()),
            "b.png": h.make_png(text={"Author": "Bob"}),
            "c.gif": h.make_gif(comment=b"c"),
            "d.bmp": h.make_bmp(),
            "e.jpeg": h.make_jpeg(),
        }
        paths = [self.write(n, d) for n, d in files.items()]
        status, out, err = run(*paths)
        self.assertEqual(status, 0, err)
        self.assertEqual(err, "")
        for token in ("Canon", "Author", "GIF", "BMP", "Creation date",
                      "Last modified", "Dimensions"):
            self.assertIn(token, out)
        self.assertEqual(out.count("==="), 2 * len(paths))

    def test_extension_is_case_insensitive(self):
        status, out, _ = run(self.write("UP.JPG", h.make_jpeg()))
        self.assertEqual(status, 0)
        self.assertIn("JPEG", out)

    def test_no_arguments_is_usage_error(self):
        self.assertEqual(run()[0], 2)

    def test_missing_file(self):
        status, out, err = run(os.path.join(self.dir, "nope.jpg"))
        self.assertEqual(status, 1)
        self.assertIn("not a regular file", err)

    def test_directory_is_rejected(self):
        status, _, err = run(self.dir)
        self.assertEqual(status, 1)
        self.assertIn("not a regular file", err)

    def test_unsupported_extension(self):
        status, _, err = run(self.write("a.txt", h.make_jpeg()))
        self.assertEqual(status, 1)
        self.assertIn("unsupported extension", err)

    def test_fake_image_content(self):
        status, _, err = run(self.write("fake.png", b"not an image at all"))
        self.assertEqual(status, 1)
        self.assertIn("not a valid", err)

    def test_corrupted_image_does_not_crash(self):
        for name, data in (("t.jpg", h.make_jpeg()[:30]),
                           ("t.bmp", b"BM" + b"\x00" * 6),
                           ("t.gif", b"GIF89a"),
                           ("t.png", h.make_png()[:20]),
                           ("e.jpg", b"\xff\xd8")):
            status, _, err = run(self.write(name, data))
            self.assertIn(status, (0, 1), name)
            self.assertNotIn("Traceback", err, name)

    def test_one_bad_file_does_not_stop_the_others(self):
        good = self.write("ok.png", h.make_png())
        bad = self.write("bad.png", b"zzz")
        status, out, err = run(bad, good)
        self.assertEqual(status, 1)
        self.assertIn("bad.png", err)
        self.assertIn("PNG", out)

    def test_unicode_filename(self):
        status, out, _ = run(self.write("café.png", h.make_png()))
        self.assertEqual(status, 0)
        self.assertIn("café.png", out)


if __name__ == "__main__":
    unittest.main()
