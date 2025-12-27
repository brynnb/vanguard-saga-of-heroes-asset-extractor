
import sys
import os
import struct

# Add paths
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ue2 import UE2Package
from lib.staticmesh_construct import parse_staticmesh

PKG_PATH = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra3_P1_C1_Bridges_mesh.usx"

def debug_parse():
    print(f"Opening {PKG_PATH}")
    pkg = UE2Package(PKG_PATH)
    
    print(f"Package loaded. Export count: {len(pkg.exports)}")
    
    target_name = "Ra3_P1_C1_Bridges_DrakeRider001_Platform04"
    
    for i, exp in enumerate(pkg.exports):
        if exp['object_name'] == target_name:
            print(f"\nFound {target_name} at index {i}")
            print(f"Serial Offset: {exp['serial_offset']}")
            print(f"Serial Size: {exp['serial_size']}")
            
            data = pkg.get_export_data(exp)
            print(f"Read {len(data)} bytes")
            
            try:
                # Calculate skip_pos manually to dump hex
                # Need to replicate staticmesh_construct logic briefly or just use hardcoded offset from previous run
                # Previous run said SkipPos: 49968164
                # Serial Offset: 49401622
                skip_pos_abs = 49968164
                rel_skip = skip_pos_abs - exp['serial_offset']
                
                print(f"DEBUG Dump at RelSkip {rel_skip}:")
                if rel_skip < len(data):
                    print(data[rel_skip:rel_skip+128].hex(' ', 4))
                
                # Debug parsing manually
                result = parse_staticmesh(data, pkg.names, exp['serial_offset'])
                
                print("\nParse Result:")
                print(f"Bytes Parsed: {result['bytes_parsed']}")
                print(f"Unknown Regions: {len(result['unknown_regions'])}")
                
                lods = result.get('data', {}).get('lods', [])
                print(f"LOD Count: {len(lods)}")
                
                for j, lod in enumerate(lods):
                    vc = lod.get('vertex_count', 0)
                    print(f"  LOD {j} Vertex Count: {vc}")
                    if vc > 0:
                        verts = lod.get('vertices', [])
                        print(f"  LOD {j} Parsed Vertices: {len(verts)}")
                        if verts:
                            v0 = verts[0]
                            print(f"    V0: ({v0.get('x')}, {v0.get('y')}, {v0.get('z')})")
            
            except Exception as e:
                print(f"Parse Error: {e}")
                import traceback
                traceback.print_exc()

if __name__ == "__main__":
    debug_parse()
