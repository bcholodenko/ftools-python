"""
Minimal, self-contained BMP reader/writer.

The original tools used the EasyBMP C++ library. EasyBMP's on-disk format is
plain, uncompressed Windows BMP (BITMAPFILEHEADER + BITMAPINFOHEADER), stored
bottom-up, with pixel byte order Blue/Green/Red[/Alpha]. Critically, EasyBMP
preserves a real per-pixel alpha channel in 32-bit BMPs - something macOS's
Preview.app and Pillow's own BMP codec do NOT reliably round-trip (Pillow's
BMP reader silently drops the 4th byte on read). To stay byte-compatible with
the original tools (and with files exchanged between them), this module
implements BMP reading/writing directly instead of relying on Pillow for file
I/O.

Pillow is still used elsewhere (in eif.py) purely for in-memory colour
quantization - never for BMP file I/O.

All functions work with flat RGBA byte buffers in TOP-DOWN row order
(row 0 is the visual top of the image), 4 bytes per pixel, regardless of the
on-disk storage order. This keeps the rest of the codebase simple.
"""

import struct
from pathlib import Path

BITMAPFILEHEADER_FMT = "<2sIHHI"
BITMAPFILEHEADER_SIZE = 14
BITMAPINFOHEADER_FMT = "<IiiHHIIiiII"
BITMAPINFOHEADER_SIZE = 40

DEFAULT_PELS_PER_METER = 3780  # matches EasyBMP's default (~96 DPI)


class BmpError(RuntimeError):
    pass


