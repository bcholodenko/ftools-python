"""
EIF <-> BMP conversion, equivalent to EifConverter.cpp/.h.

EIF ("EBD.EIF") is Ford's internal image format used for IPC (instrument
cluster) UI resources. There are three flavours, distinguished by a 1-byte
type field in the header:

    0x04  MONOCHROME  ("8-bit"):  grayscale, 1 byte/pixel, no alpha.
    0x07  MULTICOLOR  ("16-bit"): 256-colour palette + 1 alpha byte/pixel
                                   (2 bytes/pixel total).
    0x0E  SUPERCOLOR  ("32-bit"): true colour BGRA, 4 bytes/pixel, full alpha.

All images are read/written through bmpio.py (see that module for why we
don't use Pillow for BMP file I/O). Pillow IS used here, in-memory only, to
perform 256-colour palette quantization for the MULTICOLOR format - this
replaces the original tool's `exoquant` library. The quantization algorithm
is different (Pillow's median-cut/octree vs. exoquant), so byte-for-byte
identical palettes are not guaranteed, but the result is a valid, good
quality 256-colour EIF image either way.
"""

import struct
from pathlib import Path

from PIL import Image

from . import bmpio
from .utils import read_file, write_file

EIF_SIGNATURE = b"EBD\x10EIF"

EIF_TYPE_MONOCHROME = 0x04
EIF_TYPE_MULTICOLOR = 0x07
EIF_TYPE_SUPERCOLOR = 0x0E

EIF_MULTICOLOR_NUM_COLORS = 256
EIF_MULTICOLOR_PALETTE_SIZE = 0x300  # 256 * 3 bytes (RGB only, no alpha on disk)

_BASE_HEADER_FMT = "<7sBIHH"  # signature, type, length, width, height
_BASE_HEADER_SIZE = struct.calcsize(_BASE_HEADER_FMT)
assert _BASE_HEADER_SIZE == 16


class EifError(RuntimeError):
    pass


def depth_to_eif_type(depth: int) -> int:
    return {8: EIF_TYPE_MONOCHROME, 16: EIF_TYPE_MULTICOLOR, 32: EIF_TYPE_SUPERCOLOR}.get(depth) \
        or (_ for _ in ()).throw(EifError(f"Invalid color depth: {depth} (must be 8, 16, or 32)."))


def eif_type_to_depth(eif_type: int) -> int:
    return {EIF_TYPE_MONOCHROME: 8, EIF_TYPE_MULTICOLOR: 16, EIF_TYPE_SUPERCOLOR: 32}.get(eif_type, 0)


def _pad_palette_to_256(flat_rgb) -> list:
    """Pillow's getpalette() returns only as many entries as were actually
    used; pad out to exactly 256 RGB triples (768 ints) with black, since the
    on-disk EIF palette always has exactly 256 entries."""
    flat_rgb = list(flat_rgb)
    needed = 256 * 3 - len(flat_rgb)
    if needed > 0:
        flat_rgb.extend([0] * needed)
    return flat_rgb[:256 * 3]


def _quantize_rgba_to_palette(rgba: bytes, width: int, height: int, flat_rgb_768):
    """Map an RGBA buffer onto a fixed, externally supplied 256-colour
    palette via exact nearest-colour (Euclidean, in RGB space) matching.
    Alpha is ignored for the colour match, matching the original tool's
    `exq_no_transparency` mode.

    NOTE: Pillow's own `Image.quantize(palette=...)` does NOT guarantee that
    a pixel whose colour is verbatim present in the supplied palette maps
    back to that exact index (it runs the image through its own octree
    quantizer first, which can shift things slightly) - verified by direct
    test. That matters here because a common real use case is "remap onto
    this exact externally-supplied palette" (the tool's `-s` option), where
    pixel-perfect fidelity for colours that already exist in the palette is
    expected. We do an explicit, exact nearest-colour search instead via
    numpy, chunked to bound peak memory on large images."""
    import numpy as np

    n_pixels = width * height
    pixels = np.frombuffer(rgba, dtype=np.uint8).reshape(n_pixels, 4)[:, :3].astype(np.int16)
    palette = np.array(flat_rgb_768, dtype=np.int16).reshape(256, 3)

    indices = np.empty(n_pixels, dtype=np.uint8)
    chunk = 8192
    for start in range(0, n_pixels, chunk):
        end = min(start + chunk, n_pixels)
        diff = pixels[start:end, None, :] - palette[None, :, :]
        dist = np.sum(diff.astype(np.int32) * diff.astype(np.int32), axis=2)
        indices[start:end] = np.argmin(dist, axis=1).astype(np.uint8)
    return bytes(indices)


