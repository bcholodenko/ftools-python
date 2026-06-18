"""
VbfFile: pack/unpack/info handling for Ford IPC ".vbf" firmware files,
equivalent to VbfEditor/VbfFile.cpp/.h.

A .vbf file is: [ASCII text header][binary section]...[binary section]
The header is plain text (a C-struct-like block); its exact extent is found
by scanning bytes from the start of the file and balancing '{' / '}' until
the brace count returns to zero. The header text embeds, as plain strings to
be located via regex and patched in place on save, a `file_checksum = 0x...;`
field (CRC-32, standard/zlib polynomial, over the entire binary section) and
usually a `// Bytes:    NNN` comment giving the binary section's size minus
20 bytes (no explanation for the magic 20 is given upstream; preserved as-is).

Each binary section is: big-endian uint32 start_addr, big-endian uint32
length, `length` bytes of raw data, big-endian uint16 CRC-16/CCITT-FALSE of
that raw data.
"""

import json
import re
import struct
from pathlib import Path

from .crc import crc32 as _crc32_calc, crc16_ccitt_false
from .utils import read_file, write_file

_RE_FILE_CHECKSUM = re.compile(r"\bfile_checksum.*=.*0x(.*);")
_RE_BYTES = re.compile(r"\bBytes:.*?(\d+)")
_RE_BYTES_LINE = re.compile(r"// Bytes:    \d+")


class VbfError(RuntimeError):
    pass


class VbfBinarySection:
    __slots__ = ("start_addr", "length", "data", "crc16")

    def __init__(self, start_addr=0, length=0, data=b"", crc16=0):
        self.start_addr = start_addr
        self.length = length
        self.data = data
        self.crc16 = crc16


