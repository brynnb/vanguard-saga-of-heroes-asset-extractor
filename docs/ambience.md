# Vanguard ambience: world volumes, ISACT data, and runtime behavior

**Status:** living technical reference

**Last evidence review:** 2026-08-30

**Scope:** Vanguard: Saga of Heroes environmental ambience, its world-volume
placement, ISACT data, recovered runtime behavior, extraction model, and
requirements for replacement players

This is the source-of-truth overview for ambience work in the asset extractor.
The related [`music.md`](music.md) document covers the shared ISB/ICB container
foundations, original client provenance, public Creative ISACT material, and
the adaptive music controller. This document concentrates on what is distinct
about environmental sound.

## Plain-language summary

Vanguard did not usually assign one ambience recording to an entire zone.
World data placed many three-dimensional sound volumes and localized emitters
inside each chunk. Entering an area volume activated an authored ISACT ambience
entity. That entity could combine:

- one or more persistent background layers;
- separate front/rear or otherwise spatially distinct routes;
- day and night alternatives;
- weighted one-shot wildlife, weather, creaks, waves, and other events;
- independently controlled special events;
- deliberate intervals of silence;
- auxiliary families such as storms, crickets, or insects.

A town volume, coastal strip, cave, room, or water emitter therefore describes
where a sound behavior applies. The paired ICB and ISB describe what happens
while it applies. Flattening all samples into simultaneous loops loses the
selection, quiet-time, spatial, and control behavior that made the original
soundscape feel alive.

## Evidence and provenance

