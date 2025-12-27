#!/usr/bin/env python3
"""
Extract BSP geometry from Vanguard chunk files (.vgr).

This script parses UE2 Model objects containing BSP data and converts them
to triangle meshes for WebGL rendering.

Based on UTPackage.js by bunnytrack.net - ported to Python for Vanguard.

BSP Structure (UE2):
- Model contains: vectors, points, nodes, surfaces, vertices
- Polys object contains pre-triangulated polygon data
- For compiled BSP, we reconstruct triangles from surfaces + vertices
"""

import struct
import json
import os
import sys
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

# Add parent directory to path to allow importing ue2 package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ue2 import Vector, Plane, UE2Package
from ue2.reader import BinaryReader, read_compact_index_at as read_compact_index


@dataclass
class BspNode:
    """BSP tree node - defines splitting planes and child references."""

    plane: Plane = field(default_factory=Plane)
    zone_mask: int = 0
    node_flags: int = 0
    i_vert_pool: int = 0  # Index into vertex pool
    i_surf: int = 0  # Index into surfaces array
    i_front: int = 0  # Front child node
    i_back: int = 0  # Back child node
    i_plane: int = 0
    i_collision_bound: int = 0
    i_render_bound: int = 0
    i_zone: List[int] = field(default_factory=lambda: [0, 0])
    num_vertices: int = 0  # Number of vertices for this node's polygon
    i_leaf: List[int] = field(default_factory=lambda: [0, 0])


@dataclass
class BspSurface:
    """BSP surface - defines a polygon face with texture info."""

    texture: int = 0
    poly_flags: int = 0
    p_base: int = 0  # Base point index
    v_normal: int = 0  # Normal vector index
    v_texture_u: int = 0  # Texture U vector index
    v_texture_v: int = 0  # Texture V vector index
    i_light_map: int = 0
    i_brush_poly: int = 0
    pan_u: int = 0
    pan_v: int = 0
    actor: int = 0


@dataclass
class ModelVertex:
    """Vertex reference in BSP - points to actual vertex position."""

    vertex: int = 0  # Index into points array
    i_side: int = 0


@dataclass
class Polygon:
    """Pre-triangulated polygon from Polys object."""

    vertex_count: int = 0
    origin: Vector = field(default_factory=Vector)
    normal: Vector = field(default_factory=Vector)
    texture_u: Vector = field(default_factory=Vector)
    texture_v: Vector = field(default_factory=Vector)
    vertices: List[Vector] = field(default_factory=list)
    flags: int = 0
    actor: int = 0
    texture: int = 0
    item_name: int = 0
    link: int = 0
    brush_poly: int = 0
    pan_u: int = 0
    pan_v: int = 0


@dataclass
class UModel:
    """UE2 Model object containing BSP geometry."""

    vectors: List[Vector] = field(default_factory=list)
    points: List[Vector] = field(default_factory=list)
    nodes: List[BspNode] = field(default_factory=list)
    surfaces: List[BspSurface] = field(default_factory=list)
    vertices: List[ModelVertex] = field(default_factory=list)
    num_shared_sides: int = 0
    num_zones: int = 0
    polys_ref: int = 0  # Reference to Polys object
    root_outside: bool = False
    linked: bool = False


@dataclass
class UPolys:
    """Polys object containing pre-built polygon list."""

    polygons: List[Polygon] = field(default_factory=list)


# =============================================================================
# Binary Reader (Moved to ue2.reader)
# =============================================================================
# Using shared BinaryReader from ue2 package


# =============================================================================
# UE2 Package Parser (Moved to ue2.package)
# =============================================================================
# Using shared UE2Package from ue2 package


# =============================================================================
# BSP Parser
# =============================================================================


