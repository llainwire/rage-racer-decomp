#!/usr/bin/env python3
"""
Rage Racer (PAL PS1) model-bank decoder: blocks 3 and 6 of the course scene
packs, and the same ModelBankHeader format inside the car packs (even assets
10..72), the car-select pack (asset 8) and the option-screen pack (asset 9).

Evidence (see notes_model_banks.md for the full write-up):
  model_banks.c RegisterModelBank / SelectModelBank: header {u32 count; s32
      table; s32 normals; s32 models[count]}, offsets relative to the bank start;
      ctx+0x54 = table, ctx+0x58 = normals, ctx+0x50 = &models[0],
      g_ModelBankCount = count (model_banks.c:84-102).
  terrain_submission.s SubmitModel (975): models[index] -> u32 command words
      (lo16 opcode, hi16 face count, 0 ends); jal SubmitModelFaces with the
      command word + 4 in the delay slot (line 1011), so stored opcode k uses
      jtbl_8007DA14 entry k+4 (non-fog emitters). SubmitModel2 (1348) has a nop
      there (line 1384): entry k (fog / depth-cue emitters); no caller in PAL.
  SubmitModelFaces (1028): idx0 = w0 lo16, idx1 = w0 hi16, idx2 = w1 lo16,
      idx3 = w1 hi16; vertex = ctx+0x54 + idx*8, lwc2 VXY0 @+0 / VZ0 @+4.
      The face loop is a do-while (1144-1146, and every emitter's own
      addiu -1 / bnez), so a command with face count 0 would run 65536*65536
      times: parse_model rejects it.
  Emitters (1153..1583) give the face records decoded in parse_face().

Face records (offsets from the face start; 'spare' = bytes no emitter reads):
  op0 F4  0x10: idx[4] | +8 r g b code | +C spare | +D s8 otBias | +E,+F spare
  op1 FT4 0x18: idx[4] | +8 u0 v0 clut | +C u1 v1 tpage | +10 u2 v2 u3 v3
                | +14 spare | +15 s8 otBias | +16,+17 spare
  op2 G4  0x18: idx[4] | +8 n0 n1 n2 n3 (u16, normal pool) | +10 r g b code
                | +14 spare | +15 s8 otBias | +16,+17 spare
  op3 GT4 0x20: idx[4] | +8 n0..n3 | +10 u0 v0 clut | +14 u1 v1 tpage
                | +18 u2 v2 u3 v3 | +1C spare | +1D s8 otBias | +1E,+1F spare

Degenerate faces (index-equality pattern over the pairs 01 02 03 12 13 23):
  000000 quad; 000001 idx2==idx3 and 000010 idx1==idx3 are drawn triangles
  (v0 v1 v2); any repeat among v0..v2 gives NCLIP MAC0 == 0 (lines 1066-1075),
  so the face is never drawn. check_bank limits each bank family to the
  patterns observed (ALLOWED_PATTERNS) and asserts that the duplicated corner
  of a triangle also duplicates its uv and normal.

Usage:
  python -I tools/rr_model_banks.py [asset_088|88|path] [-v]
  python -I tools/rr_model_banks.py --all
  python -I tools/rr_model_banks.py asset_088 --obj extracted/mb_088.obj [--bank N] [--flip]

Importable API (no side effects on import):
  load_asset(arg) -> (asset_index or None, bytes)
  locate_banks(data, asset_index) -> [(label, base, end)]
  parse_bank(data, base, end, label) -> Bank (models -> commands -> Face with
      idx, nrm, rgb, code, clut, tpage, uv, bias, pattern, drawn, triangle)
  banks_for_asset(arg) -> [Bank]
  check_bank(bank) -> [error strings] (complete, never truncated)
  bank_stats(bank) -> dict
  model_role(bank, index) -> caller-derived role string
  course_class_slot(asset_index) -> (class, course slot)
  write_obj(banks, path, scale=1.0, flip=False, stats=None)
"""
import argparse
import hashlib
import math
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "extracted" / "assets_raw"

KIND = {0: "F4", 1: "FT4", 2: "G4", 3: "GT4"}
STRIDE = {0: 0x10, 1: 0x18, 2: 0x18, 3: 0x20}
BIAS_AT = {0: 0x0D, 1: 0x15, 2: 0x15, 3: 0x1D}
SPARE_AT = {0: (0x0C, 0x0E, 0x0F), 1: (0x14, 0x16, 0x17),
            2: (0x14, 0x16, 0x17), 3: (0x1C, 0x1E, 0x1F)}
STORED_CODE = {0: 0x28, 2: 0x38}     # F4 / G4 records carry their GPU code byte

COURSE_PACKS = range(87, 135)       # 0x57 + class*8 + course*2 (+1 for 2ND)
SCENE_PACKS = range(88, 135, 2)     # the 2ND packs (ASSET_TRACK_2ND_BASE 0x58)
CAR_PACKS = range(10, 73, 2)        # (GetCarAssetIndex << 1) + 0xA
CAR_SELECT, OPTION_SCREEN = 8, 9

