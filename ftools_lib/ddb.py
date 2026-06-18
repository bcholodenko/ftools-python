"""
.ddb <-> BMP conversion for the IPC's "device-dependent bitmap" image
resources (found inside the QNX imagefs, e.g. images/variant/.../*.ddb).

IMPORTANT - PARTIAL FORMAT SUPPORT:

The .ddb format is undocumented/proprietary; there is no public spec for it.
This module supports ONLY the variant that was fully reverse-engineered and
confirmed against real sample files - empirically derived from
JB5T-14C088-FB.vbf, NOT from any official documentation. That confirmed
variant covers roughly half of the .ddb files found in a typical resource
pack (144 of 305 in our reference sample), including all full-screen
backgrounds, popups, and gradient panels. The rest - mostly small icons,
gauge pointers, and telltales - use one of several other header signatures
that do NOT decode with this formula (likely a different bit depth and/or
a genuine compression scheme); attempting to read one of those raises
DdbError rather than silently producing garbage.

CONFIRMED FORMAT (validated three ways: exact byte-count match across 144
real samples with zero remainder, clean visual rendering with no diagonal
tearing, and a ground-truth color check - the warning-panel background_red
vs background_amber samples decode to identical R/B channels with only G
differing, exactly matching real-world red-vs-amber colour theory):

    Offset  Size  Field
    0       2     width   (u16 LE) - visible content width, in pixels
    2       2     height  (u16 LE) - visible content height, in pixels
    4       1     type    - observed values: 0x02, 0x82 (0x82 = 0x02 | 0x80;
                            the meaning of the high bit is not understood,
                            but it doesn't affect decoding - preserved
                            verbatim on round-trip)
    5       1     (always observed as 0x80 - meaning unknown, preserved
                            verbatim)
    6       2     format  (u16 LE) - must be 4 to use this decoder
    8       -     pixel data: raw RGB565 (5 bits R / 6 bits G / 5 bits B),
                            2 bytes per pixel, little-endian, NO palette.
                            Row stride is next_pow2(width) pixels (i.e. each
                            row is padded out to the next power of two,
                            presumably so the renderer can address rows with
                            a bit-shift instead of a multiply). The real
                            number of stored rows is simply
                            (file_size - 8) // (stride_px * 2); this is
                            usually `height + 1` but the exact reason for
                            the extra row isn't understood, so it's always
                            derived from the file size directly rather than
                            assumed. Only the top-left width x height
                            rectangle is the "visible" image - the rest is
                            padding (columns beyond `width`, and any row(s)
                            beyond `height`) and is preserved verbatim on
                            round-trip rather than interpreted.
"""

import struct
from pathlib import Path

from . import bmpio
from .utils import read_file, write_file

DDB_SUPPORTED_FORMAT = 4
DDB_HEADER_SIZE = 8

_HEADER_FMT = "<HHBBH"


class DdbError(RuntimeError):
    pass


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


def _parse_header(data: bytes, context: str = "") -> dict:
    if len(data) < DDB_HEADER_SIZE:
        raise DdbError(f"'{context}': too short to be a .ddb file ({len(data)} bytes).")

    width, height, type_byte, byte5, fmt = struct.unpack_from(_HEADER_FMT, data, 0)

    if width == 0 or height == 0:
        raise DdbError(f"'{context}': implausible .ddb header (width={width}, height={height}).")

    return {
        "width": width,
        "height": height,
        "type_byte": type_byte,
        "byte5": byte5,
        "format": fmt,
    }


def is_supported_ddb(data: bytes) -> bool:
    """Return True if `data` looks like the confirmed, decodable .ddb variant."""
    try:
        hdr = _parse_header(data)
    except DdbError:
        return False
    return hdr["format"] == DDB_SUPPORTED_FORMAT and (hdr["type_byte"] & 0x7F) == 2


