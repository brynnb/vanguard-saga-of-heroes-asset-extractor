#!/usr/bin/env python3
"""
Native Python StaticMesh extractor for Vanguard: Saga of Heroes.

Based on UEViewer source code (UnMesh2.cpp/UnMesh2.h/UnObject.cpp) for Vanguard-specific
StaticMesh serialization format.

Key discoveries for Vanguard (Version 129/35):
- BoundingBox is 24 bytes (no IsValid flag)
- FVanguardSkin uses uint16 for material indices
- Faces are stored as inline pre-expanded triangles (84-byte records)
- Metadata is 28 bytes per triangle record
"""

import struct
import os
import sys
import json
import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import base64

# Add parent directory to path to allow importing ue2 package
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import shared UE2 utilities
from ue2 import Vector, UE2Package
from vanguard_mesh_lib import VanguardMeshParser

# Import config
import config

# Paths
MESHES_DIR = os.path.join(config.ASSETS_PATH, "Meshes")
OUTPUT_DIR = config.MESH_BUILDINGS_DIR


@dataclass
class StaticMeshVertex:
    pos: Vector
    normal: Vector


@dataclass
class MeshUV:
    u: float
    v: float


@dataclass
class StaticMeshSection:
    """Section of a static mesh (material group)."""
    first_index: int
    num_faces: int
    material_index: int


@dataclass
class Vertex:
    """Full vertex with position, normal, and UV."""
    pos: Vector
    normal: Vector
    uv: Tuple[float, float]


class StaticMeshPackageReader(UE2Package):
    """Wrapper around UE2Package with StaticMesh-specific helpers.
    
    Extends UE2Package to add convenience methods for StaticMesh extraction.
    """

    def __init__(self, filepath: str):
        super().__init__(filepath)
        print(f"  Signature: 0x9e2a83c1")
        print(f"  Version: {self.version}/{self.licensee}")
        print(f"  Name count: {len(self.names)} @ {self.name_offset}")
        print(f"  Export count: {len(self.exports)} @ {self.export_offset}")
        print(f"  Import count: {len(self.imports)} @ {self.import_offset}")

    def get_class_name(self, class_index: int) -> str:
        """Get class name from class index (can be negative for imports)."""
        if class_index < 0:
            idx = -class_index - 1
            if 0 <= idx < len(self.imports):
                return self.imports[idx]["object_name"]
        elif class_index > 0:
            idx = class_index - 1
            if 0 <= idx < len(self.exports):
                return self.exports[idx].get("object_name", "")
        return ""

    def get_staticmesh_exports(self) -> List[Dict]:
        """Get all StaticMesh exports."""
        meshes = []
        for exp in self.exports:
            class_name = self.get_class_name(exp["class_index"])
            if class_name == "StaticMesh":
                meshes.append(exp)
        return meshes


    def parse(self) -> Optional[Dict]:
        """Parse Vanguard StaticMesh data using centralized library."""
        try:
            mesh_data = VanguardMeshParser.parse(self.data, self.pkg.names, self.pkg.exports[0]['serial_offset']) # Simplified
            if not mesh_data['lods']:
                return None
            
            # Use LOD 0
            lod = mesh_data['lods'][0]
            vertices = [(v.position.x, v.position.y, v.position.z) for v in lod.vertices]
            
            # Construct indices (must be flat list of tuples for mesh_to_gltf)
            indices = []
            for i in range(0, len(lod.indices), 3):
                if i + 2 < len(lod.indices):
                    indices.append((lod.indices[i], lod.indices[i+1], lod.indices[i+2]))
                    
            return {
                "vertices": vertices,
                "indices": indices
            }

        except Exception as e:
            print(f"  Parse error: {e}")
            return None


