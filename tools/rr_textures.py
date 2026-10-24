#!/usr/bin/env python3
"""
Rage Racer (PAL) texture / VRAM decoder for the course packs.

    python -I tools/rr_textures.py [asset_id | path]   one pack (default 088)
    python -I tools/rr_textures.py --all               every image asset + all 48 track packs
    options: --no-png (skip PNG export)  --out DIR  --series N  --env-mode N

Importable (no side effects on import).  Main entry points:

    parse_tim(data, off)                -> Tim            (one PS1 TIM, as UploadImageBlock reads it)
    parse_image_chain(data, off)        -> ImageChain     (UploadImageAsset's chain of TIMs)
    parse_track_1st(data)               -> Track1st       (<COURSE>n.1ST: 5 image blocks)
    parse_course_models(data_2nd)       -> [CourseModel]  (block 5 of the .2ND, faces with tpage/clut/uv)
    parse_course_objects(data_2nd)      -> [dict]         (block 8 CourseObject records)
    parse_env_palettes / parse_env_script (blocks 1 / 2 of the .2ND)
    parse_car_1st_image(data)           -> ImageRect      (CarImageData, LoadImage'd to g_CarImageRect)
    parse_car_2nd_images(data)          -> ImageChain     (CAR_xx.2ND block 4)
    course_model_draws(aid_2nd, ...)    -> [dict]         (race-time SubmitCourseModel/2 call sites:
                                                           model, CLUT offsets k, condition, evidence)
    runtime_clut_variants(models, draws) -> {(tpage, clut, k): info}  (faces drawn with clut + k)
    check_op3_scroll(models)            (asserts the op3 u-scroll never carries and never accumulates)
    build_race_vram(aid_2nd, ...)       -> RaceVram       (VRAM page 0 and page 1 states + upload log)
    Vram.sample_page(tpage, clut, texwin) -> (rgba bytes 256x256, stp mask)
    write_png(path, w, h, rgba)

Runtime CLUT offset: every course-model emitter adds the scratchpad word
ctx+0x84 to the face word u0v0|clut<<16 (terrain_submission.s:1659-1664,
1712-1716, 1748-1753, 2378-2384, 2613-2619, 2659-2665), and every race-time
draw site writes ctx+0x84 = k<<16 just before SubmitCourseModel/2, so a face
samples clut + k.  The base PNG uses the static clut; each (tpage, clut, k>0)
that a draw site can produce gets its own PNG tpage_XXXX_clut_YYYY_k<k>.png.

Every layout fact used here is listed with its evidence in the notes file
(notes_textures.md); the code asserts the structural invariants it relies on.
"""
import argparse
import json
import struct
import sys
import zlib
from array import array
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "extracted" / "assets_raw"
OUT_DIR = ROOT / "extracted" / "course_textures"

VRAM_W, VRAM_H = 1024, 512

# Fixed asset indices (g_AssetPaths, include/game/asset.h + docs/names.md)
ASSET_LOGO_TMS, ASSET_TITLE_TMS, ASSET_CAR_TMS, ASSET_SAVE_TMS = 0, 1, 5, 6
ASSET_SELECT_BIN, ASSET_OPTION_BIN = 8, 9
ASSET_CAR_BASE = 10            # CAR_xx.1ST = 10 + 2*car, CAR_xx.2ND = 11 + 2*car
ASSET_ROUND_SCREEN_BASE = 0x4A  # GP0..GP11.TMS = 74..85
ASSET_TIME_ATTACK_ROUND_SCREEN = 0x55
ASSET_TRACK_1ST_BASE = 0x57
ASSET_TRACK_2ND_BASE = 0x58

COURSE_NAMES = {0: "BIG_MythicalCoast", 1: "MID_OverPassCity", 2: "HI_LakesideGate", 3: "OVAL_ExtremeOval"}

# VRAM rectangles read from asm/PAL/main/data/main/6BE64.data.s
TRACK_TEXTURE_RECT = (0x240, 0x100, 0x1C0, 0x100)    # g_TrackTextureRect (swap region)
TRACK_TEXTURE_SHADOW_SIZE = 0x38000
CAR_IMAGE_RECT = (0x2C0, 0x000, 0x040, 0x100)        # g_CarImageRect
TEAM_LOGO_CLUT_RECT = (0x010, 0x1E0, 0x10, 0x1)      # g_TeamLogoClutRect (StoreImage at boot)
TEAM_LOGO_CLUT_LOAD_RECT = (0x050, 0x1E5, 0x10, 0x1) # g_TeamLogoClutLoadRect
TEAM_LOGO_CLUT_MOVE_RECT = (0x3F0, 0x0EC, 0x10, 0x1) # g_TeamLogoClutMoveRect, moved to (0x3F0, 0xE2)
TEAM_LOGO_CLUT_MOVE_DST = (0x3F0, 0x0E2)
TEAM_LOGO_RECT = (0x290, 0x030, 0x10, 0x40)          # g_TeamLogoRect (64x64 4bpp canvas)
ENV_CLUT_RECT = (0x0E0, 0x1E6, 0x10, 0x1)            # UpdateEnvironment's LoadImage
RACE_FRAMEBUFFERS = ((0, 0, 0x140, 0xF0), (0, 0xF0, 0x140, 0xF0))  # SetupDisplay240

COURSE_STRIDES = {0: 0x10, 1: 0x1C, 2: 0x20, 3: 0x20}


class FormatError(Exception):
    pass


def _u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def _u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def _s32(d, o):
    return struct.unpack_from("<i", d, o)[0]


def require(cond, msg):
    if not cond:
        raise FormatError(msg)


def load_asset(aid):
    return (ASSETS / f"asset_{aid:03d}.bin").read_bytes()


def pack_name(aid):
    if 87 <= aid <= 134:
        rel = aid - 87
        return f"{COURSE_NAMES[(rel >> 1) & 3]}_class{rel >> 3}_{'1ST' if (rel & 1) == 0 else '2ND'}"
    return f"asset_{aid:03d}"


# --------------------------------------------------------------------------
# PS1 texture-state helpers (libgpu GetTPage / GetClut / DumpTPage / DumpClut)
# --------------------------------------------------------------------------

def tpage_info(tpage):
    """Standard-mode tpage bits (GetTPage, src/main/PAL/lib/libgpu/texture_primitives.c)."""
    return {
        "x": (tpage & 0xF) * 64,
        "y": ((tpage >> 4) & 1) * 256,
        "abr": (tpage >> 5) & 3,
        "depth": (tpage >> 7) & 3,          # 0 4-bit CLUT, 1 8-bit CLUT, 2 15-bit direct
        "extra": tpage >> 9,                # bit 11 (y & 0x200) etc.; expected 0
    }


def clut_xy(clut):
    """GetClut: clut = (y << 6) | ((x >> 4) & 0x3F)."""
    return (clut & 0x3F) * 16, (clut >> 6) & 0x1FF


def make_clut(x, y):
    return ((y << 6) | ((x >> 4) & 0x3F)) & 0xFFFF


def make_tpage(tp, abr, x, y):
    return ((tp & 3) << 7) | ((abr & 3) << 5) | ((y & 0x100) >> 4) | ((x & 0x3FF) >> 6) | ((y & 0x200) << 2)


def texwin_info(word):
    """GP0(E2h) texture window: (u & ~(mask*8)) | ((off & mask)*8), per axis."""
    if word == 0:
        return None
    require((word >> 24) == 0xE2, f"texture window word {word:#010x} is not a GP0(E2h) command")
    mx, my = word & 0x1F, (word >> 5) & 0x1F
    ox, oy = (word >> 10) & 0x1F, (word >> 15) & 0x1F

    def axis(mask, off):
        m = (mask * 8) & 0xFF
        base = (off & mask) * 8
        # period: the window keeps the bits below the lowest masked bit
        if m == 0:
            return {"mask": 0, "base": 0, "period": 256, "regular": True}
        low = m & -m
        regular = (m | (low - 1)) == 0xFF        # masked bits contiguous up to bit 7
        return {"mask": m, "base": base, "period": low, "regular": regular}

    return {"word": word, "u": axis(mx, ox), "v": axis(my, oy)}


def apply_texwin(value, axis):
    return (value & ~axis["mask"] & 0xFF) | axis["base"]


def ps1_to_rgba(c, opaque_black=False):
    """BGR555 -> RGBA8888. 0x0000 is fully transparent, everything else opaque."""
    if c == 0 and not opaque_black:
        return (0, 0, 0, 0)
    r, g, b = c & 31, (c >> 5) & 31, (c >> 10) & 31
    return ((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2), 255)


# --------------------------------------------------------------------------
# Image records: TIM, chains, packs
# --------------------------------------------------------------------------

class ImageRect:
    """One LoadImage: {VRAM x, y, w (16-bit words), h, pixels}."""
    __slots__ = ("x", "y", "w", "h", "pixels", "offset", "kind")

    def __init__(self, x, y, w, h, pixels, offset, kind):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.pixels = pixels
        self.offset = offset
        self.kind = kind

    @property
    def empty(self):
        return not (self.w > 0 and self.h > 0)

    def rect(self):
        return (self.x, self.y, self.w, self.h)

    def __repr__(self):
        return f"{self.kind}({self.x},{self.y} {self.w}x{self.h})"


class Tim:
    """A standard PS1 TIM as UploadImageBlock reads it: id word (0x10), flags,
    optional CLUT block (flags & 8), image block.  Each block is
    {u32 size incl. 12-byte header, u16 x, y, w, h, pixels}."""
    __slots__ = ("offset", "end", "magic", "flags", "pmode", "clut", "image")

    def __init__(self, offset, end, magic, flags, clut, image):
        self.offset, self.end, self.magic, self.flags = offset, end, magic, flags
        self.pmode = flags & 7
        self.clut, self.image = clut, image

    def uploads(self):
        """The LoadImage calls UploadImageBlock makes, in order."""
        out = []
        if self.clut is not None:
            out.append(self.clut)
        if not self.image.empty:
            out.append(self.image)
        return out


def _parse_image_block(d, p, kind):
    require(p + 12 <= len(d), f"{kind} block header at {p:#x} runs off the data")
    size, x, y, w, h = struct.unpack_from("<IHHHH", d, p)
    npix = w * h * 2
    require(size == 12 + npix, f"{kind} block at {p:#x}: size {size:#x} != 12 + {w}x{h}x2")
    require(p + size <= len(d), f"{kind} block at {p:#x} overruns data")
    if w and h:
        require(x + w <= VRAM_W and y + h <= VRAM_H, f"{kind} rect ({x},{y},{w},{h}) outside VRAM")
    return ImageRect(x, y, w, h, bytes(d[p + 12:p + 12 + npix]), p + 12, kind), p + (size >> 2) * 4


