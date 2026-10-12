#!/bin/sh
# Render all shots for a .pcb3d without opening Blender.
#   ./render.sh path/to/board.pcb3d [--preview] [--shots hero,top] [--no-video] [--transparent]
# Set BLENDER if Blender isn't in /Applications:  BLENDER=/path/to/Blender ./render.sh board.pcb3d
BLENDER="${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}"
if [ ! -x "$BLENDER" ]; then
  echo "Blender not found at $BLENDER (set the BLENDER variable)" >&2
  exit 1
fi
exec "$BLENDER" -b -P "$(cd "$(dirname "$0")" && pwd)/pcb_render.py" -- "$@"
