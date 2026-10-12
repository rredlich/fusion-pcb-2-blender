"""
AddComponentsToPCB3D — Fusion 360 script

Adds the 3D component models of a Fusion 3D PCB to a .pcb3d file created by
export-pcb3d.ulp, so the pcb2blender Blender importer places every part on
the board (and adds solder joints), exactly like the KiCad workflow.

Workflow
  1. PCB editor:  Automation > Run ULP > export-pcb3d.ulp   -> board.pcb3d
  2. 3D PCB:      Utilities > Scripts and Add-Ins > AddComponentsToPCB3D
                  -> pick board.pcb3d, components are added in place
  3. Blender:     File > Import > PCB (.pcb3d)

How it works
  * Finds the board body in the active design by matching its size to the
    outline stored in the .pcb3d, and uses it to align Fusion's coordinates
    with the ULP's board coordinates (handles Y-up / Z-up designs).
  * Meshes every other visible body, grouped per occurrence, and writes one
    VRML file per part into components/, plus Transform/Inline entries in
    pcb.wrl in the layout pcb2blender's importer expects.
  * Each body's Fusion appearance becomes its material (name + colour).
    Running the script again replaces the previously added components.
"""

import math
import os
import re
import traceback
import zipfile

import adsk.core
import adsk.fusion

MARKER = "# --- pcb2blender-fusion components ---"
COMPONENT_UNIT_MM = 2.54          # pcb2blender's component file unit (0.1 inch)
INSTANCE_SCALE = COMPONENT_UNIT_MM / 1000.0
SIZE_TOLERANCE_MM = 1.0           # board body vs. ULP outline
COLOR_PROPERTY_IDS = (
    "opaque_albedo", "metal_f0", "transparent_color", "generic_diffuse",
    "layered_diffuse", "solidglass_transmittance", "plasticvinyl_color",
)

_app = None
_ui = None


# ---------------------------------------------------------------- helpers

def sanitized(name):
    name = re.sub(r"\W+", "_", name).strip("_")
    return name or "part"


def read_floats(text, key):
    match = re.search(r"^\s*" + key + r"\s*=\s*\[([^\]]*)\]", text, re.M)
    if not match:
        return None
    return [float(v) for v in match.group(1).split(",")]


def read_float(text, key, default):
    match = re.search(r"^\s*" + key + r"\s*=\s*([-0-9.eE+]+)", text, re.M)
    return float(match.group(1)) if match else default


def axis_map(thin_axis):
    """Returns f(x, y, z) -> (x, y, z) that puts the board plane on XY, normal +Z."""
    if thin_axis == 2:
        return lambda x, y, z: (x, y, z)
    if thin_axis == 1:                       # Y-up design: board on XZ, top = +Y
        return lambda x, y, z: (x, -z, y)
    return lambda x, y, z: (y, z, x)         # board on YZ (unusual)


def bbox_mm(body):
    bb = body.boundingBox
    return (
        (bb.minPoint.x * 10, bb.minPoint.y * 10, bb.minPoint.z * 10),
        (bb.maxPoint.x * 10, bb.maxPoint.y * 10, bb.maxPoint.z * 10),
    )


