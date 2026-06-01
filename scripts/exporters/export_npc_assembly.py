#!/usr/bin/env python3
"""Export legacy browser NPC assembly data from a VGO world snapshot.

Produces:
  output/data/npc_assembly.json          — all server pawns
  output/data/npc_assembly_viewer.json   — only pawns with resolved meshes
  output/data/race_mesh_map.json         — race→UEM prefix reference

This script is not part of the Godot live client. Godot receives actor race,
model, and attachment groups from VGOEmulator and resolves those server values
against extracted race/attachment lookup tables at runtime.

Approach:
  Each race maps to an exact UEM filename prefix (e.g. "camel", "thestran",
  "golemrock").  The prefix + gender determines the _char package which holds
  the base body SKELETON mesh.

  Attachment meshes (clothing, weapons) are resolved in two ways:
  1. Direct decode: attachment_to_clth_meshes.json maps attachment_index
     to specific _clth/_tool mesh exports parsed from ITEMS UEM templates.
  2. Race-prefix fallback: for unresolved attachments at inventory_slot=6
     (body clothing), we pick the best _clth mesh for the NPC's race+gender.

  Attachment set structure from unreal_pawn_attachment_groups/sets is preserved
  in attachment_sets. The flat meshes list remains for compatibility only.
"""

import argparse
import json
import os
import sys
import re
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "scripts", "lib"))

from vgo_world_npc_snapshot import (  # noqa: E402
    DEFAULT_DB_CONFIG,
    DEFAULT_SNAPSHOT_PATH,
    db_config_from_args,
    fetch_snapshot,
    group_snapshot,
    load_snapshot,
)

OUTPUT_DIR = os.path.join(ROOT_DIR, "output", "data")
MANIFEST_PATH = os.path.join(
    ROOT_DIR, "output", "meshes", "characters", "manifest.json"
)
ATT_CLTH_PATH = os.path.join(OUTPUT_DIR, "attachment_to_clth_meshes.json")
RACE_PREFIX_PATH = os.path.join(OUTPUT_DIR, "race_to_mesh_prefix.json")
STATIC_MESH_MANIFEST_PATH = os.path.join(
    ROOT_DIR, "output", "meshes", "buildings", "manifest.json"
)
OBJECT_RACE_MESH_MAP_PATH = os.path.join(OUTPUT_DIR, "object_race_mesh_map.json")

ASSETS_DIR = os.environ.get(
    "VANGUARD_ASSETS",
    os.environ.get("VANGUARD_ASSETS_PATH", os.path.expanduser("~/Downloads/Vanguard EMU/Assets")),
)
UEM_DIR = os.path.join(ASSETS_DIR, "Characters", "Meshes")

# OBJECT races are static meshes, not character UEM bodies. Prefer the original
# client object-race lookup table, then use these spawn-name overrides for DB
# rows where the race name is too generic or misleading.
OBJECT_STATIC_MESH_OVERRIDES = {
    "camelliaflower": [
        "Ra8001_Foliage_Meshes/Ra8001_Foliage_smallFlowers001.gltf",
    ],
    "carmelliaflower": [
        "Ra8001_Foliage_Meshes/Ra8001_Foliage_smallFlowers001.gltf",
    ],
    "trampledbushes": [
        "Ra8001_Foliage_Meshes/Ra8001_Foliage_smallBush001.gltf",
    ],
    "trialshrub": [
        "Ra8001_Foliage_Meshes/Ra8001_Foliage_smallBush001.gltf",
    ],
    "flower001": [
        "Ra8001_Foliage_Meshes/Ra8001_Foliage_smallFlowers001.gltf",
    ],
    "flower002": [
        "Ra8001_Foliage_Meshes/Ra8001_Foliage_smallFlowers002.gltf",
    ],
    "flower003": [
        "Ra8001_Foliage_Meshes/Ra8001_Foliage_smallFlowers003.gltf",
    ],
    "flower004": [
        "Ra8001_Foliage_Meshes/Ra8001_Foliage_smallFlowers004.gltf",
    ],
    "flower005": [
        "Ra8001_Foliage_Meshes/Ra8001_Foliage_smallFlowers005.gltf",
    ],
}

DB_CONFIG = dict(DEFAULT_DB_CONFIG)

# ---------------------------------------------------------------------------
# Race clothing acceptance groups.
#
# In Vanguard, the ITEMS attachment system uses human_* prefixed clothing
# meshes for ALL humanoid races (52K+ out of 56K _clth refs are human_*).
# Race-specific _clth packages exist but aren't referenced by ITEMS.
#
# Therefore: every humanoid race prefix accepts human_*/npchuman_*/thestran_*
# clothing meshes, PLUS meshes from its own body group (same skeleton).
# Creatures (not in any humanoid group) only accept their own prefix.
# ---------------------------------------------------------------------------

# Clothing prefixes that ALL humanoids accept (the shared wardrobe).
_HUMANOID_CLOTHING_PREFIXES = {
    "human", "npchuman", "thestran", "mordebi", "kojan", "qalian",
}

# Body groups: races sharing a skeleton (and thus _char/_clth body meshes).
# Each group also implicitly accepts _HUMANOID_CLOTHING_PREFIXES.
_BODY_GROUPS = [
    {"human", "npchuman", "thestran", "mordebi", "kojan", "qalian"},
    {"elf", "darkelf", "highelf", "woodelf", "npcelf"},
    {"dwarf", "npcdwarf"},
    {"orc", "npcorc", "halforc"},
    {"halfelf", "npchalfelf"},
    {"halfgiant", "npchalfgiant"},
    {"halfling", "npchalfling"},
    {"gnome", "npcgnome"},
    {"goblin", "npcgoblin"},
    {"raki", "npcraki"},
    {"vulmane", "npcvulmane"},
    {"kura", "npckura", "kurashasa"},
    {"barbarian", "npcbarbarian", "varanjar", "varanthari"},
]

