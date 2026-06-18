#!/usr/bin/env python3
"""
eifconverter.py - Ford EBD.EIF <-> BMP converter.

Python port of FTools-5.0's EifViewer ("eifconverter") command-line tool.

Usage:
    eifconverter.py -U -i image.eif -o image.bmp
    eifconverter.py -P -d 16 -i image.bmp -o image.eif
    eifconverter.py -B -i ./bmp_dir -o ./eif_dir
    eifconverter.py -U -i image.eif -s palette.bin -o image.bmp
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse

from ftools_lib import eif as eifmod
from ftools_lib.utils import read_file


def build_parser():
    p = argparse.ArgumentParser(
        prog="eifconverter.py",
        description="Ford EBD.EIF converter",
    )
    p.add_argument("-P", "--pack", action="store_true", help="Pack EIF file")
    p.add_argument("-U", "--unpack", action="store_true", help="Unpack EIF file")
    p.add_argument("-B", "--bulk", action="store_true", help="Bulk mode. Create shared palette for images set")
    p.add_argument("-d", "--depth", type=int, default=0, help="Output Eif type 8/16/32")
    p.add_argument("-i", "--input", help="Input file")
    p.add_argument("input_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    p.add_argument("-o", "--output", help="Output file")
    p.add_argument("-s", "--scheme", help="(optional) Use external palette when unpack and pack")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    input_path = args.input or args.input_pos
    if not input_path:
        print("Please specify an input file with -i/--input.")
        return 0
    input_path = Path(input_path)

    palette_path = Path(args.scheme) if args.scheme else None

    if args.output:
        out_path = Path(args.output)
    else:
        out_path = input_path
        if input_path.is_file():
            out_path = out_path.with_suffix(".bmp" if args.unpack else ".eif")

    try:
        if args.bulk:
            eifmod.bulk_pack(input_path, out_path)

        elif args.unpack:
            eif_data = read_file(input_path)
            eifmod.eif_to_bmp_file(eif_data, out_path, palette_path=palette_path, store_palette=True)

        elif args.pack:
            if args.depth not in (8, 16, 32):
                print("Invalid color depth: must be 8, 16, or 32.")
                return 0
            eifmod.bmp_file_to_eif_file(input_path, args.depth, out_path, palette_path=palette_path)

        else:
            parser.print_help()

    except RuntimeError as e:
        print(f"Error: {e}")
        return -1
    except OSError as e:
        print(f"File error: {e}")
        return -1

    return 0


if __name__ == "__main__":
    sys.exit(main())
