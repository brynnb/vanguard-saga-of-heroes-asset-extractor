#!/usr/bin/env python3
"""
Extract heightmap data from Vanguard zone files and generate terrain mesh.
Vanguard uses 16-bit grayscale heightmaps (TEXF_G16 format) stored in zone packages.
"""

import struct
import json
import os
import math
import sys
from pathlib import Path
from typing import List, Tuple, Dict, Optional
import base64

# Add parent directory to path to allow importing ue2 package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ue2 import UE2Package
import config


class HeightmapExtractor:
    """Extract and process heightmap data from Vanguard zone files."""

    def __init__(self, zone_path: str):
        self.zone_path = zone_path
        self.pkg = UE2Package(zone_path)

    # Removed read_compact_index and read_fstring (used internal UE2Package now)
    # Removed parse_package (UE2Package handles this)

    def find_heightmap_textures(self) -> List[Dict]:
        """Find all heightmap texture exports."""
        heightmaps = []
    def find_heightmap_textures(self) -> List[Dict]:
        """Find all heightmap texture exports."""
        heightmaps = []
        for exp in self.pkg.exports:
            if "Height" in exp["object_name"] and exp["serial_size"] > 0:
                heightmaps.append(exp)
        return heightmaps

    def extract_g16_texture(self, export: Dict) -> Optional[List[int]]:
        """
        Extract 16-bit grayscale texture data.
        Returns list of height values (0-65535).
        """
        offset = export["serial_offset"]
        size = export["serial_size"]

        if size <= 0:
            return None

        if size <= 0:
            return None

        # Read the texture data
        tex_data = self.pkg.get_export_data(export)

        # UE2 texture format: properties followed by mip data
        # Skip properties and find the raw pixel data
        # The texture is likely 128x128 or 256x256 16-bit values

        # Try to find the mip data - look for size that matches expected dimensions
        # 128x128 * 2 bytes = 32768 bytes
        # 256x256 * 2 bytes = 131072 bytes

        # The data size is 32979 bytes, which is close to 32768 + some header
        # Let's try to extract the last 32768 bytes as 128x128 16-bit heightmap

        expected_size = 128 * 128 * 2  # 32768 bytes for 128x128 16-bit

        if len(tex_data) >= expected_size:
            # Try reading from the end (mip data is usually at the end)
            raw_start = len(tex_data) - expected_size
            raw_data = tex_data[raw_start:]

            heights = []
            for i in range(0, len(raw_data), 2):
                if i + 2 <= len(raw_data):
                    height = struct.unpack("<H", raw_data[i : i + 2])[0]
                    heights.append(height)

            return heights

        return None

    def get_heightmap_grid(self) -> Tuple[List[List[int]], int, int]:
        """
        Extract and assemble the full heightmap grid from all tiles.
        Returns (grid, width, height) where grid[y][x] = height value.
        """
        heightmaps = self.find_heightmap_textures()

        if not heightmaps:
            return [], 0, 0

        # Parse tile coordinates from names like "chunk_n10_n10Height_8_1"
        tiles = {}
        for hm in heightmaps:
            name = hm["object_name"]
            # Extract tile coordinates
            parts = name.split("_")
            if len(parts) >= 2:
                try:
                    # Last two parts are tile X and Y
                    tile_x = int(parts[-2])
                    tile_y = int(parts[-1])
                    tiles[(tile_x, tile_y)] = hm
                except ValueError:
                    continue

        if not tiles:
            return [], 0, 0

        # Find grid dimensions
        max_x = max(t[0] for t in tiles.keys()) + 1
        max_y = max(t[1] for t in tiles.keys()) + 1

        print(f"Found {len(tiles)} heightmap tiles, grid size: {max_x}x{max_y}")

        # Tile size (assuming 128x128)
        tile_size = 128

        # Create full grid
        full_width = max_x * tile_size
        full_height = max_y * tile_size
        grid = [[0] * full_width for _ in range(full_height)]

        # Fill in each tile
        for (tile_x, tile_y), hm in tiles.items():
            heights = self.extract_g16_texture(hm)
            if heights and len(heights) == tile_size * tile_size:
                for y in range(tile_size):
                    for x in range(tile_size):
                        grid_x = tile_x * tile_size + x
                        grid_y = tile_y * tile_size + y
                        if grid_y < full_height and grid_x < full_width:
                            grid[grid_y][grid_x] = heights[y * tile_size + x]

        return grid, full_width, full_height


