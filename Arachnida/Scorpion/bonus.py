"""bonus - modify / delete the metadata of image files (used by scorpion.py).

Not meant to be run directly: scorpion.py imports it when one of the
-m / -d / -s options is given.

Supported operations
    modify TAG=VALUE   set a text tag
    delete TAG         remove a tag
    strip all          remove every piece of personal metadata

Per format
    JPEG  EXIF text tags (Make, Model, Software, Artist, Copyright, DateTime*,
          ImageDescription, UserComment, ...), "GPS" (whole section),
          "Comment", "XMP", "IPTC".
    PNG   any text chunk (tEXt / zTXt / iTXt, arbitrary key), EXIF text tags
          stored in an eXIf chunk, "GPS", "tIME".
    GIF   "Comment".
    BMP   carries no editable metadata.

Every edit is computed fully in memory first and the file is replaced
atomically, so a failure never leaves a half-written image behind.
Pixel data is never touched.

Known limit: when the EXIF block is rewritten, the MakerNote and the embedded
thumbnail are dropped (they contain absolute offsets that cannot be moved
safely).
"""

import datetime
import os
import shutil
import struct
import tempfile
import zlib

from scorpion import EXIF_TAGS, EXTENSIONS, GPS_TAGS, IFD0_TAGS, TYPES


class EditError(Exception):
    """Raised when a file cannot be edited."""


# --------------------------------------------------------------------------- #
# Tag name resolution
# --------------------------------------------------------------------------- #
_NAME_TO_TAG = {}
for _section, _table in (("IFD0", IFD0_TAGS), ("Exif", EXIF_TAGS),
                         ("GPS", GPS_TAGS)):
    for _tag, _name in _table.items():
        _NAME_TO_TAG.setdefault(_name.lower(), (_section, _tag, _name))

# (section, tag) pairs that can be written; the value is an ASCII string.
_WRITABLE = {
    ("IFD0", 0x010E), ("IFD0", 0x010F), ("IFD0", 0x0110), ("IFD0", 0x0131),
    ("IFD0", 0x0132), ("IFD0", 0x013B), ("IFD0", 0x8298),
    ("Exif", 0x9003), ("Exif", 0x9004), ("Exif", 0xA420), ("Exif", 0xA430),
    ("Exif", 0xA431), ("Exif", 0xA433), ("Exif", 0xA434),
    ("Exif", 0x9286),  # UserComment (undefined type, 8-byte charset prefix)
}
_USER_COMMENT = ("Exif", 0x9286)
_DATE_TAGS = {0x0132, 0x9003, 0x9004}
_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
# Tags whose value is an offset into the old TIFF block: unsafe to move.
_DROPPED_TAGS = {0x927C, 0xA005, 0x0201, 0x0202}


def writable_tags() -> list[str]:
    return sorted(_NAME_TO_TAG[n][2] for n in _NAME_TO_TAG
                  if _NAME_TO_TAG[n][:2] in _WRITABLE)


