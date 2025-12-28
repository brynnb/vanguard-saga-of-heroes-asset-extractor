"""
Utility functions for the server handlers.
"""

import json
import sqlite3
import os

# Import config
try:
    from config import DB_PATH
except ImportError:
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 
                           "output/data/vanguard_data.db")


def send_json(handler, data):
    """Send JSON response with CORS headers."""
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(json.dumps(data).encode())


def send_error_json(handler, message, status_code=400):
    """Send error JSON response."""
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps({"error": message}).encode())


def get_db():
    """Get database connection with Row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
