#!/usr/bin/env python3
"""rr_placement.py - world placement of the pieces of a Rage Racer (PAL) course.

Reads one .2ND course pack (extracted/assets_raw/asset_NNN.bin) and works out
where every drawable piece sits in the world, using only what the game's own
draw code does (see the notes file and the evidence strings on each instance):

  * terrain cells (block 7): one instance per non-empty cell of the 32x32 grid,
    translation (sx*8192 + 4096, 0, sy*8192 + 4096) vertex units, no rotation
    (BuildVisibleCells + SubmitTerrainCells).  sx = grid index % 32,
    sy = 31 - grid index // 32 (the grid rows are stored north-up/flipped).
  * course objects (block 8): s32 count + count * 20-byte records
    {s16 modelId, s16 rotationY, s32 x, s32 y, s32 z, s32 flags}; course model
    `modelId` drawn at 4*(x, y, z) rotated by RotY(rotationY) (DrawCourseObjects).
  * exe-resident props (positions in the PAL executable's .data, not in the
    pack): static landmark, high-class landmark, animated screen, spinners,
    start-grid figure, shuttles (course models), plus the scripted props whose
    start poses live in block 9 (route / path props in model bank 1, flyby prop
    in model bank 2).
  * course models (block 5) and bank models (blocks 3, 6) are object-local:
    they carry no transform of their own.

Units: every model vertex (terrain, course model, bank model) is in GTE units,
which are 4x the world units used by track points, course objects and car
positions (every caller shifts the camera-relative translation left by 2).
Instances are reported as  world_vertex = translation + matrix @ vertex  in
those vertex units, PS1 axes (y down).  OBJ export maps Blender X=x, Y=z, Z=-y.

Usage:
  python -I tools/rr_placement.py [asset_088|88|path]      summary + instance table
  python -I tools/rr_placement.py 88 --obj out.obj         also write the assembled scene
  python -I tools/rr_placement.py --all                    assertions over all 48 packs
Options: --class N (override Grand Prix class), --series 0|1, --env4,
         --all-conditions (list instances of every class/course condition).

Importable API (no side effects on import):
  load_pack(pack) -> (asset_id, bytes)
  parse_pack(pack) -> dict with every parsed block (faces include tpage/clut/uv
                      for course models; raw words for terrain/bank faces)
  get_instances(pack, ...) -> list of instance dicts
      {source, model_index, translation, position_world, rotation, matrix,
       kind, drawer, data_source, condition, evidence, ...}
  instance_world_vertices(scene, inst) -> list of (x, y, z) world vertex units
  write_obj(pack, path, ...)
"""

import math
import os
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "extracted" / "assets_raw"
EXE_PATH = REPO / "assets" / "PAL" / "main.exe"

FIRST_PACK = 87          # g_AssetPaths[0x57] = \PACK\BIG1.1ST
LAST_PACK = 134
GTE_SCALE = 4            # vertex units per world unit (the "<< 2" / "* 4" in every caller)
CELL_WORLD = 2048        # world units per terrain cell (BuildVisibleCells: x / 2048)
CELL_PITCH = CELL_WORLD * GTE_SCALE   # 8192 vertex units
CELL_HALF = 1024 * GTE_SCALE          # the "center = 1024" of BuildVisibleCells
GRID_N = 32
EMPTY_CELL = 0x3FF
GRID_BYTES = 0x800       # u16[32][32]
VIS_BYTES = 0x1000       # u32[32][32]
EVENT_OFFSETS_AT = 0xB64  # TrackEventData.offsets (track.h, sizeof 0x1CF4)
EVENT_DATA_SIZE = 0x1CF4
COURSE_MODEL_VERTEX_LIMIT = 120       # ctx+0x11C .. ctx+0x2FC holds 120 SXY words

COURSE_NAMES = ["BIG (Mythical Coast)", "MID (Over Pass City)",
                "HI (Lakeside Gate)", "OVAL (The Extreme Oval)"]

# Course-model face strides (SubmitCourseModel handlers, confirmed by parent).
COURSE_FACE_STRIDE = {0: 0x10, 1: 0x1C, 2: 0x20, 3: 0x20}
# Terrain face strides: SubmitTerrainCellFaces maps opcode op to jtbl_8007D9F4
# pair op-2 (op >= 2) or 2*op + ctx[0x84] (op < 2); pairs 0,1 stride 0x20, 2,3 0x24.
TERRAIN_FACE_STRIDE = {0: 0x20, 1: 0x24, 2: 0x20, 3: 0x20, 4: 0x24, 5: 0x24}
# Bank-model face strides: SubmitModel passes opcode+4 into jtbl_8007DA14
# (EmitPolyF4/FT4Raw/G4/GT4 = 0x10/0x18/0x18/0x20).
BANK_FACE_STRIDE = {0: 0x10, 1: 0x18, 2: 0x18, 3: 0x20}


