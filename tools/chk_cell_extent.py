"""Checker helper: terrain cell local extents and cross-cell overlaps (rr_assets data, read-only)."""
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rr_assets as ra  # noqa: E402


def main(aid):
    t = ra.track_models(aid)["terrain"]
    pool = t["vertex_list"]
    over = []
    tris = []
    for c in t["cell_list"]:
        ox, oy, oz = c["origin"]
        xs, zs = [], []
        for f in c["faces"]:
            for i in f["idx"]:
                xs.append(pool[i][0])
                zs.append(pool[i][2])
            ps = [(pool[i][0] + ox, pool[i][2] + oz, pool[i][1] + oy) for i in f["idx"]]
            for tri in ((0, 1, 2), (1, 3, 2)):
                q = [ps[k] for k in tri]
                if len(set(q)) == 3:
                    tris.append((q, c["id"]))
        ov = max(max(abs(min(xs)), abs(max(xs))), max(abs(min(zs)), abs(max(zs)))) - 4096
        over.append(ov)
    over.sort()
    n = len(over)
    print("asset %03d cells %d; local half-extent minus 4096: min %d p10 %d median %d p90 %d max %d"
          % (aid, n, over[0], over[n // 10], over[n // 2], over[9 * n // 10], over[-1]))
    print("  cells with |local| > 4096+64:", sum(1 for o in over if o > 64), " > 4096+1024:",
          sum(1 for o in over if o > 1024))
    # overlap height differences between different cells, sampled on a 128 grid
    buckets = defaultdict(list)
    for q, cid in tris:
        xs = [p[0] for p in q]
        zs = [p[1] for p in q]
        for bx in range(int(min(xs)) // 1024, int(max(xs)) // 1024 + 1):
            for bz in range(int(min(zs)) // 1024, int(max(zs)) // 1024 + 1):
                buckets[(bx, bz)].append((q, cid))
    dh = Counter()
    for (bx, bz), lst in buckets.items():
        if len({cid for _q, cid in lst}) < 2:
            continue
        for i in range(8):
            for j in range(8):
                X = bx * 1024 + 64 + 128 * i
                Z = bz * 1024 + 64 + 128 * j
                hs = []
                for q, cid in lst:
                    a, b, c = q
                    d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
                    if d == 0:
                        continue
                    l1 = ((b[1] - c[1]) * (X - c[0]) + (c[0] - b[0]) * (Z - c[1])) / d
                    l2 = ((c[1] - a[1]) * (X - c[0]) + (a[0] - c[0]) * (Z - c[1])) / d
                    l3 = 1 - l1 - l2
                    if min(l1, l2, l3) < -1e-9:
                        continue
                    hs.append((l1 * a[2] + l2 * b[2] + l3 * c[2], cid))
                cells = defaultdict(list)
                for h, cid in hs:
                    cells[cid].append(h)
                if len(cells) < 2:
                    continue
                ids = sorted(cells)
                best = min(abs(h1 - h2) for k1 in ids for k2 in ids if k1 < k2 for h1 in cells[k1] for h2 in cells[k2])
                if best <= 4:
                    dh["coplanar<=4"] += 1
                elif best <= 64:
                    dh["near<=64"] += 1
                elif best <= 512:
                    dh["<=512"] += 1
                else:
                    dh[">512"] += 1
    print("  cross-cell overlap samples by height gap:", dict(dh))


if __name__ == "__main__":
    for a in (sys.argv[1:] or ["88"]):
        main(int(a))
