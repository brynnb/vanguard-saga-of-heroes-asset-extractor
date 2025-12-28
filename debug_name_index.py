import sys
import os
import struct

# Add project root to path
PROJECT_ROOT = "/Users/brynnbateman/Documents/GitHub/vanguard-saga-of-hereos-asset-extractor"
sys.path.insert(0, PROJECT_ROOT)

from ue2 import UE2Package
from scripts.lib.staticmesh_construct import (
    find_none_terminator,
    is_valid_property_start,
    parse_staticmesh
)

def scan_mesh(pkg_path, mesh_name):
    print(f"\n" + "="*80)
    print(f"SCANNING MESH: {mesh_name}")
    print("="*80)
    
    pkg = UE2Package(pkg_path)
    target_exp = next((e for e in pkg.exports if e["object_name"] == mesh_name), None)
    
    if not target_exp:
        print(f"Mesh {mesh_name} not found.")
        return

    data = pkg.get_export_data(target_exp)
    serial_offset = target_exp['serial_offset']
    print(f"Total size: {len(data)} bytes")
    print(f"Serial offset: {serial_offset:08x}")
    
    # 1. Trace properties
    print("\n--- Property Trace ---")
    try:
        from ue2.reader import read_compact_index_at
        pos = 0
        prop_end = 0
        while pos < len(data):
            start_pos = pos
            name_idx, next_pos = read_compact_index_at(data, pos)
            if name_idx == 0:
                print(f"[{start_pos:04x}] 00 (None)")
                prop_end = next_pos
                break
            
            name = pkg.names[name_idx] if 0 <= name_idx < len(pkg.names) else f"INVALID({name_idx})"
            info = data[next_pos]
            p_type = info & 0x0F
            s_type = (info >> 4) & 0x07
            is_arr = (info & 0x80) != 0
            
            print(f"[{start_pos:04x}] {name} (T:{p_type} S:{s_type} A:{is_arr})", end="")
            
            pos = next_pos + 1
            if p_type == 10: # Struct
                s_name_idx, pos = read_compact_index_at(data, pos)
                s_name = pkg.names[s_name_idx] if 0 <= s_name_idx < len(pkg.names) else "INV"
                print(f" Struct:{s_name}", end="")
            
            size = 0
            size_bytes = 0
            if p_type == 3: size = 0
            elif s_type == 0: size = 1
            elif s_type == 1: size = 2
            elif s_type == 2: size = 4
            elif s_type == 3: size = 12
            elif s_type == 4: size = 16
            elif s_type == 5:
                size = data[pos]; size_bytes = 1; pos += 1
            elif s_type == 6:
                size = struct.unpack('<H', data[pos:pos+2])[0]; size_bytes = 2; pos += 2
            elif s_type == 7:
                size = struct.unpack('<I', data[pos:pos+4])[0]; size_bytes = 4; pos += 4
            
            arr_idx_size = 0
            if is_arr and p_type != 3:
                b = data[pos]
                arr_idx_size = 1
                if b >= 128:
                    if b & 0x40: arr_idx_size = 4
                    else: arr_idx_size = 2
            
            if s_type >= 5:
                exclusive_pos = pos + arr_idx_size + size
                inclusive_pos = pos + arr_idx_size + size - size_bytes
                
                inc_valid = is_valid_property_start(data, inclusive_pos, pkg.names)
                exc_valid = is_valid_property_start(data, exclusive_pos, pkg.names)
                
                if inc_valid and exc_valid:
                    if data[inclusive_pos] != 0 and data[exclusive_pos] == 0:
                        print(f" -> Size:{size} [Inclusive! (Prop vs None)]")
                        pos = inclusive_pos
                    elif data[exclusive_pos] != 0 and data[inclusive_pos] == 0:
                        print(f" -> Size:{size} [Exclusive! (Prop vs None)]")
                        pos = exclusive_pos
                    else:
                        print(f" -> Size:{size} [Exclusive! (Both valid)]")
                        pos = exclusive_pos
                elif inc_valid:
                    print(f" -> Size:{size} [Inclusive! (Only valid)]")
                    pos = inclusive_pos
                else:
                    print(f" -> Size:{size} [Exclusive! (Fallback)]")
                    pos = exclusive_pos
            else:
                print(f" -> Size:{size}")
                pos = pos + arr_idx_size + size

        print(f"Properties end at: {prop_end:04x}")
    except Exception as e:
        print(f"\nProperty parsing FAILED: {e}")
        prop_end = 0

    # 2. Search for anchors (Discovery for core fields)
    v_anchors = [11, 12, 128, 129, 60482, 60484, 60485]
    print("\n--- Anchor Search (InternalVersion) ---")
    for offset in range(0, min(len(data) - 8, 10240)):
        ver = struct.unpack("<I", data[offset : offset + 4])[0]
        if ver in v_anchors:
            sc = struct.unpack("<I", data[offset + 4 : offset + 8])[0]
            if 0 <= sc < 1000:
                print(f"FOUND ANCHOR at {offset:04x}: Version={ver}, SecCount={sc}")

    # 3. Call the real parser to see if it succeeds
    print("\n--- Full Parse Attempt ---")
    try:
        result = parse_staticmesh(data, pkg.names, serial_offset)
        print(f"Status: {result.get('parse_status')}")
        if result.get('parse_status') == 'success':
            print(f"Coverage: {result['coverage_pct']:.1f}%")
            if 'core' in result['data']:
                core = result['data']['core']
                print(f"InternalVersion: {core['internal_version']}")
                print(f"SectionCount: {core['section_count']}")
            if 'lods' in result['data']:
                for i, lod in enumerate(result['data']['lods']):
                    print(f"LOD {i}: {lod['vertex_count']} verts, {lod['index_count']} indices")
        else:
            print(f"Error: {result.get('error_message')}")
    except Exception as e:
        print(f"Parser CRASHED: {e}")

    # 4. Hex dump first 512 bytes
    print("\n--- Hex Dump (First 512 bytes) ---")
    for i in range(0, min(len(data), 512), 16):
        chunk = data[i:i+16]
        print(f"{i:04x}: {chunk.hex(' '):<48} | {''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)}")

if __name__ == "__main__":
    pkg_path = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra0001_P0001_C0001_Grid_House_mesh.usx"
    
    meshes = [
        "Ra1_P1_C1_Grid_House_ext006_type01",
        "Ra1_P1_C1_Grid_House_ext006_type01_L0",
        "Ra1_P1_C1_Grid_House_ext006_type01_L1"
    ]
    
    for mesh_name in meshes:
        scan_mesh(pkg_path, mesh_name)