def parse_tim(d, off):
    require(off + 8 <= len(d), f"TIM at {off:#x} runs off the data")
    magic, flags = struct.unpack_from("<II", d, off)
    require(magic == 0x10, f"TIM at {off:#x}: id word {magic:#x} != 0x10")
    require(flags & ~0xF == 0 and (flags & 7) <= 3, f"TIM at {off:#x}: odd flags {flags:#x}")
    p = off + 8
    clut = None
    if flags & 8:
        clut, p = _parse_image_block(d, p, "clut")
    image, p = _parse_image_block(d, p, "image")
    return Tim(off, p, magic, flags, clut, image)


class ImageChain:
    """UploadImageAsset: word0 (skipped by the loader), then links
    {s32 size, TIM of exactly `size` bytes}, ended by a link size <= 0."""
    __slots__ = ("offset", "word0", "tims", "end", "terminator")

    def __init__(self, offset, word0, tims, end, terminator):
        self.offset, self.word0, self.tims, self.end, self.terminator = offset, word0, tims, end, terminator

    def uploads(self):
        return [r for t in self.tims for r in t.uploads()]


def parse_image_chain(d, off, limit=None):
    limit = len(d) if limit is None else limit
    require(off + 8 <= limit, f"image chain at {off:#x} runs off the data")
    word0 = _s32(d, off)
    p = off + 4
    tims = []
    while True:
        require(p + 4 <= limit, f"image chain at {off:#x}: no terminator before {limit:#x}")
        size = _s32(d, p)
        p += 4
        if size <= 0:
            return ImageChain(off, word0, tims, p, size)
        tim = parse_tim(d, p)
        require(tim.end == p + size, f"chain link at {p:#x}: TIM ends {tim.end:#x}, link says {p + size:#x}")
        require(p + size <= limit, f"chain link at {p:#x} overruns {limit:#x}")
        tims.append(tim)
        p += size


