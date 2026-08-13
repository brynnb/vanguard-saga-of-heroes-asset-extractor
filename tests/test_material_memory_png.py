from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts" / "lib"))

from material_memory import _is_valid_png, _publish_valid_png, _save_png_if_missing


class MaterialMemoryPngTests(unittest.TestCase):
    def test_publish_creates_a_valid_png(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "texture.png"
            _publish_valid_png(Image.new("RGBA", (2, 2), "red"), path)
            self.assertTrue(_is_valid_png(path))

    def test_publish_replaces_only_when_explicitly_repairing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "texture.png"
            corrupt = b"\x89PNG\r\n\x1a\ntruncated"
            path.write_bytes(corrupt)
            _publish_valid_png(Image.new("RGBA", (2, 2), "green"), path)
            self.assertEqual(path.read_bytes(), corrupt)

            _publish_valid_png(
                Image.new("RGBA", (2, 2), "green"),
                path,
                replace_invalid=True,
            )
            self.assertTrue(_is_valid_png(path))

    def test_save_if_missing_repairs_a_corrupt_existing_png(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "texture.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\ntruncated")
            _save_png_if_missing(Image.new("RGBA", (2, 2), "blue"), path)
            self.assertTrue(_is_valid_png(path))


if __name__ == "__main__":
    unittest.main()
