#!/usr/bin/env python3
"""Export spatial music and ambience volumes from the vgo_world MySQL database."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="MySQL host")
    parser.add_argument("--port", type=int, default=3306, help="MySQL port")
    parser.add_argument("--user", default="root", help="MySQL user")
    parser.add_argument(
        "--password",
        default="",
        help="MySQL password. Leave empty to omit the password flag.",
    )
    parser.add_argument("--database", default="vgo_world", help="Database name")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "output/audio/world_audio_db_volumes",
        help="Output directory for manifest files",
    )
    return parser.parse_args(argv)


def run_mysql_query(args: argparse.Namespace, query: str) -> list[list[str]]:
    command = [
        "mysql",
        f"--host={args.host}",
        f"--port={args.port}",
        f"--user={args.user}",
        "--batch",
        "--raw",
        "--skip-column-names",
        args.database,
        "-e",
        query,
    ]
    if args.password:
        command.insert(4, f"--password={args.password}")

    result = subprocess.run(command, check=True, capture_output=True, text=True)
    rows: list[list[str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        rows.append(line.split("\t"))
    return rows


def parse_int(value: str) -> int:
    if value == "" or value is None:
        return 0
    return int(value)


def parse_float(value: str) -> float:
    if value == "" or value is None:
        return 0.0
    return float(value)


def clean_string(value: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def has_bbox(record: dict[str, object]) -> bool:
    bbox_min = record["bbox_min"]
    bbox_max = record["bbox_max"]
    return any(component != 0.0 for component in bbox_min + bbox_max)


def classify_shape(record: dict[str, object]) -> str:
    radius = record["radius"]
    z_range = record["z_range"]
    if has_bbox(record):
        return "bbox"
    if radius > 0 and z_range > 0:
        return "radius-zrange"
    if radius > 0:
        return "radius"
    return "point"


def build_music_rows(args: argparse.Namespace) -> list[dict[str, object]]:
    query = """
        SELECT
            m.music_id,
            m.chunk_id_fk,
            c.shortname,
            c.displayname,
            c.filename,
            c.coord_x,
            c.coord_y,
            m.location_x,
            m.location_y,
            m.location_z,
            m._3DSType,
            m.bb_IsValid,
            m.Radius,
            m.zRange,
            m.entryIntensity,
            m.entryOgg,
            m.excitementIsactFile,
            m.primaryIsactFile,
            m.secondaryIsactFile,
            m.bb_MinX,
            m.bb_MinY,
            m.bb_MinZ,
            m.bb_MaxX,
            m.bb_MaxY,
            m.bb_MaxZ
        FROM unreal_music AS m
        JOIN chunks AS c
            ON c.chunk_id = m.chunk_id_fk
        ORDER BY m.music_id
    """
    rows = run_mysql_query(args, query)
    records: list[dict[str, object]] = []
    for row in rows:
        record = {
            "id": parse_int(row[0]),
            "chunk": {
                "chunk_id": parse_int(row[1]),
                "shortname": row[2],
                "displayname": row[3],
                "filename": row[4],
                "coord_x": parse_int(row[5]),
                "coord_y": parse_int(row[6]),
            },
            "location": [parse_float(row[7]), parse_float(row[8]), parse_float(row[9])],
            "shape_type": parse_int(row[10]),
            "bbox_is_valid": parse_int(row[11]) != 0,
            "radius": parse_float(row[12]),
            "z_range": parse_float(row[13]),
            "entry_intensity": parse_int(row[14]),
            "entry_ogg": clean_string(row[15]),
            "excitement_isact_file": clean_string(row[16]),
            "primary_isact_file": clean_string(row[17]),
            "secondary_isact_file": clean_string(row[18]),
            "bbox_min": [parse_float(row[19]), parse_float(row[20]), parse_float(row[21])],
            "bbox_max": [parse_float(row[22]), parse_float(row[23]), parse_float(row[24])],
        }
        record["shape_hint"] = classify_shape(record)
        records.append(record)
    return records


def build_sound_rows(args: argparse.Namespace) -> list[dict[str, object]]:
    query = """
        SELECT
            s.sound_id,
            s.chunk_id_fk,
            c.shortname,
            c.displayname,
            c.filename,
            c.coord_x,
            c.coord_y,
            s.location_x,
            s.location_y,
            s.location_z,
            s._3DSType,
            s.bb_IsValid,
            s.Radius,
            s.zRange,
            s.isAnEmitter,
            s.oneShots,
            s.primaryIsactFile,
            s.reverbType_ID,
            s.specialAmbience,
            s.bb_MinX,
            s.bb_MinY,
            s.bb_MinZ,
            s.bb_MaxX,
            s.bb_MaxY,
            s.bb_MaxZ,
            s.sr_pitch,
            s.sr_roll,
            s.sr_yaw
        FROM unreal_sound AS s
        JOIN chunks AS c
            ON c.chunk_id = s.chunk_id_fk
        ORDER BY s.sound_id
    """
    rows = run_mysql_query(args, query)
    records: list[dict[str, object]] = []
    for row in rows:
        record = {
            "id": parse_int(row[0]),
            "chunk": {
                "chunk_id": parse_int(row[1]),
                "shortname": row[2],
                "displayname": row[3],
                "filename": row[4],
                "coord_x": parse_int(row[5]),
                "coord_y": parse_int(row[6]),
            },
            "location": [parse_float(row[7]), parse_float(row[8]), parse_float(row[9])],
            "shape_type": parse_int(row[10]),
            "bbox_is_valid": parse_int(row[11]) != 0,
            "radius": parse_float(row[12]),
            "z_range": parse_float(row[13]),
            "is_emitter": parse_int(row[14]) != 0,
            "one_shots": parse_int(row[15]) != 0,
            "primary_isact_file": clean_string(row[16]),
            "reverb_type_id": parse_int(row[17]),
            "special_ambience": parse_int(row[18]) != 0,
            "bbox_min": [parse_float(row[19]), parse_float(row[20]), parse_float(row[21])],
            "bbox_max": [parse_float(row[22]), parse_float(row[23]), parse_float(row[24])],
            "rotation": [parse_int(row[25]), parse_int(row[26]), parse_int(row[27])],
        }
        record["shape_hint"] = classify_shape(record)
        records.append(record)
    return records


def build_summary(records: list[dict[str, object]], *, file_key: str) -> dict[str, object]:
    shape_counts = Counter(record["shape_hint"] for record in records)
    type_counts = Counter(record["shape_type"] for record in records)
    file_counts = Counter()
    for record in records:
        value = record.get(file_key)
        if value:
            file_counts[value] += 1
    top_files = [
        {"name": name, "count": count}
        for name, count in file_counts.most_common(20)
    ]
    return {
        "row_count": len(records),
        "shape_hint_counts": dict(sorted(shape_counts.items())),
        "shape_type_counts": dict(sorted(type_counts.items())),
        "top_files": top_files,
    }


def build_manifest(args: argparse.Namespace) -> dict[str, object]:
    music_records = build_music_rows(args)
    sound_records = build_sound_rows(args)
    return {
        "source": {
            "database": args.database,
            "host": args.host,
            "port": args.port,
            "music_table": "unreal_music",
            "sound_table": "unreal_sound",
            "shape_hint_note": (
                "shape_hint is derived from populated radius, z_range, and bbox fields; "
                "raw _3DSType is preserved as shape_type."
            ),
        },
        "summary": {
            "music": build_summary(music_records, file_key="primary_isact_file"),
            "sound": build_summary(sound_records, file_key="primary_isact_file"),
        },
        "music_volumes": music_records,
        "sound_volumes": sound_records,
    }


def build_markdown(manifest: dict[str, object]) -> str:
    music_summary = manifest["summary"]["music"]
    sound_summary = manifest["summary"]["sound"]
    lines = [
        "# World Audio DB Volumes",
        "",
        "Generated from local MySQL `vgo_world` tables `unreal_music` and `unreal_sound`.",
        "",
        "## Summary",
        "",
        f"- music rows: `{music_summary['row_count']}`",
        f"- sound rows: `{sound_summary['row_count']}`",
        f"- music shape hints: `{json.dumps(music_summary['shape_hint_counts'], sort_keys=True)}`",
        f"- sound shape hints: `{json.dumps(sound_summary['shape_hint_counts'], sort_keys=True)}`",
        "",
        "## Notes",
        "",
        "- `shape_hint` is derived from populated radius, z-range, and bbox fields and is only a normalization aid.",
        "- Raw database `_3DSType` values are preserved as `shape_type` for later reverse engineering.",
        "- Music rows carry `entry_ogg`, `primary_isact_file`, `secondary_isact_file`, and `entry_intensity` when present.",
        "- Sound rows carry `primary_isact_file`, `special_ambience`, `one_shots`, `is_emitter`, and `rotation`.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    manifest = build_manifest(args)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.out / "manifest.md").write_text(build_markdown(manifest), encoding="utf-8")
    print(
        f"Wrote {args.out / 'manifest.json'} with "
        f"{manifest['summary']['music']['row_count']} music rows and "
        f"{manifest['summary']['sound']['row_count']} sound rows"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))