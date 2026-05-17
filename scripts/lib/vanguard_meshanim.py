"""
Parser for UE2 MeshAnimation data from .ukx packages.

Parses native binary serialization of UMeshAnimation exports to extract:
- RefBones: skeleton bone hierarchy
- Moves: per-animation MotionChunks with keyframe data (AnalogTrack / FlexTrack)
- AnimSeqs: animation sequence metadata (name, frame count, rate, notifies)

Also parses UE2 SkeletalMesh RefSkeleton for bind-pose bone transforms, and
exports animation data as standalone glTF 2.0 files.

Format based on UE2.5 source: UnSkeletalMesh.h, UnSkeletalMesh.cpp, UnMesh.cpp, UnAnim.h.

Vanguard .ukx files come in two package versions:
- v126 (lic 8-13): TArray counts use compact-index encoding (AR_INDEX)
- v129 (lic 34):   TArray counts use plain INT32
FName refs use compact-index in both versions.
"""

import struct
import math
import json
import base64
import array as _array


# ── Primitive readers ──

def _read_compact_index(data, off):
    b0 = data[off]
    neg = bool(b0 & 0x80)
    more = bool(b0 & 0x40)
    val = b0 & 0x3F
    off += 1
    if more and off < len(data):
        b = data[off]; off += 1
        more2 = bool(b & 0x80)
        val |= (b & 0x7F) << 6
        if more2 and off < len(data):
            b = data[off]; off += 1
            more3 = bool(b & 0x80)
            val |= (b & 0x7F) << 13
            if more3 and off < len(data):
                b = data[off]; off += 1
                val |= (b & 0x7F) << 20
    if neg:
        val = -val
    return val, off

def _read_int32(data, off):
    return struct.unpack_from("<i", data, off)[0], off + 4

def _read_uint32(data, off):
    return struct.unpack_from("<I", data, off)[0], off + 4

def _read_float(data, off):
    return struct.unpack_from("<f", data, off)[0], off + 4

def _read_word(data, off):
    return struct.unpack_from("<H", data, off)[0], off + 2

def _read_fname(data, off, names):
    idx, off = _read_compact_index(data, off)
    name = names[idx] if 0 <= idx < len(names) else f"?{idx}"
    return name, off

def _read_fvector(data, off):
    x, y, z = struct.unpack_from("<3f", data, off)
    return (x, y, z), off + 12

def _read_fquat(data, off):
    x, y, z, w = struct.unpack_from("<4f", data, off)
    return (x, y, z, w), off + 16

def _read_fquat_packed(data, off):
    x, y, z = struct.unpack_from("<3f", data, off)
    w_sq = 1.0 - x*x - y*y - z*z
    w = math.sqrt(max(0.0, w_sq))
    return (x, y, z, w), off + 12

def _read_fquat16(data, off):
    xi, yi, zi = struct.unpack_from("<3H", data, off)
    x = (xi - 32767) / 32767.0
    y = (yi - 32767) / 32767.0
    z = (zi - 32767) / 32767.0
    w_sq = 1.0 - x*x - y*y - z*z
    w = math.sqrt(max(0.0, w_sq))
    return (x, y, z, w), off + 6


# ── TArray count reader (version-dependent) ──

def _make_read_count(pkg_version):
    """Return a count-reader function: compact-index for v<=127, INT32 for v128+."""
    if pkg_version >= 128:
        return lambda data, off: _read_int32(data, off)
    return lambda data, off: _read_compact_index(data, off)


# ── TArray readers (count function passed in) ──

def _read_tarray_int(data, off, rc):
    count, off = rc(data, off)
    vals = []
    for _ in range(count):
        v, off = _read_int32(data, off)
        vals.append(v)
    return vals, off

def _read_tarray_float(data, off, rc):
    count, off = rc(data, off)
    vals = []
    for _ in range(count):
        v, off = _read_float(data, off)
        vals.append(v)
    return vals, off

def _read_tarray_word(data, off, rc):
    count, off = rc(data, off)
    vals = []
    for _ in range(count):
        v, off = _read_word(data, off)
        vals.append(v)
    return vals, off

