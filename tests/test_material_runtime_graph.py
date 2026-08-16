import tempfile
import unittest
from pathlib import Path

from scripts.extractors.staticmesh_pipeline import _tangent_handedness
from scripts.lib.material_memory import (
    MaterialMemoryResolver,
    _runtime_material_node_size,
)


ELVEN_SPIRE_SHADER = (
    "RA5000_P0001_C0001_RockStone_Shaders.Shaders."
    "Ra5000_p1_c1_rockstone_elven_spires001"
)


class RuntimeMaterialGraphTests(unittest.TestCase):
    def test_material_size_matches_warfare_wrapper_and_combiner_rules(self):
        texture_a = {"type": "texture", "asset": {"width": 256, "height": 512}}
        texture_b = {"type": "texture", "asset": {"width": 1024, "height": 128}}
        graph = {
            "type": "tex_scaler",
            "material": {
                "type": "combiner",
                "material1": texture_a,
                "material2": texture_b,
            },
        }
        self.assertEqual(_runtime_material_node_size(graph), (1024, 512))

    def test_authored_tangent_basis_controls_gltf_handedness(self):
        self.assertEqual(
            _tangent_handedness((0, 0, 1), (1, 0, 0), (0, 1, 0)),
            -1.0,
        )
        self.assertEqual(
            _tangent_handedness((0, 0, 1), (1, 0, 0), (0, -1, 0)),
            1.0,
        )

    def test_real_elven_spire_preserves_combiner_scalers_and_bump(self):
        resolver = MaterialMemoryResolver()
        if not resolver.available or resolver.resolve_shader(ELVEN_SPIRE_SHADER) is None:
            self.skipTest("Vanguard MaterialMemory/client packages are unavailable")
        with tempfile.TemporaryDirectory() as directory:
            graph = resolver.build_runtime_material_graph(
                ELVEN_SPIRE_SHADER, Path(directory)
            )
        self.assertIsNotNone(graph)
        self.assertEqual(graph["detail_scale"], 8.0)
        diffuse = graph["roots"]["diffuse"]
        self.assertEqual(diffuse["type"], "combiner")
        self.assertEqual(diffuse["color_operation"], 3)
        self.assertEqual(diffuse["material1"]["uv_scale"], [0.1, 0.1])
        masked = diffuse["material2"]
        self.assertEqual(masked["color_operation"], 5)
        self.assertEqual(masked["material2"]["uv_scale"], [0.5, 0.5])
        self.assertEqual(masked["mask"]["uv_scale"], [0.25, 0.25])
        self.assertEqual(masked["mask"]["material_size"], [256, 256])
        normal = graph["roots"]["normal"]
        self.assertEqual(normal["color_operation"], 2)
        bump = normal["material1"]["material"]
        self.assertEqual(bump["type"], "bump")
        self.assertEqual(bump["bump_scale"], 80.0)
        self.assertEqual(bump["height"]["asset"]["width"], 512)


if __name__ == "__main__":
    unittest.main()
