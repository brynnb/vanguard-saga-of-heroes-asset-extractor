#!/usr/bin/env python3
"""
Unified StaticMesh Pipeline for Vanguard: Saga of Heroes

This script follows the PARSING_GUIDELINES.md and provides:
1. 100% byte-accurate parsing using construct library
2. Database storage of all parsed data (canonical: vanguard_files.db)  
3. glTF export for rendering in vanguard_viewer.html
4. Proper unknown region tracking

Usage:
    python staticmesh_pipeline.py                    # Parse all files
    python staticmesh_pipeline.py --file Ra44.usx   # Parse specific file
    python staticmesh_pipeline.py --export-only     # Only export glTF, no db update
"""

import os
import sys
import json
import glob
import sqlite3
import struct
import argparse
import math
from datetime import datetime
from dataclasses import dataclass, asdict

from typing import List, Optional, Dict, Any, Tuple


# Add paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(SCRIPT_DIR)
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)

sys.path.insert(0, os.path.join(SCRIPTS_DIR, 'lib'))
sys.path.insert(0, PROJECT_ROOT)

from ue2 import UE2Package
from staticmesh_construct import parse_staticmesh, find_none_terminator

# Configuration
import config

CANONICAL_DB = config.DB_PATH
MESHES_DIR = os.path.join(config.ASSETS_PATH, "Meshes")
OUTPUT_DIR = config.MESH_BUILDINGS_DIR  # Where glTF files go


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


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ParsedVertex:
    """Vertex data extracted from LOD model."""
    x: float
    y: float 
    z: float
    nx: float = 0.0
    ny: float = 0.0
    nz: float = 1.0
    u: float = 0.0
    v: float = 0.0


@dataclass 
class ParsedMesh:
    """Complete mesh data ready for glTF export and database storage."""
    name: str
    package_path: str
    export_index: int
    # Bounds
    bbox_min: Tuple[float, float, float]
    bbox_max: Tuple[float, float, float]
    bsphere_center: Tuple[float, float, float]
    bsphere_radius: float
    # LOD geometry
    lod_index: int
    vertices: List[ParsedVertex]
    indices: List[int]
    # Parsing metrics
    bytes_total: int
    bytes_parsed: int
    bytes_unknown: int
    coverage_pct: float
    uses_heuristics: bool
    uses_skips: bool
    # Metadata
    internal_version: int
    section_count: int
    parse_status: str
    error_message: Optional[str] = None
    unknown_regions: List[Dict] = None


# =============================================================================
# PARSING
# =============================================================================

def extract_vertices_from_lod(vertices_raw: bytes, count: int) -> List[ParsedVertex]:
    """
    Extract vertex data from raw LOD vertex bytes.
    Each Vanguard LOD vertex is 56 bytes:
    - Position: 3 floats (12 bytes)
    - Normal: 3 floats (12 bytes)  
    - UV: 2 floats (8 bytes)
    - Unknown: 24 bytes
    """
    vertices = []
    VERTEX_SIZE = 56
    
    for i in range(count):
        offset = i * VERTEX_SIZE
        if offset + 24 > len(vertices_raw):
            break
            
        # Position (12 bytes)
        x, y, z = struct.unpack('<fff', vertices_raw[offset:offset+12])
        
        # For now, assume simple normal and UV layout
        # This may need refinement based on actual data analysis
        nx, ny, nz = 0.0, 0.0, 1.0  # Default up
        u, v = 0.0, 0.0
        
        # Try to read normal if enough data
        if offset + 24 <= len(vertices_raw):
            nx, ny, nz = struct.unpack('<fff', vertices_raw[offset+12:offset+24])
            
        # Try to read UV if enough data
        if offset + 32 <= len(vertices_raw):
            u, v = struct.unpack('<ff', vertices_raw[offset+24:offset+32])
        
        vertices.append(ParsedVertex(x=x, y=y, z=z, nx=nx, ny=ny, nz=nz, u=u, v=v))
    
    return vertices


