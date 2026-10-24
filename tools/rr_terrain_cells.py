#!/usr/bin/env python3
"""
Rage Racer (PAL PS1) course pack, block 7: terrain cells.

Usage (stdlib only):
  python -I tools/rr_terrain_cells.py [asset_088.bin | 88]          summary of one pack
  python -I tools/rr_terrain_cells.py 88 --obj out.obj [--local]     also write a Wavefront OBJ
  python -I tools/rr_terrain_cells.py --all                          assert over all 48 packs

Importable (no side effects on import):
  load_pack(arg) -> (path, bytes)
  parse_terrain_cells(data, name="") -> TerrainCells   (raises FormatError on any violation)
  cell_origin(sx, sy) -> (x, y, z) in vertex units
  validate(tc, data=None) -> dict of statistics (raises FormatError on violation)
  write_obj(tc, path, world=True)

Layout (evidence: InstallTerrainCellData in src/main/PAL/main/asset/model_banks.c,
SubmitTerrainCells / SubmitTerrainCellFaces / func_80028120_mode_0..3 /
EmitSubdividedTerrainQuad / SubmitTerrainCellFacesFar in
src/main/PAL/main/render/terrain_submission.s, BuildVisibleCells in
src/main/PAL/main/track/visible_cells.c). Offsets below are relative to block 7.

  +0x0000 u16 grid[32][32]   lo10 = cell id (0x3FF empty) of world cell (sx=col, sy=31-row);
                             hi6  = region id, read unflipped as grid[cy*32+cx] >> 10
  +0x0800 u32 vis[32][32]    vis[sy][sx] bit r: cell visible while camera region == r
  +0x1800 header: s32 cellCount; s32 poolOff; s32 cellOff[cellCount]  (relative to +0x1800)
          cell stream at header+cellOff[i]: { u32 cmd (lo16 opcode 0..5, hi16 count); faces }* u32 0
          vertex pool at header+poolOff: 8-byte {s16 x, s16 y, s16 z, u16 w}, runs to block end
  face (opcode 0,2,3: 0x20 bytes; opcode 1,4,5: 0x24 bytes):
     +00 u16 i0, +02 u16 i1, +04 u16 i2, +06 u16 i3   pool indices, strip order (v0 v1 / v2 v3)
     +08 u8 u0, u8 v0, u16 clut     +0C u8 u1, u8 v1, u16 tpage
     +10 u8 u2, u8 v2, u8 u3, u8 v3
     +14 u8 flags (bit0 halve uv when OTZ >= 0x800, bit1 skipped by the far path)
     +15 s8 OT bias   +16 u8 subdivision log2 along v0->v1   +17 u8 along v0->v2
     +18 u32 GP0 polyline word for the crack-fill outline (bit31 set = none)
     +1C u32 GP0 colour+code word (code 0x2C POLY_FT4)
     +20 u32 GP0 E2 texture-window word (opcode 1,4,5 only)
"""
import struct
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "extracted" / "assets_raw"
PACK_IDS = range(87, 135)  # asset_087 .. asset_134
DEFAULT_PACK = 88

BLOCK_INDEX = 7
TRACK_POINT_BLOCK = 4
GRID_SIZE = 0x800            # TERRAIN_CELL_GRID_SIZE (include/game/track.h)
VIS_SIZE = 0x1000            # CELL_VISIBILITY_TABLE_SIZE
HEADER_OFFSET = GRID_SIZE + VIS_SIZE
EMPTY_CELL = 0x3FF

CELL_WORLD_SIZE = 2048       # BuildVisibleCells: sx << 11, camera / 2048
CELL_WORLD_CENTRE = 1024     # BuildVisibleCells: center = 1024
VERTEX_SCALE = 4             # BuildVisibleCells: translation vector << 2 before ApplyMatrixLV
CELL_SIZE_VU = CELL_WORLD_SIZE * VERTEX_SCALE   # 8192 vertex units per cell
NEAR_TRZ_LIMIT = 0xA000      # SubmitTerrainCells: TRZ < 0xA000 -> near path

