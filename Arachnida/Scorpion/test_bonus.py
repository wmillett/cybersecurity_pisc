"""Tests for the bonus part: modifying / deleting metadata (bonus.py)."""

import os
import stat
import unittest

from tests import helpers as h
from tests.test_scorpion import TempDirCase, run
import bonus
import scorpion


def read_meta(path):
    with open(path, "rb") as f:
        data = f.read()
    return scorpion.detect_reader(data)(data)


class TestExifRoundTrip(unittest.TestCase):
    def test_parse_serialize_is_lossless_for_known_tags(self):
        for end in ("<", ">"):
            original = h.build_tiff(end)
            rebuilt = bonus.Exif.parse(original).serialize()
            self.assertEqual(scorpion.parse_tiff(rebuilt),
                             scorpion.parse_tiff(original), end)

    def test_endianness_is_preserved(self):
        self.assertTrue(bonus.Exif.parse(h.build_tiff(">")).serialize().startswith(b"MM"))
        self.assertTrue(bonus.Exif.parse(h.build_tiff("<")).serialize().startswith(b"II"))

    def test_invalid_header(self):
        with self.assertRaises(bonus.EditError):
            bonus.Exif.parse(b"garbage!")


class TestJpeg(unittest.TestCase):
    def edit(self, data, sets=None, deletes=None, strip=False):
        return bonus.edit_jpeg(data, sets or {}, deletes or [], strip)

    def test_modify_existing_tag(self):
        new, report = self.edit(h.make_jpeg(exif=h.build_tiff()), {"Make": "Nikon D850"})
        _, sections = scorpion.read_jpeg(new)
        self.assertEqual(sections["IFD0"]["Make"], "Nikon D850")
        self.assertEqual(sections["IFD0"]["Model"], "EOS 5D")  # untouched
        self.assertEqual(sections["GPS"]["GPSPosition (decimal)"], "48.858333, 2.350000")
        self.assertTrue(any("Make" in line for line in report))

    def test_tag_names_are_case_insensitive(self):
        new, _ = self.edit(h.make_jpeg(exif=h.build_tiff()), {"artist": "me"})
        self.assertEqual(scorpion.read_jpeg(new)[1]["IFD0"]["Artist"], "me")

    def test_modify_on_jpeg_without_exif_creates_it(self):
        new, _ = self.edit(h.make_jpeg(), {"Artist": "Jane", "DateTimeOriginal": "2024:12:31 23:59:59"})
        _, sections = scorpion.read_jpeg(new)
        self.assertEqual(sections["IFD0"]["Artist"], "Jane")
        self.assertEqual(sections["Exif"]["DateTimeOriginal"], "2024:12:31 23:59:59")

    def test_value_longer_than_original(self):
        long = "x" * 300
        new, _ = self.edit(h.make_jpeg(exif=h.build_tiff()), {"Make": long})
        self.assertEqual(scorpion.read_jpeg(new)[1]["IFD0"]["Make"], long)

    def test_user_comment(self):
        new, _ = self.edit(h.make_jpeg(), {"UserComment": "secret note"})
        self.assertEqual(scorpion.read_jpeg(new)[1]["Exif"]["UserComment"], "secret note")

    def test_user_comment_rejects_non_ascii(self):
        with self.assertRaises(bonus.EditError):
            self.edit(h.make_jpeg(), {"UserComment": "café"})

    def test_utf8_in_text_tag(self):
        new, _ = self.edit(h.make_jpeg(), {"Artist": "José"})
        self.assertEqual(scorpion.read_jpeg(new)[1]["IFD0"]["Artist"], "José")

    def test_invalid_date_rejected(self):
        for bad in ("yesterday", "2020-01-02 03:04:05", "2020:13:40 00:00:00"):
            with self.assertRaises(bonus.EditError, msg=bad):
                self.edit(h.make_jpeg(), {"DateTimeOriginal": bad})

    def test_non_text_tag_rejected(self):
        with self.assertRaises(bonus.EditError):
            self.edit(h.make_jpeg(), {"FNumber": "2.8"})

    def test_unknown_tag_rejected(self):
        with self.assertRaises(bonus.EditError):
            self.edit(h.make_jpeg(), {"NoSuchTag": "1"})

    def test_delete_tag(self):
        new, _ = self.edit(h.make_jpeg(exif=h.build_tiff()), deletes=["Make"])
        ifd0 = scorpion.read_jpeg(new)[1]["IFD0"]
        self.assertNotIn("Make", ifd0)
        self.assertIn("Model", ifd0)

    def test_delete_gps_section(self):
        new, _ = self.edit(h.make_jpeg(exif=h.build_tiff()), deletes=["GPS"])
        sections = scorpion.read_jpeg(new)[1]
        self.assertNotIn("GPS", sections)
        self.assertIn("Exif", sections)

    def test_delete_last_tags_removes_exif_segment(self):
        data = h.make_jpeg(exif=h.build_tiff(gps=False))
        new, _ = self.edit(data, deletes=["Make", "Model", "DateTimeOriginal"])
        self.assertNotIn(b"Exif\x00\x00", new)

    def test_delete_missing_tag_is_not_an_error(self):
        data = h.make_jpeg(exif=h.build_tiff())
        new, report = self.edit(data, deletes=["Artist"])
        self.assertTrue(any("not found" in line for line in report))
        self.assertEqual(scorpion.read_jpeg(new), scorpion.read_jpeg(data))

    def test_comment_set_and_delete(self):
        new, _ = self.edit(h.make_jpeg(comment=b"old"), {"Comment": "new"})
        self.assertEqual(scorpion.read_jpeg(new)[0]["Comment"], "new")
        self.assertEqual(new.count(b"\xff\xfe"), 1)
        new, _ = self.edit(new, deletes=["Comment"])
        self.assertNotIn("Comment", scorpion.read_jpeg(new)[0])

    def test_delete_xmp(self):
        new, _ = self.edit(h.make_jpeg(xmp=True), deletes=["XMP"])
        self.assertNotIn(b"xmpmeta", new)

    def test_strip_all(self):
        data = h.make_jpeg(exif=h.build_tiff(), comment=b"c", xmp=True)
        new, _ = self.edit(data, strip=True)
        info, sections = scorpion.read_jpeg(new)
        self.assertEqual(sections, {})
        self.assertNotIn("Comment", info)
        self.assertEqual(info["JFIF version"], "1.01")      # kept
        self.assertEqual(info["Dimensions"], "40 x 30")     # kept

    def test_pixel_data_is_untouched(self):
        data = h.make_jpeg(exif=h.build_tiff())
        for kwargs in ({"sets": {"Make": "X" * 50}}, {"deletes": ["GPS"]},
                       {"strip": True}):
            new, _ = self.edit(data, **kwargs)
            self.assertTrue(new.endswith(h.SCAN), kwargs)
            self.assertTrue(new.startswith(b"\xff\xd8"))

    def test_big_endian_exif(self):
        new, _ = self.edit(h.make_jpeg(exif=h.build_tiff(">")), {"Make": "Leica"})
        self.assertIn(b"MM", new)
        self.assertEqual(scorpion.read_jpeg(new)[1]["IFD0"]["Make"], "Leica")

    def test_repeated_edits_are_stable(self):
        data = h.make_jpeg(exif=h.build_tiff())
        once, _ = self.edit(data, {"Artist": "A"})
        twice, _ = self.edit(once, {"Artist": "A"})
        self.assertEqual(once, twice)

    def test_truncated_jpeg(self):
        with self.assertRaises(bonus.EditError):
            self.edit(h.make_jpeg()[:25], {"Make": "x"})

    def test_too_large_metadata(self):
        with self.assertRaises(bonus.EditError):
            self.edit(h.make_jpeg(), {"ImageDescription": "x" * 70000})


