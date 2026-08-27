#!/usr/bin/env python3
"""Inspect Unreal `.uax` audio packages using the existing UE2 package reader.

This probe does not attempt to decode audio payloads. It validates that a `.uax`
package can be opened by `UE2Package`, then emits JSON with package metadata,
export-class counts, and a compact export listing for later analysis.

Usage:
    python3 scripts/extractors/inspect_uax.py /path/to/file.uax
    python3 scripts/extractors/inspect_uax.py /path/to/Assets/Sounds --glob '*.uax'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from ue2.package import UE2Package  # noqa: E402


DEFAULT_GLOB = "*.uax"
PRINTABLE_SUFFIX_RE = re.compile(rb"([A-Za-z0-9_./:-]{4,})\x00$")


def collect_inputs(input_path: Path, glob_pattern: str) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        return sorted(input_path.rglob(glob_pattern))
    raise FileNotFoundError(f"Input path does not exist: {input_path}")


def output_stem_for_path(file_path: Path, input_root: Path) -> str:
    if input_root.is_dir():
        relative = file_path.relative_to(input_root).with_suffix("")
        return "__".join(relative.parts)
    return file_path.stem


def classify_sound_export_payload(export: dict[str, object], data: bytes) -> dict[str, object]:
    info: dict[str, object] = {
        "payload_kind": "unknown",
        "payload_prefix_hex": data[:16].hex(),
    }
    if not data:
        return info

    riff_offset = data.find(b"RIFF")
    if riff_offset != -1 and data.find(b"WAVE", riff_offset + 8) != -1:
        header_prefix = data[:riff_offset]
        info["payload_kind"] = "embedded-wav"
        info["riff_offset"] = riff_offset
        info["wave_header_hex"] = data[riff_offset : riff_offset + 16].hex()
        info["header_prefix_hex"] = header_prefix.hex()
        info["header_prefix_size"] = len(header_prefix)
        info["riff_payload_size"] = len(data) - riff_offset
        export_end_offset = export["serial_offset"] + export["serial_size"]
        if len(header_prefix) >= 10:
            prefix_end_offset = int.from_bytes(header_prefix[6:10], "little")
            info["header_prefix_layout"] = "long-end-offset-at-6"
            info["prefix_end_offset"] = prefix_end_offset
            info["prefix_end_offset_matches_export_end"] = prefix_end_offset == export_end_offset
        elif len(header_prefix) >= 6:
            prefix_end_offset = int.from_bytes(header_prefix[2:6], "little")
            info["header_prefix_layout"] = "short-end-offset-at-2"
            info["prefix_end_offset"] = prefix_end_offset
            info["prefix_end_offset_matches_export_end"] = prefix_end_offset == export_end_offset
        return info

    match = PRINTABLE_SUFFIX_RE.search(data)
    if match:
        suffix = match.group(1).decode("latin-1", errors="replace")
        wrapper_prefix = data[: match.start(1)]
        info["payload_kind"] = "string-wrapper"
        info["string_value"] = suffix
        info["wrapper_prefix_hex"] = wrapper_prefix.hex()
        info["wrapper_prefix_size"] = len(wrapper_prefix)
        info["string_length_with_nul"] = len(match.group(1)) + 1
        if wrapper_prefix:
            info["wrapper_declared_length"] = wrapper_prefix[-1]
            info["wrapper_declared_length_matches_string"] = wrapper_prefix[-1] == info["string_length_with_nul"]
        if b"." in match.group(1):
            info["dotted_name"] = suffix
            info["wrapper_namespace"] = suffix.split(".", 1)[0]
        return info

    return info


def build_sound_analysis(sound_exports: list[dict[str, object]]) -> dict[str, object]:
    wrapper_namespaces = Counter()
    wrapper_prefix_shapes = Counter()
    wrapper_prefix_sizes = Counter()
    wrapper_length_matches = Counter()
    embedded_riff_offsets = Counter()
    embedded_prefix_sizes = Counter()
    embedded_prefix_layouts = Counter()
    embedded_fixed_prefixes = Counter()
    embedded_end_offset_matches = Counter()

    for export in sound_exports:
        payload_kind = export.get("payload_kind")
        if payload_kind == "string-wrapper":
            namespace = export.get("wrapper_namespace")
            if isinstance(namespace, str) and namespace:
                wrapper_namespaces[namespace] += 1
            prefix_size = export.get("wrapper_prefix_size")
            if isinstance(prefix_size, int):
                wrapper_prefix_sizes[prefix_size] += 1
            prefix_shape = f"{export.get('wrapper_prefix_size', 0)}:{export.get('wrapper_prefix_hex', '')}"
            wrapper_prefix_shapes[prefix_shape] += 1
            if isinstance(export.get("wrapper_declared_length_matches_string"), bool):
                key = "match" if export["wrapper_declared_length_matches_string"] else "mismatch"
                wrapper_length_matches[key] += 1
        elif payload_kind == "embedded-wav":
            riff_offset = export.get("riff_offset")
            if isinstance(riff_offset, int):
                embedded_riff_offsets[riff_offset] += 1
            prefix_size = export.get("header_prefix_size")
            if isinstance(prefix_size, int):
                embedded_prefix_sizes[prefix_size] += 1
            prefix_layout = export.get("header_prefix_layout")
            if isinstance(prefix_layout, str):
                embedded_prefix_layouts[prefix_layout] += 1
            header_prefix_hex = export.get("header_prefix_hex")
            if isinstance(header_prefix_hex, str) and header_prefix_hex:
                embedded_fixed_prefixes[header_prefix_hex[:12]] += 1
            if isinstance(export.get("prefix_end_offset_matches_export_end"), bool):
                key = "match" if export["prefix_end_offset_matches_export_end"] else "mismatch"
                embedded_end_offset_matches[key] += 1
            else:
                embedded_end_offset_matches["short-prefix"] += 1

    analysis: dict[str, object] = {}
    if wrapper_namespaces:
        analysis["wrapper_namespaces"] = dict(sorted(wrapper_namespaces.items()))
    if wrapper_prefix_sizes:
        analysis["wrapper_prefix_sizes"] = dict(sorted(wrapper_prefix_sizes.items()))
    if wrapper_prefix_shapes:
        analysis["wrapper_prefix_shapes"] = dict(sorted(wrapper_prefix_shapes.items()))
    if wrapper_length_matches:
        analysis["wrapper_declared_length_matches"] = dict(sorted(wrapper_length_matches.items()))
    if embedded_riff_offsets:
        analysis["embedded_wav_riff_offsets"] = dict(sorted(embedded_riff_offsets.items()))
    if embedded_prefix_sizes:
        analysis["embedded_wav_prefix_sizes"] = dict(sorted(embedded_prefix_sizes.items()))
    if embedded_prefix_layouts:
        analysis["embedded_wav_prefix_layouts"] = dict(sorted(embedded_prefix_layouts.items()))
    if embedded_fixed_prefixes:
        analysis["embedded_wav_fixed_prefixes"] = dict(sorted(embedded_fixed_prefixes.items()))
    if embedded_end_offset_matches:
        analysis["embedded_wav_prefix_end_offset_matches"] = dict(sorted(embedded_end_offset_matches.items()))
    return analysis


def inspect_package(path: Path, export_limit: int) -> dict:
    pkg = UE2Package(str(path))
    class_counts = Counter(exp["class_name"] or "<unknown>" for exp in pkg.exports)
    sound_payload_kinds = Counter()
    sound_exports: list[dict[str, object]] = []
    exports: list[dict[str, object]] = []
    for exp in pkg.exports:
        export_info = {
            "index": exp["index"],
            "class_name": exp["class_name"],
            "object_name": exp["object_name"],
            "serial_size": exp["serial_size"],
            "serial_offset": exp["serial_offset"],
        }
        if exp["class_name"] == "Sound":
            payload_info = classify_sound_export_payload(exp, pkg.get_export_data(exp))
            export_info.update(payload_info)
            sound_exports.append(export_info)
            sound_payload_kinds[payload_info["payload_kind"]] += 1
        if len(exports) < export_limit:
            exports.append(export_info)

    return {
        "file": str(path),
        "version": pkg.version,
        "licensee": pkg.licensee,
        "name_count": len(pkg.names),
        "import_count": len(pkg.imports),
        "export_count": len(pkg.exports),
        "class_counts": dict(sorted(class_counts.items())),
        "sound_payload_kinds": dict(sorted(sound_payload_kinds.items())),
        "sound_analysis": build_sound_analysis(sound_exports),
        "exports_preview": exports,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to a .uax file or a directory containing .uax files")
    parser.add_argument(
        "--glob",
        default=DEFAULT_GLOB,
        help="Glob pattern used when input is a directory",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output/audio/uax"),
        help="Output directory for JSON inspection reports",
    )
    parser.add_argument(
        "--export-limit",
        type=int,
        default=200,
        help="Maximum number of exports to include in each JSON preview",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    input_path = Path(args.input).expanduser().resolve()
    out_root = args.out.expanduser().resolve()

    try:
        files = collect_inputs(input_path, args.glob)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not files:
        print(f"No files matched {args.glob!r} under {input_path}", file=sys.stderr)
        return 1

    out_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, object] = {"packages": [], "failed": []}

    for path in files:
        try:
            report = inspect_package(path, export_limit=args.export_limit)
        except Exception as exc:
            print(f"[FAIL] {path}: {exc}", file=sys.stderr)
            summary["failed"].append({"file": str(path), "error": str(exc)})
            continue

        report_path = out_root / f"{output_stem_for_path(path, input_path)}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary["packages"].append(
            {
                "file": str(path),
                "report": str(report_path),
                "export_count": report["export_count"],
                "class_counts": report["class_counts"],
                "sound_payload_kinds": report["sound_payload_kinds"],
                "sound_analysis": report["sound_analysis"],
            }
        )
        print(f"[OK] {path.name}: {report['export_count']} exports")

    summary_path = out_root / "packages.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Wrote summary to {summary_path}")
    return 0 if not summary["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())