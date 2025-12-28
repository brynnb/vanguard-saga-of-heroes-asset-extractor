import sqlite3
import os
import json
import sys

# Add root to path
sys.path.append(os.getcwd())

import config
from extractors.extract_staticmesh import StaticMeshExporter

def master_harvest():
    # 1. Get all unique meshes from DB
    db_path = config.DB_PATH
    output_dir = config.MESH_BUILDINGS_DIR
    
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT DISTINCT value_text FROM properties WHERE prop_name = 'StaticMesh'")
    mesh_refs = [row[0] for row in cursor.fetchall()]
    
    # Also get meshes from mesh_index (the source map)
    idx_path = os.path.join(config.DATA_DIR, "mesh_index.sqlite")
    conn_idx = sqlite3.connect(idx_path)
    cursor = conn_idx.execute("SELECT object_name, package_path FROM mesh_index WHERE class_name='StaticMesh'")
    mesh_map = {row[0]: row[1] for row in cursor.fetchall()}
    
    print(f"Total unique mesh references found: {len(mesh_refs)}")
    
    roots = [
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets",
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Maps"
    ]
    
    count = 0
    for mesh_name in mesh_refs:
        if not mesh_name: continue
        
        # Check if already extracted
        if os.path.exists(os.path.join(output_dir, f"{mesh_name}.gltf")) or \
           os.path.exists(os.path.join(output_dir, f"{mesh_name}_L0.gltf")):
            continue
            
        # Find package
        pkg_rel = mesh_map.get(mesh_name)
        if not pkg_rel:
            # Try finding if the mesh name is a partial or has package prefix
            # This is slow so we only do it for misses
            continue
            
        pkg_path = None
        for r in roots:
            c = os.path.join(r, pkg_rel)
            if os.path.exists(c):
                pkg_path = c
                break
        
        if not pkg_path:
            continue
            
        print(f"[{count}] Extracting missing mesh: {mesh_name} from {pkg_rel}")
        try:
            exporter = StaticMeshExporter(pkg_path, output_dir)
            if exporter.export_mesh(mesh_name):
                count += 1
            else:
                # Fallback: maybe it's in the package under a slightly different name?
                pass
        except Exception as e:
            print(f"  Error extracting {mesh_name}: {e}")
            
    print(f"Master Harvest complete. Extracted {count} new meshes.")
    
    # 2. Update manifest.json
    print("Updating manifest.json...")
    files = [f for f in os.listdir(output_dir) if f.endswith(".gltf")]
    with open(os.path.join(output_dir, "manifest.json"), "w") as f:
        json.dump({"files": files}, f, indent=2)
    print(f"Manifest updated with {len(files)} meshes.")

if __name__ == "__main__":
    master_harvest()
