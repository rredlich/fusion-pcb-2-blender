# Fusion PCB to Blender (.pcb3d exporter)

Export PCBs designed in **Autodesk Fusion (Electronics)** as `.pcb3d` files. These open directly in Blender with the **pcb2blender** importer (PCB 3D Importer) and include the board, its copper, solder mask, paste and silkscreen, all 3D components, and automatic solder joints.

pcb2blender was written for KiCad. This project replaces its KiCad exporter with two Fusion tools that write the same file format.

| File | Runs in | What it does |
|---|---|---|
| `export-pcb3d.ulp` | Fusion PCB editor (ULP) | Writes the board mesh (outline, cutouts, drill holes), the 8 layer images (F/B copper, paste, silkscreen, mask), the stackup (thickness, colours, finish) and the pad data used for solder joints. |
| `AddComponentsToPCB3D.py` | Fusion Design workspace (Python script) | Meshes the 3D models of every part on the board and adds them to the same `.pcb3d` file, with each part's Fusion appearance as its material. |

The work is split between two tools because Fusion's ULP language can read the PCB layout but not the 3D models, and Fusion's Python API can read the 3D models but not the PCB layout.

## Dependencies

- **Autodesk Fusion** with the Electronics workspace (ULP + Python script support)
- **Blender 4.5 LTS or 5.1**
- **PCB 3D Importer** Blender extension (pcb2blender importer)

## Install

### 1. ULP (Fusion PCB editor)
1. Copy `export-pcb3d.ulp` into a permanent folder, e.g. `Documents/Fusion ULPs`.
2. Open your profile icon → **Preferences → Electronics → Directory**.
3. In the **ULP** field, add your folder at the end of the existing path. On macOS, separate paths with `:`:
   ```
   <existing path>:/Users/<you>/Documents/Fusion ULPs
   ```
   Add to the path rather than replacing it, so Fusion's built-in ULPs stay available.
4. Click **OK**.

### 2. Python script (Fusion Design workspace)
1. Copy the `AddComponentsToPCB3D` folder somewhere permanent. Don't rename the folder or its files.
2. In a **normal design**, open **Utilities → Add-Ins → Scripts and Add-Ins** (`Shift + S`).
3. Click **+**, add a script from your computer, and select the `AddComponentsToPCB3D` folder.

### 3. Blender
In Blender, go to **Edit → Preferences → Get Extensions**, search for **PCB 3D Importer** and install it.

On Apple Silicon, set **Preferences → System → Cycles Render Devices** to **Metal** for much faster renders.

## Run

### 1. Export the board (PCB editor)
1. Open the board in the **PCB editor** (the 2D layout).
2. On the **Automation** tab, click **Run ULP**, then double-click **export-pcb3d**.
3. Set the board thickness, solder mask colour, silkscreen colour and surface finish, choose where to save, and click **Export**.
4. If Fusion asks whether the ULP may run a system command, allow it. That step zips the files into the `.pcb3d`.

### 2. Add the components (Design workspace)
The 3D PCB document doesn't offer Scripts and Add-Ins, so run the script from a normal design that contains the board:

1. Create a new design, e.g. `MyBoard – render`.
2. In the **Data Panel**, right-click the board's **3D PCB** and choose **Insert into Current Design**. Don't move or rotate it.
3. Open **Scripts and Add-Ins** (`Shift + S`), run **AddComponentsToPCB3D**, and pick the `.pcb3d` from step 1.
4. When it finishes, a message reports how many components were added. It also warns if the board thickness differs from the ULP setting.

The inserted board stays linked to the PCB. After a board change, update the design, re-run the ULP, then re-run the script. Re-running the script replaces the components it added before rather than duplicating them.

### 3. Import into Blender
Go to **File → Import → PCB (.pcb3d)**. Import into a clean scene, or delete the previous board first.

## Known limitations

- **SMD mask and paste sizes are approximate.** The mask opening is the pad plus 0.1 mm per side, and the paste equals the pad. Fusion doesn't allow per-layer size lookups for pads on bottom-side parts.
- **Bottom-side SMD detection** relies on the part being placed on the bottom (mirrored). Footprints that define SMD pads on the Bottom layer in the library may end up on the wrong side.
- **Only visible solid bodies are exported.** Every visible body other than the board and its layers is treated as a component. Hide enclosures or other parts you don't want in the export.
- **Per-face colours** set inside a part are not exported. Each body uses its main appearance.
- **Windows is not supported** by the ULP's packing step, which needs `/bin/sh` and `zip`.

## Credits

- **[pcb2blender](https://github.com/30350n/pcb2blender)** by [30350n](https://github.com/30350n), GPL-3.0. This project re-implements its KiCad exporter for Fusion and writes its `.pcb3d` format. The Blender importer, PCB shader, solder joints and mat4cad materials are all pcb2blender's work.
- **[earcut](https://github.com/mapbox/earcut)** by Mapbox, ISC licence. The ULP contains a port of earcut 2.2.4 to triangulate the board outline with its holes:
  > Copyright (c) 2016, Mapbox
  >
  > Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.
  >
  > THE SOFTWARE IS PROVIDED "AS IS" AND ISC DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL ISC BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
