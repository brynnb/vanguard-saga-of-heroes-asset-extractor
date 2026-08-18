from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/generators"))

from scripts.extractors import staticmesh_pipeline
from scripts.extractors.build_shader_texture_map import _project_shader_map_from_manifest
from scripts.generators import generate_objects_from_txt
from scripts.generators.generate_object_cell_index import is_authored_hidden_mesh
from scripts.generators.generate_godot_runtime_chunk import is_hidden_sgo_component


class StaticMeshIdentityAndVisibilityTests(unittest.TestCase):
    def test_sgo_component_prefers_outer_qualified_mesh(self) -> None:
        manifest = [
            "Tables/Ra0005_Table002.gltf",
            "Tables/__outer__/Tables/Ra0005_Table002.gltf",
            "Tables/__outer__/workbench_rustic/Ra0005_Table002.gltf",
        ]
        by_name, by_base, _by_tail, by_qualified = (
            generate_objects_from_txt.build_mesh_lookups(manifest)
        )
        previous = generate_objects_from_txt._sgo_prefabs
        generate_objects_from_txt._sgo_prefabs = {
            "Shop": {
                "actors": [
                    {
                        "mesh": "Ra0005_Table002",
                        "mesh_identity": ["Tables", "Tables"],
                    }
                ]
            }
        }
        try:
            resolved = generate_objects_from_txt.resolve_sgo_components(
                "Shop", by_name, by_base, by_qualified
            )
        finally:
            generate_objects_from_txt._sgo_prefabs = previous
        self.assertEqual(
            resolved[0]["mesh_path"],
            "Tables/__outer__/Tables/Ra0005_Table002.gltf",
        )

    def test_near_zero_authored_cull_component_is_hidden_from_runtime(self) -> None:
        self.assertTrue(is_hidden_sgo_component({"render_suppressed": True}))
        self.assertFalse(is_hidden_sgo_component({"render_suppressed": False}))

    def test_near_zero_static_mesh_metadata_is_hidden_from_cells(self) -> None:
        class Metadata:
            @staticmethod
            def lookup(_mesh_path, _mesh_name):
                return {"cull_distance": 1.0}

        self.assertTrue(is_authored_hidden_mesh("Utilities/Plain.gltf", "Plain", Metadata()))

    def test_water_classification_comes_from_material_contract(self) -> None:
        loader = staticmesh_pipeline._load_shader_texture_map
        had_cache = hasattr(loader, "_cache")
        previous = getattr(loader, "_cache", None)
        loader._cache = {"lake": {"is_water": True}}
        try:
            result = staticmesh_pipeline.classify_mesh_surfaces(
                SimpleNamespace(
                    sections=[
                        {"num_primitives": 0},
                        {"num_primitives": 2},
                    ],
                    skins=[["lake"]],
                )
            )
        finally:
            if had_cache:
                loader._cache = previous
            else:
                del loader._cache
        self.assertTrue(result["contains_water"])
        self.assertEqual(result["water_section_indices"], [1])
        self.assertEqual(result["water_shader_refs"], ["lake"])

    def test_legacy_projection_preserves_canonical_water_label(self) -> None:
        projected = _project_shader_map_from_manifest(
            {
                "Water.Shaders.Lake": {
                    "base_color": {"asset_name": "LakeColor"},
                    "is_water": True,
                    "two_sided": True,
                }
            }
        )
        self.assertTrue(projected["water.shaders.lake"]["is_water"])


if __name__ == "__main__":
    unittest.main()
