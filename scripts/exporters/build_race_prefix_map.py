#!/usr/bin/env python3
"""
Build the authoritative race → UEM mesh prefix mapping.

Scans UEM filenames to discover available mesh prefixes, then matches
each database race to its correct prefix using name transforms and
pattern matching.

Produces:
  output/data/race_to_mesh_prefix.json

Each entry maps a raceID to:
  - prefix:      UEM filename prefix for clothing/primary meshes
  - body_prefix: body fallback (only for NPC humanoids that use npcHuman body)
  - source:      how the mapping was derived
  - category:    race category from DB (NPC, PLAYER, MOUNT, OBJECT, OPT)
"""

import argparse
import os
import sys
import glob
import re
import json
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, SCRIPT_DIR)

from vgo_world_npc_snapshot import (  # noqa: E402
    DEFAULT_DB_CONFIG,
    DEFAULT_SNAPSHOT_PATH,
    db_config_from_args,
    fetch_snapshot,
    load_snapshot,
    race_spawn_counts,
)

OUTPUT_PATH = os.path.join(ROOT_DIR, "output", "data", "race_to_mesh_prefix.json")
ACTOR_RACE_VISUAL_MAP_PATH = os.path.join(
    ROOT_DIR, "output", "data", "actor_race_visual_map.json"
)
CHARACTER_MANIFEST_PATH = os.path.join(
    ROOT_DIR, "output", "meshes", "characters", "manifest.json"
)

ASSETS = os.environ.get(
    "VANGUARD_ASSETS",
    os.environ.get("VANGUARD_ASSETS_PATH", os.path.expanduser("~/Downloads/Vanguard EMU/Assets")),
)
MESH_DIR = os.path.join(ASSETS, "Characters", "Meshes")

DB_CONFIG = dict(DEFAULT_DB_CONFIG)

# ---------------------------------------------------------------------------
# Name transforms: race name → UEM prefix for non-obvious mappings that are
# not present in the extracted client actor table.
# ---------------------------------------------------------------------------
NAME_TRANSFORMS = {
    # Compound names with swapped/different conventions
    'AntGiant': 'ant',
    'BoarWild': 'boar',
    'WormGiant': 'worm',
    'WolfZombie': 'wolfundead',
    'BearZombie': 'bearundead',
    'ZombieDwarf': 'zombie',
    'ZombieGnome': 'zombie',
    # Elementals
    'EarthElemental': 'elementalearth',
    'FireElemental': 'elementalfire',
    'StormElemental': 'elementalstorm',
    'MistElemental': 'elementalmist',
    'WispElemental': 'elementalwisp',
    'BlockingWispElemental': 'elementalwisp',
    'BlockingIODWisp': 'iodwisp',
    # Golems
    'MetalGolem': 'golemmetal',
    'RockGolem': 'golemrock',
    'ElementalGolem': 'golemelemental',
    'FleshGolem': 'golemflesh',
    # Giants
    'SandGiant': 'giantsand',
    'LesserGiant': 'halfgiant',
    # Cats
    'Lion': 'catlion',
    'Leopard': 'catleopard',
    'Tiger': 'cattiger',
    'Panther': 'catpanther',
    'Cougar': 'catcougar',
    # Birds
    'Raven': 'birdraven',
    'Seagull': 'birdseagull',
    # Raptors
    'Eagle': 'raptoreagle',
    'Hawk': 'raptorhawk',
    'Falcon': 'raptorfalcon',
    'Roc': 'raptorroc',
    'Vulture': 'raptorvulture',
    'Phoenix': 'raptorphoenix',
    # Dragons
    'DragonBlack': 'dragon_black',
    'DragonSwamp': 'dragon_swamp',
    # Misc
    'Angel': 'angelpantheon',
    'Cannon': 'particlecannon',
    'WarehouseGuardian': 'guardianwarehouse',
    'WillowispFlyer': 'willowisp',
    'ShamblingMoundDesert': 'shamblingmound',
    'ForgeQalia': 'forgeqalian',
    'SmelterQalia': 'smelterqalian',
    'TreeAmada': 'treeash',
    'MountainGoat': 'goatmountain',
    'BearSkeleton': 'horseskeleton',  # no bear skeleton mesh exists
    # Player humanoid races that use shortened/different prefixes
    'Kojani': 'kojan',
    'KojanBarbarian': 'kojan',
    'Qaliathari': 'qalian',
    'Kurashasa': 'kura',
    'Varanjar': 'varanjar',
    'Varanthari': 'varanthari',
}

