#!/usr/bin/env python3
"""
Simple HTTP server to serve SQLite data for the data viewer.

Usage:
    python data_server.py

Then open http://localhost:8765 in your browser.
"""

import http.server
import socketserver
import json
import sqlite3
import os
import urllib.parse
import sys
import subprocess

# Import config
try:
    from config import DB_PATH, RENDERER_ROOT as STATIC_DIR
except ImportError:
    # Fallback if config not found (e.g. running standalone without path setup)
    print("Warning: Could not import config.py. Using default paths.")
    STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(STATIC_DIR, "output/data/vanguard_data.db")

PORT = 8000

# Extractor Registry - metadata for each script
EXTRACTORS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extractors")
EXTRACTORS = {
    "export_chunk_data": {
        "name": "Export Chunk Data",
        "script": "export_chunk_data.py",
        "description": "Parse .vgr map chunks and populate SQLite database with exports, names, and properties.",
        "inputs": ".vgr files from Assets/Maps/",
        "outputs": "vanguard_data.db",
        "category": "Data"
    },
    "extract_staticmesh": {
        "name": "Extract StaticMesh",
        "script": "extract_staticmesh.py",
        "description": "Native Python StaticMesh extractor for Vanguard .usx packages.",
        "inputs": ".usx mesh packages",
        "outputs": ".gltf files in output/buildings/",
        "category": "Mesh"
    },
    "extract_terrain_grid": {
        "name": "Extract Terrain Grid",
        "script": "extract_terrain_grid.py",
        "description": "Extract terrain heightmaps and textures for a grid of chunks.",
        "inputs": ".vgr files",
        "outputs": "Terrain folder with .gltf and textures",
        "category": "Terrain"
    },
    "master_harvest": {
        "name": "Master Harvest",
        "script": "master_harvest.py",
        "description": "Extract all missing meshes referenced in the database.",
        "inputs": "mesh_index.sqlite, vanguard_data.db",
        "outputs": ".gltf files",
        "category": "Mesh"
    },
    "build_texture_db": {
        "name": "Build Texture DB",
        "script": "build_texture_db.py",
        "description": "Build texture/shader mapping database from extracted assets.",
        "inputs": "PNG and .mat files",
        "outputs": "texture_db.json",
        "category": "Texture"
    },
    "batch_export_meshes": {
        "name": "Batch Export Meshes",
        "script": "batch_export_meshes.py",
        "description": "Batch export all mesh packages using umodel.",
        "inputs": "All .usx files",
        "outputs": ".gltf files in output/buildings/",
        "category": "Mesh"
    },
    "index_meshes": {
        "name": "Index Meshes",
        "script": "index_meshes.py",
        "description": "Index all meshes into mesh_index.sqlite for fast lookup.",
        "inputs": ".usx packages",
        "outputs": "mesh_index.sqlite",
        "category": "Data"
    },
    "resolve_prefabs": {
        "name": "Resolve Prefabs",
        "script": "resolve_prefabs.py",
        "description": "Resolve prefab references from SGO archive files.",
        "inputs": "binaryprefabs.sgo",
        "outputs": "Prefab placement data",
        "category": "Data"
    },
    "extract_bsp": {
        "name": "Extract BSP",
        "script": "extract_bsp.py",
        "description": "Parse UE2 package structure and extract BSP/brush geometry.",
        "inputs": ".vgr files",
        "outputs": "BSP geometry data",
        "category": "Data"
    },
    "extract_heightmap": {
        "name": "Extract Heightmap",
        "script": "extract_heightmap.py",
        "description": "Extract raw heightmap data from terrain chunks.",
        "inputs": ".vgr files",
        "outputs": "Heightmap images",
        "category": "Terrain"
    }
}


class DataHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/query":
            self.handle_query(parsed.query)
        elif parsed.path == "/api/class_summary":
            self.handle_class_summary()
        elif parsed.path == "/api/chunks":
            self.handle_chunks()
        elif parsed.path == "/api/length_distribution":
            self.handle_length_distribution(parsed.query)
        elif parsed.path == "/api/exports":
            self.handle_exports(parsed.query)
        elif parsed.path == "/api/names":
            self.handle_names(parsed.query)
        elif parsed.path == "/api/properties":
            self.handle_properties(parsed.query)
        elif parsed.path == "/api/property_summary":
            self.handle_property_summary(parsed.query)
        elif parsed.path == "/api/export_detail":
            self.handle_export_detail(parsed.query)
        elif parsed.path == "/api/watervolume_chain":
            self.handle_watervolume_chain(parsed.query)
        elif parsed.path == "/api/files":
            self.handle_files(parsed.query)
        elif parsed.path == "/api/extractors":
            self.handle_extractors()
        elif parsed.path == "/api/run_extractor":
            self.handle_run_extractor(parsed.query)
        elif parsed.path == "/api/file_structure":
            self.handle_file_structure(parsed.query)
        elif parsed.path == "/api/parse_status":
            self.handle_parse_status()
        elif parsed.path == "/api/class_coverage":
            self.handle_class_coverage()
        elif parsed.path == "/api/parsed_exports":
            self.handle_parsed_exports(parsed.query)
        else:
            super().do_GET()


    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_error_json(self, message):
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode())

    def get_db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def handle_query(self, query_string):
        """Execute arbitrary SQL query."""
        params = urllib.parse.parse_qs(query_string)
        sql = params.get("sql", [""])[0]

        if not sql:
            self.send_error_json("No SQL query provided")
            return

        # Basic safety check - only allow SELECT
        if not sql.strip().upper().startswith("SELECT"):
            self.send_error_json("Only SELECT queries allowed")
            return

        try:
            conn = self.get_db()
            cursor = conn.execute(sql)
            columns = [desc[0] for desc in cursor.description]
            rows = [dict(row) for row in cursor.fetchall()[:1000]]  # Limit to 1000
            conn.close()

            self.send_json({"columns": columns, "rows": rows, "count": len(rows)})
        except Exception as e:
            self.send_error_json(str(e))

    def handle_class_summary(self):
        """Get class summary statistics."""
        conn = self.get_db()
        cursor = conn.execute(
            """
            SELECT 
                class_name,
                COUNT(*) as export_count,
                COUNT(DISTINCT data_length) as length_variants,
                MIN(data_length) as min_length,
                MAX(data_length) as max_length,
                COUNT(DISTINCT chunk_id) as chunk_count
            FROM exports
            GROUP BY class_name
            ORDER BY export_count DESC
        """
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json(rows)

    def handle_chunks(self):
        """Get list of all chunks."""
        conn = self.get_db()
        cursor = conn.execute(
            """
            SELECT id, filename, export_count, name_count, parsed_at
            FROM chunks
            ORDER BY filename
        """
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json(rows)

    def handle_length_distribution(self, query_string):
        """Get length distribution for a class."""
        params = urllib.parse.parse_qs(query_string)
        class_name = params.get("class", [""])[0]

        conn = self.get_db()

        if class_name:
            cursor = conn.execute(
                """
                SELECT 
                    data_length,
                    COUNT(*) as count,
                    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) as pct
                FROM exports
                WHERE class_name = ?
                GROUP BY data_length
                ORDER BY count DESC
            """,
                (class_name,),
            )
        else:
            cursor = conn.execute(
                """
                SELECT 
                    class_name,
                    data_length,
                    COUNT(*) as count
                FROM exports
                GROUP BY class_name, data_length
                ORDER BY class_name, count DESC
            """
            )

        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json(rows)

    def handle_exports(self, query_string):
        """Get exports with filters."""
        params = urllib.parse.parse_qs(query_string)
        class_name = params.get("class", [""])[0]
        chunk_id = params.get("chunk_id", [""])[0]
        data_length = params.get("length", [""])[0]
        limit = int(params.get("limit", ["100"])[0])
        offset = int(params.get("offset", ["0"])[0])

        conditions = []
        values = []

        if class_name:
            conditions.append("e.class_name = ?")
            values.append(class_name)
        if chunk_id:
            conditions.append("e.chunk_id = ?")
            values.append(int(chunk_id))
        if data_length:
            conditions.append("e.data_length = ?")
            values.append(int(data_length))

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        conn = self.get_db()

        # Get total count
        count_cursor = conn.execute(
            f"""
            SELECT COUNT(*) FROM exports e WHERE {where_clause}
        """,
            values,
        )
        total = count_cursor.fetchone()[0]

        # Get rows
        cursor = conn.execute(
            f"""
            SELECT 
                e.id, e.export_index, e.object_name, e.class_name,
                e.data_length, e.position_x, e.position_y, e.position_z,
                e.header_bytes, c.filename as chunk_name
            FROM exports e
            JOIN chunks c ON e.chunk_id = c.id
            WHERE {where_clause}
            ORDER BY e.object_name
            LIMIT ? OFFSET ?
        """,
            values + [limit, offset],
        )

        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        self.send_json({"rows": rows, "total": total, "limit": limit, "offset": offset})

    def handle_names(self, query_string):
        """Get names from a chunk."""
        params = urllib.parse.parse_qs(query_string)
        chunk_id = params.get("chunk_id", ["1"])[0]
        search = params.get("search", [""])[0]

        conn = self.get_db()

        if search:
            cursor = conn.execute(
                """
                SELECT name_index, name
                FROM names
                WHERE chunk_id = ? AND name LIKE ?
                ORDER BY name_index
                LIMIT 500
            """,
                (int(chunk_id), f"%{search}%"),
            )
        else:
            cursor = conn.execute(
                """
                SELECT name_index, name
                FROM names
                WHERE chunk_id = ?
                ORDER BY name_index
                LIMIT 500
            """,
                (int(chunk_id),),
            )

        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json(rows)

    def handle_properties(self, query_string):
        """Get properties for an export or class."""
        params = urllib.parse.parse_qs(query_string)
        export_id = params.get("export_id", [""])[0]
        class_name = params.get("class", [""])[0]
        prop_name = params.get("prop_name", [""])[0]

        conn = self.get_db()

        if export_id:
            cursor = conn.execute(
                """
                SELECT prop_name, prop_type, prop_size, value_text, value_hex
                FROM properties
                WHERE export_id = ?
                ORDER BY id
            """,
                (int(export_id),),
            )
        elif class_name and prop_name:
            cursor = conn.execute(
                """
                SELECT p.prop_name, p.prop_type, p.value_text, p.value_hex,
                       e.object_name, c.filename
                FROM properties p
                JOIN exports e ON p.export_id = e.id
                JOIN chunks c ON e.chunk_id = c.id
                WHERE e.class_name = ? AND p.prop_name = ?
                LIMIT 100
            """,
                (class_name, prop_name),
            )
        else:
            self.send_json([])
            return

        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json(rows)

    def handle_property_summary(self, query_string):
        """Get property summary for a class."""
        params = urllib.parse.parse_qs(query_string)
        class_name = params.get("class", [""])[0]

        conn = self.get_db()

        if class_name:
            cursor = conn.execute(
                """
                SELECT p.prop_name, p.prop_type, COUNT(*) as count,
                       COUNT(DISTINCT p.value_text) as unique_values
                FROM properties p
                JOIN exports e ON p.export_id = e.id
                WHERE e.class_name = ?
                GROUP BY p.prop_name, p.prop_type
                ORDER BY count DESC
            """,
                (class_name,),
            )
        else:
            cursor = conn.execute(
                """
                SELECT prop_name, prop_type, COUNT(*) as count
                FROM properties
                GROUP BY prop_name, prop_type
                ORDER BY count DESC
                LIMIT 100
            """
            )

        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json(rows)

    def handle_export_detail(self, query_string):
        """Get detailed info for a single export including properties."""
        params = urllib.parse.parse_qs(query_string)
        export_id = params.get("id", [""])[0]

        if not export_id:
            self.send_error_json("No export ID provided")
            return

        conn = self.get_db()

        # Get export info
        cursor = conn.execute(
            """
            SELECT e.*, c.filename as chunk_name
            FROM exports e
            JOIN chunks c ON e.chunk_id = c.id
            WHERE e.id = ?
        """,
            (int(export_id),),
        )
        export = cursor.fetchone()

        if not export:
            self.send_error_json("Export not found")
            return

        export_dict = dict(export)

        # Get properties
        cursor = conn.execute(
            """
            SELECT prop_name, prop_type, prop_size, array_index, 
                   struct_name, value_text, value_hex
            FROM properties
            WHERE export_id = ?
            ORDER BY id
        """,
            (int(export_id),),
        )
        properties = [dict(row) for row in cursor.fetchall()]

        conn.close()

        self.send_json({"export": export_dict, "properties": properties})

    def handle_watervolume_chain(self, query_string):
        """Get WaterVolume -> Model -> Polys chain for a WaterVolume export."""
        params = urllib.parse.parse_qs(query_string)
        export_id = params.get("id", [""])[0]

        if not export_id:
            self.send_error_json("No export ID provided")
            return

        conn = self.get_db()

        # Get the export info
        cursor = conn.execute(
            """
            SELECT e.*, c.filename as chunk_name
            FROM exports e
            JOIN chunks c ON e.chunk_id = c.id
            WHERE e.id = ?
        """,
            (int(export_id),),
        )
        export = cursor.fetchone()

        if not export:
            self.send_error_json("Export not found")
            return

        export_dict = dict(export)
        chunk_id = export_dict["chunk_id"]

        # Find Model exports in the same chunk
        cursor = conn.execute(
            """
            SELECT id, export_index, object_name, data_length
            FROM exports
            WHERE chunk_id = ? AND class_name = 'Model'
            ORDER BY export_index
        """,
            (chunk_id,),
        )
        models = [dict(row) for row in cursor.fetchall()]

        # Find Polys exports in the same chunk
        cursor = conn.execute(
            """
            SELECT id, export_index, object_name, data_length
            FROM exports
            WHERE chunk_id = ? AND class_name = 'Polys'
            ORDER BY export_index
        """,
            (chunk_id,),
        )
        polys = [dict(row) for row in cursor.fetchall()]

        conn.close()

        # The actual WaterVolume->Model->Polys chain is embedded in raw bytes
        # We return the available Models and Polys for reference
        self.send_json(
            {
                "export": export_dict,
                "models": models,
                "polys": polys,
                "note": "WaterVolume references Models via compact indices in raw data. Models reference Polys similarly.",
            }
        )


    def handle_files(self, query_string):
        """Get files from the file index."""
        params = urllib.parse.parse_qs(query_string)
        category = params.get("category", [""])[0]
        extension = params.get("extension", [""])[0]
        search = params.get("search", [""])[0]
        sort = params.get("sort", ["size_bytes"])[0]
        order = params.get("order", ["desc"])[0]
        limit = int(params.get("limit", ["100"])[0])
        offset = int(params.get("offset", ["0"])[0])

        # Use the main DB connection
        conn = self.get_db()

        conditions = []
        values = []

        if category:
            conditions.append("category = ?")
            values.append(category)
        if extension:
            conditions.append("extension = ?")
            values.append(extension)
        if search:
            conditions.append("file_name LIKE ?")
            values.append(f"%{search}%")

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        # Validate sort column to prevent SQL injection
        allowed_sorts = ["file_name", "size_bytes", "extension", "category", "location", "modified_time"]
        if sort not in allowed_sorts:
            sort = "size_bytes"
            
        order_dir = "DESC" if order.lower() == "desc" else "ASC"

        # Get total count
        count_cursor = conn.execute(
            f"""
            SELECT COUNT(*) FROM files WHERE {where_clause}
        """,
            values,
        )
        total = count_cursor.fetchone()[0]

        # Get rows
        cursor = conn.execute(
            f"""
            SELECT *
            FROM files
            WHERE {where_clause}
            ORDER BY {sort} {order_dir}
            LIMIT ? OFFSET ?
        """,
            values + [limit, offset],
        )

        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        self.send_json({"rows": rows, "total": total, "limit": limit, "offset": offset})

    def handle_extractors(self):
        """List all available extractors with metadata."""
        extractors_list = []
        for key, info in EXTRACTORS.items():
            extractors_list.append({
                "id": key,
                "name": info["name"],
                "script": info["script"],
                "description": info["description"],
                "inputs": info["inputs"],
                "outputs": info["outputs"],
                "category": info.get("category", "Other")
            })
        self.send_json(extractors_list)

    def handle_run_extractor(self, query_string):
        """Run an extractor script and return output."""
        params = urllib.parse.parse_qs(query_string)
        extractor_id = params.get("id", [""])[0]
        
        if not extractor_id or extractor_id not in EXTRACTORS:
            self.send_error_json(f"Unknown extractor: {extractor_id}")
            return
            
        extractor = EXTRACTORS[extractor_id]
        script_path = os.path.join(EXTRACTORS_DIR, extractor["script"])
        
        if not os.path.exists(script_path):
            self.send_error_json(f"Script not found: {extractor['script']}")
            return
        
        # Run the script
        try:
            result = subprocess.run(
                ["python3", script_path],
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
                cwd=os.path.dirname(EXTRACTORS_DIR)  # Run from renderer/ dir
            )
            
            self.send_json({
                "success": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "extractor": extractor_id
            })
        except subprocess.TimeoutExpired:
            self.send_json({
                "success": False,
                "returncode": -1,
                "stdout": "",
                "stderr": "Script timed out after 5 minutes",
                "extractor": extractor_id
            })
        except Exception as e:
            self.send_json({
                "success": False,
                "returncode": -1,
                "stdout": "",
                "stderr": str(e),
                "extractor": extractor_id
            })

    def handle_file_structure(self, query_string):
        """Get hierarchical binary structure of a file."""
        params = urllib.parse.parse_qs(query_string)
        file_path = params.get("path", [""])[0]
        
        if not file_path:
            self.send_error_json("No file path provided")
            return
        
        # Handle relative paths by prepending ASSETS_PATH parent
        if not file_path.startswith("/"):
            # Config ASSETS_PATH is .../Vanguard EMU/Assets
            # Relative paths like "Assets/Meshes/..." need parent dir
            from config import ASSETS_PATH
            assets_parent = os.path.dirname(ASSETS_PATH)  # .../Vanguard EMU
            file_path = os.path.join(assets_parent, file_path)
        
        if not os.path.exists(file_path):
            self.send_error_json(f"File not found: {file_path}")
            return
        
        try:
            from extractors.parse_structure import parse_file
            result = parse_file(file_path)
            self.send_json(result)
        except ImportError as e:
            self.send_error_json(f"Parser module not available: {e}")
        except Exception as e:
            self.send_error_json(f"Parse error: {e}")

    # Get overall parsing status summary.
    def handle_parse_status(self):
        """Return parsing statistics from parsing_summary view."""
        try:
            # Use canonical database for parsing status
            canonical_db = "/Users/brynnbateman/Downloads/Vanguard EMU/vanguard_files.db"
            conn = sqlite3.connect(canonical_db)
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT class_name, total_exports, complete_count, error_count, 
                       pending_count, avg_coverage, total_bytes_parsed, 
                       total_bytes_unknown, gltf_exported_count
                FROM parsing_summary
            """)
            rows = cursor.fetchall()
            conn.close()
            
            result = []
            for row in rows:
                result.append({
                    "class_name": row[0],
                    "total_exports": row[1],
                    "complete_count": row[2],
                    "error_count": row[3],
                    "pending_count": row[4],
                    "avg_coverage": row[5],
                    "total_bytes_parsed": row[6],
                    "total_bytes_unknown": row[7],
                    "gltf_exported_count": row[8]
                })
            
            self.send_json({"status": "ok", "data": result})
        except Exception as e:
            self.send_error_json(f"Error getting parse status: {e}")

    # Get class-level coverage statistics.
    def handle_class_coverage(self):
        """Return class coverage from class_coverage view."""
        try:
            canonical_db = "/Users/brynnbateman/Downloads/Vanguard EMU/vanguard_files.db"
            conn = sqlite3.connect(canonical_db)
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT class_name, total, fully_parsed, partial, unparsed, pct_complete
                FROM class_coverage
            """)
            rows = cursor.fetchall()
            conn.close()
            
            result = []
            for row in rows:
                result.append({
                    "class_name": row[0],
                    "total": row[1],
                    "fully_parsed": row[2],
                    "partial": row[3],
                    "unparsed": row[4],
                    "pct_complete": row[5]
                })
            
            self.send_json({"status": "ok", "data": result})
        except Exception as e:
            self.send_error_json(f"Error getting class coverage: {e}")

    # Get list of parsed exports with filters.
    def handle_parsed_exports(self, query_string):
        """Return parsed exports with optional filters."""
        try:
            params = urllib.parse.parse_qs(query_string)
            class_name = params.get("class_name", [None])[0]
            status = params.get("status", [None])[0]
            limit = int(params.get("limit", [100])[0])
            offset = int(params.get("offset", [0])[0])
            
            canonical_db = "/Users/brynnbateman/Downloads/Vanguard EMU/vanguard_files.db"
            conn = sqlite3.connect(canonical_db)
            cursor = conn.cursor()
            
            where_clauses = []
            query_params = []
            
            if class_name:
                where_clauses.append("pe.class_name = ?")
                query_params.append(class_name)
            if status:
                where_clauses.append("pe.parse_status = ?")
                query_params.append(status)
            
            where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
            
            cursor.execute(f"""
                SELECT pe.id, pe.export_name, pe.class_name, pe.parse_status,
                       pe.bytes_parsed, pe.serial_size, pe.coverage_pct,
                       pe.uses_heuristics, pe.uses_skips, pe.gltf_exported,
                       pe.last_parsed_at, pe.error_message,
                       f.file_name, f.file_path
                FROM parsed_exports pe
                LEFT JOIN files f ON pe.file_id = f.id
                WHERE {where_sql}
                ORDER BY pe.last_parsed_at DESC
                LIMIT ? OFFSET ?
            """, query_params + [limit, offset])
            
            rows = cursor.fetchall()
            conn.close()
            
            result = []
            for row in rows:
                result.append({
                    "id": row[0],
                    "export_name": row[1],
                    "class_name": row[2],
                    "parse_status": row[3],
                    "bytes_parsed": row[4],
                    "serial_size": row[5],
                    "coverage_pct": row[6],
                    "uses_heuristics": bool(row[7]),
                    "uses_skips": bool(row[8]),
                    "gltf_exported": bool(row[9]),
                    "last_parsed_at": row[10],
                    "error_message": row[11],
                    "file_name": row[12],
                    "file_path": row[13]
                })
            
            self.send_json({"status": "ok", "data": result, "count": len(result)})
        except Exception as e:
            self.send_error_json(f"Error getting parsed exports: {e}")

def main():
    print(f"Starting data server on http://localhost:{PORT}")
    print(f"Database: {DB_PATH}")
    print(f"Static files: {STATIC_DIR}")
    print("\nOpen http://localhost:{} in your browser".format(PORT))
    print("Press Ctrl+C to stop\n")

    server = http.server.HTTPServer(("", PORT), DataHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