def heightmap_to_gltf(
    grid: List[List[int]],
    width: int,
    height: int,
    output_path: str,
    scale: float = 1.0,
    height_scale: float = 0.01,
    texture_path: Optional[str] = None,
):
    """
    Convert a heightmap grid to a glTF terrain mesh.

    Args:
        grid: 2D list of height values
        width, height: Grid dimensions
        output_path: Output glTF file path
        scale: XZ scale factor
        height_scale: Y (height) scale factor
    """
    if not grid or width == 0 or height == 0:
        print("Error: Empty heightmap grid")
        return False

    # Subsample for reasonable mesh size (every 4th vertex)
    step = 4
    mesh_width = width // step
    mesh_height = height // step

    print(f"Generating terrain mesh: {mesh_width}x{mesh_height} vertices")

    # Generate vertices
    positions = []
    uvs = []

    for z in range(mesh_height):
        for x in range(mesh_width):
            src_x = x * step
            src_z = z * step

            if src_z < len(grid) and src_x < len(grid[src_z]):
                h = grid[src_z][src_x]
            else:
                h = 0

            # Position (centered, Y-up for glTF)
            px = (x - mesh_width / 2) * scale
            py = h * height_scale
            pz = (z - mesh_height / 2) * scale

            positions.extend([px, py, pz])
            uvs.extend([x / mesh_width, z / mesh_height])

    # Generate indices (triangle strip converted to triangles)
    indices = []
    for z in range(mesh_height - 1):
        for x in range(mesh_width - 1):
            i0 = z * mesh_width + x
            i1 = i0 + 1
            i2 = i0 + mesh_width
            i3 = i2 + 1

            # Two triangles per quad
            indices.extend([i0, i2, i1])
            indices.extend([i1, i2, i3])

    # Calculate bounds
    min_pos = [float("inf")] * 3
    max_pos = [float("-inf")] * 3
    for i in range(0, len(positions), 3):
        for j in range(3):
            min_pos[j] = min(min_pos[j], positions[i + j])
            max_pos[j] = max(max_pos[j], positions[i + j])

    # Pack binary data
    position_bytes = struct.pack(f"<{len(positions)}f", *positions)
    uv_bytes = struct.pack(f"<{len(uvs)}f", *uvs)

    vertex_count = len(positions) // 3
    if vertex_count <= 65535:
        index_bytes = struct.pack(f"<{len(indices)}H", *indices)
        index_component_type = 5123  # UNSIGNED_SHORT
    else:
        index_bytes = struct.pack(f"<{len(indices)}I", *indices)
        index_component_type = 5125  # UNSIGNED_INT

    # Build buffer
    buffer_data = bytearray()
    buffer_data.extend(position_bytes)
    buffer_data.extend(uv_bytes)
    buffer_data.extend(index_bytes)

    # Add texture if provided
    images = []
    textures = []
    
    if texture_path and os.path.exists(texture_path):
        try:
            with open(texture_path, 'rb') as f:
                texture_data = f.read()
            
            # Determine mime type
            if texture_path.lower().endswith('.png'):
                mime_type = "image/png"
            elif texture_path.lower().endswith('.tga'):
                # TGA files - check if it's actually PNG (umodel sometimes mislabels)
                if texture_data[:8] == b'\x89PNG\r\n\x1a\n':
                    mime_type = "image/png"
                else:
                    mime_type = "image/x-tga"
            else:
                mime_type = "image/png"
            
            # Add texture to buffer
            texture_offset = len(buffer_data)
            buffer_data.extend(texture_data)
            
            print(f"  Added terrain texture: {os.path.basename(texture_path)} ({len(texture_data)} bytes)")
        except Exception as e:
            print(f"  Warning: Failed to load texture: {e}")
            texture_path = None
    
    buffer_uri = f"data:application/octet-stream;base64,{base64.b64encode(bytes(buffer_data)).decode('ascii')}"
    
    # Build buffer views
    buffer_views = [
        {"buffer": 0, "byteOffset": 0, "byteLength": len(position_bytes), "target": 34962},
        {"buffer": 0, "byteOffset": len(position_bytes), "byteLength": len(uv_bytes), "target": 34962},
        {"buffer": 0, "byteOffset": len(position_bytes) + len(uv_bytes), "byteLength": len(index_bytes), "target": 34963}
    ]
    
    # Build material
    material = {
        "name": "terrain_material",
        "pbrMetallicRoughness": {
            "baseColorFactor": [1.0, 1.0, 1.0, 1.0] if texture_path else [0.4, 0.6, 0.3, 1.0],
            "metallicFactor": 0.0,
            "roughnessFactor": 0.9
        },
        "doubleSided": True
    }
    
    # Add texture reference if we have one
    if texture_path and os.path.exists(texture_path):
        texture_buffer_view_idx = len(buffer_views)
        buffer_views.append({
            "buffer": 0,
            "byteOffset": texture_offset,
            "byteLength": len(texture_data)
        })
        
        images.append({
            "mimeType": mime_type,
            "bufferView": texture_buffer_view_idx
        })
        
        textures.append({"source": 0})
        
        material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
    
    # Build glTF
    gltf = {
        "asset": {"version": "2.0", "generator": "extract_heightmap.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "terrain"}],
        "meshes": [{
            "name": "terrain",
            "primitives": [{
                "attributes": {"POSITION": 0, "TEXCOORD_0": 1},
                "indices": 2,
                "material": 0,
                "mode": 4
            }]
        }],
        "materials": [material],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": vertex_count, "type": "VEC3", "min": min_pos, "max": max_pos},
            {"bufferView": 1, "componentType": 5126, "count": vertex_count, "type": "VEC2"},
            {"bufferView": 2, "componentType": index_component_type, "count": len(indices), "type": "SCALAR"}
        ],
        "bufferViews": buffer_views,
        "buffers": [{"uri": buffer_uri, "byteLength": len(buffer_data)}]
    }
    
    if images:
        gltf["images"] = images
    if textures:
        gltf["textures"] = textures

    with open(output_path, "w") as f:
        json.dump(gltf, f)

    print(f"Saved terrain mesh to {output_path}")
    print(f"  Vertices: {vertex_count}")
    print(f"  Triangles: {len(indices) // 3}")
    return True