# NPC humanoid races: clothing prefix + npcHuman body
NPC_HUMANOID_CLOTHING = {
    'NPCThestran': 'thestran',
    'NPCKojanHuman': 'kojan',
    'NPCMordebi': 'mordebi',
    'NPCQaliathari': 'qalian',
    'NPCHighElf': 'highelf',
    'NPCDarkElf': 'darkelf',
    'NPCWoodElf': 'woodelf',
    'NPCVaranjar': 'varanjar',
    'NPCVaranthari': 'varanthari',
    'NPCKurashasa': 'kura',
    'NPCLesserGiant': 'halfgiant',
    'OPTThestran': 'thestran',
    'OPTKojanHuman': 'kojan',
    'OPTMordebi': 'mordebi',
    'OPTQaliathari': 'qalian',
    'OPTHighElf': 'highelf',
    'OPTDarkElf': 'darkelf',
    'OPTWoodElf': 'woodelf',
    'OPTVaranjar': 'varanjar',
    'OPTVaranthari': 'varanthari',
    'OPTKurashasa': 'kura',
    'OPTLesserGiant': 'halfgiant',
}
NPC_HUMANOID_BODY = 'npchuman'

# Player humanoid races with body fallbacks
PLAYER_HUMANOID_BODY = {
    'Kojani': 'human',
    'KojanBarbarian': 'human',
    'Qaliathari': 'human',
    'Kurashasa': 'kura',
    'Varanjar': 'barbarian',
    'Varanthari': 'barbarian',
    'LesserGiant': 'halfgiant',
}

# Client actor prefixes that intentionally resolve through a different exported
# body package when runtime actor visuals look up non-playable race meshes.
CLIENT_BODY_FALLBACK = {
    "npcdwarf": "dwarf",
    "npcgnome": "gnome",
    "npcgoblin": "goblin",
    "npchalfelf": "halfelf",
    "npchalfling": "halfling",
    "npcorc": "orc",
    "npcraki": "raki",
    "npcvulmane": "vulmane",
    "varanjar": "barbarian",
    "varanthari": "barbarian",
    "lich": "skeleton",
    "wight": "skeleton",
    "wraith": "skeleton",
    "undead": "zombie",
    "deathknight": "skeleton",
    "mummy": "zombie",
}

NO_MESH_RACES = {'InvisibleMan', 'IODInvisibleMan', 'Trap', 'PlagueBearer'}


def _normalise_prefix(value):
    return str(value).strip().lower()


def _discover_uem_prefixes(uem_files):
    prefix_set = set()
    for f in uem_files:
        m = re.match(r'^uem_(.+?)_(m|f)_(char|clth|hair|tool)\.uem$', f)
        if m:
            prefix_set.add(m.group(1))
    return prefix_set


def _load_exported_prefixes():
    if not os.path.exists(CHARACTER_MANIFEST_PATH):
        return set()

    with open(CHARACTER_MANIFEST_PATH, encoding="utf-8") as f:
        entries = json.load(f)

    prefixes = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        package = str(entry.get("package", "")).lower()
        m = re.match(r'^uem_(.+?)_(m|f)_(char|clth|hair|tool)$', package)
        if m:
            prefixes.add(m.group(1))
    return prefixes


def _load_actor_race_visual_map():
    try:
        from export_actor_race_visual_map import write_actor_race_visual_map

        payload = write_actor_race_visual_map()
    except Exception as exc:
        if not os.path.exists(ACTOR_RACE_VISUAL_MAP_PATH):
            print(f"Warning: actor race visual map unavailable: {exc}")
            return {}
        print(f"Warning: using existing actor race visual map after rebuild failed: {exc}")
        with open(ACTOR_RACE_VISUAL_MAP_PATH, encoding="utf-8") as f:
            payload = json.load(f)

    races = payload.get("races", {}) if isinstance(payload, dict) else {}
    by_name = {}
    for race_name, entry in races.items():
        if not isinstance(entry, dict):
            continue
        by_name[str(race_name)] = entry

    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    if summary:
        print(
            "Actor race visual map: "
            f"{summary.get('matched_exported_prefix_count', 0)}/"
            f"{summary.get('race_count', len(by_name))} exported prefixes"
        )
    return by_name


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", default=ASSETS, help="Path to the Vanguard EMU Assets directory")
    parser.add_argument("--out", default=OUTPUT_PATH, help="Output race_to_mesh_prefix.json path")
    parser.add_argument(
        "--npc-snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT_PATH,
        help="VGO world NPC snapshot JSON; falls back to MySQL if missing",
    )
    parser.add_argument("--actor-race-visual-map", default=ACTOR_RACE_VISUAL_MAP_PATH)
    parser.add_argument("--character-manifest", default=CHARACTER_MANIFEST_PATH)
    parser.add_argument("--db-host", default=DB_CONFIG["host"], help="VGO world MySQL host")
    parser.add_argument("--db-user", default=DB_CONFIG["user"], help="VGO world MySQL user")
    parser.add_argument("--db-password", default=DB_CONFIG["password"], help="VGO world MySQL password")
    parser.add_argument("--db-name", default=DB_CONFIG["database"], help="VGO world MySQL database")
    return parser.parse_args(argv)


