"""Deterministic interior and portal source-graph assembly helpers.

The Vanguard SGO archive stores reusable prefab records.  A CompoundObject with
``m_CompoundObjectType == 3`` is an authored room instance and ``Portal``
exports inside those room prefabs are authored portal endpoints.  This module
keeps the source distinction explicit: BSP zones remain native map authority,
while room adjacency is a deterministic assembly of authored SGO room and
portal records.

No spatial proximity heuristic is used.  Portal endpoints pair only when their
transformed source positions are equal after a tiny, documented floating-point
normalization (1e-6 source units) and the pairing is unique.  Singleton
endpoints are boundaries.  Larger clusters remain unresolved and therefore
cannot expose an unloaded room at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Iterable, Iterator


ROOM_COMPOUND_TYPE = 3
PORTAL_POSITION_DECIMALS = 6
MAX_PREFAB_DEPTH = 64


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_id(prefix: str, value: Any, *, length: int = 32) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"{prefix}_{digest[:length]}"


def source_prefab_id(name: object) -> str:
    text = str(name).strip()
    if not text:
        raise ValueError("source prefab name is empty")
    return f"sgo://binaryprefabs.sgo#Prefab/{text}"


def source_component_path(
    prefab_name: object, class_name: object, object_name: object
) -> str:
    prefab = str(prefab_name).strip()
    actor_class = str(class_name).strip()
    actor = str(object_name).strip()
    if not prefab or not actor_class or not actor:
        raise ValueError("source component identity contains an empty field")
    return f"prefab/{prefab}/actor/{actor_class}/{actor}"


def nested_source_path(parent: object, child: object) -> str:
    parent_text = str(parent).strip()
    child_text = str(child).strip()
    if not parent_text or not child_text:
        raise ValueError("nested source component path is empty")
    return f"{parent_text}/contains/{child_text}"


def _finite_float(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is not numeric: {value!r}") from error
    if not math.isfinite(result):
        raise ValueError(f"{label} is not finite: {value!r}")
    return result


def vector3(value: object | None, *, default: Iterable[float]) -> tuple[float, float, float]:
    if value is None:
        values = list(default)
    elif isinstance(value, dict):
        if all(axis in value for axis in ("x", "y", "z")):
            values = [value["x"], value["y"], value["z"]]
        else:
            raise ValueError(f"vector mapping lacks x/y/z: {value!r}")
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        values = list(value)
    else:
        raise ValueError(f"invalid vector value: {value!r}")
    return tuple(_finite_float(item, "vector component") for item in values)  # type: ignore[return-value]


def rotator3(value: object | None) -> tuple[float, float, float]:
    if value is None:
        return (0.0, 0.0, 0.0)
    if isinstance(value, dict):
        if all(axis in value for axis in ("pitch", "yaw", "roll")):
            values = [value["pitch"], value["yaw"], value["roll"]]
        else:
            raise ValueError(f"rotator mapping lacks pitch/yaw/roll: {value!r}")
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        values = list(value)
    else:
        raise ValueError(f"invalid rotator value: {value!r}")
    return tuple(_finite_float(item, "rotator component") for item in values)  # type: ignore[return-value]


def _mat_mul(
    left: tuple[tuple[float, float, float], ...],
    right: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(sum(left[row][k] * right[k][column] for k in range(3)) for column in range(3))
        for row in range(3)
    )


def _mat_vec(
    matrix: tuple[tuple[float, float, float], ...],
    value: tuple[float, float, float],
) -> tuple[float, float, float]:
    return tuple(
        sum(matrix[row][column] * value[column] for column in range(3))
        for row in range(3)
    )  # type: ignore[return-value]


def ue2_rotation_matrix(
    rotation: object | None,
) -> tuple[tuple[float, float, float], ...]:
    """Return Vanguard's verified ``Rx(roll) * Ry(pitch) * Rz(yaw)`` matrix."""

    pitch_units, yaw_units, roll_units = rotator3(rotation)
    scale = (2.0 * math.pi) / 65536.0
    pitch = pitch_units * scale
    yaw = yaw_units * scale
    roll = roll_units * scale
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cr, sr = math.cos(roll), math.sin(roll)
    yaw_matrix = ((cy, -sy, 0.0), (sy, cy, 0.0), (0.0, 0.0, 1.0))
    pitch_matrix = ((cp, 0.0, sp), (0.0, 1.0, 0.0), (-sp, 0.0, cp))
    roll_matrix = ((1.0, 0.0, 0.0), (0.0, cr, -sr), (0.0, sr, cr))
    return _mat_mul(roll_matrix, _mat_mul(pitch_matrix, yaw_matrix))


