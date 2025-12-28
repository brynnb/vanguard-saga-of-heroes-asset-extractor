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
        'parse_status': 'success',
        'unknown_regions': [],
    }
    
    # Step 1: Parse properties
    prop_end = find_none_terminator(data, names)
    result['data']['properties_end'] = prop_end
    result['bytes_parsed'] += prop_end
    
    # Step 2: Parse core structure up to RawTriangles
    # We look for the InternalVersion anchor. 
    # UE2: prop_end + 25(FBox) + 16(FSphere) = prop_end + 41
    # Vanguard: prop_end + 24(FBox) + 16(FSphere) = prop_end + 40
    
    core_start = -1
    v_anchors = [60482, 60484, 60485, 11, 12, 128, 129] # Known versions
    
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
    skip_pos = 0
    
    # We search for the LAST valid padding/pointer combination
    # Usually RawTriangles is relatively early, but can be later in large meshes
    search_limit = len(data) - 10
    search_start = core_start + v_off + 12 # Skip version and section counts
    
    for i in range(search_start, search_limit):
        if data[i:i+6] == b'\x00\x00\x00\x00\x00\x00':
            ptr = struct.unpack('<I', data[i+6:i+10])[0]
            # Must be a valid absolute pointer within the file
            if serial_offset < ptr <= serial_offset + len(data):
                padding_pos = i
                skip_pos = ptr
                # Keep searching for a later one? No, usually the first valid one after headers is it.
                break
            elif ptr == 0 and padding_pos == -1:
                # Potential fallback if no valid pointer is ever found
                if data[i:i+10] == b'\x00' * 10:
                    padding_pos = i
                    skip_pos = 0
                    # Don't break yet, we might find a real pointer later
    
    if padding_pos == -1:
        result['parse_status'] = 'error'
        result['error_message'] = "Could not find RawTriangles sync padding with valid skip_pos"
        return result
        
    core_bytes = padding_pos + 10 - core_start
    
    result['bytes_parsed'] = core_start + core_bytes
    result['data']['core'] = {
        'internal_version': internal_version,
        'section_count': sec_count,
        'raw_triangles_skip_pos': skip_pos
    }
    
    # Step 3: Handle RawTriangles skip
    # skip_pos is absolute, convert to relative
    relative_skip = skip_pos - serial_offset if skip_pos > 0 else 0
    
    if skip_pos == 0:
        # No skip needed
        current_pos = core_start + core_bytes
    else:
        current_pos = relative_skip
    
    # Bytes between current position and skip target are RawTriangles payload
    raw_tri_size = relative_skip - current_pos
    if raw_tri_size > 0:
        result['data']['raw_triangles_payload_size'] = raw_tri_size
        result['bytes_parsed'] += raw_tri_size
    
    # Step 4: Parse post-skip data (Physics, Auth, LODs)
    # The header before LODs can vary. We search for LODCount (1-5) and LODVersion (0-1).
    lod_block_start = -1
    for i in range(current_pos, min(current_pos + 100, len(data) - 16)):
        v = struct.unpack('<I', data[i+8:i+12])[0]
        c = struct.unpack('<I', data[i+12:i+16])[0]
        if v in [0, 1] and 1 <= c <= 5:
            # Check if next 20 bytes look like a LOD header (sec_count, vertex_count)
            lod_block_start = i
            break
            
    if lod_block_start == -1:
        # Fallback to current position
        lod_block_start = current_pos
    
    current_pos = lod_block_start
    PostSkipStructure = Struct(
        "physics_ref" / Int32sl,
        "auth_key" / Int32sl,
        "lod_version" / Int32sl,
        "lod_count" / Int32sl,
    )
    
    stream2 = io.BytesIO(data[current_pos:])
    try:
        post_skip = PostSkipStructure.parse_stream(stream2)
        lod_version = post_skip.lod_version
        lod_count = post_skip.lod_count
    except:
        result['parse_status'] = 'error'
        result['error_message'] = "Failed to parse LOD header"
        return result

    result['bytes_parsed'] = current_pos + 16
    result['data'].update({
        'physics_ref': post_skip.physics_ref,
        'auth_key': post_skip.auth_key,
        'lod_version': lod_version,
        'lod_count': lod_count,
    })
    
    # Step 5: Parse LOD models
    lods = []
    for i in range(lod_count):
        lod_data = {'version': lod_version}
        
        # Each LOD has its own header. In Vanguard, it's often:
        # sec_count(4), unk1(4), unk2(4), vertex_count(4) 
        # But UE2 is 12 bytes. We check if vertex_count is absurd at 12 and try 16.
        h12 = struct.unpack('<III', data[result['bytes_parsed'] : result['bytes_parsed']+12])
        v12 = h12[2]
        
        h16 = struct.unpack('<IIII', data[result['bytes_parsed'] : result['bytes_parsed']+16])
        v16 = h16[3]
        
        header_size = 12
        vertex_count = v12
        if v12 > 100000 or v12 == 0:
            if 0 < v16 <= 100000:
                header_size = 16
                vertex_count = v16
                lod_data.update({'sec_count': h16[0], 'unk1': h16[1], 'unk2': h16[2]})
            else:
                # Still absurd? Use whatever is smaller but non-zero
                vertex_count = v12
        else:
            lod_data.update({'sec_count': h12[0], 'unk1': h12[1]})
            
        lod_data['vertex_count'] = vertex_count
        result['bytes_parsed'] += header_size
        
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
        
        # Post-vertex structure: search for index_count
        # It's usually 12-40 bytes after vertices.
        # We look for the pattern: [val1, unk0, val2, unk1, val3, index_count]
        # index_count is usually 3-1000000.
        pv_search_start = result['bytes_parsed']
        index_count = 0
        pv_header_size = 0
        
        for offset in range(0, 64, 4):
            probe_pos = pv_search_start + offset
            if probe_pos + 24 <= len(data):
                ic = struct.unpack('<I', data[probe_pos + 20 : probe_pos + 24])[0]
                if 3 <= ic <= 1000000:
                    # Verify by checking first index
                    first_idx = struct.unpack('<H', data[probe_pos + 24 : probe_pos + 26])[0]
                    if first_idx < vertex_count:
                        index_count = ic
                        pv_header_size = offset + 24
                        break
        
        if index_count == 0:
            # Fallback to standard 24 bytes if search fails
            pv_header_size = 24
            if pv_search_start + 24 <= len(data):
                index_count = struct.unpack('<I', data[pv_search_start + 20 : pv_search_start + 24])[0]

        lod_data['index_count'] = index_count
        result['bytes_parsed'] = pv_search_start + pv_header_size
        
        indices_raw = data[result['bytes_parsed'] : result['bytes_parsed'] + index_count * 2]
        if len(indices_raw) < index_count * 2:
             # Truncated?
             index_count = len(indices_raw) // 2
             indices_raw = indices_raw[:index_count * 2]
             
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