def _read_tarray_fvector(data, off, rc):
    count, off = rc(data, off)
    vals = []
    for _ in range(count):
        v, off = _read_fvector(data, off)
        vals.append(v)
    return vals, off

def _read_tarray_fquat(data, off, rc):
    count, off = rc(data, off)
    vals = []
    for _ in range(count):
        v, off = _read_fquat(data, off)
        vals.append(v)
    return vals, off

def _read_tarray_fquat_packed(data, off, rc):
    count, off = rc(data, off)
    vals = []
    for _ in range(count):
        v, off = _read_fquat_packed(data, off)
        vals.append(v)
    return vals, off

def _read_tarray_fquat16(data, off, rc):
    count, off = rc(data, off)
    vals = []
    for _ in range(count):
        v, off = _read_fquat16(data, off)
        vals.append(v)
    return vals, off

def _read_tarray_fname(data, off, names, rc):
    count, off = rc(data, off)
    vals = []
    for _ in range(count):
        v, off = _read_fname(data, off, names)
        vals.append(v)
    return vals, off


# ── Composite structure readers ──

def _read_analog_track(data, off, rc):
    flags, off = _read_uint32(data, off)
    key_quat, off = _read_tarray_fquat(data, off, rc)
    key_pos, off = _read_tarray_fvector(data, off, rc)
    key_time, off = _read_tarray_float(data, off, rc)
    return {"flags": flags, "key_quat": key_quat, "key_pos": key_pos, "key_time": key_time}, off

def _read_flex_track_slot(data, off, rc):
    class_type, off = _read_int32(data, off)
    track = {"type": class_type}
    if class_type == 0:  # ADT_Empty
        pass
    elif class_type == 1:  # ADT_Static
        track["orientation"], off = _read_fquat_packed(data, off)
        track["position"], off = _read_fvector(data, off)
    elif class_type == 2:  # ADT_Raw
        track["orientations"], off = _read_tarray_fquat_packed(data, off, rc)
        track["positions"], off = _read_tarray_fvector(data, off, rc)
        track["time_keys"], off = _read_tarray_word(data, off, rc)
    elif class_type == 3:  # ADT_48Bit
        track["orientations"], off = _read_tarray_fquat16(data, off, rc)
        track["positions"], off = _read_tarray_fvector(data, off, rc)
        track["time_keys"], off = _read_tarray_word(data, off, rc)
    elif class_type == 4:  # ADT_48BitRotOnly
        track["orientations"], off = _read_tarray_fquat16(data, off, rc)
        track["time_keys"], off = _read_tarray_word(data, off, rc)
        track["position"], off = _read_fvector(data, off)
    else:
        raise ValueError(f"Unknown FlexTrack type {class_type} at offset {off-4}")
    return track, off

def _read_motion_chunk(data, off, rc):
    root_speed, off = _read_fvector(data, off)
    track_time, off = _read_float(data, off)
    start_bone, off = _read_int32(data, off)
    internal_version, off = _read_int32(data, off)
    bone_indices, off = _read_tarray_int(data, off, rc)
    num_anim_tracks, off = rc(data, off)
    anim_tracks = []
    for _ in range(num_anim_tracks):
        t, off = _read_analog_track(data, off, rc)
        anim_tracks.append(t)
    root_track, off = _read_analog_track(data, off, rc)
    flex_tracks = []
    if internal_version >= 3:
        num_flex, off = rc(data, off)
        for _ in range(num_flex):
            ft, off = _read_flex_track_slot(data, off, rc)
            flex_tracks.append(ft)
    return {
        "root_speed": root_speed, "track_time": track_time,
        "start_bone": start_bone, "internal_version": internal_version,
        "bone_indices": bone_indices, "anim_tracks": anim_tracks,
        "root_track": root_track, "flex_tracks": flex_tracks,
    }, off

def _read_mesh_anim_notify(data, off, names, rc):
    time, off = _read_float(data, off)
    func_name, off = _read_fname(data, off, names)
    obj_ref, off = _read_compact_index(data, off)
    return {"time": time, "function": func_name, "object_ref": obj_ref}, off

