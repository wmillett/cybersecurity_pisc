"""Hand-built minimal images, independent of scorpion.py / bonus.py."""

import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ----------------------------------------------------------------- TIFF/EXIF
def build_tiff(end="<", make=b"Canon", model=b"EOS 5D",
               date=b"2020:01:02 03:04:05", gps=True) -> bytes:
    """IFD0 (Make, Model, ExifPtr, GPSPtr) + Exif (DateTimeOriginal) + GPS."""
    p = lambda fmt, *a: struct.pack(end + fmt, *a)
    asc = lambda b: b + b"\x00"
    ifd0_n = 3 + (1 if gps else 0)
    ifd0_off = 8
    ifd0_len = 2 + 12 * ifd0_n + 4
    exif_off = ifd0_off + ifd0_len
    exif_len = 2 + 12 + 4
    gps_off = exif_off + exif_len
    gps_n = 4
    gps_len = 2 + 12 * gps_n + 4
    data_off = gps_off + (gps_len if gps else 0)

    area = b""

    def out_of_line(raw):
        nonlocal area
        off = data_off + len(area)
        area += raw + (b"\x00" if len(raw) % 2 else b"")
        return off

    make_raw, model_raw, date_raw = asc(make), asc(model), asc(date)
    ifd0 = [
        (0x010F, 2, len(make_raw), out_of_line(make_raw)),
        (0x0110, 2, len(model_raw), out_of_line(model_raw)),
        (0x8769, 4, 1, exif_off),
    ]
    if gps:
        ifd0.append((0x8825, 4, 1, gps_off))
    exif = [(0x9003, 2, len(date_raw), out_of_line(date_raw))]

    def ifd(entries):
        b = p("H", len(entries))
        for tag, typ, cnt, val in entries:
            b += p("HHI", tag, typ, cnt) + p("I", val)
        return b + p("I", 0)

    body = ifd(ifd0) + ifd(exif)
    if gps:
        lat = out_of_line(p("IIIIII", 48, 1, 51, 1, 30, 1))
        lon = out_of_line(p("IIIIII", 2, 1, 21, 1, 0, 1))
        # ref values are inline ASCII "N\0" / "E\0"
        g = p("H", 4)
        g += p("HHI", 1, 2, 2) + b"N\x00\x00\x00"
        g += p("HHI", 2, 5, 3) + p("I", lat)
        g += p("HHI", 3, 2, 2) + b"E\x00\x00\x00"
        g += p("HHI", 4, 5, 3) + p("I", lon)
        body += g + p("I", 0)
    return (b"II" if end == "<" else b"MM") + p("HI", 42, 8) + body + area


# ---------------------------------------------------------------------- JPEG
def jpeg_segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


JFIF = jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
SCAN = (b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"
        + b"\x12\x34\x56\xff\x00\x78" + b"\xff\xd9")


def make_jpeg(exif: bytes | None = None, comment: bytes | None = None,
              xmp: bool = False, width=40, height=30) -> bytes:
    out = b"\xff\xd8" + JFIF
    if exif is not None:
        out += jpeg_segment(0xE1, b"Exif\x00\x00" + exif)
    if xmp:
        out += jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta/>")
    if comment is not None:
        out += jpeg_segment(0xFE, comment)
    sof = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    out += jpeg_segment(0xC0, sof)
    return out + SCAN


# ----------------------------------------------------------------------- PNG
def png_chunk(ctype: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + ctype + body
            + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))


def make_png(text: dict | None = None, exif: bytes | None = None,
             time: bool = False, width=4, height=3) -> bytes:
    out = b"\x89PNG\r\n\x1a\n"
    out += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    for k, v in (text or {}).items():
        out += png_chunk(b"tEXt", k.encode() + b"\x00" + v.encode("latin-1"))
    if time:
        out += png_chunk(b"tIME", struct.pack(">HBBBBB", 2021, 5, 6, 7, 8, 9))
    if exif is not None:
        out += png_chunk(b"eXIf", exif)
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    out += png_chunk(b"IDAT", zlib.compress(raw))
    return out + png_chunk(b"IEND", b"")


def png_chunks(data: bytes) -> list:
    """[(type, body)] and verifies every CRC."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    pos, out = 8, []
    while pos < len(data):
        length, ctype = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack_from(">I", data, pos + 8 + length)
        assert crc == zlib.crc32(ctype + body) & 0xFFFFFFFF, f"bad CRC {ctype}"
        out.append((ctype, body))
        pos += 12 + length
    return out


# ----------------------------------------------------------------------- GIF
def make_gif(comment: bytes | None = None, loop: bool = False) -> bytes:
    out = b"GIF89a" + struct.pack("<HHBBB", 3, 2, 0x80, 0, 0)
    out += b"\x00\x00\x00\xff\xff\xff"  # 2-colour global table
    if loop:
        out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    if comment is not None:
        out += b"\x21\xfe" + bytes([len(comment)]) + comment + b"\x00"
    out += b"\x21\xf9\x04\x00\x0a\x00\x00\x00"  # graphic control
    out += b"\x2c" + struct.pack("<HHHHB", 0, 0, 3, 2, 0)
    out += b"\x02\x02\x44\x01\x00"  # LZW data
    return out + b"\x3b"


# ----------------------------------------------------------------------- BMP
def make_bmp(width=2, height=2) -> bytes:
    pixels = b"\x00\x00\xff" * width * height
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                      len(pixels), 2835, 2835, 0, 0)
    size = 14 + len(dib) + len(pixels)
    return b"BM" + struct.pack("<IHHI", size, 0, 0, 54) + dib + pixels
