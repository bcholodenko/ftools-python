"""
ImageFs: parses/builds the QNX `mkifs` "image filesystem" binary blob found
inside some Ford/Volvo IPC bitmap/font resource downloads (a different,
independently-documented format from FTools' own zip+ttf "ImageSection"
blob handled by image_section.py). This is new functionality, not present
in the original FTools-5.0 C++ tool, added to handle real-world VBF files
whose UI resources are packaged this way instead.

The on-disk layout (reverse-engineered against real firmware data and
cross-checked against QNX's own public header,
trunk/services/system/public/sys/image.h, in the OpenQNX source mirror)
is, all fields little-endian with no struct padding:

    struct image_header {                          (88 bytes, fixed part)
        char           signature[7];                # literal b"imagefs"
        unsigned char  flags;                        # bit0=big-endian,
                                                       # bit1=read-only,
                                                       # bit2=inode bits valid
        unsigned long  image_size;                   # bytes from header
                                                       # start to (not
                                                       # including) the
                                                       # trailer
        unsigned long  hdr_dir_size;                 # bytes from header
                                                       # start to the end of
                                                       # the directory
                                                       # (== where file data
                                                       # begins)
        unsigned long  dir_offset;                   # bytes from header
                                                       # start to the first
                                                       # directory entry
        unsigned long  boot_ino[4];
        unsigned long  script_ino;
        unsigned long  chain_paddr;                  # offset to a 2nd,
                                                       # chained imagefs; 0
                                                       # if none
        unsigned long  spare[10];
        unsigned long  mountflags;
        char           mountpoint[];                 # null-terminated,
                                                       # variable length
    };
    # struct ends and the directory begins at `dir_offset`, which is
    # `image_header`'s fixed 88 bytes plus the (null-terminated)
    # mountpoint string, rounded up to a 4-byte boundary.

Directory entries are a sequence of variable-length, 4-byte-padded
records, terminated by a 4-byte zero word. Every record starts with a
24-byte common header:

    struct image_attr {
        unsigned short size;             # this record's total length,
                                          # rounded up to a multiple of 4;
                                          # 0 terminates the directory
        unsigned short extattr_offset;   # 0 = no extended attributes
        unsigned long  ino;
        unsigned long  mode;             # mode & S_IFMT selects the kind
                                          # below
        unsigned long  gid;
        unsigned long  uid;
        unsigned long  mtime;
    };

followed by, depending on `mode & S_IFMT`:
    S_IFDIR  -> char path[];                                  (no leading
                                                                slash)
    S_IFREG  -> unsigned long offset; unsigned long size; char path[];
                (offset is absolute from the header's start, like all
                other offsets in this format; file data itself is stored
                separately, packed back-to-back with zero padding between
                files, starting exactly at `hdr_dir_size`)
    S_IFLNK  -> unsigned short sym_offset; unsigned short sym_size;
                char path[]; (path holds the link's own name; the target
                string lives at path[sym_offset], i.e. appended after the
                own name's null terminator in the same buffer)
    S_IFCHR/S_IFBLK/S_IFIFO -> unsigned long dev; unsigned long rdev;
                char path[];

A 4-byte `struct image_trailer { unsigned long cksum; }` follows
immediately after `image_size` bytes (so the whole file is exactly
`image_size + 4` bytes long). The trailer's exact checksum algorithm could
not be conclusively determined: testing against a real 15MB sample showed
the body (header+directory+files, as little-endian 32-bit words) already
summed to exactly zero on its own, independent of the trailer value, which
strongly suggests *some* complement-style design was used by the original
mkifs build tool, but the precise formula relating it to the stored
trailer value wasn't pinned down despite testing the common variants
(plain 32-bit two's-complement sum, 16/32-bit end-around-carry sums,
CRC-32, Adler-32, and a header+directory-only variant). This module:
  - preserves the original trailer byte-for-byte whenever nothing in the
    image filesystem was modified (a guaranteed byte-identical round trip,
    the same approach already used elsewhere in this project for VBF's
    and ImageSection's own less-mysterious checksums), and
  - for an image filesystem that *was* modified, recomputes the trailer
    using the best-effort, documented-intent formula (the value that
    makes the body's own 32-bit word-sum complement to zero). This value
    has not been verified against any real consuming code. The sample
    firmware this was reverse-engineered from is a non-bootable UI
    resource bundle (no preceding `startup_header`/IPL boot chain), so
    it's plausible this checksum isn't actually checked by whatever loads
    these resources on the IPC -- but this has not been confirmed either.
    Treat a repacked-with-changes file as untested until verified on real
    hardware or a simulator.
"""

