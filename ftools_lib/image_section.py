"""
ImageSection: parses/builds the raw "image section" binary blob (VBF
section index 1 in Ford IPC firmware) - a packed collection of UI element
header records plus zip-compressed EIF images and raw TTF font files.
Equivalent to ImgSectionParser/ImageSection.cpp/.h.

Binary layout (all multi-byte fields are native/little-endian, no struct
padding):

    [u32 records_count][HeaderRecord x records_count]
    [u32 zip_count][zip_file x zip_count]
    [u32 ttf_count][ttf_file x ttf_count]
    [u32 unknown_int]
    [zip item data, each item padded up to a 4-byte boundary]
    [ttf item data, each item padded up to a 4-byte boundary]

Item data is NOT located by sequential offset into this blob. Instead each
zip_file/ttf_file header's `fileName` is a string of the form
`~mem/0xAAAAAAAA-          NNNN.ext`, where AAAAAAAA is the item's absolute
address in the firmware's memory map and NNNN is its exact (unpadded) byte
size. The real offset into this blob is (address - 0x02400000).
"""

import csv
import json
import re
import struct
from pathlib import Path

from .utils import read_file, write_file

BASE_MEM_ADDR = 0x02400000

EIF_TYPE_MONOCHROME = 0x04
EIF_TYPE_MULTICOLOR = 0x07
EIF_TYPE_SUPERCOLOR = 0x0E
EIF_TYPE_UNKNOWN = 0x00

_TYPE_TO_STR = {
    EIF_TYPE_MONOCHROME: "MONOCHROME",
    EIF_TYPE_MULTICOLOR: "MULTICOLOR",
    EIF_TYPE_SUPERCOLOR: "SUPERCOLOR",
}
_STR_TO_TYPE = {v: k for k, v in _TYPE_TO_STR.items()}


def image_type_to_string(t: int) -> str:
    return _TYPE_TO_STR.get(t, "[Unknown] ")


def image_type_from_string(s: str) -> int:
    return _STR_TO_TYPE.get(s, EIF_TYPE_UNKNOWN)


_HEADER_RECORD_FMT = "<4I8B"
_HEADER_RECORD_SIZE = struct.calcsize(_HEADER_RECORD_FMT)
assert _HEADER_RECORD_SIZE == 24

_ZIP_FILE_FMT = "<2IB31s"
_ZIP_FILE_SIZE = struct.calcsize(_ZIP_FILE_FMT)
assert _ZIP_FILE_SIZE == 40

_TTF_FILE_FMT = "<32s"
_TTF_FILE_SIZE = struct.calcsize(_TTF_FILE_FMT)
assert _TTF_FILE_SIZE == 32

_CSV_FIELDS = ["Width", "Height", "X", "Y", "Type", "Z-index", "Intensity", "R", "G", "B", "Palette"]


class ImageSectionError(RuntimeError):
    pass


class HeaderRecord:
    __slots__ = ("width", "height", "X", "Y", "type", "Z", "intensity", "R", "G", "B", "palette_id", "zero")

    def __init__(self, width=0, height=0, X=0, Y=0, type=0, Z=0,
                 intensity=0, R=0, G=0, B=0, palette_id=0, zero=0):
        self.width = width
        self.height = height
        self.X = X
        self.Y = Y
        self.type = type
        self.Z = Z
        self.intensity = intensity
        self.R = R
        self.G = G
        self.B = B
        self.palette_id = palette_id
        self.zero = zero  # always 0; never exported to or imported from CSV

    def pack(self) -> bytes:
        return struct.pack(
            _HEADER_RECORD_FMT,
            self.width, self.height, self.X, self.Y,
            self.type, self.Z, self.intensity, self.R, self.G, self.B,
            self.palette_id, self.zero,
        )

    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> "HeaderRecord":
        vals = struct.unpack_from(_HEADER_RECORD_FMT, data, offset)
        return cls(*vals)


class Item:
    __slots__ = ("res_type", "width", "height", "img_type", "data")

    def __init__(self, res_type, width=0, height=0, img_type=0, data=b""):
        self.res_type = res_type  # "zip" or "ttf"
        self.width = width        # zip items only
        self.height = height      # zip items only
        self.img_type = img_type  # zip items only
        self.data = data


def _decode_c_string(buf: bytes) -> str:
    nul = buf.find(b"\x00")
    if nul != -1:
        buf = buf[:nul]
    return buf.decode("latin-1")


