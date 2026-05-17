#!/usr/bin/env python3
"""Extract cloud billboard data from P0001_CloudMeshes.ucd into cloud_data.json.

The .ucd file contains 5 cloud objects (Cloud1-Cloud5), each with billboard quads
that reference a 4x4 sprite atlas (CloudTexture1.png).

Format per cloud object:
  Header (37 bytes):
    4B properties (SwarmMaterial ref)
    4B scale (float)
    1B compact_index (shader ref, -1)
    24B bounding box (6 floats: minX,minY,minZ,maxX,maxY,maxZ)
    2B flags
    2B compact_index (vertex count)
  Records (28 bytes each, 7 floats):
    x, y, z, u, v, vertex_index(0-3), billboard_size
  Records come in groups of 4 (quad vertices for one billboard).
"""

import struct
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from config import ASSETS_PATH

UCD_PATH = os.path.join(ASSETS_PATH, "CloudMeshes", "P0001_CloudMeshes.ucd")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'output', 'data', 'cloud_data.json')

RECORD_SIZE = 28

# Export table offsets and sizes for each cloud object in P0001_CloudMeshes.ucd
CLOUD_EXPORTS = {
    'Cloud1': (313, 25852),
    'Cloud2': (115253, 29696),
    'Cloud3': (55861, 29696),
    'Cloud4': (26165, 29696),
    'Cloud5': (85557, 29696),
}


def read_compact_index(data, pos):
    """Read a UE2 compact index (variable-length encoded integer)."""
    b0 = data[pos]; pos += 1
    neg = bool(b0 & 0x80)
    more = bool(b0 & 0x40)
    val = b0 & 0x3F
    shift = 6
    while more:
        b = data[pos]; pos += 1
        more = bool(b & 0x80) if shift < 20 else False
        val |= (b & 0x7F) << shift
        shift += 7
    return -val if neg else val, pos


def extract_cloud(data, name, offset, size):
    """Extract billboard data for one cloud object."""
    pos = offset
    pos += 4  # skip property bytes

    scale = struct.unpack_from('<f', data, pos)[0]; pos += 4
    _shader_ref, pos = read_compact_index(data, pos)
    bbox = struct.unpack_from('<6f', data, pos); pos += 24
    _flag1 = data[pos]; pos += 1
    _flag2 = data[pos]; pos += 1
    count, pos = read_compact_index(data, pos)

    records = []
    for i in range(count):
        rpos = pos + i * RECORD_SIZE
        records.append(struct.unpack_from('<7f', data, rpos))

    num_quads = count // 4
    billboards = []
    for q in range(num_quads):
        group = records[q * 4:(q + 1) * 4]
        cx = sum(r[0] for r in group) / 4
        cy = sum(r[1] for r in group) / 4
        cz = sum(r[2] for r in group) / 4
        us = [r[3] for r in group]
        vs = [r[4] for r in group]
        u_min = min(us)
        v_min = min(vs)
        billboard_size = group[0][6]
        atlas_col = round(u_min / 0.25)
        atlas_row = round(v_min / 0.25)
        billboards.append({
            'x': round(cx, 2),
            'y': round(cy, 2),
            'z': round(cz, 2),
            'atlas_col': atlas_col,
            'atlas_row': atlas_row,
            'size': round(billboard_size, 1)
        })

    return {
        'scale': scale,
        'bbox': [round(b, 2) for b in bbox],
        'num_billboards': len(billboards),
        'billboards': billboards
    }


def main():
    if not os.path.exists(UCD_PATH):
        print(f"Error: {UCD_PATH} not found")
        sys.exit(1)

    with open(UCD_PATH, 'rb') as f:
        data = f.read()

    all_clouds = {}
    for name in ['Cloud1', 'Cloud2', 'Cloud3', 'Cloud4', 'Cloud5']:
        offset, size = CLOUD_EXPORTS[name]
        cloud = extract_cloud(data, name, offset, size)
        all_clouds[name] = cloud
        print(f"  {name}: {cloud['num_billboards']} billboards")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w') as f:
        json.dump(all_clouds, f, indent=2)
    print(f"Saved {OUTPUT_PATH}")


if __name__ == '__main__':
    main()