# index-equality pattern over the corner pairs below ('1' = equal indices)
PAIRS = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))
PATTERN_NAME = {
    "000000": "quad",
    "000001": "tri i2=i3",
    "000010": "tri i1=i3",
    "100001": "undrawn a,a,c,c",
    "100000": "undrawn a,a,c,d",
    "010010": "undrawn a,b,a,b",
    "010000": "undrawn a,b,a,d",
}
TRI_DUP = {"000001": (2, 3), "000010": (1, 3)}    # duplicated corner pair
_COMMON = frozenset(("000000", "000001"))
ALLOWED_PATTERNS = {
    "bank1": _COMMON, "bank2": _COMMON, "car": _COMMON, "option": _COMMON,
    "carselect": _COMMON | {"000010", "100001", "100000", "010010", "010000"},
}
# 4.12 normals: every pool entry is either within this |n| range or the
# (-1,-1,-1) sentinel (|n| ~ 1.7, effectively a zero normal).
UNIT_LEN = (4090.0, 4100.0)
NULL_NORMAL = (-1, -1, -1)
# where the sentinel occurs (asset, bank label, normal-pool index); --all
# asserts this list is complete
EXPECTED_NULL_NORMALS = {(a, "bank1", 320) for a in (94, 112, 114, 116, 118, 120, 122, 124, 126)} | \
    {(OPTION_SCREEN, "option", 901)}

# Which pack carries which bank (byte identity, asserted by --all).
# bank1 follows the class except two course-slot-3 packs; bank2 (with blocks
# 4 and 6) follows the course slot except 094, whose blocks 3..8 equal 120's.
BANK1_OVERRIDE = {94: 4, 102: 2}     # asset -> class whose bank1 it carries
BANK2_OVERRIDE = {94: 0}             # asset -> course slot whose bank2 it carries
# block-level identity of the two exceptional packs (asserted by --all)
EXPECTED_SAME_BLOCKS = {(94, 120): {3, 4, 5, 6, 7, 8}, (94, 88): {4, 6, 8},
                       (102, 110): {0, 3, 4, 5, 6, 7, 8, 9, 10}}
