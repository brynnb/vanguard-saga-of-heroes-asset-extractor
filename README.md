# Vanguard: Saga of Heroes Asset Extractor

[Vanguard: Saga of Heroes](https://en.wikipedia.org/wiki/Vanguard:_Saga_of_Heroes) was a 2007 fantasy MMORPG built on a heavily customized Unreal Engine 2.5 and many other tools. This asset extractor project extracts, converts, and exports all of the 3D models, animations, audio, and many other files from the original game client that users would install to play. The goal of this process is to recreate a modern, cross-platform, performant, customizable client for Vanguard and have it run with an emulated server. Vanguard was officially shut down in 2014 by its current publisher and is now unplayable outside of a (very awesome but unaffiliated) dedicated server emulation project. Its users can currently only play by running a very dated, Windows-only original game client.

This project is the extraction side of my other project, [Vanguard: Eternal Sagas](https://www.eternalsagas.com/), which is the actual implementation of these assets into a modern engine. Both sides of this work are the result of hundreds of hours of reverse engineering, parser writing, binary inspection, format reconstruction, and reimplementation into a new engine. The Eternal Sagas website also has multiple pages showing the assets that this project extracts, including the character models, music, 3D world, and world objects.

<table>
  <tr>
    <td width="50%">
      <img src="https://github.com/user-attachments/assets/c422ea53-8a68-4c16-a5d6-6d81cba06ffd" alt="1">
    </td>
    <td width="50%">
      <img src="https://github.com/user-attachments/assets/4094dd37-fba7-47cf-81fd-7e5cad36872e" alt="2">
    </td>
  </tr>
  <tr>
    <td width="50%">
      <img src="https://github.com/user-attachments/assets/7722fce1-310f-4bf4-9613-4340e4510017" alt="3">
    </td>
    <td width="50%">
      <img src="https://github.com/user-attachments/assets/a42734de-c826-48ba-9018-c59ae05c3efd" alt="4">
    </td>
  </tr>
</table>





This is a fan-made preservation and recreation project and is not affiliated with, endorsed by, or connected to Daybreak Game Company LLC or any of its subsidiaries. Vanguard: Saga of Heroes is a registered trademark of Daybreak Game Company LLC. This repository does not distribute Vanguard client files, extracted game assets, generated output, or any other copyrighted Vanguard asset content.

## What It Extracts

The original Vanguard client data is spread across UE2 package files, Vanguard-specific mesh and animation formats, terrain chunks, audio archives, prefab/object archives, text resources, and several one-off binary formats. This project currently extracts:

- Terrain from `.vgr` chunk files, including heightmaps, layer masks, terrain textures, baked vegetation-shadow masks, and chunk transforms.
- Static meshes from UE2 package data, exported to glTF with authored tangent
  handedness when the source tangent basis is valid, and package-qualified
  material metadata. Invalid/uninitialized tangent streams are omitted so the
  target renderer can generate a correct basis. Static material exports
  preserve the recoverable UE2.5 `Shader` / `Combiner` / `TexScaler` graph,
  bump, specular, detail, opacity, and self-illumination inputs in glTF extras
  so a runtime adapter can reproduce them without flattening layered materials.
  Each glTF also carries its package/outer/export identity, decoded per-section
  `Collision.Enable Collision` policy, and authored simple-collision flags in
  `asset.extras.vg_collision`. Downstream collision compilers should consume
  this embedded contract instead of joining against a separately generated,
  flat mesh-name table. Unknown serialized Collision-array variants are marked
  `unsupported_payload` rather than guessed or allowed to fail mesh extraction.
  Object-scoped runs also retain same-package terminal `_collision` / `_coll`
  helper meshes and publish bounds-validated links in
  `output/meshes/buildings/collision_helpers.json`.
- Character meshes, playable race metadata, customization data, and item attachment references.
- EMotion FX and UE2 skeletal animations, including static hand/finger pose clips and playable facial-control sidecars.
- UAX, ISB, and ICB audio data.
- SGO prefab and world object placement data.
- Shader-to-texture mappings and diffuse texture PNGs when the optional Unreal-Library CLI is available.

The world renderer can parse only the packages referenced by an existing
immutable Cesium object artifact and write only its exact referenced mesh/LOD
outputs, while retaining package-level parallelism:

```bash
.venv/bin/python scripts/extractors/staticmesh_pipeline.py --export-only \
  --object-artifact /path/to/objects-world-v12 --workers 2
```

If those exact mesh files are already present and only the canonical inventory
was lost or replaced by a bounded extraction, recover it without decoding or
rewriting any mesh payloads:

```bash
.venv/bin/python scripts/extractors/staticmesh_pipeline.py --manifest-only \
  --object-artifact /path/to/objects-world-v12
```

This mode verifies every referenced extracted glTF before publishing a
`status: complete`, `scope: object_artifact` manifest. A selected-package
manifest is intentionally not interchangeable with that full inventory;
downstream full-world builders must reject the bounded form.

Genuine SpeedTree RT assets need one authoritative preprocessing pass before
their first static-mesh export. Vanguard stores leaf cards as collapsed runtime
data rather than ordinary triangles. Generate exact card sidecars through the
original runtime, then run the normal StaticMesh command:

```bash
.venv/bin/python scripts/speedtree/generate_speedtree_runtime_leaf_cards.py \
  --converter /path/to/Spt2Fbx.exe \
  --object-artifact /path/to/objects-world-v12
```

`Spt2Fbx.exe` and a compatible `SpeedTreeRT.dll` must be together. The
extractor does not distribute either binary; the open-source bridge and its
release are available from [VenoMKO/Spt2Fbx](https://github.com/VenoMKO/Spt2Fbx).
On Linux the script uses Wine or an installed Steam Proton runtime. Intermediate
SPT/FBX files live under ignored, disk-backed `output/work/`, while compact
runtime-derived JSON sidecars live under `output/data/speedtree_runtime_leaf_cards/`.
Spt2Fbx and SpeedTreeRT are preprocessing tools only; exported glTF and the
runtime viewer do not depend on either executable.

The normal StaticMesh exporter then replaces collapsed cards with portable
glTF quads. Repeated `POSITION` values preserve each runtime card center while
`TEXCOORD_1` carries its exact width/height corner offsets; `TEXCOORD_0` keeps
the authored atlas region. Spt2Fbx reports cards in the SpeedTree model's native
units, so the hybrid exporter recovers each tree's UE2-baked uniform scale from
the original collapsed canopy bounds and applies it to both card centers and
corner offsets. Material extras explicitly distinguish camera-facing
`leaf_card` surfaces from fixed `frond` geometry and include the tree height
range used for wind. Cesium for Godot maps the standard second UV set to Godot
UV2, so Vanguard can expand leaves in camera space without a private glTF
extension. The exporter also makes foliage double-sided, removes degenerate
source triangles, rejects corrupt tangent memory, and refuses to publish a
broken SpeedTree when a required runtime sidecar is absent. SpeedTree
`*_shadow*` textures are retained as provenance metadata, but are deliberately
excluded from generic tiled detail modulation: they contain a projected
whole-tree silhouette, not bark- or leaf-space detail.

The recovered tree contract has three separate parts and they must not be
flattened together:

- Trunks, branches, and fixed fronds remain ordinary authored StaticMesh
  geometry. Masked fronds are tagged `vg_speedtree_foliage_kind: frond` and
  are not camera-facing leaf cards.
- Runtime leaves replace the collapsed SpeedTree leaf section. Their repeated
  `POSITION` is the card center, `TEXCOORD_1` is the scaled corner offset,
  `TEXCOORD_0` selects the authored atlas region, and `COLOR_0` carries the
  SpeedTree runtime's card dimming. The JSON sidecar also retains the recovered
  `PivotXY` values even though the current portable glTF contract does not need
  a separate pivot attribute. Authored vertex colors on surviving static
  geometry remain available for tree shadow/AO tint. Leaf and frond materials
  are double-sided; bark remains normally one-sided.
- World placement comes from the native 22-byte `DecoInstance` records in each
  terrain chunk, not from the mesh or Spt2Fbx. The native mesh lookup chooses
  the tree type, position and uniform scale are authored per instance, and the
  byte at offset 15 is the heading. The generator preserves it as
  `deco_yaw_byte`, converts it to UE2 rotation units with `yaw * 256`, and emits
  the resulting glTF node rotation. Bytes 16 and 17 are retained separately as
  the compact pitch/control and roll/control fields. Treating offset 15 as an
  unknown flag makes every repeated tree face the same direction.

The placement mesh index is resolved through TerrainInfo's native mesh-lookup
table. It must never fall back to a flat list of package imports: those indices
are not equivalent and the fallback can assign thousands of vegetation
instances to the wrong tree. Whole-tree far impostors are separate from leaf
cards and are not required by the current runtime pipeline.

Keep package workers low: individual Vanguard packages can require more than
5 GiB of transient memory while their embedded glTF and images are assembled.
Each mesh is published with a same-directory atomic rename, so an interrupted
worker cannot replace a valid prior glTF with a partial JSON document.
Static-mesh sections are decoded from UE2's explicit `IsStrip` and
`NumPrimitives` fields; the exporter never guesses topology from vertex
coverage, and incomplete triangle lists are rejected before publication.
The Vanguard-to-glTF coordinate conversion changes handedness, so every
triangle is reversed exactly once after section decoding. This keeps glTF's
front face aligned with the exported normals and prevents one-sided bark,
rocks, and buildings from appearing inside-out. Foliage being double-sided is
not a substitute for correct winding.
The command exits nonzero if any requested mesh fails and writes
`staticmesh-last-failure.json` without replacing the last successful manifest.
A successful manifest lists only the files produced by that run and records a
SHA-256 revision for every source package, so bounded refreshes cannot silently
claim unrelated historical output. It also reports the count and sample paths
of older on-disk glTFs that are explicitly unclaimed by the run. Obsolete heuristic mesh harvesters have
been removed; `staticmesh_pipeline.py` is the only supported static-mesh
decoder and exporter.
When a legacy package contains different exports with the same leaf name under
different UE2 outers, the manifest records that ambiguity and the explicitly
selected export index. The flat compatibility path deliberately retains the
old exporter's last-export behavior until placement identities are fully
outer-qualified; it is no longer an order-dependent silent overwrite.

## Setup

Create a virtual environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Download the original game client files, which are available from [the emulated server project](https://vgoplayers.com/downloads.php?tab=client). Any of the "VG Full Client" downloads work.

Extract the compressed Vanguard EMU folder into one of these easy locations, or any location of your choice:

```text
./Vanguard EMU
~/Downloads/Vanguard EMU
```

The repo-local `./Vanguard EMU` path is checked first, so a fresh clone can be used by placing the client folder directly inside this repo. The folder is ignored by git.

If your client is somewhere else, set paths explicitly:

```bash
export VANGUARD_EMU_PATH="/path/to/Vanguard EMU"
export VANGUARD_ASSETS_PATH="$VANGUARD_EMU_PATH/Assets"
```

## Commands

Run everything:

```bash
python3 vanguard.py extract-all
```

Run a small smoke test:

```bash
python3 vanguard.py setup --db --files
```

Preview the full command graph without running long extraction stages:

```bash
python3 vanguard.py extract-all --dry-run
```

Run individual extraction stages:

```bash
python3 vanguard.py setup --reset
python3 vanguard.py fetch-unreal-library
python3 vanguard.py build-shaders
python3 vanguard.py extract-terrain
python3 vanguard.py export-meshes
python3 vanguard.py export-characters
python3 vanguard.py export-animations
python3 vanguard.py export-animations --workers 8 --clean-emfx
python3 vanguard.py export-facial-controls
python3 vanguard.py export-npc-assembly
python3 vanguard.py extract-audio
python3 vanguard.py extract-world
```

`export-characters` also rebuilds the authoritative appearance sidecars:
`item_appearance_catalog.json` is a small package index; its generated
`item_appearances/*.json` payloads retain package-qualified item identities and
their skin/layer/tint/hiding rules without forcing runtimes to parse the whole
wardrobe at startup. `attachment_group_catalog.json`
decodes the 17 original `.sag` template packages. These replace the obsolete
attachment-index-only lookup; consumers must never resolve an appearance from
`attachment_index` without its `package_index` and actor visual profile.
`playable_races.json` also records exact race/style skin `TintAlpha` and
`TintPalette` assets from the canonical material manifest. This is necessary
because modular playable bodies reuse `npcHuman` geometry while retaining
their own race material palette.

Generate authoritative world-object and vegetation placements after prefab,
mesh-manifest, or native TerrainInfo parsing changes:

```bash
.venv/bin/python scripts/extractors/parse_sgo_prefabs.py
.venv/bin/python scripts/generators/generate_objects_from_txt.py --all --workers 4
```

The generated `chunk_*_objects.gltf` and `chunk_*_sgo.json` files carry
the stable native placement identity and diagnostic `deco_*` fields used by
downstream cell and Cesium builders. When the sibling Eternal Sagas client is
available, prefer `./dev world-assets` there: it fingerprints this manifest,
prefab data, placements, cell indexes, and immutable object/collision artifacts
so unchanged stages are not needlessly rebuilt.

Generate Godot runtime mesh packs for the viewer. The default layout stores
shared mesh assets once under `output/godot_runtime/assets/` and lets each chunk
manifest reference that global library with lightweight `mesh_assets` refs:

When the Godot viewer repo is available, prefer its orchestrator so runtime
mesh generation, native `.scn` packing, v4 object-cell indexing, and strict
audit run as one pipeline:

```bash
python3 ../vanguard-eternal-sagas/godot-viewer/scripts/tools/build_runtime_pack.py \
  --extractor-root "$PWD" \
  --chunk chunk_n25_26 \
  --workers 8
```

`--workers 8` is passed through to the runtime generator, the Godot native scene
packer, and object-cell indexing. Use `--workers 0` for all CPUs, or tune stages
individually with `--runtime-workers`, `--scene-workers`, and
`--object-cell-workers`.

The Godot native scene packer now writes the fast `.scn` files under
`output/godot_runtime/assets/scenes/` while also externalizing repeated material
and texture resources under `assets/materials/` and `assets/textures/`.

The lower-level generator can still be run directly:

```bash
python3 scripts/generators/generate_godot_runtime_chunk.py --chunk chunk_n25_26
```

The legacy chunk-local cache layout is still available for debugging:

```bash
python3 scripts/generators/generate_godot_runtime_chunk.py \
  --chunk chunk_n25_26 \
  --asset-storage chunk
```

The older full shared manifest shape is also available with
`--manifest-layout full`, but the default `thin` layout keeps chunks small and
uses `output/godot_runtime/assets/manifest.json` as the authoritative asset
index.

For long neighborhood or full-world runs, pass multiple chunks or `--all` so the
generator can actually use those worker processes:

```bash
python3 scripts/generators/generate_godot_runtime_chunk.py \
  --all \
  --workers 8
```

Generate compact cell indexes for streamed object placement planning:

```bash
python3 scripts/generators/generate_object_cell_index.py \
  --chunk chunk_n25_26 \
  --chunk chunk_n25_27 \
  --workers 8
```

These indexes live under `output/godot_runtime/chunks/<chunk>/object_cells.json`.
They keep chunk data lightweight by storing cell bounds, centers, compact
placement arrays, a string table, and a per-chunk asset table separately from
the global mesh/material/texture library. Godot can build streamed object cells
from those records without reading the source placement glTF/SGO files at
runtime. Like the runtime-pack generator, this indexer is chunk-parallel:
repeated `--chunk` values or `--all` are what let `--workers 8` speed up a long
run.

Generate normalized SGO particle emitter data and chunk placement indexes:

```bash
python3 scripts/extractors/extract_particle_textures.py
python3 scripts/generators/generate_particle_manifest.py
python3 scripts/generators/generate_particle_cell_index.py --chunk chunk_n25_26
```

The manifest lives at `output/data/particle_emitters.json` and keeps decoded
particle ranges, curves, package-qualified texture refs, unresolved texture
audits, and the source properties for every SGO emitter template.
`extract_particle_textures.py` reads `Texture__object_ref` metadata from
`sgo_emitters.json`, opens the matching UTX package, and writes package-qualified
PNGs under `output/textures/`. Per-chunk placement indexes live at
`output/godot_runtime/chunks/<chunk>/particle_cells.json`; the Godot viewer uses
those when available and falls back to the chunk SGO sidecar.

If you rebuild an older chunk-local runtime root in place, the Godot repo has a
safe cleanup tool for stale generated chunk-local cache directories:

```bash
python3 ../vanguard-eternal-sagas/godot-viewer/scripts/tools/prune_runtime_chunk_assets.py \
  --runtime-root "$PWD/output/godot_runtime"
python3 ../vanguard-eternal-sagas/godot-viewer/scripts/tools/prune_runtime_chunk_assets.py \
  --runtime-root "$PWD/output/godot_runtime" \
  --delete \
  --yes-delete-generated-cache
```

The NPC assembly sidecars use committed client lookup tables under
`client_tables/` for race visual mappings. Spawn-level race, model,
appearance, and attachment data comes from the committed compact JSON snapshot
at `client_tables/vgo_world_npc_snapshot.json`:

```bash
python3 vanguard.py extract-all --include-npc-assembly
```

To test a different snapshot, pass it explicitly:

```bash
python3 vanguard.py extract-all --include-npc-assembly --npc-snapshot /path/to/vgo_world_npc_snapshot.json
```

Python's closest equivalent to npm scripts is a console entry point. This repo includes one, so after an editable install you can run the shorter command form:

```bash
pip install -e .
vanguard-assets export-animations
```

## Optional Unreal-Library Support

Most core parsing is native Python in this repo. Unreal-Library is only used as an optional helper for specific text/object decompilation tasks:

- `scripts/extractors/bulk_extract_chunk_data.py` dumps TerrainInfo and object reference text from `.vgr` map chunks.
- `scripts/extractors/build_shader_texture_map.py` lists/decompiles material objects so this repo can resolve shader-to-texture chains and extract diffuse PNGs with the native texture parser.
- `scripts/generators/generate_objects_from_txt.py` consumes decompiled reference text when generating object placement files.

To install that helper into `external/Unreal-Library` and build the CLI:

```bash
python3 vanguard.py fetch-unreal-library
```

The all-in-one extraction command uses the helper automatically when the built CLI is present. Without Unreal-Library, use `--skip-unreal-library`.

## Output

Generated files are written under `output/`, including:

- `output/terrain/`
- `output/meshes/`
- `output/characters/`
- `output/animations/`
- `output/audio/`
- `output/data/`
- `output/zones/`

The `output/` folder is ignored by git.

## Database Output

The setup and extraction pipeline also creates a local SQLite database:

```text
output/data/vanguard_data.db
```

The database is generated from the user's local Vanguard EMU folder and is not included in this repository. It acts as an index and working catalog for the extraction process rather than a replacement for the source client files. This was largely a development tool and could be cleaned up.

Current tables include:

- `files`: indexed client files, paths, extensions, categories, sizes, and parser notes.
- `chunks`, `names`, `imports`, and `exports`: package/chunk metadata and object tables.
- `properties`: parsed UObject property values such as locations, prefab names, mesh references, and transforms.
- `terrain_chunks`: terrain extraction state and exported terrain paths.
- `mesh_index`, `parse_sessions`, `parsed_exports`, `parsed_fields`, and `unknown_regions`: mesh parser coverage, per-export parse results, and audit data.
- `shaders` and `mesh_materials`: shader/material mappings used by mesh and texture extraction.
- `prefabs`: resolved prefab component records.

Several commands also write JSON sidecars under `output/data/`, such as texture databases, playable race data, SGO prefab data, and audio cue manifests.

Terrain layer extraction writes each chunk's reusable material inputs under
`output/terrain/terrain_grid/<chunk>_terrain_layers/`. In addition to base
color, weight maps, and material-slot data, chunks that contain one now export
`chunk_shadow.png`: the original 512x512 L8 `ChunkShadowMap`. White means no
baked shadow and darker values primarily represent precomputed tree and
vegetation footprints. Vanguard's shipped version-129 chunks do not contain
UE2's legacy `TerrainInfo.VertexColors`; do not reinterpret `chunk_shadow.png`
as vertex color or a terrain paint weight. `tile_map.json` records the shadow
file, dimensions, source export, package association, and semantic meaning so a
future renderer can stream it without repeating the reverse engineering.

## Areas Remaining

- Exact original SpeedTree branch-wind simulation and whole-tree far impostors.
  Leaf-card size, pivot, placement, UVs, dimming, camera-facing metadata, fixed
  fronds, and the height range needed for runtime wind are now recovered; the
  actual animation is intentionally a renderer responsibility.
- Playable race assembly details around mixing heads with bodies, body sizing/proportions, and body texture selection.
- Skeleton customization logic for playable races, especially how character creation sliders drive the extra facial/body deformer bones.
- Animation usage around specific hand poses, weapon/attachment sockets, and left-hand/right-hand action variants is still being mapped for runtime use.
- Foot placement, foot locking, and ground-contact handling. The current extractor parses EMotion FX motion parts and root-motion tracks, but I have not confirmed Vanguard's final foot placement logic.
- Cloud rendering and global lighting effects are not fully accurate.
- Music playback is roughly 90% recovered, but cue selection, transitions, and zone-specific behavior still need tuning.
- Only a couple of basic water shader/material cases are extracted; a modern recreation would probably be better served by cleaner replacement water shaders, and is my intention with my reimplementation project.
- Particle emitters and VFX are not fully extracted yet, including sparkles, glows, emissive effects, spell effects, and other special effects.
- Tradeskill and combat system logic is outside the current extraction coverage, but will be recreated in the reimplementation project.
- Animation usage around specific actions is not fully mapped, even where the raw animation clips export correctly.
- Roughly 5% of character meshes and textures still have small issues that need follow-up.

## License

Original project code is licensed under the PolyForm Noncommercial License 1.0.0; see `LICENSE` and `NOTICE`. This is a source-available non-commercial software license.

Some UE2 package/property/static mesh parsing code is adapted from or informed by UE Viewer, which is MIT licensed. Those portions retain their original MIT permissions and notices. See `THIRD_PARTY_NOTICES.md`.

This repository does not grant rights to Vanguard: Saga of Heroes, its trademarks, client files, or game assets.

## Credits

The effort for this project was largely unique and without much precedent. The format and structure used by the Vanguard client files was unique, proprietary, and from what I can tell never before reverse-engineered. However, I was helped in places by lots of wonderful tools for similar versions of Unreal Engine, and there's generally a great community around Unreal Engine tools.

- [UE Viewer (umodel)](https://www.gildor.org/en/projects/umodel) by Gildor: viewer and extractor for Unreal Engine assets across engine versions; used to help with inspecting meshes, textures, and packages.
- [UE Explorer](https://eliotvu.com/portfolio/view/21/ue-explorer) by Eliot Van Uytfanghe: GUI browser and decompiler for Unreal Engine packages; used to inspect UnrealScript bytecode and serialized object properties.
- [Unreal Library (UELib)](https://github.com/EliotVU/Unreal-Library) by Eliot Van Uytfanghe: .NET library for reading and deserializing Unreal Engine package files; referenced for package format conventions and used optionally for text/object dumps.
- [UTPackage.js](https://github.com/fserb/UTPackage.js) by Fernando Serboncini: JavaScript Unreal Tournament package reader; referenced for JS-side binary parsing of UE2 package structures.
- [Ghidra](https://ghidra-sre.org/) by NSA Research Directorate: open-source software reverse engineering framework; used for decompiling VGClient.exe and analyzing engine functions, audio dispatch, animation, and locomotion systems.
- [Spt2Fbx](https://github.com/VenoMKO/Spt2Fbx) by the SpeedTree community: used as the bridge to SpeedTree RT 4.x leaf-card geometry as used in the original engine.
- [vgmstream](https://vgmstream.org/) by vgmstream contributors: library for streamed video game audio formats; used as a reference for ISACT ICB/ISB codec identifiers and field naming conventions.
- [Legendary Explorer](https://github.com/ME3Tweaks/LegendaryExplorer) by ME3Tweaks: Mass Effect package editor and toolkit; referenced for ISACT codec conventions and Unreal package parsing approaches.
- [VGO Emulator Wiki](https://wiki.vgoemulator.net/Docs/Main_Page) by the VGO Emulator community: community wiki for the Vanguard emulator project; used as a reference for game mechanics, zone data, and server-side systems.

And of course, credit and thanks to the original creators, developers, and others associated with the original Vanguard project. Vanguard was a really special game with a lot of vision, and it's been a lot of fun to explore it in such a deep, digital-archeological sort of way.
