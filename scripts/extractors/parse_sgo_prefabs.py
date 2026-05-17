#!/usr/bin/env python3
"""
parse_sgo_prefabs.py — Parse binaryprefabs.sgo into a JSON prefab component map.

The SGO file is a concatenation of ~19,570 UE2 mini-packages, each containing
a single prefab definition. Each mini-package has the standard UE2 header
(signature 0x9E2A83C1), name table, import table, and export table.

Key discovery: UE2 serialization in the SGO uses multiple None-terminated
property blocks per export (typically 3):
  Block 1: instance overrides (e.g. bLightChanged)
  Block 2: more overrides (e.g. bSelected)
  Block 3: template/default properties (StaticMesh, Location, Rotation, etc.)

The parser must read through ALL None terminators to find mesh references
and transform data, which are usually in the 2nd or 3rd block.

Prefab types:
  - Leaf prefabs: contain StaticMeshActor exports with direct mesh references
  - Compound prefabs: contain CompoundObject exports referencing sub-prefabs
  - Hybrid prefabs: contain both (leaf meshes + compound sub-references)

Output: JSON mapping prefab names to arrays of resolved components, each with:
  - mesh: StaticMesh name (from import table)
  - location: [x, y, z] local offset in Vanguard coordinates
  - rotation: [pitch, yaw, roll] in UE2 rotation units (optional, only if non-zero)

Usage:
  python3 scripts/extractors/parse_sgo_prefabs.py
  python3 scripts/extractors/parse_sgo_prefabs.py --sgo /path/to/binaryprefabs.sgo
"""
import struct, json, time, os, sys, argparse

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

try:
    import config

    DEFAULT_SGO = getattr(config, "SGO_PATH", None)
except ImportError:
    DEFAULT_SGO = None

if not DEFAULT_SGO:
    DEFAULT_SGO = os.path.expanduser(
        "~/Downloads/Vanguard EMU/Assets/Archives/binaryprefabs.sgo"
    )

DEFAULT_OUT = os.path.join(PROJECT_ROOT, "output/data/sgo_prefabs.json")

parser = argparse.ArgumentParser(
    description="Parse binaryprefabs.sgo into prefab component JSON"
)
parser.add_argument("--sgo", default=DEFAULT_SGO, help="Path to binaryprefabs.sgo")
parser.add_argument("--out", default=DEFAULT_OUT, help="Output JSON path")
args = parser.parse_args()

SGO_PATH = args.sgo
OUT_PATH = args.out

if not os.path.exists(SGO_PATH):
    print(f"ERROR: SGO file not found: {SGO_PATH}")
    sys.exit(1)

data = open(SGO_PATH, "rb").read()
d = data[8:]  # Skip 8-byte SGO header


def read_ci(buf, pos):
    if pos >= len(buf):
        return (0, pos)
    b0 = buf[pos]
    pos += 1
    neg = b0 & 0x80
    val = b0 & 0x3F
    if b0 & 0x40:
        if pos >= len(buf):
            return (val if not neg else -val, pos)
        b1 = buf[pos]
        pos += 1
        val |= (b1 & 0x7F) << 6
        if b1 & 0x80:
            if pos >= len(buf):
                return (val if not neg else -val, pos)
            b2 = buf[pos]
            pos += 1
            val |= (b2 & 0x7F) << 13
            if b2 & 0x80:
                if pos >= len(buf):
                    return (val if not neg else -val, pos)
                b3 = buf[pos]
                pos += 1
                val |= (b3 & 0x7F) << 20
                if b3 & 0x80:
                    if pos >= len(buf):
                        return (val if not neg else -val, pos)
                    b4 = buf[pos]
                    pos += 1
                    val |= b4 << 27
    return (-val if neg else val, pos)


def read_fstr(buf, pos):
    length, pos = read_ci(buf, pos)
    if length < 0:
        length = -length
        s = (
            buf[pos : pos + length * 2]
            .decode("utf-16-le", errors="replace")
            .rstrip("\x00")
        )
        pos += length * 2
    else:
        s = buf[pos : pos + length].decode("latin-1", errors="replace").rstrip("\x00")
        pos += length
    return s, pos


