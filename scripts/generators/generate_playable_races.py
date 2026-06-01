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
    python3 scripts/generators/generate_playable_races.py

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
MATERIAL_MANIFEST_PATH = os.path.join(OUT_DIR, "material_manifest.json")
_MATERIAL_MANIFEST: dict | None = None
_MANIFEST_BY_TEXTURE_NAME: dict[str, dict] | None = None
_MANIFEST_BY_MATERIAL_NAME: dict[str, dict] | None = None

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
    "Varanthari":     "barbarian",
    "Varanjar":       "barbarian",
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

HEAD_STYLE_INDEX = {
    # The client maps these sub-races to a shared package plus a fixed head/ear
    # style inside that package.
    "DarkElf": 1,
    "WoodElf": 2,
    "Varanthari": 1,
}

FACE_COUNT = {
    "Kojani": 4,
    "Qaliathari": 4,
    "Thestran": 4,
    "Mordebi": 4,
    "KojanBarbarian": 4,
    "Vulmane": 2,
}

STYLE_INDEX_OVERRIDES = {
    # VGClient's race init strings place the shared human/barbarian variants in
    # T/Q/K/Mordebi order:
    #   Human_*_T_ASounds -> OPTThestran
    #   Human_*_Q_ASounds -> OPTQaliathari
    #   Human_*_K_ASounds -> OPTKojanHuman
    #   Mordebi_*_ASounds -> OPTMordebi
    #   Barbarian_*_T_ASounds -> OPTVaranjar
    #   Barbarian_*_Q_ASounds -> OPTVaranthari
    # Keep these as single default styles so Godot does not default every
    # shared-package race to the first extracted texture.
    ("Thestran", "M"): [0],
    ("Thestran", "F"): [0],
    ("Qaliathari", "M"): [1],
    ("Qaliathari", "F"): [1],
    ("Kojani", "M"): [2],
    ("Kojani", "F"): [2],
    ("Mordebi", "M"): [3],
    ("Mordebi", "F"): [3],
    # No local kojanBarbarian modular package has been found yet. The client
    # does have optimizedKojanBarbarian/OPTKojanBarbarian strings, while the
    # extracted player mapping routes the playable prefix through Kojan/human
    # art. Treat this as the current best unresolved Kojan-style mapping and
    # let audit output keep flagging that it is shared with Kojani.
    ("KojanBarbarian", "M"): [2],
    ("KojanBarbarian", "F"): [2],
    # High Elf is style 0 inside the shared Elf package. The modular body_0
    # texture is exported without a Shader wrapper, so it comes from direct CLR
    # extraction rather than shader-following.
    ("HighElf", "M"): [0],
    ("HighElf", "F"): [0],
    # Vulmane exposes modular body CLR exports; keep the playable view on those
    # textures and do not use optimized full-body assets as runtime fallback.
    ("Vulmane", "M"): [0],
    ("Vulmane", "F"): [0],
    # Varanthari use the Barbarian package in the client race init, and style 1
    # is the matching human-looking texture/body tone set.
    ("Varanjar", "M"): [0],
    ("Varanjar", "F"): [0],
    ("Varanthari", "M"): [1],
    ("Varanthari", "F"): [1],
}

BODY_STYLE_INDEX_OVERRIDES = {
    # The original UTX_vulmane_F_char Shader.vulmane_F_char_body_01_SHD points
    # at body_10 while Shader.vulmane_F_char_head_01_SHD points at head_0.
    ("Vulmane", "F"): [10],
}


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
        records = [
            _skin_texture_record(mat_name, f"/output/textures/{fname}")
            for _, fname in tones
        ]
        if len(records) > 1:  # only include if there's more than one tone available
            result[mat_name] = records

    return result


def _load_material_manifest() -> dict:
    global _MATERIAL_MANIFEST
    if _MATERIAL_MANIFEST is not None:
        return _MATERIAL_MANIFEST
    try:
        with open(MATERIAL_MANIFEST_PATH) as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        manifest = {}
    _MATERIAL_MANIFEST = manifest if isinstance(manifest, dict) else {}
    return _MATERIAL_MANIFEST


