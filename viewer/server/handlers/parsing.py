"""
Parsing status and coverage API handlers.
"""

import sqlite3
import os
import urllib.parse
from ..utils import send_json, send_error_json


# Alternative canonical DB path for parsing metrics
CANONICAL_DB = "/Users/brynnbateman/Downloads/Vanguard EMU/vanguard_files.db"


def handle_parse_status(handler):
    """Return parsing statistics from parsing_summary view."""
    try:
        if not os.path.exists(CANONICAL_DB):
            send_json(handler, {"status": "error", "message": "Canonical database not found"})
            return
        conn = sqlite3.connect(CANONICAL_DB)
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
        
        send_json(handler, {"status": "ok", "data": result})
    except Exception as e:
        send_error_json(handler, f"Error getting parse status: {e}")


def handle_class_coverage(handler):
    """Return class coverage from class_coverage view."""
    try:
        if not os.path.exists(CANONICAL_DB):
            send_json(handler, {"status": "error", "message": "Canonical database not found"})
            return
        conn = sqlite3.connect(CANONICAL_DB)
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
        
        send_json(handler, {"status": "ok", "data": result})
    except Exception as e:
        send_error_json(handler, f"Error getting class coverage: {e}")


def handle_parsed_exports(handler, query_string):
    """Return parsed exports with optional filters."""
    try:
        params = urllib.parse.parse_qs(query_string)
        class_name = params.get("class_name", [None])[0]
        status = params.get("status", [None])[0]
        limit = int(params.get("limit", [100])[0])
        offset = int(params.get("offset", [0])[0])
        
        if not os.path.exists(CANONICAL_DB):
            send_json(handler, {"status": "error", "message": "Canonical database not found"})
            return
        conn = sqlite3.connect(CANONICAL_DB)
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
        
        send_json(handler, {"status": "ok", "data": result, "count": len(result)})
    except Exception as e:
        send_error_json(handler, f"Error getting parsed exports: {e}")
