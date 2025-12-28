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
- **Format**: `TEXF_G16` (Format ID 10, 16-bit grayscale).
- **Pixel Data**: 2 bytes per pixel ($Width \times Height \times 2$).
- **Size Marker**: Look for a 4-byte little-endian integer containing the expected size (e.g., `0x80000` = 524288 for 512×512×2). The height data follows immediately after.

### Vanguard-Specific Decoding (CRITICAL)

#### 2.1 Data Storage Order: Column-Major (Fortran Order)

**Unlike typical row-major storage**, Vanguard heightmaps are stored in **column-major order** (Fortran-style). This means:
- Pixels are stored column-by-column, not row-by-row
- When reshaping the array, you MUST use `order='F'`

```python
heights = np.frombuffer(height_data, dtype='<u2').reshape(
    grid_size, grid_size, order='F'
).astype(np.float64)
```

**Symptoms if wrong**: The terrain will have severe diagonal striations and spikes at regular 34-pixel intervals.

#### 2.2 Byte Order: Little-Endian

Vanguard uses **little-endian** byte order for G16 heightmaps:
- Use `dtype='<u2'` in numpy (little-endian unsigned 16-bit)

**Symptoms if wrong**: Massive random spikes throughout the terrain (values jumping erratically).

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
        curr, left, right = heights[row, col], heights[row, col-1], heights[row, col+1]
        diff = curr - (left + right) / 2
        if 200 < diff < 320: heights[row, col] -= 256
        elif -320 < diff < -200: heights[row, col] += 256

# Vertical pass (check up/down neighbors)
for col in range(grid_size):
    for row in range(1, grid_size - 1):
        curr, up, down = heights[row, col], heights[row-1, col], heights[row+1, col]
        diff = curr - (up + down) / 2
        if 200 < diff < 320: heights[row, col] -= 256
        elif -320 < diff < -200: heights[row, col] += 256
```

**Symptoms if not applied**: Regular spikes along height layer transition lines (visible as concentric rings).

#### 2.4 Height Scale
Height scale multiplier: **3.0** (units per raw height value). This is a temporary adjustment for testing purposes but the true number is likely 2.4 and we need to correct some other aspect of height maps or rendering to make it display correctly.

#### 2.5 Terrain Scale
Horizontal terrain scale: **390.625** units per pixel (200,000 world units / 512 pixels).




## 3. Terrain Textures (Color Maps)
Base color textures are found in exports containing `baseColor`.

### Formats & Decoding
| Format ID | Name | Channel Order | Notes |
|-----------|------|---------------|-------|
| 7 | DXT5 | Standard | Block-compressed, use DXT5 decoder |
| 5 | BGRA8 | **BGRA** | Raw bytes: B=0, G=1, R=2, A=3 |
| 3 | DXT1 | Standard | Block-compressed, use DXT1 decoder |
| 63 | DXT5 | Standard | Vanguard variant of Format 7 |

**Critical Note on Format 5**: The channel order is **BGRA**, NOT RGBA or ARGB.
- Interpreting as RGBA causes pink/red tint
- Interpreting as ARGB causes grey-blue tint

### 3.1 The LAST-MARKER LAW (Alignment)

Vanguard texture exports contain **multiple size markers** in the header area (typically at offsets 133, 148, 163, 202 for color textures). Only the **LAST** marker with valid (non-zero) payload is the True Marker.

**Why Multiple Markers Exist**: The header contains structures that coincidentally match the expected data size. Earlier markers point to garbage/metadata.

**The Algorithm**:
```python
# Find all markers in header (first 500 bytes)
markers = find_all_occurrences(expected_size_bytes, header_area)

# Iterate in REVERSE order - last marker is the true one
for marker in reversed(markers):
    payload = data[marker + 4 : marker + 12]
    if payload != all_zeros:
        data_start = marker + 4  # TRUE data position
        break
```

**Examples**:
- `n10_n10Height`: Markers at 125, 194 → **194** is correct
- `n10_n11baseColor`: Markers at 133, 148, 163, 202 → **202** is correct

### 3.2 Texture Alignment & Transposition
To align color textures with the terrain mesh:
1. **Transpose**: Apply `Image.TRANSPOSE` because the mesh uses column-major heightmap data.
2. **No Shifts**: With correct marker selection, zero coordinate shifts are needed.

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
- **Parser**: `ue2/texture.py` (LAST-Marker Selection + BGRA Decoding)
- **Extractor**: `scripts/extractors/extract_all_terrain.py`
- **Output**: `output/terrain/terrain_grid/*.gltf`
- **Coverage**: ~296/321 chunks successful (remaining are ocean or special chunks).

## 6. Debugging Tips

### Visual Symptoms and Causes

| Symptom | Likely Cause |
|---------|--------------|
| Regular diagonal striations every 34 pixels | Using row-major instead of column-major order |
| Massive random spikes (heights jumping erratically) | Wrong byte order or wrong marker selection |
| Regular spikes along "height layer" contour lines | Missing wrap-around correction |
| Terrain appears completely flat | Missing or wrong HEIGHT_SCALE factor |
| Grey/white garbage at texture start | Using first marker instead of LAST marker |
| Pink/red texture tint | Format 5 interpreted as RGBA instead of BGRA |
| Grey-blue texture tint | Format 5 interpreted as ARGB instead of BGRA |

### Testing a Single Chunk
```bash
python scripts/extractors/extract_all_terrain.py --chunk chunk_n10_n10
```

### Verifying Smooth Heights
```python
heights = ...  # extracted heightmap
max_delta = np.abs(np.diff(heights, axis=1)).max()
print(f"Max horizontal delta: {max_delta}")  # Should be < 300 for smooth terrain
```

## 7. Known Issues & Future Work

### Issue #1: 256-Boundary Heuristic (UNRESOLVED ROOT CAUSE)

**Status**: ⚠️ Working but not fully understood

**Description**: The heightmap data contains anomalies at 256-value boundaries that cause spikes. We apply a heuristic correction, but the true root cause is not fully understood.

**Investigation Needed**:
1. Examine UE2.5 source code for terrain serialization
2. Check if Vanguard uses custom compression or delta-encoding for G16

### Issue #2: ~25 Failed Chunks

**Status**: Expected (not bugs)

**Description**: Some chunks fail extraction. These are likely ocean or special chunks without standard heightmap textures.

### Issue #3: Multi-Layer Terrain Textures

**Status**: Not implemented

**Description**: Vanguard terrain likely uses alpha-blended multi-layer textures (grass/rock/dirt). Currently we only extract the base color layer.
