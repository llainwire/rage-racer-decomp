#!/usr/bin/env python3
"""
Inventory of every asset in RAGE.BIN of Rage Racer (PAL, SCES-006.50).

Classifies all 135 assets (extracted/assets_raw/asset_000..134.bin) by the
loader that consumes them, splits each one into typed sub-blocks and validates
every header against the real file sizes.  Every model-bearing block (model
banks, course model tables, terrain cells) is walked down to the last face:
command streams, strides, terminators, vertex / normal indices and the GP0
words the emitters copy straight into GPU packets.  Names come from the EXE's
own tables (g_AssetPaths, g_CourseNames, g_GrandPrixNames, g_CarNames,
g_CarClassNames, g_CarModelBaseIndex), read out of assets/PAL/main.exe.

Importable, no side effects on import:
    asset_inventory(index, data=None, collect=False)
                                -> {index, path, name, format, size, blocks, errors}
    inventory_all()             -> [asset_inventory(i) for i in 0..134]
    asset_models(index)         -> model-bearing blocks with full face records
                                   (raises FormatError if the asset has any error)
    track_models(index)         -> {"bank1", "course_models", "bank2", "terrain"}
                                   for a track .2ND pack (88, 90, .., 134)
    write_obj(index, path, what=("terrain", "course_models", "banks"))
    asset_name(index)           -> (path, name, format)
    parse_tim / parse_image_chain / parse_model_bank /
    parse_course_model_table / parse_terrain_cells / parse_vab_header / walk_stream
    list_disc_files(track01_bin), check_rage_bin(track01_bin), track_block_identity()
Each block is a dict {kind, offset, size, ...}; offsets are absolute file
offsets inside the asset.  Model-bearing blocks carry 'parts' (sub-ranges);
image blocks carry 'tims' (VRAM rects).  With collect=True model blocks also
carry vertex_list / normal_list and model_list / entry_list / cell_list, whose
'faces' are dicts {offset, op, prim, idx, rgb, code, uv, clut, tpage, ot_bias,
normals, ...} (see _face).

CLI:
    python -I tools/rr_assets.py                 # asset_088, compact
    python -I tools/rr_assets.py 8               # any asset by index / name
    python -I tools/rr_assets.py 88 --obj out.obj [--what terrain,course_models,banks]
    python -I tools/rr_assets.py --list          # one line per asset
    python -I tools/rr_assets.py --all           # validate all 135 assets (+ mutation self-test)
    python -I tools/rr_assets.py --disc [BIN]    # ISO listing + RAGE.BIN check
"""
from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "extracted" / "assets_raw"
EXE_PATH = REPO / "assets" / "PAL" / "main.exe"
DISC_CANDIDATES = [
    REPO / "disc",
    Path("D:/Recomps and Decomps/ROMS/ps/Rage Racer (Europe)/Rage Racer (Europe)"),
    Path.home() / "Downloads/Rage Racer (Europe)/Rage Racer (Europe)",
]

ASSET_COUNT = 135
EXE_BASE, EXE_HEADER = 0x80010000, 0x800

# Data addresses in the PAL EXE (asm/PAL/main/data/main/6BE64.data.s).
ADDR = {
    "g_CarModelBaseIndex": 0x8007C464,   # u8[13]
    "g_CarImageRect": 0x8007C484,        # Rect {0x2C0, 0, 0x40, 0x100}
    "g_AssetPaths": 0x8007C48C,          # char *[135]
    "g_StreamCdEntries": 0x8007C6A8,     # {sector offset, count}[11]
    "g_TrackTextureRect": 0x8007C710,    # Rect {0x240, 0x100, 0x1C0, 0x100}
    "g_CarModelByCourse": 0x8007D3AC,    # u8[4][11]
    "g_GrandPrixNames": 0x8007D3D8,      # char *[11]
    "g_CourseNames": 0x8007D404,         # char *[4]
    "g_CarNames": 0x8007D4D4,            # char *[13]
    "g_CarClassNames": 0x8007D508,       # char *[13]
}

# Fallbacks, identical to what the EXE tables hold (used only if main.exe is
# missing).
_FALLBACK = {
    "course_names": ["MYTHICAL COAST", "OVER PASS CITY", "LAKESIDE GATE", "THE EXTREME OVAL"],
    "gp_names": ["CALME", "BRISE", "RAFALE", "MISTRAL", "TEMPETE", "DIABLE",
                 "AISANCE", "AGITATION", "IRRITATION", "COLERE", "RAGE"],
    "car_names": ["ERRISO", "ABEILLE", "PEGASE", "ESPERANZA", "ACCERON", "BAYONET", "HIJACK",
                  "FATALITA", "ISTANTE", "GHEPARDO", "VAINQURE", "BULSHADE", "SQUALDON"],
    "car_makers": ["AGE", "AGE", "AGE", "GNADE", "LIZARD", "LIZARD", "LIZARD",
                   "ASSOLUTE", "ASSOLUTE", "ASSOLUTE", "AGE", "LIZARD", "ASSOLUTE"],
    "car_base": [0, 4, 7, 9, 14, 18, 21, 23, 26, 28, 29, 30, 31],
}
COURSE_KEYS = ("BIG", "MID", "HI", "OVAL")

# include/game/asset.h
ASSET_ROUND_SCREEN_BASE = 0x4A
ASSET_TIME_ATTACK_ROUND_SCREEN = 0x55
ASSET_VOICE_BANK = 0x56
ASSET_TRACK_1ST_BASE = 0x57
TRACK_TEXTURE_SHADOW_SIZE = 0x38000
TERRAIN_CELL_GRID_SIZE = 0x800        # include/game/track.h:685
CELL_VISIBILITY_TABLE_SIZE = 0x1000   # include/game/track.h:686
CAR_IMAGE_RECT = (0x2C0, 0, 0x40, 0x100)
CAR_PAINT_PALETTE_OFFSET = 0x7060     # include/game/car.h CarImageData
TEAM_LOGO_SAMPLE_SIZE = 0x40 + 0x800  # u16 clut[2][16] + u16 canvas[64][16]
COURSE_OBJECT_SIZE = 0x14             # include/game/track_internal.h:17
TRACK_POINT_SIZE = 0x18               # GameTrackPoint
TRACK_EVENT_OFFSETS_AT = 0xB64        # TrackEventData.offsets (reservedB7C - 0x18)
TRACK_RENDER_MODELS = 11              # g_CarModelByCourse rows hold 0..10
COURSE_MAX_VERTS = 120                # TransformCourseModel: ctx+0x11C+4i / ctx+0x2FC+2i

# Terrain-cell placement (BuildVisibleCells, track/visible_cells.c:180-189):
# cell id = grid[(31 - sy) * 32 + sx] & 0x3FF (0x3FF = empty); the GTE
# translation is ((sx << 11) + 1024 - camera) << 2, so pool vertices are in
# units of 1/4 world unit around the cell centre (y origin 0).
EMPTY_CELL = 0x3FF
CELL_WORLD_SIZE, CELL_WORLD_CENTRE, VERTEX_SCALE = 2048, 1024, 4

# ---------------------------------------------------------------- face layouts
# Every face starts with two words holding four u16 vertex indices
# (idx0 lo16, idx1 hi16, idx2 lo16, idx3 hi16), strip order v0 v1 / v2 v3.
# 'tex' = offset of three words {u0 v0 clut}, {u1 v1 tpage}, {u2 v2 u3 v3};
# 'rgb' = offset of an RGB+code word; 'code' = the GP0 code family (code & 0xFC)
# that word must carry because the emitter copies it into the packet as is.
#
# Model banks: SubmitModel adds 4 to the opcode (terrain_submission.s:1011) and
# dispatches jtbl_8007DA14 entries 4..7 (EmitPolyF4 / FT4Raw / G4 / GT4); the
# strides are the emitters' cursor steps (:1174, :1213, :1270, :1341).
BANK_LAYOUT = {
    0: {"stride": 0x10, "prim": "F4", "rgb": 0x8, "code": 0x28, "bias": 0xD},        # :1158-1160
    1: {"stride": 0x18, "prim": "FT4", "tex": 0x8, "bias": 0x15},                     # :1186-1199
    2: {"stride": 0x18, "prim": "G4", "normals": 0x8, "rgb": 0x10, "code": 0x38,      # :1225-1260
        "bias": 0x15},
    3: {"stride": 0x20, "prim": "GT4", "normals": 0x8, "tex": 0x10, "bias": 0x1D},   # :1282-1315
}
# Course model tables: SubmitCourseModel dispatches jtbl_8007DA14+0x40
# (EmitCoursePolyF4, EmitCoursePolyFT4, EmitCourseSubdividedFT4, func_8002A550).
COURSE_LAYOUT = {
    0: {"stride": 0x10, "prim": "F4", "rgb": 0x8, "code": 0x28, "bias": 0xD},        # :1622-1624
    1: {"stride": 0x1C, "prim": "FT4", "rgb": 0x8, "code": 0x2C, "tex": 0xC,         # :1656-1670
        "bias": 0x19},
    2: {"stride": 0x20, "prim": "FT4", "rgb": 0x8, "tex": 0xC, "bias": 0x19,         # :2518-2567
        "subdiv": 0x1A, "texwin": 0x1C},
    3: {"stride": 0x20, "prim": "FT4", "rgb": 0x8, "tex": 0xC, "bias": 0x19,
        "subdiv": 0x1A, "texwin": 0x1C},
}
# Terrain cells: SubmitTerrainCellFaces (terrain_submission.s:97-113) picks
# jtbl_8007D9F4[op < 2 ? op * 2 + ctx[0x84] : op - 2]; ctx[0x84] is
# g_IsEnvironmentMode4 (0 or 1).  Entries: 0 fog FT4 / 0x20, 1 raw FT4 with
# clut+1 / 0x20, 2 fog FT4 + texture window / 0x24, 3 raw clut+1 + texture
# window / 0x24.  So the stride depends on the opcode only.
_TERRAIN_BASE = {"prim": "FT4", "tex": 0x8, "flags": 0x14, "bias": 0x15, "subdiv": 0x16,
                 "line": 0x18, "rgb": 0x1C, "code": 0x2C}
