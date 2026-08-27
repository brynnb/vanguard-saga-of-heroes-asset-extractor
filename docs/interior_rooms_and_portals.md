# Interior Room and Portal Authority

Vanguard's interior reconstruction combines two original source systems:

- Native map `UModel` BSP zones, leaves, potentially-visible-set masks, bounds,
  and collision/render-bound references from BSP nodes.
- SGO compound-type-3 rooms with authored `Portal` actors, room-owned
  components, placed instances, portal adjacency, and aperture geometry.

The extractor preserves both sources without claiming that an SGO room maps
one-to-one to a native map BSP zone. Their relationship is useful runtime
authority, but it is not interchangeable source data.

## Source publication

Generate and audit the extractor-owned interior publication from the complete
terrain inventory:

```bash
python -m scripts.generators.generate_world_residency_interiors \
  --source-terrain-inventory output/world_residency/source_terrain_inventory.json \
  --output output/world_residency/interior_source_publication.v2.json
python -m scripts.auditors.audit_world_residency_interiors \
  --publication output/world_residency/interior_source_publication.v2.json \
  --source-terrain-inventory output/world_residency/source_terrain_inventory.json
```

This path does not require first constructing a downstream immutable world
pack. A production world-pack manifest remains supported as an alternative
input through `--source-pack-manifest`.

The publication retains:

- room-owned visual and nonvisual components;
- exact placed interior instances and transforms;
- portal adjacency and connections;
- transformed portal aperture triangles;
- native BSP zones, leaves, visibility masks, and bounds;
- the source identities needed to audit every join.

## Runtime publication

Build the static-mesh source index, reusable room packs, Cesium boundary, and
portal runtime catalog:

```bash
python -m scripts.generators.generate_staticmesh_source_index
python -m scripts.generators.generate_godot_runtime_interior_assets \
  --source-authority output/world_residency/interior_source_publication.v2.json \
  --output-root output \
  --runtime-root output/godot_runtime \
  --boundary-output output/world_residency/interior_cesium_boundary.v1.json \
  --portal-runtime-output output/world_residency/interior_portal_runtime.v1.json \
  --free-space-reserve-gb 5
```

These commands stream and hash large source files and require disk-backed
outputs. The generator rejects `/tmp`, `/dev/shm`, and `/run` so large batches
cannot silently consume RAM-backed temporary storage.

The runtime publication produces reusable, content-addressed room packs and a
compact Cesium boundary. Only exact room-owned placement keys with complete,
ready, one-to-one room-pack replacements are eligible for Cesium exclusion.
Every incomplete or ambiguous instance remains an explicit Cesium fallback.
The boundary records both categories so downstream builders can fail closed
and publish deterministic retained, excluded, and fallback counts.

## Portal runtime catalog

The compact portal catalog stores:

- reusable template-local room bounds;
- exact portal aperture triangles and planes;
- room adjacency and portal connections;
- exterior portal boundaries;
- each eligible instance's room-pack, chunk, and root mapping.

Geometry is stored once per room pack rather than duplicated for every placed
building. Room bounds are conservative axis-aligned bounding boxes derived
from exact room-owned visual mesh bounds plus portal apertures. They are broad-
phase runtime bounds, not evidence that SGO rooms correspond one-to-one with
native BSP zones.

## Fidelity and safety rules

- Source identities and one-to-one placement bindings are authoritative;
  filename or position-only guesses are not.
- A missing, ambiguous, or incomplete replacement remains visible through the
  Cesium fallback path.
- Cesium exclusion is published only after the corresponding replacement is
  complete and ready.
- Portal apertures come from recovered geometry. A name-only portal reference
  is insufficient runtime authority.
- Template-local rooms and endpoints use stable local indices so reusable
  geometry does not need to be duplicated per instance.

## Remaining work

Every room-owned `StaticMeshActor` without a resolved mesh still needs to be
classified as one of:

- `no_static_mesh_property`;
- `unresolved_static_mesh_reference`;
- `resolved_reference_missing_asset`.

The latter two are extraction gaps to investigate. Unresolved actors must not
be assumed to be empty placeholders or used for Cesium exclusion until an
exact renderable mesh is recovered.
