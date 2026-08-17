import struct
import unittest

from scripts.lib.terraininfo_native import (
    DecoInstanceArrayInfo,
    MeshLookupRecord,
    iter_decoinstance_records,
)


class TerrainInfoNativeTests(unittest.TestCase):
    def test_decoinstance_heading_uses_first_rotation_byte(self) -> None:
        payload = bytearray(22)
        struct.pack_into("<h3f", payload, 0, 0, 10.0, 20.0, 30.0)
        payload[14] = 1
        payload[15] = 160
        payload[16] = 7
        payload[17] = 9
        struct.pack_into("<f", payload, 18, 1.25)
        array = DecoInstanceArrayInfo(0, 0, 1, 0, 1.0)
        lookup = {
            0: MeshLookupRecord(0, "TestTree", -1, 1000.0, 42, 0, 0)
        }

        records = list(iter_decoinstance_records(bytes(payload), array, lookup))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["yaw_byte"], 160)
        self.assertEqual(records[0]["pitch_byte"], 7)
        self.assertEqual(records[0]["roll_byte"], 9)
        self.assertAlmostEqual(records[0]["scale"], 1.25)


if __name__ == "__main__":
    unittest.main()
