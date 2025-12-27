#!/usr/bin/env python3
"""
Generate glTF markers for CompoundObjects extracted from a Vanguard chunk.
Positions markers to align with terrain from extract_terrain_grid.py.
"""

import json
import struct
import base64
import numpy as np
import os
import sys

# Add parent directory to path to allow importing ue2 and config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from ue2.reader import read_compact_index_at as read_compact_index, read_fstring_at as read_fstring


# Removed duplicate read_compact_index and read_fstring functions


def extract_objects_from_chunk(vgr_path):
    """Extract CompoundObject positions from a VGR file."""
    with open(vgr_path, "rb") as f:
        data = f.read()

    # Parse header
    name_count = struct.unpack("<I", data[12:16])[0]
    name_offset = struct.unpack("<I", data[16:20])[0]
    export_count = struct.unpack("<I", data[20:24])[0]
    export_offset = struct.unpack("<I", data[24:28])[0]

    # Read names
    names = []
    pos = name_offset
    for i in range(name_count):
        name, pos = read_fstring(data, pos)
        flags = struct.unpack("<I", data[pos : pos + 4])[0]
        pos += 4
        names.append(name)

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
                "object_name": (
                    names[object_name] if object_name < len(names) else str(object_name)
                ),
                "serial_size": serial_size,
                "serial_offset": serial_offset,
            }
        )

    # Extract CompoundObject positions
    compound_objects = [e for e in exports if "CompoundObject" in e["object_name"]]
    results = []

    for obj in compound_objects:
        offset = obj["serial_offset"]
        size = obj["serial_size"]
        raw = data[offset : offset + size]

        # Find position by scanning for valid coordinates
        best_pos = None
        for j in range(max(0, size - 20), size - 11):
            x = struct.unpack("<f", raw[j : j + 4])[0]
            y = struct.unpack("<f", raw[j + 4 : j + 8])[0]
            z = struct.unpack("<f", raw[j + 8 : j + 12])[0]
            if all(1000 < abs(v) < 500000 for v in [x, y, z]) and not any(
                v != v for v in [x, y, z]
            ):
                best_pos = (x, y, z)
                break

        if best_pos:
            results.append(
                {
                    "name": obj["object_name"],
                    "vang_x": best_pos[0],
                    "vang_y": best_pos[1],
                    "vang_z": best_pos[2],
                }
            )

    return results


def generate_marker_gltf(objects, output_path, chunk_x, chunk_y, ref_x=-30, ref_y=27):
    """Generate a glTF file with box markers at object positions."""

    # Individual terrain chunks are generated at origin (0,0,0) spanning 0 to 51200
    # Object positions in Vanguard are relative to chunk center
    # So we need to offset by half chunk size to align with terrain at origin
    chunk_size = 512 * 100  # 51200 units
    chunk_center = chunk_size / 2  # 25600

    # Box geometry
    box_verts = [
        [-1, -1, -1],
        [1, -1, -1],
        [1, 1, -1],
        [-1, 1, -1],
        [-1, -1, 1],
        [1, -1, 1],
        [1, 1, 1],
        [-1, 1, 1],
    ]
    box_faces = [
        [0, 1, 2],
        [0, 2, 3],
        [4, 6, 5],
        [4, 7, 6],
        [0, 4, 5],
        [0, 5, 1],
        [2, 6, 7],
        [2, 7, 3],
        [0, 3, 7],
        [0, 7, 4],
        [1, 5, 6],
        [1, 6, 2],
    ]

    marker_size = 300
    vertices = []
    indices = []

    for obj in objects:
        # Transform Vanguard coords to glTF coords (terrain at origin)
        # Object XY coords span ~190000 units centered at 0, terrain spans 0-51200
        # Object Z (height) is ~2x terrain height, so divide by 2 to match

        scale = 0.27  # XY scale to fit 190k range into 51k

        world_x = obj["vang_x"] * scale + chunk_center
        world_y = obj["vang_z"] / 2.0  # Object heights are 2x terrain, divide to match
        world_z = -obj["vang_y"] * scale + chunk_center

        base_idx = len(vertices)
        for bv in box_verts:
            vertices.append(
                [
                    world_x + bv[0] * marker_size,
                    world_y + bv[1] * marker_size,
                    world_z + bv[2] * marker_size,
                ]
            )

        for face in box_faces:
            indices.extend([base_idx + face[0], base_idx + face[1], base_idx + face[2]])

    vertices_arr = np.array(vertices, dtype=np.float32)
    indices_arr = np.array(indices, dtype=np.uint32)

    vertices_bin = vertices_arr.tobytes()
    indices_bin = indices_arr.tobytes()
    buffer_data = vertices_bin + indices_bin

    v_min = vertices_arr.min(axis=0).tolist()
    v_max = vertices_arr.max(axis=0).tolist()

    gltf = {
        "asset": {"version": "2.0", "generator": "Vanguard Object Markers"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": f"ObjectMarkers_n{abs(chunk_x)}_{chunk_y}"}],
        "meshes": [
            {
                "primitives": [
                    {"attributes": {"POSITION": 0}, "indices": 1, "material": 0}
                ]
            }
        ],
        "materials": [
            {
                "pbrMetallicRoughness": {
                    "baseColorFactor": [1.0, 0.3, 0.1, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.5,
                },
                "emissiveFactor": [0.8, 0.2, 0.05],
            }
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(vertices_arr),
                "type": "VEC3",
                "min": v_min,
                "max": v_max,
            },
            {
                "bufferView": 1,
                "componentType": 5125,
                "count": len(indices_arr),
                "type": "SCALAR",
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(vertices_bin)},
            {
                "buffer": 0,
                "byteOffset": len(vertices_bin),
                "byteLength": len(indices_bin),
            },
        ],
        "buffers": [
            {
                "uri": f"data:application/octet-stream;base64,{base64.b64encode(buffer_data).decode()}",
                "byteLength": len(buffer_data),
            }
        ],
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(gltf, f)

    return len(objects), v_min, v_max


def main():
    # Default to chunk -25, 26
    chunk_x = -25
    chunk_y = 26

    if len(sys.argv) >= 3:
        chunk_x = int(sys.argv[1])
        chunk_y = int(sys.argv[2])

    x_str = f"n{abs(chunk_x)}" if chunk_x < 0 else str(chunk_x)
    y_str = f"n{abs(chunk_y)}" if chunk_y < 0 else str(chunk_y)
    vgr_path = os.path.join(config.ASSETS_PATH, f"Maps/chunk_{x_str}_{y_str}.vgr")
    output_path = os.path.join(config.TERRAIN_GRID_DIR, f"chunk_{x_str}_{y_str}_markers.gltf")

    print(f"Extracting objects from chunk ({chunk_x}, {chunk_y})...")
    print(f"  VGR: {vgr_path}")

    if not os.path.exists(vgr_path):
        print(f"  ERROR: File not found")
        return

    objects = extract_objects_from_chunk(vgr_path)
    print(f"  Found {len(objects)} objects with valid positions")

    if not objects:
        print("  No objects to export")
        return

    count, v_min, v_max = generate_marker_gltf(objects, output_path, chunk_x, chunk_y)

    print(f"  Saved {count} markers to {output_path}")
    print(
        f"  World bounds: X=[{v_min[0]:.0f}, {v_max[0]:.0f}], Y=[{v_min[1]:.0f}, {v_max[1]:.0f}], Z=[{v_min[2]:.0f}, {v_max[2]:.0f}]"
    )


if __name__ == "__main__":
    main()
