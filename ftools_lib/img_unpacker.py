"""
Python port of ImgUnpacker/main.cpp ("imgunpkr").

Combines VbfFile + ImageSection + EIF conversion + in-memory single-file ZIP
handling to unpack/repack the resource images stored in VBF section 1.

Unpack flow (UnpackImg):
    1. Open the VBF, pull section 1's raw bytes, parse as an ImageSection.
    2. Dump the per-resource header records to header_lines.csv.
    3. For every "zip" item: unzip the single embedded EIF, save it under
       eif/<name>, convert it to a BMP under bmp/<name>.bmp, and add a row
       to export_list.csv (Idx, Name, Depth, palette_crc16, Width, Height).
       palette_crc16 is the CRC16-CCITT/FALSE of the 256-colour (768 byte)
       palette block for MULTICOLOR images, "0000"-style 4 hex digit
       otherwise literal "0".
    4. For every "ttf" item: dump it as <idx>.ttf with an all-zero CSV row.
    5. Create an empty custom/ directory for the user to drop replacement
       .bmp/.ttf files into before repacking.

Repack flow (PackImg / RepackResources):
    Walks custom/, matching each file there against export_list.csv by
    name (.bmp files are matched as if they were named .eif, since that's
    what the CSV's Name column holds for image resources). .ttf files
    replace the TTF item directly. .bmp files either:
      - get converted standalone and zip-compressed, or
      - if their CSV row says depth==16 (MULTICOLOR) and has a nonzero
        palette_crc16, get grouped with every other resource that originally
        shared that exact palette (pulling the *original* EIF for any
        sibling that wasn't itself replaced) so a single shared 256-colour
        palette can be (re)computed across the whole group -- matching the
        original tool's behavior for resources that must keep a common
        palette.
    The patched ImageSection is then re-serialized and written back into
    VBF section 1, and the whole VBF is saved to <out_path>/patched.vbf.
"""

from __future__ import annotations

import csv
import io
import struct
import zipfile
from pathlib import Path

from . import eif as eifmod
from .crc import crc16_ccitt_false
from .image_section import ImageSection
from .utils import read_file, write_file
from .vbf import VbfFile

ITEM_IDX = "Idx"
ITEM_NAME = "Name"
ITEM_WIDTH = "Width"
ITEM_HEIGHT = "Height"
ITEM_TYPE = "Depth"
ITEM_PALETTE_CRC = "palette_crc16"
CUSTOM_DIR = "custom"

_EIF_HEADER_FMT = "<7sBIHH"  # signature, type, length, width, height (same as eif.py)


class ImgUnpackerError(RuntimeError):
    pass


# --------------------------------------------------------------------- #
# CSV row model (port of the C++ `csv_row` struct + helpers)
# --------------------------------------------------------------------- #

class CsvRow:
    __slots__ = ("idx", "type", "crc", "name")

    def __init__(self, idx=0, type=0, crc=0, name=""):
        self.idx = idx
        self.type = type
        self.crc = crc
        self.name = name


def read_csv(config_path) -> list:
    """Port of ReadCSV: reads Idx,Name,Depth,palette_crc16 columns (ignoring
    any extra columns like Width/Height)."""
    rows = []
    with open(config_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(CsvRow(
                idx=int(r[ITEM_IDX]),
                type=int(r[ITEM_TYPE]),
                crc=int(r[ITEM_PALETTE_CRC], 16),
                name=r[ITEM_NAME],
            ))
    return rows


def get_res_csv_data(csv_rows, res_name: str):
    """Port of GetResCsvData: returns the LAST row matching res_name (the
    original iterates the whole list and keeps overwriting), or None if no
    row matches (the original's empty-name sentinel)."""
    found = None
    for row in csv_rows:
        if row.name == res_name:
            found = row
    return found


def get_name_from_idx(csv_rows, idx: int) -> str:
    for row in csv_rows:
        if row.idx == idx and row.type:
            return row.name
    raise ImgUnpackerError(f"No resource found with index {idx}.")


def get_res_with_same_palette(csv_rows, crc: int) -> list:
    return [row.idx for row in csv_rows if row.crc == crc]


# --------------------------------------------------------------------- #
# Single-file in-memory ZIP helpers (port of GetEIFfromImgSection /
# compressVector, using Python's stdlib zipfile instead of miniz)
# --------------------------------------------------------------------- #

def get_eif_from_img_section(img_sec: ImageSection, idx: int):
    """Returns (eif_bytes, eif_name) by unzipping the single-file zip stored
    as the zip item's raw data."""
    zip_bin = img_sec.get_item_data("zip", idx)
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bin)) as zf:
            names = zf.namelist()
            if not names:
                raise ImgUnpackerError("Could not read the embedded image's filename: the zip archive is empty.")
            name = names[0]
            data = zf.read(name)
    except zipfile.BadZipFile as e:
        raise ImgUnpackerError(f"Could not read the embedded zip archive: {e}")
    return data, name