def parse_all_props(buf, offset, size, names, imports):
    """Parse ALL property blocks (multiple None terminators) from serialized data.

    The only terminator that matters is the end of the serial-size window.
    Each None (CI=0) simply closes a block and a new one begins; we loop
    until we have consumed every byte.
    """
    props = {}
    pos = offset
    end = offset + size

    while pos < end - 1:
        ni, new_pos = read_ci(buf, pos)
        if ni < 0 or ni >= len(names):
            # Skip bad byte and try next
            pos = new_pos
            continue
        pn = names[ni]
        # UE2 property serialization always uses compact index 0 for None terminator.
        # In SGO mini-packages, the local name table may have 'None' at a different index,
        # but the serialized data still writes 0x00 (CI=0) as the block separator.
        if pn == "None" or ni == 0:
            pos = new_pos
            continue

        pos = new_pos
        if pos >= end:
            break
        info = buf[pos]
        pos += 1
        pt = info & 0x0F
        st = (info >> 4) & 0x07
        af = (info >> 7) & 0x01

        if pt == 3:
            props[pn] = bool(af)
            continue

        sn = None
        if pt == 10:
            si, pos = read_ci(buf, pos)
            sn = names[si] if 0 <= si < len(names) else "?"

        if st == 0:
            psz = 1
        elif st == 1:
            psz = 2
        elif st == 2:
            psz = 4
        elif st == 3:
            psz = 12
        elif st == 4:
            psz = 16
        elif st == 5:
            if pos >= end:
                break
            psz = buf[pos]
            pos += 1
        elif st == 6:
            if pos + 2 > end:
                break
            psz = struct.unpack("<H", buf[pos : pos + 2])[0]
            pos += 2
        else:
            if pos + 4 > end:
                break
            psz = struct.unpack("<I", buf[pos : pos + 4])[0]
            pos += 4

        if af:
            _, pos = read_ci(buf, pos)

        if pos + psz > end:
            break
        pdata = buf[pos : pos + psz]

        # Only store properties we care about (don't overwrite with later blocks)
        if pn not in props:
            if pt == 10 and sn == "Vector" and psz >= 12:
                x, y, z = struct.unpack("<fff", pdata[:12])
                props[pn] = [x, y, z]
            elif pt == 10 and sn == "Rotator" and psz >= 12:
                p2, y2, r2 = struct.unpack("<iii", pdata[:12])
                props[pn] = [p2, y2, r2]
            elif pt == 10 and sn == "Color" and psz >= 4:
                props[pn] = {"R": pdata[0], "G": pdata[1], "B": pdata[2], "A": pdata[3]}
            elif pt == 10 and sn == "PointRegion" and psz >= 13:
                pass  # Skip PointRegion structs
            elif pt == 5 and psz >= 1:
                ref, _ = read_ci(pdata, 0)
                if ref < 0:
                    ii = -ref - 1
                    props[pn] = imports[ii]["name"] if ii < len(imports) else None
                elif ref > 0:
                    props[pn] = f"export_{ref}"
                else:
                    props[pn] = None
            elif pt == 4 and psz >= 4:
                props[pn] = struct.unpack("<f", pdata[:4])[0]
            elif pt == 2 and psz >= 4:
                props[pn] = struct.unpack("<i", pdata[:4])[0]
            elif pt == 6 and psz >= 1:
                ni2, _ = read_ci(pdata, 0)
                props[pn] = names[ni2] if 0 <= ni2 < len(names) else None
            elif pt == 1 and psz >= 1:
                props[pn] = pdata[0]

        pos += psz

    return props


# Find boundaries
sig_bytes = struct.pack("<I", 0x9E2A83C1)
boundaries = []
pos = 0
while True:
    idx = d.find(sig_bytes, pos)
    if idx == -1:
        break
    boundaries.append(idx)
    pos = idx + 1

print(f"Found {len(boundaries)} mini-packages")
t0 = time.time()

leaf_prefabs = {}
compound_prefabs = {}
errors = 0
total_sma_with_mesh = 0
total_sma_without_mesh = 0

