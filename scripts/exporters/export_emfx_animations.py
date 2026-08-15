#!/usr/bin/env python3
"""
Batch export EMotion FX (FXM) animations from .uea packages as glTF files.

For each .uem mesh package, reads the AnimSet property from its _SKELETON
export to discover which .uea animation packages it references. Then parses
each .uea package and exports every EMFXAnim clip as a standalone glTF file
with FXA bone names (matching the mesh skeleton exactly).

Output:
  output/meshes/emfx_animations/<uea_package>/<clip_name>.gltf
  output/meshes/emfx_animations/manifest.json

The manifest maps mesh package names to their animation files.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import glob
import json
import os
import shutil
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts", "lib"))

import config
from ue2.package import UE2Package
from ue2_property_reader import BinaryReader, read_ue2_properties, decode_animset_names
from vanguard_emfxanim import parse_emfxanim_export, export_emfxanim_gltf

MESH_DIR = os.path.join(config.ASSETS_PATH, "Characters", "Meshes")
ANIM_DIR = os.path.join(config.ASSETS_PATH, "Characters", "Animations")
OUT_DIR = os.path.join(ROOT, "output", "meshes", "emfx_animations")


def extract_animset_from_uem(uem_path):
    """Extract AnimSet package names from a .uem file's SKELETON export."""
    try:
        pkg = UE2Package(uem_path)
    except Exception:
        return []

    for exp in pkg.exports:
        obj_name = exp.get("object_name", "")
        if "SKELETON" not in obj_name:
            continue

        try:
            data = pkg.get_export_data(exp)
            reader = BinaryReader(data, 0)
            props = read_ue2_properties(reader, pkg.names)
            animset_raw = props.get("AnimSet")
            if animset_raw and isinstance(animset_raw, (bytes, bytearray)):
                return decode_animset_names(animset_raw, pkg.names)
        except Exception:
            continue

    return []


def _resolve_worker_count(value):
    if value <= 0:
        return max(1, os.cpu_count() or 1)
    return max(1, value)


def _scan_uem_animset_job(uem_path):
    pkg_name = os.path.splitext(os.path.basename(uem_path))[0]
    return {
        "pkg_name": pkg_name,
        "uem_path": uem_path,
        "uea_names": extract_animset_from_uem(uem_path),
    }


def _export_uea_package_job(job):
    uea_name, uea_path, out_dir = job
    result = {
        "uea_name": uea_name,
        "clips": [],
        "exported": 0,
        "failed": 0,
        "errors": [],
    }
    try:
        pkg = UE2Package(uea_path)
    except Exception as exc:
        result["failed"] += 1
        result["errors"].append(f"SKIP {uea_name}: failed to open package: {exc}")
        return result

    pkg_out_dir = os.path.join(out_dir, uea_name)
    os.makedirs(pkg_out_dir, exist_ok=True)
    for exp in pkg.exports:
        class_name = exp.get("class_name", "")
        if class_name != "EMFXAnim":
            continue

        obj_name = exp.get("object_name", "unknown")
        try:
            data = pkg.get_export_data(exp)
            anim = parse_emfxanim_export(data)
            if not anim.submotions:
                continue

            out_path = os.path.join(pkg_out_dir, f"{obj_name}.gltf")
            export_result = export_emfxanim_gltf(
                anim,
                obj_name,
                out_path,
            )
            if export_result:
                rel_path = f"{uea_name}/{obj_name}.gltf"
                result["clips"].append(
                    {
                        "name": obj_name,
                        "path": rel_path,
                        "bones": len(anim.submotions),
                        "duration": anim.duration,
                    }
                )
                result["exported"] += 1
            else:
                result["failed"] += 1
        except Exception:
            result["failed"] += 1
            result["errors"].append(
                f"{uea_name}/{obj_name}: {traceback.format_exc().strip()}"
            )
    return result


