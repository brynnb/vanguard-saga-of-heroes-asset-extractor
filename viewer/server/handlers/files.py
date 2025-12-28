"""
File listing and structure API handlers.
"""

import os
import urllib.parse
from ..utils import send_json, send_error_json, get_db


def handle_files(handler, query_string):
    """Get files from the file index."""
    params = urllib.parse.parse_qs(query_string)
    category = params.get("category", [""])[0]
    extension = params.get("extension", [""])[0]
    search = params.get("search", [""])[0]
    sort = params.get("sort", ["size_bytes"])[0]
    order = params.get("order", ["desc"])[0]
    limit = int(params.get("limit", ["100"])[0])
    offset = int(params.get("offset", ["0"])[0])

    conn = get_db()

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

    # Check if table exists
    table_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='files'").fetchone()
    if not table_check:
        conn.close()
        send_error_json(handler, "Table 'files' not found. Run 'python3 setup.py' to initialize the database.")
        return

    try:
        # Get total count
        count_cursor = conn.execute(
            f"SELECT COUNT(*) FROM files WHERE {where_clause}",
            values,
        )
        total = count_cursor.fetchone()[0]

        # Get rows
        cursor = conn.execute(
            f"SELECT * FROM files WHERE {where_clause} ORDER BY {sort} {order_dir} LIMIT ? OFFSET ?",
            values + [limit, offset],
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        send_json(handler, {"rows": rows, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        conn.close()
        send_error_json(handler, f"Database error: {e}")


def handle_file_structure(handler, query_string):
    """Get hierarchical binary structure of a file."""
    params = urllib.parse.parse_qs(query_string)
    file_path = params.get("path", [""])[0]
    
    if not file_path:
        send_error_json(handler, "No file path provided")
        return
    
    # Handle relative paths by prepending ASSETS_PATH parent
    if not file_path.startswith("/"):
        try:
            from config import ASSETS_PATH
            assets_parent = os.path.dirname(ASSETS_PATH)
            file_path = os.path.join(assets_parent, file_path)
        except ImportError:
            send_error_json(handler, "Config not available for path resolution")
            return
    
    if not os.path.exists(file_path):
        send_error_json(handler, f"File not found: {file_path}")
        return
    
    try:
        from scripts.extractors.parse_structure import parse_file
        result = parse_file(file_path)
        send_json(handler, result)
    except ImportError as e:
        send_error_json(handler, f"Parser module not available: {e}")
    except Exception as e:
        send_error_json(handler, f"Parse error: {e}")
