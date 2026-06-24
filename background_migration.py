"""Relocates the 4 background pieces (left/center/right_active_cut,
center_quiet_cut) to full-screen target dimensions within a VBF's bitmaps.bin
imagefs blob, freeing the space they need by de-duplicating the startup
needle-sweep animation frames and defragmenting the result.

This deliberately never grows the overall imagefs - every step only
relocates bytes within space that already exists, by design (growing the
structure freely is what caused corruption in earlier, naive resize
attempts - there's apparently a fixed size budget downstream).

Trade-off: de-duplicating the major_pointer_* startup animation frames
freezes that needle-sweep intro to a single static frame. This is real,
visible, and not free - it's the price of the freed space.

Pipeline:
    join_major_pointers(blob)  - alias 58 of 59 startup-pointer frames to
                                  one frame's data, zeroing the rest
    defragment(blob)           - consolidate every resulting gap into one
                                  contiguous free-space block
    migrate_to_full_screen(blob) - relocate the 4 background pieces into
                                  that block at their full-screen target
                                  size, as blank (transparent) canvases
                                  ready for custom artwork
"""
import struct
import sys

sys.path.insert(0, '/home/claude/build/ftools-python')
import ftools_lib.imagefs as ifmod

# Exact full-screen target dimensions for the 4 relocated background pieces.
TARGET_DIMENSIONS = {
    "images/variant/backgrounds/left_active_cut.ddb":   {"width": 440, "height": 448, "realWidth": 512,  "realHeight": 448},
    "images/variant/backgrounds/right_active_cut.ddb":  {"width": 424, "height": 474, "realWidth": 512,  "realHeight": 474},
    "images/variant/backgrounds/center_active_cut.ddb": {"width": 796, "height": 448, "realWidth": 1024, "realHeight": 448},
    "images/variant/backgrounds/center_quiet_cut.ddb":  {"width": 822, "height": 281, "realWidth": 1024, "realHeight": 281},
}
MIGRATE_FILES = list(TARGET_DIMENSIONS.keys())


def parse_files(blob):
    unpacked = struct.unpack_from(ifmod._HEADER_FIXED_FMT, blob, 0)
    ints = unpacked[2:]
    image_size, hdr_dir_size, dir_offset = ints[0:3]
    files = []
    dpos = dir_offset
    while True:
        size = struct.unpack_from("<H", blob, dpos)[0]
        if size == 0:
            break
        _size, extattr_offset, ino, mode, gid, uid, mtime = struct.unpack_from(ifmod._ATTR_FMT, blob, dpos)
        rest = blob[dpos + ifmod._ATTR_SIZE:dpos + size]
        if (mode & ifmod.S_IFMT) == ifmod.S_IFREG:
            extra_sz = struct.calcsize(ifmod._FILE_EXTRA_FMT)
            offset, fsize = struct.unpack_from(ifmod._FILE_EXTRA_FMT, rest, 0)
            path = rest[extra_sz:].split(b'\x00')[0].decode('latin-1')
            files.append({'path': path, 'header_offset': dpos + ifmod._ATTR_SIZE,
                          'offset': offset, 'size': fsize, 'dpos': dpos})
        dpos += size
    return files, image_size, dir_offset


def compute_free_spaces(files, blob_len):
    """Gaps between consecutive files (sorted by data offset) within the
    blob - i.e. byte ranges no current file table entry references."""
    files_sorted = sorted(files, key=lambda f: f['offset'])
    free_spaces = []
    for i in range(1, len(files_sorted)):
        f, g = files_sorted[i - 1], files_sorted[i]
        if f['offset'] == g['offset'] and f['size'] == g['size']:
            continue
        v = f['offset'] + f['size']
        e = g['offset']
        if v != e:
            free_spaces.append({'from': v, 'to': e, 'size': e - v})
    last = files_sorted[-1]
    trailing_end = blob_len - 8
    if last['offset'] + last['size'] != trailing_end:
        free_spaces.append({'from': last['offset'] + last['size'], 'to': trailing_end,
                            'size': trailing_end - (last['offset'] + last['size'])})
    return free_spaces


def find_file(files, path):
    return next(f for f in files if f['path'] == path)


def write_u32(blob, off, val):
    struct.pack_into('<I', blob, off, val)


def join_major_pointers(blob):
    """Alias every major_pointer_* file (except 017) to 017's data, zeroing
    their old (now-unreferenced) regions. Frees real, detectable space -
    but at the cost of freezing the startup needle-sweep animation to a
    single static frame."""
    files, image_size, dir_offset = parse_files(blob)
    ref = next(f for f in files if 'major_pointer_017' in f['path'])
    targets = [f for f in files if 'major_pointer_' in f['path'] and 'major_pointer_017' not in f['path']]
    freed = 0
    for f in targets:
        write_u32(blob, f['header_offset'], ref['offset'])
        write_u32(blob, f['header_offset'] + 4, ref['size'])
        blob[f['offset']:f['offset'] + f['size']] = bytes(f['size'])
        freed += f['size']
    return freed


