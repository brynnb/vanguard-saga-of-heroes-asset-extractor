"""
Exact port of UEViewer's SerializeVanguardMesh to Python.

Parses Vanguard StaticMesh binary data to extract:
- Vertices (position, normal)
- UVs (embedded in Vanguard vertex format or from UVStream)
- Faces (triangle indices)
- Sections (material groups)
- Bounding box

Source: UEViewer/Unreal/UnrealMesh/UnMesh2.cpp SerializeVanguardMesh
        UEViewer/Unreal/UnrealMesh/UnMesh2.h (struct definitions)
"""

import struct
import math
try:
    from .ue2_property_reader import BinaryReader, skip_ue2_properties
    from .staticmesh_topology import section_triangle_indices
except ImportError:  # Direct script imports used by the extraction CLI.
    from ue2_property_reader import BinaryReader, skip_ue2_properties
    from staticmesh_topology import section_triangle_indices


class StaticMeshParseError(ValueError):
    """Raised when required Vanguard StaticMesh geometry is malformed."""


class VanguardMeshData:
    """Parsed mesh data from a Vanguard StaticMesh export."""

    __slots__ = (
        "vertices",
        "normals",
        "uvs",
        "uv_streams",
        "tangent_us",
        "tangent_vs",
        "basis_streams",
        "faces",
        "raw_indices",
        "colors",
        "sections",
        "skins",
        "bbox_min",
        "bbox_max",
        "internal_version",
        "is_new_format",
        "bytes_consumed",
    )

    def __init__(self):
        self.vertices = []  # list of (x, y, z)
        self.normals = []  # list of (nx, ny, nz)
        self.uvs = []  # list of (u, v)
        self.uv_streams = []  # list of UV streams; each stream is a list of (u, v)
        self.tangent_us = []  # list of (x, y, z)
        self.tangent_vs = []  # list of (x, y, z)
        self.basis_streams = []  # list of basis streams; each stream is a list of (x, y, z)
        self.faces = []  # list of (v0, v1, v2) - vertex indices from IndexStream
        self.raw_indices = (
            []
        )  # flat list of uint16 from IndexStream1 (for direct first_index access)
        self.colors = []  # list of (r, g, b, a) normalized floats from ColorStream
        self.sections = []  # explicit UE2 FStaticMeshSection dictionaries
        self.skins = []  # list of lists: each skin is a list of material name strings
        self.bbox_min = (0, 0, 0)
        self.bbox_max = (0, 0, 0)
        self.internal_version = 0
        self.is_new_format = False
        self.bytes_consumed = 0


def _remaining(r):
    return len(r.data) - r.tell()


def _require_bytes(r, count, label):
    if count < 0 or _remaining(r) < count:
        raise StaticMeshParseError(
            f"{label} exceeds the serialized mesh: need={count} remaining={_remaining(r)}"
        )


def _read_count(r, label, *, element_size=None, maximum=300_000_000):
    _require_bytes(r, 4, f"{label} count")
    count = r.read_int32()
    if count < 0 or count > maximum:
        raise StaticMeshParseError(f"{label} count is invalid: {count}")
    if element_size is not None:
        _require_bytes(r, count * element_size, label)
    return count


def _validated_header_anchor(data, names):
    """Locate the mesh body without accepting an arbitrary 0xEC byte match."""

    def valid(candidate):
        if candidate < 41 or candidate + 240 > len(data):
            return False
        try:
            bbox = struct.unpack_from("<6f", data, candidate - 41)
            bounds_valid = data[candidate - 17]
            sphere = struct.unpack_from("<4f", data, candidate - 16)
            if bounds_valid not in (0, 1):
                return False
            if not all(math.isfinite(value) and abs(value) <= 1.0e7 for value in bbox):
                return False
            if not all(math.isfinite(value) and abs(value) <= 1.0e7 for value in sphere):
                return False
            if sphere[-1] < 0.0:
                return False
            internal_version = struct.unpack_from("<i", data, candidate + 236)[0]
            return 0 <= internal_version <= 100
        except (IndexError, struct.error):
            return False

    property_candidate = None
    try:
        property_reader = BinaryReader(data)
        skip_ue2_properties(property_reader, names)
        candidate = property_reader.tell() + 41
        if data[candidate : candidate + 4] == struct.pack("<I", 236) and valid(candidate):
            property_candidate = candidate
    except (IndexError, KeyError, struct.error):
        pass
    if property_candidate is not None:
        return property_candidate

    anchor = struct.pack("<I", 236)
    candidates = []
    start = 41
    while start < min(len(data), 600):
        candidate = data.find(anchor, start, 600)
        if candidate < 0:
            break
        if valid(candidate):
            candidates.append(candidate)
        start = candidate + 1
    if len(candidates) != 1:
        raise StaticMeshParseError(
            "could not uniquely locate a structurally valid StaticMesh header "
            f"(candidates={candidates})"
        )
    return candidates[0]


