import sys
import os
import struct

# Helper imports
sys.path.append(os.getcwd())
try:
    from ue2 import UE2Package
except ImportError:
    sys.path.append(os.path.join(os.getcwd(), ".."))
    from ue2 import UE2Package

class StrictSunParser:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read_float(self):
        v = struct.unpack('<f', self.data[self.pos:self.pos+4])[0]
        self.pos += 4
        return v
    
    def read_byte(self):
        v = self.data[self.pos]
        self.pos += 1
        return v
        
    def read_int(self):
        v = struct.unpack('<i', self.data[self.pos:self.pos+4])[0]
        self.pos += 4
        return v
        
    def read_uint16(self):
        v = struct.unpack('<H', self.data[self.pos:self.pos+2])[0]
        self.pos += 2
        return v

    def parse(self):
        print(f"Total Data Size: {len(self.data)}")
        
        # 1. Properties (Handled by existing logic, but let's emulate skipping it)
        # In this file, properties end at 24.
        self.pos = 24 
        print(f"Properties End: {self.pos}")

        # 2. BoundingBox
        # FBox: Min(Vec), Max(Vec), IsValid(byte) => 12 + 12 + 1 = 25 bytes
        min_x = self.read_float()
        min_y = self.read_float()
        min_z = self.read_float()
        max_x = self.read_float()
        max_y = self.read_float()
        max_z = self.read_float()
        is_valid = self.read_byte()
        
        print(f"BoundingBox: Min({min_x}, {min_y}, {min_z}) Max({max_x}, {max_y}, {max_z}) Valid({is_valid})")
        print(f"Current Pos: {self.pos}")
        
        # 3. BoundingSphere?
        # FSphere: Center(Vec), W(float) => 16 bytes.
        # Let's peek
        try:
            sx = self.read_float()
            sy = self.read_float()
            sz = self.read_float()
            sr = self.read_float()
            print(f"BoundingSphere: Center({sx}, {sy}, {sz}) Radius({sr})")
        except:
            print("Failed to read sphere")
            
        print(f"Current Pos: {self.pos}")
        
        # 4. Internal Version?
        # UE2 usually: int BodySetup/Convex?; int InternalVersion?
        # Let's peek
        v1 = self.read_int() # InternalVersion?
        v2 = self.read_int() # Section Count?
        
        print(f"InternalVersion: {v1}")
        print(f"Section Count: {v2}")
        
        # Determine Section Size
        # If standard UE2: 14 bytes?
        # int f4; word FirstIndex; word FirstVertex; word LastVertex; word fe; word numFaces;
        
        SECT_SIZE = 14
        for i in range(v2):
             # Just skip bytes for now, or read strictly?
             # Let's read strictly to confirm 0s
             f0 = self.read_int() # Should be 0?
             w1 = self.read_byte(); w2 = self.read_byte() # uint16
             w3 = self.read_byte(); w4 = self.read_byte()
             w5 = self.read_byte(); w6 = self.read_byte()
             w7 = self.read_byte(); w8 = self.read_byte()
             w9 = self.read_byte(); w10 = self.read_byte()
             
             print(f"  Section {i}: StartInt={f0}")
             
        print(f"Pos after sections: {self.pos}")

        
        # 6. Collision Primitives?
        # Usually:
        # CollisionBox (25 bytes)
        # CollisionSphere (16 bytes)
        # CollisionTriangles (TArray)
        # BSP Nodes?
        
        # Let's try reading them.
        # But we already read p1, p2, p3 (12 bytes).
        # If p1 is MinX, p2 is MinY...
        # If they are 0, might be empty box?
        # Let's reset pos and read strictly.
        self.pos -= 12
        
        # CollisionBox
        cx_min = self.read_float()
        cy_min = self.read_float()
        cz_min = self.read_float()
        cx_max = self.read_float()
        cy_max = self.read_float()
        cz_max = self.read_float()
        c_valid = self.read_byte()
        print(f"CollisionBox: {cx_min},{cy_min}.. Valid={c_valid}")
        
        # CollisionSphere
        sx = self.read_float()
        sy = self.read_float()
        sz = self.read_float()
        sr = self.read_float()
        print(f"CollisionSphere: {sx},{sy}.. R={sr}")
        
        # CollisionTriangles? (TArray)
        col_tris_count = self.read_int()
        print(f"CollisionTriangles Count: {col_tris_count}")
        
        # BSP Nodes? (TArray)
        bsp_nodes_count = self.read_int()
        print(f"BSP Nodes Count: {bsp_nodes_count}")
        
        # BSP Surfs? (TArray)
        bsp_surfs_count = self.read_int()
        print(f"BSP Surfs Count: {bsp_surfs_count}")
        
        # BSP Points? (TArray)
        bsp_pts_count = self.read_int()
        print(f"BSP Points Count: {bsp_pts_count}")
        
        # kDOP Tree? (TArray)
        kdop_count = self.read_int()
        print(f"kDOP Count: {kdop_count}")
        
        print(f"Current Pos: {self.pos}")
        
        # 7. Vertex Stream?
        # FStaticMeshVertexStream
        # int count;
        # struct { ... } vertex;
        
        vert_count = self.read_int()
        print(f"Vertex Count: {vert_count}")
        
        if vert_count > 0 and vert_count < 10000:
            # FStaticMeshVertexVanguard: Pos(12) + Normal(12) + 8 floats (32) = 56 bytes
            VERT_SIZE = 56
            
            # Read all verts
            for i in range(vert_count):
                x = self.read_float()
                y = self.read_float()
                z = self.read_float()
                
                # skip rest of vert
                self.pos += (VERT_SIZE - 12)
                
                if i == 0:
                     print(f"  Vert 0: {x}, {y}, {z}")
        
        # Revision exists regardless of count?
        # Typically yes for TArrays inside structs? No, TArray is just Count + Data.
        # But VertexStream is a Struct containing a TArray.
        # struct FStaticMeshVertexStream { TArray<Vert> Verts; int Revision; }
        
        rev = self.read_int()
        print(f"Vertex Stream Revision: {rev}")
            
        print(f"Pos after VertexStream: {self.pos}")

        # 8. UV Streams (TArray<FStaticMeshUVStream>)
        # struct FStaticMeshUVStream { TArray<FMeshUVFloat> Coords; int Revision; }
        # FMeshUVFloat = float U, V;
        
        uv_stream_count = self.read_int()
        print(f"UVStream Array Count: {uv_stream_count}")
        
        for i in range(uv_stream_count):
            # Read UV Stream
            coord_count = self.read_int()
            print(f"  UVStream[{i}] Coords: {coord_count}")
            if coord_count > 0:
                self.pos += coord_count * 8 # 2 floats
            uv_rev = self.read_int()
            print(f"  UVStream[{i}] Rev: {uv_rev}")

        # 9. Color Stream (Range?)
        # FColorStream { TArray<FColor> Colors; int Revision; }
        
        # ISSUE: 233728 is too big for count in a 938 byte file.
        # Maybe it's not ColorStream Count?
        # Maybe UVStream Count was the 0?
        # Maybe UVStream Revision was the next 0?
        # Maybe ColorStream Count matches "0" somewhere?
        
        # Let's assume we read:
        # VertexStream Rev (0)
        # UVStream Count (0)
        # ColorStream Count? (233728?? IMPOSSIBLE)
        
        # Maybe the structure is:
        # UVStream (TArray) -> Count 0.
        # ColorStream (TArray) -> Count 0? (But we see 0x39100).
        
        # Let's peek hex again.
        # Hex at 148: 00 91 03 00
        
        # Let's just skip this "unknown int" and see what follows.
        unknown_val = self.read_int()
        print(f"Unknown Int at 148: {unknown_val}")
        
        # 10. Alpha Stream?
        # alpha_count = self.read_int()
        # print(f"AlphaStream Count (or next int): {alpha_count}")
        # if alpha_count > 0:
        #    pass
        # alpha_rev = self.read_int()
        # print(f"AlphaStream Rev: {alpha_rev}")

        # 11. LOD Models (TArray<FStaticMeshLODModel>)
        # lod_count = self.read_int()
        # print(f"LOD Model Count: {lod_count}")
        
        # REVISED PLAN:
        # We saw a '1' at offset ~200.
        # Current Pos: 152 (after Unknown Int).
        # Data at 164 was zeros.
        # Let's verify exactly how many zeros until the '1'.
        
        print("\nScanning for LOD Count (1)...")
        found_lod = False
        while self.pos < len(self.data) - 4:
            val = struct.unpack('<i', self.data[self.pos:self.pos+4])[0]
            if val == 1:
                print(f"Found Metadata/LOD Count '1' at {self.pos}")
                found_lod = True
                self.pos += 4
                break
            elif val != 0:
                 print(f"WARNING: Non-zero padding at {self.pos}: {val}")
            self.pos += 4
            
        if found_lod:
            # We are now inside the LOD Array? 
            # Or is '1' the count? Yes.
            
            # Start of FStaticMeshLODModel
            # struct FStaticMeshLODModel {
            #    TArray<FStaticMeshSection> Sections;
            #    FStaticMeshVertexStream VertexStream; (Wait, again?)
            #    ... Indices ...
            # }
            
            # 1. Sections (TArray)
            # Count
            lod_sect_count = self.read_int()
            print(f"LOD Section Count: {lod_sect_count}")
            
            # REVISED HYPOTHESIS:
            # The data at 380 (following count '1' at 372 is not Sections).
            # It's offsets!
            # 82 01 = 386.
            # 8d 03 = 909.
            
            # Let's read them as Unknown Header ints.
            # But wait, we read lod_sect_count as 1 at 376.
            # The next bytes were 82 01 8d 03.
            # This is inside the LODModel struct.
            
            # Maybe Vanguard `FStaticMeshLODModel` is:
            # int SectionCount;
            # int SectionsPtr; (or something?)
            
            # But `82 01 8d 03` is 4 bytes?  0x038d0182.
            # 386 and 909. Two 16-bit values?
            # Or two 32-bit values?
            
            # If 16-bit:
            # Short1: 386.
            # Short2: 909.
            
            # What is at 386?
            # Hex at 386 (from previous): 
            # 00 00 f0 c1 00 00 f0 c1 ... (-30.0f, -30.0f)
            # This looks like Vertex Data.
            
            # What is at 909?
            # 909 is near end of file. (File size 938).
            # 938 - 909 = 29 bytes.
            # Indices?
            
            # Let's verify.
            first_offset = self.read_uint16()
            second_offset = self.read_uint16()
            print(f"LOD Header Offsets?: {first_offset}, {second_offset}")
            
            # Now we are at 380 + 4 = 384.
            # Next 2 bytes to reach 386?
            padding = self.read_uint16()
            print(f"Padding at 384: {padding}")
            
            # Now at 386.
            print(f"Current Pos: {self.pos}")
            
            # Read Vertices (assuming first_offset pointed here)
            # How many verts?
            # We don't know count. The header had Section Count 1.
            # Usually LODModel has `FStaticMeshVertexStream`.
            
            # Let's just consume types until 909?
            # 909 - 386 = 523 bytes.
            # Vert Size = 12 bytes (Vanguard packed)? Or 56?
            # If -30, -30, 0 is a float vector, that's 12 bytes.
            # 523 / 12 = ~43.5 vertices.
            # 523 / 56 = ~9.3 vertices.
            
            # Let's read first few floats.
            v0x = self.read_float()
            v0y = self.read_float()
            v0z = self.read_float()
            print(f"Vert 0 at 386: {v0x}, {v0y}, {v0z}")
            
            # Let's jump to 909 and see what's there.
            self.pos = second_offset
            print(f"Jumped to {self.pos}")
            
            # Read bytes at 909
            rem_bytes = self.data[self.pos:self.pos+20]
            print(f"Bytes at 909: {rem_bytes.hex(' ')}")

def main():
    path = "/Users/brynnbateman/Downloads/Vanguard EMU/Assets/Meshes/P0001_Sun_Meshes.usx"
    pkg = UE2Package(path)
    # Get Sun Export
    exp = pkg.exports[1] # We know it's index 1
    data = pkg.get_export_data(exp)
    
    parser = StrictSunParser(data)
    parser.parse()

if __name__ == "__main__":
    main()
