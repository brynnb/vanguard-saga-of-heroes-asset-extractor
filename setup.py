#!/usr/bin/env python3
"""
Vanguard Asset Extractor - First-Time Setup

This script performs all necessary initialization for a fresh clone:
1. Validates configuration
2. Creates database and tables
3. Indexes all asset files
4. Exports chunk data
5. Indexes mesh objects
6. Builds texture database
7. Property extraction
8. (Optional) Full terrain/mesh extraction

Usage:
    python3 setup.py          # Standard setup
    python3 setup.py --reset  # Delete database and start fresh
    python3 setup.py --full   # Full setup (includes terrain + mesh extraction)
"""

import os
import sys
import sqlite3
import time
from pathlib import Path
import argparse
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# =============================================================================
# CONFIGURATION VALIDATION
# =============================================================================

def validate_config():
    """Validate that config.py exists and has required paths."""
    print("\n" + "=" * 60)
    print("STAGE 1: Validating Configuration")
    print("=" * 60)
    
    config_path = os.path.join(PROJECT_ROOT, "config.py")
    example_path = os.path.join(PROJECT_ROOT, "config.example.py")
    
    if not os.path.exists(config_path):
        print(f"\n❌ ERROR: config.py not found!")
        print(f"   Please copy config.example.py to config.py and update paths:")
        print(f"   $ cp {example_path} {config_path}")
        sys.exit(1)
    
    try:
        import config
    except ImportError as e:
        print(f"\n❌ ERROR: Failed to import config.py: {e}")
        sys.exit(1)
    
    # Validate ASSETS_PATH
    if not hasattr(config, 'ASSETS_PATH'):
        print("\n❌ ERROR: config.py missing ASSETS_PATH variable")
        sys.exit(1)
    
    if not os.path.exists(config.ASSETS_PATH):
        print(f"\n❌ ERROR: ASSETS_PATH does not exist: {config.ASSETS_PATH}")
        print("   Please update config.py with the correct path to your Vanguard Assets folder.")
        sys.exit(1)
    
    # Validate DB_PATH directory
    if not hasattr(config, 'DB_PATH'):
        print("\n❌ ERROR: config.py missing DB_PATH variable")
        sys.exit(1)
    
    db_dir = os.path.dirname(config.DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        print(f"   Created directory: {db_dir}")
    
    print(f"   ✓ config.py found")
    print(f"   ✓ ASSETS_PATH: {config.ASSETS_PATH}")
    print(f"   ✓ DB_PATH: {config.DB_PATH}")
    
    return config


# =============================================================================
# DATABASE INITIALIZATION
# =============================================================================

def init_database(config):
    """Create database and all required tables."""
    print("\n" + "=" * 60)
    print("STAGE 2: Initializing Database")
    print("=" * 60)
    
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    
    conn.executescript("""
        -- Chunks table (map chunks)
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
        );
        
        -- Exports table (objects in chunks)
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
        );
        
        -- Files table (all asset files)
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL UNIQUE,
            location TEXT,
            extension TEXT,
            category TEXT,
            size_bytes INTEGER,
            parse_coverage_pct REAL,
            is_extracted INTEGER DEFAULT 0,
            parser_notes TEXT,
            modified_time TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        
        -- Terrain chunks table
        CREATE TABLE IF NOT EXISTS terrain_chunks (
            id INTEGER PRIMARY KEY,
            chunk_id INTEGER NOT NULL,
            heightmap_offset INTEGER,
            heightmap_size INTEGER,
            grid_size INTEGER DEFAULT 512,
            height_scale REAL DEFAULT 2.4,
            terrain_scale REAL DEFAULT 390.625,
            gltf_exported INTEGER DEFAULT 0,
            export_path TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        
        -- Mesh index (maps mesh names to packages)
        CREATE TABLE IF NOT EXISTS mesh_index (
            id INTEGER PRIMARY KEY,
            object_name TEXT NOT NULL,
            class_name TEXT,
            package_path TEXT NOT NULL,
            file_type TEXT,
            UNIQUE(object_name, package_path)
        );
        
        -- Names table (name tables from each chunk)
        CREATE TABLE IF NOT EXISTS names (
            id INTEGER PRIMARY KEY,
            chunk_id INTEGER NOT NULL,
            name_index INTEGER NOT NULL,
            name TEXT NOT NULL,
            FOREIGN KEY (chunk_id) REFERENCES chunks(id),
            UNIQUE(chunk_id, name_index)
        );
        
        -- Imports table (class imports from each chunk)
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY,
            chunk_id INTEGER NOT NULL,
            import_index INTEGER NOT NULL,
            object_name TEXT,
            class_name TEXT,
            class_package TEXT,
            FOREIGN KEY (chunk_id) REFERENCES chunks(id),
            UNIQUE(chunk_id, import_index)
        );
        
        -- Properties table (parsed UObject properties)
        CREATE TABLE IF NOT EXISTS properties (
            id INTEGER PRIMARY KEY,
            export_id INTEGER NOT NULL,
            prop_name TEXT NOT NULL,
            prop_type TEXT,
            prop_size INTEGER,
            array_index INTEGER DEFAULT 0,
            struct_name TEXT,
            value_text TEXT,
            FOREIGN KEY (export_id) REFERENCES exports(id)
        );
        
        -- Parsing Sessions (for tracking mass extractions)
        CREATE TABLE IF NOT EXISTS parse_sessions (
            id INTEGER PRIMARY KEY,
            started_at TEXT,
            completed_at TEXT,
            parser_version TEXT,
            files_processed INTEGER DEFAULT 0,
            exports_processed INTEGER DEFAULT 0,
            total_bytes_parsed INTEGER DEFAULT 0,
            notes TEXT
        );

        -- Detailed parsed export data (replaces/extends exports)
        CREATE TABLE IF NOT EXISTS parsed_exports (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL,
            export_index INTEGER NOT NULL,
            export_name TEXT,
            class_name TEXT,
            serial_offset INTEGER,
            serial_size INTEGER,
            bytes_parsed INTEGER,
            bytes_unknown INTEGER,
            parse_status TEXT,
            uses_heuristics INTEGER DEFAULT 0,
            uses_skips INTEGER DEFAULT 0,
            gltf_exported INTEGER DEFAULT 0,
            session_id INTEGER,
            last_parsed_at TEXT,
            error_message TEXT,
            FOREIGN KEY (file_id) REFERENCES files(id),
            FOREIGN KEY (session_id) REFERENCES parse_sessions(id),
            UNIQUE(file_id, export_index)
        );

        -- Individual parsed fields (e.g., vertex count, bounds)
        CREATE TABLE IF NOT EXISTS parsed_fields (
            id INTEGER PRIMARY KEY,
            parsed_export_id INTEGER NOT NULL,
            field_path TEXT NOT NULL,
            field_type TEXT,
            value_int INTEGER,
            value_float REAL,
            value_text TEXT,
            array_index INTEGER DEFAULT 0,
            is_unknown INTEGER DEFAULT 0,
            FOREIGN KEY (parsed_export_id) REFERENCES parsed_exports(id),
            UNIQUE(parsed_export_id, field_path, array_index)
        );

        -- Track unknown byte regions for analysis
        CREATE TABLE IF NOT EXISTS unknown_regions (
            id INTEGER PRIMARY KEY,
            parsed_export_id INTEGER NOT NULL,
            offset_start INTEGER,
            offset_end INTEGER,
            raw_hex TEXT,
            context TEXT,
            FOREIGN KEY (parsed_export_id) REFERENCES parsed_exports(id)
        );
        
        -- Shaders table (shader/texture mappings)
        CREATE TABLE IF NOT EXISTS shaders (
            id INTEGER PRIMARY KEY,
            shader_name TEXT NOT NULL UNIQUE,
            diffuse_texture TEXT,
            normal_texture TEXT,
            specular_texture TEXT,
            properties TEXT
        );
        
        -- Mesh materials table
        CREATE TABLE IF NOT EXISTS mesh_materials (
            id INTEGER PRIMARY KEY,
            mesh_name TEXT NOT NULL,
            material_slot INTEGER DEFAULT 0,
            shader_name TEXT,
            FOREIGN KEY (shader_name) REFERENCES shaders(shader_name)
        );
        
        -- Prefabs table (resolved prefab components)
        CREATE TABLE IF NOT EXISTS prefabs (
            id INTEGER PRIMARY KEY,
            prefab_name TEXT NOT NULL,
            component_index INTEGER DEFAULT 0,
            mesh_name TEXT NOT NULL,
            local_x REAL DEFAULT 0,
            local_y REAL DEFAULT 0,
            local_z REAL DEFAULT 0,
            local_rot_pitch REAL DEFAULT 0,
            local_rot_yaw REAL DEFAULT 0,
            local_rot_roll REAL DEFAULT 0,
            scale REAL DEFAULT 1.0,
            UNIQUE(prefab_name, component_index)
        );
        
        -- Create indexes
        CREATE INDEX IF NOT EXISTS idx_exports_class ON exports(class_name);
        CREATE INDEX IF NOT EXISTS idx_exports_chunk ON exports(chunk_id);
        CREATE INDEX IF NOT EXISTS idx_files_ext ON files(extension);
        CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
        CREATE INDEX IF NOT EXISTS idx_mesh_index_name ON mesh_index(object_name);
        CREATE INDEX IF NOT EXISTS idx_mesh_index_class ON mesh_index(class_name);
        CREATE INDEX IF NOT EXISTS idx_names_chunk ON names(chunk_id);
        CREATE INDEX IF NOT EXISTS idx_imports_chunk ON imports(chunk_id);
        CREATE INDEX IF NOT EXISTS idx_properties_export ON properties(export_id);
        CREATE INDEX IF NOT EXISTS idx_properties_name ON properties(prop_name);
        CREATE INDEX IF NOT EXISTS idx_shaders_name ON shaders(shader_name);
        CREATE INDEX IF NOT EXISTS idx_mesh_materials_mesh ON mesh_materials(mesh_name);
        CREATE INDEX IF NOT EXISTS idx_prefabs_name ON prefabs(prefab_name);
    """)
    
    conn.commit()
    conn.close()
    
    print(f"   ✓ Database initialized: {config.DB_PATH}")
    print("   ✓ Tables: chunks, exports, files, terrain_chunks, mesh_index,")
    print("             names, imports, properties, shaders, mesh_materials, prefabs")


# =============================================================================
# FILE INDEXING
# =============================================================================

# Extension to category mapping
EXTENSION_CATEGORIES = {
    # Meshes
    'usx': 'Mesh',
    'ukx': 'Animation',
    # Maps
    'vgr': 'Map',
    'unr': 'Map',
    # Textures
    'utx': 'Texture',
    'mat': 'Texture',
    'dds': 'Texture',
    'tga': 'Texture',
    'png': 'Texture',
    'jpg': 'Texture',
    'bmp': 'Texture',
    # Audio
    'uax': 'Audio',
    'ogg': 'Audio',
    'wav': 'Audio',
    'mp3': 'Audio',
    # Scripts/Code
    'u': 'Script',
    'uc': 'Script',
    # Config/Data
    'ini': 'Config',
    'int': 'Config',
    'txt': 'Text',
    'xml': 'Config',
    'json': 'Config',
    # System
    'dll': 'System',
    'exe': 'System',
    'upk': 'Asset',
    'upl': 'Asset',
    # Prefabs
    'sgo': 'Asset',
    'prefab': 'Asset',
}


def print_progress_bar(iteration, total, prefix='', suffix='', decimals=1, length=40, fill='█', print_end="\r"):
    """
    Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        length      - Optional  : character length of bar (Int)
        fill        - Optional  : bar fill character (Str)
        print_end   - Optional  : end character (e.g. "\r", "\r\n") (Str)
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filled_length = int(length * iteration // total)
    bar = fill * filled_length + '-' * (length - filled_length)
    print(f'\r{prefix} |{bar}| {percent}% {suffix}', end=print_end)
    # Print New Line on Complete
    if iteration == total: 
        print()

def index_files(config):
    """Scan all files in the Assets directory and index them."""
    print("\n" + "=" * 60)
    print("STAGE 3: Indexing Asset Files")
    print("=" * 60)
    
    assets_path = config.ASSETS_PATH
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    # Clear existing file index
    cursor.execute("DELETE FROM files")
    
    # First, get total file count for progress bar
    print("   Scanning Assets directory...")
    all_files = []
    for root, dirs, files in os.walk(assets_path):
        for file in files:
            all_files.append(os.path.join(root, file))
    
    total = len(all_files)
    print(f"   Found {total} files. Indexing...")
    
    count = 0
    start_time = time.time()
    
    for file_path in all_files:
        file_name = os.path.basename(file_path)
        rel_path = os.path.relpath(file_path, assets_path)
        location = os.path.dirname(rel_path) or "./"
        
        # Get extension
        ext = Path(file_name).suffix.lower().lstrip('.')
        
        # Categorize
        category = EXTENSION_CATEGORIES.get(ext, 'Unknown')
        
        # Get file stats
        try:
            stat = os.stat(file_path)
            size_bytes = stat.st_size
            modified_time = datetime.fromtimestamp(stat.st_mtime).isoformat()
        except OSError:
            size_bytes = 0
            modified_time = None
        
        cursor.execute("""
            INSERT OR REPLACE INTO files 
            (file_name, file_path, location, extension, category, size_bytes, modified_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (file_name, rel_path, location, ext, category, size_bytes, modified_time))
        
        count += 1
        if count % 100 == 0 or count == total:
            print_progress_bar(count, total, prefix='   Progress:', suffix='Complete', length=40)
            if count % 1000 == 0:
                conn.commit()
    
    conn.commit()
    elapsed = time.time() - start_time
    
    # Print category summary
    cursor.execute("SELECT category, COUNT(*) FROM files GROUP BY category ORDER BY COUNT(*) DESC")
    categories = cursor.fetchall()
    
    print(f"\n   ✓ Indexed in {elapsed:.1f}s")
    print("\n   Category breakdown:")
    for cat, cnt in categories:
        print(f"     - {cat}: {cnt}")
    
    conn.close()


# =============================================================================
# CHUNK DATA EXPORT
# =============================================================================

def export_chunk_data(config):
    """Export chunk data using the extract_chunk_data script."""
    print("\n" + "=" * 60)
    print("STAGE 4: Exporting Chunk Data")
    print("=" * 60)
    run_extractor("Chunk Data", "extract_chunk_data.py", silent=False, args=["--silent"])


# =============================================================================
# OPTIONAL: FULL EXTRACTION
# =============================================================================

def run_extractor(name, script_name, silent=False, args=None):
    """Run an extractor script and report status."""
    import subprocess
    
    script_path = os.path.join(PROJECT_ROOT, "scripts", "extractors", script_name)
    
    if not os.path.exists(script_path):
        if not silent: print(f"   ⚠ Script not found: {script_path}")
        return False
    
    cmd = [sys.executable, script_path]
    if args:
        cmd.extend(args)
    
    try:
        # If silent, we suppress all output unless it fails
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=silent,
            text=True
        )
        if result.returncode != 0:
            if silent:
                print(f"\n   ❌ {name} failed with exit code {result.returncode}")
                if result.stderr:
                    last_error = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "Unknown error"
                    print(f"      Error: {last_error}")
            else:
                print(f"   ⚠ {name} returned non-zero: {result.returncode}")
            return False
        if not silent: print(f"   ✓ {name} completed")
        return True
    except Exception as e:
        if not silent: print(f"   ⚠ {name} failed: {e}")
        return False


