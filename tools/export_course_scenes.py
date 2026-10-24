#!/usr/bin/env python3
"""
export_course_scenes.py - assemble every in-game Rage Racer (PAL) race course
(.2ND scene packs, 4 courses x 6 classes) as one complete, textured scene:
terrain cells, course models and bank (scenery) models, each piece at the world
position the game's own draw code gives it.

Usage:
  python -I tools/export_course_scenes.py [88 | asset_088]     one scene (default 088)
  python -I tools/export_course_scenes.py --all                all 24 scenes + assertions
  options: --out DIR (default extracted/models/courses)  --no-preview  --size N

Output per scene, extracted/models/courses/<COURSE>/<CLASSn_NAME>/ (asset 094 OVAL1.2ND, the
new-game prologue scene with MYTHICAL COAST content, goes to MYTHICAL_COAST/PROLOGUE_OVAL1/):
  <STEM>.obj / <STEM>.mtl   the scene: 'o' per instance, 'g terrain' / 'g course_models' / 'g scenery'
  variants.obj              models drawn at an established placement only in other animation
                            frames / environment states (same transform as the shown frame)
  unplaced.obj              every other model of the pack (no placement on the visible course:
                            cars, unreached models, the class-6 route fallback parked off the map):
                            laid out on a display grid, offset written in a comment per object
  textures/*.png            256x256 RGBA page per (tpage, effective clut); texture-window tiles
  manifest.json             objects, placements, evidence, materials
  preview_*.png             stdlib rasteriser renders of the written OBJ files

Conventions (evidence in the notes file):
  * raw PS1 vertex units (= 4 x world units), Blender X = x, Y = z, Z = -y (a proper rotation).
  * PS1 front faces have NCLIP > 0 (terrain_submission.s:183-212 terrain, 1066-1075 bank models,
    2274-2283 course models), i.e. they are clockwise on the y-down screen; their right-hand
    normal points away from the viewer.  The quad v0 v1 / v2 v3 (strip order) is therefore
    written  f v0 v2 v3 v1  (the reverse of the loop v0 v1 v3 v2) so that Blender normals face
    the side the game draws.  Triangles: (v0 v1 v2) -> f v0 v2 v1, (v1 v3 v2) -> f v1 v2 v3.
  * vt = (u/256, 1 - v/256) on the page PNG; texture-window faces use a tile PNG with
    vt = (u/period_u, 1 - v/period_v) and rely on REPEAT.
  * vertex colours (v x y z r g b) carry the face colour word = rgb/255: for textured faces the
    PS1 modulates the texel by rgb/128 (so 0.5 = unchanged); flat faces also get Kd = rgb/255.
    blender_import_course.py reproduces this exactly (see its docstring).
  * semi-transparent course faces (GP0 0x2E, all tpage ABR 0 = 0.5*B + 0.5*F) get materials
    '<page>_semi_abr0' whose PNG has alpha 128 on STP texels (opaque texels 255, 0x0000 = 0).
  * the scene is the race-start state: env mode 4 (g_IsEnvironmentMode4) at the script seek,
    series 0, timers 0 except that time-varying CLUT offsets (blinking sign, tint cycle) use k = 0.

Importable (no side effects on import):
  exe_names() -> dict            g_AssetPaths / g_CourseNames / g_GrandPrixNames read from main.exe
  scene_names(aid, names) -> dict
  build_scene(aid, names=None) -> Scene    (objects with world vertices, faces with tpage/clut/uv)
  export_scene(aid, out_root, ...) -> dict (writes the files, returns stats)
  read_obj / read_mtl / read_png / render_view   (the preview rasteriser)
"""
import argparse
import json
import math
import struct
import sys
import time
import zlib
from array import array
from collections import Counter, defaultdict
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import export_course_models as ecm      # noqa: E402  (pack_name, verified below)
import rr_model_banks as rmb            # noqa: E402
import rr_placement as rp               # noqa: E402
import rr_terrain_cells as rtc          # noqa: E402
import rr_textures as rt                # noqa: E402

REPO = TOOLS.parent
OUT_ROOT = REPO / "extracted" / "models" / "courses"
SCENE_PACKS = list(range(88, 135, 2))
BS = chr(92)

# ------------------------------------------------------------------ errors


