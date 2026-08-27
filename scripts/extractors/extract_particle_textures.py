#!/usr/bin/env python3
"""Extract direct Texture refs used by SGO particle emitters from UTX packages."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
import struct
import sys
import time
from typing import Any

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]

from vanguard_assets import config  # noqa: E402
PROJECT_ROOT = config.PROJECT_ROOT
from ue2 import UE2Package  # noqa: E402
from ue2.texture import Texture as UE2Texture  # noqa: E402


DEFAULT_EMITTERS = PROJECT_ROOT / "output/data/sgo_by_class/sgo_emitters.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output"
DEFAULT_MANIFEST = PROJECT_ROOT / "output/data/particle_texture_refs.json"
TEXTURE_REF_FIELDS = [
    ("Texture", "Texture__object_ref"),
    ("m_RibbonTexture", "m_RibbonTexture__object_ref"),
]


@dataclass(frozen=True)
class ParticleTextureRef:
    texture_name: str
    source_package: str
    package_chain: tuple[str, ...]
    object_path: str
    class_name: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emitters", type=Path, default=DEFAULT_EMITTERS)
    parser.add_argument("--textures-dir", type=Path, default=Path(config.TEXTURES_DIR))
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--force", action="store_true", help="Rewrite existing particle PNGs.")
    args = parser.parse_args()

    emitters_path = args.emitters.expanduser().resolve()
    textures_dir = args.textures_dir.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    out_path = args.out.expanduser().resolve()
    output_textures = output_root / "textures"

    if not emitters_path.exists():
        raise SystemExit(f"missing emitter bucket: {emitters_path}")
    if not textures_dir.exists():
        raise SystemExit(f"missing source texture package dir: {textures_dir}")

    emitters = read_json(emitters_path)
    refs, uses_by_ref = collect_particle_texture_refs(emitters)
    package_paths = build_package_path_index(textures_dir)
    package_cache: dict[str, Any] = {}
    output_textures.mkdir(parents=True, exist_ok=True)

    manifest_entries: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    t0 = time.time()

    for ref in sorted(refs, key=lambda item: item.object_path.lower()):
        result = extract_ref(
            ref=ref,
            uses=uses_by_ref[ref],
            package_paths=package_paths,
            package_cache=package_cache,
            output_textures=output_textures,
            output_root=output_root,
            force=args.force,
        )
        status_counts[str(result.get("status", "unknown"))] += 1
        manifest_entries.append(result)

    manifest = {
        "version": 2,
        "generated_by": "scripts/extractors/extract_particle_textures.py",
        "generated_at_unix": int(time.time()),
        "source_relative_path": relative_path(emitters_path, output_root),
        "source_texture_package_dir": str(textures_dir),
        "unique_ref_count": len(refs),
        "texture_use_count": sum(uses_by_ref.values()),
        "status_counts": dict(sorted(status_counts.items())),
        "entries": manifest_entries,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(
        "WROTE: particle texture refs=%d uses=%d extracted=%d existing=%d missing=%d failed=%d out=%s (%.1fs)"
        % (
            len(refs),
            sum(uses_by_ref.values()),
            status_counts.get("extracted", 0),
            status_counts.get("existing", 0),
            status_counts.get("missing_package", 0) + status_counts.get("missing_export", 0),
            status_counts.get("decode_failed", 0),
            out_path,
            time.time() - t0,
        )
    )
    return 0


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_particle_texture_refs(data: Any) -> tuple[set[ParticleTextureRef], Counter[ParticleTextureRef]]:
    refs: set[ParticleTextureRef] = set()
    uses_by_ref: Counter[ParticleTextureRef] = Counter()
    if not isinstance(data, dict):
        return refs, uses_by_ref
    for actors in data.values():
        if not isinstance(actors, list):
            continue
        for actor in actors:
            if not isinstance(actor, dict):
                continue
            props = actor.get("props", {})
            if not isinstance(props, dict):
                continue
            for texture_key, object_ref_key in TEXTURE_REF_FIELDS:
                texture_name = str(props.get(texture_key, "") or "").strip()
                object_ref = props.get(object_ref_key)
                if not texture_name or not isinstance(object_ref, dict):
                    continue
                package_chain = tuple(
                    str(part).strip()
                    for part in object_ref.get("package_chain", [])
                    if str(part).strip()
                )
                source_package = str(object_ref.get("source_package", "") or "").strip()
                if not source_package and package_chain:
                    source_package = package_chain[0]
                object_path = str(object_ref.get("object_path", "") or "").strip()
                if not object_path and source_package:
                    object_path = ".".join([*package_chain, texture_name])
                ref = ParticleTextureRef(
                    texture_name=texture_name,
                    source_package=source_package,
                    package_chain=package_chain,
                    object_path=object_path,
                    class_name=str(object_ref.get("class_name", "") or "").strip(),
                )
                refs.add(ref)
                uses_by_ref[ref] += 1
    return refs, uses_by_ref


def build_package_path_index(textures_dir: Path) -> dict[str, Path]:
    return {
        path.stem.lower(): path
        for path in textures_dir.glob("*.utx")
        if path.is_file()
    }


def extract_ref(
    *,
    ref: ParticleTextureRef,
    uses: int,
    package_paths: dict[str, Path],
    package_cache: dict[str, Any],
    output_textures: Path,
    output_root: Path,
    force: bool,
) -> dict[str, Any]:
    entry = {
        "texture": ref.texture_name,
        "source_package": ref.source_package,
        "package_chain": list(ref.package_chain),
        "object_path": ref.object_path,
        "class_name": ref.class_name,
        "uses": uses,
    }
    package_path = package_paths.get(ref.source_package.lower())
    if package_path is None:
        entry["status"] = "missing_package"
        return entry

    subpackage_chain = ref.package_chain
    if subpackage_chain and subpackage_chain[0].lower() == ref.source_package.lower():
        subpackage_chain = subpackage_chain[1:]
    output_name = output_texture_name(ref.source_package, subpackage_chain, ref.texture_name)
    output_path = output_textures / output_name
    entry["package_path"] = str(package_path)
    entry["relative_path"] = relative_path(output_path, output_root)

    pkg = open_package(package_path, package_cache)
    if pkg is None:
        entry["status"] = "open_failed"
        return entry
    export = find_texture_export(pkg, ref.texture_name, subpackage_chain)
    if export is None:
        entry["status"] = "missing_export"
        return entry
    entry["export_index"] = int(export.get("index", 0) or 0)
    entry["export_object_path"] = ".".join(
        [ref.source_package, *export_package_chain(pkg, int(export.get("package", 0) or 0)), ref.texture_name]
    )

    if output_path.exists() and not force:
        entry["status"] = "existing"
        return entry

    try:
        image = texture_image_from_export(pkg, export)
    except Exception as exc:  # noqa: BLE001 - keep one bad texture from stopping the batch.
        entry["status"] = "decode_failed"
        entry["error"] = str(exc)
        return entry
    if image is None:
        entry["status"] = "decode_failed"
        return entry

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    entry["status"] = "extracted"
    entry["width"] = int(image.width)
    entry["height"] = int(image.height)
    return entry


def open_package(package_path: Path, package_cache: dict[str, Any]) -> Any:
    key = str(package_path)
    if key in package_cache:
        return package_cache[key]
    try:
        package_cache[key] = UE2Package(str(package_path))
    except Exception:
        package_cache[key] = None
    return package_cache[key]


def find_texture_export(pkg: Any, texture_name: str, package_chain: tuple[str, ...]) -> dict[str, Any] | None:
    matches = [
        exp
        for exp in pkg.exports
        if exp.get("class_name") == "Texture"
        and str(exp.get("object_name", "")).lower() == texture_name.lower()
    ]
    if not matches:
        return None
    if package_chain:
        expected = tuple(part.lower() for part in package_chain)
        exact = [
            exp
            for exp in matches
            if tuple(part.lower() for part in export_package_chain(pkg, int(exp.get("package", 0) or 0)))
            == expected
        ]
        if exact:
            return exact[0]
    if len(matches) == 1:
        return matches[0]
    return None


def export_package_chain(pkg: Any, package_idx: int) -> list[str]:
    chain: list[str] = []
    seen: set[int] = set()
    idx = package_idx
    while idx != 0 and idx not in seen:
        seen.add(idx)
        if idx > 0:
            export_idx = idx - 1
            if not (0 <= export_idx < len(pkg.exports)):
                break
            outer = pkg.exports[export_idx]
            name = str(outer.get("object_name", "") or "")
            if name:
                chain.append(name)
            idx = int(outer.get("package", 0) or 0)
            continue
        import_idx = -idx - 1
        if not (0 <= import_idx < len(pkg.imports)):
            break
        outer_import = pkg.imports[import_idx]
        name = str(outer_import.get("object_name", "") or "")
        if name:
            chain.append(name)
        idx = int(outer_import.get("package", 0) or 0)
    chain.reverse()
    return chain


def texture_image_from_export(pkg: Any, texture_export: dict[str, Any]) -> Any:
    palette = load_palette_for_texture(pkg, texture_export)
    texture = UE2Texture(pkg.get_export_data(texture_export), pkg.names, palette=palette)
    if texture.format_id == 0:
        image = p8_image(texture, palette)
    else:
        image = texture.get_image(0) if texture.mips else None
    return image.convert("RGBA") if image is not None else None


def p8_image(texture: UE2Texture, palette: list[tuple[int, int, int, int]] | None) -> Image.Image | None:
    if palette is None or len(palette) != 256 or not texture.mips:
        return None
    mip = texture.mips[0]
    expected = mip.width * mip.height
    if len(mip.data) < expected:
        return None
    pixels = bytearray(expected * 4)
    for index, palette_index in enumerate(mip.data[:expected]):
        offset = index * 4
        r, g, b, a = palette[palette_index]
        pixels[offset : offset + 4] = bytes((r, g, b, a))
    return Image.frombytes("RGBA", (mip.width, mip.height), bytes(pixels))


def load_palette_for_texture(pkg: Any, texture_export: dict[str, Any]) -> list[tuple[int, int, int, int]] | None:
    try:
        texture = UE2Texture(pkg.get_export_data(texture_export), pkg.names)
        if texture.format_id != 0:
            return None
        palette_ref = texture.properties.get("Palette")
        if palette_ref is None:
            return None
        palette_index = int(palette_ref) - 1
        if not (0 <= palette_index < len(pkg.exports)):
            return None
        palette_export = pkg.exports[palette_index]
        if palette_export.get("class_name") != "Palette":
            return None
        palette_data = pkg.get_export_data(palette_export)
        if len(palette_data) < 5:
            return None
        count = struct.unpack("<i", palette_data[1:5])[0]
        if count != 256 or len(palette_data) < 5 + count * 4:
            return None
        palette = []
        for index in range(count):
            offset = 5 + index * 4
            palette.append(
                (
                    palette_data[offset],
                    palette_data[offset + 1],
                    palette_data[offset + 2],
                    palette_data[offset + 3],
                )
            )
        return palette
    except Exception:
        return None


def output_texture_name(source_package: str, package_chain: tuple[str, ...], texture_name: str) -> str:
    parts = [source_package, *package_chain, texture_name]
    return "__".join(safe_filename_part(part) for part in parts if part) + ".png"


def safe_filename_part(value: str) -> str:
    clean = str(value).strip()
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", clean)
    return clean.strip("._") or "unnamed"


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
