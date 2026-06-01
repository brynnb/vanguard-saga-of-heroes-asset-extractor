#!/usr/bin/env python3
"""Export the small vgo_world subset needed for NPC assembly.

The asset pipeline does not need a live VGO Emulator database if this snapshot
exists. The snapshot intentionally contains only the race, pawn, scale,
attachment, and appearance fields consumed by export_npc_assembly.py and
build_race_prefix_map.py.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT_PATH = ROOT_DIR / "output" / "data" / "vgo_world_npc_snapshot.json"

DEFAULT_DB_CONFIG = {
    "host": os.environ.get("VGO_DB_HOST", "127.0.0.1"),
    "user": os.environ.get("VGO_DB_USER", "root"),
    "password": os.environ.get("VGO_DB_PASSWORD", ""),
    "database": os.environ.get("VGO_DB_NAME", "vgo_world"),
}

SNAPSHOT_SCHEMA_VERSION = 1

TABLE_QUERIES = {
    "races": (
        "SELECT id, category, name, maleMeshID, femaleMeshID "
        "FROM races ORDER BY id"
    ),
    "unreal_pawn": (
        "SELECT spawn_id_fk, gender, raceID, modelNum, "
        "playerDisplayName, iMount, wieldPreference "
        "FROM unreal_pawn ORDER BY spawn_id_fk"
    ),
    "unreal_actor": (
        "SELECT spawn_id, spawn_name, drawScale_low, drawScale_high "
        "FROM unreal_actor "
        "WHERE unreal_type = 'SGONPCPawn' "
        "ORDER BY spawn_id"
    ),
    "unreal_pawn_attachment_groups": (
        "SELECT spawn_id_fk, set_id_fk "
        "FROM unreal_pawn_attachment_groups "
        "ORDER BY spawn_id_fk, set_id_fk"
    ),
    "unreal_pawn_attachment_sets": (
        "SELECT set_id, attachment_slot, attachment_index, "
        "package_index, inventory_slot "
        "FROM unreal_pawn_attachment_sets "
        "ORDER BY set_id, attachment_slot, attachment_index"
    ),
    "unreal_pawn_appearances": (
        "SELECT spawn_id_fk, appearance_id, "
        "appearance_value_low, appearance_value_high "
        "FROM unreal_pawn_appearances "
        "ORDER BY spawn_id_fk, appearance_id"
    ),
}


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in row.items()}


def db_config_from_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        "host": args.db_host,
        "user": args.db_user,
        "password": args.db_password,
        "database": args.db_name,
    }


def fetch_snapshot(db_config: dict[str, str]) -> dict[str, Any]:
    import mysql.connector

    conn = mysql.connector.connect(**db_config)
    try:
        cur = conn.cursor(dictionary=True)
        try:
            tables: dict[str, list[dict[str, Any]]] = {}
            for name, query in TABLE_QUERIES.items():
                cur.execute(query)
                tables[name] = [_json_row(row) for row in cur.fetchall()]
        finally:
            cur.close()
    finally:
        conn.close()

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": {
            "database": db_config.get("database", "vgo_world"),
            "host": db_config.get("host", ""),
        },
        "summary": {name: len(rows) for name, rows in tables.items()},
        "tables": tables,
    }


def write_snapshot(snapshot: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2)
        handle.write("\n")
    os.replace(tmp_path, output_path)


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    parser.add_argument("--db-host", default=DEFAULT_DB_CONFIG["host"], help="VGO world MySQL host")
    parser.add_argument("--db-user", default=DEFAULT_DB_CONFIG["user"], help="VGO world MySQL user")
    parser.add_argument("--db-password", default=DEFAULT_DB_CONFIG["password"], help="VGO world MySQL password")
    parser.add_argument("--db-name", default=DEFAULT_DB_CONFIG["database"], help="VGO world MySQL database")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    snapshot = fetch_snapshot(db_config_from_args(args))
    write_snapshot(snapshot, args.out.expanduser())
    print(f"Wrote VGO world NPC snapshot to {args.out}")
    for name, count in snapshot["summary"].items():
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
