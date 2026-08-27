#!/usr/bin/env python3
"""Publish exact room packs and shared runtime GLBs for authored interiors.

The publication includes room visuals that will move out of Cesium plus authored
SGO movers and hidden collision helpers that ordinary chunk generation does not
see. This generator resolves all of them into the same content-addressed shared
library, emits reusable room presentation packs, and publishes the compact,
fail-closed boundary that permits exact room-owned placements to leave Cesium.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]

from scripts.generators.generate_godot_runtime_chunk import (  # noqa: E402
    NATIVE_SCENE_PACK_VERSION,
    RUNTIME_PACK_VERSION,
    SHARED_ASSET_LIBRARY_VERSION,
    StaticMeshSourceIndex,
    assert_free_space,
    has_valid_triangle_indices,
    materialize_mesh,
    mesh_source_signature,
    native_scene_relative_path,
    preserve_shared_native_fields,
    runtime_relative_path_for_asset,
    safe_relative_path,
    shared_asset_id,
    shared_asset_manifest_entries,
    shared_asset_manifest_entry,
)
from scripts.lib.interior_portal_runtime import build_portal_runtime_catalog  # noqa: E402


SOURCE_SCHEMA = "vanguard_world_interior_source_publication"
SOURCE_VERSION = 2
SELECTION_SCHEMA = "vanguard_godot_runtime_interior_asset_selection"
SELECTION_VERSION = 1
BOUNDARY_SCHEMA = "vanguard_interior_cesium_boundary"
BOUNDARY_VERSION = 1
ROOM_PACK_SCHEMA = "vanguard_interior_room_presentation_pack"
ROOM_PACK_VERSION = 1
GENERATOR_POLICY = "authoritative_sgo_room_visuals_movers_and_hidden_collision_helpers_v2"


@dataclass(frozen=True)
class MeshSelection:
    mesh_path: str
    mesh_name: str
    roles: tuple[str, ...]
    occurrence_count: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-authority", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument(
        "--boundary-output",
        type=Path,
        help=(
            "Compact exact Cesium exclusion/replacement manifest. Defaults beside "
            "the source interior publication."
        ),
    )
    parser.add_argument(
        "--portal-runtime-output",
        type=Path,
        help=(
            "Compact room bounds, portal graph, aperture geometry, and instance "
            "mapping catalog. Defaults beside the Cesium boundary."
        ),
    )
    parser.add_argument(
        "--static-mesh-source-index",
        type=Path,
        default=REPO_ROOT / "output/data/staticmesh_source_index.tsv",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--selection-only",
        action="store_true",
        help=(
            "Publish a source-rebound selection manifest without rewriting the "
            "shared asset manifest. Every selected runtime asset must already be "
            "present and current."
        ),
    )
    parser.add_argument("--free-space-reserve-gb", type=float, default=10.0)
    args = parser.parse_args()
    if args.selection_only and args.force:
        parser.error("--selection-only cannot be combined with --force")
    if args.selection_only and args.dry_run:
        parser.error("--selection-only cannot be combined with --dry-run")
    try:
        result = generate_runtime_assets(
            source_authority_path=args.source_authority,
            output_root=args.output_root,
            runtime_root=args.runtime_root,
            boundary_output_path=args.boundary_output,
            portal_runtime_output_path=args.portal_runtime_output,
            static_mesh_source_index_path=args.static_mesh_source_index,
            force=args.force,
            dry_run=args.dry_run,
            selection_only=args.selection_only,
            reserve_bytes=int(args.free_space_reserve_gb * 1024**3),
        )
    except (OSError, ValueError) as error:
        print(f"Interior runtime asset generation failed: {error}", file=sys.stderr)
        return 1
    label = "Interior runtime asset dry run" if args.dry_run else "Interior runtime assets"
    print(
        "%s: selection=%s meshes=%d written=%d existing=%d source_bytes=%d "
        "runtime_bytes=%d manifest=%s boundary=%s portal_runtime=%s"
        % (
            label,
            result["selection_id"],
            result["mesh_count"],
            result["written_count"],
            result["existing_count"],
            result["source_bytes"],
            result["runtime_bytes"],
            result["manifest_path"],
            result["boundary_path"],
            result["portal_runtime_path"],
        )
    )
    return 0


def generate_runtime_assets(
    *,
    source_authority_path: Path,
    output_root: Path,
    runtime_root: Path,
    boundary_output_path: Path | None = None,
    portal_runtime_output_path: Path | None = None,
    static_mesh_source_index_path: Path,
    force: bool,
    dry_run: bool,
    selection_only: bool = False,
    reserve_bytes: int,
) -> dict[str, Any]:
    source_authority_path = source_authority_path.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    runtime_root = runtime_root.expanduser().resolve()
    boundary_output_path = (
        boundary_output_path.expanduser().resolve()
        if boundary_output_path is not None
        else source_authority_path.with_name("interior_cesium_boundary.v1.json")
    )
    portal_runtime_output_path = (
        portal_runtime_output_path.expanduser().resolve()
        if portal_runtime_output_path is not None
        else boundary_output_path.with_name("interior_portal_runtime.v1.json")
    )
    static_mesh_source_index_path = static_mesh_source_index_path.expanduser().resolve()
    source_bytes = source_authority_path.read_bytes()
    source = json.loads(source_bytes)
    if (
        not isinstance(source, dict)
        or source.get("schema") != SOURCE_SCHEMA
        or int(source.get("version", 0)) != SOURCE_VERSION
        or not isinstance(source.get("interior_templates"), list)
    ):
        raise ValueError(f"interior source publication is incompatible: {source_authority_path}")
    source_identity = {
        key: value
        for key, value in source.items()
        if key not in {"content_revision", "publication_id"}
    }
    source_revision = _canonical_sha256(source_identity)
    if (
        source.get("content_revision") != f"sha256:{source_revision}"
        or source.get("publication_id")
        != f"interior_source_publication_{source_revision[:32]}"
    ):
        raise ValueError(
            f"interior source publication identity is not canonical: {source_authority_path}"
        )
    selections = collect_mesh_selections(
        source, canonical_mesh_paths(output_root / "meshes/buildings/manifest.json")
    )
    if not selections:
        raise ValueError("interior source publication has no runtime presentation meshes")
    identity = {
        "policy": GENERATOR_POLICY,
        "source_publication_id": str(source.get("publication_id", "")),
        "source_content_revision": str(source.get("content_revision", "")),
        "source_publication_sha256": f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
        "meshes": [
            {
                "mesh_path": value.mesh_path,
                "mesh_name": value.mesh_name,
                "roles": list(value.roles),
                "occurrence_count": value.occurrence_count,
            }
            for value in selections
        ],
        "tool_signatures": {
            "generate_godot_runtime_interior_assets.py": (
                f"sha256:{_file_sha256(Path(__file__).resolve())}"
            ),
            "generate_godot_runtime_chunk.py": (
                "sha256:"
                + _file_sha256(Path(__file__).resolve().with_name("generate_godot_runtime_chunk.py"))
            ),
            "interior_portal_runtime.py": (
                "sha256:"
                + _file_sha256(
                    Path(__file__).resolve().parents[1] / "lib/interior_portal_runtime.py"
                )
            ),
        },
    }
    selection_id = "interior_runtime_assets_" + _canonical_sha256(identity)[:32]
    source_index = StaticMeshSourceIndex(static_mesh_source_index_path)
    previous_assets = shared_asset_manifest_entries(runtime_root)
    mesh_root = output_root / "meshes/buildings"
    entries: dict[str, dict[str, Any]] = {}
    estimated_write_bytes = 0
    total_source_bytes = 0
    written_count = 0
    existing_count = 0
    for selection in selections:
        safe_path = safe_relative_path(selection.mesh_path)
        source_path = mesh_root / safe_path
        if not source_path.is_file():
            raise ValueError(
                f"authoritative interior source mesh is absent: {selection.mesh_path}: {source_path}"
            )
        if not has_valid_triangle_indices(source_path):
            raise ValueError(
                f"authoritative interior mesh has invalid triangle indices: {source_path}"
            )
        source_info = mesh_source_signature(source_path)
        total_source_bytes += int(source_info["source_bytes"])
        asset_id = shared_asset_id(
            selection.mesh_path,
            mode="glb",
            source_signature=str(source_info["source_signature"]),
        )
        runtime_relative = runtime_relative_path_for_asset(
            selection_id,
            safe_path,
            mode="glb",
            asset_storage="shared",
            source_signature=str(source_info["source_signature"]),
        )
        runtime_path = runtime_root / runtime_relative
        native_relative = native_scene_relative_path(runtime_relative)
        entry: dict[str, Any] = {
            "asset_id": asset_id,
            "asset_storage": "shared",
            "mesh_name": selection.mesh_name,
            "mode": "glb",
            "reason": "+".join(selection.roles),
            "reference_count": selection.occurrence_count,
            "runtime_pack_version": RUNTIME_PACK_VERSION,
            "runtime_relative_path": runtime_relative.as_posix(),
            "source_relative_path": (Path("meshes/buildings") / safe_path).as_posix(),
            "valid_triangle_indices": True,
            **source_info,
            **source_index.manifest_fields(selection.mesh_path, selection.mesh_name),
        }
        previous = previous_assets.get(asset_id, {})
        if isinstance(previous, dict):
            entry = preserve_shared_native_fields(entry, previous)
        fresh = (
            not force
            and runtime_path.is_file()
            and runtime_path.stat().st_size > 0
            and str(previous.get("source_signature", ""))
            == str(source_info["source_signature"])
        )
        if fresh:
            entry["runtime_bytes"] = runtime_path.stat().st_size
            entry["status"] = "existing"
            existing_count += 1
        else:
            estimated_write_bytes += int(int(source_info["source_bytes"]) * 0.82)
            entry["status"] = "planned" if dry_run else "pending"
        if (runtime_root / native_relative).is_file() and not force:
            entry.setdefault("native_scene_relative_path", native_relative.as_posix())
        entries[selection.mesh_path] = entry

    if selection_only:
        stale = sorted(
            mesh_path
            for mesh_path, entry in entries.items()
            if entry["status"] != "existing"
        )
        if stale:
            raise ValueError(
                "selection-only publication requires every runtime asset to be "
                f"current; stale or missing meshes={stale[:8]}"
            )

    if not dry_run and not selection_only:
        assert_free_space(runtime_root, estimated_write_bytes, reserve_bytes)
        for mesh_path in sorted(entries):
            entry = entries[mesh_path]
            if entry["status"] == "existing":
                continue
            source_path = output_root / str(entry["source_relative_path"])
            runtime_path = runtime_root / str(entry["runtime_relative_path"])
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            materialize_mesh(source_path, runtime_path, "glb")
            entry["runtime_bytes"] = runtime_path.stat().st_size
            entry["status"] = "written"
            for stale in (
                "native_scene_relative_path",
                "native_scene_bytes",
                "native_scene_status",
            ):
                entry.pop(stale, None)
            written_count += 1

    manifest_path = runtime_root / "selections" / selection_id / "manifest.json"
    manifest = {
        "version": RUNTIME_PACK_VERSION,
        "manifest_layout": "full",
        "generated_by": "scripts/generators/generate_godot_runtime_interior_assets.py",
        "generated_at_unix": int(time.time()),
        "chunk": "chunk_0_0",
        "selection_id": selection_id,
        "selection_schema": SELECTION_SCHEMA,
        "selection_version": SELECTION_VERSION,
        "selection_policy": GENERATOR_POLICY,
        "source_authority": identity,
        "mode": "glb",
        "asset_storage": "shared",
        "shared_asset_library_version": SHARED_ASSET_LIBRARY_VERSION,
        "shared_asset_manifest_relative_path": "assets/manifest.json",
        "source_output_root": str(output_root),
        "runtime_root": str(runtime_root),
        "mesh_count": len(entries),
        "native_scene_pack_version": NATIVE_SCENE_PACK_VERSION,
        "source_bytes": total_source_bytes,
        "runtime_bytes": sum(int(value.get("runtime_bytes", 0)) for value in entries.values()),
        "meshes": {key: entries[key] for key in sorted(entries)},
    }
    boundary, room_packs = build_interior_cesium_boundary(
        source,
        entries,
        selection_id,
        source_publication_sha256=hashlib.sha256(source_bytes).hexdigest(),
        require_ready=not dry_run,
    )
    portal_runtime_identity = build_portal_runtime_catalog(
        source,
        boundary,
        room_packs,
        mesh_root,
    )
    portal_runtime_revision = _canonical_sha256(portal_runtime_identity)
    portal_runtime = {
        **portal_runtime_identity,
        "catalog_id": f"interior_portal_runtime_{portal_runtime_revision[:32]}",
        "content_revision": f"sha256:{portal_runtime_revision}",
    }
    if not dry_run:
        _write_json_atomic(manifest_path, manifest)
        _write_room_packs(boundary_output_path, room_packs)
        _write_json_atomic(boundary_output_path, boundary)
        _write_json_atomic(portal_runtime_output_path, portal_runtime)
        if not selection_only:
            _merge_shared_manifest(runtime_root, entries, manifest_path)
    return {
        "selection_id": selection_id,
        "mesh_count": len(entries),
        "written_count": written_count,
        "existing_count": existing_count,
        "source_bytes": total_source_bytes,
        "runtime_bytes": manifest["runtime_bytes"],
        "estimated_write_bytes": estimated_write_bytes,
        "manifest_path": str(manifest_path),
        "boundary_path": str(boundary_output_path),
        "portal_runtime_path": str(portal_runtime_output_path),
    }


def canonical_mesh_paths(manifest_path: Path) -> dict[str, str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    meshes = manifest.get("meshes")
    if (
        manifest.get("status") != "complete"
        or manifest.get("scope") != "object_artifact"
        or not isinstance(meshes, list)
    ):
        raise ValueError(f"canonical building mesh manifest is incomplete: {manifest_path}")
    result: dict[str, str] = {}
    for value in meshes:
        path = str(value).replace("\\", "/")
        folded = path.casefold()
        previous = result.get(folded)
        if not path or (previous is not None and previous != path):
            raise ValueError(f"canonical building mesh paths collide by case: {path}")
        result[folded] = path
    return result


def collect_mesh_selections(
    source: dict[str, Any], canonical_paths: dict[str, str]
) -> list[MeshSelection]:
    by_path: dict[str, dict[str, Any]] = {}

    def add_path(raw_path: str, role: str) -> None:
        path = canonical_paths.get(raw_path.casefold(), raw_path)
        value = by_path.setdefault(
            path.casefold(),
            {
                "mesh_path": path,
                "mesh_name": Path(path).stem,
                "roles": set(),
                "count": 0,
            },
        )
        if value["mesh_path"] != path:
            raise ValueError(f"case-colliding interior mesh identities: {path}")
        value["roles"].add(role)
        value["count"] += 1

    def add(actor: object, role: str) -> None:
        if not isinstance(actor, dict):
            return
        static_source = actor.get("static_mesh_source")
        if not isinstance(static_source, dict):
            raise ValueError(
                f"interior {role} actor lacks static_mesh_source: "
                f"class={actor.get('class')} name={actor.get('name')} "
                f"path={actor.get('source_component_path')}"
            )
        package = str(static_source.get("source_package", "")).strip()
        name = str(static_source.get("name", actor.get("static_mesh", ""))).strip()
        if not package or not name:
            raise ValueError(f"interior {role} actor has an empty mesh identity")
        add_path(f"{package}/{name}.gltf", role)

    for template in source["interior_templates"]:
        if not isinstance(template, dict):
            raise ValueError("interior template is invalid")
        for room in template.get("rooms", []):
            for visual in room.get("visual_components", []):
                add(visual, "room_visual")
            for mover in room.get("movers", []):
                add(mover, "room_mover")
            for collision in room.get("collision_only_components", []):
                add(collision, "hidden_collision")
        transition = template.get("transition_actors", {})
        if isinstance(transition, dict):
            for mover in transition.get("movers", []):
                add(mover, "transition_mover")
    for instance in source.get("instances", []):
        for binding in instance.get("room_visual_bindings", []):
            for component in binding.get("available_visual_components", []):
                if not isinstance(component, dict) or not component.get("mesh_path"):
                    raise ValueError("interior placement binding lacks an exact mesh path")
                add_path(str(component["mesh_path"]), "room_visual")
    return [
        MeshSelection(
            mesh_path=value["mesh_path"],
            mesh_name=value["mesh_name"],
            roles=tuple(sorted(value["roles"])),
            occurrence_count=int(value["count"]),
        )
        for _, value in sorted(by_path.items())
    ]


def build_interior_cesium_boundary(
    source: dict[str, Any],
    entries: dict[str, dict[str, Any]],
    selection_id: str,
    *,
    source_publication_sha256: str,
    require_ready: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entries_by_fold = {path.casefold(): value for path, value in entries.items()}
    templates = source.get("interior_templates", [])
    instances = source.get("instances", [])
    if not isinstance(templates, list) or not isinstance(instances, list):
        raise ValueError("interior publication lacks templates or instances")
    template_by_id: dict[str, dict[str, Any]] = {}
    pack_by_template: dict[str, dict[str, Any]] = {}
    component_by_template_path: dict[str, dict[str, dict[str, Any]]] = {}
    room_packs: list[dict[str, Any]] = []
    placement_meshes: dict[tuple[str, str], set[str]] = {}
    eligible_template_ids: set[str] = set()
    for instance in instances:
        if not isinstance(instance, dict):
            raise ValueError("interior instance is invalid")
        if instance.get("runtime_eligibility", {}).get("eligible") is not True:
            continue
        template_id = str(instance.get("interior_space_asset_id", ""))
        eligible_template_ids.add(template_id)
        for binding in instance.get("room_visual_bindings", []):
            for component in binding.get("available_visual_components", []):
                if not isinstance(component, dict):
                    raise ValueError("interior placement binding is invalid")
                component_path = str(component.get("source_component_path", ""))
                mesh_path = str(component.get("mesh_path", "")).replace("\\", "/")
                if not component_path or not mesh_path:
                    raise ValueError("interior placement binding lacks exact identity")
                placement_meshes.setdefault((template_id, component_path), set()).add(
                    mesh_path
                )
    for template in templates:
        if not isinstance(template, dict):
            raise ValueError("interior template is invalid")
        template_id = str(template.get("interior_space_asset_id", ""))
        if not template_id or template_id in template_by_id:
            raise ValueError("interior template ID is empty or duplicated")
        template_by_id[template_id] = template
        if (
            template.get("runtime_eligibility", {}).get("eligible") is not True
            or template_id not in eligible_template_ids
        ):
            continue
        room_records: list[dict[str, Any]] = []
        component_by_path: dict[str, dict[str, Any]] = {}
        room_ids: set[str] = set()
        for room in template.get("rooms", []):
            room_id = str(room.get("room_id", ""))
            if not room_id or room_id in room_ids:
                raise ValueError(f"{template_id}: room has no unique stable ID")
            room_ids.add(room_id)
            components: list[dict[str, Any]] = []
            for actor in room.get("visual_components", []):
                component_path = str(actor.get("source_component_path", ""))
                static_source = actor.get("static_mesh_source")
                if not component_path or not isinstance(static_source, dict):
                    raise ValueError(f"{template_id}/{room_id}: visual lacks exact identity")
                package = str(static_source.get("source_package", "")).strip()
                name = str(static_source.get("name", actor.get("static_mesh", ""))).strip()
                if not package or not name:
                    raise ValueError(f"{template_id}/{room_id}: visual mesh identity is empty")
                source_mesh_path = f"{package}/{name}.gltf"
                resolved_meshes = placement_meshes.get((template_id, component_path), set())
                if len(resolved_meshes) != 1:
                    raise ValueError(
                        f"{template_id}/{room_id}: placement mesh identity is not unique: "
                        f"{sorted(resolved_meshes)[:4]}"
                    )
                mesh_path = next(iter(resolved_meshes))
                entry = entries_by_fold.get(mesh_path.casefold())
                if entry is None:
                    raise ValueError(f"{template_id}/{room_id}: replacement asset is absent")
                if require_ready and entry.get("status") not in {"existing", "written"}:
                    raise ValueError(f"{template_id}/{room_id}: replacement asset is not ready")
                component = {
                    "asset_id": str(entry.get("asset_id", "")),
                    "mesh_name": name,
                    "mesh_path": mesh_path,
                    "pack_component_index": len(component_by_path),
                    "source_class": str(actor.get("class", "")),
                    "source_component_path": component_path,
                    "source_name": str(actor.get("name", "")),
                    "source_static_mesh_path": source_mesh_path,
                    "transform": actor.get("transform"),
                    "runtime_relative_path": str(entry.get("runtime_relative_path", "")),
                }
                if (
                    not component["asset_id"]
                    or not component["runtime_relative_path"]
                    or component_path in component_by_path
                ):
                    raise ValueError(
                        f"{template_id}: visual component path is empty or duplicated: "
                        f"{component_path}"
                    )
                component_by_path[component_path] = {**component, "room_id": room_id}
                components.append(component)
            room_records.append(
                {
                    "room_id": room_id,
                    "source_component_path": str(room.get("source_component_path", "")),
                    "transform": room.get("transform"),
                    "visual_components": components,
                }
            )
        if not component_by_path:
            raise ValueError(f"{template_id}: eligible room pack has no visual components")
        pack_identity = {
            "interior_space_asset_id": template_id,
            "rooms": room_records,
            "root_prefab": str(template.get("root_prefab", "")),
            "schema": ROOM_PACK_SCHEMA,
            "source_publication_id": source.get("publication_id"),
            "version": ROOM_PACK_VERSION,
        }
        pack_revision = _canonical_sha256(pack_identity)
        pack = {
            **pack_identity,
            "content_revision": f"sha256:{pack_revision}",
            "room_pack_id": f"interior_room_pack_{pack_revision[:32]}",
        }
        pack_by_template[template_id] = pack
        component_by_template_path[template_id] = component_by_path
        room_packs.append(pack)

    exclusions: list[dict[str, Any]] = []
    eligible_instances: list[dict[str, Any]] = []
    fallback_instances: list[dict[str, Any]] = []
    fallback_placements: list[dict[str, Any]] = []
    exclusion_keys: set[tuple[Any, ...]] = set()
    fallback_keys: set[tuple[Any, ...]] = set()
    for instance in instances:
        if not isinstance(instance, dict):
            raise ValueError("interior instance is invalid")
        instance_id = str(instance.get("interior_instance_id", ""))
        template_id = str(instance.get("interior_space_asset_id", ""))
        template = template_by_id.get(template_id)
        if not instance_id or template is None:
            raise ValueError("interior instance has an invalid identity")
        eligibility = instance.get("runtime_eligibility", {})
        if eligibility.get("eligible") is not True:
            reason = str(eligibility.get("reason", "unavailable"))
            fallback_instances.append(
                {
                    "chunk": str(instance.get("chunk", "")),
                    "interior_instance_id": instance_id,
                    "interior_space_asset_id": template_id,
                    "reason": reason,
                }
            )
            for binding in instance.get("room_visual_bindings", []):
                room_id = str(binding.get("room_id", ""))
                for component_path_value in binding.get(
                    "available_visual_component_paths", []
                ):
                    component_path = str(component_path_value)
                    key = (
                        str(instance.get("chunk", "")),
                        int(instance.get("node_index", -1)),
                        str(instance.get("authoritative_source_object_id", "")),
                        str(instance.get("authoritative_source_node_id", "")),
                        component_path,
                    )
                    _validate_placement_key(instance_id, key)
                    if key in fallback_keys:
                        raise ValueError(f"duplicate Cesium fallback key: {key}")
                    fallback_keys.add(key)
                    fallback_placements.append(
                        {
                            "chunk": key[0],
                            "interior_instance_id": instance_id,
                            "interior_space_asset_id": template_id,
                            "node_index": key[1],
                            "reason": reason,
                            "room_id": room_id,
                            "source_component_path": component_path,
                            "source_node_id": key[3],
                            "source_object_id": key[2],
                        }
                    )
            continue
        pack = pack_by_template.get(template_id)
        component_by_path = component_by_template_path.get(template_id)
        if pack is None or component_by_path is None:
            raise ValueError(f"{instance_id}: eligible instance has no replacement pack")
        bound_paths: set[str] = set()
        for binding in instance.get("room_visual_bindings", []):
            missing = binding.get("missing_visual_component_paths", [])
            if missing:
                raise ValueError(f"{instance_id}: eligible instance has missing visuals")
            room_id = str(binding.get("room_id", ""))
            for component_path_value in binding.get("available_visual_component_paths", []):
                component_path = str(component_path_value)
                component = component_by_path.get(component_path)
                if component is None or component["room_id"] != room_id:
                    raise ValueError(f"{instance_id}: visual binding has no exact replacement")
                key = (
                    str(instance.get("chunk", "")),
                    int(instance.get("node_index", -1)),
                    str(instance.get("authoritative_source_object_id", "")),
                    str(instance.get("authoritative_source_node_id", "")),
                    component_path,
                )
                _validate_placement_key(instance_id, key)
                if key in exclusion_keys:
                    raise ValueError(f"duplicate Cesium exclusion key: {key}")
                exclusion_keys.add(key)
                bound_paths.add(component_path)
                exclusions.append(
                    {
                        "asset_id": component["asset_id"],
                        "chunk": key[0],
                        "interior_instance_id": instance_id,
                        "interior_space_asset_id": template_id,
                        "mesh_path": component["mesh_path"],
                        "node_index": key[1],
                        "pack_component_index": component["pack_component_index"],
                        "room_id": room_id,
                        "room_pack_id": pack["room_pack_id"],
                        "source_component_path": component_path,
                        "source_node_id": key[3],
                        "source_object_id": key[2],
                    }
                )
        if bound_paths != set(component_by_path):
            raise ValueError(f"{instance_id}: replacement coverage is not one-to-one")
        eligible_instances.append(
            {
                "chunk": str(instance.get("chunk", "")),
                "interior_instance_id": instance_id,
                "interior_space_asset_id": template_id,
                "node_index": int(instance.get("node_index", -1)),
                "room_pack_id": pack["room_pack_id"],
                "root_transform": instance.get("root_transform"),
                "source_node_id": str(instance.get("authoritative_source_node_id", "")),
                "source_object_id": str(instance.get("authoritative_source_object_id", "")),
            }
        )
    room_packs.sort(key=lambda value: value["room_pack_id"])
    exclusions.sort(
        key=lambda value: (
            value["chunk"],
            value["node_index"],
            value["source_component_path"],
        )
    )
    eligible_instances.sort(key=lambda value: value["interior_instance_id"])
    fallback_instances.sort(key=lambda value: value["interior_instance_id"])
    fallback_placements.sort(
        key=lambda value: (
            value["chunk"],
            value["node_index"],
            value["source_component_path"],
        )
    )
    pack_refs = [
        {
            "content_revision": pack["content_revision"],
            "interior_space_asset_id": pack["interior_space_asset_id"],
            "relative_path": f"interior_room_packs.v1/{pack['room_pack_id']}.json",
            "room_count": len(pack["rooms"]),
            "room_pack_id": pack["room_pack_id"],
            "visual_component_count": sum(
                len(room["visual_components"]) for room in pack["rooms"]
            ),
        }
        for pack in room_packs
    ]
    eligible_index = {
        record["interior_instance_id"]: index
        for index, record in enumerate(eligible_instances)
    }
    exclusion_format = ["eligible_instance_index", "pack_component_index"]
    fallback_format = [
        "chunk",
        "node_index",
        "source_object_id",
        "source_node_id",
        "source_component_path",
        "interior_instance_id",
        "interior_space_asset_id",
        "room_id",
        "reason",
    ]
    strings = sorted(
        {
            str(record[field])
            for records, fields in ((fallback_placements, fallback_format),)
            for record in records
            for field in fields
            if field != "node_index"
        }
    )
    string_indices = {value: index for index, value in enumerate(strings)}

    def compact_record(record: dict[str, Any], fields: list[str]) -> list[int]:
        return [
            int(record[field])
            if field == "node_index"
            else string_indices[str(record[field])]
            for field in fields
        ]

    identity = {
        "counts": {
            "eligible_instance_count": len(eligible_instances),
            "excluded_placement_count": len(exclusions),
            "fallback_instance_count": len(fallback_instances),
            "fallback_placement_count": len(fallback_placements),
            "room_pack_count": len(room_packs),
        },
        "eligible_instances": eligible_instances,
        "exclusion_record_format": exclusion_format,
        "exclusion_records": [
            [
                eligible_index[record["interior_instance_id"]],
                int(record["pack_component_index"]),
            ]
            for record in exclusions
        ],
        "fallback_instances": fallback_instances,
        "fallback_placement_record_format": fallback_format,
        "fallback_placement_records": [
            compact_record(record, fallback_format) for record in fallback_placements
        ],
        "room_packs": pack_refs,
        "runtime_selection_id": selection_id,
        "source_publication_id": source.get("publication_id"),
        "source_publication_sha256": f"sha256:{source_publication_sha256}",
        "string_table": strings,
    }
    boundary_identity = {
        **identity,
        "schema": BOUNDARY_SCHEMA,
        "version": BOUNDARY_VERSION,
    }
    revision = _canonical_sha256(boundary_identity)
    boundary = {
        **boundary_identity,
        "boundary_id": f"interior_cesium_boundary_{revision[:32]}",
        "content_revision": f"sha256:{revision}",
    }
    return boundary, room_packs


def _validate_placement_key(instance_id: str, key: tuple[Any, ...]) -> None:
    if (
        not str(key[0])
        or int(key[1]) < 0
        or not str(key[2])
        or not str(key[3])
        or not str(key[4])
    ):
        raise ValueError(f"{instance_id}: interior placement identity is incomplete")


def _write_room_packs(boundary_path: Path, room_packs: list[dict[str, Any]]) -> None:
    root = boundary_path.parent / "interior_room_packs.v1"
    root.mkdir(parents=True, exist_ok=True)
    # Packs are immutable and content-addressed. Keep earlier generations so a
    # currently published object artifact can continue to resolve its exact
    # pack set while a new boundary is being built.
    for pack in room_packs:
        _write_json_atomic(root / f"{pack['room_pack_id']}.json", pack)


def _merge_shared_manifest(
    runtime_root: Path,
    entries: dict[str, dict[str, Any]],
    selection_manifest_path: Path,
) -> None:
    path = runtime_root / "assets/manifest.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    assets = previous.get("assets", {}) if isinstance(previous, dict) else {}
    if not isinstance(assets, dict):
        raise ValueError(f"shared runtime manifest assets are invalid: {path}")
    updated = 0
    for mesh_path in sorted(entries):
        entry = entries[mesh_path]
        asset_id = str(entry["asset_id"])
        assets[asset_id] = shared_asset_manifest_entry(mesh_path, entry)
        updated += 1
    manifest = {
        "version": SHARED_ASSET_LIBRARY_VERSION,
        "runtime_pack_version": RUNTIME_PACK_VERSION,
        "native_scene_pack_version": NATIVE_SCENE_PACK_VERSION,
        "generated_by": "scripts/generators/generate_godot_runtime_interior_assets.py",
        "generated_at_unix": int(time.time()),
        "mode": "glb",
        "asset_count": len(assets),
        "updated_asset_count": updated,
        "last_runtime_manifest_path": str(selection_manifest_path),
        "assets": {key: assets[key] for key in sorted(assets)},
    }
    _write_json_atomic(path, manifest)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
