"""
UE2 Texture Parser - Sequential Property & Mipmap Decoder.

This module provides a formal parser for UE2 Texture exports, 
replacing heuristic foot-scanning with sequential binary parsing.
"""

import struct
from typing import Dict, List, Optional
from PIL import Image
from .reader import BinaryReader
from .properties import find_property_start, parse_properties, read_compact_index

class Mipmap:
    """Represents a single mipmap level in a Texture export."""
    def __init__(self, width: int, height: int, data: bytes, format_id: int):
        self.width = width
        self.height = height
        self.data = data
        self.format_id = format_id

class Texture:
    """Sequential parser for UE2 Texture exports."""
    
    def __init__(self, data: bytes, names: List[str]):
        self.data = data
        self.names = names
        self.reader = BinaryReader(data)
        self.properties = {}
        self.mips: List[Mipmap] = []
        
        # Detected properties
        self.u_size = 0
        self.v_size = 0
        self.format_id = 0
        
        self.parse()

    def parse(self):
        """Perform sequential parse of the texture data."""
        # 1. Detect property start and parse them
        start_off = find_property_start(self.data, self.names)
        if start_off < 0:
            return
            
        self.reader.seek(start_off)
        parsed_props = parse_properties(self.data, self.names, start_off)
        for p in parsed_props:
            self.properties[p['name']] = p['value']
            
        self.properties_end = self._find_none_terminator(start_off)
        
        # Extract metadata from properties
        self.u_size = self.properties.get('USize', 0)
        self.v_size = self.properties.get('VSize', 0)
        self.format_id = self.properties.get('Format', 0)
        
        # 2. Find Mip Block using Content-Aware Marker Selection
        # Vanguard injects a 69-byte metadata block with a False Marker followed by zeros.
        # The True Marker is the one whose payload is NOT all zeros.
        expected_size = 0
        if self.format_id in (7, 6, 63): # DXT5
            expected_size = self.u_size * self.v_size
        elif self.format_id == 3: # DXT1
            expected_size = (self.u_size * self.v_size) // 2
        elif self.format_id == 5: # RGBA8
            expected_size = self.u_size * self.v_size * 4
        elif self.format_id == 10: # G16 (Terrain Heightmap)
            expected_size = self.u_size * self.v_size * 2
            
        data_pos = -1
        if expected_size > 0:
            marker_bytes = struct.pack('<I', expected_size)
            # Find all markers in the header area (first 500 bytes)
            search_end = min(len(self.data), 500)
            
            # Find all candidate markers
            off = self.properties_end
            markers = []
            while True:
                off = self.data.find(marker_bytes, off, search_end)
                if off == -1: break
                markers.append(off)
                off += 1
            
            # Strategy: Use LAST marker with valid (non-zero) payload
            # For Vanguard textures, there are multiple false markers followed by the true one
            # The true marker is always the LAST one in the header section
            for m in reversed(markers):
                payload_start = m + 4
                payload_peek = self.data[payload_start:payload_start+8]
                # Skip if payload is all zeros
                if payload_peek == b'\x00' * 8:
                    continue
                data_pos = m
                break
            
            # FALLBACK: Tail-Guided Anchor (for single-mip textures like heightmaps)
            if data_pos == -1:
                true_pos = len(self.data) - 10 - expected_size - 4
                if true_pos >= self.properties_end:
                    marker_val = struct.unpack('<I', self.data[true_pos:true_pos+4])[0]
                    if marker_val == expected_size:
                        data_pos = true_pos
                
        if data_pos == -1:
            # Fallback for standard UE2 archives
            self.reader.seek(self.properties_end)
            if self.reader.remaining() >= 8:
                try:
                    _skip = self.reader.read_uint32()
                    mip_count = self.reader.read_uint32()
                    if 0 < mip_count <= 64:
                        data_pos = self.reader.tell()
                except:
                    pass
        
        if data_pos == -1:
            return # Block cannot be resolved uniquely
            
        self.reader.seek(data_pos)
        
        # 4. Read Mipmaps sequentially
        while self.reader.remaining() > 10:
            try:
                current_size = self.reader.read_int32()
                if current_size <= 0 or current_size > self.reader.remaining():
                    break
                    
                mip_data = self.reader.read_bytes(current_size)
                
                # Vanguard dimensions footer: [Width:4][Height:4][UBits:1][VBits:1]
                if self.reader.remaining() >= 10:
                    width = self.reader.read_int32()
                    height = self.reader.read_int32()
                    ubits = self.reader.read_uint8()
                    vbits = self.reader.read_uint8()
                    
                    if 1 <= width <= 8192 and 1 <= height <= 8192:
                         self.mips.append(Mipmap(width, height, mip_data, self.format_id))
                         continue 
                         
                # Fallback to calculated dimensions
                w = max(1, self.u_size >> len(self.mips))
                h = max(1, self.v_size >> len(self.mips))
                self.mips.append(Mipmap(w, h, mip_data, self.format_id))
            except:
                break

    def _find_none_terminator(self, start_pos: int) -> int:
        """Sequential property mapper to find the exact end of property list."""
        pos = start_pos
        while pos < len(self.data):
            try:
                # Safety check for remaining bytes
                if pos >= len(self.data): break
                name_idx, next_pos = read_compact_index(self.data, pos)
            except:
                break
                
            pos = next_pos
            if name_idx == 0: # 'None'
                return pos
                
            if name_idx < 0 or name_idx >= len(self.names):
                break
                
            # info byte
            if pos >= len(self.data): break
            info = self.data[pos]
            pos += 1
            
            prop_type = info & 0x0F
            size_type = (info >> 4) & 0x07
            is_array = (info & 0x80) != 0
            
            # Struct name
            if prop_type == 10: # StructProperty
                if pos >= len(self.data): break
                _, pos = read_compact_index(self.data, pos)
                
            # Determine size
            size = 0
            if prop_type == 3: # Bool
                size = 0
            elif size_type == 0: size = 1
            elif size_type == 1: size = 2
            elif size_type == 2: size = 4
            elif size_type == 3: size = 12
            elif size_type == 4: size = 16
            elif size_type == 5:
                if pos >= len(self.data): break
                size = self.data[pos]
                pos += 1
            elif size_type == 6:
                if pos + 2 > len(self.data): break
                size = struct.unpack("<H", self.data[pos:pos+2])[0]
                pos += 2
            elif size_type == 7:
                if pos + 4 > len(self.data): break
                size = struct.unpack("<I", self.data[pos:pos+4])[0]
                pos += 4
            
            # Array index
            if is_array and prop_type != 3:
                pos += 1
                
            pos += size
            
        return pos
        
    def get_image(self, mip_index: int = 0) -> Optional[Image.Image]:
        """Decode a mipmap to a PIL Image."""
        if mip_index >= len(self.mips):
            return None
            
        mip = self.mips[mip_index]
        # Avoid circular import
        from scripts.extractors.extract_all_terrain import decode_dxt5, decode_dxt1
        
        if mip.format_id in (7, 6, 63):
            return decode_dxt5(mip.data, mip.width, mip.height)
        elif mip.format_id == 3:
            return decode_dxt1(mip.data, mip.width, mip.height)
        elif mip.format_id == 5:
            # Format 5 in Vanguard is BGRA8 (B=byte 0, G=byte 1, R=byte 2, A=byte 3)
            if len(mip.data) < mip.width * mip.height * 4:
                return None
            img = Image.frombytes("RGBA", (mip.width, mip.height), mip.data, "raw", "BGRA")
            return img
        return None
