import unittest
import base64
import json
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace

from scripts.lib.speedtree_staticmesh import (
    collapsed_leaf_section,
    discard_degenerate_triangles,
    has_embedded_speedtree_payload,
    tangent_stream_is_usable,
)
from scripts.speedtree.export_reconstructed_spt2fbx_leaf_cards_gltf import build_gltf
from scripts.speedtree.build_spt2fbx_leaf_hybrid_gltf import (
    build_hybrid,
    load_gltf,
    read_accessor,
)
from scripts.extractors.staticmesh_pipeline import (
    _effective_surface_two_sided,
    _remove_speedtree_shadow_detail,
)


def vertex(x, y, z):
    return SimpleNamespace(x=x, y=y, z=z)


class SpeedTreeStaticMeshTests(unittest.TestCase):
    def test_all_speedtree_surfaces_are_two_sided(self) -> None:
        self.assertTrue(_effective_surface_two_sided(True, False))
        self.assertTrue(_effective_surface_two_sided(True, True))
        self.assertTrue(_effective_surface_two_sided(False, True))
        self.assertFalse(_effective_surface_two_sided(False, False))

    def test_detects_payload_by_format_markers_not_filename(self) -> None:
        self.assertTrue(
            has_embedded_speedtree_payload(b"prefix__IdvSpt_02_suffixSpeedTree")
        )
        self.assertFalse(has_embedded_speedtree_payload(b"PrettySpeedTreeName"))

    def test_rejects_speedtree_uninitialized_tangents(self) -> None:
        normals = [(0.0, 0.0, 1.0)] * 2
        self.assertFalse(
            tangent_stream_is_usable(
                [(-107374176.0, -107374176.0, -107374176.0)] * 2,
                normals,
            )
        )
        self.assertTrue(
            tangent_stream_is_usable([(1.0, 0.0, 0.0)] * 2, normals)
        )

    def test_collapsed_leaf_classification_and_cleanup(self) -> None:
        vertices = [
            vertex(0, 0, 0),
            vertex(0, 0, 0),
            vertex(0, 0, 0),
            vertex(1, 0, 0),
            vertex(0, 1, 0),
        ]
        collapsed = [0, 1, 2, 0, 2, 1]
        self.assertTrue(collapsed_leaf_section(vertices, collapsed))
        mixed = [0, 1, 2, 0, 3, 4]
        self.assertEqual(discard_degenerate_triangles(vertices, mixed), [0, 3, 4])

    def test_runtime_cards_export_explicit_normals_and_triangles(self) -> None:
        payload = {
            "cards": [
                {
                    "card_id": 1,
                    "dimming": 0.75,
                    "size_xy_values": [[2.0, 4.0]],
                    "avg_position_gltf": [0.5, 0.5, 0.0],
                    "vertex_records": [
                        {"position_gltf": [0, 0, 0], "diffuse_uv": [0, 0]},
                        {"position_gltf": [0, 1, 0], "diffuse_uv": [0, 1]},
                        {"position_gltf": [1, 0, 0], "diffuse_uv": [1, 0]},
                        {"position_gltf": [1, 1, 0], "diffuse_uv": [1, 1]},
                    ],
                },
                {
                    "card_id": 2,
                    "dimming": 1.0,
                    "size_xy_values": [[2.0, 4.0]],
                    "avg_position_gltf": [2.5, 0.5, 0.0],
                    "vertex_records": [
                        {"position_gltf": [2, 0, 0], "diffuse_uv": [0, 0]},
                        {"position_gltf": [2, 1, 0], "diffuse_uv": [0, 1]},
                        {"position_gltf": [3, 0, 0], "diffuse_uv": [1, 0]},
                        {"position_gltf": [3, 1, 0], "diffuse_uv": [1, 1]},
                    ],
                },
            ]
        }
        gltf = build_gltf(payload)
        primitive = gltf["meshes"][0]["primitives"][0]
        self.assertIn("NORMAL", primitive["attributes"])
        self.assertIn("TEXCOORD_1", primitive["attributes"])
        self.assertEqual(
            primitive["extras"]["vg_speedtree_foliage_kind"], "leaf_card"
        )
        index_accessor = gltf["accessors"][primitive["indices"]]
        self.assertEqual(index_accessor["count"], 12)
        index_view = gltf["bufferViews"][index_accessor["bufferView"]]
        blob = base64.b64decode(gltf["buffers"][0]["uri"].split(",", 1)[1])
        indices = struct.unpack_from(
            "<" + "H" * index_accessor["count"],
            blob,
            index_view["byteOffset"],
        )
        self.assertEqual(max(indices), 7)
        self.assertTrue(gltf["materials"][0]["doubleSided"])

        position_accessor = gltf["accessors"][primitive["attributes"]["POSITION"]]
        position_view = gltf["bufferViews"][position_accessor["bufferView"]]
        positions = struct.unpack_from(
            "<" + "f" * position_accessor["count"] * 3,
            blob,
            position_view["byteOffset"],
        )
        self.assertEqual(positions[:12], (0.5, 0.5, 0.0) * 4)
        offset_accessor = gltf["accessors"][primitive["attributes"]["TEXCOORD_1"]]
        offset_view = gltf["bufferViews"][offset_accessor["bufferView"]]
        offsets = struct.unpack_from(
            "<" + "f" * offset_accessor["count"] * 2,
            blob,
            offset_view["byteOffset"],
        )
        self.assertEqual(offsets[:8], (-1.0, 2.0, -1.0, -2.0, 1.0, 2.0, 1.0, -2.0))

    def test_hybrid_scales_runtime_leaf_cards_to_staticmesh_canopy(self) -> None:
        source_positions = [
            (0.0, 100.0, 0.0),
            (0.0, 500.0, 0.0),
            (0.0, 300.0, 0.0),
        ]
        source_indices = [0, 1, 2]
        source_blob = b"".join(
            struct.pack("<3f", *position) for position in source_positions
        ) + b"".join(struct.pack("<H", index) for index in source_indices)
        source_gltf = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "buffers": [{
                "byteLength": len(source_blob),
                "uri": "data:application/octet-stream;base64,"
                + base64.b64encode(source_blob).decode("ascii"),
            }],
            "bufferViews": [
                {"buffer": 0, "byteOffset": 0, "byteLength": 36},
                {"buffer": 0, "byteOffset": 36, "byteLength": 6},
            ],
            "accessors": [
                {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"},
                {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"},
            ],
            "materials": [{"name": "foliage"}],
            "meshes": [{"primitives": [{
                "attributes": {"POSITION": 0, "_BILLBOARD": 0},
                "indices": 1,
                "material": 0,
                "extras": {"vg_speedtree_collapsed_leaves": True},
            }]}],
        }
        runtime_gltf = build_gltf({
            "cards": [
                {
                    "card_id": 1,
                    "dimming": 1.0,
                    "size_xy_values": [[2.0, 4.0]],
                    "avg_position_gltf": [0.0, 1.0, 0.0],
                    "vertex_records": [
                        {"diffuse_uv": [0, 0]}, {"diffuse_uv": [0, 1]},
                        {"diffuse_uv": [1, 0]}, {"diffuse_uv": [1, 1]},
                    ],
                },
                {
                    "card_id": 2,
                    "dimming": 1.0,
                    "size_xy_values": [[2.0, 4.0]],
                    "avg_position_gltf": [0.0, 5.0, 0.0],
                    "vertex_records": [
                        {"diffuse_uv": [0, 0]}, {"diffuse_uv": [0, 1]},
                        {"diffuse_uv": [1, 0]}, {"diffuse_uv": [1, 1]},
                    ],
                },
            ]
        })

        with tempfile.TemporaryDirectory(dir="/var/tmp") as temp_dir:
            source_path = Path(temp_dir) / "source.gltf"
            runtime_path = Path(temp_dir) / "runtime.gltf"
            source_path.write_text(json.dumps(source_gltf))
            runtime_path.write_text(json.dumps(runtime_gltf))
            hybrid = build_hybrid(source_path, runtime_path)
            hybrid_path = Path(temp_dir) / "hybrid.gltf"
            hybrid_path.write_text(json.dumps(hybrid))
            hybrid_data, hybrid_blob = load_gltf(hybrid_path)

        primitive = hybrid_data["meshes"][0]["primitives"][-1]
        positions = read_accessor(
            hybrid_data, hybrid_blob, primitive["attributes"]["POSITION"]
        )
        offsets = read_accessor(
            hybrid_data, hybrid_blob, primitive["attributes"]["TEXCOORD_1"]
        )
        self.assertEqual(min(position[1] for position in positions), 100.0)
        self.assertEqual(max(position[1] for position in positions), 500.0)
        self.assertEqual(max(abs(offset[0]) for offset in offsets), 100.0)
        material = hybrid_data["materials"][primitive["material"]]
        self.assertEqual(
            material["extras"]["vg_speedtree_runtime_to_static_scale"], 100.0
        )

    def test_speedtree_shadow_is_not_published_as_tiled_detail(self) -> None:
        shadow_asset = {"asset_name": "ElvenTreeRT003_shadow01"}
        extras = {
            "vg_detail_texture_asset": shadow_asset,
            "vg_runtime_material_graph": {
                "detail_scale": 8.0,
                "roots": {
                    "diffuse": {"type": "texture"},
                    "detail": {"type": "texture", "asset": shadow_asset},
                },
            },
        }
        self.assertTrue(_remove_speedtree_shadow_detail(extras, shadow_asset))
        self.assertNotIn("vg_detail_texture_asset", extras)
        self.assertNotIn("detail_scale", extras["vg_runtime_material_graph"])
        self.assertNotIn("detail", extras["vg_runtime_material_graph"]["roots"])
        self.assertEqual(extras["vg_speedtree_shadow_texture_asset"], shadow_asset)


if __name__ == "__main__":
    unittest.main()
