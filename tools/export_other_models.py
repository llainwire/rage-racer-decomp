#!/usr/bin/env python3
"""
Export every Rage Racer (PAL, SCES-006.50) model that is neither a course
level nor a car: the car-select pack SELECT.BIN (asset 8: model bank 0xE and
its course-model table) and the option-screen pack OPTION.BIN (asset 9: model
bank 0).  Also scans the EXE and the other disc files for model data and
writes extracted/models/asset_index.txt, which lists every RAGE.BIN asset
0..134 and every disc file with what it holds and where its models went.

Everything is parsed by the verified decoders, imported, never forked:
    rr_assets.py           asset inventory + full model walk (asserts on every face)
    rr_model_banks.py      ModelBankHeader parser, check_bank, Face.drawn / face_loop
    export_course_models.py  independent course-model-table parser (cross-check)
    rr_textures.py         VRAM replay (Vram), tpage / clut decode, PNG writer

Output (raw PS1 units, Blender X = x, Y = z, Z = -y):
    extracted/models/other/<group>/<name>/<name>.obj, <name>.mtl, textures/*.png
    extracted/models/other/other_models.json        per-leaf provenance and counts
    extracted/models/previews/other_*.png            contact sheets (3/4 view)
    extracted/models/asset_index.txt
Faces: the PS1 strip v0 v1 / v2 v3 is the polygon cycle v0 v1 v3 v2.  The game
draws a face only when NCLIP(v0, v1, v2) > 0 (terrain_submission.s:1066-1075
for model banks, 2274-2286 for course models), i.e. clockwise on screen; the
cycle v0 v1 v3 v2 then has its right-hand (Blender) normal pointing away from
the viewer.  So each face is written as that cycle reversed, f v0 v2 v3 v1
(the same loop as export_car_models.written_loop and export_course_scenes),
which makes Blender's normals face the side the game draws (asserted against
the stored 4.12 normals of every G4/GT4 face).  --strip-loop writes the
literal f v0 v1 v3 v2 instead.  Faces the game never draws (a repeated index
among v0..v2, NCLIP == 0) are left out and counted.
UVs: vt = (u/256, 1 - v/256), the convention of the sibling exporters, so a
face spans the texel edges u0..u1 and closest sampling reads texels u0..u1-1,
as the PS1 rasteriser does (it never reaches the last column/row).  An axis on
which all of a face's corners share one u (or one v) reads a single texel
column (row) on the GPU; there u/256 would sit on a texel border, so that axis
is moved to the texel centre (+0.5), exactly export_car_models.uv_centring.
918 of 1282 OPTION.BIN faces (and 62 character faces) have such an axis
(single-texel colour swatches).

Importable, no side effects on import:
    model_sets()                 -> [ModelSet] (all leaves with faces incl. tpage/clut/uv)
    menu_vram(asset)             -> rr_textures.Vram for the screen that draws asset 8 / 9
    scan_model_structures(data)  -> [(kind, offset)] model-bank / course-table headers
    rage_str_census(track01)     -> Counter of RAGE.STR sector kinds
    export_all(out_root, ...)    -> report dict (runs every check)

CLI:
    python -I tools/export_other_models.py              # export + checks + index + previews
    python -I tools/export_other_models.py --no-previews --no-scan
    python -I tools/export_other_models.py --strip-loop  # literal f v0 v1 v3 v2
    python -I tools/export_other_models.py --index-only  # rewrite asset_index.txt only (after the
                                                         # course / car exporters have run)
    ... --require-complete    # fail unless every model-bearing asset has an export
asset_index.txt finds the sibling exports from cars/cars_manifest.json,
courses/<course>/<class>/manifest.json and the header comments of every OBJ
outside other/ (asset_NNN, assetNNN or 'asset NN'); both sources must agree.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import struct
import sys
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import export_course_models as ECM  # noqa: E402
import rr_assets as RA  # noqa: E402
import rr_model_banks as MB  # noqa: E402
import rr_textures as RT  # noqa: E402

REPO = TOOLS.parent
OUT_ROOT = REPO / "extracted" / "models"

ASSET_SELECT, ASSET_OPTION = 8, 9
BOOT_IMAGE_ASSETS = (0, 1, 5)     # InitAssetSystem: LOGO.TMS; LoadBootAssets: TITLE.TMS, CAR.TMS

# g_SaveNameCharset (asm/PAL/main/data/main/002FCC_main.rodata.s:573-618): maps a
# team-name grid cell to ASCII (memory_card_menu.c:32).  The cell is what the
# TEAM NAME screen stores (design_screens.c:242 g_TeamNameChars[n] = GameMenuCursor)
# and what DrawTeamNameCharModel submits (swap_car_model_slot.c:390-398).
G_SAVE_NAME_CHARSET = 0x80012FD0
EXPECTED_CHARSET = "0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ.-!?@"
CHAR_WORDS = {" ": "space", ".": "period", "-": "hyphen", "!": "exclamation", "?": "question", "@": "at"}
G_TEAM_LOGO_FADED_CLUT_RECT = 0x8007F95C   # 6BE64.data.s:9573
TEAM_NAME_TEXTURE_RECT = (0x282, 0x37, 0xC, 8)  # team_name_texture.c:4-24 (clear + <=6 glyphs)

# EXE-resident graphics data that is not a 3D model (listed in the index).
EXE_NON_MODEL_DATA = [
    ("g_TeamNameFontGlyphs", 0x8007C7CC, "4bpp 8x8 glyphs LoadImage'd to (0x288-len, 0x37) for the team-name "
     "texture (team_name_texture.c:13-24); 2D font, not a model"),
    ("g_TeamNameBlankTile", 0x8007D24C, "4bpp blank tile for the same rect (team_name_texture.c:4-11)"),
    ("g_CourseCardVerts", 0x80011A48, "4 SVECs rotated by ApplyMatrixSV and drawn as a screen-space "
     "GameDrawTexturedQuad (menu_mode.c:147-239, FlipCourseCard); 2D card flip, no GTE projection"),
    ("g_SkyTileMap / g_SkyTileUV", 0x8007F470, "sky panel tile map and UVs; DrawSkyBackground "
     "(track/draw_terrain_cells.c:49-804) builds screen-space panels from the camera angles and submits "
     "no model"),
    ("g_SaveNameCharset", G_SAVE_NAME_CHARSET, "44 bytes: the team-name cell -> ASCII table used to name "
     "the SELECT.BIN character models"),
]


# Preview views (azimuth from Blender -Y towards +X, elevation), per model kind.
# Bank models: front and rear three-quarter views (the pad models turn with
# g_ControllerSceneAngleY, controller_setup_scene.c:55/89/116; the dioramas and
# platform are seen from varying menu angles).  Course-table characters: the
# letters are flat (PS1 z ~ 1) and DrawTeamNameCharModel turns them by
# RotY(0x800 - angle) with target angle 0 (swap_car_model_slot.c:384,
# design_screens.c:209/219, design_mode.c:166), so the game shows their +z
# side = Blender +Y: view them from behind.
# Views are labelled by camera azimuth: for the controllers az215 is the
# button face, az35 the back.
PREVIEW_VIEWS = {"bank": (("az35", 35.0, 28.0), ("az215", 215.0, 28.0)),
                 "course_table": (("drawn_side", 205.0, 18.0),)}

# Output groups (other/<group>/), named after the screen whose C draw site
# submits the models (all names below are decomp function names; the leaf
# keeps the bank / table index).
GROUP_COURSE_VIEWS = "SELECT_BIN_menu_course_views"       # DrawMenuCourseView (swap_car_model_slot.c:283-291)
GROUP_CAR_VIEW = "SELECT_BIN_menu_car_view"               # DrawMenuCarView (swap_car_model_slot.c:179-189)
GROUP_TEAM_NAME = "SELECT_BIN_team_name_characters"       # DrawTeamNameCharModel (swap_car_model_slot.c:384-398)
GROUP_CONTROLLERS = "OPTION_BIN_controller_setup"         # DrawControllerSetupScene (controller_setup_scene.c:24-140)


class ExportError(Exception):
    pass


def require(cond, msg):
    if not cond:
        raise ExportError(msg)


# ---------------------------------------------------------------- EXE helpers
def exe_bytes():
    return RA.EXE_PATH.read_bytes()


def exe_off(addr):
    return addr - RA.EXE_BASE + RA.EXE_HEADER


def save_name_charset(exe=None):
    exe = exe or exe_bytes()
    raw = exe[exe_off(G_SAVE_NAME_CHARSET):exe_off(G_SAVE_NAME_CHARSET) + 44]
    require(raw[42:] == b"\0\0", "g_SaveNameCharset is not 42 chars + 2 zero bytes: %r" % raw)
    s = raw[:42].decode("ascii")
    require(s == EXPECTED_CHARSET, "g_SaveNameCharset = %r" % s)
    return s


def safe(s):
    out = "".join(c if c.isalnum() else "_" for c in s)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def group_stem(asset):
    path = RA.asset_name(asset)[0]          # g_AssetPaths, e.g. \..\PACK\SELECT.BIN
    return safe(path.replace("/", chr(92)).split(chr(92))[-1])


# ---------------------------------------------------------------- model sets
@dataclass
class Corner:
    v: int                      # index into Leaf.vertices
    uv: tuple = None            # (u, v) texel coordinates in the tpage
    n: tuple = None             # Blender-space unit normal or None


@dataclass
class OutFace:
    corners: list               # in written order
    material: str
    kind: str                   # F4 / G4 / FT4 / GT4
    tpage: int = None
    clut: int = None
    rgb: tuple = None
    uv_quad: tuple = None       # the 4 stored uv pairs (strip order), for coverage checks
    stored_normal: tuple = None  # mean stored normal (Blender space) or None


@dataclass
class Leaf:
    group: str
    name: str
    asset: int
    source: str                 # "bank" or "course_table"
    index: int
    role: str
    evidence: list
    vertices: list = field(default_factory=list)    # PS1 (x, y, z)
    faces: list = field(default_factory=list)       # [OutFace]
    undrawn: int = 0
    stored_faces: int = 0
    drawn_in_game: bool = True
    has_normals: bool = False


@dataclass
class ModelSet:
    group: str
    asset: int
    kind: str                   # "bank" / "course_table"
    offset: int
    leaves: list
    load_evidence: str
    image_evidence: str


def to_blender(p):
    return (float(p[0]), float(p[2]), float(-p[1]))


def _norm(v):
    L = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return None if L == 0 else (v[0] / L, v[1] / L, v[2] / L)


def newell(points):
    nx = ny = nz = 0.0
    k = len(points)
    for i in range(k):
        x0, y0, z0 = points[i]
        x1, y1, z1 = points[(i + 1) % k]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    return (nx, ny, nz)


class _IdxOnly:
    __slots__ = ("idx",)

    def __init__(self, idx):
        self.idx = idx


def written_loop(idx, strip_loop=False):
    """Corner numbers in the order written: MB.face_loop gives v0 v1 v3 v2 with
    repeated corners dropped; reversed starting at v0 (v0 v2 v3 v1, as
    export_car_models.written_loop) unless strip_loop (see module doc)."""
    loop = MB.face_loop(_IdxOnly(tuple(idx)))
    return loop if strip_loop else [loop[0]] + loop[:0:-1]


def written_uvs(uvs):
    """Texel-space (u, v) written for a textured face's corners (vt = u/256,
    1 - v/256).  Per axis: extent >= 2 keeps the stored value (the viewer reads
    texels min..max-1 inside the face, as the GPU, which never reaches the
    right / bottom edge); extent 0 or 1 means the GPU reads the single texel
    min there, so every corner gets min + 0.5, that texel's centre (at u/256
    both edges of a 1-texel strip sit on texel borders and edge samples flip
    to the neighbour).  Extent 0 and >= 2 are exactly
    export_car_models.uv_centring; extent 1 is the same reasoning carried one
    texel further."""
    out = [list(uv) for uv in uvs]
    for ax in (0, 1):
        vals = [uv[ax] for uv in uvs]
        lo, hi = min(vals), max(vals)
        if hi - lo <= 1:
            for o in out:
                o[ax] = lo + 0.5
    return [tuple(o) for o in out]


def material_for(kind, tpage, clut, rgb):
    if tpage is not None:
        return "tp%04X_cl%04X" % (tpage, clut)
    return "%s_%02X%02X%02X" % (kind, rgb[0], rgb[1], rgb[2])


def _bank_roles(asset, count):
    """Role + evidence of each model, from the C draw sites (all PAL code is C
    except terrain_submission.s, so these call sites are exhaustive)."""
    names = RA.exe_tables()["course_names"]
    out = {}
    if asset == ASSET_SELECT:
        for k in range(4):
            out[k] = ("model%02d_%s" % (k, safe(names[k])),
                      "course diorama drawn by DrawMenuCourseView when g_MenuCourseModelIndex = %d (%s)" % (k, names[k]),
                      ["swap_car_model_slot.c:283-291 SelectModelBank(14); SubmitModel(g_MenuCourseModelIndex & 3)",
                       "course_select.c:689, menu_mode.c:387: g_MenuCourseModelIndex = g_CourseIndex (also set at "
                       "course_select.c:480/511, car_select.c:603, swap_car_model_slot.c:224/236)",
                       "name: g_CourseNames[%d] (EXE 0x8007D404)" % k],
                      GROUP_COURSE_VIEWS)
        out[4] = ("model04", "no draw site found (a flat 16-face disc with the same radius 524 and CLUT 7E47 as "
                  "model 5's top; grouped with it)",
                  ["SelectModelBank(14) is followed only by SubmitModel(count>=6 ? 5 : 1) "
                   "(swap_car_model_slot.c:179-189) and SubmitModel(index & 3) (283-291)"],
                  GROUP_CAR_VIEW)
        out[5] = ("model05_car_platform",
                  "platform under the player car in the car-select view: submitted right after DrawPlayerCarModel at "
                  "the car's position and render height, with the car's matrix and the OT base pushed 120 back "
                  "(drawn behind the car); 524-radius disc, top at the car's y, 94 units deep",
                  ["swap_car_model_slot.c:170-189 DrawMenuCarView: DrawPlayerCarModel; SelectModelBank(14); "
                   "g_ScratchOrderingTableBaseWord += 120; SetGteObjectMatrix(car position, y = s2 + 30); "
                   "SubmitModel(g_ModelBankCount >= 6 ? 5 : 1)"],
                  GROUP_CAR_VIEW)
    elif asset == ASSET_OPTION:
        out[0] = ("model00_CONTROLLER", "controller shown on the pad setup screen when g_PadType == 0x41",
                  ["controller_setup_scene.c:53-67: g_PadType == 0x41 -> SubmitModel(g_ModelBankCount < 1) = 0",
                   "name: EXE string 'INSERT CONTROLLER' (main.exe file offset 0x800)"],
                  GROUP_CONTROLLERS)
        nm = ["name: EXE string 'Hold the \"NeGcon\" in an untwisted ...' (main.exe file offset 0x828); "
              "g_PadType 0x23"]
        side = ("left/right from the texture: model 1 carries the D-pad and START (the NeGcon's left half), "
                "model 2 the A, B, I and II buttons (its right half); model 1 spans PS1 x 0..4188, model 2 -4188..0, "
                "the button face is the +z side")
        twist = ["drawn only by DrawControllerSetupScene(1): negcon_calibration.c:101 (game mode 10, EXE string "
                 "'Steer play.') and :179 (mode 11, 'Maximum twist.'); controller_config.c:127/212 pass 0",
                 "one flat red GT4 triangle at the top of the twist joint (PS1 y -1730)"]
        out[1] = ("model01_NeGcon_left_half", "NeGcon left half, rotated by angle + twist",
                  ["controller_setup_scene.c:71-100: g_PadType == 0x23 -> SubmitModel(1)", side] + nm,
                  GROUP_CONTROLLERS)
        out[2] = ("model02_NeGcon_right_half", "NeGcon right half, rotated by angle - twist",
                  ["controller_setup_scene.c:113-131: SubmitModel(g_ModelBankCount >= 3 ? 2 : 1)", side] + nm,
                  GROUP_CONTROLLERS)
        out[3] = ("model03_NeGcon_left_half_twist_marker",
                  "marker drawn with the left half's (model 1) twist matrix on the steer-play / max-twist screens",
                  ["controller_setup_scene.c:101-109: variant != 0 -> SubmitModel(count >= 4 ? 3 : 1)"] + twist + nm,
                  GROUP_CONTROLLERS)
        out[4] = ("model04_NeGcon_right_half_twist_marker",
                  "marker drawn with the right half's (model 2) twist matrix on the steer-play / max-twist screens",
                  ["controller_setup_scene.c:132-140: variant != 0 -> SubmitModel(count >= 5 ? 4 : 1)"] + twist + nm,
                  GROUP_CONTROLLERS)
    require(sorted(out) == list(range(count)), "asset %d: roles for %s, bank has %d models" % (asset, sorted(out), count))
    return out


def _check_bank_role_geometry(asset, bank):
    """The geometric facts the names above rest on (side of each NeGcon half,
    twist markers on the matching side, platform below the car origin)."""
    def bbox(m):
        pts = [bank.vertices[i] for f in m.faces() if f.drawn for i in f.idx]
        return [min(p[k] for p in pts) for k in range(3)] + [max(p[k] for p in pts) for k in range(3)]
    if asset == ASSET_OPTION:
        b = {m.index: bbox(m) for m in bank.models}
        require(b[1][0] >= 0 and b[2][3] <= 0, "NeGcon halves are not split at x = 0: %s %s" % (b[1], b[2]))
        require(b[3][0] + b[3][3] > 0 > b[4][0] + b[4][3], "twist markers not on the side of their half")
        for k in (3, 4):
            require(len([f for f in bank.models[k].faces() if f.drawn]) == 1 and b[k][1] == b[k][4],
                    "model %d is not a single flat face" % k)
    elif asset == ASSET_SELECT:
        b = bbox(bank.models[5])
        require(b[1] == 0 and b[4] > 0 and b[0] == -b[3] and b[2] == -b[5],
                "SELECT model 5 is not a disc centred on the origin hanging below y = 0: %s" % b)


def _check_bank_cross(asset, bank, ra_bank):
    """rr_model_banks and rr_assets must decode the same faces."""
    ra_models = ra_bank["model_list"]
    require(len(ra_models) == bank.count, "asset %d: model count differs between decoders" % asset)
    for m, rm in zip(bank.models, ra_models):
        fa, fb = list(m.faces()), rm["faces"]
        require(len(fa) == len(fb), "asset %d model %d: %d vs %d faces" % (asset, m.index, len(fa), len(fb)))
        for a, b in zip(fa, fb):
            require(a.idx == tuple(b["idx"]) and a.uv == b["uv"] and a.clut == b["clut"] and a.tpage == b["tpage"]
                    and (a.rgb == b["rgb"]) and a.nrm == (tuple(b["normals"]) if b["normals"] else None),
                    "asset %d model %d face @%X: decoders disagree" % (asset, m.index, a.off))
    require([v for v in bank.vertices] == [tuple(v) for v in ra_bank["vertex_list"]],
            "asset %d: vertex pools differ" % asset)


def _bank_set(asset, strip_loop):
    blocks = RA.asset_models(asset)             # raises FormatError on any walk error
    ra_bank = [b for b in blocks if b["kind"] == "model_bank"]
    require(len(ra_bank) == 1, "asset %d: %d model banks" % (asset, len(ra_bank)))
    ra_bank = ra_bank[0]
    banks = MB.banks_for_asset(asset)
    require(len(banks) == 1, "asset %d: rr_model_banks found %d banks" % (asset, len(banks)))
    bank = banks[0]
    errs = MB.check_bank(bank)
    require(not errs, "asset %d check_bank: %s" % (asset, errs[:3]))
    _check_bank_cross(asset, bank, ra_bank)
    roles = _bank_roles(asset, bank.count)
    _check_bank_role_geometry(asset, bank)
    slot = 0xE if asset == ASSET_SELECT else 0
    set_label = "%s_bank%02X" % (group_stem(asset), slot)      # stats / log label only
    leaves = []
    for m in bank.models:
        name, role, ev, group = roles[m.index]
        faces = list(m.faces())
        lf = Leaf(group, name, asset, "bank", m.index, role, ev, stored_faces=len(faces))
        lf.drawn_in_game = role != "no draw site found"
        remap = {}
        for f in faces:
            if not f.drawn:
                lf.undrawn += 1
                continue
            loop = written_loop(f.idx, strip_loop)
            pts = [to_blender(bank.vertices[f.idx[k]]) for k in loop]
            geo = _norm(newell(pts))
            stored = None
            corners = []
            for k in loop:
                g = f.idx[k]
                if g not in remap:
                    remap[g] = len(lf.vertices)
                    lf.vertices.append(bank.vertices[g][:3])
                n = None
                if f.nrm is not None:
                    raw = bank.normals[f.nrm[k]][:3]
                    n = None if raw == MB.NULL_NORMAL else _norm(to_blender(raw))
                corners.append(Corner(remap[g], f.uv[k] if f.textured else None, n))
            if f.nrm is not None:
                ns = [c.n for c in corners if c.n is not None]
                if ns:
                    stored = _norm(tuple(sum(n[i] for n in ns) for i in range(3)))
                for c in corners:
                    if c.n is None:        # (-1,-1,-1) placeholder: use the face's own normal
                        c.n = geo
                lf.has_normals = True
            else:
                for c in corners:
                    c.n = geo
            lf.faces.append(OutFace(corners, material_for(f.kind, f.tpage, f.clut, f.rgb), f.kind, f.tpage, f.clut,
                                    f.rgb, f.uv, stored))
        leaves.append(lf)
    if asset == ASSET_SELECT:
        load = "car_assets.c:55-56 LoadAsset(8); RegisterModelBank(cursor + 0xC, 0xE)"
        img = "car_assets.c:70 UploadImageAsset(SELECT.BIN + offsets[2])"
    else:
        load = "menu_car_assets.c:79-81 LoadAsset(9); RegisterModelBank(&asset->modelBank, 0); SelectModelBank(0)"
        img = "menu_car_assets.c:86 g_ImageBlockBuffer = base + imageOffset; boot_scene_tail.c:28 UploadImageAsset"
    return ModelSet(set_label, asset, "bank", bank.base, leaves, load, img)


def _course_table_cross(d, cmt):
    """export_course_models.parse_pack on a synthesized 11-offset header must
    decode the same entries as rr_assets.parse_course_model_table."""
    o, size = cmt["offset"], cmt["size"]
    block = d[o:o + size]
    fake = struct.pack("<11i", 0, 0, 0, 0, 0, 0x2C, 0x2C + size, 0x2C + size, 0, 0, 0) + block
    models, table, _ = ECM.parse_pack(ASSET_SELECT, fake)
    require(all(not m.problems for m in models), "export_course_models problems: %s" %
            [m.problems for m in models if m.problems][:2])
    require(ECM.check_layout(models, len(table)) == [], "export_course_models layout notes")
    ents = cmt["entry_list"]
    require(len(models) == len(ents), "course table: %d vs %d entries" % (len(models), len(ents)))
    for m, e in zip(models, ents):
        require(m.vcount == e["vertex_count"] and [tuple(v) for v in m.verts] == [tuple(v) for v in e["vertices"]],
                "course model %d: vertices differ" % m.index)
        require(len(m.faces) == len(e["faces"]), "course model %d: face counts differ" % m.index)
        for a, b in zip(m.faces, e["faces"]):
            require(a.idx == tuple(b["idx"]) and a.uv == b["uv"] and a.clut == b["clut"] and a.tpage == b["tpage"]
                    and a.rgb == b["rgb"] and a.code == b["code"] and a.bias == b["ot_bias"],
                    "course model %d face @%X: decoders disagree" % (m.index, b["offset"]))


def _course_table_set(strip_loop):
    d = RA.load_asset(ASSET_SELECT)
    blocks = RA.asset_models(ASSET_SELECT)
    cmt = [b for b in blocks if b["kind"] == "course_model_table"]
    require(len(cmt) == 1, "SELECT.BIN: %d course model tables" % len(cmt))
    cmt = cmt[0]
    _course_table_cross(d, cmt)
    charset = save_name_charset()
    ents = cmt["entry_list"]
    require(len(ents) == len(charset), "SELECT.BIN course table has %d models, charset %d" % (len(ents), len(charset)))
    group = GROUP_TEAM_NAME
    leaves = []
    for e in ents:
        ch = charset[e["id"]]
        word = CHAR_WORDS.get(ch, ch)
        name = "char%02d_%s" % (e["id"], word)
        drawn = e["id"] != 10
        role = "team-name character %r (grid cell %d)" % (ch, e["id"])
        if not drawn:
            role += "; never drawn: DrawTeamNameCharModel skips cell 10"
        ev = ["swap_car_model_slot.c:390-398 DrawTeamNameCharModel: if (cell != 10 && cell - 42 >= 2) "
              "SubmitCourseModel(cell < g_CourseModelCount ? cell : 1)",
              "design_mode.c:169 / swap_car_model_slot.c:350: g_TeamNameCharModel = TEAM NAME grid cursor",
              "name: g_SaveNameCharset[%d] = %r (EXE 0x%08X; memory_card_menu.c:32)" % (e["id"], ch,
                                                                                 G_SAVE_NAME_CHARSET + e["id"])]
        lf = Leaf(group, name, ASSET_SELECT, "course_table", e["id"], role, ev, stored_faces=len(e["faces"]))
        lf.drawn_in_game = drawn
        verts = [tuple(v[:3]) for v in e["vertices"]]
        require(len(verts) <= RA.COURSE_MAX_VERTS, "char %d: %d vertices" % (e["id"], len(verts)))
        remap = {}
        for f in e["faces"]:
            idx = tuple(f["idx"])
            if len(set(idx[:3])) != 3:          # NCLIP == 0 (terrain_submission.s:2274-2286)
                lf.undrawn += 1
                continue
            loop = written_loop(idx, strip_loop)
            corners = []
            for k in loop:
                g = idx[k]
                if g not in remap:
                    remap[g] = len(lf.vertices)
                    lf.vertices.append(verts[g])
                corners.append(Corner(remap[g], f["uv"][k], None))
            lf.faces.append(OutFace(corners, material_for("FT4", f["tpage"], f["clut"], None), "FT4", f["tpage"],
                                    f["clut"], f["rgb"], f["uv"], None))
        leaves.append(lf)
    return ModelSet("%s_course_model_table" % group_stem(ASSET_SELECT), ASSET_SELECT, "course_table", cmt["offset"],
                    leaves,
                    "car_assets.c:65 RegisterCourseModels(SELECT.BIN + offsets[1])",
                    "car_assets.c:70 UploadImageAsset(SELECT.BIN + offsets[2])")


def model_sets(strip_loop=False):
    return [_bank_set(ASSET_SELECT, strip_loop), _course_table_set(strip_loop), _bank_set(ASSET_OPTION, strip_loop)]


# ---------------------------------------------------------------- VRAM
def menu_vram(asset):
    """VRAM when the screen that draws asset 8 / 9 models is up: the boot
    uploads (LOGO, TITLE, CAR.TMS, see rr_textures.build_race_vram), then the
    pack's own image chain.  Later menu uploads are checked separately."""
    v = RT.Vram()
    for aid in BOOT_IMAGE_ASSETS:
        v.upload_chain(RT.parse_image_chain(RT.load_asset(aid), 0), "asset%03d" % aid, "boot")
    if asset == ASSET_SELECT:
        ch = RT.parse_select_bin_images(RT.load_asset(asset))
    elif asset == ASSET_OPTION:
        ch = RT.parse_option_bin_images(RT.load_asset(asset))
    else:
        raise ExportError("no menu VRAM for asset %d" % asset)
    v.upload_chain(ch, "asset%03d" % asset, "menu")
    return v


