#!/usr/bin/env python3
"""
textsectionparser.py - Ford IPC text resources extractor ("textparser").

Python port of FTools-5.0's TextSectionParser command-line tool. Operates
on a raw VBF text-section binary blob.

Usage:
    textsectionparser.py -u -i text_section.bin -o ./exported_dir
    textsectionparser.py -p -i ./exported_dir/text_section.bin -o ./patched.bin

Note: for -p/--pack, -i must point to the ORIGINAL binary template (the
same file that was unpacked), and ui_texts.csv / ui_alerts.csv must sit in
that file's directory -- this mirrors the original tool exactly.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftools_lib import text_section as ts
from ftools_lib.utils import read_file


def build_parser():
    p = argparse.ArgumentParser(
        prog="textsectionparser.py",
        description="Ford IPC text resources extractor",
    )
    p.add_argument("-u", "--unpack", action="store_true", help="Extract text resources form to destination dir")
    p.add_argument("-p", "--pack", action="store_true", help="Pack text section")
    p.add_argument("-i", "--input", help="Input file")
    p.add_argument("input_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    p.add_argument("-o", "--output", default="", help="Output directory")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    input_path = args.input or args.input_pos
    if not input_path:
        print("Please specify an input file with -i/--input.", file=sys.stderr)
        return -1
    input_path = Path(input_path)

    out_path = Path(args.output) if args.output else Path.cwd()

    try:
        if args.pack:
            ts.pack(input_path, out_path)
        elif args.unpack:
            ts.unpack(read_file(input_path), out_path)
        else:
            parser.print_help(sys.stderr)
            return -1
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return -1

    return 0


if __name__ == "__main__":
    sys.exit(main())
