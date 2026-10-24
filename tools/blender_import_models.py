"""Import the Rage Racer (PAL) model exports under extracted/models into Blender.

The exports are written by tools/export_course_scenes.py (courses/),
tools/export_car_models.py (cars/) and tools/export_other_models.py (other/);
docs/model_asset_formats.md describes them.  This script builds nested
collections:

    Courses > <COURSE> > <CLASS> (<stem>) > Terrain, Course models, Scenery
                                            (+ Variants, Unplaced: hidden)
    Cars    > <MAKER_NAME> > <variant: CAR_xy or RIVAL_...>
    Other   > <group> > <name>

Run it from Blender's Scripting tab (Text > Open this file, adjust the
SETTINGS block if you like, Run Script) or from a shell:

    blender --python tools/blender_import_models.py -- [options] [NAME ...]

NAME (any number) imports only the courses, classes, cars, variants or groups
whose folder or file name contains NAME, ignoring case, spaces and hyphens:
"Mythical Coast", "MYTHICAL_COAST/CLASS1", "BIG1", "ERRISO", "CAR_00",
"controller".  A NAME with "/" matches consecutive folder levels.  Without a
NAME everything is imported (all 24 course scenes are about 270 MB of OBJ and
take several minutes; filtering is recommended).

Options after "--":
  --root DIR           the extracted/models folder (default: found next to this script)
  --only KINDS         comma list of courses, cars, other (default: all three)
  --car-files MODE     default: one assembled car per variant (default tyre set);
                       all: every OBJ of a variant (_fast, _tireN, _all_models, rival _far)
                       in its own sub-collection, all but the default hidden
  --no-extras          courses: skip variants.obj and unplaced.obj
  --shading MODE       auto (courses unlit, cars and other lit), unlit, lit
  --cull               enable back-face culling (the game culls back faces)
  --no-layout          leave cars and other models at their own origins
                       (default: spread on a grid so they do not overlap)
  --no-alpha-check     treat every textured material as alpha clip without
                       reading its PNG (faster)
  --folder DIR         generic mode: import every OBJ in DIR (see --pattern), one
                       collection per file (replaces blender_import_course_models.py)
  --pattern GLOB       file pattern for --folder (default *.obj)
  --save PATH          save the .blend when done

Conventions of the OBJ files (see their header comments):
  * raw PS1 vertex units (4 x game world units), already in Blender axes
    (X = PS1 x, Y = PS1 z, Z = -PS1 y), so they are imported with Forward Y, Up Z;
  * textures are 256x256 RGBA page PNGs; colour word 0x0000 is alpha 0;
    materials named *_semi_* are PS1 semi-transparent (PNG alpha 128 on STP texels);
  * course faces carry vertex colours (face colour byte / 255).  On the PS1 a
    textured course face shows min(1, texel * colour / 128).  Blender's OBJ
    importer decodes vertex colours from sRGB, so the course materials read the
    raw value back (color_srgb), store colour / 128 in the float attribute
    "ps1_modulation", multiply it with the texture sampled as Non-Color, clamp
    and decode to linear (the sRGB curve without its linear toe), as
    extracted/models/courses/blender_import_course.py does.  With the Standard
    view transform the shown colours equal the PS1 values.

Tested only by byte-compiling (python -I -m py_compile); the material code is
the course helper's, which was checked in Blender 4.2.  Needs Blender 3.3 or
newer for wm.obj_import (older versions fall back to import_scene.obj, which
ignores vertex colours).
"""
import fnmatch
import os
import re
import sys
import time

import bpy

# ---------------------------------------------------------------- SETTINGS
# Used when the script runs from Blender's text editor (no "--" arguments).
MODELS_ROOT = ""            # "" = find extracted/models next to this script
NAMES = []                  # e.g. ["MYTHICAL_COAST/CLASS1"], ["ERRISO"]; [] = everything
ONLY = ("courses", "cars", "other")
CAR_FILES = "default"       # "default" or "all"
COURSE_EXTRAS = True        # import variants.obj / unplaced.obj (hidden)
SHADING = "auto"            # "auto", "unlit" or "lit"
BACKFACE_CULLING = False
LAYOUT = True               # spread cars / other models on a grid
ALPHA_CHECK = True          # read each PNG once to tell opaque from cut-out textures
STANDARD_VIEW = True        # view transform "Standard" so colours show as on the PS1
VIEWPORT_TEXTURE = True     # solid-mode viewport colour = texture
CLIP_START, CLIP_END = 4.0, 2_000_000.0
FOLDER, PATTERN = "", "*.obj"
SAVE_AS = ""
# -------------------------------------------------------------------------

