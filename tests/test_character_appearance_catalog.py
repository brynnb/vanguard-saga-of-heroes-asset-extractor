import json
import struct
import tempfile
import unittest
from pathlib import Path

from scripts.extractors.decode_attachment_groups import CATEGORY_ORDER
from scripts.extractors.decode_items import RUNTIME_PACKAGE_INDEX_TO_SOURCE, write_catalog
from scripts.generators.generate_playable_races import PLAYABLE_VISUAL_SOURCE, _entry
from scripts.lib.material_memory import MaterialMemoryResolver
from scripts.lib.vanguard_emfxmesh import (
    FXAMaterial,
    _find_clr_texture,
    extract_skins_shaders,
)
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
    def test_tintable_materials_are_canonical_render_materials(self) -> None:
        resolver = object.__new__(MaterialMemoryResolver)
        wanted = resolver._record_wanted_property_names("TintableMaterial")

        self.assertIn("Diffuse", wanted)
        self.assertIn("TintAlpha", wanted)
        self.assertIn("TintPalette", wanted)

    def test_skins_material_slots_are_decoded_structurally(self) -> None:
        names = ["None", "Skins", "EMFXMaterial", "Material"]

        def array_property(name_index: int, payload: bytes) -> bytes:
            return bytes([name_index, 0x59, len(payload)]) + payload

        slot = bytes([3, 0x05, 0x81, 0])
        material_array = bytes([1]) + slot
        skin = array_property(2, material_array) + bytes([0])
        skins = bytes([1]) + skin
        export_data = array_property(1, skins) + bytes([0])

        class FakePackage:
            imports = [
                {
                    "class_name": "TintableMaterial",
                    "object_name": "human_M_char_head_0_SHD",
                    "package": -2,
                },
                {
                    "class_name": "Package",
                    "object_name": "UTX_human_M_char",
                    "package": 0,
                },
            ]
            exports = []

            def __init__(self) -> None:
                self.names = names

            def get_exports_by_class(self, _class_name: str) -> list[dict]:
                return [{"object_name": "human_M_char_head_0_C_0"}]

            def get_export_data(self, _export: dict) -> bytes:
                return export_data

        self.assertEqual(
            extract_skins_shaders(
                "unused.uem", "human_M_char_head_0_C_0", FakePackage()
            ),
            ["UTX_human_M_char.human_M_char_head_0_SHD"],
        )

    def test_character_material_prefers_exact_logical_texture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            texture_directory = Path(temp_directory)
            expected = (
                texture_directory
                / "UTX_human_M_char__Color__human_M_char_head_0_CLR.png"
            )
            expected.write_bytes(b"png")
            (
                texture_directory
                / "UTX_human_M_char__Color__human_M_char_body_1_CLR.png"
            ).write_bytes(b"wrong")
            material = FXAMaterial()
            material.name = "_CLASH_human_M_char_head_0_SHD3"

            resolved = _find_clr_texture(
                material,
                str(texture_directory),
                shader_map={},
                pkg_hint="UEM_human_M_char",
            )

            self.assertEqual(Path(resolved), expected)

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

    def test_item_catalog_is_split_into_indexed_package_payloads(self) -> None:
        catalog = {
            "schema": 3,
            "generated_by": "test",
            "identity": ["package_index", "attachment_index"],
            "runtime_package_index_to_source": {"15": "HEAD_ITEMS"},
            "packages": {
                "HEAD_ITEMS": {
                    "source_file": "UEM_HEAD_ITEMS.uem",
                    "source_package": "HEAD_ITEMS",
                    "attachments": {"7": [{"source_export": "Helm_7"}]},
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_directory:
            output_path = Path(temp_directory) / "item_appearance_catalog.json"
            stale_path = Path(temp_directory) / "item_appearances" / "STALE.json"
            stale_path.parent.mkdir(parents=True)
            stale_path.write_text("{}")

            write_catalog(catalog, output_path)

            index = json.loads(output_path.read_text())
            self.assertNotIn("attachments", index["packages"]["HEAD_ITEMS"])
            self.assertEqual(
                index["packages"]["HEAD_ITEMS"]["path"],
                "item_appearances/HEAD_ITEMS.json",
            )
            payload = json.loads(
                (Path(temp_directory) / index["packages"]["HEAD_ITEMS"]["path"]).read_text()
            )
            self.assertEqual(payload["attachments"]["7"][0]["source_export"], "Helm_7")
            self.assertFalse(stale_path.exists())

    def test_playable_visual_mapping_never_borrows_another_race(self) -> None:
        self.assertEqual(PLAYABLE_VISUAL_SOURCE["HighElf"], ("Elf", 0))
        self.assertEqual(PLAYABLE_VISUAL_SOURCE["DarkElf"], ("Elf", 1))
        self.assertNotIn("KojanBarbarian", PLAYABLE_VISUAL_SOURCE)
        unsupported = _entry("KojanBarbarian", "M")
        self.assertFalse(unsupported["visual_supported"])
        self.assertNotIn("optimized_package", unsupported)


if __name__ == "__main__":
    unittest.main()