import json
import struct
from pathlib import Path

from .utils import read_file, write_file

IMAGE_SIGNATURE = b"imagefs"

S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFREG = 0o100000
S_IFLNK = 0o120000
S_IFCHR = 0o020000
S_IFBLK = 0o060000
S_IFIFO = 0o010000

IMAGE_FLAGS_BIGENDIAN = 0x01
IMAGE_FLAGS_READONLY = 0x02
IMAGE_FLAGS_INO_BITS = 0x04

_HEADER_FIXED_FMT = "<7sB20I"
_HEADER_FIXED_SIZE = struct.calcsize(_HEADER_FIXED_FMT)
assert _HEADER_FIXED_SIZE == 88

_ATTR_FMT = "<HHIIIII"
_ATTR_SIZE = struct.calcsize(_ATTR_FMT)
assert _ATTR_SIZE == 24

_FILE_EXTRA_FMT = "<II"          # offset, size
_SYMLINK_EXTRA_FMT = "<HH"       # sym_offset, sym_size
_DEVICE_EXTRA_FMT = "<II"        # dev, rdev


class ImageFsError(RuntimeError):
    pass


def _round_up4(n: int) -> int:
    return (n + 3) & ~3


def _round_up8(n: int) -> int:
    return (n + 7) & ~7


def _cstr(buf: bytes) -> str:
    return buf.split(b"\x00", 1)[0].decode("latin-1")


class DirEntry:
    """One directory/file/symlink/device record. `path` never has a
    leading slash, matching the on-disk format; for nested paths it
    contains the full relative path, e.g. "fonts/MHeiM18030_C.ttf"."""

    __slots__ = (
        "kind", "path", "ino", "mode", "gid", "uid", "mtime",
        "extattr_offset", "data", "target", "dev", "rdev",
    )

    def __init__(self, kind, path, ino=0, mode=0, gid=0, uid=0, mtime=0,
                 extattr_offset=0, data=b"", target="", dev=0, rdev=0):
        self.kind = kind  # "dir" | "file" | "symlink" | "device"
        self.path = path
        self.ino = ino
        self.mode = mode
        self.gid = gid
        self.uid = uid
        self.mtime = mtime
        self.extattr_offset = extattr_offset
        self.data = data        # only meaningful for kind == "file"
        self.target = target    # only meaningful for kind == "symlink"
        self.dev = dev          # only meaningful for kind == "device"
        self.rdev = rdev        # only meaningful for kind == "device"


