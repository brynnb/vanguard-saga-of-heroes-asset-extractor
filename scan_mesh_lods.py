
import struct
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from ue2 import UE2Package

def scan_mesh(pkg_path, mesh_name):
    pkg = UE2Package(pkg_path)
    exp = next(e for e in pkg.exports if e['object_name'] == mesh_name)
    data = pkg.get_export_data(exp)
    
    print(f"Scanning {mesh_name} ({len(data)} bytes)...")
    
    # Vanguard LOD pattern:
    # Option A: physics(4), auth(4), version(4), count(4) -> 16 bytes header
    # Option B: physics(4), auth(4), count(4) -> 12 bytes header
    
    for i in range(30, len(data) - 100):
        # We look for LODCount (usually 1-3)
        # In Vanguard V1, it might be physics(4), auth(4), lod_version(4), lod_count(4)
        if i + 16 <= len(data):
            v = struct.unpack('<I', data[i+8:i+12])[0]
            c = struct.unpack('<I', data[i+12:i+16])[0]
            if v == 1 and 1 <= c <= 5:
                # Potential LOD block start
                # Read LOD 0 header
                # sec_count(4), unk(4), v_count(4)
                if i + 16 + 12 <= len(data):
                    vc = struct.unpack('<I', data[i+16+8:i+16+12])[0]
                    if 10 <= vc <= 100000:
                        print(f"FOUND LOD V1 at {i}: LODCount={c}, LOD0_VertexCount={vc}")
                        
        # Option C: Just LODCount(4)
        c2 = struct.unpack('<I', data[i:i+4])[0]
        if 1 <= c2 <= 5:
             vc2 = struct.unpack('<I', data[i+4+8:i+4+12])[0]
             if 10 <= vc2 <= 100000:
                 print(f"FOUND LOD V0? at {i}: LODCount={c2}, LOD0_VertexCount={vc2}")

if __name__ == "__main__":
    scan_mesh("/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra33_P1_C2_Grid_burialCrypt_mesh.usx", "Ra33_P1_C2_Grid_burialCrypt_column003_ver03_L0")