The evidence terms and source priority defined in [`music.md`](music.md#evidence-language-used-here)
also apply here. In brief:

1. shipped Vanguard data and binaries are authoritative;
2. original Creative documentation supplies compatible structure and vocabulary;
3. controlled traces against Vanguard's shipped DLL establish runtime behavior;
4. generated manifests are structured interpretations and preserve uncertainty;
5. naming-based inference must not silently become an activation rule.

The launch-disc and Sunset client copies of `isactwin.dll` used by this work are
byte-for-byte identical. Their SHA-256 is
`6905ee73e8d72c7ab901e99b24b8ca6d4672247285a18981f6687da4bc8361d6`.
The executable builds differ, so each client tree was audited before choosing
the matching executable/DLL pair used by the reference harness.

## The ambience data path

Four layers participate in a complete ambience reconstruction:

1. World sound rows say where a behavior is active and expose its controls.
2. ICB cue banks describe entities, Sound Events, routes, silence, and paths.
3. ISB sample banks contain the losslessly extracted Ogg Vorbis recordings and
   their 3D defaults.
4. The runtime selects, schedules, positions, attenuates, and mixes the result.

The primary generated outputs are:

- `output/audio/world_audio_db_volumes/manifest.json` — normalized source rows;
- `output/audio/world_audio_activation_manifest/manifest.json` — bundle joins
  with explicit confidence;
- `output/audio/world_audio_engine_manifest/manifest.json` — engine-facing
  chunk triggers and geometry;
- `output/audio/music_ambience_manifest/manifest.json` — playable ambience
  bundles, lanes, routes, and resolved samples.

Generated outputs are ignored snapshots. Parser and exporter code plus this
document define the maintained contract.

## World-volume activation

The recovered `unreal_sound` data is spatial. The current engine manifest
contains 2,342 sound triggers across 173 chunks; 2,311 resolve to recovered
ambience bundles. Its geometry is distributed as follows:

| Shape | Rows | Meaning in the normalized model |
| --- | ---: | --- |
| `bbox` | 372 | Axis-aligned minimum/maximum box |
| `radius` | 564 | Radius around a center point |
| `radius-zrange` | 1,406 | Horizontal radius with an independent vertical range |

The same manifest contains 1,782 area triggers and 560 records marked as
emitters.

### Area volumes versus emitters

An area volume represents the environment heard while the listener is inside
its shape. Examples include a town, plain, cave, room, canyon, coast, or
volcanic region. It can replace the broader environment when the listener
moves into a more specific authored space.

An emitter represents a localized source, such as nearby water. It is an
overlay positioned in the world, not a replacement for the complete regional
ambience. A replacement runtime must not turn every emitter in a chunk into a
full-area soundtrack.

These records preserve:

- chunk identity and display name;
- source row ID;
- normalized geometry;
- primary ISACT entity name and resolved bundle;
- `is_emitter`;
- one-shot and special-ambience controls;
- reverb type ID and rotation;
- available day/night lanes and auxiliary titles.

World-volume membership is not the same as chunk membership. Loading a chunk
only makes its triggers available. The listener's position determines which
area volume or localized emitters apply. The exact original priority rule for
every possible overlap is not yet fully recovered, so consumers should retain
the source geometry and make any approximation explicit.

## ICB ambience entities

The current generated corpus contains 45 ambience bundles and 600 classified
ambience Sound Events. Forty-four bundles expose an audio profile and recovered
distance-attenuation data.

Ambience commonly uses these ICB concepts:

| Object or field | Recovered role |
| --- | --- |
| `ento` | Callable entity and its control/action graph |
| `snde` | Sound Event containing sound and/or silence routes |
| `sndt` | Fixed-size route/control records, including sample and path routing |
| `silt` | Silence route with order, weight, and duration bounds |
| `info[0]` | Ordered versus weighted-random selection mode |
| `sync` | Synchronous-start type and multiple |
| `loop` | Serialized loop behavior retained for the runtime |
| `path` | Authored spatial or metric path data |

The extractor emits the versioned `vanguard-isact-ambience-v1` runtime model.
It is intentionally richer than a list of audio filenames. Each Sound Event
retains:

- ordered or weighted-random mode;
- every route's authored order and percentage weight;
- resolved ISB sample title and index where known;
- spatial path index;
- unresolved control-window records rather than guessed sample assignments;
- silence order, percentage, minimum duration, maximum duration, and raw flag;
- raw `info`, `sync`, and loop fields.

## Channel families and controls

The normalized model keeps independently controlled families separate:

| Family | Typical behavior |
| --- | --- |
| `ambience` | Persistent bed or authored repeating background Sound Event |
| `one_shots` | Weighted spatial events separated by authored quiet intervals |
| `special` | A separately enabled weighted event family |
| `auxiliary` | Rear ambience, storms, crickets, insects, silence, and other targets |

The bundle-level `runtime_model.selectors` contains only controls actually
exposed by that bundle:

- `TimeOfDay` when day/night families exist;
- `OneShots` when the corresponding event family exists;
- `SpecialAmbience` when separately controlled special events exist;
- `Storms` when a storm auxiliary event exists.

Absence of a selector is meaningful. A consumer should not invent a day/night
switch, storm lane, or special-event toggle for a bundle that does not expose
one.

## Weighted sounds and authored silence

The current ambience model contains 5,607 routed sound choices and 179 Sound
Events with explicit silence routes. A random Sound Event selects by its stored
integer percentages. The total may be less than 100%; Creative's format allows
the uncovered range to remain silent.

For the 179 recovered five-field silence records, 178 combine with their sound
weights to total exactly 100. `GlobalProps.DoorCreakWood` totals 75 and remains
unchanged. The exporter reports totals diagnostically rather than normalizing
them and changing the authored probabilities.

A `silence_route` is an event choice, not a generic delay after every file. Its
minimum and maximum durations are evaluated each time that route is selected.
Common observed bounds include 6–10 seconds and 8–14 seconds. Explicit
three-second `Silence` events also appear as entity action targets.

## Spatial paths and attenuation

ISB banks preserve a bank-default 3D profile through structures such as
`LIST(bfob)` and `sdst`, including minimum distance, maximum distance, rolloff,
and flags. ICB Sound Event records select path indices for individual routes.
Many ambience families deliberately use different indices for their primary
and rear layers.

This distinction matters:

- two routes using the same audio sample are not necessarily duplicates;
- their separate path indices can place or move them differently;
- the original renderer attenuated each source by distance;
- stereo panning without distance attenuation can make short wildlife loops
  much more prominent than they were in the world.

The extractor preserves path indices and raw path/control records. Not every
path knot and every final renderer gain operation has a complete semantic name,
so consumers must not replace that uncertainty with a claim of exact 3D parity.

## Original-runtime Canyon reference

A four-minute reference run used Vanguard's shipped Sunset executable/DLL pair
and the original Canyon banks:

| Input | SHA-256 |
| --- | --- |
| `VGClient.exe` | `449334c6060749227a85ed03f93b3110c1a869b293b53803ca0f6a41e081a2ef` |
| `isactwin.dll` | `6905ee73e8d72c7ab901e99b24b8ca6d4672247285a18981f6687da4bc8361d6` |
| `AmbienceCanyon.icb` | `479c51e428e33e2168bf533d03e10f19a5b79bb0dbe4becb9e3201dff49089b2` |
| `ambiencecanyon.isb` | `1807df0fbb60227eda671884a45067e714b522388271725cce3d1b74523bb6ff` |

The runtime trace demonstrated:

- two simultaneous, spatially distinct Canyon background tracks;
- weighted bird-call selection after serialized quiet intervals;
- daytime quiet intervals within the authored 6–10 second bounds;
- independently controlled special ambience;
- 6–10 second daytime and 8–14 second nighttime special quiet intervals;
- prompt replacement of day/night beds after `TimeOfDay` changes;
- already scheduled outgoing one-shots continuing after incoming content began;
- an authored `Silence` Sound Event when special ambience was turned off.

The harness recorded route, timing, gain, and spatial commands. Its standalone
renderer could not decode Vanguard's custom sample codec, so this proves engine
scheduling and spatial commands rather than the final EQ, effects, or speaker
mix embedded in `VGClient.exe`.

Detailed trace artifacts and reproduction commands live in the sibling
`vanguard-research` repository under
`docs/audio/2026-08-27_canyon_ambience_reference_harness.md` and
`tools/isact_reference_harness/`.

## Representative authored behaviors

### Town and village

`AmbienceTownVillage` is placed by 37 area volumes across 28 current chunks.
The shapes range from settlement-scale radius/z-range volumes to large boxes in
places such as Ahgram, Leth Nurae, and New Targonor. It is not a chunk-wide
default merely because its bundle can be selected globally in a preview tool.

Both day and night primary beds route to `TownVillageInsect3.wav` through path
indices 0 and 1. `RearAmbience` routes the same sample through path indices 2
and 3. The Town/Village bank's copy of that sample is approximately 0.638
seconds long. Its persistence is therefore authored, while its perceived
prominence still depends on spatial placement, distance attenuation, and the
final renderer.

Day one-shots provide eight bird samples. Night one-shots provide birds, frogs,
and insects. Their explicit silence routes use 8–14 second duration bounds.
Additional `DayInsects` and `NightInsects` auxiliary targets remain separate
from the main and rear beds.

### Coastal and dock

Coastal and Dock demonstrate why filename length is not a reliable lane
classifier. Their `DayAmbience` and `NightAmbience` objects are weighted Sound
Events with a 33% silence route lasting 2–5 seconds, not unconditional beds
that simply loop every resolved file forever.

Coastal chooses between `CricketChirp1.wav`, `Wave_Small_08.wav`, and silence
for the main background event. Dock uses `DockLappingLoop1.wav` and silence.
Their ordinary one-shots have separate 8–14 second quiet intervals, and their
auxiliary rear/cricket families remain independent.

### Volcanic

Volcanic has one primary day/night bed target,
`VolcanicRockSlide3.wav`, and its `RearAmbience` uses that same recording on
different spatial paths. This repeated bed is authored. Variety comes from
separate eruption, lava-bubble, and rock-slide one-shots with 8–14 second quiet
intervals rather than from multiple primary bed files.

## Consumer contract

A faithful consumer should:

1. Activate area ambience from listener containment in source-backed world
   geometry rather than from chunk name or soundtrack name.
2. Treat emitters as localized overlays, not replacement area environments.
3. Keep background, one-shot, special, and auxiliary families independent.
4. Start every authored active background layer and retain its spatial route.
5. Choose Sound Event routes using serialized mode, order, and percentages.
6. Preserve uncovered probability and explicit silence instead of renormalizing.
7. Draw a fresh duration within authored silence bounds for each silence choice.
8. Switch day/night and optional families without restarting unrelated lanes.
9. Allow an active one-shot tail to finish unless recovered action data requires
   an immediate stop.
10. Apply distance attenuation and authored paths in a 3D client.
11. Preserve raw unknown fields and expose unsupported behavior diagnostically.
12. Keep final EQ, effects, reverb, limiting, and category gain separate from
    verified scheduling behavior.

## Website and game-client previews

A website without a controllable 3D avatar cannot literally evaluate listener
containment. A useful public preview may begin inside a real sound volume and
move among nearby authored area volumes to suggest travel, provided it labels
that movement as a preview approximation. Such a preview should use source
geometry, exclude emitters from replacement-area selection, avoid immediate
backtracking, and crossfade between areas.

A 3D game client should use the actual listener position instead. It should
evaluate area containment continuously, layer localized emitters, apply
priority rules, and preserve active event tails across appropriate area or
selector changes.

Neither consumer should claim the website's stereo approximation or a modern
engine's default attenuation curve is the exact original Vanguard renderer.

## Extraction and regeneration

Extract and inspect source audio and cue banks with:

```bash
vanguard-assets extract-audio
python3 scripts/extractors/inspect_isb.py "$VANGUARD_ASSETS_PATH" \
  --out output/audio/inspect_isb
python3 scripts/extractors/summarize_icb_semantics.py \
  --catalog output/audio/icb/cues.json \
  --out output/audio/icb_semantics_summary
```

Generate the playable music/ambience model with:

```bash
python3 scripts/extractors/export_music_ambience_manifest.py \
  --catalog output/audio/icb/cues.json \
  --isb-inspect output/audio/inspect_isb \
  --chunk-reference /path/to/vgo-server-emulator-wiki/Reference/Chunks.md \
  --out output/audio/music_ambience_manifest
```

Generate world activation and engine-facing volume data with:

```bash
python3 scripts/extractors/export_world_audio_db_volumes.py
python3 scripts/extractors/export_world_audio_activation_manifest.py
python3 scripts/extractors/export_world_audio_engine_manifest.py
```

The exporter implementation is primarily in:

- [`scripts/extractors/export_music_ambience_manifest.py`](../scripts/extractors/export_music_ambience_manifest.py)
- [`scripts/extractors/export_world_audio_db_volumes.py`](../scripts/extractors/export_world_audio_db_volumes.py)
- [`scripts/extractors/export_world_audio_activation_manifest.py`](../scripts/extractors/export_world_audio_activation_manifest.py)
- [`scripts/extractors/export_world_audio_engine_manifest.py`](../scripts/extractors/export_world_audio_engine_manifest.py)

Focused ambience model coverage is in
[`tests/test_ambience_runtime_model.py`](../tests/test_ambience_runtime_model.py).

## Known unknowns

- Complete semantic names and control flow for every ICB entity action table.
- The exact priority and blending rule for every arrangement of overlapping
  world sound volumes.
- Full decoding of every spatial path knot and control-window layout.
- Exact stop/tail behavior for every entity action beyond the Canyon fixture.
- The final embedded renderer's distance curve, category gain, EQ, reverb,
  effects, limiting, and speaker mix.
- Whether every launch-era and Sunset ambience asset outside the checked
  fixtures is identical.
- Exact behavior for unresolved world rows and weak name-based bundle joins.

These unknowns are reasons to preserve source fields, not invitations to hide
missing behavior behind plausible defaults.

## References

- [`music.md`](music.md) — shared ISACT foundations and adaptive music behavior
- [Creative ISACT SDK 1.6.3 archive](https://archive.org/details/isact-sdk-163)
- [Creative Production Studio and SDK installer](https://archive.org/details/CreativeLabs-ISACT-163)
- `vanguard-research/docs/audio/2026-08-27_canyon_ambience_reference_harness.md`
- `vanguard-research/tools/isact_reference_harness/`
