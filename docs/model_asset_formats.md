# Rage Racer (PAL) model and course asset formats

This document describes how the PAL release of Rage Racer (SCES-006.50) stores its 3D models: the course
scenes, the cars and the menu models, together with the texture data they sample. Everything in the
"Proven" sections has two kinds of evidence:

- the decompiled C or the matched assembly in this repository, cited by file and line;
- byte-level checks the decoders in `tools/` run over every asset of a PAL disc (they raise on any mismatch).

Guesses are kept apart in [Hypotheses](#12-hypotheses), and what is still unknown is in
[Open questions](#13-open-questions). The last two sections explain how to rebuild the exports from your own
disc and how to load them in Blender.

The document copies no game data. It gives structure only: strides, counts, offsets, field layouts, opcodes
and the code's own constants. It contains no coordinates, palettes, sample records or hex dumps.

## Contents

1. [Conventions](#1-conventions)
2. [RAGE.BIN inventory by format](#2-ragebin-inventory-by-format)
3. [Track packs (.1ST and .2ND) and the scene header](#3-track-packs-1st-and-2nd-and-the-scene-header)
4. [Command streams (all model families)](#4-command-streams-all-model-families)
5. [Model banks](#5-model-banks)
6. [Course model table (scene block 5)](#6-course-model-table-scene-block-5)
7. [Terrain cells (scene block 7)](#7-terrain-cells-scene-block-7)
8. [Course objects and placement](#8-course-objects-and-placement)
9. [Car model assets](#9-car-model-assets)
10. [Other model-bearing assets](#10-other-model-bearing-assets)
11. [Textures and VRAM layout](#11-textures-and-vram-layout)
12. [Hypotheses](#12-hypotheses)
13. [Open questions](#13-open-questions)
14. [How to export](#14-how-to-export)
15. [How to import in Blender](#15-how-to-import-in-blender)

---

## 1. Conventions

Path abbreviations:

| short | path |
|---|---|
| TS | `src/main/PAL/main/render/terrain_submission.s` (TS:n means line n) |
| DATA | `asm/PAL/main/data/main/6BE64.data.s` |
| C file names | relative to `src/main/PAL/main/`, e.g. `asset/model_banks.c:50` |
| headers | `include/game/…` |

`ctx` is the scratchpad at 0x1F800000. Every renderer in TS receives it in `$a0`. The model paths use these
slots:

| ctx offset | meaning | written by |
|---|---|---|
| +0x04 | ordering table base | frame setup |
| +0x48 | course model table (entries) | RegisterCourseModels, `asset/model_banks.c:104-131` |
| +0x4C | current course model's vertex pool | TransformCourseModel, TS:2128 |
| +0x50 / +0x54 / +0x58 | model bank: `&models[0]`, vertex pool, normal pool | SelectModelBank, `asset/model_banks.c:84-102` |
| +0x5C / +0x60 | terrain cell table, terrain vertex pool | InstallTerrainCellData, `asset/model_banks.c:133-155` (+0x60 at :150) |
| +0x64 | OT shift (OTZ = AVSZ4 >> shift) | InitRenderState, `asset/model_banks.c:43` |
| +0x68 | mirror flag (negates TRX and flips the cull test) | InitRenderState, `asset/model_banks.c:47` |
| +0x6C | terrain subdivision distance shift | InitRenderState, `asset/model_banks.c:31` |
| +0x70 / +0x74 | fixed RGBC for the fog FT4 / GT4 bank emitters | InitRenderState, `asset/model_banks.c:32-39` |
| +0x80 | command-stream cursor | the face loops |
| +0x84 | "env mode 4" word. It selects terrain handlers, and course models and bank FT4/GT4 add it to the CLUT word | every caller, just before it submits |
| +0x11C / +0x2FC | screen XY / Z cache of the current course model | TransformCourseModel, TS:2164-2165 |

Other conventions:

- **Units.** Model vertices are s16 GTE units. One vertex unit is a quarter of a world unit, the unit that track
  points, course objects and car positions use (section 8.1).
- **Angles.** 4096 = 360 degrees (`render/rot_matrix.c`).
- **Axes.** The PS1 axes are x right, y down, z forward. All exports convert to Blender axes:
  X = x, Y = z, Z = −y.
- **Names.** Asset, course, class and car names come from the EXE's own tables: g_AssetPaths at DATA:1676-1812,
  g_GrandPrixNames at DATA:2837-2849, g_CourseNames at DATA:2852-2857 and g_CarNames at DATA:2992-3006.

The four courses, by course index:

| index | name | file prefix |
|---|---|---|
| 0 | MYTHICAL COAST | BIG |
| 1 | OVER PASS CITY | MID |
| 2 | LAKESIDE GATE | HI |
| 3 | THE EXTREME OVAL | OVAL |

The Grand Prix classes 0..5 are CALME, BRISE, RAFALE, MISTRAL, TEMPETE and DIABLE (series 0).

## 2. RAGE.BIN inventory by format

**Proven.**

RAGE.BIN is the only file on the disc that holds models.

- **Index.** Sector 0 holds 135 `{u32 sector, u32 size}` pairs, and the rest of the sector is zero.
  LoadDiscArchiveIndex (`asset/asset_loader.c:102`) reads it.
- **Layout.** The assets tile sectors 1 onwards with no gaps.
- **Formats.** Each asset's format is fixed by the loader that consumes it:

| assets | file names (g_AssetPaths) | format | layout | models |
|---|---|---|---|---|
| 0, 1, 5, 6 | LOGO.TMS, TITLE.TMS, CAR.TMS, SAVE.TMS | image chain | the whole file is one TIM chain (section 11.1) | no |
| 2, 3 | RG3.VH, RG3.VB | VAB head / body | sound bank | no |
| 4 | RES.DAT | resource table | 20 s32 words; InstallResourceData only prints them | no |
| 7 | SELBGM.BIN | music pack | 3-word header, VAB head, SEQ, VAB body | no |
| 8 | SELECT.BIN | car / course select pack | 3-word header; model bank at +0xC; team-logo samples; course model table; image chain | yes (section 10) |
| 9 | OPTION.BIN | option screen pack | `{s32 imageOffset}`; model bank at +4; image chain | yes (section 10) |
| 10..72 even | CAR_xy.1ST | CarModelAsset | 0x28-byte header, 22-model bank, 64x256 VRAM image | yes (section 9) |
| 11..73 odd | CAR_xy.2ND | car spec pack | 5-word header; car spec; VAB head; audio parameters; VAB body; image chain | no |
| 74..85 | GP*.TMS | image chain | menu screens | no |
| 86 | VOICE.BIN | voice pack | `{vhSize, vhOffset, vbOffset}`, VAB head, body | no |
| 87..133 odd | {BIG,MID,HI,OVAL}{1..6}.1ST | track image pack | 5-block scene header, all image data (section 3.2) | no |
| 88..134 even | {BIG,MID,HI,OVAL}{1..6}.2ND | track scene pack | 11-block scene header (section 3.3) | yes |

Index formulas:

- **Car packs.** Car asset k is pack 10 + 2k (.1ST) and 11 + 2k (.2ND), where k = g_CarModelBaseIndex[car] + grade
  (`asset/model_banks.c:22` GetCarAssetIndex). Each stem is `CAR_%X%d` of (car, grade).
- **Track packs.** The .1ST pack is 0x57 + class·8 + course·2 and the .2ND pack is one more.
  - .1ST load: `asset/race_assets.c:98-100`.
  - .2ND load: `asset/fmv_requests.c:17`.
  - Base constants: `include/game/asset.h:79-80`.

Model-bearing assets and other sources:

- 58 of the 135 assets hold models: the 24 .2ND scene packs, the 32 car .1ST packs, SELECT.BIN and OPTION.BIN.
  The other 77 hold none.
- **The EXE embeds no models.**
  - g_ModelBanks, g_CarModelSlots and g_CarImageSlots are bss.
  - Every RegisterModelBank, RegisterCourseModels and SetCarModelSlot caller passes a pointer into a loaded asset.
  - The only image data in the EXE is a few small static tables (the team-name font glyphs, a blank tile and a
    blank CLUT).
- **RAGE.STR** holds only XA audio and FMV, and the CDDA tracks are audio.

`extracted/models/asset_index.txt` (written by `tools/export_other_models.py`) lists every asset with its format
and where its models were exported.

## 3. Track packs (.1ST and .2ND) and the scene header

### 3.1 GameSceneAssetHeader

**Proven.** `include/game/asset.h:246-248`.

```
s32 offsets[N]      N = 5 (.1ST) or 11 (.2ND); offsets from the start of the pack
```

- `offsets[0]` is 4·N, and the offsets ascend.
- Block i runs from offsets[i] to offsets[i+1], and the last block runs to the end of the file.
- No block has slack bytes; the decoders assert exact tiling for all 48 packs.

### 3.2 .1ST track image pack (5 blocks)

**Proven.** LoadRaceAssets (`asset/race_assets.c:98-126`) and InstallCourseAssets (`asset/install_course_assets.c:6-31`)
upload every block and nothing else:

| block | content | uploaded by |
|---|---|---|
| 0 | image chain | UploadImageAsset (`race_assets.c:108`) |
| 1 | image chain | UploadImageAsset (`race_assets.c:112`) |
| 2 | one bare TIM: the sky texture and its CLUT | UploadImageBlock (`race_assets.c:116`) |
| 3 | image chain: texture page set A | UploadImageAsset (`race_assets.c:122`) |
| 4 | image chain: texture page set B | UploadImageAsset (`race_assets.c:125`), after StoreTeamLogoImage (`race_assets.c:123`) |

Block 4 must start at least 0x38000 bytes into the pack. Before block 4 is read, StoreTeamLogoImage copies the
448x256 track-texture rectangle over the start of the pack buffer (section 11.3).

### 3.3 .2ND track scene pack (11 blocks)

**Proven.** LoadTrackDataAssets (`asset/fmv_requests.c:8-71`) installs each block in turn:

| block | content | installer (fmv_requests.c line) | layout |
|---|---|---|---|
| 0 | TrackRenderTable | SetTrackCameraTable (:21) | `include/game/camera_types.h:20-25`: s32 textureSectionLo, s32 textureSectionHi, s32 environment start time, then 11 CarModelRenderParams {s16 axis0, u16 axis1, u16 axis2, s16 horizon} (camera_types.h:13-18), 100 bytes |
| 1 | environment palette | SetEnvPaletteTable (:26) | 48-byte records of 16 RGB triples, 5-bit components (section 11.6) |
| 2 | environment script | SetEnvironmentScript (:31) | header {u32 sky row base, s32 length}, then 0x30-byte GameEnvironmentCue (`include/game/environment.h:18-25`), ended by time = −1 |
| 3 | model bank 1 | RegisterModelBank(…, 1) (:36) | section 5 |
| 4 | track points | InstallTrackPoints (:41; `track/install_track_points.c:13`) | s32 count; count x GameTrackPoint (0x18 bytes, `include/game/track.h:22-45`); then 12-byte arc centres to the end of the block |
| 5 | course model table | RegisterCourseModels (:46) | section 6 |
| 6 | model bank 2 | RegisterModelBank(…, 2) (:51) | section 5 |
| 7 | terrain cells | InstallTerrainCellData (:56) | section 7 |
| 8 | course objects | SetCourseObjects (:61; `race/course_event_runtime.c:41`) | section 8.3 |
| 9 | TrackEventData | InstallTrackEventData (:66; `asset/race_data_installation.c:17`) | `include/game/track.h:221-233`: event tables, then TrackEventOffsets at +0xB64 (track.h:113-120): six s32 offsets relative to the offset table itself (route scenery, race intro camera, path position keys, path rotation keys, reserved, flyby scenery) |
| 10 | camera table | SelectTrackCameraTable(…, 0) (:71) | track cameras (not a model format; not decoded here) |

Block sharing between packs, found by comparing whole blocks (the identity sets are asserted by
`tools/rr_assets.py --all` and `tools/rr_model_banks.py --all`):

- **Bank 1** is shared by the courses of one class.
- **Track points, bank 2 and course objects** follow the course.
- **OVAL1.2ND** (the new-game prologue scene, reached through EnterPrologue: `race/prologue.c:77` and :89-93,
  `menu/title_screen.c:275-292`) carries blocks 3-8 of BIG5.2ND. It has its own render table, events and cameras.
  `race/prologue.c:77` sets the course index to 0 before the scene starts, so its course-indexed drawers run as
  MYTHICAL COAST.
- **OVAL2.2ND** equals OVAL3.2ND except for blocks 1-2, the environment palette and script.

## 4. Command streams (all model families)

**Proven.** Model banks, course models and terrain cells all store faces the same way.

**Stream.** A stream is a sequence of commands, and a u32 0 ends it.

- Each command is one u32 word, followed by `count` faces:
  ```
  u32  lo16 = opcode, hi16 = face count
  ```
- The dispatchers re-read the cursor from ctx+0x80 after each command:
  - SubmitModel: TS:1007-1016
  - SubmitCourseModel: TS:1586-1610
  - SubmitTerrainCells: TS:64-72
- **The count is never 0.** Every face loop is a do-while (`addiu -1` / `bnez`; e.g. TS:310-313, 1144-1146, 1172-1174),
  so a count of 0 would loop 2^32 times. The decoders assert count > 0.

**Vertex indices.** Every face starts with four u16 vertex indices:
```
+0 u16 i0   +2 u16 i1   +4 u16 i2   +6 u16 i3
```
- Prim xy0..xy3 = i0..i3, so the order is the GPU strip order, v0 v1 / v2 v3.
- The GPU draws the quad as the triangles (v0 v1 v2) and (v1 v3 v2), so the polygon cycle is **v0 v1 v3 v2**.
- In every non-degenerate bank quad both strip triangles face the same way; a fan reading fits none of them.

**Front face.** A face is drawn when NCLIP(v0, v1, v2) > 0, i.e. clockwise on the y-down screen:
- banks: TS:1066-1075
- course models: TS:2274-2283
- terrain: TS:183-212

Mirror mode (ctx+0x68) flips the sign. A repeated index among v0..v2 gives NCLIP = 0, so such a face is never
drawn. A drawn face with i3 equal to i1 or i2 is a triangle.

**Depth.** OTZ = AVSZ4 >> shift.
- A face is rejected unless 1 ≤ OTZ ≤ 0x1BF (TS:1135-1137).
- Its OT slot is `ctx[4] + 0x200 + (OTZ + bias)·4` (TS:1042, 1161-1163; terrain TS:279-285).
- `bias` is a signed byte in the face record.

**Vertex records.** Every vertex pool uses 8-byte records `{s16 x, s16 y, s16 z, u16 pad}`.
- The emitters load only `+0` (x|y) and `+4` (z), with `lwc2` or `lw`/`lh`; nothing reads the pad.
- Evidence: TS:1050-1061 and 1080-1081 (banks), TS:123-143 and 163-200 (terrain).

## 5. Model banks

### 5.1 Where they are

**Proven.**

| source | bank start | bank end | registered as | evidence |
|---|---|---|---|---|
| .2ND block 3 | offsets[3] | offsets[4] | bank 1 | `asset/fmv_requests.c:36` |
| .2ND block 6 | offsets[6] | offsets[7] | bank 2 | `asset/fmv_requests.c:51` |
| CAR_xy.1ST | +0x20 field (always 0x28) | +0x24 field (image) | bank 0 or 1 | `asset/car_assets.c:83-90`, `:147-150`; `asset/menu_car_assets.c:35-38` |
| SELECT.BIN | +0xC | first header word | bank 14 | `asset/car_assets.c:56` |
| OPTION.BIN | +4 (OptionScreenAsset.modelBank) | imageOffset | bank 0 | `asset/menu_car_assets.c:80`; `include/game/asset.h:377` |

### 5.2 Header and pools

**Proven.** `include/game/asset.h:370-375`. RegisterModelBank (`asset/model_banks.c:50-62`) rebases every offset
onto the bank start. SelectModelBank (`asset/model_banks.c:84-102`) copies the pointers to ctx and sets
g_ModelBankCount.

```
+0x00 u32 modelCount
+0x04 s32 vertex pool offset    -> ctx+0x54   8-byte {s16 x, y, z, pad}
+0x08 s32 normal pool offset    -> ctx+0x58   8-byte {s16 nx, ny, nz, pad}
+0x0C s32 models[modelCount]    -> ctx+0x50   one command stream per model
```

The on-disc order, true for every bank, is:

1. the header;
2. the model streams, back to back and in index order (each stream's terminator lands exactly on the next model,
   and the last on the vertex pool);
3. the vertex pool;
4. the normal pool, which runs to the end of the bank.

Pool properties:

- **Both pools are shared.** One vertex pool and one normal pool serve all models of a bank, and models reuse
  each other's vertices.
- **No per-model vertex limit.** Every face transforms its own corners (RTPT on v0..v2 at TS:1063, RTPS on v3 at
  TS:1083).
- **Normals.** Every stored normal is a 4.12 unit vector or the sentinel (−1, −1, −1), an effectively zero
  normal.
- **Stale comment.** `include/game/scratchpad.h:177` says ctx+0x54 is never read. In fact TS:1039 loads it as the
  vertex pool base.

### 5.3 Dispatch

**Proven.**

- **SubmitModel** (TS:975) indexes `models[]` (TS:976-979) and calls SubmitModelFaces (TS:1028) once per command.
  - In the jal delay slot it adds 4 to the opcode (TS:1011, `addiu $a1,$a1,0x4`), so stored opcode k uses
    jtbl_8007DA14 entry k+4, the non-fog emitters (DATA:3793-3800).
  - The face count is `srl 16` (TS:1036).
- **SubmitModel2** (TS:1348) is identical except for a `nop` in that delay slot (TS:1384), so it reaches the fog
  emitters, entries 0..3 (DATA:3785-3792).
  - Nothing in PAL main.exe calls SubmitModel2 (no `jal`, no `j`, no pointer to it).
- Each jtbl_8007DA14 entry is a pair {handler, stride}.
- Every caller clamps the model index: `idx < g_ModelBankCount ? idx : 1`.

### 5.4 Face records

**Proven.** Unlisted bytes are never read.

| op | stride | emitter (SubmitModel / SubmitModel2) | fields after the 4 indices | evidence |
|---|---|---|---|---|
| 0 F4 | 0x10 | EmitPolyF4 TS:1153 / EmitPolyF4Fog TS:1397 | +8 u32 RGB + GPU code (copied to prim+4, code 0x28); +0xD s8 OT bias | TS:1158-1160, 1174 |
| 1 FT4 | 0x18 | EmitPolyFT4Raw TS:1181 / EmitPolyFT4Fog TS:1426 | +8 u8 u0, v0, u16 clut (ctx+0x84 added to the word); +0xC u8 u1, v1, u16 tpage; +0x10 u8 u2, v2, u3, v3; +0x15 s8 OT bias; code forced to 0x2D | TS:1186-1199, 1213 |
| 2 G4 | 0x18 | EmitPolyG4 TS:1220 / EmitPolyG4Fog TS:1464 | +8 u16 n0, n1; +0xC u16 n2, n3 (normal pool indices); +0x10 u32 RGB + code (GTE RGBC, lit with NCCT/NCCS, code 0x38); +0x15 s8 OT bias | TS:1225-1260, 1270 |
| 3 GT4 | 0x20 | EmitPolyGT4 TS:1277 / EmitPolyGT4Fog TS:1521 | +8 n0, n1; +0xC n2, n3; +0x10 u0, v0, clut (ctx+0x84 added); +0x14 u1, v1, tpage; +0x18 u2, v2, u3, v3; +0x1D s8 OT bias; lit with NCT/NCS; code forced to 0x3C | TS:1282-1315, 1334-1336, 1341 |

Each emitter steps by its own hard-wired stride (TS:1174, 1213, 1270, 1341). These strides equal the table strides
that SubmitModelFaces adds for culled faces (TS:1035, 1146).

The fog emitters read the same offsets:

- **F4Fog** runs the face colour through DPCS.
- **FT4Fog and GT4Fog** take a fixed RGBC from ctx+0x70 / +0x74 (InitRenderState) and do **not** add ctx+0x84.

In a triangle (i3 = i2), the repeated corner's uv and normal duplicate those of i2 (asserted).

### 5.5 What the banks hold

**Proven.** Counts and opcodes, from every bank:

| bank | models | opcodes present | used by |
|---|---|---|---|
| .2ND bank 1 | 38 (classes 1-5), 23 (class 6) | F4, GT4 | rival cars (section 9.2); the last three models are scenery props drawn by the block-9 drawers (section 8.5) |
| .2ND bank 2 | 1 | GT4 | DrawFlybyScenery (`track/draw_flyby_scenery.c:23-26`) |
| CAR_xy.1ST | 22 | F4, GT4 | the player car (section 9.1) |
| SELECT.BIN | 6 | F4, G4, GT4 | menu course dioramas and the car platform (section 10) |
| OPTION.BIN | 5 | GT4 | controller setup screen (section 10) |

No bank contains opcode 1 (FT4); its layout above comes from the asm alone.

## 6. Course model table (scene block 5)

### 6.1 Layout

**Proven.**

- RegisterCourseModels (`asset/model_banks.c:104-131`) rebases the entries and stores the table at ctx+0x48.
- CourseModelAssetEntry is in `include/game/asset.h:415-419`.

```
s32 count
{ s32 geometry; s32 vertexCount; s32 model; } [count]     offsets relative to the block
```

- The header names the middle field `reserved`. TransformCourseModel reads it as the vertex count.
- **On-disc order:** header, then every model's command stream back to back, then the 8-byte vertex records, which
  run exactly to the end of the block.
- Each entry's geometry is a contiguous slice of that pool. Ranges never overlap, except that identical ranges
  may be shared.
- 25 tables exist: the 24 .2ND packs and SELECT.BIN.

### 6.2 Transform

**Proven.**

- **SubmitCourseModel** (TS:1586) calls TransformCourseModel (TS:2128):
  - The entry is `table + idx·12`, and its geometry pointer goes to ctx+0x4C.
  - **All** `vertexCount` vertices are projected with RTPT, three at a time.
  - Screen XY goes to ctx+0x11C and Z to ctx+0x2FC (TS:2164-2165).
  - Faces then index that cache.
- **The cache limits a course model to 120 vertices**, and the decoders assert this.
- **TransformCourseModel loads no translation or rotation.** The caller's GTE matrix places the model, so course
  models are object-local; only a drawer gives them a world position (section 8).

### 6.3 Dispatch and face records

**Proven.**

- **SubmitCourseModel** dispatches through jtbl_8007DA14+0x40 (DATA:3801-3804).
- **SubmitCourseModel2** (TS:2101) dispatches through +0x50 (DATA:3805-3808). Its emitters are the fog / depth-cue
  versions and read the same record offsets.
- These entries hold only the handler: each handler steps by its own stride.

| op | stride | SubmitCourseModel / SubmitCourseModel2 handler | fields after the 4 indices | evidence |
|---|---|---|---|---|
| 0 F4 | 0x10 | EmitCoursePolyF4 TS:1613 / EmitCoursePolyF4Fog TS:2209 | +8 u32 RGB + code (to prim+4, 0x28); +0xD s8 OT bias | TS:1622-1624 |
| 1 FT4 | 0x1C | EmitCoursePolyFT4 TS:1647 / EmitCoursePolyFT4Fog TS:2365 | +8 u32 RGB + code (to prim+4, 0x2C modulated); +0xC u8 u0, v0, u16 clut (ctx+0x84 added); +0x10 u8 u1, v1, u16 tpage; +0x14 u8 u2, v2; +0x16 u8 u3, v3; +0x19 s8 OT bias | TS:1656-1670, 2378-2384 |
| 2 FT4, subdivided and windowed | 0x20 | EmitCourseSubdividedFT4 TS:1692 / SubmitCourseSubdividedFaces TS:2413 (via EmitCourseSubdividedFT4Fog TS:1727) | as op 1, plus +0x1A two u8 subdivision levels and +0x1C a GP0 E2 texture-window word (sent before the quad and reset with E2000000 after) | TS:1712-1716, 1748-1753, 2518, 2527, 2565-2567, stride TS:2580 |
| 3 FT4, scrolling | 0x20 | func_8002A550 TS:2587 / func_8002A550+0xA8 | as op 2 | TS:2588-2626, 2630-2672 |

**ctx+0x84 is a CLUT offset for course models.**

- Every textured course emitter adds the whole word to the `u0 v0 clut` word (e.g. TS:1659-1663).
- Every race-time caller writes ctx+0x84 just before submitting, always with a zero low half, so a face samples
  CLUT `clut + k`, where k = word >> 16 (section 11.5).

**Opcode 3 scrolls u.**

- func_8002A550 adds `D_8009E694 & 0x7F` to ctx+0x84 and stores it back (TS:2588-2593). D_8009E694 is g_AnimTimer
  (`configs/PAL/sym.bss.main.txt:253`).
- It then adds the low half of the word to all four uv words (TS:2613-2626).
- The data keeps this carry-free: opcode 3 is only ever a model's last command, and u + 0x7F never overflows (both
  asserted). The visible effect is therefore u + (g_AnimTimer & 0x7F), a 128-frame loop.
- Opcode 3 faces carry the semi-transparent GPU code (0x2E).

## 7. Terrain cells (scene block 7)

### 7.1 Block layout

**Proven.** InstallTerrainCellData (`asset/model_banks.c:133-155`); TerrainCellAssetHeader is in
`include/game/asset.h:394-398`.

| offset in block | content | evidence |
|---|---|---|
| +0x0000 | u16 grid[32][32]: low 10 bits = cell id (0x3FF = empty), high 6 bits = camera region | `track/visible_cells.c:181`, :100-103 |
| +0x0800 | u32 vis[32][32]: bit r set = the cell is visible from camera region r | IsCellVisibleFromRegion, `track/visible_cells.c:106-113` |
| +0x1800 | header {s32 cellCount; s32 poolOffset; s32 cellOffset[cellCount]}, offsets relative to this header; cells[] become ctx+0x5C, the pool ctx+0x60 | `asset/model_banks.c:145-154` |
| header + cellOffset[i] | cell i: one command stream (opcodes 0..5) | TS:43-72 |
| header + poolOffset | 8-byte vertex pool, to the end of the block | TS:116-144, 156-170 |

Tiling rules, asserted for all 24 scene packs:

- The streams follow the header directly and tile exactly up to the pool.
- Every cell id below cellCount appears exactly once in the grid.
- Every pool vertex is used by exactly one cell. All indices point into the one shared pool.

### 7.2 Dispatch

**Proven.**

SubmitTerrainCellFaces (TS:96) computes the handler index as:

```
index = (op < 2) ? op*2 + ctx[0x84] : op - 2        (TS:97-110)
```

- The index selects an entry of jtbl_8007D9F4 (DATA:3772-3781). Each entry is a {handler, stride} pair.
- ctx+0x84 is g_IsEnvironmentMode4, which is 0 or 1 (`track/load_environment_cue.c:55-57`). The race code copies
  it there before each terrain draw.
- Opcodes above 5 would index past the table.

| entry | handler | stride | colour | CLUT | texture window |
|---|---|---|---|---|---|
| 0 | func_80028120_mode_0 TS:369 | 0x20 | DPCS fog of the face colour | as stored | no |
| 1 | func_80028120_mode_1 TS:320 | 0x20 | face colour as stored | **clut + 1** (TS:321-322) | no |
| 2 | func_80028120_mode_2 TS:509 | 0x24 | DPCS fog | as stored | yes |
| 3 | func_80028120_mode_3 TS:417 | 0x24 | as stored | **clut + 1** (TS:418-419) | yes |

How each opcode maps to an entry:

| opcode | env mode 0 | env mode 1 | stride |
|---|---|---|---|
| 0 | entry 0 | entry 1 | 0x20 |
| 1 | entry 2 | entry 3 | 0x24 |
| 2 | entry 0 | entry 0 | 0x20 |
| 3 | entry 1 | entry 1 | 0x20 |
| 4 | entry 2 | entry 2 | 0x24 |
| 5 | entry 3 | entry 3 | 0x24 |

What the handlers emit (tag 0x09000000):

- Entries 0 and 1 emit one 0x28-byte POLY_FT4.
- Entries 2 and 3 emit a two-word texture-window packet, then the quad, then a window reset (TS:447-480).

### 7.3 Face record

**Proven.** The same layout is used by all four handlers.

| offset | field | evidence |
|---|---|---|
| +0x00 | 4 x u16 vertex indices; each address is index·8 + ctx[0x60] | TS:114-144, 154-170 |
| +0x08 | u8 u0, v0, u16 clut | TS:289, 340 |
| +0x0C | u8 u1, v1, u16 tpage | TS:290 |
| +0x10 | u8 u2, v2, u3, v3 | TS:291-293, 342-343 |
| +0x14 | u8 flags: bit 0 halves every u and v (and the texture window) when OTZ ≥ 0x800; bit 1 marks the face for the far skip (section 7.5) | TS:286-306, 434-442, 960-969 |
| +0x15 | s8 OT bias | TS:279-285 |
| +0x16, +0x17 | u8 subdivision levels along v0→v1 and v0→v2. OTZ >> ctx[0x6C] is subtracted from each, and the result is clamped to ≥ 0; if either level is still > 0, the face goes to EmitSubdividedTerrainQuad | TS:324-338, 645-647 |
| +0x18 | u32 GP0 monochrome polyline word (0x48RRGGBB). Before a face is subdivided, two 3-point lines (v0 v1 v3) and (v0 v2 v3) are linked 64 OT entries deeper. Bit 31 set means no lines | TS:202-210, 606, 617-636 |
| +0x1C | u32 RGB + code (0x2C, modulated POLY_FT4). The fog handlers run it through DPCS | TS:203-206, 350-353, 370-373, 401 |
| +0x20 | (stride 0x24 only) u32 GP0 E2 texture-window word | TS:435-478, 527-569 |

### 7.4 Subdivision

**Proven.**

- EmitSubdividedTerrainQuad (TS:601) and InterpolateSubdivRow (TS:863) split a face into 2^level steps along each
  axis. The GTE INTPL interpolates position and uv together.
- The output quads reuse the face's clut and tpage (TS:811-838).
- The row buffer holds 34 entries, so a level can be at most 5.

### 7.5 Far path

**Proven.**

- SubmitTerrainCells (TS:12) takes SubmitTerrainCellFacesFar (TS:944) when the cell's TRZ ≥ 0xA000 (TS:61-63).
- The far routine resolves the same handler and stride, then skips faces while flags bit 1 is set (TS:960-969).
  At the first face without the bit it jumps back into SubmitTerrainCellFaces, which draws the rest of the
  command.
- In every command of every pack the flagged faces form a prefix (asserted). So the far version of a cell is the
  near version minus every face with bit 1 set.

## 8. Course objects and placement

### 8.1 Units

**Proven.** Every placement builds the GTE translation as 4 · View · (position − camera):

- SetGteObjectMatrix (`render/draw_packet_queue.c:164`) shifts the relative position left by 2.
- BuildVisibleCells (`track/visible_cells.c:187-189`) does the same for cell origins.

So one vertex unit is ¼ world unit. A model vertex v drawn at position p with rotation M lands at
`4·p + M·v` in vertex units.

The rotation matrices (`render/rot_matrix.c`) are:

```
RotY = [[c,0,-s],[0,1,0],[s,0,c]]
RotX = [[1,0,0],[0,c,-s],[0,s,c]]
RotZ = [[c,-s,0],[s,c,0],[0,0,1]]
```

### 8.2 Terrain cells: world-fixed

**Proven.** BuildVisibleCells (`track/visible_cells.c:115`, cell lookup at :181, translation at :187-189) and
SubmitTerrainCells (TS:43-60).

- **Lookup.** Grid square (sx, sy) holds cell `grid[(31 − sy)·32 + sx] & 0x3FF`, so the stored rows are flipped.
- **Origin.** The cell's origin in vertex units is `((sx·2048 + 1024)·4, 0, (sy·2048 + 1024)·4)`. Vertex x and z
  are relative to the cell centre, and **y is absolute**, because TRY carries only the camera height.
- **Pitch.** The cell pitch is 2048 world units, and the grid covers 65536 x 65536 world units from the origin.
- **Rotation.** Cells rotate with the view only.

The track points independently confirm the flip, the origin and the x4 scale. Every track point of every pack
lies in an occupied cell, and almost all lie within 64 vertex units of the terrain surface. Without the flipped
rows, most of them miss.

GetCellRegion (`track/visible_cells.c:100`) reads the region bits **without** the flip (see Open questions).

### 8.3 Course objects (scene block 8)

**Proven.** CourseObjectTable is in `include/game/track_internal.h:8-22`; SetCourseObjects is at
`race/course_event_runtime.c:41`.

```
u32 count
{ s16 modelId; s16 rotationY; s32 x; s32 y; s32 z; s32 flags; } [count]     0x14 bytes each
```

The block size is exactly 4 + 0x14·count in all 24 packs. DrawCourseObjects (`track/visible_cells.c:21`):

- **Culling.** An object is skipped unless its grid square is in g_VisibleCellMask.
- **Placement.**
  - M = RotY(rotationY), multiplied with the view.
  - The translation is `s16((u16)pos − cam)` per axis, so coordinates wrap modulo 65536 around the camera.
  - The exporters unwrap y to the period nearest the track.
- **Flags** (`track/visible_cells.c:80-93`):
  - bit 3 (8): ctx+0x84 = `((g_AnimTimer & 0x10) == 0) << 16`, so the material switches between CLUT +0 and +1
    every 16 frames;
  - else bit 2 (4): ctx+0x84 = 1 << 16;
  - otherwise ctx+0x84 = 0.
  - Then SubmitCourseModel2 (fog) is used when `g_IsEnvironmentMode4 ? flags & 2 : flags & 1`, and SubmitCourseModel
    otherwise.
- **Holes.** modelId −1 is an empty slot and is skipped.
- **Clamp.** Every course-model caller clamps the id: `idx < g_CourseModelCount ? idx : 1`.

### 8.4 EXE-resident scenery drawers

**Proven.** These race-time drawers place course models from position and angle tables in the EXE (not
reproduced here):

| drawer | file:line | course models | rotation | condition |
|---|---|---|---|---|
| DrawStaticScenery | `track/scenery.c:21` | one landmark; an alternate model in env mode 4 | RotY | every course; fixed z offset on course 3 |
| DrawHighClassScenery | `track/scenery.c:86` | one model; CLUT +1 in env mode 4 | RotY | course 0, high classes |
| DrawStartGridScenery | `track/dynamic_track_views.c:153` | an animated figure, one model per frame | RotY | GP mode, not class 6, before the start |
| DrawAnimatedScenery | `track/dynamic_track_views.c:201` | animated screen frames plus companion models; some palette-cycled through ctx+0x84 = ((g_SceneTimer >> 3) & 3) << 16 | RotY·RotX | GP mode, not class 6 |
| DrawAnimatedScenery2 | `track/dynamic_track_views.c:311` | as above, for the prologue, BGM select, attract and replay scenes | RotY·RotX | non-race scenes |
| DrawSpinningScenery | `track/draw_spinning_scenery.c:9` | one spinner model | RotY·RotZ(spin) | courses 0 and 1 |
| DrawShuttleScenery | `track/update_shuttle_scenery.c:77` | a shuttle model moving between two endpoints | RotY·RotZ | courses 1 and 2 |

### 8.5 Block-9 scripted props (model banks)

**Proven.**

DrawScriptedScenery (`track/draw_scripted_scenery.c:11`) runs in GP mode and picks the drawers by class mod 5:

| class mod 5 | drawers |
|---|---|
| 0 | route |
| 1, 2 | route, flyby |
| 3, 4 | route, flyby, path |

| drawer | bank model | motion data (TrackEventData offsets) |
|---|---|---|
| DrawRouteScenery `track/draw_route_scenery.c:11` | bank 1 model 0x25 if the bank has at least 0x26 models, else model 1 (:25-33) | route scenery: start poses plus keyframes |
| DrawFlybyScenery `track/draw_flyby_scenery.c:10` | bank 2 model 0 (:23-26) | flyby scenery |
| DrawPathScenery `track/draw_path_scenery.c:16` | bank 1 model 0x23 (body) and 0x24 (rotor, spun by a timer) (:32-40, :57-64) | path position keys (0x14 bytes) and rotation keys (0xC bytes) |

Class-6 bank 1 has only 23 models, so on those courses these drawers can only reach the fallback model.

### 8.6 Sky and rivals

**Proven.**

- **The sky is not a mesh.** DrawSkyBackground (`track/draw_terrain_cells.c:49`) calls neither SubmitModel nor
  SubmitCourseModel. It builds screen-space POLY_FT4 strips with a fixed tpage and CLUT, which are the sky TIM's
  page at (512,256) and the CLUT row that UpdateEnvironment rewrites (:265-289, :437-460, section 11.6). It adds
  POLY_G4 gradients (:484-492).
- **Cars** are drawn from bank 1 (rivals) and from the car bank (player); see section 9.

## 9. Car model assets

### 9.1 Player cars (CAR_xy.1ST)

**Proven.** CarModelAsset is in `include/game/asset.h:185`. The loader is in `asset/car_assets.c:83-90` and
:147-150, and in `asset/menu_car_assets.c:35-38` (SetCarModelSlot `asset/model_banks.c:165`, RegisterModelBank).

| offset | field |
|---|---|
| +0x00 | s16 modelOffsetX, modelOffsetY, modelOffsetZ: front-wheel offset (world units) |
| +0x06 | s16 horizon: ground height below the model origin (world units) |
| +0x08..+0x1F | drivetrain, ratings and power data (not geometry) |
| +0x20 | s32 modelData offset, always 0x28: the model bank (section 5) with 22 models |
| +0x24 | s32 imageData offset: 0x8000 bytes, a 64x256 16-bit VRAM image |

UploadCarImage (`asset/model_banks.c:161`) loads the image to g_CarImageRect (DATA:1670-1673): x 704, y 0,
64 x 256. The image holds the body texels and the car's 16 CLUTs. For cars with paint options, the paint colours
are blended into the RAM copy before the upload (`asset/car_assets.c:97-103`).

DrawPlayerCarModel (`render/draw_player_car_model.c:21`) uses the 22 models like this:

| model | role | evidence |
|---|---|---|
| 0 | body | `draw_player_car_model.c:85` |
| 1 | flat quad at wheel-bottom level, drawn twice before the body | :75, :79 |
| 2 + d | single front wheel, drawn at (±4·ox, 4·oy, 4·oz); the second copy is turned 180° about Y | :87-130 |
| 3 + d | rear axle (both rear wheels), at the origin | :113 |

The wheel index offset is d = renderDepth·2, plus 10 when `wheelRotation & 0x1000`; the second wheel set is used
at speed.

- **renderDepth is the tyre compound.** It is g_PlayerTireCompound (`configs/PAL/sym.bss.main.txt:271`), written
  from CarEntry.tireCompound (`menu/swap_car_model_slot.c:113`) with values 0..4.
- So models 2..11 are five tyre compounds at rest and 12..21 the same compounds at speed.
- There are no LOD or damage models.
- The ground plane lies at y = 4·horizon below the origin.

### 9.2 Rival cars (bank 1 of each .2ND)

**Proven.** DrawCar is at `render/draw_player_car_model.c:142`. DrawCars selects bank 1
(`car/car_draw_and_knockback.c`).

**Lookup.** Grid slot m maps to a rival model and then to a bank range:

1. g_CarModelByCourse (DATA:2822-2834, u8[4][11]) gives the rival model for slot m.
2. g_CarModelBankTable (DATA:2807-2819, 11 entries {base model, CLUT shift}) gives that model's bank range.
3. TrackRenderTable.models[m] (scene block 0) supplies the wheel offset and horizon.

**Near** (otDepth < 0xD00, :179):

- base+1 twice;
- the base body with ctx+0x84 = shift << 16, i.e. CLUT + shift (:223-225);
- base+3, the rear axle;
- base+2, the front wheel, twice (mirrored).

**Far** (otDepth < 0x2500, :270): base+4 alone, also with the CLUT shift (:285-287).

The car bank uses opcodes F4 and GT4 only (section 5.5).

### 9.3 CAR_xy.2ND

**Proven.** This pack holds the car spec (0x160 bytes), sound data and an image chain. It contains no models.

## 10. Other model-bearing assets

**Proven.** Only SELECT.BIN and OPTION.BIN hold models outside the course and car packs.

| asset | structure | drawer | content |
|---|---|---|---|
| SELECT.BIN (8), model bank at +0xC, bank 14 | 6 models | DrawMenuCourseView `menu/swap_car_model_slot.c:194` (SelectModelBank(14); SubmitModel(index & 3), :283-291) | models 0..3: one diorama per course on the course-select screen |
| | | DrawMenuCarView `menu/swap_car_model_slot.c:31` (:179-189) | model 5: the platform under the car in the car-select view |
| | | none found | model 4: no draw site |
| SELECT.BIN course model table | 42 entries | DrawTeamNameCharModel `menu/swap_car_model_slot.c:307` (SubmitCourseModel at :390-398) | team-name characters: digits, letters and symbols. The drawer skips the space cell |
| OPTION.BIN (9), model bank at +4, bank 0 | 5 models | `pad/controller_setup_scene.c:24` (:53-140) | the controller, and the NeGcon halves and their twist markers, on the pad setup screens |

## 11. Textures and VRAM layout

### 11.1 Image chains and TIMs

**Proven.** UploadImageAsset (`asset/image_upload.c:52-69`) reads a chain of TIMs:

```
u32 tag                     (never read)
{ u32 size; TIM[size] } ... repeated while size > 0
u32 0                       (exactly at the end of the block)
```

- **The TIMs are standard.** UploadImageBlock (`asset/image_upload.c:8`) takes one TIM: u32 0x10, u32 flags
  (bit 3 = has CLUT, bits 0-2 = pixel mode), an optional CLUT block, then the image block. Each block is
  {u32 bytes = 12 + w·h·2, u16 x, y, w, h, data}.
- **CLUT-only TIMs.** A TIM with w or h = 0 uploads only its CLUT. The decoders assert size, rect and end of every
  link.
- **Bit depth.** All track and car-pack textures are 4-bit.

### 11.2 VRAM map during a race

**Proven.** The constants are from the code; the extents are the union of the uploaded TIM rects.

| region | content | evidence |
|---|---|---|
| (0,0) and (0,240), 320 x 240 each | double-buffered frame | `menu/display_setup.c:50-53` |
| x ≥ 320, outside the swap rect | texture pages from .1ST blocks 0-1 and CAR.TMS, plus menu remnants | TIM rects |
| (576,256) 448 x 256 | swapped track texture pages (.1ST blocks 3 and 4) | g_TrackTextureRect DATA:1866-1869 |
| (512,256) | sky texture (.1ST block 2) | its TIM rect |
| (704,0) 64 x 256 | player car image (texels and its 16 CLUTs) | g_CarImageRect DATA:1670-1673 |
| y ≥ 480 | most CLUT rows (the car's CLUTs are inside the car image), including the sky CLUT row that UpdateEnvironment rewrites | `track/environment_script.c:117` |

### 11.3 Race-load order and the two texture pages

**Proven.** LoadRaceAssets (`asset/race_assets.c:105-126`) uploads in this order:

1. .1ST block 0, then block 1;
2. the bare TIM, block 2;
3. block 3;
4. StoreTeamLogoImage (`asset/image_upload.c:71-83`): it loads the team-logo CLUT, moves a CLUT row in series 1,
   and **StoreImage's g_TrackTextureRect into the start of the pack buffer**. That copy is the texture shadow;
5. block 4;
6. ResetTrackTextureSwap (`render/track_textures.c:80`).

This leaves two versions of the 448 x 256 rect:

- **Page 0** is VRAM after block 4.
- **Page 1** is the shadow, i.e. the rect as blocks 0-3 left it.

SelectTrackTexturePage (`render/track_textures.c:15`) wants page 1 while textureSectionLo ≤ camera section <
textureSectionHi. Those bounds are words 0 and 1 of the render table (`render/load_track_texture_page_range.c:6-7`).
SwapTrackTextureRow (`render/track_textures.c:100`) and StepTrackTextureSwap (:141) exchange the two pages one
448-word row at a time, every frame.

Which page a face shows therefore depends on where the camera car is, not on the face. The exporters choose the
page per face by TIM pairing, and they assert that no texel is claimed by both pages with different colours.

### 11.4 tpage and CLUT words

**Proven.** GetTPage (`lib/libgpu/texture_primitives.c:115`, under `src/main/PAL/`) and GetClut (:137):

```
tpage = (tp & 3) << 7 | (abr & 3) << 5 | (y & 0x100) >> 4 | (x & 0x3FF) >> 6 | (y & 0x200) << 2
clut  = y << 6 | (x >> 4) & 0x3F
```

**Texel and code rules.**

- Texel word 0x0000 is transparent.
- The STP bit makes a texel semi-transparent only in primitives with the semi-transparent code bit: course
  opcode 3 (0x2E). The bank emitters force their code to 0x2D (FT4) or 0x3C (GT4).
- Every course tpage is 4-bit with ABR 0.

**Texture windows.** A GP0 E2 window `(maskU, maskV, offU, offV)` maps u to
`(u & ~(mask·8)) | ((off & mask)·8)` (and likewise for v). Course opcodes 2-3 and terrain opcodes 1, 4 and 5 use
it.

**UV halving.** Terrain flag bit 0 halves uv when OTZ ≥ 0x800 (section 7.3).

### 11.5 Runtime CLUT offsets

**Proven.** Adding `k << 16` to a `u v clut` word selects CLUT `clut + k`, the 16-entry CLUT k x 16 halfwords to
the right in VRAM.

| source of k | where it applies | evidence |
|---|---|---|
| Terrain entries 1 and 3 | +1 for opcodes 3 and 5 always, and for opcodes 0 and 1 in env mode 4 | TS:321-322, 418-419 |
| Course objects | flag 4: +1; flag 8: alternates between +0 and +1 every 16 frames | `track/visible_cells.c:80-87` |
| DrawHighClassScenery | +1 in env mode 4 | `track/scenery.c:99`, :107 |
| DrawAnimatedScenery(2) | 0..3, cycling every 8 frames | `track/dynamic_track_views.c:249-256` |
| Rival cars | the bank-table CLUT shift | `render/draw_player_car_model.c:223`, :285 |
| Model-bank FT4 / GT4 | as written by the caller (0 for scenery) | TS:1189-1194, 1303-1309 |

The fog emitters of the banks do not add the offset. Terrain uses ctx+0x84 only as a table index and never adds it
to a uv word.

### 11.6 Environment palette and sky CLUT

**Proven.**

- **UpdateEnvironment** (`track/environment_script.c:117`) blends two records of the environment palette:
  - It interpolates between g_EnvPaletteTable[previous mode] and [mode] with the GTE.
  - It packs the result as 15-bit colours and LoadImages one 16-entry CLUT row, the sky CLUT.
- **SeekEnvironmentScript** (`track/environment_script.c:10`) picks the start cue from the render table's start
  time (`race/race_scene.c:110`).
- **Class 6 freezes the palette.** SeekEnvironmentScript disables the script for class 6 (DIABLE), so the seek state
  holds for the whole race.
- **Env mode 4.** g_IsEnvironmentMode4 is 1 when the current cue's mode is 4 (`track/load_environment_cue.c:55-57`).

### 11.7 Car textures

**Proven.**

- **Body.** Body faces use the car image page at x 704 with the image's own CLUTs.
- **Hubs.** Wheel hubs sample CAR.TMS texels, which stay resident from boot.
- **Team-logo decal.** A few faces per car sample the team-logo canvas that the player draws in the menu. It is all
  zero (transparent) until the player edits it.

## 12. Hypotheses

Nothing in this section is proven. Each item says what would test it.

1. **TIM chain tag.** The tag word at the head of every image chain is a format or version id. Nothing reads it.
2. **Terrain flag bit 0 as a mip level.** Bit 0 (uv halving when far) points at a half-scale copy of the texture in
   the same page, a manual mip level. Test: compare the texels at (u/2, v/2) with those at (u, v) in a VRAM dump.
3. **Crack filling.** The terrain polylines (+0x18) hide T-junction cracks between subdivided and unsubdivided
   neighbours. The mechanism is proven; the purpose is inferred.
4. **Night palette.** The clut + 1 palettes (terrain opcodes 3 and 5 always, 0 and 1 in env mode 4; course
   objects with flag 4) are night or self-lit variants that sit next to the day CLUT. This fits the images that
   were viewed (lit windows), but it is not proven for every pair.
5. **Pad bytes.** The vertex and normal pad halfwords and the unread face bytes are leftovers from the authoring
   tool. They show no structure and have no reader.
6. **Shadow quad.** Car model 1 and rival part base+1 are a ground shadow or underfloor plate. This rests on the
   shape alone: a level quad at wheel-bottom height, drawn twice.
7. **Prop identities.** The last three bank-1 models are a helicopter body, its rotor disc and an airship, and the
   bank-2 flyby model is an aircraft. These come from bounding boxes and preview renders.
8. **Opponent textures.** .1ST block 0 holds the opponent car textures; no course face samples it.
9. **Class 6 grid.** Class 6 fields fewer rival models. Its bank 1 has only 4 car bases, so the higher grid slots
   would land on scenery models or the fallback.
10. **OVAL2.** OVAL2.2ND, a copy of OVAL3 with its own environment, is a slot that Grand Prix mode never races.
11. **Hide by palette.** Faces that sample a CLUT row nothing ever writes vanish on purpose in env mode 4.
12. **Sunk props.** Props that appear sunk into the ground (on LAKESIDE GATE and some road courses) stand on a
    lower terrain layer under the visible surface. Test: sample the lowest terrain layer under each object.

## 13. Open questions

- **Region lookup.** GetCellRegion reads the camera region from `grid[z·32 + x]` without the row flip that
  BuildVisibleCells applies to the cell ids. Is this a game bug or a separate layout?
- **Pad readers.** Does anything outside the renderers read the vertex pad halfword or the unread face bytes?
- **SubmitModel2.** Is it called by code outside PAL main.exe (no overlays are configured), or in the other
  regional builds?
- **Unplaced course models.** A few course models in each pack are never placed by any drawer found. Are they used
  by menus or replay cameras, or are they dead data?
- **Prop motion.** The route, flyby, path and shuttle props are exported at their first keyframe or endpoint.
  Keyframe playback, the rival start positions and the remaining TrackEventData sub-tables are not decoded.
- **Rival textures.** Bank-1 rival cars sample CLUT rows that the boot upload wrote, not the race load. Does the
  race upload the chosen rivals' textures at runtime?
- **Unread formats.** The environment palette semantics beyond the sky, the car audio parameter table and RES.DAT
  are not decoded.
- **Series index.** `menu/menu_mode.c:343` ORs the series into the course index. Where is it masked back out before
  the pack index is computed?
- **Untested states.** Mirror mode, series-1 positions, and the time-attack and replay states are not exported.

## 14. How to export

You need your own copy of the PAL disc. No game data is stored in this repository: `extracted/` is git-ignored
and must never be committed.

### 14.1 Inputs

1. `assets/PAL/main.exe`, the PAL executable SCES_006.50, as used by the decompilation build.
2. `extracted/assets_raw/asset_000.bin` … `asset_134.bin`: one file per RAGE.BIN entry, in index order.
   - The disc extractor `tools/assetbrowser/extract.py "<Track 01.bin>"` writes them as `raw/NNN_<name>.bin`;
     rename them to `asset_NNN.bin`. Any other split of RAGE.BIN by its sector-0 index also works.
   - Check them against the disc with:
     ```
     python -I tools/rr_assets.py --disc "<Track 01.bin>"
     ```

### 14.2 Validation passes (optional, recommended)

All tools are stdlib-only Python 3 and are run from the repository root.

| command | checks |
|---|---|
| `python -I tools/rr_assets.py --all` | inventory of all 135 assets, every model walked to the last face, mutation self-test |
| `python -I tools/rr_model_banks.py --all` | bank tiling, pools, normals, sharing |
| `python -I tools/rr_terrain_cells.py --all` | block 7 of all 24 scene packs |
| `python -I tools/rr_placement.py --all` | placement and units, track-point fit |
| `python -I tools/rr_textures.py --all --no-png` | image chains, VRAM replay, page and CLUT checks |

### 14.3 Exports

Run these in this order. Each one asserts its own checks and stops on the first failure.

| step | command | output |
|---|---|---|
| 1 | `python -I tools/export_course_scenes.py --all` | `extracted/models/courses/<COURSE>/<CLASSn_NAME>/` (about 270 MB) |
| 2 | `python -I tools/export_car_models.py` | `extracted/models/cars/<MAKER_NAME>/<CAR_xy or RIVAL_…>/` |
| 3 | `python -I tools/export_other_models.py` | `extracted/models/other/<group>/<name>/`, `extracted/models/previews/` |
| 4 | `python -I tools/export_other_models.py --index-only --require-complete` | `extracted/models/asset_index.txt`; fails unless every model-bearing asset has an export |

`extracted/models/README.txt` indexes the output folders with file, object and face counts.

### 14.4 Output conventions

- **Units and axes.** OBJ vertices are raw vertex units (¼ world unit), already in Blender axes (X = x, Y = z,
  Z = −y).
- **Winding.** Quads are written as the reversed polygon cycle **`f v0 v2 v3 v1`**, so that Blender normals face
  the side the game draws (section 4). The older single-purpose writers (`rr_assets.py --obj`, `rr_placement.py`,
  `export_course_models.py`) keep the literal `v0 v1 v3 v2`.
- **UVs.** vt = (u/256, 1 − v/256). Faces that read a single texel column or row are moved to the texel centre.
  Texture-window faces get period-sized tile textures with wrapped UVs.
- **Textures.** Materials are named after the tpage and the effective CLUT. Each texture is a 256 x 256 RGBA PNG
  of the 4-bit page sampled from the replayed race VRAM. Colour 0x0000 is alpha 0.
- **Course vertex colours.** Course faces carry their colour word as vertex colours (rgb / 255). The PS1 shows
  `min(1, texel · rgb / 128)`.
- **Course object layout.** Each course class folder holds:
  - `<STEM>.obj`: the race scene, in groups `terrain`, `course_models` and `scenery`;
  - `variants.obj`: other animation frames and env states;
  - `unplaced.obj`: pack models no race-time drawer places, shelf-packed for display;
  - `manifest.json`, `textures/` and previews.

## 15. How to import in Blender

`tools/blender_import_models.py` imports everything under `extracted/models`. It needs Blender 3.3 or newer
(`wm.obj_import`) and was written against 4.x.

**From the Scripting tab:**

1. Open the file with Text > Open.
2. Optionally edit the SETTINGS block (MODELS_ROOT, NAMES, ONLY, …).
3. Click Run Script.

**From a shell:**

```
blender --python tools/blender_import_models.py -- [options] [NAME ...]
```

NAME imports only the matching courses, classes, cars, variants or groups.
- The match ignores case, spaces and hyphens, and any part of a folder or file name can match:
  `"Mythical Coast"`, `MYTHICAL_COAST/CLASS1`, `BIG1`, `ERRISO`, `CAR_00`, `controller`.
- A `/` matches consecutive folder levels.
- Without a NAME everything is imported. All 24 course scenes take several minutes, so filtering is recommended.

The collection tree:

```
Courses > <COURSE> > <CLASSn_NAME (STEM)> > Terrain STEM, Course models STEM, Scenery STEM
                                            (+ Variants STEM, Unplaced STEM, hidden)
Cars    > <MAKER_NAME> > <CAR_xy | RIVAL_...>
Other   > <group> > <name>
```

Only the first course class imported stays visible; the others are hidden in the viewport.

Options:

| option | effect |
|---|---|
| `--root DIR` | the extracted/models folder (default: found next to the script) |
| `--only courses,cars,other` | import only these kinds |
| `--car-files default\|all` | one assembled car per variant (default tyre set), or every OBJ of a variant in its own sub-collection, with all but the default hidden |
| `--no-extras` | skip the course variants.obj and unplaced.obj |
| `--shading auto\|unlit\|lit` | auto = courses unlit (Emission), cars and other lit (Principled) |
| `--cull` | turn on back-face culling, as the game does |
| `--no-layout` | leave cars and other models at their own origins (by default they are spread out in rows) |
| `--no-alpha-check` | treat every textured material as alpha clip without reading its PNG |
| `--folder DIR --pattern GLOB` | generic mode: one collection per OBJ in DIR (replaces the old `blender_import_course_models.py`) |
| `--save PATH` | save the .blend afterwards |

What the importer sets up:

- **Import axes.** OBJ files are imported with Forward Y, Up Z.
- **Texture sampling.** Every image texture node uses Closest interpolation.
- **Alpha.** The importer reads each texture's alpha:
  - opaque textures get no alpha;
  - cut-out textures use alpha clip at 0.5;
  - `_semi_` materials (PS1 semi-transparent) use alpha blend.
- **Course colours.** Course materials multiply the texture (read as Non-Color) by a `ps1_modulation` colour
  attribute (vertex colour · 255/128), clamp the product to 1 and decode it to linear. With the Standard view
  transform, which the script selects, the colours on screen equal the PS1 values.
- **Viewport.** Every 3D viewport and the scene camera get clip start 4 and clip end 2,000,000, because the scenes
  are in raw vertex units (a course spans over 250,000 units). Viewport shading is set to show textures.
