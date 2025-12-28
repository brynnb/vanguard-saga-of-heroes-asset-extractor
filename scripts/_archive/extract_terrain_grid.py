#!/usr/bin/env python3
"""
Extract terrain for a grid of chunks and output to a single folder.
Also generates a combined scene with all chunks positioned correctly.
"""

import numpy as np
from PIL import Image, ImageFilter
import struct
import json
import base64
import io
import os
import subprocess
import sys

# Add parent directory to path to allow importing ue2 package (and config)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

# Point to archived UMODEL
VANGUARD_MAPS = os.path.join(config.ASSETS_PATH, "Maps")
DB_PATH = config.DB_PATH
# Use dynamic path based on this script location (renderer/extractors/ -> renderer/)
RENDERER_DIR = config.RENDERER_ROOT

# Edge wrap fix - column shift to fix 5% edge wrap issue
COLUMN_SHIFT = 34

# Default grid parameters - from (-30, 27) top-left to (-21, 25) bottom-right
# X ranges from -30 to -21 (10 chunks)
# Y ranges from 25 to 27 (3 chunks)
DEFAULT_X_MIN, DEFAULT_X_MAX = -30, -21
DEFAULT_Y_MIN, DEFAULT_Y_MAX = 25, 27

# Output folder
OUTPUT_FOLDER = config.TERRAIN_GRID_DIR

import sqlite3


def get_all_chunk_vgr_files():
    """Get all VGR files from the Maps directory."""
    if not os.path.exists(VANGUARD_MAPS):
        return []
    return sorted([
        f for f in os.listdir(VANGUARD_MAPS)
        if f.endswith('.vgr') and f.startswith('chunk_')
    ])


def save_terrain_to_db(conn, chunk_name, heightmap_offset, heightmap_size, grid_size, export_path):
    """Save terrain extraction results to database."""
    cursor = conn.cursor()
    
    # Get chunk_id from chunks table
    chunk_row = cursor.execute(
        "SELECT id FROM chunks WHERE filename = ? OR filename = ?",
        (chunk_name, chunk_name + ".vgr")
    ).fetchone()
    
    if not chunk_row:
        print(f"    Warning: Chunk {chunk_name} not found in chunks table")
        return
    
    chunk_id = chunk_row[0]
    
    # Insert or update terrain record
    cursor.execute("""
        INSERT OR REPLACE INTO terrain_chunks 
        (chunk_id, heightmap_offset, heightmap_size, grid_size, gltf_exported, export_path)
        VALUES (?, ?, ?, ?, 1, ?)
    """, (chunk_id, heightmap_offset, heightmap_size, grid_size, export_path))
    
    conn.commit()


def extract_textures(chunk_name, vgr_path, output_dir):
    """Extract textures using umodel."""
    os.makedirs(output_dir, exist_ok=True)
    cmd = [
        "-export",
        "-game=vang",
        f"-out={output_dir}",
        vgr_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=RENDERER_DIR)
    return result.returncode == 0


def find_heightmap_offset(vgr_path, chunk_name):
    """Find the heightmap texture offset using umodel -list."""
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=RENDERER_DIR)

    # Look for the main heightmap texture: "Texture chunk_n25_26Height" (ends with Height, no underscore after)
    height_name = f"{chunk_name}Height"
    for line in result.stdout.split("\n"):
        if "Texture" in line and height_name in line:
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "Texture" and i + 1 < len(parts):
                    tex_name = parts[i + 1]
                    # Must end exactly with "Height" (not Height_X_Y)
                    if tex_name == height_name:
                        offset_hex = parts[i - 2]
                        size_hex = parts[i - 1]
                        return int(offset_hex, 16), int(size_hex, 16)
    return None, None


