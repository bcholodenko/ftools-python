#!/usr/bin/env python3
"""
ddbconverter.py - .ddb <-> PNG converter for IPC bitmap resources.

Supports all 5 known .ddb format variants, including the two compressed
ones, and round-trips byte-for-byte. See ftools_lib/ddb.py for format
details.

Usage:
    ddbconverter.py -U -i image.ddb -o image.png
    ddbconverter.py -P -i edited.png -d image.ddb -o new.ddb
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse

from ftools_lib import ddb as ddbmod
from ftools_lib.utils import read_file


def build_parser():
    p = argparse.ArgumentParser(
        prog="ddbconverter.py",
        description=".ddb <-> PNG converter",
    )
    p.add_argument("-P", "--pack", action="store_true", help="Pack a PNG back into .ddb format")
    p.add_argument("-U", "--unpack", action="store_true", help="Unpack a .ddb file to PNG")
    p.add_argument("-i", "--input", help="Input file (.ddb for -U, .png for -P)")
    p.add_argument("input_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    p.add_argument("-o", "--output", help="Output file")
    p.add_argument(
        "-d", "--donor",
        help="(required for -P) the original .ddb file this PNG came from",
    )
    p.add_argument(
        "--no-dither", action="store_true",
        help="Disable Floyd-Steinberg dithering when packing (-P); results in visible "
             "color banding on gradients but matches the literal nearest 5-bit level.",
    )
    p.add_argument(
        "--efs", action="store_true",
        help="This .ddb came from an EXE-VBF's efs.bin container (extracted via "
             "ftools_lib.efs.EfsFs), not a graphics-VBF's bitmaps.bin imagefs - uses "
             "the different realWidth rounding rule efs.bin files need. Without this "
             "flag, efs.bin-sourced files will very likely round-trip with corrupted "
             "pixel data, since the row stride assumption is wrong for them. Make sure "
             "-i/-d keeps the file's real original name (or close to it) when using "
             "this flag - it doubles as the lookup key for two known named exceptions "
             "to the realWidth rule (see ftools_lib/ddb.py's _EFS_REALWIDTH_OVERRIDES).",
    )
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    input_path = args.input or args.input_pos
    if not input_path:
        print("Please specify an input file with -i/--input.")
        return 0
    input_path = Path(input_path)
    width_convention = "efs" if args.efs else "bitmaps"

    try:
        if args.unpack:
            out_path = Path(args.output) if args.output else input_path.with_suffix(".png")
            data = read_file(input_path)
            ddbmod.ddb_to_png_file(data, out_path, context=str(input_path),
                                   width_convention=width_convention)
            print(f"Wrote {out_path}")

        elif args.pack:
            if not args.donor:
                print("Packing requires -d/--donor (the original .ddb this PNG came from).")
                return 0
            out_path = Path(args.output) if args.output else Path(args.donor)
            ddbmod.png_file_to_ddb_file(input_path, Path(args.donor), out_path,
                                        dither=not args.no_dither,
                                        width_convention=width_convention)
            print(f"Wrote {out_path}")

        else:
            parser.print_help()

    except ddbmod.DdbError as e:
        print(f"Error: {e}")
        return -1
    except OSError as e:
        print(f"File error: {e}")
        return -1

    return 0


if __name__ == "__main__":
    sys.exit(main())
