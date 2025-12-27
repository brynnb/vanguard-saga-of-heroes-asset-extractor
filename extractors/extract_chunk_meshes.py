#!/usr/bin/env python3
"""
Automatically extract all meshes referenced by a chunk.
1. Parse chunk to get all StaticMesh imports
2. Find the .usx package for each mesh
3. Export using umodel
4. Convert to glTF
"""

import struct
import os
import sys
import subprocess
import glob
from pathlib import Path

ASSETS_PATH = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets"
MESHES_PATH = os.path.join(ASSETS_PATH, "Meshes")
UMODEL_PATH = "/Users/brynnbateman/Documents/GitHub/project-telon/renderer/gildor2-UEViewer-daa3c14/umodel.exe"
WINE_PATH = "/Applications/Wine Stable.app/Contents/Resources/wine/bin/wine"
OUTPUT_DIR = (
    "/Users/brynnbateman/Documents/GitHub/project-telon/renderer/output/buildings"
)


def read_compact_index(data, pos):
    b0 = data[pos]
    pos += 1
    negative = b0 & 0x80
    value = b0 & 0x3F
    if b0 & 0x40:
        b1 = data[pos]
        pos += 1
        value |= (b1 & 0x7F) << 6
        if b1 & 0x80:
            b2 = data[pos]
            pos += 1
            value |= (b2 & 0x7F) << 13
    return (-value if negative else value), pos


def read_fstring(data, pos):
    length, pos = read_compact_index(data, pos)
    if length < 0:
        length = -length
        result = (
            data[pos : pos + length * 2]
            .decode("utf-16-le", errors="replace")
            .rstrip("\x00")
        )
        pos += length * 2
    else:
        result = (
            data[pos : pos + length].decode("latin-1", errors="replace").rstrip("\x00")
        )
        pos += length
    return result, pos


def get_chunk_mesh_imports(vgr_path, skip_vegetation=True):
    """Parse chunk and return list of StaticMesh names referenced."""
    with open(vgr_path, "rb") as f:
        data = f.read()

    name_count = struct.unpack("<I", data[12:16])[0]
    name_offset = struct.unpack("<I", data[16:20])[0]
    import_count = struct.unpack("<I", data[28:32])[0]
    import_offset = struct.unpack("<I", data[32:36])[0]

    # Read names
    names = []
    pos = name_offset
    for i in range(name_count):
        name, pos = read_fstring(data, pos)
        pos += 4
        names.append(name)

    # Read imports
    pos = import_offset
    mesh_names = []

    tree_terms = [
        "tree",
        "shrub",
        "bush",
        "fir",
        "oak",
        "aspen",
        "spruce",
        "beech",
        "yew",
        "maple",
        "bristle",
        "flower",
        "mushroom",
        "grass",
        "corn",
        "cattail",
        "speedtree",
    ]

    for i in range(import_count):
        class_pkg, pos = read_compact_index(data, pos)
        class_name, pos = read_compact_index(data, pos)
        pos += 4
        obj_name, pos = read_compact_index(data, pos)

        cls = names[class_name] if 0 <= class_name < len(names) else ""
        name = names[obj_name] if 0 <= obj_name < len(names) else ""

        if cls == "StaticMesh":
            if skip_vegetation and any(t in name.lower() for t in tree_terms):
                continue
            mesh_names.append(name)

    return mesh_names


def find_mesh_package(mesh_name):
    """Find the .usx package containing a mesh by searching package contents."""
    parts = mesh_name.split("_")

    # Try common naming patterns
    for i in range(len(parts), 1, -1):
        prefix = "_".join(parts[:i])
        for suffix in ["_mesh.usx", "_Mesh.usx", "_meshes.usx", "_Meshes.usx"]:
            path = os.path.join(MESHES_PATH, prefix + suffix)
            if os.path.exists(path):
                return path

    # Try case-insensitive glob search
    for i in range(min(4, len(parts)), 1, -1):
        prefix = "_".join(parts[:i])
        pattern = os.path.join(MESHES_PATH, f"{prefix}*[Mm]esh*.usx")
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
        # Try lowercase
        pattern = os.path.join(MESHES_PATH, f"{prefix.lower()}*[Mm]esh*.usx")
        matches = glob.glob(pattern)
        if matches:
            return matches[0]

    # Special cases - some meshes have different package naming
    # e.g., Ra5000_P1_C1_Rocks_boulder002 -> Ra5000_P1_C1_Rocks_Mesh.usx
    for i in range(len(parts) - 1, 2, -1):
        prefix = "_".join(parts[:i])
        for suffix in ["_mesh.usx", "_Mesh.usx", "_meshes.usx", "_Meshes.usx"]:
            pattern = os.path.join(MESHES_PATH, f"{prefix}*{suffix}")
            matches = glob.glob(pattern)
            if matches:
                return matches[0]

    return None


