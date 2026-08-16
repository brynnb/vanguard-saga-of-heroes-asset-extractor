"""MaterialMemory-backed material resolver for offline exporters.

The original client cache records shader/material relationships that the older
static mesh pipeline flattened to diffuse-only texture names. This module keeps
that lookup offline: exporters can ask for package-qualified shader metadata
and generated glTF-friendly normal maps without making Godot parse .tab files.
"""

from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config
from PIL import Image
from ue2.package import UE2Package
from ue2.properties import find_property_start, parse_properties
from ue2.reader import read_compact_index_at
from ue2.texture import Texture as UE2Texture


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TABLE_PATH = Path(config.VANGUARD_EMU_ROOT) / "bin" / "MaterialMemory.tab"
SOURCE_PACKAGE_EXTENSIONS = {".utx", ".usx", ".u", ".vgr", ".sgo", ".ukx"}


@dataclass(frozen=True)
class MaterialTarget:
    raw: Any = None
    kind: str = "unresolved"
    class_name: str | None = None
    object_name: str | None = None
    full_path: str | None = None
    package_name: str | None = None


@dataclass
class ShaderMaterialInfo:
    full_path: str
    package_name: str
    object_name: str
    class_name: str = "Shader"
    diffuse: MaterialTarget | None = None
    normal: MaterialTarget | None = None
    specular: MaterialTarget | None = None
    opacity: MaterialTarget | None = None
    tint_alpha: MaterialTarget | None = None
    tint_palette: MaterialTarget | None = None
    detail: MaterialTarget | None = None
    detail_scale: float | None = None
    two_sided: bool = False
    output_blending: Any = None
    surface_type: Any = None
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def alpha_mode(self) -> str | None:
        if self.opacity is not None:
            return "mask"
        if self.output_blending in (1, "1", "OB_Masked"):
            return "mask"
        return None


@dataclass
class NormalMaterialInfo:
    full_path: str
    package_name: str
    object_name: str
    bump_map: MaterialTarget | None = None
    bump_scale: float | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpecularMaterialInfo:
    full_path: str
    package_name: str
    object_name: str
    exponent_map: MaterialTarget | None = None
    diffuse_strength: float | None = None
    specular_power: float | None = None
    specular_strength: float | None = None
    specular_color: list[float] | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class _MaterialBlock:
    offset: int
    size: int
    payload: bytes


@dataclass
class _MaterialRecord:
    asset_class: str
    package_name: str
    group_path: str
    object_name: str
    full_path: str
    blocks: list[_MaterialBlock]


def _read_fstring_at(data: bytes, offset: int) -> tuple[str, int]:
    length, string_start = read_compact_index_at(data, offset)
    if length <= 0:
        raise ValueError(f"invalid FString length {length} at 0x{offset:x}")
    string_end = string_start + length
    if string_end > len(data):
        raise ValueError(f"FString at 0x{offset:x} extends past EOF")
    raw = data[string_start:string_end]
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    return raw.decode("latin-1", "replace"), string_end


def _split_asset_ref(asset_ref: str) -> tuple[str, str, str, str, str]:
    if " " not in asset_ref:
        return "", "", "", asset_ref, asset_ref
    asset_class, full_path = asset_ref.split(" ", 1)
    parts = full_path.split(".")
    package_name = parts[0] if parts else ""
    group_path = ".".join(parts[1:-1])
    object_name = parts[-1] if parts else full_path
    return asset_class, package_name, group_path, object_name, full_path


def _parse_material_memory(path: Path) -> list[_MaterialRecord]:
    data = path.read_bytes()
    records: list[_MaterialRecord] = []
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            raise ValueError(f"trailing bytes at 0x{offset:x}")
        offset += 4  # record_type, always observed as 0

        asset_ref, offset = _read_fstring_at(data, offset)
        asset_class, package_name, group_path, object_name, full_path = _split_asset_ref(
            asset_ref
        )

        if offset + 4 > len(data):
            raise ValueError(f"missing block count at 0x{offset:x}")
        block_count = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if block_count <= 0 or block_count > 128:
            raise ValueError(f"implausible block count {block_count} at 0x{offset:x}")

        blocks: list[_MaterialBlock] = []
        for _ in range(block_count):
            if offset + 16 > len(data):
                raise ValueError(f"short block at 0x{offset:x}")
            _block_flag, offset_a, _offset_b, size = struct.unpack_from(
                "<IIII", data, offset
            )
            offset += 16
            payload = data[offset : offset + size]
            if len(payload) != size:
                raise ValueError(f"short payload at 0x{offset:x}")
            offset += size
            blocks.append(_MaterialBlock(offset=offset_a, size=size, payload=payload))

        records.append(
            _MaterialRecord(
                asset_class=asset_class,
                package_name=package_name,
                group_path=group_path,
                object_name=object_name,
                full_path=full_path,
                blocks=blocks,
            )
        )
    return records


def _index_package_files(asset_root: Path) -> dict[str, Path]:
    packages: dict[str, Path] = {}
    if not asset_root.exists():
        return packages
    for path in asset_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SOURCE_PACKAGE_EXTENSIONS:
            continue
        packages.setdefault(path.stem.lower(), path)
    return packages


def _import_path(pkg: UE2Package, import_index: int) -> list[str]:
    names: list[str] = []
    seen: set[int] = set()
    idx = import_index
    while 0 <= idx < len(pkg.imports) and idx not in seen:
        seen.add(idx)
        imp = pkg.imports[idx]
        name = str(imp.get("object_name") or "")
        if name:
            names.append(name)
        outer = int(imp.get("package", 0) or 0)
        if outer < 0:
            idx = -outer - 1
            continue
        break
    return list(reversed(names))


def import_full_path(imports: list[dict[str, Any]], import_index: int) -> str | None:
    """Return a package-qualified import path from a UE2 import table."""
    names: list[str] = []
    seen: set[int] = set()
    idx = import_index
    while 0 <= idx < len(imports) and idx not in seen:
        seen.add(idx)
        imp = imports[idx]
        name = str(imp.get("object_name") or "")
        if name:
            names.append(name)
        outer = int(imp.get("package", 0) or 0)
        if outer < 0:
            idx = -outer - 1
            continue
        break
    if not names:
        return None
    return ".".join(reversed(names))


def _export_path(pkg: UE2Package, export_index: int) -> list[str]:
    names: list[str] = []
    prefix: list[str] | None = None
    seen: set[int] = set()
    idx = export_index
    while 0 <= idx < len(pkg.exports) and idx not in seen:
        seen.add(idx)
        exp = pkg.exports[idx]
        name = str(exp.get("object_name") or "")
        if name:
            names.append(name)
        outer = int(exp.get("package", 0) or 0)
        if outer > 0:
            idx = outer - 1
            continue
        if outer < 0:
            prefix = _import_path(pkg, -outer - 1)
        break
    if prefix is None:
        prefix = [Path(pkg.filepath).stem]
    return prefix + list(reversed(names))