# All prefixes that belong to a humanoid body group.
_ALL_HUMANOID_PREFIXES = set()
for _grp in _BODY_GROUPS:
    _ALL_HUMANOID_PREFIXES |= _grp

# Maps each humanoid prefix to the base player race that provides
# head_0 + ears_0 meshes in its _char package.
# Architecture: NPC humanoids use npcHuman body_0 + race-specific head/ears.
_HEAD_RACE = {}
for _head, _group in [
    ("human",     {"human", "npchuman", "thestran", "mordebi", "kojan", "qalian"}),
    ("elf",       {"elf", "darkelf", "highelf", "woodelf", "npcelf"}),
    ("dwarf",     {"dwarf", "npcdwarf"}),
    ("orc",       {"orc", "npcorc", "halforc"}),
    ("halfelf",   {"halfelf", "npchalfelf"}),
    ("halfgiant", {"halfgiant", "npchalfgiant"}),
    ("halfling",  {"halfling", "npchalfling"}),
    ("gnome",     {"gnome", "npcgnome"}),
    ("goblin",    {"goblin", "npcgoblin"}),
    ("raki",      {"raki", "npcraki"}),
    ("vulmane",   {"vulmane", "npcvulmane"}),
    ("kura",      {"kura", "npckura", "kurashasa"}),
    ("barbarian", {"barbarian", "npcbarbarian", "varanjar", "varanthari"}),
]:
    for _p in _group:
        _HEAD_RACE[_p] = _head

# OPT (optimized/LOD) race prefixes.
_OPT_PREFIXES = {
    "optimizedhuman", "optimizedelf", "optimizeddwarf", "optimizedorc",
    "optimizedhalfelf", "optimizedhalfgiant", "optimizedhalfling",
    "optimizedgnome", "optimizedgoblin", "optimizedraki",
    "optimizedvulmane", "optimizedkura", "optimizedbarbarian",
}


def _build_race_accept_map():
    """Build {npc_prefix: set_of_acceptable_mesh_prefixes}.

    Humanoid prefixes accept: own body group + shared humanoid clothing.
    OPT prefixes accept: own opt prefix + base body group + shared clothing.
    Creature prefixes (anything else): accept only their own prefix.
    """
    accept = {}
    # Humanoid body groups
    for group in _BODY_GROUPS:
        shared = group | _HUMANOID_CLOTHING_PREFIXES
        for p in group:
            accept[p] = shared
    # OPT races: find base group by stripping "optimized" prefix
    for opt_p in _OPT_PREFIXES:
        base = opt_p.replace("optimized", "", 1)
        # Find which body group the base belongs to
        base_group = {base}
        for group in _BODY_GROUPS:
            if base in group:
                base_group = group
                break
        accept[opt_p] = base_group | _HUMANOID_CLOTHING_PREFIXES | {opt_p}
    return accept


RACE_ACCEPT_MAP = _build_race_accept_map()

# Regex to parse mesh name: racePrefix_gender_type_...
_MESH_NAME_RE = re.compile(
    r'^([a-zA-Z]+)_([MFmf])_(clth|char|tool|hair|clk)', re.IGNORECASE
)


def _mesh_matches_race(mesh_name, npc_prefix):
    """Check if a mesh name is compatible with the NPC's race.

    Rules:
    - _tool meshes are race-neutral (weapons) → always accept
    - _clth and _char meshes must have a race prefix in the NPC's accept set
    - Meshes that don't match the standard naming pattern → accept (misc items)
    """
    if not npc_prefix:
        return True
    m = _MESH_NAME_RE.match(mesh_name)
    if not m:
        # Non-standard name (e.g. MOD_MOUNT_*, M_BACK_*) — accept
        return True
    mesh_race = m.group(1).lower()
    mesh_type = m.group(3).lower()
    if mesh_type == "tool":
        return True
    acceptable = RACE_ACCEPT_MAP.get(npc_prefix, {npc_prefix})
    return mesh_race in acceptable


# Clothing-only prefixes need a body _char from a different base race.
# Maps clothing prefix → {race_category_prefix: body_prefix}.
# Race categories: plain name = PLAYER, NPC prefix = NPC, OPT prefix = OPT.
BODY_FALLBACK = {
    # Human subraces (Thestran, Mordebi, Kojan, Qalian clothing → human body)
    "thestran":   {"npc": "npchuman", "player": "human", "opt": "optimizedhuman"},
    "mordebi":    {"npc": "npchuman", "player": "human", "opt": "optimizedhuman"},
    "kojan":      {"npc": "npchuman", "player": "human", "opt": "optimizedhuman"},
    "qalian":     {"npc": "npchuman", "player": "human", "opt": "optimizedhuman"},
    # Elf subraces
    "darkelf":    {"npc": "elf", "player": "elf", "opt": "optimizedelf"},
    "highelf":    {"npc": "elf", "player": "elf", "opt": "optimizedelf"},
    "woodelf":    {"npc": "elf", "player": "elf", "opt": "optimizedelf"},
    # Varanjar/Varanthari have clothing-only prefixes; VGClient maps their
    # playable character art through the shared Barbarian package.
    "varanjar":   {"npc": "barbarian", "player": "barbarian", "opt": "optimizedbarbarian"},
    "varanthari": {"npc": "barbarian", "player": "barbarian", "opt": "optimizedbarbarian"},
    # NPC races with clth-only or no packages → player race body
    "npcdwarf":    {"npc": "dwarf"},
    "npcgnome":    {"npc": "gnome"},
    "npcgoblin":   {"npc": "goblin"},
    "npchalfelf":  {"npc": "halfelf"},
    "npchalfling": {"npc": "halfling"},
    "npcorc":      {"npc": "orc"},
    "npcraki":     {"npc": "raki"},
    "npcvulmane":  {"npc": "vulmane"},
    # Undead — use zombie/skeleton body
    "lich":        {"npc": "skeleton"},
    "wight":       {"npc": "skeleton"},
    "wraith":      {"npc": "skeleton"},
    "undead":      {"npc": "zombie"},
    "deathknight": {"npc": "skeleton"},
    "mummy":       {"npc": "zombie"},
}