SCENERY_REF = (88, 35)               # 088 bank1 models 35..37 = the scenery models


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def s16(b, o):
    return struct.unpack_from("<h", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def s32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def index_pattern(idx):
    return "".join("1" if idx[p] == idx[q] else "0" for p, q in PAIRS)


@dataclass
class Face:
    opcode: int
    off: int                 # absolute file offset of the face record
    idx: tuple               # 4 vertex-pool indices (strip order v0 v1 / v2 v3)
    bias: int                # signed OT bias byte
    spare: tuple             # the bytes no emitter reads
    nrm: tuple = None        # 4 normal-pool indices (G4, GT4)
    rgb: tuple = None        # (r, g, b) (F4, G4)
    code: int = None         # GPU code byte stored in the record (F4, G4)
    clut: int = None         # FT4, GT4
    tpage: int = None        # FT4, GT4
    uv: tuple = None         # ((u0,v0),(u1,v1),(u2,v2),(u3,v3)) (FT4, GT4)

    @property
    def kind(self):
        return KIND[self.opcode]

    @property
    def textured(self):
        return self.opcode in (1, 3)

    @property
    def pattern(self):
        return index_pattern(self.idx)

    @property
    def drawn(self):
        """SubmitModelFaces culls on NCLIP of v0 v1 v2 (terrain_submission.s
        1066..1075: bgtz / bgez skip), so a repeated index among the first
        three gives MAC0 == 0 and the face is never emitted."""
        return len(set(self.idx[:3])) == 3

    @property
    def triangle(self):
        """Drawn face whose 4th corner repeats corner 1 or 2: the GPU quad
        (v0 v1 v2)+(v1 v3 v2) collapses to the triangle v0 v1 v2."""
        return self.drawn and self.idx[3] in (self.idx[1], self.idx[2])


@dataclass
class Command:
    opcode: int
    count: int
    off: int                 # absolute offset of the command word
    faces: list = field(default_factory=list)


@dataclass
class Model:
    index: int
    off: int                 # absolute offset of the first command word
    end: int                 # absolute offset just past the 0 terminator
    commands: list = field(default_factory=list)

    def faces(self):
        for c in self.commands:
            yield from c.faces

    def vertex_indices(self):
        return list(dict.fromkeys(i for f in self.faces() for i in f.idx))


@dataclass
class Bank:
    label: str
    base: int
    end: int
    count: int
    table_off: int           # bank-relative
    normals_off: int         # bank-relative
    model_offs: list         # bank-relative
    vertices: list           # (x, y, z, pad)
    normals: list            # (nx, ny, nz, pad)
    models: list
    digest: str = ""         # sha1 of the bank bytes (identity across packs)
    asset: int = None        # asset index the bank was read from


class BankError(Exception):
    pass


# ---------------------------------------------------------------- locating

def load_asset(arg):
    """'asset_088', '088', '88' or a path -> (asset index or None, bytes)."""
    p = Path(str(arg))
    if not p.is_file():
        name = str(arg)
        if name.isdigit():
            name = "asset_%03d" % int(name)
        if not name.endswith(".bin"):
            name += ".bin"
        p = RAW_DIR / name
    if not p.is_file():
        raise SystemExit("cannot find asset %r" % arg)
    idx = None
    if p.name.startswith("asset_") and p.name[6:9].isdigit():
        idx = int(p.name[6:9])
    return idx, p.read_bytes()


def scene_offsets(data, n=11):
    return list(struct.unpack_from("<%di" % n, data, 0))


def scene_blocks(data):
    """[(start, end)] of the 11 GameSceneAssetHeader blocks."""
    o = scene_offsets(data) + [len(data)]
    return [(o[i], o[i + 1]) for i in range(11)]


def is_scene_pack(data):
    """GameSceneAssetHeader: 11 increasing offsets, the first == 0x2C."""
    if len(data) < 0x2C:
        return False
    o = scene_offsets(data)
    return o[0] == 0x2C and all(o[k] < o[k + 1] for k in range(10)) and o[10] <= len(data)


def is_image_pack(data):
    """The 1ST course packs: a 5-offset header (first == 0x14) of image blocks
    that LoadRaceAssets step 5 hands to UploadImageAsset (race_assets.c:100)."""
    o = scene_offsets(data, 5)
    return o[0] == 0x14 and all(o[k] < o[k + 1] for k in range(4)) and o[4] < len(data)


def course_class_slot(asset_index):
    """Asset = 0x58 + g_GrandPrixClass*8 + g_CourseIndex*2 for the 2ND scene
    packs (fmv_requests.c:16-17, race_assets.c:137-139); 1ST packs are -1."""
    k = asset_index - 0x57
    return k // 8, (k % 8) // 2


def locate_banks(data, asset_index):
    """[(label, base, end)] of every ModelBankHeader in one asset."""
    if asset_index in COURSE_PACKS or (asset_index is None and is_scene_pack(data)):
        if not is_scene_pack(data):
            return []                           # 1ST image pack
        o = scene_offsets(data)
        return [("bank1", o[3], o[4]), ("bank2", o[6], o[7])]
    if asset_index in CAR_PACKS:
        # CarModelAsset: +0x20 modelData offset, +0x24 imageData offset
        return [("car", u32(data, 0x20), u32(data, 0x24))]
    if asset_index == CAR_SELECT:
        # car_assets.c:56 RegisterModelBank(cursor + 0xC, 0xE); offsets[0] follows
        return [("carselect", 0xC, u32(data, 0))]
    if asset_index == OPTION_SCREEN:
        # OptionScreenAsset {s32 imageOffset; ModelBankHeader modelBank}
        return [("option", 4, u32(data, 0))]
    return []


# ---------------------------------------------------------------- parsing

def parse_face(data, pos, op):
    idx = (u16(data, pos), u16(data, pos + 2), u16(data, pos + 4), u16(data, pos + 6))
    bias = struct.unpack_from("<b", data, pos + BIAS_AT[op])[0]
    spare = tuple(data[pos + k] for k in SPARE_AT[op])
    f = Face(op, pos, idx, bias, spare)
    if op == 0:
        f.rgb = tuple(data[pos + 8:pos + 11])
        f.code = data[pos + 0xB]
    elif op == 2:
        f.nrm = (u16(data, pos + 8), u16(data, pos + 0xA), u16(data, pos + 0xC), u16(data, pos + 0xE))
        f.rgb = tuple(data[pos + 0x10:pos + 0x13])
        f.code = data[pos + 0x13]
    else:
        t = pos + 8
        if op == 3:
            f.nrm = (u16(data, pos + 8), u16(data, pos + 0xA), u16(data, pos + 0xC), u16(data, pos + 0xE))
            t = pos + 0x10
        f.uv = ((data[t], data[t + 1]), (data[t + 4], data[t + 5]),
                (data[t + 8], data[t + 9]), (data[t + 10], data[t + 11]))
        f.clut = u16(data, t + 2)
        f.tpage = u16(data, t + 6)
    return f


def parse_model(data, index, pos, limit):
    m = Model(index, pos, pos)
    while True:
        if pos + 4 > limit:
            raise BankError("model %d runs past 0x%X" % (index, limit))
        w = u32(data, pos)
        pos += 4
        if w == 0:
            break
        op, n = w & 0xFFFF, w >> 16
        if op not in STRIDE:
            raise BankError("model %d: opcode %d at 0x%X" % (index, op, pos - 4))
        if n == 0:
            # do-while face loop (terrain_submission.s:1144-1146): count 0 never ends
            raise BankError("model %d: zero-count command 0x%08X at 0x%X" % (index, w, pos - 4))
        c = Command(op, n, pos - 4)
        for _ in range(n):
            if pos + STRIDE[op] > limit:
                raise BankError("model %d face runs past 0x%X" % (index, limit))
            c.faces.append(parse_face(data, pos, op))
            pos += STRIDE[op]
        m.commands.append(c)
    m.end = pos
    return m


def parse_bank(data, base, end, label="bank", asset=None):
    count = u32(data, base)
    if not 0 < count < 0x400:
        raise BankError("%s: implausible model count %d" % (label, count))
    table_off, normals_off = s32(data, base + 4), s32(data, base + 8)
    model_offs = [s32(data, base + 0xC + 4 * i) for i in range(count)]
    if not (0 < table_off < normals_off <= end - base):
        raise BankError("%s: table 0x%X normals 0x%X size 0x%X" % (label, table_off, normals_off, end - base))
    for i, o in enumerate(model_offs):
        if not 0xC + 4 * count <= o < table_off:
            raise BankError("%s: models[%d] = 0x%X outside the stream area" % (label, i, o))
    nv = (normals_off - table_off) // 8
    vertices = [struct.unpack_from("<hhhH", data, base + table_off + 8 * i) for i in range(nv)]
    nn = (end - base - normals_off) // 8
    normals = [struct.unpack_from("<hhhH", data, base + normals_off + 8 * i) for i in range(nn)]
    models = [parse_model(data, i, base + o, base + table_off) for i, o in enumerate(model_offs)]
    digest = hashlib.sha1(data[base:end]).hexdigest()[:10]
    return Bank(label, base, end, count, table_off, normals_off, model_offs, vertices, normals,
                models, digest, asset)


def banks_for_asset(arg):
    idx, data = load_asset(arg)
    return [parse_bank(data, b, e, lab, idx) for lab, b, e in locate_banks(data, idx)]


# ---------------------------------------------------------------- checks

def normal_length(n):
    return math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])