def _target_from_ref(pkg: UE2Package, value: Any) -> MaterialTarget:
    try:
        ref = int(value)
    except (TypeError, ValueError):
        return MaterialTarget(raw=value)

    if ref > 0:
        idx = ref - 1
        if 0 <= idx < len(pkg.exports):
            exp = pkg.exports[idx]
            path_parts = _export_path(pkg, idx)
            full_path = ".".join(path_parts) if path_parts else None
            return MaterialTarget(
                raw=ref,
                kind="export",
                class_name=exp.get("class_name"),
                object_name=exp.get("object_name"),
                full_path=full_path,
                package_name=path_parts[0] if path_parts else Path(pkg.filepath).stem,
            )
    if ref < 0:
        idx = -ref - 1
        if 0 <= idx < len(pkg.imports):
            imp = pkg.imports[idx]
            path_parts = _import_path(pkg, idx)
            return MaterialTarget(
                raw=ref,
                kind="import",
                class_name=imp.get("class_name"),
                object_name=imp.get("object_name"),
                full_path=".".join(path_parts) if path_parts else None,
                package_name=path_parts[0] if path_parts else None,
            )
    return MaterialTarget(raw=ref)


def _parse_payload_properties(pkg: UE2Package, payload: bytes) -> list[dict[str, Any]]:
    starts = [0]
    try:
        detected = find_property_start(payload, pkg.names)
    except Exception:
        detected = -1
    if detected >= 0:
        starts.append(detected)

    seen: set[int] = set()
    for start in starts:
        if start in seen:
            continue
        seen.add(start)
        try:
            props = parse_properties(payload, pkg.names, start)
        except Exception:
            props = []
        if props:
            return props
    return []


def _target_to_dict(target: MaterialTarget | None) -> dict[str, Any] | None:
    if target is None:
        return None
    return {
        "raw": target.raw,
        "kind": target.kind,
        "class": target.class_name,
        "name": target.object_name,
        "path": target.full_path,
        "package": target.package_name,
    }


def _sanitize_asset_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "texture"


def _target_source_ref(target: MaterialTarget | None) -> str | None:
    if target is None:
        return None
    if target.full_path:
        return target.full_path
    if target.package_name and target.object_name:
        return f"{target.package_name}.{target.object_name}"
    return target.object_name


def _target_asset_name(target: MaterialTarget, prefix: str = "") -> str:
    source_ref = _target_source_ref(target) or target.object_name or "texture"
    stem = _sanitize_asset_stem(source_ref.replace(".", "__"))
    return f"{prefix}{stem}" if prefix else stem


def _output_asset_path(output_dir: str | os.PathLike[str], asset_name: str) -> str:
    png_path = Path(output_dir) / f"{asset_name}.png"
    try:
        return png_path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return png_path.as_posix()


def _empty_texture_asset(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "texture_ref": None,
        "texture_package": None,
        "texture_name": None,
        "asset_name": None,
        "asset_path": None,
    }
    if extra:
        record.update(extra)
    return record


