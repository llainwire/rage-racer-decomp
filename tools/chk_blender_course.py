"""Checker: run inside Blender (blender -b --python tools/chk_blender_course.py -- HELPER OUTJSON OBJ...).
Imports the given OBJ files through the exporter's helper and reports what Blender actually built."""
import importlib.util
import json
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
helper, outjson, objs = argv[0], argv[1], argv[2:]
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
spec = importlib.util.spec_from_file_location("helper", helper)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
res = {"files": []}
for p in objs:
    root, subs, new = h.import_obj(p)
    res["files"].append({"path": p, "objects": len(new), "collections": {k: len(c.objects) for k, c in subs.items()}})
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
res["mesh_objects"] = len(meshes)
res["faces"] = sum(len(o.data.polygons) for o in meshes)
imgs = list(bpy.data.images)
res["images"] = len(imgs)
res["images_missing"] = [i.filepath for i in imgs if i.source == "FILE" and (not i.has_data and i.size[0] == 0)][:10]
mats = list(bpy.data.materials)
res["materials"] = len(mats)
nolink = 0
tex_mats = 0
for m in mats:
    if not m.use_nodes:
        continue
    tex = [n for n in m.node_tree.nodes if n.type == "TEX_IMAGE"]
    if tex:
        tex_mats += 1
        if tex[0].image is None:
            nolink += 1
res["textured_materials"] = tex_mats
res["textured_materials_without_image"] = nolink
# vertex colour as stored by the importer vs the OBJ text
sample = None
for o in meshes:
    if o.name.startswith("terrain_cell_") and len(o.data.color_attributes):
        ca = o.data.color_attributes[0]
        sample = {"object": o.name, "attr": ca.name, "data_type": ca.data_type, "domain": ca.domain,
                  "first_values": [list(ca.data[i].color)[:3] for i in range(min(3, len(ca.data)))],
                  "first_values_srgb": [list(ca.data[i].color_srgb)[:3] for i in range(min(3, len(ca.data)))]}
        break
res["colour_sample"] = sample
# bounding box sanity in Blender world space
import mathutils  # noqa: E402
lo = [1e30] * 3
hi = [-1e30] * 3
for o in meshes:
    for c in o.bound_box:
        w = o.matrix_world @ mathutils.Vector(c)
        for k in range(3):
            lo[k] = min(lo[k], w[k])
            hi[k] = max(hi[k], w[k])
res["bbox"] = [lo, hi]
nm = sorted(o.name for o in meshes)
res["renamed_objects"] = [n for n in nm if "." in n][:10]
res["normals_up_fraction_terrain"] = None
up = down = 0
for o in meshes:
    if o.name.startswith("terrain_cell_"):
        for p in o.data.polygons:
            n = o.matrix_world.to_3x3() @ p.normal
            if n.z > 0.2:
                up += 1
            elif n.z < -0.2:
                down += 1
res["normals_up_fraction_terrain"] = [up, down]
with open(outjson, "w") as fh:
    json.dump(res, fh, indent=1, default=str)
