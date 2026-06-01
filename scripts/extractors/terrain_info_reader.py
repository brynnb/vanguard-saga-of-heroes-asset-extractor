#!/usr/bin/env python3
"""
Terrain Info Reader — parses terrain_info.txt files extracted by Unreal-Library.

Replaces the brittle binary-scanning _parse_terrain_info() with clean text parsing
of the pre-extracted terrain layer and tile mapping data.

Each terrain_info.txt contains:
  - Layers[N]=(Texture=Shader'PackageName.ShaderName', AlphaMap=..., LayerWeightMap=...)
  - pBuildingTileLayerData[N]=<int>  (sequential layer index per tile slot, -1 = unused)
  - GrassData=(GrassMaterial=Shader'Package.Shaders.MaterialName', GrassTypeScales=...)

Usage:
    from extractors.terrain_info_reader import parse_terrain_info_file

    layers, tile_data = parse_terrain_info_file("path/to/terrain_info.txt")
    # layers: dict {int_index: {"shader_full_path": str, "shader_name": str, "alpha_map": str|None}}
    # tile_data: dict {int_index: int_layer_index}

    grass_data = parse_grass_data_file("path/to/terrain_info.txt")
    # grass_data: dict {"grass_material": str, "grass_material_name": str, "grass_type_scales": str|None}
"""

import re
import os


# Pre-compiled regexes
_LAYER_RE = re.compile(
    r"Layers\[(\d+)\]=\(Texture=Shader'([^']+)'(?:,AlphaMap=([^,\)]+))?(?:,LayerWeightMap=([^,\)]+))?\)"
)
_TILE_RE = re.compile(r"pBuildingTileLayerData\[(\d+)\]=(-?\d+)")
_GRASS_DATA_RE = re.compile(
    r"GrassData=\(GrassMaterial=Shader'([^']+)',GrassTypeScales=(.*?)\)\s*$"
)


def parse_terrain_info_file(terrain_info_path):
    """Parse a terrain_info.txt file into structured layer and tile data.
    
    Args:
        terrain_info_path: Path to terrain_info.txt
        
    Returns:
        layers: dict mapping layer_index -> {
            "shader_full_path": "P1_C2_Terrain_Shaders.Coastline.P1_C2_Terrain_beach001",
            "shader_name": "P1_C2_Terrain_beach001",
            "alpha_map": "Texture'chunk_n10_n8.Alphamaps.chunk_00005_00006_A_Base'" or None
        }
        tile_layer_data: dict mapping tile_slot_index -> sequential_layer_index
    """
    layers = {}
    tile_layer_data = {}
    
    if not os.path.exists(terrain_info_path):
        return layers, tile_layer_data
    
    with open(terrain_info_path, "r") as f:
        for line in f:
            line = line.strip()
            
            # Match Layers[N]=(Texture=Shader'...', ...)
            m = _LAYER_RE.search(line)
            if m:
                idx = int(m.group(1))
                shader_full = m.group(2)  # e.g. "P1_C2_Terrain_Shaders.Coastline.P1_C2_Terrain_beach001"
                alpha_map_raw = m.group(3)  # e.g. "Texture'chunk_n10_n8.Alphamaps...'" or "none"
                
                # Extract just the shader name (last component after last dot)
                shader_name = shader_full.split(".")[-1]
                
                # Clean up alpha map
                alpha_map = None
                if alpha_map_raw and alpha_map_raw.strip().lower() != "none":
                    alpha_map = alpha_map_raw.strip()
                
                layers[idx] = {
                    "shader_full_path": shader_full,
                    "shader_name": shader_name,
                    "alpha_map": alpha_map,
                }
                continue
            
            # Match pBuildingTileLayerData[N]=<int>
            m = _TILE_RE.search(line)
            if m:
                idx = int(m.group(1))
                val = int(m.group(2))
                tile_layer_data[idx] = val
                continue
    
    return layers, tile_layer_data