def later_menu_rects(exe=None):
    """LoadImage rects that run while these screens are shown."""
    exe = exe or exe_bytes()
    t = RA.exe_tables()
    faded = struct.unpack_from("<4H", exe, exe_off(G_TEAM_LOGO_FADED_CLUT_RECT))
    return {"UploadCarImage g_CarImageRect": tuple(t["car_image_rect"]),
            "g_TeamLogoRect canvas": RT.TEAM_LOGO_RECT,
            "g_TeamLogoClutRect": RT.TEAM_LOGO_CLUT_RECT,
            "g_TeamLogoFadedClutRect": faded,
            "team-name texture": TEAM_NAME_TEXTURE_RECT}


def _tri_cover(a, b, c, out):
    def e(p, q, x, y):
        return (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0])
    if e(a, b, c[0], c[1]) == 0:
        return
    xs, ys = (a[0], b[0], c[0]), (a[1], b[1], c[1])
    for y in range(min(ys), max(ys)):
        for x in range(min(xs), max(xs)):
            px, py = x + 0.5, y + 0.5
            w0, w1, w2 = e(b, c, px, py), e(c, a, px, py), e(a, b, px, py)
            if (w0 >= 0 and w1 >= 0 and w2 >= 0) or (w0 <= 0 and w1 <= 0 and w2 <= 0):
                out.add((x, y))