def extract_heights(vgr_path, offset, size):
    """Extract and process height data from the heightmap texture."""
    with open(vgr_path, "rb") as f:
        data = f.read()

    tex_data = data[offset : offset + size]

    # Try 512x512 first (524288 bytes = 512*512*2)
    size_marker_512 = struct.pack("<I", 524288)
    marker_pos = tex_data.find(size_marker_512)
    grid_size = 512

    # If not found, try 256x256 (131072 bytes = 256*256*2)
    if marker_pos == -1:
        size_marker_256 = struct.pack("<I", 131072)
        marker_pos = tex_data.find(size_marker_256)
        grid_size = 256

    if marker_pos == -1:
        return None, 512  # Return None with default grid size

    expected_size = grid_size * grid_size * 2
    height_start = marker_pos + 4
    height_data = tex_data[height_start : height_start + expected_size]

    if len(height_data) < expected_size:
        return None, grid_size

    # ==========================================================================
    # VANGUARD G16 HEIGHTMAP DECODE
    # ==========================================================================
    # Vanguard uses a non-standard G16 byte order and column-swizzled layout.
    # This is engine-specific, not generic UE2.
    #
    # Standard UE2 G16: height = high_byte << 8 | low_byte (little-endian uint16)
    # Vanguard G16:     height = low_byte << 8 | high_byte (swapped)
    #
    # Additionally, columns are stored with a 34-pixel offset (de-swizzle required).
    # This is likely due to per-sector padding or cache-friendly striping.
    # ==========================================================================

    raw_bytes = np.frombuffer(height_data, dtype=np.uint8)
    low_bytes = raw_bytes[::2].reshape(grid_size, grid_size).astype(np.float64)
    high_bytes = raw_bytes[1::2].reshape(grid_size, grid_size).astype(np.float64)

    # De-swizzle: roll columns by COLUMN_SHIFT to restore correct alignment
    low_bytes = np.roll(low_bytes, -COLUMN_SHIFT, axis=1)
    high_bytes = np.roll(high_bytes, -COLUMN_SHIFT, axis=1)

    # Vanguard byte order: low << 8 | high
    heights = low_bytes * 256 + high_bytes

    return heights, grid_size


def find_color_texture(output_dir, chunk_name):
    """Find the base color texture in the extracted files."""
    for root, dirs, files in os.walk(output_dir):
        for f in files:
            if "baseColor" in f and f.endswith(".tga"):
                return os.path.join(root, f)
    return None


