#!/usr/bin/env python3
"""Build a per-chunk Godot runtime mesh pack.

The source extraction output stays authoritative. This script creates a smaller
Godot-facing cache for one chunk at a time under output/godot_runtime/ so the
viewer can prefer prepacked GLB meshes while keeping the raw glTF fallback.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import shutil
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402

DEFAULT_OUTPUT_ROOT = REPO_ROOT / "output"
DEFAULT_RUNTIME_ROOT = DEFAULT_OUTPUT_ROOT / "godot_runtime"
DEFAULT_CLIENT_TABLE_ROOT = Path(config.VANGUARD_EMU_ROOT) / "bin"
DEFAULT_STATIC_MESH_TAB = DEFAULT_CLIENT_TABLE_ROOT / "staticmeshMD.tab"
DEFAULT_STATIC_MESH_SOURCE_INDEX = DEFAULT_OUTPUT_ROOT / "data" / "staticmesh_source_index.tsv"
GLB_MAGIC = 0x46546C67
GLB_JSON_CHUNK = 0x4E4F534A
GLB_BIN_CHUNK = 0x004E4942
RUNTIME_PACK_VERSION = 2
NATIVE_SCENE_PACK_VERSION = 2


@dataclass
class MeshRef:
    path: str
    name: str = ""
    reason: str = "placement"
    count: int = 0


class StaticMeshMetadata:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.by_name: dict[str, dict[str, Any]] = {}
        self.by_package_name: dict[str, dict[str, Any]] = {}
        self.by_package_index: dict[str, dict[str, Any]] = {}
        if path.exists():
            self._load(path)

    def _load(self, path: Path) -> None:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            header_seen = False
            for line in handle:
                line = line.rstrip("\n")
                if not line:
                    continue
                if not header_seen:
                    header_seen = True
                    continue
                columns = line.split("\t")
                if len(columns) < 16:
                    continue
                mesh_name = columns[0].strip()
                if not mesh_name:
                    continue
                entry = {
                    "name": mesh_name,
                    "package_name": columns[1].strip(),
                    "package": int(_to_float(columns[2])),
                    "index": int(_to_float(columns[3])),
                    "flags": int(_to_float(columns[4])),
                    "bounds_min": [
                        _to_float(columns[5]),
                        _to_float(columns[6]),
                        _to_float(columns[7]),
                    ],
                    "bounds_max": [
                        _to_float(columns[8]),
                        _to_float(columns[9]),
                        _to_float(columns[10]),
                    ],
                    "sphere_radius": _to_float(columns[11]),
                    "impostor": int(_to_float(columns[12])),
                    "impostor_distance": _to_float(columns[13]),
                    "cull_distance": _to_float(columns[14]),
                    "mesh_detail_level": int(_to_float(columns[15])),
                }
                self.by_name[self._mesh_key(mesh_name)] = entry
                self.by_package_name[
                    self._package_name_key(str(entry["package_name"]), mesh_name)
                ] = entry
                self.by_package_index[
                    self._package_index_key(str(entry["package_name"]), int(entry["index"]))
                ] = entry

    def lookup(self, mesh_path: str, mesh_name: str = "") -> dict[str, Any]:
        path = Path(mesh_path)
        package_name = path.parent.name
        path_name = path.stem
        if package_name and path_name:
            entry = self.by_package_name.get(self._package_name_key(package_name, path_name))
            if entry:
                return entry
        for candidate in [mesh_name, path_name]:
            key = self._mesh_key(candidate)
            if key and key in self.by_name:
                return self.by_name[key]
        return {}

    def lod_levels(self, mesh_path: str, mesh_name: str = "") -> list[dict[str, Any]]:
        entry = self.lookup(mesh_path, mesh_name)
        if not entry:
            return []
        levels: list[dict[str, Any]] = []
        seen: set[str] = set()
        current = entry
        begin_distance = 0.0
        while current:
            package_name = str(current.get("package_name", ""))
            current_name = str(current.get("name", ""))
            current_index = int(current.get("index", -1))
            seen_key = f"{package_name.lower()}:{current_index}:{current_name.lower()}"
            if seen_key in seen:
                break
            seen.add(seen_key)

            impostor_index = int(current.get("impostor", -1))
            target = self.by_package_index.get(
                self._package_index_key(package_name, impostor_index), {}
            )
            switch_distance = float(current.get("impostor_distance", 0.0))
            end_distance = switch_distance if target and switch_distance > begin_distance else 0.0
            level = dict(current)
            level["mesh_path"] = self.mesh_path_for_entry(current)
            level["begin_distance"] = begin_distance
            level["end_distance"] = end_distance
            levels.append(level)
            if not target or end_distance <= 0.0:
                break
            begin_distance = end_distance
            current = target
        return levels

    @staticmethod
    def mesh_path_for_entry(entry: dict[str, Any]) -> str:
        package_name = str(entry.get("package_name", "")).strip()
        mesh_name = str(entry.get("name", "")).strip()
        if not package_name or not mesh_name:
            return ""
        return f"{package_name}/{mesh_name}.gltf"

    @staticmethod
    def _mesh_key(value: str) -> str:
        return Path(str(value).strip()).stem.lower()

    @staticmethod
    def _package_name_key(package_name: str, mesh_name: str) -> str:
        return f"{package_name.strip().lower()}/{StaticMeshMetadata._mesh_key(mesh_name)}"

    @staticmethod
    def _package_index_key(package_name: str, index: int) -> str:
        return f"{package_name.strip().lower()}:{index}"


class StaticMeshSourceIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.by_package_name: dict[str, list[dict[str, str]]] = {}
        self.record_count = 0
        if path.exists():
            self._load(path)

    @property
    def available(self) -> bool:
        return bool(self.by_package_name)

    def _load(self, path: Path) -> None:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            header: list[str] = []
            for line in handle:
                line = line.rstrip("\n")
                if not line:
                    continue
                if not header:
                    header = line.split("\t")
                    continue
                columns = line.split("\t")
                if len(columns) < len(header):
                    columns.extend([""] * (len(header) - len(columns)))
                entry = {name: columns[index] for index, name in enumerate(header)}
                key = str(entry.get("package_object_key", "")).strip().lower()
                if not key:
                    key = self._package_name_key(
                        str(entry.get("package", "")), str(entry.get("object_name", ""))
                    )
                if not key:
                    continue
                self.by_package_name.setdefault(key, []).append(entry)
                self.record_count += 1

    def manifest_fields(self, mesh_path: str, mesh_name: str = "") -> dict[str, Any]:
        records = self.lookup_all(mesh_path, mesh_name)
        if not records:
            return {}
        record = records[0]
        return {
            "staticmesh_async_asset_ref": str(record.get("asset_ref", "")),
            "staticmesh_async_package": str(record.get("package", "")),
            "staticmesh_async_object_name": str(record.get("object_name", "")),
            "staticmesh_async_package_object_key": str(record.get("package_object_key", "")),
            "staticmesh_async_record_index": _to_int(record.get("record_index", ""), -1),
            "staticmesh_async_record_offset": _to_int(record.get("record_offset", ""), -1),
            "staticmesh_async_record_type": _to_int(record.get("record_type", ""), -1),
            "staticmesh_async_block_flag": _to_int(record.get("block_flag", ""), -1),
            "staticmesh_async_serial_offset": _to_int(record.get("serial_offset", ""), -1),
            "staticmesh_async_serial_offset_duplicate": _to_int(
                record.get("serial_offset_duplicate", ""), -1
            ),
            "staticmesh_async_serial_size": _to_int(record.get("serial_size", ""), -1),
            "staticmesh_async_duplicate_count": len(records),
        }

    def lookup_all(self, mesh_path: str, mesh_name: str = "") -> list[dict[str, str]]:
        path = Path(mesh_path)
        package_name = path.parent.name
        candidates = [path.stem, mesh_name]
        for candidate in candidates:
            key = self._package_name_key(package_name, candidate)
            if key in self.by_package_name:
                return self.by_package_name[key]
        return []

    @staticmethod
    def _package_name_key(package_name: str, mesh_name: str) -> str:
        package = package_name.strip().lower()
        name = Path(str(mesh_name).strip()).stem.lower()
        if not package or not name:
            return ""
        return f"{package}/{name}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", action="append", required=True, help="Chunk name, e.g. chunk_n25_26.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument(
        "--mode",
        choices=["glb", "hardlink", "copy"],
        default="glb",
        help="How to materialize referenced meshes. glb is the runtime-oriented default.",
    )
    parser.add_argument("--static-mesh-tab", type=Path, default=DEFAULT_STATIC_MESH_TAB)
    parser.add_argument(
        "--static-mesh-source-index",
        type=Path,
        default=DEFAULT_STATIC_MESH_SOURCE_INDEX,
        help="Generated StaticMeshAsync source index TSV.",
    )
    parser.add_argument("--no-lods", action="store_true", help="Do not include static mesh LOD targets.")
    parser.add_argument(
        "--neighbor-index-only",
        action="store_true",
        help="Only pack meshes referenced by the chunk's neighbor_objects.json index.",
    )
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden SGO components.")
    parser.add_argument("--dry-run", action="store_true", help="Print estimate without writing files.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing runtime mesh files.")
    parser.add_argument("--limit-meshes", type=int, default=0, help="Debug/smoke limit.")
    parser.add_argument(
        "--free-space-reserve-gb",
        type=float,
        default=10.0,
        help="Abort writes if runtime root would leave less than this much free disk.",
    )
    args = parser.parse_args()

    output_root = args.output_root.resolve()
    runtime_root = args.runtime_root.resolve()
    metadata = StaticMeshMetadata(args.static_mesh_tab)
    source_index = StaticMeshSourceIndex(args.static_mesh_source_index)
    chunks = [normalize_chunk_name(chunk) for chunk in args.chunk]

    failures = 0
    for chunk in chunks:
        try:
            result = build_runtime_chunk(
                chunk=chunk,
                output_root=output_root,
                runtime_root=runtime_root,
                mode=args.mode,
                metadata=metadata,
                source_index=source_index,
                include_lods=not args.no_lods,
                neighbor_index_only=args.neighbor_index_only,
                include_hidden=args.include_hidden,
                dry_run=args.dry_run,
                force=args.force,
                limit_meshes=args.limit_meshes,
                reserve_bytes=int(args.free_space_reserve_gb * 1024**3),
            )
            print_runtime_summary(result, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - command-line tool should report all chunk failures.
            failures += 1
            print(f"ERROR: {chunk}: {exc}", file=sys.stderr)
    return 1 if failures else 0


def build_runtime_chunk(
    *,
    chunk: str,
    output_root: Path,
    runtime_root: Path,
    mode: str,
    metadata: StaticMeshMetadata,
    source_index: StaticMeshSourceIndex,
    include_lods: bool,
    neighbor_index_only: bool,
    include_hidden: bool,
    dry_run: bool,
    force: bool,
    limit_meshes: int,
    reserve_bytes: int,
) -> dict[str, Any]:
    terrain_root = output_root / "terrain/terrain_grid"
    mesh_root = output_root / "meshes/buildings"
    objects_path = terrain_root / f"{chunk}_objects.gltf"
    sgo_path = terrain_root / f"{chunk}_sgo.json"
    if not objects_path.exists():
        raise FileNotFoundError(f"missing object placement file: {objects_path}")
    if not sgo_path.exists():
        raise FileNotFoundError(f"missing SGO manifest file: {sgo_path}")

    objects_data = read_json(objects_path)
    sgo_manifest = read_json(sgo_path)
    if neighbor_index_only:
        refs = collect_neighbor_index_refs(
            chunk,
            runtime_root,
            metadata,
            include_lods=include_lods,
        )
    else:
        refs = collect_mesh_refs(
            chunk,
            objects_data,
            sgo_manifest,
            metadata,
            include_lods=include_lods,
            include_hidden=include_hidden,
        )
    if limit_meshes > 0:
        refs = dict(sorted(refs.items())[:limit_meshes])

    chunk_root = runtime_root / "chunks" / chunk
    mesh_output_root = chunk_root / "meshes"
    manifest_path = chunk_root / "manifest.json"
    previous_manifest = read_json_if_exists(manifest_path)
    previous_meshes_value = (
        previous_manifest.get("meshes", {}) if isinstance(previous_manifest, dict) else {}
    )
    previous_meshes = previous_meshes_value if isinstance(previous_meshes_value, dict) else {}

    mesh_entries: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    invalid_triangle_meshes: list[str] = []
    source_bytes = 0
    estimated_write_bytes = 0
    existing_runtime_bytes = 0
    stale_runtime_count = 0
    stale_runtime_bytes = 0
    stale_native_scene_count = 0
    source_index_hit_count = 0
    source_index_miss_count = 0
    for mesh_path, ref in sorted(refs.items()):
        safe_path = safe_relative_path(mesh_path)
        source_path = mesh_root / safe_path
        if not source_path.exists():
            missing.append(mesh_path)
            continue
        source_info = mesh_source_signature(source_path)
        source_index_fields = source_index.manifest_fields(mesh_path, ref.name)
        if source_index_fields:
            source_index_hit_count += 1
        elif source_index.available:
            source_index_miss_count += 1
        source_size = int(source_info["source_bytes"])
        source_bytes += source_size
        valid_triangle_indices = has_valid_triangle_indices(source_path)
        entry = {
            "runtime_pack_version": RUNTIME_PACK_VERSION,
            "source_relative_path": str(Path("meshes/buildings") / safe_path),
            "mode": mode,
            "mesh_name": ref.name,
            "reason": ref.reason,
            "reference_count": ref.count,
            "valid_triangle_indices": valid_triangle_indices,
            **source_info,
            **source_index_fields,
        }
        if not valid_triangle_indices:
            invalid_triangle_meshes.append(mesh_path)
            entry["status"] = "skipped_invalid_triangles"
            mesh_entries[mesh_path] = entry
            continue
        runtime_relative = Path("chunks") / chunk / "meshes" / runtime_mesh_relative_path(safe_path, mode)
        runtime_path = runtime_root / runtime_relative
        native_scene_relative = native_scene_relative_path(runtime_relative)
        native_scene_path = runtime_root / native_scene_relative
        entry["runtime_relative_path"] = runtime_relative.as_posix()
        previous_entry_value = previous_meshes.get(mesh_path, {})
        previous_entry = previous_entry_value if isinstance(previous_entry_value, dict) else {}
        runtime_fresh = runtime_entry_is_fresh(
            previous_entry,
            entry,
            runtime_path,
            mode,
        )
        write_required = force or not runtime_fresh
        entry["_write_required"] = write_required
        if runtime_path.exists() and not write_required:
            existing_runtime_bytes += runtime_path.stat().st_size
            entry["status"] = "existing"
        elif runtime_path.exists() and not force:
            runtime_bytes = runtime_path.stat().st_size
            stale_runtime_count += 1
            stale_runtime_bytes += runtime_bytes
            estimated_write_bytes += estimate_runtime_size(source_size, mode)
            entry["status"] = "stale_runtime"
            entry["stale_runtime_bytes"] = runtime_bytes
        else:
            estimated_write_bytes += estimate_runtime_size(source_size, mode)
        if native_scene_path.exists() and not force:
            if native_scene_entry_is_fresh(previous_entry, entry, native_scene_path):
                entry["native_scene_relative_path"] = native_scene_relative.as_posix()
                entry["native_scene_bytes"] = native_scene_path.stat().st_size
                entry["native_scene_status"] = "existing"
                entry["native_scene_pack_version"] = int(
                    previous_entry.get("native_scene_pack_version", NATIVE_SCENE_PACK_VERSION)
                )
                entry["native_scene_runtime_pack_version"] = int(
                    previous_entry.get("native_scene_runtime_pack_version", RUNTIME_PACK_VERSION)
                )
                entry["native_scene_source_signature"] = str(
                    previous_entry.get("native_scene_source_signature", entry["source_signature"])
                )
                entry["native_scene_runtime_signature"] = str(
                    previous_entry.get("native_scene_runtime_signature", "")
                )
            else:
                stale_native_scene_count += 1
                entry["native_scene_status"] = "stale"
                entry["stale_native_scene_bytes"] = native_scene_path.stat().st_size
        elif native_scene_path.exists() and force:
            entry["native_scene_relative_path"] = native_scene_relative.as_posix()
            entry["native_scene_bytes"] = native_scene_path.stat().st_size
            entry["native_scene_status"] = "stale_force"
        mesh_entries[mesh_path] = entry

    if not dry_run:
        assert_free_space(runtime_root, estimated_write_bytes, reserve_bytes)
        mesh_output_root.mkdir(parents=True, exist_ok=True)
        for mesh_path, entry in mesh_entries.items():
            if not entry.get("valid_triangle_indices", True):
                continue
            source_path = output_root / entry["source_relative_path"]
            runtime_path = runtime_root / entry["runtime_relative_path"]
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            write_required = bool(entry.pop("_write_required", True))
            if runtime_path.exists() and not write_required:
                entry["runtime_bytes"] = runtime_path.stat().st_size
                entry["status"] = "existing"
                continue
            materialize_mesh(source_path, runtime_path, mode)
            entry["runtime_bytes"] = runtime_path.stat().st_size
            entry["status"] = "written"
            for stale_key in ("native_scene_relative_path", "native_scene_bytes"):
                entry.pop(stale_key, None)
            if entry.get("native_scene_status") in {"stale", "stale_force"}:
                entry.pop("native_scene_status", None)

        manifest = {
            "version": RUNTIME_PACK_VERSION,
            "generated_by": "scripts/generators/generate_godot_runtime_chunk.py",
            "generated_at_unix": int(time.time()),
            "chunk": chunk,
            "mode": mode,
            "neighbor_index_only": neighbor_index_only,
            "source_output_root": str(output_root),
            "runtime_root": str(runtime_root),
            "objects_source_relative_path": str(objects_path.relative_to(output_root)),
            "sgo_source_relative_path": str(sgo_path.relative_to(output_root)),
            "mesh_count": len(mesh_entries),
            "missing_mesh_count": len(missing),
            "missing_meshes": missing,
            "invalid_triangle_mesh_count": len(invalid_triangle_meshes),
            "invalid_triangle_meshes": invalid_triangle_meshes,
            "native_scene_count": count_native_scenes(mesh_entries),
            "native_scene_bytes": sum(
                int(entry.get("native_scene_bytes", 0)) for entry in mesh_entries.values()
            ),
            "native_scene_pack_version": NATIVE_SCENE_PACK_VERSION,
            "staticmesh_source_index_path": str(source_index.path),
            "staticmesh_source_index_record_count": source_index.record_count,
            "staticmesh_source_index_hit_count": source_index_hit_count,
            "staticmesh_source_index_miss_count": source_index_miss_count,
            "source_bytes": source_bytes,
            "runtime_bytes": sum(int(entry.get("runtime_bytes", 0)) for entry in mesh_entries.values()),
            "stale_runtime_count": stale_runtime_count,
            "stale_runtime_bytes": stale_runtime_bytes,
            "stale_native_scene_count": stale_native_scene_count,
            "meshes": mesh_entries,
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return {
        "chunk": chunk,
        "mode": mode,
        "mesh_count": len(mesh_entries),
        "missing_mesh_count": len(missing),
        "invalid_triangle_mesh_count": len(invalid_triangle_meshes),
        "native_scene_count": count_native_scenes(mesh_entries),
        "source_bytes": source_bytes,
        "estimated_write_bytes": estimated_write_bytes,
        "existing_runtime_bytes": existing_runtime_bytes,
        "stale_runtime_count": stale_runtime_count,
        "stale_runtime_bytes": stale_runtime_bytes,
        "stale_native_scene_count": stale_native_scene_count,
        "source_index_hit_count": source_index_hit_count,
        "source_index_miss_count": source_index_miss_count,
        "manifest_path": manifest_path,
        "runtime_root": runtime_root,
    }


def collect_mesh_refs(
    chunk: str,
    objects_data: dict[str, Any],
    sgo_manifest: dict[str, Any],
    metadata: StaticMeshMetadata,
    *,
    include_lods: bool,
    include_hidden: bool,
) -> dict[str, MeshRef]:
    refs: dict[str, MeshRef] = {}

    def add_ref(path: str, name: str = "", reason: str = "placement") -> None:
        path = path.strip()
        if not path:
            return
        ref = refs.get(path)
        if ref is None:
            refs[path] = MeshRef(path=path, name=name.strip(), reason=reason, count=1)
            return
        ref.count += 1
        if not ref.name and name.strip():
            ref.name = name.strip()
        if ref.reason != reason:
            ref.reason = "placement+lod"

    nodes = objects_data.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError(f"{chunk} object placement nodes are not a list")
    for node in nodes[1:]:
        if not isinstance(node, dict):
            continue
        extras = node.get("extras", {})
        if not isinstance(extras, dict):
            continue
        prefab_name = str(extras.get("prefab_name", "")).strip()
        prefab = sgo_manifest.get(prefab_name, {}) if prefab_name else {}
        components = prefab.get("components", []) if isinstance(prefab, dict) else []
        if isinstance(components, list) and components:
            for component in components:
                if not isinstance(component, dict):
                    continue
                if not include_hidden and is_hidden_sgo_component(component):
                    continue
                add_ref(
                    str(component.get("mesh_path", "")),
                    str(component.get("mesh_name", "")),
                    "placement",
                )
        else:
            add_ref(str(extras.get("mesh_path", "")), str(extras.get("mesh_ref", "")), "placement")

    if include_lods and metadata.by_name:
        for ref in list(refs.values()):
            for level in metadata.lod_levels(ref.path, ref.name):
                add_ref(str(level.get("mesh_path", "")), str(level.get("name", "")), "lod")
    return refs


def collect_neighbor_index_refs(
    chunk: str,
    runtime_root: Path,
    metadata: StaticMeshMetadata,
    *,
    include_lods: bool,
) -> dict[str, MeshRef]:
    index_path = runtime_root / "chunks" / chunk / "neighbor_objects.json"
    if not index_path.exists():
        raise FileNotFoundError(f"missing neighbor object index: {index_path}")
    index_data = read_json(index_path)
    mesh_values = index_data.get("candidate_meshes", [])
    if not isinstance(mesh_values, list):
        raise ValueError(f"neighbor object index has no candidate_meshes list: {index_path}")

    refs: dict[str, MeshRef] = {}

    def add_ref(path: str, name: str = "", reason: str = "neighbor") -> None:
        path = path.strip()
        if not path:
            return
        ref = refs.get(path)
        if ref is None:
            refs[path] = MeshRef(path=path, name=name.strip(), reason=reason, count=1)
            return
        ref.count += 1
        if not ref.name and name.strip():
            ref.name = name.strip()
        if ref.reason != reason:
            ref.reason = "neighbor+lod"

    for mesh_path_value in mesh_values:
        mesh_path = str(mesh_path_value).strip()
        add_ref(mesh_path, Path(mesh_path).stem, "neighbor")

    if include_lods and metadata.by_name:
        for ref in list(refs.values()):
            for level in metadata.lod_levels(ref.path, ref.name):
                add_ref(str(level.get("mesh_path", "")), str(level.get("name", "")), "lod")
    return refs


def mesh_source_signature(source_path: Path) -> dict[str, Any]:
    stat = source_path.stat()
    return {
        "source_bytes": stat.st_size,
        "source_mtime_unix": int(stat.st_mtime),
        "source_mtime_ns": stat.st_mtime_ns,
        "source_signature": f"{stat.st_size}:{stat.st_mtime_ns}",
    }


def runtime_entry_is_fresh(
    previous_entry: dict[str, Any],
    current_entry: dict[str, Any],
    runtime_path: Path,
    mode: str,
) -> bool:
    if not runtime_path.exists():
        return False
    if int(previous_entry.get("runtime_pack_version", 0)) != RUNTIME_PACK_VERSION:
        return False
    if str(previous_entry.get("mode", "")) != mode:
        return False
    if str(previous_entry.get("runtime_relative_path", "")) != str(
        current_entry.get("runtime_relative_path", "")
    ):
        return False
    return str(previous_entry.get("source_signature", "")) == str(
        current_entry.get("source_signature", "")
    )


def native_scene_entry_is_fresh(
    previous_entry: dict[str, Any],
    current_entry: dict[str, Any],
    native_scene_path: Path,
) -> bool:
    if not native_scene_path.exists():
        return False
    if int(previous_entry.get("native_scene_pack_version", 0)) != NATIVE_SCENE_PACK_VERSION:
        return False
    if int(previous_entry.get("native_scene_runtime_pack_version", 0)) != RUNTIME_PACK_VERSION:
        return False
    return str(previous_entry.get("native_scene_source_signature", "")) == str(
        current_entry.get("source_signature", "")
    )


def materialize_mesh(source_path: Path, runtime_path: Path, mode: str) -> None:
    if runtime_path.exists():
        runtime_path.unlink()
    if mode == "glb":
        convert_gltf_to_glb(source_path, runtime_path)
        return
    if mode == "hardlink":
        try:
            os.link(source_path, runtime_path)
            return
        except OSError:
            shutil.copy2(source_path, runtime_path)
            return
    if mode == "copy":
        shutil.copy2(source_path, runtime_path)
        return
    raise ValueError(f"unsupported mode: {mode}")


def convert_gltf_to_glb(source_path: Path, runtime_path: Path) -> None:
    gltf = read_json(source_path)
    if not isinstance(gltf, dict):
        raise ValueError(f"not a glTF JSON object: {source_path}")

    binary = bytearray()
    buffer_offsets: dict[int, int] = {}
    for index, buffer_info in enumerate(gltf.get("buffers", [])):
        if not isinstance(buffer_info, dict):
            continue
        payload = read_gltf_uri_bytes(buffer_info, source_path)
        offset = append_aligned(binary, payload)
        buffer_offsets[index] = offset

    for buffer_view in gltf.get("bufferViews", []):
        if not isinstance(buffer_view, dict):
            continue
        old_buffer = int(buffer_view.get("buffer", 0))
        old_offset = int(buffer_view.get("byteOffset", 0))
        buffer_view["buffer"] = 0
        buffer_view["byteOffset"] = int(buffer_offsets.get(old_buffer, 0)) + old_offset

    images = gltf.get("images", [])
    buffer_views = gltf.setdefault("bufferViews", [])
    if isinstance(images, list) and isinstance(buffer_views, list):
        for image_info in images:
            if not isinstance(image_info, dict) or "uri" not in image_info:
                continue
            payload, mime = read_gltf_image_bytes(image_info, source_path)
            if not payload:
                continue
            offset = append_aligned(binary, payload)
            buffer_view_index = len(buffer_views)
            buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(payload)})
            image_info.pop("uri", None)
            image_info["bufferView"] = buffer_view_index
            if mime:
                image_info["mimeType"] = mime

    gltf["buffers"] = [{"byteLength": len(binary)}]
    json_bytes = json.dumps(gltf, separators=(",", ":"), sort_keys=False).encode("utf-8")
    json_bytes = pad_bytes(json_bytes, b" ")
    bin_bytes = pad_bytes(bytes(binary), b"\0")
    total_length = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
    with runtime_path.open("wb") as handle:
        handle.write(struct.pack("<III", GLB_MAGIC, 2, total_length))
        handle.write(struct.pack("<II", len(json_bytes), GLB_JSON_CHUNK))
        handle.write(json_bytes)
        handle.write(struct.pack("<II", len(bin_bytes), GLB_BIN_CHUNK))
        handle.write(bin_bytes)


def has_valid_triangle_indices(source_path: Path) -> bool:
    if source_path.suffix.lower() != ".gltf":
        return True
    gltf = read_json(source_path)
    if not isinstance(gltf, dict):
        return True
    accessors = gltf.get("accessors", [])
    if not isinstance(accessors, list):
        return True
    meshes = gltf.get("meshes", [])
    if not isinstance(meshes, list):
        return True
    for mesh in meshes:
        if not isinstance(mesh, dict):
            continue
        primitives = mesh.get("primitives", [])
        if not isinstance(primitives, list):
            continue
        for primitive in primitives:
            if not isinstance(primitive, dict):
                continue
            if int(primitive.get("mode", 4)) != 4:
                continue
            index_accessor = int(primitive.get("indices", -1))
            if index_accessor < 0 or index_accessor >= len(accessors):
                continue
            accessor = accessors[index_accessor]
            if not isinstance(accessor, dict):
                continue
            count = int(accessor.get("count", 0))
            if count > 0 and count % 3 != 0:
                return False
    return True


def read_gltf_uri_bytes(info: dict[str, Any], source_path: Path) -> bytes:
    uri = str(info.get("uri", ""))
    if uri.startswith("data:"):
        return decode_data_uri(uri)
    if uri:
        return (source_path.parent / uri).read_bytes()
    return b""


def read_gltf_image_bytes(info: dict[str, Any], source_path: Path) -> tuple[bytes, str]:
    uri = str(info.get("uri", ""))
    if uri.startswith("data:"):
        header, payload = uri.split(",", 1)
        mime = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
        return base64.b64decode(payload), mime
    if uri:
        path = source_path.parent / uri
        mime = str(info.get("mimeType", "")) or mimetypes.guess_type(path.name)[0] or ""
        return path.read_bytes(), mime
    return b"", ""


def append_aligned(binary: bytearray, payload: bytes) -> int:
    while len(binary) % 4 != 0:
        binary.append(0)
    offset = len(binary)
    binary.extend(payload)
    while len(binary) % 4 != 0:
        binary.append(0)
    return offset


def pad_bytes(payload: bytes, pad: bytes) -> bytes:
    padding = (4 - (len(payload) % 4)) % 4
    return payload + pad * padding


def decode_data_uri(uri: str) -> bytes:
    comma = uri.find(",")
    if comma < 0:
        raise ValueError("data URI has no comma separator")
    return base64.b64decode(uri[comma + 1 :])


def runtime_mesh_relative_path(path: Path, mode: str) -> Path:
    return path.with_suffix(".glb") if mode == "glb" else path


def native_scene_relative_path(runtime_relative: Path) -> Path:
    parts = list(runtime_relative.parts)
    try:
        meshes_index = parts.index("meshes")
        parts[meshes_index] = "scenes"
        return Path(*parts).with_suffix(".scn")
    except ValueError:
        return runtime_relative.parent / "scenes" / runtime_relative.with_suffix(".scn").name


def count_native_scenes(mesh_entries: dict[str, dict[str, Any]]) -> int:
    return sum(1 for entry in mesh_entries.values() if entry.get("native_scene_relative_path"))


def estimate_runtime_size(source_size: int, mode: str) -> int:
    if mode == "glb":
        return int(source_size * 0.82)
    if mode == "hardlink":
        return 0
    return source_size


def assert_free_space(runtime_root: Path, bytes_to_write: int, reserve_bytes: int) -> None:
    check_root = runtime_root
    while not check_root.exists() and check_root.parent != check_root:
        check_root = check_root.parent
    usage = shutil.disk_usage(check_root)
    if usage.free - bytes_to_write < reserve_bytes:
        raise RuntimeError(
            "not enough free disk for runtime pack: "
            f"need about {format_bytes(bytes_to_write)} plus "
            f"{format_bytes(reserve_bytes)} reserve, have {format_bytes(usage.free)}"
        )


def print_runtime_summary(result: dict[str, Any], *, dry_run: bool) -> None:
    label = "DRY RUN" if dry_run else "WROTE"
    print(
        f"{label}: {result['chunk']} mode={result['mode']} "
        f"meshes={result['mesh_count']} missing={result['missing_mesh_count']} "
        f"invalid_triangles={result['invalid_triangle_mesh_count']} "
        f"native_scenes={result['native_scene_count']}"
    )
    print(
        "  source mesh bytes: "
        f"{format_bytes(int(result['source_bytes']))}; "
        "estimated new write: "
        f"{format_bytes(int(result['estimated_write_bytes']))}; "
        "existing runtime bytes: "
        f"{format_bytes(int(result['existing_runtime_bytes']))}"
    )
    if result.get("stale_runtime_count") or result.get("stale_native_scene_count"):
        print(
            "  stale cache: "
            f"runtime_meshes={int(result.get('stale_runtime_count', 0))} "
            f"runtime_bytes={format_bytes(int(result.get('stale_runtime_bytes', 0)))} "
            f"native_scenes={int(result.get('stale_native_scene_count', 0))}"
        )
    if result.get("source_index_hit_count") or result.get("source_index_miss_count"):
        print(
            "  StaticMeshAsync source index: "
            f"hits={int(result.get('source_index_hit_count', 0))} "
            f"misses={int(result.get('source_index_miss_count', 0))}"
        )
    print(f"  manifest: {result['manifest_path']}")


def safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe mesh path: {value}")
    return path


def normalize_chunk_name(value: str) -> str:
    name = Path(value.strip()).stem
    if name.endswith("_terrain"):
        name = name[: -len("_terrain")]
    if name.endswith("_objects"):
        name = name[: -len("_objects")]
    if not name.startswith("chunk_"):
        name = "chunk_" + name
    return name


def is_hidden_sgo_component(component: dict[str, Any]) -> bool:
    if truthy(component.get("hidden", False)) or truthy(component.get("hidden_editor", False)):
        return True
    props = component.get("props", {})
    if isinstance(props, dict):
        return truthy(props.get("bHidden", False)) or truthy(props.get("bHiddenEd", False))
    return False


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return {}
    return read_json(path)


def _to_float(value: Any) -> float:
    text = str(value).strip()
    return float(text) if text else 0.0


def _to_int(value: Any, default: int = 0) -> int:
    try:
        text = str(value).strip()
        if not text:
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024.0
    return f"{value} B"


if __name__ == "__main__":
    raise SystemExit(main())