def uv_coverage(uvq):
    """Texels whose centres lie in the strip quad's two GPU triangles
    (v0 v1 v2, v1 v3 v2); the uv0 texel when the uv area is empty."""
    s = set()
    _tri_cover(uvq[0], uvq[1], uvq[2], s)
    _tri_cover(uvq[1], uvq[3], uvq[2], s)
    if not s:
        s.add(tuple(uvq[0]))
    return s


def _clip_area(poly, x0, y0, x1, y1):
    """Area of a convex polygon clipped to the box (Sutherland-Hodgman)."""
    def clip(pts, inside, cut):
        out = []
        for i in range(len(pts)):
            a, b = pts[i - 1], pts[i]
            ia, ib = inside(a), inside(b)
            if ib:
                if not ia:
                    out.append(cut(a, b))
                out.append(b)
            elif ia:
                out.append(cut(a, b))
        return out

    def cx(x):
        return lambda a, b: (x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]))

    def cy(y):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]), y)
    p = list(poly)
    for inside, cut in ((lambda q: q[0] >= x0, cx(x0)), (lambda q: q[0] <= x1, cx(x1)),
                        (lambda q: q[1] >= y0, cy(y0)), (lambda q: q[1] <= y1, cy(y1))):
        p = clip(p, inside, cut)
        if len(p) < 3:
            return 0.0
    return abs(sum(p[i - 1][0] * p[i][1] - p[i][0] * p[i - 1][1] for i in range(len(p)))) / 2.0


