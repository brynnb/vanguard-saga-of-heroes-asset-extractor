#!/usr/bin/env python3
"""Re-export properties for ALL chunks using the fixed parser."""

import sys
import os
import sqlite3

# Add parent directory for config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Add extractors for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from export_properties import export_properties_for_chunk

DB_PATH = config.DB_PATH

def main():
    conn = sqlite3.connect(DB_PATH)
    chunks = conn.execute("SELECT filename FROM chunks ORDER BY filename").fetchall()
    conn.close()
    
    total = len(chunks)
    print(f"Re-exporting properties for {total} chunks...")
    
    for i, (chunk_name,) in enumerate(chunks, 1):
        print(f"[{i}/{total}] {chunk_name}")
        try:
            export_properties_for_chunk(chunk_name)
        except Exception as e:
            print(f"  ERROR: {e}")

    print(f"\nDone! Re-exported properties for {total} chunks.")

if __name__ == "__main__":
    main()
