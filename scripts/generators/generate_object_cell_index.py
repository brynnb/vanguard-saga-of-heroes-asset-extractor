#!/usr/bin/env python3
"""Build compact per-cell object placement indexes for Godot streaming."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import os
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
    read_json_if_exists,
    read_json,
)


OBJECT_CELL_INDEX_VERSION = 4
GLOBAL_OBJECT_CELL_INDEX_VERSION = 2
GLOBAL_LANDMARK_INDEX_VERSION = 2
DEFAULT_CELL_SIZE = 24000.0
TERRAIN_CHUNK_WORLD_SIZE = 204400.0
LANDMARK_SHELL_MIN_RADIUS = 2000.0
LANDMARK_SHELL_MIN_CULL_DISTANCE = 400000.0
TIER_BUCKET_NAMES = [
    "tier_0_landmark",
    "tier_1_far",
    "tier_2_mid",
    "tier_3_near",
    "tier_4_detail",
]
TIER_BUCKET_LOD_POLICIES = {
    "tier_0_landmark": "lowest",
    "tier_1_far": "lowest",
    "tier_2_mid": "middle",
    "tier_3_near": "highest",
    "tier_4_detail": "highest",
}
TREE_MESH_NAME_TOKENS = {
    "tree",
    "trees",
    "speedtree",
    "oak",
    "pine",
    "palm",
    "birch",
    "cedar",
    "willow",
    "spruce",
    "fir",
    "juniper",
    "maple",
    "cypress",
    "sycamore",
    "acacia",
    "baobab",
    "banyan",
    "redwood",
    "trunk",
    "stump",
}
TREE_MESH_PREFIX_TOKENS = {
    "speedtree",
    "oak",
    "pine",
    "palm",
    "birch",
    "cedar",
    "willow",
    "spruce",
    "juniper",
    "maple",
    "cypress",
    "sycamore",
    "acacia",
}
RECORD_FORMAT = [
    "asset",
    "node_index",
    "component_index",
    "object_name",
    "prefab_name",
    "translation",
    "rotation",
    "scale",
    "component",
]
COMPONENT_FORMAT = ["location", "rotation", "draw_scale_3d", "draw_scale"]
TIER_LOD_BUCKET_FORMAT = ["record", "asset"]
LANDMARK_ENTRY_FORMAT = {
    "type": "dict_v2",
    "required": [
        "source_chunk",
        "source_cell_key",
        "source_record_index",
        "source_asset",
        "selected_asset",
        "asset_id",
        "mesh_path",
        "local_position",
        "global_position",
        "placement_record",
    ],
}
GLOBAL_OBJECT_ENTRY_FORMAT = {
    "type": "dict_v2",
    "required": [
        "source_chunk",
        "source_cell_key",
        "source_record_index",
        "source_asset",
        "selected_asset",
        "bucket",
        "asset_id",
        "mesh_path",
        "local_position",
        "global_position",
        "placement_record",
    ],
}


_CELL_WORKER_CONTEXT: dict[str, Any] = {}


def _resolve_workers(workers: int) -> int:
    if workers < 1:
        return os.cpu_count() or 1
    return workers


def discover_chunks(output_root: Path) -> list[str]:
    terrain_root = output_root / "terrain/terrain_grid"
    return sorted(
        path.name.removesuffix("_objects.gltf")
        for path in terrain_root.glob("chunk_*_objects.gltf")
    )


def init_cell_worker(
    output_root: str, runtime_root: str, static_mesh_tab: str, cell_size: float
) -> None:
    global _CELL_WORKER_CONTEXT
    _CELL_WORKER_CONTEXT = {
        "output_root": Path(output_root),
        "runtime_root": Path(runtime_root),
        "metadata": StaticMeshMetadata(Path(static_mesh_tab)),
        "cell_size": float(cell_size),
    }


def cell_chunk_worker(task: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    try:
        context = _CELL_WORKER_CONTEXT
        result = build_object_cell_index(
            chunk=str(task["chunk"]),
            output_root=context["output_root"],
            runtime_root=context["runtime_root"],
            metadata=context["metadata"],
            cell_size=float(context["cell_size"]),
            dry_run=bool(task["dry_run"]),
        )
        return result, None
    except Exception as exc:  # noqa: BLE001 - worker reports chunk failures to parent.
        return None, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", action="append", default=[], help="Chunk to index.")
    parser.add_argument("--all", action="store_true", help="Index every chunk with object placements.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--static-mesh-tab", type=Path, default=DEFAULT_STATIC_MESH_TAB)
    parser.add_argument("--cell-size", type=float, default=DEFAULT_CELL_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--write-global-index",
        action="store_true",
        help="Write a compact global object-cell index for the selected chunks.",
    )
    parser.add_argument(
        "--global-index-path",
        type=Path,
        default=None,
        help="Override global object-cell index path. Defaults to <runtime-root>/global_object_cells.json.",
    )
    parser.add_argument(
        "--write-landmark-index",
        action="store_true",
        help="Write a compact far-landmark index for the selected chunks.",
    )
    parser.add_argument(
        "--landmark-index-path",
        type=Path,
        default=None,
        help="Override far-landmark index path. Defaults to <runtime-root>/global_landmark_cells.json.",
    )
    parser.add_argument("--limit-chunks", type=int, default=0, help="Debug/smoke chunk limit with --all.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Chunk worker processes; 0 uses all CPUs.",
    )
    args = parser.parse_args()

    output_root = args.output_root.resolve()
    runtime_root = args.runtime_root.resolve()
    chunks: list[str] = [normalize_chunk_name(chunk) for chunk in args.chunk]
    if args.all:
        chunks.extend(discover_chunks(output_root))
    chunks = sorted(set(chunks))
    if args.limit_chunks > 0:
        chunks = chunks[: args.limit_chunks]
    if not chunks:
        parser.error("provide at least one --chunk or --all")

    failures = 0
    workers = min(_resolve_workers(args.workers), len(chunks))
    if workers > 1:
        print(f"Workers: {workers}")
        tasks = [{"chunk": chunk, "dry_run": args.dry_run} for chunk in chunks]
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=init_cell_worker,
            initargs=(
                str(output_root),
                str(runtime_root),
                str(args.static_mesh_tab),
                float(args.cell_size),
            ),
        ) as executor:
            futures = {executor.submit(cell_chunk_worker, task): task["chunk"] for task in tasks}
            for completed, future in enumerate(as_completed(futures), start=1):
                chunk = futures[future]
                result, error = future.result()
                if result is None:
                    failures += 1
                    print(f"ERROR: {chunk}: {error}", file=sys.stderr)
                    continue
                print(f"[{completed}/{len(chunks)}]", end=" ")
                print_cell_summary(result, args.dry_run)
    else:
        for chunk in chunks:
            try:
                result = build_object_cell_index(
                    chunk=chunk,
                    output_root=output_root,
                    runtime_root=runtime_root,
                    metadata=StaticMeshMetadata(args.static_mesh_tab),
                    cell_size=float(args.cell_size),
                    dry_run=args.dry_run,
                )
                print_cell_summary(result, args.dry_run)
            except Exception as exc:  # noqa: BLE001 - CLI should continue across chunks.
                failures += 1
                print(f"ERROR: {chunk}: {exc}", file=sys.stderr)
    if failures == 0 and args.write_global_index:
        global_result = write_global_object_cell_index(
            chunks=chunks,
            runtime_root=runtime_root,
            index_path=args.global_index_path,
            cell_size=float(args.cell_size),
            dry_run=args.dry_run,
        )
        print_global_index_summary(global_result, args.dry_run)
    if failures == 0 and args.write_landmark_index:
        landmark_result = write_global_landmark_index(
            chunks=chunks,
            runtime_root=runtime_root,
            index_path=args.landmark_index_path,
            cell_size=float(args.cell_size),
            dry_run=args.dry_run,
        )
        print_global_landmark_index_summary(landmark_result, args.dry_run)
    return 1 if failures else 0


def build_object_cell_index(
    *,
    chunk: str,
    output_root: Path,
    runtime_root: Path,
    metadata: StaticMeshMetadata,
    cell_size: float,
    dry_run: bool,
) -> dict[str, Any]:
    terrain_root = output_root / "terrain/terrain_grid"
    objects_path = terrain_root / f"{chunk}_objects.gltf"
    sgo_path = terrain_root / f"{chunk}_sgo.json"
    if not objects_path.exists():
        raise FileNotFoundError(f"missing object placement file: {objects_path}")

    objects_data = read_json(objects_path)
    sgo_manifest = read_json_if_exists(sgo_path)
    if not isinstance(sgo_manifest, dict):
        sgo_manifest = {}
    runtime_meshes = runtime_mesh_entries(runtime_root, chunk)
    nodes = objects_data.get("nodes", [])
    if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
        raise ValueError(f"{chunk} object placement nodes are invalid")
    root_children = nodes[0].get("children", [])
    if not isinstance(root_children, list):
        raise ValueError(f"{chunk} root children are invalid")

    chunk_origin = chunk_global_origin(chunk)
    cell_work: dict[str, dict[str, Any]] = {}
    asset_refs: dict[str, int] = {}
    assets: list[dict[str, Any]] = []
    string_refs: dict[str, int] = {}
    strings: list[str] = []
    skipped_nodes = 0
    skipped_records = 0
    total_record_count = 0
    for node_index_value in root_children:
        node_index = int(node_index_value)
        if node_index < 0 or node_index >= len(nodes) or not isinstance(nodes[node_index], dict):
            skipped_nodes += 1
            continue
        node = nodes[node_index]
        position = node_position(node)
        global_position = add_vector(position, chunk_origin)
        cell_key = cell_key_for_position(position, cell_size)
        global_cell_key = cell_key_for_position(global_position, cell_size)
        cell = cell_work.setdefault(
            cell_key,
            {
                "node_indices": [],
                "placement_records": [],
                "global_cell_key": global_cell_key,
                "center_sum": [0.0, 0.0, 0.0],
                "global_center_sum": [0.0, 0.0, 0.0],
                "bounds_min": [position[0], position[1], position[2]],
                "bounds_max": [position[0], position[1], position[2]],
                "global_bounds_min": [
                    global_position[0],
                    global_position[1],
                    global_position[2],
                ],
                "global_bounds_max": [
                    global_position[0],
                    global_position[1],
                    global_position[2],
                ],
            },
        )
        cell["node_indices"].append(node_index)
        center_sum = cell["center_sum"]
        center_sum[0] += position[0]
        center_sum[1] += position[1]
        center_sum[2] += position[2]
        global_center_sum = cell["global_center_sum"]
        global_center_sum[0] += global_position[0]
        global_center_sum[1] += global_position[1]
        global_center_sum[2] += global_position[2]
        bounds_min = cell["bounds_min"]
        bounds_max = cell["bounds_max"]
        global_bounds_min = cell["global_bounds_min"]
        global_bounds_max = cell["global_bounds_max"]
        for axis in range(3):
            bounds_min[axis] = min(bounds_min[axis], position[axis])
            bounds_max[axis] = max(bounds_max[axis], position[axis])
            global_bounds_min[axis] = min(global_bounds_min[axis], global_position[axis])
            global_bounds_max[axis] = max(global_bounds_max[axis], global_position[axis])
        records = placement_records_for_node(
            node_index=node_index,
            node=node,
            sgo_manifest=sgo_manifest,
            runtime_meshes=runtime_meshes,
            metadata=metadata,
            asset_refs=asset_refs,
            assets=assets,
            string_refs=string_refs,
            strings=strings,
        )
        skipped_records += int(records.get("skipped", 0))
        record_values = records.get("records", [])
        if not isinstance(record_values, list):
            record_values = []
        cell["placement_records"].extend(record_values)
        total_record_count += len(record_values)

    cells: dict[str, dict[str, Any]] = {}
    for cell_key, cell in sorted(cell_work.items()):
        node_indices = cell["node_indices"]
        count = len(node_indices)
        center_sum = cell["center_sum"]
        global_center_sum = cell["global_center_sum"]
        placement_records = cell["placement_records"]
        tier_lod_buckets = tier_lod_buckets_for_records(
            placement_records, assets, metadata, runtime_meshes, asset_refs
        )
        summary = cell_summary_for_records(placement_records, assets, metadata)
        tier_buckets = tier_buckets_for_records(placement_records, assets, metadata)
        cells[cell_key] = {
            "count": count,
            "record_count": len(placement_records),
            "global_cell_key": cell["global_cell_key"],
            "center": [
                center_sum[0] / max(count, 1),
                center_sum[1] / max(count, 1),
                center_sum[2] / max(count, 1),
            ],
            "global_center": [
                global_center_sum[0] / max(count, 1),
                global_center_sum[1] / max(count, 1),
                global_center_sum[2] / max(count, 1),
            ],
            "bounds_min": cell["bounds_min"],
            "bounds_max": cell["bounds_max"],
            "global_bounds_min": cell["global_bounds_min"],
            "global_bounds_max": cell["global_bounds_max"],
            "summary": summary,
            "tier_buckets": tier_buckets,
            "tier_lod_buckets": tier_lod_buckets,
            "node_indices": node_indices,
            "placement_records": placement_records,
        }

    stat = objects_path.stat()
    index_path = runtime_root / "chunks" / chunk / "object_cells.json"
    data = {
        "version": OBJECT_CELL_INDEX_VERSION,
        "generated_by": "scripts/generators/generate_object_cell_index.py",
        "generated_at_unix": int(time.time()),
        "chunk": chunk,
        "cell_size": float(cell_size),
        "cell_key_space": "chunk_local",
        "global_cell_key_space": "world",
        "chunk_global_origin": list(chunk_origin),
        "objects_source_relative_path": str(objects_path.relative_to(output_root)),
        "objects_source_bytes": stat.st_size,
        "objects_source_mtime_unix": int(stat.st_mtime),
        "objects_source_mtime_ns": stat.st_mtime_ns,
        "total_node_count": len(root_children),
        "indexed_node_count": sum(int(cell["count"]) for cell in cells.values()),
        "skipped_node_count": skipped_nodes,
        "placement_record_count": total_record_count,
        "skipped_record_count": skipped_records,
        "asset_count": len(assets),
        "assets": assets,
        "string_count": len(strings),
        "strings": strings,
        "record_format": RECORD_FORMAT,
        "component_format": COMPONENT_FORMAT,
        "tier_lod_bucket_format": TIER_LOD_BUCKET_FORMAT,
        "cell_count": len(cells),
        "cells": cells,
    }

    if not dry_run:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return {
        "chunk": chunk,
        "index_path": index_path,
        "total_node_count": len(root_children),
        "indexed_node_count": int(data["indexed_node_count"]),
        "skipped_node_count": skipped_nodes,
        "placement_record_count": total_record_count,
        "skipped_record_count": skipped_records,
        "asset_count": len(assets),
        "cell_count": len(cells),
    }


def print_cell_summary(result: dict[str, Any], dry_run: bool) -> None:
    label = "DRY RUN" if dry_run else "WROTE"
    print(
        f"{label}: {result['chunk']} object_cells={result['cell_count']} "
        f"nodes={result['indexed_node_count']}/{result['total_node_count']} "
        f"records={result['placement_record_count']} assets={result['asset_count']} "
        f"skipped={result['skipped_node_count']}:{result['skipped_record_count']} "
        f"index={result['index_path']}"
    )


def write_global_object_cell_index(
    *,
    chunks: list[str],
    runtime_root: Path,
    index_path: Path | None,
    cell_size: float,
    dry_run: bool,
) -> dict[str, Any]:
    if index_path is None:
        index_path = runtime_root / "global_object_cells.json"
    else:
        index_path = index_path.expanduser().resolve()

    cells: dict[str, dict[str, Any]] = {}
    missing_chunks: list[str] = []
    total_records = 0
    total_embedded_records = 0
    for chunk in chunks:
        chunk_path = runtime_root / "chunks" / chunk / "object_cells.json"
        chunk_index = read_json_if_exists(chunk_path)
        if not isinstance(chunk_index, dict):
            missing_chunks.append(chunk)
            continue
        assets = chunk_index.get("assets", [])
        if not isinstance(assets, list):
            assets = []
        strings = chunk_index.get("strings", [])
        if not isinstance(strings, list):
            strings = []
        chunk_origin = vector_or_none(chunk_index.get("chunk_global_origin", []))
        if chunk_origin is None:
            chunk_origin = chunk_global_origin(chunk)
        chunk_cells = chunk_index.get("cells", {})
        if not isinstance(chunk_cells, dict):
            missing_chunks.append(chunk)
            continue
        for local_cell_key, cell_value in chunk_cells.items():
            if not isinstance(cell_value, dict):
                continue
            cell = cell_value
            global_cell_key = str(cell.get("global_cell_key", "")).strip()
            if not global_cell_key:
                global_cell_key = f"{chunk}:{local_cell_key}"
            summary = cell.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            record_count = int(cell.get("record_count", summary.get("placement_count", 0)))
            placement_entries = global_object_entries_for_cell(
                cell,
                assets,
                strings,
                chunk=chunk,
                local_cell_key=str(local_cell_key),
                chunk_origin=chunk_origin,
            )
            total_records += record_count
            total_embedded_records += len(placement_entries)
            global_cell = cells.setdefault(
                global_cell_key,
                {
                    "global_cell_key": global_cell_key,
                    "global_center_sum": [0.0, 0.0, 0.0],
                    "global_center_weight": 0,
                    "global_bounds_min": None,
                    "global_bounds_max": None,
                    "record_count": 0,
                    "chunk_count": 0,
                    "chunks": [],
                    "summary": {},
                },
            )
            global_cell["record_count"] = int(global_cell.get("record_count", 0)) + record_count
            global_cell["chunk_count"] = int(global_cell.get("chunk_count", 0)) + 1
            global_cell["chunks"].append(
                {
                    "chunk": chunk,
                    "cell_key": str(local_cell_key),
                    "record_count": record_count,
                    "placements": placement_entries,
                    "summary": summary,
                }
            )
            merge_summary(global_cell["summary"], summary)
            merge_bounds(
                global_cell,
                cell.get("global_bounds_min", cell.get("bounds_min", [])),
                cell.get("global_bounds_max", cell.get("bounds_max", [])),
            )
            add_weighted_center(
                global_cell,
                cell.get("global_center", cell.get("center", [])),
                max(record_count, 1),
            )

    for global_cell in cells.values():
        weight = max(int(global_cell.pop("global_center_weight", 0)), 1)
        center_sum = global_cell.pop("global_center_sum", [0.0, 0.0, 0.0])
        global_cell["global_center"] = [
            round(float(center_sum[0]) / weight, 6),
            round(float(center_sum[1]) / weight, 6),
            round(float(center_sum[2]) / weight, 6),
        ]
        if global_cell.get("global_bounds_min") is None:
            global_cell["global_bounds_min"] = global_cell["global_center"]
        if global_cell.get("global_bounds_max") is None:
            global_cell["global_bounds_max"] = global_cell["global_center"]

    data = {
        "version": GLOBAL_OBJECT_CELL_INDEX_VERSION,
        "generated_by": "scripts/generators/generate_object_cell_index.py",
        "generated_at_unix": int(time.time()),
        "cell_size": float(cell_size),
        "source_index_version": OBJECT_CELL_INDEX_VERSION,
        "object_entry_format": GLOBAL_OBJECT_ENTRY_FORMAT,
        "record_format": RECORD_FORMAT,
        "component_format": COMPONENT_FORMAT,
        "chunk_count": len(chunks),
        "missing_chunk_count": len(missing_chunks),
        "missing_chunks": missing_chunks,
        "record_count": total_records,
        "embedded_record_count": total_embedded_records,
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
        "record_count": total_records,
        "embedded_record_count": total_embedded_records,
    }


def print_global_index_summary(result: dict[str, Any], dry_run: bool) -> None:
    label = "DRY RUN" if dry_run else "WROTE"
    print(
        f"{label}: global_object_cells={result['cell_count']} "
        f"chunks={result['chunk_count']} missing={result['missing_chunk_count']} "
        f"records={result['record_count']} embedded={result.get('embedded_record_count', 0)} "
        f"index={result['index_path']}"
    )


def global_object_entries_for_cell(
    cell: dict[str, Any],
    assets: list[Any],
    strings: list[Any],
    *,
    chunk: str,
    local_cell_key: str,
    chunk_origin: tuple[float, float, float],
) -> list[dict[str, Any]]:
    records = cell.get("placement_records", [])
    if not isinstance(records, list):
        return []
    entries: list[dict[str, Any]] = []
    seen_record_indices: set[int] = set()
    lod_buckets = cell.get("tier_lod_buckets", {})
    if isinstance(lod_buckets, dict):
        for bucket_name in TIER_BUCKET_NAMES:
            pairs = lod_buckets.get(bucket_name, [])
            if not isinstance(pairs, list):
                continue
            for pair in pairs:
                if not isinstance(pair, list) or len(pair) < 2:
                    continue
                record_index = int(pair[0])
                if record_index in seen_record_indices:
                    continue
                entry = global_object_entry(
                    cell,
                    record_index,
                    int(pair[1]),
                    bucket_name,
                    assets,
                    strings,
                    chunk=chunk,
                    local_cell_key=local_cell_key,
                    chunk_origin=chunk_origin,
                )
                if entry:
                    entries.append(entry)
                    seen_record_indices.add(record_index)

    tier_buckets = cell.get("tier_buckets", {})
    if isinstance(tier_buckets, dict):
        for bucket_name in TIER_BUCKET_NAMES:
            record_indices = tier_buckets.get(bucket_name, [])
            if not isinstance(record_indices, list):
                continue
            for record_index_value in record_indices:
                record_index = int(record_index_value)
                if record_index in seen_record_indices:
                    continue
                entry = global_object_entry(
                    cell,
                    record_index,
                    record_asset_index_from_cell(cell, record_index),
                    bucket_name,
                    assets,
                    strings,
                    chunk=chunk,
                    local_cell_key=local_cell_key,
                    chunk_origin=chunk_origin,
                )
                if entry:
                    entries.append(entry)
                    seen_record_indices.add(record_index)

    for record_index in range(len(records)):
        if record_index in seen_record_indices:
            continue
        entry = global_object_entry(
            cell,
            record_index,
            record_asset_index_from_cell(cell, record_index),
            "tier_3_near",
            assets,
            strings,
            chunk=chunk,
            local_cell_key=local_cell_key,
            chunk_origin=chunk_origin,
        )
        if entry:
            entries.append(entry)
            seen_record_indices.add(record_index)
    return entries


def global_object_entry(
    cell: dict[str, Any],
    record_index: int,
    asset_index: int,
    bucket_name: str,
    assets: list[Any],
    strings: list[Any],
    *,
    chunk: str,
    local_cell_key: str,
    chunk_origin: tuple[float, float, float],
) -> dict[str, Any]:
    records = cell.get("placement_records", [])
    if not isinstance(records, list) or record_index < 0 or record_index >= len(records):
        return {}
    source_record = records[record_index]
    if not isinstance(source_record, list):
        return {}
    selected_asset = asset_for_index(assets, asset_index)
    mesh_path = str(selected_asset.get("mesh_path", ""))
    if not mesh_path:
        return {}
    source_asset_index = record_asset_index(source_record)
    local_position = record_local_position(source_record)
    global_position = add_vector(local_position, chunk_origin)
    placement_record = expanded_placement_record(source_record, asset_index, assets, strings)
    return {
        "source_chunk": chunk,
        "source_cell_key": local_cell_key,
        "source_record_index": record_index,
        "source_asset": source_asset_index,
        "selected_asset": asset_index,
        "bucket": bucket_name,
        "asset_id": str(selected_asset.get("asset_id", "")),
        "mesh_path": mesh_path,
        "mesh_name": str(selected_asset.get("mesh_name", "")),
        "visual_tier": str(selected_asset.get("visual_tier", "")),
        "native_scene_relative_path": str(selected_asset.get("native_scene_relative_path", "")),
        "runtime_relative_path": str(selected_asset.get("runtime_relative_path", "")),
        "valid_triangle_indices": bool(selected_asset.get("valid_triangle_indices", True)),
        "chunk_global_origin": compact_vector(list(chunk_origin)),
        "local_position": compact_vector(list(local_position)),
        "global_position": compact_vector(list(global_position)),
        "placement_record": placement_record,
    }


def write_global_landmark_index(
    *,
    chunks: list[str],
    runtime_root: Path,
    index_path: Path | None,
    cell_size: float,
    dry_run: bool,
) -> dict[str, Any]:
    if index_path is None:
        index_path = runtime_root / "global_landmark_cells.json"
    else:
        index_path = index_path.expanduser().resolve()

    cells: dict[str, dict[str, Any]] = {}
    missing_chunks: list[str] = []
    total_landmarks = 0
    for chunk in chunks:
        chunk_path = runtime_root / "chunks" / chunk / "object_cells.json"
        chunk_index = read_json_if_exists(chunk_path)
        if not isinstance(chunk_index, dict):
            missing_chunks.append(chunk)
            continue
        assets = chunk_index.get("assets", [])
        if not isinstance(assets, list):
            assets = []
        strings = chunk_index.get("strings", [])
        if not isinstance(strings, list):
            strings = []
        chunk_origin = vector_or_none(chunk_index.get("chunk_global_origin", []))
        if chunk_origin is None:
            chunk_origin = chunk_global_origin(chunk)
        chunk_cells = chunk_index.get("cells", {})
        if not isinstance(chunk_cells, dict):
            missing_chunks.append(chunk)
            continue
        for local_cell_key, cell_value in chunk_cells.items():
            if not isinstance(cell_value, dict):
                continue
            cell = cell_value
            landmark_entries = landmark_entries_for_cell(
                cell,
                assets,
                strings,
                chunk=chunk,
                local_cell_key=str(local_cell_key),
                chunk_origin=chunk_origin,
            )
            if not landmark_entries:
                continue
            global_cell_key = str(cell.get("global_cell_key", "")).strip()
            if not global_cell_key:
                global_cell_key = f"{chunk}:{local_cell_key}"
            summary = cell.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            global_cell = cells.setdefault(
                global_cell_key,
                {
                    "global_cell_key": global_cell_key,
                    "global_center_sum": [0.0, 0.0, 0.0],
                    "global_center_weight": 0,
                    "global_bounds_min": None,
                    "global_bounds_max": None,
                    "landmark_count": 0,
                    "chunk_count": 0,
                    "chunks": [],
                    "summary": {},
                },
            )
            global_cell["landmark_count"] = int(global_cell.get("landmark_count", 0)) + len(
                landmark_entries
            )
            global_cell["chunk_count"] = int(global_cell.get("chunk_count", 0)) + 1
            total_landmarks += len(landmark_entries)
            global_cell["chunks"].append(
                {
                    "chunk": chunk,
                    "cell_key": str(local_cell_key),
                    "landmark_count": len(landmark_entries),
                    "placements": landmark_entries,
                    "summary": {
                        "landmark_count": int(summary.get("landmark_count", len(landmark_entries))),
                        "max_cull_distance": float(summary.get("max_cull_distance", 0.0)),
                        "max_sphere_radius": float(summary.get("max_sphere_radius", 0.0)),
                    },
                }
            )
            merge_summary(
                global_cell["summary"],
                {
                    "landmark_count": len(landmark_entries),
                    "max_cull_distance": float(summary.get("max_cull_distance", 0.0)),
                    "max_sphere_radius": float(summary.get("max_sphere_radius", 0.0)),
                },
            )
            merge_bounds(
                global_cell,
                cell.get("global_bounds_min", cell.get("bounds_min", [])),
                cell.get("global_bounds_max", cell.get("bounds_max", [])),
            )
            add_weighted_center(
                global_cell,
                cell.get("global_center", cell.get("center", [])),
                max(len(landmark_entries), 1),
            )

    for global_cell in cells.values():
        weight = max(int(global_cell.pop("global_center_weight", 0)), 1)
        center_sum = global_cell.pop("global_center_sum", [0.0, 0.0, 0.0])
        global_cell["global_center"] = [
            round(float(center_sum[0]) / weight, 6),
            round(float(center_sum[1]) / weight, 6),
            round(float(center_sum[2]) / weight, 6),
        ]
        if global_cell.get("global_bounds_min") is None:
            global_cell["global_bounds_min"] = global_cell["global_center"]
        if global_cell.get("global_bounds_max") is None:
            global_cell["global_bounds_max"] = global_cell["global_center"]

    data = {
        "version": GLOBAL_LANDMARK_INDEX_VERSION,
        "generated_by": "scripts/generators/generate_object_cell_index.py",
        "generated_at_unix": int(time.time()),
        "cell_size": float(cell_size),
        "source_index_version": OBJECT_CELL_INDEX_VERSION,
        "landmark_entry_format": LANDMARK_ENTRY_FORMAT,
        "record_format": RECORD_FORMAT,
        "component_format": COMPONENT_FORMAT,
        "chunk_count": len(chunks),
        "missing_chunk_count": len(missing_chunks),
        "missing_chunks": missing_chunks,
        "landmark_count": total_landmarks,
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
        "landmark_count": total_landmarks,
    }


def print_global_landmark_index_summary(result: dict[str, Any], dry_run: bool) -> None:
    label = "DRY RUN" if dry_run else "WROTE"
    print(
        f"{label}: global_landmark_cells={result['cell_count']} "
        f"chunks={result['chunk_count']} missing={result['missing_chunk_count']} "
        f"landmarks={result['landmark_count']} index={result['index_path']}"
    )


def landmark_entries_for_cell(
    cell: dict[str, Any],
    assets: list[Any],
    strings: list[Any],
    *,
    chunk: str,
    local_cell_key: str,
    chunk_origin: tuple[float, float, float],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    lod_buckets = cell.get("tier_lod_buckets", {})
    if isinstance(lod_buckets, dict):
        for pair in lod_buckets.get("tier_0_landmark", []):
            if not isinstance(pair, list) or len(pair) < 2:
                continue
            record_index = int(pair[0])
            asset_index = int(pair[1])
            entry = landmark_entry(
                cell,
                record_index,
                asset_index,
                assets,
                strings,
                chunk=chunk,
                local_cell_key=local_cell_key,
                chunk_origin=chunk_origin,
            )
            if entry:
                entries.append(entry)
    if entries:
        return entries
    tier_buckets = cell.get("tier_buckets", {})
    if not isinstance(tier_buckets, dict):
        return []
    for record_index_value in tier_buckets.get("tier_0_landmark", []):
        record_index = int(record_index_value)
        asset_index = record_asset_index_from_cell(cell, record_index)
        entry = landmark_entry(
            cell,
            record_index,
            asset_index,
            assets,
            strings,
            chunk=chunk,
            local_cell_key=local_cell_key,
            chunk_origin=chunk_origin,
        )
        if entry:
            entries.append(entry)
    return entries


def landmark_entry(
    cell: dict[str, Any],
    record_index: int,
    asset_index: int,
    assets: list[Any],
    strings: list[Any],
    *,
    chunk: str,
    local_cell_key: str,
    chunk_origin: tuple[float, float, float],
) -> dict[str, Any]:
    records = cell.get("placement_records", [])
    if not isinstance(records, list) or record_index < 0 or record_index >= len(records):
        return {}
    source_record = records[record_index]
    if not isinstance(source_record, list):
        return {}
    selected_asset = asset_for_index(assets, asset_index)
    source_asset_index = record_asset_index(source_record)
    local_position = record_local_position(source_record)
    global_position = add_vector(local_position, chunk_origin)
    placement_record = expanded_placement_record(source_record, asset_index, assets, strings)
    return {
        "source_chunk": chunk,
        "source_cell_key": local_cell_key,
        "source_record_index": record_index,
        "source_asset": source_asset_index,
        "selected_asset": asset_index,
        "asset_id": str(selected_asset.get("asset_id", "")),
        "mesh_path": str(selected_asset.get("mesh_path", "")),
        "mesh_name": str(selected_asset.get("mesh_name", "")),
        "native_scene_relative_path": str(selected_asset.get("native_scene_relative_path", "")),
        "runtime_relative_path": str(selected_asset.get("runtime_relative_path", "")),
        "valid_triangle_indices": bool(selected_asset.get("valid_triangle_indices", True)),
        "local_position": compact_vector(list(local_position)),
        "global_position": compact_vector(list(global_position)),
        "placement_record": placement_record,
    }


def record_asset_index_from_cell(cell: dict[str, Any], record_index: int) -> int:
    records = cell.get("placement_records", [])
    if not isinstance(records, list) or record_index < 0 or record_index >= len(records):
        return -1
    return record_asset_index(records[record_index])


def expanded_placement_record(
    source_record: list[Any],
    selected_asset_index: int,
    assets: list[Any],
    strings: list[Any],
) -> dict[str, Any]:
    if len(source_record) < 6:
        return {}
    record: dict[str, Any] = {
        "asset": selected_asset_index,
        "node_index": int(source_record[1]),
        "component_index": int(source_record[2]),
        "translation": source_record[5],
    }
    object_name = string_from_table(strings, int(source_record[3]))
    if object_name:
        record["object_name"] = object_name
    prefab_name = string_from_table(strings, int(source_record[4]))
    if prefab_name:
        record["prefab_name"] = prefab_name
    if len(source_record) > 6 and isinstance(source_record[6], list):
        record["rotation"] = source_record[6]
    if len(source_record) > 7 and isinstance(source_record[7], list):
        record["scale"] = source_record[7]
    if len(source_record) > 8 and isinstance(source_record[8], list):
        component = expanded_component_transform(source_record[8])
        if component:
            record["component"] = component
    asset = asset_for_index(assets, selected_asset_index)
    for key, value in asset.items():
        record.setdefault(str(key), value)
    source_asset_index = record_asset_index(source_record)
    if source_asset_index != selected_asset_index:
        record["source_asset"] = source_asset_index
    return record


def expanded_component_transform(values: list[Any]) -> dict[str, Any]:
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


def record_local_position(record: list[Any]) -> tuple[float, float, float]:
    translation = vector_or_none(record[5]) if len(record) > 5 else None
    if translation is None:
        translation = (0.0, 0.0, 0.0)
    component_location = None
    if len(record) > 8 and isinstance(record[8], list) and record[8]:
        component_location = vector_or_none(record[8][0])
    if component_location is None:
        return translation
    return add_vector(translation, component_location)


def asset_for_index(assets: list[Any], asset_index: int) -> dict[str, Any]:
    if 0 <= asset_index < len(assets) and isinstance(assets[asset_index], dict):
        return dict(assets[asset_index])
    return {}


def string_from_table(strings: list[Any], index: int) -> str:
    if 0 <= index < len(strings):
        return str(strings[index])
    return ""


def merge_summary(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict):
            target_child = target.setdefault(key, {})
            if isinstance(target_child, dict):
                merge_summary(target_child, value)
            continue
        if key.startswith("max_"):
            target[key] = max(float(target.get(key, 0.0)), float(value or 0.0))
        elif isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + value


def merge_bounds(global_cell: dict[str, Any], bounds_min: Any, bounds_max: Any) -> None:
    min_values = vector_or_none(bounds_min)
    max_values = vector_or_none(bounds_max)
    if min_values is None or max_values is None:
        return
    if global_cell.get("global_bounds_min") is None:
        global_cell["global_bounds_min"] = list(min_values)
        global_cell["global_bounds_max"] = list(max_values)
        return
    current_min = global_cell["global_bounds_min"]
    current_max = global_cell["global_bounds_max"]
    for axis in range(3):
        current_min[axis] = min(float(current_min[axis]), min_values[axis])
        current_max[axis] = max(float(current_max[axis]), max_values[axis])


def add_weighted_center(global_cell: dict[str, Any], center: Any, weight: int) -> None:
    center_values = vector_or_none(center)
    if center_values is None:
        return
    center_sum = global_cell["global_center_sum"]
    for axis in range(3):
        center_sum[axis] = float(center_sum[axis]) + center_values[axis] * weight
    global_cell["global_center_weight"] = int(global_cell.get("global_center_weight", 0)) + weight


def vector_or_none(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, list) or len(value) < 3:
        return None
    return (float(value[0]), float(value[1]), float(value[2]))


def runtime_mesh_entries(runtime_root: Path, chunk: str) -> dict[str, dict[str, Any]]:
    manifest = read_json_if_exists(runtime_root / "chunks" / chunk / "manifest.json")
    if not isinstance(manifest, dict):
        return {}
    meshes = manifest.get("meshes")
    if "meshes" in manifest and isinstance(meshes, dict):
        return {str(key): value for key, value in meshes.items() if isinstance(value, dict)}
    mesh_assets = manifest.get("mesh_assets")
    if isinstance(mesh_assets, dict):
        shared_assets = shared_asset_manifest_entries(runtime_root, manifest)
        resolved: dict[str, dict[str, Any]] = {}
        for mesh_path, ref_value in mesh_assets.items():
            if not isinstance(ref_value, dict):
                continue
            ref = dict(ref_value)
            asset_id = str(ref.get("asset_id", "")).strip()
            shared_entry = shared_assets.get(asset_id, {}) if asset_id else {}
            entry: dict[str, Any] = dict(shared_entry) if isinstance(shared_entry, dict) else {}
            entry.update(ref)
            resolved[str(mesh_path)] = entry
        return resolved
    return {}


def shared_asset_manifest_entries(
    runtime_root: Path, chunk_manifest: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    relative = str(chunk_manifest.get("shared_asset_manifest_relative_path", "")).strip()
    if not relative:
        return {}
    manifest_path = Path(relative)
    if not manifest_path.is_absolute():
        manifest_path = runtime_root / manifest_path
    manifest = read_json_if_exists(manifest_path)
    if not isinstance(manifest, dict):
        return {}
    assets = manifest.get("assets", {})
    if not isinstance(assets, dict):
        return {}
    return {str(key): value for key, value in assets.items() if isinstance(value, dict)}


def placement_records_for_node(
    *,
    node_index: int,
    node: dict[str, Any],
    sgo_manifest: dict[str, Any],
    runtime_meshes: dict[str, dict[str, Any]],
    metadata: StaticMeshMetadata,
    asset_refs: dict[str, int],
    assets: list[dict[str, Any]],
    string_refs: dict[str, int],
    strings: list[str],
) -> dict[str, Any]:
    extras = node.get("extras", {})
    if not isinstance(extras, dict):
        return {"records": [], "skipped": 1}
    object_name = str(node.get("name", f"Object_{node_index}"))
    prefab_name = str(extras.get("prefab_name", "")).strip()
    prefab = sgo_manifest.get(prefab_name, {}) if prefab_name else {}
    components = prefab.get("components", []) if isinstance(prefab, dict) else []
    records: list[dict[str, Any]] = []
    skipped = 0
    if isinstance(components, list) and components:
        for component_index, component in enumerate(components):
            if not isinstance(component, dict):
                skipped += 1
                continue
            if is_hidden_sgo_component(component):
                skipped += 1
                continue
            mesh_path = str(component.get("mesh_path", "")).strip()
            if not mesh_path:
                skipped += 1
                continue
            records.append(
                placement_record(
                    node_index=node_index,
                    component_index=component_index,
                    node=node,
                    object_name=object_name,
                    prefab_name=prefab_name,
                    mesh_path=mesh_path,
                    mesh_name=str(component.get("mesh_name", "")).strip(),
                    visual_tier=str(component.get("visual_tier", "")).strip()
                    or visual_tier_for_mesh(
                        mesh_path,
                        str(component.get("mesh_name", "")).strip(),
                        str(component.get("name", component.get("tag", ""))),
                        component.get("props", {}),
                        metadata,
                    ),
                    runtime_meshes=runtime_meshes,
                    asset_refs=asset_refs,
                    assets=assets,
                    string_refs=string_refs,
                    strings=strings,
                    component=component,
                )
            )
        return {"records": records, "skipped": skipped}

    mesh_path = str(extras.get("mesh_path", "")).strip()
    if not mesh_path:
        return {"records": [], "skipped": 1}
    mesh_name = str(extras.get("mesh_ref", "")).strip()
    records.append(
        placement_record(
            node_index=node_index,
            component_index=-1,
            node=node,
            object_name=object_name,
            prefab_name=prefab_name,
            mesh_path=mesh_path,
            mesh_name=mesh_name,
            visual_tier=visual_tier_for_mesh(mesh_path, mesh_name, object_name, {}, metadata),
            runtime_meshes=runtime_meshes,
            asset_refs=asset_refs,
            assets=assets,
            string_refs=string_refs,
            strings=strings,
            component=None,
        )
    )
    return {"records": records, "skipped": skipped}


def placement_record(
    *,
    node_index: int,
    component_index: int,
    node: dict[str, Any],
    object_name: str,
    prefab_name: str,
    mesh_path: str,
    mesh_name: str,
    visual_tier: str,
    runtime_meshes: dict[str, dict[str, Any]],
    asset_refs: dict[str, int],
    assets: list[dict[str, Any]],
    string_refs: dict[str, int],
    strings: list[str],
    component: dict[str, Any] | None,
) -> list[Any]:
    asset_index = asset_index_for_mesh(
        mesh_path=mesh_path,
        mesh_name=mesh_name,
        visual_tier=visual_tier,
        runtime_meshes=runtime_meshes,
        asset_refs=asset_refs,
        assets=assets,
    )
    record: list[Any] = [
        asset_index,
        node_index,
        component_index,
        intern_string(object_name, string_refs, strings),
        intern_string(prefab_name, string_refs, strings) if prefab_name else -1,
        compact_vector(node.get("translation", [0.0, 0.0, 0.0])),
    ]
    rotation = node.get("rotation", None)
    if isinstance(rotation, list) and len(rotation) == 4:
        record.append(compact_vector(rotation, size=4))
    else:
        record.append(None)
    scale = node.get("scale", None)
    if isinstance(scale, list) and len(scale) == 3 and any(float(value) != 1.0 for value in scale):
        record.append(compact_vector(scale))
    else:
        record.append(None)
    if component is not None:
        compact_component = compact_component_transform(component)
        record.append(compact_component if compact_component else None)
    trim_trailing_nulls(record)
    return record


def intern_string(value: str, string_refs: dict[str, int], strings: list[str]) -> int:
    if value in string_refs:
        return string_refs[value]
    index = len(strings)
    string_refs[value] = index
    strings.append(value)
    return index


def asset_index_for_mesh(
    *,
    mesh_path: str,
    mesh_name: str,
    visual_tier: str,
    runtime_meshes: dict[str, dict[str, Any]],
    asset_refs: dict[str, int],
    assets: list[dict[str, Any]],
) -> int:
    ref_key = f"{mesh_path}\0{mesh_name}\0{visual_tier}"
    if ref_key in asset_refs:
        return asset_refs[ref_key]
    runtime_entry = runtime_meshes.get(mesh_path, {})
    asset: dict[str, Any] = {
        "mesh_path": mesh_path,
        "mesh_name": mesh_name,
        "visual_tier": visual_tier,
    }
    has_asset_id = bool(str(runtime_entry.get("asset_id", "")).strip())
    runtime_keys = ["asset_id", "valid_triangle_indices"]
    if not has_asset_id:
        runtime_keys.extend(["runtime_relative_path", "native_scene_relative_path"])
    for runtime_key in runtime_keys:
        if runtime_key in runtime_entry:
            asset[runtime_key] = runtime_entry[runtime_key]
    asset_index = len(assets)
    asset_refs[ref_key] = asset_index
    assets.append(asset)
    return asset_index


def cell_summary_for_records(
    records: list[Any], assets: list[dict[str, Any]], metadata: StaticMeshMetadata
) -> dict[str, Any]:
    tier_counts: dict[str, int] = {}
    bucket_counts = {bucket: 0 for bucket in TIER_BUCKET_NAMES}
    asset_counts: dict[int, int] = {}
    max_cull_distance = 0.0
    max_sphere_radius = 0.0
    landmark_count = 0
    impostor_chain_count = 0
    collision_required_count = 0
    tree_mesh_count = 0
    for record in records:
        asset_index = record_asset_index(record)
        if asset_index < 0 or asset_index >= len(assets):
            continue
        asset = assets[asset_index]
        entry = asset_metadata_entry(asset, metadata)
        visual_tier = str(asset.get("visual_tier", "")).strip() or visual_tier_for_mesh(
            str(asset.get("mesh_path", "")),
            str(asset.get("mesh_name", "")),
            "",
            {},
            metadata,
        )
        tier_counts[visual_tier] = tier_counts.get(visual_tier, 0) + 1
        bucket_name = tier_bucket_for_asset(asset, metadata)
        bucket_counts[bucket_name] = bucket_counts.get(bucket_name, 0) + 1
        cull_distance = float(entry.get("cull_distance", 0.0))
        sphere_radius = float(entry.get("sphere_radius", 0.0))
        max_cull_distance = max(max_cull_distance, cull_distance)
        max_sphere_radius = max(max_sphere_radius, sphere_radius)
        if visual_tier == "landmark_shell":
            landmark_count += 1
        if has_valid_impostor_chain_for_entry(entry, metadata):
            impostor_chain_count += 1
        if asset_has_collision(asset):
            collision_required_count += 1
        if asset_is_tree_mesh(asset, entry):
            tree_mesh_count += 1
        asset_counts[asset_index] = asset_counts.get(asset_index, 0) + 1

    repeated_asset_count = sum(1 for count in asset_counts.values() if count > 1)
    return {
        "placement_count": len(records),
        "tier_counts": tier_counts,
        "tier_bucket_counts": bucket_counts,
        "max_cull_distance": round(max_cull_distance, 3),
        "max_sphere_radius": round(max_sphere_radius, 3),
        "landmark_count": landmark_count,
        "impostor_chain_count": impostor_chain_count,
        "collision_required_count": collision_required_count,
        "tree_mesh_count": tree_mesh_count,
        "tree_billboard_count": 0,
        "repeated_asset_count": repeated_asset_count,
        "unique_asset_count": len(asset_counts),
    }


def tier_buckets_for_records(
    records: list[Any], assets: list[dict[str, Any]], metadata: StaticMeshMetadata
) -> dict[str, list[int]]:
    buckets: dict[str, list[int]] = {bucket: [] for bucket in TIER_BUCKET_NAMES}
    for record_index, record in enumerate(records):
        asset_index = record_asset_index(record)
        if asset_index < 0 or asset_index >= len(assets):
            continue
        bucket_name = tier_bucket_for_asset(assets[asset_index], metadata)
        buckets.setdefault(bucket_name, []).append(record_index)
    return {bucket: values for bucket, values in buckets.items() if values}


def tier_lod_buckets_for_records(
    records: list[Any],
    assets: list[dict[str, Any]],
    metadata: StaticMeshMetadata,
    runtime_meshes: dict[str, dict[str, Any]],
    asset_refs: dict[str, int],
) -> dict[str, list[list[int]]]:
    buckets: dict[str, list[list[int]]] = {bucket: [] for bucket in TIER_BUCKET_NAMES}
    for record_index, record in enumerate(records):
        asset_index = record_asset_index(record)
        if asset_index < 0 or asset_index >= len(assets):
            continue
        asset = assets[asset_index]
        bucket_name = tier_bucket_for_asset(asset, metadata)
        selected_asset_index = selected_lod_asset_index_for_bucket(
            asset_index,
            bucket_name,
            assets,
            metadata,
            runtime_meshes,
            asset_refs,
        )
        buckets.setdefault(bucket_name, []).append([record_index, selected_asset_index])
    return {bucket: values for bucket, values in buckets.items() if values}


def selected_lod_asset_index_for_bucket(
    asset_index: int,
    bucket_name: str,
    assets: list[dict[str, Any]],
    metadata: StaticMeshMetadata,
    runtime_meshes: dict[str, dict[str, Any]],
    asset_refs: dict[str, int],
) -> int:
    if asset_index < 0 or asset_index >= len(assets):
        return asset_index
    asset = assets[asset_index]
    levels = []
    if metadata is not None:
        levels = metadata.lod_levels(
            str(asset.get("mesh_path", "")), str(asset.get("mesh_name", ""))
        )
    if not levels:
        return asset_index
    policy = TIER_BUCKET_LOD_POLICIES.get(bucket_name, "highest")
    if policy == "lowest":
        selected_level = levels[-1]
    elif policy == "middle":
        selected_level = levels[len(levels) // 2]
    else:
        selected_level = levels[0]
    selected_path = str(selected_level.get("mesh_path", "")).strip()
    if not selected_path:
        return asset_index
    selected_name = str(selected_level.get("name", "")).strip() or Path(selected_path).stem
    return asset_index_for_mesh(
        mesh_path=selected_path,
        mesh_name=selected_name,
        visual_tier=str(asset.get("visual_tier", "")),
        runtime_meshes=runtime_meshes,
        asset_refs=asset_refs,
        assets=assets,
    )


def tier_bucket_for_asset(asset: dict[str, Any], metadata: StaticMeshMetadata) -> str:
    visual_tier = str(asset.get("visual_tier", "")).strip()
    entry = asset_metadata_entry(asset, metadata)
    cull_distance = float(entry.get("cull_distance", 0.0))
    radius = float(entry.get("sphere_radius", 0.0))
    if visual_tier == "landmark_shell":
        return "tier_0_landmark"
    if cull_distance >= 100000.0 or radius >= 2000.0:
        return "tier_1_far"
    if cull_distance >= 30000.0 or radius >= 1000.0:
        return "tier_2_mid"
    if visual_tier in {"near_decor", "interior_clutter", "light_fx"}:
        return "tier_4_detail"
    return "tier_3_near"


def record_asset_index(record: Any) -> int:
    if isinstance(record, list) and record:
        return int(record[0])
    if isinstance(record, dict):
        return int(record.get("asset_index", record.get("asset", -1)))
    return -1


def asset_metadata_entry(asset: dict[str, Any], metadata: StaticMeshMetadata) -> dict[str, Any]:
    if metadata is None:
        return {}
    return metadata.lookup(str(asset.get("mesh_path", "")), str(asset.get("mesh_name", "")))


def asset_has_collision(asset: dict[str, Any]) -> bool:
    triangle_indices = asset.get("valid_triangle_indices", [])
    return isinstance(triangle_indices, list) and bool(triangle_indices)


def asset_is_tree_mesh(asset: dict[str, Any], metadata_entry: dict[str, Any]) -> bool:
    mesh_path = str(asset.get("mesh_path", ""))
    mesh_name = str(asset.get("mesh_name", ""))
    metadata_name = str(metadata_entry.get("name", ""))
    metadata_package = str(metadata_entry.get("package_name", ""))
    normalized_path = mesh_path.lower().replace("\\", "/")
    if (
        "/speedtree" in normalized_path
        or "/trees/" in normalized_path
        or "/tree/" in normalized_path
    ):
        return True
    haystack = f"{mesh_path} {mesh_name} {metadata_name} {metadata_package}".lower()
    normalized = haystack
    for delimiter in ["/", "\\", ".", "_", "-", "(", ")", "[", "]", "{", "}"]:
        normalized = normalized.replace(delimiter, " ")
    for token in normalized.split():
        token = token.strip()
        if not token:
            continue
        if token in TREE_MESH_NAME_TOKENS:
            return True
        for prefix in TREE_MESH_PREFIX_TOKENS:
            if token.startswith(prefix) and len(token) <= len(prefix) + 10:
                return True
    return False


def has_valid_impostor_chain(
    mesh_path: str, mesh_name: str, metadata: StaticMeshMetadata
) -> bool:
    if metadata is None:
        return False
    return has_valid_impostor_chain_for_entry(metadata.lookup(mesh_path, mesh_name), metadata)


def has_valid_impostor_chain_for_entry(
    entry: dict[str, Any], metadata: StaticMeshMetadata
) -> bool:
    if metadata is None or not entry:
        return False
    impostor_index = int(entry.get("impostor", -1))
    if impostor_index < 0:
        return False
    switch_distance = float(entry.get("impostor_distance", 0.0))
    if switch_distance <= 0.0:
        return False
    package_name = str(entry.get("package_name", ""))
    target = metadata.by_package_index.get(
        metadata._package_index_key(package_name, impostor_index), {}
    )
    return bool(target)


def compact_component_transform(component: dict[str, Any]) -> list[Any]:
    result: list[Any] = [None, None, None, None]
    location = component.get("location", None)
    if isinstance(location, list) and len(location) == 3 and any(float(value) != 0.0 for value in location):
        result[0] = compact_vector(location)
    rotation = component.get("rotation", None)
    if isinstance(rotation, list) and len(rotation) == 3 and any(int(value) != 0 for value in rotation):
        result[1] = compact_vector(rotation)
    draw_scale_3d = component.get("draw_scale_3d", None)
    if (
        isinstance(draw_scale_3d, list)
        and len(draw_scale_3d) == 3
        and any(float(value) != 1.0 for value in draw_scale_3d)
    ):
        result[2] = compact_vector(draw_scale_3d)
    draw_scale = component.get("draw_scale", None)
    if isinstance(draw_scale, (int, float)) and float(draw_scale) != 1.0:
        result[3] = float(draw_scale)
    trim_trailing_nulls(result)
    return result


def trim_trailing_nulls(values: list[Any]) -> None:
    while values and values[-1] is None:
        values.pop()


def compact_vector(values: Any, size: int = 3) -> list[float]:
    if not isinstance(values, list):
        return [0.0 for _ in range(size)]
    return [round(float(values[index]), 6) for index in range(min(size, len(values)))]


def visual_tier_for_mesh(
    mesh_path: str,
    mesh_name: str,
    object_name: str,
    props: Any,
    metadata: StaticMeshMetadata,
) -> str:
    entry = metadata.lookup(mesh_path, mesh_name) if metadata is not None else {}
    cull_distance = max(
        float(entry.get("cull_distance", 0.0)),
        props_float(props, "CullDistance", 0.0),
    )
    radius = float(entry.get("sphere_radius", 0.0))
    detail_level = int(entry.get("mesh_detail_level", 0))
    has_impostor_chain = has_valid_impostor_chain_for_entry(entry, metadata)
    mesh_haystack = (
        f"{mesh_path} {mesh_name} {str(entry.get('package_name', ''))}".lower()
    )
    haystack = (
        f"{mesh_path} {mesh_name} {object_name} "
        f"{str(entry.get('package_name', ''))} {str(props_value(props, 'Tag', ''))}"
    ).lower()
    if (
        contains_any(mesh_haystack, ["light", "lights", "ember", "fire", "candle", "torch", "brazier", "lantern"])
        and radius < 1800.0
        and cull_distance < 80000.0
    ):
        return "light_fx"
    structural_name = contains_any(
        mesh_haystack,
        [
            "_ext",
            "_ro",
            "_bldg",
            "_bridge",
            "_floor",
            "_wall",
            "_stairs",
            "_porch",
            "_cave",
            "_plateau",
            "_door",
            "_gate",
            "_roof",
            "_hw",
            "grid_",
        ],
    )
    clutter_name = contains_any(
        haystack,
        [
            "artifact",
            "jar",
            "jars",
            "pillow",
            "tool",
            "fishingnet",
            "bone",
            "cage",
            "debris",
            "decor",
            "plank",
            "beam",
            "rope",
            "cloth",
            "banner",
            "barrel",
            "crate",
            "basket",
            "table",
            "chair",
            "bench",
            "book",
            "bottle",
            "pot",
            "pan",
            "knife",
            "fork",
            "cup",
            "plate",
            "bowl",
            "rug",
            "misc",
        ],
    )
    if (
        has_impostor_chain
        and radius >= LANDMARK_SHELL_MIN_RADIUS
        and cull_distance >= LANDMARK_SHELL_MIN_CULL_DISTANCE
    ):
        return "landmark_shell"
    if structural_name and (radius >= 300.0 or cull_distance >= 12000.0 or not clutter_name):
        return "near_structure"
    if clutter_name:
        if radius >= 1500.0 or cull_distance >= 50000.0:
            return "near_structure"
        if radius > 0.0 and radius <= 300.0 and cull_distance <= 5000.0:
            return "interior_clutter"
        return "near_decor"
    if detail_level > 1 and radius < 1000.0 and cull_distance < 30000.0:
        return "near_decor"
    if radius > 0.0 and radius <= 300.0 and cull_distance <= 5000.0:
        return "interior_clutter"
    return "near_structure"


def props_value(props: Any, key: str, default: Any) -> Any:
    if isinstance(props, dict):
        return props.get(key, default)
    return default


def props_float(props: Any, key: str, default: float) -> float:
    value = props_value(props, key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def contains_any(haystack: str, needles: list[str]) -> bool:
    return any(needle in haystack for needle in needles)


def node_position(node: dict[str, Any]) -> tuple[float, float, float]:
    translation = node.get("translation", [])
    if isinstance(translation, list) and len(translation) >= 3:
        return (float(translation[0]), float(translation[1]), float(translation[2]))
    return (0.0, 0.0, 0.0)


def add_vector(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


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


def cell_key_for_position(position: tuple[float, float, float], cell_size: float) -> str:
    cell_x = int(position[0] // cell_size)
    cell_z = int(position[2] // cell_size)
    return f"{cell_x}:{cell_z}"


if __name__ == "__main__":
    raise SystemExit(main())
