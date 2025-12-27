#!/usr/bin/env python3
"""Build a global index of mesh/prefab names to their containing packages."""

import os
import sys
import sqlite3
import json
from pathlib import Path

# Add current directory for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_bsp import UE2Package

# Add parent directory for config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

DB_PATH = os.path.join(config.DATA_DIR, "mesh_index.sqlite")
ASSETS_DIR = config.ASSETS_PATH

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mesh_index (
            object_name TEXT NOT NULL,
            class_name TEXT,
            package_path TEXT NOT NULL,
            file_type TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_mesh_name ON mesh_index(object_name);
        CREATE INDEX IF NOT EXISTS idx_mesh_class ON mesh_index(class_name);
    """)
    conn.commit()
    return conn

def scan_files():
    conn = init_db()
    # Clear existing
    conn.execute("DELETE FROM mesh_index")
    
    extensions = {".usx": "StaticMesh", ".vgr": "Map", ".prefab": "Prefab", ".upk": "Package"}
    files = []
    for ext in extensions.keys():
        files.extend(list(Path(ASSETS_DIR).glob(f"**/*{ext}")))
    
    print(f"Found {len(files)} files to index...")
    
    count = 0
    for i, file_path in enumerate(files):
        if i % 50 == 0:
            print(f"  [{i}/{len(files)}] Processing {file_path.name}...")
            conn.commit()
            
        rel_path = os.path.relpath(file_path, ASSETS_DIR)
        file_ext = file_path.suffix.lower()
        
        try:
            pkg = UE2Package(str(file_path))
            # Index all exports
            for exp in pkg.exports:
                obj_name = exp.get("object_name")
                class_name = exp.get("class_name")
                
                # We specifically care about things that can be mesh_refs
                if class_name in ("StaticMesh", "Prefab", "CompoundObjectPrefab", "CompoundObject"):
                    conn.execute(
                        "INSERT INTO mesh_index (object_name, class_name, package_path, file_type) VALUES (?, ?, ?, ?)",
                        (obj_name, class_name, rel_path, file_ext)
                    )
                    count += 1
        except Exception:
            # Skip if file can't be parsed (corrupt or non-UE2)
            continue
            
    conn.commit()
    conn.close()
    print(f"Done! Indexed {count} objects from {len(files)} files.")

if __name__ == "__main__":
    scan_files()