def compress_vector(data: bytes, data_name: str) -> bytes:
    """Port of compressVector: writes a single-entry, ASCII-filename,
    DEFLATE-compressed zip to an in-memory buffer (matches miniz's
    mz_zip_writer_add_mem_ex with MZ_DEFAULT_LEVEL | MZ_ZIP_FLAG_ASCII_FILENAME,
    no streaming data-descriptor since we write to a seekable buffer)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zi = zipfile.ZipInfo(data_name)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.create_system = 0
        zi.external_attr = 0x20  # DOS FILE_ATTRIBUTE_ARCHIVE, matches miniz default
        zf.writestr(zi, data)
    return buf.getvalue()


def compress_and_replace_eif(img_sec: ImageSection, idx: int, res_bin: bytes, res_name: str) -> None:
    """Port of CompressAndReplaceEIF."""
    if len(res_bin) < struct.calcsize(_EIF_HEADER_FMT):
        raise ImgUnpackerError(f"'{res_name}': not enough data for a valid EIF header.")
    _sig, eif_type, _length, width, height = struct.unpack_from(_EIF_HEADER_FMT, res_bin, 0)

    zip_bin = compress_vector(res_bin, res_name)
    img_sec.replace_item("zip", idx, zip_bin, width, height, eif_type)
    print(f"Replaced image '{res_name}'.")


# --------------------------------------------------------------------- #
# UnpackImg
# --------------------------------------------------------------------- #

def unpack_img(in_path, out_path) -> None:
    in_path = Path(in_path)
    out_path = Path(out_path)

    vbf = VbfFile()
    vbf.open_file(in_path)

    img_sec_bin = vbf.get_section_raw(1)

    img_sec = ImageSection()
    img_sec.parse(img_sec_bin)
    ImageSection.header_to_csv(img_sec.header_data, out_path / "header_lines.csv")

    eifs_path = out_path / "eif"
    bmps_path = out_path / "bmp"
    eifs_path.mkdir(parents=True, exist_ok=True)
    bmps_path.mkdir(parents=True, exist_ok=True)
    (out_path / CUSTOM_DIR).mkdir(parents=True, exist_ok=True)

    export_list_path = out_path / "export_list.csv"
    with open(export_list_path, "w", newline="") as f:
        f.write(f"{ITEM_IDX},{ITEM_NAME},{ITEM_TYPE},{ITEM_PALETTE_CRC},{ITEM_WIDTH},{ITEM_HEIGHT}\n")

        zip_items = img_sec.get_items_count("zip")
        for i in range(zip_items):
            eif_data, eif_name = get_eif_from_img_section(img_sec, i)

            write_file(eifs_path / eif_name, eif_data)

            bmp_path = (bmps_path / eif_name).with_suffix(".bmp")
            eifmod.eif_to_bmp_file(eif_data, bmp_path)

            _sig, eif_type, _length, width, height = struct.unpack_from(_EIF_HEADER_FMT, eif_data, 0)

            crc_str = "0"
            if eif_type == eifmod.EIF_TYPE_MULTICOLOR:
                palette = eif_data[16:16 + 768]
                crc16 = crc16_ccitt_false(palette)
                crc_str = f"{crc16:04X}"

            depth = _to_color_depth(eif_type)
            f.write(f"{i},{eif_name},{depth},{crc_str},{width},{height}\n")

        ttf_path = out_path / "ttf"
        ttf_path.mkdir(parents=True, exist_ok=True)
        ttf_items = img_sec.get_items_count("ttf")
        for i in range(ttf_items):
            ttf_name = f"{i}.ttf"
            item_bin = img_sec.get_item_data("ttf", i)
            write_file(ttf_path / ttf_name, item_bin)
            f.write(f"{i},{ttf_name},0,0,0,0\n")


def _to_color_depth(eif_type: int) -> int:
    """Port of ImageSection.h's ToColorDepth (same mapping as eif_type_to_depth)."""
    return eifmod.eif_type_to_depth(eif_type)


