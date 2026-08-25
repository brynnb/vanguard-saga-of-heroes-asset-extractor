"""Exact authored portal-aperture geometry for world residency.

Vanguard SGO ``Portal`` actors reference ordinary extracted StaticMesh glTFs.
This module resolves those source identities without name-only guessing, reads
their indexed triangle geometry, converts it back to Vanguard coordinates, and
applies the complete nested SGO affine transform.  The resulting aperture is
therefore suitable for runtime portal clipping rather than merely adjacency.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any

from scripts.lib.world_residency_interiors import AffineTransform, sha256_id


_COMPONENT_FORMATS = {5121: "B", 5123: "H", 5125: "I", 5126: "f"}
_COMPONENT_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def _finite(value: object, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} is not finite")
    return result


def _read_buffer(document: dict[str, Any], path: Path, index: int) -> bytes:
    buffers = document.get("buffers", [])
    if not isinstance(buffers, list) or not 0 <= index < len(buffers):
        raise ValueError(f"{path}: invalid glTF buffer {index}")
    info = buffers[index]
    if not isinstance(info, dict):
        raise ValueError(f"{path}: glTF buffer {index} is invalid")
    uri = str(info.get("uri", ""))
    if uri.startswith("data:"):
        marker = ";base64,"
        if marker not in uri:
            raise ValueError(f"{path}: glTF buffer {index} is not base64 encoded")
        try:
            payload = base64.b64decode(uri.split(marker, 1)[1], validate=True)
        except ValueError as error:
            raise ValueError(f"{path}: glTF buffer {index} has invalid base64") from error
    else:
        relative = Path(uri)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"{path}: unsafe glTF buffer URI {uri!r}")
        payload = (path.parent / relative).read_bytes()
    expected = int(info.get("byteLength", -1))
    if expected < 0 or len(payload) < expected:
        raise ValueError(f"{path}: glTF buffer {index} length mismatch")
    return payload


def _read_accessor(
    document: dict[str, Any], buffers: list[bytes], path: Path, accessor_index: int
) -> list[tuple[float | int, ...]]:
    accessors = document.get("accessors", [])
    views = document.get("bufferViews", [])
    if not isinstance(accessors, list) or not 0 <= accessor_index < len(accessors):
        raise ValueError(f"{path}: invalid accessor {accessor_index}")
    accessor = accessors[accessor_index]
    if not isinstance(accessor, dict) or "sparse" in accessor:
        raise ValueError(f"{path}: unsupported accessor {accessor_index}")
    view_index = int(accessor.get("bufferView", -1))
    if not isinstance(views, list) or not 0 <= view_index < len(views):
        raise ValueError(f"{path}: invalid buffer view {view_index}")
    view = views[view_index]
    if not isinstance(view, dict):
        raise ValueError(f"{path}: invalid buffer view {view_index}")
    buffer_index = int(view.get("buffer", -1))
    if not 0 <= buffer_index < len(buffers):
        raise ValueError(f"{path}: invalid buffer reference {buffer_index}")
    component_type = int(accessor.get("componentType", 0))
    component_format = _COMPONENT_FORMATS.get(component_type)
    component_count = _COMPONENT_COUNTS.get(str(accessor.get("type", "")))
    count = int(accessor.get("count", -1))
    if component_format is None or component_count is None or count < 0:
        raise ValueError(f"{path}: unsupported accessor layout {accessor_index}")
    item_size = struct.calcsize("<" + component_format * component_count)
    stride = int(view.get("byteStride", item_size))
    if stride < item_size:
        raise ValueError(f"{path}: accessor {accessor_index} has a short stride")
    view_start = int(view.get("byteOffset", 0))
    view_length = int(view.get("byteLength", -1))
    accessor_offset = int(accessor.get("byteOffset", 0))
    if view_start < 0 or view_length < 0 or accessor_offset < 0:
        raise ValueError(f"{path}: accessor {accessor_index} has a negative byte range")
    start = view_start + accessor_offset
    end = start if count == 0 else start + (count - 1) * stride + item_size
    payload = buffers[buffer_index]
    if end > view_start + view_length or end > len(payload):
        raise ValueError(f"{path}: accessor {accessor_index} exceeds its buffer view")
    unpack = struct.Struct("<" + component_format * component_count).unpack_from
    return [unpack(payload, start + row * stride) for row in range(count)]


def _require_accessor_layout(
    document: dict[str, Any],
    path: Path,
    accessor_index: int,
    *,
    component_types: set[int],
    value_type: str,
) -> None:
    accessors = document.get("accessors", [])
    if not isinstance(accessors, list) or not 0 <= accessor_index < len(accessors):
        raise ValueError(f"{path}: invalid accessor {accessor_index}")
    accessor = accessors[accessor_index]
    if not isinstance(accessor, dict):
        raise ValueError(f"{path}: invalid accessor {accessor_index}")
    if (
        int(accessor.get("componentType", 0)) not in component_types
        or str(accessor.get("type", "")) != value_type
        or accessor.get("normalized") is True
    ):
        raise ValueError(f"{path}: accessor {accessor_index} has an invalid semantic layout")


def _source_position(gltf_position: tuple[float | int, ...]) -> tuple[float, float, float]:
    # StaticMesh extraction writes glTF=(-Vanguard.y, Vanguard.z, Vanguard.x).
    x, y, z = (_finite(value, "portal vertex") for value in gltf_position)
    return (z, -x, y)


def _bounds(positions: list[tuple[float, float, float]]) -> dict[str, list[float]]:
    if not positions:
        raise ValueError("portal aperture has no vertices")
    return {
        "minimum": [min(value[axis] for value in positions) for axis in range(3)],
        "maximum": [max(value[axis] for value in positions) for axis in range(3)],
    }


def _plane(
    positions: list[tuple[float, float, float]], indices: list[int]
) -> dict[str, Any]:
    normal: tuple[float, float, float] | None = None
    origin: tuple[float, float, float] | None = None
    for offset in range(0, len(indices), 3):
        a, b, c = (positions[indices[offset + value]] for value in range(3))
        ab = tuple(b[axis] - a[axis] for axis in range(3))
        ac = tuple(c[axis] - a[axis] for axis in range(3))
        candidate = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        length = math.sqrt(sum(value * value for value in candidate))
        if length > 1.0e-9:
            normal = tuple(value / length for value in candidate)
            origin = a
            break
    if normal is None or origin is None:
        raise ValueError("portal aperture has no non-degenerate triangle")
    distance = -sum(normal[axis] * origin[axis] for axis in range(3))
    maximum_error = max(
        abs(sum(normal[axis] * point[axis] for axis in range(3)) + distance)
        for point in positions
    )
    return {
        "equation": [*normal, distance],
        "maximum_vertex_distance": maximum_error,
        "planar": maximum_error <= 1.0e-4,
    }


class PortalApertureLibrary:
    """Resolve and cache exact portal StaticMesh geometry."""

    def __init__(self, mesh_root: Path) -> None:
        self.mesh_root = mesh_root.resolve()
        self._path_by_fold: dict[str, Path] | None = None
        self._mesh_by_path: dict[Path, dict[str, Any]] = {}

    def _index(self) -> dict[str, Path]:
        if self._path_by_fold is None:
            self._path_by_fold = {}
            for path in self.mesh_root.rglob("*.gltf"):
                relative = path.relative_to(self.mesh_root).as_posix()
                folded = relative.casefold()
                prior = self._path_by_fold.get(folded)
                if prior is not None and prior != path:
                    raise ValueError(f"case-insensitive portal mesh collision: {prior}, {path}")
                self._path_by_fold[folded] = path
        return self._path_by_fold

    def resolve(self, actor: dict[str, Any]) -> dict[str, Any]:
        source = actor.get("static_mesh_source")
        if not isinstance(source, dict):
            raise ValueError(
                f"portal {actor.get('source_component_path', actor.get('name'))} "
                "has no exact StaticMesh source identity"
            )
        package = str(source.get("source_package", "")).strip()
        object_name = str(source.get("name", actor.get("static_mesh", ""))).strip()
        if not package or not object_name:
            raise ValueError("portal StaticMesh source identity is incomplete")
        relative = Path(package) / f"{object_name}.gltf"
        path = self.mesh_root / relative
        if not path.is_file():
            path = self._index().get(relative.as_posix().casefold(), Path())
        if not path.is_file():
            raise ValueError(f"exact portal aperture mesh is absent: {relative.as_posix()}")
        return self._load(path, source)

    def _load(self, path: Path, source: dict[str, Any]) -> dict[str, Any]:
        cached = self._mesh_by_path.get(path)
        if cached is not None:
            return cached
        raw = path.read_bytes()
        document = json.loads(raw)
        buffers_info = document.get("buffers", [])
        if not isinstance(buffers_info, list):
            raise ValueError(f"{path}: glTF buffers are invalid")
        buffers = [_read_buffer(document, path, index) for index in range(len(buffers_info))]
        primitives: list[dict[str, Any]] = []
        all_positions: list[tuple[float, float, float]] = []
        meshes = document.get("meshes", [])
        if not isinstance(meshes, list):
            raise ValueError(f"{path}: glTF meshes are invalid")
        for mesh in meshes:
            if not isinstance(mesh, dict):
                continue
            for primitive in mesh.get("primitives", []):
                if not isinstance(primitive, dict) or int(primitive.get("mode", 4)) != 4:
                    raise ValueError(f"{path}: portal mesh has a non-triangle primitive")
                attributes = primitive.get("attributes", {})
                if not isinstance(attributes, dict) or "POSITION" not in attributes:
                    raise ValueError(f"{path}: portal primitive has no POSITION")
                position_accessor = int(attributes["POSITION"])
                _require_accessor_layout(
                    document,
                    path,
                    position_accessor,
                    component_types={5126},
                    value_type="VEC3",
                )
                positions = [
                    _source_position(value)
                    for value in _read_accessor(
                        document, buffers, path, position_accessor
                    )
                ]
                if "indices" not in primitive:
                    indices = list(range(len(positions)))
                else:
                    index_accessor = int(primitive["indices"])
                    _require_accessor_layout(
                        document,
                        path,
                        index_accessor,
                        component_types={5121, 5123, 5125},
                        value_type="SCALAR",
                    )
                    values = _read_accessor(document, buffers, path, index_accessor)
                    indices = [int(value[0]) for value in values]
                if len(indices) % 3 or any(value < 0 or value >= len(positions) for value in indices):
                    raise ValueError(f"{path}: portal primitive indices are invalid")
                if not positions or not indices:
                    raise ValueError(f"{path}: portal primitive is empty")
                primitives.append(
                    {
                        "indices": indices,
                        "positions": [list(value) for value in positions],
                    }
                )
                all_positions.extend(positions)
        if not primitives:
            raise ValueError(f"{path}: portal mesh has no triangle geometry")
        relative = path.relative_to(self.mesh_root).as_posix()
        identity = {
            "geometry_sha256": hashlib.sha256(
                json.dumps(primitives, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "source_name": source.get("name"),
            "source_object_path": source.get("object_path"),
            "source_package": source.get("source_package"),
        }
        cached = {
            "aperture_mesh_id": sha256_id("portal_aperture_mesh", identity),
            "bounds": _bounds(all_positions),
            "coordinate_space": "vanguard_staticmesh_local",
            "geometry_sha256": f"sha256:{identity['geometry_sha256']}",
            "primitives": primitives,
            "source": source,
            "source_gltf_bytes": len(raw),
            "source_gltf_relative_path": relative,
            "source_gltf_sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        }
        self._mesh_by_path[path] = cached
        return cached

    def transformed(self, actor: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        mesh = self.resolve(actor)
        transform = actor.get("_transform")
        if not isinstance(transform, AffineTransform):
            raise ValueError("portal actor lacks its resolved affine transform")
        primitives: list[dict[str, Any]] = []
        all_positions: list[tuple[float, float, float]] = []
        all_indices: list[int] = []
        index_base = 0
        for primitive in mesh["primitives"]:
            positions = [
                tuple(
                    transform.origin[row]
                    + sum(transform.basis[row][column] * float(value[column]) for column in range(3))
                    for row in range(3)
                )
                for value in primitive["positions"]
            ]
            indices = [int(value) for value in primitive["indices"]]
            primitives.append({"indices": indices, "positions": [list(value) for value in positions]})
            all_positions.extend(positions)
            all_indices.extend(index_base + value for value in indices)
            index_base += len(positions)
        aperture = {
            "aperture_mesh_id": mesh["aperture_mesh_id"],
            "bounds": _bounds(all_positions),
            "coordinate_space": "interior_template_vanguard_source",
            "plane": _plane(all_positions, all_indices),
            "primitives": primitives,
            "triangle_count": len(all_indices) // 3,
            "vertex_count": len(all_positions),
        }
        return mesh, aperture

    def catalog(self) -> list[dict[str, Any]]:
        return sorted(self._mesh_by_path.values(), key=lambda value: value["aperture_mesh_id"])
