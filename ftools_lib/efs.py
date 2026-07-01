"""Parses the "efs.bin" container format found inside an EXE-type VBF
(the one holding the main HMI executable), as opposed to the QNX
mkifs image filesystem ("bitmaps.bin") that ftools_lib/imagefs.py
handles for graphics-type VBFs. The two are genuinely different formats,
not just different content - see the format note below.

This file's container format has no fixed directory section: headers and
their file data are interleaved, with each entry's header found by
scanning forward in 8-byte steps from a fixed start offset (256), and
each entry's size only known once the *next* entry's header is found
(or the terminator is). The .ddb files inside it also use a different
realWidth rounding rule than bitmaps.bin's - see ddb.py's
`width_convention="efs"`.

Header detection at a candidate 8-byte-aligned position requires:
    byte[0] == 8, byte[1] == 0, byte[2] == 0, byte[5] == 0, byte[7] == 0
(bytes 3, 4, 6 unconstrained) followed by a NUL-terminated filename.
The end of the whole container is marked by the repeating 8-byte
sequence BE FF 00 00 BE FF 00 00.

Known limitation: that header-detection rule is a loose pattern match
against whatever bytes happen to be at each candidate position - it is
not a true delimiter. Scanning through RLE-compressed pixel data (which
can contain arbitrary byte values) occasionally produces a coincidental
match in the middle of a file's own data, which truncates that file and
desyncs parsing for whatever follows until the next genuine header
happens to be found again. In practice this affects a minority of
larger RLE-compressed files (confirmed in real samples) and is a
property of this scanning approach itself, not something this module
adds on top of it - there is no available source confirming how (or
whether) the original implementation avoids it. Treat any entry whose
data fails to decode as suspect for this reason, and verify entries you
care about before relying on them.
"""
import struct

from .utils import read_file, write_file

EfsError = type("EfsError", (RuntimeError,), {})

_SCAN_START = 256
_TERMINATOR = bytes([0xBE, 0xFF, 0x00, 0x00, 0xBE, 0xFF, 0x00, 0x00])


class EfsEntry:
    __slots__ = ("header_offset", "offset", "size", "name")

    def __init__(self, header_offset, offset, size, name):
        self.header_offset = header_offset
        self.offset = offset
        self.size = size
        self.name = name

    @property
    def is_dir(self):
        # Directory/path-component markers have no real data of their own;
        # in practice their computed "size" comes out non-positive.
        return self.size <= 0

    def __repr__(self):
        return f"EfsEntry({self.name!r}, offset=0x{self.offset:x}, size={self.size})"


class EfsFs:
    def __init__(self):
        self.blob = bytearray()
        self.entries = []
        self.is_open = False

    # ------------------------------------------------------------------ #
    def parse(self, data: bytes) -> int:
        blob = bytearray(data)
        n = len(blob)
        entries = []
        prev = None
        i = _SCAN_START
        while i < n:
            b = bytes(blob[i:i + 8])
            if len(b) < 8:
                break  # not enough bytes left for a header or the terminator
            if b == _TERMINATOR:
                if prev is not None:
                    prev.size = i - prev.offset - _count_trailing_ff(blob, i)
                break
            if b[0] == 8 and b[1] == 0 and b[2] == 0 and b[5] == 0 and b[7] == 0:
                d = 8
                while i + d < n and blob[i + d] != 0:
                    d += 1
                if i + d >= n:
                    break  # ran off the end without a NUL - not a real header
                name = blob[i + 8:i + d].decode("latin-1")
                f = _round_up4(d + 1)
                if i + f + 2 > n:
                    break
                g = struct.unpack_from("<H", blob, i + f)[0]
                e_size = _round_up(f + g, 64)
                if prev is not None:
                    prev.size = i - prev.offset - _count_trailing_ff(blob, i)
                data_offset = i + e_size
                entry = EfsEntry(header_offset=i, offset=data_offset, size=0, name=name)
                entries.append(entry)
                prev = entry
                i += 8
            else:
                i += 8

        self.blob = blob
        self.entries = entries
        self.is_open = True
        return 0

    def parse_file(self, path) -> int:
        return self.parse(read_file(path))

    # ------------------------------------------------------------------ #
    def get_entries(self, name):
        return [e for e in self.entries if e.name == name]

    def get_entry(self, name):
        matches = self.get_entries(name)
        if not matches:
            raise EfsError(f"No entry named {name!r}.")
        return matches[0]

    def get_file_data(self, name) -> bytes:
        e = self.get_entry(name)
        return bytes(self.blob[e.offset:e.offset + max(e.size, 0)])

    # ------------------------------------------------------------------ #
    def replace_file(self, name, data: bytes):
        """Replace an entry's data in place. Only same-size-or-smaller
        replacement is supported (smaller is padded with 0xFF to fill the
        original space) - growing an entry would require shifting every
        entry after it, which this module deliberately does not attempt
        given the scanning ambiguity noted in the module docstring; a
        wrong shift here is harder to detect than a same-size edit is.
        """
        e = self.get_entry(name)
        if len(data) > e.size:
            raise EfsError(
                f"Cannot replace {name!r}: new data is {len(data)} bytes, "
                f"larger than the {e.size} bytes available. This module only "
                f"supports same-size-or-smaller in-place replacement."
            )
        self.blob[e.offset:e.offset + len(data)] = data
        if len(data) < e.size:
            self.blob[e.offset + len(data):e.offset + e.size] = bytes([0xFF] * (e.size - len(data)))

    # ------------------------------------------------------------------ #
    def export(self, out_dir):
        from pathlib import Path
        out_dir = Path(out_dir)
        files_dir = out_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        seen = {}
        manifest_entries = []
        for e in self.entries:
            seen[e.name] = seen.get(e.name, 0) + 1
            n = seen[e.name]
            disk_name = e.name if n == 1 else f"{e.name}.dup{n}"
            manifest_entries.append({
                "name": e.name, "header_offset": e.header_offset,
                "offset": e.offset, "size": e.size, "disk_name": disk_name,
            })
            if e.is_dir:
                continue
            data = bytes(self.blob[e.offset:e.offset + e.size])
            out_path = files_dir / disk_name
            out_path.parent.mkdir(parents=True, exist_ok=True)
            write_file(out_path, data)

        import json
        with open(out_dir / "efs_config.json", "w") as f:
            json.dump({"format": "efs-container", "entries": manifest_entries}, f, indent=2)
        return 0


def _count_trailing_ff(blob, pos):
    o = 0
    while pos - o - 1 >= 0 and blob[pos - o - 1] == 0xFF:
        o += 1
    return o


def _round_up4(n):
    return ((n + 3) // 4) * 4


def _round_up(n, multiple):
    return ((n + multiple - 1) // multiple) * multiple
