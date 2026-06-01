#!/usr/bin/env python3
"""Build and validate package-qualified material manifest data.

The canonical manifest is keyed by full source material refs and records the
package-qualified texture refs plus exported PNG asset paths used by glTF
exporters. shader_to_texture.json can be projected from this later, but it is
not the source of truth for newly touched material code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPTS_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import config  # noqa: E402
from material_memory import MaterialMemoryResolver  # noqa: E402


DEFAULT_OUTPUT = Path(config.DATA_DIR) / "material_manifest.json"
DEFAULT_TEXTURES_DIR = PROJECT_ROOT / "output" / "textures"
DEFAULT_VALIDATION_OUTPUT = Path(config.DATA_DIR) / "material_manifest_validation.json"
DEFAULT_LEGACY_SHADER_MAP = Path(config.DATA_DIR) / "shader_to_texture.json"


def _read_shader_refs(path: str | os.PathLike[str]) -> list[str]:
    refs: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        refs.append(line)
    return refs


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Manifest does not exist: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Manifest root must be an object: {path}")
    return payload


def _load_optional_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _entry_has_renderable_base(entry: dict[str, Any]) -> bool:
    base_color = entry.get("base_color") or {}
    return bool(base_color.get("asset_path") or base_color.get("color_factor"))


def _selected_shader_infos(
    resolver: MaterialMemoryResolver,
    shader_refs: list[str],
    package_names: set[str],
    limit: int | None = None,
) -> list[Any]:
    if shader_refs:
        shader_infos = [
            info
            for shader_ref in shader_refs
            if (info := resolver.resolve_shader(shader_ref)) is not None
        ]
    else:
        shader_infos = resolver.iter_shaders()
    if package_names:
        package_keys = {name.lower() for name in package_names}
        shader_infos = [
            info
            for info in shader_infos
            if info.package_name.lower() in package_keys
        ]

    selected = []
    seen: set[str] = set()
    for shader_info in shader_infos:
        key = shader_info.full_path.lower()
        if key in seen:
            continue
        seen.add(key)
        selected.append(shader_info)
        if limit is not None and len(selected) >= limit:
            break
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build output/data/material_manifest.json from MaterialMemory.tab"
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Manifest JSON output path",
    )
    parser.add_argument(
        "--textures-dir",
        default=str(DEFAULT_TEXTURES_DIR),
        help="Directory where referenced PNG assets are emitted",
    )
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="Limit build to shaders from this source package; repeatable",
    )
    parser.add_argument(
        "--shader",
        action="append",
        default=[],
        help="Limit build to this full shader source ref; repeatable",
    )
    parser.add_argument(
        "--shader-file",
        help="Text file containing shader refs to build, one per line",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate an existing manifest instead of rebuilding it",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Merge into an existing output manifest and skip entries already present",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Build at most this many selected shader refs; useful for probes",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print build progress every N selected shader refs; 0 disables progress",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=100,
        help="Write partial manifest output every N newly built entries; 0 disables partial writes",
    )
    parser.add_argument(
        "--include-nonrenderable",
        action="store_true",
        help=(
            "Include Shader records that resolve no base texture or color fallback. "
            "Default builds skip these helper/special records so the canonical "
            "manifest contains renderable material entries only."
        ),
    )
    parser.add_argument(
        "--validation-output",
        default=str(DEFAULT_VALIDATION_OUTPUT),
        help="Validation report JSON output path",
    )
    parser.add_argument(
        "--legacy-shader-map",
        default=str(DEFAULT_LEGACY_SHADER_MAP),
        help=(
            "Optional legacy shader_to_texture.json used only to validate that "
            "previously textured materials did not lose base color textures"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when validation reports errors",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    textures_dir = Path(args.textures_dir)
    validation_output = Path(args.validation_output)
    legacy_shader_map_path = Path(args.legacy_shader_map) if args.legacy_shader_map else None

    resolver = MaterialMemoryResolver()
    if not resolver.available:
        print(
            f"ERROR: MaterialMemory.tab not found at {resolver.table_path}",
            file=sys.stderr,
        )
        return 2

    built_count = 0
    skipped_count = 0
    skipped_nonrenderable_count = 0

    if args.validate_only:
        manifest = _load_manifest(output_path)
    else:
        shader_refs = list(args.shader)
        if args.shader_file:
            shader_refs.extend(_read_shader_refs(args.shader_file))
        packages = {name for name in args.package if name}
        manifest = (
            _load_manifest(output_path)
            if args.resume and output_path.exists()
            else {}
        )
        textures_dir.mkdir(parents=True, exist_ok=True)
        selected_shader_infos = _selected_shader_infos(
            resolver,
            shader_refs,
            packages,
            args.limit if args.limit is None or args.limit >= 0 else None,
        )
        existing_keys = {key.lower() for key in manifest}
        last_flushed_built_count = 0
        for index, shader_info in enumerate(selected_shader_infos, start=1):
            key = shader_info.full_path
            if key.lower() in existing_keys:
                skipped_count += 1
            else:
                entry = resolver.material_manifest_entry(key, textures_dir)
                if entry is not None:
                    if args.include_nonrenderable or _entry_has_renderable_base(entry):
                        manifest[key] = entry
                        existing_keys.add(key.lower())
                        built_count += 1
                    else:
                        skipped_nonrenderable_count += 1
            if args.progress_every and index % args.progress_every == 0:
                print(
                    "Progress: "
                    f"{index}/{len(selected_shader_infos)} selected, "
                    f"built={built_count}, skipped={skipped_count}, "
                    f"nonrenderable={skipped_nonrenderable_count}, "
                    f"manifest={len(manifest)}",
                    flush=True,
                )
            if (
                args.flush_every
                and built_count
                and built_count % args.flush_every == 0
                and built_count != last_flushed_built_count
            ):
                _write_manifest(output_path, manifest)
                last_flushed_built_count = built_count
        _write_manifest(output_path, manifest)
        if args.progress_every:
            print(
                "Progress: "
                f"{len(selected_shader_infos)}/{len(selected_shader_infos)} selected, "
                f"built={built_count}, skipped={skipped_count}, "
                f"nonrenderable={skipped_nonrenderable_count}, "
                f"manifest={len(manifest)}",
                flush=True,
            )

    legacy_shader_map = (
        _load_optional_json_object(legacy_shader_map_path)
        if legacy_shader_map_path is not None
        else {}
    )
    issues = resolver.validate_material_manifest(
        manifest, root_dir=PROJECT_ROOT, legacy_shader_map=legacy_shader_map
    )
    validation_output.parent.mkdir(parents=True, exist_ok=True)
    with validation_output.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "issue_count": len(issues),
                "issues": issues,
                "build": {
                    "built": built_count,
                    "skipped_existing": skipped_count,
                    "skipped_nonrenderable": skipped_nonrenderable_count,
                    "include_nonrenderable": bool(args.include_nonrenderable),
                },
            },
            handle,
            indent=2,
        )
        handle.write("\n")

    errors = [issue for issue in issues if issue.get("severity") == "error"]
    print(f"Manifest: {output_path}")
    print(f"Materials: {len(manifest)}")
    if not args.validate_only:
        print(f"Skipped non-renderable Shader records: {skipped_nonrenderable_count}")
    print(f"Validation: {len(errors)} errors, {len(issues) - len(errors)} warnings")
    print(f"Validation report: {validation_output}")
    if errors and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