def _parse_addr_from_filename(file_name: str) -> int:
    """Mirrors `stoul(&fileName[5], nullptr, 16)`: skip the 5-char "~mem/"
    prefix, optionally skip a "0x"/"0X" prefix, then parse hex digits up to
    the first non-hex character. Deliberately does not assume any fixed
    field width, matching std::stoul's own behaviour."""
    s = file_name[5:]
    if s[:2] in ("0x", "0X"):
        s = s[2:]
    hexdigits = ""
    for ch in s:
        if ch in "0123456789abcdefABCDEF":
            hexdigits += ch
        else:
            break
    if not hexdigits:
        raise ImageSectionError(f"Resource entry name does not contain a valid memory address: {file_name!r}")
    return int(hexdigits, 16)


_RE_SIZE_BEFORE_DOT = re.compile(r"(\d+)\.")


def _get_item_data(bin_data: bytes, file_name: str) -> bytes:
    m = _RE_SIZE_BEFORE_DOT.search(file_name)
    if not m:
        raise ImageSectionError(f"Resource entry name does not contain a valid size: {file_name!r}")
    actual_sz = int(m.group(1))
    addr = _parse_addr_from_filename(file_name)
    relative_offset = addr - BASE_MEM_ADDR
    # Python slicing silently wraps negative indices and clamps out-of-range
    # ones instead of erroring (unlike a C++ pointer/array access, which
    # this mirrors), so an out-of-bounds offset here must be checked
    # explicitly or it would silently return wrong/truncated data instead
    # of failing.
    if relative_offset < 0 or relative_offset + actual_sz > len(bin_data):
        raise ImageSectionError(
            f"item data out of bounds for {file_name!r}: "
            f"offset {relative_offset}, size {actual_sz}, buffer is {len(bin_data)} bytes"
        )
    return bin_data[relative_offset:relative_offset + actual_sz]