def generate_terrain_gltf(
    heights,
    color_texture_path,
    output_path,
    chunk_name,
    grid_size=512,
    x_offset=0,
    z_offset=0,
    height_scale=2.4,  # Canonical UE2 terrain scale (DrawScale3D.Z equivalent)
    obj_z_min=None,
    obj_x_span=None,
    obj_y_span=None,
):
    """Generate a glTF terrain mesh with optional world position offset."""
    base_img = Image.open(color_texture_path)

    # Terrain uses ~390 units per pixel to match object coordinate system
    # Objects span roughly -100k to +100k (200k total) for a 512 pixel chunk
    # 200000 / 512 = 390.625 units per pixel
    terrain_x_scale = 390.625
    terrain_z_scale = 390.625

    # Height scale: raw heightmap spans ~55k, object Z spans ~23k
    # Scale factor: ~0.42 to match object Z range
    # But we use dynamic calculation based on actual data for accuracy
    # Y offset aligns terrain min with object Z min
    y_offset = 0
    if obj_z_min is not None:
        terrain_y_min = heights.min() * height_scale
        y_offset = obj_z_min - terrain_y_min

    # Vectorized vertex generation (much faster than Python loops)
    y_coords, x_coords = np.meshgrid(
        np.arange(grid_size), np.arange(grid_size), indexing="ij"
    )
    vx = x_coords.flatten() * terrain_x_scale + x_offset
    vy = heights.flatten() * height_scale + y_offset
    vz = y_coords.flatten() * terrain_z_scale + z_offset
    vertices_arr = np.column_stack([vx, vy, vz]).astype(np.float32)

    # Vectorized UV generation
    u = x_coords.flatten() / (grid_size - 1)
    v = y_coords.flatten() / (grid_size - 1)
    uvs_arr = np.column_stack([u, v]).astype(np.float32)

    # Vectorized normal calculation using numpy roll
    h_left = np.roll(heights, 1, axis=1)
    h_left[:, 0] = heights[:, 0]
    h_right = np.roll(heights, -1, axis=1)
    h_right[:, -1] = heights[:, -1]
    h_up = np.roll(heights, 1, axis=0)
    h_up[0, :] = heights[0, :]
    h_down = np.roll(heights, -1, axis=0)
    h_down[-1, :] = heights[-1, :]

    dx = (h_right - h_left) * height_scale / (2 * terrain_x_scale)
    dz = (h_down - h_up) * height_scale / (2 * terrain_z_scale)
    nx = -dx.flatten()
    ny = np.ones_like(nx)
    nz = -dz.flatten()
    lengths = np.sqrt(nx * nx + ny * ny + nz * nz)
    normals_arr = np.column_stack([nx / lengths, ny / lengths, nz / lengths]).astype(
        np.float32
    )

    # Vectorized index generation
    y_idx, x_idx = np.meshgrid(
        np.arange(grid_size - 1), np.arange(grid_size - 1), indexing="ij"
    )
    i0 = (y_idx * grid_size + x_idx).flatten()
    indices_arr = (
        np.column_stack(
            [i0, i0 + grid_size, i0 + 1, i0 + 1, i0 + grid_size, i0 + grid_size + 1]
        )
        .flatten()
        .astype(np.uint32)
    )

    # Convert to binary (numpy tobytes is much faster than struct.pack)
    vertices_bin = vertices_arr.tobytes()
    normals_bin = normals_arr.tobytes()
    uvs_bin = uvs_arr.tobytes()
    indices_bin = indices_arr.tobytes()
    buffer_data = vertices_bin + normals_bin + uvs_bin + indices_bin

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
                "count": len(vertices_arr),
                "type": "VEC3",
                "min": v_min,
                "max": v_max,
            },
            {
                "bufferView": 1,
                "componentType": 5126,
                "count": len(normals_arr),
                "type": "VEC3",
            },
            {
                "bufferView": 2,
                "componentType": 5126,
                "count": len(uvs_arr),
                "type": "VEC2",
            },
            {
                "bufferView": 3,
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
                "uri": f"data:application/octet-stream;base64,{base64.b64encode(buffer_data).decode('ascii')}",
                "byteLength": len(buffer_data),
            }
        ],
    }

    with open(output_path, "w") as f:
        json.dump(gltf, f)

    return v_min, v_max


