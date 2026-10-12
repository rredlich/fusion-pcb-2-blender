"""
pcb_render.py — repeatable studio renders for .pcb3d boards (Blender + pcb2blender)

Imports a .pcb3d, measures the board, builds a studio (backdrop + lights) and
cameras sized from the board's bounding box, then renders every shot in SHOTS.
The same shots work for any board size.

Run from a terminal (no Blender window):
    blender -b -P pcb_render.py -- path/to/board.pcb3d
    blender -b -P pcb_render.py -- board.pcb3d --preview          # fast low-quality check
    blender -b -P pcb_render.py -- board.pcb3d --shots hero,top   # only some shots
    blender -b -P pcb_render.py -- board.pcb3d --no-video --transparent

Or inside Blender: Scripting tab > Open > pcb_render.py, set PCB3D_PATH below
(or import the board yourself first and leave it empty), then press Run Script.
Inside Blender it only builds the scene unless RENDER_FROM_UI is True, so you
can look through the cameras (Numpad 0) before rendering.

Output: <board folder>/renders/<board name>/<shot>.png, turntable.mp4, scene.blend
"""

import argparse
import math
import os
import sys
import time

import bpy
from mathutils import Matrix, Vector

# =====================================================================
#  SETTINGS — edit these to make the template yours
# =====================================================================

PCB3D_PATH = ""          # used when run from the Blender UI without arguments
RENDER_FROM_UI = False   # True: also render when run from the Scripting tab

RESOLUTION = (1920, 1080)
SAMPLES = 256            # stills (Cycles, denoised)
VIDEO_SAMPLES = 48       # turntable frames
PREVIEW_SCALE = 0.5      # --preview: resolution scale ...
PREVIEW_SAMPLES = 24     # ... and samples

STYLE = {
    "background": (0.80, 0.80, 0.82),   # backdrop colour (linear RGB)
    "backdrop_roughness": 0.55,
    "world_strength": 0.15,             # ambient fill from the environment
    "key_power": 1.0,                   # light multipliers (1.0 = calibrated default)
    "fill_power": 1.0,
    "rim_power": 1.0,
    "look": "AgX - Medium High Contrast",
}

# Shots. Angles are in degrees:
#   azimuth   0 = from the front (-Y), 90 = from the right (+X), -90 = from the left
#   elevation 0 = level with the board, 90 = straight down
#   lens      focal length in mm (longer = flatter, more "product shot")
#   margin    empty space around the board, as a fraction of the frame
#   fstop     depth of field (smaller = blurrier background), None = everything sharp
#   zoom      < 1 crops into part of the board (detail shots)
#   offset    (x, y) where the crop is aimed, as a fraction of the board size from
#             its centre: (0, 0) = centre, (0.3, -0.2) = right of centre, towards the front
#   flip      True renders the bottom side
#   ortho     True = orthographic camera (flat technical view)
SHOTS = [
    dict(name="hero",    azimuth=-35, elevation=32, lens=85,  margin=0.10, fstop=8),
    dict(name="top",     azimuth=0,   elevation=90, ortho=True, margin=0.06),
    dict(name="bottom",  azimuth=0,   elevation=90, ortho=True, margin=0.06, flip=True),
    dict(name="profile", azimuth=0,   elevation=10, lens=100, margin=0.08, fstop=11),
    dict(name="detail",  azimuth=25,  elevation=40, lens=135, margin=0.0,  fstop=4, zoom=0.45, offset=(0, 0)),
]

TURNTABLE = dict(enabled=True, seconds=6, fps=30, elevation=28, lens=85, margin=0.08)

TAG = "pcb_render"       # custom property marking objects this script created


# =====================================================================
#  helpers
# =====================================================================

