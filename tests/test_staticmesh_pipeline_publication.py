import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.extractors.staticmesh_pipeline import (
    mesh_manifest_entries_from_object_artifact,
    object_artifact_mesh_requirements,
    process_package,
    resolve_section_shader_refs,
    write_failure_report,
    write_mesh_manifest,
)


class StaticMeshPipelinePublicationTests(unittest.TestCase):
    def test_duplicate_flat_names_publish_qualified_exports_and_compatibility_alias(self) -> None:
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

        self.assertEqual(exporter.call_count, 3)
        self.assertEqual(stats["exported"], 3)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["name_collisions"][0]["selected_export_index"], 9)
        self.assertEqual(
            {entry["outer"] for entry in stats["name_collisions"][0]["qualified_exports"]},
            {"Interiors", "Exteriors"},
        )
        self.assertTrue(
            any("/__outer__/Interiors/" in path for path in stats["outputs"])
        )

    def test_sparse_single_section_uses_only_surviving_material(self) -> None:
        sections = [
            {"num_primitives": 0},
            {"num_primitives": 0},
            {"num_primitives": 4},
            {"num_primitives": 0},
        ]
        self.assertEqual(
            resolve_section_shader_refs(sections, ["Stone", None]),
            [None, None, "Stone", None],
        )

    def test_full_sized_explicit_null_material_is_not_reassigned(self) -> None:
        sections = [
            {"num_primitives": 0},
            {"num_primitives": 3},
            {"num_primitives": 0},
        ]
        self.assertEqual(
            resolve_section_shader_refs(sections, ["Roof", None, "Wood"]),
            ["Roof", None, "Wood"],
        )

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

    def test_manifest_entries_can_be_recovered_from_object_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            objects = root / "artifact" / "objects"
            (output / "Package").mkdir(parents=True)
            (objects / "Package").mkdir(parents=True)
            (output / "Package" / "Tree.gltf").write_text("{}")
            (objects / "Package" / "Tree.glb").write_bytes(b"compact")

            entries = mesh_manifest_entries_from_object_artifact(
                str(output), str(root / "artifact")
            )

            self.assertEqual(entries, ["Package/Tree.gltf"])

    def test_object_artifact_scope_uses_package_before_outer_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            objects = root / "artifact" / "objects"
            package = root / "Meshes" / "Example.usx"
            package.parent.mkdir(parents=True)
            package.write_bytes(b"source package")
            qualified = Path("Example/__outer__/Room/Tree")
            (output / qualified.parent).mkdir(parents=True)
            (objects / qualified.parent).mkdir(parents=True)
            (output / qualified.with_suffix(".gltf")).write_text("{}")
            (objects / qualified.with_suffix(".glb")).write_bytes(b"compact")

            requirements = object_artifact_mesh_requirements(str(root / "artifact"))

            self.assertEqual(requirements, {"example": {"tree"}})

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
