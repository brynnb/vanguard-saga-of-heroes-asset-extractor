# Static Mesh Extraction Contract

Vanguard static meshes are stored in UE2 package data with Vanguard-specific
serialization details. The supported extractor is
`scripts.extractors.staticmesh_pipeline`; obsolete heuristic mesh harvesters
have been retired.

## Exported representation

Static meshes are exported to glTF with:

- authored tangent handedness when the source tangent basis is valid;
- package, outer, and export identity;
- package-qualified material metadata;
- recoverable UE2.5 `Shader`, `Combiner`, and `TexScaler` graphs;
- bump, specular, detail, opacity, and self-illumination inputs;
- decoded collision policy and source collision geometry;
- explicit surface classification such as authored water materials.

Invalid or uninitialized tangent streams are omitted so the target renderer
can generate a valid basis. Material graphs remain in glTF extras instead of
being flattened into one diffuse texture.

## Collision contract

Each glTF carries decoded per-section `Collision.Enable Collision` policy and
authored simple-collision flags in `asset.extras.vg_collision`. The effective
simple policy applies the original UE2.5 class defaults when a package omits a
tagged override.

A StaticMesh's referenced `UModel` BSP collision is decoded into binary-backed
accessors in the same metadata contract. It deliberately remains outside the
glTF render scene. Downstream collision compilers should consume this embedded
contract rather than joining against a separate flat mesh-name table.

Unknown serialized Collision-array variants are marked
`unsupported_payload`; they are neither guessed nor allowed to abort otherwise
valid mesh extraction. Object-scoped runs retain same-package terminal
`_collision` and `_coll` helper meshes and publish bounds-validated links in
`output/meshes/buildings/collision_helpers.json`.

## Object-artifact-scoped extraction

The extractor can parse only packages referenced by an immutable Cesium object
artifact and write only its exact mesh and LOD outputs while retaining
package-level parallelism:

```bash
python -m scripts.extractors.staticmesh_pipeline --export-only \
  --object-artifact /path/to/objects-world-v12 \
  --workers 2
```

If the exact mesh files are already present and only the canonical inventory
was lost or replaced by a bounded extraction, recover it without decoding or
rewriting mesh payloads:

```bash
python -m scripts.extractors.staticmesh_pipeline --manifest-only \
  --object-artifact /path/to/objects-world-v12
```

This mode verifies every referenced glTF before publishing a
`status: complete`, `scope: object_artifact` manifest. A selected-package
manifest is intentionally not interchangeable with a full inventory;
downstream full-world builders must reject the bounded form.

## Publication guarantees

Keep package worker counts conservative. Individual Vanguard packages can
require more than 5 GiB of transient memory while embedded glTF and images are
assembled.

Each mesh is published using a same-directory atomic rename, so an interrupted
worker cannot replace a valid prior glTF with a partial JSON document. The
command exits nonzero if any requested mesh fails and writes
`staticmesh-last-failure.json` without replacing the last successful manifest.

A successful manifest lists only files produced by that run and records a
SHA-256 revision for every source package. Bounded refreshes therefore cannot
claim unrelated historical output. It also reports the count and sample paths
of older on-disk glTFs explicitly unclaimed by the run.

## Topology and coordinate conversion

Static-mesh sections are decoded from UE2's explicit `IsStrip` and
`NumPrimitives` fields. The exporter does not infer topology from vertex
coverage, and it rejects incomplete triangle lists before publication.

The Vanguard-to-glTF coordinate conversion changes handedness, so every
triangle is reversed exactly once after section decoding. This keeps glTF's
front face aligned with exported normals and prevents one-sided bark, rocks,
and buildings from appearing inside-out. Double-sided foliage is not a
substitute for correct winding.

Generated LODs can retain a sparse original section number while compacting
their one surviving material. The exporter accepts only unambiguous compact
layouts and maps that material back to the active section. An explicitly null,
full-sized slot remains the authored UE2 default material.

## Identity and surface classification

When a package contains multiple exports with the same leaf name under
different UE2 outers, the manifest records the ambiguity, retains the old
last-export winner at the flat compatibility path, and publishes every export
under an `__outer__/` identity path. SGO placement records preserve the full
imported object identity and select the matching outer-qualified asset.

Every glTF carries `asset.extras.vg_surface_classification`. Water sections are
identified from recovered `WaterShaderMaterial` data rather than filenames and
are also listed in the StaticMesh manifest. The label means an authored water
surface, not necessarily a global ocean; local pools, fountains, and rivers
retain the same classification.

Effectively invisible helper components or meshes with an authored
`CullDistance` of one source unit remain in source data but are suppressed from
rendered placement indexes.

## SpeedTree boundary

SpeedTree meshes add an original-runtime leaf-card preprocessing step and a
separate native terrain placement contract. See
[SpeedTree extraction and runtime contract](speedtree.md).
