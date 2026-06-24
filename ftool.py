#!/usr/bin/env python3
"""
ftool.py - Master Ford/Volvo IPC firmware workspace tool.

Unpacks a .vbf firmware file into an editable workspace directory, or
repacks a previously-unpacked workspace back into a patched .vbf.

Every sub-tool (imagefsunpacker, imgunpacker, textsectionparser, ddbconverter,
eifconverter) is invoked automatically based on what is detected inside the
VBF -- no manual tool-chaining required.

Usage:
    ftool.py unpack firmware.vbf ./workspace/
    ftool.py pack   ./workspace/ patched.vbf

Workspace layout after unpack
──────────────────────────────
  workspace/
    manifest.json                  ← roundtrip bookkeeping (do not edit)
    section_NN_<type>/             ← one directory per non-trivial section
      <type-specific contents>     ← see below per type

Section types detected and what gets unpacked:

  imagefs  (gzip+tar wrapped or raw QNX imagefs blob)
    files/                         ← all imagefs files laid out as real paths
      images/variant/.../foo.ddb   ← original .ddb (always kept for roundtrip)
      images/variant/.../foo.png   ← decoded PNG for editing
    imagefs_config.json            ← imagefs metadata (required by pack)
    wrapping.json                  ← gzip+tar wrapper metadata (required by pack)

  imgsection  (FTools ImageSection zip+EIF blob, typically section 1)
    bmp/                           ← all images as editable .bmp files
    eif/                           ← original .eif files (kept for roundtrip)
    ttf/                           ← font files
    header_lines.csv               ← image header records
    export_list.csv                ← image index/name/depth/palette catalogue
    custom/                        ← drop replacement .bmp/.ttf files here

  textsection  (Ford text-string binary blob)
    text_section.bin               ← original binary (required as pack template)
    ui_texts.csv                   ← editable UI strings
    ui_alerts.csv                  ← editable alert strings

  raw  (anything not recognised above)
    data.bin                       ← verbatim binary dump

Editing workflow:
  - imagefs .ddb files: edit the .png; pack uses it automatically if newer
    than the .ddb, otherwise the original .ddb passes through unchanged.
  - imgsection images: drop replacement .bmp/.ttf into custom/; pack picks
    them up from there (same as imgunpacker.py's workflow).
  - textsection: edit ui_texts.csv / ui_alerts.csv in-place.
  - raw / unknown: edit data.bin in-place.
"""

import argparse
import json
import os
import shutil
import struct
import sys
from pathlib import Path

# ── locate the ftools_lib package next to this script ─────────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from ftools_lib.vbf import VbfFile, VbfError
from ftools_lib.imagefs import ImageFs, ImageFsError
from ftools_lib.image_section import ImageSection
from ftools_lib.text_section import STRUCT_SIZE as _TEXT_SECTION_STRUCT_SIZE
from ftools_lib import text_section as ts
from ftools_lib import ddb as ddbmod
from ftools_lib.utils import read_file, write_file
import imagefsunpacker as _ifsmod
import ftools_lib.img_unpacker as _imgmod


# ═══════════════════════════════════════════════════════════════════════════════
# Section type detection
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_section_type(raw: bytes) -> str:
    """Return a string tag for the content type of a VBF section's raw bytes."""
    if len(raw) < 4:
        return "raw"
    # QNX imagefs (raw)
    if ImageFs.looks_like_imagefs(raw):
        return "imagefs"
    # gzip — might wrap an imagefs (gzip+tar) or something else
    if raw[:2] == b"\x1f\x8b":
        result = _ifsmod._try_unwrap_gzip_tar(raw)
        if result is not None:
            return "imagefs"
    # FTools ImageSection: starts with a specific header validated by parse()
    try:
        sec = ImageSection()
        sec.parse(raw)
        return "imgsection"
    except Exception:
        pass
    # Ford text section: recognised purely by size
    if len(raw) == _TEXT_SECTION_STRUCT_SIZE:
        return "textsection"
    return "raw"


