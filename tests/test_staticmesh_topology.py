import unittest
import struct

from scripts.lib.staticmesh_topology import (
    StaticMeshTopologyError,
    section_raw_index_count,
    section_triangle_indices,
)
from scripts.lib.vanguard_staticmesh import (
    StaticMeshParseError,
    _validated_header_anchor,
)


class StaticMeshTopologyTest(unittest.TestCase):
    @staticmethod
    def _write_header(data: bytearray, anchor: int) -> None:
        struct.pack_into("<6f", data, anchor - 41, -1, -2, -3, 1, 2, 3)
        data[anchor - 17] = 1
        struct.pack_into("<4f", data, anchor - 16, 0, 0, 0, 4)
        struct.pack_into("<I", data, anchor, 236)
        struct.pack_into("<i", data, anchor + 236, 13)

    def test_header_fallback_requires_one_structurally_valid_anchor(self) -> None:
        data = bytearray(400)
        self._write_header(data, 80)
        self.assertEqual(_validated_header_anchor(bytes(data), []), 80)

    def test_ambiguous_structural_headers_are_rejected(self) -> None:
        data = bytearray(700)
        self._write_header(data, 80)
        self._write_header(data, 360)
        with self.assertRaisesRegex(StaticMeshParseError, "uniquely"):
            _validated_header_anchor(bytes(data), [])

    def test_triangle_section_uses_explicit_primitive_count(self) -> None:
        section = {
            "is_strip": False,
            "first_index": 3,
            "num_triangles": 4,
            "num_primitives": 4,
        }
        indices = [99, 99, 99, 0, 1, 2, 2, 1, 3, 4, 5, 6, 6, 5, 7]

        self.assertEqual(section_raw_index_count(section), 12)
        self.assertEqual(section_triangle_indices(indices, section, vertex_count=8), indices[3:15])

    def test_strip_section_uses_explicit_flag_and_discards_degenerates(self) -> None:
        section = {
            "is_strip": True,
            "first_index": 0,
            "num_triangles": 2,
            "num_primitives": 4,
        }
        indices = [0, 1, 2, 2, 3, 4]

        self.assertEqual(section_raw_index_count(section), 6)
        self.assertEqual(
            section_triangle_indices(indices, section, vertex_count=5),
            [0, 1, 2, 2, 4, 3],
        )

    def test_out_of_bounds_section_is_rejected_instead_of_truncated(self) -> None:
        section = {
            "is_strip": False,
            "first_index": 3,
            "num_triangles": 2,
            "num_primitives": 2,
        }

        with self.assertRaisesRegex(StaticMeshTopologyError, "exceeds"):
            section_triangle_indices([0, 1, 2, 3], section, vertex_count=4)

    def test_source_triangle_metadata_does_not_override_render_primitives(self) -> None:
        section = {
            "is_strip": False,
            "first_index": 0,
            "num_triangles": 492,
            "num_primitives": 2,
        }

        self.assertEqual(
            section_triangle_indices(
                [0, 1, 2, 2, 1, 3], section, vertex_count=4
            ),
            [0, 1, 2, 2, 1, 3],
        )


if __name__ == "__main__":
    unittest.main()
