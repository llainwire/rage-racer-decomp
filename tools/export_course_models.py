#!/usr/bin/env python3
"""
Export the course model table (GameSceneAssetHeader block 5) of every Rage
Racer PAL track pack as real quad meshes in Wavefront OBJ form.

Format (confirmed from src/main/PAL/main/render/terrain_submission.s):

  block 5 = s32 modelCount, then modelCount entries of 12 bytes:
      s32 geometry   offset (relative to block start) of the vertex array
      s32 vertexCount
      s32 model      offset (relative to block start) of the command stream

  vertex   = 8 bytes: s16 x, s16 y, s16 z, u16 extra     (TransformCourseModel
             runs RTPT over them three at a time, 0x80029F58)
  stream   = u32 command words until a zero word; lo16 = opcode, hi16 = count
             (SubmitCourseModel 0x800296C0 / SubmitCourseModel2 0x80029E5C),
             followed by `count` face records whose size depends on opcode:
      opcode 0  POLY_F4  flat quad            16 bytes  (EmitCoursePolyF4 / ..Fog)
      opcode 1  POLY_FT4 textured quad        28 bytes  (EmitCoursePolyFT4 / ..Fog)
      opcode 2  subdivided textured quad      32 bytes  (SubmitCourseSubdividedFaces)
      opcode 3  subdivided, UV-scrolling      32 bytes  (func_8002A550)
  face     = +0x00 u16 idx0, u16 idx1       vertex indices into the geometry
             +0x04 u16 idx2, u16 idx3
             +0x08 u8 r, g, b, code          RGBC word copied into the primitive
             textured faces only:
             +0x0C u8 u0, v0, u16 clut
             +0x10 u8 u1, v1, u16 tpage
             +0x14 u8 u2, v2
             +0x16 u8 u3, v3
             +0x18 u8 ?, s8 otBias, ...      byte +0x19 is added to the OT index
             opcode 2/3 only:
             +0x1A u8 subdivX, u8 subdivY    subdivision levels (reduced by depth)
             +0x1C u32 texture window GPU command restored after the quad

PS1 quads are strip ordered (v0 v1 / v2 v3), so the OBJ face is v0 v1 v3 v2.
Axis mapping for Blender (Z up):  X = x,  Y = z,  Z = -y.
"""
import argparse
import hashlib
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "extracted" / "assets_raw"
OUT_DIR = ROOT / "extracted" / "course_models"

COURSE_NAMES = {0: "BIG_MythicalCoast", 1: "MID_OverPassCity", 2: "HI_LakesideGate", 3: "OVAL_ExtremeOval"}
STRIDES = {0: 0x10, 1: 0x1C, 2: 0x20, 3: 0x20}
OPCODE_NAMES = {0: "F4", 1: "FT4", 2: "SUBDIV_FT4", 3: "SUBDIV_FT4_SCROLL"}


def pack_name(asset_id):
    rel = asset_id - 87
    course = (rel >> 1) & 3
    cls = rel >> 3
    half = "1ST" if (rel & 1) == 0 else "2ND"
    return f"{COURSE_NAMES[course]}_class{cls}_{half}"


class Face:
    __slots__ = ("opcode", "idx", "rgb", "code", "uv", "clut", "tpage", "bias", "subdiv", "texwin", "raw")

    def __init__(self, opcode, raw):
        self.opcode = opcode
        self.raw = raw
        i0, i1, i2, i3 = struct.unpack_from("<4H", raw, 0)
        self.idx = (i0, i1, i2, i3)
        r, g, b, code = struct.unpack_from("<4B", raw, 8)
        self.rgb = (r, g, b)
        self.code = code
        self.uv = None
        self.clut = None
        self.tpage = None
        self.bias = 0
        self.subdiv = None
        self.texwin = None
        if opcode >= 1:
            u0, v0, clut, u1, v1, tpage, u2, v2, u3, v3 = struct.unpack_from("<BBHBBHBBBB", raw, 0x0C)
            self.uv = ((u0, v0), (u1, v1), (u2, v2), (u3, v3))
            self.clut = clut
            self.tpage = tpage
            self.bias = struct.unpack_from("<b", raw, 0x19)[0]
        if opcode >= 2:
            self.subdiv = struct.unpack_from("<BB", raw, 0x1A)
            self.texwin = struct.unpack_from("<I", raw, 0x1C)[0]


