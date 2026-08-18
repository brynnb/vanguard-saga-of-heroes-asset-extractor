"""Strict Vanguard UE2 BSP/model authority decoding.

Vanguard's version-128/129 packages serialize ``UModel`` trans-arrays with
32-bit counts while the records inside those arrays continue to use Unreal's
compact object/index encoding.  Older extractor code treated the counts and
records as one encoding and consequently lost alignment before the zone and
visibility tables.

This module intentionally targets the Vanguard package revision we can verify.
It decodes the source fields needed by world residency and fails closed on
unsupported revisions, malformed counts, invalid references, and truncated
records.  Licensee-specific render-section data after the common model body is
kept as a digest-bound opaque tail; it is not needed to establish BSP zone,
leaf, or visibility authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import struct
from typing import Any, Iterable

from ue2.types import Plane, Vector


SUPPORTED_VANGUARD_PACKAGE_REVISIONS = frozenset(
    ((128, 34), (129, 34), (129, 35))
)
MAX_ZONES = 64


class BspParseError(ValueError):
    """Raised when a serialized model cannot be proven safe to consume."""


@dataclass(frozen=True)
class BspBounds:
    minimum: Vector
    maximum: Vector
    valid: bool


@dataclass
class BspNode:
    plane: Plane
    zone_mask: int
    node_flags: int
    i_vert_pool: int
    i_surf: int
    i_back: int
    i_front: int
    i_plane: int
    i_collision_bound: int
    i_render_bound: int
    exclusive_sphere_bound: Plane
    inclusive_sphere_bound: Plane
    i_zone: list[int]
    num_vertices: int
    i_leaf: list[int]
    i_section: int
    i_first_vertex: int
    i_lightmap_index: int


@dataclass
class BspSurface:
    material: int
    poly_flags: int
    p_base: int
    v_normal: int
    v_texture_u: int
    v_texture_v: int
    i_brush_poly: int
    actor: int
    plane: Plane
    light_map_scale: float

    @property
    def texture(self) -> int:
        """Compatibility alias used by the legacy BSP mesh exporter."""

        return self.material


@dataclass
class ModelVertex:
    vertex: int
    i_side: int


@dataclass(frozen=True)
class ZoneProperties:
    zone_actor: int
    connectivity: int
    visibility: int
    last_render_time: float


@dataclass(frozen=True)
class BspLeaf:
    i_zone: int
    i_permeating: int
    i_volumetric: int
    visible_zones: int


@dataclass
class UModel:
    bounding_box: BspBounds
    bounding_sphere: Plane
    vectors: list[Vector] = field(default_factory=list)
    points: list[Vector] = field(default_factory=list)
    nodes: list[BspNode] = field(default_factory=list)
    surfaces: list[BspSurface] = field(default_factory=list)
    vertices: list[ModelVertex] = field(default_factory=list)
    num_shared_sides: int = 0
    zones: list[ZoneProperties] = field(default_factory=list)
    polys_ref: int = 0
    bounds: list[BspBounds] = field(default_factory=list)
    leaf_hulls: list[int] = field(default_factory=list)
    leaves: list[BspLeaf] = field(default_factory=list)
    lights: list[int] = field(default_factory=list)
    root_outside: bool = False
    linked: bool = False
    property_bytes: int = 0
    common_body_bytes: int = 0
    extension_tail_bytes: int = 0
    extension_tail_sha256: str = ""

    @property
    def num_zones(self) -> int:
        return len(self.zones)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def tell(self) -> int:
        return self.pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def need(self, size: int, label: str) -> None:
        if size < 0 or self.pos + size > len(self.data):
            raise BspParseError(
                f"truncated {label} at byte {self.pos}: need {size}, "
                f"have {self.remaining()}"
            )

    def raw(self, size: int, label: str) -> bytes:
        self.need(size, label)
        value = self.data[self.pos : self.pos + size]
        self.pos += size
        return value

    def uint8(self, label: str) -> int:
        return self.raw(1, label)[0]

    def int16(self, label: str) -> int:
        return struct.unpack("<h", self.raw(2, label))[0]

    def uint16(self, label: str) -> int:
        return struct.unpack("<H", self.raw(2, label))[0]

    def int32(self, label: str) -> int:
        return struct.unpack("<i", self.raw(4, label))[0]

    def uint32(self, label: str) -> int:
        return struct.unpack("<I", self.raw(4, label))[0]

    def uint64(self, label: str) -> int:
        return struct.unpack("<Q", self.raw(8, label))[0]

    def float32(self, label: str) -> float:
        value = struct.unpack("<f", self.raw(4, label))[0]
        if not math.isfinite(value):
            raise BspParseError(f"non-finite {label} at byte {self.pos - 4}")
        return value

    def compact_index(self, label: str) -> int:
        first = self.uint8(label)
        negative = bool(first & 0x80)
        value = first & 0x3F
        if first & 0x40:
            second = self.uint8(label)
            value |= (second & 0x7F) << 6
            if second & 0x80:
                third = self.uint8(label)
                value |= (third & 0x7F) << 13
                if third & 0x80:
                    fourth = self.uint8(label)
                    value |= (fourth & 0x7F) << 20
                    if fourth & 0x80:
                        fifth = self.uint8(label)
                        value |= fifth << 27
        return -value if negative else value


def _vector(reader: _Reader, label: str) -> Vector:
    return Vector(
        reader.float32(f"{label}.x"),
        reader.float32(f"{label}.y"),
        reader.float32(f"{label}.z"),
    )


def _plane(reader: _Reader, label: str) -> Plane:
    return Plane(
        reader.float32(f"{label}.x"),
        reader.float32(f"{label}.y"),
        reader.float32(f"{label}.z"),
        reader.float32(f"{label}.w"),
    )


def _bounds(reader: _Reader, label: str) -> BspBounds:
    return BspBounds(
        minimum=_vector(reader, f"{label}.minimum"),
        maximum=_vector(reader, f"{label}.maximum"),
        valid=bool(reader.uint8(f"{label}.valid")),
    )


def _count(reader: _Reader, label: str, *, item_min_bytes: int, limit: int) -> int:
    count = reader.int32(f"{label}.count")
    if count < 0 or count > limit:
        raise BspParseError(f"invalid {label} count {count}")
    if count * item_min_bytes > reader.remaining():
        raise BspParseError(
            f"{label} count {count} cannot fit in {reader.remaining()} remaining bytes"
        )
    return count


def _skip_properties(reader: _Reader, names: list[str]) -> int:
    start = reader.tell()
    while True:
        name_index = reader.compact_index("property.name")
        if not 0 <= name_index < len(names):
            raise BspParseError(f"invalid property name index {name_index}")
        if names[name_index].lower() == "none":
            return reader.tell() - start

        info = reader.uint8("property.info")
        prop_type = info & 0x0F
        size_type = (info >> 4) & 0x07
        array_flag = bool(info & 0x80)
        if prop_type == 10:
            struct_name = reader.compact_index("property.struct_name")
            if not 0 <= struct_name < len(names):
                raise BspParseError(f"invalid struct name index {struct_name}")

        if size_type == 0:
            size = 1
        elif size_type == 1:
            size = 2
        elif size_type == 2:
            size = 4
        elif size_type == 3:
            size = 12
        elif size_type == 4:
            size = 16
        elif size_type == 5:
            size = reader.uint8("property.size8")
        elif size_type == 6:
            size = reader.uint16("property.size16")
        else:
            size = reader.uint32("property.size32")

        if prop_type != 3 and array_flag:
            reader.compact_index("property.array_index")
        if prop_type == 3:
            size = 0
        reader.raw(size, "property.value")


def parse_model_data(
    data: bytes,
    names: list[str],
    *,
    archive_version: int,
    licensee_version: int,
    export_count: int | None = None,
    import_count: int | None = None,
) -> UModel:
    """Decode one serialized Vanguard ``UModel`` export."""

    package_revision = (archive_version, licensee_version)
    if package_revision not in SUPPORTED_VANGUARD_PACKAGE_REVISIONS:
        raise BspParseError(
            "unsupported Vanguard package revision "
            f"{archive_version}/{licensee_version}; expected one of "
            f"{sorted(SUPPORTED_VANGUARD_PACKAGE_REVISIONS)}"
        )
    if not data:
        raise BspParseError("empty Model export")

    reader = _Reader(data)
    property_bytes = _skip_properties(reader, names)
    bounding_box = _bounds(reader, "primitive.bounding_box")
    bounding_sphere = _plane(reader, "primitive.bounding_sphere")

    vector_count = _count(reader, "vectors", item_min_bytes=12, limit=2_000_000)
    vectors = [_vector(reader, f"vectors[{i}]") for i in range(vector_count)]
    point_count = _count(reader, "points", item_min_bytes=12, limit=2_000_000)
    points = [_vector(reader, f"points[{i}]") for i in range(point_count)]

    node_count = _count(reader, "nodes", item_min_bytes=80, limit=2_000_000)
    nodes: list[BspNode] = []
    for i in range(node_count):
        prefix = f"nodes[{i}]"
        nodes.append(
            BspNode(
                plane=_plane(reader, f"{prefix}.plane"),
                zone_mask=reader.uint64(f"{prefix}.zone_mask"),
                node_flags=reader.uint8(f"{prefix}.flags"),
                i_vert_pool=reader.compact_index(f"{prefix}.i_vert_pool"),
                i_surf=reader.compact_index(f"{prefix}.i_surf"),
                i_back=reader.compact_index(f"{prefix}.i_back"),
                i_front=reader.compact_index(f"{prefix}.i_front"),
                i_plane=reader.compact_index(f"{prefix}.i_plane"),
                i_collision_bound=reader.compact_index(
                    f"{prefix}.i_collision_bound"
                ),
                i_render_bound=reader.compact_index(f"{prefix}.i_render_bound"),
                exclusive_sphere_bound=_plane(
                    reader, f"{prefix}.exclusive_sphere_bound"
                ),
                inclusive_sphere_bound=_plane(
                    reader, f"{prefix}.inclusive_sphere_bound"
                ),
                i_zone=[
                    reader.uint8(f"{prefix}.i_zone[0]"),
                    reader.uint8(f"{prefix}.i_zone[1]"),
                ],
                num_vertices=reader.uint8(f"{prefix}.num_vertices"),
                i_leaf=[
                    reader.int32(f"{prefix}.i_leaf[0]"),
                    reader.int32(f"{prefix}.i_leaf[1]"),
                ],
                i_section=reader.int32(f"{prefix}.i_section"),
                i_first_vertex=reader.int32(f"{prefix}.i_first_vertex"),
                i_lightmap_index=reader.int32(f"{prefix}.i_lightmap_index"),
            )
        )

    surface_count = _count(
        reader, "surfaces", item_min_bytes=31, limit=2_000_000
    )
    surfaces: list[BspSurface] = []
    for i in range(surface_count):
        prefix = f"surfaces[{i}]"
        surfaces.append(
            BspSurface(
                material=reader.compact_index(f"{prefix}.material"),
                poly_flags=reader.uint32(f"{prefix}.poly_flags"),
                p_base=reader.compact_index(f"{prefix}.p_base"),
                v_normal=reader.compact_index(f"{prefix}.v_normal"),
                v_texture_u=reader.compact_index(f"{prefix}.v_texture_u"),
                v_texture_v=reader.compact_index(f"{prefix}.v_texture_v"),
                i_brush_poly=reader.compact_index(f"{prefix}.i_brush_poly"),
                actor=reader.compact_index(f"{prefix}.actor"),
                plane=_plane(reader, f"{prefix}.plane"),
                light_map_scale=reader.float32(f"{prefix}.light_map_scale"),
            )
        )

    vertex_count = _count(reader, "vertices", item_min_bytes=2, limit=8_000_000)
    vertices = [
        ModelVertex(
            vertex=reader.compact_index(f"vertices[{i}].vertex"),
            i_side=reader.compact_index(f"vertices[{i}].side"),
        )
        for i in range(vertex_count)
    ]

    num_shared_sides = reader.int32("num_shared_sides")
    if num_shared_sides < 0:
        raise BspParseError(f"invalid num_shared_sides {num_shared_sides}")
    zone_count = reader.int32("zones.count")
    if not 0 <= zone_count <= MAX_ZONES:
        raise BspParseError(f"invalid zone count {zone_count}")
    zones = [
        ZoneProperties(
            zone_actor=reader.compact_index(f"zones[{i}].zone_actor"),
            connectivity=reader.uint64(f"zones[{i}].connectivity"),
            visibility=reader.uint64(f"zones[{i}].visibility"),
            last_render_time=reader.float32(f"zones[{i}].last_render_time"),
        )
        for i in range(zone_count)
    ]

    polys_ref = reader.compact_index("polys_ref")
    bounds_count = _count(reader, "bounds", item_min_bytes=25, limit=2_000_000)
    bounds = [_bounds(reader, f"bounds[{i}]") for i in range(bounds_count)]
    hull_count = _count(reader, "leaf_hulls", item_min_bytes=4, limit=20_000_000)
    leaf_hulls = [reader.int32(f"leaf_hulls[{i}]") for i in range(hull_count)]
    leaf_count = _count(reader, "leaves", item_min_bytes=11, limit=2_000_000)
    leaves = [
        BspLeaf(
            i_zone=reader.compact_index(f"leaves[{i}].i_zone"),
            i_permeating=reader.compact_index(f"leaves[{i}].i_permeating"),
            i_volumetric=reader.compact_index(f"leaves[{i}].i_volumetric"),
            visible_zones=reader.uint64(f"leaves[{i}].visible_zones"),
        )
        for i in range(leaf_count)
    ]
    light_count = _count(reader, "lights", item_min_bytes=1, limit=2_000_000)
    lights = [reader.compact_index(f"lights[{i}]") for i in range(light_count)]
    root_outside_raw = reader.int32("root_outside")
    linked_raw = reader.int32("linked")
    if root_outside_raw not in (0, 1) or linked_raw not in (0, 1):
        raise BspParseError(
            f"invalid model booleans root_outside={root_outside_raw}, "
            f"linked={linked_raw}"
        )

    common_body_bytes = reader.tell()
    tail = reader.raw(reader.remaining(), "model extension tail")
    if len(tail) < 12:
        raise BspParseError(
            "Vanguard model extension tail is missing its three array counts"
        )

    model = UModel(
        bounding_box=bounding_box,
        bounding_sphere=bounding_sphere,
        vectors=vectors,
        points=points,
        nodes=nodes,
        surfaces=surfaces,
        vertices=vertices,
        num_shared_sides=num_shared_sides,
        zones=zones,
        polys_ref=polys_ref,
        bounds=bounds,
        leaf_hulls=leaf_hulls,
        leaves=leaves,
        lights=lights,
        root_outside=bool(root_outside_raw),
        linked=bool(linked_raw),
        property_bytes=property_bytes,
        common_body_bytes=common_body_bytes,
        extension_tail_bytes=len(tail),
        extension_tail_sha256=hashlib.sha256(tail).hexdigest(),
    )
    _validate_model(
        model,
        export_count=export_count,
        import_count=import_count,
    )
    return model


def model_collision_triangles(
    model: UModel,
) -> tuple[list[tuple[float, float, float]], list[int]]:
    """Materialize the polygon faces used by a UE2 ``UModel`` collision hull.

    Each BSP node owns a contiguous polygon in the model vertex pool.  The
    original collision traversal uses the BSP directly; a Godot concave shape
    needs the equivalent polygon soup, so triangulate each node as a fan while
    retaining the serialized vertex order.  The consumer enables two-sided
    collision, matching UE2's swept-box behavior.
    """

    positions: list[tuple[float, float, float]] = []
    indices: list[int] = []
    for node_index, node in enumerate(model.nodes):
        if node.num_vertices < 3:
            continue
        first = node.i_vert_pool
        end = first + node.num_vertices
        if first < 0 or end > len(model.vertices):
            raise BspParseError(
                f"collision node {node_index} vertex range {first}:{end} is invalid"
            )
        polygon: list[tuple[float, float, float]] = []
        for pool_index in range(first, end):
            point_index = model.vertices[pool_index].vertex
            if point_index < 0 or point_index >= len(model.points):
                raise BspParseError(
                    f"collision node {node_index} references invalid point {point_index}"
                )
            point = model.points[point_index]
            polygon.append((float(point.x), float(point.y), float(point.z)))
        base = len(positions)
        positions.extend(polygon)
        for offset in range(1, len(polygon) - 1):
            indices.extend((base, base + offset, base + offset + 1))
    if not positions or not indices:
        raise BspParseError("collision Model contains no materializable polygons")
    return positions, indices


def parse_model_export(package: Any, export: dict[str, Any]) -> UModel:
    if export.get("class_name") != "Model":
        raise BspParseError(
            f"export {export.get('object_name', '<unnamed>')} is not a Model"
        )
    return parse_model_data(
        package.get_export_data(export),
        package.names,
        archive_version=package.version,
        licensee_version=package.licensee,
        export_count=len(package.exports),
        import_count=len(package.imports),
    )


def find_level_model_reference(package: Any, level_export: dict[str, Any]) -> int:
    """Recover the authoritative ULevel->UModel binding from its exact trailer.

    The large actor databases at the beginning of Vanguard ``ULevel`` exports
    are sparse compact-reference arrays.  The end of the record is stable and
    self-validating: base model, approximate time, first-deleted reference,
    sixteen text-block references, and an empty 32-bit travel-info map count.
    We scan for an exact-to-end trailer and require one unique Model reference.
    """

    if level_export.get("class_name") != "Level":
        raise BspParseError("level model lookup requires a Level export")
    data = package.get_export_data(level_export)
    candidates: list[int] = []
    for offset in range(len(data)):
        reader = _Reader(data)
        reader.pos = offset
        try:
            model_ref = reader.compact_index("level.base_model")
            if not 1 <= model_ref <= len(package.exports):
                continue
            if package.exports[model_ref - 1].get("class_name") != "Model":
                continue
            reader.float32("level.approx_time")
            refs = [reader.compact_index("level.first_deleted")]
            refs.extend(
                reader.compact_index(f"level.text_blocks[{i}]") for i in range(16)
            )
            if not all(
                -len(package.imports) <= value <= len(package.exports)
                for value in refs
            ):
                continue
            if reader.int32("level.travel_info_count") != 0:
                continue
            if reader.remaining() == 0:
                candidates.append(model_ref)
        except (BspParseError, struct.error):
            continue
    unique = sorted(set(candidates))
    if len(unique) != 1:
        raise BspParseError(
            f"expected one exact ULevel Model trailer, found {unique or 'none'}"
        )
    return unique[0]


def _validate_model(
    model: UModel,
    *,
    export_count: int | None,
    import_count: int | None,
) -> None:
    node_count = len(model.nodes)
    surface_count = len(model.surfaces)
    point_count = len(model.points)
    vector_count = len(model.vectors)
    vertex_count = len(model.vertices)
    leaf_count = len(model.leaves)
    zone_count = len(model.zones)

    for i, node in enumerate(model.nodes):
        for label, value in (
            ("i_back", node.i_back),
            ("i_front", node.i_front),
            ("i_plane", node.i_plane),
        ):
            if value != -1 and not 0 <= value < node_count:
                raise BspParseError(f"nodes[{i}].{label}={value} is out of range")
        if not 0 <= node.i_surf < surface_count:
            raise BspParseError(f"nodes[{i}].i_surf={node.i_surf} is out of range")
        if node.num_vertices:
            if node.i_vert_pool < 0 or node.i_vert_pool + node.num_vertices > vertex_count:
                raise BspParseError(
                    f"nodes[{i}] vertex span {node.i_vert_pool}+{node.num_vertices} "
                    f"exceeds {vertex_count}"
                )
        for side, value in enumerate(node.i_leaf):
            if value != -1 and not 0 <= value < leaf_count:
                raise BspParseError(
                    f"nodes[{i}].i_leaf[{side}]={value} is out of range"
                )
        for side, value in enumerate(node.i_zone):
            if zone_count == 0:
                if value != 0:
                    raise BspParseError(
                        f"nodes[{i}].i_zone[{side}]={value} without zones"
                    )
            elif value >= zone_count:
                raise BspParseError(
                    f"nodes[{i}].i_zone[{side}]={value} exceeds {zone_count} zones"
                )

    for i, surface in enumerate(model.surfaces):
        for label, value, size in (
            ("p_base", surface.p_base, point_count),
            ("v_normal", surface.v_normal, vector_count),
            ("v_texture_u", surface.v_texture_u, vector_count),
            ("v_texture_v", surface.v_texture_v, vector_count),
        ):
            if not 0 <= value < size:
                raise BspParseError(
                    f"surfaces[{i}].{label}={value} is out of range {size}"
                )
    # TTransArray retains unused/editor-side FVert slots.  Vanguard level models
    # can contain stale point indices in those slots, so authority is restricted
    # to the exact spans referenced by BSP nodes.  Every referenced vertex must
    # still resolve to a real point.
    referenced_vertices: set[int] = set()
    for node in model.nodes:
        referenced_vertices.update(
            range(node.i_vert_pool, node.i_vert_pool + node.num_vertices)
        )
    for i in sorted(referenced_vertices):
        vertex = model.vertices[i]
        if not 0 <= vertex.vertex < point_count:
            raise BspParseError(
                f"referenced vertices[{i}].vertex={vertex.vertex} is out of "
                f"range {point_count}"
            )
    for i, leaf in enumerate(model.leaves):
        if not 0 <= leaf.i_zone < zone_count:
            raise BspParseError(
                f"leaves[{i}].i_zone={leaf.i_zone} is out of range {zone_count}"
            )

    if export_count is not None and import_count is not None:
        refs: Iterable[tuple[str, int]] = [
            ("polys_ref", model.polys_ref),
            *((f"zones[{i}].zone_actor", zone.zone_actor) for i, zone in enumerate(model.zones)),
            *((f"surfaces[{i}].material", surf.material) for i, surf in enumerate(model.surfaces)),
            *((f"surfaces[{i}].actor", surf.actor) for i, surf in enumerate(model.surfaces)),
            *((f"lights[{i}]", value) for i, value in enumerate(model.lights)),
        ]
        for label, value in refs:
            if not -import_count <= value <= export_count:
                raise BspParseError(f"{label}={value} is not a valid object reference")
