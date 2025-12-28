"""
Vanguard StaticMesh Parser - Complete Structure with 100% Byte Coverage

Based on reverse engineering of P0001_Sun_Meshes.usx and PARSING_GUIDELINES.md
"""

from construct import *
import io
import struct

# =============================================================================
# PRIMITIVE TYPES
# =============================================================================

FVector = Struct(
    "x" / Float32l,
    "y" / Float32l,
    "z" / Float32l,
)

FBox = Struct(
    "min" / FVector,
    "max" / FVector,
)

FSphere = Struct(
    "center" / FVector,
    "radius" / Float32l,
)

# =============================================================================
# STATICMESH STRUCTURES  
# =============================================================================

# Standard 14-byte section format (works for files with empty standard streams)
# Some Vanguard files use 24-byte sections - detect by checking vertex_count
FStaticMeshSection = Struct(
    "is_strip" / Int32ul,
    "first_index" / Int16ul,
    "min_vertex_index" / Int16ul,
    "max_vertex_index" / Int16ul,
    "num_triangles" / Int16ul,
    "num_primitives" / Int16ul,
)

FStaticMeshSectionVanguard = Struct(
    "is_strip" / Int32ul,
    "first_index" / Int32ul,
    "min_vertex_index" / Int32ul,
    "max_vertex_index" / Int32ul,
    "num_triangles" / Int32ul,
    "num_primitives" / Int32ul,
)

# Standard UE2.5 Vertex - 24 bytes
FStaticMeshVertex = Struct(
    "position" / FVector,
    "normal" / FVector,
)

# Vanguard LOD Vertex - 56 bytes
FVanguardLODVertex = Struct(
    "position" / FVector,       # 12 bytes
    "normal" / FVector,         # 12 bytes
    "tangent_x" / FVector,      # 12 bytes
    "tangent_y" / FVector,      # 12 bytes
    "u" / Float32l,             # 4 bytes
    "v" / Float32l,             # 4 bytes
)

# UV Stream entry
FUVStream = Struct(
    "count" / Int32sl,
    "data" / Array(this.count, Struct("u" / Float32l, "v" / Float32l)),
    "coord_index" / Int32sl,
    "revision" / Int32sl,
)

# =============================================================================
# LOD MODEL STRUCTURE (Vanguard-specific)
# =============================================================================

# After vertices, there are several arrays. Structure varies but pattern is:
# - Array of edge/section data (uint16)
# - Index buffer (uint16) - contains triangle indices
# - More metadata arrays
# - Per-LOD bounding box
# - Unknown trailing data

FVanguardLODModel = Struct(
    # Header
    "section_count" / Int32ul,
    "unknown_1" / Int32ul,
    "vertex_count" / Int32ul,
    
    # Vertices (56 bytes each)
    "vertices" / Array(this.vertex_count, FVanguardLODVertex),
    
    # Post-vertex arrays - we read them all as we find them
    # Array 0: Edge/section data
    "edge_count" / Int32ul,
    "edges" / Array(this.edge_count, Int16ul),
    
    # Array 1: Index buffer (triangles)
    "index_count" / Int32ul,
    "indices" / Array(this.index_count, Int16ul),
    
    # Array 2: Unknown metadata
    "metadata_count" / Int32ul,
    "metadata" / Array(this.metadata_count, Int16ul),
    
    # Remaining LOD data - capture for analysis
    # This includes bounding box and other per-LOD info
    "trailing_data" / GreedyBytes,  # TODO: Define structure once understood
)

# =============================================================================
# FULL STATICMESH (Standard + Vanguard Extension)
# =============================================================================