# --------------------------------------------------------------------- #
# PackImg / RepackResources
# --------------------------------------------------------------------- #

def repack_resources(config_path, img_sec: ImageSection, csv_rows: list) -> None:
    config_path = Path(config_path)
    custom_dir = config_path.parent / CUSTOM_DIR

    eif16_map = {}  # crc -> {"eif_idx": [...], "eifs": [EifImage16bit, ...]}

    for res_path in sorted(custom_dir.iterdir()):
        if not res_path.is_file():
            continue

        res_name = res_path.name
        ext = res_path.suffix.lower()

        if ext == ".bmp":
            res_name = res_path.stem + ".eif"

        res_csv_data = get_res_csv_data(csv_rows, res_name)
        if res_csv_data is None:
            print(f"Skipping '{res_name}': no matching entry found in the resource list.")
            continue

        if ext == ".ttf":
            res_bin = read_file(res_path)
            img_sec.replace_item("ttf", res_csv_data.idx, res_bin)
            print(f"Replaced resource '{res_name}'.")

        if ext == ".bmp":
            if res_csv_data.type == 16 and res_csv_data.crc:
                group = eif16_map.setdefault(res_csv_data.crc, {"eif_idx": [], "eifs": []})
                eif_img = eifmod.EifImage16bit()
                eif_img.open_bmp(res_path)
                group["eif_idx"].append(res_csv_data.idx)
                group["eifs"].append(eif_img)
            else:
                eif_img = eifmod.make_eif(eifmod.depth_to_eif_type(res_csv_data.type))
                eif_img.open_bmp(res_path)
                res_bin = eif_img.save_eif_to_bytes()
                compress_and_replace_eif(img_sec, res_csv_data.idx, res_bin, res_name)

        # NOTE: direct .eif replacement is a TODO in the original tool too,
        # and is intentionally not implemented here either.

    for crc, group in eif16_map.items():
        eif_idx = group["eif_idx"]
        eifs = group["eifs"]

        for idx in get_res_with_same_palette(csv_rows, crc):
            if idx not in eif_idx:
                eif_bin, _name = get_eif_from_img_section(img_sec, idx)
                eif_img = eifmod.EifImage16bit()
                eif_img.open_eif(eif_bin)
                eifs.append(eif_img)
                eif_idx.append(idx)

        eifmod.map_multi_palette(eifs)

        for i in range(len(eif_idx)):
            res_bin = eifs[i].save_eif_to_bytes()
            compress_and_replace_eif(img_sec, eif_idx[i], res_bin, get_name_from_idx(csv_rows, eif_idx[i]))


def pack_img(config_path, vbf_path, out_path) -> None:
    config_path = Path(config_path)
    vbf_path = Path(vbf_path)
    out_path = Path(out_path)

    vbf = VbfFile()
    vbf.open_file(vbf_path)

    img_sec_bin = vbf.get_section_raw(1)

    img_sec = ImageSection()
    img_sec.parse(img_sec_bin)
    img_sec.header_data = ImageSection.header_from_csv(config_path.parent / "header_lines.csv")

    csv_rows = read_csv(config_path)

    repack_resources(config_path, img_sec, csv_rows)

    new_img_sec_bin = img_sec.save_to_vector()

    vbf.replace_section_raw(1, new_img_sec_bin)
    vbf.save_to_file(out_path / "patched.vbf")
