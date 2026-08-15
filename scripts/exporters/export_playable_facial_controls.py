#!/usr/bin/env python3
"""Export playable facial control data for Godot.

This exporter keeps the original-client data path explicit:

- customization sliders from output/data/customization_sliders.json
- playable assembly data from output/data/playable_races.json
- raw FXA skeleton bones and component refs from playable .uem packages
- static face poses from FXM MOTION_PART transforms in .uea packages

The main output is output/data/playable_facial_controls.json. A diagnostic audit
report is also written to output/debug/playable_facial_controls_audit.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config  # noqa: E402
from ue2.package import UE2Package  # noqa: E402
from ue2_property_reader import (  # noqa: E402
    BinaryReader,
    decode_animset_names,
)
from vanguard_emfxanim import (  # noqa: E402
    get_animated_submotions,
    parse_emfxanim_export,
    submotion_rest_delta,
)
from vanguard_emfxmesh import parse_emfxmesh_export  # noqa: E402


SOURCE_ROOT = Path(config.ASSETS_PATH)
SOURCE_MESH_ROOT = SOURCE_ROOT / "Characters" / "Meshes"
SOURCE_ANIM_ROOT = SOURCE_ROOT / "Characters" / "Animations"
OUTPUT_DATA = REPO / "output" / "data"
OUTPUT_MESH_ROOT = REPO / "output" / "meshes" / "characters"
OUTPUT_DEBUG = REPO / "output" / "debug"

CUSTOMIZATION_SLIDERS_PATH = OUTPUT_DATA / "customization_sliders.json"
PLAYABLE_RACES_PATH = OUTPUT_DATA / "playable_races.json"
DEFAULT_SIDECAR_PATH = OUTPUT_DATA / "playable_facial_controls.json"
DEFAULT_AUDIT_PATH = OUTPUT_DEBUG / "playable_facial_controls_audit.json"

DEFAULT_EYE_MESHES = [
    "UEM_generic_M_char/generic_M_char_eye_0_L_0.gltf",
    "UEM_generic_M_char/generic_M_char_eye_0_R_0.gltf",
]

FACE_BONE_TOKENS = (
    "face",
    "brow",
    "eye",
    "eyelid",
    "orbit",
    "cheek",
    "mouth",
    "lip",
    "jaw",
    "nose",
    "nostril",
    "ear",
    "tongue",
    "cranial",
    "pariental",
)
EXPRESSION_BONE_TOKENS = (
    "brow",
    "eye",
    "eyelid",
    "orbit",
    "cheek",
    "mouth",
    "lip",
    "jaw",
    "nose",
    "nostril",
    "tongue",
)
MASTER_REF_PROPERTIES = (
    "Head",
    "Ears",
    "Eye Left",
    "Eye Right",
    "UseMesh",
    "Feet",
    "Legs Lower",
    "Legs Upper",
    "Hands",
    "Wrists",
    "Arms Lower",
    "Arms Upper",
    "Torso",
    "Neck",
)
MASTER_COMPONENT_ORDER = (
    "Torso",
    "Neck",
    "Arms Upper",
    "Arms Lower",
    "Wrists",
    "Hands",
    "Legs Upper",
    "Legs Lower",
    "Feet",
    "Head",
    "Ears",
)
FULL_FACE_REQUIRED_BONES = {
    "jawGroup",
    "l_eye",
    "r_eye",
    "l_eyebrow_1",
    "r_eyebrow_1",
    "l_eyebrow_2",
    "r_eyebrow_2",
    "l_lipUpper_1",
    "r_lipUpper_1",
    "l_lipUpper_2",
    "r_lipUpper_2",
    "l_lipLower",
    "r_lipLower",
    "l_cheek_2",
    "r_cheek_2",
}
REDUCED_FACE_REQUIRED_BONES = {
    "face_root",
    "eyeGroup",
    "l_eyeGroup",
    "r_eyeGroup",
    "l_eyelidUpper",
    "r_eyelidUpper",
    "l_eyelidLower",
    "r_eyelidLower",
    "mouthGroup",
    "lipUpperGroup",
    "lipLowerGroup",
    "jaw_1",
    "jaw_end",
    "centerBrow",
}
CORE_HAND_REQUIRED_BONES = {
    "l_hand",
    "r_hand",
    "l_thumb_1",
    "r_thumb_1",
    "l_index_1",
    "r_index_1",
    "l_ring_1",
    "r_ring_1",
}

UNRESOLVED_ALIASES = {
    "l_lipLower_1": {
        "candidate": "l_lipLower",
        "status": "unproven",
        "reason": "Customization table target was not found in checked playable skeletons.",
    },
    "r_lipLower_1": {
        "candidate": "r_lipLower",
        "status": "unproven",
        "reason": "Customization table target was not found in checked playable skeletons.",
    },
}


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def _round_float(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if rounded == -0.0 else rounded


def _round_vec(values: tuple[float, ...] | list[float]) -> list[float]:
    return [_round_float(value) for value in values]


def _socket_record(socket: Any) -> dict[str, Any]:
    """Serialize an authored EMFX socket without inventing runtime defaults."""
    return {
        "alias": str(socket.attach_alias),
        "bone": str(socket.bone_name),
        "emfx_node": int(socket.emfx_node),
        "rotation_degrees": _round_vec(socket.rotation),
        "translation": _round_vec(socket.translation),
        "test_scale": _round_float(socket.test_scale),
    }


def _is_face_bone(name: str) -> bool:
    lower = name.lower()
    return any(token in lower for token in FACE_BONE_TOKENS)


def _is_expression_bone(name: str) -> bool:
    lower = name.lower()
    return any(token in lower for token in EXPRESSION_BONE_TOKENS)


def _node_record(node: Any) -> dict[str, Any]:
    return {
        "name": node.name,
        "parent": node.parent_name or None,
        "parent_index": node.parent_index,
        "position": _round_vec(node.position),
        "rotation": _round_vec(node.rotation),
        "scale": _round_vec(node.scale),
    }


def _read_compact_index_from_bytes(raw: bytes | bytearray, pos: int = 0) -> tuple[int, int]:
    b0 = raw[pos]
    pos += 1
    neg = b0 & 0x80
    value = b0 & 0x3F
    if b0 & 0x40:
        b1 = raw[pos]
        pos += 1
        value |= (b1 & 0x7F) << 6
        if b1 & 0x80:
            b2 = raw[pos]
            pos += 1
            value |= (b2 & 0x7F) << 13
            if b2 & 0x80:
                b3 = raw[pos]
                pos += 1
                value |= (b3 & 0x7F) << 20
                if b3 & 0x80:
                    b4 = raw[pos]
                    pos += 1
                    value |= (b4 & 0x3F) << 27
    return (-value if neg else value), pos


def _ue2_object_ref_record(index: int, pkg: UE2Package) -> dict[str, Any]:
    record: dict[str, Any] = {"index": index}
    if index == 0:
        record["kind"] = "none"
        return record
    if index > 0:
        record["kind"] = "export"
        if index - 1 < len(pkg.exports):
            exp = pkg.exports[index - 1]
            record["object"] = str(exp.get("object_name", ""))
            record["class"] = str(exp.get("class_name", ""))
        return record
    record["kind"] = "import"
    import_index = -index - 1
    if import_index < len(pkg.imports):
        imp = pkg.imports[import_index]
        record["object"] = str(imp.get("object_name", ""))
        record["class"] = str(imp.get("class_name", ""))
        package = imp.get("package_name")
        if package:
            record["package"] = str(package)
    return record


def _decode_object_property(raw: bytes | bytearray, pkg: UE2Package) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        index, _ = _read_compact_index_from_bytes(raw, 0)
    except (IndexError, ValueError):
        return None
    record = _ue2_object_ref_record(index, pkg)
    if record.get("kind") == "none":
        return None
    return record


def _read_ue2_property_tags(
    data: bytes | bytearray, names: list[str]
) -> dict[str, dict[str, Any]]:
    tags: dict[str, dict[str, Any]] = {}
    reader = BinaryReader(data, 0)
    while reader.tell() < len(data):
        name_index = reader.read_compact_index()
        name = names[name_index] if 0 <= name_index < len(names) else ""
        if name.lower() == "none":
            break

        info = reader.read_byte()
        is_array = (info & 0x80) != 0
        prop_type = info & 0x0F
        struct_name = ""
        if prop_type == 10:
            struct_index = reader.read_compact_index()
            if 0 <= struct_index < len(names):
                struct_name = names[struct_index]

        size_type = (info >> 4) & 7
        if size_type == 0:
            data_size = 1
        elif size_type == 1:
            data_size = 2
        elif size_type == 2:
            data_size = 4
        elif size_type == 3:
            data_size = 12
        elif size_type == 4:
            data_size = 16
        elif size_type == 5:
            data_size = reader.read_byte()
        elif size_type == 6:
            data_size = reader.read_uint16()
        elif size_type == 7:
            data_size = reader.read_int32()
        else:
            data_size = 0

        array_index = 0
        if prop_type != 3 and is_array:
            b = reader.read_byte()
            if b < 128:
                array_index = b
            else:
                b2 = reader.read_byte()
                array_index = (b & 0x3F) | (b2 << 6)
                if b & 0x40:
                    b3 = reader.read_byte()
                    b4 = reader.read_byte()
                    array_index = (b & 0x3F) | (b2 << 6) | (b3 << 14) | (b4 << 22)

        raw = b""
        if prop_type != 3:
            raw = bytes(reader.data[reader.tell() : reader.tell() + data_size])
            reader.skip(data_size)

        tags[name] = {
            "type": prop_type,
            "is_array": is_array,
            "array_index": array_index,
            "struct": struct_name,
            "raw": raw,
        }
    return tags


def _skeleton_variant_index(export_name: str) -> int | None:
    marker = "_ALL_"
    end = "_SKELETON"
    if marker not in export_name or not export_name.endswith(end):
        return None
    suffix = export_name.split(marker, 1)[1].removesuffix(end)
    return int(suffix) if suffix.isdigit() else None


def _rig_capabilities(nodes: list[Any]) -> dict[str, Any]:
    bone_names = {node.name for node in nodes}
    missing_full_face = sorted(FULL_FACE_REQUIRED_BONES - bone_names)
    missing_reduced_face = sorted(REDUCED_FACE_REQUIRED_BONES - bone_names)
    missing_core_hand = sorted(CORE_HAND_REQUIRED_BONES - bone_names)
    return {
        "face": (
            "full"
            if not missing_full_face
            else "reduced"
            if not missing_reduced_face
            else "partial"
            if any(_is_face_bone(name) for name in bone_names)
            else "none"
        ),
        "has_full_face": not missing_full_face,
        "has_reduced_face": not missing_reduced_face,
        "has_tongue": any("tongue" in name.lower() for name in bone_names),
        "has_core_hand": not missing_core_hand,
        "has_pinky": any("pinky" in name.lower() for name in bone_names),
        "missing_full_face_bones": missing_full_face,
        "missing_reduced_face_bones": missing_reduced_face,
        "missing_core_hand_bones": missing_core_hand,
    }


def _component_parts_for_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    package = str(profile.get("source_package", ""))
    refs = profile.get("object_refs", {})
    parts: list[dict[str, Any]] = []
    for slot in MASTER_COMPONENT_ORDER:
        ref = refs.get(slot)
        if not isinstance(ref, dict):
            continue
        export_name = str(ref.get("object", ""))
        if not export_name:
            continue
        ref_package = str(ref.get("package", "")) or package
        path = f"{ref_package}/{export_name}.gltf"
        exists = (OUTPUT_MESH_ROOT / path).exists()
        parts.append(
            {
                "slot": slot,
                "package": ref_package,
                "export": export_name,
                "path": path if exists else None,
                "exists": exists,
                "ref_kind": str(ref.get("kind", "")),
            }
        )
    return parts


def _package_for_skeleton_export(export_name: str, fallback_package: str = "") -> str:
    marker = "_ALL_"
    if marker not in export_name:
        return fallback_package
    prefix = export_name.split(marker, 1)[0]
    return f"UEM_{prefix}" if prefix else fallback_package


def _use_mesh_profile_id(profile: dict[str, Any]) -> str | None:
    ref = (profile.get("object_refs", {}) or {}).get("UseMesh")
    if not isinstance(ref, dict):
        return None
    export_name = str(ref.get("object", ""))
    if not export_name:
        return None
    package = str(ref.get("package", "")) or _package_for_skeleton_export(
        export_name, str(profile.get("source_package", ""))
    )
    return f"{package}:{export_name}" if package else None


def _resolved_component_parts(
    profile: dict[str, Any], profiles_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    by_slot: dict[str, dict[str, Any]] = {}

    def collect(current: dict[str, Any], seen: set[str]) -> None:
        use_mesh_profile_id = str(current.get("use_mesh_profile", ""))
        if use_mesh_profile_id and use_mesh_profile_id not in seen:
            base = profiles_by_id.get(use_mesh_profile_id)
            if base:
                seen.add(use_mesh_profile_id)
                collect(base, seen)
        for part in current.get("component_parts", []):
            if isinstance(part, dict):
                by_slot[str(part.get("slot", ""))] = dict(part)

    collect(profile, set())
    return [
        by_slot[slot]
        for slot in MASTER_COMPONENT_ORDER
        if slot in by_slot
    ]


def _optimized_package_for_entry(entry: dict[str, Any]) -> str | None:
    if not bool(entry.get("visual_supported", False)):
        return None
    package = str(entry.get("optimized_package", ""))
    if not package:
        raise RuntimeError(
            f"Supported playable entry has no optimized package: {entry!r}"
        )
    path = SOURCE_MESH_ROOT / f"{package}.uem"
    if not path.exists():
        raise RuntimeError(f"Missing optimized source package: {path}")
    return package


def _optimized_style_index_for_entry(entry: dict[str, Any]) -> int:
    return int(entry.get("optimized_style_index", 0))


def _optimized_mesh_info(entry: dict[str, Any]) -> dict[str, Any] | None:
    package = _optimized_package_for_entry(entry)
    if not package:
        return None
    style_index = _optimized_style_index_for_entry(entry)
    stem = package.removeprefix("UEM_")
    prefix = f"{stem}_ALL_{style_index}_C_"
    package_dir = OUTPUT_MESH_ROOT / package
    variants: list[dict[str, Any]] = []
    if package_dir.exists():
        for path in sorted(package_dir.glob(f"{prefix}*.gltf")):
            suffix = path.stem.rsplit("_C_", 1)[-1]
            if not suffix.isdigit():
                continue
            variants.append(
                {
                    "variant": int(suffix),
                    "export": path.stem,
                    "path": f"{package}/{path.name}",
                }
            )
    variants.sort(key=lambda item: int(item["variant"]))
    if not variants:
        return None
    default = variants[0]
    return {
        "package": package,
        "style_index": style_index,
        "default_variant": int(default["variant"]),
        "default_path": str(default["path"]),
        "variants": variants,
    }


def _flatten_sliders(slider_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sliders: list[dict[str, Any]] = []
    slider_index = 0
    for page in slider_pages:
        page_id = page.get("id")
        page_name = str(page.get("name", ""))
        for slider in page.get("sliders", []):
            targets = []
            for bone in slider.get("bones", []):
                targets.append(
                    {
                        "bone": str(bone.get("name", "")),
                        "rows": {
                            "x": bone.get("rowMin", []),
                            "y": bone.get("rowMid", []),
                            "z": bone.get("rowMax", []),
                        },
                    }
                )
            sliders.append(
                {
                    "index": slider_index,
                    "page_id": page_id,
                    "page": page_name,
                    "name": str(slider.get("name", "")),
                    "targets": targets,
                }
            )
            slider_index += 1
    return sliders


def _slider_target_names(sliders: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for slider in sliders:
        for target in slider.get("targets", []):
            bone = str(target.get("bone", ""))
            if bone:
                names.add(bone)
    return names


@lru_cache(maxsize=None)
def _load_gltf_node_names(rel_path: str) -> frozenset[str]:
    path = OUTPUT_MESH_ROOT / rel_path
    data = _read_json(path, {})
    names = {
        str(node.get("name", ""))
        for node in data.get("nodes", [])
        if isinstance(node, dict) and node.get("name")
    }
    return frozenset(names)


@lru_cache(maxsize=None)
def _parse_emfx_mesh_exports(package_name: str) -> tuple[dict[str, Any], ...]:
    package_path = SOURCE_MESH_ROOT / f"{package_name}.uem"
    if not package_path.exists():
        return ()
    try:
        pkg = UE2Package(str(package_path))
    except Exception:
        return ()

    records: list[dict[str, Any]] = []
    for exp in pkg.exports:
        if exp.get("class_name") != "EMFXMesh":
            continue
        object_name = str(exp.get("object_name", ""))
        try:
            export_data = pkg.get_export_data(exp)
            mesh = parse_emfxmesh_export(export_data, pkg.names)
        except Exception:
            continue
        if not mesh or not mesh.nodes:
            continue
        property_tags: dict[str, dict[str, Any]] = {}
        object_refs: dict[str, dict[str, Any]] = {}
        animsets: list[str] = []
        try:
            property_tags = _read_ue2_property_tags(export_data, pkg.names)
        except Exception:
            property_tags = {}
        for prop_name in MASTER_REF_PROPERTIES:
            tag = property_tags.get(prop_name)
            if not tag or tag.get("type") != 5:
                continue
            ref = _decode_object_property(tag.get("raw", b""), pkg)
            if ref:
                object_refs[prop_name] = ref
        animset_tag = property_tags.get("AnimSet")
        if animset_tag and isinstance(animset_tag.get("raw"), (bytes, bytearray)):
            animsets = decode_animset_names(animset_tag["raw"], pkg.names)
        records.append(
            {
                "package": package_name,
                "export": object_name,
                "nodes": tuple(mesh.nodes),
                "sockets": tuple(mesh.sockets),
                "submesh_count": len(mesh.submeshes),
                "variant_index": _skeleton_variant_index(object_name),
                "object_refs": object_refs,
                "animsets": tuple(animsets),
            }
        )
    return tuple(records)


def _select_skeleton_profile(package_name: str) -> dict[str, Any] | None:
    records = list(_parse_emfx_mesh_exports(package_name))
    if not records:
        return None

    def score(record: dict[str, Any]) -> tuple[int, int]:
        name = str(record.get("export", ""))
        nodes = record.get("nodes", ())
        if name.endswith("_ALL_0_SKELETON"):
            return (4, len(nodes))
        if "_ALL_0_" in name and "SKELETON" in name:
            return (3, len(nodes))
        if "SKELETON" in name:
            return (2, len(nodes))
        return (1, len(nodes))

    selected = max(records, key=score)
    nodes = list(selected["nodes"])
    bone_names = [node.name for node in nodes]
    return {
        "source_package": package_name,
        "source_export": selected["export"],
        "variant_index": selected.get("variant_index"),
        "submesh_count": int(selected.get("submesh_count", 0)),
        "bone_count": len(nodes),
        "bones": [_node_record(node) for node in nodes],
        "sockets": [_socket_record(socket) for socket in selected.get("sockets", ())],
        "face_bones": [name for name in bone_names if _is_face_bone(name)],
        "object_refs": selected.get("object_refs", {}),
        "animsets": list(selected.get("animsets", ())),
        "capabilities": _rig_capabilities(nodes),
        "_node_lookup": {node.name: node for node in nodes},
    }


def _strip_private_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_private_keys(child)
            for key, child in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_strip_private_keys(child) for child in value]
    return value


def _current_assembly_bones(entry: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for part in entry.get("body_parts", []):
        rel_path = str(part.get("path", ""))
        if rel_path:
            names.update(_load_gltf_node_names(rel_path))
    for rel_path in DEFAULT_EYE_MESHES:
        names.update(_load_gltf_node_names(rel_path))
    return names


def _package_bone_sets(package_name: str) -> dict[str, set[str]]:
    records = _parse_emfx_mesh_exports(package_name)
    all_bones: set[str] = set()
    skeleton_bones: set[str] = set()
    for record in records:
        names = {node.name for node in record["nodes"]}
        all_bones.update(names)
        if "SKELETON" in str(record.get("export", "")):
            skeleton_bones.update(names)
    return {"all": all_bones, "skeleton": skeleton_bones}


def _master_skeleton_profile_from_record(record: dict[str, Any]) -> dict[str, Any]:
    nodes = list(record.get("nodes", ()))
    bone_names = [node.name for node in nodes]
    profile = {
        "source_package": str(record.get("package", "")),
        "source_export": str(record.get("export", "")),
        "variant_index": record.get("variant_index"),
        "submesh_count": int(record.get("submesh_count", 0)),
        "bone_count": len(nodes),
        "bones": [_node_record(node) for node in nodes],
        "sockets": [_socket_record(socket) for socket in record.get("sockets", ())],
        "face_bones": [name for name in bone_names if _is_face_bone(name)],
        "object_refs": record.get("object_refs", {}),
        "animsets": list(record.get("animsets", ())),
        "capabilities": _rig_capabilities(nodes),
        "_node_lookup": {node.name: node for node in nodes},
    }
    profile["use_mesh_profile"] = _use_mesh_profile_id(profile)
    profile["component_parts"] = _component_parts_for_profile(profile)
    return profile


def _master_skeleton_profiles_for_package(package_name: str) -> list[dict[str, Any]]:
    records = [
        record
        for record in _parse_emfx_mesh_exports(package_name)
        if "SKELETON" in str(record.get("export", ""))
    ]
    profiles = [_master_skeleton_profile_from_record(record) for record in records]
    profiles.sort(
        key=lambda profile: (
            int(profile.get("variant_index"))
            if profile.get("variant_index") is not None
            else 9999,
            str(profile.get("source_export", "")),
        )
    )
    return profiles


def _profile_id(profile: dict[str, Any] | None) -> str | None:
    if not profile:
        return None
    package = str(profile.get("source_package", ""))
    export = str(profile.get("source_export", ""))
    return f"{package}:{export}" if package and export else None


def _body_part_export_for_face(part: dict[str, Any], face_index: int) -> str:
    export_name = str(part.get("export", ""))
    if face_index <= 0 or not export_name:
        return export_name
    lower_export = export_name.lower()
    marker = ""
    if "_char_head_" in lower_export:
        marker = "_head_"
    elif "_char_ears_" in lower_export:
        marker = "_ears_"
    else:
        return export_name
    variant_export = _export_variant_by_delta(export_name, marker, face_index)
    package = str(part.get("package", ""))
    if package and variant_export:
        candidate_path = OUTPUT_MESH_ROOT / package / f"{variant_export}.gltf"
        if candidate_path.exists():
            return variant_export
    return export_name


def _export_variant_by_delta(export_name: str, marker: str, delta: int) -> str:
    marker_index = export_name.find(marker)
    if marker_index < 0:
        return ""
    number_start = marker_index + len(marker)
    number_end = number_start
    while number_end < len(export_name) and export_name[number_end].isdigit():
        number_end += 1
    if number_end <= number_start:
        return ""
    base_index = int(export_name[number_start:number_end])
    return f"{export_name[:number_start]}{max(base_index + delta, 0)}{export_name[number_end:]}"


def _entry_part_exports_for_face(entry: dict[str, Any], face_index: int) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in entry.get("body_parts", []):
        if not isinstance(part, dict):
            continue
        export_name = _body_part_export_for_face(part, face_index)
        lower_export = export_name.lower()
        if "_char_body_" in lower_export:
            result["Body"] = export_name
        elif "_char_head_" in lower_export:
            result["Head"] = export_name
        elif "_char_ears_" in lower_export:
            result["Ears"] = export_name
    return result


def _select_modular_master_profile(
    package_name: str,
    expected_parts: dict[str, str],
) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    candidates = _master_skeleton_profiles_for_package(package_name)
    scored: list[tuple[int, dict[str, Any], str]] = []
    expected_head = expected_parts.get("Head", "")
    expected_ears = expected_parts.get("Ears", "")
    for profile in candidates:
        refs = profile.get("object_refs", {})
        head_ref = str((refs.get("Head") or {}).get("object", ""))
        ears_ref = str((refs.get("Ears") or {}).get("object", ""))
        score = 0
        basis = "unmatched"
        if expected_head and head_ref == expected_head:
            score += 100
            basis = "matched_head_ref"
        elif expected_head and head_ref:
            score -= 100
        if expected_ears and ears_ref == expected_ears:
            score += 40
            basis = "matched_head_and_ears_refs" if score >= 140 else "matched_ears_ref"
        elif expected_ears and ears_ref:
            score -= 20
        if (profile.get("capabilities") or {}).get("has_core_hand"):
            score += 3
        if (profile.get("capabilities") or {}).get("has_reduced_face"):
            score += 2
        scored.append((score, profile, basis))

    scored.sort(
        key=lambda item: (
            item[0],
            -int(item[1].get("variant_index") or 0),
        ),
        reverse=True,
    )
    summary = [
        {
            "profile": _profile_id(profile),
            "score": score,
            "basis": basis,
            "head_ref": str((profile.get("object_refs", {}).get("Head") or {}).get("object", "")),
            "ears_ref": str((profile.get("object_refs", {}).get("Ears") or {}).get("object", "")),
            "capabilities": profile.get("capabilities", {}),
        }
        for score, profile, basis in scored
    ]
    if not scored or scored[0][0] <= 0:
        return None, "unresolved", summary
    return scored[0][1], scored[0][2], summary


def _selected_skeleton_export_name(package_name: str) -> str | None:
    profile = _select_skeleton_profile(package_name)
    return str(profile.get("source_export", "")) if profile else None


def _extract_animsets(package_name: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    selected_export = _selected_skeleton_export_name(package_name)
    for record in _parse_emfx_mesh_exports(package_name):
        if str(record.get("export", "")) != selected_export:
            continue
        for animset in record.get("animsets", ()):
            if animset and animset not in seen:
                names.append(str(animset))
                seen.add(str(animset))
    return names


def _transform_change_amount(transform: dict[str, Any]) -> float:
    pos = sum(abs(value) for value in transform["position_delta"])
    rot = sum(abs(value) for value in transform["rotation_delta"][:3])
    scale = sum(abs(value - 1.0) for value in transform["scale_delta"])
    return pos + rot + scale


def _export_pose_package(
    package_name: str,
    rest_profile: dict[str, Any],
    tolerance: float,
) -> dict[str, Any] | None:
    package_path = SOURCE_ANIM_ROOT / f"{package_name}.uea"
    if not package_path.exists():
        return None
    try:
        pkg = UE2Package(str(package_path))
    except Exception:
        return None

    rest_nodes: dict[str, Any] = rest_profile.get("_node_lookup", {})
    clips: list[dict[str, Any]] = []
    for exp in pkg.exports:
        if exp.get("class_name") != "EMFXAnim":
            continue
        clip_name = str(exp.get("object_name", ""))
        try:
            anim = parse_emfxanim_export(pkg.get_export_data(exp))
        except Exception:
            continue
        if not anim.submotions:
            continue
        animated = get_animated_submotions(anim)
        targets: list[dict[str, Any]] = []
        for sm in anim.submotions:
            rest_node = rest_nodes.get(sm.name)
            if rest_node is None or not _is_face_bone(sm.name):
                continue
            transform = submotion_rest_delta(sm, rest_node, use_bind_pose=True)
            amount = _transform_change_amount(transform)
            if amount <= tolerance:
                continue
            targets.append(
                {
                    "bone": sm.name,
                    "change_amount": _round_float(amount),
                    **_strip_private_keys(transform),
                }
            )
        if targets or ("face" in clip_name.lower() or "blink" in clip_name.lower()):
            clips.append(
                {
                    "name": clip_name,
                    "duration": _round_float(anim.duration),
                    "kind": "static_pose" if not animated else "keyed_or_mixed",
                    "submotions": len(anim.submotions),
                    "animated_submotions": len(animated),
                    "changed_face_targets": len(targets),
                    "targets": targets,
                }
            )
    if not clips:
        return None
    return {
        "package": package_name,
        "source_path": str(package_path.relative_to(SOURCE_ROOT)),
        "rest_profile": rest_profile["source_package"],
        "rest_export": rest_profile["source_export"],
        "clips": clips,
    }


def _scan_facial_keyframes(animset_names: list[str], pose_package_names: set[str]) -> dict[str, Any]:
    packages_scanned = 0
    clips_scanned = 0
    clips_with_face_keyframes: list[dict[str, Any]] = []
    for package_name in sorted(set(animset_names)):
        if package_name in pose_package_names:
            continue
        package_path = SOURCE_ANIM_ROOT / f"{package_name}.uea"
        if not package_path.exists():
            continue
        try:
            pkg = UE2Package(str(package_path))
        except Exception:
            continue
        packages_scanned += 1
        for exp in pkg.exports:
            if exp.get("class_name") != "EMFXAnim":
                continue
            clip_name = str(exp.get("object_name", ""))
            try:
                anim = parse_emfxanim_export(pkg.get_export_data(exp))
            except Exception:
                continue
            clips_scanned += 1
            face_tracks = sorted(
                {
                    sm.name
                    for sm in get_animated_submotions(anim)
                    if _is_expression_bone(sm.name)
                }
            )
            if face_tracks:
                clips_with_face_keyframes.append(
                    {
                        "package": package_name,
                        "clip": clip_name,
                        "duration": _round_float(anim.duration),
                        "face_tracks": face_tracks,
                    }
                )
    return {
        "packages_scanned": packages_scanned,
        "clips_scanned": clips_scanned,
        "has_face_keyframes": bool(clips_with_face_keyframes),
        "clips_with_face_keyframes": clips_with_face_keyframes,
    }


def build_payload(tolerance: float = 0.001) -> tuple[dict[str, Any], dict[str, Any]]:
    slider_pages = _read_json(CUSTOMIZATION_SLIDERS_PATH, [])
    playable_entries = _read_json(PLAYABLE_RACES_PATH, [])
    sliders = _flatten_sliders(slider_pages)
    slider_targets = _slider_target_names(sliders)

    optimized_packages = sorted(
        {
            package
            for entry in playable_entries
            for package in [_optimized_package_for_entry(entry)]
            if package
        }
    )
    modular_packages = sorted(
        {
            str(part.get("package", ""))
            for entry in playable_entries
            for part in entry.get("body_parts", [])
            if isinstance(part, dict) and str(part.get("package", ""))
        }
    )
    master_profile_packages = sorted(set(optimized_packages) | set(modular_packages))
    master_skeleton_profiles: dict[str, dict[str, Any]] = {}
    package_master_profile_ids: dict[str, list[str]] = {}
    for package in master_profile_packages:
        for profile in _master_skeleton_profiles_for_package(package):
            profile_id = _profile_id(profile)
            if not profile_id:
                continue
            master_skeleton_profiles[profile_id] = profile
            package_master_profile_ids.setdefault(package, []).append(profile_id)

    # Optimized playable meshes carry their own authoritative skin skeleton.
    # Its rest transforms are not interchangeable with the package's separate
    # ``*_SKELETON`` export: several non-human packages author race proportions
    # (head width, stature, and facial scale) directly on the rendered ``C_*``
    # export. Record the selected rendered export as a distinct skin profile;
    # the separate animation/reference profile remains supplemental coverage
    # for bones and sockets absent from the rendered mesh.
    optimized_mesh_profile_ids: dict[tuple[str, str], str] = {}
    for entry in playable_entries:
        package = _optimized_package_for_entry(entry)
        mesh_info = _optimized_mesh_info(entry)
        if not package or not mesh_info:
            continue
        selected_export = str(mesh_info.get("default_path", "")).rsplit("/", 1)[-1]
        if selected_export.endswith(".gltf"):
            selected_export = selected_export[:-5]
        selected_record = next(
            (
                record
                for record in _parse_emfx_mesh_exports(package)
                if str(record.get("export", "")) == selected_export
            ),
            None,
        )
        if selected_record is None:
            continue
        profile = _master_skeleton_profile_from_record(selected_record)
        profile_id = _profile_id(profile)
        if not profile_id:
            continue
        master_skeleton_profiles[profile_id] = profile
        package_ids = package_master_profile_ids.setdefault(package, [])
        if profile_id not in package_ids:
            package_ids.append(profile_id)
        optimized_mesh_profile_ids[
            (str(entry.get("race", "")), str(entry.get("gender", "")))
        ] = profile_id
    for profile in master_skeleton_profiles.values():
        profile["resolved_component_parts"] = _resolved_component_parts(
            profile, master_skeleton_profiles
        )

    profiles: dict[str, dict[str, Any]] = {}
    for package in optimized_packages:
        profile = _select_skeleton_profile(package)
        if profile:
            profiles[package] = profile

    entry_records: list[dict[str, Any]] = []
    missing_current_counts: dict[str, int] = {}
    unresolved_seen: dict[str, list[str]] = {name: [] for name in UNRESOLVED_ALIASES}
    for entry in playable_entries:
        race = str(entry.get("race", ""))
        gender = str(entry.get("gender", ""))
        key = f"{race}_{gender}"
        optimized_package = _optimized_package_for_entry(entry)
        current_bones = _current_assembly_bones(entry)
        optimized_bones = set()
        if optimized_package:
            optimized_bones = {
                bone.get("name", "")
                for bone in profiles.get(optimized_package, {}).get("bones", [])
            }
        missing_current = sorted(slider_targets - current_bones)
        missing_optimized = sorted(slider_targets - optimized_bones) if optimized_bones else []
        for unresolved in UNRESOLVED_ALIASES:
            if unresolved in missing_optimized:
                unresolved_seen.setdefault(unresolved, []).append(key)
        missing_current_counts[key] = len(missing_current)
        face_count = max(int(entry.get("face_count", 1) or 1), 1)
        body_parts = [part for part in entry.get("body_parts", []) if isinstance(part, dict)]
        head_part = next(
            (
                part
                for part in body_parts
                if "_char_head_" in str(part.get("export", "")).lower()
            ),
            None,
        )
        modular_package = str(head_part.get("package", "")) if head_part else ""
        style_skeletons: list[dict[str, Any]] = []
        default_master_profile_id: str | None = None
        for face_index in range(face_count):
            expected_parts = _entry_part_exports_for_face(entry, face_index)
            selected_profile, basis, candidates = _select_modular_master_profile(
                modular_package, expected_parts
            )
            selected_profile_id = _profile_id(selected_profile)
            if face_index == 0:
                default_master_profile_id = selected_profile_id
            style_skeletons.append(
                {
                    "face_index": face_index,
                    "expected_parts": expected_parts,
                    "selected_profile": selected_profile_id,
                    "selection_basis": basis,
                    "candidate_summary": candidates,
                }
            )

        optimized_skin_profile_id = optimized_mesh_profile_ids.get((race, gender))
        optimized_master_profile_id = None
        if optimized_package:
            optimized_stem = optimized_package.removeprefix("UEM_")
            optimized_export = (
                f"{optimized_stem}_ALL_{_optimized_style_index_for_entry(entry)}_SKELETON"
            )
            optimized_master_profile_id = (
                f"{optimized_package}:{optimized_export}"
                if f"{optimized_package}:{optimized_export}" in master_skeleton_profiles
                else None
            )
            if optimized_master_profile_id is None:
                fallback_export = f"{optimized_stem}_ALL_0_SKELETON"
                fallback_id = f"{optimized_package}:{fallback_export}"
                if fallback_id in master_skeleton_profiles:
                    optimized_master_profile_id = fallback_id

        entry_records.append(
            {
                "race": race,
                "gender": gender,
                "optimized_package": optimized_package,
                "current_assembly_packages": [
                    str(part.get("package", "")) for part in entry.get("body_parts", [])
                ],
                "current_assembly_bone_count": len(current_bones),
                "optimized_bone_count": len(optimized_bones),
                "optimized_mesh": _optimized_mesh_info(entry),
                "modular_master_package": modular_package,
                "default_modular_master_skeleton": default_master_profile_id,
                "style_master_skeletons": style_skeletons,
                "optimized_skin_skeleton": optimized_skin_profile_id,
                "optimized_master_skeleton": optimized_master_profile_id,
                "missing_slider_bones_current_assembly": missing_current,
                "missing_slider_bones_optimized_profile": missing_optimized,
            }
        )

    animsets_by_package = {
        package: _extract_animsets(package)
        for package in optimized_packages
    }
    pose_package_refs: dict[str, str] = {}
    for package, animsets in animsets_by_package.items():
        for animset in animsets:
            if animset.lower().endswith("_pose") and (SOURCE_ANIM_ROOT / f"{animset}.uea").exists():
                pose_package_refs.setdefault(animset, package)

    # Human male pose is the known playable face-pose source. Keep it explicit
    # even if an AnimSet scan misses it for a subset of optimized packages.
    if (SOURCE_ANIM_ROOT / "UEA_human_M_pose.uea").exists():
        pose_package_refs.setdefault("UEA_human_M_pose", "UEM_optimizedHuman_M_char")

    pose_packages: list[dict[str, Any]] = []
    for pose_package, ref_mesh_package in sorted(pose_package_refs.items()):
        preferred_package = ref_mesh_package
        if pose_package == "UEA_human_M_pose":
            preferred_package = "UEM_optimizedHuman_M_char"
        elif pose_package == "UEA_human_F_pose":
            preferred_package = "UEM_optimizedHuman_F_char"
        profile = profiles.get(preferred_package) or profiles.get(ref_mesh_package)
        if not profile:
            continue
        exported = _export_pose_package(pose_package, profile, tolerance)
        if exported:
            pose_packages.append(exported)

    all_animsets = sorted(
        {
            animset
            for animsets in animsets_by_package.values()
            for animset in animsets
        }
    )
    facial_keyframe_audit = _scan_facial_keyframes(all_animsets, set(pose_package_refs))

    sidecar = {
        "schema": 1,
        "generated_by": "scripts/exporters/export_playable_facial_controls.py",
        "source": {
            "customization": "bin/Resources/Texts/customization_data.txt",
            "race_mods": "bin/Resources/Texts/cust_race_mods_v2.txt",
            "mesh_root": str((SOURCE_ROOT / "Characters" / "Meshes").relative_to(SOURCE_ROOT)),
            "animation_root": str(
                (SOURCE_ROOT / "Characters" / "Animations").relative_to(SOURCE_ROOT)
            ),
        },
        "interpretation": {
            "control_model": "bone_driven",
            "morph_chunks_expected_empty": [
                "MESH_EXPRESSION",
                "EXPR_MOTION_PART",
                "PHONEME_MOTION",
            ],
            "slider_row_semantics": [
                "x axis row",
                "y axis row",
                "z axis row",
            ],
            "slider_row_columns": [
                "position_min",
                "position_max",
                "rotation_min",
                "rotation_max",
                "scale_min",
                "scale_max",
            ],
        },
        "bone_aliases": UNRESOLVED_ALIASES,
        "sliders": sliders,
        "skeleton_profiles": {
            package: _strip_private_keys(profile)
            for package, profile in sorted(profiles.items())
        },
        "master_skeleton_profiles": {
            profile_id: _strip_private_keys(profile)
            for profile_id, profile in sorted(master_skeleton_profiles.items())
        },
        "master_skeleton_packages": package_master_profile_ids,
        "playable_entries": entry_records,
        "pose_packages": pose_packages,
        "animation_manifest": {
            "animsets_by_optimized_package": animsets_by_package,
            "pose_packages": sorted(pose_package_refs),
            "body_clips_have_playable_facial_keyframes": facial_keyframe_audit[
                "has_face_keyframes"
            ],
            "facial_keyframe_audit": facial_keyframe_audit,
        },
    }

    audit = {
        "summary": {
            "visible_slider_count": len(sliders),
            "unique_slider_target_bones": len(slider_targets),
            "playable_entries": len(entry_records),
            "optimized_packages": len(profiles),
            "master_skeleton_profiles": len(master_skeleton_profiles),
            "master_skeleton_packages": len(package_master_profile_ids),
            "playable_styles_with_master_skeleton": sum(
                1
                for entry in entry_records
                for style in entry.get("style_master_skeletons", [])
                if style.get("selected_profile")
            ),
            "playable_styles_without_master_skeleton": sum(
                1
                for entry in entry_records
                for style in entry.get("style_master_skeletons", [])
                if not style.get("selected_profile")
            ),
            "pose_packages_exported": len(pose_packages),
            "static_pose_clips_exported": sum(
                1
                for package in pose_packages
                for clip in package.get("clips", [])
                if clip.get("kind") == "static_pose"
            ),
        },
        "missing_current_assembly_counts": missing_current_counts,
        "unresolved_alias_targets": unresolved_seen,
        "entries": entry_records,
        "master_skeleton_profiles": {
            profile_id: {
                "source_package": profile.get("source_package"),
                "source_export": profile.get("source_export"),
                "variant_index": profile.get("variant_index"),
                "submesh_count": profile.get("submesh_count"),
                "bone_count": profile.get("bone_count"),
                "object_refs": profile.get("object_refs", {}),
                "use_mesh_profile": profile.get("use_mesh_profile"),
                "component_parts": profile.get("component_parts", []),
                "resolved_component_parts": profile.get("resolved_component_parts", []),
                "animsets": profile.get("animsets", []),
                "capabilities": profile.get("capabilities", {}),
            }
            for profile_id, profile in sorted(master_skeleton_profiles.items())
        },
        "slider_targets": sorted(slider_targets),
        "pose_clip_summaries": [
            {
                "package": package.get("package"),
                "clip": clip.get("name"),
                "kind": clip.get("kind"),
                "changed_face_targets": clip.get("changed_face_targets"),
                "target_bones": [target.get("bone") for target in clip.get("targets", [])],
            }
            for package in pose_packages
            for clip in package.get("clips", [])
        ],
        "facial_keyframe_audit": facial_keyframe_audit,
    }
    return sidecar, audit


def write_payload(sidecar_path: Path, audit_path: Path, tolerance: float) -> tuple[dict[str, Any], dict[str, Any]]:
    sidecar, audit = build_payload(tolerance=tolerance)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n")
    audit_path.write_text(json.dumps(audit, indent=2) + "\n")
    return sidecar, audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_SIDECAR_PATH)
    parser.add_argument("--audit-out", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.001,
        help="Minimum rest-relative transform change to export as a pose target.",
    )
    args = parser.parse_args()

    sidecar, audit = write_payload(args.out, args.audit_out, args.tolerance)
    summary = audit["summary"]
    print(f"Wrote {args.out}")
    print(f"Wrote {args.audit_out}")
    print(
        "Playable facial controls: "
        f"{summary['visible_slider_count']} sliders, "
        f"{summary['unique_slider_target_bones']} target bones, "
        f"{summary['optimized_packages']} optimized skeleton profiles, "
        f"{summary['master_skeleton_profiles']} master skeleton profiles, "
        f"{summary['playable_styles_with_master_skeleton']} playable styles with masters, "
        f"{summary['static_pose_clips_exported']} static pose clips"
    )
    if not sidecar.get("pose_packages"):
        print("WARNING: no pose packages exported", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