MOD_ATTR = "ps1_modulation"
COURSE_ORDER = ["MYTHICAL_COAST", "OVER_PASS_CITY", "LAKESIDE_GATE", "THE_EXTREME_OVAL"]
COURSE_GROUPS = (("terrain", "Terrain"), ("course_models", "Course models"), ("scenery", "Scenery"))
MAX_NAME = 63
_ALPHA_CACHE = {}


# ---------------------------------------------------------------- paths and arguments
def script_dir():
    cands = []
    f = globals().get("__file__")
    if f:
        cands.append(f)
    try:
        space = bpy.context.space_data
        if space is not None and getattr(space, "type", "") == "TEXT_EDITOR" and space.text:
            cands.append(bpy.path.abspath(space.text.filepath))
    except (AttributeError, RuntimeError):
        pass
    for t in bpy.data.texts:
        if t.filepath and os.path.basename(t.filepath) == "blender_import_models.py":
            cands.append(bpy.path.abspath(t.filepath))
    for c in cands:
        if c and os.path.isfile(c):
            return os.path.dirname(os.path.abspath(c))
    if bpy.data.filepath:
        return os.path.dirname(bpy.data.filepath)
    return os.getcwd()


def find_root(explicit):
    if explicit:
        return os.path.abspath(explicit)
    here = script_dir()
    for c in (os.path.join(here, "..", "extracted", "models"),
              os.path.join(here, "extracted", "models"),
              os.path.join(here, "models"), here):
        c = os.path.normpath(c)
        if any(os.path.isdir(os.path.join(c, k)) for k in ("courses", "cars", "other")):
            return c
    return os.path.normpath(os.path.join(here, "..", "extracted", "models"))


def parse_args(argv):
    o = {"root": MODELS_ROOT, "names": list(NAMES), "only": list(ONLY), "car_files": CAR_FILES,
         "extras": COURSE_EXTRAS, "shading": SHADING, "cull": BACKFACE_CULLING, "layout": LAYOUT,
         "alpha_check": ALPHA_CHECK, "folder": FOLDER, "pattern": PATTERN, "save": SAVE_AS}
    it = iter(argv)
    for a in it:
        if a == "--root":
            o["root"] = next(it)
        elif a == "--only":
            o["only"] = [k.strip().lower() for k in next(it).split(",") if k.strip()]
        elif a == "--car-files":
            o["car_files"] = next(it).lower()
        elif a == "--no-extras":
            o["extras"] = False
        elif a == "--shading":
            o["shading"] = next(it).lower()
        elif a == "--cull":
            o["cull"] = True
        elif a == "--no-layout":
            o["layout"] = False
        elif a == "--no-alpha-check":
            o["alpha_check"] = False
        elif a == "--folder":
            o["folder"] = next(it)
        elif a == "--pattern":
            o["pattern"] = next(it)
        elif a == "--save":
            o["save"] = next(it)
        elif a.startswith("--"):
            raise SystemExit("unknown option %s (see the docstring)" % a)
        else:
            o["names"].append(a)
    bad = [k for k in o["only"] if k not in ("courses", "cars", "other")]
    if bad:
        raise SystemExit("--only takes courses, cars, other (got %s)" % ", ".join(bad))
    if o["car_files"] not in ("default", "all"):
        raise SystemExit("--car-files takes default or all")
    if o["shading"] not in ("auto", "unlit", "lit"):
        raise SystemExit("--shading takes auto, unlit or lit")
    return o


def norm(s):
    return re.sub(r"[\s\-.]+", "_", s.strip().lower())


