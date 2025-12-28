"""
Export and class-related API handlers.
"""

import urllib.parse
from ..utils import send_json, send_error_json, get_db


def handle_class_summary(handler):
    """Get class summary statistics."""
    conn = get_db()
    cursor = conn.execute("""
        SELECT 
            class_name,
            COUNT(*) as export_count,
            COUNT(DISTINCT serial_size) as length_variants,
            MIN(serial_size) as min_length,
            MAX(serial_size) as max_length,
            COUNT(DISTINCT chunk_id) as chunk_count
        FROM exports
        GROUP BY class_name
        ORDER BY export_count DESC
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    send_json(handler, rows)


def handle_chunks(handler):
    """Get list of all chunks."""
    conn = get_db()
    cursor = conn.execute("""
        SELECT id, filename, export_count, name_count, created_at as parsed_at
        FROM chunks
        ORDER BY filename
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    send_json(handler, rows)


def handle_length_distribution(handler, query_string):
    """Get length distribution for a class."""
    params = urllib.parse.parse_qs(query_string)
    class_name = params.get("class", [""])[0]

    conn = get_db()

    if class_name:
        cursor = conn.execute("""
            SELECT 
                serial_size as data_length,
                COUNT(*) as count,
                ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) as pct
            FROM exports
            WHERE class_name = ?
            GROUP BY serial_size
            ORDER BY count DESC
        """, (class_name,))
    else:
        cursor = conn.execute("""
            SELECT 
                class_name,
                serial_size as data_length,
                COUNT(*) as count
            FROM exports
            GROUP BY class_name, serial_size
            ORDER BY class_name, count DESC
        """)

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    send_json(handler, rows)


def handle_exports(handler, query_string):
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
        conditions.append("e.serial_size = ?")
        values.append(int(data_length))

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    conn = get_db()

    # Get total count
    count_cursor = conn.execute(
        f"SELECT COUNT(*) FROM exports e WHERE {where_clause}",
        values,
    )
    total = count_cursor.fetchone()[0]

    # Get rows
    cursor = conn.execute(
        f"""
        SELECT 
            e.id, e.export_index, e.object_name, e.class_name,
            e.serial_size as data_length, e.position_x, e.position_y, e.position_z,
            NULL as header_bytes, c.filename as chunk_name
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

    send_json(handler, {"rows": rows, "total": total, "limit": limit, "offset": offset})


def handle_names(handler, query_string):
    """Get names from a chunk."""
    params = urllib.parse.parse_qs(query_string)
    chunk_id = params.get("chunk_id", ["1"])[0]
    search = params.get("search", [""])[0]

    conn = get_db()

    if search:
        cursor = conn.execute("""
            SELECT name_index, name
            FROM names
            WHERE chunk_id = ? AND name LIKE ?
            ORDER BY name_index
            LIMIT 500
        """, (int(chunk_id), f"%{search}%"))
    else:
        cursor = conn.execute("""
            SELECT name_index, name
            FROM names
            WHERE chunk_id = ?
            ORDER BY name_index
            LIMIT 500
        """, (int(chunk_id),))

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    send_json(handler, rows)


def handle_properties(handler, query_string):
    """Get properties for an export or class."""
    params = urllib.parse.parse_qs(query_string)
    export_id = params.get("export_id", [""])[0]
    class_name = params.get("class", [""])[0]
    prop_name = params.get("prop_name", [""])[0]

    conn = get_db()

    if export_id:
        cursor = conn.execute("""
            SELECT prop_name, prop_type, prop_size, value_text
            FROM properties
            WHERE export_id = ?
            ORDER BY id
        """, (int(export_id),))
    elif class_name and prop_name:
        cursor = conn.execute("""
            SELECT p.prop_name, p.prop_type, p.value_text,
                   e.object_name, c.filename
            FROM properties p
            JOIN exports e ON p.export_id = e.id
            JOIN chunks c ON e.chunk_id = c.id
            WHERE e.class_name = ? AND p.prop_name = ?
            LIMIT 100
        """, (class_name, prop_name))
    else:
        send_json(handler, [])
        return

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    send_json(handler, rows)


def handle_property_summary(handler, query_string):
    """Get property summary for a class."""
    params = urllib.parse.parse_qs(query_string)
    class_name = params.get("class", [""])[0]

    conn = get_db()

    if class_name:
        cursor = conn.execute("""
            SELECT p.prop_name, p.prop_type, COUNT(*) as count,
                   COUNT(DISTINCT p.value_text) as unique_values
            FROM properties p
            JOIN exports e ON p.export_id = e.id
            WHERE e.class_name = ?
            GROUP BY p.prop_name, p.prop_type
            ORDER BY count DESC
        """, (class_name,))
    else:
        cursor = conn.execute("""
            SELECT prop_name, prop_type, COUNT(*) as count
            FROM properties
            GROUP BY prop_name, prop_type
            ORDER BY count DESC
            LIMIT 100
        """)

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    send_json(handler, rows)


def handle_export_detail(handler, query_string):
    """Get detailed info for a single export including properties."""
    params = urllib.parse.parse_qs(query_string)
    export_id = params.get("id", [""])[0]

    if not export_id:
        send_error_json(handler, "No export ID provided")
        return

    conn = get_db()

    # Get export info
    cursor = conn.execute("""
        SELECT e.*, c.filename as chunk_name
        FROM exports e
        JOIN chunks c ON e.chunk_id = c.id
        WHERE e.id = ?
    """, (int(export_id),))
    export = cursor.fetchone()

    if not export:
        send_error_json(handler, "Export not found")
        return

    export_dict = dict(export)

    # Get properties
    cursor = conn.execute("""
        SELECT prop_name, prop_type, prop_size, array_index, 
               struct_name, value_text
        FROM properties
        WHERE export_id = ?
        ORDER BY id
    """, (int(export_id),))
    properties = [dict(row) for row in cursor.fetchall()]

    conn.close()

    send_json(handler, {"export": export_dict, "properties": properties})


def handle_watervolume_chain(handler, query_string):
    """Get WaterVolume -> Model -> Polys chain for a WaterVolume export."""
    params = urllib.parse.parse_qs(query_string)
    export_id = params.get("id", [""])[0]

    if not export_id:
        send_error_json(handler, "No export ID provided")
        return

    conn = get_db()

    # Get the export info
    cursor = conn.execute("""
        SELECT e.*, c.filename as chunk_name
        FROM exports e
        JOIN chunks c ON e.chunk_id = c.id
        WHERE e.id = ?
    """, (int(export_id),))
    export = cursor.fetchone()

    if not export:
        send_error_json(handler, "Export not found")
        return

    export_dict = dict(export)
    chunk_id = export_dict["chunk_id"]

    # Find Model exports in the same chunk
    cursor = conn.execute("""
        SELECT id, export_index, object_name, serial_size as data_length
        FROM exports
        WHERE chunk_id = ? AND class_name = 'Model'
        ORDER BY export_index
    """, (chunk_id,))
    models = [dict(row) for row in cursor.fetchall()]

    # Find Polys exports in the same chunk
    cursor = conn.execute("""
        SELECT id, export_index, object_name, serial_size as data_length
        FROM exports
        WHERE chunk_id = ? AND class_name = 'Polys'
        ORDER BY export_index
    """, (chunk_id,))
    polys = [dict(row) for row in cursor.fetchall()]

    conn.close()

    send_json(handler, {
        "export": export_dict,
        "models": models,
        "polys": polys,
        "note": "WaterVolume references Models via compact indices in raw data. Models reference Polys similarly.",
    })