# --------------------------------------------------------------------------- #
# EXIF (TIFF) read / modify / write
# --------------------------------------------------------------------------- #
class Exif:
    """In-memory EXIF block: {section: {tag: (type, count, raw bytes)}}."""

    def __init__(self, end: str = "<"):
        self.end = end
        self.sections = {"IFD0": {}, "Exif": {}, "GPS": {}}

    @classmethod
    def parse(cls, data: bytes) -> "Exif":
        if len(data) < 8 or data[:2] not in (b"II", b"MM"):
            raise EditError("invalid EXIF header")
        exif = cls("<" if data[:2] == b"II" else ">")
        end = exif.end
        pointers: dict = {}

        def read(offset: int, section: str, pointer_tags: tuple) -> None:
            try:
                (count,) = struct.unpack_from(end + "H", data, offset)
            except struct.error:
                return
            for i in range(min(count, 512)):
                pos = offset + 2 + i * 12
                try:
                    tag, typ, cnt = struct.unpack_from(end + "HHI", data, pos)
                    if typ not in TYPES:
                        continue
                    size = TYPES[typ][1] * cnt
                    if size <= 4:
                        vpos = pos + 8
                    else:
                        (vpos,) = struct.unpack_from(end + "I", data, pos + 8)
                except struct.error:
                    break
                if vpos + size > len(data):
                    continue
                raw = data[vpos:vpos + size]
                if tag in pointer_tags:
                    pointers[tag] = struct.unpack(end + "I", raw[:4])[0]
                elif tag not in _DROPPED_TAGS:
                    exif.sections[section][tag] = (typ, cnt, raw)

        (ifd0,) = struct.unpack_from(end + "I", data, 4)
        read(ifd0, "IFD0", (0x8769, 0x8825))
        if 0x8769 in pointers:
            read(pointers[0x8769], "Exif", ())
        if 0x8825 in pointers:
            read(pointers[0x8825], "GPS", ())
        return exif

    def is_empty(self) -> bool:
        return not any(self.sections.values())

    def set_text(self, section: str, tag: int, value: str) -> None:
        if (section, tag) == _USER_COMMENT:
            try:
                raw = b"ASCII\x00\x00\x00" + value.encode("ascii")
            except UnicodeEncodeError:
                raise EditError("UserComment: only ASCII text is supported")
            self.sections[section][tag] = (7, len(raw), raw)
        else:
            raw = value.encode("utf-8") + b"\x00"
            self.sections[section][tag] = (2, len(raw), raw)

    def serialize(self) -> bytes:
        end = self.end
        ifd0 = dict(self.sections["IFD0"])
        exif, gps = self.sections["Exif"], self.sections["GPS"]

        def ifd_size(n: int) -> int:
            return 2 + 12 * n + 4

        n0 = len(ifd0) + bool(exif) + bool(gps)
        off_exif = 8 + ifd_size(n0)
        off_gps = off_exif + (ifd_size(len(exif)) if exif else 0)
        off_data = off_gps + (ifd_size(len(gps)) if gps else 0)
        area = bytearray()

        def build(entries: list) -> bytes:
            out = bytearray(struct.pack(end + "H", len(entries)))
            for tag, typ, cnt, raw in sorted(entries, key=lambda e: e[0]):
                if len(raw) <= 4:
                    value = raw.ljust(4, b"\x00")
                else:
                    value = struct.pack(end + "I", off_data + len(area))
                    area.extend(raw)
                    if len(area) % 2:
                        area.append(0)
                out += struct.pack(end + "HHI", tag, typ, cnt) + value
            out += b"\x00\x00\x00\x00"
            return bytes(out)

        def entries(section: dict) -> list:
            return [(t, ty, c, r) for t, (ty, c, r) in section.items()]

        e0 = entries(ifd0)
        if exif:
            e0.append((0x8769, 4, 1, struct.pack(end + "I", off_exif)))
        if gps:
            e0.append((0x8825, 4, 1, struct.pack(end + "I", off_gps)))
        body = build(e0)
        if exif:
            body += build(entries(exif))
        if gps:
            body += build(entries(gps))
        header = (b"II" if end == "<" else b"MM") + struct.pack(end + "HI", 42, 8)
        return header + body + bytes(area)


def _validate(canon: str, tag: int, value: str) -> None:
    if tag in _DATE_TAGS:
        try:
            datetime.datetime.strptime(value, _DATE_FORMAT)
        except ValueError:
            raise EditError(f"{canon}: value must look like "
                            f"'YYYY:MM:DD HH:MM:SS' (got {value!r})") from None


def _apply_exif_ops(exif: Exif, sets: dict, deletes: list, report: list) -> set:
    """Apply deletes then sets to `exif`. Returns the (lowercase) names acted on."""
    done: set = set()
    for name in deletes:
        key = name.lower()
        if key == "gps":
            if exif.sections["GPS"]:
                exif.sections["GPS"] = {}
                report.append("deleted GPS section")
                done.add(key)
            continue
        info = _NAME_TO_TAG.get(key)
        if info and info[1] in exif.sections[info[0]]:
            del exif.sections[info[0]][info[1]]
            report.append(f"deleted EXIF {info[2]}")
            done.add(key)
    for name, value in sets.items():
        info = _NAME_TO_TAG.get(name.lower())
        if not info:
            continue
        section, tag, canon = info
        if (section, tag) not in _WRITABLE:
            raise EditError(f"{canon}: only text tags can be modified "
                            f"({', '.join(writable_tags())})")
        _validate(canon, tag, value)
        exif.set_text(section, tag, value)
        report.append(f"set EXIF {canon} = {value!r}")
        done.add(name.lower())
    return done


def _finish(done: set, sets: dict, deletes: list,
            report: list, fmt: str, hint: str) -> None:
    """Report deletes that matched nothing; refuse sets nobody could apply."""
    for name in deletes:
        if name.lower() not in done:
            report.append(f"{name}: not found, nothing to delete")
    for name in sets:
        if name.lower() not in done:
            raise EditError(f"cannot set {name!r} in a {fmt} file ({hint})")