TERRAIN_HANDLERS = ("fog", "raw_clut+1", "fog_texwin", "raw_clut+1_texwin")
TERRAIN_LAYOUT = {}
for _op, (_stride, _variant) in {0: (0x20, "env? raw_clut+1 : fog"), 1: (0x24, "env? raw_clut+1_texwin : fog_texwin"),
                                 2: (0x20, "fog"), 3: (0x20, "raw_clut+1"),
                                 4: (0x24, "fog_texwin"), 5: (0x24, "raw_clut+1_texwin")}.items():
    TERRAIN_LAYOUT[_op] = dict(_TERRAIN_BASE, stride=_stride, variant=_variant)
    if _stride == 0x24:
        TERRAIN_LAYOUT[_op]["texwin"] = 0x20
del _op, _stride, _variant

# Backwards-compatible stride maps.
BANK_STRIDE = {k: v["stride"] for k, v in BANK_LAYOUT.items()}
COURSE_STRIDE = {k: v["stride"] for k, v in COURSE_LAYOUT.items()}
TERRAIN_STRIDE = {k: v["stride"] for k, v in TERRAIN_LAYOUT.items()}


# ---------------------------------------------------------------- utilities
def u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def s32(d, o):
    return struct.unpack_from("<i", d, o)[0]


def raw_path(index):
    return RAW_DIR / ("asset_%03d.bin" % index)


def load_asset(index):
    return raw_path(index).read_bytes()


class FormatError(ValueError):
    pass


class Problems(list):
    def check(self, cond, msg):
        if not cond:
            self.append(msg)
        return bool(cond)


def _blk(kind, offset, size, **extra):
    b = {"kind": kind, "offset": offset, "size": size}
    b.update(extra)
    return b


# ---------------------------------------------------------------- EXE tables
@lru_cache(maxsize=None)
def exe_tables(exe_path=str(EXE_PATH)):
    """Read the naming tables out of the PAL EXE (falls back to constants)."""
    p = Path(exe_path)
    if not p.exists():
        t = dict(_FALLBACK)
        t["asset_paths"] = None
        t["source"] = "fallback"
        return t
    exe = p.read_bytes()

    def off(a):
        return a - EXE_BASE + EXE_HEADER

    def cstr(a):
        o = off(a)
        return exe[o:exe.index(b"\0", o)].decode("latin-1")

    def ptrs(name, n):
        return [cstr(u32(exe, off(ADDR[name]) + 4 * i)) for i in range(n)]

    t = {
        "asset_paths": ptrs("g_AssetPaths", ASSET_COUNT),
        "course_names": ptrs("g_CourseNames", 4),
        "gp_names": ptrs("g_GrandPrixNames", 11),
        "car_names": ptrs("g_CarNames", 13),
        "car_makers": ptrs("g_CarClassNames", 13),
        "car_base": list(exe[off(ADDR["g_CarModelBaseIndex"]):off(ADDR["g_CarModelBaseIndex"]) + 13]),
        "car_model_by_course": [list(exe[off(ADDR["g_CarModelByCourse"]) + 11 * c:
                                         off(ADDR["g_CarModelByCourse"]) + 11 * c + 11]) for c in range(4)],
        "stream_entries": [struct.unpack_from("<II", exe, off(ADDR["g_StreamCdEntries"]) + 8 * i)
                           for i in range(11)],
        "car_image_rect": struct.unpack_from("<4H", exe, off(ADDR["g_CarImageRect"])),
        "track_texture_rect": struct.unpack_from("<4H", exe, off(ADDR["g_TrackTextureRect"])),
        "exe_sha1": hashlib.sha1(exe).hexdigest(),
        "source": "exe",
    }
    return t


def car_of_asset(index):
    """(car asset k, model m, grade g, half) for assets 10..73."""
    k, half = divmod(index - 10, 2)
    base = exe_tables()["car_base"]
    m = max(i for i in range(13) if base[i] <= k)
    return k, m, k - base[m], ("1ST", "2ND")[half]


def track_of_asset(index):
    """(course 0..3, class 0..5, half) for assets 87..134."""
    rel = index - ASSET_TRACK_1ST_BASE
    return (rel >> 1) & 3, rel >> 3, ("1ST", "2ND")[rel & 1]


FIXED = {
    0: ("tms", "boot logo images (InitAssetSystem)"),
    1: ("tms", "title screen images (LoadBootAssets 1)"),
    2: ("vab_vh", "main sound bank header RG3 (audio slot 0)"),
    3: ("vab_vb", "main sound bank body RG3 (audio slot 0)"),
    4: ("res_dat", "resource parameters (InstallResourceData prints only)"),
    5: ("tms", "car / HUD shared images (LoadBootAssets 6, resident)"),
    6: ("tms", "memory-card screen images"),
    7: ("selbgm", "BGM-select sound: VAB + SEQ"),
    8: ("select_bin", "car-select screen: model bank 0xE, course models, logo samples, images"),
    9: ("option_bin", "option screen: model bank 0, images"),
}


def asset_name(index):
    """(disc path, readable in-game name, format) for one asset."""
    t = exe_tables()
    path = t["asset_paths"][index] if t["asset_paths"] else None
    if index < 10:
        fmt, name = FIXED[index]
    elif index < 74:
        k, m, g, half = car_of_asset(index)
        fmt = "car_1st" if half == "1ST" else "car_2nd"
        what = "model bank + texture page" if half == "1ST" else "spec + engine sound + images"
        name = "%s %s body %d (CAR_%X%d.%s, %s)" % (t["car_makers"][m], t["car_names"][m], g, m, g, half, what)
    elif index < 86:
        r = index - ASSET_ROUND_SCREEN_BASE
        s, c = divmod(r, 6)
        fmt = "tms"
        if s == 0 or c < 5:
            gp = t["gp_names"][s * 6 + c]
            name = "round screen series %d class %d (%s GRANDPRIX)" % (s, c + 1, gp)
        else:
            name = "round screen series 1 class 6 slot = time-attack round screen"
    elif index == ASSET_VOICE_BANK:
        fmt, name = "voice_bin", "race voice / announcer sound bank (audio slot 2)"
    else:
        course, cls, half = track_of_asset(index)
        fmt = "track_1st" if half == "1ST" else "track_2nd"
        what = "course textures" if half == "1ST" else "course geometry + scene data"
        name = "%s class %d %s (%s%d.%s, %s)" % (
            t["course_names"][course], cls + 1, t["gp_names"][cls], COURSE_KEYS[course], cls + 1, half, what)
        if course == 3 and cls == 0:
            # title_screen.c:275-279 (no save: class 0, course 3) -> prologue
            # scene (frontend.c:32-35, g_SceneId 0x1F = EnterPrologue).
            name += "; new-game prologue scene, Mythical Coast data"
    return path, name, fmt


