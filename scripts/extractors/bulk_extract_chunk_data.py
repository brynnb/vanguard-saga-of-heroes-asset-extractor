#!/usr/bin/env python3
"""
Bulk Chunk Data Extractor for Vanguard: Saga of Heroes

Uses Unreal-Library to extract terrain layer/blending data and object
placement data from every .vgr chunk file.

For each chunk, extracts:
  - TerrainInfo: Layers[], pBuildingTileLayerData[], TerrainMap reference
  - CompoundObjects: Location, PrefabName, rotation, scale
  - Other placed actors: PlayerStart, Sunlight, WaterVolume, etc.

Usage:
    python3 bulk_extract_chunk_data.py

Output goes to: output/reference/Maps/<chunk_name>/ by default
  - terrain_info.txt     (full TerrainInfo decompile with layers and tile data)
  - objects.txt          (all CompoundObject and actor placements)
  - object_list.txt      (index of all objects in the chunk)
"""

import subprocess
import os
import re
import sys
import json

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: E402

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)

# Paths
DOTNET_PATH = config.DOTNET
LIB_PATH = config.UNREAL_LIBRARY_DLL
VANGUARD_MAPS_DIR = config.MAPS_DIR
OUTPUT_BASE = config.REFERENCE_MAPS_DIR


def run_unreal_lib(package_path, command):
    """Run Unreal-Library CLI with a command and return stdout."""
    result = subprocess.run(
        [DOTNET_PATH, "--roll-forward", "Major", LIB_PATH, package_path, command],
        capture_output=True, text=True, input="\n", timeout=120
    )
    return result.stdout, result.stderr


def get_object_list(package_path):
    """Get all objects in the package."""
    stdout, _ = run_unreal_lib(package_path, "obj list")
    objects = []
    for line in stdout.splitlines():
        line = line.strip()
        # Match lines like: CompoundObject'chunk_n10_n10.CompoundObject0'
        match = re.match(r"(\w+)'(.+)'", line)
        if match:
            obj_class = match.group(1)
            obj_path = match.group(2)
            objects.append((obj_class, obj_path))
    return objects


def decompile_object(package_path, obj_path):
    """Decompile a single object and return the clean output."""
    stdout, _ = run_unreal_lib(package_path, f"obj decompile {obj_path}")

    lines = stdout.splitlines()
    # Find the start of the actual decompiled output
    start_index = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("// Reference:") or line.strip().startswith("begin object"):
            start_index = i
            break

    return "\n".join(lines[start_index:])


