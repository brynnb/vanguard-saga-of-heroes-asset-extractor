#!/usr/bin/env python3
"""Convert joined world audio bindings into a simplified engine-facing trigger manifest."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--activation-manifest",
        type=Path,
        default=REPO_ROOT / "output/audio/world_audio_activation_manifest/manifest.json",
        help="Joined world audio activation manifest",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "output/audio/world_audio_engine_manifest",
        help="Output directory for the engine-facing trigger manifest",
    )
    return parser.parse_args(argv)


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def build_shape(geometry: dict[str, object]) -> dict[str, object]:
    if geometry["shape_hint"] == "bbox":
        return {
            "kind": "bbox",
            "min": geometry["bbox_min"],
            "max": geometry["bbox_max"],
        }
    return {
        "kind": "radius-zrange" if geometry["z_range"] > 0 else "radius",
        "center": geometry["location"],
        "radius": geometry["radius"],
        "z_range": geometry["z_range"],
    }


def build_music_trigger(binding: dict[str, object]) -> dict[str, object]:
    db_fields = binding["db_fields"]
    bundle = binding.get("bundle")
    resolved_name = bundle["bundle_name"] if bundle else db_fields.get("primary_isact_file")
    return {
        "id": binding["id"],
        "kind": "music",
        "chunk": binding["chunk"],
        "resolved": bool(bundle),
        "resolved_name": resolved_name,
        "raw_primary_name": db_fields.get("primary_isact_file"),
        "raw_secondary_name": db_fields.get("secondary_isact_file"),
        "raw_excitement_name": db_fields.get("excitement_isact_file"),
        "entry_ogg": db_fields.get("entry_ogg"),
        "entry_intensity": db_fields.get("entry_intensity"),
        "shape": build_shape(binding["geometry"]),
        "runtime_titles": bundle.get("matched_runtime_titles", []) if bundle else [],
        "chunk_activation": bundle.get("chunk_activation", {}) if bundle else {},
        "entry_points": bundle.get("entry_points", []) if bundle else [],
        "transitions": bundle.get("transitions", []) if bundle else [],
    }


def build_sound_trigger(binding: dict[str, object]) -> dict[str, object]:
    db_fields = binding["db_fields"]
    bundle = binding.get("bundle")
    resolved_name = bundle["bundle_name"] if bundle else db_fields.get("primary_isact_file")
    return {
        "id": binding["id"],
        "kind": "sound",
        "chunk": binding["chunk"],
        "resolved": bool(bundle),
        "resolved_name": resolved_name,
        "raw_primary_name": db_fields.get("primary_isact_file"),
        "shape": build_shape(binding["geometry"]),
        "is_emitter": db_fields.get("is_emitter"),
        "one_shots": db_fields.get("one_shots"),
        "special_ambience": db_fields.get("special_ambience"),
        "reverb_type_id": db_fields.get("reverb_type_id"),
        "rotation": db_fields.get("rotation"),
        "day_channels": bundle.get("day_channels", []) if bundle else [],
        "night_channels": bundle.get("night_channels", []) if bundle else [],
        "profile_names": bundle.get("profile_names", []) if bundle else [],
        "auxiliary_titles": bundle.get("auxiliary_titles", []) if bundle else [],
    }


def chunk_sort_key(chunk_name: str) -> tuple[int, str]:
    return (0 if chunk_name.startswith("chunk_") else 1, chunk_name)


def build_manifest(activation_manifest: dict[str, object]) -> dict[str, object]:
    music_triggers = [
        build_music_trigger(binding)
        for binding in activation_manifest["music_volume_bindings"]
    ]
    sound_triggers = [
        build_sound_trigger(binding)
        for binding in activation_manifest["sound_volume_bindings"]
    ]

    by_chunk: dict[str, dict[str, list[dict[str, object]]]] = defaultdict(
        lambda: {"music": [], "sound": []}
    )
    for trigger in music_triggers:
        by_chunk[trigger["chunk"]["filename"]]["music"].append(trigger)
    for trigger in sound_triggers:
        by_chunk[trigger["chunk"]["filename"]]["sound"].append(trigger)

    unresolved_sound_names = Counter(
        trigger["raw_primary_name"]
        for trigger in sound_triggers
        if not trigger["resolved"] and trigger.get("raw_primary_name")
    )
    unresolved_music_names = Counter(
        trigger["raw_primary_name"]
        for trigger in music_triggers
        if not trigger["resolved"] and trigger.get("raw_primary_name")
    )

    sorted_chunks = {
        chunk_name: by_chunk[chunk_name]
        for chunk_name in sorted(by_chunk, key=chunk_sort_key)
    }

    return {
        "summary": {
            "chunk_count": len(sorted_chunks),
            "music_trigger_count": len(music_triggers),
            "resolved_music_trigger_count": sum(trigger["resolved"] for trigger in music_triggers),
            "sound_trigger_count": len(sound_triggers),
            "resolved_sound_trigger_count": sum(trigger["resolved"] for trigger in sound_triggers),
            "top_unresolved_music_names": [
                {"name": name, "count": count}
                for name, count in unresolved_music_names.most_common(20)
            ],
            "top_unresolved_sound_names": [
                {"name": name, "count": count}
                for name, count in unresolved_sound_names.most_common(20)
            ],
        },
        "provenance": activation_manifest.get("provenance", {}),
        "coordinate_note": "Trigger coordinates and extents remain in Vanguard world space.",
        "chunks": sorted_chunks,
    }


def build_markdown(manifest: dict[str, object]) -> str:
    summary = manifest["summary"]
    lines = [
        "# World Audio Engine Manifest",
        "",
        "Engine-facing trigger export derived from the joined world audio activation manifest.",
        "",
        "## Summary",
        "",
        f"- chunks with triggers: `{summary['chunk_count']}`",
        f"- music triggers: `{summary['resolved_music_trigger_count']}` resolved of `{summary['music_trigger_count']}` total",
        f"- sound triggers: `{summary['resolved_sound_trigger_count']}` resolved of `{summary['sound_trigger_count']}` total",
        "",
        "## Format",
        "",
        "- Triggers are grouped under `chunks[chunk_filename].music` and `chunks[chunk_filename].sound`.",
        "- Shapes are normalized to either `bbox` or `radius-zrange`/`radius`.",
        "- Unresolved rows are kept with raw DB cue names so the engine can still route or log them.",
        "",
        f"- Coordinate note: {manifest['coordinate_note']}",
    ]
    if summary["top_unresolved_sound_names"]:
        lines.extend(
            [
                "",
                "## Top Unresolved Sound Names",
                "",
                *[
                    f"- {item['name']}: {item['count']}"
                    for item in summary["top_unresolved_sound_names"]
                ],
            ]
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    activation_manifest = load_json(args.activation_manifest)
    manifest = build_manifest(activation_manifest)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.out / "manifest.md").write_text(build_markdown(manifest), encoding="utf-8")
    print(
        f"Wrote {args.out / 'manifest.json'} with "
        f"{manifest['summary']['music_trigger_count']} music triggers across "
        f"{manifest['summary']['chunk_count']} chunks"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))