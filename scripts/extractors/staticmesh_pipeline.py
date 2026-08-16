#!/usr/bin/env python3
"""
Unified StaticMesh Pipeline for Vanguard: Saga of Heroes

This script follows the PARSING_GUIDELINES.md and provides:
1. 100% byte-accurate parsing using construct library
2. Database storage of all parsed data (canonical: vanguard_files.db)
3. glTF export for rendering in vanguard_viewer.html
4. Proper unknown region tracking

Usage:
    python staticmesh_pipeline.py                    # Parse all files
    python staticmesh_pipeline.py --file Ra44.usx   # Parse specific file
    python staticmesh_pipeline.py --export-only     # Only export glTF, no db update
"""

import os
import sys
import json
import glob
import sqlite3
import struct
import argparse
import math
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from dataclasses import dataclass, asdict
from pathlib import Path

from PIL import Image

from typing import List, Optional, Dict, Any, Tuple


# Add paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(SCRIPT_DIR)
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)

sys.path.insert(0, os.path.join(SCRIPTS_DIR, "lib"))
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, PROJECT_ROOT)

from ue2 import UE2Package
from material_memory import MaterialMemoryResolver
from vanguard_staticmesh import parse_vanguard_staticmesh
from scripts.speedtree.build_spt2fbx_leaf_hybrid_gltf import build_hybrid as build_runtime_leaf_hybrid
from scripts.speedtree.export_reconstructed_spt2fbx_leaf_cards_gltf import build_gltf as build_runtime_leaf_gltf

# Configuration
import config

CANONICAL_DB = config.DB_PATH
MESHES_DIR = os.path.join(config.ASSETS_PATH, "Meshes")
OUTPUT_DIR = config.MESH_BUILDINGS_DIR  # Where glTF files go
RUNTIME_LEAF_COMPARE_DIR = os.path.join(PROJECT_ROOT, "output", "data", "spt2fbx_attachment_compare")
OUTPUT_TEXTURES_DIR = os.path.join(PROJECT_ROOT, "output", "textures")

TREE_KEYWORDS = (
    "speedtree",
    "tree",
    "shrub",
    "bush",
    "_rt",
    "birch",
    "maple",
    "pine",
    "oak",
    "spruce",
    "elm",
    "ash",
    "olive",
    "holly",
    "fir",
    "thorn",
    "myrtle",
    "beech",
    "plane",
)


def is_tree_mesh_name(name: str) -> bool:
    lower_name = (name or "").lower()
    return any(keyword in lower_name for keyword in TREE_KEYWORDS)


def _runtime_leaf_card_json_candidates(mesh_name: str) -> list[str]:
    lower_name = (mesh_name or "").lower()
    name_variants = []
    if lower_name:
        name_variants.append(lower_name)
        if "speedtrees_" in lower_name:
            name_variants.append(lower_name.split("speedtrees_", 1)[1])
        if "speedtree_" in lower_name:
            name_variants.append(lower_name.split("speedtree_", 1)[1])
        if "_" in lower_name:
            name_variants.append(lower_name.rsplit("_", 1)[1])

    ordered_variants = []
    seen = set()
    for variant in name_variants:
        if variant and variant not in seen:
            ordered_variants.append(variant)
            seen.add(variant)

    return [os.path.join(RUNTIME_LEAF_COMPARE_DIR, f"{variant}_leaf_cards.json") for variant in ordered_variants]


def _maybe_export_runtime_leaf_hybrid(gltf_path: str, mesh_name: str) -> str | None:
    sidecar_path = next((path for path in _runtime_leaf_card_json_candidates(mesh_name) if os.path.exists(path)), None)
    if not sidecar_path and not is_tree_mesh_name(mesh_name):
        return None
    if not sidecar_path:
        return None

    with open(sidecar_path, "r", encoding="utf-8") as handle:
        card_payload = json.load(handle)

    sidecar_stem = os.path.basename(sidecar_path).removesuffix("_leaf_cards.json")
    leaf_gltf_path = os.path.join(RUNTIME_LEAF_COMPARE_DIR, f"{sidecar_stem}_leaf_cards.gltf")
    if not os.path.exists(leaf_gltf_path):
        leaf_gltf = build_runtime_leaf_gltf(card_payload)
        with open(leaf_gltf_path, "w", encoding="utf-8") as handle:
            json.dump(leaf_gltf, handle, indent=2)
            handle.write("\n")

    hybrid_output_path = gltf_path[:-5] + "_runtime_leaves_hybrid.gltf" if gltf_path.endswith(".gltf") else gltf_path + "_runtime_leaves_hybrid.gltf"
    hybrid_gltf = build_runtime_leaf_hybrid(Path(gltf_path), Path(leaf_gltf_path))
    with open(hybrid_output_path, "w", encoding="utf-8") as handle:
        json.dump(hybrid_gltf, handle, indent=2)
        handle.write("\n")

    return hybrid_output_path


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


