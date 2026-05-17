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


def run_step(label: str, command: list[str], env: dict[str, str], allow_fail: bool = False) -> bool:
    print(f"\n==> {label}")
    print("    " + " ".join(command))
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
        run_step(
            "Build shader-to-texture map and extract PNG textures",
            [sys.executable, "scripts/extractors/build_shader_texture_map.py", "--resume"],
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

    reference_maps = Path(config.REFERENCE_MAPS_DIR)
    if reference_maps.exists():
        run_step(
            "Generate chunk object placement glTF files",
            [sys.executable, "scripts/generators/generate_objects_from_txt.py", "--all"],
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
    else:
        print(f"    Skipping customization data; missing {customization_file}")

def run_animations(env: dict[str, str]) -> None:
    run_step("Export EMotion FX animations", [sys.executable, "scripts/exporters/export_emfx_animations.py"], env)
    run_step("Export UE2 skeletal animations", [sys.executable, "scripts/exporters/export_animations.py"], env)


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
        choices=["all", "core", "world", "characters", "animations", "audio"],
        help="Pipeline sections to run",
    )
    parser.add_argument("--no-reset", dest="reset", action="store_false", help="Do not delete/rebuild the SQLite DB")
    parser.add_argument("--limit-meshes", type=int, default=0, help="Limit static mesh packages during testing")
    parser.add_argument("--skip-unreal-library", action="store_true", help="Skip steps that require Unreal-Library")
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

    sections = set(args.sections)
    if "all" in sections:
        sections = {"core", "world", "characters", "animations", "audio"}

    if "core" in sections:
        run_core(args, env)
    if "world" in sections:
        run_world(args, env)
    if "characters" in sections:
        run_characters(args, env)
    if "animations" in sections:
        run_animations(env)
    if "audio" in sections:
        run_audio(args, env)

    print("\nExtraction complete. Outputs are under ./output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
