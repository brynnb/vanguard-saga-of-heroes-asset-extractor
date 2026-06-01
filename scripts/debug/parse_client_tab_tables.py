#!/usr/bin/env python3
"""Summarize Vanguard client .tab lookup/cache tables."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ue2.package import UE2Package
from ue2.reader import read_compact_index_at

import config


DEFAULT_TABLE_ROOT = Path(config.VANGUARD_EMU_ROOT) / "bin"
DEFAULT_ASSET_ROOT = Path(config.ASSETS_PATH)

TEXT_TABLES = {"staticmeshMD.tab", "compoundobjectMD.tab"}
EMBEDDED_PAYLOAD_TABLES = {
    "MaterialMemory.tab": "material_memory",
    "PackageMemory.tab": "package_memory",
    "PaletteMemory.tab": "palette_memory",
}
NO_PAYLOAD_TABLES = {"StaticMeshAsync.tab": "static_mesh_async"}
MIPS_TABLES = {"MipsMemory.tab": "mips_memory"}
KNOWN_TABLES = [
    "staticmeshMD.tab",
    "compoundobjectMD.tab",
    "MaterialMemory.tab",
    "MipsMemory.tab",
    "PackageMemory.tab",
    "PaletteMemory.tab",
    "StaticMeshAsync.tab",
]


@dataclass
class BinaryBlock:
    block_index: int
    block_flag: int
    offset_a: int
    offset_b: int
    size: int
    payload: bytes


@dataclass
class BinaryRecord:
    record_index: int
    record_offset: int
    record_type: int
    asset_ref: str
    asset_class: str
    package: str
    group_path: str
    object_name: str
    blocks: list[BinaryBlock]


def _read_fstring_at(data: bytes, offset: int) -> tuple[str, int]:
    length, string_start = read_compact_index_at(data, offset)
    if length <= 0:
        raise ValueError(f"invalid FString length {length} at 0x{offset:x}")
    string_end = string_start + length
    if string_end > len(data):
        raise ValueError(f"FString at 0x{offset:x} extends past EOF")
    raw = data[string_start:string_end]
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    return raw.decode("latin-1", "replace"), string_end


def _split_asset_ref(asset_ref: str) -> tuple[str, str, str, str]:
    if " " not in asset_ref:
        return "", "", "", asset_ref
    asset_class, path = asset_ref.split(" ", 1)
    parts = path.split(".")
    package = parts[0] if parts else ""
    group_path = ".".join(parts[1:-1])
    object_name = parts[-1] if parts else path
    return asset_class, package, group_path, object_name


def _parse_binary_table(path: Path) -> list[BinaryRecord]:
    data = path.read_bytes()
    records: list[BinaryRecord] = []
    offset = 0
    table_name = path.name

    while offset < len(data):
        record_offset = offset
        if offset + 4 > len(data):
            raise ValueError(f"trailing bytes at 0x{offset:x}")
        record_type = struct.unpack_from("<I", data, offset)[0]
        offset += 4

        asset_ref, offset = _read_fstring_at(data, offset)
        asset_class, package, group_path, object_name = _split_asset_ref(asset_ref)

        if offset + 4 > len(data):
            raise ValueError(f"missing block count after record {len(records)}")
        block_count = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if block_count <= 0 or block_count > 128:
            raise ValueError(
                f"implausible block count {block_count} at 0x{record_offset:x}"
            )

        blocks: list[BinaryBlock] = []
        for block_index in range(block_count):
            if offset + 16 > len(data):
                raise ValueError(f"short block at 0x{offset:x}")
            block_flag, offset_a, offset_b, size = struct.unpack_from("<IIII", data, offset)
            offset += 16

            payload = b""
            if table_name in EMBEDDED_PAYLOAD_TABLES:
                payload = data[offset : offset + size]
                if len(payload) != size:
                    raise ValueError(f"short embedded payload at 0x{offset:x}")
                offset += size
            elif table_name in MIPS_TABLES:
                payload = data[offset : offset + size]
                if len(payload) != size:
                    raise ValueError(f"short mip header copy at 0x{offset:x}")
                offset += size
            elif table_name in NO_PAYLOAD_TABLES:
                payload = b""
            else:
                raise ValueError(f"no binary parse mode for {table_name}")

            blocks.append(
                BinaryBlock(
                    block_index=block_index,
                    block_flag=block_flag,
                    offset_a=offset_a,
                    offset_b=offset_b,
                    size=size,
                    payload=payload,
                )
            )

        records.append(
            BinaryRecord(
                record_index=len(records),
                record_offset=record_offset,
                record_type=record_type,
                asset_ref=asset_ref,
                asset_class=asset_class,
                package=package,
                group_path=group_path,
                object_name=object_name,
                blocks=blocks,
            )
        )

    return records


def _text_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="latin-1") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _counter_dict(counter: Counter[Any], limit: int = 20) -> dict[str, int]:
    return {str(key): int(value) for key, value in counter.most_common(limit)}


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    sorted_values = sorted(values)
    return {
        "min": sorted_values[0],
        "max": sorted_values[-1],
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
    }


def summarize_staticmesh(path: Path) -> dict[str, Any]:
    rows = _text_rows(path)
    flags = Counter(row["Flags"] for row in rows)
    detail_levels = Counter(row["Mesh Detail Level"] for row in rows)
    packages = Counter(row["Package Name"] for row in rows)
    impostor_rows = [
        row for row in rows if row.get("Impostor", "-1") not in {"", "-1"}
    ]
    cull_rows = [
        row
        for row in rows
        if row.get("Cull Dist", "0") not in {"", "0", "0.0", "0.00"}
    ]
    portal_like = [row for row in rows if "portal" in row["Name"].lower()]
    collision_like = [
        row
        for row in rows
        if any(token in row["Name"].lower() for token in ("collision", "_coll", "coll_"))
    ]
    radii = [float(row["Sphere Radius"]) for row in rows if row.get("Sphere Radius")]
    return {
        "table": path.name,
        "format": "text_tsv",
        "rows": len(rows),
        "columns": list(rows[0].keys()) if rows else [],
        "flags": _counter_dict(flags),
        "mesh_detail_levels": _counter_dict(detail_levels),
        "top_packages": _counter_dict(packages),
        "rows_with_impostor": len(impostor_rows),
        "rows_with_cull_dist": len(cull_rows),
        "portal_name_rows": len(portal_like),
        "collision_name_rows": len(collision_like),
        "sphere_radius": _stats(radii),
        "sample_rows": rows[:5],
    }


def summarize_compound(path: Path) -> dict[str, Any]:
    rows = _text_rows(path)
    types = Counter(row["Type"] for row in rows)
    ag_types = Counter(row["AG_Type"] for row in rows)
    chunk_sector_rows = sum(1 for row in rows if row["Name"].startswith("chunk_"))
    radii = [float(row["Radius"]) for row in rows if row.get("Radius")]
    file_sizes = [int(row["FileSize"]) for row in rows if row.get("FileSize")]
    suffixes = Counter(row["Name"].rsplit("_", 1)[-1] for row in rows if "_" in row["Name"])
    return {
        "table": path.name,
        "format": "text_tsv",
        "rows": len(rows),
        "columns": list(rows[0].keys()) if rows else [],
        "types": _counter_dict(types),
        "ag_types": _counter_dict(ag_types),
        "chunk_sector_rows": chunk_sector_rows,
        "name_suffixes": _counter_dict(suffixes),
        "radius": _stats(radii),
        "file_size": _stats(file_sizes),
        "sample_rows": rows[:5],
    }


def _payload_prefix_hist(records: list[BinaryRecord]) -> dict[str, int]:
    prefixes = Counter()
    for record in records:
        for block in record.blocks:
            if block.payload:
                prefixes[block.payload[:1].hex()] += 1
    return _counter_dict(prefixes, 20)


def summarize_binary(path: Path) -> dict[str, Any]:
    records = _parse_binary_table(path)
    blocks = [block for record in records for block in record.blocks]
    class_counts = Counter(record.asset_class for record in records)
    package_counts = Counter(record.package for record in records)
    record_types = Counter(record.record_type for record in records)
    block_counts = Counter(len(record.blocks) for record in records)
    block_flags = Counter(block.block_flag for block in blocks)
    sizes = Counter(block.size for block in blocks)
    offset_matches = sum(1 for block in blocks if block.offset_a == block.offset_b)
    embedded_bytes = sum(len(block.payload) for block in blocks)

    summary: dict[str, Any] = {
        "table": path.name,
        "format": "binary_fstring_records",
        "records": len(records),
        "blocks": len(blocks),
        "record_types": _counter_dict(record_types),
        "asset_classes": _counter_dict(class_counts),
        "block_counts": _counter_dict(block_counts),
        "block_flags": _counter_dict(block_flags),
        "block_sizes": _counter_dict(sizes),
        "top_packages": _counter_dict(package_counts),
        "duplicate_offsets_match": offset_matches,
        "embedded_payload_bytes": embedded_bytes,
        "payload_prefixes": _payload_prefix_hist(records),
        "sample_records": [
            {
                "record_index": record.record_index,
                "record_offset": record.record_offset,
                "record_type": record.record_type,
                "asset_ref": record.asset_ref,
                "block_count": len(record.blocks),
                "blocks": [
                    {
                        "block_flag": block.block_flag,
                        "offset_a": block.offset_a,
                        "offset_b": block.offset_b,
                        "size": block.size,
                        "payload_prefix_hex": block.payload[:16].hex(" "),
                    }
                    for block in record.blocks[:3]
                ],
            }
            for record in records[:5]
        ],
    }

    if path.name == "PaletteMemory.tab":
        palette_payloads = [block.payload for block in blocks if block.payload]
        summary["palette_payload_layout"] = {
            "observed_payload_size": _counter_dict(Counter(len(p) for p in palette_payloads)),
            "interpretation": "5 byte prefix plus 256 RGBA entries",
            "first_payload_prefix_hex": palette_payloads[0][:12].hex(" ")
            if palette_payloads
            else "",
        }
    elif path.name == "StaticMeshAsync.tab":
        summary["payload_note"] = (
            "Blocks store source package serial offsets and serial sizes only; "
            "static mesh bytes are not embedded in the table."
        )
    elif path.name == "MipsMemory.tab":
        summary["payload_note"] = (
            "Blocks embed the copied Vanguard 142-byte texture mip header."
        )

    return summary


def summarize_table(path: Path) -> dict[str, Any]:
    if path.name == "staticmeshMD.tab":
        return summarize_staticmesh(path)
    if path.name == "compoundobjectMD.tab":
        return summarize_compound(path)
    return summarize_binary(path)


def _find_package_files(asset_root: Path) -> dict[str, Path]:
    package_files: dict[str, Path] = {}
    for path in asset_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {
            ".utx",
            ".usx",
            ".u",
            ".vgr",
            ".sgo",
            ".ukx",
        }:
            continue
        package_files.setdefault(path.stem.lower(), path)
    return package_files


def verify_binary_table(path: Path, asset_root: Path, limit: int) -> dict[str, Any]:
    records = _parse_binary_table(path)
    package_files = _find_package_files(asset_root)
    packages: dict[str, UE2Package | None] = {}
    checked = 0
    matched = 0
    missing_package = 0
    missing_export = 0
    mismatches: list[dict[str, Any]] = []

    for record in records:
        package_path = package_files.get(record.package.lower())
        if package_path is None:
            missing_package += 1
            continue
        if record.package not in packages:
            try:
                packages[record.package] = UE2Package(str(package_path))
            except Exception:
                packages[record.package] = None
        package = packages[record.package]
        if package is None:
            missing_package += 1
            continue

        export = next(
            (
                item
                for item in package.exports
                if item.get("class_name") == record.asset_class
                and item.get("object_name") == record.object_name
            ),
            None,
        )
        if export is None:
            missing_export += 1
            continue

        for block in record.blocks:
            if checked >= limit:
                return {
                    "table": path.name,
                    "limit": limit,
                    "checked": checked,
                    "matched": matched,
                    "missing_package": missing_package,
                    "missing_export": missing_export,
                    "mismatches": mismatches[:20],
                }
            checked += 1
            serial_offset = int(export.get("serial_offset", -1))
            serial_size = int(export.get("serial_size", -1))
            offset_ok = block.offset_a == serial_offset
            size_ok = True
            payload_ok = True
            if path.name == "StaticMeshAsync.tab":
                size_ok = block.size == serial_size
            else:
                package_bytes = package_path.read_bytes()
                expected = package_bytes[block.offset_a : block.offset_a + block.size]
                payload_ok = expected == block.payload
            if offset_ok and size_ok and payload_ok:
                matched += 1
            else:
                mismatches.append(
                    {
                        "asset_ref": record.asset_ref,
                        "block_index": block.block_index,
                        "block_offset": block.offset_a,
                        "export_serial_offset": serial_offset,
                        "block_size": block.size,
                        "export_serial_size": serial_size,
                        "offset_ok": offset_ok,
                        "size_ok": size_ok,
                        "payload_ok": payload_ok,
                    }
                )

    return {
        "table": path.name,
        "limit": limit,
        "checked": checked,
        "matched": matched,
        "missing_package": missing_package,
        "missing_export": missing_export,
        "mismatches": mismatches[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-root", type=Path, default=DEFAULT_TABLE_ROOT)
    parser.add_argument(
        "--table",
        action="append",
        choices=KNOWN_TABLES + ["all"],
        default=["all"],
        help="Table name to parse; may be repeated.",
    )
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--verify-root", type=Path, default=DEFAULT_ASSET_ROOT)
    parser.add_argument("--verify-limit", type=int, default=0)
    parser.add_argument("--verify-json", type=Path)
    args = parser.parse_args()

    selected = args.table
    if "all" in selected:
        selected = KNOWN_TABLES

    summaries: dict[str, Any] = {}
    for table_name in selected:
        table_path = args.table_root / table_name
        if not table_path.exists():
            raise FileNotFoundError(table_path)
        summaries[table_name] = summarize_table(table_path)

    print(json.dumps(summaries, indent=2, sort_keys=True, default=list))

    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        with args.summary_json.open("w") as f:
            json.dump(summaries, f, indent=2, sort_keys=True, default=list)

    if args.verify_limit > 0:
        verify: dict[str, Any] = {}
        for table_name in selected:
            if table_name in TEXT_TABLES or table_name == "MipsMemory.tab":
                continue
            verify[table_name] = verify_binary_table(
                args.table_root / table_name, args.verify_root, args.verify_limit
            )
        print(json.dumps({"verification": verify}, indent=2, sort_keys=True))
        if args.verify_json:
            args.verify_json.parent.mkdir(parents=True, exist_ok=True)
            with args.verify_json.open("w") as f:
                json.dump(verify, f, indent=2, sort_keys=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
