#!/usr/bin/env python3
import struct
import os
import json
import sqlite3
from typing import Dict, List, Any

class PrefabResolver:
    def __init__(self, sgo_path: str):
        self.sgo_path = sgo_path
        if not os.path.exists(sgo_path):
            raise FileNotFoundError(f"SGO file not found: {sgo_path}")
        self.data = open(sgo_path, 'rb').read()
        self.strings = self._load_strings()
        
    def _load_strings(self) -> List[str]:
        strings = []
        pos = 0x48 # Known start of string table
        # Read up to 2MB or until data ends
        while pos < min(len(self.data) - 4, 300000):
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
        return strings

    def resolve_prefab(self, prefab_name: str, mesh_db_path: str, seen=None, depth=0) -> List[Dict]:
        """Explode a prefab name into its component meshes and their relative transforms."""
        if seen is None:
            seen = set()
        
        # Prevent infinite recursion
        if prefab_name in seen or depth > 5:
            return []
        seen.add(prefab_name)
        
        components = []
        
        # Find the prefab block in the SGO
        # Prefabs are marked with [Name] followed by "exportBinaryPrefab"
        marker = prefab_name.encode('utf-8') + b"exportBinaryPrefab"
        idx = self.data.find(marker)
        if idx == -1:
            return []
            
        print(f"DEBUG: Found marker at {idx}")

        # Define search window: The components are usually listed BEFORE the export marker
        # We must stop at the PREVIOUS exportBinaryPrefab to avoid bleeding into the previous record.
        prev_marker = self.data.rfind(b"exportBinaryPrefab", 0, idx)
        if prev_marker != -1:
            start_search = prev_marker + len(b"exportBinaryPrefab")
        else:
            start_search = max(0, idx - 8000) # Fallback if first in file
            
        # Limit the forward search too, just in case
        end_search = idx + 200
        
        block = self.data[start_search:end_search]
        
        components = []
        
        # Load known meshes from DB to know what to look for
        conn = sqlite3.connect(mesh_db_path)
        cursor = conn.execute("SELECT object_name FROM mesh_index WHERE class_name='StaticMesh'")
        known_meshes = {row[0] for row in cursor.fetchall()}
        conn.close()
        
        # Optimization: Filter known_meshes to those that might be related
        # e.g. same package prefix "Ra3_P1_C1"
        # Relaxed: Just use all meshes that start with "Ra" to be safe, 
        # as naming conventions vary (Ra3 vs Ra0003)
        candidates = [m for m in known_meshes if m.startswith("Ra")]
        
        # Search for these mesh names in the block
        found_meshes = []
        for mesh in candidates:
            m_bytes = mesh.encode('utf-8')
            # Simple find
            m_pos = block.find(m_bytes)
            while m_pos != -1:
                # Check if it's likely a real reference (surrounded by valid bytes)
                found_meshes.append((mesh, m_pos))
                m_pos = block.find(m_bytes, m_pos + 1)
                
        # Sort by position
        found_meshes.sort(key=lambda x: x[1])
        
        # Filter duplicates (sometimes names appear twice) - keep unique scan spots
        unique_spots = []
        last_pos = -1000
        for name, pos in found_meshes:
            if pos > last_pos + 40: # min distance
                unique_spots.append((name, pos))
                last_pos = pos
        
        for mesh_name, m_pos in unique_spots:
            # Check if this mesh_name is actually another prefab
            # (Prefabs often start with package names or specific prefixes)
            # We'll check if the name exists as a prefab marker elsewhere in the SGO.
            is_nested_prefab = False
            if self.data.find(mesh_name.encode('utf-8') + b"exportBinaryPrefab") != -1:
                is_nested_prefab = True

            comp = {
                "mesh": mesh_name,
                "pos": [0.0, 0.0, 0.0],
                "rot": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
                "is_nested": is_nested_prefab
            }
            
            # Scan for properties near the mesh name
            search_start = max(0, m_pos - 400)
            search_end = min(len(block), m_pos + 400)
            scan_sub = block[search_start : search_end]
            
            # Priority 1: Vector/Location
            center = m_pos - search_start
            best_dist = 9999
            found_pos = False
            
            for j in range(len(scan_sub) - 14):
                if scan_sub[j:j+2] == b'\x0c\x3a':
                    try:
                        vals = struct.unpack('<fff', scan_sub[j+2 : j+14])
                        if all(abs(v) < 100000 for v in vals):
                            dist = abs(j - center)
                            if dist < best_dist:
                                comp["pos"] = [vals[0], vals[1], vals[2]]
                                best_dist = dist
                                found_pos = True
                    except: pass
            
            # Priority 2: X,Y,Z
            if not found_pos:
                try:
                    x_idx = scan_sub.find(b'\x0d\x24')
                    y_idx = scan_sub.find(b'\x0e\x24')
                    z_idx = scan_sub.find(b'\x0f\x24')
                    if x_idx != -1 and y_idx != -1 and z_idx != -1:
                        x = struct.unpack('<f', scan_sub[x_idx+2 : x_idx+6])[0]
                        y = struct.unpack('<f', scan_sub[y_idx+2 : y_idx+6])[0]
                        z = struct.unpack('<f', scan_sub[z_idx+2 : z_idx+6])[0]
                        comp["pos"] = [x, y, z]
                        found_pos = True
                except: pass

            # Look for Scaling (DrawScale or DrawScale3D)
            # Pattern: Index 10 (0x0A) followed by float
            ds_idx = scan_sub.find(b'\x0a\x24') # Index 10, Float type (0x24)
            if ds_idx != -1:
                try:
                    s = struct.unpack('<f', scan_sub[ds_idx+2 : ds_idx+6])[0]
                    if 0.001 < s < 100.0:
                        comp["scale"] = [s, s, s]
                except: pass

            if not found_pos:
                # Priority 3: Brute force float scan with very strict "Reasonable Float" filter
                for j in range(0, len(scan_sub) - 12, 1):
                    try:
                        vals = struct.unpack('<fff', scan_sub[j:j+12])
                        # A "Reasonable Float" in Vanguard:
                        # 1. Not microscopic (denormal)
                        # 2. Not huge (> 50000)
                        # 3. Often has some decimal or large magnitude (not just 1e-30)
                        
                        valid = True
                        if any(abs(v) > 50000 for v in vals): valid = False
                        
                        meaningful = False
                        for v in vals:
                            if abs(v) > 0.1: meaningful = True
                            if v != 0.0 and abs(v) < 1e-4: # Too small, probably garbage
                                valid = False
                                break
                        
                        if valid and (meaningful or all(v == 0.0 for v in vals)):
                            comp["pos"] = [vals[0], vals[1], vals[2]]
                            found_pos = True
                            break
                    except: pass
            
            if is_nested_prefab:
                # Recursively resolve!
                nested_comps = self.resolve_prefab(mesh_name, mesh_db_path, seen, depth + 1)
                for nc in nested_comps:
                    # Transform nested component by parent's relative transform
                    # (Simplified: just add parent position for now)
                    nc["pos"][0] += comp["pos"][0]
                    nc["pos"][1] += comp["pos"][1]
                    nc["pos"][2] += comp["pos"][2]
                    # Pass through scale
                    nc["scale"][0] *= comp["scale"][0]
                    nc["scale"][1] *= comp["scale"][1]
                    nc["scale"][2] *= comp["scale"][2]
                    components.append(nc)
            else:
                components.append(comp)

        return components

if __name__ == "__main__":
    import sys
    # Add parent directory to path to find config
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config
    
    SGO_PATH = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Archives/binaryprefabs.sgo"
    MESH_DB_PATH = os.path.join(config.DATA_DIR, "mesh_index.sqlite")
    
    resolver = PrefabResolver(SGO_PATH)
    
    if len(sys.argv) > 1:
        name = sys.argv[1]
        res = resolver.resolve_prefab(name, MESH_DB_PATH)
        print(json.dumps(res, indent=2))
    else:
        # Test known cases
        print("Curved Fence:")
        print(json.dumps(resolver.resolve_prefab("Ra3_P1_C1_Decor_fence001_curve01", MESH_DB_PATH), indent=2))
        print("\nBench:")
        print(json.dumps(resolver.resolve_prefab("Ra3_P1_C1_Decor_bench001", MESH_DB_PATH), indent=2))
