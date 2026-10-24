"""
Import the OBJ files written by tools/export_course_models.py into Blender,
one collection per file, and widen the viewport clipping so the large
landscape chunks are visible.

Use from Blender's Scripting tab (Text > Open, then Run Script), or:

    blender --python tools/blender_import_course_models.py -- [folder] [pattern]

Defaults: folder = <repo>/extracted/course_models, pattern = "*_grid.obj".
Pass "*.obj" to import everything, or a single file name.
"""
import glob
import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
DEFAULT_FOLDER = os.path.normpath(os.path.join(HERE, "..", "extracted", "course_models"))

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
folder = argv[0] if len(argv) > 0 else DEFAULT_FOLDER
pattern = argv[1] if len(argv) > 1 else "*_grid.obj"

paths = sorted(glob.glob(os.path.join(folder, pattern)))
if not paths:
    raise SystemExit(f"no OBJ files match {os.path.join(folder, pattern)}")

for path in paths:
    name = os.path.splitext(os.path.basename(path))[0]
    coll = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(coll)
    before = set(bpy.data.objects)
    # The exporter already maps PS1 axes to Blender's Z-up frame, so import as-is.
    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(filepath=path, forward_axis="Y", up_axis="Z")
    else:  # Blender < 3.3
        bpy.ops.import_scene.obj(filepath=path, axis_forward="Y", axis_up="Z")
    for obj in set(bpy.data.objects) - before:
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        coll.objects.link(obj)
    print(f"imported {path} into collection {name}")

# PS1 units are large: push the clip distance out so whole courses are visible.
for area in bpy.context.screen.areas:
    if area.type == "VIEW_3D":
        for space in area.spaces:
            if space.type == "VIEW_3D":
                space.clip_end = 1_000_000.0
                space.clip_start = 1.0
