"""SpeedTree-specific validation for Vanguard StaticMesh exports.

Vanguard embeds SpeedTree RT payloads beside a UE2 StaticMesh approximation.
The approximation contains collapsed leaf-card vertices and, in many assets,
uninitialized tangent memory.  Keep these rules separate from the generic
StaticMesh exporter so ordinary artist-authored tree meshes are unaffected.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


SPEEDTREE_PAYLOAD_MARKERS = (b"__IdvSpt", b"SpeedTree")


def has_embedded_speedtree_payload(export_data: bytes) -> bool:
    """Return whether an export carries an actual SpeedTree RT payload."""
    return all(marker in export_data for marker in SPEEDTREE_PAYLOAD_MARKERS)


def tangent_stream_is_usable(
    tangents: Sequence[tuple[float, float, float]] | None,
    normals: Sequence[tuple[float, float, float]],
) -> bool:
    """Reject corrupt or non-tangent vectors rather than normalizing garbage.

    Valid Vanguard tangent streams are approximately unit length (some reach
    sqrt(2)) and approximately perpendicular to the corresponding normal.
    SpeedTree's uninitialized 0xCCCCCCCC values decode to roughly 1.07e8.
    """
    if not tangents or len(tangents) != len(normals):
        return False

    for tangent, normal in zip(tangents, normals):
        if not all(math.isfinite(value) for value in (*tangent, *normal)):
            return False
        tangent_length = math.sqrt(sum(value * value for value in tangent))
        normal_length = math.sqrt(sum(value * value for value in normal))
        if tangent_length < 0.25 or tangent_length > 4.0 or normal_length < 1.0e-6:
            return False
        cosine = abs(
            sum(a * b for a, b in zip(tangent, normal))
            / (tangent_length * normal_length)
        )
        if cosine > 0.35:
            return False
    return True


def triangle_is_degenerate(
    vertices: Sequence[object],
    a: int,
    b: int,
    c: int,
    *,
    relative_epsilon: float = 1.0e-12,
) -> bool:
    """Return whether a triangle has duplicate indices or negligible area."""
    if a == b or b == c or a == c:
        return True
    va, vb, vc = vertices[a], vertices[b], vertices[c]
    ab = (vb.x - va.x, vb.y - va.y, vb.z - va.z)
    ac = (vc.x - va.x, vc.y - va.y, vc.z - va.z)
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    cross_sq = sum(value * value for value in cross)
    edge_scale_sq = max(
        sum(value * value for value in ab),
        sum(value * value for value in ac),
        1.0,
    )
    return cross_sq <= relative_epsilon * edge_scale_sq * edge_scale_sq


def collapsed_leaf_section(
    vertices: Sequence[object],
    indices: Sequence[int],
    *,
    minimum_degenerate_fraction: float = 0.95,
) -> bool:
    """Identify the collapsed leaf-card primitive in a SpeedTree approximation."""
    triangle_count = len(indices) // 3
    if triangle_count < 2 or len(indices) % 3:
        return False
    degenerate_count = sum(
        triangle_is_degenerate(vertices, *indices[offset : offset + 3])
        for offset in range(0, len(indices), 3)
    )
    return degenerate_count / triangle_count >= minimum_degenerate_fraction


def discard_degenerate_triangles(
    vertices: Sequence[object], indices: Sequence[int]
) -> list[int]:
    """Remove zero-area triangles after UE2 strip decoding."""
    if len(indices) % 3:
        raise ValueError(f"triangle index count is not divisible by three: {len(indices)}")
    result: list[int] = []
    for offset in range(0, len(indices), 3):
        triangle = indices[offset : offset + 3]
        if not triangle_is_degenerate(vertices, *triangle):
            result.extend(triangle)
    return result
