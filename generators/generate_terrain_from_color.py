#!/usr/bin/env python3
"""
Generate terrain mesh from zone color texture.
Uses luminance as height approximation until proper heightmap extraction is working.
"""

import struct
import json
import base64
import os
from pathlib import Path
from typing import Optional
import sys

# Add parent directory to path to allow importing config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("Warning: PIL not available, using fallback")


def load_image_as_heightmap(image_path: str, height_scale: float = 50.0) -> tuple:
    """Load an image and convert to heightmap grid."""
    if not HAS_PIL:
        return None, 0, 0

    img = Image.open(image_path)

    # Convert to grayscale for height
    gray = img.convert("L")
    width, height = gray.size

    # Downsample for reasonable mesh size
    target_size = 256
    if width > target_size:
        scale_factor = target_size / width
        new_size = (target_size, int(height * scale_factor))
        gray = gray.resize(new_size, Image.Resampling.BILINEAR)
        width, height = gray.size

    # Convert to height grid
    pixels = list(gray.getdata())
    grid = []
    for y in range(height):
        row = []
        for x in range(width):
            # Luminance 0-255 -> height
            lum = pixels[y * width + x]
            h = lum * height_scale / 255.0
            row.append(h)
        grid.append(row)

    return grid, width, height


def generate_terrain_gltf(
    grid: list,
    width: int,
    height: int,
    output_path: str,
    texture_path: Optional[str] = None,
    scale: float = 4.0,
):
    """Generate glTF terrain mesh from height grid."""

    positions = []
    uvs = []
    normals = []

    # Generate vertices
    for z in range(height):
        for x in range(width):
            h = grid[z][x]
            px = (x - width / 2) * scale
            py = h
            pz = (z - height / 2) * scale
            positions.extend([px, py, pz])
            uvs.extend([x / (width - 1), 1.0 - z / (height - 1)])

    # Generate indices
    indices = []
    for z in range(height - 1):
        for x in range(width - 1):
            i0 = z * width + x
            i1 = i0 + 1
            i2 = i0 + width
            i3 = i2 + 1
            indices.extend([i0, i2, i1])
            indices.extend([i1, i2, i3])

    # Calculate normals (simple per-vertex normals)
    vertex_normals = [[0, 0, 0] for _ in range(len(positions) // 3)]
    for i in range(0, len(indices), 3):
        i0, i1, i2 = indices[i], indices[i + 1], indices[i + 2]

        p0 = positions[i0 * 3 : i0 * 3 + 3]
        p1 = positions[i1 * 3 : i1 * 3 + 3]
        p2 = positions[i2 * 3 : i2 * 3 + 3]

        # Calculate face normal
        v1 = [p1[j] - p0[j] for j in range(3)]
        v2 = [p2[j] - p0[j] for j in range(3)]
        normal = [
            v1[1] * v2[2] - v1[2] * v2[1],
            v1[2] * v2[0] - v1[0] * v2[2],
            v1[0] * v2[1] - v1[1] * v2[0],
        ]

        # Add to vertex normals
        for idx in [i0, i1, i2]:
            for j in range(3):
                vertex_normals[idx][j] += normal[j]

    # Normalize
    for n in vertex_normals:
        length = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5
        if length > 0:
            n[0] /= length
            n[1] /= length
            n[2] /= length
        else:
            n[1] = 1.0
        normals.extend(n)

    print(f"Generated terrain: {width}x{height}")
    print(f"  Vertices: {len(positions) // 3}")
    print(f"  Triangles: {len(indices) // 3}")

    # Calculate bounds
    min_pos = [min(positions[i::3]) for i in range(3)]
    max_pos = [max(positions[i::3]) for i in range(3)]

    # Pack binary data
    position_bytes = struct.pack(f"<{len(positions)}f", *positions)
    normal_bytes = struct.pack(f"<{len(normals)}f", *normals)
    uv_bytes = struct.pack(f"<{len(uvs)}f", *uvs)

    vertex_count = len(positions) // 3
    if vertex_count <= 65535:
        index_bytes = struct.pack(f"<{len(indices)}H", *indices)
        index_type = 5123  # UNSIGNED_SHORT
    else:
        index_bytes = struct.pack(f"<{len(indices)}I", *indices)
        index_type = 5125  # UNSIGNED_INT

    # Build buffer
    buffer_data = bytearray()
    buffer_data.extend(position_bytes)
    buffer_data.extend(normal_bytes)
    buffer_data.extend(uv_bytes)
    buffer_data.extend(index_bytes)

    buffer_views = [
        {
            "buffer": 0,
            "byteOffset": 0,
            "byteLength": len(position_bytes),
            "target": 34962,
        },
        {
            "buffer": 0,
            "byteOffset": len(position_bytes),
            "byteLength": len(normal_bytes),
            "target": 34962,
        },
        {
            "buffer": 0,
            "byteOffset": len(position_bytes) + len(normal_bytes),
            "byteLength": len(uv_bytes),
            "target": 34962,
        },
        {
            "buffer": 0,
            "byteOffset": len(position_bytes) + len(normal_bytes) + len(uv_bytes),
            "byteLength": len(index_bytes),
            "target": 34963,
        },
    ]

    # Add texture if provided
    images = []
    textures = []
    material = {
        "name": "terrain_material",
        "pbrMetallicRoughness": {
            "baseColorFactor": [0.5, 0.7, 0.4, 1.0],
            "metallicFactor": 0.0,
            "roughnessFactor": 0.9,
        },
        "doubleSided": True,
    }

    if texture_path and os.path.exists(texture_path):
        with open(texture_path, "rb") as f:
            texture_data = f.read()

        # Detect mime type
        if texture_data[:8] == b"\x89PNG\r\n\x1a\n":
            mime_type = "image/png"
        elif texture_data[:3] == b"\xff\xd8\xff":
            mime_type = "image/jpeg"
        else:
            mime_type = "image/png"  # Assume PNG for TGA files that are actually PNG

        texture_offset = len(buffer_data)
        buffer_data.extend(texture_data)

        buffer_views.append(
            {"buffer": 0, "byteOffset": texture_offset, "byteLength": len(texture_data)}
        )

        images.append({"mimeType": mime_type, "bufferView": len(buffer_views) - 1})
        textures.append({"source": 0})
        material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
        material["pbrMetallicRoughness"]["baseColorFactor"] = [1.0, 1.0, 1.0, 1.0]

        print(
            f"  Texture: {os.path.basename(texture_path)} ({len(texture_data)} bytes)"
        )

    buffer_uri = f"data:application/octet-stream;base64,{base64.b64encode(bytes(buffer_data)).decode()}"

    # Build glTF
    gltf = {
        "asset": {"version": "2.0", "generator": "generate_terrain_from_color.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "terrain"}],
        "meshes": [
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
        ],
        "materials": [material],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": vertex_count,
                "type": "VEC3",
                "min": min_pos,
                "max": max_pos,
            },
            {
                "bufferView": 1,
                "componentType": 5126,
                "count": vertex_count,
                "type": "VEC3",
            },
            {
                "bufferView": 2,
                "componentType": 5126,
                "count": vertex_count,
                "type": "VEC2",
            },
            {
                "bufferView": 3,
                "componentType": index_type,
                "count": len(indices),
                "type": "SCALAR",
            },
        ],
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

    print(f"Saved to {output_path}")
    return True


def main():
    import sys

    if len(sys.argv) < 2:
        # Default: use chunk_n10_n10 baseColor
        texture_path = "output/zones/chunk_n10_n10/Texture/n10_n10baseColor.tga"
    else:
        texture_path = sys.argv[1]

    default_output = os.path.join(config.TERRAIN_DIR, "terrain_from_color.gltf")
    output_path = sys.argv[2] if len(sys.argv) > 2 else default_output

    print(f"Generating terrain from: {texture_path}")

    # Load image as heightmap
    grid, width, height = load_image_as_heightmap(texture_path, height_scale=100.0)

    if grid:
        # Generate terrain with the same texture applied
        generate_terrain_gltf(
            grid, width, height, output_path, texture_path=texture_path
        )
    else:
        print("Failed to load image")


if __name__ == "__main__":
    main()