class Model:
    def __init__(self, asset_id, index, geom_off, vcount, model_off):
        self.asset_id = asset_id
        self.index = index
        self.geom_off = geom_off
        self.vcount = vcount
        self.model_off = model_off
        self.verts = []
        self.faces = []
        self.stream_end = None
        self.problems = []

    @property
    def name(self):
        return f"asset_{self.asset_id:03d}_model_{self.index:02d}"

    def bbox(self):
        if not self.verts:
            return None
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        zs = [v[2] for v in self.verts]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def parse_pack(asset_id, data):
    offsets = struct.unpack_from("<11i", data, 0)
    start, end = offsets[5], offsets[6]
    if not (0 < start < end <= len(data)):
        raise ValueError(f"asset {asset_id}: bad course table span {start:#x}..{end:#x}")
    table = data[start:end]
    count = struct.unpack_from("<i", table, 0)[0]
    if not (0 < count < 1024) or 4 + count * 12 > len(table):
        raise ValueError(f"asset {asset_id}: implausible model count {count}")
    models = []
    for i in range(count):
        g, n, m = struct.unpack_from("<iii", table, 4 + i * 12)
        models.append(Model(asset_id, i, g, n, m))

    for mdl in models:
        if mdl.vcount <= 0 or mdl.geom_off < 0 or mdl.geom_off + mdl.vcount * 8 > len(table):
            mdl.problems.append(f"geometry span out of block: off={mdl.geom_off:#x} n={mdl.vcount}")
            continue
        for j in range(mdl.vcount):
            x, y, z, extra = struct.unpack_from("<hhhH", table, mdl.geom_off + j * 8)
            mdl.verts.append((x, y, z, extra))

        pos = mdl.model_off
        if pos < 0 or pos + 4 > len(table):
            mdl.problems.append(f"model offset out of block: {pos:#x}")
            continue
        while True:
            if pos + 4 > len(table):
                mdl.problems.append("stream ran off the end of the block without a terminator")
                break
            cmd = struct.unpack_from("<I", table, pos)[0]
            pos += 4
            if cmd == 0:
                mdl.stream_end = pos
                break
            opcode, n = cmd & 0xFFFF, cmd >> 16
            if opcode not in STRIDES:
                mdl.problems.append(f"unknown opcode {opcode:#x} at {pos - 4:#x}")
                break
            stride = STRIDES[opcode]
            if pos + n * stride > len(table):
                mdl.problems.append(f"opcode {opcode} x{n} overruns block at {pos:#x}")
                break
            for k in range(n):
                face = Face(opcode, table[pos + k * stride: pos + (k + 1) * stride])
                if max(face.idx) >= mdl.vcount:
                    mdl.problems.append(
                        f"face {len(mdl.faces)} (op {opcode}) index {max(face.idx)} >= vertexCount {mdl.vcount}")
                mdl.faces.append(face)
            pos += n * stride
    digest = hashlib.md5(table).hexdigest()[:12]
    return models, table, digest


def check_layout(models, table_len):
    """Every model's stream should end exactly where the next structure starts."""
    notes = []
    starts = sorted({m.geom_off for m in models} | {m.model_off for m in models} | {table_len})
    for m in models:
        if m.stream_end is None:
            continue
        nxt = min((s for s in starts if s > m.model_off), default=table_len)
        if m.stream_end != nxt:
            notes.append(f"{m.name}: stream ends at {m.stream_end:#x}, next structure at {nxt:#x}")
    return notes


def to_blender(v):
    x, y, z = v[0], v[1], v[2]
    return (x, z, -y)


class ObjWriter:
    def __init__(self, path, scale=1.0):
        self.path = Path(path)
        self.mtl_path = self.path.with_suffix(".mtl")
        self.scale = scale
        self.f = None
        self.materials = {}
        self.v_base = 1
        self.vt_base = 1

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = self.path.open("w", newline="\n")
        self.f.write("# Rage Racer (PAL) course models, exported by tools/export_course_models.py\n")
        self.f.write("# Axis mapping: Blender X = PS1 x, Blender Y = PS1 z, Blender Z = -PS1 y\n")
        self.f.write(f"mtllib {self.mtl_path.name}\n")
        return self

    def __exit__(self, *exc):
        self.f.close()
        with self.mtl_path.open("w", newline="\n") as m:
            for name, (r, g, b, textured) in self.materials.items():
                m.write(f"newmtl {name}\n")
                m.write(f"Kd {r / 255:.4f} {g / 255:.4f} {b / 255:.4f}\n")
                m.write("Ka 0 0 0\nKs 0 0 0\nd 1\nillum 1\n\n")

    def material_for(self, face):
        if face.uv is None:
            name = f"flat_{face.rgb[0]:02X}{face.rgb[1]:02X}{face.rgb[2]:02X}"
            self.materials.setdefault(name, (*face.rgb, False))
        else:
            name = f"tpage_{face.tpage:04X}_clut_{face.clut:04X}"
            self.materials.setdefault(name, (*face.rgb, True))
        return name

    def write_model(self, mdl, name=None, offset=(0, 0, 0)):
        f = self.f
        f.write(f"\no {name or mdl.name}\n")
        for v in mdl.verts:
            bx, by, bz = to_blender(v)
            f.write(f"v {(bx + offset[0]) * self.scale:.4f} {(by + offset[1]) * self.scale:.4f} {(bz + offset[2]) * self.scale:.4f}\n")
        vt_count = 0
        for face in mdl.faces:
            if face.uv is not None:
                for (u, v) in face.uv:
                    f.write(f"vt {u / 256:.6f} {1 - v / 256:.6f}\n")
                vt_count += 4
        cur_mtl = None
        vt = self.vt_base
        for face in mdl.faces:
            if max(face.idx) >= mdl.vcount:
                continue
            mtl = self.material_for(face)
            if mtl != cur_mtl:
                f.write(f"usemtl {mtl}\n")
                cur_mtl = mtl
            a, b, c, d = (self.v_base + i for i in face.idx)
            if face.uv is None:
                f.write(f"f {a} {b} {d} {c}\n")
            else:
                f.write(f"f {a}/{vt} {b}/{vt + 1} {d}/{vt + 3} {c}/{vt + 2}\n")
                vt += 4
        self.v_base += len(mdl.verts)
        self.vt_base += vt_count


