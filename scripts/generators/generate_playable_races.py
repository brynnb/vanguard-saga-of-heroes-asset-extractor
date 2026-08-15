#!/usr/bin/env python3
"""Generate source-proven playable character identities.

This generator does not assemble modular ``npcHuman`` bodies, guess texture
substitutions, or preserve values from an earlier generated file. Each
supported entry identifies one optimized Vanguard visual package and style.
The facial-control exporter expands that identity into a visible mesh, master
skeleton, sockets, and authored AnimSet chain.

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
# strings name a dedicated optimized visual, but that package is not present
# in the recovered asset set. Showing the Kojani-human style was a guess.
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


def _entry(race: str, gender: str) -> dict[str, Any]:
    source = PLAYABLE_VISUAL_SOURCE.get(race)
    entry: dict[str, Any] = {
        "schema": 2,
        "race": race,
        "gender": gender,
        "display": f"{race} {gender}",
        "scale": RACE_SCALES[race][gender],
        "face_count": FACE_COUNT.get(race, 1),
        "visual_supported": source is not None,
    }
    if source is None:
        entry["unsupported_reason"] = (
            "Recovered assets do not contain the dedicated optimized "
            "Kojan-barbarian visual named by the original client."
        )
        return entry

    prefix, style_index = source
    package = f"UEM_optimized{prefix}_{gender}_char"
    stem = package.removeprefix("UEM_")
    package_path = CHARACTERS / package
    master_export = f"{stem}_ALL_{style_index}_SKELETON"
    visible_pattern = f"{stem}_ALL_{style_index}_C_*.gltf"
    visible_paths = sorted(package_path.glob(visible_pattern))
    if not package_path.is_dir() or not visible_paths:
        raise RuntimeError(
            f"Missing authoritative playable visual {package}/{visible_pattern}"
        )
    entry.update(
        {
            "optimized_package": package,
            "optimized_style_index": style_index,
            "optimized_master_export": master_export,
        }
    )
    return entry


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    entries = [
        _entry(race, gender)
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
