# FTools (Python / macOS port)

A from-scratch Python 3 rewrite of [FTools-5.0](https://github.com/AuRoN89/FTools),
a toolset for unpacking and repacking the firmware of Ford IPC (instrument
cluster) infotainment units, focused on the image/font/text resources stored
inside `.vbf` firmware files. The original project is a set of C++17 console
tools built with CMake; this port re-implements every tool as a pure Python 3
script that runs on macOS (and Linux) without compiling anything.

There are six tools (the original's sixth tool, `ImgUnpacker`, already
combines several of the others into one convenient workflow), plus a
seventh, `imagefsunpacker.py`, for a second, unrelated resource-container
format some firmware downloads use instead:

- **`imgunpacker.py`** -- unpack/pack images and fonts directly from/into a `.vbf` file.
- **`vbfeditor.py`** -- unpack/pack/inspect `.vbf` files (raw binary sections).
- **`imgsectionparser.py`** -- extract/build the zip+ttf image-section binary blob on its own.
- **`eifconverter.py`** -- convert a single `.eif` image to/from `.bmp`.
- **`textsectionparser.py`** -- extract/repack the UI text & alert strings to/from CSV.
- **`ziprepacker.py`** -- swap the contents of a single-file `.zip` while keeping its filename.
- **`imagefsunpacker.py`** -- unpack/repack a QNX `mkifs` image filesystem (fonts/bitmaps/binaries) embedded in any section of a `.vbf` file.

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

## imgunpacker.py
##### Extract images and fonts
```
python3 imgunpacker.py -u -o ./dest_dir original.vbf
```
The output directory must already exist. Images go to `./dest_dir/eif`
(in IPC's native EIF format) and `./dest_dir/bmp` (converted to BMP for
easy editing); fonts go to `./dest_dir/ttf`. `./dest_dir/export_list.csv`
records which resource is which (you normally don't need to edit it
yourself) and `./dest_dir/header_lines.csv` describes each UI element's
size/colour/position. An empty `./dest_dir/custom` folder is also created.

##### Modify resources
Drop edited `.bmp` or `.ttf` files into `./dest_dir/custom`, using the
**same filenames** as the originally extracted resources.

##### Pack images and fonts back into a VBF
```
python3 imgunpacker.py -p -c ./dest_dir/export_list.csv -v original.vbf -o ./out_dir
```
Produces `./out_dir/patched.vbf`.

---

## eifconverter.py
##### EIF -> BMP
```
python3 eifconverter.py -U -i logo.eif -o out.bmp
```
##### BMP -> EIF
```
python3 eifconverter.py -P -d 16 -i edited.bmp -o out.eif
```
EIF images come in three colour depths:
- `-d 32` -- SUPERCOLOR, true colour (32-bit BGRA)
- `-d 16` -- MULTICOLOR, 256-colour palette + per-pixel alpha
- `-d 8`  -- MONOCHROME, 8-bit grayscale

##### Bulk mode (shared palette across many images)
```
python3 eifconverter.py -B -i ./bmp_dir -o ./eif_dir
```
Converts every `.bmp` under `bmp_dir` to a 256-colour `.eif`, with all of
them sharing one combined palette computed across the whole set.

---

## vbfeditor.py
##### Unpack a VBF (extract its raw binary sections + a JSON config)
```
python3 vbfeditor.py -u -i path_to.vbf -o dir/to/extract
```
##### Pack a VBF back up
```
python3 vbfeditor.py -p -i dir/to/extract/path_to.vbf_config.json -o out.vbf
```
##### Show section info
```
python3 vbfeditor.py -I -i path_to.vbf
```

---

## imgsectionparser.py
Works directly on a raw image-section binary blob -- e.g. one of the
section files produced by `vbfeditor.py -u`, or VBF section 1 specifically
(which is what `imgunpacker.py` normally extracts for you automatically).
```
python3 imgsectionparser.py -u -i section_1.bin -o ./dest_dir
python3 imgsectionparser.py -p -i ./dest_dir/_config.json -o ./patched.bin
```

---

## textsectionparser.py (alfa)
##### Extract text resources to CSV
```
python3 textsectionparser.py -u -i path/to/vbf_text_section.bin -o dir/to/extract
```
This produces `ui_alerts.csv` and `ui_texts.csv`. Edit the `line_content`
column of either file as needed.

##### Pack the text section back up
```
python3 textsectionparser.py -p -i path/to/vbf_text_section.bin -o path/to/patched_vbf_text_section.bin
```
The edited `.csv` files must sit in the **same directory** as the
`vbf_text_section.bin` you pass to `-i` (this is also true of the original
C++ tool -- `-i` must point at the same binary you originally unpacked, not
the unpack destination by itself).

---

## ziprepacker.py
Replaces the single entry inside a `.zip` archive with new content, keeping
the original entry's filename.
```
python3 ziprepacker.py -i original.zip -c new_content.eif -o repacked.zip
```

---

## imagefsunpacker.py
Some IPC firmware downloads -- typically a dedicated bitmap/font resource
package separate from the OS/app package -- embed fonts, bitmaps, or even
a small bootable OS image as a QNX `mkifs` "image filesystem" inside one of
the `.vbf`'s binary sections, usually itself wrapped in a gzip-compressed
tar archive. This is a completely different, independently-documented
container format from the zip+ttf "image section" blob that
`imgsectionparser.py`/`imgunpacker.py` work with, so it has its own tool.

##### Unpack
```
python3 imagefsunpacker.py -u -o ./exported_dir original.vbf
```
Every VBF section is scanned automatically (raw or gzip+tar-wrapped); each
image filesystem found is unpacked into its own `./exported_dir/section_N/`,
with every embedded file written under `files/` (preserving its original
path, e.g. `files/fonts/MHeiM18030_C.ttf`), plus `imagefs_config.json`
(directory metadata: inode/mode/owner/timestamp for every entry) and
`wrapping.json` (how to re-wrap the rebuilt blob, if it was gzip+tar
wrapped to begin with).

##### Modify resources
Overwrite any file under `./exported_dir/section_N/files/` in place with
edited content -- same path, any size.

##### Repack
```
python3 imagefsunpacker.py -p -e ./exported_dir -o patched.vbf original.vbf
```
Every `section_N` directory found under `-e` is rebuilt and written back
into the matching section of `original.vbf`. A section whose files weren't
actually touched is detected and left completely untouched (byte-for-byte
identical to the input), the same guarantee `vbfeditor.py` gives for its
own unpack/repack; only sections with genuine edits get rebuilt and
re-compressed.

##### A note on the trailer checksum
This format ends with a 4-byte checksum whose exact algorithm could not be
conclusively determined (see the detailed explanation in
`ftools_lib/imagefs.py`'s module docstring -- in short, a real sample's
own data already summed to zero on its own, strongly suggesting some
complement-style design, but the precise formula wasn't pinned down). An
unmodified section is repacked byte-for-byte from the original, so this
only matters if you actually edit something: in that case the checksum is
recomputed using a documented best-effort formula that has **not** been
verified against any real consuming code. Treat an edited, repacked VBF as
untested until verified on real hardware or a simulator.

##### A note on duplicate paths
Real firmware samples have been observed to contain more than one
directory entry under the exact same path (apparently an intentional QNX
shadowing/overlay mechanism, not corruption). `imagefsunpacker.py` and
`ftools_lib/imagefs.py` both handle this correctly -- exporting each copy
to a disambiguated on-disk filename so neither is lost, and updating every
matching entry if you replace that path's file -- but it's worth knowing
about if you see what looks like the same filename twice in
`imagefs_config.json`.

##### Scope
This tool unpacks and repacks at the "extract/replace whole files" level,
same as `imgsectionparser.py` does for its own format. It does not decode
the pixel data inside the `.ddb` bitmap files found in real samples (a
separate, QNX Photon-specific image format that wasn't reverse-engineered
as part of this work) the way `eifconverter.py` does for `.eif` -- so a
`.ddb` file can be extracted and swapped for another valid `.ddb`, but not
yet converted to/from `.bmp` for easy editing. That would be a reasonable
follow-up if needed.

---

## Notes on faithfulness to the original tool

Every user-facing error and warning message across all seven tools has
been rewritten for clarity and correct English (the original C++ tool's
messages were sometimes broken or misleading -- e.g. `"Warn. VBF ascii
header not contain content length"`, or a couple of cases that printed the
wrong noun entirely, like `imgunpacker.py` saying `"VBF file not found"`
when it was actually checking for the config file). This is purely
cosmetic and doesn't affect any binary format, so unlike the rest of this
section it was **not** kept faithful to the original's wording on purpose.

This port aims to be byte-for-byte compatible with the original C++ tools
for every binary format involved (VBF sections, the EIF image format, the
image-section blob, and the fixed-layout text-section blob), including
several quirks of the original implementation that are preserved
intentionally rather than "fixed":

- **Filenames inside exported VBF section files** embed the section length
  in hexadecimal even though it looks like it should be decimal (an
  std::hex stickiness artifact in the original C++ stream code). This is
  purely cosmetic -- the real length always comes from the actual file size
  on import, never from the filename.
- **`ui_text_pack_ex`**, part of the text-section binary layout, is defined
  in the original struct (and therefore reserves space, affecting the byte
  offsets of everything after it) but its contents are never read or
  written by either the unpack or pack routines in the original tool. This
  port leaves that region completely untouched too.
- **Alert CSV rows** use the alert record's *stored* `idx` field as the
  `line_id` column, and packing then uses that same value both as the new
  `idx` *and* as the array position to write into -- i.e. it assumes
  `idx == position`. This matches the original exactly.
- **`ziprepacker.py`'s default output filename**, when `-o` is omitted, is
  literally the input filename with the string `"repacked.zip"` appended
  (e.g. `image.zip` -> `image.ziprepacked.zip`), not a sensible extension
  swap -- this is the original tool's documented default, kept as-is.

Two things were deliberately changed because they only affect where
intermediate *working files* land on disk, never the binary firmware
format itself:

- `imgsectionparser.py -u`'s exported `_header.csv` is now written inside
  the chosen output directory. In the original C++ tool it was written
  relative to the process's current working directory by mistake (a
  path-join that was never joined with the output path), so running the
  tool from a different folder than expected would leave that file
  somewhere other than where its companion JSON config looks for it.
- A handful of CLI flags that the original silently mishandled when
  required arguments were omitted (e.g. `vbfeditor.py`'s `-o` defaulting to
  an empty string) now print a clear error message instead of failing with
  an obscure I/O error.

Two more things were fixed after testing against a real Volvo/Ford `.vbf`
firmware file (a different kind of VBF than the IPC UI-resource files
`imgsectionparser.py`/`imgunpacker.py`/`textsectionparser.py` are designed
for): feeding one of those tools binary data that isn't actually an
image-section or text-section blob used to either crash with a raw,
unhandled `struct.error` traceback, or -- worse -- silently return
truncated/wrong data for an out-of-range item offset (Python slicing
quietly wraps negative indices instead of erroring, unlike a C++
pointer/array access). Both tools now validate buffer bounds explicitly and
raise a clear, catchable error instead. Separately, CRC-16/CCITT-FALSE was
originally a pure-Python per-byte loop; it's now backed by the C-accelerated
`binascii.crc_hqx` (verified bit-for-bit identical across thousands of
random inputs first), which sped up parsing a real ~2.7MB firmware file by
roughly 4-5x.

One actual algorithmic difference: 256-colour (MULTICOLOR/`-d 16`) palette
generation uses Pillow's quantizer instead of the original's `exoquant`
library, so a freshly generated palette won't be byte-identical to one the
original tool would have produced from the same source image. Re-mapping
pixels onto an *already-fixed* palette (the `-s/--scheme` option, or
re-grouping siblings during `imgunpacker.py -p`), however, is done via an
exact nearest-colour search, so colours that already exist in the target
palette always map back to their exact original index.

Finally, `imagefsunpacker.py` and `ftools_lib/imagefs.py` are new
functionality, not a port of anything in the original FTools-5.0 C++
project -- added after testing against a real Ford/Volvo "bitmaps" `.vbf`
whose resources turned out to use QNX's own `mkifs` image filesystem format
rather than FTools' zip+ttf image-section format. See the dedicated
section above for details and the format's one open question (the trailer
checksum's exact algorithm).

## Disclaimer

* **No Affiliation:** This project is completely independent and has **no affiliation, association, authorization, endorsement, or official connection** with Ford Motor Company, Lincoln Motor Company, Mazda Motor Corporation, Volvo Car Corporation, Jaguar Land Rover (JLR), Geely Auto, or any of their subsidiaries or affiliates.
* **Trademarks:** All product names, logos, brands, and trademarks (including "Ford", "Lincoln", "Mazda", "Volvo", "Jaguar", "Land Rover", "Geely", and ".vbf") are the property of their respective owners. Their use in this project is strictly for asset identification and compatibility purposes and does not imply any endorsement or relationship.
* **Use at Your Own Risk:** This software is provided "as is" without warranty of any kind. The author assumes no responsibility for any damage, data loss, or bricked hardware caused by the use or misuse of this tool. Use of this software may void your vehicle's warranty.

## Credits

All format reverse-engineering and tool design is the work of the original
[FTools](https://github.com/AuRoN89/FTools) project; this repository is
purely a Python re-implementation of that work for macOS/cross-platform use
without a C++ toolchain.
