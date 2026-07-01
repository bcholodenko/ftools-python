"""
.ddb <-> PNG conversion for the IPC's "device-dependent bitmap" image
resources (found inside the QNX imagefs, e.g. images/variant/.../*.ddb).

All 5 known format variants are supported and round-trip byte-for-byte
(decode then re-encode with unedited pixels reproduces the original file
exactly).

HEADER (8 bytes, little-endian, common to all variants):
    Offset  Size  Field
    0       2     width      (u16) visible image width, pixels
    2       2     height     (u16) visible image height, pixels
    4       1     type_byte  observed: 0x02, 0x03, 0x82. High bit meaning
                             unknown; doesn't affect decoding; preserved
                             verbatim on round-trip.
    5       1     byte5      always observed as 0x80; meaning unknown;
                             preserved verbatim on round-trip.
    6       1     format     fully determines pixel layout, compression,
                             and presence of an alpha plane - see below.
    7       1     always 0 (high byte of the u16 starting at offset 6;
                             format values are all < 256)

PIXEL FORMAT:
    Each color sample is a 16-bit little-endian word:
        bit 15      alpha flag: 1 = fully opaque, 0 = fully transparent
                    (only meaningful for variants with no separate
                    mask plane; otherwise ignored for rendering, but
                    still stored data - preserved verbatim on encode)
        bits 14-10  red   (5 bits)
        bits 9-5    green (5 bits)
        bits 4-0    blue  (5 bits)
    Each 5-bit channel scales to 8-bit by multiplying by 255/31.

ROW PADDING ("real width" / "real height"):
    Pixel data is stored in a row-major grid wider than the visible
    image: realWidth columns by realHeight rows, where only the
    top-left width x height rectangle is the visible picture.
        realWidth  = the smallest value in [16,32,64,128,256,512,1024]
                     that is >= width            (format != 2, graphics-
                                                   VBF imagefs files - the
                                                   "bitmaps" convention)
                   = smallest power of 2 >= width (format != 2, EXE-VBF
                                                   efs.bin files - the
                                                   "efs" convention; no
                                                   minimum of 16, and two
                                                   specific named files
                                                   override this - see
                                                   _EFS_REALWIDTH_OVERRIDES)
                   = ceil(width / 4) * 4          (format == 2 only, both
                                                   conventions)
        realHeight = derived from the file size (see _raw_real_height())
                     for the 3 raw variants, or simply equal to the
                     declared height for the 2 RLE variants.
    A mask/alpha plane, when present, uses its own row width:
        maskWidth  = ceil(realWidth / 8) * 8
    Which convention applies is a property of which container the file
    came from, not of the file itself - decode_ddb()/encode_ddb()/
    is_supported_ddb() take it as the width_convention parameter
    ("bitmaps", the default, or "efs"). See _real_width() for detail.

THE 5 FORMAT VALUES (byte at header offset 6 encodes two flags):
    rle  = (format % 2) != 0       (odd  -> PackBits-compressed)
    mask = format in (2, 6, 7)     (has a separate alpha/mask plane)

    format  rle    mask   realWidth rule (within whichever convention
    ------  -----  -----  applies - see ROW PADDING above)
    4       False  False  pow2-list (bitmaps) / plain pow2 (efs)
    6       False  True   pow2-list (bitmaps) / plain pow2 (efs)
    2       False  True   ceil(w/4)*4, same in both conventions
    5       True   False  pow2-list (bitmaps) / plain pow2 (efs)
    7       True   True   pow2-list (bitmaps) / plain pow2 (efs)

LAYOUT WITHIN THE FILE BODY (everything after the 8-byte header):
    No mask  (format 4, 5):       [color plane]
    Has mask (format 2, 6, 7):    [color plane][mask/alpha plane]
    Not RLE  (format 2, 4, 6):    plane(s) stored raw, row-major,
                                  row stride = realWidth * bytes-per-px
                                  (2 for color, 1 for mask)
    RLE      (format 5, 7):       plane(s) PackBits-compressed
                                  independently - the mask stream starts
                                  right after the color stream's
                                  compressed length, not a fixed offset.

PACKBITS COMPRESSION (format 5 and 7 only):
    Two control-unit sizes are used: 16-bit units for the color plane,
    8-bit units for the mask plane. Shown here for the 16-bit case; the
    mask case substitutes 8-bit units and threshold 0x80 for 0x8000:

        read a control u16 `g`
        if g has its high bit set (g >= 0x8000):
            v = 0x10000 - g                  # 1..32768
            copy the next v raw u16 pixel values through unchanged
        else:
            g itself is the repeat count (0..32767)
            repeat the single u16 pixel value that follows, g times

    Decoding stops once realWidth*realHeight (color) or
    realWidth*realHeight (mask) target units have been produced; the
    mask stream then starts at the byte offset where the color stream's
    compressed data ended.

    The encoder side (_rle_encode_words/_rle_encode_bytes) reproduces the
    exact run/literal boundary choices needed for byte-identical
    round-trip: scanning left to right, pixels accumulate in a literal
    buffer; when a pixel equals its successor, that pixel joins the
    literal buffer (if non-empty) and the buffer flushes, with the repeat
    run measured starting at the successor; if that run is only 1 long,
    no repeat is emitted (it would cost more than it saves) and the lone
    pixel instead starts a new literal buffer. When the literal buffer is
    empty at the point a match is found, the run is measured directly at
    the current pixel with no absorption. A lone trailing pixel at the
    very end of the stream with no pending literal is emitted as a
    repeat of length 1.
"""

