import sys
import os
import struct
import typing
from dataclasses import dataclass, field

# Helper to import from local renderer directory
sys.path.append(os.getcwd())
try:
    from ue2 import UE2Package
except ImportError:
    # If running from renderer/ subdirectory
    sys.path.append(os.path.join(os.getcwd(), ".."))
    from ue2 import UE2Package

@dataclass
class ByteRange:
    start: int
    end: int
    desc: str
    data_type: str = "Unknown"

    @property
    def size(self):
        return self.end - self.start

class FileAuditor:
    def __init__(self, file_path):
        self.file_path = file_path
        self.file_size = os.path.getsize(file_path)
        self.ranges: typing.List[ByteRange] = []
        self.pkg = None

    def add_range(self, start, end, desc, dtype="Structure"):
        # Detect overlaps
        for r in self.ranges:
            if max(start, r.start) < min(end, r.end):
                print(f"WARNING: Overlapping range! ({desc}) overlaps with ({r.desc})")
        
        self.ranges.append(ByteRange(start, end, desc, dtype))

    def audit(self):
        print(f"Auditing {os.path.basename(self.file_path)} ({self.file_size} bytes)...")
        
        # 1. Load Package to get Table Offsets (using existing reliable logic)
        try:
            self.pkg = UE2Package(self.file_path)
        except Exception as e:
            print(f"CRITICAL: Failed to load package header: {e}")
            return

        # 2. Map Container Headers
        self.add_range(0, 64, "Package Header", "Header")
        
        # Map Tables using pkg header info
        # Note: We construct sizes carefully.
        
        # Name Table
        if self.pkg.name_count > 0:
            # Calculate precise size of Name Table
            # Each entry: [Length:4 bytes] + [String:Length bytes] + [Flags:4 bytes]
            
            # We can't easily re-read without duplicating UE2Package logic. 
            # But wait, UE2Package reads the names. We can just iterate the loaded names?
            # NO, UE2Package stores them as strings, we lose the flags and exact padding info.
            
            # Let's peek at the file manually to measure it.
            with open(self.file_path, 'rb') as f:
                f.seek(self.pkg.name_offset)
                
                for i in range(self.pkg.name_count):
                    # Read Name Length
                    # Compact Index? Or standard int?
                    # UE2 uses Compact Indices for SOME things, but NameTable is usually standard... 
                    # actually UE2Package uses LoadNameTable which does: 
                    #   Read String
                    #   Read Int (Flags)
                    
                    # Read string length (compact index-ish string read)
                    # Let's just trust our audit needs to be robust. 
                    # Simpler: The auditor should use the gap between NameOffset and the FIRST Export Offset.
                    pass

        # Strategy: NameTable ends where the first Export Data starts (usually).
        # Find the earliest Export Offset
        first_export_offset = self.file_size
        if self.pkg.exports:
            first_export_offset = min(e['serial_offset'] for e in self.pkg.exports)
        
        # Name Table End is limited by first export or ImportOffset
        name_table_end = min(self.pkg.import_offset, first_export_offset)
        
        self.add_range(self.pkg.name_offset, name_table_end, "Name Table", "Table")
        self.add_range(self.pkg.import_offset, self.pkg.export_offset, "Import Table", "Table")
        
        # Export Table is usually at the end?
        # UE2 Header has `export_offset`.
        # Export table entries are fixed size? usually ~40 bytes per export.
        export_table_size = self.pkg.export_count * 36 # rough guess, let's measure
        # Actually, let's define it from export_offset to EOF? or next section?
        # In this file, Export Table seems to be at 1338 (based on previous run log 'Export count: 2 @ 1338')
        # Wait, the previous run log said: "Export count: 2 @ 1338".
        # Let's use the pkg values.
        
        self.add_range(self.pkg.export_offset, self.file_size, "Export Table (Est)", "Table")

        # 3. Map Exports (The most important part)
        for i, exp in enumerate(self.pkg.exports):
            # Serialized size is usually data_length + header overhead?
            # Actually, `exp['serial_size']` represents the object data size ON DISK.
            offset = exp['serial_offset']
            size = exp['serial_size']
            
            # Note: UE2 export table entries point to the OBJECT DATA.
            # The Export Table *itself* is a list of structs located at pkg.export_offset.
            
            name = exp['object_name']
            self.add_range(offset, offset + size, f"Export[{i}] {name}", "ExportData")

        # 4. Sort ranges
        self.ranges.sort(key=lambda x: x.start)

        # 5. Analyze Gaps
        self.report_gaps()

    def report_gaps(self):
        cursor = 0
        total_mapped = 0
        
        print(f"\n{'Start':<10} | {'End':<10} | {'Size':<8} | {'Type':<10} | {'Description'}")
        print("-" * 80)

        for r in self.ranges:
            # Check for GAP before this range
            if r.start > cursor:
                gap_size = r.start - cursor
                print(f"{cursor:<10} | {r.start:<10} | {gap_size:<8} | {'GAP':<10} | {'[Unmapped Bytes]'}")
            
            print(f"{r.start:<10} | {r.end:<10} | {r.size:<8} | {r.data_type:<10} | {r.desc}")
            total_mapped += r.size
            cursor = max(cursor, r.end)

        # Check for GAP at EOF
        if cursor < self.file_size:
            print(f"{cursor:<10} | {self.file_size:<10} | {self.file_size - cursor:<8} | {'GAP':<10} | {'[Unmapped Bytes at EOF]'}")

        print("-" * 80)
        coverage = (total_mapped / self.file_size) * 100
        print(f"Coverage: {coverage:.2f}% ({total_mapped}/{self.file_size} bytes)")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python audit_file_structure.py <file_path>")
        sys.exit(1)
    
    auditor = FileAuditor(sys.argv[1])
    auditor.audit()
