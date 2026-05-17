"""
Python adaptation of UE Viewer's UE2 FPropertyTag serialization.

Source: UEViewer/Unreal/UnObject.cpp lines 571-627
This reads the UE2 property-tag layout used by Vanguard packages.
"""
import struct


# EPropType enum from UnObject.cpp:277
NAME_ByteProperty     = 1
NAME_IntProperty      = 2
NAME_BoolProperty     = 3
NAME_FloatProperty    = 4
NAME_ObjectProperty   = 5
NAME_NameProperty     = 6
NAME_StringProperty   = 7
NAME_ClassProperty    = 8
NAME_ArrayProperty    = 9
NAME_StructProperty   = 10
NAME_VectorProperty   = 11
NAME_RotatorProperty  = 12
NAME_StrProperty      = 13
NAME_MapProperty      = 14
NAME_FixedArrayProperty = 15


class BinaryReader:
    """Minimal binary reader for UE2 FArchive-style read patterns."""

    def __init__(self, data, offset=0):
        self.data = data
        self.pos = offset

    def read_byte(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def read_uint16(self):
        v = struct.unpack_from('<H', self.data, self.pos)[0]
        self.pos += 2
        return v

    def read_int32(self):
        v = struct.unpack_from('<i', self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_uint32(self):
        v = struct.unpack_from('<I', self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_float(self):
        v = struct.unpack_from('<f', self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_compact_index(self):
        """Read FArchive::SerializeCompactIndex / FCompactIndex encoding."""
        b0 = self.data[self.pos]; self.pos += 1
        neg = b0 & 0x80
        val = b0 & 0x3F
        if b0 & 0x40:
            b1 = self.data[self.pos]; self.pos += 1
            val |= (b1 & 0x7F) << 6
            if b1 & 0x80:
                b2 = self.data[self.pos]; self.pos += 1
                val |= (b2 & 0x7F) << 13
                if b2 & 0x80:
                    b3 = self.data[self.pos]; self.pos += 1
                    val |= (b3 & 0x7F) << 20
                    if b3 & 0x80:
                        b4 = self.data[self.pos]; self.pos += 1
                        val |= (b4 & 0x3F) << 27
        return -val if neg else val

    def skip(self, n):
        self.pos += n

    def seek(self, pos):
        self.pos = pos

    def tell(self):
        return self.pos


def skip_ue2_properties(reader, names):
    """
    Adaptation of CTypeInfo::SerializeUnrealProps + FPropertyTag::operator<< for UE2.

    Reads and skips all UE2 properties until the None terminator.
    After return, reader.pos is immediately past the None name index.

    Args:
        reader: BinaryReader positioned at the start of the property block
        names: list of name strings from the package name table

    Source: UEViewer/Unreal/UnObject.cpp lines 571-627 (FPropertyTag)
            UEViewer/Unreal/UnObject.cpp lines 978-988 (SerializeUnrealProps loop)
            UEViewer/Unreal/UnObject.cpp lines 1024-1043 (skip unknown property)
    """
    while True:
        # --- FPropertyTag::operator<< (UE1/UE2 path, line 456) ---

        # Ar << Tag.Name
        name_idx = reader.read_compact_index()
        name = names[name_idx] if 0 <= name_idx < len(names) else ""

        # if (!stricmp(Tag.Name, "None")) return Ar;  (line 457-458)
        if name.lower() == "none":
            return

        # byte info; Ar << info;  (line 572-573)
        info = reader.read_byte()

        is_array = (info & 0x80) != 0       # line 575
        prop_type = info & 0x0F             # line 577

        # if (Tag.Type == NAME_StructProperty) Ar << Tag.StrucName;  (line 580-581)
        if prop_type == NAME_StructProperty:
            reader.read_compact_index()  # StrucName (FName = compact index)

        # analyze 'size' field  (lines 584-595)
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
        elif size_type == 7:
            data_size = reader.read_int32()
        else:
            data_size = 0

        # Array index  (lines 597-618)
        if prop_type != NAME_BoolProperty and is_array:
            # read array index
            b = reader.read_byte()
            if b < 128:
                pass  # array_index = b
            else:
                b2 = reader.read_byte()
                if b & 0x40:  # really, (b & 0xC0) == 0xC0
                    reader.read_byte()  # b3
                    reader.read_byte()  # b4

        # BoolProperty: value is stored in IsArray flag, no data to skip  (lines 620-622)
        if prop_type == NAME_BoolProperty:
            data_size = 0

        # Skip property data  (line 1043: Ar.Seek(StopPos))
        reader.skip(data_size)


def read_ue2_properties(reader, names):
    """Read all UE2 properties and return them as a dict.

    Same tag parsing as skip_ue2_properties but captures property values.
    Returns dict mapping property name -> raw bytes (or decoded value for
    known types).  ArrayProperty values are returned as raw bytes for the
    caller to interpret based on context.
    """
    props = {}

    while True:
        name_idx = reader.read_compact_index()
        name = names[name_idx] if 0 <= name_idx < len(names) else ""

        if name.lower() == "none":
            return props

        info = reader.read_byte()
        is_array = (info & 0x80) != 0
        prop_type = info & 0x0F

        struct_name = ""
        if prop_type == NAME_StructProperty:
            si = reader.read_compact_index()
            struct_name = names[si] if 0 <= si < len(names) else ""

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
        elif size_type == 7:
            data_size = reader.read_int32()
        else:
            data_size = 0

        array_index = 0
        if prop_type != NAME_BoolProperty and is_array:
            b = reader.read_byte()
            if b < 128:
                array_index = b
            else:
                b2 = reader.read_byte()
                array_index = (b & 0x3F) | (b2 << 6)
                if b & 0x40:
                    b3 = reader.read_byte()
                    b4 = reader.read_byte()
                    array_index = (b & 0x3F) | (b2 << 6) | (b3 << 14) | (b4 << 22)

        if prop_type == NAME_BoolProperty:
            props[name] = is_array  # bool value stored in array flag
            continue

        raw = reader.data[reader.pos : reader.pos + data_size]
        reader.skip(data_size)

        if prop_type == NAME_FloatProperty and data_size == 4:
            import struct as _struct
            props[name] = _struct.unpack_from("<f", raw, 0)[0]
        elif prop_type == NAME_IntProperty and data_size == 4:
            import struct as _struct
            props[name] = _struct.unpack_from("<i", raw, 0)[0]
        else:
            props[name] = raw


def decode_animset_names(raw_bytes, names):
    """Decode an AnimSet ArrayProperty into a list of package name strings.

    The AnimSet property is an array of compact-index name references.
    Each element is a compact index into the package name table.

    Args:
        raw_bytes: Raw property data bytes from read_ue2_properties.
        names: Package name table list.

    Returns:
        List of animation package name strings (e.g. ['UEA_ant_M_shrd', ...]).
    """
    if not raw_bytes or len(raw_bytes) < 1:
        return []
    reader = BinaryReader(raw_bytes, 0)
    count = reader.read_compact_index()
    result = []
    for _ in range(count):
        if reader.pos >= len(raw_bytes):
            break
        ni = reader.read_compact_index()
        if 0 <= ni < len(names):
            result.append(names[ni])
    return result
