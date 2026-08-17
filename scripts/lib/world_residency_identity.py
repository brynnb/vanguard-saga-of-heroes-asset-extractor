"""Extractor-owned authoritative identities for world-residency generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _normalized_source_package_path(value: object) -> str:
    package_path = str(value).replace("\\", "/").strip("/")
    if not package_path or ".." in Path(package_path).parts:
        raise ValueError(f"invalid source package path: {package_path!r}")
    return package_path


def authoritative_source_object_id(
    chunk_name: object,
    class_name: object,
    object_name: object,
    *,
    source_package_relative_path: object | None = None,
) -> str:
    package_path = _normalized_source_package_path(
        source_package_relative_path or f"Maps/{chunk_name}.vgr"
    )
    class_text = str(class_name).strip()
    object_text = str(object_name).strip()
    if not class_text or not object_text:
        raise ValueError("source class and object names must be non-empty")
    return f"ue2://{package_path}#Export/{class_text}/{object_text}"


def authoritative_source_node_identity(
    chunk_name: object, source_object: dict[str, Any]
) -> tuple[str, str]:
    source_object_id = str(
        source_object.get("authoritative_source_object_id", "")
    ).strip()
    if not source_object_id:
        source_object_id = authoritative_source_object_id(
            chunk_name,
            source_object.get("class", ""),
            source_object.get("name", ""),
        )
    source_node_id = str(
        source_object.get("authoritative_source_node_id", "")
    ).strip()
    if not source_node_id:
        source_node_id = "actor_root"
    return source_object_id, source_node_id


def authoritative_source_terrain_id(
    *, source_package_relative_path: object, export_name: object
) -> str:
    package_path = _normalized_source_package_path(source_package_relative_path)
    export_text = str(export_name).strip()
    if not export_text:
        raise ValueError("TerrainInfo export name is empty")
    return f"ue2://{package_path}#TerrainInfo/{export_text}"
