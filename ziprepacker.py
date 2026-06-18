#!/usr/bin/env python3
"""
ziprepacker.py - Replace content in zip file.

Python port of FTools-5.0's ZipRepacker ("ZipRepacker") command-line tool.
Reads a single-entry zip's filename, then writes a brand-new zip (same
filename, new content) using Python's stdlib zipfile -- which, when writing
to a seekable file as we do here, already omits the streaming
data-descriptor bit, matching the original's miniz-based writer. We also
set create_system=0 (MS-DOS host) and external_attr=0x20 (DOS
FILE_ATTRIBUTE_ARCHIVE) on the entry to mirror miniz's defaults.

Usage:
    ziprepacker.py -i original.zip -c new_content.eif -o repacked.zip

Note: if -o/--output is omitted, the original tool's default output name is
literally the input filename with the string "repacked.zip" appended (e.g.
"image.zip" -> "image.ziprepacked.zip") -- not a sensible extension swap,
but preserved here exactly since it's the documented default behavior.
"""

import argparse
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftools_lib.utils import read_file


def build_parser():
    p = argparse.ArgumentParser(
        prog="ziprepacker.py",
        description="Replace content in zip file",
    )
    p.add_argument("-i", "--input", help="Input zip file")
    p.add_argument("input_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    p.add_argument("-c", "--content", help="Content eif file")
    p.add_argument("-o", "--output", help="Output zip file")
    return p


def repack_zip(input_zip: Path, content_file: Path, out_zip: Path) -> int:
    try:
        with zipfile.ZipFile(input_zip) as zf:
            names = zf.namelist()
            if not names:
                print("Could not read the entry filename from the input zip: it is empty.", file=sys.stderr)
                return -1
            entry_name = names[0]
    except (zipfile.BadZipFile, OSError) as e:
        print(f"Could not open the input zip file (it may be corrupt or not a valid zip): {e}", file=sys.stderr)
        return -1

    try:
        content_data = read_file(content_file)
    except RuntimeError as e:
        print(f"Could not read content file: {e}", file=sys.stderr)
        return -1

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zi = zipfile.ZipInfo(entry_name)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.create_system = 0
        zi.external_attr = 0x20
        zf.writestr(zi, content_data)

    try:
        out_zip.write_bytes(buf.getvalue())
    except OSError as e:
        print(f"Could not write the output zip file: {e}", file=sys.stderr)
        return -1

    return 0


def main():
    parser = build_parser()
    args = parser.parse_args()

    input_file_name = args.input or args.input_pos
    if not input_file_name:
        print("Please specify an input zip file with -i/--input.")
        return 0

    if not args.content:
        print("Please specify a content file with -c/--content.")
        return 0
    content_file_name = args.content

    out_file_name = args.output if args.output else (input_file_name + "repacked.zip")

    in_path = Path(input_file_name)
    content_path = Path(content_file_name)
    out_path = Path(out_file_name)

    if not in_path.exists():
        print(f"Input zip file not found: '{in_path}'", file=sys.stderr)
        return 0

    if not content_path.exists():
        print(f"Content file not found: '{content_path}'", file=sys.stderr)
        return 0

    return repack_zip(in_path, content_path, out_path)


if __name__ == "__main__":
    sys.exit(main())
