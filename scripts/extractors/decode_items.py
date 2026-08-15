#!/usr/bin/env python3
"""Build Vanguard's authoritative item-appearance catalog.

The wire identity of an appearance is ``(package_index, attachment_index)``.
The old extractor discarded the package and recursively interpreted every
ArrayProperty as object references. That made most attachment IDs ambiguous
and introduced false mesh edges. This generator instead retains every source
ITEMS package, follows only ``Item Template -> Item Components`` references,
preserves appearance semantics, and publishes only proven package mappings.

Outputs:

* ``output/data/item_appearance_catalog.json`` — small runtime package index
* ``output/data/item_appearances/<source-package>.json`` — package payloads

The complete catalog is deliberately split because a character normally uses
only a handful of appearance packages. Runtime consumers load and cache those
packages by the authoritative package index.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config
from ue2.package import UE2Package
from scripts.lib.ue2_tagged_properties import (
    TYPE_ARRAY,
    TYPE_BOOL,
    TYPE_STRUCT,
    TaggedProperty,
    TaggedPropertyError,
    decode_object_reference_array,
    decode_scalar,
    properties_by_name,
    read_tagged_properties,
)


ASSETS = Path(
    os.environ.get(
        "VANGUARD_ASSETS",
        os.environ.get("VANGUARD_ASSETS_PATH", config.ASSETS_PATH),
    )
)
ITEMS_DIR = ASSETS / "Characters" / "Meshes"
OUTPUT_PATH = ROOT / "output" / "data" / "item_appearance_catalog.json"
PACKAGE_DIRECTORY_NAME = "item_appearances"


# Protocol/content constants. Unknown categories remain unmapped and therefore
# cannot silently resolve to the wrong package. Extend only with client evidence.
RUNTIME_PACKAGE_INDEX_TO_SOURCE = {
    8: "BACK_ITEMS",
    9: "CHEST_ITEMS",
    11: "FACE_ITEMS",
    12: "FEET_ITEMS",
    13: "FINGER_ITEMS",
    14: "HAND_ITEMS",
    15: "HEAD_ITEMS",
    16: "LEG_ITEMS",
    18: "SHOULDER_ITEMS",
    19: "WAIST_ITEMS",
    20: "WRIST_ITEMS",
    21: "TOOL_ITEMS",
    22: "AXE_ITEMS",
    23: "BOW_ITEMS",
    25: "CROSSBOW_ITEMS",
    26: "DAGGER_ITEMS",
    28: "HAMMER_ITEMS",
    29: "MACE_ITEMS",
    31: "STAFF_ITEMS",
    33: "SWORD_ITEMS",
    34: "SHIELD_ITEMS",
    35: "SPEAR_ITEMS",
    36: "FIST_ITEMS",
    40: "MARTIALSWORD_ITEMS",
    41: "MARTIALSTAFF_ITEMS",
    42: "FOCUS_ITEMS",
    43: "BARD_ITEMS",
    500: "MOUNT_ITEMS",
    601: "FULLSUIT_ITEMS",
}


ATTACHMENT_ID_RE = re.compile(r"_(\d+)$")
ITEM_TEMPLATE = "Item Template"
ITEM_COMPONENTS = "Item Components"


def _source_package(path: Path) -> str:
    return path.stem.removeprefix("UEM_")


def _attachment_id(export_name: str) -> int | None:
    match = ATTACHMENT_ID_RE.search(export_name)
    return int(match.group(1)) if match else None


def _reference_record(pkg: UE2Package, index: int) -> dict[str, Any]:
    record: dict[str, Any] = {"index": index}
    if index == 0:
        record["kind"] = "none"
        return record
    if index > 0:
        if index - 1 >= len(pkg.exports):
            raise TaggedPropertyError(f"export reference {index} is out of range")
        exp = pkg.exports[index - 1]
        record.update(
            {
                "kind": "export",
                "object": str(exp.get("object_name", "")),
                "class": str(exp.get("class_name", "")),
            }
        )
        return record

    import_index = -index - 1
    if import_index >= len(pkg.imports):
        raise TaggedPropertyError(f"import reference {index} is out of range")
    imp = pkg.imports[import_index]
    record.update(
        {
            "kind": "import",
            "object": str(imp.get("object_name", "")),
            "class": str(imp.get("class_name", "")),
        }
    )
    package_name = str(imp.get("package_name", ""))
    if not package_name and int(imp.get("package", 0)) < 0:
        parent_index = -int(imp["package"]) - 1
        if 0 <= parent_index < len(pkg.imports):
            package_name = str(pkg.imports[parent_index].get("object_name", ""))
    if package_name:
        record["package"] = package_name
    return record


def _property_map(
    data: bytes, names: list[str], *, require_terminator: bool = True
) -> dict[str, TaggedProperty]:
    return properties_by_name(
        read_tagged_properties(data, names, require_terminator=require_terminator)
    )


def _component_refs(template: dict[str, TaggedProperty]) -> list[int]:
    prop = template.get(ITEM_COMPONENTS)
    if prop is None:
        return []
    if prop.type_id != TYPE_ARRAY:
        raise TaggedPropertyError(f"{ITEM_COMPONENTS!r} is not an ArrayProperty")
    return decode_object_reference_array(prop.raw)


def _template_from_export(pkg: UE2Package, exp: dict[str, Any]) -> dict[str, TaggedProperty]:
    top = _property_map(pkg.get_export_data(exp), pkg.names)
    item_template = top.get(ITEM_TEMPLATE)
    if item_template is None:
        return {}
    if item_template.type_id != TYPE_STRUCT or item_template.struct_name != "ItemTemplate":
        raise TaggedPropertyError(
            f"{exp.get('object_name')} has an invalid {ITEM_TEMPLATE!r} property"
        )
    return _property_map(item_template.raw, pkg.names, require_terminator=False)


def _resolve_component_meshes(
    pkg: UE2Package,
    refs: list[int],
    visited_exports: set[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Follow only the authored Item Components graph."""
    visited = visited_exports if visited_exports is not None else set()
    source_refs: list[dict[str, Any]] = []
    meshes: list[dict[str, Any]] = []
    for index in refs:
        record = _reference_record(pkg, index)
        source_refs.append(record)
        if record["kind"] == "none":
            continue
        if record["kind"] == "import":
            if record.get("class") != "EMFXMesh":
                raise TaggedPropertyError(
                    f"item component {record.get('object')!r} is "
                    f"{record.get('class')!r}, not EMFXMesh"
                )
            meshes.append(
                {"package": record.get("package", ""), "mesh": record.get("object", "")}
            )
            continue

        if index in visited:
            raise TaggedPropertyError(f"cycle in Item Components graph at export {index}")
        visited.add(index)
        exp = pkg.exports[index - 1]
        if exp.get("class_name") != "EMFXMesh":
            raise TaggedPropertyError(
                f"item component export {index} is {exp.get('class_name')!r}, not EMFXMesh"
            )
        nested_refs = _component_refs(_template_from_export(pkg, exp))
        nested_source, nested_meshes = _resolve_component_meshes(pkg, nested_refs, visited)
        source_refs.extend(nested_source)
        meshes.extend(nested_meshes)

    unique_meshes: list[dict[str, Any]] = []
    seen_meshes: set[tuple[str, str]] = set()
    for mesh in meshes:
        identity = (str(mesh.get("package", "")), str(mesh.get("mesh", "")))
        if identity in seen_meshes:
            continue
        seen_meshes.add(identity)
        unique_meshes.append(mesh)
    return source_refs, unique_meshes