def _manifest_indexes() -> tuple[dict[str, dict], dict[str, dict]]:
    global _MANIFEST_BY_TEXTURE_NAME, _MANIFEST_BY_MATERIAL_NAME
    if _MANIFEST_BY_TEXTURE_NAME is not None and _MANIFEST_BY_MATERIAL_NAME is not None:
        return _MANIFEST_BY_TEXTURE_NAME, _MANIFEST_BY_MATERIAL_NAME
    by_texture: dict[str, dict] = {}
    by_material: dict[str, dict] = {}
    for source_ref, entry in _load_material_manifest().items():
        source_key = str(source_ref).lower()
        by_material.setdefault(source_key.rsplit(".", 1)[-1], entry)
        base_color = entry.get("base_color") if isinstance(entry, dict) else None
        if not isinstance(base_color, dict):
            continue
        for key in ("texture_name", "asset_name"):
            value = base_color.get(key)
            if value:
                by_texture.setdefault(str(value).lower(), base_color)
    _MANIFEST_BY_TEXTURE_NAME = by_texture
    _MANIFEST_BY_MATERIAL_NAME = by_material
    return by_texture, by_material


def _skin_texture_record(material_name: str, texture_url: str) -> dict:
    filename = os.path.basename(texture_url)
    texture_stem = os.path.splitext(filename)[0]
    by_texture, by_material = _manifest_indexes()
    manifest_texture = by_texture.get(texture_stem.lower(), {})
    manifest_material = by_material.get(material_name.lower(), {})
    asset_path = manifest_texture.get("asset_path") or f"output/textures/{filename}"
    return {
        "material_name": material_name,
        "material_ref": manifest_material.get("source_ref"),
        "texture_name": manifest_texture.get("texture_name") or texture_stem,
        "texture_ref": manifest_texture.get("texture_ref"),
        "texture_package": manifest_texture.get("texture_package"),
        "asset_path": asset_path,
        "url": "/" + asset_path.lstrip("/"),
    }


def _gltf_material_names(gltf_path: str) -> list[str]:
    """Return list of material names from a gltf file (or [] on error)."""
    try:
        with open(gltf_path) as f:
            data = json.load(f)
        return [m.get("name", "") for m in data.get("materials", [])]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _texture_url(stem: str, idx: int) -> str | None:
    """Return an exact CLR texture URL for stem/index, if it exists."""
    fname = f"{stem}_{idx}_CLR.png"
    path = os.path.join(TEX_DIR, fname)
    if os.path.exists(path):
        return f"/output/textures/{fname}"
    return None


def _texture_indices(stem: str) -> list[int]:
    """Return exact primary CLR indices for a texture stem."""
    if not os.path.isdir(TEX_DIR):
        return []
    pattern = re.compile(rf'^{re.escape(stem)}_(\d+)_CLR\.png$', re.IGNORECASE)
    indices: list[int] = []
    for fname in os.listdir(TEX_DIR):
        m = pattern.match(fname)
        if not m:
            continue
        idx = int(m.group(1))
        if not _primary_variant(idx):
            continue
        indices.append(idx)
    return sorted(set(indices))


def _style_indices_for_entry(race: str, gender: str, pkg: str, head_idx: int) -> list[int]:
    override = STYLE_INDEX_OVERRIDES.get((race, gender))
    if override:
        return list(override)

    head_stem = f"{pkg}_{gender}_char_head"
    if head_idx > 0:
        indices = [head_idx]
        # Elf sub-races store face/tone variants as 11/12 and 21/22.
        for candidate in range(head_idx * 10 + 1, head_idx * 10 + 10):
            if _texture_url(head_stem, candidate):
                indices.append(candidate)
        return indices

    body_stem = f"{pkg}_{gender}_char_body"
    indices = sorted(set(_texture_indices(head_stem)) | set(_texture_indices(body_stem)))
    return indices or [0]


def _head_urls_for_styles(pkg: str, gender: str, style_indices: list[int]) -> list[str]:
    stem = f"{pkg}_{gender}_char_head"
    urls: list[str] = []
    last_url: str | None = None
    for idx in style_indices:
        url = _texture_url(stem, idx)
        if url is None:
            url = last_url or _texture_url(stem, 0)
        if url is None:
            continue
        urls.append(url)
        last_url = url
    return urls


def _body_url_for_style(
    race: str,
    gender: str,
    pkg: str,
    style_idx: int,
    head_idx: int,
) -> str | None:
    body_override = BODY_STYLE_INDEX_OVERRIDES.get((race, gender))
    candidate_indices: list[int] = []
    if body_override:
        candidate_indices.extend(body_override)
    else:
        candidate_indices.append(style_idx)
        if style_idx >= 10 and head_idx > 0:
            candidate_indices.append(head_idx)
        candidate_indices.append(0)

    stems = [f"{pkg}_{gender}_char_body"]
    for idx in candidate_indices:
        for stem in stems:
            url = _texture_url(stem, idx)
            if url:
                return url
    return None


