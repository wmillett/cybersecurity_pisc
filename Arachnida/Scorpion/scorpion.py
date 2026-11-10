#!/usr/bin/env python3
"""scorpion - receives images as input and must parse them for EXIF and other metadata, displaying them on the screen.

Usage: ./scorpion FILE1 [FILE2 ...]

File must be a valid image file (jpeg/jpg, png, gif, bmp).
"""

import argparse
import datetime
import os
import struct
import sys
import zlib

EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp")

# --------------------------------------------------------------------------- #
# EXIF tag tables (subset of the most useful tags)
# --------------------------------------------------------------------------- #
IFD0_TAGS = {
    0x010E: "ImageDescription", 0x010F: "Make", 0x0110: "Model",
    0x0112: "Orientation", 0x011A: "XResolution", 0x011B: "YResolution",
    0x0128: "ResolutionUnit", 0x0131: "Software", 0x0132: "DateTime",
    0x013B: "Artist", 0x013E: "WhitePoint", 0x0213: "YCbCrPositioning",
    0x8298: "Copyright",
}
EXIF_TAGS = {
    0x829A: "ExposureTime", 0x829D: "FNumber", 0x8822: "ExposureProgram",
    0x8827: "ISO", 0x9000: "ExifVersion", 0x9003: "DateTimeOriginal",
    0x9004: "DateTimeDigitized", 0x9101: "ComponentsConfiguration",
    0x9201: "ShutterSpeedValue", 0x9202: "ApertureValue",
    0x9204: "ExposureBiasValue", 0x9207: "MeteringMode", 0x9209: "Flash",
    0x920A: "FocalLength", 0x9286: "UserComment", 0xA000: "FlashpixVersion",
    0xA001: "ColorSpace", 0xA002: "PixelXDimension",
    0xA003: "PixelYDimension", 0xA401: "CustomRendered",
    0xA402: "ExposureMode", 0xA403: "WhiteBalance",
    0xA405: "FocalLengthIn35mmFilm", 0xA406: "SceneCaptureType",
    0xA420: "ImageUniqueID", 0xA430: "CameraOwnerName",
    0xA431: "BodySerialNumber", 0xA433: "LensMake", 0xA434: "LensModel",
}
GPS_TAGS = {
    0x0000: "GPSVersionID", 0x0001: "GPSLatitudeRef", 0x0002: "GPSLatitude",
    0x0003: "GPSLongitudeRef", 0x0004: "GPSLongitude",
    0x0005: "GPSAltitudeRef", 0x0006: "GPSAltitude", 0x0007: "GPSTimeStamp",
    0x001D: "GPSDateStamp",
}
# type id -> (struct code, size)
TYPES = {1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8),
         6: ("b", 1), 7: ("B", 1), 8: ("h", 2), 9: ("i", 4), 10: ("ii", 8),
         11: ("f", 4), 12: ("d", 8)}


class MetadataError(Exception):
    """Raised when a file is not a valid / supported image."""


# --------------------------------------------------------------------------- #
# EXIF / TIFF parsing
# --------------------------------------------------------------------------- #
def _format_value(typ: int, values: list, name: str) -> str:
    if typ == 2:
        return values[0].rstrip(b"\x00").decode("utf-8", "replace").strip()
    if typ == 7:
        raw = bytes(values)
        if name == "UserComment":
            raw = raw[8:]
        if raw and all(32 <= b < 127 or b == 0 for b in raw):
            return raw.rstrip(b"\x00").decode("ascii")
        if len(raw) <= 8:
            return " ".join(f"{b:02x}" for b in raw)
        return f"<{len(raw)} bytes>"
    if typ in (5, 10):
        out = []
        for i in range(0, len(values), 2):
            num, den = values[i], values[i + 1]
            out.append("0" if den == 0 else
                       (str(num) if den == 1 else f"{num}/{den} ({num / den:.4g})"))
        return ", ".join(out)
    if len(values) > 16:
        return f"<{len(values)} values>"
    return ", ".join(str(v) for v in values)


