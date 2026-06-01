#!/usr/bin/env python3
"""
Extract chunk metadata and placed objects from VGR files into the database.
Creates the necessary tables and populates them with extracted data.
"""

import sqlite3
import struct
import os
import sys
import json
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import time

# Add project root to path (go up 2 levels from scripts/extractors)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

try:
    import config
    DB_PATH = config.DB_PATH
    MAPS_DIR = os.path.join(config.ASSETS_PATH, "Maps")
except ImportError:
    DB_PATH = os.path.join(PROJECT_ROOT, "output", "data", "vanguard_data.db")
    MAPS_DIR = os.path.join(PROJECT_ROOT, "Vanguard EMU", "Assets", "Maps")

from ue2.properties import find_property_start, parse_properties


NAVIGATION_POINT_CLASSES = {
    "NavigationPoint",
    "PathNode",
    "PlayerStart",
    "SmallNavigationPoint",
}
PATH_NODE_CLASSES = {"PathNode"}


NAVIGATION_POINT_COLUMNS = {
    "nav_type": "TEXT NOT NULL DEFAULT 'NavigationPoint'",
    "rotation_pitch": "REAL",
    "rotation_yaw": "REAL",
    "rotation_roll": "REAL",
    "is_path_node": "INTEGER DEFAULT 0",
    "is_player_start": "INTEGER DEFAULT 0",
    "next_navigation_point_ref": "INTEGER",
    "path_list_json": "TEXT",
    "forced_paths_json": "TEXT",
    "proscribed_paths_json": "TEXT",
    "property_count": "INTEGER",
    "property_start": "INTEGER",
    "properties_json": "TEXT",
}


def ensure_navigation_point_columns(cursor):
    """Add newer navigation point columns when upgrading an existing database."""
    existing = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(navigation_points)").fetchall()
    }
    for column, definition in NAVIGATION_POINT_COLUMNS.items():
        if column not in existing:
            cursor.execute(
                f"ALTER TABLE navigation_points ADD COLUMN {column} {definition}"
            )


def ensure_path_nodes_view(cursor):
    """Keep path_nodes as a filtered compatibility view over navigation_points."""
    row = cursor.execute(
        """
        SELECT type FROM sqlite_master
        WHERE name = 'path_nodes' AND type IN ('table', 'view')
        """
    ).fetchone()

    if row and row[0] == "table":
        cursor.execute(
            """
            INSERT OR IGNORE INTO navigation_points (
                chunk_id, export_id, export_index, object_name, class_name,
                nav_type, tag, location_x, location_y, location_z, level_ref,
                region_text, region_hex, b_paths_changed, b_light_changed,
                is_path_node, is_player_start, serial_offset, serial_size
            )
            SELECT
                chunk_id, export_id, export_index, object_name, class_name,
                class_name, tag, location_x, location_y, location_z, level_ref,
                region_text, region_hex, b_paths_changed, b_light_changed,
                1, 0, serial_offset, serial_size
            FROM path_nodes
            """
        )
        cursor.execute("DROP TABLE path_nodes")
    elif row and row[0] == "view":
        cursor.execute("DROP VIEW path_nodes")

    cursor.execute(
        """
        CREATE VIEW path_nodes AS
        SELECT
            id, chunk_id, export_id, export_index, object_name, class_name,
            nav_type, tag, location_x, location_y, location_z,
            rotation_pitch, rotation_yaw, rotation_roll, level_ref,
            region_text, region_hex, b_paths_changed, b_light_changed,
            next_navigation_point_ref, path_list_json, forced_paths_json,
            proscribed_paths_json, property_count, property_start,
            properties_json, serial_offset, serial_size
        FROM navigation_points
        WHERE is_path_node = 1
        """
    )