def log(msg):
    print("[pcb_render] " + msg, flush=True)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(prog="pcb_render.py")
    p.add_argument("pcb3d", nargs="?", default=PCB3D_PATH)
    p.add_argument("--out", default="", help="output folder (default: next to the .pcb3d)")
    p.add_argument("--shots", default="", help="comma-separated shot names (default: all)")
    p.add_argument("--preview", action="store_true", help="fast low-quality renders")
    p.add_argument("--no-video", action="store_true", help="skip the turntable video")
    p.add_argument("--transparent", action="store_true", help="PNG with transparent background")
    p.add_argument("--samples", type=int, default=0)
    p.add_argument("--resolution", default="", help="e.g. 3840x2160")
    p.add_argument("--cpu", action="store_true", help="don't try to use the GPU")
    return p.parse_args(argv)


def tag(obj):
    obj[TAG] = True
    return obj


def clear_previous():
    for obj in [o for o in bpy.data.objects if o.get(TAG)]:
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in (bpy.data.meshes, bpy.data.lights, bpy.data.cameras, bpy.data.materials):
        for block in list(coll):
            if block.users == 0:
                coll.remove(block)


def reset_scene():
    # empty scene, but keep the user's preferences and enabled extensions
    bpy.ops.wm.read_homefile(use_empty=True)


def ensure_importer():
    if hasattr(bpy.ops.pcb2blender, "import_pcb3d"):
        try:
            bpy.ops.pcb2blender.import_pcb3d.get_rna_type()
            return
        except Exception:
            pass
    import addon_utils
    for mod in addon_utils.modules():
        name = mod.__name__
        if name.endswith("pcb3d_importer") or name.endswith("pcb2blender_importer"):
            addon_utils.enable(name, default_set=True)
            log("enabled " + name)
            return
    raise RuntimeError("PCB 3D Importer (pcb2blender) is not installed in this Blender. "
                       "Install it via Edit > Preferences > Get Extensions.")


def import_board(path):
    ensure_importer()
    before = set(bpy.data.objects)
    bpy.ops.pcb2blender.import_pcb3d(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    if not new:
        raise RuntimeError("the importer didn't create any objects")
    return new


def existing_board():
    objs = [o for o in bpy.context.scene.objects if not o.get(TAG)]
    if not any(o.name.startswith("PCB_") for o in objs):
        return []
    return objs


def world_bbox(objs):
    lo = Vector((math.inf,) * 3)
    hi = Vector((-math.inf,) * 3)
    for o in objs:
        if o.type != "MESH":
            continue
        for c in o.bound_box:
            p = o.matrix_world @ Vector(c)
            lo = Vector(map(min, lo, p))
            hi = Vector(map(max, hi, p))
    return lo, hi


def bbox_corners(lo, hi):
    return [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]


def make_material(name, color, roughness, metallic=0.0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic
    return mat


# =====================================================================
#  studio
# =====================================================================

def build_backdrop(size, floor_z, color, roughness):
    """Cyclorama: a floor that curves up into a back wall (behind +Y)."""
    import bmesh
    L = size * 12.0          # floor half-length / width
    r = size * 4.0           # curve radius
    wall = size * 8.0        # wall height above the curve
    back = size * 5.0        # where the curve starts (+Y)
    profile = [(-L, 0.0)]
    steps = 16
    for i in range(steps + 1):
        a = -math.pi / 2 + (math.pi / 2) * i / steps
        profile.append((back + r * math.cos(a), r + r * math.sin(a)))
    profile.append((back + r, r + wall))

    bm = bmesh.new()
    left = [bm.verts.new((-L, y, z)) for y, z in profile]
    right = [bm.verts.new((L, y, z)) for y, z in profile]
    for i in range(len(profile) - 1):
        bm.faces.new((left[i], right[i], right[i + 1], left[i + 1]))
    mesh = bpy.data.meshes.new("Backdrop")
    bm.to_mesh(mesh)
    bm.free()
    for poly in mesh.polygons:
        poly.use_smooth = True
    obj = tag(bpy.data.objects.new("Backdrop", mesh))
    obj.location.z = floor_z
    bpy.context.scene.collection.objects.link(obj)
    obj.data.materials.append(make_material("Backdrop", color, roughness))
    return obj


def add_area_light(name, center, size, azimuth, elevation, dist_factor, power, light_size, color=(1, 1, 1)):
    d = size * dist_factor
    direction = sph(azimuth, elevation)
    data = bpy.data.lights.new(name, "AREA")
    data.shape = "DISK"
    data.size = size * light_size
    data.color = color
    # irradiance falls off with d^2, so scale power with d^2 to keep exposure
    # identical for any board size (calibrated at d = 1 m -> power W)
    data.energy = power * d * d
    obj = tag(bpy.data.objects.new(name, data))
    obj.location = center + direction * d
    obj.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.collection.objects.link(obj)
    return obj


def build_lights(center, size):
    s = STYLE
    add_area_light("Key",  center, size, -55, 50, 6.0, 60 * s["key_power"],  3.0, (1.0, 0.97, 0.92))
    add_area_light("Fill", center, size,  70, 25, 7.0, 18 * s["fill_power"], 4.0, (0.92, 0.96, 1.0))
    add_area_light("Rim",  center, size, 165, 35, 6.0, 45 * s["rim_power"],  2.0)


def setup_world(color, strength):
    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = next((n for n in world.node_tree.nodes if n.type == "BACKGROUND"), None)
    if bg is None:
        bg = world.node_tree.nodes.new("ShaderNodeBackground")
        out = next(n for n in world.node_tree.nodes if n.type == "OUTPUT_WORLD")
        world.node_tree.links.new(bg.outputs[0], out.inputs[0])
    bg.inputs["Color"].default_value = (*color, 1.0)
    bg.inputs["Strength"].default_value = strength


# =====================================================================
#  cameras
# =====================================================================

def sph(azimuth, elevation):
    az, el = math.radians(azimuth), math.radians(elevation)
    return Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))


