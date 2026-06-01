#!/usr/bin/env python3
"""Analyze the stable post-74001 boundary in dumped SpeedTree .spt files."""

from __future__ import annotations

import argparse
from collections import Counter
import math
import os
import struct
import sys
from pathlib import Path
from typing import Iterable, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPTS_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT))

import config
from scripts.speedtree.inspect_speedtree_spt import build_default_dump_path, dump_spt_payload, find_export, find_spt_header
from scripts.lib.vanguard_staticmesh import parse_vanguard_staticmesh
from ue2 import UE2Package


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="Standalone .spt files to inspect. Defaults to output/data/speedtree_spt/*.spt",
    )
    parser.add_argument(
        "--package",
        help="Analyze one or more SpeedTree exports directly from a .usx package",
    )
    parser.add_argument(
        "--export-name",
        action="append",
        default=[],
        help="Specific export name or unique substring inside the package. Can be repeated.",
    )
    parser.add_argument(
        "--all-package-speedtrees",
        action="store_true",
        help="Analyze all exports containing 'SpeedTrees' from the package",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional limit when using --all-package-speedtrees",
    )
    parser.add_argument(
        "--window-bytes",
        type=int,
        default=52,
        help="Number of bytes to print after the 74001 boundary",
    )
    return parser.parse_args(argv)


def iter_default_spt_paths() -> Iterable[Path]:
    dump_dir = Path(config.DATA_DIR) / "speedtree_spt"
    if not dump_dir.exists():
        return []
    return sorted(path for path in dump_dir.glob("*.spt") if path.is_file())


def iter_package_spt_paths(
    package_path: Path,
    export_names: Sequence[str],
    all_package_speedtrees: bool,
    limit: int,
) -> tuple[list[Path], dict[Path, int]]:
    pkg = UE2Package(str(package_path))
    selected_exports = []

    if export_names:
        for export_name in export_names:
            selected_exports.append(find_export(pkg, export_name))
    elif all_package_speedtrees:
        for export in pkg.exports:
            object_name = export.get("object_name", "")
            if "speedtrees" in object_name.lower():
                selected_exports.append(export)
        selected_exports.sort(key=lambda export: export.get("object_name", ""))
        if limit > 0:
            selected_exports = selected_exports[:limit]
    else:
        raise SystemExit("--package requires --export-name or --all-package-speedtrees")

    dumped_paths = []
    vertex_counts: dict[Path, int] = {}
    for export in selected_exports:
        data = pkg.get_export_data(export)
        start_offset, _version = find_spt_header(data)
        dump_path = Path(build_default_dump_path(export["object_name"], str(package_path)))
        dump_spt_payload(data, start_offset, str(dump_path))
        dumped_paths.append(dump_path)
        mesh = parse_vanguard_staticmesh(data, pkg.names, export.get("serial_offset", 0), pkg.imports)
        if mesh is not None:
            vertex_counts[dump_path.resolve()] = len(mesh.vertices)
    return dumped_paths, vertex_counts


def scan_for_boundary(data: bytes) -> list[tuple[int, float, int]]:
    hits: list[tuple[int, float, int]] = []
    for offset in range(len(data) - 16):
        if struct.unpack_from("<I", data, offset)[0] != 74000:
            continue
        if struct.unpack_from("<I", data, offset + 4)[0] != 74002:
            continue
        angle_value = struct.unpack_from("<f", data, offset + 8)[0]
        if struct.unpack_from("<I", data, offset + 12)[0] != 74001:
            continue
        hits.append((offset, angle_value, offset + 16))
    return hits


def format_u32(data: bytes, offset: int) -> str:
    if offset + 4 > len(data):
        return "<eof>"
    value = struct.unpack_from("<I", data, offset)[0]
    return f"{value} (0x{value:08x})"


def format_f32(data: bytes, offset: int) -> str:
    if offset + 4 > len(data):
        return "<eof>"
    value = struct.unpack_from("<f", data, offset)[0]
    return f"{value:.6g}"