def decode_ddb(data: bytes, context: str = "<ddb data>") -> dict:
    """Decode a confirmed-format .ddb buffer.

    Returns {"width", "height", "rgba"} where `rgba` is width*height*4 bytes,
    top-down, RGBA order (alpha always 0xFF - this format has no alpha
    channel), cropped to the visible width x height rectangle.

    Raises DdbError if `data` is not the confirmed, decodable .ddb variant
    (e.g. one of the other header signatures that this module doesn't
    support - see the module docstring).
    """
    hdr = _parse_header(data, context)
    width, height = hdr["width"], hdr["height"]

    if hdr["format"] != DDB_SUPPORTED_FORMAT or (hdr["type_byte"] & 0x7F) != 2:
        raise DdbError(
            f"'{context}': this .ddb uses an unrecognized/unsupported variant "
            f"(type=0x{hdr['type_byte']:02x}, format={hdr['format']}). Only the "
            f"raw-16bpp variant (type byte's low 7 bits = 2, format = 4) is "
            f"currently supported - see ftools_lib/ddb.py for details."
        )

    stride_px = _next_pow2(width)
    stride_bytes = stride_px * 2
    body = data[DDB_HEADER_SIZE:]

    if len(body) % stride_bytes != 0:
        raise DdbError(
            f"'{context}': pixel data length ({len(body)} bytes) is not an exact "
            f"multiple of the expected row stride ({stride_bytes} bytes) - this "
            f"file may be corrupt or not actually this .ddb variant."
        )

    real_rows = len(body) // stride_bytes
    if real_rows < height:
        raise DdbError(
            f"'{context}': declared height ({height}) exceeds the number of rows "
            f"actually present in the file ({real_rows})."
        )

    rgba = bytearray(width * height * 4)
    for row in range(height):
        row_off = row * stride_bytes
        row_pixels = struct.unpack_from(f"<{width}H", body, row_off)
        out_off = row * width * 4
        for col, val in enumerate(row_pixels):
            r = (val >> 11) & 0x1F
            g = (val >> 5) & 0x3F
            b = val & 0x1F
            o = out_off + col * 4
            rgba[o] = (r * 255) // 31
            rgba[o + 1] = (g * 255) // 63
            rgba[o + 2] = (b * 255) // 31
            rgba[o + 3] = 0xFF

    return {"width": width, "height": height, "rgba": bytes(rgba)}


def encode_ddb(original_data: bytes, rgba: bytes, width: int, height: int, context: str = "<ddb data>") -> bytes:
    """Re-encode pixel data back into .ddb format, using `original_data` (a
    real, confirmed-format .ddb file) as a donor for the header bytes and any
    padding region (columns beyond `width`, rows beyond `height`), which are
    preserved verbatim rather than regenerated.

    `width`/`height` must exactly match the original file's declared
    dimensions - this rebuilds the SAME image at the SAME size with new
    pixel content; it does not support resizing.

    `rgba` must be width*height*4 bytes, top-down, RGBA order (alpha is
    ignored - the format has no alpha channel).
    """
    hdr = _parse_header(original_data, context)
    if hdr["format"] != DDB_SUPPORTED_FORMAT or (hdr["type_byte"] & 0x7F) != 2:
        raise DdbError(
            f"'{context}': the original .ddb donor uses an unsupported variant; "
            f"cannot re-encode."
        )
    if width != hdr["width"] or height != hdr["height"]:
        raise DdbError(
            f"'{context}': new image is {width}x{height} but the original .ddb is "
            f"{hdr['width']}x{hdr['height']}. Resizing is not supported - the "
            f"replacement image must be exactly the same dimensions."
        )
    if len(rgba) != width * height * 4:
        raise DdbError(f"'{context}': RGBA buffer size does not match width*height*4.")

    stride_px = _next_pow2(width)
    stride_bytes = stride_px * 2
    body = bytearray(original_data[DDB_HEADER_SIZE:])  # start from donor, preserves padding

    if len(body) % stride_bytes != 0:
        raise DdbError(f"'{context}': original .ddb body length is not a multiple of the row stride.")

    for row in range(height):
        row_off = row * stride_bytes
        in_off = row * width * 4
        packed = []
        for col in range(width):
            o = in_off + col * 4
            r8, g8, b8 = rgba[o], rgba[o + 1], rgba[o + 2]
            r5 = (r8 * 31 + 127) // 255
            g6 = (g8 * 63 + 127) // 255
            b5 = (b8 * 31 + 127) // 255
            packed.append((r5 << 11) | (g6 << 5) | b5)
        struct.pack_into(f"<{width}H", body, row_off, *packed)

    return bytes(original_data[:DDB_HEADER_SIZE]) + bytes(body)


def ddb_to_bmp_file(ddb_data: bytes, out_path, context: str = "") -> None:
    decoded = decode_ddb(ddb_data, context=context or str(out_path))
    bmpio.write_bmp(out_path, decoded["width"], decoded["height"], decoded["rgba"], bit_depth=32)


def bmp_file_to_ddb_file(bmp_path, original_ddb_path, out_path) -> None:
    bmp = bmpio.read_bmp(bmp_path)
    original_data = read_file(original_ddb_path)
    new_ddb = encode_ddb(
        original_data, bmp["rgba"], bmp["width"], bmp["height"], context=str(original_ddb_path)
    )
    write_file(out_path, new_ddb)
