#!/usr/bin/env python3
"""
Decode ITEMS UEM files to map attachment_index → actual mesh references.

ITEMS UEM exports are ~1KB metadata templates containing NO mesh geometry.
They contain chains of internal cross-references:
  NPCHUMAN_M_253000 → Item Components → [NPCHUMAN_M_261014] (export)
  NPCHUMAN_M_261014 → Skins → [some import or more exports]
  Eventually → import:npcHuman_M_clth_plainclothes_10_C_0

This script recursively follows those chains to resolve each attachment_index
to the actual _clth/_tool mesh names it references.

Produces:
  output/data/attachment_to_clth_meshes.json
"""

import os
import sys
import json
import struct
import glob

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, ROOT_DIR)

import config
from ue2.package import UE2Package
from scripts.lib.ue2_property_reader import BinaryReader

ASSETS = os.environ.get(
    "VANGUARD_ASSETS",
    os.environ.get("VANGUARD_ASSETS_PATH", config.ASSETS_PATH),
)
ITEMS_DIR = os.path.join(ASSETS, "Characters", "Meshes")
OUTPUT_PATH = os.path.join(ROOT_DIR, "output", "data", "attachment_to_clth_meshes.json")

# UE2 property type IDs
NAME_BoolProperty = 3
NAME_ObjectProperty = 5
NAME_StructProperty = 10
NAME_ArrayProperty = 9
NAME_ByteProperty = 1
NAME_IntProperty = 2
NAME_FloatProperty = 4
NAME_NameProperty = 6


def collect_object_refs(data, names):
    """Parse UE2 properties, return all ObjectProperty compact-index values + Array elements."""
    reader = BinaryReader(data)
    refs = []
    max_pos = len(data)

    def parse_props(end_pos):
        while reader.pos < end_pos - 1:
            try:
                name_idx = reader.read_compact_index()
                if name_idx < 0 or name_idx >= len(names):
                    return
                if names[name_idx].lower() == "none":
                    return

                info = reader.read_byte()
                is_array = (info & 0x80) != 0
                prop_type = info & 0x0F

                if prop_type == NAME_StructProperty:
                    reader.read_compact_index()  # struct name

                size_type = (info >> 4) & 7
                sz = {0: 1, 1: 2, 2: 4, 3: 12, 4: 16}
                if size_type in sz:
                    data_size = sz[size_type]
                elif size_type == 5:
                    data_size = reader.read_byte()
                elif size_type == 6:
                    data_size = reader.read_uint16()
                elif size_type == 7:
                    data_size = reader.read_int32()
                else:
                    data_size = 0

                if prop_type != NAME_BoolProperty and is_array:
                    b = reader.read_byte()
                    if b >= 128:
                        b2 = reader.read_byte()
                        if b & 0x40:
                            reader.read_byte()
                            reader.read_byte()

                if prop_type == NAME_BoolProperty:
                    continue

                if reader.pos + data_size > max_pos:
                    return

                prop_end = reader.pos + data_size

                if prop_type == NAME_ObjectProperty:
                    ref = reader.read_compact_index()
                    if ref != 0:
                        refs.append(ref)
                    reader.seek(prop_end)
                elif prop_type == NAME_ArrayProperty:
                    if data_size >= 1:
                        count = reader.read_compact_index()
                        for _ in range(min(count, 50)):
                            if reader.pos >= prop_end:
                                break
                            ref = reader.read_compact_index()
                            if ref != 0:
                                refs.append(ref)
                    reader.seek(prop_end)
                elif prop_type == NAME_StructProperty:
                    parse_props(prop_end)
                    reader.seek(prop_end)
                else:
                    reader.seek(prop_end)
            except (IndexError, struct.error):
                return

    parse_props(max_pos)
    return refs


