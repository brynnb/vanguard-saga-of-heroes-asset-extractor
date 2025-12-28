"""
DataHandler base class with routing logic.
"""

import http.server
import urllib.parse
import os
import sys

# Import handlers
from .handlers import (
    handle_class_summary,
    handle_chunks,
    handle_length_distribution,
    handle_exports,
    handle_names,
    handle_properties,
    handle_property_summary,
    handle_export_detail,
    handle_watervolume_chain,
    handle_files,
    handle_file_structure,
    handle_query,
    handle_table_data,
    handle_table_counts,
    handle_parse_status,
    handle_class_coverage,
    handle_parsed_exports,
)
from .utils import send_json, send_error_json, get_db


class DataHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP request handler with API routing."""
    
    def __init__(self, *args, static_dir=None, **kwargs):
        self._static_dir = static_dir
        super().__init__(*args, directory=static_dir, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        try:
            if parsed.path == "/api/query":
                handle_query(self, parsed.query)
            elif parsed.path == "/api/class_summary":
                handle_class_summary(self)
            elif parsed.path == "/api/chunks":
                handle_chunks(self)
            elif parsed.path == "/api/length_distribution":
                handle_length_distribution(self, parsed.query)
            elif parsed.path == "/api/exports":
                handle_exports(self, parsed.query)
            elif parsed.path == "/api/names":
                handle_names(self, parsed.query)
            elif parsed.path == "/api/properties":
                handle_properties(self, parsed.query)
            elif parsed.path == "/api/property_summary":
                handle_property_summary(self, parsed.query)
            elif parsed.path == "/api/export_detail":
                handle_export_detail(self, parsed.query)
            elif parsed.path == "/api/watervolume_chain":
                handle_watervolume_chain(self, parsed.query)
            elif parsed.path == "/api/files":
                handle_files(self, parsed.query)
            elif parsed.path == "/api/file_structure":
                handle_file_structure(self, parsed.query)
            elif parsed.path == "/api/parse_status":
                handle_parse_status(self)
            elif parsed.path == "/api/class_coverage":
                handle_class_coverage(self)
            elif parsed.path == "/api/parsed_exports":
                handle_parsed_exports(self, parsed.query)
            elif parsed.path == "/api/table_data":
                handle_table_data(self, parsed.query)
            elif parsed.path == "/api/table_counts":
                handle_table_counts(self)
            else:
                super().do_GET()
        except Exception as e:
            print(f"Error handling request {parsed.path}: {e}")
            send_error_json(self, str(e))
    
    def translate_path(self, path):
        """Map /output/ to the project root's output folder."""
        # Normalize path
        normalized_path = path.split('?', 1)[0]
        normalized_path = normalized_path.split('#', 1)[0]
        
        if normalized_path.startswith('/output/'):
            # PROJECT_ROOT is handled in __init__.py and imported or accessible
            from . import PROJECT_ROOT
            # Strip leading slash and join with PROJECT_ROOT
            # normalized_path starts with /output/
            rel_path = normalized_path[1:] # output/...
            return os.path.join(PROJECT_ROOT, rel_path)
            
        return super().translate_path(path)
    
    # Keep these methods on the class for backward compatibility
    def send_json(self, data):
        send_json(self, data)
    
    def send_error_json(self, message):
        send_error_json(self, message)
    
    def get_db(self):
        return get_db()
