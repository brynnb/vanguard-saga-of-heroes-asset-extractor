#!/usr/bin/env python3
"""
Batch export all mesh packages to build a complete mesh library.
Run once to create output/buildings/ with all available meshes.
"""

import os
import subprocess
import glob
from pathlib import Path

ASSETS_PATH = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets"
MESHES_PATH = os.path.join(ASSETS_PATH, "Meshes")
UMODEL_PATH = "/Users/brynnbateman/Documents/GitHub/project-telon/renderer/gildor2-UEViewer-daa3c14/umodel.exe"
WINE_PATH = "/Applications/Wine Stable.app/Contents/Resources/wine/bin/wine"
OUTPUT_DIR = (
    "/Users/brynnbateman/Documents/GitHub/project-telon/renderer/output/buildings"
)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def export_package(package_path):
    """Export all meshes from a package using umodel."""
    cmd = [
        WINE_PATH,
        UMODEL_PATH,
        "-export",
        "-game=vang",
        "-noanim",
        f"-path={ASSETS_PATH}",
        package_path,
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=SCRIPT_DIR
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception as e:
        return False


def convert_pskx_to_gltf(pskx_path, gltf_path):
    """Convert a .pskx file to .gltf"""
    try:
        result = subprocess.run(
            ["python3", "psk_to_gltf.py", pskx_path, gltf_path],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=SCRIPT_DIR,
        )
        return os.path.exists(gltf_path)
    except:
        return False


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Get all mesh packages
    packages = sorted(glob.glob(os.path.join(MESHES_PATH, "*.usx")))
    print(f"Found {len(packages)} mesh packages")

    # Track progress
    exported_count = 0
    converted_count = 0
    failed_packages = []

    # Get existing meshes to skip
    existing = set(f[:-5] for f in os.listdir(OUTPUT_DIR) if f.endswith(".gltf"))
    print(f"Already have {len(existing)} meshes")

    for i, pkg_path in enumerate(packages):
        pkg_name = os.path.basename(pkg_path)

        # Skip SpeedTrees (vegetation) packages
        if "speedtree" in pkg_name.lower():
            continue

        print(f"[{i+1}/{len(packages)}] {pkg_name}...", end=" ", flush=True)

        # Export package
        if not export_package(pkg_path):
            print("FAIL export")
            failed_packages.append(pkg_name)
            continue

        # Find and convert new .pskx files
        umodel_dir = os.path.join(SCRIPT_DIR, "UmodelExport")
        new_meshes = 0

        for root, dirs, files in os.walk(umodel_dir):
            for f in files:
                if not f.endswith(".pskx"):
                    continue

                mesh_name = f[:-5]  # Remove .pskx
                if mesh_name in existing:
                    continue

                pskx_path = os.path.join(root, f)
                gltf_path = os.path.join(OUTPUT_DIR, f"{mesh_name}.gltf")

                if convert_pskx_to_gltf(pskx_path, gltf_path):
                    existing.add(mesh_name)
                    new_meshes += 1
                    converted_count += 1

        if new_meshes > 0:
            print(f"OK (+{new_meshes})")
            exported_count += 1
        else:
            print("OK (no new)")

    print(f"\n=== DONE ===")
    print(f"Packages processed: {len(packages)}")
    print(f"Packages with new meshes: {exported_count}")
    print(f"Total meshes converted: {converted_count}")
    print(f"Total meshes available: {len(existing)}")
    print(f"Failed packages: {len(failed_packages)}")


if __name__ == "__main__":
    main()
