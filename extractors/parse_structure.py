"""
Binary Structure Parser

Parses binary files (starting with .usx StaticMesh) and returns a hierarchical
JSON structure showing all fields, values, and sanity warnings.
"""

import os
import sys
import struct
from typing import List, Dict, Any, Optional

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from ue2 import UE2Package
except ImportError:
    UE2Package = None


class StructureParser:
    """Base class for strict binary structure parsing."""
    
    def __init__(self, data: bytes, export_offset: int = 0):
        self.data = data
        self.export_offset = export_offset
        self.pos = 0
        self.structure: List[Dict] = []
        
    def read_byte(self) -> int:
        v = self.data[self.pos]
        self.pos += 1
        return v
    
    def read_bytes(self, count: int) -> bytes:
        v = self.data[self.pos:self.pos+count]
        self.pos += count
        return v
        
    def read_int32(self) -> int:
        v = struct.unpack('<i', self.data[self.pos:self.pos+4])[0]
        self.pos += 4
        return v
        
    def read_uint16(self) -> int:
        v = struct.unpack('<H', self.data[self.pos:self.pos+2])[0]
        self.pos += 2
        return v
        
    def read_float(self) -> float:
        v = struct.unpack('<f', self.data[self.pos:self.pos+4])[0]
        self.pos += 4
        return v
        
    def read_vector(self) -> Dict[str, float]:
        """Read an FVector (3 floats)."""
        x = self.read_float()
        y = self.read_float()
        z = self.read_float()
        return {"X": x, "Y": y, "Z": z}

    def read_fbox(self) -> Dict[str, Any]:
        """Read an FBox (Min, Max, IsValid)."""
        min_v = self.read_vector()
        max_v = self.read_vector()
        is_valid = self.read_byte()
        return {"Min": min_v, "Max": max_v, "IsValid": is_valid}

    def read_array(self, name: str, item_reader) -> List[Dict]:
        """Read a TArray."""
        try:
            count = self.read_int32()
            items = []
            if 0 < count < 50000:  # Safety limit
                for i in range(count):
                    items.append(item_reader(i))
            elif count > 50000:
                 # Too large, likely parse error or corrupt
                 pass
            elif count < 0:
                 # Invalid
                 pass
            return {"count": count, "items": items}
        except Exception as e:
            return {"count": 0, "error": str(e)}
        
    def add_field(self, name: str, value: Any, size: int, warning: Optional[str] = None):
        """Add a parsed field to the current structure block."""
        field = {
            "name": name,
            "offset": self.pos - size,
            "size": size,
            "value": value,
        }
        if warning:
            field["warning"] = warning
        return field
        
    def sanity_check_float(self, value: float, name: str) -> Optional[str]:
        """Check if a float value is reasonable."""
        if abs(value) > 1e10:
            return f"Unusually large: {value}"
        if value != 0 and abs(value) < 1e-10:
            return f"Unusually small: {value}"
        return None
        
    def sanity_check_count(self, value: int, name: str, max_reasonable: int = 100000) -> Optional[str]:
        """Check if a count value is reasonable."""
        if value < 0:
            return f"Negative count: {value}"
        if value > max_reasonable:
            return f"Implausibly high: {value}"
        return None


