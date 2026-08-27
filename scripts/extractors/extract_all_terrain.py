#!/usr/bin/env python3
"""
Extract terrain from all VGR chunks using direct binary parsing.
Does not require umodel or Wine - parses textures directly from VGR files.

Usage:
    python extract_all_terrain.py --all        # Process all VGR chunk files
    python extract_all_terrain.py --chunk X   # Process single chunk by name
    python extract_all_terrain.py --hd-layers --all  # Rebuild Godot terrain layer data
"""

import numpy as np
from PIL import Image, ImageFilter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import struct
import json
import base64
import io
import os
import re
import sqlite3
import math
from pathlib import Path

from vanguard_assets import config
from ue2 import UE2Package
from scripts.extractors.terrain_info_reader import parse_terrain_info_file

# Configuration
PROJECT_ROOT = str(config.PROJECT_ROOT)
DB_PATH = config.DB_PATH
VANGUARD_MAPS = os.path.join(config.ASSETS_PATH, "Maps")
OUTPUT_DIR = config.TERRAIN_GRID_DIR

def _configured_reference_dir(configured_path, legacy_path):
    """Prefer this extractor's configured reference output, with old layouts as fallback."""
    if os.path.isdir(configured_path):
        return configured_path
    if os.path.isdir(legacy_path):
        return legacy_path
    return configured_path


_LEGACY_REFERENCE_ROOT = os.path.join(
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ),
    "vanguard-client",
    "reference",
)

