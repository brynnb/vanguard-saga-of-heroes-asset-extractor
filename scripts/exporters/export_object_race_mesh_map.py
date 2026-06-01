#!/usr/bin/env python3
"""Export legacy object-race static mesh paths from the client decompile.

The emulator database stores OBJECT races with race names and mesh IDs, but the
legacy browser NPC assembly viewer needs exported glTF paths. The original
client has a static object-race table with race name, package name, and mesh
export name; this script extracts that table from the checked-in Ghidra
decompile and joins it to the local static mesh manifest.
"""

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_GHIDRA_PATH = ROOT_DIR / "ghidra" / "audio_dispatch_round4.json"
DEFAULT_STATIC_MESH_MANIFEST_PATH = (
    ROOT_DIR / "output" / "meshes" / "buildings" / "manifest.json"
)
DEFAULT_OUTPUT_PATH = ROOT_DIR / "output" / "data" / "object_race_mesh_map.json"

_OBJECT_CALL_RE = re.compile(
    r'FUN_[0-9a-fA-F]+\(L"([^"]+)"\s*(?:,\s*(?:0x[0-9a-fA-F]+|\d+))?\);'
)
_PACKAGE_ASSIGN_RE = re.compile(r'\+\s*0x30\)\s*=\s*L"([^"]+)"\s*;', re.S)
_MESH_ASSIGN_RE = re.compile(r'\+\s*0x20\)\s*=\s*L"([^"]+)"\s*;', re.S)
_TABLE_OFFSET_RE = re.compile(r"param_1\s*\+\s*0x([0-9a-fA-F]+)")


def _load_static_mesh_paths(manifest_path: Path) -> list[str]:
    with open(manifest_path, encoding="utf-8") as handle:
        raw = json.load(handle)
    mesh_paths = raw.get("meshes", []) if isinstance(raw, dict) else raw
    return [
        path
        for path in mesh_paths
        if isinstance(path, str) and path.lower().endswith(".gltf")
    ]


def _clean_package_name(value: str) -> str:
    package = str(value).strip().rstrip(".")
    if "." in package:
        package = package.split(".")[-1]
    return package


def _mesh_name_candidates(value: str) -> list[str]:
    mesh_name = str(value).strip().rstrip(".")
    candidates = [mesh_name]
    if "." in mesh_name:
        candidates.append(mesh_name.split(".")[-1])
    return candidates


def _package_candidates(package_name: str) -> list[str]:
    package = _clean_package_name(package_name)
    candidates = [package]
    lower = package.lower()
    if lower.endswith("_prefab"):
        candidates.append(package[:-7] + "_meshes")
        candidates.append(package[:-7] + "_mesh")
    return candidates


def _build_static_lookup(mesh_paths: list[str]) -> tuple[dict[tuple[str, str], str], dict[str, list[str]]]:
    by_package_and_export: dict[tuple[str, str], str] = {}
    by_export: dict[str, list[str]] = {}
    for mesh_path in mesh_paths:
        path = Path(mesh_path)
        package_key = path.parent.as_posix().lower()
        export_key = path.stem.lower()
        by_package_and_export[(package_key, export_key)] = mesh_path
        by_export.setdefault(export_key, []).append(mesh_path)
    return by_package_and_export, by_export


def _resolve_static_path(
    package_name: str,
    mesh_name: str,
    by_package_and_export: dict[tuple[str, str], str],
    by_export: dict[str, list[str]],
) -> str:
    for mesh_candidate in _mesh_name_candidates(mesh_name):
        for package_candidate in _package_candidates(package_name):
            package_key = package_candidate.lower()
            for export_candidate in (
                mesh_candidate,
                mesh_candidate + "_L0",
                mesh_candidate + "_l0",
            ):
                resolved = by_package_and_export.get(
                    (package_key, export_candidate.lower())
                )
                if resolved:
                    return resolved

    fallback_paths: list[str] = []
    for mesh_candidate in _mesh_name_candidates(mesh_name):
        for export_candidate in (
            mesh_candidate.lower(),
            (mesh_candidate + "_L0").lower(),
            (mesh_candidate + "_l0").lower(),
        ):
            fallback_paths.extend(by_export.get(export_candidate, []))
    unique_paths = sorted(set(fallback_paths))
    return unique_paths[0] if len(unique_paths) == 1 else ""