def null_normals(bank):
    """Pool indices of the (-1,-1,-1) sentinel normals."""
    return [i for i, n in enumerate(bank.normals) if n[:3] == NULL_NORMAL]


def check_bank(bank):
    """Return every consistency failure (empty list = all assertions hold)."""
    err = []
    size = bank.end - bank.base
    if bank.model_offs[0] != 0xC + 4 * bank.count:
        err.append("models[0]=0x%X, header ends 0x%X" % (bank.model_offs[0], 0xC + 4 * bank.count))
    # the command streams tile [header end, table) exactly, in index order
    pos = bank.base + 0xC + 4 * bank.count
    for m in bank.models:
        if m.off != pos:
            err.append("model %d starts 0x%X, expected 0x%X" % (m.index, m.off - bank.base, pos - bank.base))
        pos = m.end
    if pos != bank.base + bank.table_off:
        err.append("last stream ends 0x%X, table at 0x%X" % (pos - bank.base, bank.table_off))
    if (bank.normals_off - bank.table_off) % 8:
        err.append("vertex pool size 0x%X not a multiple of 8" % (bank.normals_off - bank.table_off))
    if (size - bank.normals_off) % 8:
        err.append("normal pool size 0x%X not a multiple of 8" % (size - bank.normals_off))
    nv, nn = len(bank.vertices), len(bank.normals)
    used_v, used_n = set(), set()
    allowed = ALLOWED_PATTERNS.get(bank.label, frozenset(PATTERN_NAME))
    for m in bank.models:
        for c in m.commands:
            if c.count <= 0:
                err.append("model %d command 0x%X has face count %d" % (m.index, c.off - bank.base, c.count))
        for f in m.faces():
            where = "model %d face 0x%X %s" % (m.index, f.off - bank.base, f.kind)
            if max(f.idx) >= nv:
                err.append("%s vertex %d >= %d" % (where, max(f.idx), nv))
            used_v.update(f.idx)
            if f.nrm is not None:
                if max(f.nrm) >= nn:
                    err.append("%s normal %d >= %d" % (where, max(f.nrm), nn))
                used_n.update(f.nrm)
            if f.opcode in STORED_CODE and f.code != STORED_CODE[f.opcode]:
                err.append("%s stores code 0x%02X" % (where, f.code))
            p = f.pattern
            if p not in allowed:
                err.append("%s index pattern %s %s not allowed for %s" % (
                    where, p, PATTERN_NAME.get(p, "?"), bank.label))
            if p in TRI_DUP:
                a, b = TRI_DUP[p]
                if f.uv is not None and f.uv[a] != f.uv[b]:
                    err.append("%s triangle %s with uv%d != uv%d" % (where, p, a, b))
                if f.nrm is not None and f.nrm[a] != f.nrm[b]:
                    err.append("%s triangle %s with n%d != n%d" % (where, p, a, b))
            if f.drawn and nv > max(f.idx):
                g = _tri_normal(*(bank.vertices[i] for i in f.idx[:3]))
                if g == (0, 0, 0):
                    err.append("%s has zero 3D area (v0 v1 v2 collinear)" % where)
    if len(used_v) != nv:
        err.append("%d of %d pool vertices unreferenced" % (nv - len(used_v), nv))
    if nn and len(used_n) != nn:
        err.append("%d of %d pool normals unreferenced" % (nn - len(used_n), nn))
    for i, n in enumerate(bank.normals):
        if n[:3] == NULL_NORMAL:
            continue
        L = normal_length(n)
        if not UNIT_LEN[0] <= L <= UNIT_LEN[1]:
            err.append("normal %d = %s, |n| = %.1f" % (i, n[:3], L))
    return err


def bank_stats(bank):
    faces = [f for m in bank.models for f in m.faces()]
    regular = [normal_length(n) for n in bank.normals if n[:3] != NULL_NORMAL]
    st = {
        "models": bank.count, "vertices": len(bank.vertices), "normals": len(bank.normals),
        "faces": Counter(f.kind for f in faces),
        "commands_per_model": Counter(len(m.commands) for m in bank.models),
        "patterns": Counter(f.pattern for f in faces),
        "tris": sum(1 for f in faces if f.triangle),
        "never_drawn": sum(1 for f in faces if not f.drawn),
        "vertex_pad": Counter(v[3] for v in bank.vertices),
        "normal_pad": Counter(n[3] for n in bank.normals),
        "normal_len": (min(regular, default=0), max(regular, default=0)),
        "null_normals": null_normals(bank),
        "codes": Counter((f.kind, f.code) for f in faces if f.code is not None),
        "bias": Counter(f.bias for f in faces),
        "spare": Counter(f.spare for f in faces),
        "tpages": Counter(f.tpage for f in faces if f.tpage is not None),
        "cluts": Counter(f.clut for f in faces if f.clut is not None),
    }
    owner = {}
    shared = contiguous = 0
    for m in bank.models:
        vi = m.vertex_indices()
        if vi and max(vi) - min(vi) + 1 == len(vi):
            contiguous += 1
        for i in vi:
            if i in owner and owner[i] != m.index:
                shared += 1
            owner.setdefault(i, m.index)
    st["models_contiguous"] = contiguous
    st["shared_vertex_refs"] = shared
    # stored lighting normals vs face winding: PS1-space right-hand (v1-v0)x(v2-v0)
    # and vs the direction from the model centroid to the face centroid
    pos_dot = neg_dot = out_c = in_c = 0
    for m in bank.models:
        vi = m.vertex_indices()
        if not vi:
            continue
        cen = [sum(bank.vertices[i][k] for i in vi) / len(vi) for k in range(3)]
        for f in m.faces():
            if f.nrm is None or not f.drawn:
                continue
            g = _tri_normal(*(bank.vertices[i] for i in f.idx[:3]))
            nsum = [sum(bank.normals[k][j] for k in f.nrm) for j in range(3)]
            d = g[0] * nsum[0] + g[1] * nsum[1] + g[2] * nsum[2]
            if d > 0:
                pos_dot += 1
            elif d < 0:
                neg_dot += 1
            fc = [sum(bank.vertices[i][k] for i in f.idx) / 4.0 - cen[k] for k in range(3)]
            e = fc[0] * nsum[0] + fc[1] * nsum[1] + fc[2] * nsum[2]
            if e > 0:
                out_c += 1
            elif e < 0:
                in_c += 1
    st["winding_vs_normal"] = (pos_dot, neg_dot)
    st["normal_vs_centroid"] = (out_c, in_c)
    # strip order check: for a true quad the strip triangles (v0 v1 v2) and
    # (v1 v3 v2) face the same way; read as a fan (v0 v1 v2)/(v0 v2 v3) they would not
    strip = fan = 0
    for f in faces:
        if len(set(f.idx)) < 4:
            continue
        p = [bank.vertices[i] for i in f.idx]
        n1 = _tri_normal(p[0], p[1], p[2])
        n2 = _tri_normal(p[1], p[3], p[2])
        d = n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2]
        if d > 0:
            strip += 1
        elif d < 0:
            fan += 1
    st["quad_strip_vs_fan"] = (strip, fan)
    return st