# --------------------------------------------------------------------------- #
# JPEG
# --------------------------------------------------------------------------- #
_EXIF_ID = b"Exif\x00\x00"
_XMP_ID = b"http://ns.adobe.com/xap/1.0/"


def _jpeg_split(data: bytes) -> tuple[list, bytes]:
    """Return ([(marker, body, raw)...], tail from SOS/EOI on)."""
    segments = []
    pos = 2
    while True:
        if pos + 2 > len(data):
            raise EditError("truncated JPEG")
        if data[pos] != 0xFF:
            raise EditError("corrupted JPEG (bad marker)")
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xDA, 0xD9):
            return segments, data[pos:]
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            segments.append((marker, b"", data[pos:pos + 2]))
            pos += 2
            continue
        if pos + 4 > len(data):
            raise EditError("truncated JPEG")
        (length,) = struct.unpack_from(">H", data, pos + 2)
        if length < 2 or pos + 2 + length > len(data):
            raise EditError("truncated JPEG segment")
        segments.append((marker, data[pos + 4:pos + 2 + length],
                         data[pos:pos + 2 + length]))
        pos += 2 + length


def _jpeg_segment(marker: int, body: bytes) -> tuple:
    if len(body) + 2 > 0xFFFF:
        raise EditError("metadata too large for a JPEG segment")
    raw = bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body
    return marker, body, raw


def _jpeg_keep_on_strip(marker: int, body: bytes) -> bool:
    """APPn segments needed to display the image correctly are kept."""
    if marker == 0xE0:
        return body.startswith(b"JFIF\x00")
    if marker == 0xE2:
        return body.startswith(b"ICC_PROFILE\x00")
    return marker == 0xEE  # Adobe colour transform


def _jpeg_label(marker: int, body: bytes) -> str:
    if marker == 0xFE:
        return "Comment"
    if marker == 0xE1 and body.startswith(_EXIF_ID):
        return "EXIF"
    if marker == 0xE1 and body.startswith(_XMP_ID):
        return "XMP"
    if marker == 0xED:
        return "IPTC/Photoshop"
    return f"APP{marker - 0xE0}"


def edit_jpeg(data: bytes, sets: dict, deletes: list,
              strip_all: bool) -> tuple[bytes, list]:
    segments, tail = _jpeg_split(data)
    report: list = []
    lower_sets = {k.lower() for k in sets}
    lower_dels = {d.lower() for d in deletes}
    kept: list = []
    exif = None
    done: set = set()

    if strip_all:
        removed = []
        for seg in segments:
            marker, body, _ = seg
            droppable = marker == 0xFE or 0xE0 <= marker <= 0xEF
            if droppable and not _jpeg_keep_on_strip(marker, body):
                removed.append(_jpeg_label(marker, body))
            else:
                kept.append(seg)
        report.append("stripped: " + (", ".join(removed) if removed
                                      else "nothing to remove"))
    else:
        for seg in segments:
            marker, body, _ = seg
            label = _jpeg_label(marker, body)
            if label == "EXIF" and exif is None:
                exif = Exif.parse(body[6:])
                continue
            if label == "XMP" and "xmp" in lower_dels:
                report.append("deleted XMP packet")
                done.add("xmp")
                continue
            if label == "IPTC/Photoshop" and "iptc" in lower_dels:
                report.append("deleted IPTC/Photoshop block")
                done.add("iptc")
                continue
            if label == "Comment" and ("comment" in lower_dels
                                       or "comment" in lower_sets):
                if "comment" in lower_dels:
                    report.append("deleted Comment")
                    done.add("comment")
                continue
            kept.append(seg)

        exif = exif or Exif("<")
        done |= _apply_exif_ops(exif, sets, deletes, report)
        extra = []
        if not exif.is_empty():
            extra.append(_jpeg_segment(0xE1, _EXIF_ID + exif.serialize()))
        for name, value in sets.items():
            if name.lower() == "comment":
                extra.append(_jpeg_segment(0xFE, value.encode("utf-8")))
                report.append(f"set Comment = {value!r}")
                done.add("comment")
        at = 1 if kept and kept[0][0] == 0xE0 and kept[0][1].startswith(b"JFIF") else 0
        kept[at:at] = extra
        _finish(done, sets, deletes, report, "JPEG",
                "EXIF text tags, Comment")

    return b"\xff\xd8" + b"".join(s[2] for s in kept) + tail, report


