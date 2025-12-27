#!/usr/bin/env python3
"""
Extract terrain from all VGR chunks using direct binary parsing.
Does not require umodel or Wine - parses textures directly from VGR files.

Usage:
    python extract_all_terrain.py --all        # Process all VGR chunk files
    python extract_all_terrain.py --chunk X   # Process single chunk by name
"""

import numpy as np
from PIL import Image
import struct
import json
import base64
import io
import os
import sys
import sqlite3
from pathlib import Path

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from ue2 import UE2Package

# Configuration
DB_PATH = config.DB_PATH
VANGUARD_MAPS = os.path.join(config.ASSETS_PATH, "Maps")
OUTPUT_DIR = config.TERRAIN_GRID_DIR

# Vanguard-specific terrain decoding constants
# See PARSING_GUIDELINES.md for details
COLUMN_SHIFT = 34  # Column de-swizzle offset
HEIGHT_SCALE = 2.4  # Canonical UE2 terrain height scale
TERRAIN_SCALE = 390.625  # Units per pixel (200k world / 512 grid)


def find_texture_exports(pkg, pattern):
    """Find texture exports matching a pattern."""
    results = []
    for exp in pkg.exports:
        if exp["class_name"] == "Texture" and pattern in exp["object_name"]:
            results.append(exp)
    return results


def extract_g16_heightmap(pkg, chunk_name):
    """
    Extract G16 heightmap data directly from binary.
    Returns (heights_array, grid_size) or (None, None) on failure.
    """
    # Find the main heightmap texture (ends with "Height", not "Height_X_Y")
    height_name = f"{chunk_name}Height"
    
    for exp in pkg.exports:
        if exp["class_name"] == "Texture" and exp["object_name"] == height_name:
            tex_data = pkg.get_export_data(exp)
            if not tex_data or len(tex_data) < 1000:
                continue
            
            # Try 512x512 first (524288 bytes = 512*512*2)
            for grid_size in [512, 256]:
                expected_size = grid_size * grid_size * 2
                size_marker = struct.pack("<I", expected_size)
                marker_pos = tex_data.find(size_marker)
                
                if marker_pos != -1:
                    height_start = marker_pos + 4
                    height_data = tex_data[height_start:height_start + expected_size]
                    
                    if len(height_data) == expected_size:
                        # =================================================
                        # VANGUARD G16 HEIGHTMAP DECODE
                        # =================================================
                        # Vanguard uses non-standard byte order and column 
                        # swizzling for heightmaps.
                        #
                        # Standard UE2: height = high_byte << 8 | low_byte
                        # Vanguard:     height = low_byte << 8 | high_byte
                        #
                        # Columns are offset by 34 pixels (de-swizzle required)
                        # =================================================
                        raw_bytes = np.frombuffer(height_data, dtype=np.uint8)
                        low_bytes = raw_bytes[::2].reshape(grid_size, grid_size).astype(np.float64)
                        high_bytes = raw_bytes[1::2].reshape(grid_size, grid_size).astype(np.float64)
                        
                        # De-swizzle columns
                        low_bytes = np.roll(low_bytes, -COLUMN_SHIFT, axis=1)
                        high_bytes = np.roll(high_bytes, -COLUMN_SHIFT, axis=1)
                        
                        # Vanguard byte order: low << 8 | high
                        heights = low_bytes * 256 + high_bytes
                        
                        return heights, grid_size
    
    return None, None


def get_texture_format(pkg, data):
    """Scan texture data for Format property."""
    try:
        format_idx = -1
        for i, name_str in enumerate(pkg.names):
            if name_str == "Format":
                format_idx = i
                break
        if format_idx == -1: return None
        encoded = b''
        if format_idx < 0x40: encoded = bytes([format_idx])
        elif format_idx < 0x2000: encoded = bytes([(format_idx & 0x3F) | 0x40, (format_idx >> 6) & 0x7F])
        pos = data.find(encoded, 0, 300)
        if pos != -1:
            info_pos = pos + len(encoded)
            if info_pos < len(data) and (data[info_pos] & 0x0F) == 1:
                return data[info_pos + 1]
    except Exception: pass
    return None