def load_manifest():
    """Load character mesh manifest; return {package_lower: [entries]}."""
    if not os.path.exists(MANIFEST_PATH):
        print(f"Warning: manifest not found at {MANIFEST_PATH}")
        return {}
    with open(MANIFEST_PATH) as f:
        entries = json.load(f)
    by_pkg = {}
    for e in entries:
        by_pkg.setdefault(e["package"].lower(), []).append(e)
    return by_pkg


def load_attachment_clth_map():
    """Load decoded attachment_index → clth mesh refs."""
    if not os.path.exists(ATT_CLTH_PATH):
        print(f"Warning: attachment clth map not found at {ATT_CLTH_PATH}")
        return {}
    with open(ATT_CLTH_PATH) as f:
        return json.load(f)


def load_static_mesh_manifest():
    """Load exported static mesh paths used for OBJECT race fallbacks."""
    if not os.path.exists(STATIC_MESH_MANIFEST_PATH):
        print(f"Warning: static mesh manifest not found at {STATIC_MESH_MANIFEST_PATH}")
        return {}, {}

    with open(STATIC_MESH_MANIFEST_PATH) as f:
        raw = json.load(f)

    mesh_paths = raw.get("meshes", []) if isinstance(raw, dict) else raw
    by_path = {}
    by_export_key = {}
    for mesh_path in mesh_paths:
        if not isinstance(mesh_path, str) or not mesh_path.endswith(".gltf"):
            continue
        package = os.path.dirname(mesh_path)
        export = os.path.splitext(os.path.basename(mesh_path))[0]
        entry = {
            "path": mesh_path,
            "export": export,
            "slot": "body",
            "package": package,
            "vertices": 0,
            "faces": 0,
            "source": "object_static_mesh",
        }
        by_path[mesh_path.lower()] = entry
        by_export_key.setdefault(_object_mesh_key(export), []).append(entry)

    return by_path, by_export_key


def load_object_race_mesh_map():
    """Load or regenerate the client object-race name -> static mesh map."""
    try:
        from export_object_race_mesh_map import write_object_race_mesh_map

        payload = write_object_race_mesh_map()
    except Exception as exc:
        if not os.path.exists(OBJECT_RACE_MESH_MAP_PATH):
            print(f"Warning: object race mesh map unavailable: {exc}")
            return {}
        print(f"Warning: using existing object race mesh map after rebuild failed: {exc}")
        with open(OBJECT_RACE_MESH_MAP_PATH, encoding="utf-8") as f:
            payload = json.load(f)

    races = payload.get("races", {}) if isinstance(payload, dict) else {}
    by_key = {}
    for race_name, entry in races.items():
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        by_key[_object_mesh_key(race_name)] = entry

    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    if summary:
        print(
            "Object race mesh map: "
            f"{summary.get('matched_count', len(by_key))}/"
            f"{summary.get('race_count', len(races))} matched"
        )
    return by_key


def _object_mesh_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _clone_mesh_entry(entry):
    return {
        "path": entry["path"],
        "export": entry["export"],
        "slot": entry.get("slot", "body"),
        "package": entry.get("package", ""),
        "vertices": entry.get("vertices", 0),
        "faces": entry.get("faces", 0),
        "source": entry.get("source", "object_static_mesh"),
    }


def _resolve_static_meshes_by_name(names, static_by_path, static_by_export):
    paths = []
    seen_paths = set()
    for name in names:
        key = _object_mesh_key(name)
        if not key:
            continue

        for override_path in OBJECT_STATIC_MESH_OVERRIDES.get(key, []):
            path_key = override_path.lower()
            if path_key in static_by_path and path_key not in seen_paths:
                paths.append(static_by_path[path_key])
                seen_paths.add(path_key)

        for entry in static_by_export.get(key, []):
            path_key = entry["path"].lower()
            if path_key not in seen_paths:
                paths.append(entry)
                seen_paths.add(path_key)
    return paths


def _resolve_object_race_table_mesh(race, object_race_mesh_map, static_by_path):
    key = _object_mesh_key(race.get("name", ""))
    if not key or key not in object_race_mesh_map:
        return []

    mapped = object_race_mesh_map[key]
    path_key = str(mapped.get("path", "")).lower()
    if not path_key or path_key not in static_by_path:
        return []

    entry = _clone_mesh_entry(static_by_path[path_key])
    entry["source"] = "object_race_table"
    entry["source_package"] = mapped.get("source_package", "")
    entry["source_export"] = mapped.get("source_export", "")
    entry["client_table_index"] = mapped.get("client_table_index", 0)
    return [entry]


def resolve_object_race_meshes(
    pawn,
    race,
    actor_row,
    static_by_path,
    static_by_export,
    object_race_mesh_map,
):
    """Resolve OBJECT race meshes from local static glTF exports.

    Spawn/display names get first chance so curated resource aliases such as
    "Carmellia Flower" can override generic race names. Otherwise the client
    object-race source table supplies the exact package/export pair.
    """
    spawn_meshes = _resolve_static_meshes_by_name(
        [
            pawn.get("playerDisplayName", ""),
            actor_row.get("spawn_name", "") if actor_row else "",
        ],
        static_by_path,
        static_by_export,
    )
    if spawn_meshes:
        return [_clone_mesh_entry(entry) for entry in spawn_meshes]

    table_meshes = _resolve_object_race_table_mesh(
        race, object_race_mesh_map, static_by_path
    )
    if table_meshes:
        return table_meshes

    race_meshes = _resolve_static_meshes_by_name(
        [race.get("name", "")],
        static_by_path,
        static_by_export,
    )
    return [_clone_mesh_entry(entry) for entry in race_meshes]


