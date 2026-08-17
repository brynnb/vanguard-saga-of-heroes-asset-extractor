import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.extractors.staticmesh_pipeline import (
    ParsedMesh,
    ParsedVertex,
    build_collision_helper_catalog,
    collision_family_base,
    collision_helper_base,
    mesh_to_gltf,
)
from scripts.lib.vanguard_staticmesh import read_staticmesh_collision_metadata


def compact_index(value: int) -> bytes:
    assert 0 <= value < 64
    return bytes([value])


def collision_property_fixture() -> tuple[bytes, list[str]]:
    names = [
        "None",
        "Collision",
        "Enable Collision",
        "UseSimpleLineCollision",
    ]
    enabled = compact_index(2) + bytes([0xD3, 0]) + compact_index(0)
    disabled = compact_index(2) + bytes([0x53, 0]) + compact_index(0)
    collision = compact_index(2) + enabled + disabled
    properties = (
        compact_index(1)
        + bytes([0x59, len(collision)])
        + collision
        + compact_index(3)
        + bytes([0xD3, 0])
        + compact_index(0)
    )
    return properties, names


class StaticMeshCollisionMetadataTests(unittest.TestCase):
    def test_terminal_helper_names_share_visible_lod_family(self) -> None:
        self.assertEqual(collision_helper_base("Keep_collision01"), "keep")
        self.assertEqual(collision_helper_base("Keep_collObj2"), "keep")
        self.assertEqual(collision_family_base("Keep_L0"), "keep")
        self.assertEqual(collision_family_base("Keep001_L0"), "keep")
        self.assertIsNone(collision_helper_base("CollisionTestBuilding"))

    def test_decodes_nested_collision_slots_and_simple_flags(self) -> None:
        data, names = collision_property_fixture()
        metadata = read_staticmesh_collision_metadata(data, names)
        self.assertEqual(metadata["status"], "decoded")
        self.assertEqual(metadata["slots"], [True, False])
        self.assertEqual(
            metadata["simple_flags"], {"UseSimpleLineCollision": True}
        )
        self.assertEqual(metadata["properties_end_offset"], len(data))

    def test_unknown_collision_array_does_not_break_mesh_extraction(self) -> None:
        data, names = collision_property_fixture()
        malformed = bytearray(data)
        # The first byte of the Collision payload is the compact array count.
        malformed[3] = 63
        metadata = read_staticmesh_collision_metadata(bytes(malformed), names)
        self.assertEqual(metadata["status"], "unsupported_payload")
        self.assertEqual(metadata["slots"], [])

    def test_nonstandard_property_stream_does_not_break_mesh_extraction(self) -> None:
        metadata = read_staticmesh_collision_metadata(b"\xff", ["None"])
        self.assertEqual(metadata["status"], "unsupported_property_stream")
        self.assertEqual(metadata["slots"], [])

    def test_gltf_persists_collision_contract(self) -> None:
        mesh = ParsedMesh(
            name="Wall",
            package_path="/assets/Meshes/Building.usx",
            export_index=12,
            bbox_min=(0.0, 0.0, 0.0),
            bbox_max=(1.0, 1.0, 0.0),
            bsphere_center=(0.5, 0.5, 0.0),
            bsphere_radius=1.0,
            lod_index=0,
            vertices=[
                ParsedVertex(0.0, 0.0, 0.0),
                ParsedVertex(1.0, 0.0, 0.0),
                ParsedVertex(0.0, 1.0, 0.0),
            ],
            indices=[0, 1, 2],
            bytes_total=1,
            bytes_parsed=1,
            bytes_unknown=0,
            coverage_pct=100.0,
            uses_heuristics=False,
            uses_skips=False,
            internal_version=13,
            section_count=1,
            parse_status="complete",
            outer_index=5,
            outer_name="Exterior",
            sections=[{"first_index": 0, "num_faces": 1}],
            skins=[],
            collision_slots=[True],
            simple_collision_flags={"UseSimpleBoxCollision": False},
            collision_metadata_status="decoded",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "wall.gltf"
            self.assertTrue(mesh_to_gltf(mesh, str(output)))
            collision = json.loads(output.read_text())["asset"]["extras"][
                "vg_collision"
            ]
        self.assertEqual(collision["section_slots"], [True])
        self.assertIs(collision["matches_section_count"], True)
        self.assertEqual(
            collision["source_identity"],
            {
                "package": "Building",
                "outer": "Exterior",
                "outer_index": 5,
                "export_index": 12,
                "mesh": "Wall",
            },
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            package = root / "Building"
            visual_path = package / "Wall_L0.gltf"
            helper_path = package / "Wall_collision01.gltf"
            self.assertTrue(mesh_to_gltf(replace(mesh, name="Wall_L0"), str(visual_path)))
            self.assertTrue(
                mesh_to_gltf(replace(mesh, name="Wall_collision01"), str(helper_path))
            )
            catalog = build_collision_helper_catalog(
                root,
                [
                    "Building/Wall_L0.gltf",
                    "Building/Wall_collision01.gltf",
                ],
            )
        self.assertEqual(catalog["link_count"], 1)
        self.assertEqual(
            catalog["links"]["Building/Wall_L0.gltf"]["helper_mesh_path"],
            "Building/Wall_collision01.gltf",
        )
