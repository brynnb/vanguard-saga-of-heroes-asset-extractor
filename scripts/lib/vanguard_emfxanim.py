"""
Parser for Vanguard EMotion FX Motion (FXM v1.1) animation data from .uea packages.

Parses the FXM binary format embedded in EMFXAnim exports to extract:
- Per-bone animation keyframes (rotation quaternions + position vectors)
- Submotion bone names (matching FXA skeleton bone names)
- Pose and bind-pose transforms per bone

FXM v1.1 chunk structure (chunk IDs confirmed via Ghidra VGClient.exe decompilation):
  Chunk ID=16 (INFO):         Metadata (source app, filename, export date)
  Chunk ID=1  (MOTION_PART):  80-byte submotion header + bone name string
  Chunk ID=2  (ANIM_KEYFRAME): 8-byte header (NrKeys, IPType, AnimType) + keyframe data

Animation chunks follow their associated NODE chunk:
  NODE(bone_A) → ANIM(rotation for A) → ANIM(position for A) → NODE(bone_B) → ...

Format reference: EMFX_GUIDE.md in the project root.
"""

import math
import os
import struct


# --------------------------------------------------------------------------- #
# Quaternion helpers (XYZW format)
# --------------------------------------------------------------------------- #

def _quat_inv(q):
    """Return the inverse (conjugate) of a unit quaternion (x,y,z,w)."""
    return (-q[0], -q[1], -q[2], q[3])


