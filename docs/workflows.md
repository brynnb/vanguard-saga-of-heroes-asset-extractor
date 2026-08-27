# Advanced Extraction Workflows

The installed `vanguard-assets` CLI is the authoritative public entry point.
The lower-level `python -m scripts...` commands below are intended for focused
regeneration, debugging, and integration with the Eternal Sagas client.

Run commands from the extractor workspace so generated files use its `output/`
directory. Set `VANGUARD_WORKSPACE_ROOT` explicitly when invoking modules from
another directory.

## World objects and vegetation

Regenerate authoritative world-object and vegetation placements after prefab,
mesh-manifest, or native TerrainInfo parsing changes:

```bash
python -m scripts.extractors.parse_sgo_prefabs
python -m scripts.generators.generate_objects_from_txt --all --workers 4
```

Generated `chunk_*_objects.gltf` and `chunk_*_sgo.json` files retain stable
native placement identities and diagnostic `deco_*` fields used by downstream
cell and Cesium builders.

When the sibling Eternal Sagas repository is available, prefer its
`./dev world-assets` orchestration. It fingerprints manifests, prefabs,
placements, cell indexes, and immutable object/collision artifacts so unchanged
stages are not rebuilt.

## Godot runtime packs

The default runtime layout stores shared mesh assets once under
`output/godot_runtime/assets/`. Chunk manifests refer to that global library
using lightweight `mesh_assets` references.

When the Godot viewer repository is available, use its orchestrator so runtime
mesh generation, native `.scn` packing, object-cell indexing, and strict audit
run as one pipeline:

```bash
python ../vanguard-eternal-sagas/godot-viewer/scripts/tools/build_runtime_pack.py \
  --extractor-root "$PWD" \
  --chunk chunk_n25_26 \
  --workers 8
```

`--workers 8` is passed to runtime generation, native scene packing, and
object-cell indexing. Use `--workers 0` for all CPUs or tune individual stages
with `--runtime-workers`, `--scene-workers`, and `--object-cell-workers`.

The native scene packer writes `.scn` files under
`output/godot_runtime/assets/scenes/` and externalizes repeated material and
texture resources under `assets/materials/` and `assets/textures/`.

The lower-level generator remains available:

```bash
python -m scripts.generators.generate_godot_runtime_chunk \
  --chunk chunk_n25_26
```

The legacy chunk-local cache is available for debugging:

```bash
python -m scripts.generators.generate_godot_runtime_chunk \
  --chunk chunk_n25_26 \
  --asset-storage chunk
```

The older full shared manifest shape is available with
`--manifest-layout full`. The default `thin` layout keeps chunks small and uses
`output/godot_runtime/assets/manifest.json` as the authoritative asset index.

For neighborhood or full-world runs, pass multiple chunks or `--all` so worker
processes can operate in parallel:

```bash
python -m scripts.generators.generate_godot_runtime_chunk \
  --all \
  --workers 8
```

## Object cell indexes

Generate compact cell indexes for streamed object placement planning:

```bash
python -m scripts.generators.generate_object_cell_index \
  --chunk chunk_n25_26 \
  --chunk chunk_n25_27 \
  --workers 8
```

Indexes are written to
`output/godot_runtime/chunks/<chunk>/object_cells.json`. They store cell bounds,
centers, compact placement arrays, a string table, and per-chunk asset tables
separately from the global mesh/material/texture library. Repeated `--chunk`
values or `--all` enable chunk-level parallelism.

## Particle extraction and indexes

Generate normalized SGO particle data and chunk placement indexes:

```bash
python -m scripts.extractors.extract_particle_textures
python -m scripts.generators.generate_particle_manifest
python -m scripts.generators.generate_particle_cell_index \
  --chunk chunk_n25_26
```

`output/data/particle_emitters.json` retains decoded ranges, curves,
package-qualified texture references, unresolved-texture audits, and source
properties for each SGO emitter template. Extracted package-qualified PNGs are
written under `output/textures/`. Per-chunk placement indexes are written to
`output/godot_runtime/chunks/<chunk>/particle_cells.json`.

## Generated-cache cleanup

The Godot repository includes a safe tool for finding stale generated
chunk-local caches:

```bash
python ../vanguard-eternal-sagas/godot-viewer/scripts/tools/prune_runtime_chunk_assets.py \
  --runtime-root "$PWD/output/godot_runtime"
```

Deletion requires explicit flags:

```bash
python ../vanguard-eternal-sagas/godot-viewer/scripts/tools/prune_runtime_chunk_assets.py \
  --runtime-root "$PWD/output/godot_runtime" \
  --delete \
  --yes-delete-generated-cache
```

## NPC assembly

NPC assembly uses committed client lookup tables under `client_tables/` for
race visual mappings. Spawn-level race, model, appearance, and attachment data
comes from `client_tables/vgo_world_npc_snapshot.json`.

```bash
vanguard-assets extract-all --include-npc-assembly
```

To test another snapshot without replacing the committed input:

```bash
vanguard-assets extract-all \
  --include-npc-assembly \
  --npc-snapshot /path/to/vgo_world_npc_snapshot.json
```

## Optional Unreal-Library support

Most core parsing is native Python. Unreal-Library is an optional helper for:

- decompiling map reference text used by world-object generation;
- enumerating material objects and shader-to-texture chains;
- extracting diffuse texture PNGs through the native texture parser.

Install it into the configured `external/Unreal-Library` directory:

```bash
vanguard-assets fetch-unreal-library
```

The full pipeline uses the helper automatically when its built CLI is present.
Use `--skip-unreal-library` when it is intentionally unavailable.