for i in range(len(boundaries)):
    pkg_start = boundaries[i]
    pkg_end = boundaries[i + 1] if i + 1 < len(boundaries) else len(d)
    buf = d[pkg_start:]

    try:
        nc = struct.unpack("<I", buf[12:16])[0]
        no = struct.unpack("<I", buf[16:20])[0]
        ec = struct.unpack("<I", buf[20:24])[0]
        eo = struct.unpack("<I", buf[24:28])[0]
        ic = struct.unpack("<I", buf[28:32])[0]
        io_val = struct.unpack("<I", buf[32:36])[0]
        if nc > 10000 or ec > 10000 or ic > 10000:
            errors += 1
            continue

        names = []
        p = no
        for _ in range(nc):
            s, p = read_fstr(buf, p)
            p += 4
            names.append(s)

        prefab_name = None
        for n in names:
            if "exportBinaryPrefab" in n:
                prefab_name = n.split("exportBinaryPrefab")[0]
                break
        if not prefab_name:
            continue

        imports = []
        p = io_val
        for _ in range(ic):
            cp, p = read_ci(buf, p)
            cn, p = read_ci(buf, p)
            p += 4
            on, p = read_ci(buf, p)
            cname = names[cn] if 0 <= cn < len(names) else "???"
            oname = names[on] if 0 <= on < len(names) else "???"
            imports.append({"class": cname, "name": oname})

        export_info = []
        p = eo
        for _ in range(ec):
            ci2, p = read_ci(buf, p)
            si2, p = read_ci(buf, p)
            p += 4
            oni, p = read_ci(buf, p)
            p += 4
            ss, p = read_ci(buf, p)
            so2 = 0
            if ss > 0:
                so2, p = read_ci(buf, p)
            cls = ""
            if ci2 < 0:
                ii = -ci2 - 1
                cls = imports[ii]["name"] if ii < len(imports) else ""
            export_info.append((cls, ss, so2))

        mesh_components = []
        sub_refs = []

        for cls, ss, so2 in export_info:
            if ss <= 0:
                continue
            try:
                props = parse_all_props(buf, so2, ss, names, imports)
            except:
                continue

            if cls == "StaticMeshActor":
                sm = props.get("StaticMesh")
                if sm:
                    total_sma_with_mesh += 1
                    comp = {"mesh": sm}
                    if props.get("Location"):
                        comp["location"] = props["Location"]
                    if props.get("Rotation") and any(v != 0 for v in props["Rotation"]):
                        comp["rotation"] = props["Rotation"]
                    if props.get("DrawScale3D") and props["DrawScale3D"] != [
                        1.0,
                        1.0,
                        1.0,
                    ]:
                        comp["draw_scale_3d"] = props["DrawScale3D"]
                    if props.get("DrawScale") and props["DrawScale"] != 1.0:
                        comp["draw_scale"] = props["DrawScale"]
                    mesh_components.append(comp)
                else:
                    total_sma_without_mesh += 1

            elif cls in ("Light", "DynamicLight"):
                light = {"type": "light"}
                if props.get("Location"):
                    light["location"] = props["Location"]
                if props.get("Rotation") and any(v != 0 for v in props["Rotation"]):
                    light["rotation"] = props["Rotation"]
                # Brightness: float 0-255, default 64
                if "LightBrightness" in props:
                    light["brightness"] = props["LightBrightness"]
                # Radius: float in world units, default 64
                if "LightRadius" in props:
                    light["radius"] = props["LightRadius"]
                # Color: RGBA dict {R,G,B,A} bytes 0-255
                if "LightColor" in props and isinstance(props["LightColor"], dict):
                    lc = props["LightColor"]
                    light["color"] = [lc.get("R", 255), lc.get("G", 255), lc.get("B", 255)]
                # HSB: Hue/Saturation bytes 0-255
                if "LightHue" in props:
                    light["hue"] = props["LightHue"]
                if "LightSaturation" in props:
                    light["saturation"] = props["LightSaturation"]
                # Cone angle for spotlights
                if "LightCone" in props:
                    light["cone"] = props["LightCone"]
                # Type and effect enums (stored as ints)
                if "LightType" in props:
                    light["light_type"] = props["LightType"]
                if "LightEffect" in props:
                    light["light_effect"] = props["LightEffect"]
                if cls == "DynamicLight":
                    light["dynamic"] = True
                mesh_components.append(light)

            elif cls == "CompoundObject":
                pn = props.get("PrefabName")
                if pn:
                    ref = {"sub_prefab": pn}
                    if props.get("Location"):
                        ref["location"] = props["Location"]
                    if props.get("Rotation") and any(v != 0 for v in props["Rotation"]):
                        ref["rotation"] = props["Rotation"]
                    if props.get("DrawScale3D") and props["DrawScale3D"] != [1.0, 1.0, 1.0]:
                        ref["draw_scale_3d"] = props["DrawScale3D"]
                    if props.get("DrawScale") and props["DrawScale"] != 1.0:
                        ref["draw_scale"] = props["DrawScale"]
                    sub_refs.append(ref)

        if mesh_components:
            leaf_prefabs[prefab_name] = mesh_components
        if sub_refs:
            compound_prefabs[prefab_name] = sub_refs
    except:
        errors += 1

t1 = time.time()
print(
    f"Pass 1 ({t1-t0:.1f}s): {len(leaf_prefabs)} leaf, {len(compound_prefabs)} compound"
)
print(f"  SMA with mesh: {total_sma_with_mesh}, without: {total_sma_without_mesh}")
print(f"  Errors: {errors}")