def _tri_normal(a, b, c):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)


def model_bbox(bank, m):
    vi = m.vertex_indices()
    if not vi:
        return None
    xs, ys, zs = ([bank.vertices[i][k] for i in vi] for k in range(3))
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def model_geometry(bank, m):
    """Index-free description of a model (positions, uv, clut, tpage, rgb,
    bias, normal vectors per face) for cross-bank comparison."""
    out = []
    for f in m.faces():
        out.append((f.opcode, tuple(bank.vertices[i][:3] for i in f.idx), f.uv, f.clut,
                    f.tpage, f.rgb, f.bias,
                    None if f.nrm is None else tuple(bank.normals[i][:3] for i in f.nrm)))
    return out


_RIVAL_PART = ("near body (DrawCar otDepth < 0xD00, clut += lod[1])",
               "part +1 (drawn twice before the body)",
               "part +2 (drawn twice, mirrored about axis 0)",
               "part +3",
               "far body (0xD00 <= otDepth < 0x2500, clut += lod[1])")
_SCENERY = ("DrawPathScenery body (needs count >= 0x24)",
            "DrawPathScenery part rotated by g_SceneTimer (needs count >= 0x25)",
            "DrawRouteScenery (needs count >= 0x26)")


def model_role(bank, index):
    """Role of model `index`, from the C callers (draw_player_car_model.c,
    draw_path_scenery.c, draw_route_scenery.c, draw_flyby_scenery.c)."""
    if bank.label == "bank1":
        first = bank.count - 3
        if index >= first:
            role = "scenery: " + _SCENERY[index - first]
            if index != 0x23 + (index - first):
                role += " - unreachable at index %d, drawers fall back to model 1" % index
            return role
        return "rival base %d %s" % (index - index % 5, _RIVAL_PART[index % 5])
    if bank.label == "bank2":
        return "DrawFlybyScenery" if index == 0 else "unused"
    if bank.label == "car":
        if index == 0:
            return "player body"
        if index == 1:
            return "player part 1 (drawn twice before the body)"
        k = index - 2
        return "player wheel set: renderDepth*2%s = %d, part %s" % (
            " + 10" if k >= 10 else "", k % 10 - k % 2, "+2" if k % 2 == 0 else "+3")
    return "%s model %d" % (bank.label, index)


# ---------------------------------------------------------------- OBJ

def to_blender(v, scale=1.0):
    """PS1 (x, y down, z) -> Blender Z-up: X = x, Y = z, Z = -y (raw units)."""
    return (v[0] * scale, v[2] * scale, -v[1] * scale)


def face_loop(f):
    """Strip order v0 v1 / v2 v3 -> polygon loop v0 v1 v3 v2 (corner numbers),
    with repeated vertices dropped (idx2 == idx3 or idx1 == idx3 both give the
    triangle v0 v1 v2)."""
    loop = []
    for k in (0, 1, 3, 2):
        if not loop or f.idx[k] != f.idx[loop[-1]]:
            loop.append(k)
    if len(loop) > 1 and f.idx[loop[0]] == f.idx[loop[-1]]:
        loop.pop()
    return loop


def material_name(f):
    if f.textured:
        return "tp%04X_cl%04X" % (f.tpage, f.clut)
    return "%s_%02X%02X%02X" % (f.kind, f.rgb[0], f.rgb[1], f.rgb[2])


