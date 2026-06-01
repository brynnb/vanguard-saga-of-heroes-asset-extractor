#!/usr/bin/env python3
"""Generate a Godot-facing StaticMeshAsync source offset index.

The original StaticMeshAsync.tab stays as the archival source. This sidecar
keeps the practical runtime/build value in a compact TSV: package/object ->
serialized source package byte range.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.debug.parse_client_tab_tables import (  # noqa: E402
    DEFAULT_TABLE_ROOT,
    BinaryRecord,
    _parse_binary_table,
)


DEFAULT_OUTPUT = REPO_ROOT / "output" / "data" / "staticmesh_source_index.tsv"
SCHEMA_VERSION = 1
FIELD_NAMES = [
    "schema_version",
    "record_index",
    "record_offset",
    "record_type",
    "asset_ref",
    "asset_class",
    "package",
    "group_path",
    "object_name",
    "block_count",
    "block_index",
    "block_flag",
    "serial_offset",
    "serial_offset_duplicate",
    "serial_size",
    "package_object_key",
    "qualified_key",
]


def package_object_key(package_name: str, object_name: str) -> str:
    return f"{package_name.strip().lower()}/{object_name.strip().lower()}"


def qualified_key(record: BinaryRecord) -> str:
    parts = [record.package.strip().lower()]
    if record.group_path.strip():
        parts.extend(part.strip().lower() for part in record.group_path.split(".") if part)
    parts.append(record.object_name.strip().lower())
    return "/".join(part for part in parts if part)


def record_to_entry(record: BinaryRecord) -> dict[str, Any]:
    block = record.blocks[0] if record.blocks else None
    entry: dict[str, Any] = {
        "record_index": record.record_index,
        "record_offset": record.record_offset,
        "record_type": record.record_type,
        "asset_ref": record.asset_ref,
        "asset_class": record.asset_class,
        "package": record.package,
        "group_path": record.group_path,
        "object_name": record.object_name,
        "block_count": len(record.blocks),
        "package_object_key": package_object_key(record.package, record.object_name),
        "qualified_key": qualified_key(record),
    }
    if block is not None:
        entry.update(
            {
                "block_index": block.block_index,
                "block_flag": block.block_flag,
                "serial_offset": block.offset_a,
                "serial_offset_duplicate": block.offset_b,
                "serial_size": block.size,
            }
        )
    else:
        entry.update(
            {
                "block_index": "",
                "block_flag": "",
                "serial_offset": "",
                "serial_offset_duplicate": "",
                "serial_size": "",
            }
        )
    return entry


def build_entries(table_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = _parse_binary_table(table_path)
    entries_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    record_types: Counter[int] = Counter()
    asset_classes: Counter[str] = Counter()
    block_counts: Counter[int] = Counter()
    block_flags: Counter[int] = Counter()
    duplicate_offsets_match = 0

    for record in records:
        record_types[record.record_type] += 1
        asset_classes[record.asset_class] += 1
        block_counts[len(record.blocks)] += 1
        entry = record_to_entry(record)
        entries_by_key[entry["package_object_key"]].append(entry)
        for block in record.blocks:
            block_flags[block.block_flag] += 1
            if block.offset_a == block.offset_b:
                duplicate_offsets_match += 1

    duplicate_keys = {
        key: len(values) for key, values in entries_by_key.items() if len(values) > 1
    }
    rows: list[dict[str, Any]] = []
    for record in records:
        entry = record_to_entry(record)
        entry["schema_version"] = SCHEMA_VERSION
        rows.append(entry)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "source_table": str(table_path),
        "record_count": len(records),
        "key_count": len(entries_by_key),
        "duplicate_key_count": len(duplicate_keys),
        "duplicate_record_count": sum(count - 1 for count in duplicate_keys.values()),
        "record_types": {str(key): value for key, value in sorted(record_types.items())},
        "asset_classes": dict(sorted(asset_classes.items())),
        "block_counts": {str(key): value for key, value in sorted(block_counts.items())},
        "block_flags": {str(key): value for key, value in sorted(block_flags.items())},
        "duplicate_offsets_match": duplicate_offsets_match,
        "duplicate_keys": dict(sorted(duplicate_keys.items())),
    }
    return rows, summary


def write_tsv(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        handle.write("\t".join(FIELD_NAMES) + "\n")
        for row in rows:
            values = [str(row.get(field, "")).replace("\t", " ") for field in FIELD_NAMES]
            handle.write("\t".join(values) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table",
        type=Path,
        default=DEFAULT_TABLE_ROOT / "StaticMeshAsync.tab",
        help="Path to StaticMeshAsync.tab",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows, summary = build_entries(args.table)
    write_tsv(rows, args.output)
    print(
        "StaticMesh source index: "
        f"{summary['record_count']} records, "
        f"{summary['key_count']} keys, "
        f"{summary['duplicate_key_count']} duplicate keys -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
