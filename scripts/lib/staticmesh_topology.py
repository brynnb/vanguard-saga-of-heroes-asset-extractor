"""Authoritative UE2 static-mesh section topology helpers.

UE2 stores an explicit primitive type and primitive count for each static-mesh
section.  Do not infer triangle lists versus strips from vertex coverage: that
can turn a valid triangle count into a non-triangular glTF index accessor.
"""

from collections.abc import Mapping, Sequence


class StaticMeshTopologyError(ValueError):
    """Raised when a serialized section cannot describe valid triangles."""


def _nonnegative_int(section: Mapping[str, object], key: str) -> int:
    value = int(section.get(key, 0) or 0)
    if value < 0:
        raise StaticMeshTopologyError(f"section {key} must be non-negative, got {value}")
    return value


def section_raw_index_count(section: Mapping[str, object]) -> int:
    """Return the number of serialized indices consumed by a UE2 section."""
    primitives = _nonnegative_int(section, "num_primitives")
    if primitives == 0:
        return 0
    if bool(section.get("is_strip", False)):
        return primitives + 2
    return primitives * 3


def section_triangle_indices(
    all_indices: Sequence[int],
    section: Mapping[str, object],
    *,
    vertex_count: int | None = None,
) -> list[int]:
    """Decode one UE2 section into a strict glTF triangle-list index array."""
    first_index = _nonnegative_int(section, "first_index")
    index_count = section_raw_index_count(section)
    end_index = first_index + index_count
    if end_index > len(all_indices):
        raise StaticMeshTopologyError(
            "section index range exceeds the shared index buffer: "
            f"first={first_index} count={index_count} buffer={len(all_indices)}"
        )

    raw = [int(value) for value in all_indices[first_index:end_index]]
    if vertex_count is not None:
        invalid = next((value for value in raw if value < 0 or value >= vertex_count), None)
        if invalid is not None:
            raise StaticMeshTopologyError(
                f"section index {invalid} exceeds vertex count {vertex_count}"
            )

    if bool(section.get("is_strip", False)):
        triangles: list[int] = []
        for offset in range(max(0, len(raw) - 2)):
            a, b, c = raw[offset : offset + 3]
            if a == b or b == c or a == c:
                continue
            triangles.extend((a, b, c) if offset % 2 == 0 else (a, c, b))
    else:
        triangles = raw

    if len(triangles) % 3 != 0:
        raise StaticMeshTopologyError(
            f"decoded triangle list has {len(triangles)} indices"
        )

    # NumTriangles is authored/source triangle metadata in Vanguard and can be
    # larger than the optimized render stream. NumPrimitives is authoritative
    # for index consumption, exactly as the UE2 renderer's DrawPrimitive call
    # uses it. In strips it also includes degenerate connector primitives.
    return triangles
