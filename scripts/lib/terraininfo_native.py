"""Utilities for parsing Vanguard TerrainInfo native serialized data."""

from __future__ import annotations

from dataclasses import dataclass, field
import struct
from typing import Any


NATIVE_BODY_SEARCH_START = 35000
NATIVE_BODY_SEARCH_END: int | None = None
MAX_MESH_LOOKUP_COUNT = 500
DECOINSTANCE_SIZE = 22


@dataclass(frozen=True)
class MeshLookupRecord:
    mesh_index: int
    mesh_name: str
    object_ref: int
    draw_distance: float
    native_offset: int
    sub_record_count: int
    int16_record_count: int


@dataclass(frozen=True)
class DecoInstanceArrayInfo:
    count_offset: int
    array_offset: int
    count: int
    max_mesh_index: int
    validation_ratio: float


@dataclass
class TerrainInfoNativeParse:
    native_body_offset: int | None = None
    sectors_count: int = 0
    sectors_x: int = 0
    sectors_y: int = 0
    heightmap_x: int = 0
    heightmap_y: int = 0
    float_array_count: int = 0
    deco_layer_count: int = 0
    mesh_lookup_offset: int | None = None
    mesh_lookup_count: int = 0
    mesh_lookup: dict[int, MeshLookupRecord] = field(default_factory=dict)
    deco_array: DecoInstanceArrayInfo | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def mesh_mapping(self) -> dict[int, str]:
        return {index: record.mesh_name for index, record in self.mesh_lookup.items()}


def read_compact_index(data: bytes, pos: int) -> tuple[int, int]:
    """Read a UE2 compact index from binary data."""
    b0 = data[pos]
    pos += 1
    neg = b0 & 0x80
    more = b0 & 0x40
    val = b0 & 0x3F
    shift = 6
    while more:
        b = data[pos]
        pos += 1
        more = b & 0x80
        val |= (b & 0x7F) << shift
        shift += 7
    if neg:
        val = -val
    return val, pos


def import_name_lookup(imports: list[dict[str, Any]]) -> dict[int, str]:
    """Build compact-index import-name lookup from a UE2Package import table."""
    names: dict[int, str] = {}
    for imp in imports:
        idx = imp.get("index")
        if isinstance(idx, int):
            names[idx] = str(imp.get("object_name", ""))
    return names


def static_mesh_import_names(imports: list[dict[str, Any]]) -> list[str]:
    """Return StaticMesh import object names in package import-table order."""
    return [
        str(imp.get("object_name", ""))
        for imp in imports
        if imp.get("class_name") == "StaticMesh"
    ]


def find_native_body_offsets(ti_data: bytes) -> list[int]:
    """Find candidate TerrainInfo native body starts.

    The loading path begins with int32=1 followed by a TArray of 256 sectors in
    normal terrain chunks. Search through the export body and let structural
    validation reject false positives instead of relying on a fixed upper bound.
    """
    total = len(ti_data)
    search_end = total - 8
    if NATIVE_BODY_SEARCH_END is not None:
        search_end = min(search_end, NATIVE_BODY_SEARCH_END)
    if search_end < NATIVE_BODY_SEARCH_START:
        return []
    offsets: list[int] = []
    for off in range(NATIVE_BODY_SEARCH_START, search_end + 1):
        if struct.unpack_from("<II", ti_data, off) == (1, 256):
            offsets.append(off)
    return offsets


def parse_terraininfo_native(
    ti_data: bytes, import_names: dict[int, str]
) -> TerrainInfoNativeParse:
    """Parse known TerrainInfo native structures.

    Returns an empty parse object with warnings when no candidate fully parses.
    """
    warnings: list[str] = []
    candidates = find_native_body_offsets(ti_data)
    if not candidates:
        return TerrainInfoNativeParse(warnings=["native body signature not found"])
    for nb_start in candidates:
        try:
            parsed = _parse_native_body_at(ti_data, import_names, nb_start)
        except (IndexError, struct.error, ValueError) as exc:
            warnings.append(f"candidate {nb_start}: {exc}")
            continue
        if parsed.mesh_lookup:
            parsed.deco_array = find_decoinstance_array(ti_data, parsed.mesh_lookup_count)
            parsed.warnings.extend(warnings)
            return parsed
        warnings.append(f"candidate {nb_start}: no mesh lookup records")
    return TerrainInfoNativeParse(warnings=warnings)