def _read_ifd(data: bytes, offset: int, end: str, tags: dict,
              pointers: dict, raw: dict) -> dict:
    result = {}
    try:
        (count,) = struct.unpack_from(end + "H", data, offset)
    except struct.error:
        return result
    for i in range(min(count, 512)):
        pos = offset + 2 + i * 12
        try:
            tag, typ, cnt = struct.unpack_from(end + "HHI", data, pos)
        except struct.error:
            break
        if typ not in TYPES:
            continue
        code, unit = TYPES[typ]
        size = unit * cnt
        if size > len(data):
            continue
        if size <= 4:
            vpos = pos + 8
        else:
            (vpos,) = struct.unpack_from(end + "I", data, pos + 8)
        if vpos + size > len(data):
            continue
        if typ == 2:
            values = [data[vpos:vpos + cnt]]
        else:
            values = list(struct.unpack_from(end + code * cnt, data, vpos))
        if tag in pointers:
            pointers[tag] = values[0]
            continue
        raw[(id(tags), tag)] = (typ, values)
        name = tags.get(tag)
        if name:
            result[name] = _format_value(typ, values, name)
    return result


def _gps_to_decimal(raw: dict, tags: dict) -> str | None:
    def rat(tag):
        item = raw.get((id(tags), tag))
        return item[1] if item else None

    def coord(tag, ref_tag):
        vals, ref = rat(tag), raw.get((id(tags), ref_tag))
        if not vals or len(vals) < 6 or not ref:
            return None
        parts = [vals[i] / vals[i + 1] if vals[i + 1] else 0.0 for i in (0, 2, 4)]
        dec = parts[0] + parts[1] / 60 + parts[2] / 3600
        if ref[1][0][:1] in (b"S", b"W"):
            dec = -dec
        return dec

    lat, lon = coord(0x0002, 0x0001), coord(0x0004, 0x0003)
    if lat is None or lon is None:
        return None
    return f"{lat:.6f}, {lon:.6f}"


def parse_tiff(data: bytes) -> dict:
    """Parse a TIFF block (as found in EXIF) and return {section: {tag: value}}."""
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return {}
    end = "<" if data[:2] == b"II" else ">"
    (ifd0,) = struct.unpack_from(end + "I", data, 4)
    ptrs = {0x8769: None, 0x8825: None}
    raw: dict = {}
    sections = {}
    sections["IFD0"] = _read_ifd(data, ifd0, end, IFD0_TAGS, ptrs, raw)
    if ptrs[0x8769]:
        sections["Exif"] = _read_ifd(data, ptrs[0x8769], end, EXIF_TAGS,
                                     {0xA005: None}, raw)
    if ptrs[0x8825]:
        gps = _read_ifd(data, ptrs[0x8825], end, GPS_TAGS, {}, raw)
        decimal = _gps_to_decimal(raw, GPS_TAGS)
        if decimal:
            gps["GPSPosition (decimal)"] = decimal
        sections["GPS"] = gps
    return {k: v for k, v in sections.items() if v}


# --------------------------------------------------------------------------- #
# Format-specific readers: each returns (info, sections)
# --------------------------------------------------------------------------- #
def read_jpeg(data: bytes) -> tuple[dict, dict]:
    info: dict = {"Format": "JPEG"}
    sections: dict = {}
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker in (0xD9, 0xDA):  # EOI / SOS: image data follows
            break
        (length,) = struct.unpack_from(">H", data, pos + 2)
        seg = data[pos + 4:pos + 2 + length]
        if marker == 0xE1 and seg.startswith(b"Exif\x00\x00"):
            sections.update(parse_tiff(seg[6:]))
        elif marker == 0xE1 and seg.startswith(b"http://ns.adobe.com/xap/1.0/"):
            sections.setdefault("XMP", {})["Present"] = f"yes ({len(seg)} bytes)"
        elif marker == 0xE0 and seg.startswith(b"JFIF\x00") and len(seg) >= 9:
            info["JFIF version"] = f"{seg[5]}.{seg[6]:02d}"
        elif marker == 0xFE:
            info["Comment"] = seg.decode("utf-8", "replace").strip()
        elif (0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC)
              and len(seg) >= 6):
            precision, h, w, comps = struct.unpack_from(">BHHB", seg, 0)
            info["Dimensions"] = f"{w} x {h}"
            info["Bits per sample"] = str(precision)
            info["Components"] = str(comps)
        pos += 2 + length
    return info, sections