def write_obj(banks, path, scale=1.0, flip=False, stats=None):
    """One 'o' per model (<bank label>_model_NN, or model_NN when one bank is
    written), each with only the vertices it references; vt = u/256, 1 - v/256
    inside the face's texture page; usemtl per tpage/clut or flat colour.
    Faces the engine never draws (NCLIP == 0, see Face.drawn) are left out;
    their number is written as a comment and returned in stats['undrawn'].
    Default loop is v0 v1 v3 v2; the stored lighting normals point the other
    way, so flip=True reverses every loop to get outward Blender normals."""
    path = Path(path)
    mtl_path = path.with_suffix(".mtl")
    mats = {}
    vbase = tbase = 1
    undrawn = written = 0
    body = []
    for bank in banks:
        for m in bank.models:
            name = ("model_%02d" % m.index) if len(banks) == 1 else ("%s_model_%02d" % (bank.label, m.index))
            faces = list(m.faces())
            drawn = [f for f in faces if f.drawn]
            undrawn += len(faces) - len(drawn)
            vi = list(dict.fromkeys(i for f in drawn for i in f.idx))
            if not vi:
                continue
            local = {g: k for k, g in enumerate(vi)}
            body.append("o %s" % name)
            body.append("# %s; %d faces, %d never drawn" % (model_role(bank, m.index), len(faces),
                                                            len(faces) - len(drawn)))
            for g in vi:
                body.append("v %g %g %g" % to_blender(bank.vertices[g], scale))
            cur = None
            for f in drawn:
                loop = face_loop(f)
                if flip:
                    loop = loop[::-1]
                mat = material_name(f)
                mats.setdefault(mat, f)
                if mat != cur:
                    body.append("usemtl %s" % mat)
                    cur = mat
                if f.textured:
                    for k in loop:
                        u, v = f.uv[k]
                        body.append("vt %.6f %.6f" % (u / 256.0, 1.0 - v / 256.0))
                    body.append("f " + " ".join("%d/%d" % (vbase + local[f.idx[k]], tbase + j)
                                                for j, k in enumerate(loop)))
                    tbase += len(loop)
                else:
                    body.append("f " + " ".join("%d" % (vbase + local[f.idx[k]]) for k in loop))
                written += 1
            vbase += len(vi)
    head = ["# Rage Racer model banks, raw PS1 units, X=x Y=z Z=-y, loops %s" %
            ("reversed (outward)" if flip else "v0 v1 v3 v2"),
            "# %d faces written, %d never-drawn faces (repeat among v0..v2) left out" % (written, undrawn),
            "mtllib %s" % mtl_path.name]
    path.write_text("\n".join(head + body) + "\n")
    mtl = []
    for mat, f in sorted(mats.items()):
        kd = (0.8, 0.8, 0.8) if f.textured else tuple(c / 255.0 for c in f.rgb)
        mtl += ["newmtl %s" % mat, "Kd %.4f %.4f %.4f" % kd, ""]
    mtl_path.write_text("\n".join(mtl))
    if stats is not None:
        stats.update(written=written, undrawn=undrawn)
    return path


# ---------------------------------------------------------------- CLI

def fmt_counter(c, n=6, hexkeys=False):
    items = c.most_common(n)
    if hexkeys:
        return ", ".join("%s:%d" % ("%04X" % k if isinstance(k, int) else k, v) for k, v in items)
    return ", ".join("%s:%d" % (k, v) for k, v in items)


def fmt_patterns(c):
    return ", ".join("%s:%d" % (PATTERN_NAME.get(k, k), v) for k, v in sorted(c.items()))


def print_bank(bank, verbose=False):
    st = bank_stats(bank)
    print("[%s] base=0x%X size=%d models=%d vertices=%d normals=%d sha1=%s" % (
        bank.label, bank.base, bank.end - bank.base, st["models"], st["vertices"], st["normals"], bank.digest))
    print("  faces %s  drawn tris=%d never-drawn(NCLIP=0)=%d" % (
        dict(st["faces"]), st["tris"], st["never_drawn"]))
    print("  index patterns %s" % fmt_patterns(st["patterns"]))
    print("  codes %s" % {"%s:%02X" % k: v for k, v in st["codes"].items()})
    print("  tpages(%d) %s" % (len(st["tpages"]), fmt_counter(st["tpages"], 8, True)))
    print("  cluts(%d) %s" % (len(st["cluts"]), fmt_counter(st["cluts"], 6, True)))
    print("  bias %s" % fmt_counter(st["bias"], 8))
    print("  spare bytes %s" % fmt_counter(st["spare"], 4))
    print("  vertex pad %s  normal pad %s  |n| %.1f..%.1f  (-1,-1,-1) normals at %s" % (
        fmt_counter(st["vertex_pad"], 3), fmt_counter(st["normal_pad"], 3), *st["normal_len"],
        st["null_normals"] or "none"))
    print("  contiguous-slice models %d/%d  shared vertex refs %d  quads strip/fan %d/%d" % (
        st["models_contiguous"], bank.count, st["shared_vertex_refs"], *st["quad_strip_vs_fan"]))
    print("  stored normal vs right-hand winding +%d/-%d  vs model-centroid out/in %d/%d" % (
        *st["winding_vs_normal"], *st["normal_vs_centroid"]))
    if verbose:
        for m in bank.models:
            vi = m.vertex_indices()
            print("    model %02d @0x%05X cmds=%s nv=%d v[%d..%d] bbox=%s  %s" % (
                m.index, m.off - bank.base, "+".join("%s*%d" % (KIND[c.opcode], c.count) for c in m.commands),
                len(vi), min(vi) if vi else -1, max(vi) if vi else -1, model_bbox(bank, m),
                model_role(bank, m.index)))
    errs = check_bank(bank)
    print("  checks: %s" % ("OK" if not errs else "%d failures: %s" % (len(errs), "; ".join(errs[:20]))))
    return errs


