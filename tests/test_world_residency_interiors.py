import json
from pathlib import Path
import tempfile
import unittest

from scripts.generators.generate_world_residency_interiors import (
    instance_runtime_eligibility,
    load_source_terrain_inventory,
    reject_volatile_output,
)
from scripts.lib.world_residency_interiors import (
    AffineTransform,
    assemble_portal_graph,
    discover_rooms,
    nested_source_path,
    prefab_name_from_raw_record,
    root_has_room,
    source_component_path,
    walk_room_actors,
)


def transform(x=0.0, y=0.0, z=0.0):
    return AffineTransform.from_properties({"Location": [x, y, z]})


def compound(prefab, number, child, compound_type, x=0.0, y=0.0, z=0.0):
    value = transform(x, y, z)
    return {
        "compound_type": compound_type,
        "source_component_path": source_component_path(
            prefab, "CompoundObject", f"CompoundObject{number}"
        ),
        "sub_prefab": child,
        "transform": value.serialize(),
        "_transform": value,
    }


def actor(prefab, number, class_name="Portal", x=0.0, y=0.0, z=0.0):
    value = transform(x, y, z)
    return {
        "class": class_name,
        "name": f"{class_name}{number}",
        "source_component_path": source_component_path(
            prefab, class_name, f"{class_name}{number}"
        ),
        "transform": value.serialize(),
        "_transform": value,
    }


class WorldResidencyInteriorsTest(unittest.TestCase):
    def fixture(self):
        return {
            "Root": {
                "compound_refs": [compound("Root", 0, "Module", 5, 100, 0, 0)],
                "actors": [],
            },
            "Module": {
                "compound_refs": [
                    compound("Module", 0, "RoomA", 3, 0, 0, 0),
                    compound("Module", 1, "RoomB", 3, 20, 0, 0),
                ],
                "actors": [],
            },
            "RoomA": {
                "compound_refs": [],
                "actors": [
                    actor("RoomA", 0, "Portal", 10, 0, 0),
                    actor("RoomA", 1, "Portal", -10, 0, 0),
                    actor("RoomA", 2, "StaticMeshActor", 0, 0, 0),
                ],
            },
            "RoomB": {
                "compound_refs": [],
                "actors": [
                    actor("RoomB", 0, "Portal", -10, 0, 0),
                    actor("RoomB", 1, "Portal", 10, 0, 0),
                ],
            },
        }

    def test_prefab_name_comes_from_exact_trailer_suffix(self):
        self.assertEqual(
            prefab_name_from_raw_record(
                {
                    "index": 7,
                    "trailer_entry": {"name": "Fixture_Room_binaryprefab.ubc"},
                }
            ),
            "Fixture_Room",
        )
        with self.assertRaisesRegex(ValueError, "unsupported trailer"):
            prefab_name_from_raw_record(
                {"index": 8, "trailer_entry": {"name": "Fixture_Room.ubc"}}
            )

    def test_room_paths_and_transforms_preserve_compound_hierarchy(self):
        prefabs = self.fixture()
        self.assertTrue(root_has_room("root", prefabs))
        rooms = discover_rooms("Root", prefabs)

        self.assertEqual(len(rooms), 2)
        self.assertEqual(rooms[0]["transform"]["origin"], [100.0, 0.0, 0.0])
        self.assertEqual(rooms[1]["transform"]["origin"], [120.0, 0.0, 0.0])
        self.assertEqual(
            rooms[0]["source_component_path"],
            nested_source_path(
                source_component_path("Root", "CompoundObject", "CompoundObject0"),
                source_component_path("Module", "CompoundObject", "CompoundObject0"),
            ),
        )

        actors = list(walk_room_actors(rooms[0], prefabs))
        self.assertEqual(len(actors), 3)
        self.assertEqual(
            [value["transform"]["origin"] for value in actors],
            [[110.0, 0.0, 0.0], [90.0, 0.0, 0.0], [100.0, 0.0, 0.0]],
        )

    def test_only_unique_coincident_cross_room_portals_pair(self):
        prefabs = self.fixture()
        rooms = discover_rooms("Root", prefabs)
        for room in rooms:
            room["portals"] = [
                value
                for value in walk_room_actors(room, prefabs)
                if value["class"] == "Portal"
            ]
        graph = assemble_portal_graph(rooms)

        self.assertEqual(len(graph["connections"]), 1)
        self.assertEqual(graph["connections"][0]["position"], [110.0, 0.0, 0.0])
        self.assertEqual(len(graph["boundaries"]), 2)
        self.assertEqual(graph["unresolved"], [])

    def test_ambiguous_coincident_cluster_remains_unresolved(self):
        prefabs = self.fixture()
        prefabs["Module"]["compound_refs"].append(
            compound("Module", 2, "RoomC", 3, 10, 0, 0)
        )
        prefabs["RoomC"] = {
            "compound_refs": [],
            "actors": [actor("RoomC", 0, "Portal", 0, 0, 0)],
        }
        rooms = discover_rooms("Root", prefabs)
        for room in rooms:
            room["portals"] = [
                value
                for value in walk_room_actors(room, prefabs)
                if value["class"] == "Portal"
            ]
        graph = assemble_portal_graph(rooms)

        self.assertEqual(graph["connections"], [])
        self.assertEqual(len(graph["unresolved"]), 1)
        self.assertEqual(
            graph["unresolved"][0]["reason"],
            "ambiguous_coincident_endpoint_cluster",
        )

    def test_volatile_large_output_locations_are_rejected(self):
        for path in (Path("/tmp/phase8/output.json"), Path("/dev/shm/output.json")):
            with self.assertRaisesRegex(ValueError, "volatile location"):
                reject_volatile_output(path)
        reject_volatile_output(Path("/home/brynn/Code/artifacts/phase8/output.json"))

    def test_complete_source_inventory_can_drive_publication_without_pack(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source_terrain_inventory.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "vanguard_source_terrain_inventory",
                        "version": 1,
                        "inventory_id": "fixture",
                        "generated_inputs_complete": True,
                        "chunk_count": 2,
                        "chunks": [
                            {"chunk": "chunk_2_1"},
                            {"chunk": "chunk_1_1"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            inventory, digest, chunks = load_source_terrain_inventory(path)

            self.assertEqual(inventory["inventory_id"], "fixture")
            self.assertEqual(len(digest), 64)
            self.assertEqual(chunks, ["chunk_1_1", "chunk_2_1"])

    def test_instance_runtime_eligibility_fails_closed_on_missing_visuals(self):
        template = {"runtime_eligibility": {"eligible": True}}
        self.assertEqual(
            instance_runtime_eligibility(template, []),
            {
                "eligible": True,
                "missing_visual_component_paths": [],
                "reason": "eligible",
            },
        )
        self.assertEqual(
            instance_runtime_eligibility(template, ["Room/Mesh1", "Room/Mesh1"]),
            {
                "eligible": False,
                "missing_visual_component_paths": ["Room/Mesh1"],
                "reason": "unavailable_room_visual_binding",
            },
        )
        self.assertEqual(
            instance_runtime_eligibility(
                {"runtime_eligibility": {"eligible": False}}, []
            )["reason"],
            "template_ineligible",
        )


if __name__ == "__main__":
    unittest.main()