def grid_offset(index, bboxes, spacing):
    """Place model `index` in a square grid so models do not overlap."""
    cols = max(1, int(len(bboxes) ** 0.5 + 0.999))
    row, col = divmod(index, cols)
    return (col * spacing, row * spacing, 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("assets", nargs="*", type=int, help="asset ids (default 87..134)")
    ap.add_argument("--out", default=str(OUT_DIR), help="output directory")
    ap.add_argument("--scale", type=float, default=1.0, help="multiply coordinates (default 1.0 = raw PS1 units)")
    ap.add_argument("--grid", action="store_true",
                    help="also write *_grid.obj with models laid out on a grid instead of their stored positions")
    ap.add_argument("--no-obj", action="store_true", help="only validate and print statistics")
    args = ap.parse_args()

    ids = args.assets or list(range(87, 135))
    out_dir = Path(args.out)
    all_models = []
    digests = {}
    opcode_totals = Counter()
    extra_values = Counter()
    problems = []
    layout_notes = []

    for asset_id in ids:
        path = ASSETS / f"asset_{asset_id:03d}.bin"
        if not path.exists():
            print(f"missing {path}")
            continue
        data = path.read_bytes()
        try:
            models, table, digest = parse_pack(asset_id, data)
        except ValueError as e:
            print(e)
            continue
        digests.setdefault(digest, []).append(asset_id)
        layout_notes += check_layout(models, len(table))
        n_faces = Counter()
        for m in models:
            for fc in m.faces:
                n_faces[fc.opcode] += 1
            for v in m.verts:
                extra_values[v[3]] += 1
            for p in m.problems:
                problems.append(f"{m.name}: {p}")
        opcode_totals.update(n_faces)
        all_models += models
        vtotal = sum(m.vcount for m in models)
        print(f"asset_{asset_id:03d} {pack_name(asset_id):34s} models={len(models):3d} verts={vtotal:5d} "
              f"faces={sum(n_faces.values()):5d} by opcode={dict(sorted(n_faces.items()))} table_md5={digest}")

        if not args.no_obj:
            with ObjWriter(out_dir / f"asset_{asset_id:03d}_{pack_name(asset_id)}.obj", args.scale) as w:
                for m in models:
                    w.write_model(m)
            if args.grid:
                bboxes = [m.bbox() for m in models]
                spans = [max(b[1][i] - b[0][i] for i in range(3)) for b in bboxes if b]
                spacing = (max(spans) if spans else 1000) * 1.2
                with ObjWriter(out_dir / f"asset_{asset_id:03d}_{pack_name(asset_id)}_grid.obj", args.scale) as w:
                    for i, m in enumerate(models):
                        b = m.bbox()
                        if not b:
                            continue
                        cx = (b[0][0] + b[1][0]) / 2
                        cy = (b[0][2] + b[1][2]) / 2  # PS1 z -> Blender Y
                        cz = -(b[0][1] + b[1][1]) / 2  # PS1 y -> Blender Z
                        gx, gy, gz = grid_offset(i, bboxes, spacing)
                        w.write_model(m, offset=(gx - cx, gy - cy, gz - cz))

    if not args.no_obj and len(ids) > 1:
        with ObjWriter(out_dir / "all_course_models.obj", args.scale) as w:
            for m in all_models:
                w.write_model(m)

    print()
    print(f"models: {len(all_models)}   faces by opcode: "
          + ", ".join(f"{OPCODE_NAMES[k]}={v}" for k, v in sorted(opcode_totals.items())))
    print(f"distinct vertex 'extra' u16 values: {len(extra_values)}; most common: {extra_values.most_common(6)}")
    print(f"unique course model tables (by md5): {len(digests)}")
    for d, members in digests.items():
        print(f"  {d}: assets {members}")
    if layout_notes:
        print(f"\nstreams not ending at the next structure ({len(layout_notes)}):")
        for n in layout_notes[:20]:
            print("  " + n)
    if problems:
        print(f"\nPROBLEMS ({len(problems)}):")
        for p in problems[:40]:
            print("  " + p)
        if len(problems) > 40:
            print(f"  ... {len(problems) - 40} more")
        return 1
    print("\nall vertex indices in range, all streams terminated: OK")
    if not args.no_obj:
        print(f"OBJ files written under {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