def _accumulate(totals, bank, st):
    lab = bank.label
    for k, v in st["faces"].items():
        totals[(lab, "faces", k)] += v
    for k, v in st["codes"].items():
        totals[(lab, "code", "%s:%02X" % k)] += v
    for k, v in st["patterns"].items():
        totals[(lab, "pattern", PATTERN_NAME.get(k, k))] += v
    totals[(lab, "tris")] += st["tris"]
    totals[(lab, "never_drawn")] += st["never_drawn"]
    totals[(lab, "vertex_pad_nonzero")] += sum(v for k, v in st["vertex_pad"].items() if k)
    totals[(lab, "vertices")] += st["vertices"]
    totals[(lab, "normal_pad_nonzero")] += sum(v for k, v in st["normal_pad"].items() if k)
    totals[(lab, "normals")] += st["normals"]
    totals[(lab, "null_normals")] += len(st["null_normals"])
    totals[(lab, "spare_nonzero_faces")] += sum(v for k, v in st["spare"].items() if any(k))
    totals[(lab, "winding_vs_normal+")] += st["winding_vs_normal"][0]
    totals[(lab, "winding_vs_normal-")] += st["winding_vs_normal"][1]
    totals[(lab, "normal_vs_centroid_out")] += st["normal_vs_centroid"][0]
    totals[(lab, "normal_vs_centroid_in")] += st["normal_vs_centroid"][1]
    totals[(lab, "models_noncontiguous")] += bank.count - st["models_contiguous"]
    totals[(lab, "quads_strip_consistent")] += st["quad_strip_vs_fan"][0]
    totals[(lab, "quads_fan_consistent")] += st["quad_strip_vs_fan"][1]
    totals[(lab, "bias_min")] = min(totals.get((lab, "bias_min"), 0), min(st["bias"]))
    totals[(lab, "bias_max")] = max(totals.get((lab, "bias_max"), 0), max(st["bias"]))
    lo, hi = st["normal_len"]
    totals[(lab, "unit_len_x100_min")] = min(totals.get((lab, "unit_len_x100_min"), 10 ** 9), int(lo * 100))
    totals[(lab, "unit_len_x100_max")] = max(totals.get((lab, "unit_len_x100_max"), 0), int(hi * 100))
    for tp in st["tpages"]:
        totals[(lab, "tpage", "%04X" % tp)] += 1


def _parse_checked(a, data, idx, failures):
    out = []
    for lab, b, e in locate_banks(data, idx):
        try:
            bank = parse_bank(data, b, e, lab, a)
        except BankError as ex:
            failures.append("%03d %s: %s" % (a, lab, ex))
            continue
        for x in check_bank(bank):
            failures.append("%03d %s: %s" % (a, lab, x))
        out.append(bank)
    return out


def _partition(items):
    """[(asset, key)] -> the grouping of the assets by key, as a frozenset of frozensets."""
    g = defaultdict(set)
    for a, k in items:
        g[k].add(a)
    return frozenset(frozenset(v) for v in g.values())


def _check_sharing(scene_banks, failures):
    """bank digest partition == the documented class / course-slot partition."""
    for lab, override, which in (("bank1", BANK1_OVERRIDE, 0), ("bank2", BANK2_OVERRIDE, 1)):
        by_digest = _partition([(a, b.digest) for a, banks in scene_banks.items()
                                for b in banks if b.label == lab])
        expect = _partition([(a, override.get(a, course_class_slot(a)[which]))
                             for a in scene_banks])
        if by_digest != expect:
            failures.append("%s sharing differs from the class/slot rule with overrides %s" % (lab, override))


def _check_blocks(failures):
    rows = []
    for (a, b), want in sorted(EXPECTED_SAME_BLOCKS.items()):
        _, da = load_asset(str(a))
        _, db = load_asset(str(b))
        ba, bb = scene_blocks(da), scene_blocks(db)
        same = {i for i in range(11) if da[ba[i][0]:ba[i][1]] == db[bb[i][0]:bb[i][1]]}
        rows.append("%03d vs %03d: identical blocks %s" % (a, b, sorted(same)))
        if same != want:
            failures.append("%03d vs %03d identical blocks %s, expected %s" % (a, b, sorted(same), sorted(want)))
    return rows


def _check_scenery(scene_banks, failures):
    ra, ri = SCENERY_REF
    ref = [b for b in scene_banks[ra] if b.label == "bank1"][0]
    refg = [model_geometry(ref, ref.models[ri + j]) for j in range(3)]
    at = Counter()
    for a, banks in scene_banks.items():
        b1 = [b for b in banks if b.label == "bank1"][0]
        cls = course_class_slot(a)[0]
        want = 23 if cls == 5 else 38
        if b1.count != want:
            failures.append("%03d bank1 has %d models, expected %d" % (a, b1.count, want))
        first = b1.count - 3
        for j in range(3):
            if model_geometry(b1, b1.models[first + j]) != refg[j]:
                failures.append("%03d bank1 model %d differs from 088 model %d" % (a, first + j, ri + j))
        at[(first, b1.count)] += 1
        b2 = [b for b in banks if b.label == "bank2"][0]
        if b2.count != 1:
            failures.append("%03d bank2 has %d models" % (a, b2.count))
    return ", ".join("models %d..%d of %d in %d packs" % (f, f + 2, n, k) for (f, n), k in sorted(at.items()))


