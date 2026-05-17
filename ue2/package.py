"""
UE2 Package Reader.

Parses UE2 package files (.usx, .vgr, .upk, etc.) and provides access to
the name table, import table, export table, and export data.
"""

from typing import List, Dict, Optional
from .reader import BinaryReader


class UE2Package:
    """Parser for UE2 package files (.vgr, .usx, .upk, etc.).
    
    Parses the package header and provides access to:
    - names: List of all names in the package
    - imports: List of imported object references
    - exports: List of exported objects with class and offset info
    """

    # UE2 package signature
    SIGNATURE = 0x9E2A83C1

    def __init__(self, filepath: str):
        """Load and parse a UE2 package file.
        
        Args:
            filepath: Path to the package file
        """
        self.filepath = filepath
        with open(filepath, "rb") as f:
            self.data = f.read()

        self.reader = BinaryReader(self.data)
        self.names: List[str] = []
        self.imports: List[Dict] = []
        self.exports: List[Dict] = []
        self.version = 0
        self.licensee = 0

        self._parse_header()

    def _parse_header(self):
        """Parse package header and all tables."""
        r = self.reader
        r.seek(0)

        # Signature
        signature = r.read_uint32()
        if signature != self.SIGNATURE:
            raise ValueError(f"Invalid UE2 package signature: {hex(signature)}")

        # Version info
        self.version = r.read_uint16()
        self.licensee = r.read_uint16()

        # Package flags
        self.package_flags = r.read_uint32()

        # Table locations
        self.name_count = r.read_uint32()
        self.name_offset = r.read_uint32()
        self.export_count = r.read_uint32()
        self.export_offset = r.read_uint32()
        self.import_count = r.read_uint32()
        self.import_offset = r.read_uint32()

        # Parse tables
        self._parse_names()
        self._parse_imports()
        self._parse_exports()

    def _parse_names(self):
        """Parse name table."""
        r = self.reader
        r.seek(self.name_offset)

        for _ in range(self.name_count):
            name = r.read_fstring()
            _flags = r.read_uint32()  # Name flags (usually 0)
            self.names.append(name)

    def _parse_imports(self):
        """Parse import table."""
        r = self.reader
        r.seek(self.import_offset)

        for i in range(self.import_count):
            class_package = r.read_compact_index()
            class_name = r.read_compact_index()
            package = r.read_int32()
            object_name = r.read_compact_index()

            self.imports.append({
                "index": -(i + 1),  # Import indices are negative
                "class_package": self._safe_name(class_package),
                "class_name": self._safe_name(class_name),
                "package": package,
                "object_name": self._safe_name(object_name),
            })

    def _parse_exports(self):
        """Parse export table."""
        r = self.reader
        r.seek(self.export_offset)

        for i in range(self.export_count):
            class_index = r.read_compact_index()
            super_index = r.read_compact_index()
            package = r.read_int32()
            object_name = r.read_compact_index()
            object_flags = r.read_uint32()
            serial_size = r.read_compact_index()
            serial_offset = r.read_compact_index() if serial_size > 0 else 0

            # Resolve class name
            class_name = self._get_class_name(class_index)

            self.exports.append({
                "index": i + 1,  # Export indices are 1-based positive
                "class_index": class_index,
                "class_name": class_name,
                "super_index": super_index,
                "package": package,
                "object_name": self._safe_name(object_name),
                "object_flags": object_flags,
                "serial_size": serial_size,
                "serial_offset": serial_offset,
            })

    def _safe_name(self, index: int) -> str:
        """Safely get name from index."""
        if 0 <= index < len(self.names):
            return self.names[index]
        return ""

    def _get_class_name(self, class_index: int) -> str:
        """Get class name from class index (can be negative for imports)."""
        if class_index < 0:
            # Import reference
            idx = -class_index - 1
            if 0 <= idx < len(self.imports):
                return self.imports[idx]["object_name"]
        elif class_index > 0:
            # Export reference (rare for classes)
            idx = class_index - 1
            if 0 <= idx < len(self.exports):
                return self.exports[idx].get("object_name", "")
        return "Class"

    def get_exports_by_class(self, class_name: str) -> List[Dict]:
        """Get all exports of a specific class.
        
        Args:
            class_name: Name of the class to filter by
            
        Returns:
            List of export dictionaries matching the class
        """
        return [e for e in self.exports if e["class_name"] == class_name]

    def get_export_data(self, export: Dict) -> bytes:
        """Get raw serialized data for an export.
        
        Args:
            export: Export dictionary (from self.exports)
            
        Returns:
            Raw bytes of the export's serialized data
        """
        if export["serial_size"] <= 0:
            return b""
        return self.data[
            export["serial_offset"] : export["serial_offset"] + export["serial_size"]
        ]

    def get_object_name(self, index: int) -> str:
        """Get object name from index (positive=export, negative=import).
        
        Args:
            index: Object index
            
        Returns:
            Object name or empty string
        """
        if index > 0 and index <= len(self.exports):
            return self.exports[index - 1]["object_name"]
        elif index < 0 and -index <= len(self.imports):
            return self.imports[-index - 1]["object_name"]
        return ""

    def get_import_by_index(self, index: int) -> Optional[Dict]:
        """Get import by its negative index.
        
        Args:
            index: Negative import index
            
        Returns:
            Import dictionary or None
        """
        if index < 0:
            idx = -index - 1
            if 0 <= idx < len(self.imports):
                return self.imports[idx]
        return None

    def dump_info(self):
        """Print package summary information."""
        print(f"Package: {self.filepath}")
        print(f"  Version: {self.version}/{self.licensee}")
        print(f"  Names: {len(self.names)}")
        print(f"  Imports: {len(self.imports)}")
        print(f"  Exports: {len(self.exports)}")
