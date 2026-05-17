import struct
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any, Optional
from ue2.reader import BinaryReader

@dataclass
class FVector:
    x: float
    y: float
    z: float

    @classmethod
    def read(cls, reader: BinaryReader) -> 'FVector':
        return cls(reader.read_float(), reader.read_float(), reader.read_float())

@dataclass
class FStaticMeshSection:
    is_strip: bool
    first_index: int
    min_vertex_index: int
    max_vertex_index: int
    num_triangles: int
    num_primitives: int

    @classmethod
    def read(cls, reader: BinaryReader) -> 'FStaticMeshSection':
        is_strip = reader.read_uint32()
        first_idx = reader.read_uint16()
        min_v = reader.read_uint16()
        max_v = reader.read_uint16()
        num_tri = reader.read_uint16()
        num_prim = reader.read_uint16()
        return cls(is_strip == 1, first_idx, min_v, max_v, num_tri, num_prim)

@dataclass
class FVanguardLODVertex:
    position: FVector
    normal: FVector
    tangent_x: FVector
    tangent_y: FVector
    u: float
    v: float

    @classmethod
    def read(cls, reader: BinaryReader) -> 'FVanguardLODVertex':
        pos = FVector.read(reader)
        norm = FVector.read(reader)
        tan_x = FVector.read(reader)
        tan_y = FVector.read(reader)
        u = reader.read_float()
        v = reader.read_float()
        return cls(pos, norm, tan_x, tan_y, u, v)

@dataclass
class FVanguardLODModel:
    vertices: List[FVanguardLODVertex]
    indices: List[int]
    sections: List[Any] = None

    @classmethod
    def read(cls, reader: BinaryReader, version: int) -> 'FVanguardLODModel':
        if version == 1:
            sec_count = reader.read_uint32()
            u1 = reader.read_uint32()
            v_count = reader.read_uint32()
        else: # version 0
            u1 = reader.read_uint32()
            u2 = reader.read_uint32()
            v_count = reader.read_uint32()
            u3 = reader.read_uint32()
            
        print(f"        VertexCount: {v_count}")
        vertices = []
        if 0 < v_count < 200000:
            for _ in range(v_count):
                vertices.append(FVanguardLODVertex.read(reader))
        
        # Robust Index Buffer Scan
        potential_indices = []
        for i in range(10): # Scan more slots
            if reader.remaining() < 4: break
            start_pos = reader.tell()
            count = reader.read_uint32()
            if count == 0: continue
            if count > 1000000:
                reader.seek(start_pos)
                break
            
            # Peek at data
            needed = count * 2
            if reader.remaining() < needed:
                reader.seek(start_pos)
                break
                
            data_bytes = reader.read_bytes(needed)
            words = struct.unpack(f'<{count}H', data_bytes)
            
            # Heuristic: Is this an index buffer?
            invalid = [w for w in words if w >= v_count]
            if words and len(invalid) < max(1, count * 0.05) and count >= 3:
                print(f"        Array[{i}] at @{start_pos} (Count: {count}) is a valid candidate. (Invalid: {len(invalid)})")
                potential_indices.append(list(words))
            else:
                print(f"        Array[{i}] at @{start_pos} (Count: {count}) ignored. (Invalid: {len(invalid)})")
            
        # Choose the largest candidate (likely the main geometry)
        indices = []
        if potential_indices:
            indices = max(potential_indices, key=len)
            print(f"        Selected main index buffer with {len(indices)} indices.")
            
        return cls(vertices, indices)

class VanguardMeshParser:
    """Universal Vanguard StaticMesh Parser."""
    
    @staticmethod
    def skip_properties(reader: BinaryReader, names: List[str]) -> int:
        while True:
            name_idx = reader.read_compact_index()
            if name_idx == 0 or abs(name_idx) >= len(names):
                break
            
            info = reader.read_uint8()
            prop_type = info & 0x0F
            size_type = (info >> 4) & 0x07
            is_array = (info & 0x80) != 0
            
            if prop_type == 10: # StructProperty
                reader.read_compact_index()
                
            size = 0
            if size_type == 0: size = 1
            elif size_type == 1: size = 2
            elif size_type == 2: size = 4
            elif size_type == 3: size = 12
            elif size_type == 4: size = 16
            elif size_type == 5: size = reader.read_uint8()
            elif size_type == 6: size = reader.read_uint16()
            elif size_type == 7: size = reader.read_uint32()
            
            if prop_type != 3 and is_array:
                b = reader.read_uint8()
                if b >= 128:
                    if b & 0x40:
                        reader.read_uint8(); reader.read_uint8(); reader.read_uint8()
                    else:
                        reader.read_uint8()
            
            if prop_type == 3: size = 0
            reader.read_bytes(size)
        return reader.tell()

    @staticmethod
    def parse(data: bytes, names: List[str], serial_offset: int = 0) -> Dict:
        reader = BinaryReader(data)
        VanguardMeshParser.skip_properties(reader, names)
        
        # Bounds (UPrimitive 41 bytes)
        bbox_min = FVector.read(reader)
        bbox_max = FVector.read(reader)
        is_valid = reader.read_uint8()
        bsphere_center = FVector.read(reader)
        bsphere_radius = reader.read_float()
        
        # Mesh (UStaticMesh)
        internal_ver = reader.read_int32()
        sec_count = reader.read_int32()
        for _ in range(sec_count): FStaticMeshSection.read(reader)
        
        # Streams
        v_count = reader.read_int32(); reader.read_bytes(v_count * 24); reader.read_int32()
        c_count = reader.read_int32(); reader.read_bytes(c_count * 4); reader.read_int32()
        a_count = reader.read_int32(); reader.read_bytes(a_count * 4); reader.read_int32()
        uv_count = reader.read_int32()
        for _ in range(uv_count):
            u_count = reader.read_int32(); reader.read_bytes(u_count * 8); reader.read_int32(); reader.read_int32()
        i_count = reader.read_int32(); reader.read_bytes(i_count * 2); reader.read_int32()
        w_count = reader.read_int32(); reader.read_bytes(w_count * 2); reader.read_int32()
        
        # Collision
        reader.read_int32() # ColModel
        ct_count = reader.read_int32(); reader.read_bytes(ct_count * 10)
        cn_count = reader.read_int32(); reader.read_bytes(cn_count * 33)
        
        # Alignment & Skip
        reader.read_bytes(6)
        skip_pos = reader.read_uint32()
        if skip_pos > serial_offset:
            rel_skip = skip_pos - serial_offset
            if rel_skip > reader.pos and rel_skip < len(data): reader.seek(rel_skip)
            
        # Physics/Auth
        reader.read_int32() # Physics
        reader.read_int32() # Auth
        
        # LODs
        lods = []
        if reader.remaining() >= 4:
            v_flag = reader.read_int32() # This is Version (0 or 1) OR LODCount if very large
            if v_flag > 10: # Likely an old build or direct LODCount
                lod_count = v_flag
                v_flag = 1 # Assume version 1 logic
            else:
                lod_count = reader.read_int32()
                if lod_count == 0 and reader.remaining() >= 4: # Retry logic
                    lod_count = reader.read_int32()

            print(f"    LODCount: {lod_count} (LOD Version Flag: {v_flag})")
            if 0 < lod_count < 10:
                for i in range(lod_count):
                    print(f"      Parsing LOD {i}...")
                    lods.append(FVanguardLODModel.read(reader, v_flag))
                    
        return {
            "version": internal_ver,
            "lods": lods,
            "bbox": {"min": bbox_min, "max": bbox_max}
        }