def _mesh_matches_gender(mesh_name, gender):
    """Check if a mesh name is compatible with the NPC's gender.

    Returns True if the mesh is gender-neutral or matches.
    Gender: 0=Male, 1=Female.
    """
    name_lower = mesh_name.lower()
    # Gendered mesh names contain _M_ or _F_ after the race prefix
    # e.g. human_M_clth_*, human_F_clth_*, npcHuman_M_char_*
    if gender == 1:  # Female NPC
        # Reject meshes explicitly marked male
        if "_m_clth_" in name_lower or "_m_char_" in name_lower:
            return False
    else:  # Male NPC
        # Reject meshes explicitly marked female
        if "_f_clth_" in name_lower or "_f_char_" in name_lower:
            return False
    return True


def _attachment_mesh_entry(entry, slot_label, set_id, part):
    return {
        "path": entry["path"],
        "export": entry["export"],
        "slot": slot_label,
        "package": entry["package"],
        "vertices": entry["vertices"],
        "faces": entry["faces"],
        "set_id": set_id,
        "attachment_slot": part["attachment_slot"],
        "inventory_slot": part["inventory_slot"],
        "package_index": part["package_index"],
        "attachment_index": part["attachment_index"],
    }


def resolve_attachment_sets(
    spawn_id, att_groups, att_sets, att_clth_map,
    manifest_by_export, prefix, gender, manifest_by_pkg
):
    """Resolve attachment meshes for an NPC, preserving attachment set groups.

    Returns (flat_meshes, attachment_sets). The flat mesh list is kept for
    compatibility with older consumers; attachment_sets is authoritative for
    deciding which outfit set to display.
    Uses direct decode from att_clth_map, filtering refs by NPC race + gender.
    _tool meshes (weapons) bypass race filtering; _clth/_char must match.
    """
    flat_results = []
    flat_seen_paths = set()
    result_sets = []

    set_ids = att_groups.get(spawn_id, [])
    for sid in set_ids:
        parts = att_sets.get(sid, [])
        set_meshes = []
        set_seen_paths = set()
        for part in parts:
            att_idx = part["attachment_index"]
            inv_slot = part["inventory_slot"]

            # Try direct decode
            refs = att_clth_map.get(str(att_idx), [])
            for ref in refs:
                mesh_name = ref["mesh"]
                # Filter by gender
                if not _mesh_matches_gender(mesh_name, gender):
                    continue
                # Filter by race (_tool meshes bypass this)
                if not _mesh_matches_race(mesh_name, prefix):
                    continue
                entry = manifest_by_export.get(mesh_name)
                if not entry or entry["path"] in set_seen_paths:
                    continue
                set_seen_paths.add(entry["path"])
                slot_label = _inv_slot_label(inv_slot)
                mesh_entry = _attachment_mesh_entry(entry, slot_label, sid, part)
                set_meshes.append(mesh_entry)
                if entry["path"] not in flat_seen_paths:
                    flat_seen_paths.add(entry["path"])
                    flat_results.append(dict(mesh_entry))

        if set_meshes:
            result_sets.append({
                "set_id": sid,
                "meshes": set_meshes,
            })

    return flat_results, result_sets


def resolve_attachment_meshes(
    spawn_id, att_groups, att_sets, att_clth_map,
    manifest_by_export, prefix, gender, manifest_by_pkg
):
    """Compatibility wrapper returning the flattened attachment mesh list."""
    flat_results, _attachment_sets = resolve_attachment_sets(
        spawn_id, att_groups, att_sets, att_clth_map,
        manifest_by_export, prefix, gender, manifest_by_pkg
    )
    return flat_results


def _inv_slot_label(inv_slot):
    """Map inventory_slot int to a human-readable slot label."""
    labels = {
        6: "clothing", 7: "cloak", 28: "weapon_primary",
        29: "weapon_secondary", 32: "weapon_2h", 33: "weapon_2h",
        34: "weapon_2h", 35: "weapon_2h", 36: "hand",
        42: "focus", 0: "clothing",
    }
    return labels.get(inv_slot, f"slot_{inv_slot}")


def load_race_prefix_map():
    """Load the authoritative race → UEM prefix mapping.

    Returns two dicts:
      race_prefix:  {raceID: prefix}  — clothing/primary mesh prefix
      race_body:    {raceID: body_prefix} — body fallback (only for NPC humanoids)
    """
    race_prefix = {}
    race_body = {}
    if not os.path.exists(RACE_PREFIX_PATH):
        print(f"Warning: race prefix map not found at {RACE_PREFIX_PATH}")
        return race_prefix, race_body
    with open(RACE_PREFIX_PATH) as f:
        data = json.load(f)
    for rid_str, entry in data.items():
        rid = int(rid_str)
        prefix = entry.get("prefix")
        if prefix:
            race_prefix[rid] = prefix
        body = entry.get("body_prefix")
        if body:
            race_body[rid] = body
    return race_prefix, race_body


def _find_char_entries(prefix, gender_char, manifest_by_pkg):
    """Look up _char manifest entries for a prefix, trying both genders."""
    for gc in [gender_char, "f" if gender_char == "m" else "m"]:
        pkg_key = f"uem_{prefix}_{gc}_char"
        entries = manifest_by_pkg.get(pkg_key, [])
        if entries:
            return entries
    return []