@dataclass(frozen=True)
class AffineTransform:
    basis: tuple[tuple[float, float, float], ...]
    origin: tuple[float, float, float]

    @staticmethod
    def identity() -> "AffineTransform":
        return AffineTransform(
            basis=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            origin=(0.0, 0.0, 0.0),
        )

    @staticmethod
    def from_properties(properties: dict[str, Any]) -> "AffineTransform":
        origin = vector3(properties.get("Location"), default=(0.0, 0.0, 0.0))
        scale3 = vector3(properties.get("DrawScale3D"), default=(1.0, 1.0, 1.0))
        scale = _finite_float(properties.get("DrawScale", 1.0), "DrawScale")
        rotation = ue2_rotation_matrix(properties.get("Rotation"))
        diagonal = (
            (scale3[0] * scale, 0.0, 0.0),
            (0.0, scale3[1] * scale, 0.0),
            (0.0, 0.0, scale3[2] * scale),
        )
        return AffineTransform(basis=_mat_mul(rotation, diagonal), origin=origin)

    def compose(self, child: "AffineTransform") -> "AffineTransform":
        translated = _mat_vec(self.basis, child.origin)
        return AffineTransform(
            basis=_mat_mul(self.basis, child.basis),
            origin=tuple(self.origin[i] + translated[i] for i in range(3)),  # type: ignore[arg-type]
        )

    def serialize(self, *, decimals: int = 9) -> dict[str, list[Any]]:
        def clean(value: float) -> float:
            rounded = round(value, decimals)
            return 0.0 if rounded == 0.0 else rounded

        return {
            "basis": [[clean(item) for item in row] for row in self.basis],
            "origin": [clean(item) for item in self.origin],
        }


def properties_by_name(export: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for prop in export.get("props", []):
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("name", "")).strip()
        if name:
            result[name] = prop.get("value")
    return result


def prefab_name_from_raw_record(record: dict[str, Any]) -> str:
    trailer = record.get("trailer_entry", {})
    trailer_name = str(trailer.get("name", "")).strip()
    suffix = "_binaryprefab.ubc"
    if not trailer_name.lower().endswith(suffix):
        raise ValueError(
            f"SGO record {record.get('index', '?')} has unsupported trailer name "
            f"{trailer_name!r}"
        )
    name = trailer_name[: -len(suffix)]
    if not name:
        raise ValueError(f"SGO record {record.get('index', '?')} has an empty prefab name")
    return name


def compact_compound_refs(record: dict[str, Any]) -> list[dict[str, Any]]:
    prefab_name = prefab_name_from_raw_record(record)
    refs: list[dict[str, Any]] = []
    for export in record.get("exports", []):
        if str(export.get("class", "")) != "CompoundObject":
            continue
        props = properties_by_name(export)
        sub_prefab = str(props.get("PrefabName", "")).strip()
        if not sub_prefab:
            raise ValueError(
                f"{prefab_name}/{export.get('name', '<unnamed>')} has no PrefabName"
            )
        try:
            compound_type = int(props.get("m_CompoundObjectType", 0))
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{prefab_name}/{export.get('name', '<unnamed>')} has invalid "
                "m_CompoundObjectType"
            ) from error
        object_name = str(export.get("name", "")).strip()
        path = source_component_path(prefab_name, "CompoundObject", object_name)
        transform = AffineTransform.from_properties(props)
        refs.append(
            {
                "class": "CompoundObject",
                "compound_type": compound_type,
                "name": object_name,
                "source_component_path": path,
                "sub_prefab": sub_prefab,
                "transform": transform.serialize(),
                "_transform": transform,
            }
        )
    refs.sort(key=lambda value: value["source_component_path"])
    return refs


def prefab_closure(
    roots: Iterable[str],
    prefabs: dict[str, dict[str, Any]],
    *,
    by_fold: dict[str, str] | None = None,
) -> set[str]:
    by_fold = by_fold or {name.casefold(): name for name in prefabs}
    closure: set[str] = set()
    pending = sorted(set(str(root) for root in roots), reverse=True)
    while pending:
        requested = pending.pop()
        resolved = by_fold.get(requested.casefold())
        if resolved is None:
            raise ValueError(f"referenced SGO prefab is missing: {requested}")
        if resolved in closure:
            continue
        closure.add(resolved)
        for ref in prefabs[resolved].get("compound_refs", []):
            child = str(ref["sub_prefab"])
            child_resolved = by_fold.get(child.casefold())
            if child_resolved is None:
                raise ValueError(f"{resolved} references missing SGO prefab {child}")
            if child_resolved not in closure:
                pending.append(child_resolved)
    return closure