def process_items_file(filepath, result_map, stats):
    """Process one ITEMS UEM file."""
    try:
        pkg = UE2Package(filepath)
    except Exception as e:
        stats["errors"].append(f"{os.path.basename(filepath)}: {e}")
        return

    stats["files_processed"] += 1

    # Build export index → data cache
    export_by_idx = {}
    for exp in pkg.exports:
        export_by_idx[exp["index"]] = exp

    def resolve_to_mesh_imports(exp, visited=None):
        """Recursively follow export refs to find mesh import names."""
        if visited is None:
            visited = set()
        if exp["index"] in visited:
            return []
        visited.add(exp["index"])

        data = pkg.get_export_data(exp)
        if not data or len(data) < 4:
            return []

        refs = collect_object_refs(data, pkg.names)
        mesh_names = []

        for ref in refs:
            if ref < 0:
                # Import ref — check if it's an EMFXMesh
                idx = -ref - 1
                if 0 <= idx < len(pkg.imports):
                    imp = pkg.imports[idx]
                    if imp["class_name"] == "EMFXMesh":
                        pkg_name = ""
                        if imp["package"] < 0:
                            pidx = -imp["package"] - 1
                            if pidx < len(pkg.imports):
                                pkg_name = pkg.imports[pidx]["object_name"]
                        mesh_names.append((imp["object_name"], pkg_name))
            elif ref > 0:
                # Internal export ref — recurse
                sub_exp = export_by_idx.get(ref)
                if sub_exp and sub_exp["class_name"] == "EMFXMesh":
                    mesh_names.extend(resolve_to_mesh_imports(sub_exp, visited))

        return mesh_names

    # Process each EMFXMesh export
    for exp in pkg.exports:
        if exp["class_name"] != "EMFXMesh":
            continue

        att_idx = None
        parts = exp["object_name"].rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            att_idx = parts[1]
        if att_idx is None:
            continue

        stats["exports_scanned"] += 1

        mesh_refs = resolve_to_mesh_imports(exp)

        if mesh_refs:
            # Deduplicate
            seen = set()
            unique = []
            for mesh_name, pkg_name in mesh_refs:
                if mesh_name not in seen:
                    seen.add(mesh_name)
                    unique.append({"mesh": mesh_name, "package": pkg_name})

            # Keep the version with more refs
            if att_idx in result_map:
                if len(unique) > len(result_map[att_idx]):
                    result_map[att_idx] = unique
            else:
                result_map[att_idx] = unique
            stats["resolved"] += 1
        else:
            stats["no_mesh_refs"] += 1


def main():
    items_files = sorted(set(glob.glob(os.path.join(ITEMS_DIR, "*ITEMS*.uem"))))
    print(f"Found {len(items_files)} ITEMS files")

    result_map = {}
    stats = {
        "files_processed": 0,
        "exports_scanned": 0,
        "resolved": 0,
        "no_mesh_refs": 0,
        "errors": [],
    }

    for filepath in items_files:
        prev = stats["resolved"]
        process_items_file(filepath, result_map, stats)
        new = stats["resolved"] - prev
        if new > 0:
            print(f"  {os.path.basename(filepath)}: +{new} ({stats['resolved']} total)")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result_map, f, separators=(",", ":"))

    print(f"\n=== Results ===")
    print(f"  Files: {stats['files_processed']}")
    print(f"  Exports scanned: {stats['exports_scanned']}")
    print(f"  Resolved: {stats['resolved']}")
    print(f"  No mesh refs: {stats['no_mesh_refs']}")
    print(f"  Unique attachment indices: {len(result_map)}")

    multi = sum(1 for v in result_map.values() if len(v) > 1)
    print(f"  Single-mesh entries: {len(result_map) - multi}")
    print(f"  Multi-mesh entries: {multi}")
    if result_map:
        print(f"  Max meshes/entry: {max(len(v) for v in result_map.values())}")

    if stats["errors"]:
        print(f"\n  Errors ({len(stats['errors'])}):")
        for e in stats["errors"][:10]:
            print(f"    {e}")


if __name__ == "__main__":
    main()
