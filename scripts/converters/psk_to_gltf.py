#!/usr/bin/env python3
"""
PSK/PSKX to glTF converter for Unreal Engine static meshes.
Converts ActorX PSK format to glTF 2.0 for WebGL rendering.
"""

import struct
import json
import base64
import sys
import os
from pathlib import Path
from typing import List, Tuple, NamedTuple


class Vector3(NamedTuple):
    x: float
    y: float
    z: float


class Vertex(NamedTuple):
    position: Vector3
    u: float
    v: float
    material_index: int


class Triangle(NamedTuple):
    v0: int
    v1: int
    v2: int
    material_index: int
    smoothing_group: int


class PSKMesh:
    def __init__(self):
        self.points: List[Vector3] = []
        self.wedges: List[Tuple[int, float, float, int]] = (
            []
        )  # point_index, u, v, material
        self.faces: List[Triangle] = []
        self.materials: List[str] = []


def read_chunk_header(f) -> Tuple[str, int, int, int]:
    """Read a PSK chunk header."""
    chunk_id = f.read(20).decode("ascii").rstrip("\x00")
    type_flag = struct.unpack("<I", f.read(4))[0]
    data_size = struct.unpack("<I", f.read(4))[0]
    data_count = struct.unpack("<I", f.read(4))[0]
    return chunk_id, type_flag, data_size, data_count


def parse_psk(filepath: str) -> PSKMesh:
    """Parse a PSK/PSKX file and extract mesh data."""
    mesh = PSKMesh()

    with open(filepath, "rb") as f:
        while True:
            header_data = f.read(32)
            if len(header_data) < 32:
                break

            # Parse header
            chunk_id = header_data[:20].decode("ascii").rstrip("\x00")
            type_flag = struct.unpack("<I", header_data[20:24])[0]
            data_size = struct.unpack("<I", header_data[24:28])[0]
            data_count = struct.unpack("<I", header_data[28:32])[0]

            chunk_data = f.read(data_size * data_count)

            if chunk_id == "ACTRHEAD":
                # Header chunk, skip
                pass
            elif chunk_id == "PNTS0000":
                # Points (vertices)
                for i in range(data_count):
                    offset = i * 12
                    x, y, z = struct.unpack("<fff", chunk_data[offset : offset + 12])
                    mesh.points.append(Vector3(x, y, z))
            elif chunk_id == "VTXW0000":
                # Wedges (vertex + UV) - format varies by version
                for i in range(data_count):
                    offset = i * data_size
                    if data_size == 16:
                        # PSKX format: point_index(4 bytes), padding(4), u(4), v(4)
                        # Or: point_index(4 bytes), padding(12) with UVs elsewhere
                        point_idx = struct.unpack(
                            "<I", chunk_data[offset : offset + 4]
                        )[0]
                        u = struct.unpack("<f", chunk_data[offset + 8 : offset + 12])[0]
                        v = struct.unpack("<f", chunk_data[offset + 12 : offset + 16])[
                            0
                        ]
                        mat_idx = 0
                    elif data_size == 12:
                        point_idx = struct.unpack(
                            "<H", chunk_data[offset : offset + 2]
                        )[0]
                        u, v = struct.unpack(
                            "<ff", chunk_data[offset + 2 : offset + 10]
                        )
                        mat_idx = (
                            chunk_data[offset + 10]
                            if len(chunk_data) > offset + 10
                            else 0
                        )
                    else:
                        # Try to parse anyway
                        point_idx = struct.unpack(
                            "<I", chunk_data[offset : offset + 4]
                        )[0]
                        u, v = 0.0, 0.0
                        mat_idx = 0
                    mesh.wedges.append((point_idx, u, v, mat_idx))
            elif chunk_id == "FACE0000":
                # Faces (triangles) - format: 3 wedge indices (2 bytes each) + mat index (1 byte) + aux (1 byte) + smoothing group (4 bytes)
                for i in range(data_count):
                    offset = i * data_size
                    if data_size == 12:
                        w0, w1, w2 = struct.unpack(
                            "<HHH", chunk_data[offset : offset + 6]
                        )
                        mat_idx = chunk_data[offset + 6]
                        smooth = struct.unpack(
                            "<I", chunk_data[offset + 8 : offset + 12]
                        )[0]
                    else:
                        # Extended format
                        w0, w1, w2 = struct.unpack(
                            "<HHH", chunk_data[offset : offset + 6]
                        )
                        mat_idx = (
                            chunk_data[offset + 6]
                            if len(chunk_data) > offset + 6
                            else 0
                        )
                        smooth = 0
                    mesh.faces.append(Triangle(w0, w1, w2, mat_idx, smooth))
            elif chunk_id == "MATT0000":
                # Materials
                for i in range(data_count):
                    offset = i * data_size
                    mat_name = (
                        chunk_data[offset : offset + 64]
                        .decode("ascii", errors="ignore")
                        .rstrip("\x00")
                    )
                    mesh.materials.append(mat_name)

    return mesh