def parse_padded_tail_records(data: bytes, payload_offset: int) -> list[tuple[float, tuple[float, float, float], tuple[float, float, float], float]]:
    header_padding_offset = payload_offset + 28
    if header_padding_offset + 24 > len(data):
        return []
    if not all(struct.unpack_from("<I", data, header_padding_offset + i * 4)[0] == 0xCCCCCCCC for i in range(6)):
        return []

    records = []
    offset = header_padding_offset + 24
    while offset + 56 <= len(data):
        if not all(struct.unpack_from("<I", data, offset + 32 + i * 4)[0] == 0xCCCCCCCC for i in range(6)):
            break
        records.append(
            (
                struct.unpack_from("<f", data, offset)[0],
                struct.unpack_from("<3f", data, offset + 4),
                struct.unpack_from("<3f", data, offset + 16),
                struct.unpack_from("<f", data, offset + 28)[0],
            )
        )
        offset += 56
    return records


def compact_tail_record_ok(data: bytes, base_offset: int) -> bool:
    if base_offset + 56 > len(data):
        return False
    return (
        struct.unpack_from("<I", data, base_offset)[0] == 0
        and struct.unpack_from("<I", data, base_offset + 4)[0] == 0x3F7FF622
        and struct.unpack_from("<I", data, base_offset + 8)[0] == 0x00A2B648
        and struct.unpack_from("<I", data, base_offset + 12)[0] == 0x10105CDA
        and 0 < struct.unpack_from("<I", data, base_offset + 16)[0] < 10000
        and struct.unpack_from("<I", data, base_offset + 16)[0] == struct.unpack_from("<I", data, base_offset + 20)[0]
    )


def parse_compact_tail_records(
    data: bytes,
    payload_offset: int,
) -> tuple[int | None, list[tuple[int, float, float, tuple[float, float, float], tuple[float, float, float]]]]:
    start_offset = payload_offset + 28
    if not compact_tail_record_ok(data, start_offset):
        return None, []

    records = []
    offset = start_offset
    count_tag: int | None = None
    while compact_tail_record_ok(data, offset):
        current_count = struct.unpack_from("<I", data, offset + 16)[0]
        if count_tag is None:
            count_tag = current_count
        if current_count != count_tag:
            break
        records.append(
            (
                current_count,
                struct.unpack_from("<f", data, offset + 24)[0],
                struct.unpack_from("<f", data, offset + 28)[0],
                struct.unpack_from("<3f", data, offset + 32),
                struct.unpack_from("<3f", data, offset + 44),
            )
        )
        offset += 56
    return count_tag, records


def summarize_padded_tail_rows(
    records: list[tuple[float, tuple[float, float, float], tuple[float, float, float], float]],
) -> tuple[int, str, str, str]:
    row_map: dict[float, list[float]] = {}
    ordered_pairs: list[tuple[float, float]] = []

    for first_value, anchor, direction, scale in records:
        row_key = round(anchor[0], 6)
        c0_value = round(first_value, 6)
        row_map.setdefault(row_key, []).append(c0_value)
        ordered_pairs.append((c0_value, row_key))

    row_widths = Counter(len(sorted(set(values))) for values in row_map.values())
    row_width_summary = ", ".join(f"{width}x{count}" for width, count in row_widths.most_common())
    ordered_pair_preview = ", ".join(f"({c0:.6g},{c1:.6g})" for c0, c1 in ordered_pairs[:12])

    row_examples = []
    for row_key in sorted(row_map)[:8]:
        unique_values = sorted(set(row_map[row_key]))
        preview = ", ".join(f"{value:.6g}" for value in unique_values[:8])
        row_examples.append(f"{row_key:.6g}:[{preview}]")
    row_example_summary = "; ".join(row_examples)

    return len(row_map), row_width_summary, ordered_pair_preview, row_example_summary


def second_block_record_ok(data: bytes, base_offset: int) -> bool:
    if base_offset + 64 > len(data):
        return False
    words = [struct.unpack_from("<I", data, base_offset + 32 + i * 4)[0] for i in range(8)]
    return (
        words[0] == 0xCCCCCCCC
        and words[3] == 0xCCCCCCCC
        and words[4] == 5
        and words[5] == 0xCCCCCCCC
        and words[6] == 0x3F000000
        and words[7] == 0x80000000
    )