def _bool_mask(prop: TaggedProperty | None, names: list[str]) -> list[str]:
    if prop is None:
        return []
    if prop.type_id != TYPE_STRUCT:
        raise TaggedPropertyError(f"mask {prop.name!r} is not a StructProperty")
    enabled: list[str] = []
    for entry in read_tagged_properties(prop.raw, names, require_terminator=False):
        if entry.type_id != TYPE_BOOL:
            raise TaggedPropertyError(
                f"mask field {entry.name!r} is type {entry.type_id}, not BoolProperty"
            )
        if bool(decode_scalar(entry, names)):
            enabled.append(entry.name)
    return enabled


def _scalar_or_default(
    template: dict[str, TaggedProperty], name: str, names: list[str], default: Any
) -> Any:
    prop = template.get(name)
    return default if prop is None else decode_scalar(prop, names)


def _variant_selector(export_name: str) -> dict[str, str]:
    """Preserve the authored model/gender qualifier embedded in export names."""
    match = re.search(r"(?:^|_)(M|F)(?:_|$)", export_name, re.IGNORECASE)
    if match is None:
        return {}
    gender = match.group(1).upper()
    family = export_name[: match.start(1)].rstrip("_")
    return {"gender": gender, "model_family": family}


def _attachment_record(pkg: UE2Package, exp: dict[str, Any]) -> dict[str, Any]:
    template = _template_from_export(pkg, exp)
    source_refs, meshes = _resolve_component_meshes(pkg, _component_refs(template))
    source_export = str(exp.get("object_name", ""))
    return {
        "source_export": source_export,
        "selector": _variant_selector(source_export),
        "components": source_refs,
        "meshes": meshes,
        "skin": _scalar_or_default(template, "Skin", pkg.names, 0),
        "layer": _scalar_or_default(template, "Layer", pkg.names, 0),
        "hide_armor_only": _scalar_or_default(
            template, "Hide Armor Only", pkg.names, False
        ),
        "skin_children": _scalar_or_default(template, "Skin Children", pkg.names, False),
        "tint_primary": _scalar_or_default(template, "Tint Primary", pkg.names, 0),
        "tint_secondary": _scalar_or_default(template, "Tint Secondary", pkg.names, 0),
        "tint_children": _scalar_or_default(template, "Tint Children", pkg.names, False),
        "hidden_by": _bool_mask(template.get("Item Hidden By"), pkg.names),
        "hides": _bool_mask(template.get("Item Hides"), pkg.names),
    }