def mesh_to_gltf(mesh_data, name):
    """Convert extracted mesh data to glTF format."""
    vertices = mesh_data.get("vertices", [])
    indices = mesh_data.get("indices", [])

    if not vertices or not indices:
        print(f"No mesh data to convert for {name}")
        return None

    # Convert vertices to numpy array
    positions = np.array(vertices, dtype=np.float32)

    # Convert indices, handling negative values by filtering out invalid triangles
    valid_indices = []
    max_vertex_idx = len(vertices) - 1
    for tri in indices:
        i1, i2, i3 = tri
        # Check if indices are within valid range; if not, skip this triangle
        if (
            (i1 >= 0 and i1 <= max_vertex_idx)
            and (i2 >= 0 and i2 <= max_vertex_idx)
            and (i3 >= 0 and i3 <= max_vertex_idx)
        ):
            valid_indices.extend([i1, i2, i3])
        else:
            print(
                f"Skipping invalid triangle with indices {i1}, {i2}, {i3} (max vertex index: {max_vertex_idx})"
            )

    if not valid_indices:
        print(f"No valid triangles after filtering for {name}")
        return None

    indices_arr = np.array(valid_indices, dtype=np.uint32)

    # Create glTF structure
    gltf_data = {
        "asset": {"version": "2.0", "generator": "Vanguard Mesh Extractor"},
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [
            {
                "primitives": [
                    {
                        "attributes": {"POSITION": 0},
                        "indices": 1,
                        "mode": 4,  # TRIANGLES
                    }
                ],
                "name": name,
            }
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,  # FLOAT
                "count": len(vertices),
                "type": "VEC3",
                "max": positions.max(axis=0).tolist(),
                "min": positions.min(axis=0).tolist(),
            },
            {
                "bufferView": 1,
                "componentType": 5125,  # UNSIGNED_INT
                "count": len(valid_indices),
                "type": "SCALAR",
            },
        ],
        "bufferViews": [
            {
                "buffer": 0,
                "byteLength": positions.nbytes,
                "byteStride": 12,
                "target": 34962,  # ARRAY_BUFFER
            },
            {
                "buffer": 0,
                "byteOffset": positions.nbytes,
                "byteLength": indices_arr.nbytes,
                "target": 34963,  # ELEMENT_ARRAY_BUFFER
            },
        ],
        "buffers": [
            {
                "byteLength": positions.nbytes + indices_arr.nbytes,
                "uri": f"data:application/octet-stream;base64,{base64.b64encode(positions.tobytes() + indices_arr.tobytes()).decode('utf-8')}",
            }
        ],
    }
    return gltf_data


class StaticMeshExporter:
    """Helper class to extract StaticMeshes from a package."""
    
    def __init__(self, pkg_path: str, output_dir: str):
        self.pkg_path = pkg_path
        self.output_dir = output_dir
        self.pkg = StaticMeshPackageReader(pkg_path)
        
    def export_all_meshes(self) -> int:
        """Extract all StaticMeshes from the package."""
        meshes = self.pkg.get_staticmesh_exports()
        print(f"  StaticMesh exports: {len(meshes)}")
        
        os.makedirs(self.output_dir, exist_ok=True)
        extracted = 0
        
        for exp in meshes:
            name = exp["object_name"]
            if self.export_mesh(name):
                extracted += 1
        return extracted
        
    def export_mesh(self, mesh_name: str) -> bool:
        """Extract a single mesh by name from the package."""
        # Find the export by name
        target_exp = None
        for exp in self.pkg.exports:
            if exp["object_name"] == mesh_name and self.pkg.get_class_name(exp["class_index"]) == "StaticMesh":
                target_exp = exp
                break
        
        if not target_exp:
            # Fallback: maybe the name in the package is different from what we expect
            # but usually it should match exactly.
            return False
            
        print(f"\n  Parsing: {mesh_name}")
        data = self.pkg.get_export_data(target_exp)
        
        try:
            mesh_data = VanguardMeshParser.parse(data, self.pkg.names, target_exp['serial_offset'])
            if mesh_data and mesh_data['lods']:
                # Flatten LOD 0 for GLTF
                lod = mesh_data['lods'][0]
                vertices = [(v.position.x, v.position.y, v.position.z) for v in lod.vertices]
                indices = []
                for i in range(0, len(lod.indices), 3):
                    if i + 2 < len(lod.indices):
                        indices.append((lod.indices[i], lod.indices[i+1], lod.indices[i+2]))
                
                final_data = {"vertices": vertices, "indices": indices}
                result = mesh_to_gltf(final_data, mesh_name)
                if result:
                    gltf_path = os.path.join(self.output_dir, f"{mesh_name}.gltf")
                    with open(gltf_path, "w") as f:
                        json.dump(result, f, indent=2)
                    print(f"    Saved: {gltf_path}")
                    return True
        except Exception as e:
            print(f"    Error parsing {mesh_name}: {e}")
            
        return False


def extract_package(pkg_path: str, output_dir: str) -> int:
    """Extract all StaticMeshes from a package."""
    print(f"Loading package: {os.path.basename(pkg_path)}")

    try:
        exporter = StaticMeshExporter(pkg_path, output_dir)
        print(f"  Version: {exporter.pkg.version}/{exporter.pkg.licensee}")
        print(f"  Exports: {len(exporter.pkg.exports)}")
        return exporter.export_all_meshes()
    except Exception as e:
        print(f"  Failed to process package: {e}")
        return 0


def main():
    import sys

    if len(sys.argv) > 1:
        pkg_path = sys.argv[1]
    else:
        # Default: test with Ra3_P1_C1_Decor_mesh.usx
        pkg_path = os.path.join(MESHES_DIR, "Ra3_P1_C1_Decor_mesh.usx")

    output_dir = OUTPUT_DIR
    extracted = extract_package(pkg_path, output_dir)
    print(f"\nExtracted {extracted} meshes")


if __name__ == "__main__":
    main()