def read_bmp(path) -> dict:
    """Read a BMP file (1/4/8/24/32 bit, top-down or bottom-up, uncompressed).

    Returns a dict: {"width": int, "height": int, "bit_depth": int,
    "rgba": bytes} where `rgba` is width*height*4 bytes, top-down, RGBA order.
    For 24-bit (and palette) source images the alpha byte is always 0xFF.
    For 32-bit source images the alpha byte is taken verbatim from the file.
    """
    data = Path(path).read_bytes()
    if len(data) < BITMAPFILEHEADER_SIZE + 4:
        raise BmpError(f"'{path}' is not a valid BMP file: the file is too short to contain a BMP header.")

    bf_type, bf_size, _r1, _r2, bf_off_bits = struct.unpack_from(
        BITMAPFILEHEADER_FMT, data, 0
    )
    if bf_type != b"BM":
        raise BmpError(f"'{path}' is not a valid BMP file: missing the 'BM' signature.")

    bi_size = struct.unpack_from("<I", data, BITMAPFILEHEADER_SIZE)[0]
    if bi_size < BITMAPINFOHEADER_SIZE:
        raise BmpError(f"'{path}': unsupported BMP header size ({bi_size} bytes).")

    (
        _bi_size,
        bi_width,
        bi_height,
        _bi_planes,
        bi_bit_count,
        bi_compression,
        _bi_size_image,
        _bi_xppm,
        _bi_yppm,
        bi_clr_used,
        _bi_clr_important,
    ) = struct.unpack_from(BITMAPINFOHEADER_FMT, data, BITMAPFILEHEADER_SIZE)

    if bi_compression != 0:
        raise BmpError(f"'{path}': compressed BMP files are not supported (compression type {bi_compression}).")

    top_down = bi_height < 0
    width = bi_width
    height = abs(bi_height)
    if width <= 0 or height <= 0:
        raise BmpError(f"'{path}': invalid image dimensions ({width}x{height}).")

    palette = None
    if bi_bit_count in (1, 4, 8):
        num_colors = bi_clr_used if bi_clr_used else (1 << bi_bit_count)
        pal_off = BITMAPFILEHEADER_SIZE + bi_size
        palette = []
        for i in range(num_colors):
            b, g, r, _a = struct.unpack_from("<4B", data, pal_off + i * 4)
            palette.append((r, g, b))
    elif bi_bit_count not in (24, 32):
        raise BmpError(f"'{path}': unsupported bit depth ({bi_bit_count}-bit).")

    row_stride = ((width * bi_bit_count + 31) // 32) * 4
    pixel_off = bf_off_bits

    out = bytearray(width * height * 4)

    for file_row in range(height):
        row_start = pixel_off + file_row * row_stride
        row = data[row_start:row_start + row_stride]

        # file_row 0 is the first row stored in the file. For bottom-up BMPs
        # (the common case) that's the visual BOTTOM row; flip to get the
        # top-down visual row index.
        visual_row = file_row if top_down else (height - 1 - file_row)
        out_off = visual_row * width * 4

        if bi_bit_count == 32:
            # de-interleave B,G,R,A planes with fast slice ops, then
            # re-interleave as R,G,B,A
            out_row = bytearray(width * 4)
            out_row[0::4] = row[2::4][:width]  # R
            out_row[1::4] = row[1::4][:width]  # G
            out_row[2::4] = row[0::4][:width]  # B
            out_row[3::4] = row[3::4][:width]  # A
            out[out_off:out_off + width * 4] = out_row
        elif bi_bit_count == 24:
            pixels = row[:width * 3]
            out_row = bytearray(width * 4)
            out_row[0::4] = pixels[2::3]  # R
            out_row[1::4] = pixels[1::3]  # G
            out_row[2::4] = pixels[0::3]  # B
            out_row[3::4] = b"\xff" * width
            out[out_off:out_off + width * 4] = out_row
        elif bi_bit_count == 8:
            out_row = bytearray(width * 4)
            for x in range(width):
                r, g, b = palette[row[x]]
                out_row[x * 4:x * 4 + 4] = bytes((r, g, b, 0xFF))
            out[out_off:out_off + width * 4] = out_row
        elif bi_bit_count == 4:
            out_row = bytearray(width * 4)
            for x in range(width):
                byte = row[x // 2]
                idx = (byte >> 4) if (x % 2 == 0) else (byte & 0x0F)
                r, g, b = palette[idx]
                out_row[x * 4:x * 4 + 4] = bytes((r, g, b, 0xFF))
            out[out_off:out_off + width * 4] = out_row
        elif bi_bit_count == 1:
            out_row = bytearray(width * 4)
            for x in range(width):
                byte = row[x // 8]
                idx = (byte >> (7 - (x % 8))) & 0x01
                r, g, b = palette[idx]
                out_row[x * 4:x * 4 + 4] = bytes((r, g, b, 0xFF))
            out[out_off:out_off + width * 4] = out_row

    return {"width": width, "height": height, "bit_depth": bi_bit_count, "rgba": bytes(out)}


def write_bmp(path, width: int, height: int, rgba: bytes, bit_depth: int = 32) -> None:
    """Write a BMP file in EasyBMP-compatible format.

    `rgba` must be width*height*4 bytes, top-down, RGBA order.
    `bit_depth` must be 24 (alpha discarded) or 32 (alpha preserved).
    Output is always uncompressed, stored bottom-up, with no colour table -
    matching exactly what EasyBMP writes for 24/32 bit images.
    """
    if bit_depth not in (24, 32):
        raise BmpError(f"Cannot write BMP: only 24-bit and 32-bit output is supported (got {bit_depth}-bit).")
    if len(rgba) != width * height * 4:
        raise BmpError("Cannot write BMP: the RGBA buffer size does not match width × height × 4 bytes.")

    row_stride = ((width * bit_depth + 31) // 32) * 4
    pixel_data = bytearray(row_stride * height)

    for visual_row in range(height):
        file_row = height - 1 - visual_row  # bottom-up storage
        src_off = visual_row * width * 4
        dst_off = file_row * row_stride
        row_rgba = rgba[src_off:src_off + width * 4]

        if bit_depth == 32:
            out_row = bytearray(width * 4)
            out_row[0::4] = row_rgba[2::4]  # B
            out_row[1::4] = row_rgba[1::4]  # G
            out_row[2::4] = row_rgba[0::4]  # R
            out_row[3::4] = row_rgba[3::4]  # A
            pixel_data[dst_off:dst_off + width * 4] = out_row
        else:
            out_row = bytearray(width * 3)
            out_row[0::3] = row_rgba[2::4]  # B
            out_row[1::3] = row_rgba[1::4]  # G
            out_row[2::3] = row_rgba[0::4]  # R
            pixel_data[dst_off:dst_off + width * 3] = out_row
            # remaining bytes up to row_stride are left as zero padding

    off_bits = BITMAPFILEHEADER_SIZE + BITMAPINFOHEADER_SIZE
    total_size = off_bits + len(pixel_data)

    file_header = struct.pack(
        BITMAPFILEHEADER_FMT, b"BM", total_size, 0, 0, off_bits
    )
    info_header = struct.pack(
        BITMAPINFOHEADER_FMT,
        BITMAPINFOHEADER_SIZE,
        width,
        height,  # positive => bottom-up
        1,
        bit_depth,
        0,  # BI_RGB, no compression
        len(pixel_data),
        DEFAULT_PELS_PER_METER,
        DEFAULT_PELS_PER_METER,
        0,
        0,
    )

    Path(path).write_bytes(file_header + info_header + bytes(pixel_data))
