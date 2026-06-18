#!/usr/bin/env python3
"""Build normalized SGO particle emitter templates and audit coverage."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "output"
DEFAULT_EMITTERS_JSON = DEFAULT_OUTPUT_ROOT / "data/sgo_by_class/sgo_emitters.json"
DEFAULT_OUT = DEFAULT_OUTPUT_ROOT / "data/particle_emitters.json"
DEFAULT_AUDIO_ROOT = DEFAULT_OUTPUT_ROOT / "audio/full_isb_casefix"
PARTICLE_MANIFEST_VERSION = 14

SPRITE_CLASSES = {"SpriteEmitter", "SparkEmitter"}
BEAM_CLASSES = {"BeamEmitter"}
MESH_CLASSES = {"MeshEmitter"}
LIGHT_CLASSES = {"LightEmitter"}
PARTICLE_EMITTER_CLASSES = (
    SPRITE_CLASSES
    | BEAM_CLASSES
    | MESH_CLASSES
    | LIGHT_CLASSES
    | {
        "ParticleEmitter",
        "TrailEmitter",
        "ProjectorEmitter",
        "TextEmitter",
        "EMFXMeshEmitter",
    }
)
PARTICLE_DEFAULT_MAX_PARTICLES = 10
PARTICLE_DEFAULT_DRAW_STYLE = 3
PARTICLE_DEFAULT_ALPHA_REF = 0
PARTICLE_DEFAULT_ALPHA_TEST = True
PARTICLE_DEFAULT_ACCEPTS_PROJECTORS = False
PARTICLE_DEFAULT_Z_TEST = True
PARTICLE_DEFAULT_Z_WRITE = False
PARTICLE_FORCE_TWO_SIDED_CLASSES = {
    "SpriteEmitter",
    "BeamEmitter",
    "SparkEmitter",
    "TrailEmitter",
}


def scalar_range(value: float) -> dict[str, float]:
    return {"min": float(value), "max": float(value)}


def scalar_range_vector(value: float) -> dict[str, dict[str, float]]:
    return {
        "x": scalar_range(value),
        "y": scalar_range(value),
        "z": scalar_range(value),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emitters", type=Path, default=DEFAULT_EMITTERS_JSON)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--textures-root", type=Path, default=None)
    parser.add_argument("--audio-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    output_root = args.output_root.expanduser().resolve()
    emitters_path = args.emitters.expanduser().resolve()
    textures_root = (
        args.textures_root.expanduser().resolve()
        if args.textures_root is not None
        else output_root / "textures"
    )
    audio_root = (
        args.audio_root.expanduser().resolve()
        if args.audio_root is not None
        else output_root / "audio/full_isb_casefix"
    )
    out_path = args.out.expanduser().resolve()

    if not emitters_path.exists():
        raise SystemExit(f"missing emitter bucket: {emitters_path}")

    emitters_by_prefab = read_json(emitters_path)
    if not isinstance(emitters_by_prefab, dict):
        raise SystemExit(f"invalid emitter bucket: {emitters_path}")

    texture_index = build_texture_index(textures_root, output_root)
    audio_index = build_audio_index(audio_root, output_root)
    templates: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    prop_counts: Counter[str] = Counter()
    texture_counts: Counter[str] = Counter()
    unresolved_textures: Counter[str] = Counter()
    ambient_sound_counts: Counter[str] = Counter()
    unresolved_ambient_sounds: Counter[str] = Counter()
    sound_entry_counts: Counter[str] = Counter()
    unresolved_sound_entries: Counter[str] = Counter()
    decoded_array_counts: Counter[str] = Counter()
    normalized_range_counts: Counter[str] = Counter()
    source_default_counts: Counter[str] = Counter()
    renderable_count = 0

    for prefab_name in sorted(emitters_by_prefab):
        actors = emitters_by_prefab.get(prefab_name, [])
        if not isinstance(actors, list):
            continue
        for actor_index, actor_value in enumerate(actors):
            if not isinstance(actor_value, dict):
                continue
            actor = actor_value
            class_name = str(actor.get("class", "")).strip()
            actor_name = str(actor.get("name", "")).strip()
            props = actor.get("props", {})
            if not isinstance(props, dict):
                props = {}
            kind = classify_emitter(class_name)
            class_counts[class_name] += 1
            kind_counts[kind] += 1
            prop_counts.update(str(key) for key in props.keys())
            template_id = particle_template_id(prefab_name, actor_index, class_name, actor_name)
            normalized = normalize_props(props, texture_index, audio_index, class_name)
            source_default_counts.update(
                str(key) for key in normalized.get("source_defaulted_fields", [])
            )
            if bool(normalized.get("renderable", False)):
                renderable_count += 1
            texture_ref = normalized.get("texture_ref", "")
            if texture_ref:
                texture_counts[str(texture_ref)] += 1
                if not normalized.get("texture_relative_path"):
                    unresolved_textures[str(texture_ref)] += 1
            ambient_sound_key = sound_ref_key(
                normalized.get("ambient_sound_object_path"),
                normalized.get("ambient_sound_ref"),
            )
            if ambient_sound_key:
                ambient_sound_counts[ambient_sound_key] += 1
                if not normalized.get("ambient_sound_relative_path"):
                    unresolved_ambient_sounds[ambient_sound_key] += 1
            for sound in normalized_sound_entries(normalized.get("sounds")):
                sound_key = sound_ref_key(sound.get("sound_object_path"), sound.get("sound_ref"))
                if not sound_key:
                    continue
                sound_entry_counts[sound_key] += 1
                if not sound.get("sound_relative_path"):
                    unresolved_sound_entries[sound_key] += 1
            for key, value in props.items():
                if isinstance(value, dict):
                    if isinstance(value.get("elements"), list):
                        decoded_array_counts[str(key)] += 1
                    if looks_like_range(value) or looks_like_range_vector(value):
                        normalized_range_counts[str(key)] += 1
            templates.append(
                {
                    "id": template_id,
                    "source": {
                        "kind": "sgo_prefab_extra",
                        "prefab": prefab_name,
                        "actor_index": actor_index,
                        "class": class_name,
                        "name": actor_name,
                    },
                    "kind": kind,
                    "renderable": bool(normalized.get("renderable", False)),
                    "normalized": normalized,
                    "props": props,
                }
            )

    manifest = {
        "version": PARTICLE_MANIFEST_VERSION,
        "generated_by": "scripts/generators/generate_particle_manifest.py",
        "generated_at_unix": int(time.time()),
        "source_relative_path": relative_path(emitters_path, output_root),
        "texture_root_relative_path": relative_path(textures_root, output_root),
        "prefab_count": len(emitters_by_prefab),
        "template_count": len(templates),
        "renderable_template_count": renderable_count,
        "texture_file_count": len(texture_index.get("_files", [])),
        "audio_root_relative_path": relative_path(audio_root, output_root),
        "audio_sample_file_count": len(audio_index.get("_files", [])),
        "texture_ref_count": sum(texture_counts.values()),
        "resolved_texture_ref_count": sum(texture_counts.values()) - sum(unresolved_textures.values()),
        "unresolved_texture_ref_count": sum(unresolved_textures.values()),
        "ambient_sound_ref_count": sum(ambient_sound_counts.values()),
        "resolved_ambient_sound_ref_count": sum(ambient_sound_counts.values())
        - sum(unresolved_ambient_sounds.values()),
        "unresolved_ambient_sound_ref_count": sum(unresolved_ambient_sounds.values()),
        "sound_entry_ref_count": sum(sound_entry_counts.values()),
        "resolved_sound_entry_ref_count": sum(sound_entry_counts.values())
        - sum(unresolved_sound_entries.values()),
        "unresolved_sound_entry_ref_count": sum(unresolved_sound_entries.values()),
        "class_counts": dict(sorted(class_counts.items())),
        "kind_counts": dict(sorted(kind_counts.items())),
        "property_counts": dict(prop_counts.most_common()),
        "decoded_array_counts": dict(sorted(decoded_array_counts.items())),
        "normalized_range_counts": dict(sorted(normalized_range_counts.items())),
        "source_default_counts": dict(sorted(source_default_counts.items())),
        "top_texture_refs": dict(texture_counts.most_common(100)),
        "top_unresolved_texture_refs": dict(unresolved_textures.most_common(100)),
        "top_ambient_sound_refs": dict(ambient_sound_counts.most_common(100)),
        "top_unresolved_ambient_sound_refs": dict(unresolved_ambient_sounds.most_common(100)),
        "top_sound_entry_refs": dict(sound_entry_counts.most_common(100)),
        "top_unresolved_sound_entry_refs": dict(unresolved_sound_entries.most_common(100)),
        "templates": templates,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        (
            "WROTE: particle templates=%d renderable=%d textures=%d resolved=%d "
            "unresolved=%d ambient_sounds=%d resolved=%d unresolved=%d out=%s"
        )
        % (
            len(templates),
            renderable_count,
            sum(texture_counts.values()),
            int(manifest["resolved_texture_ref_count"]),
            int(manifest["unresolved_texture_ref_count"]),
            int(manifest["ambient_sound_ref_count"]),
            int(manifest["resolved_ambient_sound_ref_count"]),
            int(manifest["unresolved_ambient_sound_ref_count"]),
            out_path,
        )
    )
    return 0


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def particle_template_id(prefab_name: str, actor_index: int, class_name: str, actor_name: str) -> str:
    source = f"sgo:{prefab_name}:{actor_index}:{class_name}:{actor_name}"
    return "sgo_" + hashlib.sha1(source.encode("utf-8", errors="replace")).hexdigest()[:16]


def classify_emitter(class_name: str) -> str:
    if class_name in SPRITE_CLASSES:
        return "sprite"
    if class_name in BEAM_CLASSES:
        return "beam"
    if class_name in MESH_CLASSES:
        return "mesh"
    if class_name in LIGHT_CLASSES:
        return "light"
    lower = class_name.lower()
    if lower.endswith("fx") or lower.startswith(("fx", "particle", "flame")):
        return "fx_actor"
    return "emitter"


def normalize_props(
    props: dict[str, Any],
    texture_index: dict[str, Any],
    audio_index: dict[str, Any] | None = None,
    class_name: str = "",
) -> dict[str, Any]:
    classless = {
        "renderable": False,
        "texture_ref": "",
        "texture_relative_path": "",
        "texture_output_path": "",
    }
    texture_ref = str(props.get("Texture", "") or "").strip()
    texture_object_ref = props.get("Texture__object_ref")
    if not isinstance(texture_object_ref, dict):
        texture_object_ref = {}
    texture_match = (
        resolve_texture(texture_ref, texture_index, texture_object_ref) if texture_ref else {}
    )
    texture_relative_path = texture_match.get("relative_path", "")
    ribbon_texture_ref = str(props.get("m_RibbonTexture", "") or "").strip()
    ribbon_texture_object_ref = props.get("m_RibbonTexture__object_ref")
    if not isinstance(ribbon_texture_object_ref, dict):
        ribbon_texture_object_ref = {}
    ribbon_texture_match = (
        resolve_texture(ribbon_texture_ref, texture_index, ribbon_texture_object_ref)
        if ribbon_texture_ref
        else {}
    )
    ambient_sound_ref = str(props.get("AmbientSound", "") or "").strip()
    ambient_sound_object_ref = props.get("AmbientSound__object_ref")
    if not isinstance(ambient_sound_object_ref, dict):
        ambient_sound_object_ref = {}
    ambient_sound_match = (
        resolve_sound(ambient_sound_ref, audio_index or {}, ambient_sound_object_ref)
        if ambient_sound_ref or ambient_sound_object_ref
        else {}
    )
    static_mesh_name = first_string(
        props,
        ["StaticMesh", "MeshSpawningStaticMesh"],
    )
    static_mesh_object_ref = first_dict(
        props,
        ["StaticMesh__object_ref", "MeshSpawningStaticMesh__object_ref"],
    )
    normalized: dict[str, Any] = {
        "renderable": bool(texture_relative_path),
        "texture_ref": texture_ref,
        "texture_object_ref": texture_object_ref,
        "texture_object_path": str(texture_object_ref.get("object_path", "")),
        "texture_relative_path": texture_relative_path,
        "texture_output_path": texture_match.get("output_path", ""),
        "texture_resolution": texture_match.get("resolution", ""),
        "max_particles": int_value(props.get("MaxParticles"), PARTICLE_DEFAULT_MAX_PARTICLES),
        "lifetime": normalize_range(props.get("LifetimeRange")),
        "start_size": normalize_range_vector(props.get("StartSizeRange")),
        "start_velocity": normalize_range_vector(props.get("StartVelocityRange")),
        "start_location_range": normalize_range_vector(props.get("StartLocationRange")),
        "start_location_polar_range": normalize_range_vector(props.get("StartLocationPolarRange")),
        "sphere_radius": normalize_range(props.get("SphereRadiusRange")),
        "start_location_offset": normalize_vector(props.get("StartLocationOffset")),
        "actor_location": normalize_vector(props.get("Location")),
        "draw_scale_3d": normalize_vector(props.get("DrawScale3D")),
        "draw_scale": float_or_none(props.get("DrawScale")),
        "opacity": float_or_none(props.get("Opacity")),
        "fade_in": bool(props.get("FadeIn", False)),
        "fade_in_end_time": float_or_none(props.get("FadeInEndTime")),
        "fade_out": bool(props.get("FadeOut", False)),
        "fade_out_start_time": float_or_none(props.get("FadeOutStartTime")),
        "uniform_size": bool(props.get("UniformSize", False)),
        "draw_style": props.get("DrawStyle"),
        "alpha_ref": int_or_none(props.get("AlphaRef")),
        "alpha_test": bool(props.get("AlphaTest", False)),
        "accepts_projectors": bool(props.get("AcceptsProjectors", False)),
        "z_test": bool(props.get("ZTest", False)),
        "z_write": bool(props.get("ZWrite", False)),
        "render_two_sided": bool(props.get("RenderTwoSided", False)),
        "coordinate_system": props.get("CoordinateSystem"),
        "automatic_initial_spawning": bool(props.get("AutomaticInitialSpawning", True)),
        "respawn_dead_particles": bool(props.get("RespawnDeadParticles", True)),
        "initial_particles_per_second": float_or_none(props.get("InitialParticlesPerSecond")),
        "particles_per_second": float_or_none(props.get("ParticlesPerSecond")),
        "initial_delay": normalize_range(props.get("InitialDelayRange")),
        "relative_warmup_time": float_or_none(props.get("RelativeWarmupTime")),
        "warmup_ticks_per_second": float_or_none(props.get("WarmupTicksPerSecond")),
        "texture_u_subdivisions": max(1, int_value(props.get("TextureUSubdivisions"), 1)),
        "texture_v_subdivisions": max(1, int_value(props.get("TextureVSubdivisions"), 1)),
        "subdivision_start": int_value(props.get("SubdivisionStart"), 0),
        "subdivision_end": int_value(props.get("SubdivisionEnd"), 0),
        "blend_between_subdivisions": bool(props.get("BlendBetweenSubdivisions", False)),
        "use_random_subdivision": bool(props.get("UseRandomSubdivision", False)),
        "use_subdivision_scale": bool(props.get("UseSubdivisionScale", False)),
        "subdivision_scale": normalize_subdivision_scale(props.get("SubdivisionScale")),
        "color_scale": normalize_curve(props.get("ColorScale")),
        "color_multiplier": normalize_range_vector(props.get("ColorMultiplierRange")),
        "size_scale": normalize_curve(props.get("SizeScale")),
        "use_size_scale": bool(props.get("UseSizeScale", False)),
        "use_regular_size_scale": bool(props.get("UseRegularSizeScale", True)),
        "velocity_scale": normalize_curve(props.get("VelocityScale")),
        "velocity_scale_range": normalize_range_vector(props.get("VelocityScaleRange")),
        "use_velocity_scale": bool(props.get("UseVelocityScale", False)),
        "acceleration": normalize_vector(props.get("Acceleration")),
        "max_abs_velocity": float_or_none(props.get("MaxAbsVelocity")),
        "velocity_loss": normalize_range_vector(props.get("VelocityLossRange")),
        "line_segments": normalize_range(props.get("LineSegmentsRange")),
        "time_between_segments": normalize_range(props.get("TimeBetweenSegmentsRange")),
        "time_before_visible": normalize_range(props.get("TimeBeforeVisibleRange")),
        "add_location_from_other_emitter": int_or_none(
            props.get("AddLocationFromOtherEmitter")
        ),
        "add_velocity_from_other_emitter": int_or_none(
            props.get("AddVelocityFromOtherEmitter")
        ),
        "start_velocity_radial": normalize_range(props.get("StartVelocityRadialRange")),
        "start_spin": normalize_range_vector(props.get("StartSpinRange")),
        "spin_particles": bool(props.get("SpinParticles", False)),
        "spins_per_second": normalize_range_vector(props.get("SpinsPerSecondRange")),
        "spin_ccw_or_cw": float_or_none(props.get("SpinCCWorCW")),
        "use_revolution": bool(props.get("UseRevolution", False)),
        "use_revolution_source_present": "UseRevolution" in props,
        "revolutions_per_second": normalize_range_vector(props.get("RevolutionsPerSecondRange")),
        "revolution_center_offset": normalize_range_vector(props.get("RevolutionCenterOffsetRange")),
        "use_revolution_scale": bool(props.get("UseRevolutionScale", False)),
        "revolution_scale": normalize_curve(props.get("RevolutionScale")),
        "revolution_scale_repeats": float_or_none(props.get("RevolutionScaleRepeats")),
        "start_location_shape": props.get("StartLocationShape"),
        "ribbon_on": bool(props.get("bRibbonOn", False)),
        "ribbon_width": float_or_none(props.get("RibbonWide")),
        "ribbon_length": float_or_none(props.get("RibbonLength")),
        "ribbon_texture_ref": ribbon_texture_ref,
        "ribbon_texture_object_ref": ribbon_texture_object_ref,
        "ribbon_texture_object_path": str(ribbon_texture_object_ref.get("object_path", "")),
        "ribbon_texture_relative_path": ribbon_texture_match.get("relative_path", ""),
        "ribbon_texture_output_path": ribbon_texture_match.get("output_path", ""),
        "sample_point_timing": float_or_none(props.get("SamplePointTiming")),
        "max_div_per_sample_point": int_or_none(props.get("MaxDivPerSamplePoint")),
        "use_direction_as": int_or_none(props.get("UseDirectionAs")),
        "use_skeletal_location_as": int_or_none(props.get("UseSkeletalLocationAs")),
        "spawn_only_in_direction_of_normal": bool(props.get("SpawnOnlyInDirectionOfNormal", False)),
        "projection_normal": normalize_vector(props.get("ProjectionNormal")),
        "get_velocity_direction_from": int_or_none(props.get("GetVelocityDirectionFrom")),
        "beam_distance": normalize_range(props.get("BeamDistanceRange")),
        "determine_end_point_by": int_or_none(props.get("DetermineEndPointBy")),
        "beam_endpoints": normalize_beam_endpoints(props.get("BeamEndPoints")),
        "beam_texture_u_scale": float_value(props.get("BeamTextureUScale"), 1.0),
        "beam_texture_v_scale": float_value(props.get("BeamTextureVScale"), 1.0),
        "rotating_sheets": int_value(props.get("RotatingSheets"), 1),
        "low_frequency_noise_range": normalize_range_vector(props.get("LowFrequencyNoiseRange")),
        "low_frequency_points": int_or_none(props.get("LowFrequencyPoints")),
        "high_frequency_noise_range": normalize_range_vector(props.get("HighFrequencyNoiseRange")),
        "high_frequency_points": int_or_none(props.get("HighFrequencyPoints")),
        "noise_determines_end_point": bool(props.get("NoiseDeterminesEndPoint", False)),
        "use_low_frequency_scale": bool(props.get("UseLowFrequencyScale", False)),
        "low_frequency_scale_factors": normalize_range_vector(props.get("LFScaleFactors")),
        "high_frequency_scale_factors": normalize_range_vector(props.get("HFScaleFactors")),
        "high_frequency_scale_repeats": float_or_none(props.get("HFScaleRepeats")),
        "use_branching": bool(props.get("UseBranching", False)),
        "branch_probability": float_or_none(props.get("BranchProbability")),
        "branch_emitter": int_or_none(props.get("BranchEmitter")),
        "branch_high_frequency_points": normalize_range(props.get("BranchHFPointsRange")),
        "branch_spawn_amount": normalize_range(props.get("BranchSpawnAmountRange")),
        "linkup_lifetime": bool(props.get("LinkupLifetime", False)),
        "light_radius": float_or_none(props.get("LightRadius")),
        "light_brightness": float_or_none(props.get("LightBrightness")),
        "light_color": normalize_color(props.get("LightColor")),
        "light_emitter_type": props.get("LightEmitterType"),
        "light_emitter_effect": props.get("LightEmitterEffect"),
        "ambient_sound_ref": ambient_sound_ref,
        "ambient_sound_object_ref": ambient_sound_object_ref,
        "ambient_sound_object_path": str(ambient_sound_object_ref.get("object_path", "")),
        "ambient_sound_package": str(ambient_sound_object_ref.get("source_package", "")),
        "ambient_sound_package_path": str(ambient_sound_object_ref.get("package_path", "")),
        "ambient_sound_resolved": bool(ambient_sound_match.get("relative_path", "")),
        "ambient_sound_relative_path": ambient_sound_match.get("relative_path", ""),
        "ambient_sound_output_path": ambient_sound_match.get("output_path", ""),
        "ambient_sound_bank": ambient_sound_match.get("bank", ""),
        "ambient_sound_bank_dir": ambient_sound_match.get("bank_dir", ""),
        "ambient_sound_sample_title": ambient_sound_match.get("title", ""),
        "ambient_sound_duration_seconds": ambient_sound_match.get("duration_seconds"),
        "ambient_sound_resolution": ambient_sound_match.get("resolution", "unresolved"),
        "sound_radius": float_or_none(props.get("SoundRadius")),
        "sound_volume": float_or_none(props.get("SoundVolume")),
        "spawning_sound_index": normalize_range_or_scalar(props.get("SpawningSoundIndex")),
        "spawning_sound_probability": normalize_range_or_scalar(
            props.get("SpawningSoundProbability")
        ),
        "sounds": normalize_sound_entries(props.get("Sounds"), audio_index or {}),
        "static_mesh": static_mesh_name,
        "static_mesh_object_ref": static_mesh_object_ref,
        "static_mesh_object_path": str(static_mesh_object_ref.get("object_path", "")),
        "static_mesh_package": str(static_mesh_object_ref.get("source_package", "")),
        "static_mesh_package_path": str(static_mesh_object_ref.get("package_path", "")),
        "mesh_scale": normalize_range(props.get("MeshScaleRange")),
        "uniform_mesh_scale": bool(props.get("UniformMeshScale", False)),
        "raw_array_counts": raw_array_counts(props),
    }
    normalized["source_defaulted_fields"] = apply_source_defaults(
        normalized, props, class_name
    )
    for key, value in classless.items():
        normalized.setdefault(key, value)
    return normalized


def source_default_entries(class_name: str, props: dict[str, Any]) -> list[tuple[str, str, Any]]:
    if class_name not in PARTICLE_EMITTER_CLASSES:
        return []
    start_size_default = 1.0 if class_name in {"MeshEmitter", "EMFXMeshEmitter"} else 100.0
    entries: list[tuple[str, str, Any]] = [
        ("MaxParticles", "max_particles", PARTICLE_DEFAULT_MAX_PARTICLES),
        ("DrawStyle", "draw_style", PARTICLE_DEFAULT_DRAW_STYLE),
        ("AlphaRef", "alpha_ref", PARTICLE_DEFAULT_ALPHA_REF),
        ("AlphaTest", "alpha_test", PARTICLE_DEFAULT_ALPHA_TEST),
        ("AcceptsProjectors", "accepts_projectors", PARTICLE_DEFAULT_ACCEPTS_PROJECTORS),
        ("ZTest", "z_test", PARTICLE_DEFAULT_Z_TEST),
        ("ZWrite", "z_write", PARTICLE_DEFAULT_Z_WRITE),
        ("LifetimeRange", "lifetime", scalar_range(4.0)),
        ("StartSizeRange", "start_size", scalar_range_vector(start_size_default)),
        ("AutomaticInitialSpawning", "automatic_initial_spawning", True),
        ("RespawnDeadParticles", "respawn_dead_particles", True),
        ("UseRegularSizeScale", "use_regular_size_scale", True),
        ("AddLocationFromOtherEmitter", "add_location_from_other_emitter", -1),
        ("AddVelocityFromOtherEmitter", "add_velocity_from_other_emitter", -1),
        ("ColorMultiplierRange", "color_multiplier", scalar_range_vector(1.0)),
        ("VelocityScaleRange", "velocity_scale_range", scalar_range_vector(1.0)),
    ]
    if class_name == "SparkEmitter":
        entries.append(("LineSegmentsRange", "line_segments", scalar_range(5.0)))
    if class_name in PARTICLE_FORCE_TWO_SIDED_CLASSES:
        entries.append(("RenderTwoSided", "render_two_sided", True))
    if class_name in {"MeshEmitter", "EMFXMeshEmitter"}:
        entries.append(("RenderTwoSided", "render_two_sided", False))
    if class_name == "BeamEmitter":
        entries.extend(
            [
                ("LowFrequencyPoints", "low_frequency_points", 3),
                ("HighFrequencyPoints", "high_frequency_points", 10),
                ("BeamTextureUScale", "beam_texture_u_scale", 1.0),
                ("BeamTextureVScale", "beam_texture_v_scale", 1.0),
                ("BranchEmitter", "branch_emitter", -1),
                (
                    "BranchHFPointsRange",
                    "branch_high_frequency_points",
                    {"min": 0.0, "max": 1000.0},
                ),
            ]
        )
    if bool(props.get("bRibbonOn", False)):
        entries.extend(
            [
                ("SamplePointTiming", "sample_point_timing", 0.06),
                ("MaxDivPerSamplePoint", "max_div_per_sample_point", 4),
                ("RibbonWide", "ribbon_width", 5.0),
                ("RibbonLength", "ribbon_length", 1.0),
            ]
        )
    return entries


def apply_source_defaults(
    normalized: dict[str, Any], props: dict[str, Any], class_name: str
) -> list[str]:
    defaulted: list[str] = []
    for source_key, normalized_key, default_value in source_default_entries(class_name, props):
        if source_key in props:
            continue
        normalized[normalized_key] = default_value
        defaulted.append(normalized_key)
    return defaulted


def first_string(props: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = props.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def first_dict(props: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    for key in keys:
        value = props.get(key)
        if isinstance(value, dict):
            return value
    return {}


def normalize_range(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    min_value = value.get("min", value.get("a", value.get("Min", None)))
    max_value = value.get("max", value.get("b", value.get("Max", None)))
    if not isinstance(min_value, (int, float)) or not isinstance(max_value, (int, float)):
        return None
    return {"min": float(min_value), "max": float(max_value)}


def normalize_range_vector(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for axis in ("x", "y", "z"):
        axis_value = value.get(axis, value.get(axis.upper(), None))
        axis_range = normalize_range(axis_value)
        if axis_range is None:
            return None
        out[axis] = axis_range
    return out


def normalize_vector(value: Any) -> list[float] | None:
    if isinstance(value, list) and len(value) >= 3:
        return [float(value[0]), float(value[1]), float(value[2])]
    if isinstance(value, dict) and all(axis in value for axis in ("x", "y", "z")):
        return [float(value["x"]), float(value["y"]), float(value["z"])]
    return None


def normalize_curve(value: Any) -> list[dict[str, Any]] | dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    elements = value.get("elements")
    if not isinstance(elements, list):
        return {
            "count": int_value(value.get("count"), 0),
            "raw_hex": value.get("raw_hex", ""),
        }
    out: list[dict[str, Any]] = []
    for element_value in elements:
        if not isinstance(element_value, dict):
            continue
        element: dict[str, Any] = {}
        time_value = first_value(element_value, ["RelativeTime", "Time", "InVal", "Min", "Max"])
        if isinstance(time_value, (int, float)):
            element["time"] = float(time_value)
        color = normalize_color(first_value(element_value, ["Color", "RelativeColor"]))
        if color:
            element["color"] = color
        size_value = first_value(element_value, ["RelativeSize", "Size", "Value", "OutVal"])
        if isinstance(size_value, (int, float)):
            element["size"] = float(size_value)
        velocity = normalize_vector(first_value(element_value, ["RelativeVelocity", "Velocity"]))
        if velocity:
            element["velocity"] = velocity
        revolution = normalize_vector(first_value(element_value, ["RelativeRevolution", "Revolution"]))
        if revolution:
            element["revolution"] = revolution
        element["raw"] = element_value
        out.append(element)
    return out


def normalize_subdivision_scale(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_hex = str(value.get("raw_hex", "") or "")
    samples: list[float] = []
    elements = value.get("elements")
    if isinstance(elements, list):
        for element_value in elements:
            sample = float_or_none(element_value)
            if sample is None and isinstance(element_value, dict):
                sample = float_or_none(first_value(element_value, ["Value", "OutVal", "RelativeTime"]))
            if sample is not None:
                samples.append(sample)
    elif raw_hex:
        samples = decode_float_array_payload(raw_hex, int_value(value.get("count"), 0))
    out: dict[str, Any] = {"count": int_value(value.get("count"), len(samples))}
    if samples:
        out["samples"] = samples
        out["count"] = len(samples)
    if raw_hex:
        out["raw_hex"] = raw_hex
    return out


def decode_float_array_payload(raw_hex: str, count: int) -> list[float]:
    try:
        payload = bytes.fromhex(raw_hex)
    except ValueError:
        return []
    if not payload:
        return []
    if count <= 0:
        count = len(payload) // 4
        offset = 0
    elif len(payload) >= 1 + count * 4 and payload[0] == count:
        offset = 1
    elif len(payload) >= 4 + count * 4 and int.from_bytes(payload[:4], "little") == count:
        offset = 4
    elif len(payload) >= count * 4:
        offset = 0
    else:
        return []
    samples: list[float] = []
    for index in range(count):
        sample_offset = offset + index * 4
        if sample_offset + 4 > len(payload):
            break
        samples.append(float(struct.unpack_from("<f", payload, sample_offset)[0]))
    return samples


def normalize_beam_endpoints(value: Any) -> list[dict[str, Any]] | dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    elements = value.get("elements")
    if not isinstance(elements, list):
        return {
            "count": int_value(value.get("count"), 0),
            "raw_hex": value.get("raw_hex", ""),
        }
    out: list[dict[str, Any]] = []
    for element_value in elements:
        if not isinstance(element_value, dict):
            continue
        endpoint: dict[str, Any] = {}
        actor_tag = str(element_value.get("ActorTag", "") or "").strip()
        if actor_tag:
            endpoint["actor_tag"] = actor_tag
        offset = normalize_range_vector(element_value.get("offset"))
        if offset is not None:
            endpoint["offset"] = offset
        weight = float_or_none(element_value.get("Weight"))
        if weight is not None:
            endpoint["weight"] = weight
        endpoint["raw"] = element_value
        out.append(endpoint)
    return out


def normalize_sound_entries(
    value: Any, audio_index: dict[str, Any] | None = None
) -> list[dict[str, Any]] | dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    audio_index = audio_index or {}
    elements = value.get("elements")
    if not isinstance(elements, list):
        return {
            "count": int_value(value.get("count"), 0),
            "raw_hex": value.get("raw_hex", ""),
        }
    out: list[dict[str, Any]] = []
    for element_value in elements:
        if not isinstance(element_value, dict):
            continue
        sound: dict[str, Any] = {}
        sound_ref = str(element_value.get("Sound", "") or "").strip()
        if sound_ref:
            sound["sound_ref"] = sound_ref
        sound_object_ref = element_value.get("Sound__object_ref")
        if isinstance(sound_object_ref, dict):
            sound["sound_object_ref"] = sound_object_ref
            sound["sound_object_path"] = str(sound_object_ref.get("object_path", ""))
            sound["sound_package"] = str(sound_object_ref.get("source_package", ""))
            sound["sound_package_path"] = str(sound_object_ref.get("package_path", ""))
        sound_match = (
            resolve_sound(sound_ref, audio_index, sound_object_ref)
            if sound_ref or isinstance(sound_object_ref, dict)
            else {}
        )
        sound["sound_resolved"] = bool(sound_match.get("relative_path", ""))
        sound["sound_relative_path"] = sound_match.get("relative_path", "")
        sound["sound_output_path"] = sound_match.get("output_path", "")
        sound["sound_bank"] = sound_match.get("bank", "")
        sound["sound_bank_dir"] = sound_match.get("bank_dir", "")
        sound["sound_sample_title"] = sound_match.get("title", "")
        sound["sound_duration_seconds"] = sound_match.get("duration_seconds")
        sound["sound_resolution"] = sound_match.get("resolution", "unresolved")
        for raw_key, normalized_key in (
            ("Radius", "radius"),
            ("Volume", "volume"),
            ("Pitch", "pitch"),
            ("Probability", "probability"),
        ):
            range_value = normalize_range_or_scalar(element_value.get(raw_key))
            if range_value is not None:
                sound[normalized_key] = range_value
        weight = float_or_none(element_value.get("Weight"))
        if weight is not None:
            sound["weight"] = weight
        sound["raw"] = element_value
        out.append(sound)
    return out


def normalize_color(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = ("R", "G", "B", "A") if "R" in value else ("r", "g", "b", "a")
    if not all(key in value for key in keys[:3]):
        return None
    alpha = value.get(keys[3], 255)
    rgba = [int(value[keys[0]]), int(value[keys[1]]), int(value[keys[2]]), int(alpha)]
    return {
        "rgba": rgba,
        "unit": [round(channel / 255.0, 6) for channel in rgba],
    }


def first_value(data: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def raw_array_counts(props: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in props.items():
        if isinstance(value, dict) and "raw_hex" in value:
            out[str(key)] = int_value(value.get("count"), 0)
    return out


def looks_like_range(value: Any) -> bool:
    return normalize_range(value) is not None


def looks_like_range_vector(value: Any) -> bool:
    return normalize_range_vector(value) is not None


def normalize_range_or_scalar(value: Any) -> dict[str, float] | None:
    range_value = normalize_range(value)
    if range_value is not None:
        return range_value
    scalar_value = float_or_none(value)
    if scalar_value is None:
        return None
    return {"min": scalar_value, "max": scalar_value}


def int_value(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_texture_index(textures_root: Path, output_root: Path) -> dict[str, Any]:
    index: dict[str, list[dict[str, str]]] = {}
    files: list[str] = []
    if not textures_root.exists():
        return {"_files": files}
    for path in sorted(textures_root.glob("*")):
        if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        entry = {
            "output_path": str(path),
            "relative_path": relative_path(path, output_root),
            "name": path.name,
        }
        files.append(entry["relative_path"])
        for key in texture_keys(path):
            index.setdefault(key, []).append(entry)
    index["_files"] = files
    return index


def build_audio_index(audio_root: Path, output_root: Path) -> dict[str, Any]:
    index: dict[str, Any] = {"_files": []}
    if not audio_root.exists():
        return index
    for manifest_path in sorted(audio_root.glob("*/manifest.json")):
        try:
            manifest = read_json(manifest_path)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(manifest, dict):
            continue
        samples = manifest.get("samples", [])
        if not isinstance(samples, list):
            continue
        bank_dir = manifest_path.parent.name
        bank = str(manifest.get("bank", "") or "")
        bank_aliases = audio_bank_aliases(bank, bank_dir, manifest.get("bank_path"))
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            output_path_text = str(sample.get("output_path", "") or "")
            if not output_path_text:
                continue
            output_path = Path(output_path_text)
            title = str(sample.get("title", "") or sample.get("display_name", "") or "")
            sample_key = audio_sample_key(title)
            if not sample_key:
                continue
            entry = {
                "output_path": str(output_path),
                "relative_path": relative_path(output_path, output_root),
                "bank": bank,
                "bank_dir": bank_dir,
                "title": title,
                "duration_seconds": float_or_none(sample.get("duration_seconds")),
                "channels": int_or_none(sample.get("channels")),
                "sample_rate": int_or_none(sample.get("sample_rate")),
                "resolution": "exact-bank-sample",
            }
            index["_files"].append(entry["relative_path"])
            for bank_alias in bank_aliases:
                bank_samples = index.setdefault(bank_alias, {})
                bank_samples.setdefault(sample_key, []).append(entry)
    return index


def audio_bank_aliases(bank: str, bank_dir: str, bank_path: Any) -> set[str]:
    aliases: set[str] = set()
    for value in (bank, bank_dir, str(bank_path or "")):
        text = str(value or "").strip()
        if not text:
            continue
        stem = Path(text).stem if "." in Path(text).name else text
        aliases.add(audio_bank_key(stem))
        aliases.add(audio_bank_key(Path(text).name))
        if "__" in text:
            aliases.add(audio_bank_key(text.split("__")[-1]))
    return {alias for alias in aliases if alias}


def audio_bank_key(value: str) -> str:
    text = value.strip().lower()
    if text.endswith(".isb"):
        text = text[:-4]
    if text.startswith("sounds__"):
        text = text[len("sounds__"):]
    return text


def audio_package_bank_keys(package: str, package_path: str, object_path: str) -> list[str]:
    keys: list[str] = []
    for value in (package, package_path.split(".")[0] if package_path else "", object_path.split(".")[0]):
        text = str(value or "").strip()
        if not text:
            continue
        candidates = [text]
        if text.lower().endswith("sounds"):
            candidates.append(text[:-6])
        for candidate in candidates:
            key = audio_bank_key(candidate)
            if key and key not in keys:
                keys.append(key)
    return keys


def audio_sample_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    stem = Path(text).stem
    if stem.lower().endswith(".wav"):
        stem = stem[:-4]
    return stem.lower()


def resolve_sound(
    sound_ref: str, audio_index: dict[str, Any], object_ref: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not audio_index:
        return {}
    ref = str(sound_ref or "").strip()
    object_ref = object_ref if isinstance(object_ref, dict) else {}
    object_path = str(object_ref.get("object_path", "") or "").strip()
    package_path = str(object_ref.get("package_path", "") or "").strip()
    package = str(object_ref.get("source_package", "") or "").strip()
    if not object_path and ref:
        object_path = ref
    object_name = object_path.split(".")[-1] if object_path else Path(ref).stem
    sample_key = audio_sample_key(object_name)
    if not sample_key:
        return {}
    for bank_key in audio_package_bank_keys(package, package_path, object_path):
        bank_samples = audio_index.get(bank_key)
        if not isinstance(bank_samples, dict):
            continue
        matches = bank_samples.get(sample_key)
        if isinstance(matches, list) and matches:
            return min(matches, key=lambda item: len(str(item.get("relative_path", ""))))
    return {}


def normalized_sound_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def sound_ref_key(object_path: Any, sound_ref: Any) -> str:
    object_text = str(object_path or "").strip()
    if object_text:
        return object_text
    return str(sound_ref or "").strip()


def texture_keys(path: Path) -> set[str]:
    stem = path.stem
    keys = {stem.lower(), path.name.lower()}
    if stem.lower().startswith("normal__"):
        keys.add(stem[len("normal__"):].lower())
    parts = stem.split("__")
    for part in parts:
        if part:
            keys.add(part.lower())
    if parts:
        keys.add(parts[-1].lower())
        keys.add(".".join(parts).lower())
        for start in range(len(parts)):
            suffix_parts = parts[start:]
            keys.add("__".join(suffix_parts).lower())
            keys.add(".".join(suffix_parts).lower())
    return keys


def resolve_texture(
    texture_ref: str, texture_index: dict[str, Any], object_ref: dict[str, Any] | None = None
) -> dict[str, str]:
    ref = texture_ref.strip()
    if not ref:
        return {}
    candidates: list[str] = []
    qualified = False
    if isinstance(object_ref, dict):
        object_path = str(object_ref.get("object_path", "") or "").strip()
        package_path = str(object_ref.get("package_path", "") or "").strip()
        package_chain = object_ref.get("package_chain", [])
        if object_path:
            candidates.extend([object_path, object_path.replace(".", "__")])
            qualified = "." in object_path or "__" in object_path
        if package_path:
            candidates.extend([
                f"{package_path}.{ref}",
                f"{package_path.replace('.', '__')}__{ref}",
            ])
            qualified = True
        if isinstance(package_chain, list) and package_chain:
            chain = [str(part) for part in package_chain if str(part)]
            if chain:
                candidates.extend([
                    ".".join([*chain, ref]),
                    "__".join([*chain, ref]),
                ])
                qualified = True
    if not qualified:
        candidates.extend([ref, Path(ref).name, Path(ref).stem])
        for suffix in ("_CLR", "_C", "_BMP"):
            candidates.append(ref + suffix)
    normalized_candidates = []
    for candidate in candidates:
        if not candidate:
            continue
        normalized = candidate.lower()
        if normalized not in normalized_candidates:
            normalized_candidates.append(normalized)
    for candidate in normalized_candidates:
        matches = texture_index.get(candidate)
        if isinstance(matches, list) and matches:
            return min(matches, key=lambda item: len(str(item.get("relative_path", ""))))
    return {}


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
