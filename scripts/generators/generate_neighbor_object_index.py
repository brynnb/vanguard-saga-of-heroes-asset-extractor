#!/usr/bin/env python3
"""Build per-chunk indexes for distant neighbor object rendering."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from generate_godot_runtime_chunk import (
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_RUNTIME_ROOT,
    DEFAULT_STATIC_MESH_TAB,
    StaticMeshMetadata,
    is_hidden_sgo_component,
    normalize_chunk_name,
    read_json,
)


NEIGHBOR_OBJECT_MIN_CULL_DISTANCE = 100000.0
NEIGHBOR_OBJECT_MIN_RADIUS = 1000.0
NEIGHBOR_OBJECT_STRONG_CULL_DISTANCE = 409600.0
NEIGHBOR_OBJECT_STRONG_MIN_RADIUS = 450.0
NEIGHBOR_OBJECT_NO_CULL_MIN_RADIUS = 20000.0
NEIGHBOR_OBJECT_MAX_MESH_DETAIL_LEVEL = 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", action="append", default=[], help="Chunk to index.")
    parser.add_argument("--center", action="append", default=[], help="Generate neighbors around center chunk.")
    parser.add_argument("--radius", type=int, default=1, help="Neighbor radius for --center.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--static-mesh-tab", type=Path, default=DEFAULT_STATIC_MESH_TAB)
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden SGO components.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    chunks: list[str] = [normalize_chunk_name(chunk) for chunk in args.chunk]
    for center in args.center:
        chunks.extend(neighbor_chunk_names(normalize_chunk_name(center), args.radius))
    chunks = sorted(set(chunks))
    if not chunks:
        parser.error("provide at least one --chunk or --center")

    metadata = StaticMeshMetadata(args.static_mesh_tab)
    failures = 0
    for chunk in chunks:
        try:
            result = build_neighbor_object_index(
                chunk=chunk,
                output_root=args.output_root.resolve(),
                runtime_root=args.runtime_root.resolve(),
                metadata=metadata,
                include_hidden=args.include_hidden,
                dry_run=args.dry_run,
            )
            label = "DRY RUN" if args.dry_run else "WROTE"
            print(
                f"{label}: {chunk} neighbor candidates="
                f"{result['candidate_node_count']}/{result['total_node_count']} "
                f"components={result['candidate_component_count']} "
                f"meshes={result['candidate_mesh_count']} "
                f"index={result['index_path']}"
            )
        except Exception as exc:  # noqa: BLE001 - CLI should continue across chunks.
            failures += 1
            print(f"ERROR: {chunk}: {exc}", file=sys.stderr)
    return 1 if failures else 0


def build_neighbor_object_index(
    *,
    chunk: str,
    output_root: Path,
    runtime_root: Path,
    metadata: StaticMeshMetadata,
    include_hidden: bool,
    dry_run: bool,
) -> dict[str, Any]:
    terrain_root = output_root / "terrain/terrain_grid"
    objects_path = terrain_root / f"{chunk}_objects.gltf"
    sgo_path = terrain_root / f"{chunk}_sgo.json"
    if not objects_path.exists():
        raise FileNotFoundError(f"missing object placement file: {objects_path}")
    if not sgo_path.exists():
        raise FileNotFoundError(f"missing SGO manifest file: {sgo_path}")

    objects_data = read_json(objects_path)
    sgo_manifest = read_json(sgo_path)
    nodes = objects_data.get("nodes", [])
    if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
        raise ValueError(f"{chunk} object placement nodes are invalid")
    root_children = nodes[0].get("children", [])
    if not isinstance(root_children, list):
        raise ValueError(f"{chunk} root children are invalid")

    node_indices: list[int] = []
    component_indices_by_node: dict[str, list[int]] = {}
    candidate_meshes: set[str] = set()
    candidate_component_count = 0
    for node_index_value in root_children:
        node_index = int(node_index_value)
        if node_index < 0 or node_index >= len(nodes) or not isinstance(nodes[node_index], dict):
            continue
        node = nodes[node_index]
        extras = node.get("extras", {})
        if not isinstance(extras, dict):
            continue
        allowed_meshes, allowed_component_indices = node_neighbor_meshes(
            node=node,
            extras=extras,
            sgo_manifest=sgo_manifest,
            metadata=metadata,
            include_hidden=include_hidden,
        )
        if not allowed_meshes:
            continue
        node_indices.append(node_index)
        if allowed_component_indices:
            component_indices_by_node[str(node_index)] = allowed_component_indices
        candidate_component_count += len(allowed_meshes)
        candidate_meshes.update(allowed_meshes)

    index_path = runtime_root / "chunks" / chunk / "neighbor_objects.json"
    data = {
        "version": 1,
        "generated_by": "scripts/generators/generate_neighbor_object_index.py",
        "generated_at_unix": int(time.time()),
        "chunk": chunk,
        "objects_source_relative_path": str(objects_path.relative_to(output_root)),
        "sgo_source_relative_path": str(sgo_path.relative_to(output_root)),
        "filter": {
            "max_mesh_detail_level": NEIGHBOR_OBJECT_MAX_MESH_DETAIL_LEVEL,
            "min_cull_distance": NEIGHBOR_OBJECT_MIN_CULL_DISTANCE,
            "min_radius": NEIGHBOR_OBJECT_MIN_RADIUS,
            "strong_cull_distance": NEIGHBOR_OBJECT_STRONG_CULL_DISTANCE,
            "strong_min_radius": NEIGHBOR_OBJECT_STRONG_MIN_RADIUS,
            "no_cull_min_radius": NEIGHBOR_OBJECT_NO_CULL_MIN_RADIUS,
        },
        "total_node_count": len(root_children),
        "candidate_node_count": len(node_indices),
        "filtered_node_count": len(root_children) - len(node_indices),
        "candidate_component_count": candidate_component_count,
        "candidate_mesh_count": len(candidate_meshes),
        "candidate_meshes": sorted(candidate_meshes),
        "node_indices": node_indices,
        "component_indices_by_node": component_indices_by_node,
    }

    if not dry_run:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return {
        "chunk": chunk,
        "index_path": index_path,
        "total_node_count": len(root_children),
        "candidate_node_count": len(node_indices),
        "candidate_component_count": candidate_component_count,
        "candidate_mesh_count": len(candidate_meshes),
    }


def node_neighbor_meshes(
    *,
    node: dict[str, Any],
    extras: dict[str, Any],
    sgo_manifest: dict[str, Any],
    metadata: StaticMeshMetadata,
    include_hidden: bool,
) -> tuple[list[str], list[int]]:
    allowed: list[str] = []
    allowed_component_indices: list[int] = []
    prefab_name = str(extras.get("prefab_name", "")).strip()
    prefab = sgo_manifest.get(prefab_name, {}) if prefab_name else {}
    components = prefab.get("components", []) if isinstance(prefab, dict) else []
    if isinstance(components, list) and components:
        for component_index, component in enumerate(components):
            if not isinstance(component, dict):
                continue
            if not include_hidden and is_hidden_sgo_component(component):
                continue
            mesh_path = str(component.get("mesh_path", "")).strip()
            mesh_name = str(component.get("mesh_name", "")).strip()
            if mesh_path and allows_neighbor_visual(metadata, mesh_path, str(node.get("name", "")), mesh_name):
                allowed.append(mesh_path)
                allowed_component_indices.append(component_index)
        return allowed, allowed_component_indices

    mesh_path = str(extras.get("mesh_path", "")).strip()
    mesh_name = str(extras.get("mesh_ref", "")).strip()
    if mesh_path and allows_neighbor_visual(metadata, mesh_path, str(node.get("name", "")), mesh_name):
        allowed.append(mesh_path)
    return allowed, allowed_component_indices


def allows_neighbor_visual(
    metadata: StaticMeshMetadata, mesh_path: str, _object_name: str, mesh_name: str = ""
) -> bool:
    entry = metadata.lookup(mesh_path, mesh_name)
    if not entry:
        return False

    detail = int(entry.get("mesh_detail_level", 0))
    if detail > NEIGHBOR_OBJECT_MAX_MESH_DETAIL_LEVEL:
        return False

    cull_distance = float(entry.get("cull_distance", 0.0))
    radius = float(entry.get("sphere_radius", 0.0))
    if cull_distance >= NEIGHBOR_OBJECT_STRONG_CULL_DISTANCE:
        return radius >= NEIGHBOR_OBJECT_STRONG_MIN_RADIUS
    if cull_distance >= NEIGHBOR_OBJECT_MIN_CULL_DISTANCE:
        return radius >= NEIGHBOR_OBJECT_MIN_RADIUS
    if abs(cull_distance) <= 0.000001:
        return radius >= NEIGHBOR_OBJECT_NO_CULL_MIN_RADIUS
    return False


def neighbor_chunk_names(chunk_name: str, radius: int) -> list[str]:
    x, y = chunk_coords(chunk_name)
    safe_radius = max(1, radius)
    names: list[str] = []
    for dx in range(-safe_radius, safe_radius + 1):
        for dy in range(-safe_radius, safe_radius + 1):
            if dx == 0 and dy == 0:
                continue
            names.append(chunk_name_from_coords(x + dx, y + dy))
    return names


def chunk_coords(chunk_name: str) -> tuple[int, int]:
    normalized = normalize_chunk_name(chunk_name)
    parts = normalized.removeprefix("chunk_").split("_")
    if len(parts) != 2:
        raise ValueError(f"invalid chunk name: {chunk_name}")
    return parse_chunk_coord(parts[0]), parse_chunk_coord(parts[1])


def parse_chunk_coord(value: str) -> int:
    return -int(value[1:]) if value.startswith("n") else int(value)


def chunk_name_from_coords(x: int, y: int) -> str:
    return f"chunk_{format_chunk_coord(x)}_{format_chunk_coord(y)}"


def format_chunk_coord(value: int) -> str:
    return f"n{abs(value)}" if value < 0 else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
