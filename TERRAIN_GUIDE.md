# Vanguard Terrain & VGR Specification

This document covers the extraction and reconstruction of Vanguard: Saga of Heroes terrain from `.vgr` chunk files.

## 1. File Structure (VGR)
VGR files are standard UE2 packages that contain one primary `TerrainInfo` actor and its associated textures.

- **Coordinate System**: Chunks are named `chunk_X_Y` (e.g., `chunk_n25_26`). 'n' indicates a negative value.
- **World Units**: Each chunk covers roughly **200,000 Unreal Units**.
- **Pixels per Chunk**: Most terrain is 512x512 pixels.
- **Resolution**: ~390.625 units per pixel ($200,000 / 512$).

## 2. Heightmap Parsing (G16)
The heightmap is stored as a `Texture` export named `{ChunkName}Height`.

### Binary Format
- **Format**: `TEXF_G16` (16-bit grayscale).
- **Pixel Data**: 2 bytes per pixel ($Width \times Height \times 2$).

### Vanguard-Specific Hurdles
1. **Byte Order**: Unlike standard UE2 G16 (`High << 8 | Low`), Vanguard uses **swapped byte order**: `Low << 8 | High`.
2. **Column Shift (De-swizzle)**: The data is stored with a **34-pixel column shift**. To align correctly, you must roll the pixel columns: `np.roll(data, -34, axis=1)`.
3. **Height Scale**: The canonical multiplier for terrain height is **2.4**.

## 3. Terrain Textures (DXT5 / RGBA)
Base color textures are found in exports containing `baseColor`.

### Extraction Strategy
Do NOT rely on standard UE2 serialization offsets. Vanguard's VGR textures often have variable header lengths. Use the **Footer Scanning** method:
1. Scan backwards from the end of the export data for a **10-byte footer**.
2. **Footer Format**: `[USize (4)][VSize (4)][UBits (1)][VBits (1)]`.
3. Valid USizes are typically `512`, `1024`, or `2048`.

### Formats & Decoding
1. **Format 7 (DXT5)**: 
   - Standard DXT5 block compression.
   - Use our native Python `decode_dxt5` (in `extract_all_terrain.py`) to avoid Wine/umodel overhead.
2. **Format 5 (RGBA8)**:
   - Raw bytes stored as **BGRA**.
   - Must swap Blue and Red channels before displaying.

**Important**: Unlike heightmaps, color textures **DO NOT** require the column shift.

## 4. Current Status
- **Scripts**: `renderer/extractors/extract_all_terrain.py` (Native Python).
- **Output**: `renderer/output/terrain/terrain_grid/*.gltf`.
- **Coverage**: 100% of non-ocean chunks.