def matches(names, parts):
    """True when names is empty or one name matches consecutive path parts (substring)."""
    if not names:
        return True
    comps = [norm(p) for p in parts]
    for name in names:
        toks = [norm(t) for t in name.replace("\\", "/").split("/") if t.strip()]
        if not toks:
            continue
        for i in range(len(comps) - len(toks) + 1):
            if all(t in comps[i + j] for j, t in enumerate(toks)):
                return True
    return False


def subdirs(path):
    return sorted(d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d)))


def files(path, ext):
    return sorted(f for f in os.listdir(path) if f.lower().endswith(ext))


# ---------------------------------------------------------------- collections
def new_collection(name, parent):
    c = bpy.data.collections.new(name[:MAX_NAME])
    parent.children.link(c)
    return c


def layer_collection(coll, lc=None):
    lc = lc or bpy.context.view_layer.layer_collection
    if lc.collection == coll:
        return lc
    for ch in lc.children:
        r = layer_collection(coll, ch)
        if r is not None:
            return r
    return None


def hide(coll):
    lc = layer_collection(coll)
    if lc is not None:
        lc.hide_viewport = True


def move_to(obj, coll):
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    coll.objects.link(obj)


# ---------------------------------------------------------------- OBJ reading
def obj_layout(path):
    """({object name: group name}, has vertex colours) from the OBJ text."""
    groups, g, coloured, seen_v = {}, None, False, False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            c = line[:2]
            if c == "g ":
                g = line[2:].strip()
            elif c == "o ":
                groups[line[2:].strip()[:MAX_NAME]] = g
            elif c == "v " and not seen_v:
                seen_v = True
                coloured = len(line.split()) >= 7
    return groups, coloured


def import_obj_file(path):
    """Import one OBJ (Forward Y, Up Z: the files are already in Blender axes); return new objects."""
    before = set(bpy.data.objects)
    if hasattr(bpy.ops.wm, "obj_import"):
        kw = dict(filepath=path, forward_axis="Y", up_axis="Z", use_split_objects=True,
                  use_split_groups=False)
        try:
            bpy.ops.wm.obj_import(**kw)
        except TypeError:
            bpy.ops.wm.obj_import(filepath=path, forward_axis="Y", up_axis="Z")
    else:  # Blender < 3.3 (no vertex colours)
        bpy.ops.import_scene.obj(filepath=path, axis_forward="Y", axis_up="Z",
                                 use_split_objects=True, use_split_groups=False)
    return [o for o in bpy.data.objects if o not in before]


def group_of(obj, groups):
    n = obj.name
    if n in groups:
        return groups[n]
    base = n.rsplit(".", 1)[0] if re.search(r"\.\d{3}$", n) else n
    return groups.get(base)


# ---------------------------------------------------------------- vertex colours (course helper)
def _to_srgb(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1.0 / 2.4) - 0.055


def color_attributes(mesh):
    return getattr(mesh, "color_attributes", None)


def base_color_layer(mesh):
    attrs = color_attributes(mesh)
    if attrs is None:
        return None
    for a in attrs:
        if a.name != MOD_ATTR:
            return a.name
    return None


def add_modulation_attribute(mesh):
    """FLOAT_COLOR MOD_ATTR = round(OBJ colour * 255) / 128 (the PS1 texel factor), from the
    importer's colour attribute read back in sRGB (= the value written in the OBJ)."""
    name = base_color_layer(mesh)
    attrs = color_attributes(mesh)
    if name is None or MOD_ATTR in attrs:
        return name
    src = attrs[name]
    n = len(src.data)
    vals = [0.0] * (4 * n)
    try:
        src.data.foreach_get("color_srgb", vals)
    except (AttributeError, TypeError, RuntimeError):
        lin = [0.0] * (4 * n)
        src.data.foreach_get("color", lin)
        vals = [c if i % 4 == 3 else _to_srgb(c) for i, c in enumerate(lin)]
    out = [1.0 if i % 4 == 3 else round(c * 255.0) / 128.0 for i, c in enumerate(vals)]
    dst = attrs.new(MOD_ATTR, "FLOAT_COLOR", src.domain)
    dst.data.foreach_set("color", out)
    return name