def process_chunk(vgr_path):
    """Process a single .vgr chunk file and extract terrain + placement data."""
    chunk_name = os.path.splitext(os.path.basename(vgr_path))[0]
    output_dir = os.path.join(OUTPUT_BASE, chunk_name)

    print(f"\n  Processing: {chunk_name}", flush=True)

    # Step 1: Get object list
    try:
        objects = get_object_list(vgr_path)
    except subprocess.TimeoutExpired:
        print(f"    TIMEOUT listing objects, skipping...", flush=True)
        return None
    except Exception as e:
        print(f"    ERROR listing objects: {e}", flush=True)
        return None

    if not objects:
        print(f"    No objects found", flush=True)
        return None

    os.makedirs(output_dir, exist_ok=True)

    # Save object list
    with open(os.path.join(output_dir, "object_list.txt"), "w") as f:
        for obj_class, obj_path in objects:
            f.write(f"{obj_class}\t{obj_path}\n")

    # Categorize objects
    terrain_infos = [(c, p) for c, p in objects if c == "TerrainInfo"]
    compound_objects = [(c, p) for c, p in objects if c == "CompoundObject"]
    actors = [(c, p) for c, p in objects
              if c in ("PlayerStart", "Sunlight", "WaterVolume", "ZoneInfo",
                       "LevelInfo", "DefaultPhysicsVolume", "Brush",
                       "NavigationPoint", "PathNode", "SmallNavigationPoint")]

    stats = {
        "chunk": chunk_name,
        "total_objects": len(objects),
        "terrain_infos": len(terrain_infos),
        "compound_objects": len(compound_objects),
        "terrain_layers": 0,
        "tile_data_entries": 0,
    }

    # Step 2: Extract TerrainInfo (the big prize - layers and blending data)
    if terrain_infos:
        print(f"    Extracting {len(terrain_infos)} TerrainInfo(s)...", flush=True)
        terrain_output = []
        for obj_class, obj_path in terrain_infos:
            try:
                decompiled = decompile_object(vgr_path, obj_path)
                terrain_output.append(decompiled)

                # Count layers and tile data
                layer_count = len(re.findall(r"Layers\[\d+\]=", decompiled))
                tile_count = len(re.findall(r"pBuildingTileLayerData\[\d+\]=", decompiled))
                stats["terrain_layers"] = max(stats["terrain_layers"], layer_count)
                stats["tile_data_entries"] = max(stats["tile_data_entries"], tile_count)

                print(f"    + TerrainInfo: {layer_count} layers, {tile_count} tile entries", flush=True)
            except subprocess.TimeoutExpired:
                print(f"    TIMEOUT on TerrainInfo", flush=True)
            except Exception as e:
                print(f"    ERROR on TerrainInfo: {e}", flush=True)

        if terrain_output:
            with open(os.path.join(output_dir, "terrain_info.txt"), "w") as f:
                f.write("\n\n".join(terrain_output))

    # Step 3: Extract CompoundObject placements
    if compound_objects:
        print(f"    Extracting {len(compound_objects)} CompoundObject(s)...", flush=True)
        placement_output = []
        for obj_class, obj_path in compound_objects:
            try:
                decompiled = decompile_object(vgr_path, obj_path)
                placement_output.append(decompiled)
            except subprocess.TimeoutExpired:
                print(f"    TIMEOUT on {obj_path}", flush=True)
            except Exception as e:
                print(f"    ERROR on {obj_path}: {e}", flush=True)

        if placement_output:
            with open(os.path.join(output_dir, "objects.txt"), "w") as f:
                f.write("\n\n".join(placement_output))
            print(f"    + {len(placement_output)} placements extracted", flush=True)

    # Step 4: Extract other actors (quick pass)
    if actors:
        actor_output = []
        for obj_class, obj_path in actors:
            try:
                decompiled = decompile_object(vgr_path, obj_path)
                actor_output.append(decompiled)
            except:
                pass

        if actor_output:
            with open(os.path.join(output_dir, "actors.txt"), "w") as f:
                f.write("\n\n".join(actor_output))

    return stats


def main():
    os.makedirs(OUTPUT_BASE, exist_ok=True)

    vgr_files = sorted([
        os.path.join(VANGUARD_MAPS_DIR, f)
        for f in os.listdir(VANGUARD_MAPS_DIR)
        if f.endswith(".vgr")
    ])

    print(f"Vanguard Chunk Data Bulk Extractor", flush=True)
    print(f"Found {len(vgr_files)} .vgr files in {VANGUARD_MAPS_DIR}", flush=True)
    print(f"Output: {OUTPUT_BASE}", flush=True)

    all_stats = []
    for vgr_path in vgr_files:
        stats = process_chunk(vgr_path)
        if stats:
            all_stats.append(stats)

    # Summary
    total_layers = sum(s["terrain_layers"] for s in all_stats)
    total_placements = sum(s["compound_objects"] for s in all_stats)
    total_tiles = sum(s["tile_data_entries"] for s in all_stats)

    print(f"\n{'='*60}", flush=True)
    print(f"EXTRACTION COMPLETE", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"  Chunks processed: {len(all_stats)}/{len(vgr_files)}", flush=True)
    print(f"  Total terrain layers found: {total_layers}", flush=True)
    print(f"  Total tile data entries: {total_tiles}", flush=True)
    print(f"  Total object placements: {total_placements}", flush=True)
    print(f"  Output: {OUTPUT_BASE}", flush=True)

    # Save summary JSON
    with open(os.path.join(OUTPUT_BASE, "extraction_summary.json"), "w") as f:
        json.dump({
            "total_chunks": len(all_stats),
            "total_vgr_files": len(vgr_files),
            "total_terrain_layers": total_layers,
            "total_tile_entries": total_tiles,
            "total_placements": total_placements,
            "chunks": all_stats,
        }, f, indent=2)


if __name__ == "__main__":
    main()