class FormatError(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise FormatError(msg)


def _u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def _s16(d, o):
    return struct.unpack_from("<h", d, o)[0]


def _u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def _s32(d, o):
    return struct.unpack_from("<i", d, o)[0]


# ---------------------------------------------------------------- pack access

def asset_path(pack):
    if isinstance(pack, int):
        return RAW_DIR / ("asset_%03d.bin" % pack)
    s = str(pack)
    if s.isdigit():
        return RAW_DIR / ("asset_%03d.bin" % int(s))
    p = Path(s)
    if p.exists():
        return p
    if not s.endswith(".bin"):
        s += ".bin"
    return RAW_DIR / s


def _asset_id_from_path(p):
    stem = Path(p).stem
    digits = "".join(ch for ch in stem if ch.isdigit())
    return int(digits) if digits else None


def load_pack(pack):
    p = asset_path(pack)
    return _asset_id_from_path(p), p.read_bytes()


def pack_meta(asset_id):
    """Course / class / half from g_AssetPaths order: 0x57 + course*2 + class*8 (+1 for .2ND)."""
    if asset_id is None or not FIRST_PACK <= asset_id <= LAST_PACK:
        return {"asset_id": asset_id, "course": None, "class": None, "half": None, "name": "?"}
    rel = asset_id - FIRST_PACK
    course = (rel % 8) // 2
    gp_class = rel // 8
    half = "1ST" if rel % 2 == 0 else "2ND"
    return {"asset_id": asset_id, "course": course, "class": gp_class, "half": half,
            "name": "%s class %d .%s" % (COURSE_NAMES[course], gp_class, half)}


def read_header(data):
    offs = list(struct.unpack_from("<11i", data, 0))
    ends = offs[1:] + [len(data)]
    return offs, ends


def is_scene_pack(data):
    if len(data) < 0x2C:
        return False
    offs, _ = read_header(data)
    if offs[0] != 0x2C:
        return False
    return all(offs[i] < offs[i + 1] for i in range(10)) and offs[10] <= len(data)


# ---------------------------------------------------------------- block parsers

def parse_track_points(data, b, e):
    """Block 4: s32 count, count * GameTrackPoint (0x18), then 12-byte arc centres."""
    count = _s32(data, b)
    check(count > 0, "track point count %d" % count)
    pts_end = b + 4 + 0x18 * count
    check(pts_end <= e, "track points overrun block 4")
    rem = e - pts_end
    check(rem % 12 == 0, "arc-centre tail %d not a multiple of 12" % rem)
    pts = []
    for i in range(count):
        x, z, y, angle = struct.unpack_from("<iihh", data, b + 4 + 0x18 * i)
        pts.append({"x": x, "y": y, "z": z, "angle": angle})
    arcs = [struct.unpack_from("<ii", data, pts_end + 12 * i) for i in range(rem // 12)]
    return {"count": count, "points": pts, "arc_centers": arcs}


def _course_face(data, p, opcode):
    w0, w1 = struct.unpack_from("<II", data, p)
    stride = COURSE_FACE_STRIDE[opcode]
    face = {"opcode": opcode,
            "idx": (w0 & 0xFFFF, w0 >> 16, w1 & 0xFFFF, w1 >> 16),
            "rgbc": _u32(data, p + 8),
            "raw": bytes(data[p:p + stride])}
    if opcode in (1, 2, 3):
        a = _u32(data, p + 0xC)
        c = _u32(data, p + 0x10)
        face["uv"] = ((a & 0xFF, (a >> 8) & 0xFF), (c & 0xFF, (c >> 8) & 0xFF),
                      (data[p + 0x14], data[p + 0x15]), (data[p + 0x16], data[p + 0x17]))
        face["clut"] = a >> 16
        face["tpage"] = c >> 16
        face["ot_bias"] = struct.unpack_from("<b", data, p + 0x19)[0]
    else:
        face["uv"] = None
        face["clut"] = None
        face["tpage"] = None
        face["ot_bias"] = None
    return face


def parse_course_models(data, b, e):
    """Block 5: s32 count + count * {geometry off, vertexCount, model off} (RegisterCourseModels)."""
    count = _s32(data, b)
    check(0 < count < 1024, "course model count %d" % count)
    table_end = 4 + 12 * count
    models = []
    for i in range(count):
        g, vc, m = struct.unpack_from("<iii", data, b + 4 + 12 * i)
        check(0 < vc, "course model %d vertexCount %d" % (i, vc))
        check(table_end <= g and g + 8 * vc <= e - b, "course model %d geometry out of block" % i)
        check(table_end <= m < e - b, "course model %d stream out of block" % i)
        verts = [struct.unpack_from("<hhhH", data, b + g + 8 * k) for k in range(vc)]
        p = b + m
        faces = []
        commands = []
        while True:
            check(p + 4 <= e, "course model %d stream runs off block" % i)
            w = _u32(data, p)
            p += 4
            if w == 0:
                break
            op, n = w & 0xFFFF, w >> 16
            check(op in COURSE_FACE_STRIDE, "course model %d opcode %d" % (i, op))
            commands.append((op, n))
            for _ in range(n):
                f = _course_face(data, p, op)
                for vi in f["idx"]:
                    check(vi < vc, "course model %d face index %d >= %d" % (i, vi, vc))
                faces.append(f)
                p += COURSE_FACE_STRIDE[op]
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        models.append({"index": i, "geometry_offset": g, "vertex_count": vc,
                       "model_offset": m, "model_end": p - b, "commands": commands,
                       "vertices": [(v[0], v[1], v[2]) for v in verts],
                       "vertex_extra": [v[3] for v in verts], "faces": faces,
                       "bbox": ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))})
    # tiling: streams back to back after the table, then geometry back to back to block end
    by_m = sorted(models, key=lambda m: m["model_offset"])
    check(by_m[0]["model_offset"] == table_end, "first course model stream not at table end")
    for a, c in zip(by_m, by_m[1:]):
        check(a["model_end"] == c["model_offset"],
              "course model %d stream ends 0x%x, next starts 0x%x" % (a["index"], a["model_end"], c["model_offset"]))
    by_g = sorted(models, key=lambda m: m["geometry_offset"])
    check(by_g[0]["geometry_offset"] == by_m[-1]["model_end"], "geometry does not follow the last stream")
    for a, c in zip(by_g, by_g[1:]):
        check(a["geometry_offset"] + 8 * a["vertex_count"] == c["geometry_offset"],
              "course model %d geometry not contiguous" % a["index"])
    last = by_g[-1]
    check(last["geometry_offset"] + 8 * last["vertex_count"] == e - b, "geometry does not end at block end")
    return models


def parse_terrain_cells(data, b, e):
    """Block 7: u16 grid[32][32], u32 vis[32][32], {s32 cellCount, faces off, cells[] offs}.

    Offsets are relative to the header (InstallTerrainCellData adds them to
    address.bytes after skipping grid+vis).  'faces' is really the shared
    8-byte vertex pool that SubmitTerrainCellFaces indexes (ctx+0x60)."""
    grid = list(struct.unpack_from("<1024H", data, b))
    vis = list(struct.unpack_from("<1024I", data, b + GRID_BYTES))
    hdr = b + GRID_BYTES + VIS_BYTES
    count, pool_off = struct.unpack_from("<ii", data, hdr)
    check(0 < count < EMPTY_CELL, "terrain cell count %d" % count)
    cell_offs = list(struct.unpack_from("<%di" % count, data, hdr + 8))
    pool = hdr + pool_off
    check(pool < e and (e - pool) % 8 == 0, "terrain vertex pool does not end on a record at block end")
    npool = (e - pool) // 8
    pool_verts = [struct.unpack_from("<hhhH", data, pool + 8 * k) for k in range(npool)]
    placement = {}
    for gi, g in enumerate(grid):
        cid = g & 0x3FF
        if cid == EMPTY_CELL:
            continue
        check(cid < count, "grid cell id %d >= cellCount %d" % (cid, count))
        check(cid not in placement, "cell %d placed twice" % cid)
        row, sx = divmod(gi, GRID_N)
        placement[cid] = (gi, sx, GRID_N - 1 - row)
    cells = []
    expect = hdr + 8 + 4 * count
    owner = [None] * npool
    for cid in range(count):
        p = hdr + cell_offs[cid]
        check(p == expect, "cell %d stream at 0x%x, expected 0x%x" % (cid, p - b, expect - b))
        faces = []
        while True:
            check(p + 4 <= pool, "cell %d stream runs into vertex pool" % cid)
            w = _u32(data, p)
            p += 4
            if w == 0:
                break
            op, n = w & 0xFFFF, w >> 16
            check(op in TERRAIN_FACE_STRIDE, "cell %d opcode %d" % (cid, op))
            st = TERRAIN_FACE_STRIDE[op]
            for _ in range(n):
                w0, w1 = struct.unpack_from("<II", data, p)
                idx = (w0 & 0xFFFF, w0 >> 16, w1 & 0xFFFF, w1 >> 16)
                for vi in idx:
                    check(vi < npool, "cell %d vertex index %d >= pool %d" % (cid, vi, npool))
                    if owner[vi] is None:
                        owner[vi] = cid
                    elif owner[vi] != cid:
                        owner[vi] = -1
                faces.append({"opcode": op, "idx": idx, "raw": bytes(data[p:p + st])})
                p += st
        expect = p
        pl = placement.get(cid)
        cells.append({"id": cid, "faces": faces, "grid_index": pl[0] if pl else None,
                      "sx": pl[1] if pl else None, "sy": pl[2] if pl else None,
                      "translation": ((pl[1] * CELL_PITCH + CELL_HALF, 0, pl[2] * CELL_PITCH + CELL_HALF)
                                      if pl else None)})
    check(expect == pool, "last cell stream ends 0x%x, vertex pool at 0x%x" % (expect - b, pool - b))
    return {"grid": grid, "visibility": vis, "cell_count": count, "pool_offset": pool - b,
            "pool": [(v[0], v[1], v[2]) for v in pool_verts], "pool_extra": [v[3] for v in pool_verts],
            "cells": cells, "placed": len(placement),
            "shared_vertices": sum(1 for o in owner if o == -1),
            "unreferenced_vertices": sum(1 for o in owner if o is None)}


def parse_course_objects(data, b, e, course_model_count):
    """Block 8: u32 count + count * CourseObject (0x14) (SetCourseObjects, track_internal.h)."""
    count = _u32(data, b)
    check(e - b == 4 + 20 * count, "block 8 size %d != 4 + 20*%d" % (e - b, count))
    objs = []
    for i in range(count):
        mid, rot, x, y, z, fl = struct.unpack_from("<hhiiii", data, b + 4 + 20 * i)
        check(mid == -1 or 0 <= mid < course_model_count,
              "course object %d model %d out of range" % (i, mid))
        objs.append({"index": i, "model_id": mid, "rotation_y": rot, "x": x, "y": y, "z": z, "flags": fl})
    return objs


def parse_model_bank(data, b, e):
    """Blocks 3/6: ModelBankHeader {u32 count, table off, normals off, models[count] offs}."""
    count, table, normals = struct.unpack_from("<Iii", data, b)
    check(0 < count < 256, "bank model count %d" % count)
    hdr_end = 12 + 4 * count
    offs = list(struct.unpack_from("<%di" % count, data, b + 12))
    check(hdr_end <= table < normals <= e - b, "bank table/normals offsets out of order")
    check((normals - table) % 8 == 0 and (e - b - normals) % 8 == 0, "bank pools not 8-byte records")
    npool = (normals - table) // 8
    verts = [struct.unpack_from("<hhh", data, b + table + 8 * k) for k in range(npool)]
    models = []
    for i, m in enumerate(offs):
        check(hdr_end <= m < table, "bank model %d offset 0x%x outside stream area" % (i, m))
        p = b + m
        faces = []
        while True:
            check(p + 4 <= b + table, "bank model %d stream runs into vertex table" % i)
            w = _u32(data, p)
            p += 4
            if w == 0:
                break
            op, n = w & 0xFFFF, w >> 16
            check(op in BANK_FACE_STRIDE, "bank model %d opcode %d" % (i, op))
            st = BANK_FACE_STRIDE[op]
            for _ in range(n):
                w0, w1 = struct.unpack_from("<II", data, p)
                idx = (w0 & 0xFFFF, w0 >> 16, w1 & 0xFFFF, w1 >> 16)
                for vi in idx:
                    check(vi < npool, "bank model %d vertex index %d >= %d" % (i, vi, npool))
                faces.append({"opcode": op, "idx": idx, "raw": bytes(data[p:p + st])})
                p += st
        models.append({"index": i, "offset": m, "end": p - b, "faces": faces})
    by = sorted(models, key=lambda m: m["offset"])
    check(by[0]["offset"] == hdr_end, "first bank model not at header end")
    for a, c in zip(by, by[1:]):
        check(a["end"] == c["offset"], "bank model %d ends 0x%x, next at 0x%x" % (a["index"], a["end"], c["offset"]))
    check(by[-1]["end"] == table, "last bank model ends 0x%x, vertex table at 0x%x" % (by[-1]["end"], table))
    for m in models:
        used = sorted({vi for f in m["faces"] for vi in f["idx"]})
        if used:
            pts = [verts[vi] for vi in used]
            m["bbox"] = (tuple(min(p[k] for p in pts) for k in range(3)),
                         tuple(max(p[k] for p in pts) for k in range(3)))
        else:
            m["bbox"] = None
    return {"count": count, "table_offset": table, "normals_offset": normals,
            "vertices": verts, "normal_count": (e - b - normals) // 8, "models": models}


def _motion_data(data, p):
    """SceneryMotionData (track.h): triggerSection[2][2], firstKeyframe[2][2], start[2] (0x20), keyframes (0xC)."""
    trig = struct.unpack_from("<4h", data, p)
    first = struct.unpack_from("<4h", data, p + 8)
    starts = [struct.unpack_from("<4i", data, p + 0x10 + 0x20 * s) for s in range(2)]
    return {"trigger_section": ((trig[0], trig[1]), (trig[2], trig[3])),
            "first_keyframe": ((first[0], first[1]), (first[2], first[3])),
            "start": [s[:3] for s in starts], "keyframes_at": p + 0x50}


def _keyframe(data, kf_base, index):
    rx, ry, rz, dur, speed, _ = struct.unpack_from("<6h", data, kf_base + 12 * index)
    return {"rotation_x": rx, "rotation_y": ry, "rotation_z": rz, "duration": dur, "speed": speed}


def parse_track_events(data, b, e):
    """Block 9 = TrackEventData (0x1CF4).  Scenery offsets are relative to &offsets (block + 0xB64)."""
    check(e - b == EVENT_DATA_SIZE, "block 9 size 0x%x != 0x1CF4" % (e - b))
    base = b + EVENT_OFFSETS_AT
    route, intro, ppos, prot, reserved, flyby = struct.unpack_from("<6i", data, base)
    for name, o in (("route", route), ("introCamera", intro), ("pathPos", ppos),
                    ("pathRot", prot), ("flyby", flyby)):
        check(0 <= base + o - b < e - b, "event offset %s 0x%x outside block 9" % (name, o))
    out = {"offsets": {"route": route, "intro_camera": intro, "path_position": ppos,
                       "path_rotation": prot, "reserved": reserved, "flyby": flyby}}
    for name, o in (("route", route), ("flyby", flyby)):
        md = _motion_data(data, base + o)
        md["first"] = [_keyframe(data, md["keyframes_at"], md["first_keyframe"][s][0]) for s in range(2)]
        check(md["keyframes_at"] + 12 * max(md["first_keyframe"][s][0] for s in range(2)) + 12 <= e,
              "%s keyframes overrun block 9" % name)
        out[name] = md
    pp = base + ppos
    pr = base + prot
    pfirst = struct.unpack_from("<2h", data, pp)
    rfirst = struct.unpack_from("<2h", data, pr)
    out["path"] = {"first_position_key": pfirst, "first_rotation_key": rfirst,
                   "position": [struct.unpack_from("<3i", data, pp + 4 + 0x14 * pfirst[s]) for s in range(2)],
                   "rotation": [struct.unpack_from("<3h", data, pr + 4 + 0xC * rfirst[s]) for s in range(2)]}
    for s in range(2):
        check(pp + 4 + 0x14 * pfirst[s] + 0x14 <= e and pr + 4 + 0xC * rfirst[s] + 0xC <= e,
              "path scenery keys overrun block 9")
    return out


def parse_pack(pack):
    """Parse every block this module needs.  `pack` is an id, name or path (or (id, bytes))."""
    if isinstance(pack, tuple):
        aid, data = pack
    else:
        aid, data = load_pack(pack)
    meta = pack_meta(aid)
    check(is_scene_pack(data), "asset %s is not a scene pack (header %r)" % (aid, read_header(data)[0][:3]))
    offs, ends = read_header(data)
    scene = {"asset_id": aid, "meta": meta, "size": len(data), "offsets": offs, "ends": ends}
    scene["track"] = parse_track_points(data, offs[4], ends[4])
    scene["course_models"] = parse_course_models(data, offs[5], ends[5])
    scene["terrain"] = parse_terrain_cells(data, offs[7], ends[7])
    scene["course_objects"] = parse_course_objects(data, offs[8], ends[8], len(scene["course_models"]))
    scene["bank1"] = parse_model_bank(data, offs[3], ends[3])
    scene["bank2"] = parse_model_bank(data, offs[6], ends[6])
    scene["events"] = parse_track_events(data, offs[9], ends[9])
    return scene


# ---------------------------------------------------------------- exe constants

# PAL main.exe .data (asm/PAL/main/data/main/6BE64.data.s, file offset = addr - 0x80010000 + 0x800).
EXE_TABLES = {
    # name: (address, struct format)
    "start_grid_step": (0x8007E290, "<4h"),     # StartGridSceneryStep[2] {s16 x, s16 z}
    "start_grid_pos": (0x8007E298, "<8i"),      # Vec4[2]
    "start_grid_angle": (0x8007E2B8, "<2i"),    # s32[2] yaw
    "anim_pos": (0x8007E2C0, "<8i"),            # Vec4[2] {x, y, z, yaw}
    "anim_pitch": (0x8007E2E0, "<2h"),          # s16[2]
    "spin_pos": (0x8007E2F4, "<16i"),           # Vec4[4] {x, y, z, yaw}
    "spin_angle": (0x8007E334, "<4h"),          # s16[4] initial Z spin
    "static_pos": (0x8007E340, "<4i"),          # Vec4 {x, y, z, yaw}
    "high_class": (0x8007E350, "<4i"),          # {x, y, z} + g_HighClassSceneryYaw
    "shuttle_points": (0x8007E360, "<24i"),     # ShuttlePath[3] {Vec4 endpoint[2]}
    "shuttle_angles": (0x8007E3C0, "<12h"),     # SVECTOR[3] {vx, vy, vz, pad}
}
EXE_FALLBACK = {
    "start_grid_step": (72, 4, -68, -14),
    "start_grid_pos": (46685, 6010, 12495, 0, 39567, 5782, 11986, 0),
    "start_grid_angle": (2926, 5078),
    "anim_pos": (39491, 5473, 11857, 3150, 20717, 5792, 10467, 2691),
    "anim_pitch": (-111, 0),
    "spin_pos": (17805, 5646, 44714, 590, 30065, 3143, 40558, 1995,
                 30171, 3054, 38836, 2007, 30888, 2954, 37357, 2005),
    "spin_angle": (0, 64, 128, 256),
    "static_pos": (40594, 6002, 11940, 1088),
    "high_class": (29266, 6039, 45612, 1621),
    "shuttle_points": (20908, 3188, 37947, 0, 27399, 250, 34015, 0,
                       6934, 1271, 31155, 0, 9723, 1547, 29190, 0,
                       9762, 1566, 28960, 0, 6887, 1275, 30981, 0),
    "shuttle_angles": (0, 3736, -240, 0, 0, 10234, 0, 0, 0, 10234, 0, 0),
}


def load_exe_tables(exe_path=None):
    """Read the scenery constants from the PAL executable; fall back to the transcribed copy."""
    p = Path(exe_path) if exe_path else EXE_PATH
    try:
        exe = p.read_bytes()
    except OSError:
        return dict(EXE_FALLBACK), False
    if exe[:8] != b"PS-X EXE":
        return dict(EXE_FALLBACK), False
    taddr = _u32(exe, 0x18)
    out = {}
    for name, (addr, fmt) in EXE_TABLES.items():
        out[name] = struct.unpack_from(fmt, exe, addr - taddr + 0x800)
    return out, True


# ---------------------------------------------------------------- matrices

def _rs(a):
    t = (a & 0xFFF) * 2.0 * math.pi / 4096.0
    return math.sin(t), math.cos(t)


def rot_x(a):   # BuildRotMatrixX (render/rot_matrix.c:43)
    s, c = _rs(a)
    return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))