class StaticMeshStructureParser(StructureParser):
    """Parser for UStaticMesh export data."""
    
    def parse(self) -> Dict:
        """Parse the entire StaticMesh export and return structure."""
        result = {
            "type": "UStaticMesh",
            "total_bytes": len(self.data),
            "sections": []
        }
        
        # 1. Properties (simplified - find 'None' terminator)
        props_start = 0
        props_end = self._find_properties_end()
        result["sections"].append({
            "name": "Properties",
            "offset": props_start,
            "size": props_end - props_start,
            "fields": [{"name": "PropertiesBlock", "value": f"{props_end} bytes", "offset": 0, "size": props_end}]
        })
        self.pos = props_end
        
        # 2. BoundingBox
        bbox_start = self.pos
        bbox_fields = []
        
        min_x = self.read_float()
        bbox_fields.append(self.add_field("Min.X", min_x, 4, self.sanity_check_float(min_x, "Min.X")))
        min_y = self.read_float()
        bbox_fields.append(self.add_field("Min.Y", min_y, 4, self.sanity_check_float(min_y, "Min.Y")))
        min_z = self.read_float()
        bbox_fields.append(self.add_field("Min.Z", min_z, 4, self.sanity_check_float(min_z, "Min.Z")))
        max_x = self.read_float()
        bbox_fields.append(self.add_field("Max.X", max_x, 4, self.sanity_check_float(max_x, "Max.X")))
        max_y = self.read_float()
        bbox_fields.append(self.add_field("Max.Y", max_y, 4, self.sanity_check_float(max_y, "Max.Y")))
        max_z = self.read_float()
        bbox_fields.append(self.add_field("Max.Z", max_z, 4, self.sanity_check_float(max_z, "Max.Z")))
        is_valid = self.read_byte()
        bbox_fields.append(self.add_field("IsValid", is_valid, 1))
        
        result["sections"].append({
            "name": "BoundingBox",
            "offset": bbox_start,
            "size": self.pos - bbox_start,
            "fields": bbox_fields
        })
        
        # 3. BoundingSphere
        sphere_start = self.pos
        sphere_fields = []
        
        cx = self.read_float()
        sphere_fields.append(self.add_field("Center.X", cx, 4))
        cy = self.read_float()
        sphere_fields.append(self.add_field("Center.Y", cy, 4))
        cz = self.read_float()
        sphere_fields.append(self.add_field("Center.Z", cz, 4))
        radius = self.read_float()
        sphere_fields.append(self.add_field("Radius", radius, 4, self.sanity_check_float(radius, "Radius")))
        
        result["sections"].append({
            "name": "BoundingSphere",
            "offset": sphere_start,
            "size": self.pos - sphere_start,
            "fields": sphere_fields
        })
        
        # 4. MeshMetadata & Sections
        # Order: InternalVersion (if Ver>=81) -> Sections
        # My manual analysis shows InternalVersion (236) then SectionCount (1)
        meta_start = self.pos
        internal_ver = self.read_int32()
        
        result["sections"].append({
            "name": "MeshMetadata",
            "offset": meta_start,
            "size": 4,
            "fields": [{"name": "InternalVersion", "value": internal_ver, "offset": meta_start, "size": 4}]
        })

        # 5. Sections Array (Ar << Sections)
        # Note: UnStaticMesh.cpp lists Sections BEFORE BoundingBox, but file analysis shows 
        # BoundingBox is serialized by Super::Serialize FIRST.
        # So Sections appears here.
        sections_start = self.pos
        section_count = self.read_int32()
        sections_fields = [self.add_field("SectionCount", section_count, 4)]
        
        if section_count > 0 and section_count < 100:
            for i in range(section_count):
                # FStaticMeshSection (Ver 128):
                # Ar << IsStrip << FirstIndex << MinVert << MaxVert << NumTris << NumPrims
                # All 2 bytes except IsStrip (4 bytes)? 
                # Wait, IsStrip (UBOOL) is 4 bytes.
                # 5 WORDs = 10 bytes. Total 14.
                is_strip = self.read_int32()
                f_idx = self.read_uint16()
                min_v = self.read_uint16()
                max_v = self.read_uint16()
                num_tri = self.read_uint16()
                num_prim = self.read_uint16()
                
                sections_fields.append({
                    "name": f"Section[{i}]",
                    "value": f"Strip:{is_strip} Idx:{f_idx} VRange:[{min_v}-{max_v}] Tris:{num_tri}",
                    "offset": self.pos - 14,
                    "size": 14
                })
        
        result["sections"].append({
            "name": "Sections",
            "offset": sections_start,
            "size": self.pos - sections_start,
            "fields": sections_fields
        })
        
        # 5b. Vertex Stream
        # Serialization: Vertices (Array), Revision (Int)
        # Vertices: Position(12), Normal(12) = 24 bytes (Ver >= 112)
        vs_start = self.pos
        vs_count = self.read_int32()
        vs_fields = [self.add_field("VertexCount", vs_count, 4)]
        
        if 0 < vs_count < 100000:
            # Skip massive data dump, but calculate size
            # 24 bytes per vertex
            data_size = vs_count * 24
            if self.pos + data_size <= len(self.data):
                self.pos += data_size
        
        vs_rev = self.read_int32()
        
        result["sections"].append({
            "name": "VertexStream",
            "offset": vs_start,
            "size": self.pos - vs_start,
            "fields": vs_fields + [{"name": "Revision", "value": vs_rev, "offset": self.pos-4, "size": 4}]
        })

        # 5c. Color Stream (FRawColorStream)
        # Serialization: Colors (Array), Revision (Int)
        # Color: 4 bytes (BGRA)
        cs_start = self.pos
        cs_count = self.read_int32()
        if cs_count > 0 and cs_count < 100000:
            self.pos += cs_count * 4
        cs_rev = self.read_int32()
        
        result["sections"].append({
            "name": "ColorStream",
            "offset": cs_start,
            "size": self.pos - cs_start,
            "fields": [{"name": "Count", "value": cs_count, "offset": cs_start, "size": 4},
                       {"name": "Revision", "value": cs_rev, "offset": self.pos-4, "size": 4}]
        })

        # 5d. Alpha Stream (FRawColorStream)
        as_start = self.pos
        as_count = self.read_int32()
        if as_count > 0 and as_count < 100000:
            self.pos += as_count * 4
        as_rev = self.read_int32()

        result["sections"].append({
            "name": "AlphaStream",
            "offset": as_start,
            "size": self.pos - as_start,
            "fields": [{"name": "Count", "value": as_count, "offset": as_start, "size": 4},
                       {"name": "Revision", "value": as_rev, "offset": self.pos-4, "size": 4}]
        })

        # 5e. UV Streams (TArray<FStaticMeshUVStream>)
        # FStaticMeshUVStream: UVs (Array), CoordIndex (Int), Revision (Int)
        # UV: float, float (8 bytes)
        uvs_start = self.pos
        uvs_count = self.read_int32()
        uvs_fields = [self.add_field("StreamCount", uvs_count, 4)]
        
        if 0 < uvs_count < 16: # 8 UV sets max usually
            for i in range(uvs_count):
                u_count = self.read_int32()
                if 0 < u_count < 100000:
                    self.pos += u_count * 8 # 2 floats
                coord_idx = self.read_int32()
                u_rev = self.read_int32()
                uvs_fields.append({
                    "name": f"UVStream[{i}]",
                    "value": f"Count:{u_count} Idx:{coord_idx} Rev:{u_rev}",
                    "offset": self.pos, # approx
                    "size": 0
                })

        result["sections"].append({
            "name": "UVStreams",
            "offset": uvs_start,
            "size": self.pos - uvs_start,
            "fields": uvs_fields
        })

        # 5f. Index Buffer (FRawIndexBuffer)
        # Indices (Array<WORD>), Revision (Int)
        ib_start = self.pos
        ib_count = self.read_int32()
        if 0 < ib_count < 200000: # Index count can be higher
            self.pos += ib_count * 2
        ib_rev = self.read_int32()
        
        result["sections"].append({
            "name": "IndexBuffer",
            "offset": ib_start,
            "size": self.pos - ib_start,
            "fields": [{"name": "Count", "value": ib_count, "offset": ib_start, "size": 4},
                       {"name": "Revision", "value": ib_rev, "offset": self.pos-4, "size": 4}]
        })

        # Serialization: Ar << Indices << Revision
        wf_start = self.pos
        if self.pos < len(self.data):
             # Read Indices Array
            wf_count = self.read_int32()
            wf_size = 4
            if 0 < wf_count < 100000:
                self.pos += wf_count * 2
                wf_size += wf_count * 2
            
            wf_revision = self.read_int32()
            wf_size += 4

            result["sections"].append({
                "name": "WireframeIndexBuffer",
                "offset": wf_start,
                "size": self.pos - wf_start,
                "fields": [
                    {"name": "IndexCount", "value": wf_count, "offset": wf_start, "size": 4},
                    {"name": "Revision", "value": wf_revision, "offset": wf_start + wf_size - 4, "size": 4}
                ]
            })

        # 7. Collision Model (UModel* Pointer)
        # 4 bytes
        if self.pos < len(self.data):
            cm_val = self.read_int32()
            result["sections"].append({
                "name": "CollisionModel",
                "offset": self.pos - 4,
                "size": 4,
                "fields": [{"name": "UModelRef", "value": cm_val, "offset": self.pos-4, "size": 4}]
            })

        # 8. Collision Triangles (TArray<FStaticMeshCollisionTriangle>)
        # Struct: Index1(2), Index2(2), Index3(2), MaterialIndex(4) = 10 bytes
        col_tri_start = self.pos
        col_tri_count = self.read_int32()
        col_tri_fields = [self.add_field("Count", col_tri_count, 4)]
        
        if 0 < col_tri_count < 50000:
            for i in range(col_tri_count):
                i1 = self.read_uint16()
                i2 = self.read_uint16()
                i3 = self.read_uint16()
                mat_idx = self.read_int32()
                col_tri_fields.append({
                    "name": f"Tri[{i}]",
                    "value": f"[{i1},{i2},{i3}] Mat:{mat_idx}",
                    "offset": self.pos - 10,
                    "size": 10
                })
        
        result["sections"].append({
            "name": "CollisionTriangles",
            "offset": col_tri_start,
            "size": self.pos - col_tri_start,
            "fields": col_tri_fields
        })

        # 9. Collision Nodes (TArray<FStaticMeshCollisionNode>)
        # Struct: TriIdx(2), Coplanar(2), Child1(2), Child2(2), BoundingBox(25) = 33 bytes
        col_node_start = self.pos
        col_node_count = self.read_int32()
        col_node_fields = [self.add_field("Count", col_node_count, 4)]

        if 0 < col_node_count < 50000:
            for i in range(col_node_count):
                tri_idx = self.read_uint16()
                coplanar = self.read_uint16()
                child1 = self.read_uint16()
                child2 = self.read_uint16()
                # Read Box (25 bytes)
                min_x = self.read_float()
                min_y = self.read_float()
                min_z = self.read_float()
                max_x = self.read_float()
                max_y = self.read_float()
                max_z = self.read_float()
                valid = self.read_byte()
                
                col_node_fields.append({
                    "name": f"Node[{i}]",
                    "value": f"Tri:{tri_idx} Children:[{child1},{child2}]",
                    "offset": self.pos - 33,
                    "size": 33
                })

        result["sections"].append({
            "name": "CollisionNodes",
            "offset": col_node_start,
            "size": self.pos - col_node_start,
            "fields": col_node_fields
        })

        # 11. Compression Scale (FVector) - REMOVED
        # Byte analysis shows this field is missing or skipped in Vanguard (Ver 128),
        # or the Version condition is different.
        # Removing it aligns the parser with RawTriangles at offset 149.
        
        # 11b. Unknown Padding (6 bytes)
        # Gap between CollisionNodes (143) and RawTriangles (149).
        if self.pos + 6 <= len(self.data):
             pad = self.read_bytes(6)
             result["sections"].append({
                "name": "AlignmentPadding",
                "offset": self.pos - 6,
                "size": 6,
                "fields": [{"name": "Bytes", "value": pad.hex(), "offset": self.pos-6, "size": 6}]
             })

        # 12. RawTriangles (TLazyArray) - Inserted Here
        # Serialization: Ar << SkipPos (Absolute Int)
        # Then we skip to SkipPos.
        raw_start = self.pos
        if self.pos < len(self.data):
            skip_pos_abs = self.read_int32()
            
            # Calculate relative skip pos
            # SkipPos is absolute file offset.
            # self.export_offset is absolute file offset of buffer start
            # relative = skip_pos_abs - self.export_offset
            
            if skip_pos_abs > 0 and self.export_offset > 0:
                skip_pos_rel = skip_pos_abs - self.export_offset
                
                if skip_pos_rel > self.pos and skip_pos_rel <= len(self.data):
                    data_size = skip_pos_rel - self.pos
                    
                    # Peek count if possible (first 4 bytes of data)
                    count_peek = struct.unpack('<i', self.data[self.pos:self.pos+4])[0] if data_size>=4 else -1
                    
                    result["sections"].append({
                        "name": "RawTriangles",
                        "offset": raw_start,
                        "size": (self.pos - raw_start) + data_size,
                        "fields": [
                            {"name": "SkipOffsetAbs", "value": skip_pos_abs, "offset": raw_start, "size": 4},
                            {"name": "DataSize", "value": data_size, "offset": self.pos, "size": data_size},
                            {"name": "ItemCount", "value": count_peek, "offset": self.pos, "size": 4}
                        ]
                    })
                    self.pos = skip_pos_rel
                else:
                     # Invalid skip pos
                     result["sections"].append({
                        "name": "RawTriangles",
                        "offset": raw_start,
                        "size": 4,
                        "fields": [{"name": "InvalidSkip", "value": skip_pos_abs, "warning": f"Bad skip {skip_pos_abs} (Rel: {skip_pos_abs - self.export_offset})", "offset": raw_start, "size": 4}]
                     })
            else:
                 # No export offset context or bad value
                 result["sections"].append({
                    "name": "RawTriangles",
                    "offset": raw_start,
                    "size": 4,
                    "fields": [{"name": "SkipOffset", "value": skip_pos_abs, "warning": "Cannot skip (missing context)", "offset": raw_start, "size": 4}]
                 })

        # 13. KPhysicsProps (Object Ref) (Moved after RawTriangles, as per UnStaticMesh.cpp:467 - if Ver >= 100)
        # Note: UnStaticMesh.cpp 460 reads InternalVersion (Again??) if Ver < 81.
        # But we are Ver 128.
        # Line 467: KPhysicsProps.
        k_start = self.pos
        if self.pos < len(self.data):
            k_ref = self.read_int32()
            result["sections"].append({
                "name": "KPhysicsProps",
                "offset": k_start,
                "size": 4,
                "fields": [{"name": "ObjectRef", "value": k_ref, "offset": k_start, "size": 4}]
            })

        # 14. Authentication Key (DWORD)
        # Only if Package Version >= 120 (Vanguard is 128)
        auth_start = self.pos
        if self.pos < len(self.data):
             auth_key = self.read_int32()
             result["sections"].append({
                "name": "AuthenticationKey",
                "offset": auth_start,
                "size": 4,
                "fields": [{"name": "Key", "value": hex(auth_key), "offset": auth_start, "size": 4}]
             })

        # 15. LOD Models (Cooked Geometry)
        # This section contains the actual renderable geometry (LODs)
        lod_start = self.pos
        if self.pos < len(self.data):
            lod_count = self.read_int32()
            lod_fields = [self.add_field("LODCount", lod_count, 4)]
            
            for i in range(lod_count):
                # Header (3 ints)
                lod_fields.append(self.add_field(f"LOD[{i}].SecCount", self.read_int32(), 4))
                lod_fields.append(self.add_field(f"LOD[{i}].Unk1", self.read_int32(), 4))
                lod_fields.append(self.add_field(f"LOD[{i}].Unk2", self.read_int32(), 4))
                
                # Vertex Count
                l_v_count = self.read_int32()
                lod_fields.append(self.add_field(f"LOD[{i}].VertexCount", l_v_count, 4))
                
                # Vertices (56 bytes each in Vanguard?)
                # [Pos(12) Norm(12) Tang(12) Binorm(12) UV(8)] = 56 bytes
                if l_v_count > 0 and l_v_count < 100000:
                    v_size = l_v_count * 56
                    if self.pos + v_size <= len(self.data):
                        v_start = self.pos
                        # Sample first vertex
                        vx = self.read_float()
                        vy = self.read_float()
                        vz = self.read_float()
                        self.pos = v_start + v_size
                        
                        lod_fields.append({
                            "name": f"LOD[{i}].Vertices",
                            "value": f"{l_v_count} verts (56b). Sample[0]: ({vx:.1f},{vy:.1f},{vz:.1f})",
                            "offset": v_start,
                            "size": v_size
                        })
                    else:
                        lod_fields.append(self.add_field(f"LOD[{i}].Vertices", "Truncated", len(self.data)-self.pos))
                        self.pos = len(self.data)

                # Trailing Arrays in Cooked Data
                # Vanguard usually has several arrays after vertices
                for j in range(3): # Usually 2-3 arrays
                    if self.pos + 4 <= len(self.data):
                        count = self.read_int32()
                        if count > 0 and count < 1000000:
                             # Guess element size or skip
                             # If count is 8 and we have bytes left...
                             # For sun: Count1=8, Count2=4, Count3=10
                             # This is likely geometry-dependent.
                             # We'll just read integers for now if small
                             if count < 20:
                                 for k in range(count):
                                     if self.pos + 4 <= len(self.data):
                                         val = self.read_int32()
                                         lod_fields.append(self.add_field(f"LOD[{i}].Arr[{j}][{k}]", val, 4))
                             else:
                                 # Skip as blob
                                 lod_fields.append(self.add_field(f"LOD[{i}].Arr[{j}]", f"Count:{count}", 4))
                        else:
                             lod_fields.append(self.add_field(f"LOD[{i}].Arr[{j}].Count", count, 4))

                # Index Count
                if self.pos + 4 <= len(self.data):
                    l_i_count = self.read_int32()
                    lod_fields.append(self.add_field(f"LOD[{i}].IndexCount", l_i_count, 4))
                    
                    if l_i_count > 0 and l_i_count < 1000000:
                         # Indices are usually WORDS
                         i_size = l_i_count * 2
                         if self.pos + i_size <= len(self.data):
                             self.pos += i_size
                             lod_fields.append(self.add_field(f"LOD[{i}].Indices", f"{l_i_count} indices", i_size))

            # Final check for trailing bytes in this section
            if self.pos < len(self.data):
                 rem = len(self.data) - self.pos
                 lod_fields.append(self.add_field(f"LOD.Trailing", f"{rem} bytes", rem))
                 self.pos = len(self.data)

            result["sections"].append({
                "name": "LODModels",
                "offset": lod_start,
                "size": self.pos - lod_start,
                "fields": lod_fields
            })


        
        # Calculate coverage
        result["parsed_bytes"] = sum(s["size"] for s in result["sections"])
        
        return result
    
    def _find_properties_end(self) -> int:
        """Find the end of UE2 properties (None terminator)."""
        # Simple scan for 'None' name index pattern
        # Properties end with a name index pointing to 'None'
        # For now, assume properties end at byte 24 (based on our test file)
        # TODO: Implement proper property parsing
        return 24