def _pick_default_mesh(entries, model_num=None):
    """Pick the default mesh(es) from a _char package.

    Naming conventions:
      Creatures:   _ALL_0_SKELETON  (standalone body, colour variants numbered 1,10,11)
      NPC humans:  _body_0 + _head_0 + _ears_0  (modular parts).
                   Also has visibility variants like _neck_0, _neckArmsHands_0, etc.
                   which are alternate body meshes shown when clothing hides areas.
                   Only load body_0 + head_0 + ears_0.
      OPT humans:  _ALL_0_C_0       (baked skin+hair combos)
      Player race: _head_0_C_0 + _ears_0  (head parts only)

    Returns a list of entries.
    """
    if not entries:
        return entries
    if len(entries) == 1:
        return entries

    # Creatures: prefer the server model variant, then the base skeleton.
    if model_num is not None and int(model_num) >= 0:
        model_token = f"_ALL_{int(model_num)}_SKELETON"
        for e in entries:
            if model_token in e["export"]:
                return [e]
    for e in entries:
        if "_ALL_0_SKELETON" in e["export"]:
            return [e]

    # OPT humanoids: prefer _ALL_0_C_0 (not hair)
    for e in entries:
        exp = e["export"]
        if "_ALL_0_C_0" in exp and "hair" not in exp.lower():
            return [e]

    # Humanoids: pick only body_0, head_0, ears_0 (the actual modular parts)
    # Skip visibility variants (neck_0, neckArmsHands_0, etc.)
    core_parts = []
    for e in entries:
        exp_lower = e["export"].lower()
        # Match _body_0, _head_0, _ears_0 but NOT _neck_0, _neckArms*
        if "_char_body_0" in exp_lower or "_char_head_0" in exp_lower or "_char_ears_0" in exp_lower:
            core_parts.append(e)
    if core_parts:
        return core_parts

    # Fallback: just return the first entry
    return [entries[0]]


def _find_clth_entries(prefix, gender_char, manifest_by_pkg):
    """Look up _clth manifest entries for a prefix, trying both genders."""
    for gc in [gender_char, "f" if gender_char == "m" else "m"]:
        pkg_key = f"uem_{prefix}_{gc}_clth"
        entries = manifest_by_pkg.get(pkg_key, [])
        if entries:
            return entries
    return []


def _pick_default_clth(entries):
    """Pick the best default clothing mesh from a _clth package.

    Prefers plainclothes_0 > diplomacy_0 > crafting_10 > first body outfit.
    Skips helmets, boots, gloves, tools (partial-body pieces).
    """
    if not entries:
        return None

    # Prefer full-body outfits (plainclothes, mage, robe, armor, diplomacy, crafting)
    body_keywords = [
        "plainclothes_0_C_0",
        "diplomacy_0_C_0",
        "crafting_10_C_0",
        "mage_0_C_0",
        "robeMage_0_C_0",
        "noble_0_C_0",
        "armorLeather_0_C_0",
    ]
    for keyword in body_keywords:
        for e in entries:
            if keyword in e["export"]:
                return e

    # Skip partial-body pieces (helms, boots, gloves, chainPants, etc.)
    skip_keywords = ["Helm", "boots", "gloves", "chainPants", "chainSleeves",
                     "tightsPants", "tools", "armorScale"]
    for e in entries:
        name = e["export"]
        if any(kw in name for kw in skip_keywords):
            continue
        return e

    # If ALL entries are partial pieces (e.g. dwarf _clth is all helmets),
    # return None — no suitable body outfit exists
    return None


def get_body_meshes(prefix, gender, manifest_by_pkg, body_prefix=None, model_num=None):
    """Get the base body glTF paths for a race prefix + gender.

    Looks for the _char package matching the prefix + gender.  If the prefix
    is clothing-only (no _char), uses body_prefix (from race_to_mesh_prefix.json)
    or falls back to BODY_FALLBACK.
    For creatures with only M variants (most animals), gender is ignored.

    For humanoid races (those with modular _char parts like head/body/ears),
    also includes a default _clth body outfit so NPCs aren't naked.
    """
    gender_char = "f" if gender == 1 else "m"
    results = []

    # Try primary prefix _char
    entries = _find_char_entries(prefix, gender_char, manifest_by_pkg)

    # If no _char package, use explicit body_prefix from mapping
    if not entries and body_prefix:
        entries = _find_char_entries(body_prefix, gender_char, manifest_by_pkg)

    # If still nothing, check legacy BODY_FALLBACK for clothing-only prefixes
    if not entries and prefix in BODY_FALLBACK:
        fallback = BODY_FALLBACK[prefix]
        fb_prefix = fallback.get("npc")
        if fb_prefix:
            entries = _find_char_entries(fb_prefix, gender_char, manifest_by_pkg)

    if entries:
        picked = _pick_default_mesh(entries, model_num)
        is_humanoid = not any("_ALL_0_SKELETON" in e["export"] for e in entries)

        # Composite humanoid assembly: NPC humanoids split body and head
        # across two packages — body_0 from npcHuman_*_char, head_0+ears_0
        # from the player race _char (e.g. dwarf, elf, orc).
        if is_humanoid:
            has_body = any("_char_body_0" in e["export"].lower() for e in picked)
            has_head = any("_char_head_0" in e["export"].lower() for e in picked)

            if has_body and not has_head:
                # Got NPC body, need head/ears from player race
                head_race = _HEAD_RACE.get(prefix) or _HEAD_RACE.get(body_prefix or "")
                if head_race:
                    head_entries = _find_char_entries(head_race, gender_char, manifest_by_pkg)
                    for he in head_entries:
                        exp = he["export"].lower()
                        if "_char_head_0" in exp or "_char_ears_0" in exp:
                            picked.append(he)

            elif has_head and not has_body:
                # Got player head/ears, need body from npcHuman
                body_entries = _find_char_entries("npchuman", gender_char, manifest_by_pkg)
                for be in body_entries:
                    if "_char_body_0" in be["export"].lower():
                        picked.insert(0, be)
                        break

        for e in picked:
            results.append({
                "path": e["path"],
                "export": e["export"],
                "slot": "body",
                "package": e["package"],
                "vertices": e["vertices"],
                "faces": e["faces"],
            })

        # For humanoid races, add a default _clth body outfit
        if is_humanoid:
            # Try the NPC-specific _clth first, then the body_prefix _clth
            clth_prefix = body_prefix or prefix
            clth_entries = _find_clth_entries(clth_prefix, gender_char, manifest_by_pkg)
            if not clth_entries:
                clth_entries = _find_clth_entries(prefix, gender_char, manifest_by_pkg)
            if not clth_entries and prefix in BODY_FALLBACK:
                fallback = BODY_FALLBACK[prefix]
                fb = fallback.get("npc")
                if fb:
                    clth_entries = _find_clth_entries(fb, gender_char, manifest_by_pkg)
            default_clth = _pick_default_clth(clth_entries)
            if default_clth:
                results.append({
                    "path": default_clth["path"],
                    "export": default_clth["export"],
                    "slot": "default_clothing",
                    "package": default_clth["package"],
                    "vertices": default_clth["vertices"],
                    "faces": default_clth["faces"],
                })

        return results

    # If nothing found, the creature might only have an _items package
    for gc in [gender_char, "f" if gender_char == "m" else "m", ""]:
        suffix = f"_{gc}_items" if gc else "_items"
        pkg_key = f"{prefix}{suffix}"
        entries = manifest_by_pkg.get(pkg_key, [])
        if entries:
            for e in entries:
                results.append({
                    "path": e["path"],
                    "export": e["export"],
                    "slot": "items",
                    "package": e["package"],
                    "vertices": e["vertices"],
                    "faces": e["faces"],
                })
            break

    return results