def export_mesh(mesh_name, package_path):
    """Export a mesh using umodel, return path to .pskx if successful."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    umodel_export_dir = os.path.join(script_dir, "UmodelExport")

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
            cmd, capture_output=True, text=True, timeout=30, cwd=script_dir
        )

        # Find exported .pskx
        for root, dirs, files in os.walk(umodel_export_dir):
            for f in files:
                if f.endswith(".pskx") and mesh_name in f:
                    return os.path.join(root, f)
    except Exception as e:
        pass

    return None


def convert_to_gltf(psk_path, output_path):
    """Convert .pskx to .gltf"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["python3", "psk_to_gltf.py", psk_path, output_path],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=script_dir,
        )
        return os.path.exists(output_path)
    except:
        return False


def main():
    chunk_x = -23
    chunk_y = 27

    if len(sys.argv) >= 3:
        chunk_x = int(sys.argv[1])
        chunk_y = int(sys.argv[2])

    vgr_path = f"{ASSETS_PATH}/Maps/chunk_n{abs(chunk_x)}_{chunk_y}.vgr"

    print(f"=== Extracting meshes for chunk ({chunk_x}, {chunk_y}) ===")
    print(f"VGR: {vgr_path}")

    if not os.path.exists(vgr_path):
        print("ERROR: Chunk file not found")
        return

    # Get all mesh references
    mesh_names = get_chunk_mesh_imports(vgr_path, skip_vegetation=True)
    print(f"\nFound {len(mesh_names)} non-vegetation mesh references")

    # Check which we already have
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    existing = set(f[:-5] for f in os.listdir(OUTPUT_DIR) if f.endswith(".gltf"))

    needed = []
    for name in mesh_names:
        # Check exact match or LOD variants
        if name in existing:
            continue
        base = name.split("_L")[0] if "_L" in name else name
        if any(base in e or e in base for e in existing):
            continue
        needed.append(name)

    needed = list(set(needed))  # Dedupe
    print(f"Already have: {len(mesh_names) - len(needed)}")
    print(f"Need to extract: {len(needed)}")

    if not needed:
        print("\nAll meshes already extracted!")
        return

    # Group by package
    package_meshes = {}
    not_found = []

    print("\nFinding packages...")
    for mesh_name in needed:
        pkg = find_mesh_package(mesh_name)
        if pkg:
            if pkg not in package_meshes:
                package_meshes[pkg] = []
            package_meshes[pkg].append(mesh_name)
        else:
            not_found.append(mesh_name)

    print(f"Found packages for {len(needed) - len(not_found)} meshes")
    print(f"Could not find package for {len(not_found)} meshes")

    if not_found:
        print("\nMeshes without packages (first 10):")
        for m in not_found[:10]:
            print(f"  {m}")

    # Export by package
    success = 0
    failed = 0

    for pkg_path, meshes in package_meshes.items():
        pkg_name = os.path.basename(pkg_path)
        print(f"\n--- {pkg_name} ({len(meshes)} meshes) ---")

        for mesh_name in meshes:
            # Check if already exported
            gltf_path = os.path.join(OUTPUT_DIR, f"{mesh_name}.gltf")
            if os.path.exists(gltf_path):
                success += 1
                continue

            # Export
            psk_path = export_mesh(mesh_name, pkg_path)
            if not psk_path:
                print(f"  FAIL export: {mesh_name}")
                failed += 1
                continue

            # Convert
            if convert_to_gltf(psk_path, gltf_path):
                print(f"  OK: {mesh_name}")
                success += 1
            else:
                print(f"  FAIL convert: {mesh_name}")
                failed += 1

    print(f"\n=== DONE ===")
    print(f"Success: {success}")
    print(f"Failed: {failed}")
    print(f"No package: {len(not_found)}")


if __name__ == "__main__":
    main()
