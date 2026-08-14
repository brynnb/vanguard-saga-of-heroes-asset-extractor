import unittest
from unittest.mock import patch

from PIL import Image

from scripts.extractors.extract_all_terrain import extract_chunk_shadow_map


class _FakePackage:
    names = []
    exports = [
        {
            "class_name": "Texture",
            # Vanguard has real packages whose retained export suffix does not
            # match the containing chunk; the package remains authoritative.
            "object_name": "ChunkShadow_chunk_n23_27Height",
        }
    ]

    @staticmethod
    def get_export_data(_export):
        return b"fixture"


class _FakeTexture:
    def __init__(self, _data, _names):
        pass

    @staticmethod
    def get_image(_mip):
        return Image.new("L", (512, 512), 255)


class TerrainChunkShadowTest(unittest.TestCase):
    def test_shadow_is_bound_to_containing_package_and_described(self) -> None:
        with patch("ue2.texture.Texture", _FakeTexture):
            image, metadata = extract_chunk_shadow_map(
                _FakePackage(), "chunk_n18_4"
            )

        self.assertIsNotNone(image)
        self.assertEqual(image.mode, "L")
        self.assertEqual(image.size, (512, 512))
        self.assertEqual(metadata["file"], "chunk_shadow.png")
        self.assertEqual(metadata["format"], "l8")
        self.assertEqual(metadata["source_chunk"], "chunk_n18_4")
        self.assertEqual(metadata["association"], "containing_vgr_package")
        self.assertEqual(metadata["semantic"], "baked_vegetation_shadow_mask")
        self.assertIn("not vertex color", metadata["note"])


if __name__ == "__main__":
    unittest.main()