# ---------------------------------------------------------------- images
def parse_tim(d, o, end, probs=None):
    """One TIM (UploadImageBlock): magic 0x10, flags (bit3 = CLUT), CLUT block,
    pixel block.  Returns a dict with VRAM rects (w in 16-bit units)."""
    probs = probs if probs is not None else Problems()
    magic, flags = struct.unpack_from("<II", d, o)
    probs.check(magic == 0x10, "TIM@%X magic %X" % (o, magic))
    probs.check(flags in (0, 1, 2, 3, 8, 9), "TIM@%X flags %X" % (o, flags))
    p = o + 8
    clut = None
    if flags & 8:
        bn, x, y, w, h = struct.unpack_from("<IHHHH", d, p)
        probs.check(bn == 12 + 2 * w * h, "TIM@%X clut bnum %X != 12+2*%d*%d" % (o, bn, w, h))
        clut = (x, y, w, h)
        p += bn
    bn, x, y, w, h = struct.unpack_from("<IHHHH", d, p)
    probs.check(bn == 12 + 2 * w * h, "TIM@%X pixel bnum %X != 12+2*%d*%d" % (o, bn, w, h))
    pix_off = p + 12
    p += bn
    probs.check(p <= end, "TIM@%X runs past %X" % (o, end))
    bpp = (4, 8, 16, 24)[flags & 3]
    return {"offset": o, "size": p - o, "bpp": bpp, "clut": clut,
            "rect": (x, y, w, h), "pixel_offset": pix_off}


def parse_image_chain(d, o, end, probs=None):
    """UploadImageAsset chain (image_upload.c): u32 tag (never read, always
    0x100 on disc), then {u32 size; TIM[size]} links until a size <= 0."""
    probs = probs if probs is not None else Problems()
    tag = s32(d, o)
    p = o + 4
    tims = []
    while True:
        probs.check(p + 4 <= end, "chain@%X: no terminator before %X" % (o, end))
        if p + 4 > end:
            break
        size = s32(d, p)
        p += 4
        if size <= 0:
            break
        t = parse_tim(d, p, p + size, probs)
        probs.check(t["size"] == size, "chain@%X link@%X size %X != TIM %X" % (o, p, size, t["size"]))
        tims.append(t)
        p += size
        if p > end:
            probs.append("chain@%X overruns %X" % (o, end))
            break
    probs.check(p == end, "chain@%X ends %X, block ends %X" % (o, p, end))
    return {"tag": tag, "end": p, "tims": tims}


def tim_summary(tims):
    c = Counter(t["bpp"] for t in tims)
    n_clut_only = sum(1 for t in tims if t["rect"][2] == 0 or t["rect"][3] == 0)
    return "%d TIMs (%s; %d CLUT-only)" % (len(tims), ", ".join("%dbpp x%d" % kv for kv in sorted(c.items())),
                                          n_clut_only)


# ---------------------------------------------------------------- models
def _face(d, p, op, lay, idx):
    """Decode one face record at absolute offset p with layout lay."""
    f = {"offset": p, "op": op, "prim": lay["prim"], "idx": idx, "rgb": None, "code": None,
         "uv": None, "clut": None, "tpage": None, "normals": None,
         "ot_bias": struct.unpack_from("<b", d, p + lay["bias"])[0]}
    if "rgb" in lay:
        r, g, b, c = d[p + lay["rgb"]:p + lay["rgb"] + 4]
        f["rgb"], f["code"] = (r, g, b), c
    if "tex" in lay:
        w0, w1, w2 = struct.unpack_from("<3I", d, p + lay["tex"])
        f["uv"] = ((w0 & 0xFF, (w0 >> 8) & 0xFF), (w1 & 0xFF, (w1 >> 8) & 0xFF),
                   (w2 & 0xFF, (w2 >> 8) & 0xFF), ((w2 >> 16) & 0xFF, w2 >> 24))
        f["clut"], f["tpage"] = w0 >> 16, w1 >> 16
    if "normals" in lay:
        f["normals"] = struct.unpack_from("<4H", d, p + lay["normals"])
    if "flags" in lay:
        f["flags"] = d[p + lay["flags"]]
    if "subdiv" in lay:
        f["subdiv"] = (d[p + lay["subdiv"]], d[p + lay["subdiv"] + 1])
    if "line" in lay:
        f["line"] = u32(d, p + lay["line"])
    if "texwin" in lay:
        f["texwin"] = u32(d, p + lay["texwin"])
    if "variant" in lay:
        f["variant"] = lay["variant"]
    return f


def walk_stream(d, p, end, layout, nverts, probs, tag, nnorms=None, collect=False, used=None):
    """u32 command stream: lo16 opcode, hi16 face count, faces, ..., u32 0.

    Asserts: opcode in layout; count > 0 (every face loop is a do-while on the
    decremented count, e.g. terrain_submission.s:1144-1146 / :310-313, so 0
    would run 2^32 times); faces inside [p, end); every vertex index < nverts;
    every normal index < nnorms; the GP0 words the emitters copy verbatim
    carry the right command byte.  Returns (end_offset, Counter(op->faces),
    faces or None)."""
    ops = Counter()
    faces = [] if collect else None
    while True:
        if not probs.check(p + 4 <= end, "%s: stream runs past %X" % (tag, end)):
            return p, ops, faces
        cmd = u32(d, p)
        p += 4
        if cmd == 0:
            return p, ops, faces
        op, n = cmd & 0xFFFF, cmd >> 16
        if not probs.check(op in layout, "%s: opcode %d at %X" % (tag, op, p - 4)):
            return p, ops, faces
        if not probs.check(n > 0, "%s: opcode %d with face count 0 at %X" % (tag, op, p - 4)):
            return p, ops, faces
        lay = layout[op]
        st = lay["stride"]
        if not probs.check(p + n * st <= end, "%s: %d faces op %d at %X run past %X" % (tag, n, op, p - 4, end)):
            return p, ops, faces
        for _ in range(n):
            idx = struct.unpack_from("<4H", d, p)
            if max(idx) >= nverts:
                probs.append("%s: vertex index %d >= %d at %X" % (tag, max(idx), nverts, p))
            if used is not None:
                used.update(idx)
            if nnorms is not None and "normals" in lay:
                nidx = struct.unpack_from("<4H", d, p + lay["normals"])
                if max(nidx) >= nnorms:
                    probs.append("%s: normal index %d >= %d at %X" % (tag, max(nidx), nnorms, p))
            if "code" in lay and (d[p + lay["rgb"] + 3] & 0xFC) != lay["code"]:
                probs.append("%s: op %d face at %X code %02X not %02X family" % (
                    tag, op, p, d[p + lay["rgb"] + 3], lay["code"]))
            if "texwin" in lay and d[p + lay["texwin"] + 3] != 0xE2:
                probs.append("%s: op %d face at %X texture-window word %08X lacks GP0 E2" % (
                    tag, op, p, u32(d, p + lay["texwin"])))
            if "line" in lay:
                w = u32(d, p + lay["line"])
                if not (w >> 31) and ((w >> 24) & 0xFC) != 0x48:
                    probs.append("%s: op %d face at %X line word %08X is neither off nor GP0 48..4B" % (
                        tag, op, p, w))
            if collect:
                faces.append(_face(d, p, op, lay, idx))
            p += st
        ops[op] += n


def _vertices(d, o, n):
    return [struct.unpack_from("<3hH", d, o + 8 * i) for i in range(n)]


def parse_model_bank(d, o, end, slot=None, probs=None, collect=False):
    """ModelBankHeader {u32 count; s32 vertexPool; s32 normalPool; s32 models[count]},
    offsets relative to the bank start (RegisterModelBank, model_banks.c).
    SubmitModelFaces reads vertices through ctx+0x54 = bank+[+4]
    (terrain_submission.s:1039, :1053-1061) and normals through ctx+0x58 =
    bank+[+8] (:1225, :1282).  Layout observed on disc: header | model
    streams | vertex pool (8 B) | normal pool (8 B) to the end of the block."""
    probs = probs if probs is not None else Problems()
    cnt, tbl, nrm = struct.unpack_from("<Iii", d, o)
    size = end - o
    ok = probs.check(0 < cnt < 0x400 and 12 + 4 * cnt <= size, "bank@%X count %d" % (o, cnt))
    if not ok:
        return _blk("model_bank", o, size, slot=slot)
    models = list(struct.unpack_from("<%di" % cnt, d, o + 12))
    hdr = 12 + 4 * cnt
    probs.check(models[0] == hdr, "bank@%X first model %X != header end %X" % (o, models[0], hdr))
    probs.check(models == sorted(models), "bank@%X model offsets not ascending" % o)
    if not probs.check(hdr <= tbl < nrm < size, "bank@%X vertices %X normals %X size %X" % (o, tbl, nrm, size)):
        return _blk("model_bank", o, size, slot=slot)
    probs.check((nrm - tbl) % 8 == 0 and (size - nrm) % 8 == 0, "bank@%X pools not 8-byte multiples" % o)
    nverts, nnorms = (nrm - tbl) // 8, (size - nrm) // 8
    faces = Counter()
    used = set()
    model_list = []
    for k, m in enumerate(models):
        stop = models[k + 1] if k + 1 < cnt else tbl
        if not probs.check(hdr <= m < tbl, "bank@%X model %d offset %X out of range" % (o, k, m)):
            continue
        e, f, fl = walk_stream(d, o + m, o + tbl, BANK_LAYOUT, nverts, probs, "bank@%X model %d" % (o, k),
                               nnorms, collect, used)
        probs.check(e == o + stop, "bank@%X model %d ends %X, next starts %X" % (o, k, e - o, stop))
        faces.update(f)
        if collect:
            model_list.append({"id": k, "offset": o + m, "faces": fl})
    parts = [_blk("bank_header", o, hdr), _blk("model_streams", o + hdr, tbl - hdr),
             _blk("vertex_pool", o + tbl, nrm - tbl, count=nverts),
             _blk("normal_pool", o + nrm, size - nrm, count=nnorms)]
    b = _blk("model_bank", o, size, slot=slot, models=cnt, vertices=nverts, normals=nnorms,
             faces=dict(sorted(faces.items())), unused_vertices=nverts - len(used), parts=parts)
    if collect:
        b["vertex_list"] = _vertices(d, o + tbl, nverts)
        b["normal_list"] = [struct.unpack_from("<4h", d, o + nrm + 8 * i) for i in range(nnorms)]
        b["model_list"] = model_list
    return b