# ---------------------------------------------------------------- materials
def image_alpha_kind(img, check):
    """'opaque' or 'cutout' for a texture PNG (cached by file path)."""
    key = bpy.path.abspath(img.filepath) or img.name
    if key in _ALPHA_CACHE:
        return _ALPHA_CACHE[key]
    kind = "cutout"
    if check:
        try:
            import numpy as np
            w, h = img.size
            if w and h and img.channels == 4:
                px = np.empty(w * h * 4, dtype=np.float32)
                img.pixels.foreach_get(px)
                kind = "cutout" if float(px[3::4].min()) < 0.999 else "opaque"
            elif w and h:
                kind = "opaque"
        except Exception:  # numpy missing or image unreadable: keep the safe choice
            kind = "cutout"
    _ALPHA_CACHE[key] = kind
    return kind


def _to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def set_render_method(mat, mode):
    """mode: OPAQUE, CLIP or BLEND.  Blender < 4.2 uses blend_method (set first), 4.2+
    surface_render_method (set last so it wins); a binary alpha gives a hard cut-out either way."""
    shadow = {"OPAQUE": "OPAQUE", "CLIP": "CLIP", "BLEND": "HASHED"}[mode]
    new = "BLENDED" if mode == "BLEND" else "DITHERED"
    for attr, val in (("blend_method", mode), ("shadow_method", shadow),
                      ("surface_render_method", new)):
        try:
            setattr(mat, attr, val)
        except (AttributeError, TypeError, ValueError):
            pass
    if mode == "CLIP":
        try:
            mat.alpha_threshold = 0.5
        except (AttributeError, TypeError, ValueError):
            pass


def material_inputs(mat):
    """(image, base colour RGBA) left by the OBJ importer's node tree."""
    img, col = None, tuple(getattr(mat, "diffuse_color", (0.8, 0.8, 0.8, 1.0)))
    if mat.use_nodes and mat.node_tree:
        for n in mat.node_tree.nodes:
            if n.type == "TEX_IMAGE" and n.image is not None and img is None:
                img = n.image
            elif n.type == "BSDF_PRINCIPLED":
                col = tuple(n.inputs["Base Color"].default_value)
    return img, col


