#!/usr/bin/env python3
"""Parse VSOHWeatherDefs.ini and VSOHWeatherPerChunk.ini into a single JSON file."""

import json
import re
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from config import ASSETS_PATH

def parse_weather_defs(filepath):
    """Parse VSOHWeatherDefs.ini into a dict of zone_name -> weather data."""
    zones = {}
    current_zone = None

    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('[') and line.endswith(']'):
                current_zone = line[1:-1]
                zones[current_zone] = {}
                continue
            if current_zone and '=' in line:
                key, val = line.split('=', 1)
                key = key.strip()
                val = val.strip()

                # Parse the key to extract property name and hour
                m = re.match(r'^(.+?)Hour(\d+)$', key)
                if not m:
                    continue
                prop = m.group(1)
                hour = int(m.group(2))

                # Initialize array if needed
                if prop not in zones[current_zone]:
                    zones[current_zone][prop] = [None] * 24

                # Parse value - either a single float or space-separated RGB
                parts = val.split()
                if len(parts) == 3:
                    zones[current_zone][prop][hour] = [float(x) for x in parts]
                else:
                    zones[current_zone][prop][hour] = float(val)

    return zones


def parse_chunk_weather_map(filepath):
    """Parse VSOHWeatherPerChunk.ini into a dict of chunk_name -> zone_name."""
    mapping = {}
    current_chunk = None

    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('[') and line.endswith(']'):
                current_chunk = line[1:-1]
                continue
            if current_chunk and line.startswith('WEATHER='):
                zone = line.split('=', 1)[1].strip()
                mapping[f"chunk_{current_chunk}"] = zone

    return mapping


def main():
    bin_dir = os.path.join(os.path.dirname(ASSETS_PATH.rstrip('/')), 'bin')
    defs_path = os.path.join(bin_dir, 'VSOHWeatherDefs.ini')
    chunks_path = os.path.join(bin_dir, 'VSOHWeatherPerChunk.ini')

    if not os.path.exists(defs_path):
        print(f"ERROR: {defs_path} not found")
        sys.exit(1)
    if not os.path.exists(chunks_path):
        print(f"ERROR: {chunks_path} not found")
        sys.exit(1)

    zones = parse_weather_defs(defs_path)
    chunk_map = parse_chunk_weather_map(chunks_path)

    output = {
        "chunkToZone": chunk_map,
        "zones": zones,
    }

    out_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'output', 'data')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'weather_data.json')

    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"Parsed {len(zones)} weather zones, {len(chunk_map)} chunk mappings")
    print(f"Written to {out_path}")

    # Show sample for current default chunk
    sample_chunk = "chunk_n25_26"
    if sample_chunk in chunk_map:
        zone_name = chunk_map[sample_chunk]
        zone = zones.get(zone_name, {})
        print(f"\nSample: {sample_chunk} -> zone '{zone_name}'")
        print(f"  FogRGB Hour 12: {zone.get('FogRGB', [None]*24)[12]}")
        print(f"  LighModRGB Hour 12: {zone.get('LighModRGB', [None]*24)[12]}")
        print(f"  LighModFactor Hour 12: {zone.get('LighModFactor', [None]*24)[12]}")


if __name__ == '__main__':
    main()