# Recursive resolution
#
# Each CompoundObject reference carries its own Location/Rotation/DrawScale3D/
# DrawScale. When flattening, child components must be transformed by the
# parent's frame:
#   child_world_loc = parent_rot * (parent_scale * child_loc) + parent_loc
#   child_world_rot = parent_rot ∘ child_rot         (UE2 rotation units)
#   child_world_scale = parent_scale * child_scale
#
# UE2 FRotator: pitch=rot_around_Y, yaw=rot_around_Z, roll=rot_around_X.
# Units: 65536 == 2π radians. Rotation order follows FRotationMatrix:
# M = M_roll(X) * M_pitch(Y) * M_yaw(Z) applied to column vectors
# (i.e. yaw applied first, then pitch, then roll).
import math as _math

_ROT_SCALE = (2.0 * _math.pi) / 65536.0


def _rot_matrix(rot):
    """Build a 3x3 rotation matrix from a UE2 Rotator [pitch, yaw, roll]."""
    if not rot or (rot[0] == 0 and rot[1] == 0 and rot[2] == 0):
        return None
    p = rot[0] * _ROT_SCALE
    y = rot[1] * _ROT_SCALE
    r = rot[2] * _ROT_SCALE
    cp, sp = _math.cos(p), _math.sin(p)
    cy, sy = _math.cos(y), _math.sin(y)
    cr, sr = _math.cos(r), _math.sin(r)
    # M_yaw (Z)
    mz = [[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]]
    # M_pitch (Y)
    my = [[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]]
    # M_roll (X)
    mx = [[1, 0, 0], [0, cr, -sr], [0, sr, cr]]

    def matmul(a, b):
        return [
            [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)
        ]

    return matmul(mx, matmul(my, mz))


def _mat_apply(m, v):
    if m is None:
        return list(v)
    return [
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    ]


