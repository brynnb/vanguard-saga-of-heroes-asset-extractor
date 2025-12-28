#!/usr/bin/env python3
"""
Universal UE2 Property Parser

Parses properties from ALL export types by auto-detecting the property start offset.
This creates a comprehensive knowledge base of all parsed data from the client.

UE2 Property Format:
  [name_index: compact_int][info_byte][size?][struct_name?][array_idx?][value]

Info byte layout:
  bits 0-3: property type (0=None, 1=Byte, 2=Int, 3=Bool, 4=Float, etc.)
  bits 4-6: size type (0=1byte, 1=2bytes, 2=4bytes, 3=12bytes, 4=16bytes, 5-7=variable)
  bit 7: array flag

Usage:
    python universal_property_parser.py                    # Re-parse all
    python universal_property_parser.py --stats            # Show parsing stats
    python universal_property_parser.py --class LevelInfo  # Parse specific class
"""

import sys
import os
import sqlite3
import struct
import json
from pathlib import Path
from collections import defaultdict

from .package import UE2Package
from .reader import read_compact_index_at as read_compact_index

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "data", "vanguard_data.db")
MAPS_DIR = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Maps"

PROP_TYPES = {
    0: "None",
    1: "Byte",
    2: "Int",
    3: "Bool",
    4: "Float",
    5: "Object",
    6: "Name",
    7: "String",
    8: "Class",
    9: "Array",
    10: "Struct",
    11: "Vector",
    12: "Rotator",
    13: "Str",
    14: "Map",
    15: "FixedArray",
}

# Expected sizes for fixed-size property types
EXPECTED_SIZES = {
    1: [1],  # Byte: always 1 byte
    2: [4],  # Int: always 4 bytes
    3: [0],  # Bool: 0 bytes (value in array flag)
    4: [4],  # Float: always 4 bytes
    11: [12],  # Vector: always 12 bytes (3 floats)
    12: [12],  # Rotator: always 12 bytes (3 ints)
}

# Minimum sizes for variable-size property types
# If a property is smaller than this, it's likely misidentified
MIN_SIZES = {
    7: 2,  # String: at least length byte + 1 char
    9: 4,  # Array: at least count + some data
    10: 4,  # Struct: at least one property or terminator
    13: 2,  # Str: at least length byte + 1 char
}

# Maximum sizes for reference types (compact index is typically 1-5 bytes)
MAX_SIZES = {
    5: 5,  # Object: compact index reference
    6: 5,  # Name: compact index reference
    8: 5,  # Class: compact index reference
}

# Types that are extremely rare in UE2 and almost always indicate sync issues
# When we see these, stop parsing immediately
INVALID_TYPES = {14, 15}  # Map, FixedArray - virtually never used in Vanguard

# Struct names that are actually property names - indicates parser sync issue
INVALID_STRUCT_NAMES = {
    "InternalTime",
    "LODSet",
    "StartSpinRange",
    "StartLocationRange",
    "LifetimeRange",
    "StartVelocityRange",
    "CoordinateSystem",
    "bLockLocation",
    "UClamp",
    "VClamp",
    "pBuildingTileLayerData",
    "PrefabPackageName",
}

# Property names that should never be String/Str type - indicates sync issue
NON_STRING_PROP_NAMES = {
    "Vector",
    "Region",
    "StartSizeRange",
    "InternalTime",
    "Zone",
    "PointRegion",
    "UBits",
    "VBits",
    "Level",
    "Tag",
    "bLightChanged",
    "WaterVolumeType",
    "VelocityVectors",
    "pBuildingTileLayerData",
    "Rotation",
    "Location",
    "Color",
    "RelativeTime",
    "ZoneNumber",
    "bNoDelete",
    "Scale",
    "LevelInfo",
    "m_CompoundObjectType",
    "ChunkPosition",
    "Layers",
    "MainScale",
    "Range",
    "PostScale",
    "DrawScale",
    "DrawScale3D",
    "Brush",
    "Model",
    "Texture",
    "TerrainLayer",
    "Rotator",
    "SheerAxis",
    "SheerRate",
    "UClamp",
    "WaterVolume",
    "iLeaf",
    "ColLocation",
    "Format",
    "MaxParticles",
    "RangeVector",
    "USize",
    "VSize",
    "VClamp",
    "RelativeVelocity",
    "LocationPriority",
    "bSelected",
}

