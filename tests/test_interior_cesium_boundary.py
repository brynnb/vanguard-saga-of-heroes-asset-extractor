import json
from pathlib import Path
import tempfile
import unittest

from scripts.generators.generate_godot_runtime_interior_assets import (
    build_interior_cesium_boundary,
)
from scripts.lib.interior_portal_runtime import build_portal_runtime_catalog


def visual(path: str, name: str) -> dict:
    return {
        "class": "StaticMeshActor",
        "name": name,
        "source_component_path": path,
        "static_mesh_source": {
            "name": name,
            "source_package": "FixturePackage",
        },
        "transform": {
            "basis": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "origin": [10.0, 20.0, 30.0],
        },
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
                "portal_graph": {
                    "adjacency": [
                        {"room_id": "room_fixture", "visible_room_ids": []}
                    ],
                    "boundaries": [
                        {
                            "boundary_id": "boundary_fixture",
                            "endpoint_id": "endpoint_fixture",
                            "room_id": "room_fixture",
                        }
                    ],
                    "connections": [],
                    "endpoints": [
                        {
                            "aperture_status": "exact",
                            "endpoint_id": "endpoint_fixture",
                            "room_id": "room_fixture",
                            "aperture_geometry": {
                                "primitives": [
                                    {
                                        "indices": [0, 1, 2],
                                        "positions": [
                                            [0.0, 0.0, 0.0],
                                            [0.0, 1.0, 0.0],
                                            [0.0, 0.0, 1.0],
                                        ],
                                    }
                                ]
                            },
                        }
                    ],
                    "unresolved": [],
                },
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
                "chunk_global_origin": [1000.0, 0.0, 2000.0],
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

    def test_portal_runtime_catalog_publishes_bounds_aperture_and_instance_mapping(self):
        source = fixture_source()
        boundary, packs = build_interior_cesium_boundary(
            source,
            fixture_entries(),
            "selection_fixture",
            source_publication_sha256="a" * 64,
            require_ready=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            mesh_root = Path(temporary)
            mesh_path = mesh_root / "FixturePackage/Wall.gltf"
            mesh_path.parent.mkdir(parents=True)
            mesh_path.write_text(
                json.dumps(
                    {
                        "accessors": [
                            {
                                "max": [2.0, 4.0, 6.0],
                                "min": [0.0, 0.0, 0.0],
                            }
                        ],
                        "meshes": [
                            {"primitives": [{"attributes": {"POSITION": 0}}]}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            catalog = build_portal_runtime_catalog(source, boundary, packs, mesh_root)

        self.assertEqual(
            catalog["counts"],
            {
                "template_count": 1,
                "instance_count": 1,
                "room_count": 1,
                "endpoint_count": 1,
                "connection_count": 0,
                "exterior_boundary_count": 1,
            },
        )
        room = catalog["templates"][0]["rooms"][0]
        self.assertEqual(room[1], [-20.0, 0.0, 0.0])
        self.assertEqual(room[2], [0.0, 34.0, 16.0])
        endpoint = catalog["templates"][0]["endpoints"][0]
        self.assertEqual(endpoint[5], [[-0.0, 0.0, 0.0], [-1.0, 0.0, 0.0], [-0.0, 1.0, 0.0]])
        self.assertEqual(endpoint[6], [0, 1, 2])
        boundary_record = catalog["templates"][0]["boundaries"][0]
        self.assertEqual(boundary_record[1:], [0, 0])
        self.assertEqual(catalog["string_table"][boundary_record[0]], "boundary_fixture")
        template = catalog["templates"][0]
        pack_path = catalog["string_table"][template["room_pack_relative_path_string"]]
        self.assertEqual(pack_path, f"interior_room_packs.v1/{packs[0]['room_pack_id']}.json")
        instance = catalog["instances"][0]
        self.assertEqual(instance[5], [1000.0, 0.0, 2000.0])
        self.assertEqual(instance[7], [1010.0, 20.0, 2030.0])

    def test_portal_connection_uses_template_local_room_and_endpoint_indices(self):
        source = fixture_source()
        template = source["interior_templates"][0]
        second_path = "sgo://Fixture/RoomSecond/Wall"
        template["rooms"].append(
            {
                "room_id": "room_second",
                "source_component_path": "sgo://Fixture/RoomSecond",
                "transform": {
                    "basis": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                    "origin": [0.0, 0.0, 0.0],
                },
                "visual_components": [visual(second_path, "WallSecond")],
            }
        )
        graph = template["portal_graph"]
        graph["adjacency"] = [
            {"room_id": "room_fixture", "visible_room_ids": ["room_second"]},
            {"room_id": "room_second", "visible_room_ids": ["room_fixture"]},
        ]
        graph["boundaries"] = []
        second_endpoint = dict(graph["endpoints"][0])
        second_endpoint["endpoint_id"] = "endpoint_second"
        second_endpoint["room_id"] = "room_second"
        graph["endpoints"].append(second_endpoint)
        graph["connections"] = [
            {
                "connection_id": "connection_fixture",
                "endpoint_ids": ["endpoint_fixture", "endpoint_second"],
                "room_ids": ["room_fixture", "room_second"],
            }
        ]
        source["instances"][0]["room_visual_bindings"].append(
            {
                "available_visual_components": [
                    {
                        "asset_id": "source-wall-second",
                        "mesh_path": "FixturePackage/WallSecond.gltf",
                        "source_component_path": second_path,
                    }
                ],
                "available_visual_component_paths": [second_path],
                "missing_visual_component_paths": [],
                "room_id": "room_second",
            }
        )
        entries = fixture_entries()
        entries["FixturePackage/WallSecond.gltf"] = {
            "asset_id": "shared_asset_wall_second",
            "runtime_relative_path": "assets/WallSecond.glb",
            "status": "existing",
        }
        boundary, packs = build_interior_cesium_boundary(
            source,
            entries,
            "selection_fixture",
            source_publication_sha256="a" * 64,
            require_ready=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            mesh_root = Path(temporary)
            for name in ("Wall", "WallSecond"):
                mesh_path = mesh_root / f"FixturePackage/{name}.gltf"
                mesh_path.parent.mkdir(parents=True, exist_ok=True)
                mesh_path.write_text(
                    json.dumps(
                        {
                            "accessors": [{"max": [1.0, 1.0, 1.0], "min": [0.0, 0.0, 0.0]}],
                            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
                        }
                    ),
                    encoding="utf-8",
                )
            catalog = build_portal_runtime_catalog(source, boundary, packs, mesh_root)

        connection = catalog["templates"][0]["connections"][0]
        self.assertEqual(connection[1], [0, 1])
        self.assertEqual(connection[2], [0, 1])
        self.assertEqual(catalog["templates"][0]["rooms"][0][4], [1])
        self.assertEqual(catalog["templates"][0]["rooms"][1][4], [0])


if __name__ == "__main__":
    unittest.main()
