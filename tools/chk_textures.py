"""Checker helper: compare exported page PNG texels with VRAM built independently from the
.1ST pack TIMs (set A = blocks 0-3, set B = set A + block 4 over it), per textured face.

python -I tools/chk_textures.py OUT_ROOT [aid ...]
"""
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chk_course_scenes as c  # noqa: E402
import rr_assets as ra  # noqa: E402


def build_vram(aid1):
    d = ra.load_asset(aid1)
    inv = ra.asset_inventory(aid1, d)
    blocks = {b.get("block"): b for b in inv["blocks"] if "block" in b}
    va = [None] * (1024 * 512)
    owner = [None] * (1024 * 512)

    def upload(tims, vr, tag):
        for t in tims:
            parts = []
            if t["clut"]:
                x, y, w, h = t["clut"]
                # clut data follows the 12-byte clut block header at offset+8
                parts.append((x, y, w, h, t["offset"] + 8 + 12))
            x, y, w, h = t["rect"]
            parts.append((x, y, w, h, t["pixel_offset"]))
            for x, y, w, h, off in parts:
                for r in range(h):
                    for k in range(w):
                        hw = d[off + 2 * (r * w + k)] | (d[off + 2 * (r * w + k) + 1] << 8)
                        p = ((y + r) & 511) * 1024 + ((x + k) & 1023)
                        vr[p] = hw
                        owner[p] = tag

    for k in (0, 1, 2, 3):
        upload(blocks[k]["tims"], va, "A%d" % k)
    vb = list(va)
    upload(blocks[4]["tims"], vb, "B4")
    return va, vb, inv


def texel(vr, tp, cl, u, v):
    tx = (tp & 0xF) * 64
    ty = ((tp >> 4) & 1) * 256
    hw = vr[(ty + v) * 1024 + tx + (u >> 2)]
    if hw is None:
        return None
    idx = (hw >> ((u & 3) * 4)) & 0xF
    cx = (cl & 0x3F) * 16
    cy = (cl >> 6) & 0x1FF
    col = vr[cy * 1024 + cx + idx]
    return col


def png_rgba(path):
    w, h, alpha, err = c.png_load(path)
    # need RGB too: reload raw (filter 0 assumed by png_load fast path)
    import struct
    import zlib
    b = Path(path).read_bytes()
    p = 8
    idat = bytearray()
    while p < len(b):
        n = struct.unpack_from(">I", b, p)[0]
        if b[p + 4:p + 8] == b"IDAT":
            idat += b[p + 8:p + 8 + n]
        p += 12 + n
    raw = zlib.decompress(bytes(idat))
    px = bytearray()
    for y in range(h):
        row = raw[y * (1 + 4 * w):(y + 1) * (1 + 4 * w)]
        if row[0] != 0:
            return None
        px += row[1:]
    return w, h, px


def match(px, w, col, x, y):
    i = 4 * (y * w + x)
    r, g, b, a = px[i:i + 4]
    if col is None:
        return None
    if col == 0:
        return a == 0
    return a == 255 and (r >> 3) == (col & 31) and (g >> 3) == ((col >> 5) & 31) and (b >> 3) == ((col >> 10) & 31)


def check(root, aid, tables):
    rel = aid - 0x58
    course, cls = (rel >> 1) & 3, rel >> 3
    fname = tables["asset_paths"][aid].split(chr(92))[-1]
    out = Path(root) / tables["course_names"][course].replace(" ", "_") / (
        "CLASS%d_%s" % (cls + 1, tables["gp_names"][cls]))
    stem = fname.replace(".", "_")
    va, vb, _inv = build_vram(aid - 1)
    mats = c.parse_mtl(out / (stem + ".mtl"))
    pngs = {}
    res = Counter()
    bad_mats = Counter()
    ob = c.parse_obj(out / (stem + ".obj"))
    for o in ob["objs"]:
        for mname, cs in o["faces"]:
            m = mats.get(mname, {})
            if "map_Kd" not in m:
                continue
            if "_tw_" in mname:
                res["tile_skipped"] += 1
                continue
            mm = re.match(r"^tpage_([0-9A-F]{4})_clut_([0-9A-F]{4})", mname)
            tp, cl = int(mm.group(1), 16), int(mm.group(2), 16)
            if mname not in pngs:
                pngs[mname] = png_rgba(out / m["map_Kd"])
            w, h, px = pngs[mname]
            us = [round(ob["VT"][t][0] * 256) for _v, t in cs]
            vs = [round((1 - ob["VT"][t][1]) * 256) for _v, t in cs]
            u0, u1 = min(us), max(us)
            v0, v1 = min(vs), max(vs)
            u1 = max(u0, u1 - 1)
            v1 = max(v0, v1 - 1)
            okA = okB = True
            unk = 0
            n = 0
            for v in range(v0, min(v1, 255) + 1):
                for u in range(u0, min(u1, 255) + 1):
                    n += 1
                    ca = texel(va, tp, cl, u, v)
                    cb = texel(vb, tp, cl, u, v)
                    ma = match(px, w, ca, u, v)
                    mb = match(px, w, cb, u, v)
                    if ma is None and mb is None:
                        unk += 1
                    if ma is False or ma is None:
                        okA = False
                    if mb is False or mb is None:
                        okB = False
            if unk == n:
                res["face_not_in_1ST_vram"] += 1
                bad_mats[("unwritten", mname, o["name"][:14])] += 1
            elif okA and okB:
                res["face_matches_A_and_B"] += 1
            elif okA:
                res["face_matches_A"] += 1
            elif okB:
                res["face_matches_B"] += 1
            else:
                res["face_mismatch"] += 1
                bad_mats[("mismatch", mname, o["name"][:14])] += 1
    print(aid, dict(res))
    for k, v in bad_mats.most_common(8):
        print("    ", v, k)


if __name__ == "__main__":
    root = Path(sys.argv[1])
    ids = [int(a) for a in sys.argv[2:]] or list(range(88, 135, 2))
    t = ra.exe_tables()
    for aid in ids:
        check(root, aid, t)
        sys.stdout.flush()