def create_tables(conn):
    """Create the chunks, exports, and navigation point tables if needed."""
    cursor = conn.cursor()
    
    # Chunks table - one row per VGR file
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL UNIQUE,
            filepath TEXT NOT NULL,
            chunk_x INTEGER,
            chunk_y INTEGER,
            name_count INTEGER,
            export_count INTEGER,
            import_count INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Exports table - objects placed in chunks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS exports (
            id INTEGER PRIMARY KEY,
            chunk_id INTEGER NOT NULL,
            export_index INTEGER NOT NULL,
            object_name TEXT NOT NULL,
            class_name TEXT,
            position_x REAL,
            position_y REAL,
            position_z REAL,
            rotation_pitch REAL,
            rotation_yaw REAL,
            rotation_roll REAL,
            scale_x REAL DEFAULT 1.0,
            scale_y REAL DEFAULT 1.0,
            scale_z REAL DEFAULT 1.0,
            mesh_ref TEXT,
            prefab_name TEXT,
            serial_offset INTEGER,
            serial_size INTEGER,
            FOREIGN KEY (chunk_id) REFERENCES chunks(id),
            UNIQUE(chunk_id, export_index)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS navigation_points (
            id INTEGER PRIMARY KEY,
            chunk_id INTEGER NOT NULL,
            export_id INTEGER NOT NULL,
            export_index INTEGER NOT NULL,
            object_name TEXT NOT NULL,
            class_name TEXT NOT NULL,
            nav_type TEXT NOT NULL DEFAULT 'NavigationPoint',
            tag TEXT,
            location_x REAL,
            location_y REAL,
            location_z REAL,
            rotation_pitch REAL,
            rotation_yaw REAL,
            rotation_roll REAL,
            level_ref INTEGER,
            region_text TEXT,
            region_hex TEXT,
            b_paths_changed INTEGER,
            b_light_changed INTEGER,
            is_path_node INTEGER DEFAULT 0,
            is_player_start INTEGER DEFAULT 0,
            next_navigation_point_ref INTEGER,
            path_list_json TEXT,
            forced_paths_json TEXT,
            proscribed_paths_json TEXT,
            property_count INTEGER,
            property_start INTEGER,
            properties_json TEXT,
            serial_offset INTEGER,
            serial_size INTEGER,
            FOREIGN KEY (chunk_id) REFERENCES chunks(id),
            FOREIGN KEY (export_id) REFERENCES exports(id),
            UNIQUE(chunk_id, export_index)
        )
    """)
    ensure_navigation_point_columns(cursor)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_navigation_points_chunk "
        "ON navigation_points(chunk_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_navigation_points_class "
        "ON navigation_points(class_name)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_navigation_points_location "
        "ON navigation_points(location_x, location_y, location_z)"
    )
    ensure_path_nodes_view(cursor)
    
    conn.commit()
    print("Database tables created/verified")


def read_compact_index(data: bytes, pos: int) -> Tuple[int, int]:
    """Read a UE2 compact index."""
    b0 = data[pos]
    pos += 1
    negative = b0 & 0x80
    value = b0 & 0x3F
    
    if b0 & 0x40:
        b1 = data[pos]
        pos += 1
        value |= (b1 & 0x7F) << 6
        if b1 & 0x80:
            b2 = data[pos]
            pos += 1
            value |= (b2 & 0x7F) << 13
            if b2 & 0x80:
                b3 = data[pos]
                pos += 1
                value |= (b3 & 0x7F) << 20
                if b3 & 0x80:
                    b4 = data[pos]
                    pos += 1
                    value |= b4 << 27
    
    return (-value if negative else value, pos)


def print_progress_bar(iteration, total, prefix='', suffix='', decimals=1, length=40, fill='█', print_end="\r"):
    """
    Call in a loop to create terminal progress bar
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filled_length = int(length * iteration // total)
    bar = fill * filled_length + '-' * (length - filled_length)
    print(f'\r{prefix} |{bar}| {percent}% {suffix}', end=print_end)
    if iteration == total: 
        print()


def read_fstring(data: bytes, pos: int) -> Tuple[str, int]:
    """Read a UE2 FString."""
    length, pos = read_compact_index(data, pos)
    if length < 0:
        length = -length
        result = data[pos:pos + length * 2].decode('utf-16-le', errors='replace').rstrip('\x00')
        pos += length * 2
    else:
        result = data[pos:pos + length].decode('latin-1', errors='replace').rstrip('\x00')
        pos += length
    return result, pos


def parse_chunk_name(filename: str) -> Tuple[Optional[int], Optional[int]]:
    """Parse chunk coordinates from filename like 'chunk_n25_26.vgr'."""
    import re
    match = re.search(r'chunk_n?(-?\d+)_n?(-?\d+)', filename)
    if match:
        x = int(match.group(1))
        y = int(match.group(2))
        return (x, y)
    return (None, None)


def extract_position_from_data(obj_data: bytes) -> Optional[Tuple[float, float, float]]:
    """Extract position from CompoundObject/Actor serialized data."""
    # Try multiple methods
    
    # Method 1: Look for 0x0b marker (common in CompoundObjects)
    for i in range(40, min(len(obj_data) - 12, 100)):
        if obj_data[i] == 0x0B:
            if i + 13 <= len(obj_data):
                x = struct.unpack('<f', obj_data[i+1:i+5])[0]
                y = struct.unpack('<f', obj_data[i+5:i+9])[0]
                z = struct.unpack('<f', obj_data[i+9:i+13])[0]
                if all(not (v != v) and 1000 < abs(v) < 500000 for v in [x, y, z]):
                    return (x, y, z)
    
    # Method 2: Scan last portion for valid coordinate triplet
    for j in range(max(0, len(obj_data) - 30), len(obj_data) - 11):
        x = struct.unpack('<f', obj_data[j:j+4])[0]
        y = struct.unpack('<f', obj_data[j+4:j+8])[0]
        z = struct.unpack('<f', obj_data[j+8:j+12])[0]
        if all(1000 < abs(v) < 500000 for v in [x, y, z]) and not any(v != v for v in [x, y, z]):
            return (x, y, z)
    
    return None


def is_path_node_class(class_name: str) -> bool:
    """Return true for concrete PathNode-style navigation anchors."""
    return bool(class_name) and (
        class_name in PATH_NODE_CLASSES or class_name.endswith("PathNode")
    )


def is_player_start_class(class_name: str) -> bool:
    """Return true for PlayerStart navigation anchors."""
    return bool(class_name) and (
        class_name == "PlayerStart" or class_name.endswith("PlayerStart")
    )


def is_navigation_point_class(class_name: str) -> bool:
    """Return true for UE2 NavigationPoint subclasses identifiable by class name."""
    if not class_name:
        return False
    return (
        class_name in NAVIGATION_POINT_CLASSES
        or class_name.endswith("NavigationPoint")
        or class_name.endswith("PathNode")
        or class_name.endswith("PlayerStart")
    )


def json_vector_to_tuple(value) -> Optional[Tuple[float, float, float]]:
    """Convert a parsed UE2 vector JSON string/dict into an xyz tuple."""
    if value is None:
        return None

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None

    if not isinstance(value, dict):
        return None

    try:
        x = float(value["x"])
        y = float(value["y"])
        z = float(value["z"])
    except (KeyError, TypeError, ValueError):
        return None

    if any(v != v or abs(v) > 500000 for v in (x, y, z)):
        return None
    return (x, y, z)


def json_rotator_to_tuple(value) -> Optional[Tuple[int, int, int]]:
    """Convert a parsed UE2 rotator JSON string/dict into a pitch/yaw/roll tuple."""
    if value is None:
        return None

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None

    if not isinstance(value, dict):
        return None

    try:
        pitch = int(value["pitch"])
        yaw = int(value["yaw"])
        roll = int(value["roll"])
    except (KeyError, TypeError, ValueError):
        return None

    return (pitch, yaw, roll)


def property_size_from_info(data: bytes, pos: int, size_type: int) -> Tuple[Optional[int], int]:
    """Read the UE2 property size field for direct tag scans."""
    if size_type == 0:
        return 1, pos
    if size_type == 1:
        return 2, pos
    if size_type == 2:
        return 4, pos
    if size_type == 3:
        return 12, pos
    if size_type == 4:
        return 16, pos
    if size_type == 5:
        if pos >= len(data):
            return None, pos
        return data[pos], pos + 1
    if size_type == 6:
        if pos + 2 > len(data):
            return None, pos
        return struct.unpack("<H", data[pos:pos + 2])[0], pos + 2
    if size_type == 7:
        if pos + 4 > len(data):
            return None, pos
        return struct.unpack("<I", data[pos:pos + 4])[0], pos + 4
    return None, pos


def scan_struct_property(
    obj_data: bytes,
    names: List[str],
    prop_name: str,
    struct_name: str,
) -> Optional[Tuple]:
    """Find a fixed-size struct property by tag when the chain parser desyncs."""
    for pos in range(max(0, len(obj_data) - 1)):
        try:
            name_idx, offset = read_compact_index(obj_data, pos)
        except (IndexError, TypeError):
            continue
        if name_idx < 0 or name_idx >= len(names) or names[name_idx] != prop_name:
            continue
        if offset >= len(obj_data):
            continue

        info_byte = obj_data[offset]
        offset += 1
        prop_type = info_byte & 0x0F
        size_type = (info_byte >> 4) & 0x07
        array_flag = (info_byte >> 7) & 0x01
        if prop_type != 10 or array_flag:
            continue

        prop_size, offset = property_size_from_info(obj_data, offset, size_type)
        if prop_size != 12:
            continue

        try:
            struct_idx, offset = read_compact_index(obj_data, offset)
        except (IndexError, TypeError):
            continue
        if struct_idx < 0 or struct_idx >= len(names) or names[struct_idx] != struct_name:
            continue
        if offset + prop_size > len(obj_data):
            continue

        raw = obj_data[offset:offset + prop_size]
        if struct_name == "Vector":
            value = struct.unpack("<fff", raw)
            if all(v == v and abs(v) <= 500000 for v in value):
                return value
        elif struct_name == "Rotator":
            return struct.unpack("<iii", raw)

    return None


def bool_to_int(value) -> Optional[int]:
    """Convert parsed UE2 bools to SQLite-friendly values without hiding nulls."""
    if value is None:
        return None
    return int(bool(value))


def property_value_as_text(prop: Optional[Dict]) -> Optional[str]:
    """Return a compact text/JSON representation for optional nav fields."""
    if not prop:
        return None

    value = prop.get("value")
    if value is None:
        return prop.get("value_hex")
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def navigation_properties_json(props: List[Dict]) -> str:
    """Serialize parsed nav properties so unexpected fields remain inspectable."""
    compact = []
    for prop in props:
        compact.append(
            {
                "name": prop.get("name"),
                "type": prop.get("type"),
                "struct_name": prop.get("struct_name"),
                "array_index": prop.get("array_index"),
                "list_index": prop.get("list_index"),
                "value": prop.get("value"),
                "value_hex": prop.get("value_hex"),
            }
        )
    return json.dumps(compact, sort_keys=True)


def extract_navigation_point_from_data(
    obj_data: bytes, names: List[str], max_search: int = 80
) -> Optional[Dict]:
    """Extract fields from a NavigationPoint/PathNode/PlayerStart export."""
    start_offset = find_property_start(obj_data, names, max_search=max_search)
    if start_offset < 0:
        return None

    props = parse_properties(obj_data, names, start_offset)
    by_name = {prop["name"]: prop for prop in props}

    location = json_vector_to_tuple(by_name.get("Location", {}).get("value"))
    used_direct_scan = False
    if location is None:
        location = scan_struct_property(obj_data, names, "Location", "Vector")
        used_direct_scan = location is not None
    if location is None:
        return None

    rotation = json_rotator_to_tuple(by_name.get("Rotation", {}).get("value"))
    if rotation is None:
        rotation = scan_struct_property(obj_data, names, "Rotation", "Rotator")
    region = by_name.get("Region")
    return {
        "tag": by_name.get("Tag", {}).get("value"),
        "location": location,
        "rotation": rotation,
        "level_ref": by_name.get("Level", {}).get("value"),
        "region_text": region.get("value") if region else None,
        "region_hex": region.get("value_hex") if region else None,
        "b_paths_changed": by_name.get("bPathsChanged", {}).get("value"),
        "b_light_changed": by_name.get("bLightChanged", {}).get("value"),
        "next_navigation_point_ref": by_name.get("nextNavigationPoint", {}).get("value"),
        "path_list_json": property_value_as_text(by_name.get("PathList")),
        "forced_paths_json": property_value_as_text(by_name.get("ForcedPaths")),
        "proscribed_paths_json": property_value_as_text(by_name.get("ProscribedPaths")),
        "property_count": len(props),
        "property_start": start_offset,
        "properties_json": navigation_properties_json(props),
        "used_direct_scan": used_direct_scan,
    }


def parse_vgr_file(filepath: str) -> Dict:
    """Parse a VGR chunk file and extract all exports."""
    with open(filepath, 'rb') as f:
        data = f.read()
    
    # Parse header
    signature = struct.unpack('<I', data[0:4])[0]
    version = struct.unpack('<H', data[4:6])[0]
    
    name_count = struct.unpack('<I', data[12:16])[0]
    name_offset = struct.unpack('<I', data[16:20])[0]
    export_count = struct.unpack('<I', data[20:24])[0]
    export_offset = struct.unpack('<I', data[24:28])[0]
    import_count = struct.unpack('<I', data[28:32])[0]
    import_offset = struct.unpack('<I', data[32:36])[0]
    
    # Read names
    names = []
    pos = name_offset
    for _ in range(name_count):
        name, pos = read_fstring(data, pos)
        flags = struct.unpack('<I', data[pos:pos+4])[0]
        pos += 4
        names.append(name)
    
    # Read imports
    imports = []
    pos = import_offset
    for _ in range(import_count):
        class_pkg, pos = read_compact_index(data, pos)
        class_name, pos = read_compact_index(data, pos)
        package = struct.unpack('<i', data[pos:pos+4])[0]
        pos += 4
        obj_name, pos = read_compact_index(data, pos)
        imports.append({
            'class': names[class_name] if 0 <= class_name < len(names) else '',
            'name': names[obj_name] if 0 <= obj_name < len(names) else '',
        })
    
    # Read exports
    exports = []
    pos = export_offset
    for idx in range(export_count):
        class_index, pos = read_compact_index(data, pos)
        super_index, pos = read_compact_index(data, pos)
        package = struct.unpack('<i', data[pos:pos+4])[0]
        pos += 4
        object_name, pos = read_compact_index(data, pos)
        object_flags = struct.unpack('<I', data[pos:pos+4])[0]
        pos += 4
        serial_size, pos = read_compact_index(data, pos)
        serial_offset = 0
        if serial_size > 0:
            serial_offset, pos = read_compact_index(data, pos)
        
        # Get class name from imports
        class_name = ''
        if class_index < 0:
            import_idx = -class_index - 1
            if import_idx < len(imports):
                class_name = imports[import_idx]['name']
        
        obj_name = names[object_name] if 0 <= object_name < len(names) else ''
        
        obj_data = data[serial_offset:serial_offset + serial_size]

        # Extract position for placeable objects
        position = None
        navigation_point = None
        if class_name in ('CompoundObject', 'Actor', 'StaticMeshActor', 'Prefab'):
            position = extract_position_from_data(obj_data)
        elif is_navigation_point_class(class_name):
            navigation_point = extract_navigation_point_from_data(obj_data, names)
            if navigation_point:
                position = navigation_point["location"]
        
        exports.append({
            'index': idx + 1,
            'object_name': obj_name,
            'class_name': class_name,
            'serial_offset': serial_offset,
            'serial_size': serial_size,
            'position': position,
            'rotation': navigation_point.get("rotation") if navigation_point else None,
            'navigation_point': navigation_point,
        })
    
    return {
        'name_count': name_count,
        'export_count': export_count,
        'import_count': import_count,
        'exports': exports,
    }


def process_chunk_file(conn, filepath: str, silent=False):
    """Process a single VGR file and store in database."""
    filename = os.path.basename(filepath)
    chunk_x, chunk_y = parse_chunk_name(filename)
    
    if not silent:
        print(f"  Parsing {filename}...", end=" ")
    
    try:
        parsed = parse_vgr_file(filepath)
    except Exception as e:
        print(f"ERROR: {e}")
        return 0
    
    cursor = conn.cursor()
    
    # Insert or update chunk without changing its primary key. SQLite REPLACE
    # deletes the old row, which would orphan exports/properties for reruns.
    cursor.execute("""
        INSERT INTO chunks
        (filename, filepath, chunk_x, chunk_y, name_count, export_count, import_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(filename) DO UPDATE SET
            filepath = excluded.filepath,
            chunk_x = excluded.chunk_x,
            chunk_y = excluded.chunk_y,
            name_count = excluded.name_count,
            export_count = excluded.export_count,
            import_count = excluded.import_count
    """, (
        filename, filepath, chunk_x, chunk_y,
        parsed['name_count'], parsed['export_count'], parsed['import_count']
    ))
    
    chunk_id = cursor.execute(
        "SELECT id FROM chunks WHERE filename = ?", (filename,)
    ).fetchone()[0]
    
    # Clear old dependent rows and exports for this chunk.
    if cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'properties'"
    ).fetchone():
        cursor.execute(
            """
            DELETE FROM properties
            WHERE export_id IN (SELECT id FROM exports WHERE chunk_id = ?)
            """,
            (chunk_id,),
        )
    cursor.execute("DELETE FROM navigation_points WHERE chunk_id = ?", (chunk_id,))
    cursor.execute("DELETE FROM exports WHERE chunk_id = ?", (chunk_id,))
    
    # Insert exports with positions and navigation point metadata.
    placed_count = 0
    navigation_point_count = 0
    for exp in parsed['exports']:
        pos = exp.get('position')
        rot = exp.get('rotation')
        cursor.execute("""
            INSERT INTO exports
            (chunk_id, export_index, object_name, class_name, 
             position_x, position_y, position_z,
             rotation_pitch, rotation_yaw, rotation_roll,
             serial_offset, serial_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chunk_id, exp['index'], exp['object_name'], exp['class_name'],
            pos[0] if pos else None,
            pos[1] if pos else None,
            pos[2] if pos else None,
            rot[0] if rot else None,
            rot[1] if rot else None,
            rot[2] if rot else None,
            exp['serial_offset'], exp['serial_size']
        ))
        export_id = cursor.lastrowid
        if pos:
            placed_count += 1

        navigation_point = exp.get("navigation_point")
        if navigation_point:
            cursor.execute("""
                INSERT INTO navigation_points (
                    chunk_id, export_id, export_index, object_name, class_name,
                    nav_type, tag, location_x, location_y, location_z,
                    rotation_pitch, rotation_yaw, rotation_roll, level_ref,
                    region_text, region_hex, b_paths_changed, b_light_changed,
                    is_path_node, is_player_start, next_navigation_point_ref,
                    path_list_json, forced_paths_json, proscribed_paths_json,
                    property_count, property_start, properties_json,
                    serial_offset, serial_size
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                chunk_id,
                export_id,
                exp['index'],
                exp['object_name'],
                exp['class_name'],
                exp['class_name'],
                navigation_point.get("tag"),
                navigation_point["location"][0],
                navigation_point["location"][1],
                navigation_point["location"][2],
                rot[0] if rot else None,
                rot[1] if rot else None,
                rot[2] if rot else None,
                navigation_point.get("level_ref"),
                navigation_point.get("region_text"),
                navigation_point.get("region_hex"),
                bool_to_int(navigation_point.get("b_paths_changed")),
                bool_to_int(navigation_point.get("b_light_changed")),
                bool_to_int(is_path_node_class(exp['class_name'])),
                bool_to_int(is_player_start_class(exp['class_name'])),
                navigation_point.get("next_navigation_point_ref"),
                navigation_point.get("path_list_json"),
                navigation_point.get("forced_paths_json"),
                navigation_point.get("proscribed_paths_json"),
                navigation_point.get("property_count"),
                navigation_point.get("property_start"),
                navigation_point.get("properties_json"),
                exp['serial_offset'],
                exp['serial_size'],
            ))
            navigation_point_count += 1
    
    conn.commit()
    if not silent:
        print(
            f"OK ({parsed['export_count']} exports, {placed_count} with positions, "
            f"{navigation_point_count} navigation points)"
        )
    return placed_count


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Extract chunk data to database")
    parser.add_argument('--file', help="Process specific chunk file")
    parser.add_argument('--limit', type=int, help="Limit number of files to process")
    parser.add_argument('--silent', action='store_true', help="Suppress all output except errors")
    args = parser.parse_args()
    
    if not args.silent:
        print("=" * 60)
        print("Chunk Data Extractor")
        print("=" * 60)
        print(f"Database: {DB_PATH}")
        print(f"Maps Dir: {MAPS_DIR}")
        print()
    
    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    
    # Find VGR files
    if args.file:
        vgr_files = [os.path.join(MAPS_DIR, args.file)]
    else:
        vgr_files = sorted([
            os.path.join(MAPS_DIR, f) 
            for f in os.listdir(MAPS_DIR) 
            if f.endswith('.vgr') and f.startswith('chunk_')
        ])
    
    if args.limit:
        vgr_files = vgr_files[:args.limit]
    
    if not args.silent:
        print(f"Found {len(vgr_files)} chunk files to process")
        print()
    
    total_placed = 0
    total_files = len(vgr_files)
    
    for i, filepath in enumerate(vgr_files):
        if os.path.exists(filepath):
            total_placed += process_chunk_file(conn, filepath, silent=True)
            print_progress_bar(i + 1, total_files, prefix='   Progress:', suffix=f'({i+1}/{total_files})', length=40)
    
    if not args.silent:
        # Summary
        cursor = conn.cursor()
        chunk_count = cursor.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        export_count = cursor.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
        placed_count = cursor.execute(
            "SELECT COUNT(*) FROM exports WHERE position_x IS NOT NULL"
        ).fetchone()[0]
        navigation_point_count = cursor.execute(
            "SELECT COUNT(*) FROM navigation_points"
        ).fetchone()[0]
        path_node_count = cursor.execute("SELECT COUNT(*) FROM path_nodes").fetchone()[0]
        
        print()
        print("=" * 60)
        print("Extraction Complete")
        print("=" * 60)
        print(f"Chunks in database: {chunk_count}")
        print(f"Total exports: {export_count}")
        print(f"Exports with positions: {placed_count}")
        print(f"Navigation points: {navigation_point_count}")
        print(f"Path nodes: {path_node_count}")
    
    conn.close()


if __name__ == "__main__":
    main()
