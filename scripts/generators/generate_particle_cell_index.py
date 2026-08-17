#!/usr/bin/env python3
"""Build per-chunk Godot runtime placement indexes for SGO particle emitters."""

from __future__ import annotations

import argparse
from collections import Counter
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.world_residency_interiors import (
    AffineTransform,
    MAX_PREFAB_DEPTH,
    ROOM_COMPOUND_TYPE,
    compact_compound_refs,
    nested_source_path,
    prefab_name_from_raw_record,
    source_component_path,
)

from generate_particle_manifest import (
    DEFAULT_OUTPUT_ROOT,
    PARTICLE_DEFAULT_MAX_PARTICLES,
    build_texture_index,
    classify_emitter,
    normalize_props,
    particle_template_id,
    read_json,
)


DEFAULT_RUNTIME_ROOT = DEFAULT_OUTPUT_ROOT / "godot_runtime"
DEFAULT_PARTICLE_MANIFEST = DEFAULT_OUTPUT_ROOT / "data/particle_emitters.json"
PARTICLE_CELL_INDEX_VERSION = 15
GLOBAL_PARTICLE_CELL_INDEX_VERSION = 14
DEFAULT_CELL_SIZE = 24000.0
TERRAIN_CHUNK_WORLD_SIZE = 204400.0

RUNTIME_NORMALIZED_FIELDS = [
    "texture_resolution",
    "draw_style",
    "alpha_ref",
    "alpha_test",
    "accepts_projectors",
    "z_test",
    "z_write",
    "render_two_sided",
    "uniform_size",
    "coordinate_system",
    "automatic_initial_spawning",
    "respawn_dead_particles",
    "initial_particles_per_second",
    "particles_per_second",
    "initial_delay",
    "relative_warmup_time",
    "warmup_ticks_per_second",
    "fade_in",
    "fade_in_end_time",
    "fade_out",
    "fade_out_start_time",
    "use_random_subdivision",
    "use_subdivision_scale",
    "subdivision_scale",
    "size_scale",
    "use_size_scale",
    "use_regular_size_scale",
    "velocity_scale",
    "velocity_scale_range",
    "use_velocity_scale",
    "acceleration",
    "max_abs_velocity",
    "velocity_loss",
    "line_segments",
    "time_between_segments",
    "time_before_visible",
    "add_location_from_other_emitter",
    "add_velocity_from_other_emitter",
    "start_velocity_radial",
    "start_spin",
    "spin_particles",
    "spins_per_second",
    "spin_ccw_or_cw",
    "use_revolution",
    "use_revolution_source_present",
    "revolutions_per_second",
    "revolution_center_offset",
    "use_revolution_scale",
    "revolution_scale",
    "revolution_scale_repeats",
    "start_location_range",
    "start_location_polar_range",
    "start_location_shape",
    "ribbon_on",
    "ribbon_width",
    "ribbon_length",
    "ribbon_texture_ref",
    "ribbon_texture_object_path",
    "ribbon_texture_relative_path",
    "sample_point_timing",
    "max_div_per_sample_point",
    "use_direction_as",
    "use_skeletal_location_as",
    "spawn_only_in_direction_of_normal",
    "projection_normal",
    "get_velocity_direction_from",
    "determine_end_point_by",
    "beam_endpoints",
    "beam_texture_u_scale",
    "beam_texture_v_scale",
    "rotating_sheets",
    "low_frequency_noise_range",
    "low_frequency_points",
    "high_frequency_noise_range",
    "high_frequency_points",
    "noise_determines_end_point",
    "use_low_frequency_scale",
    "low_frequency_scale_factors",
    "high_frequency_scale_factors",
    "high_frequency_scale_repeats",
    "use_branching",
    "branch_probability",
    "branch_emitter",
    "branch_high_frequency_points",
    "branch_spawn_amount",
    "linkup_lifetime",
    "ambient_sound_ref",
    "ambient_sound_object_ref",
    "ambient_sound_object_path",
    "ambient_sound_package",
    "ambient_sound_package_path",
    "ambient_sound_resolved",
    "ambient_sound_relative_path",
    "ambient_sound_output_path",
    "ambient_sound_bank",
    "ambient_sound_bank_dir",
    "ambient_sound_sample_title",
    "ambient_sound_duration_seconds",
    "ambient_sound_resolution",
    "sound_radius",
    "sound_volume",
    "spawning_sound_index",
    "spawning_sound_probability",
    "sounds",
    "source_defaulted_fields",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", action="append", default=[], help="Chunk to index.")
    parser.add_argument("--all", action="store_true", help="Index every chunk with SGO sidecars.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--particle-manifest", type=Path, default=DEFAULT_PARTICLE_MANIFEST)
    parser.add_argument(
        "--sgo-raw",
        type=Path,
        default=None,
        help="Canonical raw SGO JSONL used to resolve nested compound prefabs.",
    )
    parser.add_argument("--cell-size", type=float, default=DEFAULT_CELL_SIZE)
    parser.add_argument("--limit-chunks", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--require-emitter-count",
        type=int,
        default=0,
        help="Fail if the selected source set produces fewer emitters than this guard.",
    )
    parser.add_argument(
        "--write-global-index",
        action="store_true",
        help="Write a global proximity particle-cell index for the selected chunks.",
    )
    parser.add_argument(
        "--global-index-path",
        type=Path,
        default=None,
        help="Override global particle-cell index path. Defaults to <runtime-root>/global_particle_cells.json.",
    )
    args = parser.parse_args()

    output_root = args.output_root.expanduser().resolve()
    runtime_root = args.runtime_root.expanduser().resolve()
    particle_manifest_path = args.particle_manifest.expanduser().resolve()
    sgo_raw_path = (
        args.sgo_raw.expanduser().resolve()
        if args.sgo_raw is not None
        else output_root / "data/sgo_raw.jsonl"
    )
    chunks = [normalize_chunk_name(chunk) for chunk in args.chunk]
    if args.all:
        chunks.extend(discover_chunks(output_root))
    chunks = sorted(set(chunks))
    if args.limit_chunks > 0:
        chunks = chunks[: args.limit_chunks]
    if not chunks:
        parser.error("provide at least one --chunk or --all")

    template_map, manifest_emitters_by_prefab = load_particle_manifest_maps(
        particle_manifest_path
    )
    placed_root_prefabs = discover_placed_root_prefabs(chunks, output_root)
    compound_graph, compound_graph_source = load_compound_prefab_graph(
        sgo_raw_path, placed_root_prefabs
    )
    texture_index = build_texture_index(output_root / "textures", output_root)

    failures = 0
    selected_emitter_count = 0
    for chunk in chunks:
        try:
            result = build_particle_cell_index(
                chunk=chunk,
                output_root=output_root,
                runtime_root=runtime_root,
                particle_manifest_path=particle_manifest_path,
                template_map=template_map,
                manifest_emitters_by_prefab=manifest_emitters_by_prefab,
                compound_graph=compound_graph,
                compound_graph_source=compound_graph_source,
                texture_index=texture_index,
                cell_size=float(args.cell_size),
                dry_run=args.dry_run,
            )
            selected_emitter_count += int(result["emitter_count"])
            print_summary(result, args.dry_run)
        except Exception as exc:  # noqa: BLE001 - index all requested chunks when possible.
            failures += 1
            print(f"ERROR: {chunk}: {exc}")
    if failures == 0 and selected_emitter_count < max(0, args.require_emitter_count):
        failures += 1
        print(
            "ERROR: selected emitter count %d is below required minimum %d"
            % (selected_emitter_count, max(0, args.require_emitter_count))
        )
    if failures == 0 and args.write_global_index:
        template_map.clear()
        manifest_emitters_by_prefab.clear()
        compound_graph.clear()
        texture_index.clear()
        gc.collect()
        global_result = write_global_particle_cell_index(
            chunks=chunks,
            runtime_root=runtime_root,
            index_path=args.global_index_path,
            cell_size=float(args.cell_size),
            dry_run=args.dry_run,
        )
        print_global_index_summary(global_result, args.dry_run)
    return 1 if failures else 0


def discover_chunks(output_root: Path) -> list[str]:
    terrain_root = output_root / "terrain/terrain_grid"
    return sorted(path.name.removesuffix("_sgo.json") for path in terrain_root.glob("chunk_*_sgo.json"))


def discover_placed_root_prefabs(chunks: list[str], output_root: Path) -> set[str]:
    roots: set[str] = set()
    terrain_root = output_root / "terrain/terrain_grid"
    for chunk in chunks:
        objects_path = terrain_root / f"{chunk}_objects.gltf"
        if not objects_path.is_file():
            raise ValueError(f"missing object placement file: {objects_path}")
        objects_data = read_json(objects_path)
        nodes = objects_data.get("nodes", []) if isinstance(objects_data, dict) else []
        if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
            raise ValueError(f"{chunk} object placement nodes are invalid")
        root_children = nodes[0].get("children", [])
        if not isinstance(root_children, list):
            raise ValueError(f"{chunk} root children are invalid")
        for node_index_value in root_children:
            node_index = int(node_index_value)
            if node_index < 0 or node_index >= len(nodes):
                continue
            node = nodes[node_index]
            if not isinstance(node, dict):
                continue
            extras = node.get("extras", {})
            if not isinstance(extras, dict):
                continue
            prefab_name = str(extras.get("prefab_name", "")).strip()
            if prefab_name:
                roots.add(prefab_name)
    if not roots:
        raise ValueError("selected chunks contain no placed root prefabs")
    return roots


def load_template_map(path: Path) -> dict[str, dict[str, Any]]:
    return load_particle_manifest_maps(path)[0]


def load_particle_manifest_maps(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[Any]]]:
    if not path.exists():
        return {}, {}
    manifest = read_json(path)
    templates = manifest.get("templates", []) if isinstance(manifest, dict) else []
    if not isinstance(templates, list):
        return {}, {}
    template_map = {
        str(template.get("id", "")): template
        for template in templates
        if isinstance(template, dict) and template.get("id")
    }
    return template_map, manifest_emitters_by_prefab_from_templates(templates)


