#!/usr/bin/env python3
"""
Split sgo_raw.jsonl into per-category JSON files of typed actor records.

Categories:
  lights    — Light, DynamicLight, Sunlight
  portals   — Portal
  movers    — Mover, ClientMover
  triggers  — UseTrigger, *Trigger
  emitters  — SpriteEmitter, BeamEmitter, MeshEmitter, LightEmitter,
              SparkEmitter, Emitter, *fx / *Fx (all particle classes)
  physics   — KActor, KarmaParams
  misc      — everything else except StaticMeshActor + CompoundObject
              (which are handled by parse_sgo_prefabs.py)

Each category file is a dict keyed by prefab name:
    { prefab_name: [ { class, name, props: {...} }, ... ] }

``props`` is a flat dict {property_name: decoded_value}. The raw_hex
fallback is preserved in sgo_raw.jsonl; this splitter prefers typed values.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict


PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW_DEFAULT = os.path.join(PROJ, "output/data/sgo_raw.jsonl")
OUT_DIR_DEFAULT = os.path.join(PROJ, "output/data/sgo_by_class")


LIGHT_CLASSES = {"Light", "DynamicLight", "Sunlight"}
PORTAL_CLASSES = {"Portal"}
MOVER_CLASSES = {"Mover", "ClientMover"}
PHYSICS_CLASSES = {"KActor", "KarmaParams"}
EMITTER_BASE = {"SpriteEmitter", "BeamEmitter", "MeshEmitter",
                "LightEmitter", "SparkEmitter", "Emitter"}
SKIP_CLASSES = {"StaticMeshActor", "CompoundObject"}


def categorise(cls: str) -> str | None:
    if cls in SKIP_CLASSES:
        return None
    if cls in LIGHT_CLASSES:
        return "lights"
    if cls in PORTAL_CLASSES:
        return "portals"
    if cls in MOVER_CLASSES:
        return "movers"
    if cls in PHYSICS_CLASSES:
        return "physics"
    if cls in EMITTER_BASE:
        return "emitters"
    # *fx / *Fx classes are instance particle actors.
    if cls.endswith("fx") or cls.endswith("Fx"):
        return "emitters"
    # Vanguard particle prefab-instance prefixes: FX*, Particle*, flame*.
    # Matches e.g. FXmystical_PantheonHoloRunes003x, ParticleSwordCold,
    # flame01 — all confirmed particle/FX actors in the data.
    cls_low = cls.lower()
    if cls_low.startswith(("fx", "particle", "flame")):
        return "emitters"
    if cls.endswith("Trigger"):
        return "triggers"
    return "misc"


def extract_prefab_name(names: list[str], trailer_entry: dict | None) -> str:
    """The canonical prefab name is the one ending in exportBinaryPrefab."""
    for n in names:
        if "exportBinaryPrefab" in n:
            return n.split("exportBinaryPrefab")[0]
    if trailer_entry:
        # fallback: strip ".ubc" suffix
        nm = trailer_entry.get("name", "")
        return nm.split("_binaryprefab.ubc")[0] if "_binaryprefab.ubc" in nm else nm
    return ""


def props_to_dict(props: list[dict]) -> dict:
    """Collapse the ordered prop list into a flat dict name -> value.

    If a property appears multiple times across override/default blocks, the
    first non-None value wins for compatibility and every additional occurrence
    is preserved under ``<name>__extra``. Object-property package details are
    preserved alongside the compatible value as ``<name>__object_ref``.
    """
    out: dict = {}
    extras: dict = defaultdict(list)
    ref_extras: dict = defaultdict(list)
    for p in props:
        name = p["name"]
        val = p["value"]
        object_ref = p.get("object_ref")
        ref_key = f"{name}__object_ref"
        if name not in out:
            out[name] = val
            if object_ref is not None:
                out[ref_key] = object_ref
        elif out[name] is None and val is not None:
            if name in out:
                extras[name].append(out[name])
            if ref_key in out:
                ref_extras[name].append(out[ref_key])
            out[name] = val
            if object_ref is not None:
                out[ref_key] = object_ref
            elif ref_key in out:
                del out[ref_key]
        else:
            extras[name].append(val)
            if object_ref is not None:
                ref_extras[name].append(object_ref)
    for k, v in extras.items():
        out[f"{k}__extra"] = v
    for k, v in ref_extras.items():
        out[f"{k}__object_ref__extra"] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=RAW_DEFAULT)
    ap.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    args = ap.parse_args()

    if not os.path.isfile(args.raw):
        print(f"ERROR: raw JSONL not found: {args.raw}", file=sys.stderr)
        print("Run scripts/extractors/dump_sgo_raw.py first.", file=sys.stderr)
        return 1
    os.makedirs(args.out_dir, exist_ok=True)

    buckets: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    class_totals: dict[str, int] = defaultdict(int)

    t0 = time.time()
    lines = 0
    exports_total = 0
    exports_emitted = 0
    with open(args.raw, "r", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            prefab = extract_prefab_name(rec.get("names", []), rec.get("trailer_entry"))
            for exp in rec.get("exports", []):
                exports_total += 1
                cls = exp.get("class") or "<none>"
                cat = categorise(cls)
                class_totals[cls] += 1
                if cat is None:
                    continue
                buckets[cat][prefab].append({
                    "class": cls,
                    "name": exp.get("name"),
                    "props": props_to_dict(exp.get("props", [])),
                })
                exports_emitted += 1
            lines += 1
            if lines % 2000 == 0:
                print(f"  processed {lines:,} packages "
                      f"({time.time()-t0:.1f}s)")

    for cat, prefabs in buckets.items():
        out_path = os.path.join(args.out_dir, f"sgo_{cat}.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(prefabs, fh, ensure_ascii=False,
                      separators=(",", ":"))
        n_actors = sum(len(v) for v in prefabs.values())
        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        print(f"  wrote {cat:>9s}: {n_actors:>7,} actors across "
              f"{len(prefabs):>5,} prefabs  ({size_mb:.1f} MB)  "
              f"{out_path}")

    # Summary audit
    print()
    print(f"packages scanned: {lines:,}")
    print(f"exports seen:     {exports_total:,}")
    print(f"exports emitted:  {exports_emitted:,}  "
          f"(skipped SMA+CompoundObject: "
          f"{exports_total - exports_emitted:,})")

    # Cross-check: emitted + skipped should equal exports_total
    assert exports_total == exports_emitted + (
        class_totals.get("StaticMeshActor", 0)
        + class_totals.get("CompoundObject", 0)
    ), "bucket accounting mismatch"
    print("\ncategory totals:")
    for cat, prefabs in sorted(buckets.items()):
        print(f"  {cat:<10s}  {sum(len(v) for v in prefabs.values()):>7,} actors")
    print(f"\nTook {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
