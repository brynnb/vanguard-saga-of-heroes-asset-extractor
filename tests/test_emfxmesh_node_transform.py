import struct
import unittest

from scripts.lib.vanguard_emfxmesh import _parse_node_chunk


class EmfxMeshNodeTransformTest(unittest.TestCase):
    def test_scale_rotation_quaternion_is_separate_from_scale(self) -> None:
        payload = bytearray()
        payload.extend(struct.pack("<3f", 1.0, 2.0, 3.0))
        payload.extend(struct.pack("<4f", 0.1, 0.2, 0.3, 0.9))
        payload.extend(struct.pack("<4f", 0.0, 0.0, 0.0, 1.0))
        payload.extend(struct.pack("<3f", 1.51, 1.0, 1.4))
        payload.extend(struct.pack("<III", 0, 0, 0))
        payload.extend(struct.pack("<I", len("l_ulna_SCL")))
        payload.extend(b"l_ulna_SCL")
        payload.extend(struct.pack("<I", 0))

        node = _parse_node_chunk(bytes(payload), 3)

        self.assertIsNotNone(node)
        self.assertEqual(node.scale_rot, (0.0, 0.0, 0.0, 1.0))
        for actual, expected in zip(node.scale, (1.51, 1.0, 1.4)):
            self.assertAlmostEqual(actual, expected, places=5)
