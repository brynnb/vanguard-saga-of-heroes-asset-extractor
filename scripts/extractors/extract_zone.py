#!/usr/bin/env python3
"""
Extract zone data from Vanguard .vgr files.
Extracts terrain, placed objects (CompoundObjects), and generates a combined scene.
"""

import struct
import json
import os
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass


@dataclass
class PlacedObject:
    """A placed object in the zone (CompoundObject)."""

    name: str
    x: float
    y: float
    z: float
    # TODO: rotation, scale, mesh reference


@dataclass
class ZoneData:
    """Extracted zone data."""

    name: str
    placed_objects: List[PlacedObject]
    terrain_texture: Optional[str]
    bounds: Tuple[
        float, float, float, float, float, float
    ]  # min_x, min_y, min_z, max_x, max_y, max_z


class ZoneExtractor:
    """Extract zone data from Vanguard .vgr files."""

    def __init__(self, zone_path: str):
        self.zone_path = zone_path
        self.zone_name = Path(zone_path).stem

        with open(zone_path, "rb") as f:
            self.data = f.read()

        self._parse_package()

    def _read_compact_index(self, pos: int) -> Tuple[int, int]:
        """Read a compact index and return (value, new_position)."""
        b0 = self.data[pos]
        pos += 1
        negative = b0 & 0x80
        value = b0 & 0x3F

        if b0 & 0x40:
            b1 = self.data[pos]
            pos += 1
            value |= (b1 & 0x7F) << 6
            if b1 & 0x80:
                b2 = self.data[pos]
                pos += 1
                value |= (b2 & 0x7F) << 13
                if b2 & 0x80:
                    b3 = self.data[pos]
                    pos += 1
                    value |= (b3 & 0x7F) << 20
                    if b3 & 0x80:
                        b4 = self.data[pos]
                        pos += 1
                        value |= b4 << 27

        return (-value if negative else value, pos)

    def _read_fstring(self, pos: int) -> Tuple[str, int]:
        """Read a length-prefixed string."""
        length, pos = self._read_compact_index(pos)
        if length < 0:
            length = -length
            result = (
                self.data[pos : pos + length * 2]
                .decode("utf-16-le", errors="replace")
                .rstrip("\x00")
            )
            pos += length * 2
        else:
            result = (
                self.data[pos : pos + length]
                .decode("latin-1", errors="replace")
                .rstrip("\x00")
            )
            pos += length
        return result, pos

    def _parse_package(self):
        """Parse the UE2 package header and tables."""
        # Header
        self.signature = struct.unpack("<I", self.data[0:4])[0]
        self.version = struct.unpack("<H", self.data[4:6])[0]
        self.licensee = struct.unpack("<H", self.data[6:8])[0]
        self.package_flags = struct.unpack("<I", self.data[8:12])[0]

        self.name_count = struct.unpack("<I", self.data[12:16])[0]
        self.name_offset = struct.unpack("<I", self.data[16:20])[0]
        self.export_count = struct.unpack("<I", self.data[20:24])[0]
        self.export_offset = struct.unpack("<I", self.data[24:28])[0]
        self.import_count = struct.unpack("<I", self.data[28:32])[0]
        self.import_offset = struct.unpack("<I", self.data[32:36])[0]

        # Parse name table
        self.names = []
        pos = self.name_offset
        for _ in range(self.name_count):
            name, pos = self._read_fstring(pos)
            flags = struct.unpack("<I", self.data[pos : pos + 4])[0]
            pos += 4
            self.names.append(name)

        # Parse import table
        self.imports = []
        pos = self.import_offset
        for _ in range(self.import_count):
            class_package, pos = self._read_compact_index(pos)
            class_name, pos = self._read_compact_index(pos)
            package = struct.unpack("<i", self.data[pos : pos + 4])[0]
            pos += 4
            object_name, pos = self._read_compact_index(pos)

            self.imports.append(
                {
                    "class_name": (
                        self.names[class_name]
                        if 0 <= class_name < len(self.names)
                        else ""
                    ),
                    "object_name": (
                        self.names[object_name]
                        if 0 <= object_name < len(self.names)
                        else ""
                    ),
                }
            )

        # Parse export table
        self.exports = []
        pos = self.export_offset
        for _ in range(self.export_count):
            class_index, pos = self._read_compact_index(pos)
            super_index, pos = self._read_compact_index(pos)
            package = struct.unpack("<i", self.data[pos : pos + 4])[0]
            pos += 4
            object_name, pos = self._read_compact_index(pos)
            object_flags = struct.unpack("<I", self.data[pos : pos + 4])[0]
            pos += 4
            serial_size, pos = self._read_compact_index(pos)
            serial_offset = 0
            if serial_size > 0:
                serial_offset, pos = self._read_compact_index(pos)

            # Get class name
            class_name = ""
            if class_index < 0:
                import_idx = -class_index - 1
                if import_idx < len(self.imports):
                    class_name = self.imports[import_idx]["object_name"]

            self.exports.append(
                {
                    "class_name": class_name,
                    "object_name": (
                        self.names[object_name]
                        if 0 <= object_name < len(self.names)
                        else ""
                    ),
                    "serial_size": serial_size,
                    "serial_offset": serial_offset,
                }
            )

    def _get_exports_by_class(self, class_name: str) -> List[Dict]:
        """Get all exports of a specific class."""
        return [e for e in self.exports if e["class_name"] == class_name]

    def _read_export_data(self, export: Dict) -> bytes:
        """Read the serialized data for an export."""
        return self.data[
            export["serial_offset"] : export["serial_offset"] + export["serial_size"]
        ]

    def _extract_compound_object_position(
        self, obj_data: bytes
    ) -> Optional[Tuple[float, float, float]]:
        """Extract position from CompoundObject data."""
        # Find the 0x0b marker which precedes position data
        for i in range(40, min(len(obj_data) - 12, 70)):
            if obj_data[i] == 0x0B:
                if i + 13 <= len(obj_data):
                    x = struct.unpack("<f", obj_data[i + 1 : i + 5])[0]
                    y = struct.unpack("<f", obj_data[i + 5 : i + 9])[0]
                    z = struct.unpack("<f", obj_data[i + 9 : i + 13])[0]

                    # Validate
                    if all(not (v != v) and abs(v) < 500000 for v in [x, y, z]):
                        return (x, y, z)
        return None

    def extract(self) -> ZoneData:
        """Extract all zone data."""
        placed_objects = []

        # Extract CompoundObjects
        compound_objects = self._get_exports_by_class("CompoundObject")
        for co in compound_objects:
            obj_data = self._read_export_data(co)
            pos = self._extract_compound_object_position(obj_data)
            if pos:
                placed_objects.append(
                    PlacedObject(name=co["object_name"], x=pos[0], y=pos[1], z=pos[2])
                )

        # Calculate bounds
        if placed_objects:
            min_x = min(o.x for o in placed_objects)
            min_y = min(o.y for o in placed_objects)
            min_z = min(o.z for o in placed_objects)
            max_x = max(o.x for o in placed_objects)
            max_y = max(o.y for o in placed_objects)
            max_z = max(o.z for o in placed_objects)
            bounds = (min_x, min_y, min_z, max_x, max_y, max_z)
        else:
            bounds = (0, 0, 0, 0, 0, 0)

        # Find terrain texture
        terrain_texture = None
        texture_exports = self._get_exports_by_class("Texture")
        for tex in texture_exports:
            if "baseColor" in tex["object_name"]:
                terrain_texture = tex["object_name"]
                break

        return ZoneData(
            name=self.zone_name,
            placed_objects=placed_objects,
            terrain_texture=terrain_texture,
            bounds=bounds,
        )

    def export_json(self, output_path: str):
        """Export zone data to JSON."""
        zone_data = self.extract()

        data = {
            "name": zone_data.name,
            "bounds": {
                "min": list(zone_data.bounds[:3]),
                "max": list(zone_data.bounds[3:]),
            },
            "terrain_texture": zone_data.terrain_texture,
            "placed_objects": [
                {"name": obj.name, "position": [obj.x, obj.y, obj.z]}
                for obj in zone_data.placed_objects
            ],
        }

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2)

        print(f"Exported zone data to {output_path}")
        print(f"  Placed objects: {len(zone_data.placed_objects)}")
        print(f"  Bounds: {zone_data.bounds}")

        return data


def main():
    import sys

    if len(sys.argv) < 2:
        zone_path = (
            "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Maps/chunk_n10_n10.vgr"
        )
    else:
        zone_path = sys.argv[1]

    output_path = (
        sys.argv[2]
        if len(sys.argv) > 2
        else f"output/zones/{Path(zone_path).stem}/zone_data.json"
    )

    print(f"Extracting zone: {zone_path}")

    extractor = ZoneExtractor(zone_path)
    extractor.export_json(output_path)


if __name__ == "__main__":
    main()
