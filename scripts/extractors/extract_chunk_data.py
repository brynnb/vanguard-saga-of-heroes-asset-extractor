#!/usr/bin/env python3
"""
Extract chunk metadata and placed objects from VGR files into the database.
Creates the necessary tables and populates them with extracted data.
"""

import sqlite3
import struct
import os
import sys
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
    DB_PATH = "/Users/brynnbateman/Downloads/Vanguard EMU/vanguard_files.db"
    MAPS_DIR = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Maps"


def create_tables(conn):
    """Create the chunks and exports tables if they don't exist."""
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
        
        # Extract position for placeable objects
        position = None
        if class_name in ('CompoundObject', 'Actor', 'StaticMeshActor', 'Prefab'):
            obj_data = data[serial_offset:serial_offset + serial_size]
            position = extract_position_from_data(obj_data)
        
        exports.append({
            'index': idx,
            'object_name': obj_name,
            'class_name': class_name,
            'serial_offset': serial_offset,
            'serial_size': serial_size,
            'position': position,
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
    
    # Insert or update chunk
    cursor.execute("""
        INSERT OR REPLACE INTO chunks 
        (filename, filepath, chunk_x, chunk_y, name_count, export_count, import_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        filename, filepath, chunk_x, chunk_y,
        parsed['name_count'], parsed['export_count'], parsed['import_count']
    ))
    
    chunk_id = cursor.lastrowid or cursor.execute(
        "SELECT id FROM chunks WHERE filename = ?", (filename,)
    ).fetchone()[0]
    
    # Clear old exports for this chunk
    cursor.execute("DELETE FROM exports WHERE chunk_id = ?", (chunk_id,))
    
    # Insert exports with positions
    placed_count = 0
    for exp in parsed['exports']:
        pos = exp.get('position')
        cursor.execute("""
            INSERT INTO exports
            (chunk_id, export_index, object_name, class_name, 
             position_x, position_y, position_z,
             serial_offset, serial_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chunk_id, exp['index'], exp['object_name'], exp['class_name'],
            pos[0] if pos else None,
            pos[1] if pos else None,
            pos[2] if pos else None,
            exp['serial_offset'], exp['serial_size']
        ))
        if pos:
            placed_count += 1
    
    conn.commit()
    if not silent:
        print(f"OK ({parsed['export_count']} exports, {placed_count} with positions)")
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
        
        print()
        print("=" * 60)
        print("Extraction Complete")
        print("=" * 60)
        print(f"Chunks in database: {chunk_count}")
        print(f"Total exports: {export_count}")
        print(f"Exports with positions: {placed_count}")
    
    conn.close()


if __name__ == "__main__":
    main()