def print_progress(i, total, filename, start_time, stats):
    """Print a rich progress line: bar + current file + ETA."""
    elapsed = time.time() - start_time
    rate = i / elapsed if elapsed > 0 else 0
    eta_s = int((total - i) / rate) if rate > 0 else 0
    eta_str = f"{eta_s // 60}m{eta_s % 60:02d}s" if eta_s >= 60 else f"{eta_s}s"
    pct = int(100 * i / total)
    bar_len = 30
    filled = int(bar_len * i // total)
    bar = "█" * filled + "░" * (bar_len - filled)
    name = filename[:30].ljust(30)
    line = (
        f"\r[{bar}] {pct:3d}% {i:4d}/{total}"
        f"  ok={stats['success']} err={stats['error']}"
        f"  ETA {eta_str}  {name}"
    )
    print(line, end="", flush=True)
    if i == total:
        print()


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class ParsedVertex:
    """Vertex data extracted from LOD model."""

    x: float
    y: float
    z: float
    nx: float = 0.0
    ny: float = 0.0
    nz: float = 1.0
    u: float = 0.0
    v: float = 0.0
    r: float = 1.0
    g: float = 1.0
    b: float = 1.0
    a: float = 1.0


@dataclass
class ParsedMesh:
    """Complete mesh data ready for glTF export and database storage."""

    name: str
    package_path: str
    export_index: int
    # Bounds
    bbox_min: Tuple[float, float, float]
    bbox_max: Tuple[float, float, float]
    bsphere_center: Tuple[float, float, float]
    bsphere_radius: float
    # LOD geometry
    lod_index: int
    vertices: List[ParsedVertex]
    indices: List[int]
    # Parsing metrics
    bytes_total: int
    bytes_parsed: int
    bytes_unknown: int
    coverage_pct: float
    uses_heuristics: bool
    uses_skips: bool
    # Metadata
    internal_version: int
    section_count: int
    parse_status: str
    error_message: Optional[str] = None
    unknown_regions: List[Dict] = None
    sections: List[Dict] = None
    skins: List[List[str]] = None
    uv1s: Optional[List[Tuple[float, float]]] = None
    tangent_us: Optional[List[Tuple[float, float, float]]] = None
    tangent_vs: Optional[List[Tuple[float, float, float]]] = None
    basis_streams: Optional[List[List[Tuple[float, float, float]]]] = None


# =============================================================================
# PARSING
# =============================================================================


def extract_vertices_from_lod(vertices_raw: bytes, count: int) -> List[ParsedVertex]:
    """
    Extract vertex data from raw LOD vertex bytes.
    Each Vanguard LOD vertex is 56 bytes:
    - Position: 3 floats (12 bytes)
    - Normal: 3 floats (12 bytes)
    - TangentU: 3 floats (12 bytes)
    - TangentV: 3 floats (12 bytes)
    - U, V: 2 floats (8 bytes)
    """
    vertices = []
    VERTEX_SIZE = 56

    for i in range(count):
        offset = i * VERTEX_SIZE
        if offset + VERTEX_SIZE > len(vertices_raw):
            break

        x, y, z = struct.unpack_from("<fff", vertices_raw, offset)
        nx, ny, nz = struct.unpack_from("<fff", vertices_raw, offset + 12)
        # Skip TangentU (12 bytes) and TangentV (12 bytes)
        u, v = struct.unpack_from("<ff", vertices_raw, offset + 48)

        vertices.append(ParsedVertex(x=x, y=y, z=z, nx=nx, ny=ny, nz=nz, u=u, v=v))

    return vertices


def parse_staticmesh_file(pkg_path: str) -> List[ParsedMesh]:
    """
    Parse all StaticMesh exports from a package file.
    Uses exact port of UEViewer's SerializeVanguardMesh.
    Returns list of ParsedMesh objects.
    """
    meshes = []

    try:
        pkg = UE2Package(pkg_path)
    except Exception as e:
        print(f"  Error loading package: {e}")
        return meshes

    static_mesh_exports = [e for e in pkg.exports if e["class_name"] == "StaticMesh"]

    for exp in static_mesh_exports:
        try:
            data = pkg.get_export_data(exp)
            serial_offset = exp["serial_offset"]

            mesh_data = parse_vanguard_staticmesh(
                data, pkg.names, serial_offset, imports=pkg.imports
            )

            if mesh_data is None:
                meshes.append(
                    ParsedMesh(
                        name=exp["object_name"],
                        package_path=pkg_path,
                        export_index=exp["index"],
                        bbox_min=(0, 0, 0),
                        bbox_max=(0, 0, 0),
                        bsphere_center=(0, 0, 0),
                        bsphere_radius=0,
                        lod_index=0,
                        vertices=[],
                        indices=[],
                        bytes_total=len(data),
                        bytes_parsed=0,
                        bytes_unknown=len(data),
                        coverage_pct=0.0,
                        uses_heuristics=False,
                        uses_skips=False,
                        internal_version=0,
                        section_count=0,
                        parse_status="error",
                        error_message="parse_vanguard_staticmesh returned None",
                    )
                )
                continue

            # Convert to ParsedVertex list
            vertices = []
            for i in range(len(mesh_data.vertices)):
                vx, vy, vz = mesh_data.vertices[i]
                nx, ny, nz = (
                    mesh_data.normals[i] if i < len(mesh_data.normals) else (0, 0, 1)
                )
                u, v = mesh_data.uvs[i] if i < len(mesh_data.uvs) else (0, 0)
                cr, cg, cb, ca = (
                    mesh_data.colors[i]
                    if i < len(mesh_data.colors)
                    else (1.0, 1.0, 1.0, 1.0)
                )
                vertices.append(
                    ParsedVertex(x=vx, y=vy, z=vz, nx=nx, ny=ny, nz=nz, u=u, v=v, r=cr, g=cg, b=cb, a=ca)
                )

            # Use raw index buffer for correct section-based slicing via first_index
            indices = mesh_data.raw_indices if mesh_data.raw_indices else []
            if not indices:
                # Fallback: flatten faces (but section first_index won't work correctly)
                for f in mesh_data.faces:
                    indices.extend(f)

            meshes.append(
                ParsedMesh(
                    name=exp["object_name"],
                    package_path=pkg_path,
                    export_index=exp["index"],
                    bbox_min=mesh_data.bbox_min,
                    bbox_max=mesh_data.bbox_max,
                    bsphere_center=(0, 0, 0),
                    bsphere_radius=0,
                    lod_index=0,
                    vertices=vertices,
                    indices=indices,
                    bytes_total=len(data),
                    bytes_parsed=len(data),
                    bytes_unknown=0,
                    coverage_pct=100.0,
                    uses_heuristics=False,
                    uses_skips=False,
                    internal_version=mesh_data.internal_version,
                    section_count=len(mesh_data.sections),
                    parse_status="complete",
                    sections=mesh_data.sections,
                    skins=mesh_data.skins,
                    uv1s=mesh_data.uv_streams[1] if len(mesh_data.uv_streams) > 1 else None,
                    tangent_us=mesh_data.tangent_us,
                    tangent_vs=mesh_data.tangent_vs,
                    basis_streams=mesh_data.basis_streams,
                )
            )

        except Exception as e:
            meshes.append(
                ParsedMesh(
                    name=exp["object_name"],
                    package_path=pkg_path,
                    export_index=exp["index"],
                    bbox_min=(0, 0, 0),
                    bbox_max=(0, 0, 0),
                    bsphere_center=(0, 0, 0),
                    bsphere_radius=0,
                    lod_index=0,
                    vertices=[],
                    indices=[],
                    bytes_total=0,
                    bytes_parsed=0,
                    bytes_unknown=0,
                    coverage_pct=0.0,
                    uses_heuristics=False,
                    uses_skips=False,
                    internal_version=0,
                    section_count=0,
                    parse_status="error",
                    error_message=str(e),
                )
            )

    return meshes


# =============================================================================
# DATABASE
# =============================================================================


def get_or_create_file_id(conn: sqlite3.Connection, pkg_path: str) -> int:
    """Get file_id from files table, or create if not exists."""
    cursor = conn.cursor()

    # Ensure we use relative paths for the database
    rel_path = pkg_path
    if os.path.isabs(pkg_path):
        import config

        try:
            rel_path = os.path.relpath(pkg_path, config.ASSETS_PATH)
        except ValueError:
            pass

    # Check if exists
    cursor.execute("SELECT id FROM files WHERE file_path = ?", (rel_path,))
    row = cursor.fetchone()
    if row:
        return row[0]

    # Create new entry
    filename = os.path.basename(pkg_path)
    parent = os.path.dirname(rel_path)
    # Use absolute path for os.path.getsize
    abs_path = (
        pkg_path
        if os.path.isabs(pkg_path)
        else os.path.join(config.ASSETS_PATH, pkg_path)
    )
    size = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0

    # Determine file type
    ext = os.path.splitext(filename)[1].lower().lstrip(".")

    cursor.execute(
        """
        INSERT INTO files (file_path, file_name, size_bytes, location, extension, category)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (rel_path, filename, size, parent, ext, "Mesh"),
    )

    conn.commit()
    return cursor.lastrowid


def create_parse_session(conn: sqlite3.Connection) -> int:
    """Create a new parse session and return its ID."""
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO parse_sessions (started_at, parser_version)
        VALUES (?, ?)
    """,
        (datetime.now().isoformat(), "staticmesh_construct_v1"),
    )
    conn.commit()
    return cursor.lastrowid


def store_parsed_mesh(
    conn: sqlite3.Connection, mesh: ParsedMesh, file_id: int, session_id: int
):
    """Store parsed mesh data in the database."""
    cursor = conn.cursor()

    # Map parse_status to allowed values
    status_map = {
        "success": "complete",
        "complete": "complete",
        "error": "error",
        "skipped_variant_format": "error",
        "skipped_populated_stream": "error",
    }
    db_status = status_map.get(mesh.parse_status, "error")

    # Insert or update parsed_exports
    cursor.execute(
        """
        INSERT INTO parsed_exports (
            file_id, export_index, export_name, class_name,
            serial_offset, serial_size, bytes_parsed, bytes_unknown,
            parse_status, uses_heuristics, uses_skips,
            session_id, last_parsed_at, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id, export_index) DO UPDATE SET
            bytes_parsed = excluded.bytes_parsed,
            bytes_unknown = excluded.bytes_unknown,
            parse_status = excluded.parse_status,
            uses_heuristics = excluded.uses_heuristics,
            uses_skips = excluded.uses_skips,
            session_id = excluded.session_id,
            last_parsed_at = excluded.last_parsed_at,
            error_message = excluded.error_message
    """,
        (
            file_id,
            mesh.export_index,
            mesh.name,
            "StaticMesh",
            0,  # serial_offset - we'd need to track this
            mesh.bytes_total,
            mesh.bytes_parsed,
            mesh.bytes_unknown,
            db_status,
            1 if mesh.uses_heuristics else 0,
            1 if mesh.uses_skips else 0,
            session_id,
            datetime.now().isoformat(),
            mesh.error_message,
        ),
    )

    parsed_export_id = cursor.lastrowid

    # Store key fields
    fields_to_store = [
        ("bounds.bbox_min.x", "float", mesh.bbox_min[0]),
        ("bounds.bbox_min.y", "float", mesh.bbox_min[1]),
        ("bounds.bbox_min.z", "float", mesh.bbox_min[2]),
        ("bounds.bbox_max.x", "float", mesh.bbox_max[0]),
        ("bounds.bbox_max.y", "float", mesh.bbox_max[1]),
        ("bounds.bbox_max.z", "float", mesh.bbox_max[2]),
        ("internal_version", "int32", mesh.internal_version),
        ("section_count", "int32", mesh.section_count),
        ("lod_index", "int32", mesh.lod_index),
        ("vertex_count", "int32", len(mesh.vertices)),
        ("index_count", "int32", len(mesh.indices)),
        ("triangle_count", "int32", len(mesh.indices) // 3),
    ]

    for field_path, field_type, value in fields_to_store:
        cursor.execute(
            """
            INSERT INTO parsed_fields (
                parsed_export_id, field_path, field_type,
                value_int, value_float, is_unknown
            ) VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(parsed_export_id, field_path, array_index) DO UPDATE SET
                value_int = excluded.value_int,
                value_float = excluded.value_float
        """,
            (
                parsed_export_id,
                field_path,
                field_type,
                int(value) if field_type == "int32" else None,
                float(value) if field_type == "float" else None,
            ),
        )

    # Store unknown regions
    if mesh.unknown_regions:
        for region in mesh.unknown_regions:
            # Get raw hex from the region if available
            raw_hex = region.get(
                "raw_hex", "00"
            )  # Default to single byte if not available
            cursor.execute(
                """
                INSERT INTO unknown_regions (
                    parsed_export_id, offset_start, offset_end, raw_hex, context
                ) VALUES (?, ?, ?, ?, ?)
            """,
                (
                    parsed_export_id,
                    region.get("offset", 0),
                    region.get("offset", 0) + region.get("size", 0),
                    raw_hex,
                    region.get("name", "unknown"),
                ),
            )

    conn.commit()


# =============================================================================
# GLTF EXPORT
# =============================================================================


def _load_shader_texture_map():
    """Load the legacy shader→texture projection for fallback-only lookups."""
    if not hasattr(_load_shader_texture_map, "_cache"):
        map_path = os.path.join(
            PROJECT_ROOT, "output", "data", "shader_to_texture.json"
        )
        if os.path.exists(map_path):
            with open(map_path) as f:
                _load_shader_texture_map._cache = json.load(f)
        else:
            _load_shader_texture_map._cache = {}
    return _load_shader_texture_map._cache


def _load_material_memory_resolver():
    """Load MaterialMemory resolver once, if the client cache is present."""
    if not hasattr(_load_material_memory_resolver, "_cache"):
        try:
            resolver = MaterialMemoryResolver()
        except Exception as exc:
            print(f"WARNING: MaterialMemory resolver unavailable: {exc}", file=sys.stderr)
            resolver = None
        _load_material_memory_resolver._cache = (
            resolver if resolver is not None and resolver.available else None
        )
    return _load_material_memory_resolver._cache


def _shader_map_entry(shader_map, shader_ref):
    """Find legacy shader_to_texture entries from a full or bare shader ref."""
    if not shader_ref:
        return None
    keys = [str(shader_ref).lower()]
    if "." in keys[0]:
        keys.append(keys[0].rsplit(".", 1)[-1])
    for key in keys:
        if key in shader_map:
            return shader_map[key]
    return None


def _load_texture_image_b64(texture_name):
    """Load a texture PNG as base64 data URI. Returns (data_uri, mime) or (None, None)."""
    import base64

    tex_dir = OUTPUT_TEXTURES_DIR
    # Try exact name, then case-insensitive
    for fname in os.listdir(tex_dir) if os.path.isdir(tex_dir) else []:
        if fname.lower() == texture_name.lower() + ".png":
            fpath = os.path.join(tex_dir, fname)
            try:
                with Image.open(fpath) as image:
                    if image.format != "PNG":
                        raise ValueError(f"not PNG data: {fpath}")
                    image.verify()
            except (OSError, ValueError) as exc:
                raise ValueError(f"refusing to embed corrupt texture {fpath}: {exc}") from exc
            with open(fpath, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            return f"data:image/png;base64,{b64}", "image/png"
    return None, None


def _compute_leaf_billboard_data(mesh: "ParsedMesh", eligible_vertices=None):
    """
    Compute billboard corner offsets for collapsed SpeedTree leaf card quads.

    SpeedTree stores leaf cards as 4 vertices at the same center position
    with different UVs (quad corners). The game engine expands them at runtime
    via a vertex shader into camera-facing billboards.

    We detect collapsed quads, compute the corner offset each vertex needs
    (right/up in screen space × half_size), and store it in mesh._billboard_offsets.
    The vertex positions remain at the center. The viewer's vertex shader will
    expand them toward the camera.

    mesh._billboard_offsets: list of (sx, sy) per vertex, where:
        sx = -1 or +1 (right direction)
        sy = -1 or +1 (up direction)
        Multiplied by the card's half_size at render time.
    mesh._billboard_sizes: list of float per vertex (half_size for this card, 0 if not a billboard)
    """
    import math

    if not mesh.indices or not mesh.vertices:
        return

    eligible_vertices = set(eligible_vertices or [])

    # Initialize per-vertex billboard data (0 = not a billboard vertex)
    n = len(mesh.vertices)
    bb_sx = [0.0] * n  # corner x: -1 or +1
    bb_sy = [0.0] * n  # corner y: -1 or +1
    bb_size = [0.0] * n  # half_size (0 = non-billboard)

    # Pass 1: detect all collapsed quads and collect centers + vertex lists
    collapsed_quads = []  # list of (center_pos, quad_verts, uv_data)
    
    # Group vertices by their rounded spatial position
    from collections import defaultdict
    pos_map = defaultdict(list)
    for vi, v in enumerate(mesh.vertices):
        if eligible_vertices and vi not in eligible_vertices:
            continue
        # Round to 2 decimal places to handle floating point inaccuracies
        k = (round(v.x, 2), round(v.y, 2), round(v.z, 2))
        pos_map[k].append(vi)
        
    for center_k, vert_indices in pos_map.items():
        # A collapsed leaf quad has exactly 4 vertices at the same spot.
        # (Sometimes 8 if double-sided or multiple leaves at one spot, but we handle in chunks of 4)
        if len(vert_indices) >= 4:
            # We process them in chunks of 4
            for chunk_start in range(0, len(vert_indices) - 3, 4):
                quad_verts = vert_indices[chunk_start:chunk_start+4]
                uvs = [(mesh.vertices[vi].u, mesh.vertices[vi].v) for vi in quad_verts]
                collapsed_quads.append((center_k, quad_verts, uvs))

    if not collapsed_quads:
        mesh._billboard_sx = bb_sx
        mesh._billboard_sy = bb_sy
        mesh._billboard_size = bb_size
        return

    # Pass 2: compute half_size from average nearest-neighbor distance
    # Card diameter should match avg spacing for seamless coverage
    centers = [q[0] for q in collapsed_quads]
    if len(centers) >= 2:
        nn_dists = []
        for i, c1 in enumerate(centers):
            min_d = float('inf')
            for j, c2 in enumerate(centers):
                if i == j:
                    continue
                d = math.sqrt((c1[0]-c2[0])**2 + (c1[1]-c2[1])**2 + (c1[2]-c2[2])**2)
                if d < min_d:
                    min_d = d
            nn_dists.append(min_d)
        avg_nn = sum(nn_dists) / len(nn_dists)
        half_size = avg_nn  # card diameter = 2× avg spacing for heavy overlap
    else:
        # Fallback for single card: use 5% of mesh height
        all_z = [v.z for v in mesh.vertices]
        tree_height = max(all_z) - min(all_z) if all_z else 0
        half_size = tree_height * 0.05

    # Pass 3: assign corner offsets and sizes
    for center, quad_verts, uvs in collapsed_quads:
        u_min = min(uv[0] for uv in uvs)
        u_max = max(uv[0] for uv in uvs)
        v_min = min(uv[1] for uv in uvs)
        v_max = max(uv[1] for uv in uvs)
        u_mid = (u_min + u_max) / 2
        v_mid = (v_min + v_max) / 2

        for vi in quad_verts:
            v = mesh.vertices[vi]
            bb_sx[vi] = 1.0 if v.u > u_mid else -1.0
            bb_sy[vi] = -1.0 if v.v > v_mid else 1.0  # V flipped (UV v increases downward)
            bb_size[vi] = half_size

    # Store on mesh for the glTF exporter to pick up
    mesh._billboard_sx = bb_sx
    mesh._billboard_sy = bb_sy
    mesh._billboard_size = bb_size


def _gltf_basis_vector(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = vector
    return (
        -(y if math.isfinite(y) else 0.0),
        z if math.isfinite(z) else 0.0,
        x if math.isfinite(x) else 0.0,
    )


def _tangent_handedness(
    source_normal: tuple[float, float, float],
    source_tangent: tuple[float, float, float],
    source_bitangent: tuple[float, float, float],
) -> float:
    """Return glTF tangent.w from the two authored tangent directions."""
    normal = _gltf_basis_vector(source_normal)
    tangent = _gltf_basis_vector(source_tangent)
    bitangent = _gltf_basis_vector(source_bitangent)
    cross_nt = (
        normal[1] * tangent[2] - normal[2] * tangent[1],
        normal[2] * tangent[0] - normal[0] * tangent[2],
        normal[0] * tangent[1] - normal[1] * tangent[0],
    )
    return -1.0 if sum(a * b for a, b in zip(cross_nt, bitangent)) < 0.0 else 1.0


def mesh_to_gltf(mesh: ParsedMesh, output_path: str) -> bool:
    """
    Export ParsedMesh to glTF format with positions, normals, UVs, and textures.

    If sections and skins are available and textures have been extracted,
    creates multi-material primitives with embedded texture images.
    Falls back to single untextured primitive otherwise.

    Returns True on success.
    """
    if not mesh.vertices or not mesh.indices:
        return False

    import base64

    has_uvs = any(v.u != 0.0 or v.v != 0.0 for v in mesh.vertices)
    has_uv1s = bool(mesh.uv1s) and len(mesh.uv1s) == len(mesh.vertices)
    has_tangent_us = bool(mesh.tangent_us) and len(mesh.tangent_us) == len(mesh.vertices)
    has_tangent_vs = bool(mesh.tangent_vs) and len(mesh.tangent_vs) == len(mesh.vertices)
    basis_streams = [
        stream
        for stream in (mesh.basis_streams or [])
        if stream and len(stream) == len(mesh.vertices)
    ]

    # --- Resolve materials for each section ---
    shader_map = _load_shader_texture_map()
    material_resolver = _load_material_memory_resolver()
    section_materials = []
    # list of (
    #   shader_ref, texture_name, base_color_factor, alpha_mode, is_water, two_sided,
    #   normal_texture_name, normal_scale, material_extras,
    #   specular_texture_name, specular_factor, specular_color_factor,
    #   detail_texture_name, detail_scale
    # ) per section
    has_any_texture = False

    if mesh.sections and mesh.skins and len(mesh.skins) > 0:
        skin0 = mesh.skins[0]  # Use first skin set
        for si, sec in enumerate(mesh.sections):
            if sec.get("num_faces", 0) == 0:
                section_materials.append(
                    (
                        None,
                        None,
                        None,
                        None,
                        False,
                        False,
                        None,
                        None,
                        {},
                        None,
                        None,
                        None,
                        None,
                        None,
                    )
                )
                continue
            shader_ref = skin0[si] if si < len(skin0) and skin0[si] else None
            map_entry = _shader_map_entry(shader_map, shader_ref)
            # Handle both string and dict entries in shader map
            if isinstance(map_entry, dict):
                texture_name = map_entry.get("texture")
                alpha_mode = map_entry.get("alpha")  # "mask" or None
                is_water = map_entry.get("is_water", False)
                two_sided = map_entry.get("two_sided", False)
                # Water shader: color stored as "r,g,b" in "color" key (no "color:" prefix)
                if texture_name is None and "color" in map_entry:
                    texture_name = "color:" + map_entry["color"]
            else:
                texture_name = map_entry  # plain string or None
                alpha_mode = None
                is_water = False
                two_sided = False

            base_color_factor = None
            normal_texture_name = None
            normal_scale = None
            specular_texture_name = None
            specular_factor = None
            specular_color_factor = None
            detail_texture_name = None
            detail_scale = None
            material_extras = {}
            if material_resolver is not None and shader_ref:
                shader_info = material_resolver.resolve_shader(shader_ref)
                if shader_info is not None:
                    diffuse_asset = material_resolver.ensure_diffuse_asset(
                        shader_ref, OUTPUT_TEXTURES_DIR
                    )
                    if diffuse_asset and diffuse_asset.get("asset_name"):
                        texture_name = diffuse_asset["asset_name"]
                    base_color_factor = material_resolver.base_color_factor(shader_ref)
                    if alpha_mode is None:
                        alpha_mode = shader_info.alpha_mode
                    two_sided = two_sided or shader_info.two_sided
                    material_extras = material_resolver.shader_extras(shader_ref)
                    runtime_graph = material_resolver.build_runtime_material_graph(
                        shader_ref, OUTPUT_TEXTURES_DIR
                    )
                    if runtime_graph is not None:
                        material_extras["vg_runtime_material_graph"] = runtime_graph
                    if diffuse_asset:
                        material_extras["vg_base_color_texture"] = diffuse_asset
                    if base_color_factor:
                        material_extras["vg_base_color_factor"] = base_color_factor
                    normal_asset, normal_scale = material_resolver.ensure_normal_asset(
                        shader_ref, OUTPUT_TEXTURES_DIR
                    )
                    if normal_asset and normal_asset.get("asset_name"):
                        normal_texture_name = normal_asset["asset_name"]
                        material_extras["vg_normal_texture"] = normal_asset
                    specular_asset, specular_factor, specular_color_factor = (
                        material_resolver.ensure_specular_asset(
                            shader_ref, OUTPUT_TEXTURES_DIR
                        )
                    )
                    if specular_asset and specular_asset.get("asset_name"):
                        specular_texture_name = specular_asset["asset_name"]
                        material_extras["vg_specular_texture_asset"] = specular_asset
                    detail_asset = material_resolver.ensure_detail_asset(
                        shader_ref, OUTPUT_TEXTURES_DIR
                    )
                    if detail_asset and detail_asset.get("asset_name"):
                        detail_texture_name = detail_asset["asset_name"]
                        material_extras["vg_detail_texture_asset"] = detail_asset
                    detail_scale = shader_info.detail_scale

            section_materials.append(
                (
                    shader_ref,
                    texture_name,
                    base_color_factor,
                    alpha_mode,
                    is_water,
                    two_sided,
                    normal_texture_name,
                    normal_scale,
                    material_extras,
                    specular_texture_name,
                    specular_factor,
                    specular_color_factor,
                    detail_texture_name,
                    detail_scale,
                )
            )
            if (
                texture_name
                or normal_texture_name
                or specular_texture_name
                or detail_texture_name
            ):
                has_any_texture = True

    # --- Expand collapsed SpeedTree leaf card quads ---
    # SpeedTree stores leaf cards as 4 vertices at the same center position
    # with different UVs (quad corners). We expand them into real quads.
    # Only run on SpeedTree / tree meshes — on buildings it corrupts geometry
    # where thin faces happen to share vertex positions.
    path_lower = output_path.lower()
    is_tree_mesh = is_tree_mesh_name(path_lower)
    if is_tree_mesh and mesh.sections and mesh.indices:
        billboard_vertices = set()
        non_billboard_vertices = set()
        for si, sec in enumerate(mesh.sections):
            nf = sec.get("num_faces", 0)
            if nf <= 0:
                continue

            first_index = sec.get("first_index", 0)
            if first_index + nf * 3 <= len(mesh.indices):
                slice_as_faces = mesh.indices[first_index : first_index + nf * 3]
                slice_as_idx = mesh.indices[first_index : first_index + nf]
                use_faces = (len(slice_as_faces) % 3 == 0 and len(slice_as_faces) >= len(slice_as_idx))
                num_indices = nf * 3 if use_faces else nf
            else:
                num_indices = nf

            if first_index + num_indices > len(mesh.indices):
                num_indices = len(mesh.indices) - first_index
            if num_indices <= 0:
                continue

            sec_indices = mesh.indices[first_index : first_index + num_indices]
            sec_vertices = {idx for idx in sec_indices if 0 <= idx < len(mesh.vertices)}
            if not sec_vertices:
                continue

            sm = (
                section_materials[si]
                if si < len(section_materials)
                else (
                    None,
                    None,
                    None,
                    None,
                    False,
                    False,
                    None,
                    None,
                    {},
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
            alpha_mode = sm[3] if len(sm) > 3 else None
            if alpha_mode == "mask":
                billboard_vertices.update(sec_vertices)
            else:
                non_billboard_vertices.update(sec_vertices)

        eligible_billboard_vertices = billboard_vertices - non_billboard_vertices
        _compute_leaf_billboard_data(mesh, eligible_billboard_vertices)

    # Check if billboard data was computed
    has_billboards = hasattr(mesh, '_billboard_size') and any(s > 0 for s in mesh._billboard_size)

    # Build attribute accessor indices
    attr_pos_idx = 0
    attr_norm_idx = 1
    attr_uv_idx = 2 if has_uvs else None

    attributes = {"POSITION": attr_pos_idx, "NORMAL": attr_norm_idx}
    if has_uvs:
        attributes["TEXCOORD_0"] = attr_uv_idx

    buffer_data = bytearray()
    buffer_views = []
    accessors = []

    # --- Positions ---
    pos_start = len(buffer_data)
    min_pos = [float("inf")] * 3
    max_pos = [float("-inf")] * 3

    for v in mesh.vertices:
        vx = v.x if math.isfinite(v.x) else 0.0
        vy = v.y if math.isfinite(v.y) else 0.0
        vz = v.z if math.isfinite(v.z) else 0.0

        # Apply Vanguard -> glTF coordinate swizzle at export time
        # glTF_X = -Vang_Y (negated for left->right handedness)
        # glTF_Y = Vang_Z (up), glTF_Z = Vang_X
        gx = -vy  # Vanguard Y (north) -> glTF X (negated)
        gy = vz  # Vanguard Z (up)    -> glTF Y
        gz = vx  # Vanguard X (east)  -> glTF Z

        buffer_data.extend(struct.pack("<fff", gx, gy, gz))
        min_pos[0] = min(min_pos[0], gx)
        min_pos[1] = min(min_pos[1], gy)
        min_pos[2] = min(min_pos[2], gz)
        max_pos[0] = max(max_pos[0], gx)
        max_pos[1] = max(max_pos[1], gy)
        max_pos[2] = max(max_pos[2], gz)

    pos_end = len(buffer_data)
    buffer_views.append(
        {
            "buffer": 0,
            "byteOffset": pos_start,
            "byteLength": pos_end - pos_start,
            "target": 34962,
        }
    )
    accessors.append(
        {
            "bufferView": 0,
            "componentType": 5126,  # FLOAT
            "count": len(mesh.vertices),
            "type": "VEC3",
            "min": min_pos,
            "max": max_pos,
        }
    )

    # --- Normals ---
    norm_start = len(buffer_data)
    for v in mesh.vertices:
        nx = v.nx if math.isfinite(v.nx) else 0.0
        ny = v.ny if math.isfinite(v.ny) else 0.0
        nz = v.nz if math.isfinite(v.nz) else 1.0

        # Same coordinate swizzle as positions (X negated)
        gx = -ny
        gy = nz
        gz = nx

        buffer_data.extend(struct.pack("<fff", gx, gy, gz))

    norm_end = len(buffer_data)
    buffer_views.append(
        {
            "buffer": 0,
            "byteOffset": norm_start,
            "byteLength": norm_end - norm_start,
            "target": 34962,
        }
    )
    accessors.append(
        {
            "bufferView": 1,
            "componentType": 5126,
            "count": len(mesh.vertices),
            "type": "VEC3",
        }
    )

    # --- UVs ---
    if has_uvs:
        uv_start = len(buffer_data)
        for v in mesh.vertices:
            u = v.u if math.isfinite(v.u) else 0.0
            vv = v.v if math.isfinite(v.v) else 0.0
            buffer_data.extend(struct.pack("<ff", u, vv))

        uv_end = len(buffer_data)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": uv_start,
                "byteLength": uv_end - uv_start,
                "target": 34962,
            }
        )
        accessors.append(
            {
                "bufferView": 2,
                "componentType": 5126,
                "count": len(mesh.vertices),
                "type": "VEC2",
            }
        )

    if has_uv1s:
        uv1_start = len(buffer_data)
        for u1, v1 in mesh.uv1s:
            buffer_data.extend(
                struct.pack(
                    "<ff",
                    u1 if math.isfinite(u1) else 0.0,
                    v1 if math.isfinite(v1) else 0.0,
                )
            )

        uv1_end = len(buffer_data)
        uv1_bv_idx = len(buffer_views)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": uv1_start,
                "byteLength": uv1_end - uv1_start,
                "target": 34962,
            }
        )
        uv1_acc_idx = len(accessors)
        accessors.append(
            {
                "bufferView": uv1_bv_idx,
                "componentType": 5126,
                "count": len(mesh.vertices),
                "type": "VEC2",
            }
        )
        attributes["TEXCOORD_1"] = uv1_acc_idx

    if has_tangent_us:
        tangent_start = len(buffer_data)
        for tangent_index, (tx, ty, tz) in enumerate(mesh.tangent_us):
            gx = -(ty if math.isfinite(ty) else 0.0)
            gy = tz if math.isfinite(tz) else 0.0
            gz = tx if math.isfinite(tx) else 0.0
            handedness = 1.0
            if has_tangent_vs:
                vertex = mesh.vertices[tangent_index]
                handedness = _tangent_handedness(
                    (vertex.nx, vertex.ny, vertex.nz),
                    (tx, ty, tz),
                    mesh.tangent_vs[tangent_index],
                )
            buffer_data.extend(struct.pack("<ffff", gx, gy, gz, handedness))

        tangent_end = len(buffer_data)
        tangent_bv_idx = len(buffer_views)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": tangent_start,
                "byteLength": tangent_end - tangent_start,
                "target": 34962,
            }
        )
        tangent_acc_idx = len(accessors)
        accessors.append(
            {
                "bufferView": tangent_bv_idx,
                "componentType": 5126,
                "count": len(mesh.vertices),
                "type": "VEC4",
            }
        )
        attributes["TANGENT"] = tangent_acc_idx

    if has_tangent_vs:
        tangent_v_start = len(buffer_data)
        for tx, ty, tz in mesh.tangent_vs:
            gx = -(ty if math.isfinite(ty) else 0.0)
            gy = tz if math.isfinite(tz) else 0.0
            gz = tx if math.isfinite(tx) else 0.0
            buffer_data.extend(struct.pack("<fff", gx, gy, gz))

        tangent_v_end = len(buffer_data)
        tangent_v_bv_idx = len(buffer_views)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": tangent_v_start,
                "byteLength": tangent_v_end - tangent_v_start,
                "target": 34962,
            }
        )
        tangent_v_acc_idx = len(accessors)
        accessors.append(
            {
                "bufferView": tangent_v_bv_idx,
                "componentType": 5126,
                "count": len(mesh.vertices),
                "type": "VEC3",
            }
        )
        attributes["_TANGENT_V"] = tangent_v_acc_idx

    for basis_idx, basis_stream in enumerate(basis_streams):
        basis_start = len(buffer_data)
        for bx, by, bz in basis_stream:
            gx = -(by if math.isfinite(by) else 0.0)
            gy = bz if math.isfinite(bz) else 0.0
            gz = bx if math.isfinite(bx) else 0.0
            buffer_data.extend(struct.pack("<fff", gx, gy, gz))

        basis_end = len(buffer_data)
        basis_bv_idx = len(buffer_views)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": basis_start,
                "byteLength": basis_end - basis_start,
                "target": 34962,
            }
        )
        basis_acc_idx = len(accessors)
        accessors.append(
            {
                "bufferView": basis_bv_idx,
                "componentType": 5126,
                "count": len(mesh.vertices),
                "type": "VEC3",
            }
        )
        attributes[f"_BASIS{basis_idx}"] = basis_acc_idx

    # --- Billboard attribute for SpeedTree leaf cards ---
    attr_bb_idx = None
    if has_billboards:
        bb_start = len(buffer_data)
        for i in range(len(mesh.vertices)):
            sx = mesh._billboard_sx[i]
            sy = mesh._billboard_sy[i]
            sz = mesh._billboard_size[i]
            buffer_data.extend(struct.pack("<fff", sx, sy, sz))
        bb_end = len(buffer_data)

        bb_bv_idx = len(buffer_views)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": bb_start,
                "byteLength": bb_end - bb_start,
                "target": 34962,
            }
        )
        attr_bb_idx = len(accessors)
        accessors.append(
            {
                "bufferView": bb_bv_idx,
                "componentType": 5126,
                "count": len(mesh.vertices),
                "type": "VEC3",
            }
        )

    # --- Vertex colors (COLOR_0) for SpeedTree shadow/AO tinting ---
    has_colors = any(
        v.r != 1.0 or v.g != 1.0 or v.b != 1.0 or v.a != 1.0
        for v in mesh.vertices
    )
    if has_colors:
        color_start = len(buffer_data)
        for v in mesh.vertices:
            buffer_data.extend(
                struct.pack("<ffff", v.r, v.g, v.b, v.a)
            )
        color_end = len(buffer_data)

        color_bv_idx = len(buffer_views)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": color_start,
                "byteLength": color_end - color_start,
                "target": 34962,
            }
        )
        color_acc_idx = len(accessors)
        accessors.append(
            {
                "bufferView": color_bv_idx,
                "componentType": 5126,
                "count": len(mesh.vertices),
                "type": "VEC4",
            }
        )
        attributes["COLOR_0"] = color_acc_idx

    # Number of accessors so far (for vertex attributes)
    next_accessor = len(accessors)

    # --- Build materials and texture images ---
    gltf_materials = []
    gltf_textures = []
    gltf_images = []
    gltf_samplers = []
    material_cache = {}  # material definition key -> material_index
    texture_index_cache = {}  # texture_name -> glTF texture index
    extensions_used = set()

    def get_or_create_texture(texture_name):
        if not texture_name:
            return None
        key = str(texture_name).lower()
        if key in texture_index_cache:
            return texture_index_cache[key]

        data_uri, mime = _load_texture_image_b64(texture_name)
        if not data_uri:
            texture_index_cache[key] = None
            return None

        if not gltf_samplers:
            gltf_samplers.append(
                {
                    "magFilter": 9729,  # LINEAR
                    "minFilter": 9987,  # LINEAR_MIPMAP_LINEAR
                    "wrapS": 10497,  # REPEAT
                    "wrapT": 10497,  # REPEAT
                }
            )

        img_idx = len(gltf_images)
        gltf_images.append({"uri": data_uri, "mimeType": mime})

        tex_idx = len(gltf_textures)
        gltf_textures.append({"source": img_idx, "sampler": 0})
        texture_index_cache[key] = tex_idx
        return tex_idx

    def apply_material_metadata(
        mat_def,
        normal_texture_name=None,
        normal_scale=None,
        specular_texture_name=None,
        specular_factor=None,
        specular_color_factor=None,
        detail_texture_name=None,
        detail_scale=None,
        is_water=False,
        material_extras=None,
    ):
        if normal_texture_name:
            normal_tex_idx = get_or_create_texture(normal_texture_name)
            if normal_tex_idx is not None:
                normal_def = {"index": normal_tex_idx}
                if normal_scale is not None:
                    normal_def["scale"] = normal_scale
                mat_def["normalTexture"] = normal_def

        specular_def = {}
        if specular_factor is not None:
            specular_def["specularFactor"] = specular_factor
        if specular_color_factor:
            specular_def["specularColorFactor"] = specular_color_factor[:3]
        if specular_texture_name:
            specular_tex_idx = get_or_create_texture(specular_texture_name)
            if specular_tex_idx is not None:
                specular_def["specularTexture"] = {"index": specular_tex_idx}
        if specular_def:
            mat_def.setdefault("extensions", {})["KHR_materials_specular"] = specular_def
            extensions_used.add("KHR_materials_specular")

        detail_tex_idx = None
        if detail_texture_name:
            detail_tex_idx = get_or_create_texture(detail_texture_name)

        extras = json.loads(json.dumps(material_extras or {}))

        runtime_texture_indices = []

        def embed_runtime_graph_textures(value):
            if isinstance(value, dict):
                if value.get("type") == "texture":
                    asset = value.get("asset") or {}
                    texture_name = asset.get("asset_name")
                    texture_index = (
                        get_or_create_texture(texture_name) if texture_name else None
                    )
                    if texture_index is not None:
                        value["texture_index"] = texture_index
                        runtime_texture_indices.append(texture_index)
                for child_value in value.values():
                    embed_runtime_graph_textures(child_value)
            elif isinstance(value, list):
                for child_value in value:
                    embed_runtime_graph_textures(child_value)

        runtime_graph = extras.get("vg_runtime_material_graph")
        if runtime_graph:
            embed_runtime_graph_textures(runtime_graph)
            extras["vg_runtime_material_texture_indices"] = sorted(
                set(runtime_texture_indices)
            )
            extras["cesium_godot_application_texture_indices"] = sorted(
                set(runtime_texture_indices)
            )
        if is_water:
            extras["is_water"] = True
        if normal_texture_name:
            extras["vg_generated_normal_texture"] = normal_texture_name
        if specular_texture_name:
            extras["vg_specular_texture"] = specular_texture_name
        if detail_texture_name:
            extras["vg_detail_texture"] = detail_texture_name
        if detail_tex_idx is not None:
            extras["vg_detail_texture_index"] = detail_tex_idx
        if detail_scale is not None:
            extras["vg_detail_scale"] = detail_scale
        if extras:
            mat_def.setdefault("extras", {}).update(extras)

    def get_or_create_material(
        texture_name,
        shader_name,
        base_color_factor=None,
        alpha_mode=None,
        is_water=False,
        two_sided=False,
        normal_texture_name=None,
        normal_scale=None,
        specular_texture_name=None,
        specular_factor=None,
        specular_color_factor=None,
        detail_texture_name=None,
        detail_scale=None,
        material_extras=None,
    ):
        """Get or create a glTF material for a texture or color. Returns material index.
        alpha_mode: "mask" if UE2 Shader has OutputBlending=OB_Masked or Opacity property.
        is_water: True if this is a WaterShaderMaterial — embeds extras for viewer-side animation.
        two_sided: Whether to render both faces (from Shader.TwoSided property; False for all water shaders).
        """
        extras_key = json.dumps(material_extras or {}, sort_keys=True, default=str)
        cache_key = (
            texture_name or shader_name,
            tuple(base_color_factor or []),
            alpha_mode,
            is_water,
            two_sided,
            normal_texture_name,
            normal_scale,
            specular_texture_name,
            specular_factor,
            tuple(specular_color_factor or []),
            detail_texture_name,
            detail_scale,
            extras_key,
        )
        if cache_key in material_cache:
            return material_cache[cache_key]

        mat_idx = len(gltf_materials)

        # Check for constant color (format: "color:r,g,b")
        if texture_name and texture_name.startswith("color:"):
            parts = texture_name[6:].split(",")
            r, g, b = float(parts[0]), float(parts[1]), float(parts[2])
            mat_def = {
                "name": shader_name or "color",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [r, g, b, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.8,
                },
                "doubleSided": two_sided,
            }
            apply_material_metadata(
                mat_def,
                normal_texture_name=normal_texture_name,
                normal_scale=normal_scale,
                specular_texture_name=specular_texture_name,
                specular_factor=specular_factor,
                specular_color_factor=specular_color_factor,
                detail_texture_name=detail_texture_name,
                detail_scale=detail_scale,
                is_water=is_water,
                material_extras=material_extras,
            )
            gltf_materials.append(mat_def)
            material_cache[cache_key] = mat_idx
            return mat_idx

        if texture_name:
            tex_idx = get_or_create_texture(texture_name)
            if tex_idx is not None:
                mat_def = {
                    "name": shader_name or texture_name,
                    "pbrMetallicRoughness": {
                        "baseColorTexture": {"index": tex_idx},
                        "metallicFactor": 0.0,
                        "roughnessFactor": 1.0 if alpha_mode == "mask" else 0.8,
                    },
                    "doubleSided": two_sided,
                }
                if alpha_mode == "mask":
                    mat_def["alphaMode"] = "MASK"
                    mat_def["alphaCutoff"] = 0.01

                apply_material_metadata(
                    mat_def,
                    normal_texture_name=normal_texture_name,
                    normal_scale=normal_scale,
                    specular_texture_name=specular_texture_name,
                    specular_factor=specular_factor,
                    specular_color_factor=specular_color_factor,
                    detail_texture_name=detail_texture_name,
                    detail_scale=detail_scale,
                    is_water=is_water,
                    material_extras=material_extras,
                )
                gltf_materials.append(mat_def)
                material_cache[cache_key] = mat_idx
                return mat_idx

        # Fallback: untextured material
        mat_def = {
            "name": shader_name or "default",
            "pbrMetallicRoughness": {
                "baseColorFactor": base_color_factor or [0.7, 0.7, 0.7, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.9,
            },
            "doubleSided": two_sided,
        }
        apply_material_metadata(
            mat_def,
            normal_texture_name=normal_texture_name,
            normal_scale=normal_scale,
            specular_texture_name=specular_texture_name,
            specular_factor=specular_factor,
            specular_color_factor=specular_color_factor,
            detail_texture_name=detail_texture_name,
            detail_scale=detail_scale,
            is_water=is_water,
            material_extras=material_extras,
        )
        gltf_materials.append(mat_def)
        material_cache[cache_key] = mat_idx
        return mat_idx

    # --- Helper: detect and convert triangle strips to triangle lists ---
    def strip_to_list(raw_indices):
        """Convert a triangle strip index buffer to a triangle list.
        Returns (converted_indices, was_strip)."""
        if len(raw_indices) < 3:
            return raw_indices, False

        # Detect strip: count degenerate triangles when interpreted as a list
        list_tris = len(raw_indices) // 3
        degen_count = 0
        for i in range(0, list_tris * 3, 3):
            a, b, c = raw_indices[i], raw_indices[i + 1], raw_indices[i + 2]
            if a == b or b == c or a == c:
                degen_count += 1

        # If >10% of triangles are degenerate as a list, it's a strip
        if list_tris > 0 and degen_count / list_tris > 0.10:
            # Unpack triangle strip to triangle list
            out = []
            for i in range(len(raw_indices) - 2):
                i0, i1, i2 = raw_indices[i], raw_indices[i + 1], raw_indices[i + 2]
                # Skip degenerate triangles
                if i0 == i1 or i1 == i2 or i0 == i2:
                    continue
                # Flip winding on odd triangles to maintain consistent face orientation
                if i % 2 == 0:
                    out.extend([i0, i1, i2])
                else:
                    out.extend([i0, i2, i1])
            return out, True

        return raw_indices, False

    # --- Build primitives (one per section or single fallback) ---
    primitives = []

    if section_materials and any(sm[0] is not None for sm in section_materials):
        # Multi-material: one primitive per non-empty section
        for si, sec in enumerate(mesh.sections):
            nf = sec.get("num_faces", 0)
            if nf == 0:
                continue

            first_index = sec.get("first_index", 0)

            # Determine if num_faces is a face count (triangle list) or index count (strip).
            # Triangle lists: num_faces * 3 = actual index count
            # Triangle strips: num_faces = index count (strip length)
            # Heuristic: if first_index + nf*3 fits in the buffer AND using nf as
            # index count would leave >50% of vertices unused, treat as face count.
            num_indices = nf
            if first_index + nf * 3 <= len(mesh.indices):
                # Check coverage: how many unique verts does nf-as-indices cover?
                slice_as_idx = mesh.indices[first_index : first_index + nf]
                unique_as_idx = len(set(slice_as_idx))
                expected_verts = (
                    sec.get("last_vertex", 0) - sec.get("first_vertex", 0) + 1
                )
                if expected_verts > 0 and unique_as_idx < expected_verts * 0.6:
                    num_indices = nf * 3  # It's a face count, multiply by 3

            # Bounds check
            if first_index + num_indices > len(mesh.indices):
                num_indices = len(mesh.indices) - first_index
                if num_indices <= 0:
                    continue

            # Extract section indices and convert strips to lists
            sec_indices = list(mesh.indices[first_index : first_index + num_indices])
            sec_indices, was_strip = strip_to_list(sec_indices)

            # Pad to 4-byte alignment
            while len(buffer_data) % 4 != 0:
                buffer_data.append(0)

            # Write section indices
            idx_bv = len(buffer_views)
            idx_start = len(buffer_data)
            for idx in sec_indices:
                buffer_data.extend(struct.pack("<H", idx))
            idx_end = len(buffer_data)

            buffer_views.append(
                {
                    "buffer": 0,
                    "byteOffset": idx_start,
                    "byteLength": idx_end - idx_start,
                    "target": 34963,
                }
            )
            idx_accessor = len(accessors)
            accessors.append(
                {
                    "bufferView": idx_bv,
                    "componentType": 5123,
                    "count": len(sec_indices),
                    "type": "SCALAR",
                }
            )

            # Get material
            sm = (
                section_materials[si]
                if si < len(section_materials)
                else (
                    None,
                    None,
                    None,
                    None,
                    False,
                    False,
                    None,
                    None,
                    {},
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
            shader_name, texture_name, base_color_factor, alpha_mode = (
                sm[0],
                sm[1],
                sm[2] if len(sm) > 2 else None,
                sm[3] if len(sm) > 3 else None,
            )
            is_water = sm[4] if len(sm) > 4 else False
            two_sided = sm[5] if len(sm) > 5 else False
            normal_texture_name = sm[6] if len(sm) > 6 else None
            normal_scale = sm[7] if len(sm) > 7 else None
            material_extras = sm[8] if len(sm) > 8 else {}
            specular_texture_name = sm[9] if len(sm) > 9 else None
            specular_factor = sm[10] if len(sm) > 10 else None
            specular_color_factor = sm[11] if len(sm) > 11 else None
            detail_texture_name = sm[12] if len(sm) > 12 else None
            detail_scale = sm[13] if len(sm) > 13 else None
            mat_idx = get_or_create_material(
                texture_name,
                shader_name,
                base_color_factor=base_color_factor,
                alpha_mode=alpha_mode,
                is_water=is_water,
                two_sided=two_sided,
                normal_texture_name=normal_texture_name,
                normal_scale=normal_scale,
                specular_texture_name=specular_texture_name,
                specular_factor=specular_factor,
                specular_color_factor=specular_color_factor,
                detail_texture_name=detail_texture_name,
                detail_scale=detail_scale,
                material_extras=material_extras,
            )

            prim_attributes = dict(attributes)
            if attr_bb_idx is not None and alpha_mode == "mask":
                if any(mesh._billboard_size[idx] > 0 for idx in sec_indices if 0 <= idx < len(mesh._billboard_size)):
                    prim_attributes["_BILLBOARD"] = attr_bb_idx

            prim = {
                "attributes": prim_attributes,
                "indices": idx_accessor,
                "material": mat_idx,
                "mode": 4,
            }
            primitives.append(prim)

    if not primitives:
        # Fallback: single primitive with all indices (no material split)
        fallback_indices = list(mesh.indices)
        fallback_indices, _ = strip_to_list(fallback_indices)

        while len(buffer_data) % 4 != 0:
            buffer_data.append(0)

        idx_bv = len(buffer_views)
        idx_start = len(buffer_data)
        for idx in fallback_indices:
            buffer_data.extend(struct.pack("<H", idx))
        idx_end = len(buffer_data)

        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": idx_start,
                "byteLength": idx_end - idx_start,
                "target": 34963,
            }
        )
        idx_accessor = len(accessors)
        accessors.append(
            {
                "bufferView": idx_bv,
                "componentType": 5123,
                "count": len(fallback_indices),
                "type": "SCALAR",
            }
        )

        prim_attributes = dict(attributes)
        if attr_bb_idx is not None:
            prim_attributes["_BILLBOARD"] = attr_bb_idx

        prim = {"attributes": prim_attributes, "indices": idx_accessor, "mode": 4}
        # Add default material if we have any materials
        if gltf_materials:
            prim["material"] = 0
        primitives.append(prim)

    # --- Assemble glTF ---
    gltf = {
        "asset": {"version": "2.0", "generator": "Vanguard StaticMesh Pipeline v2"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": mesh.name}],
        "meshes": [{"primitives": primitives, "name": mesh.name}],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [],
    }

    if gltf_materials:
        gltf["materials"] = gltf_materials
    if gltf_textures:
        gltf["textures"] = gltf_textures
    if gltf_images:
        gltf["images"] = gltf_images
    if gltf_samplers:
        gltf["samplers"] = gltf_samplers
    if extensions_used:
        gltf["extensionsUsed"] = sorted(extensions_used)

    # Encode buffer as base64 data URI
    b64_data = base64.b64encode(buffer_data).decode("utf-8")
    gltf["buffers"] = [
        {
            "uri": f"data:application/octet-stream;base64,{b64_data}",
            "byteLength": len(buffer_data),
        }
    ]

    # Write file
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    candidate_path = f"{output_path}.writing-{os.getpid()}"
    try:
        with open(candidate_path, "w") as f:
            json.dump(gltf, f)
        os.replace(candidate_path, output_path)
    finally:
        if os.path.exists(candidate_path):
            os.unlink(candidate_path)

    return True


# =============================================================================
# MAIN PIPELINE
# =============================================================================


def process_package(
    pkg_path: str,
    conn: Optional[sqlite3.Connection],
    session_id: int,
    export_gltf: bool = True,
    output_dir: str = None,
    only_trees: bool = False,
    export_runtime_leaf_hybrids: bool = False,
    required_mesh_names: Optional[set[str]] = None,
) -> Dict[str, int]:
    """
    Process a single package file through the complete pipeline.
    Returns stats dict.
    """
    stats = {"success": 0, "error": 0, "skipped": 0, "exported": 0, "hybrid_exported": 0}

    file_id = get_or_create_file_id(conn, pkg_path) if conn else 0
    # sys.stderr.write(f"DEBUG: Processing {os.path.basename(pkg_path)}\n")
    meshes = parse_staticmesh_file(pkg_path)

    for mesh in meshes:
        if (
            required_mesh_names is not None
            and mesh.name.casefold() not in required_mesh_names
        ):
            stats["skipped"] += 1
            continue
        if only_trees and not is_tree_mesh_name(mesh.name):
            stats["skipped"] += 1
            continue

        # Store in database
        if conn:
            store_parsed_mesh(conn, mesh, file_id, session_id)

        if mesh.parse_status == "complete":
            stats["success"] += 1

            # Export glTF if requested
            if export_gltf and output_dir and mesh.vertices and mesh.indices:
                pkg_name = os.path.splitext(os.path.basename(pkg_path))[0]
                gltf_path = os.path.join(output_dir, pkg_name, f"{mesh.name}.gltf")
                if mesh_to_gltf(mesh, gltf_path):
                    stats["exported"] += 1
                    if export_runtime_leaf_hybrids:
                        hybrid_path = _maybe_export_runtime_leaf_hybrid(gltf_path, mesh.name)
                        if hybrid_path:
                            stats["hybrid_exported"] += 1
                    # Mark as exported in database
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            """
                            UPDATE parsed_exports 
                            SET gltf_exported = 1 
                            WHERE file_id = ? AND export_index = ?
                        """,
                            (file_id, mesh.export_index),
                        )
                        conn.commit()

        elif "skipped" in mesh.parse_status:
            stats["skipped"] += 1
        else:
            stats["error"] += 1

    return stats


def empty_stats() -> Dict[str, int]:
    return {"success": 0, "error": 0, "skipped": 0, "exported": 0, "hybrid_exported": 0}


def merge_stats(total: Dict[str, int], stats: Dict[str, int]) -> None:
    for key in total:
        total[key] += stats.get(key, 0)


def process_package_worker(args) -> Tuple[str, Dict[str, int], Optional[str]]:
    (
        pkg_path,
        export_gltf,
        output_dir,
        only_trees,
        export_runtime_leaf_hybrids,
        required_mesh_names,
    ) = args
    try:
        stats = process_package(
            pkg_path,
            None,
            0,
            export_gltf=export_gltf,
            output_dir=output_dir,
            only_trees=only_trees,
            export_runtime_leaf_hybrids=export_runtime_leaf_hybrids,
            required_mesh_names=required_mesh_names,
        )
        return pkg_path, stats, None
    except Exception as exc:
        stats = empty_stats()
        stats["error"] = 1
        return pkg_path, stats, str(exc)


def run_pipeline(
    file_pattern: str = None,
    object_artifact: str = None,
    export_gltf: bool = True,
    export_only: bool = False,
    limit: int = 0,
    silent: bool = False,
    only_trees: bool = False,
    export_runtime_leaf_hybrids: bool = False,
    workers: int = 1,
):
    """
    Run the complete StaticMesh parsing pipeline.
    """
    if workers < 1:
        workers = os.cpu_count() or 1

    if not silent:
        print("=" * 60)
        print("Vanguard StaticMesh Pipeline")
        print("=" * 60)
        print(f"Database: {CANONICAL_DB}")
        print(f"Meshes Dir: {MESHES_DIR}")
        print(f"Output Dir: {OUTPUT_DIR}")
        if export_only:
            print("Mode: EXPORT-ONLY (Skipping database updates)")
        if workers > 1:
            print(f"Workers: {workers} process(es)")
            if not export_only:
                print("Mode: PARALLEL EXPORT (Skipping database updates)")
        if only_trees:
            print("Filter: TREE MESHES ONLY")
        if export_runtime_leaf_hybrids:
            print("Extra Export: RUNTIME-LEAF HYBRIDS")
        print()

    # Find files to process
    required_mesh_names_by_package: dict[str, set[str]] = {}
    if object_artifact:
        object_root = Path(object_artifact) / "objects"
        if not object_root.is_dir():
            raise FileNotFoundError(
                f"object artifact has no shared object store: {object_root}"
            )
        for path in object_root.rglob("*.glb"):
            package_name = path.parent.name.casefold()
            required_mesh_names_by_package.setdefault(package_name, set()).add(
                path.stem.casefold()
            )
        required_packages = set(required_mesh_names_by_package)
        available_packages = {
            Path(path).stem.casefold(): path
            for path in glob.glob(os.path.join(MESHES_DIR, "*.usx"))
        }
        missing = sorted(required_packages - available_packages.keys())
        if missing:
            raise FileNotFoundError(
                "object artifact references missing mesh packages: "
                + ", ".join(missing[:20])
            )
        files = [available_packages[name] for name in sorted(required_packages)]
    elif file_pattern:
        pattern = (
            file_pattern if file_pattern.endswith(".usx") else file_pattern + "*.usx"
        )
        files = glob.glob(os.path.join(MESHES_DIR, pattern))
    else:
        files = glob.glob(os.path.join(MESHES_DIR, "*.usx"))

    if limit > 0:
        files = files[:limit]

    if not silent:
        print(f"Found {len(files)} files to process")
        print()

    total_files = len(files)
    if total_files == 0:
        return

    workers = min(workers, total_files)
    parallel = workers > 1
    db_enabled = not export_only and not parallel

    # Connect to database
    conn = None
    session_id = None
    if db_enabled:
        conn = sqlite3.connect(CANONICAL_DB)
        session_id = create_parse_session(conn)
        if not silent:
            print(f"Created parse session: {session_id}")
            print()

    # Process files
    total_stats = empty_stats()

    start_time = time.time()
    if parallel:
        worker_args = [
            (
                pkg_path,
                export_gltf,
                OUTPUT_DIR,
                only_trees,
                export_runtime_leaf_hybrids,
                required_mesh_names_by_package.get(Path(pkg_path).stem.casefold()),
            )
            for pkg_path in files
        ]
        completed = 0
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(process_package_worker, args) for args in worker_args]
            for future in as_completed(futures):
                pkg_path, stats, error = future.result()
                completed += 1
                merge_stats(total_stats, stats)
                filename = os.path.basename(pkg_path)

                if total_files > 5 and not silent:
                    print_progress(completed, total_files, filename, start_time, total_stats)
                elif not silent:
                    if error:
                        print(f"[{completed}/{total_files}] {filename}: ERROR {error}")
                    else:
                        print(f"[{completed}/{total_files}] {filename}: OK ({stats['success']} meshes)")

                if error and total_files > 5:
                    print(f"\n  ERROR processing {filename}: {error}")
    else:
        for i, pkg_path in enumerate(files):
            filename = os.path.basename(pkg_path)
            # Show progress bar if many files
            if total_files > 5 and not silent:
                print_progress(i + 1, total_files, filename, start_time, total_stats)
            elif not silent:
                print(f"[{i+1}/{total_files}] Processing {filename}...")

            try:
                stats = process_package(
                    pkg_path,
                    conn if db_enabled else None,
                    session_id if db_enabled else 0,
                    export_gltf=export_gltf,
                    output_dir=OUTPUT_DIR,
                    only_trees=only_trees,
                    export_runtime_leaf_hybrids=export_runtime_leaf_hybrids,
                    required_mesh_names=required_mesh_names_by_package.get(
                        Path(pkg_path).stem.casefold()
                    ),
                )
                merge_stats(total_stats, stats)

                if not silent and total_files <= 5:
                    print(f" OK ({stats['success']} meshes)")
            except Exception as e:
                if not silent:
                    print(f"\n  ERROR processing {os.path.basename(pkg_path)}: {e}")
                total_stats["error"] += 1

    # Update session as complete (skip if export-only mode)
    if conn is not None:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE parse_sessions 
            SET completed_at = ?, 
                files_processed = ?,
                exports_processed = ?,
                total_bytes_parsed = ?
            WHERE id = ?
        """,
            (
                datetime.now().isoformat(),
                len(files),
                total_stats["success"] + total_stats["error"] + total_stats["skipped"],
                total_stats["success"],
                session_id,
            ),
        )
        conn.commit()
        conn.close()

    # Print summary
    if not silent:
        print()
        print("=" * 60)
        print("Pipeline Complete")
        print("=" * 60)
        print(f"Files Processed: {len(files)}")
        print(f"Meshes Parsed:   {total_stats['success']}")
        print(f"Meshes Skipped:  {total_stats['skipped']}")
        print(f"Meshes Failed:   {total_stats['error']}")
        print(f"glTF Exported:   {total_stats['exported']}")
        print(f"Hybrids Exported:{total_stats['hybrid_exported']}")
        print()

    if not silent:
        success_rate = (
            total_stats["success"]
            / (total_stats["success"] + total_stats["error"] + total_stats["skipped"])
            * 100
            if (total_stats["success"] + total_stats["error"] + total_stats["skipped"])
            > 0
            else 0
        )
        print(f"Success Rate: {success_rate:.1f}%")


def write_mesh_manifest(output_dir: str) -> int:
    """Write the static mesh manifest consumed by chunk object generation."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    meshes = sorted(
        {
            path.relative_to(output_path).as_posix()
            for path in output_path.rglob("*.gltf")
        }
    )
    manifest_path = output_path / "manifest.json"
    tmp_path = manifest_path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump({"meshes": meshes}, handle, indent=2)
        handle.write("\n")
    os.replace(tmp_path, manifest_path)
    return len(meshes)


