import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vanguard_assets import cli, config, pipeline


class CliPackagingTests(unittest.TestCase):
    def test_console_parser_exposes_supported_commands(self) -> None:
        parser = cli.build_parser()
        command_action = next(
            action
            for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        self.assertEqual(
            set(command_action.choices),
            {
                "setup",
                "extract-all",
                "build-shaders",
                "extract-terrain",
                "export-meshes",
                "export-characters",
                "export-animations",
                "export-facial-controls",
                "export-npc-assembly",
                "extract-audio",
                "extract-world",
                "fetch-unreal-library",
            },
        )

    def test_every_pipeline_stage_is_importable(self) -> None:
        modules = {
            "scripts.setup_assets",
            "scripts.extractors.bulk_extract_chunk_data",
            "scripts.extractors.build_material_manifest",
            "scripts.extractors.build_shader_texture_map",
            "scripts.extractors.parse_sgo_prefabs",
            "scripts.extractors.dump_sgo_raw",
            "scripts.extractors.split_sgo_by_class",
            "scripts.extractors.fold_actors_into_prefabs",
            "scripts.extractors.extract_particle_textures",
            "scripts.generators.generate_particle_manifest",
            "scripts.generators.generate_objects_from_txt",
            "scripts.generators.generate_particle_cell_index",
            "scripts.exporters.export_character_meshes",
            "scripts.extractors.decode_items",
            "scripts.extractors.decode_attachment_groups",
            "scripts.generators.generate_playable_races",
            "scripts.generators.generate_customization_data",
            "scripts.exporters.export_playable_facial_controls",
            "scripts.exporters.export_emfx_animations",
            "scripts.exporters.export_animations",
            "scripts.exporters.export_actor_race_visual_map",
            "scripts.exporters.export_object_race_mesh_map",
            "scripts.exporters.build_race_prefix_map",
            "scripts.exporters.export_npc_assembly",
            "scripts.extractors.extract_uax_wav",
            "scripts.extractors.extract_isb",
            "scripts.extractors.dump_icb",
        }
        missing = sorted(
            module for module in modules if importlib.util.find_spec(module) is None
        )
        self.assertEqual(missing, [])

    def test_dry_run_uses_packaged_pipeline_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "emu" / "Assets"
            assets.mkdir(parents=True)
            args = cli.build_parser().parse_args(
                [
                    "extract-all",
                    "--assets",
                    str(assets),
                    "--emu-root",
                    str(assets.parent),
                    "--sections",
                    "audio",
                    "--dry-run",
                ]
            )
            with patch.object(cli, "run") as run:
                args.func(args)

            command = run.call_args.args[1]
            self.assertEqual(command[:3], [sys.executable, "-m", "vanguard_assets.pipeline"])
            self.assertIn("--dry-run", command)

    def test_pipeline_rejects_an_uninstalled_stage(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Pipeline module not found"):
            pipeline.validate_command(
                [sys.executable, "-m", "scripts.extractors.not_a_real_stage"]
            )

    def test_workspace_defaults_outside_the_installed_package(self) -> None:
        package_root = Path(cli.__file__).resolve().parent
        self.assertNotEqual(config.PROJECT_ROOT, package_root)


if __name__ == "__main__":
    unittest.main()
