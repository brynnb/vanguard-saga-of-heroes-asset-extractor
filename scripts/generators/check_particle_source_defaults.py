#!/usr/bin/env python3
"""Validate source-backed particle class defaults in generated manifests/cells."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from generate_particle_manifest import DEFAULT_OUTPUT_ROOT


DEFAULT_MANIFEST = DEFAULT_OUTPUT_ROOT / "data/particle_emitters.json"
DEFAULT_GLOBAL_INDEX = DEFAULT_OUTPUT_ROOT / "godot_runtime/global_particle_cells.json"
RENDER_DEFAULT_CLASSES = {"SpriteEmitter", "SparkEmitter", "BeamEmitter", "MeshEmitter"}
FORCED_TWO_SIDED_CLASSES = {"SpriteEmitter", "SparkEmitter", "BeamEmitter"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--global-index", type=Path, default=DEFAULT_GLOBAL_INDEX)
    args = parser.parse_args()

    manifest = read_json(args.manifest)
    global_index = read_json(args.global_index)

    errors: list[str] = []
    manifest_stats = validate_manifest(manifest, errors)
    global_stats = validate_global_index(global_index, errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print(
        "Particle source-default check OK: "
        f"manifest={json.dumps(manifest_stats, sort_keys=True)} "
        f"global={json.dumps(global_stats, sort_keys=True)}"
    )
    return 0


def read_json(path: Path) -> Any:
    with path.expanduser().resolve().open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_manifest(manifest: dict[str, Any], errors: list[str]) -> dict[str, int]:
    templates = manifest.get("templates", [])
    if not isinstance(templates, list):
        errors.append("manifest templates is not a list")
        return {}

    stats = {
        "templates": 0,
        "fx_actor_defaulted": 0,
        "missing_max_default_10": 0,
        "missing_max_default_24": 0,
        "missing_draw_style_default_3": 0,
        "alpha_test_default_true": 0,
        "alpha_ref_default_0": 0,
        "accepts_projectors_default_false": 0,
        "z_test_default_true": 0,
        "z_write_default_false": 0,
        "forced_two_sided_default_true": 0,
        "mesh_two_sided_default_false": 0,
        "spark_line_default_5": 0,
        "beam_hf_default_10": 0,
        "beam_lf_default_3": 0,
        "ribbon_length_default_1": 0,
    }
    for template in templates:
        if not isinstance(template, dict):
            continue
        stats["templates"] += 1
        source = template.get("source", {})
        props = template.get("props", {})
        normalized = template.get("normalized", {})
        if not isinstance(source, dict) or not isinstance(props, dict) or not isinstance(normalized, dict):
            continue
        class_name = str(source.get("class", ""))
        kind = str(template.get("kind", ""))
        defaults = normalized.get("source_defaulted_fields", [])
        if defaults and kind == "fx_actor":
            stats["fx_actor_defaulted"] += 1
        if class_name == "SpriteEmitter" and "MaxParticles" not in props:
            if normalized.get("max_particles") == 10:
                stats["missing_max_default_10"] += 1
            if normalized.get("max_particles") == 24:
                stats["missing_max_default_24"] += 1
        if class_name in RENDER_DEFAULT_CLASSES:
            if "DrawStyle" not in props and normalized.get("draw_style") == 3:
                stats["missing_draw_style_default_3"] += 1
            if "AlphaTest" not in props and normalized.get("alpha_test") is True:
                stats["alpha_test_default_true"] += 1
            if "AlphaRef" not in props and normalized.get("alpha_ref") == 0:
                stats["alpha_ref_default_0"] += 1
            if "AcceptsProjectors" not in props and normalized.get("accepts_projectors") is False:
                stats["accepts_projectors_default_false"] += 1
            if "ZTest" not in props and normalized.get("z_test") is True:
                stats["z_test_default_true"] += 1
            if "ZWrite" not in props and normalized.get("z_write") is False:
                stats["z_write_default_false"] += 1
        if class_name in FORCED_TWO_SIDED_CLASSES and "RenderTwoSided" not in props:
            if normalized.get("render_two_sided") is True:
                stats["forced_two_sided_default_true"] += 1
        if class_name == "MeshEmitter" and "RenderTwoSided" not in props:
            if normalized.get("render_two_sided") is False:
                stats["mesh_two_sided_default_false"] += 1
        if class_name == "SparkEmitter" and "LineSegmentsRange" not in props:
            if normalized.get("line_segments") == {"min": 5.0, "max": 5.0}:
                stats["spark_line_default_5"] += 1
        if class_name == "BeamEmitter":
            if "HighFrequencyPoints" not in props and normalized.get("high_frequency_points") == 10:
                stats["beam_hf_default_10"] += 1
            if "LowFrequencyPoints" not in props and normalized.get("low_frequency_points") == 3:
                stats["beam_lf_default_3"] += 1
        if bool(normalized.get("ribbon_on")) and "RibbonLength" not in props:
            if normalized.get("ribbon_length") == 1.0:
                stats["ribbon_length_default_1"] += 1

    if stats["fx_actor_defaulted"] != 0:
        errors.append(f"fx_actor records inherited particle defaults: {stats['fx_actor_defaulted']}")
    if stats["missing_max_default_24"] != 0:
        errors.append(f"missing MaxParticles still normalized to 24: {stats['missing_max_default_24']}")
    if stats["missing_max_default_10"] == 0:
        errors.append("no missing MaxParticles templates defaulted to 10")
    if stats["missing_draw_style_default_3"] == 0:
        errors.append("no missing DrawStyle templates defaulted to PTDS_Translucent")
    if stats["alpha_test_default_true"] == 0:
        errors.append("no missing AlphaTest templates defaulted to true")
    if stats["alpha_ref_default_0"] == 0:
        errors.append("no missing AlphaRef templates defaulted to 0")
    if stats["accepts_projectors_default_false"] == 0:
        errors.append("no missing AcceptsProjectors templates defaulted to false")
    if stats["z_test_default_true"] == 0:
        errors.append("no missing ZTest templates defaulted to true")
    if stats["z_write_default_false"] == 0:
        errors.append("no missing ZWrite templates defaulted to false")
    if stats["forced_two_sided_default_true"] == 0:
        errors.append("no sprite/beam/spark templates defaulted to forced two-sided rendering")
    if stats["mesh_two_sided_default_false"] == 0:
        errors.append("no MeshEmitter templates defaulted to non-two-sided rendering")
    if stats["spark_line_default_5"] == 0:
        errors.append("no SparkEmitter missing LineSegmentsRange defaulted to 5")
    if stats["beam_hf_default_10"] == 0:
        errors.append("no BeamEmitter missing HighFrequencyPoints defaulted to 10")
    if stats["beam_lf_default_3"] == 0:
        errors.append("no BeamEmitter missing LowFrequencyPoints defaulted to 3")
    return stats


def validate_global_index(index: dict[str, Any], errors: list[str]) -> dict[str, int]:
    stats = {
        "records": 0,
        "defaulted_records": 0,
        "default_max_10": 0,
        "default_max_24": 0,
        "default_draw_style_3": 0,
        "default_alpha_test_true": 0,
        "default_alpha_ref_0": 0,
        "default_accepts_projectors_false": 0,
        "default_z_test_true": 0,
        "default_z_write_false": 0,
        "default_forced_two_sided_true": 0,
        "default_mesh_two_sided_false": 0,
        "spark_line_default_5": 0,
        "beam_hf_default_10": 0,
        "beam_lf_default_3": 0,
    }
    for record in iter_global_records(index):
        stats["records"] += 1
        defaults = record.get("source_defaulted_fields", [])
        if not isinstance(defaults, list) or not defaults:
            continue
        stats["defaulted_records"] += 1
        if "max_particles" in defaults:
            if record.get("max_particles") == 10:
                stats["default_max_10"] += 1
            if record.get("max_particles") == 24:
                stats["default_max_24"] += 1
        if "draw_style" in defaults and record.get("draw_style") == 3:
            stats["default_draw_style_3"] += 1
        if "alpha_test" in defaults and record.get("alpha_test") is True:
            stats["default_alpha_test_true"] += 1
        if "alpha_ref" in defaults and record.get("alpha_ref") == 0:
            stats["default_alpha_ref_0"] += 1
        if "accepts_projectors" in defaults and record.get("accepts_projectors") is False:
            stats["default_accepts_projectors_false"] += 1
        if "z_test" in defaults and record.get("z_test") is True:
            stats["default_z_test_true"] += 1
        if "z_write" in defaults and record.get("z_write") is False:
            stats["default_z_write_false"] += 1
        if (
            record.get("class") in FORCED_TWO_SIDED_CLASSES
            and "render_two_sided" in defaults
            and record.get("render_two_sided") is True
        ):
            stats["default_forced_two_sided_true"] += 1
        if (
            record.get("class") == "MeshEmitter"
            and "render_two_sided" in defaults
            and record.get("render_two_sided") is False
        ):
            stats["default_mesh_two_sided_false"] += 1
        if (
            record.get("class") == "SparkEmitter"
            and "line_segments" in defaults
            and record.get("line_segments") == {"min": 5.0, "max": 5.0}
        ):
            stats["spark_line_default_5"] += 1
        if record.get("class") == "BeamEmitter":
            if "high_frequency_points" in defaults and record.get("high_frequency_points") == 10:
                stats["beam_hf_default_10"] += 1
            if "low_frequency_points" in defaults and record.get("low_frequency_points") == 3:
                stats["beam_lf_default_3"] += 1

    if stats["records"] == 0:
        errors.append("global index contained no particle placement records")
    if stats["defaulted_records"] == 0:
        errors.append("global index contained no defaulted particle placement records")
    if stats["default_max_24"] != 0:
        errors.append(f"global records defaulted MaxParticles to 24: {stats['default_max_24']}")
    if stats["default_max_10"] == 0:
        errors.append("global records did not carry any MaxParticles=10 defaults")
    if stats["default_draw_style_3"] == 0:
        errors.append("global records did not carry any DrawStyle=PTDS_Translucent defaults")
    if stats["default_alpha_test_true"] == 0:
        errors.append("global records did not carry any AlphaTest=true defaults")
    if stats["default_alpha_ref_0"] == 0:
        errors.append("global records did not carry any AlphaRef=0 defaults")
    if stats["default_accepts_projectors_false"] == 0:
        errors.append("global records did not carry any AcceptsProjectors=false defaults")
    if stats["default_z_test_true"] == 0:
        errors.append("global records did not carry any ZTest=true defaults")
    if stats["default_z_write_false"] == 0:
        errors.append("global records did not carry any ZWrite=false defaults")
    if stats["default_forced_two_sided_true"] == 0:
        errors.append("global records did not carry any forced two-sided render defaults")
    if stats["default_mesh_two_sided_false"] == 0:
        errors.append("global records did not carry any MeshEmitter two-sided=false defaults")
    return stats


def iter_global_records(index: dict[str, Any]):
    cells = index.get("cells", {})
    if not isinstance(cells, dict):
        return
    for cell in cells.values():
        if not isinstance(cell, dict):
            continue
        for record in cell.get("placements", []):
            if isinstance(record, dict):
                yield record
        for chunk in cell.get("chunks", []):
            if not isinstance(chunk, dict):
                continue
            for record in chunk.get("placements", []):
                if isinstance(record, dict):
                    yield record


if __name__ == "__main__":
    raise SystemExit(main())