def manifest_emitters_by_prefab_from_templates(
    templates: list[Any],
) -> dict[str, list[Any]]:
    """Rebuild the canonical prefab actor table from normalized templates.

    Newer residency-oriented SGO sidecars intentionally contain only component
    and compound structure.  Particle templates retain the exact emitter actor
    index, class, name, and raw properties, so this join is the authoritative
    compatibility path rather than relying on the removed source_extras table.
    """

    sparse: dict[str, dict[int, dict[str, Any]]] = {}
    for template_value in templates:
        if not isinstance(template_value, dict):
            continue
        source = template_value.get("source", {})
        if not isinstance(source, dict) or source.get("kind") != "sgo_prefab_extra":
            continue
        prefab_name = str(source.get("prefab", "")).strip()
        actor_index_value = source.get("actor_index")
        if not prefab_name or isinstance(actor_index_value, bool) or not isinstance(
            actor_index_value, (int, float)
        ):
            continue
        actor_index = int(actor_index_value)
        if actor_index < 0 or float(actor_index_value) != float(actor_index):
            continue
        actor = {
            "class": str(source.get("class", "")).strip(),
            "name": str(source.get("name", "")).strip(),
            "props": template_value.get("props", {}),
        }
        existing = sparse.setdefault(prefab_name, {}).get(actor_index)
        if existing is not None and existing != actor:
            raise ValueError(
                f"particle manifest has conflicting {prefab_name} actor index {actor_index}"
            )
        sparse[prefab_name][actor_index] = actor
    result: dict[str, list[Any]] = {}
    for prefab_name, actors_by_index in sparse.items():
        actor_count = max(actors_by_index) + 1
        actors: list[Any] = [None] * actor_count
        for actor_index, actor in actors_by_index.items():
            actors[actor_index] = actor
        result[prefab_name] = actors
    return result