def parse_staticmesh_file(pkg_path: str) -> List[ParsedMesh]:
    """
    Parse all StaticMesh exports from a package file.
    Returns list of ParsedMesh objects.
    """
    meshes = []
    
    try:
        pkg = UE2Package(pkg_path)
    except Exception as e:
        print(f"  Error loading package: {e}")
        return meshes
    
    static_mesh_exports = [e for e in pkg.exports if e['class_name'] == 'StaticMesh']
    
    for exp in static_mesh_exports:
        try:
            data = pkg.get_export_data(exp)
            serial_offset = exp['serial_offset']
            
            result = parse_staticmesh(data, pkg.names, serial_offset)
            
            # Check for skip statuses
            status = result.get('parse_status', 'success')
            if status != 'success':
                meshes.append(ParsedMesh(
                    name=exp['object_name'],
                    package_path=pkg_path,
                    export_index=exp['index'],
                    bbox_min=(0, 0, 0),
                    bbox_max=(0, 0, 0),
                    bsphere_center=(0, 0, 0),
                    bsphere_radius=0,
                    lod_index=0,
                    vertices=[],
                    indices=[],
                    bytes_total=result['bytes_total'],
                    bytes_parsed=result['bytes_parsed'],
                    bytes_unknown=result['bytes_unknown'],
                    coverage_pct=0.0,
                    uses_heuristics=result['uses_heuristics'],
                    uses_skips=result['uses_skips'],
                    internal_version=0,
                    section_count=0,
                    parse_status=status,
                    error_message=result.get('error_message'),
                    unknown_regions=result.get('unknown_regions', [])
                ))
                continue
            
            # Extract bounds from core data
            core = result.get('data', {}).get('core', {})
            bbox = core.get('bounding_box', {})
            bbox_min_data = bbox.get('min', {})
            bbox_max_data = bbox.get('max', {})
            bsphere = core.get('bounding_sphere', {})
            center = bsphere.get('center', {})
            
            bbox_min = (
                float(bbox_min_data.get('x', 0)),
                float(bbox_min_data.get('y', 0)),
                float(bbox_min_data.get('z', 0))
            )
            bbox_max = (
                float(bbox_max_data.get('x', 0)),
                float(bbox_max_data.get('y', 0)),
                float(bbox_max_data.get('z', 0))
            )
            bsphere_center = (
                float(center.get('x', 0)),
                float(center.get('y', 0)),
                float(center.get('z', 0))
            )
            bsphere_radius = float(bsphere.get('radius', 0))
            
            # Extract LOD data
            lods = result.get('data', {}).get('lods', [])
            
            for lod_idx, lod in enumerate(lods):
                indices = lod.get('indices', [])
                vertex_count = lod.get('vertex_count', 0)
                
                # Get parsed vertices from the result
                parsed_verts = lod.get('vertices', [])
                vertices = []
                for v in parsed_verts:
                    vertices.append(ParsedVertex(
                        x=v.get('x', 0), y=v.get('y', 0), z=v.get('z', 0),
                        nx=v.get('nx', 0), ny=v.get('ny', 0), nz=v.get('nz', 1),
                        u=v.get('u', 0), v=v.get('v', 0)
                    ))

                
                meshes.append(ParsedMesh(
                    name=exp['object_name'],
                    package_path=pkg_path,
                    export_index=exp['index'],
                    bbox_min=bbox_min,
                    bbox_max=bbox_max,
                    bsphere_center=bsphere_center,
                    bsphere_radius=bsphere_radius,
                    lod_index=lod_idx,
                    vertices=vertices,
                    indices=indices,
                    bytes_total=result['bytes_total'],
                    bytes_parsed=result['bytes_parsed'],
                    bytes_unknown=result['bytes_unknown'],
                    coverage_pct=result['bytes_parsed'] / result['bytes_total'] * 100 if result['bytes_total'] > 0 else 0,
                    uses_heuristics=result['uses_heuristics'],
                    uses_skips=result['uses_skips'],
                    internal_version=core.get('internal_version', 0),
                    section_count=core.get('section_count', 0),
                    parse_status='complete',
                    unknown_regions=result.get('unknown_regions', [])
                ))
                
        except Exception as e:
            print(f"  Error parsing {exp['object_name']}: {e}")
            meshes.append(ParsedMesh(
                name=exp['object_name'],
                package_path=pkg_path,
                export_index=exp['index'],
                bbox_min=(0, 0, 0),
                bbox_max=(0, 0, 0),
                bsphere_center=(0, 0, 0),
                bsphere_radius=0,
                lod_index=0,
                vertices=[],
                indices=[],
                bytes_total=0,
                bytes_parsed=0,
                bytes_unknown=0,
                coverage_pct=0.0,
                uses_heuristics=False,
                uses_skips=False,
                internal_version=0,
                section_count=0,
                parse_status='error',
                error_message=str(e)
            ))
    
    return meshes