def _body_urls_for_styles(
    race: str,
    gender: str,
    pkg: str,
    style_indices: list[int],
    head_idx: int,
) -> list[str]:
    urls: list[str] = []
    for idx in style_indices:
        url = _body_url_for_style(race, gender, pkg, idx, head_idx)
        if url:
            urls.append(url)
    return urls


def _variant_head_materials(pkg: str, gender: str, start_idx: int, face_count: int) -> set[str]:
    mats: set[str] = set()
    for idx in range(start_idx, start_idx + max(face_count, 1)):
        for part_name in ["head", "ears"]:
            export = f"{pkg}_{gender}_char_{part_name}_{idx}_C_0"
            gltf_path = os.path.join(
                CHARS_DIR, f"UEM_{pkg}_{gender}_char", f"{export}.gltf"
            )
            for mat in _gltf_material_names(gltf_path):
                if "_char_head_" in mat:
                    mats.add(mat)
    return mats


def skin_tones_for_entry(
    race: str,
    gender: str,
    pkg: str,
    body_parts: list[dict],
    head_idx: int,
    face_count: int,
) -> dict | None:
    """Build an aligned skin-tone map for body and head materials.

    The body mesh is always npcHuman, so its body/neck materials must be mapped
    to the selected race's extracted body textures when those exist.
    """
    style_indices = _style_indices_for_entry(race, gender, pkg, head_idx)
    head_urls = _head_urls_for_styles(pkg, gender, style_indices)
    body_urls = _body_urls_for_styles(race, gender, pkg, style_indices, head_idx)
    if not head_urls or not body_urls:
        return None

    tone_count = min(len(head_urls), len(body_urls))
    if tone_count <= 0:
        return None
    head_urls = head_urls[:tone_count]
    body_urls = body_urls[:tone_count]

    tones: dict[str, list[str]] = {}
    body_material = f"human_{gender}_char_body_0_SHD"
    hand_material = f"human_{gender}_char_head_0_SHD"
    tones[body_material] = body_urls
    # The shared npcHuman body mesh carries hands as a separate primitive with
    # the legacy "head" material name. Its UVs match head/skin textures, not
    # the torso body atlas.
    tones[hand_material] = head_urls

    for mat in _variant_head_materials(pkg, gender, head_idx, face_count):
        tones[mat] = head_urls

    # Keep only materials that can actually appear on the selected body parts or
    # their style-swapped head/ear variants.
    valid_mats: set[str] = {body_material, hand_material}
    valid_mats |= _variant_head_materials(pkg, gender, head_idx, face_count)
    tones = {
        mat: [_skin_texture_record(mat, url) for url in urls]
        for mat, urls in tones.items()
        if mat in valid_mats
    }
    return tones if tones else None


# ---------------------------------------------------------------------------
# Entry builder
# ---------------------------------------------------------------------------

def build_entry(race: str, gender: str) -> dict:
    pkg = CHAR_PKG_MAP[race]
    g = gender
    head_idx = HEAD_STYLE_INDEX.get(race, 0)
    face_count = FACE_COUNT.get(race, 1)

    body_pkg    = f"UEM_npcHuman_{g}_char"
    body_export = f"npcHuman_{g}_char_body_0_C_0"
    head_pkg    = f"UEM_{pkg}_{g}_char"
    head_export = f"{pkg}_{g}_char_head_{head_idx}_C_0"
    ears_export = f"{pkg}_{g}_char_ears_{head_idx}_C_0"

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
        "skin_tones":   skin_tones_for_entry(race, g, pkg, body_parts, head_idx, face_count),
        "face_count":   face_count,
    }


def _load_existing_slider_defaults(path: str) -> dict[str, list]:
    try:
        with open(path) as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    defaults: dict[str, list] = {}
    if not isinstance(existing, list):
        return defaults
    for entry in existing:
        if not isinstance(entry, dict):
            continue
        key = f"{entry.get('race', '')}_{entry.get('gender', '')}"
        values = entry.get("slider_defaults")
        if key != "_" and isinstance(values, list):
            defaults[key] = values
    return defaults


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    pr_path = os.path.join(OUT_DIR, "playable_races.json")
    existing_slider_defaults = _load_existing_slider_defaults(pr_path)

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
            entry = build_entry(race, gender)
            key = f"{race}_{gender}"
            if key in existing_slider_defaults:
                entry["slider_defaults"] = existing_slider_defaults[key]
            entries.append(entry)

    with open(pr_path, "w") as f:
        json.dump(entries, f, indent=2)
    print(f"Wrote {len(entries)} entries to {pr_path}")


if __name__ == "__main__":
    main()
