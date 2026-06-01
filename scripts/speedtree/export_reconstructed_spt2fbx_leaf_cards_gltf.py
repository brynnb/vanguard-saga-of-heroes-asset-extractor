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
    uvs = []
    colors = []
    indices = []

    for card in card_data["cards"]:
        vertex_records = card["vertex_records"]
        if len(vertex_records) != 4:
            continue
        base_index = len(positions)
        dimming = float(card["dimming"])
        for record in vertex_records:
            positions.extend(record["position_gltf"])
            uvs.extend(record["diffuse_uv"])
            colors.extend([dimming, dimming, dimming, 1.0])
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
    uv_bytes = b"".join(struct.pack("<f", value) for value in uvs)
    color_bytes = b"".join(struct.pack("<f", value) for value in colors)
    idx_bytes = b"".join(struct.pack("<H", value) for value in indices)

    pos_start, pos_end = aligned_extend(buffer, pos_bytes)
    uv_start, uv_end = aligned_extend(buffer, uv_bytes)
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
            {"buffer": 0, "byteOffset": uv_start, "byteLength": uv_end - uv_start, "target": 34962},
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
            {"bufferView": 1, "componentType": 5126, "count": len(uvs) // 2, "type": "VEC2"},
            {"bufferView": 2, "componentType": 5126, "count": len(colors) // 4, "type": "VEC4"},
            {"bufferView": 3, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
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
                        "attributes": {"POSITION": 0, "TEXCOORD_0": 1, "COLOR_0": 2},
                        "indices": 3,
                        "material": 0,
                        "mode": 4,
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