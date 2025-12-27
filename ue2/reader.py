"""
Binary Reader for UE2 files.

Provides low-level binary reading utilities with UE2 format support,
including compact index reading and length-prefixed strings.
"""

import struct
from typing import Optional, Callable, List
from .types import Vector, Plane


class BinaryReader:
    """Binary data reader with UE2 format support.
    
    Provides methods for reading primitive types and UE2-specific formats
    like compact indices and FStrings.
    """

    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.pos = offset

    def seek(self, pos: int):
        """Seek to absolute position."""
        self.pos = pos

    def tell(self) -> int:
        """Return current position."""
        return self.pos

    def remaining(self) -> int:
        """Return remaining bytes."""
        return len(self.data) - self.pos

    def read_bytes(self, count: int) -> bytes:
        """Read raw bytes."""
        result = self.data[self.pos : self.pos + count]
        self.pos += count
        return result

    def read_int8(self) -> int:
        return struct.unpack("<b", self.read_bytes(1))[0]

    def read_uint8(self) -> int:
        return struct.unpack("<B", self.read_bytes(1))[0]

    def read_int16(self) -> int:
        return struct.unpack("<h", self.read_bytes(2))[0]

    def read_uint16(self) -> int:
        return struct.unpack("<H", self.read_bytes(2))[0]

    def read_int32(self) -> int:
        return struct.unpack("<i", self.read_bytes(4))[0]

    def read_uint32(self) -> int:
        return struct.unpack("<I", self.read_bytes(4))[0]

    def read_int64(self) -> int:
        return struct.unpack("<q", self.read_bytes(8))[0]

    def read_uint64(self) -> int:
        return struct.unpack("<Q", self.read_bytes(8))[0]

    def read_float(self) -> float:
        return struct.unpack("<f", self.read_bytes(4))[0]

    def read_compact_index(self) -> int:
        """Read UE2 compact index (variable-length integer).
        
        This is UE2's space-efficient integer encoding:
        - Bit 7 of first byte: sign
        - Bit 6 of first byte: continuation flag
        - Bits 0-5: 6 bits of value
        - Subsequent bytes: 7 bits of value + continuation flag
        """
        b0 = self.read_uint8()
        negative = b0 & 0x80
        value = b0 & 0x3F

        if b0 & 0x40:
            b1 = self.read_uint8()
            value |= (b1 & 0x7F) << 6

            if b1 & 0x80:
                b2 = self.read_uint8()
                value |= (b2 & 0x7F) << 13

                if b2 & 0x80:
                    b3 = self.read_uint8()
                    value |= (b3 & 0x7F) << 20

                    if b3 & 0x80:
                        b4 = self.read_uint8()
                        value |= b4 << 27

        return -value if negative else value

    def read_fstring(self) -> str:
        """Read UE2 length-prefixed string (FString).
        
        Format:
        - Compact index for length (negative = Unicode)
        - String data (ASCII or UTF-16LE)
        """
        length = self.read_compact_index()
        if length < 0:
            # Unicode string (UTF-16LE)
            length = -length
            result = (
                self.read_bytes(length * 2)
                .decode("utf-16-le", errors="replace")
                .rstrip("\x00")
            )
        elif length > 0:
            # ASCII/Latin-1 string
            result = (
                self.read_bytes(length)
                .decode("latin-1", errors="replace")
                .rstrip("\x00")
            )
        else:
            result = ""
        return result

    def read_vector(self) -> Vector:
        """Read a 3D vector (3 floats)."""
        return Vector(
            x=self.read_float(),
            y=self.read_float(),
            z=self.read_float()
        )

    def read_plane(self) -> Plane:
        """Read a plane (4 floats: normal + distance)."""
        return Plane(
            x=self.read_float(),
            y=self.read_float(),
            z=self.read_float(),
            w=self.read_float(),
        )

    def read_tarray(self, read_func: Callable, count: Optional[int] = None) -> List:
        """Read a TArray (count-prefixed array).
        
        Args:
            read_func: Function to read each element
            count: Optional override for array count (otherwise read from stream)
        """
        if count is None:
            count = self.read_compact_index()
        return [read_func() for _ in range(count)]


def read_compact_index_at(data: bytes, offset: int) -> tuple[int, int]:
    """Read a compact index from bytes at given offset.
    
    Standalone function for cases where a BinaryReader isn't used.
    
    Args:
        data: Raw bytes
        offset: Starting offset
        
    Returns:
        (value, new_offset) tuple
    """
    if offset >= len(data):
        raise IndexError("Offset beyond data length")
    
    b0 = data[offset]
    negative = b0 & 0x80
    value = b0 & 0x3F
    pos = offset + 1

    if b0 & 0x40:
        if pos >= len(data):
            raise IndexError("Unexpected end of data in compact index")
        b1 = data[pos]
        value |= (b1 & 0x7F) << 6
        pos += 1

        if b1 & 0x80:
            if pos >= len(data):
                raise IndexError("Unexpected end of data in compact index")
            b2 = data[pos]
            value |= (b2 & 0x7F) << 13
            pos += 1

            if b2 & 0x80:
                if pos >= len(data):
                    raise IndexError("Unexpected end of data in compact index")
                b3 = data[pos]
                value |= (b3 & 0x7F) << 20
                pos += 1

                if b3 & 0x80:
                    if pos >= len(data):
                        raise IndexError("Unexpected end of data in compact index")
                    b4 = data[pos]
                    value |= b4 << 27
                    pos += 1

    return (-value if negative else value), pos


def read_fstring_at(data: bytes, offset: int) -> tuple[str, int]:
    """Read an FString from bytes at given offset.
    
    Standalone function for cases where a BinaryReader isn't used.
    
    Args:
        data: Raw bytes
        offset: Starting offset
        
    Returns:
        (string, new_offset) tuple
    """
    length, pos = read_compact_index_at(data, offset)
    
    if length < 0:
        # Unicode
        length = -length
        end = pos + length * 2
        result = data[pos:end].decode("utf-16-le", errors="replace").rstrip("\x00")
        return result, end
    elif length > 0:
        end = pos + length
        result = data[pos:end].decode("latin-1", errors="replace").rstrip("\x00")
        return result, end
    else:
        return "", pos
