"""Checker helper: for faces that match neither VRAM set A nor B, classify their texels
(inside the UV polygon only) to tell exporter errors from bbox / mixed-set artefacts.

python -I tools/chk_tex_diag.py OUT_ROOT aid
"""
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chk_course_scenes as c  # noqa: E402
import chk_textures as ct  # noqa: E402
import rr_assets as ra  # noqa: E402


def inside(poly, x, y):
    # polygon given in drawing order; test both triangles of fan
    def tri(a, b, cc):
        d = (b[1] - cc[1]) * (a[0] - cc[0]) + (cc[0] - b[0]) * (a[1] - cc[1])
        if d == 0:
            return False
        l1 = ((b[1] - cc[1]) * (x - cc[0]) + (cc[0] - b[0]) * (y - cc[1])) / d
        l2 = ((cc[1] - a[1]) * (x - cc[0]) + (a[0] - cc[0]) * (y - cc[1])) / d
        return min(l1, l2, 1 - l1 - l2) >= -1e-6
    for k in range(1, len(poly) - 1):
        if tri(poly[0], poly[k], poly[k + 1]):
            return True
    return False


def main(root, aid):
    t = ra.exe_tables()
    rel = aid - 0x58
    course, cls = (rel >> 1) & 3, rel >> 3
    fname = t["asset_paths"][aid].split(chr(92))[-1]
    out = Path(root) / t["course_names"][course].replace(" ", "_") / (
        "CLASS%d_%s" % (cls + 1, t["gp_names"][cls]))
    stem = fname.replace(".", "_")
    va, vb, _inv = ct.build_vram(aid - 1)
    mats = c.parse_mtl(out / (stem + ".mtl"))
    ob = c.parse_obj(out / (stem + ".obj"))
    pngs = {}
    tot = Counter()
    shown = 0
    for o in ob["objs"]:
        for mname, cs in o["faces"]:
            m = mats.get(mname, {})
            if "map_Kd" not in m or "_tw_" in mname:
                continue
            mm = re.match(r"^tpage_([0-9A-F]{4})_clut_([0-9A-F]{4})", mname)
            tp, cl = int(mm.group(1), 16), int(mm.group(2), 16)
            if mname not in pngs:
                pngs[mname] = ct.png_rgba(out / m["map_Kd"])
            w, h, px = pngs[mname]
            uv = [(ob["VT"][tt][0] * 256, (1 - ob["VT"][tt][1]) * 256) for _v, tt in cs]
            us = [round(a) for a, _b in uv]
            vs = [round(b) for _a, b in uv]
            cnt = Counter()
            for v in range(min(vs), min(max(vs), 255) + 1):
                for u in range(min(us), min(max(us), 255) + 1):
                    if not inside(uv, u + 0.5, v + 0.5) and not inside(uv, u, v):
                        cnt["bbox_only"] += 0
                        continue
                    ca = ct.texel(va, tp, cl, u, v)
                    cb = ct.texel(vb, tp, cl, u, v)
                    ma = ct.match(px, w, ca, u, v)
                    mb = ct.match(px, w, cb, u, v)
                    cnt[("A" if ma else ("a?" if ma is None else "a-")) + ("B" if mb else ("b?" if mb is None else "b-"))] += 1
            okA = all(k[0] == "A" for k in cnt if isinstance(k, str) and k != "bbox_only" and cnt[k])
            okB = all(k.endswith("B") for k in cnt if isinstance(k, str) and k != "bbox_only" and cnt[k])
            if okA or okB:
                tot["ok_inside_poly"] += 1
                continue
            tot["bad_inside_poly"] += 1
            if shown < 12:
                shown += 1
                print(o["name"], mname, "uv", [(round(a), round(b)) for a, b in uv], dict(cnt))
    print(aid, dict(tot))


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]))
