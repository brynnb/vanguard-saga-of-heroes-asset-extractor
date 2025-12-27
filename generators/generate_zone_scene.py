#!/usr/bin/env python3
"""
Generate a complete zone scene with terrain and placed object markers.
Combines terrain mesh with CompoundObject positions for visualization.
"""

import struct
import json
import base64
import os
import math
from pathlib import Path
from typing import List, Dict, Optional
import sys

# Add parent directory to path to allow importing config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def load_zone_data(zone_data_path: str) -> Dict:
    """Load zone data from JSON."""
    with open(zone_data_path, "r") as f:
        return json.load(f)


def generate_terrain_mesh(
    width: int,
    height: int,
    scale: float = 1.0,
    height_grid: Optional[List[List[float]]] = None,
) -> Dict:
    """Generate terrain mesh data."""
    positions = []
    normals = []
    uvs = []
    indices = []

    for z in range(height):
        for x in range(width):
            # Position
            px = (x - width / 2) * scale
            py = height_grid[z][x] if height_grid else 0
            pz = (z - height / 2) * scale
            positions.extend([px, py, pz])

            # Normal (simple up vector, will be recalculated)
            normals.extend([0, 1, 0])

            # UV
            uvs.extend([x / (width - 1), 1.0 - z / (height - 1)])

    # Generate indices
    for z in range(height - 1):
        for x in range(width - 1):
            i0 = z * width + x
            i1 = i0 + 1
            i2 = i0 + width
            i3 = i2 + 1
            indices.extend([i0, i2, i1, i1, i2, i3])

    return {"positions": positions, "normals": normals, "uvs": uvs, "indices": indices}


def generate_marker_mesh(x: float, y: float, z: float, size: float = 50.0) -> Dict:
    """Generate a simple box marker mesh at the given position."""
    # Simple box vertices
    half = size / 2
    box_positions = [
        # Front face
        x - half,
        y,
        z - half,
        x + half,
        y,
        z - half,
        x + half,
        y + size,
        z - half,
        x - half,
        y + size,
        z - half,
        # Back face
        x - half,
        y,
        z + half,
        x + half,
        y,
        z + half,
        x + half,
        y + size,
        z + half,
        x - half,
        y + size,
        z + half,
    ]

    box_indices = [
        0,
        1,
        2,
        0,
        2,
        3,  # Front
        5,
        4,
        7,
        5,
        7,
        6,  # Back
        4,
        0,
        3,
        4,
        3,
        7,  # Left
        1,
        5,
        6,
        1,
        6,
        2,  # Right
        3,
        2,
        6,
        3,
        6,
        7,  # Top
        4,
        5,
        1,
        4,
        1,
        0,  # Bottom
    ]

    return {"positions": box_positions, "indices": box_indices}


