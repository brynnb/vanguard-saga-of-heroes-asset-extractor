import unittest
import base64
import struct
from types import SimpleNamespace

from scripts.lib.speedtree_staticmesh import (
    collapsed_leaf_section,
    discard_degenerate_triangles,
    has_embedded_speedtree_payload,
    tangent_stream_is_usable,
)
from scripts.speedtree.export_reconstructed_spt2fbx_leaf_cards_gltf import build_gltf


def vertex(x, y, z):
    return SimpleNamespace(x=x, y=y, z=z)


class SpeedTreeStaticMeshTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