# --------------------------------------------------------------------------- #
# PNG
# --------------------------------------------------------------------------- #
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_TEXT = (b"tEXt", b"zTXt", b"iTXt")


def _png_chunk(ctype: bytes, body: bytes) -> tuple:
    raw = (struct.pack(">I", len(body)) + ctype + body
           + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))
    return ctype, body, raw


def _png_split(data: bytes) -> tuple[list, bytes]:
    chunks = []
    pos = 8
    while pos + 12 <= len(data):
        length, ctype = struct.unpack_from(">I4s", data, pos)
        if pos + 12 + length > len(data):
            raise EditError("truncated PNG chunk")
        chunks.append((ctype, data[pos + 8:pos + 8 + length],
                       data[pos:pos + 12 + length]))
        pos += 12 + length
        if ctype == b"IEND":
            break
    if not any(c[0] == b"IDAT" for c in chunks):
        raise EditError("corrupted PNG (no image data)")
    return chunks, data[pos:]


def _png_key(body: bytes) -> str:
    return body.split(b"\x00", 1)[0].decode("latin-1")


def _png_text_chunk(key: str, value: str) -> tuple:
    try:
        raw_key = key.encode("latin-1")
    except UnicodeEncodeError:
        raise EditError(f"PNG text key {key!r} must be Latin-1") from None
    if not 1 <= len(raw_key) <= 79 or b"\x00" in raw_key:
        raise EditError("PNG text key must be 1-79 characters, without NUL")
    try:
        return _png_chunk(b"tEXt", raw_key + b"\x00" + value.encode("latin-1"))
    except UnicodeEncodeError:  # needs UTF-8: use iTXt
        body = raw_key + b"\x00\x00\x00\x00\x00" + value.encode("utf-8")
        return _png_chunk(b"iTXt", body)


def edit_png(data: bytes, sets: dict, deletes: list,
             strip_all: bool) -> tuple[bytes, list]:
    chunks, tail = _png_split(data)
    report: list = []
    kept: list = []
    inserts: list = []

    if strip_all:
        dropped = (*_PNG_TEXT, b"eXIf", b"tIME")
        removed = [c[0].decode() for c in chunks if c[0] in dropped]
        kept = [c for c in chunks if c[0] not in dropped]
        report.append("stripped: " + (", ".join(removed) if removed
                                      else "nothing to remove"))
    else:
        lower_dels = {d.lower() for d in deletes}
        existing = {_png_key(c[1]).lower() for c in chunks if c[0] in _PNG_TEXT}
        # Existing text chunk wins; otherwise a known EXIF name goes to EXIF.
        text_sets = {n: v for n, v in sets.items()
                     if n.lower() in existing or n.lower() not in _NAME_TO_TAG}
        exif_sets = {n: v for n, v in sets.items() if n not in text_sets}
        replaced = {n.lower() for n in text_sets}
        exif = None
        done: set = set()
        for chunk in chunks:
            ctype, body, _ = chunk
            if ctype == b"eXIf" and exif is None:
                exif = Exif.parse(body)
                continue
            if ctype in _PNG_TEXT:
                key = _png_key(body).lower()
                if key in lower_dels:
                    report.append(f"deleted text chunk {_png_key(body)!r}")
                    done.add(key)
                    continue
                if key in replaced:
                    continue
            if ctype == b"tIME" and "time" in lower_dels:
                report.append("deleted tIME chunk")
                done.add("time")
                continue
            kept.append(chunk)

        for name, value in text_sets.items():
            inserts.append(_png_text_chunk(name, value))
            report.append(f"set text {name} = {value!r}")
            done.add(name.lower())
        exif = exif or Exif("<")
        done |= _apply_exif_ops(exif, exif_sets, deletes, report)
        if not exif.is_empty():
            inserts.append(_png_chunk(b"eXIf", exif.serialize()))
        _finish(done, sets, deletes, report, "PNG", "any text key")

    at = next(i for i, c in enumerate(kept) if c[0] == b"IDAT")
    kept[at:at] = inserts
    return _PNG_SIG + b"".join(c[2] for c in kept) + tail, report


# --------------------------------------------------------------------------- #
# GIF
# --------------------------------------------------------------------------- #
_GIF_KEEP_APPS = (b"NETSCAPE2.0", b"ANIMEXTS1.0")


def _gif_sub_blocks(data: bytes, pos: int) -> tuple[int, bytes]:
    payload = b""
    while True:
        if pos >= len(data):
            raise EditError("truncated GIF")
        size = data[pos]
        payload += data[pos + 1:pos + 1 + size]
        pos += 1 + size
        if size == 0:
            return pos, payload