def _tri_uv_area2(t):
    return (t[1][0] - t[0][0]) * (t[2][1] - t[0][1]) - (t[1][1] - t[0][1]) * (t[2][0] - t[0][0])


def texels_read(tris):
    """Texels read by closest sampling over triangles given in texel space
    (each triangle has screen area; the face is taken as magnified enough that
    every part of it is sampled).  A triangle with uv area reads the texels
    whose square it overlaps with positive area; a triangle with no uv area
    (a uv line or point) reads the texels floor() meets along its edges, open
    at the end points (zero-length edges add nothing; a uv point reads its own
    texel)."""
    out = set()
    for t in tris:
        if abs(_tri_uv_area2(t)) > 1e-12:
            tx0, tx1 = int(math.floor(min(p[0] for p in t))), int(math.ceil(max(p[0] for p in t)))
            ty0, ty1 = int(math.floor(min(p[1] for p in t))), int(math.ceil(max(p[1] for p in t)))
            for ty in range(ty0, ty1):
                for tx in range(tx0, tx1):
                    if (tx, ty) not in out and _clip_area(t, tx, ty, tx + 1, ty + 1) > 1e-9:
                        out.add((tx, ty))
            continue
        line = set()
        for k in range(3):
            a, b = t[k], t[(k + 1) % 3]
            if a == b:
                continue
            n = 8 * (int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))) + 1)
            for i in range(n):
                s = (i + 0.5) / n
                line.add((int(math.floor(a[0] + (b[0] - a[0]) * s)), int(math.floor(a[1] + (b[1] - a[1]) * s))))
        out |= line or {(int(math.floor(t[0][0])), int(math.floor(t[0][1])))}
    return out


def uv_cycle_convex(uvs):
    """True when the written uv cycle is a convex polygon (no reflex or folded
    corner; collinear corners allowed) - then the OBJ quad's triangulation
    covers the same uv area as the GPU's strip triangles v0 v1 v2 / v1 v3 v2.
    Triangles (3 corners) are always convex."""
    if len(uvs) < 4:
        return True
    s = set()
    for i in range(len(uvs)):
        o, a, b = uvs[i - 1], uvs[i], uvs[(i + 1) % len(uvs)]
        c = (a[0] - o[0]) * (b[1] - a[1]) - (a[1] - o[1]) * (b[0] - a[0])
        if c:
            s.add(c > 0)
    return len(s) <= 1


def gpu_texels(uvq, quad=True):
    """Texels the GPU reads for a strip quad: triangles v0 v1 v2 and v1 v3 v2
    on the stored uv (the right / bottom edge never reached).  For a drawn
    triangle (idx3 repeats idx2 or idx1) the second strip triangle has no
    screen area and only v0 v1 v2 is read."""
    q = [tuple(map(float, p)) for p in uvq]
    return texels_read([(q[0], q[1], q[2]), (q[1], q[3], q[2])] if quad else [(q[0], q[1], q[2])])


def viewer_texels(wuv):
    """Texels a closest-sampling viewer reads for the written uv loop (fan)."""
    return texels_read([(wuv[0], wuv[k], wuv[k + 1]) for k in range(1, len(wuv) - 1)])