def defragment(blob):
    """Consolidate free space by sliding every file after the first gap down
    to close it. A single pass starting from the earliest gap inherently
    closes every later gap too, since all files after that point get packed
    with zero spacing between them. Correctly handles the case where
    multiple directory entries alias the same source data (e.g. after
    join_major_pointers) by only copying bytes once per distinct source
    offset.
    """
    files, image_size, dir_offset = parse_files(blob)
    free_spaces = compute_free_spaces(files, len(blob))
    if not free_spaces:
        return 0
    gap = free_spaces[0]
    files_sorted = sorted(files, key=lambda f: f['offset'])
    to_shift = [f for f in files_sorted if f['offset'] > gap['from']]

    c = gap['from']
    prev = None
    moved = 0
    for f in to_shift:
        if prev is not None and f['offset'] != prev['offset']:
            c += prev['size']
        write_u32(blob, f['header_offset'], c)
        if prev is None or f['offset'] != prev['offset']:
            data = bytes(blob[f['offset']:f['offset'] + f['size']])
            blob[c:c + f['size']] = data
            moved += 1
        prev = f
    return moved


def migrate_to_full_screen(blob):
    """Relocate the 4 background pieces to their full-screen target
    dimensions, in free space. New header is (width,height) from
    TARGET_DIMENSIONS plus [type_byte=2, byte5=128, format=4]; pixel data is
    left blank/transparent (the user fills it with their own artwork
    afterward); the old location is zeroed."""
    sizes = []
    for path in MIGRATE_FILES:
        g = TARGET_DIMENSIONS[path]
        sizes.append(g['realWidth'] * (g['realHeight'] + 1) * 2 + 8)
    total_needed = sum(sizes)

    files, image_size, dir_offset = parse_files(blob)
    free_spaces = compute_free_spaces(files, len(blob))
    candidate = next((fs for fs in free_spaces if fs['size'] > total_needed), None)
    if candidate is None:
        raise RuntimeError(
            f"No free space region is large enough for migration "
            f"(need {total_needed} bytes; largest available: "
            f"{max((fs['size'] for fs in free_spaces), default=0)} bytes)."
        )

    c = candidate['from']
    for path, A in zip(MIGRATE_FILES, sizes):
        f = find_file(files, path)
        g = TARGET_DIMENSIONS[path]

        new_header = bytearray(8)
        struct.pack_into('<H', new_header, 0, g['width'])
        struct.pack_into('<H', new_header, 2, g['height'])
        new_header[4:8] = bytes([2, 128, 4, 0])

        blob[c:c + 8] = bytes(new_header)
        blob[c + 8:c + A] = bytes(A - 8)  # blank/transparent pixel data

        write_u32(blob, f['header_offset'], c)
        write_u32(blob, f['header_offset'] + 4, A)
        blob[f['offset']:f['offset'] + f['size']] = bytes(f['size'])

        c += A

    return total_needed


def run_full_migration(vbf_path, out_path, section_idx=18, skip_join=False):
    """End-to-end: unwrap the imagefs section, run the full pipeline, rewrap,
    and write a new VBF. Returns a short summary string."""
    import gzip
    import sys as _sys
    _sys.path.insert(0, '/home/claude/build/ftools-python')
    from ftools_lib.vbf import VbfFile
    import imagefsunpacker as ifu

    vbf = VbfFile()
    vbf.open_file(str(vbf_path))
    sec_data = vbf.get_section_raw(section_idx)

    result = ifu._try_unwrap_gzip_tar(sec_data)
    if result is None:
        raise RuntimeError(f"Section {section_idx} isn't a gzip+tar-wrapped imagefs - "
                            f"check this is the right section index for this VBF.")
    imagefs_bytes, wrap_info = result
    blob = bytearray(imagefs_bytes)

    lines = []
    if not skip_join:
        freed = join_major_pointers(blob)
        lines.append(f"de-duplicated startup-pointer animation frames, freed {freed:,} bytes "
                      f"(trade-off: that animation is now a single static frame)")

    moved = defragment(blob)
    lines.append(f"defragmented free space ({moved} entries repositioned)")

    needed = migrate_to_full_screen(blob)
    lines.append(f"relocated {len(MIGRATE_FILES)} background pieces to full-screen size "
                 f"({needed:,} bytes)")

    new_section_bytes = ifu._rewrap_gzip_tar(bytes(blob), wrap_info)
    vbf.replace_section_raw(section_idx, new_section_bytes)
    vbf.save_to_file(str(out_path))
    return lines


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("vbf", help="Input graphics VBF (the one containing the background .ddb files)")
    p.add_argument("output", help="Output VBF path")
    p.add_argument("--section", type=int, default=18,
                   help="Section index containing the imagefs (default: 18, "
                        "matching ftool.py's own numbering - check with "
                        "'ftool.py unpack' first if unsure)")
    p.add_argument("--skip-join", action="store_true",
                   help="Skip de-duplicating the startup-pointer animation frames "
                        "(keeps that animation intact, but there may not be enough "
                        "free space for the migration without it)")
    args = p.parse_args()

    print(f"Opening {args.vbf} …")
    try:
        lines = run_full_migration(args.vbf, args.output, section_idx=args.section,
                                   skip_join=args.skip_join)
    except Exception as e:
        print(f"Error: {e}")
        return 1
    for line in lines:
        print(f"  - {line}")
    print(f"Wrote {args.output}")
    print("The 4 relocated backgrounds are now blank/transparent canvases at full-screen "
          "size, ready for your own artwork.")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())

