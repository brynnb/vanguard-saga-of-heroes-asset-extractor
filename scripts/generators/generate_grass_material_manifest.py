#!/usr/bin/env python3
"""Generate a chunk-to-grass-material manifest from decompiled terrain metadata."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from extractors.terrain_info_reader import parse_grass_data_file
from material_memory import MaterialMemoryResolver


DEFAULT_MAPS_ROOT = PROJECT_ROOT.parent / "vanguard-client" / "reference" / "Maps"
DEFAULT_SHADERS_ROOT = PROJECT_ROOT.parent / "vanguard-client" / "reference" / "Shaders"
DEFAULT_TEXTURES_ROOT = PROJECT_ROOT / "output" / "textures"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "terrain" / "terrain_grid" / "grass_materials.json"
DEFAULT_GRASS_MATERIAL = "P0001_SpeedTrees_shaders.Shaders.GrassTest"
DEFAULT_GRASS_TEXTURE = "New_Thestra_Grass01.png"

_TEXTURE_FIELD_RE = re.compile(r"\b(Diffuse|Detail)=Texture'([^']+)'")


def _texture_filename(texture_ref: str) -> str:
    return texture_ref.split(".")[-1] + ".png"


def _texture_package(texture_ref: str | None) -> str | None:
    if not texture_ref or "." not in texture_ref:
        return None
    return texture_ref.split(".", 1)[0]


def _texture_name(texture_ref: str | None) -> str | None:
    if not texture_ref:
        return None
    return texture_ref.split(".")[-1]


def _repo_path_exists(path_value: str | None) -> bool:
    if not path_value:
        return False
    path = Path(path_value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.exists()


def _shader_file_for_material(material_ref: str, shaders_root: Path) -> Path:
    parts = material_ref.split(".")
    package_name = parts[0] if parts else ""
    material_name = parts[-1] if parts else material_ref
    return shaders_root / package_name / f"{material_name}.txt"


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _parse_shader_textures(shader_file: Path) -> dict[str, str]:
    if not shader_file.exists():
        return {}

    textures: dict[str, str] = {}
    with shader_file.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _TEXTURE_FIELD_RE.search(line)
            if m:
                textures[m.group(1).lower()] = m.group(2)
    return textures


def _apply_manifest_channel(
    material: dict[str, Any], prefix: str, channel: dict[str, Any] | None
) -> None:
    if not channel:
        return
    key_prefix = "" if prefix == "texture" else f"{prefix}_"
    material[f"{key_prefix}texture_reference"] = channel.get("texture_ref")
    material[f"{key_prefix}texture_package"] = channel.get("texture_package")
    material[f"{key_prefix}texture_name"] = channel.get("texture_name")
    material[f"{key_prefix}asset_name"] = channel.get("asset_name")
    material[f"{key_prefix}asset_path"] = channel.get("asset_path")
    if channel.get("texture_name"):
        material[f"{key_prefix}texture"] = f"{channel['texture_name']}.png"
    material[f"{key_prefix}texture_exists"] = _repo_path_exists(channel.get("asset_path"))


def _resolve_material(
    material_ref: str,
    shaders_root: Path,
    textures_root: Path,
    resolver: MaterialMemoryResolver | None,
) -> dict[str, Any]:
    material_name = material_ref.split(".")[-1]
    shader_file = _shader_file_for_material(material_ref, shaders_root)
    shader_textures = _parse_shader_textures(shader_file)
    material = {
        "name": material_name,
        "shader_file": _relative_path(shader_file, shaders_root),
        "shader_file_exists": shader_file.exists(),
        "texture_reference": None,
        "texture_package": None,
        "texture_name": None,
        "texture": None,
        "asset_name": None,
        "asset_path": None,
        "texture_exists": False,
        "detail_texture_reference": None,
        "detail_texture_package": None,
        "detail_texture_name": None,
        "detail_texture": None,
        "detail_asset_name": None,
        "detail_asset_path": None,
        "detail_texture_exists": False,
    }

    if resolver is not None and resolver.available:
        manifest_entry = resolver.material_manifest_entry(material_ref, textures_root)
        if manifest_entry:
            _apply_manifest_channel(material, "texture", manifest_entry.get("base_color"))
            _apply_manifest_channel(material, "detail", manifest_entry.get("detail"))
            return material

    diffuse_ref = shader_textures.get("diffuse")
    if diffuse_ref:
        texture = _texture_filename(diffuse_ref)
        material["texture_reference"] = diffuse_ref
        material["texture_package"] = _texture_package(diffuse_ref)
        material["texture_name"] = _texture_name(diffuse_ref)
        material["texture"] = texture
        material["texture_exists"] = (textures_root / texture).exists()

    detail_ref = shader_textures.get("detail")
    if detail_ref:
        detail_texture = _texture_filename(detail_ref)
        material["detail_texture_reference"] = detail_ref
        material["detail_texture_package"] = _texture_package(detail_ref)
        material["detail_texture_name"] = _texture_name(detail_ref)
        material["detail_texture"] = detail_texture
        material["detail_texture_exists"] = (textures_root / detail_texture).exists()

    return material


def build_manifest(maps_root: Path, shaders_root: Path, textures_root: Path) -> dict[str, Any]:
    chunks: dict[str, dict[str, Any]] = {}
    materials: dict[str, dict[str, Any]] = {}
    material_counts: dict[str, int] = {}
    resolver = MaterialMemoryResolver()

    if not maps_root.exists():
        return {
            "default_material": DEFAULT_GRASS_MATERIAL,
            "default_texture": DEFAULT_GRASS_TEXTURE,
            "default_asset_path": None,
            "chunks": chunks,
            "materials": materials,
            "summary": {
                "chunks_with_grass": 0,
                "materials": 0,
                "maps_root": str(maps_root),
                "maps_root_exists": False,
                "missing_shader_files": 0,
                "missing_textures": 0,
                "material_counts": material_counts,
            },
        }

    for terrain_info_path in sorted(maps_root.glob("*/terrain_info.txt")):
        grass_data = parse_grass_data_file(terrain_info_path)
        if not grass_data:
            continue

        material_ref = grass_data["grass_material"]
        if material_ref not in materials:
            materials[material_ref] = _resolve_material(
                material_ref, shaders_root, textures_root, resolver
            )

        material = materials[material_ref]
        chunk_name = terrain_info_path.parent.name
        material_counts[material_ref] = material_counts.get(material_ref, 0) + 1
        chunks[chunk_name] = {
            "grass_material": material_ref,
            "grass_material_name": grass_data["grass_material_name"],
            "grass_type_scales": grass_data["grass_type_scales"],
            "texture": material["texture"],
            "texture_reference": material["texture_reference"],
            "texture_package": material["texture_package"],
            "texture_name": material["texture_name"],
            "asset_name": material["asset_name"],
            "asset_path": material["asset_path"],
            "texture_exists": material["texture_exists"],
            "detail_texture": material["detail_texture"],
            "detail_texture_reference": material["detail_texture_reference"],
            "detail_texture_package": material["detail_texture_package"],
            "detail_texture_name": material["detail_texture_name"],
            "detail_asset_name": material["detail_asset_name"],
            "detail_asset_path": material["detail_asset_path"],
            "detail_texture_exists": material["detail_texture_exists"],
        }

    missing_shader_files = sum(
        1 for material in materials.values() if not material["shader_file_exists"]
    )
    missing_textures = sum(
        1 for material in materials.values() if material["texture"] and not material["texture_exists"]
    )

    return {
        "default_material": DEFAULT_GRASS_MATERIAL,
        "default_texture": DEFAULT_GRASS_TEXTURE,
        "default_asset_path": materials.get(DEFAULT_GRASS_MATERIAL, {}).get("asset_path"),
        "chunks": chunks,
        "materials": materials,
        "summary": {
            "chunks_with_grass": len(chunks),
            "materials": len(materials),
            "maps_root": str(maps_root),
            "maps_root_exists": True,
            "shaders_root": str(shaders_root),
            "textures_root": str(textures_root),
            "missing_shader_files": missing_shader_files,
            "missing_textures": missing_textures,
            "material_counts": dict(sorted(material_counts.items())),
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maps-root", type=Path, default=DEFAULT_MAPS_ROOT)
    parser.add_argument("--shaders-root", type=Path, default=DEFAULT_SHADERS_ROOT)
    parser.add_argument("--textures-root", type=Path, default=DEFAULT_TEXTURES_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    manifest = build_manifest(args.maps_root, args.shaders_root, args.textures_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if not args.quiet:
        summary = manifest["summary"]
        print(f"Wrote {args.output}")
        print(
            "Grass materials: chunks={chunks} materials={materials} missing_shaders={missing_shaders} missing_textures={missing_textures}".format(
                chunks=summary["chunks_with_grass"],
                materials=summary["materials"],
                missing_shaders=summary["missing_shader_files"],
                missing_textures=summary["missing_textures"],
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
