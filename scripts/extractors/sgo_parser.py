#!/usr/bin/env python3
import struct
import os
import sqlite3
from typing import Dict, List, Any

class SGOParser:
    def __init__(self, sgo_path: str):
        self.sgo_path = sgo_path
        self.data = open(sgo_path, 'rb').read()
        self.strings = self._load_strings()
        
    def _load_strings(self) -> List[str]:
        # Based on my analysis, strings start at 0x48
        # and are [len][bytes][null]
        strings = []
        pos = 0x48
        while pos < 0x80000: # Heuristic end of string table
            l = self.data[pos]
            if l == 0:
                pos += 1
                continue
            if 2 < l < 127:
                try:
                    s_bytes = self.data[pos+1 : pos+1+l]
                    s = s_bytes.decode('utf-8').rstrip('\x00')
                    if all(32 <= ord(c) < 127 for c in s):
                        strings.append(s)
                        pos += l + 1
                        while pos < len(self.data) and self.data[pos] == 0: pos += 1
                        continue
                except: pass
            pos += 1
            if len(strings) > 20000: break
        return strings

    def get_string(self, idx: int) -> str:
        if 0 <= idx < len(self.strings):
            return self.strings[idx]
        return f"Unknown_{idx}"

    def find_prefab(self, prefab_name: str) -> List[Dict]:
        marker = (prefab_name + "exportBinaryPrefab").encode('utf-8')
        idx = self.data.find(marker)
        if idx == -1:
            # Try without the full suffix if it differs
            idx = self.data.find(prefab_name.encode('utf-8'))
            if idx == -1: return []
            
        print(f"Found prefab marker for {prefab_name} near 0x{idx:x}")
        
        # We need to find all components associated with this prefab.
        # Prefab components seem to be listed in the string table or inline near the marker.
        # Let's look at 2000 bytes before the marker.
        start_search = max(0, idx - 4000)
        search_area = self.data[start_search : idx + 128]
        
        components = []
        # Search for actor name strings in the search area
        actor_marker = (prefab_name + "StaticMeshActor").encode('utf-8')
        curr = 0
        while True:
            c_pos = search_area.find(actor_marker, curr)
            if c_pos == -1: break
            
            # Find the full name string
            end_name = search_area.find(b'\x00', c_pos)
            if end_name == -1: end_name = c_pos + 64
            comp_name = search_area[c_pos:end_name].decode('utf-8', errors='ignore')
            
            comp = {"name": comp_name, "mesh": None, "pos": [0,0,0], "rot": [0,0,0], "scale": [1,1,1]}
            
            # Look at the data following the name string for properties
            # Local offset in search_area
            prop_data = search_area[end_name : end_name + 512]
            
            # Look for StaticMesh (12)
            # Look for Location (9)
            # Look for DrawScale (some index?)
            
            # Heuristic for StaticMesh: Look for "Ra" strings in the next few hundred bytes
            for s in self.strings:
                if len(s) > 10 and s.startswith("Ra") and s.encode('utf-8') in prop_data:
                    comp["mesh"] = s
                    break
            
            # Heuristic for Location: Look for index 9
            for j in range(len(prop_data) - 13):
                if prop_data[j] == 9: # Location
                    try:
                        # Skip property header (usually 4 bytes)
                        raw = prop_data[j+4 : j+16]
                        x, y, z = struct.unpack('<fff', raw)
                        if abs(x) < 50000 and abs(y) < 50000 and abs(z) < 50000:
                            comp["pos"] = [x, y, z]
                            break
                    except: pass

            components.append(comp)
            curr = c_pos + 1
            
        return components

if __name__ == "__main__":
    import sys
    sgo = SGOParser("/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Archives/binaryprefabs.sgo")
    if len(sys.argv) > 1:
        res = sgo.find_prefab(sys.argv[1])
        for c in res:
            print(c)