def decode_dxt5(data, width, height):
    """Decode DXT5 compressed texture to PIL Image."""
    try:
        pixels = bytearray(width * height * 4)
        blocks_x, blocks_y = width // 4, height // 4
        
        for block_y in range(blocks_y):
            for block_x in range(blocks_x):
                block_idx = (block_y * blocks_x + block_x) * 16
                if block_idx + 16 > len(data): break
                
                # Alpha Block (8 bytes)
                a0, a1 = data[block_idx], data[block_idx+1]
                # Read 48 bits of alpha indices (bytes 2-7)
                bits = struct.unpack("<Q", data[block_idx:block_idx+8])[0] >> 16
                
                alphas = [a0, a1]
                if a0 > a1:
                    alphas.extend([((6-i)*a0 + (i+1)*a1)//7 for i in range(6)])
                else:
                    alphas.extend([((4-i)*a0 + (i+1)*a1)//5 for i in range(4)])
                    alphas.extend([0, 255])
                
                # Color Block (8 bytes)
                c_idx = block_idx + 8
                c0 = struct.unpack("<H", data[c_idx:c_idx+2])[0]
                c1 = struct.unpack("<H", data[c_idx+2:c_idx+4])[0]
                
                def decode565(c):
                    return ((c >> 11) & 0x1F) * 255 // 31, ((c >> 5) & 0x3F) * 255 // 63, (c & 0x1F) * 255 // 31
                
                r0, g0, b0 = decode565(c0)
                r1, g1, b1 = decode565(c1)
                
                # DXT3/5 always uses 4-color interpolation (no 1-bit alpha in color block)
                color_table = [
                    (r0, g0, b0), (r1, g1, b1),
                    ((2*r0+r1)//3, (2*g0+g1)//3, (2*b0+b1)//3),
                    ((r0+2*r1)//3, (g0+2*g1)//3, (b0+2*b1)//3)
                ]
                
                c_indices = struct.unpack("<I", data[c_idx+4:c_idx+8])[0]
                
                for py in range(4):
                    y = block_y * 4 + py
                    if y >= height: continue
                    row_offset = (y * width + block_x * 4) * 4
                    for px in range(4):
                        p_idx = py * 4 + px
                        a_val = alphas[(bits >> (3 * p_idx)) & 0x07]
                        c_val = color_table[(c_indices >> (2 * p_idx)) & 0x03]
                        pixels[row_offset + px*4 : row_offset + px*4 + 4] = bytes([c_val[0], c_val[1], c_val[2], a_val])

        return Image.frombytes("RGBA", (width, height), bytes(pixels))
    except Exception: return None

def extract_color_texture(pkg, chunk_name):
    """Extract and decode base color texture from VGR package."""
    for exp in pkg.exports:
        if exp["class_name"] == "Texture" and "baseColor" in exp["object_name"]:
            tex_data = pkg.get_export_data(exp)
            if not tex_data or len(tex_data) < 1000: continue
            
            # 1. Detect format from properties
            fmt = get_texture_format(pkg, tex_data)
            
            # 2. Find Mip 0 by looking for the trailing footer: [USize][VSize][UBits][VBits]
            # Suffix is 10 bytes: 4 (USize), 4 (VSize), 1 (UBits), 1 (VBits)
            # We search backwards to find the footer of the first (largest) mip
            for offset in range(len(tex_data) - 10, 300, -1):
                try:
                    u_size, v_size = struct.unpack("<II", tex_data[offset:offset+8])
                    u_bits, v_bits = tex_data[offset+8], tex_data[offset+9]
                    
                    if u_size in [512, 1024, 2048] and u_size == v_size and (1 << u_bits) == u_size:
                        # Determine data size based on format
                        if fmt == 7 or fmt == 6: # DXT5/3
                            expected_size = u_size * v_size
                        elif fmt == 3: # DXT1
                            expected_size = (u_size * v_size) // 2
                        elif fmt == 5: # RGBA8
                            expected_size = u_size * v_size * 4
                        else:
                            expected_size = 1048576 if u_size == 1024 else (u_size * v_size)
                        
                        data_start = offset - expected_size
                        if data_start >= 4:
                            # Check for serialized size marker prefix
                            serialized_size = struct.unpack("<I", tex_data[data_start-4 : data_start])[0]
                            if serialized_size == expected_size:
                                print(f"  Detected {u_size}x{v_size} texture (format {fmt}) at offset {data_start}")
                                mip_data = tex_data[data_start : offset]
                                
                                img = None
                                if fmt in [7, 6] or (fmt is None and expected_size == u_size * v_size):
                                    img = decode_dxt5(mip_data, u_size, v_size)
                                elif fmt == 3 or (fmt is None and expected_size == (u_size * v_size) // 2):
                                    img = decode_dxt1(mip_data, u_size, v_size)
                                elif fmt == 5 or (fmt is None and expected_size == u_size * v_size * 4):
                                    # RGBA8 (Format 5) usually BGRA in UE2
                                    rgba_array = np.frombuffer(mip_data, dtype=np.uint8).reshape(v_size, u_size, 4)
                                    # Swap B/R for PIL (BGRA -> RGBA)
                                    rgba_array = rgba_array[:, :, [2, 1, 0, 3]]
                                    img = Image.fromarray(rgba_array, "RGBA")
                                
                                if img:
                                    # Vanguard Color Textures are standard DXT/RGBA and do NOT need the 
                                    # 34-pixel column shift that the G16 heightmaps require.
                                    return img
                                break
                except: continue
    return None

def decode_dxt1(data, width, height):
    """Decode DXT1 compressed texture to PIL Image."""
    try:
        pixels = bytearray(width * height * 4)
        blocks_x, blocks_y = width // 4, height // 4
        for block_y in range(blocks_y):
            for block_x in range(blocks_x):
                block_idx = (block_y * blocks_x + block_x) * 8
                if block_idx + 8 > len(data): break
                block = data[block_idx:block_idx + 8]
                c0, c1 = struct.unpack("<H", block[0:2])[0], struct.unpack("<H", block[2:4])[0]
                r0, g0, b0 = ((c0 >> 11) & 0x1F) * 255 // 31, ((c0 >> 5) & 0x3F) * 255 // 63, (c0 & 0x1F) * 255 // 31
                r1, g1, b1 = ((c1 >> 11) & 0x1F) * 255 // 31, ((c1 >> 5) & 0x3F) * 255 // 63, (c1 & 0x1F) * 255 // 31
                if c0 > c1:
                    colors = [(r0, g0, b0, 255), (r1, g1, b1, 255), 
                              ((2*r0+r1)//3, (2*g0+g1)//3, (2*b0+b1)//3, 255),
                              ((r0+2*r1)//3, (g0+2*g1)//3, (b0+2*b1)//3, 255)]
                else:
                    colors = [(r0, g0, b0, 255), (r1, g1, b1, 255),
                              ((r0+r1)//2, (g0+g1)//2, (b0+b1)//2, 255), (0, 0, 0, 0)]
                indices = struct.unpack("<I", block[4:8])[0]
                for py in range(4):
                    y = block_y * 4 + py
                    if y >= height: continue
                    row_offset = (y * width + block_x * 4) * 4
                    for px in range(4):
                        idx = (indices >> (2 * (py * 4 + px))) & 0x3
                        pixels[row_offset + px*4 : row_offset + px*4 + 4] = bytes(colors[idx])
        return Image.frombytes("RGBA", (width, height), bytes(pixels))
    except Exception: return None



def generate_terrain_gltf(heights, color_image, output_path, chunk_name, grid_size=512):
    """Generate a glTF terrain mesh with texture."""
    
    # Generate placeholder green texture if no color image
    if color_image is None:
        color_image = Image.new("RGB", (grid_size, grid_size), (100, 140, 80))
    
    # Vectorized vertex generation
    y_coords, x_coords = np.meshgrid(
        np.arange(grid_size), np.arange(grid_size), indexing="ij"
    )
    vx = x_coords.flatten() * TERRAIN_SCALE
    vy = heights.flatten() * HEIGHT_SCALE
    vz = y_coords.flatten() * TERRAIN_SCALE
    vertices_arr = np.column_stack([vx, vy, vz]).astype(np.float32)
    
    # UVs
    u = x_coords.flatten() / (grid_size - 1)
    v = y_coords.flatten() / (grid_size - 1)
    uvs_arr = np.column_stack([u, v]).astype(np.float32)
    
    # Normals
    h_left = np.roll(heights, 1, axis=1)
    h_left[:, 0] = heights[:, 0]
    h_right = np.roll(heights, -1, axis=1)
    h_right[:, -1] = heights[:, -1]
    h_up = np.roll(heights, 1, axis=0)
    h_up[0, :] = heights[0, :]
    h_down = np.roll(heights, -1, axis=0)
    h_down[-1, :] = heights[-1, :]
    
    dx = (h_right - h_left) * HEIGHT_SCALE / (2 * TERRAIN_SCALE)
    dz = (h_down - h_up) * HEIGHT_SCALE / (2 * TERRAIN_SCALE)
    nx = -dx.flatten()
    ny = np.ones_like(nx)
    nz = -dz.flatten()
    lengths = np.sqrt(nx * nx + ny * ny + nz * nz)
    normals_arr = np.column_stack([nx / lengths, ny / lengths, nz / lengths]).astype(np.float32)
    
    # Indices
    y_idx, x_idx = np.meshgrid(
        np.arange(grid_size - 1), np.arange(grid_size - 1), indexing="ij"
    )
    i0 = (y_idx * grid_size + x_idx).flatten()
    indices_arr = np.column_stack([
        i0, i0 + grid_size, i0 + 1,
        i0 + 1, i0 + grid_size, i0 + grid_size + 1
    ]).flatten().astype(np.uint32)
    
    # Pack buffers
    vertices_bin = vertices_arr.tobytes()
    normals_bin = normals_arr.tobytes()
    uvs_bin = uvs_arr.tobytes()
    indices_bin = indices_arr.tobytes()
    buffer_data = vertices_bin + normals_bin + uvs_bin + indices_bin
    
    # Texture
    buf = io.BytesIO()
    color_image.save(buf, format="PNG")
    texture_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    
    v_min = vertices_arr.min(axis=0).tolist()
    v_max = vertices_arr.max(axis=0).tolist()
    
    gltf = {
        "asset": {"version": "2.0", "generator": "extract_all_terrain.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": chunk_name}],
        "meshes": [{
            "primitives": [{
                "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                "indices": 3,
                "material": 0
            }]
        }],
        "materials": [{
            "pbrMetallicRoughness": {
                "baseColorTexture": {"index": 0},
                "metallicFactor": 0.0,
                "roughnessFactor": 1.0
            }
        }],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}],
        "images": [{"uri": f"data:image/png;base64,{texture_b64}"}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(vertices_arr), "type": "VEC3", "min": v_min, "max": v_max},
            {"bufferView": 1, "componentType": 5126, "count": len(normals_arr), "type": "VEC3"},
            {"bufferView": 2, "componentType": 5126, "count": len(uvs_arr), "type": "VEC2"},
            {"bufferView": 3, "componentType": 5125, "count": len(indices_arr), "type": "SCALAR"}
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(vertices_bin)},
            {"buffer": 0, "byteOffset": len(vertices_bin), "byteLength": len(normals_bin)},
            {"buffer": 0, "byteOffset": len(vertices_bin) + len(normals_bin), "byteLength": len(uvs_bin)},
            {"buffer": 0, "byteOffset": len(vertices_bin) + len(normals_bin) + len(uvs_bin), "byteLength": len(indices_bin)}
        ],
        "buffers": [{
            "uri": f"data:application/octet-stream;base64,{base64.b64encode(buffer_data).decode('ascii')}",
            "byteLength": len(buffer_data)
        }]
    }
    
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(gltf, f)
    
    return True


def process_chunk(chunk_name, output_dir, conn=None):
    """Process a single chunk by name."""
    vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk_name}.vgr")
    
    if not os.path.exists(vgr_path):
        return None
    
    print(f"  {chunk_name}...", end=" ", flush=True)
    
    try:
        pkg = UE2Package(vgr_path)
        
        # Extract heightmap
        heights, grid_size = extract_g16_heightmap(pkg, chunk_name)
        if heights is None:
            print("NO HEIGHTMAP")
            return None
        
        # Extract color texture (optional)
        color_image = extract_color_texture(pkg, chunk_name)
        
        # Generate glTF
        output_path = os.path.join(output_dir, f"{chunk_name}_terrain.gltf")
        generate_terrain_gltf(heights, color_image, output_path, chunk_name, grid_size)
        
        # Save to database
        if conn:
            try:
                cursor = conn.cursor()
                chunk_row = cursor.execute(
                    "SELECT id FROM chunks WHERE filename = ? OR filename = ?",
                    (chunk_name, chunk_name + ".vgr")
                ).fetchone()
                
                if chunk_row:
                    cursor.execute("""
                        INSERT OR REPLACE INTO terrain_chunks 
                        (chunk_id, grid_size, gltf_exported, export_path)
                        VALUES (?, ?, 1, ?)
                    """, (chunk_row[0], grid_size, output_path))
                    conn.commit()
            except Exception:
                pass
        
        color_status = "with texture" if color_image else "no texture"
        print(f"OK ({grid_size}x{grid_size}, {color_status})")
        
        return {"chunk_name": chunk_name, "grid_size": grid_size}
    
    except Exception as e:
        print(f"ERROR: {e}")
        return None


def get_all_chunks():
    """Get list of all chunk VGR files."""
    if not os.path.exists(VANGUARD_MAPS):
        return []
    return sorted([
        f.replace(".vgr", "") 
        for f in os.listdir(VANGUARD_MAPS) 
        if f.startswith("chunk_") and f.endswith(".vgr")
    ])


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract terrain from VGR chunks (no umodel)")
    parser.add_argument("--all", action="store_true", help="Process all chunk files")
    parser.add_argument("--chunk", type=str, help="Process single chunk by name")
    args = parser.parse_args()
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH) if os.path.exists(DB_PATH) else None
    
    print("=" * 60)
    print("Terrain Extractor (Binary Parsing)")
    print("=" * 60)
    print(f"Output: {OUTPUT_DIR}")
    print()
    
    successful = []
    failed = []
    
    if args.all:
        chunks = get_all_chunks()
        print(f"Processing {len(chunks)} chunks...")
        print()
        
        for chunk in chunks:
            result = process_chunk(chunk, OUTPUT_DIR, conn)
            if result:
                successful.append(result)
            else:
                failed.append(chunk)
    
    elif args.chunk:
        result = process_chunk(args.chunk, OUTPUT_DIR, conn)
        if result:
            successful.append(result)
        else:
            failed.append(args.chunk)
    
    else:
        print("Usage:")
        print("  python extract_all_terrain.py --all")
        print("  python extract_all_terrain.py --chunk chunk_n25_26")
        return
    
    if conn:
        conn.close()
    
    print()
    print("=" * 60)
    print("Complete")
    print("=" * 60)
    print(f"Successful: {len(successful)}")
    print(f"Failed: {len(failed)}")


if __name__ == "__main__":
    main()
