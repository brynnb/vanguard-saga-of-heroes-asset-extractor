#!/usr/bin/env python3
"""Export actor race visual prefixes from the committed client lookup table.

The emulator database stores race IDs/names, but most NPC character visuals are
selected by a client-side race definition table. That table assigns each race a
UEM character prefix such as ``RiftWalker -> chupacabra``. This script reads a
clean lookup table under ``client_tables/`` and records whether the referenced
prefix is present in the local exported character mesh manifest. Pass
``--ghidra`` only when regenerating the table from decompilation evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CLIENT_TABLE_PATH = ROOT_DIR / "client_tables" / "actor_race_visual_table.json"
DEFAULT_CHARACTER_MANIFEST_PATH = (
    ROOT_DIR / "output" / "meshes" / "characters" / "manifest.json"
)
DEFAULT_OUTPUT_PATH = ROOT_DIR / "output" / "data" / "actor_race_visual_map.json"

ASSETS_DIR = Path(
    os.environ.get(
        "VANGUARD_ASSETS",
        os.environ.get("VANGUARD_ASSETS_PATH", os.path.expanduser("~/Downloads/Vanguard EMU/Assets")),
    )
)
DEFAULT_UEM_DIR = ASSETS_DIR / "Characters" / "Meshes"

_RACE_CALL_RE = re.compile(
    r'FUN_(?:00412280|00413240)\(L"([^"]+)"(?:,\s*([^)]+))?\);'
)
_PREFIX_ASSIGN_RE = re.compile(r'\+\s*0x20\)\s*=\s*L"([^"]+)"\s*;', re.S)
_PREFIX_DAT_ASSIGN_RE = re.compile(
    r'\+\s*0x20\)\s*=\s*&?(DAT_[0-9a-fA-F]+)\s*;', re.S
)
_PACKAGE_ASSIGN_RE = re.compile(r'\+\s*0x30\)\s*=\s*L"([^"]+)"\s*;', re.S)
_ANIM_MALE_RE = re.compile(r'\+\s*0x34\)\s*=\s*L"([^"]+)"\s*;', re.S)
_ANIM_FEMALE_RE = re.compile(r'\+\s*0x38\)\s*=\s*L"([^"]+)"\s*;', re.S)
_SOUND_PACKAGE_RE = re.compile(r'\+\s*0x48\)\s*=\s*L"([^"]+)"\s*;', re.S)
_PREFIX_RE = re.compile(r"^uem_(.+?)_[mf]_(char|clth|tool|hair)$", re.I)

# Ghidra emitted this one visual prefix as a data symbol instead of a string
# literal. The adjacent client entries use Oni visuals, and the local client
# assets contain UEM_Oni_M_char + UEM_Oni_Minor_M_char.
_DATA_SYMBOL_PREFIX_HINTS = {
    "DAT_01193a3c": "Oni",
}


def _load_json(path: Path) -> object:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _display_path(path: Path) -> str:
    try:
        return os.path.relpath(path, ROOT_DIR)
    except ValueError:
        return str(path)


def _normalise_prefix(value: str) -> str:
    return str(value).strip().lower()


def _load_exported_prefixes(manifest_path: Path) -> set[str]:
    if not manifest_path.exists():
        return set()

    raw = _load_json(manifest_path)
    if not isinstance(raw, list):
        return set()

    prefixes: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        package = str(entry.get("package", "")).lower()
        match = _PREFIX_RE.match(package)
        if match:
            prefixes.add(match.group(1).lower())
    return prefixes


def _load_raw_uem_prefixes(uem_dir: Path) -> set[str]:
    prefixes: set[str] = set()
    for path in uem_dir.glob("UEM_*.*"):
        match = _PREFIX_RE.match(path.stem.lower())
        if match:
            prefixes.add(match.group(1).lower())
    return prefixes


def _find_actor_table_function(functions: dict[str, object]) -> dict[str, object]:
    named = functions.get("assign_actor_sound_packages")
    if isinstance(named, dict) and str(named.get("code", "")).count("FUN_00412280") > 50:
        return named

    candidates: list[tuple[int, dict[str, object]]] = []
    for function in functions.values():
        if not isinstance(function, dict):
            continue
        code = str(function.get("code", ""))
        count = len(_RACE_CALL_RE.findall(code))
        if count > 50:
            candidates.append((count, function))

    if not candidates:
        return {}
    return max(candidates, key=lambda item: item[0])[1]


def _extract_actor_entries(ghidra_path: Path) -> list[dict[str, object]]:
    ghidra_data = _load_json(ghidra_path)
    functions = ghidra_data.get("functions", {}) if isinstance(ghidra_data, dict) else {}
    if not isinstance(functions, dict):
        return []

    function = _find_actor_table_function(functions)
    code = str(function.get("code", "")) if function else ""
    calls = list(_RACE_CALL_RE.finditer(code))
    entries: list[dict[str, object]] = []

    for index, call in enumerate(calls):
        segment_end = calls[index + 1].start() if index + 1 < len(calls) else len(code)
        segment = code[call.end() : segment_end]

        # Static object races use +0x30 as a package and are handled by the
        # legacy browser object-race map. This table is for character visuals.
        if _PACKAGE_ASSIGN_RE.search(segment):
            continue

        prefix_match = _PREFIX_ASSIGN_RE.search(segment)
        data_symbol_match = _PREFIX_DAT_ASSIGN_RE.search(segment)
        visual_prefix = prefix_match.group(1) if prefix_match else ""
        data_symbol = data_symbol_match.group(1) if data_symbol_match else ""
        if not visual_prefix and data_symbol:
            visual_prefix = _DATA_SYMBOL_PREFIX_HINTS.get(data_symbol, "")

        male_anim = _ANIM_MALE_RE.search(segment)
        female_anim = _ANIM_FEMALE_RE.search(segment)
        sound_package = _SOUND_PACKAGE_RE.search(segment)
        entries.append(
            {
                "race_name": call.group(1),
                "registration_arg": call.group(2) or "",
                "visual_prefix": visual_prefix,
                "normalized_prefix": _normalise_prefix(visual_prefix) if visual_prefix else "",
                "data_symbol": data_symbol,
                "male_animation": male_anim.group(1) if male_anim else "",
                "female_animation": female_anim.group(1) if female_anim else "",
                "sound_package": sound_package.group(1) if sound_package else "",
                "decompile_function": function.get("name", ""),
            }
        )

    return entries


def _load_actor_entries(client_table_path: Path) -> list[dict[str, object]]:
    payload = _load_json(client_table_path)
    rows = payload.get("rows", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError(f"{client_table_path} does not contain a rows list")

    entries: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = dict(row)
        visual_prefix = str(entry.get("visual_prefix", ""))
        entry["normalized_prefix"] = str(
            entry.get("normalized_prefix") or _normalise_prefix(visual_prefix)
        )
        entries.append(entry)
    return entries


def build_actor_race_visual_map(
    client_table_path: Path = DEFAULT_CLIENT_TABLE_PATH,
    character_manifest_path: Path = DEFAULT_CHARACTER_MANIFEST_PATH,
    uem_dir: Path = DEFAULT_UEM_DIR,
    ghidra_path: Path | None = None,
) -> dict[str, object]:
    exported_prefixes = _load_exported_prefixes(character_manifest_path)
    raw_prefixes = _load_raw_uem_prefixes(uem_dir)
    if ghidra_path is not None:
        entries = _extract_actor_entries(ghidra_path)
        source_path = ghidra_path
    else:
        entries = _load_actor_entries(client_table_path)
        source_path = client_table_path

    races: dict[str, dict[str, object]] = {}
    for entry in entries:
        normalized = str(entry.get("normalized_prefix", ""))
        race_entry = dict(entry)
        race_entry["has_exported_prefix"] = bool(normalized and normalized in exported_prefixes)
        race_entry["has_raw_uem_prefix"] = bool(normalized and normalized in raw_prefixes)
        races[str(entry["race_name"])] = race_entry

    matched = sum(1 for entry in races.values() if entry.get("has_exported_prefix"))
    raw_only = sum(
        1
        for entry in races.values()
        if entry.get("has_raw_uem_prefix") and not entry.get("has_exported_prefix")
    )
    unresolved = sum(1 for entry in races.values() if not entry.get("normalized_prefix"))

    return {
        "source": _display_path(source_path),
        "character_manifest": os.path.relpath(character_manifest_path, ROOT_DIR),
        "uem_dir": str(uem_dir),
        "summary": {
            "race_count": len(races),
            "exported_prefix_count": len(exported_prefixes),
            "raw_uem_prefix_count": len(raw_prefixes),
            "matched_exported_prefix_count": matched,
            "raw_only_prefix_count": raw_only,
            "unresolved_prefix_count": unresolved,
        },
        "races": races,
    }


def write_actor_race_visual_map(
    output_path: Path = DEFAULT_OUTPUT_PATH,
    client_table_path: Path = DEFAULT_CLIENT_TABLE_PATH,
    character_manifest_path: Path = DEFAULT_CHARACTER_MANIFEST_PATH,
    uem_dir: Path = DEFAULT_UEM_DIR,
    ghidra_path: Path | None = None,
) -> dict[str, object]:
    payload = build_actor_race_visual_map(
        client_table_path=client_table_path,
        character_manifest_path=character_manifest_path,
        uem_dir=uem_dir,
        ghidra_path=ghidra_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(tmp_path, output_path)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Export actor race visual map")
    parser.add_argument("--client-table", type=Path, default=DEFAULT_CLIENT_TABLE_PATH)
    parser.add_argument("--ghidra", type=Path, help="Optional Ghidra JSON override used only for table regeneration")
    parser.add_argument(
        "--character-manifest",
        type=Path,
        default=DEFAULT_CHARACTER_MANIFEST_PATH,
    )
    parser.add_argument("--uem-dir", type=Path, default=DEFAULT_UEM_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    payload = write_actor_race_visual_map(
        output_path=args.out,
        client_table_path=args.client_table,
        character_manifest_path=args.character_manifest,
        uem_dir=args.uem_dir,
        ghidra_path=args.ghidra,
    )
    summary = payload["summary"]
    print(
        "Wrote actor race visual map: "
        f"{summary['matched_exported_prefix_count']}/"
        f"{summary['race_count']} exported prefixes "
        f"({args.out})"
    )


if __name__ == "__main__":
    main()