def parse_course_model_table(d, o, end, probs=None, collect=False):
    """CourseModelAssetHeader {s32 count; {geometry, vertexCount, model}[count]},
    offsets relative to the table (RegisterCourseModels).  Every entry's
    stream is walked against that entry's own vertexCount; streams must tile
    header-end..geometry and geometry ranges must tile geometry..block end
    (identical ranges may be shared, partial overlaps are errors)."""
    probs = probs if probs is not None else Problems()
    size = end - o
    n = s32(d, o)
    if not probs.check(0 < n < 0x400 and 4 + 12 * n <= size, "cmt@%X count %d" % (o, n)):
        return _blk("course_model_table", o, size)
    ents = [struct.unpack_from("<3i", d, o + 4 + 12 * k) for k in range(n)]
    hdr = 4 + 12 * n
    mods = sorted(set(e[2] for e in ents))
    geos = sorted(set((e[0], e[1]) for e in ents))
    gstart = geos[0][0]
    if not probs.check(mods[0] == hdr and mods[-1] < gstart <= size,
                       "cmt@%X first model %X / header end %X / geometry %X" % (o, mods[0], hdr, gstart)):
        return _blk("course_model_table", o, size)
    faces = Counter()
    walked = {}
    stream_end = {}
    used = set()
    entry_list = []
    for k, (g, nv, m) in enumerate(ents):
        ok = probs.check(0 < nv <= COURSE_MAX_VERTS, "cmt@%X entry %d vertexCount %d" % (o, k, nv))
        ok &= probs.check(hdr <= m < gstart and gstart <= g and g + 8 * nv <= size,
                          "cmt@%X entry %d offsets out of range" % (o, k))
        if not ok:
            continue
        key = (m, nv)
        if key not in walked:
            u = set()
            e, f, fl = walk_stream(d, o + m, o + gstart, COURSE_LAYOUT, nv, probs, "cmt@%X entry %d" % (o, k),
                                   collect=collect, used=u)
            walked[key] = fl
            used.update((g, i) for i in u)
            if m in stream_end:
                probs.check(stream_end[m] == e - o, "cmt@%X stream %X ends differently per entry" % (o, m))
            else:
                stream_end[m] = e - o
                faces.update(f)
        if collect:
            entry_list.append({"id": k, "geometry": o + g, "vertex_count": nv, "model": o + m,
                               "vertices": _vertices(d, o + g, nv), "faces": walked[key]})
    for a, b in zip(mods, mods[1:] + [gstart]):
        probs.check(stream_end.get(a) == b, "cmt@%X stream %X ends %s, next %X" % (o, a, stream_end.get(a), b))
    cur = gstart
    overlaps = 0
    for g, nv in geos:
        if g < cur:
            overlaps += 1
            probs.append("cmt@%X geometry %X+%d overlaps %X" % (o, g, nv, cur))
        probs.check(g <= cur, "cmt@%X geometry gap %X..%X" % (o, cur, g))
        cur = max(cur, g + 8 * nv)
    probs.check(cur == size, "cmt@%X geometry ends %X, block %X" % (o, cur, size))
    nvert_total = (size - gstart) // 8
    parts = [_blk("table_header", o, hdr), _blk("model_streams", o + hdr, gstart - hdr),
             _blk("geometry", o + gstart, size - gstart, count=nvert_total)]
    b = _blk("course_model_table", o, size, models=n, unique_streams=len(mods), unique_geometry=len(geos),
             vertices=nvert_total, faces=dict(sorted(faces.items())),
             unused_vertices=sum(nv for _, nv in geos) - len(used), parts=parts)
    if collect:
        b["entry_list"] = entry_list
    return b


def cell_origin(sx, sy):
    """Cell centre in pool vertex units (BuildVisibleCells, visible_cells.c:185-189)."""
    return ((sx * CELL_WORLD_SIZE + CELL_WORLD_CENTRE) * VERTEX_SCALE, 0,
            (sy * CELL_WORLD_SIZE + CELL_WORLD_CENTRE) * VERTEX_SCALE)


def parse_terrain_cells(d, o, end, probs=None, collect=False):
    """InstallTerrainCellData (model_banks.c:133-155):
        +0x0000 u16 grid[32][32]  lo10 cell id (0x3FF empty), hi6 region
        +0x0800 u32 vis[32][32]   bit r = visible from camera region r
        +0x1800 {s32 cellCount; s32 vertexPool; s32 cells[count]} (relative to +0x1800)
    ctx+0x5C = cells[] relocated to pointers; ctx+0x60 = header + vertexPool.
    Each cell is a command stream (SubmitTerrainCells, terrain_submission.s:
    43-72: cursor at cell+4, re-read from ctx+0x80 after each command) of
    opcodes 0..5 with TERRAIN_LAYOUT faces, ending in 0.  The vertex pool is
    8-byte {s16 x, y, z, u16 pad}, indexed idx*8 (:116-144, :156-170)."""
    probs = probs if probs is not None else Problems()
    size = end - o
    h = o + TERRAIN_CELL_GRID_SIZE + CELL_VISIBILITY_TABLE_SIZE
    hs = end - h
    if not probs.check(hs >= 12, "cells@%X block too small (%X)" % (o, size)):
        return _blk("terrain_cells", o, size)
    cc, vo = struct.unpack_from("<ii", d, h)
    if not probs.check(0 < cc < EMPTY_CELL and 8 + 4 * cc <= hs, "cells@%X count %d" % (o, cc)):
        return _blk("terrain_cells", o, size)
    cells = list(struct.unpack_from("<%di" % cc, d, h + 8))
    hdr = 8 + 4 * cc
    probs.check(cells[0] == hdr, "cells@%X first cell %X != header end %X" % (o, cells[0], hdr))
    probs.check(all(a < b for a, b in zip(cells, cells[1:])), "cells@%X cell offsets not ascending" % o)
    if not probs.check(hdr <= cells[-1] < vo <= hs and (hs - vo) % 8 == 0,
                       "cells@%X vertex pool %X vs last cell %X / size %X" % (o, vo, cells[-1], hs)):
        return _blk("terrain_cells", o, size, cells=cc)
    npool = (hs - vo) // 8
    grid = struct.unpack_from("<1024H", d, o)
    placed = {}
    regions = set()
    for r in range(32):
        for sx in range(32):
            g = grid[r * 32 + sx]
            cid, reg = g & 0x3FF, g >> 10
            regions.add(reg)
            # IsCellVisibleFromRegion tests (1 << region) against a u32 mask.
            probs.check(reg < 32, "cells@%X grid[%d][%d] region %d >= 32" % (o, r, sx, reg))
            if cid == EMPTY_CELL:
                continue
            if not probs.check(cid < cc, "cells@%X grid[%d][%d] cell %d >= %d" % (o, r, sx, cid, cc)):
                continue
            if probs.check(cid not in placed, "cells@%X cell %d placed twice" % (o, cid)):
                placed[cid] = (sx, 31 - r)
    ops = Counter()
    used = set()
    cell_list = []
    for k in range(cc):
        start = h + cells[k]
        stop = h + (cells[k + 1] if k + 1 < cc else vo)
        e, f, fl = walk_stream(d, start, h + vo, TERRAIN_LAYOUT, npool, probs, "cells@%X cell %d" % (o, k),
                               collect=collect, used=used)
        probs.check(e == stop, "cells@%X cell %d stream ends %X, next structure at %X" % (o, k, e, stop))
        ops.update(f)
        if collect:
            pos = placed.get(k)
            cell_list.append({"id": k, "offset": start, "grid": pos,
                              "origin": cell_origin(*pos) if pos else None, "faces": fl})
    parts = [_blk("cell_grid", o, TERRAIN_CELL_GRID_SIZE),
             _blk("cell_visibility", o + TERRAIN_CELL_GRID_SIZE, CELL_VISIBILITY_TABLE_SIZE),
             _blk("cell_header", h, hdr), _blk("cell_streams", h + hdr, vo - hdr, count=cc),
             _blk("vertex_pool", h + vo, hs - vo, count=npool)]
    b = _blk("terrain_cells", o, size, cells=cc, vertices=npool, faces=dict(sorted(ops.items())),
             placed=len(placed), unplaced=cc - len(placed), regions=len(regions),
             unused_vertices=npool - len(used), parts=parts)
    if collect:
        b["grid"] = list(grid)
        b["visibility"] = list(struct.unpack_from("<1024I", d, o + TERRAIN_CELL_GRID_SIZE))
        b["vertex_list"] = _vertices(d, h + vo, npool)
        b["cell_list"] = cell_list
    return b