class VbfFile:
    def __init__(self):
        self.file_name = ""
        self.file_length = 0
        self.crc32 = 0
        self.content_size = 0
        self.ascii_header = ""
        self.sections = []  # list[VbfBinarySection], in on-disk order
        self.is_open = False

    # ---------------------------------------------------------------- #
    @staticmethod
    def _scan_ascii_header(data: bytes) -> bytes:
        """Collect bytes from the start of `data` until '{'/'}' balance
        back to zero (mirrors the original char-by-char brace-counting
        loop exactly, including stopping only after at least one '{' has
        been seen)."""
        header = bytearray()
        opened = 0
        for byte in data:
            header.append(byte)
            if byte == 0x7B:  # '{'
                opened += 1
            elif byte == 0x7D:  # '}'
                opened -= 1
                if opened == 0:
                    break
        return bytes(header)

    def open_file(self, file_path):
        file_path = Path(file_path)
        try:
            data = read_file(file_path)
        except Exception as e:
            raise VbfError(f"Could not open VBF file '{file_path}': {e}")

        self.file_name = file_path.name
        self.file_length = len(data)

        header_bytes = self._scan_ascii_header(data)
        self.ascii_header = header_bytes.decode("latin-1")

        m = _RE_FILE_CHECKSUM.search(self.ascii_header)
        if not m:
            raise VbfError("Could not parse VBF file: the ASCII header does not contain a file_checksum field.")
        self.crc32 = int(m.group(1), 16)

        data_section_offset = len(header_bytes)
        self.content_size = len(data) - data_section_offset

        m2 = _RE_BYTES.search(self.ascii_header)
        if not m2:
            print("Warning: the VBF header does not specify a content length (no '// Bytes:' comment); skipping that check.")
        else:
            if self.content_size != int(m2.group(1)) + 20:
                # NOTE: i don't know why but real content size is 20 bytes
                # more than the header's Bytes value - preserved verbatim
                # from the original tool's comment/behaviour.
                print("Warning: the content length declared in the VBF header does not match the file's actual content length.")

        content = data[data_section_offset:data_section_offset + self.content_size]
        crc = _crc32_calc(content)
        if self.crc32 != crc:
            raise VbfError("Could not parse VBF file: the binary data's CRC-32 does not match the checksum in the header.")

        self.sections = []
        i = data_section_offset
        end = len(data)
        while i < end:
            section_offset = i
            start_addr = struct.unpack_from(">I", data, i)[0]
            i += 4
            length = struct.unpack_from(">I", data, i)[0]
            i += 4
            sec_data = data[i:i + length]
            i += length
            crc16 = struct.unpack_from(">H", data, i)[0]
            i += 2

            calc_crc16 = crc16_ccitt_false(sec_data)
            if crc16 != calc_crc16:
                print(f"Warning: section at offset 0x{section_offset:x} has a CRC-16 mismatch (its data may be corrupt).")

            self.sections.append(VbfBinarySection(start_addr, length, sec_data, crc16))

        self.is_open = True
        return 0

    # ---------------------------------------------------------------- #
    def export(self, out_dir):
        if not self.is_open:
            return -1
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        header_name = f"{self.file_name}_ascii_head.txt"
        write_file(out_dir / header_name, self.ascii_header.encode("latin-1"))

        sections_json = []
        for i, section in enumerate(self.sections, start=1):
            # NOTE: faithfully reproduces the original's stream-formatting
            # quirk where `length` ends up printed in hex too (an un-reset
            # std::hex manipulator left active from `start_addr`), not
            # decimal. This only affects the cosmetic filename; the actual
            # section length is always read back from the file's real size
            # on Import, never parsed out of the filename.
            fname = f"{self.file_name}_section_{i}_{section.start_addr:x}_{section.length:x}.bin"
            write_file(out_dir / fname, section.data)
            sections_json.append({
                "file": fname,
                "address": f"0x{section.start_addr:x}",
            })

        config = {"header": header_name, "sections": sections_json}
        with open(out_dir / f"{self.file_name}_config.json", "w") as f:
            json.dump(config, f, indent=4)
        return 0

    def import_config(self, conf_file_path):
        conf_file_path = Path(conf_file_path)
        config_dir = conf_file_path.resolve().parent

        try:
            with open(conf_file_path, "r", encoding="utf-8") as f:
                document = json.load(f)
        except OSError as e:
            raise VbfError(f"Could not open config file '{conf_file_path}': {e}")

        header_path = config_dir / document["header"]
        try:
            header_bytes = read_file(header_path)
        except Exception:
            raise VbfError(f"Could not open header file '{header_path}'.")
        vbf_header = header_bytes.decode("latin-1")

        self.content_size = 0
        sections = []
        crc = 0
        for sec_obj in document["sections"]:
            start_addr = int(sec_obj["address"], 16)
            sec_path = config_dir / sec_obj["file"]
            try:
                sec_data = read_file(sec_path)
            except Exception:
                raise VbfError(f"Could not open section file '{sec_path}'.")

            length = len(sec_data)
            crc16 = crc16_ccitt_false(sec_data)

            addr_be = struct.pack(">I", start_addr)
            length_be = struct.pack(">I", length)
            crc16_be = struct.pack(">H", crc16)

            crc = _crc32_calc(addr_be, crc)
            crc = _crc32_calc(length_be, crc)
            crc = _crc32_calc(sec_data, crc)
            crc = _crc32_calc(crc16_be, crc)

            self.content_size += length + 4 + 4 + 2
            sections.append(VbfBinarySection(start_addr, length, sec_data, crc16))

        self.sections = sections
        self.crc32 = crc
        self.file_name = "imported.vbf"
        self.ascii_header = vbf_header
        self.is_open = True
        return 0

    # ---------------------------------------------------------------- #
    def _calc_crc32(self) -> int:
        crc = 0
        for section in self.sections:
            addr_be = struct.pack(">I", section.start_addr)
            length_be = struct.pack(">I", section.length)
            crc16_be = struct.pack(">H", section.crc16)
            crc = _crc32_calc(addr_be, crc)
            crc = _crc32_calc(length_be, crc)
            crc = _crc32_calc(section.data, crc)
            crc = _crc32_calc(crc16_be, crc)
        return crc

    def save_to_file(self, file_path):
        # fix header
        self.crc32 = self._calc_crc32()

        m = _RE_FILE_CHECKSUM.search(self.ascii_header)
        if not m:
            raise VbfError("Could not save VBF file: the ASCII header does not contain a file_checksum field.")
        old_hex = m.group(1)
        # Preserve the original hex case (some VBFs use lowercase, others uppercase)
        fmt = "{:08x}" if old_hex == old_hex.lower() else "{:08X}"
        new_crc_hex = fmt.format(self.crc32)
        self.ascii_header = self.ascii_header.replace(old_hex, new_crc_hex)

        m2 = _RE_BYTES_LINE.search(self.ascii_header)
        if m2:
            new_line = f"// Bytes:    {self.content_size - 20}"  # backward fix
            self.ascii_header = self.ascii_header.replace(m2.group(0), new_line)

        file_path = Path(file_path)
        try:
            with open(file_path, "wb") as f:
                f.write(self.ascii_header.encode("latin-1"))
                for section in self.sections:
                    f.write(struct.pack(">I", section.start_addr))
                    f.write(struct.pack(">I", section.length))
                    f.write(section.data)
                    f.write(struct.pack(">H", section.crc16))
        except OSError as e:
            raise VbfError(f"Could not create output file '{file_path}': {e}")
        return 0

    # ---------------------------------------------------------------- #
    def get_section_raw(self, section_idx: int) -> bytes:
        if section_idx >= len(self.sections):
            raise VbfError(
                f"Invalid section index {section_idx}: this VBF file only has "
                f"{len(self.sections)} section(s) (valid indices are 0-{len(self.sections) - 1})."
            )
        return self.sections[section_idx].data

    def replace_section_raw(self, section_idx: int, section_data: bytes):
        if section_idx >= len(self.sections):
            raise VbfError(
                f"Invalid section index {section_idx}: this VBF file only has "
                f"{len(self.sections)} section(s) (valid indices are 0-{len(self.sections) - 1})."
            )
        section = self.sections[section_idx]
        self.content_size += len(section_data) - section.length
        section.data = section_data
        section.length = len(section_data)
        section.crc16 = crc16_ccitt_false(section_data)

    def get_section_info(self, section_idx: int):
        if section_idx >= len(self.sections):
            raise VbfError(
                f"Invalid section index {section_idx}: this VBF file only has "
                f"{len(self.sections)} section(s) (valid indices are 0-{len(self.sections) - 1})."
            )
        section = self.sections[section_idx]
        return {"start_addr": section.start_addr, "length": section.length}

    def sections_count(self) -> int:
        return len(self.sections)

    def header_sz(self) -> int:
        return len(self.ascii_header)