# ---------------------------------------------------------------------------
# Bone world-transform extraction for attachment positioning.
# ---------------------------------------------------------------------------

def _quat_mult(q1, q2):
    """Multiply two quaternions (x, y, z, w)."""
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return (
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2,
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
    )


def _quat_rotate(q, v):
    """Rotate vector v by quaternion q (x, y, z, w)."""
    vq = (v[0], v[1], v[2], 0.0)
    qc = (-q[0], -q[1], -q[2], q[3])
    return _quat_mult(_quat_mult(q, vq), qc)[:3]


# Bones we want world transforms for (attachment points)
_ATTACHMENT_BONES = {"r_hand", "l_hand", "r_hand_root", "l_hand_root"}


def _compute_bone_world_transforms(nodes):
    """Compute world-space position and rotation for each bone node.

    Processes nodes in index order (parents before children).
    Returns (world_pos, world_rot) lists parallel to nodes.
    """
    n = len(nodes)
    world_pos = [(0.0, 0.0, 0.0)] * n
    world_rot = [(0.0, 0.0, 0.0, 1.0)] * n
    for i, node in enumerate(nodes):
        if node.parent_index < 0:
            world_pos[i] = node.position
            world_rot[i] = node.rotation
        else:
            pi = node.parent_index
            rotated = _quat_rotate(world_rot[pi], node.position)
            world_pos[i] = (
                world_pos[pi][0] + rotated[0],
                world_pos[pi][1] + rotated[1],
                world_pos[pi][2] + rotated[2],
            )
            world_rot[i] = _quat_mult(world_rot[pi], node.rotation)
    return world_pos, world_rot


def _extract_bone_transforms(body_mesh_path):
    """Extract attachment bone world transforms from a body mesh's FXA data.

    Args:
        body_mesh_path: relative path like "uem_npchuman_m_char/npcHuman_M_char_body_0.gltf"

    Returns:
        dict mapping bone name to {"pos": [x,y,z], "rot": [x,y,z,w]}
        or empty dict if parsing fails.
    """
    # Derive the UEM package name from the mesh path
    # Path format: <package_name>/<export_name>.gltf
    parts = body_mesh_path.split("/")
    if len(parts) < 2:
        return {}
    pkg_name = parts[0]  # e.g. "uem_npchuman_m_char"
    export_name = parts[1].replace(".gltf", "")

    # Find the UEM file (case-insensitive)
    uem_filename = pkg_name + ".uem"
    uem_path = None
    if os.path.isdir(UEM_DIR):
        for fn in os.listdir(UEM_DIR):
            if fn.lower() == uem_filename.lower():
                uem_path = os.path.join(UEM_DIR, fn)
                break
    if not uem_path:
        return {}

    try:
        from ue2.package import UE2Package
        from vanguard_emfxmesh import parse_emfxmesh_export

        pkg = UE2Package(uem_path)
        target = None
        for exp in pkg.exports:
            if exp["object_name"] == export_name:
                target = exp
                break
        if not target:
            return {}

        data = pkg.get_export_data(target)
        mesh = parse_emfxmesh_export(data)
        if not mesh.nodes:
            return {}

        world_pos, world_rot = _compute_bone_world_transforms(mesh.nodes)

        result = {}
        for i, node in enumerate(mesh.nodes):
            if node.name.lower() in _ATTACHMENT_BONES:
                wp = world_pos[i]
                wr = world_rot[i]
                result[node.name] = {
                    "pos": [round(wp[0], 2), round(wp[1], 2), round(wp[2], 2)],
                    "rot": [round(wr[0], 5), round(wr[1], 5),
                            round(wr[2], 5), round(wr[3], 5)],
                }
        return result
    except Exception:
        return {}