def generate_zone_gltf(
    zone_data: Dict,
    output_path: str,
    texture_path: Optional[str] = None,
    terrain_size: int = 128,
    include_markers: bool = True,
):
    """Generate a glTF file for the zone scene."""

    # Calculate terrain scale based on zone bounds
    bounds = zone_data.get(
        "bounds", {"min": [-50000, -50000, 0], "max": [50000, 50000, 0]}
    )
    min_bounds = bounds["min"]
    max_bounds = bounds["max"]

    world_width = max_bounds[0] - min_bounds[0]
    world_depth = max_bounds[1] - min_bounds[1]

    # Scale terrain to match world coordinates
    scale = max(world_width, world_depth) / terrain_size

    # Generate terrain
    terrain = generate_terrain_mesh(terrain_size, terrain_size, scale)

    # Offset terrain to match world coordinates
    center_x = (min_bounds[0] + max_bounds[0]) / 2
    center_y = (min_bounds[1] + max_bounds[1]) / 2

    # Adjust terrain positions to world space
    for i in range(0, len(terrain["positions"]), 3):
        terrain["positions"][i] += center_x
        terrain["positions"][i + 2] += center_y

    print(f"Terrain: {terrain_size}x{terrain_size}, scale={scale:.1f}")
    print(
        f"World bounds: X[{min_bounds[0]:.0f}, {max_bounds[0]:.0f}] Y[{min_bounds[1]:.0f}, {max_bounds[1]:.0f}]"
    )

    # Generate marker meshes for placed objects
    markers_positions = []
    markers_indices = []

    if include_markers:
        placed_objects = zone_data.get("placed_objects", [])
        marker_size = scale * 2  # Size relative to terrain scale

        for obj in placed_objects:
            pos = obj["position"]
            # Swap Y and Z for glTF (Y-up)
            marker = generate_marker_mesh(pos[0], pos[2], pos[1], marker_size)

            # Offset indices
            base_idx = len(markers_positions) // 3
            for idx in marker["indices"]:
                markers_indices.append(idx + base_idx)
            markers_positions.extend(marker["positions"])

        print(f"Markers: {len(placed_objects)} objects")

    # Build glTF
    nodes = []
    meshes = []
    accessors = []
    buffer_views = []
    materials = []

    buffer_data = bytearray()

    # --- Terrain mesh ---
    terrain_pos_bytes = struct.pack(
        f'<{len(terrain["positions"])}f', *terrain["positions"]
    )
    terrain_normal_bytes = struct.pack(
        f'<{len(terrain["normals"])}f', *terrain["normals"]
    )
    terrain_uv_bytes = struct.pack(f'<{len(terrain["uvs"])}f', *terrain["uvs"])

    terrain_vertex_count = len(terrain["positions"]) // 3
    if terrain_vertex_count <= 65535:
        terrain_idx_bytes = struct.pack(
            f'<{len(terrain["indices"])}H', *terrain["indices"]
        )
        terrain_idx_type = 5123
    else:
        terrain_idx_bytes = struct.pack(
            f'<{len(terrain["indices"])}I', *terrain["indices"]
        )
        terrain_idx_type = 5125

    # Calculate terrain bounds
    terrain_min = [min(terrain["positions"][i::3]) for i in range(3)]
    terrain_max = [max(terrain["positions"][i::3]) for i in range(3)]

    # Add terrain buffer views
    terrain_pos_offset = len(buffer_data)
    buffer_data.extend(terrain_pos_bytes)
    terrain_normal_offset = len(buffer_data)
    buffer_data.extend(terrain_normal_bytes)
    terrain_uv_offset = len(buffer_data)
    buffer_data.extend(terrain_uv_bytes)
    terrain_idx_offset = len(buffer_data)
    buffer_data.extend(terrain_idx_bytes)

    buffer_views.extend(
        [
            {
                "buffer": 0,
                "byteOffset": terrain_pos_offset,
                "byteLength": len(terrain_pos_bytes),
                "target": 34962,
            },
            {
                "buffer": 0,
                "byteOffset": terrain_normal_offset,
                "byteLength": len(terrain_normal_bytes),
                "target": 34962,
            },
            {
                "buffer": 0,
                "byteOffset": terrain_uv_offset,
                "byteLength": len(terrain_uv_bytes),
                "target": 34962,
            },
            {
                "buffer": 0,
                "byteOffset": terrain_idx_offset,
                "byteLength": len(terrain_idx_bytes),
                "target": 34963,
            },
        ]
    )

    accessors.extend(
        [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": terrain_vertex_count,
                "type": "VEC3",
                "min": terrain_min,
                "max": terrain_max,
            },
            {
                "bufferView": 1,
                "componentType": 5126,
                "count": terrain_vertex_count,
                "type": "VEC3",
            },
            {
                "bufferView": 2,
                "componentType": 5126,
                "count": terrain_vertex_count,
                "type": "VEC2",
            },
            {
                "bufferView": 3,
                "componentType": terrain_idx_type,
                "count": len(terrain["indices"]),
                "type": "SCALAR",
            },
        ]
    )

    # Terrain material
    terrain_material = {
        "name": "terrain_material",
        "pbrMetallicRoughness": {
            "baseColorFactor": [0.4, 0.6, 0.3, 1.0],
            "metallicFactor": 0.0,
            "roughnessFactor": 0.9,
        },
        "doubleSided": True,
    }

    # Add texture if provided
    images = []
    textures = []
    if texture_path and os.path.exists(texture_path):
        with open(texture_path, "rb") as f:
            texture_data = f.read()

        if texture_data[:8] == b"\x89PNG\r\n\x1a\n":
            mime_type = "image/png"
        else:
            mime_type = "image/png"

        texture_offset = len(buffer_data)
        buffer_data.extend(texture_data)

        buffer_views.append(
            {"buffer": 0, "byteOffset": texture_offset, "byteLength": len(texture_data)}
        )

        images.append({"mimeType": mime_type, "bufferView": len(buffer_views) - 1})
        textures.append({"source": 0})
        terrain_material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
        terrain_material["pbrMetallicRoughness"]["baseColorFactor"] = [
            1.0,
            1.0,
            1.0,
            1.0,
        ]

        print(f"Texture: {os.path.basename(texture_path)}")

    materials.append(terrain_material)

    meshes.append(
        {
            "name": "terrain",
            "primitives": [
                {
                    "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                    "indices": 3,
                    "material": 0,
                    "mode": 4,
                }
            ],
        }
    )

    nodes.append({"mesh": 0, "name": "terrain"})

    # --- Marker meshes ---
    if markers_positions:
        marker_pos_bytes = struct.pack(
            f"<{len(markers_positions)}f", *markers_positions
        )
        marker_idx_bytes = struct.pack(f"<{len(markers_indices)}H", *markers_indices)

        marker_pos_offset = len(buffer_data)
        buffer_data.extend(marker_pos_bytes)
        marker_idx_offset = len(buffer_data)
        buffer_data.extend(marker_idx_bytes)

        marker_vertex_count = len(markers_positions) // 3
        marker_min = [min(markers_positions[i::3]) for i in range(3)]
        marker_max = [max(markers_positions[i::3]) for i in range(3)]

        marker_pos_bv = len(buffer_views)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": marker_pos_offset,
                "byteLength": len(marker_pos_bytes),
                "target": 34962,
            }
        )
        marker_idx_bv = len(buffer_views)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": marker_idx_offset,
                "byteLength": len(marker_idx_bytes),
                "target": 34963,
            }
        )

        marker_pos_acc = len(accessors)
        accessors.append(
            {
                "bufferView": marker_pos_bv,
                "componentType": 5126,
                "count": marker_vertex_count,
                "type": "VEC3",
                "min": marker_min,
                "max": marker_max,
            }
        )
        marker_idx_acc = len(accessors)
        accessors.append(
            {
                "bufferView": marker_idx_bv,
                "componentType": 5123,
                "count": len(markers_indices),
                "type": "SCALAR",
            }
        )

        # Marker material (red)
        marker_material_idx = len(materials)
        materials.append(
            {
                "name": "marker_material",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [1.0, 0.2, 0.2, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.5,
                },
            }
        )

        meshes.append(
            {
                "name": "markers",
                "primitives": [
                    {
                        "attributes": {"POSITION": marker_pos_acc},
                        "indices": marker_idx_acc,
                        "material": marker_material_idx,
                        "mode": 4,
                    }
                ],
            }
        )

        nodes.append({"mesh": 1, "name": "markers"})

    # Build final glTF
    buffer_uri = f"data:application/octet-stream;base64,{base64.b64encode(bytes(buffer_data)).decode()}"

    gltf = {
        "asset": {"version": "2.0", "generator": "generate_zone_scene.py"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"uri": buffer_uri, "byteLength": len(buffer_data)}],
    }

    if images:
        gltf["images"] = images
    if textures:
        gltf["textures"] = textures

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(gltf, f)

    print(f"Saved zone scene to {output_path}")
    return True


def main():
    import sys

    zone_name = sys.argv[1] if len(sys.argv) > 1 else "chunk_n10_n10"

    zone_data_path = os.path.join(config.ZONES_DIR, zone_name, "zone_data.json")
    texture_dir = os.path.join(config.ZONES_DIR, zone_name, "Texture")
    texture_path = os.path.join(texture_dir, f"{zone_name.replace('chunk_', '').replace('_', '_')}baseColor.tga")

    # Try alternative texture paths
    if not os.path.exists(texture_path):
        parts = zone_name.split("_")
        if len(parts) >= 3:
            alt_texture = (
                f"output/zones/{zone_name}/Texture/{parts[1]}_{parts[2]}baseColor.tga"
            )
            if os.path.exists(alt_texture):
                texture_path = alt_texture

    output_path = os.path.join(config.ZONES_DIR, zone_name, "zone_scene.gltf")

    if not os.path.exists(zone_data_path):
        print(f"Zone data not found: {zone_data_path}")
        print("Run extract_zone.py first")
        return

    print(f"Generating zone scene for: {zone_name}")

    zone_data = load_zone_data(zone_data_path)
    generate_zone_gltf(zone_data, output_path, texture_path=texture_path)


if __name__ == "__main__":
    main()
