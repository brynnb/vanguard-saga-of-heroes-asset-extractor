"""
UE2 SkeletalMesh LODModel parser + glTF exporter for Vanguard .ukx files.
Vanguard: FSkelMeshSection=18B, FVertInfluence=8B, sentinel in SkinningData.
"""
import struct, json, base64, math
import array as _array

def _ri32(d, p): return struct.unpack_from('<i', d, p)[0], p + 4
def _rflt(d, p): return struct.unpack_from('<f', d, p)[0], p + 4
def _rci(data, pos):
    b0 = data[pos]; pos += 1
    neg = b0 & 0x80; more = b0 & 0x40; val = b0 & 0x3F
    if more:
        b1 = data[pos]; pos += 1; val |= (b1 & 0x7F) << 6
        if b1 & 0x80:
            b2 = data[pos]; pos += 1; val |= (b2 & 0x7F) << 13
            if b2 & 0x80:
                b3 = data[pos]; pos += 1; val |= (b3 & 0x7F) << 20
                if b3 & 0x80:
                    b4 = data[pos]; pos += 1; val |= (b4 & 0x3F) << 27
    return -val if neg else val, pos

def _make_rc(v): return _ri32 if v >= 128 else _rci

def _read_ta(data, pos, es, rc, d=""):
    c, pos = rc(data, pos)
    if c < 0 or c > 2000000: raise ValueError(f"TArray({d}): bad count {c}")
    end = pos + c * es
    if end > len(data): raise ValueError(f"TArray({d}): overflow")
    return c, pos, end

def _read_rib(data, pos, rc, d=""):
    c, s, e = _read_ta(data, pos, 2, rc, d)
    rev, pos = _ri32(data, e)
    return c, s, pos

def _read_tla(data, pos, es, so, rc, d=""):
    skip, pos = _ri32(data, pos)
    c, pos = rc(data, pos)
    if c < 0 or c > 2000000: raise ValueError(f"TLazy({d}): bad {c}")
    end = pos + c * es
    if end > len(data): raise ValueError(f"TLazy({d}): overflow")
    return c, pos, end

def _find_lodmodels(data, use_int32):
    sentinel = b'\xED\xFE\xFF\xEF\xEF\xBE\xAD\xDE'
    idx = data.find(sentinel)
    if idx < 0: return None, None
    if use_int32:
        if idx < 8: return None, None
        sd, _ = _ri32(data, idx - 4); lc, _ = _ri32(data, idx - 8)
        return (idx - 8, lc) if sd >= 2 and sd < 500000 and 1 <= lc <= 10 else (None, None)
    for sl in range(1, 5):
        ss = idx - sl
        if ss < 1: continue
        sd, se = _rci(data, ss)
        if se != idx or sd < 2 or sd > 500000: continue
        for ll in range(1, 5):
            ls = ss - ll
            if ls < 0: continue
            lc, le = _rci(data, ls)
            if le == ss and 1 <= lc <= 10: return ls, lc
    return None, None