# Property names that should never be Name type - indicates sync issue
NON_NAME_PROP_NAMES = {
    "InternalTime",
    "Vector",
    "VBits",
    "UClamp",
    "LODSet",
    "m_CompoundObjectType",
    "pBuildingTileLayerData",
    "RangeVector",
    "SheerRate",
    "Summary",
    "CameraLocationDynamic",
    "FadeOutStartTime",
}

# Property names that should never be Array type - indicates sync issue
NON_ARRAY_PROP_NAMES = {
    "Vector",
    "bDynamicLight",
    "Color",
    "Max",
    "pBuildingTileLayerData",
    "LightAmbientBrightness",
    "LightAmbientColor",
    "VSize",
    "Group",
}


def validate_property_type_size(prop_type: int, prop_size: int) -> bool:
    """Check if property type and size are compatible."""
    # Reject types that are almost always misidentified
    if prop_type in INVALID_TYPES:
        return False

    # Check exact size requirements
    if prop_type in EXPECTED_SIZES:
        return prop_size in EXPECTED_SIZES[prop_type]

    # Check minimum size requirements
    if prop_type in MIN_SIZES and prop_size < MIN_SIZES[prop_type]:
        return False

    # Check maximum size requirements for reference types
    if prop_type in MAX_SIZES and prop_size > MAX_SIZES[prop_type]:
        return False

    return True  # Other types have no constraints