def _read_tarray_raw(r, element_size):
    """Read TArray: int32 count, then count * element_size bytes. Returns raw bytes."""
    count = _read_count(r, "raw array", element_size=element_size)
    data = r.data[r.pos : r.pos + count * element_size]
    r.skip(count * element_size)
    return count, data


def _skip_tarray(r, element_size):
    """Skip a TArray of fixed-size elements."""
    count = _read_count(r, "fixed array", element_size=element_size)
    r.skip(count * element_size)
    return count


def _skip_lazy_array(r):
    """Skip a TLazyArray: int32 SkipPos + TArray (int32 count + data).
    For complex element types, we use SkipPos to jump past."""
    skip_pos = r.read_int32()  # absolute file position to skip to
    return skip_pos


def _read_compact_index_array(r):
    """Read TArray<CompactIndex> (e.g. TArray<UObject*>)."""
    count = _read_count(r, "compact-index array")
    items = []
    for _ in range(count):
        items.append(r.read_compact_index())
    return items


def _import_full_path(imports, import_index):
    """Resolve an import-table entry to its package-qualified object path."""
    names = []
    seen = set()
    idx = import_index
    while imports is not None and 0 <= idx < len(imports) and idx not in seen:
        seen.add(idx)
        imp = imports[idx]
        object_name = str(imp.get("object_name") or "")
        if object_name:
            names.append(object_name)
        outer = int(imp.get("package", 0) or 0)
        if outer < 0:
            idx = -outer - 1
            continue
        break
    if not names:
        return None
    return ".".join(reversed(names))


