#!/usr/bin/env python3
"""Publish exact Phase 9 effect, light, and grass source authority.

The particle cell generator owns effect placement topology.  This companion
publication binds that output to the production chunk contract, maps every
room-owned effect to its Phase 8 interior instance/room, derives exterior
lights while stopping at authored room subtrees, and records conservative
grass authority.  Missing grass density is an explicit disabled state; it is
never replaced by procedural or cross-chunk data.

The output is intentionally a single source-authority document.  The client
pack publisher subsequently normalizes templates and writes bounded pages.
Large output must be an explicit disk-backed path; volatile Linux locations
are rejected.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from scripts.generators.generate_particle_cell_index import (  # noqa: E402
    add_vector,
    chunk_global_origin,
    compact_node_transform,
    discover_placed_root_prefabs,
    load_compound_prefab_graph,
    read_json,
    source_affine_transform_gltf_position,
    transform_local_position,
)
from scripts.lib.world_residency_interiors import (  # noqa: E402
    AffineTransform,
    canonical_json_bytes,
    sha256_id,
    source_component_path,
    walk_nonroom_actors,
)


SCHEMA = "vanguard_world_activity_source_publication"
VERSION = 1
POLICY = "exact_effect_topology_exterior_light_and_grass_authority_v1"
EFFECT_POLICY = "source_complete_compound_effect_topology_v1"
LIGHT_POLICY = "placed_sgo_light_graph_excluding_authored_room_subtrees_v1"
GRASS_POLICY = "exact_density_or_explicit_disabled_no_cross_chunk_fallback_v1"
ROOM_EFFECT_POLICY = "phase8_instance_room_source_path_exact_join_v1"
VOLATILE_OUTPUT_ROOTS = (Path("/tmp"), Path("/dev/shm"), Path("/run"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-pack-manifest", required=True, type=Path)
    parser.add_argument("--effect-index-root", required=True, type=Path)
    parser.add_argument("--interior-source-publication", required=True, type=Path)
    parser.add_argument("--grass-materials", required=True, type=Path)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(config.OUTPUT_DIR),
        help="Extractor output root containing terrain, data, textures, and meshes.",
    )
    parser.add_argument(
        "--sgo-raw",
        type=Path,
        default=Path(config.OUTPUT_DIR) / "data" / "sgo_raw.jsonl",
    )
    parser.add_argument(
        "--light-index",
        type=Path,
        default=Path(config.OUTPUT_DIR) / "data" / "sgo_by_class" / "sgo_lights.json",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        publication = build_publication(
            source_pack_manifest=args.source_pack_manifest.expanduser().resolve(),
            effect_index_root=args.effect_index_root.expanduser().resolve(),
            interior_source_publication=(
                args.interior_source_publication.expanduser().resolve()
            ),
            grass_materials=args.grass_materials.expanduser().resolve(),
            output_root=args.output_root.expanduser().resolve(),
            sgo_raw=args.sgo_raw.expanduser().resolve(),
            light_index=args.light_index.expanduser().resolve(),
        )
        write_publication(args.output.expanduser().resolve(), publication)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    counts = publication["counts"]
    print(
        "World residency activity sources: "
        f"effects={counts['effect_placement_count']} "
        f"room_effects={counts['room_effect_placement_count']} "
        f"exterior_lights={counts['exterior_light_count']} "
        f"interior_lights={counts['interior_light_instance_count']} "
        f"grass_density_chunks={counts['exact_grass_density_chunk_count']} "
        f"publication={publication['publication_id']} output={args.output}"
    )
    return 0


def build_publication(
    *,
    source_pack_manifest: Path,
    effect_index_root: Path,
    interior_source_publication: Path,
    grass_materials: Path,
    output_root: Path,
    sgo_raw: Path,
    light_index: Path,
) -> dict[str, Any]:
    source_pack, source_pack_bytes = _read_object_bytes(
        source_pack_manifest, "source pack manifest"
    )
    if (
        source_pack.get("schema") != "vanguard_world_residency_pack"
        or source_pack.get("publication_class") != "production"
    ):
        raise ValueError("activity source publication requires a production source pack")
    chunks = _canonical_chunks(source_pack.get("build_contract", {}).get("chunks"))
    source_pack_id = str(source_pack.get("pack_id", ""))
    if not source_pack_id:
        raise ValueError("source pack identity is empty")

    effect_global_path = effect_index_root / "global_particle_cells.json"
    effect_global, effect_global_bytes = _read_object_bytes(
        effect_global_path, "global effect index"
    )
    if (
        int(effect_global.get("version", 0)) < 14
        or effect_global.get("chunks") != chunks
        or int(effect_global.get("chunk_count", -1)) != len(chunks)
        or int(effect_global.get("missing_chunk_count", -1)) != 0
        or int(effect_global.get("unresolved_texture_ref_count", -1)) != 0
    ):
        raise ValueError("effect index is incomplete or belongs to another chunk set")
    effect_sources = _validate_effect_chunk_indexes(effect_index_root, chunks)

    interior, interior_bytes = _read_object_bytes(
        interior_source_publication, "interior source publication"
    )
    _validate_phase8_interior_source_binding(
        phase8_pack_root=source_pack_manifest.parent,
        source_pack_id=source_pack_id,
        interior=interior,
        interior_bytes=interior_bytes,
    )
    room_effect_bindings = map_room_effect_bindings(effect_global, interior)
    interior_light_counts = _interior_light_counts(interior)

    roots = discover_placed_root_prefabs(chunks, output_root)
    compound_graph, compound_source = load_compound_prefab_graph(sgo_raw, roots)
    lights, light_bytes = _read_object_bytes(light_index, "SGO light index")
    closure_light_templates = attach_light_actors(compound_graph, lights)
    exterior_lights, placement_sources = build_exterior_lights(
        chunks=chunks,
        output_root=output_root,
        compound_graph=compound_graph,
    )

    grass, grass_bytes = _read_object_bytes(grass_materials, "grass materials")
    grass_chunks, grass_resources = build_grass_authority(
        chunks=chunks,
        grass=grass,
        output_root=output_root,
    )

    effect_placements = effect_global.get("effect_placements")
    if not isinstance(effect_placements, list):
        raise ValueError("global effect topology table is invalid")
    dependency_cycles = sum(
        bool(value.get("dependency_cycle", False))
        for value in effect_placements
        if isinstance(value, dict)
    )
    if dependency_cycles:
        raise ValueError("production effect topology contains a dependency cycle")
    counts = {
        "chunk_count": len(chunks),
        "effect_cell_count": int(effect_global["cell_count"]),
        "effect_placement_count": int(effect_global["effect_placement_count"]),
        "compound_effect_placement_count": int(
            effect_global["compound_effect_placement_count"]
        ),
        "emitter_record_count": int(effect_global["emitter_count"]),
        "emitter_component_count": int(effect_global["emitter_component_count"]),
        "renderable_emitter_count": int(effect_global["renderable_emitter_count"]),
        "dependency_edge_count": int(effect_global["dependency_edge_count"]),
        "atomic_activation_group_count": int(
            effect_global["atomic_activation_group_count"]
        ),
        "dependency_cycle_count": dependency_cycles,
        "room_effect_placement_count": len(room_effect_bindings),
        "exterior_effect_placement_count": (
            int(effect_global["effect_placement_count"]) - len(room_effect_bindings)
        ),
        "exterior_light_count": len(exterior_lights),
        "exterior_light_template_count": len(
            {str(value["light_template_id"]) for value in exterior_lights}
        ),
        "placed_graph_closure_light_template_count": closure_light_templates,
        "interior_light_template_count": interior_light_counts[0],
        "interior_light_instance_count": interior_light_counts[1],
        "grass_material_metadata_chunk_count": sum(
            bool(value["material_metadata_present"]) for value in grass_chunks
        ),
        "exact_grass_density_chunk_count": sum(
            value["density_authority"] == "exact_authored_grass_alpha"
            for value in grass_chunks
        ),
        "disabled_grass_density_chunk_count": sum(
            value["density_authority"] == "absent_disabled"
            for value in grass_chunks
        ),
        "grass_resource_count": len(grass_resources),
    }
    if counts["effect_placement_count"] != len(effect_placements):
        raise ValueError("effect placement count differs from topology table")
    if (
        counts["room_effect_placement_count"]
        + counts["exterior_effect_placement_count"]
        != counts["effect_placement_count"]
    ):
        raise ValueError("effect exterior/interior ownership partition differs")

    source_contract = {
        "source_pack_id": source_pack_id,
        "source_pack_manifest_bytes": len(source_pack_bytes),
        "source_pack_manifest_sha256": hashlib.sha256(source_pack_bytes).hexdigest(),
        "chunks": chunks,
        "effect_global_index": _source_file_record(
            effect_global_path, effect_global_bytes, "global_particle_cells.json"
        ),
        "effect_chunk_indexes": effect_sources,
        "interior_source_publication": _source_file_record(
            interior_source_publication,
            interior_bytes,
            interior_source_publication.name,
        ),
        "sgo_compound_graph": compound_source,
        "sgo_light_index": _source_file_record(
            light_index, light_bytes, "data/sgo_by_class/sgo_lights.json"
        ),
        "object_placement_sources": placement_sources,
        "grass_material_source": _source_file_record(
            grass_materials, grass_bytes, grass_materials.name
        ),
    }
    identity = {
        "schema": SCHEMA,
        "version": VERSION,
        "policy": POLICY,
        "effect_policy": EFFECT_POLICY,
        "light_policy": LIGHT_POLICY,
        "grass_policy": GRASS_POLICY,
        "room_effect_policy": ROOM_EFFECT_POLICY,
        "source_contract": source_contract,
        "counts": counts,
        "room_effect_bindings": room_effect_bindings,
        "exterior_lights": exterior_lights,
        "grass_chunks": grass_chunks,
        "grass_resources": grass_resources,
        "runtime_layer_contract": {
            "default_layer": "base",
            "authored_layer_count": 1,
            "authored_layers": ["base"],
            "variant_key_policy": "empty_means_nonreplacement_base_content_v1",
            "production_content_invention": False,
        },
    }
    publication = dict(identity)
    publication["publication_id"] = sha256_id(
        "activity_source_publication", identity
    )
    publication["content_revision"] = hashlib.sha256(
        canonical_json_bytes(identity)
    ).hexdigest()
    return publication


def map_room_effect_bindings(
    effect_global: dict[str, Any], interior: dict[str, Any]
) -> list[dict[str, str]]:
    templates = interior.get("interior_templates")
    instances = interior.get("instances")
    placements = effect_global.get("effect_placements")
    if not isinstance(templates, list) or not isinstance(instances, list):
        raise ValueError("interior template/instance tables are invalid")
    if not isinstance(placements, list):
        raise ValueError("effect placement table is invalid")
    template_rooms: dict[str, dict[str, str]] = {}
    for template in templates:
        if not isinstance(template, dict):
            raise ValueError("interior template row is invalid")
        root = str(template.get("root_prefab", "")).casefold()
        rooms = template.get("rooms")
        if not root or root in template_rooms or not isinstance(rooms, list):
            raise ValueError("interior template root/room identity differs")
        paths: dict[str, str] = {}
        for room in rooms:
            path = str(room.get("source_component_path", ""))
            room_id = str(room.get("room_id", ""))
            if not path or not room_id or path in paths:
                raise ValueError("interior room source-path identity differs")
            paths[path] = room_id
        template_rooms[root] = paths
    instance_by_node: dict[tuple[str, int], dict[str, Any]] = {}
    for instance in instances:
        if not isinstance(instance, dict):
            raise ValueError("interior instance row is invalid")
        key = (str(instance.get("chunk", "")), int(instance.get("node_index", -1)))
        if key in instance_by_node:
            raise ValueError(f"interior instance node identity overlaps: {key}")
        instance_by_node[key] = instance

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for placement in placements:
        if not isinstance(placement, dict):
            raise ValueError("effect placement row is invalid")
        room_path = str(placement.get("room_source_component_path", ""))
        if not room_path:
            continue
        placement_id = str(placement.get("placement_id", ""))
        key = (
            str(placement.get("source_chunk", "")),
            int(placement.get("source_node_index", -1)),
        )
        instance = instance_by_node.get(key)
        if instance is None:
            raise ValueError(f"room effect has no interior instance: {placement_id}")
        room_id = template_rooms.get(
            str(instance.get("root_prefab", "")).casefold(), {}
        ).get(room_path)
        if not room_id:
            raise ValueError(f"room effect has no exact room path: {placement_id}")
        if not placement_id or placement_id in seen:
            raise ValueError(f"room effect identity overlaps: {placement_id}")
        seen.add(placement_id)
        result.append(
            {
                "placement_id": placement_id,
                "interior_instance_id": str(instance["interior_instance_id"]),
                "room_id": room_id,
                "room_source_component_path": room_path,
            }
        )
    result.sort(key=lambda value: value["placement_id"])
    return result


def attach_light_actors(
    compound_graph: dict[str, dict[str, Any]], lights: dict[str, Any]
) -> int:
    light_by_fold = {str(name).casefold(): value for name, value in lights.items()}
    template_ids: set[str] = set()
    for prefab_name, prefab in compound_graph.items():
        raw_actors = light_by_fold.get(prefab_name.casefold(), [])
        if not isinstance(raw_actors, list):
            raise ValueError(f"SGO light list is invalid: {prefab_name}")
        actors: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for raw in raw_actors:
            if not isinstance(raw, dict):
                raise ValueError(f"SGO light actor is invalid: {prefab_name}")
            class_name = str(raw.get("class", "")).strip()
            name = str(raw.get("name", "")).strip()
            props = raw.get("props", {})
            if not class_name or not name or not isinstance(props, dict):
                raise ValueError(f"SGO light actor identity/properties differ: {prefab_name}")
            path = source_component_path(prefab_name, class_name, name)
            if path in seen_paths:
                raise ValueError(f"duplicate SGO light actor path: {path}")
            seen_paths.add(path)
            transform = AffineTransform.from_properties(props)
            template_id = sha256_id(
                "light_template", {"class": class_name, "properties": props}
            )
            template_ids.add(template_id)
            actors.append(
                {
                    "class": class_name,
                    "name": name,
                    "properties": props,
                    "source_component_path": path,
                    "transform": transform.serialize(),
                    "_transform": transform,
                    "light_template_id": template_id,
                }
            )
        actors.sort(key=lambda value: value["source_component_path"])
        prefab["actors"] = actors
    return len(template_ids)


def build_exterior_lights(
    *,
    chunks: list[str],
    output_root: Path,
    compound_graph: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    terrain_root = output_root / "terrain" / "terrain_grid"
    graph_by_fold = {name.casefold(): name for name in compound_graph}
    relative_lights: dict[str, list[dict[str, Any]]] = {}
    result: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for chunk in chunks:
        path = terrain_root / f"{chunk}_objects.gltf"
        raw = path.read_bytes()
        data = json.loads(raw)
        nodes = data.get("nodes", []) if isinstance(data, dict) else []
        if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
            raise ValueError(f"object placement nodes are invalid: {path}")
        root_children = nodes[0].get("children", [])
        if not isinstance(root_children, list):
            raise ValueError(f"object placement root children are invalid: {path}")
        chunk_origin = chunk_global_origin(chunk)
        for node_index_value in root_children:
            node_index = int(node_index_value)
            if not 0 <= node_index < len(nodes) or not isinstance(nodes[node_index], dict):
                raise ValueError(f"object placement node index is invalid: {chunk}/{node_index}")
            node = nodes[node_index]
            extras = node.get("extras", {})
            if not isinstance(extras, dict):
                continue
            requested = str(extras.get("prefab_name", "")).strip()
            resolved = graph_by_fold.get(requested.casefold())
            if not resolved:
                continue
            if resolved not in relative_lights:
                relative_lights[resolved] = list(
                    walk_nonroom_actors(resolved, compound_graph)
                )
            node_transform = compact_node_transform(node)
            for actor in relative_lights[resolved]:
                source_transform = actor.get("_transform")
                if not isinstance(source_transform, AffineTransform):
                    raise ValueError("resolved exterior light lacks affine transform")
                local_position = source_affine_transform_gltf_position(
                    source_transform, [0.0, 0.0, 0.0]
                )
                chunk_position = transform_local_position(
                    node_transform, local_position
                )
                global_position = add_vector(chunk_position, chunk_origin)
                source_path = str(actor["source_component_path"])
                identity = {
                    "chunk": chunk,
                    "node_index": node_index,
                    "source_component_path": source_path,
                }
                light_id = sha256_id("exterior_light", identity)
                if light_id in seen_ids:
                    raise ValueError(f"exterior light identity overlaps: {light_id}")
                seen_ids.add(light_id)
                properties = actor.get("properties", {})
                brightness = _finite_number(properties.get("LightBrightness", 100.0))
                radius = _finite_number(properties.get("LightRadius", 100.0))
                result.append(
                    {
                        "light_id": light_id,
                        "light_template_id": str(actor["light_template_id"]),
                        "source_chunk": chunk,
                        "source_node_index": node_index,
                        "source_root_prefab": resolved,
                        "source_component_path": source_path,
                        "class": str(actor["class"]),
                        "name": str(actor["name"]),
                        "properties": properties,
                        "node_transform": node_transform,
                        "source_local_transform": actor["transform"],
                        "chunk_position": _compact_vector(chunk_position),
                        "global_position": _compact_vector(global_position),
                        "layer_membership": ["base"],
                        "variant_key": "",
                        "active_by_default": (
                            properties.get("bHidden") is not True
                            and properties.get("bInitiallyOn") is not False
                        ),
                        "importance": round(max(0.0, brightness) * max(0.0, radius), 6),
                    }
                )
        sources.append(
            {
                "chunk": chunk,
                "relative_path": f"terrain/terrain_grid/{chunk}_objects.gltf",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    result.sort(key=lambda value: value["light_id"])
    return result, sources


def build_grass_authority(
    *, chunks: list[str], grass: dict[str, Any], output_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    chunk_materials = grass.get("chunks")
    materials = grass.get("materials")
    default_material = str(grass.get("default_material", ""))
    if not isinstance(chunk_materials, dict) or not isinstance(materials, dict):
        raise ValueError("grass material inventory tables are invalid")
    default = materials.get(default_material)
    if not default_material or not isinstance(default, dict):
        raise ValueError("grass default material is absent")

    resources_by_source: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for chunk in chunks:
        material_present = chunk in chunk_materials
        raw_material = chunk_materials.get(chunk, default)
        if not isinstance(raw_material, dict):
            raise ValueError(f"grass material record is invalid: {chunk}")
        material_name = str(raw_material.get("grass_material", default_material))
        material = materials.get(material_name, raw_material)
        if not isinstance(material, dict):
            raise ValueError(f"grass material definition is absent: {material_name}")
        atlas_path = _resolve_output_asset_path(
            str(material.get("asset_path", raw_material.get("asset_path", ""))),
            output_root,
        )
        atlas = _resource_record(
            atlas_path,
            source_relative=_source_relative(atlas_path, output_root),
            role="grass_atlas",
        )
        resources_by_source.setdefault(atlas["source_relative_path"], atlas)
        alpha_path = (
            output_root / "terrain" / "terrain_grid" / f"{chunk}_grass_alpha.png"
        )
        density_resource_id = ""
        if alpha_path.is_file():
            alpha = _resource_record(
                alpha_path,
                source_relative=_source_relative(alpha_path, output_root),
                role="grass_density",
            )
            resources_by_source.setdefault(alpha["source_relative_path"], alpha)
            density_resource_id = alpha["resource_id"]
        records.append(
            {
                "chunk": chunk,
                "material_metadata_present": material_present,
                "material_authority": (
                    "exact_authored_grass_data"
                    if material_present
                    else "resolved_default_material_no_authored_grass_data"
                ),
                "source_material": material_name,
                "atlas_resource_id": atlas["resource_id"],
                "density_authority": (
                    "exact_authored_grass_alpha"
                    if density_resource_id
                    else "absent_disabled"
                ),
                "density_resource_id": density_resource_id,
                "runtime_enabled": bool(density_resource_id),
                "layer_membership": ["base", "quality:grass"],
                "quality_scale_policy": "runtime_quality_tier_density_and_distance_v1",
                "cross_chunk_density_fallback_allowed": False,
            }
        )
    records.sort(key=lambda value: value["chunk"])
    resources = sorted(
        resources_by_source.values(), key=lambda value: value["resource_id"]
    )
    if len(records) != len(chunks):
        raise ValueError("grass authority does not cover the production chunk set")
    return records, resources


def _interior_light_counts(interior: dict[str, Any]) -> tuple[int, int]:
    templates = interior.get("interior_templates", [])
    instances = interior.get("instances", [])
    light_count_by_root: dict[str, int] = {}
    template_count = 0
    for template in templates:
        root = str(template.get("root_prefab", "")).casefold()
        count = sum(
            len(room.get("lights", []))
            for room in template.get("rooms", [])
            if isinstance(room, dict)
        )
        light_count_by_root[root] = count
        template_count += count
    instance_count = sum(
        light_count_by_root.get(str(value.get("root_prefab", "")).casefold(), 0)
        for value in instances
        if isinstance(value, dict)
    )
    return template_count, instance_count


def _validate_phase8_interior_source_binding(
    *,
    phase8_pack_root: Path,
    source_pack_id: str,
    interior: dict[str, Any],
    interior_bytes: bytes,
) -> None:
    if interior.get("schema") != "vanguard_world_interior_source_publication":
        raise ValueError("interior source publication schema is incompatible")
    catalog = read_json(phase8_pack_root / "world_catalog.json")
    spaces = catalog.get("spaces", []) if isinstance(catalog, dict) else []
    if not isinstance(spaces, list) or len(spaces) != 1:
        raise ValueError("Phase 8 source pack catalog is ambiguous")
    manifest_relative = str(spaces[0].get("interior_manifest_path", ""))
    if not manifest_relative:
        raise ValueError("Phase 8 source pack has no interior manifest")
    manifest = read_json(phase8_pack_root / manifest_relative)
    source_binding = (
        manifest.get("source_binding", {}).get("source_interior_publication", {})
        if isinstance(manifest, dict)
        else {}
    )
    interior_source_pack = interior.get("source_contract", {}).get("source_pack", {})
    digest = hashlib.sha256(interior_bytes).hexdigest()
    if (
        manifest.get("pack_id") != source_pack_id
        or source_binding.get("publication_id") != interior.get("publication_id")
        or source_binding.get("sha256") != f"sha256:{digest}"
        or source_binding.get("content_revision")
        != interior.get("content_revision")
        or not isinstance(interior_source_pack, dict)
        or source_binding.get("source_pack_id") != interior_source_pack.get("pack_id")
    ):
        raise ValueError(
            "interior source publication differs from the Phase 8 manifest binding"
        )


def _validate_effect_chunk_indexes(
    effect_root: Path, chunks: list[str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    for chunk in chunks:
        path = effect_root / "chunks" / chunk / "particle_cells.json"
        value, raw = _read_object_bytes(path, f"{chunk} effect index")
        if (
            int(value.get("version", 0)) < 15
            or value.get("chunk") != chunk
            or int(value.get("unresolved_texture_ref_count", -1)) != 0
        ):
            raise ValueError(f"effect chunk index is incompatible: {chunk}")
        for field in (
            "emitter_count",
            "renderable_emitter_count",
            "effect_placement_count",
            "emitter_component_count",
            "dependency_edge_count",
            "atomic_activation_group_count",
        ):
            totals[field] += int(value.get(field, 0))
        result.append(
            {
                "chunk": chunk,
                "relative_path": f"chunks/{chunk}/particle_cells.json",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "effect_placement_count": int(value["effect_placement_count"]),
                "emitter_component_count": int(value["emitter_component_count"]),
            }
        )
    return result


def _canonical_chunks(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("source pack chunk list is absent")
    chunks = sorted(str(item).strip() for item in value)
    if any(not item for item in chunks) or len(chunks) != len(set(chunks)):
        raise ValueError("source pack chunk list contains empty/duplicate values")
    return chunks


def _read_object_bytes(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object: {path}")
    return value, raw


def _source_file_record(path: Path, raw: bytes, relative: str) -> dict[str, Any]:
    return {
        "relative_path": relative,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _resolve_output_asset_path(value: str, output_root: Path) -> Path:
    text = value.strip()
    if not text:
        raise ValueError("grass resource path is empty")
    path = Path(text)
    if path.is_absolute():
        result = path.resolve()
    elif path.parts and path.parts[0] == "output":
        result = (output_root.parent / path).resolve()
    else:
        result = (output_root / path).resolve()
    if not result.is_file():
        raise ValueError(f"grass source resource does not exist: {result}")
    return result


def _source_relative(path: Path, output_root: Path) -> str:
    try:
        return path.resolve().relative_to(output_root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(f"source resource is outside extractor output: {path}") from error


def _resource_record(path: Path, *, source_relative: str, role: str) -> dict[str, Any]:
    raw_size = path.stat().st_size
    revision = _sha256_file(path)
    identity = {"source_relative_path": source_relative, "sha256": revision}
    return {
        "resource_id": sha256_id("activity_resource", identity),
        "role": role,
        "source_relative_path": source_relative,
        "bytes": raw_size,
        "sha256": revision,
        "extension": path.suffix.lower(),
    }


def _sha256_file(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def _finite_number(value: Any) -> float:
    number = float(value)
    if not (-float("inf") < number < float("inf")):
        raise ValueError(f"non-finite light property: {value!r}")
    return number


def _compact_vector(value: Iterable[Any]) -> list[float]:
    result: list[float] = []
    for raw in value:
        number = round(_finite_number(raw), 6)
        result.append(0.0 if number == 0.0 else number)
    if len(result) != 3:
        raise ValueError("position vector does not contain three values")
    return result


def write_publication(path: Path, publication: dict[str, Any]) -> None:
    for root in VOLATILE_OUTPUT_ROOTS:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        raise ValueError(f"large residency output may not use volatile location {root}: {path}")
    if path.exists():
        raise ValueError(f"activity source publication is immutable: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_name(f".{path.name}.candidate-{os.getpid()}")
    if candidate.exists():
        raise ValueError(f"activity source candidate already exists: {candidate}")
    candidate.write_bytes(canonical_json_bytes(publication) + b"\n")
    os.replace(candidate, path)


if __name__ == "__main__":
    raise SystemExit(main())
