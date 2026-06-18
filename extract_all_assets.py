#!/usr/bin/env python3
"""Run the extraction pipeline from a fresh clone."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def validate_command(command: list[str]) -> None:
    """Catch missing project scripts before a long extraction run starts."""
    if len(command) < 2 or not command[1].endswith(".py"):
        return
    script_path = ROOT / command[1]
    if not script_path.exists():
        raise SystemExit(f"Pipeline script not found: {script_path}")


def run_step(label: str, command: list[str], env: dict[str, str], allow_fail: bool = False) -> bool:
    print(f"\n==> {label}")
    print("    " + " ".join(command))
    validate_command(command)
    if env.get("VANGUARD_EXTRACT_DRY_RUN") == "1":
        print("    dry-run: not executed")
        return True
    result = subprocess.run(command, cwd=ROOT, env=env)
    if result.returncode != 0:
        message = f"{label} failed with exit code {result.returncode}"
        if allow_fail:
            print(f"    WARNING: {message}")
            return False
        raise SystemExit(message)
    return True


def has_unreal_library(env: dict[str, str]) -> bool:
    dll = Path(env.get("UNREAL_LIBRARY_DLL", config.UNREAL_LIBRARY_DLL)).expanduser()
    return dll.exists()


def run_core(args: argparse.Namespace, env: dict[str, str]) -> None:
    if not args.skip_unreal_library and has_unreal_library(env):
        run_step(
            "Dump Unreal-Library map reference text",
            [sys.executable, "scripts/extractors/bulk_extract_chunk_data.py"],
            env,
        )
        env["VANGUARD_REFERENCE_MAPS_PLANNED"] = "1"
        run_step(
            "Build material manifest",
            [
                sys.executable,
                "scripts/extractors/build_material_manifest.py",
                "--progress-every",
                "500",
                "--flush-every",
                "100",
            ],
            env,
        )
        run_step(
            "Build shader-to-texture map and extract PNG textures",
            [sys.executable, "scripts/extractors/build_shader_texture_map.py", "--from-material-manifest"],
            env,
        )
    else:
        print("\n==> Skipping Unreal-Library-dependent reference and shader extraction")
        print("    Set UNREAL_LIBRARY_DLL to Eliot.UELib.CLI.dll to enable this step.")

    setup_cmd = [sys.executable, "scripts/setup_assets.py"]
    if args.reset:
        setup_cmd.append("--reset")
    run_step("Initialize database and core indexes", setup_cmd, env)

    extract_cmd = [sys.executable, "scripts/setup_assets.py", "--skip-core", "--terrain", "--meshes"]
    if args.limit_meshes:
        extract_cmd.extend(["--limit", str(args.limit_meshes)])
    run_step("Extract terrain and static meshes", extract_cmd, env)


def run_world(args: argparse.Namespace, env: dict[str, str]) -> None:
    sgo_path = Path(env["VANGUARD_ASSETS_PATH"]) / "Archives" / "binaryprefabs.sgo"
    if not sgo_path.exists():
        print(f"\n==> Skipping world prefab extraction; missing {sgo_path}")
        return

    run_step(
        "Parse SGO prefab mesh/light templates",
        [sys.executable, "scripts/extractors/parse_sgo_prefabs.py", "--sgo", str(sgo_path)],
        env,
    )
    run_step(
        "Dump raw SGO actor records",
        [sys.executable, "scripts/extractors/dump_sgo_raw.py", "--sgo", str(sgo_path)],
        env,
    )
    run_step("Split SGO actors by class", [sys.executable, "scripts/extractors/split_sgo_by_class.py"], env)
    run_step("Fold SGO extras into prefabs", [sys.executable, "scripts/extractors/fold_actors_into_prefabs.py"], env)
    run_step("Extract particle texture refs", [sys.executable, "scripts/extractors/extract_particle_textures.py"], env)
    run_step(
        "Generate particle emitter manifest",
        [sys.executable, "scripts/generators/generate_particle_manifest.py"],
        env,
    )

    reference_maps = Path(config.REFERENCE_MAPS_DIR)
    if reference_maps.exists() or env.get("VANGUARD_REFERENCE_MAPS_PLANNED") == "1":
        run_step(
            "Generate chunk object placement glTF files",
            [sys.executable, "scripts/generators/generate_objects_from_txt.py", "--all"],
            env,
            allow_fail=args.keep_going,
        )
        run_step(
            "Generate particle emitter cell indexes",
            [sys.executable, "scripts/generators/generate_particle_cell_index.py", "--all"],
            env,
            allow_fail=args.keep_going,
        )
    else:
        print(f"    Skipping object placement glTFs; missing {reference_maps}")


def run_characters(args: argparse.Namespace, env: dict[str, str]) -> None:
    run_step("Export character meshes", [sys.executable, "scripts/exporters/export_character_meshes.py"], env)
    run_step("Decode item attachment mesh references", [sys.executable, "scripts/extractors/decode_items.py"], env)
    run_step("Generate playable race data", [sys.executable, "scripts/generators/generate_playable_races.py"], env)

    emu_root = Path(env["VANGUARD_EMU_PATH"])
    customization_file = emu_root / "bin" / "Resources" / "Texts" / "customization_data.txt"
    if customization_file.exists():
        run_step(
            "Generate customization slider data",
            [sys.executable, "scripts/generators/generate_customization_data.py", "--emu-root", str(emu_root)],
            env,
        )
        run_step(
            "Export playable facial controls",
            [sys.executable, "scripts/exporters/export_playable_facial_controls.py"],
            env,
        )
    else:
        print(f"    Skipping customization data; missing {customization_file}")
        print("    Skipping playable facial controls; customization data is required")

def run_animations(args: argparse.Namespace, env: dict[str, str]) -> None:
    emfx_command = [sys.executable, "scripts/exporters/export_emfx_animations.py"]
    if args.emfx_workers != 1:
        emfx_command.extend(["--workers", str(args.emfx_workers)])
    if args.clean_emfx:
        emfx_command.append("--clean")
    run_step("Export EMotion FX animations", emfx_command, env)
    run_step("Export UE2 skeletal animations", [sys.executable, "scripts/exporters/export_animations.py"], env)


def run_npc_assembly(args: argparse.Namespace, env: dict[str, str]) -> None:
    run_step(
        "Export actor race visual map",
        [sys.executable, "scripts/exporters/export_actor_race_visual_map.py"],
        env,
    )
    run_step(
        "Export object race mesh map",
        [sys.executable, "scripts/exporters/export_object_race_mesh_map.py"],
        env,
    )
    race_prefix_cmd = [sys.executable, "scripts/exporters/build_race_prefix_map.py"]
    assembly_cmd = [sys.executable, "scripts/exporters/export_npc_assembly.py"]
    if args.npc_snapshot:
        race_prefix_cmd.extend(["--npc-snapshot", args.npc_snapshot])
        assembly_cmd.extend(["--npc-snapshot", args.npc_snapshot])
    run_step(
        "Build race prefix map",
        race_prefix_cmd,
        env,
    )
    run_step(
        "Export NPC assembly data",
        assembly_cmd,
        env,
    )


def run_audio(args: argparse.Namespace, env: dict[str, str]) -> None:
    assets = env["VANGUARD_ASSETS_PATH"]
    run_step(
        "Extract embedded UAX WAV files",
        [sys.executable, "scripts/extractors/extract_uax_wav.py", assets, "--glob", "*.uax"],
        env,
        allow_fail=args.keep_going,
    )
    run_step(
        "Extract ISB Ogg samples",
        [sys.executable, "scripts/extractors/extract_isb.py", assets, "--glob", "*.isb"],
        env,
        allow_fail=args.keep_going,
    )
    run_step(
        "Dump ICB cue metadata",
        [sys.executable, "scripts/extractors/dump_icb.py", assets, "--glob", "*.icb"],
        env,
        allow_fail=args.keep_going,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--assets",
        default=config.ASSETS_PATH,
        help="Path to the Vanguard EMU Assets directory",
    )
    parser.add_argument(
        "--emu-root",
        default=str(config.VANGUARD_EMU_ROOT),
        help="Path to the Vanguard EMU root directory",
    )
    parser.add_argument(
        "--sections",
        nargs="+",
        default=["all"],
        choices=["all", "core", "world", "characters", "animations", "audio", "npc"],
        help="Pipeline sections to run",
    )
    parser.add_argument("--no-reset", dest="reset", action="store_false", help="Do not delete/rebuild the SQLite DB")
    parser.add_argument("--limit-meshes", type=int, default=0, help="Limit static mesh packages during testing")
    parser.add_argument(
        "--emfx-workers",
        type=int,
        default=1,
        help="Worker processes for EMotion FX animation export; 0 uses all CPUs.",
    )
    parser.add_argument(
        "--clean-emfx",
        action="store_true",
        help="Delete output/meshes/emfx_animations before exporting EMotion FX animations.",
    )
    parser.add_argument("--skip-unreal-library", action="store_true", help="Skip steps that require Unreal-Library")
    parser.add_argument(
        "--include-npc-assembly",
        action="store_true",
        help="Include NPC assembly sidecars in an all-sections run",
    )
    parser.add_argument("--npc-snapshot", help="Override committed NPC snapshot JSON for NPC assembly stages")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without running child stages")
    parser.add_argument("--keep-going", action="store_true", help="Continue past non-critical section failures")
    parser.set_defaults(reset=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    assets = Path(args.assets).expanduser().resolve()
    emu_root = Path(args.emu_root).expanduser().resolve()

    if not assets.exists():
        raise SystemExit(f"Assets directory not found: {assets}")

    env = os.environ.copy()
    env["VANGUARD_ASSETS_PATH"] = str(assets)
    env["VANGUARD_ASSETS"] = str(assets)
    env["VANGUARD_EMU_PATH"] = str(emu_root)
    env.setdefault("UNREAL_LIBRARY_DLL", config.UNREAL_LIBRARY_DLL)
    if args.dry_run:
        env["VANGUARD_EXTRACT_DRY_RUN"] = "1"

    sections = set(args.sections)
    if "all" in sections:
        sections = {"core", "world", "characters", "animations", "audio"}
        if args.include_npc_assembly:
            sections.add("npc")

    if "core" in sections:
        run_core(args, env)
    if "world" in sections:
        run_world(args, env)
    if "characters" in sections:
        run_characters(args, env)
    if "animations" in sections:
        run_animations(args, env)
    if "npc" in sections:
        run_npc_assembly(args, env)
    if "audio" in sections:
        run_audio(args, env)

    if args.dry_run:
        print("\nDry run complete. No child extraction stages were executed.")
    else:
        print("\nExtraction complete. Outputs are under ./output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