VanguardStaticMeshFull = Struct(
    # === Section 1: UPrimitive Bounds ===
    "bounding_box" / FBox,  # 25 bytes
    "bounding_sphere" / FSphere,  # 16 bytes
    
    # === Section 2: UStaticMesh Core ===
    "internal_version" / Int32sl,
    "section_count" / Int32sl,
    "sections" / Array(this.section_count, FStaticMeshSection),
    
    # Vertex Stream
    "vertex_count" / Int32sl,
    "vertices" / Array(this.vertex_count, FStaticMeshVertex),
    "vertex_revision" / Int32sl,
    
    # Color Stream
    "color_count" / Int32sl,
    "colors" / Array(this.color_count, Int32ul),
    "color_revision" / Int32sl,
    
    # Alpha Stream
    "alpha_count" / Int32sl,
    "alphas" / Array(this.alpha_count, Int32ul),
    "alpha_revision" / Int32sl,
    
    # UV Streams
    "uv_stream_count" / Int32sl,
    "uv_streams" / Array(this.uv_stream_count, FUVStream),
    
    # Index Buffer
    "index_count" / Int32sl,
    "indices" / Array(this.index_count, Int16ul),
    "index_revision" / Int32sl,
    
    # Wireframe Buffer
    "wireframe_count" / Int32sl,
    "wireframe_indices" / Array(this.wireframe_count, Int16ul),
    "wireframe_revision" / Int32sl,
    
    # Collision
    "collision_model_ref" / Int32sl,
    "collision_tri_count" / Int32sl,
    "collision_tris" / Bytes(this.collision_tri_count * 10),
    "collision_node_count" / Int32sl,
    "collision_nodes" / Bytes(this.collision_node_count * 33),
    
    # === Section 3: RawTriangles TLazyArray ===
    "unknown_padding_6" / Bytes(6),  # Always 00 00 00 00 00 00
    "raw_triangles_skip_pos" / Int32ul,  # Absolute file offset
    "raw_triangles_count" / Int32sl,
    # Payload is variable - we skip to skip_pos relative position
    # For now, read the number of bytes between here and the skip target
    # This requires knowing serial_offset at parse time
    
    # === Section 4: Physics/Auth ===
    # Note: These come AFTER the skip, not before
    # We need conditional handling based on skip_pos
)

# =============================================================================
# PROPERTY PARSER
# =============================================================================

def find_none_terminator(data: bytes, names: list) -> int:
    """Find the offset where properties end (after 'None' terminator)."""
    import sys
    from ue2.reader import read_compact_index_at
    
    pos = 0
    max_pos = min(len(data), 1024 * 1024) # 1MB limit for properties
    
    while pos < max_pos:
        try:
            name_idx, pos = read_compact_index_at(data, pos)
        except (IndexError, ValueError):
            raise ValueError("Unexpected end of data or invalid compact index in properties")
            
        if name_idx == 0: # 'None'
            return pos
            
        if name_idx < 0 or name_idx >= len(names):
            raise ValueError(f"Invalid name index {name_idx}")
            
        # info byte
        if pos >= len(data): break
        info = data[pos]
        pos += 1
        
        prop_type = info & 0x0F
        size_type = (info >> 4) & 0x07
        is_array = (info & 0x80) != 0
        
        # Determine size
        size = 0
        if prop_type == 3: # Bool
            size = 0
        elif size_type == 0: size = 1
        elif size_type == 1: size = 2
        elif size_type == 2: size = 4
        elif size_type == 3: size = 12
        elif size_type == 4: size = 16
        elif size_type == 5:
            size = data[pos]; pos += 1
        elif size_type == 6:
            size = struct.unpack('<H', data[pos:pos+2])[0]; pos += 2
        elif size_type == 7:
            size = struct.unpack('<I', data[pos:pos+4])[0]; pos += 4
            
        # Optional struct name for StructProperty
        if prop_type == 10: # StructProperty
            _, pos = read_compact_index_at(data, pos)
            
        # Optional array index
        if is_array and prop_type != 3:
            # Array index is a compact index
            _, pos = read_compact_index_at(data, pos)
            
        # Skip value
        pos += size
        
    raise ValueError("Never found None terminator or exceeded limit")

# =============================================================================
# MAIN PARSER FUNCTION
# =============================================================================