# jtbl_8007D9F4 (asm/PAL/main/data/main/6BE64.data.s): entry -> (handler, stride, meaning)
JTBL_ENTRIES = {
    0: ("func_80028120_mode_0", 0x20, "DPCS fog, POLY_FT4"),
    1: ("func_80028120_mode_1", 0x20, "clut+1, no fog, POLY_FT4"),
    2: ("func_80028120_mode_2", 0x24, "DPCS fog, POLY_FT4 inside E2 texture window"),
    3: ("func_80028120_mode_3", 0x24, "clut+1, no fog, POLY_FT4 inside E2 texture window"),
}
MAX_OPCODE = 5   # opcode 6+ would index past jtbl_8007D9F4 into jtbl_8007DA14


class FormatError(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise FormatError(msg)


def opcode_entry(opcode, env_mode4=0):
    """SubmitTerrainCellFaces 0x80028120..0x80028138: op<2 -> op*2+mode4, else op-2."""
    return opcode * 2 + env_mode4 if opcode < 2 else opcode - 2


def opcode_stride(opcode):
    s0 = JTBL_ENTRIES[opcode_entry(opcode, 0)][1]
    s1 = JTBL_ENTRIES[opcode_entry(opcode, 1)][1]
    assert s0 == s1
    return s0


# jtbl_8007D9F4 entries 1 (mode_1) and 3 (mode_3) add 1 to the clut before
# writing the FT4 (terrain_submission.s:321-322 and 418-419, lui 1; addu t6) and
# the subdivided path keeps that adjusted word (602 sw $t6,0xC8 / 737 lhu 0xCA).
CLUT_PLUS_ONE_ENTRIES = (1, 3)


def clut_offset(opcode, env_mode4=0):
    """CLUT offset the handler applies: 1 for jtbl entries 1/3 (opcodes 3 and 5
    always, opcodes 0 and 1 when SCRATCH_ENV_MODE4 = g_IsEnvironmentMode4 is 1)."""
    return 1 if opcode_entry(opcode, env_mode4) in CLUT_PLUS_ONE_ENTRIES else 0


def effective_clut(face, env_mode4=0):
    """The CLUT the GPU actually samples for this face (stored clut + clut_offset)."""
    return (face.clut + clut_offset(face.opcode, env_mode4)) & 0xFFFF


def cell_origin(sx, sy):
    """World translation of grid cell (sx, sy) in vertex units (y is absolute)."""
    return ((sx * CELL_WORLD_SIZE + CELL_WORLD_CENTRE) * VERTEX_SCALE, 0,
            (sy * CELL_WORLD_SIZE + CELL_WORLD_CENTRE) * VERTEX_SCALE)


@dataclass
class TerrainFace:
    offset: int            # absolute offset in the pack
    opcode: int
    indices: tuple         # (i0, i1, i2, i3), strip order v0 v1 / v2 v3
    uv: tuple              # ((u0, v0), (u1, v1), (u2, v2), (u3, v3))
    clut: int
    tpage: int
    flags: int             # byte +0x14
    ot_bias: int           # s8 +0x15
    subdiv_01: int         # byte +0x16, log2 segments along v0->v1 (outer loop)
    subdiv_02: int         # byte +0x17, log2 segments along v0->v2 (inner loop)
    line_cmd: int          # u32 +0x18
    rgbc: int              # u32 +0x1C
    tex_window: object = None  # u32 +0x20 or None

    @property
    def code(self):
        return self.rgbc >> 24

    @property
    def rgb(self):
        return (self.rgbc & 0xFF, (self.rgbc >> 8) & 0xFF, (self.rgbc >> 16) & 0xFF)

    @property
    def semi_transparent(self):
        return bool(self.code & 2)

    @property
    def raw_texture(self):
        return bool(self.code & 1)

    @property
    def clut_xy(self):
        return ((self.clut & 0x3F) * 16, (self.clut >> 6) & 0x1FF)

    @property
    def tpage_xy(self):
        return ((self.tpage & 0xF) * 64, ((self.tpage >> 4) & 1) * 256)

    @property
    def tpage_bpp(self):
        return {0: 4, 1: 8, 2: 16, 3: 16}[(self.tpage >> 7) & 3]

    @property
    def tpage_abr(self):
        return (self.tpage >> 5) & 3

    @property
    def has_outline(self):
        return not (self.line_cmd & 0x80000000)

    @property
    def far_skip(self):
        return bool(self.flags & 2)

    @property
    def half_uv_far(self):
        return bool(self.flags & 1)

    @property
    def window(self):
        """Decoded E2 window in texels: (mask_x, mask_y, off_x, off_y) or None."""
        w = self.tex_window
        if w is None:
            return None
        return ((w & 0x1F) * 8, ((w >> 5) & 0x1F) * 8, ((w >> 10) & 0x1F) * 8, ((w >> 15) & 0x1F) * 8)


@dataclass
class TerrainCommand:
    offset: int
    opcode: int
    count: int
    faces: list = field(default_factory=list)


@dataclass
class TerrainCell:
    cell_id: int
    offset: int            # absolute offset of the command stream
    end: int               # absolute offset just past the 0 terminator
    commands: list = field(default_factory=list)
    grid_pos: object = None    # (sx, sy) world grid position, from grid[(31-sy)*32+sx]

    @property
    def faces(self):
        return [f for c in self.commands for f in c.faces]

    @property
    def origin(self):
        return cell_origin(*self.grid_pos) if self.grid_pos is not None else (0, 0, 0)


@dataclass
class TerrainCells:
    name: str
    block_start: int
    block_end: int
    grid: list             # 1024 u16, raw row-major as stored
    vis: list              # 32 rows of 32 u32
    cell_count: int
    pool_offset: int       # absolute
    vertices: list         # (x, y, z, w)
    cells: list

    def cell_id_at(self, sx, sy):
        v = self.grid[(31 - sy) * 32 + sx] & 0x3FF
        return None if v == EMPTY_CELL else v

    def region_at(self, cx, cy):
        return self.grid[cy * 32 + cx] >> 10

    def iter_faces(self):
        for c in self.cells:
            for cmd in c.commands:
                for f in cmd.faces:
                    yield c, f

    def world_vertex(self, cell, index):
        """Pool vertex index as used by cell, in world-placed vertex units (x, y, z)."""
        x, y, z, _w = self.vertices[index]
        ox, oy, oz = cell.origin
        return (x + ox, y + oy, z + oz)

    def face_world_quad(self, cell, face):
        """The four corners of face in strip order v0 v1 v2 v3, world-placed vertex units."""
        return [self.world_vertex(cell, i) for i in face.indices]


def resolve_pack(arg):
    if arg is None:
        arg = DEFAULT_PACK
    s = str(arg)
    if s.isdigit():
        return RAW_DIR / ("asset_%03d.bin" % int(s))
    p = Path(s)
    if not p.exists() and (RAW_DIR / s).exists():
        p = RAW_DIR / s
    return p


def load_pack(arg=None):
    p = resolve_pack(arg)
    return p, p.read_bytes()


def pack_offsets(data):
    return list(struct.unpack_from("<11i", data, 0))


def pack_kind(data):
    """'scene' = 11-offset GameSceneAssetHeader (first offset 0x2C), 'image' = the
    5-offset image pack (first offset 0x14), else 'unknown'."""
    first = struct.unpack_from("<i", data, 0)[0]
    n = first // 4 if first in (0x14, 0x2C) else 0
    if not n:
        return "unknown"
    offs = struct.unpack_from("<%di" % n, data, 0)
    if not all(offs[i] < offs[i + 1] for i in range(n - 1)) or offs[-1] >= len(data):
        return "unknown"
    return "scene" if n == 11 else "image"


def parse_terrain_cells(data, name=""):
    offs = pack_offsets(data)
    check(all(offs[i] < offs[i + 1] for i in range(10)) and offs[10] < len(data),
          "%s: header offsets not ascending" % name)
    b0 = offs[BLOCK_INDEX]
    b1 = offs[BLOCK_INDEX + 1]
    check(b1 - b0 > HEADER_OFFSET + 8, "%s: block 7 too small" % name)
    grid = list(struct.unpack_from("<1024H", data, b0))
    vis = [list(struct.unpack_from("<32I", data, b0 + GRID_SIZE + r * 0x80)) for r in range(32)]
    hdr = b0 + HEADER_OFFSET
    count, pool_rel = struct.unpack_from("<ii", data, hdr)
    check(0 < count < EMPTY_CELL, "%s: cellCount %d" % (name, count))
    cell_rel = struct.unpack_from("<%di" % count, data, hdr + 8)
    pool = hdr + pool_rel
    check(hdr + 8 + 4 * count <= pool <= b1, "%s: pool offset out of block" % name)
    check((b1 - pool) % 8 == 0, "%s: pool size %d not a multiple of 8" % (name, b1 - pool))
    nverts = (b1 - pool) // 8
    vertices = [struct.unpack_from("<hhhH", data, pool + 8 * i) for i in range(nverts)]

    # grid position of every cell id (game lookup: grid[(31 - sy) * 32 + sx] & 0x3FF)
    pos_of = {}
    for sy in range(32):
        for sx in range(32):
            cid = grid[(31 - sy) * 32 + sx] & 0x3FF
            if cid == EMPTY_CELL:
                continue
            check(cid < count, "%s: grid cell id %d >= count %d" % (name, cid, count))
            check(cid not in pos_of, "%s: cell id %d appears twice in the grid" % (name, cid))
            pos_of[cid] = (sx, sy)

    cells = []
    expect = hdr + 8 + 4 * count   # first stream must start right after cellOff[]
    for cid in range(count):
        o = hdr + cell_rel[cid]
        check(o == expect, "%s: cell %d starts at %#x, previous stream ended at %#x" % (name, cid, o, expect))
        cell = TerrainCell(cid, o, 0, grid_pos=pos_of.get(cid))
        while True:
            check(o + 4 <= pool, "%s: cell %d stream runs into the pool" % (name, cid))
            cmd = struct.unpack_from("<I", data, o)[0]
            o += 4
            if cmd == 0:
                break
            op, n = cmd & 0xFFFF, cmd >> 16
            check(op <= MAX_OPCODE, "%s: cell %d opcode %d at %#x" % (name, cid, op, o - 4))
            check(n > 0, "%s: cell %d zero-count command at %#x" % (name, cid, o - 4))
            stride = opcode_stride(op)
            tc = TerrainCommand(o - 4, op, n)
            check(o + n * stride <= pool, "%s: cell %d faces run into the pool" % (name, cid))
            for k in range(n):
                f = o + k * stride
                i0, i1, i2, i3 = struct.unpack_from("<4H", data, f)
                u0, v0, clut, u1, v1, tpage, u2, v2, u3, v3 = struct.unpack_from("<BBHBBHBBBB", data, f + 8)
                flags, bias, s01, s02 = struct.unpack_from("<BbBB", data, f + 0x14)
                line, rgbc = struct.unpack_from("<II", data, f + 0x18)
                win = struct.unpack_from("<I", data, f + 0x20)[0] if stride == 0x24 else None
                for idx in (i0, i1, i2, i3):
                    check(idx < nverts, "%s: cell %d face %#x index %d >= pool %d" % (name, cid, f, idx, nverts))
                tc.faces.append(TerrainFace(f, op, (i0, i1, i2, i3),
                                            ((u0, v0), (u1, v1), (u2, v2), (u3, v3)),
                                            clut, tpage, flags, bias, s01, s02, line, rgbc, win))
            o += n * stride
            cell.commands.append(tc)
        cell.end = o
        expect = o
        cells.append(cell)
    check(expect == pool, "%s: last stream ends at %#x, pool starts at %#x" % (name, expect, pool))
    return TerrainCells(name, b0, b1, grid, vis, count, pool, vertices, cells)


def track_points(data):
    """Block 4 GameTrackPoint {s32 x, s32 z, s16 y, ...} 0x18 bytes, s32 count first
    (include/game/track.h, InstallTrackPoints). World units. Used only as a check."""
    b4 = pack_offsets(data)[TRACK_POINT_BLOCK]
    n = struct.unpack_from("<i", data, b4)[0]
    return [struct.unpack_from("<iih", data, b4 + 4 + 0x18 * i) for i in range(n)]


def _border_matches(tc, lookup):
    """Exact world-space vertex coincidences between 4-neighbour cells placed by lookup."""
    vsets = {}
    for sy in range(32):
        for sx in range(32):
            cid = lookup(sx, sy)
            if cid is None:
                continue
            ox, _, oz = cell_origin(sx, sy)
            s = set()
            for f in tc.cells[cid].faces:
                for i in f.indices:
                    x, y, z, _w = tc.vertices[i]
                    s.add((x + ox, y, z + oz))
            vsets[(sx, sy)] = s
    pairs = shared = 0
    for (sx, sy), s in vsets.items():
        for nb in ((sx + 1, sy), (sx, sy + 1)):
            t = vsets.get(nb)
            if t is None:
                continue
            pairs += 1
            shared += len(s & t)
    return pairs, shared


def _height_at(tc, cell, px, pz):
    """Vertex-unit heights of every face of cell whose XZ projection holds (px, pz)."""
    hs = []
    V = tc.vertices
    for f in cell.faces:
        a, b, c, d = (V[i] for i in f.indices)
        for t in ((a, b, c), (b, d, c)):
            (x0, y0, z0, _), (x1, y1, z1, _), (x2, y2, z2, _) = t
            den = (z1 - z2) * (x0 - x2) + (x2 - x1) * (z0 - z2)
            if den == 0:
                continue
            l0 = ((z1 - z2) * (px - x2) + (x2 - x1) * (pz - z2)) / den
            l1 = ((z2 - z0) * (px - x2) + (x0 - x2) * (pz - z2)) / den
            l2 = 1.0 - l0 - l1
            if min(l0, l1, l2) >= -1e-6:
                hs.append(l0 * y0 + l1 * y1 + l2 * y2)
    return hs


def track_check(tc, data):
    """Each track point (world units) must fall in an occupied cell and on a face
    whose interpolated height equals 4 * y. Returns stats."""
    pts = track_points(data)
    in_cell = in_cell_raw = covered = close = 0
    diffs = []
    for x, z, y in pts:
        sx, sy = x // CELL_WORLD_SIZE, z // CELL_WORLD_SIZE
        if not (0 <= sx < 32 and 0 <= sy < 32):
            continue
        if (tc.grid[sy * 32 + sx] & 0x3FF) != EMPTY_CELL:
            in_cell_raw += 1
        cid = tc.cell_id_at(sx, sy)
        if cid is None:
            continue
        in_cell += 1
        ox, _, oz = cell_origin(sx, sy)
        hs = _height_at(tc, tc.cells[cid], x * VERTEX_SCALE - ox, z * VERTEX_SCALE - oz)
        if not hs:
            continue
        covered += 1
        dmin = min(abs(h - y * VERTEX_SCALE) for h in hs)
        diffs.append(dmin)
        if dmin <= 64:
            close += 1
    diffs.sort()
    med = diffs[len(diffs) // 2] if diffs else None
    return dict(points=len(pts), in_cell=in_cell, in_cell_raw=in_cell_raw, covered=covered,
                within64=close, median=med)


def validate(tc, data=None):
    """Consistency checks beyond the parse-time bounds asserts; returns stats."""
    st = {}
    nverts = len(tc.vertices)
    used = Counter()
    ops_cmd = Counter()
    ops_face = Counter()
    codes = Counter()
    linecodes = Counter()
    wincodes = Counter()
    flags = Counter()
    subdiv = Counter()
    bias = Counter()
    tpages = Counter()
    bpp = Counter()
    far_prefix_ok = far_prefix_bad = degenerate = cell_ranges_contig = outline_unsub = 0
    owner = {}
    shared_between_cells = 0
    for c in tc.cells:
        cidx = set()
        for cmd in c.commands:
            ops_cmd[cmd.opcode] += 1
            ops_face[cmd.opcode] += len(cmd.faces)
            seen_draw = False
            prefix_ok = True
            for f in cmd.faces:
                for i in f.indices:
                    used[i] += 1
                    cidx.add(i)
                codes[f.code] += 1
                if f.line_cmd & 0x80000000:
                    linecodes["off" if f.line_cmd == 0x80000000 else "off+%02x" % (f.line_cmd >> 24)] += 1
                else:
                    linecodes["%02x" % (f.line_cmd >> 24)] += 1
                    if f.subdiv_01 == 0 and f.subdiv_02 == 0:
                        outline_unsub += 1
                if f.tex_window is not None:
                    wincodes["%02x" % (f.tex_window >> 24)] += 1
                flags[f.flags] += 1
                subdiv[(f.subdiv_01, f.subdiv_02)] += 1
                bias[f.ot_bias] += 1
                tpages[f.tpage] += 1
                bpp[f.tpage_bpp] += 1
                if len(set(f.indices)) < 4:
                    degenerate += 1
                if f.far_skip:
                    if seen_draw:
                        prefix_ok = False
                else:
                    seen_draw = True
            if prefix_ok:
                far_prefix_ok += 1
            else:
                far_prefix_bad += 1
        for i in cidx:
            if i in owner:
                shared_between_cells += 1
            owner[i] = c.cell_id
        if cidx and max(cidx) - min(cidx) + 1 == len(cidx):
            cell_ranges_contig += 1
    # GPU command bytes must be what the handlers forward verbatim
    check(set(codes) <= {0x2C, 0x2D, 0x2E, 0x2F}, "%s: colour code bytes %s" % (tc.name, sorted(codes)))
    check(all(k.startswith("off") or k in ("48", "4a") for k in linecodes), "%s: outline words %s" % (tc.name, dict(linecodes)))
    check(set(wincodes) <= {"e2"}, "%s: texture window words %s" % (tc.name, dict(wincodes)))
    check(max(max(k) for k in subdiv) <= 5, "%s: subdivision > 5 overflows the 34-entry row buffer" % tc.name)
    placed = sum(1 for c in tc.cells if c.grid_pos is not None)
    check(placed == tc.cell_count, "%s: %d of %d cells not in the grid" % (tc.name, tc.cell_count - placed, tc.cell_count))
    unref = [i for i in range(nverts) if i not in used]
    w_all = Counter(v[3] for v in tc.vertices)
    w_used = Counter(tc.vertices[i][3] for i in used)
    xs = [tc.vertices[i][0] for i in used]
    ys = [tc.vertices[i][1] for i in used]
    zs = [tc.vertices[i][2] for i in used]
    half = CELL_SIZE_VU // 2
    inside = sum(1 for i in used if abs(tc.vertices[i][0]) <= half and abs(tc.vertices[i][2]) <= half)
    st.update(cells=tc.cell_count, verts=nverts, verts_used=len(used), verts_unref=len(unref),
              unref_first=unref[:5], cmds=dict(sorted(ops_cmd.items())), faces=dict(sorted(ops_face.items())),
              faces_total=sum(ops_face.values()), codes={"%02x" % k: v for k, v in sorted(codes.items())},
              outline=dict(linecodes), texwin=dict(wincodes), flags={"%02x" % k: v for k, v in sorted(flags.items())},
              subdiv=dict(sorted(subdiv.items())), ot_bias=(min(bias), max(bias)), tpages=len(tpages),
              bpp=dict(bpp), outline_unsub=outline_unsub, far_prefix_ok=far_prefix_ok, far_prefix_bad=far_prefix_bad, degenerate=degenerate,
              cell_ranges_contig=cell_ranges_contig, shared_between_cells=shared_between_cells,
              bbox=((min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs))), inside_half=inside,
              w_zero_all=w_all[0], w_zero_used=w_used[0], w_distinct=len(w_all), grid_occupied=placed)

    def raw(sx, sy):
        v = tc.grid[sy * 32 + sx] & 0x3FF
        return None if v == EMPTY_CELL else v

    def trans(sx, sy):
        v = tc.grid[sx * 32 + sy] & 0x3FF
        return None if v == EMPTY_CELL else v
    st["border_game"] = _border_matches(tc, tc.cell_id_at)
    st["border_unflipped"] = _border_matches(tc, raw)
    st["border_transposed"] = _border_matches(tc, trans)
    # visibility: a cell should be visible from the region of the cell the camera is in
    self_unflip = self_flip = occ = 0
    regions = set()
    for sy in range(32):
        for sx in range(32):
            if tc.cell_id_at(sx, sy) is None:
                continue
            occ += 1
            r = tc.region_at(sx, sy)
            regions.add(r)
            self_unflip += (tc.vis[sy][sx] >> r) & 1
            rf = tc.grid[(31 - sy) * 32 + sx] >> 10
            self_flip += (tc.vis[sy][sx] >> rf) & 1
    st["regions"] = len(regions)
    st["self_vis_region_unflipped"] = (self_unflip, occ)
    st["self_vis_region_flipped"] = (self_flip, occ)
    if data is not None:
        st["track"] = track_check(tc, data)
    return st


def write_obj(tc, path, world=True, env_mode4=0):
    """One 'o' per cell; raw vertex units; Blender X=x, Y=z, Z=-y; quads f v0 v1 v3 v2.
    world=True adds the cell translation from BuildVisibleCells (vertex units).
    Material names use the effective clut (effective_clut, env_mode4 0 or 1)."""
    out = ["# Rage Racer PAL terrain cells (block 7) of %s" % tc.name,
           "# units: raw PS1 vertex units (= world units * %d); cell pitch %d" % (VERTEX_SCALE, CELL_SIZE_VU),
           "# placement: %s" % ("world, cell origin ((sx*2048+1024)*4, 0, (sy*2048+1024)*4)" if world else "cell-relative"),
           "# axes: Blender X = x, Y = z, Z = -y; vt = (u/256, 1 - v/256) in tpage texels; usemtl tpXXXX_clYYYY"]
    vbase = 1
    tbase = 1
    for c in tc.cells:
        ox, oy, oz = c.origin if world else (0, 0, 0)
        sx, sy = c.grid_pos if c.grid_pos is not None else (-1, -1)
        out.append("o cell_%03d_x%02d_z%02d" % (c.cell_id, sx, sy))
        local = {}
        order = []
        for f in c.faces:
            for i in f.indices:
                if i not in local:
                    local[i] = vbase + len(order)
                    order.append(i)
        for i in order:
            x, y, z, _w = tc.vertices[i]
            out.append("v %d %d %d" % (x + ox, z + oz, -(y + oy)))
        cur_mtl = None
        nt = 0
        for f in c.faces:
            mtl = "tp%04x_cl%04x" % (f.tpage, effective_clut(f, env_mode4))
            if mtl != cur_mtl:
                out.append("usemtl " + mtl)
                cur_mtl = mtl
            corners = []
            for k in (0, 1, 3, 2):
                u, v = f.uv[k]
                out.append("vt %.6f %.6f" % (u / 256.0, 1.0 - v / 256.0))
                corners.append((local[f.indices[k]], tbase + nt))
                nt += 1
            poly = []
            for vi, ti in corners:
                if not poly or poly[-1][0] != vi:
                    poly.append((vi, ti))
            if len(poly) > 1 and poly[0][0] == poly[-1][0]:
                poly.pop()
            if len(poly) >= 3 and len(set(p[0] for p in poly)) == len(poly):
                out.append("f " + " ".join("%d/%d" % p for p in poly))
        vbase += len(order)
        tbase += nt
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(out) + "\n")


