"""
SGO (Saga-of-Heroes binaryprefabs.sgo) package parser.

A single SGO file is a concatenation of ~19,570 UE2 mini-packages plus a
prefab-directory trailer. This library parses one mini-package or the
entire file into fully typed Python dicts.

Design principles:
* Every byte of serial data is consumed (parsing stops only at
  serial_size exhaustion).
* Every property is either decoded into a typed Python value OR stored
  with a ``raw_hex`` fallback; no data is silently discarded.
* Multiple None-terminated property blocks per export are supported.
* All types observed in binaryprefabs.sgo are decoded: bool, byte, int,
  float, object-ref, name-ref, ansi-string, class-ref, array, struct
  (Vector, Rotator, Color, Plane, Quat, Range, RangeVector, Scale,
  PointRegion), wide-string.

"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any


SGO_MAGIC = b"SGOA"
SGO_VERSION = 2
PKG_SIG = 0x9E2A83C1
PKG_SIG_BYTES = struct.pack("<I", PKG_SIG)

# EPropType — UE2 FPropertyTag property type codes
PT_BYTE = 1
PT_INT = 2
PT_BOOL = 3
PT_FLOAT = 4
PT_OBJECT = 5
PT_NAME = 6
PT_STRING = 7       # ansi length-prefixed
PT_CLASS = 8
PT_ARRAY = 9
PT_STRUCT = 10
PT_VECTOR = 11
PT_ROTATOR = 12
PT_STR = 13         # wide (UTF-16LE) length-prefixed
PT_MAP = 14
PT_FIXED_ARRAY = 15

PTYPE_NAMES = {
    PT_BYTE: "Byte", PT_INT: "Int", PT_BOOL: "Bool", PT_FLOAT: "Float",
    PT_OBJECT: "Object", PT_NAME: "Name", PT_STRING: "Str",
    PT_CLASS: "Class", PT_ARRAY: "Array", PT_STRUCT: "Struct",
    PT_VECTOR: "Vector", PT_ROTATOR: "Rotator", PT_STR: "StrW",
    PT_MAP: "Map", PT_FIXED_ARRAY: "FixedArray",
}


# --------------------------------------------------------------------------
# Low-level readers
# --------------------------------------------------------------------------

def read_ci(buf: bytes, pos: int) -> tuple[int, int]:
    """FCompactIndex: 1-5 byte variable-length signed integer."""
    if pos >= len(buf):
        return 0, pos
    b0 = buf[pos]; pos += 1
    neg = b0 & 0x80
    val = b0 & 0x3F
    if b0 & 0x40:
        if pos >= len(buf):
            return (-val if neg else val), pos
        b1 = buf[pos]; pos += 1
        val |= (b1 & 0x7F) << 6
        if b1 & 0x80:
            if pos >= len(buf):
                return (-val if neg else val), pos
            b2 = buf[pos]; pos += 1
            val |= (b2 & 0x7F) << 13
            if b2 & 0x80:
                if pos >= len(buf):
                    return (-val if neg else val), pos
                b3 = buf[pos]; pos += 1
                val |= (b3 & 0x7F) << 20
                if b3 & 0x80:
                    if pos >= len(buf):
                        return (-val if neg else val), pos
                    b4 = buf[pos]; pos += 1
                    val |= b4 << 27
    return (-val if neg else val), pos


def read_fstr(buf: bytes, pos: int) -> tuple[str, int]:
    """FString: CI length; negative = UTF-16, positive = ANSI. Includes NUL."""
    length, pos = read_ci(buf, pos)
    if length < 0:
        length = -length
        s = buf[pos:pos + length * 2].decode("utf-16-le", errors="replace").rstrip("\x00")
        pos += length * 2
    elif length > 0:
        s = buf[pos:pos + length].decode("latin-1", errors="replace").rstrip("\x00")
        pos += length
    else:
        s = ""
    return s, pos


# --------------------------------------------------------------------------
# Package header
# --------------------------------------------------------------------------

@dataclass
class PackageHeader:
    signature: int
    version: int
    licensee: int
    name_count: int
    name_offset: int
    export_count: int
    export_offset: int
    import_count: int
    import_offset: int
    guid: bytes           # 16 bytes
    generations: list[tuple[int, int]]  # [(export_count, name_count), ...]


def parse_package_header(buf: bytes, pos: int = 0) -> tuple[PackageHeader, int]:
    """Parse fixed 36-byte mini-package header + FGuid + FGenerationInfo[]."""
    sig, ver, lic, nc, no, ec, eo, ic, io_off = struct.unpack_from("<IIIIIIIII", buf, pos)
    if sig != PKG_SIG:
        raise ValueError(f"Bad package signature 0x{sig:08x} at {pos}")
    pos += 36
    guid = bytes(buf[pos:pos + 16]); pos += 16
    gen_count = struct.unpack_from("<i", buf, pos)[0]; pos += 4
    gens: list[tuple[int, int]] = []
    for _ in range(gen_count):
        ec2, nc2 = struct.unpack_from("<ii", buf, pos)
        gens.append((ec2, nc2))
        pos += 8
    return PackageHeader(sig, ver, lic, nc, no, ec, eo, ic, io_off, guid, gens), pos


# --------------------------------------------------------------------------
# Name / import / export tables
# --------------------------------------------------------------------------

@dataclass
class ImportEntry:
    class_package: str
    class_name: str
    package_idx: int
    object_name: str


@dataclass
class ExportEntry:
    class_idx: int        # negative = import, positive = local export, 0 = class=None
    super_idx: int
    package_idx: int      # raw u32 just after super
    object_name_idx: int
    flags: int            # 4 bytes after object name
    serial_size: int
    serial_offset: int
    class_ref: str        # resolved class name (import or export lookup)
    object_name: str


def parse_name_table(buf: bytes, header: PackageHeader) -> list[str]:
    pos = header.name_offset
    names: list[str] = []
    for _ in range(header.name_count):
        s, pos = read_fstr(buf, pos)
        pos += 4  # flags u32
        names.append(s)
    return names


def parse_import_table(buf: bytes, header: PackageHeader, names: list[str]) -> list[ImportEntry]:
    pos = header.import_offset
    imports: list[ImportEntry] = []
    for _ in range(header.import_count):
        cp, pos = read_ci(buf, pos)
        cn, pos = read_ci(buf, pos)
        pkg_idx = struct.unpack_from("<i", buf, pos)[0]; pos += 4
        on, pos = read_ci(buf, pos)
        imports.append(ImportEntry(
            class_package=names[cp] if 0 <= cp < len(names) else "",
            class_name=names[cn] if 0 <= cn < len(names) else "",
            package_idx=pkg_idx,
            object_name=names[on] if 0 <= on < len(names) else "",
        ))
    return imports


def parse_export_table(buf: bytes, header: PackageHeader,
                       names: list[str], imports: list[ImportEntry]) -> list[ExportEntry]:
    pos = header.export_offset
    exports: list[ExportEntry] = []
    for _ in range(header.export_count):
        ci2, pos = read_ci(buf, pos)
        si, pos = read_ci(buf, pos)
        pkg_idx = struct.unpack_from("<i", buf, pos)[0]; pos += 4
        oni, pos = read_ci(buf, pos)
        flags = struct.unpack_from("<i", buf, pos)[0]; pos += 4
        ss, pos = read_ci(buf, pos)
        so = 0
        if ss > 0:
            so, pos = read_ci(buf, pos)
        cls_ref = ""
        if ci2 < 0:
            ii = -ci2 - 1
            cls_ref = imports[ii].object_name if 0 <= ii < len(imports) else ""
        elif ci2 > 0:
            # forward/local export reference (rare in SGO)
            cls_ref = f"export_{ci2}"
        exports.append(ExportEntry(
            class_idx=ci2, super_idx=si, package_idx=pkg_idx,
            object_name_idx=oni, flags=flags,
            serial_size=ss, serial_offset=so,
            class_ref=cls_ref,
            object_name=names[oni] if 0 <= oni < len(names) else "",
        ))
    return exports


# --------------------------------------------------------------------------
# Object-reference resolver
# --------------------------------------------------------------------------

def resolve_object_ref(ci_val: int, imports: list[ImportEntry],
                       exports: list[ExportEntry] | None = None) -> str | None:
    """Resolve a CI object reference to a name (import or export)."""
    if ci_val == 0:
        return None
    if ci_val < 0:
        i = -ci_val - 1
        return imports[i].object_name if 0 <= i < len(imports) else None
    # positive: local export reference (1-based)
    if exports is None:
        return f"export_{ci_val}"
    i = ci_val - 1
    return exports[i].object_name if 0 <= i < len(exports) else None


def _outer_chain(package_idx: int, imports: list[ImportEntry],
                 exports: list[ExportEntry] | None = None) -> list[str]:
    """Return outer package/object names from root to leaf."""
    chain: list[str] = []
    seen: set[int] = set()
    idx = package_idx
    while idx != 0 and idx not in seen:
        seen.add(idx)
        if idx < 0:
            import_index = -idx - 1
            if not (0 <= import_index < len(imports)):
                break
            outer = imports[import_index]
            if outer.object_name:
                chain.append(outer.object_name)
            idx = outer.package_idx
            continue
        if exports is None:
            break
        export_index = idx - 1
        if not (0 <= export_index < len(exports)):
            break
        outer_export = exports[export_index]
        if outer_export.object_name:
            chain.append(outer_export.object_name)
        idx = outer_export.package_idx
    chain.reverse()
    return chain


def resolve_object_ref_detail(ci_val: int, imports: list[ImportEntry],
                              exports: list[ExportEntry] | None = None
                              ) -> dict[str, Any] | None:
    """Resolve a CI object reference, preserving package-qualified context."""
    if ci_val == 0:
        return None
    if ci_val < 0:
        import_index = -ci_val - 1
        if not (0 <= import_index < len(imports)):
            return {"ci": ci_val, "kind": "unresolved"}
        imp = imports[import_index]
        package_chain = _outer_chain(imp.package_idx, imports, exports)
        object_parts = [*package_chain, imp.object_name]
        object_path = ".".join(part for part in object_parts if part)
        package_path = ".".join(package_chain)
        return {
            "ci": ci_val,
            "kind": "import",
            "import_index": import_index,
            "class_package": imp.class_package,
            "class_name": imp.class_name,
            "name": imp.object_name,
            "package_chain": package_chain,
            "package_path": package_path,
            "source_package": package_chain[0] if package_chain else "",
            "object_path": object_path,
        }

    export_index = ci_val - 1
    if exports is None or not (0 <= export_index < len(exports)):
        return {
            "ci": ci_val,
            "kind": "export",
            "export_index": export_index,
            "name": f"export_{ci_val}",
            "object_path": f"export_{ci_val}",
        }
    exp = exports[export_index]
    package_chain = _outer_chain(exp.package_idx, imports, exports)
    object_parts = [*package_chain, exp.object_name]
    object_path = ".".join(part for part in object_parts if part)
    package_path = ".".join(package_chain)
    return {
        "ci": ci_val,
        "kind": "export",
        "export_index": export_index,
        "class_name": exp.class_ref,
        "name": exp.object_name,
        "package_chain": package_chain,
        "package_path": package_path,
        "source_package": package_chain[0] if package_chain else "",
        "object_path": object_path,
    }


# --------------------------------------------------------------------------
# Struct decoders
# --------------------------------------------------------------------------

def _range_value(min_value: float, max_value: float) -> dict[str, float]:
    """Return range values with both clear and legacy-compatible key names."""
    return {
        "min": min_value,
        "max": max_value,
        "a": min_value,
        "b": max_value,
    }


def _range_from_props(props: dict[str, Any]) -> dict[str, float] | None:
    min_value = props.get("Min", props.get("min", None))
    max_value = props.get("Max", props.get("max", None))
    if isinstance(min_value, (int, float)) and isinstance(max_value, (int, float)):
        return _range_value(float(min_value), float(max_value))
    return None


def _decode_struct(struct_name: str, data: bytes,
                   names: list[str], imports: list[ImportEntry],
                   exports: list[ExportEntry] | None = None,
                   depth: int = 0) -> Any:
    n = len(data)
    if struct_name == "Vector" and n >= 12:
        return {"x": struct.unpack_from("<f", data, 0)[0],
                "y": struct.unpack_from("<f", data, 4)[0],
                "z": struct.unpack_from("<f", data, 8)[0]}
    if struct_name == "Rotator" and n >= 12:
        p, y, r = struct.unpack_from("<iii", data, 0)
        return {"pitch": p, "yaw": y, "roll": r}
    if struct_name == "Color" and n >= 4:
        # Vanguard Core.u links Object.Color fields in B, G, R, A order.
        return {"R": data[2], "G": data[1], "B": data[0], "A": data[3]}
    if struct_name == "Plane" and n >= 16:
        x, y, z, w = struct.unpack_from("<ffff", data, 0)
        return {"x": x, "y": y, "z": z, "w": w}
    if struct_name == "Quat" and n >= 16:
        x, y, z, w = struct.unpack_from("<ffff", data, 0)
        return {"x": x, "y": y, "z": z, "w": w}
    if struct_name == "Range":
        props = _decode_tagged_struct_props(data, names, imports, exports, depth + 1)
        if props:
            range_value = _range_from_props(props)
            if range_value is not None:
                return range_value
        if n >= 8:
            a, b = struct.unpack_from("<ff", data, 0)
            return _range_value(a, b)
    if struct_name == "RangeVector":
        props = _decode_tagged_struct_props(data, names, imports, exports, depth + 1)
        if props:
            out: dict[str, Any] = {}
            for axis in ("X", "Y", "Z"):
                value = props.get(axis)
                if isinstance(value, dict):
                    axis_range = _range_from_props(value)
                    out[axis.lower()] = axis_range if axis_range is not None else value
            if all(axis in out for axis in ("x", "y", "z")):
                return out
        if n >= 24:
            xa, xb, ya, yb, za, zb = struct.unpack_from("<ffffff", data, 0)
            return {"x": _range_value(xa, xb),
                    "y": _range_value(ya, yb),
                    "z": _range_value(za, zb)}
    if struct_name == "Scale" and n >= 17:
        sx, sy, sz, sheer = struct.unpack_from("<ffff", data, 0)
        axis = data[16]
        return {"scale": {"x": sx, "y": sy, "z": sz},
                "sheer_rate": sheer, "sheer_axis": axis}
    if struct_name == "PointRegion":
        # {ObjectRef Zone; int32 iLeaf; byte ZoneNumber}
        # iLeaf may be negative sentinel (0xffffffff = -1) — leaf index
        try:
            zone_ci, cur = read_ci(data, 0)
            if cur + 5 > n:
                raise ValueError
            leaf = struct.unpack_from("<i", data, cur)[0]
            zone_num = data[cur + 4]
            zone_ref = resolve_object_ref(zone_ci, imports)
            return {"zone": zone_ref, "leaf": leaf, "zone_number": zone_num}
        except Exception:
            return {"raw_hex": data.hex()}
    # Unknown struct — fall back to raw hex.
    return {"raw_hex": data.hex()}


# --------------------------------------------------------------------------
# Array decoder
# --------------------------------------------------------------------------

def _props_to_dict(props: list["PropValue"]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    extras: dict[str, list[Any]] = {}
    for prop in props:
        if prop.name not in out:
            out[prop.name] = prop.value
            if prop.object_ref is not None:
                out[f"{prop.name}__object_ref"] = prop.object_ref
            continue
        extras.setdefault(prop.name, []).append(prop.value)
    for key, values in extras.items():
        out[f"{key}__extra"] = values
    return out


def _decode_tagged_struct_props(
    data: bytes,
    names: list[str],
    imports: list[ImportEntry],
    exports: list[ExportEntry] | None = None,
    depth: int = 0,
) -> dict[str, Any] | None:
    if depth > 6 or not data:
        return None
    try:
        props, consumed = parse_properties(
            data, 0, len(data), names, imports, exports, depth=depth
        )
    except Exception:
        return None
    if not props or consumed != len(data):
        return None
    return _props_to_dict(props)


def _decode_array(data: bytes, names: list[str], imports: list[ImportEntry],
                  exports: list[ExportEntry] | None = None,
                  depth: int = 0) -> dict:
    """Decode an Array property payload as {count, element_size, raw_hex}.

    Without class reflection we cannot know the element type. We store:
      * count     — the CI-encoded element count
      * elem_size — (total_bytes - count_bytes) / count if evenly divisible
      * raw_hex   — the full payload so no data is lost.

    Particle curves in binaryprefabs.sgo are commonly arrays of nested
    property blocks (for example RelativeTime + Color or RelativeSize).
    When that shape is detected, an ``elements`` list is included while the
    original raw payload stays available for auditing.
    """
    if not data:
        return {"count": 0, "raw_hex": ""}
    count, cur = read_ci(data, 0)
    rem = len(data) - cur
    out: dict[str, Any] = {"count": count, "raw_hex": data.hex()}
    if count > 0 and rem >= 0 and rem % count == 0:
        out["elem_size"] = rem // count
    if count > 0 and depth <= 6:
        elements: list[dict[str, Any]] = []
        elem_pos = cur
        for _ in range(count):
            try:
                props, consumed = parse_properties(
                    data,
                    elem_pos,
                    len(data) - elem_pos,
                    names,
                    imports,
                    exports,
                    depth=depth + 1,
                    stop_after_first_none=True,
                )
            except Exception:
                elements = []
                break
            if consumed <= 0 or not props:
                elements = []
                break
            elements.append(_props_to_dict(props))
            elem_pos += consumed
        if elements and len(elements) == count and elem_pos == len(data):
            out["elements"] = elements
    return out


# --------------------------------------------------------------------------
# Property parser
# --------------------------------------------------------------------------

@dataclass
class PropValue:
    name: str
    ptype: int
    type_name: str
    struct_name: str | None
    array_flag: bool
    size: int
    raw_hex: str
    value: Any
    object_ref: dict[str, Any] | None = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type_name,
        }
        if self.struct_name is not None:
            out["struct"] = self.struct_name
        if self.array_flag:
            out["array_flag"] = True
        out["value"] = self.value
        if self.object_ref is not None:
            out["object_ref"] = self.object_ref
        out["raw_hex"] = self.raw_hex
        return out


def parse_properties(buf: bytes, start: int, size: int,
                     names: list[str], imports: list[ImportEntry],
                     exports: list[ExportEntry] | None = None,
                     depth: int = 0,
                     stop_after_first_none: bool = False,
                     ) -> tuple[list[PropValue], int]:
    """Parse all property-tag blocks until `size` bytes are consumed.

    Returns (list_of_PropValue, bytes_consumed). ``bytes_consumed`` will
    equal ``size`` on a clean parse; anything less indicates a parse stop.
    A CI=0 byte closes a block; the loop continues reading a new block
    until the serial window is exhausted.
    """
    out: list[PropValue] = []
    pos = start
    end = start + size

    while pos < end:
        # A lone 0x00 byte is a None-terminator (CI=0); advance and continue.
        if buf[pos] == 0x00:
            pos += 1
            if stop_after_first_none:
                break
            continue

        ni, new_pos = read_ci(buf, pos)
        if ni < 0 or ni >= len(names):
            break
        pn = names[ni]
        if pn.lower() == "none":
            pos = new_pos
            if stop_after_first_none:
                break
            continue
        pos = new_pos

        if pos >= end:
            break

        info = buf[pos]; pos += 1
        pt = info & 0x0F
        st = (info >> 4) & 0x07
        af = (info >> 7) & 0x01

        struct_name: str | None = None
        if pt == PT_STRUCT:
            si, pos = read_ci(buf, pos)
            struct_name = names[si] if 0 <= si < len(names) else None

        # Determine payload size
        if pt == PT_BOOL:
            psz = 0
        elif st == 0:
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
            if pos >= end: break
            psz = buf[pos]; pos += 1
        elif st == 6:
            if pos + 2 > end: break
            psz = struct.unpack_from("<H", buf, pos)[0]; pos += 2
        else:  # st == 7
            if pos + 4 > end: break
            psz = struct.unpack_from("<I", buf, pos)[0]; pos += 4

        # Array index (for fixed-array element N), only when array_flag set
        # and the property itself is not a Bool.
        if af and pt != PT_BOOL:
            _, pos = read_ci(buf, pos)

        if pos + psz > end:
            break

        pdata = bytes(buf[pos:pos + psz])

        # --- Decode by ptype ---
        value: Any = None
        object_ref_detail: dict[str, Any] | None = None
        if pt == PT_BOOL:
            value = bool(af)
        elif pt == PT_BYTE and psz >= 1:
            value = pdata[0]
        elif pt == PT_INT and psz >= 4:
            value = struct.unpack_from("<i", pdata, 0)[0]
        elif pt == PT_FLOAT and psz >= 4:
            value = struct.unpack_from("<f", pdata, 0)[0]
        elif pt in (PT_OBJECT, PT_CLASS) and psz >= 1:
            ref, _ = read_ci(pdata, 0)
            object_ref_detail = resolve_object_ref_detail(ref, imports, exports)
            value = None if object_ref_detail is None else object_ref_detail.get("name")
        elif pt == PT_NAME and psz >= 1:
            ni2, _ = read_ci(pdata, 0)
            value = names[ni2] if 0 <= ni2 < len(names) else None
        elif pt == PT_STRING:
            # ansi length-prefixed
            s, _ = read_fstr(pdata, 0)
            value = s
        elif pt == PT_STR:
            # wide (UTF-16) length-prefixed; same read_fstr handles both
            s, _ = read_fstr(pdata, 0)
            value = s
        elif pt == PT_STRUCT:
            value = _decode_struct(struct_name or "", pdata, names, imports, exports, depth + 1)
        elif pt == PT_ARRAY:
            value = _decode_array(pdata, names, imports, exports, depth + 1)
        elif pt == PT_VECTOR and psz >= 12:
            x, y, z = struct.unpack_from("<fff", pdata, 0)
            value = {"x": x, "y": y, "z": z}
        elif pt == PT_ROTATOR and psz >= 12:
            p, y, r = struct.unpack_from("<iii", pdata, 0)
            value = {"pitch": p, "yaw": y, "roll": r}
        # Anything not decoded falls through with value=None; raw_hex always present.

        out.append(PropValue(
            name=pn,
            ptype=pt,
            type_name=PTYPE_NAMES.get(pt, f"pt{pt}"),
            struct_name=struct_name,
            array_flag=bool(af),
            size=psz,
            raw_hex=pdata.hex(),
            value=value,
            object_ref=object_ref_detail,
        ))
        pos += psz

    return out, pos - start


# --------------------------------------------------------------------------
# High-level: parse one mini-package
# --------------------------------------------------------------------------

@dataclass
class MiniPackage:
    file_offset: int
    byte_size: int
    header: PackageHeader
    names: list[str]
    imports: list[ImportEntry]
    exports: list[ExportEntry]
    # per-export list of (exp, props, bytes_consumed)
    export_props: list[tuple[ExportEntry, list[PropValue], int]]


def parse_mini_package(data: bytes, pkg_start: int, pkg_end: int) -> MiniPackage:
    buf = data[pkg_start:pkg_end]
    hdr, _ = parse_package_header(buf, 0)
    names = parse_name_table(buf, hdr)
    imports = parse_import_table(buf, hdr, names)
    exports = parse_export_table(buf, hdr, names, imports)

    exp_props: list[tuple[ExportEntry, list[PropValue], int]] = []
    for exp in exports:
        if exp.serial_size <= 0:
            exp_props.append((exp, [], 0))
            continue
        props, consumed = parse_properties(
            buf, exp.serial_offset, exp.serial_size, names, imports, exports
        )
        exp_props.append((exp, props, consumed))

    return MiniPackage(
        file_offset=pkg_start,
        byte_size=pkg_end - pkg_start,
        header=hdr, names=names, imports=imports, exports=exports,
        export_props=exp_props,
    )


# --------------------------------------------------------------------------
# Trailer directory
# --------------------------------------------------------------------------

@dataclass
class TrailerEntry:
    file_offset: int
    zero: int
    hash_a: int
    hash_b: int
    timestamp: int
    size_a: int
    size_a_dup: int
    cumulative: int
    namelen: int
    name: str


def parse_trailer(data: bytes, trailer_start: int) -> tuple[list[TrailerEntry], int]:
    """Parse the SGO trailer directory. Returns (entries, footer_u32)."""
    total = len(data)
    entries: list[TrailerEntry] = []
    pos = trailer_start
    footer_pos = total - 4
    while pos + 36 <= footer_pos:
        file_off, zero, ha, hb, ts, sa, sb, cum, namelen = struct.unpack_from(
            "<IIIIIIIII", data, pos
        )
        if namelen <= 0 or namelen > 400 or namelen % 2:
            break
        name_start = pos + 36
        if name_start + namelen > footer_pos:
            break
        name = data[name_start:name_start + namelen].decode("utf-16-le", errors="replace").rstrip("\x00")
        entries.append(TrailerEntry(file_off, zero, ha, hb, ts, sa, sb, cum, namelen, name))
        pos = name_start + namelen
    footer = struct.unpack_from("<I", data, footer_pos)[0]
    return entries, footer


# --------------------------------------------------------------------------
# Top-level: iterate mini-packages
# --------------------------------------------------------------------------

def find_mini_package_boundaries(data: bytes) -> list[int]:
    """Return absolute offsets of every PKG_SIG occurrence (mini-package start)."""
    out: list[int] = []
    pos = 0
    while True:
        i = data.find(PKG_SIG_BYTES, pos)
        if i == -1:
            break
        out.append(i)
        pos = i + 1
    return out
