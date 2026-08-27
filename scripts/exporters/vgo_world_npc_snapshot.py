"""Load the committed vgo_world NPC subset needed for NPC assembly.

The snapshot intentionally contains only the race, pawn, scale, attachment,
and appearance fields consumed by export_npc_assembly.py and
build_race_prefix_map.py.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any


DEFAULT_SNAPSHOT_PATH = Path(
    str(files("client_tables").joinpath("vgo_world_npc_snapshot.json"))
)


def load_snapshot(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        snapshot = json.load(handle)
    if not isinstance(snapshot, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    tables = snapshot.get("tables")
    if not isinstance(tables, dict):
        raise ValueError(f"{path} does not contain a tables object")
    return snapshot


def _int_key(value: Any) -> int:
    return int(value or 0)


def group_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    tables = snapshot.get("tables", {})
    races = {_int_key(row["id"]): row for row in tables.get("races", [])}
    pawns = {
        _int_key(row["spawn_id_fk"]): row
        for row in tables.get("unreal_pawn", [])
    }
    actor_scales = {
        _int_key(row["spawn_id"]): row
        for row in tables.get("unreal_actor", [])
    }

    att_groups: dict[int, list[int]] = {}
    for row in tables.get("unreal_pawn_attachment_groups", []):
        att_groups.setdefault(_int_key(row["spawn_id_fk"]), []).append(_int_key(row["set_id_fk"]))

    att_sets: dict[int, list[dict[str, Any]]] = {}
    for row in tables.get("unreal_pawn_attachment_sets", []):
        att_sets.setdefault(_int_key(row["set_id"]), []).append(row)

    appearances: dict[int, list[dict[str, Any]]] = {}
    for row in tables.get("unreal_pawn_appearances", []):
        appearances.setdefault(_int_key(row["spawn_id_fk"]), []).append(row)

    return {
        "races": races,
        "pawns": pawns,
        "actor_scales": actor_scales,
        "att_groups": att_groups,
        "att_sets": att_sets,
        "appearances": appearances,
    }


def race_spawn_counts(snapshot: dict[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for row in snapshot.get("tables", {}).get("unreal_pawn", []):
        race_id = _int_key(row.get("raceID"))
        counts[race_id] = counts.get(race_id, 0) + 1
    return counts