def _get_body_bone_transforms(body_meshes):
    """Get attachment bone transforms from the body mesh in a mesh list.

    Looks for the _char_body_0 mesh among body_meshes and extracts
    bone world transforms from its FXA skeleton.
    """
    for m in body_meshes:
        if "_char_body_0" in m.get("export", "").lower():
            return _extract_bone_transforms(m["path"])
        # Creature _ALL_0_SKELETON also has bones
        if "_ALL_0_SKELETON" in m.get("export", ""):
            return _extract_bone_transforms(m["path"])
    return {}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", default=ASSETS_DIR, help="Path to the Vanguard EMU Assets directory")
    parser.add_argument("--out-dir", default=OUTPUT_DIR, help="Directory for NPC assembly JSON outputs")
    parser.add_argument("--manifest", default=MANIFEST_PATH, help="Character mesh manifest JSON")
    parser.add_argument("--attachment-clth", default=ATT_CLTH_PATH, help="Decoded item attachment mesh map JSON")
    parser.add_argument("--race-prefix", default=RACE_PREFIX_PATH, help="Race to mesh prefix JSON")
    parser.add_argument(
        "--npc-snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT_PATH,
        help="VGO world NPC snapshot JSON; falls back to MySQL if missing",
    )
    parser.add_argument("--static-mesh-manifest", default=STATIC_MESH_MANIFEST_PATH, help="Static mesh manifest JSON")
    parser.add_argument("--object-race-mesh-map", default=OBJECT_RACE_MESH_MAP_PATH, help="Object race static mesh map JSON")
    parser.add_argument("--db-host", default=DB_CONFIG["host"], help="VGO world MySQL host")
    parser.add_argument("--db-user", default=DB_CONFIG["user"], help="VGO world MySQL user")
    parser.add_argument("--db-password", default=DB_CONFIG["password"], help="VGO world MySQL password")
    parser.add_argument("--db-name", default=DB_CONFIG["database"], help="VGO world MySQL database")
    return parser.parse_args(argv)


def configure_paths(args):
    global OUTPUT_DIR
    global MANIFEST_PATH
    global ATT_CLTH_PATH
    global RACE_PREFIX_PATH
    global STATIC_MESH_MANIFEST_PATH
    global OBJECT_RACE_MESH_MAP_PATH
    global ASSETS_DIR
    global UEM_DIR

    OUTPUT_DIR = os.path.abspath(os.path.expanduser(args.out_dir))
    MANIFEST_PATH = os.path.abspath(os.path.expanduser(args.manifest))
    ATT_CLTH_PATH = os.path.abspath(os.path.expanduser(args.attachment_clth))
    RACE_PREFIX_PATH = os.path.abspath(os.path.expanduser(args.race_prefix))
    STATIC_MESH_MANIFEST_PATH = os.path.abspath(os.path.expanduser(args.static_mesh_manifest))
    OBJECT_RACE_MESH_MAP_PATH = os.path.abspath(os.path.expanduser(args.object_race_mesh_map))
    ASSETS_DIR = os.path.abspath(os.path.expanduser(args.assets))
    UEM_DIR = os.path.join(ASSETS_DIR, "Characters", "Meshes")


def load_npc_source(args):
    snapshot_path = Path(args.npc_snapshot).expanduser()
    if snapshot_path.exists():
        snapshot = load_snapshot(snapshot_path)
        print(f"Loaded NPC source snapshot: {snapshot_path}")
    else:
        snapshot = fetch_snapshot(db_config_from_args(args))
        print(
            "Loaded NPC source rows from vgo_world MySQL; "
            "run export-npc-snapshot to cache this as JSON."
        )

    grouped = group_snapshot(snapshot)
    races = grouped["races"]
    pawns = grouped["pawns"]
    actor_scales = grouped["actor_scales"]
    att_groups = grouped["att_groups"]
    att_sets = grouped["att_sets"]
    appearances = grouped["appearances"]
    print(f"Loaded {len(pawns)} pawns")
    print(f"Loaded {len(actor_scales)} pawn draw-scale rows")
    return races, pawns, actor_scales, att_groups, att_sets, appearances


