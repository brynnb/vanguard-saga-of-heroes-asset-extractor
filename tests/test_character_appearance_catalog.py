import json
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.generators import generate_playable_races
from scripts.exporters import build_race_prefix_map

from scripts.extractors.decode_attachment_groups import CATEGORY_ORDER
from scripts.extractors.decode_items import RUNTIME_PACKAGE_INDEX_TO_SOURCE, write_catalog
from scripts.generators.generate_playable_races import (
    PLAYABLE_VISUAL_SOURCE,
    _entry,
    _skin_tint_record,
)
from scripts.exporters.export_playable_facial_controls import (
    _modular_package_name_for_entry,
)
from scripts.exporters.export_character_meshes import RECOVERED_HAIR_TOP_EXPORTS
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
    def test_player_identity_uses_modular_without_requiring_optimized_meshes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            characters = Path(temp_directory)
            head = characters / "UEM_elf_M_char" / "elf_M_char_head_0_C_0.gltf"
            head.parent.mkdir()
            head.write_text("{}")
            with patch.object(generate_playable_races, "CHARACTERS", characters):
                entry = _entry("HighElf", "M")
                self.assertEqual(entry["visual_kind"], "modular_player")
                self.assertEqual(entry["modular_package"], "UEM_elf_M_char")
                self.assertEqual(entry["modular_master_export"], "elf_M_char_ALL_0_SKELETON")
                self.assertEqual(_modular_package_name_for_entry(entry), "UEM_elf_M_char")
                with self.assertRaisesRegex(RuntimeError, "Missing playable modular head"):
                    _entry("HighElf", "F")

    def test_npc_and_optimized_identities_are_not_collapsed(self) -> None:
        races = [
            {"id": 188, "name": "NPCHighElf", "category": "NPC"},
            {"id": 350, "name": "OPTHighElf", "category": "NPC"},
            {"id": 999, "name": "OPTUnknown", "category": "NPC"},
        ]
        recovered = {
            "NPCHighElf": {"visual_prefix": "npcElf", "normalized_prefix": "npcelf"},
            "OPTHighElf": {"visual_prefix": "optimizedElf", "normalized_prefix": "optimizedelf"},
        }
        with tempfile.TemporaryDirectory() as temp_directory:
            output = Path(temp_directory) / "race_to_mesh_prefix.json"
            with (
                patch.object(build_race_prefix_map, "configure_paths"),
                patch.object(build_race_prefix_map, "OUTPUT_PATH", str(output)),
                patch.object(build_race_prefix_map, "_load_exported_prefixes", return_value={"highelf", "npchuman", "optimizedelf", "unknown"}),
                patch.object(build_race_prefix_map, "_load_actor_race_visual_map", return_value=recovered),
                patch.object(build_race_prefix_map, "load_race_source", return_value=(races, {})),
                patch.object(build_race_prefix_map.glob, "glob", return_value=[]),
                redirect_stdout(StringIO()),
            ):
                build_race_prefix_map.main([])
            result = json.loads(output.read_text())
            self.assertEqual(result["188"]["visual_kind"], "modular_npc")
            self.assertEqual(result["188"]["client_visual_prefix"], "npcElf")
            self.assertEqual(result["350"]["visual_kind"], "optimized_npc")
            self.assertEqual(result["350"]["prefix"], "optimizedelf")
            self.assertNotIn("body_prefix", result["350"])
            self.assertIsNone(result["999"]["prefix"])
            self.assertEqual(result["999"]["source"], "unresolved_optimized_identity")

    def test_missing_hair_item_templates_have_an_exact_recovery_set(self) -> None:
        self.assertEqual(len(RECOVERED_HAIR_TOP_EXPORTS), 26)
        self.assertIn(
            "elf_M_char_hair_AB_WD_Messy1", RECOVERED_HAIR_TOP_EXPORTS
        )
        self.assertIn(
            "orc_M_char_hair_AB_Ponytail1", RECOVERED_HAIR_TOP_EXPORTS
        )
        self.assertIn("human_F_hair_idara_0_C_0", RECOVERED_HAIR_TOP_EXPORTS)
        self.assertTrue(
            all("Brow" not in name and "Eyebrow" not in name
                for name in RECOVERED_HAIR_TOP_EXPORTS)
        )

    def test_modular_master_package_derives_from_playable_family(self) -> None:
        self.assertEqual(
            _modular_package_name_for_entry(
                {
                    "visual_supported": True,
                    "optimized_package": "UEM_optimizedDwarf_M_char",
                }
            ),
            "UEM_dwarf_M_char",
        )
        self.assertEqual(
            _modular_package_name_for_entry(
                {
                    "visual_supported": True,
                    "optimized_package": "UEM_optimizedBarbarian_F_char",
                }
            ),
            "UEM_barbarian_F_char",
        )

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

    def test_eyebrow_placeholder_material_uses_authoritative_skin_shader(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            texture_directory = Path(temp_directory)
            expected = texture_directory / "female_brow.png"
            expected.write_bytes(b"png")
            source_ref = "UTX_generic_M_hair.Shader.Color_0_hair_brow_1"
            material = FXAMaterial()
            material.name = "generic_M_hair_eyebrows_1_SHD"

            resolved = _find_clr_texture(
                material,
                str(texture_directory),
                shader_map={},
                pkg_hint="UEM_elf_F_hair",
                skins_shaders=[source_ref],
                material_manifest={
                    source_ref: {
                        "base_color": {"asset_path": str(expected)},
                    }
                },
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

    def test_skin_tint_record_preserves_authored_material_assets(self) -> None:
        source_ref = "UTX_halfGiant_F_char.Shader.halfGiant_F_char_head_0_SHD"
        manifest = {
            source_ref: {
                "tint_alpha": {"asset_path": "output/textures/head_TNTA.png"},
                "tint_palette": {"asset_path": "output/textures/skin_TNT.png"},
            }
        }

        record = _skin_tint_record(manifest, "HalfGiant", "F", 0)

        self.assertEqual(record["head"]["source_ref"], source_ref)
        self.assertEqual(
            record["palette"]["asset_path"], "output/textures/skin_TNT.png"
        )
        self.assertEqual(record["body"], {})


if __name__ == "__main__":
    unittest.main()