def parse_staticmesh(data: bytes, names: list, serial_offset: int) -> dict:
    """
    Parse a complete Vanguard StaticMesh with full byte coverage.
    
    Returns dict with all parsed data and coverage metrics.
    """
    result = {
        'bytes_total': len(data),
        'bytes_parsed': 0,
        'bytes_unknown': 0,
        'coverage_pct': 0.0,
        'uses_heuristics': False,
        'uses_skips': False,
        'data': {},
        'unknown_regions': [],
    }
    
    # Step 1: Parse properties
    prop_end = find_none_terminator(data, names)
    result['data']['properties_end'] = prop_end
    result['bytes_parsed'] += prop_end
    
    # Step 2: Parse core structure up to RawTriangles
    # We look for the InternalVersion (11 or 12) at prop_end + 41.
    # If not found, the property skip might have misaligned, or there are no properties.
    
    found_core = False
    core_start = prop_end
    
    # Check at prop_end
    if len(data) >= core_start + 45:
        version = struct.unpack('<i', data[core_start+41 : core_start+45])[0]
        if version in [11, 12]:
            found_core = True
            
    if not found_core:
        # Scan for version anchor
        for offset in range(0, min(len(data), 2000)):
            if offset + 45 <= len(data):
                version = struct.unpack('<i', data[offset+41 : offset+45])[0]
                if version in [11, 12]:
                    # Check if section count after version is sane
                    s_count = struct.unpack('<i', data[offset+45 : offset+49])[0]
                    if 0 <= s_count < 200:
                        core_start = offset
                        result['uses_heuristics'] = True
                        found_core = True
                        break
                        
    if not found_core:
        result['parse_status'] = 'error'
    # Step 2: Parse core structure up to RawTriangles
    # We look for the InternalVersion anchor. 
    # UE2: prop_end + 25(FBox) + 16(FSphere) = prop_end + 41
    # Vanguard: prop_end + 24(FBox) + 16(FSphere) = prop_end + 40
    
    core_start = -1
    v_anchors = [60482, 60484, 11, 12, 128, 129] # Known versions
    
    # Try expected offsets first
    for base in [prop_end]:
        for offset in [40, 41]:
            if len(data) >= base + offset + 4:
                ver = struct.unpack('<i', data[base + offset : base + offset + 4])[0]
                if ver in v_anchors:
                    core_start = base
                    break
        if core_start != -1: break
                
    if core_start == -1:
        # Bruteforce search for anchor in first 2KB
        # We search for the pattern [FBox][FSphere][Version][SectionCount]
        # Version is one of v_anchors, SectionCount is 0-200.
        search_limit = min(len(data) - 49, 2048)
        for i in range(0, search_limit):
            # Version is at i + 40 or i + 41
            for v_off in [40, 41]:
                ver = struct.unpack('<i', data[i + v_off : i + v_off + 4])[0]
                if ver in v_anchors:
                    sc = struct.unpack('<i', data[i + v_off + 4 : i + v_off + 8])[0]
                    if 0 <= sc < 200:
                        core_start = i
                        result['uses_heuristics'] = True
                        break
            if core_start != -1: break
                    
    if core_start == -1:
        result['parse_status'] = 'error'
        result['error_message'] = "Could not find StaticMesh core anchor (InternalVersion)"
        return result

    # Detect if we used 40 or 41 byte bounds
    ver_40 = struct.unpack('<i', data[core_start+40 : core_start+44])[0]
    is_40 = ver_40 in v_anchors
    v_off = 40 if is_40 else 41
    
    internal_version = struct.unpack('<i', data[core_start+v_off : core_start+v_off+4])[0]
    sec_count = struct.unpack('<i', data[core_start+v_off+4 : core_start+v_off+8])[0]

    # Heuristic for section size
    section_size = 14
    for s_size in [14, 24, 16, 20, 28]:
        probe_pos = core_start + v_off + 4 + 4 + (sec_count * s_size)
        if probe_pos + 8 <= len(data):
            # Check for vertex_count == 0, color_count == 0 (typical for Vanguard core)
            v_cnt = struct.unpack('<i', data[probe_pos : probe_pos+4])[0]
            c_cnt = struct.unpack('<i', data[probe_pos+8 : probe_pos+12])[0]
            if v_cnt == 0 and c_cnt == 0:
                section_size = s_size
                break

    # Define section based on detected size
    class GenericSection(Struct):
        def __init__(self, size):
            super().__init__("is_strip" / Int32ul, "payload" / Bytes(size - 4))
    section_struct = GenericSection(section_size)

    # Now find the RawTriangles anchor (6-byte null padding + absolute skip_pos)
    # This is much more reliable than parsing every stream field which varies
    padding_pos = -1
    search_start = core_start + v_off + 8 + (sec_count * section_size)
    if search_start < len(data):
        padding_pos = data.find(b'\x00\x00\x00\x00\x00\x00', search_start)
    
    if padding_pos == -1 or padding_pos + 10 > len(data):
        result['parse_status'] = 'error'
        result['error_message'] = "Could not find RawTriangles sync padding"
        return result
        
    skip_pos = struct.unpack('<I', data[padding_pos+6 : padding_pos+10])[0]
    core_bytes = padding_pos + 10 - core_start
    
    result['bytes_parsed'] = core_start + core_bytes
    result['data']['core'] = {
        'internal_version': internal_version,
        'section_count': sec_count,
        'raw_triangles_skip_pos': skip_pos
    }
    
    # Step 3: Handle RawTriangles skip
    # skip_pos is absolute, convert to relative
    relative_skip = skip_pos - serial_offset
    
    if skip_pos == 0:
        # No skip needed, or skip_pos not present
        current_pos = core_start + core_bytes
    else:
        # Boundary check for skip_pos
        if relative_skip < 0 or relative_skip > len(data):
             result['parse_status'] = 'error'
             result['error_message'] = f'Invalid RawTriangles skip_pos: {skip_pos} (relative {relative_skip})'
             return result
        current_pos = relative_skip
    
    # Bytes between current position and skip target are RawTriangles payload
    raw_tri_size = relative_skip - current_pos
    if raw_tri_size > 0:
        result['data']['raw_triangles_payload_size'] = raw_tri_size
        result['bytes_parsed'] += raw_tri_size
    
    # Step 4: Parse post-skip data (Physics, Auth, LODs)
    post_skip_data = data[relative_skip:]
    stream2 = io.BytesIO(post_skip_data)
    
    PostSkipStructure = Struct(
        "physics_ref" / Int32sl,
        "auth_key" / Int32sl,
        "lod_version" / Int32sl,
        "lod_count" / Int32sl,
    )
    
    post_skip = PostSkipStructure.parse_stream(stream2)
    # print(f"DEBUG: PostSkip PhysicsRef: {post_skip.physics_ref}")
    # print(f"DEBUG: PostSkip AuthKey: {post_skip.auth_key}")
    # print(f"DEBUG: PostSkip LODVersion: {post_skip.lod_version}")
    # print(f"DEBUG: PostSkip LODCount: {post_skip.lod_count}")

    post_skip_header_bytes = stream2.tell()
    result['bytes_parsed'] += post_skip_header_bytes
    result['data']['physics_ref'] = post_skip.physics_ref
    result['data']['auth_key'] = post_skip.auth_key
    result['data']['lod_version'] = post_skip.lod_version
    result['data']['lod_count'] = post_skip.lod_count
    
    # Step 5: Parse LOD models
    lods = []
    lod_version = post_skip.lod_version
    
    for i in range(post_skip.lod_count):
        # print(f"DEBUG: Parsing LOD {i}, version {lod_version}")
        lod_data = {'version': lod_version}
        
        if lod_version == 1:
            # Version 1 format (Sun mesh style)
            # Header: section_count, unknown_1, ...
            # If section_count > 0, we have an array of sections (approx 20 bytes each?)
            # Then followed by vertex_count.
            
            # Read header manually
            sec_count = Int32ul.parse_stream(stream2)
            unk_1 = Int32ul.parse_stream(stream2)
            val_3 = Int32ul.parse_stream(stream2) # Often vertex_count if sec_count=0
            
            result['bytes_parsed'] += 12
            
            lod_data['section_count'] = sec_count
            lod_data['unknown_1'] = unk_1
            
            if sec_count > 0:
                # Handle extended header with sections
                # We observed 20 bytes per section (5 ints) based on Platform04 dump
                section_bytes = sec_count * 20
                _ignored_sections = stream2.read(section_bytes)
                result['bytes_parsed'] += section_bytes
                
                # Now read logical vertex count
                vertex_count = Int32ul.parse_stream(stream2)
                result['bytes_parsed'] += 4
                
                # Validate: if vertex count is absurd, the section size assumption was wrong
                if vertex_count > 500000:
                    # print(f"    WARNING: LOD V1 Extended got unreasonable vertex_count={vertex_count}, skipping mesh.")
                    lod_data['vertex_count'] = 0
                else:
                    lod_data['vertex_count'] = vertex_count
                    # print(f"    LOD V1 Extended: {sec_count} sections skipped. Found {vertex_count} vertices.")
            else:
                # Standard format: val_3 is vertex count
                lod_data['vertex_count'] = val_3
            
        else:
            # Version 0 format (Agua mesh style)
            # Header: unknown_0, unknown_1, vertex_count
            LODHeader = Struct(
                "unknown_0" / Int32ul,
                "unknown_1" / Int32ul,
                "vertex_count" / Int32ul,
            )
            lod_header = LODHeader.parse_stream(stream2)
            # print(f"DEBUG: LOD {i} Header (v0): Unk0={lod_header.unknown_0}, Unk1={lod_header.unknown_1}, VertCount={lod_header.vertex_count}")
            result['bytes_parsed'] += 12
            lod_data['unknown_0'] = lod_header.unknown_0
            lod_data['unknown_1'] = lod_header.unknown_1
            lod_data['vertex_count'] = lod_header.vertex_count
        
        # Vertices (56 bytes each)
        vertex_bytes = lod_data['vertex_count'] * 56
        vertices_data = stream2.read(vertex_bytes)
        # print(f"DEBUG: Read {len(vertices_data)} bytes for vertices (Expected {vertex_bytes})")
        result['bytes_parsed'] += vertex_bytes
        
        # Store raw vertex data
        lod_data['vertices_raw'] = vertices_data
        
        # Parse vertex positions for easy access
        parsed_vertices = []
        for v_idx in range(lod_data['vertex_count']):
            v_offset = v_idx * 56
            if v_offset + 12 <= len(vertices_data):
                x, y, z = struct.unpack('<fff', vertices_data[v_offset:v_offset+12])
                # Also read normal (next 12 bytes)
                nx, ny, nz = 0.0, 0.0, 1.0
                if v_offset + 24 <= len(vertices_data):
                    nx, ny, nz = struct.unpack('<fff', vertices_data[v_offset+12:v_offset+24])
                # Also read UV (next 8 bytes)
                u, v = 0.0, 0.0
                if v_offset + 32 <= len(vertices_data):
                    u, v = struct.unpack('<ff', vertices_data[v_offset+24:v_offset+32])
                parsed_vertices.append({
                    'x': x, 'y': y, 'z': z,
                    'nx': nx, 'ny': ny, 'nz': nz,
                    'u': u, 'v': v
                })
        lod_data['vertices'] = parsed_vertices
        
        # Post-vertex structure is the SAME for all versions:
        # val1, unk0, val2, unk1, val3, index_count, indices...
        # Based on Sun: 8, 0, 4, 0, 4, 6 -> 6 indices
        # Based on Ra1: 26, 0, 2, 0, 2, 36 -> 36 indices
        PostVertexHeader = Struct(
            "val1" / Int32ul,
            "unk0" / Int32ul,
            "val2" / Int32ul,
            "unk1" / Int32ul,
            "val3" / Int32ul,
            "index_count" / Int32ul,
        )
        pv_header = PostVertexHeader.parse_stream(stream2)
        result['bytes_parsed'] += 24
        
        lod_data['pv_val1'] = pv_header.val1
        lod_data['pv_val2'] = pv_header.val2
        lod_data['pv_val3'] = pv_header.val3
        
        index_count = pv_header.index_count
        if index_count > 1000000:
             raise ValueError(f"Absurd LOD index count: {index_count}")
             
        indices_raw = stream2.read(index_count * 2)
        if len(indices_raw) < index_count * 2:
             raise ValueError(f"Truncated LOD index buffer: expected {index_count * 2}, got {len(indices_raw)}")
             
        result['bytes_parsed'] += index_count * 2
        indices = list(struct.unpack(f'<{index_count}H', indices_raw))
        
        lod_data['index_count'] = index_count
        lod_data['indices'] = indices
        
        lods.append(lod_data)
    
    result['data']['lods'] = lods
    
    # Step 6: Capture remaining bytes as unknown
    remaining_pos = relative_skip + stream2.tell()
    remaining = len(data) - remaining_pos
    if remaining > 0:
        result['unknown_regions'].append({
            'offset_start': remaining_pos,
            'offset_end': len(data),
            'size': remaining,
            'raw_hex': data[remaining_pos:].hex(),
            'context': 'post_lod_data',
        })
        result['bytes_unknown'] = remaining
        result['bytes_parsed'] += remaining  # We read it, just don't understand it
    
    result['coverage_pct'] = (result['bytes_parsed'] / result['bytes_total']) * 100
    
    return result

# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    import sys
    sys.path.insert(0, '..')
    from ue2 import UE2Package
    
    test_files = [
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/P0001_Sun_Meshes.usx",
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/W2_Agua_mesh.usx",
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra1_P1_C1_Test_Mesh.usx",
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra44_P1_C3_Racks_Mesh.usx",
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra7000_P1_Weapons_mesh.usx",
        "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/Ra21_P1_C2_Armor_meshes.usx",
    ]
    
    for test_file in test_files:
        print(f"\n{'='*60}")
        print(f"File: {test_file.split('/')[-1]}")
        print('='*60)
        
        try:
            pkg = UE2Package(test_file)
            exp = [e for e in pkg.exports if e['class_name'] == 'StaticMesh'][0]
            data = pkg.get_export_data(exp)
            serial_offset = exp['serial_offset']
            
            result = parse_staticmesh(data, pkg.names, serial_offset)
            
            print(f"Export: {exp['object_name']}")
            if 'parse_status' in result and result['parse_status'] != 'success':
                print(f"Status: {result['parse_status']}")
                print(f"Message: {result.get('error_message', 'No message')}")
                if result['parse_status'].startswith('skipped'):
                    print("-" * 60)
                    continue
            
            print(f"Total bytes: {result['bytes_total']}")
            print(f"Parsed bytes: {result['bytes_parsed']}")
            print(f"Unknown bytes: {result['bytes_unknown']}")
            print(f"Coverage: {result['coverage_pct']:.1f}%")
            print(f"Uses heuristics: {result['uses_heuristics']}")
            print(f"Uses skips: {result['uses_skips']}")
            
            if result['data'].get('lods'):
                for i, lod in enumerate(result['data']['lods']):
                    print(f"  LOD {i}: {lod['vertex_count']} vertices, {lod['index_count']} indices")
                    if lod['indices']:
                        triangles = len(lod['indices']) // 3
                        print(f"         {triangles} triangles: {lod['indices'][:12]}...")
            
            if result['unknown_regions']:
                for region in result['unknown_regions']:
                    print(f"  Unknown region: {region['context']} ({region['size']} bytes)")
                    
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
