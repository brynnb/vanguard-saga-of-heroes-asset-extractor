#!/usr/bin/env python3
"""
Extract terrain from all VGR chunks using direct binary parsing.
Does not require UE Viewer/umodel or Wine - parses terrain and textures
directly from VGR files. If optional map reference text has been dumped with
scripts/extractors/bulk_extract_chunk_data.py, this script uses it for terrain
layer names and tile mapping.

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
# Add project root to path (go up 2 levels from scripts/extractors or scripts/generators)
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

import config
from ue2 import UE2Package
from extractors.terrain_info_reader import parse_terrain_info_file

# Configuration
DB_PATH = config.DB_PATH
VANGUARD_MAPS = os.path.join(config.ASSETS_PATH, "Maps")
OUTPUT_DIR = config.TERRAIN_GRID_DIR

# Optional pre-extracted terrain info text produced via Unreal-Library CLI.
TERRAIN_INFO_DIR = getattr(
    config,
    "REFERENCE_MAPS_DIR",
    os.path.join(config.OUTPUT_DIR, "reference", "Maps"),
)

# Vanguard-specific terrain decoding constants
# Confirmed from VGOEmulator source (TerrainInfo.cpp GetGlobalVertex):
#   world_x = (X - 256) * 400
#   world_y = (Y - 256) * 400
#   world_z = (raw_u16 - 10001) * 10
HEIGHT_OFFSET = 10001.0  # Baseline subtracted from raw u16 before scaling
HEIGHT_SCALE = 10.0  # World units per raw height unit (after offset)
VERTEX_SPACING = 400.0  # World units between adjacent vertices
GRID_CENTER = 256  # Grid origin offset (512/2)


def print_progress_bar(
    iteration,
    total,
    prefix="",
    suffix="",
    decimals=1,
    length=40,
    fill="█",
    print_end="\r",
):
    """
    Call in a loop to create terminal progress bar
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filled_length = int(length * iteration // total)
    bar = fill * filled_length + "-" * (length - filled_length)
    print(f"\r{prefix} |{bar}| {percent}% {suffix}", end=print_end)
    if iteration == total:
        print()


def find_texture_exports(pkg, pattern):
    """Find texture exports matching a pattern."""
    results = []
    for exp in pkg.exports:
        if exp["class_name"] == "Texture" and pattern in exp["object_name"]:
            results.append(exp)
    return results


def extract_quad_visibility(pkg):
    """Extract QuadVisibilityBitmap from TerrainInfo in a VGR package.

    The bitmap is 8192 bytes = 65536 bits, stored as a flat bit array with
    a row stride of 512 bits (64 bytes per row), giving 128 rows × 512 columns.
    Each bit covers 1 quad in X and 4 quads in Y on a 512×512 heightmap grid.
    Bit=1 means visible, bit=0 means invisible (hole).

    Returns:
        numpy bool array of shape (128, 512) where True=visible,
        or None if not found or all quads are visible.
    """
    from ue2.reader import read_compact_index_at as _ci

    exp = next((e for e in pkg.exports if e["class_name"] == "TerrainInfo"), None)
    if not exp:
        return None

    data = pkg.get_export_data(exp)
    try:
        qvb_idx = pkg.names.index("QuadVisibilityBitmap")
    except ValueError:
        return None

    # Scan for ArrayProperty named QuadVisibilityBitmap
    for pos in range(min(len(data) - 10, 50000)):
        idx, nxt = _ci(data, pos)
        if idx != qvb_idx or nxt >= len(data):
            continue
        info = data[nxt]
        p_type = info & 0x0F
        if p_type != 9:  # Must be Array
            continue

        size_bits = (info >> 4) & 0x07
        p = nxt + 1
        if size_bits <= 4:
            size = [1, 2, 4, 12, 16][size_bits]
        elif size_bits == 5:
            size = data[p]; p += 1
        elif size_bits == 6:
            size = struct.unpack("<H", data[p : p + 2])[0]; p += 2
        elif size_bits == 7:
            size = struct.unpack("<I", data[p : p + 4])[0]; p += 4
        else:
            continue

        count, ci_len = _ci(data, p)
        if count != 8192:
            continue

        raw = data[p + ci_len : p + ci_len + 8192]
        if len(raw) < 8192:
            continue

        # Unpack bits with stride=512 (64 bytes/row), 128 rows
        bitmap = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little")
        bitmap = bitmap[:65536].reshape(128, 512).astype(bool)

        # If all visible, no need to return the bitmap
        if bitmap.all():
            return None

        return bitmap

    return None


def extract_grass_alpha(pkg, chunk_name):
    """Extract GrassAlpha density map from VGR package as grayscale PNG.
    
    Returns the output path if successful, None otherwise.
    """
    from ue2.texture import Texture

    grass_name = f"GrassAlpha_{chunk_name}Height"
    for exp in pkg.exports:
        if exp["class_name"] == "Texture" and exp["object_name"] == grass_name:
            try:
                data = pkg.get_export_data(exp)
                tex = Texture(data, pkg.names)
                img = tex.get_image(0)
                if img is None and tex.format_id == 9:
                    # Tail-guided fallback for L8 textures with ghost block issues.
                    # Layout: [...][size:4][L8_data][width:4][height:4][ubits:1][vbits:1]
                    import struct
                    expected = tex.u_size * tex.v_size
                    if len(tex.data) >= expected + 10:
                        l8_end = len(tex.data) - 10
                        l8_start = l8_end - expected
                        raw = tex.data[l8_start:l8_end]
                        img = Image.frombytes("L", (tex.u_size, tex.v_size), bytes(raw))
                if img:
                    if img.mode != "L":
                        img = img.convert("L")
                    output_path = os.path.join(OUTPUT_DIR, f"{chunk_name}_grass_alpha.png")
                    img.save(output_path)
                    return output_path
            except Exception as e:
                print(f"    Warning: Failed to extract GrassAlpha: {e}")
    return None


def extract_g16_heightmap(pkg, chunk_name):
    """Extract and decode G16 heightmap from VGR package (Low Detail 512x512)."""
    from ue2.texture import Texture

    height_name = f"{chunk_name}Height"
    for exp in pkg.exports:
        if exp["class_name"] == "Texture" and exp["object_name"] == height_name:
            data = pkg.get_export_data(exp)
            tex = Texture(data, pkg.names)
            if tex.mips:
                mip = tex.mips[0]
                grid_size = mip.width
                height_data = mip.data
                heights = (
                    np.frombuffer(height_data, dtype="<u2")
                    .reshape(grid_size, grid_size, order="F")
                    .astype(np.float64)
                )

                # Apply 256-boundary heuristic correction
                for row in range(grid_size):
                    for col in range(1, grid_size - 1):
                        curr, left, right = (
                            heights[row, col],
                            heights[row, col - 1],
                            heights[row, col + 1],
                        )
                        diff = curr - (left + right) / 2
                        if 200 < diff < 320:
                            heights[row, col] -= 256
                        elif -320 < diff < -200:
                            heights[row, col] += 256
                for col in range(grid_size):
                    for row in range(1, grid_size - 1):
                        curr, up, down = (
                            heights[row, col],
                            heights[row - 1, col],
                            heights[row + 1, col],
                        )
                        diff = curr - (up + down) / 2
                        if 200 < diff < 320:
                            heights[row, col] -= 256
                        elif -320 < diff < -200:
                            heights[row, col] += 256
                return heights, grid_size
    return None, None


def apply_256_boundary_correction(heights, grid_h, grid_w):
    """Apply 256-boundary heuristic correction to a heightmap array.

    See TERRAIN_GUIDE.md Section 2.3 for details on this heuristic.
    """
    # Horizontal pass (check left/right neighbors)
    for row in range(grid_h):
        for col in range(1, grid_w - 1):
            curr, left, right = (
                heights[row, col],
                heights[row, col - 1],
                heights[row, col + 1],
            )
            diff = curr - (left + right) / 2
            if 200 < diff < 320:
                heights[row, col] -= 256
            elif -320 < diff < -200:
                heights[row, col] += 256
    # Vertical pass (check up/down neighbors)
    for col in range(grid_w):
        for row in range(1, grid_h - 1):
            curr, up, down = (
                heights[row, col],
                heights[row - 1, col],
                heights[row + 1, col],
            )
            diff = curr - (up + down) / 2
            if 200 < diff < 320:
                heights[row, col] -= 256
            elif -320 < diff < -200:
                heights[row, col] += 256
    return heights


def _bilinear_upsample(arr, target_h, target_w):
    """Bilinear upsample a 2D array to target dimensions using numpy only."""
    src_h, src_w = arr.shape
    x_src = np.linspace(0, target_w - 1, src_w)
    x_dst = np.arange(target_w)
    # Interpolate columns first
    temp = np.zeros((src_h, target_w))
    for row in range(src_h):
        temp[row] = np.interp(x_dst, x_src, arr[row])
    # Then interpolate rows
    y_src = np.linspace(0, target_h - 1, src_h)
    y_dst = np.arange(target_h)
    result = np.zeros((target_h, target_w))
    for col in range(target_w):
        result[:, col] = np.interp(y_dst, y_src, temp[:, col])
    return result


def extract_hd_heightmap(pkg, chunk_name):
    """Extract high-detail terrain by upsampling the low-res G16 heightmap.

    NOTE: Vanguard chunks ONLY contain a single 512x512 heightmap (Format 10).
    The Format 17 tiles (misleadingly named 'Height_R_C') are actually
    TERRAIN LAYER WEIGHT MAPS, not heightmaps.

    Until a higher-detailed height source is found (unlikely), HD terrain
    uses bilinear upsampling of the 512x512 LR to 2048x2048.
    """
    from ue2.texture import Texture

    HD_SIZE = 2048  # Upsample target (4x the 512 LR)

    # Extract the low-res heightmap
    lr_heights = None
    height_name = f"{chunk_name}Height"
    for exp in pkg.exports:
        if exp["class_name"] == "Texture" and exp["object_name"] == height_name:
            data = pkg.get_export_data(exp)
            tex = Texture(data, pkg.names)
            if tex.mips:
                mip = tex.mips[0]
                grid_size = mip.width
                lr_heights = (
                    np.frombuffer(mip.data, dtype="<u2")
                    .reshape(grid_size, grid_size, order="F")
                    .astype(np.float64)
                )
                lr_heights = apply_256_boundary_correction(
                    lr_heights, grid_size, grid_size
                )
            break

    if lr_heights is None:
        return None, None, None

    # Bilinear upsample 512x512 -> 2048x2048
    hd_heights = _bilinear_upsample(lr_heights, HD_SIZE, HD_SIZE)

    return hd_heights, HD_SIZE, HD_SIZE


def _decode_weight_tile(raw_data):
    """Decode a Format 17 tile as a layer weight map.

    Format 17 tiles are 32768 bytes = 128x128 uint16. Each byte contains
    2 nibbles (high, low), interleaved to form a 256x256 bitmask grid.

    Each 4-bit nibble is a bitmask with REVERSED bit-to-slot mapping:
      bit 3 (8) = slot 0,  bit 2 (4) = slot 1,
      bit 1 (2) = slot 2,  bit 0 (1) = slot 3

    Returns 256x256 uint8 array of layer bitmasks.
    """
    all_bytes = np.frombuffer(raw_data, dtype=np.uint8)
    high = (all_bytes >> 4) & 0xF
    low = all_bytes & 0xF
    nibbles = np.empty(len(all_bytes) * 2, dtype=np.uint8)
    nibbles[0::2] = high
    nibbles[1::2] = low
    return nibbles.reshape(256, 256)


def _build_terrain_shader_cache():
    """Build a cache mapping shader name -> PIL.Image (diffuse texture).

    Scans all terrain/grass shader .utx packages once and caches the results.
    """
    if hasattr(_build_terrain_shader_cache, "_cache"):
        return _build_terrain_shader_cache._cache

    from ue2.texture import Texture as Tex
    from ue2.properties import find_property_start, parse_properties

    textures_dir = config.TEXTURES_DIR
    cache = {}  # shader_name -> PIL.Image (RGB)

    for fname in sorted(os.listdir(textures_dir)):
        if not fname.endswith(".utx"):
            continue
        name_lower = fname.lower()
        if "terrain" not in name_lower and "grass" not in name_lower:
            continue

        pkg_path = os.path.join(textures_dir, fname)
        try:
            utx_pkg = UE2Package(pkg_path)
            for e in utx_pkg.exports:
                if e["class_name"] != "Shader":
                    continue
                shader_name = e["object_name"]
                if shader_name in cache:
                    continue
                try:
                    edata = utx_pkg.get_export_data(e)
                    estart = find_property_start(edata, utx_pkg.names)
                    eprops = parse_properties(edata, utx_pkg.names, estart)
                    diffuse_ref = None
                    for p in eprops:
                        if p["name"] == "Diffuse":
                            diffuse_ref = p["value"]
                            break
                    if diffuse_ref and isinstance(diffuse_ref, int) and diffuse_ref > 0:
                        diff_idx = diffuse_ref - 1
                        if diff_idx < len(utx_pkg.exports):
                            diff_exp = utx_pkg.exports[diff_idx]
                            if diff_exp["class_name"] == "Texture":
                                diff_data = utx_pkg.get_export_data(diff_exp)
                                diff_tex = Tex(diff_data, utx_pkg.names)
                                img = diff_tex.get_image(0)
                                if img:
                                    cache[shader_name] = img.convert("RGB")
                except Exception:
                    continue
        except Exception:
            continue

    _build_terrain_shader_cache._cache = cache
    return cache


def _parse_terrain_info(pkg):
    """Parse TerrainInfo for all Layers and per-tile mapping data.

    Walks the full UE2 property chain (continuing past None terminators)
    to collect:
      - Layers[] struct entries (both TerrainLayer and pBuildingTileLayerData
        struct types) in parse order. Each contains a Texture object ref
        pointing to a shader.
      - pBuildingTileLayerData[N] int entries that map (tile*4+bit) to a
        sequential layer index.

    The sequential layer index used by pBuildingTileLayerData refers to the
    Nth Layers[] entry in the order they appear in the serialized data,
    NOT the Layers[] array index.

    Returns:
        all_layers: list of (seq_idx, shader_name) in parse order
        shader_layers: dict seq_idx -> shader_name (only non-None entries)
        tile_layer_data: dict mapping_array_idx -> sequential_layer_index
    """
    from ue2.reader import read_compact_index_at as _ci

    exp = next((e for e in pkg.exports if e["class_name"] == "TerrainInfo"), None)
    if not exp:
        return [], {}, {}

    data = pkg.get_export_data(exp)
    names = pkg.names

    try:
        layers_name_idx = names.index("Layers")
        mapping_name_idx = names.index("pBuildingTileLayerData")
        texture_name_idx = names.index("Texture")
    except ValueError:
        return [], {}, {}

    def _read_prop_tag(data, pos):
        """Read one UE2 property tag. Returns (name_idx, p_type, size, struct_name, array_idx, val_start, val_end) or None."""
        if pos >= len(data) - 1:
            return None
        name_idx, p = _ci(data, pos)
        if name_idx < 0 or name_idx >= len(names):
            return None
        if names[name_idx] == "None":
            return ("None", 0, 0, None, 0, p, p)

        info = data[p]
        p += 1
        p_type = info & 0x0F
        size_bits = (info >> 4) & 0x07
        array_bit = (info >> 7) & 0x01

        size = 0
        if size_bits <= 4:
            size = [1, 2, 4, 12, 16][size_bits]
        elif size_bits == 5:
            size = data[p]
            p += 1
        elif size_bits == 6:
            size = struct.unpack("<H", data[p : p + 2])[0]
            p += 2
        elif size_bits == 7:
            size = struct.unpack("<I", data[p : p + 4])[0]
            p += 4

        struct_name = None
        if p_type == 10:
            si, p = _ci(data, p)
            if 0 <= si < len(names):
                struct_name = names[si]

        array_idx = 0
        if array_bit and p_type != 3:
            b = data[p]
            p += 1
            if b < 128:
                array_idx = b
            else:
                b2 = data[p]
                p += 1
                if not (b & 0x40):
                    array_idx = ((b << 8) | b2) & 0x3FFF
                else:
                    b3 = data[p]
                    p += 1
                    b4 = data[p]
                    p += 1
                    array_idx = ((b << 24) | (b2 << 16) | (b3 << 8) | b4) & 0x3FFFFF

        val_start = p
        val_end = p + size
        if val_end > len(data):
            return None
        return (name_idx, p_type, size, struct_name, array_idx, val_start, val_end)

    def _find_texture_in_struct(struct_data):
        """Find a Texture object reference inside a Layers[] struct entry."""
        for ipos in range(len(struct_data) - 2):
            try:
                ii, inp = _ci(struct_data, ipos)
                if ii == texture_name_idx:
                    if inp < len(struct_data):
                        sub_info = struct_data[inp]
                        sub_type = sub_info & 0x0F
                        if sub_type in (5, 8):
                            ref, _ = _ci(struct_data, inp + 1)
                            return pkg.get_object_name(ref)
                if 0 <= ii < len(names) and names[ii] == "None":
                    break
            except:
                pass
        return None

    # --- Phase 1: Find ALL Layers[] struct entries by scanning the full data ---
    # We scan every byte position for a valid Layers struct property tag.
    # This is necessary because the pBuildingTileLayerData struct entries
    # have an inflated size (84 bytes) that overlaps subsequent Layers entries,
    # so sequential property walking skips entries.
    layer_positions = []  # (offset, shader_name)
    for scan_pos in range(len(data) - 6):
        try:
            idx, nxt = _ci(data, scan_pos)
            if idx != layers_name_idx:
                continue
            info = data[nxt]
            p_type = info & 0x0F
            if p_type != 10:  # Must be Struct type
                continue
            size_bits = (info >> 4) & 0x07
            p = nxt + 1
            size = 0
            if size_bits <= 4:
                size = [1, 2, 4, 12, 16][size_bits]
            elif size_bits == 5:
                size = data[p]
                p += 1
            elif size_bits == 6:
                size = struct.unpack("<H", data[p : p + 2])[0]
                p += 2
            elif size_bits == 7:
                size = struct.unpack("<I", data[p : p + 4])[0]
                p += 4
            # Read struct name
            si, p = _ci(data, p)
            if si < 0 or si >= len(names):
                continue
            sname = names[si]
            if sname not in ("TerrainLayer", "pBuildingTileLayerData"):
                continue
            # Skip array index
            array_bit = (info >> 7) & 0x01
            if array_bit:
                b = data[p]
                p += 1
                if b >= 128:
                    p += 1
                    if b & 0x40:
                        p += 2
            # p now points to the struct value data — find Texture ref
            # For TerrainLayer (12 bytes): Texture + AlphaMap + LayerWeightMap + None
            # For pBuildingTileLayerData: first inner property should be Texture-like
            shader = _find_texture_in_struct(data[p : p + min(size, 20)])
            layer_positions.append((scan_pos, shader))
        except:
            continue

    # Sort by offset to get sequential order
    layer_positions.sort(key=lambda x: x[0])
    layers_seq = [shader for _, shader in layer_positions]

    # --- Phase 2: Find ALL pBuildingTileLayerData[N] int entries ---
    # These come after the Layers entries and large arrays.
    # Walk the property chain from the beginning to find them.
    tile_layer_data = {}

    # Find property start
    prop_start = None
    for try_pos in range(min(30, len(data))):
        try:
            tag = _read_prop_tag(data, try_pos)
            if tag and tag[0] != "None" and tag[0] >= 0:
                prop_start = try_pos
                break
        except:
            continue

    if prop_start is not None:
        pos = prop_start
        consecutive_nones = 0
        while pos < len(data) - 1:
            tag = _read_prop_tag(data, pos)
            if tag is None:
                break
            if tag[0] == "None":
                pos = tag[6]  # val_end
                consecutive_nones += 1
                if consecutive_nones > 10:
                    break
                continue
            consecutive_nones = 0
            name_idx, p_type, size, struct_name, array_idx, val_start, val_end = tag

            if name_idx == mapping_name_idx and p_type == 2 and size == 4:
                val = struct.unpack("<i", data[val_start:val_end])[0]
                tile_layer_data[array_idx] = val

            pos = val_end

    # Build output: all_layers is indexed by sequential position
    all_layers = [(i, name) for i, name in enumerate(layers_seq)]
    shader_layers = {i: name for i, name in enumerate(layers_seq) if name}

    return all_layers, shader_layers, tile_layer_data


def _resolve_layer_textures(pkg, chunk_name):
    """Resolve terrain layer shader references to diffuse texture images.

    Prefers optional pre-extracted terrain_info.txt generated by
    bulk_extract_chunk_data.py via the Unreal-Library CLI,
    falls back to binary scanning if the file doesn't exist.

    Returns:
        shader_images: dict shader_name -> PIL.Image for all resolvable shaders
        num_layers: int total number of resolvable shader layers
        all_layers: the full parsed layer list as [(seq_idx, shader_name), ...]
        tile_layer_data: dict mapping_array_idx -> sequential_layer_index
    """
    # Try optional terrain_info.txt first (generated by bulk_extract_chunk_data.py)
    terrain_info_path = os.path.join(TERRAIN_INFO_DIR, chunk_name, "terrain_info.txt")
    if os.path.exists(terrain_info_path):
        layers_dict, tile_layer_data = parse_terrain_info_file(terrain_info_path)
        # Convert to the (seq_idx, shader_name) list format expected downstream
        all_layers = [
            (idx, layers_dict[idx]["shader_name"]) for idx in sorted(layers_dict.keys())
        ]
    else:
        # Fallback to binary scanning
        all_layers, shader_layers, tile_layer_data = _parse_terrain_info(pkg)

    shader_cache = _build_terrain_shader_cache()

    shader_images = {}
    for _, shader_name in all_layers:
        if shader_name and shader_name in shader_cache:
            shader_images[shader_name] = shader_cache[shader_name]

    return shader_images, len(all_layers), all_layers, tile_layer_data


def _find_tile_layer_mapping(tile_weight, base_tile_mean, layer_names, mean_colors):
    """Find the best bit-to-layer permutation for a single tile.

    Compares predicted weighted-mean color against the baseColor tile region
    for all permutations of 4 layers from the available set.

    Args:
        tile_weight: (256, 256) uint8 nibble weight map for this tile
        base_tile_mean: (3,) mean RGB of the baseColor for this tile region
        layer_names: list of shader names (may be >4)
        mean_colors: dict shader_name -> (3,) mean RGB

    Returns:
        best_perm: tuple of 4 indices into layer_names (bit 0-3 mapping)
    """
    from itertools import permutations

    # Pre-compute per-bit pixel counts for this tile
    bit_weights = []
    for bit in range(4):
        mask = ((tile_weight >> bit) & 1).astype(np.float64)
        bit_weights.append(mask.sum())

    best_err = 1e18
    best_perm = (
        (0, 1, 2, 3)
        if len(layer_names) >= 4
        else tuple(range(min(4, len(layer_names))))
    )

    n = len(layer_names)
    for perm in permutations(range(n), min(4, n)):
        pred_mean = np.zeros(3)
        total_weight = 0
        for bit in range(len(perm)):
            w = bit_weights[bit]
            if w > 0:
                pred_mean += w * mean_colors[layer_names[perm[bit]]]
                total_weight += w
        if total_weight > 0:
            pred_mean /= total_weight
        err = np.sum((base_tile_mean - pred_mean) ** 2)
        if err < best_err:
            best_err = err
            best_perm = perm

    return best_perm


def extract_hd_texture(pkg, chunk_name):
    """Composite HD terrain texture from Format 17 layer weight maps.

    Weight map format (confirmed empirically with 100% accuracy on multiple chunks):
    - 256x256 nibble bitmask per tile (from 32768-byte uint16 tiles, hi-lo interleaved)
    - Bit-to-slot mapping is REVERSED: bit3→slot0, bit2→slot1, bit1→slot2, bit0→slot3
    - Tile indexing is COLUMN-MAJOR: tile_data_idx = (c*16+r)*4 + slot
    - Missing pBuildingTileLayerData slot 0 entries default to layer 0 (base layer)
    """
    from ue2.texture import Texture as Tex

    TILES = 16
    TILE_PIX = 256
    FULL_SIZE = TILES * TILE_PIX  # 4096
    TEXTURE_REPEATS = 320

    # Reversed bit-to-slot mapping: bit 3→slot 0, bit 2→slot 1, bit 1→slot 2, bit 0→slot 3
    SLOT_TO_BIT = [3, 2, 1, 0]  # SLOT_TO_BIT[slot] = bit number

    # Resolve layer textures
    res = _resolve_layer_textures(pkg, chunk_name)
    if not res:
        return None
    shader_images, num_layers, all_layers, tile_layer_data = res
    if not shader_images:
        print("  Warning: No terrain shaders found.")
        return None

    # Pre-sample all layer textures into full-size tiled arrays via UV mapping.
    # UV coords wrap TEXTURE_REPEATS times across FULL_SIZE pixels.
    tex_size = 512
    coords = np.arange(FULL_SIZE, dtype=np.float64)
    uv = (coords / FULL_SIZE) * TEXTURE_REPEATS * tex_size
    uv_idx = (uv.astype(np.int64)) % tex_size

    sampled_layers = {}
    for name, img in shader_images.items():
        try:
            tex_arr = np.array(img.convert("RGB"), dtype=np.float64)
            sampled_layers[name] = tex_arr[uv_idx[:, np.newaxis], uv_idx[np.newaxis, :]]
        except Exception as e:
            print(f"  Warning: Failed to sample layer {name}: {e}")

    # Load baseColor as fallback for uncached layer 0 (baseColor_shader)
    # Layer 0 is often the pre-baked baseColor which isn't a tileable texture,
    # so it won't be in the shader cache. Use the actual baseColor texture instead.
    base_img = extract_color_texture(pkg, chunk_name)
    base_sampled = None
    if base_img is not None:
        base_sampled = np.array(
            base_img.convert("RGB").resize((FULL_SIZE, FULL_SIZE), Image.BILINEAR),
            dtype=np.float64,
        )

    # Register baseColor as a stand-in for any uncached layer 0 shader
    base_shader_name = all_layers[0][1] if all_layers else None
    if (
        base_shader_name
        and base_shader_name not in sampled_layers
        and base_sampled is not None
    ):
        sampled_layers[base_shader_name] = base_sampled

    # Build the full 4096x4096 weight map
    full_weight = np.zeros((FULL_SIZE, FULL_SIZE), dtype=np.uint8)
    tile_coords = {}
    for exp in pkg.exports:
        if exp["class_name"] != "Texture" or "Height_" not in exp["object_name"]:
            continue
        parts = exp["object_name"].split("Height_", 1)[-1].split("_")
        if len(parts) != 2:
            continue
        try:
            r, c = int(parts[0]), int(parts[1])
            data = pkg.get_export_data(exp)
            tex = Tex(data, pkg.names)
            raw = tex.get_raw_g16(0)
            if raw and len(raw) >= 32768:
                # VANGUARD: Weights are Column-Major
                full_weight[
                    r * TILE_PIX : (r + 1) * TILE_PIX, c * TILE_PIX : (c + 1) * TILE_PIX
                ] = _decode_weight_tile(raw[:32768]).T
                tile_coords[(r, c)] = True
        except:
            continue

    # Composite output
    output = np.zeros((FULL_SIZE, FULL_SIZE, 3), dtype=np.float64)
    layer_count = np.zeros((FULL_SIZE, FULL_SIZE), dtype=np.float64)

    for r in range(TILES):
        for c in range(TILES):
            r0, r1 = r * TILE_PIX, (r + 1) * TILE_PIX
            c0, c1 = c * TILE_PIX, (c + 1) * TILE_PIX

            # COLUMN-MAJOR tile indexing (confirmed 100% match)
            t_base = (c * 16 + r) * 4

            # Build slot→layer_name mapping for this tile
            # Missing slot 0 defaults to layer 0 (base layer), others default to -1 (unused)
            slot_to_layer = {}
            for slot in range(4):
                key = t_base + slot
                if key in tile_layer_data:
                    raw_val = tile_layer_data[key]
                else:
                    raw_val = (
                        0 if slot == 0 else -1
                    )  # Unreal-Library omits 0-valued entries
                layer_seq_idx = (
                    raw_val + 1 if raw_val >= 0 else -1
                )  # +1 shift: raw 0-based excluding baseColor

                if 0 <= layer_seq_idx < len(all_layers):
                    lname = all_layers[layer_seq_idx][1]
                    if lname and lname in sampled_layers:
                        slot_to_layer[slot] = lname

            if not slot_to_layer:
                continue

            if (r, c) not in tile_coords:
                first_layer = next(iter(slot_to_layer.values()))
                output[r0:r1, c0:c1] = sampled_layers[first_layer][r0:r1, c0:c1]
                layer_count[r0:r1, c0:c1] = 1
                continue

            tile_weight = full_weight[r0:r1, c0:c1]

            # Apply REVERSED bit-to-slot mapping (the key fix)
            for slot, lname in slot_to_layer.items():
                bit = SLOT_TO_BIT[slot]
                mask = ((tile_weight >> bit) & 1).astype(np.float64)
                output[r0:r1, c0:c1, :] += (
                    mask[:, :, np.newaxis] * sampled_layers[lname][r0:r1, c0:c1, :]
                )
                layer_count[r0:r1, c0:c1] += mask

    # Normalize and fill unresolved pixels with baseColor fallback
    unresolved = layer_count == 0
    layer_count = np.maximum(layer_count, 1)
    output /= layer_count[:, :, np.newaxis]

    if base_sampled is not None and unresolved.any():
        output[unresolved] = base_sampled[unresolved]

    final_img = Image.fromarray(output.clip(0, 255).astype(np.uint8))
    return final_img


def export_hd_layer_data(pkg, chunk_name, output_dir):
    """Export weight map + layer textures + metadata for runtime GPU blending.

    Outputs:
        <chunk>_layers/weightmap.png   — 4096x4096 RGBA (R=slot0 bits, G=slot1, B=slot2, A=slot3)
        <chunk>_layers/tile_map.json   — per-tile slot→layer mapping + layer texture list
        <chunk>_layers/<shader>.png    — individual layer textures at full resolution
        <chunk>_layers/basecolor.png   — fallback base color texture
    """
    from ue2.texture import Texture as Tex

    TILES = 16
    TILE_PIX = 256
    FULL_SIZE = TILES * TILE_PIX  # 4096
    SLOT_TO_BIT = [3, 2, 1, 0]

    res = _resolve_layer_textures(pkg, chunk_name)
    if not res:
        return None
    shader_images, num_layers, all_layers, tile_layer_data = res
    if not shader_images:
        return None

    layers_dir = os.path.join(output_dir, f"{chunk_name}_layers")
    os.makedirs(layers_dir, exist_ok=True)

    # 1. Export individual layer textures at full resolution
    layer_files = {}
    for name, img in shader_images.items():
        fname = f"{name}.png"
        img.convert("RGB").save(os.path.join(layers_dir, fname))
        layer_files[name] = fname

    # 2. Export base color fallback
    base_img = extract_color_texture(pkg, chunk_name)
    if base_img:
        base_img.convert("RGB").save(os.path.join(layers_dir, "basecolor.png"))

    # 3. Build weight map (4-channel RGBA, each channel = one slot's bit mask)
    full_weight = np.zeros((FULL_SIZE, FULL_SIZE), dtype=np.uint8)
    tile_coords = {}
    for exp in pkg.exports:
        if exp["class_name"] != "Texture" or "Height_" not in exp["object_name"]:
            continue
        parts = exp["object_name"].split("Height_", 1)[-1].split("_")
        if len(parts) != 2:
            continue
        try:
            r, c = int(parts[0]), int(parts[1])
            data = pkg.get_export_data(exp)
            tex = Tex(data, pkg.names)
            raw = tex.get_raw_g16(0)
            if raw and len(raw) >= 32768:
                full_weight[
                    r * TILE_PIX : (r + 1) * TILE_PIX, c * TILE_PIX : (c + 1) * TILE_PIX
                ] = _decode_weight_tile(raw[:32768]).T
                tile_coords[(r, c)] = True
        except:
            continue

    # Split weight map into two RGB PNGs (avoids browser alpha premultiplication)
    # weightmap_01.png: R=slot0, G=slot1, B=unused
    # weightmap_23.png: R=slot2, G=slot3, B=unused
    wmap_01 = np.zeros((FULL_SIZE, FULL_SIZE, 3), dtype=np.uint8)
    wmap_23 = np.zeros((FULL_SIZE, FULL_SIZE, 3), dtype=np.uint8)
    for slot in range(4):
        bit = SLOT_TO_BIT[slot]
        channel_data = ((full_weight >> bit) & 1) * 255
        if slot < 2:
            wmap_01[:, :, slot] = channel_data
        else:
            wmap_23[:, :, slot - 2] = channel_data

    Image.fromarray(wmap_01, "RGB").save(os.path.join(layers_dir, "weightmap_01.png"))
    Image.fromarray(wmap_23, "RGB").save(os.path.join(layers_dir, "weightmap_23.png"))

    # 4. Build tile mapping JSON: for each tile, slot→layer shader name
    # BaseColor (layer 0) is an LOD/distance texture, not an active HD layer.
    # Skip it entirely — slots that map to baseColor are left unassigned.
    tile_map = {}
    for r in range(TILES):
        for c in range(TILES):
            t_base = (c * 16 + r) * 4
            slots = {}
            for slot in range(4):
                key = t_base + slot
                if key in tile_layer_data:
                    raw_val = tile_layer_data[key]
                else:
                    raw_val = (
                        0 if slot == 0 else -1
                    )  # Unreal-Library omits 0-valued entries
                layer_seq_idx = (
                    raw_val + 1 if raw_val >= 0 else -1
                )  # +1 shift: raw 0-based excluding baseColor

                # Skip baseColor (layer 0) and unused slots
                if layer_seq_idx <= 0:
                    continue

                if layer_seq_idx < len(all_layers):
                    lname = all_layers[layer_seq_idx][1]
                    if lname and lname in layer_files:
                        slots[str(slot)] = lname

            has_weight = (r, c) in tile_coords
            tile_map[f"{r}_{c}"] = {"slots": slots, "has_weight": has_weight}

    metadata = {
        "chunk": chunk_name,
        "tiles": TILES,
        "tile_pix": TILE_PIX,
        "full_size": FULL_SIZE,
        "layers": layer_files,
        "tile_map": tile_map,
        "has_basecolor": base_img is not None,
    }

    with open(os.path.join(layers_dir, "tile_map.json"), "w") as f:
        json.dump(metadata, f)

    print(
        f"  Exported layer data: {len(layer_files)} textures, {len(tile_coords)} weight tiles"
    )
    return layers_dir


def export_hd_tiles(pkg, chunk_name, output_dir):
    """Export HD terrain as 16x16 tile GLBs using upsampled LR heightmap.

    Each tile is a 128x128 region of the 2048x2048 upsampled heightmap.
    """
    from ue2.texture import Texture

    TILES_X = 16
    TILES_Y = 16
    TILE_PIX = 128  # 128x128 per tile from 2048/16

    TILE_WORLD_W = 200000.0 / TILES_X
    TILE_WORLD_H = 200000.0 / TILES_Y

    tiles_dir = os.path.join(output_dir, "chunk_tiles", chunk_name)
    os.makedirs(tiles_dir, exist_ok=True)

    # Extract and upsample the LR heightmap
    hd_heights, hd_w, hd_h = extract_hd_heightmap(pkg, chunk_name)
    if hd_heights is None:
        print(f"  Warning: No heightmap found for {chunk_name}")
        return 0

    # TODO: Re-enable once hole placement is verified correct (see TODO.md)
    # quad_visibility = extract_quad_visibility(pkg)
    quad_visibility = None

    exported = 0
    for r in range(TILES_Y):
        for c in range(TILES_X):
            r0, r1 = r * TILE_PIX, (r + 1) * TILE_PIX
            c0, c1 = c * TILE_PIX, (c + 1) * TILE_PIX
            heights = hd_heights[r0:r1, c0:c1]

            h, w = heights.shape
            # Use emulator formula: spacing = 400 * (512/total_grid)
            tile_spacing = VERTEX_SPACING * (512.0 / (TILES_X * w))
            # Tile offset in world coords (centered grid)
            tile_center_x = (c * w + w / 2.0) - (TILES_X * w / 2.0)
            tile_center_z = (r * h + h / 2.0) - (TILES_Y * h / 2.0)

            y_coords, x_coords = np.meshgrid(np.arange(h), np.arange(w), indexing="ij")
            vx = -(
                (x_coords.flatten() - w / 2.0 + tile_center_x) * tile_spacing
            ).astype(np.float32)
            vy = ((heights.flatten() - HEIGHT_OFFSET) * HEIGHT_SCALE).astype(np.float32)
            vz = ((y_coords.flatten() - h / 2.0 + tile_center_z) * tile_spacing).astype(
                np.float32
            )
            vertices_arr = np.column_stack([vx, vy, vz]).astype(np.float32)

            u = x_coords.flatten() / (w - 1)
            v = y_coords.flatten() / (h - 1)
            uvs_arr = np.column_stack([u, v]).astype(np.float32)

            h_left = np.roll(heights, 1, axis=1)
            h_left[:, 0] = heights[:, 0]
            h_right = np.roll(heights, -1, axis=1)
            h_right[:, -1] = heights[:, -1]
            h_up = np.roll(heights, 1, axis=0)
            h_up[0, :] = heights[0, :]
            h_down = np.roll(heights, -1, axis=0)
            h_down[-1, :] = heights[-1, :]
            dx = (h_right - h_left) * HEIGHT_SCALE / (2 * tile_spacing)
            dz = (h_down - h_up) * HEIGHT_SCALE / (2 * tile_spacing)
            nx = dx.flatten()  # negated position X -> flip normal X
            ny = np.ones_like(nx)
            nz = -dz.flatten()
            lengths = np.sqrt(nx * nx + ny * ny + nz * nz)
            normals_arr = np.column_stack(
                [nx / lengths, ny / lengths, nz / lengths]
            ).astype(np.float32)

            y_idx, x_idx = np.meshgrid(
                np.arange(h - 1), np.arange(w - 1), indexing="ij"
            )
            i0 = (y_idx * w + x_idx).flatten()
            indices_arr = (
                np.column_stack([i0, i0 + w, i0 + 1, i0 + 1, i0 + w, i0 + w + 1])
                .flatten()
                .astype(np.uint32)
            )

            # TODO: Re-enable once hole placement is verified correct (see TODO.md)
            # if quad_visibility is not None:
            #     qvb_h, qvb_w = quad_visibility.shape
            #     total_h = TILES_Y * TILE_PIX  # full grid pixel height
            #     total_w = TILES_X * TILE_PIX
            #     scale_y = (total_h - 1) / qvb_h
            #     scale_x = (total_w - 1) / qvb_w
            #     # Map tile-local quad coords to global QVB coords
            #     global_y = r * TILE_PIX + y_idx.flatten()
            #     global_x = c * TILE_PIX + x_idx.flatten()
            #     vis_row = np.clip((global_y / scale_y).astype(int), 0, qvb_h - 1)
            #     vis_col = np.clip((global_x / scale_x).astype(int), 0, qvb_w - 1)
            #     visible = quad_visibility[vis_row, vis_col]
            #     visible_6 = np.repeat(visible, 6)
            #     indices_arr = indices_arr[visible_6]

            vertices_bin = vertices_arr.tobytes()
            normals_bin = normals_arr.tobytes()
            uvs_bin = uvs_arr.tobytes()
            indices_bin = indices_arr.tobytes()
            bin_buffer = vertices_bin + normals_bin + uvs_bin + indices_bin

            v_min = vertices_arr.min(axis=0).tolist()
            v_max = vertices_arr.max(axis=0).tolist()

            gltf = {
                "asset": {"version": "2.0"},
                "scene": 0,
                "scenes": [{"nodes": [0]}],
                "nodes": [{"mesh": 0, "name": f"tile_{r}_{c}"}],
                "meshes": [
                    {
                        "primitives": [
                            {
                                "attributes": {
                                    "POSITION": 0,
                                    "NORMAL": 1,
                                    "TEXCOORD_0": 2,
                                },
                                "indices": 3,
                            }
                        ]
                    }
                ],
                "buffers": [{"byteLength": len(bin_buffer)}],
                "bufferViews": [
                    {
                        "buffer": 0,
                        "byteOffset": 0,
                        "byteLength": len(vertices_bin),
                        "target": 34962,
                    },
                    {
                        "buffer": 0,
                        "byteOffset": len(vertices_bin),
                        "byteLength": len(normals_bin),
                        "target": 34962,
                    },
                    {
                        "buffer": 0,
                        "byteOffset": len(vertices_bin) + len(normals_bin),
                        "byteLength": len(uvs_bin),
                        "target": 34962,
                    },
                    {
                        "buffer": 0,
                        "byteOffset": len(vertices_bin)
                        + len(normals_bin)
                        + len(uvs_bin),
                        "byteLength": len(indices_bin),
                        "target": 34963,
                    },
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
            }

            tile_path = os.path.join(tiles_dir, f"tile_{r}_{c}.glb")
            save_glb(tile_path, gltf, bin_buffer)
            exported += 1

    return exported


from ue2.dxt import decode_dxt5, decode_dxt1


def extract_color_texture(pkg, chunk_name):
    """Extract and decode base color texture from VGR package using formal parsing."""
    from ue2.texture import Texture

    candidates = []
    search_coord = chunk_name.replace("chunk_", "").lower()
    for exp in pkg.exports:
        if exp["class_name"] != "Texture":
            continue
        obj_name = exp["object_name"].lower()
        score = 0
        if search_coord in obj_name and "basecolor" in obj_name:
            score = 100
        elif "basecolor" in obj_name:
            score = 50
        elif search_coord in obj_name:
            score = 30
        elif obj_name.endswith("_base") or obj_name.endswith("base"):
            if not any(x in obj_name for x in ["shadow", "alpha", "grass", "noise"]):
                score = 10
        if score > 0:
            candidates.append((score, exp))
    candidates.sort(key=lambda x: x[0], reverse=True)
    for score, exp in candidates:
        try:
            data = pkg.get_export_data(exp)
            if not data:
                continue
            tex = Texture(data, pkg.names)
            if tex.mips:
                img = tex.get_image(0)
                if img:
                    img = img.transpose(Image.TRANSPOSE)
                    return img
        except:
            continue
    return None


def save_glb(output_path, json_dict, bin_buffer):
    """Save a dictionary and binary buffer as a .glb file."""
    # Align JSON to 4-byte boundary
    json_str = json.dumps(json_dict, separators=(",", ":")).encode("utf-8")
    padding_json = (4 - (len(json_str) % 4)) % 4
    json_str += b" " * padding_json

    # Align BIN to 4-byte boundary
    padding_bin = (4 - (len(bin_buffer) % 4)) % 4
    bin_buffer += b"\x00" * padding_bin

    # Header: magic (4), version (4), length (4)
    # JSON chunk: length (4), type (4), data
    # BIN chunk: length (4), type (4), data
    total_size = 12 + (8 + len(json_str)) + (8 + len(bin_buffer))

    with open(output_path, "wb") as f:
        # Header
        f.write(b"glTF")
        f.write(struct.pack("<I", 2))
        f.write(struct.pack("<I", total_size))

        # JSON Chunk
        f.write(struct.pack("<I", len(json_str)))
        f.write(b"JSON")
        f.write(json_str)

        # BIN Chunk
        f.write(struct.pack("<I", len(bin_buffer)))
        f.write(b"BIN\x00")
        f.write(bin_buffer)


def generate_terrain_gltf(
    heights, color_image, output_path, chunk_name, grid_w=512, grid_h=512,
    quad_visibility=None
):
    """Generate a binary glTF (.glb) terrain mesh.
    
    Args:
        quad_visibility: optional 256x256 bool array (True=visible). When
            provided, quads whose visibility bit is False are omitted from
            the index buffer, creating holes in the terrain mesh.
    """

    # Vectorized vertex generation using emulator formula:
    #   world_x = (X - 256) * 400, world_z = (Y - 256) * 400
    #   world_y = (raw_u16 - 10001) * 10
    # For non-512 grids (upsampled), scale spacing proportionally
    spacing = VERTEX_SPACING * (512.0 / grid_w)  # 400 for 512, 200 for 1024, etc.
    center_offset = grid_w / 2.0  # Center of grid

    y_coords, x_coords = np.meshgrid(
        np.arange(grid_h), np.arange(grid_w), indexing="ij"
    )
    vx = -((x_coords.flatten() - center_offset) * spacing).astype(np.float32)
    vy = ((heights.flatten() - HEIGHT_OFFSET) * HEIGHT_SCALE).astype(np.float32)
    vz = ((y_coords.flatten() - center_offset) * spacing).astype(np.float32)
    vertices_arr = np.column_stack([vx, vy, vz]).astype(np.float32)

    # UVs
    u = x_coords.flatten() / (grid_w - 1)
    v = y_coords.flatten() / (grid_h - 1)
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

    dx = (h_right - h_left) * HEIGHT_SCALE / (2 * spacing)
    dz = (h_down - h_up) * HEIGHT_SCALE / (2 * spacing)
    nx = dx.flatten()  # negated position X -> flip normal X
    ny = np.ones_like(nx)
    nz = -dz.flatten()
    lengths = np.sqrt(nx * nx + ny * ny + nz * nz)
    normals_arr = np.column_stack([nx / lengths, ny / lengths, nz / lengths]).astype(
        np.float32
    )

    # Indices
    y_idx, x_idx = np.meshgrid(
        np.arange(grid_h - 1), np.arange(grid_w - 1), indexing="ij"
    )
    i0 = (y_idx * grid_w + x_idx).flatten()
    indices_arr = (
        np.column_stack([i0, i0 + 1, i0 + grid_w, i0 + grid_w, i0 + 1, i0 + grid_w + 1])
        .flatten()
        .astype(np.uint32)
    )

    # TODO: Re-enable once hole placement is verified correct (see TODO.md)
    # if quad_visibility is not None:
    #     # quad_visibility is 128x512 (128 rows, 512 cols).
    #     # Each bit covers 1 quad in X, ~4 quads in Y on the heightmap grid.
    #     qvb_h, qvb_w = quad_visibility.shape  # 128, 512
    #     scale_y = (grid_h - 1) / qvb_h  # ~4.0 for 512-grid
    #     scale_x = (grid_w - 1) / qvb_w  # ~1.0 for 512-grid
    #     quad_y = y_idx.flatten()
    #     quad_x = x_idx.flatten()
    #     vis_row = np.clip((quad_y / scale_y).astype(int), 0, qvb_h - 1)
    #     vis_col = np.clip((quad_x / scale_x).astype(int), 0, qvb_w - 1)
    #     visible = quad_visibility[vis_row, vis_col]
    #     visible_6 = np.repeat(visible, 6)
    #     indices_arr = indices_arr[visible_6]

    # Pack binary data
    vertices_bin = vertices_arr.tobytes()
    normals_bin = normals_arr.tobytes()
    uvs_bin = uvs_arr.tobytes()
    indices_bin = indices_arr.tobytes()

    # Add texture if available
    texture_bytes = b""
    if color_image:
        img_buf = io.BytesIO()
        color_image.save(img_buf, format="PNG")
        texture_bytes = img_buf.getvalue()

    # Total BIN buffer
    bin_buffer = vertices_bin + normals_bin + uvs_bin + indices_bin + texture_bytes

    v_min = vertices_arr.min(axis=0).tolist()
    v_max = vertices_arr.max(axis=0).tolist()

    gltf = {
        "asset": {"version": "2.0", "generator": "extract_all_terrain.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": chunk_name}],
        "meshes": [
            {
                "primitives": [
                    {
                        "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                        "indices": 3,
                        **({"material": 0} if color_image else {}),
                    }
                ]
            }
        ],
        "buffers": [{"byteLength": len(bin_buffer)}],
        "bufferViews": [
            {
                "buffer": 0,
                "byteOffset": 0,
                "byteLength": len(vertices_bin),
                "target": 34962,
            },  # ARRAY_BUFFER
            {
                "buffer": 0,
                "byteOffset": len(vertices_bin),
                "byteLength": len(normals_bin),
                "target": 34962,
            },
            {
                "buffer": 0,
                "byteOffset": len(vertices_bin) + len(normals_bin),
                "byteLength": len(uvs_bin),
                "target": 34962,
            },
            {
                "buffer": 0,
                "byteOffset": len(vertices_bin) + len(normals_bin) + len(uvs_bin),
                "byteLength": len(indices_bin),
                "target": 34963,
            },  # ELEMENT_ARRAY_BUFFER
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
    }

    # Image and Texture support
    if color_image:
        img_view_idx = len(gltf["bufferViews"])
        gltf["bufferViews"].append(
            {
                "buffer": 0,
                "byteOffset": len(vertices_bin)
                + len(normals_bin)
                + len(uvs_bin)
                + len(indices_bin),
                "byteLength": len(texture_bytes),
            }
        )
        gltf["images"] = [{"bufferView": img_view_idx, "mimeType": "image/png"}]
        gltf["textures"] = [{"source": 0}]
        gltf["materials"] = [
            {
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": 0},
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                }
            }
        ]

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    save_glb(output_path, gltf, bin_buffer)
    return True


def process_chunk(chunk_name, output_dir, conn=None, silent=False, high_detail=False):
    """Process a single chunk by name."""
    vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk_name}.vgr")

    if not os.path.exists(vgr_path):
        return None

    label = f"{chunk_name} (HD)" if high_detail else chunk_name
    if not silent:
        print(f"  {label}...", end=" ", flush=True)

    try:
        pkg = UE2Package(vgr_path)

        # Extract heightmap
        if high_detail:
            heights, grid_w, grid_h = extract_hd_heightmap(pkg, chunk_name)
            # Subsample 4096x4096 to 1024x1024 for the stitched GLB
            # (full resolution is preserved in per-tile exports)
            if heights is not None and grid_w > 1024:
                step = grid_w // 1024
                heights = heights[::step, ::step]
                grid_w = heights.shape[1]
                grid_h = heights.shape[0]
        else:
            heights, grid_size = extract_g16_heightmap(pkg, chunk_name)
            grid_w = grid_h = grid_size

        if heights is None:
            if not silent:
                print("NO HEIGHTMAP")
            return None

        # Extract color texture
        color_image = None
        if high_detail:
            # Try HD composited texture from layer weight maps first
            color_image = extract_hd_texture(pkg, chunk_name)
            if color_image:
                # QUALITY: Do not downsample HD textures to the low mesh grid resolution.
                # Preserve 2048 or higher for visual fidelity.
                hd_target_res = 2048
                if color_image.width > hd_target_res:
                    color_image = color_image.resize(
                        (hd_target_res, hd_target_res), Image.LANCZOS
                    )
        if color_image is None:
            color_image = extract_color_texture(pkg, chunk_name)

        # Determine output path
        suffix = "_terrain_hd.glb" if high_detail else "_terrain.glb"
        output_path = os.path.join(output_dir, f"{chunk_name}{suffix}")

        # TODO: Re-enable once hole placement is verified correct (see TODO.md)
        # quad_visibility = extract_quad_visibility(pkg)
        quad_visibility = None

        # Generate glTF
        generate_terrain_gltf(
            heights, color_image, output_path, chunk_name, grid_w, grid_h,
            quad_visibility=quad_visibility
        )

        # Save to database (optional, might need separate table or field for HD)
        if conn:
            try:
                cursor = conn.cursor()
                chunk_row = cursor.execute(
                    "SELECT id FROM chunks WHERE filename = ? OR filename = ?",
                    (chunk_name, chunk_name + ".vgr"),
                ).fetchone()

                if chunk_row:
                    column = "gltf_exported_hd" if high_detail else "gltf_exported"
                    path_column = "export_path_hd" if high_detail else "export_path"

                    # Ensure columns exist (heuristic: try to update, if fail, oh well)
                    cursor.execute(
                        f"""
                        UPDATE terrain_chunks 
                        SET {column} = 1, {path_column} = ?
                        WHERE chunk_id = ?
                    """,
                        (output_path, chunk_row[0]),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            f"""
                            INSERT INTO terrain_chunks 
                            (chunk_id, grid_size, {column}, {path_column})
                            VALUES (?, ?, 1, ?)
                        """,
                            (chunk_row[0], grid_w, output_path),
                        )
                    conn.commit()
            except Exception:
                pass

        color_status = "with texture" if color_image else "no texture"
        
        # Extract GrassAlpha density map
        grass_path = extract_grass_alpha(pkg, chunk_name)
        grass_status = "grass✓" if grass_path else "no grass"
        holes_status = f"holes:{(~quad_visibility).sum()}" if quad_visibility is not None else ""
        
        if not silent:
            parts = [f"{grid_w}x{grid_h}", color_status, grass_status]
            if holes_status:
                parts.append(holes_status)
            print(f"OK ({', '.join(parts)})")

        return {
            "chunk_name": chunk_name,
            "grid_w": grid_w,
            "grid_h": grid_h,
            "hd": high_detail,
        }

    except Exception as e:
        if not silent:
            print(f"ERROR: {e}")
        return None