import struct

from .utils import read_file, write_file

DDB_HEADER_SIZE = 8
_HEADER_FMT = "<HHBBH"

_POW2_WIDTHS = (16, 32, 64, 128, 256, 512, 1024)
_CHANNEL_SCALE = 255 / 31  # 5-bit -> 8-bit


class DdbError(RuntimeError):
    pass


def _parse_header(data: bytes, context: str = "") -> dict:
    if len(data) < DDB_HEADER_SIZE:
        raise DdbError(f"'{context}': too short to be a .ddb file ({len(data)} bytes).")
    width, height, type_byte, byte5, fmt_u16 = struct.unpack_from(_HEADER_FMT, data, 0)
    fmt = fmt_u16 & 0xFF
    if fmt_u16 > 0xFF:
        raise DdbError(f"'{context}': unexpected format value 0x{fmt_u16:04x} (high byte set).")
    if width == 0 or height == 0:
        raise DdbError(f"'{context}': implausible .ddb header (width={width}, height={height}).")
    return {
        "width": width, "height": height,
        "type_byte": type_byte, "byte5": byte5,
        "format": fmt,
        "rle": (fmt % 2) != 0,
        "mask": fmt in (2, 6, 7),
    }


# Two known, named exceptions to the "efs" convention's realWidth formula,
# found by cross-checking every entry in the reference tool's own static
# per-file metadata table (the one it uses instead of computing realWidth
# from width, for files inside an EXE-VBF's efs.bin) against the formula
# below. Confirmed: every other entry in that table (468 real files) matches
# the formula exactly; only these two don't, and both look like one-off
# manual overrides in a hand-maintained table rather than evidence of a
# third general rule. Since the reference tool's own static table is the
# thing real hardware was actually built against, these two filenames are
# special-cased here rather than left to silently compute the wrong row
# stride - especially important for the RLE one (c489_backup_slot.ddb),
# where a wrong realWidth has no other validation to catch it (unlike the
# raw formats, where _raw_real_height() at least has a chance to notice the
# file size doesn't divide evenly and raise instead of silently corrupting).
_EFS_REALWIDTH_OVERRIDES = {
    "c489_backup_slot.ddb": 256,       # formula would give 128 (width=95)
    "das_TJA_grey_dash_6_.ddb": 16,    # formula would give 4   (width=3)
}


