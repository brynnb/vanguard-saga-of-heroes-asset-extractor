import base64
import json
from pathlib import Path
import struct
import tempfile
import unittest

from scripts.lib.world_residency_interiors import AffineTransform
from scripts.lib.world_residency_portals import PortalApertureLibrary


def fixture_gltf() -> dict:
    positions = struct.pack(
        "<12f",
        -2.0,
        -1.0,
        0.0,
        2.0,
        -1.0,
        0.0,
        2.0,
        1.0,
        0.0,
        -2.0,
        1.0,
        0.0,
    )
    indices = struct.pack("<6H", 0, 1, 2, 0, 2, 3)
    payload = positions + indices
    return {
        "asset": {"version": "2.0"},
        "buffers": [
            {
                "byteLength": len(payload),
                "uri": "data:application/octet-stream;base64,"
                + base64.b64encode(payload).decode("ascii"),
            }
        ],
        "bufferViews": [
            {"buffer": 0, "byteLength": len(positions), "byteOffset": 0},
            {
                "buffer": 0,
                "byteLength": len(indices),
                "byteOffset": len(positions),
            },
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5123, "count": 6, "type": "SCALAR"},
        ],
        "meshes": [
            {
                "primitives": [
                    {
                        "attributes": {"POSITION": 0},
                        "indices": 1,
                        "mode": 4,
                    }
                ]
            }
        ],
    }


class WorldResidencyPortalsTest(unittest.TestCase):
    def test_exact_mesh_identity_and_transformed_aperture_are_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "FixturePackage" / "FixturePortal.gltf"
            path.parent.mkdir()
            path.write_text(json.dumps(fixture_gltf()), encoding="utf-8")
            transform = AffineTransform.from_properties(
                {
                    "Location": [10.0, 20.0, 30.0],
                    "DrawScale3D": [2.0, 3.0, 4.0],
                }
            )
            actor = {
                "name": "Portal0",
                "source_component_path": "fixture/Portal0",
                "static_mesh": "FixturePortal",
                "static_mesh_source": {
                    "name": "FixturePortal",
                    "object_path": "FixturePackage.FixturePortal",
                    "source_package": "FixturePackage",
                },
                "_transform": transform,
            }

            library = PortalApertureLibrary(root)
            mesh, aperture = library.transformed(actor)

            self.assertEqual(mesh["coordinate_space"], "vanguard_staticmesh_local")
            # glTF(-Y,Z,X) converts back to Vanguard(X,-glTF-X,Y).
            self.assertEqual(mesh["primitives"][0]["positions"][0], [0.0, 2.0, -1.0])
            self.assertEqual(
                aperture["primitives"][0]["positions"][0],
                [10.0, 26.0, 26.0],
            )
            self.assertEqual(aperture["triangle_count"], 2)
            self.assertEqual(aperture["vertex_count"], 4)
            self.assertTrue(aperture["plane"]["planar"])
            self.assertEqual(aperture["aperture_mesh_id"], mesh["aperture_mesh_id"])
            self.assertEqual(len(library.catalog()), 1)

    def test_name_only_portal_mesh_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = PortalApertureLibrary(Path(temporary))
            with self.assertRaisesRegex(ValueError, "no exact StaticMesh source identity"):
                library.resolve({"name": "Portal0", "static_mesh": "Ambiguous"})

    def test_semantically_invalid_position_accessor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "FixturePackage" / "FixturePortal.gltf"
            path.parent.mkdir()
            document = fixture_gltf()
            document["accessors"][0]["componentType"] = 5125
            path.write_text(json.dumps(document), encoding="utf-8")
            actor = {
                "name": "Portal0",
                "static_mesh_source": {
                    "name": "FixturePortal",
                    "object_path": "FixturePackage.FixturePortal",
                    "source_package": "FixturePackage",
                },
                "_transform": AffineTransform.identity(),
            }

            with self.assertRaisesRegex(ValueError, "invalid semantic layout"):
                PortalApertureLibrary(root).transformed(actor)


if __name__ == "__main__":
    unittest.main()
