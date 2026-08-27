import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATORS_ROOT = REPO_ROOT / "scripts" / "generators"

from scripts.generators.generate_object_cell_index import placement_record
from scripts.lib.world_residency_identity import (
    authoritative_source_node_identity,
    authoritative_source_object_id,
)
from scripts.generators.generate_world_residency_source_inventory import build_inventory


class WorldResidencySourceIdentityTest(unittest.TestCase):
    def test_actor_and_deco_source_identity_are_stable(self) -> None:
        object_id = authoritative_source_object_id(
            "chunk_n25_26", "StaticMeshActor", "StaticMeshActor107"
        )
        self.assertEqual(
            object_id,
            "ue2://Maps/chunk_n25_26.vgr#Export/StaticMeshActor/StaticMeshActor107",
        )
        self.assertEqual(
            authoritative_source_node_identity(
                "chunk_n25_26",
                {"class": "StaticMeshActor", "name": "StaticMeshActor107"},
            ),
            (object_id, "actor_root"),
        )

    def test_compact_record_preserves_authoritative_component_identity(self) -> None:
        strings: list[str] = []
        record = placement_record(
            node_index=7,
            component_index=2,
            node={
                "translation": [1.0, 2.0, 3.0],
                "extras": {
                    "authoritative_source_object_id": "ue2://Maps/test.vgr#Export/Compound/C0",
                    "authoritative_source_node_id": "actor_root",
                },
            },
            object_name="C0",
            prefab_name="FixturePrefab",
            mesh_path="Fixture/Test.gltf",
            mesh_name="Test",
            visual_tier="near_structure",
            runtime_meshes={"Fixture/Test.gltf": {"asset_id": "Fixture/Test#abc"}},
            asset_refs={},
            assets=[],
            string_refs={},
            strings=strings,
            component={
                "source_component_path": "prefab/FixturePrefab/actor/StaticMeshActor/S0"
            },
        )
        self.assertEqual(len(record), 12)
        self.assertEqual(strings[record[9]], "ue2://Maps/test.vgr#Export/Compound/C0")
        self.assertEqual(strings[record[10]], "actor_root")
        self.assertEqual(
            strings[record[11]],
            "prefab/FixturePrefab/actor/StaticMeshActor/S0",
        )

    def test_inventory_comes_from_source_terraininfo_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            maps = root / "Maps"
            output = root / "output"
            runtime = output / "godot_runtime"
            maps.mkdir()
            for chunk in ("chunk_0_0", "chunk_0_1", "chunk_0_2"):
                (maps / f"{chunk}.vgr").write_bytes(b"fixture")
            for chunk in ("chunk_0_0", "chunk_0_1"):
                terrain_path = output / "terrain" / "terrain_grid" / f"{chunk}_terrain.glb"
                object_path = runtime / "chunks" / chunk / "object_cells.json"
                terrain_path.parent.mkdir(parents=True, exist_ok=True)
                object_path.parent.mkdir(parents=True, exist_ok=True)
                terrain_path.write_bytes(b"glb")
                object_path.write_text("{}", encoding="utf-8")

            class FixturePackage:
                def __init__(self, path: str) -> None:
                    name = Path(path).stem
                    self.exports = (
                        []
                        if name == "chunk_0_2"
                        else [
                            {
                                "class_name": "TerrainInfo",
                                "object_name": f"TerrainInfo_{name}",
                            }
                        ]
                    )

            with patch(
                "scripts.generators.generate_world_residency_source_inventory.UE2Package",
                FixturePackage,
            ):
                inventory = build_inventory(
                    maps_root=maps,
                    output_root=output,
                    runtime_root=runtime,
                    space_asset_id="fixture-space",
                    source_zone_asset_id="fixture-zone",
                    require_generated_inputs=True,
                )
            self.assertEqual(inventory["chunk_count"], 2)
            self.assertEqual(inventory["source_chunk_count"], 3)
            self.assertTrue(inventory["generated_inputs_complete"])
            self.assertEqual(
                inventory["excluded_source_chunks"],
                [
                    {
                        "chunk": "chunk_0_2",
                        "source_package_relative_path": "Maps/chunk_0_2.vgr",
                        "reason": "no_terraininfo_export",
                    }
                ],
            )
            self.assertTrue(
                inventory["chunks"][0]["authoritative_source_terrain_id"].startswith(
                    "ue2://Maps/chunk_0_0.vgr#TerrainInfo/"
                )
            )

    def test_inventory_can_use_authoritative_continent_partition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            maps = root / "Maps"
            output = root / "output"
            runtime = output / "godot_runtime"
            maps.mkdir()
            for chunk in ("chunk_n20_4", "chunk_n13_n9", "chunk_n26_1"):
                (maps / f"{chunk}.vgr").write_bytes(b"fixture")
                terrain_path = output / "terrain" / "terrain_grid" / f"{chunk}_terrain.glb"
                object_path = runtime / "chunks" / chunk / "object_cells.json"
                terrain_path.parent.mkdir(parents=True, exist_ok=True)
                object_path.parent.mkdir(parents=True, exist_ok=True)
                terrain_path.write_bytes(b"glb")
                object_path.write_text("{}", encoding="utf-8")
            catalog = root / "vgo_world.sql"
            catalog.write_text(
                "INSERT INTO `chunks` VALUES "
                "(4,'Kojan','GullosGrotto','Gullo\\'s Grotto','chunk_n20_4',-20,4,0),"
                "(85,'Qalia','Khal','Khal','chunk_n13_n9',-13,-9,0),"
                "(1,'Isle of Dawn','IsleofDawn','Isle of Dawn','chunk_n26_1',-26,1,0);\n",
                encoding="utf-8",
            )

            class FixturePackage:
                def __init__(self, path: str) -> None:
                    self.exports = [
                        {
                            "class_name": "TerrainInfo",
                            "object_name": f"TerrainInfo_{Path(path).stem}",
                        }
                    ]

            with patch(
                "scripts.generators.generate_world_residency_source_inventory.UE2Package",
                FixturePackage,
            ):
                inventory = build_inventory(
                    maps_root=maps,
                    output_root=output,
                    runtime_root=runtime,
                    space_asset_id="vanguard-world-exterior-kojan",
                    source_zone_asset_id="vanguard-source-continent-kojan",
                    require_generated_inputs=True,
                    chunk_catalog_path=catalog,
                    source_continent="Kojan",
                )
            self.assertEqual(inventory["chunk_count"], 1)
            self.assertEqual(inventory["source_chunk_count"], 1)
            self.assertEqual(inventory["chunks"][0]["chunk"], "chunk_n20_4")
            self.assertEqual(inventory["chunks"][0]["source_chunk_catalog_id"], 4)
            self.assertEqual(inventory["chunks"][0]["source_continent"], "Kojan")
            self.assertEqual(
                inventory["source_partition"]["selected_source_chunk_count"], 1
            )
            self.assertEqual(inventory["source_partition"]["all_source_chunk_count"], 3)
            self.assertTrue(
                inventory["source_partition"]["source_revision"].startswith("sha256:")
            )


if __name__ == "__main__":
    unittest.main()
