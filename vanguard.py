#!/usr/bin/env python3
"""Command router for the Vanguard asset extraction scripts."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


UNREAL_LIBRARY_REPO = "https://github.com/EliotVU/Unreal-Library.git"
DEFAULT_UNREAL_LIBRARY_DIR = ROOT / "external" / "Unreal-Library"


def run(label: str, command: list[str], env: dict[str, str]) -> None:
    print(f"\n==> {label}")
    print("    " + " ".join(command))
    sys.stdout.flush()
    sys.stderr.flush()
    result = subprocess.run(command, cwd=ROOT, env=env)
    if result.returncode != 0:
        raise SystemExit(f"{label} failed with exit code {result.returncode}")


def add_path_options(parser: argparse.ArgumentParser) -> None:
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


def build_env(args: argparse.Namespace) -> dict[str, str]:
    assets = Path(args.assets).expanduser().resolve()
    emu_root = Path(args.emu_root).expanduser().resolve()

    if not assets.exists():
        raise SystemExit(f"Assets directory not found: {assets}")

    env = os.environ.copy()
    env["VANGUARD_ASSETS_PATH"] = str(assets)
    env["VANGUARD_ASSETS"] = str(assets)
    env["VANGUARD_EMU_PATH"] = str(emu_root)
    env.setdefault("UNREAL_LIBRARY_DLL", config.UNREAL_LIBRARY_DLL)
    return env


def cmd_setup(args: argparse.Namespace) -> None:
    env = build_env(args)
    command = [sys.executable, "scripts/setup_assets.py"]
    for attr, flag in [
        ("reset", "--reset"),
        ("full", "--full"),
        ("skip_core", "--skip-core"),
        ("db", "--db"),
        ("files", "--files"),
        ("chunks", "--chunks"),
        ("mesh_index", "--mesh-index"),
        ("textures", "--textures"),
        ("properties", "--properties"),
        ("shader_map", "--shader-map"),
        ("terrain", "--terrain"),
        ("meshes", "--meshes"),
        ("sgo", "--sgo"),
        ("objects", "--objects"),
    ]:
        if getattr(args, attr, False):
            command.append(flag)
    if args.chunk:
        command.extend(["--chunk", args.chunk])
    if args.limit:
        command.extend(["--limit", str(args.limit)])
    run("Set up database and indexes", command, env)


def cmd_extract_all(args: argparse.Namespace) -> None:
    env = build_env(args)
    command = [
        sys.executable,
        "extract_all_assets.py",
        "--assets",
        env["VANGUARD_ASSETS_PATH"],
        "--emu-root",
        env["VANGUARD_EMU_PATH"],
    ]
    if args.sections:
        command.append("--sections")
        command.extend(args.sections)
    if args.no_reset:
        command.append("--no-reset")
    if args.limit_meshes:
        command.extend(["--limit-meshes", str(args.limit_meshes)])
    if args.skip_unreal_library:
        command.append("--skip-unreal-library")
    if args.keep_going:
        command.append("--keep-going")
    run("Run extraction pipeline", command, env)


def cmd_build_shaders(args: argparse.Namespace) -> None:
    env = build_env(args)
    run(
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
    run(
        "Build shader texture map",
        [
            sys.executable,
            "scripts/extractors/build_shader_texture_map.py",
            "--from-material-manifest",
        ],
        env,
    )


def cmd_extract_terrain(args: argparse.Namespace) -> None:
    env = build_env(args)
    command = [sys.executable, "scripts/extractors/extract_all_terrain.py"]
    if args.chunk:
        command.extend(["--chunk", args.chunk])
    else:
        command.append("--all")
    if args.hd:
        command.append("--hd")
    if args.tiles:
        command.append("--tiles")
    if args.texture_only:
        command.append("--texture-only")
    run("Extract terrain", command, env)


def cmd_export_meshes(args: argparse.Namespace) -> None:
    env = build_env(args)
    command = [sys.executable, "scripts/extractors/staticmesh_pipeline.py"]
    if args.file:
        command.extend(["--file", args.file])
    if args.limit:
        command.extend(["--limit", str(args.limit)])
    if args.trees:
        command.append("--trees")
    if args.runtime_leaf_hybrids:
        command.append("--runtime-leaf-hybrids")
    run("Export static meshes", command, env)


def cmd_export_characters(args: argparse.Namespace) -> None:
    env = build_env(args)
    command = [sys.executable, "scripts/exporters/export_character_meshes.py"]
    if args.filter:
        command.extend(["--filter", args.filter])
    run("Export character meshes", command, env)
    run("Decode item attachment mesh references", [sys.executable, "scripts/extractors/decode_items.py"], env)
    run("Generate playable race data", [sys.executable, "scripts/generators/generate_playable_races.py"], env)

    customization_file = Path(env["VANGUARD_EMU_PATH"]) / "bin" / "Resources" / "Texts" / "customization_data.txt"
    if customization_file.exists():
        run(
            "Generate customization slider data",
            [
                sys.executable,
                "scripts/generators/generate_customization_data.py",
                "--emu-root",
                env["VANGUARD_EMU_PATH"],
            ],
            env,
        )
        run(
            "Export playable facial controls",
            [sys.executable, "scripts/exporters/export_playable_facial_controls.py"],
            env,
        )
    else:
        print(f"    Skipping customization data; missing {customization_file}")
        print("    Skipping playable facial controls; customization data is required")


def cmd_export_animations(args: argparse.Namespace) -> None:
    env = build_env(args)
    run("Export EMotion FX animations", [sys.executable, "scripts/exporters/export_emfx_animations.py"], env)
    run("Export UE2 skeletal animations", [sys.executable, "scripts/exporters/export_animations.py"], env)


def cmd_export_facial_controls(args: argparse.Namespace) -> None:
    env = build_env(args)
    run(
        "Export playable facial controls",
        [sys.executable, "scripts/exporters/export_playable_facial_controls.py"],
        env,
    )


def cmd_export_npc_assembly(args: argparse.Namespace) -> None:
    env = build_env(args)
    run(
        "Export actor race visual map",
        [sys.executable, "scripts/exporters/export_actor_race_visual_map.py"],
        env,
    )
    run(
        "Export object race mesh map",
        [sys.executable, "scripts/exporters/export_object_race_mesh_map.py"],
        env,
    )
    run(
        "Build race prefix map",
        [sys.executable, "scripts/exporters/build_race_prefix_map.py"],
        env,
    )
    run(
        "Export NPC assembly data",
        [sys.executable, "scripts/exporters/export_npc_assembly.py"],
        env,
    )


def cmd_extract_audio(args: argparse.Namespace) -> None:
    env = build_env(args)
    assets = env["VANGUARD_ASSETS_PATH"]
    run(
        "Extract embedded UAX WAV files",
        [sys.executable, "scripts/extractors/extract_uax_wav.py", assets, "--glob", "*.uax"],
        env,
    )
    run(
        "Extract ISB Ogg samples",
        [sys.executable, "scripts/extractors/extract_isb.py", assets, "--glob", "*.isb"],
        env,
    )
    run(
        "Dump ICB cue metadata",
        [sys.executable, "scripts/extractors/dump_icb.py", assets, "--glob", "*.icb"],
        env,
    )


def cmd_extract_world(args: argparse.Namespace) -> None:
    env = build_env(args)
    sgo_path = Path(env["VANGUARD_ASSETS_PATH"]) / "Archives" / "binaryprefabs.sgo"
    if not sgo_path.exists():
        raise SystemExit(f"SGO archive not found: {sgo_path}")

    run(
        "Parse SGO prefab mesh/light templates",
        [sys.executable, "scripts/extractors/parse_sgo_prefabs.py", "--sgo", str(sgo_path)],
        env,
    )
    run(
        "Dump raw SGO actor records",
        [sys.executable, "scripts/extractors/dump_sgo_raw.py", "--sgo", str(sgo_path)],
        env,
    )
    run("Split SGO actors by class", [sys.executable, "scripts/extractors/split_sgo_by_class.py"], env)
    run("Fold SGO extras into prefabs", [sys.executable, "scripts/extractors/fold_actors_into_prefabs.py"], env)

    if args.generate_objects:
        run(
            "Generate chunk object placement glTF files",
            [sys.executable, "scripts/generators/generate_objects_from_txt.py", "--all"],
            env,
        )


def cmd_fetch_unreal_library(args: argparse.Namespace) -> None:
    env = os.environ.copy()
    target = Path(args.dir).expanduser().resolve()

    if target.exists():
        if not (target / ".git").exists():
            raise SystemExit(f"Target exists but is not a git repo: {target}")
        if args.update:
            run(
                "Update Unreal-Library",
                ["git", "-C", str(target), "pull", "--ff-only"],
                env,
            )
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        run(
            "Clone Unreal-Library",
            ["git", "clone", "--depth", "1", UNREAL_LIBRARY_REPO, str(target)],
            env,
        )

    dll_path = target / "CLI" / "bin" / "Debug" / "net8.0" / "Eliot.UELib.CLI.dll"
    if not args.no_build:
        csproj = target / "CLI" / "Eliot.UELib.CLI.csproj"
        build_target = str(csproj if csproj.exists() else target / "CLI")
        run(
            "Build Unreal-Library CLI",
            [config.DOTNET, "build", build_target, "-c", "Debug"],
            env,
        )

    print(f"\nUnreal-Library CLI path:\n  {dll_path}")
    if not dll_path.exists():
        print(
            "Build output was not found at the expected path. "
            "Set UNREAL_LIBRARY_DLL if the CLI builds elsewhere."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Vanguard: Saga of Heroes asset extraction commands."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="Initialize the database and asset indexes")
    add_path_options(setup)
    setup.add_argument("--reset", action="store_true", help="Delete and rebuild the SQLite database")
    setup.add_argument("--full", action="store_true", help="Run full world extraction stages")
    setup.add_argument("--skip-core", action="store_true", help="Skip core setup stages when a DB already exists")
    setup.add_argument("--db", action="store_true", help="Initialize the SQLite database")
    setup.add_argument("--files", action="store_true", help="Index client asset files")
    setup.add_argument("--chunks", action="store_true", help="Export chunk/package metadata")
    setup.add_argument("--mesh-index", action="store_true", help="Index StaticMesh exports")
    setup.add_argument("--textures", action="store_true", help="Build texture database")
    setup.add_argument("--properties", action="store_true", help="Extract UObject properties")
    setup.add_argument("--shader-map", action="store_true", help="Build material and shader texture maps")
    setup.add_argument("--terrain", action="store_true", help="Extract terrain data")
    setup.add_argument("--meshes", action="store_true", help="Export static meshes")
    setup.add_argument("--sgo", action="store_true", help="Rebuild SGO prefab sidecars")
    setup.add_argument("--objects", action="store_true", help="Generate object placement sidecars")
    setup.add_argument("--chunk", help="Limit chunk-scoped setup stages to one chunk")
    setup.add_argument("--limit", type=int, default=0, help="Limit StaticMesh package count")
    setup.set_defaults(func=cmd_setup)

    extract_all = subparsers.add_parser("extract-all", help="Run the full extraction pipeline")
    add_path_options(extract_all)
    extract_all.add_argument(
        "--sections",
        nargs="+",
        choices=["all", "core", "world", "characters", "animations", "audio"],
        help="Pipeline sections to run",
    )
    extract_all.add_argument("--no-reset", action="store_true", help="Do not delete/rebuild the SQLite DB")
    extract_all.add_argument("--limit-meshes", type=int, default=0, help="Limit static mesh packages during testing")
    extract_all.add_argument("--skip-unreal-library", action="store_true", help="Skip Unreal-Library-dependent steps")
    extract_all.add_argument("--keep-going", action="store_true", help="Continue past non-critical failures")
    extract_all.set_defaults(func=cmd_extract_all)

    build_shaders = subparsers.add_parser("build-shaders", help="Build shader to diffuse texture mappings")
    add_path_options(build_shaders)
    build_shaders.set_defaults(func=cmd_build_shaders)

    terrain = subparsers.add_parser("extract-terrain", help="Extract terrain from .vgr chunk files")
    add_path_options(terrain)
    terrain.add_argument("--chunk", help="Process one chunk by name, such as chunk_n15_n9")
    terrain.add_argument("--hd", action="store_true", help="Extract high-detail stitched terrain tiles")
    terrain.add_argument("--tiles", action="store_true", help="Export HD tiles for LOD streaming")
    terrain.add_argument("--texture-only", action="store_true", help="Only extract terrain color textures")
    terrain.set_defaults(func=cmd_extract_terrain)

    meshes = subparsers.add_parser("export-meshes", help="Export static meshes to glTF")
    add_path_options(meshes)
    meshes.add_argument("--file", help='Specific file pattern to process, such as "Ra44*.usx"')
    meshes.add_argument("--limit", type=int, default=0, help="Limit number of files to process")
    meshes.add_argument("--trees", action="store_true", help="Only process tree meshes")
    meshes.add_argument("--runtime-leaf-hybrids", action="store_true", help="Write SpeedTree runtime leaf hybrid assets")
    meshes.set_defaults(func=cmd_export_meshes)

    characters = subparsers.add_parser("export-characters", help="Export character meshes and race metadata")
    add_path_options(characters)
    characters.add_argument("--filter", help="Only export UEM files whose filename contains this string")
    characters.set_defaults(func=cmd_export_characters)

    animations = subparsers.add_parser("export-animations", help="Export EMotion FX and UE2 skeletal animations")
    add_path_options(animations)
    animations.set_defaults(func=cmd_export_animations)

    facial_controls = subparsers.add_parser(
        "export-facial-controls",
        help="Export playable facial-control sidecars from extracted character data",
    )
    add_path_options(facial_controls)
    facial_controls.set_defaults(func=cmd_export_facial_controls)

    npc_assembly = subparsers.add_parser(
        "export-npc-assembly",
        help="Export legacy NPC assembly/race visual lookup sidecars",
    )
    add_path_options(npc_assembly)
    npc_assembly.set_defaults(func=cmd_export_npc_assembly)

    audio = subparsers.add_parser("extract-audio", help="Extract UAX, ISB, and ICB audio data")
    add_path_options(audio)
    audio.set_defaults(func=cmd_extract_audio)

    world = subparsers.add_parser("extract-world", help="Extract SGO prefab/world placement data")
    add_path_options(world)
    world.add_argument(
        "--generate-objects",
        action="store_true",
        help="Also generate chunk object placement glTF files from reference text",
    )
    world.set_defaults(func=cmd_extract_world)

    unreal_library = subparsers.add_parser(
        "fetch-unreal-library",
        help="Clone and build the optional Unreal-Library CLI helper",
    )
    unreal_library.add_argument(
        "--dir",
        default=str(DEFAULT_UNREAL_LIBRARY_DIR),
        help="Directory where Unreal-Library should be cloned",
    )
    unreal_library.add_argument(
        "--update",
        action="store_true",
        help="Run git pull --ff-only when the clone already exists",
    )
    unreal_library.add_argument(
        "--no-build",
        action="store_true",
        help="Clone/update only; do not run dotnet build",
    )
    unreal_library.set_defaults(func=cmd_fetch_unreal_library)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