def _extract_object_entries(ghidra_path: Path) -> list[dict[str, object]]:
    with open(ghidra_path, encoding="utf-8") as handle:
        ghidra_data = json.load(handle)

    functions = ghidra_data.get("functions", {}) if isinstance(ghidra_data, dict) else {}
    entries: list[dict[str, object]] = []
    for function in functions.values():
        if not isinstance(function, dict):
            continue
        code = str(function.get("code", ""))
        calls = list(_OBJECT_CALL_RE.finditer(code))
        for index, call in enumerate(calls):
            segment_end = calls[index + 1].start() if index + 1 < len(calls) else len(code)
            segment = code[call.end() : segment_end]
            package_match = _PACKAGE_ASSIGN_RE.search(segment)
            mesh_match = _MESH_ASSIGN_RE.search(segment)
            offsets = [
                int(match, 16)
                for match in _TABLE_OFFSET_RE.findall(segment)
            ]
            if not package_match or not mesh_match or not offsets:
                continue
            table_offset = Counter(offsets).most_common(1)[0][0]
            entries.append(
                {
                    "race_name": call.group(1),
                    "table_offset": table_offset,
                    "source_package": package_match.group(1),
                    "source_export": mesh_match.group(1),
                    "decompile_function": function.get("name", ""),
                }
            )

    if entries:
        base_offset = min(int(entry["table_offset"]) for entry in entries)
        for entry in entries:
            entry["client_table_index"] = (
                (int(entry["table_offset"]) - base_offset) // 4
            ) + 1
    return sorted(entries, key=lambda entry: (int(entry["table_offset"]), str(entry["race_name"])))


def build_object_race_mesh_map(
    ghidra_path: Path = DEFAULT_GHIDRA_PATH,
    static_mesh_manifest_path: Path = DEFAULT_STATIC_MESH_MANIFEST_PATH,
) -> dict[str, object]:
    mesh_paths = _load_static_mesh_paths(static_mesh_manifest_path)
    by_package_and_export, by_export = _build_static_lookup(mesh_paths)
    entries = _extract_object_entries(ghidra_path)

    races: dict[str, dict[str, object]] = {}
    matched = 0
    for entry in entries:
        path = _resolve_static_path(
            str(entry["source_package"]),
            str(entry["source_export"]),
            by_package_and_export,
            by_export,
        )
        race_entry = dict(entry)
        race_entry["path"] = path
        race_entry["matched"] = bool(path)
        if path:
            matched += 1
        races[str(entry["race_name"])] = race_entry

    return {
        "source": os.path.relpath(ghidra_path, ROOT_DIR),
        "static_mesh_manifest": os.path.relpath(static_mesh_manifest_path, ROOT_DIR),
        "summary": {
            "race_count": len(races),
            "matched_count": matched,
            "unmatched_count": len(races) - matched,
        },
        "races": races,
    }


def write_object_race_mesh_map(
    output_path: Path = DEFAULT_OUTPUT_PATH,
    ghidra_path: Path = DEFAULT_GHIDRA_PATH,
    static_mesh_manifest_path: Path = DEFAULT_STATIC_MESH_MANIFEST_PATH,
) -> dict[str, object]:
    payload = build_object_race_mesh_map(ghidra_path, static_mesh_manifest_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(tmp_path, output_path)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Export object-race mesh map")
    parser.add_argument("--ghidra", type=Path, default=DEFAULT_GHIDRA_PATH)
    parser.add_argument(
        "--static-mesh-manifest",
        type=Path,
        default=DEFAULT_STATIC_MESH_MANIFEST_PATH,
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    payload = write_object_race_mesh_map(
        output_path=args.out,
        ghidra_path=args.ghidra,
        static_mesh_manifest_path=args.static_mesh_manifest,
    )
    summary = payload["summary"]
    print(
        "Wrote object race mesh map: "
        f"{summary['matched_count']}/{summary['race_count']} matched "
        f"({args.out})"
    )


if __name__ == "__main__":
    main()