def parse_vanguard_staticmesh(data, names, serial_offset=0, imports=None):
    """
    Parse a Vanguard StaticMesh export's serialized data.

    Args:
        data: bytes of the export's serial data
        names: list of name strings from the package name table
        serial_offset: absolute file offset where this export's data starts
                       (needed for TLazyArray skip positions)
        imports: optional list of import dicts from UE2Package (needed for
                 resolving Skins material references to names)

    Returns:
        VanguardMeshData or None if parsing fails

    Source: UEViewer SerializeVanguardMesh (UnMesh2.cpp:1801-1883)
    """
    mesh = VanguardMeshData()
    r = BinaryReader(data)

    # Prefer the exact end of the UE2 property stream. A bounded byte scan is
    # retained only as a fallback and must yield one structurally valid header.
    anchor_pos = _validated_header_anchor(data, names)

    # Read BBox from the 41 bytes before the anchor (UPrimitive data)
    prim_start = anchor_pos - 41
    prim_r = BinaryReader(data, prim_start)
    prim_bbox_min = (prim_r.read_float(), prim_r.read_float(), prim_r.read_float())
    prim_bbox_max = (prim_r.read_float(), prim_r.read_float(), prim_r.read_float())
    prim_is_valid = prim_r.read_byte()
    # Validate: IsValid should be 0 or 1, floats should be reasonable
    if prim_is_valid > 1 or any(
        not math.isfinite(f) or abs(f) > 1e7
        for f in prim_bbox_min + prim_bbox_max
    ):
        raise StaticMeshParseError("UPrimitive bounds are invalid")

    # Position reader at the anchor (start of mesh header body)
    r.seek(anchor_pos)

    # --- SerializeVanguardMesh body (line 1807+) ---

    # Skip the header block (first int32 = 236 is the header size)
    r.skip(236)

    # Ar << InternalVersion
    _require_bytes(r, 4, "internal version")
    mesh.internal_version = r.read_int32()
    mesh.is_new_format = mesh.internal_version >= 13

    # Ar << unk1CC << unk134 << f108 << unk198 << unk194 << unk19C
    # int32, int32, CompactIndex(UObject*), CompactIndex(UObject*), float, float
    r.read_int32()  # unk1CC
    r.read_int32()  # unk134
    r.read_compact_index()  # f108 (UObject*)
    r.read_compact_index()  # unk198 (UObject*)
    r.read_float()  # unk194
    r.read_float()  # unk19C

    # if (InternalVersion > 11) Ar << unk1A0
    if mesh.internal_version > 11:
        r.read_float()  # unk1A0

    # Ar << unk1A4 (byte)
    r.read_byte()  # unk1A4

    # Ar << unk1DC (UObject*)
    r.read_compact_index()  # unk1DC

    # Ar << BoundingBox (FBox: 6 floats + 1 byte = 25 bytes)
    bmin = (r.read_float(), r.read_float(), r.read_float())
    bmax = (r.read_float(), r.read_float(), r.read_float())
    r.read_byte()  # IsValid
    mesh.bbox_min = bmin
    mesh.bbox_max = bmax

    # --- Ar << Sections (TArray<FStaticMeshSection>) ---
    # FStaticMeshSection: int32 + 5*uint16 = 14 bytes
    sec_count = _read_count(r, "sections", element_size=14, maximum=65_535)
    for i in range(sec_count):
        is_strip = r.read_int32()
        first_index = r.read_uint16()
        first_vertex = r.read_uint16()
        last_vertex = r.read_uint16()
        num_triangles = r.read_uint16()
        num_primitives = r.read_uint16()
        mesh.sections.append(
            {
                "is_strip": bool(is_strip),
                "first_index": first_index,
                "first_vertex": first_vertex,
                "last_vertex": last_vertex,
                "num_triangles": num_triangles,
                "num_primitives": num_primitives,
            }
        )

    # --- Ar << Skins (TArray<FVanguardSkin>) ---
    # FVanguardSkin: TArray<UMaterial*> + FName
    skin_count = _read_count(r, "skins", maximum=65_535)
    for _ in range(skin_count):
        tex_count = _read_count(r, "skin materials", maximum=65_535)
        mat_names = []
        for _ in range(tex_count):
            ref_idx = r.read_compact_index()  # UMaterial*
            mat_name = None
            if imports is not None and ref_idx < 0:
                imp_idx = -ref_idx - 1
                if 0 <= imp_idx < len(imports):
                    mat_name = _import_full_path(imports, imp_idx) or imports[
                        imp_idx
                    ].get("object_name")
            mat_names.append(mat_name)
        r.read_compact_index()  # FName
        mesh.skins.append(mat_names)

    # --- Ar << Faces (TLazyArray<FStaticMeshTriangle>) ---
    # TLazyArray: int32 SkipPos, then TArray serialization
    # FStaticMeshTriangle is variable-size, so we use SkipPos to skip
    face_skip_pos = r.read_int32()  # absolute file offset past the array
    # Compute relative skip target
    face_skip_relative = face_skip_pos - serial_offset
    if face_skip_relative > r.tell() and face_skip_relative <= len(data):
        r.seek(face_skip_relative)
    else:
        # Fallback: try to read the count and skip manually
        # This shouldn't normally happen
        face_count = r.read_int32()
        # FStaticMeshTriangle is variable size, can't skip without parsing
        # Give up on this mesh
        return None

    # --- Ar << UVStream (TArray<FStaticMeshUVStream>) ---
    # Each FStaticMeshUVStream: TArray<FMeshUVFloat> + int32 f10 + int32 f1C
    # FMeshUVFloat: 2 floats = 8 bytes
    uv_stream_count = _read_count(r, "UV streams", maximum=64)
    uv_streams = []
    for _ in range(uv_stream_count):
        uv_count = _read_count(r, "UV coordinates", element_size=8)
        uv_data = []
        for j in range(uv_count):
            u = r.read_float()
            v = r.read_float()
            uv_data.append((u, v))
        r.read_int32()  # f10
        r.read_int32()  # f1C
        uv_streams.append(uv_data)
    mesh.uv_streams = uv_streams

    # --- Ar << BasisStream (TArray<FVanguardUTangentStream>) ---
    # FVanguardUTangentStream: TArray<FVector> + int32
    basis_count = _read_count(r, "basis streams", maximum=64)
    for _ in range(basis_count):
        vec_count = _read_count(r, "basis vectors", element_size=12)
        basis_data = []
        for _j in range(vec_count):
            basis_data.append((r.read_float(), r.read_float(), r.read_float()))
        mesh.basis_streams.append(basis_data)
        r.read_int32()

    # --- Ar << unk144 (TArray<int>) << unk150 (TArray<int>) << unk200 (TArray<byte>) ---
    _skip_tarray(r, 4)  # unk144
    _skip_tarray(r, 4)  # unk150
    _skip_tarray(r, 1)  # unk200

    # --- VertexStream ---
    if mesh.is_new_format:
        # New format: TArray<FStaticMeshVertexVanguard> + int32 Revision
        # FStaticMeshVertexVanguard: Pos(12) + Normal(12) + TangentU(12) + TangentV(12) + U(4) + V(4) = 56 bytes
        vert_count = _read_count(r, "vertices", element_size=56)
        for i in range(vert_count):
            px, py, pz = r.read_float(), r.read_float(), r.read_float()
            nx, ny, nz = r.read_float(), r.read_float(), r.read_float()
            tux, tuy, tuz = r.read_float(), r.read_float(), r.read_float()
            tvx, tvy, tvz = r.read_float(), r.read_float(), r.read_float()
            # UV
            u, v = r.read_float(), r.read_float()
            mesh.vertices.append((px, py, pz))
            mesh.normals.append((nx, ny, nz))
            mesh.tangent_us.append((tux, tuy, tuz))
            mesh.tangent_vs.append((tvx, tvy, tvz))
            mesh.uvs.append((u, v))
        r.read_int32()  # Revision
    else:
        # Old format: TArray<FStaticMeshVertex> + int32 Revision
        # FStaticMeshVertex: Pos(12) + Normal(12) = 24 bytes
        vert_count = _read_count(r, "vertices", element_size=24)
        for i in range(vert_count):
            px, py, pz = r.read_float(), r.read_float(), r.read_float()
            nx, ny, nz = r.read_float(), r.read_float(), r.read_float()
            mesh.vertices.append((px, py, pz))
            mesh.normals.append((nx, ny, nz))
        r.read_int32()  # Revision

        # Use UVStream for UVs (if available)
        if uv_streams and len(uv_streams[0]) == vert_count:
            mesh.uvs = uv_streams[0]

    # If new format had empty UVStream, UVs are already populated from vertex data
    # If old format, UVs come from UVStream above

    # --- ColorStream (FRawColorStream: TArray<FColor> + int32 Revision) ---
    color_count = _read_count(r, "vertex colors", element_size=4)
    for _ci in range(color_count):
        b, g, rv, a = struct.unpack_from("<BBBB", r.data, r.pos)
        r.skip(4)
        mesh.colors.append((rv / 255.0, g / 255.0, b / 255.0, a / 255.0))
    _require_bytes(r, 4, "vertex color revision")
    r.read_int32()  # Revision

    # --- AlphaStream (FRawColorStream: TArray<FColor> + int32 Revision) ---
    _skip_tarray(r, 4)  # TArray<FColor>
    _require_bytes(r, 4, "alpha revision")
    r.read_int32()  # Revision

    # --- IndexStream1 (FRawIndexBuffer: TArray<uint16> + int32 Revision) ---
    idx1_count = _read_count(r, "primary indices", element_size=2)
    idx1_data = r.data[r.pos : r.pos + idx1_count * 2]
    r.skip(idx1_count * 2)
    _require_bytes(r, 4, "primary index revision")
    r.read_int32()  # Revision

    # --- IndexStream2 (FRawIndexBuffer: TArray<uint16> + int32 Revision) ---
    idx2_count = _read_count(r, "secondary indices", element_size=2)
    r.skip(idx2_count * 2)
    _require_bytes(r, 4, "secondary index revision")
    r.read_int32()

    # Store raw index buffer for direct section-based access in glTF export
    if idx1_count >= 3:
        indices = struct.unpack_from(f"<{idx1_count}H", idx1_data)
        mesh.raw_indices = list(indices)
        # Build a canonical face list from the section's serialized topology.
        for sec in mesh.sections:
            triangle_indices = section_triangle_indices(
                indices, sec, vertex_count=len(mesh.vertices)
            )
            mesh.faces.extend(
                tuple(triangle_indices[offset : offset + 3])
                for offset in range(0, len(triangle_indices), 3)
            )

    if not mesh.vertices:
        raise StaticMeshParseError("mesh has no vertices")
    if mesh.sections and not mesh.raw_indices:
        raise StaticMeshParseError("mesh sections have no primary index buffer")
    mesh.bytes_consumed = r.tell()
    return mesh
