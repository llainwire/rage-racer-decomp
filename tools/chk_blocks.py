"""Checker helper: compare GameSceneAssetHeader blocks between course packs (read-only)."""
import hashlib
import struct
import sys
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "extracted" / "assets_raw"


def blocks(aid):
    d = (RAW / ("asset_%03d.bin" % aid)).read_bytes()
    offs = list(struct.unpack_from("<11i", d, 0))
    out = []
    for i in range(11):
        e = offs[i + 1] if i < 10 else len(d)
        out.append(hashlib.md5(d[offs[i]:e]).hexdigest()[:8])
    return out


if __name__ == "__main__":
    ids = [int(a) for a in sys.argv[1:]] or [94, 120, 88, 102]
    tab = {a: blocks(a) for a in ids}
    for a in ids:
        print("%03d" % a, " ".join(tab[a]))
    # which packs share each block of the first id
    allb = {a: blocks(a) for a in range(88, 135, 2)}
    a0 = ids[0]
    for i in range(11):
        same = [a for a in allb if a != a0 and allb[a][i] == allb[a0][i]]
        print("block %d of %03d equals: %s" % (i, a0, same))
