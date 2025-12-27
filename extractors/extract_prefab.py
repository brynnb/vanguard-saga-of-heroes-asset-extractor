#!/usr/bin/env python3
"""
Vanguard Prefab Resolution Script

Parses chunk data to resolve CompoundObject prefabs to StaticMesh exports.
"""

import os
import struct
from typing import Dict, List, Optional, Tuple


class VanguardChunkParser:
    """Parser for Vanguard chunk data containing prefabs."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.objects: List[Dict] = []

    def read_int32(self) -> int:
        val = struct.unpack("<i", self.data[self.pos : self.pos + 4])[0]
        self.pos += 4
        return val

    def read_float(self) -> float:
        val = struct.unpack("<f", self.data[self.pos : self.pos + 4])[0]
        self.pos += 4
        return val

    def read_compact_index(self) -> int:
        """Read UE2 compact index encoding."""
        b = self.data[self.pos]
        self.pos += 1
        if b & 0x80:
            if b & 0x40:
                b2 = self.data[self.pos]
                b3 = self.data[self.pos + 1]
                b4 = self.data[self.pos + 2]
                self.pos += 3
                return ((b << 24) | (b2 << 16) | (b3 << 8) | b4) & 0x3FFFFFFF
            else:
                b2 = self.data[self.pos]
                self.pos += 1
                return ((b << 8) | b2) & 0x3FFF
        else:
            return b & 0x7F

    def parse_chunk(self) -> List[Dict]:
        """Parse chunk data and extract objects."""
        # Chunk format: sequence of objects
        # Each object: class_ref (compact index), padding (8 bytes), properties, position (12 bytes)

        while self.pos < len(self.data) - 20:  # Minimum object size
            obj_start = self.pos
            try:
                # Read class reference
                class_ref = self.read_compact_index()

                # Skip padding
                self.pos += 8

                # Read properties until 'None'
                properties = {}
                while self.pos < len(self.data) - 4:
                    prop_name_idx = self.read_compact_index()
                    if prop_name_idx < 0 or prop_name_idx >= 10000:  # Arbitrary limit
                        break
                    # For simplicity, assume name index maps to known names
                    # In real implementation, need name table
                    prop_name = f"Property{prop_name_idx}"

                    # Read property value (simplified)
                    prop_value = self.read_int32()
                    properties[prop_name] = prop_value

                    # Check for 'None' (name index 0)
                    if prop_name_idx == 0:
                        break

                # Read position
                x = self.read_float()
                y = self.read_float()
                z = self.read_float()
                position = (x, y, z)

                obj = {
                    "class_ref": class_ref,
                    "properties": properties,
                    "position": position,
                }
                self.objects.append(obj)

            except Exception as e:
                print(f"Error parsing object at pos {obj_start}: {e}")
                break

        return self.objects


def resolve_prefab(prefab_name: str, prefab_package: str) -> Optional[str]:
    """Resolve prefab to StaticMesh export name."""
    # Placeholder implementation
    # In real implementation, load prefab definition from package or .sgo
    # Map PrefabName to StaticMesh export

    # Example mapping (would be loaded from actual data)
    prefab_mappings = {"Tower01": "Ra3_P1_C1_Towers_mesh.StaticMesh01"}

    return prefab_mappings.get(prefab_name)


def extract_prefab_meshes(chunk_data: bytes) -> List[Dict]:
    """Extract StaticMesh references from chunk prefabs."""
    parser = VanguardChunkParser(chunk_data)
    objects = parser.parse_chunk()

    mesh_refs = []
    for obj in objects:
        if obj["class_ref"] == -2:  # CompoundObject class
            prefab_name = obj["properties"].get("PrefabName")
            prefab_package = obj["properties"].get("PrefabPackageName")

            if prefab_name and prefab_package:
                mesh_export = resolve_prefab(prefab_name, prefab_package)
                if mesh_export:
                    mesh_refs.append(
                        {"mesh_export": mesh_export, "position": obj["position"]}
                    )

    return mesh_refs


if __name__ == "__main__":
    # Example usage
    # chunk_path = "path/to/chunk.vgr"
    # with open(chunk_path, 'rb') as f:
    #     chunk_data = f.read()
    #
    # meshes = extract_prefab_meshes(chunk_data)
    # for mesh in meshes:
    #     print(f"Mesh: {mesh['mesh_export']} at {mesh['position']}")

    print("Prefab resolution script ready. Provide chunk data to process.")
