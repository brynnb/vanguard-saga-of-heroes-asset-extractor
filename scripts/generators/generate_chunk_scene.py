#!/usr/bin/env python3
"""
Generate glTF scenes for chunks using database information.
Creates a scene with terrain and placed object nodes.
"""

import sqlite3
import json
import base64
import numpy as np
import sys
import os
import math
import struct
from pathlib import Path

# Add parent directory to path to allow importing config
# Add project root to path (go up 2 levels from scripts/extractors or scripts/generators)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# Add extractors directory for imports
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extractors"))

try:
    import config
    DB_PATH = config.DB_PATH
    MESH_DIR = config.MESH_BUILDINGS_DIR
    OUTPUT_DIR = config.TERRAIN_GRID_DIR
except ImportError:
    # Fallback
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = os.path.join(base_dir, "output/data/vanguard_data.db")
    MESH_DIR = os.path.join(base_dir, "output/meshes/buildings")
    OUTPUT_DIR = os.path.join(base_dir, "output/terrain/terrain_grid")
    MESH_INDEX_PATH = os.path.join(base_dir, "output/data/mesh_index.sqlite")

from extractors.resolve_prefabs import PrefabResolver

import re

CHUNK_SIZE = 200000
SECTORS_PER_CHUNK = 8
SECTOR_SIZE = CHUNK_SIZE / SECTORS_PER_CHUNK  # 25000

# Keep objects in Vanguard units to match terrain (which is also in Vanguard units)
# The viewer has manual scale controls if unit conversion is needed
UNIT_SCALE = 1.0

def parse_sector_from_prefab(prefab_name):
    """Extract sector grid position from PrefabName like 'chunk_-25_26_Sector_6_7_ag'.
    
    Returns (sector_x, sector_y) or None if not a sector prefab.
    """
    if not prefab_name:
        return None
    match = re.search(r'Sector_(\d+)_(\d+)', str(prefab_name))
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))

def sector_to_position(sector_x, sector_y):
    """Convert sector grid indices to chunk-local world position (at sector center)."""
    # Position at center of sector, relative to chunk origin (0,0)
    x = (sector_x + 0.5) * SECTOR_SIZE - CHUNK_SIZE / 2
    y = (sector_y + 0.5) * SECTOR_SIZE - CHUNK_SIZE / 2
    return (x, y)

def parse_xyz_from_raw(data):
    """Extract X, Y, Z position from CompoundObject raw data at offset 57-68.
    
    Returns (x, y, z) tuple if all three values are valid world coordinates,
    otherwise returns None.
    """
    if not data or len(data) < 69:
        return None
    try:
        x = struct.unpack("<f", data[57:61])[0]
        y = struct.unpack("<f", data[61:65])[0]
        z = struct.unpack("<f", data[65:69])[0]
        
        # Validate all coordinates are reasonable world values
        if (math.isnan(x) or math.isnan(y) or math.isnan(z) or
            abs(x) > 200000 or abs(y) > 200000 or abs(z) > 200000):
            return None
            
        return (x, y, z)
    except:
        return None


def get_chunk_id(conn, chunk_name):
    """Get the database ID for a chunk name."""
    res = conn.execute(
        "SELECT id FROM chunks WHERE filename = ? OR filename = ?",
        (chunk_name, chunk_name + ".vgr"),
    ).fetchone()
    return res[0] if res else None


