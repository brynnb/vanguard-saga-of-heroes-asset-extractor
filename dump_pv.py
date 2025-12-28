
import struct
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from ue2 import UE2Package

def dump_post_vertex(pkg_path, mesh_name):
    pkg = UE2Package(pkg_path)
    exp = next(e for e in pkg.exports if e['object_name'] == mesh_name)
    data = pkg.get_export_data(exp)
    
    # We use our scanner logic to find LOD 0
    for i in range(100, len(data) - 100):
        # Header: Version(4), Count(4) or direct Count(4)
        c = struct.unpack('<I', data[i:i+4])[0]
        if 1 <= c <= 5:
             # Check for vertex count
             v_count = -1
             header_size = -1
             for h_size in [12, 16]:
                 vc = struct.unpack('<I', data[i+h_size-4:i+h_size])[0]
                 if 10 <= vc <= 100000:
                     v_count = vc
                     header_size = h_size
                     break
             if v_count > 0:
                 print(f"Mesh: {mesh_name} | LOD 0 at {i} | VCount: {v_count} | Header: {header_size}")
                 pv_start = i + header_size + (v_count * 56)
                 if pv_start + 40 <= len(data):
                     print(f"  PostVertex at {pv_start}: {data[pv_start:pv_start+40].hex(' ')}")
                 break

if __name__ == "__main__":
    dump_post_vertex("/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra33_P1_C2_Grid_burialCrypt_mesh.usx", "Ra33_P1_C2_Grid_burialCrypt_column003_ver03_L0")
    dump_post_vertex("/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra3_P1_C1_Decor_mesh.usx", "Ra3_P1_C1_Decor_fence004_L0")
