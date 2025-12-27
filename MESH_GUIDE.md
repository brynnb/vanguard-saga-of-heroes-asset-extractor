# Vanguard StaticMesh Specification

This document details the binary structure and extraction process for Vanguard StaticMeshes (`.usx` files).

## 1. Serialization Overview (Build 128/34)
Vanguard uses a custom extension of the Unreal Engine 2.5 StaticMesh format. 

### Key Discovery: The "LODModels" Extension
Standard UE2.5 StaticMeshes store geometry in the `VertexStream` and `IndexBuffer` sections. In Vanguard, these are often **empty** (Count = 0). Instead, the actual render-ready geometry is stored in a custom **LODModels** block appended at the end of the file.

## 2. Binary Layout
| Section | Type | Notes |
|---------|------|-------|
| **Properties** | UObject | Variable length terminator "None" |
| **Bounds** | FBox + FSphere | Standard UE2 |
| **InternalVersion** | int32 | Always 128/34 |
| **Sections** | TArray | Contains material index and triangle counts |
| **Streams** | TArray | (Usually empty in Vanguard) |
| **RawTriangles** | TLazyArray | Pre-computed triangles (usually skipped) |
| **LODModels** | Custom | **The primary data source** |

## 3. Detailed Data Structures

### FStaticMeshSection (14 bytes)
- `IsStrip` (4 bytes)
- `FirstIndex` (2 bytes)
- `MinVertexIndex` (2 bytes)
- `MaxVertexIndex` (2 bytes)
- `NumTriangles` (2 bytes)
- `NumPrimitives` (2 bytes)

### LODModels (The Geometry Block)
- `LODCount` (4 bytes)
- For each LOD:
  - `VertexCount` (4 bytes)
  - `Vertices` (**56 bytes per vertex**):
    - `Pos` (12), `Norm` (12), `TanX` (12), `TanY` (12), `UV` (8)
  - `IndexCount` (4 bytes)
  - `Indices` (uint16)

### StaticMesh Format Variants
We have identified two distinct StaticMesh binary formats sharing the same package version (128/34).

**Standard Format (Supported)**:
- Uses 14-byte `FStaticMeshSection`.
- Standard vertex/index streams are empty (`count=0`).
- Geometry data is stored in the custom Vanguard `LODModels` section.
- Post-vertex LOD structure: `val1, unk0, val2, unk1, val3, index_count, indices`.

**Variant Format (Work-In-Progress)**:
- Uses 24-byte `FStaticMeshSection` (6 x `int32`).
- Standard vertex/index streams are **populated** (non-zero counts).
- Often used for large meshes (armor, weapons, detailed buildings).
- **Detection**: Probe `VertexCount` at the standard offset; if valid but non-zero, or if followed by invalid `ColorCount` (misalignment), treat as Variant.

## 4. Extraction & Pipeline
We use a two-pronged approach for meshes:

1. **Native Parser** (`renderer/lib/staticmesh_construct.py`):
   - Stabilized for ~70% of meshes.
   - Provides 100% byte coverage for supported variants.
   - Saves parsed metadata to the `staticmesh_data` database table.

2. **umodel Fallback**:
   - For complex variants not yet fully supported by the native parser, we use `umodel.exe` via Wine as a fallback.

### Suffix & Material Handling
- **LODs**: Meshes often have suffixes like `_L0`, `_L1`. We prioritize `_L0`.
- **Materials**: Material references are extracted via `extract_mesh_materials.py` and stored in `mesh_materials.json`.

## 5. Success Breakthroughs (Post-Mortem)
- **LOD Suffix Robustness**: The viewer uses an `attemptLoad` cascade: `[BaseName] -> [BaseName]_L0 -> [BaseName]_ver01`.
- **Denormal Float Filtering**: We ignore any world positions where $abs(v) > 500,000$ to prevent "teleporting" meshes caused by misinterpreting binary headers as floats.