def _parse_native_body_at(
    ti_data: bytes, import_names: dict[int, str], nb_start: int
) -> TerrainInfoNativeParse:
    total = len(ti_data)
    if nb_start < 0 or nb_start + 8 > total:
        raise ValueError("native body candidate out of range")
    nb = ti_data[nb_start:]
    pos = 4

    sectors_count = _read_i32(nb, pos, "sectors count")
    pos += 4
    _guard_count(sectors_count, 0, 10000, "sectors count")
    for _ in range(sectors_count):
        _, pos = read_compact_index(nb, pos)

    sectors_x = _read_i32(nb, pos, "SectorsX")
    sectors_y = _read_i32(nb, pos + 4, "SectorsY")
    pos += 8
    pos += 96
    heightmap_x = _read_i32(nb, pos, "HeightmapX")
    heightmap_y = _read_i32(nb, pos + 4, "HeightmapY")
    pos += 8

    _, pos = read_compact_index(nb, pos)
    float_count = _read_i32(nb, pos, "float array count")
    pos += 4
    _guard_count(float_count, 0, 1000000, "float array count")
    pos += float_count * 4

    pos += 4

    deco_layer_count = _read_i32(nb, pos, "DecoLayer count")
    pos += 4
    _guard_count(deco_layer_count, 0, 10000, "DecoLayer count")
    for _ in range(deco_layer_count):
        pos += 12 + 12 + 4 + 25
        arr12 = _read_i32(nb, pos, "DecoLayer arr12 count")
        pos += 4
        _guard_count(arr12, 0, 100000, "DecoLayer arr12 count")
        pos += arr12 * 12
        arr32 = _read_i32(nb, pos, "DecoLayer arr32 count")
        pos += 4
        _guard_count(arr32, 0, 100000, "DecoLayer arr32 count")
        pos += arr32 * 4
        pos += 25 + 4

    mesh_lookup_offset = nb_start + pos
    mesh_count = _read_i32(nb, pos, "mesh lookup count")
    pos += 4
    _guard_count(mesh_count, 0, MAX_MESH_LOOKUP_COUNT, "mesh lookup count")

    mesh_lookup: dict[int, MeshLookupRecord] = {}
    for mesh_index in range(mesh_count):
        record_offset = nb_start + pos
        draw_distance = _read_f32(nb, pos, "mesh lookup draw distance")
        pos += 4
        obj_ref, pos = read_compact_index(nb, pos)
        if obj_ref < 0:
            mesh_name = import_names.get(obj_ref, f"import_{obj_ref}")
        elif obj_ref > 0:
            mesh_name = f"export_{obj_ref}"
        else:
            mesh_name = "None"

        sub_count = _read_i32(nb, pos, "mesh lookup sub-record count")
        pos += 4
        _guard_count(sub_count, 0, 100000, "mesh lookup sub-record count")
        pos += sub_count * 36
        pos += 4
        int16_count = _read_i32(nb, pos, "mesh lookup int16 count")
        pos += 4
        _guard_count(int16_count, 0, 100000, "mesh lookup int16 count")
        pos += int16_count * 2
        pos += 8

        mesh_lookup[mesh_index] = MeshLookupRecord(
            mesh_index=mesh_index,
            mesh_name=mesh_name,
            object_ref=obj_ref,
            draw_distance=draw_distance,
            native_offset=record_offset,
            sub_record_count=sub_count,
            int16_record_count=int16_count,
        )

    return TerrainInfoNativeParse(
        native_body_offset=nb_start,
        sectors_count=sectors_count,
        sectors_x=sectors_x,
        sectors_y=sectors_y,
        heightmap_x=heightmap_x,
        heightmap_y=heightmap_y,
        float_array_count=float_count,
        deco_layer_count=deco_layer_count,
        mesh_lookup_offset=mesh_lookup_offset,
        mesh_lookup_count=mesh_count,
        mesh_lookup=mesh_lookup,
    )


def find_decoinstance_array(
    ti_data: bytes, mesh_count: int
) -> DecoInstanceArrayInfo | None:
    """Find a valid DecoInstance array using the Ghidra-verified 22-byte layout."""
    if mesh_count <= 0:
        return None
    total = len(ti_data)
    best: DecoInstanceArrayInfo | None = None
    for off in range(0, total - (4 + DECOINSTANCE_SIZE)):
        count = struct.unpack_from("<i", ti_data, off)[0]
        if count < 50 or count > 30000:
            continue
        array_start = off + 4
        array_end = array_start + count * DECOINSTANCE_SIZE
        if array_end > total:
            continue
        if not _decoinstance_sample_valid(ti_data, array_start, count, mesh_count):
            continue
        max_mesh_index, ratio = _decoinstance_validation_stats(
            ti_data, array_start, count, mesh_count
        )
        if ratio < 0.95 or max_mesh_index >= mesh_count:
            continue
        candidate = DecoInstanceArrayInfo(
            count_offset=off,
            array_offset=array_start,
            count=count,
            max_mesh_index=max_mesh_index,
            validation_ratio=ratio,
        )
        if (
            best is None
            or candidate.validation_ratio > best.validation_ratio
            or (
                candidate.validation_ratio == best.validation_ratio
                and candidate.count > best.count
            )
        ):
            best = candidate
    return best


