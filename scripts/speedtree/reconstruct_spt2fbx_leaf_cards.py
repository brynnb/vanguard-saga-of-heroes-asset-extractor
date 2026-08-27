#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPTS_DIR.parent


from scripts.speedtree import extract_spt2fbx_fbx_layers as fbx_layers


def round_pair(pair, digits: int = 6) -> tuple[float, float]:
    return (round(pair[0], digits), round(pair[1], digits))


def extract_vertices(nodes) -> list[tuple[float, float, float]]:
    geometries = fbx_layers.find_object_nodes(nodes, "Geometry")
    if not geometries:
        raise ValueError("No Geometry node found in FBX")
    vertices_node = fbx_layers.find_first_child(geometries[0], "Vertices")
    if not vertices_node or not vertices_node.properties:
        raise ValueError("Geometry has no Vertices array")
    raw = vertices_node.properties[0]
    return list(zip(raw[0::3], raw[1::3], raw[2::3]))


def extract_layer_pair_values(nodes) -> dict[str, list[tuple[float, float]]]:
    values = {}
    for root in nodes:
        for node in fbx_layers.walk(root):
            if not node.name.startswith("LayerElement"):
                continue
            name_child = next((child for child in node.children if child.name == "Name" and child.properties), None)
            uv_child = next((child for child in node.children if child.name == "UV" and child.properties), None)
            if not name_child or not uv_child:
                continue
            raw = uv_child.properties[0]
            values[name_child.properties[0]] = list(zip(raw[0::2], raw[1::2]))
    return values


def vanguard_to_gltf(vertex: tuple[float, float, float]) -> tuple[float, float, float]:
    vx, vy, vz = vertex
    return (-vy, vz, vx)


def build_leaf_cards(fbx_path: Path) -> dict:
    version, nodes = fbx_layers.read_fbx(fbx_path)
    topology = fbx_layers.extract_faces_and_materials(nodes)
    if topology is None:
        raise ValueError("Could not extract geometry topology from FBX")
    _, faces, face_materials = topology
    material_names = fbx_layers.extract_material_names(nodes)
    if not material_names:
        raise ValueError("Could not extract material names from FBX")
    try:
        leaf_material_index = material_names.index("LeafMAT")
    except ValueError as exc:
        raise ValueError("FBX has no LeafMAT material") from exc

    vertices = extract_vertices(nodes)
    pairs = extract_layer_pair_values(nodes)
    required_layers = ["DiffuseUV", "SizeXY", "CenterXY", "CenterZDimming", "PivotXY"]
    missing = [layer for layer in required_layers if layer not in pairs]
    if missing:
        raise ValueError(f"Missing expected layer(s): {missing}")

    leaf_control_points = sorted(
        {cp for face_index, face in enumerate(faces) if face_materials[face_index] == leaf_material_index for cp in face}
    )

    card_groups = defaultdict(list)
    for cp_index in leaf_control_points:
        key = (
            *round_pair(pairs["CenterXY"][cp_index]),
            *round_pair(pairs["CenterZDimming"][cp_index]),
        )
        card_groups[key].append(cp_index)

    cards = []
    for card_id, (group_key, control_points) in enumerate(sorted(card_groups.items()), 1):
        control_points = sorted(control_points)
        group_vertices = [vertices[index] for index in control_points]
        group_vertices_gltf = [vanguard_to_gltf(vertex) for vertex in group_vertices]
        size_values = sorted({round_pair(pairs["SizeXY"][index]) for index in control_points})
        diffuse_values = sorted({round_pair(pairs["DiffuseUV"][index]) for index in control_points})
        pivot_values = sorted({round_pair(pairs["PivotXY"][index]) for index in control_points})
        vertex_records = []
        for cp_index, vertex_vanguard, vertex_gltf in zip(control_points, group_vertices, group_vertices_gltf):
            vertex_records.append(
                {
                    "control_point": cp_index,
                    "position_vanguard": list(vertex_vanguard),
                    "position_gltf": list(vertex_gltf),
                    "diffuse_uv": list(round_pair(pairs["DiffuseUV"][cp_index])),
                    "pivot_uv": list(round_pair(pairs["PivotXY"][cp_index])),
                }
            )
        vertex_records.sort(key=lambda item: (item["diffuse_uv"][0], item["diffuse_uv"][1], item["control_point"]))

        avg_vanguard = tuple(mean(vertex[axis] for vertex in group_vertices) for axis in range(3))
        avg_gltf = tuple(mean(vertex[axis] for vertex in group_vertices_gltf) for axis in range(3))
        bbox_min = tuple(min(vertex[axis] for vertex in group_vertices) for axis in range(3))
        bbox_max = tuple(max(vertex[axis] for vertex in group_vertices) for axis in range(3))

        cards.append(
            {
                "card_id": card_id,
                "control_points": control_points,
                "control_point_count": len(control_points),
                "center_xy": [group_key[0], group_key[1]],
                "center_z": group_key[2],
                "dimming": group_key[3],
                "size_xy_values": [list(value) for value in size_values],
                "pivot_xy_values": [list(value) for value in pivot_values],
                "diffuse_uv_values": [list(value) for value in diffuse_values],
                "vertex_records": vertex_records,
                "avg_position_vanguard": list(avg_vanguard),
                "avg_position_gltf": list(avg_gltf),
                "bbox_vanguard": {"min": list(bbox_min), "max": list(bbox_max)},
            }
        )

    size_histogram = Counter(tuple(card["size_xy_values"][0]) for card in cards)
    pivot_histogram = Counter(tuple(tuple(value) for value in card["pivot_xy_values"]) for card in cards)
    diffuse_histogram = Counter(len(card["diffuse_uv_values"]) for card in cards)
    group_size_histogram = Counter(card["control_point_count"] for card in cards)
    offset_groups = defaultdict(list)
    for card in cards:
        pivot_key = tuple(tuple(value) for value in card["pivot_xy_values"])
        avg_vanguard = card["avg_position_vanguard"]
        offset_groups[pivot_key].append(
            (
                card["center_xy"][0] - avg_vanguard[0],
                card["center_xy"][1] - avg_vanguard[1],
                card["center_z"] - avg_vanguard[2],
            )
        )

    offset_summary = {}
    for pivot_key, offsets in sorted(offset_groups.items()):
        xs = [value[0] for value in offsets]
        ys = [value[1] for value in offsets]
        zs = [value[2] for value in offsets]
        offset_summary[str([list(item) for item in pivot_key])] = {
            "count": len(offsets),
            "dx_range": [min(xs), max(xs)],
            "dy_range": [min(ys), max(ys)],
            "dz_range": [min(zs), max(zs)],
            "dx_avg": mean(xs),
            "dy_avg": mean(ys),
            "dz_avg": mean(zs),
        }

    payload = {
        "file": str(fbx_path),
        "fbx_version": version,
        "material_names": material_names,
        "leaf_control_point_count": len(leaf_control_points),
        "leaf_card_count": len(cards),
        "group_size_histogram": dict(sorted(group_size_histogram.items())),
        "size_histogram": {str(list(key)): value for key, value in sorted(size_histogram.items())},
        "pivot_histogram": {str([list(item) for item in key]): value for key, value in sorted(pivot_histogram.items())},
        "diffuse_variant_histogram": dict(sorted(diffuse_histogram.items())),
        "center_minus_avg_by_pivot": offset_summary,
        "cards": cards,
    }
    return payload


