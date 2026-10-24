#!/usr/bin/env python3
"""export_car_models.py - every PAL Rage Racer car model as textured OBJ.

Two kinds of car model exist in the binary, and both are exported:

1. Player cars, assets 10..73.  CAR_xy.1ST (even 10..72) holds the
   CarModelAsset header, the 22-model car bank and the 64x256-word car image;
   CAR_xy.2ND (odd 11..73) holds spec data, the engine sound and an image chain
   (block 4) that no car face samples.  x = model (hex), y = grade
   (car_of_asset / the EXE path table): 32 assets = 13 cars in 1..5 grades.
   Output: cars/<MAKER>_<NAME>/CAR_xy/.

2. Rival cars, drawn by DrawCars / DrawPlayerCarOnly (car_draw_and_knockback.c
   102/118: SelectModelBank(1) then DrawCar) from bank 1 (block 3) of the
   course .2ND packs.  Model slot m (0..10, g_RaceGridSlots is the static list
   0..10, so every slot races) resolves through g_CarModelBankTable[m] =
   (lod base, clut shift k) and TrackRenderTable.models[m] (block 0) =
   (axis0, axis1, axis2, horizon).  Output: cars/<MAKER>_<NAME>/RIVAL_CLASSn_..
   when the slot is identified as a player car (render params == that car's
   CAR_xy.1ST header AND bank-1 part +3 == one of its rear-axle models, byte
   for byte), else cars/UNNAMED_RIVAL_CLASSn_<GP>_BASEbb/...

How the game draws a player car (render/draw_player_car_model.c, at rest:
angles, roll, steering and wheelRotation 0, modelY == y):
  model 1      SubmitModel twice at (x, modelY - horizon, z), ground matrix
  model 0      the body, at obj (y - horizon)
  rear axle    model otDepth + 3 at obj, RotX(wheelRotation)
  front wheel  model otDepth + 2, i = 0, 1 at (+-modelOffsetX, modelOffsetY,
               modelOffsetZ) world units = 4x that in vertex units
               (SetGteObjectMatrix, draw_packet_queue.c:164); i = 1 uses
               m_B0[1] = m_B0[0] * diag(-1, 1, -1), a 180 deg turn about Y
  otDepth      renderDepth * 2, + 10 when wheelRotation & 0x1000
               (update_player_car.c:202-203: car->speed > 800)
  renderDepth  GameRenderObject +0xE4 of g_PlayerCar (0x8009E6D4) is
               g_PlayerTireCompound (0x8009E7B8), written only by
               swap_car_model_slot.c:113 = g_CarTable[car].tireCompound, the
               CUSTOMIZE tyre setting 0..4 (car_select.c:756-766, 818); the
               default is g_SaveDefaults[car].tireCompound.
So with the model origin at the rear axle centre, the ground is at
y = +4 * horizon (PS1 y is down) and the car faces -z.  <stem>.obj uses the
default tyre compound, <stem>_tireN[_fast].obj every option.

DrawCar (rivals) is the same with lod[0] + {0 body, 1 ground quad, 2 front
wheel, 3 rear axle}, wheels at TrackRenderTable.models[m].axis0..2, steering
* 2, the body drawn with g_ScratchRenderMode (ctx+0x84) = lod[1] << 16, which
the model-bank FT4/GT4 emitters add to the u0v0|clut word (clut + k), and
lod[0] + 4 alone (clut + k) as the far LOD for 0xD00 <= |dx|+|dz| < 0x2500.

Textures: the player car image sits at VRAM (704..767, 0..255) = tpage
0x000B and carries its own 16 CLUT rows (224..239, x 752..767).  Wheel hubs
are CAR.TMS (asset 5, boot resident) images at x 640..703 (tpage 0x000A) read
through car-image CLUT rows; one face samples the team-logo canvas through
the team-logo CLUT (0x7801).  The body paint (ApplyBodyColor1/2, run on the
RAM copy before UploadCarImage for car index < 10, car_assets.c:97-103) is
applied with the default colours from g_SaveDefaults.  Rival textures are the
course .1ST uploads (block 0) plus CAR.TMS hubs.  The VRAM is
rt.build_race_vram; every sampled texel and CLUT word is checked to be
identical (same owner class, same value) on both texture pages, both GP
series, every course pack (player cars) / every member pack and every player
car (rivals).

Winding: the game draws a face when NCLIP of v0 v1 v2 is > 0 and the stored
GTE normals point out of the body, which makes the literal strip loop
v0 v1 v3 v2 inward-facing under the (x, z, -y) axis map (det +1).  The OBJ is
therefore written with the same cycle reversed (v0 v2 v3 v1) so Blender
normals point outward (as export_course_scenes.py / export_other_models.py
do); --ps1-winding writes the literal v0 v1 v3 v2.

UVs: vt = u/256, 1 - v/256 in the face's 256x256 page, except that an axis
on which all corners share one texel (a solid-colour fill reading one texel
row or column) is moved to the texel centre (+0.5), see uv_centring().

Import has no side effects.  Run:  python -I tools/export_car_models.py
"""
import argparse
import json
import math
import re
import struct
import sys
import time
import zlib
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rr_assets as ra          # noqa: E402
import rr_model_banks as mb     # noqa: E402
import rr_textures as tx        # noqa: E402

OUT_ROOT = mb.REPO / "extracted" / "models" / "cars"
CAR_COUNT = 32
MODELS_PER_CAR = 22
TIRE_COMPOUNDS = 5               # CUSTOMIZE row 0: g_MenuSubCursor 0..4 (car_select.c:756-766)
WORLD_TO_VERTEX = 4              # SetGteObjectMatrix: translation 4 * View * (pos - cam)
CAR_TPAGE = 0x000B               # x 704 (g_CarImageRect), 4-bit
HUB_TPAGE = 0x000A               # x 640, 4-bit (CAR.TMS hubs, team-logo canvas)
TEAM_LOGO_CLUT = tx.make_clut(tx.TEAM_LOGO_CLUT_RECT[0], tx.TEAM_LOGO_CLUT_RECT[1])
PAINT_PALETTE_OFF = 0x7060       # CarImageData.paintPalette (include/game/car.h:314)
CAR_IMAGE_SIZE = 0x8000
REFERENCE_COURSE = 88            # build_race_vram reference pack (BIG class 0 .2ND)
COURSE_2ND = range(88, 135, 2)

# main.exe data (asm/PAL/main/data/main/6BE64.data.s)
EXE_ADDR = {
    "g_SaveDefaults": 0x8007BE68,        # CarEntry[13], 8 bytes each (line 1038)
    "g_BodyColorPrimary": 0x8007D30C,    # u16[18] (line 2740)
    "g_BodyColorSecondary": 0x8007D330,  # u16[18] (line 2762)
    "g_PaintSlots3StopA": 0x8007D354,    # u16[9] used (line 2784)
    "g_PaintSlots3StopB": 0x8007D368,    # u16[8] (line 2793)
    "g_PaintSlots4Stop": 0x8007D378,     # u16[4] (line 2801)
    "g_CarModelBankTable": 0x8007D380,   # s16[11][2] (line 2807)
    "g_CarModelByCourse": 0x8007D3AC,    # u8[4][11] (line 2822)
    "g_RaceGridSlots": 0x8007E074,       # s32[12] (line 4428)
    "g_AttractGridSlots": 0x8007E0A4,    # s32[12] (line 4444)
}
ASM_STOP_A = (0x001, 0x041, 0x0C1, 0x101, 0x181, 0x241, 0x281, 0x301, 0x341)
ASM_STOP_B = (0x001, 0x041, 0x0C1, 0x181, 0x241, 0x281, 0x301, 0x341)
ASM_STOP_4 = (0x141, 0x1C1, 0x201, 0x401)
ASM_TIRE_DEFAULTS = (2, 3, 4, 1, 0, 0, 0, 2, 2, 3, 4, 0, 3)   # g_SaveDefaults byte +1 (6BE64.data.s:1038-1064)
ASM_BANK_TABLE = ((0, 0), (5, 0), (10, 0), (15, 0), (20, 0), (20, 1), (25, 0), (25, 1), (30, 0), (30, 1), (30, 2))
ASM_BY_COURSE = ((0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10), (1, 2, 0, 3, 4, 5, 6, 7, 8, 9, 10),
                 (2, 0, 1, 3, 4, 5, 6, 7, 8, 9, 10), (3, 0, 1, 2, 4, 5, 6, 7, 8, 9, 10))
ASM_GRID = tuple(range(11)) + (-1,)
RIVAL_SLOTS = 11
RIVAL_PART_NAMES = ("body", "ground_quad", "front_wheel", "rear_axle", "far_body")
# bank 1 of these .2ND packs is another class's (rr_model_banks run_all, EXPECTED_SAME_BLOCKS)
RIVAL_CLASS_OVERRIDE = {94: 4, 102: 2}
PAINT_COLOURS = 18
PAINTABLE_CARS = 10              # car_assets.c:97 / 154 'carIndex < 10'

RY180 = ((-1, 0, 0), (0, 1, 0), (0, 0, -1))
IDENT = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


class ExportError(Exception):
    pass


def require(cond, msg):
    if not cond:
        raise ExportError(msg)


# ---------------------------------------------------------------- naming

@dataclass
class CarVariant:
    k: int              # car asset index 0..31 (GetCarAssetIndex)
    asset_1st: int
    asset_2nd: int
    model: int          # car 0..12 (g_CarNames index)
    grade: int          # 0.. (k - g_CarModelBaseIndex[model])
    maker: str          # g_CarClassNames[model]
    name: str           # g_CarNames[model]
    path_1st: str       # g_AssetPaths
    path_2nd: str
    disc_stem: str      # CAR_xy
    folder: str         # <maker>_<name>
    stem: str           # <maker>_<name>_CAR_xy

    @property
    def leaf(self):
        return OUT_ROOT / self.folder / self.disc_stem