# =============================================================================
# DATABASE
# =============================================================================

def get_or_create_file_id(conn: sqlite3.Connection, pkg_path: str) -> int:
    """Get file_id from files table, or create if not exists."""
    cursor = conn.cursor()
    
    # Ensure we use relative paths for the database
    rel_path = pkg_path
    if os.path.isabs(pkg_path):
        import config
        try:
            rel_path = os.path.relpath(pkg_path, config.ASSETS_PATH)
        except ValueError:
            pass 

    # Check if exists
    cursor.execute("SELECT id FROM files WHERE file_path = ?", (rel_path,))
    row = cursor.fetchone()
    if row:
        return row[0]
    
    # Create new entry
    filename = os.path.basename(pkg_path)
    parent = os.path.dirname(rel_path)
    # Use absolute path for os.path.getsize
    abs_path = pkg_path if os.path.isabs(pkg_path) else os.path.join(config.ASSETS_PATH, pkg_path)
    size = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
    
    # Determine file type
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    
    cursor.execute("""
        INSERT INTO files (file_path, file_name, size_bytes, location, extension, category)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (rel_path, filename, size, parent, ext, 'Mesh'))
    
    conn.commit()
    return cursor.lastrowid
    

def create_parse_session(conn: sqlite3.Connection) -> int:
    """Create a new parse session and return its ID."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO parse_sessions (started_at, parser_version)
        VALUES (?, ?)
    """, (datetime.now().isoformat(), "staticmesh_construct_v1"))
    conn.commit()
    return cursor.lastrowid


def store_parsed_mesh(conn: sqlite3.Connection, mesh: ParsedMesh, file_id: int, session_id: int):
    """Store parsed mesh data in the database."""
    cursor = conn.cursor()
    
    # Map parse_status to allowed values
    status_map = {
        'success': 'complete',
        'complete': 'complete',
        'error': 'error',
        'skipped_variant_format': 'error',
        'skipped_populated_stream': 'error',
    }
    db_status = status_map.get(mesh.parse_status, 'error')
    
    # Insert or update parsed_exports
    cursor.execute("""
        INSERT INTO parsed_exports (
            file_id, export_index, export_name, class_name,
            serial_offset, serial_size, bytes_parsed, bytes_unknown,
            parse_status, uses_heuristics, uses_skips,
            session_id, last_parsed_at, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id, export_index) DO UPDATE SET
            bytes_parsed = excluded.bytes_parsed,
            bytes_unknown = excluded.bytes_unknown,
            parse_status = excluded.parse_status,
            uses_heuristics = excluded.uses_heuristics,
            uses_skips = excluded.uses_skips,
            session_id = excluded.session_id,
            last_parsed_at = excluded.last_parsed_at,
            error_message = excluded.error_message
    """, (
        file_id,
        mesh.export_index,
        mesh.name,
        'StaticMesh',
        0,  # serial_offset - we'd need to track this
        mesh.bytes_total,
        mesh.bytes_parsed,
        mesh.bytes_unknown,
        db_status,
        1 if mesh.uses_heuristics else 0,
        1 if mesh.uses_skips else 0,
        session_id,
        datetime.now().isoformat(),
        mesh.error_message
    ))
    
    parsed_export_id = cursor.lastrowid
    
    # Store key fields
    fields_to_store = [
        ('bounds.bbox_min.x', 'float', mesh.bbox_min[0]),
        ('bounds.bbox_min.y', 'float', mesh.bbox_min[1]),
        ('bounds.bbox_min.z', 'float', mesh.bbox_min[2]),
        ('bounds.bbox_max.x', 'float', mesh.bbox_max[0]),
        ('bounds.bbox_max.y', 'float', mesh.bbox_max[1]),
        ('bounds.bbox_max.z', 'float', mesh.bbox_max[2]),
        ('internal_version', 'int32', mesh.internal_version),
        ('section_count', 'int32', mesh.section_count),
        ('lod_index', 'int32', mesh.lod_index),
        ('vertex_count', 'int32', len(mesh.vertices)),
        ('index_count', 'int32', len(mesh.indices)),
        ('triangle_count', 'int32', len(mesh.indices) // 3),
    ]
    
    for field_path, field_type, value in fields_to_store:
        cursor.execute("""
            INSERT INTO parsed_fields (
                parsed_export_id, field_path, field_type,
                value_int, value_float, is_unknown
            ) VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(parsed_export_id, field_path, array_index) DO UPDATE SET
                value_int = excluded.value_int,
                value_float = excluded.value_float
        """, (
            parsed_export_id,
            field_path,
            field_type,
            int(value) if field_type == 'int32' else None,
            float(value) if field_type == 'float' else None
        ))
    
    # Store unknown regions
    if mesh.unknown_regions:
        for region in mesh.unknown_regions:
            # Get raw hex from the region if available
            raw_hex = region.get('raw_hex', '00')  # Default to single byte if not available
            cursor.execute("""
                INSERT INTO unknown_regions (
                    parsed_export_id, offset_start, offset_end, raw_hex, context
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                parsed_export_id,
                region.get('offset', 0),
                region.get('offset', 0) + region.get('size', 0),
                raw_hex,
                region.get('name', 'unknown')
            ))

    
    conn.commit()


