import struct
import unittest

from scripts.lib.vanguard_bsp import (
    BspParseError,
    find_level_model_reference,
    model_collision_triangles,
    parse_model_data,
)


def compact(value: int) -> bytes:
    negative = value < 0
    value = abs(value)
    first = value & 0x3F
    value >>= 6
    if negative:
        first |= 0x80
    result = bytearray([first])
    if value:
        result[0] |= 0x40
        while value:
            next_byte = value & 0x7F
            value >>= 7
            if value:
                next_byte |= 0x80
            result.append(next_byte)
    return bytes(result)


def vector(x: float, y: float, z: float) -> bytes:
    return struct.pack("<fff", x, y, z)


def plane(x: float, y: float, z: float, w: float) -> bytes:
    return struct.pack("<ffff", x, y, z, w)


def primitive() -> bytes:
    return vector(-1.0, -2.0, -3.0) + vector(1.0, 2.0, 3.0) + b"\x01" + plane(0, 0, 0, 4)


def empty_model() -> bytes:
    return b"".join(
        [
            compact(0),
            primitive(),
            struct.pack("<i", 0),  # vectors
            struct.pack("<i", 0),  # points
            struct.pack("<i", 0),  # nodes
            struct.pack("<i", 0),  # surfaces
            struct.pack("<i", 0),  # vertices
            struct.pack("<i", 4),  # shared sides
            struct.pack("<i", 0),  # zones
            compact(0),  # polys
            struct.pack("<i", 0),  # bounds
            struct.pack("<i", 0),  # leaf hulls
            struct.pack("<i", 0),  # leaves
            struct.pack("<i", 0),  # lights
            struct.pack("<ii", 1, 0),
            struct.pack("<iii", 0, 0, 0),  # extension arrays
        ]
    )


def zoned_model() -> bytes:
    node = b"".join(
        [
            plane(0, 0, 1, 0),
            struct.pack("<Q", 0b11),
            b"\x00",
            *(compact(value) for value in (0, 0, -1, -1, -1, -1, -1)),
            plane(0, 0, 0, 1),
            plane(0, 0, 0, 1),
            bytes((0, 1, 3)),
            struct.pack("<ii", -1, 0),
            struct.pack("<iii", 0, 0, -1),
        ]
    )
    surface = b"".join(
        [
            compact(0),
            struct.pack("<I", 0),
            *(compact(value) for value in (0, 0, 1, 2, -1, 0)),
            plane(0, 0, 1, 0),
            struct.pack("<f", 32.0),
        ]
    )
    zones = b"".join(
        [
            compact(0),
            struct.pack("<QQf", 1, 3, 0.0),
            compact(0),
            struct.pack("<QQf", 2, 2, 0.0),
        ]
    )
    return b"".join(
        [
            compact(0),
            primitive(),
            struct.pack("<i", 3),
            vector(0, 0, 1),
            vector(1, 0, 0),
            vector(0, 1, 0),
            struct.pack("<i", 3),
            vector(0, 0, 0),
            vector(1, 0, 0),
            vector(0, 1, 0),
            struct.pack("<i", 1),
            node,
            struct.pack("<i", 1),
            surface,
            struct.pack("<i", 3),
            *(compact(value) for pair in ((0, -1), (1, -1), (2, -1)) for value in pair),
            struct.pack("<ii", 0, 2),
            zones,
            compact(0),
            struct.pack("<i", 0),
            struct.pack("<i", 0),
            struct.pack("<i", 1),
            compact(1),
            compact(0),
            compact(0),
            struct.pack("<Q", 3),
            struct.pack("<i", 0),
            struct.pack("<ii", 0, 0),
            struct.pack("<iii", 0, 0, 0),
        ]
    )


class VanguardBspTest(unittest.TestCase):
    def parse(self, data: bytes):
        return parse_model_data(
            data,
            ["None"],
            archive_version=128,
            licensee_version=34,
            export_count=4,
            import_count=2,
        )

    def test_empty_model_uses_int32_array_counts(self) -> None:
        model = self.parse(empty_model())

        self.assertEqual(model.num_zones, 0)
        self.assertEqual(model.num_shared_sides, 4)
        self.assertTrue(model.root_outside)
        self.assertEqual(model.extension_tail_bytes, 12)

    def test_zone_connectivity_visibility_and_leaf_are_retained(self) -> None:
        model = self.parse(zoned_model())

        self.assertEqual(len(model.nodes), 1)
        self.assertEqual(model.nodes[0].i_zone, [0, 1])
        self.assertEqual(model.nodes[0].i_collision_bound, -1)
        self.assertEqual(model.nodes[0].i_render_bound, -1)
        self.assertEqual(model.bounds, [])
        self.assertEqual([zone.connectivity for zone in model.zones], [1, 2])
        self.assertEqual([zone.visibility for zone in model.zones], [3, 2])
        self.assertEqual(model.leaves[0].i_zone, 1)
        self.assertEqual(model.leaves[0].visible_zones, 3)

        positions, indices = model_collision_triangles(model)
        self.assertEqual(positions, [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)])
        self.assertEqual(indices, [0, 1, 2])

    def test_revision_129_35_uses_the_verified_model_layout(self) -> None:
        model = parse_model_data(
            zoned_model(),
            ["None"],
            archive_version=129,
            licensee_version=35,
            export_count=4,
            import_count=2,
        )
        self.assertEqual(len(model.nodes), 1)

    def test_revision_129_34_uses_the_verified_model_layout(self) -> None:
        model = parse_model_data(
            zoned_model(),
            ["None"],
            archive_version=129,
            licensee_version=34,
            export_count=4,
            import_count=2,
        )
        self.assertEqual(len(model.nodes), 1)

    def test_truncated_model_fails_closed(self) -> None:
        with self.assertRaisesRegex(BspParseError, "truncated|cannot fit|missing"):
            self.parse(zoned_model()[:-7])

    def test_unsupported_package_revision_fails_closed(self) -> None:
        with self.assertRaisesRegex(BspParseError, "unsupported Vanguard package revision"):
            parse_model_data(
                empty_model(),
                ["None"],
                archive_version=127,
                licensee_version=34,
            )

    def test_level_trailer_selects_one_authoritative_model(self) -> None:
        trailer = b"".join(
            [
                compact(2),
                struct.pack("<f", 0.0),
                compact(0),
                *(compact(0) for _ in range(16)),
                struct.pack("<i", 0),
            ]
        )

        class FixturePackage:
            imports = [{}, {}]
            exports = [
                {"class_name": "Texture"},
                {"class_name": "Model"},
                {"class_name": "Level"},
            ]

            @staticmethod
            def get_export_data(_export):
                return b"fixture-prefix" + trailer

        self.assertEqual(
            find_level_model_reference(
                FixturePackage(), FixturePackage.exports[2]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
