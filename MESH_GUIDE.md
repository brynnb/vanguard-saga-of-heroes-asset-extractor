# Vanguard StaticMesh Specification

This document details the binary structure and extraction process for Vanguard StaticMeshes (`.usx` files).

## 1. Serialization Overview
Vanguard uses a heavily modified version of the Unreal Engine 2.5 StaticMesh format. Unlike standard UE2 meshes, Vanguard meshes rely on custom "LODModels" blocks and often skip standard stream sections entirely using a `TLazyArray` logic.

### Key Parsing Discovery: Adaptive Robustness
Due to significant variance in file headers (different property blocks, variable padding, misaligned offsets), we use a multi-layered adaptive strategy:
1.  **Property Boundary Probing**: We detect if property sizes are "Inclusive" (Vanguard-specific) or "Exclusive" (UE2 standard) by probing both potential offsets for the next valid property start.
2.  **Property Priority**: We prioritize a non-zero property start over a `None` (0x00) terminator if both offsets look valid, preventing premature termination.
3.  **Anchor-Based Scanning**: We scan for the `InternalVersion` signature with variable gaps (40-52 bytes) after the property block.
4.  **Forward-Only Skipping**: RawTriangles skip pointers must point forward to avoid loops.

## 2. Binary Layout

| Section | Type | Strategy |
|---------|------|----------|
| **Properties** | UObject | Parsed up to `None`, but often misaligned. |
| **Core Anchor** | Start | **We scan for InternalVersion here.** |
| **Bounds** | FBox + FSphere | **FBox is 24 bytes** (Vanguard-specific, no `IsValid` flag). |
| **InternalVersion** | int32 | Known versions: `11`, `12`, `128`, `129`, `236`, `60482`, `60484`, `60485`. |
| **Sections** | TArray | `FStaticMeshSection` is 24 bytes. |
| **Streams** | TArray | Often empty. |
| **RawTriangles** | TLazyArray | **We scan for 6-byte null padding** to find the absolute skip pointer. |
| **LODModels** | Custom | The primary geometry source. Follows the RawTriangles skip. |

## 3. Detailed Data Structures

### StaticMesh Core
Found by scanning for `InternalVersion` after properties.
- **Variable Gap**: The gap between properties and `InternalVersion` can be 40, 41, 44, 45, 48, or 52 bytes depending on the mesh complexity.
- `FBox` (24 bytes): Min(Vector), Max(Vector). **Note**: Standard UE2 is 25 bytes; Vanguard is 24.
- `FSphere` (16 bytes): 3 floats + radius.
- `InternalVersion` (4 bytes): Key anchor.
- `SectionCount` (4 bytes).
- `Sections`: Array of 24-byte structs.

### RawTriangles Skip Logic
Vanguard uses a `TLazyArray` for raw triangle data, which usually just contains a skip pointer.
- **Pattern**: `00 00 00 00 00 00 [AbsoluteOffset]`
- We scan the file (using `data.find`) for this 6-byte padding to locate the geometry block reliably.

### LODModels (The Geometry Block)
Located at the `RawTriangles` skip target.
- `PhysicsRef` (4 bytes)
- `AuthKey` (4 bytes)
- `LODVersion` (4 bytes)
- `LODCount` (4 bytes)

**Observation**: Some meshes claim `LODCount=2` but include trailing garbage data as the second LOD. The parser validates each LOD's vertex count against remaining file space and gracefully breaks when data is insufficient.

#### LOD Header Variants
Each LOD has a header before vertex data. This varies by mesh version!
1.  **Standard (12 bytes)**: `[SecCount, Unk1, VertexCount]`
2.  **Extended (16 bytes)**: `[SecCount, Unk1, Unk2, VertexCount]`

**Detection**: We heuristic probe the 12-byte and 16-byte offsets. If the presumed `VertexCount` is > 100,000 or 0, we assume the other format.

#### Vertex Data
- **Size**: 56 bytes per vertex.
- **Layout**:
  - `Position` (3 floats)
  - `Normal` (3 floats)
  - `TangentX` (3 floats)
  - `TangentY` (3 floats)
  - `UV` (2 floats)

#### Index Data (Triangle List)
Located *after* vertices, but separated by a variable-length header (padding/spheres).
- **Probing**: We scan 10-60 bytes after the vertex block for a valid `IndexCount` (3 to 1,000,000) followed by a valid `FirstIndex` (< VertexCount).
- **Format**: `uint16` indices.

## 4. Extraction Pipeline
The parser (`scripts/lib/staticmesh_construct.py`) uses a "Survive and Report" strategy:
1.  **Scan** for Core Anchor.
2.  **Scan** for RawTriangles padding.
3.  **Jump** to LOD block.
4.  **Probe** LOD headers and Index headers.
5.  **Capture** any remaining bytes as "Unknown Regions" for future analysis.

### Known Artifacts
- **"Teleporting" Meshes**: Caused by reading header bytes as float positions. Filtering `abs(pos) > 500,000` catches this.
- **Missing Faces**: Usually due to failing to find the Index Buffer offset correctly.

## 5. Setup & Usage
Use `setup.py` specific flags for rapid testing:
- `python3 setup.py --meshes --limit 50`: Test parser on 50 meshes.
- `python3 setup.py --full`: Run full extraction.
