#!/usr/bin/env python3
"""
Extract material/shader references from Vanguard meshes using umodel dump.
Creates a mapping of mesh names to their shader references.
"""

import subprocess
import re
import json
import os
import sys

# Add parent directory to path to allow importing config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

WINE = config.WINE_PATH
UMODEL = config.UMODEL_PATH


def extract_shaders_from_mesh(usx_path: str, mesh_name: str = None) -> dict:
    """
    Run umodel -dump on a mesh and extract shader references.
    Returns dict mapping mesh names to list of shader names.
    """
    cmd = [WINE, UMODEL, "-dump", "-game=vang"]
    if mesh_name:
        cmd.extend(["-obj=" + mesh_name])
    cmd.append(usx_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
    except Exception as e:
        print(f"Error running umodel: {e}")
        return {}

    # Parse shader imports
    # Pattern: WARNING: Import(Shader'ShaderName'): package PackageName was not found
    # Or successful loads
    shader_pattern = re.compile(r"Import\(Shader'([^']+)'\)")
    mesh_pattern = re.compile(r"Loading StaticMesh (\S+) from package")

    mesh_shaders = {}
    current_mesh = None

    for line in output.split("\n"):
        # Check for mesh loading
        mesh_match = mesh_pattern.search(line)
        if mesh_match:
            current_mesh = mesh_match.group(1)
            if current_mesh not in mesh_shaders:
                mesh_shaders[current_mesh] = []

        # Check for shader imports
        shader_match = shader_pattern.search(line)
        if shader_match and current_mesh:
            shader_name = shader_match.group(1)
            if shader_name not in mesh_shaders[current_mesh]:
                mesh_shaders[current_mesh].append(shader_name)

    return mesh_shaders


def build_mesh_material_db(meshes_dir: str, output_path: str = "mesh_materials.json"):
    """
    Scan all .usx files and extract shader references for each mesh.
    """
    db = {}

    meshes_path = Path(meshes_dir)
    usx_files = list(meshes_path.glob("*.usx"))

    print(f"Scanning {len(usx_files)} .usx files...")

    for i, usx_file in enumerate(usx_files):
        if i % 10 == 0:
            print(f"  Processing {i+1}/{len(usx_files)}: {usx_file.name}")

        mesh_shaders = extract_shaders_from_mesh(str(usx_file))

        for mesh_name, shaders in mesh_shaders.items():
            db[mesh_name.lower()] = {
                "package": usx_file.stem,
                "shaders": [s.lower() for s in shaders],
            }

    with open(output_path, "w") as f:
        json.dump(db, f, indent=2)

    print(f"\nSaved {len(db)} mesh material mappings to {output_path}")
    return db


def main():
    import sys

    if len(sys.argv) < 2:
        # Default: scan a single mesh file for testing
        meshes_asset_dir = os.path.join(config.ASSETS_PATH, "Meshes")
        usx_path = os.path.join(meshes_asset_dir, "Ra0001_P0001_Barrel001_Mesh.usx")
        print(f"Extracting shaders from: {usx_path}")
        mesh_shaders = extract_shaders_from_mesh(usx_path)
        for mesh, shaders in mesh_shaders.items():
            print(f"  {mesh}: {shaders}")
    else:
        meshes_dir = sys.argv[1]
        output_path = sys.argv[2] if len(sys.argv) > 2 else config.MESH_MATERIALS_PATH
        build_mesh_material_db(meshes_dir, output_path)


if __name__ == "__main__":
    main()