def mapped_bbox(bbox, fmap):
    (x0, y0, z0), (x1, y1, z1) = bbox
    corners = [fmap(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    lo = tuple(min(c[i] for c in corners) for i in range(3))
    hi = tuple(max(c[i] for c in corners) for i in range(3))
    return lo, hi


def iter_bodies(root):
    """(occurrence or None, proxy body) for every visible solid body."""
    for body in root.bRepBodies:
        if body.isVisible and body.isSolid:
            yield None, body
    for occ in root.allOccurrences:
        if not occ.isVisible:
            continue
        for body in occ.bRepBodies:
            if body.isVisible and body.isSolid:
                yield occ, body


def appearance_info(body):
    name, color = "Default", (0.6, 0.6, 0.6)
    try:
        ap = body.appearance
        if not ap:
            return name, color
        name = ap.name or name
        props = ap.appearanceProperties
        value = None
        for pid in COLOR_PROPERTY_IDS:
            prop = adsk.core.ColorProperty.cast(props.itemById(pid))
            if prop and prop.value:
                value = prop.value
                break
        if value is None:
            for i in range(props.count):
                prop = adsk.core.ColorProperty.cast(props.item(i))
                if prop and prop.value:
                    value = prop.value
                    break
        if value is not None:
            color = (value.red / 255.0, value.green / 255.0, value.blue / 255.0)
    except Exception:
        pass
    return name, color


def mesh_body(body, occ, quality):
    """Triangle mesh of a body in root-space millimetres: (flat xyz list, flat index list)."""
    calc = body.meshManager.createMeshCalculator()
    calc.setQuality(quality)
    mesh = calc.calculate()
    pts = [v * 10.0 for v in mesh.nodeCoordinatesAsFloat]
    idx = list(mesh.nodeIndices)
    if not pts:
        return pts, idx

    # Proxy meshes should already be in root space; verify against the body's
    # root-space bounding box and fall back to applying the occurrence transform.
    (bx0, by0, bz0), (bx1, by1, bz1) = bbox_mm(body)

    def fits(p):
        xs, ys, zs = p[0::3], p[1::3], p[2::3]
        return (abs(min(xs) - bx0) < 0.1 and abs(max(xs) - bx1) < 0.1 and
                abs(min(ys) - by0) < 0.1 and abs(max(ys) - by1) < 0.1 and
                abs(min(zs) - bz0) < 0.1 and abs(max(zs) - bz1) < 0.1)

    if occ is not None and not fits(pts):
        m = occ.transform2.asArray()
        moved = []
        for i in range(0, len(pts), 3):
            x, y, z = pts[i] / 10.0, pts[i + 1] / 10.0, pts[i + 2] / 10.0
            moved += [
                (m[0] * x + m[1] * y + m[2] * z + m[3]) * 10.0,
                (m[4] * x + m[5] * y + m[6] * z + m[7]) * 10.0,
                (m[8] * x + m[9] * y + m[10] * z + m[11]) * 10.0,
            ]
        if fits(moved):
            pts = moved
    return pts, idx


# ---------------------------------------------------------------- VRML

def component_wrl(parts, fmap, offset):
    """parts: list of (appearance name, rgb, pts_mm, indices) -> VRML text."""
    out = ["#VRML V2.0 utf8"]
    defined = set()
    ox, oy, oz = offset
    for ap_name, (r, g, b), pts, idx in parts:
        def_name = "FUS_" + sanitized(ap_name)
        if def_name in defined:
            material = "material USE %s" % def_name
        else:
            defined.add(def_name)
            material = "material DEF %s Material { diffuseColor %.4f %.4f %.4f }" % (def_name, r, g, b)
        out.append("Shape {")
        out.append("  appearance Appearance { %s }" % material)
        out.append("  geometry IndexedFaceSet {")
        out.append("    solid FALSE")
        out.append("    coord Coordinate { point [")
        for i in range(0, len(pts), 3):
            x, y, z = fmap(pts[i], pts[i + 1], pts[i + 2])
            out.append("%.5f %.5f %.5f," % (
                (x + ox) / COMPONENT_UNIT_MM, (y + oy) / COMPONENT_UNIT_MM, (z + oz) / COMPONENT_UNIT_MM))
        out.append("    ] }")
        out.append("    coordIndex [")
        for i in range(0, len(idx), 3):
            out.append("%d,%d,%d,-1," % (idx[i], idx[i + 1], idx[i + 2]))
        out.append("    ]")
        out.append("  }")
        out.append("}")
    return "\n".join(out) + "\n"


def pcb_wrl_with_components(pcb_wrl, names):
    base = pcb_wrl.split(MARKER)[0].rstrip() + "\n"
    if not names:
        return base
    lines = [MARKER, "Transform {", "  children ["]
    for name in names:
        lines.append("Transform {")
        lines.append("scale %.5f %.5f %.5f" % (INSTANCE_SCALE, INSTANCE_SCALE, INSTANCE_SCALE))
        lines.append('children [ Inline { url "components/%s.wrl" } ]' % name)
        lines.append("}")
    lines += ["  ]", "}"]
    return base + "\n".join(lines) + "\n"


def rewrite_pcb3d(path, pcb_wrl, components):
    tmp = path + ".tmp"
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        has_dir = False
        for item in zin.infolist():
            name = item.filename
            if name == "components/":
                has_dir = True
            elif name.startswith("components/") or name == "pcb.wrl":
                continue
            zout.writestr(item, zin.read(name))
        if not has_dir:
            zout.writestr("components/", "")
        zout.writestr("pcb.wrl", pcb_wrl)
        for name, text in components:
            zout.writestr("components/%s.wrl" % name, text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        design = adsk.fusion.Design.cast(_app.activeProduct)
        if not design:
            _ui.messageBox("Open the board's 3D PCB design first, then run this script again.")
            return

        dlg = _ui.createFileDialog()
        dlg.title = "Select the .pcb3d exported by export-pcb3d.ulp"
        dlg.filter = "pcb2blender files (*.pcb3d)"
        if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
            return
        path = dlg.filename

        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "pcb.wrl" not in names or "layers/bounds.toml" not in names:
                _ui.messageBox("This doesn't look like a .pcb3d file from export-pcb3d.ulp.")
                return
            pcb_wrl = z.read("pcb.wrl").decode("utf-8")
            bounds = z.read("layers/bounds.toml").decode("utf-8")
            stackup = z.read("layers/stackup.toml").decode("utf-8") if "layers/stackup.toml" in names else ""

        # board outline from the ULP (SVG bounds are KiCad coords with a 1 mm margin)
        top_left, size = read_floats(bounds, "top_left"), read_floats(bounds, "size")
        min_x = top_left[0] + 1.0
        max_y = -(top_left[1] + 1.0)
        width, height = size[0] - 2.0, size[1] - 2.0
        min_y = max_y - height
        thickness = read_float(stackup, "thickness_mm", 1.6)

        # ---- find the board body and the axis mapping ----
        root = design.rootComponent
        bodies = list(iter_bodies(root))
        best = None
        for occ, body in bodies:
            bb = bbox_mm(body)
            ext = [bb[1][i] - bb[0][i] for i in range(3)]
            thin = ext.index(min(ext))
            if ext[thin] > 5.0:
                continue
            fmap = axis_map(thin)
            lo, hi = mapped_bbox(bb, fmap)
            score = abs((hi[0] - lo[0]) - width) + abs((hi[1] - lo[1]) - height)
            if best is None or score < best[0]:
                best = (score, body, fmap, lo, hi)

        if best is None or best[0] > 2 * SIZE_TOLERANCE_MM:
            found = ""
            if best is not None:
                found = "\nClosest body: %.2f x %.2f mm." % (best[4][0] - best[3][0], best[4][1] - best[3][1])
            _ui.messageBox(
                "Couldn't find the board body in this design.\n"
                "Expected a board of %.2f x %.2f mm (from the .pcb3d).%s\n\n"
                "Make sure the 3D PCB design is active, the board is visible and not "
                "rotated by 90 degrees, and the .pcb3d is from the same board."
                % (width, height, found))
            return

        _, board_body, fmap, b_lo, b_hi = best
        offset = (min_x - b_lo[0], min_y - b_lo[1], -(b_lo[2] + b_hi[2]) / 2.0)
        board_thickness = b_hi[2] - b_lo[2]
        b_w, b_h = b_hi[0] - b_lo[0], b_hi[1] - b_lo[1]

        # ---- collect component bodies, grouped per occurrence ----
        groups = {}
        order = []
        skipped_layers = 0
        for occ, body in bodies:
            if body == board_body:
                continue
            lo, hi = mapped_bbox(bbox_mm(body), fmap)
            in_slab = lo[2] >= b_lo[2] - 0.05 and hi[2] <= b_hi[2] + 0.05
            board_sized = (hi[0] - lo[0]) > 0.5 * b_w and (hi[1] - lo[1]) > 0.5 * b_h
            if in_slab and board_sized:      # copper / mask / silk layer bodies
                skipped_layers += 1
                continue
            key = occ.fullPathName if occ is not None else "root"
            if key not in groups:
                groups[key] = (occ, [])
                order.append(key)
            groups[key][1].append(body)

        if not order:
            _ui.messageBox("No component bodies found next to the board.")
            return

        progress = _ui.createProgressDialog()
        progress.isCancelButtonShown = True
        progress.show("Adding components to .pcb3d", "Meshing part %v of %m", 0, len(order))

        quality = adsk.fusion.TriangleMeshQualityOptions.HighQualityTriangleMesh
        components, used = [], set()
        triangles = 0
        for n, key in enumerate(order):
            if progress.wasCancelled:
                progress.hide()
                return
            progress.progressValue = n
            adsk.doEvents()

            occ, group_bodies = groups[key]
            parts = []
            for body in group_bodies:
                pts, idx = mesh_body(body, occ, quality)
                if not idx:
                    continue
                ap_name, color = appearance_info(body)
                parts.append((ap_name, color, pts, idx))
                triangles += len(idx) // 3
            if not parts:
                continue

            name = sanitized(occ.name if occ is not None else "root")
            base, i = name, 1
            while name in used:
                i += 1
                name = "%s_%d" % (base, i)
            used.add(name)
            components.append((name, component_wrl(parts, fmap, offset)))
        progress.hide()

        rewrite_pcb3d(path, pcb_wrl_with_components(pcb_wrl, [c[0] for c in components]), components)

        msg = "Added %d components (%d triangles) to:\n%s" % (len(components), triangles, path)
        if abs(board_thickness - thickness) > 0.1:
            msg += ("\n\nNote: the board in Fusion is %.2f mm thick but the .pcb3d says %.2f mm. "
                    "Re-run the ULP with %.2f mm so parts sit flush on the board."
                    % (board_thickness, thickness, board_thickness))
        if skipped_layers:
            msg += "\n\nSkipped %d board-layer bodies (copper/mask/silk)." % skipped_layers
        msg += "\n\nIn Blender: File > Import > PCB (.pcb3d)."
        _ui.messageBox(msg)

    except Exception:
        if _ui:
            _ui.messageBox("Failed:\n{}".format(traceback.format_exc()))


def stop(context):
    pass
