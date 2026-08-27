# SpeedTree Extraction and Runtime Contract

Vanguard's genuine SpeedTree RT assets require one authoritative preprocessing
pass before their first static-mesh export. The client stores leaf cards as
collapsed runtime data rather than ordinary triangles, so exporting only the
visible static-mesh streams loses the canopy.

## Runtime preprocessing

Generate exact leaf-card sidecars through a compatible original SpeedTree
runtime, then run the normal static-mesh exporter:

```bash
python -m scripts.speedtree.generate_speedtree_runtime_leaf_cards \
  --converter /path/to/Spt2Fbx.exe \
  --object-artifact /path/to/objects-world-v12
```

`Spt2Fbx.exe` and a compatible `SpeedTreeRT.dll` must be together. This
repository distributes neither binary. The open-source bridge and its release
are available from [VenoMKO/Spt2Fbx](https://github.com/VenoMKO/Spt2Fbx).
On Linux, the generator uses Wine or an installed Steam Proton runtime.

Intermediate SPT and FBX files live under the ignored, disk-backed
`output/work/` directory. Compact runtime-derived JSON sidecars are written to
`output/data/speedtree_runtime_leaf_cards/`. Spt2Fbx and SpeedTreeRT are
preprocessing tools only; the exported glTF and runtime viewer do not depend on
either executable.

## Recovered tree contract

The recovered tree contract has three separate parts. They must not be
flattened into one guessed mesh representation.

### Trunks, branches, and fronds

Trunks, branches, and fixed fronds remain ordinary authored StaticMesh
geometry. Masked fronds are tagged `vg_speedtree_foliage_kind: frond`; they are
not camera-facing leaf cards. Every surface recovered from an embedded
SpeedTree payload is explicitly double-sided because generated trunk and
branch shells and thin foliage cannot safely inherit ordinary StaticMesh
culling.

### Runtime leaf cards

Runtime leaves replace the collapsed SpeedTree leaf section. The portable glTF
representation uses:

- repeated `POSITION` values for each card center;
- `TEXCOORD_1` for the exact scaled width/height corner offset;
- `TEXCOORD_0` for the authored atlas region;
- `COLOR_0` for SpeedTree runtime card dimming.

The JSON sidecar also retains recovered `PivotXY` values even though the
current portable glTF contract does not require a separate pivot attribute.
Authored vertex colors on surviving static geometry remain available for tree
shadow and ambient-occlusion tint.

Spt2Fbx reports cards in the SpeedTree model's native units. The hybrid
exporter recovers each tree's UE2-baked uniform scale from the original
collapsed canopy bounds and applies it to both card centers and corner
offsets. Material metadata distinguishes camera-facing `leaf_card` surfaces
from fixed `frond` geometry and includes the tree height range needed by a wind
renderer. Cesium for Godot maps the standard second UV set to Godot UV2, so the
client can expand leaves in camera space without a private glTF extension.

The exporter makes leaf, frond, branch, and bark materials double-sided,
removes degenerate source triangles, rejects corrupt tangent memory, and
refuses to publish a broken SpeedTree when its required runtime sidecar is
missing.

SpeedTree `*_shadow*` textures are retained as provenance metadata but excluded
from generic tiled detail modulation. They contain projected whole-tree
silhouettes, not bark- or leaf-space detail.

### World placement

World placement comes from native 22-byte `DecoInstance` records in each
terrain chunk—not from the mesh or Spt2Fbx. The native TerrainInfo mesh-lookup
index selects the tree type. Position and uniform scale are authored per
instance.

The byte at offset 15 is the heading. The generator preserves it as
`deco_yaw_byte`, converts it to UE2 rotation units with `yaw * 256`, and emits
the resulting glTF node rotation. Bytes 16 and 17 remain separate compact
pitch/control and roll/control fields. Treating offset 15 as an unknown flag
makes every repeated tree face the same direction.

The placement mesh index must never fall back to a flat list of package
imports. Those indices are not equivalent, and such a fallback can assign
thousands of vegetation instances to the wrong tree.

## Known remaining work

Exact original branch-wind simulation and whole-tree far impostors have not
been recovered. Leaf-card size, pivot, placement, UVs, dimming, camera-facing
metadata, fixed fronds, and the height range needed for runtime wind are
recovered. Actual animation remains a renderer responsibility. Whole-tree far
impostors are separate from leaf cards and are not required by the current
runtime pipeline.