def _parse_lod(data, pos, so, rc):
    sd_c, _, pos = _read_ta(data, pos, 4, rc, "SD")
    sp_c, _, pos = _read_ta(data, pos, 16, rc, "SP")
    nsw, pos = _ri32(data, pos)
    ss_c, ss_s, pos = _read_ta(data, pos, 18, rc, "SS")
    sections = []
    for i in range(ss_c):
        u = struct.unpack_from('<9H', data, ss_s + i * 18)
        sections.append({'mat_idx': u[0], 'first_face': u[8]})
    rs_c, _, pos = _read_ta(data, pos, 18, rc, "RS")
    si_c, si_s, pos = _read_rib(data, pos, rc, "SI")
    ri_c, ri_s, pos = _read_rib(data, pos, rc, "RI")
    vs_r, pos = _ri32(data, pos); vs_b, pos = _ri32(data, pos); vs_s, pos = _ri32(data, pos)
    vs_c, _, pos = _read_ta(data, pos, 32, rc, "VS")
    vi_c, vi_s, pos = _read_tla(data, pos, 8, so, rc, "VI")
    w_c, w_s, pos = _read_tla(data, pos, 10, so, rc, "W")
    f_c, f_s, pos = _read_tla(data, pos, 8, so, rc, "F")
    p_c, p_s, pos = _read_tla(data, pos, 12, so, rc, "P")
    ldf, pos = _rflt(data, pos); lh, pos = _rflt(data, pos)
    for _ in range(4): _, pos = _ri32(data, pos)
    unk_c, _, pos = _read_ta(data, pos, 4, rc, "unkC")
    points = [struct.unpack_from('<3f', data, p_s + i*12) for i in range(p_c)]
    wedges = []
    for i in range(w_c):
        o = w_s + i * 10
        iv = struct.unpack_from('<H', data, o)[0]
        u, v = struct.unpack_from('<2f', data, o + 2)
        wedges.append((iv, u, v))
    faces = [struct.unpack_from('<4H', data, f_s + i*8) for i in range(f_c)]
    influences = []
    for i in range(vi_c):
        o = vi_s + i * 8
        w = struct.unpack_from('<f', data, o)[0]
        vi = struct.unpack_from('<H', data, o+4)[0]
        bi = struct.unpack_from('<H', data, o+6)[0]
        influences.append((w, vi, bi))
    return {'points': points, 'wedges': wedges, 'faces': faces,
            'influences': influences, 'sections': sections, 'nsw': nsw}, pos


def extract_lod0_geometry(export_data, serial_offset, pkg_version):
    """Extract LOD0 geometry from a SkeletalMesh export. Returns dict or None."""
    use_int32 = pkg_version >= 128
    rc = _make_rc(pkg_version)
    start, lod_count = _find_lodmodels(export_data, use_int32)
    if start is None:
        return None
    pos = start
    _, pos = rc(export_data, pos)
    geom, _ = _parse_lod(export_data, pos, serial_offset, rc)
    return geom


def _invert4x4(m):
    inv = [0]*16
    inv[0] = m[5]*(m[10]*m[15]-m[11]*m[14]) - m[9]*(m[6]*m[15]-m[7]*m[14]) + m[13]*(m[6]*m[11]-m[7]*m[10])
    inv[4] = -(m[4]*(m[10]*m[15]-m[11]*m[14]) - m[8]*(m[6]*m[15]-m[7]*m[14]) + m[12]*(m[6]*m[11]-m[7]*m[10]))
    inv[8] = m[4]*(m[9]*m[15]-m[11]*m[13]) - m[8]*(m[5]*m[15]-m[7]*m[13]) + m[12]*(m[5]*m[11]-m[7]*m[9])
    inv[12] = -(m[4]*(m[9]*m[14]-m[10]*m[13]) - m[8]*(m[5]*m[14]-m[6]*m[13]) + m[12]*(m[5]*m[10]-m[6]*m[9]))
    inv[1] = -(m[1]*(m[10]*m[15]-m[11]*m[14]) - m[9]*(m[2]*m[15]-m[3]*m[14]) + m[13]*(m[2]*m[11]-m[3]*m[10]))
    inv[5] = m[0]*(m[10]*m[15]-m[11]*m[14]) - m[8]*(m[2]*m[15]-m[3]*m[14]) + m[12]*(m[2]*m[11]-m[3]*m[10])
    inv[9] = -(m[0]*(m[9]*m[15]-m[11]*m[13]) - m[8]*(m[1]*m[15]-m[3]*m[13]) + m[12]*(m[1]*m[11]-m[3]*m[9]))
    inv[13] = m[0]*(m[9]*m[14]-m[10]*m[13]) - m[8]*(m[1]*m[14]-m[2]*m[13]) + m[12]*(m[1]*m[10]-m[2]*m[9])
    inv[2] = m[1]*(m[6]*m[15]-m[7]*m[14]) - m[5]*(m[2]*m[15]-m[3]*m[14]) + m[13]*(m[2]*m[7]-m[3]*m[6])
    inv[6] = -(m[0]*(m[6]*m[15]-m[7]*m[14]) - m[4]*(m[2]*m[15]-m[3]*m[14]) + m[12]*(m[2]*m[7]-m[3]*m[6]))
    inv[10] = m[0]*(m[5]*m[15]-m[7]*m[13]) - m[4]*(m[1]*m[15]-m[3]*m[13]) + m[12]*(m[1]*m[7]-m[3]*m[5])
    inv[14] = -(m[0]*(m[5]*m[14]-m[6]*m[13]) - m[4]*(m[1]*m[14]-m[2]*m[13]) + m[12]*(m[1]*m[6]-m[2]*m[5]))
    inv[3] = -(m[1]*(m[6]*m[11]-m[7]*m[10]) - m[5]*(m[2]*m[11]-m[3]*m[10]) + m[9]*(m[2]*m[7]-m[3]*m[6]))
    inv[7] = m[0]*(m[6]*m[11]-m[7]*m[10]) - m[4]*(m[2]*m[11]-m[3]*m[10]) + m[8]*(m[2]*m[7]-m[3]*m[6])
    inv[11] = -(m[0]*(m[5]*m[11]-m[7]*m[9]) - m[4]*(m[1]*m[11]-m[3]*m[9]) + m[8]*(m[1]*m[7]-m[3]*m[5]))
    inv[15] = m[0]*(m[5]*m[10]-m[6]*m[9]) - m[4]*(m[1]*m[10]-m[2]*m[9]) + m[8]*(m[1]*m[6]-m[2]*m[5])
    det = m[0]*inv[0] + m[1]*inv[4] + m[2]*inv[8] + m[3]*inv[12]
    if abs(det) < 1e-10: det = 1.0
    return [x/det for x in inv]


