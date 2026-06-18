#!/usr/bin/env python3
"""
imgunpacker.py - Ford IMG resources unpacker ("imgunpkr").

Python port of FTools-5.0's ImgUnpacker command-line tool. Works directly on
a .vbf firmware file: unpacks every image/font resource from VBF section 1
to individual .eif/.bmp/.ttf files plus an editable export_list.csv, or
repacks edited resources (dropped into the exported custom/ directory) back
into a new patched.vbf.

Usage:
    imgunpacker.py -u -o ./exported_dir firmware.vbf
    imgunpacker.py -p -c ./exported_dir/export_list.csv -o ./out_dir firmware.vbf
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftools_lib.img_unpacker import pack_img, unpack_img


def build_parser():
    p = argparse.ArgumentParser(
        prog="imgunpacker.py",
        description="Ford IMG resources unpacker",
    )
    p.add_argument("-p", "--pack", action="store_true", help="Pack VBF file")
    p.add_argument("-u", "--unpack", action="store_true", help="Unpack VBF file")
    p.add_argument("-c", "--conf", help="Config file")
    p.add_argument("-o", "--output", help="Output path")
    p.add_argument("-v", "--vbf", help="VBF file which will be patched")
    p.add_argument("vbf_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    vbf_path = args.vbf or args.vbf_pos
    if args.pack and not args.conf:
        print("Please specify a config file with -c/--conf.")
        return 0
    if args.pack and not Path(args.conf).exists():
        print(f"Config file not found: '{args.conf}'")
        return 0
    if not args.pack and not args.unpack:
        print("Please specify a mode: -p/--pack or -u/--unpack.")
        return 0
    if not vbf_path:
        print("Please specify a VBF file (-v/--vbf or as a positional argument).")
        return 0
    vbf_path = Path(vbf_path)
    if not vbf_path.exists():
        print(f"VBF file not found: '{vbf_path}'")
        return 0

    if args.output:
        out_path = Path(args.output)
        if not out_path.is_dir():
            print(f"Output directory not found: '{out_path}'")
            return 0
    else:
        out_path = vbf_path.parent

    try:
        if args.pack:
            pack_img(args.conf, vbf_path, out_path)
        else:
            unpack_img(vbf_path, out_path)
    except RuntimeError as e:
        print(f"Error: {e}")
        return -1

    return 0


if __name__ == "__main__":
    sys.exit(main())
