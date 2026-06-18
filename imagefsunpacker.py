#!/usr/bin/env python3
"""
imagefsunpacker.py - QNX "imagefs" image filesystem unpacker/repacker for
Ford/Volvo IPC firmware .vbf files.

Some IPC firmware downloads (typically a dedicated bitmap/font resource
package, distinct from the OS/app package -- look for a header comment
naming something like "bitmaps.bin.tar.gz") embed a QNX `mkifs` "image
filesystem" blob in one of the VBF's binary sections, usually itself
wrapped in a gzip-compressed tar archive. This is a different, separate
container format from FTools' own zip+ttf "ImageSection" blob handled by
imgsectionparser.py/imgunpacker.py, so it needs its own tool. See
ftools_lib/imagefs.py's module docstring for the on-disk format details
and an important caveat about the trailer checksum's exact algorithm not
having been conclusively determined.

This tool scans every section of a .vbf file looking for either:
  - an imagefs blob directly (raw, unwrapped), or
  - a gzip+tar archive containing exactly one non-empty regular-file
    member whose decompressed content is an imagefs blob (the form seen
    in real Ford/Volvo "bitmaps" VBFs)
and unpacks every one found into its own subdirectory (section fonts,
bitmaps, and any other embedded files become real files on disk, plus a
JSON manifest), or repacks edited resources back into a patched copy of
the original .vbf.

Usage:
    imagefsunpacker.py -u -o ./exported_dir firmware.vbf
    imagefsunpacker.py -p -e ./exported_dir -o ./patched.vbf firmware.vbf

If nothing under the exported directory was actually modified, repacking
reproduces the original .vbf byte-for-byte (the same guarantee vbfeditor.py
gives for its own unpack/repack); only sections containing files that were
genuinely changed get rebuilt and re-compressed.
"""

import argparse
import gzip
import io
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftools_lib.vbf import VbfFile, VbfError
from ftools_lib.imagefs import ImageFs, ImageFsError


def _try_unwrap_gzip_tar(data: bytes):
    """If `data` is a gzip-compressed tar archive containing exactly one
    sizeable regular-file member whose content looks like an imagefs
    blob, returns (imagefs_bytes, wrap_info); otherwise returns None.
    wrap_info captures everything needed to rebuild the same wrapping on
    repack (the gzip header's embedded filename/mtime, and every tar
    member's name/type/mode/mtime/uid/gid, so non-target members --
    typically empty marker files/directories used by the firmware's
    install script -- are reproduced as-is)."""
    if data[:2] != b"\x1f\x8b":
        return None
    try:
        decompressed = gzip.decompress(data)
    except OSError:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(decompressed)) as tf:
            members = tf.getmembers()
            candidates = [m for m in members if m.isreg() and m.size > 0]
            if len(candidates) != 1:
                return None
            target = candidates[0]
            content = tf.extractfile(target).read()
    except tarfile.TarError:
        return None

    if not ImageFs.looks_like_imagefs(content):
        return None

    gz_filename = None
    if len(data) > 10 and (data[3] & 0x08):
        end = data.index(b"\x00", 10)
        gz_filename = data[10:end].decode("latin-1")
    gz_mtime = int.from_bytes(data[4:8], "little")

    wrap_info = {
        "type": "gzip+tar",
        "gzip_filename": gz_filename,
        "gzip_mtime": gz_mtime,
        "target_member": target.name,
        "members": [
            {
                "name": m.name,
                "type": m.type.decode("latin-1") if isinstance(m.type, bytes) else m.type,
                "mode": m.mode,
                "mtime": m.mtime,
                "uid": m.uid,
                "gid": m.gid,
            }
            for m in members
        ],
    }
    return content, wrap_info