def _real_width(fmt: int, width: int, convention: str = "bitmaps", context: str = "") -> int:
    """`convention` distinguishes the two real-width rules observed in the
    wild for non-format-2 images:
      - "bitmaps" (default): smallest of the fixed list (16,32,...,1024)
        that's >= width. This is what every file in a graphics-VBF
        ("bitmaps.bin"-style) imagefs uses, and what this module has
        always assumed.
      - "efs": the mathematically simpler "smallest power of 2 >= width",
        with no minimum of 16 - i.e. a width of 3 gets realWidth 4, not
        16. This is what files inside an EXE-VBF's "efs.bin" container
        use instead (see ftools_lib/efs.py) - same pixel format and
        per-format rules otherwise, just a different width rounding rule
        for this one part. Confirmed against 468 known real files; only
        handles widths that fit in 16 bits, same as the format itself.

    `context` (typically a file path or name) is checked against
    _EFS_REALWIDTH_OVERRIDES when convention=="efs" - see that dict's
    comment. Matching is by suffix, so a full path or a bare filename both
    work; pass "" (the default) if the caller has no name to give, which
    just means those two specific files won't get their override applied.
    """
    if convention == "efs" and context:
        for name, override in _EFS_REALWIDTH_OVERRIDES.items():
            if context.endswith(name):
                return override
    if fmt == 2:
        return ((width + 3) // 4) * 4
    if convention == "efs":
        w = 1
        while w < width:
            w *= 2
        return w
    for w in _POW2_WIDTHS:
        if w >= width:
            return w
    raise DdbError(
        f"width {width} exceeds 1024, the largest row width this format supports. "
        f"This isn't a tool limitation - it mirrors the same lookup table the real "
        f"IPC firmware uses internally, so a wider image likely couldn't be shown "
        f"correctly on real hardware even if a file could be built for it."
    )


def _mask_width(real_width: int) -> int:
    return ((real_width + 7) // 8) * 8


def _raw_real_height(fmt: int, width: int, height: int, real_width: int, file_size: int) -> tuple:
    """Derive (color_real_height, mask_real_height) for the 3 non-RLE
    formats (2, 4, 6) from the file size. When the simple division isn't
    an integer, the mask plane's row count gets a "+1"/"+2" adjustment
    preferentially while the color plane keeps the unadjusted value -
    they are not always equal."""
    body_size = file_size - DDB_HEADER_SIZE
    s = real_width
    if fmt == 4:
        c = body_size / s / 2
        if c == int(c) and int(c) >= height:
            return int(c), int(c)
        raise DdbError(f"format 4: body size {body_size} doesn't divide evenly by row stride {s*2}.")

    o = _mask_width(s)
    max_extra = 1 if fmt == 2 else 2
    c = body_size / (2 * s + o)
    d = None
    for extra in range(1, max_extra + 1):
        if c == int(c):
            break
        c = (body_size - extra * o) / (2 * s + o)
        d = c + extra
    if c != int(c):
        raise DdbError(
            f"format {fmt}: body size {body_size} doesn't fit the expected row layout "
            f"(realWidth={s}, maskWidth={o}) for 0..{max_extra} extra padding chunks."
        )
    color_h = int(c)
    mask_h = int(d) if d is not None else color_h
    if color_h < height:
        raise DdbError(f"format {fmt}: derived color real-height {color_h} is less than declared height {height}.")
    return color_h, mask_h


# ── PackBits-style RLE (formats 5, 7) ──────────────────────────────────────────

def _rle_decode_words(data: bytes, target_bytes: int) -> tuple:
    """Decode the 16-bit-unit PackBits stream used for the color plane.
    Returns (decoded_bytes, bytes_consumed_from_input)."""
    out = bytearray()
    c = 0
    n = len(data)
    while c < n and len(out) < target_bytes:
        if c + 1 >= n:
            break
        g = data[c] | (data[c + 1] << 8)
        c += 2
        if g & 0x8000:
            v = 0x10000 - g
            for _ in range(v):
                if c + 1 >= n:
                    break
                out.append(data[c]); out.append(data[c + 1])
                c += 2
        else:
            if c + 1 >= n:
                break
            b0, b1 = data[c], data[c + 1]
            for _ in range(g):
                out.append(b0); out.append(b1)
            c += 2
    return bytes(out[:target_bytes]), c


def _rle_decode_bytes(data: bytes, target_bytes: int) -> bytes:
    """Decode the 8-bit-unit PackBits stream used for the mask plane."""
    out = bytearray()
    d = 0
    n = len(data)
    while d < n and len(out) < target_bytes:
        f = data[d]
        d += 1
        if f & 0x80:
            g = 256 - f
            for _ in range(g):
                if d >= n:
                    break
                out.append(data[d]); d += 1
        else:
            if d >= n:
                break
            b0 = data[d]
            for _ in range(f):
                out.append(b0)
            d += 1
    return bytes(out[:target_bytes])


def _rle_encode_words(pixel_bytes: bytes) -> bytes:
    """PackBits-style encoder for the 16-bit-unit color stream. See the
    module docstring for the exact run/literal boundary rules."""
    n_units = len(pixel_bytes) // 2
    units = struct.unpack(f"<{n_units}H", pixel_bytes) if n_units else ()
    out = bytearray()
    literal_buf = []

    def flush():
        if literal_buf:
            out.extend(struct.pack("<H", 0x10000 - len(literal_buf)))
            for v in literal_buf:
                out.extend(struct.pack("<H", v))
            literal_buf.clear()

    i = 0
    while i < n_units:
        if i + 1 < n_units and units[i] == units[i + 1]:
            if literal_buf:
                literal_buf.append(units[i])
                flush()
                run_start = i + 1
            else:
                run_start = i
            run_len = 1
            while (run_start + run_len < n_units
                   and units[run_start + run_len] == units[run_start]
                   and run_len < 32767):
                run_len += 1
            if run_len >= 2:
                out.extend(struct.pack("<H", run_len))
                out.extend(struct.pack("<H", units[run_start]))
                i = run_start + run_len
            else:
                literal_buf.append(units[run_start])
                i = run_start + 1
        elif i == n_units - 1 and not literal_buf:
            out.extend(struct.pack("<H", 1))
            out.extend(struct.pack("<H", units[i]))
            i += 1
        else:
            literal_buf.append(units[i])
            if len(literal_buf) >= 32768:
                flush()
            i += 1
    flush()
    return bytes(out)


def _rle_encode_bytes(data: bytes) -> bytes:
    """PackBits-style encoder for the 8-bit-unit mask stream, same
    scan/flush behavior as _rle_encode_words()."""
    n = len(data)
    out = bytearray()
    literal_buf = []

    def flush():
        if literal_buf:
            out.append(256 - len(literal_buf))
            out.extend(literal_buf)
            literal_buf.clear()

    i = 0
    while i < n:
        if i + 1 < n and data[i] == data[i + 1]:
            if literal_buf:
                literal_buf.append(data[i])
                flush()
                run_start = i + 1
            else:
                run_start = i
            run_len = 1
            while (run_start + run_len < n
                   and data[run_start + run_len] == data[run_start]
                   and run_len < 127):
                run_len += 1
            if run_len >= 2:
                out.append(run_len)
                out.append(data[run_start])
                i = run_start + run_len
            else:
                literal_buf.append(data[run_start])
                i = run_start + 1
        elif i == n - 1 and not literal_buf:
            out.append(1)
            out.append(data[i])
            i += 1
        else:
            literal_buf.append(data[i])
            if len(literal_buf) >= 128:
                flush()
            i += 1
    flush()
    return bytes(out)


# ── pixel <-> RGBA ──────────────────────────────────────────────────────────────

def _word_to_rgb(val: int) -> tuple:
    r = round(((val >> 10) & 0x1F) * _CHANNEL_SCALE)
    g = round(((val >> 5) & 0x1F) * _CHANNEL_SCALE)
    b = round((val & 0x1F) * _CHANNEL_SCALE)
    return r, g, b


def _rgb_to_word(r: int, g: int, b: int) -> int:
    r5 = round(r / _CHANNEL_SCALE)
    g5 = round(g / _CHANNEL_SCALE)
    b5 = round(b / _CHANNEL_SCALE)
    return ((r5 & 0x1F) << 10) | ((g5 & 0x1F) << 5) | (b5 & 0x1F)


def dither_rgb_to_5bit(rgba: bytes, width: int, height: int) -> list:
    """Floyd-Steinberg dithered quantization of the R/G/B channels (alpha
    ignored - callers handle that separately, since how alpha is stored
    is format-specific) from 8-bit to 5-bit per channel, row-major
    top-down order. Returns a flat list of `width*height` (r5,g5,b5)
    tuples.

    5-bit-per-channel color only has 32 levels per channel, so a smooth
    gradient quantized without dithering shows visible banding; this
    breaks that up by diffusing each pixel's rounding error into its
    not-yet-processed neighbors (right, bottom-left, bottom,
    bottom-right, with the standard 7/3/5/1-sixteenths weights).

    Shared by encode_ddb() and png_to_bmp_a1r5g5b5.py specifically so
    both apply identical dithering - this is the single source of truth
    for that algorithm, not a copy of it.

    For a pixel whose 8-bit value already sits exactly on the 5-bit
    quantization grid (e.g. read back from a never-edited decode_ddb()
    PNG), the rounding error is exactly zero, so nothing gets diffused
    to its neighbors - dithering an entirely unedited image is a no-op,
    only edited regions (and pixels near them) are actually affected.
    """
    data = list(rgba)
    out = [None] * (width * height)
    for y in range(height):
        for x in range(width):
            idx = (y * width + x) * 4
            r = max(0, min(255, data[idx]))
            g = max(0, min(255, data[idx + 1]))
            b = max(0, min(255, data[idx + 2]))

            r5 = max(0, min(31, int(r * 31 / 255 + 0.5)))
            g5 = max(0, min(31, int(g * 31 / 255 + 0.5)))
            b5 = max(0, min(31, int(b * 31 / 255 + 0.5)))
            out[y * width + x] = (r5, g5, b5)

            r8 = round(r5 * _CHANNEL_SCALE)
            g8 = round(g5 * _CHANNEL_SCALE)
            b8 = round(b5 * _CHANNEL_SCALE)
            err_r = r - r8
            err_g = g - g8
            err_b = b - b8

            if err_r or err_g or err_b:
                if x + 1 < width:
                    n = idx + 4
                    data[n] += err_r * 0.4375
                    data[n + 1] += err_g * 0.4375
                    data[n + 2] += err_b * 0.4375
                if x - 1 >= 0 and y + 1 < height:
                    n = ((y + 1) * width + x - 1) * 4
                    data[n] += err_r * 0.1875
                    data[n + 1] += err_g * 0.1875
                    data[n + 2] += err_b * 0.1875
                if y + 1 < height:
                    n = ((y + 1) * width + x) * 4
                    data[n] += err_r * 0.3125
                    data[n + 1] += err_g * 0.3125
                    data[n + 2] += err_b * 0.3125
                if x + 1 < width and y + 1 < height:
                    n = ((y + 1) * width + x + 1) * 4
                    data[n] += err_r * 0.0625
                    data[n + 1] += err_g * 0.0625
                    data[n + 2] += err_b * 0.0625
    return out


# ── public API ──────────────────────────────────────────────────────────────────

def is_supported_ddb(data: bytes, width_convention: str = "bitmaps", context: str = "") -> bool:
    """Return True if `data` is a decodable .ddb file. `width_convention`
    matters because it changes the expected size of the file - see
    _real_width(). Pass `context` (the file's path or name) when known and
    width_convention=="efs" so the two named realWidth exceptions in
    _EFS_REALWIDTH_OVERRIDES get applied here too, not just in decode_ddb()/
    encode_ddb() - otherwise this check would validate against the wrong
    (formula) realWidth for those two files and could wrongly report them
    as unsupported."""
    try:
        hdr = _parse_header(data)
    except DdbError:
        return False
    if hdr["format"] not in (2, 4, 5, 6, 7):
        return False
    try:
        s = _real_width(hdr["format"], hdr["width"], width_convention, context)
        if not hdr["rle"]:
            _raw_real_height(hdr["format"], hdr["width"], hdr["height"], s, len(data))
        return True
    except DdbError:
        return False


def decode_ddb(data: bytes, context: str = "<ddb data>", width_convention: str = "bitmaps") -> dict:
    """Decode a .ddb buffer to RGBA pixel data.

    Returns {"width", "height", "rgba"} where rgba is width*height*4 bytes,
    top-down, R/G/B/A order, cropped to the visible width x height rectangle.

    For width_convention=="efs", `context` doubles as the lookup key for
    _EFS_REALWIDTH_OVERRIDES (matched by suffix) - pass the file's real
    name/path here, not a generic placeholder, or the two named exceptions
    won't get caught and this will silently decode with the wrong row
    stride for those two specific files.

    `width_convention` selects the realWidth rounding rule - "bitmaps"
    (default) for files from a graphics-VBF's imagefs, "efs" for files
    from an EXE-VBF's efs.bin container. See _real_width().
    """
    hdr = _parse_header(data, context)
    fmt, width, height = hdr["format"], hdr["width"], hdr["height"]
    if fmt not in (2, 4, 5, 6, 7):
        raise DdbError(
            f"'{context}': unrecognized .ddb format byte {fmt} "
            f"(known values: 2, 4, 5, 6, 7)."
        )

    body = data[DDB_HEADER_SIZE:]
    s = _real_width(fmt, width, width_convention, context)
    m = _mask_width(s)

    if hdr["rle"]:
        real_h = height
        color_target = s * real_h * 2
        color_plane, consumed = _rle_decode_words(body, color_target)
        if len(color_plane) != color_target:
            raise DdbError(f"'{context}': RLE color stream decoded short "
                            f"({len(color_plane)}/{color_target} bytes).")
        mask_plane = b""
        if hdr["mask"]:
            mask_target = m * real_h
            mask_plane = _rle_decode_bytes(body[consumed:], mask_target)
            if len(mask_plane) != mask_target:
                raise DdbError(f"'{context}': RLE mask stream decoded short "
                                f"({len(mask_plane)}/{mask_target} bytes).")
    else:
        color_h, mask_h = _raw_real_height(fmt, width, height, s, len(data))
        color_target = s * color_h * 2
        color_plane = body[:color_target]
        if len(color_plane) < color_target:
            raise DdbError(f"'{context}': file too short for color plane "
                            f"({len(color_plane)}/{color_target} bytes).")
        mask_plane = b""
        if hdr["mask"]:
            mask_target = m * mask_h
            mask_plane = body[color_target:color_target + mask_target]
            if len(mask_plane) < mask_target:
                raise DdbError(f"'{context}': file too short for mask plane "
                                f"({len(mask_plane)}/{mask_target} bytes).")

    rgba = bytearray(width * height * 4)
    for row in range(height):
        c_row_off = row * s * 2
        m_row_off = row * m
        out_off = row * width * 4
        row_words = struct.unpack_from(f"<{width}H", color_plane, c_row_off)
        for col, val in enumerate(row_words):
            r, g, b = _word_to_rgb(val)
            if hdr["mask"]:
                a = mask_plane[m_row_off + col]
            else:
                a = 0xFF if (val & 0x8000) else 0x00
            o = out_off + col * 4
            rgba[o] = r; rgba[o + 1] = g; rgba[o + 2] = b; rgba[o + 3] = a

    return {"width": width, "height": height, "rgba": bytes(rgba)}


def encode_ddb(original_data: bytes, rgba: bytes, width: int, height: int,
               context: str = "<ddb data>", width_convention: str = "bitmaps",
               dither: bool = True) -> bytes:
    """Re-encode RGBA pixel data back into .ddb format.

    Uses `original_data` (a real .ddb file) as a donor for the header
    (type_byte/byte5/format) and, when `width`/`height` match the donor
    exactly, for any padding columns/rows beyond the visible rectangle too
    (preserved verbatim, giving a byte-identical file for unedited
    pixels - including for the RLE formats, since the encoder reproduces
    the original compressor's exact run/literal choices).

    `width`/`height` may differ from the donor's own dimensions - the
    image is resized: a fresh buffer is built at the new size (padding
    columns/rows are zero-filled rather than donor-preserved, since the
    donor's padding doesn't correspond to anything at the new size) and
    the header's width/height fields are updated to match. The output
    will naturally be a different length than the donor in this case.

    `rgba` must be width*height*4 bytes, top-down, R/G/B/A order.

    `width_convention` selects the realWidth rounding rule - "bitmaps"
    (default) for files from a graphics-VBF's imagefs, "efs" for files
    from an EXE-VBF's efs.bin container. See _real_width(). For "efs",
    `context` doubles as the _EFS_REALWIDTH_OVERRIDES lookup key (matched
    by suffix) - pass the donor's real name/path, not a placeholder, or
    the two named exceptions won't get caught.

    `dither` (default True) applies the same Floyd-Steinberg dithering as
    png_to_bmp_a1r5g5b5.py - see dither_rgb_to_5bit(). This is a no-op
    for pixels that are already exactly representable in 5-bit color (in
    particular, an entirely unedited decode_ddb() PNG round-trips
    byte-identically either way), so leaving it on doesn't change
    behavior for unedited files; it only changes how genuinely new or
    edited pixel data gets quantized.
    """
    hdr = _parse_header(original_data, context)
    fmt = hdr["format"]
    if fmt not in (2, 4, 5, 6, 7):
        raise DdbError(f"'{context}': donor .ddb uses an unsupported format byte {fmt}.")
    if width <= 0 or height <= 0:
        raise DdbError(f"'{context}': width and height must be positive (got {width}x{height}).")
    if len(rgba) != width * height * 4:
        raise DdbError(f"'{context}': RGBA buffer size does not match width*height*4.")

    resized = (width != hdr["width"] or height != hdr["height"])
    body = original_data[DDB_HEADER_SIZE:]
    s = _real_width(fmt, width, width_convention, context)
    m = _mask_width(s)


    if resized:
        # Fresh buffers at the new size - no donor pixel data carries over,
        # since the donor's padding/layout corresponds to different dimensions.
        color_h = mask_h = height
        donor_color = bytes(s * color_h * 2)
        mask_plane = bytearray(m * mask_h) if hdr["mask"] else bytearray()
    elif hdr["rle"]:
        color_h = mask_h = height
        color_target = s * height * 2
        donor_color, consumed = _rle_decode_words(body, color_target)
        mask_plane = bytearray()
        if hdr["mask"]:
            mask_target = m * height
            mask_plane[:] = _rle_decode_bytes(body[consumed:], mask_target)
    else:
        color_h, mask_h = _raw_real_height(fmt, width, height, s, len(original_data))
        color_target = s * color_h * 2
        donor_color = body[:color_target]
        mask_plane = bytearray()
        if hdr["mask"]:
            mask_target = m * mask_h
            mask_plane[:] = body[color_target:color_target + mask_target]

    color_plane = bytearray(donor_color)
    dithered = dither_rgb_to_5bit(rgba, width, height) if dither else None
    for row in range(height):
        c_row_off = row * s * 2
        m_row_off = row * m
        in_off = row * width * 4
        words = []
        for col in range(width):
            o = in_off + col * 4
            r, g, b, a = rgba[o], rgba[o + 1], rgba[o + 2], rgba[o + 3]
            if dithered is not None:
                r5, g5, b5 = dithered[row * width + col]
                val = (r5 << 10) | (g5 << 5) | b5
            else:
                val = _rgb_to_word(r, g, b)
            if hdr["mask"]:
                if not resized:
                    # bit15 is unused for transparency here but still stored;
                    # preserve the donor's bit so unedited pixels round-trip exactly.
                    donor_word = struct.unpack_from("<H", donor_color, c_row_off + col * 2)[0]
                    val |= (donor_word & 0x8000)
                mask_plane[m_row_off + col] = a
            else:
                if a >= 0x80:
                    val |= 0x8000
                else:
                    val &= 0x7FFF
            words.append(val)
        struct.pack_into(f"<{width}H", color_plane, c_row_off, *words)

    if hdr["rle"]:
        new_body = bytearray(_rle_encode_words(bytes(color_plane)))
        if hdr["mask"]:
            new_body += _rle_encode_bytes(bytes(mask_plane))
    else:
        new_body = bytearray(color_plane)
        if hdr["mask"]:
            new_body += mask_plane
        if not resized:
            consumed_total = len(new_body)
            if len(body) > consumed_total:
                new_body += body[consumed_total:]

    new_header = struct.pack(_HEADER_FMT, width, height, hdr["type_byte"], hdr["byte5"], fmt)
    return new_header + bytes(new_body)


# ── file-level helpers ────────────────────────────────────────────────────────

def ddb_to_png_file(ddb_data: bytes, out_path, context: str = "",
                     width_convention: str = "bitmaps") -> None:
    """Decode a .ddb buffer and write a PNG file, preserving transparency.

    `width_convention` - "bitmaps" (default) for files from a graphics-VBF's
    imagefs, "efs" for files from an EXE-VBF's efs.bin container - see
    _real_width() in this module. Pass `context` as the file's real name
    when using "efs" so the two named realWidth exceptions get applied;
    falling back to `out_path` (the default when context is empty) works
    too as long as the output filename still matches the original name.
    """
    from PIL import Image
    decoded = decode_ddb(ddb_data, context=context or str(out_path),
                         width_convention=width_convention)
    img = Image.frombytes("RGBA", (decoded["width"], decoded["height"]), decoded["rgba"])
    img.save(str(out_path), "PNG")


def png_file_to_ddb_file(png_path, original_ddb_path, out_path, dither: bool = True,
                          width_convention: str = "bitmaps") -> None:
    """Read a PNG (including its alpha channel) and encode it back to
    .ddb using the original as a donor for header/padding bytes.

    `width_convention` - see ddb_to_png_file() above and _real_width() in
    this module. `original_ddb_path` doubles as the realWidth-override
    lookup key for "efs", so pass the file's real name, not a renamed copy.
    """
    from PIL import Image
    img = Image.open(png_path).convert("RGBA")
    rgba = img.tobytes()
    original_data = read_file(original_ddb_path)
    new_ddb = encode_ddb(
        original_data, rgba, img.width, img.height,
        context=str(original_ddb_path), dither=dither,
        width_convention=width_convention,
    )
    write_file(out_path, new_ddb)