def _gif_split(data: bytes) -> tuple[bytes, list, bytes]:
    """Return (header + global table, [(kind, label, payload, raw)...], tail)."""
    if len(data) < 13:
        raise EditError("truncated GIF header")
    flags = data[10]
    pos = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)
    header, blocks = data[:pos], []
    while pos < len(data):
        start, block = pos, data[pos]
        if block == 0x21:
            if pos + 2 > len(data):
                raise EditError("truncated GIF")
            label = data[pos + 1]
            pos, payload = _gif_sub_blocks(data, pos + 2)
            blocks.append(("ext", label, payload, data[start:pos]))
        elif block == 0x2C:
            if pos + 11 > len(data):
                raise EditError("truncated GIF")
            lflags = data[pos + 9]
            pos += 10 + (3 * (2 << (lflags & 7)) if lflags & 0x80 else 0) + 1
            pos, _ = _gif_sub_blocks(data, pos)
            blocks.append(("image", 0, b"", data[start:pos]))
        elif block == 0x3B:
            return header, blocks, data[pos:]
        else:
            raise EditError("corrupted GIF (unknown block)")
    return header, blocks, b""


def _gif_comment(text: str) -> bytes:
    raw = text.encode("utf-8")
    out = bytearray(b"\x21\xfe")
    for i in range(0, len(raw), 255):
        part = raw[i:i + 255]
        out += bytes([len(part)]) + part
    return bytes(out) + b"\x00"


def edit_gif(data: bytes, sets: dict, deletes: list,
             strip_all: bool) -> tuple[bytes, list]:
    header, blocks, tail = _gif_split(data)
    report: list = []
    kept: list = []
    prefix = b""

    if strip_all:
        removed = []
        for block in blocks:
            kind, label, payload, _ = block
            drop = kind == "ext" and (
                label in (0xFE, 0x01)
                or (label == 0xFF and payload[:11] not in _GIF_KEEP_APPS))
            if drop:
                removed.append({0xFE: "Comment", 0x01: "PlainText"}.get(
                    label, "Application extension"))
            else:
                kept.append(block)
        report.append("stripped: " + (", ".join(removed) if removed
                                      else "nothing to remove"))
    else:
        lower_sets = {k.lower(): v for k, v in sets.items()}
        done: set = set()
        for block in blocks:
            kind, label, _, _ = block
            if kind == "ext" and label == 0xFE:
                if "comment" in {d.lower() for d in deletes}:
                    report.append("deleted Comment")
                    done.add("comment")
                    continue
                if "comment" in lower_sets:
                    continue
            kept.append(block)
        if "comment" in lower_sets:
            prefix = _gif_comment(lower_sets["comment"])
            report.append(f"set Comment = {lower_sets['comment']!r}")
            done.add("comment")
        _finish(done, sets, deletes, report, "GIF", "only Comment")

    return header + prefix + b"".join(b[3] for b in kept) + tail, report


# --------------------------------------------------------------------------- #
# Entry point used by scorpion.py
# --------------------------------------------------------------------------- #
def _detect(data: bytes):
    if data.startswith(b"\xff\xd8"):
        return "JPEG", edit_jpeg
    if data.startswith(_PNG_SIG):
        return "PNG", edit_png
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF", edit_gif
    if data.startswith(b"BM"):
        return "BMP", None
    return None, None


def edit_file(path: str, sets: dict | None = None, deletes: list | None = None,
              strip_all: bool = False, backup: bool = False) -> list[str]:
    """Edit the metadata of `path` in place and return a list of messages."""
    sets, deletes = sets or {}, deletes or []
    if not os.path.isfile(path):
        raise EditError("not a regular file")
    if not path.lower().endswith(EXTENSIONS):
        raise EditError("unsupported extension (jpg, jpeg, png, gif, bmp)")
    with open(path, "rb") as handle:
        data = handle.read()
    fmt, editor = _detect(data)
    if fmt is None:
        raise EditError("content is not a valid jpeg/png/gif/bmp image")
    if editor is None:
        raise EditError("BMP files carry no editable metadata")
    try:
        new_data, report = editor(data, sets, deletes, strip_all)
    except (struct.error, IndexError) as error:
        raise EditError(f"corrupted image ({error})") from error

    if new_data == data:
        return report + ["no change needed"]
    if backup:
        shutil.copy2(path, path + ".bak")
        report.append(f"backup saved to {path}.bak")
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".scorpion-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(new_data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return report
