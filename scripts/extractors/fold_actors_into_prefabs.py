#!/usr/bin/env python3
"""
Fold non-mesh actors (portals, movers, triggers, emitters, physics) from
output/data/sgo_by_class/*.json into output/data/sgo_prefabs.json under
a new ``extras`` key per prefab.

Lights are NOT folded in: ``parse_sgo_prefabs.py`` already emits them as
flat {type: 'light', brightness, color, radius, ...} entries in each
prefab's main actor list, so duplicating here would bloat the file.

Output shape after merging (per prefab):

    "<PrefabName>": {
        "actors": [ ...existing meshes+lights... ],
        "extras": {
            "portals":  [ {class, name, props}, ... ],
            "movers":   [ ... ],
            "triggers": [ ... ],
            "emitters": [ ... ],
            "physics":  [ ... ]
        }
    }

Prefabs with no extras get ``extras: {}`` so the viewer can rely on the
key always existing. The original flat list form is replaced by the new
shape, so this rewrites sgo_prefabs.json in place unless --out is given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict

from vanguard_assets import config

PROJ = str(config.PROJECT_ROOT)
PREFABS_DEFAULT = os.path.join(PROJ, "output/data/sgo_prefabs.json")
PREFAB_INDEX_DEFAULT = os.path.join(PROJ, "output/data/sgo_prefab_index.json")
BY_CLASS_DIR = os.path.join(PROJ, "output/data/sgo_by_class")

# Lights skipped on purpose — already in the main actors list.
FOLD_CATEGORIES = ("portals", "movers", "triggers", "emitters", "physics", "misc")


def _write_prefab_index(prefabs: dict, out_path: str) -> int:
    index = {}
    for name, entry in prefabs.items():
        actors = entry.get("actors", []) if isinstance(entry, dict) else entry
        index[name] = sum(
            1
            for actor in actors
            if isinstance(actor, dict) and "mesh" in actor
        )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"))
    os.replace(tmp_path, out_path)
    return len(index)


def load_category(name: str) -> dict:
    path = os.path.join(BY_CLASS_DIR, f"sgo_{name}.json")
    if not os.path.isfile(path):
        print(f"  warn: missing {path}", file=sys.stderr)
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefabs", default=PREFABS_DEFAULT)
    ap.add_argument("--out", default=None,
                    help="Output path (default: rewrite --prefabs in place)")
    args = ap.parse_args()

    t0 = time.time()
    with open(args.prefabs, "r", encoding="utf-8") as fh:
        prefabs = json.load(fh)
    print(f"loaded {len(prefabs):,} prefabs from {args.prefabs} "
          f"({time.time()-t0:.1f}s)")

    # Load every category once, building a flat {prefab_name: {cat: [actors]}}.
    extras_by_prefab: dict[str, dict[str, list]] = defaultdict(
        lambda: {c: [] for c in FOLD_CATEGORIES})
    category_totals = {}
    for cat in FOLD_CATEGORIES:
        data = load_category(cat)
        n_actors = 0
        for prefab_name, actors in data.items():
            extras_by_prefab[prefab_name][cat].extend(actors)
            n_actors += len(actors)
        category_totals[cat] = n_actors
        print(f"  {cat:>9s}: {n_actors:>7,} actors across {len(data):>5,} prefabs")

    # Sanity: make sure every prefab key coming from the category files
    # exists in sgo_prefabs.json. Prefabs with only non-mesh/non-light
    # actors are not emitted by parse_sgo_prefabs.py, so we add them here
    # with an empty ``actors`` list — otherwise we'd drop data.
    missing = [p for p in extras_by_prefab if p not in prefabs]
    if missing:
        print(f"  adding {len(missing)} prefabs that only have non-mesh actors"
              f" (first few: {missing[:3]})")
        for name in missing:
            prefabs[name] = {"actors": []}

    # Transform sgo_prefabs.json: wrap existing actor list as ``actors`` and
    # attach ``extras``.
    merged: dict[str, dict] = {}
    prefabs_with_extras = 0
    for name, prefab_entry in prefabs.items():
        if isinstance(prefab_entry, dict):
            actors = prefab_entry.get("actors", [])
            merged_entry = {
                k: v
                for k, v in prefab_entry.items()
                if k not in ("actors", "extras")
            }
            existing_extras = prefab_entry.get("extras", {})
        else:
            actors = prefab_entry
            merged_entry = {}
            existing_extras = {}

        extras = extras_by_prefab.get(name, {c: [] for c in FOLD_CATEGORIES})
        if isinstance(existing_extras, dict):
            for cat, existing_actors in existing_extras.items():
                if existing_actors and not extras.get(cat):
                    extras[cat] = existing_actors
        # Strip empty categories for compactness.
        extras_clean = {k: v for k, v in extras.items() if v}
        if extras_clean:
            prefabs_with_extras += 1
        merged_entry["actors"] = actors
        merged_entry["extras"] = extras_clean
        merged[name] = merged_entry

    out_path = args.out or args.prefabs
    # Write atomically: tmp + rename so a crash can't corrupt the file.
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, out_path)

    index_path = (
        PREFAB_INDEX_DEFAULT
        if os.path.abspath(out_path) == os.path.abspath(PREFABS_DEFAULT)
        else out_path + ".index.json"
    )
    index_count = _write_prefab_index(merged, index_path)

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print()
    print(f"wrote {out_path}  ({size_mb:.1f} MB)")
    print(f"wrote {index_path}  ({index_count:,} prefabs)")
    print(f"  prefabs total:        {len(merged):,}")
    print(f"  prefabs with extras:  {prefabs_with_extras:,}")
    print(f"  categories folded in: {', '.join(FOLD_CATEGORIES)}")
    print(f"  elapsed: {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
