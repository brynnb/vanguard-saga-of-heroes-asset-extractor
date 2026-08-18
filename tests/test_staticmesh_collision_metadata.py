import base64
import json
import struct
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
            # Vanguard's clockwise face order is intentionally opposite the
            # source vertex normal. The handedness-changing glTF position
            # swizzle makes the exported face and normal agree without an
            # additional index reversal.
            vertices=[
                ParsedVertex(0.0, 0.0, 0.0, nz=1.0),
                ParsedVertex(1.0, 0.0, 0.0, nz=1.0),
                ParsedVertex(0.0, 1.0, 0.0, nz=1.0),
            ],
            indices=[0, 2, 1],
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
            effective_simple_collision_flags={
                "UseSimpleLineCollision": False,
                "UseSimpleBoxCollision": True,
                "UseSimpleKarmaCollision": True,
            },
            collision_model_ref=4,
            collision_model_name="Model4",
            collision_model_status="decoded",
            collision_model_positions=[
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            ],
            collision_model_indices=[0, 1, 2],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "wall.gltf"
            self.assertTrue(mesh_to_gltf(mesh, str(output)))
            document = json.loads(output.read_text())
            collision = document["asset"]["extras"]["vg_collision"]
            encoded = document["buffers"][0]["uri"].split(",", 1)[1]
            payload = base64.b64decode(encoded)
            primitive = document["meshes"][0]["primitives"][0]
            index_accessor = document["accessors"][primitive["indices"]]
            index_view = document["bufferViews"][index_accessor["bufferView"]]
            index_offset = int(index_view.get("byteOffset", 0)) + int(
                index_accessor.get("byteOffset", 0)
            )
            indices = struct.unpack_from("<3H", payload, index_offset)
            position_accessor = document["accessors"][
                primitive["attributes"]["POSITION"]
            ]
            position_view = document["bufferViews"][position_accessor["bufferView"]]
            position_offset = int(position_view.get("byteOffset", 0)) + int(
                position_accessor.get("byteOffset", 0)
            )
            positions = [
                struct.unpack_from("<3f", payload, position_offset + index * 12)
                for index in indices
            ]
            normal_accessor = document["accessors"][primitive["attributes"]["NORMAL"]]
            normal_view = document["bufferViews"][normal_accessor["bufferView"]]
            normal_offset = int(normal_view.get("byteOffset", 0)) + int(
                normal_accessor.get("byteOffset", 0)
            )
            normal = struct.unpack_from("<3f", payload, normal_offset + indices[0] * 12)
            edge_a = tuple(positions[1][axis] - positions[0][axis] for axis in range(3))
            edge_b = tuple(positions[2][axis] - positions[0][axis] for axis in range(3))
            face_normal = (
                edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
                edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
                edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
            )
            face_normal_dot = sum(
                face_normal[axis] * normal[axis] for axis in range(3)
            )
        self.assertEqual(collision["section_slots"], [True])
        self.assertEqual(collision["version"], 2)
        self.assertTrue(collision["effective_simple_flags"]["UseSimpleBoxCollision"])
        self.assertEqual(collision["collision_model"]["name"], "Model4")
        self.assertEqual(collision["collision_model"]["triangle_count"], 1)
        self.assertEqual(indices, (0, 2, 1))
        self.assertGreater(face_normal_dot, 0.0)
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
