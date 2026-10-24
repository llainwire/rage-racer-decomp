"""Adversarial checker for tools/export_course_scenes.py output (read-only on the export).

python -I tools/chk_course_scenes.py [OUT_ROOT] [--packs 88,90] [--json out.json]

Independent of export_course_scenes: names from rr_assets.exe_tables, geometry
from rr_assets.track_models, OBJ/MTL/PNG parsed here.
"""
import json
import math
import re
import struct
import sys
import zlib
from collections import Counter, defaultdict
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import rr_assets as ra  # noqa: E402

REPO = TOOLS.parent
RAW = REPO / "extracted" / "assets_raw"

# ------------------------------------------------------------- PNG


def unfilter(raw, w, h, bpp):
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
    return out


_png_cache = {}


def png_load(path):
    """-> (w, h, alpha bytearray, error or None). Validates signature, CRCs, IHDR, IEND, IDAT size."""
    key = str(path)
    if key in _png_cache:
        return _png_cache[key]
    res = None
    try:
        b = Path(path).read_bytes()
        if b[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("bad signature")
        p = 8
        chunks = []
        idat = bytearray()
        while p < len(b):
            n = struct.unpack_from(">I", b, p)[0]
            tag = b[p + 4:p + 8]
            body = b[p + 8:p + 8 + n]
            crc = struct.unpack_from(">I", b, p + 8 + n)[0]
            if zlib.crc32(tag + body) & 0xFFFFFFFF != crc:
                raise ValueError("CRC mismatch in %s" % tag)
            chunks.append(tag)
            if tag == b"IHDR":
                w, h, bd, ct, cm, fm, il = struct.unpack(">IIBBBBB", body)
            elif tag == b"IDAT":
                idat += body
            p += 12 + n
        if chunks[0] != b"IHDR" or chunks[-1] != b"IEND" or b"IDAT" not in chunks:
            raise ValueError("chunk order %s" % chunks)
        if bd != 8 or ct not in (2, 6) or il != 0 or cm != 0 or fm != 0:
            raise ValueError("unsupported IHDR bd %d ct %d il %d" % (bd, ct, il))
        bpp = 4 if ct == 6 else 3
        raw = zlib.decompress(bytes(idat))
        if len(raw) != h * (1 + w * bpp):
            raise ValueError("IDAT size %d != %d" % (len(raw), h * (1 + w * bpp)))
        fts = set(raw[y * (1 + w * bpp)] for y in range(h))
        if not fts <= {0, 1, 2, 3, 4}:
            raise ValueError("bad filter types %s" % fts)
        if fts == {0}:
            px = bytearray()
            for y in range(h):
                px += raw[y * (1 + w * bpp) + 1:(y + 1) * (1 + w * bpp)]
        else:
            px = unfilter(raw, w, h, bpp)
        if bpp == 4:
            alpha = px[3::4]
        else:
            alpha = bytearray([255]) * (w * h)
        res = (w, h, alpha, None)
    except Exception as e:  # noqa: BLE001
        res = (0, 0, None, "%s: %s" % (type(e).__name__, e))
    _png_cache[key] = res
    return res


# ------------------------------------------------------------- OBJ / MTL


def parse_mtl(path):
    mats = {}
    cur = None
    for line in Path(path).read_text().splitlines():
        t = line.split()
        if not t or t[0].startswith("#"):
            continue
        if t[0] == "newmtl":
            if t[1] in mats:
                mats[t[1]]["dup"] = True
            cur = mats.setdefault(t[1], {})
        elif cur is not None and t[0] in ("map_Kd", "map_d"):
            cur[t[0]] = " ".join(t[1:])
        elif cur is not None and t[0] in ("Kd", "d"):
            cur[t[0]] = [float(x) for x in t[1:]]
    return mats


def parse_obj(path):
    V, C, VT = [], [], []
    objs = []
    cur = None
    group = None
    mtl = None
    info = None
    mtllib = None
    bad_lines = 0
    with open(path) as fh:
        for line in fh:
            if line.startswith("# {"):
                try:
                    info = json.loads(line[2:])
                except ValueError:
                    info = None
                continue
            if not line.strip() or line[0] == "#":
                continue
            t = line.split()
            k = t[0]
            if k == "v":
                V.append(tuple(float(x) for x in t[1:4]))
                C.append(tuple(float(x) for x in t[4:7]) if len(t) >= 7 else None)
            elif k == "vt":
                VT.append((float(t[1]), float(t[2])))
            elif k == "f":
                cs = []
                for c in t[1:]:
                    s = c.split("/")
                    cs.append((int(s[0]) - 1, int(s[1]) - 1 if len(s) > 1 and s[1] else None))
                if cur is None:
                    bad_lines += 1
                    continue
                cur["faces"].append((mtl, cs))
            elif k == "o":
                cur = {"name": t[1], "group": group, "faces": [], "info": info}
                info = None
                objs.append(cur)
            elif k == "g":
                group = t[1] if len(t) > 1 else None
            elif k == "usemtl":
                mtl = t[1]
            elif k == "mtllib":
                mtllib = t[1]
            else:
                bad_lines += 1
    return {"V": V, "C": C, "VT": VT, "objs": objs, "mtllib": mtllib, "bad_lines": bad_lines}


# ------------------------------------------------------------- expected polys (declared convention)


def expected_polys(idx):
    """PS1 strip quad -> polygons in the exporter's declared OBJ order (v0 v2 v3 v1; tris
    (0 1 2)->(0 2 1), (1 3 2)->(1 2 3)).  Returned as tuples of corner slots 0..3."""
    if len(set(idx)) == 4:
        return [(0, 2, 3, 1)]
    out, seen = [], set()
    for tri, order in (((0, 1, 2), (0, 2, 1)), ((1, 3, 2), (1, 2, 3))):
        ids = frozenset(idx[k] for k in tri)
        if len(ids) == 3 and ids not in seen:
            seen.add(ids)
            out.append(order)
    return out


def solve4(A, b):
    n = 4
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-6:
            return None
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_affine(pairs):
    """pairs [(raw xyz, exported xyz PS1)] -> (M 3x3, t, max residual) or None if degenerate."""
    # centre raw points to keep the normal equations well-conditioned
    cx = [sum(p[0][k] for p in pairs) / len(pairs) for k in range(3)]
    rows = [[p[0][0] - cx[0], p[0][1] - cx[1], p[0][2] - cx[2], 1.0] for p in pairs]
    AtA = [[sum(r[i] * r[j] for r in rows) for j in range(4)] for i in range(4)]
    # scale check for planarity: smallest eigen proxy via determinant of 3x3 part
    M, t = [], []
    for k in range(3):
        Atb = [sum(r[i] * p[1][k] for r, p in zip(rows, pairs)) for i in range(4)]
        sol = solve4(AtA, Atb)
        if sol is None:
            return None
        M.append(sol[:3])
        t.append(sol[3])
    res = 0.0
    for r, p in zip(rows, pairs):
        for k in range(3):
            e = M[k][0] * r[0] + M[k][1] * r[1] + M[k][2] * r[2] + t[k]
            res = max(res, abs(e - p[1][k]))
    return M, t, res


def det3(M):
    return (M[0][0] * (M[1][1] * M[2][2] - M[1][2] * M[2][1])
            - M[0][1] * (M[1][0] * M[2][2] - M[1][2] * M[2][0])
            + M[0][2] * (M[1][0] * M[2][1] - M[1][1] * M[2][0]))


def ortho_err(M):
    e = 0.0
    for i in range(3):
        for j in range(3):
            d = sum(M[k][i] * M[k][j] for k in range(3))
            e = max(e, abs(d - (1.0 if i == j else 0.0)))
    return e


def planar(pts):
    if len(pts) < 4:
        return True
    p0 = pts[0]
    best = None
    for a in pts[1:]:
        for b in pts[1:]:
            u = [a[k] - p0[k] for k in range(3)]
            v = [b[k] - p0[k] for k in range(3)]
            n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            if abs(n[0]) + abs(n[1]) + abs(n[2]) > 1e3:
                best = n
                break
        if best:
            break
    if best is None:
        return True
    nn = math.sqrt(sum(x * x for x in best))
    return all(abs(sum(best[k] * (q[k] - p0[k]) for k in range(3))) / nn < 2.0 for q in pts)


# ------------------------------------------------------------- per-pack check

NAME_RES = {
    "terrain": re.compile(r"^terrain_cell_(\d\d)_(\d\d)$"),
    "course": re.compile(r"^course_model_(\d\d)_i(\d+)$"),
    "scen": re.compile(r"^scenery_(bank[12])_model_(\d\d)_i(\d+)$"),
    "cunp": re.compile(r"^course_model_(\d\d)_unplaced$"),
    "bunp": re.compile(r"^(bank[12])_model_(\d\d)_unplaced$"),
}


def classify(name):
    m = NAME_RES["terrain"].match(name)
    if m:
        return ("terrain", int(m.group(1)), int(m.group(2)))
    m = NAME_RES["course"].match(name)
    if m:
        return ("course_models", int(m.group(1)), int(m.group(2)))
    m = NAME_RES["scen"].match(name)
    if m:
        return (m.group(1), int(m.group(2)), int(m.group(3)))
    m = NAME_RES["cunp"].match(name)
    if m:
        return ("course_models", int(m.group(1)), "unplaced")
    m = NAME_RES["bunp"].match(name)
    if m:
        return (m.group(1), int(m.group(2)), "unplaced")
    return None


def track_points(aid):
    d = (RAW / ("asset_%03d.bin" % aid)).read_bytes()
    offs = struct.unpack_from("<11i", d, 0)
    b = offs[4]
    n = struct.unpack_from("<i", d, b)[0]
    pts = []
    for i in range(n):
        x, z, y = struct.unpack_from("<iih", d, b + 4 + 0x18 * i)
        pts.append((4 * x, 4 * z, -4 * y))       # Blender X Y Z
    return pts


def check_pack(aid, root, tables, problems, stats):
    rel = aid - 0x58
    course, cls = (rel >> 1) & 3, rel >> 3
    fname = tables["asset_paths"][aid].split(chr(92))[-1]
    cfolder = tables["course_names"][course].replace(" ", "_")
    kfolder = "CLASS%d_%s" % (cls + 1, tables["gp_names"][cls])
    stem = fname.replace(".", "_")
    out = Path(root) / cfolder / kfolder
    P = lambda msg: problems.append("asset %03d: %s" % (aid, msg))  # noqa: E731
    st = stats[aid] = Counter()
    st["folder"] = "%s/%s/%s" % (cfolder, kfolder, stem)
    for f in ("%s.obj" % stem, "%s.mtl" % stem, "variants.obj", "unplaced.obj", "manifest.json"):
        if not (out / f).is_file():
            P("missing file %s/%s" % (out, f))
    if not (out / ("%s.obj" % stem)).is_file():
        return
    mats = parse_mtl(out / ("%s.mtl" % stem))
    tm = ra.track_models(aid)
    terrain = tm["terrain"]
    pool = terrain["vertex_list"]
    cells_by_grid = {}
    for c in terrain["cell_list"]:
        cells_by_grid.setdefault(tuple(c["grid"]), []).append(c)
    models = {"course_models": tm["course_models"]["entry_list"], "bank1": tm["bank1"]["model_list"],
              "bank2": tm["bank2"]["model_list"]}
    mverts = {"bank1": tm["bank1"]["vertex_list"], "bank2": tm["bank2"]["vertex_list"]}

    # MTL textures
    for name, m in mats.items():
        if m.get("dup"):
            P("duplicate newmtl %s" % name)
        for key in ("map_Kd", "map_d"):
            if key in m:
                pth = out / m[key]
                if not pth.is_file():
                    P("%s %s missing" % (key, m[key]))
                    continue
                w, h, _a, err = png_load(pth)
                if err:
                    P("%s %s invalid PNG: %s" % (key, m[key], err))
                st["png_checked"] += 1
        if ("map_Kd" in m) != ("map_d" in m):
            P("material %s has map_Kd xor map_d" % name)

    seen_names = Counter()
    covered = defaultdict(set)
    terrain_bbox = {}
    terrain_tris = []
    cm_placed = []
    for fileno, fn in enumerate(("%s.obj" % stem, "variants.obj", "unplaced.obj")):
        if not (out / fn).is_file():
            continue
        ob = parse_obj(out / fn)
        if ob["bad_lines"]:
            P("%s: %d unexpected lines" % (fn, ob["bad_lines"]))
        if ob["mtllib"] != "%s.mtl" % stem:
            P("%s: mtllib %s" % (fn, ob["mtllib"]))
        V, VT, C = ob["V"], ob["VT"], ob["C"]
        for i, v in enumerate(V):
            if not all(math.isfinite(x) for x in v):
                P("%s: non-finite vertex %d" % (fn, i))
                break
        if V:
            st["maxabs_%d" % fileno] = max(st["maxabs_%d" % fileno], int(max(max(abs(x) for x in v) for v in V)))
        for i, t in enumerate(VT):
            if not all(math.isfinite(x) for x in t):
                P("%s: non-finite vt %d" % (fn, i))
                break
        for o in ob["objs"]:
            name = o["name"]
            seen_names[name] += 1
            cl = classify(name)
            if cl is None:
                P("%s: object name %s does not follow the naming scheme" % (fn, name))
                continue
            src, mi, inst = cl
            if fileno == 0:
                want_group = {"terrain": "terrain", "course_models": "course_models", "bank1": "scenery",
                              "bank2": "scenery"}[src]
                if inst == "unplaced":
                    P("%s: unplaced object %s in the scene file" % (fn, name))
                if o["group"] != want_group:
                    st["wrong_group"] += 1
            if fileno == 2 and inst != "unplaced":
                P("%s: placed-style name %s in unplaced.obj" % (fn, name))
            if fileno != 2 and inst == "unplaced":
                P("%s: unplaced name %s outside unplaced.obj" % (fn, name))
            st["objects_%d" % fileno] += 1
            # ---- face-level comparison against rr_assets
            if src == "terrain":
                sy, sx = mi, inst
                cs = cells_by_grid.get((sx, sy))
                if not cs:
                    P("%s: %s has no cell at grid (sx %d, sy %d) in rr_assets" % (fn, name, sx, sy))
                    continue
                if len(cs) != 1:
                    st["grid_multi"] += 1
                cell = cs[0]
                covered["terrain"].add(cell["id"])
                rfaces = cell["faces"]
                ox, oy, oz = cell["origin"]
                rv = lambda i: pool[i]  # noqa: E731
            else:
                ent = models[src]
                if mi >= len(ent):
                    P("%s: %s model index %d >= %d" % (fn, name, mi, len(ent)))
                    continue
                covered[src].add(mi)
                rfaces = ent[mi]["faces"]
                if src == "course_models":
                    verts = ent[mi]["vertices"]
                    rv = lambda i, verts=verts: verts[i]  # noqa: E731
                else:
                    vl = mverts[src]
                    rv = lambda i, vl=vl: vl[i]  # noqa: E731
                if src != "course_models":
                    rfaces = [f for f in rfaces if len(set(f["idx"][:3])) == 3]
            exp = []
            for f in rfaces:
                for order in expected_polys(f["idx"]):
                    exp.append((f, order))
            got = o["faces"]
            if len(exp) != len(got):
                P("%s: %s has %d faces, rr_assets gives %d" % (fn, name, len(got), len(exp)))
                st["facecount_mismatch"] += 1
                continue
            st["faces_%d" % fileno] += len(got)
            pairs = {}
            ok_pos = True
            for (f, order), (mname, cs) in zip(exp, got):
                if len(cs) != len(order):
                    P("%s: %s corner count differs" % (fn, name))
                    ok_pos = False
                    break
                if mname not in mats:
                    P("%s: usemtl %s not in MTL" % (fn, mname))
                    continue
                m = mats[mname]
                textured = "map_Kd" in m
                if textured != (f["uv"] is not None):
                    st["textured_flag_mismatch"] += 1
                for slot, (vi, ti) in zip(order, cs):
                    if vi < 0 or vi >= len(V):
                        P("%s: vertex index out of range in %s" % (fn, name))
                        ok_pos = False
                        break
                    r = rv(f["idx"][slot])
                    X, Y, Z = V[vi]
                    e = (X, -Z, Y)            # back to PS1 x, y, z
                    if src == "terrain":
                        if (round(e[0] - ox), round(e[1] - oy), round(e[2] - oz)) != tuple(r[:3]):
                            st["terrain_vertex_mismatch"] += 1
                    else:
                        key = f["idx"][slot]
                        if key in pairs and pairs[key][1] != e:
                            st["inconsistent_vertex"] += 1
                        pairs[key] = (tuple(r[:3]), e)
                    if textured:
                        if ti is None or ti < 0 or ti >= len(VT):
                            st["textured_corner_without_vt"] += 1
                            continue
                        u, v = VT[ti]
                        if "_tw_" in mname:
                            w, h, _a, _e = png_load(out / m["map_Kd"])
                            pu, pv = w, h
                        else:
                            pu = pv = 256
                            if not (-1e-6 <= u <= 1 + 1e-6 and -1e-6 <= v <= 1 + 1e-6):
                                st["page_vt_outside_01"] += 1
                        uu, vv = u * pu, (1 - v) * pv
                        if f["uv"] is not None:
                            ru, rv_ = f["uv"][slot]
                            if abs(uu - ru) > 0.01 or abs(vv - rv_) > 0.01:
                                st["uv_mismatch"] += 1
                    # vertex colour
                    col = C[vi]
                    if col is not None and f["rgb"] is not None:
                        if any(abs(col[k] - f["rgb"][k] / 255.0) > 0.003 for k in range(3)):
                            st["vcolour_differs_from_face_rgb_" + src] += 1
                if textured and f["uv"] is not None:
                    mm = re.match(r"^tpage_([0-9A-F]{4})_clut_([0-9A-F]{4})", mname)
                    if not mm:
                        st["textured_material_unparsed"] += 1
                    else:
                        tp, cl_ = int(mm.group(1), 16), int(mm.group(2), 16)
                        if tp != f["tpage"]:
                            st["tpage_mismatch"] += 1
                        dk = (cl_ - f["clut"]) & 0xFFFF
                        st["clut_delta_%s_%d" % (src, dk)] += 1
                    # sampled alpha over the uv rect
                    w, h, alpha, err = png_load(out / m["map_Kd"])
                    if alpha is not None:
                        us = [VT[ti][0] * w for _vi, ti in cs if ti is not None]
                        vs = [(1 - VT[ti][1]) * h for _vi, ti in cs if ti is not None]
                        if us:
                            lo_u, hi_u, lo_v, hi_v = min(us), max(us), min(vs), max(vs)
                            opaque = 0
                            n = 0
                            for a in range(8):
                                for b2 in range(8):
                                    su = lo_u + (hi_u - lo_u) * (a + 0.5) / 8
                                    sv = lo_v + (hi_v - lo_v) * (b2 + 0.5) / 8
                                    px = int(math.floor(su)) % w
                                    py = int(math.floor(sv)) % h
                                    n += 1
                                    if alpha[py * w + px]:
                                        opaque += 1
                            if opaque == 0:
                                st["faces_fully_transparent_%d" % fileno] += 1
                                if fileno == 0:
                                    st["transparent_mat_" + mname] += 1
                if src == "terrain" and fileno == 0:
                    ps = [V[vi] for vi, _ti in cs]
                    for k in range(1, len(ps) - 1):
                        terrain_tris.append((ps[0], ps[k], ps[k + 1], name))
            if not ok_pos:
                continue
            if src == "terrain" and fileno == 0:
                xs = [V[vi][0] for _m, cs in got for vi, _t in cs]
                ys = [V[vi][1] for _m, cs in got for vi, _t in cs]
                terrain_bbox[name] = (min(xs), min(ys), max(xs), max(ys), sx, sy)
                cx, cy = ox, oz
                if (cx, cy) != ((sx * 2048 + 1024) * 4, (sy * 2048 + 1024) * 4):
                    st["cell_origin_not_grid_formula"] += 1
            if src != "terrain" and pairs:
                plist = list(pairs.values())
                rawpts = [p[0] for p in plist]
                if planar(rawpts):
                    # distances only
                    md = 0.0
                    for i in range(min(len(plist), 12)):
                        for j in range(i + 1, min(len(plist), 12)):
                            a, b = plist[i], plist[j]
                            d0 = math.dist(a[0], b[0])
                            d1 = math.dist(a[1], b[1])
                            md = max(md, abs(d0 - d1) / max(d0, 64.0))
                    st["planar_models_%d" % fileno] += 1
                    if md > 0.03:
                        st["planar_distance_distortion_%d" % fileno] += 1
                        P("%s: %s planar model distances distorted %.3f" % (fn, name, md))
                else:
                    fit = fit_affine(plist)
                    if fit is None:
                        st["fit_failed"] += 1
                    else:
                        M, t, res = fit
                        oe, dt = ortho_err(M), det3(M)
                        st["fitted_%d" % fileno] += 1
                        if oe > 0.02 or dt < 0 or res > 8:
                            st["non_rigid_%d" % fileno] += 1
                            P("%s: %s transform not a proper rotation: ortho err %.3f det %.3f residual %.1f"
                              % (fn, name, oe, dt, res))
                if fileno == 0:
                    pts = [p[1] for p in plist]
                    cxyz = [sum(q[k] for q in pts) / len(pts) for k in range(3)]
                    cm_placed.append((name, cxyz, min(q[1] for q in pts), max(q[1] for q in pts)))
    for n, c in seen_names.items():
        if c > 1:
            P("object name %s used %d times" % (n, c))
    # coverage of every model / cell
    if covered["terrain"] != {c["id"] for c in terrain["cell_list"]}:
        P("terrain cells missing: %s" % sorted({c["id"] for c in terrain["cell_list"]} - covered["terrain"])[:10])
    for src in ("course_models", "bank1", "bank2"):
        missing = set(range(len(models[src]))) - covered[src]
        if missing:
            P("%s models missing from all three OBJ files: %s" % (src, sorted(missing)))
        st["models_" + src] = len(models[src])
    st["terrain_cells"] = len(terrain["cell_list"])
    # terrain tiling
    over = 0
    for name, (x0, y0, x1, y1, sx, sy) in terrain_bbox.items():
        if x0 < sx * 8192 - 2 or x1 > sx * 8192 + 8192 + 2 or y0 < sy * 8192 - 2 or y1 > sy * 8192 + 8192 + 2:
            over += 1
    st["cells_outside_own_square"] = over
    # raster coverage at 128-unit samples inside present squares, and track points
    B = 8192
    buckets = defaultdict(list)
    for t in terrain_tris:
        xs = [p[0] for p in t[:3]]
        ys = [p[1] for p in t[:3]]
        for bx in range(int(min(xs)) // 1024, int(max(xs)) // 1024 + 1):
            for by in range(int(min(ys)) // 1024, int(max(ys)) // 1024 + 1):
                buckets[(bx, by)].append(t)

    def hits(X, Y):
        out_ = []
        for a, b, c, nm in buckets.get((int(X) // 1024, int(Y) // 1024), ()):
            d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if d == 0:
                continue
            l1 = ((b[1] - c[1]) * (X - c[0]) + (c[0] - b[0]) * (Y - c[1])) / d
            l2 = ((c[1] - a[1]) * (X - c[0]) + (a[0] - c[0]) * (Y - c[1])) / d
            l3 = 1 - l1 - l2
            if min(l1, l2, l3) < -1e-9:
                continue
            out_.append((l1 * a[2] + l2 * b[2] + l3 * c[2], nm))
        return out_

    samples = holes = multi = 0
    for name, (x0, y0, x1, y1, sx, sy) in terrain_bbox.items():
        for i in range(64):
            for j in range(64):
                X = sx * B + 64 + 128 * i
                Y = sy * B + 64 + 128 * j
                hs = hits(X, Y)
                samples += 1
                if not hs:
                    holes += 1
                elif len({nm for _h, nm in hs}) > 1:
                    multi += 1
    st["tile_samples"] = samples
    st["tile_holes"] = holes
    st["tile_multi_cell"] = multi
    tp = track_points(aid)
    off = 0
    far = 0
    for X, Y, Z in tp:
        hs = hits(X, Y)
        if not hs:
            off += 1
            continue
        if min(abs(h - Z) for h, _n in hs) > 64:
            far += 1
    st["track_points"] = len(tp)
    st["track_points_no_terrain"] = off
    st["track_points_gt64"] = far
    # placed models: distance to the track, base vs terrain
    def nearest_tp(c):
        return min(math.hypot(c[0] - q[0], c[1] - q[1]) for q in tp)
    dists = []
    for name, c, zlo, zhi in cm_placed:
        d = nearest_tp(c)
        dists.append(d)
        if d > 120000:
            st["placed_far_from_track"] += 1
            P("%s centroid %.0f units from the nearest track point" % (name, d))
    if dists:
        dists.sort()
        st["placed_track_dist_median"] = int(dists[len(dists) // 2])
        st["placed_track_dist_max"] = int(dists[-1])


def main(argv):
    root = REPO / "extracted" / "models" / "courses"
    packs = list(range(88, 135, 2))
    js = None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--packs":
            packs = [int(x) for x in args.pop(0).split(",")]
        elif a == "--json":
            js = args.pop(0)
        else:
            root = Path(a)
    tables = ra.exe_tables()
    problems = []
    stats = {}
    for aid in packs:
        try:
            check_pack(aid, root, tables, problems, stats)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            problems.append("asset %03d: checker exception %s: %s" % (aid, type(e).__name__, e))
        st = stats.get(aid, {})
        keys = sorted(k for k in st if not k.startswith("transparent_mat_"))
        print("%03d %s" % (aid, " ".join("%s=%s" % (k, st[k]) for k in keys)))
        tm = sorted(((v, k) for k, v in st.items() if k.startswith("transparent_mat_")), reverse=True)[:6]
        if tm:
            print("    transparent scene faces by material:", tm)
        sys.stdout.flush()
    print("PROBLEMS %d" % len(problems))
    for p in problems[:80]:
        print("  ", p)
    if js:
        Path(js).write_text(json.dumps({"problems": problems, "stats": {str(k): dict(v) for k, v in stats.items()}},
                                       indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