def main(argv=None):
    args = parse_args(argv)
    configure_paths(args)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    manifest_by_pkg = load_manifest()
    att_clth_map = load_attachment_clth_map()
    static_by_path, static_by_export = load_static_mesh_manifest()
    object_race_mesh_map = load_object_race_mesh_map()

    # Build export-name → manifest entry lookup
    manifest_by_export = {}
    for entries in manifest_by_pkg.values():
        for e in entries:
            manifest_by_export[e["export"]] = e

    races, pawns, actor_scales, att_groups, att_sets, appearances = load_npc_source(args)

    # Load authoritative race→prefix mapping
    race_prefix_map, race_body_map = load_race_prefix_map()
    print(f"Race prefix map: {len(race_prefix_map)} prefixes, "
          f"{len(race_body_map)} body fallbacks")

    body_mesh_cache = {}
    bone_cache = {}
    for rid, prefix in race_prefix_map.items():
        body_prefix = race_body_map.get(rid)
        for gender in (0, 1):
            key = (rid, gender)
            body_mesh_cache[key] = get_body_meshes(
                prefix, gender, manifest_by_pkg, body_prefix, 0
            )
            bone_cache[key] = _get_body_bone_transforms(
                body_mesh_cache[key]
            )
        # Gender 2/3 = random spawn variants; map to male(0)/female(1)
        body_mesh_cache[(rid, 2)] = body_mesh_cache[(rid, 0)]
        body_mesh_cache[(rid, 3)] = body_mesh_cache[(rid, 0)]
        bone_cache[(rid, 2)] = bone_cache[(rid, 0)]
        bone_cache[(rid, 3)] = bone_cache[(rid, 0)]

    bones_found = sum(1 for v in bone_cache.values() if v)
    print(f"Bone transforms extracted for {bones_found} race/gender combos")

    pawn_body_mesh_cache = {}
    pawn_bone_cache = {}

    def _cached_pawn_body_meshes(race_id, gender, model_num):
        key = (race_id, gender, model_num)
        if key not in pawn_body_mesh_cache:
            prefix = race_prefix_map.get(race_id)
            body_prefix = race_body_map.get(race_id)
            pawn_body_mesh_cache[key] = (
                get_body_meshes(prefix, gender, manifest_by_pkg, body_prefix, model_num)
                if prefix else []
            )
            pawn_bone_cache[key] = _get_body_bone_transforms(pawn_body_mesh_cache[key])
        return pawn_body_mesh_cache[key], pawn_bone_cache[key]

    # Build assembly output
    assembly = []
    matched_count = 0
    unmatched_count = 0

    for spawn_id, pawn in sorted(pawns.items()):
        race = races.get(pawn["raceID"], {})
        race_cat = race.get("category", "")

        name = pawn["playerDisplayName"] or ""
        gender = pawn["gender"]
        race_name = race.get("name", f"unknown_{pawn['raceID']}")

        actor_scale = actor_scales.get(spawn_id, {})
        if race_cat == "OBJECT":
            meshes = resolve_object_race_meshes(
                pawn,
                race,
                actor_scale,
                static_by_path,
                static_by_export,
                object_race_mesh_map,
            )
            bones = {}
            att_meshes = []
            attachment_sets = []
        else:
            meshes, bones = _cached_pawn_body_meshes(
                pawn["raceID"], gender, pawn["modelNum"]
            )
            meshes = list(meshes)
            if not meshes:
                # Try male fallback (most creatures only have M variants)
                meshes, bones = _cached_pawn_body_meshes(
                    pawn["raceID"], 0, pawn["modelNum"]
                )
                meshes = list(meshes)

            # Resolve attachment meshes (clothing, weapons, etc.)
            prefix = race_prefix_map.get(pawn["raceID"])
            att_meshes, attachment_sets = resolve_attachment_sets(
                spawn_id, att_groups, att_sets, att_clth_map,
                manifest_by_export, prefix, gender, manifest_by_pkg
            )
            meshes.extend(att_meshes)

        entry = {
            "spawn_id": spawn_id,
            "name": name,
            "race": race_name,
            "race_id": pawn["raceID"],
            "race_category": race_cat,
            "gender": gender,
            "model_num": pawn["modelNum"],
            "mount_type": pawn["iMount"],
        }

        if race_cat == "OBJECT":
            mesh_key = "femaleMeshID" if gender == 1 else "maleMeshID"
            entry["race_mesh_id"] = race.get(mesh_key, race.get("maleMeshID", 0))

        if actor_scale:
            entry["spawn_name"] = actor_scale.get("spawn_name", "")
            entry["draw_scale_low"] = actor_scale["drawScale_low"]
            entry["draw_scale_high"] = actor_scale["drawScale_high"]

        if bones:
            entry["bone_transforms"] = bones

        if meshes:
            entry["meshes"] = meshes
            matched_count += 1
        else:
            entry["meshes"] = []
            unmatched_count += 1

        # Attachment slot count
        set_ids = att_groups.get(spawn_id, [])
        slot_count = 0
        for sid in set_ids:
            parts = att_sets.get(sid, [])
            slot_count += len(parts)
        entry["attachment_slots"] = slot_count
        entry["attachment_meshes"] = len(att_meshes)
        if attachment_sets:
            entry["attachment_sets"] = attachment_sets
            entry["attachment_set_count"] = len(attachment_sets)

        # Appearances
        if spawn_id in appearances:
            entry["appearances"] = [
                {
                    "id": a["appearance_id"],
                    "low": a["appearance_value_low"],
                    "high": a["appearance_value_high"],
                }
                for a in appearances[spawn_id]
            ]

        assembly.append(entry)

    # Write full output
    out_path = os.path.join(OUTPUT_DIR, "npc_assembly.json")
    with open(out_path, "w") as f:
        json.dump(assembly, f, separators=(",", ":"))
    print(f"Wrote {len(assembly)} NPCs to {out_path}")

    # Write viewer-friendly version (only NPCs with meshes)
    viewer_data = [e for e in assembly if e.get("meshes")]
    viewer_path = os.path.join(OUTPUT_DIR, "npc_assembly_viewer.json")
    with open(viewer_path, "w") as f:
        json.dump(viewer_data, f, separators=(",", ":"))
    print(f"Wrote {len(viewer_data)} NPCs (with meshes) to {viewer_path}")

    # Write race→prefix reference
    race_map = {}
    for rid, race in sorted(races.items()):
        prefix = race_prefix_map.get(rid)
        if prefix:
            for gender in (0, 1):
                meshes = body_mesh_cache.get((rid, gender), [])
                if meshes:
                    key = f"{race['name']}_{'F' if gender else 'M'}"
                    race_map[key] = {
                        "race_id": rid,
                        "category": race["category"],
                        "prefix": prefix,
                        "mesh_count": len(meshes),
                    }
    race_map_path = os.path.join(OUTPUT_DIR, "race_mesh_map.json")
    with open(race_map_path, "w") as f:
        json.dump(race_map, f, indent=2)
    print(f"Wrote {len(race_map)} race→mesh mappings to {race_map_path}")

    # Stats
    has_meshes = sum(1 for e in assembly if e.get("meshes"))
    has_appearances = sum(1 for e in assembly if e.get("appearances"))
    has_att_meshes = sum(1 for e in assembly if e.get("attachment_meshes", 0) > 0)
    total_att_meshes = sum(e.get("attachment_meshes", 0) for e in assembly)
    has_att_sets = sum(1 for e in assembly if e.get("attachment_sets"))
    total_att_sets = sum(len(e.get("attachment_sets", [])) for e in assembly)
    print(f"\nStats:")
    print(f"  Total pawns: {len(assembly)}")
    print(f"  With mesh matches: {has_meshes} ({100*has_meshes/len(assembly):.1f}%)")
    print(f"  Without meshes: {unmatched_count} (likely invisible/scripted)")
    object_entries = sum(1 for e in assembly if e.get("race_category") == "OBJECT")
    object_meshes = sum(
        1 for e in assembly
        if e.get("race_category") == "OBJECT" and e.get("meshes")
    )
    print(f"  Object race pawns: {object_entries} ({object_meshes} with meshes)")
    print(f"  With appearances: {has_appearances}")
    print(f"  Races matched: {len(race_prefix_map)}/{len(races)}")
    print(f"  With attachment meshes: {has_att_meshes} ({total_att_meshes} total)")
    print(f"  With attachment sets: {has_att_sets} ({total_att_sets} total)")
    print(f"  Attachment clth map entries: {len(att_clth_map)}")


if __name__ == "__main__":
    main()