class BSPParser:
    """Parse BSP/Model data from UE2 packages."""

    def __init__(self, package: UE2Package):
        self.package = package

    def skip_properties(self, reader: BinaryReader) -> int:
        """Skip UE2 property block, return bytes consumed."""
        start_pos = reader.tell()

        while True:
            # Read property name index
            name_idx = reader.read_compact_index()
            if name_idx < 0 or name_idx >= len(self.package.names):
                break

            prop_name = self.package.names[name_idx]
            if prop_name.lower() == "none":
                break

            # Read property info byte
            info = reader.read_uint8()
            prop_type = info & 0x0F
            size_type = (info >> 4) & 0x07
            array_flag = (info >> 7) & 0x01

            # Struct subtype
            if prop_type == 10:  # Struct
                reader.read_compact_index()  # struct name

            # Determine size
            if size_type == 0:
                prop_size = 1
            elif size_type == 1:
                prop_size = 2
            elif size_type == 2:
                prop_size = 4
            elif size_type == 3:
                prop_size = 12
            elif size_type == 4:
                prop_size = 16
            elif size_type == 5:
                prop_size = reader.read_uint8()
            elif size_type == 6:
                prop_size = reader.read_uint16()
            elif size_type == 7:
                prop_size = reader.read_uint32()
            else:
                prop_size = 0

            # Array index
            if prop_type != 3 and array_flag:  # Not boolean
                idx_byte = reader.read_uint8()
                if idx_byte >= 0x80:
                    reader.read_bytes((idx_byte >> 6) - 1)

            # Skip property data
            reader.read_bytes(prop_size)

        return reader.tell() - start_pos

    def parse_model(self, export: Dict) -> Optional[UModel]:
        """Parse a Model export into UModel structure."""
        data = self.package.get_export_data(export)
        if not data:
            return None

        reader = BinaryReader(data)
        model = UModel()

        try:
            # Skip properties
            self.skip_properties(reader)

            # UE2 version check - Vanguard is version 128+
            version = self.package.version

            if version <= 61:
                # Old format - just indices
                model.vectors = []
                model.points = []
                model.nodes = []
                model.surfaces = []
                model.vertices = []
                reader.read_compact_index()  # vectors ref
                reader.read_compact_index()  # points ref
                reader.read_compact_index()  # nodes ref
                reader.read_compact_index()  # surfaces ref
                reader.read_compact_index()  # vertices ref
            else:
                # New format - inline arrays
                # Vectors
                vec_count = reader.read_compact_index()
                model.vectors = [reader.read_vector() for _ in range(vec_count)]

                # Points
                point_count = reader.read_compact_index()
                model.points = [reader.read_vector() for _ in range(point_count)]

                # Nodes
                node_count = reader.read_compact_index()
                model.nodes = []
                for _ in range(node_count):
                    node = BspNode()
                    node.plane = reader.read_plane()
                    node.zone_mask = reader.read_uint64()
                    node.node_flags = reader.read_uint8()
                    node.i_vert_pool = reader.read_compact_index()
                    node.i_surf = reader.read_compact_index()
                    node.i_front = reader.read_compact_index()
                    node.i_back = reader.read_compact_index()
                    node.i_plane = reader.read_compact_index()
                    node.i_collision_bound = reader.read_compact_index()
                    node.i_render_bound = reader.read_compact_index()
                    node.i_zone = [
                        reader.read_compact_index(),
                        reader.read_compact_index(),
                    ]
                    node.num_vertices = reader.read_uint8()
                    node.i_leaf = [reader.read_uint32(), reader.read_uint32()]
                    model.nodes.append(node)

                # Surfaces
                surf_count = reader.read_compact_index()
                model.surfaces = []
                for _ in range(surf_count):
                    surf = BspSurface()
                    surf.texture = reader.read_compact_index()
                    surf.poly_flags = reader.read_uint32()
                    surf.p_base = reader.read_compact_index()
                    surf.v_normal = reader.read_compact_index()
                    surf.v_texture_u = reader.read_compact_index()
                    surf.v_texture_v = reader.read_compact_index()
                    surf.i_light_map = reader.read_compact_index()
                    surf.i_brush_poly = reader.read_compact_index()
                    surf.pan_u = reader.read_int16()
                    surf.pan_v = reader.read_int16()
                    surf.actor = reader.read_compact_index()
                    model.surfaces.append(surf)

                # Vertices
                vert_count = reader.read_compact_index()
                model.vertices = []
                for _ in range(vert_count):
                    mv = ModelVertex()
                    mv.vertex = reader.read_compact_index()
                    mv.i_side = reader.read_compact_index()
                    model.vertices.append(mv)

                # Additional fields
                model.num_shared_sides = reader.read_int32()
                model.num_zones = reader.read_int32()

                # Skip zones array
                for _ in range(model.num_zones):
                    reader.read_compact_index()  # zone_actor
                    reader.read_uint64()  # connectivity
                    reader.read_uint64()  # visibility

            # Polys reference
            model.polys_ref = reader.read_compact_index()

            return model

        except Exception as e:
            print(f"    Error parsing Model: {e}")
            return None

    def parse_polys(self, export: Dict) -> Optional[UPolys]:
        """Parse a Polys export into UPolys structure.

        Vanguard Polys format (discovered through reverse engineering):
        - Byte 0: None property terminator (0)
        - Bytes 1-2: polygon count (compact index)
        - Bytes 3-8: unknown header (6 bytes, possibly padding)
        - Byte 9+: polygon data

        Each polygon:
        - 1 byte: vertex count
        - 12 bytes: origin (3 floats)
        - 12 bytes: normal (3 floats)
        - 12 bytes: texture U (3 floats)
        - 12 bytes: texture V (3 floats)
        - vertex_count * 12 bytes: vertices (3 floats each)
        - 4 bytes: flags
        - compact indices: actor, texture, item_name, link, brush_poly
        - 4 bytes: pan_u, pan_v (2 uint16)
        """
        data = self.package.get_export_data(export)
        if not data or len(data) < 10:
            return None

        reader = BinaryReader(data)
        polys = UPolys()

        try:
            # Vanguard format: skip None terminator (byte 0)
            reader.read_uint8()

            # Read polygon count
            poly_count = reader.read_compact_index()

            if poly_count <= 0 or poly_count > 10000:
                return None

            # Skip 7-byte header (unknown purpose - possibly padding/alignment)
            reader.read_bytes(7)

            for _ in range(poly_count):
                poly = Polygon()
                poly.vertex_count = reader.read_uint8()

                if poly.vertex_count <= 0 or poly.vertex_count > 100:
                    break

                poly.origin = reader.read_vector()
                poly.normal = reader.read_vector()
                poly.texture_u = reader.read_vector()
                poly.texture_v = reader.read_vector()

                # Read vertices
                poly.vertices = [reader.read_vector() for _ in range(poly.vertex_count)]

                poly.flags = reader.read_uint32()
                poly.actor = reader.read_compact_index()
                poly.texture = reader.read_compact_index()
                poly.item_name = reader.read_compact_index()
                poly.link = reader.read_compact_index()
                poly.brush_poly = reader.read_compact_index()
                poly.pan_u = reader.read_uint16()
                poly.pan_v = reader.read_uint16()

                # Sign extend pan values
                if poly.pan_u > 0x8000:
                    poly.pan_u |= 0xFFFF0000
                if poly.pan_v > 0x8000:
                    poly.pan_v |= 0xFFFF0000

                polys.polygons.append(poly)

            return polys

        except Exception as e:
            print(f"    Error parsing Polys: {e}")
            return None

    def model_to_triangles(self, model: UModel) -> Tuple[np.ndarray, np.ndarray]:
        """Convert Model BSP data to triangle mesh.

        Returns (vertices, indices) arrays.
        """
        all_vertices = []
        all_indices = []
        vertex_offset = 0

        # Method 1: Use BSP nodes to reconstruct polygons
        # Each node references a surface and a range of vertices
        for node in model.nodes:
            if node.num_vertices < 3:
                continue

            surf_idx = node.i_surf
            if surf_idx < 0 or surf_idx >= len(model.surfaces):
                continue

            # Get vertices for this node's polygon
            vert_start = node.i_vert_pool
            vert_count = node.num_vertices

            if vert_start < 0 or vert_start + vert_count > len(model.vertices):
                continue

            # Collect polygon vertices
            poly_verts = []
            for i in range(vert_count):
                mv = model.vertices[vert_start + i]
                if mv.vertex < 0 or mv.vertex >= len(model.points):
                    continue
                pt = model.points[mv.vertex]
                poly_verts.append([pt.x, pt.y, pt.z])

            if len(poly_verts) < 3:
                continue

            # Fan triangulation
            base_idx = len(all_vertices)
            all_vertices.extend(poly_verts)

            for i in range(1, len(poly_verts) - 1):
                all_indices.extend([base_idx, base_idx + i, base_idx + i + 1])

        if not all_vertices:
            return np.array([], dtype=np.float32), np.array([], dtype=np.uint32)

        return (
            np.array(all_vertices, dtype=np.float32),
            np.array(all_indices, dtype=np.uint32),
        )

    def polys_to_triangles(
        self, polys: UPolys, max_span: float = 2000000
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Convert Polys data to triangle mesh.

        Args:
            polys: UPolys object containing polygon data
            max_span: Maximum coordinate span to include (filters out world bounds)

        Returns (vertices, indices) arrays.
        """
        all_vertices = []
        all_indices = []

        for poly in polys.polygons:
            if poly.vertex_count < 3:
                continue

            # Collect vertices
            poly_verts = [[v.x, v.y, v.z] for v in poly.vertices]

            # Check coordinate span - skip world bounds/zone volumes
            coords = [c for v in poly_verts for c in v]
            span = max(coords) - min(coords)
            if span > max_span:
                continue

            # Fan triangulation
            base_idx = len(all_vertices)
            all_vertices.extend(poly_verts)

            for i in range(1, len(poly_verts) - 1):
                all_indices.extend([base_idx, base_idx + i, base_idx + i + 1])

        if not all_vertices:
            return np.array([], dtype=np.float32), np.array([], dtype=np.uint32)

        return (
            np.array(all_vertices, dtype=np.float32),
            np.array(all_indices, dtype=np.uint32),
        )


# =============================================================================
# Utility Functions
# =============================================================================


# =============================================================================
# Utility Functions (Moved to ue2.reader)
# =============================================================================
# Using shared read_compact_index_at from ue2 package


# =============================================================================
# CompoundObject Position Parsing
# =============================================================================


def parse_compound_object_position(pkg: "UE2Package", export: Dict) -> Optional[Vector]:
    """Parse a CompoundObject export to extract its world position.

    CompoundObject position is stored at the end of the data:
    - Position X: bytes[-13:-9] as float
    - Position Y: bytes[-9:-5] as float
    - Position Z: bytes[-5:-1] as float
    """
    data = pkg.get_export_data(export)
    if not data or len(data) < 13:
        return None

    try:
        x = struct.unpack("<f", data[-13:-9])[0]
        y = struct.unpack("<f", data[-9:-5])[0]
        z = struct.unpack("<f", data[-5:-1])[0]

        # Validate - skip NaN values
        import math

        if math.isnan(x) or math.isnan(y) or math.isnan(z):
            return None

        # Validate - skip invalid positions (too large for chunk bounds)
        # Chunk is 200000 units, so positions should be within reasonable range
        if any(abs(v) > 500000 for v in [x, y, z]):
            return None
        if all(v == 0 for v in [x, y, z]):
            return None

        return Vector(x, y, z)
    except:
        return None


# =============================================================================
# glTF Export
# =============================================================================


def create_gltf(vertices: np.ndarray, indices: np.ndarray, name: str = "BSP") -> dict:
    """Create a glTF 2.0 document from vertices and indices."""
    if len(vertices) == 0:
        return None

    vertices_bin = vertices.astype(np.float32).tobytes()
    indices_bin = indices.astype(np.uint32).tobytes()
    buffer_data = vertices_bin + indices_bin

    v_min = vertices.min(axis=0).tolist()
    v_max = vertices.max(axis=0).tolist()

    gltf = {
        "asset": {"version": "2.0", "generator": "Vanguard BSP Extractor"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [
            {
                "primitives": [
                    {"attributes": {"POSITION": 0}, "indices": 1, "material": 0}
                ]
            }
        ],
        "materials": [
            {
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.7, 0.6, 0.5, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.8,
                },
                "doubleSided": True,
            }
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,  # FLOAT
                "count": len(vertices),
                "type": "VEC3",
                "min": v_min,
                "max": v_max,
            },
            {
                "bufferView": 1,
                "componentType": 5125,  # UNSIGNED_INT
                "count": len(indices),
                "type": "SCALAR",
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(vertices_bin)},
            {
                "buffer": 0,
                "byteOffset": len(vertices_bin),
                "byteLength": len(indices_bin),
            },
        ],
        "buffers": [
            {
                "uri": f"data:application/octet-stream;base64,{base64.b64encode(buffer_data).decode()}",
                "byteLength": len(buffer_data),
            }
        ],
    }

    return gltf


def create_gltf_multi_mesh(mesh_list: List[Tuple], scene_name: str = "BSP") -> dict:
    """Create a glTF 2.0 document with separate named meshes for click identification.

    mesh_list: List of (vertices, indices, position, wv_name, polys_name) tuples
    """
    if not mesh_list:
        return None

    # Coordinate transform constants
    CHUNK_HALF_SIZE = 100000

    # Build all mesh data
    nodes = []
    meshes = []
    accessors = []
    buffer_views = []
    all_buffer_data = b""
    current_byte_offset = 0
    accessor_idx = 0

    for i, mesh_tuple in enumerate(mesh_list):
        vertices, indices, position = mesh_tuple[0], mesh_tuple[1], mesh_tuple[2]
        wv_name = mesh_tuple[3] if len(mesh_tuple) > 3 else f"Mesh{i}"
        polys_name = mesh_tuple[4] if len(mesh_tuple) > 4 else ""

        if len(vertices) == 0:
            continue

        # Transform vertices to world space (same as combine_meshes)
        transformed = vertices.copy()
        transformed[:, 0] = vertices[:, 0] + position.x + CHUNK_HALF_SIZE  # X
        transformed[:, 1] = vertices[:, 2] + position.z  # Z (height) -> glTF Y
        transformed[:, 2] = vertices[:, 1] + position.y + CHUNK_HALF_SIZE  # Y -> glTF Z

        # Create binary data
        vertices_bin = transformed.astype(np.float32).tobytes()
        indices_bin = indices.astype(np.uint32).tobytes()

        # Calculate bounds
        v_min = transformed.min(axis=0).tolist()
        v_max = transformed.max(axis=0).tolist()

        # Add buffer views
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": current_byte_offset,
                "byteLength": len(vertices_bin),
            }
        )
        vert_bv_idx = len(buffer_views) - 1
        current_byte_offset += len(vertices_bin)

        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": current_byte_offset,
                "byteLength": len(indices_bin),
            }
        )
        idx_bv_idx = len(buffer_views) - 1
        current_byte_offset += len(indices_bin)

        # Add accessors
        accessors.append(
            {
                "bufferView": vert_bv_idx,
                "componentType": 5126,  # FLOAT
                "count": len(transformed),
                "type": "VEC3",
                "min": v_min,
                "max": v_max,
            }
        )
        pos_accessor = len(accessors) - 1

        accessors.append(
            {
                "bufferView": idx_bv_idx,
                "componentType": 5125,  # UNSIGNED_INT
                "count": len(indices),
                "type": "SCALAR",
            }
        )
        idx_accessor = len(accessors) - 1

        # Add mesh with name including WaterVolume and Polys info
        mesh_name = f"{wv_name}|{polys_name}"
        meshes.append(
            {
                "name": mesh_name,
                "primitives": [
                    {
                        "attributes": {"POSITION": pos_accessor},
                        "indices": idx_accessor,
                        "material": 0,
                    }
                ],
            }
        )

        # Add node
        nodes.append({"mesh": len(meshes) - 1, "name": mesh_name})

        # Accumulate buffer data
        all_buffer_data += vertices_bin + indices_bin

    if not nodes:
        return None

    # Create scene with all nodes
    gltf = {
        "asset": {"version": "2.0", "generator": "Vanguard BSP Extractor"},
        "scene": 0,
        "scenes": [{"name": scene_name, "nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": [
            {
                "name": "WaterMaterial",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.2, 0.5, 0.8, 0.7],  # Blue-ish water color
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.3,
                },
                "doubleSided": True,
                "alphaMode": "BLEND",
            }
        ],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [
            {
                "uri": f"data:application/octet-stream;base64,{base64.b64encode(all_buffer_data).decode()}",
                "byteLength": len(all_buffer_data),
            }
        ],
    }

    return gltf


def combine_meshes(
    mesh_list: List[Tuple],
) -> Tuple[np.ndarray, np.ndarray]:
    """Combine multiple meshes with transforms into one.

    mesh_list: List of (vertices, indices, position, ...) tuples (extra fields ignored)
    """
    all_vertices = []
    all_indices = []
    vertex_offset = 0

    # Coordinate transform constants
    CHUNK_HALF_SIZE = 100000

    for mesh_tuple in mesh_list:
        vertices, indices, position = mesh_tuple[0], mesh_tuple[1], mesh_tuple[2]
        if len(vertices) == 0:
            continue

        # Transform vertices to world space
        # Polys vertices are in local coords centered at origin
        # Position is the CompoundObject world position (X, Y, Z where Z is height)
        # For glTF: swap Y/Z (glTF is Y-up)
        transformed = vertices.copy()

        # Apply local-to-world transform:
        # Local X -> World X, Local Y -> World Y, Local Z -> World Z (height)
        # Then swap for glTF: World X -> glTF X, World Z -> glTF Y, World Y -> glTF Z
        transformed[:, 0] = vertices[:, 0] + position.x + CHUNK_HALF_SIZE  # X
        transformed[:, 1] = vertices[:, 2] + position.z  # Z (height) -> glTF Y
        transformed[:, 2] = vertices[:, 1] + position.y + CHUNK_HALF_SIZE  # Y -> glTF Z

        all_vertices.append(transformed)
        all_indices.append(indices + vertex_offset)
        vertex_offset += len(vertices)

    if not all_vertices:
        return np.array([], dtype=np.float32), np.array([], dtype=np.uint32)

    return np.vstack(all_vertices), np.concatenate(all_indices)


# =============================================================================
# Main Extraction
# =============================================================================


def extract_bsp_from_chunk(vgr_path: str, output_path: str) -> int:
    """Extract all BSP geometry from a chunk file.

    Returns number of models extracted.
    """
    print(f"Loading package: {vgr_path}")
    pkg = UE2Package(vgr_path)
    parser = BSPParser(pkg)

    print(f"  Version: {pkg.version}")
    print(f"  Exports: {len(pkg.exports)}")

    # Find Model exports
    model_exports = pkg.get_exports_by_class("Model")
    print(f"  Model objects: {len(model_exports)}")

    # Find Polys exports
    polys_exports = pkg.get_exports_by_class("Polys")
    print(f"  Polys objects: {len(polys_exports)}")

    # Find WaterVolume exports - these contain world positions for Polys
    water_exports = pkg.get_exports_by_class("WaterVolume")
    print(f"  WaterVolumes: {len(water_exports)}")

    all_meshes = []

    # Build lookup tables for tracing references
    model_by_idx = {exp["index"]: exp for exp in model_exports}
    polys_by_idx = {exp["index"]: exp for exp in polys_exports}

    # Polys97 is the giant ocean volume - export separately
    polys97_export = next(
        (e for e in polys_exports if e["object_name"] == "Polys97"), None
    )

    # Parse Polys geometry FIRST (need extents to select best Polys per Model)
    print("\n  Parsing Polys geometry...")
    polys_geometry = {}
    polys_extents = {}  # Store XY extent for selection heuristic
    for exp in polys_exports:
        if exp["object_name"] in ("UPolys", "Polys97"):
            continue
        polys = parser.parse_polys(exp)
        if polys and polys.polygons:
            vertices, indices = parser.polys_to_triangles(polys)
            if len(vertices) > 0:
                polys_geometry[exp["object_name"]] = (vertices, indices)
                # Calculate XY extent for selection
                xs = vertices[:, 0]
                ys = vertices[:, 1]
                extent_x = float(xs.max() - xs.min())
                extent_y = float(ys.max() - ys.min())
                polys_extents[exp["object_name"]] = extent_x * extent_y
                print(
                    f"    {exp['object_name']}: {len(polys.polygons)} polys -> {len(vertices)} verts"
                )

    print(f"    Total Polys parsed: {len(polys_geometry)}")

    # Build Model -> Polys mapping by scanning for Polys references
    # Filter out UPolys (index 6) and Polys97 (ocean), take the LAST valid ref
    print("\n  Building Model -> Polys mapping...")
    model_to_all_polys = {}
    model_to_polys_ref = {}

    # Find UPolys and Polys97 indices to exclude
    upolys_idx = next(
        (e["index"] for e in polys_exports if e["object_name"] == "UPolys"), None
    )
    polys97_idx = next(
        (e["index"] for e in polys_exports if e["object_name"] == "Polys97"), None
    )
    exclude_polys = {upolys_idx, polys97_idx} - {None}

    for model_idx, model_exp in model_by_idx.items():
        model_data = pkg.get_export_data(model_exp)
        if not model_data:
            continue

        # Scan for Polys references, excluding UPolys and Polys97
        valid_polys_refs = []
        offset = 0
        while offset < len(model_data) - 1:
            val, new_offset = read_compact_index(model_data, offset)
            if val is None:
                break
            if val in polys_by_idx and val not in exclude_polys:
                polys_name = polys_by_idx[val]["object_name"]
                valid_polys_refs.append(polys_name)
            offset = new_offset

        # Take the LAST valid Polys reference (typically the actual geometry)
        if valid_polys_refs:
            polys_name = valid_polys_refs[-1]
            model_to_polys_ref[model_idx] = polys_name
            model_to_all_polys[model_idx] = valid_polys_refs

    print(f"    Models with valid Polys ref: {len(model_to_polys_ref)}")

    # Shared/template Models that contain generic Polys
    SHARED_MODELS = {25, 27}

    print(f"    Models with Polys references: {len(model_to_all_polys)}")

    # Build WaterVolume -> Polys mapping using NAMING CONVENTION
    # WaterVolumeN typically corresponds to ModelN (93%+ match rate)
    print("\n  Mapping WaterVolumes to Polys via naming convention...")

    # Build Model lookup by number suffix
    model_by_num = {}
    for model_idx, model_exp in model_by_idx.items():
        model_name = model_exp["object_name"]
        model_num = "".join(filter(str.isdigit, model_name))
        if model_num:
            model_by_num[model_num] = model_idx

    matched = 0
    matched_by_name = 0
    all_meshes = []

    for exp in water_exports:
        data = pkg.get_export_data(exp)
        if not data or len(data) < 13:
            continue

        # Get WaterVolume position
        pos = parse_compound_object_position(pkg, exp)
        if not pos:
            continue

        wv_name = exp["object_name"]
        wv_num = "".join(filter(str.isdigit, wv_name))

        polys_name = None
        target_model = None

        # Strategy 1: Use naming convention (WaterVolumeN -> ModelN)
        if wv_num and wv_num in model_by_num:
            target_model = model_by_num[wv_num]
            if target_model in model_to_polys_ref:
                polys_name = model_to_polys_ref[target_model]
                matched_by_name += 1

        # Strategy 2: Fallback - find nearest Model by export index
        if not polys_name:
            wv_idx = exp["index"]
            # Find closest Model that has a valid Polys ref
            closest_model = None
            closest_dist = float("inf")
            for model_idx in model_to_polys_ref.keys():
                dist = abs(model_idx - wv_idx)
                if dist < closest_dist:
                    closest_dist = dist
                    closest_model = model_idx

            if closest_model:
                polys_name = model_to_polys_ref[closest_model]

        if not polys_name or polys_name not in polys_geometry:
            continue

        vertices, indices = polys_geometry[polys_name]
        all_meshes.append((vertices, indices, pos, wv_name, polys_name))
        matched += 1

    print(f"    Matched WaterVolumes: {matched} / {len(water_exports)}")
    print(f"    Matched by naming convention: {matched_by_name}")

    # Skip Model parsing for now (format issues)
    # print("\n  Parsing Model objects...")
    # for exp in model_exports:
    #     model = parser.parse_model(exp)
    #     if model:
    #         vertices, indices = parser.model_to_triangles(model)
    #         if len(vertices) > 0:
    #             print(
    #                 f"    {exp['object_name']}: {len(model.nodes)} nodes, {len(model.points)} points -> {len(vertices)} verts"
    #             )
    #             all_meshes.append((vertices, indices, Vector(0, 0, 0)))

    if not all_meshes:
        print("\n  No BSP geometry found!")
        return 0

    # Create glTF with separate named meshes for click identification
    print(f"\n  Creating glTF with {len(all_meshes)} separate meshes...")
    chunk_name = Path(vgr_path).stem
    gltf = create_gltf_multi_mesh(all_meshes, f"BSP_{chunk_name}")

    if gltf:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(gltf, f)
        print(f"\n  Saved: {output_path}")

    # Export Polys97 (ocean volume) separately if it exists
    if polys97_export:
        print("\n  Exporting Polys97 (ocean volume) separately...")
        polys97 = parser.parse_polys(polys97_export)
        if polys97 and polys97.polygons:
            # Use larger max_span for ocean volume (it's huge)
            vertices, indices = parser.polys_to_triangles(polys97, max_span=2000000)
            if len(vertices) > 0:
                # Place at origin with CHUNK_HALF_SIZE offset
                ocean_meshes = [(vertices, indices, Vector(0, 0, 0))]
                ocean_verts, ocean_indices = combine_meshes(ocean_meshes)
                ocean_gltf = create_gltf(
                    ocean_verts, ocean_indices, f"Ocean_{chunk_name}"
                )
                if ocean_gltf:
                    ocean_path = output_path.replace("_bsp.gltf", "_ocean.gltf")
                    with open(ocean_path, "w") as f:
                        json.dump(ocean_gltf, f)
                    print(f"    Saved: {ocean_path}")
                    print(
                        f"    Polys97: {len(polys97.polygons)} polys -> {len(vertices)} verts"
                    )

    return len(all_meshes)


def main():
    # Default chunk
    chunk_x = -25
    chunk_y = 26

    if len(sys.argv) >= 3:
        chunk_x = int(sys.argv[1])
        chunk_y = int(sys.argv[2])

    # Build paths
    x_str = f"n{abs(chunk_x)}" if chunk_x < 0 else str(chunk_x)
    y_str = f"n{abs(chunk_y)}" if chunk_y < 0 else str(chunk_y)
    chunk_name = f"chunk_{x_str}_{y_str}"

    vgr_path = (
        f"/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Maps/{chunk_name}.vgr"
    )
    output_path = f"output/terrain_grid/{chunk_name}_bsp.gltf"

    if not os.path.exists(vgr_path):
        print(f"ERROR: Chunk file not found: {vgr_path}")
        return 1

    print(f"Extracting BSP from chunk ({chunk_x}, {chunk_y})...")
    count = extract_bsp_from_chunk(vgr_path, output_path)

    if count > 0:
        print(f"\nDone! Extracted {count} BSP meshes")
    else:
        print("\nNo BSP geometry extracted")

    return 0


if __name__ == "__main__":
    sys.exit(main())
