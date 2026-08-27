#!/usr/bin/env python3
"""Inspect Vanguard `.acn` Action and `.aby` Ability packages.

This is a lossless first-pass inspector, not a full native Action/Ability
decoder. It uses the existing UE2 package reader, skips the leading UE2
property block, records the stable native header fields observed so far, and
preserves unknown native bytes for later schema work.

Usage:
    python3 scripts/extractors/inspect_acn_aby.py /path/to/Assets
    python3 scripts/extractors/inspect_acn_aby.py /path/to/Assets/Actions --glob '*.acn'
    python3 scripts/extractors/inspect_acn_aby.py /path/to/file.acn
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

from scripts.lib.ue2_property_reader import BinaryReader, skip_ue2_properties  # noqa: E402
from ue2.package import UE2Package  # noqa: E402


DEFAULT_GLOBS = ("*.acn", "*.aby")
TARGET_CLASSES = {"Action", "Ability"}
NAME_SUFFIX_RE = re.compile(r"_(\d+)$")


def collect_inputs(input_path: Path, glob_patterns: list[str]) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        files: list[Path] = []
        for pattern in glob_patterns:
            files.extend(input_path.rglob(pattern))
        return sorted(set(files))
    raise FileNotFoundError(f"Input path does not exist: {input_path}")


def output_stem_for_path(file_path: Path, input_root: Path) -> str:
    if input_root.is_dir():
        relative = file_path.relative_to(input_root).with_suffix("")
        return "__".join(relative.parts)
    return file_path.stem


def counter_to_dict(counter: Counter[Any]) -> dict[str, int]:
    return {str(key): value for key, value in sorted(counter.items(), key=lambda item: str(item[0]))}


def read_uint32(data: bytes, offset: int) -> int | None:
    if offset < 0 or offset + 4 > len(data):
        return None
    return int.from_bytes(data[offset : offset + 4], "little", signed=False)


def trailing_number(name: str) -> int | None:
    match = NAME_SUFFIX_RE.search(name)
    if not match:
        return None
    return int(match.group(1))


def inspect_property_block(pkg: UE2Package, data: bytes) -> dict[str, Any]:
    reader = BinaryReader(data)
    info: dict[str, Any] = {
        "property_block_status": "ok",
        "property_block_offset": 0,
        "property_block_end": 0,
        "property_block_size": 0,
    }
    try:
        skip_ue2_properties(reader, pkg.names)
    except Exception as exc:
        info.update(
            {
                "property_block_status": "error",
                "property_block_error": str(exc),
                "property_block_end": reader.tell(),
                "property_block_size": reader.tell(),
            }
        )
        return info

    end = reader.tell()
    info.update(
        {
            "property_block_end": end,
            "property_block_size": end,
        }
    )
    return info


def classify_native_payload(
    pkg: UE2Package,
    export: dict[str, Any],
    data: bytes,
    raw_preview_bytes: int,
) -> dict[str, Any]:
    info = inspect_property_block(pkg, data)
    prop_end = int(info.get("property_block_end", 0))
    native_size = max(0, len(data) - prop_end)
    native_version = read_uint32(data, prop_end)
    native_id = read_uint32(data, prop_end + 4)
    name_suffix = trailing_number(str(export["object_name"]))

    info.update(
        {
            "native_body_offset": prop_end,
            "native_body_size": native_size,
            "native_known_header_size": 8 if native_size >= 8 else native_size,
            "native_format_version": native_version,
            "native_object_id": native_id,
            "name_suffix_id": name_suffix,
            "name_suffix_matches_native_id": (
                native_id == name_suffix if native_id is not None and name_suffix is not None else None
            ),
            "native_prefix_hex": data[prop_end : prop_end + raw_preview_bytes].hex(),
            "native_unknown_after_header_hex": data[prop_end + 8 : prop_end + 8 + raw_preview_bytes].hex()
            if native_size > 8
            else "",
            "native_tail_hex": data[max(prop_end, len(data) - raw_preview_bytes) :].hex(),
        }
    )
    return info


def inspect_export(
    pkg: UE2Package,
    export: dict[str, Any],
    raw_preview_bytes: int,
) -> dict[str, Any]:
    data = pkg.get_export_data(export)
    info: dict[str, Any] = {
        "index": export["index"],
        "class_name": export["class_name"],
        "object_name": export["object_name"],
        "serial_size": export["serial_size"],
        "serial_offset": export["serial_offset"],
        "object_flags": export.get("object_flags"),
    }
    if export["class_name"] in TARGET_CLASSES:
        info.update(classify_native_payload(pkg, export, data, raw_preview_bytes))
    else:
        info["payload_prefix_hex"] = data[:raw_preview_bytes].hex()
    return info


def package_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".acn":
        return "action"
    if suffix == ".aby":
        return "ability"
    return suffix.lstrip(".") or "unknown"


def inspect_package(path: Path, export_limit: int, raw_preview_bytes: int) -> dict[str, Any]:
    pkg = UE2Package(str(path))
    class_counts = Counter(exp["class_name"] or "<unknown>" for exp in pkg.exports)
    import_class_counts = Counter(imp["class_name"] or "<unknown>" for imp in pkg.imports)
    target_exports: list[dict[str, Any]] = []
    exports_preview: list[dict[str, Any]] = []

    native_versions = Counter()
    id_match_counts = Counter()
    property_block_status = Counter()
    serial_sizes = Counter()

    for export in pkg.exports:
        export_info = inspect_export(pkg, export, raw_preview_bytes)
        if len(exports_preview) < export_limit:
            exports_preview.append(export_info)

        if export["class_name"] in TARGET_CLASSES:
            target_exports.append(export_info)
            native_versions[(export["class_name"], export_info.get("native_format_version"))] += 1
            match_value = export_info.get("name_suffix_matches_native_id")
            id_match_counts[str(match_value).lower()] += 1
            property_block_status[export_info.get("property_block_status", "unknown")] += 1
            serial_sizes[int(export["serial_size"])] += 1

    serial_size_values = [
        int(export["serial_size"])
        for export in pkg.exports
        if export["class_name"] in TARGET_CLASSES and int(export["serial_size"]) > 0
    ]

    target_summary: dict[str, Any] = {
        "target_export_count": len(target_exports),
        "native_versions": {
            f"{class_name}:{version}": count
            for (class_name, version), count in sorted(native_versions.items(), key=lambda item: str(item[0]))
        },
        "name_suffix_id_matches": counter_to_dict(id_match_counts),
        "property_block_status": counter_to_dict(property_block_status),
        "serial_size_counts_top": counter_to_dict(Counter(dict(serial_sizes.most_common(20)))),
    }
    if serial_size_values:
        target_summary.update(
            {
                "serial_size_min": min(serial_size_values),
                "serial_size_max": max(serial_size_values),
                "serial_size_total": sum(serial_size_values),
            }
        )

    return {
        "file": str(path),
        "kind": package_kind(path),
        "version": pkg.version,
        "licensee": pkg.licensee,
        "package_flags": pkg.package_flags,
        "name_count": len(pkg.names),
        "import_count": len(pkg.imports),
        "export_count": len(pkg.exports),
        "class_counts": counter_to_dict(class_counts),
        "import_class_counts": counter_to_dict(import_class_counts),
        "target_summary": target_summary,
        "target_exports": target_exports[:export_limit],
        "exports_preview": exports_preview,
    }


def package_summary(report: dict[str, Any], report_path: Path) -> dict[str, Any]:
    return {
        "file": report["file"],
        "report": str(report_path),
        "kind": report["kind"],
        "version": report["version"],
        "licensee": report["licensee"],
        "name_count": report["name_count"],
        "import_count": report["import_count"],
        "export_count": report["export_count"],
        "class_counts": report["class_counts"],
        "target_summary": report["target_summary"],
    }


def update_aggregate(summary: dict[str, Any], report: dict[str, Any]) -> None:
    summary["package_count"] += 1
    summary["export_count"] += int(report["export_count"])
    summary["class_counts"].update(report["class_counts"])
    summary["kinds"].update([report["kind"]])
    summary["package_versions"].update([f"{report['version']}/{report['licensee']}"])

    target_summary = report["target_summary"]
    summary["target_export_count"] += int(target_summary["target_export_count"])
    summary["native_versions"].update(target_summary["native_versions"])
    summary["name_suffix_id_matches"].update(target_summary["name_suffix_id_matches"])
    summary["property_block_status"].update(target_summary["property_block_status"])


def write_exports_jsonl(path: Path, report: dict[str, Any], raw_preview_bytes: int) -> None:
    pkg = UE2Package(str(report["file"]))
    with path.open("a", encoding="utf-8") as fh:
        for export in pkg.exports:
            record = {
                "package": report["file"],
                "kind": report["kind"],
                "version": report["version"],
                "licensee": report["licensee"],
                "export": inspect_export(pkg, export, raw_preview_bytes),
            }
            fh.write(json.dumps(record, sort_keys=True) + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to an .acn/.aby file or a directory containing packages")
    parser.add_argument(
        "--glob",
        action="append",
        dest="glob_patterns",
        help="Glob pattern used when input is a directory. Repeat to inspect multiple extensions.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output/research/acn_aby"),
        help="Output directory for JSON inspection reports",
    )
    parser.add_argument(
        "--export-limit",
        type=int,
        default=200,
        help="Maximum exports to include in each per-package JSON preview",
    )
    parser.add_argument(
        "--raw-preview-bytes",
        type=int,
        default=32,
        help="Number of raw payload bytes to include in preview fields",
    )
    parser.add_argument(
        "--no-jsonl",
        action="store_true",
        help="Skip writing aggregate exports.jsonl",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    input_path = Path(args.input).expanduser().resolve()
    out_root = args.out.expanduser().resolve()
    glob_patterns = args.glob_patterns or list(DEFAULT_GLOBS)

    try:
        files = collect_inputs(input_path, glob_patterns)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not files:
        print(f"No files matched {glob_patterns!r} under {input_path}", file=sys.stderr)
        return 1

    out_root.mkdir(parents=True, exist_ok=True)
    exports_jsonl = out_root / "exports.jsonl"
    if exports_jsonl.exists() and not args.no_jsonl:
        exports_jsonl.unlink()

    summary: dict[str, Any] = {
        "input": str(input_path),
        "glob_patterns": glob_patterns,
        "package_count": 0,
        "export_count": 0,
        "target_export_count": 0,
        "class_counts": Counter(),
        "kinds": Counter(),
        "package_versions": Counter(),
        "native_versions": Counter(),
        "name_suffix_id_matches": Counter(),
        "property_block_status": Counter(),
        "packages": [],
        "failed": [],
    }

    for file_path in files:
        try:
            report = inspect_package(file_path, args.export_limit, args.raw_preview_bytes)
        except Exception as exc:
            print(f"[FAIL] {file_path}: {exc}", file=sys.stderr)
            summary["failed"].append({"file": str(file_path), "error": str(exc)})
            continue

        report_path = out_root / f"{output_stem_for_path(file_path, input_path)}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary["packages"].append(package_summary(report, report_path))
        update_aggregate(summary, report)
        if not args.no_jsonl:
            write_exports_jsonl(exports_jsonl, report, args.raw_preview_bytes)
        print(
            "[OK] "
            f"{file_path.name}: {report['export_count']} exports, "
            f"{report['target_summary']['target_export_count']} targets"
        )

    output_summary = {
        key: counter_to_dict(value) if isinstance(value, Counter) else value
        for key, value in summary.items()
    }
    summary_path = out_root / "packages.json"
    summary_path.write_text(json.dumps(output_summary, indent=2), encoding="utf-8")
    print(f"Wrote summary to {summary_path}")
    if not args.no_jsonl:
        print(f"Wrote exports to {exports_jsonl}")
    return 0 if not summary["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
