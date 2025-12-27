#!/usr/bin/env python3
import json
import os
import sys
import sqlite3
import subprocess

# Add parent directory to path to allow importing config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from extractors.extract_staticmesh import StaticMeshExporter

def convert_mesh(mesh_name, package_rel_path, output_dir):
    """
    Extract a mesh using the Python exporter directly.
    """
    # Construct full package path
    # package_rel_path comes from DB as "Meshes/filename.usx"
    # We need to prepend the Assets root.
    
    # Try common roots
    roots = [
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets",
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Maps"
    ]
    
    pkg_path = None
    for r in roots:
        candidate = os.path.join(r, package_rel_path)
        if os.path.exists(candidate):
            pkg_path = candidate
            break
            
    if not pkg_path:
        # Try finding it by name in the roots
        basename = os.path.basename(package_rel_path)
        for r in roots:
            c = os.path.join(r, "Meshes", basename)
            if os.path.exists(c): 
                pkg_path = c
                break
            c = os.path.join(r, basename)
            if os.path.exists(c): 
                pkg_path = c
                break
    
    if not pkg_path:
        print(f"  [Error] Could not locate package file: {package_rel_path}")
        return

    print(f"Extracting {mesh_name} from {pkg_path}...")
    
    try:
        exporter = StaticMeshExporter(pkg_path, output_dir)
        # Try to export just the specific mesh to save time
        if not exporter.export_mesh(mesh_name):
            # If named export fails, try all as fallback
            exporter.export_all_meshes()
    except Exception as e:
        print(f"  [Error] Extraction failed: {e}")

def main():
    scene_path = "output/terrain/terrain_grid/chunk_n25_26_objects.gltf"
    mesh_db_path = "output/data/mesh_index.sqlite"
    mesh_dir = "output/meshes/buildings"
    
    if not os.path.exists(scene_path):
        print(f"Scene file not found: {scene_path}")
        return

    with open(scene_path, 'r') as f:
        gltf = json.load(f)
        
    required_meshes = set()
    for node in gltf.get("nodes", []):
        extras = node.get("extras", {})
        mesh_ref = extras.get("mesh_ref")
        if mesh_ref:
            required_meshes.add(mesh_ref)
            
    print(f"Found {len(required_meshes)} unique mesh references in scene.")
    
    # Check what is missing
    missing = []
    for m in required_meshes:
        # Check standard and LOD variants
        found = False
        candidates = [
            f"{m}.gltf",
            f"{m}_L0.gltf",
            f"{m}_L1.gltf",
            f"{m}_ver01.gltf"
        ]
        for c in candidates:
            if os.path.exists(os.path.join(mesh_dir, c)):
                found = True
                break
        
        if not found:
            missing.append(m)
            
    print(f"Identified {len(missing)} missing meshes.")
    if len(missing) == 0:
        print("All meshes present!")
        return

    # Look up in DB
    conn = sqlite3.connect(mesh_db_path)
    cursor = conn.cursor()
    
    for m in missing:
        print(f"Resolving {m}...")
        # Try exact match first
        cursor.execute("SELECT package_path FROM mesh_index WHERE object_name = ?", (m,))
        res = cursor.fetchone()
        
        if not res:
            # Try fuzzy match (e.g. without suffix if DB creates simple names)
            pass
            
        if res:
            # We found it in the index! Extract it.
            # Convert package path relative to assets
            # In index: "Meshes/..." 
            # We assume extract_staticmesh.py handles lookup by name using the index itself
            convert_mesh(m, res[0], mesh_dir)
        else:
            print(f"  [Warn] Mesh {m} not found in index.")
            
    conn.close()

    # Update manifest.json
    print("Updating manifest.json...")
    files = [f for f in os.listdir(mesh_dir) if f.endswith(".gltf")]
    with open(os.path.join(mesh_dir, "manifest.json"), "w") as f:
        json.dump({"files": list(set(files))}, f, indent=2)
    print(f"Manifest updated with {len(files)} meshes.")

if __name__ == "__main__":
    main()
