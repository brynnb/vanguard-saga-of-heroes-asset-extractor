import json
import unittest

from scripts.lib.sgo_parser import _decode_struct
from ue2.properties import parse_struct_value


class VanguardColorDecodingTest(unittest.TestCase):
    def test_sgo_color_uses_vanguard_core_field_order(self) -> None:
        raw_bgra = bytes.fromhex("1c8dffff")

        decoded = _decode_struct("Color", raw_bgra, [], [])

        self.assertEqual(decoded, {"R": 255, "G": 141, "B": 28, "A": 255})

    def test_ue2_property_color_uses_vanguard_core_field_order(self) -> None:
        raw_bgra = bytes.fromhex("1c8dffff")

        decoded = json.loads(parse_struct_value(raw_bgra, "Color"))

        self.assertEqual(decoded, {"r": 255, "g": 141, "b": 28, "a": 255})


if __name__ == "__main__":
    unittest.main()
