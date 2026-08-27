import unittest

from scripts.generators.generate_particle_cell_index import (
    build_effect_placement_topology,
    manifest_emitters_by_prefab_from_templates,
    walk_prefab_emitter_instances,
)


def record(template_id, **values):
    result = {
        "template_id": template_id,
        "object_name": "OBJ_Fixture",
        "chunk_position": [1.0, 2.0, 3.0],
        "global_position": [101.0, 2.0, -97.0],
    }
    result.update(values)
    return result


class ParticleCompoundTopologyTest(unittest.TestCase):
    def test_nested_prefab_walk_preserves_room_owner_and_compound_transform(self):
        room_path = "prefab/Root/actor/CompoundObject/RoomRef0"
        sidecar = {
            "Root": {
                "compound_refs": [
                    {
                        "class": "CompoundObject",
                        "name": "RoomRef0",
                        "sub_prefab": "RoomChild",
                        "source_component_path": room_path,
                        "props": {
                            "m_CompoundObjectType": 3,
                            "Location": [10.0, 20.0, 30.0],
                        },
                    }
                ]
            },
            "RoomChild": {"compound_refs": []},
        }
        emitters = {
            "RoomChild": [
                {"class": "FXFixture", "name": "FXFixture0", "props": {}}
            ]
        }

        result = walk_prefab_emitter_instances("Root", sidecar, emitters)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["prefab_name"], "RoomChild")
        self.assertEqual(result[0]["source_parent_path"], room_path)
        self.assertEqual(result[0]["room_source_component_path"], room_path)
        self.assertEqual(result[0]["prefab_transform"].origin, (10.0, 20.0, 30.0))

    def test_canonical_template_manifest_reconstructs_sparse_prefab_actor_table(self):
        templates = [
            {
                "id": "wrapper",
                "props": {"Emitters": {"count": 1, "raw_hex": "0002"}},
                "source": {
                    "kind": "sgo_prefab_extra",
                    "prefab": "Fixture",
                    "actor_index": 0,
                    "class": "FXFixture",
                    "name": "FXFixture0",
                },
            },
            {
                "id": "child",
                "props": {"MaxParticles": 4},
                "source": {
                    "kind": "sgo_prefab_extra",
                    "prefab": "Fixture",
                    "actor_index": 1,
                    "class": "SpriteEmitter",
                    "name": "SpriteEmitter0",
                },
            },
        ]

        result = manifest_emitters_by_prefab_from_templates(templates)

        self.assertEqual(len(result["Fixture"]), 2)
        self.assertEqual(result["Fixture"][0]["class"], "FXFixture")
        self.assertEqual(result["Fixture"][1]["props"], {"MaxParticles": 4})

    def test_wrapper_preserves_source_order_dependencies_and_atomic_groups(self):
        wrapper = record("wrapper_template")
        children = [
            record("child_a"),
            record("child_b", add_location_from_other_emitter=0),
            record("child_c"),
        ]

        placement = build_effect_placement_topology(
            chunk="chunk_n1_2",
            node_index=7,
            prefab_name="Fixture",
            root_actor_index=4,
            source_effect_path="prefab/Fixture/actor/FX/FXFixture0",
            wrapper_record=wrapper,
            component_records=children,
            source_actor_indices=[8, 6, 9],
        )

        component_ids = placement["ordered_emitter_component_ids"]
        self.assertEqual(len(component_ids), 3)
        self.assertEqual(
            placement["activation_ordered_emitter_component_ids"], component_ids
        )
        self.assertFalse(placement["dependency_cycle"])
        self.assertEqual(
            placement["dependency_edges"],
            [
                {
                    "source_component_id": component_ids[0],
                    "target_component_id": component_ids[1],
                    "relationship": "add_location",
                    "source_child_slot": 0,
                    "target_child_slot": 1,
                    "source_field": "add_location_from_other_emitter",
                }
            ],
        )
        self.assertEqual(len(placement["atomic_activation_groups"]), 2)
        self.assertEqual(
            placement["atomic_activation_groups"][0]["ordered_emitter_component_ids"],
            component_ids[:2],
        )
        self.assertEqual(
            placement["atomic_activation_groups"][1]["ordered_emitter_component_ids"],
            component_ids[2:],
        )
        self.assertTrue(wrapper["compound_wrapper_metadata_only"])
        self.assertEqual(wrapper["emitter_component_id"], "")
        self.assertEqual(children[1]["source_child_slot"], 1)
        self.assertEqual(children[0]["effect_placement_id"], placement["placement_id"])
        self.assertEqual(children[0]["layer_membership"], ["base"])

    def test_dependency_cycle_is_explicit_and_source_order_is_stable_fallback(self):
        children = [
            record("child_a", branch_emitter=1),
            record("child_b", add_velocity_from_other_emitter=0),
        ]
        placement = build_effect_placement_topology(
            chunk="chunk_0_0",
            node_index=1,
            prefab_name="CycleFixture",
            root_actor_index=0,
            source_effect_path="prefab/CycleFixture/actor/FX/CycleFixture0",
            wrapper_record=record("cycle_wrapper"),
            component_records=children,
            source_actor_indices=[1, 2],
        )

        self.assertTrue(placement["dependency_cycle"])
        self.assertEqual(
            placement["activation_ordered_emitter_component_ids"],
            placement["ordered_emitter_component_ids"],
        )
        self.assertEqual(len(placement["atomic_activation_groups"]), 1)

    def test_standalone_component_has_stable_single_member_transaction(self):
        first_record = record("standalone")
        first = build_effect_placement_topology(
            chunk="chunk_3_4",
            node_index=2,
            prefab_name="Standalone",
            root_actor_index=5,
            source_effect_path="prefab/Standalone/actor/SpriteEmitter/Standalone5",
            wrapper_record=None,
            component_records=[first_record],
            source_actor_indices=[5],
        )
        second_record = record("standalone")
        second = build_effect_placement_topology(
            chunk="chunk_3_4",
            node_index=2,
            prefab_name="Standalone",
            root_actor_index=5,
            source_effect_path="prefab/Standalone/actor/SpriteEmitter/Standalone5",
            wrapper_record=None,
            component_records=[second_record],
            source_actor_indices=[5],
        )

        self.assertEqual(first["placement_id"], second["placement_id"])
        self.assertEqual(
            first["ordered_emitter_component_ids"],
            second["ordered_emitter_component_ids"],
        )
        self.assertEqual(len(first["atomic_activation_groups"]), 1)
        self.assertEqual(first["wrapper_template_id"], "")


if __name__ == "__main__":
    unittest.main()
