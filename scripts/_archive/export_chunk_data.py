import sys
import os
import sqlite3
import json
import struct
from pathlib import Path
from datetime import datetime

# Add parent directory for config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Add current directory for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_bsp import UE2Package, read_compact_index

MAPS_DIR = os.path.join(config.ASSETS_PATH, "Maps")
DB_PATH = config.DB_PATH


def init_database(db_path: str) -> sqlite3.Connection:
    """Initialize the SQLite database with schema."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.executescript(
        """
        -- Chunks table
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            filename TEXT UNIQUE NOT NULL,
            filepath TEXT NOT NULL,
            version INTEGER,
            export_count INTEGER,
            name_count INTEGER,
            import_count INTEGER,
            parsed_at TEXT
        );
        
        -- Exports table (all objects in all chunks)
        CREATE TABLE IF NOT EXISTS exports (
            id INTEGER PRIMARY KEY,
            chunk_id INTEGER NOT NULL,
            export_index INTEGER NOT NULL,
            object_name TEXT NOT NULL,
            class_name TEXT,
            class_index INTEGER,
            super_index INTEGER,
            outer_index INTEGER,
            data_length INTEGER,
            data_offset INTEGER,
            position_x REAL,
            position_y REAL,
            position_z REAL,
            header_bytes TEXT,
            FOREIGN KEY (chunk_id) REFERENCES chunks(id),
            UNIQUE(chunk_id, export_index)
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
        
        -- Class summary view
        CREATE VIEW IF NOT EXISTS class_summary AS
        SELECT 
            class_name,
            COUNT(*) as export_count,
            COUNT(DISTINCT data_length) as length_variants,
            MIN(data_length) as min_length,
            MAX(data_length) as max_length,
            COUNT(DISTINCT chunk_id) as chunk_count
        FROM exports
        GROUP BY class_name
        ORDER BY export_count DESC;
        
        -- Length distribution view
        CREATE VIEW IF NOT EXISTS length_distribution AS
        SELECT 
            class_name,
            data_length,
            COUNT(*) as count,
            ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY class_name), 1) as pct
        FROM exports
        GROUP BY class_name, data_length
        ORDER BY class_name, count DESC;
        
        -- Create indexes for fast queries
        CREATE INDEX IF NOT EXISTS idx_exports_class ON exports(class_name);
        CREATE INDEX IF NOT EXISTS idx_exports_length ON exports(data_length);
        CREATE INDEX IF NOT EXISTS idx_exports_chunk ON exports(chunk_id);
        CREATE INDEX IF NOT EXISTS idx_names_chunk ON names(chunk_id);
    """
    )

    conn.commit()
    return conn


def parse_position(data: bytes) -> tuple:
    """Extract position by searching for 3 consecutive floats that look like coordinates."""
    size = len(data)
    if size < 12:
        return None, None, None
    
    # Vanguard world coordinates are typically large (>1000) for Z, 
    # but X/Y can be small (local offsets).
    # Brute-force search from the end (where Location usually lives)
    for j in range(max(0, size - 100), size - 11):
        try:
            x = struct.unpack("<f", data[j : j + 4])[0]
            y = struct.unpack("<f", data[j + 4 : j + 8])[0]
            z = struct.unpack("<f", data[j + 8 : j + 12])[0]
            
            # Heuristic: Valid world-scale height (Z) and reasonable range for X/Y
            if abs(x) < 1000000 and abs(y) < 1000000 and 1000 < abs(z) < 1000000:
                return x, y, z
        except:
            continue
            
    return None, None, None


def export_chunk(conn: sqlite3.Connection, chunk_path: str) -> int:
    """Export a single chunk to the database."""
    filename = os.path.basename(chunk_path)

    # Check if already exported
    existing = conn.execute(
        "SELECT id FROM chunks WHERE filename = ?", (filename,)
    ).fetchone()

    if existing:
        print(f"  {filename}: Already in database (id={existing['id']})")
        return existing["id"]

    try:
        pkg = UE2Package(chunk_path)
    except Exception as e:
        print(f"  {filename}: Failed to parse - {e}")
        return None

    # Insert chunk
    cursor = conn.execute(
        """
        INSERT INTO chunks (filename, filepath, version, export_count, name_count, import_count, parsed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """,
        (
            filename,
            chunk_path,
            pkg.version,
            len(pkg.exports),
            len(pkg.names),
            len(pkg.imports),
            datetime.now().isoformat(),
        ),
    )
    chunk_id = cursor.lastrowid

    # Insert names
    for i, name in enumerate(pkg.names):
        conn.execute(
            "INSERT INTO names (chunk_id, name_index, name) VALUES (?, ?, ?)",
            (chunk_id, i, name),
        )

    # Insert imports
    for imp in pkg.imports:
        conn.execute(
            """
            INSERT INTO imports (chunk_id, import_index, object_name, class_name, class_package)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                chunk_id,
                imp.get("index", 0),
                imp.get("object_name", ""),
                imp.get("class_name", ""),
                imp.get("class_package", ""),
            ),
        )

    # Insert exports
    for exp in pkg.exports:
        data = pkg.get_export_data(exp)
        data_length = len(data) if data else 0

        # Get position for actors
        x, y, z = None, None, None
        if data and len(data) >= 13:
            x, y, z = parse_position(data)

        # Get header bytes
        header_bytes = data[:4].hex() if data and len(data) >= 4 else None

        conn.execute(
            """
            INSERT INTO exports (
                chunk_id, export_index, object_name, class_name, class_index,
                super_index, outer_index, data_length, data_offset, position_x, position_y,
                position_z, header_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                chunk_id,
                exp.get("index", 0),
                exp.get("object_name", ""),
                exp.get("class_name", ""),
                exp.get("class_index", 0),
                exp.get("super_index", 0),
                exp.get("outer_index", 0),
                data_length,
                exp.get("serial_offset", 0),
                x,
                y,
                z,
                header_bytes,
            ),
        )

    conn.commit()
    print(f"  {filename}: Exported {len(pkg.exports)} exports, {len(pkg.names)} names")
    return chunk_id


def run_query(db_path: str, query: str):
    """Run a query against the database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.execute(query)
        rows = cursor.fetchall()

        if rows:
            # Print header
            columns = rows[0].keys()
            print("\t".join(columns))
            print("-" * 80)

            # Print rows
            for row in rows[:100]:  # Limit to 100 rows
                print("\t".join(str(row[col]) for col in columns))

            if len(rows) > 100:
                print(f"\n... and {len(rows) - 100} more rows")
        else:
            print("No results")

    except Exception as e:
        print(f"Query error: {e}")
    finally:
        conn.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--query":
        # Query mode
        query = " ".join(sys.argv[2:])
        run_query(DB_PATH, query)
        return

    print("=" * 80)
    print("EXPORTING CHUNK DATA TO SQLITE")
    print(f"Database: {DB_PATH}")
    print("=" * 80)

    conn = init_database(DB_PATH)

    if len(sys.argv) > 1:
        # Export specific chunk(s)
        for chunk_name in sys.argv[1:]:
            chunk_path = os.path.join(MAPS_DIR, chunk_name)
            if os.path.exists(chunk_path):
                export_chunk(conn, chunk_path)
            else:
                print(f"  {chunk_name}: File not found")
    else:
        # Export all chunks
        chunks = list(Path(MAPS_DIR).glob("chunk_*.vgr"))
        print(f"\nFound {len(chunks)} chunks to export\n")

        for i, chunk_path in enumerate(chunks):
            if i % 10 == 0:
                print(f"Progress: {i}/{len(chunks)}")
            export_chunk(conn, str(chunk_path))

    conn.close()

    # Print summary
    print("\n" + "=" * 80)
    print("EXPORT COMPLETE")
    print("=" * 80)

    conn = sqlite3.connect(DB_PATH)

    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    export_count = conn.execute("SELECT COUNT(*) FROM exports").fetchone()[0]

    print(f"\nDatabase contains:")
    print(f"  Chunks: {chunk_count}")
    print(f"  Exports: {export_count}")

    print("\nExample queries:")
    print('  python export_chunk_data.py --query "SELECT * FROM class_summary"')
    print(
        "  python export_chunk_data.py --query \"SELECT * FROM length_distribution WHERE class_name='WaterVolume'\""
    )
    print(
        '  python export_chunk_data.py --query "SELECT class_name, data_length, COUNT(*) FROM exports GROUP BY class_name, data_length"'
    )

    conn.close()


if __name__ == "__main__":
    main()
