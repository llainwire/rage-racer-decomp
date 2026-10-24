"""Checker helper: normals of the written terrain faces directly under each track point
(the road the car drives on must face up) and of faces above it (overpasses)."""
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chk_course_scenes as c  # noqa: E402


def main(objpath, aid):
    ob = c.parse_obj(objpath)
    V = ob["V"]
    buckets = defaultdict(list)
    for o in ob["objs"]:
        if not o["name"].startswith("terrain_cell_"):
            continue
        for _m, cs in o["faces"]:
            ps = [V[vi] for vi, _t in cs]
            # Newell normal
            nx = ny = nz = 0.0
            for i in range(len(ps)):
                a, b = ps[i], ps[(i + 1) % len(ps)]
                nx += (a[1] - b[1]) * (a[2] + b[2])
                ny += (a[2] - b[2]) * (a[0] + b[0])
                nz += (a[0] - b[0]) * (a[1] + b[1])
            for k in range(1, len(ps) - 1):
                t = (ps[0], ps[k], ps[k + 1], nz)
                xs = [p[0] for p in t[:3]]
                ys = [p[1] for p in t[:3]]
                for bx in range(int(min(xs)) // 1024, int(max(xs)) // 1024 + 1):
                    for by in range(int(min(ys)) // 1024, int(max(ys)) // 1024 + 1):
                        buckets[(bx, by)].append(t)
    res = Counter()
    for X, Y, Z in c.track_points(aid):
        best = None
        above = []
        for a, b, cc, nz in buckets.get((int(X) // 1024, int(Y) // 1024), ()):
            d = (b[1] - cc[1]) * (a[0] - cc[0]) + (cc[0] - b[0]) * (a[1] - cc[1])
            if d == 0:
                continue
            l1 = ((b[1] - cc[1]) * (X - cc[0]) + (cc[0] - b[0]) * (Y - cc[1])) / d
            l2 = ((cc[1] - a[1]) * (X - cc[0]) + (a[0] - cc[0]) * (Y - cc[1])) / d
            l3 = 1 - l1 - l2
            if min(l1, l2, l3) < -1e-9:
                continue
            h = l1 * a[2] + l2 * b[2] + l3 * cc[2]
            if best is None or abs(h - Z) < abs(best[0] - Z):
                best = (h, nz)
            if h > Z + 400:
                above.append(nz)
        if best is None:
            res["no_face"] += 1
            continue
        if abs(best[0] - Z) > 64:
            res["road_gt64"] += 1
        res["road_up" if best[1] > 0 else "road_down"] += 1
        for nz in above:
            res["above_up" if nz > 0 else "above_down"] += 1
    print(aid, dict(res))


if __name__ == "__main__":
    root = Path(sys.argv[1])
    import rr_assets as ra
    t = ra.exe_tables()
    for aid in range(88, 135, 2):
        rel = aid - 0x58
        course, cls = (rel >> 1) & 3, rel >> 3
        fname = t["asset_paths"][aid].split(chr(92))[-1]
        p = root / t["course_names"][course].replace(" ", "_") / ("CLASS%d_%s" % (cls + 1, t["gp_names"][cls])) / (
            fname.replace(".", "_") + ".obj")
        main(p, aid)
