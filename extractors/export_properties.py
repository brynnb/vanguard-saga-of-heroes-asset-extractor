#!/usr/bin/env python3
import sys
import os
import sqlite3
import json
from pathlib import Path

# Add parent directory for config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Add current directory for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_bsp import UE2Package
from ue2.properties import find_property_start, parse_properties

DB_PATH = config.DB_PATH

def export_properties_for_chunk(chunk_name):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # Get chunk ID and filepath
    row = conn.execute("SELECT id, filepath FROM chunks WHERE filename = ?", (chunk_name,)).fetchone()
    if not row:
        print(f"Chunk {chunk_name} not found in database")
        return
    
    chunk_id = row['id']
    filepath = row['filepath']
    
    print(f"Parsing properties for {chunk_name}...")
    pkg = UE2Package(filepath)
    
    # Get exports for this chunk to match IDs
    exports = conn.execute("SELECT id, export_index FROM exports WHERE chunk_id = ?", (chunk_id,)).fetchall()
    export_map = {e['export_index']: e['id'] for e in exports}
    
    # Clear existing properties for this chunk to avoid duplicates
    conn.execute("""
        DELETE FROM properties 
        WHERE export_id IN (SELECT id FROM exports WHERE chunk_id = ?)
    """, (chunk_id,))
    
    count = 0
    for i, exp in enumerate(pkg.exports):
        # Match using the index from the export itself, which matches the DB
        export_idx = exp.get('index', i)
        if export_idx not in export_map: continue
        db_export_id = export_map[export_idx]
        
        data = pkg.get_export_data(exp)
        if not data: continue
        
        prop_start = find_property_start(data, pkg.names)
        if prop_start == -1: continue
        
        try:
            props = parse_properties(data, pkg.names, prop_start)
            if not props: continue
            
            for p in props:
                conn.execute("""
                    INSERT INTO properties (export_id, prop_name, prop_type, prop_size, array_index, struct_name, value_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    db_export_id,
                    p.get('name'),
                    p.get('type'),
                    p.get('size'),
                    p.get('array_index', 0),
                    p.get('struct_name'),
                    str(p.get('value')) if p.get('value') is not None else None
                ))
            count += 1
        except Exception as e:
            # print(f"  Error parsing properties for export {i}: {e}")
            pass
            
    conn.commit()
    conn.close()
    print(f"  Exported properties for {count} objects")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        for chunk_name in sys.argv[1:]:
            export_properties_for_chunk(chunk_name)
    else:
        print("Usage: python3 export_properties.py <chunk_name>")
