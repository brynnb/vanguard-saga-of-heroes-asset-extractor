#!/usr/bin/env python3
"""
Build shader and texture database from extracted Vanguard assets.

Populates the shaders and mesh_materials tables in the canonical database.
Maps shader names to their diffuse texture PNG files.
"""

import os
import sys
import sqlite3
from pathlib import Path

# Add project root to path (go up 2 levels from scripts/extractors)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import config

# Use canonical database
DB_PATH = config.DB_PATH
TEXTURES_DIR = getattr(config, 'TEXTURES_DIR', os.path.join(config.ASSETS_PATH, "Textures"))


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


def parse_mat_file(mat_path: str) -> dict:
    """Parse a .mat file and extract texture references."""
    result = {}
    try:
        with open(mat_path, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    key, value = line.split("=", 1)
                    result[key.strip()] = value.strip()
    except Exception:
        pass
    return result


def build_texture_database(silent=False):
    """
    Scan for textures and shader .mat files, populate database tables.
    """
    conn = sqlite3.connect(DB_PATH)
    
    # Clear existing entries
    conn.execute("DELETE FROM shaders")
    
    textures_path = Path(TEXTURES_DIR)
    
    if not textures_path.exists():
        if not silent:
            print(f"   ⚠ Textures directory not found: {TEXTURES_DIR}")
            print("   Skipping texture database build.")
        conn.close()
        return
    
    # First pass: collect all PNG textures
    print("   Scanning for PNG textures...")
    textures = {}
    for png_file in textures_path.rglob("*.png"):
        texture_name = png_file.stem.lower()
        textures[texture_name] = str(png_file.absolute())

    if not silent:
        print(f"   Found {len(textures)} PNG textures")

    # Second pass: parse all .mat files and insert into shaders table
    if not silent:
        print("   Parsing shader .mat files...")
    
    mat_files = list(textures_path.rglob("*.mat"))
    total_mats = len(mat_files)
    shader_count = 0
    mapped_count = 0
    
    for i, mat_file in enumerate(mat_files):
        print_progress_bar(i + 1, total_mats, prefix='   Progress:', suffix=f'({i+1}/{total_mats})', length=40)
        shader_name = mat_file.stem
        mat_data = parse_mat_file(str(mat_file))

        if mat_data:
            diffuse = mat_data.get("Diffuse", "")
            normal = mat_data.get("Normal", "")
            specular = mat_data.get("Specular", "")
            
            # Resolve diffuse to actual texture path
            diffuse_path = None
            if diffuse:
                diffuse_lower = diffuse.lower()
                if diffuse_lower in textures:
                    diffuse_path = textures[diffuse_lower]
                    mapped_count += 1
            
            # Insert into shaders table
            conn.execute("""
                INSERT OR REPLACE INTO shaders 
                (shader_name, diffuse_texture, normal_texture, specular_texture)
                VALUES (?, ?, ?, ?)
            """, (shader_name.lower(), diffuse_path, normal, specular))
            shader_count += 1

    conn.commit()
    conn.close()
    
    if not silent:
        print(f"   ✓ Parsed {shader_count} shader definitions")
        print(f"   ✓ Mapped {mapped_count} shaders to textures")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Texture Database Builder")
    parser.add_argument('--silent', action='store_true', help="Suppress all output except errors")
    args = parser.parse_args()
    
    if not args.silent:
        print("=" * 60)
        print("Texture Database Builder")
        print("=" * 60)
        print(f"Database: {DB_PATH}")
        print(f"Textures: {TEXTURES_DIR}")
        print()
    build_texture_database(silent=args.silent)
