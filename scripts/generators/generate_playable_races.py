#!/usr/bin/env python3
"""Generate source-proven playable character identities.

Playable identities select the modular UEM master and its authored UseMesh
assembly. Optimized package fields are supplemental identities for explicitly
optimized NPCs and forensic comparisons, not player defaults. The facial-control
exporter resolves the master, components, sockets and authored AnimSet chain.

``generate_customization_data.py`` subsequently adds deterministic slider
defaults from the original client tables.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CHARACTERS = ROOT / "output" / "meshes" / "characters"
OUTPUT = ROOT / "output" / "data"
MATERIAL_MANIFEST = OUTPUT / "material_manifest.json"


# Values recovered from the server race table. Half Elf is the human-sized
# baseline; this scale applies to the complete visual assembly.
RACE_SCALES = {
    "Dwarf": {"M": 1.11, "F": 0.9053},
    "DarkElf": {"M": 1.0563, "F": 0.9941},
    "HighElf": {"M": 1.0563, "F": 0.9941},
    "WoodElf": {"M": 1.0563, "F": 0.9941},
    "Gnome": {"M": 0.8, "F": 0.8},
    "Goblin": {"M": 0.8331, "F": 0.8043},
    "HalfElf": {"M": 1.0, "F": 1.0},
    "LesserGiant": {"M": 1.2378, "F": 1.1691},
    "Halfling": {"M": 0.658, "F": 0.6427},
    "Kojani": {"M": 1.0, "F": 1.0},
    "Qaliathari": {"M": 1.0, "F": 1.0},
    "Thestran": {"M": 1.0, "F": 1.0},
    "Mordebi": {"M": 1.0, "F": 1.0},
    "Orc": {"M": 1.0049, "F": 1.0042},
    "Raki": {"M": 0.85, "F": 0.8},
    "Vulmane": {"M": 1.1361, "F": 1.0772},
    "KojanBarbarian": {"M": 1.0, "F": 1.0},
    "Varanthari": {"M": 1.0884, "F": 1.088},
    "Varanjar": {"M": 1.0884, "F": 1.0884},
    "Kurashasa": {"M": 1.1505, "F": 1.0834},
}

PLAYER_RACES = list(RACE_SCALES)


# Original-client race initialization strings and actual ALL_<style> exports
# corroborate this ordering. KojanBarbarian is deliberately absent: client
# evidence does not establish a supported playable modular assembly for it.
# Showing the Kojani-human style was a guess.
PLAYABLE_VISUAL_SOURCE: dict[str, tuple[str, int]] = {
    "Dwarf": ("Dwarf", 0),
    "DarkElf": ("Elf", 1),
    "HighElf": ("Elf", 0),
    "WoodElf": ("Elf", 2),
    "Gnome": ("Gnome", 0),
    "Goblin": ("Goblin", 0),
    "HalfElf": ("HalfElf", 0),
    "LesserGiant": ("HalfGiant", 0),
    "Halfling": ("Halfling", 0),
    "Kojani": ("Human", 2),
    "Qaliathari": ("Human", 1),
    "Thestran": ("Human", 0),
    "Mordebi": ("Human", 3),
    "Orc": ("Orc", 0),
    "Raki": ("Raki", 0),
    "Vulmane": ("Vulmane", 0),
    "Varanthari": ("Barbarian", 1),
    "Varanjar": ("Barbarian", 0),
    "Kurashasa": ("Kura", 0),
}

# The facial-control exporter imports these compatibility views. They are
# derived from the one source table rather than being independent policy.
HEAD_STYLE_INDEX = {race: source[1] for race, source in PLAYABLE_VISUAL_SOURCE.items()}
STYLE_INDEX_OVERRIDES = {
    (race, gender): [style]
    for race, (_prefix, style) in PLAYABLE_VISUAL_SOURCE.items()
    for gender in ("M", "F")
}

FACE_COUNT = {
    "Kojani": 4,
    "Qaliathari": 4,
    "Thestran": 4,
    "Mordebi": 4,
    "Vulmane": 2,
}


def _skin_tint_record(
    material_manifest: dict[str, Any], prefix: str, gender: str, style_index: int
) -> dict[str, Any]:
    source_prefix = prefix[:1].lower() + prefix[1:]
    package = f"UTX_{source_prefix}_{gender}_char"

    def material(part: str) -> dict[str, Any]:
        source_ref = (
            f"{package}.Shader.{source_prefix}_{gender}_char_{part}_{style_index}_SHD"
        )
        entry = material_manifest.get(source_ref, {})
        tint_alpha = entry.get("tint_alpha") or {}
        tint_palette = entry.get("tint_palette") or {}
        if not tint_alpha.get("asset_path") or not tint_palette.get("asset_path"):
            return {}
        return {
            "source_ref": source_ref,
            "tint_alpha": tint_alpha,
            "tint_palette": tint_palette,
        }

    body = material("body")
    head = material("head")
    palette = (head or body).get("tint_palette", {})
    if not palette:
        return {}
    return {"body": body, "head": head, "palette": palette}


def _entry(
    race: str, gender: str, material_manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    material_manifest = material_manifest or {}
    source = PLAYABLE_VISUAL_SOURCE.get(race)
    entry: dict[str, Any] = {
        "schema": 3,
        "race": race,
        "gender": gender,
        "display": f"{race} {gender}",
        "scale": RACE_SCALES[race][gender],
        "face_count": FACE_COUNT.get(race, 1),
        "visual_supported": source is not None,
    }
    if source is None:
        entry["unsupported_reason"] = (
            "No reviewed playable modular assembly for KojanBarbarian; "
            "do not substitute the unrelated Kojani human model."
        )
        return entry

    prefix, style_index = source
    modular_prefix = prefix[:1].lower() + prefix[1:]
    modular_package = f"UEM_{modular_prefix}_{gender}_char"
    modular_stem = modular_package.removeprefix("UEM_")
    head_path = CHARACTERS / modular_package / f"{modular_stem}_head_{style_index}_C_0.gltf"
    if not head_path.is_file():
        raise RuntimeError(f"Missing playable modular head: {head_path}")
    package = f"UEM_optimized{prefix}_{gender}_char"
    stem = package.removeprefix("UEM_")
    master_export = f"{stem}_ALL_{style_index}_SKELETON"
    entry.update(
        {
            "visual_kind": "modular_player",
            "modular_package": modular_package,
            "modular_style_index": style_index,
            "modular_master_export": f"{modular_stem}_ALL_{style_index}_SKELETON",
            "optimized_package": package,
            "optimized_style_index": style_index,
            "optimized_master_export": master_export,
        }
    )
    skin_tint = _skin_tint_record(material_manifest, prefix, gender, style_index)
    if skin_tint:
        entry["skin_tint"] = skin_tint
    return entry


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    material_manifest: dict[str, Any] = {}
    if MATERIAL_MANIFEST.exists():
        material_manifest = json.loads(MATERIAL_MANIFEST.read_text(encoding="utf-8"))
    entries = [
        _entry(race, gender, material_manifest)
        for race in PLAYER_RACES
        for gender in ("M", "F")
    ]
    playable_path = OUTPUT / "playable_races.json"
    playable_path.write_text(json.dumps(entries, indent=2) + "\n")
    scales_path = OUTPUT / "race_scales.json"
    scales_path.write_text(json.dumps(RACE_SCALES, indent=2) + "\n")
    supported = sum(bool(entry["visual_supported"]) for entry in entries)
    print(
        f"Wrote {len(entries)} playable identities ({supported} supported) "
        f"to {playable_path}"
    )
    print(f"Wrote {len(RACE_SCALES)} race scales to {scales_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