class TestPng(unittest.TestCase):
    def edit(self, data, sets=None, deletes=None, strip=False):
        new, report = bonus.edit_png(data, sets or {}, deletes or [], strip)
        h.png_chunks(new)  # asserts every CRC is valid
        return new, report

    def test_add_text_chunk(self):
        new, _ = self.edit(h.make_png(), {"Author": "Alice"})
        self.assertEqual(scorpion.read_png(new)[1]["Text chunks"], {"Author": "Alice"})

    def test_replace_text_chunk(self):
        new, _ = self.edit(h.make_png(text={"Author": "Bob", "Title": "T"}),
                           {"author": "Alice"})
        self.assertEqual(scorpion.read_png(new)[1]["Text chunks"],
                         {"Title": "T", "Author": "Alice"})

    def test_text_chunks_precede_idat(self):
        new, _ = self.edit(h.make_png(), {"Author": "Alice"})
        types = [t for t, _ in h.png_chunks(new)]
        self.assertLess(types.index(b"tEXt"), types.index(b"IDAT"))
        self.assertEqual(types[-1], b"IEND")

    def test_non_latin1_value_uses_itxt(self):
        new, _ = self.edit(h.make_png(), {"Title": "日本語"})
        self.assertIn(b"iTXt", [t for t, _ in h.png_chunks(new)])
        self.assertEqual(scorpion.read_png(new)[1]["Text chunks"]["Title"], "日本語")

    def test_bad_text_key(self):
        for key in ("k" * 80, "日本"):
            with self.assertRaises(bonus.EditError, msg=key):
                self.edit(h.make_png(), {key: "v"})

    def test_delete_text_chunk(self):
        new, _ = self.edit(h.make_png(text={"Author": "Bob", "Title": "T"}),
                           deletes=["Author"])
        self.assertEqual(scorpion.read_png(new)[1]["Text chunks"], {"Title": "T"})

    def test_modify_exif_in_png(self):
        new, _ = self.edit(h.make_png(exif=h.build_tiff()), {"Make": "Sony"})
        sections = scorpion.read_png(new)[1]
        self.assertEqual(sections["IFD0"]["Make"], "Sony")
        self.assertEqual(sections["IFD0"]["Model"], "EOS 5D")

    def test_create_exif_in_png(self):
        new, _ = self.edit(h.make_png(), {"Artist": "Zed"})
        self.assertEqual(scorpion.read_png(new)[1]["IFD0"]["Artist"], "Zed")

    def test_existing_text_chunk_wins_over_exif_name(self):
        new, _ = self.edit(h.make_png(text={"Software": "gimp"}), {"Software": "x"})
        sections = scorpion.read_png(new)[1]
        self.assertEqual(sections["Text chunks"]["Software"], "x")
        self.assertNotIn("IFD0", sections)

    def test_delete_gps_and_time(self):
        new, _ = self.edit(h.make_png(exif=h.build_tiff(), time=True),
                           deletes=["GPS", "tIME"])
        info, sections = scorpion.read_png(new)
        self.assertNotIn("GPS", sections)
        self.assertNotIn("Last modification (tIME)", info)

    def test_strip_all(self):
        new, _ = self.edit(h.make_png(text={"a": "b"}, exif=h.build_tiff(), time=True),
                           strip=True)
        types = [t for t, _ in h.png_chunks(new)]
        self.assertEqual(types, [b"IHDR", b"IDAT", b"IEND"])

    def test_image_data_is_untouched(self):
        data = h.make_png(text={"a": "b"})
        idat = [b for t, b in h.png_chunks(data) if t == b"IDAT"]
        new, _ = self.edit(data, {"Author": "x"})
        self.assertEqual([b for t, b in h.png_chunks(new) if t == b"IDAT"], idat)
        new, _ = self.edit(data, strip=True)
        self.assertEqual([b for t, b in h.png_chunks(new) if t == b"IDAT"], idat)

    def test_truncated_png(self):
        with self.assertRaises(bonus.EditError):
            self.edit(h.make_png()[:40], {"a": "b"})