def parse_chunk_coords(chunk_name):
    """Parse chunk name like 'chunk_n25_26' into (row, col) integers.
    
    'n' prefix means negative. E.g. chunk_n25_26 → (-25, 26), chunk_5_n3 → (5, -3).
    """
    import re
    m = re.match(r'chunk_(n?\d+)_(n?\d+)', chunk_name)
    if not m:
        return None
    def parse_coord(s):
        return -int(s[1:]) if s.startswith('n') else int(s)
    return (parse_coord(m.group(1)), parse_coord(m.group(2)))


def chunk_name_from_coords(row, col):
    """Convert (row, col) back to chunk name."""
    r = f"n{abs(row)}" if row < 0 else str(row)
    c = f"n{abs(col)}" if col < 0 else str(col)
    return f"chunk_{r}_{c}"


def stitch_heightmaps(heightmaps):
    """Average shared edges between adjacent chunks in-place.
    
    heightmaps: dict of chunk_name -> numpy array (512x512 float64)
    
    Adjacent chunks share edge vertices:
    - chunk at (r,c) row -1 (last) == chunk at (r+1,c) row 0 (first)
    - chunk at (r,c) col -1 (last) == chunk at (r,c+1) col 0 (first)
    """
    # Build coord lookup
    coord_map = {}
    for name in heightmaps:
        coords = parse_chunk_coords(name)
        if coords:
            coord_map[coords] = name

    stitched = 0
    for (r, c), name in coord_map.items():
        h = heightmaps[name]
        # Stitch with south neighbor (r+1, c) - our last row = their first row
        south = coord_map.get((r + 1, c))
        if south:
            hn = heightmaps[south]
            avg = (h[-1, :] + hn[0, :]) / 2.0
            h[-1, :] = avg
            hn[0, :] = avg
            stitched += 1
        # Stitch with east neighbor (r, c+1)
        # vx = -(col - 256) * 400, so col 0 is the +X (east) edge
        # Our col 0 meets (r, c+1)'s col 511
        east = coord_map.get((r, c + 1))
        if east:
            hn = heightmaps[east]
            avg = (h[:, 0] + hn[:, -1]) / 2.0
            h[:, 0] = avg
            hn[:, -1] = avg
            stitched += 1

    return stitched