def run_full_extraction(config):
    """Run terrain and mesh extraction (takes hours)."""
    print("\n" + "=" * 60)
    print("STAGE 8: Full Extraction (Terrain + Meshes)")
    print("=" * 60)
    print("   This stage extracts terrain and meshes from all chunks.")
    print("   WARNING: This can take several minutes!")
    
    print_progress_bar(0, 1, prefix='   Terrain:', suffix='Running...', length=40)
    if run_extractor("Terrain Extraction", "extract_all_terrain.py", silent=False, args=["--all", "--silent"]):
        print_progress_bar(1, 1, prefix='   Terrain:', suffix='Complete  ', length=40)
    
    print_progress_bar(0, 1, prefix='   StaticMesh:', suffix='Running...', length=40)
    if run_extractor("StaticMesh Pipeline", "staticmesh_pipeline.py", silent=False, args=["--silent"]):
        print_progress_bar(1, 1, prefix='   StaticMesh:', suffix='Complete  ', length=40)


# =============================================================================
# MAIN
# =============================================================================

def print_summary(config):
    """Print final summary and next steps."""
    print("\n" + "=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)
    
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()
    
    file_count = cursor.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chunk_count = cursor.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    export_count = cursor.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
    prop_count = cursor.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    
    conn.close()
    
    print(f"\n   Database: {config.DB_PATH}")
    print(f"   Files indexed: {file_count}")
    print(f"   Chunks processed: {chunk_count}")
    print(f"   Exports cataloged: {export_count}")
    print(f"   Properties parsed: {prop_count}")
    
    print("\n   Next steps:")
    print("   1. Start the server: python3 server.py")
    print("   2. Open Data Viewer: http://localhost:8000/data_viewer/")
    print("   3. Open Mesh Viewer: http://localhost:8000/mesh_viewer/")


def main():
    parser = argparse.ArgumentParser(description="Vanguard Asset Extractor Setup")
    parser.add_argument('--reset', action='store_true', help='Delete existing database and start fresh')
    parser.add_argument('--full', action='store_true', help='Run full extraction (Terrain + Meshes)')
    parser.add_argument('--skip-core', action='store_true', help='Skip core setup stages (1-4) if DB exists')
    
    # Granular stage flags
    parser.add_argument('--db', action='store_true', help='Stage 2: Initialize Database')
    parser.add_argument('--files', action='store_true', help='Stage 3: Index Files')
    parser.add_argument('--chunks', action='store_true', help='Stage 4: Export Chunk Data')
    parser.add_argument('--mesh-index', action='store_true', help='Stage 5: Index Mesh Objects')
    parser.add_argument('--textures', action='store_true', help='Stage 6: Build Texture Database')
    parser.add_argument('--properties', action='store_true', help='Stage 7: extract Object Properties')
    parser.add_argument('--terrain', action='store_true', help='Stage 8a: Extract Terrain')
    parser.add_argument('--meshes', action='store_true', help='Stage 8b: Extract StaticMeshes')
    parser.add_argument('--limit', type=int, default=0, help='Limit number of items to process in Stage 8')
    
    args = parser.parse_args()
    
    # Determine if we are running specific stages or default flow
    specific_stage = any([args.db, args.files, args.chunks, args.mesh_index, 
                         args.textures, args.properties, args.terrain, args.meshes])
    
    print("\n" + "=" * 60)
    print("VANGUARD ASSET EXTRACTOR - SETUP")
    print("=" * 60)
    
    # Stage 1: Validate config (always run)
    config = validate_config()
    
    # Handle --reset flag
    if args.reset:
        print("\n   ⚠ RESET MODE: Deleting existing database...")
        if os.path.exists(config.DB_PATH):
            os.remove(config.DB_PATH)
            print(f"   ✓ Deleted: {config.DB_PATH}")
        else:
            print("   (No existing database found)")
    
    # Default flow runs stages 2-7 unless skipped or specific stage selected
    should_run_defaults = not specific_stage and not args.skip_core
    
    # Stage 2: Initialize database
    if args.db or should_run_defaults:
        init_database(config)
    
    # Stage 3: Index files
    if args.files or should_run_defaults:
        index_files(config)
    
    # Stage 4: Export chunk data
    if args.chunks or should_run_defaults:
        export_chunk_data(config)
    
    # Stage 5: Index meshes
    if args.mesh_index or should_run_defaults:
        print("\n" + "=" * 60)
        print("STAGE 5: Indexing Mesh Objects")
        print("=" * 60)
        run_extractor("Mesh Index", "index_meshes.py", silent=False, args=["--silent"])
    
    # Stage 6: Build texture database
    if args.textures or should_run_defaults:
        print("\n" + "=" * 60)
        print("STAGE 6: Building Texture Database")
        print("=" * 60)
        run_extractor("Texture DB", "build_texture_db.py", silent=False, args=["--silent"])
    
    # Stage 7: Property Extraction
    if args.properties or should_run_defaults:
        print("\n" + "=" * 60)
        print("STAGE 7: Extracting Object Properties")
        print("=" * 60)
        print("   This parses class member values (Location, Mesh, etc.)")
        run_extractor("Property Extraction", "extract_properties.py", silent=False, args=["--silent"])
    
    # Stage 8: Full Extraction (Terrain + Meshes)
    # Only run if --full is set OR specific flags are set
    if args.full or args.terrain:
        print("\n" + "=" * 60)
        print("STAGE 8a: Terrain Extraction")
        print("=" * 60)
        print_progress_bar(0, 1, prefix='   Terrain:', suffix='Running...', length=40)
        if run_extractor("Terrain Extraction", "extract_all_terrain.py", silent=False, args=["--all", "--silent"]):
            print_progress_bar(1, 1, prefix='   Terrain:', suffix='Complete  ', length=40)

    if args.full or args.meshes:
        print("\n" + "=" * 60)
        print("STAGE 8b: StaticMesh Extraction")
        print("=" * 60)
        print_progress_bar(0, 1, prefix='   StaticMesh:', suffix='Running...', length=40)
        mesh_args = ["--silent"]
        if args.limit > 0:
            mesh_args.extend(["--limit", str(args.limit)])
        if run_extractor("StaticMesh Pipeline", "staticmesh_pipeline.py", silent=False, args=mesh_args):
            print_progress_bar(1, 1, prefix='   StaticMesh:', suffix='Complete  ', length=40)
    
    # Summary
    print_summary(config)


if __name__ == "__main__":
    main()
