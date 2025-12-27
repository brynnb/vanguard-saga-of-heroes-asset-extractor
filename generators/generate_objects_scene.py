#!/usr/bin/env python3
"""
Generate glTF object scenes for chunks using the exports table.
Simplified version that uses the new chunk/exports schema.
"""

import sqlite3
import json
import base64
import numpy as np
import sys
import os
import math
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    DB_PATH = config.DB_PATH
    OUTPUT_DIR = config.TERRAIN_GRID_DIR
except ImportError:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = "/Users/brynnbateman/Downloads/Vanguard EMU/vanguard_files.db"
    OUTPUT_DIR = os.path.join(base_dir, "output/terrain/terrain_grid")

CHUNK_SIZE = 200000
UNIT_SCALE = 1.0


def get_chunk_objects(conn, chunk_name):
    """Get all exports with positions for a chunk."""
    # Get chunk ID
    chunk_row = conn.execute(
        "SELECT id FROM chunks WHERE filename = ? OR filename = ?",
        (chunk_name, chunk_name + ".vgr")
    ).fetchone()
    
    if not chunk_row:
        return []
    
    chunk_id = chunk_row[0]
    
    # Get exports with valid positions
    exports = conn.execute("""
        SELECT id, object_name, class_name, 
               position_x, position_y, position_z,
               mesh_ref, prefab_name
        FROM exports
        WHERE chunk_id = ? 
          AND position_x IS NOT NULL 
          AND position_y IS NOT NULL
          AND position_z IS NOT NULL
        ORDER BY id
    """, (chunk_id,)).fetchall()
    
    objects = []
    for exp in exports:
        exp_id, name, class_name, x, y, z, mesh_ref, prefab_name = exp
        
        # Validate coordinates
        if abs(x) > 500000 or abs(y) > 500000 or abs(z) > 500000:
            continue
        
        # Try to extract mesh reference from object name
        if not mesh_ref:
            # Common patterns: ObjectName often contains mesh hint
            mesh_ref = name
        
        objects.append({
            "id": exp_id,
            "name": name,
            "class": class_name,
            "x": x,
            "y": y,
            "z": z,
            "mesh_ref": mesh_ref,
            "prefab_name": prefab_name,
        })
    
    return objects


def generate_scene_gltf(objects, output_path, chunk_name):
    """Generate a glTF with nodes for each object."""
    CHUNK_HALF_SIZE = 100000
    
    # Unit cube for fallback markers (small, hidden)
    s = 10.0
    vertices = np.array([
        [-s, -s, -s], [s, -s, -s], [s, s, -s], [-s, s, -s],
        [-s, -s, s], [s, -s, s], [s, s, s], [-s, s, s]
    ], dtype=np.float32)

    indices = np.array([
        0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6, 0, 4, 5, 0, 5, 1,
        2, 6, 7, 2, 7, 3, 0, 3, 7, 0, 7, 4, 1, 5, 6, 1, 6, 2
    ], dtype=np.uint32)

    buffer_data = vertices.tobytes() + indices.tobytes()
    
    nodes = []
    # Node 0 is the root container
    nodes.append({
        "name": f"ChunkRoot_{chunk_name}",
        "children": []
    })

    for obj in objects:
        x = obj["x"]
        y = obj["y"]
        z = obj["z"]
        
        # Transform to glTF coordinates (Y-up)
        # Vanguard: X=East, Y=North, Z=Up
        # glTF: X=Right, Y=Up, Z=Forward
        gx = (x + CHUNK_HALF_SIZE) * UNIT_SCALE
        gy = z * UNIT_SCALE  # Height
        gz = (y + CHUNK_HALF_SIZE) * UNIT_SCALE

        node_name = obj.get("mesh_ref") or obj["name"]
        
        parent_node = {
            "name": f"OBJ_{node_name}",
            "translation": [float(gx), float(gy), float(gz)],
            "mesh": 0,  # Cube marker
            "extras": {
                "class": obj["class"],
                "vanguard_id": obj["id"],
                "mesh_ref": obj.get("mesh_ref", ""),
                "is_prefab": False
            }
        }
        
        p_idx = len(nodes)
        nodes.append(parent_node)
        nodes[0]["children"].append(p_idx)

    gltf = {
        "asset": {"version": "2.0", "generator": "generate_objects_scene.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": [{
            "name": "MarkerCube",
            "primitives": [{
                "attributes": {"POSITION": 0},
                "indices": 1,
                "mode": 4
            }]
        }],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": 8,
                "type": "VEC3",
                "min": [-s, -s, -s],
                "max": [s, s, s]
            },
            {
                "bufferView": 1,
                "componentType": 5125,
                "count": 36,
                "type": "SCALAR"
            }
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 96},
            {"buffer": 0, "byteOffset": 96, "byteLength": 144}
        ],
        "buffers": [{
            "uri": f"data:application/octet-stream;base64,{base64.b64encode(buffer_data).decode()}",
            "byteLength": len(buffer_data)
        }]
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(gltf, f)
    
    return len(objects)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate glTF object scenes for chunks")
    parser.add_argument('chunk', nargs='?', help="Specific chunk name (e.g., chunk_n25_26)")
    parser.add_argument('--all', action='store_true', help="Process all chunks with terrain files")
    args = parser.parse_args()
    
    conn = sqlite3.connect(DB_PATH)
    
    if args.all:
        # Find all terrain files and generate objects for them
        terrain_files = sorted(Path(OUTPUT_DIR).glob("*_terrain.gltf"))
        chunks = [f.stem.replace("_terrain", "") for f in terrain_files]
        print(f"Found {len(chunks)} terrain files")
    elif args.chunk:
        chunks = [args.chunk]
    else:
        print("Usage: python generate_objects_scene.py <chunk_name>")
        print("       python generate_objects_scene.py --all")
        sys.exit(1)
    
    total_objects = 0
    for chunk_name in chunks:
        print(f"Processing {chunk_name}...", end=" ")
        
        objects = get_chunk_objects(conn, chunk_name)
        if not objects:
            print("No objects with positions")
            continue
        
        output_path = os.path.join(OUTPUT_DIR, f"{chunk_name}_objects.gltf")
        count = generate_scene_gltf(objects, output_path, chunk_name)
        print(f"OK ({count} objects)")
        total_objects += count
    
    print(f"\nTotal: {total_objects} objects across {len(chunks)} chunks")
    conn.close()


if __name__ == "__main__":
    main()
