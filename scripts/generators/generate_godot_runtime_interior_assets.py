#!/usr/bin/env python3
"""Generate shared runtime GLBs for authoritative interior-only mesh actors.

Ordinary chunk runtime generation sees placed StaticMeshActors.  Authored SGO
Movers and hidden collision helpers intentionally do not appear in that input,
so this narrow generator materializes their exact source glTFs into the same
content-addressed shared library.  It writes a dedicated selection manifest for
the existing Godot native-scene converter and merges only those selected assets
into the global shared manifest.
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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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


SOURCE_SCHEMA = "vanguard_world_interior_source_publication"
SOURCE_VERSION = 2
SELECTION_SCHEMA = "vanguard_godot_runtime_interior_asset_selection"
SELECTION_VERSION = 1
GENERATOR_POLICY = "authoritative_sgo_movers_and_hidden_collision_helpers_v1"


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
        "runtime_bytes=%d manifest=%s"
        % (
            label,
            result["selection_id"],
            result["mesh_count"],
            result["written_count"],
            result["existing_count"],
            result["source_bytes"],
            result["runtime_bytes"],
            result["manifest_path"],
        )
    )
    return 0


def generate_runtime_assets(
    *,
    source_authority_path: Path,
    output_root: Path,
    runtime_root: Path,
    static_mesh_source_index_path: Path,
    force: bool,
    dry_run: bool,
    selection_only: bool = False,
    reserve_bytes: int,
) -> dict[str, Any]:
    source_authority_path = source_authority_path.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    runtime_root = runtime_root.expanduser().resolve()
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
    selections = collect_mesh_selections(source)
    if not selections:
        raise ValueError("interior source publication has no mover/collision-only meshes")
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
    if not dry_run:
        _write_json_atomic(manifest_path, manifest)
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
    }


def collect_mesh_selections(source: dict[str, Any]) -> list[MeshSelection]:
    by_path: dict[str, dict[str, Any]] = {}

    def add(actor: object, role: str) -> None:
        if not isinstance(actor, dict):
            return
        static_source = actor.get("static_mesh_source")
        if not isinstance(static_source, dict):
            raise ValueError(f"interior {role} actor lacks static_mesh_source")
        package = str(static_source.get("source_package", "")).strip()
        name = str(static_source.get("name", actor.get("static_mesh", ""))).strip()
        if not package or not name:
            raise ValueError(f"interior {role} actor has an empty mesh identity")
        path = f"{package}/{name}.gltf"
        value = by_path.setdefault(
            path.casefold(),
            {"mesh_path": path, "mesh_name": name, "roles": set(), "count": 0},
        )
        if value["mesh_path"] != path or value["mesh_name"] != name:
            raise ValueError(f"case-colliding interior mesh identities: {path}")
        value["roles"].add(role)
        value["count"] += 1

    for template in source["interior_templates"]:
        if not isinstance(template, dict):
            raise ValueError("interior template is invalid")
        for room in template.get("rooms", []):
            for mover in room.get("movers", []):
                add(mover, "room_mover")
            for collision in room.get("collision_only_components", []):
                add(collision, "hidden_collision")
        transition = template.get("transition_actors", {})
        if isinstance(transition, dict):
            for mover in transition.get("movers", []):
                add(mover, "transition_mover")
    return [
        MeshSelection(
            mesh_path=value["mesh_path"],
            mesh_name=value["mesh_name"],
            roles=tuple(sorted(value["roles"])),
            occurrence_count=int(value["count"]),
        )
        for _, value in sorted(by_path.items())
    ]


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
