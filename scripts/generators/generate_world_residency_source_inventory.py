#!/usr/bin/env python3
"""Publish the extractor-owned source inventory used by world residency packs.

The inventory is derived from Vanguard source packages, not from whichever
generated terrain files happen to exist.  A chunk is terrain-bearing when its
source ``.vgr`` package contains exactly one ``TerrainInfo`` export.  Generated
terrain/object inputs are checked separately so a stale or partial extraction
cannot masquerade as complete source coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from ue2 import UE2Package  # noqa: E402
from scripts.lib.world_residency_identity import (  # noqa: E402
    authoritative_source_terrain_id,
)


SCHEMA = "vanguard_source_terrain_inventory"
VERSION = 1
LEGACY_DISCOVERY_POLICY = "ue2_chunk_package_single_terraininfo_export_v1"
PARTITIONED_DISCOVERY_POLICY = (
    "vgo_world_chunk_catalog_continent_then_ue2_single_terraininfo_v1"
)
PARTITION_POLICY = "vgo_world_chunks_continent_v1"
_SQL_STRING = r"'((?:[^'\\]|\\.)*)'"
_CHUNK_ROW_PATTERN = re.compile(
    r"\((\d+),"
    + _SQL_STRING
    + ","
    + _SQL_STRING
    + ","
    + _SQL_STRING
    + ","
    + _SQL_STRING
    + r",(-?\d+),(-?\d+),"
)


def main() -> int:
    args = parse_args()
    try:
        inventory = build_inventory(
            maps_root=args.maps_root.resolve(),
            output_root=args.output_root.resolve(),
            runtime_root=args.runtime_root.resolve(),
            space_asset_id=args.space_asset_id,
            source_zone_asset_id=args.source_zone_asset_id,
            require_generated_inputs=not args.allow_missing_generated_inputs,
            chunk_catalog_path=(
                args.chunk_catalog_sql.resolve() if args.chunk_catalog_sql else None
            ),
            source_continent=args.source_continent,
        )
        write_json_atomic(args.output.resolve(), inventory)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        "World residency source inventory: "
        f"terrain={inventory['chunk_count']} "
        f"excluded={inventory['excluded_source_chunk_count']} "
        f"inventory={inventory['inventory_id']} output={args.output.resolve()}"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maps-root", type=Path, default=Path(config.MAPS_DIR))
    parser.add_argument("--output-root", type=Path, default=Path(config.OUTPUT_DIR))
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path(config.OUTPUT_DIR) / "godot_runtime",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(config.OUTPUT_DIR)
        / "world_residency"
        / "source_terrain_inventory.json",
    )
    parser.add_argument("--space-asset-id", required=True)
    parser.add_argument("--source-zone-asset-id", required=True)
    parser.add_argument(
        "--chunk-catalog-sql",
        type=Path,
        help=(
            "VGO world SQL dump containing the authoritative chunks.continent "
            "partition. Requires --source-continent."
        ),
    )
    parser.add_argument(
        "--source-continent",
        default=None,
        help=(
            "Exact chunks.continent value to publish (an explicitly empty value is "
            "valid). Requires --chunk-catalog-sql."
        ),
    )
    parser.add_argument(
        "--allow-missing-generated-inputs",
        action="store_true",
        help=(
            "Publish source discovery even when generated terrain/object inputs are "
            "incomplete. Such an inventory will be marked not build-ready."
        ),
    )
    return parser.parse_args()


def build_inventory(
    *,
    maps_root: Path,
    output_root: Path,
    runtime_root: Path,
    space_asset_id: str,
    source_zone_asset_id: str,
    require_generated_inputs: bool,
    chunk_catalog_path: Path | None = None,
    source_continent: str | None = None,
) -> dict[str, Any]:
    space_asset_id = str(space_asset_id).strip()
    source_zone_asset_id = str(source_zone_asset_id).strip()
    if not space_asset_id:
        raise ValueError("space_asset_id is empty")
    if not source_zone_asset_id:
        raise ValueError("source_zone_asset_id is empty")
    if not maps_root.is_dir():
        raise ValueError(f"maps root does not exist: {maps_root}")

    source_continent_was_provided = source_continent is not None
    source_continent = "" if source_continent is None else str(source_continent).strip()
    if (chunk_catalog_path is not None) != source_continent_was_provided:
        raise ValueError(
            "chunk_catalog_path and source_continent must be provided together"
        )

    all_source_paths = sorted(
        maps_root.glob("chunk_*.vgr"), key=lambda path: path.name.lower()
    )
    if not all_source_paths:
        raise ValueError(f"maps root contains no chunk_*.vgr packages: {maps_root}")

    discovery_policy = LEGACY_DISCOVERY_POLICY
    source_catalog: dict[str, Any] | None = None
    catalog_by_chunk: dict[str, dict[str, Any]] = {}
    source_paths = all_source_paths
    if chunk_catalog_path is not None:
        chunk_catalog_path = chunk_catalog_path.resolve()
        catalog_by_chunk, source_catalog = read_vgo_chunk_catalog(chunk_catalog_path)
        missing_catalog_chunks = sorted(
            normalize_chunk_name(path.stem)
            for path in all_source_paths
            if normalize_chunk_name(path.stem) not in catalog_by_chunk
        )
        if missing_catalog_chunks:
            raise ValueError(
                f"{len(missing_catalog_chunks)} source chunk packages are absent from "
                f"the VGO chunk catalog: {missing_catalog_chunks[:8]}"
            )
        available_continents = sorted(
            {
                str(catalog_by_chunk[normalize_chunk_name(path.stem)]["continent"])
                for path in all_source_paths
            }
        )
        if source_continent not in available_continents:
            raise ValueError(
                f"source continent {source_continent!r} is absent from mapped source "
                f"chunks; available={available_continents}"
            )
        source_paths = [
            path
            for path in all_source_paths
            if str(catalog_by_chunk[normalize_chunk_name(path.stem)]["continent"])
            == source_continent
        ]
        discovery_policy = PARTITIONED_DISCOVERY_POLICY

    chunks: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    missing_generated_inputs: list[dict[str, Any]] = []
    for source_path in source_paths:
        chunk = normalize_chunk_name(source_path.stem)
        package = UE2Package(str(source_path))
        terrain_exports = [
            (index, value)
            for index, value in enumerate(package.exports)
            if str(value.get("class_name", "")) == "TerrainInfo"
        ]
        if not terrain_exports:
            excluded.append(
                {
                    "chunk": chunk,
                    "source_package_relative_path": f"Maps/{source_path.name}",
                    "reason": "no_terraininfo_export",
                }
            )
            continue
        if len(terrain_exports) != 1:
            names = [str(value.get("object_name", "")) for _, value in terrain_exports]
            raise ValueError(
                f"{source_path.name} has {len(terrain_exports)} TerrainInfo exports: {names}"
            )
        export_index, terrain_export = terrain_exports[0]
        export_name = str(terrain_export.get("object_name", "")).strip()
        if not export_name:
            raise ValueError(f"{source_path.name} TerrainInfo export has no object name")

        source_package_relative_path = f"Maps/{source_path.name}"
        terrain_id = authoritative_source_terrain_id(
            source_package_relative_path=source_package_relative_path,
            export_name=export_name,
        )
        terrain_relative_path = f"terrain/terrain_grid/{chunk}_terrain.glb"
        object_index_relative_path = f"godot_runtime/chunks/{chunk}/object_cells.json"
        missing = []
        if not (output_root / terrain_relative_path).is_file():
            missing.append(terrain_relative_path)
        if not (output_root / object_index_relative_path).is_file():
            missing.append(object_index_relative_path)
        if missing:
            missing_generated_inputs.append({"chunk": chunk, "missing": missing})

        descriptor = {
            "chunk": chunk,
            "authoritative_source_terrain_id": terrain_id,
            "source_package_relative_path": source_package_relative_path,
            "source_export_class": "TerrainInfo",
            "source_export_name": export_name,
            "source_export_index": export_index,
            "terrain_output_relative_path": terrain_relative_path,
            "object_index_relative_path": object_index_relative_path,
        }
        if catalog_by_chunk:
            catalog_record = catalog_by_chunk[chunk]
            descriptor.update(
                {
                    "source_chunk_catalog_id": catalog_record["chunk_id"],
                    "source_chunk_shortname": catalog_record["shortname"],
                    "source_chunk_displayname": catalog_record["displayname"],
                    "source_continent": catalog_record["continent"],
                }
            )
        chunks.append(descriptor)

    chunks.sort(key=lambda value: value["chunk"])
    excluded.sort(key=lambda value: value["chunk"])
    missing_generated_inputs.sort(key=lambda value: value["chunk"])
    if not chunks:
        raise ValueError(f"no terrain-bearing source chunks were found in {maps_root}")
    if require_generated_inputs and missing_generated_inputs:
        preview = "; ".join(
            f"{value['chunk']}: {', '.join(value['missing'])}"
            for value in missing_generated_inputs[:8]
        )
        raise ValueError(
            f"{len(missing_generated_inputs)} source terrain chunks lack generated inputs: {preview}"
        )

    identity_payload = {
        "schema": SCHEMA,
        "version": VERSION,
        "discovery_policy": discovery_policy,
        "space_asset_id": space_asset_id,
        "source_zone_asset_id": source_zone_asset_id,
        "source_terrain_ids": [
            [value["chunk"], value["authoritative_source_terrain_id"]] for value in chunks
        ],
    }
    inventory_id = f"terrain_inventory_{canonical_sha256(identity_payload)[:32]}"
    result = {
        "schema": SCHEMA,
        "version": VERSION,
        "inventory_id": inventory_id,
        "discovery_policy": discovery_policy,
        "space_asset_id": space_asset_id,
        "source_zone_asset_id": source_zone_asset_id,
        "source_package_glob": "Maps/chunk_*.vgr",
        "source_chunk_count": len(source_paths),
        "chunk_count": len(chunks),
        "chunks": chunks,
        "excluded_source_chunk_count": len(excluded),
        "excluded_source_chunks": excluded,
        "generated_inputs_complete": not missing_generated_inputs,
        "missing_generated_input_count": len(missing_generated_inputs),
        "missing_generated_inputs": missing_generated_inputs,
    }
    if source_catalog is not None:
        source_catalog = dict(source_catalog)
        source_catalog.update(
            {
                "partition_policy": PARTITION_POLICY,
                "source_continent": source_continent,
                "all_source_chunk_count": len(all_source_paths),
                "selected_source_chunk_count": len(source_paths),
                "other_partition_source_chunk_count": (
                    len(all_source_paths) - len(source_paths)
                ),
            }
        )
        result["source_partition"] = source_catalog
    return result


def read_vgo_chunk_catalog(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read the bounded chunks INSERT without parsing the complete SQL dump."""

    if not path.is_file():
        raise ValueError(f"VGO chunk catalog SQL does not exist: {path}")
    digest = hashlib.sha256()
    chunks_insert = ""
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                digest.update(raw_line)
                if raw_line.startswith(b"INSERT INTO `chunks` VALUES "):
                    if chunks_insert:
                        raise ValueError(
                            f"VGO chunk catalog contains multiple chunks INSERT lines: {path}"
                        )
                    chunks_insert = raw_line.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"could not read VGO chunk catalog {path}: {error}") from error
    if not chunks_insert:
        raise ValueError(f"VGO chunk catalog contains no chunks INSERT line: {path}")

    records: dict[str, dict[str, Any]] = {}
    for match in _CHUNK_ROW_PATTERN.finditer(chunks_insert):
        (
            chunk_id,
            continent,
            shortname,
            displayname,
            filename,
            coord_x,
            coord_y,
        ) = match.groups()
        if not filename.lower().startswith("chunk_"):
            continue
        chunk = normalize_chunk_name(filename)
        record = {
            "chunk_id": int(chunk_id),
            "continent": _unescape_mysql_string(continent),
            "shortname": _unescape_mysql_string(shortname),
            "displayname": _unescape_mysql_string(displayname),
            "filename": chunk,
            "coord_x": int(coord_x),
            "coord_y": int(coord_y),
        }
        expected_coords = chunk_coordinates(chunk)
        if expected_coords != (record["coord_x"], record["coord_y"]):
            raise ValueError(
                f"VGO chunk catalog filename/coordinate mismatch: {chunk}: "
                f"name={expected_coords} row={(record['coord_x'], record['coord_y'])}"
            )
        if chunk in records:
            raise ValueError(f"VGO chunk catalog contains duplicate filename: {chunk}")
        records[chunk] = record
    if not records:
        raise ValueError(f"VGO chunk catalog contains no chunk rows: {path}")
    return records, {
        "kind": "vgo_world_sql_chunks_table",
        "source_file_name": path.name,
        "source_revision": f"sha256:{digest.hexdigest()}",
        "catalog_chunk_count": len(records),
    }


def chunk_coordinates(chunk: str) -> tuple[int, int]:
    parts = normalize_chunk_name(chunk).removeprefix("chunk_").split("_")
    if len(parts) != 2:
        raise ValueError(f"chunk name does not contain two coordinates: {chunk!r}")

    def coordinate(value: str) -> int:
        if value.startswith("n"):
            value = "-" + value[1:]
        try:
            return int(value)
        except ValueError as error:
            raise ValueError(f"invalid chunk coordinate: {chunk!r}") from error

    return coordinate(parts[0]), coordinate(parts[1])


def _unescape_mysql_string(value: str) -> str:
    return value.replace("\\'", "'").replace("\\\\", "\\")


def normalize_chunk_name(value: str) -> str:
    chunk = str(value).strip().lower().replace("-", "_")
    if not chunk.startswith("chunk_"):
        chunk = f"chunk_{chunk}"
    if not chunk or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in chunk):
        raise ValueError(f"invalid chunk name: {value!r}")
    return chunk


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(canonical_json_bytes(value) + b"\n")
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