def _rewrap_gzip_tar(imagefs_bytes: bytes, wrap_info: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for m in wrap_info["members"]:
            ti = tarfile.TarInfo(name=m["name"])
            ti.type = m["type"].encode("latin-1") if isinstance(m["type"], str) else m["type"]
            ti.mode = m["mode"]
            ti.mtime = m["mtime"]
            ti.uid = m["uid"]
            ti.gid = m["gid"]
            if m["name"] == wrap_info["target_member"]:
                ti.size = len(imagefs_bytes)
                tf.addfile(ti, io.BytesIO(imagefs_bytes))
            else:
                ti.size = 0
                tf.addfile(ti, io.BytesIO(b""))
    tar_bytes = buf.getvalue()

    out = io.BytesIO()
    gz_kwargs = {"mode": "wb", "fileobj": out, "mtime": wrap_info.get("gzip_mtime") or 0}
    if wrap_info.get("gzip_filename"):
        gz_kwargs["filename"] = wrap_info["gzip_filename"]
    with gzip.GzipFile(**gz_kwargs) as gz:
        gz.write(tar_bytes)
    return out.getvalue()


def _entries_equal(a: list, b: list) -> bool:
    """True if two DirEntry lists describe the same directory structure
    and file content, ignoring nothing -- used to detect a true no-op
    repack (see pack_vbf)."""
    if len(a) != len(b):
        return False
    for e1, e2 in zip(a, b):
        if (e1.kind, e1.path, e1.ino, e1.mode, e1.gid, e1.uid, e1.mtime, e1.extattr_offset) != \
           (e2.kind, e2.path, e2.ino, e2.mode, e2.gid, e2.uid, e2.mtime, e2.extattr_offset):
            return False
        if e1.kind == "file" and e1.data != e2.data:
            return False
        if e1.kind == "symlink" and e1.target != e2.target:
            return False
        if e1.kind == "device" and (e1.dev, e1.rdev) != (e2.dev, e2.rdev):
            return False
    return True


def find_imagefs_sections(vbf: VbfFile) -> dict:
    """Returns {section_index: (imagefs_bytes, wrap_info_or_None)} for
    every section that contains an imagefs blob, directly or wrapped in
    gzip+tar."""
    found = {}
    for i in range(vbf.sections_count()):
        raw = vbf.get_section_raw(i)
        if ImageFs.looks_like_imagefs(raw):
            found[i] = (raw, None)
            continue
        result = _try_unwrap_gzip_tar(raw)
        if result is not None:
            found[i] = result
    return found


def unpack_vbf(vbf_path, out_dir) -> int:
    vbf = VbfFile()
    vbf.open_file(vbf_path)

    found = find_imagefs_sections(vbf)
    if not found:
        print(f"No QNX image filesystem found in any section of '{vbf_path}'.")
        return 1

    out_dir = Path(out_dir)
    for idx, (data, wrap_info) in sorted(found.items()):
        section_dir = out_dir / f"section_{idx}"
        ifs = ImageFs()
        ifs.parse(data)
        ifs.export(section_dir)
        with open(section_dir / "wrapping.json", "w") as f:
            json.dump({"section_index": idx, "wrap": wrap_info}, f, indent=2)
        kind = "raw" if wrap_info is None else "gzip+tar wrapped"
        print(
            f"Section {idx} ({kind}): {len(ifs.entries)} entries, "
            f"{len(ifs.list_files())} files -> {section_dir}"
        )
    return 0


def pack_vbf(vbf_path, exported_dir, out_path) -> int:
    vbf = VbfFile()
    vbf.open_file(vbf_path)

    exported_dir = Path(exported_dir)
    section_dirs = sorted(exported_dir.glob("section_*"))
    if not section_dirs:
        raise ImageFsError(f"No 'section_*' directories found under '{exported_dir}'.")

    original_found = find_imagefs_sections(vbf)

    for section_dir in section_dirs:
        wrapping_path = section_dir / "wrapping.json"
        try:
            with open(wrapping_path) as f:
                wrapping = json.load(f)
        except OSError as e:
            raise ImageFsError(f"Could not open '{wrapping_path}': {e}")
        idx = wrapping["section_index"]
        wrap_info = wrapping["wrap"]

        ifs = ImageFs()
        ifs.import_config(section_dir / "imagefs_config.json")

        # Decide whether anything was actually edited by comparing
        # entry-level content (paths/metadata/data) against a fresh
        # parse of what's currently in the VBF -- NOT by comparing
        # rebuilt bytes, since a rebuild can never exactly reproduce the
        # original's unrecoverable tail padding or its checksum trailer
        # (see imagefs.py's module docstring) even when nothing changed.
        original_section = vbf.get_section_raw(idx)
        original_entry = original_found.get(idx)
        unchanged = False
        if original_entry is not None:
            orig_ifs = ImageFs()
            orig_ifs.parse(original_entry[0])
            unchanged = _entries_equal(orig_ifs.entries, ifs.entries)

        if unchanged:
            new_section_bytes = original_section
            print(f"Section {idx}: unchanged, keeping the original bytes as-is.")
        elif wrap_info is None:
            new_section_bytes = ifs.save_to_vector()
            print(f"Section {idx}: repacked ({len(new_section_bytes)} bytes).")
        else:
            imagefs_bytes = ifs.save_to_vector()
            new_section_bytes = _rewrap_gzip_tar(imagefs_bytes, wrap_info)
            print(f"Section {idx}: repacked and re-wrapped in gzip+tar ({len(new_section_bytes)} bytes).")

        vbf.replace_section_raw(idx, new_section_bytes)

    vbf.save_to_file(out_path)
    print(f"Wrote '{out_path}'.")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="imagefsunpacker.py",
        description="QNX image filesystem unpacker/repacker for Ford/Volvo IPC .vbf files",
    )
    p.add_argument("-u", "--unpack", action="store_true", help="Find and unpack every image filesystem in a VBF")
    p.add_argument("-p", "--pack", action="store_true", help="Repack edited resources into a patched VBF")
    p.add_argument("-e", "--exported", help="Exported directory to repack from (required for -p)")
    p.add_argument("-o", "--output", help="Output directory (-u) or output VBF path (-p)")
    p.add_argument("-v", "--vbf", help="VBF file to read from / patch")
    p.add_argument("vbf_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    vbf_path = args.vbf or args.vbf_pos
    if not args.pack and not args.unpack:
        print("Please specify a mode: -u/--unpack or -p/--pack.")
        return 0
    if not vbf_path:
        print("Please specify a VBF file (-v/--vbf or as a positional argument).")
        return 0
    vbf_path = Path(vbf_path)
    if not vbf_path.exists():
        print(f"VBF file not found: '{vbf_path}'")
        return 0

    try:
        if args.unpack:
            out_dir = Path(args.output) if args.output else Path.cwd() / "imagefs_exported"
            return unpack_vbf(vbf_path, out_dir)
        else:
            if not args.exported:
                print("Please specify the exported directory to repack from with -e/--exported.")
                return 0
            if not args.output:
                print("Please specify an output VBF path with -o/--output.")
                return 0
            return pack_vbf(vbf_path, args.exported, args.output)
    except (VbfError, ImageFsError) as e:
        print(f"Error: {e}")
        return -1


if __name__ == "__main__":
    sys.exit(main())
