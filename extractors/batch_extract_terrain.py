#!/usr/bin/env python3
"""
Batch extract terrain from Vanguard zone files.
Uses smoothed low byte + high byte method for proper height reconstruction.

Key fixes applied:
- Column shift by ~34 pixels to fix edge wrap issue
- Vertical UV flip to align texture with terrain mesh
- 10x Gaussian blur on low byte for smooth layer transitions
- 25,000 unit height scale for realistic terrain elevation
"""

# Edge wrap fix: heightmap data has ~34 columns that belong on the opposite side
COLUMN_SHIFT = 34

import numpy as np
from PIL import Image, ImageFilter
import struct
import json
import base64
import io
import os
import subprocess
import sys

WINE_PATH = "/Applications/Wine Stable.app/Contents/Resources/wine/bin/wine"
UMODEL_PATH = "gildor2-UEViewer-daa3c14/umodel.exe"
VANGUARD_MAPS = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Maps"
OUTPUT_BASE = "output/zones"
RENDERER_DIR = "/Users/brynnbateman/Documents/GitHub/project-telon/renderer"


def extract_textures(chunk_name, vgr_path, output_dir):
    """Extract textures using umodel."""
    os.makedirs(output_dir, exist_ok=True)

    cmd = [
        WINE_PATH,
        UMODEL_PATH,
        "-export",
        "-game=vang",
        f"-out={output_dir}",
        vgr_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=RENDERER_DIR)
    return result.returncode == 0


def find_heightmap_offset(vgr_path, chunk_name):
    """Find the heightmap texture offset using umodel -list."""
    cmd = [WINE_PATH, UMODEL_PATH, "-list", "-game=vang", vgr_path]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=RENDERER_DIR)

    # Parse output to find the main Height texture (without coordinates)
    # Format: "875   B2A317    800D0 Texture chunk_n25_26Height"
    height_name = f"{chunk_name}Height"

    for line in result.stdout.split("\n"):
        if (
            height_name in line
            and "Texture" in line
            and "_" not in line.split(height_name)[-1][:2]
        ):
            # Found the main heightmap
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "Texture":
                    # Offset is typically 2 positions before 'Texture'
                    offset_hex = parts[i - 2]
                    size_hex = parts[i - 1]
                    return int(offset_hex, 16), int(size_hex, 16)

    return None, None


def extract_heights(vgr_path, offset, size):
    """Extract and process height data from the heightmap texture."""
    with open(vgr_path, "rb") as f:
        data = f.read()

    tex_data = data[offset : offset + size]

    # Find the 524288 size marker (512x512 * 2 bytes)
    size_marker = struct.pack("<I", 524288)
    marker_pos = tex_data.find(size_marker)

    if marker_pos == -1:
        return None

    height_start = marker_pos + 4
    height_data = tex_data[height_start : height_start + 524288]

    if len(height_data) < 524288:
        return None

    raw_bytes = np.frombuffer(height_data, dtype=np.uint8)
    low_bytes = raw_bytes[::2].reshape(512, 512).astype(np.float64)
    high_bytes = raw_bytes[1::2].reshape(512, 512).astype(np.float64)

    # Apply column shift to fix edge wrap issue
    low_bytes = np.roll(low_bytes, -COLUMN_SHIFT, axis=1)
    high_bytes = np.roll(high_bytes, -COLUMN_SHIFT, axis=1)

    # Smooth the low byte heavily to get continuous layer values
    low_img = Image.fromarray(low_bytes.astype(np.uint8))
    for _ in range(10):
        low_img = low_img.filter(ImageFilter.GaussianBlur(radius=2))
    low_smooth = np.array(low_img).astype(np.float64)

    # Combine: smoothed_low * 256 + high_byte
    heights = low_smooth * 256 + high_bytes

    # Light smoothing to remove any remaining discontinuities
    heights_img = Image.fromarray(
        ((heights - heights.min()) / (heights.max() - heights.min()) * 255).astype(
            np.uint8
        )
    )
    for _ in range(2):
        heights_img = heights_img.filter(ImageFilter.GaussianBlur(radius=1))
    heights_smooth = np.array(heights_img).astype(np.float64)

    # Scale back
    heights_final = (
        heights_smooth / 255 * (heights.max() - heights.min()) + heights.min()
    )

    return heights_final


def find_color_texture(output_dir, chunk_name):
    """Find the base color texture in the extracted files."""
    # Extract coordinates from chunk name (e.g., chunk_n25_26 -> n25_26)
    coords = chunk_name.replace("chunk_", "")

    # Look for baseColor texture
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            if "baseColor" in f and f.endswith(".tga"):
                return os.path.join(root, f)

    return None