def get_chunk_objects(conn, chunk_id):
    """Get all placeable objects for a chunk, with correct positions from raw data."""
    # First get the chunk file path
    chunk_info = conn.execute(
        "SELECT filepath FROM chunks WHERE id = ?", (chunk_id,)
    ).fetchone()
    
    chunk_file = chunk_info[0] if chunk_info else None
    pkg = None
    
    # Try to load the package for raw position parsing
    if chunk_file and os.path.exists(chunk_file):
        try:
            from extract_bsp import UE2Package
            pkg = UE2Package(chunk_file)
        except Exception as e:
            print(f"  Warning: Could not load chunk file for raw parsing: {e}")
    
    exports = conn.execute(
        """
        SELECT e.id, e.object_name, e.class_name, 
               e.position_x, e.position_y, e.position_z, e.export_index
        FROM exports e
        WHERE e.chunk_id = ?
        ORDER BY e.id
    """,
        (chunk_id,),
    ).fetchall()


    # Track current sector position for carry-forward to non-sector objects
    current_sector_x = 0.0
    current_sector_y = 0.0
    
    objects = []
    for exp in exports:
        export_id, name, class_name, x, y, z, export_index = exp
        
        if class_name == "CompoundObject":
            # Get Location property (now captured from secondary property list!)
            location_row = conn.execute(
                "SELECT value_text FROM properties WHERE export_id = ? AND prop_name = 'Location'",
                (export_id,)
            ).fetchone()
            
            if location_row and location_row[0]:
                try:
                    import json
                    loc = json.loads(location_row[0])
                    x = loc.get("x", x)
                    y = loc.get("y", y)
                    z = loc.get("z", z)
                except:
                    pass
            else:
                # Fallback: Try sector from PrefabName if Location isn't in DB yet
                prefab_row = conn.execute(
                    "SELECT value_text FROM properties WHERE export_id = ? AND prop_name = 'PrefabName'",
                    (export_id,)
                ).fetchone()
                prefab_name = prefab_row[0] if prefab_row else None
                
                sector = parse_sector_from_prefab(prefab_name)
                if sector:
                    sector_pos = sector_to_position(sector[0], sector[1])
                    x, y = sector_pos
                    current_sector_x, current_sector_y = sector_pos
                elif current_sector_x != 0 or current_sector_y != 0:
                    x, y = current_sector_x, current_sector_y

        


        # Skip if position is still invalid
        if x is None or y is None or z is None:
            continue
        if abs(x) > 500000 or abs(y) > 500000 or abs(z) > 500000:
            continue

        # Get properties for this export
        props = conn.execute(
            """
            SELECT prop_name, prop_type, value_text
            FROM properties
            WHERE export_id = ? AND value_text IS NOT NULL
        """,
            (export_id,),
        ).fetchall()

        prop_dict = {p[0]: (p[1], p[2]) for p in props}

        obj = {
            "id": export_id,
            "name": name,
            "class": class_name,
            "x": x,
            "y": y,
            "z": z,
            "properties": prop_dict,
        }



        # Extract rotation if available
        if "Rotation" in prop_dict:
            try:
                rot = json.loads(prop_dict["Rotation"][1])
                obj["rotation"] = rot
            except:
                pass

        # Extract scale if available
        if "DrawScale3D" in prop_dict:
            try:
                scale = json.loads(prop_dict["DrawScale3D"][1])
                obj["scale"] = scale
            except:
                pass
        elif "DrawScale" in prop_dict:
            try:
                scale_val = float(prop_dict["DrawScale"][1])
                obj["scale"] = {"x": scale_val, "y": scale_val, "z": scale_val}
            except:
                pass

        if "StaticMesh" in prop_dict:
            obj["mesh_ref"] = prop_dict["StaticMesh"][1]
        elif "Mesh" in prop_dict:
            obj["mesh_ref"] = prop_dict["Mesh"][1]
        elif "PrefabName" in prop_dict:
            # Mark it for prefab resolution later
            obj["prefab_name"] = prop_dict["PrefabName"][1]
            obj["mesh_ref"] = prop_dict["PrefabName"][1]
        
        # FINAL FALLBACK: If we still don't have a mesh_ref, but we have a package,
        # try to parse the properties from the raw data.
        if not obj.get("mesh_ref") and pkg and 0 <= export_index - 1 < len(pkg.exports):
            try:
                from ue2.properties import find_property_start, parse_properties
                exp_data = pkg.get_export_data(pkg.exports[export_index - 1])
                start = find_property_start(exp_data, pkg.names)
                if start != -1:
                    raw_props = parse_properties(exp_data, pkg.names, start)
                    # Convert to our internal format
                    for p_name, p_val in raw_props:
                        if p_name == "StaticMesh" and isinstance(p_val, dict):
                            # It's an object reference
                            ref_name = pkg.get_object_name(p_val.get("ref", 0))
                            if ref_name:
                                obj["mesh_ref"] = ref_name
                                # Update prop_dict for compatibility
                                prop_dict["StaticMesh"] = ("Object", ref_name)
                        elif p_name == "PrefabName" and isinstance(p_val, str):
                            obj["prefab_name"] = p_val
                            obj["mesh_ref"] = p_val
                            prop_dict["PrefabName"] = ("Name", p_val)
            except Exception as e:
                # print(f"  Fallback parsing failed for {name}: {e}")
                pass

        objects.append(obj)

    return objects


