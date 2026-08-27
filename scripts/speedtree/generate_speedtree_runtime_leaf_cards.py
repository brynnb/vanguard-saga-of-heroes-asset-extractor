#!/usr/bin/env python3
"""Recover exact leaf-card geometry through the original SpeedTree runtime.

Spt2Fbx calls SpeedTreeRT::GetGeometry and stores the resulting card width,
height, center, pivot, dimming, and explicit vertices in FBX channels.  This
script extracts Vanguard's embedded .spt payloads, runs that external bridge,
and publishes deterministic JSON sidecars consumed by staticmesh_pipeline.py.

The converter and proprietary runtime are deliberately not distributed by
this repository.  Put Spt2Fbx.exe and a compatible SpeedTreeRT.dll together
and pass --converter (or set SPT2FBX_EXE).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPTS_DIR.parent

from vanguard_assets import config
PROJECT_ROOT = config.PROJECT_ROOT
from scripts.lib.speedtree_staticmesh import has_embedded_speedtree_payload
from scripts.speedtree.inspect_speedtree_spt import dump_spt_payload, find_spt_header
from scripts.speedtree.reconstruct_spt2fbx_leaf_cards import build_leaf_cards
from ue2 import UE2Package


DEFAULT_OUTPUT = Path(config.DATA_DIR) / "speedtree_runtime_leaf_cards"
DEFAULT_WORK = Path(config.OUTPUT_DIR) / "work" / "speedtree_runtime"
PROTON_CANDIDATE = (
    Path.home()
    / ".local/share/Steam/steamapps/common/Proton - Experimental/proton"
)


def wine_path(path: Path) -> str:
    return "Z:" + str(path.resolve()).replace("/", "\\")


def required_meshes_from_artifact(path: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    object_root = path / "objects"
    if not object_root.is_dir():
        raise FileNotFoundError(f"object artifact has no objects/ directory: {path}")
    for mesh_path in object_root.rglob("*.glb"):
        result.setdefault(mesh_path.parent.name.casefold(), set()).add(
            mesh_path.stem.casefold()
        )
    return result


def resolve_runner(converter: Path, work_root: Path) -> tuple[list[str], dict[str, str]]:
    if os.name == "nt":
        return [str(converter)], os.environ.copy()
    wine = shutil.which("wine")
    if wine:
        return [wine, wine_path(converter)], os.environ.copy()
    if PROTON_CANDIDATE.is_file():
        environment = os.environ.copy()
        environment.update(
            {
                "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(
                    Path.home() / ".local/share/Steam"
                ),
                "STEAM_COMPAT_DATA_PATH": str(work_root / "proton-prefix"),
                "WINEDEBUG": "-all",
            }
        )
        Path(environment["STEAM_COMPAT_DATA_PATH"]).mkdir(parents=True, exist_ok=True)
        return [str(PROTON_CANDIDATE), "run", wine_path(converter)], environment
    raise RuntimeError(
        "Spt2Fbx.exe requires Windows, Wine, or Steam Proton; none was found"
    )


def convert_package(
    package_path: Path,
    required_names: set[str] | None,
    converter_command: list[str],
    environment: dict[str, str],
    work_root: Path,
    output_root: Path,
) -> tuple[int, int]:
    package = UE2Package(str(package_path))
    package_work = work_root / package_path.stem
    package_work.mkdir(parents=True, exist_ok=True)
    requested: list[tuple[str, Path]] = []

    for export in package.exports:
        if export.get("class_name") != "StaticMesh":
            continue
        mesh_name = str(export["object_name"])
        if required_names is not None and mesh_name.casefold() not in required_names:
            continue
        export_data = package.get_export_data(export)
        if not has_embedded_speedtree_payload(export_data):
            continue
        spt_offset, _version = find_spt_header(export_data)
        spt_path = package_work / f"{mesh_name}.spt"
        dump_spt_payload(export_data, spt_offset, str(spt_path))
        requested.append((mesh_name, spt_path))

    if not requested:
        return 0, 0

    expected_fbx = [path.with_suffix(".fbx") for _, path in requested]
    for path in expected_fbx:
        path.unlink(missing_ok=True)

    process = subprocess.Popen(
        [
            *converter_command,
            str(package_work) if os.name == "nt" else wine_path(package_work),
        ],
        env=environment,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 300.0
    previous_snapshot: tuple[tuple[int, int], ...] | None = None
    stable_samples = 0
    completed_files = False
    while time.monotonic() < deadline:
        if all(path.is_file() and path.stat().st_size > 0 for path in expected_fbx):
            snapshot = tuple(
                (path.stat().st_size, path.stat().st_mtime_ns) for path in expected_fbx
            )
            if snapshot == previous_snapshot:
                stable_samples += 1
            else:
                previous_snapshot = snapshot
                stable_samples = 0
            if stable_samples >= 3:
                completed_files = True
                # Spt2Fbx sometimes leaves its completion window alive under
                # Proton. All requested FBX payloads are already closed and
                # stable, so stop only this converter process group.
                os.killpg(process.pid, signal.SIGTERM)
                break
        time.sleep(0.25)

    if not completed_files:
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)

    missing_fbx = [str(path) for path in expected_fbx if not path.is_file()]
    if (process.returncode not in (0, -signal.SIGTERM) and not completed_files) or missing_fbx:
        raise RuntimeError(
            f"Spt2Fbx failed for {package_path.name} (exit {process.returncode}); "
            f"missing={missing_fbx[:5]}"
        )

    package_output = output_root / package_path.stem
    package_output.mkdir(parents=True, exist_ok=True)
    published = 0
    for mesh_name, spt_path in requested:
        try:
            payload = build_leaf_cards(spt_path.with_suffix(".fbx"))
        except ValueError as exc:
            if str(exc) == "FBX has no LeafMAT material":
                # Leafless SpeedTrees (dead trunks, roots, and some frond-only
                # plants) need no runtime leaf replacement.
                continue
            raise
        if not payload.get("cards"):
            raise ValueError(f"{mesh_name}: SpeedTree runtime returned no leaf cards")
        # The reconstruction helper records its input FBX path for interactive
        # diagnostics.  That path points into this machine's ignored work
        # directory, so do not publish it in the reproducible sidecar.
        payload.pop("file", None)
        payload["source"] = {
            "package": package_path.name,
            "mesh": mesh_name,
            "method": "SpeedTreeRT GetGeometry via Spt2Fbx",
        }
        output_path = package_output / f"{mesh_name}.json"
        candidate = output_path.with_suffix(f".json.writing-{os.getpid()}")
        candidate.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
        os.replace(candidate, output_path)
        published += 1
    return len(requested), published


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--converter",
        default=os.environ.get("SPT2FBX_EXE"),
        help="Path to Spt2Fbx.exe (or set SPT2FBX_EXE)",
    )
    parser.add_argument("--object-artifact", type=Path)
    parser.add_argument("--file", help="Optional package stem/prefix")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    args = parser.parse_args()

    if not args.converter:
        parser.error("--converter or SPT2FBX_EXE is required")
    converter = Path(args.converter).expanduser().resolve()
    if not converter.is_file():
        parser.error(f"converter not found: {converter}")
    if not (converter.parent / "SpeedTreeRT.dll").is_file():
        parser.error(f"SpeedTreeRT.dll must be beside {converter.name}")

    required = (
        required_meshes_from_artifact(args.object_artifact.resolve())
        if args.object_artifact
        else None
    )
    package_paths = sorted(Path(config.ASSETS_PATH, "Meshes").glob("*.usx"))
    if args.file:
        package_paths = [
            path for path in package_paths if path.stem.casefold().startswith(args.file.casefold())
        ]
    if required is not None:
        package_paths = [path for path in package_paths if path.stem.casefold() in required]
    if not package_paths:
        raise FileNotFoundError("no matching mesh packages")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    command, environment = resolve_runner(converter, args.work_dir)
    extracted = published = 0
    for index, package_path in enumerate(package_paths, 1):
        package_extracted, package_published = convert_package(
            package_path,
            required.get(package_path.stem.casefold()) if required is not None else None,
            command,
            environment,
            args.work_dir,
            args.output,
        )
        extracted += package_extracted
        published += package_published
        if package_extracted:
            print(
                f"[{index}/{len(package_paths)}] {package_path.name}: "
                f"{package_published} runtime leaf sidecars"
            )
    print(f"SpeedTree runtime leaf recovery complete: extracted={extracted} published={published}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
