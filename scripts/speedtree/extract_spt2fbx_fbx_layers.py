#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean


MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"


@dataclass
class FbxNode:
    name: str
    properties: list[object] = field(default_factory=list)
    children: list["FbxNode"] = field(default_factory=list)


SCALAR_FORMATS = {
    b"Y": "<h",
    b"C": "<?",
    b"I": "<i",
    b"F": "<f",
    b"D": "<d",
    b"L": "<q",
}

ARRAY_FORMATS = {
    b"b": "<{}b",
    b"i": "<{}i",
    b"l": "<{}q",
    b"f": "<{}f",
    b"d": "<{}d",
}


def read_exact(handle, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise EOFError(f"Expected {size} bytes, got {len(data)}")
    return data


def read_scalar(handle, code: bytes):
    fmt = SCALAR_FORMATS.get(code)
    if fmt is None:
        raise ValueError(f"Unsupported scalar property type {code!r}")
    return struct.unpack(fmt, read_exact(handle, struct.calcsize(fmt)))[0]


def read_array(handle, code: bytes):
    length, encoding, compressed_length = struct.unpack("<III", read_exact(handle, 12))
    raw = read_exact(handle, compressed_length)
    payload = zlib.decompress(raw) if encoding == 1 else raw
    fmt = ARRAY_FORMATS.get(code)
    if fmt is None:
        raise ValueError(f"Unsupported array property type {code!r}")
    if length == 0:
        return []
    return list(struct.unpack(fmt.format(length), payload))


def read_property(handle):
    code = read_exact(handle, 1)
    if code in SCALAR_FORMATS:
        return read_scalar(handle, code)
    if code in ARRAY_FORMATS:
        return read_array(handle, code)
    if code in {b"S", b"R"}:
        length = struct.unpack("<I", read_exact(handle, 4))[0]
        payload = read_exact(handle, length)
        if code == b"S":
            return payload.decode("utf-8", errors="replace")
        return payload
    raise ValueError(f"Unsupported property type {code!r}")


def null_record_length(version: int) -> int:
    return 25 if version >= 7500 else 13


def read_node(handle, version: int):
    if version >= 7500:
        end_offset, num_props, prop_list_len = struct.unpack("<QQQ", read_exact(handle, 24))
    else:
        end_offset, num_props, prop_list_len = struct.unpack("<III", read_exact(handle, 12))
    name_len = struct.unpack("<B", read_exact(handle, 1))[0]

    if end_offset == 0 and num_props == 0 and prop_list_len == 0 and name_len == 0:
        return None

    name = read_exact(handle, name_len).decode("utf-8", errors="replace")
    properties = [read_property(handle) for _ in range(num_props)]
    children: list[FbxNode] = []

    sentinel = null_record_length(version)
    while handle.tell() < end_offset:
        remaining = end_offset - handle.tell()
        if remaining == sentinel:
            tail = read_exact(handle, sentinel)
            if any(tail):
                raise ValueError(f"Expected null sentinel at {handle.tell() - sentinel}")
            break
        child = read_node(handle, version)
        if child is None:
            break
        children.append(child)

    if handle.tell() != end_offset:
        handle.seek(end_offset)
    return FbxNode(name=name, properties=properties, children=children)


def read_fbx(path: Path) -> tuple[int, list[FbxNode]]:
    with path.open("rb") as handle:
        magic = read_exact(handle, len(MAGIC))
        if magic != MAGIC:
            raise ValueError(f"Not a binary FBX file: {path}")
        version = struct.unpack("<I", read_exact(handle, 4))[0]
        nodes: list[FbxNode] = []
        while True:
            node = read_node(handle, version)
            if node is None:
                break
            nodes.append(node)
        return version, nodes


def walk(node: FbxNode):
    yield node
    for child in node.children:
        yield from walk(child)


def summarize_numeric_pairs(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "pair_count": 0}
    pair_count = len(values) // 2
    xs = values[0::2]
    ys = values[1::2]
    non_zero_pairs = [
        [x_value, y_value]
        for x_value, y_value in zip(xs, ys)
        if abs(x_value) > 1e-6 or abs(y_value) > 1e-6
    ]
    unique_non_zero_pairs = sorted({(round(pair[0], 6), round(pair[1], 6)) for pair in non_zero_pairs})
    sample_pairs = [[xs[index], ys[index]] for index in range(min(5, pair_count))]
    return {
        "count": len(values),
        "pair_count": pair_count,
        "x_range": [min(xs), max(xs)] if xs else None,
        "y_range": [min(ys), max(ys)] if ys else None,
        "x_avg": mean(xs) if xs else None,
        "y_avg": mean(ys) if ys else None,
        "sample_pairs": sample_pairs,
        "non_zero_pair_count": len(non_zero_pairs),
        "sample_non_zero_pairs": non_zero_pairs[:5],
        "unique_non_zero_pair_count": len(unique_non_zero_pairs),
        "sample_unique_non_zero_pairs": [list(pair) for pair in unique_non_zero_pairs[:8]],
    }


def extract_layer_summary(layer_node: FbxNode) -> dict:
    summary = {
        "fbx_node_name": layer_node.name,
        "layer_index": layer_node.properties[0] if layer_node.properties else None,
    }
    for child in layer_node.children:
        if child.name == "Name" and child.properties:
            summary["name"] = child.properties[0]
        elif child.name == "MappingInformationType" and child.properties:
            summary["mapping"] = child.properties[0]
        elif child.name == "ReferenceInformationType" and child.properties:
            summary["reference"] = child.properties[0]
        elif child.name == "UV" and child.properties:
            values = child.properties[0]
            summary["_raw_uv_values"] = values
            summary["uv"] = summarize_numeric_pairs(values)
        elif child.name == "UVIndex" and child.properties:
            values = child.properties[0]
            summary["uv_index"] = {
                "count": len(values),
                "range": [min(values), max(values)] if values else None,
                "sample": values[:12],
            }
    return summary


def collect_layer_summaries(nodes: list[FbxNode]) -> list[dict]:
    summaries: list[dict] = []
    for root in nodes:
        for node in walk(root):
            if node.name.startswith("LayerElement"):
                summary = extract_layer_summary(node)
                if summary.get("name"):
                    summaries.append(summary)
    return summaries


def find_first_child(node: FbxNode, name: str) -> FbxNode | None:
    for child in node.children:
        if child.name == name:
            return child
    return None


def find_first_node(nodes: list[FbxNode], name: str) -> FbxNode | None:
    for root in nodes:
        for node in walk(root):
            if node.name == name:
                return node
    return None


def find_object_nodes(nodes: list[FbxNode], object_name: str) -> list[FbxNode]:
    objects = find_first_node(nodes, "Objects")
    if not objects:
        return []
    return [child for child in objects.children if child.name == object_name]


def clean_fbx_name(raw_name: str) -> str:
    cleaned = raw_name.split("\x00", 1)[0]
    if "::" in cleaned:
        cleaned = cleaned.split("::", 1)[1]
    return cleaned


def extract_material_names(nodes: list[FbxNode]) -> list[str]:
    names = []
    for node in find_object_nodes(nodes, "Material"):
        if len(node.properties) >= 2 and isinstance(node.properties[1], str):
            names.append(clean_fbx_name(node.properties[1]))
    return names


def extract_faces_and_materials(nodes: list[FbxNode]) -> tuple[int, list[list[int]], list[int]] | None:
    geometries = find_object_nodes(nodes, "Geometry")
    if not geometries:
        return None
    geometry = geometries[0]
    vertices_node = find_first_child(geometry, "Vertices")
    polygon_node = find_first_child(geometry, "PolygonVertexIndex")
    layer_material = next((child for child in geometry.children if child.name == "LayerElementMaterial"), None)
    if not vertices_node or not polygon_node or not layer_material:
        return None

    vertex_values = vertices_node.properties[0]
    control_point_count = len(vertex_values) // 3
    polygon_values = polygon_node.properties[0]
    faces: list[list[int]] = []
    current: list[int] = []
    for value in polygon_values:
        if value < 0:
            current.append(-value - 1)
            faces.append(current)
            current = []
        else:
            current.append(value)

    materials_node = find_first_child(layer_material, "Materials")
    face_materials = materials_node.properties[0] if materials_node and materials_node.properties else []
    return control_point_count, faces, face_materials


def enrich_with_material_usage(nodes: list[FbxNode], summaries: list[dict]) -> dict | None:
    topology = extract_faces_and_materials(nodes)
    if topology is None:
        return None
    control_point_count, faces, face_materials = topology
    material_names = extract_material_names(nodes)
    if not material_names:
        material_names = [f"Material{index}" for index in sorted(set(face_materials))]

    material_usage = {
        material_name: {
            "triangle_count": 0,
            "unique_control_points": set(),
        }
        for material_name in material_names
    }

    for face_index, face in enumerate(faces):
        material_index = face_materials[face_index] if face_index < len(face_materials) else 0
        if material_index >= len(material_names):
            material_name = f"Material{material_index}"
            material_usage.setdefault(
                material_name,
                {"triangle_count": 0, "unique_control_points": set()},
            )
        else:
            material_name = material_names[material_index]
        material_usage[material_name]["triangle_count"] += 1
        material_usage[material_name]["unique_control_points"].update(face)

    for summary in summaries:
        raw_values = summary.get("_raw_uv_values")
        if not raw_values:
            continue
        pair_values = list(zip(raw_values[0::2], raw_values[1::2]))
        summary["material_usage"] = {}
        for material_name, usage in material_usage.items():
            indices = sorted(index for index in usage["unique_control_points"] if index < len(pair_values))
            subset = [pair_values[index] for index in indices]
            non_zero = [pair for pair in subset if abs(pair[0]) > 1e-6 or abs(pair[1]) > 1e-6]
            unique_non_zero = sorted({(round(pair[0], 6), round(pair[1], 6)) for pair in non_zero})
            summary["material_usage"][material_name] = {
                "triangle_count": usage["triangle_count"],
                "unique_control_point_count": len(usage["unique_control_points"]),
                "non_zero_pair_count": len(non_zero),
                "sample_non_zero_pairs": [list(pair) for pair in non_zero[:5]],
                "unique_non_zero_pair_count": len(unique_non_zero),
                "sample_unique_non_zero_pairs": [list(pair) for pair in unique_non_zero[:8]],
            }

    return {
        "control_point_count": control_point_count,
        "face_count": len(faces),
        "materials": {
            material_name: {
                "triangle_count": usage["triangle_count"],
                "unique_control_point_count": len(usage["unique_control_points"]),
            }
            for material_name, usage in material_usage.items()
        },
    }


def build_markdown(path: Path, version: int, summaries: list[dict]) -> str:
    lines = [
        "# Spt2Fbx FBX Layer Extraction",
        "",
        f"- File: `{path}`",
        f"- FBX version: {version}",
        f"- Layer elements found: {len(summaries)}",
        "",
    ]
    for summary in summaries:
        lines.extend(
            [
                f"## {summary['name']}",
                "",
                f"- FBX node: {summary['fbx_node_name']}",
                f"- Mapping: {summary.get('mapping')}",
                f"- Reference: {summary.get('reference')}",
            ]
        )
        uv = summary.get("uv")
        if uv:
            lines.extend(
                [
                    f"- UV float count: {uv['count']}",
                    f"- Pair count: {uv['pair_count']}",
                    f"- Non-zero pair count: {uv['non_zero_pair_count']}",
                    f"- Unique non-zero pair count: {uv['unique_non_zero_pair_count']}",
                    f"- X range: {uv['x_range'][0]:.6f} .. {uv['x_range'][1]:.6f}" if uv.get("x_range") else "- X range: n/a",
                    f"- Y range: {uv['y_range'][0]:.6f} .. {uv['y_range'][1]:.6f}" if uv.get("y_range") else "- Y range: n/a",
                    f"- Sample pairs: {uv['sample_pairs']}",
                    f"- Sample non-zero pairs: {uv['sample_non_zero_pairs']}",
                    f"- Sample unique non-zero pairs: {uv['sample_unique_non_zero_pairs']}",
                ]
            )
        uv_index = summary.get("uv_index")
        if uv_index:
            lines.extend(
                [
                    f"- UV index count: {uv_index['count']}",
                    f"- UV index range: {uv_index['range']}",
                    f"- UV index sample: {uv_index['sample']}",
                ]
            )
        material_usage = summary.get("material_usage")
        if material_usage:
            lines.append("- Material usage:")
            for material_name, usage in material_usage.items():
                lines.append(
                    f"  - {material_name}: {usage['triangle_count']} tris, {usage['unique_control_point_count']} control points, {usage['non_zero_pair_count']} non-zero pairs, {usage['unique_non_zero_pair_count']} unique"
                )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract custom layer summaries from a binary Spt2Fbx FBX file.")
    parser.add_argument("fbx", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    version, nodes = read_fbx(args.fbx)
    summaries = collect_layer_summaries(nodes)
    mesh_usage = enrich_with_material_usage(nodes, summaries)
    for summary in summaries:
        summary.pop("_raw_uv_values", None)
    payload = {
        "file": str(args.fbx),
        "fbx_version": version,
        "mesh_usage": mesh_usage,
        "layer_elements": summaries,
    }

    print(json.dumps(payload, indent=2))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2) + "\n")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(build_markdown(args.fbx, version, summaries))


if __name__ == "__main__":
    main()