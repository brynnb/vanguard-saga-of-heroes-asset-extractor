#!/usr/bin/env python3
"""Join DB-backed world audio volumes to recovered music and ambience bundles."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

SOUND_FAMILY_ALIASES = {
    "AmbienceWaterSounds": "AmbienceWater",
    "AurasSounds": "Auras",
}


def compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-volumes",
        type=Path,
        default=REPO_ROOT / "output/audio/world_audio_db_volumes/manifest.json",
        help="Normalized DB world-audio volume manifest",
    )
    parser.add_argument(
        "--bundle-manifest",
        type=Path,
        default=REPO_ROOT / "output/audio/music_ambience_manifest/manifest.json",
        help="Recovered ICB/runtime bundle manifest",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "output/audio/world_audio_activation_manifest",
        help="Output directory for the joined activation manifest",
    )
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def build_bundle_index(bundles: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {
        compact_name(bundle["bundle_name"]): bundle
        for bundle in bundles
        if bundle.get("bundle_name")
    }


def build_music_candidates(row: dict[str, object]) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for source in ("primary_isact_file", "secondary_isact_file", "excitement_isact_file", "entry_ogg"):
        value = row.get(source)
        if isinstance(value, str) and value:
            candidates.append((source, value))
    return candidates


def build_sound_candidates(row: dict[str, object]) -> list[tuple[str, str]]:
    value = row.get("primary_isact_file")
    if not isinstance(value, str) or not value:
        return []
    head = value.split(".", 1)[0]
    candidates = [("primary_isact_file.head", head)]
    alias = SOUND_FAMILY_ALIASES.get(head)
    if alias:
        candidates.append(("primary_isact_file.family_alias", alias))
    if not head.lower().startswith("ambience"):
        candidates.append(("primary_isact_file.prefixed", f"Ambience{head}"))
    return candidates


def geometry_from_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "shape_type": row["shape_type"],
        "shape_hint": row["shape_hint"],
        "bbox_is_valid": row["bbox_is_valid"],
        "location": row["location"],
        "radius": row["radius"],
        "z_range": row["z_range"],
        "bbox_min": row["bbox_min"],
        "bbox_max": row["bbox_max"],
    }


def summarize_ambience_bundle(bundle: dict[str, object]) -> dict[str, object]:
    day_channels = sorted(bundle.get("channels", {}).get("day", {}).keys())
    night_channels = sorted(bundle.get("channels", {}).get("night", {}).keys())
    auxiliary_titles = sorted(
        entry.get("title")
        for entry in bundle.get("auxiliary", [])
        if entry.get("title")
    )
    return {
        "bundle_name": bundle["bundle_name"],
        "paired_isb_bank": bundle.get("paired_isb_bank"),
        "profile_names": bundle.get("profile_names", []),
        "day_channels": day_channels,
        "night_channels": night_channels,
        "auxiliary_titles": auxiliary_titles,
    }


def summarize_music_bundle(bundle: dict[str, object]) -> dict[str, object]:
    return {
        "bundle_name": bundle["bundle_name"],
        "paired_isb_bank": bundle.get("paired_isb_bank"),
        "matched_runtime_titles": bundle.get("matched_runtime_titles", []),
        "chunk_activation": bundle.get("chunk_activation", {}),
        "entry_points": bundle.get("entry_points", []),
        "transitions": bundle.get("transitions", []),
    }


def join_row(
    row: dict[str, object],
    *,
    row_kind: str,
    bundle_index: dict[str, dict[str, object]],
) -> dict[str, object]:
    candidates = build_music_candidates(row) if row_kind == "music" else build_sound_candidates(row)
    matched_bundle = None
    match_source = None
    match_value = None
    for source, value in candidates:
        matched_bundle = bundle_index.get(compact_name(value))
        if matched_bundle:
            match_source = source
            match_value = value
            break

    binding = {
        "id": row["id"],
        "chunk": row["chunk"],
        "geometry": geometry_from_row(row),
        "db_fields": {
            key: row[key]
            for key in sorted(row.keys())
            if key not in {"id", "chunk", "location", "shape_type", "shape_hint", "bbox_is_valid", "radius", "z_range", "bbox_min", "bbox_max"}
        },
        "bundle_match": {
            "matched": bool(matched_bundle),
            "source": match_source,
            "value": match_value,
        },
    }
    if matched_bundle:
        binding["bundle"] = (
            summarize_music_bundle(matched_bundle)
            if row_kind == "music"
            else summarize_ambience_bundle(matched_bundle)
        )
    return binding


def build_summary(
    bindings: list[dict[str, object]],
    *,
    unmatched_name_getter,
) -> dict[str, object]:
    matched = [binding for binding in bindings if binding["bundle_match"]["matched"]]
    unmatched = [binding for binding in bindings if not binding["bundle_match"]["matched"]]
    shape_counts = Counter(binding["geometry"]["shape_hint"] for binding in bindings)
    unmatched_name_counts = Counter()
    for binding in unmatched:
        name = unmatched_name_getter(binding)
        if name:
            unmatched_name_counts[name] += 1
    return {
        "row_count": len(bindings),
        "matched_rows": len(matched),
        "unmatched_rows": len(unmatched),
        "matched_ratio": (len(matched) / len(bindings)) if bindings else 0.0,
        "shape_hint_counts": dict(sorted(shape_counts.items())),
        "top_unmatched_names": [
            {"name": name, "count": count}
            for name, count in unmatched_name_counts.most_common(20)
        ],
    }


def build_manifest(db_manifest: dict[str, object], bundle_manifest: dict[str, object]) -> dict[str, object]:
    music_index = build_bundle_index(bundle_manifest["music_bundles"])
    ambience_index = build_bundle_index(bundle_manifest["ambience_bundles"])

    music_bindings = [
        join_row(row, row_kind="music", bundle_index=music_index)
        for row in db_manifest["music_volumes"]
    ]
    sound_bindings = [
        join_row(row, row_kind="sound", bundle_index=ambience_index)
        for row in db_manifest["sound_volumes"]
    ]

    return {
        "summary": {
            "music": build_summary(
                music_bindings,
                unmatched_name_getter=lambda binding: binding["db_fields"].get("primary_isact_file"),
            ),
            "sound": build_summary(
                sound_bindings,
                unmatched_name_getter=lambda binding: binding["db_fields"].get("primary_isact_file"),
            ),
        },
        "provenance": {
            "db_source": db_manifest.get("source", {}),
            "bundle_source": bundle_manifest.get("provenance", {}),
            "bundle_source_summary": bundle_manifest.get("summary", {}),
        },
        "music_volume_bindings": music_bindings,
        "sound_volume_bindings": sound_bindings,
    }


def build_markdown(manifest: dict[str, object]) -> str:
    music = manifest["summary"]["music"]
    sound = manifest["summary"]["sound"]
    lines = [
        "# World Audio Activation Manifest",
        "",
        "Joined from the DB-backed world-audio volume manifest and the recovered music/ambience bundle manifest.",
        "",
        "## Summary",
        "",
        f"- music rows: `{music['row_count']}` total, `{music['matched_rows']}` matched, `{music['unmatched_rows']}` unmatched",
        f"- sound rows: `{sound['row_count']}` total, `{sound['matched_rows']}` matched, `{sound['unmatched_rows']}` unmatched",
        f"- music shapes: `{json.dumps(music['shape_hint_counts'], sort_keys=True)}`",
        f"- sound shapes: `{json.dumps(sound['shape_hint_counts'], sort_keys=True)}`",
        "",
        "## Interpretation",
        "",
        "- A matched music row is a world-space trigger volume that already points at a recovered music bundle.",
        "- A matched sound row is a world-space ambience or environment volume that already points at a recovered ambience bundle.",
        "- Unmatched rows are preserved verbatim so later archaeology can resolve DB-only aliases or emulator-only content.",
    ]
    if music["top_unmatched_names"]:
        lines.extend(
            [
                "",
                "## Top Unmatched Music Names",
                "",
                *[
                    f"- {item['name']}: {item['count']}"
                    for item in music["top_unmatched_names"]
                ],
            ]
        )
    if sound["top_unmatched_names"]:
        lines.extend(
            [
                "",
                "## Top Unmatched Sound Names",
                "",
                *[
                    f"- {item['name']}: {item['count']}"
                    for item in sound["top_unmatched_names"]
                ],
            ]
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    db_manifest = load_json(args.db_volumes)
    bundle_manifest = load_json(args.bundle_manifest)
    manifest = build_manifest(db_manifest, bundle_manifest)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.out / "manifest.md").write_text(build_markdown(manifest), encoding="utf-8")
    print(
        f"Wrote {args.out / 'manifest.json'} with "
        f"{manifest['summary']['music']['matched_rows']} matched music rows and "
        f"{manifest['summary']['sound']['matched_rows']} matched sound rows"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))