# ---------------------------------------------------------------- audio
def parse_vab_header(d, o, size, probs=None):
    """PsyQ VAB head: 'pBAV', version, id, fsize (head + body), ps (programs)
    at +0x12; head = 0x20 + 0x800 + 0x200 * ps + 0x200 (VAG size table)."""
    probs = probs if probs is not None else Problems()
    probs.check(d[o:o + 4] == b"pBAV", "VAB@%X magic %r" % (o, d[o:o + 4]))
    ver, vid, fsize = struct.unpack_from("<III", d, o + 4)
    ps, ts, vs = struct.unpack_from("<HHH", d, o + 0x12)
    head = 0x20 + 0x800 + 0x200 * ps + 0x200
    probs.check(head == size, "VAB@%X head size %X != block %X (ps %d)" % (o, head, size, ps))
    vag = struct.unpack_from("<256H", d, o + head - 0x200)
    body = 8 * sum(vag)
    return {"version": ver, "programs": ps, "tones": ts, "vags": vs, "fsize": fsize, "body_size": body}


def _vab_pair(d, vh_off, vh_size, vb_off, vb_size, probs):
    v = parse_vab_header(d, vh_off, vh_size, probs)
    probs.check(v["body_size"] == vb_size, "VAB@%X body %X != VAG sum %X" % (vh_off, vb_size, v["body_size"]))
    probs.check(v["fsize"] == vh_size + vb_size, "VAB@%X fsize %X != %X" % (vh_off, v["fsize"], vh_size + vb_size))
    return [_blk("vab_header", vh_off, vh_size, programs=v["programs"], vags=v["vags"]),
            _blk("vab_body", vb_off, vb_size)]


# ---------------------------------------------------------------- packs
def scene_header(d, n, probs):
    """GameSceneAssetHeader: n s32 offsets from the pack start, ascending."""
    offs = list(struct.unpack_from("<%di" % n, d, 0))
    probs.check(offs[0] == 4 * n, "header: first offset %X != %X" % (offs[0], 4 * n))
    probs.check(all(a < b for a, b in zip(offs, offs[1:])), "header: offsets not ascending")
    probs.check(offs[-1] < len(d), "header: last offset past end")
    ends = offs[1:] + [len(d)]
    return offs, ends


def _image_block(kind, d, o, e, probs, **extra):
    c = parse_image_chain(d, o, e, probs)
    return _blk(kind, o, e - o, tag=c["tag"], tims=c["tims"], **extra)


def _blocks_tms(d, probs, collect):
    return [_image_block("image_chain", d, 0, len(d), probs)]


def _blocks_track_1st(d, probs, collect):
    offs, ends = scene_header(d, 5, probs)
    out = [_blk("pack_header", 0, offs[0])]
    roles = ["images (uploaded first)", "images", "single TIM (UploadImageBlock)",
             "texture page set A: uploaded, then the track-texture rect is snapshotted",
             "texture page set B: uploaded over A; rows swap with the snapshot by section"]
    for k in range(5):
        if k == 2:
            t = parse_tim(d, offs[k], ends[k], probs)
            probs.check(t["size"] == ends[k] - offs[k], "1ST block 2 TIM size %X != %X" % (t["size"], ends[k] - offs[k]))
            out.append(_blk("tim", offs[k], ends[k] - offs[k], tims=[t], role=roles[k], block=k))
        else:
            out.append(_image_block("image_chain", d, offs[k], ends[k], probs, role=roles[k], block=k))
    return out


TRACK_2ND_KINDS = ["track_render_table", "env_palette", "env_script", "model_bank", "track_points",
                   "course_model_table", "model_bank", "terrain_cells", "course_objects",
                   "track_event_data", "camera_table"]


