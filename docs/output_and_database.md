# Generated Output and Database

Generated files are written below the configured workspace's `output/`
directory. They are derived from the user's Vanguard client and are ignored by
Git.

## Output directories

- `output/terrain/`: terrain geometry, textures, material inputs, and tiles;
- `output/meshes/`: static and character mesh exports;
- `output/characters/`: playable race, appearance, and attachment sidecars;
- `output/animations/`: EMotion FX and UE2 animation exports;
- `output/audio/`: extracted samples, cue dumps, and audio manifests;
- `output/data/`: shared indexes, catalogs, and the SQLite database;
- `output/zones/`: zone-level extracted data;
- `output/godot_runtime/`: compact runtime assets and chunk indexes.

These outputs are not substitutes for the original client data. They are
regenerated indexes and converted assets used by analysis tools and the modern
client pipeline.

## SQLite database

The setup and extraction pipeline creates:

```text
output/data/vanguard_data.db
```

The database is a supported working index for extractors that need normalized
package, object, property, terrain, mesh, and material relationships. It is
generated from local client files and is not committed.

Current table groups include:

- `files`: indexed client paths, extensions, categories, sizes, and parser
  notes;
- `chunks`, `names`, `imports`, and `exports`: UE2 package and chunk object
  tables;
- `properties`: decoded UObject property names and values;
- `terrain_chunks`: terrain extraction state and exported paths;
- `mesh_index`, `parse_sessions`, `parsed_exports`, `parsed_fields`, and
  `unknown_regions`: mesh/parser coverage and diagnostics;
- `shaders` and `mesh_materials`: material and texture relationships;
- `prefabs`: resolved prefab component records.

JSON sidecars under `output/data/` provide smaller domain-specific contracts
for consumers that do not need the complete database. Examples include texture
indexes, playable race data, SGO prefab data, and audio cue manifests.

## Terrain layers and chunk shadows

Terrain extraction writes reusable material inputs under:

```text
output/terrain/terrain_grid/<chunk>_terrain_layers/
```

Alongside base color, weight maps, and material-slot data, a chunk can contain
`chunk_shadow.png`, the original 512 by 512 L8 `ChunkShadowMap`. White means no
baked shadow; darker values primarily represent precomputed tree and vegetation
footprints.

Vanguard's shipped version-129 chunks do not contain UE2's legacy
`TerrainInfo.VertexColors`. `chunk_shadow.png` must not be reinterpreted as
vertex color or a terrain-paint weight. `tile_map.json` records its file,
dimensions, source export, package association, and semantic meaning.

## Runtime outputs

Godot/runtime outputs use shared content-addressed assets and compact chunk
indexes. See [advanced extraction workflows](workflows.md) for generation
commands and [interior room and portal authority](interior_rooms_and_portals.md)
for room packs, portal catalogs, and Cesium exclusion boundaries.
