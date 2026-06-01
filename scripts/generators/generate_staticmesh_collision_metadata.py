#!/usr/bin/env python3
"""Build compact Godot metadata for StaticMesh section collision flags.

The input corpus audit is intentionally verbose. This generator keeps only the
records that change runtime behavior: meshes with at least one disabled
Collision slot, authored collision-like names, or simple collision flags.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO_ROOT / "ghidra/output/staticmesh_collision_corpus_audit.json"
DEFAULT_OUTPUT = REPO_ROOT / "output" / "data" / "staticmesh_collision.json"


def mesh_key(package_name: str, mesh_name: str) -> str:
    return f"{package_name.strip().lower()}/{Path(mesh_name.strip()).stem.lower()}"


def compact_record(record: dict[str, Any]) -> dict[str, Any] | None:
    package_file = str(record.get("package", "")).strip()
    mesh_name = str(record.get("mesh", "")).strip()
    if not package_file or not mesh_name:
        return None

    package_name = Path(package_file).stem
    properties = record.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    collision = properties.get("collision")
    if not isinstance(collision, dict):
        collision = {}
    raw_slots = collision.get("enable_values", [])
    slots = [bool(value) for value in raw_slots if isinstance(value, bool)]
    enabled_count = sum(1 for value in slots if value)
    disabled_count = sum(1 for value in slots if not value)

    simple_flags = properties.get("simple_flags", {})
    if not isinstance(simple_flags, dict):
        simple_flags = {}
    simple_flags = {
        str(name): bool(value)
        for name, value in sorted(simple_flags.items())
        if isinstance(value, bool)
    }
    simple_true = any(simple_flags.values())
    authored_collision_name = bool(record.get("authored_collision_like_name", False))

    if not slots and not simple_true and not authored_collision_name:
        return None
    if slots and disabled_count == 0 and not simple_true and not authored_collision_name:
        return None

    geometry = record.get("geometry", {})
    if not isinstance(geometry, dict):
        geometry = {}
    correlation = record.get("collision_geometry_correlation", {})
    if not isinstance(correlation, dict):
        correlation = {}

    output = {
        "package_name": package_name,
        "mesh": mesh_name,
        "slots": slots,
        "slot_count": len(slots),
        "enabled_count": enabled_count,
        "disabled_count": disabled_count,
        "any_enabled": bool(slots and enabled_count > 0),
        "all_enabled": bool(slots and disabled_count == 0),
        "simple_flags": simple_flags,
        "authored_collision_like_name": authored_collision_name,
    }

    if geometry.get("status") == "ok":
        output["section_count"] = int(geometry.get("section_count", 0))
        output["first_skin_material_count"] = int(geometry.get("first_skin_material_count", 0))
        output["zero_face_section_count"] = int(geometry.get("zero_face_section_count", 0))
    if correlation:
        output["matches_section_count"] = bool(correlation.get("matches_section_count", False))
        output["matches_first_skin_material_count"] = bool(
            correlation.get("matches_first_skin_material_count", False)
        )
    return output


def build_metadata(data: dict[str, Any], input_path: Path) -> dict[str, Any]:
    records = data.get("records", [])
    if not isinstance(records, list):
        raise ValueError(f"{input_path} does not contain a records list")

    meshes: dict[str, dict[str, Any]] = {}
    included_disabled = 0
    included_all_disabled = 0
    included_simple_true = 0
    included_authored_name = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        compact = compact_record(record)
        if compact is None:
            continue
        key = mesh_key(str(compact["package_name"]), str(compact["mesh"]))
        meshes[key] = compact
        if int(compact.get("disabled_count", 0)) > 0:
            included_disabled += 1
        if int(compact.get("slot_count", 0)) > 0 and int(compact.get("enabled_count", 0)) == 0:
            included_all_disabled += 1
        simple_flags = compact.get("simple_flags", {})
        if isinstance(simple_flags, dict) and any(bool(value) for value in simple_flags.values()):
            included_simple_true += 1
        if bool(compact.get("authored_collision_like_name", False)):
            included_authored_name += 1

    source_summary = data.get("summary", {})
    if not isinstance(source_summary, dict):
        source_summary = {}
    return {
        "version": 1,
        "description": "StaticMesh Collision.Enable Collision flags for Godot runtime filtering.",
        "source": str(input_path.relative_to(REPO_ROOT) if input_path.is_relative_to(REPO_ROOT) else input_path),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": {
            "source_total_staticmesh_exports": int(source_summary.get("total_staticmesh_exports", 0)),
            "source_exports_with_collision_property": int(
                source_summary.get("exports_with_collision_property", 0)
            ),
            "source_collision_property_records_any_disabled": int(
                source_summary.get("collision_property_records_any_disabled", 0)
            ),
            "mesh_records_included": len(meshes),
            "mesh_records_with_disabled_slots": included_disabled,
            "mesh_records_all_slots_disabled": included_all_disabled,
            "mesh_records_with_simple_true_flags": included_simple_true,
            "mesh_records_with_authored_collision_like_name": included_authored_name,
        },
        "meshes": dict(sorted(meshes.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    with args.input.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    metadata = build_metadata(data, args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata["summary"], indent=2))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