def _read_mesh_anim_seq(data, off, names, rc):
    """FMeshAnimSeq (ver >= 115): Bookmark, Name, Groups, StartFrame, NumFrames, Notifys, Rate"""
    bookmark, off = _read_float(data, off)
    name, off = _read_fname(data, off, names)
    groups, off = _read_tarray_fname(data, off, names, rc)
    start_frame, off = _read_int32(data, off)
    num_frames, off = _read_int32(data, off)
    num_notifys, off = rc(data, off)
    notifys = []
    for _ in range(num_notifys):
        n, off = _read_mesh_anim_notify(data, off, names, rc)
        notifys.append(n)
    rate, off = _read_float(data, off)
    return {
        "name": name, "groups": groups, "start_frame": start_frame,
        "num_frames": num_frames, "rate": rate, "notifys": notifys, "bookmark": bookmark,
    }, off


# ── Top-level parser ──

def parse_mesh_animation(data, names, pkg_version=126):
    """Parse a MeshAnimation export's raw serial data.
    
    Args:
        data: Raw bytes from UE2Package.get_export_data()
        names: Package name table list
        pkg_version: Package version (126=compact-index counts, 129+=INT32 counts)
        
    Returns:
        Dict with keys: internal_version, ref_bones, moves, anim_seqs
    """
    rc = _make_read_count(pkg_version)

    # Skip None property terminator (compact index to "None" name entry)
    none_idx, off = _read_compact_index(data, 0)
    if none_idx < 0 or none_idx >= len(names) or names[none_idx] != "None":
        raise ValueError(f"Expected None terminator at byte 0, got name index {none_idx}")

    internal_version, off = _read_int32(data, off)

    # TArray<FNamedBone> RefBones
    num_bones, off = rc(data, off)
    ref_bones = []
    for _ in range(num_bones):
        bone_name, off = _read_fname(data, off, names)
        flags, off = _read_uint32(data, off)
        parent_idx, off = _read_int32(data, off)
        ref_bones.append({"name": bone_name, "flags": flags, "parent_index": parent_idx})

    # TArray<MotionChunk> Moves
    num_moves, off = rc(data, off)
    moves = []
    for _ in range(num_moves):
        move, off = _read_motion_chunk(data, off, rc)
        moves.append(move)

    # TArray<FMeshAnimSeq> AnimSeqs
    num_seqs, off = rc(data, off)
    anim_seqs = []
    for _ in range(num_seqs):
        seq, off = _read_mesh_anim_seq(data, off, names, rc)
        anim_seqs.append(seq)

    return {
        "internal_version": internal_version,
        "ref_bones": ref_bones,
        "moves": moves,
        "anim_seqs": anim_seqs,
        "bytes_parsed": off,
        "bytes_total": len(data),
    }


# ── UE2 SkeletalMesh RefSkeleton parser ──

def find_refskeleton(data, names):
    """Scan a SkeletalMesh export's raw data for the RefSkeleton array.

    FMeshBone serialization: FName(ci) + Flags(4) + VJointPos(44) +
    NumChildren(4) + ParentIndex(4) = compact_index + 56 fixed bytes.

    Returns a list of dicts with keys: name, parent_index, quat, pos,
    num_children, flags.  Returns None if not found.
    """

    def _try_read_bones(start, max_bones=300):
        off = start
        bones = []
        for i in range(max_bones):
            if off + 57 > len(data):
                break
            try:
                name_ci, name_end = _read_compact_index(data, off)
                if name_ci < 0 or name_ci >= len(names):
                    break
                fixed = name_end
                if fixed + 56 > len(data):
                    break
                flags = struct.unpack_from("<I", data, fixed)[0]
                qx, qy, qz, qw = struct.unpack_from("<4f", data, fixed + 4)
                px, py, pz = struct.unpack_from("<3f", data, fixed + 20)
                # skip Length, XSize, YSize, ZSize (16 bytes)
                nch = struct.unpack_from("<i", data, fixed + 48)[0]
                par = struct.unpack_from("<i", data, fixed + 52)[0]
                q_len = (qx * qx + qy * qy + qz * qz + qw * qw) ** 0.5
                if nch < 0 or nch > 100:
                    break
                if i > 0 and (par < 0 or par >= i):
                    break
                if q_len < 0.5 or q_len > 2.0:
                    break
                if abs(px) > 100000 or abs(py) > 100000 or abs(pz) > 100000:
                    break
                bones.append({
                    "name": names[name_ci], "flags": flags,
                    "quat": (qx, qy, qz, qw), "pos": (px, py, pz),
                    "num_children": nch, "parent_index": par,
                })
                off = fixed + 56
            except Exception:
                break
        return bones if len(bones) >= 3 else None

    best = None
    for start in range(len(data) - 100):
        bones = _try_read_bones(start)
        if bones and (best is None or len(bones) > len(best)):
            best = bones
            if len(bones) >= 20:
                # verify count prefix
                for try_off in range(max(0, start - 5), start):
                    ci_val, ci_end = _read_compact_index(data, try_off)
                    if ci_end == start and ci_val == len(bones):
                        return best
                    if try_off + 4 == start:
                        i32 = struct.unpack_from("<I", data, try_off)[0]
                        if i32 == len(bones):
                            return best
                return best
    return best