def _quantize_rgba_self(rgba: bytes, width: int, height: int):
    """Build a fresh 256-colour palette from this image's own RGB content
    (alpha ignored) and map pixels to it. Returns (indices, flat_rgb_768)."""
    rgb_img = Image.frombytes("RGBA", (width, height), rgba).convert("RGB")
    mapped = rgb_img.quantize(colors=256, dither=Image.Dither.NONE)
    indices = bytes(mapped.getdata())
    flat_rgb = _pad_palette_to_256(mapped.getpalette())
    return indices, flat_rgb


class EifImageBase:
    eif_type = None

    def __init__(self):
        self.width = 0
        self.height = 0
        self.rgba = b""  # always top-down RGBA, 4 bytes/pixel, width*height*4 long

    def open_bmp(self, file_path):
        info = bmpio.read_bmp(file_path)
        self.width = info["width"]
        self.height = info["height"]
        self.rgba = info["rgba"]
        self._on_bmp_loaded(info["bit_depth"])

    def _on_bmp_loaded(self, source_bit_depth):
        pass

    def save_bmp(self, file_path, bit_depth=24):
        bmpio.write_bmp(file_path, self.width, self.height, self.rgba, bit_depth=bit_depth)

    def open_eif(self, data: bytes):
        raise NotImplementedError

    def _store_palette(self) -> bytes:
        return b""

    def _store_bitmap(self) -> bytes:
        raise NotImplementedError

    def save_eif_to_bytes(self) -> bytes:
        if self.eif_type == EIF_TYPE_MULTICOLOR:
            length = self.width * self.height * 2
        else:
            length = len(self._store_bitmap_raw())
        header = struct.pack(
            _BASE_HEADER_FMT,
            EIF_SIGNATURE,
            self.eif_type,
            length,
            self.width & 0xFFFF,
            self.height & 0xFFFF,
        )
        return header + self._store_palette() + self._store_bitmap()

    def _store_bitmap_raw(self) -> bytes:
        """Used only to compute `length` for non-multicolor types; the
        actual on-disk bitmap bytes for those types equal this exactly."""
        return self._store_bitmap()

    def save_eif(self, file_path):
        write_file(file_path, self.save_eif_to_bytes())