def print_summary(tc, st):
    print("%s: block 7 %#x..%#x (%d bytes), header %#x, pool %#x" % (
        tc.name, tc.block_start, tc.block_end, tc.block_end - tc.block_start,
        tc.block_start + HEADER_OFFSET, tc.pool_offset))
    print("  cells %d (all placed once in the 32x32 grid), regions %d" % (st["cells"], st["regions"]))
    print("  commands per opcode %s" % st["cmds"])
    print("  faces per opcode    %s  total %d (degenerate %d)" % (st["faces"], st["faces_total"], st["degenerate"]))
    print("  vertex pool %d (used %d, unreferenced %d, shared between cells %d); 4th u16 zero: %d of all, %d of used, %d distinct" % (
        st["verts"], st["verts_used"], st["verts_unref"], st["shared_between_cells"],
        st["w_zero_all"], st["w_zero_used"], st["w_distinct"]))
    (x0, x1), (y0, y1), (z0, z1) = st["bbox"]
    print("  used vertex bbox x %d..%d y %d..%d z %d..%d; inside +-%d in x,z: %d" % (
        x0, x1, y0, y1, z0, z1, CELL_SIZE_VU // 2, st["inside_half"]))
    print("  colour codes %s  outline words %s (enabled on unsubdivided faces: %d)  texwin %s" % (
        st["codes"], st["outline"], st["outline_unsub"], st["texwin"]))
    print("  flags %s  OT bias %d..%d  tpages %d bpp %s" % (st["flags"], st["ot_bias"][0], st["ot_bias"][1], st["tpages"], st["bpp"]))
    print("  subdiv (v0v1, v0v2) %s" % st["subdiv"])
    print("  far-skip faces form a prefix of their command: %d ok, %d not" % (st["far_prefix_ok"], st["far_prefix_bad"]))
    print("  cells whose indices are one contiguous pool range: %d of %d" % (st["cell_ranges_contig"], st["cells"]))
    print("  border vertex matches (pairs, shared): game %s, unflipped %s, transposed %s" % (
        st["border_game"], st["border_unflipped"], st["border_transposed"]))
    print("  own cell visible from own region: unflipped-region %s, flipped-region %s" % (
        st["self_vis_region_unflipped"], st["self_vis_region_flipped"]))
    if "track" in st:
        t = st["track"]
        print("  track points %d: in occupied cell %d (raw grid %d), on a face %d, |h - 4y| <= 64: %d, median %s" % (
            t["points"], t["in_cell"], t["in_cell_raw"], t["covered"], t["within64"],
            None if t["median"] is None else round(t["median"], 1)))


def run_all():
    tot = Counter()
    fails = 0
    print("pack  cells  faces  op0/op1/op2/op3/op4/op5          verts  unref  border game/unfl  farbad  track in/on/<=64")
    for pid in PACK_IDS:
        p = RAW_DIR / ("asset_%03d.bin" % pid)
        try:
            data = p.read_bytes()
            kind = pack_kind(data)
            # LoadTrackDataAssets loads 0x58 + class*8 + course*2 (even) as the scene pack;
            # race_assets.c loads 0x57 + ... (odd) and only UploadImageAsset's its blocks.
            check(kind == ("scene" if pid % 2 == 0 else "image"), "%s: pack kind %s at %s index" % (
                p.name, kind, "even" if pid % 2 == 0 else "odd"))
            if kind == "image":
                tot["image_packs"] += 1
                print("%03d  image (1ST) pack, 5-offset header, no block 7 - not applicable" % pid)
                continue
            tc = parse_terrain_cells(data, p.name)
            st = validate(tc, data)
            bg, bu = st["border_game"][1], st["border_unflipped"][1]
            check(bg > bu, "%s: game grid orientation does not beat unflipped (%d vs %d)" % (p.name, bg, bu))
            t = st["track"]
            check(t["in_cell"] == t["points"], "%s: %d track points outside occupied cells" % (p.name, t["points"] - t["in_cell"]))
        except FormatError as e:
            fails += 1
            print("%03d  FAIL %s" % (pid, e))
            continue
        f = st["faces"]
        print("%03d  %5d  %5d  %-30s %6d  %5d  %6d/%-6d  %5d  %d/%d/%d" % (
            pid, st["cells"], st["faces_total"], "/".join(str(f.get(k, 0)) for k in range(6)),
            st["verts"], st["verts_unref"], bg, bu, st["far_prefix_bad"],
            t["in_cell"], t["covered"], t["within64"]))
        tot["packs"] += 1
        for k in ("cells", "faces_total", "verts", "verts_unref", "far_prefix_bad", "degenerate"):
            tot[k] += st[k]
        tot["track_pts"] += t["points"]
        tot["track_on"] += t["covered"]
        tot["track_close"] += t["within64"]
    print("OK %d scene packs decoded, %d image packs classified, %d failures; totals: cells %d faces %d verts %d "
          "unreferenced %d degenerate %d far-prefix violations %d; "
          "track points %d, on a face %d, within 64 units %d" % (
              tot["packs"], tot["image_packs"], fails, tot["cells"], tot["faces_total"], tot["verts"], tot["verts_unref"],
              tot["degenerate"], tot["far_prefix_bad"], tot["track_pts"], tot["track_on"], tot["track_close"]))
    return fails


def main(argv):
    args = list(argv[1:])
    if "--all" in args:
        return 1 if run_all() else 0
    obj = None
    world = "--local" not in args
    if "--obj" in args:
        i = args.index("--obj")
        obj = args[i + 1]
        del args[i:i + 2]
    args = [a for a in args if not a.startswith("--")]
    path, data = load_pack(args[0] if args else None)
    tc = parse_terrain_cells(data, path.name)
    st = validate(tc, data)
    print_summary(tc, st)
    if obj:
        write_obj(tc, obj, world)
        print("  wrote %s (%s placement)" % (obj, "world" if world else "cell-relative"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
