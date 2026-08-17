#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import struct
from pathlib import Path


def aligned_extend(buffer: bytearray, payload: bytes) -> tuple[int, int]:
    while len(buffer) % 4 != 0:
        buffer.append(0)
    start = len(buffer)
    buffer.extend(payload)
    end = len(buffer)
    return start, end


def build_gltf(card_data: dict) -> dict:
    positions = []
    normals = []
    uvs = []
    billboard_offsets = []
    colors = []
    indices = []

    for card in card_data["cards"]:
        vertex_records = card["vertex_records"]
        if len(vertex_records) != 4:
            continue
        base_index = len(positions) // 3
        dimming = float(card["dimming"])
        center = card.get("avg_position_gltf")
        if center is None:
            center = [
                sum(float(record["position_gltf"][axis]) for record in vertex_records) / 4.0
                for axis in range(3)
            ]
        size_values = card.get("size_xy_values", [])
        if len(size_values) != 1 or len(size_values[0]) != 2:
            raise ValueError(
                f"Recovered leaf card {card.get('card_id')} has no unique runtime size"
            )
        width, height = (float(value) for value in size_values[0])
        uv_min_x = min(float(record["diffuse_uv"][0]) for record in vertex_records)
        uv_max_x = max(float(record["diffuse_uv"][0]) for record in vertex_records)
        uv_min_y = min(float(record["diffuse_uv"][1]) for record in vertex_records)
        uv_max_y = max(float(record["diffuse_uv"][1]) for record in vertex_records)
        uv_width = uv_max_x - uv_min_x
        uv_height = uv_max_y - uv_min_y
        if uv_width <= 1.0e-8 or uv_height <= 1.0e-8:
            raise ValueError(f"Recovered leaf card {card.get('card_id')} has degenerate UVs")
        for record in vertex_records:
            positions.extend(center)
            diffuse_uv = record["diffuse_uv"]
            uvs.extend(diffuse_uv)
            local_u = (float(diffuse_uv[0]) - uv_min_x) / uv_width
            local_v = (float(diffuse_uv[1]) - uv_min_y) / uv_height
            billboard_offsets.extend([
                (local_u - 0.5) * width,
                (0.5 - local_v) * height,
            ])
            colors.extend([dimming, dimming, dimming, 1.0])
        # Leaf-card vertices intentionally share one center. TEXCOORD_1 carries
        # the exact SpeedTree runtime width/height offsets that the renderer
        # expands in camera space. A stable normal keeps the portable glTF
        # valid; the leaf-card shader is unshaded, as in the prior client.
        normals.extend([0.0, 0.0, 1.0] * 4)
        indices.extend([
            base_index + 0,
            base_index + 1,
            base_index + 3,
            base_index + 0,
            base_index + 3,
            base_index + 2,
        ])

    if not positions:
        raise ValueError("No reconstructed card geometry available to export")

    buffer = bytearray()
    pos_bytes = b"".join(struct.pack("<f", value) for value in positions)
    normal_bytes = b"".join(struct.pack("<f", value) for value in normals)
    uv_bytes = b"".join(struct.pack("<f", value) for value in uvs)
    billboard_offset_bytes = b"".join(
        struct.pack("<f", value) for value in billboard_offsets
    )
    color_bytes = b"".join(struct.pack("<f", value) for value in colors)
    idx_bytes = b"".join(struct.pack("<H", value) for value in indices)

    pos_start, pos_end = aligned_extend(buffer, pos_bytes)
    normal_start, normal_end = aligned_extend(buffer, normal_bytes)
    uv_start, uv_end = aligned_extend(buffer, uv_bytes)
    billboard_offset_start, billboard_offset_end = aligned_extend(
        buffer, billboard_offset_bytes
    )
    color_start, color_end = aligned_extend(buffer, color_bytes)
    idx_start, idx_end = aligned_extend(buffer, idx_bytes)

    position_triplets = list(zip(positions[0::3], positions[1::3], positions[2::3]))
    min_pos = [min(axis) for axis in zip(*position_triplets)]
    max_pos = [max(axis) for axis in zip(*position_triplets)]
    encoded_buffer = base64.b64encode(buffer).decode("ascii")

    return {
        "asset": {"version": "2.0", "generator": "export_reconstructed_spt2fbx_leaf_cards_gltf.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "ReconstructedLeafCards"}],
        "buffers": [{"byteLength": len(buffer), "uri": f"data:application/octet-stream;base64,{encoded_buffer}"}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": pos_start, "byteLength": pos_end - pos_start, "target": 34962},
            {"buffer": 0, "byteOffset": normal_start, "byteLength": normal_end - normal_start, "target": 34962},
            {"buffer": 0, "byteOffset": uv_start, "byteLength": uv_end - uv_start, "target": 34962},
            {"buffer": 0, "byteOffset": billboard_offset_start, "byteLength": billboard_offset_end - billboard_offset_start, "target": 34962},
            {"buffer": 0, "byteOffset": color_start, "byteLength": color_end - color_start, "target": 34962},
            {"buffer": 0, "byteOffset": idx_start, "byteLength": idx_end - idx_start, "target": 34963},
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(position_triplets),
                "type": "VEC3",
                "min": min_pos,
                "max": max_pos,
            },
            {"bufferView": 1, "componentType": 5126, "count": len(normals) // 3, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5126, "count": len(uvs) // 2, "type": "VEC2"},
            {"bufferView": 3, "componentType": 5126, "count": len(billboard_offsets) // 2, "type": "VEC2"},
            {"bufferView": 4, "componentType": 5126, "count": len(colors) // 4, "type": "VEC4"},
            {"bufferView": 5, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
        ],
        "materials": [
            {
                "name": "RecoveredLeafCards",
                "doubleSided": True,
                "alphaMode": "OPAQUE",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
            }
        ],
        "meshes": [
            {
                "name": "RecoveredLeafCards",
                "primitives": [
                    {
                        "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2, "TEXCOORD_1": 3, "COLOR_0": 4},
                        "indices": 5,
                        "material": 0,
                        "mode": 4,
                        "extras": {"vg_speedtree_foliage_kind": "leaf_card"},
                    }
                ],
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export reconstructed Spt2Fbx leaf cards as a standalone glTF.")
    parser.add_argument("card_json", type=Path)
    parser.add_argument("output_gltf", type=Path)
    args = parser.parse_args()

    card_data = json.loads(args.card_json.read_text())
    gltf = build_gltf(card_data)
    args.output_gltf.parent.mkdir(parents=True, exist_ok=True)
    args.output_gltf.write_text(json.dumps(gltf, indent=2) + "\n")
    print(f"Wrote {args.output_gltf}")


if __name__ == "__main__":
    main()
