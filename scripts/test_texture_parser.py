import sys
import os
import struct

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from ue2.package import UE2Package
from ue2.texture import Texture
import config

def test_texture_parsing(chunk_name):
    vgr_path = os.path.join(config.ASSETS_PATH, 'Maps', f'{chunk_name}.vgr')
    if not os.path.exists(vgr_path):
        print(f"File not found: {vgr_path}")
        return

    pkg = UE2Package(vgr_path)
    # Find the baseColor texture
    search_coord = chunk_name.replace("chunk_", "").lower()
    for exp in pkg.exports:
        if exp["class_name"] == "Texture" and "basecolor" in exp["object_name"].lower():
            print(f"\nParsing Texture: {exp['object_name']}")
            data = pkg.get_export_data(exp)
            print(f"Data length: {len(data)}")
            
            try:
                tex = Texture(data, pkg.names)
                print(f"Properties end: {tex.properties_end}")
                print(f"Parsed Properties: {list(tex.properties.keys())}")
                print(f"USize: {tex.u_size}, VSize: {tex.v_size}, Format: {tex.format_id}")
                
                print(f"Found {len(tex.mips)} mips")
                for i, mip in enumerate(tex.mips):
                    print(f"  Mip {i}: {mip.width}x{mip.height}, Data size: {len(mip.data)}")
                
                # Check byte coverage
                last_pos = tex.reader.tell()
                coverage = (last_pos / len(data)) * 100
                print(f"Byte coverage: {last_pos}/{len(data)} ({coverage:.2f}%)")
                if last_pos < len(data):
                    print(f"  Trailing bytes: {data[last_pos:].hex()[:64]}...")
            except Exception as e:
                print(f"Error parsing texture: {e}")
                import traceback
                traceback.print_exc()

if __name__ == "__main__":
    test_texture_parsing("chunk_n15_n9")
    test_texture_parsing("chunk_n17_5")
