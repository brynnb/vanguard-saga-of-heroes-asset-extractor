#!/usr/bin/env python3
"""
Extract all placed objects from a chunk as actual meshes (not box markers).
1. Parse chunk to get mesh references and positions
2. Export meshes using umodel
3. Convert to glTF and combine into a scene

TODO: This script uses umodel/wine for mesh export. Consider using the native
      extract_staticmesh.py parser instead for better reliability.
"""

import struct
import json
import os
import sys
import subprocess
import glob
from pathlib import Path

# Add parent directory to path to allow importing ue2 package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ue2.reader import read_compact_index_at as read_compact_index, read_fstring_at as read_fstring

# Import config
import config

ASSETS_PATH = config.ASSETS_PATH
UMODEL_PATH = config.UMODEL_PATH
WINE_PATH = config.WINE_PATH


# Removed duplicate read_compact_index and read_fstring functions
# Using ue2.reader versions instead


def parse_chunk(vgr_path):
    """Parse chunk file to get mesh list and object positions."""
    with open(vgr_path, "rb") as f:
        data = f.read()

    name_count = struct.unpack("<I", data[12:16])[0]
    name_offset = struct.unpack("<I", data[16:20])[0]
    export_count = struct.unpack("<I", data[20:24])[0]
    export_offset = struct.unpack("<I", data[24:28])[0]
    import_count = struct.unpack("<I", data[28:32])[0]
    import_offset = struct.unpack("<I", data[32:36])[0]

    # Read names
    names = []
    pos = name_offset
    for i in range(name_count):
        name, pos = read_fstring(data, pos)
        flags = struct.unpack("<I", data[pos : pos + 4])[0]
        pos += 4
        names.append(name)

    # Read imports
    pos = import_offset
    imports = []
    for i in range(import_count):
        class_pkg, pos = read_compact_index(data, pos)
        class_name, pos = read_compact_index(data, pos)
        package = struct.unpack("<i", data[pos : pos + 4])[0]
        pos += 4
        obj_name, pos = read_compact_index(data, pos)
        imports.append(
            {
                "class": names[class_name] if class_name < len(names) else "",
                "name": names[obj_name] if obj_name < len(names) else "",
            }
        )

    # Get StaticMesh imports
    mesh_list = [imp["name"] for imp in imports if imp["class"] == "StaticMesh"]

    # Read exports
    pos = export_offset
    exports = []
    for i in range(export_count):
        class_index, pos = read_compact_index(data, pos)
        super_index, pos = read_compact_index(data, pos)
        package = struct.unpack("<i", data[pos : pos + 4])[0]
        pos += 4
        object_name, pos = read_compact_index(data, pos)
        object_flags = struct.unpack("<I", data[pos : pos + 4])[0]
        pos += 4
        serial_size, pos = read_compact_index(data, pos)
        serial_offset = 0
        if serial_size > 0:
            serial_offset, pos = read_compact_index(data, pos)
        exports.append(
            {
                "class_index": class_index,
                "object_name": names[object_name] if object_name < len(names) else "",
                "serial_size": serial_size,
                "serial_offset": serial_offset,
            }
        )

    # Extract positions from CompoundObjects
    placements = []
    for obj in exports:
        if "CompoundObject" not in obj["object_name"]:
            continue

        offset = obj["serial_offset"]
        size = obj["serial_size"]
        raw = data[offset : offset + size]

        # TODO: BRUTE-FORCE POSITION EXTRACTION
        # This scans the raw bytes looking for 3 consecutive floats that "look like"
        # valid world coordinates. This is fragile and may:
        # - Miss positions that don't match the heuristic
        # - Pick up false positives from other float data
        # Better approach: Use universal_property_parser.py to properly parse
        # the Location property from the object's property list.
        best_pos = None
        for j in range(max(0, size - 20), size - 11):
            x = struct.unpack("<f", raw[j : j + 4])[0]
            y = struct.unpack("<f", raw[j + 4 : j + 8])[0]
            z = struct.unpack("<f", raw[j + 8 : j + 12])[0]
            # Heuristic: X/Y can be small (local offsets), but Z is usually world height (>1000)
            if abs(x) < 500000 and abs(y) < 500000 and 1000 < abs(z) < 500000 and not any(
                v != v for v in [x, y, z]
            ):
                best_pos = (x, y, z)
                break

        if best_pos:
            placements.append(
                {
                    "name": obj["object_name"],
                    "x": best_pos[0],
                    "y": best_pos[1],
                    "z": best_pos[2],
                }
            )

    return mesh_list, placements


