"""
Parser for Vanguard EMotion FX Actor (FXA v1.1) mesh data from .uem packages.

Parses the FXA binary format embedded in EMFXMesh exports to extract:
- Bone hierarchy (nodes with transforms and parent/child relationships)
- Materials and texture layers
- Mesh geometry (positions, normals, UVs, triangle list indices) per submesh
- Skinning data (bone weights per original vertex)

Format reference: EMFX_GUIDE.md in the project root.
"""

import struct
import math
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MANIFEST_LOOKUP_CACHE = {}


class FXANode:
    """A bone/joint in the skeleton hierarchy."""

    __slots__ = (
        "name",
        "parent_name",
        "position",
        "rotation",
        "scale",
        "parent_index",
        "child_indices",
        "scale_rot",
        "unk_bind_rot",
        "unk_bind_w",
    )

    def __init__(self):
        self.name = ""
        self.parent_name = ""
        self.position = (0.0, 0.0, 0.0)
        self.rotation = (0.0, 0.0, 0.0, 1.0)  # quaternion XYZW
        self.scale = (1.0, 1.0, 1.0)
        self.parent_index = -1  # resolved after all nodes parsed
        self.child_indices = []
        # Fields at offsets 28-55 in FXA NODE v3 — currently unused.
        # Hypothesis: these might be bind_pose_pos/bind_pose_rot like FXM has.
        self.scale_rot = (0.0, 0.0, 0.0)       # offset 28
        self.unk_bind_rot = (1.0, 1.0, 1.0)    # offset 40 (labeled "scale" but might be bind_rot xyz)
        self.unk_bind_w = 0.0                    # offset 52 (might be bind_rot w)


class FXAMaterialLayer:
    """A texture layer within a material."""

    __slots__ = (
        "name",
        "material_number",
        "map_type",
        "blend_mode",
        "amount",
        "u_offset",
        "v_offset",
        "u_tiling",
        "v_tiling",
        "rotation_radians",
    )

    def __init__(self):
        self.name = ""
        self.material_number = 0
        self.map_type = 0
        self.blend_mode = 0
        self.amount = 1.0
        self.u_offset = 0.0
        self.v_offset = 0.0
        self.u_tiling = 1.0
        self.v_tiling = 1.0
        self.rotation_radians = 0.0


class FXAMaterial:
    """A material reference with texture layers."""

    __slots__ = (
        "name",
        "layers",
        "ambient",
        "diffuse",
        "specular",
        "emissive",
        "shine",
        "shine_strength",
        "opacity",
        "ior",
        "double_sided",
        "wireframe",
        "transparency_type",
        "num_layers",
    )

    def __init__(self):
        self.name = ""
        self.layers = []  # list of FXAMaterialLayer
        self.ambient = (0.0, 0.0, 0.0)
        self.diffuse = (0.0, 0.0, 0.0)
        self.specular = (0.0, 0.0, 0.0)
        self.emissive = (0.0, 0.0, 0.0)
        self.shine = 0.0
        self.shine_strength = 0.0
        self.opacity = 0.0
        self.ior = 1.0
        self.double_sided = False
        self.wireframe = False
        self.transparency_type = "F"
        self.num_layers = 0


class FXASubmesh:
    """A submesh within the MESH chunk."""

    __slots__ = (
        "flags",
        "material_index",
        "num_indices",
        "num_vertices",
        "org_vertex_offset",
        "vertices",
        "normals",
        "uvs",
        "uv_sets",
        "org_vertex_numbers",
        "strip_indices",
        "faces",
    )

    def __init__(self):
        self.flags = 0
        self.material_index = 0
        self.num_indices = 0
        self.num_vertices = 0
        self.org_vertex_offset = 0
        self.vertices = []  # list of (x, y, z)
        self.normals = []  # list of (nx, ny, nz)
        self.uvs = []  # list of (u, v) — first UV set (shortcut)
        self.uv_sets = []  # list of lists of (u, v), one per UV set
        self.org_vertex_numbers = []  # list of uint32
        self.strip_indices = []  # raw triangle strip uint32 indices
        self.faces = []  # list of (v0, v1, v2) after strip→list conversion


class FXASkinInfluence:
    """A single bone influence on a vertex."""

    __slots__ = ("bone_index", "weight")

    def __init__(self, bone_index, weight):
        self.bone_index = bone_index
        self.weight = weight


class FXASocket:
    """An attachment socket from the UE2 property block."""

    __slots__ = (
        "attach_alias",
        "bone_name",
        "emfx_node",
        "rotation",
        "translation",
        "test_scale",
    )

    def __init__(self):
        self.attach_alias = ""
        self.bone_name = ""
        self.emfx_node = 0
        self.rotation = (0.0, 0.0, 0.0)
        self.translation = (0.0, 0.0, 0.0)
        self.test_scale = 1.0


class FXAJointLimit:
    """A joint rotation constraint record (38-byte stride in post-chunk data)."""

    __slots__ = (
        "bone_name_hash",
        "bone_identifier",
        "bone_index",
        "scale",
        "rotation_limits",
    )

    def __init__(self):
        self.bone_name_hash = 0      # uint16 at offset 0
        self.bone_identifier = 0     # uint32 at offset 2
        self.bone_index = 0          # uint16 at offset 6
        self.scale = 1.0             # float at offset 10 (always 1.0)
        self.rotation_limits = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)  # 6 floats at offset 14


class FXAPostChunkData:
    """Parsed post-chunk tail data from FXA files.

    The tail data follows the chunk stream and contains:
    - Joint limit records (38-byte stride, ~47% of files)
    - Extended bone records (76-byte stride, ~24% of files)
    - Dense per-vertex body data (~64 bytes/vertex)
    """

    __slots__ = (
        "joint_limits",
        "body_data",
        "body_offset",
        "total_tail_size",
        "tail_type",
        "gpu_tangents",
        "lod_levels",
    )

    def __init__(self):
        self.joint_limits = []       # list of FXAJointLimit
        self.body_data = b""         # raw bytes of unparsed body section
        self.body_offset = 0         # offset within tail where body starts
        self.total_tail_size = 0     # total bytes of post-chunk data
        self.tail_type = "unknown"   # "jl+body", "stride76+body", "dense", "none"
        self.gpu_tangents = []       # list of (tx, ty, tz, sign) per total vertex, glTF VEC4 format
        self.lod_levels = []         # list of FXALODLevel


class FXALODLevel:
    """A LOD level extracted from the post-chunk GPU data."""

    __slots__ = ("vertices", "normals", "uvs", "tangents", "indices")

    def __init__(self):
        self.vertices = []   # list of (x, y, z) in FXA space
        self.normals = []    # list of (nx, ny, nz)
        self.uvs = []        # list of (u, v)
        self.tangents = []   # list of (tx, ty, tz, sign) glTF VEC4 format
        self.indices = []    # list of int (triangle list)


class FXAChunkInfo:
    """Metadata about a chunk encountered during parsing."""

    __slots__ = ("chunk_id", "version", "size", "offset", "parsed")

    def __init__(self, chunk_id, version, size, offset, parsed=False):
        self.chunk_id = chunk_id
        self.version = version
        self.size = size
        self.offset = offset
        self.parsed = parsed


class FXAMeshData:
    """Complete parsed data from an EMFXMesh/FXA export."""

    __slots__ = (
        "fxa_version_hi",
        "fxa_version_lo",
        "source_app",
        "original_filename",
        "export_date",
        "nodes",
        "materials",
        "submeshes",
        "skinning",
        "num_org_vertices",
        "num_total_vertices",
        "num_total_indices",
        "num_uv_sets",
        "post_chunk_data",
        "chunk_audit",
        "sockets",
    )

    def __init__(self):
        self.fxa_version_hi = 0
        self.fxa_version_lo = 0
        self.source_app = ""
        self.original_filename = ""
        self.export_date = ""
        self.nodes = []  # list of FXANode
        self.materials = []  # list of FXAMaterial
        self.submeshes = []  # list of FXASubmesh
        self.skinning = []  # list of list of FXASkinInfluence, indexed by org vertex
        self.num_org_vertices = 0
        self.num_total_vertices = 0
        self.num_total_indices = 0
        self.num_uv_sets = 1
        self.post_chunk_data = None   # FXAPostChunkData or None
        self.chunk_audit = []         # list of FXAChunkInfo for all chunks seen
        self.sockets = []             # list of FXASocket from UE2 property block

    @property
    def all_vertices(self):
        """Flat list of all vertices across submeshes."""
        result = []
        for sm in self.submeshes:
            result.extend(sm.vertices)
        return result

    @property
    def all_normals(self):
        """Flat list of all normals across submeshes."""
        result = []
        for sm in self.submeshes:
            result.extend(sm.normals)
        return result

    @property
    def all_uvs(self):
        """Flat list of all UVs across submeshes."""
        result = []
        for sm in self.submeshes:
            result.extend(sm.uvs)
        return result

    @property
    def all_faces(self):
        """Flat list of all faces with global vertex indices."""
        result = []
        vtx_base = 0
        for sm in self.submeshes:
            for v0, v1, v2 in sm.faces:
                result.append((v0 + vtx_base, v1 + vtx_base, v2 + vtx_base))
            vtx_base += sm.num_vertices
        return result

    def get_skin_for_total_vertex(self, total_vertex_index):
        """Get bone influences for a total vertex via hybrid skinning lookup."""
        skin_count = len(self.skinning) if self.skinning else 0
        all_unique_pos = set()
        all_unique_orgs = set()
        max_org = 0
        orgs_scattered = False
        for sm in self.submeshes:
            for pos in sm.vertices:
                all_unique_pos.add(pos)
            sm_orgs = set()
            for org in sm.org_vertex_numbers:
                all_unique_orgs.add(org)
                sm_orgs.add(org)
                if org > max_org:
                    max_org = org
            if len(sm_orgs) > 1:
                span = max(sm_orgs) - min(sm_orgs) + 1
                if span > len(sm_orgs) * 2:
                    orgs_scattered = True
        orgs_in_bounds = max_org < skin_count
        num_submeshes = len(self.submeshes)

        if num_submeshes > 1:
            strategy = "per_submesh_offset"
        elif len(all_unique_pos) == skin_count and len(all_unique_orgs) < skin_count:
            strategy = "position"
        elif orgs_in_bounds and len(all_unique_orgs) >= len(all_unique_pos):
            strategy = "org"
        else:
            strategy = "org"

        idx = total_vertex_index
        for si, sm in enumerate(self.submeshes):
            if idx < sm.num_vertices:
                if strategy == "org":
                    skin_idx = sm.org_vertex_numbers[idx]
                elif strategy == "per_submesh_offset":
                    pos_to_skin_idx = {}
                    counter = sm.org_vertex_offset
                    for vi in range(sm.num_vertices):
                        pos = sm.vertices[vi]
                        if pos not in pos_to_skin_idx:
                            pos_to_skin_idx[pos] = counter
                            counter += 1
                    skin_idx = pos_to_skin_idx.get(sm.vertices[idx], 0)
                else:  # position
                    pos_to_skin_idx = {}
                    counter = 0
                    for vi in range(sm.num_vertices):
                        pos = sm.vertices[vi]
                        if pos not in pos_to_skin_idx:
                            pos_to_skin_idx[pos] = counter
                            counter += 1
                    skin_idx = pos_to_skin_idx.get(sm.vertices[idx], 0)
                if 0 <= skin_idx < len(self.skinning):
                    return self.skinning[skin_idx]
                return []
            idx -= sm.num_vertices
        return []


def _read_uint8(data, offset):
    return data[offset], offset + 1


def _read_uint16(data, offset):
    return struct.unpack_from("<H", data, offset)[0], offset + 2


def _read_uint32(data, offset):
    return struct.unpack_from("<I", data, offset)[0], offset + 4


def _read_int32(data, offset):
    return struct.unpack_from("<i", data, offset)[0], offset + 4


def _read_float(data, offset):
    return struct.unpack_from("<f", data, offset)[0], offset + 4


def _read_float3(data, offset):
    vals = struct.unpack_from("<3f", data, offset)
    return vals, offset + 12


def _read_float4(data, offset):
    vals = struct.unpack_from("<4f", data, offset)
    return vals, offset + 16


def _read_string(data, offset):
    """Read a uint32-length-prefixed string."""
    length, offset = _read_uint32(data, offset)
    if length > 10000:
        raise ValueError(f"String length {length} at offset {offset - 4} is too large")
    s = data[offset : offset + length].decode("ascii", errors="replace").rstrip("\x00")
    return s, offset + length


def _parse_info_chunk(data):
    """Parse INFO chunk (ID=16). Returns (source_app, filename, date).
    
    Layout: 8-byte header + 3 uint32-prefixed strings.
    Header bytes are unknown (first uint16 may be a version or count).
    """
    off = 8  # skip 8-byte header
    source_app, off = _read_string(data, off)
    filename, off = _read_string(data, off)
    date, off = _read_string(data, off)
    return source_app, filename, date


def _parse_node_chunk(data, version):
    """Parse NODE chunk (ID=0). Returns FXANode or None if version unsupported.
    
    Version 3 layout (68 fixed bytes + variable):
      0-11:  float3 position
      12-27: float4 quaternion (XYZW)
      28-39: float3 scaleRotation (usually 0,0,0)
      40-51: float3 scale (usually 1,1,1)
      52-55: float unknown (usually 1.0)
      56-59: uint32 unknown (usually 0)
      60-63: uint32 unknown (usually 0)
      64-67: uint32 numChildren
      68+:   uint32[numChildren] child indices
      then:  uint32-prefixed string: node name
      then:  uint32-prefixed string: parent node name (empty = root)
    
    Version 2: Different layout (skeleton stubs). Not yet decoded.
    """
    if version < 3:
        # Version 2 nodes are found in _ITEMS.uem skeleton stubs
        # They have a packed format we can't reliably parse yet
        return None

    node = FXANode()
    off = 0
    node.position, off = _read_float3(data, off)        # 0
    node.rotation, off = _read_float4(data, off)         # 12
    node.scale_rot, off = _read_float3(data, off)        # 28
    node.scale, off = _read_float3(data, off)            # 40
    _unk_float, off = _read_float(data, off)             # 52
    node.unk_bind_rot = node.scale                       # save for analysis
    node.unk_bind_w = _unk_float                         # save for analysis
    _unk1, off = _read_uint32(data, off)                 # 56
    _unk2, off = _read_uint32(data, off)                 # 60
    num_children, off = _read_uint32(data, off)          # 64

    node.child_indices = []
    for _ in range(num_children):
        child, off = _read_uint32(data, off)
        node.child_indices.append(child)

    node.name, off = _read_string(data, off)
    
    # Parent name string (may be empty/zero-length for root nodes)
    if off + 4 <= len(data):
        node.parent_name, off = _read_string(data, off)
    
    return node


def _parse_material_chunk(data):
    """Parse MATERIAL chunk (ID=6, version 5). Returns FXAMaterial.
    
    Layout: 4×RGB(48) + shine(4) + shineStr(4) + opacity(4) + ior(4) +
    doubleSided(1) + wireFrame(1) + transType(1) + numLayers(1) = 68 bytes
    + uint32-prefixed name + 4 trailing bytes.
    """
    mat = FXAMaterial()
    off = 0
    mat.ambient = struct.unpack_from("<3f", data, off); off += 12
    mat.diffuse = struct.unpack_from("<3f", data, off); off += 12
    mat.specular = struct.unpack_from("<3f", data, off); off += 12
    mat.emissive = struct.unpack_from("<3f", data, off); off += 12
    mat.shine = struct.unpack_from("<f", data, off)[0]; off += 4
    mat.shine_strength = struct.unpack_from("<f", data, off)[0]; off += 4
    mat.opacity = struct.unpack_from("<f", data, off)[0]; off += 4
    mat.ior = struct.unpack_from("<f", data, off)[0]; off += 4
    mat.double_sided = data[off] != 0; off += 1
    mat.wireframe = data[off] != 0; off += 1
    tt = data[off]; off += 1
    mat.transparency_type = chr(tt) if 0x20 <= tt < 0x7F else "U"
    mat.num_layers = data[off]; off += 1
    mat.name, _ = _read_string(data, off)
    return mat