# ═══════════════════════════════════════════════════════════════════════════════
# Per-type unpack helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _unpack_imagefs(section_idx: int, raw: bytes, section_dir: Path, verbose: bool) -> dict:
    """Unpack a QNX imagefs section (raw or gzip+tar wrapped).
    Returns the per-section manifest entry dict."""
    result = _ifsmod._try_unwrap_gzip_tar(raw)
    if result is not None:
        imagefs_bytes, wrap_info = result
    else:
        imagefs_bytes, wrap_info = raw, None

    ifs = ImageFs()
    ifs.parse(imagefs_bytes)
    ifs.export(section_dir)  # writes imagefs_config.json + all files under files/

    wrap_path = section_dir / "wrapping.json"
    with open(wrap_path, "w") as f:
        json.dump({"section_index": section_idx, "wrap": wrap_info}, f, indent=2)

    # Convert supported .ddb files to .png alongside the originals.
    # Set the .png mtime one second BEHIND the .ddb so that pack's
    # "png newer than ddb?" test correctly treats a freshly-unpacked,
    # unedited workspace as a no-op.
    ddb_converted = 0
    ddb_skipped = 0
    files_dir = section_dir / "files"
    for ddb_path in sorted(files_dir.rglob("*.ddb")):
        ddb_data = read_file(ddb_path)
        if not ddbmod.is_supported_ddb(ddb_data):
            ddb_skipped += 1
            continue
        try:
            png_path = ddb_path.with_suffix(".png")
            ddbmod.ddb_to_png_file(ddb_data, png_path, context=str(ddb_path))
            # Backdate the .png so pack treats it as older than the .ddb
            ddb_mtime = ddb_path.stat().st_mtime
            os.utime(png_path, (ddb_mtime - 1, ddb_mtime - 1))
            ddb_converted += 1
        except ddbmod.DdbError as e:
            if verbose:
                print(f"    WARNING: could not decode {ddb_path.name}: {e}")
            ddb_skipped += 1

    kind = "gzip+tar+imagefs" if wrap_info else "imagefs"
    n_files = len(ifs.list_files())
    print(f"  section {section_idx:2d}  [{kind}]  {n_files} files, "
          f"{ddb_converted} .ddb→.png, {ddb_skipped} .ddb unsupported")

    return {
        "section_index": section_idx,
        "type": "imagefs",
        "dir": section_dir.name,
    }