def _blocks_track_2nd(d, probs, collect):
    offs, ends = scene_header(d, 11, probs)
    out = [_blk("pack_header", 0, offs[0])]
    for k in range(11):
        o, e = offs[k], ends[k]
        sz = e - o
        kind = TRACK_2ND_KINDS[k]
        if k == 0:
            lo, hi, env = struct.unpack_from("<3i", d, o)
            probs.check(sz == 12 + 8 * TRACK_RENDER_MODELS, "render table size %X" % sz)
            b = _blk(kind, o, sz, texture_section_lo=lo, texture_section_hi=hi, car_params=(sz - 12) // 8)
        elif k in (3, 6):
            b = parse_model_bank(d, o, e, slot=1 if k == 3 else 2, probs=probs, collect=collect)
        elif k == 4:
            n = s32(d, o)
            probs.check(4 + TRACK_POINT_SIZE * n <= sz, "track points %d overflow" % n)
            b = _blk(kind, o, sz, points=n, arc_centre_bytes=sz - 4 - TRACK_POINT_SIZE * n)
        elif k == 5:
            b = parse_course_model_table(d, o, e, probs, collect=collect)
        elif k == 7:
            b = parse_terrain_cells(d, o, e, probs, collect=collect)
        elif k == 8:
            n = u32(d, o)
            probs.check(4 + COURSE_OBJECT_SIZE * n == sz, "course objects %d x 0x14 != %X" % (n, sz))
            b = _blk(kind, o, sz, objects=n)
        elif k == 9:
            base = o + TRACK_EVENT_OFFSETS_AT
            names = ["route_scenery", "race_intro_camera", "path_scenery_position",
                     "path_scenery_rotation", "reserved", "flyby_scenery"]
            rel = struct.unpack_from("<6i", d, base)
            parts = []
            for nm, r in zip(names, rel):
                probs.check(0 <= TRACK_EVENT_OFFSETS_AT + r < sz, "event %s offset %X" % (nm, r))
                parts.append(_blk(nm, base + r, None))
            b = _blk(kind, o, sz, parts=parts)
        elif k == 10:
            rel = struct.unpack_from("<3i", d, o)
            probs.check(all(12 <= r < sz for r in rel), "camera table offsets %s" % (rel,))
            b = _blk(kind, o, sz, series0=rel[0], series1=rel[1], default=rel[2])
        else:
            b = _blk(kind, o, sz)
        b["block"] = k
        out.append(b)
    return out


def _blocks_car_1st(d, probs, collect):
    """CarModelAsset (asset.h): +0x18 u32 model bank size (RelocateCarModel
    copies 0x28 + it), +0x1C u32 image size, +0x20 modelData, +0x24 imageData."""
    bank_size, img_size, md, im = struct.unpack_from("<4I", d, 0x18)
    probs.check(md == 0x28 and md + bank_size == im and im + img_size == len(d),
                "car header md %X bank %X im %X img %X size %X" % (md, bank_size, im, img_size, len(d)))
    w, h = CAR_IMAGE_RECT[2], CAR_IMAGE_RECT[3]
    probs.check(img_size == 2 * w * h, "car image %X != 2*%d*%d" % (img_size, w, h))
    return [_blk("car_model_header", 0, 0x28),
            parse_model_bank(d, md, im, slot="car slot (SetCarModelSlot; bank 0 in car select)", probs=probs,
                             collect=collect),
            _blk("car_image_vram", im, len(d) - im, rect=CAR_IMAGE_RECT,
                 paint_palette=im + CAR_PAINT_PALETTE_OFFSET)]


def _blocks_car_2nd(d, probs, collect):
    offs, ends = scene_header(d, 5, probs)
    out = [_blk("pack_header", 0, offs[0]), _blk("car_spec", offs[0], ends[0] - offs[0])]
    vh, tb = _vab_pair(d, offs[1], ends[1] - offs[1], offs[3], ends[3] - offs[3], probs)
    out += [vh, _blk("audio_param_table", offs[2], ends[2] - offs[2]), tb,
            _image_block("image_chain", d, offs[4], ends[4], probs)]
    return out


def _blocks_selbgm(d, probs, collect):
    offs, ends = scene_header(d, 3, probs)
    probs.check(d[offs[1]:offs[1] + 4] == b"pQES", "SELBGM block 1 is not a SEQ")
    vh, vb = _vab_pair(d, offs[0], ends[0] - offs[0], offs[2], ends[2] - offs[2], probs)
    return [_blk("pack_header", 0, offs[0]), vh, _blk("seq", offs[1], ends[1] - offs[1]), vb]


def _blocks_select(d, probs, collect):
    """LoadCarSelectAssets: 3 offsets, ModelBankHeader at +0xC (bank 0xE)."""
    o0, o1, o2 = struct.unpack_from("<3i", d, 0)
    probs.check(0xC < o0 < o1 < o2 < len(d), "SELECT header %X %X %X" % (o0, o1, o2))
    probs.check((o1 - o0) % TEAM_LOGO_SAMPLE_SIZE == 0, "logo samples %X not x0x840" % (o1 - o0))
    return [_blk("pack_header", 0, 0xC), parse_model_bank(d, 0xC, o0, slot=0xE, probs=probs, collect=collect),
            _blk("team_logo_samples", o0, o1 - o0, count=(o1 - o0) // TEAM_LOGO_SAMPLE_SIZE),
            parse_course_model_table(d, o1, o2, probs, collect=collect),
            _image_block("image_chain", d, o2, len(d), probs)]


def _blocks_option(d, probs, collect):
    """LoadOptionScreenAssets: {s32 imageOffset; ModelBankHeader} (bank 0)."""
    io = s32(d, 0)
    probs.check(4 < io < len(d), "OPTION imageOffset %X" % io)
    return [_blk("pack_header", 0, 4), parse_model_bank(d, 4, io, slot=0, probs=probs, collect=collect),
            _image_block("image_chain", d, io, len(d), probs)]


def _blocks_voice(d, probs, collect):
    """LoadRoundAssets: {vhSize, vhOffset, vbOffset}."""
    vhs, vho, vbo = struct.unpack_from("<3i", d, 0)
    probs.check(vho == 0xC and vho + vhs == vbo, "VOICE header %X %X %X" % (vhs, vho, vbo))
    return [_blk("pack_header", 0, 0xC)] + _vab_pair(d, vho, vhs, vbo, len(d) - vbo, probs)


def _blocks_vab_vh(d, probs, collect):
    vb = load_asset(3)
    v = parse_vab_header(d, 0, len(d), probs)
    probs.check(v["fsize"] == len(d) + len(vb) and v["body_size"] == len(vb), "RG3 VH/VB sizes")
    return [_blk("vab_header", 0, len(d), programs=v["programs"], vags=v["vags"])]


def _blocks_vab_vb(d, probs, collect):
    return [_blk("vab_body", 0, len(d))]


def _blocks_res(d, probs, collect):
    probs.check(len(d) == 80, "RES.DAT size %d" % len(d))
    return [_blk("resource_data", 0, len(d))]


_BUILDERS = {
    "tms": _blocks_tms, "track_1st": _blocks_track_1st, "track_2nd": _blocks_track_2nd,
    "car_1st": _blocks_car_1st, "car_2nd": _blocks_car_2nd, "selbgm": _blocks_selbgm,
    "select_bin": _blocks_select, "option_bin": _blocks_option, "voice_bin": _blocks_voice,
    "vab_vh": _blocks_vab_vh, "vab_vb": _blocks_vab_vb, "res_dat": _blocks_res,
}

MODEL_KINDS = ("model_bank", "course_model_table", "terrain_cells")
IMAGE_KINDS = ("image_chain", "tim", "car_image_vram", "team_logo_samples")
AUDIO_KINDS = ("vab_header", "vab_body", "seq", "audio_param_table")


def asset_inventory(index, data=None, collect=False):
    """Classify asset `index` and split it into typed blocks.  Never raises on
    bad data; problems are listed in result['errors'].  collect=True adds the
    full vertex / face lists to model-bearing blocks."""
    d = load_asset(index) if data is None else data
    path, name, fmt = asset_name(index)
    probs = Problems()
    try:
        blocks = _BUILDERS[fmt](d, probs, collect)
    except (struct.error, ValueError, IndexError) as exc:
        blocks = []
        probs.append("parse failure: %s" % exc)
    # top-level blocks must tile the file exactly
    cur = 0
    for b in blocks:
        probs.check(b["offset"] == cur, "block %s starts %X, expected %X" % (b["kind"], b["offset"], cur))
        cur = b["offset"] + b["size"]
    if blocks:
        probs.check(cur == len(d), "blocks end %X, file %X" % (cur, len(d)))
    return {"index": index, "path": path, "name": name, "format": fmt, "size": len(d),
            "blocks": blocks, "errors": list(probs)}


def inventory_all():
    return [asset_inventory(i) for i in range(ASSET_COUNT)]


def asset_models(index, data=None):
    """Model-bearing blocks of one asset with full vertex and face lists.
    Raises FormatError if any check fails anywhere in the asset."""
    inv = asset_inventory(index, data, collect=True)
    if inv["errors"]:
        raise FormatError("asset_%03d: %d errors, first: %s" % (index, len(inv["errors"]), inv["errors"][0]))
    return [b for b in inv["blocks"] if b["kind"] in MODEL_KINDS]


def track_models(index, data=None):
    """{"bank1", "course_models", "bank2", "terrain"} of a track .2ND pack."""
    if asset_name(index)[2] != "track_2nd":
        raise FormatError("asset_%03d is not a track .2ND pack" % index)
    by_block = {b["block"]: b for b in asset_models(index, data)}
    return {"bank1": by_block[3], "course_models": by_block[5], "bank2": by_block[6], "terrain": by_block[7]}


# ---------------------------------------------------------------- OBJ
def write_obj(index, path, what=("terrain", "course_models", "banks"), data=None):
    """Wavefront OBJ of one asset's models: one 'o' per terrain-cell placement,
    course-model entry and bank model.  Raw PS1 vertex units; written as
    Blender X = x, Y = z, Z = -y (import with Forward Y / Up Z); quads as
    f v0 v1 v3 v2; repeated corners collapse to triangles.  Terrain cells are
    placed at their grid centre (cell_origin), everything else at its local
    origin.  Returns counts."""
    blocks = asset_models(index, data)
    out = ["# Rage Racer PAL asset_%03d %s" % (index, asset_name(index)[1]),
           "# raw PS1 vertex units; axes Blender X=x Y=z Z=-y (import Forward Y, Up Z); quads f v0 v1 v3 v2",
           "# terrain cells at ((sx*2048+1024)*4, 0, (sy*2048+1024)*4); other models at local origin",
           "# vt = (u/256, 1-v/256) within the face's tpage; usemtl tpTTTT_clCCCC or untextured"]
    st = {"objects": 0, "vertices": 0, "faces": 0, "degenerate": 0}
    vbase = [1]
    tbase = [1]

    def emit(name, verts, faces, origin=(0, 0, 0)):
        local, order = {}, []
        for f in faces:
            for i in f["idx"]:
                if i not in local:
                    local[i] = vbase[0] + len(order)
                    order.append(i)
        if not order:
            return
        out.append("o " + name)
        ox, oy, oz = origin
        for i in order:
            x, y, z = verts[i][:3]
            out.append("v %d %d %d" % (x + ox, z + oz, -(y + oy)))
        cur = None
        for f in faces:
            ring = [(f["idx"][k], k) for k in (0, 1, 3, 2)]
            poly = []
            for vi, k in ring:
                if not poly or poly[-1][0] != vi:
                    poly.append((vi, k))
            if len(poly) > 1 and poly[0][0] == poly[-1][0]:
                poly.pop()
            if len(poly) < 3 or len(set(p[0] for p in poly)) != len(poly):
                st["degenerate"] += 1
                continue
            mtl = "tp%04X_cl%04X" % (f["tpage"], f["clut"]) if f["uv"] else "untextured"
            if mtl != cur:
                out.append("usemtl " + mtl)
                cur = mtl
            if f["uv"]:
                refs = []
                for vi, k in poly:
                    u, v = f["uv"][k]
                    out.append("vt %.6f %.6f" % (u / 256.0, 1.0 - v / 256.0))
                    refs.append("%d/%d" % (local[vi], tbase[0]))
                    tbase[0] += 1
                out.append("f " + " ".join(refs))
            else:
                out.append("f " + " ".join(str(local[vi]) for vi, _ in poly))
            st["faces"] += 1
        vbase[0] += len(order)
        st["vertices"] += len(order)
        st["objects"] += 1

    for b in blocks:
        tagb = "b%d_" % b["block"] if "block" in b else ""
        if b["kind"] == "terrain_cells" and "terrain" in what:
            for c in b["cell_list"]:
                if c["grid"]:
                    emit("%scell_%03d_x%02d_z%02d" % (tagb, c["id"], c["grid"][0], c["grid"][1]),
                         b["vertex_list"], c["faces"], c["origin"])
                else:
                    emit("%scell_%03d_unplaced" % (tagb, c["id"]), b["vertex_list"], c["faces"])
        elif b["kind"] == "course_model_table" and "course_models" in what:
            for e in b["entry_list"]:
                emit("%scmt_%03d" % (tagb, e["id"]), e["vertices"], e["faces"])
        elif b["kind"] == "model_bank" and "banks" in what:
            slot = b["slot"] if isinstance(b["slot"], int) else "car"
            for m in b["model_list"]:
                emit("%sbank%s_model_%03d" % (tagb, slot, m["id"]), b["vertex_list"], m["faces"])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(out) + "\n")
    return st


# ---------------------------------------------------------------- identity
def track_block_identity(indices=range(87, 135)):
    """SHA1 of every scene-header block of the track packs:
    {index: [sha1 per block]} plus {(half, block, sha1): [indices]}."""
    per = {}
    groups = defaultdict(list)
    for i in indices:
        d = load_asset(i)
        n = 5 if asset_name(i)[2] == "track_1st" else 11
        offs, ends = scene_header(d, n, Problems())
        per[i] = [hashlib.sha1(d[a:b]).hexdigest() for a, b in zip(offs, ends)]
        for k, h in enumerate(per[i]):
            groups[(n, k, h)].append(i)
    return per, groups


def _identity_line(i, per, groups):
    n = len(per[i])
    bits = []
    for k, h in enumerate(per[i]):
        peers = [j for j in groups[(n, k, h)] if j != i]
        bits.append("b%d=%s" % (k, ",".join(map(str, peers)) if peers else "-"))
    return "asset_%03d %s: %s" % (i, (asset_name(i)[0] or "").split(chr(92))[-1], " ".join(bits))


# ---------------------------------------------------------------- disc
def find_track01(path=None):
    cands = [Path(path)] if path else DISC_CANDIDATES
    for c in cands:
        if c.is_file():
            return c
        if c.is_dir():
            hits = sorted(c.glob("*Track 01*.bin")) or sorted(c.glob("*.bin"))
            if hits:
                return hits[0]
    return None


def _sector_reader(track01):
    f = Path(track01).open("rb")

    def read(lba, n=1):
        out = bytearray()
        for k in range(n):
            f.seek((lba + k) * 2352)
            out += f.read(2352)[24:24 + 2048]   # MODE2/2352, form 1 user data
        return bytes(out)
    return f, read


def list_disc_files(track01):
    """ISO9660 walk of the data track: [{path, lba, size, dir, xa}]."""
    f, read = _sector_reader(track01)
    out = []
    try:
        pvd = read(16)
        if pvd[1:6] != b"CD001":
            raise ValueError("no ISO9660 PVD at LBA 16")

        def walk(lba, size, prefix):
            data = read(lba, (size + 2047) // 2048)
            p = 0
            while p < size:
                ln = data[p]
                if ln == 0:
                    p = (p // 2048 + 1) * 2048
                    continue
                ext, dl = u32(data, p + 2), u32(data, p + 10)
                flags, nl = data[p + 25], data[p + 32]
                nm = data[p + 33:p + 33 + nl]
                so = p + 33 + nl + ((nl + 1) & 1)
                xa = struct.unpack_from(">H", data, so + 4)[0] if p + ln - so >= 14 else 0
                if nm not in (b"\0", b"\1"):
                    full = prefix + "/" + nm.decode("latin-1")
                    out.append({"path": full, "lba": ext, "size": dl, "dir": bool(flags & 2), "xa": xa})
                    if flags & 2:
                        walk(ext, dl, full)
                p += ln
        root = pvd[156:190]
        walk(u32(root, 2), u32(root, 10), "")
        return {"volume": pvd[40:72].decode("latin-1").strip(), "blocks": u32(pvd, 80), "files": out}
    finally:
        f.close()


def check_rage_bin(track01, files=None):
    """Read the RAGE.BIN index (135 x {sector offset, size}) and compare every
    asset with extracted/assets_raw; also report gaps, slack bytes and raw
    files that are missing (a missing file is a failure, not a skip)."""
    files = files or list_disc_files(track01)["files"]
    rb = next(x for x in files if x["path"].upper().startswith("/RAGE.BIN"))
    f, read = _sector_reader(track01)
    try:
        nsec = (rb["size"] + 2047) // 2048
        blob = read(rb["lba"], nsec)
    finally:
        f.close()
    ents = [struct.unpack_from("<II", blob, 8 * i) for i in range(ASSET_COUNT)]
    res = {"lba": rb["lba"], "sectors": nsec, "mismatch": [], "missing": [], "gaps": [], "slack_nonzero": [],
           "index_tail_nonzero": any(blob[8 * ASSET_COUNT:2048])}
    cur = 1
    for i in sorted(range(ASSET_COUNT), key=lambda i: ents[i][0]):
        so, sz = ents[i]
        if so != cur:
            res["gaps"].append((cur, so))
        cur = so + (sz + 2047) // 2048
        chunk = blob[so * 2048:so * 2048 + sz]
        if not raw_path(i).exists():
            res["missing"].append(i)
        elif chunk != load_asset(i):
            res["mismatch"].append(i)
        if any(blob[so * 2048 + sz:cur * 2048]):
            res["slack_nonzero"].append(i)
    res["end_sector"] = cur
    return res


# ---------------------------------------------------------------- self-test
def self_test():
    """Corrupt asset_088 in ways a shallow parser would miss and confirm that
    each one is reported.  Returns [(case, error count)]."""
    base = load_asset(88)
    offs = struct.unpack_from("<11i", base, 0)
    h = offs[7] + TERRAIN_CELL_GRID_SIZE + CELL_VISIBILITY_TABLE_SIZE
    cells = struct.unpack_from("<2i", base, h + 8)
    c0, c1 = h + cells[0], h + cells[1]
    bank = offs[3]
    m0 = bank + s32(base, bank + 12)
    cmt = offs[5]
    g0 = s32(base, cmt + 4)
    # first terrain face with a texture-window word (op 1/4/5)
    tw = None
    inv = asset_inventory(88, collect=True)
    for c in next(b for b in inv["blocks"] if b["kind"] == "terrain_cells")["cell_list"]:
        tw = next((f["offset"] for f in c["faces"] if "texwin" in f), None)
        if tw:
            break
    cases = [
        ("terrain opcode 9 in cell 0", lambda x: struct.pack_into("<H", x, c0, 9)),
        ("terrain vertex index 0xFFFF in first face", lambda x: struct.pack_into("<H", x, c0 + 4, 0xFFFF)),
        ("terrain face count 0x7FFF in cell 1", lambda x: struct.pack_into("<H", x, c1 + 2, 0x7FFF)),
        ("terrain face count 0 in cell 1", lambda x: struct.pack_into("<H", x, c1 + 2, 0)),
        ("terrain RGB code byte 0x00 in first face", lambda x: struct.pack_into("<B", x, c0 + 4 + 0x1F, 0)),
        ("terrain texture-window word without E2", lambda x: struct.pack_into("<I", x, tw + 0x20, 0)),
        ("bank1 model 0 command op 3 with face count 0", lambda x: struct.pack_into("<I", x, m0, 3)),
        ("bank1 model 0 vertex index past pool", lambda x: struct.pack_into("<H", x, m0 + 4, 0xFFFE)),
        ("course model entry 1 geometry overlaps entry 0", lambda x: struct.pack_into("<i", x, cmt + 16, g0 + 8)),
    ]
    res = []
    for name, mut in cases:
        x = bytearray(base)
        mut(x)
        res.append((name, len(asset_inventory(88, data=bytes(x))["errors"])))
    return res


# ---------------------------------------------------------------- CLI
def _fmt_block(b, indent="  "):
    extra = []
    for k in ("block", "slot", "models", "unique_streams", "unique_geometry", "vertices", "normals", "cells",
              "placed", "unplaced", "regions", "unused_vertices", "objects", "points", "count", "programs",
              "vags", "texture_section_lo", "texture_section_hi"):
        if k in b and b[k] is not None:
            extra.append("%s=%s" % (k, b[k]))
    if "faces" in b:
        extra.append("faces=%d(%s)" % (sum(b["faces"].values()), "/".join("op%d:%d" % kv for kv in b["faces"].items())))
    if "tims" in b:
        extra.append(tim_summary(b["tims"]))
    if "rect" in b:
        extra.append("vram rect=%s" % (b["rect"],))
    size = "%8X" % b["size"] if b["size"] is not None else "       ?"
    line = "%s%-20s @%06X %s  %s" % (indent, b["kind"], b["offset"], size, " ".join(extra))
    lines = [line.rstrip()]
    for p in b.get("parts", []):
        lines += _fmt_block(p, indent + "    ")
    return lines


def _resolve_arg(a):
    a = a.lower().replace("asset_", "").replace(".bin", "")
    return int(a, 0) if a.startswith("0x") else int(a)


def _print_one(inv):
    print("asset_%03d  %s  [%s]  %d bytes" % (inv["index"], inv["path"], inv["format"], inv["size"]))
    print("  " + inv["name"])
    for b in inv["blocks"]:
        print("\n".join(_fmt_block(b)))
    print("  errors: %d" % len(inv["errors"]))
    for e in inv["errors"][:20]:
        print("    " + e)


def _run_all():
    invs = inventory_all()
    t = exe_tables()
    bad = [inv for inv in invs if inv["errors"]]
    by_fmt = Counter(inv["format"] for inv in invs)
    kinds = Counter()
    nmodels = Counter()
    nfaces = Counter()
    nverts = Counter()
    unused = Counter()
    ntims = 0
    track_face_ops = Counter()
    for inv in invs:
        for b in inv["blocks"]:
            kinds[b["kind"]] += 1
            if b["kind"] in MODEL_KINDS:
                nmodels[b["kind"]] += b.get("models", b.get("cells", 0)) or 0
                nfaces[b["kind"]] += sum(b.get("faces", {}).values())
                nverts[b["kind"]] += b.get("vertices", 0) or 0
                unused[b["kind"]] += b.get("unused_vertices", 0) or 0
                if b["kind"] == "terrain_cells":
                    track_face_ops.update(b.get("faces", {}))
            ntims += len(b.get("tims", []))
    # name / path consistency
    name_errs = []
    if t["asset_paths"]:
        for i in range(10, 74):
            k, m, g, half = car_of_asset(i)
            if not t["asset_paths"][i].endswith("CAR_%X%d.%s" % (m, g, half)):
                name_errs.append(i)
        for i in range(87, 135):
            c, cl, half = track_of_asset(i)
            if not t["asset_paths"][i].endswith("%s%d.%s" % (COURSE_KEYS[c], cl + 1, half)):
                name_errs.append(i)
    print("names from: %s (exe sha1 %s)" % (t["source"], t.get("exe_sha1", "-")))
    print("formats: " + ", ".join("%s=%d" % kv for kv in sorted(by_fmt.items())))
    print("block kinds: " + ", ".join("%s=%d" % kv for kv in sorted(kinds.items())))
    for k in MODEL_KINDS:
        print("walked %-18s units=%d faces=%d vertices=%d unreferenced vertices=%d" % (
            k, nmodels[k], nfaces[k], nverts[k], unused[k]))
    print("terrain faces by opcode: " + " ".join("op%d:%d" % kv for kv in sorted(track_face_ops.items())))
    print("TIMs in image blocks: %d" % ntims)
    tags = Counter(b["tag"] for inv in invs for b in inv["blocks"] if b["kind"] == "image_chain")
    print("image-chain tags: %s" % dict(tags))
    print("path/name cross-check failures: %s" % (name_errs or "none"))
    per, groups = track_block_identity()
    for i in (93, 94, 102):
        print("identity " + _identity_line(i, per, groups))
    st = self_test()
    st_fail = [n for n, e in st if e == 0]
    print("mutation self-test: %d/%d corruptions detected%s" % (
        len(st) - len(st_fail), len(st), "" if not st_fail else " (MISSED: %s)" % "; ".join(st_fail)))
    print("assets with errors: %d / %d" % (len(bad), len(invs)))
    for inv in bad[:10]:
        print("  asset_%03d: %s" % (inv["index"], "; ".join(inv["errors"][:3])))
    ok = not bad and not name_errs and len(invs) == ASSET_COUNT and not st_fail
    print("ALL CHECKS PASSED" if ok else "CHECKS FAILED")
    return 0 if ok else 1


def _run_disc(path):
    tr = find_track01(path)
    if tr is None:
        print("no disc image found (looked in %s)" % ", ".join(str(c) for c in DISC_CANDIDATES))
        return 1
    print("disc: %s" % tr)
    iso = list_disc_files(tr)
    print("volume %s, %d blocks" % (iso["volume"], iso["blocks"]))
    for x in iso["files"]:
        if x["path"].startswith("/CDDA/"):
            continue
        print("  %-16s lba %6d size %10d%s" % (x["path"], x["lba"], x["size"], " <dir>" if x["dir"] else ""))
    da = [x for x in iso["files"] if x["path"].startswith("/CDDA/")]
    print("  /CDDA/*.DA        %d CD-DA entries, lba %d..%d (audio tracks)" % (
        len(da), min(x["lba"] for x in da), max(x["lba"] for x in da)))
    r = check_rage_bin(tr, iso["files"])
    print("RAGE.BIN lba %d, %d sectors, index end sector %d, gaps %s, slack %s, index tail %s, "
          "mismatches %s, missing raw files %s" % (
              r["lba"], r["sectors"], r["end_sector"], r["gaps"] or "none", r["slack_nonzero"] or "none",
              "nonzero" if r["index_tail_nonzero"] else "zero", r["mismatch"] or "none", r["missing"] or "none"))
    t = exe_tables()
    st = next((x for x in iso["files"] if x["path"].upper().startswith("/RAGE.STR")), None)
    if st and t["source"] == "exe":
        nsec = st["size"] // 2048
        print("RAGE.STR lba %d, %d sectors; g_StreamCdEntries (sector, count): %s" % (
            st["lba"], nsec, t["stream_entries"]))
    exe = next((x for x in iso["files"] if x["path"].upper().startswith("/SCES")), None)
    if exe:
        f, read = _sector_reader(tr)
        try:
            blob = read(exe["lba"], (exe["size"] + 2047) // 2048)[:exe["size"]]
        finally:
            f.close()
        print("EXE on disc sha1 %s (repo main.exe %s)" % (hashlib.sha1(blob).hexdigest(), t.get("exe_sha1")))
    ok = (not r["mismatch"] and not r["missing"] and not r["gaps"] and not r["slack_nonzero"]
          and r["end_sector"] == r["sectors"])
    print("DISC CHECKS PASSED" if ok else "DISC CHECKS FAILED")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("asset", nargs="?", default="88", help="asset index or asset_NNN (default 88)")
    ap.add_argument("--all", action="store_true", help="validate all 135 assets")
    ap.add_argument("--list", action="store_true", help="one line per asset")
    ap.add_argument("--disc", nargs="?", const="", default=None, help="list the disc and check RAGE.BIN")
    ap.add_argument("--obj", default=None, help="write the asset's models as Wavefront OBJ")
    ap.add_argument("--what", default="terrain,course_models,banks",
                    help="comma list for --obj: terrain, course_models, banks")
    a = ap.parse_args(argv)
    if a.disc is not None:
        return _run_disc(a.disc or None)
    if a.list:
        for inv in inventory_all():
            kinds = Counter(b["kind"] for b in inv["blocks"])
            print("%3d %-22s %-10s %8d  %s | %s%s" % (
                inv["index"], (inv["path"] or "").split(chr(92))[-1], inv["format"], inv["size"], inv["name"],
                " ".join("%s%s" % (k, "" if v == 1 else "x%d" % v) for k, v in kinds.items()),
                "  ERR" if inv["errors"] else ""))
        return 0
    if a.all:
        return _run_all()
    idx = _resolve_arg(a.asset)
    inv = asset_inventory(idx)
    _print_one(inv)
    if a.obj and not inv["errors"]:
        st = write_obj(idx, a.obj, tuple(x.strip() for x in a.what.split(",")))
        print("  obj: %s  objects=%d vertices=%d faces=%d degenerate-skipped=%d" % (
            a.obj, st["objects"], st["vertices"], st["faces"], st["degenerate"]))
    return 1 if inv["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