class TestGif(unittest.TestCase):
    def edit(self, data, sets=None, deletes=None, strip=False):
        return bonus.edit_gif(data, sets or {}, deletes or [], strip)

    def test_set_comment_when_none(self):
        new, _ = self.edit(h.make_gif(), {"Comment": "hello"})
        info, _ = scorpion.read_gif(new)
        self.assertEqual(info["Comment"], "hello")
        self.assertEqual(info["Frames"], "1")

    def test_replace_comment(self):
        new, _ = self.edit(h.make_gif(comment=b"old"), {"Comment": "new"})
        self.assertEqual(scorpion.read_gif(new)[0]["Comment"], "new")

    def test_long_comment_spans_sub_blocks(self):
        text = "y" * 600
        new, _ = self.edit(h.make_gif(), {"Comment": text})
        self.assertEqual(scorpion.read_gif(new)[0]["Comment"], text)

    def test_delete_comment(self):
        new, _ = self.edit(h.make_gif(comment=b"old"), deletes=["Comment"])
        self.assertNotIn("Comment", scorpion.read_gif(new)[0])

    def test_strip_keeps_animation_loop(self):
        new, _ = self.edit(h.make_gif(comment=b"old", loop=True), strip=True)
        info, _ = scorpion.read_gif(new)
        self.assertNotIn("Comment", info)
        self.assertEqual(info["Loop count"], "infinite")
        self.assertEqual(info["Frames"], "1")

    def test_other_tags_rejected(self):
        with self.assertRaises(bonus.EditError):
            self.edit(h.make_gif(), {"Artist": "x"})

    def test_image_block_is_untouched(self):
        data = h.make_gif(comment=b"c")
        new, _ = self.edit(data, strip=True)
        self.assertIn(b"\x2c" + data.split(b"\x2c", 1)[1], new)

    def test_truncated_gif(self):
        with self.assertRaises(bonus.EditError):
            self.edit(h.make_gif()[:-6], {"Comment": "x"})


