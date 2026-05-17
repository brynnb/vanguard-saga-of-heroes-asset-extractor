"""
generate_playable_races.py

Generates three output files:

  output/data/playable_races.json   — 40 composite definitions (20 races × 2 genders)
  output/data/race_scales.json      — per-race 3D scale values from the server races table
  output/data/skin_variants.json    — material-name → primary skin-tone texture URL list

Each playable_races entry describes:
  - body_parts: [npcHuman body, race head, race ears] with gltf paths
  - clth_package: always UEM_human_M/F_clth (shared wardrobe)
  - scale: uniform XYZ scale the engine applies to the assembled composite
  - skin_tones: {materialName: [url_tone0, url_tone1, ...]} for texture swapping

Usage:
    python scripts/generators/generate_playable_races.py

Requirements:
    output/textures/   must be populated (run scripts/exporters/export_character_meshes.py first)
    output/meshes/characters/  must contain the relevant _char gltf packages
"""

import json
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR   = os.path.dirname(os.path.dirname(SCRIPT_DIR))
CHARS_DIR  = os.path.join(ROOT_DIR, "output", "meshes", "characters")
TEX_DIR    = os.path.join(ROOT_DIR, "output", "textures")
OUT_DIR    = os.path.join(ROOT_DIR, "output", "data")

# ---------------------------------------------------------------------------
# Race -> char package prefix mapping
# ---------------------------------------------------------------------------
CHAR_PKG_MAP = {
    "Dwarf":          "dwarf",
    "DarkElf":        "elf",
    "HighElf":        "elf",
    "WoodElf":        "elf",
    "Gnome":          "gnome",
    "Goblin":         "goblin",
    "HalfElf":        "halfElf",
    "LesserGiant":    "halfGiant",
    "Halfling":       "halfling",
    "Kojani":         "human",
    "Qaliathari":     "human",
    "Thestran":       "human",
    "Mordebi":        "human",
    "Orc":            "orc",
    "Raki":           "raki",
    "Vulmane":        "vulmane",
    "KojanBarbarian": "human",
    "Varanthari":     "vulmane",
    "Varanjar":       "halfGiant",
    "Kurashasa":      "kura",
}

# ---------------------------------------------------------------------------
# 3D scale values from VG.sql `races` table.
# male3dScale / female3dScale — applied to the assembled composite at runtime.
# HalfElf (scale=1.0) is the baseline human reference.
# ---------------------------------------------------------------------------
RACE_SCALES = {
    "Dwarf":          {"M": 1.11,    "F": 0.9053},
    "DarkElf":        {"M": 1.0563,  "F": 0.9941},
    "HighElf":        {"M": 1.0563,  "F": 0.9941},
    "WoodElf":        {"M": 1.0563,  "F": 0.9941},
    "Gnome":          {"M": 0.8,     "F": 0.8},
    "Goblin":         {"M": 0.8331,  "F": 0.8043},
    "HalfElf":        {"M": 1.0,     "F": 1.0},
    "LesserGiant":    {"M": 1.2378,  "F": 1.1691},
    "Halfling":       {"M": 0.658,   "F": 0.6427},
    "Kojani":         {"M": 1.0,     "F": 1.0},
    "Qaliathari":     {"M": 1.0,     "F": 1.0},
    "Thestran":       {"M": 1.0,     "F": 1.0},
    "Mordebi":        {"M": 1.0,     "F": 1.0},
    "Orc":            {"M": 1.0049,  "F": 1.0042},
    "Raki":           {"M": 0.85,    "F": 0.8},
    "Vulmane":        {"M": 1.1361,  "F": 1.0772},
    "KojanBarbarian": {"M": 1.0,     "F": 1.0},
    "Varanthari":     {"M": 1.0884,  "F": 1.088},
    "Varanjar":       {"M": 1.0884,  "F": 1.088},
    "Kurashasa":      {"M": 1.1505,  "F": 1.0834},
}

PLAYER_RACES = [
    "Dwarf", "DarkElf", "HighElf", "WoodElf", "Gnome", "Goblin",
    "HalfElf", "LesserGiant", "Halfling", "Kojani", "Qaliathari",
    "Thestran", "Mordebi", "Orc", "Raki", "Vulmane", "KojanBarbarian",
    "Varanthari", "Varanjar", "Kurashasa",
]


# ---------------------------------------------------------------------------
# Skin variant discovery
# ---------------------------------------------------------------------------

def _primary_variant(n: int) -> bool:
    """True if variant index N is a 'primary' skin tone (not a face-style sub-combo).

    Kept:  0–9  (human-style tone index: 0=default, 1-3=alternates)
           10, 20, 30  (raki/kura-style fur-colour patterns)
    Dropped: face-style combos (11, 12, 13, 21 …) and outliers (40+, 100).
    """
    if n < 10:
        return True
    # Allow only the first three tens-multiples for creature-style races
    if n in (10, 20, 30):
        return True
    return False