def get_all_chunks():
    """Get list of all chunk VGR files."""
    if not os.path.exists(VANGUARD_MAPS):
        return []
    return sorted(
        [
            f.replace(".vgr", "")
            for f in os.listdir(VANGUARD_MAPS)
            if f.startswith("chunk_") and f.endswith(".vgr")
        ]
    )


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract terrain from VGR chunks (no umodel)"
    )
    parser.add_argument("--all", action="store_true", help="Process all chunk files")
    parser.add_argument("--chunk", type=str, help="Process single chunk by name")
    parser.add_argument(
        "--silent", action="store_true", help="Suppress all output except errors"
    )
    parser.add_argument(
        "--texture-only",
        action="store_true",
        help="Only extract color textures as PNG (faster)",
    )
    parser.add_argument(
        "--hd",
        action="store_true",
        help="Extract high-detail terrain (2048x2048 stitched tiles)",
    )
    parser.add_argument(
        "--tiles",
        action="store_true",
        help="Export HD tiles as 256 individual GLB files for LOD streaming",
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH) if os.path.exists(DB_PATH) else None

    # Texture-only mode: just extract PNGs
    if args.texture_only:
        chunks = get_all_chunks() if args.all else [args.chunk] if args.chunk else []
        print(f"Extracting textures for {len(chunks)} chunks...")
        for chunk in chunks:
            vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk}.vgr")
            if not os.path.exists(vgr_path):
                continue
            try:
                pkg = UE2Package(vgr_path)
                color_image = extract_color_texture(pkg, chunk)
                if color_image:
                    out_path = os.path.join(OUTPUT_DIR, f"{chunk}_texture.png")
                    color_image.save(out_path)
                    print(f"  {chunk}: OK")
                else:
                    print(f"  {chunk}: No texture")
            except Exception as e:
                print(f"  {chunk}: ERROR {e}")
        return

    # Tiles mode: export individual HD tiles for LOD streaming
    if args.tiles:
        chunks = get_all_chunks() if args.all else [args.chunk] if args.chunk else []
        print(f"Exporting HD tiles for {len(chunks)} chunks...")
        for chunk in chunks:
            vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk}.vgr")
            if not os.path.exists(vgr_path):
                print(f"  {chunk}: VGR not found")
                continue
            try:
                pkg = UE2Package(vgr_path)
                count = export_hd_tiles(pkg, chunk, OUTPUT_DIR)
                print(f"  {chunk}: {count} tiles exported")
            except Exception as e:
                print(f"  {chunk}: ERROR {e}")
        return

    if not args.silent:
        print("=" * 60)
        print("Terrain Extractor (Binary Parsing)")
        hd_label = " [HIGH DETAIL]" if args.hd else " [LOW DETAIL]"
        print("=" * 60 + hd_label)
        print(f"Output: {OUTPUT_DIR}")
        print()

    successful = []
    failed = []

    if args.all:
        chunks = get_all_chunks()
        if not args.silent:
            print(f"Processing {len(chunks)} chunks...")
            print()

        total_chunks = len(chunks)

        if not args.hd and not args.texture_only:
            # Two-pass extraction with edge stitching for low-detail terrain
            # Pass 1: Extract all raw heightmaps
            if not args.silent:
                print("Pass 1: Extracting heightmaps...")
            all_heights = {}
            all_pkgs = {}
            for i, chunk in enumerate(chunks):
                vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk}.vgr")
                if not os.path.exists(vgr_path):
                    failed.append(chunk)
                    continue
                try:
                    pkg = UE2Package(vgr_path)
                    heights, grid_size = extract_g16_heightmap(pkg, chunk)
                    if heights is not None:
                        all_heights[chunk] = heights
                        all_pkgs[chunk] = pkg
                    else:
                        failed.append(chunk)
                except Exception:
                    failed.append(chunk)
                print_progress_bar(
                    i + 1, total_chunks,
                    prefix="   Heightmaps:",
                    suffix=f"({i+1}/{total_chunks})",
                    length=40,
                )

            # Pass 2: Stitch edges
            if not args.silent:
                print()
            n_stitched = stitch_heightmaps(all_heights)
            if not args.silent:
                print(f"Pass 2: Stitched {n_stitched} edges")
                print("Pass 3: Generating GLBs...")

            # Pass 3: Generate GLBs with stitched heightmaps
            gen_chunks = list(all_heights.keys())
            for i, chunk in enumerate(gen_chunks):
                try:
                    heights = all_heights[chunk]
                    pkg = all_pkgs[chunk]
                    grid_w = grid_h = heights.shape[0]
                    color_image = extract_color_texture(pkg, chunk)
                    # TODO: Re-enable once hole placement is verified correct (see TODO.md)
                    # quad_visibility = extract_quad_visibility(pkg)
                    quad_visibility = None
                    suffix = "_terrain.glb"
                    output_path = os.path.join(OUTPUT_DIR, f"{chunk}{suffix}")
                    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                    generate_terrain_gltf(
                        heights, color_image, output_path, chunk, grid_w, grid_h,
                        quad_visibility=quad_visibility
                    )
                    # Extract GrassAlpha
                    extract_grass_alpha(pkg, chunk)
                    successful.append({"chunk_name": chunk, "grid_w": grid_w, "grid_h": grid_h, "hd": False})
                except Exception as e:
                    failed.append(chunk)
                print_progress_bar(
                    i + 1, len(gen_chunks),
                    prefix="   GLBs:",
                    suffix=f"({i+1}/{len(gen_chunks)})",
                    length=40,
                )
        else:
            # Original single-pass for HD or texture-only
            for i, chunk in enumerate(chunks):
                result = process_chunk(
                    chunk, OUTPUT_DIR, conn, silent=True, high_detail=args.hd
                )
                if result:
                    successful.append(result)
                else:
                    failed.append(chunk)
                print_progress_bar(
                    i + 1,
                    total_chunks,
                    prefix="   Progress:",
                    suffix=f"({i+1}/{total_chunks})",
                    length=40,
                )

    elif args.chunk:
        result = process_chunk(
            args.chunk, OUTPUT_DIR, conn, silent=args.silent, high_detail=args.hd
        )
        if result:
            successful.append(result)
        else:
            failed.append(args.chunk)

    else:
        if not args.silent:
            print("Usage:")
            print("  python extract_all_terrain.py --all [--hd]")
            print("  python extract_all_terrain.py --chunk chunk_n25_26 [--hd]")
        return

    if conn:
        conn.close()

    if not args.silent:
        print()
        print("=" * 60)
        print("Complete")
        print("=" * 60)
        print(f"Successful: {len(successful)}")
        print(f"Failed: {len(failed)}")


if __name__ == "__main__":
    main()
