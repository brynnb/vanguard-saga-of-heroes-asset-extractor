import unittest

from scripts.lib.staticmesh_topology import (
    StaticMeshTopologyError,
    section_raw_index_count,
    section_triangle_indices,
)


class StaticMeshTopologyTest(unittest.TestCase):
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