def build_markdown(payload: dict) -> str:
    lines = [
        "# Spt2Fbx Leaf Card Reconstruction",
        "",
        f"- File: `{payload['file']}`",
        f"- Leaf control points: {payload['leaf_control_point_count']}",
        f"- Reconstructed leaf cards: {payload['leaf_card_count']}",
        f"- Group size histogram: {payload['group_size_histogram']}",
        f"- Size histogram: {payload['size_histogram']}",
        f"- Pivot histogram: {payload['pivot_histogram']}",
        f"- Diffuse variant histogram: {payload['diffuse_variant_histogram']}",
        f"- Center-minus-average by pivot: {payload['center_minus_avg_by_pivot']}",
        "",
        "## Sample Cards",
        "",
    ]
    for card in payload["cards"][:8]:
        lines.extend(
            [
                f"### Card {card['card_id']}",
                "",
                f"- Control points: {card['control_points']}",
                f"- CenterXY: {card['center_xy']}",
                f"- CenterZ/Dimming: [{card['center_z']}, {card['dimming']}]",
                f"- SizeXY: {card['size_xy_values']}",
                f"- PivotXY: {card['pivot_xy_values']}",
                f"- DiffuseUV: {card['diffuse_uv_values']}",
                f"- Vertex records: {card['vertex_records']}",
                f"- Avg Vanguard position: {card['avg_position_vanguard']}",
                f"- Avg glTF position: {card['avg_position_gltf']}",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct explicit leaf card records from an Spt2Fbx FBX file.")
    parser.add_argument("fbx", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    payload = build_leaf_cards(args.fbx)
    print(json.dumps(payload, indent=2))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2) + "\n")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(build_markdown(payload))


if __name__ == "__main__":
    main()