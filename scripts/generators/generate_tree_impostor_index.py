#!/usr/bin/env python3
"""Build per-chunk indexes for runtime tree impostor placement."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from generate_godot_runtime_chunk import (
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_RUNTIME_ROOT,
    normalize_chunk_name,
    read_json,
)
from generate_neighbor_object_index import neighbor_chunk_names


DEFAULT_TREE_IMPOSTOR_DATA = DEFAULT_OUTPUT_ROOT / "data/tree_impostors.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", action="append", default=[], help="Chunk to index.")
    parser.add_argument("--center", action="append", default=[], help="Generate neighbors around center chunk.")
    parser.add_argument("--radius", type=int, default=1, help="Neighbor radius for --center.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--tree-impostors-json", type=Path, default=DEFAULT_TREE_IMPOSTOR_DATA)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    chunks: list[str] = [normalize_chunk_name(chunk) for chunk in args.chunk]
    for center in args.center:
        chunks.extend(neighbor_chunk_names(normalize_chunk_name(center), args.radius))
    chunks = sorted(set(chunks))
    if not chunks:
        parser.error("provide at least one --chunk or --center")

    impostor_meshes = load_impostor_meshes(args.tree_impostors_json.resolve())
    failures = 0
    for chunk in chunks:
        try:
            result = build_tree_impostor_index(
                chunk=chunk,
                output_root=args.output_root.resolve(),
                runtime_root=args.runtime_root.resolve(),
                impostor_meshes=impostor_meshes,
                dry_run=args.dry_run,
            )
            label = "DRY RUN" if args.dry_run else "WROTE"
            print(
                f"{label}: {chunk} tree impostors="
                f"{result['billboard_count']} nodes={result['candidate_node_count']}/"
                f"{result['total_node_count']} meshes={result['candidate_mesh_count']} "
                f"index={result['index_path']}"
            )
        except Exception as exc:  # noqa: BLE001 - CLI should continue across chunks.
            failures += 1
            print(f"ERROR: {chunk}: {exc}", file=sys.stderr)
    return 1 if failures else 0


def load_impostor_meshes(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"missing tree impostor data: {path}")
    data = read_json(path)
    meshes = data.get("meshes", {})
    if not isinstance(meshes, dict):
        raise ValueError(f"tree impostor data has no meshes dictionary: {path}")
    return {Path(str(mesh_name)).stem.lower() for mesh_name in meshes.keys()}


def build_tree_impostor_index(
    *,
    chunk: str,
    output_root: Path,
    runtime_root: Path,
    impostor_meshes: set[str],
    dry_run: bool,
) -> dict[str, Any]:
    terrain_root = output_root / "terrain/terrain_grid"
    objects_path = terrain_root / f"{chunk}_objects.gltf"
    if not objects_path.exists():
        raise FileNotFoundError(f"missing object placement file: {objects_path}")

    objects_data = read_json(objects_path)
    nodes = objects_data.get("nodes", [])
    if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
        raise ValueError(f"{chunk} object placement nodes are invalid")
    root_children = nodes[0].get("children", [])
    if not isinstance(root_children, list):
        raise ValueError(f"{chunk} root children are invalid")

    instances_by_mesh: dict[str, list[dict[str, Any]]] = {}
    candidate_node_count = 0
    for node_index_value in root_children:
        node_index = int(node_index_value)
        if node_index < 0 or node_index >= len(nodes) or not isinstance(nodes[node_index], dict):
            continue
        node = nodes[node_index]
        extras = node.get("extras", {})
        if not isinstance(extras, dict):
            continue
        if str(extras.get("class", "")) != "DecoInstance":
            continue
        mesh_path = str(extras.get("mesh_path", "")).strip()
        if not mesh_path or not has_impostor(mesh_path, impostor_meshes):
            continue
        instance = instance_from_node(node)
        if not instance:
            continue
        instances_by_mesh.setdefault(mesh_path, []).append(instance)
        candidate_node_count += 1

    index_path = runtime_root / "chunks" / chunk / "tree_impostors.json"
    data = {
        "version": 1,
        "generated_by": "scripts/generators/generate_tree_impostor_index.py",
        "generated_at_unix": int(time.time()),
        "chunk": chunk,
        "objects_source_relative_path": str(objects_path.relative_to(output_root)),
        "total_node_count": len(root_children),
        "candidate_node_count": candidate_node_count,
        "candidate_mesh_count": len(instances_by_mesh),
        "billboard_count": sum(len(instances) for instances in instances_by_mesh.values()),
        "instances_by_mesh": {
            mesh_path: instances for mesh_path, instances in sorted(instances_by_mesh.items())
        },
    }

    if not dry_run:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return {
        "chunk": chunk,
        "index_path": index_path,
        "total_node_count": len(root_children),
        "candidate_node_count": candidate_node_count,
        "candidate_mesh_count": len(instances_by_mesh),
        "billboard_count": data["billboard_count"],
    }


def has_impostor(mesh_path: str, impostor_meshes: set[str]) -> bool:
    return Path(mesh_path).stem.lower() in impostor_meshes


def instance_from_node(node: dict[str, Any]) -> dict[str, Any]:
    translation = node.get("translation", [0.0, 0.0, 0.0])
    if not isinstance(translation, list) or len(translation) < 3:
        return {}
    position = [float(translation[0]), float(translation[1]), float(translation[2])]

    yaw = 0.0
    rotation = node.get("rotation")
    if isinstance(rotation, list) and len(rotation) == 4:
        yaw = quaternion_yaw(float(rotation[0]), float(rotation[1]), float(rotation[2]), float(rotation[3]))

    scale = 1.0
    scale_data = node.get("scale")
    if isinstance(scale_data, list) and len(scale_data) >= 3:
        scale = (float(scale_data[0]) + float(scale_data[1]) + float(scale_data[2])) / 3.0

    return {"position": position, "yaw": yaw, "scale": scale}


def quaternion_yaw(x: float, y: float, z: float, w: float) -> float:
    sin_y = 2.0 * (w * y + x * z)
    cos_y = 1.0 - 2.0 * (y * y + x * x)
    return math.atan2(sin_y, cos_y)


if __name__ == "__main__":
    raise SystemExit(main())