def euler_to_quaternion(pitch_units, yaw_units, roll_units):
    """Convert UE2 rotation units (0-65535) to glTF quaternion."""
    # UE2: 65536 units = 360 degrees
    # glTF/Three.js: Y-up, Right-handed
    
    # Convert to radians and map axes
    # Pitch -> X, Yaw -> Y (inverted), Roll -> Z
    p = (pitch_units / 65536.0) * 2.0 * math.pi
    y = -(yaw_units / 65536.0) * 2.0 * math.pi
    r = (roll_units / 65536.0) * 2.0 * math.pi

    cy = math.cos(y * 0.5)
    sy = math.sin(y * 0.5)
    cp = math.cos(p * 0.5)
    sp = math.sin(p * 0.5)
    cr = math.cos(r * 0.5)
    sr = math.sin(r * 0.5)

    w = cr * cp * cy + sr * sp * sy
    x = cr * sp * cy + sr * cp * sy
    y = cr * cp * sy - sr * sp * cy
    z = sr * cp * cy - cr * sp * sy
    
    return [float(x), float(y), float(z), float(w)]


def generate_scene_gltf(objects, output_path, chunk_name):
    """Generate a glTF with nodes for each object."""
    CHUNK_HALF_SIZE = 100000
    
    # Unit cube for fallback markers
    s = 50.0
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
        "name": f"Root_{chunk_name}",
        "children": []
    })

    # Initialize Prefab Resolver
    sgo_path = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Archives/binaryprefabs.sgo"
    try:
        resolver = PrefabResolver(sgo_path)
    except:
        resolver = None
        print("Warning: Could not initialize SGO resolver.")

    if not "MESH_INDEX_PATH" in globals():
         # Fallback path if config failed
         MESH_INDEX_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output/data/mesh_index.sqlite")

    # Process all objects - positions are now correctly set in get_chunk_objects
    for i, obj in enumerate(objects):
        x = obj["x"]
        y = obj["y"]
        z = obj["z"]



        # Transform to local-to-chunk coordinates
        # In Vanguard: X=East, Y=North, Z=Up
        # In glTF/Three: X=Right, Y=Up, Z=Forward  
        # Chunk origin is at center (CHUNK_HALF_SIZE, CHUNK_HALF_SIZE)
        # Apply UNIT_SCALE (0.02) to convert everything to meters
        gx = (x + CHUNK_HALF_SIZE) * UNIT_SCALE
        gy = z * UNIT_SCALE # Height (vertical)
        gz = (y + CHUNK_HALF_SIZE) * UNIT_SCALE

        node_name = obj.get("mesh_ref") or obj["name"]
        
        # Determine if we should treat this as a Parent Node (Prefab) or Leaf
        prefab_components = []
        # Attempt to resolve ALL prefabs if we have a valid prefab name
        if resolver and obj.get("prefab_name"):
            # Try to resolve components
            comps = resolver.resolve_prefab(obj["prefab_name"], MESH_INDEX_PATH)
            if comps:
                prefab_components = comps
                print(f"  Exploded prefab {obj['prefab_name']} into {len(comps)} components")

        parent_node = {
            "name": f"OBJ_{node_name}",
            "translation": [float(gx), float(gy), float(gz)],
            "children": []
        }
        
        if "rotation" in obj:
            rot = obj["rotation"]
            parent_node["rotation"] = euler_to_quaternion(
                rot.get("Pitch", 0), 
                rot.get("Yaw", 0), 
                rot.get("Roll", 0)
            )
            
        # Scale
        base_scale = UNIT_SCALE
        sx, sy, sz = base_scale, base_scale, base_scale
        if "scale" in obj:
            sc = obj["scale"]
            sx *= float(sc.get("x", 1))
            sy *= float(sc.get("y", 1))
            sz *= float(sc.get("z", 1))
        parent_node["scale"] = [sx, sy, sz]
        
        # Metadata
        # If we successfully exploded this prefab, we MUST NOT pass the prefab name as a mesh_ref
        # to the viewer, otherwise it will try to resolve the container itself (resulting in substring matches)
        # and render a duplicate mesh on top of the components.
        final_mesh_ref = obj.get("mesh_ref", "")
        if prefab_components:
            final_mesh_ref = "" 

        parent_node["extras"] = {
            "class": obj["class"],
            "vanguard_id": obj["id"],
            "mesh_ref": final_mesh_ref,
            "is_prefab": len(prefab_components) > 0
        }

        # If it's a prefab with components, add children
        # If not, treating it as a visible node (green box fallback or static mesh)
        if prefab_components:
            for comp in prefab_components:
                # Component position is LOCAL to the prefab
                # Ideally, we add a child node with local transform
                # glTF handles the composition.
                # BUT: The component 'pos' from SGO is in Vanguard Units.
                # We need to scale the translation by UNIT_SCALE ONLY IF the parent scaling doesn't cover it.
                # Wait: parent_node has scale [UNIT_SCALE, UNIT_SCALE, UNIT_SCALE].
                # So children should just use raw Vanguard units? 
                # NO. If parent is scaled by 0.02, then a child translation of 100 becomes 2 meters.
                # SGO units are Vanguard units. So yes, raw units are correct IF parent has scale.
                
                # Check SGO coords: Bench marble is at Z=223.
                # If we put 223 in child.translation, and parent has scale 1.0 (since UNIT_SCALE=1.0 now),
                # then it's at 223 meters up. Correct.
                
                c_pos = comp["pos"]
                child_node = {
                    "name": f"COMP_{comp['mesh']}",
                    "translation": [c_pos[0], c_pos[2], c_pos[1]], # Swizzle Y/Z?
                    # Vanguard: X=East, Y=North, Z=Up. 
                    # Prefab Local: Likely X=Forward, Y=Right, Z=Up? Or same as world?
                    # Let's assume standard mapping first: X->X, Z->Y (height), Y->Z
                    
                    # Wait, our main mapping is:
                    # World X -> GLTF X
                    # World Z -> GLTF Y (Height)
                    # World Y -> GLTF Z
                    # So let's map child the same way.
                    "mesh": 0, # Cube marker by default, will be loaded by viewer
                    "extras": { "mesh_ref": comp["mesh"] }
                }
                
                # Add child to nodes list
                c_idx = len(nodes)
                nodes.append(child_node)
                parent_node["children"].append(c_idx)
        else:
             # Just a visual node itself
             parent_node["mesh"] = 0 

        # Add parent to root
        p_idx = len(nodes)
        nodes.append(parent_node)
        nodes[0]["children"].append(p_idx)

    gltf = {
        "asset": {"version": "2.0", "generator": "generate_chunk_scene.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": [{
            "name": "MarkerCube",
            "primitives": [{
                "attributes": {"POSITION": 0}, 
                "indices": 1,
                "material": 0
            }]
        }],
        "materials": [
            {
                "name": "MarkerMaterial",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.0, 1.0, 0.0, 1.0], # Full opaque neon green
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0
                }
            }
        ],
        "accessors": [
            {
                "bufferView": 0, "componentType": 5126, "count": len(vertices),
                "type": "VEC3", "min": vertices.min(axis=0).tolist(), "max": vertices.max(axis=0).tolist()
            },
            {
                "bufferView": 1, "componentType": 5125, "count": len(indices), "type": "SCALAR"
            }
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(vertices) * 12, "target": 34962},
            {"buffer": 0, "byteOffset": len(vertices) * 12, "byteLength": len(indices) * 4, "target": 34963}
        ],
        "buffers": [{
            "uri": "data:application/octet-stream;base64," + base64.b64encode(buffer_data).decode(),
            "byteLength": len(buffer_data)
        }]
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(gltf, f)

    print(f"  Generated {output_path} with {len(objects)} object nodes")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate glTF scene for a chunk")
    parser.add_argument("chunk", help="Chunk name (e.g., chunk_n17_5)")
    args = parser.parse_args()

    chunk_name = args.chunk

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get chunk ID
    chunk_id = get_chunk_id(conn, chunk_name)
    if not chunk_id:
        print(f"Chunk {chunk_name} not found in database")
        return

    print(f"Processing {chunk_name} (chunk_id={chunk_id})")

    # Get all objects
    objects = get_chunk_objects(conn, chunk_id)
    print(f"  Found {len(objects)} placeable objects")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, f"{chunk_name}_objects.gltf")

    generate_scene_gltf(objects, output_path, chunk_name)

    conn.close()


if __name__ == "__main__":
    main()
