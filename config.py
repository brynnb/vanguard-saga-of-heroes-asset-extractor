import os

# Base paths
# RENDERER_ROOT assumes this file is in renderer/ directory
RENDERER_ROOT = os.path.dirname(os.path.abspath(__file__))

# Default assets path - can be overridden by env var or manually changed here
ASSETS_PATH = os.environ.get("VANGUARD_ASSETS_PATH", "/Users/brynnbateman/Downloads/Vanguard EMU/Assets")

# Output paths
OUTPUT_DIR = os.path.join(RENDERER_ROOT, "output")
MESHES_DIR = os.path.join(OUTPUT_DIR, "meshes")
MESH_BUILDINGS_DIR = os.path.join(MESHES_DIR, "buildings")
TERRAIN_DIR = os.path.join(OUTPUT_DIR, "terrain")
TERRAIN_GRID_DIR = os.path.join(TERRAIN_DIR, "terrain_grid")
CHUNKS_DIR = os.path.join(OUTPUT_DIR, "chunks")
DATA_DIR = os.path.join(OUTPUT_DIR, "data")
ZONES_DIR = os.path.join(OUTPUT_DIR, "zones")

# Data files
DB_PATH = os.path.join(DATA_DIR, "vanguard_data.db")
TEXTURE_DB_PATH = os.path.join(DATA_DIR, "texture_db.json")
MESH_MATERIALS_PATH = os.path.join(DATA_DIR, "mesh_materials.json")

# External Tools
ARCHIVE_DIR = os.path.join(RENDERER_ROOT, "_archive")
UMODEL_PATH = os.path.join(ARCHIVE_DIR, "gildor2-UEViewer-daa3c14/umodel.exe")
WINE_PATH = "/Applications/Wine Stable.app/Contents/Resources/wine/bin/wine"
