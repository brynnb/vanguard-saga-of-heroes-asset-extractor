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
- **Size Marker**: Look for a 4-byte little-endian integer containing the expected size (e.g., `0x80000` = 524288 for 512×512×2). The height data follows immediately after.

### Vanguard-Specific Decoding (CRITICAL)

The G16 heightmap format in Vanguard requires several non-standard transformations:

#### 2.1 Data Storage Order: Column-Major (Fortran Order)

**Unlike typical row-major storage**, Vanguard heightmaps are stored in **column-major order** (Fortran-style). This means:
- Pixels are stored column-by-column, not row-by-row
- When reshaping the array, you MUST use `order='F'`

```python
heights = np.frombuffer(height_data, dtype='>u2').reshape(
    grid_size, grid_size, order='F'
).astype(np.float64)
```

**Symptoms if wrong**: The terrain will have severe diagonal striations and spikes at regular 34-pixel intervals.

#### 2.1.1 Alignment Shift (De-swizzle)

After reading the column-major data, a **35-pixel vertical shift** is required to align the terrain correctly within its chunk boundaries. Because of the column-major storage, this affects the **row axis** (axis 0):

```python
# Move the internal seam (at row 34) to the edge
heights = np.roll(heights, -35, axis=0)
```

**Symptoms if missing**: A horizontal seam appears ~7% from the top/bottom of the chunk, and it will not align vertically with its neighbors.

#### 2.2 Byte Order: Big-Endian

Vanguard uses **big-endian** byte order for G16 heightmaps:
- Each 16-bit height value = `first_byte * 256 + second_byte`
- Use `dtype='>u2'` in numpy (big-endian unsigned 16-bit)

**Symptoms if wrong**: Massive random spikes throughout the terrain.

#### 2.3 Wrap-Around Correction (256-Boundary Fix) - ⚠️ HEURISTIC

> **WARNING**: This section documents a **heuristic workaround**, not a true understanding of the data format. Per DEVELOPER_GUIDELINES.md Section 7.4, this requires documentation and tracking.

Even with correct byte order and column-major storage, there are **anomalies** at 256-value boundaries in the data. These manifest as spikes where the terrain should be smooth.

**Observed Behavior**: 
- Spikes occur at specific height values near multiples of 256
- The spikes are exactly ~256 units higher or lower than expected
- They follow "height layer" contour lines in a predictable pattern

**Unknown Root Cause**: We do NOT understand WHY the data has these anomalies. Possible explanations that need investigation:
1. Custom compression/encoding in Vanguard's terrain serialization
2. A proprietary height encoding scheme we haven't reverse-engineered
3. Deliberate data obfuscation
4. An artifact of how Vanguard's terrain editor exported heightmaps

**Heuristic Workaround**: We detect values ~256 off from their neighbors' midpoint and correct them:

```python
# Horizontal pass (check left/right neighbors)
for row in range(grid_size):
    for col in range(1, grid_size - 1):
        curr = heights[row, col]
        left = heights[row, col - 1]
        right = heights[row, col + 1]
        expected = (left + right) / 2
        diff = curr - expected
        
        if diff > 200 and diff < 320:
            heights[row, col] -= 256
        elif diff < -200 and diff > -320:
            heights[row, col] += 256

# Vertical pass (check up/down neighbors)
for col in range(grid_size):
    for row in range(1, grid_size - 1):
        curr = heights[row, col]
        up = heights[row - 1, col]
        down = heights[row + 1, col]
        expected = (up + down) / 2
        diff = curr - expected
        
        if diff > 200 and diff < 320:
            heights[row, col] -= 256
        elif diff < -200 and diff > -320:
            heights[row, col] += 256
```

**Detection Range**: 200-320 works well (centered around 256 with some tolerance).

**Symptoms if not applied**: Regular spikes along height layer transition lines (visible as concentric rings in the heightmap image).

#### 2.4 Height Scale

The height scale multiplier is **3.0** (units per raw height value).

#### 2.5 Terrain Scale

Horizontal terrain scale is **390.625** units per pixel (200,000 world units / 512 pixels).

### What NOT to Do

The following approaches were tried and **do not work**:

