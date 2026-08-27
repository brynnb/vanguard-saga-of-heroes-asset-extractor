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

    def _extract_strings(self, block: bytes) -> List[tuple]:
        """Extract all length-prefixed strings from a binary block.
        Returns list of (offset, string) tuples."""
        results = []
        pos = 0
        while pos < len(block) - 4:
            l = block[pos]
            if 4 < l < 120:
                try:
                    s = block[pos+1:pos+1+l].decode('utf-8', errors='strict').rstrip('\x00')
                    if len(s) >= 4 and all(32 <= ord(c) < 127 for c in s):
                        results.append((pos, s))
                        pos += l + 1
                        continue
                except:
                    pass
            pos += 1
        return results

    def resolve_prefab(self, prefab_name: str, mesh_db_path: str = None, seen=None, depth=0) -> List[Dict]:
        """Explode a prefab name into its component meshes and their relative transforms.
        
        The SGO format stores each prefab as a block ending with:
            [PrefabName]exportBinaryPrefab[N]
        
        The block between the PREVIOUS exportBinaryPrefab marker and the current one
        contains: property name strings, mesh package name, component mesh names,
        and sub-actor definitions.
        
        We extract all readable strings from this block and identify:
        - Mesh package name (contains "_mesh" or "_Mesh")
        - Component mesh names (strings containing the package prefix)
        - Nested prefab refs (strings that have their own exportBinaryPrefab marker)
        """
        if seen is None:
            seen = set()
        
        if prefab_name in seen or depth > 5:
            return []
        seen.add(prefab_name)
        
        # Find the prefab's exportBinaryPrefab marker
        marker = prefab_name.encode('utf-8') + b"exportBinaryPrefab"
        idx = self.data.find(marker)
        if idx == -1:
            return []

        # Search window: from previous exportBinaryPrefab to current one
        prev_marker = self.data.rfind(b"exportBinaryPrefab", 0, idx)
        if prev_marker != -1:
            start_search = prev_marker + len(b"exportBinaryPrefab")
            # Skip past the number suffix after previous marker (e.g. "exportBinaryPrefab27")
            while start_search < idx and self.data[start_search:start_search+1].isdigit():
                start_search += 1
        else:
            start_search = max(0, idx - 4000)
        
        block = self.data[start_search:idx]
        
        # Extract all strings from the block
        strings = self._extract_strings(block)
        
        # Identify mesh package (contains "_mesh" or "_Mesh")  
        mesh_package = None
        for _, s in strings:
            if '_mesh' in s.lower() and not s.endswith('StaticMesh') and 'Meshes' not in s:
                mesh_package = s
                # The package name tells us the expected mesh name prefix
                break
            if '_Meshes' in s and not s.startswith('Ra0'):
                mesh_package = s
                break
        
        # Known UE2 property/class names to exclude
        SKIP_STRINGS = {
            'None', 'Class', 'Package', 'Engine', 'Core', 'Vector', 'Rotator',
            'StaticMesh', 'StaticMeshActor', 'CompoundObject', 'Mover',
            'bLightChanged', 'bSelected', 'bLockLocation', 'bSunLit', 'bInteriorLit',
            'Zone', 'iLeaf', 'ZoneNumber', 'Region', 'PointRegion',
            'Location', 'Rotation', 'DrawScale', 'CullDistance',
            'PrefabPackageName', 'PrefabName', 'm_CompoundObjectType',
            'ColLocation', 'Attached', 'MoveTime', 'MoverType', 'StayOpenTime',
            'OpeningEvent', 'ClosingEvent', 'KeyRot', 'DrawType',
            'walls', 'activator', 'benches', 'Bookcases',
        }
        
        # Find the LAST occurrence of the property block header before our marker.
        # Each sub-definition starts with a block of property name strings.
        # We look for the last "None" string followed by property names — that's
        # where OUR prefab's data starts (everything before is the previous prefab).
        last_none_offset = -1
        for offset, s in strings:
            if s == 'None':
                last_none_offset = offset
        
        # If we found a "None" boundary, only consider strings after it
        if last_none_offset >= 0:
            strings = [(o, s) for o, s in strings if o >= last_none_offset]
        
        # Filter to candidate mesh/prefab names:
        # - Not in skip list
        # - Not ending with StaticMeshActor/CompoundObject/exportBinaryPrefab
        # - Contains an underscore (mesh names always have them)
        candidates = []
        for offset, s in strings:
            if s in SKIP_STRINGS:
                continue
            if 'StaticMeshActor' in s or 'CompoundObject' in s or 'exportBinaryPrefab' in s:
                continue
            if 'Mover' in s and s.endswith(('Mover', 'Mover1', 'Mover17')):
                continue
            # Skip the prefab name when it appears with a suffix (actor refs)
            if s.startswith(prefab_name) and len(s) > len(prefab_name):
                continue
            if '_' not in s:
                continue
            # Must look like a game asset name (starts with Ra, P0, or similar prefix)
            if not any(s.startswith(p) for p in ('Ra', 'P0', 'P1', 'ship', 'RA')):
                continue
            # Skip if it's a package name (ends with _mesh or _Meshes or _prefab)
            if s.lower().endswith(('_mesh', '_meshes', '_prefab')):
                continue
            candidates.append((offset, s))
        
        # Special case: single-mesh prefab where mesh name == prefab name.
        # If we found no candidates but the mesh package exists, the prefab IS the mesh.
        if not candidates and mesh_package:
            candidates.append((0, prefab_name))
        
        components = []
        for offset, mesh_name in candidates:
            # Check if this is a nested prefab (has its own exportBinaryPrefab marker)
            is_nested = self.data.find(mesh_name.encode('utf-8') + b"exportBinaryPrefab") != -1
            
            comp = {
                "mesh": mesh_name,
                "package": mesh_package or "",
                "pos": [0.0, 0.0, 0.0],
                "rot": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
                "is_nested": is_nested,
            }
            
            if is_nested and depth < 5:
                nested_comps = self.resolve_prefab(mesh_name, mesh_db_path, seen, depth + 1)
                if nested_comps:
                    components.extend(nested_comps)
                else:
                    # Nested prefab couldn't be resolved further; keep as-is
                    components.append(comp)
            else:
                components.append(comp)

        return components

if __name__ == "__main__":
    import sys
    # Add parent directory to path to find config
    from vanguard_assets import config
    
    SGO_PATH = config.SGO_PATH
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