def root_has_room(
    root: str,
    prefabs: dict[str, dict[str, Any]],
    *,
    by_fold: dict[str, str] | None = None,
    result_cache: dict[str, bool] | None = None,
) -> bool:
    by_fold = by_fold or {name.casefold(): name for name in prefabs}
    result_cache = result_cache if result_cache is not None else {}

    def visit(name: str, ancestors: frozenset[str], depth: int) -> bool:
        if depth > MAX_PREFAB_DEPTH:
            raise ValueError(f"prefab graph exceeds {MAX_PREFAB_DEPTH} levels at {name}")
        resolved = by_fold.get(name.casefold())
        if resolved is None:
            raise ValueError(f"referenced SGO prefab is missing: {name}")
        folded = resolved.casefold()
        if folded in result_cache:
            return result_cache[folded]
        if folded in ancestors:
            raise ValueError(f"cycle in SGO prefab graph at {resolved}")
        next_ancestors = ancestors | {folded}
        for ref in prefabs[resolved].get("compound_refs", []):
            if int(ref.get("compound_type", 0)) == ROOM_COMPOUND_TYPE:
                result_cache[folded] = True
                return True
            if visit(str(ref["sub_prefab"]), next_ancestors, depth + 1):
                result_cache[folded] = True
                return True
        result_cache[folded] = False
        return False

    return visit(root, frozenset(), 0)