def car_variants():
    """The 32 car assets, named from main.exe tables (g_AssetPaths, g_CarNames,
    g_CarClassNames, g_CarModelBaseIndex)."""
    t = ra.exe_tables()
    require(t["source"] == "exe", "main.exe not found; names must come from the EXE")
    out = []
    for k in range(CAR_COUNT):
        a1 = tx.ASSET_CAR_BASE + 2 * k
        kk, m, g, half = ra.car_of_asset(a1)
        require(kk == k and half == "1ST", f"car_of_asset({a1}) = {kk, m, g, half}")
        require(t["car_base"][m] + g == k, f"g_CarModelBaseIndex[{m}] + {g} != {k}")
        p1, p2 = t["asset_paths"][a1], t["asset_paths"][a1 + 1]
        s1 = re.split(r"[\\/]", p1)[-1]
        s2 = re.split(r"[\\/]", p2)[-1]
        stem = "CAR_%X%d" % (m, g)
        require(s1 == stem + ".1ST" and s2 == stem + ".2ND", f"asset {a1}: paths {p1!r} {p2!r}, expected {stem}")
        folder = "%s_%s" % (t["car_makers"][m], t["car_names"][m])
        require(re.fullmatch(r"[A-Z0-9_]+", folder) is not None, f"folder name {folder!r}")
        out.append(CarVariant(k, a1, a1 + 1, m, g, t["car_makers"][m], t["car_names"][m], p1, p2,
                              stem, folder, folder + "_" + stem))
    return out


# ---------------------------------------------------------------- paint

def _exe_bytes():
    return Path(ra.EXE_PATH).read_bytes()


def _exe_off(addr):
    return addr - ra.EXE_BASE + ra.EXE_HEADER


def _exe_u16(exe, addr, n):
    return list(struct.unpack_from("<%dH" % n, exe, _exe_off(addr)))


def paint_tables(exe=None):
    """Body colour tables and the default CarEntry table out of main.exe."""
    exe = exe if exe is not None else _exe_bytes()
    t = {
        "primary": _exe_u16(exe, EXE_ADDR["g_BodyColorPrimary"], PAINT_COLOURS),
        "secondary": _exe_u16(exe, EXE_ADDR["g_BodyColorSecondary"], PAINT_COLOURS),
        "stopA": tuple(_exe_u16(exe, EXE_ADDR["g_PaintSlots3StopA"], 9)),
        "stopB": tuple(_exe_u16(exe, EXE_ADDR["g_PaintSlots3StopB"], 8)),
        "stop4": tuple(_exe_u16(exe, EXE_ADDR["g_PaintSlots4Stop"], 4)),
    }
    require(t["stopA"] == ASM_STOP_A and t["stopB"] == ASM_STOP_B and t["stop4"] == ASM_STOP_4,
            f"paint slot tables differ from the data asm: {t['stopA']} {t['stopB']} {t['stop4']}")
    require(t["primary"][0] == 0xBE73 and t["primary"][17] == 0xA5AB and
            t["secondary"][0] == 0x9929 and t["secondary"][17] == 0x8CA4, "body colour tables differ from the data asm")
    o = _exe_off(EXE_ADDR["g_SaveDefaults"])
    d = []
    for i in range(13):
        b = exe[o + 8 * i:o + 8 * i + 8]
        d.append({"modelVariant": b[0], "tireCompound": b[1], "transmission": b[2],
                  "paintColor1": b[3], "paintColor2": b[4], "enabled": b[5]})
    t["defaults"] = d
    tires = tuple(e["tireCompound"] for e in d)
    require(tires == ASM_TIRE_DEFAULTS, f"g_SaveDefaults tireCompound {tires} differs from the data asm")
    require(all(0 <= x < TIRE_COMPOUNDS for x in tires), f"tireCompound outside 0..4: {tires}")
    return t


def _blend(s1, s2):
    """BlendPaintColor (paint_color.c)."""
    return (((s1 >> 1) & 0x3DEF) + ((s2 >> 1) & 0x3DEF)) | 0x8000


