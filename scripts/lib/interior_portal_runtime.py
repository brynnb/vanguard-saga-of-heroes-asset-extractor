"""Compact runtime catalog for authored Vanguard room-and-portal interiors."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


PORTAL_RUNTIME_SCHEMA = "vanguard_interior_portal_runtime_catalog"
PORTAL_RUNTIME_VERSION = 1
COORDINATE_SPACE = "godot_template_local_y_up"
ROOM_BOUNDS_POLICY = "room_visual_aabb_plus_portal_apertures_v1"


def _finite(value: object, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} is not finite")
    return result


def _vector(value: object, length: int, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"{label} must contain {length} values")
    return [_finite(entry, label) for entry in value]


def _source_to_godot(point: list[float] | tuple[float, float, float]) -> list[float]:
    # StaticMesh extraction writes glTF=(-Vanguard.y, Vanguard.z, Vanguard.x).
    return [-float(point[1]), float(point[2]), float(point[0])]


def _godot_to_source(point: list[float] | tuple[float, float, float]) -> list[float]:
    return [float(point[2]), -float(point[0]), float(point[1])]


def _apply_source_transform(point: list[float], transform: object, label: str) -> list[float]:
    if not isinstance(transform, dict):
        raise ValueError(f"{label} transform is invalid")
    basis = transform.get("basis")
    if not isinstance(basis, list) or len(basis) != 3:
        raise ValueError(f"{label} transform basis is invalid")
    rows = [_vector(row, 3, f"{label} transform basis") for row in basis]
    origin = _vector(transform.get("origin"), 3, f"{label} transform origin")
    return [
        origin[row] + sum(rows[row][column] * point[column] for column in range(3))
        for row in range(3)
    ]


def _bounds(points: list[list[float]], label: str) -> list[list[float]]:
    if not points:
        raise ValueError(f"{label} has no bounds geometry")
    minimum = [min(point[axis] for point in points) for axis in range(3)]
    maximum = [max(point[axis] for point in points) for axis in range(3)]
    if any(maximum[axis] < minimum[axis] for axis in range(3)):
        raise ValueError(f"{label} bounds are invalid")
    return [minimum, maximum]


def _corners(bounds: list[list[float]]) -> list[list[float]]:
    minimum, maximum = bounds
    return [
        [x, y, z]
        for x in (minimum[0], maximum[0])
        for y in (minimum[1], maximum[1])
        for z in (minimum[2], maximum[2])
    ]


def _plane(positions: list[list[float]], indices: list[int], label: str) -> list[float]:
    for offset in range(0, len(indices), 3):
        a, b, c = (positions[indices[offset + index]] for index in range(3))
        ab = [b[axis] - a[axis] for axis in range(3)]
        ac = [c[axis] - a[axis] for axis in range(3)]
        normal = [
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        ]
        length = math.sqrt(sum(value * value for value in normal))
        if length > 1.0e-9:
            normal = [value / length for value in normal]
            return [*normal, -sum(normal[axis] * a[axis] for axis in range(3))]
    raise ValueError(f"{label} has no non-degenerate aperture triangle")


class GltfBoundsLibrary:
    """Read conservative local AABBs from generated building glTF metadata."""

    def __init__(self, mesh_root: Path) -> None:
        self.mesh_root = mesh_root.resolve()
        self._cache: dict[str, list[list[float]]] = {}

    def get(self, relative_path: str) -> list[list[float]]:
        folded = relative_path.casefold()
        cached = self._cache.get(folded)
        if cached is not None:
            return cached
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe interior mesh path: {relative_path!r}")
        path = self.mesh_root / relative
        if not path.is_file():
            raise ValueError(f"interior mesh is absent: {path}")
        document = json.loads(path.read_bytes())
        meshes = document.get("meshes", [])
        accessors = document.get("accessors", [])
        nodes = document.get("nodes", [])
        points: list[list[float]] = []
        if (
            not isinstance(meshes, list)
            or not isinstance(accessors, list)
            or not isinstance(nodes, list)
        ):
            raise ValueError(f"{path}: glTF mesh/accessor tables are invalid")
        for node in nodes:
            if not isinstance(node, dict) or "mesh" not in node:
                continue
            if any(field in node for field in ("matrix", "translation", "rotation", "scale")):
                raise ValueError(f"{path}: transformed glTF mesh nodes are unsupported")
        for mesh in meshes:
            if not isinstance(mesh, dict):
                continue
            for primitive in mesh.get("primitives", []):
                attributes = primitive.get("attributes", {}) if isinstance(primitive, dict) else {}
                if not isinstance(attributes, dict) or "POSITION" not in attributes:
                    continue
                accessor_index = int(attributes["POSITION"])
                if not 0 <= accessor_index < len(accessors):
                    raise ValueError(f"{path}: invalid POSITION accessor {accessor_index}")
                accessor = accessors[accessor_index]
                if not isinstance(accessor, dict):
                    raise ValueError(f"{path}: invalid POSITION accessor {accessor_index}")
                minimum = _vector(accessor.get("min"), 3, f"{path} POSITION minimum")
                maximum = _vector(accessor.get("max"), 3, f"{path} POSITION maximum")
                points.extend(_corners([minimum, maximum]))
        cached = _bounds(points, str(path))
        self._cache[folded] = cached
        return cached


def build_portal_runtime_catalog(
    source: dict[str, Any],
    boundary: dict[str, Any],
    room_packs: list[dict[str, Any]],
    mesh_root: Path,
) -> dict[str, Any]:
    """Build reusable template geometry plus exact instance-to-pack mappings."""

    eligible_instances = boundary.get("eligible_instances", [])
    if not isinstance(eligible_instances, list):
        raise ValueError("interior boundary eligible instances are invalid")
    template_by_id = {
        str(template.get("interior_space_asset_id", "")): template
        for template in source.get("interior_templates", [])
        if isinstance(template, dict)
    }
    source_instance_by_id = {
        str(instance.get("interior_instance_id", "")): instance
        for instance in source.get("instances", [])
        if isinstance(instance, dict)
    }
    pack_by_template = {
        str(pack.get("interior_space_asset_id", "")): pack for pack in room_packs
    }
    strings = sorted(
        {
            str(value)
            for record in eligible_instances
            for value in (
                record.get("chunk", ""),
                record.get("interior_instance_id", ""),
                record.get("interior_space_asset_id", ""),
                record.get("room_pack_id", ""),
            )
        }
        | {
            str(value)
            for template_id in pack_by_template
            for value in (template_id, pack_by_template[template_id].get("room_pack_id", ""))
        }
    )
    string_index = {value: index for index, value in enumerate(strings)}

    def intern(value: object) -> int:
        text = str(value)
        if text not in string_index:
            string_index[text] = len(strings)
            strings.append(text)
        return string_index[text]

    bounds_library = GltfBoundsLibrary(mesh_root)
    templates: list[dict[str, Any]] = []
    template_index_by_id: dict[str, int] = {}
    total_rooms = 0
    total_endpoints = 0
    total_connections = 0
    total_boundaries = 0
    for template_id in sorted(pack_by_template):
        template = template_by_id.get(template_id)
        pack = pack_by_template[template_id]
        if template is None:
            raise ValueError(f"room pack has no source template: {template_id}")
        graph = template.get("portal_graph")
        if not isinstance(graph, dict) or graph.get("unresolved"):
            raise ValueError(f"{template_id}: portal graph is unresolved")
        source_rooms = template.get("rooms", [])
        pack_rooms = pack.get("rooms", [])
        if not isinstance(source_rooms, list) or not isinstance(pack_rooms, list):
            raise ValueError(f"{template_id}: room records are invalid")
        source_room_by_id = {str(room.get("room_id", "")): room for room in source_rooms}
        room_index_by_id = {
            str(room.get("room_id", "")): index for index, room in enumerate(pack_rooms)
        }
        if set(source_room_by_id) != set(room_index_by_id):
            raise ValueError(f"{template_id}: source and room-pack rooms differ")
        endpoint_records: list[list[Any]] = []
        endpoint_index_by_id: dict[str, int] = {}
        endpoint_indices_by_room: dict[str, list[int]] = {
            room_id: [] for room_id in room_index_by_id
        }
        aperture_points_by_room: dict[str, list[list[float]]] = {
            room_id: [] for room_id in room_index_by_id
        }
        for endpoint in graph.get("endpoints", []):
            if not isinstance(endpoint, dict) or endpoint.get("aperture_status") != "exact":
                raise ValueError(f"{template_id}: portal endpoint is not exact")
            endpoint_id = str(endpoint.get("endpoint_id", ""))
            room_id = str(endpoint.get("room_id", ""))
            if (
                not endpoint_id
                or endpoint_id in endpoint_index_by_id
                or room_id not in room_index_by_id
            ):
                raise ValueError(f"{template_id}: portal endpoint identity is invalid")
            aperture = endpoint.get("aperture_geometry")
            primitives = aperture.get("primitives", []) if isinstance(aperture, dict) else []
            positions: list[list[float]] = []
            indices: list[int] = []
            for primitive in primitives:
                primitive_positions = [
                    _source_to_godot(_vector(value, 3, f"{endpoint_id} aperture position"))
                    for value in primitive.get("positions", [])
                ]
                primitive_indices = [int(value) for value in primitive.get("indices", [])]
                if len(primitive_indices) % 3 or any(
                    value < 0 or value >= len(primitive_positions) for value in primitive_indices
                ):
                    raise ValueError(f"{endpoint_id}: aperture indices are invalid")
                base = len(positions)
                positions.extend(primitive_positions)
                indices.extend(base + value for value in primitive_indices)
            aperture_bounds = _bounds(positions, endpoint_id)
            endpoint_index = len(endpoint_records)
            endpoint_index_by_id[endpoint_id] = endpoint_index
            endpoint_indices_by_room[room_id].append(endpoint_index)
            aperture_points_by_room[room_id].extend(positions)
            endpoint_records.append(
                [
                    intern(endpoint_id),
                    room_index_by_id[room_id],
                    aperture_bounds[0],
                    aperture_bounds[1],
                    _plane(positions, indices, endpoint_id),
                    positions,
                    indices,
                ]
            )
        adjacency = {
            str(record.get("room_id", "")): [
                str(value) for value in record.get("visible_room_ids", [])
            ]
            for record in graph.get("adjacency", [])
        }
        room_records: list[list[Any]] = []
        for pack_room in pack_rooms:
            room_id = str(pack_room.get("room_id", ""))
            source_room = source_room_by_id[room_id]
            room_points = list(aperture_points_by_room[room_id])
            for actor in source_room.get("visual_components", []):
                component_path = str(actor.get("source_component_path", ""))
                pack_component = next(
                    (
                        value
                        for value in pack_room.get("visual_components", [])
                        if value.get("source_component_path") == component_path
                    ),
                    None,
                )
                if pack_component is None:
                    raise ValueError(f"{template_id}/{room_id}: visual is absent from room pack")
                local_bounds = bounds_library.get(str(pack_component.get("mesh_path", "")))
                for corner in _corners(local_bounds):
                    source_corner = _godot_to_source(corner)
                    transformed = _apply_source_transform(
                        source_corner, actor.get("transform"), component_path
                    )
                    room_points.append(_source_to_godot(transformed))
            room_bounds = _bounds(room_points, f"{template_id}/{room_id}")
            neighbors = adjacency.get(room_id)
            if neighbors is None or any(value not in room_index_by_id for value in neighbors):
                raise ValueError(f"{template_id}/{room_id}: adjacency is invalid")
            room_records.append(
                [
                    intern(room_id),
                    room_bounds[0],
                    room_bounds[1],
                    sorted(endpoint_indices_by_room[room_id]),
                    sorted(room_index_by_id[value] for value in neighbors),
                ]
            )
        connection_records: list[list[Any]] = []
        for connection in graph.get("connections", []):
            endpoint_ids = [str(value) for value in connection.get("endpoint_ids", [])]
            room_ids = [str(value) for value in connection.get("room_ids", [])]
            if any(value not in endpoint_index_by_id for value in endpoint_ids) or any(
                value not in room_index_by_id for value in room_ids
            ):
                raise ValueError(f"{template_id}: portal connection is invalid")
            connection_records.append(
                [
                    intern(connection.get("connection_id", "")),
                    [endpoint_index_by_id[value] for value in endpoint_ids],
                    [room_index_by_id[value] for value in room_ids],
                ]
            )
        boundary_records: list[list[int]] = []
        for record in graph.get("boundaries", []):
            endpoint_id = str(record.get("endpoint_id", ""))
            room_id = str(record.get("room_id", ""))
            if endpoint_id not in endpoint_index_by_id or room_id not in room_index_by_id:
                raise ValueError(f"{template_id}: exterior portal boundary is invalid")
            boundary_records.append(
                [
                    intern(record.get("boundary_id", "")),
                    endpoint_index_by_id[endpoint_id],
                    room_index_by_id[room_id],
                ]
            )
        template_index_by_id[template_id] = len(templates)
        room_pack_id = str(pack.get("room_pack_id", ""))
        templates.append(
            {
                "template_id_string": intern(template_id),
                "room_pack_id_string": intern(room_pack_id),
                "room_pack_relative_path_string": intern(
                    f"interior_room_packs.v1/{room_pack_id}.json"
                ),
                "rooms": room_records,
                "endpoints": endpoint_records,
                "connections": connection_records,
                "boundaries": boundary_records,
            }
        )
        total_rooms += len(room_records)
        total_endpoints += len(endpoint_records)
        total_connections += len(connection_records)
        total_boundaries += len(boundary_records)

    instance_records: list[list[Any]] = []
    for record in eligible_instances:
        instance_id = str(record.get("interior_instance_id", ""))
        template_id = str(record.get("interior_space_asset_id", ""))
        source_instance = source_instance_by_id.get(instance_id)
        if source_instance is None or template_id not in template_index_by_id:
            raise ValueError(f"{instance_id}: portal runtime mapping is incomplete")
        chunk_origin = _vector(
            source_instance.get("chunk_global_origin"), 3, f"{instance_id} chunk origin"
        )
        root = source_instance.get("root_transform")
        if not isinstance(root, dict):
            raise ValueError(f"{instance_id}: root transform is invalid")
        translation = _vector(root.get("translation"), 3, f"{instance_id} translation")
        rotation = root.get("rotation_quaternion")
        scale = root.get("scale")
        instance_records.append(
            [
                intern(instance_id),
                template_index_by_id[template_id],
                intern(record.get("room_pack_id", "")),
                intern(record.get("chunk", "")),
                int(record.get("node_index", -1)),
                chunk_origin,
                translation,
                [chunk_origin[axis] + translation[axis] for axis in range(3)],
                None if rotation is None else _vector(rotation, 4, f"{instance_id} rotation"),
                None if scale is None else _vector(scale, 3, f"{instance_id} scale"),
            ]
        )
    if len(instance_records) != len(eligible_instances):
        raise ValueError("portal runtime instance coverage is not one-to-one")

    return {
        "schema": PORTAL_RUNTIME_SCHEMA,
        "version": PORTAL_RUNTIME_VERSION,
        "coordinate_space": COORDINATE_SPACE,
        "room_bounds_policy": ROOM_BOUNDS_POLICY,
        "source_publication_id": source.get("publication_id"),
        "source_publication_sha256": boundary.get("source_publication_sha256"),
        "cesium_boundary_id": boundary.get("boundary_id"),
        "runtime_selection_id": boundary.get("runtime_selection_id"),
        "counts": {
            "template_count": len(templates),
            "instance_count": len(instance_records),
            "room_count": total_rooms,
            "endpoint_count": total_endpoints,
            "connection_count": total_connections,
            "exterior_boundary_count": total_boundaries,
        },
        "room_record_format": [
            "room_id_string",
            "bounds_minimum_xyz",
            "bounds_maximum_xyz",
            "endpoint_indices",
            "adjacent_room_indices",
        ],
        "endpoint_record_format": [
            "endpoint_id_string",
            "room_index",
            "bounds_minimum_xyz",
            "bounds_maximum_xyz",
            "plane_xyzd",
            "positions_xyz",
            "triangle_indices",
        ],
        "connection_record_format": [
            "connection_id_string",
            "endpoint_indices",
            "room_indices",
        ],
        "boundary_record_format": [
            "boundary_id_string",
            "endpoint_index",
            "room_index",
        ],
        "instance_record_format": [
            "instance_id_string",
            "template_index",
            "room_pack_id_string",
            "chunk_string",
            "node_index",
            "chunk_global_origin_xyz",
            "root_translation_chunk_local_xyz",
            "root_translation_global_xyz",
            "root_rotation_quaternion_xyzw",
            "root_scale_xyz",
        ],
        "string_table": strings,
        "templates": templates,
        "instances": instance_records,
    }
