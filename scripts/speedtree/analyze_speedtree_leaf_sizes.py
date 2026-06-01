#!/usr/bin/env python3
"""Build a comparison table for size-related SpeedTree leaf fields."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPTS_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT))

import config
from scripts.speedtree.run_spttools_lite import DEFAULT_SPTTOOLS_DIR, ensure_spttools_lite_binaries, run_command


DEFAULT_OUTPUT_STEM = Path(config.DATA_DIR) / "speedtree_leaf_size_analysis"
DEFAULT_SPT_DIR = Path(config.DATA_DIR) / "speedtree_spt"
DEFAULT_MESH_DIR = Path(config.OUTPUT_DIR) / "meshes" / "buildings" / "Ra5000_P1_C1_SpeedTrees_mesh"
RATIO_COLUMNS = [
    "half_size_over_4006",
    "half_size_over_4005",
    "half_size_over_9009",
    "half_size_over_global_size",
    "half_size_over_4006_x_9009",
    "half_size_over_4006_x_global_size",
    "half_size_over_4005_x_global_size",
    "half_size_over_4005_x_global_size_x_9009",
]

SECTION_LABELS = {
    1006: "leaf_texture_map_count",
    2006: "global_size",
    2007: "global_size_variance",
    4005: "leaf_map_size_xy",
    4006: "leaf_prototype_size_xy_candidate",
    4007: "leaf_unknown_scalar_4007",
    9003: "billboard_radius",
    9004: "billboard_exponent",
    9009: "billboard_size_pct",
    9011: "billboard_num_lods",
    16014: "leaf_transition_pct_halved",
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spt-dir",
        default=str(DEFAULT_SPT_DIR),
        help="Directory containing dumped .spt files",
    )
    parser.add_argument(
        "--mesh-dir",
        default=str(DEFAULT_MESH_DIR),
        help="Directory containing matching exported SpeedTree glTF files",
    )
    parser.add_argument(
        "--spttools-dir",
        default=str(DEFAULT_SPTTOOLS_DIR),
        help="Path to local spttools checkout",
    )
    parser.add_argument(
        "--output-stem",
        default=str(DEFAULT_OUTPUT_STEM),
        help="Output file stem; writes .csv and .md next to it",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force rebuild of sptparser-lite before analysis",
    )
    return parser.parse_args(argv)


def parse_sptparser_output(output: str) -> dict[int, list[list[float | int]]]:
    values_by_section: dict[int, list[list[float | int]]] = {}
    current_section: int | None = None
    current_values: list[float | int] = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("section = "):
            current_section = int(line.split("=", 1)[1].strip())
            current_values = []
            continue
        if line.startswith("float = "):
            current_values.append(float(line.split("=", 1)[1].strip()))
            continue
        if line.startswith("int = "):
            current_values.append(int(line.split("=", 1)[1].strip()))
            continue
        if line.startswith("byte = "):
            current_values.append(int(line.split("=", 1)[1].strip()))
            continue
        if line.startswith("desc = ") and current_section is not None:
            if current_section in SECTION_LABELS:
                values_by_section.setdefault(current_section, []).append(current_values[:])
            current_section = None
            current_values = []

    return values_by_section


def format_occurrences(occurrences: list[list[float | int]]) -> str:
    if not occurrences:
        return ""
    formatted = []
    for values in occurrences:
        if not values:
            formatted.append("[]")
            continue
        parts = []
        for value in values:
            if isinstance(value, int):
                parts.append(str(value))
            else:
                parts.append(f"{value:.6g}")
        formatted.append("[" + ", ".join(parts) + "]")
    return "; ".join(formatted)


def extract_first_scalar(occurrences: list[list[float | int]]) -> float | int | None:
    if not occurrences or not occurrences[0]:
        return None
    return occurrences[0][0]


def extract_first_xy(occurrences: list[list[float | int]]) -> tuple[float, float] | None:
    if not occurrences or len(occurrences[0]) < 2:
        return None
    return float(occurrences[0][0]), float(occurrences[0][1])


def load_gltf_billboard_half_size(gltf_path: Path) -> float | None:
    if not gltf_path.exists():
        return None

    gltf = json.loads(gltf_path.read_text(encoding="utf-8"))
    uri = gltf.get("buffers", [{}])[0].get("uri")
    if not uri or "," not in uri:
        return None

    blob = base64.b64decode(uri.split(",", 1)[1])
    sizes: list[float] = []
    for mesh in gltf.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            accessor_index = primitive.get("attributes", {}).get("_BILLBOARD")
            if accessor_index is None:
                continue
            accessor = gltf["accessors"][accessor_index]
            buffer_view = gltf["bufferViews"][accessor["bufferView"]]
            start = buffer_view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
            stride = buffer_view.get("byteStride", 12)
            for i in range(accessor["count"]):
                base = start + i * stride
                size = float.fromhex("0x0")
                import struct

                _sx, _sy, size = struct.unpack_from("<fff", blob, base)
                if size > 0:
                    sizes.append(size)
    if not sizes:
        return None
    return statistics.fmean(sizes)


def find_matching_gltf(mesh_dir: Path, spt_stem: str) -> Path | None:
    candidates = [mesh_dir / f"{spt_stem}.gltf"]
    if "__" in spt_stem:
        candidates.append(mesh_dir / f"{spt_stem.split('__', 1)[1]}.gltf")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def add_ratio(row: dict[str, Any], ratio_key: str, numerator: float | None, denominator: float | None) -> None:
    if numerator is None or denominator in (None, 0):
        row[ratio_key] = None
        return
    row[ratio_key] = round(numerator / denominator, 6)


def summarize_ratio_columns(rows: list[dict[str, Any]], ratio_columns: list[str]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for column in ratio_columns:
        values = [float(row[column]) for row in rows if row.get(column) is not None]
        if not values:
            continue
        mean_value = statistics.fmean(values)
        stdev_value = statistics.stdev(values) if len(values) > 1 else 0.0
        cv_value = stdev_value / mean_value if mean_value else float("inf")
        summaries.append(
            {
                "ratio_column": column,
                "count": len(values),
                "mean": round(mean_value, 6),
                "stdev": round(stdev_value, 6),
                "cv": round(cv_value, 6),
                "min": round(min(values), 6),
                "max": round(max(values), 6),
            }
        )
    summaries.sort(key=lambda item: (item["cv"], item["stdev"]))
    return summaries


def build_json_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ratio_summaries = summarize_ratio_columns(rows, RATIO_COLUMNS)
    ratio_means = {summary["ratio_column"]: summary["mean"] for summary in ratio_summaries}

    meshes: dict[str, dict[str, Any]] = {}
    for row in rows:
        current_half_size = row.get("current_exported_billboard_half_size")
        predicted_4006 = None
        predicted_4005_global = None
        scale_4006 = None
        scale_4005_global = None

        if row.get("prototype_size_x") is not None and ratio_means.get("half_size_over_4006") is not None:
            predicted_4006 = round(float(row["prototype_size_x"]) * float(ratio_means["half_size_over_4006"]), 6)
        if row.get("map_times_global_size") is not None and ratio_means.get("half_size_over_4005_x_global_size") is not None:
            predicted_4005_global = round(
                float(row["map_times_global_size"]) * float(ratio_means["half_size_over_4005_x_global_size"]),
                6,
            )
        if current_half_size not in (None, 0) and predicted_4006 is not None:
            scale_4006 = round(predicted_4006 / float(current_half_size), 6)
        if current_half_size not in (None, 0) and predicted_4005_global is not None:
            scale_4005_global = round(predicted_4005_global / float(current_half_size), 6)

        entry = {
            "stem": row.get("stem"),
            "gltf_file": row.get("gltf_file"),
            "current_exported_billboard_half_size": current_half_size,
            "map_size_x": row.get("map_size_x"),
            "prototype_size_x": row.get("prototype_size_x"),
            "global_size": row.get("global_size"),
            "billboard_size_pct": row.get("billboard_size_pct"),
            "map_times_global_size": row.get("map_times_global_size"),
            "predicted_half_size_4006": predicted_4006,
            "predicted_half_size_4005_x_global_size": predicted_4005_global,
            "theory_scale_4006": scale_4006,
            "theory_scale_4005_x_global_size": scale_4005_global,
            "theory_distance_boost_max_9009": round(1.0 + float(row["billboard_size_pct"]), 6)
            if row.get("billboard_size_pct") is not None
            else None,
        }

        keys = {str(row.get("stem", "")).lower()}
        gltf_file = row.get("gltf_file")
        if gltf_file:
            keys.add(Path(str(gltf_file)).stem.lower())
        for key in keys:
            if key:
                meshes[key] = entry

    return {
        "ratio_summaries": ratio_summaries,
        "ratio_means": ratio_means,
        "meshes": meshes,
    }


def build_rows(spt_dir: Path, mesh_dir: Path, parser_bin: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for spt_path in sorted(spt_dir.glob("*.spt")):
        parse_result = run_command([str(parser_bin), str(spt_path)], parser_bin.parent)
        if parse_result.returncode != 0 and not parse_result.stdout:
            raise SystemExit(parse_result.stderr or f"Failed to parse {spt_path}")

        values = parse_sptparser_output(parse_result.stdout)
        stem = spt_path.stem.lower()
        gltf_path = find_matching_gltf(mesh_dir, spt_path.stem)
        half_size = load_gltf_billboard_half_size(gltf_path)

        map_size_xy = extract_first_xy(values.get(4005, []))
        prototype_size_xy = extract_first_xy(values.get(4006, []))
        billboard_size_pct = extract_first_scalar(values.get(9009, []))
        global_size = extract_first_scalar(values.get(2006, []))
        map_size_x = round(map_size_xy[0], 6) if map_size_xy else None
        prototype_size_x = round(prototype_size_xy[0], 6) if prototype_size_xy else None
        prototype_times_billboard_pct = None
        prototype_times_global_size = None
        map_times_global_size = None
        map_times_global_times_billboard_pct = None

        if prototype_size_xy and billboard_size_pct is not None:
            prototype_times_billboard_pct = round(prototype_size_xy[0] * float(billboard_size_pct), 6)
        if prototype_size_xy and global_size is not None:
            prototype_times_global_size = round(prototype_size_xy[0] * float(global_size), 6)
        if map_size_xy and global_size is not None:
            map_times_global_size = round(map_size_xy[0] * float(global_size), 6)
        if map_size_xy and global_size is not None and billboard_size_pct is not None:
            map_times_global_times_billboard_pct = round(map_size_xy[0] * float(global_size) * float(billboard_size_pct), 6)

        row: dict[str, Any] = {
            "stem": stem,
            "spt_file": spt_path.name,
            "gltf_file": gltf_path.name if gltf_path is not None else "",
            "leaf_texture_map_count": extract_first_scalar(values.get(1006, [])),
            "global_size": global_size,
            "global_size_variance": extract_first_scalar(values.get(2007, [])),
            "leaf_map_size_xy": format_occurrences(values.get(4005, [])),
            "leaf_prototype_size_xy_candidate": format_occurrences(values.get(4006, [])),
            "leaf_unknown_scalar_4007": format_occurrences(values.get(4007, [])),
            "billboard_radius": extract_first_scalar(values.get(9003, [])),
            "billboard_exponent": extract_first_scalar(values.get(9004, [])),
            "billboard_size_pct": billboard_size_pct,
            "billboard_num_lods": extract_first_scalar(values.get(9011, [])),
            "leaf_transition_pct_halved": extract_first_scalar(values.get(16014, [])),
            "current_exported_billboard_half_size": round(half_size, 6) if half_size is not None else None,
            "map_size_x": map_size_x,
            "prototype_size_x": prototype_size_x,
            "prototype_times_billboard_pct": prototype_times_billboard_pct,
            "prototype_times_global_size": prototype_times_global_size,
            "map_times_global_size": map_times_global_size,
            "map_times_global_times_billboard_pct": map_times_global_times_billboard_pct,
        }

        add_ratio(row, "half_size_over_4006", half_size, prototype_size_x)
        add_ratio(row, "half_size_over_4005", half_size, map_size_x)
        add_ratio(row, "half_size_over_9009", half_size, float(billboard_size_pct) if billboard_size_pct is not None else None)
        add_ratio(row, "half_size_over_global_size", half_size, float(global_size) if global_size is not None else None)
        add_ratio(row, "half_size_over_4006_x_9009", half_size, prototype_times_billboard_pct)
        add_ratio(row, "half_size_over_4006_x_global_size", half_size, prototype_times_global_size)
        add_ratio(row, "half_size_over_4005_x_global_size", half_size, map_times_global_size)
        add_ratio(
            row,
            "half_size_over_4005_x_global_size_x_9009",
            half_size,
            map_times_global_times_billboard_pct,
        )

        rows.append(row)

    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(payload: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    ratio_summaries = summarize_ratio_columns(rows, RATIO_COLUMNS)
    columns = [
        "stem",
        "leaf_texture_map_count",
        "global_size",
        "leaf_map_size_xy",
        "leaf_prototype_size_xy_candidate",
        "billboard_size_pct",
        "current_exported_billboard_half_size",
        "half_size_over_4006",
        "half_size_over_4005",
        "half_size_over_9009",
        "half_size_over_4006_x_9009",
        "half_size_over_4005_x_global_size",
    ]
    lines = [
        "# SpeedTree Leaf Size Analysis",
        "",
        "This table compares dumped SPT leaf-size candidates against the currently exported glTF billboard half-size.",
        "",
        "## Ratio stability summary",
        "",
        "Lower coefficient of variation means the candidate behaves more like a consistent base size that the runtime multiplies by one shared factor.",
        "",
        "| ratio_column | count | mean | stdev | cv | min | max |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for summary in ratio_summaries:
        lines.append(
            "| {ratio_column} | {count} | {mean} | {stdev} | {cv} | {min} | {max} |".format(**summary)
        )
    lines.extend(
        [
            "",
            "## Per-tree table",
            "",
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join(["---"] * len(columns)) + " |",
        ]
    )
    for row in rows:
        values = ["" if row.get(column) is None else str(row.get(column)) for column in columns]
        lines.append("| " + " | ".join(values) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    spt_dir = Path(args.spt_dir).resolve()
    mesh_dir = Path(args.mesh_dir).resolve()
    output_stem = Path(args.output_stem).resolve()
    output_stem.parent.mkdir(parents=True, exist_ok=True)

    parser_bin, _compiler_bin = ensure_spttools_lite_binaries(Path(args.spttools_dir).resolve(), args.rebuild)
    rows = build_rows(spt_dir, mesh_dir, parser_bin)
    json_payload = build_json_payload(rows)

    csv_path = output_stem.with_suffix(".csv")
    json_path = output_stem.with_suffix(".json")
    md_path = output_stem.with_suffix(".md")
    write_csv(rows, csv_path)
    write_json(json_payload, json_path)
    write_markdown(rows, md_path)

    print(f"rows={len(rows)}")
    print(f"csv_output={csv_path}")
    print(f"json_output={json_path}")
    print(f"markdown_output={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))