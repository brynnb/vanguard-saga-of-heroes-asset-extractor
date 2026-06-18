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

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
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


def _resolve_workers(workers):
    if workers < 1:
        return os.cpu_count() or 1
    return workers


def process_chunk(vgr_path, verbose=True):
    """Process a single .vgr chunk file and extract terrain + placement data."""
    chunk_name = os.path.splitext(os.path.basename(vgr_path))[0]
    output_dir = os.path.join(OUTPUT_BASE, chunk_name)

    def log(message):
        if verbose:
            print(message, flush=True)

    log(f"\n  Processing: {chunk_name}")

    # Step 1: Get object list
    try:
        objects = get_object_list(vgr_path)
    except subprocess.TimeoutExpired:
        log(f"    TIMEOUT listing objects, skipping...")
        return None
    except Exception as e:
        log(f"    ERROR listing objects: {e}")
        return None

    if not objects:
        log(f"    No objects found")
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
        log(f"    Extracting {len(terrain_infos)} TerrainInfo(s)...")
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

                log(f"    + TerrainInfo: {layer_count} layers, {tile_count} tile entries")
            except subprocess.TimeoutExpired:
                log(f"    TIMEOUT on TerrainInfo")
            except Exception as e:
                log(f"    ERROR on TerrainInfo: {e}")

        if terrain_output:
            with open(os.path.join(output_dir, "terrain_info.txt"), "w") as f:
                f.write("\n\n".join(terrain_output))

    # Step 3: Extract CompoundObject placements
    if compound_objects:
        log(f"    Extracting {len(compound_objects)} CompoundObject(s)...")
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
            log(f"    + {len(placement_output)} placements extracted")

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


def process_chunk_worker(vgr_path):
    """Worker entry point for chunk-level parallel extraction."""
    try:
        return vgr_path, process_chunk(vgr_path, verbose=False), None
    except Exception as exc:
        return vgr_path, None, str(exc)


def chunk_name_from_path(path):
    return os.path.splitext(os.path.basename(path))[0]


def select_vgr_files(chunk_names, limit):
    all_files = {
        chunk_name_from_path(os.path.join(VANGUARD_MAPS_DIR, f)): os.path.join(VANGUARD_MAPS_DIR, f)
        for f in os.listdir(VANGUARD_MAPS_DIR)
        if f.endswith(".vgr")
    }
    if chunk_names:
        selected = []
        for chunk_name in chunk_names:
            normalized = chunk_name[:-4] if chunk_name.endswith(".vgr") else chunk_name
            if normalized in all_files:
                selected.append(all_files[normalized])
            else:
                print(f"WARNING: chunk not found: {normalized}", file=sys.stderr, flush=True)
    else:
        selected = [all_files[name] for name in sorted(all_files)]
    if limit > 0:
        selected = selected[:limit]
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", action="append", default=[], help="Chunk name to process.")
    parser.add_argument("--limit", type=int, default=0, help="Limit chunk count for testing.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Chunk worker processes; 0 uses all CPUs.",
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_BASE, exist_ok=True)

    vgr_files = select_vgr_files(args.chunk, args.limit)
    workers = min(_resolve_workers(args.workers), len(vgr_files)) if vgr_files else 1

    print(f"Vanguard Chunk Data Bulk Extractor", flush=True)
    print(f"Found {len(vgr_files)} .vgr files in {VANGUARD_MAPS_DIR}", flush=True)
    print(f"Output: {OUTPUT_BASE}", flush=True)
    print(f"Workers: {workers}", flush=True)

    all_stats = []
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(process_chunk_worker, vgr_path) for vgr_path in vgr_files]
            for completed, future in enumerate(as_completed(futures), start=1):
                vgr_path, stats, error = future.result()
                chunk_name = chunk_name_from_path(vgr_path)
                if stats:
                    all_stats.append(stats)
                    print(
                        f"[{completed}/{len(vgr_files)}] {chunk_name}: "
                        f"objects={stats['total_objects']} compounds={stats['compound_objects']} "
                        f"layers={stats['terrain_layers']} tiles={stats['tile_data_entries']}",
                        flush=True,
                    )
                else:
                    detail = f": {error}" if error else ""
                    print(f"[{completed}/{len(vgr_files)}] {chunk_name}: FAILED{detail}", flush=True)
    else:
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
