#!/usr/bin/env python3
"""Audit a Phase 8 BSP/SGO interior source publication and its source files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]

from vanguard_assets import config  # noqa: E402
REPO_ROOT = config.PROJECT_ROOT
from scripts.lib.world_residency_interiors import canonical_json_bytes  # noqa: E402


SCHEMA = "vanguard_world_interior_source_publication"
VERSION = 2


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    try:
        path = args.publication.resolve()
        raw = path.read_bytes()
        publication = json.loads(raw)
        errors.extend(audit_publication(publication))
        if not args.skip_source_files:
            errors.extend(
                audit_source_files(
                    publication,
                    maps_root=args.maps_root.resolve(),
                    sgo_raw=args.sgo_raw.resolve(),
                    terrain_grid_root=args.terrain_grid_root.resolve(),
                    object_index_root=args.object_index_root.resolve(),
                    mesh_root=args.mesh_root.resolve(),
                    source_pack_manifest=(
                        args.source_pack_manifest.resolve()
                        if args.source_pack_manifest
                        else None
                    ),
                    source_terrain_inventory=(
                        args.source_terrain_inventory.resolve()
                        if args.source_terrain_inventory
                        else None
                    ),
                )
            )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(str(error))
        publication = {}
        raw = b""

    report = {
        "error_count": len(errors),
        "errors": errors,
        "publication_bytes": len(raw),
        "publication_file_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
        "publication_id": publication.get("publication_id"),
        "schema": "vanguard_world_interior_source_audit",
        "source_files_verified": not args.skip_source_files,
        "version": 1,
    }
    if args.report:
        write_json(args.report.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if errors else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, required=True)
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
    source.add_argument("--source-terrain-inventory", type=Path)
    parser.add_argument("--skip-source-files", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def audit_publication(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if value.get("schema") != SCHEMA:
        errors.append(f"schema is {value.get('schema')!r}; expected {SCHEMA!r}")
    if value.get("version") != VERSION:
        errors.append(f"version is {value.get('version')!r}; expected {VERSION}")
    identity = {
        key: item
        for key, item in value.items()
        if key not in {"content_revision", "publication_id"}
    }
    digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    if value.get("content_revision") != f"sha256:{digest}":
        errors.append("content_revision does not match canonical publication content")
    if value.get("publication_id") != f"interior_source_publication_{digest[:32]}":
        errors.append("publication_id does not match canonical publication content")

    templates = value.get("interior_templates")
    instances = value.get("instances")
    bsp = value.get("bsp_authority")
    if not isinstance(templates, list) or not templates:
        errors.append("interior_templates is empty or invalid")
        templates = []
    if not isinstance(instances, list) or not instances:
        errors.append("instances is empty or invalid")
        instances = []
    if not isinstance(bsp, list) or not bsp:
        errors.append("bsp_authority is empty or invalid")
        bsp = []

    template_ids = [str(item.get("interior_space_asset_id", "")) for item in templates]
    if any(not value for value in template_ids) or len(template_ids) != len(set(template_ids)):
        errors.append("interior template IDs are empty or duplicated")
    template_by_id = {
        str(item.get("interior_space_asset_id")): item for item in templates
    }
    instance_ids = [str(item.get("interior_instance_id", "")) for item in instances]
    if any(not value for value in instance_ids) or len(instance_ids) != len(set(instance_ids)):
        errors.append("interior instance IDs are empty or duplicated")

    computed = {
        "bsp_bound_count": sum(int(item.get("bound_count", 0)) for item in bsp),
        "bsp_leaf_count": sum(int(item.get("leaf_count", 0)) for item in bsp),
        "bsp_leaf_hull_count": sum(int(item.get("leaf_hull_count", 0)) for item in bsp),
        "bsp_node_count": sum(int(item.get("node_count", 0)) for item in bsp),
        "bsp_zone_count": sum(int(item.get("zone_count", 0)) for item in bsp),
        "interior_instance_count": len(instances),
        "interior_template_count": len(templates),
        "portal_boundary_count": sum(
            len(item.get("portal_graph", {}).get("boundaries", [])) for item in templates
        ),
        "portal_connection_count": sum(
            len(item.get("portal_graph", {}).get("connections", [])) for item in templates
        ),
        "portal_endpoint_count": sum(
            len(item.get("portal_graph", {}).get("endpoints", [])) for item in templates
        ),
        "portal_aperture_mesh_count": len(value.get("portal_aperture_meshes", [])),
        "portal_aperture_unavailable_endpoint_count": sum(
            1
            for item in templates
            for endpoint in item.get("portal_graph", {}).get("endpoints", [])
            if endpoint.get("aperture_status") != "exact"
        ),
        "room_count": sum(len(item.get("rooms", [])) for item in templates),
        "runtime_eligible_template_count": sum(
            1
            for item in templates
            if item.get("runtime_eligibility", {}).get("eligible") is True
        ),
        "runtime_eligible_instance_count": sum(
            1
            for item in instances
            if item.get("runtime_eligibility", {}).get("eligible") is True
        ),
        "unavailable_room_visual_binding_count": sum(
            len(binding.get("missing_visual_component_paths", []))
            for item in instances
            for binding in item.get("room_visual_bindings", [])
        ),
        "unresolved_portal_cluster_count": sum(
            len(item.get("portal_graph", {}).get("unresolved", [])) for item in templates
        ),
    }
    published_counts = value.get("counts", {})
    for key, expected in computed.items():
        if published_counts.get(key) != expected:
            errors.append(
                f"counts.{key}={published_counts.get(key)!r}; computed {expected}"
            )

    for template in templates:
        errors.extend(audit_template(template))
    aperture_meshes = value.get("portal_aperture_meshes", [])
    if not isinstance(aperture_meshes, list) or not aperture_meshes:
        errors.append("portal_aperture_meshes is empty or invalid")
        aperture_meshes = []
    aperture_ids = [str(item.get("aperture_mesh_id", "")) for item in aperture_meshes]
    if any(not item for item in aperture_ids) or len(aperture_ids) != len(set(aperture_ids)):
        errors.append("portal aperture mesh IDs are empty or duplicated")
    known_apertures = set(aperture_ids)
    for template in templates:
        for endpoint in template.get("portal_graph", {}).get("endpoints", []):
            aperture = endpoint.get("aperture_geometry", {})
            aperture_id = str(endpoint.get("aperture_mesh_id", ""))
            if endpoint.get("aperture_status") != "exact":
                if aperture_id or aperture:
                    errors.append(
                        f"{template.get('root_prefab')}: unavailable endpoint publishes geometry"
                    )
                if not str(endpoint.get("aperture_unavailable_reason", "")):
                    errors.append(
                        f"{template.get('root_prefab')}: unavailable endpoint has no reason"
                    )
                continue
            if aperture_id not in known_apertures:
                errors.append(f"{template.get('root_prefab')}: endpoint has unknown aperture mesh")
            if aperture.get("aperture_mesh_id") != aperture_id:
                errors.append(f"{template.get('root_prefab')}: endpoint aperture identity mismatch")
            if int(aperture.get("triangle_count", 0)) <= 0 or int(
                aperture.get("vertex_count", 0)
            ) <= 0:
                errors.append(f"{template.get('root_prefab')}: endpoint aperture is empty")
    for instance in instances:
        template = template_by_id.get(str(instance.get("interior_space_asset_id", "")))
        if template is None:
            errors.append(
                f"instance {instance.get('interior_instance_id')} references an unknown template"
            )
            continue
        errors.extend(audit_instance(instance, template))
    for record in bsp:
        errors.extend(audit_bsp_record(record))

    binding = value.get("audit", {}).get("binding", {})
    if binding.get("missing_template_instance_count") != 0:
        errors.append("production publication has interior templates without instances")
    if binding.get("missing_room_visual_binding_count") != computed[
        "unavailable_room_visual_binding_count"
    ]:
        errors.append("room visual binding audit count does not match instances")
    return errors


def audit_template(template: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    label = str(template.get("root_prefab", "<unnamed>"))
    rooms = template.get("rooms", [])
    room_ids = [str(room.get("room_id", "")) for room in rooms]
    if any(not value for value in room_ids) or len(room_ids) != len(set(room_ids)):
        errors.append(f"{label}: room IDs are empty or duplicated")
    room_id_set = set(room_ids)
    graph = template.get("portal_graph", {})
    endpoints = graph.get("endpoints", [])
    endpoint_ids = [str(item.get("endpoint_id", "")) for item in endpoints]
    if any(not value for value in endpoint_ids) or len(endpoint_ids) != len(set(endpoint_ids)):
        errors.append(f"{label}: portal endpoint IDs are empty or duplicated")
    endpoint_id_set = set(endpoint_ids)
    ownership: dict[str, int] = {value: 0 for value in endpoint_ids}
    expected_adjacency: dict[str, set[str]] = {value: set() for value in room_ids}
    for endpoint in endpoints:
        if str(endpoint.get("room_id", "")) not in room_id_set:
            errors.append(f"{label}: endpoint references an unknown room")
    for connection in graph.get("connections", []):
        refs = [str(value) for value in connection.get("endpoint_ids", [])]
        rooms_ref = [str(value) for value in connection.get("room_ids", [])]
        if len(refs) != 2 or any(value not in endpoint_id_set for value in refs):
            errors.append(f"{label}: portal connection has invalid endpoint references")
            continue
        if len(rooms_ref) != 2 or len(set(rooms_ref)) != 2 or any(
            value not in room_id_set for value in rooms_ref
        ):
            errors.append(f"{label}: portal connection has invalid room references")
            continue
        ownership[refs[0]] += 1
        ownership[refs[1]] += 1
        expected_adjacency[rooms_ref[0]].add(rooms_ref[1])
        expected_adjacency[rooms_ref[1]].add(rooms_ref[0])
    for boundary in graph.get("boundaries", []):
        endpoint_id = str(boundary.get("endpoint_id", ""))
        if endpoint_id not in endpoint_id_set:
            errors.append(f"{label}: boundary references an unknown endpoint")
        else:
            ownership[endpoint_id] += 1
    for cluster in graph.get("unresolved", []):
        for endpoint_id in cluster.get("endpoint_ids", []):
            endpoint_id = str(endpoint_id)
            if endpoint_id not in endpoint_id_set:
                errors.append(f"{label}: unresolved cluster references an unknown endpoint")
            else:
                ownership[endpoint_id] += 1
    bad_ownership = sorted(key for key, count in ownership.items() if count != 1)
    if bad_ownership:
        errors.append(
            f"{label}: {len(bad_ownership)} endpoints do not have exactly one graph role"
        )
    published_adjacency = {
        str(item.get("room_id")): set(str(value) for value in item.get("visible_room_ids", []))
        for item in graph.get("adjacency", [])
    }
    if published_adjacency != expected_adjacency:
        errors.append(f"{label}: published room adjacency does not match connections")
    entrance_ids = set(str(value) for value in template.get("entrance_room_ids", []))
    boundary_ids = set(str(value.get("room_id")) for value in graph.get("boundaries", []))
    if entrance_ids != boundary_ids:
        errors.append(f"{label}: entrance rooms do not match boundary endpoints")
    eligibility = template.get("runtime_eligibility", {})
    unavailable_aperture_ids = {
        str(endpoint.get("endpoint_id"))
        for endpoint in endpoints
        if endpoint.get("aperture_status") != "exact"
    }
    expected_eligible = (
        bool(boundary_ids)
        and not graph.get("unresolved", [])
        and not unavailable_aperture_ids
    )
    if eligibility.get("eligible") is not expected_eligible:
        errors.append(f"{label}: runtime eligibility does not match graph safety")
    if set(eligibility.get("unavailable_aperture_endpoint_ids", [])) != unavailable_aperture_ids:
        errors.append(f"{label}: unavailable aperture endpoint set does not match graph")
    unresolved_room_ids = sorted(
        {
            str(room_id)
            for cluster in graph.get("unresolved", [])
            for room_id in cluster.get("room_ids", [])
        }
    )
    if eligibility.get("unresolved_room_ids") != unresolved_room_ids:
        errors.append(f"{label}: unresolved room set does not match graph")
    expected_reason = (
        "eligible"
        if expected_eligible
        else "no_authored_boundary_endpoint"
        if not boundary_ids
        else "unavailable_portal_aperture"
        if unavailable_aperture_ids
        else "unresolved_portal_cluster"
    )
    if eligibility.get("reason") != expected_reason:
        errors.append(f"{label}: runtime eligibility reason does not match graph safety")
    return errors


def audit_instance(instance: dict[str, Any], template: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    label = str(instance.get("interior_instance_id", "<unnamed>"))
    expected_by_room = {
        str(room.get("room_id")): {
            str(component.get("source_component_path"))
            for component in room.get("visual_components", [])
        }
        for room in template.get("rooms", [])
    }
    bindings = instance.get("room_visual_bindings", [])
    if {str(item.get("room_id")) for item in bindings} != set(expected_by_room):
        errors.append(f"{label}: room visual binding set does not match template rooms")
        return errors
    for binding in bindings:
        room_id = str(binding.get("room_id"))
        available = set(str(value) for value in binding.get("available_visual_component_paths", []))
        missing = set(str(value) for value in binding.get("missing_visual_component_paths", []))
        if available & missing or available | missing != expected_by_room[room_id]:
            errors.append(f"{label}/{room_id}: visual binding partition is invalid")
    missing_paths = sorted(
        str(path)
        for binding in bindings
        for path in binding.get("missing_visual_component_paths", [])
    )
    template_eligible = template.get("runtime_eligibility", {}).get("eligible") is True
    expected_eligible = template_eligible and not missing_paths
    eligibility = instance.get("runtime_eligibility", {})
    if eligibility.get("eligible") is not expected_eligible:
        errors.append(f"{label}: runtime eligibility does not match visual safety")
    if eligibility.get("missing_visual_component_paths") != missing_paths:
        errors.append(f"{label}: runtime missing visual component set does not match bindings")
    expected_reason = (
        "eligible"
        if expected_eligible
        else "template_ineligible"
        if not template_eligible
        else "unavailable_room_visual_binding"
    )
    if eligibility.get("reason") != expected_reason:
        errors.append(f"{label}: runtime eligibility reason does not match visual safety")
    return errors


def audit_bsp_record(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    label = str(record.get("chunk", "<unnamed>"))
    nodes = record.get("nodes", [])
    zones = record.get("zones", [])
    leaves = record.get("leaves", [])
    bounds = record.get("bounds", [])
    leaf_hulls = record.get("leaf_hulls", [])
    if record.get("node_count") != len(nodes):
        errors.append(f"{label}: BSP node_count mismatch")
    if record.get("zone_count") != len(zones):
        errors.append(f"{label}: BSP zone_count mismatch")
    if record.get("leaf_count") != len(leaves):
        errors.append(f"{label}: BSP leaf_count mismatch")
    if record.get("bound_count") != len(bounds):
        errors.append(f"{label}: BSP bound_count mismatch")
    if record.get("leaf_hull_count") != len(leaf_hulls):
        errors.append(f"{label}: BSP leaf_hull_count mismatch")
    for node in nodes:
        for zone_index in node.get("zone_indices", []):
            if not isinstance(zone_index, int) or not 0 <= zone_index < len(zones):
                errors.append(f"{label}: BSP node has an invalid zone index")
        for leaf_index in node.get("leaf_indices", []):
            if leaf_index != -1 and (
                not isinstance(leaf_index, int) or not 0 <= leaf_index < len(leaves)
            ):
                errors.append(f"{label}: BSP node has an invalid leaf index")
        collision_offset = node.get("collision_leaf_hull_offset")
        if collision_offset != -1 and (
            not isinstance(collision_offset, int)
            or not 0 <= collision_offset < len(leaf_hulls)
        ):
            errors.append(f"{label}: BSP node has an invalid collision leaf-hull offset")
        render_bound_index = node.get("render_bound_index")
        if render_bound_index != -1 and (
            not isinstance(render_bound_index, int)
            or not 0 <= render_bound_index < len(bounds)
        ):
            errors.append(f"{label}: BSP node has an invalid render bound index")
    for leaf in leaves:
        zone_index = leaf.get("zone_index")
        if not isinstance(zone_index, int) or not 0 <= zone_index < len(zones):
            errors.append(f"{label}: BSP leaf has an invalid zone index")
    return errors


def audit_source_files(
    publication: dict[str, Any],
    *,
    maps_root: Path,
    sgo_raw: Path,
    terrain_grid_root: Path,
    object_index_root: Path,
    mesh_root: Path,
    source_pack_manifest: Path | None,
    source_terrain_inventory: Path | None,
) -> list[str]:
    errors: list[str] = []

    def verify(path: Path, expected: str, label: str) -> None:
        if not path.is_file():
            errors.append(f"missing {label}: {path}")
            return
        digest_state = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                block = handle.read(4 * 1024 * 1024)
                if not block:
                    break
                digest_state.update(block)
        digest = digest_state.hexdigest()
        if digest != expected:
            errors.append(f"{label} digest mismatch: {path}")

    source = publication.get("source_contract", {})
    verify(
        sgo_raw,
        str(source.get("sgo_raw", {}).get("sha256", "")),
        "raw SGO JSONL",
    )
    if source_pack_manifest is not None:
        verify(
            source_pack_manifest,
            str(source.get("source_pack", {}).get("manifest_sha256", "")),
            "source pack manifest",
        )
    elif source_terrain_inventory is not None:
        verify(
            source_terrain_inventory,
            str(source.get("source_terrain_inventory", {}).get("sha256", "")),
            "source terrain inventory",
        )
    else:
        errors.append("no source pack or terrain inventory was supplied")
    for item in source.get("sgo_chunk_sidecars", []):
        verify(
            terrain_grid_root / f"{item.get('chunk')}_sgo.json",
            str(item.get("sha256", "")),
            f"{item.get('chunk')} SGO sidecar",
        )
    for item in source.get("object_indices", []):
        verify(
            object_index_root / str(item.get("chunk")) / "object_cells.json",
            str(item.get("sha256", "")),
            f"{item.get('chunk')} object index",
        )
    for item in publication.get("bsp_authority", []):
        verify(
            maps_root / f"{item.get('chunk')}.vgr",
            str(item.get("source_package_sha256", "")),
            f"{item.get('chunk')} map package",
        )
    for item in publication.get("portal_aperture_meshes", []):
        expected = str(item.get("source_gltf_sha256", ""))
        if expected.startswith("sha256:"):
            expected = expected.removeprefix("sha256:")
        verify(
            mesh_root / str(item.get("source_gltf_relative_path", "")),
            expected,
            f"portal aperture {item.get('aperture_mesh_id')}",
        )
    return errors


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


if __name__ == "__main__":
    raise SystemExit(main())
