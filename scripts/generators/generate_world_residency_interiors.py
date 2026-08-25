#!/usr/bin/env python3
"""Publish Vanguard BSP authority and placed SGO interior room graphs.

This is the extractor-owned Phase 8 source publication.  It deliberately does
not consume ``sgo_prefabs.json``: that resolved monolith is hundreds of
megabytes and expands substantially in memory.  Instead, the generator streams
``sgo_raw.jsonl`` twice.  The first pass retains only the compact compound graph;
the second retains actors for the closure of placed prefabs that actually own
authored rooms.

Large output must be written to an explicit disk-backed path.  Volatile Linux
locations such as ``/tmp`` and ``/dev/shm`` are rejected.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from ue2 import UE2Package  # noqa: E402
from scripts.lib.vanguard_bsp import (  # noqa: E402
    BspParseError,
    find_level_model_reference,
    parse_model_export,
)
from scripts.lib.world_residency_interiors import (  # noqa: E402
    AffineTransform,
    actor_category,
    assemble_portal_graph,
    canonical_json_bytes,
    compact_compound_refs,
    discover_rooms,
    nested_source_path,
    prefab_closure,
    prefab_name_from_raw_record,
    properties_by_name,
    root_has_room,
    sha256_id,
    source_component_path,
    source_prefab_id,
    strip_private,
    walk_nonroom_actors,
    walk_room_actors,
)
from scripts.lib.world_residency_portals import PortalApertureLibrary  # noqa: E402


SCHEMA = "vanguard_world_interior_source_publication"
VERSION = 2
SOURCE_ASSEMBLY_POLICY = "vanguard_bsp_plus_sgo_compound_type3_rooms_and_apertures_v2"
EXPECTED_RECORD_FORMAT = [
    "asset",
    "node_index",
    "component_index",
    "object_name",
    "prefab_name",
    "translation",
    "rotation",
    "scale",
    "component",
    "authoritative_source_object_id",
    "authoritative_source_node_id",
    "preserved_component_path",
]
VOLATILE_OUTPUT_ROOTS = (Path("/tmp"), Path("/dev/shm"), Path("/run"))


PHASE8_PROPERTY_NAMES = {
    "AmbientSound",
    "BasePos",
    "bBlockActors",
    "bBlockKarma",
    "bBlockNonZeroExtentTraces",
    "bBlockPlayers",
    "bBlockZeroExtentTraces",
    "bCollideActors",
    "bDynamicLight",
    "bHidden",
    "bHiddenEd",
    "bInitiallyOn",
    "bNoDelete",
    "bOpaque",
    "bSpecialLit",
    "ClosedSound",
    "ClosingSound",
    "CollisionHeight",
    "CollisionRadius",
    "DrawScale",
    "DrawScale3D",
    "Event",
    "InitialState",
    "KeyNum",
    "KeyPos",
    "KeyRot",
    "LightBrightness",
    "LightColor",
    "LightCone",
    "LightEffect",
    "LightHue",
    "LightPeriod",
    "LightPhase",
    "LightRadius",
    "LightSaturation",
    "LightType",
    "Location",
    "MoveTime",
    "OpeningSound",
    "Region",
    "Rotation",
    "SavedPos",
    "SavedRot",
    "SoundPitch",
    "SoundRadius",
    "SoundVolume",
    "StaticMesh",
    "StayOpenTime",
    "Tag",
    "TargetKeyframe",
}


def main() -> int:
    args = parse_args()
    try:
        output = args.output.resolve()
        reject_volatile_output(output)
        publication = build_publication(
            maps_root=args.maps_root.resolve(),
            sgo_raw=args.sgo_raw.resolve(),
            terrain_grid_root=args.terrain_grid_root.resolve(),
            object_index_root=args.object_index_root.resolve(),
            mesh_root=args.mesh_root.resolve(),
            source_pack_manifest=(
                args.source_pack_manifest.resolve() if args.source_pack_manifest else None
            ),
            source_terrain_inventory=(
                args.source_terrain_inventory.resolve()
                if args.source_terrain_inventory
                else None
            ),
            progress_every=max(0, args.progress_every),
        )
        write_json_atomic(output, publication)
    except (BspParseError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        "World residency interiors: "
        f"templates={publication['counts']['interior_template_count']} "
        f"instances={publication['counts']['interior_instance_count']} "
        f"rooms={publication['counts']['room_count']} "
        f"connections={publication['counts']['portal_connection_count']} "
        f"unresolved={publication['counts']['unresolved_portal_cluster_count']} "
        f"publication={publication['publication_id']} output={output}"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maps-root", type=Path, default=Path(config.MAPS_DIR))
    parser.add_argument(
        "--sgo-raw",
        type=Path,
        default=Path(config.OUTPUT_DIR) / "data" / "sgo_raw.jsonl",
    )
    parser.add_argument(
        "--terrain-grid-root",
        type=Path,
        default=Path(config.OUTPUT_DIR) / "terrain" / "terrain_grid",
    )
    parser.add_argument(
        "--object-index-root",
        type=Path,
        default=Path(config.OUTPUT_DIR) / "godot_runtime" / "chunks",
    )
    parser.add_argument(
        "--mesh-root",
        type=Path,
        default=Path(config.OUTPUT_DIR) / "meshes" / "buildings",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-pack-manifest", type=Path)
    source.add_argument(
        "--source-terrain-inventory",
        type=Path,
        help=(
            "Authoritative extractor inventory used when a downstream immutable "
            "production pack has not yet been assembled"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Explicit disk-backed publication path; /tmp, /dev/shm, and /run are rejected.",
    )
    parser.add_argument("--progress-every", type=int, default=2000)
    return parser.parse_args()


def reject_volatile_output(path: Path) -> None:
    for root in VOLATILE_OUTPUT_ROOTS:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        raise ValueError(
            f"large residency output may not use volatile location {root}: {path}"
        )


def sha256_file(path: Path, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def load_source_pack(path: Path) -> tuple[dict[str, Any], str, list[str]]:
    if not path.is_file():
        raise ValueError(f"source pack manifest does not exist: {path}")
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema") != "vanguard_world_residency_pack":
        raise ValueError(f"unsupported source pack schema: {manifest.get('schema')!r}")
    if manifest.get("publication_class") != "production":
        raise ValueError("interior publication requires a production source pack")
    chunks = manifest.get("build_contract", {}).get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("source pack build contract has no chunks")
    normalized = sorted({str(chunk).strip() for chunk in chunks})
    if len(normalized) != len(chunks) or any(not value for value in normalized):
        raise ValueError("source pack chunk list is empty or contains duplicates")
    return manifest, hashlib.sha256(raw).hexdigest(), normalized


def load_source_terrain_inventory(
    path: Path,
) -> tuple[dict[str, Any], str, list[str]]:
    if not path.is_file():
        raise ValueError(f"source terrain inventory does not exist: {path}")
    raw = path.read_bytes()
    inventory = json.loads(raw)
    if inventory.get("schema") != "vanguard_source_terrain_inventory":
        raise ValueError(
            f"unsupported source terrain inventory schema: {inventory.get('schema')!r}"
        )
    if inventory.get("generated_inputs_complete") is not True:
        raise ValueError("interior publication requires a complete source terrain inventory")
    records = inventory.get("chunks")
    if not isinstance(records, list) or not records:
        raise ValueError("source terrain inventory has no chunks")
    chunks = [str(record.get("chunk", "")).strip() for record in records if isinstance(record, dict)]
    normalized = sorted(set(chunks))
    if len(chunks) != len(records) or len(normalized) != len(chunks) or any(not value for value in chunks):
        raise ValueError("source terrain inventory chunk list is invalid or duplicated")
    if int(inventory.get("chunk_count", -1)) != len(normalized):
        raise ValueError("source terrain inventory chunk_count does not match its records")
    return inventory, hashlib.sha256(raw).hexdigest(), normalized


def load_placed_prefab_names(
    chunks: Iterable[str], terrain_grid_root: Path
) -> tuple[set[str], list[dict[str, Any]]]:
    names: set[str] = set()
    sources: list[dict[str, Any]] = []
    for chunk in chunks:
        path = terrain_grid_root / f"{chunk}_sgo.json"
        if not path.is_file():
            raise ValueError(f"missing SGO chunk sidecar: {path}")
        raw = path.read_bytes()
        sidecar = json.loads(raw)
        if not isinstance(sidecar, dict):
            raise ValueError(f"SGO sidecar is not an object: {path}")
        chunk_names = sorted(str(value).strip() for value in sidecar)
        if any(not value for value in chunk_names):
            raise ValueError(f"SGO sidecar contains an empty prefab name: {path}")
        names.update(chunk_names)
        sources.append(
            {
                "bytes": len(raw),
                "chunk": chunk,
                "prefab_template_count": len(chunk_names),
                "relative_path": f"terrain/terrain_grid/{chunk}_sgo.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return names, sources


def stream_prefab_graph(
    sgo_raw: Path, *, progress_every: int
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not sgo_raw.is_file():
        raise ValueError(f"raw SGO JSONL does not exist: {sgo_raw}")
    prefabs: dict[str, dict[str, Any]] = {}
    by_fold: dict[str, str] = {}
    source_digest = hashlib.sha256()
    record_count = 0
    export_count = 0
    with sgo_raw.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                raise ValueError(f"blank line in raw SGO JSONL at {line_number}")
            source_digest.update(raw_line)
            record = json.loads(raw_line)
            name = prefab_name_from_raw_record(record)
            folded = name.casefold()
            prior = by_fold.get(folded)
            if prior is not None:
                raise ValueError(f"duplicate case-insensitive SGO prefab names: {prior}, {name}")
            refs = compact_compound_refs(record)
            entry = {
                "compound_refs": refs,
                "record_index": int(record.get("index", record_count)),
                "record_sha256": hashlib.sha256(raw_line).hexdigest(),
                "source_prefab_id": source_prefab_id(name),
            }
            prefabs[name] = entry
            by_fold[folded] = name
            record_count += 1
            export_count += len(record.get("exports", []))
            if progress_every and record_count % progress_every == 0:
                print(f"  SGO graph pass: {record_count:,} records", file=sys.stderr)
    if not record_count:
        raise ValueError(f"raw SGO JSONL is empty: {sgo_raw}")
    return prefabs, {
        "bytes": sgo_raw.stat().st_size,
        "export_count": export_count,
        "record_count": record_count,
        "relative_path": "data/sgo_raw.jsonl",
        "sha256": source_digest.hexdigest(),
    }


def _safe_source_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite actor property: {value!r}")
        return value
    if isinstance(value, list):
        if len(value) > 256:
            return {"omitted_element_count": len(value)}
        return [_safe_source_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in sorted(value.items()):
            if str(key) in {"raw_hex", "bytes"}:
                continue
            result[str(key)] = _safe_source_value(item, depth=depth + 1)
        return result
    return str(value)


def _object_ref_for_property(export: dict[str, Any], property_name: str) -> dict[str, Any] | None:
    for prop in reversed(export.get("props", [])):
        if not isinstance(prop, dict) or prop.get("name") != property_name:
            continue
        value = prop.get("object_ref")
        if not isinstance(value, dict):
            return None
        allowed = (
            "class_name",
            "class_package",
            "kind",
            "name",
            "object_path",
            "package_path",
            "source_package",
        )
        return {key: value[key] for key in allowed if value.get(key) is not None}
    return None


def compact_actors(record: dict[str, Any]) -> list[dict[str, Any]]:
    prefab = prefab_name_from_raw_record(record)
    actors: list[dict[str, Any]] = []
    for export in record.get("exports", []):
        class_name = str(export.get("class", "")).strip()
        if not class_name or class_name == "CompoundObject":
            continue
        name = str(export.get("name", "")).strip()
        if not name:
            raise ValueError(f"{prefab} has an unnamed {class_name} export")
        props = properties_by_name(export)
        transform = AffineTransform.from_properties(props)
        retained_props = {
            key: _safe_source_value(value)
            for key, value in sorted(props.items())
            if key in PHASE8_PROPERTY_NAMES and value is not None
        }
        actor: dict[str, Any] = {
            "class": class_name,
            "name": name,
            "properties": retained_props,
            "source_component_path": source_component_path(prefab, class_name, name),
            "transform": transform.serialize(),
            "_transform": transform,
        }
        mesh_ref = _object_ref_for_property(export, "StaticMesh")
        if props.get("StaticMesh") is not None:
            actor["static_mesh"] = str(props["StaticMesh"])
        if mesh_ref:
            actor["static_mesh_source"] = mesh_ref
        for sound_property in (
            "AmbientSound",
            "ClosedSound",
            "ClosingSound",
            "OpeningSound",
        ):
            sound_ref = _object_ref_for_property(export, sound_property)
            if sound_ref:
                actor.setdefault("sound_sources", {})[sound_property] = sound_ref
        actors.append(actor)
    actors.sort(key=lambda value: value["source_component_path"])
    return actors


def stream_needed_actors(
    sgo_raw: Path,
    prefabs: dict[str, dict[str, Any]],
    needed: set[str],
    *,
    progress_every: int,
) -> dict[str, int]:
    needed_by_fold = {name.casefold(): name for name in needed}
    found: set[str] = set()
    class_counts: Counter[str] = Counter()
    with sgo_raw.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            record = json.loads(raw_line)
            raw_name = prefab_name_from_raw_record(record)
            name = needed_by_fold.get(raw_name.casefold())
            if name is None:
                continue
            if hashlib.sha256(raw_line).hexdigest() != prefabs[name]["record_sha256"]:
                raise ValueError(f"SGO record changed between streaming passes: {name}")
            actors = compact_actors(record)
            prefabs[name]["actors"] = actors
            found.add(name)
            class_counts.update(str(actor["class"]) for actor in actors)
            if progress_every and len(found) % progress_every == 0:
                print(
                    f"  SGO actor pass: retained {len(found):,}/{len(needed):,} prefabs",
                    file=sys.stderr,
                )
    missing = sorted(needed - found)
    if missing:
        raise ValueError(f"actor pass missed {len(missing)} required prefabs: {missing[:8]}")
    return dict(sorted(class_counts.items()))


def _resolved_prefab_name(
    requested: str,
    prefabs: dict[str, dict[str, Any]],
    by_fold: dict[str, str] | None = None,
) -> str:
    by_fold = by_fold or {name.casefold(): name for name in prefabs}
    result = by_fold.get(requested.casefold())
    if result is None:
        raise ValueError(f"referenced SGO prefab is missing: {requested}")
    return result


def assemble_template(
    root: str,
    prefabs: dict[str, dict[str, Any]],
    aperture_library: PortalApertureLibrary,
    by_fold: dict[str, str],
) -> dict[str, Any]:
    root = _resolved_prefab_name(root, prefabs, by_fold)
    rooms = discover_rooms(root, prefabs, by_fold=by_fold)
    if not rooms:
        raise ValueError(f"interior root unexpectedly has no rooms: {root}")
    all_visual_paths: set[str] = set()
    for room in rooms:
        categorized: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for actor in walk_room_actors(room, prefabs, by_fold=by_fold):
            category = actor_category(actor["class"])
            if category == "portals":
                try:
                    mesh, aperture = aperture_library.transformed(actor)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    actor["aperture_status"] = "unavailable"
                    actor["aperture_unavailable_reason"] = str(error)
                else:
                    actor["aperture_mesh_id"] = mesh["aperture_mesh_id"]
                    actor["aperture_geometry"] = aperture
                    actor["aperture_status"] = "exact"
            properties = actor.get("properties", {})
            if category == "visual_components" and not isinstance(
                actor.get("static_mesh_source"), dict
            ):
                # A StaticMeshActor export without a resolved StaticMesh is not
                # a renderable placement and cannot be replaced by a room pack.
                category = "deferred_components"
            if category == "visual_components" and (
                properties.get("bHidden") is True
                or properties.get("bHiddenEd") is True
            ):
                # Authored invisible walls/blocks are physics authority.  They
                # must never enter a visual page merely because their source
                # class is StaticMeshActor.
                explicitly_non_colliding = (
                    properties.get("bCollideActors") is False
                    and not any(
                        properties.get(flag) is True
                        for flag in (
                            "bBlockActors",
                            "bBlockKarma",
                            "bBlockNonZeroExtentTraces",
                            "bBlockPlayers",
                            "bBlockZeroExtentTraces",
                        )
                    )
                )
                category = (
                    "deferred_components"
                    if explicitly_non_colliding
                    else "collision_only_components"
                )
            categorized[category].append(actor)
        for values in categorized.values():
            values.sort(key=lambda value: value["source_component_path"])
        room.update(categorized)
        visual_paths = [
            str(component["source_component_path"])
            for component in room.get("visual_components", [])
        ]
        overlap = all_visual_paths.intersection(visual_paths)
        if overlap:
            raise ValueError(f"room visual component ownership overlaps in {root}: {sorted(overlap)[:4]}")
        all_visual_paths.update(visual_paths)
        room["collision_components"] = []
        for role, components in (
            ("visual_and_collision", room.get("visual_components", [])),
            ("collision_only", room.get("collision_only_components", [])),
        ):
            room["collision_components"].extend(
                {
                    "source_component_path": component["source_component_path"],
                    "static_mesh": component.get("static_mesh"),
                    "static_mesh_source": component.get("static_mesh_source"),
                    "visibility_role": role,
                }
                for component in components
            )
        room["collision_components"].sort(
            key=lambda value: value["source_component_path"]
        )
        room["counts"] = {
            key.removesuffix("s").removesuffix("_component") + "_count": len(values)
            for key, values in sorted(categorized.items())
        }

    portal_graph = assemble_portal_graph(rooms)
    transition_actors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for actor in walk_nonroom_actors(root, prefabs, by_fold=by_fold):
        category = actor_category(actor["class"])
        if category not in {"movers", "triggers", "audio", "portals"}:
            continue
        transition_actors[category].append(actor)
    for category in transition_actors:
        transition_actors[category].sort(
            key=lambda value: value["source_component_path"]
        )

    target_by_position: dict[tuple[float, float, float], list[tuple[str, str]]] = (
        defaultdict(list)
    )
    for connection in portal_graph["connections"]:
        target_by_position[tuple(connection["position"])].append(
            ("connection", str(connection["connection_id"]))
        )
    for boundary in portal_graph["boundaries"]:
        target_by_position[tuple(boundary["position"])].append(
            ("boundary", str(boundary["boundary_id"]))
        )
    mover_bindings: list[dict[str, Any]] = []
    for mover in transition_actors.get("movers", []):
        transform = mover.get("_transform")
        if not isinstance(transform, AffineTransform):
            raise ValueError(f"{root} transition mover lacks an affine transform")
        position = tuple(round(value, 6) for value in transform.origin)
        targets = target_by_position.get(position, [])
        mover_id = sha256_id(
            "mover",
            {
                "root_prefab": root,
                "source_component_path": mover["source_component_path"],
            },
        )
        mover["mover_id"] = mover_id
        mover["portal_binding_status"] = (
            "unique_exact_source_position" if len(targets) == 1 else "unbound"
        )
        if len(targets) == 1:
            target_kind, target_id = targets[0]
            mover_bindings.append(
                {
                    "mover_id": mover_id,
                    "target_id": target_id,
                    "target_kind": target_kind,
                }
            )
    mover_bindings.sort(key=lambda value: value["mover_id"])
    boundary_room_ids = sorted({str(value["room_id"]) for value in portal_graph["boundaries"]})
    unavailable_aperture_endpoint_ids = sorted(
        str(value["endpoint_id"])
        for value in portal_graph["endpoints"]
        if value.get("aperture_status") != "exact"
    )
    unresolved_room_ids = sorted(
        {
            str(room_id)
            for cluster in portal_graph["unresolved"]
            for room_id in cluster["room_ids"]
        }
    )
    identity = {
        "root_prefab": root,
        "source_prefab_id": source_prefab_id(root),
        "room_source_paths": [room["source_component_path"] for room in rooms],
    }
    template = {
        "entrance_room_ids": boundary_room_ids,
        "interior_collision_component_paths": sorted(
            str(component["source_component_path"])
            for room in rooms
            for component in room.get("collision_components", [])
        ),
        "interior_component_paths": sorted(all_visual_paths),
        "interior_space_asset_id": sha256_id("interior_space", identity),
        "portal_graph": portal_graph,
        "root_prefab": root,
        "room_count": len(rooms),
        "rooms": rooms,
        "runtime_eligibility": {
            "eligible": (
                bool(boundary_room_ids)
                and not portal_graph["unresolved"]
                and not unavailable_aperture_endpoint_ids
            ),
            "reason": (
                "eligible"
                if (
                    boundary_room_ids
                    and not portal_graph["unresolved"]
                    and not unavailable_aperture_endpoint_ids
                )
                else "no_authored_boundary_endpoint"
                if not boundary_room_ids
                else "unavailable_portal_aperture"
                if unavailable_aperture_endpoint_ids
                else "unresolved_portal_cluster"
            ),
            "unavailable_aperture_endpoint_ids": unavailable_aperture_endpoint_ids,
            "unresolved_room_ids": unresolved_room_ids,
        },
        "source_assembly_policy": SOURCE_ASSEMBLY_POLICY,
        "source_prefab_id": source_prefab_id(root),
        "transition_actors": dict(sorted(transition_actors.items())),
        "transition_mover_bindings": mover_bindings,
    }
    return strip_private(template)


def _string_at(strings: list[Any], index: Any, label: str) -> str:
    if not isinstance(index, int) or not 0 <= index < len(strings):
        raise ValueError(f"invalid {label} string index: {index!r}")
    value = str(strings[index]).strip()
    if not value:
        raise ValueError(f"empty {label} string")
    return value


def load_interior_instances(
    chunks: Iterable[str],
    object_index_root: Path,
    templates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    template_by_fold = {str(value["root_prefab"]).casefold(): value for value in templates}
    room_paths_by_template = {
        key: {
            str(room["room_id"]): set(
                str(component["source_component_path"])
                for component in room.get("visual_components", [])
            )
            for room in value["rooms"]
        }
        for key, value in template_by_fold.items()
    }
    instances: dict[tuple[Any, ...], dict[str, Any]] = {}
    bound_paths_by_instance: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    bound_records_by_instance: dict[
        tuple[Any, ...], dict[str, dict[str, str]]
    ] = defaultdict(dict)
    sources: list[dict[str, Any]] = []
    record_count = 0
    matching_record_count = 0
    for chunk in chunks:
        path = object_index_root / chunk / "object_cells.json"
        if not path.is_file():
            raise ValueError(f"missing compact object index: {path}")
        raw = path.read_bytes()
        payload = json.loads(raw)
        if payload.get("record_format") != EXPECTED_RECORD_FORMAT:
            raise ValueError(f"unsupported placement record format in {path}")
        if str(payload.get("chunk", "")) != chunk:
            raise ValueError(f"object index chunk mismatch in {path}")
        strings = payload.get("strings")
        assets = payload.get("assets")
        cells = payload.get("cells")
        if (
            not isinstance(strings, list)
            or not isinstance(assets, list)
            or not isinstance(cells, dict)
        ):
            raise ValueError(f"object index lacks strings/cells: {path}")
        chunk_origin = payload.get("chunk_global_origin")
        if not isinstance(chunk_origin, list) or len(chunk_origin) != 3:
            raise ValueError(f"object index lacks chunk_global_origin: {path}")
        for cell in cells.values():
            if not isinstance(cell, dict):
                raise ValueError(f"invalid cell record in {path}")
            records = cell.get("placement_records", [])
            if not isinstance(records, list):
                raise ValueError(f"invalid placement_records in {path}")
            for record in records:
                record_count += 1
                if not isinstance(record, list) or len(record) != len(EXPECTED_RECORD_FORMAT):
                    raise ValueError(f"invalid placement record in {path}")
                # Direct map actors have no prefab template.  Version 5 records
                # encode that optional string reference as -1.
                if record[4] == -1:
                    continue
                prefab = _string_at(strings, record[4], "prefab")
                template = template_by_fold.get(prefab.casefold())
                if template is None:
                    continue
                matching_record_count += 1
                object_id = _string_at(strings, record[9], "source object")
                node_id = _string_at(strings, record[10], "source node")
                component_path = _string_at(strings, record[11], "component path")
                asset_index = int(record[0])
                if not 0 <= asset_index < len(assets) or not isinstance(
                    assets[asset_index], dict
                ):
                    raise ValueError(f"invalid placement asset index in {path}")
                mesh_path = str(assets[asset_index].get("mesh_path", "")).replace(
                    "\\", "/"
                )
                asset_id = str(assets[asset_index].get("asset_id", ""))
                if not mesh_path:
                    raise ValueError(f"placement asset lacks exact identity in {path}")
                node_index = record[1]
                key = (chunk, node_index, object_id, node_id, prefab.casefold())
                transform = {
                    "translation": record[5],
                    "rotation_quaternion": record[6],
                    "scale": record[7],
                }
                prior = instances.get(key)
                if prior is None:
                    identity = {
                        "authoritative_source_node_id": node_id,
                        "authoritative_source_object_id": object_id,
                        "chunk": chunk,
                        "node_index": node_index,
                    }
                    instances[key] = {
                        "authoritative_source_node_id": node_id,
                        "authoritative_source_object_id": object_id,
                        "chunk": chunk,
                        "chunk_global_origin": chunk_origin,
                        "interior_instance_id": sha256_id("interior_instance", identity),
                        "interior_space_asset_id": template["interior_space_asset_id"],
                        "node_index": node_index,
                        "root_prefab": template["root_prefab"],
                        "root_transform": transform,
                    }
                elif prior["root_transform"] != transform:
                    raise ValueError(f"inconsistent root transform for {key}")
                bound_paths_by_instance[key].add(component_path)
                binding = {"asset_id": asset_id, "mesh_path": mesh_path}
                previous_binding = bound_records_by_instance[key].get(component_path)
                if previous_binding is not None and previous_binding != binding:
                    raise ValueError(
                        f"component path resolves to multiple placement assets: {key} "
                        f"{component_path}"
                    )
                bound_records_by_instance[key][component_path] = binding
        sources.append(
            {
                "bytes": len(raw),
                "chunk": chunk,
                "placement_record_count": int(payload.get("placement_record_count", 0)),
                "relative_path": f"godot_runtime/chunks/{chunk}/object_cells.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    ordered: list[dict[str, Any]] = []
    expected_instance_counts: Counter[str] = Counter()
    resolved_instance_counts: Counter[str] = Counter()
    missing_bindings: list[dict[str, Any]] = []
    for key, instance in sorted(instances.items(), key=lambda value: value[1]["interior_instance_id"]):
        template_key = str(instance["root_prefab"]).casefold()
        available = bound_paths_by_instance[key]
        room_bindings = []
        for room_id, expected in sorted(room_paths_by_template[template_key].items()):
            present = sorted(expected.intersection(available))
            missing = sorted(expected - available)
            expected_instance_counts[template_key] += len(expected)
            resolved_instance_counts[template_key] += len(present)
            room_bindings.append(
                {
                    "available_visual_components": [
                        {
                            **bound_records_by_instance[key][path],
                            "source_component_path": path,
                        }
                        for path in present
                    ],
                    "available_visual_component_paths": present,
                    "missing_visual_component_paths": missing,
                    "room_id": room_id,
                }
            )
            if missing:
                missing_bindings.append(
                    {
                        "interior_instance_id": instance["interior_instance_id"],
                        "missing_count": len(missing),
                        "room_id": room_id,
                    }
                )
        missing_visual_component_paths = sorted(
            str(path)
            for binding in room_bindings
            for path in binding["missing_visual_component_paths"]
        )
        instance["room_visual_bindings"] = room_bindings
        instance["runtime_eligibility"] = instance_runtime_eligibility(
            template_by_fold[template_key], missing_visual_component_paths
        )
        instance["source_component_record_count"] = len(available)
        ordered.append(instance)

    template_instance_counts = Counter(str(value["root_prefab"]).casefold() for value in ordered)
    missing_templates = sorted(
        value["root_prefab"]
        for key, value in template_by_fold.items()
        if template_instance_counts[key] == 0
    )
    audit = {
        "missing_room_visual_binding_count": sum(value["missing_count"] for value in missing_bindings),
        "missing_room_visual_bindings": missing_bindings,
        "missing_template_instance_count": len(missing_templates),
        "missing_template_instances": missing_templates,
        "object_placement_record_count": record_count,
        "room_visual_binding_count": sum(resolved_instance_counts.values()),
        "room_visual_binding_expected_count": sum(expected_instance_counts.values()),
        "room_visual_matching_placement_record_count": matching_record_count,
    }
    return ordered, sources, audit


def instance_runtime_eligibility(
    template: dict[str, Any], missing_visual_component_paths: list[str]
) -> dict[str, Any]:
    template_eligible = template.get("runtime_eligibility", {}).get("eligible") is True
    missing = sorted(set(missing_visual_component_paths))
    eligible = template_eligible and not missing
    return {
        "eligible": eligible,
        "missing_visual_component_paths": missing,
        "reason": (
            "eligible"
            if eligible
            else "template_ineligible"
            if not template_eligible
            else "unavailable_room_visual_binding"
        ),
    }


def _xyz(value: Any) -> list[float]:
    return [float(value.x), float(value.y), float(value.z)]


def _plane(value: Any) -> list[float]:
    return [float(value.x), float(value.y), float(value.z), float(value.w)]


def _mask(value: int) -> dict[str, Any]:
    unsigned = int(value) & ((1 << 64) - 1)
    return {
        "hex": f"0x{unsigned:016x}",
        "zone_indices": [index for index in range(64) if unsigned & (1 << index)],
    }


def _object_reference(package: Any, value: int) -> dict[str, Any] | None:
    if value == 0:
        return None
    if value > 0:
        record = package.exports[value - 1]
        return {
            "class": str(record.get("class_name", "")),
            "kind": "export",
            "name": str(record.get("object_name", "")),
            "reference": value,
        }
    record = package.imports[-value - 1]
    return {
        "class": str(record.get("class_name", "")),
        "kind": "import",
        "name": str(record.get("object_name", record.get("name", ""))),
        "reference": value,
    }


def extract_bsp_authority(chunks: Iterable[str], maps_root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for number, chunk in enumerate(chunks, 1):
        source_path = maps_root / f"{chunk}.vgr"
        if not source_path.is_file():
            raise ValueError(f"missing source map package: {source_path}")
        print(f"  BSP authority: {number} {chunk}", file=sys.stderr)
        package = UE2Package(str(source_path))
        levels = [value for value in package.exports if value.get("class_name") == "Level"]
        if len(levels) != 1:
            raise ValueError(f"{chunk} has {len(levels)} Level exports; expected one")
        model_ref = find_level_model_reference(package, levels[0])
        model_export = package.exports[model_ref - 1]
        model = parse_model_export(package, model_export)
        nodes = []
        for index, node in enumerate(model.nodes):
            nodes.append(
                {
                    "back_node_index": node.i_back,
                    "collision_leaf_hull_offset": node.i_collision_bound,
                    "exclusive_sphere_bound": _plane(node.exclusive_sphere_bound),
                    "front_node_index": node.i_front,
                    "inclusive_sphere_bound": _plane(node.inclusive_sphere_bound),
                    "leaf_indices": node.i_leaf,
                    "node_flags": node.node_flags,
                    "node_index": index,
                    "plane": _plane(node.plane),
                    "plane_node_index": node.i_plane,
                    "render_bound_index": node.i_render_bound,
                    "surface_index": node.i_surf,
                    "zone_indices": node.i_zone,
                    "zone_mask": _mask(node.zone_mask),
                }
            )
        zones = []
        for index, zone in enumerate(model.zones):
            zones.append(
                {
                    "connectivity": _mask(zone.connectivity),
                    "visibility": _mask(zone.visibility),
                    "zone_actor": _object_reference(package, zone.zone_actor),
                    "zone_index": index,
                }
            )
        leaves = []
        for index, leaf in enumerate(model.leaves):
            leaves.append(
                {
                    "leaf_index": index,
                    "permeating_object_ref": leaf.i_permeating,
                    "visible_zones": _mask(leaf.visible_zones),
                    "volumetric_object_ref": leaf.i_volumetric,
                    "zone_index": leaf.i_zone,
                }
            )
        bounds = [
            {
                "bound_index": index,
                "maximum": _xyz(bound.maximum),
                "minimum": _xyz(bound.minimum),
                "valid": bound.valid,
            }
            for index, bound in enumerate(model.bounds)
        ]
        results.append(
            {
                "archive_version": int(package.version),
                "bounding_box": {
                    "maximum": _xyz(model.bounding_box.maximum),
                    "minimum": _xyz(model.bounding_box.minimum),
                    "valid": model.bounding_box.valid,
                },
                "bound_count": len(bounds),
                "bounds": bounds,
                "chunk": chunk,
                "extension_tail_bytes": model.extension_tail_bytes,
                "extension_tail_sha256": model.extension_tail_sha256,
                "leaf_count": len(leaves),
                "leaf_hull_count": len(model.leaf_hulls),
                "leaf_hulls": model.leaf_hulls,
                "leaves": leaves,
                "licensee_version": int(package.licensee),
                "linked": model.linked,
                "model_export_index": model_ref,
                "model_export_name": str(model_export.get("object_name", "")),
                "model_source_id": (
                    f"ue2://Maps/{source_path.name}#Export/Model/"
                    f"{model_export.get('object_name', '')}"
                ),
                "node_count": len(nodes),
                "nodes": nodes,
                "root_outside": model.root_outside,
                "source_package_bytes": source_path.stat().st_size,
                "source_package_relative_path": f"Maps/{source_path.name}",
                "source_package_sha256": sha256_file(source_path),
                "surface_count": len(model.surfaces),
                "zone_count": len(zones),
                "zones": zones,
            }
        )
    return results


def build_publication(
    *,
    maps_root: Path,
    sgo_raw: Path,
    terrain_grid_root: Path,
    object_index_root: Path,
    mesh_root: Path,
    source_pack_manifest: Path | None,
    source_terrain_inventory: Path | None,
    progress_every: int,
) -> dict[str, Any]:
    if (source_pack_manifest is None) == (source_terrain_inventory is None):
        raise ValueError("exactly one source pack or source terrain inventory is required")
    source_pack: dict[str, Any] | None = None
    source_inventory: dict[str, Any] | None = None
    source_manifest_sha256 = ""
    if source_pack_manifest is not None:
        source_pack, source_manifest_sha256, chunks = load_source_pack(source_pack_manifest)
    else:
        assert source_terrain_inventory is not None
        source_inventory, source_manifest_sha256, chunks = load_source_terrain_inventory(
            source_terrain_inventory
        )
    placed_prefabs, sidecar_sources = load_placed_prefab_names(chunks, terrain_grid_root)
    print(
        f"Streaming compact SGO graph for {len(placed_prefabs):,} placed prefab names...",
        file=sys.stderr,
    )
    prefabs, sgo_source = stream_prefab_graph(sgo_raw, progress_every=progress_every)
    by_fold = {name.casefold(): name for name in prefabs}
    absent_placed = sorted(name for name in placed_prefabs if name.casefold() not in by_fold)
    if absent_placed:
        raise ValueError(
            f"{len(absent_placed)} placed prefab templates are missing from raw SGO: "
            f"{absent_placed[:8]}"
        )
    room_presence: dict[str, bool] = {}
    interior_roots = sorted(
        by_fold[name.casefold()]
        for name in placed_prefabs
        if root_has_room(
            by_fold[name.casefold()],
            prefabs,
            by_fold=by_fold,
            result_cache=room_presence,
        )
    )
    if not interior_roots:
        raise ValueError("no placed prefab roots contain authored rooms")
    closure = prefab_closure(interior_roots, prefabs, by_fold=by_fold)
    print(
        f"Streaming actors for {len(closure):,} prefabs in {len(interior_roots):,} interior roots...",
        file=sys.stderr,
    )
    retained_class_counts = stream_needed_actors(
        sgo_raw,
        prefabs,
        closure,
        progress_every=progress_every,
    )
    aperture_library = PortalApertureLibrary(mesh_root)
    templates = [
        assemble_template(root, prefabs, aperture_library, by_fold)
        for root in interior_roots
    ]
    templates.sort(key=lambda value: value["interior_space_asset_id"])
    instances, object_sources, binding_audit = load_interior_instances(
        chunks, object_index_root, templates
    )
    if not instances:
        raise ValueError("no placed interior instances were found in compact object indices")
    bsp = extract_bsp_authority(chunks, maps_root)

    counts = {
        "bsp_bound_count": sum(value["bound_count"] for value in bsp),
        "bsp_leaf_count": sum(value["leaf_count"] for value in bsp),
        "bsp_leaf_hull_count": sum(value["leaf_hull_count"] for value in bsp),
        "bsp_node_count": sum(value["node_count"] for value in bsp),
        "bsp_zone_count": sum(value["zone_count"] for value in bsp),
        "interior_instance_count": len(instances),
        "interior_template_count": len(templates),
        "placed_prefab_template_count": len(placed_prefabs),
        "portal_boundary_count": sum(len(value["portal_graph"]["boundaries"]) for value in templates),
        "portal_connection_count": sum(len(value["portal_graph"]["connections"]) for value in templates),
        "portal_endpoint_count": sum(len(value["portal_graph"]["endpoints"]) for value in templates),
        "portal_aperture_mesh_count": len(aperture_library.catalog()),
        "portal_aperture_unavailable_endpoint_count": sum(
            1
            for template in templates
            for endpoint in template["portal_graph"]["endpoints"]
            if endpoint.get("aperture_status") != "exact"
        ),
        "room_count": sum(value["room_count"] for value in templates),
        "runtime_eligible_template_count": sum(
            1 for value in templates if value["runtime_eligibility"]["eligible"]
        ),
        "runtime_eligible_instance_count": sum(
            1 for value in instances if value["runtime_eligibility"]["eligible"]
        ),
        "unavailable_room_visual_binding_count": sum(
            len(value["runtime_eligibility"]["missing_visual_component_paths"])
            for value in instances
        ),
        "unresolved_portal_cluster_count": sum(
            len(value["portal_graph"]["unresolved"]) for value in templates
        ),
    }
    source_contract: dict[str, Any] = {
        "chunk_count": len(chunks),
        "chunks": chunks,
        "object_indices": object_sources,
        "sgo_chunk_sidecars": sidecar_sources,
        "sgo_raw": sgo_source,
    }
    if source_pack is not None:
        source_contract["source_pack"] = {
            "manifest_relative_path": "pack_manifest.json",
            "manifest_sha256": source_manifest_sha256,
            "pack_id": source_pack["pack_id"],
            "publication_class": source_pack["publication_class"],
        }
    else:
        assert source_inventory is not None
        source_contract["source_terrain_inventory"] = {
            "chunk_count": len(chunks),
            "inventory_id": source_inventory.get("inventory_id"),
            "relative_path": "world_residency/source_terrain_inventory.json",
            "sha256": source_manifest_sha256,
        }
    audit = {
        "binding": binding_audit,
        "native_bsp_chunk_count": len(bsp),
        "native_bsp_errors": [],
        "portal_ambiguity_policy": "retain_unresolved_and_forbid_runtime_exposure_v1",
        "retained_actor_class_counts": retained_class_counts,
        "retained_prefab_closure_count": len(closure),
        "source_authority": {
            "exterior_zones": "native_map_umodel",
            "interior_rooms": "native_sgo_compound_type_3",
            "portal_endpoints": "native_sgo_portal_actor",
            "portal_relationships": "deterministic_unique_coincident_source_assembly",
        },
    }
    identity_payload = {
        "audit": audit,
        "bsp_authority": bsp,
        "counts": counts,
        "instances": instances,
        "interior_templates": templates,
        "portal_aperture_meshes": aperture_library.catalog(),
        "schema": SCHEMA,
        "source_assembly_policy": SOURCE_ASSEMBLY_POLICY,
        "source_contract": source_contract,
        "version": VERSION,
    }
    revision = hashlib.sha256(canonical_json_bytes(identity_payload)).hexdigest()
    return {
        **identity_payload,
        "content_revision": f"sha256:{revision}",
        "publication_id": f"interior_source_publication_{revision[:32]}",
    }


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        candidate.write_bytes(canonical_json_bytes(value) + b"\n")
        os.replace(candidate, path)
    finally:
        if candidate.exists():
            candidate.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