def detect_second_block_stride(data: bytes, start_offset: int) -> int | None:
    candidate_offsets = []
    search_end = min(len(data), start_offset + 4096)
    for offset in range(start_offset, search_end, 4):
        if second_block_record_ok(data, offset):
            candidate_offsets.append(offset)
    if len(candidate_offsets) < 2:
        return None
    deltas = [candidate_offsets[i + 1] - candidate_offsets[i] for i in range(len(candidate_offsets) - 1)]
    if not deltas:
        return None
    return Counter(deltas).most_common(1)[0][0]


def parse_second_block_records(data: bytes, start_offset: int) -> tuple[int | None, list[tuple[float, ...]]]:
    stride = detect_second_block_stride(data, start_offset)
    if stride is None or not second_block_record_ok(data, start_offset):
        return None, []

    records = []
    offset = start_offset
    while second_block_record_ok(data, offset):
        records.append(struct.unpack_from("<8f", data, offset))
        offset += stride
    return stride, records


def third_block_record_ok(data: bytes, base_offset: int) -> bool:
    if base_offset + 64 > len(data):
        return False
    words = [struct.unpack_from("<I", data, base_offset + 32 + i * 4)[0] for i in range(8)]
    return (
        words[1] == 0xCCCCCCCC
        and words[2] == 0xCCCCCCCC
        and words[3] == 0xCCCCCCCC
        and words[5] == 0xCCCCCCCC
        and words[6] == 0x3F000000
        and words[7] in (0x80000000, 0xBF000000, 0xBF800000)
    )


def parse_third_block_records(data: bytes, start_offset: int) -> list[tuple[float, ...]]:
    records = []
    offset = start_offset
    while third_block_record_ok(data, offset):
        records.append(struct.unpack_from("<8f", data, offset))
        offset += 64
    return records


