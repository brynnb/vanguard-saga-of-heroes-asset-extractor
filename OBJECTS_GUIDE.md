# Vanguard World Objects & Placements

This document covers how objects (Actors, CompoundObjects, Prefabs) are placed and structured in the game world.

## 1. World Hierarchy
Vanguard organizes the world into **Chunks** (VGR files). Each chunk is a local scene containing:
- **Actors**: Simple objects with a position and a mesh.
- **CompoundObjects**: Complex "recipes" (prefabs) that reference multiple sub-meshes with local offsets and rotations.

## 2. The Prefab System (Cooked Metadata)
Vanguard uses a "Prefab Explosion" technique for complex structures. This data is extracted from the `binaryprefabs.sgo` and `.vgr` files.

### Critical Discovery: SGO Structure
- **Global Table**: Maps Prefab IDs to Name Indices.
- **Recipe Block**: Lists components (StaticMeshes), their local positions (FVector), and rotations (FRotator).

---

## 3. Placement Extraction & Binary Structures
We extract placements from VGR files using `renderer/extractors/extract_chunk_data.py`.

### Critical Hurdle: SGO "Garbage" Mitigation
- **The Problem**: Some binary headers in Vanguard packages look like valid floating-point numbers. Early parsers would treat these as coordinates, "teleporting" objects to the edge of the universe.
- **The Solution**: We implement a **Priority Property Scanner**.
  - We strictly look for the UE2 property index `0x09` (Location).
  - We verify that coordinates are within a "Reasonable World Bound" ($abs(v) < 100,000$ for local, $500,000$ for world).

## 4. Scene Generation
We generate a combined scene for each chunk named `{ChunkName}_objects.gltf`.

- **Universal Class Inclusion**: We do NOT filter by class name alone. We include any object that has a `StaticMesh`, `Mesh`, or `PrefabName` property. This ensures we don't miss movers, emitters, or decorations.
- **Reference Resolution**: If a mesh name mismatched between the prefab and the disk (e.g., `Fence01` vs `Fence01_L0`), the generator applies a resolution strategy to find the correct asset.

## 5. Current Status
- **Scripts**: `renderer/generators/generate_objects_scene.py`.
- **Database**: `exports` table and `properties` table in `vanguard_data.db`.
- **Properties**: Every placed object's properties (Location, Rotation, DrawScale, etc.) are extracted during `setup.py` via the `extract_properties.py` stage.
- **Resolution**: 95%+ success rate for placed assets. Remaining gaps are typically internal "Brush Models" or legacy sprite effects.
