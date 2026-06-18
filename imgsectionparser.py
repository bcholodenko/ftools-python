#!/usr/bin/env python3
"""
imgsectionparser.py - Ford IPC images extractor ("imgparcer").

Python port of FTools-5.0's ImgSectionParser command-line tool. Operates on
a raw VBF section-1 binary blob (e.g. one previously extracted via
vbfeditor.py's -u/--unpack, or via vbfeditor.py -I/--info to find the right
section index).

Usage:
    imgsectionparser.py -u -i section_1.bin -o ./exported_dir
    imgsectionparser.py -p -i ./exported_dir/_config.json -o ./patched.bin
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftools_lib.image_section import ImageSection


def build_parser():
    p = argparse.ArgumentParser(
        prog="imgsectionparser.py",
        description="Ford IPC images extractor",
    )
    p.add_argument("-u", "--unpack", action="store_true",
                    help="Extract resources form image section to destination dir")
    p.add_argument("-p", "--pack", action="store_true", help="Pack image section")
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

    if not args.output:
        out_path = Path.cwd() / "patched.bin"
    else:
        out_path = Path(args.output)
        if not out_path.is_dir() and not out_path.parent.is_dir():
            print(f"Output directory does not exist: '{out_path.parent}'", file=sys.stderr)
            return -1

    try:
        if args.pack:
            section = ImageSection()
            section.import_config(input_path)
            section.save_to_file(out_path)
        elif args.unpack:
            section = ImageSection()
            section.parse_file(input_path)
            section.export(out_path)
        else:
            parser.print_help(sys.stderr)
            return -1
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return -1

    return 0


if __name__ == "__main__":
    sys.exit(main())
