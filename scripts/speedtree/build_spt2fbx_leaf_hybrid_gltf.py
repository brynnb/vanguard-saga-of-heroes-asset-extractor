#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import struct
from pathlib import Path


COMPONENT_TYPE_TO_FORMAT = {
    5123: "H",
    5125: "I",
    5126: "f",
}

TYPE_COMPONENTS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
}


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
    component_count = TYPE_COMPONENTS[accessor["type"]]
    stride = buffer_view.get("byteStride") or component_count * struct.calcsize(COMPONENT_TYPE_TO_FORMAT[accessor["componentType"]])
    fmt = "<" + COMPONENT_TYPE_TO_FORMAT[accessor["componentType"]] * component_count
    values = []
    for index in range(accessor["count"]):
        value = struct.unpack_from(fmt, blob, start + index * stride)
        values.append(value if component_count > 1 else value[0])
    return values


def aligned_append(buffer: bytearray, payload: bytes) -> tuple[int, int]:
    while len(buffer) % 4 != 0:
        buffer.append(0)
    start = len(buffer)
    buffer.extend(payload)
    end = len(buffer)
    return start, end


def pack_floats(values) -> bytes:
    flat = []
    for value in values:
        if isinstance(value, tuple) or isinstance(value, list):
            flat.extend(value)
        else:
            flat.append(value)
    return b"".join(struct.pack("<f", float(item)) for item in flat)


def pack_uint16(values) -> bytes:
    return b"".join(struct.pack("<H", int(item)) for item in values)


def build_hybrid(our_gltf_path: Path, leaf_gltf_path: Path) -> dict:
    our_data, our_blob = load_gltf(our_gltf_path)
    leaf_data, leaf_blob = load_gltf(leaf_gltf_path)

    our_mesh = our_data["meshes"][0]
    our_materials = our_data["materials"]
    foliage_material_index = next(
        primitive["material"]
        for primitive in our_mesh["primitives"]
        if "_BILLBOARD" in primitive["attributes"]
    )

    surviving_primitives = [primitive for primitive in our_mesh["primitives"] if "_BILLBOARD" not in primitive["attributes"]]

    leaf_primitive = leaf_data["meshes"][0]["primitives"][0]
    leaf_positions = read_accessor(leaf_data, leaf_blob, leaf_primitive["attributes"]["POSITION"])
    leaf_uvs = read_accessor(leaf_data, leaf_blob, leaf_primitive["attributes"]["TEXCOORD_0"])
    leaf_colors = read_accessor(leaf_data, leaf_blob, leaf_primitive["attributes"]["COLOR_0"])
    leaf_indices = read_accessor(leaf_data, leaf_blob, leaf_primitive["indices"])

    buffer = bytearray()
    buffer_views = []
    accessors = []

    def append_accessor(values, component_type, gltf_type, target=None, minmax=None):
        if component_type == 5126:
            payload = pack_floats(values)
        elif component_type == 5123:
            payload = pack_uint16(values)
        else:
            raise ValueError(f"Unsupported component type {component_type}")
        start, end = aligned_append(buffer, payload)
        buffer_view_index = len(buffer_views)
        view = {"buffer": 0, "byteOffset": start, "byteLength": end - start}
        if target is not None:
            view["target"] = target
        buffer_views.append(view)
        accessor = {
            "bufferView": buffer_view_index,
            "componentType": component_type,
            "count": len(values),
            "type": gltf_type,
        }
        if minmax:
            accessor.update(minmax)
        accessors.append(accessor)
        return len(accessors) - 1

    new_primitives = []
    for primitive in surviving_primitives:
        copied_attrs = {}
        for attr_name, accessor_index in primitive["attributes"].items():
            values = read_accessor(our_data, our_blob, accessor_index)
            accessor = our_data["accessors"][accessor_index]
            minmax = {}
            if "min" in accessor:
                minmax["min"] = accessor["min"]
            if "max" in accessor:
                minmax["max"] = accessor["max"]
            copied_attrs[attr_name] = append_accessor(values, accessor["componentType"], accessor["type"], target=34962, minmax=minmax or None)

        copied_indices = None
        if "indices" in primitive:
            accessor = our_data["accessors"][primitive["indices"]]
            values = read_accessor(our_data, our_blob, primitive["indices"])
            copied_indices = append_accessor(values, accessor["componentType"], accessor["type"], target=34963)

        new_primitive = {
            "attributes": copied_attrs,
            "material": primitive["material"],
            "mode": primitive.get("mode", 4),
        }
        if copied_indices is not None:
            new_primitive["indices"] = copied_indices
        new_primitives.append(new_primitive)

    pos_min = [min(axis) for axis in zip(*leaf_positions)]
    pos_max = [max(axis) for axis in zip(*leaf_positions)]
    pos_accessor = append_accessor(leaf_positions, 5126, "VEC3", target=34962, minmax={"min": pos_min, "max": pos_max})
    uv_accessor = append_accessor(leaf_uvs, 5126, "VEC2", target=34962)
    color_accessor = append_accessor(leaf_colors, 5126, "VEC4", target=34962)
    idx_accessor = append_accessor(leaf_indices, 5123, "SCALAR", target=34963)
    new_primitives.append(
        {
            "attributes": {
                "POSITION": pos_accessor,
                "TEXCOORD_0": uv_accessor,
                "COLOR_0": color_accessor,
            },
            "indices": idx_accessor,
            "material": foliage_material_index,
            "mode": 4,
        }
    )

    encoded = base64.b64encode(buffer).decode("ascii")
    hybrid = {
        "asset": {"version": "2.0", "generator": "build_spt2fbx_leaf_hybrid_gltf.py"},
        "scene": our_data.get("scene", 0),
        "scenes": our_data.get("scenes", [{"nodes": [0]}]),
        "nodes": our_data.get("nodes", [{"mesh": 0}]),
        "materials": our_materials,
        "meshes": [{"name": our_mesh.get("name", "HybridTree"), "primitives": new_primitives}],
        "buffers": [{"byteLength": len(buffer), "uri": f"data:application/octet-stream;base64,{encoded}"}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    for key in ("samplers", "images", "textures", "extensionsUsed", "extensionsRequired"):
        if key in our_data:
            hybrid[key] = our_data[key]
    return hybrid


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a hybrid glTF by replacing our billboard leaf primitive with recovered runtime leaf cards.")
    parser.add_argument("--ours", type=Path, required=True)
    parser.add_argument("--runtime-leaf-gltf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    hybrid = build_hybrid(args.ours, args.runtime_leaf_gltf)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(hybrid, indent=2) + "\n")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()