# Pre-extracted terrain info from Unreal-Library (reliable layer/tile data)
TERRAIN_INFO_DIR = _configured_reference_dir(
    config.REFERENCE_MAPS_DIR,
    os.path.join(_LEGACY_REFERENCE_ROOT, "Maps"),
)
SHADER_INFO_DIR = _configured_reference_dir(
    os.path.join(config.REFERENCE_DIR, "Shaders"),
    os.path.join(_LEGACY_REFERENCE_ROOT, "Shaders"),
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
NATIVE_TERRAIN_TEXTURE_COORD_SCALE = 1.0 / 204.8
NATIVE_TERRAIN_TEXTURE_REPEATS = (GRID_CENTER * 2 - 1) * VERTEX_SPACING * NATIVE_TERRAIN_TEXTURE_COORD_SCALE
TERRAIN_WEIGHT_MASK_SMOOTHING_RADIUS = 3.0
TERRAIN_LAYER_SCHEMA = "vges_terrain_chunk_layers"
TERRAIN_LAYER_SCHEMA_VERSION = 2
TERRAIN_MATERIAL_LIBRARY_SCHEMA = "vges_terrain_material_library"
TERRAIN_MATERIAL_LIBRARY_VERSION = 2
TERRAIN_MATERIAL_LIBRARY_NAME = "global"
TERRAIN_MATERIAL_LIBRARY_DIR = os.path.join(
    "terrain_material_libraries", TERRAIN_MATERIAL_LIBRARY_NAME
)
TERRAIN_ATLAS_CELL_SIZE = 512
TERRAIN_ATLAS_GUTTER = 8
TERRAIN_ATLAS_MAX_DIMENSION = 8192


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
    print(f"\r{prefix} |{bar}| {percent}% {suffix}", end=print_end, flush=True)
    if iteration == total:
        print()


def find_texture_exports(pkg, pattern):
    """Find texture exports matching a pattern."""
    results = []
    for exp in pkg.exports:
        if exp["class_name"] == "Texture" and pattern in exp["object_name"]:
            results.append(exp)
    return results


def extract_quad_visibility(pkg, grid_w=512, grid_h=512):
    """Extract QuadVisibilityBitmap from TerrainInfo in a VGR package.

    Stock UE2 stores this as TArray<DWORD>. Vanguard's 512×512 terrain chunks
    therefore serialize 8192 DWORDs = 32768 bytes = 262144 bits. The C++ access
    pattern is BitIndex = x + y * HeightmapX, with bit 0 in each DWORD covering
    the lowest X coordinate. Bit=1 means visible, bit=0 means invisible (hole).

    The last row/column have bits because the array size is HeightmapX*HeightmapY,
    but the renderer only emits quads for x < HeightmapX-1 and y < HeightmapY-1.

    Returns:
        numpy bool array of shape (grid_h, grid_w) where True=visible,
        or None if not found or all rendered quads are visible.
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
        try:
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
            else:
                continue
        except (IndexError, struct.error):
            continue

        try:
            count, payload_start = _ci(data, p)
        except IndexError:
            continue
        count_size = payload_start - p
        expected_words = (grid_w * grid_h + 31) // 32
        if count != expected_words:
            continue

        byte_len = count * 4
        raw = data[payload_start : payload_start + byte_len]
        if len(raw) < byte_len or size < count_size + byte_len:
            continue

        # Unpack DWORD bits with the same little-endian bit order as
        # GetQuadVisibilityBitmap(): (word & (1 << (BitIndex & 0x1f))).
        bitmap = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little")
        bitmap = bitmap[: grid_w * grid_h].reshape(grid_h, grid_w).astype(bool)

        # Ignore the unused final row/column when deciding whether the mesh needs
        # holes. UE2 allocates bits for them but does not emit those quads.
        if bitmap[: grid_h - 1, : grid_w - 1].all():
            return None

        return bitmap

    return None


def count_rendered_invisible_quads(quad_visibility):
    if quad_visibility is None:
        return 0
    return int((~quad_visibility[:-1, :-1]).sum())


def filter_indices_by_quad_visibility(
    indices_arr,
    quad_x,
    quad_y,
    quad_visibility,
    mesh_grid_w,
    mesh_grid_h,
):
    """Remove triangle indices for invisible terrain quads.

    quad_x/quad_y are mesh-space quad coordinates. The terrain mesh is generated
    in the same glTF swizzle used for placed objects: mesh rows correspond to
    Vanguard/UE terrain X and mesh columns correspond to Vanguard/UE terrain Y.
    QuadVisibilityBitmap uses the stock UE2 lookup bit_index = x + y * width, so
    mesh row/column must be transposed back to bitmap X/Y before lookup. For
    upsampled meshes, the coordinates are scaled back to the original 512x512
    QuadVisibilityBitmap coordinate space first.
    """
    if quad_visibility is None:
        return indices_arr

    qvb_h, qvb_w = quad_visibility.shape
    if qvb_h < 2 or qvb_w < 2:
        return indices_arr

    mesh_col_to_qvb_y = (mesh_grid_w - 1) / (qvb_h - 1)
    mesh_row_to_qvb_x = (mesh_grid_h - 1) / (qvb_w - 1)
    qvb_y = np.floor(quad_x / mesh_col_to_qvb_y).astype(np.int32)
    qvb_x = np.floor(quad_y / mesh_row_to_qvb_x).astype(np.int32)
    qvb_y = np.clip(qvb_y, 0, qvb_h - 2)
    qvb_x = np.clip(qvb_x, 0, qvb_w - 2)

    visible = quad_visibility[qvb_y, qvb_x]
    return indices_arr[np.repeat(visible, 6)]


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


def extract_chunk_shadow_map(pkg, chunk_name):
    """Decode Vanguard's baked per-chunk vegetation shadow mask.

    Modern Vanguard chunks do not serialize UE2's legacy ``VertexColors``
    array. Their equivalent cooked terrain-lighting input is a 512x512 L8
    texture whose export name starts with ``ChunkShadow_``. Some packages keep
    a stale object-name suffix copied from another chunk, so the containing VGR
    package—not that suffix—is the authoritative chunk association.

    Returns:
        ``(image, metadata)`` where image is an 8-bit grayscale PIL image, or
        ``(None, None)`` when the package has no usable chunk-shadow texture.
    """
    from ue2.texture import Texture

    candidates = [
        exp
        for exp in pkg.exports
        if exp["class_name"] == "Texture"
        and exp["object_name"].lower().startswith("chunkshadow_")
    ]
    if not candidates:
        return None, None

    expected_name = f"ChunkShadow_{chunk_name}Height".lower()
    candidates.sort(
        key=lambda exp: 0 if exp["object_name"].lower() == expected_name else 1
    )
    for exp in candidates:
        try:
            texture = Texture(pkg.get_export_data(exp), pkg.names)
            image = texture.get_image(0)
            if image is None:
                continue
            if image.mode != "L":
                image = image.convert("L")
            return image, {
                "file": "chunk_shadow.png",
                "format": "l8",
                "size": [image.width, image.height],
                "source_export": exp["object_name"],
                "source_chunk": chunk_name,
                "association": "containing_vgr_package",
                "semantic": "baked_vegetation_shadow_mask",
                "unshadowed_value": 255,
                "note": (
                    "This is not vertex color or a terrain paint weight; darker "
                    "values are baked vegetation/tree shadows."
                ),
            }
        except Exception:
            continue
    return None, None


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


def _identity_uv_transform():
    return {
        "m00": 1.0,
        "m01": 0.0,
        "m10": 0.0,
        "m11": 1.0,
        "u_offset": 0.0,
        "v_offset": 0.0,
    }


def _reference_object_text(package_name, object_name):
    path = os.path.join(SHADER_INFO_DIR, package_name, f"{object_name}.txt")
    if not os.path.exists(path):
        return None
    with open(path, "r", errors="ignore") as f:
        return f.read()


def _reference_package_and_object(ref_path):
    parts = ref_path.split(".")
    if len(parts) < 2:
        return None, parts[-1]
    return parts[0], parts[-1]


def _package_qualified_ref(package_name, object_name_or_ref):
    value = str(object_name_or_ref or "")
    if "." in value:
        return value
    return f"{package_name}.{value}" if package_name and value else value


def _float_property(text, name, default):
    m = re.search(rf"\b{name}=([-+]?\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else default


def _plane_from_matrix_text(text, plane_name):
    m = re.search(
        rf"{plane_name}=\(W=([-+]?\d+(?:\.\d+)?),X=([-+]?\d+(?:\.\d+)?),"
        rf"Y=([-+]?\d+(?:\.\d+)?),Z=([-+]?\d+(?:\.\d+)?)\)",
        text,
    )
    if not m:
        return None
    return {
        "w": float(m.group(1)),
        "x": float(m.group(2)),
        "y": float(m.group(3)),
        "z": float(m.group(4)),
    }


def _tex_scaler_transform(text):
    u_scale = _float_property(text, "UScale", 1.0)
    v_scale = _float_property(text, "VScale", 1.0)
    u_offset = _float_property(text, "UOffset", 0.0)
    v_offset = _float_property(text, "VOffset", 0.0)
    x_plane = _plane_from_matrix_text(text, "XPlane")
    y_plane = _plane_from_matrix_text(text, "YPlane")

    if x_plane and y_plane:
        transform = {
            "m00": x_plane["x"],
            "m01": x_plane["y"],
            "m10": y_plane["x"],
            "m11": y_plane["y"],
            "u_offset": u_offset,
            "v_offset": v_offset,
        }
        source = "TexScaler.M"
    else:
        transform = {
            "m00": 1.0 / u_scale if u_scale != 0.0 else 1.0,
            "m01": 0.0,
            "m10": 0.0,
            "m11": 1.0 / v_scale if v_scale != 0.0 else 1.0,
            "u_offset": u_offset,
            "v_offset": v_offset,
        }
        source = "TexScaler.UScale/VScale"

    return transform, u_scale, v_scale, source


def _resolve_reference_material(package_name, object_name, object_type="Shader", visited=None):
    if visited is None:
        visited = set()
    key = (package_name, object_name, object_type)
    if key in visited:
        return None
    visited.add(key)

    text = _reference_object_text(package_name, object_name)
    if not text:
        return None

    if object_type == "Texture":
        return {
            "texture_name": object_name,
            "texture_package": package_name,
            "texture_ref": _package_qualified_ref(package_name, object_name),
            "diffuse_target_type": "Texture",
            "diffuse_target_name": object_name,
            "uv_transform": _identity_uv_transform(),
            "uv_transform_source": "direct texture",
        }

    if object_type == "Shader":
        m = re.search(r"Diffuse=(\w+)'([^']+)'", text)
        if not m:
            return None
        ref_type, ref_path = m.group(1), m.group(2)
        ref_package, ref_object = _reference_package_and_object(ref_path)
        if ref_type == "Texture":
            texture_package = ref_package or package_name
            return {
                "texture_name": ref_object,
                "texture_package": texture_package,
                "texture_ref": _package_qualified_ref(texture_package, ref_path),
                "diffuse_target_type": "Texture",
                "diffuse_target_name": ref_path,
                "uv_transform": _identity_uv_transform(),
                "uv_transform_source": "Shader.Diffuse texture",
            }
        resolved = _resolve_reference_material(
            ref_package or package_name, ref_object, ref_type, visited
        )
        if resolved:
            resolved["diffuse_target_type"] = ref_type
            resolved["diffuse_target_name"] = ref_path
        return resolved

    if object_type == "TexScaler":
        transform, u_scale, v_scale, transform_source = _tex_scaler_transform(text)
        m = re.search(r"Material=(\w+)'([^']+)'", text)
        if not m:
            return None
        ref_type, ref_path = m.group(1), m.group(2)
        ref_package, ref_object = _reference_package_and_object(ref_path)
        if ref_type != "Texture":
            return None
        texture_package = ref_package or package_name
        return {
            "texture_name": ref_object,
            "texture_package": texture_package,
            "texture_ref": _package_qualified_ref(texture_package, ref_path),
            "tex_scaler": object_name,
            "tex_scaler_u_scale": u_scale,
            "tex_scaler_v_scale": v_scale,
            "tex_scaler_material": ref_path,
            "uv_transform": transform,
            "uv_transform_source": transform_source,
        }

    return None


def _find_texture_export(pkg, texture_name):
    texture_name_lower = texture_name.lower()
    for exp in pkg.exports:
        if (
            exp["class_name"] == "Texture"
            and exp["object_name"].lower() == texture_name_lower
        ):
            return exp
    return None


def _find_export(pkg, object_name, class_name=None):
    object_name_lower = object_name.lower()
    for exp in pkg.exports:
        if exp["object_name"].lower() != object_name_lower:
            continue
        if class_name is not None and exp["class_name"] != class_name:
            continue
        return exp
    return None


def _export_from_object_ref(pkg, object_ref):
    if not isinstance(object_ref, int) or object_ref <= 0:
        return None
    export_index = object_ref - 1
    if export_index >= len(pkg.exports):
        return None
    return pkg.exports[export_index]


def _open_texture_package(package_name):
    if not package_name:
        return None
    if not hasattr(_open_texture_package, "_cache"):
        _open_texture_package._cache = {}
    cache = _open_texture_package._cache
    if package_name in cache:
        return cache[package_name]

    pkg_path = os.path.join(config.TEXTURES_DIR, package_name + ".utx")
    if not os.path.exists(pkg_path):
        cache[package_name] = None
        return None

    try:
        cache[package_name] = UE2Package(pkg_path)
    except Exception:
        cache[package_name] = None
    return cache[package_name]


def _load_texture_palette(pkg, texture_export):
    from ue2.texture import Texture as Tex

    try:
        data = pkg.get_export_data(texture_export)
        tex_obj = Tex(data, pkg.names)
        if tex_obj.format_id != 0:
            return None

        palette_ref = tex_obj.properties.get("Palette")
        if palette_ref is None:
            return None

        palette_index = int(palette_ref) - 1
        if not (0 <= palette_index < len(pkg.exports)):
            return None

        palette_export = pkg.exports[palette_index]
        if palette_export["class_name"] != "Palette":
            return None

        palette_data = pkg.get_export_data(palette_export)
        if len(palette_data) < 5:
            return None

        count = struct.unpack("<i", palette_data[1:5])[0]
        if count != 256 or len(palette_data) < 5 + count * 4:
            return None

        palette = []
        for index in range(count):
            offset = 5 + index * 4
            palette.append(
                (
                    palette_data[offset],
                    palette_data[offset + 1],
                    palette_data[offset + 2],
                    palette_data[offset + 3],
                )
            )
        return palette
    except Exception:
        return None


def _texture_image_from_export(pkg, texture_export):
    from ue2.texture import Texture as Tex

    try:
        texture_data = pkg.get_export_data(texture_export)
        palette = _load_texture_palette(pkg, texture_export)
        texture = Tex(texture_data, pkg.names, palette=palette)
        image = texture.get_image(0)
        return image.convert("RGB") if image else None
    except Exception:
        return None


def _texture_image_from_package(package_name, texture_name):
    pkg = _open_texture_package(package_name)
    if pkg is None:
        return None

    texture_exp = _find_texture_export(pkg, texture_name)
    if texture_exp is None:
        return None

    return _texture_image_from_export(pkg, texture_exp)


def _bump_scale_to_normal_strength(bump_scale):
    try:
        scale = float(bump_scale)
    except (TypeError, ValueError):
        scale = 1.0
    return max(0.2, min(3.0, scale / 32.0))


def _bump_image_to_normal_map(image, bump_scale):
    height = np.asarray(image.convert("L"), dtype=np.float32) / 255.0
    strength = _bump_scale_to_normal_strength(bump_scale)
    dx = np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)
    dy = np.roll(height, -1, axis=0) - np.roll(height, 1, axis=0)
    nx = -dx * strength
    ny = -dy * strength
    nz = np.ones_like(height, dtype=np.float32)
    length = np.sqrt(nx * nx + ny * ny + nz * nz)
    normal = np.stack(
        [
            (nx / length) * 0.5 + 0.5,
            (ny / length) * 0.5 + 0.5,
            (nz / length) * 0.5 + 0.5,
        ],
        axis=2,
    )
    normal = np.clip(normal * 255.0, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(normal, "RGB")


def _export_properties(pkg, export, preferred_starts=None):
    from ue2.properties import find_property_start, parse_properties

    data = pkg.get_export_data(export)
    starts = []
    if preferred_starts:
        starts.extend(preferred_starts)

    detected_start = find_property_start(data, pkg.names)
    if detected_start >= 0:
        starts.append(detected_start)
    starts.append(0)

    seen = set()
    for start in starts:
        if start in seen or start < 0 or start >= len(data):
            continue
        seen.add(start)
        props = parse_properties(data, pkg.names, start)
        if props:
            return props
    return []


def _property_value(properties, name, default=None):
    for prop in properties:
        if prop.get("name") == name:
            return prop.get("value")
    return default


def _normal_material_record_from_export(
    pkg, package_name, normal_export, source_property="Normal"
):
    normal_type = normal_export["class_name"]
    normal_name = normal_export["object_name"]
    result = {
        "normal_material_type": normal_type,
        "normal_material_name": f"{package_name}.{normal_name}",
    }

    if normal_type == "Texture":
        image = _texture_image_from_export(pkg, normal_export)
        result.update(
            {
                "normal_texture_package": package_name,
                "normal_texture_name": normal_name,
                "normal_source_property": source_property,
            }
        )
        if image is not None:
            result["normal_image"] = image
        return result

    if normal_type != "NormalBitmapMaterial":
        return result

    properties = _export_properties(pkg, normal_export, preferred_starts=[0])
    bump_ref = _property_value(properties, "BumpMap")
    bump_export = _export_from_object_ref(pkg, bump_ref)
    if bump_export is None or bump_export["class_name"] != "Texture":
        return result

    image = _texture_image_from_export(pkg, bump_export)
    result.update(
        {
            "normal_texture_package": package_name,
            "normal_texture_name": bump_export["object_name"],
            "normal_source_property": "BumpMap",
            "normal_bump_scale": _property_value(properties, "BumpScale", 1.0),
        }
    )
    if image is not None:
        result["normal_image"] = image
    return result


def _resolve_shader_normal_material(package_name, shader_name):
    shader_text = _reference_object_text(package_name, shader_name)
    if not shader_text:
        return None

    normal_match = re.search(r"\bNormal=(\w+)'([^']+)'", shader_text)
    if not normal_match:
        return None

    normal_type, normal_path = normal_match.group(1), normal_match.group(2)
    normal_package, normal_object = _reference_package_and_object(normal_path)
    normal_package = normal_package or package_name

    result = {
        "normal_material_type": normal_type,
        "normal_material_name": normal_path,
    }

    if normal_type == "Texture":
        image = _texture_image_from_package(normal_package, normal_object)
        if image is None:
            return result
        result.update(
            {
                "normal_texture_package": normal_package,
                "normal_texture_name": normal_object,
                "normal_source_property": "Normal",
                "normal_image": image,
            }
        )
        return result

    if normal_type != "NormalBitmapMaterial":
        return result

    normal_text = _reference_object_text(normal_package, normal_object)
    if not normal_text:
        return result

    bump_match = re.search(r"\bBumpMap=Texture'([^']+)'", normal_text)
    if not bump_match:
        return result

    bump_path = bump_match.group(1)
    bump_package, bump_object = _reference_package_and_object(bump_path)
    bump_package = bump_package or normal_package
    image = _texture_image_from_package(bump_package, bump_object)

    result.update(
        {
            "normal_texture_package": bump_package,
            "normal_texture_name": bump_object,
            "normal_source_property": "BumpMap",
            "normal_bump_scale": _float_property(normal_text, "BumpScale", 1.0),
        }
    )
    if image is not None:
        result["normal_image"] = image
    return result


def _resolve_shader_normal_material_from_binary(utx_pkg, shader_export, package_name):
    properties = _export_properties(utx_pkg, shader_export)
    normal_ref = _property_value(properties, "Normal")
    normal_export = _export_from_object_ref(utx_pkg, normal_ref)
    if normal_export is None:
        return None
    return _normal_material_record_from_export(utx_pkg, package_name, normal_export)


def _terrain_shader_record_from_reference(utx_pkg, package_name, shader_name):
    from ue2.texture import Texture as Tex

    resolved = _resolve_reference_material(package_name, shader_name, "Shader")
    if not resolved:
        return None

    texture_name = resolved.get("texture_name")
    texture_package = resolved.get("texture_package") or package_name
    texture_pkg = (
        utx_pkg
        if texture_package.lower() == package_name.lower()
        else _open_texture_package(texture_package)
    )
    if texture_pkg is None:
        return None
    texture_exp = _find_texture_export(texture_pkg, texture_name)
    if texture_exp is None:
        return None

    diff_data = texture_pkg.get_export_data(texture_exp)
    diff_tex = Tex(diff_data, texture_pkg.names)
    img = diff_tex.get_image(0)
    if not img:
        return None

    resolved["package"] = package_name
    resolved["source_package"] = package_name
    resolved["source_material_ref"] = f"{package_name}.Shaders.{shader_name}"
    resolved["shader_name"] = shader_name
    resolved["image"] = img.convert("RGB")
    normal_record = _resolve_shader_normal_material(package_name, shader_name)
    if not normal_record or normal_record.get("normal_image") is None:
        shader_export = _find_export(utx_pkg, shader_name, "Shader")
        if shader_export is not None:
            binary_normal_record = _resolve_shader_normal_material_from_binary(
                utx_pkg, shader_export, package_name
            )
            if binary_normal_record:
                normal_record = binary_normal_record
    if normal_record:
        resolved.update(normal_record)
    return resolved


def _terrain_shader_record_from_binary(utx_pkg, shader_export, package_name):
    from ue2.texture import Texture as Tex
    from ue2.properties import find_property_start, parse_properties

    edata = utx_pkg.get_export_data(shader_export)
    estart = find_property_start(edata, utx_pkg.names)
    eprops = parse_properties(edata, utx_pkg.names, estart)
    diffuse_ref = None
    for p in eprops:
        if p["name"] == "Diffuse":
            diffuse_ref = p["value"]
            break
    if not diffuse_ref or not isinstance(diffuse_ref, int) or diffuse_ref <= 0:
        return None
    diff_idx = diffuse_ref - 1
    if diff_idx >= len(utx_pkg.exports):
        return None
    diff_exp = utx_pkg.exports[diff_idx]
    if diff_exp["class_name"] != "Texture":
        return None
    diff_data = utx_pkg.get_export_data(diff_exp)
    diff_tex = Tex(diff_data, utx_pkg.names)
    img = diff_tex.get_image(0)
    if not img:
        return None
    record = {
        "package": package_name,
        "source_package": package_name,
        "source_material_ref": f"{package_name}.Shaders.{shader_export['object_name']}",
        "shader_name": shader_export["object_name"],
        "texture_name": diff_exp["object_name"],
        "texture_package": package_name,
        "texture_ref": _package_qualified_ref(package_name, diff_exp["object_name"]),
        "diffuse_target_type": "Texture",
        "diffuse_target_name": diff_exp["object_name"],
        "uv_transform": _identity_uv_transform(),
        "uv_transform_source": "Shader.Diffuse texture",
        "image": img.convert("RGB"),
    }
    normal_record = _resolve_shader_normal_material_from_binary(
        utx_pkg, shader_export, package_name
    )
    if normal_record:
        record.update(normal_record)
    return record


def _build_terrain_shader_metadata_cache():
    """Build shader metadata, including diffuse texture and UV transform."""
    if hasattr(_build_terrain_shader_metadata_cache, "_cache"):
        return _build_terrain_shader_metadata_cache._cache

    textures_dir = config.TEXTURES_DIR
    cache = {}

    for fname in sorted(os.listdir(textures_dir)):
        if not fname.endswith(".utx"):
            continue
        name_lower = fname.lower()
        if "terrain" not in name_lower and "grass" not in name_lower:
            continue

        pkg_path = os.path.join(textures_dir, fname)
        package_name = os.path.splitext(fname)[0]
        try:
            utx_pkg = UE2Package(pkg_path)
            for e in utx_pkg.exports:
                if e["class_name"] != "Shader":
                    continue
                shader_name = e["object_name"]
                if shader_name in cache:
                    continue
                try:
                    record = _terrain_shader_record_from_reference(
                        utx_pkg, package_name, shader_name
                    )
                    if record is None:
                        record = _terrain_shader_record_from_binary(
                            utx_pkg, e, package_name
                        )
                    if record is not None:
                        cache[shader_name] = record
                except Exception:
                    continue
        except Exception:
            continue

    _build_terrain_shader_metadata_cache._cache = cache
    return cache


def _build_terrain_shader_cache():
    """Build a cache mapping shader name -> PIL.Image (diffuse texture)."""
    if hasattr(_build_terrain_shader_cache, "_cache"):
        return _build_terrain_shader_cache._cache

    metadata_cache = _build_terrain_shader_metadata_cache()
    cache = {
        shader_name: record["image"]
        for shader_name, record in metadata_cache.items()
        if record.get("image") is not None
    }
    _build_terrain_shader_cache._cache = cache
    return cache


def _exportable_shader_metadata(record):
    result = {
        "package": record.get("package", ""),
        "source_package": record.get("source_package", record.get("package", "")),
        "source_material_ref": record.get("source_material_ref", ""),
        "texture_name": record.get("texture_name", ""),
        "texture_package": record.get("texture_package", record.get("package", "")),
        "texture_ref": record.get("texture_ref", ""),
        "diffuse_target_type": record.get("diffuse_target_type", "Texture"),
        "diffuse_target_name": record.get("diffuse_target_name", ""),
        "uv_transform": record.get("uv_transform", _identity_uv_transform()),
        "uv_transform_source": record.get("uv_transform_source", ""),
    }
    if "tex_scaler" in record:
        result["tex_scaler"] = record["tex_scaler"]
        result["tex_scaler_u_scale"] = record.get("tex_scaler_u_scale", 1.0)
        result["tex_scaler_v_scale"] = record.get("tex_scaler_v_scale", 1.0)
        result["tex_scaler_material"] = record.get("tex_scaler_material", "")
    if "normal_material_name" in record:
        result["normal_material_type"] = record.get("normal_material_type", "")
        result["normal_material_name"] = record.get("normal_material_name", "")
    if "normal_texture_name" in record:
        result["normal_texture_package"] = record.get("normal_texture_package", "")
        result["normal_texture_name"] = record.get("normal_texture_name", "")
        result["normal_source_property"] = record.get("normal_source_property", "")
    if "normal_bump_scale" in record:
        result["normal_bump_scale"] = record.get("normal_bump_scale", 1.0)
    return result


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

    Prefers pre-extracted terrain_info.txt from Unreal-Library (reliable),
    falls back to binary scanning if the file doesn't exist.

    Returns:
        shader_images: dict shader_name -> PIL.Image for all resolvable shaders
        num_layers: int total number of resolvable shader layers
        all_layers: the full parsed layer list as [(seq_idx, shader_name), ...]
        tile_layer_data: dict mapping_array_idx -> sequential_layer_index
    """
    # Try pre-extracted terrain_info.txt first (from Unreal-Library)
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


def _smooth_weight_map_tile_local(weight_map, tile_pix, radius):
    if radius <= 0:
        return weight_map.copy()

    smoothed = np.zeros_like(weight_map)
    height, width, channels = weight_map.shape
    for y0 in range(0, height, tile_pix):
        y1 = min(y0 + tile_pix, height)
        for x0 in range(0, width, tile_pix):
            x1 = min(x0 + tile_pix, width)
            for channel in range(channels):
                tile = weight_map[y0:y1, x0:x1, channel]
                blurred = Image.fromarray(tile, "L").filter(ImageFilter.GaussianBlur(radius))
                smoothed[y0:y1, x0:x1, channel] = np.array(blurred, dtype=np.uint8)
    return smoothed


def _relpath_for_manifest(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _terrain_atlas_layout(material_count):
    stride = TERRAIN_ATLAS_CELL_SIZE + TERRAIN_ATLAS_GUTTER * 2
    max_cols = max(1, TERRAIN_ATLAS_MAX_DIMENSION // stride)
    cols = min(max_cols, max(1, material_count))
    rows = int(math.ceil(material_count / float(cols))) if material_count > 0 else 1
    width = cols * stride
    height = rows * stride
    return cols, rows, width, height, stride


def _resize_terrain_source_image(image, fill_color):
    if image is None:
        return Image.new("RGB", (TERRAIN_ATLAS_CELL_SIZE, TERRAIN_ATLAS_CELL_SIZE), fill_color)
    return image.convert("RGB").resize(
        (TERRAIN_ATLAS_CELL_SIZE, TERRAIN_ATLAS_CELL_SIZE),
        Image.Resampling.LANCZOS,
    )


def _blit_terrain_atlas_cell(atlas, image, index, columns, stride):
    cell = TERRAIN_ATLAS_CELL_SIZE
    gutter = TERRAIN_ATLAS_GUTTER
    col = index % columns
    row = index // columns
    x0 = col * stride
    y0 = row * stride
    inner_x = x0 + gutter
    inner_y = y0 + gutter

    atlas.paste(image, (inner_x, inner_y))
    if gutter <= 0:
        return

    atlas.paste(image.crop((0, 0, cell, 1)).resize((cell, gutter)), (inner_x, y0))
    atlas.paste(
        image.crop((0, cell - 1, cell, cell)).resize((cell, gutter)),
        (inner_x, inner_y + cell),
    )
    atlas.paste(image.crop((0, 0, 1, cell)).resize((gutter, cell)), (x0, inner_y))
    atlas.paste(
        image.crop((cell - 1, 0, cell, cell)).resize((gutter, cell)),
        (inner_x + cell, inner_y),
    )

    atlas.paste(image.crop((0, 0, 1, 1)).resize((gutter, gutter)), (x0, y0))
    atlas.paste(
        image.crop((cell - 1, 0, cell, 1)).resize((gutter, gutter)),
        (inner_x + cell, y0),
    )
    atlas.paste(
        image.crop((0, cell - 1, 1, cell)).resize((gutter, gutter)),
        (x0, inner_y + cell),
    )
    atlas.paste(
        image.crop((cell - 1, cell - 1, cell, cell)).resize((gutter, gutter)),
        (inner_x + cell, inner_y + cell),
    )


def _terrain_material_source_image(record, atlas_kind):
    if record is None:
        return None
    if atlas_kind == "diffuse":
        return record.get("image")
    if atlas_kind == "bump":
        return record.get("normal_image")
    if atlas_kind == "normal" and record.get("normal_image") is not None:
        return _bump_image_to_normal_map(
            record["normal_image"],
            record.get("normal_bump_scale", 1.0),
        )
    return None


def _save_terrain_atlas(material_names, shader_metadata_cache, atlas_kind, fill_color, path):
    cols, rows, _width, _height, stride = _terrain_atlas_layout(len(material_names))
    atlas = Image.new("RGB", (_width, _height), fill_color)
    fallback_image = None
    for index, material_name in enumerate(material_names):
        record = shader_metadata_cache.get(material_name)
        source = _terrain_material_source_image(record, atlas_kind)
        image = _resize_terrain_source_image(source, fill_color)
        if source is not None:
            fallback_image = image
        _blit_terrain_atlas_cell(atlas, image, index, cols, stride)
    if fallback_image is not None:
        for index in range(len(material_names), cols * rows):
            _blit_terrain_atlas_cell(atlas, fallback_image, index, cols, stride)
    atlas.save(path)


def _material_library_needs_rebuild(manifest_path, material_names):
    if not os.path.exists(manifest_path):
        return True
    try:
        with open(manifest_path, "r") as f:
            existing = json.load(f)
    except Exception:
        return True
    if existing.get("schema") != TERRAIN_MATERIAL_LIBRARY_SCHEMA:
        return True
    if int(existing.get("version", 0)) != TERRAIN_MATERIAL_LIBRARY_VERSION:
        return True
    return list(existing.get("materials", {}).keys()) != material_names


def _build_shared_terrain_material_library(output_dir, material_names):
    """Build a shared terrain material atlas library once per export batch."""
    library_dir = os.path.join(output_dir, TERRAIN_MATERIAL_LIBRARY_DIR)
    os.makedirs(library_dir, exist_ok=True)
    manifest_path = os.path.join(library_dir, "material_library.json")
    existing_material_names = []
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r") as f:
                existing_manifest = json.load(f)
            if existing_manifest.get("schema") == TERRAIN_MATERIAL_LIBRARY_SCHEMA:
                existing_material_names = list(existing_manifest.get("materials", {}).keys())
        except Exception:
            existing_material_names = []

    if existing_material_names:
        new_material_names = sorted(
            set(material_names).difference(existing_material_names),
            key=lambda value: value.lower(),
        )
        material_names = existing_material_names + new_material_names
    else:
        material_names = sorted(set(material_names), key=lambda value: value.lower())
    if not material_names:
        return None

    cols, rows, width, height, stride = _terrain_atlas_layout(len(material_names))
    shader_metadata_cache = _build_terrain_shader_metadata_cache()
    diffuse_atlas = os.path.join(library_dir, "diffuse_atlas.png")
    bump_atlas = os.path.join(library_dir, "bump_atlas.png")
    normal_atlas = os.path.join(library_dir, "normal_atlas.png")

    if _material_library_needs_rebuild(manifest_path, material_names):
        _save_terrain_atlas(
            material_names, shader_metadata_cache, "diffuse", (0, 0, 0), diffuse_atlas
        )
        _save_terrain_atlas(
            material_names, shader_metadata_cache, "bump", (128, 128, 128), bump_atlas
        )
        _save_terrain_atlas(
            material_names, shader_metadata_cache, "normal", (128, 128, 255), normal_atlas
        )

        materials = {}
        for index, material_name in enumerate(material_names):
            record = shader_metadata_cache.get(material_name, {})
            materials[material_name] = {
                "index": index,
                "atlas_col": index % cols,
                "atlas_row": index // cols,
                "has_diffuse": record.get("image") is not None,
                "has_bump": record.get("normal_image") is not None,
                "has_normal": record.get("normal_image") is not None,
                "metadata": _exportable_shader_metadata(record) if record else {},
            }

        manifest = {
            "schema": TERRAIN_MATERIAL_LIBRARY_SCHEMA,
            "version": TERRAIN_MATERIAL_LIBRARY_VERSION,
            "name": TERRAIN_MATERIAL_LIBRARY_NAME,
            "material_count": len(material_names),
            "atlas": {
                "cell_size": TERRAIN_ATLAS_CELL_SIZE,
                "gutter": TERRAIN_ATLAS_GUTTER,
                "stride": stride,
                "columns": cols,
                "rows": rows,
                "width": width,
                "height": height,
                "max_dimension": TERRAIN_ATLAS_MAX_DIMENSION,
            },
            "atlases": {
                "diffuse": _relpath_for_manifest(diffuse_atlas, output_dir),
                "bump": _relpath_for_manifest(bump_atlas, output_dir),
                "normal": _relpath_for_manifest(normal_atlas, output_dir),
            },
            "materials": materials,
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

    with open(manifest_path, "r") as f:
        manifest = json.load(f)
    manifest["_manifest_path"] = manifest_path
    return manifest


def _decode_hd_layer_weight_maps(pkg, tile_pix, full_size, slot_to_bit):
    from ue2.texture import Texture as Tex

    full_weight = np.zeros((full_size, full_size), dtype=np.uint8)
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
                    r * tile_pix : (r + 1) * tile_pix,
                    c * tile_pix : (c + 1) * tile_pix,
                ] = _decode_weight_tile(raw[:32768]).T
                tile_coords[(r, c)] = True
        except Exception:
            continue

    wmap_01 = np.zeros((full_size, full_size, 3), dtype=np.uint8)
    wmap_23 = np.zeros((full_size, full_size, 3), dtype=np.uint8)
    for slot in range(4):
        bit = slot_to_bit[slot]
        channel_data = ((full_weight >> bit) & 1) * 255
        if slot < 2:
            wmap_01[:, :, slot] = channel_data
        else:
            wmap_23[:, :, slot - 2] = channel_data
    return wmap_01, wmap_23, tile_coords


def export_hd_layer_data(
    pkg, chunk_name, output_dir, material_library=None, resolved_layers=None, silent=False
):
    """Export chunk-specific HD terrain masks and shared-library indices.

    Reusable terrain textures are written once under
    terrain_material_libraries/global/. Each chunk gets only its base fallback,
    weight maps, and tile slot mappings into that library.
    """
    TILES = 16
    TILE_PIX = 256
    FULL_SIZE = TILES * TILE_PIX  # 4096
    SLOT_TO_BIT = [3, 2, 1, 0]

    res = resolved_layers or _resolve_layer_textures(pkg, chunk_name)
    if not res:
        return None
    shader_images, _num_layers, all_layers, tile_layer_data = res
    if not shader_images:
        return None

    material_names = sorted(shader_images.keys(), key=lambda value: value.lower())
    if material_library is None:
        material_library = _build_shared_terrain_material_library(output_dir, material_names)
    if material_library is None:
        return None

    material_entries = material_library.get("materials", {})
    material_indices = {
        name: material_entries[name]["index"]
        for name in material_names
        if name in material_entries
    }
    if not material_indices:
        return None

    layers_dir = os.path.join(output_dir, f"{chunk_name}_terrain_layers")
    os.makedirs(layers_dir, exist_ok=True)

    base_img = extract_color_texture(pkg, chunk_name)
    if base_img:
        base_img.convert("RGB").save(os.path.join(layers_dir, "basecolor.png"))

    chunk_shadow, chunk_shadow_metadata = extract_chunk_shadow_map(pkg, chunk_name)
    if chunk_shadow is not None:
        chunk_shadow.save(os.path.join(layers_dir, "chunk_shadow.png"))

    wmap_01, wmap_23, tile_coords = _decode_hd_layer_weight_maps(
        pkg, TILE_PIX, FULL_SIZE, SLOT_TO_BIT
    )
    Image.fromarray(wmap_01, "RGB").save(os.path.join(layers_dir, "weightmap_01_binary.png"))
    Image.fromarray(wmap_23, "RGB").save(os.path.join(layers_dir, "weightmap_23_binary.png"))

    runtime_wmap_01 = _smooth_weight_map_tile_local(
        wmap_01, TILE_PIX, TERRAIN_WEIGHT_MASK_SMOOTHING_RADIUS
    )
    runtime_wmap_23 = _smooth_weight_map_tile_local(
        wmap_23, TILE_PIX, TERRAIN_WEIGHT_MASK_SMOOTHING_RADIUS
    )
    Image.fromarray(runtime_wmap_01, "RGB").save(os.path.join(layers_dir, "weightmap_01.png"))
    Image.fromarray(runtime_wmap_23, "RGB").save(os.path.join(layers_dir, "weightmap_23.png"))

    tile_map = {}
    tile_material_indices = np.full((TILES, TILES, 4), 255, dtype=np.uint8)
    used_material_names = set()
    for r in range(TILES):
        for c in range(TILES):
            t_base = (c * 16 + r) * 4
            slots = {}
            slot_indices = {}
            for slot in range(4):
                key = t_base + slot
                if key in tile_layer_data:
                    raw_val = tile_layer_data[key]
                else:
                    raw_val = (
                        0 if slot == 0 else -1
                    )  # Unreal-Library omits 0-valued entries.
                layer_seq_idx = (
                    raw_val + 1 if raw_val >= 0 else -1
                )  # Raw values are 0-based excluding baseColor.

                if layer_seq_idx <= 0:
                    continue

                if layer_seq_idx < len(all_layers):
                    material_name = all_layers[layer_seq_idx][1]
                    if material_name and material_name in material_indices:
                        slots[str(slot)] = material_name
                        material_index = material_indices[material_name]
                        slot_indices[str(slot)] = material_index
                        if material_index < 255:
                            tile_material_indices[r, c, slot] = material_index
                        used_material_names.add(material_name)

            tile_map[f"{r}_{c}"] = {
                "slots": slots,
                "material_indices": slot_indices,
                "has_weight": (r, c) in tile_coords,
            }

    used_materials = [
        {"name": name, "index": material_indices[name]}
        for name in sorted(used_material_names, key=lambda value: value.lower())
    ]
    Image.fromarray(tile_material_indices, "RGBA").save(
        os.path.join(layers_dir, "tile_material_indices.png")
    )

    metadata = {
        "schema": TERRAIN_LAYER_SCHEMA,
        "version": TERRAIN_LAYER_SCHEMA_VERSION,
        "chunk": chunk_name,
        "tiles": TILES,
        "tile_pix": TILE_PIX,
        "full_size": FULL_SIZE,
        "material_library": _relpath_for_manifest(
            material_library["_manifest_path"], output_dir
        ),
        "material_library_name": material_library.get("name", TERRAIN_MATERIAL_LIBRARY_NAME),
        "material_atlases": material_library.get("atlases", {}),
        "material_atlas": material_library.get("atlas", {}),
        "materials_used": used_materials,
        "weight_maps": {
            "slot_0_1": "weightmap_01.png",
            "slot_2_3": "weightmap_23.png",
            "slot_0_1_binary": "weightmap_01_binary.png",
            "slot_2_3_binary": "weightmap_23_binary.png",
        },
        "tile_material_indices": {
            "file": "tile_material_indices.png",
            "format": "rgba8",
            "channels": ["slot0", "slot1", "slot2", "slot3"],
            "unused_value": 255,
        },
        "texture_repeats": NATIVE_TERRAIN_TEXTURE_REPEATS,
        "texture_repeat_source": (
            "VS_TerrainLOD detail UV: (world_xy + 102400) * 0.0048828125; "
            "Godot terrain UV spans 511 intervals of 400 world units"
        ),
        "weight_map_filter": {
            "runtime": "tile_local_gaussian_blur",
            "radius_px": TERRAIN_WEIGHT_MASK_SMOOTHING_RADIUS,
            "binary_files": ["weightmap_01_binary.png", "weightmap_23_binary.png"],
        },
        "layers": {
            name: material_indices[name]
            for name in sorted(material_indices.keys(), key=lambda value: value.lower())
        },
        "tile_map": tile_map,
        "has_basecolor": base_img is not None,
        "chunk_shadow": chunk_shadow_metadata,
        "has_chunk_shadow": chunk_shadow is not None,
    }

    with open(os.path.join(layers_dir, "tile_map.json"), "w") as f:
        json.dump(metadata, f)

    if not silent:
        print(
            f"  Exported terrain layer data: {len(used_materials)} used materials, "
            f"{len(tile_coords)} weight tiles"
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

    quad_visibility = extract_quad_visibility(pkg)

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

            if quad_visibility is not None:
                total_h = TILES_Y * TILE_PIX
                total_w = TILES_X * TILE_PIX
                global_y = r * TILE_PIX + y_idx.flatten()
                global_x = c * TILE_PIX + x_idx.flatten()
                indices_arr = filter_indices_by_quad_visibility(
                    indices_arr,
                    global_x,
                    global_y,
                    quad_visibility,
                    total_w,
                    total_h,
                )

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
        quad_visibility: optional 512x512 bool array (True=visible). When
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

    if quad_visibility is not None:
        indices_arr = filter_indices_by_quad_visibility(
            indices_arr,
            x_idx.flatten(),
            y_idx.flatten(),
            quad_visibility,
            grid_w,
            grid_h,
        )

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


def process_chunk(chunk_name, output_dir, conn=None, silent=False):
    """Process a single chunk by name."""
    vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk_name}.vgr")

    if not os.path.exists(vgr_path):
        return None

    if not silent:
        print(f"  {chunk_name}...", end=" ", flush=True)

    try:
        pkg = UE2Package(vgr_path)

        # Extract heightmap
        heights, grid_size = extract_g16_heightmap(pkg, chunk_name)
        grid_w = grid_h = grid_size

        if heights is None:
            if not silent:
                print("NO HEIGHTMAP")
            return None

        # Extract color texture
        color_image = extract_color_texture(pkg, chunk_name)

        # Determine output path
        output_path = os.path.join(output_dir, f"{chunk_name}_terrain.glb")

        quad_visibility = extract_quad_visibility(pkg)

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
                    # Ensure columns exist (heuristic: try to update, if fail, oh well)
                    cursor.execute(
                        """
                        UPDATE terrain_chunks 
                        SET gltf_exported = 1, export_path = ?
                        WHERE chunk_id = ?
                    """,
                        (output_path, chunk_row[0]),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """
                            INSERT INTO terrain_chunks 
                            (chunk_id, grid_size, gltf_exported, export_path)
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
        hd_layers_path = export_hd_layer_data(pkg, chunk_name, output_dir, silent=silent)
        hd_status = "hd✓" if hd_layers_path else "no hd"
        holes_status = (
            f"holes:{count_rendered_invisible_quads(quad_visibility)}"
            if quad_visibility is not None
            else ""
        )
        
        if not silent:
            parts = [f"{grid_w}x{grid_h}", color_status, grass_status, hd_status]
            if holes_status:
                parts.append(holes_status)
            print(f"OK ({', '.join(parts)})")

        return {
            "chunk_name": chunk_name,
            "grid_w": grid_w,
            "grid_h": grid_h,
            "hd": hd_layers_path is not None,
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


def _resolve_workers(workers):
    if workers < 1:
        return os.cpu_count() or 1
    return workers


def load_heightmap_for_chunk(chunk):
    vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk}.vgr")
    if not os.path.exists(vgr_path):
        return chunk, None, None, "VGR not found"
    try:
        pkg = UE2Package(vgr_path)
        heights, _grid_size = extract_g16_heightmap(pkg, chunk)
        if heights is None:
            return chunk, None, None, "NO HEIGHTMAP"
        return chunk, heights, pkg, None
    except Exception as exc:
        return chunk, None, None, str(exc)


def generate_stitched_terrain_for_chunk(chunk, heights, pkg, output_dir):
    try:
        grid_w = grid_h = heights.shape[0]
        color_image = extract_color_texture(pkg, chunk)
        quad_visibility = extract_quad_visibility(pkg)
        output_path = os.path.join(output_dir, f"{chunk}_terrain.glb")
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        generate_terrain_gltf(
            heights,
            color_image,
            output_path,
            chunk,
            grid_w,
            grid_h,
            quad_visibility=quad_visibility,
        )
        extract_grass_alpha(pkg, chunk)
        return {
            "chunk_name": chunk,
            "grid_w": grid_w,
            "grid_h": grid_h,
            "hd": False,
        }, None
    except Exception as exc:
        return None, str(exc)


def resolve_hd_layer_chunk_for_export(chunk):
    """Resolve a chunk's HD layer metadata without returning texture images."""
    vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk}.vgr")
    if not os.path.exists(vgr_path):
        return {"chunk": chunk, "status": "failed", "error": "VGR not found"}
    try:
        pkg = UE2Package(vgr_path)
        resolved = _resolve_layer_textures(pkg, chunk)
        if not resolved or not resolved[0]:
            return {"chunk": chunk, "status": "skipped", "reason": "no HD layer data"}
        shader_images, num_layers, all_layers, tile_layer_data = resolved
        return {
            "chunk": chunk,
            "status": "resolved",
            "material_names": sorted(shader_images.keys(), key=lambda value: value.lower()),
            "num_layers": num_layers,
            "all_layers": all_layers,
            "tile_layer_data": tile_layer_data,
        }
    except Exception as exc:
        return {"chunk": chunk, "status": "failed", "error": str(exc)}


def export_hd_layer_chunk_from_resolved(chunk, output_dir, material_library, resolved_record):
    vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk}.vgr")
    if not os.path.exists(vgr_path):
        return {"chunk": chunk, "status": "failed", "error": "VGR not found"}
    try:
        material_names = resolved_record.get("material_names", [])
        resolved_layers = (
            {name: True for name in material_names},
            int(resolved_record.get("num_layers", 0)),
            resolved_record.get("all_layers", []),
            resolved_record.get("tile_layer_data", {}),
        )
        pkg = UE2Package(vgr_path)
        layers_dir = export_hd_layer_data(
            pkg,
            chunk,
            output_dir,
            material_library=material_library,
            resolved_layers=resolved_layers,
            silent=True,
        )
        if layers_dir:
            return {"chunk": chunk, "status": "success"}
        return {"chunk": chunk, "status": "skipped", "reason": "no HD layer data"}
    except Exception as exc:
        return {"chunk": chunk, "status": "failed", "error": str(exc)}


def export_hd_layer_bundles(chunks, output_dir, silent=False, packages=None, workers=1):
    """Export Godot terrain layer chunks and their shared material library."""
    successful = []
    skipped = []
    failed = []
    packages = packages or {}
    resolved_by_chunk = {}
    package_by_chunk = {}
    material_names = set()
    workers = min(_resolve_workers(workers), len(chunks)) if chunks else 1
    use_process_workers = workers > 1 and not packages

    if not silent:
        worker_note = f" with {workers} workers" if use_process_workers else ""
        print(f"Exporting terrain layer data for {len(chunks)} chunks{worker_note}...", flush=True)

    if use_process_workers:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(resolve_hd_layer_chunk_for_export, chunk): chunk for chunk in chunks
            }
            for i, future in enumerate(as_completed(futures), start=1):
                chunk = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"chunk": chunk, "status": "failed", "error": str(exc)}
                status = result.get("status", "failed")
                if status == "resolved":
                    resolved_by_chunk[chunk] = result
                    material_names.update(result.get("material_names", []))
                elif status == "skipped":
                    skipped.append(chunk)
                else:
                    failed.append(chunk)
                    if not silent:
                        print(f"\n  {chunk}: ERROR {result.get('error', 'unknown error')}", flush=True)
                print_progress_bar(
                    i,
                    len(chunks),
                    prefix="   Terrain layers scan:",
                    suffix=(
                        f"({i}/{len(chunks)}) ok={len(resolved_by_chunk)} "
                        f"skip={len(skipped)} fail={len(failed)}"
                    ),
                    length=40,
                )
    else:
        for i, chunk in enumerate(chunks):
            vgr_path = os.path.join(VANGUARD_MAPS, f"{chunk}.vgr")
            if chunk in packages:
                pkg = packages[chunk]
            elif os.path.exists(vgr_path):
                try:
                    pkg = UE2Package(vgr_path)
                except Exception as e:
                    failed.append(chunk)
                    if not silent:
                        print(f"  {chunk}: ERROR {e}", flush=True)
                    continue
            else:
                failed.append(chunk)
                if not silent:
                    print(f"  {chunk}: VGR not found", flush=True)
                continue

            try:
                resolved = _resolve_layer_textures(pkg, chunk)
                if not resolved or not resolved[0]:
                    skipped.append(chunk)
                    if not silent:
                        print(f"  {chunk}: no HD layer data", flush=True)
                    continue
                resolved_by_chunk[chunk] = resolved
                package_by_chunk[chunk] = pkg
                material_names.update(resolved[0].keys())
            except Exception as e:
                failed.append(chunk)
                if not silent:
                    print(f"  {chunk}: ERROR {e}", flush=True)

            print_progress_bar(
                i + 1,
                len(chunks),
                prefix="   Terrain layers scan:",
                suffix=(
                    f"({i+1}/{len(chunks)}) ok={len(resolved_by_chunk)} "
                    f"skip={len(skipped)} fail={len(failed)}"
                ),
                length=40,
            )

    material_library = _build_shared_terrain_material_library(output_dir, material_names)
    if material_names and material_library is None:
        return [], skipped, sorted(set(failed + list(resolved_by_chunk.keys())))

    export_chunks = list(resolved_by_chunk.keys())
    if use_process_workers:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    export_hd_layer_chunk_from_resolved,
                    chunk,
                    output_dir,
                    material_library,
                    resolved_by_chunk[chunk],
                ): chunk
                for chunk in export_chunks
            }
            for i, future in enumerate(as_completed(futures), start=1):
                chunk = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"chunk": chunk, "status": "failed", "error": str(exc)}
                status = result.get("status", "failed")
                if status == "success":
                    successful.append(chunk)
                elif status == "skipped":
                    skipped.append(chunk)
                else:
                    failed.append(chunk)
                    if not silent:
                        print(f"\n  {chunk}: ERROR {result.get('error', 'unknown error')}", flush=True)
                print_progress_bar(
                    i,
                    len(export_chunks),
                    prefix="   Terrain layers write:",
                    suffix=f"({i}/{len(export_chunks)}) ok={len(successful)} fail={len(failed)}",
                    length=40,
                )
    else:
        for i, chunk in enumerate(export_chunks):
            try:
                layers_dir = export_hd_layer_data(
                    package_by_chunk[chunk],
                    chunk,
                    output_dir,
                    material_library=material_library,
                    resolved_layers=resolved_by_chunk[chunk],
                    silent=silent,
                )
                if layers_dir:
                    successful.append(chunk)
                    if not silent:
                        print(f"  {chunk}: OK", flush=True)
                else:
                    skipped.append(chunk)
                    if not silent:
                        print(f"  {chunk}: no HD layer data", flush=True)
            except Exception as e:
                failed.append(chunk)
                if not silent:
                    print(f"  {chunk}: ERROR {e}", flush=True)

            if export_chunks:
                print_progress_bar(
                    i + 1,
                    len(export_chunks),
                    prefix="   Terrain layers write:",
                    suffix=f"({i+1}/{len(export_chunks)}) ok={len(successful)} fail={len(failed)}",
                    length=40,
                )

    return successful, skipped, failed


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
        "--tiles",
        action="store_true",
        help="Export HD tiles as 256 individual GLB files for LOD streaming",
    )
    parser.add_argument(
        "--hd-layers",
        action="store_true",
        help="Only export Godot terrain layer data plus the shared terrain material library",
    )
    parser.add_argument(
        "--skip-hd-layers",
        action="store_true",
        help="Skip Godot terrain layer data export during normal terrain extraction",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Chunk workers for all-mode extraction and HD layer export; 0 uses all CPUs.",
    )
    parser.add_argument(
        "--limit-chunks",
        type=int,
        default=0,
        help="Debug/smoke chunk limit with --all.",
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH) if os.path.exists(DB_PATH) else None

    if args.hd_layers:
        chunks = get_all_chunks() if args.all else [args.chunk] if args.chunk else []
        if args.limit_chunks > 0:
            chunks = chunks[: args.limit_chunks]
        if not chunks:
            print("Usage: python extract_all_terrain.py --hd-layers --all")
            print("       python extract_all_terrain.py --hd-layers --chunk chunk_n25_26")
            return
        successful, skipped, failed = export_hd_layer_bundles(
            chunks, OUTPUT_DIR, silent=args.silent, workers=args.workers
        )
        if not args.silent:
            print(
                "Terrain layer data: "
                f"{len(successful)} successful, {len(skipped)} skipped, {len(failed)} failed"
            )
        if failed:
            sys.exit(1)
        return

    # Texture-only mode: just extract PNGs
    if args.texture_only:
        chunks = get_all_chunks() if args.all else [args.chunk] if args.chunk else []
        if args.limit_chunks > 0:
            chunks = chunks[: args.limit_chunks]
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
        if args.limit_chunks > 0:
            chunks = chunks[: args.limit_chunks]
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
        print("=" * 60)
        print(f"Output: {OUTPUT_DIR}")
        print()

    successful = []
    failed = []

    if args.all:
        chunks = get_all_chunks()
        if args.limit_chunks > 0:
            chunks = chunks[: args.limit_chunks]
        if not args.silent:
            print(f"Processing {len(chunks)} chunks...")
            print()

        total_chunks = len(chunks)
        workers = min(_resolve_workers(args.workers), total_chunks) if total_chunks else 1
        if workers > 1 and not args.silent:
            print(f"Workers: {workers}")

        # Two-pass extraction with edge stitching for standard terrain
        # Pass 1: Extract all raw heightmaps
        if not args.silent:
            print("Pass 1: Extracting heightmaps...")
        all_heights = {}
        all_pkgs = {}
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(load_heightmap_for_chunk, chunk): chunk for chunk in chunks}
                for i, future in enumerate(as_completed(futures), start=1):
                    chunk, heights, pkg, error = future.result()
                    if heights is not None and pkg is not None:
                        all_heights[chunk] = heights
                        all_pkgs[chunk] = pkg
                    else:
                        failed.append(chunk)
                        if not args.silent:
                            print(f"  {chunk}: {error}")
                    print_progress_bar(
                        i, total_chunks,
                        prefix="   Heightmaps:",
                        suffix=f"({i}/{total_chunks})",
                        length=40,
                    )
        else:
            for i, chunk in enumerate(chunks):
                chunk, heights, pkg, error = load_heightmap_for_chunk(chunk)
                if heights is not None and pkg is not None:
                    all_heights[chunk] = heights
                    all_pkgs[chunk] = pkg
                else:
                    failed.append(chunk)
                    if not args.silent:
                        print(f"  {chunk}: {error}")
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
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        generate_stitched_terrain_for_chunk,
                        chunk,
                        all_heights[chunk],
                        all_pkgs[chunk],
                        OUTPUT_DIR,
                    ): chunk
                    for chunk in gen_chunks
                }
                for i, future in enumerate(as_completed(futures), start=1):
                    chunk = futures[future]
                    result, error = future.result()
                    if result:
                        successful.append(result)
                    else:
                        failed.append(chunk)
                        if not args.silent:
                            print(f"  {chunk}: ERROR {error}")
                    print_progress_bar(
                        i, len(gen_chunks),
                        prefix="   GLBs:",
                        suffix=f"({i}/{len(gen_chunks)})",
                        length=40,
                    )
        else:
            for i, chunk in enumerate(gen_chunks):
                result, error = generate_stitched_terrain_for_chunk(
                    chunk,
                    all_heights[chunk],
                    all_pkgs[chunk],
                    OUTPUT_DIR,
                )
                if result:
                    successful.append(result)
                else:
                    failed.append(chunk)
                    if not args.silent:
                        print(f"  {chunk}: ERROR {error}")
                print_progress_bar(
                    i + 1, len(gen_chunks),
                    prefix="   GLBs:",
                    suffix=f"({i+1}/{len(gen_chunks)})",
                    length=40,
                )

        if not args.skip_hd_layers:
            hd_successful, hd_skipped, hd_failed = export_hd_layer_bundles(
                gen_chunks, OUTPUT_DIR, silent=args.silent, packages=all_pkgs, workers=args.workers
            )
            hd_successful_set = set(hd_successful)
            for entry in successful:
                if entry["chunk_name"] in hd_successful_set:
                    entry["hd"] = True
            for chunk in hd_failed:
                if chunk not in failed:
                    failed.append(chunk)
            if not args.silent:
                print(
                    "Terrain layer data: "
                    f"{len(hd_successful)} successful, "
                    f"{len(hd_skipped)} skipped, {len(hd_failed)} failed"
                )

    elif args.chunk:
        result = process_chunk(
            args.chunk, OUTPUT_DIR, conn, silent=args.silent
        )
        if result:
            successful.append(result)
        else:
            failed.append(args.chunk)

    else:
        if not args.silent:
            print("Usage:")
            print("  python extract_all_terrain.py --all")
            print("  python extract_all_terrain.py --chunk chunk_n25_26")
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