def find_mesh_package(mesh_name):
    """Find the .usx package containing a mesh."""
    meshes_dir = os.path.join(ASSETS_PATH, "Meshes")

    # Try common package naming patterns
    # e.g., Ra5000_P1_C1_SpeedTrees_shrub001 -> Ra5000_P1_C1_SpeedTrees_mesh.usx
    parts = mesh_name.split("_")

    for i in range(len(parts), 2, -1):
        prefix = "_".join(parts[:i])
        for suffix in ["_mesh.usx", "_Mesh.usx", "_meshes.usx", "_Meshes.usx"]:
            path = os.path.join(meshes_dir, prefix + suffix)
            if os.path.exists(path):
                return path

    # Search all packages for the mesh
    for usx in glob.glob(os.path.join(meshes_dir, "*.usx")):
        # Quick check - this is slow but thorough
        try:
            result = subprocess.run(
                [WINE_PATH, UMODEL_PATH, "-list", "-game=vang", usx],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if mesh_name in result.stdout:
                return usx
        except:
            pass

    return None


def export_mesh(mesh_name, package_path, base_dir):
    """Export a single mesh using umodel."""
    # umodel exports to UmodelExport/ in the current working directory
    umodel_export_dir = os.path.join(base_dir, "UmodelExport")

    cmd = [
        WINE_PATH,
        UMODEL_PATH,
        "-export",
        "-game=vang",
        "-noanim",
        f"-path={ASSETS_PATH}",
        package_path,
        mesh_name,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, cwd=base_dir
        )

        # Find the exported .pskx file in UmodelExport/
        for root, dirs, files in os.walk(umodel_export_dir):
            for f in files:
                if f.endswith(".pskx") and mesh_name in f:
                    return os.path.join(root, f)
    except Exception as e:
        print(f"    Error exporting {mesh_name}: {e}")

    return None


def convert_psk_to_gltf(psk_path, gltf_path):
    """Convert PSK to glTF using existing converter."""
    try:
        # Use absolute path to converter script
        converter_script = os.path.join(config.RENDERER_ROOT, "converters/psk_to_gltf.py")
        result = subprocess.run(
            ["python3", converter_script, psk_path, gltf_path],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=config.RENDERER_ROOT,
        )
        return os.path.exists(gltf_path)
    except:
        return False


def main():
    chunk_x = -25
    chunk_y = 26

    if len(sys.argv) >= 3:
        chunk_x = int(sys.argv[1])
        chunk_y = int(sys.argv[2])

    vgr_path = f"{ASSETS_PATH}/Maps/chunk_n{abs(chunk_x)}_{chunk_y}.vgr"
    vgr_path = f"{ASSETS_PATH}/Maps/chunk_n{abs(chunk_x)}_{chunk_y}.vgr"
    # Output to structured chunks directory
    output_dir = os.path.join(config.CHUNKS_DIR, f"chunk_n{abs(chunk_x)}_{chunk_y}_objects")

    print(f"Processing chunk ({chunk_x}, {chunk_y})...")
    print(f"  VGR: {vgr_path}")

    if not os.path.exists(vgr_path):
        print("  ERROR: Chunk file not found")
        return

    # Parse chunk
    mesh_list, placements = parse_chunk(vgr_path)
    print(f"  Found {len(mesh_list)} unique meshes, {len(placements)} placements")

    # Export each unique mesh
    os.makedirs(output_dir, exist_ok=True)
    exported_meshes = {}

    print(f"\nExporting meshes...")
    for i, mesh_name in enumerate(mesh_list[:10]):  # Limit to first 10 for testing
        print(f"  [{i+1}/{min(len(mesh_list), 10)}] {mesh_name}...")

        # Find package
        package_path = find_mesh_package(mesh_name)
        if not package_path:
            print(f"    Package not found, skipping")
            continue

        # Export mesh (run from current directory so UmodelExport goes here)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        psk_path = export_mesh(mesh_name, package_path, script_dir)
        if not psk_path:
            print(f"    Export failed, skipping")
            continue

        # Convert to glTF
        gltf_path = os.path.join(output_dir, f"{mesh_name}.gltf")
        if convert_psk_to_gltf(psk_path, gltf_path):
            exported_meshes[mesh_name] = gltf_path
            print(f"    OK -> {gltf_path}")
        else:
            print(f"    Conversion failed")

    print(f"\nExported {len(exported_meshes)} meshes")

    # Save placement data
    placement_file = os.path.join(output_dir, "placements.json")
    with open(placement_file, "w") as f:
        json.dump(
            {
                "meshes": list(exported_meshes.keys()),
                "placements": placements,
                "chunk_x": chunk_x,
                "chunk_y": chunk_y,
            },
            f,
            indent=2,
        )
    print(f"Saved placements to {placement_file}")


if __name__ == "__main__":
    main()