def export_skelmesh_gltf(geom, bind_bones, output_path):
    """Export mesh + skeleton + skinning as glTF 2.0.
    Returns glTF dict, or None if geometry is empty (TLazy-only meshes)."""
    points = geom['points']
    wedges = geom['wedges']
    faces = geom['faces']
    influences = geom['influences']
    nw = len(wedges)
    nf = len(faces)
    nb = len(bind_bones)

    if nw == 0 or nf == 0:
        return None

    pos_a = _array.array('f')
    uv_a = _array.array('f')
    for iv, u, v in wedges:
        x, y, z = points[iv] if iv < len(points) else (0,0,0)
        pos_a.extend([x, z, -y])
        uv_a.extend([u, 1.0 - v])

    idx_a = _array.array('H' if nw < 65536 else 'I')
    for w0, w1, w2, mat in faces:
        idx_a.extend([w0, w1, w2])

    pt_infl = {}
    for w, vi, bi in influences:
        pt_infl.setdefault(vi, []).append((w, bi))

    jt_a = _array.array('H')
    wt_a = _array.array('f')
    for iv, u, v in wedges:
        infl = pt_infl.get(iv, [(1.0, 0)])
        infl = sorted(infl, key=lambda x: -x[0])[:4]
        while len(infl) < 4:
            infl.append((0.0, 0))
        total = sum(w for w, _ in infl)
        if total > 0:
            infl = [(w/total, b) for w, b in infl]
        for w, b in infl:
            jt_a.append(b)
            wt_a.append(w)

    ch_map = {}
    for bi, bone in enumerate(bind_bones):
        pi = bone['parent_index']
        if pi != bi and pi >= 0:
            ch_map.setdefault(pi, []).append(bi)

    wrapper = {"name": "ue2_to_gltf", "rotation": [1.0, 0.0, 0.0, 0.0]}
    nodes = [wrapper]
    bo = 1
    roots = []
    ji = list(range(bo, bo + nb))

    for bi, bone in enumerate(bind_bones):
        nd = {"name": bone['name']}
        qx, qy, qz, qw = bone.get('quat', (0,0,0,1))
        nd["rotation"] = [-qx, -qy, -qz, qw]
        px, py, pz = bone.get('pos', (0,0,0))
        nd["translation"] = [px, py, pz]
        kids = ch_map.get(bi, [])
        if kids:
            nd["children"] = [k + bo for k in kids]
        nodes.append(nd)
        pi = bone['parent_index']
        if pi == bi or pi < 0:
            roots.append(bi + bo)

    wrapper["children"] = list(roots)

    wts = []
    for bi, bone in enumerate(bind_bones):
        qx, qy, qz, qw = bone.get('quat', (0,0,0,1))
        px, py, pz = bone.get('pos', (0,0,0))
        qx, qy, qz = -qx, -qy, -qz
        xx, yy, zz = qx*qx, qy*qy, qz*qz
        xy, xz, yz = qx*qy, qx*qz, qy*qz
        wx, wy, wz = qw*qx, qw*qy, qw*qz
        m = [1-2*(yy+zz), 2*(xy+wz), 2*(xz-wy), 0,
             2*(xy-wz), 1-2*(xx+zz), 2*(yz+wx), 0,
             2*(xz+wy), 2*(yz-wx), 1-2*(xx+yy), 0,
             px, py, pz, 1]
        pi = bone['parent_index']
        if pi >= 0 and pi != bi and pi < len(wts):
            pm = wts[pi]
            r = [0]*16
            for row in range(4):
                for col in range(4):
                    s = 0
                    for k in range(4):
                        s += m[row*4+k] * pm[k*4+col]
                    r[row*4+col] = s
            m = r
        wts.append(m)

    ibm_a = _array.array('f')
    for wt in wts:
        ibm_a.extend(_invert4x4(wt))

    parts = []; bvs = []; accs = []; off = 0
    def _add(a, tgt=None, stride=None):
        nonlocal off
        b = a.tobytes(); parts.append(b)
        bv = {"buffer":0,"byteOffset":off,"byteLength":len(b)}
        if tgt: bv["target"] = tgt
        if stride: bv["byteStride"] = stride
        bvs.append(bv); off += len(b)
        return len(bvs) - 1

    bv0 = _add(pos_a, 34962)
    mn = [min(pos_a[i::3]) for i in range(3)]
    mx = [max(pos_a[i::3]) for i in range(3)]
    accs.append({"bufferView":bv0,"componentType":5126,"count":nw,"type":"VEC3","min":mn,"max":mx})

    bv1 = _add(uv_a, 34962)
    accs.append({"bufferView":bv1,"componentType":5126,"count":nw,"type":"VEC2"})

    bv2 = _add(idx_a, 34963)
    ct = 5123 if nw < 65536 else 5125
    accs.append({"bufferView":bv2,"componentType":ct,"count":nf*3,"type":"SCALAR"})

    bv3 = _add(jt_a, 34962, 8)
    accs.append({"bufferView":bv3,"componentType":5123,"count":nw,"type":"VEC4"})

    bv4 = _add(wt_a, 34962)
    accs.append({"bufferView":bv4,"componentType":5126,"count":nw,"type":"VEC4"})

    bv5 = _add(ibm_a)
    accs.append({"bufferView":bv5,"componentType":5126,"count":nb,"type":"MAT4"})

    buf = b"".join(parts)
    uri = "data:application/octet-stream;base64," + base64.b64encode(buf).decode()

    mi = len(nodes)
    nodes.append({"name": "mesh", "mesh": 0, "skin": 0})
    wrapper["children"].append(mi)

    gltf = {
        "asset": {"version": "2.0", "generator": "vanguard_skelmesh"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": [{"primitives": [{"attributes": {
            "POSITION": 0, "TEXCOORD_0": 1, "JOINTS_0": 3, "WEIGHTS_0": 4
        }, "indices": 2}]}],
        "skins": [{"inverseBindMatrices": 5, "joints": ji,
                    "skeleton": roots[0] if roots else bo}],
        "accessors": accs,
        "bufferViews": bvs,
        "buffers": [{"uri": uri, "byteLength": len(buf)}],
    }
    with open(output_path, 'w') as f:
        json.dump(gltf, f)
    return gltf
