# Unreal Engine 2.5 Source Reference Guide

This document describes how to use the **Unreal Engine 2.5 (Unreal Warfare, 2007)** source code as a reference for reverse-engineering Vanguard's binary assets.

## 📍 Location
Path: `/Users/brynnbateman/Downloads/Unreal Engine [v2.5]_ Unreal Warfare [09-29-2007]`

---

## 🏗️ Key Directories & Files

### 1. Core (`/Core`)
This is the foundation of the engine. It defines how every object is saved to disk.
- **`Inc/UnTemplate.h`**: Search for `TLazyArray` or `TArray`. This shows how Unreal serializes lists and "lazy" data blocks (which use absolute file offsets to skip data).
- **`Inc/UnNames.h`**: Definitions for standard engine names.
- **`Src/UnObj.cpp`**: Contains the core `Serialize` methods for `UObject`. Use this to understand the **Property Table** (Tagged Properties) that starts every Unreal export.
- **`Src/UnCore.cpp`**: Look for `operator<<` overloads for primitive types (`int`, `float`, `FString`, `FCompactIndex`).

### 2. Engine (`/Engine`)
Contains the specific logic for game assets.
- **`Inc/UnTex.h` & `Src/UnTex.cpp`**:
  - The blueprint for **Textures**.
  - Shows how `Serialize` handles different formats (DXT1, DXT5, RGBA8, G16).
  - Search for `SerializeMips` to see how mipmap data is structured.
- **`Inc/UnStaticMesh.h` & `Src/UnStaticMesh.cpp`**:
  - The blueprint for **StaticMeshes**.
  - Shows the standard structure of `VertexStream`, `IndexBuffer`, and `Sections`.
  - **Note**: This is where we found the "Mismatch": Vanguard appends a custom `LODModels` section that is NOT in this base source.
- **`Inc/UnTerrain.h` & `Src/UnTerrain.cpp`**:
  - The blueprint for **Terrain**. Reference this when parsing `TerrainInfo` actors in VGR files.

### 3. Editor (`/Editor`)
Useful for understanding how assets are "cooked" and optimized.
- **`Src/UnMeshEd.cpp`**: Often contains logic for importing/optimizing mesh data.

---

## 🔍 How to Search
When you encounter an unknown byte sequence in a Vanguard file:
1. **Find the Class**: Determine the `class_name` of the export (e.g., `Texture` or `StaticMesh`).
2. **Locate the Header/Source**: Go to `Engine/Inc/Un{ClassName}.h` or `Engine/Src/Un{ClassName}.cpp`.
3. **Trace the `Serialize` Method**: Search for `void U{ClassName}::Serialize( FArchive& Ar )`. 
4. **Follow the Buffer (`Ar`)**: Every time you see `Ar << someVariable;`, it represents data being read from or written to the file.
   - If `someVariable` is a `TArray`, it reads a count (CompactIndex or int32) followed by that many elements.
   - If it's a `TLazyArray`, it reads an absolute file offset (SkipPos) for fast-skipping.

---

## ⚠️ Vanguard Deviations
Vanguard (Build 128/Lic 34) is a "modded" version of this source. Common differences to watch for:
- **Custom Sections**: Vanguard often appends extra data (like `LODModels`) after the standard UE2 serialization is finished.
- **Byte Order**: As seen in Terrain G16, Vanguard sometimes swaps byte orders compared to the reference source.
- **Property Indices**: The indices in the Name Table for properties like `Location` or `Rotation` might differ, but the serialization logic (`InfoByte`, `Size`) usually remains identical.