def decode_items_file(path: Path) -> dict[str, Any]:
    pkg = UE2Package(str(path))
    attachments: dict[str, list[dict[str, Any]]] = {}
    for exp in pkg.exports:
        if exp.get("class_name") != "EMFXMesh":
            continue
        attachment_id = _attachment_id(str(exp.get("object_name", "")))
        if attachment_id is None:
            continue
        key = str(attachment_id)
        record = _attachment_record(pkg, exp)
        variants = attachments.setdefault(key, [])
        # Some packages contain byte-identical duplicate exports. Preserve one
        # semantic variant rather than making file order part of resolution.
        if record not in variants:
            variants.append(record)
    for variants in attachments.values():
        variants.sort(key=lambda variant: str(variant.get("source_export", "")))
    return {
        "source_file": path.name,
        "source_package": _source_package(path),
        "attachments": attachments,
    }


def build_catalog(items_dir: Path = ITEMS_DIR) -> dict[str, Any]:
    packages: dict[str, dict[str, Any]] = {}
    files = sorted({Path(path) for path in glob.glob(str(items_dir / "*ITEMS*.uem"))})
    for path in files:
        record = decode_items_file(path)
        package = record["source_package"]
        if package in packages:
            raise TaggedPropertyError(f"duplicate source package {package!r}")
        packages[package] = record

    missing_sources = sorted(
        set(RUNTIME_PACKAGE_INDEX_TO_SOURCE.values()) - set(packages.keys())
    )
    if missing_sources:
        raise TaggedPropertyError(
            "runtime package map references absent source packages: "
            + ", ".join(missing_sources)
        )
    return {
        "schema": 3,
        "generated_by": "scripts/extractors/decode_items.py",
        "identity": ["package_index", "attachment_index"],
        "runtime_package_index_to_source": {
            str(index): source
            for index, source in sorted(RUNTIME_PACKAGE_INDEX_TO_SOURCE.items())
        },
        "packages": packages,
    }


def write_catalog(catalog: dict[str, Any], output_path: Path) -> None:
    """Write an index plus independently loadable package payloads."""
    package_directory = output_path.parent / PACKAGE_DIRECTORY_NAME
    package_directory.mkdir(parents=True, exist_ok=True)

    expected_files: set[str] = set()
    package_index: dict[str, dict[str, Any]] = {}
    for source_package, payload in sorted(catalog["packages"].items()):
        filename = f"{source_package}.json"
        expected_files.add(filename)
        package_path = package_directory / filename
        package_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        package_index[source_package] = {
            "source_file": payload["source_file"],
            "attachment_count": len(payload["attachments"]),
            "path": f"{PACKAGE_DIRECTORY_NAME}/{filename}",
        }

    # The package directory is generator-owned. Stale package payloads must
    # not remain silently addressable after the source set changes.
    for stale_path in package_directory.glob("*.json"):
        if stale_path.name not in expected_files:
            stale_path.unlink()

    index_payload = {
        "schema": catalog["schema"],
        "generated_by": catalog["generated_by"],
        "identity": catalog["identity"],
        "runtime_package_index_to_source": catalog[
            "runtime_package_index_to_source"
        ],
        "packages": package_index,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(index_payload, indent=2, sort_keys=True) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items-dir", type=Path, default=ITEMS_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalog = build_catalog(args.items_dir)
    write_catalog(catalog, args.output)
    attachment_count = sum(
        len(package["attachments"]) for package in catalog["packages"].values()
    )
    print(
        f"Wrote {len(catalog['packages'])} source packages and "
        f"{attachment_count} package-qualified attachments to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
