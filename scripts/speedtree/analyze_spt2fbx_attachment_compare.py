#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import math
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean


@dataclass
class OurBillboardGroup:
    center: tuple[float, float, float]
    half_size: float
    count: int


@dataclass
class RuntimeCard:
    material: str
    center: tuple[float, float, float]
    radius: float


def load_gltf(path: Path) -> tuple[dict, bytes]:
    data = json.loads(path.read_text())
    uri = data["buffers"][0]["uri"]
    if uri.startswith("data:"):
        blob = base64.b64decode(uri.split(",", 1)[1])
    else:
        blob = (path.parent / uri).read_bytes()
    return data, blob


def read_accessor(data: dict, blob: bytes, accessor_index: int):
    accessor = data["accessors"][accessor_index]
    buffer_view = data["bufferViews"][accessor["bufferView"]]
    start = buffer_view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    stride = buffer_view.get("byteStride") or {
        "SCALAR": 4,
        "VEC2": 8,
        "VEC3": 12,
        "VEC4": 16,
    }[accessor["type"]]
    component_count = {
        "SCALAR": 1,
        "VEC2": 2,
        "VEC3": 3,
        "VEC4": 4,
    }[accessor["type"]]

    component_type = accessor["componentType"]
    if component_type == 5126:
        fmt = "<" + "f" * component_count
    elif component_type == 5123:
        fmt = "<" + "H" * component_count
    elif component_type == 5125:
        fmt = "<" + "I" * component_count
    else:
        raise ValueError(f"Unsupported componentType {component_type}")

    values = []
    for index in range(accessor["count"]):
        value = struct.unpack_from(fmt, blob, start + index * stride)
        values.append(value if component_count > 1 else value[0])
    return values


