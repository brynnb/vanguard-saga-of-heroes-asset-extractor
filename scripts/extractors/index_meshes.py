#!/usr/bin/env python3
"""Build a global index of mesh/prefab names to their containing packages.

Populates the mesh_index table in the canonical database.
"""

import os
import sys
import sqlite3
from pathlib import Path

# Add project root to path (go up 2 levels from scripts/extractors)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# Add current directory for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_bsp import UE2Package

import config

# Use canonical database
DB_PATH = config.DB_PATH
ASSETS_DIR = config.ASSETS_PATH


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


def scan_files(silent=False):
    """Scan all mesh packages and populate mesh_index table."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    
    # Clear existing mesh_index entries
    conn.execute("DELETE FROM mesh_index")
    
    extensions = {".usx": "StaticMesh", ".vgr": "Map", ".prefab": "Prefab", ".upk": "Package"}
    files = []
    for ext in extensions.keys():
        files.extend(list(Path(ASSETS_DIR).glob(f"**/*{ext}")))
    
    if not silent:
        print(f"   Found {len(files)} files to index...")
    
    total_files = len(files)
    for i, file_path in enumerate(files):
        if i % 100 == 0 and i > 0:
            conn.commit()
            
        print_progress_bar(i + 1, total_files, prefix='   Progress:', suffix=f'({i+1}/{total_files})', length=40)
            
        rel_path = os.path.relpath(file_path, ASSETS_DIR)
        file_ext = file_path.suffix.lower()
        
        try:
            pkg = UE2Package(str(file_path))
            # Index all exports that could be mesh references
            for exp in pkg.exports:
                obj_name = exp.get("object_name")
                class_name = exp.get("class_name")
                
                # We specifically care about things that can be mesh_refs
                if class_name in ("StaticMesh", "Prefab", "CompoundObjectPrefab", "CompoundObject"):
                    conn.execute(
                        "INSERT OR IGNORE INTO mesh_index (object_name, class_name, package_path, file_type) VALUES (?, ?, ?, ?)",
                        (obj_name, class_name, rel_path, file_ext)
                    )
                    count += 1
        except Exception:
            # Skip if file can't be parsed (corrupt or non-UE2)
            continue
            
    conn.commit()
    conn.close()
    if not silent:
        print(f"   ✓ Indexed {count} mesh objects from {len(files)} files")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Mesh Index Builder")
    parser.add_argument('--silent', action='store_true', help="Suppress all output except errors")
    args = parser.parse_args()
    
    if not args.silent:
        print("=" * 60)
        print("Mesh Index Builder")
        print("=" * 60)
        print(f"Database: {DB_PATH}")
        print(f"Assets: {ASSETS_DIR}")
        print()
    scan_files(silent=args.silent)