# =============================================================================
# GLTF EXPORT
# =============================================================================

def mesh_to_gltf(mesh: ParsedMesh, output_path: str) -> bool:
    """
    Export ParsedMesh to glTF format.
    Returns True on success.
    """
    if not mesh.vertices or not mesh.indices:
        return False
    
    # Build glTF structure
    gltf = {
        "asset": {"version": "2.0", "generator": "Vanguard StaticMesh Pipeline"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": mesh.name}],
        "meshes": [{
            "primitives": [{
                "attributes": {"POSITION": 0},
                "indices": 1,
                "mode": 4  # TRIANGLES
            }],
            "name": mesh.name
        }],
        "accessors": [],
        "bufferViews": [],
        "buffers": []
    }
    
    # Build binary buffer
    import struct
    import base64
    
    buffer_data = bytearray()
    
    # Positions
    pos_start = len(buffer_data)
    min_pos = [float('inf')] * 3
    max_pos = [float('-inf')] * 3
    
    for v in mesh.vertices:
        # Sanitize NaN/Inf values which are invalid in JSON
        vx = v.x if math.isfinite(v.x) else 0.0
        vy = v.y if math.isfinite(v.y) else 0.0
        vz = v.z if math.isfinite(v.z) else 0.0
        
        # Apply Vanguard (Z-up) -> glTF (Y-up) coordinate swizzle at export time
        # Vanguard: X=Right, Y=Forward, Z=Up
        # glTF:     X=Right, Y=Up,      Z=Forward (negated for right-hand convention)
        gx = vx
        gy = vz   # Vanguard height (Z) becomes glTF height (Y)
        gz = -vy  # Vanguard forward (Y) becomes glTF depth (Z), negated
        
        buffer_data.extend(struct.pack('<fff', gx, gy, gz))
        min_pos[0] = min(min_pos[0], gx)
        min_pos[1] = min(min_pos[1], gy)
        min_pos[2] = min(min_pos[2], gz)
        max_pos[0] = max(max_pos[0], gx)
        max_pos[1] = max(max_pos[1], gy)
        max_pos[2] = max(max_pos[2], gz)

    
    pos_end = len(buffer_data)
    
    # Indices
    idx_start = len(buffer_data)
    for idx in mesh.indices:
        buffer_data.extend(struct.pack('<H', idx))
    idx_end = len(buffer_data)
    
    # Add buffer views
    gltf["bufferViews"] = [
        {"buffer": 0, "byteOffset": pos_start, "byteLength": pos_end - pos_start, "target": 34962},
        {"buffer": 0, "byteOffset": idx_start, "byteLength": idx_end - idx_start, "target": 34963}
    ]
    
    # Add accessors
    gltf["accessors"] = [
        {
            "bufferView": 0,
            "componentType": 5126,  # FLOAT
            "count": len(mesh.vertices),
            "type": "VEC3",
            "min": min_pos,
            "max": max_pos
        },
        {
            "bufferView": 1,
            "componentType": 5123,  # UNSIGNED_SHORT
            "count": len(mesh.indices),
            "type": "SCALAR"
        }
    ]
    
    # Encode buffer as base64 data URI
    b64_data = base64.b64encode(buffer_data).decode('utf-8')
    gltf["buffers"] = [{
        "uri": f"data:application/octet-stream;base64,{b64_data}",
        "byteLength": len(buffer_data)
    }]
    
    # Write file
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(gltf, f)
    
    return True


# =============================================================================
# MAIN PIPELINE
# =============================================================================

def process_package(pkg_path: str, conn: Optional[sqlite3.Connection], session_id: int, 
                   export_gltf: bool = True, output_dir: str = None) -> Dict[str, int]:
    """
    Process a single package file through the complete pipeline.
    Returns stats dict.
    """
    stats = {'success': 0, 'error': 0, 'skipped': 0, 'exported': 0}
    
    file_id = get_or_create_file_id(conn, pkg_path) if conn else 0
    meshes = parse_staticmesh_file(pkg_path)
    
    for mesh in meshes:
        # Store in database
        if conn:
            store_parsed_mesh(conn, mesh, file_id, session_id)
        
        if mesh.parse_status == 'complete':
            stats['success'] += 1
            
            # Export glTF if requested
            if export_gltf and output_dir and mesh.vertices and mesh.indices:
                pkg_name = os.path.splitext(os.path.basename(pkg_path))[0]
                gltf_path = os.path.join(output_dir, pkg_name, f"{mesh.name}.gltf")
                if mesh_to_gltf(mesh, gltf_path):
                    stats['exported'] += 1
                    # Mark as exported in database
                    if conn:
                        cursor = conn.cursor()
                        cursor.execute("""
                            UPDATE parsed_exports 
                            SET gltf_exported = 1 
                            WHERE file_id = ? AND export_index = ?
                        """, (file_id, mesh.export_index))
                        conn.commit()

        elif 'skipped' in mesh.parse_status:
            stats['skipped'] += 1
        else:
            stats['error'] += 1

    
    return stats


def run_pipeline(file_pattern: str = None, export_gltf: bool = True, export_only: bool = False, limit: int = 0, silent: bool = False):
    """
    Run the complete StaticMesh parsing pipeline.
    """
    if not silent:
        print("=" * 60)
        print("Vanguard StaticMesh Pipeline")
        print("=" * 60)
        print(f"Database: {CANONICAL_DB}")
        print(f"Meshes Dir: {MESHES_DIR}")
        print(f"Output Dir: {OUTPUT_DIR}")
        if export_only:
            print("Mode: EXPORT-ONLY (Skipping database updates)")
        print()
    
    # Find files to process
    if file_pattern:
        pattern = file_pattern if file_pattern.endswith('.usx') else file_pattern + "*.usx"
        files = glob.glob(os.path.join(MESHES_DIR, pattern))
    else:
        files = glob.glob(os.path.join(MESHES_DIR, "*.usx"))
    
    if limit > 0:
        files = files[:limit]
    
    if not silent:
        print(f"Found {len(files)} files to process")
        print()
    
    # Connect to database
    conn = None
    session_id = None
    if not export_only:
        conn = sqlite3.connect(CANONICAL_DB)
        session_id = create_parse_session(conn)
        if not silent:
            print(f"Created parse session: {session_id}")
            print()
    
    # Process files
    total_stats = {'success': 0, 'error': 0, 'skipped': 0, 'exported': 0}
    
    total_files = len(files)
    for i, pkg_path in enumerate(files):
        # Show progress bar if many files
        if total_files > 5:
            print_progress_bar(i + 1, total_files, prefix='   Progress:', suffix=f'({i+1}/{total_files})', length=40)
        elif not silent:
            print(f"[{i+1}/{total_files}] Processing {os.path.basename(pkg_path)}...")
        
        try:
            # When export_only, we don't pass conn
            stats = process_package(
                pkg_path, 
                conn if not export_only else None, 
                session_id if not export_only else 0, 
                export_gltf=export_gltf, 
                output_dir=OUTPUT_DIR
            )
            for key in total_stats:
                total_stats[key] += stats[key]
            
            if not silent and total_files <= 5:
                print(f" OK ({stats['success']} meshes)")
        except Exception as e:
            if not silent:
                print(f"\n ERROR processing {os.path.basename(pkg_path)}: {e}")
            total_stats['error'] += 1
    
    # Update session as complete (skip if export-only mode)
    if conn is not None:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE parse_sessions 
            SET completed_at = ?, 
                files_processed = ?,
                exports_processed = ?,
                total_bytes_parsed = ?
            WHERE id = ?
        """, (
            datetime.now().isoformat(),
            len(files),
            total_stats['success'] + total_stats['error'] + total_stats['skipped'],
            total_stats['success'],
            session_id
        ))
        conn.commit()
        conn.close()
    
    # Print summary
    if not silent:
        print()
        print("=" * 60)
        print("Pipeline Complete")
        print("=" * 60)
        print(f"Files Processed: {len(files)}")
        print(f"Meshes Parsed:   {total_stats['success']}")
        print(f"Meshes Skipped:  {total_stats['skipped']}")
        print(f"Meshes Failed:   {total_stats['error']}")
        print(f"glTF Exported:   {total_stats['exported']}")
        print()
    
    if not silent:
        success_rate = total_stats['success'] / (total_stats['success'] + total_stats['error'] + total_stats['skipped']) * 100 if (total_stats['success'] + total_stats['error'] + total_stats['skipped']) > 0 else 0
        print(f"Success Rate: {success_rate:.1f}%")


def main():
    parser = argparse.ArgumentParser(description="Vanguard StaticMesh Pipeline")
    parser.add_argument('--file', '-f', help='Specific file pattern to process (e.g., "Ra44*.usx")')
    parser.add_argument('--export-only', action='store_true', help='Only export glTF, skip DB updates')
    parser.add_argument('--limit', '-n', type=int, default=0, help='Limit number of files to process')
    parser.add_argument('--silent', action='store_true', help='Suppress all output except errors')
    
    args = parser.parse_args()
    
    run_pipeline(
        file_pattern=args.file,
        export_gltf=True,
        export_only=args.export_only,
        limit=args.limit,
        silent=args.silent
    )




if __name__ == "__main__":
    main()