def parse_struct_value(
    data: bytes, struct_name: str, names: list = None, depth: int = 0
) -> str:
    """Parse known struct types into readable format.

    For complex structs with nested property lists, recursively parse them.
    """
    if not data:
        return None

    # Prevent infinite recursion
    if depth > 3:
        return None

    try:
        # Color struct (4 bytes: R, G, B, A) - only parse if exactly 4 bytes
        if struct_name == "Color" and len(data) == 4:
            r, g, b, a = data[0], data[1], data[2], data[3]
            return json.dumps({"r": r, "g": g, "b": b, "a": a})

        # Vector struct (12 bytes: X, Y, Z floats) - only parse if exactly 12 bytes
        if struct_name == "Vector" and len(data) == 12:
            x, y, z = struct.unpack("<fff", data[:12])
            if all(-1e10 < v < 1e10 for v in (x, y, z)):
                return json.dumps(
                    {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}
                )

        # Rotator struct (12 bytes: Pitch, Yaw, Roll ints) - only parse if exactly 12 bytes
        if struct_name == "Rotator" and len(data) == 12:
            pitch, yaw, roll = struct.unpack("<iii", data[:12])
            return json.dumps({"pitch": pitch, "yaw": yaw, "roll": roll})

        # Scale structs - check for 12-byte raw floats
        if (
            struct_name in ("Scale", "MainScale", "PostScale", "DrawScale3D")
            and len(data) == 12
        ):
            x, y, z = struct.unpack("<fff", data[:12])
            if all(-1e6 < v < 1e6 for v in (x, y, z)):
                return json.dumps(
                    {"x": round(x, 4), "y": round(y, 4), "z": round(z, 4)}
                )

        # Range struct (8 bytes: Min, Max floats)
        # Also handles particle emitter range structs
        range_structs = (
            "Range",
            "StartSpinRange",
            "SpinsPerSecondRange",
            "StartSizeRange",
            "LifetimeRange",
            "InitialDelayRange",
        )
        if struct_name in range_structs and len(data) == 8:
            min_val, max_val = struct.unpack("<ff", data[:8])
            if all(-1e10 < v < 1e10 for v in (min_val, max_val)):
                return json.dumps({"min": round(min_val, 2), "max": round(max_val, 2)})

        # RangeVector struct (24 bytes: Min Vector, Max Vector)
        # Used for location/velocity ranges in particle emitters
        range_vector_structs = (
            "StartLocationRange",
            "StartVelocityRange",
            "ColorMultiplierRange",
            "VelocityLossRange",
        )
        if struct_name in range_vector_structs and len(data) == 24:
            min_x, min_y, min_z = struct.unpack("<fff", data[0:12])
            max_x, max_y, max_z = struct.unpack("<fff", data[12:24])
            if all(
                -1e10 < v < 1e10 for v in (min_x, min_y, min_z, max_x, max_y, max_z)
            ):
                return json.dumps(
                    {
                        "min": {
                            "x": round(min_x, 2),
                            "y": round(min_y, 2),
                            "z": round(min_z, 2),
                        },
                        "max": {
                            "x": round(max_x, 2),
                            "y": round(max_y, 2),
                            "z": round(max_z, 2),
                        },
                    }
                )

        # Plane struct (16 bytes: X, Y, Z, W floats)
        if struct_name == "Plane" and len(data) == 16:
            x, y, z, w = struct.unpack("<ffff", data[:16])
            if all(-1e10 < v < 1e10 for v in (x, y, z, w)):
                return json.dumps(
                    {
                        "x": round(x, 4),
                        "y": round(y, 4),
                        "z": round(z, 4),
                        "w": round(w, 4),
                    }
                )

        # Box struct (24 bytes: Min Vector, Max Vector)
        if struct_name == "Box" and len(data) >= 24:
            min_x, min_y, min_z = struct.unpack("<fff", data[0:12])
            max_x, max_y, max_z = struct.unpack("<fff", data[12:24])
            if all(
                -1e10 < v < 1e10 for v in (min_x, min_y, min_z, max_x, max_y, max_z)
            ):
                return json.dumps(
                    {
                        "min": {
                            "x": round(min_x, 1),
                            "y": round(min_y, 1),
                            "z": round(min_z, 1),
                        },
                        "max": {
                            "x": round(max_x, 1),
                            "y": round(max_y, 1),
                            "z": round(max_z, 1),
                        },
                    }
                )

        # Guid struct (16 bytes: 4 uint32s)
        if struct_name == "Guid" and len(data) == 16:
            a, b, c, d = struct.unpack("<IIII", data[:16])
            return json.dumps({"guid": f"{a:08X}-{b:08X}-{c:08X}-{d:08X}"})

        # Object reference (4 bytes: compact index)
        if len(data) == 4 and struct_name in (
            "StaticMeshActor",
            "Texture",
            "Material",
            "StaticMesh",
            "Shader",
            "Combiner",
            "AlphaMap",
            "LayerWeightMap",
            "Layers",
            "LODSet",
            "CullDistance",
            "CompoundObject",
        ):
            ref_idx = struct.unpack("<i", data[:4])[0]
            return json.dumps({"ref": ref_idx})

        # Location/Position structs - 12 bytes = Vector
        if struct_name in ("Location", "ColLocation", "Min", "Max") and len(data) == 12:
            x, y, z = struct.unpack("<fff", data[:12])
            if all(-1e10 < v < 1e10 for v in (x, y, z)):
                return json.dumps(
                    {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}
                )

        # 2D coordinate (8 bytes: 2 floats)
        if len(data) == 8 and struct_name in ("USize", "VSize", "UClamp", "VClamp"):
            u, v = struct.unpack("<ff", data[:8])
            if all(-1e10 < val < 1e10 for val in (u, v)):
                return json.dumps({"u": round(u, 2), "v": round(v, 2)})

        # Integer pair (8 bytes: 2 ints)
        if len(data) == 8 and struct_name in ("UBits", "VBits", "Format"):
            a, b = struct.unpack("<ii", data[:8])
            return json.dumps({"a": a, "b": b})

        # Single int (4 bytes)
        if len(data) == 4 and struct_name in (
            "InternalTime",
            "ZoneNumber",
            "Priority",
            "MaxParticles",
            "WaterVolumeType",
        ):
            val = struct.unpack("<i", data[:4])[0]
            return json.dumps({"value": val})

        # Single float (4 bytes)
        if len(data) == 4 and struct_name in (
            "DistanceFogStart",
            "DistanceFogEnd",
            "DrawScale",
            "CullDistance",
            "FadeIn",
            "FadeOut",
            "LifetimeRange",
        ):
            val = struct.unpack("<f", data[:4])[0]
            if -1e10 < val < 1e10:
                return json.dumps({"value": round(val, 4)})

        # NonUniformRelativeSize (16 bytes: 4 floats)
        if struct_name == "NonUniformRelativeSize" and len(data) == 16:
            vals = struct.unpack("<ffff", data[:16])
            if all(-1e6 < v < 1e6 for v in vals):
                return json.dumps(
                    {
                        "x": round(vals[0], 4),
                        "y": round(vals[1], 4),
                        "z": round(vals[2], 4),
                        "w": round(vals[3], 4),
                    }
                )

        # For unknown structs, try to decode as floats if size is multiple of 4 and small
        if len(data) >= 4 and len(data) % 4 == 0 and len(data) <= 16:
            floats = struct.unpack(f"<{len(data)//4}f", data)
            # Check if values look like floats (not garbage)
            if all(-1e10 < f < 1e10 for f in floats):
                return json.dumps([round(f, 4) for f in floats])

        # For larger structs, try recursive property parsing if we have names
        if names and len(data) > 4:
            nested = parse_nested_struct(data, names, depth + 1)
            if nested:
                return nested

    except Exception:
        pass

    return None