def build_material(mat, course_style, color_layer, unlit, opt):
    if mat is None or mat.get("rr_shading"):
        return
    img, base = material_inputs(mat)
    semi = "_semi_" in mat.name
    mat.use_nodes = True
    nt = mat.node_tree
    nodes, links = nt.nodes, nt.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (1100, 0)
    alpha, mode = None, "OPAQUE"
    if img is not None:
        tex = nodes.new("ShaderNodeTexImage")
        tex.image, tex.interpolation, tex.location = img, "Closest", (-900, 200)
        if course_style:
            # PS1: shown = min(1, texel * colour / 128), done in display space, then to linear.
            img.colorspace_settings.name = "Non-Color"
            mod = nodes.new("ShaderNodeVertexColor")
            mod.layer_name, mod.location = MOD_ATTR, (-900, -150)
            mul = nodes.new("ShaderNodeVectorMath")
            mul.operation, mul.location = "MULTIPLY", (-650, 100)
            links.new(tex.outputs["Color"], mul.inputs[0])
            links.new(mod.outputs["Color"], mul.inputs[1])
            clamp = nodes.new("ShaderNodeVectorMath")
            clamp.operation, clamp.location = "MINIMUM", (-450, 100)
            clamp.inputs[1].default_value = (1.0, 1.0, 1.0)
            links.new(mul.outputs["Vector"], clamp.inputs[0])
            lin = nodes.new("ShaderNodeVectorMath")
            lin.operation, lin.location = "MULTIPLY_ADD", (-250, 100)
            lin.inputs[1].default_value = (1.0 / 1.055,) * 3
            lin.inputs[2].default_value = (0.055 / 1.055,) * 3
            links.new(clamp.outputs["Vector"], lin.inputs[0])
            gam = nodes.new("ShaderNodeGamma")
            gam.location = (-50, 100)
            gam.inputs["Gamma"].default_value = 2.4
            links.new(lin.outputs["Vector"], gam.inputs["Color"])
            color = gam.outputs["Color"]
        else:
            color = tex.outputs["Color"]   # sRGB PNG: decoded by Blender, shown as stored
        if semi:
            mode, alpha = "BLEND", tex.outputs["Alpha"]
        elif image_alpha_kind(img, opt["alpha_check"]) == "cutout":
            mode = "CLIP"
            cut = nodes.new("ShaderNodeMath")
            cut.operation, cut.location = "GREATER_THAN", (200, -250)
            cut.inputs[1].default_value = 0.5
            links.new(tex.outputs["Alpha"], cut.inputs[0])
            alpha = cut.outputs["Value"]
    elif course_style and color_layer:
        vc = nodes.new("ShaderNodeVertexColor")    # flat course face: imported (linear) vertex colour
        vc.layer_name, vc.location = color_layer, (-250, 100)
        color = vc.outputs["Color"]
    else:
        # flat model face: MTL Kd = PS1 colour / 255 (display value); the OBJ importer copies Kd
        # unconverted, so decode it to linear here to show the PS1 colour
        rgb = nodes.new("ShaderNodeRGB")
        rgb.outputs[0].default_value = (_to_linear(base[0]), _to_linear(base[1]), _to_linear(base[2]), 1.0)
        rgb.location = (-250, 100)
        color = rgb.outputs[0]
    if unlit:
        sh = nodes.new("ShaderNodeEmission")
        sh.inputs["Strength"].default_value = 1.0
        links.new(color, sh.inputs["Color"])
        surf = sh.outputs["Emission"]
    else:
        sh = nodes.new("ShaderNodeBsdfPrincipled")
        for name, value in (("Roughness", 1.0), ("Specular IOR Level", 0.0), ("Specular", 0.0)):
            if name in sh.inputs:
                sh.inputs[name].default_value = value
        links.new(color, sh.inputs["Base Color"])
        surf = sh.outputs["BSDF"]
    sh.location = (450, 100)
    if alpha is not None:
        tr = nodes.new("ShaderNodeBsdfTransparent")
        tr.location = (450, -200)
        mix = nodes.new("ShaderNodeMixShader")
        mix.location = (800, 0)
        links.new(alpha, mix.inputs[0])
        links.new(tr.outputs["BSDF"], mix.inputs[1])
        links.new(surf, mix.inputs[2])
        surf = mix.outputs["Shader"]
    links.new(surf, out.inputs["Surface"])
    set_render_method(mat, mode)
    mat.use_backface_culling = bool(opt["cull"])
    mat["rr_shading"] = mode
    mat["ps1_semi_transparent"] = semi


def finish_objects(objs, kind, coloured, opt):
    """Vertex-colour attribute and materials for the objects of one OBJ file."""
    course_style = False
    layer = None
    done = set()
    if coloured:
        for o in objs:
            if o.type == "MESH" and o.data.name not in done:
                done.add(o.data.name)
                name = add_modulation_attribute(o.data)
                if name is not None:
                    course_style = True
                    layer = layer or name
    unlit = opt["shading"] == "unlit" or (opt["shading"] == "auto" and kind in ("courses", "folder")
                                           and course_style)
    for o in objs:
        if o.type == "MESH":
            for m in o.data.materials:
                build_material(m, course_style, layer, unlit, opt)


# ---------------------------------------------------------------- importers per kind
def import_into(path, kind, opt):
    t = time.time()
    groups, coloured = obj_layout(path)
    objs = import_obj_file(path)
    finish_objects(objs, kind, coloured, opt)
    print("  %-70s %4d objects  %.1fs" % (os.path.relpath(path, opt["root"]) if opt["root"] else path,
                                            len(objs), time.time() - t))
    return objs, groups


