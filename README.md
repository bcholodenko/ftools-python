# ftools-python

A Python 3 port of [FTools-5.0](https://github.com/AuRoN89/FTools), a
toolset for unpacking and repacking the firmware of Ford IPC (instrument
cluster) infotainment units, focused on the image/font/text resources
stored inside `.vbf` firmware files. The original project is a set of
C++17 console tools built with CMake; this port re-implements every tool
as a pure Python 3 script that runs on macOS and Linux without compiling
anything. A few additional tools with no equivalent in the original
project are also included - see [New tools](#new-tools-not-in-ftools-50)
below for which is which.

---

## Quick start — `ftool.py`

The simplest way to work with a `.vbf` file is the master tool, which
auto-detects what's inside and invokes every sub-tool automatically:

```
python3 ftool.py unpack firmware.vbf ./workspace/
python3 ftool.py pack   ./workspace/ patched.vbf
```

See the [ftool.py](#ftoolpy) section below for full details. The individual
sub-tools remain available if you need finer-grained control.

---

## Installation

Requires Python 3.9+ (tested on 3.12) and two pure-Python dependencies:

```
pip3 install -r requirements.txt
```

(`Pillow` is used for 256-colour palette generation; `numpy` is used for
exact nearest-colour pixel matching when remapping onto a fixed palette.
Everything else -- BMP/VBF/zip/CRC handling -- is implemented from scratch
using only the standard library, so there is nothing to compile.)

No other setup is required -- just run the scripts with `python3`.

---

## ftool.py

The master unpack/pack tool. Accepts a `.vbf` firmware file, auto-detects
every section's content type, and runs the appropriate sub-tool on each one.
No knowledge of which sections contain what is required.

##### Unpack
```
python3 ftool.py unpack firmware.vbf ./workspace/
```

The workspace directory must not already exist. After unpacking, the layout
is:

```
workspace/
  manifest.json                    ← bookkeeping for pack (do not edit)
  section_NN_<type>/               ← one directory per non-trivial section
    ...                            ← type-specific contents (see below)
```

What each section type produces:

**`imagefs`** (QNX image filesystem, raw or gzip+tar wrapped):
```
section_18_imagefs/
  files/                           ← full imagefs file tree (real paths)
    images/variant/.../foo.ddb     ← original .ddb, always kept
    images/variant/.../foo.png     ← decoded PNG for editing (transparency-aware)
    fonts/MHeiM18030_C.ttf         ← other embedded files
  imagefs_config.json              ← required by pack
  wrapping.json                    ← required by pack
```

**`imgsection`** (FTools zip+EIF image section):
```
section_01_imgsection/
  bmp/                             ← all images as editable .bmp files
  eif/                             ← original .eif files
  ttf/                             ← font files
  header_lines.csv
  export_list.csv
  custom/                          ← drop replacement .bmp/.ttf files here
```

**`textsection`** (Ford UI string binary):
```
section_NN_textsection/
  text_section.bin                 ← original binary, required as pack template
  ui_texts.csv                     ← editable UI strings
  ui_alerts.csv                    ← editable alert strings
```

**`raw`** (anything else):
```
section_NN_raw/
  data.bin                         ← verbatim binary dump
```

##### Editing

- **imagefs `.ddb` bitmaps:** edit the `.png` in place (including its alpha
  channel — see the `ddbconverter.py` section below for how transparency
  maps between the two formats). The PNG can be resized to different
  dimensions than the original. Pack re-encodes it back to `.ddb`
  automatically if the `.png` is newer than the `.ddb`; an unedited `.ddb`
  always passes through byte-for-byte unchanged.
- **imagefs other files:** overwrite in place under `files/`.
- **imgsection images/fonts:** drop replacements into `custom/` using the
  same filename as the original (same convention as `imgunpacker.py`).
- **textsection:** edit `ui_texts.csv` and/or `ui_alerts.csv` in place.
- **raw:** edit `data.bin` in place.

##### Pack
```
python3 ftool.py pack ./workspace/ patched.vbf
```

The original `.vbf` must be in the same directory as the workspace
(detected by name from `manifest.json`), or specified explicitly:

```
python3 ftool.py pack ./workspace/ patched.vbf --vbf /path/to/original.vbf
```

Pack is change-aware: it compares each section's current content to the
original and only rebuilds sections where something actually changed.
An unmodified workspace produces a byte-identical copy of the original.

Every `.vbf`'s ASCII header declares a `file_checksum` (a CRC-32 over the
binary content), and every section separately carries its own CRC-16.
`pack` always recalculates both correctly from scratch when saving, so a
packed file's checksums are always valid regardless of what the input
looked like.

If an imagefs section's repacked size grows beyond its original size (for
example after significantly enlarging an image), `pack` prints a warning:
real hardware appears to allocate a fixed amount of space per section, and
exceeding it can produce visual corruption even though the resulting file
is well-formed. See `background_migration.py` below for a tool that edits
within a fixed size budget instead of growing it.

##### Working with a VBF that already has a bad checksum

Both `unpack` and `pack` verify the file-level CRC-32 before doing
anything else. If you have a file whose stored checksum is already stale
for some other reason and you still want to work with it, pass
`--ignore-crc`:

```
python3 ftool.py unpack already-modified.vbf ./workspace/ --ignore-crc
python3 ftool.py pack ./workspace/ patched.vbf --ignore-crc
```

This downgrades the check to a printed warning instead of stopping.
Nothing else changes - the actual section data is read/written exactly the
same way, and `pack`'s output will have a correct checksum either way. The
same `--ignore-crc` flag is available on `vbfeditor.py`,
`imagefsunpacker.py`, and `imgunpacker.py`.

---

## imgunpacker.py

Works directly on a `.vbf` file. Extracts all images and fonts from the
FTools "image section" (zip+EIF blob, typically VBF section 1).

##### Extract images and fonts
```
python3 imgunpacker.py -u -o ./dest_dir original.vbf
```

The output directory must already exist. Images go to `./dest_dir/eif`
(native EIF format) and `./dest_dir/bmp` (converted to BMP for editing);
fonts go to `./dest_dir/ttf`. `./dest_dir/export_list.csv` records which
resource is which and `./dest_dir/header_lines.csv` describes each UI
element's size/colour/position. An empty `./dest_dir/custom/` folder is
created for replacements.

##### Modify resources
Drop edited `.bmp` or `.ttf` files into `./dest_dir/custom/`, using the
**same filenames** as the originally extracted resources.

##### Pack back into a VBF
```
python3 imgunpacker.py -p -c ./dest_dir/export_list.csv -v original.vbf -o ./out_dir
```

Produces `./out_dir/patched.vbf`.

---

## eifconverter.py

##### EIF → BMP
```
python3 eifconverter.py -U -i logo.eif -o out.bmp
```

##### BMP → EIF
```
python3 eifconverter.py -P -d 16 -i edited.bmp -o out.eif
```

EIF images come in three colour depths:
- `-d 32` -- SUPERCOLOR, true colour (32-bit BGRA)
- `-d 16` -- MULTICOLOR, 256-colour palette + per-pixel alpha
- `-d 8`  -- MONOCHROME, 8-bit grayscale

##### Bulk mode (shared palette)
```
python3 eifconverter.py -B -i ./bmp_dir -o ./eif_dir
```

Converts every `.bmp` in `bmp_dir` to 256-colour `.eif`, all sharing one
combined palette computed across the whole set.

---

## vbfeditor.py

Raw section-level access to `.vbf` files. Useful for inspecting or manually
swapping binary sections; for full image/text editing, `ftool.py` is simpler.

##### Unpack (extract raw binary sections + JSON config)
```
python3 vbfeditor.py -u -i firmware.vbf -o ./dir
```

##### Pack back up
```
python3 vbfeditor.py -p -i ./dir/firmware.vbf_config.json -o out.vbf
```

##### Show section info
```
python3 vbfeditor.py -I -i firmware.vbf
```

---

## imgsectionparser.py

Works directly on a raw image-section binary blob (e.g. a section file
produced by `vbfeditor.py -u`, or the same data `imgunpacker.py` normally
reads automatically from VBF section 1).

```
python3 imgsectionparser.py -u -i section_1.bin -o ./dest_dir
python3 imgsectionparser.py -p -i ./dest_dir/_config.json -o ./patched.bin
```

---

## textsectionparser.py

##### Extract text resources to CSV
```
python3 textsectionparser.py -u -i vbf_text_section.bin -o ./dest_dir
```

Produces `ui_alerts.csv` and `ui_texts.csv`. Edit the `line_content` column
of either file as needed.

##### Pack back up
```
python3 textsectionparser.py -p -i vbf_text_section.bin -o ./patched.bin
```

The edited `.csv` files must sit in the **same directory** as the
`vbf_text_section.bin` passed to `-i`.

---

## ziprepacker.py

Replaces the single entry inside a `.zip` archive with new content, keeping
the original entry's filename.

```
python3 ziprepacker.py -i original.zip -c new_content.eif -o repacked.zip
```

---

## imagefsunpacker.py

Some IPC firmware downloads embed a QNX `mkifs` "image filesystem" inside
one of the `.vbf`'s binary sections, usually wrapped in a gzip+tar archive.
This is a different container format from the zip+EIF image section that
`imgunpacker.py` works with. `ftool.py` handles this automatically; use this
tool directly if you need section-level control.

##### Unpack
```
python3 imagefsunpacker.py -u -o ./exported_dir firmware.vbf
```

Every VBF section is scanned automatically. Each image filesystem found is
unpacked into `./exported_dir/section_N/`, with every embedded file written
under `files/` (preserving its original path), plus `imagefs_config.json`
and `wrapping.json`.

##### Modify resources
Overwrite files under `./exported_dir/section_N/files/` in place.

##### Repack
```
python3 imagefsunpacker.py -p -e ./exported_dir -o patched.vbf firmware.vbf
```

Sections whose files weren't actually touched are left byte-for-byte
identical to the input; only changed sections are rebuilt and re-compressed.

##### A note on the trailer checksum
The image filesystem format ends with a 4-byte trailer value of unknown
purpose, written as a best-effort placeholder when a section is rebuilt
from scratch (see `ftools_lib/imagefs.py`). An unmodified section is
always repacked from its original bytes untouched, so this only matters
if you actually change something inside a section. Treat an edited,
repacked VBF as untested until verified on real hardware.

##### A note on duplicate paths
Real firmware has been observed to contain more than one directory entry
under the exact same path (an intentional QNX shadowing mechanism, not
corruption). Both copies are exported to disambiguated filenames and both
are updated if that path is replaced.

---

## ddbconverter.py

Converts a single `.ddb` bitmap to/from `.png`. `ftool.py` handles `.ddb`
conversion automatically during unpack/pack; use this tool for individual
files.

```
python3 ddbconverter.py -U -i image.ddb -o image.png
python3 ddbconverter.py -P -i edited.png -d image.ddb -o new.ddb
```

Packing (`-P`) requires `-d/--donor`: the original `.ddb` file, which
supplies the header byte (type_byte/byte5/format) verbatim. The
replacement image can be a different size than the original - the file's
width/height fields and internal layout are recomputed to match. Same-size
edits additionally preserve any padding columns/rows from the donor
byte-for-byte, so an unedited image round-trips to an identical file; a
resized image is rebuilt fresh at the new dimensions instead, since the
donor's padding doesn't correspond to anything at a different size. PNG
was chosen over BMP specifically because `.ddb` pixels can carry
transparency, and PNG's alpha channel maps onto it directly.

##### Format

`.ddb` is a proprietary bitmap format used by the IPC's QNX image
filesystem. Five format variants are supported, covering every bitmap
found in a typical resource pack, with byte-exact round-trip (decode to
PNG, re-encode to `.ddb`, identical to the original) for every one of them.

**Header** (8 bytes, little-endian):

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 2 | width | visible width, pixels |
| 2 | 2 | height | visible height, pixels |
| 4 | 1 | type_byte | observed 0x02/0x03/0x82; doesn't affect decoding |
| 5 | 1 | byte5 | always 0x80 in every sample |
| 6 | 1 | format | see table below — determines everything else |
| 7 | 1 | always 0 |

**Pixel format** — a 16-bit word per pixel, A1R5G5B5 (*not* RGB565):

| Bits | Field |
|---|---|
| 15 | alpha flag (1=opaque, 0=transparent) — only meaningful where there's no separate mask plane |
| 14–10 | red (5 bits) |
| 9–5 | green (5 bits) |
| 4–0 | blue (5 bits) |

Each 5-bit channel scales to 8-bit by multiplying by `255/31`.

**The `format` byte** encodes two flags directly: `rle = format % 2 != 0`
and `mask = format in (2, 6, 7)` (mask means a separate per-pixel alpha
plane follows the color plane, instead of relying on bit 15):

| format | rle | mask | typical use |
|---|---|---|---|
| 4 | no | no | opaque backgrounds/panels |
| 6 | no | yes | opaque-or-transparent icons |
| 2 | no | yes | infotainment tray panels |
| 5 | yes | no | compressed, fully opaque images |
| 7 | yes | yes | compressed icons/signs with transparency |

Pixel data is stored in a row-major grid padded wider than the visible
image (`realWidth` columns, only the leftmost `width` are visible) so the
renderer can address rows with a shift instead of a multiply. `realWidth`
is the smallest value in `[16,32,64,128,256,512,1024]` that's `>= width`,
except format 2 which uses `ceil(width/4)*4`. The mask plane (when
present) pads its own row width to a multiple of 8.

Compressed formats (5, 7) use a PackBits-style run-length scheme: a control
word's top bit selects between a literal run (copy N raw pixels) and a
repeat run (one pixel value repeated N times); the mask plane uses the same
scheme one byte at a time instead of one 16-bit word at a time. Full bit-
level detail is in the `ftools_lib/ddb.py` module docstring.

---

## png_to_bmp_a1r5g5b5.py

Standalone PNG → 16-bit BMP (A1R5G5B5) converter, independent of the
`.ddb`/VBF pipeline above. Some IPC customization workflows expect a
bitmap in exactly this pixel format as a direct upload, rather than a
`.ddb` file.

```
python3 png_to_bmp_a1r5g5b5.py input.png output.bmp
python3 png_to_bmp_a1r5g5b5.py input.png output.bmp --no-alpha
```

Produces a standard `BITMAPINFOHEADER` BMP, 16 bits/pixel, `BI_BITFIELDS`
compression with explicit R/G/B masks (`0x7C00`/`0x03E0`/`0x001F`) - the
same bit layout as the `.ddb` pixel format above (1-bit alpha + 5-5-5 RGB).
Use `--no-alpha` to force every pixel fully opaque, for files meant as a
base/background bitmap rather than something with its own transparency
mask (a separate mask image, saved as 8-bit grayscale, is generally
expected to carry transparency information instead - that file is
produced with standard image editing tools, not this script).

---

## background_migration.py

Relocates the 4 background image pieces (`left_active_cut`,
`center_active_cut`, `right_active_cut`, `center_quiet_cut`) inside a
graphics VBF's imagefs to full-screen dimensions, by de-duplicating the
startup needle-sweep pointer animation frames and defragmenting the space
that frees up.

```
python3 background_migration.py input-FB.vbf output-FB.vbf
```

The 4 relocated pieces come out as blank/transparent canvases at full
size, ready for custom artwork. Options:

- `--skip-join` -- keep the startup needle-sweep pointer animation intact
  instead of de-duplicating its frames down to one static frame; there may
  not be enough free space for the migration without that step, though.
- `--section N` -- the imagefs section index, if it isn't the usual one
  for your VBF (unpack with `ftool.py` first to check).

Unlike a plain resize through `ftool.py` (which rebuilds the imagefs
section at whatever size the new content needs), this tool only ever
relocates existing data within space that's already accounted for within
the section, so the section's overall size never changes - see the size
note in the `ftool.py` section above for why that matters.

---

## New tools (not in FTools-5.0)

The following have no equivalent in the original C++ project and were
added in this port:

- **`ftool.py`** -- master unpack/pack orchestrator; auto-detects and
  handles all section types in one command.
- **`imagefsunpacker.py`** / **`ftools_lib/imagefs.py`** -- QNX `mkifs`
  image filesystem support, for firmware downloads that use this container
  instead of (or alongside) the FTools zip+EIF format.
- **`ddbconverter.py`** / **`ftools_lib/ddb.py`** -- pixel-level `.ddb`
  bitmap conversion, covering all known format variants with byte-exact
  round-trip.
- **`png_to_bmp_a1r5g5b5.py`** -- standalone PNG to 16-bit BMP (A1R5G5B5)
  converter.
- **`background_migration.py`** -- full-screen background relocation
  within a graphics VBF's imagefs.

Everything else (`imgunpacker.py`, `eifconverter.py`, `vbfeditor.py`,
`imgsectionparser.py`, `textsectionparser.py`, `ziprepacker.py`) is a
direct port of the corresponding original FTools-5.0 tool.

---

## Disclaimer

* **No Affiliation:** This project is completely independent and has **no affiliation, association, authorization, endorsement, or official connection** with Ford Motor Company, Lincoln Motor Company, Mazda Motor Corporation, Volvo Car Corporation, Jaguar Land Rover (JLR), Geely Auto, or any of their subsidiaries or affiliates.
* **Trademarks:** All product names, logos, brands, and trademarks (including "Ford", "Lincoln", "Mazda", "Volvo", "Jaguar", "Land Rover", "Geely", and ".vbf") are the property of their respective owners. Their use in this project is strictly for asset identification and compatibility purposes and does not imply any endorsement or relationship.
* **Use at Your Own Risk:** This software is provided "as is" without warranty of any kind. The author assumes no responsibility for any damage, data loss, or bricked hardware caused by the use or misuse of this tool. Use of this software may void your vehicle's warranty.

## License

This project is licensed under the GNU General Public License v3.0 (GPLv3).
See the `LICENSE` file for the full license text.