def read_png(data: bytes) -> tuple[dict, dict]:
    info: dict = {"Format": "PNG"}
    sections: dict = {}
    text: dict = {}
    color_types = {0: "Grayscale", 2: "RGB", 3: "Palette",
                   4: "Grayscale+Alpha", 6: "RGBA"}
    pos = 8
    while pos + 8 <= len(data):
        length, ctype = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR" and len(body) >= 13:
            w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body[:13])
            info["Dimensions"] = f"{w} x {h}"
            info["Bit depth"] = str(depth)
            info["Color type"] = color_types.get(color, str(color))
            info["Interlaced"] = "yes" if interlace else "no"
        elif ctype == b"tEXt" and b"\x00" in body:
            key, _, val = body.partition(b"\x00")
            text[key.decode("latin-1")] = val.decode("latin-1")
        elif ctype == b"zTXt" and b"\x00" in body:
            key, _, rest = body.partition(b"\x00")
            try:
                text[key.decode("latin-1")] = zlib.decompress(rest[1:]).decode(
                    "utf-8", "replace")
            except zlib.error:
                pass
        elif ctype == b"iTXt" and b"\x00" in body:
            key, _, rest = body.partition(b"\x00")
            if len(rest) >= 2:
                comp, rest = rest[0], rest[2:]
                parts = rest.split(b"\x00", 2)
                if len(parts) == 3:
                    val = parts[2]
                    try:
                        if comp:
                            val = zlib.decompress(val)
                        text[key.decode("latin-1")] = val.decode("utf-8", "replace")
                    except zlib.error:
                        pass
        elif ctype == b"tIME" and len(body) == 7:
            y, mo, d, h, mi, s = struct.unpack(">HBBBBB", body)
            info["Last modification (tIME)"] = f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"
        elif ctype == b"pHYs" and len(body) == 9:
            x, y, unit = struct.unpack(">IIB", body)
            info["Pixel density"] = f"{x} x {y}" + (" px/m" if unit == 1 else "")
        elif ctype == b"gAMA" and len(body) == 4:
            info["Gamma"] = f"{struct.unpack('>I', body)[0] / 100000:.5f}"
        elif ctype == b"eXIf":
            sections.update(parse_tiff(body))
        elif ctype == b"IEND":
            break
    if text:
        sections["Text chunks"] = {k: (v if len(v) < 200 else v[:200] + "...")
                                   for k, v in text.items()}
    return info, sections


def _skip_sub_blocks(data: bytes, pos: int) -> tuple[int, bytes]:
    chunks = b""
    while pos < len(data) and data[pos]:
        size = data[pos]
        chunks += data[pos + 1:pos + 1 + size]
        pos += 1 + size
    return pos + 1, chunks


def read_gif(data: bytes) -> tuple[dict, dict]:
    info: dict = {"Format": "GIF", "Version": data[3:6].decode("ascii", "replace")}
    sections: dict = {}
    if len(data) < 13:
        raise MetadataError("truncated GIF header")
    w, h, flags, bg, aspect = struct.unpack_from("<HHBBB", data, 6)
    info["Dimensions"] = f"{w} x {h}"
    pos = 13
    if flags & 0x80:
        pos += 3 * (2 << (flags & 7))
    frames, comments, loops = 0, [], None
    while pos < len(data):
        block = data[pos]
        if block == 0x21 and pos + 1 < len(data):
            label = data[pos + 1]
            pos, payload = _skip_sub_blocks(data, pos + 2)
            if label == 0xFE:
                comments.append(payload.decode("utf-8", "replace"))
            elif label == 0xFF and payload.startswith(b"NETSCAPE") and len(payload) >= 14:
                loops = struct.unpack("<H", payload[12:14])[0]
        elif block == 0x2C:
            frames += 1
            if pos + 10 > len(data):
                break
            lflags = data[pos + 9]
            pos += 10
            if lflags & 0x80:
                pos += 3 * (2 << (lflags & 7))
            pos, _ = _skip_sub_blocks(data, pos + 1)
        else:
            break
    info["Frames"] = str(frames)
    if loops is not None:
        info["Loop count"] = "infinite" if loops == 0 else str(loops)
    if comments:
        info["Comment"] = " | ".join(comments)
    return info, sections