def rot_y(a):   # BuildRotMatrixY (render/rot_matrix.c:25)
    s, c = _rs(a)
    return ((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c))


def rot_z(a):   # BuildRotMatrixZ (render/rot_matrix.c)
    s, c = _rs(a)
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def matmul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def apply(m, v):
    return tuple(m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3))


# ---------------------------------------------------------------- instances

def _wrap_near(value, ref):
    """DrawCourseObjects / SetGteObjectMatrix subtract the camera into s16 relative[3],
    so a coordinate only matters modulo 65536 around the camera."""
    while value - ref > 32767:
        value -= 65536
    while value - ref < -32768:
        value += 65536
    return value


def _inst(source, model_index, pos_world, matrix, rotation, kind, drawer, data_source,
          condition, evidence, **extra):
    t = tuple(int(round(c * GTE_SCALE)) for c in pos_world)
    d = {"source": source, "model_index": model_index, "translation": t,
         "position_world": tuple(pos_world), "rotation": rotation, "matrix": matrix,
         "kind": kind, "drawer": drawer, "data_source": data_source,
         "condition": condition, "evidence": evidence}
    d.update(extra)
    return d


def get_instances(pack, grand_prix_class=None, series=0, env_mode4=False,
                  include_terrain=True, include_dynamic=True, all_conditions=False, exe_tables=None):
    """Instances for one course pack.

    pack: id / name / path / (id, bytes) / an already-parsed scene dict.
    grand_prix_class: defaults to the pack's own class (pack index = 0x58 + class*8 + course*2).
    series: g_RaceSeries (0/1) for the per-series tables.
    env_mode4: g_IsEnvironmentMode4 (selects model 0x3A vs 0x39 for the static landmark).
    all_conditions: also return instances whose class/course condition is false (active=False).
    """
    scene = pack if isinstance(pack, dict) else parse_pack(pack)
    meta = scene["meta"]
    course = meta["course"]
    gpc = meta["class"] if grand_prix_class is None else grand_prix_class
    ncm = len(scene["course_models"])
    exe = exe_tables or load_exe_tables()[0]
    ty = sorted(p["y"] for p in scene["track"]["points"])[len(scene["track"]["points"]) // 2]
    out = []

    def add(active, inst):
        inst["active"] = bool(active)
        if active or all_conditions:
            out.append(inst)

    def cm(idx):   # every caller clamps: (idx < g_CourseModelCount) ? idx : 1
        return idx if idx < ncm else 1

    if include_terrain:
        for c in scene["terrain"]["cells"]:
            if c["translation"] is None:
                continue
            tx, tyy, tz = c["translation"]
            add(True, _inst("terrain_cell", c["id"], (tx / 4.0, 0.0, tz / 4.0), IDENTITY, None, "static",
                            "DrawTerrainCells -> SubmitTerrainCells",
                            "block7 grid[%d] (sx %d, sy %d)" % (c["grid_index"], c["sx"], c["sy"]),
                            "always (cull: region visibility)",
                            "visible_cells.c:139-171 BuildVisibleCells vec=((sx<<11)-(cam-1024))<<2, "
                            "cell id grid[((31-sy)<<5)+sx]&0x3FF; terrain_submission.s:43-60 TRX/TRY/TRZ "
                            "= list x/y/z, rotation = view only (draw_terrain_cells.c:15)",
                            grid_sx=c["sx"], grid_sy=c["sy"]))

    for o in scene["course_objects"]:
        if o["model_id"] == -1:
            continue
        y_eff = _wrap_near(o["y"], ty)
        mode2 = (o["flags"] & 2) if env_mode4 else (o["flags"] % 2)
        add(True, _inst("course_model", o["model_id"], (o["x"], y_eff, o["z"]), rot_y(o["rotation_y"]),
                        {"composition": "RotY(yaw)", "yaw": o["rotation_y"]}, "static",
                        "DrawCourseObjects -> %s" % ("SubmitCourseModel2" if mode2 else "SubmitCourseModel"),
                        "block8 object %d (raw y %d, flags 0x%x)" % (o["index"], o["y"], o["flags"]),
                        "always (cull: cell (x/2048, z/2048) in g_VisibleCellMask)",
                        "visible_cells.c:36-93 DrawCourseObjects: BuildRotMatrixY(rotationY), "
                        "t = 4*View*s16((u16)pos - cam)",
                        flags=o["flags"], raw_y=o["y"],
                        # visible_cells.c:80-87: ctx[0x84] = flag8 ? ((animTimer&0x10)==0)<<16
                        #                        : flag4 ? 0x10000 : 0  (face-mode word)
                        mode_word=("toggles 0/0x10000 every 16 frames" if o["flags"] & 8
                                   else 0x10000 if o["flags"] & 4 else 0)))

    z3 = 0x5000 if course == 3 else 0
    gp_ok = gpc != 5

    # Static landmark: DrawStaticScenery(course==3), all four courses.
    sp = exe["static_pos"]
    add(True, _inst("course_model", cm(0x3A if env_mode4 else 0x39), (sp[0], sp[1], sp[2] + z3),
                    rot_y(sp[3]), {"composition": "RotY(yaw)", "yaw": sp[3]}, "static",
                    "DrawStaticScenery -> %s" % ("SubmitCourseModel" if env_mode4 else "SubmitCourseModel2"),
                    "exe g_StaticSceneryPos 0x8007E340", "all courses; z+0x5000 on course 3",
                    "scenery.c:21-79 DrawStaticScenery, scenery.c:115-151 DrawCourseScenery",
                    alt_models=[cm(0x39), cm(0x3A)]))

    # High-class landmark: course 0, class >= 4, no cull.
    hc = exe["high_class"]
    add(course == 0 and gpc >= 4,
        _inst("course_model", cm(0x3F), (hc[0], hc[1], hc[2]), rot_y(hc[3]),
              {"composition": "RotY(yaw)", "yaw": hc[3]}, "static",
              "DrawHighClassScenery", "exe 0x8007E350 (g_HighClassSceneryYaw - 3 words)",
              "course 0 and class >= 4", "scenery.c:81-106, scenery.c:120-125"))

    # Animated screen (two parts): GP only, class != 5, instance 1 only on course 3.
    ap = exe["anim_pos"]
    pitch = exe["anim_pitch"]
    for inst in (0, 1):
        x, y, z, yaw = ap[4 * inst:4 * inst + 4]
        m = matmul(rot_y(yaw), rot_x(pitch[inst]))
        act = gp_ok and (inst == 0 or course == 3)
        # race: frame+10 (10..22) / racePosition (1..3) / frame+0x18 (24..39); replay
        # (DrawAnimatedScenery2): frame+10 over 16 frames (10..25)
        for part, model, alts in (("frame", 0x18, list(range(10, 26)) + [1, 2, 3] + list(range(0x18, 0x28))),
                                  ("companion", 7, list(range(4, 10)))):
            add(act, _inst("course_model", cm(model), (x, y, z + z3), m,
                           {"composition": "RotY(yaw)*RotX(pitch)", "yaw": yaw, "pitch": pitch[inst]},
                           "animated", "DrawAnimatedScenery -> SubmitCourseModel",
                           "exe g_AnimSceneryPos[%d] 0x%08X" % (inst, 0x8007E2C0 + 16 * inst),
                           "Grand Prix mode, class != 5%s; z+0x5000 on course 3" %
                           ("" if inst == 0 else ", course 3 only"),
                           "dynamic_track_views.c:201-309 (model swaps every 4 frames)",
                           part=part, alt_models=sorted(set(cm(a) for a in alts))))

    # Start-grid figure: GP mode, class != 5, race phase < 2, scene timer >= 81.
    gp = exe["start_grid_pos"]
    ga = exe["start_grid_angle"]
    gs = exe["start_grid_step"]
    x, y, z = gp[4 * series:4 * series + 3]
    add(gp_ok, _inst("course_model", cm(0x28), (x, y, z + z3), rot_y(ga[series]),
                     {"composition": "RotY(yaw)", "yaw": ga[series]}, "animated",
                     "DrawStartGridScenery -> SubmitCourseModel",
                     "exe g_StartGridSceneryPos[%d] 0x%08X" % (series, 0x8007E298 + 16 * series),
                     "Grand Prix mode, class != 5, race phase < 2; z+0x5000 on course 3",
                     "dynamic_track_views.c:153-196, race_scene.c:296-299",
                     alt_models=sorted(set(cm(0x28 + r) for r in range(15))),
                     step_per_15_frames=(gs[2 * series], gs[2 * series + 1])))

    # Spinners: course 0 -> entry 0; course 1 and class >= 2 -> entries 1..3. Model 0x3E.
    spn = exe["spin_pos"]
    sang = exe["spin_angle"]
    for i in range(4):
        x, y, z, yaw = spn[4 * i:4 * i + 4]
        act = (course == 0 and i == 0) or (course == 1 and gpc >= 2 and i >= 1)
        add(act, _inst("course_model", cm(0x3E), (x, y, z), matmul(rot_y(yaw), rot_z(sang[i])),
                       {"composition": "RotY(yaw)*RotZ(spin)", "yaw": yaw, "spin": sang[i]}, "animated",
                       "DrawSpinningScenery -> SubmitCourseModel2",
                       "exe g_SpinningSceneryPos[%d] 0x%08X" % (i, 0x8007E2F4 + 16 * i),
                       "course 0 entry 0; course 1 entries 1-3 from class 2",
                       "draw_spinning_scenery.c:23-80, scenery.c:115-135"))

    # Shuttles: course 1 instance 0 on path 0 (model 0x3F); course 2 paths 1 and 2 (model 0x3C).
    pts = exe["shuttle_points"]
    angs = exe["shuttle_angles"]
    for path in (0, 1, 2):
        act = (course == 1 and path == 0) or (course == 2 and path in (1, 2))
        ep0 = pts[8 * path:8 * path + 3]
        ep1 = pts[8 * path + 4:8 * path + 7]
        ay, az = angs[4 * path + 1], angs[4 * path + 2]
        model = cm(0x3C if course in (2, 3) else 0x3F)
        add(act, _inst("course_model", model, ep0, matmul(rot_y(ay), rot_z(az)),
                       {"composition": "RotY(angleY)*RotZ(angleZ)", "angleY": ay, "angleZ": az},
                       "animated", "DrawShuttleScenery -> SubmitCourseModel",
                       "exe g_ShuttlePathPoints[%d] 0x%08X endpoint 0" % (path, 0x8007E360 + 32 * path),
                       "course 1 path 0; course 2 paths 1 and 2",
                       "update_shuttle_scenery.c (lerp between endpoints), draw_route_scenery.c:39-83 "
                       "InitShuttleScenery",
                       endpoint1_world=ep1))

    if include_dynamic:
        ev = scene["events"]
        cls5 = gpc % 5
        # Route prop: bank 1 model 0x25, every class (DrawScriptedScenery cases 0..4).
        r = ev["route"]
        kf = r["first"][series]
        m = matmul(matmul(rot_y(0x800 - kf["rotation_y"]), rot_x(kf["rotation_x"])), rot_z(kf["rotation_z"]))
        nb1 = scene["bank1"]["count"]
        add(True, _inst("bank1", 0x25 if nb1 >= 0x26 else 1, r["start"][series], m,
                        {"composition": "RotY(0x800-ry)*RotX(rx)*RotZ(rz)", "rx": kf["rotation_x"],
                         "ry": kf["rotation_y"], "rz": kf["rotation_z"]}, "animated",
                        "DrawRouteScenery -> SubmitModel (bank 1)",
                        "block9 route start[%d] (+0x%x)" % (series, EVENT_OFFSETS_AT + ev["offsets"]["route"]),
                        "Grand Prix mode, every class",
                        "draw_route_scenery.c:1-22, draw_scripted_scenery.c:11-42, "
                        "race_data_installation.c:17-46"))
        # Flyby prop: bank 2 model 0, class%5 in 1..4, only while its timer runs.
        f = ev["flyby"]
        nb2 = scene["bank2"]["count"]
        add(cls5 in (1, 2, 3, 4),
            _inst("bank2", 0 if nb2 >= 1 else 1, f["start"][series], rot_y(0x800),
                  {"composition": "RotY(0x800-ry)*RotX(rx)*RotZ(rz)", "rx": 0, "ry": 0, "rz": 0}, "animated",
                  "DrawFlybyScenery -> SubmitModel (bank 2)",
                  "block9 flyby start[%d] (+0x%x)" % (series, EVENT_OFFSETS_AT + ev["offsets"]["flyby"]),
                  "class %% 5 in 1..4, after its trigger lap/section",
                  "draw_flyby_scenery.c, scenery.c UpdateFlybyScenery"))
        # Path prop: bank 1 models 0x23 (body) and 0x24 (rotor), class%5 in 3..4.
        pth = ev["path"]
        rx, ry, rz = pth["rotation"][series]
        m = matmul(matmul(rot_y(0x800 - ry), rot_x(rx)), rot_z(rz))
        for model, part in ((0x23, "body"), (0x24, "rotor")):
            add(cls5 in (3, 4),
                _inst("bank1", model if nb1 > model else 1, pth["position"][series], m,
                      {"composition": "RotY(0x800-ry)*RotX(rx)*RotZ(rz)" + ("*RotY(t)" if part == "rotor" else ""),
                       "rx": rx, "ry": ry, "rz": rz}, "animated",
                      "DrawPathScenery -> SubmitModel (bank 1)",
                      "block9 path position key %d / rotation key %d" %
                      (pth["first_position_key"][series], pth["first_rotation_key"][series]),
                      "class %% 5 in 3..4", "draw_path_scenery.c:16-66, draw_scripted_scenery.c:58-100",
                      part=part))
    return out


# ---------------------------------------------------------------- geometry

def instance_geometry(scene, inst):
    """(local vertices, faces) of the model an instance draws."""
    src = inst["source"]
    if src == "terrain_cell":
        cell = scene["terrain"]["cells"][inst["model_index"]]
        used = sorted({vi for f in cell["faces"] for vi in f["idx"]})
        remap = {vi: k for k, vi in enumerate(used)}
        verts = [scene["terrain"]["pool"][vi] for vi in used]
        faces = [tuple(remap[vi] for vi in f["idx"]) for f in cell["faces"]]
        return verts, faces
    if src == "course_model":
        m = scene["course_models"][inst["model_index"]]
        return m["vertices"], [f["idx"] for f in m["faces"]]
    bank = scene["bank1" if src == "bank1" else "bank2"]
    m = bank["models"][inst["model_index"]]
    used = sorted({vi for f in m["faces"] for vi in f["idx"]})
    remap = {vi: k for k, vi in enumerate(used)}
    return [bank["vertices"][vi] for vi in used], [tuple(remap[vi] for vi in f["idx"]) for f in m["faces"]]


def instance_world_vertices(scene, inst):
    verts, _ = instance_geometry(scene, inst)
    t = inst["translation"]
    mtx = inst["matrix"]
    out = []
    for v in verts:
        r = apply(mtx, v)
        out.append((t[0] + r[0], t[1] + r[1], t[2] + r[2]))
    return out


def world_bbox(points):
    return (tuple(min(p[k] for p in points) for k in range(3)), tuple(max(p[k] for p in points) for k in range(3)))


class TerrainHeight(object):
    """Ground height query over the placed terrain cells (strip-ordered quads -> 2 triangles)."""

    BUCKET = 1024

    def __init__(self, scene):
        self.tris = []
        self.buckets = {}
        pool = scene["terrain"]["pool"]
        for c in scene["terrain"]["cells"]:
            if c["translation"] is None:
                continue
            tx, _, tz = c["translation"]
            for f in c["faces"]:
                v = [(pool[i][0] + tx, pool[i][1], pool[i][2] + tz) for i in f["idx"]]
                for tri in ((v[0], v[1], v[2]), (v[1], v[3], v[2])):
                    k = len(self.tris)
                    self.tris.append(tri)
                    x0 = min(p[0] for p in tri) // self.BUCKET
                    x1 = max(p[0] for p in tri) // self.BUCKET
                    z0 = min(p[2] for p in tri) // self.BUCKET
                    z1 = max(p[2] for p in tri) // self.BUCKET
                    for bx in range(x0, x1 + 1):
                        for bz in range(z0, z1 + 1):
                            self.buckets.setdefault((bx, bz), []).append(k)

    def heights(self, x, z):
        res = []
        for k in self.buckets.get((int(x) // self.BUCKET, int(z) // self.BUCKET), ()):
            a, b, c = self.tris[k]
            d = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
            if d == 0:
                continue
            l1 = ((b[2] - c[2]) * (x - c[0]) + (c[0] - b[0]) * (z - c[2])) / d
            l2 = ((c[2] - a[2]) * (x - c[0]) + (a[0] - c[0]) * (z - c[2])) / d
            l3 = 1.0 - l1 - l2
            if l1 >= -1e-9 and l2 >= -1e-9 and l3 >= -1e-9:
                res.append(l1 * a[1] + l2 * b[1] + l3 * c[1])
        return res

    def nearest(self, x, z, y):
        hs = self.heights(x, z)
        if not hs:
            return None
        return min(hs, key=lambda h: abs(h - y))


def terrain_seam_stats(scene):
    """Compare boundary vertices of 4-neighbour cells after placement (game pitch 8192)."""
    terr = scene["terrain"]
    pool = terr["pool"]
    at = {}
    pos = {}
    for c in terr["cells"]:
        if c["translation"] is None:
            continue
        pos[c["id"]] = (c["sx"], c["sy"])
        at[(c["sx"], c["sy"])] = c
    pts = {}
    for c in at.values():
        tx, _, tz = c["translation"]
        s = set()
        for f in c["faces"]:
            for i in f["idx"]:
                s.add((pool[i][0] + tx, pool[i][1], pool[i][2] + tz))
        pts[c["id"]] = s
    exact = plus1 = other = pairs = 0
    for (sx, sy), c in at.items():
        for dx, dz in ((1, 0), (0, 1)):
            n = at.get((sx + dx, sy + dz))
            if n is None:
                continue
            pairs += 1
            mine = pts[c["id"]]
            index = {}
            for q in pts[n["id"]]:
                index.setdefault((q[0] >> 3, q[2] >> 3), []).append(q)
            for p in mine:
                best = None
                for bx in (p[0] >> 3, (p[0] >> 3) - 1, (p[0] >> 3) + 1):
                    for bz in (p[2] >> 3, (p[2] >> 3) - 1, (p[2] >> 3) + 1):
                        for q in index.get((bx, bz), ()):
                            dd = (q[0] - p[0], q[1] - p[1], q[2] - p[2])
                            if max(abs(e) for e in dd) <= 2 and (best is None or
                                                                 sum(abs(e) for e in dd) < sum(abs(e) for e in best)):
                                best = dd
                if best is None:
                    continue
                if best == (0, 0, 0):
                    exact += 1
                elif best == (dx, 0, dz):
                    plus1 += 1
                else:
                    other += 1
    return {"neighbour_pairs": pairs, "exact": exact, "plus1_along_axis": plus1, "other_le2": other}


# ---------------------------------------------------------------- OBJ

def write_obj(pack, path, instances=None, **kw):
    scene = pack if isinstance(pack, dict) else parse_pack(pack)
    insts = instances if instances is not None else get_instances(scene, **kw)
    lines = ["# Rage Racer PAL course assembly, asset %s (%s)" % (scene["asset_id"], scene["meta"]["name"]),
             "# units: GTE vertex units (= 4 x world units); Blender X=x, Y=z, Z=-y; quads f v0 v1 v3 v2",
             "# generated by tools/rr_placement.py"]
    base = 1
    for k, inst in enumerate(insts):
        if not inst.get("active", True):
            continue
        verts, faces = instance_geometry(scene, inst)
        wv = instance_world_vertices(scene, inst)
        if inst["source"] == "terrain_cell":
            name = "terrain_cell_%03d_sx%02d_sy%02d" % (inst["model_index"], inst["grid_sx"], inst["grid_sy"])
        else:
            name = "%s_%03d_%s_%d" % (inst["source"], inst["model_index"],
                                      inst["drawer"].split(" ")[0], k)
        lines.append("o %s" % name)
        for v in wv:
            lines.append("v %.3f %.3f %.3f" % (v[0], v[2], -v[1]))
        for f in faces:
            order = (f[0], f[1], f[3], f[2])
            poly = []
            for i in order:
                if not poly or poly[-1] != i:
                    poly.append(i)
            if len(poly) > 1 and poly[0] == poly[-1]:
                poly.pop()
            if len(set(poly)) < 3:
                continue
            lines.append("f " + " ".join(str(base + i) for i in poly))
        base += len(wv)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n")
    return base - 1


# ---------------------------------------------------------------- reporting

def _fmt_rot(r):
    if not r:
        return "-"
    return r["composition"] + " " + ",".join("%s=%d" % (k, v) for k, v in r.items() if k != "composition")


def placement_checks(scene, insts, hf=None):
    hf = hf or TerrainHeight(scene)
    tp = scene["track"]["points"]
    dys = []
    miss = 0
    for p in tp:
        h = hf.nearest(4 * p["x"], 4 * p["z"], 4 * p["y"])
        if h is None:
            miss += 1
        else:
            dys.append(abs(h - 4 * p["y"]))
    dys.sort()
    track = {"points": len(tp), "no_ground": miss,
             "median_abs_dy": dys[len(dys) // 2] if dys else None,
             "within_64": sum(1 for d in dys if d <= 64)}
    inst_rows = []
    for inst in insts:
        if inst["source"] == "terrain_cell":
            continue
        t = inst["translation"]
        bottom = world_bbox(instance_world_vertices(scene, inst))[1][1]   # largest y = lowest point
        h = hf.nearest(t[0], t[2], bottom)
        approx = False
        if h is None:
            for r in (256, 512, 1024):
                hs = [hf.nearest(t[0] + dx, t[2] + dz, bottom) for dx, dz in ((r, 0), (-r, 0), (0, r), (0, -r))]
                hs = [q for q in hs if q is not None]
                if hs:
                    h = min(hs, key=lambda q: abs(q - bottom))
                    approx = True
                    break
        inst["ground_dy"] = None if h is None else bottom - h
        inst["ground_dy_approx"] = approx
        inst_rows.append((inst, inst["ground_dy"]))
    return track, inst_rows


def print_summary(scene, insts, checks=True):
    m = scene["meta"]
    print("asset %03d  %s  size %d" % (scene["asset_id"], m["name"], scene["size"]))
    t = scene["terrain"]
    print("  terrain: %d cells (%d placed in grid), vertex pool %d, faces %d" %
          (t["cell_count"], t["placed"], len(t["pool"]), sum(len(c["faces"]) for c in t["cells"])))
    cms = scene["course_models"]
    print("  course models: %d (max vertexCount %d); course objects: %d; bank1 %d models, bank2 %d models" %
          (len(cms), max(c["vertex_count"] for c in cms), len(scene["course_objects"]),
           scene["bank1"]["count"], scene["bank2"]["count"]))
    sx = [c["sx"] for c in t["cells"] if c["sx"] is not None]
    sy = [c["sy"] for c in t["cells"] if c["sy"] is not None]
    tp = scene["track"]["points"]
    print("  grid cells sx %d..%d sy %d..%d  -> world x %d..%d z %d..%d; track x %d..%d z %d..%d y %d..%d" %
          (min(sx), max(sx), min(sy), max(sy), min(sx) * CELL_WORLD, (max(sx) + 1) * CELL_WORLD,
           min(sy) * CELL_WORLD, (max(sy) + 1) * CELL_WORLD,
           min(p["x"] for p in tp), max(p["x"] for p in tp), min(p["z"] for p in tp), max(p["z"] for p in tp),
           min(p["y"] for p in tp), max(p["y"] for p in tp)))
    if not checks:
        return
    seam = terrain_seam_stats(scene)
    print("  terrain seams (pitch 8192): %d neighbour pairs; boundary vertex matches within 2u: exact %d, "
          "+1 along neighbour axis %d, other %d" %
          (seam["neighbour_pairs"], seam["exact"], seam["plus1_along_axis"], seam["other_le2"]))
    track, rows = placement_checks(scene, insts)
    print("  track points on terrain: %d/%d have ground, median |ground - 4*y| = %s vertex units, %d within 64" %
          (track["points"] - track["no_ground"], track["points"],
           None if track["median_abs_dy"] is None else round(track["median_abs_dy"], 1), track["within_64"]))
    print("  instances (non-terrain; translation in vertex units = 4 x world; dyG = lowest vertex y - ground y "
          "under the origin, + = below ground, ~ = ground sampled up to 1024u away):")
    print("    %-12s %4s %8s %8s %8s  %-38s %-8s %6s  %s" %
          ("source", "idx", "tx", "ty", "tz", "rotation", "kind", "dyG", "drawer / data"))
    for inst, dy in rows:
        tt = inst["translation"]
        print("    %-12s %4d %8d %8d %8d  %-38s %-8s %6s  %s / %s%s" %
              (inst["source"], inst["model_index"], tt[0], tt[1], tt[2], _fmt_rot(inst["rotation"])[:38],
               inst["kind"], "-" if dy is None else ("~" if inst.get("ground_dy_approx") else "") + "%d" % round(dy),
               inst["drawer"], inst["data_source"],
               "" if inst.get("active", True) else "  [inactive: %s]" % inst["condition"]))


def course_model_users(scene, insts):
    users = {}
    for inst in insts:
        if inst["source"] != "course_model":
            continue
        for mi in [inst["model_index"]] + list(inst.get("alt_models", [])):
            users.setdefault(mi, set()).add(inst["drawer"].split(" ")[0])
    return users


def run_all():
    exe, from_exe = load_exe_tables()
    if from_exe:
        for k, v in EXE_FALLBACK.items():
            check(tuple(exe[k]) == tuple(v), "exe table %s differs from transcription" % k)
    print("exe scenery tables: %s" % ("read from %s, match transcription" % EXE_PATH if from_exe
                                       else "exe missing, using transcription"))
    n_scene = n_other = 0
    totals = {"cells": 0, "objects": 0, "instances": 0, "track_ok": 0, "track_pts": 0,
              "seam_exact": 0, "seam_plus1": 0, "seam_other": 0}
    max_cm_vc = 0
    cm_extent = 0
    bad = []
    for aid in range(FIRST_PACK, LAST_PACK + 1):
        _, data = load_pack(aid)
        meta = pack_meta(aid)
        if not is_scene_pack(data):
            check(meta["half"] == "1ST", "asset %d (.2ND) is not a scene pack" % aid)
            n_other += 1
            continue
        check(meta["half"] == "2ND", "asset %d (.1ST) parsed as a scene pack" % aid)
        try:
            scene = parse_pack((aid, data))
            check(scene["terrain"]["shared_vertices"] == 0, "terrain vertices shared between cells")
            check(scene["terrain"]["unreferenced_vertices"] == 0, "unreferenced terrain vertices")
            check(scene["terrain"]["placed"] == scene["terrain"]["cell_count"], "cells missing from grid")
            for c in scene["course_models"]:
                check(-(-c["vertex_count"] // 3) * 3 <= COURSE_MODEL_VERTEX_LIMIT,
                      "course model %d has %d vertices (> 120 SXY slots)" % (c["index"], c["vertex_count"]))
                max_cm_vc = max(max_cm_vc, c["vertex_count"])
                bb = c["bbox"]
                cm_extent = max(cm_extent, max(abs(e) for e in bb[0] + bb[1]))
            insts = get_instances(scene, exe_tables=exe)
            seam = terrain_seam_stats(scene)
            track, rows = placement_checks(scene, insts)
        except FormatError as ex:
            bad.append((aid, str(ex)))
            continue
        n_scene += 1
        totals["cells"] += scene["terrain"]["cell_count"]
        totals["objects"] += len(scene["course_objects"])
        totals["instances"] += len(insts)
        totals["track_ok"] += track["within_64"]
        totals["track_pts"] += track["points"]
        totals["seam_exact"] += seam["exact"]
        totals["seam_plus1"] += seam["plus1_along_axis"]
        totals["seam_other"] += seam["other_le2"]
        static_dy = [dy for inst, dy in rows if inst["kind"] == "static" and dy is not None]
        floating = [(inst["drawer"].split(" ")[0], inst["model_index"], round(dy)) for inst, dy in rows
                    if dy is not None and abs(dy) > 256]
        noground = [(inst["drawer"].split(" ")[0], inst["model_index"]) for inst, dy in rows if dy is None]
        print("%03d %-26s cells %3d objs %2d cm %2d inst %3d | seams exact %3d +1 %4d other %3d | "
              "track %3d/%3d within 64u (median %4s) | static |dyG| max %s | |dyG|>256: %s | no ground: %s" %
              (aid, "%s c%d" % (COURSE_NAMES[meta["course"]].split(" ")[0], meta["class"]),
               scene["terrain"]["cell_count"], len(scene["course_objects"]), len(scene["course_models"]),
               len(insts), seam["exact"], seam["plus1_along_axis"], seam["other_le2"],
               track["within_64"], track["points"],
               None if track["median_abs_dy"] is None else int(round(track["median_abs_dy"])),
               None if not static_dy else int(round(max(abs(d) for d in static_dy))),
               floating or "-", noground or "-"))
    print("scene packs %d, non-scene (.1ST) packs %d, failures %d" % (n_scene, n_other, len(bad)))
    for aid, msg in bad:
        print("  FAIL %03d: %s" % (aid, msg))
    print("totals: %s; max course-model vertexCount %d; max |course-model local coord| %d" %
          (totals, max_cm_vc, cm_extent))
    return 1 if bad else 0


def main(argv):
    args = list(argv[1:])
    opts = {"obj": None, "class": None, "series": 0, "env4": False, "allc": False}
    pos = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--all":
            return run_all()
        if a == "--obj":
            opts["obj"] = args[i + 1]
            i += 2
            continue
        if a == "--class":
            opts["class"] = int(args[i + 1])
            i += 2
            continue
        if a == "--series":
            opts["series"] = int(args[i + 1])
            i += 2
            continue
        if a == "--env4":
            opts["env4"] = True
        elif a == "--all-conditions":
            opts["allc"] = True
        else:
            pos.append(a)
        i += 1
    pack = pos[0] if pos else 88
    scene = parse_pack(pack)
    insts = get_instances(scene, grand_prix_class=opts["class"], series=opts["series"],
                          env_mode4=opts["env4"], all_conditions=opts["allc"])
    print_summary(scene, insts)
    users = course_model_users(scene, get_instances(scene, all_conditions=True))
    unused = [c["index"] for c in scene["course_models"] if c["index"] not in users]
    print("  course models referenced by any drawer (all conditions): %d of %d; never referenced here: %s" %
          (len(users), len(scene["course_models"]), unused or "-"))
    if opts["obj"]:
        n = write_obj(scene, opts["obj"], instances=insts)
        print("  wrote %s (%d vertices, %d objects)" % (opts["obj"], n, sum(1 for x in insts if x.get("active", True))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