def configure_paths(args):
    global ASSETS
    global MESH_DIR
    global OUTPUT_PATH
    global ACTOR_RACE_VISUAL_MAP_PATH
    global CHARACTER_MANIFEST_PATH
    global DB_CONFIG

    ASSETS = os.path.abspath(os.path.expanduser(args.assets))
    MESH_DIR = os.path.join(ASSETS, "Characters", "Meshes")
    OUTPUT_PATH = os.path.abspath(os.path.expanduser(args.out))
    ACTOR_RACE_VISUAL_MAP_PATH = os.path.abspath(os.path.expanduser(args.actor_race_visual_map))
    CHARACTER_MANIFEST_PATH = os.path.abspath(os.path.expanduser(args.character_manifest))
    DB_CONFIG = {
        "host": args.db_host,
        "user": args.db_user,
        "password": args.db_password,
        "database": args.db_name,
    }


def load_race_source(args):
    snapshot_path = Path(args.npc_snapshot).expanduser()
    if snapshot_path.exists():
        snapshot = load_snapshot(snapshot_path)
        print(f"Loaded race source from NPC snapshot: {snapshot_path}")
    else:
        snapshot = fetch_snapshot(db_config_from_args(args))
        print(
            "Loaded race source from vgo_world MySQL; "
            "run export-npc-snapshot to cache this as JSON."
        )

    tables = snapshot.get("tables", {})
    races = sorted(tables.get("races", []), key=lambda row: int(row["id"]))
    return races, race_spawn_counts(snapshot)


