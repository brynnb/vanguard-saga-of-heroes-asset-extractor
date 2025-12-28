"""
Generic table data and SQL query API handlers.
"""

import sqlite3
import urllib.parse
from ..utils import send_json, send_error_json, get_db, DB_PATH


def handle_query(handler, query_string):
    """Execute arbitrary SQL query."""
    params = urllib.parse.parse_qs(query_string)
    sql = params.get("sql", [""])[0]

    if not sql:
        send_error_json(handler, "No SQL query provided")
        return

    # Only allow SELECT statements
    if not sql.strip().upper().startswith("SELECT"):
        send_error_json(handler, "Only SELECT queries are allowed")
        return

    try:
        conn = get_db()
        cursor = conn.execute(sql)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        conn.close()

        send_json(handler, {"columns": columns, "rows": [list(row) for row in rows]})
    except Exception as e:
        send_error_json(handler, f"Query error: {e}")


def handle_table_data(handler, query_string):
    """Get paginated data from any table."""
    params = urllib.parse.parse_qs(query_string)
    table_name = params.get("table", [""])[0]
    search = params.get("search", [""])[0]
    limit = int(params.get("limit", ["100"])[0])
    offset = int(params.get("offset", ["0"])[0])
    
    # Whitelist of allowed tables
    allowed_tables = [
        "chunks", "exports", "files", "mesh_index", "names", 
        "imports", "properties", "shaders", "mesh_materials", 
        "prefabs", "terrain_chunks"
    ]
    
    if table_name not in allowed_tables:
        send_error_json(handler, f"Invalid table: {table_name}")
        return
    
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # Build query with optional search
        if search:
            # Search across all text columns
            cursor = conn.execute(f"PRAGMA table_info({table_name})")
            columns = [row[1] for row in cursor.fetchall()]
            text_cols = columns[:5]  # Search first 5 columns
            
            where_clauses = [f"{col} LIKE ?" for col in text_cols]
            where_sql = " OR ".join(where_clauses)
            search_params = [f"%{search}%" for _ in text_cols]
            
            count_sql = f"SELECT COUNT(*) FROM {table_name} WHERE {where_sql}"
            data_sql = f"SELECT * FROM {table_name} WHERE {where_sql} LIMIT ? OFFSET ?"
            
            total = conn.execute(count_sql, search_params).fetchone()[0]
            rows = conn.execute(data_sql, search_params + [limit, offset]).fetchall()
        else:
            total = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM {table_name} LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
        
        # Get column names
        cursor = conn.execute(f"PRAGMA table_info({table_name})")
        columns = [row[1] for row in cursor.fetchall()]
        
        # Convert to list of dicts
        data = []
        for row in rows:
            data.append({col: row[i] for i, col in enumerate(columns)})
        
        conn.close()
        
        send_json(handler, {
            "table": table_name,
            "total": total,
            "limit": limit,
            "offset": offset,
            "columns": columns,
            "rows": data
        })
    except Exception as e:
        send_error_json(handler, f"Database error: {e}")


def handle_table_counts(handler):
    """Get row counts for all tables."""
    try:
        conn = sqlite3.connect(DB_PATH)
        
        tables = [
            "chunks", "exports", "files", "mesh_index", "names",
            "imports", "properties", "shaders", "mesh_materials",
            "prefabs", "terrain_chunks"
        ]
        
        counts = {}
        for table in tables:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                counts[table] = count
            except:
                counts[table] = 0
        
        conn.close()
        send_json(handler, counts)
    except Exception as e:
        send_error_json(handler, f"Database error: {e}")
