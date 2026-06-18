"""
.ddb <-> BMP conversion for the IPC's "device-dependent bitmap" image
resources (found inside the QNX imagefs, e.g. images/variant/.../*.ddb).

IMPORTANT - PARTIAL FORMAT SUPPORT:

The .ddb format is undocumented/proprietary; there is no public spec for it.
This module supports the variants that were reverse-engineered and confirmed
against real sample files - empirically derived from JB5T-14C088-FB.vbf, NOT
from any official documentation. Together the two confirmed raw/raster
variants below cover roughly 60% of the .ddb files found in a typical
resource pack (184 of 305 in our reference sample): all full-screen
backgrounds, popups, gradient panels, and most icons. The rest (121 of 305)
use one of three other header signatures (format 7, 5, or 2) that show clear
evidence of genuine RLE-style compression (same width/height pairs produce
different file sizes depending on image complexity) and have NOT been fully
decoded yet; attempting to read one of those raises DdbError rather than
silently producing garbage.

CONFIRMED FORMAT 1 - RAW16 (validated three ways: exact byte-count match
across 144 real samples with zero remainder, clean visual rendering with no
diagonal tearing, and a ground-truth color check - the warning-panel
background_red vs background_amber samples decode to identical R/B channels
with only G differing, exactly matching real-world red-vs-amber colour
theory):

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

CONFIRMED FORMAT 2 - RAW24 (type byte = 0x03, format = 6; 40 files in the
reference sample). Same padding scheme as RAW16 (row stride = next_pow2
(width), real row count derived from file size) but 3 bytes per pixel
(direct R,G,B, no palette) instead of 2, and an additional wrinkle: roughly
a third of the files in this group (13 of 40) have an extra fixed 32-byte
block between the 8-byte header and the start of pixel data, whose purpose
isn't understood (it isn't a palette consumed by the pixel data, since the
pixel data is direct colour, not indexed) but which is preserved verbatim on
round-trip. There is no header field that distinguishes the with-prefix from
without-prefix files; this module detects it from the body length (whichever
of extra=0 / extra=32 divides the remaining body evenly into whole rows close
to `height` is used). Validated visually on large backgrounds (popup and
stack background panels, where wide runs of identical row bytes confirmed
deliberate solid-colour fill regions, not corruption) and on a thin gradient
divider bar. NOTE: a subset of small interactive icons in this group
(checkbox/radio-button states, arrow buttons, and on/off toggle icon pairs
such as entertainment/navigation/phone) appear to actually need 2
bytes/pixel (RAW16-style) rather than 3, based on spot-checks, but no
reliable automatic signal to distinguish the two sub-cases was found in the
time available - those specific icons may show visible banding artifacts
under this decoder. This is a known, open limitation.
"""

import struct
from pathlib import Path

from . import bmpio
from .utils import read_file, write_file

DDB_SUPPORTED_FORMAT = 4
DDB_RAW24_FORMAT = 6
DDB_RAW24_EXTRA_CANDIDATES = (0, 32)
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


def _is_raw16(hdr: dict) -> bool:
    return hdr["format"] == DDB_SUPPORTED_FORMAT and (hdr["type_byte"] & 0x7F) == 2


def _is_raw24(hdr: dict) -> bool:
    return hdr["format"] == DDB_RAW24_FORMAT and hdr["type_byte"] == 3


def _raw24_find_extra(body: bytes, width: int, height: int) -> int:
    """Determine the RAW24 prefix size (0 or 32 bytes) for this body, by
    finding whichever candidate divides the remaining bytes evenly into a
    whole number of stride_bytes-sized rows, with that row count close to
    `height`. Raises DdbError if neither candidate fits."""
    stride_bytes = _next_pow2(width) * 3
    for extra in DDB_RAW24_EXTRA_CANDIDATES:
        if len(body) <= extra:
            continue
        remainder_body = body[extra:]
        if stride_bytes and len(remainder_body) % stride_bytes == 0:
            rows = len(remainder_body) // stride_bytes
            if height <= rows <= height + 1:
                return extra
    raise DdbError(
        "RAW24 .ddb body length doesn't cleanly divide into rows for any "
        "known prefix size (0 or 32 bytes) - this file may be corrupt, or "
        "use a variant of this format not yet seen."
    )


def is_supported_ddb(data: bytes) -> bool:
    """Return True if `data` looks like one of the confirmed, decodable .ddb
    variants (RAW16 or RAW24 - see module docstring)."""
    try:
        hdr = _parse_header(data)
    except DdbError:
        return False
    if _is_raw16(hdr):
        return True
    if _is_raw24(hdr):
        try:
            _raw24_find_extra(data[DDB_HEADER_SIZE:], hdr["width"], hdr["height"])
        except DdbError:
            return False
        return True
    return False


