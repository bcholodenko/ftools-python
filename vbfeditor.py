#!/usr/bin/env python3
"""
vbfeditor.py - Simple console VBF files unpacker/packer.

Python port of FTools-5.0's VbfEditor ("VBFEditor") command-line tool.

Usage:
    vbfeditor.py -u -i firmware.vbf -o ./exported_dir
    vbfeditor.py -p -i ./exported_dir/firmware.vbf_config.json -o ./repacked.vbf
    vbfeditor.py -I -i firmware.vbf
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftools_lib.vbf import VbfFile, VbfError


def build_parser():
    p = argparse.ArgumentParser(
        prog="vbfeditor.py",
        description="Simple console VBF files unpacker/packer",
    )
    p.add_argument("-p", "--pack", action="store_true", help="Pack VBF file")
    p.add_argument("-u", "--unpack", action="store_true", help="Unpack VBF file")
    p.add_argument("-I", "--info", action="store_true", help="Show info about VBF file")
    p.add_argument("-i", "--input", help="Input file")
    p.add_argument("input_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    p.add_argument("-o", "--output", default="", help="Output directory")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    input_path = args.input or args.input_pos
    if not input_path:
        print("Please specify an input file with -i/--input.")
        return 0

    if args.pack:
        vbf = VbfFile()
        try:
            vbf.import_config(input_path)
        except VbfError as e:
            print(f"Error importing config: {e}")
            return 0
        if not args.output:
            print("Please specify an output file with -o/--output.")
            return 0
        try:
            vbf.save_to_file(args.output)
        except VbfError as e:
            print(f"Error saving VBF file: {e}")
        return 0

    if args.unpack:
        vbf = VbfFile()
        try:
            vbf.open_file(input_path)
        except VbfError as e:
            print(f"Error opening VBF file: {e}")
            return 0
        if not args.output:
            print("Please specify an output directory with -o/--output.")
            return 0
        try:
            vbf.export(args.output)
        except VbfError as e:
            print(f"Error exporting VBF file: {e}")
        return 0

    if args.info:
        vbf = VbfFile()
        try:
            vbf.open_file(input_path)
        except VbfError as e:
            print(f"Error opening VBF file: {e}")
            return 0

        sections_count = vbf.sections_count()
        print(f"Found {sections_count} section(s).")

        if sections_count:
            print("  #" + " | " + "   Offset   " + " | " + " Start addr " + " | " + " Length ")

            hex_off = vbf.header_sz()
            for i in range(sections_count):
                info = vbf.get_section_info(i)
                start_addr = info["start_addr"]
                length = info["length"]

                row = (
                    f"{i:>3}"
                    + " | "
                    + " 0x"
                    + f"{hex_off:08x}"
                    + " "
                    + " | "
                    + " 0x"
                    + f"{start_addr:08x}"
                    + " "
                    + " | "
                    + " 0x"
                    + f"{length:08x}"
                    + f" ({length})"
                )
                print(row)

                hex_off += length + 4 + 4 + 2  # uint32 + uint32 + uint16

        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