def import_courses(root, parent, opt):
    base = os.path.join(root, "courses")
    if not os.path.isdir(base):
        return 0
    order = [c for c in COURSE_ORDER if os.path.isdir(os.path.join(base, c))]
    order += [c for c in subdirs(base) if c not in order]
    top, n, first = None, 0, True
    for course in order:
        course_coll = None
        for cls in subdirs(os.path.join(base, course)):
            d = os.path.join(base, course, cls)
            mtls = files(d, ".mtl")
            stem = os.path.splitext(mtls[0])[0] if mtls else None
            main = stem + ".obj" if stem and os.path.isfile(os.path.join(d, stem + ".obj")) else None
            if main is None:
                cands = [f for f in files(d, ".obj") if f not in ("variants.obj", "unplaced.obj")]
                main = cands[0] if cands else None
            if main is None or not matches(opt["names"], ["courses", course, cls, main[:-4]]):
                continue
            stem = main[:-4]
            if top is None:
                top = new_collection("Courses", parent)
            if course_coll is None:
                course_coll = new_collection(course, top)
            cls_coll = new_collection("%s (%s)" % (cls, stem), course_coll)
            subs = {g: new_collection("%s %s" % (label, stem), cls_coll) for g, label in COURSE_GROUPS}
            objs, groups = import_into(os.path.join(d, main), "courses", opt)
            for o in objs:
                g = group_of(o, groups)
                move_to(o, subs.get(g) or subs["scenery"])
            n += 1
            if opt["extras"]:
                for fn, label in (("variants.obj", "Variants"), ("unplaced.obj", "Unplaced")):
                    p = os.path.join(d, fn)
                    if os.path.isfile(p):
                        c = new_collection("%s %s" % (label, stem), cls_coll)
                        for o in import_into(p, "courses", opt)[0]:
                            move_to(o, c)
                        hide(c)
            if not first:
                hide(cls_coll)        # classes share coordinates: show only the first one
            first = False
    return n


CAR_SUFFIX_ORDER = ["", "_fast"] + ["_tire%d%s" % (t, f) for t in range(5) for f in ("", "_fast")] + \
                   ["_all_models", "_far"]


def car_files(stem, names):
    """[(suffix, file)] in a stable order: default assembly first, then _fast, _tireN, ..."""
    out = []
    for fn in names:
        suffix = fn[len(stem):-4] if fn.startswith(stem) else fn[:-4]
        rank = CAR_SUFFIX_ORDER.index(suffix) if suffix in CAR_SUFFIX_ORDER else len(CAR_SUFFIX_ORDER)
        out.append((rank, suffix, fn))
    return [(s, f) for _, s, f in sorted(out)]


def import_cars(root, parent, opt):
    base = os.path.join(root, "cars")
    if not os.path.isdir(base):
        return 0, []
    top, n, rows = None, 0, []
    for car in subdirs(base):
        car_coll, row = None, []
        for var in subdirs(os.path.join(base, car)):
            d = os.path.join(base, car, var)
            mtls = files(d, ".mtl")
            objs_files = files(d, ".obj")
            if not objs_files or not matches(opt["names"], ["cars", car, var]):
                continue
            stem = os.path.splitext(mtls[0])[0] if mtls else objs_files[0][:-4]
            keyed = car_files(stem, objs_files)
            if opt["car_files"] == "default":
                keyed = [k for k in keyed if k[0] == ""] or keyed[:1]
            if top is None:
                top = new_collection("Cars", parent)
            if car_coll is None:
                car_coll = new_collection(car, top)
            var_coll = new_collection(var, car_coll)
            placed = []
            for suffix, fn in keyed:
                objs = import_into(os.path.join(d, fn), "cars", opt)[0]
                if opt["car_files"] == "all":
                    c = new_collection("%s %s" % (var, suffix.strip("_") or "default"), var_coll)
                    if suffix:
                        hide(c)
                else:
                    c = var_coll
                for o in objs:
                    move_to(o, c)
                placed += objs
                n += 1
            row.append(placed)
        if row:
            rows.append(row)
    return n, rows


def import_other(root, parent, opt):
    base = os.path.join(root, "other")
    if not os.path.isdir(base):
        return 0, []
    top, n, rows = None, 0, []
    for group in subdirs(base):
        group_coll, row = None, []
        for leaf in subdirs(os.path.join(base, group)):
            d = os.path.join(base, group, leaf)
            objs_files = files(d, ".obj")
            if not objs_files or not matches(opt["names"], ["other", group, leaf]):
                continue
            if top is None:
                top = new_collection("Other", parent)
            if group_coll is None:
                group_coll = new_collection(group, top)
            leaf_coll = new_collection(leaf, group_coll)
            placed = []
            for fn in objs_files:
                for o in import_into(os.path.join(d, fn), "other", opt)[0]:
                    move_to(o, leaf_coll)
                    placed.append(o)
                n += 1
            row.append(placed)
        if row:
            rows.append(row)
    return n, rows


