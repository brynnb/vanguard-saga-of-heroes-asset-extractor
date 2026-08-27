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

from scripts.generators.generate_godot_runtime_chunk import (
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_RUNTIME_ROOT,
    normalize_chunk_name,
    read_json,
)
from scripts.generators.generate_neighbor_object_index import neighbor_chunk_names


DEFAULT_TREE_IMPOSTOR_DATA = DEFAULT_OUTPUT_ROOT / "data/tree_impostors.json"
CONTENT_FILTER = "real_trees_only"
GLOBAL_TREE_IMPOSTOR_INDEX_VERSION = 2
CHUNK_TREE_IMPOSTOR_INDEX_VERSION = 2
DEFAULT_CELL_SIZE = 24000.0
TERRAIN_CHUNK_WORLD_SIZE = 204400.0
UNDERSTORY_NAME_TOKENS = (
    "brush",
    "bush",
    "cattail",
    "corn",
    "elephantear",
    "fern",
    "flower",
    "grass",
    "hangingpod",
    "lakeplant",
    "mushroom",
    "seaweed",
    "seaweeed",
    "seedling",
    "shortpalm",
    "shrub",
)
IMPOSTOR_CANDIDATE_TOKENS = (
    "speedtree",
    "tree",
    "oak",
    "fir",
    "spruce",
    "palm",
    "maple",
    "birch",
    "aspen",
    "baobab",
    "hathor",
    "qurxatree",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", action="append", default=[], help="Chunk to index.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Generate every chunk with object_cells.json or object placement glTF data.",
    )
    parser.add_argument("--center", action="append", default=[], help="Generate neighbors around center chunk.")
    parser.add_argument("--radius", type=int, default=1, help="Neighbor radius for --center.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--tree-impostors-json", type=Path, default=DEFAULT_TREE_IMPOSTOR_DATA)
    parser.add_argument("--cell-size", type=float, default=DEFAULT_CELL_SIZE)
    parser.add_argument(
        "--write-global-index",
        action="store_true",
        help="Write a global tree-billboard cell index for the selected chunks.",
    )
    parser.add_argument(
        "--global-index-path",
        type=Path,
        default=None,
        help="Override global tree-billboard index path. Defaults to <runtime-root>/global_tree_impostors.json.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    chunks: list[str] = [normalize_chunk_name(chunk) for chunk in args.chunk]
    if args.all:
        chunks.extend(
            discover_chunks_for_tree_impostors(
                runtime_root=args.runtime_root.resolve(),
                output_root=args.output_root.resolve(),
            )
        )
    for center in args.center:
        chunks.extend(neighbor_chunk_names(normalize_chunk_name(center), args.radius))
    chunks = sorted(set(chunks))
    if not chunks:
        parser.error("provide at least one --chunk, --center, or --all")

    impostor_meshes = load_impostor_meshes(args.tree_impostors_json.resolve())
    failures = 0
    if args.write_global_index:
        try:
            result = write_global_tree_impostor_index(
                chunks=chunks,
                output_root=args.output_root.resolve(),
                runtime_root=args.runtime_root.resolve(),
                impostor_meshes=impostor_meshes,
                index_path=args.global_index_path,
                cell_size=float(args.cell_size),
                dry_run=args.dry_run,
            )
            label = "DRY RUN" if args.dry_run else "WROTE"
            print(
                f"{label}: global_tree_impostors={result['cell_count']} "
                f"chunks={result['chunk_count']} missing={result['missing_chunk_count']} "
                f"billboards={result['billboard_count']} "
                f"excluded_understory={result['excluded_understory_count']} "
                f"missing_impostors={result['missing_impostor_count']} "
                f"index={result['index_path']}"
            )
        except Exception as exc:  # noqa: BLE001 - CLI should continue across chunks.
            failures = 1
            print(f"ERROR: global tree impostors: {exc}", file=sys.stderr)
        if failures == 0:
            cleanup_result = delete_stale_chunk_tree_impostor_indexes(
                chunks=chunks,
                runtime_root=args.runtime_root.resolve(),
                dry_run=args.dry_run,
            )
            label = "DRY RUN" if args.dry_run else "DELETED"
            print(
                f"{label}: stale per-chunk tree_impostors={cleanup_result['deleted_count']} "
                f"missing={cleanup_result['missing_count']}"
            )
    else:
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


def discover_chunks_for_tree_impostors(runtime_root: Path, output_root: Path) -> list[str]:
    chunks_root = runtime_root / "chunks"
    chunks: list[str] = []
    if chunks_root.exists():
        chunks = sorted(
            path.parent.name
            for path in chunks_root.glob("chunk_*/object_cells.json")
            if path.is_file()
        )
    if chunks:
        return chunks

    terrain_root = output_root / "terrain/terrain_grid"
    return sorted(
        path.name.removesuffix("_objects.gltf")
        for path in terrain_root.glob("chunk_*_objects.gltf")
    )


def load_impostor_meshes(path: Path) -> set[str]:
    if not path.exists():
        raise FileNotFoundError(f"missing tree impostor data: {path}")
    data = read_json(path)
    meshes = data.get("meshes", {})
    if not isinstance(meshes, dict):
        raise ValueError(f"tree impostor data has no meshes dictionary: {path}")
    require_generated_entries = is_generated_billboard_manifest(data)
    keys: set[str] = set()
    for mesh_name, entry in meshes.items():
        if require_generated_entries and not is_renderable_billboard_entry(entry):
            continue
        source_mesh_path = ""
        if isinstance(entry, dict):
            source_mesh_path = normalize_mesh_path(str(entry.get("source_mesh_path", "")))
        if is_understory_mesh_path(str(mesh_name), source_mesh_path):
            continue
        name_stem = Path(str(mesh_name)).stem.lower()
        if name_stem:
            keys.add(name_stem)
        if isinstance(entry, dict):
            if source_mesh_path:
                keys.add(source_mesh_path)
                keys.add(Path(source_mesh_path).stem.lower())
    return keys


def is_generated_billboard_manifest(data: dict[str, Any]) -> bool:
    generated_by = str(data.get("generated_by", ""))
    meta = data.get("meta", {})
    meta_source = str(meta.get("source", "")) if isinstance(meta, dict) else ""
    return (
        json_int(data.get("version", 0)) >= 3
        or "generate_tree_billboards.gd" in generated_by
        or meta_source == "godot-tree-billboard-generator"
    )


def is_renderable_billboard_entry(entry: Any) -> bool:
    return is_renderable_generated_billboard_entry(
        entry
    ) or is_renderable_extracted_billboard_entry(entry)


def is_renderable_generated_billboard_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    atlas = str(entry.get("atlas", "")).replace("\\", "/")
    return (
        str(entry.get("generated_from", "")) == "godot-tree-billboard-generator"
        and "generated_tree_billboards/" in atlas
    )


def is_renderable_extracted_billboard_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    atlas = str(entry.get("atlas", "")).replace("\\", "/").strip()
    return (
        str(entry.get("fallback_impostor_source", "")) == "extracted_billboard_atlas"
        and bool(str(entry.get("source_mesh_path", "")).strip())
        and bool(atlas)
        and "generated_tree_billboards/" not in atlas
    )


def json_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
    candidate_record_count = 0
    excluded_understory_count = 0
    missing_impostor_count = 0
    placement_source = "objects_gltf"
    object_cells_path = runtime_root / "chunks" / chunk / "object_cells.json"
    if object_cells_path.exists():
        cell_result = collect_instances_from_object_cells(object_cells_path, impostor_meshes)
        instances_by_mesh = cell_result["instances_by_mesh"]
        candidate_node_count = int(cell_result["candidate_node_count"])
        candidate_record_count = int(cell_result["candidate_record_count"])
        excluded_understory_count = int(cell_result["excluded_understory_count"])
        missing_impostor_count = int(cell_result["missing_impostor_count"])
        placement_source = "object_cells"
    else:
        raw_result = collect_instances_from_objects_gltf(nodes, root_children, impostor_meshes)
        instances_by_mesh = raw_result["instances_by_mesh"]
        candidate_node_count = int(raw_result["candidate_node_count"])
        candidate_record_count = int(raw_result["candidate_record_count"])
        excluded_understory_count = int(raw_result["excluded_understory_count"])
        missing_impostor_count = int(raw_result["missing_impostor_count"])
        placement_source = "objects_gltf"

    index_path = runtime_root / "chunks" / chunk / "tree_impostors.json"
    data = {
        "version": CHUNK_TREE_IMPOSTOR_INDEX_VERSION,
        "generated_by": "scripts/generators/generate_tree_impostor_index.py",
        "generated_at_unix": int(time.time()),
        "content_filter": CONTENT_FILTER,
        "chunk": chunk,
        "objects_source_relative_path": str(objects_path.relative_to(output_root)),
        "placement_source": placement_source,
        "object_cells_source_relative_path": (
            str(object_cells_path.relative_to(runtime_root)) if object_cells_path.exists() else ""
        ),
        "total_node_count": len(root_children),
        "candidate_node_count": candidate_node_count,
        "candidate_record_count": candidate_record_count,
        "excluded_understory_count": excluded_understory_count,
        "missing_impostor_count": missing_impostor_count,
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
        "candidate_record_count": candidate_record_count,
        "excluded_understory_count": excluded_understory_count,
        "missing_impostor_count": missing_impostor_count,
        "candidate_mesh_count": len(instances_by_mesh),
        "billboard_count": data["billboard_count"],
        "instances_by_mesh": instances_by_mesh,
    }


def collect_instances_from_objects_gltf(
    nodes: list[Any],
    root_children: list[Any],
    impostor_meshes: set[str],
) -> dict[str, Any]:
    instances_by_mesh: dict[str, list[dict[str, Any]]] = {}
    candidate_node_count = 0
    excluded_understory_count = 0
    missing_impostor_count = 0
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
        mesh_name = str(extras.get("mesh_name", "")).strip()
        status = impostor_match_status(mesh_path, mesh_name, impostor_meshes)
        if status == "excluded_understory":
            excluded_understory_count += 1
            continue
        if status == "missing_impostor":
            missing_impostor_count += 1
            continue
        if status != "included":
            continue
        instance = instance_from_node(node)
        if not instance:
            continue
        instances_by_mesh.setdefault(mesh_path, []).append(instance)
        candidate_node_count += 1
    return {
        "instances_by_mesh": instances_by_mesh,
        "candidate_node_count": candidate_node_count,
        "candidate_record_count": candidate_node_count,
        "excluded_understory_count": excluded_understory_count,
        "missing_impostor_count": missing_impostor_count,
    }


def collect_instances_from_object_cells(
    object_cells_path: Path,
    impostor_meshes: set[str],
) -> dict[str, Any]:
    data = read_json(object_cells_path)
    assets = data.get("assets", [])
    if not isinstance(assets, list):
        assets = []
    strings = data.get("strings", [])
    if not isinstance(strings, list):
        strings = []
    cells = data.get("cells", {})
    if not isinstance(cells, dict):
        cells = {}

    instances_by_mesh: dict[str, list[dict[str, Any]]] = {}
    candidate_records = 0
    excluded_understory_count = 0
    missing_impostor_count = 0
    candidate_node_indices: set[int] = set()
    for cell in cells.values():
        if not isinstance(cell, dict):
            continue
        records = cell.get("placement_records", [])
        if not isinstance(records, list):
            continue
        for record_values in records:
            if not isinstance(record_values, list):
                continue
            record = record_from_compact_array(record_values, assets, strings)
            mesh_path = str(record.get("mesh_path", "")).strip()
            mesh_name = str(record.get("mesh_name", "")).strip()
            status = impostor_match_status(mesh_path, mesh_name, impostor_meshes)
            if status == "excluded_understory":
                excluded_understory_count += 1
                continue
            if status == "missing_impostor":
                missing_impostor_count += 1
                continue
            if status != "included":
                continue
            instance = instance_from_placement_record(record)
            if not instance:
                continue
            instances_by_mesh.setdefault(mesh_path, []).append(instance)
            candidate_records += 1
            node_index = int(record.get("node_index", -1))
            if node_index >= 0:
                candidate_node_indices.add(node_index)

    return {
        "instances_by_mesh": instances_by_mesh,
        "candidate_node_count": len(candidate_node_indices),
        "candidate_record_count": candidate_records,
        "excluded_understory_count": excluded_understory_count,
        "missing_impostor_count": missing_impostor_count,
    }


def write_global_tree_impostor_index(
    *,
    chunks: list[str],
    output_root: Path,
    runtime_root: Path,
    impostor_meshes: set[str],
    index_path: Path | None,
    cell_size: float,
    dry_run: bool,
) -> dict[str, Any]:
    if index_path is None:
        index_path = runtime_root / "global_tree_impostors.json"
    else:
        index_path = index_path.expanduser().resolve()

    cells: dict[str, dict[str, Any]] = {}
    missing_chunks: list[str] = []
    total_billboards = 0
    excluded_understory_count = 0
    missing_impostor_count = 0
    candidate_record_count = 0
    candidate_node_count = 0
    for chunk in chunks:
        try:
            chunk_index = build_tree_impostor_index(
                chunk=chunk,
                output_root=output_root,
                runtime_root=runtime_root,
                impostor_meshes=impostor_meshes,
                dry_run=True,
            )
        except FileNotFoundError:
            missing_chunks.append(chunk)
            continue
        groups = chunk_index.get("instances_by_mesh", {})
        if not isinstance(groups, dict):
            missing_chunks.append(chunk)
            continue
        excluded_understory_count += int(chunk_index.get("excluded_understory_count", 0))
        missing_impostor_count += int(chunk_index.get("missing_impostor_count", 0))
        candidate_record_count += int(chunk_index.get("candidate_record_count", 0))
        candidate_node_count += int(chunk_index.get("candidate_node_count", 0))
        chunk_origin = chunk_global_origin(chunk)
        for mesh_path, instances_value in groups.items():
            if not isinstance(instances_value, list):
                continue
            for instance in instances_value:
                if not isinstance(instance, dict):
                    continue
                local_position = vector_or_none(instance.get("position", []))
                if local_position is None:
                    continue
                global_position = add_vector(local_position, chunk_origin)
                global_cell_key = cell_key_for_position(global_position, cell_size)
                cell = cells.setdefault(
                    global_cell_key,
                    {
                        "global_cell_key": global_cell_key,
                        "global_center_sum": [0.0, 0.0, 0.0],
                        "global_center_weight": 0,
                        "global_bounds_min": None,
                        "global_bounds_max": None,
                        "billboard_count": 0,
                        "chunk_count": 0,
                        "chunks": {},
                        "instances_by_mesh": {},
                    },
                )
                mesh_instances = cell["instances_by_mesh"].setdefault(str(mesh_path), [])
                global_instance = {
                    "chunk": chunk,
                    "position": [
                        round(global_position[0], 6),
                        round(global_position[1], 6),
                        round(global_position[2], 6),
                    ],
                    "yaw": float(instance.get("yaw", 0.0)),
                    "scale": float(instance.get("scale", 1.0)),
                }
                rotation = compact_quaternion(instance.get("rotation"))
                if rotation is not None:
                    global_instance["rotation"] = rotation
                mesh_instances.append(global_instance)
                cell["billboard_count"] = int(cell.get("billboard_count", 0)) + 1
                cell["chunks"][chunk] = int(cell["chunks"].get(chunk, 0)) + 1
                total_billboards += 1
                merge_bounds(cell, global_position, global_position)
                add_weighted_center(cell, global_position, 1)

    for cell in cells.values():
        weight = max(int(cell.pop("global_center_weight", 0)), 1)
        center_sum = cell.pop("global_center_sum", [0.0, 0.0, 0.0])
        cell["global_center"] = [
            round(float(center_sum[0]) / weight, 6),
            round(float(center_sum[1]) / weight, 6),
            round(float(center_sum[2]) / weight, 6),
        ]
        if cell.get("global_bounds_min") is None:
            cell["global_bounds_min"] = cell["global_center"]
        if cell.get("global_bounds_max") is None:
            cell["global_bounds_max"] = cell["global_center"]
        chunk_counts = cell.pop("chunks", {})
        cell["chunk_count"] = len(chunk_counts)
        cell["chunks"] = [
            {"chunk": chunk, "billboard_count": count}
            for chunk, count in sorted(chunk_counts.items())
        ]

    data = {
        "version": GLOBAL_TREE_IMPOSTOR_INDEX_VERSION,
        "generated_by": "scripts/generators/generate_tree_impostor_index.py",
        "generated_at_unix": int(time.time()),
        "content_filter": CONTENT_FILTER,
        "cell_size": float(cell_size),
        "chunk_count": len(chunks),
        "missing_chunk_count": len(missing_chunks),
        "missing_chunks": missing_chunks,
        "candidate_node_count": candidate_node_count,
        "candidate_record_count": candidate_record_count,
        "included_billboard_count": total_billboards,
        "excluded_understory_count": excluded_understory_count,
        "missing_impostor_count": missing_impostor_count,
        "billboard_count": total_billboards,
        "cell_count": len(cells),
        "chunks": chunks,
        "cells": dict(sorted(cells.items())),
    }
    if not dry_run:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {
        "index_path": index_path,
        "chunk_count": len(chunks),
        "missing_chunk_count": len(missing_chunks),
        "cell_count": len(cells),
        "billboard_count": total_billboards,
        "excluded_understory_count": excluded_understory_count,
        "missing_impostor_count": missing_impostor_count,
    }


def delete_stale_chunk_tree_impostor_indexes(
    *, chunks: list[str], runtime_root: Path, dry_run: bool
) -> dict[str, Any]:
    deleted_count = 0
    missing_count = 0
    for chunk in chunks:
        index_path = runtime_root / "chunks" / chunk / "tree_impostors.json"
        if not index_path.exists():
            missing_count += 1
            continue
        deleted_count += 1
        if not dry_run:
            index_path.unlink()
    return {"deleted_count": deleted_count, "missing_count": missing_count}


def impostor_match_status(mesh_path: str, mesh_name: str, impostor_meshes: set[str]) -> str:
    if not mesh_path.strip():
        return "ignored"
    if is_understory_mesh_path(mesh_path, mesh_name):
        return "excluded_understory"
    if mesh_has_impostor(mesh_path, impostor_meshes):
        return "included"
    if is_impostor_candidate_mesh_path(mesh_path, mesh_name):
        return "missing_impostor"
    return "ignored"


def has_impostor(mesh_path: str, impostor_meshes: set[str]) -> bool:
    if is_understory_mesh_path(mesh_path):
        return False
    return mesh_has_impostor(mesh_path, impostor_meshes)


def mesh_has_impostor(mesh_path: str, impostor_meshes: set[str]) -> bool:
    normalized_path = normalize_mesh_path(mesh_path)
    return Path(normalized_path).stem.lower() in impostor_meshes or normalized_path in impostor_meshes


def is_understory_mesh_path(mesh_path: str, mesh_name: str = "") -> bool:
    haystack = compact_mesh_text(f"{mesh_path} {mesh_name}")
    return any(token in haystack for token in UNDERSTORY_NAME_TOKENS)


def is_impostor_candidate_mesh_path(mesh_path: str, mesh_name: str = "") -> bool:
    haystack = compact_mesh_text(f"{mesh_path} {mesh_name}")
    return any(token in haystack for token in IMPOSTOR_CANDIDATE_TOKENS)


def compact_mesh_text(value: str) -> str:
    return "".join(character for character in value.lower().replace("\\", "/") if character.isalnum())


def normalize_mesh_path(mesh_path: str) -> str:
    return mesh_path.strip().replace("\\", "/").lower()


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

    instance = {"position": position, "yaw": yaw, "scale": scale}
    rotation_values = compact_quaternion(rotation)
    if rotation_values is not None:
        instance["rotation"] = rotation_values
    return instance


def record_from_compact_array(values: list[Any], assets: list[Any], strings: list[Any]) -> dict[str, Any]:
    if len(values) < 6:
        return {}
    record: dict[str, Any] = {
        "asset": int(values[0]),
        "node_index": int(values[1]),
        "component_index": int(values[2]),
        "translation": values[5],
    }
    object_name = string_from_table(strings, int(values[3]))
    if object_name:
        record["object_name"] = object_name
    prefab_name = string_from_table(strings, int(values[4]))
    if prefab_name:
        record["prefab_name"] = prefab_name
    if len(values) > 6 and isinstance(values[6], list):
        record["rotation"] = values[6]
    if len(values) > 7 and isinstance(values[7], list):
        record["scale"] = values[7]
    if len(values) > 8 and isinstance(values[8], list):
        component = component_from_compact_array(values[8])
        if component:
            record["component"] = component
    merge_record_asset(record, assets)
    return record


def component_from_compact_array(values: list[Any]) -> dict[str, Any]:
    component: dict[str, Any] = {}
    if len(values) > 0 and isinstance(values[0], list):
        component["location"] = values[0]
    if len(values) > 1 and isinstance(values[1], list):
        component["rotation"] = values[1]
    if len(values) > 2 and isinstance(values[2], list):
        component["draw_scale_3d"] = values[2]
    if len(values) > 3 and values[3] is not None:
        component["draw_scale"] = float(values[3])
    return component


def merge_record_asset(record: dict[str, Any], assets: list[Any]) -> None:
    asset_index = int(record.get("asset", -1))
    if asset_index < 0 or asset_index >= len(assets) or not isinstance(assets[asset_index], dict):
        return
    for key, value in assets[asset_index].items():
        record.setdefault(str(key), value)


def string_from_table(strings: list[Any], index: int) -> str:
    if index < 0 or index >= len(strings):
        return ""
    return str(strings[index])


def instance_from_placement_record(record: dict[str, Any]) -> dict[str, Any]:
    node_position = vector3_or_default(record.get("translation"), (0.0, 0.0, 0.0))
    node_rotation = quaternion_or_identity(record.get("rotation"))
    node_scale = vector3_or_default(record.get("scale"), (1.0, 1.0, 1.0))

    component = record.get("component", {})
    component_position = (0.0, 0.0, 0.0)
    component_rotation = (0.0, 0.0, 0.0, 1.0)
    component_scale = (1.0, 1.0, 1.0)
    if isinstance(component, dict):
        component_position = vang_offset_to_gltf(component.get("location"))
        component_rotation = vang_rotation_to_gltf_quat(component.get("rotation"))
        component_scale = vang_scale_to_gltf(
            component.get("draw_scale_3d"),
            component.get("draw_scale"),
        )

    scaled_component_position = (
        component_position[0] * node_scale[0],
        component_position[1] * node_scale[1],
        component_position[2] * node_scale[2],
    )
    rotated_component_position = rotate_vector(node_rotation, scaled_component_position)
    position = add_vector(node_position, rotated_component_position)
    rotation = normalize_quaternion(quaternion_multiply(node_rotation, component_rotation))
    scale_vector = (
        node_scale[0] * component_scale[0],
        node_scale[1] * component_scale[1],
        node_scale[2] * component_scale[2],
    )
    scale = (scale_vector[0] + scale_vector[1] + scale_vector[2]) / 3.0

    instance = {
        "position": [round(position[0], 6), round(position[1], 6), round(position[2], 6)],
        "yaw": quaternion_yaw(rotation[0], rotation[1], rotation[2], rotation[3]),
        "scale": scale,
    }
    rotation_values = compact_quaternion(list(rotation))
    if rotation_values is not None:
        instance["rotation"] = rotation_values
    return instance


def vector3_or_default(value: Any, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    if isinstance(value, list) and len(value) >= 3:
        return (float(value[0]), float(value[1]), float(value[2]))
    return fallback


def quaternion_or_identity(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, list) and len(value) == 4:
        return normalize_quaternion(
            (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
        )
    return (0.0, 0.0, 0.0, 1.0)


def vang_offset_to_gltf(value: Any) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        return (0.0, 0.0, 0.0)
    if float(value[0]) == 0.0 and float(value[1]) == 0.0 and float(value[2]) == 0.0:
        return (0.0, 0.0, 0.0)
    return (-float(value[1]), float(value[2]), float(value[0]))


def vang_rotation_to_gltf_quat(value: Any) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        return (0.0, 0.0, 0.0, 1.0)
    if int(value[0]) == 0 and int(value[1]) == 0 and int(value[2]) == 0:
        return (0.0, 0.0, 0.0, 1.0)

    unit_to_rad = math.tau / 65536.0
    rx = -float(value[0]) * unit_to_rad
    ry = -float(value[1]) * unit_to_rad
    rz = float(value[2]) * unit_to_rad

    cx = math.cos(rx * 0.5)
    sx = math.sin(rx * 0.5)
    cy = math.cos(ry * 0.5)
    sy = math.sin(ry * 0.5)
    cz = math.cos(rz * 0.5)
    sz = math.sin(rz * 0.5)

    return normalize_quaternion(
        (
            sx * cy * cz + cx * sy * sz,
            cx * sy * cz - sx * cy * sz,
            cx * cy * sz - sx * sy * cz,
            cx * cy * cz + sx * sy * sz,
        )
    )


def vang_scale_to_gltf(draw_scale_3d: Any, draw_scale: Any) -> tuple[float, float, float]:
    uniform_scale = float(draw_scale) if isinstance(draw_scale, (int, float)) else 1.0
    sx = sy = sz = 1.0
    if isinstance(draw_scale_3d, list) and len(draw_scale_3d) == 3:
        sx = float(draw_scale_3d[0])
        sy = float(draw_scale_3d[1])
        sz = float(draw_scale_3d[2])
    return (sy * uniform_scale, sz * uniform_scale, sx * uniform_scale)


def quaternion_multiply(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    lx, ly, lz, lw = left
    rx, ry, rz, rw = right
    return (
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    )


def normalize_quaternion(
    value: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    length = math.sqrt(sum(component * component for component in value))
    if length <= 0.0:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(component / length for component in value)  # type: ignore[return-value]


def rotate_vector(
    quat: tuple[float, float, float, float],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    qvec = (vector[0], vector[1], vector[2], 0.0)
    inverse = (-quat[0], -quat[1], -quat[2], quat[3])
    rotated = quaternion_multiply(quaternion_multiply(quat, qvec), inverse)
    return (rotated[0], rotated[1], rotated[2])


def compact_quaternion(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    values = [float(component) for component in value]
    identity = [0.0, 0.0, 0.0, 1.0]
    if all(abs(values[index] - identity[index]) < 0.000001 for index in range(4)):
        return None
    return [round(component, 8) for component in values]


def chunk_global_origin(chunk_name: str) -> tuple[float, float, float]:
    chunk_x, chunk_y = chunk_coords(chunk_name)
    return (
        float(chunk_y) * TERRAIN_CHUNK_WORLD_SIZE,
        0.0,
        float(chunk_x) * TERRAIN_CHUNK_WORLD_SIZE,
    )


def chunk_coords(chunk_name: str) -> tuple[int, int]:
    normalized = normalize_chunk_name(chunk_name)
    coord_text = normalized.removeprefix("chunk_")
    parts = coord_text.split("_")
    if len(parts) != 2:
        return (0, 0)
    return (_parse_chunk_coord(parts[0]), _parse_chunk_coord(parts[1]))


def _parse_chunk_coord(value: str) -> int:
    return -int(value[1:]) if value.startswith("n") else int(value)


def vector_or_none(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    return (float(value[0]), float(value[1]), float(value[2]))


def add_vector(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


def cell_key_for_position(position: tuple[float, float, float], cell_size: float) -> str:
    cell_x = int(position[0] // cell_size)
    cell_z = int(position[2] // cell_size)
    return f"{cell_x}:{cell_z}"


def merge_bounds(cell: dict[str, Any], bounds_min: Any, bounds_max: Any) -> None:
    min_values = vector_or_none(bounds_min)
    max_values = vector_or_none(bounds_max)
    if min_values is None or max_values is None:
        return
    if cell.get("global_bounds_min") is None:
        cell["global_bounds_min"] = list(min_values)
        cell["global_bounds_max"] = list(max_values)
        return
    current_min = cell["global_bounds_min"]
    current_max = cell["global_bounds_max"]
    for axis in range(3):
        current_min[axis] = min(float(current_min[axis]), min_values[axis])
        current_max[axis] = max(float(current_max[axis]), max_values[axis])


def add_weighted_center(cell: dict[str, Any], center: Any, weight: int) -> None:
    center_values = vector_or_none(center)
    if center_values is None:
        return
    center_sum = cell["global_center_sum"]
    for axis in range(3):
        center_sum[axis] = float(center_sum[axis]) + center_values[axis] * weight
    cell["global_center_weight"] = int(cell.get("global_center_weight", 0)) + weight


def quaternion_yaw(x: float, y: float, z: float, w: float) -> float:
    sin_y = 2.0 * (w * y + x * z)
    cos_y = 1.0 - 2.0 * (y * y + x * x)
    return math.atan2(sin_y, cos_y)


if __name__ == "__main__":
    raise SystemExit(main())