def main():
    parser = argparse.ArgumentParser(description="Vanguard StaticMesh Pipeline")
    parser.add_argument(
        "--file", "-f", help='Specific file pattern to process (e.g., "Ra44*.usx")'
    )
    parser.add_argument(
        "--export-only", action="store_true", help="Only export glTF, skip DB updates"
    )
    parser.add_argument(
        "--object-artifact",
        help=(
            "Only export mesh packages referenced by an existing Cesium "
            "artifact's shared objects/ store"
        ),
    )
    parser.add_argument(
        "--limit", "-n", type=int, default=0, help="Limit number of files to process"
    )
    parser.add_argument(
        "--silent", action="store_true", help="Suppress all output except errors"
    )
    parser.add_argument(
        "--trees", action="store_true", help="Only process/export tree meshes"
    )
    parser.add_argument(
        "--runtime-leaf-hybrids",
        action="store_true",
        help="When recovered leaf-card sidecars exist, write sibling *_runtime_leaves_hybrid.gltf assets",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Worker processes for package-level export; 0 uses all CPUs. Values above 1 skip SQLite writes.",
    )

    args = parser.parse_args()

    run_pipeline(
        file_pattern=args.file,
        object_artifact=args.object_artifact,
        export_gltf=True,
        export_only=args.export_only,
        limit=args.limit,
        silent=args.silent,
        only_trees=args.trees,
        export_runtime_leaf_hybrids=args.runtime_leaf_hybrids,
        workers=args.workers,
    )

    manifest_count = write_mesh_manifest(OUTPUT_DIR)
    if not args.silent:
        print(f"Wrote mesh manifest: {manifest_count} entries")


if __name__ == "__main__":
    main()
