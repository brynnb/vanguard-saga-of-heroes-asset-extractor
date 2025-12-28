"""
Vanguard Data Viewer Server

This package provides the HTTP server for the data and mesh viewers.
"""

import http.server
import os
import sys

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Add scripts folder to path for imports
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Import config
try:
    from config import DB_PATH, RENDERER_ROOT
    # Static files are now in viewer/ subdirectory
    STATIC_DIR = os.path.join(RENDERER_ROOT, "viewer")
except ImportError:
    print("Warning: Could not import config.py. Using default paths.")
    STATIC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = os.path.join(PROJECT_ROOT, "output/data/vanguard_data.db")

PORT = 8000

from .base import DataHandler


def create_handler(static_dir):
    """Create a handler class with the given static directory."""
    class ConfiguredHandler(DataHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, static_dir=static_dir, **kwargs)
    return ConfiguredHandler


def main():
    """Start the HTTP server."""
    print(f"Starting data server on http://localhost:{PORT}")
    print(f"Database: {DB_PATH}")
    print(f"Static files: {STATIC_DIR}")
    print("\nOpen http://localhost:{} in your browser".format(PORT))
    print("Press Ctrl+C to stop\n")

    handler = create_handler(STATIC_DIR)
    server = http.server.HTTPServer(("", PORT), handler)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