def parse_grass_data_file(terrain_info_path):
    """Parse the terrain grass material metadata from a terrain_info.txt file.

    Args:
        terrain_info_path: Path to terrain_info.txt

    Returns:
        dict with keys:
            "grass_material": full shader path, e.g. "P0001_SpeedTrees_shaders.Shaders.GrassTest"
            "grass_material_name": final material component, e.g. "GrassTest"
            "grass_type_scales": raw GrassTypeScales value, or None for "none"
        Returns {} when the file is missing or has no GrassData line.
    """
    if not os.path.exists(terrain_info_path):
        return {}

    with open(terrain_info_path, "r") as f:
        for line in f:
            line = line.strip()
            m = _GRASS_DATA_RE.search(line)
            if not m:
                continue

            grass_material = m.group(1)
            grass_type_scales = m.group(2).strip()
            if grass_type_scales.lower() == "none":
                grass_type_scales = None

            return {
                "grass_material": grass_material,
                "grass_material_name": grass_material.split(".")[-1],
                "grass_type_scales": grass_type_scales,
            }

    return {}


def get_shader_names_for_chunk(terrain_info_path):
    """Get ordered list of unique shader names referenced by a chunk.
    
    Returns list of shader_name strings in layer order (deduped).
    """
    layers, _ = parse_terrain_info_file(terrain_info_path)
    seen = set()
    result = []
    for idx in sorted(layers.keys()):
        name = layers[idx]["shader_name"]
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def get_tile_layer_mapping(terrain_info_path):
    """Get the complete tile-to-layer mapping for terrain blending.
    
    Vanguard terrain is a 16x16 grid of tiles. Each tile has 4 slots
    (pBuildingTileLayerData[tile*4+0..3]) that reference layer indices.
    -1 means "no layer for this slot".
    
    Returns:
        tile_layers: dict mapping (row, col) -> list of up to 4 layer indices (excluding -1)
        layers: the full layer dict from parse_terrain_info_file
    """
    layers, tile_data = parse_terrain_info_file(terrain_info_path)
    
    tile_layers = {}
    for r in range(16):
        for c in range(16):
            # COLUMN-MAJOR: confirmed in extract_all_terrain.py
            t_idx = (c * 16 + r) * 4
            active = []
            for bit in range(4):
                layer_idx = tile_data.get(t_idx + bit, -1)
                if layer_idx >= 0:
                    active.append(layer_idx)
            tile_layers[(r, c)] = active
    
    return tile_layers, layers


# Quick test / standalone usage
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python terrain_info_reader.py <terrain_info.txt>")
        sys.exit(1)
    
    path = sys.argv[1]
    layers, tile_data = parse_terrain_info_file(path)
    
    print(f"Layers: {len(layers)}")
    for idx in sorted(layers.keys()):
        l = layers[idx]
        print(f"  [{idx}] {l['shader_name']}  (full: {l['shader_full_path']})")
        if l['alpha_map']:
            print(f"       alpha: {l['alpha_map']}")
    
    print(f"\nTile mapping entries: {len(tile_data)}")

    grass_data = parse_grass_data_file(path)
    if grass_data:
        print(
            "\nGrass: {grass_material_name}  (full: {grass_material})".format(
                **grass_data
            )
        )
        if grass_data["grass_type_scales"]:
            print(f"       scales: {grass_data['grass_type_scales']}")
    
    # Show tile grid summary
    tile_layers, _ = get_tile_layer_mapping(path)
    active_tiles = sum(1 for v in tile_layers.values() if v)
    print(f"Active tiles (have layers): {active_tiles}/256")
    
    # Show first few tiles
    for r in range(4):
        for c in range(4):
            active = tile_layers.get((r, c), [])
            if active:
                names = [layers[i]["shader_name"] if i in layers else f"?{i}" for i in active]
                print(f"  Tile ({r},{c}): {names}")
