#!/usr/bin/env python3
"""
Parse Vanguard zone files (.vgr) to extract terrain and object placement data.
These are Unreal Engine 2 packages with custom Vanguard classes.
"""

import struct
import json
import os
from pathlib import Path
from typing import Dict, List, Tuple, Any


class UE2PackageReader:
    """Reader for Unreal Engine 2 package files."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        with open(filepath, "rb") as f:
            self.data = f.read()
        self.pos = 0
        self.names = []
        self.imports = []
        self.exports = []
        self.parse_header()

    def read_bytes(self, count: int) -> bytes:
        result = self.data[self.pos : self.pos + count]
        self.pos += count
        return result

    def read_int32(self) -> int:
        return struct.unpack("<i", self.read_bytes(4))[0]

    def read_uint32(self) -> int:
        return struct.unpack("<I", self.read_bytes(4))[0]

    def read_uint16(self) -> int:
        return struct.unpack("<H", self.read_bytes(2))[0]

    def read_float(self) -> float:
        return struct.unpack("<f", self.read_bytes(4))[0]

    def read_compact_index(self) -> int:
        """Read a compact index (variable-length integer)."""
        b0 = self.data[self.pos]
        self.pos += 1

        negative = b0 & 0x80
        value = b0 & 0x3F

        if b0 & 0x40:
            b1 = self.data[self.pos]
            self.pos += 1
            value |= (b1 & 0x7F) << 6

            if b1 & 0x80:
                b2 = self.data[self.pos]
                self.pos += 1
                value |= (b2 & 0x7F) << 13

                if b2 & 0x80:
                    b3 = self.data[self.pos]
                    self.pos += 1
                    value |= (b3 & 0x7F) << 20

                    if b3 & 0x80:
                        b4 = self.data[self.pos]
                        self.pos += 1
                        value |= b4 << 27

        return -value if negative else value

    def read_fstring(self) -> str:
        """Read a length-prefixed string."""
        length = self.read_compact_index()
        if length < 0:
            # Unicode string
            length = -length
            result = self.read_bytes(length * 2).decode("utf-16-le").rstrip("\x00")
        else:
            result = self.read_bytes(length).decode("latin-1").rstrip("\x00")
        return result

    def parse_header(self):
        """Parse the package header."""
        self.pos = 0

        # Signature
        self.signature = self.read_uint32()
        if self.signature != 0x9E2A83C1:
            raise ValueError(f"Invalid UE2 package signature: {hex(self.signature)}")

        # Version
        self.version = self.read_uint16()
        self.licensee = self.read_uint16()

        # Package flags
        self.package_flags = self.read_uint32()

        # Table counts and offsets
        self.name_count = self.read_uint32()
        self.name_offset = self.read_uint32()
        self.export_count = self.read_uint32()
        self.export_offset = self.read_uint32()
        self.import_count = self.read_uint32()
        self.import_offset = self.read_uint32()

        print(f"Package: {self.filepath}")
        print(f"  Version: {self.version}/{self.licensee}")
        print(f"  Names: {self.name_count} at {hex(self.name_offset)}")
        print(f"  Exports: {self.export_count} at {hex(self.export_offset)}")
        print(f"  Imports: {self.import_count} at {hex(self.import_offset)}")

        # Parse name table
        self.parse_names()

        # Parse import table
        self.parse_imports()

        # Parse export table
        self.parse_exports()

    def parse_names(self):
        """Parse the name table."""
        self.pos = self.name_offset
        self.names = []

        for i in range(self.name_count):
            name = self.read_fstring()
            flags = self.read_uint32()
            self.names.append(name)

    def parse_imports(self):
        """Parse the import table."""
        self.pos = self.import_offset
        self.imports = []

        for i in range(self.import_count):
            class_package = self.read_compact_index()
            class_name = self.read_compact_index()
            package = self.read_int32()
            object_name = self.read_compact_index()

            self.imports.append(
                {
                    "class_package": (
                        self.names[class_package]
                        if class_package < len(self.names)
                        else str(class_package)
                    ),
                    "class_name": (
                        self.names[class_name]
                        if class_name < len(self.names)
                        else str(class_name)
                    ),
                    "package": package,
                    "object_name": (
                        self.names[object_name]
                        if object_name < len(self.names)
                        else str(object_name)
                    ),
                }
            )

    def parse_exports(self):
        """Parse the export table."""
        self.pos = self.export_offset
        self.exports = []

        for i in range(self.export_count):
            class_index = self.read_compact_index()
            super_index = self.read_compact_index()
            package = self.read_int32()
            object_name = self.read_compact_index()
            object_flags = self.read_uint32()
            serial_size = self.read_compact_index()
            serial_offset = self.read_compact_index() if serial_size > 0 else 0

            # Get class name
            if class_index < 0:
                class_name = self.imports[-class_index - 1]["object_name"]
            elif class_index > 0:
                class_name = (
                    self.names[self.exports[class_index - 1]["object_name_idx"]]
                    if class_index <= len(self.exports)
                    else "Unknown"
                )
            else:
                class_name = "Class"

            self.exports.append(
                {
                    "class_index": class_index,
                    "class_name": class_name,
                    "super_index": super_index,
                    "package": package,
                    "object_name_idx": object_name,
                    "object_name": (
                        self.names[object_name]
                        if object_name < len(self.names)
                        else str(object_name)
                    ),
                    "object_flags": object_flags,
                    "serial_size": serial_size,
                    "serial_offset": serial_offset,
                }
            )

    def get_exports_by_class(self, class_name: str) -> List[Dict]:
        """Get all exports of a specific class."""
        return [e for e in self.exports if e["class_name"] == class_name]

    def read_export_data(self, export: Dict) -> bytes:
        """Read the raw data for an export."""
        if export["serial_size"] <= 0:
            return b""
        return self.data[
            export["serial_offset"] : export["serial_offset"] + export["serial_size"]
        ]

    def dump_info(self):
        """Dump package information."""
        class_counts = {}
        for e in self.exports:
            class_name = e["class_name"]
            class_counts[class_name] = class_counts.get(class_name, 0) + 1

        print("\nClass statistics:")
        for class_name, count in sorted(class_counts.items()):
            print(f"  {count:4d} {class_name}")

        return class_counts


def analyze_zone(zone_path: str):
    """Analyze a Vanguard zone file."""
    pkg = UE2PackageReader(zone_path)
    class_counts = pkg.dump_info()

    # Look for terrain-related exports
    terrain_info = pkg.get_exports_by_class("TerrainInfo")
    terrain_sectors = pkg.get_exports_by_class("TerrainSector")
    compound_objects = pkg.get_exports_by_class("CompoundObject")

    print(f"\nTerrain Info: {len(terrain_info)}")
    print(f"Terrain Sectors: {len(terrain_sectors)}")
    print(f"Compound Objects: {len(compound_objects)}")

    # Analyze compound objects (placed meshes) - these contain position/rotation data
    print("\n=== Compound Objects (Placed Meshes) ===")
    for i, obj in enumerate(compound_objects[:5]):  # First 5
        print(f"\n{obj['object_name']}:")
        print(
            f"  Offset: {hex(obj['serial_offset'])}, Size: {obj['serial_size']} bytes"
        )

        data = pkg.read_export_data(obj)
        if len(data) >= 12:
            # Try to find float values that might be position/rotation
            # UE2 objects typically have Location, Rotation, Scale properties
            print(f"  Raw (first 80 bytes): {data[:80].hex()}")

    return pkg


def extract_zone_info(zone_path: str) -> dict:
    """Extract zone information for rendering."""
    pkg = UE2PackageReader(zone_path)

    zone_info = {
        "name": Path(zone_path).stem,
        "terrain_sectors": len(pkg.get_exports_by_class("TerrainSector")),
        "compound_objects": [],
        "textures": [],
    }

    # Get texture exports
    for exp in pkg.get_exports_by_class("Texture"):
        zone_info["textures"].append(exp["object_name"])

    # Get compound objects
    for exp in pkg.get_exports_by_class("CompoundObject"):
        zone_info["compound_objects"].append(
            {
                "name": exp["object_name"],
                "offset": exp["serial_offset"],
                "size": exp["serial_size"],
            }
        )

    return zone_info


def main():
    import sys

    if len(sys.argv) < 2:
        zone_path = (
            "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Maps/chunk_n10_n10.vgr"
        )
    else:
        zone_path = sys.argv[1]

    analyze_zone(zone_path)


if __name__ == "__main__":
    main()