class ImageFs:
    def __init__(self):
        self.flags = IMAGE_FLAGS_INO_BITS
        self.boot_ino = [0, 0, 0, 0]
        self.script_ino = 0
        self.chain_paddr = 0
        self.spare = [0] * 10
        self.mountflags = 0
        self.mountpoint = ""
        self.entries = []  # list[DirEntry], in original directory order
        self.is_open = False
        # Set by parse(); returned verbatim by save_to_vector() as long as
        # nothing has been modified since, guaranteeing a byte-identical
        # round trip for the unmodified case (the trailer checksum's exact
        # algorithm is not reliably known -- see module docstring).
        self._original_bytes = None

    # ------------------------------------------------------------------ #
    @staticmethod
    def looks_like_imagefs(data: bytes) -> bool:
        return data[:7] == IMAGE_SIGNATURE

    def parse(self, bin_data: bytes):
        if len(bin_data) < _HEADER_FIXED_SIZE:
            raise ImageFsError(
                "Not a valid QNX image filesystem: data is too short to contain a header."
            )
        sig = bin_data[:7]
        if sig != IMAGE_SIGNATURE:
            raise ImageFsError(
                f"Not a valid QNX image filesystem: expected signature {IMAGE_SIGNATURE!r}, got {sig!r}."
            )

        unpacked = struct.unpack_from(_HEADER_FIXED_FMT, bin_data, 0)
        flags = unpacked[1]
        ints = unpacked[2:]
        image_size, hdr_dir_size, dir_offset = ints[0:3]
        boot_ino = list(ints[3:7])
        script_ino, chain_paddr = ints[7:9]
        spare = list(ints[9:19])
        mountflags = ints[19]

        if image_size + 4 > len(bin_data):
            raise ImageFsError(
                f"Truncated QNX image filesystem: header declares image_size={image_size} "
                f"(needs {image_size + 4} bytes total including the trailer), but only "
                f"{len(bin_data)} bytes are available."
            )
        if dir_offset < _HEADER_FIXED_SIZE or dir_offset > hdr_dir_size or hdr_dir_size > image_size:
            raise ImageFsError(
                f"Not a valid QNX image filesystem: header offsets are inconsistent "
                f"(dir_offset={dir_offset}, hdr_dir_size={hdr_dir_size}, image_size={image_size})."
            )

        mountpoint = _cstr(bin_data[_HEADER_FIXED_SIZE:dir_offset])

        entries = []
        dpos = dir_offset
        while True:
            if dpos + 2 > len(bin_data):
                raise ImageFsError(
                    f"Truncated QNX image filesystem: ran off the end of the data while "
                    f"reading the directory at offset {dpos}."
                )
            size = struct.unpack_from("<H", bin_data, dpos)[0]
            if size == 0:
                break
            if size < _ATTR_SIZE:
                raise ImageFsError(
                    f"Invalid directory entry at offset {dpos}: declared size {size} is "
                    f"smaller than the minimum entry header ({_ATTR_SIZE} bytes)."
                )
            if dpos + size > len(bin_data):
                raise ImageFsError(
                    f"Truncated QNX image filesystem: directory entry at offset {dpos} "
                    f"declares size {size}, which extends past the end of the data."
                )

            _size, extattr_offset, ino, mode, gid, uid, mtime = struct.unpack_from(
                _ATTR_FMT, bin_data, dpos
            )
            rest = bin_data[dpos + _ATTR_SIZE:dpos + size]
            typ = mode & S_IFMT

            if typ == S_IFREG:
                extra_sz = struct.calcsize(_FILE_EXTRA_FMT)
                if len(rest) < extra_sz:
                    raise ImageFsError(f"Truncated file entry at offset {dpos}.")
                offset, fsize = struct.unpack_from(_FILE_EXTRA_FMT, rest, 0)
                path = _cstr(rest[extra_sz:])
                if offset + fsize > len(bin_data):
                    raise ImageFsError(
                        f"File entry {path!r} extends past the end of the data "
                        f"(offset={offset}, size={fsize}, data is {len(bin_data)} bytes)."
                    )
                data = bin_data[offset:offset + fsize]
                entry = DirEntry("file", path, ino, mode, gid, uid, mtime, extattr_offset, data=data)

            elif typ == S_IFDIR:
                path = _cstr(rest)
                entry = DirEntry("dir", path, ino, mode, gid, uid, mtime, extattr_offset)

            elif typ == S_IFLNK:
                extra_sz = struct.calcsize(_SYMLINK_EXTRA_FMT)
                if len(rest) < extra_sz:
                    raise ImageFsError(f"Truncated symlink entry at offset {dpos}.")
                sym_offset, _sym_size = struct.unpack_from(_SYMLINK_EXTRA_FMT, rest, 0)
                path_buf = rest[extra_sz:]
                path = _cstr(path_buf)
                target = _cstr(path_buf[sym_offset:]) if sym_offset else ""
                entry = DirEntry("symlink", path, ino, mode, gid, uid, mtime, extattr_offset, target=target)

            elif typ in (S_IFCHR, S_IFBLK, S_IFIFO):
                extra_sz = struct.calcsize(_DEVICE_EXTRA_FMT)
                if len(rest) < extra_sz:
                    raise ImageFsError(f"Truncated device entry at offset {dpos}.")
                dev, rdev = struct.unpack_from(_DEVICE_EXTRA_FMT, rest, 0)
                path = _cstr(rest[extra_sz:])
                entry = DirEntry("device", path, ino, mode, gid, uid, mtime, extattr_offset, dev=dev, rdev=rdev)

            else:
                raise ImageFsError(
                    f"Directory entry at offset {dpos} has an unrecognized type (mode={oct(mode)})."
                )

            entries.append(entry)
            dpos += size

        self.flags = flags
        self.boot_ino = boot_ino
        self.script_ino = script_ino
        self.chain_paddr = chain_paddr
        self.spare = spare
        self.mountflags = mountflags
        self.mountpoint = mountpoint
        self.entries = entries
        self.is_open = True
        self._original_bytes = bytes(bin_data[:image_size + 4])
        return 0

    def parse_file(self, file_path):
        self.parse(read_file(file_path))
        return 0

    # ------------------------------------------------------------------ #
    def list_files(self):
        return [e.path for e in self.entries if e.kind == "file"]

    def get_entries(self, path: str) -> list:
        """Returns every entry with this exact path. Real-world image
        filesystems have been observed to contain more than one entry
        under the same path (apparently an intentional QNX
        shadowing/overlay mechanism, not a bug in this parser) -- so
        callers that care about every copy of a resource should use this
        rather than get_entry()."""
        matches = [e for e in self.entries if e.path == path]
        if not matches:
            raise ImageFsError(f"No entry named {path!r} in this image filesystem.")
        return matches

    def get_entry(self, path: str) -> DirEntry:
        """Returns the first entry with this path. See get_entries() if
        the image filesystem may contain duplicate paths."""
        return self.get_entries(path)[0]

    def get_file_data(self, path: str) -> bytes:
        entry = self.get_entry(path)
        if entry.kind != "file":
            raise ImageFsError(f"{path!r} is a {entry.kind}, not a file.")
        return entry.data

    def replace_file(self, path: str, data: bytes):
        """Replace an existing regular file's content in place (size may
        change; this module recomputes every subsequent file's offset on
        the next save_to_vector() call). If `path` names more than one
        entry (see get_entries()), every matching file entry is updated,
        since that is almost always what's wanted when replacing a named
        resource. Raises if `path` doesn't name any regular file -- this
        module doesn't support adding brand new entries, only editing the
        content of existing ones."""
        matches = self.get_entries(path)
        if not any(e.kind == "file" for e in matches):
            kinds = ", ".join(sorted({e.kind for e in matches}))
            raise ImageFsError(f"Cannot replace {path!r}: it is a {kinds}, not a file.")
        for entry in matches:
            if entry.kind == "file":
                entry.data = bytes(data)
        self._original_bytes = None  # invalidate the byte-identical fast path
        return 0

    # ------------------------------------------------------------------ #
    def export(self, out_dir):
        if not self.is_open:
            return -1
        out_dir = Path(out_dir)
        files_dir = out_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        # Real-world image filesystems can contain more than one entry
        # under the same path (see get_entries()); give each one's
        # on-disk copy a distinct name so a later duplicate never
        # silently overwrites an earlier one with different content.
        seen_paths = {}

        entries_json = []
        for e in self.entries:
            item = {
                "kind": e.kind,
                "path": e.path,
                "ino": e.ino,
                "mode": e.mode,
                "gid": e.gid,
                "uid": e.uid,
                "mtime": e.mtime,
                "extattr_offset": e.extattr_offset,
            }
            if e.kind == "file":
                seen_paths[e.path] = seen_paths.get(e.path, 0) + 1
                n = seen_paths[e.path]
                disk_rel = e.path if n == 1 else f"{e.path}.dup{n}"
                data_file = files_dir / disk_rel
                data_file.parent.mkdir(parents=True, exist_ok=True)
                write_file(data_file, e.data)
                item["data_file"] = str(Path("files") / disk_rel)
            elif e.kind == "symlink":
                item["target"] = e.target
            elif e.kind == "device":
                item["dev"] = e.dev
                item["rdev"] = e.rdev
            entries_json.append(item)

        manifest = {
            "format": "qnx-imagefs",
            "flags": self.flags,
            "boot_ino": self.boot_ino,
            "script_ino": self.script_ino,
            "chain_paddr": self.chain_paddr,
            "spare": self.spare,
            "mountflags": self.mountflags,
            "mountpoint": self.mountpoint,
            "entries": entries_json,
        }
        with open(out_dir / "imagefs_config.json", "w") as f:
            json.dump(manifest, f, indent=2)
        return 0

    def import_config(self, config_path):
        config_path = Path(config_path)
        config_dir = config_path.resolve().parent

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except OSError as e:
            raise ImageFsError(f"Could not open config file '{config_path}': {e}")
        except json.JSONDecodeError as e:
            raise ImageFsError(f"Config file '{config_path}' is not valid JSON: {e}")

        try:
            entries = []
            for item in manifest["entries"]:
                kind = item["kind"]
                common = (
                    item["path"], item["ino"], item["mode"], item["gid"],
                    item["uid"], item["mtime"], item.get("extattr_offset", 0),
                )
                if kind == "file":
                    data_path = config_dir / item["data_file"]
                    try:
                        data = read_file(data_path)
                    except Exception as e:
                        raise ImageFsError(f"Could not read file data '{data_path}': {e}")
                    entries.append(DirEntry("file", *common, data=data))
                elif kind == "dir":
                    entries.append(DirEntry("dir", *common))
                elif kind == "symlink":
                    entries.append(DirEntry("symlink", *common, target=item.get("target", "")))
                elif kind == "device":
                    entries.append(DirEntry(
                        "device", *common, dev=item.get("dev", 0), rdev=item.get("rdev", 0)
                    ))
                else:
                    raise ImageFsError(f"Unknown entry kind {kind!r} in config for path {item['path']!r}.")

            self.flags = manifest.get("flags", IMAGE_FLAGS_INO_BITS)
            self.boot_ino = manifest.get("boot_ino", [0, 0, 0, 0])
            self.script_ino = manifest.get("script_ino", 0)
            self.chain_paddr = manifest.get("chain_paddr", 0)
            self.spare = manifest.get("spare", [0] * 10)
            self.mountflags = manifest.get("mountflags", 0)
            self.mountpoint = manifest.get("mountpoint", "")
            self.entries = entries
        except KeyError as e:
            raise ImageFsError(f"Config file '{config_path}' is missing required field {e}.")

        self.is_open = True
        self._original_bytes = None
        return 0

    # ------------------------------------------------------------------ #
    def _encode_entry(self, entry: DirEntry, file_offset: int = 0) -> bytes:
        path_b = entry.path.encode("latin-1") + b"\x00"

        if entry.kind == "file":
            extra = struct.pack(_FILE_EXTRA_FMT, file_offset, len(entry.data))
            body = extra + path_b
        elif entry.kind == "dir":
            body = path_b
        elif entry.kind == "symlink":
            target_b = entry.target.encode("latin-1") + b"\x00"
            sym_offset = len(path_b)
            extra = struct.pack(_SYMLINK_EXTRA_FMT, sym_offset, len(target_b))
            body = extra + path_b + target_b
        elif entry.kind == "device":
            extra = struct.pack(_DEVICE_EXTRA_FMT, entry.dev, entry.rdev)
            body = extra + path_b
        else:
            raise ImageFsError(f"Unknown entry kind {entry.kind!r} for path {entry.path!r}.")

        unpadded = _ATTR_SIZE + len(body)
        size = _round_up4(unpadded)
        attr = struct.pack(_ATTR_FMT, size, entry.extattr_offset, entry.ino, entry.mode, entry.gid, entry.uid, entry.mtime)
        record = attr + body
        record += b"\x00" * (size - len(record))
        return record

    def save_to_vector(self) -> bytes:
        if self._original_bytes is not None:
            return self._original_bytes

        if not self.entries:
            raise ImageFsError("Cannot build an image filesystem with no directory entries.")

        mountpoint_b = self.mountpoint.encode("latin-1") + b"\x00"
        dir_offset = _round_up4(_HEADER_FIXED_SIZE + len(mountpoint_b))

        # Pass 1: encode every entry with a placeholder file offset of 0,
        # to learn each record's final size (independent of the offset
        # value itself) and therefore where the directory ends.
        provisional = [self._encode_entry(e, 0) for e in self.entries]
        dir_size = sum(len(r) for r in provisional) + 4  # +4 for the terminator
        hdr_dir_size = dir_offset + dir_size

        # Pass 2: now that file data placement can start at hdr_dir_size,
        # lay files out back-to-back (no inter-file padding, matching the
        # original format) in directory-traversal order, and re-encode
        # every entry with its real file offset filled in.
        cursor = hdr_dir_size
        final_records = []
        file_blobs = []
        for entry in self.entries:
            if entry.kind == "file":
                final_records.append(self._encode_entry(entry, cursor))
                file_blobs.append(entry.data)
                cursor += len(entry.data)
            else:
                final_records.append(self._encode_entry(entry))

        body_len_unpadded = cursor
        image_size = _round_up8(body_len_unpadded)
        tail_pad = image_size - body_len_unpadded

        header = struct.pack(
            _HEADER_FIXED_FMT,
            IMAGE_SIGNATURE, self.flags,
            image_size, hdr_dir_size, dir_offset,
            *self.boot_ino, self.script_ino, self.chain_paddr,
            *self.spare, self.mountflags,
        )
        header += mountpoint_b
        header += b"\x00" * (dir_offset - len(header))

        directory = b"".join(final_records) + b"\x00\x00\x00\x00"
        files = b"".join(file_blobs) + (b"\x00" * tail_pad)

        body = header + directory + files
        if len(body) != image_size:
            raise ImageFsError(
                f"Internal error building image filesystem: expected body length "
                f"{image_size}, got {len(body)}."
            )

        trailer = self._best_effort_checksum(body)
        return body + struct.pack("<I", trailer)

    @staticmethod
    def _best_effort_checksum(body: bytes) -> int:
        """Best-effort trailer value: makes the body's own little-endian
        32-bit word sum complement to zero. Documented as unverified in
        the module docstring -- this is an educated guess at the design
        intent, not a confirmed algorithm."""
        total = 0
        for off in range(0, len(body), 4):
            total = (total + struct.unpack_from("<I", body, off)[0]) & 0xFFFFFFFF
        return (-total) & 0xFFFFFFFF

    def save_to_file(self, out_path):
        write_file(out_path, self.save_to_vector())
        return 0
