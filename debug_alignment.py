
import sys
import os
import struct

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from ue2 import UE2Package

def debug_mesh(pkg_path, mesh_name):
    print(f"Analyzing {mesh_name} in {pkg_path}")
    pkg = UE2Package(pkg_path)
    
    # Find the export
    exp = None
    for e in pkg.exports:
        if e['object_name'] == mesh_name:
            exp = e
            break
            
    if not exp:
        print(f"Mesh {mesh_name} not found in package.")
        return
        
    data = pkg.get_export_data(exp)
    print(f"Total serial size: {len(data)}")
    
    # Find None terminator
    from scripts.lib.staticmesh_construct import find_none_terminator
    try:
        prop_end = find_none_terminator(data, pkg.names)
        print(f"Properties end at: {prop_end}")
    except Exception as e:
        print(f"Property parse failed: {e}")
        # Search for None index (0)
        prop_end = -1
        for i in range(min(len(data), 1000)):
             if data[i] == 0:
                 # Check if next byte looks like start of bounds (not 0)
                 if i + 1 < len(data) and data[i+1] != 0:
                     prop_end = i + 1
                     print(f"Found potential 'None' at {i}, prop_end={prop_end}")
                     break

    if prop_end != -1:
        # Core starts right after properties for standard UE2
        # Standard layout: FBox(25) + FSphere(16) = 41 bytes
        # Then InternalVersion (4 bytes)
        core_pos = prop_end
        if core_pos + 45 <= len(data):
            version = struct.unpack('<i', data[core_pos+41 : core_pos+45])[0]
            print(f"Value at version offset ({core_pos+41}): {version}")
            
            # Print hex dump around that area
            start = max(0, core_pos + 30)
            end = min(len(data), core_pos + 100)
            print(f"Hex dump ({start}-{end}):")
            print(data[start:end].hex(' '))
            
            # Check for version 11 or 12 nearby
            for offset in range(max(0, core_pos), min(len(data)-4, core_pos + 500)):
                v = struct.unpack('<i', data[offset : offset + 4])[0]
                if v in [11, 12]:
                    # Likely InternalVersion
                    potential_core_start = offset - 41
                    print(f"FOUND InternalVersion {v} at offset {offset}. Potential core_start: {potential_core_start}")
                    
                    # Check section count
                    if offset + 4 + 4 <= len(data):
                        sc = struct.unpack('<i', data[offset+4 : offset+8])[0]
                        print(f"  Potential SectionCount: {sc}")

if __name__ == "__main__":
    path = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra33_P1_C2_Grid_burialCrypt_mesh.usx"
    name = "Ra33_P1_C2_Grid_burialCrypt_column003_ver03_L0"
    debug_mesh(path, name)