def iter_decoinstance_records(
    ti_data: bytes,
    array_info: DecoInstanceArrayInfo,
    mesh_lookup: dict[int, MeshLookupRecord],
):
    """Yield decoded non-null DecoInstance records with native metadata."""
    for instance_index in range(array_info.count):
        offset = array_info.array_offset + instance_index * DECOINSTANCE_SIZE
        mesh_index = struct.unpack_from("<h", ti_data, offset)[0]
        px, py, pz = struct.unpack_from("<3f", ti_data, offset + 2)
        flag1 = ti_data[offset + 14]
        # The placement heading is the first byte after Flag1. It is authored
        # in sixteen 22.5-degree buckets (0, 16, ... 240). Bytes 16 and 17 are
        # the remaining compact rotation/control fields. Reading byte 16 as
        # yaw makes most full-size trees identity-rotated because that field is
        # normally zero for upright vegetation.
        yaw_byte = ti_data[offset + 15]
        pitch_byte = ti_data[offset + 16]
        roll_byte = ti_data[offset + 17]
        scale = struct.unpack_from("<f", ti_data, offset + 18)[0]
        if px == 0 and py == 0 and pz == 0 and scale == 0:
            continue
        lookup_record = mesh_lookup.get(mesh_index)
        if lookup_record is None:
            continue
        yield {
            "instance_index": instance_index,
            "native_offset": offset,
            "mesh_index": mesh_index,
            "mesh_lookup": lookup_record,
            "position": (px, py, pz),
            "flag1": flag1,
            "yaw_byte": yaw_byte,
            "pitch_byte": pitch_byte,
            "roll_byte": roll_byte,
            "scale": scale,
        }


def _decoinstance_sample_valid(
    ti_data: bytes, array_start: int, count: int, mesh_count: int
) -> bool:
    sample_ok = 0
    sample_total = 0
    sample_indexes = list(range(min(5, count))) + list(range(max(0, count - 5), count))
    for ci in sample_indexes:
        offset = array_start + ci * DECOINSTANCE_SIZE
        mesh_idx = struct.unpack_from("<h", ti_data, offset)[0]
        scale = struct.unpack_from("<f", ti_data, offset + 18)[0]
        sample_total += 1
        if 0 <= mesh_idx < mesh_count and 0.0 <= scale < 50:
            sample_ok += 1
    return sample_total > 0 and sample_ok / sample_total >= 0.8


def _decoinstance_validation_stats(
    ti_data: bytes, array_start: int, count: int, mesh_count: int
) -> tuple[int, float]:
    valid = 0
    max_mesh_index = 0
    for ci in range(count):
        offset = array_start + ci * DECOINSTANCE_SIZE
        if offset + DECOINSTANCE_SIZE > len(ti_data):
            break
        mesh_idx = struct.unpack_from("<h", ti_data, offset)[0]
        px, py, pz = struct.unpack_from("<3f", ti_data, offset + 2)
        scale = struct.unpack_from("<f", ti_data, offset + 18)[0]
        max_mesh_index = max(max_mesh_index, mesh_idx)
        if px == 0 and py == 0 and pz == 0 and scale == 0:
            valid += 1
            continue
        if (
            abs(px) < 200000
            and abs(py) < 200000
            and abs(pz) < 200000
            and 0.0 <= scale < 50
            and 0 <= mesh_idx < mesh_count
        ):
            valid += 1
    return max_mesh_index, valid / count if count > 0 else 0.0


def _guard_count(value: int, min_value: int, max_value: int, label: str) -> None:
    if value < min_value or value > max_value:
        raise ValueError(f"{label} out of range: {value}")


def _read_i32(data: bytes, pos: int, label: str) -> int:
    if pos < 0 or pos + 4 > len(data):
        raise ValueError(f"{label} read out of range at {pos}")
    return struct.unpack_from("<i", data, pos)[0]


def _read_f32(data: bytes, pos: int, label: str) -> float:
    if pos < 0 or pos + 4 > len(data):
        raise ValueError(f"{label} read out of range at {pos}")
    return struct.unpack_from("<f", data, pos)[0]
