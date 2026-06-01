#!/usr/bin/env python3
"""
Wrapper for the Universal Property Parser.
Allows it to be called from setup.py like other extractors.
"""

import os
import sys

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

try:
    from ue2.properties import main
    if __name__ == "__main__":
        # Pass any arguments to the property parser
        import sys
        # By default, we might want to limit for setup.py if it's too slow,
        # but the user said "properties should be part of setup.py",
        # implying they want the full run.
        main()
except ImportError as e:
    print(f"Error: Could not import ue2.properties: {e}")
    sys.exit(1)