class SceneError(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise SceneError(msg)


def _u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


# ------------------------------------------------------------------ names (main.exe)

G_ASSET_PATHS = 0x8007C48C        # dlabel g_AssetPaths, 6BE64.data.s:1676 (135 words to 1812)
ASSET_PATH_COUNT = 135
G_GRAND_PRIX_NAMES = 0x8007D3D8   # dlabel g_GrandPrixNames, 6BE64.data.s:2837 (11 words)
G_COURSE_NAMES = 0x8007D404       # dlabel g_CourseNames, 6BE64.data.s:2852 (4 words)
TRACK_2ND_BASE = 0x58             # race_assets.c:98-100 LoadAsset(course*2 + class*8 + 0x57), .2ND = +1

# The new-game prologue scene.  title_screen.c:275-279 / 288-292: with no save, g_GrandPrixClass = 0,
# g_CourseIndex = 3, RequestTrackLoad -> OVAL1.1ST / OVAL1.2ND (assets 93 / 94); frontend.c:28-35
# g_GrandPrixMode = 1, g_SceneId = 0x1F (EnterPrologue); prologue.c:77 g_CourseIndex = 0 before
# InitTrackScene; prologue.c:245-250 draws DrawCars, DrawSkyBackground, DrawTerrainCellsWide,
# DrawCourseObjects, DrawCourseScenery2 only (no DrawStartGridScenery, no DrawScriptedScenery).
# Course select never offers course 3 below class 2 (course_select.c:405-412).  Its blocks 3-8 are
# BIG5.2ND's (asserted in build_scene), so the scene is MYTHICAL COAST content.
PROLOGUE_CLASS, PROLOGUE_LOAD_COURSE, PROLOGUE_DRAW_COURSE = 0, 3, 0
PROLOGUE_ASSET = TRACK_2ND_BASE + PROLOGUE_CLASS * 8 + PROLOGUE_LOAD_COURSE * 2       # 94
PROLOGUE_SAME_AS = TRACK_2ND_BASE + 4 * 8 + PROLOGUE_DRAW_COURSE * 2                  # 120 = BIG5.2ND
PROLOGUE_DRAWERS = ("DrawTerrainCells", "DrawCourseObjects", "DrawAnimatedScenery", "DrawSpinningScenery",
                    "DrawHighClassScenery", "DrawStaticScenery")

# DrawRouteScenery (draw_route_scenery.c:11-33) draws bank-1 model 0x25, or model 1 when the bank has
# fewer than 0x26 models; DrawScriptedScenery switches on g_GrandPrixClass % 5, so class 5 takes case 0
# and draws it (draw_scripted_scenery.c:11-18, race_scene.c:296-301).  Class-5 packs (23 bank-1 models)
# store the route start (0, 10000, 0) for both series and every keyframe has speed 0
# (update_route_scenery.c:94-107 moves by speed only): a rival-car part parked below the map corner.
ROUTE_MODEL, ROUTE_MIN_BANK = 0x25, 0x26
ROUTE_PARKED_WORLD = (0, 10000, 0)


def exe_names(exe_path=None):
    """Name tables read from the PAL executable (file offset = addr - t_addr + 0x800)."""
    exe = Path(exe_path or rp.EXE_PATH).read_bytes()
    check(exe[:8] == b"PS-X EXE", "main.exe is not a PS-X EXE")
    taddr = _u32(exe, 0x18)

    def off(a):
        return a - taddr + 0x800

    def ptr(a):
        return _u32(exe, off(a))

    def cstr(a):
        o = off(a)
        return exe[o:exe.index(b"\0", o)].decode("ascii")

    return {"asset_paths": [cstr(ptr(G_ASSET_PATHS + 4 * i)) for i in range(ASSET_PATH_COUNT)],
            "grand_prix": [cstr(ptr(G_GRAND_PRIX_NAMES + 4 * i)) for i in range(11)],
            "courses": [cstr(ptr(G_COURSE_NAMES + 4 * i)) for i in range(4)],
            "evidence": "main.exe g_AssetPaths 0x8007C48C, g_GrandPrixNames 0x8007D3D8, "
                        "g_CourseNames 0x8007D404 (6BE64.data.s:1676, 2837, 2852)"}


def scene_names(aid, names=None):
    """Course / class / file names of one .2ND pack, cross-checked against the
    load formula, export_course_models.pack_name and rr_placement.pack_meta."""
    names = names or exe_names()
    rel = aid - TRACK_2ND_BASE
    check(0 <= rel < 48 and rel % 2 == 0, "asset %d is not a .2ND course pack" % aid)
    cls, course = rel >> 3, (rel >> 1) & 3
    path = names["asset_paths"][aid]
    fname = path.split(BS)[-1]
    check(path == BS + ".." + BS + "PACK" + BS + fname, "g_AssetPaths[%d] = %r" % (aid, path))
    check(fname.endswith("%d.2ND" % (cls + 1)), "g_AssetPaths[%d] %s does not end in class %d" % (aid, fname, cls + 1))
    prefix = fname[:-len("%d.2ND" % (cls + 1))]
    # the same prefix for every class of this course, and BIG/MID/HI/OVAL in course order
    for c2 in range(6):
        f2 = names["asset_paths"][TRACK_2ND_BASE + c2 * 8 + course * 2].split(BS)[-1]
        check(f2 == "%s%d.2ND" % (prefix, c2 + 1), "g_AssetPaths course %d class %d is %s" % (course, c2, f2))
        f1 = names["asset_paths"][TRACK_2ND_BASE - 1 + c2 * 8 + course * 2].split(BS)[-1]
        check(f1 == "%s%d.1ST" % (prefix, c2 + 1), "g_AssetPaths .1ST course %d class %d is %s" % (course, c2, f1))
    course_name = names["courses"][course]
    class_name = names["grand_prix"][cls]
    role = "race"
    file_course_name = course_name
    if aid == PROLOGUE_ASSET:
        check((cls, course) == (PROLOGUE_CLASS, PROLOGUE_LOAD_COURSE), "prologue pack %d" % aid)
        role = "prologue"
        course_name = names["courses"][PROLOGUE_DRAW_COURSE]
    # export_course_models.pack_name / rr_placement.pack_meta / rr_textures.pack_name agree
    ecm_name = ecm.pack_name(aid)
    ecm_prefix, ecm_course = ecm.COURSE_NAMES[course].split("_", 1)
    check(ecm_name == "%s_class%d_2ND" % (ecm.COURSE_NAMES[course], cls), "pack_name(%d) = %s" % (aid, ecm_name))
    check(ecm_prefix == prefix, "export_course_models course %d prefix %s != exe %s" % (course, ecm_prefix, prefix))
    check(ecm_course.lower() in file_course_name.replace(" ", "").lower(),
          "export_course_models course %d %s does not match g_CourseNames %s" % (course, ecm_course, file_course_name))
    meta = rp.pack_meta(aid)
    check((meta["course"], meta["class"], meta["half"]) == (course, cls, "2ND"), "rr_placement.pack_meta(%d)" % aid)
    check(rt.pack_course_class(aid) == (course, cls), "rr_textures.pack_course_class(%d)" % aid)
    out = {"asset": aid, "course": course, "class": cls, "file": fname, "prefix": prefix, "role": role,
           "course_name": course_name, "file_course_name": file_course_name,
           "class_name": class_name, "class_number": cls + 1,
           "course_folder": course_name.replace(" ", "_"),
           "class_folder": "CLASS%d_%s" % (cls + 1, class_name),
           "stem": fname.replace(".", "_"),
           "series1_class_name": names["grand_prix"][cls + 6] if cls + 6 < 11 else None,
           "evidence": names["evidence"] + "; result_screens.c:266-270 (CLASS%d = class + 1, "
                       "g_GrandPrixNames[series ? class + 6 : class])"}
    if role == "prologue":
        # file OVAL1.2ND (g_AssetPaths[94]) holding MYTHICAL COAST content, drawn as course 0 by the
        # new-game prologue: filed under the course it shows, folder named after the scene + file
        out["class_folder"] = "PROLOGUE_%s%d" % (prefix, cls + 1)
        out["evidence"] += ("; prologue: title_screen.c:275-292, frontend.c:28-35, prologue.c:77 and 245-250, "
                            "course_select.c:405-412; blocks 3-8 == %s (asserted)"
                            % names["asset_paths"][PROLOGUE_SAME_AS].split(BS)[-1])
    return out


# ------------------------------------------------------------------ face adapters


class TexFace(object):
    """The attributes rr_textures' coverage / compose code reads (tpage, clut =
    stored clut, uv, texwin, opcode, model, index).  `opcode` is the course
    opcode for course faces (3 = u-scroll in face_texel_rect) and 100 + op for
    terrain / bank faces so the course scroll rule never applies to them."""
    __slots__ = ("tpage", "clut", "uv", "texwin", "opcode", "model", "index", "eff_clut", "source", "stored_clut",
                 "semi")

    def __init__(self, tpage, clut, uv, texwin, opcode, model, index, eff_clut, source, stored_clut=None, semi=False):
        self.tpage, self.clut, self.uv, self.texwin = tpage, clut, uv, texwin or 0
        self.opcode, self.model, self.index = opcode, model, index
        self.eff_clut, self.source = eff_clut & 0xFFFF, source
        self.stored_clut = clut if stored_clut is None else stored_clut
        # GP0 code bit 1 (0x2E): semi-transparent; blended with tpage ABR on texels whose STP bit is set
        self.semi = bool(semi)

    def with_clut(self, clut):
        return TexFace(self.tpage, clut, self.uv, self.texwin, self.opcode, self.model, self.index,
                       self.eff_clut, self.source, self.stored_clut, self.semi)

    @property
    def abr(self):
        return (self.tpage >> 5) & 3


def window_needed(face):
    """True when the GP0(E2h) texture window changes a texel the face samples
    (then the face needs a REPEAT tile, else the page PNG is exact)."""
    if not face.texwin:
        return False
    tw = rt.texwin_info(face.texwin)
    us = [uv[0] for uv in face.uv]
    vs = [uv[1] for uv in face.uv]
    for axis, lo, hi in ((tw["u"], min(us), max(us)), (tw["v"], min(vs), max(vs))):
        for c in range(lo, hi):
            if rt.apply_texwin(c & 0xFF, axis) != c:
                return True
    return False


def strip_polys(idx):
    """PS1 quad (v0 v1 / v2 v3) -> list of corner-position tuples in OBJ order
    (reversed winding, see the module docstring).  Degenerate faces keep every
    distinct triangle of the GPU split (v0 v1 v2) + (v1 v3 v2)."""
    if len(set(idx)) == 4:
        return [(0, 2, 3, 1)]
    out = []
    seen = set()
    for tri, obj_order in (((0, 1, 2), (0, 2, 1)), ((1, 3, 2), (1, 2, 3))):
        ids = frozenset(idx[k] for k in tri)
        if len(ids) == 3 and ids not in seen:
            seen.add(ids)
            out.append(obj_order)
    return out


# ------------------------------------------------------------------ scene model


class Poly(object):
    __slots__ = ("corners", "uv", "face", "rgb", "flat")

    def __init__(self, corners, uv, face, rgb, flat):
        self.corners, self.uv, self.face, self.rgb, self.flat = corners, uv, face, rgb, flat


class SceneObject(object):
    def __init__(self, name, group, info):
        self.name, self.group, self.info = name, group, info
        self.positions = {}      # source vertex index -> world (x, y, z) PS1 units
        self.polys = []

    def face_count(self):
        return len(self.polys)


class Scene(object):
    def __init__(self, aid, names):
        self.aid = aid
        self.names = names
        self.objects = []        # the scene file
        self.variants = []       # variants.obj
        self.unplaced = []       # unplaced.obj
        self.stats = Counter()
        self.notes = []
        self.offmap = {}         # (source, model) -> game draw parked off the map (exported in unplaced.obj)


def _xf(t, m, v):
    r = rp.apply(m, v)
    return (t[0] + r[0], t[1] + r[1], t[2] + r[2])


def _add_faces(obj, positions, faces, source, scene):
    """faces: [(idx, texface or None, rgb, kind)]; positions: idx -> world position."""
    for idx, tf, rgb, kind in faces:
        polys = strip_polys(idx)
        if len(polys) != 1 or len(polys[0]) != 4:
            scene.stats["degenerate_faces_" + source] += 1
        if not polys:
            scene.stats["dropped_two_index_faces_" + source] += 1
        for order in polys:
            corners = [idx[k] for k in order]
            uv = [tf.uv[k] for k in order] if tf is not None else None
            obj.polys.append(Poly(corners, uv, tf, rgb, tf is None))
        for i in idx:
            if i not in obj.positions:
                obj.positions[i] = positions(i)


# ------------------------------------------------------------------ build


GROUPS = {"terrain_cell": "terrain", "course_model": "course_models", "bank1": "scenery", "bank2": "scenery"}


def instance_k(inst, env_mode4):
    """ctx+0x84 CLUT offset k (ctx+0x84 = k << 16) that the draw site writes before
    SubmitCourseModel/2, evaluated for the static export (see rr_textures.course_model_draws):
    DrawCourseObjects flags & 4 -> 1, flags & 8 blinks -> 0 (base palette), else 0
    (visible_cells.c:80-93); DrawHighClassScenery: 1 in env mode 4 (scenery.c:97-104);
    the animated-screen tint ((g_SceneTimer >> 3) & 3) is a cycle -> 0; all other sites 0."""
    if inst["source"] != "course_model":
        return 0       # bank scenery: every SubmitModel caller but DrawCar zeroes ctx+0x84 first
    d = inst["drawer"]
    if d.startswith("DrawCourseObjects"):
        fl = inst.get("flags", 0)
        if fl & 8:
            return 0
        return 1 if fl & 4 else 0
    if d.startswith("DrawHighClassScenery"):
        return 1 if env_mode4 else 0
    return 0


def build_scene(aid, names=None, series=0):
    names = names or exe_names()
    sn = scene_names(aid, names)
    scene = Scene(aid, sn)
    data = rt.load_asset(aid)

    # ---- race-start state
    m4 = rt.env_mode4_states(aid, data)
    env_mode4 = bool(m4["start"])
    scene.env = {"env_mode4_at_start": env_mode4, "env_mode4_states": m4["states"],
                 "env_script_t0": m4["t0"], "mode4_cue_times": m4["mode4_cue_times"],
                 "script_enabled": m4["script_enabled"],
                 "evidence": "rr_textures.env_mode4_states (environment_script.c:60-98, load_environment_cue.c:54-59)"}

    # ---- placement (rr_placement) on the shared parse
    pscene = rp.parse_pack((aid, data))
    draw_course = sn["course"]
    if sn["role"] == "prologue":
        # OVAL1.2ND is the new-game prologue scene: title_screen.c:275-292 load class 0 course 3,
        # then prologue.c:77 sets g_CourseIndex = 0 before InitTrackScene, so the course-indexed
        # drawers run as course 0 (its blocks 3-8 are BIG5.2ND's, rr_model_banks EXPECTED_SAME_BLOCKS).
        draw_course = PROLOGUE_DRAW_COURSE
        pscene = dict(pscene)
        meta = dict(pscene["meta"])
        meta["course"] = 0
        pscene["meta"] = meta
        scene.notes.append("asset 094 (OVAL1.2ND): course-indexed drawers evaluated as course 0 "
                           "(prologue.c:77 g_CourseIndex = 0 before InitTrackScene)")
    scene.draw_course = draw_course
    insts = rp.get_instances(pscene, series=series, env_mode4=env_mode4, all_conditions=True)
    if sn["role"] == "prologue":
        # the content is BIG5.2ND's (blocks 3-8 byte-identical), i.e. MYTHICAL COAST
        same = rt.load_asset(PROLOGUE_SAME_AS)
        oa, ob = rt.parse_scene_offsets(data), rt.parse_scene_offsets(same)
        for blk in range(3, 9):
            check(data[oa[blk]:oa[blk + 1]] == same[ob[blk]:ob[blk + 1]],
                  "prologue pack %d block %d differs from asset %d" % (aid, blk, PROLOGUE_SAME_AS))
        # prologue.c:245-250 draw list: only terrain, course objects and DrawCourseScenery2 (scenery.c:163-:
        # DrawAnimatedScenery2 instance 0, then the course-0 spinner / class>=4 landmark / static landmark)
        cm_count = len(pscene["course_models"])

        def cmc(i):
            return i if i < cm_count else 1
        pro = []
        for inst in insts:
            inst = dict(inst)
            if not inst["drawer"].startswith(PROLOGUE_DRAWERS):
                if inst["active"]:
                    scene.notes.append("prologue: %s not drawn (not in prologue.c:245-250)" % inst["drawer"])
                inst["active"] = False
            elif inst["drawer"].startswith("DrawAnimatedScenery") and inst["active"]:
                # DrawAnimatedScenery2(timer, 0, g_SceneId == 0x11 -> 0 in scene 0x1F, animate):
                # frame + 0x18 (16 frames) and companion variant + 7, variant = (Random15() & 7) / 3 in 0..2
                # (dynamic_track_views.c:311-415)
                inst["drawer"] = "DrawAnimatedScenery2 -> SubmitCourseModel"
                inst["evidence"] = "dynamic_track_views.c:311-415 (isReplay 0), scenery.c:163-171, prologue.c:250"
                inst["alt_models"] = sorted({cmc(0x18 + i) for i in range(16)} if inst.get("part") == "frame"
                                            else {cmc(7 + v) for v in range(3)})
            pro.append(inst)
        insts = pro

    # ---- geometry decoders
    tcells = rtc.parse_terrain_cells(data, "asset_%03d" % aid)
    cmodels = rt.parse_course_models(data)
    rt.check_op3_scroll(cmodels)
    banks = {}
    for label, b, e in rmb.locate_banks(data, aid):
        banks[label] = rmb.parse_bank(data, b, e, label, aid)
        errs = rmb.check_bank(banks[label])
        check(not errs, "asset %d %s: rr_model_banks.check_bank: %s" % (aid, label, errs[:3]))
    check(set(banks) == {"bank1", "bank2"}, "asset %d banks %s" % (aid, sorted(banks)))

    # ---- cross-check the decoders against rr_placement's parse (same bytes, same order)
    check(len(cmodels) == len(pscene["course_models"]), "course model count differs")
    for a, b in zip(cmodels, pscene["course_models"]):
        check([tuple(v[:3]) for v in a.verts] == list(b["vertices"]), "course model %d vertices differ" % a.index)
        check([f.idx for f in a.faces] == [f["idx"] for f in b["faces"]], "course model %d faces differ" % a.index)
    pt = pscene["terrain"]
    check(tcells.cell_count == pt["cell_count"] and len(tcells.vertices) == len(pt["pool"]), "terrain parse differs")
    for c, pc in zip(tcells.cells, pt["cells"]):
        check([f.indices for f in c.faces] == [f["idx"] for f in pc["faces"]], "terrain cell %d faces differ" % c.cell_id)
        if pc["translation"] is not None:
            check(tuple(pc["translation"]) == tuple(c.origin), "terrain cell %d origin %s != %s"
                  % (c.cell_id, pc["translation"], c.origin))
    for label in ("bank1", "bank2"):
        pb, bb = pscene[label], banks[label]
        check(pb["count"] == bb.count, "%s count differs" % label)
        for m, pm in zip(bb.models, pb["models"]):
            check([f.idx for f in m.faces()] == [f["idx"] for f in pm["faces"]], "%s model %d faces differ" % (label, m.index))

    # ---- face records
    def terrain_faces(cell):
        out = []
        for i, f in enumerate(cell.faces):
            # SubmitTerrainCells adds the +1 to the clut word itself before the packet is
            # written (terrain_submission.s:321-322, 602), so the GPU clut is the effective one
            eff = rtc.effective_clut(f, 1 if env_mode4 else 0)
            tf = TexFace(f.tpage, eff, f.uv, f.tex_window, 100 + f.opcode, "cell%d" % cell.cell_id, i, eff, "terrain",
                         stored_clut=f.clut)
            out.append((f.indices, tf, f.rgb, "terrain"))
        return out

    def course_faces(mi, k):
        out = []
        for f in cmodels[mi].faces:
            tf = None
            # the course emitters copy the face word +8 (RGB + GP0 code) into the packet unchanged:
            # EmitCoursePolyF4/FT4 terrain_submission.s:1622-1625 / 1656-1658, subdivided FT4 via
            # ctx+0xA0 (1722-1724 -> 2041-2044, 2085-2088), fog variants lwc2 RGBC (code kept)
            check(f.code in (0x28, 0x2A) if not f.textured else f.code in (0x2C, 0x2E),
                  "asset %d course model %d face %d: GP0 code %02X" % (aid, mi, f.index, f.code))
            if f.textured:
                tf = TexFace(f.tpage, f.clut, f.uv, f.texwin, f.opcode, "cm%d" % mi, f.index, f.clut + k, "course",
                             semi=f.semi_transparent)
            else:
                # no shipped course model has opcode-0 (F4) faces; a semi F4 would need a d < 1 material
                check(not f.semi_transparent, "asset %d course model %d face %d: semi-transparent F4 "
                      "(not representable here)" % (aid, mi, f.index))
            out.append((f.idx, tf, f.rgb, "course"))
        return out

    def bank_faces(label, mi):
        out = []
        for i, f in enumerate(banks[label].models[mi].faces()):
            if not f.drawn:          # NCLIP of v0 v1 v2 == 0: never emitted (rr_model_banks Face.drawn)
                scene.stats["bank_faces_never_drawn"] += 1
                continue
            tf = None
            rgb = f.rgb if f.rgb is not None else (128, 128, 128)
            if f.textured:
                tf = TexFace(f.tpage, f.clut, f.uv, 0, 100 + 10 + f.opcode, "%s_%d" % (label, mi), i, f.clut,
                             label)
            out.append((f.idx, tf, rgb, label))
        return out

    used = {"course_model": set(), "bank1": set(), "bank2": set()}
    counters = Counter()

    def inst_name(source, mi):
        k = counters[(source, mi)]
        counters[(source, mi)] += 1
        if source == "course_model":
            return "course_model_%02d_i%d" % (mi, k)
        return "scenery_%s_model_%02d_i%d" % (source, mi, k)

    def model_object(inst, mi, name, info_extra=None):
        src = inst["source"]
        t, m = inst["translation"], inst["matrix"]
        info = {"source": src, "model_index": mi, "drawer": inst["drawer"], "data_source": inst["data_source"],
                "condition": inst["condition"], "evidence": inst["evidence"], "kind": inst["kind"],
                "translation": list(t), "rotation": inst["rotation"], "position_world": list(inst["position_world"])}
        if info_extra:
            info.update(info_extra)
        obj = SceneObject(name, GROUPS[src], info)
        if src == "course_model":
            k = instance_k(inst, env_mode4)
            info["clut_offset_k"] = k
            verts = cmodels[mi].verts
            _add_faces(obj, lambda i: _xf(t, m, verts[i][:3]), course_faces(mi, k), "course", scene)
        else:
            bank = banks[src]
            _add_faces(obj, lambda i: _xf(t, m, bank.vertices[i][:3]), bank_faces(src, mi), src, scene)
        return obj

    for inst in insts:
        src = inst["source"]
        if src == "terrain_cell":
            check(inst["active"], "terrain instance inactive")
            cell = tcells.cells[inst["model_index"]]
            sx, sy = cell.grid_pos
            check((sx, sy) == (inst["grid_sx"], inst["grid_sy"]), "terrain cell %d grid pos" % cell.cell_id)
            ox, oy, oz = cell.origin
            obj = SceneObject("terrain_cell_%02d_%02d" % (sy, sx), "terrain",
                              {"source": "terrain_cell", "cell_id": cell.cell_id, "grid_sx": sx, "grid_sy": sy,
                               "grid_row_stored": 31 - sy, "translation": [ox, oy, oz],
                               "drawer": inst["drawer"], "data_source": inst["data_source"],
                               "evidence": inst["evidence"]})
            verts = tcells.vertices
            _add_faces(obj, lambda i: (verts[i][0] + ox, verts[i][1] + oy, verts[i][2] + oz),
                       terrain_faces(cell), "terrain", scene)
            scene.objects.append(obj)
            continue
        if not inst["active"]:
            continue
        mi = inst["model_index"]
        if (src == "bank1" and inst["drawer"].startswith("DrawRouteScenery")
                and banks["bank1"].count < ROUTE_MIN_BANK):
            # the class-5 route fallback: a real draw, but of a rival-car part parked off the map;
            # kept out of the scene, exported in unplaced.obj with the game transform recorded
            check(mi == 1, "route fallback model %d" % mi)
            r = pscene["events"]["route"]
            check(all(tuple(p) == ROUTE_PARKED_WORLD for p in r["start"]),
                  "asset %d route start %s is not the parked position" % (aid, r["start"]))
            ty_max = max(v[1] for v in tcells.vertices)
            pw = [4 * c for c in ROUTE_PARKED_WORLD]
            dmin = min(math.hypot(4 * p["x"] - pw[0], 4 * p["z"] - pw[2]) for p in pscene["track"]["points"])
            # UpdateRouteScenery moves the prop only by keyframe speed: every reachable keyframe is 0
            for first in {k for ser in r["first_keyframe"] for k in ser}:
                for step in range(256):
                    kf = rp._keyframe(data, r["keyframes_at"], first + step)
                    check(kf["speed"] == 0, "asset %d route keyframe %d speed %d" % (aid, first + step, kf["speed"]))
                    if kf["duration"] == -1:
                        break
                else:
                    raise SceneError("asset %d route keyframes from %d have no -1 terminator" % (aid, first))
            check(pw[1] > ty_max + 8192 and dmin > 32768,
                  "asset %d parked route prop: y %d vs lowest ground %d, %d from the track" % (aid, pw[1], ty_max, dmin))
            scene.offmap[(src, mi)] = {
                "drawer": inst["drawer"], "translation": list(inst["translation"]),
                "position_world": list(inst["position_world"]), "rotation": inst["rotation"],
                "condition": inst["condition"], "data_source": inst["data_source"],
                "below_lowest_ground_raw": pw[1] - ty_max, "horizontal_distance_to_track_raw": round(dmin),
                "evidence": "draw_route_scenery.c:28-33 (drawId 1 when g_ModelBankCount < 0x26), "
                            "draw_scripted_scenery.c:11-18 (class 5 % 5 = case 0), race_scene.c:296-301, "
                            "update_route_scenery.c:94-107 (moves by keyframe speed; every reachable keyframe "
                            "has speed 0, asserted)",
                "note": "drawn every frame at the class-5 parked route start, below the ground at the map corner; "
                        "not visible course scenery, so not placed in the scene file"}
            scene.notes.append("route fallback: bank1 model 1 parked at world %s (%d raw below the lowest ground, "
                               "%d raw from the track) moved to unplaced.obj"
                               % (ROUTE_PARKED_WORLD, pw[1] - ty_max, round(dmin)))
            continue
        used[src].add(mi)
        scene.objects.append(model_object(inst, mi, inst_name(src, mi)))
        # alternate frames / env-state variants drawn at this same transform
        for alt in inst.get("alt_models", []):
            if alt == mi:
                continue
            used[src].add(alt)
            scene.variants.append(model_object(inst, alt, inst_name(src, alt),
                                               {"variant_of": "%s model %d" % (src, mi),
                                                "variant_note": "drawn here instead of model %d in other animation "
                                                                "frames / env states (alt_models from rr_placement)" % mi}))
    # terrain cells not referenced by the grid (none expected)
    placed_cells = {o.info["cell_id"] for o in scene.objects if o.info["source"] == "terrain_cell"}
    check(len(placed_cells) == tcells.cell_count, "asset %d: %d of %d terrain cells placed"
          % (aid, len(placed_cells), tcells.cell_count))

    # ---- unplaced: every model with no race-time placement in this pack
    grid_items = []
    for mi in range(len(cmodels)):
        if mi not in used["course_model"]:
            grid_items.append(("course_model", mi))
    for label in ("bank1", "bank2"):
        for mi in range(banks[label].count):
            if mi not in used[label]:
                grid_items.append((label, mi))
    boxes = {}
    for src, mi in grid_items:
        if src == "course_model":
            pts = [v[:3] for v in cmodels[mi].verts]
        else:
            b = banks[src]
            pts = [b.vertices[i][:3] for i in b.models[mi].vertex_indices()] or [(0, 0, 0)]
        lo = tuple(min(p[k] for p in pts) for k in range(3))
        hi = tuple(max(p[k] for p in pts) for k in range(3))
        boxes[(src, mi)] = (lo, hi)
    # display layout only (never a game placement): shelf packing in table order along +x,
    # rows along +z, each model's x/z box corner at its slot, its y left as authored
    gap = 128
    area = sum((hi[0] - lo[0] + gap) * (hi[2] - lo[2] + gap) for lo, hi in boxes.values())
    row_w = max(max((hi[0] - lo[0] for lo, hi in boxes.values()), default=0), int(math.sqrt(max(area, 1)) * 1.2))
    slots = {}
    cx = cz = row_d = 0
    for key in grid_items:
        lo, hi = boxes[key]
        w, d = hi[0] - lo[0], hi[2] - lo[2]
        if cx > 0 and cx + w > row_w:
            cx, cz, row_d = 0, cz + row_d + gap, 0
        slots[key] = (cx - lo[0], 0, cz - lo[2])
        cx += w + gap
        row_d = max(row_d, d)
    for src, mi in grid_items:
        off = slots[(src, mi)]
        if src == "course_model":
            name = "course_model_%02d_unplaced" % mi
            role = "course model table entry %d: no race-time draw site places it in this pack" % mi
        else:
            name = "%s_model_%02d_unplaced" % (src, mi)
            role = rmb.model_role(banks[src], mi)
        info = {"source": src, "model_index": mi, "display_offset": list(off), "role": role,
                "note": "not placed by any race-time drawer for this pack; position is a display grid only"}
        if (src, mi) in scene.offmap:
            info["note"] = "position is a display grid only; the game's own (off-map) draw is in game_draw_offmap"
            info["game_draw_offmap"] = scene.offmap[(src, mi)]
        obj = SceneObject(name, "unplaced", info)
        if src == "course_model":
            verts = cmodels[mi].verts
            _add_faces(obj, lambda i: (verts[i][0] + off[0], verts[i][1] + off[1], verts[i][2] + off[2]),
                       course_faces(mi, 0), "course", scene)
        else:
            b = banks[src]
            _add_faces(obj, lambda i: (b.vertices[i][0] + off[0], b.vertices[i][1] + off[1], b.vertices[i][2] + off[2]),
                       bank_faces(src, mi), src, scene)
        scene.unplaced.append(obj)

    # ---- accounting: every model of the pack appears in exactly one role set
    shown = Counter()
    for o in scene.objects + scene.variants + scene.unplaced:
        if o.info["source"] != "terrain_cell":
            shown[(o.info["source"], o.info["model_index"])] += 1
    for mi in range(len(cmodels)):
        check(shown[("course_model", mi)] >= 1, "course model %d missing from every file" % mi)
    for label in ("bank1", "bank2"):
        for mi in range(banks[label].count):
            check(shown[(label, mi)] >= 1, "%s model %d missing from every file" % (label, mi))
    unplaced_keys = {(o.info["source"], o.info["model_index"]) for o in scene.unplaced}
    check(set(scene.offmap) <= unplaced_keys, "off-map draws %s not in unplaced.obj" % sorted(set(scene.offmap) - unplaced_keys))
    check(not any((o.info["source"], o.info.get("model_index")) in scene.offmap for o in scene.objects),
          "an off-map draw is still in the scene file")
    names_seen = Counter(o.name for o in scene.objects + scene.variants + scene.unplaced)
    dup = [n for n, c in names_seen.items() if c > 1]
    check(not dup, "duplicate object names %s" % dup[:5])

    scene.tcells, scene.cmodels, scene.banks, scene.pscene, scene.insts = tcells, cmodels, banks, pscene, insts
    scene.env_mode4 = env_mode4
    scene.series = series
    return scene


# ------------------------------------------------------------------ materials / textures


def flat_material(rgb):
    return "flat_%02X%02X%02X" % tuple(rgb)


def page_material(tpage, clut, semi=False):
    """semi: GP0 0x2E faces (tpage ABR 0) get their own material, '_semi_abr0', whose PNG has
    alpha 128 on STP texels."""
    return "tpage_%04X_clut_%04X%s" % (tpage, clut, "_semi_abr%d" % ((tpage >> 5) & 3) if semi else "")


def resolve_materials(scene, rv, tex_dir, write=True):
    """Compose one 256x256 page per (tpage, effective clut) from the race VRAM
    (rr_textures.compose_pair_texture, per-face page 0/1 choice by TIM pairing);
    if faces of one pair need different VRAM pages for the same texels, split
    into _page0 / _page1 materials.  Texture-window faces get a tile crop."""
    allobjs = scene.objects + scene.variants + scene.unplaced
    faces = []
    seen = set()
    for o in allobjs:
        for p in o.polys:
            if p.face is not None and id(p.face) not in seen:
                seen.add(id(p.face))
                faces.append((p.face, o in scene.unplaced))
    for f, _u in faces:
        ti = rt.tpage_info(f.tpage)
        check(ti["depth"] == 0 and ti["extra"] == 0, "%s face %s/%s: tpage %04X not 4-bit" % (f.source, f.model, f.index, f.tpage))
    cov_cache = {}
    covs = {}

    def coverage(f):
        key = (f.clut, rt.face_vram_rect(f, False))
        if key not in cov_cache:
            cov_cache[key] = rt.face_coverage(rv, f)
        return cov_cache[key]

    stats = Counter()
    kind_of = {}
    for f, unpl in faces:
        cov = coverage(f)
        pn, how = rt._face_page(cov)
        if pn is None and f.stored_clut != f.clut:
            # terrain clut+1 face whose texel words belong to the TIM of the stored CLUT
            # (env mode 4 entries 1/3 on opcodes 0/1): page choice from the stored CLUT's TIM,
            # sampled through clut+1 like a course-model k variant
            alt = coverage(f.with_clut(f.stored_clut))
            if rt._face_page(alt)[0] is not None:
                cov, how = alt, "palette_variant"
                pn = rt._face_page(alt)[0]
        if pn is None and all(c == "teamlogo" for c in cov[0]["clut"]):
            # the player's team logo (menu canvas + g_TeamLogoClut, asset_requests.c:56-64) left in
            # VRAM: a runtime texture, exported as VRAM holds it at race start
            how = "team_logo"
        covs[id(f)] = cov
        kind_of[id(f)] = how or "uncovered"
        stats["faces_%s_%s" % ("unplaced" if unpl else "placed", how or "uncovered")] += 1
    # every face drawn in the scene / variants samples race-written texels (or the team logo)
    check(stats["faces_placed_uncovered"] == 0, "asset %d: %d placed faces sample VRAM the race load never wrote"
          % (scene.aid, stats["faces_placed_uncovered"]))
    groups = defaultdict(list)
    placed_pair = set()
    for f, unpl in faces:
        if f.semi:
            # only ABR 0 (0.5*B + 0.5*F) is representable as a straight alpha; it is the only mode used
            check(f.abr == 0, "asset %d %s face %s/%s: semi-transparent with ABR %d"
                  % (scene.aid, f.source, f.model, f.index, f.abr))
        groups[(f.tpage, f.eff_clut, f.semi)].append(f)
        if not unpl and kind_of[id(f)] != "team_logo":
            placed_pair.add((f.tpage, f.eff_clut))
    mats = {}           # name -> info
    face_mat = {}       # id(face) -> material name
    tiles_done = {}

    def emit(name, tp, eff, fl, rgba, info, stp=None):
        semi = fl[0].semi
        if semi:
            # GP0 0x2E: texels with STP set are blended 0.5*B + 0.5*F (tpage ABR 0), texels without STP
            # stay opaque, 0x0000 stays transparent -> alpha 128 / 255 / 0 in this material's PNG
            rgba = bytearray(rgba)
            for i, s in enumerate(stp):
                if s and rgba[i * 4 + 3]:
                    rgba[i * 4 + 3] = 128
            rgba = bytes(rgba)
            rects = set()
            for f in fl:
                u0, u1, v0, v1 = rt.face_texel_rect(f, inclusive=False)
                for v in range(v0, v1 + 1):
                    rects.update(range(v * 256 + u0, v * 256 + u1 + 1))
            info = dict(info)
            info["semi_transparent"] = {
                "gp0_code": "2E", "abr": 0, "blend": "0.5*background + 0.5*texel on STP texels (alpha 128)",
                "stp_texels_page": sum(1 for i, s in enumerate(stp) if s and rgba[i * 4 + 3]),
                "stp_texels_in_face_rects": sum(1 for i in rects if stp[i] and rgba[i * 4 + 3]),
                "evidence": "course face word +8 copied to the packet (terrain_submission.s:1656-1658, "
                            "1722-1724, 2041-2044); PS1 GPU semi-transparency applies to STP texels only"}
            stats["semi_transparent_faces"] += len(fl)
        mats[name] = dict(info)
        mats[name].update({"kind": "page", "tpage": "%04X" % tp, "clut": "%04X" % eff,
                           "png": "textures/%s.png" % name, "faces": len(fl), "semi": semi,
                           "sources": dict(Counter(f.source for f in fl)),
                           "transparent_texels": sum(1 for i in range(3, len(rgba), 4) if rgba[i] == 0)})
        if write:
            rt.write_png(tex_dir / ("%s.png" % name), 256, 256, rgba)
        for f in fl:
            if window_needed(f):
                tw = rt.texwin_info(f.texwin)
                check(tw["u"]["regular"] and tw["v"]["regular"], "irregular texture window %08X" % f.texwin)
                tname = "%s_tw_%05X" % (name, f.texwin & 0xFFFFF)
                if tname not in tiles_done:
                    pu, pv = tw["u"]["period"], tw["v"]["period"]
                    bu, bv = tw["u"]["base"], tw["v"]["base"]
                    tiles_done[tname] = (pu, pv)
                    mats[tname] = {"kind": "tile", "tpage": "%04X" % tp, "clut": "%04X" % eff,
                                   "texwin": "%08X" % f.texwin, "u_base": bu, "v_base": bv,
                                   "u_period": pu, "v_period": pv, "png": "textures/%s.png" % tname,
                                   "faces": 0, "page_material": name, "semi": semi}
                    if write:
                        rt.write_png(tex_dir / ("%s.png" % tname), pu, pv, rt.crop_rgba(rgba, 256, bu, bv, pu, pv))
                mats[tname]["faces"] += 1
                face_mat[id(f)] = tname
                stats["tile_faces"] += 1
            else:
                face_mat[id(f)] = name

    for (tp, eff, semi), fl in sorted(groups.items()):
        k = (eff - fl[0].clut) & 0xFFFF
        rgba, stp, info = rt.compose_pair_texture(rv, fl, [covs[id(f)] for f in fl], k=k)
        cr = rt.clut_row_status(rv, eff)
        info["clut_row"] = {"race_written": cr["race_written"], "pages_equal": cr["pages_equal"],
                            "tim_sources": cr["tim_sources"]}
        cx, cy = rt.clut_xy(eff)
        unwritten = all(set(rt.classify(rv.page(pn), rt.rect_owners(rv.page(pn), cx, cy, cx + 15, cy))) == {"unwritten"}
                        for pn in (0, 1))
        if (tp, eff) in placed_pair and unwritten and all(f.stored_clut != f.clut or f.eff_clut != f.clut for f in fl):
            # a clut+1 / k variant whose palette row nothing ever writes (VRAM cleared = 0x0000 =
            # transparent): the faces vanish in this state (OVER PASS CITY env mode 4, row 7BD1);
            # exported as drawn, with the fully transparent page
            stats["pairs_unwritten_variant_clut_row"] += 1
            stats["faces_placed_unwritten_variant_clut_row"] += len(fl)
            info["clut_row"]["unwritten_variant_row"] = True
        elif (tp, eff) in placed_pair:
            # every CLUT row a placed face samples must be a race-written .1ST TIM CLUT on both pages
            check(cr["race_written"] and cr["pages_equal"] and cr["tim_sources"],
                  "asset %d %s: CLUT row %04X is not a race-written .1ST TIM CLUT on both pages (%s)"
                  % (scene.aid, page_material(tp, eff), eff, cr))
        else:
            stats["pairs_only_unplaced"] += 1
        if info["conflict_texels"] == 0:
            emit(page_material(tp, eff, semi), tp, eff, fl, rgba, info, stp)
            continue
        stats["split_pairs"] += 1
        by_page = defaultdict(list)
        for f in fl:
            pn, _how = rt._face_page(covs[id(f)])
            by_page[1 if pn == 1 else 0].append(f)
        for pn, sub in sorted(by_page.items()):
            rgba2, stp2, info2 = rt.compose_pair_texture(rv, sub, [covs[id(f)] for f in sub], k=(eff - sub[0].clut) & 0xFFFF)
            check(info2["conflict_texels"] == 0, "asset %d pair %04X/%04X page %d still conflicts" % (scene.aid, tp, eff, pn))
            info2["clut_row"] = info["clut_row"]
            info2["split_from_conflicts"] = info["conflict_texels"]
            emit(page_material(tp, eff, semi) + "_page%d" % pn, tp, eff, sub, rgba2, info2, stp2)
    for name, m in mats.items():
        if m["kind"] == "page":
            stats["faces_uncovered"] += m.get("faces_uncovered", 0)
            stats["faces_exact"] += m.get("faces_exact", 0)
            stats["faces_race_only"] += m.get("faces_race_only", 0)
    # flat materials
    for o in allobjs:
        for p in o.polys:
            if p.flat:
                n = flat_material(p.rgb)
                if n not in mats:
                    mats[n] = {"kind": "flat", "rgb": list(p.rgb), "faces": 0}
                mats[n]["faces"] += 1
    scene.materials = mats
    scene.face_mat = face_mat
    scene.face_kind = kind_of
    scene.tex_stats = stats
    return mats


def texture_coverage(scene, obj):
    """Per object: textured faces by what the race VRAM holds at their texels
    (rr_textures face coverage: exact = a .1ST TIM of this tpage/clut, race = race-written
    but not one TIM, uncovered = never written by the race load, i.e. stale/cleared VRAM)."""
    seen = set()
    c = Counter()
    for p in obj.polys:
        if p.face is not None and id(p.face) not in seen:
            seen.add(id(p.face))
            c[scene.face_kind[id(p.face)]] += 1
        elif p.face is None and id(p) not in seen:
            seen.add(id(p))
            c["flat_polys"] += 1
    return dict(c)


# ------------------------------------------------------------------ OBJ / MTL writing


def _fmt(x):
    r = round(x)
    if abs(x - r) < 1e-6:
        return "%d" % r
    return "%.3f" % x


def poly_material(scene, p):
    if p.flat:
        return flat_material(p.rgb)
    return scene.face_mat[id(p.face)]


def write_obj(scene, objs, path, mtl_name, header):
    mats = scene.materials
    lines = list(header)
    lines.append("mtllib %s" % mtl_name)
    vbase = 1
    tbase = 1
    nface = 0
    cur_group = None
    for o in objs:
        if o.group != cur_group:
            # one group line per section, before its objects: with Blender's default split by
            # object the 'o' names win; with split by group as well the empty group geometry is
            # replaced by the next 'o' (blender_import_course.py builds collections from these)
            cur_group = o.group
            lines.append("g %s" % o.group)
        lines.append("# %s" % json.dumps(o.info, sort_keys=True, default=str))
        lines.append("o %s" % o.name)
        # vertices: one per (source index, colour)
        vid = {}
        vout = []
        for p in o.polys:
            for i in p.corners:
                key = (i, p.rgb)
                if key not in vid:
                    vid[key] = vbase + len(vout)
                    vout.append(key)
        for i, rgb in vout:
            x, y, z = o.positions[i]
            lines.append("v %s %s %s %.4f %.4f %.4f" % (_fmt(x), _fmt(z), _fmt(-y),
                                                        rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0))
        cur_mtl = None
        nt = 0
        for p in o.polys:
            mname = poly_material(scene, p)
            if mname != cur_mtl:
                lines.append("usemtl %s" % mname)
                cur_mtl = mname
            if p.flat:
                lines.append("f " + " ".join(str(vid[(i, p.rgb)]) for i in p.corners))
            else:
                m = mats[mname]
                if m["kind"] == "tile":
                    pu, pv = float(m["u_period"]), float(m["v_period"])
                else:
                    pu = pv = 256.0
                refs = []
                for i, (u, v) in zip(p.corners, p.uv):
                    lines.append("vt %.6f %.6f" % (u / pu, 1.0 - v / pv))
                    refs.append("%d/%d" % (vid[(i, p.rgb)], tbase + nt))
                    nt += 1
                lines.append("f " + " ".join(refs))
            nface += 1
        vbase += len(vout)
        tbase += nt
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n")
    return {"vertices": vbase - 1, "vt": tbase - 1, "faces": nface, "objects": len(objs)}


def write_mtl(scene, path, used):
    lines = ["# Rage Racer PAL course materials, asset %03d (%s)" % (scene.aid, scene.names["file"]),
             "# page: 256x256 RGBA of the 4-bit tpage through the effective CLUT (texel 0x0000 = alpha 0)",
             "# tile: GP0(E2h) texture-window tile, vt in tile periods (REPEAT)",
             "# *_semi_abr0: GP0 0x2E semi-transparent faces; STP texels blend 0.5*B + 0.5*F -> PNG alpha 128",
             "#   (use alpha blending, not alpha clip; opaque texels alpha 255, texel 0x0000 alpha 0)",
             "# flat: untextured F4/G4 colour (Kd = rgb/255)"]
    for name in sorted(used):
        m = scene.materials[name]
        lines.append("")
        if m.get("semi"):
            lines.append("# semi-transparent (GP0 0x2E, tpage ABR 0): alpha 128 = 50% blend on STP texels")
        lines.append("newmtl %s" % name)
        lines.append("Ka 0.000 0.000 0.000")
        if m["kind"] == "flat":
            r, g, b = m["rgb"]
            lines.append("Kd %.4f %.4f %.4f" % (r / 255.0, g / 255.0, b / 255.0))
            lines.append("Ks 0.000 0.000 0.000")
            lines.append("d 1.0")
            lines.append("illum 1")
        else:
            lines.append("Kd 1.000 1.000 1.000")
            lines.append("Ks 0.000 0.000 0.000")
            lines.append("d 1.0")
            lines.append("illum 1")
            lines.append("map_Kd %s" % m["png"])
            lines.append("map_d %s" % m["png"])
    Path(path).write_text("\n".join(lines) + "\n")


# ------------------------------------------------------------------ preview rasteriser (stdlib)


def read_png(path):
    """8-bit RGBA / RGB PNG -> (w, h, bytearray RGBA)."""
    b = Path(path).read_bytes()
    check(b[:8] == b"\x89PNG\r\n\x1a\n", "%s is not a PNG" % path)
    p = 8
    idat = bytearray()
    w = h = ct = None
    while p < len(b):
        n = struct.unpack_from(">I", b, p)[0]
        tag = b[p + 4:p + 8]
        body = b[p + 8:p + 8 + n]
        if tag == b"IHDR":
            w, h, bd, ct, _c, _f, il = struct.unpack(">IIBBBBB", body)
            check(bd == 8 and ct in (2, 6) and il == 0, "%s: unsupported PNG" % path)
        elif tag == b"IDAT":
            idat += body
        p += 12 + n
    bpp = 4 if ct == 6 else 3
    raw = zlib.decompress(bytes(idat))
    stride = w * bpp
    out = bytearray(w * h * bpp)
    prev = bytearray(stride)
    q = 0
    for y in range(h):
        ft = raw[q]
        row = bytearray(raw[q + 1:q + 1 + stride])
        q += 1 + stride
        if ft == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(prev[i] - c), abs(a - c), abs(a + prev[i] - 2 * c)
                pr = a if pa <= pb and pa <= pc else (prev[i] if pb <= pc else c)
                row[i] = (row[i] + pr) & 255
        out[y * stride:(y + 1) * stride] = row
        prev = row
    if bpp == 3:
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            rgba[4 * i:4 * i + 3] = out[3 * i:3 * i + 3]
            rgba[4 * i + 3] = 255
        out = rgba
    return w, h, out


def read_mtl(path):
    mats = {}
    cur = None
    for line in Path(path).read_text().splitlines():
        t = line.split()
        if not t:
            continue
        if t[0] == "newmtl":
            cur = mats.setdefault(t[1], {})
        elif t[0] == "Kd" and cur is not None:
            cur["Kd"] = tuple(float(x) for x in t[1:4])
        elif t[0] == "map_Kd" and cur is not None:
            cur["map_Kd"] = t[-1]
        elif t[0] == "map_d" and cur is not None:
            cur["map_d"] = t[-1]
    return mats


def read_obj(path):
    """-> {verts, cols, vts, faces: [(mtl, [(vi, ti)], object)], mtllib}"""
    verts, cols, vts, faces = [], [], [], []
    mtl = obj = None
    mtllib = None
    with open(path) as fh:
        for line in fh:
            if not line or line[0] == "#":
                continue
            t = line.split()
            if not t:
                continue
            k = t[0]
            if k == "v":
                verts.append((float(t[1]), float(t[2]), float(t[3])))
                cols.append((float(t[4]), float(t[5]), float(t[6])) if len(t) >= 7 else (1.0, 1.0, 1.0))
            elif k == "vt":
                vts.append((float(t[1]), float(t[2])))
            elif k == "f":
                cs = []
                for c in t[1:]:
                    s = c.split("/")
                    cs.append((int(s[0]) - 1, int(s[1]) - 1 if len(s) > 1 and s[1] else None))
                faces.append((mtl, cs, obj))
            elif k == "usemtl":
                mtl = t[1]
            elif k == "o":
                obj = t[1]
            elif k == "mtllib":
                mtllib = t[1]
    return {"verts": verts, "cols": cols, "vts": vts, "faces": faces, "mtllib": mtllib}


def _normal(ps):
    nx = ny = nz = 0.0
    n = len(ps)
    for i in range(n):
        a, b = ps[i], ps[(i + 1) % n]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    ln = math.sqrt(nx * nx + ny * ny + nz * nz)
    return (nx / ln, ny / ln, nz / ln) if ln > 0 else (0.0, 0.0, 0.0)


def render_view(obj_paths, out_png, view="top", size=1024, region=None, az=-35.0, el=35.0,
                tint_backfaces=False, cull=False, bg=(24, 26, 32)):
    """Orthographic textured render of OBJ files (Blender axes).  view 'top' looks
    down -Z; '3q' is a three-quarter view (azimuth az, elevation el in degrees).
    region = (xmin, ymin, xmax, ymax) in Blender XY limits the faces drawn.
    cull drops faces whose OBJ normal points away from the viewer (as the game's
    NCLIP test does); tint_backfaces paints back-facing winners magenta.
    Returns stats {pixels, backface_pixels (or culled_faces), triangles, textures}."""
    if view == "top":
        right, up, toward = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    else:
        a, e = math.radians(az), math.radians(el)
        toward = (math.cos(e) * math.sin(a), -math.cos(e) * math.cos(a), math.sin(e))
        right = (math.cos(a), math.sin(a), 0.0)
        up = (toward[1] * right[2] - toward[2] * right[1], toward[2] * right[0] - toward[0] * right[2],
              toward[0] * right[1] - toward[1] * right[0])
    light = (0.35, 0.25, 0.9)
    ll = math.sqrt(sum(c * c for c in light))
    light = tuple(c / ll for c in light)
    tris = []
    tex_cache = {}
    nback = 0
    for path in obj_paths:
        path = Path(path)
        ob = read_obj(path)
        mats = read_mtl(path.parent / ob["mtllib"]) if ob["mtllib"] else {}
        V, C, T = ob["verts"], ob["cols"], ob["vts"]
        for mname, cs, _o in ob["faces"]:
            ps = [V[vi] for vi, _ti in cs]
            if region is not None:
                cx = sum(p[0] for p in ps) / len(ps)
                cy = sum(p[1] for p in ps) / len(ps)
                if not (region[0] <= cx <= region[2] and region[1] <= cy <= region[3]):
                    continue
            m = mats.get(mname, {})
            tex = None
            if "map_Kd" in m:
                tp = (path.parent / m["map_Kd"]).resolve()
                if tp not in tex_cache:
                    tex_cache[tp] = read_png(tp)
                tex = tex_cache[tp]
            n = _normal(ps)
            facing = n[0] * toward[0] + n[1] * toward[1] + n[2] * toward[2]
            shade = 0.72 + 0.28 * max(0.0, abs(n[0] * light[0] + n[1] * light[1] + n[2] * light[2]))
            col = C[cs[0][0]]
            if tex is not None:
                mod = (min(2.0, col[0] * 2.0) * shade, min(2.0, col[1] * 2.0) * shade, min(2.0, col[2] * 2.0) * shade)
            else:
                kd = m.get("Kd", col)
                mod = (kd[0] * 255 * shade, kd[1] * 255 * shade, kd[2] * 255 * shade)
            back = facing < -1e-6
            if back and cull:
                nback += 1
                continue
            sp = [(p[0] * right[0] + p[1] * right[1] + p[2] * right[2],
                   p[0] * up[0] + p[1] * up[1] + p[2] * up[2],
                   p[0] * toward[0] + p[1] * toward[1] + p[2] * toward[2]) for p in ps]
            uvs = [T[ti] if (ti is not None and tex is not None) else (0.0, 0.0) for _vi, ti in cs]
            for k in range(1, len(cs) - 1):
                tris.append((sp[0], sp[k], sp[k + 1], uvs[0], uvs[k], uvs[k + 1], tex, mod, back,
                             "_semi_" in mname))
    if not tris:
        return {"pixels": 0, "backface_pixels": 0, "faces": 0}
    # semi-transparent faces after the opaque ones: alpha 1..254 texels blend over what is drawn
    tris.sort(key=lambda t: t[9])
    xs = [p[0] for t in tris for p in t[:3]]
    ys = [p[1] for t in tris for p in t[:3]]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    span = max(x1 - x0, y1 - y0) or 1.0
    scale = (size - 8) / span
    W = int((x1 - x0) * scale) + 8
    H = int((y1 - y0) * scale) + 8
    img = bytearray(bytes(bg) * (W * H))
    zb = array("d", [-1e30]) * (W * H)
    bk = bytearray(W * H)
    nblend = 0
    for (a, b, c, ta, tb, tc, tex, mod, back, _semi) in tris:
        ax, ay = (a[0] - x0) * scale + 4, (y1 - a[1]) * scale + 4
        bx, by = (b[0] - x0) * scale + 4, (y1 - b[1]) * scale + 4
        cx, cy = (c[0] - x0) * scale + 4, (y1 - c[1]) * scale + 4
        area = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        if abs(area) < 1e-9:
            continue
        inv = 1.0 / area
        minx, maxx = max(0, int(min(ax, bx, cx))), min(W - 1, int(max(ax, bx, cx)) + 1)
        miny, maxy = max(0, int(min(ay, by, cy))), min(H - 1, int(max(ay, by, cy)) + 1)
        if tex is not None:
            tw_, th_, tpx = tex
        for py in range(miny, maxy + 1):
            fy = py + 0.5
            for px in range(minx, maxx + 1):
                fx = px + 0.5
                w0 = ((bx - fx) * (cy - fy) - (cx - fx) * (by - fy)) * inv
                w1 = ((cx - fx) * (ay - fy) - (ax - fx) * (cy - fy)) * inv
                w2 = 1.0 - w0 - w1
                if w0 < -1e-9 or w1 < -1e-9 or w2 < -1e-9:
                    continue
                z = w0 * a[2] + w1 * b[2] + w2 * c[2]
                o = py * W + px
                if z <= zb[o]:
                    continue
                if tex is not None:
                    u = w0 * ta[0] + w1 * tb[0] + w2 * tc[0]
                    v = w0 * ta[1] + w1 * tb[1] + w2 * tc[1]
                    tx = int(math.floor(u * tw_)) % tw_
                    ty = int(math.floor((1.0 - v) * th_)) % th_
                    q = (ty * tw_ + tx) * 4
                    al = tpx[q + 3]
                    if al == 0:
                        continue
                    r = min(255, int(tpx[q] * mod[0]))
                    g = min(255, int(tpx[q + 1] * mod[1]))
                    bb = min(255, int(tpx[q + 2] * mod[2]))
                    if al < 255:
                        # semi-transparent texel: blend over the pixel, keep its depth
                        if back and tint_backfaces:
                            r, g, bb = (r + 255) >> 1, g >> 1, (bb + 255) >> 1
                        o3 = o * 3
                        img[o3] = (img[o3] * (255 - al) + r * al) // 255
                        img[o3 + 1] = (img[o3 + 1] * (255 - al) + g * al) // 255
                        img[o3 + 2] = (img[o3 + 2] * (255 - al) + bb * al) // 255
                        nblend += 1
                        continue
                else:
                    r, g, bb = min(255, int(mod[0])), min(255, int(mod[1])), min(255, int(mod[2]))
                if back and tint_backfaces:
                    r, g, bb = (r + 255) >> 1, g >> 1, (bb + 255) >> 1
                zb[o] = z
                img[o * 3:o * 3 + 3] = bytes((r, g, bb))
                bk[o] = 1 if back else 0
    covered = sum(1 for z in zb if z > -1e29)
    culled = nback
    nback = sum(bk)
    rgba = bytearray(W * H * 4)
    rgba[0::4] = img[0::3]
    rgba[1::4] = img[1::3]
    rgba[2::4] = img[2::3]
    rgba[3::4] = b"\xff" * (W * H)
    rt.write_png(out_png, W, H, bytes(rgba))
    return {"pixels": covered, "backface_pixels": nback, "culled_faces": culled, "triangles": len(tris),
            "size": [W, H], "textures": len(tex_cache), "blended_pixels": nblend}


def downscale(w, h, rgba, f):
    W, H = w // f, h // f
    out = bytearray(W * H * 4)
    for y in range(H):
        for x in range(W):
            acc = [0, 0, 0, 0]
            for dy in range(f):
                base = ((y * f + dy) * w + x * f) * 4
                for dx in range(f):
                    q = base + dx * 4
                    acc[0] += rgba[q]
                    acc[1] += rgba[q + 1]
                    acc[2] += rgba[q + 2]
                    acc[3] += rgba[q + 3]
            n = f * f
            o = (y * W + x) * 4
            out[o:o + 4] = bytes((acc[0] // n, acc[1] // n, acc[2] // n, acc[3] // n))
    return W, H, out


def contact_sheet(pngs, out_png, cell=256, cols=6, bg=(10, 10, 12)):
    rows = (len(pngs) + cols - 1) // cols
    W, H = cols * cell, rows * cell
    out = bytearray(bytes(bg + (255,)) * (W * H))
    for n, p in enumerate(pngs):
        w, h, px = read_png(p)
        f = max(1, int(math.ceil(max(w, h) / float(cell))))
        if f > 1:
            w, h, px = downscale(w, h, px, f)
        r, c = divmod(n, cols)
        for y in range(min(h, cell)):
            o = ((r * cell + y) * W + c * cell) * 4
            out[o:o + min(w, cell) * 4] = px[y * w * 4:y * w * 4 + min(w, cell) * 4]
    rt.write_png(out_png, W, H, bytes(out))


# ------------------------------------------------------------------ checks on the written files


def check_written_scene(scene, obj_path, mtl_path):
    """Re-read the written scene OBJ and assert: every face's material exists in
    the MTL, every texture file exists, every textured face has vt, the terrain
    cells tile (track points lie on the written terrain), face normals of the
    ground point up (winding), and the cells do not overlap."""
    ob = read_obj(obj_path)
    mats = read_mtl(mtl_path)
    base = Path(obj_path).parent
    missing = set()
    for name, m in mats.items():
        if "map_Kd" in m:
            check((base / m["map_Kd"]).is_file(), "texture %s missing" % m["map_Kd"])
            w, h, _px = read_png(base / m["map_Kd"])
            if "_tw_" not in name:
                check((w, h) == (256, 256), "%s is %dx%d, not 256x256" % (m["map_Kd"], w, h))
    tex_faces = notex = 0
    terrain_tris = []
    terrain_nc = []
    up = down = 0
    for mname, cs, oname in ob["faces"]:
        if mname not in mats:
            missing.add(mname)
            continue
        if "map_Kd" in mats[mname]:
            check(all(ti is not None for _vi, ti in cs), "textured face without vt in %s" % oname)
            tex_faces += 1
        else:
            notex += 1
        if oname.startswith("terrain_cell_"):
            ps = [ob["verts"][vi] for vi, _ti in cs]
            n = _normal(ps)
            if n[2] > 0.2:
                up += 1
            elif n[2] < -0.2:
                down += 1
            terrain_nc.append((n, [sum(p[k] for p in ps) / len(ps) for k in range(3)]))
            for k in range(1, len(ps) - 1):
                terrain_tris.append((ps[0], ps[k], ps[k + 1]))
    check(not missing, "materials missing from the MTL: %s" % sorted(missing)[:5])
    check(up > 2 * down, "terrain winding: %d faces up, %d down (expected mostly up)" % (up, down))
    # track points (block 4, world units *4) on the written terrain: Blender (x, z, -y)
    buckets = defaultdict(list)
    B = 2048
    for t in terrain_tris:
        xs = [p[0] for p in t]
        ys = [p[1] for p in t]
        for bx in range(int(min(xs)) // B, int(max(xs)) // B + 1):
            for by in range(int(min(ys)) // B, int(max(ys)) // B + 1):
                buckets[(bx, by)].append(t)
    on = near = 0
    pts = scene.pscene["track"]["points"]
    for p in pts:
        X, Y, Z = 4 * p["x"], 4 * p["z"], -4 * p["y"]
        best = None
        for (a, b, c) in buckets.get((int(X) // B, int(Y) // B), ()):
            d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if d == 0:
                continue
            l1 = ((b[1] - c[1]) * (X - c[0]) + (c[0] - b[0]) * (Y - c[1])) / d
            l2 = ((c[1] - a[1]) * (X - c[0]) + (a[0] - c[0]) * (Y - c[1])) / d
            l3 = 1 - l1 - l2
            if min(l1, l2, l3) < -1e-9:
                continue
            h = l1 * a[2] + l2 * b[2] + l3 * c[2]
            if best is None or abs(h - Z) < abs(best - Z):
                best = h
        if best is not None:
            on += 1
            if abs(best - Z) <= 64:
                near += 1
    # winding against the game camera: a terrain face within two cells of the track should face
    # a camera 600 units above the nearest track point (inverted winding gives the complement)
    tb = defaultdict(list)
    tpts = [(4 * p["x"], 4 * p["z"], -4 * p["y"]) for p in pts]
    for q in tpts:
        tb[(int(q[0]) // 16384, int(q[1]) // 16384)].append(q)
    cam_front = cam_back = 0
    for n, c in terrain_nc:
        bx, by = int(c[0]) // 16384, int(c[1]) // 16384
        cand = [q for dx in (-1, 0, 1) for dy in (-1, 0, 1) for q in tb.get((bx + dx, by + dy), ())]
        if not cand:
            continue
        q = min(cand, key=lambda q: (q[0] - c[0]) ** 2 + (q[1] - c[1]) ** 2)
        if (q[0] - c[0]) ** 2 + (q[1] - c[1]) ** 2 > 16384 ** 2:
            continue
        d = (q[0] - c[0], q[1] - c[1], q[2] + 600 - c[2])
        dot = n[0] * d[0] + n[1] * d[1] + n[2] * d[2]
        if dot > 0:
            cam_front += 1
        elif dot < 0:
            cam_back += 1
    check(cam_front >= 0.6 * (cam_front + cam_back), "terrain winding vs track camera: %d front, %d back"
          % (cam_front, cam_back))
    check(on >= 0.97 * len(pts), "only %d of %d track points lie on the written terrain" % (on, len(pts)))
    check(near >= 0.95 * len(pts), "only %d of %d track points within 64 units of the written terrain" % (near, len(pts)))
    # cell tiling: each cell's grid square holds its own cell (unique), seam stats from rr_placement
    seam = rp.terrain_seam_stats(scene.pscene)
    tot = seam["exact"] + seam["plus1_along_axis"] + seam["other_le2"]
    check(tot > 0 and seam["other_le2"] <= 0.05 * tot, "terrain seams: %s" % seam)
    return {"textured_faces": tex_faces, "flat_faces": notex, "terrain_up": up, "terrain_down": down,
            "track_points": len(pts), "track_on_terrain": on, "track_within_64": near, "seams": seam,
            "terrain_facing_track_camera": cam_front, "terrain_away_from_track_camera": cam_back}


# ------------------------------------------------------------------ Blender import helper

BLENDER_HELPER = '''"""Import Rage Racer course scene OBJ files (tools/export_course_scenes.py) into Blender 4.x.

Run:  blender --python blender_import_course.py -- <SCENE.obj> [variants.obj] [unplaced.obj]
or open this file in Blender's Text Editor, fill PATHS and press Run Script.

Each file becomes a collection with one child collection per OBJ group (terrain,
course_models, scenery, unplaced).  The OBJ files are written in Blender axes
(X = PS1 x, Y = PS1 z, Z = -PS1 y), so they are imported with forward Y, up Z.

PS1 colour of a textured face: shown = min(1, texel * colour_byte / 128) per channel.
The OBJ vertex colour is colour_byte / 255, and Blender's OBJ importer decodes it
from sRGB to linear.  So this helper reads it back with color_srgb, stores the raw
factor colour_byte / 128 in the float colour attribute "ps1_modulation" (stored
as is), samples the page PNG as Non-Color (raw texel), multiplies and clamps in
display space, then decodes the product to linear with ((p + 0.055) / 1.055) ** 2.4
(the sRGB curve without its linear toe: under 3/255 off for p < 0.04).
Flat F4/G4 faces use the imported (linear) vertex colour as is.
UNLIT (default) emits that colour, because the PS1 does not light course faces
(their colours are baked), and STANDARD_VIEW sets the Standard view transform, so
renders show the PS1 values.  Texel 0x0000 is transparent (alpha 0); materials
named *_semi_abr0 (GP0 0x2E) carry alpha 128 on STP texels and use alpha blending.
Nearest-neighbour sampling.  Generated file; do not edit.
"""
import sys
from pathlib import Path

import bpy

PATHS = []      # e.g. ["D:/.../extracted/models/courses/MYTHICAL_COAST/CLASS1_CALME/BIG1_2ND.obj"]
UNLIT = True            # emission (PS1: baked vertex colours, no lighting); False: Principled BSDF base colour
STANDARD_VIEW = True    # scene view transform "Standard" so displayed colours equal the PS1 values
MOD_ATTR = "ps1_modulation"


def obj_groups(path):
    groups, g = {}, None
    with open(path) as fh:
        for line in fh:
            if line.startswith("g "):
                g = line[2:].strip()
            elif line.startswith("o "):
                groups[line[2:].strip()] = g
    return groups


def _to_srgb(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1.0 / 2.4) - 0.055


def base_color_layer(mesh):
    for a in mesh.color_attributes:
        if a.name != MOD_ATTR:
            return a.name
    return None


def add_modulation_attribute(mesh):
    """FLOAT_COLOR attribute MOD_ATTR = round(OBJ colour * 255) / 128 (the PS1 texel factor),
    from the importer's colour attribute read back in sRGB (= the OBJ value)."""
    name = base_color_layer(mesh)
    if name is None or MOD_ATTR in mesh.color_attributes:
        return name
    src = mesh.color_attributes[name]
    n = len(src.data)
    vals = [0.0] * (4 * n)
    try:
        src.data.foreach_get("color_srgb", vals)
    except (AttributeError, TypeError, RuntimeError):
        lin = [0.0] * (4 * n)
        src.data.foreach_get("color", lin)
        vals = [c if i % 4 == 3 else _to_srgb(c) for i, c in enumerate(lin)]
    domain = src.domain
    out = [1.0 if i % 4 == 3 else round(c * 255.0) / 128.0 for i, c in enumerate(vals)]
    dst = mesh.color_attributes.new(MOD_ATTR, "FLOAT_COLOR", domain)
    dst.data.foreach_set("color", out)
    return name


def _render_method(mat, blended):
    # Blender < 4.2: blend_method; 4.2+: surface_render_method (set last so it wins)
    for attr, val in (("blend_method", "BLEND" if blended else "CLIP"),
                      ("surface_render_method", "BLENDED" if blended else "DITHERED")):
        try:
            setattr(mat, attr, val)
        except (AttributeError, TypeError, ValueError):
            pass


def ps1_material(mat, color_layer):
    if mat is None or mat.get("ps1_shading"):
        return
    mat.use_nodes = True
    nt = mat.node_tree
    img = next((n.image for n in nt.nodes if n.type == "TEX_IMAGE" and n.image is not None), None)
    semi = "_semi_" in mat.name
    nodes, links = nt.nodes, nt.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (1000, 0)
    alpha = None
    if img is not None:
        img.colorspace_settings.name = "Non-Color"      # raw texel values, the PS1 display space
        tex = nodes.new("ShaderNodeTexImage")
        tex.image, tex.interpolation, tex.location = img, "Closest", (-800, 200)
        mod = nodes.new("ShaderNodeVertexColor")
        mod.layer_name, mod.location = MOD_ATTR, (-800, -150)
        mul = nodes.new("ShaderNodeVectorMath")
        mul.operation, mul.location = "MULTIPLY", (-550, 100)
        links.new(tex.outputs["Color"], mul.inputs[0])
        links.new(mod.outputs["Color"], mul.inputs[1])
        clamp = nodes.new("ShaderNodeVectorMath")
        clamp.operation, clamp.location = "MINIMUM", (-350, 100)
        clamp.inputs[1].default_value = (1.0, 1.0, 1.0)
        links.new(mul.outputs["Vector"], clamp.inputs[0])
        lin = nodes.new("ShaderNodeVectorMath")
        lin.operation, lin.location = "MULTIPLY_ADD", (-150, 100)
        lin.inputs[1].default_value = (1.0 / 1.055,) * 3
        lin.inputs[2].default_value = (0.055 / 1.055,) * 3
        links.new(clamp.outputs["Vector"], lin.inputs[0])
        gam = nodes.new("ShaderNodeGamma")
        gam.location = (50, 100)
        gam.inputs["Gamma"].default_value = 2.4
        links.new(lin.outputs["Vector"], gam.inputs["Color"])
        color = gam.outputs["Color"]
        alpha = tex.outputs["Alpha"]
    else:
        vc = nodes.new("ShaderNodeVertexColor")       # imported colour = sRGB-decoded = what the PS1 shows
        vc.layer_name, vc.location = color_layer or "", (-150, 100)
        color = vc.outputs["Color"]
    if UNLIT:
        sh = nodes.new("ShaderNodeEmission")
        sh.inputs["Strength"].default_value = 1.0
        links.new(color, sh.inputs["Color"])
        surf = sh.outputs["Emission"]
    else:
        sh = nodes.new("ShaderNodeBsdfPrincipled")
        for name, value in (("Roughness", 1.0), ("Specular IOR Level", 0.0)):
            if name in sh.inputs:
                sh.inputs[name].default_value = value
        links.new(color, sh.inputs["Base Color"])
        surf = sh.outputs["BSDF"]
    sh.location = (350, 100)
    if alpha is not None:
        tr = nodes.new("ShaderNodeBsdfTransparent")
        tr.location = (350, -150)
        mix = nodes.new("ShaderNodeMixShader")
        mix.location = (700, 0)
        links.new(alpha, mix.inputs[0])
        links.new(tr.outputs["BSDF"], mix.inputs[1])
        links.new(surf, mix.inputs[2])
        surf = mix.outputs["Shader"]
    links.new(surf, out.inputs["Surface"])
    _render_method(mat, semi)
    mat["ps1_shading"] = True
    mat["ps1_semi_transparent"] = semi


def import_obj(path, parent=None):
    path = Path(path)
    groups = obj_groups(path)
    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=str(path), forward_axis="Y", up_axis="Z",
                          use_split_objects=True, use_split_groups=False)
    new = [o for o in bpy.data.objects if o not in before]
    label = "%s %s %s" % (path.parent.parent.name, path.parent.name, path.stem)
    root = bpy.data.collections.new(label)
    (parent or bpy.context.scene.collection).children.link(root)
    subs = {}
    layer = None
    done = set()
    for o in new:
        g = groups.get(o.name) or groups.get(o.name.rsplit(".", 1)[0]) or "other"
        if g not in subs:
            subs[g] = bpy.data.collections.new("%s %s" % (path.stem, g))
            root.children.link(subs[g])
        for c in list(o.users_collection):
            c.objects.unlink(o)
        subs[g].objects.link(o)
        if o.type == "MESH" and o.data.name not in done:
            done.add(o.data.name)
            name = add_modulation_attribute(o.data)
            if layer is None:
                layer = name
    for o in new:
        if o.type == "MESH":
            for m in o.data.materials:
                ps1_material(m, layer)
    if STANDARD_VIEW:
        try:
            bpy.context.scene.view_settings.view_transform = "Standard"
            bpy.context.scene.view_settings.look = "None"
        except (AttributeError, TypeError, ValueError):
            pass
    return root, subs, new


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    for p in [a for a in argv if a.lower().endswith(".obj")] or PATHS:
        import_obj(p)


if __name__ == "__main__":
    main()
'''


def write_blender_helper(out_root=OUT_ROOT):
    p = Path(out_root) / "blender_import_course.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(BLENDER_HELPER)
    return p


# ------------------------------------------------------------------ export


def export_scene(aid, out_root=OUT_ROOT, names=None, preview=True, size=1024, write_png_files=True, verbose=True):
    t0 = time.time()
    names = names or exe_names()
    scene = build_scene(aid, names)
    sn = scene.names
    out = Path(out_root) / sn["course_folder"] / sn["class_folder"]
    out.mkdir(parents=True, exist_ok=True)
    write_blender_helper(out_root)
    tex_dir = out / "textures"
    rv = rt.build_race_vram(aid, series=scene.series)
    resolve_materials(scene, rv, tex_dir, write=write_png_files)
    for o in scene.objects + scene.variants + scene.unplaced:
        if o.info["source"] != "terrain_cell":
            o.info["texture_coverage"] = texture_coverage(scene, o)
    unpl_cov = defaultdict(Counter)
    for o in scene.unplaced:
        role = o.info.get("role", "")
        cat = ("rival_car_parts" if role.startswith("rival base") else "bank1_scenery" if role.startswith("scenery")
               else "course_models" if o.info["source"] == "course_model" else o.info["source"])
        unpl_cov[cat].update(o.info["texture_coverage"])
    stem = sn["stem"]
    title = ("# Rage Racer (PAL) %s = %s, CLASS%d %s (asset_%03d.bin)" %
             (sn["file"], sn["course_name"], sn["class_number"], sn["class_name"], aid))
    if sn["role"] == "prologue":
        title = ("# Rage Racer (PAL) %s = %s as drawn by the new-game prologue (class %d %s, g_CourseIndex 0; "
                 "file named for course 3 %s; blocks 3-8 = BIG5.2ND) (asset_%03d.bin)"
                 % (sn["file"], sn["course_name"], sn["class"], sn["class_name"], sn["file_course_name"], aid))
    header = [title,
              "# raw PS1 vertex units (4 x world units); written in Blender axes X = x, Y = z, Z = -y:",
              "# import with Forward Axis Y, Up Axis Z (or run ../../blender_import_course.py)",
              "# quads: PS1 strip v0 v1 / v2 v3 written f v0 v2 v3 v1 (front = NCLIP > 0 -> outward normals)",
              "# v x y z r g b: r g b = face colour word / 255 (texel x rgb/128 on the PS1)",
              "# state: %s start, series %d, env mode 4 = %s; generated by tools/export_course_scenes.py"
              % ("prologue" if sn["role"] == "prologue" else "race", scene.series, scene.env_mode4),
              "# materials *_semi_abr0: semi-transparent (GP0 0x2E), PNG alpha 128 on STP texels = 50% blend"]
    used = set()
    for o in scene.objects + scene.variants + scene.unplaced:
        for p in o.polys:
            used.add(poly_material(scene, p))
    write_mtl(scene, out / ("%s.mtl" % stem), used)
    # scene objects sorted by group so Blender lists terrain / course models / scenery together
    order = {"terrain": 0, "course_models": 1, "scenery": 2}
    objs = sorted(scene.objects, key=lambda o: (order[o.group], o.name))
    st_scene = write_obj(scene, objs, out / ("%s.obj" % stem), "%s.mtl" % stem,
                         header + ["# scene: %d objects" % len(objs)])
    st_var = write_obj(scene, scene.variants, out / "variants.obj", "%s.mtl" % stem,
                       header + ["# variants: models drawn at the same established transform as a scene object "
                                 "in other animation frames / env states"])
    st_unp = write_obj(scene, scene.unplaced, out / "unplaced.obj", "%s.mtl" % stem,
                       header + ["# unplaced: models with no placement on the visible course in this scene (cars and "
                                 "other dynamic models, models no drawer reaches, draws parked off the map); positions "
                                 "are a display grid (offset in each object's comment), never a game position",
                                 "# texture_coverage in each comment: exact = texels of a .1ST TIM of that tpage/clut, "
                                 "race = race-written VRAM but not one TIM, uncovered = VRAM the race load never "
                                 "writes (stale or cleared)"])
    wc = check_written_scene(scene, out / ("%s.obj" % stem), out / ("%s.mtl" % stem))
    res = {"asset": aid, "names": sn, "env": scene.env, "draw_course": scene.draw_course, "notes": scene.notes,
           "scene": st_scene, "variants": st_var, "unplaced": st_unp,
           "groups": dict(Counter(o.group for o in scene.objects)),
           "stats": dict(scene.stats), "texture_stats": dict(scene.tex_stats),
           "unplaced_texture_coverage": {k: dict(v) for k, v in sorted(unpl_cov.items())},
           "offmap": {"%s_%d" % k: v for k, v in scene.offmap.items()},
           "materials": {"page": sum(1 for m in scene.materials.values() if m["kind"] == "page"),
                         "tile": sum(1 for m in scene.materials.values() if m["kind"] == "tile"),
                         "flat": sum(1 for m in scene.materials.values() if m["kind"] == "flat")},
           "written_checks": wc}
    if preview:
        pv = {}
        sp = out / ("%s.obj" % stem)
        # top-down views cull faces turned away from a camera above (the game's NCLIP), so the
        # road shows through overpass decks / tunnel roofs, which face down
        pv["topdown"] = render_view([sp], out / "preview_topdown.png", "top", size, cull=True)
        p0 = scene.pscene["track"]["points"][0]
        cx, cy = 4 * p0["x"], 4 * p0["z"]
        half = 3 * 8192
        reg = (cx - half, cy - half, cx + half, cy + half)
        pv["start_top"] = render_view([sp], out / "preview_start_topdown.png", "top", 768, region=reg, cull=True)
        # three-quarter view without culling, back faces tinted magenta (winding check by eye)
        pv["start_3q"] = render_view([sp], out / "preview_start_3q.png", "3q", 900, region=reg, tint_backfaces=True)
        pv["unplaced_3q"] = render_view([out / "unplaced.obj"], out / "preview_unplaced_3q.png", "3q", 900)
        if scene.variants:
            pv["variants_3q"] = render_view([out / "variants.obj"], out / "preview_variants_3q.png", "3q", 700)
        res["previews"] = pv
    manifest = {"result": res,
                "objects": [{"name": o.name, "group": o.group, "faces": o.face_count(), "info": o.info}
                            for o in objs],
                "variants": [{"name": o.name, "faces": o.face_count(), "info": o.info} for o in scene.variants],
                "unplaced": [{"name": o.name, "faces": o.face_count(), "info": o.info} for o in scene.unplaced],
                "materials": scene.materials}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
    res["seconds"] = round(time.time() - t0, 1)
    res["out"] = str(out)
    if verbose:
        print_result(res)
    return res


def print_result(res):
    sn = res["names"]
    print("asset %03d %-9s -> %s/%s/%s.obj  (%.1fs)" % (res["asset"], sn["file"], sn["course_folder"],
                                                     sn["class_folder"], sn["stem"], res["seconds"]))
    print("  scene: %(objects)d objects, %(faces)d faces, %(vertices)d v, %(vt)d vt" % res["scene"],
          "| groups", res["groups"])
    print("  variants: %(objects)d objects %(faces)d faces | unplaced: " % res["variants"]
          + "%(objects)d objects %(faces)d faces" % res["unplaced"])
    print("  materials", res["materials"], "| texture", res["texture_stats"])
    print("  env mode4 at start %s; draw course %d; stats %s" % (res["env"]["env_mode4_at_start"],
                                                                res["draw_course"], res["stats"]))
    wc = res["written_checks"]
    print("  written: track points %d/%d on terrain, %d within 64; terrain faces up %d down %d; "
          "facing track camera %d/%d; seams %s"
          % (wc["track_on_terrain"], wc["track_points"], wc["track_within_64"], wc["terrain_up"],
             wc["terrain_down"], wc["terrain_facing_track_camera"],
             wc["terrain_facing_track_camera"] + wc["terrain_away_from_track_camera"], wc["seams"]))
    if "previews" in res:
        print("  previews (pixels, back-face pixels, culled faces)",
              {k: (v.get("pixels"), v.get("backface_pixels"), v.get("culled_faces")) for k, v in res["previews"].items()})


def run_all(out_root=OUT_ROOT, preview=True, size=1024):
    names = exe_names()
    results = []
    failures = []
    for aid in SCENE_PACKS:
        try:
            results.append(export_scene(aid, out_root, names, preview=preview, size=size))
        except Exception as e:      # report every pack, then fail
            failures.append((aid, "%s: %s" % (type(e).__name__, e)))
            print("asset %03d FAILED: %s" % (aid, failures[-1][1]))
    # global assertions
    folders = Counter((r["names"]["course_folder"], r["names"]["class_folder"]) for r in results)
    if any(c > 1 for c in folders.values()):
        failures.append(("all", "two packs map to one folder"))
    tot = Counter()
    for r in results:
        tot["objects"] += r["scene"]["objects"]
        tot["faces"] += r["scene"]["faces"]
        tot["variant_faces"] += r["variants"]["faces"]
        tot["unplaced_faces"] += r["unplaced"]["faces"]
        tot["page_materials"] += r["materials"]["page"]
        tot["tile_materials"] += r["materials"]["tile"]
        tot["terrain_cells"] += r["groups"].get("terrain", 0)
        tot["track_points"] += r["written_checks"]["track_points"]
        tot["track_within_64"] += r["written_checks"]["track_within_64"]
    for r in results:
        for cat, cov in r["unplaced_texture_coverage"].items():
            for kind, n in cov.items():
                tot["unplaced_%s_%s" % (cat, kind)] += n
        tot["semi_transparent_faces"] += r["texture_stats"].get("semi_transparent_faces", 0)
        tot["offmap_draws"] += len(r["offmap"])
    print("TOTAL", dict(tot))
    # the output tree holds only this run's scene folders and files (no stale names, no bytecode caches)
    root = Path(out_root)
    pyc = root / "__pycache__"
    if pyc.is_dir():
        for p in pyc.iterdir():
            if p.suffix == ".pyc":
                p.unlink()
        pyc.rmdir()
    expected = {(r["names"]["course_folder"], r["names"]["class_folder"]) for r in results}
    for course_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for class_dir in sorted(p for p in course_dir.iterdir() if p.is_dir()):
            if (course_dir.name, class_dir.name) not in expected and (class_dir / "manifest.json").is_file():
                failures.append(("all", "stale scene folder %s/%s (not produced by this run)"
                                 % (course_dir.name, class_dir.name)))
    if preview and results:
        contact_sheet([Path(r["out"]) / "preview_topdown.png" for r in results], Path(out_root) / "contact_topdown.png")
        contact_sheet([Path(r["out"]) / "preview_start_3q.png" for r in results], Path(out_root) / "contact_start_3q.png",
                      cell=320)
    summary = {"results": results, "failures": failures, "totals": dict(tot)}
    (Path(out_root) / "export_course_scenes_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    if failures:
        print("FAILURES: %d" % len(failures))
        for f in failures:
            print("  ", f)
        return 1
    print("ALL %d SCENES EXPORTED, ALL ASSERTIONS PASSED" % len(results))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("pack", nargs="?", default="88")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=str(OUT_ROOT))
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--size", type=int, default=1024)
    a = ap.parse_args(argv)
    if a.all:
        return run_all(Path(a.out), preview=not a.no_preview, size=a.size)
    s = "".join(ch for ch in a.pack if ch.isdigit())
    export_scene(int(s), Path(a.out), preview=not a.no_preview, size=a.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