def _mat_mul(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return [
        [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]


def _mat_to_rotator(m):
    """Recover a UE2 [pitch, yaw, roll] (int rotation units) from a matrix
    built by _rot_matrix. Inverse of M_roll*M_pitch*M_yaw decomposition."""
    if m is None:
        return None
    # pitch = asin(m[0][2]) with m built as above  →  m[0][2] = sin(pitch)*cos(yaw)... not quite.
    # Simpler: recover Euler angles in the inverse order. m = Rx(r)*Ry(p)*Rz(y)
    # →  m[0][2] = sin(p)*cos(y)+... ; cleaner to re-derive numerically.
    # For our data, most rotations are yaw-only (building orientation), so we
    # accept a small round-trip error and extract yaw directly:
    #   m[0][0] = cos(y)*cos(p)   m[0][1] = -sin(y)*cos(p) + ...
    # Extracting full Euler from a composed matrix is fiddly; use atan2 on the
    # appropriate rows:
    sp = -m[2][0]
    # Clamp for float error.
    sp = max(-1.0, min(1.0, sp))
    p = _math.asin(sp)
    cp = _math.cos(p)
    if abs(cp) > 1e-6:
        y = _math.atan2(m[1][0], m[0][0])
        r = _math.atan2(m[2][1], m[2][2])
    else:
        # Gimbal lock — roll and yaw collapse; split arbitrarily.
        y = _math.atan2(-m[0][1], m[1][1])
        r = 0.0
    inv = 1.0 / _ROT_SCALE
    return [int(round(p * inv)) & 0xFFFF, int(round(y * inv)) & 0xFFFF, int(round(r * inv)) & 0xFFFF]


def resolve_prefab(name, visited=None, depth=0):
    if visited is None:
        visited = set()
    if name in visited or depth > 10:
        return []
    visited.add(name)
    result = []

    name_lower = name.lower()
    leaf = leaf_prefabs.get(name)
    if not leaf:
        for k in leaf_prefabs:
            if k.lower() == name_lower:
                leaf = leaf_prefabs[k]
                break
    if leaf:
        result.extend(leaf)

    compound = compound_prefabs.get(name)
    if not compound:
        for k in compound_prefabs:
            if k.lower() == name_lower:
                compound = compound_prefabs[k]
                break

    if compound:
        for ref in compound:
            sub_name = ref["sub_prefab"]
            parent_loc = ref.get("location") or [0, 0, 0]
            if not isinstance(parent_loc, list):
                parent_loc = [0, 0, 0]
            parent_rot = ref.get("rotation") or [0, 0, 0]
            if not isinstance(parent_rot, list):
                parent_rot = [0, 0, 0]
            parent_rot_m = _rot_matrix(parent_rot)
            parent_s3 = ref.get("draw_scale_3d") or [1.0, 1.0, 1.0]
            if not isinstance(parent_s3, list):
                parent_s3 = [1.0, 1.0, 1.0]
            parent_s = ref.get("draw_scale", 1.0)
            try:
                parent_s = float(parent_s)
            except (TypeError, ValueError):
                parent_s = 1.0
            sub_components = resolve_prefab(sub_name, visited.copy(), depth + 1)
            for comp in sub_components:
                child_loc = comp.get("location") or [0, 0, 0]
                if not isinstance(child_loc, list):
                    child_loc = [0, 0, 0]
                # Apply parent scale, then parent rotation, then translation.
                scaled = [
                    child_loc[0] * parent_s3[0] * parent_s,
                    child_loc[1] * parent_s3[1] * parent_s,
                    child_loc[2] * parent_s3[2] * parent_s,
                ]
                rotated = _mat_apply(parent_rot_m, scaled)
                new_comp = dict(comp)
                new_comp["location"] = [parent_loc[i] + rotated[i] for i in range(3)]

                # Compose rotation: world = parent ∘ child
                child_rot = comp.get("rotation")
                child_rot_m = _rot_matrix(child_rot) if child_rot else None
                composed = _mat_mul(parent_rot_m, child_rot_m)
                if composed is not None:
                    rec = _mat_to_rotator(composed)
                    if rec and any(v != 0 for v in rec):
                        new_comp["rotation"] = rec
                    else:
                        new_comp.pop("rotation", None)

                # Compose scale
                child_s3 = comp.get("draw_scale_3d") or [1.0, 1.0, 1.0]
                if not isinstance(child_s3, list):
                    child_s3 = [1.0, 1.0, 1.0]
                child_s = comp.get("draw_scale", 1.0)
                try:
                    child_s = float(child_s)
                except (TypeError, ValueError):
                    child_s = 1.0
                out_s3 = [parent_s3[i] * child_s3[i] for i in range(3)]
                out_s = parent_s * child_s
                if out_s3 != [1.0, 1.0, 1.0]:
                    new_comp["draw_scale_3d"] = out_s3
                else:
                    new_comp.pop("draw_scale_3d", None)
                if out_s != 1.0:
                    new_comp["draw_scale"] = out_s
                else:
                    new_comp.pop("draw_scale", None)

                result.append(new_comp)

    return result


all_prefabs = dict(leaf_prefabs)
for name in compound_prefabs:
    resolved = resolve_prefab(name)
    if resolved:
        all_prefabs[name] = resolved

t2 = time.time()
total_actors = sum(len(v) for v in all_prefabs.values())
print(f"Pass 2 ({t2-t1:.1f}s): {len(all_prefabs)} prefabs, {total_actors} actors")

os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
with open(OUT_PATH, "w") as f:
    json.dump(all_prefabs, f, indent=1)
print(f"Saved ({os.path.getsize(OUT_PATH)/1024:.0f} KB)")

# Check targets
targets = [
    "Ra3_P1_C1_Bridges_bridge001_ver01",
    "Ra3_P1_C1_Grid_BuVillage01Bank_bu001_ver01",
    "Ra3_P1_C1_Grid_BuVillage01Home_bu001_ver01",
    "Ra3_P1_C1_Grid_BuVillage01Shop_bu001_ver01",
    "Ra3_P1_C1_Grid_BuVillage01Temple_bu001_ver01",
    "Ra3_P1_C1_Grid_LethStairs_stair001_v01",
    "Ra3_P1_C1_Grid_LethStairs_stair002_v01",
    "Ra3_P1_C1_Grid_Arcanium01_Root001",
    "Ra3_P1_C1_Decor_gazebo002",
    "Ra3_P1_C1_Decor_bench001",
    "Ra3_P1_C1_Lights_streetLamp001",
    "Ra76_P1_C1_Camps_fairyRing004",
    "Ra3_P1_C1_Decor_plant003_sunlit01",
    "Ra3_P1_C1_Decor_firebowl001",
    "Ra3_P1_C1_Decor_fence001_curve01",
    "Ra03_P1_C1_Grid_Tomb01_root_quest01",
]

print(f"\n{'='*60}")
found = 0
for name in targets:
    if name in all_prefabs:
        comps = all_prefabs[name]
        found += 1
        meshes = set(c.get("mesh", "?") for c in comps)
        has_offsets = any(
            any(v != 0 for v in c.get("location", [0, 0, 0])) for c in comps
        )
        print(
            f"  ✓ {name}: {len(comps)} comps, {len(meshes)} meshes, offsets={'yes' if has_offsets else 'no'}"
        )
    else:
        print(f"  ✗ {name}")
print(f"\nFound: {found}/{len(targets)}")