# ── glTF animation export ──

def export_animation_gltf(anim_data, bind_bones, output_path):
    """Export parsed animation data as a standalone glTF 2.0 file.

    Creates a skeleton from *bind_bones* (UE2 RefSkeleton or animation
    RefBones) and adds one glTF animation per AnimSeq.

    Args:
        anim_data: Dict returned by ``parse_mesh_animation``.
        bind_bones: List of dicts with keys *name*, *parent_index*,
            *quat* ``(x,y,z,w)``, *pos* ``(x,y,z)``.  When a parsed
            RefSkeleton is available use that; otherwise the anim
            RefBones (which lack bind-pose transforms) can be used with
            the first-frame fallback.
        output_path: Path to write the ``.gltf`` file.

    Returns:
        The glTF dict that was written.
    """
    ref_bones = anim_data["ref_bones"]
    moves = anim_data["moves"]
    anim_seqs = anim_data["anim_seqs"]
    num_bones = len(ref_bones)

    # ── skeleton nodes ──
    children_map = {}
    for bi, bone in enumerate(ref_bones):
        pi = bone["parent_index"]
        if pi != bi and pi >= 0:
            children_map.setdefault(pi, []).append(bi)

    # Node 0 is a wrapper that converts UE2 to glTF coordinate system.
    # UE2 uses row-vector math (FastQuatToFCoords produces transposed matrix
    # vs glTF column-vector convention), so all bone quaternions are conjugated
    # (X,Y,Z negated, W kept) to produce correct rotations in glTF/Three.js.
    # UE2 axes (with conjugated quats): X=lateral, -Y=up, -Z=forward.
    # glTF axes: X=right, Y=up, -Z=forward.
    # Mapping: (x,y,z) -> (x, -y, -z) = 180° rotation around X.
    # Quaternion for 180° around X: (sin(90°), 0, 0, cos(90°)) = (1, 0, 0, 0)
    wrapper_node = {
        "name": "ue2_to_gltf",
        "rotation": [1.0, 0.0, 0.0, 0.0],
    }

    gltf_nodes = [wrapper_node]  # index 0 = wrapper
    root_joints = []
    bone_node_offset = 1  # bone nodes start at index 1

    for bi in range(num_bones):
        node = {"name": ref_bones[bi]["name"]}

        # Bind pose from RefSkeleton if available, else first frame.
        # Quaternions are conjugated (negate X,Y,Z) for UE2 row-vector → glTF
        # column-vector conversion. Positions stay unchanged (wrapper handles axes).
        bb = bind_bones[bi] if bi < len(bind_bones) else None
        if bb and "quat" in bb:
            qx, qy, qz, qw = bb["quat"]
            node["rotation"] = [-qx, -qy, -qz, qw]
            px, py, pz = bb["pos"]
            node["translation"] = [px, py, pz]
        elif moves and bi < len(moves[0]["anim_tracks"]):
            t = moves[0]["anim_tracks"][bi]
            if t["key_quat"]:
                qx, qy, qz, qw = t["key_quat"][0]
                node["rotation"] = [-qx, -qy, -qz, qw]
            if t["key_pos"]:
                px, py, pz = t["key_pos"][0]
                node["translation"] = [px, py, pz]

        kids = children_map.get(bi, [])
        if kids:
            node["children"] = [k + bone_node_offset for k in kids]

        gltf_nodes.append(node)

        pi = ref_bones[bi]["parent_index"]
        if pi == bi or pi < 0:
            root_joints.append(bi + bone_node_offset)

    # Attach root joints as children of the wrapper node
    wrapper_node["children"] = list(root_joints)

    # ── animation buffer data ──
    buffer_parts = []
    buffer_views = []
    accessors = []
    animations = []
    current_offset = 0

    for seq_idx, seq in enumerate(anim_seqs):
        if seq_idx >= len(moves):
            break
        move = moves[seq_idx]
        rate = seq["rate"] if seq["rate"] > 0 else 30.0

        anim_channels = []
        anim_samplers = []

        for bone_idx in range(min(num_bones, len(move["anim_tracks"]))):
            track = move["anim_tracks"][bone_idx]
            n_keys = len(track["key_quat"])
            if n_keys == 0:
                continue

            # Time values
            time_arr = _array.array("f")
            for ki in range(n_keys):
                t = (track["key_time"][ki] / rate) if ki < len(track["key_time"]) else ki / rate
                time_arr.append(t)
            time_bytes = time_arr.tobytes()
            time_min = float(min(time_arr))
            time_max = float(max(time_arr))

            # Rotation (xyzw) — conjugated for UE2→glTF conversion
            rot_arr = _array.array("f")
            for qx, qy, qz, qw in track["key_quat"]:
                rot_arr.extend([-qx, -qy, -qz, qw])
            rot_bytes = rot_arr.tobytes()

            # Translation — raw UE2 space, wrapper handles conversion
            pos_arr = _array.array("f")
            n_pos = len(track["key_pos"])
            for ki in range(n_keys):
                if ki < n_pos:
                    px, py, pz = track["key_pos"][ki]
                elif n_pos > 0:
                    px, py, pz = track["key_pos"][-1]
                else:
                    px, py, pz = 0.0, 0.0, 0.0
                pos_arr.extend([px, py, pz])
            pos_bytes = pos_arr.tobytes()

            # Buffer views + accessors for time, rotation, translation
            for bdata, comp_type, atype, minmax in [
                (time_bytes, 5126, "SCALAR", ([time_min], [time_max])),
                (rot_bytes, 5126, "VEC4", None),
                (pos_bytes, 5126, "VEC3", None),
            ]:
                bv = len(buffer_views)
                buffer_views.append({
                    "buffer": 0, "byteOffset": current_offset,
                    "byteLength": len(bdata),
                })
                buffer_parts.append(bdata)
                current_offset += len(bdata)
                acc = {"bufferView": bv, "componentType": comp_type,
                       "count": n_keys, "type": atype}
                if minmax:
                    acc["min"], acc["max"] = minmax
                accessors.append(acc)

            base_acc = len(accessors) - 3  # time, rot, pos

            # Samplers: rotation then translation
            s_rot = len(anim_samplers)
            anim_samplers.append({"input": base_acc, "output": base_acc + 1,
                                  "interpolation": "LINEAR"})
            s_pos = len(anim_samplers)
            anim_samplers.append({"input": base_acc, "output": base_acc + 2,
                                  "interpolation": "LINEAR"})

            bone_node = bone_idx + bone_node_offset
            anim_channels.append({"sampler": s_rot,
                                  "target": {"node": bone_node, "path": "rotation"}})
            anim_channels.append({"sampler": s_pos,
                                  "target": {"node": bone_node, "path": "translation"}})

        if anim_channels:
            animations.append({
                "name": seq["name"],
                "channels": anim_channels,
                "samplers": anim_samplers,
            })

    # ── assemble buffer ──
    buffer_bytes = b"".join(buffer_parts)
    buffer_uri = ("data:application/octet-stream;base64,"
                  + base64.b64encode(buffer_bytes).decode("ascii"))

    gltf = {
        "asset": {"version": "2.0", "generator": "vanguard_meshanim"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": gltf_nodes,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"uri": buffer_uri, "byteLength": len(buffer_bytes)}],
    }
    if animations:
        gltf["animations"] = animations

    with open(output_path, "w") as f:
        json.dump(gltf, f)

    return gltf