def _run_jobs(label, jobs, worker_count, job_func, progress_every):
    if not jobs:
        return []
    if worker_count == 1:
        results = []
        for index, job in enumerate(jobs, 1):
            results.append(job_func(job))
            if progress_every > 0 and (index == len(jobs) or index % progress_every == 0):
                print(f"  {label}: {index}/{len(jobs)}")
        return results

    results = []
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(job_func, job) for job in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if progress_every > 0 and (index == len(jobs) or index % progress_every == 0):
                print(f"  {label}: {index}/{len(jobs)}")
    return results


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Worker processes for UEM scanning and UEA package export; 0 uses all CPUs.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print progress after this many completed jobs; 0 disables progress messages.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete output/meshes/emfx_animations before exporting.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    worker_count = _resolve_worker_count(args.workers)
    if args.clean and os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Using {worker_count} EMFX worker process{'es' if worker_count != 1 else ''}")

    # --- Phase 1: Scan all .uem files to build mesh → UEA mapping ---
    uem_files = sorted(glob.glob(os.path.join(MESH_DIR, "*.uem")))
    print(f"Scanning {len(uem_files)} .uem files for AnimSet references...")

    mesh_to_uea = {}  # mesh_pkg_name -> [uea_pkg_names]
    all_uea_names = set()

    scan_results = _run_jobs(
        "Scanned UEM packages",
        uem_files,
        worker_count,
        _scan_uem_animset_job,
        args.progress_every,
    )
    for result in sorted(scan_results, key=lambda item: item["pkg_name"].lower()):
        pkg_name = result["pkg_name"]
        uea_names = result["uea_names"]
        if uea_names:
            mesh_to_uea[pkg_name] = uea_names
            all_uea_names.update(uea_names)

    print(f"  {len(mesh_to_uea)} meshes reference {len(all_uea_names)} unique UEA packages")

    # --- Phase 2: Locate .uea files on disk ---
    uea_files_on_disk = {}  # uea_pkg_name -> filepath
    for uea_path in glob.glob(os.path.join(ANIM_DIR, "*.uea")):
        name = os.path.splitext(os.path.basename(uea_path))[0]
        uea_files_on_disk[name] = uea_path

    # Also check MESH_DIR in case some .uea are co-located
    for uea_path in glob.glob(os.path.join(MESH_DIR, "*.uea")):
        name = os.path.splitext(os.path.basename(uea_path))[0]
        uea_files_on_disk.setdefault(name, uea_path)

    found = all_uea_names & set(uea_files_on_disk.keys())
    missing = all_uea_names - found
    print(f"  Found {len(found)}/{len(all_uea_names)} UEA files on disk")
    if missing:
        print(f"  Missing: {sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}")

    # --- Phase 3: Parse and export each UEA package ---
    manifest = {}  # mesh_pkg -> {uea_packages: [{name, clips: [{name, path}]}]}
    total_exported = 0
    total_failed = 0
    exported_uea = {}  # uea_name -> [{clip_name, rel_path}]

    export_jobs = [
        (
            uea_name,
            uea_files_on_disk[uea_name],
            OUT_DIR,
        )
        for uea_name in sorted(found)
    ]
    export_results = _run_jobs(
        "Exported UEA packages",
        export_jobs,
        worker_count,
        _export_uea_package_job,
        args.progress_every,
    )
    export_errors = []
    for result in sorted(export_results, key=lambda item: item["uea_name"].lower()):
        total_exported += int(result["exported"])
        total_failed += int(result["failed"])
        if result["clips"]:
            exported_uea[result["uea_name"]] = result["clips"]
        export_errors.extend(result["errors"])

    for error in export_errors[:20]:
        print(f"  {error}")
    if len(export_errors) > 20:
        print(f"  ... {len(export_errors) - 20} more export errors omitted")

    # --- Phase 4: Build manifest ---
    for mesh_name, uea_names in mesh_to_uea.items():
        uea_entries = []
        for uea_name in uea_names:
            clips = exported_uea.get(uea_name, [])
            if clips:
                uea_entries.append({"package": uea_name, "clips": clips})
        if uea_entries:
            manifest[mesh_name] = {
                "binding": "authoritative_fxm",
                "uea_packages": uea_entries,
            }

    manifest_path = os.path.join(OUT_DIR, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Batch export complete:")
    print(f"  Exported:       {total_exported} animation clips")
    print(f"  Failed:         {total_failed}")
    print(f"  Meshes mapped:  {len(manifest)}")
    print(f"  UEA packages:   {len(exported_uea)}")
    print(f"  Manifest:       {manifest_path}")


if __name__ == "__main__":
    main()
