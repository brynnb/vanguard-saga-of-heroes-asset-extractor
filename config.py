"""Local path configuration for the Vanguard extraction scripts.

The defaults first look for a local Vanguard EMU folder inside this repo:

    ./Vanguard EMU

If that folder is not present, the scripts fall back to:

    ~/Downloads/Vanguard EMU

Override paths with environment variables when needed:

    VANGUARD_EMU_PATH="/path/to/Vanguard EMU"
    VANGUARD_ASSETS_PATH="/path/to/Vanguard EMU/Assets"
    UNREAL_LIBRARY_DLL=/path/to/Eliot.UELib.CLI.dll
"""

from __future__ import annotations

import os
from pathlib import Path


RENDERER_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = Path(RENDERER_ROOT)

LOCAL_VANGUARD_EMU_ROOT = PROJECT_ROOT / "Vanguard EMU"
DOWNLOADS_VANGUARD_EMU_ROOT = Path("~/Downloads/Vanguard EMU").expanduser()
DEFAULT_VANGUARD_EMU_ROOT = (
    LOCAL_VANGUARD_EMU_ROOT
    if (LOCAL_VANGUARD_EMU_ROOT / "Assets").exists()
    else DOWNLOADS_VANGUARD_EMU_ROOT
)

VANGUARD_EMU_ROOT = Path(
    os.environ.get("VANGUARD_EMU_PATH", str(DEFAULT_VANGUARD_EMU_ROOT))
).expanduser()

ASSETS_PATH = os.path.expanduser(
    os.environ.get(
        "VANGUARD_ASSETS_PATH",
        os.environ.get("VANGUARD_ASSETS", str(VANGUARD_EMU_ROOT / "Assets")),
    )
)

OUTPUT_DIR = os.path.join(RENDERER_ROOT, "output")
MESHES_DIR = os.path.join(OUTPUT_DIR, "meshes")
MESH_BUILDINGS_DIR = os.path.join(MESHES_DIR, "buildings")
TERRAIN_DIR = os.path.join(OUTPUT_DIR, "terrain")
TERRAIN_GRID_DIR = os.path.join(TERRAIN_DIR, "terrain_grid")
CHUNKS_DIR = os.path.join(OUTPUT_DIR, "chunks")
DATA_DIR = os.path.join(OUTPUT_DIR, "data")
ZONES_DIR = os.path.join(OUTPUT_DIR, "zones")

TEXTURES_DIR = os.path.join(ASSETS_PATH, "Textures")
MAPS_DIR = os.path.join(ASSETS_PATH, "Maps")
ARCHIVES_DIR = os.path.join(ASSETS_PATH, "Archives")
CHARACTER_MESHES_DIR = os.path.join(ASSETS_PATH, "Characters", "Meshes")
CHARACTER_ANIMATIONS_DIR = os.path.join(ASSETS_PATH, "Characters", "Animations")
SKELETAL_MESHES_DIR = os.path.join(ASSETS_PATH, "SkeletalMeshes")

DB_PATH = os.path.join(DATA_DIR, "vanguard_data.db")
TEXTURE_DB_PATH = os.path.join(DATA_DIR, "texture_db.json")
MESH_MATERIALS_PATH = os.path.join(DATA_DIR, "mesh_materials.json")
SGO_PATH = os.path.join(ARCHIVES_DIR, "binaryprefabs.sgo")

REFERENCE_DIR = os.path.expanduser(
    os.environ.get("VANGUARD_REFERENCE_DIR", os.path.join(OUTPUT_DIR, "reference"))
)
REFERENCE_MAPS_DIR = os.path.join(REFERENCE_DIR, "Maps")

UNREAL_LIBRARY_DLL = os.path.expanduser(
    os.environ.get(
        "UNREAL_LIBRARY_DLL",
        str(
            PROJECT_ROOT
            / "external"
            / "Unreal-Library"
            / "CLI"
            / "bin"
            / "Debug"
            / "net8.0"
            / "Eliot.UELib.CLI.dll"
        ),
    )
)
DOTNET = os.environ.get("DOTNET", "dotnet")