1. ❌ **Row-major order** (`order='C'`) - causes severe striations every 34 columns
2. ❌ **Little-endian byte order** (`dtype='<u2'`) - causes massive random spikes
3. ❌ **34-pixel column shift** (`np.roll(data, ±34, axis=1)`) - this was a red herring; the real fix is column-major order
4. ❌ **Swapped byte interpretation** (`low << 8 | high`) - causes extreme spikes

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
   - Use our native Python `decode_dxt5` (in `extract_all_terrain.py`).
2. **Format 5 (RGBA8)**:
   - Raw bytes stored as **BGRA**.
   - Must swap Blue and Red channels before displaying.

**CRITICAL**: Like heightmaps, Vanguard terrain textures are effectively stored in a layout that corresponds to the column-major grid. To align correctly with the mesh, the extracted image MUST be **transposed** (flipped along the diagonal):
```python
img = img.transpose(Image.TRANSPOSE)
```

**Note**: Unlike heightmaps, color textures **DO NOT** require the -35 pixel alignment shift (they are already correctly windowed).

## 4. Mesh Generation

### Vertex Generation
```python
y_coords, x_coords = np.meshgrid(np.arange(grid_size), np.arange(grid_size), indexing="ij")
vx = x_coords.flatten() * TERRAIN_SCALE
vy = heights.flatten() * HEIGHT_SCALE
vz = y_coords.flatten() * TERRAIN_SCALE
```

### Triangle Index Generation
Standard grid triangulation with two triangles per quad:
```python
i0 = (y_idx * grid_size + x_idx).flatten()
indices = [i0, i0 + grid_size, i0 + 1, i0 + 1, i0 + grid_size, i0 + grid_size + 1]
```

### Normals
Calculate using central differences of neighboring height values.

## 5. Current Status
- **Scripts**: `scripts/extractors/extract_all_terrain.py` (Native Python).
- **Output**: `output/terrain/terrain_grid/*.gltf`.
- **Coverage**: 299/321 chunks successful (remaining 22 are ocean or special chunks without heightmaps).

## 6. Debugging Tips

### Visual Symptoms and Causes

| Symptom | Likely Cause |
|---------|--------------|
| Regular diagonal striations every 34 pixels | Using row-major instead of column-major order |
| Massive random spikes (heights in 30000-60000 range) | Wrong byte order (little-endian instead of big-endian) |
| Regular spikes along "height layer" contour lines | Missing wrap-around correction |
| Terrain appears completely flat | Missing or wrong HEIGHT_SCALE factor |
| Texture misaligned with terrain | Accidentally applying heightmap transforms to texture |

### Testing a Single Chunk
```bash
python scripts/extractors/extract_all_terrain.py --chunk chunk_n10_n10
```

### Verifying Smooth Heights
Check that adjacent height values have small deltas (typically < 50):
```python
heights = ...  # extracted heightmap
max_delta = np.abs(np.diff(heights, axis=1)).max()
print(f"Max horizontal delta: {max_delta}")  # Should be < 300 for smooth terrain
```

---

## 7. Known Issues & Future Work

### Issue #1: 256-Boundary Heuristic (UNRESOLVED ROOT CAUSE)

**Status**: ⚠️ Working but not understood

**Description**: The heightmap data contains anomalies at 256-value boundaries that cause spikes. We apply a heuristic correction (neighbor-averaging to detect and fix ~256-unit outliers), but we do NOT understand the true root cause.

**Current Workaround**: Two-pass correction in `extract_all_terrain.py` that detects values ~256 off from neighbors and adjusts.

**Residual Issues**: 
- ~1000 pixels per chunk (~0.4%) may still have uncorrected anomalies
- Some corrections may incorrectly modify legitimate steep terrain features

**Investigation Needed**:
1. Examine Unreal Engine 2.5 source code for terrain serialization (`UnTerrain.cpp`)
2. Compare raw heightmap bytes with known-good terrain to find encoding pattern
3. Check if Vanguard uses a custom compression or delta-encoding for G16
4. Analyze whether the pattern correlates with specific height ranges or tile positions

**Files Affected**:
- `scripts/extractors/extract_all_terrain.py` (lines 105-135)
- All terrain chunks in `output/terrain/terrain_grid/`

### Issue #2: 22 Failed Chunks

**Status**: Expected (not bugs)

**Description**: 22 of 321 chunks fail extraction. These are likely ocean or special chunks that don't contain standard heightmap textures.

**Investigation Needed**: Verify these are intentionally heightmap-less chunks.