def _quat_mul(a, b):
    """Hamilton product of two quaternions (x,y,z,w)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

class FXMSubMotion:
    """Per-bone animation submotion with pose transforms and keyframes."""

    __slots__ = (
        "name",
        "pose_pos", "pose_rot", "pose_scale",
        "bind_pose_pos", "bind_pose_rot", "bind_pose_scale",
        "rot_keys",   # list of (time, qx, qy, qz, qw)
        "pos_keys",   # list of (time, x, y, z)
    )

    def __init__(self):
        self.name = ""
        self.pose_pos = (0.0, 0.0, 0.0)
        self.pose_rot = (0.0, 0.0, 0.0, 1.0)
        self.pose_scale = (1.0, 1.0, 1.0)
        self.bind_pose_pos = (0.0, 0.0, 0.0)
        self.bind_pose_rot = (0.0, 0.0, 0.0, 1.0)
        self.bind_pose_scale = (1.0, 1.0, 1.0)
        self.rot_keys = []
        self.pos_keys = []


class FXMAnimation:
    """A single parsed FXM animation clip."""

    __slots__ = (
        "name",
        "submotions",     # list of FXMSubMotion
        "duration",       # max keyframe time across all tracks
        "source_app",
        "original_filename",
        "export_date",
    )

    def __init__(self):
        self.name = ""
        self.submotions = []
        self.duration = 0.0
        self.source_app = ""
        self.original_filename = ""
        self.export_date = ""


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _read_string(data, off):
    """Read a uint32-length-prefixed ASCII string."""
    if off + 4 > len(data):
        return "", off
    slen = struct.unpack_from("<I", data, off)[0]
    if slen > 2000 or off + 4 + slen > len(data):
        return "", off
    try:
        s = data[off + 4 : off + 4 + slen].decode("ascii")
    except (UnicodeDecodeError, ValueError):
        s = ""
    return s, off + 4 + slen


def _parse_info_chunk(cdata):
    """Parse FXM INFO chunk (ID=16) — same format as FXA INFO."""
    if len(cdata) < 8:
        return "", "", ""
    off = 8  # skip 8-byte header (exporter version + unknown)
    src, off = _read_string(cdata, off)
    fname, off = _read_string(cdata, off)
    date, off = _read_string(cdata, off)
    return src, fname, date


def _parse_node_chunk(cdata):
    """Parse FXM MOTION_PART chunk (ID=1, ver=3): 80-byte header + bone name string.

    Header layout (80 bytes) — confirmed via Ghidra MotionPartChunkProcessor3:
      FileVector3     mPosePos       (12B)  — bytes  0-11
      FileQuaternion  mPoseRot       (16B)  — bytes 12-27  (XYZW)
      FileVector3     mPoseScale     (12B)  — bytes 28-39
      FileVector3     mBindPosePos   (12B)  — bytes 40-51
      FileQuaternion  mBindPoseRot   (16B)  — bytes 52-67  (XYZW)
      FileVector3     mBindPoseScale (12B)  — bytes 68-79
    Followed by:
      string          bone name
    """
    if len(cdata) < 84:
        return None

    sm = FXMSubMotion()

    # Pose position
    sm.pose_pos = struct.unpack_from("<3f", cdata, 0)
    # Pose rotation (quaternion XYZW)
    sm.pose_rot = struct.unpack_from("<4f", cdata, 12)
    # Pose scale
    sm.pose_scale = struct.unpack_from("<3f", cdata, 28)
    # Bind pose position
    sm.bind_pose_pos = struct.unpack_from("<3f", cdata, 40)
    # Bind pose rotation (quaternion XYZW)
    sm.bind_pose_rot = struct.unpack_from("<4f", cdata, 52)
    # Bind pose scale
    sm.bind_pose_scale = struct.unpack_from("<3f", cdata, 68)

    # Bone name string at offset 80
    sm.name, _ = _read_string(cdata, 80)
    return sm


def _parse_anim_chunk(cdata):
    """Parse FXM ANIM chunk (ID=2, ver=1): keyframe data.

    Header (8 bytes) — confirmed via Ghidra decompilation of AnimKeyFrameChunkProcessor:
      uint32  numKeys
      byte    IPType    (interpolation: 'L'=linear, 'H'=hermite)
      byte    AnimType  ('R'=rotation quaternion, 'P'=position vector)
      2 bytes padding

    Key formats (determined by AnimType field):
      'R' Rotation: 20 bytes/key = float time + float qx, qy, qz, qw
      'P' Position: 16 bytes/key = float time + float x, y, z

    Returns: ("rotation", keys) or ("position", keys) or None
    """
    if len(cdata) < 8:
        return None

    num_keys = struct.unpack_from("<I", cdata, 0)[0]
    if num_keys == 0 or num_keys > 100000:
        return None

    ip_type = chr(cdata[4]) if cdata[4] != 0 else '?'
    anim_type = chr(cdata[5]) if cdata[5] != 0 else '?'

    remaining = len(cdata) - 8

    # Use AnimType field from header when available, fall back to size heuristic
    if anim_type == 'R' or (anim_type == '?' and remaining == num_keys * 20):
        if remaining < num_keys * 20:
            return None
        # Rotation keys: time + quaternion (XYZW)
        keys = []
        off = 8
        for _ in range(num_keys):
            t = struct.unpack_from("<f", cdata, off)[0]
            qx, qy, qz, qw = struct.unpack_from("<4f", cdata, off + 4)
            keys.append((t, qx, qy, qz, qw))
            off += 20
        return "rotation", keys

    elif anim_type == 'P' or (anim_type == '?' and remaining == num_keys * 16):
        if remaining < num_keys * 16:
            return None
        # Position keys: time + vec3
        keys = []
        off = 8
        for _ in range(num_keys):
            t = struct.unpack_from("<f", cdata, off)[0]
            x, y, z = struct.unpack_from("<3f", cdata, off + 4)
            keys.append((t, x, y, z))
            off += 16
        return "position", keys

    return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

FXM_CHUNK_NODE = 1       # MOTION_PART: 80B header + bone name
FXM_CHUNK_ANIM = 2       # ANIM_KEYFRAME: 8B header (NrKeys, IPType, AnimType) + keys
FXM_CHUNK_INFO = 16      # INFO: metadata (source app, filename, export date)


def parse_fxm(fxm_data):
    """Parse FXM binary data starting at the "FXM " signature.

    Returns an FXMAnimation with all submotions and keyframes.
    """
    anim = FXMAnimation()

    if len(fxm_data) < 6 or fxm_data[:4] != b"FXM ":
        return anim

    off = 6  # skip "FXM " + 2 version bytes
    current_submotion = None
    max_time = 0.0

    while off + 12 <= len(fxm_data):
        chunk_id = struct.unpack_from("<I", fxm_data, off)[0]
        chunk_size = struct.unpack_from("<I", fxm_data, off + 4)[0]
        chunk_ver = struct.unpack_from("<I", fxm_data, off + 8)[0]

        if chunk_size > len(fxm_data) - off - 12 or chunk_ver > 100 or chunk_id > 100:
            break

        cdata = fxm_data[off + 12 : off + 12 + chunk_size]
        off += 12 + chunk_size

        if chunk_id == FXM_CHUNK_INFO:
            try:
                anim.source_app, anim.original_filename, anim.export_date = (
                    _parse_info_chunk(cdata)
                )
            except (struct.error, ValueError):
                pass

        elif chunk_id == FXM_CHUNK_NODE:
            sm = _parse_node_chunk(cdata)
            if sm is not None:
                anim.submotions.append(sm)
                current_submotion = sm

        elif chunk_id == FXM_CHUNK_ANIM:
            result = _parse_anim_chunk(cdata)
            if result is not None and current_submotion is not None:
                ktype, keys = result
                if ktype == "rotation":
                    current_submotion.rot_keys = keys
                    if keys:
                        max_time = max(max_time, keys[-1][0])
                elif ktype == "position":
                    current_submotion.pos_keys = keys
                    if keys:
                        max_time = max(max_time, keys[-1][0])

    anim.duration = max_time
    return anim


def parse_emfxanim_export(export_data):
    """Parse an EMFXAnim export's raw binary data.

    Locates the FXM signature within the export data and parses it.
    Returns an FXMAnimation.
    """
    fxm_off = export_data.find(b"FXM ")
    if fxm_off < 0:
        return FXMAnimation()
    anim = parse_fxm(export_data[fxm_off:])
    return anim


def get_animated_submotions(anim):
    """Return only submotions that have actual keyframe data."""
    return [
        sm for sm in anim.submotions
        if sm.rot_keys or sm.pos_keys
    ]


def get_static_pose_submotions(anim):
    """Return submotions that carry a static MOTION_PART pose and no keys.

    Vanguard playable face pose clips such as UEA_human_M_pose store their useful
    facial data in MOTION_PART pose/bind-pose transforms. They have no
    ANIM_KEYFRAME payload, so keyframe-only exporters must handle them
    separately instead of treating them as empty animations.
    """
    return [
        sm for sm in anim.submotions
        if not sm.rot_keys and not sm.pos_keys
    ]


def submotion_pose_transform(sm, use_bind_pose=True):
    """Return a serializable local TRS tuple from an FXM submotion.

    The observed Vanguard face-pose clips have identical pose and bind-pose
    fields for the authored static pose. The bind-pose fields are the ones used
    by the animation export pipeline for FXM rest correction, so they remain the
    default here.
    """
    if use_bind_pose:
        return {
            "position": list(sm.bind_pose_pos),
            "rotation": list(sm.bind_pose_rot),
            "scale": list(sm.bind_pose_scale),
        }
    return {
        "position": list(sm.pose_pos),
        "rotation": list(sm.pose_rot),
        "scale": list(sm.pose_scale),
    }


def submotion_rest_delta(sm, rest_node, use_bind_pose=True):
    """Return local TRS plus rest-relative deltas against an FXA node."""
    pose = submotion_pose_transform(sm, use_bind_pose=use_bind_pose)
    rest_pos = rest_node.position
    rest_rot = rest_node.rotation
    rest_scale = rest_node.scale
    pose_pos = tuple(pose["position"])
    pose_rot = tuple(pose["rotation"])
    pose_scale = tuple(pose["scale"])
    return {
        "position": list(pose_pos),
        "rotation": list(pose_rot),
        "scale": list(pose_scale),
        "rest_position": list(rest_pos),
        "rest_rotation": list(rest_rot),
        "rest_scale": list(rest_scale),
        "position_delta": [
            pose_pos[0] - rest_pos[0],
            pose_pos[1] - rest_pos[1],
            pose_pos[2] - rest_pos[2],
        ],
        "rotation_delta": list(_quat_mul(_quat_inv(rest_rot), pose_rot)),
        "scale_delta": [
            pose_scale[0] / rest_scale[0] if rest_scale[0] else pose_scale[0],
            pose_scale[1] / rest_scale[1] if rest_scale[1] else pose_scale[1],
            pose_scale[2] / rest_scale[2] if rest_scale[2] else pose_scale[2],
        ],
    }


def _compute_fxm_bind_ibms(anim, mesh_nodes, bone_name_to_node):
    """Compute inverse bind matrices from FXM animation bind pose.

    Uses the mesh's bone hierarchy (parent-child relationships) but substitutes
    FXM bind pose local transforms. This produces IBMs where non-animated bones
    are at their natural FXM resting position instead of mesh T-pose.

    Returns dict with 'ibm_bytes', 'n_joints', 'joint_indices', or None if
    mesh_nodes don't overlap with animation submotions.
    """
    import struct as _struct

    def qm(q1, q2):
        x1, y1, z1, w1 = q1
        x2, y2, z2, w2 = q2
        return (
            w1*x2 + x1*w2 + y1*z2 - z1*y2,
            w1*y2 - x1*z2 + y1*w2 + z1*x2,
            w1*z2 + x1*y2 - y1*x2 + z1*w2,
            w1*w2 - x1*x2 - y1*y2 - z1*z2,
        )

    def qr(q, v):
        vq = (v[0], v[1], v[2], 0.0)
        qc = (-q[0], -q[1], -q[2], q[3])
        r = qm(qm(q, vq), qc)
        return (r[0], r[1], r[2])

    # Build FXM bind pose lookup
    fxm_bind = {}  # bone_name -> (pos, rot)
    for sm in anim.submotions:
        fxm_bind[sm.name] = (sm.bind_pose_pos, sm.bind_pose_rot)

    # Walk mesh hierarchy, use FXM bind transforms (fall back to mesh if missing)
    n = len(mesh_nodes)
    world_pos = [(0.0, 0.0, 0.0)] * n
    world_rot = [(0.0, 0.0, 0.0, 1.0)] * n

    for i, node in enumerate(mesh_nodes):
        if node.name in fxm_bind:
            local_pos, local_rot = fxm_bind[node.name]
        else:
            local_pos = node.position
            local_rot = node.rotation

        if node.parent_index < 0:
            world_pos[i] = local_pos
            world_rot[i] = local_rot
        else:
            pi = node.parent_index
            rotated = qr(world_rot[pi], local_pos)
            world_pos[i] = (
                world_pos[pi][0] + rotated[0],
                world_pos[pi][1] + rotated[1],
                world_pos[pi][2] + rotated[2],
            )
            world_rot[i] = qm(world_rot[pi], local_rot)

    # Compute IBMs for each mesh bone -> column-major 4x4
    # Map mesh node index -> animation node index
    ibm_list = []  # list of 16-float IBM, one per mesh node
    joint_indices = []  # animation glTF node indices corresponding to each IBM

    mesh_name_to_idx = {node.name: i for i, node in enumerate(mesh_nodes)}

    for i, node in enumerate(mesh_nodes):
        anim_node_idx = bone_name_to_node.get(node.name)
        if anim_node_idx is None:
            continue  # mesh bone not in animation — skip

        qx, qy, qz, qw = world_rot[i]
        px, py, pz = world_pos[i]

        # Inverse rotation (transpose of rotation matrix)
        r00 = 1.0 - 2.0*(qy*qy + qz*qz)
        r01 = 2.0*(qx*qy + qz*qw)
        r02 = 2.0*(qx*qz - qy*qw)
        r10 = 2.0*(qx*qy - qz*qw)
        r11 = 1.0 - 2.0*(qx*qx + qz*qz)
        r12 = 2.0*(qy*qz + qx*qw)
        r20 = 2.0*(qx*qz + qy*qw)
        r21 = 2.0*(qy*qz - qx*qw)
        r22 = 1.0 - 2.0*(qx*qx + qy*qy)

        # inv(T*R) = inv(R) * inv(T) = R^T * (-p)
        tx = -(r00*px + r01*py + r02*pz)
        ty = -(r10*px + r11*py + r12*pz)
        tz = -(r20*px + r21*py + r22*pz)

        # Column-major
        ibm_list.append([
            r00, r10, r20, 0.0,
            r01, r11, r21, 0.0,
            r02, r12, r22, 0.0,
            tx,  ty,  tz,  1.0,
        ])
        joint_indices.append(anim_node_idx)

    if not ibm_list:
        return None

    # Pack as binary float32
    ibm_bytes = b""
    for ibm in ibm_list:
        ibm_bytes += _struct.pack("<16f", *ibm)

    return {
        "ibm_bytes": ibm_bytes,
        "n_joints": len(ibm_list),
        "joint_indices": joint_indices,
    }


def export_emfxanim_gltf(anim, clip_name, output_path, mesh_bind_rotations=None, mesh_bind_positions=None, mesh_nodes=None):
    """Export an FXMAnimation as a standalone glTF 2.0 animation file.

    The skeleton uses FXA bone names (matching the FXA mesh skeleton),
    so Three.js AnimationMixer can directly match tracks to mesh bones.

    Args:
        anim: FXMAnimation from parse_fxm / parse_emfxanim_export.
        clip_name: Name for the animation clip.
        output_path: Path to write the .gltf file.
        mesh_bind_rotations: Optional dict {bone_name: (qx,qy,qz,qw)} from
            the FXA mesh. Used for position offset computation.
        mesh_bind_positions: Optional dict {bone_name: (px,py,pz)} from
            the FXA mesh. Position keyframes are offset by
            (meshBindPos - animBindPos) to reconcile bind pose differences.
        mesh_nodes: Optional list of FXA mesh node objects (with .name,
            .parent_index, .rotation, .position, .scale). When provided,
            FXM-bind inverse bind matrices are computed and included as a
            skin in the glTF. This allows the viewer to swap IBMs so that
            non-animated bones rest at FXM bind pose (not mesh T-pose).

    Returns:
        The glTF dict that was written, or None if no animation data.
    """
    import json
    import base64
    import struct as _struct
    import array as _array

    if not anim.submotions:
        return None

    submotions = anim.submotions

    # Build skeleton nodes from ALL submotions (needed for IBM computation).
    # Node rest pose = FXM animation bind pose (when mesh_nodes provided).
    gltf_nodes = []
    bone_name_to_node = {}

    # Precompute per-bone position offsets: meshBindPos - animBindPos
    bone_pos_offsets = {}  # bone_name -> (dx, dy, dz)
    if mesh_bind_positions:
        for sm in anim.submotions:
            if sm.name in mesh_bind_positions:
                mx, my, mz = mesh_bind_positions[sm.name]
                ax, ay, az = sm.bind_pose_pos
                dx, dy, dz = mx - ax, my - ay, mz - az
                if abs(dx) > 0.001 or abs(dy) > 0.001 or abs(dz) > 0.001:
                    bone_pos_offsets[sm.name] = (dx, dy, dz)

    for si, sm in enumerate(anim.submotions):
        node = {"name": sm.name}
        if mesh_nodes:
            # Use FXM animation bind pose as node rest — matches the IBMs
            # we'll compute below, so non-animated bones are already at
            # their natural resting position without needing keyframe tracks.
            px, py, pz = sm.bind_pose_pos
            qx, qy, qz, qw = sm.bind_pose_rot
            sx, sy, sz = sm.bind_pose_scale
        else:
            # Legacy: mesh bind pose as rest
            if mesh_bind_positions and sm.name in mesh_bind_positions:
                px, py, pz = mesh_bind_positions[sm.name]
            else:
                px, py, pz = sm.bind_pose_pos
            if mesh_bind_rotations and sm.name in mesh_bind_rotations:
                qx, qy, qz, qw = mesh_bind_rotations[sm.name]
            else:
                qx, qy, qz, qw = sm.bind_pose_rot
            sx, sy, sz = sm.bind_pose_scale
        if px != 0.0 or py != 0.0 or pz != 0.0:
            node["translation"] = [px, py, pz]
        if qx != 0.0 or qy != 0.0 or qz != 0.0 or qw != 1.0:
            node["rotation"] = [qx, qy, qz, qw]
        if sx != 1.0 or sy != 1.0 or sz != 1.0:
            node["scale"] = [sx, sy, sz]
        gltf_nodes.append(node)
        bone_name_to_node[sm.name] = si

    # Rotation correction DISABLED — raw keyframes are in FXM local space,
    # which matches the FXM-bind IBMs.
    bone_corrections = {}

    # Animation buffer data
    buffer_parts = []
    buffer_views = []
    accessors = []
    anim_channels = []
    anim_samplers = []
    current_offset = 0

    def add_track(node_idx, path, time_values, value_values, value_type):
        nonlocal current_offset
        if not time_values:
            return

        time_arr = _array.array("f", time_values)
        value_arr = _array.array("f", value_values)
        time_bytes = time_arr.tobytes()
        value_bytes = value_arr.tobytes()
        time_min = float(min(time_arr))
        time_max = float(max(time_arr))

        bv_time = len(buffer_views)
        buffer_views.append({
            "buffer": 0,
            "byteOffset": current_offset,
            "byteLength": len(time_bytes),
        })
        buffer_parts.append(time_bytes)
        current_offset += len(time_bytes)
        acc_time = len(accessors)
        accessors.append({
            "bufferView": bv_time,
            "componentType": 5126,
            "count": len(time_values),
            "type": "SCALAR",
            "min": [time_min],
            "max": [time_max],
        })

        bv_value = len(buffer_views)
        buffer_views.append({
            "buffer": 0,
            "byteOffset": current_offset,
            "byteLength": len(value_bytes),
        })
        buffer_parts.append(value_bytes)
        current_offset += len(value_bytes)
        acc_value = len(accessors)
        accessors.append({
            "bufferView": bv_value,
            "componentType": 5126,
            "count": len(time_values),
            "type": value_type,
        })

        s_idx = len(anim_samplers)
        anim_samplers.append({
            "input": acc_time,
            "output": acc_value,
            "interpolation": "LINEAR",
        })
        anim_channels.append({"sampler": s_idx, "target": {"node": node_idx, "path": path}})

    def static_times():
        if anim.duration > 0.0:
            return [0.0, float(anim.duration)]
        return [0.0]

    for sm in submotions:
        node_idx = bone_name_to_node.get(sm.name)
        if node_idx is None:
            continue

        # Rotation keys
        if sm.rot_keys:
            correction = bone_corrections.get(sm.name)

            time_values = [k[0] for k in sm.rot_keys]
            rot_values = []
            prev = None
            for _, qx, qy, qz, qw in sm.rot_keys:
                if correction:
                    qx, qy, qz, qw = _quat_mul(correction, (qx, qy, qz, qw))
                # Hemisphere fix: ensure consecutive quaternions have dot > 0
                # so linear interpolation takes the shortest path.
                if prev is not None:
                    dot = prev[0]*qx + prev[1]*qy + prev[2]*qz + prev[3]*qw
                    if dot < 0:
                        qx, qy, qz, qw = -qx, -qy, -qz, -qw
                prev = (qx, qy, qz, qw)
                rot_values.extend([qx, qy, qz, qw])
            add_track(node_idx, "rotation", time_values, rot_values, "VEC4")
        else:
            qx, qy, qz, qw = sm.bind_pose_rot
            time_values = static_times()
            rot_values = []
            for _ in time_values:
                rot_values.extend([qx, qy, qz, qw])
            add_track(node_idx, "rotation", time_values, rot_values, "VEC4")

        # Position keys
        if sm.pos_keys:
            pos_offset = bone_pos_offsets.get(sm.name)

            # Root motion extraction (O3DE style): for root bones like
            # body_root and ground, strip horizontal (X/Z) movement and
            # keep only vertical (Y) bounce.  The horizontal component
            # would be applied to the entity's world transform at runtime
            # by the movement system; in our viewer it just causes the
            # character to drift across the grid.
            # We detect root motion bones as those whose bind position is
            # near the skeleton root (parent is skeleton/ground).
            is_root_motion_bone = sm.name in ("body_root", "ground")

            time_values = [k[0] for k in sm.pos_keys]
            pos_values = []

            if is_root_motion_bone and len(sm.pos_keys) > 1:
                # Use first key as reference; strip X/Z delta, keep Y
                ref_x, ref_y, ref_z = sm.pos_keys[0][1], sm.pos_keys[0][2], sm.pos_keys[0][3]
                for _, x, y, z in sm.pos_keys:
                    # Keep Y as-is (vertical bounce), pin X/Z to first key
                    x = ref_x
                    z = ref_z
                    if pos_offset:
                        x += pos_offset[0]
                        y += pos_offset[1]
                        z += pos_offset[2]
                    pos_values.extend([x, y, z])
            else:
                for _, x, y, z in sm.pos_keys:
                    if pos_offset:
                        x += pos_offset[0]
                        y += pos_offset[1]
                        z += pos_offset[2]
                    pos_values.extend([x, y, z])
            add_track(node_idx, "translation", time_values, pos_values, "VEC3")
        else:
            x, y, z = sm.bind_pose_pos
            pos_offset = bone_pos_offsets.get(sm.name)
            if pos_offset:
                x += pos_offset[0]
                y += pos_offset[1]
                z += pos_offset[2]
            time_values = static_times()
            pos_values = []
            for _ in time_values:
                pos_values.extend([x, y, z])
            add_track(node_idx, "translation", time_values, pos_values, "VEC3")

        sx, sy, sz = sm.bind_pose_scale
        if sx != 1.0 or sy != 1.0 or sz != 1.0:
            time_values = static_times()
            scale_values = []
            for _ in time_values:
                scale_values.extend([sx, sy, sz])
            add_track(node_idx, "scale", time_values, scale_values, "VEC3")

    if not anim_channels:
        return None

    # Compute FXM-bind inverse bind matrices when mesh_nodes provided.
    # This lets the viewer swap the mesh's IBMs so non-animated bones
    # rest at FXM bind pose instead of mesh T-pose.
    skin_data = None
    if mesh_nodes:
        skin_data = _compute_fxm_bind_ibms(anim, mesh_nodes, bone_name_to_node)

    buffer_bytes = b"".join(buffer_parts)

    # If we have IBM data, append it to the buffer
    ibm_accessor_idx = None
    if skin_data:
        ibm_bytes = skin_data["ibm_bytes"]
        ibm_offset = len(buffer_bytes)
        buffer_bytes += ibm_bytes

        bv_ibm = len(buffer_views)
        buffer_views.append({
            "buffer": 0,
            "byteOffset": ibm_offset,
            "byteLength": len(ibm_bytes),
        })
        ibm_accessor_idx = len(accessors)
        n_joints = skin_data["n_joints"]
        accessors.append({
            "bufferView": bv_ibm,
            "componentType": 5126,
            "count": n_joints,
            "type": "MAT4",
        })

    buffer_uri = "data:application/octet-stream;base64," + base64.b64encode(buffer_bytes).decode("ascii")

    # Scene: all bone nodes as roots (flat skeleton — mesh provides hierarchy)
    scene_nodes = list(range(len(gltf_nodes)))

    gltf = {
        "asset": {"version": "2.0", "generator": "vanguard_emfxanim"},
        "scene": 0,
        "scenes": [{"nodes": scene_nodes}],
        "nodes": gltf_nodes,
        "animations": [{
            "name": clip_name,
            "channels": anim_channels,
            "samplers": anim_samplers,
        }],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"uri": buffer_uri, "byteLength": len(buffer_bytes)}],
    }

    # Include skin with FXM-bind IBMs so viewer can swap them
    if skin_data and ibm_accessor_idx is not None:
        gltf["skins"] = [{
            "inverseBindMatrices": ibm_accessor_idx,
            "joints": skin_data["joint_indices"],
            "name": "fxm_bind_skin",
        }]

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(gltf, f)

    return gltf
