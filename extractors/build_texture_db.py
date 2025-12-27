#!/usr/bin/env python3
"""
Build a texture database from extracted Vanguard assets.
Maps shader names to their diffuse texture PNG files.
"""

import os
import json
import re
from pathlib import Path
import sys

# Add parent directory to path to allow importing config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


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
    except Exception as e:
        pass
    return result


def build_texture_database(textures_root: str) -> dict:
    """
    Build a database mapping shader names to texture file paths.

    Returns:
        Dict with structure:
        {
            "shaders": { shader_name: { "diffuse": texture_name, ... } },
            "textures": { texture_name: absolute_path_to_png },
            "shader_to_texture": { shader_name: absolute_path_to_png }
        }
    """
    db = {"shaders": {}, "textures": {}, "shader_to_texture": {}}

    textures_path = Path(textures_root)

    # First pass: collect all PNG textures
    print("Scanning for PNG textures...")
    for png_file in textures_path.rglob("*.png"):
        texture_name = png_file.stem.lower()
        db["textures"][texture_name] = str(png_file.absolute())

    print(f"Found {len(db['textures'])} PNG textures")

    # Second pass: parse all .mat files
    print("Parsing shader .mat files...")
    for mat_file in textures_path.rglob("*.mat"):
        shader_name = mat_file.stem
        mat_data = parse_mat_file(str(mat_file))

        if mat_data:
            db["shaders"][shader_name.lower()] = mat_data

            # Map shader to diffuse texture
            diffuse = mat_data.get("Diffuse", "")
            if diffuse:
                diffuse_lower = diffuse.lower()
                if diffuse_lower in db["textures"]:
                    db["shader_to_texture"][shader_name.lower()] = db["textures"][
                        diffuse_lower
                    ]

    print(f"Parsed {len(db['shaders'])} shader definitions")
    print(f"Mapped {len(db['shader_to_texture'])} shaders to textures")

    return db


def save_database(db: dict, output_path: str):
    """Save the texture database to a JSON file."""
    with open(output_path, "w") as f:
        json.dump(db, f, indent=2)
    print(f"Saved texture database to {output_path}")


def main():
    import sys

    if len(sys.argv) < 2:
        textures_root = config.TEXTURES_DIR
    else:
        textures_root = sys.argv[1]

    output_path = sys.argv[2] if len(sys.argv) > 2 else config.TEXTURE_DB_PATH

    print(f"Building texture database from: {textures_root}")
    db = build_texture_database(textures_root)
    save_database(db, output_path)

    # Print some stats
    print(f"\nDatabase summary:")
    print(f"  Total textures: {len(db['textures'])}")
    print(f"  Total shaders: {len(db['shaders'])}")
    print(f"  Mapped shader->texture: {len(db['shader_to_texture'])}")


if __name__ == "__main__":
    main()