def build_skin_variants(tex_dir: str) -> dict:
    """Scan textures dir; return {material_name: [url0, url1, ...]} for _char_ CLR textures.

    Material name convention: {prefix}_{gender}_char_{part}_{N}_SHD
    CLR texture convention:   {prefix}_{gender}_char_{part}_{N}_CLR.png
    """
    if not os.path.isdir(tex_dir):
        return {}

    # Pattern: {anything}_char_{part}_{index}_CLR.png (case-insensitive CLR)
    pattern = re.compile(r'^(.+?_char_.+?)_(\d+)_CLR\d*\.png$', re.IGNORECASE)

    # Collect: prefix → {variant_index: filename}
    groups: dict[str, dict[int, str]] = {}
    for fname in os.listdir(tex_dir):
        m = pattern.match(fname)
        if not m:
            continue
        prefix = m.group(1)   # e.g. 'human_M_char_body'
        idx    = int(m.group(2))
        if not _primary_variant(idx):
            continue
        groups.setdefault(prefix, {})[idx] = fname

    # Build material_name → [url, url, ...]
    result: dict[str, list[str]] = {}
    for prefix, variants in groups.items():
        if not variants:
            continue
        mat_name = f"{prefix}_0_SHD"  # the gltf always embeds the _0 variant
        tones = sorted(variants.items())  # sort by variant index
        urls = [f"/output/textures/{fname}" for _, fname in tones]
        if len(urls) > 1:  # only include if there's more than one tone available
            result[mat_name] = urls

    return result


def _gltf_material_names(gltf_path: str) -> list[str]:
    """Return list of material names from a gltf file (or [] on error)."""
    try:
        with open(gltf_path) as f:
            data = json.load(f)
        return [m.get("name", "") for m in data.get("materials", [])]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def skin_tones_for_parts(body_parts: list[dict], skin_variants: dict) -> dict:
    """Return the skin_tones subset relevant to the gltf parts for this race entry."""
    all_mats: set[str] = set()
    for part in body_parts:
        gltf_path = os.path.join(CHARS_DIR, part["path"])
        for mat in _gltf_material_names(gltf_path):
            if "_char_" in mat:
                all_mats.add(mat)

    # Intersect with known variants
    tones = {mat: skin_variants[mat] for mat in all_mats if mat in skin_variants}
    return tones if tones else None


# ---------------------------------------------------------------------------
# Entry builder
# ---------------------------------------------------------------------------

def build_entry(race: str, gender: str, skin_variants: dict) -> dict:
    pkg = CHAR_PKG_MAP[race]
    g = gender

    body_pkg    = f"UEM_npcHuman_{g}_char"
    body_export = f"npcHuman_{g}_char_body_0_C_0"
    head_pkg    = f"UEM_{pkg}_{g}_char"
    head_export = f"{pkg}_{g}_char_head_0_C_0"
    ears_export = f"{pkg}_{g}_char_ears_0_C_0"

    body_parts = [
        {"package": body_pkg, "export": body_export,
         "path": f"{body_pkg}/{body_export}.gltf"},
        {"package": head_pkg, "export": head_export,
         "path": f"{head_pkg}/{head_export}.gltf"},
        {"package": head_pkg, "export": ears_export,
         "path": f"{head_pkg}/{ears_export}.gltf"},
    ]

    return {
        "race":         race,
        "gender":       g,
        "display":      f"{race} {g}",
        "clth_package": f"UEM_human_{g}_clth",
        "scale":        RACE_SCALES[race][g],
        "body_parts":   body_parts,
        "skin_tones":   skin_tones_for_parts(body_parts, skin_variants),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # ── Skin variants index ─────────────────────────────────────────────────
    skin_variants = build_skin_variants(TEX_DIR)
    sv_path = os.path.join(OUT_DIR, "skin_variants.json")
    with open(sv_path, "w") as f:
        json.dump(skin_variants, f, indent=2)
    print(f"Wrote {len(skin_variants)} material entries to {sv_path}")

    # ── Race scales ─────────────────────────────────────────────────────────
    rs_path = os.path.join(OUT_DIR, "race_scales.json")
    with open(rs_path, "w") as f:
        json.dump(RACE_SCALES, f, indent=2)
    print(f"Wrote {len(RACE_SCALES)} race scale entries to {rs_path}")

    # ── Playable races ──────────────────────────────────────────────────────
    entries = []
    for race in PLAYER_RACES:
        for gender in ["M", "F"]:
            entries.append(build_entry(race, gender, skin_variants))

    pr_path = os.path.join(OUT_DIR, "playable_races.json")
    with open(pr_path, "w") as f:
        json.dump(entries, f, indent=2)
    print(f"Wrote {len(entries)} entries to {pr_path}")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