def parse_nested_struct(data: bytes, names: list, depth: int = 0) -> str:
    """Parse a struct that contains a nested property list."""
    if depth > 3 or len(data) < 2:
        return None

    try:
        # Try to parse as a property list starting at offset 0
        props = parse_struct_properties(data, names, 0, depth)
        if props and len(props) > 0:
            return json.dumps(props)
    except:
        pass

    return None


def parse_struct_properties(
    data: bytes, names: list, start_offset: int, depth: int
) -> dict:
    """Parse properties from a nested struct, returning a dict of name->value."""
    if depth > 3:
        return None

    result = {}
    offset = start_offset
    max_props = 20  # Limit for nested structs

    while offset < len(data) - 1 and len(result) < max_props:
        # Read property name index
        name_idx, new_offset = read_compact_index(data, offset)

        if name_idx is None or name_idx < 0 or name_idx >= len(names):
            break

        prop_name = names[name_idx]

        if prop_name == "None":
            break

        # Validate property name
        if not prop_name or len(prop_name) < 1 or len(prop_name) > 100:
            break
        if prop_name[0].isdigit():
            break

        offset = new_offset
        if offset >= len(data):
            break

        # Read info byte
        info_byte = data[offset]
        offset += 1

        prop_type = info_byte & 0x0F
        size_type = (info_byte >> 4) & 0x07
        array_flag = (info_byte >> 7) & 0x01

        if prop_type > 15:
            break

        # Calculate property size
        prop_size = 0
        if size_type == 0:
            prop_size = 1
        elif size_type == 1:
            prop_size = 2
        elif size_type == 2:
            prop_size = 4
        elif size_type == 3:
            prop_size = 12
        elif size_type == 4:
            prop_size = 16
        elif size_type == 5:
            if offset >= len(data):
                break
            prop_size = data[offset]
            offset += 1
        elif size_type == 6:
            if offset + 2 > len(data):
                break
            prop_size = struct.unpack("<H", data[offset : offset + 2])[0]
            offset += 2
        elif size_type == 7:
            if offset + 4 > len(data):
                break
            prop_size = struct.unpack("<I", data[offset : offset + 4])[0]
            offset += 4

        # Sanity check size
        if prop_size > len(data) - offset or prop_size > 10000:
            break

        # Read struct name for struct types
        struct_name = None
        if prop_type == 10:
            struct_idx, offset = read_compact_index(data, offset)
            if struct_idx is not None and 0 <= struct_idx < len(names):
                struct_name = names[struct_idx]

        # Skip array index
        if array_flag and prop_type != 3:
            if offset < len(data):
                offset += 1

        # Read value
        if offset + prop_size > len(data):
            break

        value_bytes = data[offset : offset + prop_size]
        offset += prop_size

        # Parse value based on type
        value = None
        try:
            if prop_type == 1:  # Byte
                value = value_bytes[0] if value_bytes else None
            elif prop_type == 2:  # Int
                if len(value_bytes) >= 4:
                    value = struct.unpack("<i", value_bytes[:4])[0]
            elif prop_type == 3:  # Bool
                value = bool(array_flag)
            elif prop_type == 4:  # Float
                if len(value_bytes) >= 4:
                    val = struct.unpack("<f", value_bytes[:4])[0]
                    if -1e10 < val < 1e10:
                        value = round(val, 6)
            elif prop_type == 5:  # Object reference
                obj_idx, _ = read_compact_index(value_bytes, 0)
                value = {"ref": obj_idx}
            elif prop_type == 6:  # Name reference
                name_ref, _ = read_compact_index(value_bytes, 0)
                if name_ref is not None and 0 <= name_ref < len(names):
                    value = names[name_ref]
            elif prop_type == 10:  # Struct - recurse
                parsed = parse_struct_value(value_bytes, struct_name, names, depth + 1)
                if parsed:
                    try:
                        value = json.loads(parsed)
                    except:
                        value = parsed
            elif prop_type == 11:  # Vector
                if len(value_bytes) >= 12:
                    x, y, z = struct.unpack("<fff", value_bytes[:12])
                    if all(-1e10 < v < 1e10 for v in (x, y, z)):
                        value = {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}
            elif prop_type == 12:  # Rotator
                if len(value_bytes) >= 12:
                    pitch, yaw, roll = struct.unpack("<iii", value_bytes[:12])
                    value = {"pitch": pitch, "yaw": yaw, "roll": roll}
        except:
            pass

        if value is not None:
            result[prop_name] = value

    return result if len(result) > 0 else None