def read_bmp(data: bytes) -> tuple[dict, dict]:
    info: dict = {"Format": "BMP"}
    if len(data) < 26:
        raise MetadataError("truncated BMP header")
    (dib,) = struct.unpack_from("<I", data, 14)
    if dib == 12:
        w, h, _, bpp = struct.unpack_from("<HHHH", data, 18)
    else:
        w, h, _, bpp = struct.unpack_from("<iiHH", data, 18)
    info["Dimensions"] = f"{w} x {abs(h)}"
    info["Bits per pixel"] = str(bpp)
    info["DIB header size"] = str(dib)
    if dib >= 40 and len(data) >= 46:
        comp, _, xppm, yppm, colors = struct.unpack_from("<IIiiI", data, 30)
        names = {0: "none", 1: "RLE8", 2: "RLE4", 3: "bitfields"}
        info["Compression"] = names.get(comp, str(comp))
        info["Pixel density"] = f"{xppm} x {yppm} px/m"
        info["Palette colors"] = str(colors)
    return info, {}


def detect_reader(data: bytes):
    if data.startswith(b"\xff\xd8"):
        return read_jpeg
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return read_png
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return read_gif
    if data.startswith(b"BM"):
        return read_bmp
    return None


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #
def _ts(value: float) -> str:
    return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def file_attributes(path: str) -> dict:
    st = os.stat(path)
    attrs = {
        "Name": os.path.basename(path),
        "Size": f"{st.st_size} bytes",
        "Permissions": oct(st.st_mode & 0o777),
    }
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        attrs["Creation date"] = _ts(birth)
    else:
        attrs["Creation date"] = f"{_ts(st.st_ctime)} (inode change time)"
    attrs["Last modified"] = _ts(st.st_mtime)
    attrs["Last accessed"] = _ts(st.st_atime)
    return attrs


def print_block(title: str, items: dict) -> None:
    print(f"  [{title}]")
    width = max(len(k) for k in items)
    for key, value in items.items():
        print(f"    {key.ljust(width)} : {value}")


def process_file(path: str) -> None:
    if not os.path.isfile(path):
        raise MetadataError("not a regular file")
    if not path.lower().endswith(EXTENSIONS):
        raise MetadataError("unsupported extension (jpg, jpeg, png, gif, bmp)")
    with open(path, "rb") as handle:
        data = handle.read()
    reader = detect_reader(data)
    if reader is None:
        raise MetadataError("content is not a valid jpeg/png/gif/bmp image")
    try:
        info, sections = reader(data)
    except (struct.error, IndexError) as error:
        raise MetadataError(f"corrupted image ({error})") from error

    print(f"=== {path} ===")
    print_block("File attributes", file_attributes(path))
    print_block("Image", info)
    if sections:
        for name, items in sections.items():
            print_block(f"EXIF - {name}" if name in ("IFD0", "Exif", "GPS") else name,
                        items)
    else:
        print("  [No EXIF / extra metadata found]")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scorpion",
        description="Display EXIF and other metadata of image files "
                    "(jpeg/jpg, png, gif, bmp).")
    parser.add_argument("files", metavar="FILE", nargs="+", help="image file(s)")
    args = parser.parse_args(argv)

    status = 0
    for path in args.files:
        try:
            process_file(path)
        except (OSError, MetadataError) as error:
            print(f"scorpion: {path}: {error}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