def analyze_file(path: Path, window_bytes: int, expected_vertex_count: int | None = None) -> None:
    data = path.read_bytes()
    hits = scan_for_boundary(data)

    print(f"file={path}")
    print(f"boundary_hit_count={len(hits)}")
    if not hits:
        return

    for index, (block_start, angle_value, payload_offset) in enumerate(hits, start=1):
        first_u32 = struct.unpack_from("<I", data, payload_offset)[0]
        print(f"  hit[{index}] block_start={block_start} angle={angle_value:.6g} payload_offset={payload_offset}")
        if expected_vertex_count is not None:
            print(
                f"    vertex_count_field={first_u32} expected_mesh_vertices={expected_vertex_count} "
                f"match={str(first_u32 == expected_vertex_count).lower()}"
            )
        if payload_offset + 28 <= len(data):
            anchor = struct.unpack_from("<3f", data, payload_offset + 4)
            direction = struct.unpack_from("<3f", data, payload_offset + 16)
            direction_norm = math.sqrt(sum(component * component for component in direction))
            print(
                "    "
                f"header_anchor=({anchor[0]:.6g}, {anchor[1]:.6g}, {anchor[2]:.6g}) "
                f"header_direction=({direction[0]:.6g}, {direction[1]:.6g}, {direction[2]:.6g}) "
                f"direction_norm={direction_norm:.6g}"
            )
        if payload_offset + 52 <= len(data):
            padding_words = struct.unpack_from("<6I", data, payload_offset + 28)
            padding_match = all(word == 0xCCCCCCCC for word in padding_words)
            print(f"    header_padding_cccccccc={str(padding_match).lower()}")
            if padding_match:
                records = parse_padded_tail_records(data, payload_offset)
                if records:
                    first_values = [round(record[0], 6) for record in records]
                    value_counts = Counter(first_values)
                    preview = ", ".join(f"{value:.6g}" for value in first_values[:16])
                    most_common = ", ".join(f"{value:.6g}x{count}" for value, count in value_counts.most_common(8))
                    row_count, row_width_summary, ordered_pair_preview, row_example_summary = summarize_padded_tail_rows(records)
                    second_block_start = payload_offset + 28 + 24 + len(records) * 56
                    second_stride, second_records = parse_second_block_records(data, second_block_start)
                    print(f"    padded_record_count={len(records)}")
                    print(f"    padded_record_first_field_unique_count={len(value_counts)}")
                    print(f"    padded_record_first_field_preview=[{preview}]")
                    print(f"    padded_record_first_field_common=[{most_common}]")
                    print(f"    padded_record_row_count={row_count} row_widths=[{row_width_summary}]")
                    print(f"    padded_record_pair_preview=[{ordered_pair_preview}]")
                    print(f"    padded_record_row_examples=[{row_example_summary}]")
                    if second_stride is not None and second_records:
                        second_first = second_records[0]
                        second_preview = ", ".join(f"{value:.6g}" for value in second_first)
                        second_c0 = Counter(round(record[0], 6) for record in second_records)
                        second_c1 = Counter(round(record[1], 6) for record in second_records)
                        second_c0_common = ", ".join(f"{value:.6g}x{count}" for value, count in second_c0.most_common(4))
                        second_c1_common = ", ".join(f"{value:.6g}x{count}" for value, count in second_c1.most_common(4))
                        print(f"    second_block_stride={second_stride} second_block_record_count={len(second_records)}")
                        print(f"    second_block_first_record=[{second_preview}]")
                        print(f"    second_block_c0_common=[{second_c0_common}] second_block_c1_common=[{second_c1_common}]")
                        third_block_start = second_block_start + len(second_records) * second_stride
                        third_records = parse_third_block_records(data, third_block_start)
                        if third_records:
                            third_first = third_records[0]
                            third_preview = ", ".join(f"{value:.6g}" for value in third_first)
                            third_words = [struct.unpack_from("<I", data, third_block_start + 32 + i * 4)[0] for i in range(8)]
                            third_scaffold = ", ".join(f"0x{word:08x}" for word in third_words)
                            print(f"    third_block_record_count={len(third_records)} third_block_stride=64")
                            print(f"    third_block_first_record=[{third_preview}]")
                            print(f"    third_block_scaffold=[{third_scaffold}]")
            else:
                compact_count_tag, compact_records = parse_compact_tail_records(data, payload_offset)
                if compact_records:
                    step_values = [record[1] for record in compact_records]
                    aux_values = [record[2] for record in compact_records]
                    step_preview = ", ".join(f"{value:.6g}" for value in step_values[:16])
                    aux_preview = ", ".join(f"{value:.6g}" for value in aux_values[:16])
                    compact_first = compact_records[0]
                    print(
                        f"    compact_tail_record_count={len(compact_records)} compact_tail_stride=56 "
                        f"compact_tail_count_tag={compact_count_tag}"
                    )
                    print(f"    compact_tail_step_preview=[{step_preview}]")
                    print(f"    compact_tail_aux_preview=[{aux_preview}]")
                    print(
                        "    "
                        f"compact_tail_first_record="
                        f"[count={compact_first[0]}, step={compact_first[1]:.6g}, aux={compact_first[2]:.6g}, "
                        f"pos=({compact_first[3][0]:.6g}, {compact_first[3][1]:.6g}, {compact_first[3][2]:.6g}), "
                        f"dir=({compact_first[4][0]:.6g}, {compact_first[4][1]:.6g}, {compact_first[4][2]:.6g})]"
                    )
        max_offset = min(len(data), payload_offset + window_bytes)
        for offset in range(payload_offset, max_offset, 4):
            print(
                f"    @{offset}: u32={format_u32(data, offset)} f32={format_f32(data, offset)}"
            )


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    expected_vertex_counts: dict[Path, int] = {}
    if args.package:
        paths, expected_vertex_counts = iter_package_spt_paths(
            Path(args.package).resolve(),
            args.export_name,
            args.all_package_speedtrees,
            args.limit,
        )
    else:
        paths = [Path(path).resolve() for path in args.paths] if args.paths else list(iter_default_spt_paths())
    if not paths:
        raise SystemExit("No .spt files found to analyze")

    for path in paths:
        if not path.exists():
            print(f"missing={path}")
            continue
        analyze_file(path, args.window_bytes, expected_vertex_counts.get(path.resolve()))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))