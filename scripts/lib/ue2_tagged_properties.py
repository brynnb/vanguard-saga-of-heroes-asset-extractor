"""Lossless reader for Unreal Engine 2 tagged-property blocks.

This module intentionally separates tag decoding from schema interpretation.
Callers must decide which ArrayProperty payload type is valid for a named
field; treating every array element as an object reference was the source of
the old attachment catalog's false edges.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Any, Iterable

from scripts.lib.ue2_property_reader import BinaryReader


TYPE_BYTE = 1
TYPE_INT = 2
TYPE_BOOL = 3
TYPE_FLOAT = 4
TYPE_OBJECT = 5
TYPE_NAME = 6
TYPE_ARRAY = 9
TYPE_STRUCT = 10


@dataclass(frozen=True)
class TaggedProperty:
    name: str
    type_id: int
    raw: bytes
    is_array_indexed: bool = False
    array_index: int = 0
    struct_name: str = ""


class TaggedPropertyError(ValueError):
    pass


def read_tagged_properties(
    data: bytes | bytearray,
    names: list[str],
    *,
    require_terminator: bool = True,
) -> list[TaggedProperty]:
    """Read one UE2 property list, preserving order and duplicates.

    Serialized StructProperty payload sizes exclude the outer ``None`` byte in
    some Vanguard packages. Callers decoding such a bounded payload must pass
    ``require_terminator=False``; top-level export blocks should remain strict.
    """
    reader = BinaryReader(data, 0)
    result: list[TaggedProperty] = []
    while reader.tell() < len(data):
        try:
            name_index = reader.read_compact_index()
        except (IndexError, struct.error) as exc:
            raise TaggedPropertyError("truncated property name") from exc
        if not 0 <= name_index < len(names):
            raise TaggedPropertyError(f"property name index {name_index} is out of range")
        name = names[name_index]
        if name.lower() == "none":
            return result

        try:
            info = reader.read_byte()
            is_array_indexed = bool(info & 0x80)
            type_id = info & 0x0F
            struct_name = ""
            if type_id == TYPE_STRUCT:
                struct_index = reader.read_compact_index()
                if not 0 <= struct_index < len(names):
                    raise TaggedPropertyError(
                        f"property {name!r} has invalid struct name index {struct_index}"
                    )
                struct_name = names[struct_index]

            size_type = (info >> 4) & 7
            if size_type == 0:
                data_size = 1
            elif size_type == 1:
                data_size = 2
            elif size_type == 2:
                data_size = 4
            elif size_type == 3:
                data_size = 12
            elif size_type == 4:
                data_size = 16
            elif size_type == 5:
                data_size = reader.read_byte()
            elif size_type == 6:
                data_size = reader.read_uint16()
            else:
                data_size = reader.read_int32()

            array_index = 0
            if type_id != TYPE_BOOL and is_array_indexed:
                first = reader.read_byte()
                if first < 128:
                    array_index = first
                else:
                    second = reader.read_byte()
                    array_index = (first & 0x3F) | (second << 6)
                    if first & 0x40:
                        third = reader.read_byte()
                        fourth = reader.read_byte()
                        array_index = (
                            (first & 0x3F)
                            | (second << 6)
                            | (third << 14)
                            | (fourth << 22)
                        )
        except (IndexError, struct.error) as exc:
            raise TaggedPropertyError(f"truncated header for property {name!r}") from exc

        if type_id == TYPE_BOOL:
            raw = b""
        else:
            end = reader.tell() + data_size
            if data_size < 0 or end > len(data):
                raise TaggedPropertyError(
                    f"property {name!r} payload exceeds its property block"
                )
            raw = bytes(reader.data[reader.tell() : end])
            reader.seek(end)
        result.append(
            TaggedProperty(
                name=name,
                type_id=type_id,
                raw=raw,
                is_array_indexed=is_array_indexed,
                array_index=array_index,
                struct_name=struct_name,
            )
        )
    if require_terminator:
        raise TaggedPropertyError("property block has no None terminator")
    return result


def properties_by_name(properties: Iterable[TaggedProperty]) -> dict[str, TaggedProperty]:
    """Index a schema that requires unique property names, rejecting duplicates."""
    result: dict[str, TaggedProperty] = {}
    for prop in properties:
        if prop.name in result:
            raise TaggedPropertyError(f"duplicate property {prop.name!r}")
        result[prop.name] = prop
    return result


def decode_compact_index(raw: bytes | bytearray) -> int:
    if not raw:
        raise TaggedPropertyError("empty compact index")
    reader = BinaryReader(raw, 0)
    try:
        value = reader.read_compact_index()
    except (IndexError, struct.error) as exc:
        raise TaggedPropertyError("truncated compact index") from exc
    if reader.tell() != len(raw):
        raise TaggedPropertyError("compact index payload has trailing bytes")
    return value


def decode_object_reference_array(raw: bytes | bytearray) -> list[int]:
    """Decode an explicitly object-typed UE2 ArrayProperty payload."""
    reader = BinaryReader(raw, 0)
    try:
        count = reader.read_compact_index()
        if count < 0:
            raise TaggedPropertyError(f"negative object array count {count}")
        refs = [reader.read_compact_index() for _ in range(count)]
    except (IndexError, struct.error) as exc:
        raise TaggedPropertyError("truncated object-reference array") from exc
    if reader.tell() != len(raw):
        raise TaggedPropertyError("object-reference array has trailing bytes")
    return refs


def decode_scalar(prop: TaggedProperty, names: list[str]) -> Any:
    if prop.type_id == TYPE_BOOL:
        return prop.is_array_indexed
    if prop.type_id == TYPE_BYTE:
        if len(prop.raw) != 1:
            raise TaggedPropertyError(f"byte property {prop.name!r} is not one byte")
        return prop.raw[0]
    if prop.type_id == TYPE_INT:
        if len(prop.raw) != 4:
            raise TaggedPropertyError(f"int property {prop.name!r} is not four bytes")
        return struct.unpack("<i", prop.raw)[0]
    if prop.type_id == TYPE_FLOAT:
        if len(prop.raw) != 4:
            raise TaggedPropertyError(f"float property {prop.name!r} is not four bytes")
        return struct.unpack("<f", prop.raw)[0]
    if prop.type_id in (TYPE_OBJECT, TYPE_NAME):
        index = decode_compact_index(prop.raw)
        if prop.type_id == TYPE_NAME:
            if not 0 <= index < len(names):
                raise TaggedPropertyError(
                    f"name property {prop.name!r} index {index} is out of range"
                )
            return names[index]
        return index
    raise TaggedPropertyError(
        f"property {prop.name!r} type {prop.type_id} is not a scalar"
    )
