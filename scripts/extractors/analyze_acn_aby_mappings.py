#!/usr/bin/env python3
"""Correlate Vanguard ACN/ABY native IDs without using Ghidra.

This script reopens the original `.acn`/`.aby` packages, extracts the stable
native header fields observed by `inspect_acn_aby.py`, scans the native bytes
for candidate uint32 references, and optionally cross-checks those IDs against
VGOEmulator spell animation IDs.

The output is intentionally evidence-oriented: it does not pretend to name
unknown native fields, but it records where IDs line up cleanly enough to guide
the next parser pass.

Usage:
    python3 scripts/extractors/analyze_acn_aby_mappings.py /path/to/Assets
    python3 scripts/extractors/analyze_acn_aby_mappings.py /path/to/Assets \
        --vgo-emulator /path/to/VGOEmulator
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.ue2_property_reader import BinaryReader, skip_ue2_properties  # noqa: E402
from ue2.package import UE2Package  # noqa: E402


DEFAULT_OUT = Path("output/research/acn_aby_mappings")
TARGETS = {
    ".acn": ("action", "Action"),
    ".aby": ("ability", "Ability"),
}
NAME_SUFFIX_RE = re.compile(r"_(\d+)$")
ASCII_SYMBOL_RE = re.compile(rb"[A-Za-z0-9_./:+-]{4,}")
INSERT_RE = re.compile(
    r"insert\s+into\s+`?(spell|spells)`?\s*\((?P<columns>.*?)\)\s*values\s*(?P<values>.*?);",
    re.IGNORECASE | re.DOTALL,
)
ANIMATION_COLUMN_ALIASES = {
    "cast": ("cast_animation_id", "cast_anim"),
    "impact": ("impact_animation_id", "impact_anim"),
    "secondary": ("secondary_animation_id", "secondary_anim"),
}


def read_uint32(data: bytes, offset: int) -> int | None:
    if offset < 0 or offset + 4 > len(data):
        return None
    return int.from_bytes(data[offset : offset + 4], "little", signed=False)


def trailing_number(name: str) -> int | None:
    match = NAME_SUFFIX_RE.search(name)
    if not match:
        return None
    return int(match.group(1))


def name_without_trailing_id(name: str) -> str:
    return NAME_SUFFIX_RE.sub("", name)


def package_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() not in TARGETS:
            raise ValueError(f"Expected .acn or .aby file, got {input_path}")
        return [input_path]

    if not input_path.is_dir():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    files: list[Path] = []
    for suffix in TARGETS:
        files.extend(input_path.rglob(f"*{suffix}"))
    return sorted(set(files))


def relative_package(path: Path, input_root: Path) -> str:
    if input_root.is_dir():
        return str(path.relative_to(input_root))
    return path.name


def native_body(pkg: UE2Package, export: dict[str, Any]) -> tuple[int, bytes]:
    data = pkg.get_export_data(export)
    reader = BinaryReader(data)
    skip_ue2_properties(reader, pkg.names)
    offset = reader.tell()
    return offset, data[offset:]


def ascii_symbols(data: bytes, limit: int = 16) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for match in ASCII_SYMBOL_RE.finditer(data):
        text = match.group(0).decode("ascii", errors="ignore")
        text = re.sub(r"^[^A-Za-z]+", "", text)
        if not any(ch.isalpha() for ch in text):
            continue
        if len(text) < 4:
            continue
        if text in seen:
            continue
        seen.add(text)
        symbols.append(text)
        if len(symbols) >= limit:
            break
    return symbols


def inspect_exports(input_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for path in package_files(input_path):
        kind, target_class = TARGETS[path.suffix.lower()]
        try:
            pkg = UE2Package(str(path))
        except Exception as exc:
            failures.append({"file": str(path), "error": str(exc)})
            continue

        for export in pkg.exports:
            if export["class_name"] != target_class:
                continue

            try:
                body_offset, body = native_body(pkg, export)
            except Exception as exc:
                failures.append(
                    {
                        "file": str(path),
                        "export": export.get("object_name"),
                        "error": str(exc),
                    }
                )
                continue

            native_id = read_uint32(body, 4)
            name_suffix_id = trailing_number(str(export["object_name"]))
            record = {
                "kind": kind,
                "class_name": target_class,
                "package": relative_package(path, input_path),
                "file": str(path),
                "export_index": export["index"],
                "object_name": export["object_name"],
                "object_prefix": name_without_trailing_id(str(export["object_name"])),
                "serial_size": export["serial_size"],
                "native_body_offset": body_offset,
                "native_body_size": len(body),
                "native_format_version": read_uint32(body, 0),
                "native_id": native_id,
                "name_suffix_id": name_suffix_id,
                "name_suffix_matches_native_id": (
                    native_id == name_suffix_id if native_id is not None and name_suffix_id is not None else None
                ),
                "ascii_symbols": ascii_symbols(body),
                "_native_body": body,
            }
            records.append(record)

    return records, failures


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def compact_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "package": record["package"],
        "object_name": record["object_name"],
        "object_prefix": record["object_prefix"],
        "native_format_version": record["native_format_version"],
        "native_body_size": record["native_body_size"],
        "ascii_symbols": record["ascii_symbols"][:6],
    }


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True) + "\n")


def split_columns(columns: str) -> list[str]:
    return [column.strip().strip("`").lower() for column in columns.split(",")]


def split_sql_rows(values: str) -> list[str]:
    rows: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = False
    escape = False

    for char in values:
        if in_quote:
            current.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == "'":
                in_quote = False
            continue

        if char == "'":
            in_quote = True
            current.append(char)
        elif char == "(":
            if depth == 0:
                current = []
            else:
                current.append(char)
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                rows.append("".join(current))
            else:
                current.append(char)
        elif depth > 0:
            current.append(char)

    return rows


def split_sql_values(row: str) -> list[str]:
    values: list[str] = []
    current: list[str] = []
    in_quote = False
    escape = False

    for char in row:
        if in_quote:
            current.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == "'":
                in_quote = False
            continue

        if char == "'":
            in_quote = True
            current.append(char)
        elif char == ",":
            values.append("".join(current).strip())
            current = []
        else:
            current.append(char)

    values.append("".join(current).strip())
    return values


def parse_sql_string(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        value = value[1:-1]
    return (
        value.replace("\\'", "'")
        .replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\\\", "\\")
    )


def parse_sql_uint(value: str) -> int | None:
    value = value.strip()
    if not value or value.lower() == "null":
        return None
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        value = value[1:-1]
    try:
        return int(value, 10)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return None


def sql_files(vgo_emulator: Path) -> list[Path]:
    preferred = [
        vgo_emulator / "vgo_world_clean.sql",
        vgo_emulator / "seed_test_data.sql",
        vgo_emulator / "Main" / "conf" / "vgo_world.sql",
    ]
    files = [path for path in preferred if path.exists()]
    for path in sorted(vgo_emulator.rglob("*.sql")):
        if path not in files:
            files.append(path)
    return files


def animation_columns(columns: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    column_set = set(columns)
    for role, aliases in ANIMATION_COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in column_set:
                found[role] = alias
                break
    return found


def load_server_animation_refs(vgo_emulator: Path | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if vgo_emulator is None:
        return [], []

    refs: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for path in sql_files(vgo_emulator):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            failures.append({"file": str(path), "error": str(exc)})
            continue

        for match in INSERT_RE.finditer(text):
            columns = split_columns(match.group("columns"))
            role_columns = animation_columns(columns)
            if not role_columns:
                continue

            spell_id_column = "spell_id" if "spell_id" in columns else None
            spell_name_column = "spell_name" if "spell_name" in columns else None
            rows = split_sql_rows(match.group("values"))

            for row_number, row in enumerate(rows, start=1):
                values = split_sql_values(row)
                if len(values) != len(columns):
                    failures.append(
                        {
                            "file": str(path),
                            "row_number": row_number,
                            "error": f"column/value mismatch: {len(columns)} columns, {len(values)} values",
                        }
                    )
                    continue

                row_data = dict(zip(columns, values))
                spell_id = parse_sql_uint(row_data[spell_id_column]) if spell_id_column else None
                spell_name = parse_sql_string(row_data[spell_name_column]) if spell_name_column else ""
                for role, column in role_columns.items():
                    animation_id = parse_sql_uint(row_data[column])
                    if not animation_id or animation_id == 0xFFFFFFFF:
                        continue
                    refs.append(
                        {
                            "animation_id": animation_id,
                            "role": role,
                            "column": column,
                            "spell_id": spell_id,
                            "spell_name": spell_name,
                            "sql_file": str(path),
                        }
                    )

    return refs, failures


def dedupe_server_animation_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for ref in refs:
        key = (
            ref["animation_id"],
            ref["role"],
            ref["spell_id"],
            ref["spell_name"],
        )
        existing = deduped.get(key)
        if existing is None:
            item = dict(ref)
            item["sql_files"] = [ref["sql_file"]]
            deduped[key] = item
            continue

        if ref["sql_file"] not in existing["sql_files"]:
            existing["sql_files"].append(ref["sql_file"])

    return [
        deduped[key]
        for key in sorted(
            deduped,
            key=lambda item: (
                int(item[0]),
                str(item[1]),
                -1 if item[2] is None else int(item[2]),
                str(item[3]),
            ),
        )
    ]


def server_source_summary(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ref in refs:
        by_file[ref["sql_file"]].append(ref)
    return [
        {
            "sql_file": sql_file,
            "reference_count": len(file_refs),
            "unique_animation_ids": len({ref["animation_id"] for ref in file_refs}),
        }
        for sql_file, file_refs in sorted(by_file.items())
    ]


def records_by_id(records: list[dict[str, Any]], kind: str) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["kind"] != kind:
            continue
        native_id = record.get("native_id")
        if native_id is None:
            continue
        grouped[int(native_id)].append(record)
    return grouped


def role_counts(refs: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(ref["role"] for ref in refs).items()))


def unique_spell_examples(refs: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    seen: set[tuple[Any, str, str]] = set()
    for ref in refs:
        key = (ref["spell_id"], ref["spell_name"], ref["role"])
        if key in seen:
            continue
        seen.add(key)
        examples.append(
            {
                "spell_id": ref["spell_id"],
                "spell_name": ref["spell_name"],
                "role": ref["role"],
            }
        )
        if len(examples) >= limit:
            break
    return examples


def build_crosswalk(
    server_refs: list[dict[str, Any]],
    action_by_id: dict[int, list[dict[str, Any]]],
    ability_by_id: dict[int, list[dict[str, Any]]],
    max_matches: int,
    max_spell_examples: int,
) -> list[dict[str, Any]]:
    refs_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for ref in server_refs:
        refs_by_id[int(ref["animation_id"])].append(ref)

    crosswalk: list[dict[str, Any]] = []
    for animation_id in sorted(refs_by_id):
        action_matches = action_by_id.get(animation_id, [])
        ability_matches = ability_by_id.get(animation_id, [])
        refs = refs_by_id[animation_id]
        crosswalk.append(
            {
                "animation_id": animation_id,
                "reference_count": len(refs),
                "roles": role_counts(refs),
                "spell_examples": unique_spell_examples(refs, max_spell_examples),
                "action_match_count": len(action_matches),
                "ability_match_count": len(ability_matches),
                "action_matches": [compact_record(record) for record in action_matches[:max_matches]],
                "ability_matches": [compact_record(record) for record in ability_matches[:max_matches]],
            }
        )
    return crosswalk


def id_set(grouped: dict[int, list[dict[str, Any]]]) -> set[int]:
    return set(grouped)


def duplicate_examples(grouped: dict[int, list[dict[str, Any]]], limit: int = 10) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for native_id, matches in sorted(grouped.items()):
        if len(matches) <= 1:
            continue
        examples.append(
            {
                "native_id": native_id,
                "count": len(matches),
                "matches": [compact_record(record) for record in matches[:5]],
            }
        )
        if len(examples) >= limit:
            break
    return examples


def first_match_examples(crosswalk: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for row in crosswalk:
        if not row["action_match_count"] and not row["ability_match_count"]:
            continue
        examples.append(row)
        if len(examples) >= limit:
            break
    return examples


def build_offset_summary(
    records: list[dict[str, Any]],
    action_ids: set[int],
    ability_ids: set[int],
    server_ids: set[int],
    max_scan_bytes: int,
    min_reference_id: int,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int | None, int], dict[str, Any]] = {}

    for record in records:
        body = record["_native_body"]
        scan_size = min(max(0, len(body) - 3), max_scan_bytes)
        key = (record["class_name"], record["native_format_version"], record["native_body_size"])
        group = groups.setdefault(
            key,
            {
                "class_name": record["class_name"],
                "native_format_version": record["native_format_version"],
                "native_body_size": record["native_body_size"],
                "record_count": 0,
                "_offsets": defaultdict(
                    lambda: {
                        "scan_count": 0,
                        "aligned_count": 0,
                        "self_id_hits": 0,
                        "server_animation_id_hits": 0,
                        "action_id_hits": 0,
                        "ability_id_hits": 0,
                        "external_known_id_hits": 0,
                        "aligned_values": Counter(),
                    }
                ),
            },
        )
        group["record_count"] += 1
        native_id = record.get("native_id")

        for offset in range(scan_size):
            value = read_uint32(body, offset)
            if value is None:
                continue
            value_can_be_reference = value >= min_reference_id

            stats = group["_offsets"][offset]
            stats["scan_count"] += 1
            if value == native_id:
                stats["self_id_hits"] += 1
            if value_can_be_reference and value in server_ids:
                stats["server_animation_id_hits"] += 1
            if value_can_be_reference and value in action_ids:
                stats["action_id_hits"] += 1
            if value_can_be_reference and value in ability_ids:
                stats["ability_id_hits"] += 1
            if value_can_be_reference and value != native_id and (
                value in server_ids or value in action_ids or value in ability_ids
            ):
                stats["external_known_id_hits"] += 1

            if offset % 4 == 0:
                stats["aligned_count"] += 1
                stats["aligned_values"][value] += 1

    output: list[dict[str, Any]] = []
    for group in sorted(
        groups.values(),
        key=lambda item: (
            str(item["class_name"]),
            -int(item["record_count"]),
            int(item["native_body_size"]),
        ),
    ):
        offsets: list[dict[str, Any]] = []
        for offset, stats in sorted(group["_offsets"].items()):
            aligned = offset % 4 == 0
            label_threshold = max(3, int(stats["scan_count"] * 0.05))
            self_label = stats["self_id_hits"] == stats["scan_count"] or stats["self_id_hits"] >= label_threshold
            server_label = stats["server_animation_id_hits"] >= label_threshold or (
                self_label and stats["server_animation_id_hits"] > 0
            )
            external_label = stats["external_known_id_hits"] >= label_threshold
            interesting_reference = self_label or server_label or external_label
            if not aligned and not interesting_reference:
                continue

            labels: list[str] = []
            if offset == 0 and aligned:
                labels.append("native_format_version_candidate")
            if self_label:
                labels.append("native_self_id_candidate")
            if server_label:
                labels.append("server_animation_id_candidate")
            if external_label:
                labels.append("external_known_id_candidate")

            offset_record: dict[str, Any] = {
                "offset": offset,
                "scan_count": stats["scan_count"],
                "labels": labels,
                "self_id_hits": stats["self_id_hits"],
                "server_animation_id_hits": stats["server_animation_id_hits"],
                "action_id_hits": stats["action_id_hits"],
                "ability_id_hits": stats["ability_id_hits"],
                "external_known_id_hits": stats["external_known_id_hits"],
            }

            if aligned:
                values = stats["aligned_values"]
                offset_record.update(
                    {
                        "aligned_count": stats["aligned_count"],
                        "aligned_unique_values": len(values),
                        "aligned_constant_value": next(iter(values)) if len(values) == 1 else None,
                        "aligned_most_common": [
                            {"value": value, "count": count}
                            for value, count in values.most_common(6)
                        ],
                    }
                )

            offsets.append(offset_record)

        public_group = {
            "class_name": group["class_name"],
            "native_format_version": group["native_format_version"],
            "native_body_size": group["native_body_size"],
            "record_count": group["record_count"],
            "offsets": offsets,
        }
        output.append(public_group)

    return output


def build_summary(
    records: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    sql_failures: list[dict[str, Any]],
    action_by_id: dict[int, list[dict[str, Any]]],
    ability_by_id: dict[int, list[dict[str, Any]]],
    raw_server_refs: list[dict[str, Any]],
    server_refs: list[dict[str, Any]],
    crosswalk: list[dict[str, Any]],
) -> dict[str, Any]:
    class_counts = Counter(record["class_name"] for record in records)
    native_versions = Counter(
        f"{record['class_name']}:{record['native_format_version']}" for record in records
    )
    body_sizes = Counter(
        f"{record['class_name']}:{record['native_format_version']}:{record['native_body_size']}"
        for record in records
    )
    prefix_counts = Counter(record["object_prefix"] for record in records if record["kind"] == "action")
    server_ids = {ref["animation_id"] for ref in server_refs}
    action_ids = id_set(action_by_id)
    ability_ids = id_set(ability_by_id)

    return {
        "export_count": len(records),
        "class_counts": dict(sorted(class_counts.items())),
        "native_versions": dict(sorted(native_versions.items())),
        "native_body_size_groups_top": dict(body_sizes.most_common(20)),
        "package_failures": failures,
        "sql_failures": sql_failures,
        "unique_action_ids": len(action_ids),
        "unique_ability_ids": len(ability_ids),
        "duplicate_action_id_count": sum(1 for matches in action_by_id.values() if len(matches) > 1),
        "duplicate_ability_id_count": sum(1 for matches in ability_by_id.values() if len(matches) > 1),
        "duplicate_action_id_examples": duplicate_examples(action_by_id),
        "duplicate_ability_id_examples": duplicate_examples(ability_by_id),
        "action_prefix_counts_top": dict(prefix_counts.most_common(30)),
        "server_animation_raw_ref_count": len(raw_server_refs),
        "server_animation_source_files": server_source_summary(raw_server_refs),
        "server_animation_ref_count": len(server_refs),
        "server_animation_unique_ids": len(server_ids),
        "server_animation_role_counts": dict(sorted(Counter(ref["role"] for ref in server_refs).items())),
        "server_ids_in_action_exports": len(server_ids & action_ids),
        "server_ids_in_ability_exports": len(server_ids & ability_ids),
        "server_ids_in_either_export_type": len(server_ids & (action_ids | ability_ids)),
        "server_ids_missing_from_exports": len(server_ids - (action_ids | ability_ids)),
        "server_crosswalk_examples": first_match_examples(crosswalk),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to the Vanguard Assets root or one .acn/.aby file")
    parser.add_argument(
        "--vgo-emulator",
        type=Path,
        help="Optional path to a VGOEmulator checkout for spell animation SQL correlation",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Output directory for mapping JSON reports",
    )
    parser.add_argument(
        "--max-matches",
        type=int,
        default=8,
        help="Maximum ACN/ABY matches to include inline per server animation ID",
    )
    parser.add_argument(
        "--max-spell-examples",
        type=int,
        default=8,
        help="Maximum spell examples to include inline per server animation ID",
    )
    parser.add_argument(
        "--max-scan-bytes",
        type=int,
        default=256,
        help="Maximum native body bytes to scan for uint32 candidate offset summaries",
    )
    parser.add_argument(
        "--min-reference-id",
        type=int,
        default=100,
        help="Minimum uint32 value to consider as a cross-record ID reference candidate",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    input_path = Path(args.input).expanduser().resolve()
    out_root = args.out.expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    vgo_emulator = args.vgo_emulator.expanduser().resolve() if args.vgo_emulator else None

    records, package_failures = inspect_exports(input_path)
    raw_server_refs, sql_failures = load_server_animation_refs(vgo_emulator)
    server_refs = dedupe_server_animation_refs(raw_server_refs)

    action_records = [public_record(record) for record in records if record["kind"] == "action"]
    ability_records = [public_record(record) for record in records if record["kind"] == "ability"]
    action_by_id = records_by_id(records, "action")
    ability_by_id = records_by_id(records, "ability")
    crosswalk = build_crosswalk(
        server_refs,
        action_by_id,
        ability_by_id,
        args.max_matches,
        args.max_spell_examples,
    )
    offset_summary = build_offset_summary(
        records,
        id_set(action_by_id),
        id_set(ability_by_id),
        {ref["animation_id"] for ref in server_refs},
        args.max_scan_bytes,
        args.min_reference_id,
    )
    summary = build_summary(
        records,
        package_failures,
        sql_failures,
        action_by_id,
        ability_by_id,
        raw_server_refs,
        server_refs,
        crosswalk,
    )

    write_jsonl(out_root / "action_ids.jsonl", action_records)
    write_jsonl(out_root / "ability_ids.jsonl", ability_records)
    write_jsonl(out_root / "server_animation_refs.jsonl", server_refs)
    write_jsonl(out_root / "server_animation_crosswalk.jsonl", crosswalk)
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out_root / "native_offset_summary.json").write_text(
        json.dumps(offset_summary, indent=2),
        encoding="utf-8",
    )

    print(f"ACN Action exports: {len(action_records)}")
    print(f"ABY Ability exports: {len(ability_records)}")
    print(f"Server animation refs: {len(server_refs)}")
    print(f"Server unique animation IDs: {summary['server_animation_unique_ids']}")
    print(f"Server IDs in ACN Actions: {summary['server_ids_in_action_exports']}")
    print(f"Server IDs in ABY Abilities: {summary['server_ids_in_ability_exports']}")
    print(f"Wrote mapping reports to {out_root}")
    return 0 if not package_failures and not sql_failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
