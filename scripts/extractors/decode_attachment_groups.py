#!/usr/bin/env python3
"""Decode Vanguard's 17 authored SGO attachment-group packages."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

from vanguard_assets import config
ROOT = config.PROJECT_ROOT
from ue2.package import UE2Package
from scripts.lib.ue2_tagged_properties import (
    TYPE_ARRAY,
    TaggedPropertyError,
    decode_object_reference_array,
    decode_scalar,
    read_tagged_properties,
)


ASSETS = Path(
    os.environ.get(
        "VANGUARD_ASSETS",
        os.environ.get("VANGUARD_ASSETS_PATH", config.ASSETS_PATH),
    )
)
GROUP_DIR = ASSETS / "AttachmentGroups"
OUTPUT_PATH = ROOT / "output" / "data" / "attachment_group_catalog.json"

# Exact order returned by the original client's attachment-group package switch.
CATEGORY_ORDER = [
    "Shirts",
    "Pants",
    "Footwear",
    "Belts",
    "Bracers",
    "Gloves",
    "Aprons",
    "WholeOutfits",
    "Swords",
    "Tools",
    "Shields",
    "Headwear",
    "Axes",
    "BluntWeapons",
    "Knives",
    "Hair",
    "FacialHair",
]


def _numeric_suffix(value: str) -> int:
    match = re.search(r"(\d+)$", value)
    if match is None:
        raise TaggedPropertyError(f"object {value!r} has no numeric suffix")
    return int(match.group(1))


def _template_record(pkg: UE2Package, exp: dict[str, Any]) -> dict[str, Any]:
    properties = read_tagged_properties(pkg.get_export_data(exp), pkg.names)
    result = {
        "source_export_index": int(exp.get("index", 0)),
        "template_index": (
            _numeric_suffix(str(exp.get("object_name", "")))
            if str(exp.get("object_name", "")).startswith("SGOAttachmentTemplate")
            else None
        ),
        "source_export": str(exp.get("object_name", "")),
        "attachment_type": 0,
        "version_index": 0,
        "active_skin": 0,
        "covers": {},
    }
    for prop in properties:
        if prop.name == "m_AttachmentType":
            result["attachment_type"] = int(decode_scalar(prop, pkg.names))
        elif prop.name == "m_VersionIndex":
            result["version_index"] = int(decode_scalar(prop, pkg.names))
        elif prop.name == "m_ActiveSkin":
            result["active_skin"] = int(decode_scalar(prop, pkg.names))
        elif prop.name == "m_Covers":
            result["covers"][str(prop.array_index)] = int(decode_scalar(prop, pkg.names))
        else:
            raise TaggedPropertyError(
                f"unsupported template property {prop.name!r} in {exp.get('object_name')}"
            )
    return result


def _group_record(pkg: UE2Package, exp: dict[str, Any]) -> dict[str, Any]:
    properties = read_tagged_properties(pkg.get_export_data(exp), pkg.names)
    if len(properties) != 1 or properties[0].name != "m_AttachmentTemplates":
        raise TaggedPropertyError(
            f"attachment group {exp.get('object_name')} has an unexpected property schema"
        )
    prop = properties[0]
    if prop.type_id != TYPE_ARRAY:
        raise TaggedPropertyError("m_AttachmentTemplates is not an ArrayProperty")
    refs = decode_object_reference_array(prop.raw)
    template_exports: list[int] = []
    for ref in refs:
        if ref <= 0 or ref - 1 >= len(pkg.exports):
            raise TaggedPropertyError(
                f"attachment group {exp.get('object_name')} has invalid template ref {ref}"
            )
        template_exp = pkg.exports[ref - 1]
        if template_exp.get("class_name") != "SGOAttachmentTemplate":
            raise TaggedPropertyError(
                f"attachment group {exp.get('object_name')} ref {ref} is not a template"
            )
        template_exports.append(ref)
    return {
        "group_index": _numeric_suffix(str(exp.get("object_name", ""))),
        "source_export": str(exp.get("object_name", "")),
        "template_exports": template_exports,
    }


def decode_group_package(path: Path, category_index: int) -> dict[str, Any]:
    pkg = UE2Package(str(path))
    templates: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, Any]] = {}
    for exp in pkg.exports:
        class_name = str(exp.get("class_name", ""))
        if class_name == "SGOAttachmentTemplate":
            record = _template_record(pkg, exp)
            key = str(record["source_export_index"])
            if key in templates:
                raise TaggedPropertyError(f"{path.name} repeats template {key}")
            templates[key] = record
        elif class_name == "SGOAttachmentGroup":
            record = _group_record(pkg, exp)
            key = str(record["group_index"])
            if key in groups:
                raise TaggedPropertyError(f"{path.name} repeats group {key}")
            groups[key] = record
        else:
            raise TaggedPropertyError(
                f"{path.name} contains unsupported export class {class_name!r}"
            )
    for group in groups.values():
        missing = [
            value for value in group["template_exports"] if str(value) not in templates
        ]
        if missing:
            raise TaggedPropertyError(
                f"{path.name} group {group['group_index']} references missing templates {missing}"
            )
    return {
        "category_index": category_index,
        "category": path.stem,
        "source_file": path.name,
        "templates": templates,
        "groups": groups,
    }


def build_catalog(group_dir: Path = GROUP_DIR) -> dict[str, Any]:
    packages: dict[str, dict[str, Any]] = {}
    for category_index, category in enumerate(CATEGORY_ORDER):
        path = group_dir / f"{category}.sag"
        if not path.exists():
            raise FileNotFoundError(path)
        packages[str(category_index)] = decode_group_package(path, category_index)
    extras = sorted(path.stem for path in group_dir.glob("*.sag") if path.stem not in CATEGORY_ORDER)
    if extras:
        raise TaggedPropertyError("unmapped SAG packages: " + ", ".join(extras))
    return {
        "schema": 1,
        "generated_by": "scripts/extractors/decode_attachment_groups.py",
        "category_order": CATEGORY_ORDER,
        "packages": packages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-dir", type=Path, default=GROUP_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)
    catalog = build_catalog(args.group_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(catalog['packages'])} attachment groups to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