def main(argv=None):
    args = parse_args(argv)
    configure_paths(args)

    # Discover all UEM prefixes from filenames
    uem_files = [os.path.basename(f).lower()
                 for f in glob.glob(os.path.join(MESH_DIR, "*.uem"))]
    raw_prefix_set = _discover_uem_prefixes(uem_files)
    exported_prefix_set = _load_exported_prefixes()
    prefix_set = exported_prefix_set or raw_prefix_set

    print(f"Discovered {len(raw_prefix_set)} raw UEM prefixes from {len(uem_files)} files")
    if exported_prefix_set:
        print(f"Discovered {len(exported_prefix_set)} exported character prefixes")
    actor_visual_map = _load_actor_race_visual_map()

    races, spawn_counts = load_race_source(args)

    mapping = {}

    for race in races:
        rid = race['id']
        rname = race['name']
        cat = race['category']
        entry = {'race_id': rid, 'race_name': rname, 'category': cat}

        if rname in NO_MESH_RACES:
            entry['prefix'] = None
            entry['source'] = 'no_mesh'
            mapping[rid] = entry
            continue

        # NPC humanoid races
        if rname in NPC_HUMANOID_CLOTHING:
            entry['prefix'] = NPC_HUMANOID_CLOTHING[rname]
            entry['body_prefix'] = NPC_HUMANOID_BODY
            entry['source'] = 'npc_humanoid'
            mapping[rid] = entry
            continue

        # Original client actor table: race name -> visual UEM prefix.
        # Prefer this over local name heuristics when the referenced prefix has
        # exported glTF data. If the client references a prefix we do not have a
        # renderable mesh for, keep that evidence in the map and do not guess a
        # different body. NPC humanoids above are intentionally handled first:
        # the client table gives their base actor body, while the viewer needs
        # the race-specific clothing/head prefix plus npcHuman body fallback.
        actor_visual = actor_visual_map.get(rname)
        if actor_visual:
            client_prefix = _normalise_prefix(actor_visual.get("normalized_prefix", ""))
            client_visual_prefix = actor_visual.get("visual_prefix", "")
            if client_prefix and client_prefix in prefix_set:
                entry['prefix'] = client_prefix
                entry['source'] = 'client_actor_table'
                entry['client_visual_prefix'] = client_visual_prefix
                if actor_visual.get("data_symbol"):
                    entry['client_data_symbol'] = actor_visual.get("data_symbol")
                mapping[rid] = entry
                continue
            if (
                client_prefix
                and actor_visual.get("has_raw_uem_prefix")
                and client_prefix in CLIENT_BODY_FALLBACK
            ):
                entry['prefix'] = client_prefix
                entry['source'] = 'client_actor_table_body_fallback'
                entry['client_visual_prefix'] = client_visual_prefix
                body_prefix = CLIENT_BODY_FALLBACK[client_prefix]
                if body_prefix:
                    entry['body_prefix'] = body_prefix
                mapping[rid] = entry
                continue
            if client_prefix:
                entry['prefix'] = None
                entry['source'] = 'client_actor_table_missing_local_mesh'
                entry['client_visual_prefix'] = client_visual_prefix
                entry['client_normalized_prefix'] = client_prefix
                entry['client_has_raw_uem_prefix'] = bool(
                    actor_visual.get("has_raw_uem_prefix")
                )
                entry['client_has_exported_prefix'] = bool(
                    actor_visual.get("has_exported_prefix")
                )
                mapping[rid] = entry
                continue

        # Explicit transforms
        if rname in NAME_TRANSFORMS:
            pfx = NAME_TRANSFORMS[rname]
            if pfx in prefix_set:
                entry['prefix'] = pfx
                entry['source'] = 'name_transform'
                if rname in PLAYER_HUMANOID_BODY:
                    body = PLAYER_HUMANOID_BODY[rname]
                    if body != pfx:
                        entry['body_prefix'] = body
                mapping[rid] = entry
                continue

        # Exact lowercase
        rname_lower = rname.lower()
        if rname_lower in prefix_set:
            entry['prefix'] = rname_lower
            entry['source'] = 'exact'
            mapping[rid] = entry
            continue

        # Strip NPC/OPT prefix
        stripped = rname
        for pfx_str in ['NPC', 'OPT', 'Player']:
            if rname.startswith(pfx_str) and len(rname) > len(pfx_str):
                stripped = rname[len(pfx_str):]
        stripped_lower = stripped.lower()

        for variant in [f"npc{stripped_lower}", f"optimized{stripped_lower}", stripped_lower]:
            if variant in prefix_set:
                entry['prefix'] = variant
                entry['source'] = 'prefix_strip'
                mapping[rid] = entry
                break

        if rid in mapping:
            continue

        # Keep server-declared playable races visible in generated evidence even
        # when no local mesh/export match has been reverse engineered yet.
        if cat == 'PLAYER':
            entry['prefix'] = None
            entry['source'] = 'unmatched_player_no_local_mesh'
            mapping[rid] = entry
            continue

        # Check spawns for unmapped non-object races
        cnt = spawn_counts.get(rid, 0)
        if cnt > 0 and cat not in ('OBJECT',):
            entry['prefix'] = None
            entry['source'] = 'unmatched'
            entry['spawns'] = cnt
            mapping[rid] = entry

    # Validate prefixes exist
    bad = []
    for rid, e in mapping.items():
        prefix = e.get('prefix')
        if not prefix or prefix in prefix_set:
            continue
        if (
            e.get('source') == 'client_actor_table_body_fallback'
            and e.get('body_prefix') in prefix_set
        ):
            continue
        bad.append((rid, e['race_name'], prefix))
    if bad:
        print(f"WARNING: {len(bad)} non-existent prefixes:")
        for rid, rname, pfx in bad:
            print(f"  {rid} {rname} → '{pfx}'")

    # Stats
    sources = {}
    for v in mapping.values():
        sources[v.get('source', '?')] = sources.get(v.get('source', '?'), 0) + 1
    print(f"\nTotal: {len(mapping)} race mappings")
    for s, c in sorted(sources.items(), key=lambda x: -x[1]):
        print(f"  {s}: {c}")

    # Spawn coverage
    total_spawns = 0
    covered_spawns = 0
    for race_id, count in spawn_counts.items():
        total_spawns += count
        if race_id in mapping:
            e = mapping[race_id]
            if e.get('prefix') or e.get('body_prefix'):
                covered_spawns += count
    pct = 100 * covered_spawns / total_spawns if total_spawns else 0
    print(f"Spawn coverage: {covered_spawns}/{total_spawns} ({pct:.1f}%)")

    # Save
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(mapping, f, indent=2, default=str)
    print(f"Saved {len(mapping)} entries to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