def mesh_to_gltf(mesh: PSKMesh, name: str) -> dict:
    """Convert PSKMesh to glTF 2.0 format."""

    # Build vertex buffer with positions and UVs
    positions = []
    uvs = []
    indices = []

    # Create vertices from wedges
    for wedge in mesh.wedges:
        point_idx, u, v, _ = wedge
        if point_idx < len(mesh.points):
            pos = mesh.points[point_idx]
            # Convert from Unreal coordinate system (Z-up, left-handed) to glTF (Y-up, right-handed)
            positions.extend([pos.x, pos.z, -pos.y])
            uvs.extend([u, 1.0 - v])  # Flip V coordinate

    # Build index buffer from faces
    for face in mesh.faces:
        # Reverse winding order for right-handed coordinate system
        indices.extend([face.v0, face.v2, face.v1])

    if not positions or not indices:
        return None

    # Calculate bounding box
    min_pos = [float("inf")] * 3
    max_pos = [float("-inf")] * 3
    for i in range(0, len(positions), 3):
        for j in range(3):
            min_pos[j] = min(min_pos[j], positions[i + j])
            max_pos[j] = max(max_pos[j], positions[i + j])

    # Pack binary data
    position_bytes = struct.pack(f"<{len(positions)}f", *positions)
    uv_bytes = struct.pack(f"<{len(uvs)}f", *uvs)

    # Determine index type based on vertex count
    vertex_count = len(positions) // 3
    if vertex_count <= 65535:
        index_bytes = struct.pack(f"<{len(indices)}H", *indices)
        index_component_type = 5123  # UNSIGNED_SHORT
    else:
        index_bytes = struct.pack(f"<{len(indices)}I", *indices)
        index_component_type = 5125  # UNSIGNED_INT

    # Combine into single buffer
    buffer_data = position_bytes + uv_bytes + index_bytes
    buffer_uri = f"data:application/octet-stream;base64,{base64.b64encode(buffer_data).decode('ascii')}"

    gltf = {
        "asset": {"version": "2.0", "generator": "psk_to_gltf.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [
            {
                "name": name,
                "primitives": [
                    {
                        "attributes": {"POSITION": 0, "TEXCOORD_0": 1},
                        "indices": 2,
                        "mode": 4,  # TRIANGLES
                    }
                ],
            }
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,  # FLOAT
                "count": vertex_count,
                "type": "VEC3",
                "min": min_pos,
                "max": max_pos,
            },
            {
                "bufferView": 1,
                "componentType": 5126,  # FLOAT
                "count": vertex_count,
                "type": "VEC2",
            },
            {
                "bufferView": 2,
                "componentType": index_component_type,
                "count": len(indices),
                "type": "SCALAR",
            },
        ],
        "bufferViews": [
            {
                "buffer": 0,
                "byteOffset": 0,
                "byteLength": len(position_bytes),
                "target": 34962,  # ARRAY_BUFFER
            },
            {
                "buffer": 0,
                "byteOffset": len(position_bytes),
                "byteLength": len(uv_bytes),
                "target": 34962,  # ARRAY_BUFFER
            },
            {
                "buffer": 0,
                "byteOffset": len(position_bytes) + len(uv_bytes),
                "byteLength": len(index_bytes),
                "target": 34963,  # ELEMENT_ARRAY_BUFFER
            },
        ],
        "buffers": [{"uri": buffer_uri, "byteLength": len(buffer_data)}],
    }

    return gltf


def convert_psk_to_gltf(input_path: str, output_path: str = None) -> bool:
    """Convert a PSK/PSKX file to glTF."""
    try:
        mesh = parse_psk(input_path)

        if not mesh.points or not mesh.faces:
            print(f"Error: No mesh data found in {input_path}")
            return False

        name = Path(input_path).stem
        gltf = mesh_to_gltf(mesh, name)

        if gltf is None:
            print(f"Error: Failed to convert {input_path}")
            return False

        if output_path is None:
            output_path = str(Path(input_path).with_suffix(".gltf"))

        with open(output_path, "w") as f:
            json.dump(gltf, f, indent=2)

        print(f"Converted: {input_path} -> {output_path}")
        print(
            f"  Vertices: {len(mesh.points)}, Wedges: {len(mesh.wedges)}, Faces: {len(mesh.faces)}"
        )
        return True

    except Exception as e:
        print(f"Error converting {input_path}: {e}")
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: psk_to_gltf.py <input.psk> [output.gltf]")
        print("       psk_to_gltf.py <directory>  # Convert all PSK/PSKX files")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    if os.path.isdir(input_path):
        # Convert all PSK files in directory
        success = 0
        failed = 0
        for root, dirs, files in os.walk(input_path):
            for file in files:
                if file.lower().endswith((".psk", ".pskx")):
                    psk_path = os.path.join(root, file)
                    if convert_psk_to_gltf(psk_path):
                        success += 1
                    else:
                        failed += 1
        print(f"\nConverted {success} files, {failed} failed")
    else:
        if not convert_psk_to_gltf(input_path, output_path):
            sys.exit(1)


if __name__ == "__main__":
    main()