class TestEditFile(TempDirCase):
    def test_edits_in_place(self):
        path = self.write("a.jpg", h.make_jpeg(exif=h.build_tiff()))
        bonus.edit_file(path, sets={"Artist": "Me"})
        self.assertEqual(read_meta(path)[1]["IFD0"]["Artist"], "Me")

    def test_backup(self):
        original = h.make_jpeg(exif=h.build_tiff())
        path = self.write("a.jpg", original)
        bonus.edit_file(path, deletes=["Make"], backup=True)
        with open(path + ".bak", "rb") as f:
            self.assertEqual(f.read(), original)

    def test_no_backup_without_flag_and_no_temp_leftovers(self):
        path = self.write("a.jpg", h.make_jpeg(exif=h.build_tiff()))
        bonus.edit_file(path, deletes=["Make"])
        self.assertEqual(os.listdir(self.dir), ["a.jpg"])

    def test_permissions_preserved(self):
        path = self.write("a.png", h.make_png())
        os.chmod(path, 0o600)
        bonus.edit_file(path, sets={"Author": "x"})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_no_change_does_not_rewrite_or_backup(self):
        path = self.write("a.jpg", h.make_jpeg())
        report = bonus.edit_file(path, deletes=["Make"], backup=True)
        self.assertIn("no change needed", report)
        self.assertFalse(os.path.exists(path + ".bak"))

    def test_failed_edit_leaves_file_untouched(self):
        original = h.make_jpeg(exif=h.build_tiff())
        path = self.write("a.jpg", original)
        with self.assertRaises(bonus.EditError):
            # first op is valid, second is not: nothing may be written
            bonus.edit_file(path, sets={"Artist": "ok", "DateTimeOriginal": "bad"})
        with open(path, "rb") as f:
            self.assertEqual(f.read(), original)
        self.assertEqual(os.listdir(self.dir), ["a.jpg"])

    def test_rejections(self):
        cases = {
            "missing": os.path.join(self.dir, "nope.jpg"),
            "wrong ext": self.write("a.txt", h.make_jpeg()),
            "fake": self.write("fake.png", b"nothing"),
            "bmp": self.write("a.bmp", h.make_bmp()),
        }
        for label, path in cases.items():
            with self.assertRaises(bonus.EditError, msg=label):
                bonus.edit_file(path, sets={"Artist": "x"})

    def test_strip_all_every_format(self):
        files = {
            "a.jpg": h.make_jpeg(exif=h.build_tiff(), comment=b"c"),
            "b.png": h.make_png(text={"k": "v"}, exif=h.build_tiff()),
            "c.gif": h.make_gif(comment=b"c"),
        }
        for name, data in files.items():
            path = self.write(name, data)
            bonus.edit_file(path, strip_all=True)
            _, sections = read_meta(path)
            self.assertEqual(sections, {}, name)