def _parse_material_layer_chunk(data):
    """Parse MATERIAL_LAYER chunk (ID=7, version 4). Returns FXAMaterialLayer.
    
    Layout: uint16 materialNumber + uint8 mapType + uint8 blendMode +
    6×float (amount, uOff, vOff, uTil, vTil, rotation) = 28 bytes
    + uint32-prefixed name.
    """
    layer = FXAMaterialLayer()
    off = 0
    layer.material_number = struct.unpack_from("<H", data, off)[0]; off += 2
    layer.map_type = data[off]; off += 1
    layer.blend_mode = data[off]; off += 1
    layer.amount = struct.unpack_from("<f", data, off)[0]; off += 4
    layer.u_offset = struct.unpack_from("<f", data, off)[0]; off += 4
    layer.v_offset = struct.unpack_from("<f", data, off)[0]; off += 4
    layer.u_tiling = struct.unpack_from("<f", data, off)[0]; off += 4
    layer.v_tiling = struct.unpack_from("<f", data, off)[0]; off += 4
    layer.rotation_radians = struct.unpack_from("<f", data, off)[0]; off += 4
    layer.name, _ = _read_string(data, off)
    return layer


def _strip_to_triangles(indices, vertices=None, normals=None):
    """Convert triangle strip indices to triangle list, skipping degenerates.

    When *vertices* (list of (x,y,z) tuples) is provided, also detects and
    removes **bridge triangles** — strip-concatenation artifacts where the
    strip optimizer joined separate sub-strips without degenerate restart
    markers.  Two complementary heuristics are used:

    1. **Global IQR** — flags any consecutive-vertex distance exceeding
       Q3 + 4·IQR of all non-zero distances in the strip.
    2. **Local median ratio** — flags any distance exceeding 5× the median
       of its ±10 neighbours.  This catches bridges on compact meshes
       (tools, helmets) where the absolute jump is below the global
       threshold but far exceeds the local geometry scale.

    Any non-degenerate triangle that straddles a flagged jump is omitted.

    When *normals* (list of (nx,ny,nz) tuples) is also provided, each
    emitted face is checked: if its geometric normal (from the cross
    product) disagrees with the average vertex normal, the winding is
    flipped.  This corrects ~12% of faces where EMFX's strip layout
    produces wrong winding after degenerate restart sequences.
    """
    n = len(indices)
    if n < 3:
        return []

    # --- build spatial-jump mask when vertex data is available -----------
    is_jump = None
    if vertices is not None and len(vertices) > 0:
        dists = []
        for i in range(n - 1):
            va, vb = vertices[indices[i]], vertices[indices[i + 1]]
            d = math.sqrt((va[0]-vb[0])**2 + (va[1]-vb[1])**2 + (va[2]-vb[2])**2)
            dists.append(d)

        nd = len(dists)
        nonzero = sorted(d for d in dists if d > 1e-3)

        if len(nonzero) >= 10:
            q1 = nonzero[len(nonzero) // 4]
            q3 = nonzero[3 * len(nonzero) // 4]
            global_thresh = q3 + 4.0 * (q3 - q1)

            _W = 10          # local window half-size
            _FACTOR = 5.0    # local median multiplier

            is_jump = [False] * nd
            for i in range(nd):
                # Global check
                if dists[i] > global_thresh:
                    is_jump[i] = True
                    continue
                # Local median check
                lo = max(0, i - _W)
                hi = min(nd, i + _W + 1)
                nb = sorted(dists[j] for j in range(lo, hi)
                            if j != i and dists[j] > 1e-3)
                if len(nb) >= 4:
                    med = nb[len(nb) // 2]
                    if med > 0 and dists[i] > med * _FACTOR:
                        is_jump[i] = True

    # --- emit triangles -------------------------------------------------
    faces = []
    for i in range(n - 2):
        i0, i1, i2 = indices[i], indices[i + 1], indices[i + 2]
        if i0 == i1 or i1 == i2 or i0 == i2:
            continue
        if is_jump is not None and (is_jump[i] or is_jump[i + 1]):
            continue

        # Winding correction: flip face if geometric normal disagrees
        # with vertex normals.
        if normals is not None and vertices is not None:
            v0, v1, v2 = vertices[i0], vertices[i1], vertices[i2]
            e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
            e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
            cx = e1[1]*e2[2] - e1[2]*e2[1]
            cy = e1[2]*e2[0] - e1[0]*e2[2]
            cz = e1[0]*e2[1] - e1[1]*e2[0]
            fn_sq = cx*cx + cy*cy + cz*cz
            if fn_sq > 1e-20:
                n0, n1, n2 = normals[i0], normals[i1], normals[i2]
                ax = n0[0]+n1[0]+n2[0]
                ay = n0[1]+n1[1]+n2[1]
                az = n0[2]+n1[2]+n2[2]
                dot = cx*ax + cy*ay + cz*az
                if dot < 0:
                    i0, i1 = i1, i0  # Flip winding

        faces.append((i0, i1, i2))
    return faces


def _parse_mesh_chunk(data, chunk_version=0):
    """
    Parse MESH chunk (ID=3).
    Returns (num_org_verts, num_total_verts, num_total_indices, num_uv_sets, submeshes).

    Chunk version 3 (Ghidra: MeshChunkProcessor3) per-vertex layout:
      - uint32 orgVertexNumber (4 bytes) — FIRST field
      - float3 position (12 bytes)
      - float3 normal (12 bytes)
      - [UV sets read separately: float2 × num_uv_sets]
    Per-submesh descriptor is 12 bytes (no orgVertexOffset field).

    Our byte-level parser reads the same total bytes per submesh regardless
    of interpretation: the 4-byte difference between the 12-byte (v3) and
    16-byte (legacy) descriptors is balanced by reading N-1 vs N indices.
    The v3 orgVertexNumber shift is corrected post-parse: what the legacy
    reader sees as orgVtxOff is actually vertex[0]'s org, and each
    vertex[i]'s org is shifted by +1 (reading vertex[i+1]'s org).
    After index reconstruction (which relies on the un-shifted last-org),
    we rotate org values: [orgVtxOff] + org[0:-1].
    """
    off = 0
    _node_idx, off = _read_uint32(data, off)
    num_org_verts, off = _read_uint32(data, off)
    num_total_verts, off = _read_uint32(data, off)
    num_total_indices, off = _read_uint32(data, off)
    num_submeshes, off = _read_uint32(data, off)
    num_uv_sets, off = _read_uint32(data, off)
    # 4 mystery bytes (runtime pointer, skip)
    off += 4

    vertex_stride = 28 + 8 * num_uv_sets

    submeshes = []
    for _ in range(num_submeshes):
        sm = FXASubmesh()

        # Per-submesh descriptor (16 bytes)
        sm.flags, off = _read_uint32(data, off)
        sm.material_index = sm.flags & 0xFF
        sm.num_indices, off = _read_uint32(data, off)
        sm.num_vertices, off = _read_uint32(data, off)
        sm.org_vertex_offset, off = _read_uint32(data, off)

        # Initialize UV set lists
        sm.uv_sets = [[] for _ in range(num_uv_sets)]

        # Read vertices (variable stride)
        for _ in range(sm.num_vertices):
            pos = struct.unpack_from("<3f", data, off)
            nrm = struct.unpack_from("<3f", data, off + 12)
            sm.vertices.append(pos)
            sm.normals.append(nrm)

            uv_off = off + 24
            for uv_idx in range(num_uv_sets):
                uv = struct.unpack_from("<2f", data, uv_off)
                sm.uv_sets[uv_idx].append(uv)
                uv_off += 8

            org_vtx = struct.unpack_from("<I", data, uv_off)[0]
            sm.org_vertex_numbers.append(org_vtx)
            off += vertex_stride

        # First UV set is the primary one (shortcut)
        if num_uv_sets > 0:
            sm.uvs = sm.uv_sets[0]

        # Read N-1 stored indices, reconstruct full triangle list
        stored_count = sm.num_indices - 1
        sm.strip_indices = []
        for _ in range(stored_count):
            idx, off = _read_uint32(data, off)
            sm.strip_indices.append(idx)

        # Prepend last vertex's orgVertexNumber to form N indices (triangle list)
        if sm.strip_indices and sm.org_vertex_numbers:
            first_index = sm.org_vertex_numbers[-1]
            full_indices = [first_index] + sm.strip_indices
        else:
            full_indices = []

        # Convert to face triples, skipping degenerates
        sm.faces = []
        for t in range(len(full_indices) // 3):
            i0, i1, i2 = full_indices[t*3], full_indices[t*3+1], full_indices[t*3+2]
            if i0 != i1 and i1 != i2 and i0 != i2:
                sm.faces.append((i0, i1, i2))

        # V3 orgVertexNumber correction: in v3, org is the first field of
        # each vertex record, but our byte-level reader places it last.
        # This shifts every org by +1 position: our vertex[i].org is
        # actually vertex[i+1]'s org in the v3 layout. The "orgVtxOff"
        # field (absorbed into the descriptor) is vertex[0]'s true org.
        # Rotate: correct_org = [orgVtxOff] + org[0:-1]
        if chunk_version >= 3 and sm.org_vertex_numbers:
            sm.org_vertex_numbers = (
                [sm.org_vertex_offset] + sm.org_vertex_numbers[:-1]
            )

        submeshes.append(sm)

    return num_org_verts, num_total_verts, num_total_indices, num_uv_sets, submeshes


def _parse_skinning_chunk(data, num_org_verts):
    """
    Parse SKINNING chunk (ID=4).
    Returns list of (list of FXASkinInfluence) indexed by original vertex.
    """
    off = 0
    _node_idx, off = _read_uint32(data, off)

    skinning = []
    for _ in range(num_org_verts):
        num_inf, off = _read_uint8(data, off)
        influences = []
        for _ in range(num_inf):
            bone_idx, off = _read_uint16(data, off)
            _padding, off = _read_uint16(data, off)
            weight, off = _read_float(data, off)
            influences.append(FXASkinInfluence(bone_idx, weight))
        skinning.append(influences)

    return skinning


def _parse_joint_limit_record(data, offset):
    """Parse a single 38-byte joint limit record.
    
    Layout (38 bytes):
      0x00  uint16  bone_name_hash
      0x02  uint32  bone_identifier
      0x06  uint16  bone_index
      0x08  uint16  reserved (always 0)
      0x0A  float   scale (always 1.0)
      0x0E  float[6] rotation_limits
    """
    jl = FXAJointLimit()
    jl.bone_name_hash = struct.unpack_from("<H", data, offset)[0]
    jl.bone_identifier = struct.unpack_from("<I", data, offset + 2)[0]
    jl.bone_index = struct.unpack_from("<H", data, offset + 6)[0]
    jl.scale = struct.unpack_from("<f", data, offset + 10)[0]
    jl.rotation_limits = struct.unpack_from("<6f", data, offset + 14)
    return jl


def _parse_post_chunk_data(tail_data):
    """Parse the post-chunk tail section of an FXA file.
    
    The tail comes after all chunk data and contains:
    1. Optional joint limit records (38-byte stride with float 1.0 at offset 10)
    2. A body section of dense data
    
    Returns FXAPostChunkData.
    """
    pcd = FXAPostChunkData()
    pcd.total_tail_size = len(tail_data)

    if len(tail_data) < 38:
        pcd.tail_type = "none" if len(tail_data) == 0 else "tiny"
        pcd.body_data = tail_data
        return pcd

    pos = 0

    # Check for joint limit records (38-byte stride, float 1.0 at offset 10)
    has_jl = struct.unpack_from("<f", tail_data, 10)[0] == 1.0
    if has_jl:
        while pos + 38 <= len(tail_data):
            if struct.unpack_from("<f", tail_data, pos + 10)[0] != 1.0:
                break
            jl = _parse_joint_limit_record(tail_data, pos)
            pcd.joint_limits.append(jl)
            pos += 38

    pcd.body_offset = pos
    pcd.body_data = tail_data[pos:]

    # Classify tail type
    if pcd.joint_limits and len(pcd.body_data) > 100:
        pcd.tail_type = "jl+body"
    elif pcd.joint_limits:
        pcd.tail_type = "jl_only"
    elif len(pcd.body_data) > 76:
        # Check for 76-byte stride records (extended bone data)
        # These have float 1.0 at a consistent offset within each 76B record
        ones = []
        for i in range(0, min(len(tail_data), 1000), 4):
            if struct.unpack_from("<f", tail_data, i)[0] == 1.0:
                ones.append(i)
        if len(ones) >= 2 and ones[1] - ones[0] == 76:
            pcd.tail_type = "stride76+body"
        else:
            pcd.tail_type = "dense"
    else:
        pcd.tail_type = "dense"

    return pcd


def _parse_gpu_vertex_buffer(pcd, submeshes):
    """Extract tangent vectors and LOD levels from the post-chunk GPU buffer.

    The body_data layout is:
        [header]  80-600B metadata
        [uint16 indices × total_idx]  LOD0 index buffer
        [56B GPU vertices × total_verts]  LOD0 vertex buffer
        [optional additional LOD index+vertex blocks]
        [trailer]  ~328B

    Each 56-byte GPU vertex record:
        0x00  float[3]  TBN data
        0x0C  float[3]  Position (Z,X,Y axis swap from FXA)
        0x18  float[3]  Normal
        0x24  float[3]  Tangent
        0x30  float[2]  UV

    Populates pcd.gpu_tangents and pcd.lod_levels.
    """
    body = pcd.body_data
    if len(body) < 56 or not submeshes:
        return

    total_verts = sum(sm.num_vertices for sm in submeshes)
    total_idx = sum(sm.num_indices for sm in submeshes)
    if total_verts == 0:
        return

    # Locate the LOD0 vertex block by finding FXA vert[0].y in the body
    fxa_y = submeshes[0].vertices[0][1]
    target = struct.pack("<f", fxa_y)
    found_at = body.find(target)
    if found_at < 0:
        return

    # fxa_y maps to GPU position Z (byte 20 within each 56B record)
    # so vert_block_start = found_at - 20
    vert_block_start = found_at - 20
    vert_block_end = vert_block_start + total_verts * 56
    if vert_block_start < 0 or vert_block_end > len(body):
        return

    # Validate with second vertex if available
    if total_verts > 1 and len(submeshes[0].vertices) > 1:
        fxa_y2 = submeshes[0].vertices[1][1]
        off2 = vert_block_start + 56 + 20
        if off2 + 4 <= len(body):
            gpu_y2 = struct.unpack_from("<f", body, off2)[0]
            if abs(gpu_y2 - fxa_y2) > 0.001:
                return  # layout mismatch

    # Extract tangents from LOD0 GPU vertices
    # GPU axis mapping: GPU(x,y,z) = FXA(z,x,y)
    # To convert back: FXA(x,y,z) = GPU(y,z,x)
    tangents = []
    for i in range(total_verts):
        off = vert_block_start + i * 56
        # Normal at offset 0x18, Tangent at offset 0x24
        nrm_gpu = struct.unpack_from("<3f", body, off + 0x18)
        tan_gpu = struct.unpack_from("<3f", body, off + 0x24)

        # Convert tangent from GPU space (Z,X,Y) back to FXA space (Y,Z,X)
        tx, ty, tz = tan_gpu[1], tan_gpu[2], tan_gpu[0]

        # Compute bitangent sign: sign = dot(cross(n, t), b)
        # We don't have the bitangent explicitly, so use +1.0 as default
        # (standard for right-handed TBN)
        tangents.append((tx, ty, tz, 1.0))

    pcd.gpu_tangents = tangents

    # Extract LOD levels from trailer (data after LOD0 vertex block)
    trailer_start = vert_block_end
    remaining = body[trailer_start:]
    if len(remaining) <= 340:
        return  # just the standard small trailer, no LODs

    # Scan trailer for additional 56B vertex blocks
    # LOD vertex blocks contain vertices where the first float is small (TBN, |v|<=1)
    pos = 0
    while pos + 56 <= len(remaining):
        # Find start of a vertex block
        f0 = struct.unpack_from("<f", remaining, pos)[0]
        if abs(f0) > 1.01:
            pos += 2
            continue

        # Count consecutive 56B records that look like vertices
        count = 0
        scan = pos
        while scan + 56 <= len(remaining):
            f0 = struct.unpack_from("<f", remaining, scan)[0]
            if abs(f0) <= 1.01:
                count += 1
                scan += 56
            else:
                break

        if count < 10:
            pos += 2
            continue

        # Extract this LOD vertex block
        lod = FXALODLevel()
        for i in range(count):
            off = pos + i * 56
            # Position: GPU(z,x,y) -> FXA(y,z,x) = (gpu[1], gpu[2], gpu[0])
            p_gpu = struct.unpack_from("<3f", remaining, off + 0x0C)
            lod.vertices.append((p_gpu[1], p_gpu[2], p_gpu[0]))
            # Normal
            n_gpu = struct.unpack_from("<3f", remaining, off + 0x18)
            lod.normals.append((n_gpu[1], n_gpu[2], n_gpu[0]))
            # UV
            uv = struct.unpack_from("<2f", remaining, off + 0x30)
            lod.uvs.append(uv)
            # Tangent
            t_gpu = struct.unpack_from("<3f", remaining, off + 0x24)
            lod.tangents.append((t_gpu[1], t_gpu[2], t_gpu[0], 1.0))

        # Try to find indices before this vertex block
        # Look backwards from vertex block start for uint16 values < count
        idx_end = pos
        # Scan backwards to find start of index run
        idx_scan = idx_end - 2
        while idx_scan >= 0:
            v = struct.unpack_from("<H", remaining, idx_scan)[0]
            if v < count:
                idx_scan -= 2
            else:
                idx_scan += 2
                break
        if idx_scan < 0:
            idx_scan = 0

        if idx_end - idx_scan >= 6:  # at least one triangle
            for j in range((idx_end - idx_scan) // 2):
                v = struct.unpack_from("<H", remaining, idx_scan + j * 2)[0]
                lod.indices.append(v)

        pcd.lod_levels.append(lod)
        pos = scan  # skip past the vertex block


def _parse_node_v0_chunk(data):
    """Parse NODE chunk version 0 (contains 76-byte per-bone records + body).
    
    These large chunks contain packed node data without strings.
    Each 76-byte record has a float 1.0 at offset +36 (scale factor).
    
    Returns (list of raw 76-byte record dicts, remaining body bytes).
    """
    records = []
    pos = 0
    while pos + 76 <= len(data):
        # Check for the 1.0 scale marker at offset 36
        scale = struct.unpack_from("<f", data, pos + 36)[0]
        if scale != 1.0:
            break
        # Extract the 76-byte record as raw fields
        rec = {
            "raw": data[pos:pos + 76],
            "floats": struct.unpack_from("<19f", data, pos),
            "scale": scale,
        }
        records.append(rec)
        pos += 76

    body = data[pos:]
    return records, body


def _parse_node_v2_chunk(data):
    """Parse NODE chunk version 2 (324-byte skeleton stub nodes).
    
    Used in _ITEMS.uem files (117 files) and some creature files.
    Fixed 324-byte layout discovered by hex analysis:
    
      0-N:     Null-terminated ASCII bone name (up to ~64 chars)
      N+1-255: Stale serialized C++ object memory (pointers, vtable — not meaningful)
      256-267: float3 position (local bind pose)
      268-271: float padding/unknown (usually 0)
      272-275: float quat.x
      276-279: float quat.y
      280-283: float quat.z
      284-287: float quat.w (often -1.0 or 1.0)
      288-291: float padding/unknown (usually 0)
      292-295: float padding/unknown (usually 0)
      296-299: float padding/unknown (usually 0)
      300-303: float scale.x (usually 1.0)
      304-307: float scale.y (usually 1.0)
      308-311: float scale.z (usually 1.0)
      312-315: float scale.w (usually 1.0, ignored)
      316-323: padding (usually 0)
    
    No parent_name is stored — hierarchy cannot be resolved from v2 chunks alone.
    
    Returns FXANode or None.
    """
    if len(data) < 324:
        return None
    
    # Extract null-terminated name from offset 0
    null_pos = data.find(b'\x00')
    if null_pos <= 0 or null_pos > 128:
        return None
    
    try:
        name = data[:null_pos].decode('ascii')
        if not all(0x20 <= ord(c) < 0x7F for c in name):
            return None
    except (UnicodeDecodeError, ValueError):
        return None
    
    node = FXANode()
    node.name = name
    
    # Transform data at offset 256
    node.position = struct.unpack_from("<3f", data, 256)     # 256-267
    node.rotation = struct.unpack_from("<4f", data, 272)     # 272-287 (xyzw)
    node.scale = struct.unpack_from("<3f", data, 300)        # 300-311
    
    return node


# Chunk IDs — confirmed via Ghidra decompilation of VGClient.exe chunk processor
# registration (FUN_00e3f0d0) and Process() vtable methods.
CHUNK_NODE = 0               # Node/bone hierarchy (v0-v3)
CHUNK_MOTION_PART = 1        # Motion part (animation submotions, used in FXM)
CHUNK_ANIM_KEYFRAME = 2      # Animation keyframes (NrKeys, AnimType, IPType; FXM)
CHUNK_MESH = 3               # Mesh data (NumOrgVerts, NumSubMeshes, NumUVSets)
CHUNK_SKINNING = 4           # Skinning info (per-node bone weights)
# 5 is unused
CHUNK_MATERIAL = 6           # Material properties (Ambient, Diffuse, Specular, etc.)
CHUNK_MATERIAL_LAYER = 7     # Material layer (Texture path, MapType, Amount)
CHUNK_LIMITS = 8             # Node transform limits (TranslateMin/Max, RotateMin/Max)
CHUNK_PHYSICS_INFO = 9       # Physics objects (PhysicsInfoChunkProcessor1)
CHUNK_MESH_EXPRESSION = 10   # Morph targets (LOD, NumDeforms, NumTransforms, IsPhoneme)
CHUNK_EXPR_MOTION_PART = 11  # Expression motion keyframes (Hermite interp)
CHUNK_PHONEME_MOTION = 12    # Phoneme/lipsync data (NrKeys, NumPhonemes)
# Verified 2026-04-16: chunks 10/11/12 appear ONLY as size=0 markers across all
# 582 shipped .uem files (0 payload, 3+6+193 empty markers total). Vanguard does
# NOT ship baked morph targets. Character customization is instead implemented
# via extra skeleton bones (cheekGroup, noseGroup, l_breast, centerBrow, etc.)
# that act as region deformers. The sliders in customization_data.txt target
# these bones directly. See notes/2026-04-16_customization_investigation.md.
CHUNK_FX_MATERIAL = 13       # Shader material (Effect file, int/float/color/bitmap params)
CHUNK_REPOSITION_NODE = 14   # Repositioning node (position/rotation/scale mask)
CHUNK_INFO = 16              # File metadata (source app, filename, export date)


def parse_fxa(fxa_data):
    """
    Parse a complete FXA v1.1 binary blob.

    Args:
        fxa_data: bytes starting with "FXA " signature

    Returns:
        FXAMeshData with all parsed fields populated

    Raises:
        ValueError: if signature is missing or data is corrupt
    """
    if fxa_data[:4] != b"FXA ":
        raise ValueError(f"Invalid FXA signature: {fxa_data[:4]!r}")

    result = FXAMeshData()
    result.fxa_version_hi = fxa_data[4]
    result.fxa_version_lo = fxa_data[5]

    # Parse chunks
    off = 6
    current_material = None

    while off + 12 <= len(fxa_data):
        chunk_id = struct.unpack_from("<I", fxa_data, off)[0]
        chunk_size = struct.unpack_from("<I", fxa_data, off + 4)[0]
        chunk_ver = struct.unpack_from("<I", fxa_data, off + 8)[0]

        # Sanity checks
        if chunk_size > len(fxa_data) - off - 12 or chunk_ver > 100:
            break

        chunk_data = fxa_data[off + 12 : off + 12 + chunk_size]
        parsed = False

        if chunk_id == CHUNK_INFO:
            try:
                src, fname, date = _parse_info_chunk(chunk_data)
                result.source_app = src
                result.original_filename = fname
                result.export_date = date
                parsed = True
            except (struct.error, ValueError):
                pass  # INFO chunk format varies by version

        elif chunk_id == CHUNK_NODE:
            if chunk_ver == 3 and chunk_size >= 68:
                node = _parse_node_chunk(chunk_data, chunk_ver)
                if node is not None:
                    result.nodes.append(node)
                    parsed = True
            elif chunk_ver == 2 and chunk_size >= 68:
                node = _parse_node_v2_chunk(chunk_data)
                if node is not None:
                    result.nodes.append(node)
                    parsed = True
            elif chunk_ver == 0 and chunk_size > 76:
                # Large NODE v0 chunks contain packed per-bone records + body
                # We don't add these as nodes (no names), but record the data
                parsed = True  # Mark as handled even though data goes to audit
            elif chunk_size == 0:
                parsed = True  # Size-0 marker chunks are expected

        elif chunk_id == CHUNK_MATERIAL:
            try:
                mat = _parse_material_chunk(chunk_data)
                result.materials.append(mat)
                current_material = mat
                parsed = True
            except (struct.error, ValueError):
                pass

        elif chunk_id == CHUNK_MATERIAL_LAYER:
            try:
                layer = _parse_material_layer_chunk(chunk_data)
                if current_material is not None:
                    current_material.layers.append(layer)
                parsed = True
            except (struct.error, ValueError):
                pass

        elif chunk_id == CHUNK_MESH:
            if chunk_size >= 28:
                org_v, total_v, total_i, uv_sets, submeshes = _parse_mesh_chunk(chunk_data, chunk_ver)
                result.num_org_vertices = org_v
                result.num_total_vertices = total_v
                result.num_total_indices = total_i
                result.num_uv_sets = uv_sets
                result.submeshes = submeshes
                parsed = True

        elif chunk_id == CHUNK_SKINNING:
            if chunk_size >= 5 and result.num_org_vertices > 0:
                result.skinning = _parse_skinning_chunk(
                    chunk_data, result.num_org_vertices
                )
                parsed = True

        elif chunk_id == CHUNK_FX_MATERIAL and chunk_size == 0:
            # FX_MATERIAL (ID=13) with size=0 acts as end-of-chunks marker
            result.chunk_audit.append(
                FXAChunkInfo(chunk_id, chunk_ver, chunk_size, off, True)
            )
            off += 12 + chunk_size
            break

        else:
            # Size-0 marker chunks (IDs 1,2,5,9-42) are expected
            if chunk_size == 0:
                parsed = True

        result.chunk_audit.append(
            FXAChunkInfo(chunk_id, chunk_ver, chunk_size, off, parsed)
        )
        off += 12 + chunk_size

    # Parse post-chunk tail data
    tail_data = fxa_data[off:]
    if len(tail_data) > 0:
        result.post_chunk_data = _parse_post_chunk_data(tail_data)
        # Extract GPU tangent vectors and LOD levels
        if result.post_chunk_data and result.submeshes:
            _parse_gpu_vertex_buffer(result.post_chunk_data, result.submeshes)

    # Resolve parent indices from parent names
    name_to_idx = {node.name: i for i, node in enumerate(result.nodes)}
    for node in result.nodes:
        if node.parent_name:
            node.parent_index = name_to_idx.get(node.parent_name, -1)
        else:
            node.parent_index = -1

    return result


def _read_compact_index_at(data, offset):
    """Read a UE2 compact index from data at offset. Returns (value, new_offset)."""
    if offset >= len(data):
        return None, offset
    b0 = data[offset]
    offset += 1
    negative = b0 & 0x80
    value = b0 & 0x3F
    if b0 & 0x40:
        if offset >= len(data):
            return None, offset
        b1 = data[offset]; offset += 1
        value |= (b1 & 0x7F) << 6
        if b1 & 0x80:
            if offset >= len(data):
                return None, offset
            b2 = data[offset]; offset += 1
            value |= (b2 & 0x7F) << 13
            if b2 & 0x80:
                if offset >= len(data):
                    return None, offset
                b3 = data[offset]; offset += 1
                value |= (b3 & 0x7F) << 20
                if b3 & 0x80:
                    if offset >= len(data):
                        return None, offset
                    b4 = data[offset]; offset += 1
                    value |= b4 << 27
    return (-value if negative else value), offset


def parse_sockets(export_data, names):
    """Parse socket attachment points from the UE2 property block before FXA data.

    Args:
        export_data: raw bytes from UE2Package.get_export_data()
        names: name table from UE2Package.names

    Returns:
        list of FXASocket
    """
    rci = _read_compact_index_at

    # Find the "Sockets" name index in the name table
    sockets_idx = None
    for i, n in enumerate(names):
        if n == "Sockets":
            sockets_idx = i
            break
    if sockets_idx is None:
        return []

    # Encode sockets_idx as compact index bytes to search for
    # Build the expected byte pattern
    if sockets_idx < 64:
        pattern = bytes([sockets_idx])
    else:
        pattern = bytes([0x40 | (sockets_idx & 0x3F), (sockets_idx >> 6) & 0x7F])

    # Search for the pattern followed by Array type (info & 0x0F == 9)
    fxa_pos = export_data.find(b"FXA ")
    search_end = fxa_pos if fxa_pos > 0 else len(export_data)

    for i in range(search_end - len(pattern) - 1):
        if export_data[i:i + len(pattern)] == pattern:
            off = i + len(pattern)
            if off >= search_end:
                continue
            info = export_data[off]
            if (info & 0x0F) != 9:  # Not Array type
                continue

            # Found Sockets Array — parse it
            off += 1
            sztype = (info >> 4) & 0x07

            if sztype == 6:
                psz = struct.unpack('<H', export_data[off:off + 2])[0]; off += 2
            elif sztype == 7:
                psz = struct.unpack('<I', export_data[off:off + 4])[0]; off += 4
            elif sztype == 5:
                psz = export_data[off]; off += 1
            else:
                continue

            cnt, off = rci(export_data, off)
            if cnt is None or cnt <= 0 or cnt > 200:
                continue

            sockets = []
            for _ in range(cnt):
                sock = FXASocket()
                for __ in range(20):
                    ni, off2 = rci(export_data, off)
                    if ni is None or ni < 0 or ni >= len(names):
                        break
                    name = names[ni]
                    if name == "None":
                        off = off2
                        break

                    info2 = export_data[off2]; off2 += 1
                    pt = info2 & 0x0F
                    st = (info2 >> 4) & 0x07
                    af = (info2 >> 7) & 1

                    if st == 0: ps = 1
                    elif st == 1: ps = 2
                    elif st == 2: ps = 4
                    elif st == 3: ps = 12
                    elif st == 4: ps = 16
                    elif st == 5: ps = export_data[off2]; off2 += 1
                    elif st == 6: ps = struct.unpack('<H', export_data[off2:off2 + 2])[0]; off2 += 2
                    elif st == 7: ps = struct.unpack('<I', export_data[off2:off2 + 4])[0]; off2 += 4

                    sn = None
                    if pt == 10:
                        si, off2 = rci(export_data, off2)
                        if si is not None and 0 <= si < len(names):
                            sn = names[si]

                    if af and pt != 3:
                        b = export_data[off2]; off2 += 1
                        if b >= 128:
                            off2 += 1
                            if b & 0x40:
                                off2 += 2

                    if pt == 2 and ps >= 4:  # Int
                        val = struct.unpack('<i', export_data[off2:off2 + 4])[0]
                        if name == "EMFXNode":
                            sock.emfx_node = val
                    elif pt == 4 and ps >= 4:  # Float
                        val = struct.unpack('<f', export_data[off2:off2 + 4])[0]
                        if name == "TestScale":
                            sock.test_scale = round(val, 4)
                    elif pt == 6:  # Name
                        v, _ = rci(export_data, off2)
                        if v is not None and 0 <= v < len(names):
                            if name == "AttachAlias":
                                sock.attach_alias = names[v]
                            elif name == "BoneName":
                                sock.bone_name = names[v]
                    elif pt == 10 and sn == "Rotator" and ps >= 12:
                        p, y, r = struct.unpack('<iii', export_data[off2:off2 + 12])
                        sock.rotation = (
                            round(p * 360.0 / 65536.0, 2),
                            round(y * 360.0 / 65536.0, 2),
                            round(r * 360.0 / 65536.0, 2),
                        )
                    elif pt == 10 and sn == "Vector" and ps >= 12:
                        x, y, z = struct.unpack('<fff', export_data[off2:off2 + 12])
                        if name == "Translation":
                            sock.translation = (round(x, 2), round(y, 2), round(z, 2))

                    off = off2 if pt == 3 else off2 + ps

                sockets.append(sock)
            return sockets

    return []


def parse_emfxmesh_export(export_data, names=None):
    """
    Parse an EMFXMesh export's raw binary data.

    Locates the FXA signature within the export data (which may include
    UE2 property prefixes) and parses the FXA format.

    Args:
        export_data: raw bytes from UE2Package.get_export_data()
        names: optional name table from UE2Package.names for socket parsing

    Returns:
        FXAMeshData

    Raises:
        ValueError: if no FXA signature found
    """
    fxa_pos = export_data.find(b"FXA ")
    if fxa_pos < 0:
        raise ValueError("No FXA signature found in export data")

    result = parse_fxa(export_data[fxa_pos:])

    if names is not None:
        result.sockets = parse_sockets(export_data, names)

    return result


def export_obj(mesh_data, filepath, include_normals=True, include_uvs=True):
    """
    Export parsed mesh data to Wavefront OBJ format.

    Args:
        mesh_data: FXAMeshData instance
        filepath: output .obj file path
        include_normals: write vertex normals
        include_uvs: write UV coordinates
    """
    all_verts = mesh_data.all_vertices
    all_norms = mesh_data.all_normals
    all_uvs = mesh_data.all_uvs
    all_faces = mesh_data.all_faces

    with open(filepath, "w") as f:
        f.write(f"# Vanguard EMotion FX v{mesh_data.fxa_version_hi}.{mesh_data.fxa_version_lo}\n")
        if mesh_data.original_filename:
            f.write(f"# Source: {mesh_data.original_filename}\n")
        f.write(f"# {len(all_verts)} vertices, {len(all_faces)} triangles\n")
        f.write(f"# {len(mesh_data.submeshes)} submeshes, {len(mesh_data.nodes)} bones\n\n")

        for x, y, z in all_verts:
            f.write(f"v {x:.6f} {y:.6f} {z:.6f}\n")

        if include_uvs and all_uvs:
            f.write("\n")
            for u, v in all_uvs:
                f.write(f"vt {u:.6f} {v:.6f}\n")

        if include_normals and all_norms:
            f.write("\n")
            for nx, ny, nz in all_norms:
                f.write(f"vn {nx:.6f} {ny:.6f} {nz:.6f}\n")

        f.write("\n")

        # Write faces per submesh group
        vtx_base = 0
        for si, sm in enumerate(mesh_data.submeshes):
            mat_idx = sm.material_index
            mat_name = f"submesh_{si}"
            if mat_idx < len(mesh_data.materials):
                mat_name = mesh_data.materials[mat_idx].name
            f.write(f"g {mat_name}\n")

            for v0, v1, v2 in sm.faces:
                g0 = v0 + vtx_base + 1  # OBJ is 1-indexed
                g1 = v1 + vtx_base + 1
                g2 = v2 + vtx_base + 1
                if include_uvs and include_normals:
                    f.write(f"f {g0}/{g0}/{g0} {g1}/{g1}/{g1} {g2}/{g2}/{g2}\n")
                elif include_normals:
                    f.write(f"f {g0}//{g0} {g1}//{g1} {g2}//{g2}\n")
                elif include_uvs:
                    f.write(f"f {g0}/{g0} {g1}/{g1} {g2}/{g2}\n")
                else:
                    f.write(f"f {g0} {g1} {g2}\n")

            vtx_base += sm.num_vertices


def _lookup_shader_map_entry(shader_map, shader_ref):
    if not shader_map or not shader_ref:
        return None
    key = str(shader_ref).lower()
    candidates = [key]
    if "." in key:
        candidates.append(key.rsplit(".", 1)[-1])
    for candidate in candidates:
        entry = shader_map.get(candidate)
        if entry is not None:
            return entry
    return None


def _material_manifest_indexes(material_manifest):
    if not material_manifest:
        return {}, {}
    cache_key = id(material_manifest)
    cached = _MANIFEST_LOOKUP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    by_ref = {}
    by_name = {}
    for source_ref, entry in material_manifest.items():
        source_key = str(source_ref).lower()
        by_ref[source_key] = entry
        object_name = source_key.rsplit(".", 1)[-1]
        by_name.setdefault(object_name, []).append(entry)
    cached = (by_ref, by_name)
    _MANIFEST_LOOKUP_CACHE[cache_key] = cached
    return cached


def _manifest_entry_for_ref(material_manifest, shader_ref):
    if not material_manifest or not shader_ref:
        return None
    by_ref, by_name = _material_manifest_indexes(material_manifest)
    key = str(shader_ref).lower()
    entry = by_ref.get(key)
    if entry is not None:
        return entry
    object_name = key.rsplit(".", 1)[-1]
    matches = by_name.get(object_name, [])
    return matches[0] if len(matches) == 1 else None


def _manifest_texture_path(material_manifest, shader_ref, channel="base_color"):
    entry = _manifest_entry_for_ref(material_manifest, shader_ref)
    if not entry:
        return None
    return _manifest_channel_path_from_entry(entry, channel)


def _manifest_channel_path_from_entry(manifest_entry, channel="base_color"):
    if not manifest_entry:
        return None
    texture_record = manifest_entry.get(channel) or {}
    asset_path = texture_record.get("asset_path")
    if not asset_path:
        return None
    if os.path.isabs(asset_path):
        path = asset_path
    else:
        path = os.path.join(PROJECT_ROOT, asset_path)
    return path if os.path.exists(path) else None


def _manifest_entry_for_material(material_manifest, material, mat_index, skins_shaders):
    if not material_manifest or material is None:
        return None
    if (
        skins_shaders
        and mat_index < len(skins_shaders)
        and _is_generic_material_name(material.name)
    ):
        entry = _manifest_entry_for_ref(material_manifest, skins_shaders[mat_index])
        if entry is not None:
            return entry
    return _manifest_entry_for_ref(material_manifest, material.name)


def _manifest_alpha_mode(material_manifest, shader_ref):
    entry = _manifest_entry_for_ref(material_manifest, shader_ref)
    if not entry:
        return None
    alpha_mode = str(entry.get("alpha_mode") or "OPAQUE").upper()
    if alpha_mode == "BLEND":
        return "BLEND", None
    if alpha_mode == "MASK":
        base_color = entry.get("base_color") or {}
        if (
            _is_soft_alpha_eyelash_key(shader_ref)
            or _is_soft_alpha_eyelash_key(entry.get("source_ref"))
            or _is_soft_alpha_eyelash_key(base_color.get("texture_ref"))
            or _is_soft_alpha_eyelash_key(base_color.get("texture_name"))
            or _is_soft_alpha_eyelash_key(base_color.get("asset_name"))
        ):
            return "BLEND", None
        return "MASK", entry.get("alpha_cutoff", 0.01)
    return "OPAQUE", None


def _is_generic_material_name(material_name):
    import re as _re
    generic_pat = _re.compile(
        r'^(lambert\d*|blinn\d*|phong\d*|initialShadingGroup|lambert'
        r'|standardSurface\d*|defaultMaterial|SG\d*|.*SG\d*$)',
        _re.IGNORECASE,
    )
    return bool(generic_pat.match(str(material_name or "")))


def _find_clr_texture(material, texture_dir, shader_map=None, pkg_hint=None, mat_index=0,
                      skins_shaders=None, material_manifest=None):
    """Find a diffuse texture PNG for an FXAMaterial.

    Resolution order:
    1. Package-qualified material manifest by Skins/material shader ref.
    2. Material layer ending in ``_CLR`` or ``_CLRH`` → match against PNGs.
    3. Any material layer name tried directly, then with ``Color`` suffix,
       then with ``_CLR`` suffix (catches bare names like ``Cow`` → ``CowColor``).
    4. Shader-map fallback: look up *material.name* (lower-cased) in
       *shader_map* to get a texture name, then match against PNGs.
    5. Package-hint fallback: if *pkg_hint* is provided (e.g. ``UEM_djinn_M_char``),
       derive a package prefix and search shader_map for entries matching
       ``{prefix}_*_{mat_index}_shd`` or ``{prefix}_body_{mat_index}_shd`` etc.

    Returns the full path to the PNG, or *None*.
    """
    import os

    if texture_dir is None or not os.path.isdir(texture_dir):
        return None

    # Build a case-insensitive lookup of available PNGs (cached on first call)
    if not hasattr(_find_clr_texture, "_cache") or _find_clr_texture._cache[0] != texture_dir:
        lookup = {}
        for fn in os.listdir(texture_dir):
            if fn.lower().endswith(".png"):
                lookup[fn[:-4].lower()] = os.path.join(texture_dir, fn)
        _find_clr_texture._cache = (texture_dir, lookup)
    lookup = _find_clr_texture._cache[1]

    # Ghost/Stealth shaders are special-effect materials (invisible, translucent
    # shimmer in the game engine). Do NOT assign a regular diffuse texture —
    # applying body textures to ghost polygons causes visible misapplication.
    if material.name.lower().startswith("ghost"):
        return None

    # 0. UEM Skins property — deterministic per-slot mapping.
    #    Only applied to GENERIC Maya material names (lambert3SG, blinn5SG,
    #    phong4SG, initialShadingGroup, etc.).  When the FXA already stores a
    #    real game-engine shader name (e.g. human_F_char_head_0_SHD), the Tier 1
    #    direct smap lookup handles it correctly.  Forcing Skins on real names
    #    breaks composite meshes where Skins order ≠ FXA submesh order.
    _mat_name_is_generic = _is_generic_material_name(material.name)
    if _mat_name_is_generic and skins_shaders and mat_index < len(skins_shaders):
        skin_key = skins_shaders[mat_index]
        path = _manifest_texture_path(material_manifest, skin_key)
        if path:
            return path
        entry = _lookup_shader_map_entry(shader_map, skin_key)
        if entry:
            tex_name = entry.get("texture") if isinstance(entry, dict) else (
                entry if isinstance(entry, str) and not entry.startswith("color:") else None)
            if tex_name:
                path = lookup.get(tex_name.lower())
                if path:
                    return path

    manifest_path = _manifest_texture_path(material_manifest, material.name)
    if manifest_path:
        return manifest_path

    # 1. Try _CLR / _CLRH layers (primary convention)
    for layer in material.layers:
        layer_name = layer.name if hasattr(layer, "name") else str(layer)
        upper = layer_name.upper()
        if upper.endswith("_CLR") or upper.endswith("_CLRH"):
            path = lookup.get(layer_name.lower())
            if path:
                return path

    # 2. Shader-map fallback by material name (moved before layer-suffix guessing
    #    so smap has priority over loose layer→texture heuristics).
    #    Also tries a de-duplicated fallback: if the exact material name isn't
    #    found, collapse consecutive identical letters (e.g. "raptorVullture" →
    #    "raptorVulture") and retry.  This handles artist typos in UEM mat names.
    if shader_map:
        import re as _re2
        mat_key = material.name.lower()
        entry = _lookup_shader_map_entry(shader_map, mat_key)
        if entry is None:
            dedup_key = _re2.sub(r'([a-z])\1+', r'\1', mat_key)
            if dedup_key != mat_key:
                entry = _lookup_shader_map_entry(shader_map, dedup_key)
        tex_name = None
        if isinstance(entry, dict):
            tex_name = entry.get("texture")
        elif isinstance(entry, str) and not entry.startswith("color:"):
            tex_name = entry
        if tex_name:
            path = lookup.get(tex_name.lower())
            if path:
                return path

    # 3. Try any layer name with extended suffixes (bare names, Color, _CLR).
    #    Also substitute _ALL_ → _body_ for combined-atlas layer names like
    #    'Oni_M_char_ALL_0_CLR' which maps to 'Oni_M_char_body_0_CLR'.
    for layer in material.layers:
        layer_name = layer.name if hasattr(layer, "name") else str(layer)
        key = layer_name.lower()
        candidates = [key]
        if "_all_" in key:
            candidates.append(key.replace("_all_", "_body_"))
        for base_key in candidates:
            for suffix in ("", "color", "_clr", "_clrh"):
                path = lookup.get(base_key + suffix)
                if path:
                    return path

    # 4. Package-hint fallback: prefix-scan shader_map for keys starting with
    #    the package base name.  This handles generic material names like
    #    phong15SG/initialShadingGroup and slight key mismatches (e.g. the
    #    shader key uses "all" where the material name says "body").
    #
    #    Preference order for ambiguous multi-key packages:
    #      a) key ending  _{mat_index}_shd      (most common pattern)
    #      b) key ending  _shd_{mat_index}       (GiantSand pattern)
    #      c) key ending  _shd  with no trailing digit (base/mat-0 shader)
    #      d) first key alphabetically           (single-entry packages)
    if shader_map and pkg_hint:
        import re as _re
        pkg_base = pkg_hint.lower().replace("uem_", "").replace(".uem", "")
        prefix_keys = sorted(k for k in shader_map if k.startswith(pkg_base + "_"))

        def _resolve_entry(key):
            entry = shader_map[key]
            tex_name = entry.get("texture") if isinstance(entry, dict) else (
                entry if isinstance(entry, str) and not entry.startswith("color:") else None)
            if tex_name:
                return lookup.get(tex_name.lower())
            return None

        # a) _{mat_index}_shd
        for key in prefix_keys:
            if key.endswith(f"_{mat_index}_shd"):
                path = _resolve_entry(key)
                if path:
                    return path
        # b) _shd_{mat_index}
        for key in prefix_keys:
            if key.endswith(f"_shd_{mat_index}"):
                path = _resolve_entry(key)
                if path:
                    return path
        # c) ends with _shd, no trailing digit  (base / mat-0 shader)
        for key in prefix_keys:
            if _re.search(r'_shd$', key) and not _re.search(r'_\d+_shd$|_shd_\d+$', key):
                path = _resolve_entry(key)
                if path:
                    return path
        # d) Sequential fallback: assign key[mat_index] so that packages with
        #    multiple generic material names (blinn3SG, phong4SG) get different
        #    textures instead of all resolving to the same first alphabetical key.
        #    E.g. Sporebeast mat0→body_0, mat1→limbs_0 when both keys are present.
        if prefix_keys:
            target_key = prefix_keys[min(mat_index, len(prefix_keys) - 1)]
            path = _resolve_entry(target_key)
            if path:
                return path

    # 5. Texture-directory prefix scan: smap-independent fallback.
    #    Useful when smap has no entry for the main body texture but the PNG
    #    exists (e.g. TentacleLord_M_char_body_0_CLR.png with no smap entry).
    if pkg_hint:
        pkg_base = pkg_hint.lower().replace("uem_", "").replace(".uem", "")
        tex_keys = sorted(k for k in lookup if k.startswith(pkg_base + "_"))
        # Prefer keys that embed the mat_index number
        for k in tex_keys:
            if f"_{mat_index}" in k:
                return lookup[k]
        # Sequential fallback: key[mat_index] so multiple-material packages
        # spread across different textures rather than all getting key[0].
        if tex_keys:
            return lookup[tex_keys[min(mat_index, len(tex_keys) - 1)]]

    return None


def _is_soft_alpha_eyelash_key(value):
    lower = str(value or "").lower()
    compact = lower.replace("_", "").replace("-", "")
    return "eyelash" in compact


def _find_material_alpha_mode(material, png_path, shader_map=None,
                              skins_key=None, material_manifest=None):
    """Return the glTF alphaMode for a material given its resolved texture PNG.

    Only use the explicit ``alpha`` key in shader_map — never infer from PNG
    headers, because character CLR textures use their alpha channel for
    specular intensity, not cutout transparency.

    Lookup order:
    0. Skins-derived key (passed when FXA material name is generic, e.g.
       ``file15SG``).  In that case Tier 0 of _find_clr_texture resolved the
       texture via the UEM Skins property → this same smap entry carries the
       authoritative alpha flag.
    1. Direct material name match in shader_map.
    2. Reverse lookup by resolved PNG basename — finds the smap entry that
       points at this texture for known alpha-cutout keyword categories.

    Returns a tuple ``(alpha_mode, alpha_cutoff)`` where alpha_cutoff is a
    float (0.0–1.0) used only for MASK mode, or None for BLEND/OPAQUE.
    """
    def _alpha_from_entry(entry, material_key=""):
        entry_texture = ""
        if isinstance(entry, dict):
            entry_texture = entry.get("texture", "")
            alpha = entry.get("alpha", "").lower()
            if alpha == "blend":
                return ("BLEND", None)
            if alpha == "mask":
                if (
                    _is_soft_alpha_eyelash_key(material_key)
                    or _is_soft_alpha_eyelash_key(entry_texture)
                ):
                    return ("BLEND", None)
                return ("MASK", 0.01)
        return None

    # 0. Manifest-backed shader identity. This path is package-qualified and
    # should win over shader_to_texture.json whenever present.
    if skins_key:
        result = _manifest_alpha_mode(material_manifest, skins_key)
        if result:
            return result

    material_name = material.name.lower()
    result = _manifest_alpha_mode(material_manifest, material_name)
    if result:
        return result

    if shader_map:
        # 1. Skins-derived key — used when the FXA material name is generic
        #    (lambert3SG, file15SG, etc.) and only the Skins property reveals
        #    the true shader identity.
        if skins_key:
            result = _alpha_from_entry(
                _lookup_shader_map_entry(shader_map, skins_key), skins_key
            )
            if result:
                return result

        # 2. Direct material name lookup
        result = _alpha_from_entry(
            _lookup_shader_map_entry(shader_map, material_name), material_name
        )
        if result:
            return result

        # 3. Reverse lookup by resolved texture filename.
        #    Only apply MASK if the smap key that references this texture
        #    belongs to a known alpha-cutout category (hair, leaf, etc.).
        #    Body/head/skin materials share the "human_f_" prefix with hair,
        #    so a prefix guard is NOT sufficient — use explicit keyword list only.
        if png_path:
            import os
            _ALPHA_HINT_WORDS = frozenset({
                "hair", "leaf", "foliage", "vine", "feather", "fur",
                "wing", "eyelash", "grass", "tree", "plant",
                "fern", "bush", "flower", "petal", "branch", "billboard",
            })
            tex_base = os.path.splitext(os.path.basename(png_path))[0].lower()
            for smap_key, entry in shader_map.items():
                if isinstance(entry, dict):
                    if entry.get("texture", "").lower() == tex_base:
                        if any(w in smap_key for w in _ALPHA_HINT_WORDS):
                            result = _alpha_from_entry(entry, smap_key)
                            if result:
                                return result

    return ("OPAQUE", None)


def _dilate_alpha_edges(png_bytes, passes=4):
    """Dilate opaque pixel colors into transparent border pixels.

    Eliminates white/black fringing at alpha-cutout edges (e.g. Vinewalker
    leaf textures whose source DXT art has white RGB in transparent pixels).
    Applies *passes* rounds of 4-directional flood fill.
    Fast-exits if the image has no transparent pixels or no alpha channel.

    Returns (possibly modified) PNG bytes.  Falls back to original bytes if
    numpy or Pillow are unavailable, or if any exception occurs.
    """
    try:
        import numpy as np
        from PIL import Image
        import io as _io
    except ImportError:
        return png_bytes

    try:
        img = Image.open(_io.BytesIO(png_bytes))
        if img.mode not in ('RGBA', 'LA'):
            return png_bytes
        img = img.convert('RGBA')
        arr = np.array(img, dtype=np.uint8)
        alpha = arr[:, :, 3]
        opaque = alpha > 10
        if opaque.all():
            return png_bytes  # No transparent pixels — nothing to dilate.

        filled_rgb = arr[:, :, :3].astype(np.float32)
        filled_opaque = opaque.copy()

        for _ in range(passes):
            new_rgb = filled_rgb.copy()
            new_opaque = filled_opaque.copy()
            for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                # shifted[y, x] = filled[y+dy, x+dx]  (without wraparound at edges)
                shifted_rgb = np.roll(filled_rgb, -dy, axis=0)
                shifted_rgb = np.roll(shifted_rgb, -dx, axis=1)
                shifted_opaque = np.roll(filled_opaque, -dy, axis=0)
                shifted_opaque = np.roll(shifted_opaque, -dx, axis=1)
                # Zero-out edge positions that wrapped around (no valid neighbor)
                if dy > 0:
                    shifted_opaque[-dy:, :] = False
                elif dy < 0:
                    shifted_opaque[:-dy, :] = False
                if dx > 0:
                    shifted_opaque[:, -dx:] = False
                elif dx < 0:
                    shifted_opaque[:, :-dx] = False
                can_fill = ~filled_opaque & shifted_opaque
                new_rgb[can_fill] = shifted_rgb[can_fill]
                new_opaque[can_fill] = True
            filled_rgb = new_rgb
            filled_opaque = new_opaque

        result = arr.copy()
        result[~opaque, :3] = np.clip(filled_rgb[~opaque], 0, 255).astype(np.uint8)
        buf = _io.BytesIO()
        Image.fromarray(result, 'RGBA').save(buf, 'PNG')
        return buf.getvalue()
    except Exception:
        return png_bytes


def _make_alpha_only_texture(png_bytes):
    """Replace RGB with white while preserving alpha for tint-driven soft alpha art."""
    try:
        from PIL import Image
        import io as _io
    except ImportError:
        return png_bytes

    try:
        img = Image.open(_io.BytesIO(png_bytes)).convert('RGBA')
        alpha = img.getchannel('A')
        white = Image.new('L', img.size, 255)
        result = Image.merge('RGBA', (white, white, white, alpha))
        buf = _io.BytesIO()
        result.save(buf, 'PNG')
        return buf.getvalue()
    except Exception:
        return png_bytes


def _build_skinning_data(mesh_data):
    """Build per-total-vertex JOINTS and WEIGHTS arrays for glTF skinning.

    glTF requires exactly 4 joint influences per vertex, padded with zeros.
    Returns (joints_data, weights_data) as array.array objects, or (None, None)
    if no skinning data is available.

    ``joints_data`` is an array of unsigned short (4 per vertex).
    ``weights_data`` is an array of float (4 per vertex).
    """
    import array as _array

    if not mesh_data.skinning or not mesh_data.submeshes:
        return None, None

    joints = _array.array("H")   # unsigned short
    weights = _array.array("f")  # float

    # Decide skinning index strategy:
    # The engine uses org_vertex_numbers as direct indices into the skinning
    # array. However, some meshes have an off-by-one bug where UV-seam
    # duplicate vertices each consume an org slot, shifting all values by
    # one (e.g. cow: orgs start at 1 instead of 0, unique_orgs < skin_count).
    # For those meshes, position-based sequential indexing works instead.
    #
    # Detection: check if orgs within any submesh are scattered (span >> count).
    # If so, orgs are true direct indices and must be used as-is. If orgs are
    # dense/sequential and unique positions match skin count, use position-based.
    skin_count = len(mesh_data.skinning)
    all_unique_pos = set()
    all_unique_orgs = set()
    max_org = 0
    orgs_scattered = False
    for sm in mesh_data.submeshes:
        for pos in sm.vertices:
            all_unique_pos.add(pos)
        sm_orgs = set()
        for org in sm.org_vertex_numbers:
            all_unique_orgs.add(org)
            sm_orgs.add(org)
            if org > max_org:
                max_org = org
        if len(sm_orgs) > 1:
            span = max(sm_orgs) - min(sm_orgs) + 1
            if span > len(sm_orgs) * 2:
                orgs_scattered = True

    orgs_in_bounds = max_org < skin_count
    num_submeshes = len(mesh_data.submeshes)

    # Detect non-monotonic orgVertexOffsets (e.g. dragon [0,696,89]).
    # NOTE: In V3 mesh chunks (chunk_version >= 3), the orgVertexOffset
    # field does not exist — it is actually vertex[0]'s orgVertexNumber,
    # absorbed by our byte-level parser as a descriptor field. The V3
    # org-shift fix (applied in _parse_mesh_chunk) makes all org values
    # correct absolute indices, so non-monotonic detection is no longer
    # relevant and should be disabled.
    offsets = [sm.org_vertex_offset for sm in mesh_data.submeshes]
    non_monotonic_offsets = False  # V3 fix makes orgs correct; heuristics unnecessary

    if orgs_in_bounds:
        strategy = "org_direct"
    elif len(all_unique_pos) == skin_count:
        strategy = "position"
    else:
        strategy = "org"  # fallback with neighbor correction

    # For non-monotonic meshes, build a position dedup map for SM0 only.
    # SM0's org values are corrupted (some entries point into wrong bone regions),
    # but local position dedup (offset=0, sequential) gives correct wing bones.
    # SM1 and SM2 org values are correct direct indices — use org_direct for them.
    sm_pos_dedups = None  # list indexed by si, or None if not applicable
    sm_pos_min_org = None  # per-position minimum org, used in boundary zone
    if non_monotonic_offsets and orgs_in_bounds:
        sm_pos_dedups = []
        sm_pos_min_org = []
        for si, sm in enumerate(mesh_data.submeshes):
            if si == 0:
                dedup = {}
                min_org = {}
                local_idx = 0
                for vi in range(sm.num_vertices):
                    pos = sm.vertices[vi]
                    org = sm.org_vertex_numbers[vi]
                    if pos not in dedup:
                        dedup[pos] = local_idx  # offset=0, so just sequential
                        local_idx += 1
                    # Track minimum org per position so UV-seam duplicates on
                    # the boundary zone all use the same (lowest) skinning entry.
                    if pos not in min_org or org < min_org[pos]:
                        min_org[pos] = org
                sm_pos_dedups.append(dedup)
                sm_pos_min_org.append(min_org)
            else:
                # SM1/SM2: no dedup needed, but still track min-org per
                # position to fix UV-seam dups that straddle bone boundaries.
                min_org = {}
                for vi in range(sm.num_vertices):
                    pos = sm.vertices[vi]
                    org = sm.org_vertex_numbers[vi]
                    if pos not in min_org or org < min_org[pos]:
                        min_org[pos] = org
                sm_pos_dedups.append(None)  # use org_direct
                sm_pos_min_org.append(min_org)

    # Build skinning index maps based on strategy.
    if strategy == "position":
        # Global position→skinning index map built in orgVertexOffset order.
        # The skinning array is laid out in orgVertexOffset order
        # (lowest offset first), so iterating submeshes in that order ensures
        # the Nth unique position maps to skinning entry N.
        global_pos_to_skin = {}
        global_pos_idx = 0
        for sm in sorted(mesh_data.submeshes, key=lambda s: s.org_vertex_offset):
            for vi in range(sm.num_vertices):
                pos = sm.vertices[vi]
                if pos not in global_pos_to_skin:
                    global_pos_to_skin[pos] = global_pos_idx
                    global_pos_idx += 1

    # Emit joints/weights in file order (SM0, SM1, SM2...)
    for si in range(len(mesh_data.submeshes)):
        sm = mesh_data.submeshes[si]
        prev_org = -1
        prev_pos = None

        for vi in range(sm.num_vertices):
            if sm_pos_dedups is not None and sm_pos_dedups[si] is not None:
                # SM0 of non-monotonic mesh: local position dedup fixes the bulk
                # of corrupted org values. However, the last ~25 SM0 verts have
                # valid high org values (5100+) that correctly index near the end
                # of the skinning array — their dedup indices (~698-706) fall
                # inside SM1's range (offset=696) and would get tail bones.
                # Detect this by checking if dedup index >= SM1's offset, and
                # fall back to org_direct for those verts.
                sm1_offset = mesh_data.submeshes[1].org_vertex_offset if len(mesh_data.submeshes) > 1 else skin_count
                dedup_idx = sm_pos_dedups[si].get(sm.vertices[vi], sm.org_vertex_numbers[vi])
                org_idx = sm.org_vertex_numbers[vi]
                if dedup_idx >= sm1_offset:
                    # Boundary zone: use org_direct, but use the minimum org
                    # across all UV-seam duplicates at this position so that
                    # dups straddling a left/right wing boundary all agree.
                    skin_idx = sm_pos_min_org[si].get(sm.vertices[vi], org_idx)
                else:
                    skin_idx = dedup_idx
            elif strategy == "org_direct":
                # Use min-org per position when available (non-monotonic meshes)
                # so UV-seam duplicates at the same position all get the same
                # bone instead of straddling a bone-region boundary.
                if sm_pos_min_org is not None and sm_pos_min_org[si] is not None:
                    skin_idx = sm_pos_min_org[si].get(sm.vertices[vi], sm.org_vertex_numbers[vi])
                else:
                    skin_idx = sm.org_vertex_numbers[vi]
            elif strategy == "org":
                skin_idx = sm.org_vertex_numbers[vi]
                # Neighbor correction: detect UV-seam duplicate at bone region
                # boundary.  The vertex shares the same position as its
                # predecessor but its org is prev_org+1, landing in a
                # completely different bone region.  Use previous vertex's org.
                if prev_pos is not None and skin_idx == prev_org + 1:
                    cur_pos = sm.vertices[vi]
                    dx = cur_pos[0] - prev_pos[0]
                    dy = cur_pos[1] - prev_pos[1]
                    dz = cur_pos[2] - prev_pos[2]
                    dist_sq = dx*dx + dy*dy + dz*dz
                    if dist_sq < 36.0:  # within 6 units
                        # Check if bone changed dramatically
                        cur_infs = mesh_data.skinning[skin_idx] if 0 <= skin_idx < len(mesh_data.skinning) else []
                        prev_infs = mesh_data.skinning[prev_org] if 0 <= prev_org < len(mesh_data.skinning) else []
                        if cur_infs and prev_infs:
                            if cur_infs[0].bone_index != prev_infs[0].bone_index:
                                skin_idx = prev_org
                prev_org = sm.org_vertex_numbers[vi]
                prev_pos = sm.vertices[vi]
            elif strategy == "position":
                pos = sm.vertices[vi]
                skin_idx = global_pos_to_skin[pos]
            if 0 <= skin_idx < len(mesh_data.skinning):
                infs = mesh_data.skinning[skin_idx]
            else:
                infs = []

            # Pad to exactly 4 influences
            j = [0, 0, 0, 0]
            w = [0.0, 0.0, 0.0, 0.0]
            for k, inf in enumerate(infs[:4]):
                j[k] = inf.bone_index
                w[k] = inf.weight
            # Normalize weights (glTF requires sum == 1.0)
            ws = sum(w)
            if ws > 0.0:
                w = [x / ws for x in w]

            joints.extend(j)
            weights.extend(w)

    # ── Neighbor-based correction for non-monotonic meshes ──────────────
    # Some org values point into completely wrong bone regions (e.g. a tail
    # vertex referencing a wing skinning entry).  For these isolated outlier
    # vertices *none* of their mesh-neighbors share the same primary bone.
    # Fix them by copying joints/weights from the nearest neighbor that has
    # the majority bone.
    #
    # Two stages:
    #   Stage 1 – strict: vertex has ZERO neighbors with the same bone.
    #   Stage 2 – relaxed: vertex has at most 1 neighbor with the same bone
    #             AND the majority bone has ≥3 votes (catches mutual pairs
    #             of wrong verts that validate each other in stage 1).
    if non_monotonic_offsets:
        total_vi_offset = 0
        for si in range(num_submeshes):
            sm = mesh_data.submeshes[si]
            nv = sm.num_vertices

            # Build 1-ring adjacency from faces
            adj = [[] for _ in range(nv)]
            for v0, v1, v2 in sm.faces:
                adj[v0].append(v1); adj[v0].append(v2)
                adj[v1].append(v0); adj[v1].append(v2)
                adj[v2].append(v0); adj[v2].append(v1)
            # Deduplicate neighbor lists
            adj = [list(set(a)) for a in adj]

            def _primary_joint(vi_local):
                base = (total_vi_offset + vi_local) * 4
                best_j, best_w = 0, 0.0
                for k in range(4):
                    w = weights[base + k]
                    if w > best_w:
                        best_w = w
                        best_j = joints[base + k]
                return best_j

            # Stage 1: strict – zero neighbors match
            for _pass in range(5):
                corrections = 0
                for vi in range(nv):
                    if not adj[vi]:
                        continue
                    my_bone = _primary_joint(vi)
                    if any(_primary_joint(nvi) == my_bone for nvi in adj[vi]):
                        continue
                    # Outlier — find majority bone among neighbors
                    bone_counts = {}
                    for nvi in adj[vi]:
                        nb = _primary_joint(nvi)
                        bone_counts[nb] = bone_counts.get(nb, 0) + 1
                    majority = max(bone_counts, key=bone_counts.get)
                    # Copy from first neighbor with majority bone
                    for nvi in adj[vi]:
                        if _primary_joint(nvi) == majority:
                            src = (total_vi_offset + nvi) * 4
                            dst = (total_vi_offset + vi) * 4
                            for k in range(4):
                                joints[dst + k] = joints[src + k]
                                weights[dst + k] = weights[src + k]
                            corrections += 1
                            break
                if corrections == 0:
                    break

            # Stage 2: relaxed – at most 1 neighbor matches, majority ≥ 3
            for _pass in range(3):
                corrections = 0
                for vi in range(nv):
                    if len(adj[vi]) < 4:
                        continue
                    my_bone = _primary_joint(vi)
                    same = sum(1 for nvi in adj[vi] if _primary_joint(nvi) == my_bone)
                    if same != 1:
                        continue
                    bone_counts = {}
                    for nvi in adj[vi]:
                        nb = _primary_joint(nvi)
                        bone_counts[nb] = bone_counts.get(nb, 0) + 1
                    majority = max(bone_counts, key=bone_counts.get)
                    if majority != my_bone and bone_counts[majority] >= 3:
                        for nvi in adj[vi]:
                            if _primary_joint(nvi) == majority:
                                src = (total_vi_offset + nvi) * 4
                                dst = (total_vi_offset + vi) * 4
                                for k in range(4):
                                    joints[dst + k] = joints[src + k]
                                    weights[dst + k] = weights[src + k]
                                corrections += 1
                                break
                if corrections == 0:
                    break

            # Stage 3: small-cluster elimination (skip SM0 — wings are
            # already correct from the dedup path).
            # After stages 1–2, some wrong verts form small isolated
            # clusters that validate each other (e.g. 3 tail_root verts
            # in a sea of tail_3).  Detect these via connected-component
            # analysis: if a bone has a small component (≤ MAX_CLUSTER)
            # AND a larger component elsewhere, the small one is a wrong-
            # assignment artifact.  Replace it with the majority bone
            # among its boundary neighbors.
            _MAX_CLUSTER = 5
            if si > 0:
                for _s3_pass in range(3):
                    s3_corrections = 0
                    bone_map = [_primary_joint(vi) for vi in range(nv)]
                    visited = [False] * nv
                    # bone_id → list of component sizes
                    bone_comp_sizes = {}
                    all_components = []  # (bone_id, [vertex list])
                    for start in range(nv):
                        if visited[start]:
                            continue
                        bone = bone_map[start]
                        comp = []
                        queue = [start]
                        visited[start] = True
                        while queue:
                            v = queue.pop()
                            comp.append(v)
                            for n2 in adj[v]:
                                if not visited[n2] and bone_map[n2] == bone:
                                    visited[n2] = True
                                    queue.append(n2)
                        all_components.append((bone, comp))
                        bone_comp_sizes.setdefault(bone, []).append(len(comp))

                    for comp_bone, comp_verts in all_components:
                        if len(comp_verts) > _MAX_CLUSTER:
                            continue
                        # Only correct if this bone also has a LARGER
                        # component — otherwise it may be a legitimate
                        # small bone region.
                        if max(bone_comp_sizes[comp_bone]) <= _MAX_CLUSTER:
                            continue
                        # Tally boundary neighbors (adjacent verts with
                        # different bone).
                        boundary = {}
                        for v in comp_verts:
                            for n2 in adj[v]:
                                nb = bone_map[n2]
                                if nb != comp_bone:
                                    boundary[nb] = boundary.get(nb, 0) + 1
                        if not boundary:
                            continue
                        total_boundary = sum(boundary.values())
                        maj_bone = max(boundary, key=boundary.get)
                        # Size-dependent threshold: singletons need ≥35%
                        # consensus (they're usually clear outliers),
                        # larger clusters need >60% (avoids false
                        # positives in complex regions like the head).
                        if len(comp_verts) == 1:
                            if boundary[maj_bone] * 100 < total_boundary * 35:
                                continue
                        else:
                            if boundary[maj_bone] * 10 <= total_boundary * 6:
                                continue
                        # Correct: outer verts first (they touch a
                        # boundary neighbor), then inner verts.
                        for v in comp_verts:
                            for n2 in adj[v]:
                                if bone_map[n2] == maj_bone:
                                    src = (total_vi_offset + n2) * 4
                                    dst = (total_vi_offset + v) * 4
                                    for k in range(4):
                                        joints[dst + k] = joints[src + k]
                                        weights[dst + k] = weights[src + k]
                                    bone_map[v] = maj_bone
                                    s3_corrections += 1
                                    break
                        # Second pass for verts not yet corrected (interior)
                        for v in comp_verts:
                            if bone_map[v] != comp_bone:
                                continue
                            for n2 in adj[v]:
                                if bone_map[n2] == maj_bone:
                                    src = (total_vi_offset + n2) * 4
                                    dst = (total_vi_offset + v) * 4
                                    for k in range(4):
                                        joints[dst + k] = joints[src + k]
                                        weights[dst + k] = weights[src + k]
                                    bone_map[v] = maj_bone
                                    s3_corrections += 1
                                    break
                    if s3_corrections == 0:
                        break

            total_vi_offset += nv

    return joints, weights


def _compute_inverse_bind_matrices(nodes, bind_rot_overrides=None, bind_pos_overrides=None):
    """Compute inverse bind matrices for each bone node.

    Walks the bone hierarchy to build world-space transforms, then inverts
    each to produce the 4x4 inverse bind matrix that glTF requires.

    When *bind_rot_overrides* / *bind_pos_overrides* are provided (dicts of
    ``{bone_name: (x, y, z, w)}`` / ``{bone_name: (x, y, z)}``), they replace
    the FXA node values so that IBMs match what animation keyframes expect.

    Returns a list of 16-element lists (column-major 4x4 matrices).
    """
    n = len(nodes)
    # Build world-space transforms (position, rotation, scale)
    world_pos = [(0.0, 0.0, 0.0)] * n
    world_rot = [(0.0, 0.0, 0.0, 1.0)] * n
    world_scl = [(1.0, 1.0, 1.0)] * n

    def qm(q1, q2):
        x1, y1, z1, w1 = q1
        x2, y2, z2, w2 = q2
        return (
            w1*x2 + x1*w2 + y1*z2 - z1*y2,
            w1*y2 - x1*z2 + y1*w2 + z1*x2,
            w1*z2 + x1*y2 - y1*x2 + z1*w2,
            w1*w2 - x1*x2 - y1*y2 - z1*z2,
        )

    def qr(q, v):
        vq = (v[0], v[1], v[2], 0.0)
        qc = (-q[0], -q[1], -q[2], q[3])
        r = qm(qm(q, vq), qc)
        return (r[0], r[1], r[2])

    computed = [False] * n
    visiting = [False] * n

    def compute_world(i):
        if computed[i]:
            return
        if visiting[i]:
            raise ValueError(f"Cycle in skeleton hierarchy at node {nodes[i].name!r}")

        visiting[i] = True
        node = nodes[i]
        local_pos = node.position
        local_rot = node.rotation
        local_scl = node.scale
        if bind_pos_overrides and node.name in bind_pos_overrides:
            local_pos = bind_pos_overrides[node.name]
        if bind_rot_overrides and node.name in bind_rot_overrides:
            local_rot = bind_rot_overrides[node.name]

        try:
            if node.parent_index < 0:
                world_pos[i] = local_pos
                world_rot[i] = local_rot
                world_scl[i] = local_scl
            elif node.parent_index >= n:
                raise ValueError(
                    f"Invalid parent index {node.parent_index} for skeleton node {node.name!r}"
                )
            else:
                pi = node.parent_index
                compute_world(pi)
                # Apply parent scale to child's local position before rotation.
                ps = world_scl[pi]
                scaled_pos = (local_pos[0] * ps[0], local_pos[1] * ps[1], local_pos[2] * ps[2])
                rotated = qr(world_rot[pi], scaled_pos)
                world_pos[i] = (
                    world_pos[pi][0] + rotated[0],
                    world_pos[pi][1] + rotated[1],
                    world_pos[pi][2] + rotated[2],
                )
                world_rot[i] = qm(world_rot[pi], local_rot)
                world_scl[i] = (ps[0] * local_scl[0], ps[1] * local_scl[1], ps[2] * local_scl[2])
            computed[i] = True
        finally:
            visiting[i] = False

    for i in range(n):
        compute_world(i)

    # Compute inverse bind matrix for each bone
    # IBM = inverse(world_transform) = inverse(T * R * S)
    ibms = []
    for i in range(n):
        qx, qy, qz, qw = world_rot[i]
        px, py, pz = world_pos[i]
        sx, sy, sz = world_scl[i]

        # Rotation matrix from quaternion (transposed = inverse rotation)
        # glTF stores column-major
        r00 = 1.0 - 2.0*(qy*qy + qz*qz)
        r01 = 2.0*(qx*qy + qz*qw)
        r02 = 2.0*(qx*qz - qy*qw)
        r10 = 2.0*(qx*qy - qz*qw)
        r11 = 1.0 - 2.0*(qx*qx + qz*qz)
        r12 = 2.0*(qy*qz + qx*qw)
        r20 = 2.0*(qx*qz + qy*qw)
        r21 = 2.0*(qy*qz - qx*qw)
        r22 = 1.0 - 2.0*(qx*qx + qy*qy)

        # Inverse scale factors
        isx = 1.0 / sx if sx != 0.0 else 1.0
        isy = 1.0 / sy if sy != 0.0 else 1.0
        isz = 1.0 / sz if sz != 0.0 else 1.0

        # Inverse: inv(T*R*S) = inv(S) * inv(R) * inv(T)
        # inv(R) = R^T,  inv(S) = diag(1/s)
        # Combined: each row of R^T is scaled by the inverse scale
        tx = -(r00*px + r01*py + r02*pz)
        ty = -(r10*px + r11*py + r12*pz)
        tz = -(r20*px + r21*py + r22*pz)

        # Column-major 4x4: inv(S) * R^T rows, inv(S) * t
        ibms.append([
            r00*isx, r10*isy, r20*isz, 0.0,
            r01*isx, r11*isy, r21*isz, 0.0,
            r02*isx, r12*isy, r22*isz, 0.0,
            tx*isx,  ty*isy,  tz*isz,  1.0,
        ])

    return ibms


def extract_skins_shaders(uem_path, exp_name, pkg=None):
    """Parse the UEM *Skins* property to get the deterministic material→shader map.

    The Skins array in an EMFXMesh export lists the Shader/Material objects
    assigned to each material slot in sequential order.  This is more reliable
    than heuristic name-based matching when the UEM material names are generic
    (e.g. ``blinn3SG``, ``phong4SG``, ``initialShadingGroup``).

    Returns a list of shader object-names in material-slot order (first-unique
    occurrence), e.g. ``["drake_M_char_body_0_SHD", "drake_M_char_eye_0_SHD"]``,
    or an empty list if the property cannot be found or parsed.

    The *pkg* argument is an optional already-opened :class:`ue2.package.UE2Package`
    instance; if omitted the package is opened fresh from *uem_path*.

    Implementation note
    -------------------
    The Skins raw bytes are scanned for single-byte (and two-byte) UE2 compact
    indices that are *negative* (i.e. import references).  Negative compact-index
    byte pattern: ``(b & 0x80) != 0``.  For single-byte: ``val = b & 0x3F``,
    ``import_index = val - 1``.  For two-byte: ``(b & 0x40) != 0`` indicates a
    second byte; ``val = (b & 0x3F) | ((b1 & 0x7F) << 6)``.
    Only imports whose ``class_name`` is a known shader/material class are kept.
    Shader-class imports are returned in the order of first unique occurrence —
    this order matches mat-slot assignment.
    """
    try:
        import sys as _sys
        import os as _os
        _here = _os.path.dirname(_os.path.abspath(__file__))
        _root = _os.path.dirname(_os.path.dirname(_here))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from ue2.package import UE2Package
        from material_memory import import_full_path
        from ue2_property_reader import BinaryReader, read_ue2_properties
    except ImportError:
        return []

    _SHADER_CLASSES = frozenset({
        "Shader", "Material", "FinalBlend", "TexEnvMap",
        "Combiner", "TexPanner", "TexOscillator", "TexScaler",
    })

    try:
        if pkg is None:
            pkg = UE2Package(uem_path)

        # Locate the EMFXMesh export by name
        target_exp = None
        for exp in pkg.get_exports_by_class("EMFXMesh"):
            if exp["object_name"] == exp_name:
                target_exp = exp
                break
        if target_exp is None:
            return []

        data = pkg.get_export_data(target_exp)
        reader = BinaryReader(data, 0)
        props = read_ue2_properties(reader, pkg.names)

        skins_raw = props.get("Skins")
        if not skins_raw or not isinstance(skins_raw, (bytes, bytearray)):
            return []

        # Scan for compact indices that reference imports (negative = import ref).
        # UE2 compact index byte encoding:
        #   bit7 = sign (1 = negative)
        #   bit6 = continuation (1 = second byte follows)
        #   bits5-0 = value bits 0-5
        # For single-byte: bit6=0  → final; import_idx = (b & 0x3F) - 1
        # For two-byte:    bit6=1  → next byte; import_idx = ((b & 0x3F) | ((b1 & 0x7F) << 6)) - 1
        seen = []
        seen_set = set()
        i = 0
        raw = bytes(skins_raw)
        while i < len(raw):
            b = raw[i]
            if not (b & 0x80):   # bit7=0 → positive or zero, not an import ref
                i += 1
                continue
            # Negative compact index
            if b & 0x40:
                # Two-byte form
                if i + 1 < len(raw):
                    b1 = raw[i + 1]
                    val = (b & 0x3F) | ((b1 & 0x7F) << 6)
                    import_idx = val - 1
                    i += 2
                else:
                    i += 1
                    continue
            else:
                # Single-byte form
                val = b & 0x3F
                import_idx = val - 1
                i += 1

            if import_idx < 0 or import_idx >= len(pkg.imports):
                continue
            imp = pkg.imports[import_idx]
            if imp["class_name"] not in _SHADER_CLASSES:
                continue
            obj_name = imp["object_name"]
            source_ref = import_full_path(pkg.imports, import_idx) or obj_name
            if source_ref not in seen_set:
                seen.append(source_ref)
                seen_set.add(source_ref)

        return seen

    except Exception:
        return []


def export_gltf(mesh_data, filepath, texture_dir=None, shader_map=None,
                bind_rot_overrides=None, bind_pos_overrides=None, pkg_name=None,
                skins_shaders=None, material_manifest=None):
    """
    Export parsed mesh data to a standalone glTF 2.0 file with embedded buffer.

    Includes full skeletal skinning when the mesh has bone and skinning data:
    joint nodes with hierarchy, inverse bind matrices, and per-vertex
    JOINTS_0/WEIGHTS_0 attributes.

    When *bind_rot_overrides* / *bind_pos_overrides* are provided, they are
    used instead of the FXA node transforms for IBM computation so that the
    mesh IBMs match the animation bind pose.

    When *texture_dir* is provided, resolves per-submesh materials by looking
    up the ``_CLR`` layer name in the FXA material and embedding the matching
    PNG as a base64 texture.  Each submesh becomes a separate glTF primitive
    with its own material.  Transparency is set automatically from the PNG
    alpha channel or from shader_map ``alpha`` entries.

    Args:
        mesh_data: FXAMeshData instance
        filepath: output .gltf file path
        texture_dir: optional directory containing extracted texture PNGs
        shader_map: optional legacy dict mapping shader names to texture names
            (fallback)
        material_manifest: optional package-qualified material manifest.
        pkg_name: optional UEM package name (e.g. ``UEM_djinn_M_char``) used as
            last-resort fallback when material name is generic (e.g. ``phong2SG``)
    """
    import json
    import base64
    import array
    import os

    all_verts = mesh_data.all_vertices
    all_norms = mesh_data.all_normals
    all_uvs = mesh_data.all_uvs

    if not all_verts or not mesh_data.submeshes:
        return

    has_skin = bool(mesh_data.skinning) and len(mesh_data.nodes) > 1

    # --- vertex data (shared across all primitives) -------------------------
    pos_data = array.array("f")
    nrm_data = array.array("f")
    uv_data = array.array("f")

    for x, y, z in all_verts:
        pos_data.extend([x, y, z])
    for nx, ny, nz in all_norms:
        nrm_data.extend([nx, ny, nz])
    for u, v in all_uvs:
        uv_data.extend([u, v])

    pos_bytes = pos_data.tobytes()
    nrm_bytes = nrm_data.tobytes()
    uv_bytes = uv_data.tobytes()

    # --- tangent vertex data ------------------------------------------------
    tan_bytes = b""
    has_tangents = (hasattr(mesh_data, 'post_chunk_data') and
                    mesh_data.post_chunk_data is not None and
                    len(getattr(mesh_data.post_chunk_data, 'gpu_tangents', [])) == len(all_verts))
    if has_tangents:
        tan_data = array.array("f")
        for tx, ty, tz, tw in mesh_data.post_chunk_data.gpu_tangents:
            tan_data.extend([tx, ty, tz, tw])
        tan_bytes = tan_data.tobytes()

    # --- skinning vertex data -----------------------------------------------
    joints_bytes = b""
    weights_bytes = b""
    if has_skin:
        joints_arr, weights_arr = _build_skinning_data(mesh_data)
        if joints_arr is not None:
            joints_bytes = joints_arr.tobytes()
            weights_bytes = weights_arr.tobytes()
        else:
            has_skin = False

    # --- per-submesh index buffers ------------------------------------------
    # Use LOCAL (0-based) indices per submesh.  Each primitive gets its own
    # accessor slice into the shared vertex buffer via byteOffset so that
    # Three.js (and other loaders) don't need to handle large absolute indices
    # into a shared accessor.
    idx_buffers = []          # list of bytes
    for sm in mesh_data.submeshes:
        idx = array.array("I")
        for v0, v1, v2 in sm.faces:
            idx.extend([v0, v1, v2])
        idx_buffers.append(idx.tobytes())

    # --- inverse bind matrices ----------------------------------------------
    # IBMs must use FXA-native transforms because vertices are in FXA space.
    # Skeleton *nodes* use animation bind overrides so animations line up,
    # but IBMs invert the original mesh-space transforms.
    ibm_bytes = b""
    if has_skin:
        ibms = _compute_inverse_bind_matrices(mesh_data.nodes)
        ibm_arr = array.array("f")
        for mat in ibms:
            ibm_arr.extend(mat)
        ibm_bytes = ibm_arr.tobytes()

    # --- bounding box -------------------------------------------------------
    min_pos = [float("inf")] * 3
    max_pos = [float("-inf")] * 3
    for x, y, z in all_verts:
        for i, c in enumerate([x, y, z]):
            min_pos[i] = min(min_pos[i], c)
            max_pos[i] = max(max_pos[i], c)

    nv = len(all_verts)
    mesh_name = mesh_data.original_filename or "character"

    # --- resolve textures per submesh ---------------------------------------
    tex_images = []       # list of png_path or None, per submesh
    sm_manifest_entries = []
    for si, sm in enumerate(mesh_data.submeshes):
        mat_idx = sm.material_index
        if mat_idx < len(mesh_data.materials):
            manifest_entry = _manifest_entry_for_material(
                material_manifest, mesh_data.materials[mat_idx], mat_idx, skins_shaders
            )
            png_path = _find_clr_texture(
                mesh_data.materials[mat_idx], texture_dir, shader_map,
                pkg_hint=pkg_name, mat_index=mat_idx,
                skins_shaders=skins_shaders, material_manifest=material_manifest,
            )
        else:
            manifest_entry = None
            png_path = None
        sm_manifest_entries.append(manifest_entry)
        tex_images.append(png_path)

    # Pre-compute alpha modes so the PNG loading loop knows which textures to
    # dilate.  Only MASK-mode textures (alpha cutout) need dilation; OPAQUE and
    # BLEND textures don't have the white-fringe problem.
    _tex_alpha_mode = {}   # png_path -> 'OPAQUE'|'MASK'|'BLEND'
    for si, sm in enumerate(mesh_data.submeshes):
        png_path = tex_images[si]
        if png_path is None or png_path in _tex_alpha_mode:
            continue
        mat_idx = sm.material_index
        fxa_mat = mesh_data.materials[mat_idx] if mat_idx < len(mesh_data.materials) else None
        if fxa_mat:
            sk = (
                skins_shaders[mat_idx].lower()
                if (
                    skins_shaders
                    and mat_idx < len(skins_shaders)
                    and _is_generic_material_name(fxa_mat.name)
                )
                else None
            )
            mode, _ = _find_material_alpha_mode(fxa_mat, png_path, shader_map,
                                                skins_key=sk,
                                                material_manifest=material_manifest)
        else:
            mode = 'OPAQUE'
        _tex_alpha_mode[png_path] = mode

    # --- assemble binary buffer ---------------------------------------------
    # Layout: [pos | nrm | uv | tan? | joints | weights | ibm | idx0..N | tex0..N]
    buffer_parts = [pos_bytes, nrm_bytes, uv_bytes]
    if has_tangents:
        buffer_parts.append(tan_bytes)
    if has_skin:
        buffer_parts.extend([joints_bytes, weights_bytes, ibm_bytes])
    buffer_parts.extend(idx_buffers)

    # Load texture PNGs and append to buffer
    image_entries = []     # (mime, png_bytes) for each unique image
    image_cache = {}       # (png_path, role, alpha_mode) -> image_entries index
    sm_image_idx = []      # per-submesh: index into image_entries or -1

    def _image_index_for_png(png_path, role="texture", alpha_mode="OPAQUE"):
        if png_path is None:
            return -1
        cache_key = (png_path, role, alpha_mode if role == "base" else "")
        if cache_key in image_cache:
            return image_cache[cache_key]
        try:
            with open(png_path, "rb") as f:
                png_bytes = f.read()
            # Dilate alpha edges for MASK-mode (alpha-cutout) textures to
            # eliminate white/black fringing in transparent border pixels.
            if role == "base" and alpha_mode == 'MASK':
                png_bytes = _dilate_alpha_edges(png_bytes)
            elif role == "base" and _is_soft_alpha_eyelash_key(png_path):
                png_bytes = _make_alpha_only_texture(png_bytes)
            idx = len(image_entries)
            image_entries.append(("image/png", png_bytes))
            buffer_parts.append(png_bytes)
            image_cache[cache_key] = idx
            return idx
        except Exception:
            return -1

    for png_path in tex_images:
        sm_image_idx.append(
            _image_index_for_png(
                png_path,
                role="base",
                alpha_mode=_tex_alpha_mode.get(png_path, "OPAQUE"),
            )
        )

    sm_normal_image_idx = []
    sm_specular_image_idx = []
    sm_detail_image_idx = []
    for manifest_entry in sm_manifest_entries:
        sm_normal_image_idx.append(
            _image_index_for_png(
                _manifest_channel_path_from_entry(manifest_entry, "normal"),
                role="normal",
            )
        )
        sm_specular_image_idx.append(
            _image_index_for_png(
                _manifest_channel_path_from_entry(manifest_entry, "specular"),
                role="specular",
            )
        )
        sm_detail_image_idx.append(
            _image_index_for_png(
                _manifest_channel_path_from_entry(manifest_entry, "detail"),
                role="detail",
            )
        )

    # --- LOD level data -------------------------------------------------------
    has_lods = (hasattr(mesh_data, 'post_chunk_data') and
                mesh_data.post_chunk_data is not None and
                len(getattr(mesh_data.post_chunk_data, 'lod_levels', [])) > 0)
    lod_buffer_entries = []  # list of (pos_bytes, nrm_bytes, uv_bytes, tan_bytes, idx_bytes, nv, ni, min_pos, max_pos)
    if has_lods:
        total_main_verts = len(all_verts)
        for lod in mesh_data.post_chunk_data.lod_levels:
            if len(lod.vertices) < 3:
                continue
            # Only export LODs with real parsed indices (sequential fallback = garbage geometry)
            if not lod.indices:
                continue
            # Real LODs have fewer vertices than the main mesh
            if len(lod.vertices) >= total_main_verts:
                continue
            lod_indices = lod.indices
            lp = array.array("f")
            ln = array.array("f")
            lu = array.array("f")
            lt = array.array("f")
            li = array.array("I")
            lmin = [float("inf")] * 3
            lmax = [float("-inf")] * 3
            for x, y, z in lod.vertices:
                lp.extend([x, y, z])
                for k, c in enumerate([x, y, z]):
                    lmin[k] = min(lmin[k], c)
                    lmax[k] = max(lmax[k], c)
            for nx, ny, nz in lod.normals:
                ln.extend([nx, ny, nz])
            for u, v in lod.uvs:
                lu.extend([u, v])
            for tx, ty, tz, tw in lod.tangents:
                lt.extend([tx, ty, tz, tw])
            for idx_val in lod_indices:
                li.append(idx_val)
            lpb = lp.tobytes()
            lnb = ln.tobytes()
            lub = lu.tobytes()
            ltb = lt.tobytes()
            lib = li.tobytes()
            lod_buffer_entries.append((lpb, lnb, lub, ltb, lib, len(lod.vertices), len(lod_indices), lmin, lmax))
            buffer_parts.extend([lpb, lnb, lub, ltb, lib])

    buffer_bytes = b"".join(buffer_parts)
    buffer_uri = "data:application/octet-stream;base64," + base64.b64encode(
        buffer_bytes
    ).decode("ascii")

    # --- build glTF structure -----------------------------------------------
    buffer_views = []
    accessors = []
    materials = []
    textures_list = []
    images_list = []
    samplers_list = []
    primitives = []
    gltf_nodes = []
    skins = []
    extensions_used = set()

    # Shared buffer views are NOT used for vertex data because Three.js
    # GLTFLoader ignores accessor-level byteOffset when multiple accessors
    # share a buffer view.  Instead, per-primitive buffer views are created
    # below.  We still record the base offsets for computing per-primitive
    # buffer view offsets.
    base_pos_off = 0
    base_nrm_off = len(pos_bytes)
    base_uv_off = base_nrm_off + len(nrm_bytes)
    base_tan_off = base_uv_off + len(uv_bytes)  # 0 if no tangents

    # Skinning: record base offsets for per-primitive buffer views below.
    # No shared buffer views for joints/weights (Three.js byteOffset bug).
    ibm_acc = None
    skin_off = 0
    if has_skin:
        skin_off = base_uv_off + len(uv_bytes) + len(tan_bytes)

        # Inverse bind matrices (single accessor, own buffer view — safe)
        ibm_off = skin_off + len(joints_bytes) + len(weights_bytes)
        bv_ibm = len(buffer_views)
        buffer_views.append({
            "buffer": 0, "byteOffset": ibm_off,
            "byteLength": len(ibm_bytes),
        })
        ibm_acc = len(accessors)
        accessors.append({
            "bufferView": bv_ibm, "componentType": 5126,
            "count": len(mesh_data.nodes), "type": "MAT4",
        })

    # Sampler (shared, wrap repeat)
    if image_entries:
        samplers_list.append({
            "magFilter": 9729, "minFilter": 9987,
            "wrapS": 10497, "wrapT": 10497,
        })

    # Images and textures
    fixed_prefix = len(pos_bytes) + len(nrm_bytes) + len(uv_bytes) + len(tan_bytes)
    if has_skin:
        fixed_prefix += len(joints_bytes) + len(weights_bytes) + len(ibm_bytes)
    img_offset = fixed_prefix + sum(len(b) for b in idx_buffers)
    for mime, png_bytes in image_entries:
        bv_idx = len(buffer_views)
        buffer_views.append({
            "buffer": 0, "byteOffset": img_offset, "byteLength": len(png_bytes),
        })
        images_list.append({"bufferView": bv_idx, "mimeType": mime})
        textures_list.append({"source": len(images_list) - 1, "sampler": 0})
        img_offset += len(png_bytes)

    # Per-submesh: each primitive gets its OWN buffer views for vertex data
    # (pos, nrm, uv, joints, weights) so Three.js can correctly slice them.
    idx_offset = fixed_prefix
    vtx_byte_offset = 0  # cumulative vertex count
    for si, sm in enumerate(mesh_data.submeshes):
        if not sm.faces:
            continue

        sm_nv = sm.num_vertices
        ni = len(sm.faces) * 3

        # --- per-submesh buffer views + accessors ---
        sm_pos_off = base_pos_off + vtx_byte_offset * 3 * 4
        sm_nrm_off = base_nrm_off + vtx_byte_offset * 3 * 4
        sm_uv_off = base_uv_off + vtx_byte_offset * 2 * 4

        # Compute bounding box for this submesh
        sm_min = [float("inf")] * 3
        sm_max = [float("-inf")] * 3
        for x, y, z in sm.vertices:
            for i, c in enumerate([x, y, z]):
                sm_min[i] = min(sm_min[i], c)
                sm_max[i] = max(sm_max[i], c)

        bv_pos_sm = len(buffer_views)
        buffer_views.append({"buffer": 0, "byteOffset": sm_pos_off, "byteLength": sm_nv * 3 * 4, "target": 34962})
        pos_acc = len(accessors)
        accessors.append({
            "bufferView": bv_pos_sm, "componentType": 5126, "count": sm_nv,
            "type": "VEC3", "min": sm_min, "max": sm_max,
        })

        bv_nrm_sm = len(buffer_views)
        buffer_views.append({"buffer": 0, "byteOffset": sm_nrm_off, "byteLength": sm_nv * 3 * 4, "target": 34962})
        nrm_acc = len(accessors)
        accessors.append({
            "bufferView": bv_nrm_sm, "componentType": 5126, "count": sm_nv, "type": "VEC3",
        })

        bv_uv_sm = len(buffer_views)
        buffer_views.append({"buffer": 0, "byteOffset": sm_uv_off, "byteLength": sm_nv * 2 * 4, "target": 34962})
        uv_acc = len(accessors)
        accessors.append({
            "bufferView": bv_uv_sm, "componentType": 5126, "count": sm_nv, "type": "VEC2",
        })

        attrs = {"POSITION": pos_acc, "NORMAL": nrm_acc, "TEXCOORD_0": uv_acc}

        if has_tangents:
            sm_tan_off = base_tan_off + vtx_byte_offset * 4 * 4
            bv_tan_sm = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": sm_tan_off, "byteLength": sm_nv * 4 * 4, "target": 34962})
            tan_acc = len(accessors)
            accessors.append({
                "bufferView": bv_tan_sm, "componentType": 5126, "count": sm_nv, "type": "VEC4",
            })
            attrs["TANGENT"] = tan_acc

        if has_skin:
            sm_joints_off = skin_off + vtx_byte_offset * 4 * 2
            bv_joints_sm = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": sm_joints_off, "byteLength": sm_nv * 4 * 2, "target": 34962})
            joints_acc_sm = len(accessors)
            accessors.append({
                "bufferView": bv_joints_sm, "componentType": 5123, "count": sm_nv, "type": "VEC4",
            })

            sm_weights_off = skin_off + len(joints_bytes) + vtx_byte_offset * 4 * 4
            bv_weights_sm = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": sm_weights_off, "byteLength": sm_nv * 4 * 4, "target": 34962})
            weights_acc_sm = len(accessors)
            accessors.append({
                "bufferView": bv_weights_sm, "componentType": 5126, "count": sm_nv, "type": "VEC4",
            })
            attrs["JOINTS_0"] = joints_acc_sm
            attrs["WEIGHTS_0"] = weights_acc_sm

        vtx_byte_offset += sm_nv

        # --- index buffer ---
        idx_bv = len(buffer_views)
        buffer_views.append({
            "buffer": 0, "byteOffset": idx_offset,
            "byteLength": len(idx_buffers[si]), "target": 34963,
        })
        idx_offset += len(idx_buffers[si])

        idx_acc = len(accessors)
        accessors.append({
            "bufferView": idx_bv, "componentType": 5125,
            "count": ni, "type": "SCALAR",
        })

        # Material
        mat_idx_gltf = len(materials)
        mat_name = f"submesh_{si}"
        fxa_mat = mesh_data.materials[sm.material_index] if sm.material_index < len(mesh_data.materials) else None
        if fxa_mat:
            mat_name = fxa_mat.name

        pbr = {"metallicFactor": 0.0, "roughnessFactor": 0.7}
        img_idx = sm_image_idx[si] if si < len(sm_image_idx) else -1
        resolved_png = (tex_images[si] if si < len(tex_images) else None)
        if img_idx >= 0:
            pbr["baseColorTexture"] = {"index": img_idx}
            if _is_soft_alpha_eyelash_key(resolved_png):
                pbr["baseColorFactor"] = [0.04, 0.03, 0.02, 1.0]
        else:
            pbr["baseColorFactor"] = [0.8, 0.8, 0.8, 1.0]

        # Determine alphaMode from shader_map and/or PNG header
        mat_idx_for_alpha = sm.material_index
        _sk = (
            skins_shaders[mat_idx_for_alpha].lower()
            if (
                fxa_mat
                and skins_shaders
                and mat_idx_for_alpha < len(skins_shaders)
                and _is_generic_material_name(fxa_mat.name)
            )
            else None
        )
        alpha_mode, alpha_cutoff = _find_material_alpha_mode(
            fxa_mat, resolved_png, shader_map, skins_key=_sk,
            material_manifest=material_manifest
        ) if fxa_mat else ("OPAQUE", None)

        mat_entry = {
            "name": mat_name,
            "pbrMetallicRoughness": pbr,
            "doubleSided": True,
        }
        manifest_entry = (
            sm_manifest_entries[si] if si < len(sm_manifest_entries) else None
        )
        if manifest_entry is not None:
            base_color_record = manifest_entry.get("base_color") or {}
            base_color_factor = base_color_record.get("color_factor")
            if img_idx < 0 and base_color_factor:
                factor = list(base_color_factor)
                if len(factor) == 3:
                    factor.append(1.0)
                if len(factor) >= 4:
                    pbr["baseColorFactor"] = factor[:4]
            extras = {
                "vg_source_material_ref": manifest_entry.get("source_ref"),
                "vg_source_package": manifest_entry.get("source_package"),
                "vg_surface_type": manifest_entry.get("surface_type"),
                "vg_base_color_texture": base_color_record,
            }
            normal_record = manifest_entry.get("normal") or {}
            normal_img_idx = (
                sm_normal_image_idx[si] if si < len(sm_normal_image_idx) else -1
            )
            if normal_record.get("asset_path"):
                extras["vg_normal_texture"] = normal_record
            if normal_img_idx >= 0:
                normal_def = {"index": normal_img_idx}
                if normal_record.get("scale") is not None:
                    normal_def["scale"] = normal_record.get("scale")
                mat_entry["normalTexture"] = normal_def
            specular_record = manifest_entry.get("specular") or {}
            specular_img_idx = (
                sm_specular_image_idx[si] if si < len(sm_specular_image_idx) else -1
            )
            specular_def = {}
            if specular_record.get("factor") not in (None, 0, 0.0):
                specular_def["specularFactor"] = specular_record.get("factor")
            if specular_record.get("color_factor"):
                specular_def["specularColorFactor"] = specular_record.get(
                    "color_factor"
                )[:3]
            if specular_record.get("asset_path"):
                extras["vg_specular_texture_asset"] = specular_record
            if specular_img_idx >= 0:
                specular_def["specularTexture"] = {"index": specular_img_idx}
            if specular_def and (
                specular_img_idx >= 0
                or specular_record.get("factor") not in (None, 0, 0.0)
                or specular_record.get("color_factor")
            ):
                mat_entry.setdefault("extensions", {})[
                    "KHR_materials_specular"
                ] = specular_def
                extensions_used.add("KHR_materials_specular")
            detail_record = manifest_entry.get("detail") or {}
            detail_img_idx = (
                sm_detail_image_idx[si] if si < len(sm_detail_image_idx) else -1
            )
            if detail_record.get("asset_path"):
                extras["vg_detail_texture_asset"] = detail_record
                extras["vg_detail_scale"] = detail_record.get("scale")
            if detail_img_idx >= 0:
                extras["vg_detail_texture_index"] = detail_img_idx
            mat_entry["extras"] = {
                key: value for key, value in extras.items() if value is not None
            }
        if alpha_mode != "OPAQUE":
            mat_entry["alphaMode"] = alpha_mode
            if alpha_cutoff is not None:
                mat_entry["alphaCutoff"] = alpha_cutoff

        materials.append(mat_entry)

        primitives.append({
            "attributes": attrs,
            "indices": idx_acc,
            "mode": 4,
            "material": mat_idx_gltf,
        })

    # --- build node hierarchy -----------------------------------------------
    if has_skin:
        # Node 0 = skinned mesh node
        # Nodes 1..N = skeleton joints (one per FXA bone)
        num_bones = len(mesh_data.nodes)
        joint_node_base = 1  # first joint node index in gltf_nodes

        # Rebuild children from parent_index (FXA child_indices are often empty)
        children_map = {}  # bone_index -> [child_bone_indices]
        for bi, bone in enumerate(mesh_data.nodes):
            if bone.parent_index >= 0:
                children_map.setdefault(bone.parent_index, []).append(bi)

        # Mesh node (node 0)
        mesh_node = {"mesh": 0, "name": mesh_name, "skin": 0}
        gltf_nodes.append(mesh_node)

        # Joint nodes (nodes 1..N)
        root_joints = []
        for bi, bone in enumerate(mesh_data.nodes):
            node_entry = {"name": bone.name}

            # Local transform — always use FXA-native values so that the
            # rest pose matches the IBMs (also computed from FXA).
            # Animation keyframes are absolute and override these when playing.
            px, py, pz = bone.position
            if px != 0.0 or py != 0.0 or pz != 0.0:
                node_entry["translation"] = [px, py, pz]
            qx, qy, qz, qw = bone.rotation
            if qx != 0.0 or qy != 0.0 or qz != 0.0 or qw != 1.0:
                node_entry["rotation"] = [qx, qy, qz, qw]
            sx, sy, sz = bone.scale
            if sx != 1.0 or sy != 1.0 or sz != 1.0:
                node_entry["scale"] = [sx, sy, sz]

            # Children (rebuilt from parent_index)
            kids = children_map.get(bi, [])
            if kids:
                node_entry["children"] = [
                    ci + joint_node_base for ci in kids
                ]

            gltf_nodes.append(node_entry)

            if bone.parent_index < 0:
                root_joints.append(bi + joint_node_base)

        # Skin object
        all_joints = list(range(joint_node_base, joint_node_base + num_bones))
        skin_obj = {
            "joints": all_joints,
            "inverseBindMatrices": ibm_acc,
        }
        if root_joints:
            skin_obj["skeleton"] = root_joints[0]
        skins.append(skin_obj)

        # Scene root: mesh node + skeleton roots
        scene_nodes = [0] + root_joints
    else:
        # No skinning — single mesh node
        gltf_nodes.append({"mesh": 0, "name": mesh_name})
        scene_nodes = [0]

    gltf = {
        "asset": {"version": "2.0", "generator": "vanguard_emfxmesh"},
        "scene": 0,
        "scenes": [{"nodes": scene_nodes}],
        "nodes": gltf_nodes,
        "meshes": [{"name": mesh_name, "primitives": primitives}],
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"uri": buffer_uri, "byteLength": len(buffer_bytes)}],
    }

    # --- MSFT_lod extension for LOD levels ----------------------------------
    if lod_buffer_entries:
        # Compute starting offset for LOD data in the buffer
        lod_data_off = (fixed_prefix + sum(len(b) for b in idx_buffers) +
                        sum(len(png) for _, png in image_entries))
        lod_mesh_indices = []
        for lpb, lnb, lub, ltb, lib, lnv, lni, lmin, lmax in lod_buffer_entries:
            # Buffer views and accessors for this LOD
            bv_lp = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": lod_data_off, "byteLength": len(lpb), "target": 34962})
            acc_lp = len(accessors)
            accessors.append({"bufferView": bv_lp, "componentType": 5126, "count": lnv, "type": "VEC3", "min": lmin, "max": lmax})
            lod_data_off += len(lpb)

            bv_ln = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": lod_data_off, "byteLength": len(lnb), "target": 34962})
            acc_ln = len(accessors)
            accessors.append({"bufferView": bv_ln, "componentType": 5126, "count": lnv, "type": "VEC3"})
            lod_data_off += len(lnb)

            bv_lu = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": lod_data_off, "byteLength": len(lub), "target": 34962})
            acc_lu = len(accessors)
            accessors.append({"bufferView": bv_lu, "componentType": 5126, "count": lnv, "type": "VEC2"})
            lod_data_off += len(lub)

            bv_lt = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": lod_data_off, "byteLength": len(ltb), "target": 34962})
            acc_lt = len(accessors)
            accessors.append({"bufferView": bv_lt, "componentType": 5126, "count": lnv, "type": "VEC4"})
            lod_data_off += len(ltb)

            bv_li = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": lod_data_off, "byteLength": len(lib), "target": 34963})
            acc_li = len(accessors)
            accessors.append({"bufferView": bv_li, "componentType": 5125, "count": lni, "type": "SCALAR"})
            lod_data_off += len(lib)

            # LOD mesh with single primitive (no material/skinning for LODs)
            lod_prim = {
                "attributes": {"POSITION": acc_lp, "NORMAL": acc_ln, "TEXCOORD_0": acc_lu, "TANGENT": acc_lt},
                "indices": acc_li,
                "mode": 4,
            }
            lod_mesh_idx = len(gltf["meshes"])
            gltf["meshes"].append({"name": f"{mesh_name}_LOD{lod_mesh_idx}", "primitives": [lod_prim]})
            lod_mesh_indices.append(lod_mesh_idx)

        # Add LOD nodes and link via MSFT_lod on the main mesh node
        lod_node_indices = []
        for lmi in lod_mesh_indices:
            lod_node_idx = len(gltf_nodes)
            gltf_nodes.append({"mesh": lmi, "name": gltf["meshes"][lmi]["name"]})
            lod_node_indices.append(lod_node_idx)

        # MSFT_lod: main mesh node lists LOD node indices
        gltf_nodes[0]["extensions"] = {"MSFT_lod": {"ids": lod_node_indices}}
        gltf.setdefault("extensionsUsed", []).append("MSFT_lod")
        # LOD nodes must be in the scene but hidden by capable viewers
        scene_nodes.extend(lod_node_indices)

    if skins:
        gltf["skins"] = skins
    if samplers_list:
        gltf["samplers"] = samplers_list
    if textures_list:
        gltf["textures"] = textures_list
    if images_list:
        gltf["images"] = images_list
    if extensions_used:
        existing_extensions = set(gltf.get("extensionsUsed", []))
        existing_extensions.update(extensions_used)
        gltf["extensionsUsed"] = sorted(existing_extensions)

    with open(filepath, "w") as f:
        json.dump(gltf, f)