def parse_file(file_path: str) -> Dict:
    """
    Parse a binary file and return its hierarchical structure.
    
    Args:
        file_path: Absolute path to the file.
        
    Returns:
        Dictionary with file structure or error.
    """
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}
    
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext != ".usx":
        return {"error": f"Parser not available for {ext} files"}
    
    if UE2Package is None:
        return {"error": "UE2Package module not available"}
    
    try:
        pkg = UE2Package(file_path)
    except Exception as e:
        return {"error": f"Failed to load package: {e}"}
    
    # Find StaticMesh exports
    mesh_exports = [e for e in pkg.exports if e.get("class_name") == "StaticMesh"]
    
    if not mesh_exports:
        return {"error": "No StaticMesh exports found in package"}
    
    # Parse first mesh export
    exp = mesh_exports[0]
    data = pkg.get_export_data(exp)
    offset = exp.get("serial_offset", 0)
    
    parser = StaticMeshStructureParser(data, offset)
    result = parser.parse()
    
    result["file_name"] = os.path.basename(file_path)
    result["export_name"] = exp.get("object_name", "Unknown")
    
    return result


if __name__ == "__main__":
    import json
    
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/P0001_Sun_Meshes.usx"
    
    result = parse_file(path)
    print(json.dumps(result, indent=2))