def parse_scene_offsets(d):
    """GameSceneAssetHeader: offsets[0] is also the header size, so the entry
    count is offsets[0] / 4 (0x14 -> 5 for .1ST and CAR .2ND, 0x2C -> 11 for .2ND)."""
    first = _s32(d, 0)
    require(first % 4 == 0 and 4 <= first <= 0x40, f"implausible header size {first:#x}")
    offs = [_s32(d, 4 * i) for i in range(first // 4)]
    require(all(offs[i] < offs[i + 1] for i in range(len(offs) - 1)) and offs[-1] <= len(d),
            f"header offsets not increasing / in range: {[hex(o) for o in offs]}")
    return offs


class Track1st:
    """<COURSE>n.1ST: header of 5 offsets; blocks 0, 1, 3, 4 are image chains
    (UploadImageAsset), block 2 is one bare TIM (UploadImageBlock)."""

    def __init__(self, aid, data):
        self.aid = aid
        self.size = len(data)
        self.offsets = parse_scene_offsets(data)
        require(len(self.offsets) == 5, f"asset {aid}: .1ST header has {len(self.offsets)} entries, expected 5")
        ends = self.offsets[1:] + [len(data)]
        self.blocks = {}
        for i, (o, e) in enumerate(zip(self.offsets, ends)):
            if i == 2:
                tim = parse_tim(data, o)
                require(tim.end == e, f"asset {aid} block 2 TIM ends {tim.end:#x}, next block {e:#x}")
                self.blocks[i] = tim
            else:
                ch = parse_image_chain(data, o, e)
                require(ch.end == e, f"asset {aid} block {i} chain ends {ch.end:#x}, next block {e:#x}")
                self.blocks[i] = ch
        # StoreTeamLogoImage(pack start) writes 0x38000 bytes over the pack
        # before block 4 is uploaded, so block 4 must lie beyond that.
        require(self.offsets[4] >= TRACK_TEXTURE_SHADOW_SIZE,
                f"asset {aid}: block 4 at {self.offsets[4]:#x} would be clobbered by the shadow StoreImage")

    def block_uploads(self, i):
        b = self.blocks[i]
        return b.uploads()


def parse_track_1st(data, aid=None):
    return Track1st(aid, data)


def parse_car_1st_image(data):
    """CAR_xx.1ST: CarModelAsset at the start, imageData offset at +0x24;
    UploadCarImage LoadImage's 0x40 x 0x100 words from it to g_CarImageRect."""
    off = _s32(data, 0x24)
    x, y, w, h = CAR_IMAGE_RECT
    n = w * h * 2
    require(0 < off and off + n <= len(data), f"car image at {off:#x} + {n:#x} outside pack {len(data):#x}")
    return ImageRect(x, y, w, h, bytes(data[off:off + n]), off, "carimage")


def parse_car_2nd_images(data):
    offs = parse_scene_offsets(data)
    require(len(offs) == 5, f"CAR .2ND header has {len(offs)} entries")
    ch = parse_image_chain(data, offs[4])
    require(ch.end == len(data), f"CAR .2ND block 4 chain ends {ch.end:#x}, pack {len(data):#x}")
    return ch


def parse_select_bin_images(data):
    """SELECT.BIN (asset 8): three s32 offsets then a model bank at +0xC
    (LoadCarSelectAssets: RegisterModelBank(cursor + 0xC)); offsets[2] is the
    image chain handed to UploadImageAsset, running to the end of the file."""
    offs = [_s32(data, 4 * i) for i in range(3)]
    require(0xC < offs[0] < offs[1] < offs[2] < len(data), f"SELECT.BIN offsets {[hex(o) for o in offs]}")
    ch = parse_image_chain(data, offs[2])
    require(ch.end == len(data), "SELECT.BIN image chain does not end at file end")
    return ch


def parse_option_bin_images(data):
    """OPTION.BIN (asset 9): OptionScreenAsset.imageOffset at +0, chain to file end."""
    ch = parse_image_chain(data, _s32(data, 0))
    require(ch.end == len(data), "OPTION.BIN image chain does not end at file end")
    return ch


def parse_env_palettes(data_2nd):
    """Block 1 of the .2ND: EnvironmentPalette records, 16 x Rgb (3 bytes, 5-bit
    components), 48 bytes each; UpdateEnvironment lerps two of them into the
    16-entry CLUT at (0xE0, 0x1E6)."""
    offs = parse_scene_offsets(data_2nd)
    blk = data_2nd[offs[1]:offs[2]]
    require(len(blk) % 48 == 0, f"env palette block size {len(blk)} not a multiple of 48")
    require(max(blk) <= 31, "env palette component above 31 (not 5-bit)")
    pals = []
    for i in range(0, len(blk), 48):
        pals.append([(blk[i + 3 * k], blk[i + 3 * k + 1], blk[i + 3 * k + 2]) for k in range(16)])
    return pals


def env_palette_clut(pal):
    return [r | (g << 5) | (b << 10) for r, g, b in pal]


def parse_env_script(data_2nd):
    """Block 2 of the .2ND (SetEnvironmentScript): u32 skyRowBase, u32 length,
    then 0x30-byte GameEnvironmentCue records {s32 time, colors[9], u16 duration,
    u16 reserved, u16 mode, u16 spareTarget}.

    SeekEnvironmentScript scans until time == -1 (environment_script.c:31-46),
    UpdateEnvironment wraps when the next time < 0 (environment_script.c:138-140).
    The two agree only if the list ends with exactly -1 and every real cue
    time is >= 0, so both are asserted; times must also ascend (the seek's
    `clock < time` scan assumes it)."""
    offs = parse_scene_offsets(data_2nd)
    s, e = offs[2], offs[3]
    require(s + 8 <= e, "env script block shorter than its 8-byte header")
    sky_row_base, length = struct.unpack_from("<Ii", data_2nd, s)
    require(length > 0, f"env script length {length} <= 0 (the seek takes clock modulo it)")
    cues = []
    p = s + 8
    while True:
        require(p + 4 <= e, f"env script has no -1 terminator before block 3 ({e:#x})")
        t = _s32(data_2nd, p)
        if t == -1:
            break
        require(t >= 0, f"env cue time {t} at {p:#x} is negative but not the -1 terminator")
        require(p + 0x30 <= e, "env cue overruns block 2")
        require(not cues or t > cues[-1]["time"], f"env cue times not ascending at {p:#x}")
        dur, _res, mode, spare = struct.unpack_from("<HHHH", data_2nd, p + 0x28)
        cues.append({"time": t, "duration": dur, "mode": mode, "spare": spare})
        p += 0x30
    require(len(cues) >= 2, f"env script has {len(cues)} cues; the seek needs >= 2")
    return {"sky_row_base": sky_row_base, "length": length, "cues": cues, "end": p,
            "terminator_full_record": p + 0x30 <= e}


def parse_render_table(data_2nd):
    """Block 0 of the .2ND = TrackRenderTable (include/game/camera_types.h):
    s32 textureSectionLo, textureSectionHi, environmentScriptOffset, ...
    SelectTrackTexturePage compares the u16 section against [Lo, Hi)
    (track_textures.c:15-35), so both must be non-negative with Lo < Hi."""
    offs = parse_scene_offsets(data_2nd)
    require(offs[0] + 12 <= offs[1], "render table shorter than 3 words")
    lo, hi, env_t0 = struct.unpack_from("<iii", data_2nd, offs[0])
    require(0 <= lo < hi <= 0x10000, f"texture section range [{lo}, {hi}) implausible")
    require(env_t0 >= 0, f"environment script start {env_t0} < 0")
    return {"texture_section_lo": lo, "texture_section_hi": hi, "env_script_start": env_t0}


def env_start_state(script, t0):
    """Env state after race_scene.c's SeekEnvironmentScript(t0) and the one
    UpdateEnvironment it runs (environment_script.c, load_environment_cue.c):
    returns (prev_mode, mode, frac4096, cue index)."""
    cues, length = script["cues"], script["length"]
    require(cues and length > 0, "empty environment script")
    clock = (t0 + length) % length
    count = 0
    while count < len(cues) and not clock < cues[count]["time"]:
        count += 1
    k = count - 2 if count >= 2 else len(cues) - 1
    prev = cues[k]["mode"]
    k1 = (k + 1) % len(cues)
    cue = cues[k1]
    dur = cue["duration"]
    frame = ((clock - cue["time"]) & 0xFFFF)
    if frame >= 0x8000:
        frame -= 0x10000
    if dur < frame:          # (s16)duration < (s16)frame
        frame = dur
    if frame < dur:          # UpdateEnvironment's lerp step
        frame += 1
    # environment_script.c:157 frac = (frame << 12) / duration: the game divides
    # by the cue duration, so a 0 there would be a division by zero.
    require(0 < dur < 0x8000, f"env cue {k1} duration {dur}: 0 divides by zero, >= 0x8000 breaks the s16 clamp")
    require(frame >= 0, f"env seek frame {frame} < 0")
    frac = (frame << 12) // dur
    return prev, cue["mode"], frac, k1


def env_lerp_clut(pals, prev, mode, frac):
    """UpdateEnvironment's 16 CLUT words: GTE Intpl from pals[prev] (IR = c<<4)
    toward the far colour pals[mode] (SetFarColor, FC = c<<4), output MAC>>4;
    bit 15 stays 0."""
    out = []
    for (r1, g1, b1), (r2, g2, b2) in zip(pals[prev], pals[mode]):
        cs = []
        for a, b in ((r1, r2), (g1, g2), (b1, b2)):
            ir = a << 4
            mac = ((ir << 12) + frac * ((b << 4) - ir)) >> 12
            cs.append(min(max(mac >> 4, 0), 31))
        out.append(cs[0] | (cs[1] << 5) | (cs[2] << 10))
    return out


# --------------------------------------------------------------------------
# Course models (block 5 of the .2ND) - layout confirmed by the parent session
# --------------------------------------------------------------------------

class CourseFace:
    __slots__ = ("model", "index", "opcode", "idx", "rgb", "code", "uv", "clut", "tpage", "bias",
                 "subdiv", "texwin", "offset")

    def __init__(self, model, index, opcode, raw, offset):
        self.model, self.index, self.opcode, self.offset = model, index, opcode, offset
        self.idx = struct.unpack_from("<4H", raw, 0)
        r, g, b, code = struct.unpack_from("<4B", raw, 8)
        self.rgb, self.code = (r, g, b), code
        self.uv = self.clut = self.tpage = self.subdiv = None
        self.bias, self.texwin = 0, 0
        if opcode >= 1:
            u0, v0, clut, u1, v1, tpage, u2, v2, u3, v3 = struct.unpack_from("<BBHBBHBBBB", raw, 0x0C)
            self.uv = ((u0, v0), (u1, v1), (u2, v2), (u3, v3))
            self.clut, self.tpage = clut, tpage
            self.bias = struct.unpack_from("<b", raw, 0x19)[0]
        if opcode >= 2:
            self.subdiv = struct.unpack_from("<BB", raw, 0x1A)
            self.texwin = _u32(raw, 0x1C)

    @property
    def textured(self):
        return self.opcode >= 1

    @property
    def semi_transparent(self):
        return bool(self.code & 2)

    @property
    def material(self):
        """Matches tools/export_course_models.py ObjWriter.material_for."""
        if not self.textured:
            return None
        return f"tpage_{self.tpage:04X}_clut_{self.clut:04X}"


class CourseModel:
    def __init__(self, index, geom_off, vcount, model_off):
        self.index, self.geom_off, self.vcount, self.model_off = index, geom_off, vcount, model_off
        self.verts = []
        self.faces = []
        self.commands = []       # [(opcode, face count, offset in block 5)]
        self.stream_end = None


def parse_course_models(data_2nd):
    offs = parse_scene_offsets(data_2nd)
    require(len(offs) == 11, f".2ND header has {len(offs)} entries")
    table = data_2nd[offs[5]:offs[6]]
    count = _s32(table, 0)
    require(0 < count < 1024 and 4 + 12 * count <= len(table), f"course model count {count}")
    models = []
    for i in range(count):
        g, n, m = struct.unpack_from("<iii", table, 4 + 12 * i)
        mdl = CourseModel(i, g, n, m)
        require(0 < n <= 120 and 0 <= g and g + 8 * n <= len(table), f"model {i}: geometry {g:#x} x{n}")
        mdl.verts = [struct.unpack_from("<hhhH", table, g + 8 * j) for j in range(n)]
        p = m
        while True:
            require(0 <= p and p + 4 <= len(table), f"model {i}: stream runs off block 5")
            cmd = _u32(table, p)
            p += 4
            if cmd == 0:
                mdl.stream_end = p
                break
            op, cnt = cmd & 0xFFFF, cmd >> 16
            require(op in COURSE_STRIDES, f"model {i}: unknown opcode {op:#x} at {p - 4:#x}")
            st = COURSE_STRIDES[op]
            require(p + cnt * st <= len(table), f"model {i}: op {op} x{cnt} overruns block 5")
            mdl.commands.append((op, cnt, p - 4))
            for k in range(cnt):
                f = CourseFace(i, len(mdl.faces), op, table[p + k * st:p + (k + 1) * st], offs[5] + p + k * st)
                require(max(f.idx) < n, f"model {i} face {f.index}: index {max(f.idx)} >= {n}")
                mdl.faces.append(f)
            p += cnt * st
        models.append(mdl)
    return models


OP3_SCROLL_MAX = 0x7F      # g_AnimTimer & 0x7F (terrain_submission.s:2588-2592, 2630-2634)


def check_op3_scroll(models):
    """Assert the two assumptions face_texel_rect makes about the op3 scroll.

    func_8002A550 (terrain_submission.s:2588-2593 and the fog twin 2630-2635)
    does ctx+0x84 += g_AnimTimer & 0x7F and stores it back; nothing restores
    it before the next command.  Per face it adds the whole word to
    u0v0|clut and its low half to u1v1|tpage, u2v2 and u3v3 as 16-bit adds
    (2613-2626, 2659-2672).  So:
      (a) a u + scroll above 0xFF would carry into v (and, for u1v1, v into
          tpage) - asserted never to happen: max u + 0x7F <= 0xFF;
      (b) a second op3 command, or any textured command after an op3 one in the
          same model, would see the accumulated scroll - asserted absent.
    Returns {model index: number of op3 faces}."""
    out = {}
    for m in models:
        ops = [c[0] for c in m.commands]
        if 3 not in ops:
            continue
        require(ops.count(3) == 1, f"model {m.index}: {ops.count(3)} op3 commands (scroll would accumulate)")
        require(ops.index(3) == len(ops) - 1,
                f"model {m.index}: commands {ops} follow op3 (they would inherit the accumulated scroll)")
        n = 0
        for f in m.faces:
            if f.opcode != 3:
                continue
            n += 1
            require(max(uv[0] for uv in f.uv) + OP3_SCROLL_MAX <= 0xFF,
                    f"model {m.index} face {f.index}: u + scroll carries into v")
        out[m.index] = n
    return out


def face_texel_rect(face, inclusive=True):
    """(u0, u1, v0, v1) inclusive texel bounds the face can sample inside its
    256x256 page, after the texture window (opcodes 2/3) and the opcode-3 UV
    scroll (which adds 0..127 to every u, so a scrolling face may sample the
    whole window tile horizontally).  The scroll is a 16-bit add on u|v<<8;
    check_op3_scroll asserts it never carries into v, which is required here."""
    us = [uv[0] for uv in face.uv]
    vs = [uv[1] for uv in face.uv]
    umin, umax, vmin, vmax = min(us), max(us), min(vs), max(vs)
    if not inclusive:
        umax = max(umin, umax - 1)
        vmax = max(vmin, vmax - 1)
    tw = texwin_info(face.texwin) if face.texwin else None
    if face.opcode == 3:
        require(max(us) + OP3_SCROLL_MAX <= 0xFF, f"model {face.model} face {face.index}: op3 scroll carries into v")
        umax = umax + OP3_SCROLL_MAX
    if tw is None:
        return umin, umax, vmin, vmax
    ueff = {apply_texwin(u & 0xFF, tw["u"]) for u in range(umin, umax + 1)}
    veff = {apply_texwin(v & 0xFF, tw["v"]) for v in range(vmin, vmax + 1)}
    return min(ueff), max(ueff), min(veff), max(veff)


# --------------------------------------------------------------------------
# Race-time course-model draw sites: which model is drawn with which CLUT
# offset k (ctx+0x84 = k << 16).  Every SubmitCourseModel / SubmitCourseModel2
# caller in src/main/PAL (no asm caller remains; swap_car_model_slot.c is the
# menu) is listed with its file:line.  Every caller clamps the model index with
# (idx < g_CourseModelCount) ? idx : 1.
# --------------------------------------------------------------------------

def parse_course_objects(data_2nd):
    """Block 8 of the .2ND = CourseObjectTable (include/game/track_internal.h:8-22):
    u32 count, then count x CourseObject {s16 modelId, s16 rotationY, s32 x, y,
    z, s32 flags} (0x14 bytes); modelId -1 is skipped (visible_cells.c:37)."""
    offs = parse_scene_offsets(data_2nd)
    b, e = offs[8], offs[9]
    n = _u32(data_2nd, b)
    require(e - b == 4 + 20 * n, f"block 8 size {e - b:#x} != 4 + 20*{n}")
    out = []
    for i in range(n):
        mid, rot, x, y, z, fl = struct.unpack_from("<hhiiii", data_2nd, b + 4 + 20 * i)
        out.append({"index": i, "model": mid, "rotation_y": rot, "x": x, "y": y, "z": z, "flags": fl})
    return out


def pack_course_class(aid_2nd):
    """(g_CourseIndex & 3, g_GrandPrixClass) for a .2ND id, from the load
    formula LoadAsset(g_CourseIndex*2 + g_GrandPrixClass*8 + 0x57) + 1
    (race_assets.c:98-100).  Asset 094 is therefore course 3 even though its
    content looks like BIG (notes, hypothesis 8)."""
    rel = aid_2nd - ASSET_TRACK_2ND_BASE
    require(0 <= rel < 48 and rel % 2 == 0, f"{aid_2nd} is not a track .2ND pack")
    return (rel >> 1) & 3, rel >> 3


def env_mode4_states(aid_2nd, data_2nd=None):
    """Which values g_IsEnvironmentMode4 takes during the race.

    LoadEnvironmentCue sets it to (mode == 4) (load_environment_cue.c:54-59).
    The seek loads the start cue (environment_script.c:60-81).  For class >= 5
    the seek then disables the script (environment_script.c:96-98) and
    UpdateEnvironment returns at once (130-132), so the start value holds for
    the whole race; otherwise every cue of the script is reached in turn."""
    d = load_asset(aid_2nd) if data_2nd is None else data_2nd
    _course, cls = pack_course_class(aid_2nd)
    script = parse_env_script(d)
    t0 = parse_render_table(d)["env_script_start"]
    _prev, mode, _frac, k1 = env_start_state(script, t0)
    start = mode == 4
    states = {start}
    if cls < 5:
        states |= {c["mode"] == 4 for c in script["cues"]}
    mode4_times = [c["time"] for c in script["cues"] if c["mode"] == 4]
    return {"start": start, "states": sorted(states), "script_enabled": cls < 5,
            "start_cue": k1, "t0": t0, "mode4_cue_times": mode4_times}


ANIM_TINT_KS = (0, 1, 2, 3)     # g_AnimSceneryTint = ((timer >> 3) & 3) << 16, timer = g_SceneTimer


def course_model_draws(aid_2nd, data_2nd=None, model_count=None, gp_mode=True):
    """Race-time course-model draws for one pack.  Returns a list of dicts
    {models, ks, site, condition, evidence, active}; `ks` are the CLUT
    offsets the site writes to ctx+0x84 (as k, i.e. ctx+0x84 = k << 16)."""
    d = load_asset(aid_2nd) if data_2nd is None else data_2nd
    course, cls = pack_course_class(aid_2nd)
    if model_count is None:
        model_count = len(parse_course_models(d))
    objects = parse_course_objects(d)
    m4 = env_mode4_states(aid_2nd, d)

    def cm(i):
        return i if i < model_count else 1

    out = []

    def add(active, models, ks, site, condition, evidence):
        out.append({"models": sorted({cm(i) for i in models}), "ks": tuple(sorted(set(ks))), "site": site,
                    "condition": condition, "evidence": evidence, "active": bool(active)})

    # DrawCourseObjects: flags & 8 -> ((g_AnimTimer & 0x10) == 0) << 16, else
    # flags & 4 -> 0x10000, else 0; then SubmitCourseModel2 if (mode4 ? flags & 2
    # : flags & 1) else SubmitCourseModel.
    grp = defaultdict(int)
    for o in objects:
        mid = o["model"]
        if mid == -1:
            continue
        require(0 <= mid < model_count, f"course object {o['index']}: model {mid} outside 0..{model_count - 1}")
        fl = o["flags"]
        ks = (0, 1) if fl & 8 else (1,) if fl & 4 else (0,)
        grp[(mid, ks, fl)] += 1
    for (mid, ks, fl), n in sorted(grp.items()):
        how = ("blinks k=1 while (g_AnimTimer & 0x10) == 0, 16 frames on / 16 off" if fl & 8
               else "k=1 always" if fl & 4 else "k=0")
        add(True, [mid], ks, "DrawCourseObjects",
            f"{n} block-8 object(s) flags {fl:#x}: {how}; culled by g_VisibleCellMask",
            "visible_cells.c:80-93")
    # DrawStaticScenery: model 0x3A in env mode 4 (SubmitCourseModel), else 0x39 (SubmitCourseModel2), k = 0.
    add(True in m4["states"], [0x3A], (0,), "DrawStaticScenery", "env mode 4", "scenery.c:64-72")
    add(False in m4["states"], [0x39], (0,), "DrawStaticScenery", "env mode != 4", "scenery.c:73-81")
    # DrawHighClassScenery: course 0, class >= 4; model 0x3F, k = 1 in env mode 4 else 0.
    hc = course == 0 and cls >= 4
    add(hc and True in m4["states"], [0x3F], (1,), "DrawHighClassScenery",
        "course 0, class >= 4, env mode 4: SubmitCourseModel with ctx+0x84 = 0x10000"
        + (" for the whole race (class 5 freezes the seek state)" if not m4["script_enabled"]
           else f" (from cue time {m4['mode4_cue_times']})"),
        "scenery.c:97-104, 128-132, environment_script.c:96-98")
    add(hc and False in m4["states"], [0x3F], (0,), "DrawHighClassScenery",
        "course 0, class >= 4, env mode != 4: SubmitCourseModel2 with ctx+0x84 = 0", "scenery.c:105-112")
    # DrawAnimatedScenery (race): GP mode, class != 5; instance 0, plus 1 on course 3.
    an = gp_mode and cls != 5
    add(an, range(10, 23), (0,), "DrawAnimatedScenery", "GP, class != 5, position 1..3, frame < 13",
        "dynamic_track_views.c:270-276")
    add(an, range(1, 4), (0,), "DrawAnimatedScenery", "GP, class != 5, frame >= 13: model = position 1..3",
        "dynamic_track_views.c:277-282")
    add(an, range(4, 7), ANIM_TINT_KS, "DrawAnimatedScenery",
        "GP, class != 5, position 1..3: model 4 + (Random15()&7)/3, k = (g_SceneTimer>>3)&3 (cycles every 8 frames)",
        "dynamic_track_views.c:249-256, 268, 285-291")
    add(an, range(0x18, 0x28), (0,), "DrawAnimatedScenery", "GP, class != 5, other positions: frame + 0x18",
        "dynamic_track_views.c:293-299")
    add(an, range(7, 10), ANIM_TINT_KS, "DrawAnimatedScenery",
        "GP, class != 5, other positions: model 7 + (Random15()&7)/3, k = (g_SceneTimer>>3)&3",
        "dynamic_track_views.c:300-307")
    # DrawAnimatedScenery2 (DrawCourseScenery2: prologue / BGM select / attract; replay when g_SceneId == 0x11).
    add(an, range(10, 26), (0,), "DrawAnimatedScenery2", "GP, class != 5, replay scene: frame + 0xA",
        "dynamic_track_views.c:375-381, scenery.c:172")
    add(an, range(4, 7), ANIM_TINT_KS, "DrawAnimatedScenery2", "GP, class != 5, replay scene: variant + 4, tint k",
        "dynamic_track_views.c:365, 373, 383-391, 410-414")
    add(an, range(0x18, 0x28), (0,), "DrawAnimatedScenery2", "GP, class != 5: frame + 0x18",
        "dynamic_track_views.c:393-398")
    add(an, range(7, 10), ANIM_TINT_KS, "DrawAnimatedScenery2", "GP, class != 5: variant + 7, tint k",
        "dynamic_track_views.c:400-414")
    # DrawStartGridScenery: GP, class != 5, race phase < 2, scene timer >= 0x51: 0x28 + (0..14).
    add(an, range(0x28, 0x37), (0,), "DrawStartGridScenery", "GP, class != 5, before the start",
        "dynamic_track_views.c:162-194, race_scene.c:296-298")
    # DrawSpinningScenery: course 0, or course 1 from class 2; model 0x3E, k = 0.
    add(course == 0 or (course == 1 and cls >= 2), [0x3E], (0,), "DrawSpinningScenery",
        "course 0; course 1 class >= 2", "draw_spinning_scenery.c:67-72, scenery.c:128-129, 136-137")
    # DrawShuttleScenery: course 1 (one), course 2 (two); 0x3F, or 0x3C when course & 3 >= 2; k = 0.
    add(course in (1, 2), [0x3C if course >= 2 else 0x3F], (0,), "DrawShuttleScenery",
        "course 1 and 2", "update_shuttle_scenery.c:77-133 (model 116-125, ctx 128), scenery.c:142, 150-151")
    return out


def drawn_models(draws):
    return sorted({m for d in draws if d["active"] for m in d["models"]})


def runtime_clut_variants(models, draws):
    """{(tpage, clut, k): {"faces", "models", "sites", "conditions"}} for every
    textured face of an active draw site whose k != 0; the face then samples
    CLUT (clut + k) & 0xFFFF (the emitters' 32-bit add carries k into the clut
    halfword only, since the low half of ctx+0x84 is 0 at these sites)."""
    out = {}
    for d in draws:
        if not d["active"]:
            continue
        for k in d["ks"]:
            if k == 0:
                continue
            for mi in d["models"]:
                for f in models[mi].faces:
                    if not f.textured:
                        continue
                    e = out.setdefault((f.tpage, f.clut, k), {"faces": [], "models": set(), "sites": set(),
                                                              "conditions": set()})
                    if f not in e["faces"]:
                        e["faces"].append(f)
                    e["models"].add(mi)
                    e["sites"].add(f"{d['site']} ({d['evidence']})")
                    e["conditions"].add(d["condition"])
    return out


def clut_tim_sources(track1st, clut, entries):
    """.1ST TIMs whose CLUT block covers the 16 words of `clut` and holds exactly
    `entries` there: [(block, tim number, clut_only)]."""
    cx, cy = clut_xy(clut)
    out = []
    for b in (0, 1, 2, 3, 4):
        blk = track1st.blocks[b]
        tims = [blk] if isinstance(blk, Tim) else blk.tims
        for n, t in enumerate(tims):
            c = t.clut
            if c is None or not (c.x <= cx and cx + 16 <= c.x + c.w and c.y <= cy < c.y + c.h):
                continue
            o = ((cy - c.y) * c.w + (cx - c.x)) * 2
            if list(struct.unpack_from("<16H", c.pixels, o)) == list(entries):
                out.append((b, n, t.image.empty))
    return out


# --------------------------------------------------------------------------
# VRAM model
# --------------------------------------------------------------------------

class Vram:
    """1024x512 16-bit VRAM with a per-word owner map into an upload log."""

    def __init__(self):
        self.pix = array("H", bytes(VRAM_W * VRAM_H * 2))
        self.owner = array("h", [-1]) * (VRAM_W * VRAM_H)
        self.log = []            # [(tag, category, x, y, w, h, paired clut)]

    def copy(self):
        v = Vram.__new__(Vram)
        v.pix = array("H", self.pix)
        v.owner = array("h", self.owner)
        v.log = list(self.log)
        return v

    def _log(self, tag, category, x, y, w, h, pair_clut=None):
        self.log.append((tag, category, x, y, w, h, pair_clut))
        return len(self.log) - 1

    def upload(self, x, y, w, h, pixels, tag, category, pair_clut=None):
        """LoadImage(rect, pixels); rects are asserted in bounds (no wrap needed)."""
        require(len(pixels) >= w * h * 2, f"{tag}: {len(pixels)} bytes for {w}x{h}")
        require(0 <= x and 0 <= y and x + w <= VRAM_W and y + h <= VRAM_H, f"{tag}: rect outside VRAM")
        li = self._log(tag, category, x, y, w, h, pair_clut)
        for r in range(h):
            row = array("H", pixels[r * w * 2:(r + 1) * w * 2])
            base = (y + r) * VRAM_W + x
            self.pix[base:base + w] = row
            self.owner[base:base + w] = array("h", [li]) * w
        return li

    def upload_rect(self, rect, tag, category, pair_clut=None):
        if rect.empty:
            return None
        return self.upload(rect.x, rect.y, rect.w, rect.h, rect.pixels, tag, category, pair_clut)

    def upload_tim(self, tim, tag, category):
        """UploadImageBlock: CLUT first, then the image unless w or h is 0.  The
        image's log entry remembers the TIM's own CLUT (as a clut value)."""
        pc = make_clut(tim.clut.x, tim.clut.y) if tim.clut is not None else None
        for r in tim.uploads():
            self.upload_rect(r, f"{tag}:{r.kind}", category, pc if r.kind == "image" else None)

    def upload_chain(self, chain, tag, category):
        for n, tim in enumerate(chain.tims):
            self.upload_tim(tim, f"{tag}#{n}", category)

    def store(self, x, y, w, h):
        out = array("H")
        for r in range(h):
            base = (y + r) * VRAM_W + x
            out.extend(self.pix[base:base + w])
        return out.tobytes()

    def store_owner(self, x, y, w, h):
        out = array("h")
        for r in range(h):
            base = (y + r) * VRAM_W + x
            out.extend(self.owner[base:base + w])
        return out

    def load_owner(self, x, y, w, h, owners):
        for r in range(h):
            base = (y + r) * VRAM_W + x
            self.owner[base:base + w] = owners[r * w:(r + 1) * w]

    def move(self, sx, sy, w, h, dx, dy, tag, category):
        """MoveImage: copy, and the destination is owned by a new log entry."""
        data = self.store(sx, sy, w, h)
        return self.upload(dx, dy, w, h, data, tag, category)

    def word(self, x, y):
        return self.pix[y * VRAM_W + x]

    def clut_entries(self, clut, n=16):
        cx, cy = clut_xy(clut)
        return [self.pix[cy * VRAM_W + ((cx + i) & 0x3FF)] for i in range(n)]

    def texel_index(self, tpage, u, v):
        t = tpage_info(tpage)
        y = t["y"] + v
        if t["depth"] == 0:
            w = self.pix[y * VRAM_W + ((t["x"] + (u >> 2)) & 0x3FF)]
            return (w >> ((u & 3) * 4)) & 0xF
        if t["depth"] == 1:
            w = self.pix[y * VRAM_W + ((t["x"] + (u >> 1)) & 0x3FF)]
            return (w >> ((u & 1) * 8)) & 0xFF
        return self.pix[y * VRAM_W + ((t["x"] + u) & 0x3FF)]

    def sample_page(self, tpage, clut, texwin=0):
        """Sample the whole 256x256 texture page through `clut` (4/8-bit) or
        direct (15-bit), optionally through a GP0(E2h) texture window.
        Returns (RGBA bytes, STP bytearray (bit 15 per texel), raw colour array)."""
        t = tpage_info(tpage)
        tw = texwin_info(texwin) if texwin else None
        depth = t["depth"]
        px, py = t["x"], t["y"]
        pal = self.clut_entries(clut, 16 if depth == 0 else 256) if depth in (0, 1) else None
        uidx = [apply_texwin(u, tw["u"]) if tw else u for u in range(256)]
        cols = array("H")
        pix = self.pix
        for v in range(256):
            vv = apply_texwin(v, tw["v"]) if tw else v
            rowbase = ((py + vv) & 0x1FF) * VRAM_W
            if depth == 0:
                words = [pix[rowbase + ((px + k) & 0x3FF)] for k in range(64)]
                idx = [(words[k >> 2] >> ((k & 3) * 4)) & 0xF for k in range(256)]
                cols.extend(pal[idx[u]] for u in uidx)
            elif depth == 1:
                words = [pix[rowbase + ((px + k) & 0x3FF)] for k in range(128)]
                idx = [(words[k >> 1] >> ((k & 1) * 8)) & 0xFF for k in range(256)]
                cols.extend(pal[idx[u]] for u in uidx)
            else:
                words = [pix[rowbase + ((px + k) & 0x3FF)] for k in range(256)]
                cols.extend(words[u] for u in uidx)
        lut = {c: bytes(ps1_to_rgba(c)) for c in set(cols)}
        rgba = b"".join(lut[c] for c in cols)
        stp = bytearray(c >> 15 for c in cols)
        return rgba, stp, cols

    def to_rgba_direct(self, opaque=True):
        """Whole VRAM viewed as 15-bit direct colour (4/8-bit areas look like noise);
        opaque=True shows 0x0000 as opaque black so empty VRAM is visible."""
        lut = {c: bytes(ps1_to_rgba(c, opaque_black=opaque)) for c in set(self.pix)}
        return b"".join(lut[c] for c in self.pix)

    OWNER_COLORS = {"boot": (90, 90, 90), "menu": (120, 60, 140), "car": (60, 140, 200),
                    "course": (40, 170, 70), "course_blk4": (200, 160, 40), "env": (230, 60, 60),
                    "teamlogo": (240, 120, 200)}

    def owner_category_rgba(self):
        """Ownership map: black never written, grey boot, purple menu, blue car,
        green .1ST blocks 0..3, amber .1ST block 4, red env CLUT, pink team logo."""
        cat = [bytes(self.OWNER_COLORS.get(e[1], (255, 255, 255)) + (255,)) for e in self.log]
        black = bytes((0, 0, 0, 255))
        return b"".join(black if o < 0 else cat[o] for o in self.owner)

    def tim_view_rgba(self, chains):
        """4096x512 view: every TIM image of `chains` drawn at 4x width through its
        own CLUT as it stands in this VRAM (4-bit TIMs only; others skipped)."""
        out = bytearray(4096 * 512 * 4)
        for ch in chains:
            tims = [ch] if isinstance(ch, Tim) else ch.tims
            for t in tims:
                if t.pmode != 0 or t.clut is None or t.image.empty:
                    continue
                pal = [bytes(ps1_to_rgba(c)) for c in self.clut_entries(make_clut(t.clut.x, t.clut.y))]
                im = t.image
                for r in range(im.h):
                    y = im.y + r
                    row = bytearray()
                    for k in range(im.w):
                        w = self.pix[y * VRAM_W + im.x + k]
                        for n in range(4):
                            row += pal[(w >> (4 * n)) & 0xF]
                    o = (y * 4096 + im.x * 4) * 4
                    out[o:o + len(row)] = row
        return bytes(out)


# --------------------------------------------------------------------------
# PNG writer (stdlib only)
# --------------------------------------------------------------------------

def write_png(path, width, height, rgba):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stride = width * 4
    require(len(rgba) == stride * height, f"PNG {path.name}: {len(rgba)} bytes for {width}x{height}")
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    def chunk(tag, body):
        c = struct.pack(">I", len(body)) + tag + body
        return c + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)
    return path


def crop_rgba(rgba, width, x, y, w, h):
    out = bytearray()
    for r in range(y, y + h):
        out.extend(rgba[(r * width + x) * 4:(r * width + x + w) * 4])
    return bytes(out)


# --------------------------------------------------------------------------
# Race VRAM reconstruction
# --------------------------------------------------------------------------

class RaceVram:
    """VRAM during a race of one course/class.

    page0: state after LoadRaceAssets step 5 (block 4 of the .1ST on top) - what
           VRAM holds when the car is outside [textureSectionLo, Hi).
    page1: the same with the g_TrackTextureRect rows exchanged for the shadow
           (blocks 0..3 only) - what StepTrackTextureSwap converges to inside
           that section window.
    """

    def __init__(self, aid_2nd, page0, page1, track1st, env_mode, env_script, notes):
        self.aid_2nd = aid_2nd
        self.page0, self.page1 = page0, page1
        self.track1st = track1st
        self.env_mode = env_mode
        self.env_script = env_script
        self.notes = notes
        self.tims = tim_index(track1st)      # {clut value: [(block, image rect)]}

    def page(self, n):
        return self.page1 if n == 1 else self.page0


def build_race_vram(aid_2nd, series=0, car=0, gp_class=None, env_mode=None, menu_history=True,
                    car_image=None):
    """Replay the VRAM uploads from boot to the first race frame (see notes).

    Pre-race uploads (boot/menu) are replayed so that stale texels are visible
    as such; they do not change any texel the race load writes.  `car` is the
    player car index (CAR_xx assets 10 + 2*car / 11 + 2*car).  `car_image`
    optionally replaces the 0x8000-byte car image (e.g. after the body paint
    of export_car_models.apply_paint); None uploads the pack bytes as stored."""
    require(88 <= aid_2nd <= 134 and (aid_2nd - 88) % 2 == 0, f"{aid_2nd} is not a track .2ND pack")
    rel = aid_2nd - ASSET_TRACK_2ND_BASE
    cls = rel >> 3
    if gp_class is None:
        gp_class = cls
    v = Vram()
    notes = []
    team_clut = bytearray(32)      # g_TeamLogoClut (bss, zero) when the boot path is not replayed
    if menu_history:
        # InitAssetSystem: asset 0;  LoadBootAssets: 1 (TITLE), 5 (CAR.TMS)
        for aid, cat in ((ASSET_LOGO_TMS, "boot"), (ASSET_TITLE_TMS, "boot"), (ASSET_CAR_TMS, "boot")):
            v.upload_chain(parse_image_chain(load_asset(aid), 0), f"asset{aid:03d}", cat)
        # asset_requests.c:56-64: right after CAR.TMS, StoreImage(g_TeamLogoClutRect,
        # g_TeamLogoClut) and StoreImage(g_TeamLogoRect, canvas); then entry 0 = 0.
        team_clut = bytearray(v.store(*TEAM_LOGO_CLUT_RECT))
        team_clut[0:2] = b"\x00\x00"
        team_canvas = v.store(*TEAM_LOGO_RECT)
        # option screen (only when visited).  It is a main-menu item
        # (title_screen.c:305-307 RequestOptionScreenAssets) and returns to the
        # main menu, so it always precedes car select; EnterCarSelectScreen
        # (car_select.c:285-287) re-runs InstallCarModelSlot -> UploadCarImage.
        # (Fixed 2026-10-24: this used to be replayed after the car image, which
        # left OPTION.BIN texels over 7004 of the car image's 16384 words.)
        d9 = load_asset(ASSET_OPTION_BIN)
        v.upload_chain(parse_image_chain(d9, _s32(d9, 0)), "asset009.image", "menu")
        # car select: SELECT.BIN block 2; CAR_xx.1ST image (UploadCarImage)
        v.upload_chain(parse_select_bin_images(load_asset(ASSET_SELECT_BIN)), "asset008.blk2", "menu")
        ci = parse_car_1st_image(load_asset(ASSET_CAR_BASE + 2 * car))
        if car_image is not None:
            # the RAM copy after ApplyBodyColor1/2 (car_assets.c:97-103), which is
            # what UploadCarImage sends; same rect and size as the pack image
            require(len(car_image) == len(ci.pixels), f"car_image is {len(car_image)} bytes, want {len(ci.pixels)}")
            ci = ImageRect(ci.x, ci.y, ci.w, ci.h, bytes(car_image), ci.offset, ci.kind)
        v.upload_rect(ci, f"asset{ASSET_CAR_BASE + 2 * car:03d}.carimage", "car")
        # course select (course_select.c:77-78): LoadImage(g_TeamLogoRect, canvas) and
        # LoadImage(g_TeamLogoClutRect, g_TeamLogoClut).  The logo editor
        # (draw_team_logo_canvas.c:77-93) can change both; user edits are not replayed.
        x, y, w, h = TEAM_LOGO_RECT
        v.upload(x, y, w, h, team_canvas, "course_select.teamlogo_canvas", "menu")
        x, y, w, h = TEAM_LOGO_CLUT_RECT
        v.upload(x, y, w, h, bytes(team_clut), "course_select.teamlogo_clut", "menu")
        # the round screen GP*.TMS
        gp = ASSET_ROUND_SCREEN_BASE + series * 6 + gp_class
        v.upload_chain(parse_image_chain(load_asset(gp), 0), f"asset{gp:03d}", "menu")
    # LoadRaceAssets step 3: CAR_xx.2ND block 4
    car2 = ASSET_CAR_BASE + 2 * car + 1
    v.upload_chain(parse_car_2nd_images(load_asset(car2)), f"asset{car2:03d}.blk4", "car")
    # step 5: the .1ST pack
    aid_1st = aid_2nd - 1
    t1 = Track1st(aid_1st, load_asset(aid_1st))
    v.upload_chain(t1.blocks[0], f"asset{aid_1st:03d}.blk0", "course")
    v.upload_chain(t1.blocks[1], f"asset{aid_1st:03d}.blk1", "course")
    v.upload_tim(t1.blocks[2], f"asset{aid_1st:03d}.blk2", "course")
    v.upload_chain(t1.blocks[3], f"asset{aid_1st:03d}.blk3", "course")
    # StoreTeamLogoImage (image_upload.c:71-83): g_TeamLogoClut, the copy kept at
    # boot (above), with entry 0 forced to 0x8000, loaded at (0x50,0x1E5)
    tl = bytearray(team_clut)
    tl[0:2] = struct.pack("<H", 0x8000)
    x, y, w, h = TEAM_LOGO_CLUT_LOAD_RECT
    v.upload(x, y, w, h, bytes(tl), "StoreTeamLogoImage.clut", "teamlogo")
    if series != 0:
        sx, sy, w, h = TEAM_LOGO_CLUT_MOVE_RECT
        v.move(sx, sy, w, h, *TEAM_LOGO_CLUT_MOVE_DST, "StoreTeamLogoImage.move", "teamlogo")
    sx, sy, sw, sh = TRACK_TEXTURE_RECT
    shadow = v.store(sx, sy, sw, sh)
    shadow_owner = v.store_owner(sx, sy, sw, sh)
    v.upload_chain(t1.blocks[4], f"asset{aid_1st:03d}.blk4", "course_blk4")
    # .2ND: the environment CLUT, uploaded every frame by UpdateEnvironment
    d2 = load_asset(aid_2nd)
    pals = parse_env_palettes(d2)
    script = parse_env_script(d2)
    x, y, w, h = ENV_CLUT_RECT
    if env_mode is None:
        # race_scene.c: SeekEnvironmentScript(g_TrackRenderTable->environmentScriptOffset)
        t0 = parse_render_table(d2)["env_script_start"]
        prev, env_mode, frac, k1 = env_start_state(script, t0)
        require(max(prev, env_mode) < len(pals), f"env modes {prev}/{env_mode} outside {len(pals)} palettes")
        words = env_lerp_clut(pals, prev, env_mode, frac)
        notes.append(f"env CLUT at race start: t0={t0} cue {k1}, lerp mode {prev} -> {env_mode} frac {frac}/4096")
        # environment_script.c:96-98 / 130-132: class >= 5 disables the script after
        # the seek, so the sky CLUT stays at this state for the whole race; otherwise
        # it is re-uploaded every frame while fogEnabled != 0 (146-148).
        env_start = {"t0": t0, "cue": k1, "prev_mode": prev, "mode": env_mode, "frac4096": frac,
                     "clut_words": [f"{c:04X}" for c in words], "script_enabled": cls < 5,
                     "sky_clut": ("static at this seek state (class >= 5)" if cls >= 5 else
                                  "re-uploaded each frame while fogEnabled != 0")}
        v.upload(x, y, w, h, struct.pack("<16H", *words), f"envclut.start{prev}to{env_mode}", "env")
    else:
        require(0 <= env_mode < len(pals), f"env mode {env_mode} outside {len(pals)} palettes")
        v.upload(x, y, w, h, struct.pack("<16H", *env_palette_clut(pals[env_mode])), f"envclut.mode{env_mode}", "env")
        env_start = {"forced_mode": env_mode}
    page0 = v
    page1 = v.copy()
    li = page1._log("TrackTextureShadow", "course", sx, sy, sw, sh)
    for r in range(sh):
        base = (sy + r) * VRAM_W + sx
        page1.pix[base:base + sw] = array("H", shadow[r * sw * 2:(r + 1) * sw * 2])
    page1.load_owner(sx, sy, sw, sh, shadow_owner)
    del li
    # category rename: in page0 the block-4 texels are the live 'page 0' set
    rv = RaceVram(aid_2nd, page0, page1, t1, env_mode, script, notes)
    rv.env_start = env_start
    rv.render_table = parse_render_table(d2)
    return rv


RACE_CATEGORIES = {"course", "course_blk4", "env", "teamlogo"}


def rect_owners(vram, x0, y0, x1, y1):
    """Owner log indices over an inclusive VRAM word rect."""
    s = Counter()
    for y in range(y0, y1 + 1):
        base = y * VRAM_W
        for x in range(x0, x1 + 1):
            s[vram.owner[base + (x & 0x3FF)]] += 1
    return s


def face_vram_rect(face, inclusive=True):
    t = tpage_info(face.tpage)
    u0, u1, v0, v1 = face_texel_rect(face, inclusive)
    shift = {0: 2, 1: 1, 2: 0}.get(t["depth"], 0)
    return t["x"] + (u0 >> shift), t["y"] + v0, t["x"] + (u1 >> shift), t["y"] + v1


def classify(vram, owners):
    cats = Counter()
    for o, n in owners.items():
        cats["unwritten" if o < 0 else vram.log[o][1]] += n
    return cats


def _paired_words(vr, x0, y0, x1, y1, cands):
    """Count VRAM words in the inclusive rect whose value equals the pixel some
    TIM image in `cands` (TIMs carrying the face's CLUT) puts there."""
    ok = 0
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            w = vr.pix[y * VRAM_W + x]
            for _b, im in cands:
                if im.x <= x < im.x + im.w and im.y <= y < im.y + im.h:
                    k = ((y - im.y) * im.w + (x - im.x)) * 2
                    if im.pixels[k] | (im.pixels[k + 1] << 8) == w:
                        ok += 1
                        break
    return ok


def face_coverage(rv, face, inclusive=False):
    """Per race VRAM page: 'race' = every texel word and CLUT entry the face can
    sample was written during the race load; 'exact' = additionally every texel
    word holds the value a .1ST TIM carrying the face's CLUT puts there (so the
    texels are the ones the artist paired with that palette); 'score' = the
    fraction of such words."""
    x0, y0, x1, y1 = face_vram_rect(face, inclusive)
    cx, cy = clut_xy(face.clut)
    cands = rv.tims.get(face.clut, [])
    res = {}
    for pn in (0, 1):
        vr = rv.page(pn)
        towners = rect_owners(vr, x0, y0, x1, y1)
        tex = classify(vr, towners)
        clu = classify(vr, rect_owners(vr, cx, cy, cx + 15, cy))
        race = all(c in RACE_CATEGORIES for c in tex) and all(c in RACE_CATEGORIES for c in clu)
        paired = _paired_words(vr, x0, y0, x1, y1, cands)
        total = sum(towners.values())
        exact = race and paired == total
        res[pn] = {"race": race, "exact": exact, "score": paired / total, "tex": tex, "clut": clu}
    return res


def tim_index(track1st):
    """{clut value: [(block, image rect)]} over every TIM of a .1ST pack."""
    out = defaultdict(list)
    for b in (0, 1, 2, 3, 4):
        blk = track1st.blocks[b]
        tims = [blk] if isinstance(blk, Tim) else blk.tims
        for t in tims:
            if t.clut is None or t.image.empty:
                continue
            out[make_clut(t.clut.x, t.clut.y)].append((b, t.image))
    return out


def match_face_tim(face, index, inclusive=False):
    """Blocks whose TIM images (with the face's CLUT) together cover every VRAM
    word of the face's texel rect; [] when some word is not covered."""
    x0, y0, x1, y1 = face_vram_rect(face, inclusive)
    cands = index.get(face.clut, [])
    used = set()
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            for b, im in cands:
                if im.x <= x < im.x + im.w and im.y <= y < im.y + im.h:
                    used.add(b)
                    break
            else:
                return []
    return sorted(used)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def _face_page(cov):
    """Page that gives the face exact texels (0 preferred); else the page with
    the larger share of CLUT-paired texel words (edge-bleed faces whose rect
    pokes one texel past their TIM), race-written coverage breaking ties; else
    None."""
    for pn in (0, 1):
        if cov[pn]["exact"]:
            return pn, "exact"
    pn = max((0, 1), key=lambda p: (cov[p]["score"], cov[p]["race"], -p))
    if cov[pn]["score"] == 0 and not cov[pn]["race"]:
        return None, None
    return pn, "race"


def compose_pair_texture(rv, faces, covs, k=0):
    """256x256 RGBA for one (tpage, clut): sampled from the page most of its
    faces need, with the texel rects of faces that need the other page copied
    in from that page.  `k` samples through CLUT clut + k instead (the
    runtime ctx+0x84 offset); the page choice per face is unchanged because
    the texel words do not depend on the palette.

    conflict_texels is order independent: the texels claimed both by a face
    that needs page 0 and by a face that needs page 1 whose colours differ
    between the pages (so no single PNG can serve both).  Returns (rgba, stp, info)."""
    tp, cl = faces[0].tpage, (faces[0].clut + k) & 0xFFFF
    need = Counter()
    fp = []
    claims = {0: set(), 1: set()}
    for f, c in zip(faces, covs):
        pn, how = _face_page(c)
        fp.append((pn, how))
        if pn is None:
            continue
        need[pn] += 1
        u0, u1, v0, v1 = face_texel_rect(f, inclusive=False)
        for v in range(v0, v1 + 1):
            claims[pn].update(range(v * 256 + u0, v * 256 + u1 + 1))
    base = 0 if need[0] >= need[1] else 1
    rgba, stp, cols = rv.page(base).sample_page(tp, cl)
    rgba = bytearray(rgba)
    overlaid = 0
    conflicts = 0
    if need[1 - base]:
        o_rgba, o_stp, o_cols = rv.page(1 - base).sample_page(tp, cl)
        for i in claims[1 - base]:
            rgba[i * 4:i * 4 + 4] = o_rgba[i * 4:i * 4 + 4]
            stp[i] = o_stp[i]
        overlaid = need[1 - base]
        conflicts = sum(1 for i in claims[0] & claims[1] if cols[i] != o_cols[i])
    info = {"base_page": base, "faces_page0": need[0], "faces_page1": need[1],
            "faces_exact": sum(1 for p, h in fp if h == "exact"),
            "faces_race_only": sum(1 for p, h in fp if h == "race"),
            "faces_uncovered": sum(1 for p, h in fp if p is None),
            "overlaid_faces": overlaid, "conflict_texels": conflicts,
            "texels_claimed_by_both_pages": len(claims[0] & claims[1])}
    return bytes(rgba), stp, info


def clut_row_status(rv, clut, base_clut=None):
    """The 16 live entries of `clut` on both race pages: race-written?, equal on
    both pages?, which .1ST TIM CLUT blocks hold exactly these words, and (with
    base_clut) how many entries differ from the base palette."""
    cx, cy = clut_xy(clut)
    require(cx + 16 <= VRAM_W, f"clut {clut:04X} row runs past VRAM x 1024")
    race = True
    ent = {}
    for pn in (0, 1):
        vr = rv.page(pn)
        cats = classify(vr, rect_owners(vr, cx, cy, cx + 15, cy))
        race = race and all(c in RACE_CATEGORIES for c in cats)
        ent[pn] = vr.clut_entries(clut)
    out = {"clut": f"{clut:04X}", "clut_xy": (cx, cy), "race_written": race, "pages_equal": ent[0] == ent[1],
           "tim_sources": clut_tim_sources(rv.track1st, clut, ent[0])}
    if base_clut is not None:
        out["entries_differing_from_base"] = sum(1 for a, b in zip(ent[0], rv.page0.clut_entries(base_clut)) if a != b)
    return out


def _texwin_tiles(manifest, name, fl, rgba, out, write):
    for f in fl:
        if not f.texwin:
            continue
        key = f"{name}_tw_{f.texwin & 0xFFFFF:05X}"
        if key in manifest["texwin_tiles"]:
            manifest["texwin_tiles"][key]["faces"] += 1
            continue
        tw = texwin_info(f.texwin)
        pu, pv = tw["u"]["period"], tw["v"]["period"]
        bu, bv = tw["u"]["base"], tw["v"]["base"]
        manifest["texwin_tiles"][key] = {"faces": 1, "texwin": f"{f.texwin:08X}",
                                         "u_base": bu, "u_period": pu, "v_base": bv, "v_period": pv,
                                         "regular": tw["u"]["regular"] and tw["v"]["regular"]}
        if write:
            write_png(out / f"{key}.png", pu, pv, crop_rgba(rgba, 256, bu, bv, pu, pv))


def variant_material(tpage, clut, k):
    """Material / PNG name of a runtime CLUT variant (faces drawn with ctx+0x84 = k<<16)."""
    return f"tpage_{tpage:04X}_clut_{clut:04X}_k{k}"


def export_pack_textures(rv, models, out_root, write=True, covs=None, draws=None):
    """One 256x256 PNG per (tpage, clut) used by the course faces, one per
    runtime CLUT variant (tpage, clut, k>0) a race-time draw site produces,
    one tile PNG per (tpage, clut[, k], texwin), the sky per env mode,
    whole-VRAM views and a manifest.json."""
    out = Path(out_root) / f"asset_{rv.aid_2nd:03d}"
    faces = [f for m in models for f in m.faces if f.textured]
    if covs is None:
        covs = {id(f): face_coverage(rv, f) for f in faces}
    if draws is None:
        draws = course_model_draws(rv.aid_2nd, model_count=len(models))
    drawn = set(drawn_models(draws))
    by_pair = defaultdict(list)
    for f in faces:
        by_pair[(f.tpage, f.clut)].append(f)
    manifest = {"asset": rv.aid_2nd, "pack": pack_name(rv.aid_2nd), "track_1st": rv.aid_2nd - 1,
                "course_class_by_formula": pack_course_class(rv.aid_2nd),
                "env_mode": rv.env_mode, "env_modes_in_script": sorted({c["mode"] for c in rv.env_script["cues"]}),
                "env_start": rv.env_start, "render_table": rv.render_table,
                "uv_note": "vt = (u/256, 1 - v/256) on the page PNG; texwin faces: tile PNG with REPEAT, vt = (u/u_period, 1 - v/v_period)",
                "drawn_models": sorted(drawn),
                "undrawn_models": [m.index for m in models if m.index not in drawn],
                "pairs": {}, "clut_variants": {}, "texwin_tiles": {}}
    for (tp, cl), fl in sorted(by_pair.items()):
        name = f"tpage_{tp:04X}_clut_{cl:04X}"
        rgba, stp, info = compose_pair_texture(rv, fl, [covs[id(f)] for f in fl])
        entry = dict(info)
        entry.update({"faces": len(fl), "tpage": tpage_info(tp), "clut_xy": clut_xy(cl),
                      "models": sorted({f.model for f in fl}),
                      "faces_on_undrawn_models": sum(1 for f in fl if f.model not in drawn),
                      "opcodes": dict(Counter(f.opcode for f in fl)),
                      "semi_transparent_faces": sum(1 for f in fl if f.semi_transparent),
                      "transparent_texels": sum(1 for i in range(3, len(rgba), 4) if rgba[i] == 0),
                      "stp_texels": sum(stp),
                      "clut_row": clut_row_status(rv, cl)})
        if write:
            write_png(out / f"{name}.png", 256, 256, rgba)
        manifest["pairs"][name] = entry
        _texwin_tiles(manifest, name, fl, rgba, out, write)
    for (tp, cl, k), e in sorted(runtime_clut_variants(models, draws).items()):
        name = variant_material(tp, cl, k)
        fl = e["faces"]
        rgba, stp, info = compose_pair_texture(rv, fl, [covs[id(f)] for f in fl], k=k)
        entry = dict(info)
        entry.update({"base_material": f"tpage_{tp:04X}_clut_{cl:04X}", "k": k,
                      "runtime_clut": f"{(cl + k) & 0xFFFF:04X}", "faces": len(fl),
                      "models": sorted(e["models"]), "sites": sorted(e["sites"]),
                      "conditions": sorted(e["conditions"]),
                      "same_as_static_pair": f"tpage_{tp:04X}_clut_{(cl + k) & 0xFFFF:04X}" in manifest["pairs"],
                      "clut_row": clut_row_status(rv, (cl + k) & 0xFFFF, cl)})
        if write:
            write_png(out / f"{name}.png", 256, 256, rgba)
        manifest["clut_variants"][name] = entry
        _texwin_tiles(manifest, name, fl, rgba, out, write)
    if write:
        # sky: block 2 bare TIM through every env palette
        sky = rv.track1st.blocks[2]
        tp = make_tpage(sky.pmode, 0, sky.image.x, sky.image.y)
        cl = make_clut(sky.clut.x, sky.clut.y)
        pals = parse_env_palettes(load_asset(rv.aid_2nd))
        for m, pal in enumerate(pals):
            vv = rv.page0.copy()
            vv.upload(sky.clut.x, sky.clut.y, 16, 1, struct.pack("<16H", *env_palette_clut(pal)), "sky", "env")
            rgba, _, _ = vv.sample_page(tp, cl)
            h = sky.image.h
            write_png(out / f"sky_tpage_{tp:04X}_clut_{cl:04X}_envmode{m}.png", 256, h, crop_rgba(rgba, 256, 0, 0, 256, h))
        # sky through the race-start env CLUT already in page 0
        rgba, _, _ = rv.page0.sample_page(tp, cl)
        write_png(out / f"sky_tpage_{tp:04X}_clut_{cl:04X}_racestart.png", 256, sky.image.h,
                  crop_rgba(rgba, 256, 0, 0, 256, sky.image.h))
        write_png(out / "vram_page0.png", VRAM_W, VRAM_H, rv.page0.to_rgba_direct())
        write_png(out / "vram_page1.png", VRAM_W, VRAM_H, rv.page1.to_rgba_direct())
        write_png(out / "vram_owner_page0.png", VRAM_W, VRAM_H, rv.page0.owner_category_rgba())
        t1 = rv.track1st
        write_png(out / "vram_timview_page0.png", 4096, 512,
                  rv.page0.tim_view_rgba([t1.blocks[i] for i in (0, 1, 2, 4)]))
        write_png(out / "vram_timview_page1.png", 4096, 512,
                  rv.page1.tim_view_rgba([t1.blocks[i] for i in (0, 1, 2, 3)]))
        (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


# --------------------------------------------------------------------------
# Analysis / reporting
# --------------------------------------------------------------------------

def analyse_2nd(aid, write_png_files=True, out_root=OUT_DIR, series=0, env_mode=None, verbose=True):
    d = load_asset(aid)
    models = parse_course_models(d)
    op3 = check_op3_scroll(models)
    draws = course_model_draws(aid, d, model_count=len(models))
    drawn = set(drawn_models(draws))
    rv = build_race_vram(aid, series=series, env_mode=env_mode)
    index = tim_index(rv.track1st)
    faces = [f for m in models for f in m.faces if f.textured]
    split = Counter()
    blocks = Counter()
    unmatched, uncovered, inexact = [], [], []
    covs = {}
    for f in faces:
        ti = tpage_info(f.tpage)
        require(ti["depth"] == 0 and ti["extra"] == 0, f"face {f.model}/{f.index}: tpage {f.tpage:04X} not 4-bit std")
        cov = covs[id(f)] = face_coverage(rv, f)
        split[("exact", cov[0]["exact"], cov[1]["exact"])] += 1
        split[("race", cov[0]["race"], cov[1]["race"])] += 1
        if not (cov[0]["race"] or cov[1]["race"]):
            uncovered.append((f, cov))
        elif not (cov[0]["exact"] or cov[1]["exact"]):
            inexact.append((f, cov))
        hits = match_face_tim(f, index)
        if hits:
            blocks[tuple(hits)] += 1
        else:
            unmatched.append(f)
    man = export_pack_textures(rv, models, out_root, write=write_png_files, covs=covs, draws=draws)
    # Assumptions the export relies on, asserted per pack:
    #  - one PNG per (tpage, clut[, k]) is enough: 0 texels needed from both pages with different colours;
    #  - every CLUT row a drawn face samples (static clut or clut + k) is race-written, identical on
    #    both pages, and equal to the CLUT data of some .1ST TIM.
    for name, e in list(man["pairs"].items()) + list(man["clut_variants"].items()):
        require(e["conflict_texels"] == 0, f"asset {aid} {name}: {e['conflict_texels']} page-conflicting texels")
        if name in man["pairs"] and e["faces_on_undrawn_models"] == e["faces"]:
            continue
        cr = e["clut_row"]
        require(cr["race_written"] and cr["pages_equal"] and cr["tim_sources"],
                f"asset {aid} {name}: CLUT row {cr['clut']} not a race-written .1ST TIM CLUT on both pages")
    variants = man["clut_variants"]
    res = {"aid": aid, "faces": len(faces), "pairs": len(man["pairs"]), "tiles": len(man["texwin_tiles"]),
           "split": split, "blocks": blocks, "tim_matched": sum(blocks.values()),
           "uncovered": uncovered, "inexact": inexact, "unmatched": unmatched,
           "inexact_drawn": [x for x in inexact if x[0].model in drawn],
           "inexact_undrawn": [x for x in inexact if x[0].model not in drawn],
           "undrawn_faces": sum(1 for f in faces if f.model not in drawn),
           "drawn_models": sorted(drawn), "op3": op3,
           "variants": variants,
           "variant_faces": sum(e["faces"] for e in variants.values()),
           "variant_clut_only": sum(1 for e in variants.values() if any(s[2] for s in e["clut_row"]["tim_sources"])),
           "env_mode": rv.env_mode, "env_modes": man["env_modes_in_script"],
           "env_mode4": env_mode4_states(aid, d),
           "conflicts": sum(e["conflict_texels"] for e in man["pairs"].values()),
           "overlaid": sum(e["overlaid_faces"] for e in man["pairs"].values()),
           "manifest": man}
    if verbose:
        print_2nd(res)
    return res, rv, models


def print_2nd(res):
    aid = res["aid"]
    s = res["split"]
    print(f"asset_{aid:03d} {pack_name(aid)}: textured course faces {res['faces']}, (tpage,clut) pairs {res['pairs']}, "
          f"texwin tiles {res['tiles']}, env mode {res['env_mode']} (script modes {res['env_modes']})")
    print(f"  race-written texels+CLUT: any page {res['faces'] - len(res['uncovered'])}/{res['faces']} "
          f"(both {s[('race', True, True)]}, page0 only {s[('race', True, False)]}, page1 only {s[('race', False, True)]})")
    print(f"  exact (texels from a TIM paired with the face CLUT): page0 only {s[('exact', True, False)]}, "
          f"page1 only {s[('exact', False, True)]}, both {s[('exact', True, True)]}, neither {s[('exact', False, False)]}")
    bl = {"+".join(map(str, k)): v for k, v in sorted(res["blocks"].items())}
    print(f"  static TIM match (rect inside .1ST TIM images with the same CLUT): {res['tim_matched']}/{res['faces']} by block {bl}")
    print(f"  PNG composition: faces overlaid from the non-base page {res['overlaid']}, "
          f"conflicting texels (order independent) {res['conflicts']}")
    m4 = res["env_mode4"]
    print(f"  drawn course models {len(res['drawn_models'])}/{len(res['manifest']['drawn_models']) + len(res['manifest']['undrawn_models'])}"
          f" (undrawn {res['manifest']['undrawn_models']}, {res['undrawn_faces']} textured faces); "
          f"op3 models {res['op3']}; env mode4 at start {m4['start']}, reachable {m4['states']}, script on {m4['script_enabled']}")
    print(f"  runtime CLUT variants (clut + k): {len(res['variants'])} materials, {res['variant_faces']} faces, "
          f"{res['variant_clut_only']} backed by a CLUT-only .1ST TIM")
    for name, e in sorted(res["variants"].items()):
        cr = e["clut_row"]
        print(f"   {name}: clut {cr['clut']} models {e['models']} faces {e['faces']} "
              f"differs {cr['entries_differing_from_base']}/16 sources {cr['tim_sources'][:2]} via {sorted({s.split(' ')[0] for s in e['sites']})}")
    for f, cov in res["uncovered"][:3]:
        print(f"   uncovered: model {f.model} face {f.index} op{f.opcode} {f.material} uv {f.uv} "
              f"p0 tex {dict(cov[0]['tex'])} clut {dict(cov[0]['clut'])}")
    print(f"  inexact faces: drawn models {len(res['inexact_drawn'])}, undrawn models {len(res['inexact_undrawn'])}")
    for f, cov in res["inexact_drawn"][:3] + res["inexact_undrawn"][:2]:
        print(f"   inexact: model {f.model} face {f.index} op{f.opcode} {f.material} uv {f.uv} win {f.texwin:08X}")
    for f in res["unmatched"][:3]:
        print(f"   no TIM match: model {f.model} face {f.index} op{f.opcode} {f.material} uv {f.uv} win {f.texwin:08X}")


def summarize_chain(tag, ch):
    tims = ch.tims if isinstance(ch, ImageChain) else [ch]
    img = [t.image for t in tims if not t.image.empty]
    cl = [t.clut for t in tims if t.clut is not None]

    def bb(rs):
        if not rs:
            return "-"
        return f"x{min(r.x for r in rs)}..{max(r.x + r.w for r in rs)} y{min(r.y for r in rs)}..{max(r.y + r.h for r in rs)}"
    pm = dict(Counter(t.pmode for t in tims))
    w0 = f" word0={ch.word0:#x}" if isinstance(ch, ImageChain) else ""
    return f"{tag}: {len(tims)} TIMs pmode{pm}{w0} images {len(img)} {bb(img)}; cluts {len(cl)} {bb(cl)}"


def print_1st(aid):
    t = Track1st(aid, load_asset(aid))
    print(f"asset_{aid:03d} {pack_name(aid)} size {t.size:#x} offsets {[hex(o) for o in t.offsets]}")
    for i in range(5):
        print("  " + summarize_chain(f"block {i}", t.blocks[i]))
    return t


def overlap_report(t1):
    """CLUT words written by both block 3 (page 1 textures) and block 4."""
    def words(chs):
        s = {}
        for r in chs.uploads():
            if r.kind == "clut":
                for i in range(r.w):
                    s[(r.x + i, r.y)] = r.pixels[2 * i:2 * i + 2]
        return s
    w3, w4 = words(t1.blocks[3]), words(t1.blocks[4])
    common = set(w3) & set(w4)
    diff = sum(1 for k in common if w3[k] != w4[k])
    return len(common), diff


def run_all(write_png_files, out_root):
    print("== fixed / menu / car image assets ==")
    for aid in (ASSET_LOGO_TMS, ASSET_TITLE_TMS, ASSET_CAR_TMS, ASSET_SAVE_TMS) + tuple(range(74, 86)):
        d = load_asset(aid)
        ch = parse_image_chain(d, 0)
        require(ch.end == len(d), f"asset {aid}: chain ends {ch.end:#x}, file {len(d):#x}")
        if aid < 74 or aid == 74:
            print("  " + summarize_chain(f"asset_{aid:03d}", ch))
    print("  asset_075..085 GP*.TMS: same chain format, ends at file end (asserted)")
    ch = parse_select_bin_images(load_asset(ASSET_SELECT_BIN))
    print("  " + summarize_chain("asset_008 SELECT.BIN block 2", ch))
    d9 = load_asset(ASSET_OPTION_BIN)
    ch = parse_image_chain(d9, _s32(d9, 0))
    require(ch.end == len(d9), "OPTION.BIN image chain does not end at file end")
    print("  " + summarize_chain("asset_009 OPTION.BIN imageOffset", ch))
    nc1 = nc2 = 0
    for car in range(32):
        d1 = load_asset(ASSET_CAR_BASE + 2 * car)
        im = parse_car_1st_image(d1)
        require(im.offset + len(im.pixels) == len(d1), f"car {car}: image is not the tail of CAR .1ST")
        parse_car_2nd_images(load_asset(ASSET_CAR_BASE + 2 * car + 1))
        nc1 += 1
        nc2 += 1
    print(f"  CAR_xx.1ST image (last 0x8000 bytes -> g_CarImageRect) x{nc1}; CAR_xx.2ND block 4 chains x{nc2}: all asserted")
    print("== track .1ST packs ==")
    agg = Counter()
    ov = Counter()
    for aid in range(87, 135, 2):
        t = Track1st(aid, load_asset(aid))
        for i in range(5):
            blk = t.blocks[i]
            tims = [blk] if isinstance(blk, Tim) else blk.tims
            agg[f"blk{i}_tims"] += len(tims)
            for tm in tims:
                agg[f"pmode{tm.pmode}"] += 1
                if not tm.image.empty:
                    r = tm.image
                    sx, sy, sw, sh = TRACK_TEXTURE_RECT
                    inside = sx <= r.x and r.x + r.w <= sx + sw and sy <= r.y and r.y + r.h <= sy + sh
                    if i in (3, 4):
                        agg[f"blk{i}_in_swap_rect" if inside else f"blk{i}_outside_swap_rect"] += 1
            if isinstance(blk, ImageChain):
                agg[f"word0_{blk.word0:#x}"] += 1
        c, dif = overlap_report(t)
        ov["shared_clut_words"] += c
        ov["differing"] += dif
    print(f"  24 packs parsed, every chain ends at the next block / file end: {dict(sorted(agg.items()))}")
    print(f"  CLUT words written by both block 3 and block 4: {ov['shared_clut_words']} (differing {ov['differing']})")
    print("== track .2ND packs: course faces vs race VRAM ==")
    tot = Counter()
    for aid in range(88, 135, 2):
        res, rv, models = analyse_2nd(aid, write_png_files, out_root, verbose=False)
        s = res["split"]
        covered = res["faces"] - len(res["uncovered"])
        exact = res["faces"] - s[("exact", False, False)]
        print(f"  {aid:03d} {pack_name(aid):30s} faces {res['faces']:4d} pairs {res['pairs']:2d} tiles {res['tiles']:2d} "
              f"race {covered:4d} exact {exact:4d} (p0 {s[('exact', True, False)]:4d} p1 {s[('exact', False, True)]:3d} "
              f"both {s[('exact', True, True)]:4d}) TIM {res['tim_matched']:4d} ovl {res['overlaid']:3d} cfl {res['conflicts']} "
              f"env {res['env_mode']} m4 {''.join('1' if x else '0' for x in res['env_mode4']['states'])} "
              f"var {len(res['variants']):2d} ({res['variant_faces']:2d}f) inexact d{len(res['inexact_drawn'])}/u{len(res['inexact_undrawn'])} "
              f"undrawn-f {res['undrawn_faces']:3d}")
        for f, cov in res["uncovered"][:2] + res["inexact"][:2]:
            print(f"      not exact: model {f.model}{'' if f.model in res['drawn_models'] else ' (undrawn)'} face {f.index} "
                  f"op{f.opcode} {f.material} uv {f.uv} win {f.texwin:08X} "
                  f"p0 tex {dict(cov[0]['tex'])} clut {dict(cov[0]['clut'])}")
        vk = Counter()
        for e in res["variants"].values():
            vk[(tuple(e["models"]), e["k"])] += 1
        print(f"      variants (models, k) -> materials: {dict(sorted(vk.items()))}")
        tot["faces"] += res["faces"]
        tot["covered"] += covered
        tot["exact"] += exact
        tot["tim"] += res["tim_matched"]
        tot["pairs"] += res["pairs"]
        tot["conflicts"] += res["conflicts"]
        tot["page1_only"] += s[("exact", False, True)]
        tot["variants"] += len(res["variants"])
        tot["variant_faces"] += res["variant_faces"]
        tot["variant_clut_only"] += res["variant_clut_only"]
        tot["inexact_drawn"] += len(res["inexact_drawn"])
        tot["inexact_undrawn"] += len(res["inexact_undrawn"])
        tot["undrawn_faces"] += res["undrawn_faces"]
        tot["op3_faces"] += sum(res["op3"].values())
    print(f"  TOTAL faces {tot['faces']} race-covered {tot['covered']} exact {tot['exact']} (page1-only {tot['page1_only']}) "
          f"TIM-matched {tot['tim']} pairs {tot['pairs']} conflicting texels {tot['conflicts']}")
    print(f"  TOTAL runtime CLUT variants {tot['variants']} materials / {tot['variant_faces']} faces "
          f"({tot['variant_clut_only']} backed by CLUT-only TIMs); inexact faces on drawn models {tot['inexact_drawn']}, "
          f"on undrawn models {tot['inexact_undrawn']}; textured faces of undrawn models {tot['undrawn_faces']}; "
          f"op3 faces (scroll asserted carry-free, not accumulated) {tot['op3_faces']}")
    require(tot["covered"] == tot["faces"], "some course faces sample VRAM not written by the race load")
    return tot


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("pack", nargs="?", default="88")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--series", type=int, default=0)
    ap.add_argument("--env-mode", type=int, default=None)
    a = ap.parse_args(argv)
    if a.all:
        run_all(not a.no_png, a.out)
        return 0
    s = a.pack
    if not s.isdigit():
        s = Path(s).stem.split("_")[-1]
    aid = int(s)
    if 87 <= aid <= 134 and aid % 2 == 1:
        print_1st(aid)
        return 0
    require(88 <= aid <= 134, f"asset {aid} is not a track pack")
    t1 = print_1st(aid - 1)
    c, dif = overlap_report(t1)
    print(f"  CLUT words shared by blocks 3 and 4: {c} (differing {dif})")
    analyse_2nd(aid, not a.no_png, a.out, series=a.series, env_mode=a.env_mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
