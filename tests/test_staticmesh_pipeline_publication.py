import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.extractors.staticmesh_pipeline import (
    process_package,
    write_failure_report,
    write_mesh_manifest,
)


class StaticMeshPipelinePublicationTests(unittest.TestCase):
    def test_duplicate_flat_names_select_historical_last_export_explicitly(self) -> None:
        first = SimpleNamespace(
            name="Duplicate",
            export_index=3,
            outer_name="Interiors",
            parse_status="complete",
            vertices=[object()],
            indices=[0, 0, 0],
        )
        last = SimpleNamespace(
            name="Duplicate",
            export_index=9,
            outer_name="Exteriors",
            parse_status="complete",
            vertices=[object()],
            indices=[0, 0, 0],
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def publish(mesh, path):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text("{}")
                return True

            with patch(
                "scripts.extractors.staticmesh_pipeline.parse_staticmesh_file",
                return_value=[first, last],
            ), patch(
                "scripts.extractors.staticmesh_pipeline.mesh_to_gltf",
                side_effect=publish,
            ) as exporter:
                stats = process_package(
                    "/source/Example.usx", None, 0, output_dir=str(output)
                )

        self.assertEqual(exporter.call_args.args[0].export_index, 9)
        self.assertEqual(stats["exported"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["name_collisions"][0]["selected_export_index"], 9)

    def test_manifest_contains_only_outputs_from_the_successful_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            package = root / "Meshes" / "Example.usx"
            package.parent.mkdir(parents=True)
            package.write_bytes(b"source package")
            (output / "Example").mkdir(parents=True)
            (output / "Example" / "Current.gltf").write_text("{}")
            (output / "Example" / "Historical.gltf").write_text("{}")

            count = write_mesh_manifest(
                str(output), ["Example/Current.gltf"], [str(package)]
            )

            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(count, 1)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["meshes"], ["Example/Current.gltf"])
            self.assertEqual(manifest["unclaimed_gltf_count"], 1)
            self.assertEqual(
                manifest["unclaimed_gltf_examples"], ["Example/Historical.gltf"]
            )
            self.assertEqual(len(manifest["source_packages"][0]["sha256"]), 64)

    def test_failure_report_does_not_replace_complete_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            manifest = output / "manifest.json"
            manifest.write_text('{"status":"complete"}\n')

            report = write_failure_report(
                str(output),
                {
                    "error": 1,
                    "failures": [{"package": "broken.usx", "error": "truncated"}],
                    "outputs": ["broken/partial.gltf"],
                },
            )

            self.assertEqual(json.loads(manifest.read_text())["status"], "complete")
            self.assertEqual(json.loads(report.read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
