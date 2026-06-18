#!/usr/bin/env python3
"""Audit TerrainInfo native parsing coverage across Vanguard terrain chunks."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from scripts.lib.terraininfo_native import (  # noqa: E402
    find_decoinstance_array,
    find_native_body_offsets,
    import_name_lookup,
    parse_terraininfo_native,
    static_mesh_import_names,
)
from ue2.package import UE2Package  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Parse TerrainInfo native data and report chunks where native "
            "vegetation extraction may be incomplete."
        )
    )
    parser.add_argument(
        "--maps-dir",
        default=config.MAPS_DIR,
        help="Directory containing chunk_*.vgr files.",
    )
    parser.add_argument(
        "--chunk",
        action="append",
        default=[],
        help="Chunk name or .vgr path to audit. May be passed more than once.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of chunks to scan when --chunk is not provided.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional path to write full audit records as JSON.",
    )
    parser.add_argument(
        "--allow-suspicious",
        action="store_true",
        help="Return success even when suspicious chunks are found.",
    )
    args = parser.parse_args()

    paths = resolve_chunk_paths(Path(args.maps_dir).expanduser(), args.chunk)
    if not paths:
        paths = sorted(Path(args.maps_dir).expanduser().glob("chunk_*.vgr"))
        if args.limit > 0:
            paths = paths[: args.limit]

    records = [audit_chunk(path) for path in paths]
    summary = Counter(record["status"] for record in records)
    suspicious = [record for record in records if record.get("suspicious")]

    print(
        "TerrainInfo native audit: "
        f"chunks={len(records)} "
        f"parsed_with_deco={summary['parsed_with_deco']} "
        f"parsed_no_deco={summary['parsed_no_deco']} "
        f"no_mapping_valid_static_deco={summary['no_mapping_valid_static_deco']} "
        f"no_mapping_no_deco={summary['no_mapping_no_deco']} "
        f"no_terraininfo={summary['no_terraininfo']} "
        f"errors={summary['error']}"
    )

    for record in suspicious[:20]:
        print(
            "  Suspicious: "
            f"{record['chunk']} status={record['status']} "
            f"native_candidates={record['native_body_candidates']} "
            f"static_mesh_imports={record['static_mesh_import_count']} "
            f"static_deco_count={record.get('static_deco_count')}"
        )
    if len(suspicious) > 20:
        print(f"  ... {len(suspicious) - 20} more suspicious chunks")

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(records, indent=2) + "\n")
        print(f"Wrote audit JSON: {args.json_output}")

    if suspicious and not args.allow_suspicious:
        return 1
    return 0


def resolve_chunk_paths(maps_dir: Path, chunks: list[str]) -> list[Path]:
    paths: list[Path] = []
    for chunk in chunks:
        candidate = Path(chunk).expanduser()
        if candidate.exists():
            paths.append(candidate)
            continue
        name = candidate.stem
        if not name.startswith("chunk_"):
            name = f"chunk_{name}"
        paths.append(maps_dir / f"{name}.vgr")
    return paths


def audit_chunk(path: Path) -> dict:
    record = {
        "chunk": path.stem,
        "path": str(path),
        "status": "error",
        "suspicious": False,
        "native_body_candidates": [],
        "native_body_offset": None,
        "mesh_lookup_count": 0,
        "static_mesh_import_count": 0,
        "deco_array_offset": None,
        "deco_count": 0,
        "deco_validation_ratio": 0.0,
        "static_deco_count": None,
        "warnings": [],
    }
    if not path.exists():
        record["warnings"].append("chunk file not found")
        return record

    try:
        pkg = UE2Package(str(path))
    except Exception as exc:
        record["warnings"].append(f"package parse failed: {exc}")
        return record

    ti_export = next(
        (exp for exp in pkg.exports if exp.get("class_name") == "TerrainInfo"), None
    )
    if ti_export is None:
        record["status"] = "no_terraininfo"
        return record

    try:
        ti_data = pkg.get_export_data(ti_export)
    except Exception as exc:
        record["warnings"].append(f"TerrainInfo export read failed: {exc}")
        return record

    static_meshes = static_mesh_import_names(pkg.imports)
    record["static_mesh_import_count"] = len(static_meshes)
    record["native_body_candidates"] = find_native_body_offsets(ti_data)

    native_parse = parse_terraininfo_native(ti_data, import_name_lookup(pkg.imports))
    record["warnings"] = list(native_parse.warnings)
    record["native_body_offset"] = native_parse.native_body_offset
    record["mesh_lookup_count"] = native_parse.mesh_lookup_count

    if native_parse.deco_array is not None:
        record["status"] = "parsed_with_deco"
        record["deco_array_offset"] = native_parse.deco_array.array_offset
        record["deco_count"] = native_parse.deco_array.count
        record["deco_validation_ratio"] = native_parse.deco_array.validation_ratio
        return record

    static_deco = (
        find_decoinstance_array(ti_data, len(static_meshes)) if static_meshes else None
    )
    if static_deco is not None:
        record["static_deco_count"] = static_deco.count

    if native_parse.mesh_lookup:
        record["status"] = "parsed_no_deco"
    elif static_deco is not None:
        record["status"] = "no_mapping_valid_static_deco"
        record["suspicious"] = True
    else:
        record["status"] = "no_mapping_no_deco"
    return record


if __name__ == "__main__":
    raise SystemExit(main())