class ImageSection:
    def __init__(self):
        self.header_data = []  # list[HeaderRecord]
        self.zip_items = []    # list[Item]
        self.ttf_items = []    # list[Item]
        self.unknown_int = 0

    # ---------------------------------------------------------------- #
    def parse(self, bin_data: bytes):
        total_len = len(bin_data)

        def need(offset, size, what):
            if offset + size > total_len:
                raise ImageSectionError(
                    f"Image section data too short while reading {what}: "
                    f"need {offset + size} bytes, got {total_len} "
                    f"(this usually means the input isn't a valid image-section "
                    f"binary blob -- e.g. the wrong VBF section index was used)"
                )

        need(0, 4, "records_count")
        records_count = struct.unpack_from("<I", bin_data, 0)[0]
        read_idx = 4

        need(read_idx, _HEADER_RECORD_SIZE * records_count, "header records")
        self.header_data = [
            HeaderRecord.unpack(bin_data, read_idx + i * _HEADER_RECORD_SIZE)
            for i in range(records_count)
        ]
        read_idx += _HEADER_RECORD_SIZE * records_count

        # `records_count` is read directly from the input as a raw 32-bit
        # value, so data that isn't actually an image-section blob can
        # still produce a small, in-bounds count purely by chance and
        # pass the check above while being complete garbage. width/
        # height/X/Y are themselves full 32-bit fields, and every real
        # IPC display this format targets is far smaller than 65536
        # pixels in any dimension, so a value at or above that is an
        # extremely reliable (and otherwise impossible) sign that this
        # buffer isn't really an image section.
        _MAX_PLAUSIBLE_DIMENSION = 65536
        for rec in self.header_data:
            if max(rec.width, rec.height, rec.X, rec.Y) >= _MAX_PLAUSIBLE_DIMENSION:
                raise ImageSectionError(
                    "This doesn't look like a valid image-section binary blob: a header "
                    f"record has an implausible dimension (width={rec.width}, height={rec.height}, "
                    f"X={rec.X}, Y={rec.Y}). This usually means the wrong VBF section index was used, "
                    "or this firmware part doesn't contain image-section data at all."
                )

        need(read_idx, 4, "zip_count")
        zip_count = struct.unpack_from("<I", bin_data, read_idx)[0]
        read_idx += 4
        zip_headers_offset = read_idx
        need(read_idx, _ZIP_FILE_SIZE * zip_count, "zip file headers")
        read_idx += _ZIP_FILE_SIZE * zip_count

        need(read_idx, 4, "ttf_count")
        ttf_count = struct.unpack_from("<I", bin_data, read_idx)[0]
        read_idx += 4
        ttf_headers_offset = read_idx
        need(read_idx, _TTF_FILE_SIZE * ttf_count, "ttf file headers")
        read_idx += _TTF_FILE_SIZE * ttf_count

        need(read_idx, 4, "unknown_int")
        self.unknown_int = struct.unpack_from("<I", bin_data, read_idx)[0]

        self.zip_items = []
        for i in range(zip_count):
            off = zip_headers_offset + i * _ZIP_FILE_SIZE
            width, height, img_type, name_buf = struct.unpack_from(_ZIP_FILE_FMT, bin_data, off)
            data = _get_item_data(bin_data, _decode_c_string(name_buf))
            self.zip_items.append(Item("zip", width, height, img_type, data))

        self.ttf_items = []
        for i in range(ttf_count):
            off = ttf_headers_offset + i * _TTF_FILE_SIZE
            (name_buf,) = struct.unpack_from(_TTF_FILE_FMT, bin_data, off)
            data = _get_item_data(bin_data, _decode_c_string(name_buf))
            self.ttf_items.append(Item("ttf", data=data))

    def parse_file(self, file_path):
        self.parse(read_file(file_path))

    # ---------------------------------------------------------------- #
    @staticmethod
    def header_to_csv(header_data, csv_file_path):
        with open(csv_file_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(_CSV_FIELDS)
            for hr in header_data:
                w.writerow([hr.width, hr.height, hr.X, hr.Y, hr.type, hr.Z,
                            hr.intensity, hr.R, hr.G, hr.B, hr.palette_id])

    @staticmethod
    def header_from_csv(csv_file_path):
        header_data = []
        try:
            with open(csv_file_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    header_data.append(HeaderRecord(
                        width=int(row["Width"]), height=int(row["Height"]),
                        X=int(row["X"]), Y=int(row["Y"]),
                        type=int(row["Type"]), Z=int(row["Z-index"]),
                        intensity=int(row["Intensity"]),
                        R=int(row["R"]), G=int(row["G"]), B=int(row["B"]),
                        palette_id=int(row["Palette"]),
                    ))
        except Exception as e:
            raise ImageSectionError(f"Could not read header CSV '{csv_file_path}': {e}")
        return header_data

    # ---------------------------------------------------------------- #
    def export(self, out_path, name_prefix=""):
        out_path = Path(out_path)
        (out_path / "zip").mkdir(parents=True, exist_ok=True)
        (out_path / "ttf").mkdir(parents=True, exist_ok=True)

        header_csv_name = f"{name_prefix}_header.csv"
        # NOTE: the original C++ tool writes this CSV relative to the
        # process's current working directory (a path-join bug: unlike the
        # JSON config below, it never joins this with out_path), so a run
        # from a different cwd leaves the header file somewhere other than
        # where the JSON config expects it. Fixed here to write it inside
        # out_path, alongside everything else - this affects only where an
        # intermediate working file lands on disk, never the binary
        # firmware format itself.
        self.header_to_csv(self.header_data, out_path / header_csv_name)

        def save_resources(item_prefix, items, with_image_fields):
            section_json = []
            for i, item in enumerate(items):
                file_name = f"{i}.{item_prefix}"
                write_file(out_path / item_prefix / file_name, item.data)
                entry = {"file": f"{item_prefix}/{file_name}"}
                if with_image_fields:
                    entry["width"] = item.width
                    entry["height"] = item.height
                    entry["type"] = image_type_to_string(item.img_type)
                section_json.append(entry)
            return section_json

        img_sections = save_resources("zip", self.zip_items, True)
        ttf_sections = save_resources("ttf", self.ttf_items, False)

        config = {
            "header": header_csv_name,
            "unknown-int": self.unknown_int,
            "image-sections": img_sections,
            "ttf-sections": ttf_sections,
        }
        with open(out_path / f"{name_prefix}_config.json", "w") as f:
            json.dump(config, f, indent=4)
        return 0

    def import_config(self, config_path):
        config_path = Path(config_path)
        config_dir = config_path.parent

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                document = json.load(f)
        except OSError as e:
            raise ImageSectionError(f"Could not open config file '{config_path}': {e}")
        except json.JSONDecodeError as e:
            raise ImageSectionError(f"Config file '{config_path}' is not valid JSON: {e}")

        required = ("header", "image-sections", "ttf-sections", "unknown-int")
        if not all(k in document for k in required):
            raise ImageSectionError(
            "Config file is missing one or more required fields "
            '("header", "image-sections", "ttf-sections", "unknown-int").'
        )

        self.unknown_int = int(document["unknown-int"])
        self.header_data = self.header_from_csv(config_dir / document["header"])

        def get_section_files(section_name, is_zip):
            items = []
            for sec_obj in document[section_name]:
                data = read_file(config_dir / sec_obj["file"])
                if "width" in sec_obj:
                    items.append(Item("zip", int(sec_obj["width"]), int(sec_obj["height"]),
                                       image_type_from_string(sec_obj["type"]), data))
                else:
                    items.append(Item("ttf", data=data))
            return items

        self.zip_items = get_section_files("image-sections", True)
        self.ttf_items = get_section_files("ttf-sections", False)
        return 0

    # ---------------------------------------------------------------- #
    def save_to_vector(self) -> bytes:
        out = bytearray()

        records_count = len(self.header_data)
        out += struct.pack("<I", records_count)
        for hr in self.header_data:
            out += hr.pack()
        head_in_bytes = _HEADER_RECORD_SIZE * records_count

        zip_count = len(self.zip_items)
        ttf_count = len(self.ttf_items)

        data_offset = BASE_MEM_ADDR + head_in_bytes + 4
        data_offset += zip_count * _ZIP_FILE_SIZE + 4
        data_offset += ttf_count * _TTF_FILE_SIZE + 4
        data_offset += 4  # magic/unknown int

        def make_name(ext, actual_sz):
            return f"~mem/0x{data_offset:08X}-{actual_sz:10d}.{ext}"

        out += struct.pack("<I", zip_count)
        for item in self.zip_items:
            actual_sz = len(item.data)
            remainder = actual_sz % 4
            padded_sz = actual_sz + ((4 - remainder) if remainder else 0)
            name_bytes = (make_name("zip", actual_sz).encode("latin-1") + b"\x00").ljust(31, b"\x00")[:31]
            out += struct.pack(_ZIP_FILE_FMT, item.width, item.height, item.img_type, name_bytes)
            data_offset += padded_sz

        out += struct.pack("<I", ttf_count)
        for item in self.ttf_items:
            actual_sz = len(item.data)
            remainder = actual_sz % 4
            padded_sz = actual_sz + ((4 - remainder) if remainder else 0)
            name_bytes = (make_name("ttf", actual_sz).encode("latin-1") + b"\x00").ljust(32, b"\x00")[:32]
            out += struct.pack(_TTF_FILE_FMT, name_bytes)
            data_offset += padded_sz

        out += struct.pack("<I", self.unknown_int)

        def pack_items(items):
            for item in items:
                data = item.data
                out.extend(data)
                remainder = len(data) % 4
                if remainder:
                    out.extend(b"\x00" * (4 - remainder))

        pack_items(self.zip_items)
        pack_items(self.ttf_items)

        return bytes(out)

    def save_to_file(self, out_path):
        write_file(out_path, self.save_to_vector())
        return 0

    # ---------------------------------------------------------------- #
    def get_items_count(self, res_type: str) -> int:
        if res_type == "zip":
            return len(self.zip_items)
        if res_type == "ttf":
            return len(self.ttf_items)
        return 0

    def get_item_data(self, res_type: str, idx: int) -> bytes:
        items = self._items_for(res_type)
        if idx < 0 or idx >= len(items):
            raise ImageSectionError(
                f"Invalid {res_type} item index {idx}: this image section has {len(items)} {res_type} item(s)."
            )
        return items[idx].data

    def replace_item(self, res_type: str, idx: int, data: bytes, w=0, h=0, t=0):
        items = self._items_for(res_type)
        if idx < 0 or idx >= len(items):
            raise ImageSectionError(
                f"Invalid {res_type} item index {idx}: this image section has {len(items)} {res_type} item(s)."
            )
        item = items[idx]
        item.data = data
        if res_type == "zip":
            item.width = w
            item.height = h
            item.img_type = t
        return 0

    def _items_for(self, res_type: str) -> list:
        if res_type == "zip":
            return self.zip_items
        if res_type == "ttf":
            return self.ttf_items
        raise ImageSectionError(f"Unknown resource type {res_type!r}: expected \"zip\" or \"ttf\".")