def texel_word(tpage, u, v):
    t = RT.tpage_info(tpage)
    div = (4, 2, 1, 1)[t["depth"]]
    return ((t["x"] + u // div) & 0x3FF, (t["y"] + v) & 0x1FF)


def texture_checks(mset, vram, later):
    """Every textured face samples texels uploaded by a TIM whose own CLUT is
    the face's CLUT, and the CLUT row was uploaded by the same pack; no later
    menu upload touches either.  Returns stats."""
    st = Counter()
    words = set()
    clut_words = set()
    foreign = Counter()
    for lf in mset.leaves:
        for f in lf.faces:
            if f.tpage is None:
                continue
            t = RT.tpage_info(f.tpage)
            require(t["depth"] in (0, 1) and t["extra"] == 0, "tpage %04X depth %d" % (f.tpage, t["depth"]))
            cx, cy = RT.clut_xy(f.clut)
            for k in range(16 if t["depth"] == 0 else 256):
                o = vram.owner[cy * RT.VRAM_W + cx + k]
                require(o >= 0 and vram.log[o][0].startswith("asset%03d#" % mset.asset) and vram.log[o][0].endswith(":clut"),
                        "%s/%s: CLUT %04X word %d owned by %s" % (lf.group, lf.name, f.clut, k,
                                                                  vram.log[o][0] if o >= 0 else None))
                clut_words.add((cx + k, cy))
            cov = uv_coverage(f.uv_quad)
            pal = vram.clut_entries(f.clut, 16 if t["depth"] == 0 else 256)
            # texels / colours a closest-sampling viewer reads with the written vt vs what the GPU reads.
            # The colours must agree on every face that one OBJ quad can represent: uv cycle convex and both
            # strip triangles with uv area.  A non-convex / folded uv quad depends on the GPU's v1-v2
            # diagonal, and a strip triangle with no uv area reads floor() of its uv line or point over its
            # whole screen area; neither fits one quad with shared corners - those are counted.
            quad = len(f.corners) == 4
            q = f.uv_quad
            gpu = gpu_texels(q, quad)
            wuv = written_uvs([c.uv for c in f.corners])
            seen = viewer_texels(wuv)
            require(all(0 <= u < 256 and 0 <= v < 256 for u, v in seen), "%s/%s: viewer uv outside the page" % (
                lf.group, lf.name))
            structural = (not uv_cycle_convex([c.uv for c in f.corners]) or _tri_uv_area2((q[0], q[1], q[2])) == 0
                          or (quad and _tri_uv_area2((q[1], q[3], q[2])) == 0))
            colour = (lambda s: {pal[RT.Vram.texel_index(vram, f.tpage, u, v)] for u, v in s})
            st["viewer_faces"] += 1
            st["viewer_faces_structural"] += structural
            if seen == gpu:
                st["viewer_texels_equal"] += 1
            if colour(seen) == colour(gpu):
                st["viewer_colours_equal"] += 1
            else:
                require(structural, "%s/%s: uv face %s: viewer reads %s, GPU %s" % (
                    lf.group, lf.name, q, sorted(seen - gpu)[:4], sorted(gpu - seen)[:4]))
            for ax in (0, 1):                    # a written fan triangle on a texel border (float noise decides)
                if any(len({p[ax] for p in (wuv[0], wuv[k], wuv[k + 1])}) == 1 and float(wuv[k][ax]).is_integer()
                       for k in range(1, len(wuv) - 1)):
                    st["viewer_faces_on_texel_border"] += 1
                    break
            for (u, v) in cov:
                x, y = texel_word(f.tpage, u, v)
                words.add((x, y))
                o = vram.owner[y * RT.VRAM_W + x]
                ok = o >= 0 and vram.log[o][6] == f.clut and vram.log[o][0].startswith("asset%03d#" % mset.asset)
                st["texels"] += 1
                if ok:
                    st["texels_own_tim"] += 1
                else:
                    foreign[vram.log[o][0] if o >= 0 else "unwritten"] += 1
                if vram.clut_entries(f.clut, 16 if t["depth"] == 0 else 256)[RT.Vram.texel_index(vram, f.tpage, u, v)]:
                    st["texels_opaque"] += 1
    if st["texels"]:
        frac = st["texels_own_tim"] / st["texels"]
        require(frac >= 0.99, "%s: only %.4f of covered texels come from a TIM with the face's CLUT (%s)" %
                (mset.group, frac, foreign.most_common(3)))
    for nm, (x, y, w, h) in later.items():
        hit = [p for p in words | clut_words if x <= p[0] < x + w and y <= p[1] < y + h]
        require(not hit, "%s: later upload %s overwrites used VRAM words %s" % (mset.group, nm, hit[:3]))
    st["foreign"] = dict(foreign)
    return st


# ---------------------------------------------------------------- writing
def _mtl_lines(mat, info):
    out = ["newmtl %s" % mat]
    if info["png"]:
        out += ["Ka 1.000 1.000 1.000", "Kd 1.000 1.000 1.000", "Ks 0.000 0.000 0.000", "d 1.0", "illum 1",
                "map_Kd textures/%s" % info["png"], "map_d textures/%s" % info["png"]]
    else:
        r, g, b = (c / 255.0 for c in info["rgb"])
        out += ["Ka %.4f %.4f %.4f" % (r, g, b), "Kd %.4f %.4f %.4f" % (r, g, b), "Ks 0.000 0.000 0.000", "d 1.0",
                "illum 1"]
    return out + [""]


def write_leaf(lf, leaf_dir, page_png, strip_loop, mset):
    """Write <name>.obj / .mtl / textures/*.png for one leaf."""
    leaf_dir.mkdir(parents=True, exist_ok=True)
    obj_p, mtl_p = leaf_dir / (lf.name + ".obj"), leaf_dir / (lf.name + ".mtl")
    mats = {}
    for f in lf.faces:
        if f.material not in mats:
            mats[f.material] = {"png": ("%s.png" % f.material) if f.tpage is not None else None,
                                "rgb": f.rgb, "tpage": f.tpage, "clut": f.clut, "kind": f.kind}
    textures = sorted(m["png"] for m in mats.values() if m["png"])
    if textures:
        (leaf_dir / "textures").mkdir(exist_ok=True)
    for mat, m in mats.items():
        if m["png"]:
            (leaf_dir / "textures" / m["png"]).write_bytes(page_png[(m["tpage"], m["clut"])])
    head = ["# Rage Racer (PAL SCES-006.50) %s / %s" % (lf.group, lf.name),
            "# source: asset_%03d %s (%s), %s %d at file offset 0x%X" % (
                lf.asset, RA.asset_name(lf.asset)[0], RA.asset_name(lf.asset)[1],
                "model-bank model" if lf.source == "bank" else "course-model-table entry", lf.index, mset.offset),
            "# role: %s" % lf.role]
    head += ["# evidence: %s" % e for e in lf.evidence]
    head += ["# load: %s; images: %s" % (mset.load_evidence, mset.image_evidence),
             "# raw PS1 units; Blender X=x Y=z Z=-y (import Forward Y, Up Z)",
             "# faces: strip v0 v1 / v2 v3; written %s" % (
                 "as the cycle f v0 v1 v3 v2" if strip_loop else
                 "as the cycle v0 v1 v3 v2 reversed (f v0 v2 v3 v1) so normals face the side NCLIP draws"),
             "# vt = (u/256, 1 - v/256) in the face's 256x256 texture page; an axis on which all corners share "
             "one texel (single-texel colour swatches) is moved to the texel centre (+0.5), as export_car_models; "
             "material tpTTTT_clCCCC = page TTTT through CLUT CCCC; F4_/G4_RRGGBB = flat / gouraud colour",
             "# %d stored faces, %d written, %d never drawn by the game (repeat among v0..v2) left out%s" % (
                 lf.stored_faces, len(lf.faces), lf.undrawn, "" if lf.drawn_in_game else "; MODEL NEVER DRAWN"),
             "mtllib %s" % mtl_p.name, "o %s" % lf.name]
    body = []
    for p in lf.vertices:
        body.append("v %g %g %g" % to_blender(p))
    vt_n = vn_n = 0
    cur = None
    for f in lf.faces:
        if f.material != cur:
            body.append("usemtl %s" % f.material)
            cur = f.material
        refs = []
        wuv = written_uvs([c.uv for c in f.corners]) if f.tpage is not None else [None] * len(f.corners)
        for c, tw in zip(f.corners, wuv):
            r = "%d" % (c.v + 1)
            if c.uv is not None:
                body.append("vt %.6f %.6f" % (tw[0] / 256.0, 1.0 - tw[1] / 256.0))
                vt_n += 1
                r += "/%d" % vt_n
            if lf.has_normals:
                n = c.n or (0.0, 0.0, 1.0)
                body.append("vn %.5f %.5f %.5f" % n)
                vn_n += 1
                r += ("/%d" if c.uv is not None else "//%d") % vn_n
            refs.append(r)
        body.append("f " + " ".join(refs))
    obj_p.write_text("\n".join(head + body) + "\n")
    mtl = ["# materials of %s / %s" % (lf.group, lf.name)]
    for mat in sorted(mats):
        mtl += _mtl_lines(mat, mats[mat])
    mtl_p.write_text("\n".join(mtl))
    return {"obj": obj_p, "mtl": mtl_p, "materials": len(mats), "textures": textures, "vt": vt_n, "vn": vn_n}


# ---------------------------------------------------------------- read-back
def read_png_rgba(path):
    """PNG reader for the files rr_textures.write_png produces (8-bit RGBA, filter 0)."""
    b = Path(path).read_bytes()
    require(b[:8] == bytes((137, 80, 78, 71, 13, 10, 26, 10)), "%s: not a PNG" % path)
    p, idat, ihdr = 8, b"", None
    while p < len(b):
        ln = struct.unpack_from(">I", b, p)[0]
        tag = b[p + 4:p + 8]
        body = b[p + 8:p + 8 + ln]
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        p += 12 + ln
    w, h, depth, ctype = ihdr[:4]
    require(depth == 8 and ctype == 6, "%s: not 8-bit RGBA" % path)
    raw = zlib.decompress(idat)
    st = w * 4
    out = bytearray()
    for y in range(h):
        require(raw[y * (st + 1)] == 0, "%s: row filter %d" % (path, raw[y * (st + 1)]))
        out += raw[y * (st + 1) + 1:(y + 1) * (st + 1)]
    return w, h, bytes(out)


def read_obj(obj_path):
    """Minimal OBJ + MTL reader: returns verts, faces [(mat, [(vi, ti, ni)])], materials."""
    obj_path = Path(obj_path)
    v, vt, vn, faces = [], [], [], []
    mats, cur, mtllib = {}, None, None
    for line in obj_path.read_text().splitlines():
        if not line or line[0] == "#":
            continue
        t = line.split()
        if t[0] == "v":
            v.append(tuple(map(float, t[1:4])))
        elif t[0] == "vt":
            vt.append(tuple(map(float, t[1:3])))
        elif t[0] == "vn":
            vn.append(tuple(map(float, t[1:4])))
        elif t[0] == "usemtl":
            cur = t[1]
        elif t[0] == "mtllib":
            mtllib = t[1]
        elif t[0] == "f":
            cs = []
            for r in t[1:]:
                parts = r.split("/")
                vi = int(parts[0]) - 1
                ti = int(parts[1]) - 1 if len(parts) > 1 and parts[1] else None
                ni = int(parts[2]) - 1 if len(parts) > 2 and parts[2] else None
                cs.append((vi, ti, ni))
            faces.append((cur, cs))
    require(mtllib is not None, "%s: no mtllib" % obj_path)
    name = None
    for line in (obj_path.parent / mtllib).read_text().splitlines():
        t = line.split()
        if not t:
            continue
        if t[0] == "newmtl":
            name = t[1]
            mats[name] = {}
        elif t[0] == "Kd":
            mats[name]["Kd"] = tuple(map(float, t[1:4]))
        elif t[0] in ("map_Kd", "map_d"):
            mats[name][t[0]] = t[1]
    for mat, cs in faces:
        require(mat in mats, "%s: usemtl %s not in MTL" % (obj_path, mat))
        for vi, ti, ni in cs:
            require(0 <= vi < len(v), "%s: vertex %d out of range" % (obj_path, vi))
            require(ti is None or 0 <= ti < len(vt), "%s: vt %s out of range" % (obj_path, ti))
            require(ni is None or 0 <= ni < len(vn), "%s: vn %s out of range" % (obj_path, ni))
        if "map_Kd" in mats[mat]:
            require(all(ti is not None for _, ti, _ in cs), "%s: textured face without vt" % obj_path)
    return v, vt, vn, faces, mats


# ---------------------------------------------------------------- preview rasteriser
_FONT = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
    "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001001001001",
    "8": "111101111101111", "9": "111101111001111", "A": "010101111101101", "B": "110101110101110",
    "C": "011100100100011", "D": "110101101101110", "E": "111100110100111", "F": "111100110100100",
    "G": "011100101101011", "H": "101101111101101", "I": "111010010010111", "J": "001001001101010",
    "K": "101101110101101", "L": "100100100100111", "M": "101111111101101", "N": "110101101101101",
    "O": "010101101101010", "P": "110101110100100", "Q": "010101101110011", "R": "110101110101101",
    "S": "011100010001110", "T": "111010010010010", "U": "101101101101111", "V": "101101101101010",
    "W": "101101111111101", "X": "101101010101101", "Y": "101101010010010", "Z": "111001010100111",
    "_": "000000000000111", "-": "000000111000000", ".": "000000000000010", " ": "000000000000000",
}


def draw_text(img, w, x, y, text, scale=2, col=(255, 255, 255)):
    for ch in text.upper():
        g = _FONT.get(ch, _FONT["-"])
        for r in range(5):
            for c in range(3):
                if g[r * 3 + c] == "1":
                    for dy in range(scale):
                        for dx in range(scale):
                            px, py = x + c * scale + dx, y + r * scale + dy
                            if 0 <= px < w and 0 <= py < len(img) // (4 * w):
                                o = (py * w + px) * 4
                                img[o:o + 4] = bytes(col) + b"\xff"
        x += 4 * scale


def render_obj(obj_path, size=256, az=35.0, el=28.0, bg=(46, 46, 54)):
    """Orthographic three-quarter view of an OBJ written by this tool, read
    back from disk (OBJ, MTL and PNGs).  Blender convention: a face whose
    winding normal points away from the camera is culled, so an inside-out
    export shows up as missing faces.  Returns (rgba, stats)."""
    v, vt, vn, faces, mats = read_obj(obj_path)
    texcache = {}
    for m in mats.values():
        if "map_Kd" in m and m["map_Kd"] not in texcache:
            texcache[m["map_Kd"]] = read_png_rgba(Path(obj_path).parent / m["map_Kd"])
    a, e = math.radians(az), math.radians(el)
    d = (math.cos(e) * math.sin(a), -math.cos(e) * math.cos(a), math.sin(e))   # towards the camera
    fwd = (-d[0], -d[1], -d[2])
    r = _norm((fwd[1] * 1.0 - 0.0, 0.0 - fwd[0] * 1.0, 0.0))                   # fwd x up(0,0,1)
    up = (r[1] * fwd[2] - r[2] * fwd[1], r[2] * fwd[0] - r[0] * fwd[2], r[0] * fwd[1] - r[1] * fwd[0])
    light = _norm((d[0] - 0.4 * r[0] + 0.5 * up[0], d[1] - 0.4 * r[1] + 0.5 * up[1], d[2] - 0.4 * r[2] + 0.5 * up[2]))
    proj = [(p[0] * r[0] + p[1] * r[1] + p[2] * r[2], p[0] * up[0] + p[1] * up[1] + p[2] * up[2],
             p[0] * d[0] + p[1] * d[1] + p[2] * d[2]) for p in v]
    img = bytearray(bytes(bg) + b"\xff") * (size * size)
    st = Counter()
    if not proj:
        return img, st
    xs, ys = [p[0] for p in proj], [p[1] for p in proj]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    sc = (size * 0.86) / span
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    scr = [((p[0] - cx) * sc + size / 2, size / 2 - (p[1] - cy) * sc, p[2]) for p in proj]
    zbuf = [-1e30] * (size * size)
    for mat, cs in faces:
        pts = [v[vi] for vi, _, _ in cs]
        n = _norm(newell(pts))
        if n is None:
            st["degenerate"] += 1
            continue
        if n[0] * d[0] + n[1] * d[1] + n[2] * d[2] <= 0:
            st["culled"] += 1
            continue
        st["drawn"] += 1
        lam = 0.45 + 0.55 * max(0.0, n[0] * light[0] + n[1] * light[1] + n[2] * light[2])
        m = mats[mat]
        tex = texcache.get(m.get("map_Kd"))
        kd = m.get("Kd", (0.8, 0.8, 0.8))
        for t in range(1, len(cs) - 1):
            tri = (cs[0], cs[t], cs[t + 1])
            P = [scr[c[0]] for c in tri]
            T = [vt[c[1]] if (tex and c[1] is not None) else (0.0, 0.0) for c in tri]
            x0, y0 = max(int(min(p[0] for p in P)), 0), max(int(min(p[1] for p in P)), 0)
            x1, y1 = min(int(max(p[0] for p in P)) + 1, size - 1), min(int(max(p[1] for p in P)) + 1, size - 1)
            den = (P[1][1] - P[2][1]) * (P[0][0] - P[2][0]) + (P[2][0] - P[1][0]) * (P[0][1] - P[2][1])
            if den == 0:
                continue
            for py in range(y0, y1 + 1):
                for px in range(x0, x1 + 1):
                    fx, fy = px + 0.5, py + 0.5
                    w0 = ((P[1][1] - P[2][1]) * (fx - P[2][0]) + (P[2][0] - P[1][0]) * (fy - P[2][1])) / den
                    w1 = ((P[2][1] - P[0][1]) * (fx - P[2][0]) + (P[0][0] - P[2][0]) * (fy - P[2][1])) / den
                    w2 = 1.0 - w0 - w1
                    if w0 < -1e-6 or w1 < -1e-6 or w2 < -1e-6:
                        continue
                    z = w0 * P[0][2] + w1 * P[1][2] + w2 * P[2][2]
                    k = py * size + px
                    if z <= zbuf[k]:
                        continue
                    if tex:
                        s = w0 * T[0][0] + w1 * T[1][0] + w2 * T[2][0]
                        tt = w0 * T[0][1] + w1 * T[1][1] + w2 * T[2][1]
                        tw, th, tp = tex
                        ux = min(max(int(s * tw), 0), tw - 1)
                        vy = min(max(int((1.0 - tt) * th), 0), th - 1)
                        o = (vy * tw + ux) * 4
                        if tp[o + 3] == 0:
                            continue
                        col = (tp[o] * lam, tp[o + 1] * lam, tp[o + 2] * lam)
                    else:
                        col = (kd[0] * 255 * lam, kd[1] * 255 * lam, kd[2] * 255 * lam)
                    zbuf[k] = z
                    o = k * 4
                    img[o:o + 4] = bytes((min(int(col[0]), 255), min(int(col[1]), 255), min(int(col[2]), 255), 255))
    return img, st


def contact_sheet(tiles, cols, tile, path):
    rows = (len(tiles) + cols - 1) // cols
    W, H = cols * tile, rows * tile
    sheet = bytearray(b"\x10\x10\x14\xff") * (W * H)
    for k, (label, img) in enumerate(tiles):
        ox, oy = (k % cols) * tile, (k // cols) * tile
        for y in range(tile):
            sheet[((oy + y) * W + ox) * 4:((oy + y) * W + ox + tile) * 4] = img[y * tile * 4:(y + 1) * tile * 4]
        draw_text(sheet, W, ox + 4, oy + 4, label, 2 if len(label) * 8 <= tile - 8 else 1)
    RT.write_png(path, W, H, bytes(sheet))
    return path


# ---------------------------------------------------------------- scans
def scan_model_structures(d):
    """Every 4-aligned offset whose bytes form a complete ModelBankHeader or
    CourseModelAssetHeader: header fields in range and every command stream
    walking cleanly (rr_assets.walk_stream: opcodes, strides, vertex bounds,
    GP0 code bytes) and tiling up to the vertex pool / geometry."""
    hits = []
    n = len(d)
    u32, s32 = RA.u32, RA.s32
    for o in range(0, n - 16, 4):
        c = u32(d, o)
        if 0 < c < 0x400:
            hdr = 12 + 4 * c
            tbl, nrm = s32(d, o + 4), s32(d, o + 8)
            if hdr <= tbl < nrm and (nrm - tbl) % 8 == 0 and o + nrm <= n and o + hdr <= n:
                mods = struct.unpack_from("<%di" % c, d, o + 12)
                if mods[0] == hdr and all(mods[i] < mods[i + 1] for i in range(c - 1)) and mods[-1] < tbl:
                    probs = RA.Problems()
                    ok = True
                    for k, m in enumerate(mods):
                        stop = mods[k + 1] if k + 1 < c else tbl
                        e, ops, _ = RA.walk_stream(d, o + m, o + tbl, RA.BANK_LAYOUT, (nrm - tbl) // 8, probs, "scan")
                        if probs or e != o + stop or not ops:
                            ok = False
                            break
                    if ok:
                        hits.append(("model_bank", o))
            hdr = 4 + 12 * c
            if o + hdr <= n:
                ents = [struct.unpack_from("<3i", d, o + 4 + 12 * k) for k in range(c)]
                if all(0 < vc <= RA.COURSE_MAX_VERTS and g >= hdr and m >= hdr and o + g + 8 * vc <= n and o + m < n
                       for g, vc, m in ents) and min(m for _, _, m in ents) == hdr:
                    probs = RA.Problems()
                    ok = True
                    starts = sorted(set(m for _, _, m in ents))
                    geo0 = min(g for g, _, _ in ents)
                    for g, vc, m in ents:
                        nxt = min([s for s in starts if s > m] + [geo0])
                        e, ops, _ = RA.walk_stream(d, o + m, o + geo0, RA.COURSE_LAYOUT, vc, probs, "scan")
                        if probs or e != o + nxt or not ops:
                            ok = False
                            break
                    if ok:
                        hits.append(("course_model_table", o))
    return hits


def expected_structures(asset):
    """Model-structure offsets rr_assets knows for an asset (positive control)."""
    inv = RA.asset_inventory(asset)
    return sorted((b["kind"], b["offset"]) for b in inv["blocks"] if b["kind"] in ("model_bank", "course_model_table"))


def rage_str_census(track01, lba, nsec):
    """Sector kinds of RAGE.STR from the raw MODE2 sectors: XA audio (submode
    bit 2), STR video (user data starts 0x0160, 0x8001), all-zero, other."""
    c = Counter()
    with open(track01, "rb") as f:
        f.seek(lba * 2352)
        for _ in range(nsec):
            s = f.read(2352)
            if s[18] & 0x04:
                c["xa_audio"] += 1
            elif struct.unpack_from("<HH", s, 24) == (0x0160, 0x8001):
                c["str_video"] += 1
            elif not any(s[24:24 + 2048]):
                c["zero"] += 1
            else:
                c["other"] += 1
    return c


# ---------------------------------------------------------------- asset index
# 'asset_088.bin' (course OBJs), 'asset 18' (car OBJs), 'asset018' - but not 'assets 10..73'
_ASSET_TOKEN = re.compile(r"(?<![A-Za-z0-9_])asset[ _]?(\d{1,3})(?![0-9])", re.I)


def obj_header_assets(path):
    """Asset numbers named in an OBJ's leading comment block (up to the first
    non-comment, non-mtllib line)."""
    out = set()
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("#"):
                    out.update(int(m.group(1)) for m in _ASSET_TOKEN.finditer(s))
                elif s and not s.startswith("mtllib"):
                    break
    except OSError:
        pass
    return out


def _rel(p, root):
    return str(Path(p).relative_to(root)).replace(chr(92), "/")


def sibling_outputs(models_root):
    """What the course and car exporters wrote, per asset.  Returns
    {"models": {asset: {folder: [obj names]}}, "uses": {asset: [text]},
     "via": {asset: set("manifest", "header")}, "problems": [text]}.
    Sources: cars/cars_manifest.json (cars[].assets = [.1ST, .2ND], folder),
    courses/*/*/manifest.json (result.asset = the .2ND pack), and the header
    comments of every OBJ outside other/.  A manifest folder must hold OBJs
    whose headers name the same asset; a header asset must be in a manifest."""
    models_root = Path(models_root)
    models = defaultdict(lambda: defaultdict(list))
    uses = defaultdict(list)
    via = defaultdict(set)
    problems = []
    objs = [p for p in sorted(models_root.rglob("*.obj")) if _rel(p, models_root).split("/")[0] != "other"]
    head = {p: obj_header_assets(p) for p in objs}
    for p in objs:
        for a in head[p]:
            models[a][_rel(p.parent, models_root)].append(p.name)
            via[a].add("header")
    man_folders = defaultdict(set)
    cm = models_root / "cars" / "cars_manifest.json"
    if cm.exists():
        cars = json.loads(cm.read_text())
        for c in cars.get("cars", []):
            a1, a2 = c["assets"]
            folder = "cars/" + c["folder"]
            man_folders[a1].add(folder)
            via[a1].add("manifest")
            uses[a2].append("car spec pack of %s (read by export_car_models.py)" % folder)
            if not list((models_root / folder).glob("*.obj")):
                problems.append("cars_manifest lists %s for asset %d but it holds no OBJ" % (folder, a1))
    for mf in sorted(models_root.glob("courses/*/*/manifest.json")):
        res = json.loads(mf.read_text()).get("result", {})
        a = res.get("asset")
        if a is None:
            problems.append("%s: no result.asset" % _rel(mf, models_root))
            continue
        folder = _rel(mf.parent, models_root)
        man_folders[a].add(folder)
        via[a].add("manifest")
        p1, p2 = RA.asset_name(a - 1)[0], RA.asset_name(a)[0]
        if p1[:-4] == p2[:-4] and p1.upper().endswith(".1ST") and p2.upper().endswith(".2ND"):
            uses[a - 1].append("image pack of %s (textures, read by export_course_scenes.py)" % folder)
        else:
            problems.append("asset %d (%s) is not the .1ST of %d (%s)" % (a - 1, p1, a, p2))
    for a, folders in man_folders.items():
        for folder in folders:
            if folder not in models.get(a, {}):
                problems.append("manifest folder %s (asset %d): no OBJ there names asset %d" % (folder, a, a))
    for a, fm in models.items():
        for folder in fm:
            if folder not in man_folders.get(a, ()):
                problems.append("OBJ header in %s names asset %d, which no manifest assigns to that folder" % (folder, a))
    return {"models": models, "uses": uses, "via": via, "problems": problems}


def _model_summary(inv):
    parts = []
    for b in inv["blocks"]:
        if b["kind"] == "model_bank":
            parts.append("model bank slot %s: %d models, %d faces" % (b.get("slot"), b["models"], sum(b["faces"].values())))
        elif b["kind"] == "course_model_table":
            parts.append("course model table: %d models, %d faces" % (b["models"], sum(b["faces"].values())))
        elif b["kind"] == "terrain_cells":
            parts.append("terrain: %d cells, %d faces" % (b["cells"] if "cells" in b else -1,
                                                           sum(b["faces"].values()) if isinstance(b.get("faces"), dict) else -1))
    return parts


SIBLING_EXPORTER = {"track_2nd": "tools/export_course_scenes.py -> extracted/models/courses/ (terrain, course "
                                  "models, bank1 rival cars and scenery, bank2 flyby; unplaced.obj / variants.obj)",
                    "car_1st": "tools/export_car_models.py -> extracted/models/cars/"}


def asset_index_status(leaves_by_asset, models_root):
    """Per-asset export status for the index.  Returns (rows, summary) where
    rows[i] = (has_models, status, text) and status is 'here', 'sibling',
    'missing' or 'none'."""
    sib = sibling_outputs(models_root)
    rows = {}
    for i in range(RA.ASSET_COUNT):
        inv = RA.asset_inventory(i)
        require(not inv["errors"], "asset %d inventory errors: %s" % (i, inv["errors"][:2]))
        msum = _model_summary(inv)
        fmt = inv["format"]
        got = sib["models"].get(i, {})
        if i in leaves_by_asset:
            require(not got, "asset %d exported both here and in %s" % (i, sorted(got)))
            rows[i] = (True, "here", "EXPORTED here: " + "; ".join(leaves_by_asset[i]))
        elif msum:
            who = SIBLING_EXPORTER.get(fmt)
            require(who is not None, "asset %d (%s) holds models but no exporter covers format %s" % (i, inv["path"], fmt))
            if got:
                txt = "EXPORTED by %s: %s (found via %s)" % (who.split(" ")[0], "; ".join(
                    "%s [%s]" % (folder, ", ".join(sorted(names))) for folder, names in sorted(got.items())),
                    " + ".join(sorted(sib["via"][i])))
                rows[i] = (True, "sibling", txt)
            else:
                rows[i] = (True, "missing", "NOT EXPORTED YET: %s has written no OBJ for this asset "
                           "(re-run with --index-only after it finishes)" % who)
        else:
            require(not got, "asset %d has no model data but OBJs name it: %s" % (i, sorted(got)))
            rows[i] = (False, "none", "no model data" + ("; " + "; ".join(sib["uses"][i]) if sib["uses"].get(i) else ""))
    cnt = Counter(st for _, st, _ in rows.values())
    summary = {"model_assets": sum(1 for h, _, _ in rows.values() if h), "here": cnt["here"],
               "sibling": cnt["sibling"], "missing": sorted(i for i, r in rows.items() if r[1] == "missing"),
               "no_models": cnt["none"], "problems": sib["problems"]}
    return rows, summary


def write_asset_index(path, leaves_by_asset, scans, models_root, require_complete=False):
    rows, summ = asset_index_status(leaves_by_asset, models_root)
    require(not summ["problems"], "sibling outputs inconsistent: %s" % summ["problems"][:4])
    if require_complete:
        require(not summ["missing"], "model-bearing assets with no export: %s" % summ["missing"])
    miss = summ["missing"]
    lines = ["Rage Racer (PAL, SCES-006.50) - every RAGE.BIN asset and disc file, and where its models went",
             "written by tools/export_other_models.py; names from g_AssetPaths (EXE 0x8007C48C) and the EXE name tables",
             "sibling exports found from cars/cars_manifest.json, courses/*/*/manifest.json and OBJ header comments",
             "summary: %d of %d assets hold model data: %d exported by this tool (other/), %d by the course / car "
             "exporters, %d not exported yet%s; %d hold no model data" % (
                 summ["model_assets"], RA.ASSET_COUNT, summ["here"], summ["sibling"], len(miss),
                 (" (assets %s)" % ", ".join(str(a) for a in miss)) if miss else "", summ["no_models"]),
             "columns: index | disc path | format | size | content blocks | models | export",
             ""]
    for i in range(RA.ASSET_COUNT):
        inv = RA.asset_inventory(i)
        kinds = Counter(b["kind"] for b in inv["blocks"])
        blk = ", ".join("%s%s" % (k, "" if n == 1 else " x%d" % n) for k, n in kinds.items())
        msum = _model_summary(inv)
        lines.append("%3d | %-24s | %-10s | %8d | %s" % (i, inv["path"], inv["format"], inv["size"], inv["name"]))
        lines.append("    blocks: %s" % blk)
        lines.append("    models: %s" % ("; ".join(msum) if msum else "none"))
        lines.append("    export: %s" % rows[i][2])
    lines += ["", "Disc files (ISO9660 of Track 01; Tracks 02.. are CD-DA audio)"]
    for row in scans.get("disc_rows", []):
        lines.append("    " + row)
    lines += ["", "EXE-resident graphics data (no 3D models; scan result above)"]
    for nm, addr, what in EXE_NON_MODEL_DATA:
        lines.append("    %-28s 0x%08X  %s" % (nm, addr, what))
    path.write_text("\n".join(lines) + "\n")
    return path, summ


def disc_rows(scans):
    tr = RA.find_track01()
    if tr is None:
        return ["disc image not found: disc files not listed (RAGE.BIN assets above are from extracted/assets_raw)"]
    listing = RA.list_disc_files(tr)
    rows = []
    cdda = [f for f in listing["files"] if f["path"].upper().startswith("/CDDA/")]
    for f in listing["files"]:
        p = f["path"].upper()
        if p.startswith("/CDDA/"):
            continue
        if f["dir"]:
            what = "directory (%d CD-DA track entries, audio only)" % len(cdda) if p == "/CDDA" else "directory"
        elif p.startswith("/RAGE.BIN"):
            what = "asset archive: 135 assets indexed above; RAGE.BIN index checked against extracted/assets_raw (%s)" % (
                scans.get("rage_bin", "?"))
        elif p.startswith("/RAGE.STR"):
            what = "FMV/XA stream, %s; no model data" % scans.get("rage_str", "?")
        elif p.startswith("/SCES_006.50"):
            what = "the EXE (sha1 equal to assets/PAL/main.exe: %s); %s" % (scans.get("exe_same"), scans.get("exe_scan", "?"))
        elif p.startswith("/SYSTEM.CNF"):
            what = "boot config text; no model data"
        else:
            what = "?"
        rows.append("%-16s lba %6d size %9d  %s" % (f["path"], f["lba"], f["size"], what))
    if cdda:
        rows.append("/CDDA/*.DA        %d entries, lba %d..%d: CD-DA music; no model data" % (
            len(cdda), min(f["lba"] for f in cdda), max(f["lba"] for f in cdda)))
    return rows


# ---------------------------------------------------------------- driver
def export_all(out_root=OUT_ROOT, strip_loop=False, previews=True, scans=True, log=print, require_complete=False):
    out_root = Path(out_root)
    other = out_root / "other"
    require(other.name == "other" and other.parent.name == "models", "refusing to clear %s" % other)
    if other.exists():
        shutil.rmtree(other)
    other.mkdir(parents=True)
    report = {"sets": [], "failures": []}
    sets = model_sets(strip_loop)
    exe = exe_bytes()
    later = later_menu_rects(exe)
    leaves_by_asset = defaultdict(list)
    manifest = []
    sheet_jobs = {}                 # output group -> (tiles, tile size)
    for ms in sets:
        vram = menu_vram(ms.asset)
        tstats = texture_checks(ms, vram, later)
        pages = sorted({(f.tpage, f.clut) for lf in ms.leaves for f in lf.faces if f.tpage is not None})
        page_png = {}
        for tp, cl in pages:
            rgba, _stp, _cols = vram.sample_page(tp, cl)
            tmp = other / ("_tmp_%04X_%04X.png" % (tp, cl))
            RT.write_png(tmp, 256, 256, rgba)
            page_png[(tp, cl)] = tmp.read_bytes()
            tmp.unlink()
        winding = Counter()
        for lf in ms.leaves:
            vb = [to_blender(p) for p in lf.vertices]
            mc = tuple(sum(p[i] for p in vb) / max(len(vb), 1) for i in range(3))
            for f in lf.faces:
                pts = [vb[c.v] for c in f.corners]
                g = newell(pts)
                if f.stored_normal is None:
                    # no stored normal (F4, course-model FT4): report only, the
                    # written cycle follows the same NCLIP rule as the G4/GT4 faces
                    fc = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
                    out = sum(g[i] * (fc[i] - mc[i]) for i in range(3))
                    winding["no_normal_outward" if out > 0 else "no_normal_inward"] += 1
                    continue
                dot = sum(g[i] * f.stored_normal[i] for i in range(3))
                winding["agree" if dot > 0 else "disagree"] += 1
            leaf_dir = other / lf.group / lf.name
            w = write_leaf(lf, leaf_dir, page_png, strip_loop, ms)
            v, vt, vn, faces, mats = read_obj(w["obj"])          # read-back validation
            require(len(v) == len(lf.vertices) and len(faces) == len(lf.faces), "%s: read-back counts" % w["obj"])
            require(all(0.0 <= s <= 1.0 and 0.0 <= t <= 1.0 for s, t in vt), "%s: vt outside [0, 1]" % w["obj"])
            for (mat, cs), of in zip(faces, lf.faces):
                require(mat == of.material and len(cs) == len(of.corners), "%s: read-back face differs" % w["obj"])
                if of.tpage is None:
                    continue
                # read-back vt * 256 must be the stored texel uv (collapsed to the texel centre only on an
                # axis of extent 0 / 1), and every texel a closest-sampling viewer reads must be one the GPU reads
                exp_uv = written_uvs([c.uv for c in of.corners])
                for (_vi, ti, _ni), c, e in zip(cs, of.corners, exp_uv):
                    s, t = vt[ti]
                    require(abs(s * 256.0 - e[0]) < 1e-3 and abs((1.0 - t) * 256.0 - e[1]) < 1e-3,
                            "%s: vt %s does not match texel uv %s" % (w["obj"], vt[ti], c.uv))
                for ax in (0, 1):
                    vals = [c.uv[ax] for c in of.corners]
                    lo, hi = min(vals), max(vals)
                    got = {e[ax] for e in exp_uv}
                    require((hi - lo >= 2 and got == set(vals)) or (hi - lo <= 1 and got == {lo + 0.5}),
                            "%s: uv axis %d written as %s from %s" % (w["obj"], ax, sorted(got), vals))
            for m in mats.values():
                if "map_Kd" in m:
                    tw, th, _ = read_png_rgba(leaf_dir / m["map_Kd"])
                    require((tw, th) == (256, 256) and m.get("map_d") == m["map_Kd"], "%s: texture %s" % (w["obj"], m))
            entry = {"group": lf.group, "name": lf.name, "asset": lf.asset, "source": lf.source, "index": lf.index,
                     "role": lf.role, "evidence": lf.evidence, "drawn_in_game": lf.drawn_in_game,
                     "vertices": len(lf.vertices), "faces_written": len(lf.faces), "faces_stored": lf.stored_faces,
                     "faces_never_drawn": lf.undrawn, "kinds": dict(Counter(f.kind for f in lf.faces)),
                     "materials": w["materials"], "textures": w["textures"],
                     "obj": str(w["obj"].relative_to(out_root)).replace(chr(92), "/")}
            manifest.append(entry)
            leaves_by_asset[lf.asset].append("other/%s/%s" % (lf.group, lf.name))
            if previews:
                label = lf.name.split("_", 1)[1] if "_" in lf.name else lf.name
                tsize = 256 if ms.kind == "bank" else 192
                job = sheet_jobs.setdefault(lf.group, ([], tsize))
                multi = len(PREVIEW_VIEWS[ms.kind]) > 1
                for vname, az, el in PREVIEW_VIEWS[ms.kind]:
                    img, rst = render_obj(w["obj"], tsize, az, el)
                    entry["preview_%s_drawn_culled" % vname] = [rst["drawn"], rst["culled"]]
                    job[0].append((label + (" " + vname if multi else ""), img))
        exp = "disagree" if strip_loop else "agree"
        bad = winding["agree" if strip_loop else "disagree"]
        require(bad == 0, "%s: %d faces whose written winding is opposite to the stored normal" % (ms.group, bad))
        st = {"group": ms.group, "asset": ms.asset, "kind": ms.kind, "offset": ms.offset, "leaves": len(ms.leaves),
              "faces_written": sum(len(lf.faces) for lf in ms.leaves),
              "faces_never_drawn": sum(lf.undrawn for lf in ms.leaves),
              "vertices": sum(len(lf.vertices) for lf in ms.leaves), "pages": ["%04X/%04X" % p for p in pages],
              "texels_checked": tstats["texels"], "texels_own_tim": tstats["texels_own_tim"],
              "texels_opaque": tstats["texels_opaque"], "texels_foreign": tstats["foreign"],
              "viewer_vs_gpu": {"textured_faces": tstats["viewer_faces"],
                                "same_texels": tstats["viewer_texels_equal"],
                                "same_colours": tstats["viewer_colours_equal"],
                                "faces_one_quad_cannot_represent": tstats["viewer_faces_structural"],
                                "faces_with_a_written_triangle_on_a_texel_border":
                                    tstats["viewer_faces_on_texel_border"]},
              "winding_vs_stored_normals": {exp: winding[exp],
                                            "faces_with_normals": winding["agree"] + winding["disagree"]},
              "no_normal_faces_centroid_outward": [winding["no_normal_outward"],
                                                   winding["no_normal_outward"] + winding["no_normal_inward"]]}
        report["sets"].append(st)
        log("%-28s asset %d %-12s leaves %2d faces %4d (never drawn %3d) verts %4d pages %s texels own-TIM %d/%d "
            "opaque %d; winding %s %d/%d; no-normal faces centroid-outward %d/%d; written vt vs GPU: same texels "
            "%d, same colours %d of %d textured faces (%d have a uv-degenerate strip triangle or a non-convex uv "
            "cycle, where one quad may differ; %d have a written triangle on a texel border)" % (
                ms.group, ms.asset, ms.kind, st["leaves"], st["faces_written"], st["faces_never_drawn"],
                st["vertices"], ",".join(st["pages"]) or "-", st["texels_own_tim"], st["texels_checked"],
                st["texels_opaque"], exp, winding[exp], winding["agree"] + winding["disagree"],
                *st["no_normal_faces_centroid_outward"], st["viewer_vs_gpu"]["same_texels"],
                st["viewer_vs_gpu"]["same_colours"], st["viewer_vs_gpu"]["textured_faces"],
                st["viewer_vs_gpu"]["faces_one_quad_cannot_represent"],
                st["viewer_vs_gpu"]["faces_with_a_written_triangle_on_a_texel_border"]))
    if previews:
        pv = out_root / "previews"
        pv.mkdir(parents=True, exist_ok=True)
        for old in pv.glob("other_*.png"):          # this tool's sheets only (siblings write elsewhere)
            old.unlink()
        for group, (tiles, tile) in sheet_jobs.items():
            cols = 7 if len(tiles) > 24 else 4
            p = contact_sheet(tiles, cols, tile, pv / ("other_%s.png" % group))
            report.setdefault("previews", []).append(str(p))
            log("preview %s (%d tiles)" % (p, len(tiles)))
    sc = run_scans(exe, log) if scans else {}
    idx, summ = write_asset_index(out_root / "asset_index.txt", leaves_by_asset, sc, out_root, require_complete)
    log_index_summary(summ, log)
    (other / "other_models.json").write_text(json.dumps({"leaves": manifest, "sets": report["sets"],
                                                         "strip_loop": strip_loop}, indent=1))
    report["asset_index"] = str(idx)
    report["index_summary"] = summ
    report["manifest"] = str(other / "other_models.json")
    report["leaves"] = len(manifest)
    return report


def log_index_summary(summ, log=print):
    log("asset index: %d model-bearing assets: %d exported here, %d by the course / car exporters, %d not yet%s" % (
        summ["model_assets"], summ["here"], summ["sibling"], len(summ["missing"]),
        (" %s" % summ["missing"]) if summ["missing"] else ""))


def index_only(out_root=OUT_ROOT, scans=True, require_complete=False, log=print):
    """Rewrite asset_index.txt from the current tree (other/other_models.json
    for this tool's leaves) without re-exporting."""
    out_root = Path(out_root)
    mf = out_root / "other" / "other_models.json"
    require(mf.exists(), "%s missing: run the full export first" % mf)
    leaves_by_asset = defaultdict(list)
    for e in json.loads(mf.read_text())["leaves"]:
        leaves_by_asset[e["asset"]].append("other/%s/%s" % (e["group"], e["name"]))
        require((out_root / e["obj"]).exists(), "%s listed in %s but missing" % (e["obj"], mf))
    sc = run_scans(exe_bytes(), log) if scans else {}
    idx, summ = write_asset_index(out_root / "asset_index.txt", leaves_by_asset, sc, out_root, require_complete)
    log_index_summary(summ, log)
    return {"asset_index": str(idx), "index_summary": summ, "leaves": sum(len(v) for v in leaves_by_asset.values()),
            "manifest": str(mf)}


def run_scans(exe, log=print):
    """EXE model-structure scan with positive controls, RAGE.STR census,
    RAGE.BIN index check, disc EXE identity, disc file rows."""
    sc = {}
    hits = scan_model_structures(exe)
    require(not hits, "EXE holds model structures at %s" % hits[:5])
    ctrl = {}
    for a in (8, 9, 10, 88):
        got = sorted(scan_model_structures(RA.load_asset(a)))
        want = expected_structures(a)
        require(got == want, "scanner positive control asset %d: found %s, expected %s" % (a, got, want))
        ctrl[a] = len(got)
    sc["exe_scan"] = ("scanned all %d 4-aligned offsets for ModelBankHeader / CourseModelAssetHeader: 0 hits "
                      "(scanner finds exactly the known %s in assets 8/9/10/88)" % (len(exe) // 4, ctrl))
    log(sc["exe_scan"])
    tr = RA.find_track01()
    if tr is not None:
        files = RA.list_disc_files(tr)["files"]
        rs = next(f for f in files if f["path"].upper().startswith("/RAGE.STR"))
        nsec = (rs["size"] + 2047) // 2048
        cen = rage_str_census(tr, rs["lba"], nsec)
        require(sum(cen.values()) == nsec and cen["other"] == 0, "RAGE.STR census %s" % cen)
        sc["rage_str"] = "%d sectors: %d STR video, %d XA audio, %d zero" % (nsec, cen["str_video"], cen["xa_audio"],
                                                                           cen["zero"])
        rb = RA.check_rage_bin(tr, files)
        require(not rb["mismatch"] and not rb["missing"], "RAGE.BIN check %s" % rb)
        sc["rage_bin"] = "0 mismatches, 0 missing, gaps %d" % len(rb["gaps"])
        exe_disc = next(f for f in files if f["path"].upper().startswith("/SCES_006.50"))
        f0, read = RA._sector_reader(tr)
        try:
            blob = read(exe_disc["lba"], (exe_disc["size"] + 2047) // 2048)[:exe_disc["size"]]
        finally:
            f0.close()
        sc["exe_same"] = blob == exe
        require(sc["exe_same"], "SCES_006.50 on disc differs from assets/PAL/main.exe")
        log("RAGE.STR: %s; RAGE.BIN: %s" % (sc["rage_str"], sc["rage_bin"]))
    sc["disc_rows"] = disc_rows(sc)
    return sc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--out", default=str(OUT_ROOT))
    ap.add_argument("--strip-loop", action="store_true", help="write literal f v0 v1 v3 v2 (inward Blender normals)")
    ap.add_argument("--no-previews", action="store_true")
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--index-only", action="store_true", help="only rewrite asset_index.txt from the current tree")
    ap.add_argument("--require-complete", action="store_true",
                    help="fail unless every model-bearing asset has an export (run after the sibling exporters)")
    a = ap.parse_args(argv)
    try:
        if a.index_only:
            rep = index_only(a.out, not a.no_scan, a.require_complete)
        else:
            rep = export_all(a.out, a.strip_loop, not a.no_previews, not a.no_scan, require_complete=a.require_complete)
    except (ExportError, RA.FormatError, MB.BankError, RT.FormatError) as exc:
        print("FAILED: %s" % exc)
        return 1
    print("leaves %d; index %s; manifest %s" % (rep["leaves"], rep["asset_index"], rep["manifest"]))
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