def look_rotation(direction, elevation):
    if elevation >= 89.9:      # straight down: keep the board's X axis horizontal
        return Matrix.Rotation(0.0, 4, "Z").to_euler()
    return (-direction).to_track_quat("-Z", "Y").to_euler()


def fit_camera(cam, points, center, margin):
    """Place cam (rotation already set) so all points fit in frame, plus margin."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    flat = [c for p in points for c in p]
    loc, scale = cam.camera_fit_coords(depsgraph, flat)
    if cam.data.type == "ORTHO":
        cam.data.ortho_scale = scale / max(1e-6, 1.0 - 2 * margin)
        back = cam.matrix_world.to_quaternion() @ Vector((0, 0, 1))
        cam.location = loc + back * (scale * 2.0)
    else:
        back = cam.matrix_world.to_quaternion() @ Vector((0, 0, 1))
        dist = (loc - center).length
        cam.location = loc + back * dist * (margin * 2.2)
    cam.data.clip_start = max(1e-4, scale * 0.001)
    cam.data.clip_end = scale * 1000 + 100


def make_camera(name):
    data = bpy.data.cameras.new(name)
    data.sensor_width = 36.0
    obj = tag(bpy.data.objects.new(name, data))
    bpy.context.scene.collection.objects.link(obj)
    return obj


def setup_shot_camera(shot, objs, focus):
    lo, hi = world_bbox(objs)
    center = (lo + hi) / 2
    ox, oy = shot.get("offset", (0, 0))
    center = center + Vector(((hi.x - lo.x) * ox, (hi.y - lo.y) * oy, 0))
    cam = make_camera("CAM_" + shot["name"])
    direction = sph(shot.get("azimuth", 0), shot.get("elevation", 30))
    cam.rotation_euler = look_rotation(direction, shot.get("elevation", 30))
    cam.location = center + direction * (hi - lo).length * 3
    if shot.get("ortho"):
        cam.data.type = "ORTHO"
    else:
        cam.data.lens = shot.get("lens", 85)
    bpy.context.view_layer.update()

    zoom = shot.get("zoom", 1.0)
    if zoom < 1.0:   # crop: fit a smaller box around the centre of the board
        half = (hi - lo) / 2 * zoom
        half.z = (hi.z - lo.z) / 2
        pts = bbox_corners(center - half, center + half)
    else:
        pts = bbox_corners(lo, hi)
    fit_camera(cam, pts, center, shot.get("margin", 0.08))

    if shot.get("fstop") and not shot.get("ortho"):
        cam.data.dof.use_dof = True
        focus.matrix_world = Matrix.Translation(center)
        cam.data.dof.focus_object = focus
        cam.data.dof.aperture_fstop = shot["fstop"]
    return cam


# =====================================================================
#  render
# =====================================================================

def setup_render(args):
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.use_denoising = True
    scene.cycles.use_adaptive_sampling = True
    scene.render.film_transparent = args.transparent
    scale = PREVIEW_SCALE if args.preview else 1.0
    res = RESOLUTION
    if args.resolution:
        w, h = args.resolution.lower().split("x")
        res = (int(w), int(h))
    scene.render.resolution_x = int(res[0] * scale)
    scene.render.resolution_y = int(res[1] * scale)
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA" if args.transparent else "RGB"
    scene.view_settings.view_transform = "AgX"
    for look in (STYLE["look"], STYLE["look"].replace("AgX - ", ""), "None"):
        try:
            scene.view_settings.look = look
            break
        except TypeError:
            continue

    if not args.cpu:
        try:
            prefs = bpy.context.preferences.addons["cycles"].preferences
            for kind in ("METAL", "OPTIX", "CUDA", "HIP", "ONEAPI"):
                try:
                    prefs.compute_device_type = kind
                except TypeError:
                    continue
                prefs.get_devices()
                gpus = [d for d in prefs.devices if d.type != "CPU"]
                if gpus:
                    for d in prefs.devices:
                        d.use = True
                    scene.cycles.device = "GPU"
                    log("rendering on %s (%s)" % (kind, ", ".join(d.name for d in gpus)))
                    break
            else:
                log("no GPU found, rendering on CPU")
        except Exception as e:
            log("GPU setup failed (%s), rendering on CPU" % e)


def set_samples(n):
    bpy.context.scene.cycles.samples = n


def render_still(cam, path):
    scene = bpy.context.scene
    scene.camera = cam
    scene.render.filepath = path
    t = time.time()
    bpy.ops.render.render(write_still=True)
    log("  %s (%.0fs)" % (os.path.basename(path), time.time() - t))


def set_video_output(path, fps):
    scene = bpy.context.scene
    scene.render.fps = fps
    scene.render.filepath = path
    img = scene.render.image_settings
    if hasattr(img, "media_type"):          # Blender 5.x
        img.media_type = "VIDEO"
    img.file_format = "FFMPEG"
    ff = scene.render.ffmpeg
    ff.format = "MPEG4"
    ff.codec = "H264"
    ff.constant_rate_factor = "HIGH"
    ff.ffmpeg_preset = "GOOD"
    scene.render.film_transparent = False


# =====================================================================
#  main
# =====================================================================

def main():
    args = parse_args()
    in_ui = not bpy.app.background
    path = bpy.path.abspath(args.pcb3d) if args.pcb3d else ""

    if path:
        if not os.path.isfile(path):
            raise RuntimeError("file not found: " + path)
        reset_scene()
        objs = import_board(path)
        board_name = os.path.splitext(os.path.basename(path))[0]
        base_dir = os.path.dirname(path)
    else:
        clear_previous()
        objs = existing_board()
        if not objs:
            raise RuntimeError("no .pcb3d given and no imported board (PCB_*) in the scene")
        board_name = next(o.name for o in objs if o.name.startswith("PCB_"))[4:]
        base_dir = os.path.dirname(bpy.data.filepath) or os.path.expanduser("~")
    out_dir = args.out or os.path.join(base_dir, "renders", board_name)
    os.makedirs(out_dir, exist_ok=True)

    scene = bpy.context.scene
    bpy.context.view_layer.update()
    lo, hi = world_bbox(objs)
    center = (lo + hi) / 2
    size = max(hi.x - lo.x, hi.y - lo.y)          # board footprint, metres
    log("board %s: %.1f x %.1f x %.1f mm" % (board_name, (hi.x - lo.x) * 1000,
                                               (hi.y - lo.y) * 1000, (hi.z - lo.z) * 1000))

    # pivot for flipping / turntable, at the board centre
    root = tag(bpy.data.objects.new("PCB_ROOT", None))
    root.location = center
    scene.collection.objects.link(root)
    bpy.context.view_layer.update()
    for o in objs:
        if o.parent is None:
            mw = o.matrix_world.copy()
            o.parent = root
            o.matrix_world = mw
    focus = tag(bpy.data.objects.new("FOCUS", None))
    focus.parent = root
    scene.collection.objects.link(focus)

    setup_world(STYLE["background"], STYLE["world_strength"])
    build_lights(center, size)
    backdrop = build_backdrop(size, lo.z - size * 0.0005, STYLE["background"], STYLE["backdrop_roughness"])
    if args.transparent:
        backdrop.is_shadow_catcher = True
    setup_render(args)

    wanted = [s.strip() for s in args.shots.split(",") if s.strip()]
    shots = [s for s in SHOTS if not wanted or s["name"] in wanted]
    do_video = TURNTABLE["enabled"] and not args.no_video and (not wanted or "turntable" in wanted)
    do_render = (not in_ui) or RENDER_FROM_UI
    samples = args.samples or (PREVIEW_SAMPLES if args.preview else SAMPLES)

    # ---- stills ----
    cams = []
    for shot in shots:
        root.rotation_euler = (math.pi if shot.get("flip") else 0.0, 0.0, 0.0)
        bpy.context.view_layer.update()
        slo, shi = world_bbox(objs)
        backdrop.location.z = slo.z - size * 0.0005
        cam = setup_shot_camera(shot, objs, focus)
        cams.append(cam)
        if do_render:
            set_samples(samples)
            render_still(cam, os.path.join(out_dir, shot["name"] + ".png"))
    root.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    backdrop.location.z = lo.z - size * 0.0005

    # ---- turntable ----
    if TURNTABLE["enabled"]:
        t = TURNTABLE
        frames = int(t["seconds"] * t["fps"])
        scene.frame_start, scene.frame_end = 1, frames
        root.rotation_euler = (0, 0, 0)
        root.keyframe_insert("rotation_euler", index=2, frame=1)
        root.rotation_euler = (0, 0, 2 * math.pi * frames / (frames + 1))
        root.keyframe_insert("rotation_euler", index=2, frame=frames)
        try:
            fcurves = root.animation_data.action.fcurves
        except AttributeError:      # Blender 5 layered actions
            fcurves = root.animation_data.action.layers[0].strips[0].channelbags[0].fcurves
        for fc in fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
        root.rotation_euler = (0, 0, 0)

        cam = make_camera("CAM_turntable")
        direction = sph(-30, t["elevation"])
        cam.rotation_euler = look_rotation(direction, t["elevation"])
        cam.data.lens = t["lens"]
        cam.location = center + direction * size * 3
        bpy.context.view_layer.update()
        pts = []
        for i in range(16):   # fit the board at every angle of the spin
            rot = Matrix.Rotation(2 * math.pi * i / 16, 4, "Z")
            pts += [center + rot @ (p - center) for p in bbox_corners(lo, hi)]
        fit_camera(cam, pts, center, t["margin"])
        cams.append(cam)
        if do_render and do_video:
            set_samples(min(samples, VIDEO_SAMPLES))
            scene.camera = cam
            set_video_output(os.path.join(out_dir, "turntable.mp4"), t["fps"])
            log("turntable: %d frames ..." % frames)
            tt = time.time()
            bpy.ops.render.render(animation=True)
            log("  turntable.mp4 (%.0fs)" % (time.time() - tt))

    scene.frame_set(1)
    scene.camera = cams[0] if cams else None
    blend = os.path.join(out_dir, "scene.blend")
    if not in_ui:
        bpy.ops.wm.save_as_mainfile(filepath=blend, compress=True)
    log("done -> " + out_dir)


if __name__ == "__main__":
    main()
