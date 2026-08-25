import unittest

from scripts.generators.generate_godot_runtime_interior_assets import (
    build_interior_cesium_boundary,
)


def visual(path: str, name: str) -> dict:
    return {
        "class": "StaticMeshActor",
        "name": name,
        "source_component_path": path,
        "static_mesh_source": {
            "name": name,
            "source_package": "FixturePackage",
        },
        "transform": {"origin": [1.0, 2.0, 3.0]},
    }


def fixture_source(*, instance_eligible: bool = True) -> dict:
    component_path = "sgo://Fixture/Room/Wall"
    reason = "eligible" if instance_eligible else "unavailable_room_visual_binding"
    return {
        "publication_id": "interior_source_publication_fixture",
        "interior_templates": [
            {
                "interior_space_asset_id": "interior_fixture",
                "root_prefab": "Fixture",
                "runtime_eligibility": {"eligible": True},
                "rooms": [
                    {
                        "room_id": "room_fixture",
                        "source_component_path": "sgo://Fixture/Room",
                        "transform": {"origin": [0.0, 0.0, 0.0]},
                        "visual_components": [visual(component_path, "Wall")],
                    }
                ],
            }
        ],
        "instances": [
            {
                "authoritative_source_node_id": "source-node",
                "authoritative_source_object_id": "source-object",
                "chunk": "chunk_1_2",
                "interior_instance_id": "instance_fixture",
                "interior_space_asset_id": "interior_fixture",
                "node_index": 12,
                "root_transform": {"translation": [10.0, 20.0, 30.0]},
                "room_visual_bindings": [
                    {
                        "available_visual_components": [
                            {
                                "asset_id": "source-wall",
                                "mesh_path": "FixturePackage/Wall.gltf",
                                "source_component_path": component_path,
                            }
                        ],
                        "available_visual_component_paths": [component_path],
                        "missing_visual_component_paths": [],
                        "room_id": "room_fixture",
                    }
                ],
                "runtime_eligibility": {"eligible": instance_eligible, "reason": reason},
            }
        ],
    }


def fixture_entries(*, ready: bool = True) -> dict:
    return {
        "FixturePackage/Wall.gltf": {
            "asset_id": "shared_asset_wall",
            "runtime_relative_path": "assets/Wall.glb",
            "status": "existing" if ready else "planned",
        }
    }


class InteriorCesiumBoundaryTest(unittest.TestCase):
    def test_exact_replacement_publishes_one_exclusion_and_room_pack(self):
        boundary, packs = build_interior_cesium_boundary(
            fixture_source(),
            fixture_entries(),
            "selection_fixture",
            source_publication_sha256="a" * 64,
            require_ready=True,
        )

        self.assertEqual(boundary["counts"]["eligible_instance_count"], 1)
        self.assertEqual(boundary["counts"]["excluded_placement_count"], 1)
        self.assertEqual(boundary["counts"]["fallback_placement_count"], 0)
        self.assertEqual(
            boundary["exclusion_record_format"],
            ["eligible_instance_index", "pack_component_index"],
        )
        self.assertEqual(boundary["exclusion_records"], [[0, 0]])
        self.assertEqual(
            packs[0]["rooms"][0]["visual_components"][0]["asset_id"],
            "shared_asset_wall",
        )
        self.assertEqual(len(packs), 1)
        self.assertEqual(
            packs[0]["rooms"][0]["visual_components"][0]["runtime_relative_path"],
            "assets/Wall.glb",
        )

    def test_ineligible_instance_remains_an_explicit_cesium_fallback(self):
        boundary, packs = build_interior_cesium_boundary(
            fixture_source(instance_eligible=False),
            fixture_entries(),
            "selection_fixture",
            source_publication_sha256="a" * 64,
            require_ready=True,
        )

        self.assertEqual(boundary["counts"]["eligible_instance_count"], 0)
        self.assertEqual(boundary["counts"]["excluded_placement_count"], 0)
        self.assertEqual(boundary["counts"]["fallback_instance_count"], 1)
        self.assertEqual(boundary["counts"]["fallback_placement_count"], 1)
        record = dict(
            zip(
                boundary["fallback_placement_record_format"],
                boundary["fallback_placement_records"][0],
                strict=True,
            )
        )
        self.assertEqual(
            boundary["string_table"][record["reason"]],
            "unavailable_room_visual_binding",
        )
        self.assertEqual(len(packs), 0)

    def test_ready_replacement_is_required_before_exclusion_publication(self):
        with self.assertRaisesRegex(ValueError, "replacement asset is not ready"):
            build_interior_cesium_boundary(
                fixture_source(),
                fixture_entries(ready=False),
                "selection_fixture",
                source_publication_sha256="a" * 64,
                require_ready=True,
            )

    def test_missing_instance_binding_fails_one_to_one_coverage(self):
        source = fixture_source()
        source["instances"][0]["room_visual_bindings"][0][
            "available_visual_component_paths"
        ] = []
        with self.assertRaisesRegex(ValueError, "coverage is not one-to-one"):
            build_interior_cesium_boundary(
                source,
                fixture_entries(),
                "selection_fixture",
                source_publication_sha256="a" * 64,
                require_ready=True,
            )


if __name__ == "__main__":
    unittest.main()