def _decode_raw16(data: bytes, hdr: dict, context: str) -> dict:
    width, height = hdr["width"], hdr["height"]
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


def _decode_raw24(data: bytes, hdr: dict, context: str) -> dict:
    width, height = hdr["width"], hdr["height"]
    body = data[DDB_HEADER_SIZE:]
    extra = _raw24_find_extra(body, width, height)
    body = body[extra:]
    stride_bytes = _next_pow2(width) * 3

    rgba = bytearray(width * height * 4)
    for row in range(height):
        row_off = row * stride_bytes
        out_off = row * width * 4
        for col in range(width):
            o = row_off + col * 3
            r, g, b = body[o], body[o + 1], body[o + 2]
            oo = out_off + col * 4
            rgba[oo] = r
            rgba[oo + 1] = g
            rgba[oo + 2] = b
            rgba[oo + 3] = 0xFF

    return {"width": width, "height": height, "rgba": bytes(rgba)}


def decode_ddb(data: bytes, context: str = "<ddb data>") -> dict:
    """Decode a confirmed-format .ddb buffer (RAW16 or RAW24 - see module
    docstring for both).

    Returns {"width", "height", "rgba"} where `rgba` is width*height*4 bytes,
    top-down, RGBA order (alpha always 0xFF - neither confirmed format has
    an alpha channel), cropped to the visible width x height rectangle.

    Raises DdbError if `data` is not one of the confirmed, decodable .ddb
    variants (e.g. one of the other header signatures that this module
    doesn't support - see the module docstring).
    """
    hdr = _parse_header(data, context)

    if _is_raw16(hdr):
        return _decode_raw16(data, hdr, context)
    if _is_raw24(hdr):
        return _decode_raw24(data, hdr, context)

    raise DdbError(
        f"'{context}': this .ddb uses an unrecognized/unsupported variant "
        f"(type=0x{hdr['type_byte']:02x}, format={hdr['format']}). Only the "
        f"raw-16bpp variant (type byte's low 7 bits = 2, format = 4) and the "
        f"raw-24bpp variant (type byte = 3, format = 6) are currently "
        f"supported - see ftools_lib/ddb.py for details."
    )


def _encode_raw16(original_data: bytes, hdr: dict, rgba: bytes, width: int, height: int) -> bytes:
    stride_px = _next_pow2(width)
    stride_bytes = stride_px * 2
    body = bytearray(original_data[DDB_HEADER_SIZE:])  # start from donor, preserves padding

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


def _encode_raw24(original_data: bytes, hdr: dict, rgba: bytes, width: int, height: int) -> bytes:
    extra = _raw24_find_extra(original_data[DDB_HEADER_SIZE:], width, height)
    prefix = original_data[DDB_HEADER_SIZE:DDB_HEADER_SIZE + extra]
    stride_bytes = _next_pow2(width) * 3
    body = bytearray(original_data[DDB_HEADER_SIZE + extra:])  # donor, preserves padding

    for row in range(height):
        row_off = row * stride_bytes
        in_off = row * width * 4
        for col in range(width):
            o = in_off + col * 4
            oo = row_off + col * 3
            body[oo] = rgba[o]
            body[oo + 1] = rgba[o + 1]
            body[oo + 2] = rgba[o + 2]

    return bytes(original_data[:DDB_HEADER_SIZE]) + bytes(prefix) + bytes(body)


def encode_ddb(original_data: bytes, rgba: bytes, width: int, height: int, context: str = "<ddb data>") -> bytes:
    """Re-encode pixel data back into .ddb format, using `original_data` (a
    real, confirmed-format .ddb file) as a donor for the header bytes and any
    padding region (columns beyond `width`, rows beyond `height`, and any
    RAW24 prefix block), which are preserved verbatim rather than
    regenerated.

    `width`/`height` must exactly match the original file's declared
    dimensions - this rebuilds the SAME image at the SAME size with new
    pixel content; it does not support resizing.

    `rgba` must be width*height*4 bytes, top-down, RGBA order (alpha is
    ignored - neither confirmed format has an alpha channel).
    """
    hdr = _parse_header(original_data, context)
    is16 = _is_raw16(hdr)
    is24 = _is_raw24(hdr)
    if not (is16 or is24):
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

    if is16:
        return _encode_raw16(original_data, hdr, rgba, width, height)
    return _encode_raw24(original_data, hdr, rgba, width, height)


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