def main():
    import sys
    
    if len(sys.argv) < 2:
        zone_path = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Maps/chunk_n10_n10.vgr"
    else:
        zone_path = sys.argv[1]
    
    output_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(config.TERRAIN_DIR, "terrain.gltf")
    texture_path = sys.argv[3] if len(sys.argv) > 3 else None
    
    # Try to find terrain color texture automatically
    if not texture_path:
        zone_name = Path(zone_path).stem
        possible_textures = [
            f"output/zones/{zone_name}/Texture/{zone_name.replace('chunk_', '')}baseColor.tga",
            f"output/zones/{zone_name}/Texture/{zone_name.split('_')[1]}_{zone_name.split('_')[2]}baseColor.tga",
        ]
        for pt in possible_textures:
            if os.path.exists(pt):
                texture_path = pt
                break
    
    print(f"Extracting heightmap from: {zone_path}")
    if texture_path:
        print(f"Using terrain texture: {texture_path}")
    
    extractor = HeightmapExtractor(zone_path)
    
    # Find heightmap textures
    heightmaps = extractor.find_heightmap_textures()
    print(f"Found {len(heightmaps)} heightmap textures")
    
    for hm in heightmaps[:5]:
        print(f"  {hm['object_name']}: offset={hex(hm['serial_offset'])}, size={hm['serial_size']}")
    
    # Extract full heightmap grid
    grid, width, height = extractor.get_heightmap_grid()
    
    if grid:
        print(f"Extracted heightmap: {width}x{height}")
        
        # Generate terrain mesh
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        heightmap_to_gltf(grid, width, height, output_path, texture_path=texture_path)
    else:
        print("Failed to extract heightmap grid")


if __name__ == "__main__":
    main()