def get_object_bounds(vgr_path):
    """Extract object coordinate bounds from StaticMeshActor placements."""
    try:
        # Import the parser from generate_objects_scene
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "generate_objects_scene",
            os.path.join(RENDERER_DIR, "generators/generate_objects_scene.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        placements = module.parse_chunk_placements(vgr_path)
        if placements:
            x_values = [p["x"] for p in placements]
            y_values = [p["y"] for p in placements]
            z_values = [p["z"] for p in placements]
            return {
                "x_min": min(x_values),
                "x_max": max(x_values),
                "y_min": min(y_values),
                "y_max": max(y_values),
                "z_min": min(z_values),
                "z_max": max(z_values),
            }
    except Exception as e:
        print(f"    Warning: Could not get object bounds: {e}")
    return None


def process_chunk(x, y, output_folder, conn=None):
    """Process a single chunk and return success status and bounds."""
    # Convert coordinates to chunk name format
    # Both X and Y use 'n' prefix for negative values
    x_str = f"n{abs(x)}" if x < 0 else str(x)
    y_str = f"n{abs(y)}" if y < 0 else str(y)
    chunk_name = f"chunk_{x_str}_{y_str}"
    
    result = process_chunk_by_name(chunk_name, output_folder, conn)
    if result:
        result["x"] = x
        result["y"] = y
    return result


def process_chunk_by_name(chunk_name, output_folder, conn=None):
    """Process a chunk by name and return success status."""
    vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk_name}.vgr")

    if not os.path.exists(vgr_path):
        print(f"  SKIP: {chunk_name} - file not found")
        return None

    print(f"  Processing {chunk_name}...", end=" ", flush=True)

    # Extract to temp folder (use relative path to avoid absolute path mangling in Wine)
    temp_dir = os.path.join("output", "zones", chunk_name)
    extract_textures(chunk_name, vgr_path, temp_dir)

    # Find heightmap
    offset, size = find_heightmap_offset(vgr_path, chunk_name)
    if offset is None:
        print("ERROR: Could not find heightmap")
        return None

    # Extract heights
    result = extract_heights(vgr_path, offset, size)
    if result[0] is None:
        print("ERROR: Could not extract heights")
        return None
    heights, grid_size = result

    # Find color texture
    color_path = find_color_texture(temp_dir, chunk_name)
    if color_path is None:
        print("ERROR: Could not find color texture")
        return None

    # ==========================================================================
    # TERRAIN HEIGHT SCALE
    # ==========================================================================
    # Use canonical UE2 terrain scale of 2.4 (equivalent to DrawScale3D.Z).
    # ==========================================================================
    height_scale = 2.4

    # Get object bounds for Y offset alignment (optional)
    obj_bounds = get_object_bounds(vgr_path)
    obj_z_min = obj_bounds["z_min"] if obj_bounds else None
    obj_x_span = obj_bounds["x_max"] - obj_bounds["x_min"] if obj_bounds else None
    obj_y_span = obj_bounds["y_max"] - obj_bounds["y_min"] if obj_bounds else None

    # Generate glTF
    gltf_path = os.path.join(output_folder, f"{chunk_name}_terrain.gltf")
    v_min, v_max = generate_terrain_gltf(
        heights,
        color_path,
        gltf_path,
        chunk_name,
        grid_size,
        height_scale=height_scale,
        obj_z_min=obj_z_min,
        obj_x_span=obj_x_span,
        obj_y_span=obj_y_span,
    )

    # Save to database
    if conn:
        save_terrain_to_db(conn, chunk_name, offset, size, grid_size, gltf_path)

    print(f"OK ({grid_size}x{grid_size})")

    return {
        "chunk_name": chunk_name,
        "heights": heights,
        "grid_size": grid_size,
        "color_path": color_path,
        "v_min": v_min,
        "v_max": v_max,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract terrain from VGR chunks")
    parser.add_argument('--all', action='store_true', help="Process all VGR chunk files")
    parser.add_argument('--chunk', type=str, help="Process single chunk by name (e.g., chunk_n25_26)")
    parser.add_argument('x_min', type=int, nargs='?', help="X min for grid mode")
    parser.add_argument('y_min', type=int, nargs='?', help="Y min for grid mode")
    parser.add_argument('x_max', type=int, nargs='?', help="X max for grid mode")
    parser.add_argument('y_max', type=int, nargs='?', help="Y max for grid mode")
    args = parser.parse_args()

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    
    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    
    print("=" * 60)
    print("Terrain Grid Extractor")
    print("=" * 60)
    print(f"Output folder: {OUTPUT_FOLDER}")
    print(f"Database: {DB_PATH}")
    print()

    successful_chunks = []
    failed_chunks = []

    if args.all:
        # Process all VGR files
        vgr_files = get_all_chunk_vgr_files()
        print(f"Processing all {len(vgr_files)} chunk files...")
        print()
        
        for vgr_file in vgr_files:
            chunk_name = vgr_file.replace('.vgr', '')
            result = process_chunk_by_name(chunk_name, OUTPUT_FOLDER, conn)
            if result:
                successful_chunks.append(result)
            else:
                failed_chunks.append(chunk_name)
    
    elif args.chunk:
        # Single chunk by name
        result = process_chunk_by_name(args.chunk, OUTPUT_FOLDER, conn)
        if result:
            successful_chunks.append(result)
        else:
            failed_chunks.append(args.chunk)
    
    elif args.x_min is not None and args.y_min is not None:
        # Grid mode
        X_MIN = args.x_min
        Y_MIN = args.y_min
        X_MAX = args.x_max if args.x_max is not None else X_MIN
        Y_MAX = args.y_max if args.y_max is not None else Y_MIN
        
        print(f"Extracting terrain grid from ({X_MIN}, {Y_MAX}) to ({X_MAX}, {Y_MIN})")
        print()
        
        for x in range(X_MIN, X_MAX + 1):
            for y in range(Y_MIN, Y_MAX + 1):
                result = process_chunk(x, y, OUTPUT_FOLDER, conn)
                if result:
                    successful_chunks.append(result)
                else:
                    failed_chunks.append((x, y))
    else:
        # Default grid
        X_MIN, X_MAX = DEFAULT_X_MIN, DEFAULT_X_MAX
        Y_MIN, Y_MAX = DEFAULT_Y_MIN, DEFAULT_Y_MAX
        
        print(f"Extracting default terrain grid from ({X_MIN}, {Y_MAX}) to ({X_MAX}, {Y_MIN})")
        print()
        
        for x in range(X_MIN, X_MAX + 1):
            for y in range(Y_MIN, Y_MAX + 1):
                result = process_chunk(x, y, OUTPUT_FOLDER, conn)
                if result:
                    successful_chunks.append(result)
                else:
                    failed_chunks.append((x, y))

    conn.close()

    print()
    print("=" * 60)
    print("Extraction Complete")
    print("=" * 60)
    print(f"Successful: {len(successful_chunks)}")
    print(f"Failed: {len(failed_chunks)}")

    # Save manifest
    manifest = {
        "total_chunks": len(successful_chunks) + len(failed_chunks),
        "successful": len(successful_chunks),
        "failed": len(failed_chunks),
        "chunks": [c["chunk_name"] for c in successful_chunks]
    }
    with open(os.path.join(OUTPUT_FOLDER, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Saved manifest.json")


def generate_combined_scene(chunks, output_folder):
    """Generate a single glTF with all chunks positioned in world space."""
    # Use 512x512 chunk size for positioning (standard chunk)
    standard_chunk_size = 512 * 100  # 51200 units per chunk

    # Find the reference point (top-left corner)
    ref_x = min(c["x"] for c in chunks)
    ref_y = max(c["y"] for c in chunks)

    all_nodes = []
    all_meshes = []
    all_materials = []
    all_textures = []
    all_samplers = []
    all_images = []
    all_accessors = []
    all_buffer_views = []
    all_buffers_data = []

    current_buffer_offset = 0

    for i, chunk in enumerate(chunks):
        # Calculate world position relative to reference
        # X increases to the right (less negative = more positive offset)
        # Y increases downward in world space
        world_x = (chunk["x"] - ref_x) * standard_chunk_size
        world_z = (ref_y - chunk["y"]) * standard_chunk_size

        heights = chunk["heights"]
        color_path = chunk["color_path"]
        grid_size = chunk.get("grid_size", 512)

        base_img = Image.open(color_path)
        terrain_scale = 100.0 * (512 / grid_size)
        # Canonical UE2 terrain scale (DrawScale3D.Z equivalent)
        height_scale = 2.4

        # Vectorized mesh generation (same as individual chunks)
        y_coords, x_coords = np.meshgrid(
            np.arange(grid_size), np.arange(grid_size), indexing="ij"
        )
        vx = x_coords.flatten() * terrain_scale + world_x
        vy = heights.flatten() * height_scale
        vz = y_coords.flatten() * terrain_scale + world_z
        vertices_arr = np.column_stack([vx, vy, vz]).astype(np.float32)

        u = x_coords.flatten() / (grid_size - 1)
        v = y_coords.flatten() / (grid_size - 1)
        uvs_arr = np.column_stack([u, v]).astype(np.float32)

        h_left = np.roll(heights, 1, axis=1)
        h_left[:, 0] = heights[:, 0]
        h_right = np.roll(heights, -1, axis=1)
        h_right[:, -1] = heights[:, -1]
        h_up = np.roll(heights, 1, axis=0)
        h_up[0, :] = heights[0, :]
        h_down = np.roll(heights, -1, axis=0)
        h_down[-1, :] = heights[-1, :]

        dx = (h_right - h_left) * height_scale / (2 * terrain_scale)
        dz = (h_down - h_up) * height_scale / (2 * terrain_scale)
        nx = -dx.flatten()
        ny = np.ones_like(nx)
        nz = -dz.flatten()
        lengths = np.sqrt(nx * nx + ny * ny + nz * nz)
        normals_arr = np.column_stack(
            [nx / lengths, ny / lengths, nz / lengths]
        ).astype(np.float32)

        y_idx, x_idx = np.meshgrid(
            np.arange(grid_size - 1), np.arange(grid_size - 1), indexing="ij"
        )
        i0 = (y_idx * grid_size + x_idx).flatten()
        indices_arr = (
            np.column_stack(
                [i0, i0 + grid_size, i0 + 1, i0 + 1, i0 + grid_size, i0 + grid_size + 1]
            )
            .flatten()
            .astype(np.uint32)
        )

        chunk_buffer = (
            vertices_arr.tobytes()
            + normals_arr.tobytes()
            + uvs_arr.tobytes()
            + indices_arr.tobytes()
        )
        all_buffers_data.append(chunk_buffer)

        v_min = vertices_arr.min(axis=0).tolist()
        v_max = vertices_arr.max(axis=0).tolist()

        # Texture
        buf = io.BytesIO()
        base_img.save(buf, format="PNG")
        texture_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        # Add to collections
        accessor_base = len(all_accessors)
        buffer_view_base = len(all_buffer_views)

        vertices_bin = vertices_arr.tobytes()
        normals_bin = normals_arr.tobytes()
        uvs_bin = uvs_arr.tobytes()
        indices_bin = indices_arr.tobytes()

        all_accessors.extend(
            [
                {
                    "bufferView": buffer_view_base,
                    "componentType": 5126,
                    "count": len(vertices_arr),
                    "type": "VEC3",
                    "min": v_min,
                    "max": v_max,
                },
                {
                    "bufferView": buffer_view_base + 1,
                    "componentType": 5126,
                    "count": len(normals_arr),
                    "type": "VEC3",
                },
                {
                    "bufferView": buffer_view_base + 2,
                    "componentType": 5126,
                    "count": len(uvs_arr),
                    "type": "VEC2",
                },
                {
                    "bufferView": buffer_view_base + 3,
                    "componentType": 5125,
                    "count": len(indices_arr),
                    "type": "SCALAR",
                },
            ]
        )

        all_buffer_views.extend(
            [
                {"buffer": i, "byteOffset": 0, "byteLength": len(vertices_bin)},
                {
                    "buffer": i,
                    "byteOffset": len(vertices_bin),
                    "byteLength": len(normals_bin),
                },
                {
                    "buffer": i,
                    "byteOffset": len(vertices_bin) + len(normals_bin),
                    "byteLength": len(uvs_bin),
                },
                {
                    "buffer": i,
                    "byteOffset": len(vertices_bin) + len(normals_bin) + len(uvs_bin),
                    "byteLength": len(indices_bin),
                },
            ]
        )

        all_nodes.append({"mesh": i, "name": chunk["chunk_name"]})
        all_meshes.append(
            {
                "primitives": [
                    {
                        "attributes": {
                            "POSITION": accessor_base,
                            "NORMAL": accessor_base + 1,
                            "TEXCOORD_0": accessor_base + 2,
                        },
                        "indices": accessor_base + 3,
                        "material": i,
                    }
                ]
            }
        )
        all_materials.append(
            {
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": i},
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                }
            }
        )
        all_textures.append({"source": i, "sampler": 0})
        all_images.append({"uri": f"data:image/png;base64,{texture_b64}"})

    all_samplers.append(
        {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}
    )

    # Build buffers list
    all_buffers = []
    for buf_data in all_buffers_data:
        all_buffers.append(
            {
                "uri": f"data:application/octet-stream;base64,{base64.b64encode(buf_data).decode('ascii')}",
                "byteLength": len(buf_data),
            }
        )

    combined_gltf = {
        "asset": {"version": "2.0", "generator": "Vanguard Terrain Grid Generator"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(all_nodes)))}],
        "nodes": all_nodes,
        "meshes": all_meshes,
        "materials": all_materials,
        "textures": all_textures,
        "samplers": all_samplers,
        "images": all_images,
        "accessors": all_accessors,
        "bufferViews": all_buffer_views,
        "buffers": all_buffers,
    }

    combined_path = os.path.join(output_folder, "combined_terrain.gltf")
    with open(combined_path, "w") as f:
        json.dump(combined_gltf, f)

    print(f"Saved combined scene: {combined_path}")
    print(f"  Contains {len(chunks)} chunks")


if __name__ == "__main__":
    main()