def run_all():
    failures = []
    totals = Counter()
    kinds = Counter()
    groups = defaultdict(list)           # (label, digest) -> [asset]
    scene_banks = {}
    nulls = set()
    rows = []
    for a in COURSE_PACKS:
        idx, data = load_asset(str(a))
        if not is_scene_pack(data):
            kinds["1ST image pack"] += 1
            if not is_image_pack(data):
                failures.append("%03d: neither scene nor image pack" % a)
            if a % 2 == 0:
                failures.append("%03d: image pack at an even (2ND) index" % a)
            continue
        kinds["2ND scene pack"] += 1
        if a % 2 == 1:
            failures.append("%03d: scene pack at an odd (1ST) index" % a)
        banks = _parse_checked(a, data, idx, failures)
        scene_banks[a] = banks
        for bank in banks:
            _accumulate(totals, bank, bank_stats(bank))
            groups[(bank.label, bank.digest)].append(a)
            nulls.update((a, bank.label, i) for i in null_normals(bank))
        rows.append("%03d %s" % (a, " ".join("%s=%dm/%dv/%dn" % (
            b.label, b.count, len(b.vertices), len(b.normals)) for b in banks)))
    if len(scene_banks) != 24 or any(len(v) != 2 for v in scene_banks.values()):
        failures.append("expected 24 scene packs with 2 banks each")
    else:
        _check_sharing(scene_banks, failures)
        scenery = _check_scenery(scene_banks, failures)
    block_rows = _check_blocks(failures)
    orow = []
    for a in list(CAR_PACKS) + [CAR_SELECT, OPTION_SCREEN]:
        idx, data = load_asset(str(a))
        banks = _parse_checked(a, data, idx, failures)
        for bank in banks:
            _accumulate(totals, bank, bank_stats(bank))
            nulls.update((a, bank.label, i) for i in null_normals(bank))
            if bank.label == "car" and u32(data, 0x18) != bank.end - bank.base:
                failures.append("%03d: +0x18 = 0x%X, bank size 0x%X" % (a, u32(data, 0x18), bank.end - bank.base))
            if bank.label == "car" and u32(data, 0x1C) != len(data) - u32(data, 0x24):
                failures.append("%03d: +0x1C = 0x%X, image size 0x%X" % (a, u32(data, 0x1C), len(data) - u32(data, 0x24)))
            if bank.label == "car" and u32(data, 0x20) != 0x28:
                failures.append("%03d: modelData offset 0x%X != 0x28" % (a, u32(data, 0x20)))
            if bank.label == "car" and bank.count != 22:
                failures.append("%03d: car bank has %d models" % (a, bank.count))
            orow.append("%03d=%dm/%dv/%dn" % (a, bank.count, len(bank.vertices), len(bank.normals)))
    if nulls != EXPECTED_NULL_NORMALS:
        failures.append("(-1,-1,-1) normals at %s, expected %s" % (
            sorted(nulls ^ EXPECTED_NULL_NORMALS), "the documented list"))
    # odd car packs (11..73) carry the car spec (race_assets.c:61), no bank
    for a in range(11, 74, 2):
        _, data = load_asset(str(a))
        if is_scene_pack(data):
            failures.append("%03d: odd car pack unexpectedly has a scene header" % a)
    print("course packs 087..134: %s" % dict(kinds))
    for i in range(0, len(rows), 3):
        print("  " + " | ".join(rows[i:i + 3]))
    print("identical banks across course packs (asset: class/slot):")
    for (lab, dg), assets in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[1][0])):
        print("  %s %s: %s" % (lab, dg, " ".join("%03d:%d/%d" % ((x,) + course_class_slot(x)) for x in assets)))
    print("  rule: bank1 = class, except 094 -> class 4 and 102 -> class 2; bank2 = course slot, except 094 -> slot 0")
    for r in block_rows:
        print("  " + r)
    if len(scene_banks) == 24:
        print("scenery models (same geometry as 088 35..37): %s" % scenery)
    print("(-1,-1,-1) normals: %s" % ", ".join("%03d %s #%d" % t for t in sorted(nulls)))
    print("car packs 010..072 (even), 008 car-select, 009 option:")
    for i in range(0, len(orow), 9):
        print("  " + " ".join(orow[i:i + 9]))
    print("totals:")
    for k in sorted(totals, key=str):
        print("  %s = %d" % ("/".join(map(str, k)), totals[k]))
    for f in failures[:40]:
        print("FAIL " + f)
    print("ALL CHECKS %s (%d failures)" % ("PASSED" if not failures else "FAILED", len(failures)))
    return len(failures)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rage Racer PAL model-bank decoder")
    ap.add_argument("asset", nargs="?", default="asset_088")
    ap.add_argument("--all", action="store_true", help="assert over all course/car/menu packs")
    ap.add_argument("--obj", help="write a Wavefront OBJ (+ .mtl) of the bank models")
    ap.add_argument("--bank", type=int, help="only bank N (1-based) of the asset")
    ap.add_argument("--flip", action="store_true", help="reverse loops (outward Blender normals)")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.all:
        return 1 if run_all() else 0
    idx, data = load_asset(args.asset)
    spans = locate_banks(data, idx)
    if not spans:
        print("asset %s holds no model bank (%s)" % (
            args.asset, "1ST image pack" if is_image_pack(data) else "unknown layout"))
        return 0
    banks = [parse_bank(data, b, e, lab, idx) for lab, b, e in spans]
    if args.bank:
        banks = [banks[args.bank - 1]]
    errs = 0
    for bank in banks:
        errs += len(print_bank(bank, args.verbose))
    if args.obj:
        st = {}
        p = write_obj(banks, args.obj, args.scale, args.flip, st)
        print("wrote %s (+ %s): %d faces, %d never-drawn faces left out" % (
            p, p.with_suffix(".mtl").name, st["written"], st["undrawn"]))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
