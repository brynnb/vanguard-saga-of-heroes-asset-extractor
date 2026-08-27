#!/usr/bin/env python3
"""
Batch export all Vanguard .usx StaticMeshes to .pskx using UEViewer (umodel) via Wine.

Usage:
    python3 scripts/extractors/batch_umodel_export.py [--max N] [--resume]

Output goes to output/pskx/<PackageName>/StaticMesh/*.pskx
"""
import subprocess
import os
import sys
import glob
import time
import argparse

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from vanguard_assets import config  # noqa: E402
PROJECT_ROOT = config.PROJECT_ROOT

UMODEL_EXE = os.environ.get("UMODEL_EXE", "umodel.exe")
MESH_DIR = os.path.join(config.ASSETS_PATH, "Meshes")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output", "pskx")


def wine_path(native_path):
    """Convert macOS path to Wine Z: path."""
    return "Z:" + native_path


def export_package(usx_path, output_dir):
    """Export one .usx package to .pskx via wine+umodel. Returns (mesh_count, error)."""
    cmd = [
        "wine", UMODEL_EXE,
        "-export",
        "-game=vang",
        "-psk",
        f"-out={wine_path(output_dir)}",
        wine_path(usx_path),
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        
        # Count exported meshes
        mesh_count = output.count("Exporting StaticMesh")
        
        # Check for fatal errors
        if "Access violation" in output or "FATAL" in output:
            error_lines = [l for l in output.split('\n') if "ERROR" in l or "FATAL" in l or "Access violation" in l]
            return mesh_count, "; ".join(error_lines[:3])
        
        return mesh_count, None
        
    except subprocess.TimeoutExpired:
        return 0, "TIMEOUT"
    except Exception as e:
        return 0, str(e)


def main():
    parser = argparse.ArgumentParser(description="Batch export .usx to .pskx via umodel")
    parser.add_argument("--max", type=int, default=0, help="Max packages to process (0=all)")
    parser.add_argument("--resume", action="store_true", help="Skip packages that already have output")
    parser.add_argument("--package", type=str, default="", help="Export single package by name substring")
    args = parser.parse_args()
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    usx_files = sorted(glob.glob(os.path.join(MESH_DIR, "*.usx")))
    print(f"Found {len(usx_files)} .usx packages")
    
    if args.package:
        usx_files = [f for f in usx_files if args.package.lower() in os.path.basename(f).lower()]
        print(f"Filtered to {len(usx_files)} matching '{args.package}'")
    
    if args.max > 0:
        usx_files = usx_files[:args.max]
    
    total_meshes = 0
    errors = []
    skipped = 0
    t0 = time.time()
    
    for i, usx_path in enumerate(usx_files):
        pkg_name = os.path.splitext(os.path.basename(usx_path))[0]
        pkg_output = os.path.join(OUTPUT_DIR, pkg_name)
        
        # Resume: skip if output directory already has .pskx files
        if args.resume and os.path.isdir(pkg_output):
            existing = glob.glob(os.path.join(pkg_output, "**", "*.pskx"), recursive=True)
            if existing:
                skipped += 1
                total_meshes += len(existing)
                continue
        
        elapsed = time.time() - t0
        rate = (i - skipped) / elapsed if elapsed > 0 and (i - skipped) > 0 else 0
        remaining = (len(usx_files) - i) / rate / 60 if rate > 0 else 0
        
        print(f"[{i+1}/{len(usx_files)}] {pkg_name} (est {remaining:.0f}m left) ... ", end="", flush=True)
        
        mesh_count, error = export_package(usx_path, pkg_output)
        total_meshes += mesh_count
        
        if error:
            errors.append((pkg_name, error))
            print(f"{mesh_count} meshes, ERROR: {error[:80]}")
        else:
            print(f"{mesh_count} meshes")
    
    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"Done in {elapsed:.0f}s ({elapsed/60:.1f}m)")
    print(f"Total meshes exported: {total_meshes}")
    if skipped:
        print(f"Skipped (resume): {skipped}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for pkg, err in errors:
            print(f"  {pkg}: {err[:120]}")
    else:
        print("No errors!")


if __name__ == "__main__":
    main()
