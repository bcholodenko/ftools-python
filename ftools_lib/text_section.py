"""
Python port of TextSectionPacker.cpp/h (FTools-5.0, TextSectionParser tool "textparser").

Parses/builds the Ford IPC VBF "text section" binary blob (the `mk3_5` UI
text/alerts layout). Faithfully replicates the original C++ behavior,
including its quirks:

  * The original source also defines a `TextSection_mk3` struct layout, but
    it is dead code -- only `TextSection_mk3_5` is ever used (reinterpret_cast
    target) by unpack()/pack(). It is therefore not implemented here.
  * `ui_text_pack_ex` is part of the mk3_5 struct layout (it affects the byte
    offsets of the fields that follow it) but its *contents* are never read
    or written by either unpack() or pack() in the original tool. This is
    preserved as-is (not "fixed") since fixing it would change which bytes
    of the file are touched by pack(), and we don't know what (if anything)
    legitimately lives there.
  * unpack() in the original only ever writes `ui_texts.csv` and
    `ui_alerts.csv` -- code to dump header.bin/unk.bin/unk2.bin/eif.bin
    exists in the mk3-era version but is commented out / unused in the
    mk3_5 path. We only emit the two CSVs, matching actual behavior.
  * Alerts CSV "line_id" column is the *stored* `idx` field of each alert
    record, not its position within the array. pack() then uses that same
    CSV value both as the new `idx` field AND as the array index to write
    back into -- i.e. it assumes idx == position. This is preserved exactly.
  * The byte-escaping scheme is a direct port of the C++ `Escape`/`Unescape`
    helpers (C-style backslash escapes for \\0 \\a \\b \\f \\n \\r \\t \\v
    \\\\ \\' \\" and, unusually, also literal '?' -> "\\?"). Unrecognized
    escape sequences after a backslash are silently dropped (matching the
    original's `default: cerr << ...; continue;` which never appends
    anything for unknown sequences).
  * CSV rows are written/read using a small hand-rolled format (not Python's
    `csv` module) because the original's escaping scheme already protects
    commas and quotes within content; layering "real" CSV quote-doubling on
    top of that backslash-escaped text would not match the original
    fast-cpp-csv-parser semantics, which simply locates the field between
    the first and last double-quote character of the row.

Field bytes can hold arbitrary byte values (0-255), not necessarily valid
UTF-8 text. To preserve them losslessly through a text CSV file we use the
'latin-1' codec everywhere, which maps byte values 0-255 to/from Unicode
code points 0-255 one-to-one with no transformation.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from . import utils


class TextSectionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# TextSection_mk3_5 layout (#pragma pack(1), no padding)
# ---------------------------------------------------------------------------

HEADER_SIZE = 0x1D680

UI_NUM_LANGS = 18
UI_LINES_PER_LANG = 0x438
UI_LINE_SIZE = 0x28
UI_PACK_SIZE = UI_NUM_LANGS * UI_LINES_PER_LANG * UI_LINE_SIZE

# ui_text_pack_ex: single TextUI_ex instance (not an array) -- present only
# to occupy space; never read or written by unpack()/pack() in the original.
UI_EX_LINES = 0x438
UI_EX_LINE_SIZE = 0x56
UI_PACK_EX_SIZE = UI_EX_LINES * UI_EX_LINE_SIZE

UNK_SIZE = 0x37EC

ALERT_NUM_LANGS = 19
ALERT_LINES_PER_LANG = 0xF6
ALERT_IDX_SIZE = 2  # uint16_t idx
ALERT_LINE_SIZE = 0xC6
ALERT_RECORD_SIZE = ALERT_IDX_SIZE + ALERT_LINE_SIZE
ALERT_PACK_SIZE = ALERT_NUM_LANGS * ALERT_LINES_PER_LANG * ALERT_RECORD_SIZE

HEADER_OFFSET = 0
UI_PACK_OFFSET = HEADER_OFFSET + HEADER_SIZE
UI_PACK_EX_OFFSET = UI_PACK_OFFSET + UI_PACK_SIZE
UNK_OFFSET = UI_PACK_EX_OFFSET + UI_PACK_EX_SIZE
ALERT_PACK_OFFSET = UNK_OFFSET + UNK_SIZE
STRUCT_SIZE = ALERT_PACK_OFFSET + ALERT_PACK_SIZE


# ---------------------------------------------------------------------------
# Escape / Unescape -- direct port of TextSectionPacker::Escape / Unescape
# ---------------------------------------------------------------------------

_ESCAPE_MAP = {
    0x00: "\\0",
    0x07: "\\a",
    0x08: "\\b",
    0x0C: "\\f",
    0x0A: "\\n",
    0x0D: "\\r",
    0x09: "\\t",
    0x0B: "\\v",
    ord("\\"): "\\\\",
    ord("'"): "\\'",
    ord('"'): '\\"',
    ord("?"): "\\?",
}

_UNESCAPE_MAP = {
    "0": 0x00,
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    "\\": ord("\\"),
    "'": ord("'"),
    '"': ord('"'),
    "?": ord("?"),
}


def escape_bytes(data: bytes) -> str:
    """Port of the Escape output-stream operator. Escapes a raw byte buffer
    (which may include embedded/trailing NULs) into a printable string."""
    out = []
    for b in data:
        out.append(_ESCAPE_MAP.get(b, chr(b)))
    return "".join(out)


def unescape_str(s: str) -> bytes:
    """Port of TextSectionPacker::Unescape. Unknown escape sequences are
    silently dropped (matching the original's behavior of printing a
    warning and not appending anything for the unrecognized character)."""
    out = bytearray()
    is_special = False
    for ch in s:
        if is_special:
            is_special = False
            if ch in _UNESCAPE_MAP:
                out.append(_UNESCAPE_MAP[ch])
            else:
                print(f"Warning: unrecognized escape sequence '\\{ch}' in CSV content; leaving it as-is.", file=sys.stderr)
            continue
        if ch == "\\":
            is_special = True
            continue
        out.append(ord(ch) & 0xFF)
    return bytes(out)


# ---------------------------------------------------------------------------
# CSV helpers (hand-rolled, see module docstring for why)
# ---------------------------------------------------------------------------

_CSV_HEADER = "lang_id,line_id,line_content\n"


def _write_csv_row(f, lang_id: int, line_id: int, content: bytes) -> None:
    f.write(f'{lang_id},{line_id},"{escape_bytes(content)}"\n')


def _read_csv_rows(path: Path):
    """Yields (lang_id, line_id, content_bytes) for each data row, skipping
    the header line. Mirrors the simple `int,int,"escaped"` format we write."""
    with open(path, "r", encoding="latin-1", newline="") as f:
        lines = f.readlines()
    if not lines:
        return
    for raw_line in lines[1:]:
        line = raw_line.rstrip("\r\n")
        if not line:
            continue
        first_comma = line.find(",")
        second_comma = line.find(",", first_comma + 1)
        if first_comma == -1 or second_comma == -1:
            continue
        lang_id = int(line[:first_comma])
        line_id = int(line[first_comma + 1:second_comma])
        rest = line[second_comma + 1:]
        first_quote = rest.find('"')
        last_quote = rest.rfind('"')
        if first_quote == -1 or last_quote <= first_quote:
            content_escaped = ""
        else:
            content_escaped = rest[first_quote + 1:last_quote]
        yield lang_id, line_id, unescape_str(content_escaped)


# ---------------------------------------------------------------------------
# Public API: unpack / pack
# ---------------------------------------------------------------------------

def unpack(bin_data: bytes, out_path: Path) -> None:
    """Port of TextSectionPacker::unpack. Writes ui_texts.csv and
    ui_alerts.csv into out_path (matching the original, which leaves the
    header/unk/unk2/eif dump code commented out)."""
    if len(bin_data) < STRUCT_SIZE:
        raise TextSectionError(
            f"input data too small: got {len(bin_data)} bytes, "
            f"need at least {STRUCT_SIZE} bytes for TextSection_mk3_5"
        )

    out_path = Path(out_path)
    out_path.mkdir(parents=True, exist_ok=True)

    texts_path = out_path / "ui_texts.csv"
    alerts_path = out_path / "ui_alerts.csv"

    with open(texts_path, "w", encoding="latin-1", newline="") as f:
        f.write(_CSV_HEADER)
        for lang_id in range(UI_NUM_LANGS):
            lang_base = UI_PACK_OFFSET + lang_id * UI_LINES_PER_LANG * UI_LINE_SIZE
            for line_id in range(UI_LINES_PER_LANG):
                off = lang_base + line_id * UI_LINE_SIZE
                raw = bin_data[off:off + UI_LINE_SIZE]
                _write_csv_row(f, lang_id, line_id, raw)

    with open(alerts_path, "w", encoding="latin-1", newline="") as f:
        f.write(_CSV_HEADER)
        for lang_id in range(ALERT_NUM_LANGS):
            lang_base = ALERT_PACK_OFFSET + lang_id * ALERT_LINES_PER_LANG * ALERT_RECORD_SIZE
            for rec_idx in range(ALERT_LINES_PER_LANG):
                off = lang_base + rec_idx * ALERT_RECORD_SIZE
                idx_val = struct.unpack_from("<H", bin_data, off)[0]
                raw = bin_data[off + ALERT_IDX_SIZE:off + ALERT_RECORD_SIZE]
                _write_csv_row(f, lang_id, idx_val, raw)


def pack(in_path: Path, out_path: Path) -> None:
    """Port of TextSectionPacker::pack. `in_path` is a binary template file
    (typically produced by a prior unpack() of the same section) whose
    sibling directory must contain ui_alerts.csv and ui_texts.csv. The
    patched binary is written to `out_path` (a *file* path, matching the
    original's direct `vectorToFile(out_path, ...)` call -- the original
    CLI's "-o/--output" help text calls this an output directory, but the
    underlying pack() always treats it as a file path)."""
    in_path = Path(in_path)
    out_path = Path(out_path)

    alerts_path = in_path.parent / "ui_alerts.csv"
    ui_path = in_path.parent / "ui_texts.csv"

    vbf_bin = bytearray(utils.read_file(in_path))
    if len(vbf_bin) < STRUCT_SIZE:
        raise TextSectionError(
            f"input binary too small: got {len(vbf_bin)} bytes, "
            f"need at least {STRUCT_SIZE} bytes for TextSection_mk3_5"
        )

    if not alerts_path.exists():
        raise TextSectionError(f"Missing required file: '{alerts_path}'.")
    if not ui_path.exists():
        raise TextSectionError(f"Missing required file: '{ui_path}'.")

    # pack alerts: CSV's line_id is used BOTH as the new `idx` field value
    # AND as the array index to write into (matches original exactly).
    for lang_id, line_id, content in _read_csv_rows(alerts_path):
        if not (0 <= lang_id < ALERT_NUM_LANGS) or not (0 <= line_id < ALERT_LINES_PER_LANG):
            continue
        rec_off = ALERT_PACK_OFFSET + lang_id * ALERT_LINES_PER_LANG * ALERT_RECORD_SIZE \
            + line_id * ALERT_RECORD_SIZE
        struct.pack_into("<H", vbf_bin, rec_off, line_id)
        copy_sz = min(len(content), ALERT_LINE_SIZE)
        line_off = rec_off + ALERT_IDX_SIZE
        vbf_bin[line_off:line_off + copy_sz] = content[:copy_sz]
        vbf_bin[line_off + copy_sz:line_off + ALERT_LINE_SIZE] = b"\x00" * (ALERT_LINE_SIZE - copy_sz)

    # pack ui texts
    for lang_id, line_id, content in _read_csv_rows(ui_path):
        if not (0 <= lang_id < UI_NUM_LANGS) or not (0 <= line_id < UI_LINES_PER_LANG):
            continue
        line_off = UI_PACK_OFFSET + lang_id * UI_LINES_PER_LANG * UI_LINE_SIZE \
            + line_id * UI_LINE_SIZE
        copy_sz = min(len(content), UI_LINE_SIZE)
        vbf_bin[line_off:line_off + copy_sz] = content[:copy_sz]
        vbf_bin[line_off + copy_sz:line_off + UI_LINE_SIZE] = b"\x00" * (UI_LINE_SIZE - copy_sz)

    utils.write_file(out_path, bytes(vbf_bin))