def _unresolved_texture_asset(
    target: MaterialTarget | None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    if target is None:
        return _empty_texture_asset(extra)
    texture_ref = _target_source_ref(target)
    record: dict[str, Any] = {
        "texture_ref": texture_ref,
        "texture_package": target.package_name
        or (
            texture_ref.split(".", 1)[0]
            if texture_ref and "." in texture_ref
            else None
        ),
        "texture_name": target.object_name,
        "asset_name": None,
        "asset_path": None,
        "class_name": target.class_name,
        "source_kind": target.kind,
    }
    if extra:
        record.update(extra)
    return record


def _first_float_property(props: list[dict[str, Any]], name: str) -> float | None:
    for prop in props:
        if prop.get("name") == name:
            return _float_or_none(prop.get("value"))
    return None


def _target_preference_text(*targets: MaterialTarget | None) -> str:
    parts: list[str] = []
    for target in targets:
        if target is None:
            continue
        for value in (
            target.full_path,
            target.package_name,
            target.object_name,
            target.class_name,
        ):
            if value:
                parts.append(str(value))
    return " ".join(parts).lower()


def _combiner_diffuse_score(
    combiner: MaterialTarget,
    candidate: MaterialTarget | None,
    resolved: MaterialTarget | None,
) -> int:
    if candidate is None:
        return -100
    score = 0
    text = _target_preference_text(candidate, resolved)
    combiner_package = (combiner.package_name or "").lower()
    candidate_package = (candidate.package_name or "").lower()
    resolved_package = (resolved.package_name if resolved is not None else "") or ""
    if candidate_package and candidate_package == combiner_package:
        score += 3
    if resolved_package.lower() == combiner_package:
        score += 2
    if resolved is not None:
        score += 1
    if "overlay" in text:
        score -= 4
    if re.search(r"\b(dirt|crud|grime|nasty|stain|smudge|scum)\b", text):
        score -= 3
    if re.search(
        r"\b(metal|bronze|iron|steel|stone|brick|wood|cloth|leather|glass|bone|tile|roof|trim)\b",
        text,
    ):
        score += 2
    return score


def _looks_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return False


class MaterialMemoryResolver:
    """Resolves Shader -> diffuse/normal metadata from MaterialMemory.tab."""

    def __init__(
        self,
        table_path: str | os.PathLike[str] | None = None,
        asset_root: str | os.PathLike[str] | None = None,
    ):
        self.table_path = Path(table_path) if table_path else DEFAULT_TABLE_PATH
        self.asset_root = Path(asset_root) if asset_root else Path(config.ASSETS_PATH)
        self.available = self.table_path.exists()
        self._package_files: dict[str, Path] = {}
        self._packages: dict[str, UE2Package | None] = {}
        self._shaders_by_path: dict[str, ShaderMaterialInfo] = {}
        self._shaders_by_name: dict[str, list[ShaderMaterialInfo]] = {}
        self._normals_by_path: dict[str, NormalMaterialInfo] = {}
        self._normals_by_name: dict[str, list[NormalMaterialInfo]] = {}
        self._speculars_by_path: dict[str, SpecularMaterialInfo] = {}
        self._speculars_by_name: dict[str, list[SpecularMaterialInfo]] = {}
        self._constant_colors_by_path: dict[str, list[float]] = {}
        self._constant_colors_by_name: dict[str, list[tuple[str, list[float]]]] = {}
        self._normal_texture_cache: dict[str, tuple[str, float] | None] = {}
        self._diffuse_texture_cache: dict[str, str | None] = {}
        self._specular_texture_cache: dict[
            str, tuple[str | None, float | None, list[float] | None] | None
        ] = {}
        self._detail_texture_cache: dict[str, str | None] = {}
        self._diffuse_asset_cache: dict[str, dict[str, Any] | None] = {}
        self._normal_asset_cache: dict[
            str, tuple[dict[str, Any] | None, float | None] | None
        ] = {}
        self._specular_asset_cache: dict[
            str,
            tuple[dict[str, Any] | None, float | None, list[float] | None] | None,
        ] = {}
        self._detail_asset_cache: dict[str, dict[str, Any] | None] = {}
        self._target_props_cache: dict[
            str, tuple[UE2Package | None, list[dict[str, Any]]]
        ] = {}
        self._resolved_texture_target_cache: dict[str, MaterialTarget | None] = {}
        self._resolved_bump_target_cache: dict[
            str, tuple[MaterialTarget | None, float | None] | None
        ] = {}

        if self.available:
            self._build_index()

    def _build_index(self) -> None:
        self._package_files = _index_package_files(self.asset_root)
        for record in _parse_material_memory(self.table_path):
            wanted_names = self._record_wanted_property_names(record.asset_class)
            if not wanted_names:
                continue
            pkg = self._package(record.package_name)
            if pkg is None:
                continue
            props = self._record_properties(pkg, record, wanted_names)
            if not props:
                continue
            if record.asset_class in {"Shader", "TintableMaterial"}:
                info = self._parse_shader_record(pkg, record, props)
                self._shaders_by_path[info.full_path.lower()] = info
                self._shaders_by_name.setdefault(info.object_name.lower(), []).append(info)
            elif record.asset_class == "NormalBitmapMaterial":
                info = self._parse_normal_record(pkg, record, props)
                self._normals_by_path[info.full_path.lower()] = info
                self._normals_by_name.setdefault(info.object_name.lower(), []).append(info)
            elif record.asset_class == "SpecularBitmapMaterial":
                info = self._parse_specular_record(pkg, record, props)
                self._speculars_by_path[info.full_path.lower()] = info
                self._speculars_by_name.setdefault(info.object_name.lower(), []).append(info)
            elif record.asset_class == "ConstantColor":
                color_factor = self._parse_constant_color_record(props)
                if color_factor is not None:
                    self._constant_colors_by_path[record.full_path.lower()] = color_factor
                    self._constant_colors_by_name.setdefault(
                        record.object_name.lower(), []
                    ).append((record.package_name.lower(), color_factor))

    def _record_wanted_property_names(self, asset_class: str) -> set[str]:
        if asset_class in {"Shader", "TintableMaterial"}:
            return {
                "Diffuse",
                "Normal",
                "Specular",
                "Opacity",
                "TintAlpha",
                "TintPalette",
                "Detail",
                "DetailScale",
                "TwoSided",
                "OutputBlending",
                "SurfaceType",
            }
        if asset_class == "NormalBitmapMaterial":
            return {"BumpMap", "BumpScale"}
        if asset_class == "SpecularBitmapMaterial":
            return {
                "ExponentMap",
                "DiffuseStrength",
                "SpecularPower",
                "SpecularStrength",
                "SpecularColor",
            }
        if asset_class == "ConstantColor":
            return {"Color"}
        return set()

    def _record_properties(
        self, pkg: UE2Package, record: _MaterialRecord, wanted_names: set[str]
    ) -> list[dict[str, Any]]:
        target = MaterialTarget(
            kind="export",
            class_name=record.asset_class,
            object_name=record.object_name,
            full_path=record.full_path,
            package_name=record.package_name,
        )
        exp = self._find_export(pkg, target)
        if exp is not None:
            try:
                data = pkg.get_export_data(exp)
                props = self._parse_export_properties(pkg, data, wanted_names)
            except Exception:
                props = []
            if any(prop.get("name") in wanted_names for prop in props):
                return props
        return self._first_props(pkg, record.blocks)

    def _package(self, package_name: str | None) -> UE2Package | None:
        if not package_name:
            return None
        key = package_name.lower()
        if key in self._packages:
            return self._packages[key]
        path = self._package_files.get(key)
        if path is None:
            self._packages[key] = None
            return None
        try:
            pkg = UE2Package(str(path))
        except Exception:
            pkg = None
        self._packages[key] = pkg
        return pkg

    def _first_props(
        self, pkg: UE2Package, blocks: list[_MaterialBlock]
    ) -> list[dict[str, Any]]:
        for block in blocks:
            props = _parse_payload_properties(pkg, block.payload)
            if props:
                return props
        return []

    def _parse_shader_record(
        self, pkg: UE2Package, record: _MaterialRecord, props: list[dict[str, Any]]
    ) -> ShaderMaterialInfo:
        info = ShaderMaterialInfo(
            full_path=record.full_path,
            package_name=record.package_name,
            object_name=record.object_name,
            class_name=record.asset_class,
        )
        for prop in props:
            name = str(prop.get("name") or "")
            value = prop.get("value")
            info.properties[name] = value
            target = (
                _target_from_ref(pkg, value)
                if prop.get("type") in {"Object", "Class"}
                else None
            )
            if name == "Diffuse":
                info.diffuse = target
            elif name == "Normal":
                info.normal = target
            elif name == "Specular":
                info.specular = target
            elif name == "Opacity":
                info.opacity = target
            elif name == "TintAlpha":
                info.tint_alpha = target
            elif name == "TintPalette":
                info.tint_palette = target
            elif name == "Detail":
                info.detail = target
            elif name == "DetailScale":
                info.detail_scale = _float_or_none(value)
            elif name == "TwoSided":
                info.two_sided = _looks_truthy(value)
            elif name == "OutputBlending":
                info.output_blending = value
            elif name == "SurfaceType":
                info.surface_type = value
        return info

    def _parse_normal_record(
        self, pkg: UE2Package, record: _MaterialRecord, props: list[dict[str, Any]]
    ) -> NormalMaterialInfo:
        info = NormalMaterialInfo(
            full_path=record.full_path,
            package_name=record.package_name,
            object_name=record.object_name,
        )
        for prop in props:
            name = str(prop.get("name") or "")
            value = prop.get("value")
            info.properties[name] = value
            if name == "BumpMap" and prop.get("type") in {"Object", "Class"}:
                info.bump_map = _target_from_ref(pkg, value)
            elif name == "BumpScale":
                info.bump_scale = _float_or_none(value)
        return info

    def _parse_specular_record(
        self, pkg: UE2Package, record: _MaterialRecord, props: list[dict[str, Any]]
    ) -> SpecularMaterialInfo:
        info = SpecularMaterialInfo(
            full_path=record.full_path,
            package_name=record.package_name,
            object_name=record.object_name,
        )
        for prop in props:
            name = str(prop.get("name") or "")
            value = prop.get("value")
            info.properties[name] = value
            if name == "ExponentMap" and prop.get("type") in {"Object", "Class"}:
                info.exponent_map = _target_from_ref(pkg, value)
            elif name == "DiffuseStrength":
                info.diffuse_strength = _float_or_none(value)
            elif name == "SpecularPower":
                info.specular_power = _float_or_none(value)
            elif name == "SpecularStrength":
                info.specular_strength = _float_or_none(value)
            elif name == "SpecularColor":
                info.specular_color = _color_factor(value)
        return info

    def _parse_constant_color_record(
        self, props: list[dict[str, Any]]
    ) -> list[float] | None:
        for prop in props:
            if prop.get("name") != "Color":
                continue
            factor = _rgba_color_factor(prop.get("value"))
            if factor is not None:
                return factor
        return [0.0, 0.0, 0.0, 1.0]

    def resolve_shader(self, shader_ref: str | None) -> ShaderMaterialInfo | None:
        return self._resolve(
            shader_ref, self._shaders_by_path, self._shaders_by_name
        )

    def resolve_normal(
        self, target: MaterialTarget | None, shader_info: ShaderMaterialInfo | None = None
    ) -> NormalMaterialInfo | None:
        if target is None:
            return None
        normal = None
        if target.full_path:
            normal = self._normals_by_path.get(target.full_path.lower())
        if normal is None and target.object_name:
            candidates = self._normals_by_name.get(target.object_name.lower(), [])
            if shader_info is not None:
                same_package = [
                    candidate
                    for candidate in candidates
                    if candidate.package_name.lower() == shader_info.package_name.lower()
                ]
                if same_package:
                    candidates = same_package
            if len(candidates) == 1:
                normal = candidates[0]
        return normal

    def resolve_specular(
        self, target: MaterialTarget | None, shader_info: ShaderMaterialInfo | None = None
    ) -> SpecularMaterialInfo | None:
        if target is None:
            return None
        specular = None
        if target.full_path:
            specular = self._speculars_by_path.get(target.full_path.lower())
        if specular is None and target.object_name:
            candidates = self._speculars_by_name.get(target.object_name.lower(), [])
            if shader_info is not None:
                same_package = [
                    candidate
                    for candidate in candidates
                    if candidate.package_name.lower() == shader_info.package_name.lower()
                ]
                if same_package:
                    candidates = same_package
            if len(candidates) == 1:
                specular = candidates[0]
        return specular

    def ensure_diffuse_texture(
        self, shader_ref: str | None, output_dir: str | os.PathLike[str]
    ) -> str | None:
        asset = self.ensure_diffuse_asset(shader_ref, output_dir)
        return str(asset["asset_name"]) if asset and asset.get("asset_name") else None

    def ensure_diffuse_asset(
        self, shader_ref: str | None, output_dir: str | os.PathLike[str]
    ) -> dict[str, Any] | None:
        shader_info = self.resolve_shader(shader_ref)
        if shader_info is None or shader_info.diffuse is None:
            return None
        cache_key = shader_info.full_path.lower()
        if cache_key in self._diffuse_asset_cache:
            return self._diffuse_asset_cache[cache_key]
        texture_target = self._resolve_texture_target(shader_info.diffuse)
        asset = (
            self._ensure_texture_asset(texture_target, output_dir)
            if texture_target is not None
            else None
        )
        self._diffuse_asset_cache[cache_key] = asset
        self._diffuse_texture_cache[cache_key] = (
            str(asset["asset_name"]) if asset and asset.get("asset_name") else None
        )
        return asset

    def base_color_factor(self, shader_ref: str | None) -> list[float] | None:
        shader_info = self.resolve_shader(shader_ref)
        if shader_info is None:
            return None
        return self._resolve_color_factor(shader_info.diffuse)

    def ensure_normal_texture(
        self, shader_ref: str | None, output_dir: str | os.PathLike[str]
    ) -> tuple[str | None, float | None]:
        asset, scale = self.ensure_normal_asset(shader_ref, output_dir)
        return (
            str(asset["asset_name"]) if asset and asset.get("asset_name") else None,
            scale,
        )

    def ensure_normal_asset(
        self, shader_ref: str | None, output_dir: str | os.PathLike[str]
    ) -> tuple[dict[str, Any] | None, float | None]:
        shader_info = self.resolve_shader(shader_ref)
        if shader_info is None:
            return None, None
        cache_key = shader_info.full_path.lower()
        if cache_key in self._normal_asset_cache:
            cached = self._normal_asset_cache[cache_key]
            return cached if cached is not None else (None, None)

        normal_info = self.resolve_normal(shader_info.normal, shader_info)
        bump_map = normal_info.bump_map if normal_info is not None else None
        bump_scale = normal_info.bump_scale if normal_info is not None else None
        if bump_map is None:
            bump_map, bump_scale = self._resolve_bump_texture_target(
                shader_info.normal, shader_info
            )
        if bump_map is None:
            self._normal_asset_cache[cache_key] = None
            self._normal_texture_cache[cache_key] = None
            return None, None

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        normal_name = _target_asset_name(bump_map, prefix="normal__")
        normal_file = output_path / f"{normal_name}.png"
        strength = _normal_strength(bump_scale)
        if not normal_file.exists():
            image = self._load_texture_image(bump_map)
            if image is None:
                self._normal_asset_cache[cache_key] = None
                self._normal_texture_cache[cache_key] = None
                return None, None
            _write_height_to_normal_png(image, normal_file, strength)

        asset = self._texture_asset_record(bump_map, output_dir, normal_name)
        if asset is not None:
            asset["scale"] = 1.0
            asset["source_kind"] = "generated_normal"
        self._normal_texture_cache[cache_key] = (normal_name, 1.0)
        self._normal_asset_cache[cache_key] = (asset, 1.0)
        return asset, 1.0

    def ensure_specular_texture(
        self, shader_ref: str | None, output_dir: str | os.PathLike[str]
    ) -> tuple[str | None, float | None, list[float] | None]:
        asset, factor, color = self.ensure_specular_asset(shader_ref, output_dir)
        return (
            str(asset["asset_name"]) if asset and asset.get("asset_name") else None,
            factor,
            color,
        )

    def ensure_specular_asset(
        self, shader_ref: str | None, output_dir: str | os.PathLike[str]
    ) -> tuple[dict[str, Any] | None, float | None, list[float] | None]:
        shader_info = self.resolve_shader(shader_ref)
        if shader_info is None:
            return None, None, None
        cache_key = shader_info.full_path.lower()
        if cache_key in self._specular_asset_cache:
            cached = self._specular_asset_cache[cache_key]
            return cached if cached is not None else (None, None, None)

        specular_info = self.resolve_specular(shader_info.specular, shader_info)
        if specular_info is None:
            self._specular_asset_cache[cache_key] = None
            self._specular_texture_cache[cache_key] = None
            return None, None, None

        asset = None
        if specular_info.exponent_map is not None:
            texture_target = self._resolve_texture_target(specular_info.exponent_map)
            asset = (
                self._ensure_texture_asset(texture_target, output_dir)
                if texture_target is not None
                else None
            )
        factor = _specular_factor(specular_info.specular_strength)
        color = specular_info.specular_color
        result = (asset, factor, color)
        self._specular_asset_cache[cache_key] = result
        self._specular_texture_cache[cache_key] = (
            str(asset["asset_name"]) if asset and asset.get("asset_name") else None,
            factor,
            color,
        )
        return result

    def ensure_detail_texture(
        self, shader_ref: str | None, output_dir: str | os.PathLike[str]
    ) -> str | None:
        asset = self.ensure_detail_asset(shader_ref, output_dir)
        return str(asset["asset_name"]) if asset and asset.get("asset_name") else None

    def ensure_detail_asset(
        self, shader_ref: str | None, output_dir: str | os.PathLike[str]
    ) -> dict[str, Any] | None:
        shader_info = self.resolve_shader(shader_ref)
        if shader_info is None or shader_info.detail is None:
            return None
        cache_key = shader_info.full_path.lower()
        if cache_key in self._detail_asset_cache:
            return self._detail_asset_cache[cache_key]
        texture_target = self._resolve_texture_target(shader_info.detail)
        asset = (
            self._ensure_texture_asset(texture_target, output_dir)
            if texture_target is not None
            else None
        )
        self._detail_asset_cache[cache_key] = asset
        self._detail_texture_cache[cache_key] = (
            str(asset["asset_name"]) if asset and asset.get("asset_name") else None
        )
        return asset

    def shader_extras(self, shader_ref: str | None) -> dict[str, Any]:
        shader_info = self.resolve_shader(shader_ref)
        if shader_info is None:
            return {}
        extras: dict[str, Any] = {
            "vg_material_memory_shader": shader_info.full_path,
            "vg_source_material_ref": shader_info.full_path,
            "vg_source_package": shader_info.package_name,
        }
        if shader_info.surface_type is not None:
            extras["vg_surface_type"] = shader_info.surface_type
        if shader_info.detail is not None:
            extras["vg_detail"] = _target_to_dict(shader_info.detail)
        if shader_info.specular is not None:
            extras["vg_specular"] = _target_to_dict(shader_info.specular)
        if shader_info.opacity is not None:
            extras["vg_opacity"] = _target_to_dict(shader_info.opacity)
        if shader_info.detail_scale is not None:
            extras["vg_detail_scale"] = shader_info.detail_scale
        if shader_info.output_blending is not None:
            extras["vg_output_blending"] = shader_info.output_blending
        return extras

    def iter_shaders(self) -> list[ShaderMaterialInfo]:
        return sorted(
            self._shaders_by_path.values(), key=lambda info: info.full_path.lower()
        )

    def material_manifest_entry(
        self,
        shader_ref: str | None,
        output_dir: str | os.PathLike[str],
    ) -> dict[str, Any] | None:
        shader_info = self.resolve_shader(shader_ref)
        if shader_info is None:
            return None

        diffuse_target = self._resolve_texture_target(shader_info.diffuse)
        normal_target, _normal_bump_scale = self._normal_texture_target(shader_info)
        specular_info = self.resolve_specular(shader_info.specular, shader_info)
        specular_target = (
            self._resolve_texture_target(specular_info.exponent_map)
            if specular_info is not None and specular_info.exponent_map is not None
            else None
        )
        detail_target = self._resolve_texture_target(shader_info.detail)
        tint_alpha_target = self._resolve_texture_target(shader_info.tint_alpha)
        tint_palette_target = self._resolve_texture_target(shader_info.tint_palette)

        base_color = self.ensure_diffuse_asset(shader_info.full_path, output_dir)
        base_color_factor = self._resolve_color_factor(shader_info.diffuse)
        normal_asset, normal_scale = self.ensure_normal_asset(
            shader_info.full_path, output_dir
        )
        specular_asset, specular_factor, specular_color = self.ensure_specular_asset(
            shader_info.full_path, output_dir
        )
        detail_asset = self.ensure_detail_asset(shader_info.full_path, output_dir)
        tint_alpha_asset = (
            self._ensure_texture_asset(tint_alpha_target, output_dir)
            if tint_alpha_target is not None
            else None
        )
        tint_palette_asset = (
            self._ensure_texture_asset(tint_palette_target, output_dir)
            if tint_palette_target is not None
            else None
        )

        normal = _unresolved_texture_asset(normal_target, {"scale": normal_scale})
        if normal_asset:
            normal.update(normal_asset)
            normal["scale"] = normal_scale

        specular = _empty_texture_asset(
            {
                "factor": specular_factor if specular_factor is not None else 0.0,
                "color_factor": specular_color,
            }
        )
        if specular_asset:
            specular.update(specular_asset)
            specular["factor"] = specular_factor if specular_factor is not None else 0.0
            specular["color_factor"] = specular_color
        elif specular_target is not None:
            specular.update(_unresolved_texture_asset(specular_target))

        detail = _unresolved_texture_asset(
            detail_target, {"scale": shader_info.detail_scale}
        )
        if detail_asset:
            detail.update(detail_asset)
            detail["scale"] = shader_info.detail_scale

        alpha_mode = "MASK" if shader_info.alpha_mode == "mask" else "OPAQUE"
        return {
            "source_package": shader_info.package_name,
            "source_ref": shader_info.full_path,
            "class_name": shader_info.class_name,
            "base_color": base_color
            or _unresolved_texture_asset(
                diffuse_target, {"color_factor": base_color_factor}
            ),
            "normal": normal,
            "specular": specular,
            "detail": detail,
            "tint_alpha": tint_alpha_asset
            or _unresolved_texture_asset(tint_alpha_target),
            "tint_palette": tint_palette_asset
            or _unresolved_texture_asset(tint_palette_target),
            "alpha_mode": alpha_mode,
            "alpha_cutoff": 0.01 if alpha_mode == "MASK" else None,
            "two_sided": bool(shader_info.two_sided),
            "surface_type": shader_info.surface_type,
            "output_blending": shader_info.output_blending,
            "graph": {
                "diffuse_chain": self._endpoint_chain(shader_info.diffuse, diffuse_target),
                "normal_chain": self._endpoint_chain(shader_info.normal, normal_target),
                "detail_chain": self._endpoint_chain(shader_info.detail, detail_target),
                "specular_chain": self._endpoint_chain(
                    shader_info.specular, specular_target
                ),
            },
        }

    def build_material_manifest(
        self,
        output_dir: str | os.PathLike[str],
        shader_refs: list[str] | None = None,
        package_names: set[str] | None = None,
    ) -> dict[str, Any]:
        if shader_refs is not None:
            shader_infos = [
                info
                for ref in shader_refs
                if (info := self.resolve_shader(ref)) is not None
            ]
        else:
            shader_infos = self.iter_shaders()
        if package_names:
            package_keys = {name.lower() for name in package_names}
            shader_infos = [
                info
                for info in shader_infos
                if info.package_name.lower() in package_keys
            ]

        manifest: dict[str, Any] = {}
        seen: set[str] = set()
        for shader_info in shader_infos:
            key = shader_info.full_path
            lower_key = key.lower()
            if lower_key in seen:
                continue
            seen.add(lower_key)
            entry = self.material_manifest_entry(key, output_dir)
            if entry is not None:
                manifest[key] = entry
        return manifest

    def validate_material_manifest(
        self,
        manifest: dict[str, Any],
        root_dir: str | os.PathLike[str] | None = None,
        legacy_shader_map: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        root = Path(root_dir) if root_dir is not None else REPO_ROOT
        asset_names: dict[str, tuple[str | None, str | None]] = {}
        legacy_lookup = self._legacy_shader_lookup(legacy_shader_map)

        for material_ref, entry in manifest.items():
            base_color = entry.get("base_color") or {}
            if not base_color.get("asset_path") and not base_color.get("color_factor"):
                issues.append(
                    {
                        "severity": "error",
                        "code": "missing_renderable_base_color",
                        "material_ref": material_ref,
                    }
                )

            legacy_entry, legacy_key = self._legacy_shader_entry(
                legacy_lookup, material_ref
            )
            legacy_texture = self._legacy_shader_texture_name(legacy_entry)
            if legacy_texture and not base_color.get("asset_path"):
                issues.append(
                    {
                        "severity": "error",
                        "code": "previous_texture_missing",
                        "material_ref": material_ref,
                        "legacy_key": legacy_key,
                        "legacy_texture": legacy_texture,
                    }
                )

            for channel in ("base_color", "normal", "specular", "detail"):
                record = entry.get(channel) or {}
                asset_name = record.get("asset_name")
                asset_path = record.get("asset_path")
                texture_ref = record.get("texture_ref")
                texture_package = record.get("texture_package")

                if (
                    texture_ref
                    and texture_package
                    and self._package(texture_package) is None
                ):
                    issues.append(
                        {
                            "severity": "error",
                            "code": "unopenable_texture_package",
                            "material_ref": material_ref,
                            "channel": channel,
                            "texture_ref": texture_ref,
                            "texture_package": texture_package,
                        }
                    )

                if asset_name:
                    prior = asset_names.get(str(asset_name))
                    current = (texture_ref, asset_path)
                    if prior is not None and prior != current:
                        issues.append(
                            {
                                "severity": "error",
                                "code": "duplicate_asset_name",
                                "material_ref": material_ref,
                                "channel": channel,
                                "asset_name": asset_name,
                                "first_texture_ref": prior[0],
                                "texture_ref": texture_ref,
                            }
                        )
                    else:
                        asset_names[str(asset_name)] = current

                if asset_path:
                    path = Path(asset_path)
                    if not path.is_absolute():
                        path = root / path
                    if not path.exists():
                        issues.append(
                            {
                                "severity": "error",
                                "code": "missing_asset_path",
                                "material_ref": material_ref,
                                "channel": channel,
                                "asset_path": asset_path,
                            }
                        )
                elif texture_ref:
                    issues.append(
                        {
                            "severity": "error",
                            "code": "unresolved_texture_ref",
                            "material_ref": material_ref,
                            "channel": channel,
                            "texture_ref": texture_ref,
                        }
                    )

        return issues

    @staticmethod
    def _legacy_shader_lookup(
        legacy_shader_map: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not legacy_shader_map:
            return {}
        return {str(key).lower(): value for key, value in legacy_shader_map.items()}

    @staticmethod
    def _legacy_shader_entry(
        legacy_lookup: dict[str, Any], material_ref: str
    ) -> tuple[Any | None, str | None]:
        if not legacy_lookup or not material_ref:
            return None, None
        full_key = str(material_ref).lower()
        for key in (full_key, full_key.rsplit(".", 1)[-1]):
            if key in legacy_lookup:
                return legacy_lookup[key], key
        return None, None

    @staticmethod
    def _legacy_shader_texture_name(entry: Any) -> str | None:
        if isinstance(entry, dict):
            texture_name = entry.get("texture") or entry.get("asset_name")
        elif isinstance(entry, str):
            texture_name = entry
        else:
            return None
        if not texture_name:
            return None
        texture_text = str(texture_name)
        if texture_text.startswith("color:"):
            return None
        return texture_text

    def _endpoint_chain(
        self, source: MaterialTarget | None, resolved: MaterialTarget | None
    ) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        if source is not None:
            source_dict = _target_to_dict(source)
            if source_dict is not None:
                chain.append(source_dict)
        include_resolved = resolved is not None
        if include_resolved and source is not None:
            include_resolved = self._target_cache_key(resolved) != self._target_cache_key(
                source
            )
        if include_resolved:
            resolved_dict = _target_to_dict(resolved)
            if resolved_dict is not None:
                chain.append(resolved_dict)
        return chain

    def _normal_texture_target(
        self, shader_info: ShaderMaterialInfo
    ) -> tuple[MaterialTarget | None, float | None]:
        normal_info = self.resolve_normal(shader_info.normal, shader_info)
        bump_map = normal_info.bump_map if normal_info is not None else None
        bump_scale = normal_info.bump_scale if normal_info is not None else None
        if bump_map is None:
            bump_map, bump_scale = self._resolve_bump_texture_target(
                shader_info.normal, shader_info
            )
        return bump_map, bump_scale

    def _resolve(
        self,
        shader_ref: str | None,
        by_path: dict[str, Any],
        by_name: dict[str, list[Any]],
    ) -> Any | None:
        if not shader_ref:
            return None
        key = shader_ref.lower()
        if key in by_path:
            return by_path[key]
        object_name = key.rsplit(".", 1)[-1]
        candidates = by_name.get(object_name, [])
        if len(candidates) == 1:
            return candidates[0]
        if "." in key:
            package_name = key.split(".", 1)[0]
            same_package = [
                candidate
                for candidate in candidates
                if getattr(candidate, "package_name", "").lower() == package_name
            ]
            if len(same_package) == 1:
                return same_package[0]
        return None

    def _target_package(self, target: MaterialTarget) -> UE2Package | None:
        if target.package_name:
            pkg = self._package(target.package_name)
            if pkg is not None:
                return pkg
        if target.full_path:
            package_name = target.full_path.split(".", 1)[0]
            pkg = self._package(package_name)
            if pkg is not None:
                return pkg
        return None

    def _target_cache_key(self, target: MaterialTarget) -> str:
        return "|".join(
            str(part or "")
            for part in (
                target.kind,
                target.package_name,
                target.full_path,
                target.class_name,
                target.object_name,
                target.raw,
            )
        )

    def _find_export(
        self, pkg: UE2Package, target: MaterialTarget
    ) -> dict[str, Any] | None:
        try:
            raw_ref = int(target.raw)
        except (TypeError, ValueError):
            raw_ref = 0
        if raw_ref > 0:
            idx = raw_ref - 1
            if 0 <= idx < len(pkg.exports):
                exp = pkg.exports[idx]
                if (
                    not target.object_name
                    or str(exp.get("object_name", "")).lower()
                    == target.object_name.lower()
                ):
                    return exp

        object_name = (target.object_name or "").lower()
        class_name = (target.class_name or "").lower()
        if not object_name:
            return None
        for exp in pkg.exports:
            if str(exp.get("object_name", "")).lower() != object_name:
                continue
            if class_name and str(exp.get("class_name", "")).lower() != class_name:
                continue
            return exp
        return None

    def _parse_export_properties(
        self,
        pkg: UE2Package,
        data: bytes,
        wanted_names: set[str],
    ) -> list[dict[str, Any]]:
        starts: list[int] = [0]
        try:
            detected = find_property_start(data, pkg.names)
        except Exception:
            detected = -1
        if detected >= 0:
            starts.append(detected)
        starts.extend(range(min(len(data), 256)))

        best_props: list[dict[str, Any]] = []
        best_score = -1
        seen: set[int] = set()
        for start in starts:
            if start in seen:
                continue
            seen.add(start)
            try:
                props = parse_properties(data, pkg.names, start)
            except Exception:
                continue
            if not props:
                continue
            names = [str(prop.get("name") or "") for prop in props]
            score = 0
            for prop, name in zip(props, names):
                if name not in wanted_names:
                    continue
                score += 60 if prop.get("type") in {"Object", "Class"} else 8
            score += sum(1 for prop in props if prop.get("value") is not None)
            if start == 0:
                score += 1
            if score > best_score:
                best_score = score
                best_props = props
        return best_props

    def _target_properties(
        self,
        target: MaterialTarget,
        wanted_names: set[str],
    ) -> tuple[UE2Package | None, list[dict[str, Any]]]:
        key = self._target_cache_key(target) + "||" + ",".join(sorted(wanted_names))
        if key in self._target_props_cache:
            return self._target_props_cache[key]

        pkg = self._target_package(target)
        if pkg is None:
            result = (None, [])
            self._target_props_cache[key] = result
            return result
        exp = self._find_export(pkg, target)
        if exp is None:
            result = (pkg, [])
            self._target_props_cache[key] = result
            return result
        try:
            data = pkg.get_export_data(exp)
            props = self._parse_export_properties(pkg, data, wanted_names)
        except Exception:
            props = []
        result = (pkg, props)
        self._target_props_cache[key] = result
        return result

    def _property_target(
        self, pkg: UE2Package, prop: dict[str, Any]
    ) -> MaterialTarget | None:
        if prop.get("type") not in {"Object", "Class"}:
            return None
        return _target_from_ref(pkg, prop.get("value"))

    def _property_targets_by_name(
        self,
        pkg: UE2Package,
        props: list[dict[str, Any]],
        names: tuple[str, ...],
    ) -> list[MaterialTarget]:
        targets: list[MaterialTarget] = []
        for name in names:
            for prop in props:
                if prop.get("name") != name:
                    continue
                target = self._property_target(pkg, prop)
                if target is not None:
                    targets.append(target)
        return targets

    def _combiner_property_targets(
        self, pkg: UE2Package, props: list[dict[str, Any]]
    ) -> list[MaterialTarget]:
        targets: list[MaterialTarget] = []
        seen: set[str] = set()
        for name in ("Material1", "Material2", "Material"):
            for prop in props:
                if prop.get("name") != name:
                    continue
                target = self._property_target(pkg, prop)
                if target is None:
                    continue
                key = self._target_cache_key(target)
                if key in seen:
                    continue
                seen.add(key)
                targets.append(target)
        return targets

    def _ordered_combiner_texture_targets(
        self,
        combiner: MaterialTarget,
        pkg: UE2Package,
        props: list[dict[str, Any]],
        visited: set[str],
        depth: int,
    ) -> list[MaterialTarget]:
        ranked: list[tuple[int, int, MaterialTarget]] = []
        for index, candidate in enumerate(self._combiner_property_targets(pkg, props)):
            resolved = self._resolve_texture_target(candidate, set(visited), depth + 1)
            score = _combiner_diffuse_score(combiner, candidate, resolved)
            ranked.append((score, -index, candidate))
        ranked.sort(reverse=True)
        return [candidate for _score, _order, candidate in ranked]

    def _resolve_texture_target(
        self,
        target: MaterialTarget | None,
        visited: set[str] | None = None,
        depth: int = 0,
    ) -> MaterialTarget | None:
        if target is None or depth > 12:
            return None
        key = self._target_cache_key(target)
        if key in self._resolved_texture_target_cache:
            return self._resolved_texture_target_cache[key]
        if visited is None:
            visited = set()
        if key in visited:
            return None
        visited.add(key)

        class_name = (target.class_name or "").lower()
        if class_name == "texture":
            self._resolved_texture_target_cache[key] = target
            return target

        wanted = {
            "Diffuse",
            "Material",
            "Material1",
            "Material2",
            "Texture",
            "BumpMap",
            "ExponentMap",
            "OceanGradient",
            "SurfaceMap",
        }
        pkg, props = self._target_properties(target, wanted)
        if pkg is None or not props:
            self._resolved_texture_target_cache[key] = None
            return None

        if class_name == "combiner":
            for next_target in self._ordered_combiner_texture_targets(
                target, pkg, props, visited, depth
            ):
                resolved = self._resolve_texture_target(
                    next_target, set(visited), depth + 1
                )
                if resolved is not None:
                    self._resolved_texture_target_cache[key] = resolved
                    return resolved
            self._resolved_texture_target_cache[key] = None
            return None

        if class_name == "shader":
            field_order = ("Diffuse", "Material")
        elif class_name in {
            "texscaler",
            "texpanner",
            "texrotator",
            "texoscillator",
            "texcoordsource",
            "texenvmap",
            "tintablematerial",
        }:
            field_order = ("Material", "Diffuse")
        elif class_name == "normalbitmapmaterial":
            field_order = ("BumpMap", "Material", "Diffuse")
        elif class_name == "specularbitmapmaterial":
            field_order = ("ExponentMap", "Material", "Diffuse")
        elif class_name == "watershadermaterial":
            field_order = ("OceanGradient", "SurfaceMap", "Material", "Diffuse")
        else:
            field_order = (
                "Material",
                "Diffuse",
                "Material1",
                "Material2",
                "Texture",
                "BumpMap",
                "ExponentMap",
                "OceanGradient",
                "SurfaceMap",
            )

        for next_target in self._property_targets_by_name(pkg, props, field_order):
            resolved = self._resolve_texture_target(
                next_target, set(visited), depth + 1
            )
            if resolved is not None:
                self._resolved_texture_target_cache[key] = resolved
                return resolved

        self._resolved_texture_target_cache[key] = None
        return None

    def _constant_color_factor(
        self, target: MaterialTarget | None
    ) -> list[float] | None:
        if target is None or (target.class_name or "").lower() != "constantcolor":
            return None
        if target.full_path:
            factor = self._constant_colors_by_path.get(target.full_path.lower())
            if factor is not None:
                return factor
        if target.object_name:
            candidates = self._constant_colors_by_name.get(
                target.object_name.lower(), []
            )
            if target.package_name:
                same_package = [
                    factor
                    for package_name, factor in candidates
                    if package_name == target.package_name.lower()
                ]
                if same_package:
                    return same_package[0]
            if len(candidates) == 1:
                return candidates[0][1]
        pkg, props = self._target_properties(target, {"Color"})
        if pkg is None:
            return None
        if not props:
            return [0.0, 0.0, 0.0, 1.0]
        for prop in props:
            if prop.get("name") != "Color":
                continue
            factor = _rgba_color_factor(prop.get("value"))
            if factor is not None:
                return factor
        return None

    def _resolve_color_factor(
        self,
        target: MaterialTarget | None,
        visited: set[str] | None = None,
        depth: int = 0,
    ) -> list[float] | None:
        if target is None or depth > 12:
            return None
        key = self._target_cache_key(target)
        if visited is None:
            visited = set()
        if key in visited:
            return None
        visited.add(key)

        class_name = (target.class_name or "").lower()
        if class_name == "constantcolor":
            return _opaque_base_color_factor(self._constant_color_factor(target))

        wanted = {
            "Color",
            "Diffuse",
            "Material",
            "Material1",
            "Material2",
            "WaterColor",
        }
        pkg, props = self._target_properties(target, wanted)
        if pkg is None or not props:
            return None

        for color_name in ("WaterColor", "Color"):
            for prop in props:
                if prop.get("name") != color_name:
                    continue
                factor = _rgba_color_factor(prop.get("value"))
                if factor is not None:
                    return _opaque_base_color_factor(factor)

        if class_name == "shader":
            field_order = ("Diffuse", "Material")
        elif class_name == "combiner":
            field_order = ("Material1", "Material2", "Material")
        elif class_name in {
            "texscaler",
            "texpanner",
            "texrotator",
            "texoscillator",
            "texcoordsource",
            "texenvmap",
            "tintablematerial",
        }:
            field_order = ("Material", "Diffuse")
        else:
            field_order = ("Material", "Diffuse", "Material1", "Material2")

        for next_target in self._property_targets_by_name(pkg, props, field_order):
            factor = self._resolve_color_factor(next_target, set(visited), depth + 1)
            if factor is not None:
                return _opaque_base_color_factor(factor)
        return None

    def _resolve_bump_texture_target(
        self,
        target: MaterialTarget | None,
        shader_info: ShaderMaterialInfo | None = None,
        visited: set[str] | None = None,
        depth: int = 0,
    ) -> tuple[MaterialTarget | None, float | None]:
        if target is None or depth > 12:
            return None, None
        key = self._target_cache_key(target)
        if key in self._resolved_bump_target_cache:
            cached = self._resolved_bump_target_cache[key]
            return cached if cached is not None else (None, None)
        if visited is None:
            visited = set()
        if key in visited:
            return None, None
        visited.add(key)

        class_name = (target.class_name or "").lower()
        if class_name == "texture":
            result = (target, None)
            self._resolved_bump_target_cache[key] = result
            return result

        if class_name == "normalbitmapmaterial":
            normal_info = self.resolve_normal(target, shader_info)
            if normal_info is not None and normal_info.bump_map is not None:
                result = (normal_info.bump_map, normal_info.bump_scale)
                self._resolved_bump_target_cache[key] = result
                return result

        wanted = {"BumpMap", "Material", "Material1", "Material2", "Diffuse"}
        pkg, props = self._target_properties(target, wanted)
        if pkg is None or not props:
            self._resolved_bump_target_cache[key] = None
            return None, None

        if class_name == "normalbitmapmaterial":
            bump_targets = self._property_targets_by_name(pkg, props, ("BumpMap",))
            bump_scale = _first_float_property(props, "BumpScale")
            if bump_targets:
                bump_target = (
                    bump_targets[0]
                    if (bump_targets[0].class_name or "").lower() == "texture"
                    else self._resolve_texture_target(
                        bump_targets[0], set(visited), depth + 1
                    )
                )
                result = (bump_target, bump_scale)
                self._resolved_bump_target_cache[key] = result
                return result

        if class_name == "combiner":
            field_order = ("Material1", "Material2", "Material")
        elif class_name in {
            "texscaler",
            "texpanner",
            "texrotator",
            "texoscillator",
            "texcoordsource",
            "texenvmap",
        }:
            field_order = ("Material", "Diffuse")
        else:
            field_order = ("Material", "Diffuse", "Material1", "Material2")

        for next_target in self._property_targets_by_name(pkg, props, field_order):
            bump_target, bump_scale = self._resolve_bump_texture_target(
                next_target, shader_info, set(visited), depth + 1
            )
            if bump_target is not None:
                result = (bump_target, bump_scale)
                self._resolved_bump_target_cache[key] = result
                return result

        self._resolved_bump_target_cache[key] = None
        return None, None

    def _find_texture_export(
        self, pkg: UE2Package, target: MaterialTarget
    ) -> dict[str, Any] | None:
        object_name = (target.object_name or "").lower()
        if not object_name:
            return None
        for exp in pkg.exports:
            if (
                exp.get("class_name") == "Texture"
                and str(exp.get("object_name", "")).lower() == object_name
            ):
                return exp
        return None

    def _load_texture_image(self, target: MaterialTarget) -> Image.Image | None:
        pkg = self._target_package(target)
        if pkg is None:
            return None
        exp = self._find_texture_export(pkg, target)
        if exp is None:
            return None
        try:
            palette = self._load_palette_for_texture(pkg, exp)
            tex = UE2Texture(pkg.get_export_data(exp), pkg.names, palette=palette)
            if not tex.mips:
                return None
            image = tex.get_image(0)
            if image is None and tex.format_id == 0 and tex.mips:
                mip = tex.mips[0]
                expected = mip.width * mip.height
                if len(mip.data) >= expected:
                    image = Image.frombytes(
                        "L", (mip.width, mip.height), mip.data[:expected]
                    )
            return image.convert("RGBA") if image is not None else None
        except Exception:
            return None

    def _load_palette_for_texture(
        self, pkg: UE2Package, texture_export: dict[str, Any]
    ) -> list[tuple[int, int, int, int]] | None:
        try:
            data = pkg.get_export_data(texture_export)
            tex_obj = UE2Texture(data, pkg.names)
            if tex_obj.format_id != 0:
                return None
            palette_ref = tex_obj.properties.get("Palette")
            if palette_ref is None:
                return None
            palette_target = _target_from_ref(pkg, palette_ref)
            palette_pkg = self._target_package(palette_target) or pkg
            palette_export = self._find_export(palette_pkg, palette_target)
            if palette_export is None or palette_export.get("class_name") != "Palette":
                return None
            return _decode_palette_export(palette_pkg, palette_export)
        except Exception:
            return None

    def _texture_asset_record(
        self,
        target: MaterialTarget,
        output_dir: str | os.PathLike[str],
        asset_name: str,
    ) -> dict[str, Any] | None:
        texture_ref = _target_source_ref(target)
        return {
            "texture_ref": texture_ref,
            "texture_package": target.package_name
            or (
                texture_ref.split(".", 1)[0]
                if texture_ref and "." in texture_ref
                else None
            ),
            "texture_name": target.object_name,
            "asset_name": asset_name,
            "asset_path": _output_asset_path(output_dir, asset_name),
            "class_name": target.class_name,
            "source_kind": target.kind,
        }

    def _ensure_texture_asset(
        self,
        target: MaterialTarget,
        output_dir: str | os.PathLike[str],
        prefix: str = "",
    ) -> dict[str, Any] | None:
        if not target.object_name:
            return None
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        asset_name = _target_asset_name(target, prefix=prefix)
        png_path = output_path / f"{asset_name}.png"
        if _is_valid_png(png_path):
            return self._texture_asset_record(target, output_dir, asset_name)
        image = self._load_texture_image(target)
        if image is None:
            return None
        _publish_valid_png(image, png_path, replace_invalid=png_path.exists())
        return self._texture_asset_record(target, output_dir, asset_name)

    def _ensure_texture_png(
        self, target: MaterialTarget, output_dir: str | os.PathLike[str]
    ) -> str | None:
        asset = self._ensure_texture_asset(target, output_dir)
        return str(asset["asset_name"]) if asset and asset.get("asset_name") else None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _color_factor(value: Any) -> list[float] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    channels = []
    for key in ("r", "g", "b"):
        channel = value.get(key, value.get(key.upper()))
        try:
            channels.append(max(0.0, min(1.0, float(channel) / 255.0)))
        except (TypeError, ValueError):
            return None
    return channels


def _rgba_color_factor(value: Any) -> list[float] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    channels = []
    for key, fallback in (("r", 0), ("g", 0), ("b", 0), ("a", 255)):
        channel = value.get(key, value.get(key.upper(), fallback))
        try:
            channels.append(max(0.0, min(1.0, float(channel) / 255.0)))
        except (TypeError, ValueError):
            return None
    return channels


def _opaque_base_color_factor(value: list[float] | None) -> list[float] | None:
    if value is None:
        return None
    factor = list(value)
    if len(factor) == 3:
        factor.append(1.0)
    elif len(factor) >= 4:
        factor[3] = 1.0
    return factor[:4] if len(factor) >= 4 else None


def _specular_factor(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _normal_strength(bump_scale: float | None) -> float:
    if bump_scale is None:
        return 1.0
    return max(0.35, min(4.0, float(bump_scale) / 4.0))


def _is_valid_png(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                return False
            image.verify()
        return True
    except (OSError, ValueError):
        return False


def _publish_valid_png(
    image: Image.Image, output_path: Path, *, replace_invalid: bool = False
) -> None:
    """Atomically publish a verified PNG, replacing only a corrupt old file."""
    if output_path.exists() and not replace_invalid:
        return
    tmp_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    image.save(tmp_path, format="PNG")
    if not _is_valid_png(tmp_path):
        tmp_path.unlink(missing_ok=True)
        raise ValueError(f"failed to encode a valid PNG: {output_path}")
    try:
        if replace_invalid:
            os.replace(tmp_path, output_path)
        else:
            try:
                os.link(tmp_path, output_path)
            except FileExistsError:
                pass
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _save_png_if_missing(image: Image.Image, output_path: Path) -> None:
    """Publish a valid PNG without clobbering another worker's valid output."""
    _publish_valid_png(
        image,
        output_path,
        replace_invalid=output_path.exists() and not _is_valid_png(output_path),
    )


def _write_height_to_normal_png(
    image: Image.Image, output_path: Path, strength: float
) -> None:
    import numpy as np

    height = np.asarray(image.convert("L"), dtype=np.float32) / 255.0
    dx = np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)
    dy = np.roll(height, -1, axis=0) - np.roll(height, 1, axis=0)
    nx = -dx * strength
    ny = -dy * strength
    nz = np.ones_like(height, dtype=np.float32)
    length = np.sqrt(nx * nx + ny * ny + nz * nz)
    normal = np.stack((nx / length, ny / length, nz / length), axis=2)
    rgb = ((normal * 0.5 + 0.5) * 255.0).clip(0, 255).astype(np.uint8)
    _save_png_if_missing(Image.fromarray(rgb, "RGB"), output_path)


def _decode_palette_export(
    pkg: UE2Package, palette_export: dict[str, Any]
) -> list[tuple[int, int, int, int]] | None:
    try:
        pal_data = pkg.get_export_data(palette_export)
        if len(pal_data) < 5:
            return None
        count = struct.unpack("<i", pal_data[1:5])[0]
        if count != 256 or len(pal_data) < 5 + 256 * 4:
            return None
        palette = []
        for i in range(256):
            off = 5 + i * 4
            palette.append(
                (
                    pal_data[off],
                    pal_data[off + 1],
                    pal_data[off + 2],
                    pal_data[off + 3],
                )
            )
        return palette
    except Exception:
        return None
