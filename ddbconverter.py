#!/usr/bin/env python3
"""
ddbconverter.py - .ddb <-> BMP converter for IPC bitmap resources.

Only the confirmed "raw 16bpp" .ddb variant is supported - see
ftools_lib/ddb.py for format details and scope/limitations. Files using one
of the other (uncracked) variants will raise a clear error rather than
producing garbage.

Usage:
    ddbconverter.py -U -i image.ddb -o image.bmp
    ddbconverter.py -P -i edited.bmp -d image.ddb -o new.ddb
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
        description=".ddb <-> BMP converter (confirmed raw-16bpp variant only)",
    )
    p.add_argument("-P", "--pack", action="store_true", help="Pack a BMP back into .ddb format")
    p.add_argument("-U", "--unpack", action="store_true", help="Unpack a .ddb file to BMP")
    p.add_argument("-i", "--input", help="Input file (.ddb for -U, .bmp for -P)")
    p.add_argument("input_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    p.add_argument("-o", "--output", help="Output file")
    p.add_argument(
        "-d", "--donor",
        help="(required for -P) the original .ddb file this BMP came from - supplies the "
             "header and padding bytes; the replacement image must be the same dimensions",
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

    try:
        if args.unpack:
            out_path = Path(args.output) if args.output else input_path.with_suffix(".bmp")
            data = read_file(input_path)
            ddbmod.ddb_to_bmp_file(data, out_path, context=str(input_path))
            print(f"Wrote {out_path}")

        elif args.pack:
            if not args.donor:
                print("Packing requires -d/--donor (the original .ddb this BMP came from).")
                return 0
            out_path = Path(args.output) if args.output else Path(args.donor)
            ddbmod.bmp_file_to_ddb_file(input_path, Path(args.donor), out_path)
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
