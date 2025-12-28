#!/usr/bin/env python3
"""
Universal HTTP server to serve asset viewers and SQLite data.

Usage:
    python server.py

Then open http://localhost:8000 in your browser.

Server implementation is in viewer/server/ for maintainability.
"""

import os
import sys

# Ensure project root is in path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Import and run from the modular server package
from viewer.server import main

if __name__ == "__main__":
    main()
