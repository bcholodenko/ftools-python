#!/usr/bin/env python3
"""Splits a single 1280x422 background template image into the 6 individual
background pieces (left/center/right, active/quiet), each cropped to its
correct position and size, ready to drop into a `ftool.py`-unpacked
workspace and pack normally.

This is the companion to `background_migration.py`: that tool relocates
the 4 active/quiet-cut backgrounds to full-screen size (as blank
canvases); this one fills a canvas of that size from one source image,
instead of requiring six manually-cropped pieces.

Usage:
    python3 split_fullscreen_template.py template.png ./output_dir/
    python3 split_fullscreen_template.py template.png ./output_dir/ --pieces active
    python3 split_fullscreen_template.py template.png ./output_dir/ --pieces quiet
"""
import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("This tool requires Pillow: pip install Pillow")

SOURCE_WIDTH, SOURCE_HEIGHT = 1280, 422

# (width, height, preview_x, preview_y, real_height, group) per piece.
#
# The crop region within the 1280x422 source is:
#   x: [preview_x, preview_x+width)
#   y: [preview_y-real_height+1, preview_y-real_height+1+min(SOURCE_HEIGHT, height))
# i.e. up to SOURCE_HEIGHT (422) rows get filled starting at that y, and -
# for the 3 active pieces specifically, whose migrated height (448/448/474)
# exceeds 422 - anything past row 422 of the destination canvas is left
# fully transparent rather than filled with more cropped source content.
# This matches the reference tool's own template-fill feature
# (`fullscreenTemplate()`/`hx()`), confirmed by simulating it directly
# against a row+column-tagged test image rather than just reading the
# minified source: it only ever writes the first min(422, height) rows of
# the destination, regardless of how much taller the canvas actually is.
# The "+1" comes directly from hx()'s own source-row arithmetic (confirmed
# empirically, not just algebraically - an earlier pass at this got the
# offset right but dropped this +1, which would have shifted every piece
# up by exactly one source row).
#
# `real_height` here is the *live* value the reference tool would actually
# see at the point this feature runs - not the realHeight column in the
# il table. For the 4 pieces background_migration.py relocates (the 3
# active ones plus center_quiet_cut), the migration always rewrites the
# file as format 4 (raw) with body size realWidth*(realHeight+1)*2 - so by
# the time a template-fill could run on the migrated file, decoding that
# file's own size yields realHeight+1, not the il table's realHeight.
# Confirmed directly: feeding a buffer sized via that exact formula through
# this project's own _raw_real_height() (the same computation the reference
# tool's ax() does) returns il_realHeight+1, not il_realHeight itself.
# left_quiet_cut/right_quiet_cut are never migrated, so there's no analogous
# "+1 from the migration formula" - their real_height here is each file's
# own live value instead, taken directly from the real sample firmware
# (left_quiet_cut is RLE, where realHeight==height exactly; right_quiet_cut
# is raw format 4, where it happens to need the same kind of +1, but for an
# unrelated, file-size-driven reason specific to that file rather than to
# any migration step).
PIECES = {
    "left_active_cut":   {"width": 440, "height": 448, "preview_x": -3,  "preview_y": 448, "real_height": 449, "group": "active"},
    "center_active_cut": {"width": 796, "height": 448, "preview_x": 64,  "preview_y": 448, "real_height": 449, "group": "active"},
    "right_active_cut":  {"width": 424, "height": 474, "preview_x": 856, "preview_y": 474, "real_height": 475, "group": "active"},
    "left_quiet_cut":    {"width": 398, "height": 281, "preview_x": 38,  "preview_y": 321, "real_height": 281, "group": "quiet"},
    "center_quiet_cut":  {"width": 822, "height": 281, "preview_x": 48,  "preview_y": 321, "real_height": 282, "group": "quiet"},
    "right_quiet_cut":   {"width": 345, "height": 281, "preview_x": 900, "preview_y": 321, "real_height": 282, "group": "quiet"},
}


def crop_piece(source: "Image.Image", spec: dict) -> "Image.Image":
    """Crops one piece's region out of the source, handling out-of-bounds
    edges (which several pieces have) by leaving them transparent rather
    than erroring or wrapping. Also leaves anything past the first
    min(SOURCE_HEIGHT, height) destination rows transparent, matching the
    reference tool's own template-fill feature - see the PIECES comment
    above for why that's correct rather than filling the whole height."""
    width, height = spec["width"], spec["height"]
    real_height = spec["real_height"]
    crop_left = spec["preview_x"]
    crop_top = spec["preview_y"] - real_height + 1
    window_rows = min(SOURCE_HEIGHT, height)
    crop_bottom = crop_top + window_rows

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    # Overlap between the requested crop region and the actual source canvas:
    src_left = max(crop_left, 0)
    src_top = max(crop_top, 0)
    src_right = min(crop_left + width, source.width)
    src_bottom = min(crop_bottom, source.height)
    if src_right > src_left and src_bottom > src_top:
        region = source.crop((src_left, src_top, src_right, src_bottom))
        canvas.paste(region, (src_left - crop_left, src_top - crop_top))
    return canvas


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("template", type=Path, help="Source image, must be exactly 1280x422")
    p.add_argument("output_dir", type=Path, help="Directory to write the cropped pieces into")
    p.add_argument("--pieces", choices=["all", "active", "quiet"], default="all",
                   help="Which pieces to produce (default: all 6). 'active' produces just the "
                        "3 pieces background_migration.py relocates to full-screen size; "
                        "'quiet' produces the 3 that stay at their original size.")
    args = p.parse_args()

    source = Image.open(args.template).convert("RGBA")
    if source.size != (SOURCE_WIDTH, SOURCE_HEIGHT):
        sys.exit(f"Error: template must be exactly {SOURCE_WIDTH}x{SOURCE_HEIGHT} "
                 f"(got {source.width}x{source.height}).")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, spec in PIECES.items():
        if args.pieces != "all" and spec["group"] != args.pieces:
            continue
        piece = crop_piece(source, spec)
        out_path = args.output_dir / f"{name}.png"
        piece.save(out_path)
        print(f"  wrote {out_path}  ({piece.width}x{piece.height})")

    print("\nDrop these into the corresponding "
          "images/variant/backgrounds/*.png files in a ftool.py-unpacked "
          "workspace, then pack normally. The active pieces only fit "
          "correctly if the VBF has already been migrated to full-screen "
          "size first (see background_migration.py).")


if __name__ == "__main__":
    main()
