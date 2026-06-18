# ftools-python

A from-scratch Python 3 rewrite of [FTools-5.0](https://github.com/AuRoN89/FTools),
a toolset for unpacking and repacking the firmware of Ford IPC (instrument
cluster) infotainment units, focused on the image/font/text resources stored
inside `.vbf` firmware files. The original project is a set of C++17 console
tools built with CMake; this port re-implements every tool as a pure Python 3
script that runs on macOS (and Linux) without compiling anything.

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
    images/variant/.../foo.bmp     ← decoded BMP for editing (184/305 files)
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

- **imagefs `.ddb` bitmaps:** edit the `.bmp` in place. Pack re-encodes it
  back to `.ddb` automatically if it's newer than the original `.ddb`.
  Unsupported `.ddb` files (those without a `.bmp` sibling) pass through
  unchanged.
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
The image filesystem format ends with a 4-byte checksum whose exact
algorithm could not be conclusively determined (see `ftools_lib/imagefs.py`
for details). An unmodified section is always repacked from its original
bytes, so this only matters if you actually change something. Treat an
edited, repacked VBF as untested until verified on real hardware.

##### A note on duplicate paths
Real firmware has been observed to contain more than one directory entry
under the exact same path (an intentional QNX shadowing mechanism, not
corruption). Both copies are exported to disambiguated filenames and both
are updated if that path is replaced.

---

## ddbconverter.py

Converts a single `.ddb` bitmap to/from `.bmp`. `ftool.py` handles `.ddb`
conversion automatically during unpack/pack; use this tool for individual
files.

```
python3 ddbconverter.py -U -i image.ddb -o image.bmp
python3 ddbconverter.py -P -i edited.bmp -d image.ddb -o new.ddb
```

Packing (`-P`) requires `-d/--donor`: the original `.ddb` file, which
supplies the header bytes and any padding verbatim. The replacement image
must be the exact same dimensions as the original.

##### Format support

`.ddb` is an undocumented, proprietary format reverse-engineered empirically
against a real resource pack (`JB5T-14C088-FB.vbf`, 305 files). Two of
several observed variants are supported (~60% of files):

- **RAW16 (144/305 files, ~47%):** raw uncompressed 16-bit-per-pixel
  RGB565, no palette. Row stride is `next_pow2(width)` pixels. Covers
  every full-screen background, popup panel, and gradient image in the
  reference pack.

- **RAW24 (40/305 files, ~13%):** raw uncompressed 24-bit-per-pixel RGB,
  no palette. Same stride scheme as RAW16. About a third of files in this
  group have an extra 32-byte block before the pixel data (purpose unknown;
  preserved verbatim on round-trip); the decoder auto-detects this from
  the file length. **Known limitation:** a small subset of small interactive
  icons in this group (checkbox/radio-button states, arrow buttons, on/off
  toggle pairs) appear to need 16bpp rather than 24bpp, but no reliable
  automatic discriminator was found -- those specific icons may show colour
  banding. All 40 files pass byte-exact round-trip encode regardless.

- **Not supported (121/305 files, ~40%):** three header signatures used
  mostly for telltales, gauge pointers, and small icons. These are
  confirmed genuinely compressed (same dimensions produce different file
  sizes). Unsupported files raise a clear error rather than silently
  producing garbage, and pass through unchanged in `ftool.py`.

---

## New tools (not in FTools-5.0)

The following tools have no equivalent in the original C++ project and were
added during this port:

- **`ftool.py`** -- master unpack/pack orchestrator; auto-detects and
  handles all section types in one command.
- **`imagefsunpacker.py`** / **`ftools_lib/imagefs.py`** -- QNX `mkifs`
  image filesystem support, for firmware downloads that use this container
  instead of (or alongside) the FTools zip+EIF format.
- **`ddbconverter.py`** / **`ftools_lib/ddb.py`** -- pixel-level `.ddb`
  bitmap conversion, for the 60% of `.ddb` files whose format was
  successfully reverse-engineered.

---

## Disclaimer

* **No Affiliation:** This project is completely independent and has **no affiliation, association, authorization, endorsement, or official connection** with Ford Motor Company, Lincoln Motor Company, Mazda Motor Corporation, Volvo Car Corporation, Jaguar Land Rover (JLR), Geely Auto, or any of their subsidiaries or affiliates.
* **Trademarks:** All product names, logos, brands, and trademarks (including "Ford", "Lincoln", "Mazda", "Volvo", "Jaguar", "Land Rover", "Geely", and ".vbf") are the property of their respective owners. Their use in this project is strictly for asset identification and compatibility purposes and does not imply any endorsement or relationship.
* **Use at Your Own Risk:** This software is provided "as is" without warranty of any kind. The author assumes no responsibility for any damage, data loss, or bricked hardware caused by the use or misuse of this tool. Use of this software may void your vehicle's warranty.

## License

This project is licensed under the GNU General Public License v3.0 (GPLv3).
See the `LICENSE` file for the full license text.
