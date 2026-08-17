from __future__ import annotations

import unittest

from scripts.generators.generate_world_residency_activity_sources import (
    map_room_effect_bindings,
)


class WorldResidencyActivitySourceTests(unittest.TestCase):
    def test_room_effect_join_is_exact_by_chunk_node_and_source_path(self) -> None:
        interior = {
            "interior_templates": [
                {
                    "root_prefab": "HouseRoot",
                    "rooms": [
                        {
                            "room_id": "room_a",
                            "source_component_path": "prefab/HouseRoot/room/A",
                        }
                    ],
                }
            ],
            "instances": [
                {
                    "chunk": "chunk_n1_2",
                    "node_index": 7,
                    "root_prefab": "houseroot",
                    "interior_instance_id": "interior_1",
                }
            ],
        }
        effects = {
            "effect_placements": [
                {
                    "placement_id": "effect_b",
                    "source_chunk": "chunk_n1_2",
                    "source_node_index": 7,
                    "room_source_component_path": "prefab/HouseRoot/room/A",
                },
                {
                    "placement_id": "effect_a",
                    "source_chunk": "chunk_n1_2",
                    "source_node_index": 8,
                    "room_source_component_path": "",
                },
            ]
        }
        self.assertEqual(
            map_room_effect_bindings(effects, interior),
            [
                {
                    "placement_id": "effect_b",
                    "interior_instance_id": "interior_1",
                    "room_id": "room_a",
                    "room_source_component_path": "prefab/HouseRoot/room/A",
                }
            ],
        )

    def test_room_effect_without_exact_room_authority_fails(self) -> None:
        interior = {
            "interior_templates": [{"root_prefab": "root", "rooms": []}],
            "instances": [
                {
                    "chunk": "chunk_n1_2",
                    "node_index": 7,
                    "root_prefab": "root",
                    "interior_instance_id": "interior_1",
                }
            ],
        }
        effects = {
            "effect_placements": [
                {
                    "placement_id": "effect_a",
                    "source_chunk": "chunk_n1_2",
                    "source_node_index": 7,
                    "room_source_component_path": "missing",
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "no exact room path"):
            map_room_effect_bindings(effects, interior)


if __name__ == "__main__":
    unittest.main()