def generate_terrain_gltf(heights, color_texture_path, output_path, chunk_name):
    """Generate a glTF terrain mesh."""
    base_img = Image.open(color_texture_path)

    grid_size = 512
    terrain_scale = 100.0

    heights_normalized = (heights - heights.min()) / (heights.max() - heights.min())
    height_scale = 25000.0

    vertices = []
    normals = []
    uvs = []

    for y in range(grid_size):
        for x in range(grid_size):
            vertices.extend(
                [
                    x * terrain_scale,
                    heights_normalized[y, x] * height_scale,
                    y * terrain_scale,
                ]
            )
            # Vertical flip applied to align texture with terrain mesh
            uvs.extend([x / (grid_size - 1), y / (grid_size - 1)])

    for y in range(grid_size):
        for x in range(grid_size):
            h_left = heights_normalized[y, max(0, x - 1)]
            h_right = heights_normalized[y, min(grid_size - 1, x + 1)]
            h_up = heights_normalized[max(0, y - 1), x]
            h_down = heights_normalized[min(grid_size - 1, y + 1), x]
            dx = (h_right - h_left) * height_scale / (2 * terrain_scale)
            dz = (h_down - h_up) * height_scale / (2 * terrain_scale)
            nx, ny, nz = -dx, 1.0, -dz
            length = np.sqrt(nx * nx + ny * ny + nz * nz)
            normals.extend([nx / length, ny / length, nz / length])

    indices = []
    for y in range(grid_size - 1):
        for x in range(grid_size - 1):
            i0 = y * grid_size + x
            indices.extend(
                [i0, i0 + grid_size, i0 + 1, i0 + 1, i0 + grid_size, i0 + grid_size + 1]
            )

    vertices_bin = struct.pack(f"<{len(vertices)}f", *vertices)
    normals_bin = struct.pack(f"<{len(normals)}f", *normals)
    uvs_bin = struct.pack(f"<{len(uvs)}f", *uvs)
    indices_bin = struct.pack(f"<{len(indices)}I", *indices)
    buffer_data = vertices_bin + normals_bin + uvs_bin + indices_bin

    vertices_arr = np.array(vertices).reshape(-1, 3)
    v_min = vertices_arr.min(axis=0).tolist()
    v_max = vertices_arr.max(axis=0).tolist()

    buf = io.BytesIO()
    base_img.save(buf, format="PNG")
    texture_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    gltf = {
        "asset": {"version": "2.0", "generator": "Vanguard Terrain Generator"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": chunk_name}],
        "meshes": [
            {
                "primitives": [
                    {
                        "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                        "indices": 3,
                        "material": 0,
                    }
                ]
            }
        ],
        "materials": [
            {
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": 0},
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                }
            }
        ],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [
            {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}
        ],
        "images": [{"uri": f"data:image/png;base64,{texture_b64}"}],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(vertices) // 3,
                "type": "VEC3",
                "min": v_min,
                "max": v_max,
            },
            {
                "bufferView": 1,
                "componentType": 5126,
                "count": len(normals) // 3,
                "type": "VEC3",
            },
            {
                "bufferView": 2,
                "componentType": 5126,
                "count": len(uvs) // 2,
                "type": "VEC2",
            },
            {
                "bufferView": 3,
                "componentType": 5125,
                "count": len(indices),
                "type": "SCALAR",
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(vertices_bin)},
            {
                "buffer": 0,
                "byteOffset": len(vertices_bin),
                "byteLength": len(normals_bin),
            },
            {
                "buffer": 0,
                "byteOffset": len(vertices_bin) + len(normals_bin),
                "byteLength": len(uvs_bin),
            },
            {
                "buffer": 0,
                "byteOffset": len(vertices_bin) + len(normals_bin) + len(uvs_bin),
                "byteLength": len(indices_bin),
            },
        ],
        "buffers": [
            {
                "uri": f'data:application/octet-stream;base64,{base64.b64encode(buffer_data).decode("ascii")}',
                "byteLength": len(buffer_data),
            }
        ],
    }

    with open(output_path, "w") as f:
        json.dump(gltf, f)

    return True


def process_chunk(vgr_path):
    """Process a single chunk file."""
    chunk_name = os.path.basename(vgr_path).replace(".vgr", "")
    output_dir = os.path.join(OUTPUT_BASE, chunk_name)

    print(f"\n{'='*60}")
    print(f"Processing: {chunk_name}")
    print(f"{'='*60}")

    # Step 1: Extract textures
    print(f"  Extracting textures...")
    extract_textures(chunk_name, vgr_path, output_dir)

    # Step 2: Find heightmap offset
    print(f"  Finding heightmap...")
    offset, size = find_heightmap_offset(vgr_path, chunk_name)
    if offset is None:
        print(f"  ERROR: Could not find heightmap for {chunk_name}")
        return False
    print(f"  Heightmap at offset {hex(offset)}, size {hex(size)}")

    # Step 3: Extract heights
    print(f"  Extracting heights...")
    heights = extract_heights(vgr_path, offset, size)
    if heights is None:
        print(f"  ERROR: Could not extract heights for {chunk_name}")
        return False
    print(f"  Height range: {heights.min():.0f} to {heights.max():.0f}")

    # Step 4: Find color texture
    print(f"  Finding color texture...")
    color_path = find_color_texture(output_dir, chunk_name)
    if color_path is None:
        print(f"  ERROR: Could not find color texture for {chunk_name}")
        return False
    print(f"  Color texture: {os.path.basename(color_path)}")

    # Step 5: Generate glTF
    print(f"  Generating terrain mesh...")
    gltf_path = os.path.join(output_dir, f"{chunk_name}_terrain.gltf")
    generate_terrain_gltf(heights, color_path, gltf_path, chunk_name)
    print(f"  Saved: {gltf_path}")

    return True


def main():
    # Get list of chunk files - only process chunks with 'n' prefix (main game world)
    chunk_files = []
    for f in os.listdir(VANGUARD_MAPS):
        if f.startswith("chunk_n") and f.endswith(".vgr"):
            chunk_files.append(os.path.join(VANGUARD_MAPS, f))

    chunk_files.sort()

    # Process specified number of chunks
    num_chunks = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    print(f"Found {len(chunk_files)} chunk files")
    print(f"Processing {num_chunks} chunks...")

    successful = 0
    failed = 0

    for i, vgr_path in enumerate(chunk_files[:num_chunks]):
        try:
            if process_chunk(vgr_path):
                successful += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"COMPLETE: {successful} successful, {failed} failed")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