def _thirds(s1, s2):
    """BlendPaintColorThirds; channel sums can carry into the next channel
    (r * 2 / 3 up to 41), kept as the game computes it."""
    r = (s1 & 31) + (s2 & 31)
    g = ((s1 >> 5) & 31) + ((s2 >> 5) & 31)
    b = ((s1 >> 10) & 31) + ((s2 >> 10) & 31)
    t0 = ((b * 2 // 3) << 10) + ((g * 2 // 3) << 5) + r * 2 // 3 + 0x8000
    t1 = ((b // 3) << 10) + ((g // 3) << 5) + r // 3 + 0x8000
    return t0 & 0xFFFF, t1 & 0xFFFF


def _quarters(s1, s2):
    """BlendPaintColorQuarters -> (shade0, shade1, shade2)."""
    a = (s1 >> 1) & 0x3DEF
    b = (s2 >> 1) & 0x3DEF
    c = a + b
    high = (c + 0x8000) & 0xFFFF
    c = (c >> 1) & 0x3DEF
    return (a + c - 0x8000) & 0xFFFF, high, (c + b - 0x8000) & 0xFFFF


def apply_paint(image, colour1, colour2, tables):
    """ApplyBodyColor1(colour1) then ApplyBodyColor2(colour2) on a copy of the
    0x8000-byte car image.  Returns (bytes, {entry: (old, new)})."""
    require(len(image) == CAR_IMAGE_SIZE, f"car image {len(image):#x} bytes")
    n = (CAR_IMAGE_SIZE - PAINT_PALETTE_OFF) // 2
    e = list(struct.unpack_from("<%dH" % n, image, PAINT_PALETTE_OFF))
    old = list(e)
    written = set()

    def put(i, val):
        require(0 <= i < n and PAINT_PALETTE_OFF + 2 * i + 2 <= CAR_IMAGE_SIZE, f"paint entry {i:#x} outside the image")
        e[i] = val & 0xFFFF
        written.add(i)

    P, S = tables["primary"], tables["secondary"]
    # ApplyBodyColor1
    s1, s2 = P[colour1], S[colour1]
    put(0x81, s1)                                  # fixed.bodyColor1
    sh0 = _blend(s1, s2)
    for i in tables["stopA"]:
        put(i, s1), put(i + 1, sh0), put(i + 2, s2)
    t0, t1 = _thirds(s1, s2)
    for i in tables["stop4"]:
        put(i, s1), put(i + 1, t0), put(i + 2, t1), put(i + 3, s2)
    q0, q1, q2 = _quarters(s1, s2)
    for j, val in enumerate((s1, q0, q1, q2, s2)):
        put(0x2C1 + j, val)                        # bodyColor1Gradient
    # ApplyBodyColor2
    s1, s2 = P[colour2], S[colour2]
    sh0 = _blend(s1, s2)
    for i in tables["stopB"]:
        put(i + 3, s1), put(i + 4, sh0), put(i + 5, s2)
    t0, t1 = _thirds(s1, s2)
    for i in tables["stop4"]:
        put(i + 4, s1), put(i + 5, t0), put(i + 6, t1), put(i + 7, s2)
    q0, q1, q2 = _quarters(s1, s2)
    for j, val in enumerate((s1, q0, q1, q2, s2)):
        put(0x2C6 + j, val)                        # bodyColor2Gradient
    out = bytearray(image)
    struct.pack_into("<%dH" % n, out, PAINT_PALETTE_OFF, *e)
    changes = {i: (old[i], e[i]) for i in sorted(written) if old[i] != e[i]}
    return bytes(out), changes


def paint_entry_vram(i):
    """VRAM word of paint palette entry i (image word 0x7060/2 + i)."""
    w = PAINT_PALETTE_OFF // 2 + i
    x, y, _, _ = tx.CAR_IMAGE_RECT
    return x + w % 64, y + w // 64


# ---------------------------------------------------------------- bank helpers

def drawn_faces(bank, index, drawn_only=True):
    return [f for f in bank.models[index].faces() if f.drawn or not drawn_only]


def effective_clut(f, k=0):
    """u0v0|clut word + (k << 16) as the model-bank FT4Raw / GT4 emitters add
    ctx+0x84 (terrain_submission.s:1190-1192, 1305-1307): clut + k.  Asserted
    not to carry from the x/16 field into the y field."""
    require((f.clut & 0x3F) + k <= 0x3F, f"face {f.off:#x}: clut {f.clut:#06x} + {k} carries into y")
    return f.clut + k


def material_of(f, k=0):
    if f.textured:
        return "tp%04X_cl%04X" % (f.tpage, effective_clut(f, k))
    return "flat_%02X%02X%02X" % f.rgb


def vertex_multiset(bank, index):
    return sorted(tuple(bank.vertices[i][:3]) for i in bank.models[index].vertex_indices())


# ---------------------------------------------------------------- car data

def model_kind(index):
    """(object stem, tyre compound or None, fast flag) for player model `index`."""
    if index == 0:
        return "body", None, False
    if index == 1:
        return "ground_quad", None, False
    k = index - 2
    tire, fast = (k % 10) // 2, k >= 10
    part = "front_wheel" if k % 2 == 0 else "rear_axle"
    return "%s_tire%d%s" % (part, tire, "_fast" if fast else ""), tire, fast


@dataclass
class Car:
    variant: CarVariant
    data: bytes
    header: dict
    bank: object
    image: bytes                  # car image as stored in the pack
    painted: bytes                # after ApplyBodyColor1/2 (== image for cars >= 10)
    paint: dict
    tire_default: int = 0         # g_SaveDefaults[model].tireCompound
    geometry: dict = field(default_factory=dict)

    def faces(self, index, drawn_only=True):
        return drawn_faces(self.bank, index, drawn_only)


def load_car(variant, tables=None):
    """Parse and check one CAR_xy.1ST; returns a Car with the mb.Bank (faces
    carry opcode, idx, nrm, rgb, tpage, clut, uv) and the painted image."""
    a = variant.asset_1st
    inv = ra.asset_inventory(a)
    require(inv["errors"] == [], f"asset {a}: inventory errors {inv['errors'][:3]}")
    _, data = mb.load_asset(a)
    ox, oy, oz, hz = struct.unpack_from("<4h", data, 0)
    header = {"modelOffsetX": ox, "modelOffsetY": oy, "modelOffsetZ": oz, "horizon": hz,
              "modelData": struct.unpack_from("<i", data, 0x20)[0],
              "imageData": struct.unpack_from("<i", data, 0x24)[0]}
    banks = mb.banks_for_asset(a)
    require(len(banks) == 1 and banks[0].label == "car", f"asset {a}: banks {[b.label for b in banks]}")
    bank = banks[0]
    errs = mb.check_bank(bank)
    require(errs == [], f"asset {a}: check_bank {errs[:3]}")
    require(bank.count == MODELS_PER_CAR and len(bank.models) == MODELS_PER_CAR, f"asset {a}: {bank.count} models")
    img = tx.parse_car_1st_image(data)
    require((img.x, img.y, img.w, img.h) == tx.CAR_IMAGE_RECT and len(img.pixels) == CAR_IMAGE_SIZE,
            f"asset {a}: car image {img.rect()}")
    tables = tables or paint_tables()
    d = tables["defaults"][variant.model]
    if variant.model < PAINTABLE_CARS:
        c1, c2 = d["paintColor1"], d["paintColor2"]
        painted, changes = apply_paint(img.pixels, c1, c2, tables)
        paint = {"applied": True, "paintColor1": c1, "paintColor2": c2, "changed_entries": len(changes),
                 "evidence": "car_assets.c:97-103 ApplyBodyColor1/2(g_CarTable[i].paintColor1/2), "
                             "defaults g_SaveDefaults (save_defaults.c ResetProgressSlot / InitSaveDefaults)"}
    else:
        painted, paint = img.pixels, {"applied": False, "reason": "car index >= 10 (car_assets.c:97)"}
    car = Car(variant, data, header, bank, img.pixels, painted, paint, d["tireCompound"])
    car.geometry = check_structure(car)
    return car


def wheel_geometry(bank, label, body, quad, fronts, rears, front_c, horizon):
    """Shared player/rival wheel and ground assertions; returns measurements
    (vertex units, PS1 axes).  fronts/rears: model indices."""
    bd = drawn_faces(bank, body, False)
    require(bd and {f.opcode for f in bd} <= {0, 3}, f"{label}: body opcodes {Counter(f.opcode for f in bd)}")
    gq = drawn_faces(bank, quad, False)
    require(gq and all(f.opcode == 0 for f in gq), f"{label}: model {quad} is not flat F4")
    gy = {bank.vertices[i][1] for f in gq for i in f.idx}
    require(len(gy) == 1, f"{label}: model {quad} not level (y {sorted(gy)})")
    ground_quad_y = gy.pop()
    fb, rb = [], []
    for mi in list(fronts) + list(rears):
        bb = mb.model_bbox(bank, bank.models[mi])
        require(bb is not None, f"{label}: model {mi} empty")
        cx, cy, cz = (bb[0] + bb[3]) / 2, (bb[1] + bb[4]) / 2, (bb[2] + bb[5]) / 2
        require(abs(cx) <= 1 and abs(cy) <= 1 and abs(cz) <= 1, f"{label}: model {mi} not centred {bb}")
        (fb if mi in fronts else rb).append(bb)
    fr = max(bb[4] - bb[1] for bb in fb) / 2
    rr = max(bb[4] - bb[1] for bb in rb) / 2
    fw = max(bb[3] - bb[0] for bb in fb)
    rw = min(bb[3] - bb[0] for bb in rb)
    require(rw > 4 * fw, f"{label}: rear models ({rw}) are not two-wheel axles (front width {fw})")
    rear_half_track = max(bb[3] for bb in rb) - fw / 2
    geo = {"front_wheel_centre": front_c, "front_radius": fr, "rear_radius": rr,
           "front_bottom_y": front_c[1] + fr, "rear_bottom_y": rr, "ground_quad_y": ground_quad_y,
           "ground_y": WORLD_TO_VERTEX * horizon, "front_wheel_width": fw,
           "rear_half_track_approx": rear_half_track}
    # wheels must reach the ground quad (within a few units) and not sit above it
    for key in ("front_bottom_y", "rear_bottom_y"):
        require(abs(geo[key] - ground_quad_y) <= 4, f"{label}: {key} {geo[key]} vs ground quad y {ground_quad_y}")
    require(0 <= geo["ground_y"] - ground_quad_y <= 8, f"{label}: ground {geo['ground_y']} vs quad {ground_quad_y}")
    # front wheels sit ahead of the rear axle (car faces -z) and outside the body centre line
    require(front_c[2] < -100 and front_c[0] > fw / 2, f"{label}: front wheel centre {front_c}")
    return geo


def check_structure(car):
    """Asserts the 22-model layout DrawPlayerCarModel relies on."""
    h = car.header
    front_c = tuple(WORLD_TO_VERTEX * h[k] for k in ("modelOffsetX", "modelOffsetY", "modelOffsetZ"))
    return wheel_geometry(car.bank, "asset %d" % car.variant.asset_1st, 0, 1,
                          range(2, MODELS_PER_CAR, 2), range(3, MODELS_PER_CAR, 2), front_c, h["horizon"])


def assemble(car, tire=0, fast=False):
    """Instances as DrawPlayerCarModel draws them at rest with renderDepth =
    tyre compound `tire`: [(object name, model index, R (PS1 3x3), t (PS1
    vertex units), clut shift)]."""
    require(0 <= tire < TIRE_COMPOUNDS, f"tyre compound {tire}")
    ot = 2 * tire + (10 if fast else 0)
    require(ot + 3 < car.bank.count, f"otDepth {ot} falls back to model 1")
    h = car.header
    ox, oy, oz = (WORLD_TO_VERTEX * h[k] for k in ("modelOffsetX", "modelOffsetY", "modelOffsetZ"))
    fw, rw = ot + 2, ot + 3
    return [
        ("m01_" + model_kind(1)[0], 1, IDENT, (0, 0, 0), 0),
        ("m00_" + model_kind(0)[0], 0, IDENT, (0, 0, 0), 0),
        ("m%02d_%s" % (rw, model_kind(rw)[0]), rw, IDENT, (0, 0, 0), 0),
        ("m%02d_%s_i0_posx" % (fw, model_kind(fw)[0]), fw, IDENT, (ox, oy, oz), 0),
        ("m%02d_%s_i1_negx" % (fw, model_kind(fw)[0]), fw, RY180, (-ox, oy, oz), 0),
    ]


def all_models_layout(bank, names, gap=40):
    """Every model at its own origin, spaced along PS1 +x."""
    out, x = [], 0
    for mi in range(bank.count):
        bb = mb.model_bbox(bank, bank.models[mi])
        x -= bb[0]
        out.append(("m%02d_%s" % (mi, names(mi)), mi, IDENT, (x, 0, 0), 0))
        x += bb[3] + gap
    return out


# ---------------------------------------------------------------- VRAM

def owner_tag(v, word_index):
    o = v.owner[word_index]
    return v.log[o][0] if o >= 0 else None


def allowed_tag(car, tag):
    if tag is None:
        return False
    return (tag == "asset%03d.carimage" % car.variant.asset_1st or tag.startswith("asset005#")
            or tag in ("course_select.teamlogo_canvas", "course_select.teamlogo_clut"))


def rival_allowed(aid_2nd):
    """Rival words may come from the race's own .1ST uploads or CAR.TMS."""
    pre = "asset%03d.blk" % (aid_2nd - 1)
    return lambda tag: tag is not None and (tag.startswith(pre) or tag.startswith("asset005#"))


def sampled_words(bank, pairs):
    """{word index: role} of every VRAM word the drawn textured faces of the
    (model, clut shift) pairs read: texel words of the UV rectangle
    (inclusive) and the 16 CLUT words of clut + k."""
    out = {}
    for mi, k in pairs:
        for f in drawn_faces(bank, mi):
            if not f.textured:
                continue
            t = tx.tpage_info(f.tpage)
            require(t["depth"] == 0, f"model {mi} face {f.off:#x}: tpage {f.tpage:#06x} is not 4-bit")
            us = [p[0] for p in f.uv]
            vs = [p[1] for p in f.uv]
            for y in range(t["y"] + min(vs), t["y"] + max(vs) + 1):
                for x in range(t["x"] + min(us) // 4, t["x"] + max(us) // 4 + 1):
                    out.setdefault(y * 1024 + x, "texel")
            cx, cy = tx.clut_xy(effective_clut(f, k))
            for i in range(16):
                out[cy * 1024 + cx + i] = "clut"
    return out


def car_vram(car, aid_2nd=REFERENCE_COURSE, series=0):
    return tx.build_race_vram(aid_2nd, series=series, car=car.variant.k, car_image=car.painted)


def race_check(car, ref, words, courses=COURSE_2ND, series=(0, 1)):
    """Every sampled word has the same value and an allowed owner in page 0
    and page 1 of every course pack's race VRAM (both GP series)."""
    keys = sorted(words)
    ref_vals = [ref.pix[i] for i in keys]
    n = 0
    for aid in courses:
        for s in series:
            rv = car_vram(car, aid, s)
            for page in (rv.page0, rv.page1):
                vals = [page.pix[i] for i in keys]
                if vals != ref_vals:
                    bad = [(i % 1024, i // 1024) for i, a, b in zip(keys, vals, ref_vals) if a != b][:4]
                    raise ExportError(f"{car.variant.stem}: course {aid} series {s} changes sampled words at {bad}")
                tags = {owner_tag(page, i) for i in keys}
                badt = [t for t in tags if not allowed_tag(car, t)]
                require(not badt, f"{car.variant.stem}: course {aid} series {s} owners {badt}")
            n += 1
    return n


def texture_rgba(v, tpage, clut, allowed):
    """256x256 RGBA of (tpage, clut); texels whose VRAM word is not owned by
    an allowed upload are written transparent."""
    rgba, _, _ = v.sample_page(tpage, clut)
    t = tx.tpage_info(tpage)
    require(t["depth"] == 0, f"tpage {tpage:#06x} not 4-bit")
    out = bytearray(rgba)
    blank = 0
    ok_cache = {}
    for vv in range(256):
        row = (t["y"] + vv) * 1024
        for wx in range(64):
            o = v.owner[row + t["x"] + wx]
            ok = ok_cache.get(o)
            if ok is None:
                ok = ok_cache[o] = allowed(v.log[o][0] if o >= 0 else None)
            if not ok:
                p = (vv * 256 + wx * 4) * 4
                out[p:p + 16] = bytes(16)
                blank += 4
    return bytes(out), blank


def write_textures(leaf, bank, pairs, v, allowed):
    """PNG per material of the (model, k) pairs; returns ({material: info},
    {flat material: rgb})."""
    need, flat = {}, {}
    for mi, k in pairs:
        for f in drawn_faces(bank, mi):
            m = material_of(f, k)
            if f.textured:
                need[m] = (f.tpage, effective_clut(f, k))
            else:
                flat[m] = f.rgb
    info = {}
    (leaf / "textures").mkdir(parents=True, exist_ok=True)
    for m, (tp, cl) in sorted(need.items()):
        rgba, blank = texture_rgba(v, tp, cl, allowed)
        p = tx.write_png(leaf / "textures" / (m + ".png"), 256, 256, rgba)
        cx, cy = tx.clut_xy(cl)
        info[m] = {"tpage": "%04X" % tp, "clut": "%04X" % cl, "clut_xy": [cx, cy],
                   "clut_owner": owner_tag(v, cy * 1024 + cx), "blanked_texels": blank,
                   "bytes": p.stat().st_size}
    return info, flat


def invisible_faces(leaf, bank, pairs):
    """(model, face offset, clut) of drawn textured faces whose UV rectangle
    holds only transparent texels in the written PNG."""
    out = []
    for mi, k in pairs:
        for f in drawn_faces(bank, mi):
            if not f.textured:
                continue
            rgba = _tex_cache(leaf, material_of(f, k))
            us = [q[0] for q in f.uv]
            vv = [q[1] for q in f.uv]
            if not any(rgba[((y * 256) + x) * 4 + 3] for y in range(min(vv), max(vv) + 1)
                       for x in range(min(us), max(us) + 1)):
                out.append((mi, f.off, f.clut))
    _TEX_CACHE.clear()
    return out


# ---------------------------------------------------------------- OBJ

def _apply(R, p):
    return (R[0][0] * p[0] + R[0][1] * p[1] + R[0][2] * p[2],
            R[1][0] * p[0] + R[1][1] * p[1] + R[1][2] * p[2],
            R[2][0] * p[0] + R[2][1] * p[1] + R[2][2] * p[2])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a):
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n) if n > 0 else (0.0, 0.0, 0.0)


def _newell(pts):
    n = [0.0, 0.0, 0.0]
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        n[0] += (p[1] - q[1]) * (p[2] + q[2])
        n[1] += (p[2] - q[2]) * (p[0] + q[0])
        n[2] += (p[0] - q[0]) * (p[1] + q[1])
    return tuple(n)


def written_loop(f, ps1_winding=False):
    """Corner numbers (0..3, strip order) in the order the OBJ face lists them."""
    loop = mb.face_loop(f)                      # v0 v1 v3 v2 (duplicates dropped)
    return loop if ps1_winding else [loop[0]] + loop[:0:-1]


def uv_centring(f, loop):
    """(du, dv) added to the face's texel coordinates before /256.  A face
    whose corners share one u (or one v) reads a single texel column (row) on
    the GPU; at u/256 that lands exactly on a texel border, where nearest or
    linear filtering picks or blends the neighbour, so such an axis is moved
    to the texel centre (+0.5).  Every other axis keeps u/256."""
    if not f.textured:
        return 0.0, 0.0
    us = {f.uv[c][0] for c in loop}
    vs = {f.uv[c][1] for c in loop}
    return (0.5 if len(us) == 1 else 0.0), (0.5 if len(vs) == 1 else 0.0)


def write_obj(bank, instances, path, mtl_name, ps1_winding=False, comments=()):
    """One 'o' per instance (name, model, R, t, clut shift); returns per-object
    stats and the materials used."""
    lines = ["# %s" % c for c in comments]
    lines += ["# axes: Blender X = x, Y = z, Z = -y (raw PS1 vertex units)",
              "# winding: %s" % ("PS1 literal v0 v1 v3 v2 (inward)" if ps1_winding else
                                 "v0 v2 v3 v1 (the PS1 strip loop v0 v1 v3 v2 reversed, outward)"),
              "mtllib %s" % mtl_name]
    vb = vtb = vnb = 1
    stats, mats = [], Counter()
    for name, mi, R, t, k in instances:
        faces = drawn_faces(bank, mi)
        vids = list(dict.fromkeys(i for f in faces for i in f.idx))
        local = {vi: n for n, vi in enumerate(vids)}
        pos = [tuple(a + c for a, c in zip(_apply(R, bank.vertices[vi][:3]), t)) for vi in vids]
        bpos = [mb.to_blender(p) for p in pos]
        vts, vns, recs = OrderedDict(), OrderedDict(), []
        vol = 0.0
        up = centred = 0
        for f in faces:
            loop = written_loop(f, ps1_winding)
            pts = [bpos[local[f.idx[c]]] for c in loop]
            gn = _norm(_newell(pts))
            corners = []
            du, dv = uv_centring(f, loop)
            centred += (du + dv) > 0
            for c in loop:
                vt = None
                if f.textured:
                    u, v = f.uv[c]
                    vt = vts.setdefault((u + du, v + dv), len(vts))
                if f.nrm is not None:
                    n = _norm(mb.to_blender(_apply(R, bank.normals[f.nrm[c]][:3])))
                else:
                    n = gn
                key = tuple(round(x, 4) for x in n)
                vn = vns.setdefault(key, len(vns))
                corners.append((local[f.idx[c]], vt, vn))
            m = material_of(f, k)
            recs.append((m, corners))
            mats[m] += 1
            for j in range(1, len(pts) - 1):
                vol += _dot(pts[0], _cross(pts[j], pts[j + 1])) / 6.0
            up += gn[2] > 0
        lines.append("o %s" % name)
        lines.append("# model %d (%s); R %s; t %s; clut + %d" % (
            mi, mb.model_role(bank, mi), "I" if R == IDENT else "RotY180", t, k))
        lines.extend("v %d %d %d" % p for p in bpos)
        lines.extend("vt %.6f %.6f" % (u / 256.0, 1.0 - v / 256.0) for u, v in vts)
        lines.extend("vn %.4f %.4f %.4f" % n for n in vns)
        cur = None
        for mat, corners in sorted(recs, key=lambda r: r[0]):
            if mat != cur:
                lines.append("usemtl %s" % mat)
                cur = mat
            if corners[0][1] is None:
                lines.append("f " + " ".join("%d//%d" % (vb + v, vnb + n) for v, _, n in corners))
            else:
                lines.append("f " + " ".join("%d/%d/%d" % (vb + v, vtb + t_, vnb + n) for v, t_, n in corners))
        stats.append({"object": name, "model": mi, "clut_shift": k, "vertices": len(vids), "faces": len(faces),
                      "triangles": sum(1 for f in faces if len(mb.face_loop(f)) == 3),
                      "undrawn_skipped": len(drawn_faces(bank, mi, False)) - len(faces),
                      "signed_volume": round(vol, 1), "faces_up": up,
                      "uv_centred_faces": centred})
        vb += len(vids)
        vtb += len(vts)
        vnb += len(vns)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return stats, mats


def check_orientation(label, stats, quad_models):
    """Ground quads face up (+Z), every other object has positive volume."""
    for o in stats:
        if o["model"] in quad_models:
            require(o["faces_up"] == o["faces"], f"{label}: {o['object']} faces down")
        else:
            require(o["signed_volume"] > 0, f"{label}: {o['object']} has negative volume (inside out)")


def normal_agreement(bank, models, ps1_winding=False):
    """(agree, total): GT4 faces whose written-loop normal points the same way
    as the mean of their stored GTE normals (PS1 space)."""
    ok = tot = 0
    for mi in models:
        for f in drawn_faces(bank, mi):
            if f.nrm is None:
                continue
            pts = [bank.vertices[f.idx[c]][:3] for c in written_loop(f, ps1_winding)]
            gn = _newell(pts)
            sn = [sum(bank.normals[f.nrm[c]][k] for c in range(4)) for k in range(3)]
            tot += 1
            ok += _dot(gn, sn) > 0
    return ok, tot


def write_mtl(path, mats, title, flat_rgb):
    lines = ["# %s materials: tp<tpage>_cl<clut> = the 256x256 4-bit page decoded through that CLUT" % title,
             "# (transparent where the colour word is 0x0000); flat_RRGGBB = F4 record colour"]
    for m in sorted(mats):
        lines.append("")
        lines.append("newmtl %s" % m)
        if m.startswith("flat_"):
            r, g, bl = flat_rgb[m]
            lines += ["Ka 0 0 0", "Kd %.4f %.4f %.4f" % (r / 255, g / 255, bl / 255), "Ks 0 0 0", "d 1", "illum 1"]
        else:
            lines += ["Ka 0 0 0", "Kd 1 1 1", "Ks 0 0 0", "d 1", "illum 1",
                      "map_Kd textures/%s.png" % m, "map_d textures/%s.png" % m]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


# ---------------------------------------------------------------- preview (stdlib)

def read_png_rgba(path):
    d = Path(path).read_bytes()
    require(d[:8] == b"\x89PNG\r\n\x1a\n", f"{path}: not a PNG")
    p, idat, w = 8, b"", None
    while p < len(d):
        n = struct.unpack_from(">I", d, p)[0]
        tag, body = d[p + 4:p + 8], d[p + 8:p + 8 + n]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack_from(">IIBB", body)
            require(depth == 8 and ctype == 6, f"{path}: PNG depth {depth} type {ctype}")
        elif tag == b"IDAT":
            idat += body
        p += 12 + n
    raw = zlib.decompress(idat)
    stride = w * 4
    out = bytearray(stride * h)
    prev = bytearray(stride)
    for y in range(h):
        ft = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            bb = prev[i]
            c = prev[i - 4] if i >= 4 else 0
            if ft == 1:
                line[i] = (line[i] + a) & 255
            elif ft == 2:
                line[i] = (line[i] + bb) & 255
            elif ft == 3:
                line[i] = (line[i] + ((a + bb) >> 1)) & 255
            elif ft == 4:
                pa, pb, pc = abs(bb - c), abs(a - c), abs(a + bb - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else bb if pb <= pc else c)) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, bytes(out)


def read_obj(path):
    """(vertices, vts, vns, faces [(object, material, [(v, vt|None, vn|None)])], mtllib)."""
    vs, vts, vns, faces = [], [], [], []
    obj = mat = mtllib = None
    for line in Path(path).read_text(encoding="ascii").splitlines():
        p = line.split()
        if not p or p[0].startswith("#"):
            continue
        if p[0] == "v":
            vs.append(tuple(float(x) for x in p[1:4]))
        elif p[0] == "vt":
            vts.append((float(p[1]), float(p[2])))
        elif p[0] == "vn":
            vns.append(tuple(float(x) for x in p[1:4]))
        elif p[0] == "o":
            obj = p[1]
        elif p[0] == "usemtl":
            mat = p[1]
        elif p[0] == "mtllib":
            mtllib = p[1]
        elif p[0] == "f":
            cs = []
            for c in p[1:]:
                q = c.split("/")
                cs.append((int(q[0]) - 1, int(q[1]) - 1 if len(q) > 1 and q[1] else None,
                           int(q[2]) - 1 if len(q) > 2 and q[2] else None))
            faces.append((obj, mat, cs))
    return vs, vts, vns, faces, mtllib


def read_mtl(path):
    mats, cur = {}, None
    for line in Path(path).read_text(encoding="ascii").splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "newmtl":
            cur = mats.setdefault(p[1], {})
        elif p[0] == "Kd":
            cur["Kd"] = tuple(float(x) for x in p[1:4])
        elif p[0] == "map_Kd":
            cur["map_Kd"] = p[1]
        elif p[0] == "map_d":
            cur["map_d"] = p[1]
    return mats


FONT = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
    "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001001001001",
    "8": "111101111101111", "9": "111101111001111", "A": "010101111101101", "B": "110101110101110",
    "C": "011100100100011", "D": "110101101101110", "E": "111100110100111", "F": "111100110100100",
    "G": "011100101101011", "H": "101101111101101", "I": "111010010010111", "J": "001001001101010",
    "K": "101101110101101", "L": "100100100100111", "M": "101111111101101", "N": "110101101101101",
    "O": "010101101101010", "P": "110101110100100", "Q": "010101101110011", "R": "110101110101101",
    "S": "011100010001110", "T": "111010010010010", "U": "101101101101111", "V": "101101101101010",
    "W": "101101111111101", "X": "101101010101101", "Y": "101101010010010", "Z": "111001010100111",
    "_": "000000000000111", " ": "000000000000000", "=": "000111000111000", "+": "000010111010000",
}


def draw_text(rgb, width, x, y, text, scale=2, colour=(0, 0, 0)):
    for ch in text:
        g = FONT.get(ch, FONT[" "])
        for r in range(5):
            for c in range(3):
                if g[r * 3 + c] == "1":
                    for dy in range(scale):
                        for dx in range(scale):
                            px, py = x + c * scale + dx, y + r * scale + dy
                            o = (py * width + px) * 3
                            if 0 <= px < width and 0 <= o < len(rgb) - 2:
                                rgb[o:o + 3] = bytes(colour)
        x += 4 * scale


def render_obj(obj_path, width=320, height=240, cam=(0.9, -1.1, 0.65), bg=(196, 200, 210), label=None,
               fit=None, margin=0.92):
    """Orthographic, back-face culled, z-buffered, nearest-texel render of an
    OBJ as written (re-read from disk with its MTL and PNGs).  `fit` limits
    the framing to objects whose name contains that text.  Returns
    (rgb bytes, stats)."""
    obj_path = Path(obj_path)
    vs, vts, vns, faces, mtllib = read_obj(obj_path)
    mats = read_mtl(obj_path.parent / mtllib)
    texs = {}
    for name, m in mats.items():
        if "map_Kd" in m:
            w, h, px = read_png_rgba(obj_path.parent / m["map_Kd"])
            require(w == 256 and h == 256, f"{m['map_Kd']}: {w}x{h}")
            texs[name] = px
    cd = _norm(cam)
    right = _norm(_cross((-cd[0], -cd[1], -cd[2]), (0, 0, 1)))
    upv = _cross(right, (-cd[0], -cd[1], -cd[2]))
    light = _norm((0.4, -0.6, 1.0))
    proj = [(_dot(p, right), _dot(p, upv), _dot(p, cd)) for p in vs]
    if fit:
        used = sorted({c[0] for o, _, cs in faces if o and fit in o for c in cs})
        require(used, f"{obj_path.name}: no object matches {fit!r}")
    else:
        used = range(len(proj))
    xs = [proj[i][0] for i in used]
    ys = [proj[i][1] for i in used]
    span = max(max(xs) - min(xs), (max(ys) - min(ys)) * width / height) or 1.0
    s = margin * width / span
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    scr = [((p[0] - cx) * s + width / 2, (cy - p[1]) * s + height / 2, p[2]) for p in proj]
    rgb = bytearray(bytes(bg) * (width * height))
    zb = [-1e30] * (width * height)
    front = back = 0
    tri_list = []
    for _, mat, cs in faces:
        pts = [vs[c[0]] for c in cs]
        n = _norm(_newell(pts))
        if _dot(n, cd) <= 0:
            back += 1
            continue
        front += 1
        shade = 0.45 + 0.55 * max(0.0, _dot(n, light))
        for j in range(1, len(cs) - 1):
            tri_list.append(((cs[0], cs[j], cs[j + 1]), mat, shade))
    for (c0, c1, c2), mat, shade in tri_list:
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = scr[c0[0]], scr[c1[0]], scr[c2[0]]
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-9:
            continue
        x_lo, x_hi = max(0, int(min(x0, x1, x2))), min(width - 1, int(max(x0, x1, x2)) + 1)
        y_lo, y_hi = max(0, int(min(y0, y1, y2))), min(height - 1, int(max(y0, y1, y2)) + 1)
        if x_lo > x_hi or y_lo > y_hi:
            continue
        tex = texs.get(mat)
        if tex is not None:
            (u0, v0), (u1, v1), (u2, v2) = vts[c0[1]], vts[c1[1]], vts[c2[1]]
        kd = mats.get(mat, {}).get("Kd", (1, 0, 1))
        flat = (int(kd[0] * 255 * shade), int(kd[1] * 255 * shade), int(kd[2] * 255 * shade))
        inv = 1.0 / area
        for py in range(y_lo, y_hi + 1):
            yc = py + 0.5
            for px in range(x_lo, x_hi + 1):
                xc = px + 0.5
                w0 = ((x1 - xc) * (y2 - yc) - (x2 - xc) * (y1 - yc)) * inv
                if w0 < 0:
                    continue
                w1 = ((x2 - xc) * (y0 - yc) - (x0 - xc) * (y2 - yc)) * inv
                if w1 < 0:
                    continue
                w2 = 1.0 - w0 - w1
                if w2 < 0:
                    continue
                i = py * width + px
                z = w0 * z0 + w1 * z1 + w2 * z2
                if z <= zb[i]:
                    continue
                if tex is not None:
                    u = w0 * u0 + w1 * u1 + w2 * u2
                    v = w0 * v0 + w1 * v1 + w2 * v2
                    tu = min(255, max(0, int(u * 256)))
                    tv = min(255, max(0, int((1.0 - v) * 256)))
                    o = (tv * 256 + tu) * 4
                    if tex[o + 3] < 128:
                        continue
                    col = (int(tex[o] * shade), int(tex[o + 1] * shade), int(tex[o + 2] * shade))
                else:
                    col = flat
                zb[i] = z
                rgb[i * 3:i * 3 + 3] = bytes(col)
    if label:
        draw_text(rgb, width, 4, 4, label)
    covered = sum(1 for z in zb if z > -1e30)
    return bytes(rgb), {"faces_front": front, "faces_back": back, "pixels": covered}


def write_rgb_png(path, width, height, rgb):
    rgba = bytearray()
    for i in range(width * height):
        rgba += rgb[i * 3:i * 3 + 3] + b"\xff"
    return tx.write_png(path, width, height, bytes(rgba))


def paste(sheet, W, tile, tw, th, ox, oy):
    for y in range(th):
        d = ((oy + y) * W + ox) * 3
        sheet[d:d + tw * 3] = tile[y * tw * 3:(y + 1) * tw * 3]


# ---------------------------------------------------------------- export: player cars

def export_car(variant, tables, ps1_winding=False, preview=True, race=True, log=print):
    t0 = time.time()
    car = load_car(variant, tables)
    leaf = variant.leaf
    (leaf / "textures").mkdir(parents=True, exist_ok=True)
    pairs = [(mi, 0) for mi in range(MODELS_PER_CAR)]
    ref = car_vram(car).page0
    words = sampled_words(car.bank, pairs)
    owners = Counter(owner_tag(ref, i) for i in words)
    bad = {t: n for t, n in owners.items() if not allowed_tag(car, t)}
    require(not bad, f"{variant.stem}: sampled words owned by {bad}")
    courses_checked = race_check(car, ref, words) if race else 0
    tex_info, flat_rgb = write_textures(leaf, car.bank, pairs, ref, lambda tag: allowed_tag(car, tag))
    invisible = invisible_faces(leaf, car.bank, pairs)
    # The team-logo decal samples g_TeamLogoCanvas, which CAR.TMS #39 uploads
    # all zero (the player draws the logo in design mode), so it is invisible
    # until a logo exists; any other fully transparent face is an error.
    logo = [(mi, off) for mi, off, cl in invisible if cl == TEAM_LOGO_CLUT]
    other = [(mi, off) for mi, off, cl in invisible if cl != TEAM_LOGO_CLUT]
    require(not other, f"{variant.stem}: faces sampling only transparent texels {other[:4]}")
    mtl = leaf / (variant.stem + ".mtl")
    objs = {}
    all_mats = Counter()
    head = ["%s (%s, asset %d) car bank; origin = rear axle centre, car faces -Y, ground at Z = %d" % (
        variant.stem, variant.path_1st, variant.asset_1st, -WORLD_TO_VERTEX * car.header["horizon"])]
    td = car.tire_default
    jobs = [("", assemble(car, td, False),
             "assembled as DrawPlayerCarModel draws it at rest, tyre compound %d (g_SaveDefaults default)" % td),
            ("_fast", assemble(car, td, True),
             "assembled, tyre compound %d, speed > 800 wheel set (wheelRotation & 0x1000, otDepth + 10)" % td)]
    for n in range(TIRE_COMPOUNDS):
        jobs.append(("_tire%d" % n, assemble(car, n, False),
                     "assembled, tyre compound %d (renderDepth = CarEntry.tireCompound)" % n))
        jobs.append(("_tire%d_fast" % n, assemble(car, n, True),
                     "assembled, tyre compound %d, speed > 800 wheel set (otDepth + 10)" % n))
    jobs.append(("_all_models", all_models_layout(car.bank, lambda mi: model_kind(mi)[0]),
                 "all 22 bank models at their own origins, spaced along X"))
    for label, inst, title in jobs:
        path = leaf / (variant.stem + label + ".obj")
        st, mats = write_obj(car.bank, inst, path, mtl.name, ps1_winding, [title] + head)
        all_mats.update(mats)
        if label != "_all_models" and not ps1_winding:
            check_orientation(variant.stem + label, st, {1})
        objs[label.lstrip("_") or "assembled"] = {"file": path.name, "objects": st, "faces": sum(s["faces"] for s in st)}
    write_mtl(mtl, all_mats, variant.stem, flat_rgb)
    agree, total = normal_agreement(car.bank, range(MODELS_PER_CAR), ps1_winding)
    checks = {"normal_agreement": [agree, total], "courses_checked": courses_checked,
              "blank_team_logo_faces": [[mi, "%#x" % off] for mi, off in logo],
              "sampled_words": len(words), "sampled_owner_counts": dict(owners)}
    if not ps1_winding:
        require(agree == total, f"{variant.stem}: {total - agree}/{total} faces wind against their stored normals")
    previews = {}
    if preview:
        for label, w, h, cam in (("", 320, 240, (0.9, -1.1, 0.65)), ("_all_models", 960, 200, (0.25, -1.0, 0.35))):
            rgb, st = render_obj(leaf / (variant.stem + label + ".obj"), w, h, cam,
                                 label=variant.disc_stem + " K=%02d T%d" % (variant.k, td) if not label else
                                 variant.disc_stem)
            p = write_rgb_png(leaf / (variant.stem + label + "_preview.png"), w, h, rgb)
            previews[label.lstrip("_") or "assembled"] = dict(st, file=p.name)
            if label == "" and not ps1_winding:
                require(st["faces_front"] > st["faces_back"] * 0.6, f"{variant.stem}: preview culls {st}")
        # front wheel i0 close-ups: tyre compounds 0..4 (top row) and their fast sets (bottom row)
        tw = 128
        W, H = tw * TIRE_COMPOUNDS, tw * 2
        sheet = bytearray(b"\xff\xff\xff" * (W * H))
        for n in range(TIRE_COMPOUNDS):
            for row, suf in enumerate(("", "_fast")):
                rgb, _ = render_obj(leaf / ("%s_tire%d%s.obj" % (variant.stem, n, suf)), tw, tw, (1.0, -0.35, 0.25),
                                    label="T%d%s" % (n, "F" if suf else "") + ("=D" if n == td else ""),
                                    fit="_i0_posx", margin=0.8)
                paste(sheet, W, rgb, tw, tw, n * tw, row * tw)
        p = write_rgb_png(leaf / (variant.stem + "_tires_preview.png"), W, H, sheet)
        previews["tires"] = {"file": p.name}
    size = sum(p.stat().st_size for p in leaf.rglob("*") if p.is_file())
    entry = {
        "k": variant.k, "assets": [variant.asset_1st, variant.asset_2nd],
        "paths": [variant.path_1st, variant.path_2nd], "model": variant.model, "grade": variant.grade,
        "maker": variant.maker, "name": variant.name, "folder": leaf.relative_to(OUT_ROOT).as_posix(),
        "stem": variant.stem, "header": car.header, "geometry": car.geometry, "paint": car.paint,
        "tire_compound_default": td,
        "tire_evidence": "renderDepth (+0xE4) of g_PlayerCar = g_PlayerTireCompound 0x8009E7B8 "
                         "(sym.bss.main.txt:261,271), set from g_CarTable[car].tireCompound "
                         "(swap_car_model_slot.c:113); default g_SaveDefaults (6BE64.data.s:1038)",
        "models": [{"index": mi, "role": mb.model_role(car.bank, mi), "name": model_kind(mi)[0],
                    "faces": len(car.faces(mi)), "faces_total": len(car.faces(mi, False)),
                    "bbox": mb.model_bbox(car.bank, car.bank.models[mi])} for mi in range(MODELS_PER_CAR)],
        "objs": objs, "textures": tex_info, "checks": checks, "previews": previews, "bytes": size,
        "seconds": round(time.time() - t0, 2),
    }
    log("%-26s k=%2d faces %4d (assembled %3d, tyre %d) materials %2d textures %2d  %6.1f KB  %.1fs" % (
        variant.stem, variant.k, sum(e["faces"] for e in entry["models"]), objs["assembled"]["faces"], td,
        len(all_mats), len(tex_info), size / 1024, entry["seconds"]))
    return car, entry


_TEX_CACHE = {}


def _tex_cache(leaf, m):
    key = (leaf, m)
    if key not in _TEX_CACHE:
        _TEX_CACHE[key] = read_png_rgba(leaf / "textures" / (m + ".png"))[2]
    return _TEX_CACHE[key]


# ---------------------------------------------------------------- rival cars

def rival_tables(exe=None):
    """g_CarModelBankTable, g_CarModelByCourse and the grid slot lists out of
    main.exe, asserted against the data asm."""
    exe = exe if exe is not None else _exe_bytes()
    o = _exe_off(EXE_ADDR["g_CarModelBankTable"])
    lod = tuple(struct.unpack_from("<2h", exe, o + 4 * i) for i in range(RIVAL_SLOTS))
    require(lod == ASM_BANK_TABLE, f"g_CarModelBankTable {lod}")
    o = _exe_off(EXE_ADDR["g_CarModelByCourse"])
    by_course = tuple(tuple(exe[o + 11 * c:o + 11 * c + 11]) for c in range(4))
    require(by_course == ASM_BY_COURSE, f"g_CarModelByCourse {by_course}")
    require([list(r) for r in by_course] == ra.exe_tables()["car_model_by_course"], "rr_assets car_model_by_course")
    for name in ("g_RaceGridSlots", "g_AttractGridSlots"):
        g = struct.unpack_from("<12i", exe, _exe_off(EXE_ADDR[name]))
        require(g == ASM_GRID, f"{name} {g}")
    return {"lod": lod, "by_course": by_course, "grid": ASM_GRID}


def render_params(data_2nd):
    """TrackRenderTable (block 0 of the .2ND, camera_types.h): 3 s32 then
    CarModelRenderParams models[11] {axis0, axis1, axis2, horizon}; DrawCar
    copies axis1/axis2 into an s16 vector (draw_player_car_model.c:257-259),
    so all four are read signed."""
    offs = mb.scene_offsets(data_2nd)
    tx.parse_render_table(data_2nd)
    require(offs[1] - offs[0] == 12 + 8 * RIVAL_SLOTS, f"render table is {offs[1] - offs[0]} bytes, want 100")
    return [struct.unpack_from("<4h", data_2nd, offs[0] + 12 + 8 * m) for m in range(RIVAL_SLOTS)]


def pack_class(aid):
    return (aid - 88) >> 3


@dataclass
class RivalGroup:
    packs: list           # .2ND asset ids sharing bank 1 and the render table
    canonical: int
    cls: int              # class the group is named after
    class_folder: str     # CLASS<n>_<g_GrandPrixNames[cls]>
    bank: object
    params: list
    pack_files: dict      # aid -> g_AssetPaths file name


def rival_groups():
    """Partition the 24 .2ND packs by (bank 1 bytes, render table); asserted
    to be the per-class rule with the 094/102 overrides."""
    t = ra.exe_tables()
    keyed = OrderedDict()
    for aid in COURSE_2ND:
        _, d = mb.load_asset(aid)
        b1 = [b for b in mb.banks_for_asset(aid) if b.label == "bank1"]
        require(len(b1) == 1, f"asset {aid}: bank1 {len(b1)}")
        errs = mb.check_bank(b1[0])
        require(errs == [], f"asset {aid}: bank1 check_bank {errs[:3]}")
        key = (b1[0].digest, tuple(render_params(d)))
        keyed.setdefault(key, []).append((aid, b1[0], key[1]))
    groups = []
    expect = defaultdict(set)
    for aid in COURSE_2ND:
        expect[RIVAL_CLASS_OVERRIDE.get(aid, pack_class(aid))].add(aid)
    got = sorted(sorted(a for a, _, _ in v) for v in keyed.values())
    require(got == sorted(sorted(s) for s in expect.values()), f"rival pack partition {got}")
    for members in keyed.values():
        aids = [a for a, _, _ in members]
        cls = Counter(RIVAL_CLASS_OVERRIDE.get(a, pack_class(a)) for a in aids)
        require(len(cls) == 1, f"group {aids} mixes classes {cls}")
        c = next(iter(cls))
        canon = min(a for a in aids if pack_class(a) == c)
        gp = t["gp_names"][c]
        require(re.fullmatch(r"[A-Z0-9]+", gp) is not None, f"g_GrandPrixNames[{c}] = {gp!r}")
        files = {a: re.split(r"[\\/]", t["asset_paths"][a])[-1] for a in aids}
        groups.append(RivalGroup(aids, canon, c, "CLASS%d_%s" % (c + 1, gp), members[0][1],
                                 list(members[0][2]), files))
    return groups


def player_reference(variants):
    """Per player car asset: header (4 s16) and the vertex multisets of its
    rear-axle models (3, 5, .., 21), for rival identification."""
    ref = []
    for v in variants:
        _, d = mb.load_asset(v.asset_1st)
        b = mb.banks_for_asset(v.asset_1st)[0]
        ref.append((v, struct.unpack_from("<4h", d, 0),
                    {mi: vertex_multiset(b, mi) for mi in range(3, MODELS_PER_CAR, 2)}, body_dims(b, 0)))
    return ref


def body_dims(bank, mi):
    """Bounding-box size (x, y, z) of a model, PS1 vertex units."""
    bb = mb.model_bbox(bank, bank.models[mi])
    return [bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2]]


def identify_rival(bank, base, params, ref):
    """Player car assets whose CAR_xy.1ST header equals the render params and
    one of whose rear-axle models equals bank-1 part +3 vertex for vertex:
    [(variant, rear-axle model hits, player body dims)].  The low-poly rival
    body is not a copy of the player body; its size is recorded alongside."""
    axle = vertex_multiset(bank, base + 3)
    out = []
    for v, hdr, axles, dims in ref:
        if tuple(hdr) == tuple(params):
            hits = [mi for mi, vs in axles.items() if vs == axle]
            if hits:
                out.append((v, hits, dims))
    return out


def rival_assemble(base, k, params):
    ax0, ax1, ax2, _ = params
    ox, oy, oz = WORLD_TO_VERTEX * ax0, WORLD_TO_VERTEX * ax1, WORLD_TO_VERTEX * ax2
    return [
        ("m%02d_ground_quad" % (base + 1), base + 1, IDENT, (0, 0, 0), 0),
        ("m%02d_body_clut%d" % (base, k), base, IDENT, (0, 0, 0), k),
        ("m%02d_rear_axle" % (base + 3), base + 3, IDENT, (0, 0, 0), 0),
        ("m%02d_front_wheel_i0_posx" % (base + 2), base + 2, IDENT, (ox, oy, oz), 0),
        ("m%02d_front_wheel_i1_negx" % (base + 2), base + 2, RY180, (-ox, oy, oz), 0),
    ]


def rival_far(base, k):
    return [("m%02d_far_body_clut%d" % (base + 4, k), base + 4, IDENT, (0, 0, 0), k)]


def rival_resolution(count, base):
    """Bank-1 models DrawCar submits for lod base `base` (index or the model-1
    fallback when the index is not below g_ModelBankCount)."""
    return {name: (base + p if base + p < count else 1) for p, name in enumerate(RIVAL_PART_NAMES)}


def rival_vram_check(group, words, ref_words, race=True, unowned=frozenset()):
    """The rival's sampled words equal the canonical race VRAM on both pages
    of every member pack, both GP series and every player car, with owners
    from that pack's .1ST or CAR.TMS (or no upload at all for the texel words
    in `unowned`, which must then stay unwritten everywhere)."""
    keys = sorted(words)
    ref_vals = [ref_words.pix[i] for i in keys]
    runs = [(a, s, 0) for a in group.packs for s in (0, 1)]
    if race:
        runs += [(group.canonical, 0, c) for c in range(1, CAR_COUNT)]
    else:
        runs = [(group.canonical, 0, 0)]
    for aid, s, c in runs:
        rv = _rival_vram(aid, s, c)
        ok = rival_allowed(aid)
        for page in (rv.page0, rv.page1):
            vals = [page.pix[i] for i in keys]
            if vals != ref_vals:
                bad = [(i % 1024, i // 1024) for i, a, b in zip(keys, vals, ref_vals) if a != b][:4]
                raise ExportError(f"rival group {group.packs}: pack {aid} series {s} car {c} differs at {bad}")
            tags = {owner_tag(page, i) for i in keys if i not in unowned}
            tags |= {"written:%s" % owner_tag(page, i) for i in unowned if owner_tag(page, i) is not None}
            badt = [t for t in tags if not ok(t)]
            require(not badt, f"rival group {group.packs}: pack {aid} series {s} car {c} owners {badt}")
    return len(runs)


_VRAM_CACHE = {}


def _rival_vram(aid, series, car):
    key = (aid, series, car)
    if key not in _VRAM_CACHE:
        if len(_VRAM_CACHE) > 64:
            _VRAM_CACHE.clear()
        _VRAM_CACHE[key] = tx.build_race_vram(aid, series=series, car=car)
    return _VRAM_CACHE[key]


def export_rivals(variants, tables, ps1_winding=False, preview=True, race=True, log=print, only_class=None):
    """Every rival model slot of every rival group; returns (entries, not_exported)."""
    rt = rival_tables()
    ref = player_reference(variants)
    entries, skipped = [], []
    for g in rival_groups():
        if only_class is not None and g.cls != only_class:
            continue
        bank = g.bank
        rv = _rival_vram(g.canonical, 0, 0)
        ok = rival_allowed(g.canonical)
        for m in range(RIVAL_SLOTS):
            t0 = time.time()
            base, k = rt["lod"][m]
            res = rival_resolution(bank.count, base)
            slots = {c: [i for i in range(RIVAL_SLOTS) if rt["by_course"][c][i] == m] for c in range(4)}
            if base + 4 >= bank.count - 3:
                # bank 1 ends with 3 scenery models (rr_model_banks.model_role); a lod
                # base whose parts reach them or the fallback is not a car
                skipped.append({"group": g.class_folder, "packs": g.packs, "model_slot": m, "lod": [base, k],
                                "bank1_count": bank.count, "resolves_to": res,
                                "reason": "lod base + 4 >= bank count - 3: DrawCar would submit scenery models "
                                          "or the model-1 fallback (draw_player_car_model.c:212-287), not a car"})
                continue
            params = g.params[m]
            ids = identify_rival(bank, base, params, ref)
            car_models = {v.model for v, _, _ in ids}
            require(len(car_models) <= 1, f"{g.class_folder} model {m}: matches several cars {car_models}")
            if ids:
                # the low-poly body must also be the identified car's size (16 of ~600 units)
                rd = body_dims(bank, base)
                diff = min(max(abs(a - c) for a, c in zip(rd, dims)) for _, _, dims in ids)
                require(diff <= 16, f"{g.class_folder} model {m}: body dims {rd} differ from the identified car by {diff}")
            cname = "%s_MODEL%02d_CLUT%d" % (g.class_folder, m, k)
            variant = "RIVAL_" + cname
            if ids:
                folder = ids[0][0].folder
                stem = folder + "_" + variant
            else:
                folder = "UNNAMED_RIVAL_%s_BASE%02d" % (g.class_folder, base)
                stem = folder + "_MODEL%02d_CLUT%d" % (m, k)
            leaf = OUT_ROOT / folder / variant
            label = stem
            front_c = tuple(WORLD_TO_VERTEX * a for a in params[:3])
            geo = wheel_geometry(bank, label, base, base + 1, [base + 2], [base + 3], front_c, params[3])
            far = drawn_faces(bank, base + 4, False)
            require(far and {f.opcode for f in far} <= {0, 3}, f"{label}: far body opcodes")
            pairs = [(base, k), (base + 1, 0), (base + 2, 0), (base + 3, 0), (base + 4, k)]
            words = sampled_words(bank, pairs)
            owners = Counter(owner_tag(rv.page0, i) for i in words)
            # A few rival faces have UV rectangles that reach one 4-texel word
            # past their image into VRAM no upload writes (value 0 = CLUT
            # index 0).  Those texel words are accepted only if they hold 0 and
            # stay unwritten in every checked VRAM; the PNG writes them
            # transparent like any texel without an allowed owner.
            unowned = frozenset(i for i in words if owner_tag(rv.page0, i) is None)
            require(all(words[i] == "texel" and rv.page0.pix[i] == 0 for i in unowned),
                    f"{label}: unowned CLUT words or non-zero unowned texels "
                    f"{[(i % 1024, i // 1024) for i in sorted(unowned)][:4]}")
            bad = {t: n for t, n in owners.items() if t is not None and not ok(t)}
            require(not bad, f"{label}: sampled words owned by {bad}")
            runs = rival_vram_check(g, words, rv.page0, race, unowned)
            tex_info, flat_rgb = write_textures(leaf, bank, pairs, rv.page0, ok)
            invisible = invisible_faces(leaf, bank, pairs)
            require(not invisible, f"{label}: faces sampling only transparent texels {invisible[:4]}")
            mtl = leaf / (stem + ".mtl")
            files = ", ".join("%d %s" % (a, g.pack_files[a]) for a in g.packs)
            head = ["bank 1 of %s; lod base %d, clut + %d; TrackRenderTable.models[%d] = %s" % (
                files, base, k, m, tuple(params)),
                "origin = rear axle centre, car faces -Y, ground at Z = %d" % (-WORLD_TO_VERTEX * params[3])]
            objs, all_mats = {}, Counter()
            for suf, inst, title in (
                    ("", rival_assemble(base, k, params), "rival car assembled as DrawCar draws it at rest "
                                                          "(near LOD, |dx|+|dz| < 0xD00)"),
                    ("_far", rival_far(base, k), "rival car far LOD (0xD00 <= |dx|+|dz| < 0x2500), "
                                                 "drawn alone with the body matrix")):
                path = leaf / (stem + suf + ".obj")
                st, mats = write_obj(bank, inst, path, mtl.name, ps1_winding, [title] + head)
                all_mats.update(mats)
                if not ps1_winding:
                    check_orientation(stem + suf, st, {base + 1})
                objs[suf.lstrip("_") or "assembled"] = {"file": path.name, "objects": st,
                                                       "faces": sum(s["faces"] for s in st)}
            write_mtl(mtl, all_mats, stem, flat_rgb)
            agree, total = normal_agreement(bank, [base + p for p in range(5)], ps1_winding)
            if not ps1_winding:
                require(agree == total, f"{label}: {total - agree}/{total} faces wind against their stored normals")
            previews = {}
            if preview:
                for suf, w, h in (("", 320, 240), ("_far", 200, 150)):
                    rgb, st = render_obj(leaf / (stem + suf + ".obj"), w, h, (0.9, -1.1, 0.65),
                                         label="C%d M%02d K%d%s" % (g.cls + 1, m, k, " FAR" if suf else ""))
                    p = write_rgb_png(leaf / (stem + suf + "_preview.png"), w, h, rgb)
                    previews[suf.lstrip("_") or "assembled"] = dict(st, file=p.name)
                    if not ps1_winding:
                        require(st["faces_front"] > st["faces_back"] * 0.6, f"{label}{suf}: preview culls {st}")
            size = sum(p.stat().st_size for p in leaf.rglob("*") if p.is_file())
            e = {"group": g.class_folder, "class": g.cls, "packs": g.packs, "pack_files": g.pack_files,
                 "canonical_pack": g.canonical, "model_slot": m, "lod_base": base, "clut_shift": k,
                 "render_params": {"axis0": params[0], "axis1": params[1], "axis2": params[2], "horizon": params[3]},
                 "grid_slots_by_course": slots, "folder": leaf.relative_to(OUT_ROOT).as_posix(), "stem": stem,
                 "body_dims": body_dims(bank, base),
                 "identified_as": [{"k": v.k, "stem": v.stem, "rear_axle_models": hits,
                                    "body_dims": dims,
                                    "body_dims_max_diff": max(abs(a - c) for a, c in zip(dims, body_dims(bank, base)))}
                                   for v, hits, dims in ids],
                 "name_evidence": ("TrackRenderTable.models[%d] == CAR_xy.1ST {modelOffsetX,Y,Z,horizon} and bank1 "
                                   "model %d == that car's rear-axle model vertex for vertex" % (m, base + 3))
                 if ids else "no player car has this header and rear axle; named by indices",
                 "bank1_models": {name: base + p for p, name in enumerate(RIVAL_PART_NAMES)},
                 "geometry": geo, "objs": objs, "textures": tex_info,
                 "checks": {"normal_agreement": [agree, total], "vram_runs": runs, "sampled_words": len(words),
                            "unowned_zero_texel_words": [[i % 1024, i // 1024] for i in sorted(unowned)],
                            "sampled_owner_counts": dict(owners)},
                 "previews": previews, "bytes": size, "seconds": round(time.time() - t0, 2)}
            entries.append(e)
            log("%-62s faces %3d+%2d far, materials %2d, %5.1f KB %.1fs" % (
                stem, objs["assembled"]["faces"], objs["far"]["faces"], len(all_mats), size / 1024, e["seconds"]))
    return entries, skipped


# ---------------------------------------------------------------- sheets / main

def contact_sheets(entries, prefix, per=8, cols=4, w=320, h=240):
    out = []
    for s in range(0, len(entries), per):
        group = entries[s:s + per]
        rows = (len(group) + cols - 1) // cols
        W, H = cols * w, rows * h
        sheet = bytearray(b"\xff\xff\xff" * (W * H))
        for n, e in enumerate(group):
            leaf = OUT_ROOT / e["folder"]
            pw, ph, px = read_png_rgba(leaf / e["previews"]["assembled"]["file"])
            rgb = bytearray()
            for i in range(pw * ph):
                rgb += px[i * 4:i * 4 + 3]
            paste(sheet, W, bytes(rgb), pw, ph, (n % cols) * w, (n // cols) * h)
        p = write_rgb_png(OUT_ROOT / ("%s_%d.png" % (prefix, s // per)), W, H, sheet)
        out.append((p, [e["stem"] for e in group]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", type=int, action="append", help="player car asset index k (0..31), repeatable")
    ap.add_argument("--no-players", action="store_true", help="skip the 32 player cars")
    ap.add_argument("--no-rivals", action="store_true", help="skip the rival cars")
    ap.add_argument("--rival-class", type=int, help="only the rival group of this class (0..5)")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--no-race-check", action="store_true", help="skip the multi-pack / series / car VRAM checks")
    ap.add_argument("--ps1-winding", action="store_true", help="write the literal PS1 loop v0 v1 v3 v2")
    args = ap.parse_args(argv)
    variants = car_variants()
    tables = paint_tables()
    entries, failures, rivals, skipped = [], [], [], []
    full = args.only is None and args.rival_class is None and not args.no_players and not args.no_rivals
    if not args.no_players:
        sel = [v for v in variants if args.only is None or v.k in args.only]
        for v in sel:
            try:
                _, e = export_car(v, tables, args.ps1_winding, not args.no_preview, not args.no_race_check)
                entries.append(e)
            except ExportError as exc:
                failures.append(str(exc))
                print("FAIL", exc)
    if not args.no_rivals and args.only is None:
        try:
            rivals, skipped = export_rivals(variants, tables, args.ps1_winding, not args.no_preview,
                                            not args.no_race_check, only_class=args.rival_class)
        except ExportError as exc:
            failures.append(str(exc))
            print("FAIL", exc)
    if not args.no_preview and full:
        for p, ks in contact_sheets(entries, "preview_sheet"):
            print("sheet", p.relative_to(mb.REPO), len(ks))
        for p, ks in contact_sheets(rivals, "rivals_sheet", per=12):
            print("sheet", p.relative_to(mb.REPO), len(ks))
    if full:
        man = {"source": "tools/export_car_models.py", "root": "extracted/models/cars",
               "axes": "Blender X = x, Y = z, Z = -y (raw PS1 vertex units)",
               "winding": "ps1 literal" if args.ps1_winding else "PS1 loop reversed (outward)",
               "paint_tables": {k: tables[k] for k in ("primary", "secondary")},
               "tire_defaults": [d["tireCompound"] for d in tables["defaults"]],
               "rival_tables": {"g_CarModelBankTable": ASM_BANK_TABLE, "g_CarModelByCourse": ASM_BY_COURSE,
                                "g_RaceGridSlots": ASM_GRID},
               "cars": entries, "rivals": rivals, "rivals_not_exported": skipped, "failures": failures}
        (OUT_ROOT / "cars_manifest.json").write_text(json.dumps(man, indent=1), encoding="ascii")
    tot_faces = sum(sum(m["faces"] for m in e["models"]) for e in entries)
    tot_tex = sum(len(e["textures"]) for e in entries) + sum(len(e["textures"]) for e in rivals)
    tot_bytes = sum(e["bytes"] for e in entries) + sum(e["bytes"] for e in rivals)
    named = sum(1 for e in rivals if e["identified_as"])
    print("exported %d player cars (%d faces), %d rival variants (%d named, %d not exported), %d textures, "
          "%.1f MB; %d failures" % (len(entries), tot_faces, len(rivals), named, len(skipped), tot_tex,
                                    tot_bytes / 1048576, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