def import_folder(folder, pattern, parent, opt):
    paths = sorted(os.path.join(folder, f) for f in os.listdir(folder) if fnmatch.fnmatch(f, pattern))
    if not paths:
        raise SystemExit("no OBJ files match %s" % os.path.join(folder, pattern))
    top = new_collection("OBJ files", parent)
    for p in paths:
        c = new_collection(os.path.splitext(os.path.basename(p))[0], top)
        for o in import_into(p, "folder", opt)[0]:
            move_to(o, c)
    return len(paths)


# ---------------------------------------------------------------- layout and viewport
def bbox(objs):
    """World-space box of mesh objects from their vertices (the imported objects have identity
    rotation and scale; bound_box can still be empty before the depsgraph runs)."""
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3
    for o in objs:
        if o.type != "MESH" or not len(o.data.vertices):
            continue
        co = [0.0] * (3 * len(o.data.vertices))
        o.data.vertices.foreach_get("co", co)
        for i in range(3):
            axis = co[i::3]
            lo[i] = min(lo[i], min(axis) + o.location[i])
            hi[i] = max(hi[i], max(axis) + o.location[i])
    return lo, hi


def layout_rows(rows, y_top, gap):
    """Spread groups of objects: one row per list, going towards Blender -Y from y_top, the
    groups of a row side by side along +X.  Returns the y below the last row."""
    y = y_top
    for row in rows:
        x, depth = 0.0, 0.0
        for objs in row:
            lo, hi = bbox(objs)
            if lo[0] == float("inf"):
                continue
            dx, dy = x - lo[0], y - hi[1]
            for o in objs:
                o.location.x += dx
                o.location.y += dy
            x += (hi[0] - lo[0]) + gap
            depth = max(depth, hi[1] - lo[1])
        y -= depth + gap
    return y


def setup_viewports():
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    space.clip_start, space.clip_end = CLIP_START, CLIP_END
                    if VIEWPORT_TEXTURE:
                        try:
                            space.shading.color_type = "TEXTURE"
                        except (AttributeError, TypeError, ValueError):
                            pass
    if STANDARD_VIEW:
        try:
            bpy.context.scene.view_settings.view_transform = "Standard"
            bpy.context.scene.view_settings.look = "None"
        except (AttributeError, TypeError, ValueError):
            pass
    for cam in bpy.data.cameras:
        cam.clip_start, cam.clip_end = CLIP_START, CLIP_END


def main(argv=None):
    if argv is None:
        argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opt = parse_args(argv)
    t0 = time.time()
    parent = bpy.context.scene.collection
    if opt["folder"]:
        opt["root"] = os.path.abspath(opt["folder"])
        n = import_folder(opt["root"], opt["pattern"], parent, opt)
        print("imported %d OBJ files from %s" % (n, opt["root"]))
    else:
        root = find_root(opt["root"])
        opt["root"] = root
        if not os.path.isdir(root):
            raise SystemExit("models folder not found: %s (set MODELS_ROOT or pass --root)" % root)
        print("Rage Racer models from %s (names: %s)" % (root, ", ".join(opt["names"]) or "all"))
        counts, rows_all = {}, []
        if "courses" in opt["only"]:
            counts["courses"] = import_courses(root, parent, opt)
        if "cars" in opt["only"]:
            counts["cars"], rows = import_cars(root, parent, opt)
            rows_all.append(rows)
        if "other" in opt["only"]:
            counts["other"], rows = import_other(root, parent, opt)
            rows_all.append(rows)
        if opt["layout"]:
            # cars and menu models all sit at their own origins: lay them out in rows (one per car
            # or group) on the Blender -Y side of the course area, which starts near Y = 0
            y = -20000.0
            for rows in rows_all:
                y = layout_rows(rows, y, 200.0) - 2000.0
        print("imported %s" % ", ".join("%s %d file(s)" % kv for kv in counts.items()))
        if not any(counts.values()):
            print("nothing matched %s" % (opt["names"] or "the folder"))
    setup_viewports()
    if opt["save"]:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(opt["save"]))
        print("saved %s" % opt["save"])
    print("done in %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