def load_compound_prefab_graph(
    sgo_raw: Path,
    roots: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not sgo_raw.is_file():
        raise ValueError(f"canonical raw SGO JSONL does not exist: {sgo_raw}")
    if not roots:
        raise ValueError("compound prefab graph root set is empty")
    graph: dict[str, dict[str, Any]] = {}
    retained_by_fold: dict[str, str] = {}
    digest = hashlib.sha256()
    record_count = 0
    pending = {name.casefold() for name in roots}
    pass_count = 0
    while pending:
        pass_count += 1
        found: set[str] = set()
        discovered: set[str] = set()
        with sgo_raw.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.strip():
                    raise ValueError(f"blank line in raw SGO JSONL at {line_number}")
                if pass_count == 1:
                    digest.update(raw_line)
                    record_count += 1
                record = json.loads(raw_line)
                name = prefab_name_from_raw_record(record)
                folded = name.casefold()
                if folded not in pending:
                    continue
                if folded in retained_by_fold:
                    raise ValueError(
                        f"duplicate case-insensitive SGO prefab name: {name}"
                    )
                refs = compact_compound_refs(record)
                graph[name] = {"compound_refs": refs}
                retained_by_fold[folded] = name
                found.add(folded)
                discovered.update(
                    str(ref["sub_prefab"]).casefold()
                    for ref in refs
                    if str(ref.get("sub_prefab", "")).strip()
                )
        missing = sorted(pending - found)
        if missing:
            raise ValueError(
                f"compound prefab graph is missing {len(missing)} requested prefabs: "
                f"{missing[:8]}"
            )
        pending = discovered - set(retained_by_fold)
    if not graph:
        raise ValueError(f"canonical raw SGO JSONL is empty: {sgo_raw}")
    return graph, {
        "bytes": sgo_raw.stat().st_size,
        "closure_prefab_count": len(graph),
        "closure_root_count": len(roots),
        "closure_scan_pass_count": pass_count,
        "record_count": record_count,
        "relative_path": "data/sgo_raw.jsonl",
        "sha256": digest.hexdigest(),
    }


def walk_prefab_emitter_instances(
    root_prefab: str,
    sidecar: dict[str, Any],
    manifest_emitters_by_prefab: dict[str, list[Any]],
    compound_graph: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve direct and nested emitter prefabs for one placed root.

    The compact residency sidecar preserves CompoundObject structure even
    though it no longer duplicates emitter actors.  Walking that graph is
    necessary both for exact transforms and for retaining authored room
    ownership instead of treating nested interior effects as exterior cells.
    """

    compound_graph = compound_graph if compound_graph is not None else sidecar
    sidecar_by_fold = {str(name).casefold(): str(name) for name in sidecar}
    graph_by_fold = {str(name).casefold(): str(name) for name in compound_graph}
    emitter_by_fold = {
        str(name).casefold(): str(name) for name in manifest_emitters_by_prefab
    }
    result: list[dict[str, Any]] = []

    def visit(
        requested: str,
        prefab_transform: AffineTransform,
        source_parent_path: str,
        room_source_component_path: str,
        ancestors: frozenset[str],
        depth: int,
    ) -> None:
        if depth > MAX_PREFAB_DEPTH:
            raise ValueError(
                f"particle prefab graph exceeds {MAX_PREFAB_DEPTH} levels at {requested}"
            )
        folded = requested.casefold()
        if folded in ancestors:
            raise ValueError(f"cycle in particle prefab graph at {requested}")
        sidecar_name = sidecar_by_fold.get(folded)
        graph_name = graph_by_fold.get(folded)
        emitter_name = emitter_by_fold.get(folded)
        resolved_name = graph_name or sidecar_name or emitter_name
        if resolved_name is None:
            raise ValueError(f"particle prefab graph references missing prefab {requested}")
        sidecar_entry = sidecar.get(sidecar_name, {}) if sidecar_name is not None else {}
        if not isinstance(sidecar_entry, dict):
            raise ValueError(f"particle prefab sidecar entry is invalid: {resolved_name}")
        graph_entry = (
            compound_graph.get(graph_name, {}) if graph_name is not None else sidecar_entry
        )
        if not isinstance(graph_entry, dict):
            raise ValueError(f"particle compound graph entry is invalid: {resolved_name}")
        source_extras = sidecar_entry.get("source_extras", {})
        emitters = (
            source_extras.get("emitters", [])
            if isinstance(source_extras, dict)
            else []
        )
        emitter_source = "sgo_sidecar_source_extras"
        if not isinstance(emitters, list) or not emitters:
            emitters = manifest_emitters_by_prefab.get(emitter_name or resolved_name, [])
            emitter_source = "particle_template_manifest_prefab_join"
        if isinstance(emitters, list) and any(
            isinstance(value, dict) for value in emitters
        ):
            result.append(
                {
                    "prefab_name": emitter_name or resolved_name,
                    "emitters": emitters,
                    "prefab_transform": prefab_transform,
                    "source_parent_path": source_parent_path,
                    "room_source_component_path": room_source_component_path,
                    "emitter_source": emitter_source,
                }
            )

        next_ancestors = ancestors | {folded}
        refs = graph_entry.get("compound_refs", [])
        if not isinstance(refs, list):
            raise ValueError(f"particle prefab compound refs are invalid: {resolved_name}")
        for ref_value in refs:
            if not isinstance(ref_value, dict):
                continue
            child_name = str(ref_value.get("sub_prefab", "")).strip()
            if not child_name:
                raise ValueError(f"particle compound below {resolved_name} has no prefab")
            props = ref_value.get("props", {})
            if not isinstance(props, dict):
                props = {}
            ref_transform = ref_value.get("_transform")
            if not isinstance(ref_transform, AffineTransform):
                ref_transform = AffineTransform.from_properties(props)
            child_transform = prefab_transform.compose(ref_transform)
            direct_path = str(ref_value.get("source_component_path", "")).strip()
            if not direct_path:
                direct_path = source_component_path(
                    resolved_name,
                    str(ref_value.get("class", "CompoundObject")),
                    str(ref_value.get("name", "")),
                )
            child_path = (
                nested_source_path(source_parent_path, direct_path)
                if source_parent_path
                else direct_path
            )
            child_room_path = room_source_component_path
            compound_type = int(
                ref_value.get(
                    "compound_type", props.get("m_CompoundObjectType", 0)
                )
                or 0
            )
            if compound_type == ROOM_COMPOUND_TYPE:
                child_room_path = child_path
            visit(
                child_name,
                child_transform,
                child_path,
                child_room_path,
                next_ancestors,
                depth + 1,
            )

    visit(
        root_prefab,
        AffineTransform.identity(),
        "",
        "",
        frozenset(),
        0,
    )
    return result


def build_particle_cell_index(
    *,
    chunk: str,
    output_root: Path,
    runtime_root: Path,
    particle_manifest_path: Path,
    template_map: dict[str, dict[str, Any]],
    manifest_emitters_by_prefab: dict[str, list[Any]],
    compound_graph: dict[str, dict[str, Any]],
    compound_graph_source: dict[str, Any],
    texture_index: dict[str, Any],
    cell_size: float,
    dry_run: bool,
) -> dict[str, Any]:
    terrain_root = output_root / "terrain/terrain_grid"
    objects_path = terrain_root / f"{chunk}_objects.gltf"
    sgo_path = terrain_root / f"{chunk}_sgo.json"
    if not objects_path.exists():
        raise FileNotFoundError(f"missing object placement file: {objects_path}")
    if not sgo_path.exists():
        raise FileNotFoundError(f"missing SGO sidecar: {sgo_path}")

    objects_data = read_json(objects_path)
    sgo_manifest = read_json(sgo_path)
    nodes = objects_data.get("nodes", []) if isinstance(objects_data, dict) else []
    if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
        raise ValueError(f"{chunk} object placement nodes are invalid")
    root_children = nodes[0].get("children", [])
    if not isinstance(root_children, list):
        raise ValueError(f"{chunk} root children are invalid")

    chunk_origin = chunk_global_origin(chunk)
    cells: dict[str, dict[str, Any]] = {}
    class_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    unresolved_textures: Counter[str] = Counter()
    total_emitters = 0
    renderable_emitters = 0
    sprite_emitters = 0
    skipped_nodes = 0
    prefabs_with_emitters: set[str] = set()
    effect_placements: list[dict[str, Any]] = []
    emitter_source_counts: Counter[str] = Counter()

    def append_particle_record(record: dict[str, Any]) -> None:
        nonlocal total_emitters, renderable_emitters, sprite_emitters
        kind = str(record.get("kind", ""))
        class_name = str(record.get("class", ""))
        texture_ref = str(record.get("texture_ref", "") or "")
        texture_relative_path = str(record.get("texture_relative_path", "") or "")
        renderable = bool(record.get("renderable", False))
        if renderable:
            renderable_emitters += 1
        if kind == "sprite":
            sprite_emitters += 1
        if texture_ref and not texture_relative_path:
            unresolved_textures[texture_ref] += 1
        class_counts[class_name] += 1
        kind_counts[kind] += 1
        total_emitters += 1
        chunk_position = vector_from_list(record.get("chunk_position", []), [0.0, 0.0, 0.0])
        cell_key = cell_key_for_position(chunk_position, cell_size)
        add_record_to_cell(cells, cell_key, record)

    for node_index_value in root_children:
        node_index = int(node_index_value)
        if node_index < 0 or node_index >= len(nodes) or not isinstance(nodes[node_index], dict):
            skipped_nodes += 1
            continue
        node = nodes[node_index]
        extras = node.get("extras", {})
        if not isinstance(extras, dict):
            continue
        prefab_name = str(extras.get("prefab_name", "")).strip()
        if not prefab_name:
            continue
        node_transform = compact_node_transform(node)
        for emitter_instance in walk_prefab_emitter_instances(
            prefab_name,
            sgo_manifest,
            manifest_emitters_by_prefab,
            compound_graph,
        ):
            instance_prefab = str(emitter_instance["prefab_name"])
            emitters = emitter_instance["emitters"]
            prefab_transform = emitter_instance["prefab_transform"]
            source_parent_path = str(emitter_instance["source_parent_path"])
            room_source_component_path = str(
                emitter_instance["room_source_component_path"]
            )
            emitter_source = str(emitter_instance["emitter_source"])
            prefabs_with_emitters.add(instance_prefab)
            emitter_source_counts[emitter_source] += 1
            referenced_emitter_indices = referenced_concrete_emitter_indices(emitters)
            for emitter_index, actor_value in enumerate(emitters):
                if not isinstance(actor_value, dict):
                    continue
                props = actor_value.get("props", {})
                if not isinstance(props, dict):
                    props = {}
                class_name = str(actor_value.get("class", "")).strip()
                actor_name = str(actor_value.get("name", "")).strip()
                kind = classify_emitter(class_name)
                if emitter_index in referenced_emitter_indices and is_concrete_emitter_kind(kind):
                    continue
                direct_effect_path = source_component_path(
                    instance_prefab, class_name, actor_name
                )
                source_effect_path = (
                    nested_source_path(source_parent_path, direct_effect_path)
                    if source_parent_path
                    else direct_effect_path
                )
                if kind != "fx_actor":
                    record = particle_record_for_actor(
                        prefab_name=instance_prefab,
                        actor_index=emitter_index,
                        actor=actor_value,
                        node_index=node_index,
                        node=node,
                        node_transform=node_transform,
                        chunk_origin=chunk_origin,
                        template_map=template_map,
                        texture_index=texture_index,
                        prefab_transform=prefab_transform,
                        source_effect_path=source_effect_path,
                        room_source_component_path=room_source_component_path,
                    )
                    placement = build_effect_placement_topology(
                        chunk=chunk,
                        node_index=node_index,
                        prefab_name=instance_prefab,
                        root_actor_index=emitter_index,
                        source_effect_path=source_effect_path,
                        wrapper_record=None,
                        component_records=[record],
                        source_actor_indices=[emitter_index],
                    )
                    effect_placements.append(placement)
                    append_particle_record(record)
                    continue
                wrapper_record = particle_record_for_actor(
                    prefab_name=instance_prefab,
                    actor_index=emitter_index,
                    actor=actor_value,
                    node_index=node_index,
                    node=node,
                    node_transform=node_transform,
                    chunk_origin=chunk_origin,
                    template_map=template_map,
                    texture_index=texture_index,
                    prefab_transform=prefab_transform,
                    source_effect_path=source_effect_path,
                    room_source_component_path=room_source_component_path,
                )
                parent_normalized = normalized_actor_props(
                    instance_prefab,
                    emitter_index,
                    actor_value,
                    template_map,
                    texture_index,
                )
                parent_local_position, parent_local_source = emitter_local_position(
                    parent_normalized
                )
                child_records: list[dict[str, Any]] = []
                child_actor_indices: list[int] = []
                for child_index in fx_emitter_reference_indices(props, len(emitters)):
                    child_actor = emitters[child_index] if 0 <= child_index < len(emitters) else None
                    if not isinstance(child_actor, dict):
                        continue
                    child_kind = classify_emitter(str(child_actor.get("class", "")).strip())
                    if not is_concrete_emitter_kind(child_kind):
                        continue
                    child_record = particle_record_for_actor(
                        prefab_name=instance_prefab,
                        actor_index=child_index,
                        actor=child_actor,
                        node_index=node_index,
                        node=node,
                        node_transform=node_transform,
                        chunk_origin=chunk_origin,
                        template_map=template_map,
                        texture_index=texture_index,
                        parent_actor=actor_value,
                        parent_actor_index=emitter_index,
                        parent_normalized=parent_normalized,
                        parent_local_position=parent_local_position,
                        parent_local_position_source=parent_local_source,
                        prefab_transform=prefab_transform,
                        source_effect_path=source_effect_path,
                        room_source_component_path=room_source_component_path,
                    )
                    child_records.append(child_record)
                    child_actor_indices.append(child_index)
                placement = build_effect_placement_topology(
                    prefab_name=instance_prefab,
                    chunk=chunk,
                    node_index=node_index,
                    root_actor_index=emitter_index,
                    source_effect_path=source_effect_path,
                    wrapper_record=wrapper_record,
                    component_records=child_records,
                    source_actor_indices=child_actor_indices,
                )
                effect_placements.append(placement)
                append_particle_record(wrapper_record)
                for child_record in child_records:
                    append_particle_record(child_record)

    for cell in cells.values():
        finalize_cell(cell)

    data = {
        "version": PARTICLE_CELL_INDEX_VERSION,
        "generated_by": "scripts/generators/generate_particle_cell_index.py",
        "generated_at_unix": deterministic_generation_epoch(),
        "chunk": chunk,
        "cell_size": float(cell_size),
        "cell_key_space": "chunk_local",
        "global_cell_key_space": "world",
        "chunk_global_origin": compact_vector(chunk_origin),
        "objects_source_relative_path": relative_path(objects_path, output_root),
        "sgo_source_relative_path": relative_path(sgo_path, output_root),
        "particle_manifest_relative_path": relative_path(particle_manifest_path, output_root),
        "compound_graph_source": compound_graph_source,
        "emitter_source_policy": (
            "compound_graph_walk_with_sidecar_source_extras_or_canonical_"
            "template_prefab_join_v2"
        ),
        "emitter_source_counts": dict(sorted(emitter_source_counts.items())),
        "total_node_count": len(root_children),
        "skipped_node_count": skipped_nodes,
        "prefab_with_emitters_count": len(prefabs_with_emitters),
        "emitter_count": total_emitters,
        "renderable_emitter_count": renderable_emitters,
        "sprite_emitter_count": sprite_emitters,
        "class_counts": dict(sorted(class_counts.items())),
        "kind_counts": dict(sorted(kind_counts.items())),
        "unresolved_texture_ref_count": sum(unresolved_textures.values()),
        "top_unresolved_texture_refs": dict(unresolved_textures.most_common(50)),
        "effect_placement_count": len(effect_placements),
        "compound_effect_placement_count": sum(
            1 for placement in effect_placements if placement.get("wrapper_template_id")
        ),
        "emitter_component_count": sum(
            len(placement.get("ordered_emitter_component_ids", []))
            for placement in effect_placements
        ),
        "dependency_edge_count": sum(
            len(placement.get("dependency_edges", [])) for placement in effect_placements
        ),
        "atomic_activation_group_count": sum(
            len(placement.get("atomic_activation_groups", []))
            for placement in effect_placements
        ),
        "effect_placements": effect_placements,
        "cell_count": len(cells),
        "cells": dict(sorted(cells.items())),
    }

    index_path = runtime_root / "chunks" / chunk / "particle_cells.json"
    if not dry_run:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return {
        "chunk": chunk,
        "index_path": index_path,
        "emitter_count": total_emitters,
        "renderable_emitter_count": renderable_emitters,
        "sprite_emitter_count": sprite_emitters,
        "cell_count": len(cells),
        "prefab_with_emitters_count": len(prefabs_with_emitters),
        "unresolved_texture_ref_count": sum(unresolved_textures.values()),
    }


def print_summary(result: dict[str, Any], dry_run: bool) -> None:
    label = "DRY RUN" if dry_run else "WROTE"
    print(
        "%s: %s particle_cells=%d emitters=%d renderable=%d sprites=%d prefabs=%d unresolved_textures=%d index=%s"
        % (
            label,
            result["chunk"],
            int(result["cell_count"]),
            int(result["emitter_count"]),
            int(result["renderable_emitter_count"]),
            int(result["sprite_emitter_count"]),
            int(result["prefab_with_emitters_count"]),
            int(result["unresolved_texture_ref_count"]),
            result["index_path"],
        )
    )


def write_global_particle_cell_index(
    *,
    chunks: list[str],
    runtime_root: Path,
    index_path: Path | None,
    cell_size: float,
    dry_run: bool,
) -> dict[str, Any]:
    if index_path is None:
        index_path = runtime_root / "global_particle_cells.json"
    else:
        index_path = index_path.expanduser().resolve()

    cells: dict[str, dict[str, Any]] = {}
    missing_chunks: list[str] = []
    class_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    total_emitters = 0
    total_renderable = 0
    total_unresolved_textures = 0
    effect_placements: list[dict[str, Any]] = []
    total_emitter_components = 0
    total_dependency_edges = 0
    total_atomic_activation_groups = 0

    for chunk in chunks:
        chunk_path = runtime_root / "chunks" / chunk / "particle_cells.json"
        if not chunk_path.exists():
            missing_chunks.append(chunk)
            continue
        chunk_index = read_json(chunk_path)
        if not isinstance(chunk_index, dict):
            missing_chunks.append(chunk)
            continue
        chunk_effect_placements = chunk_index.get("effect_placements", [])
        if isinstance(chunk_effect_placements, list):
            for placement_value in chunk_effect_placements:
                if not isinstance(placement_value, dict):
                    continue
                placement = placement_value.copy()
                placement["source_chunk"] = chunk
                effect_placements.append(placement)
                total_emitter_components += len(
                    placement.get("ordered_emitter_component_ids", [])
                )
                total_dependency_edges += len(placement.get("dependency_edges", []))
                total_atomic_activation_groups += len(
                    placement.get("atomic_activation_groups", [])
                )
        chunk_origin = vector_or_none(chunk_index.get("chunk_global_origin", []))
        if chunk_origin is None:
            chunk_origin = chunk_global_origin(chunk)
        chunk_cells = chunk_index.get("cells", {})
        if not isinstance(chunk_cells, dict):
            missing_chunks.append(chunk)
            continue
        for local_cell_key, cell_value in chunk_cells.items():
            if not isinstance(cell_value, dict):
                continue
            placements = cell_value.get("placements", [])
            if not isinstance(placements, list):
                continue
            for record_index, record_value in enumerate(placements):
                if not isinstance(record_value, dict):
                    continue
                record = record_value.copy()
                global_position = vector_or_none(record.get("global_position", []))
                if global_position is None:
                    local_position = vector_or_none(record.get("chunk_position", []))
                    if local_position is None:
                        continue
                    global_position = add_vector(local_position, chunk_origin)
                    record["global_position"] = compact_vector(global_position)
                global_cell_key = cell_key_for_position(global_position, cell_size)
                record["source_chunk"] = chunk
                record["source_cell_key"] = str(local_cell_key)
                record["source_record_index"] = int(record_index)
                record["chunk_global_origin"] = compact_vector(chunk_origin)

                kind = str(record.get("kind", ""))
                class_name = str(record.get("class", ""))
                renderable = bool(record.get("renderable", False))
                unresolved_texture = (
                    str(record.get("texture_ref", "") or "").strip() != ""
                    and str(record.get("texture_relative_path", "") or "").strip() == ""
                )

                total_emitters += 1
                if renderable:
                    total_renderable += 1
                if unresolved_texture:
                    total_unresolved_textures += 1
                bump_count(kind_counts, kind)
                bump_count(class_counts, class_name)

                global_cell = cells.setdefault(
                    global_cell_key,
                    {
                        "global_cell_key": global_cell_key,
                        "global_center_sum": [0.0, 0.0, 0.0],
                        "global_center_weight": 0,
                        "global_bounds_min": None,
                        "global_bounds_max": None,
                        "emitter_count": 0,
                        "renderable_emitter_count": 0,
                        "unresolved_texture_ref_count": 0,
                        "class_counts": {},
                        "kind_counts": {},
                        "_chunks": {},
                    },
                )
                chunk_key = f"{chunk}|{local_cell_key}"
                chunk_entry = global_cell["_chunks"].setdefault(
                    chunk_key,
                    {
                        "chunk": chunk,
                        "cell_key": str(local_cell_key),
                        "emitter_count": 0,
                        "renderable_emitter_count": 0,
                        "unresolved_texture_ref_count": 0,
                        "class_counts": {},
                        "kind_counts": {},
                        "placements": [],
                    },
                )
                chunk_entry["placements"].append(record)
                chunk_entry["emitter_count"] = int(chunk_entry.get("emitter_count", 0)) + 1
                if renderable:
                    chunk_entry["renderable_emitter_count"] = (
                        int(chunk_entry.get("renderable_emitter_count", 0)) + 1
                    )
                if unresolved_texture:
                    chunk_entry["unresolved_texture_ref_count"] = (
                        int(chunk_entry.get("unresolved_texture_ref_count", 0)) + 1
                    )
                bump_count(chunk_entry["kind_counts"], kind)
                bump_count(chunk_entry["class_counts"], class_name)

                global_cell["emitter_count"] = int(global_cell.get("emitter_count", 0)) + 1
                if renderable:
                    global_cell["renderable_emitter_count"] = (
                        int(global_cell.get("renderable_emitter_count", 0)) + 1
                    )
                if unresolved_texture:
                    global_cell["unresolved_texture_ref_count"] = (
                        int(global_cell.get("unresolved_texture_ref_count", 0)) + 1
                    )
                bump_count(global_cell["kind_counts"], kind)
                bump_count(global_cell["class_counts"], class_name)
                merge_position_bounds(global_cell, global_position)
                add_weighted_center(global_cell, global_position, 1)

    for global_cell in cells.values():
        weight = max(int(global_cell.pop("global_center_weight", 0)), 1)
        center_sum = global_cell.pop("global_center_sum", [0.0, 0.0, 0.0])
        global_cell["global_center"] = [
            round(float(center_sum[0]) / weight, 6),
            round(float(center_sum[1]) / weight, 6),
            round(float(center_sum[2]) / weight, 6),
        ]
        if global_cell.get("global_bounds_min") is None:
            global_cell["global_bounds_min"] = global_cell["global_center"]
        if global_cell.get("global_bounds_max") is None:
            global_cell["global_bounds_max"] = global_cell["global_center"]
        chunk_entries = list(global_cell.pop("_chunks", {}).values())
        for chunk_entry in chunk_entries:
            chunk_entry["kind_counts"] = dict(sorted(chunk_entry["kind_counts"].items()))
            chunk_entry["class_counts"] = dict(sorted(chunk_entry["class_counts"].items()))
        chunk_entries.sort(key=lambda entry: (str(entry.get("chunk", "")), str(entry.get("cell_key", ""))))
        global_cell["chunk_count"] = len(chunk_entries)
        global_cell["chunks"] = chunk_entries
        global_cell["kind_counts"] = dict(sorted(global_cell["kind_counts"].items()))
        global_cell["class_counts"] = dict(sorted(global_cell["class_counts"].items()))

    data = {
        "version": GLOBAL_PARTICLE_CELL_INDEX_VERSION,
        "generated_by": "scripts/generators/generate_particle_cell_index.py",
        "generated_at_unix": deterministic_generation_epoch(),
        "cell_size": float(cell_size),
        "cell_key_space": "world",
        "source_index_version": PARTICLE_CELL_INDEX_VERSION,
        "chunk_count": len(chunks),
        "missing_chunk_count": len(missing_chunks),
        "missing_chunks": missing_chunks,
        "emitter_count": total_emitters,
        "renderable_emitter_count": total_renderable,
        "unresolved_texture_ref_count": total_unresolved_textures,
        "effect_placement_count": len(effect_placements),
        "compound_effect_placement_count": sum(
            1 for placement in effect_placements if placement.get("wrapper_template_id")
        ),
        "emitter_component_count": total_emitter_components,
        "dependency_edge_count": total_dependency_edges,
        "atomic_activation_group_count": total_atomic_activation_groups,
        "effect_placements": effect_placements,
        "class_counts": dict(sorted(class_counts.items())),
        "kind_counts": dict(sorted(kind_counts.items())),
        "cell_count": len(cells),
        "chunks": chunks,
        "cells": dict(sorted(cells.items())),
    }
    if not dry_run:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {
        "index_path": index_path,
        "chunk_count": len(chunks),
        "missing_chunk_count": len(missing_chunks),
        "cell_count": len(cells),
        "emitter_count": total_emitters,
        "renderable_emitter_count": total_renderable,
        "unresolved_texture_ref_count": total_unresolved_textures,
    }


def print_global_index_summary(result: dict[str, Any], dry_run: bool) -> None:
    label = "DRY RUN" if dry_run else "WROTE"
    print(
        "%s: global_particle_cells=%d chunks=%d missing=%d emitters=%d renderable=%d unresolved_textures=%d index=%s"
        % (
            label,
            int(result["cell_count"]),
            int(result["chunk_count"]),
            int(result["missing_chunk_count"]),
            int(result["emitter_count"]),
            int(result["renderable_emitter_count"]),
            int(result["unresolved_texture_ref_count"]),
            result["index_path"],
        )
    )


def particle_record_for_actor(
    *,
    prefab_name: str,
    actor_index: int,
    actor: dict[str, Any],
    node_index: int,
    node: dict[str, Any],
    node_transform: dict[str, Any],
    chunk_origin: tuple[float, float, float],
    template_map: dict[str, dict[str, Any]],
    texture_index: dict[str, Any],
    parent_actor: dict[str, Any] | None = None,
    parent_actor_index: int | None = None,
    parent_normalized: dict[str, Any] | None = None,
    parent_local_position: tuple[float, float, float] | None = None,
    parent_local_position_source: str = "",
    prefab_transform: AffineTransform | None = None,
    source_effect_path: str = "",
    room_source_component_path: str = "",
) -> dict[str, Any]:
    props = actor.get("props", {})
    if not isinstance(props, dict):
        props = {}
    class_name = str(actor.get("class", "")).strip()
    actor_name = str(actor.get("name", "")).strip()
    kind = classify_emitter(class_name)
    template_id = particle_template_id(prefab_name, actor_index, class_name, actor_name)
    normalized = normalized_actor_props(prefab_name, actor_index, actor, template_map, texture_index)
    local_position, local_position_source = emitter_local_position(normalized)
    if parent_actor is not None and parent_normalized is not None:
        if parent_local_position is None:
            parent_local_position, parent_local_position_source = emitter_local_position(parent_normalized)
        local_position = add_vector(parent_local_position, local_position)
        local_position_source = "%s+%s" % (parent_local_position_source, local_position_source)
        normalized = normalized_with_parent_fx_scale(normalized, parent_normalized)
    if prefab_transform is not None:
        local_position = source_affine_transform_gltf_position(
            prefab_transform, local_position
        )
        if prefab_transform != AffineTransform.identity():
            local_position_source = "compound_path+%s" % local_position_source
    chunk_position = transform_local_position(node_transform, local_position)
    global_position = add_vector(chunk_position, chunk_origin)
    texture_ref = str(normalized.get("texture_ref", "") or "")
    texture_object_path = str(normalized.get("texture_object_path", "") or "")
    texture_relative_path = str(normalized.get("texture_relative_path", "") or "")
    static_mesh = str(normalized.get("static_mesh", "") or "")
    static_mesh_object_path = str(normalized.get("static_mesh_object_path", "") or "")
    renderable = (
        kind in {"sprite", "beam"} and bool(texture_relative_path)
    ) or (
        kind == "mesh" and bool(static_mesh or static_mesh_object_path)
    ) or kind == "light"
    record = {
        "template_id": template_id,
        "kind": kind,
        "class": class_name,
        "name": actor_name,
        "prefab_name": prefab_name,
        "node_index": node_index,
        "object_name": str(node.get("name", "")),
        "chunk_position": compact_vector(chunk_position),
        "global_position": compact_vector(global_position),
        "local_position": compact_vector(local_position),
        "local_position_source": local_position_source,
        "node_transform": node_transform,
        "renderable": renderable,
        "texture_ref": texture_ref,
        "texture_object_path": texture_object_path,
        "texture_relative_path": texture_relative_path,
        "max_particles": int(
            normalized.get("max_particles", PARTICLE_DEFAULT_MAX_PARTICLES)
            or PARTICLE_DEFAULT_MAX_PARTICLES
        ),
        "lifetime": normalized.get("lifetime"),
        "start_size": normalized.get("start_size"),
        "start_velocity": normalized.get("start_velocity"),
        "sphere_radius": normalized.get("sphere_radius"),
        "opacity": normalized.get("opacity"),
        "color_scale": normalized.get("color_scale"),
        "color_multiplier": normalized.get("color_multiplier"),
        "draw_scale": normalized.get("draw_scale"),
        "draw_scale_3d": normalized.get("draw_scale_3d"),
        "texture_u_subdivisions": normalized.get("texture_u_subdivisions"),
        "texture_v_subdivisions": normalized.get("texture_v_subdivisions"),
        "subdivision_start": normalized.get("subdivision_start"),
        "subdivision_end": normalized.get("subdivision_end"),
        "blend_between_subdivisions": normalized.get("blend_between_subdivisions"),
        "beam_distance": normalized.get("beam_distance"),
        "beam_texture_u_scale": normalized.get("beam_texture_u_scale"),
        "beam_texture_v_scale": normalized.get("beam_texture_v_scale"),
        "light_radius": normalized.get("light_radius"),
        "light_brightness": normalized.get("light_brightness"),
        "light_color": normalized.get("light_color"),
        "light_emitter_type": normalized.get("light_emitter_type"),
        "light_emitter_effect": normalized.get("light_emitter_effect"),
        "static_mesh": static_mesh,
        "static_mesh_object_path": static_mesh_object_path,
        "static_mesh_package": str(normalized.get("static_mesh_package", "") or ""),
        "static_mesh_package_path": str(normalized.get("static_mesh_package_path", "") or ""),
        "static_mesh_object_ref": normalized.get("static_mesh_object_ref", {}),
        "mesh_scale": normalized.get("mesh_scale"),
        "uniform_mesh_scale": normalized.get("uniform_mesh_scale"),
        "raw_property_count": len(props),
        "source_emitter_index": int(actor_index),
        "source_effect_path": source_effect_path,
        "room_source_component_path": room_source_component_path,
    }
    for field in RUNTIME_NORMALIZED_FIELDS:
        record[field] = normalized.get(field)
    if parent_actor is not None:
        parent_class = str(parent_actor.get("class", "")).strip()
        parent_name = str(parent_actor.get("name", "")).strip()
        record["parent_fx_class"] = parent_class
        record["parent_fx_name"] = parent_name
        record["parent_fx_emitter_index"] = int(parent_actor_index or 0)
        record["parent_fx_template_id"] = particle_template_id(
            prefab_name, int(parent_actor_index or 0), parent_class, parent_name
        )
    return record


def build_effect_placement_topology(
    *,
    chunk: str,
    node_index: int,
    prefab_name: str,
    root_actor_index: int,
    source_effect_path: str,
    wrapper_record: dict[str, Any] | None,
    component_records: list[dict[str, Any]],
    source_actor_indices: list[int],
) -> dict[str, Any]:
    """Annotate one source effect without flattening its wrapper topology.

    Vanguard inter-emitter indices address ordered wrapper child slots, not
    prefab actor indices.  Stable component IDs therefore include both the
    source child slot and actor index.  Dependency-connected components become
    explicit atomic activation groups; the complete placement remains a single
    residency transaction even when it contains multiple disconnected groups.
    """

    if len(component_records) != len(source_actor_indices):
        raise ValueError("effect component records and source actor indices differ")
    wrapper_template_id = (
        str(wrapper_record.get("template_id", "")) if wrapper_record is not None else ""
    )
    placement_id = stable_runtime_id(
        "effect_placement",
        chunk,
        int(node_index),
        prefab_name,
        int(root_actor_index),
        source_effect_path,
        wrapper_template_id,
    )
    component_ids: list[str] = []
    for child_slot, (record, source_actor_index) in enumerate(
        zip(component_records, source_actor_indices, strict=True)
    ):
        component_id = stable_runtime_id(
            "emitter_component",
            placement_id,
            int(child_slot),
            int(source_actor_index),
            str(record.get("template_id", "")),
        )
        component_ids.append(component_id)
        record["effect_placement_id"] = placement_id
        record["emitter_component_id"] = component_id
        record["source_child_slot"] = int(child_slot)
        record["wrapper_template_id"] = wrapper_template_id
        record["layer_membership"] = ["base"]
        record["variant_key"] = ""

    dependency_edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    dependency_fields = (
        ("add_location_from_other_emitter", "add_location"),
        ("add_velocity_from_other_emitter", "add_velocity"),
        ("branch_emitter", "branch_source"),
    )
    for target_slot, record in enumerate(component_records):
        for source_field, relationship in dependency_fields:
            source_slot = emitter_dependency_slot(record.get(source_field))
            if source_slot < 0 or source_slot >= len(component_ids):
                continue
            edge_key = (
                component_ids[source_slot],
                component_ids[target_slot],
                relationship,
            )
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            dependency_edges.append(
                {
                    "source_component_id": component_ids[source_slot],
                    "target_component_id": component_ids[target_slot],
                    "relationship": relationship,
                    "source_child_slot": int(source_slot),
                    "target_child_slot": int(target_slot),
                    "source_field": source_field,
                }
            )

    atomic_groups = atomic_activation_groups(placement_id, component_ids, dependency_edges)
    group_id_by_component: dict[str, str] = {}
    for group in atomic_groups:
        group_id = str(group["activation_group_id"])
        for component_id in group["ordered_emitter_component_ids"]:
            group_id_by_component[str(component_id)] = group_id
    for record, component_id in zip(component_records, component_ids, strict=True):
        record["atomic_activation_group_id"] = group_id_by_component[component_id]

    activation_order, dependency_cycle = dependency_order(component_ids, dependency_edges)
    if wrapper_record is not None:
        wrapper_record["effect_placement_id"] = placement_id
        wrapper_record["emitter_component_id"] = ""
        wrapper_record["source_child_slot"] = -1
        wrapper_record["wrapper_template_id"] = wrapper_template_id
        wrapper_record["compound_wrapper_metadata_only"] = True
        wrapper_record["layer_membership"] = ["base"]
        wrapper_record["variant_key"] = ""

    position_record = wrapper_record
    if position_record is None and component_records:
        position_record = component_records[0]
    position_record = position_record or {}
    return {
        "placement_id": placement_id,
        "source_chunk": chunk,
        "source_node_index": int(node_index),
        "source_root_emitter_index": int(root_actor_index),
        "source_effect_path": source_effect_path,
        "room_source_component_path": str(
            position_record.get("room_source_component_path", "")
        ),
        "prefab_name": prefab_name,
        "object_name": str(position_record.get("object_name", "")),
        "wrapper_template_id": wrapper_template_id,
        "ordered_emitter_component_ids": component_ids,
        "activation_ordered_emitter_component_ids": activation_order,
        "dependency_edges": dependency_edges,
        "dependency_cycle": dependency_cycle,
        "atomic_activation_group_ids": [
            str(group["activation_group_id"]) for group in atomic_groups
        ],
        "atomic_activation_groups": atomic_groups,
        "chunk_position": position_record.get("chunk_position", [0.0, 0.0, 0.0]),
        "global_position": position_record.get("global_position", [0.0, 0.0, 0.0]),
        "layer_membership": ["base"],
        "variant_key": "",
        "transaction_policy": "whole_effect_prepare_then_atomic_group_activation_v1",
    }


def stable_runtime_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(
        list(parts), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


def deterministic_generation_epoch() -> int:
    value = os.environ.get("SOURCE_DATE_EPOCH", "0").strip()
    if not value:
        return 0
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f"SOURCE_DATE_EPOCH is not an integer: {value!r}") from error
    if result < 0:
        raise ValueError("SOURCE_DATE_EPOCH cannot be negative")
    return result


def emitter_dependency_slot(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return -1
    numeric = int(value)
    return numeric if float(value) == float(numeric) else -1


def atomic_activation_groups(
    placement_id: str,
    component_ids: list[str],
    dependency_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not component_ids:
        return []
    parent = list(range(len(component_ids)))
    index_by_id = {component_id: index for index, component_id in enumerate(component_ids)}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for edge in dependency_edges:
        source = index_by_id.get(str(edge.get("source_component_id", "")))
        target = index_by_id.get(str(edge.get("target_component_id", "")))
        if source is not None and target is not None:
            union(source, target)

    components_by_root: dict[int, list[str]] = {}
    for index, component_id in enumerate(component_ids):
        components_by_root.setdefault(find(index), []).append(component_id)
    ordered_components = sorted(
        components_by_root.values(), key=lambda values: index_by_id[values[0]]
    )
    return [
        {
            "activation_group_id": stable_runtime_id(
                "effect_activation_group", placement_id, values
            ),
            "ordered_emitter_component_ids": values,
        }
        for values in ordered_components
    ]


def dependency_order(
    component_ids: list[str], dependency_edges: list[dict[str, Any]]
) -> tuple[list[str], bool]:
    index_by_id = {component_id: index for index, component_id in enumerate(component_ids)}
    outgoing: dict[str, set[str]] = {component_id: set() for component_id in component_ids}
    indegree = {component_id: 0 for component_id in component_ids}
    for edge in dependency_edges:
        source = str(edge.get("source_component_id", ""))
        target = str(edge.get("target_component_id", ""))
        if source not in outgoing or target not in indegree or target in outgoing[source]:
            continue
        outgoing[source].add(target)
        indegree[target] += 1
    ready = [component_id for component_id in component_ids if indegree[component_id] == 0]
    result: list[str] = []
    while ready:
        ready.sort(key=index_by_id.__getitem__)
        component_id = ready.pop(0)
        result.append(component_id)
        for target in sorted(outgoing[component_id], key=index_by_id.__getitem__):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    dependency_cycle = len(result) != len(component_ids)
    if dependency_cycle:
        result.extend(component_id for component_id in component_ids if component_id not in result)
    return result, dependency_cycle


def normalized_actor_props(
    prefab_name: str,
    actor_index: int,
    actor: dict[str, Any],
    template_map: dict[str, dict[str, Any]],
    texture_index: dict[str, Any],
) -> dict[str, Any]:
    props = actor.get("props", {})
    if not isinstance(props, dict):
        props = {}
    class_name = str(actor.get("class", "")).strip()
    actor_name = str(actor.get("name", "")).strip()
    template_id = particle_template_id(prefab_name, actor_index, class_name, actor_name)
    template = template_map.get(template_id, {})
    normalized = template.get("normalized", {}) if isinstance(template, dict) else {}
    if isinstance(normalized, dict) and normalized:
        return normalized.copy()
    return normalize_props(props, texture_index, class_name=class_name)


def normalized_with_parent_fx_scale(
    child_normalized: dict[str, Any], parent_normalized: dict[str, Any]
) -> dict[str, Any]:
    normalized = child_normalized.copy()
    parent_draw_scale = parent_normalized.get("draw_scale")
    if isinstance(parent_draw_scale, (int, float)):
        child_draw_scale = normalized.get("draw_scale")
        normalized["draw_scale"] = float(parent_draw_scale) * (
            float(child_draw_scale) if isinstance(child_draw_scale, (int, float)) else 1.0
        )
    parent_draw_scale_3d = parent_normalized.get("draw_scale_3d")
    if isinstance(parent_draw_scale_3d, list) and len(parent_draw_scale_3d) >= 3:
        child_draw_scale_3d = normalized.get("draw_scale_3d")
        if not (isinstance(child_draw_scale_3d, list) and len(child_draw_scale_3d) >= 3):
            child_draw_scale_3d = [1.0, 1.0, 1.0]
        normalized["draw_scale_3d"] = [
            float(parent_draw_scale_3d[axis]) * float(child_draw_scale_3d[axis])
            for axis in range(3)
        ]
    return normalized


def referenced_concrete_emitter_indices(emitters: list[Any]) -> set[int]:
    referenced: set[int] = set()
    for actor_value in emitters:
        if not isinstance(actor_value, dict):
            continue
        props = actor_value.get("props", {})
        if not isinstance(props, dict):
            continue
        for child_index in fx_emitter_reference_indices(props, len(emitters)):
            child_actor = emitters[child_index] if 0 <= child_index < len(emitters) else None
            if not isinstance(child_actor, dict):
                continue
            child_kind = classify_emitter(str(child_actor.get("class", "")).strip())
            if is_concrete_emitter_kind(child_kind):
                referenced.add(child_index)
    return referenced


def fx_emitter_reference_indices(props: dict[str, Any], actor_count: int) -> list[int]:
    emitters = props.get("Emitters")
    if not isinstance(emitters, dict):
        return []
    count = int(emitters.get("count", 0) or 0)
    raw_hex = str(emitters.get("raw_hex", "") or "")
    if count <= 0 or not raw_hex:
        return []
    try:
        raw = bytes.fromhex(raw_hex)
    except ValueError:
        return []
    if len(raw) < count + 1:
        return []
    refs = list(raw[-count:])
    indices: list[int] = []
    for ref in refs:
        index = int(ref) - 1
        if 0 <= index < actor_count:
            indices.append(index)
    return indices


def is_concrete_emitter_kind(kind: str) -> bool:
    return kind in {"sprite", "beam", "mesh", "light"}


def compact_node_transform(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "translation": compact_vector(vector_from_list(node.get("translation"), [0.0, 0.0, 0.0])),
        "rotation": compact_vector(vector_from_list(node.get("rotation"), [0.0, 0.0, 0.0, 1.0]), 4),
        "scale": compact_vector(vector_from_list(node.get("scale"), [1.0, 1.0, 1.0])),
    }


def emitter_local_position(normalized: dict[str, Any]) -> tuple[tuple[float, float, float], str]:
    actor_location = normalized.get("actor_location")
    if isinstance(actor_location, list) and len(actor_location) >= 3:
        return vang_offset_to_gltf(actor_location), "Location"
    start_location_offset = normalized.get("start_location_offset")
    if isinstance(start_location_offset, list) and len(start_location_offset) >= 3:
        return vang_offset_to_gltf(start_location_offset), "StartLocationOffset"
    return (0.0, 0.0, 0.0), "prefab_origin"


def transform_local_position(
    node_transform: dict[str, Any], local_position: tuple[float, float, float]
) -> tuple[float, float, float]:
    translation = vector_from_list(node_transform.get("translation"), [0.0, 0.0, 0.0])
    rotation = vector_from_list(node_transform.get("rotation"), [0.0, 0.0, 0.0, 1.0])
    scale = vector_from_list(node_transform.get("scale"), [1.0, 1.0, 1.0])
    scaled = (
        local_position[0] * scale[0],
        local_position[1] * scale[1],
        local_position[2] * scale[2],
    )
    rotated = quat_rotate(rotation, scaled)
    return (
        translation[0] + rotated[0],
        translation[1] + rotated[1],
        translation[2] + rotated[2],
    )


def source_affine_transform_gltf_position(
    transform: AffineTransform, position: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Apply a Vanguard-source affine transform to a converted Godot point."""

    source = (position[2], -position[0], position[1])
    transformed = tuple(
        transform.origin[row]
        + sum(transform.basis[row][column] * source[column] for column in range(3))
        for row in range(3)
    )
    return (-transformed[1], transformed[2], transformed[0])


def quat_rotate(q: tuple[float, float, float, float], v: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z, w = q
    vx, vy, vz = v
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def add_record_to_cell(cells: dict[str, dict[str, Any]], cell_key: str, record: dict[str, Any]) -> None:
    position = tuple(float(value) for value in record["chunk_position"])
    global_position = tuple(float(value) for value in record["global_position"])
    cell = cells.setdefault(
        cell_key,
        {
            "record_count": 0,
            "center_sum": [0.0, 0.0, 0.0],
            "global_center_sum": [0.0, 0.0, 0.0],
            "bounds_min": list(position),
            "bounds_max": list(position),
            "global_bounds_min": list(global_position),
            "global_bounds_max": list(global_position),
            "class_counts": {},
            "kind_counts": {},
            "placements": [],
        },
    )
    cell["record_count"] += 1
    cell["placements"].append(record)
    for axis in range(3):
        cell["center_sum"][axis] += position[axis]
        cell["global_center_sum"][axis] += global_position[axis]
        cell["bounds_min"][axis] = min(float(cell["bounds_min"][axis]), position[axis])
        cell["bounds_max"][axis] = max(float(cell["bounds_max"][axis]), position[axis])
        cell["global_bounds_min"][axis] = min(float(cell["global_bounds_min"][axis]), global_position[axis])
        cell["global_bounds_max"][axis] = max(float(cell["global_bounds_max"][axis]), global_position[axis])
    bump_count(cell["class_counts"], str(record.get("class", "")))
    bump_count(cell["kind_counts"], str(record.get("kind", "")))


def finalize_cell(cell: dict[str, Any]) -> None:
    count = max(int(cell.get("record_count", 0)), 1)
    center_sum = cell.pop("center_sum", [0.0, 0.0, 0.0])
    global_center_sum = cell.pop("global_center_sum", [0.0, 0.0, 0.0])
    cell["center"] = compact_vector(tuple(float(value) / count for value in center_sum))
    cell["global_center"] = compact_vector(tuple(float(value) / count for value in global_center_sum))
    cell["bounds_min"] = compact_vector(cell["bounds_min"])
    cell["bounds_max"] = compact_vector(cell["bounds_max"])
    cell["global_bounds_min"] = compact_vector(cell["global_bounds_min"])
    cell["global_bounds_max"] = compact_vector(cell["global_bounds_max"])


def bump_count(counts: dict[str, int], key: str) -> None:
    counts[key] = int(counts.get(key, 0)) + 1


def vector_or_none(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    return (float(value[0]), float(value[1]), float(value[2]))


def merge_position_bounds(global_cell: dict[str, Any], position: tuple[float, float, float]) -> None:
    values = [float(position[0]), float(position[1]), float(position[2])]
    if global_cell.get("global_bounds_min") is None:
        global_cell["global_bounds_min"] = values.copy()
        global_cell["global_bounds_max"] = values.copy()
        return
    bounds_min = global_cell["global_bounds_min"]
    bounds_max = global_cell["global_bounds_max"]
    for axis in range(3):
        bounds_min[axis] = min(float(bounds_min[axis]), values[axis])
        bounds_max[axis] = max(float(bounds_max[axis]), values[axis])


def add_weighted_center(
    global_cell: dict[str, Any], position: tuple[float, float, float], weight: int
) -> None:
    center_sum = global_cell["global_center_sum"]
    for axis in range(3):
        center_sum[axis] += float(position[axis]) * weight
    global_cell["global_center_weight"] = int(global_cell.get("global_center_weight", 0)) + weight


def vector_from_list(value: Any, default: list[float]) -> tuple[float, ...]:
    if isinstance(value, list) and len(value) >= len(default):
        return tuple(float(value[index]) for index in range(len(default)))
    return tuple(float(value) for value in default)


def compact_vector(values: Any, size: int = 3) -> list[float]:
    if not isinstance(values, (list, tuple)):
        return [0.0 for _ in range(size)]
    return [round(float(values[index]), 6) for index in range(min(size, len(values)))]


def vang_offset_to_gltf(value: list[float]) -> tuple[float, float, float]:
    return (-float(value[1]), float(value[2]), float(value[0]))


def cell_key_for_position(position: tuple[float, float, float], cell_size: float) -> str:
    x = math.floor(position[0] / cell_size)
    z = math.floor(position[2] / cell_size)
    return f"{x}:{z}"


def chunk_global_origin(chunk_name: str) -> tuple[float, float, float]:
    chunk_x, chunk_y = chunk_coords(chunk_name)
    return (
        float(chunk_y) * TERRAIN_CHUNK_WORLD_SIZE,
        0.0,
        float(chunk_x) * TERRAIN_CHUNK_WORLD_SIZE,
    )


def chunk_coords(chunk_name: str) -> tuple[int, int]:
    coord_text = normalize_chunk_name(chunk_name).removeprefix("chunk_")
    parts = coord_text.split("_")
    if len(parts) != 2:
        return (0, 0)
    return (_parse_chunk_coord(parts[0]), _parse_chunk_coord(parts[1]))


def normalize_chunk_name(chunk_name: str) -> str:
    normalized = str(chunk_name).strip()
    normalized = Path(normalized).stem
    for suffix in ("_terrain", "_objects", "_sgo"):
        if normalized.endswith(suffix):
            normalized = normalized.removesuffix(suffix)
    if not normalized.startswith("chunk_"):
        normalized = "chunk_" + normalized
    return normalized


def _parse_chunk_coord(value: str) -> int:
    return -int(value[1:]) if value.startswith("n") else int(value)


def add_vector(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