def compute_bbox(points: list[tuple[float, float, float]]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    mins = tuple(min(point[axis] for point in points) for axis in range(3))
    maxs = tuple(max(point[axis] for point in points) for axis in range(3))
    return mins, maxs


def normalize_point(point: tuple[float, float, float], mins, maxs) -> tuple[float, float, float]:
    out = []
    for axis in range(3):
        span = max(maxs[axis] - mins[axis], 1e-6)
        out.append((point[axis] - mins[axis]) / span)
    return tuple(out)


def runtime_center_to_gltf(center_xy: list[float], center_z: float) -> tuple[float, float, float]:
    return (-center_xy[1], center_z, center_xy[0])


def extract_our_billboards(path: Path) -> tuple[list[tuple[float, float, float]], list[OurBillboardGroup]]:
    data, blob = load_gltf(path)
    mesh = data["meshes"][0]
    all_positions: list[tuple[float, float, float]] = []
    groups: dict[tuple[float, float, float], OurBillboardGroup] = {}

    for primitive in mesh["primitives"]:
        positions = read_accessor(data, blob, primitive["attributes"]["POSITION"])
        all_positions.extend(positions)
        billboard_accessor = primitive["attributes"].get("_BILLBOARD")
        if billboard_accessor is None:
            continue
        billboards = read_accessor(data, blob, billboard_accessor)
        for position, billboard in zip(positions, billboards):
            if billboard[2] <= 0:
                continue
            key = tuple(round(value, 6) for value in position)
            existing = groups.get(key)
            if existing is None:
                groups[key] = OurBillboardGroup(center=position, half_size=billboard[2], count=1)
            else:
                existing.count += 1

    return all_positions, list(groups.values())


def extract_runtime_cards(path: Path) -> tuple[list[tuple[float, float, float]], list[RuntimeCard], dict[str, int]]:
    data, blob = load_gltf(path)
    mesh = data["meshes"][0]
    materials = data["materials"]
    all_positions: list[tuple[float, float, float]] = []
    cards: list[RuntimeCard] = []
    material_vertex_counts: dict[str, int] = {}

    for primitive in mesh["primitives"]:
        positions = read_accessor(data, blob, primitive["attributes"]["POSITION"])
        all_positions.extend(positions)
        material_name = materials[primitive["material"]]["name"]
        material_vertex_counts[material_name] = material_vertex_counts.get(material_name, 0) + len(positions)
        if material_name != "LeafMAT":
            continue
        for index in range(0, len(positions) - 3, 4):
            quad = positions[index : index + 4]
            center = tuple(sum(vertex[axis] for vertex in quad) / 4.0 for axis in range(3))
            radius = max(math.dist(center, vertex) for vertex in quad)
            cards.append(RuntimeCard(material=material_name, center=center, radius=radius))

    return all_positions, cards, material_vertex_counts


def extract_runtime_cards_from_json(path: Path) -> tuple[list[tuple[float, float, float]], list[RuntimeCard], dict[str, int]]:
    data = json.loads(path.read_text())
    cards: list[RuntimeCard] = []
    all_positions: list[tuple[float, float, float]] = []
    for card in data["cards"]:
        center = runtime_center_to_gltf(card["center_xy"], card["center_z"])
        cards.append(
            RuntimeCard(
                material="LeafMAT",
                center=center,
                radius=float(card["size_xy_values"][0][0]) / 2.0,
            )
        )
        for vertex_record in card.get("vertex_records", []):
            all_positions.append(tuple(vertex_record["position_gltf"]))
    material_vertex_counts = {"LeafMAT": data.get("leaf_control_point_count", len(all_positions))}
    return all_positions, cards, material_vertex_counts


def summarize(ours_path: Path, runtime_path: Path, runtime_card_json_path: Path | None = None) -> dict:
    our_positions, our_groups = extract_our_billboards(ours_path)
    if runtime_card_json_path:
        runtime_positions, runtime_cards, runtime_material_vertex_counts = extract_runtime_cards_from_json(runtime_card_json_path)
        runtime_source_note = f"Recovered runtime card JSON: {runtime_card_json_path}"
    else:
        runtime_positions, runtime_cards, runtime_material_vertex_counts = extract_runtime_cards(runtime_path)
        runtime_source_note = "Converted runtime glTF quad centroids"

    our_bbox = compute_bbox(our_positions)
    runtime_bbox = compute_bbox(runtime_positions)
    our_norm = [normalize_point(group.center, *our_bbox) for group in our_groups]
    runtime_norm = [normalize_point(card.center, *runtime_bbox) for card in runtime_cards]

    nearest_distances = []
    for point in runtime_norm:
        nearest_distances.append(min(math.dist(point, other) for other in our_norm))

    return {
        "ours": {
            "path": str(ours_path),
            "billboard_group_count": len(our_groups),
            "half_size_range": [min(group.half_size for group in our_groups), max(group.half_size for group in our_groups)],
            "bbox": [*our_bbox[0], *our_bbox[1]],
            "sample_centers": [list(group.center) for group in our_groups[:5]],
        },
        "runtime": {
            "path": str(runtime_path),
            "card_json_path": str(runtime_card_json_path) if runtime_card_json_path else None,
            "center_source": runtime_source_note,
            "leaf_card_count": len(runtime_cards),
            "radius_range": [min(card.radius for card in runtime_cards), max(card.radius for card in runtime_cards)],
            "bbox": [*runtime_bbox[0], *runtime_bbox[1]],
            "material_vertex_counts": runtime_material_vertex_counts,
            "sample_centers": [list(card.center) for card in runtime_cards[:5]],
        },
        "normalized_center_delta": {
            "min": min(nearest_distances),
            "max": max(nearest_distances),
            "avg": mean(nearest_distances),
        },
        "notes": [
            "Our exporter stores collapsed billboard anchors in _BILLBOARD and reconstructs a synthetic quad in the shader.",
            "Only LeafMAT is treated as authoritative billboard-card geometry here. The original FBX carries SizeXY/Center/Pivot channels on LeafMAT but not on FrondMAT.",
            f"Runtime center source: {runtime_source_note}.",
        ],
    }


def build_markdown(summary: dict) -> str:
    ours = summary["ours"]
    runtime = summary["runtime"]
    delta = summary["normalized_center_delta"]
    lines = [
        "# Spt2Fbx Attachment Compare",
        "",
        f"- Our export: `{ours['path']}`",
        f"- Runtime export: `{runtime['path']}`",
        f"- Runtime center source: {runtime['center_source']}",
        "",
        "## Counts",
        "",
        f"- Our collapsed billboard groups: {ours['billboard_group_count']}",
        f"- Runtime leaf cards: {runtime['leaf_card_count']}",
        f"- Runtime material vertex counts: {runtime['material_vertex_counts']}",
        "",
        "## Size",
        "",
        f"- Our half-size range: {ours['half_size_range'][0]:.4f} .. {ours['half_size_range'][1]:.4f}",
        f"- Runtime card radius range: {runtime['radius_range'][0]:.4f} .. {runtime['radius_range'][1]:.4f}",
        "",
        "## Normalized Center Delta",
        "",
        f"- Min nearest-center distance: {delta['min']:.4f}",
        f"- Max nearest-center distance: {delta['max']:.4f}",
        f"- Avg nearest-center distance: {delta['avg']:.4f}",
        "",
        "## Notes",
        "",
    ]
    lines.extend(f"- {note}" for note in summary["notes"])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare runtime Spt2Fbx card placement against our billboard export.")
    parser.add_argument("--ours", type=Path, required=True, help="Path to our exporter glTF")
    parser.add_argument("--runtime", type=Path, required=True, help="Path to Spt2Fbx-derived glTF")
    parser.add_argument("--runtime-card-json", type=Path, help="Optional reconstructed runtime leaf-card JSON")
    parser.add_argument("--json-out", type=Path, help="Optional JSON summary output")
    parser.add_argument("--markdown-out", type=Path, help="Optional Markdown summary output")
    args = parser.parse_args()

    summary = summarize(args.ours, args.runtime, args.runtime_card_json)
    print(json.dumps(summary, indent=2))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(summary, indent=2) + "\n")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(build_markdown(summary))


if __name__ == "__main__":
    main()