def parse_array_value(data: bytes, prop_name: str, struct_name: str) -> str:
    """Parse array values into readable format."""
    if not data or len(data) < 1:
        return None

    try:
        # First byte (or compact index) is array count
        count, offset = read_compact_index(data, 0)
        if count is None or count < 0 or count > 1000:
            return None

        remaining = data[offset:]

        # VelocityLocations / VelocityVectors - arrays of Vectors
        if (
            prop_name in ("VelocityLocations", "VelocityVectors")
            and len(remaining) >= count * 12
        ):
            vectors = []
            for i in range(count):
                x, y, z = struct.unpack("<fff", remaining[i * 12 : (i + 1) * 12])
                vectors.append({"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)})
            return json.dumps({"count": count, "vectors": vectors})

        # Generic array of objects (compact indices)
        if len(remaining) == count * 1 or (count > 0 and len(remaining) // count == 1):
            # Array of bytes
            values = list(remaining[:count])
            return json.dumps({"count": count, "values": values})

        # Array of ints (4 bytes each)
        if count > 0 and len(remaining) >= count * 4 and len(remaining) // count == 4:
            values = [
                struct.unpack("<i", remaining[i * 4 : (i + 1) * 4])[0]
                for i in range(count)
            ]
            return json.dumps({"count": count, "values": values})

        # Array of floats (4 bytes each)
        if count > 0 and len(remaining) >= count * 4:
            try:
                values = [
                    round(struct.unpack("<f", remaining[i * 4 : (i + 1) * 4])[0], 4)
                    for i in range(count)
                ]
                if all(-1e10 < v < 1e10 for v in values):
                    return json.dumps({"count": count, "floats": values})
            except:
                pass

        # Just return count if we can't parse the contents
        return json.dumps({"count": count, "size": len(remaining)})

    except Exception:
        pass

    return None


def find_property_start(data: bytes, names: list, max_search: int = 50) -> int:
    """
    Auto-detect where properties start by finding the offset that produces
    the most valid property chain ending in 'None'.

    UE2 object data typically has:
    - Bytes 0-1: Class reference (compact index)
    - Bytes 2-9: Padding (often 0xFFFFFFFF...)
    - Bytes 10+: Property list

    But some objects have longer headers, so we search up to max_search bytes.
    """
    if len(data) < 2:
        return -1

    best_offset = -1
    best_score = 0

    for start in range(min(max_search, len(data) - 2)):
        score = score_property_chain(data, names, start)
        if score > best_score:
            best_score = score
            best_offset = start

    return best_offset if best_score > 0 else -1


def score_property_chain(data: bytes, names: list, start_offset: int) -> int:
    """
    Score how valid a property chain is from this offset.
    Higher score = more likely to be correct start.
    """
    offset = start_offset
    score = 0
    seen_props = 0

    while offset < len(data) - 1 and seen_props < 100:
        # Read property name index
        name_idx, new_offset = read_compact_index(data, offset)

        if name_idx is None or name_idx < 0 or name_idx >= len(names):
            break

        prop_name = names[name_idx]

        # Found None terminator - this is good!
        if prop_name == "None":
            score += 10 + seen_props * 2  # Bonus for finding terminator
            break

        # Validate property name looks reasonable
        if not prop_name or len(prop_name) < 1 or len(prop_name) > 100:
            break

        # Check for garbage names (all numbers, weird chars, or object-like names)
        if prop_name[0].isdigit() or prop_name.startswith("_"):
            score -= 5
        # Names ending with numbers (like CompoundObject61) are likely object names, not properties
        if prop_name[-1].isdigit() and any(c.isalpha() for c in prop_name):
            score -= 3

        offset = new_offset
        if offset >= len(data):
            break

        # Read and validate info byte
        info_byte = data[offset]
        offset += 1

        prop_type = info_byte & 0x0F
        size_type = (info_byte >> 4) & 0x07
        array_flag = (info_byte >> 7) & 0x01

        if prop_type > 15:
            break

        # Calculate property size
        prop_size = 0
        if size_type == 0:
            prop_size = 1
        elif size_type == 1:
            prop_size = 2
        elif size_type == 2:
            prop_size = 4
        elif size_type == 3:
            prop_size = 12
        elif size_type == 4:
            prop_size = 16
        elif size_type == 5:
            if offset >= len(data):
                break
            prop_size = data[offset]
            offset += 1
        elif size_type == 6:
            if offset + 2 > len(data):
                break
            prop_size = struct.unpack("<H", data[offset : offset + 2])[0]
            offset += 2
        elif size_type == 7:
            if offset + 4 > len(data):
                break
            prop_size = struct.unpack("<I", data[offset : offset + 4])[0]
            offset += 4

        # Sanity check size
        if prop_size > len(data) - offset:
            break

        # Validate type/size combination
        if not validate_property_type_size(prop_type, prop_size):
            break

        # Skip struct name for struct types
        if prop_type == 10:
            struct_idx, offset = read_compact_index(data, offset)
            if struct_idx is None:
                break

        # Skip array index
        if array_flag and prop_type != 3:
            if offset >= len(data):
                break
            offset += 1

        # Skip value
        offset += prop_size
        seen_props += 1
        score += 1

    return score


def parse_properties(data: bytes, names: list, start_offset: int) -> list:
    """Parse all properties from the given offset.
    
    IMPORTANT: This function continues parsing past 'None' terminators to capture
    secondary property lists (e.g., parent class properties like Location, Rotation).
    It only stops when it encounters two consecutive Nones or reaches end of data.
    """
    properties = []
    offset = start_offset
    consecutive_nones = 0
    current_list = 0  # Track which property list we're in (0=first, 1=second, etc.)

    while offset < len(data) - 1 and len(properties) < 200:
        prop_start = offset

        # Read property name index
        name_idx, new_offset = read_compact_index(data, offset)

        if name_idx is None or name_idx < 0 or name_idx >= len(names):
            break

        prop_name = names[name_idx]

        if prop_name == "None":
            consecutive_nones += 1
            if consecutive_nones >= 2:
                # Two consecutive Nones = truly done with all property lists
                break
            # Move past this None and continue to next property list
            offset = new_offset
            current_list += 1
            continue

        # Reset consecutive counter since we found a real property
        consecutive_nones = 0

        offset = new_offset
        if offset >= len(data):
            break

        # Read info byte
        info_byte = data[offset]
        offset += 1

        prop_type = info_byte & 0x0F
        size_type = (info_byte >> 4) & 0x07
        array_flag = (info_byte >> 7) & 0x01

        # Validate property name vs type - certain names should never be String/Str
        if prop_type in (7, 13) and prop_name in NON_STRING_PROP_NAMES:
            break

        # Validate property name vs type - certain names should never be Name
        if prop_type == 6 and prop_name in NON_NAME_PROP_NAMES:
            break

        # Calculate property size
        prop_size = 0
        if size_type == 0:
            prop_size = 1
        elif size_type == 1:
            prop_size = 2
        elif size_type == 2:
            prop_size = 4
        elif size_type == 3:
            prop_size = 12
        elif size_type == 4:
            prop_size = 16
        elif size_type == 5:
            if offset >= len(data):
                break
            prop_size = data[offset]
            offset += 1
        elif size_type == 6:
            if offset + 2 > len(data):
                break
            prop_size = struct.unpack("<H", data[offset : offset + 2])[0]
            offset += 2
        elif size_type == 7:
            if offset + 4 > len(data):
                break
            prop_size = struct.unpack("<I", data[offset : offset + 4])[0]
            offset += 4

        # Validate type/size combination - if invalid, parser is out of sync
        if not validate_property_type_size(prop_type, prop_size):
            break

        # Sanity check: prop_size should be reasonable (< 10MB)
        if prop_size > 10_000_000:
            break

        # Read struct name for struct types
        struct_name = None
        if prop_type == 10:
            struct_idx, offset = read_compact_index(data, offset)
            if struct_idx is not None and 0 <= struct_idx < len(names):
                struct_name = names[struct_idx]
                # Validate struct name - reject known invalid names and object-like names
                if struct_name in INVALID_STRUCT_NAMES:
                    break
                if struct_name and len(struct_name) > 3 and struct_name[-1].isdigit():
                    break

        # Read array index
        array_index = 0
        if array_flag and prop_type != 3:
            if offset < len(data):
                array_index = data[offset]
                offset += 1

        # Read and parse value
        value = None
        value_hex = None

        if offset + prop_size <= len(data):
            value_bytes = data[offset : offset + prop_size]
            value_hex = value_bytes.hex()

            # Parse value based on type
            try:
                if prop_type == 1:  # Byte
                    value = value_bytes[0] if value_bytes else None
                elif prop_type == 2:  # Int
                    if len(value_bytes) >= 4:
                        value = struct.unpack("<i", value_bytes[:4])[0]
                elif prop_type == 3:  # Bool
                    value = bool(array_flag)  # Bool value is in array flag bit
                elif prop_type == 4:  # Float
                    if len(value_bytes) >= 4:
                        value = round(struct.unpack("<f", value_bytes[:4])[0], 6)
                elif prop_type == 5 or prop_type == 8:  # Object/Class reference
                    obj_idx, _ = read_compact_index(value_bytes, 0)
                    value = obj_idx
                elif prop_type == 6:  # Name reference
                    name_ref, _ = read_compact_index(value_bytes, 0)
                    if name_ref is not None and 0 <= name_ref < len(names):
                        value = names[name_ref]
                elif prop_type == 7 or prop_type == 13:  # String/Str
                    # FString: compact_index length + chars + null terminator
                    if len(value_bytes) >= 1:
                        str_len, str_start = read_compact_index(value_bytes, 0)
                        if (
                            str_len is not None
                            and str_len > 0
                            and str_start + str_len <= len(value_bytes)
                        ):
                            # String includes null terminator, so decode str_len-1 chars
                            str_data = value_bytes[str_start : str_start + str_len - 1]
                            decoded = str_data.decode("latin-1", errors="replace")
                            # Validate: at least 50% printable ASCII (or empty)
                            if (
                                len(decoded) == 0
                                or sum(32 <= ord(c) <= 126 for c in decoded)
                                >= len(decoded) * 0.5
                            ):
                                value = decoded
                elif prop_type == 9:  # Array
                    value = parse_array_value(value_bytes, prop_name, struct_name)
                elif prop_type == 10:  # Struct
                    value = parse_struct_value(value_bytes, struct_name, names, 0)
                elif prop_type == 11:  # Vector
                    if len(value_bytes) >= 12:
                        x, y, z = struct.unpack("<fff", value_bytes[:12])
                        value = json.dumps(
                            {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}
                        )
                elif prop_type == 12:  # Rotator
                    if len(value_bytes) >= 12:
                        pitch, yaw, roll = struct.unpack("<iii", value_bytes[:12])
                        value = json.dumps({"pitch": pitch, "yaw": yaw, "roll": roll})
            except:
                pass

            offset += prop_size

        properties.append(
            {
                "name": prop_name,
                "type": PROP_TYPES.get(prop_type, f"Unknown({prop_type})"),
                "size": prop_size,
                "array_index": array_index,
                "struct_name": struct_name,
                "value": value,
                "value_hex": value_hex,
                "list_index": current_list,  # Track which property list this came from
            }
        )

    return properties



def init_database(conn):
    """Ensure properties table exists with proper schema."""
    conn.executescript(
        """
        DROP TABLE IF EXISTS properties;
        
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY,
            export_id INTEGER NOT NULL,
            prop_name TEXT NOT NULL,
            prop_type TEXT,
            prop_size INTEGER,
            array_index INTEGER DEFAULT 0,
            struct_name TEXT,
            value_text TEXT,
            value_hex TEXT,
            FOREIGN KEY (export_id) REFERENCES exports(id)
        );
        
        CREATE INDEX idx_properties_export ON properties(export_id);
        CREATE INDEX idx_properties_name ON properties(prop_name);
        CREATE INDEX idx_properties_type ON properties(prop_type);
    """
    )
    conn.commit()


def parse_chunk(conn, chunk_id: int, chunk_path: str, class_filter: str = None) -> dict:
    """Parse all exports in a chunk."""
    stats = {"exports": 0, "properties": 0, "failed": 0}

    try:
        pkg = UE2Package(chunk_path)
    except Exception as e:
        return stats

    # Get exports for this chunk
    if class_filter:
        cursor = conn.execute(
            """
            SELECT id, export_index, class_name 
            FROM exports 
            WHERE chunk_id = ? AND class_name = ?
        """,
            (chunk_id, class_filter),
        )
    else:
        cursor = conn.execute(
            """
            SELECT id, export_index, class_name 
            FROM exports 
            WHERE chunk_id = ?
        """,
            (chunk_id,),
        )

    exports = cursor.fetchall()

    for exp_id, exp_index, class_name in exports:
        # Get the export
        exp = next((e for e in pkg.exports if e.get("index") == exp_index), None)
        if not exp:
            continue

        data = pkg.get_export_data(exp)
        if not data or len(data) < 2:
            continue

        stats["exports"] += 1

        # Find property start
        start_offset = find_property_start(data, pkg.names)

        if start_offset < 0:
            stats["failed"] += 1
            continue

        # Parse properties
        properties = parse_properties(data, pkg.names, start_offset)

        # Insert properties
        for prop in properties:
            value_text = None
            if prop["value"] is not None:
                value_text = str(prop["value"])

            conn.execute(
                """
                INSERT INTO properties (export_id, prop_name, prop_type, prop_size, 
                                        array_index, struct_name, value_text, value_hex)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    exp_id,
                    prop["name"],
                    prop["type"],
                    prop["size"],
                    prop["array_index"],
                    prop["struct_name"],
                    value_text,
                    prop["value_hex"],
                ),
            )
            stats["properties"] += 1

    conn.commit()
    return stats


def show_stats(conn):
    """Show parsing statistics."""
    print("\n" + "=" * 60)
    print("PROPERTY PARSING STATISTICS")
    print("=" * 60)

    # Total properties
    total = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    print(f"\nTotal properties parsed: {total:,}")

    # By class
    print("\nProperties by class (top 20):")
    cursor = conn.execute(
        """
        SELECT e.class_name, COUNT(p.id) as prop_count, COUNT(DISTINCT e.id) as export_count
        FROM properties p
        JOIN exports e ON p.export_id = e.id
        GROUP BY e.class_name
        ORDER BY prop_count DESC
        LIMIT 20
    """
    )
    for row in cursor:
        print(f"  {row[0]}: {row[1]:,} properties from {row[2]:,} exports")

    # By property name
    print("\nMost common properties (top 20):")
    cursor = conn.execute(
        """
        SELECT prop_name, prop_type, COUNT(*) as count
        FROM properties
        GROUP BY prop_name, prop_type
        ORDER BY count DESC
        LIMIT 20
    """
    )
    for row in cursor:
        print(f"  {row[0]} ({row[1]}): {row[2]:,}")

    # Classes with no properties
    print("\nClasses with no parsed properties:")
    cursor = conn.execute(
        """
        SELECT e.class_name, COUNT(DISTINCT e.id) as export_count
        FROM exports e
        LEFT JOIN properties p ON e.id = p.export_id
        WHERE p.id IS NULL
        GROUP BY e.class_name
        ORDER BY export_count DESC
        LIMIT 10
    """
    )
    for row in cursor:
        print(f"  {row[0]}: {row[1]:,} exports")


def print_progress_bar(iteration, total, prefix='', suffix='', decimals=1, length=40, fill='█', print_end="\r"):
    """
    Call in a loop to create terminal progress bar
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filled_length = int(length * iteration // total)
    bar = fill * filled_length + '-' * (length - filled_length)
    print(f'\r{prefix} |{bar}| {percent}% {suffix}', end=print_end)
    if iteration == total: 
        print()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Universal UE2 Property Parser")
    parser.add_argument("--stats", action="store_true", help="Show parsing statistics")
    parser.add_argument(
        "--class", dest="class_filter", help="Only parse specific class"
    )
    parser.add_argument("--silent", action="store_true", help="Suppress all output except errors")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.stats:
        show_stats(conn)
        conn.close()
        return

    if not args.silent:
        print("=" * 60)
        print("UNIVERSAL PROPERTY PARSER")
        print("=" * 60)

    # Initialize fresh properties table
    if not args.silent:
        print("\nInitializing properties table...")
    init_database(conn)

    # Get all chunks
    chunks = conn.execute("SELECT id, filename, filepath FROM chunks").fetchall()
    if not args.silent:
        print(f"Processing {len(chunks)} chunks...")

    total_stats = {"exports": 0, "properties": 0, "failed": 0}

    total_chunks = len(chunks)
    for i, (chunk_id, filename, filepath) in enumerate(chunks):
        stats = parse_chunk(conn, chunk_id, filepath, args.class_filter)
        total_stats["exports"] += stats["exports"]
        total_stats["properties"] += stats["properties"]
        total_stats["failed"] += stats["failed"]
        
        print_progress_bar(i + 1, total_chunks, prefix='   Progress:', suffix=f'({i+1}/{total_chunks})', length=40)

    conn.close()

    if not args.silent:
        print("\n" + "=" * 60)
        print("PARSING COMPLETE")
        print("=" * 60)
        print(f"Exports processed: {total_stats['exports']:,}")
        print(f"Properties parsed: {total_stats['properties']:,}")
        print(f"Failed to parse: {total_stats['failed']:,}")

        # Show stats
        conn = sqlite3.connect(DB_PATH)
        show_stats(conn)
        conn.close()


if __name__ == "__main__":
    main()