def _unpack_imgsection(section_idx: int, raw: bytes, section_dir: Path,
                       vbf_path: Path, verbose: bool) -> dict:
    """Unpack an FTools ImageSection (zip+EIF blob).  Delegates to the
    existing img_unpacker library (which itself opens the VBF), so we just
    point it at the right paths."""
    # img_unpacker.unpack_img() wants (vbf_path, out_dir); it always reads
    # section index 1 from the VBF.  Since the master tool calls this for
    # whichever section is the imgsection, we use the same approach but
    # delegate knowing the section may not be index 1.  For now we call the
    # library directly and replicate the minimal logic to avoid hard-coding
    # "section 1".
    section_dir.mkdir(parents=True, exist_ok=True)

    img_sec = ImageSection()
    img_sec.parse(raw)
    ImageSection.header_to_csv(img_sec.header_data, section_dir / "header_lines.csv")

    eifs_path = section_dir / "eif"
    bmps_path = section_dir / "bmp"
    ttf_path  = section_dir / "ttf"
    custom_path = section_dir / "custom"
    for p in (eifs_path, bmps_path, ttf_path, custom_path):
        p.mkdir(parents=True, exist_ok=True)

    import ftools_lib.eif as eifmod
    from ftools_lib.crc import crc16_ccitt_false
    _EIF_HEADER_FMT = "<7sBIHH"

    export_rows = []
    zip_items = img_sec.get_items_count("zip")
    for i in range(zip_items):
        eif_data, eif_name = _imgmod.get_eif_from_img_section(img_sec, i)
        write_file(eifs_path / eif_name, eif_data)
        bmp_path = (bmps_path / eif_name).with_suffix(".bmp")
        eifmod.eif_to_bmp_file(eif_data, bmp_path)
        _sig, eif_type, _length, width, height = struct.unpack_from(_EIF_HEADER_FMT, eif_data, 0)
        crc_str = "0"
        if eif_type == eifmod.EIF_TYPE_MULTICOLOR:
            palette = eif_data[16:16 + 768]
            crc_str = f"{crc16_ccitt_false(palette):04X}"
        depth = eifmod.eif_type_to_depth(eif_type)
        export_rows.append((i, eif_name, depth, crc_str, width, height))

    ttf_items = img_sec.get_items_count("ttf")
    for i in range(ttf_items):
        item_bin = img_sec.get_item_data("ttf", i)
        write_file(ttf_path / f"{i}.ttf", item_bin)
        export_rows.append((i, f"{i}.ttf", 0, "0", 0, 0))

    export_list_path = section_dir / "export_list.csv"
    with open(export_list_path, "w", newline="") as f:
        f.write("Idx,Name,Depth,palette_crc16,Width,Height\n")
        for row in export_rows:
            f.write(",".join(str(v) for v in row) + "\n")

    print(f"  section {section_idx:2d}  [imgsection]  "
          f"{zip_items} images, {ttf_items} fonts → {section_dir.name}/")

    return {
        "section_index": section_idx,
        "type": "imgsection",
        "dir": section_dir.name,
    }


def _unpack_textsection(section_idx: int, raw: bytes, section_dir: Path,
                        verbose: bool) -> dict:
    section_dir.mkdir(parents=True, exist_ok=True)
    # Keep original binary so pack can use it as a template
    bin_path = section_dir / "text_section.bin"
    write_file(bin_path, raw)
    ts.unpack(raw, section_dir)
    print(f"  section {section_idx:2d}  [textsection]  "
          f"ui_texts.csv + ui_alerts.csv → {section_dir.name}/")
    return {
        "section_index": section_idx,
        "type": "textsection",
        "dir": section_dir.name,
    }