def discover_rooms(
    root: str,
    prefabs: dict[str, dict[str, Any]],
    *,
    by_fold: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return each authored room reference instantiated below ``root``."""

    by_fold = by_fold or {name.casefold(): name for name in prefabs}
    rooms: list[dict[str, Any]] = []

    def visit(
        requested: str,
        transform: AffineTransform,
        resolved_path: str | None,
        ancestors: frozenset[str],
        depth: int,
    ) -> None:
        if depth > MAX_PREFAB_DEPTH:
            raise ValueError(f"prefab graph exceeds {MAX_PREFAB_DEPTH} levels at {requested}")
        name = by_fold.get(requested.casefold())
        if name is None:
            raise ValueError(f"referenced SGO prefab is missing: {requested}")
        folded = name.casefold()
        if folded in ancestors:
            raise ValueError(f"cycle in SGO prefab graph at {name}")
        next_ancestors = ancestors | {folded}
        for ref in prefabs[name].get("compound_refs", []):
            ref_transform = ref.get("_transform")
            if not isinstance(ref_transform, AffineTransform):
                raise ValueError(f"{name} compound ref lacks an affine transform")
            child_transform = transform.compose(ref_transform)
            direct_path = str(ref["source_component_path"])
            child_path = direct_path if resolved_path is None else nested_source_path(resolved_path, direct_path)
            child_name = by_fold.get(str(ref["sub_prefab"]).casefold())
            if child_name is None:
                raise ValueError(f"{name} references missing SGO prefab {ref['sub_prefab']}")
            if int(ref.get("compound_type", 0)) == ROOM_COMPOUND_TYPE:
                room_identity = {
                    "root_prefab": root,
                    "source_component_path": child_path,
                    "source_room_prefab": child_name,
                }
                rooms.append(
                    {
                        "room_id": sha256_id("room", room_identity),
                        "source_component_path": child_path,
                        "source_room_prefab": child_name,
                        "source_room_prefab_id": source_prefab_id(child_name),
                        "transform": child_transform.serialize(),
                        "_transform": child_transform,
                    }
                )
            visit(
                child_name,
                child_transform,
                child_path,
                next_ancestors,
                depth + 1,
            )

    visit(root, AffineTransform.identity(), None, frozenset(), 0)
    rooms.sort(key=lambda value: value["source_component_path"])
    paths = [str(room["source_component_path"]) for room in rooms]
    if len(paths) != len(set(paths)):
        raise ValueError(f"duplicate room source paths below {root}")
    return rooms


def walk_room_actors(
    room: dict[str, Any],
    prefabs: dict[str, dict[str, Any]],
    *,
    by_fold: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield resolved actors owned by one room, stopping at nested room refs."""

    by_fold = by_fold or {name.casefold(): name for name in prefabs}
    room_transform = room.get("_transform")
    if not isinstance(room_transform, AffineTransform):
        raise ValueError(f"room {room.get('room_id', '?')} lacks an affine transform")

    def visit(
        requested: str,
        transform: AffineTransform,
        resolved_path: str,
        ancestors: frozenset[str],
        depth: int,
    ) -> Iterator[dict[str, Any]]:
        if depth > MAX_PREFAB_DEPTH:
            raise ValueError(f"prefab graph exceeds {MAX_PREFAB_DEPTH} levels at {requested}")
        name = by_fold.get(requested.casefold())
        if name is None:
            raise ValueError(f"referenced SGO prefab is missing: {requested}")
        folded = name.casefold()
        if folded in ancestors:
            raise ValueError(f"cycle in SGO prefab graph at {name}")
        next_ancestors = ancestors | {folded}

        for actor in prefabs[name].get("actors", []):
            actor_transform = actor.get("_transform")
            if not isinstance(actor_transform, AffineTransform):
                raise ValueError(f"{name} actor lacks an affine transform")
            direct_path = str(actor["source_component_path"])
            output = {key: value for key, value in actor.items() if not key.startswith("_")}
            output["source_component_path"] = nested_source_path(resolved_path, direct_path)
            output["transform"] = transform.compose(actor_transform).serialize()
            output["_transform"] = transform.compose(actor_transform)
            yield output

        for ref in prefabs[name].get("compound_refs", []):
            if int(ref.get("compound_type", 0)) == ROOM_COMPOUND_TYPE:
                continue
            ref_transform = ref.get("_transform")
            if not isinstance(ref_transform, AffineTransform):
                raise ValueError(f"{name} compound ref lacks an affine transform")
            direct_path = str(ref["source_component_path"])
            child_path = nested_source_path(resolved_path, direct_path)
            yield from visit(
                str(ref["sub_prefab"]),
                transform.compose(ref_transform),
                child_path,
                next_ancestors,
                depth + 1,
            )

    yield from visit(
        str(room["source_room_prefab"]),
        room_transform,
        str(room["source_component_path"]),
        frozenset(),
        0,
    )


def walk_nonroom_actors(
    root: str,
    prefabs: dict[str, dict[str, Any]],
    *,
    by_fold: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield actors below a placed root while excluding every room subtree.

    Movers and entrance triggers are frequently siblings of the authored room
    references in a master-interior prefab.  They are template-level transition
    authority and must not be silently assigned to an arbitrary room.
    """

    by_fold = by_fold or {name.casefold(): name for name in prefabs}

    def visit(
        requested: str,
        transform: AffineTransform,
        resolved_path: str | None,
        ancestors: frozenset[str],
        depth: int,
    ) -> Iterator[dict[str, Any]]:
        if depth > MAX_PREFAB_DEPTH:
            raise ValueError(f"prefab graph exceeds {MAX_PREFAB_DEPTH} levels at {requested}")
        name = by_fold.get(requested.casefold())
        if name is None:
            raise ValueError(f"referenced SGO prefab is missing: {requested}")
        folded = name.casefold()
        if folded in ancestors:
            raise ValueError(f"cycle in SGO prefab graph at {name}")
        next_ancestors = ancestors | {folded}
        for actor in prefabs[name].get("actors", []):
            actor_transform = actor.get("_transform")
            if not isinstance(actor_transform, AffineTransform):
                raise ValueError(f"{name} actor lacks an affine transform")
            direct_path = str(actor["source_component_path"])
            actor_path = (
                direct_path
                if resolved_path is None
                else nested_source_path(resolved_path, direct_path)
            )
            output = {key: value for key, value in actor.items() if not key.startswith("_")}
            output["source_component_path"] = actor_path
            output["transform"] = transform.compose(actor_transform).serialize()
            output["_transform"] = transform.compose(actor_transform)
            yield output
        for ref in prefabs[name].get("compound_refs", []):
            if int(ref.get("compound_type", 0)) == ROOM_COMPOUND_TYPE:
                continue
            ref_transform = ref.get("_transform")
            if not isinstance(ref_transform, AffineTransform):
                raise ValueError(f"{name} compound ref lacks an affine transform")
            direct_path = str(ref["source_component_path"])
            child_path = (
                direct_path
                if resolved_path is None
                else nested_source_path(resolved_path, direct_path)
            )
            yield from visit(
                str(ref["sub_prefab"]),
                transform.compose(ref_transform),
                child_path,
                next_ancestors,
                depth + 1,
            )

    yield from visit(root, AffineTransform.identity(), None, frozenset(), 0)


def actor_category(class_name: object) -> str:
    text = str(class_name).casefold()
    if text == "portal":
        return "portals"
    if text == "staticmeshactor":
        return "visual_components"
    if "light" in text:
        return "lights"
    if "ambient" in text or "sound" in text or "audio" in text:
        return "audio"
    if "mover" in text or "door" in text:
        return "movers"
    if "trigger" in text or "volume" in text:
        return "triggers"
    return "deferred_components"


def assemble_portal_graph(rooms: list[dict[str, Any]]) -> dict[str, Any]:
    endpoints: list[dict[str, Any]] = []
    for room in rooms:
        for portal in room.get("portals", []):
            transform = portal.get("_transform")
            if not isinstance(transform, AffineTransform):
                raise ValueError(f"portal {portal.get('name', '?')} lacks an affine transform")
            identity = {
                "room_id": room["room_id"],
                "source_component_path": portal["source_component_path"],
            }
            endpoint = {key: value for key, value in portal.items() if not key.startswith("_")}
            endpoint.update(
                {
                    "endpoint_id": sha256_id("portal_endpoint", identity),
                    "room_id": room["room_id"],
                    "position": [
                        0.0 if round(value, PORTAL_POSITION_DECIMALS) == 0.0 else round(value, PORTAL_POSITION_DECIMALS)
                        for value in transform.origin
                    ],
                }
            )
            endpoints.append(endpoint)
    endpoints.sort(key=lambda value: value["endpoint_id"])

    groups: dict[tuple[float, float, float], list[dict[str, Any]]] = {}
    for endpoint in endpoints:
        key = tuple(float(value) for value in endpoint["position"])
        groups.setdefault(key, []).append(endpoint)

    connections: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for position in sorted(groups):
        group = sorted(groups[position], key=lambda value: value["endpoint_id"])
        room_ids = {str(value["room_id"]) for value in group}
        if len(group) == 2 and len(room_ids) == 2:
            endpoint_ids = [str(value["endpoint_id"]) for value in group]
            connection_identity = {"endpoint_ids": endpoint_ids, "position": list(position)}
            connections.append(
                {
                    "connection_id": sha256_id("portal", connection_identity),
                    "endpoint_ids": endpoint_ids,
                    "position": list(position),
                    "room_ids": sorted(room_ids),
                    "source_assembly_policy": "unique_coincident_authored_endpoints_v1",
                }
            )
        elif len(group) == 1:
            endpoint = group[0]
            boundaries.append(
                {
                    "boundary_id": sha256_id(
                        "portal_boundary", {"endpoint_id": endpoint["endpoint_id"]}
                    ),
                    "endpoint_id": endpoint["endpoint_id"],
                    "position": list(position),
                    "room_id": endpoint["room_id"],
                }
            )
        else:
            unresolved.append(
                {
                    "endpoint_ids": [value["endpoint_id"] for value in group],
                    "position": list(position),
                    "reason": (
                        "same_room_endpoint_pair"
                        if len(group) == 2
                        else "ambiguous_coincident_endpoint_cluster"
                    ),
                    "room_ids": sorted(room_ids),
                }
            )

    adjacency: dict[str, set[str]] = {str(room["room_id"]): set() for room in rooms}
    for connection in connections:
        left, right = connection["room_ids"]
        adjacency[left].add(right)
        adjacency[right].add(left)
    return {
        "adjacency": [
            {"room_id": room_id, "visible_room_ids": sorted(neighbors)}
            for room_id, neighbors in sorted(adjacency.items())
        ],
        "boundaries": boundaries,
        "connections": connections,
        "endpoints": [{key: value for key, value in endpoint.items() if not key.startswith("_")} for endpoint in endpoints],
        "pairing_policy": (
            "unique_cross_room_position_after_1e-6_source_unit_float_normalization_v1"
        ),
        "unresolved": unresolved,
    }


def strip_private(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_private(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [strip_private(item) for item in value]
    if isinstance(value, tuple):
        return [strip_private(item) for item in value]
    return value