class EifImage8bit(EifImageBase):
    """MONOCHROME: 1 byte/pixel grayscale, rows padded to a 4-byte boundary."""
    eif_type = EIF_TYPE_MONOCHROME

    def __init__(self):
        super().__init__()
        self._gray = b""  # raw on-disk bytes, height * aligned_width, may include row padding

    def _on_bmp_loaded(self, source_bit_depth):
        width, height = self.width, self.height
        aligned_width = width if (width % 4 == 0) else (width // 4 + 1) * 4
        gray = bytearray(height * aligned_width)
        for y in range(height):
            row_off = y * width * 4
            out_off = y * aligned_width
            for x in range(width):
                r, g, b, _a = self.rgba[row_off + x * 4:row_off + x * 4 + 4]
                gray[out_off + x] = (r + g + b) // 3
            # remaining aligned_width - width bytes in this row stay 0
        self._gray = bytes(gray)

    def open_eif(self, data: bytes):
        if len(data) < _BASE_HEADER_SIZE:
            raise EifError("Not a valid EIF file: too short to contain a header.")
        signature, eif_type, length, width, height = struct.unpack_from(_BASE_HEADER_FMT, data, 0)
        if signature != EIF_SIGNATURE:
            raise EifError("Not a valid EIF file: signature does not match.")
        if eif_type != EIF_TYPE_MONOCHROME:
            raise EifError("Not a valid EIF file: unexpected image type for this format.")

        data_offset = _BASE_HEADER_SIZE
        if length > (len(data) - data_offset):
            raise EifError("Not a valid EIF file: declared data length exceeds the available data.")
        if height == 0:
            raise EifError("Not a valid EIF file: height is zero.")

        aligned_width = length // height
        if aligned_width % 4:
            raise EifError("Not a valid EIF file: row width is not properly aligned.")
        if aligned_width < width:
            raise EifError("Not a valid EIF file: declared width is larger than the aligned row width.")

        gray = bytearray(width * height)
        for row in range(height):
            src_off = data_offset + row * aligned_width
            gray[row * width:(row + 1) * width] = data[src_off:src_off + width]

        self.width = width
        self.height = height
        rgba = bytearray(width * height * 4)
        rgba[0::4] = gray
        rgba[1::4] = gray
        rgba[2::4] = gray
        rgba[3::4] = b"\xff" * (width * height)
        self.rgba = bytes(rgba)
        self._gray = bytes(gray)  # unpadded; matches original behaviour after openEif

    def _store_bitmap(self) -> bytes:
        return self._gray


class EifImage32bit(EifImageBase):
    """SUPERCOLOR: true colour, 4 bytes/pixel, stored on-disk as B,G,R,A."""
    eif_type = EIF_TYPE_SUPERCOLOR

    def open_eif(self, data: bytes):
        if len(data) < _BASE_HEADER_SIZE:
            raise EifError("Not a valid EIF file: too short to contain a header.")
        signature, eif_type, length, width, height = struct.unpack_from(_BASE_HEADER_FMT, data, 0)
        if signature != EIF_SIGNATURE:
            raise EifError("Not a valid EIF file: signature does not match.")
        if eif_type != EIF_TYPE_SUPERCOLOR:
            raise EifError("Not a valid EIF file: unexpected image type for this format.")

        data_offset = _BASE_HEADER_SIZE
        if length > (len(data) - data_offset):
            raise EifError("Not a valid EIF file: declared data length exceeds the available data.")
        if height == 0:
            raise EifError("Not a valid EIF file: height is zero.")

        aligned4_width = length // height
        if aligned4_width % 4:
            raise EifError("Not a valid EIF file: row width is not properly aligned.")
        if (aligned4_width // 4) < width:
            raise EifError("Not a valid EIF file: declared width is larger than the aligned row width.")

        bgra = bytearray(width * height * 4)
        for row in range(height):
            src_off = data_offset + row * aligned4_width
            bgra[row * width * 4:(row + 1) * width * 4] = data[src_off:src_off + width * 4]

        rgba = bytearray(width * height * 4)
        rgba[0::4] = bgra[2::4]
        rgba[1::4] = bgra[1::4]
        rgba[2::4] = bgra[0::4]
        rgba[3::4] = bgra[3::4]

        self.width = width
        self.height = height
        self.rgba = bytes(rgba)

    def _store_bitmap(self) -> bytes:
        bgra = bytearray(len(self.rgba))
        bgra[0::4] = self.rgba[2::4]
        bgra[1::4] = self.rgba[1::4]
        bgra[2::4] = self.rgba[0::4]
        bgra[3::4] = self.rgba[3::4]
        return bytes(bgra)


class EifImage16bit(EifImageBase):
    """MULTICOLOR: 256-colour palette + per-pixel alpha (2 bytes/pixel)."""
    eif_type = EIF_TYPE_MULTICOLOR

    def __init__(self):
        super().__init__()
        self.palette = None  # flat list of 768 ints (256 * RGB), or None if not yet set
        self._indices = None  # cached palette-index bytes, one per pixel (set on open_eif, or lazily computed)

    def set_palette(self, flat_rgb_768):
        flat_rgb_768 = list(flat_rgb_768)
        if len(flat_rgb_768) != EIF_MULTICOLOR_NUM_COLORS * 3:
            raise EifError("Invalid palette: expected exactly 256 RGB entries.")
        self.palette = flat_rgb_768
        self._indices = None  # must be recomputed against the new palette

    def save_palette(self, file_path):
        if not self.palette:
            raise EifError("No palette has been set for this image.")
        flat = bytearray()
        for i in range(EIF_MULTICOLOR_NUM_COLORS):
            r, g, b = self.palette[i * 3:i * 3 + 3]
            flat += bytes((r, g, b, 0))
        write_file(file_path, bytes(flat))

    def _ensure_quantized(self):
        if self._indices is not None:
            return
        if self.palette is None:
            self._indices, self.palette = _quantize_rgba_self(self.rgba, self.width, self.height)
        else:
            self._indices = _quantize_rgba_to_palette(self.rgba, self.width, self.height, self.palette)

    def open_eif(self, data: bytes):
        if len(data) < _BASE_HEADER_SIZE:
            raise EifError("Not a valid EIF file: too short to contain a header.")
        signature, eif_type, length, width, height = struct.unpack_from(_BASE_HEADER_FMT, data, 0)
        if signature != EIF_SIGNATURE:
            raise EifError("Not a valid EIF file: signature does not match.")
        if eif_type != EIF_TYPE_MULTICOLOR:
            raise EifError("Not a valid EIF file: unexpected image type for this format.")

        data_offset = _BASE_HEADER_SIZE + EIF_MULTICOLOR_PALETTE_SIZE
        if length > (len(data) - data_offset):
            raise EifError("Not a valid EIF file: declared data length exceeds the available data.")
        if height == 0:
            raise EifError("Not a valid EIF file: height is zero.")
        if (length // height) % 2:
            raise EifError("Not a valid EIF file: row width is not properly aligned.")

        if self.palette is None:
            pal_bytes = data[_BASE_HEADER_SIZE:_BASE_HEADER_SIZE + EIF_MULTICOLOR_PALETTE_SIZE]
            self.palette = list(pal_bytes)

        num_pixels = (len(data) - data_offset) // 2
        rgba = bytearray(num_pixels * 4)
        indices = bytearray(num_pixels)
        for i in range(num_pixels):
            idx = data[data_offset + i * 2]
            alpha = data[data_offset + i * 2 + 1]
            r, g, b = self.palette[idx * 3:idx * 3 + 3]
            rgba[i * 4:i * 4 + 4] = bytes((r, g, b, alpha))
            indices[i] = idx

        self.width = width
        self.height = height
        self.rgba = bytes(rgba)
        self._indices = bytes(indices)

    def _store_palette(self) -> bytes:
        self._ensure_quantized()
        flat = bytearray()
        for i in range(EIF_MULTICOLOR_NUM_COLORS):
            flat += bytes(self.palette[i * 3:i * 3 + 3])
        return bytes(flat)

    def _store_bitmap(self) -> bytes:
        self._ensure_quantized()
        num_pixels = self.width * self.height
        out = bytearray(num_pixels * 2)
        out[0::2] = self._indices
        out[1::2] = self.rgba[3::4]  # original per-pixel alpha
        return bytes(out)


def make_eif(eif_type: int) -> EifImageBase:
    if eif_type == EIF_TYPE_MONOCHROME:
        return EifImage8bit()
    if eif_type == EIF_TYPE_MULTICOLOR:
        return EifImage16bit()
    if eif_type == EIF_TYPE_SUPERCOLOR:
        return EifImage32bit()
    raise EifError("Unsupported EIF image type.")


def eif_to_bmp_file(eif_data: bytes, out_bmp_path, palette_path=None, store_palette=False):
    if len(eif_data) < 8:
        raise EifError("Not a valid EIF file: not enough data for a header.")
    image = make_eif(eif_data[7])

    if palette_path and image.eif_type == EIF_TYPE_MULTICOLOR:
        pal_bytes = read_file(palette_path)
        # palette file format: 256 * (R,G,B,unused) = 1024 bytes
        flat_rgb = []
        for i in range(EIF_MULTICOLOR_NUM_COLORS):
            flat_rgb.extend(pal_bytes[i * 4:i * 4 + 3])
        image.set_palette(flat_rgb)

    image.open_eif(eif_data)

    bit_depth = 24 if image.eif_type == EIF_TYPE_MONOCHROME else 32
    image.save_bmp(out_bmp_path, bit_depth=bit_depth)

    if image.eif_type == EIF_TYPE_MULTICOLOR and store_palette:
        pal_path = Path(out_bmp_path).with_suffix(".pal")
        image.save_palette(pal_path)


def bmp_file_to_eif_file(bmp_path, depth: int, out_eif_path, palette_path=None):
    eif_type = depth_to_eif_type(depth)
    image = make_eif(eif_type)

    if palette_path:
        pal_bytes = read_file(palette_path)
        flat_rgb = []
        for i in range(EIF_MULTICOLOR_NUM_COLORS):
            flat_rgb.extend(pal_bytes[i * 4:i * 4 + 3])
        image.set_palette(flat_rgb)

    image.open_bmp(bmp_path)
    image.save_eif(out_eif_path)


def map_multi_palette(images):
    """Given a list of EifImage16bit instances, compute ONE shared 256-colour
    palette from all of their pixels combined, and assign it to every image
    (replacing any palette they already had)."""
    if not images:
        return

    total_pixels = sum(img.width * img.height for img in images)
    # Build one combined "training" image (1 pixel tall) containing every
    # source pixel, then quantize that to get a single shared palette.
    combined = bytearray()
    for img in images:
        rgba = img.rgba
        r = rgba[0::4]
        g = rgba[1::4]
        b = rgba[2::4]
        interleaved = bytearray(len(r) * 3)
        interleaved[0::3] = r
        interleaved[1::3] = g
        interleaved[2::3] = b
        combined += interleaved

    combined_img = Image.frombytes("RGB", (total_pixels, 1), bytes(combined))
    quantized = combined_img.quantize(colors=EIF_MULTICOLOR_NUM_COLORS, dither=Image.Dither.NONE)
    flat_rgb = _pad_palette_to_256(quantized.getpalette())

    for img in images:
        img.set_palette(flat_rgb)


def bulk_pack(bmp_dir, out_dir):
    """Convert every .bmp under bmp_dir (recursively) to a MULTICOLOR .eif in
    out_dir, all sharing one combined palette."""
    bmp_dir = Path(bmp_dir)
    out_dir = Path(out_dir)
    bmp_files = sorted(p for p in bmp_dir.rglob("*.bmp") if p.is_file())

    images = []
    for p in bmp_files:
        img = EifImage16bit()
        img.open_bmp(p)
        images.append(img)

    map_multi_palette(images)

    out_dir.mkdir(parents=True, exist_ok=True)
    for p, img in zip(bmp_files, images):
        img.save_eif(out_dir / (p.stem + ".eif"))
