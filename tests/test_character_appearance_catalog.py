import struct
import unittest

from scripts.extractors.decode_attachment_groups import CATEGORY_ORDER
from scripts.extractors.decode_items import RUNTIME_PACKAGE_INDEX_TO_SOURCE
from scripts.lib.ue2_tagged_properties import (
    TYPE_BOOL,
    TYPE_INT,
    TaggedPropertyError,
    decode_object_reference_array,
    decode_scalar,
    properties_by_name,
    read_tagged_properties,
)


class TaggedPropertyReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.names = ["None", "Value", "Enabled", "Nested", "TestStruct"]

    def test_preserves_bool_value_and_int_payload(self) -> None:
        data = bytes([1, 0x22]) + struct.pack("<i", 37) + bytes([2, 0x83, 0])

        props = properties_by_name(read_tagged_properties(data, self.names))

        self.assertEqual(props["Value"].type_id, TYPE_INT)
        self.assertEqual(decode_scalar(props["Value"], self.names), 37)
        self.assertEqual(props["Enabled"].type_id, TYPE_BOOL)
        self.assertTrue(decode_scalar(props["Enabled"], self.names))

    def test_bounded_struct_may_end_at_its_declared_size(self) -> None:
        data = bytes([1, 0x22]) + struct.pack("<i", 9)

        props = read_tagged_properties(data, self.names, require_terminator=False)

        self.assertEqual(len(props), 1)
        self.assertEqual(decode_scalar(props[0], self.names), 9)
        with self.assertRaises(TaggedPropertyError):
            read_tagged_properties(data, self.names)

    def test_object_arrays_decode_compact_signed_references_exactly(self) -> None:
        self.assertEqual(decode_object_reference_array(bytes([2, 0x85, 7])), [-5, 7])
        with self.assertRaises(TaggedPropertyError):
            decode_object_reference_array(bytes([1, 7, 99]))


class AppearanceCatalogContractTest(unittest.TestCase):
    def test_runtime_package_mapping_is_one_to_one(self) -> None:
        sources = list(RUNTIME_PACKAGE_INDEX_TO_SOURCE.values())
        self.assertEqual(len(sources), len(set(sources)))
        self.assertEqual(RUNTIME_PACKAGE_INDEX_TO_SOURCE[15], "HEAD_ITEMS")
        self.assertEqual(RUNTIME_PACKAGE_INDEX_TO_SOURCE[33], "SWORD_ITEMS")

    def test_sag_category_order_matches_original_client_switch(self) -> None:
        self.assertEqual(CATEGORY_ORDER[0], "Shirts")
        self.assertEqual(CATEGORY_ORDER[8], "Swords")
        self.assertEqual(CATEGORY_ORDER[-2:], ["Hair", "FacialHair"])
        self.assertEqual(len(CATEGORY_ORDER), 17)


if __name__ == "__main__":
    unittest.main()