class TestScorpionCLIBonus(TempDirCase):
    def test_modify_then_display(self):
        path = self.write("a.jpg", h.make_jpeg(exif=h.build_tiff()))
        status, out, err = run("-m", "Artist=Jane Doe", "-m", "Make=Nikon", path)
        self.assertEqual(status, 0, err)
        self.assertIn("set EXIF Artist", out)
        self.assertIn("Jane Doe", out)
        self.assertIn("Nikon", out)
        self.assertEqual(read_meta(path)[1]["IFD0"]["Make"], "Nikon")

    def test_value_may_contain_equals_sign(self):
        path = self.write("a.jpg", h.make_jpeg())
        run("-m", "ImageDescription=a=b", path)
        self.assertEqual(read_meta(path)[1]["IFD0"]["ImageDescription"], "a=b")

    def test_delete_and_strip(self):
        path = self.write("a.jpg", h.make_jpeg(exif=h.build_tiff()))
        self.assertEqual(run("-d", "GPS", "-d", "Model", path)[0], 0)
        sections = read_meta(path)[1]
        self.assertNotIn("GPS", sections)
        self.assertNotIn("Model", sections["IFD0"])
        status, out, _ = run("-s", path)
        self.assertEqual(status, 0)
        self.assertIn("No EXIF", out)

    def test_backup_flag(self):
        path = self.write("a.png", h.make_png())
        run("-m", "Author=x", "-b", path)
        self.assertTrue(os.path.exists(path + ".bak"))

    def test_multiple_files_continue_after_error(self):
        bad = self.write("a.bmp", h.make_bmp())
        good = self.write("b.gif", h.make_gif())
        status, out, err = run("-m", "Comment=hi", bad, good)
        self.assertEqual(status, 1)
        self.assertIn("no editable metadata", err)
        self.assertEqual(read_meta(good)[0]["Comment"], "hi")

    def test_edit_error_gives_status_1_and_message(self):
        path = self.write("a.jpg", h.make_jpeg())
        status, _, err = run("-m", "DateTimeOriginal=nope", path)
        self.assertEqual(status, 1)
        self.assertIn("YYYY:MM:DD", err)

    def test_argument_errors(self):
        path = self.write("a.jpg", h.make_jpeg())
        self.assertEqual(run("-m", "noequals", path)[0], 2)
        self.assertEqual(run("-m", "=v", path)[0], 2)
        self.assertEqual(run("-s", "-d", "Make", path)[0], 2)
        self.assertEqual(run("-b", path)[0], 2)

    def test_display_only_never_modifies(self):
        data = h.make_jpeg(exif=h.build_tiff())
        path = self.write("a.jpg", data)
        run(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), data)


if __name__ == "__main__":
    unittest.main()