def _unpack_raw(section_idx: int, raw: bytes, section_dir: Path) -> dict:
    section_dir.mkdir(parents=True, exist_ok=True)
    write_file(section_dir / "data.bin", raw)
    info_str = f"{len(raw)} bytes"
    print(f"  section {section_idx:2d}  [raw]  {info_str} → {section_dir.name}/data.bin")
    return {
        "section_index": section_idx,
        "type": "raw",
        "dir": section_dir.name,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Per-type pack helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _pack_imagefs(entry: dict, workspace: Path, vbf: VbfFile) -> None:
    """Repack an imagefs section, converting any edited .png files back to
    .ddb first. Files without a corresponding .png, or with an unsupported
    .ddb, pass through unchanged."""
    section_idx = entry["section_index"]
    section_dir = workspace / entry["dir"]
    files_dir = section_dir / "files"

    # Reload the wrapping metadata
    with open(section_dir / "wrapping.json") as f:
        wrapping = json.load(f)
    wrap_info = wrapping["wrap"]

    # Re-encode any .png files that are newer than their .ddb counterpart
    kept_original = []  # (filename, reason) for anything that did NOT take the edit
    for png_path in sorted(files_dir.rglob("*.png")):
        ddb_path = png_path.with_suffix(".ddb")
        if not ddb_path.exists():
            print(f"    WARNING: {png_path.name} has no matching .ddb — skipping")
            kept_original.append((png_path.name, "no matching .ddb"))
            continue
        if png_path.stat().st_mtime <= ddb_path.stat().st_mtime:
            continue  # .png not newer — .ddb is authoritative, no re-encode needed
        ddb_data = read_file(ddb_path)
        if not ddbmod.is_supported_ddb(ddb_data):
            print(f"    WARNING: {ddb_path.name} is unsupported format — "
                  f"cannot re-encode from .png, using original .ddb")
            kept_original.append((ddb_path.name, "unsupported format"))
            continue
        try:
            ddbmod.png_file_to_ddb_file(png_path, ddb_path, ddb_path)
            print(f"    re-encoded {ddb_path.name} from .png")
        except ddbmod.DdbError as e:
            print(f"    WARNING: could not re-encode {ddb_path.name}: {e}")
            kept_original.append((ddb_path.name, str(e)))

    if kept_original:
        print(f"    *** {len(kept_original)} edited .png(s) did NOT make it into this VBF - "
              f"easy to miss among the lines above, so check these: ***")
        for name, reason in kept_original:
            print(f"        - {name}: {reason}")

    # Now (re-)import the imagefs from disk, compare to original, rebuild only if changed.
    # import_config reads the .ddb files from disk (including any just re-encoded above),
    # so _entries_equal sees the actual post-encode content.
    ifs = ImageFs()
    ifs.import_config(section_dir / "imagefs_config.json")

    original_raw = vbf.get_section_raw(section_idx)
    original_result = _ifsmod._try_unwrap_gzip_tar(original_raw)
    if original_result is not None:
        original_imagefs_bytes, _ = original_result
    elif ImageFs.looks_like_imagefs(original_raw):
        original_imagefs_bytes = original_raw
    else:
        original_imagefs_bytes = None

    unchanged = False
    if original_imagefs_bytes is not None:
        orig_ifs = ImageFs()
        orig_ifs.parse(original_imagefs_bytes)
        unchanged = _ifsmod._entries_equal(orig_ifs.entries, ifs.entries)

    if unchanged:
        # Content is byte-identical to original — keep original compressed bytes.
        # This handles both "no PNGs touched" and "PNGs re-encoded losslessly".
        print(f"  section {section_idx:2d}  [imagefs]  unchanged — keeping original bytes")
        return

    imagefs_bytes = ifs.save_to_vector()

    if original_imagefs_bytes is not None and len(imagefs_bytes) > len(original_imagefs_bytes):
        growth = len(imagefs_bytes) - len(original_imagefs_bytes)
        print(
            f"    *** WARNING: this section grew by {growth:,} bytes "
            f"({len(original_imagefs_bytes):,} -> {len(imagefs_bytes):,}). Editing/resizing images "
            f"normally rebuilds this section at whatever size the new content needs, with no "
            f"guarantee the result still fits within whatever space the real hardware actually "
            f"budgets for it - real devices have shown visual corruption after flashing an "
            f"oversized rebuild like this, even though the file itself is well-formed. If you're "
            f"enlarging images significantly, consider relocating them into existing free space "
            f"instead (see background_migration.py) so the overall section size never changes. ***"
        )

    if wrap_info is None:
        new_section_bytes = imagefs_bytes
    else:
        new_section_bytes = _ifsmod._rewrap_gzip_tar(imagefs_bytes, wrap_info)
    print(f"  section {section_idx:2d}  [imagefs]  repacked ({len(new_section_bytes):,} bytes)")
    vbf.replace_section_raw(section_idx, new_section_bytes)


def _pack_imgsection(entry: dict, workspace: Path, vbf: VbfFile) -> None:
    """Repack an ImageSection by invoking the img_unpacker library's
    repack_resources() exactly as imgunpacker.py does."""
    section_idx = entry["section_index"]
    section_dir = workspace / entry["dir"]

    raw = vbf.get_section_raw(section_idx)
    img_sec = ImageSection()
    img_sec.parse(raw)
    img_sec.header_data = ImageSection.header_from_csv(section_dir / "header_lines.csv")

    export_list_path = section_dir / "export_list.csv"
    csv_rows = _imgmod.read_csv(export_list_path)

    custom_dir = section_dir / "custom"
    has_custom = any(custom_dir.iterdir()) if custom_dir.exists() else False
    if not has_custom:
        print(f"  section {section_idx:2d}  [imgsection]  no files in custom/ — keeping original")
        return

    _imgmod.repack_resources(export_list_path, img_sec, csv_rows)
    new_raw = img_sec.save_to_vector()
    vbf.replace_section_raw(section_idx, new_raw)
    print(f"  section {section_idx:2d}  [imgsection]  repacked ({len(new_raw):,} bytes)")


def _pack_textsection(entry: dict, workspace: Path, vbf: VbfFile) -> None:
    section_idx = entry["section_index"]
    section_dir = workspace / entry["dir"]
    bin_path = section_dir / "text_section.bin"
    new_raw = ts.pack(bin_path, section_dir)
    # ts.pack writes a file; read it back
    out_path = section_dir / "text_section_packed.bin"
    if out_path.exists():
        new_raw = read_file(out_path)
    else:
        # ts.pack returns None but writes next to the template;
        # fall back to re-reading the binary from the expected output path
        new_raw = read_file(section_dir / "text_section.bin")
    vbf.replace_section_raw(section_idx, new_raw)
    print(f"  section {section_idx:2d}  [textsection]  repacked ({len(new_raw):,} bytes)")


def _pack_raw(entry: dict, workspace: Path, vbf: VbfFile) -> None:
    section_idx = entry["section_index"]
    section_dir = workspace / entry["dir"]
    data_path = section_dir / "data.bin"
    raw = read_file(data_path)
    original = vbf.get_section_raw(section_idx)
    if raw == original:
        print(f"  section {section_idx:2d}  [raw]  unchanged")
        return
    vbf.replace_section_raw(section_idx, raw)
    print(f"  section {section_idx:2d}  [raw]  updated ({len(raw):,} bytes)")


# ═══════════════════════════════════════════════════════════════════════════════
# Main commands: unpack / pack
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_unpack(vbf_path: Path, workspace: Path, verbose: bool, ignore_crc: bool = False) -> int:
    print(f"Opening {vbf_path.name} …")
    vbf = VbfFile()
    try:
        vbf.open_file(vbf_path, ignore_crc_mismatch=ignore_crc)
    except VbfError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    n = vbf.sections_count()
    print(f"  {n} sections found")

    if workspace.exists() and any(workspace.iterdir()):
        print(f"Error: workspace '{workspace}' already exists and is not empty.\n"
              f"Delete it or choose a different path.", file=sys.stderr)
        return 1
    workspace.mkdir(parents=True, exist_ok=True)

    manifest = {
        "vbf_filename": vbf_path.name,
        "sections_total": n,
        "sections": [],
    }

    raw_count = 0
    for i in range(n):
        raw = vbf.get_section_raw(i)
        kind = _detect_section_type(raw)

        if kind == "imagefs":
            # Distinguish trivial (4-byte sentinel) sections from real imagefs
            if len(raw) <= 8:
                kind = "raw"

        if kind == "raw" and len(raw) <= 8:
            # Tiny sentinel sections (4 bytes, all identical in the reference VBF)
            # are extremely common; batch them silently under a shared directory
            # and only mention them in the manifest.
            raw_count += 1
            manifest["sections"].append({
                "section_index": i,
                "type": "raw_sentinel",
                "dir": None,
            })
            continue

        # Give each section a directory: section_NN_type
        dir_name = f"section_{i:02d}_{kind}"
        section_dir = workspace / dir_name

        if kind == "imagefs":
            entry = _unpack_imagefs(i, raw, section_dir, verbose)
        elif kind == "imgsection":
            entry = _unpack_imgsection(i, raw, section_dir, vbf_path, verbose)
        elif kind == "textsection":
            entry = _unpack_textsection(i, raw, section_dir, verbose)
        else:
            entry = _unpack_raw(i, raw, section_dir)

        manifest["sections"].append(entry)

    if raw_count:
        print(f"  ({raw_count} small sentinel sections stored only in manifest)")

    manifest_path = workspace / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\nWorkspace written to: {workspace}")
    print(f"Edit files under the section directories, then run:")
    print(f"  ftool.py pack {workspace} <output.vbf>")
    return 0


def cmd_pack(workspace: Path, out_vbf: Path, vbf_override: Path | None,
             verbose: bool, ignore_crc: bool = False) -> int:
    manifest_path = workspace / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: no manifest.json in '{workspace}' — was this unpacked with ftool.py?",
              file=sys.stderr)
        return 1

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Find the source VBF: prefer explicit override, then look next to workspace
    vbf_path = vbf_override
    if vbf_path is None:
        # Try to find it next to the workspace, by original filename
        candidate = workspace.parent / manifest["vbf_filename"]
        if candidate.exists():
            vbf_path = candidate
    if vbf_path is None or not vbf_path.exists():
        print(
            f"Error: cannot locate the original VBF '{manifest['vbf_filename']}'.\n"
            f"Place it next to the workspace directory, or specify it with --vbf.",
            file=sys.stderr,
        )
        return 1

    print(f"Opening {vbf_path.name} …")
    vbf = VbfFile()
    try:
        vbf.open_file(vbf_path, ignore_crc_mismatch=ignore_crc)
    except VbfError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print("Repacking sections …")
    for entry in manifest["sections"]:
        kind = entry["type"]
        if kind == "raw_sentinel":
            continue  # tiny sentinels keep their original bytes
        try:
            if kind == "imagefs":
                _pack_imagefs(entry, workspace, vbf)
            elif kind == "imgsection":
                _pack_imgsection(entry, workspace, vbf)
            elif kind == "textsection":
                _pack_textsection(entry, workspace, vbf)
            elif kind == "raw":
                _pack_raw(entry, workspace, vbf)
        except Exception as e:
            print(f"  ERROR in section {entry['section_index']}: {e}", file=sys.stderr)
            if verbose:
                import traceback
                traceback.print_exc()
            return 1

    try:
        vbf.save_to_file(out_vbf)
    except VbfError as e:
        print(f"Error saving VBF: {e}", file=sys.stderr)
        return 1

    print(f"\nWrote {out_vbf}  ({out_vbf.stat().st_size:,} bytes)")
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ftool.py",
        description="Master Ford/Volvo IPC firmware workspace tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1].split("\n\nWorkspace")[0].strip(),
    )
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print extra detail (warnings, tracebacks)")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    up = sub.add_parser("unpack", help="Unpack a .vbf into an editable workspace")
    up.add_argument("vbf", type=Path, help="Input .vbf firmware file")
    up.add_argument("workspace", type=Path, help="Output workspace directory (must not exist)")
    up.add_argument("--ignore-crc", action="store_true",
                    help="Continue even if the VBF's stored CRC-32 doesn't match its actual "
                         "content (e.g. a file that's already been hand-modified elsewhere) - "
                         "prints a warning instead of stopping")

    pk = sub.add_parser("pack", help="Repack a workspace into a patched .vbf")
    pk.add_argument("workspace", type=Path, help="Workspace directory created by 'unpack'")
    pk.add_argument("output", type=Path, help="Output .vbf path")
    pk.add_argument("--vbf", dest="vbf_source", type=Path, default=None,
                    help="Original .vbf to patch (default: auto-detected from manifest)")
    pk.add_argument("--ignore-crc", action="store_true",
                    help="Same as unpack's --ignore-crc, applied when opening the donor .vbf")

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "unpack":
        return cmd_unpack(args.vbf, args.workspace, args.verbose, args.ignore_crc)
    elif args.command == "pack":
        return cmd_pack(args.workspace, args.output,
                        getattr(args, "vbf_source", None), args.verbose, args.ignore_crc)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
