# Vanguard music: data, runtime behavior, and reverse-engineering record

**Status:** living technical reference

**Last evidence review:** 2026-08-27

**Scope:** Vanguard: Saga of Heroes music, its ISACT data, its connection to
world data, and the methods used to recover the original behavior

This document is the source-of-truth overview for music work in the asset
extractor. It intentionally distinguishes facts recovered from shipped files
and binaries from interpretation. Generated manifests describe the current
data; this document explains what that data means, what has been proved at
runtime, and what remains unknown.

The short version is:

- Vanguard did not store most world music as one finished song per region.
- It stored synchronized Ogg Vorbis stems in `.isb` sample banks.
- Paired `.icb` cue bundles describe states, combinations, percentage weights,
  synchronization, entry points, and transitions.
- `VGClient.exe` chose a region, day/night family, and musical intensity. The
  ISACT runtime then selected and synchronized the authored material.
- Names such as `Idle`, `Walk`, `Run`, and `Adventure` are musical intensity
  states. They are not evidence that character velocity directly selected the
  music.
- The shipped runtime uses weighted reselection, marker waits, linear
  transition envelopes, and a one-request transition queue. Flattening the
  data into a numbered playlist loses those behaviors.

## Evidence language used here

The following terms are deliberate:

- **Verified from assets** means the value is serialized in Vanguard's shipped
  `.isb`, `.icb`, `.uax`, or world data and has been checked across the corpus.
- **Verified from the client** means it was recovered from the original
  `VGClient.exe` or `isactwin.dll`, normally by static decompilation.
- **Verified at runtime** means a focused harness observed the shipped DLL
  performing the behavior.
- **Corroborated by the SDK** means Creative's contemporary public headers,
  manuals, or tutorial project define the same structure or concept.
- **Inference** means the evidence supports the interpretation but does not
  prove every detail.
- **Unknown** means a consumer must preserve the raw value and must not silently
  replace it with a plausible guess.

Later reconstructed code, generated manifests, filenames, and decompiler
variable names are useful evidence but are not automatically authoritative.
The preferred evidence order is:

1. Shipped Vanguard data and binaries.
2. Creative's original ISACT headers, manuals, and sample projects.
3. Controlled runtime traces using Vanguard's shipped DLL and banks.
4. Contemporary third-party documentation.
5. Later parsers and reconstructions.
6. Naming-based or waveform-based inference.

## A plain-language model

It is useful to picture Vanguard's music as a live mixer rather than a folder
of ordinary songs.

An `.isb` bank contains the recorded parts: pads, percussion, pulses,
instruments, introductions, fills, and other stems. An `.icb` file contains the
instructions: which parts may play together, how likely each combination is,
when another choice may begin, and how one state should hand over to another.
The game client decides the broad situation, such as the current region,
day/night phase, or intensity family. ISACT carries out the musically timed
selection and transition.

In that analogy:

- `.isb` is the collection of recordings;
- `.icb` is the score and conducting instructions;
- `VGClient.exe` decides what the game currently needs;
- `isactwin.dll` is the conductor and scheduler;
- Vanguard's embedded OpenAL renderer is the final mixing desk.

This separation explains why extracted stems sound authentic but a simple
playlist can still sound subtly wrong. The recordings may be exact while the
selection, synchronization, transition, gain, or effects behavior is not.

## Source and binary provenance

### Vanguard client copies

The launch-disc and Sunset installations were audited separately before the
reference harness was built.

| Artifact | Launch client | Sunset client | Finding |
| --- | --- | --- | --- |
| `VGClient.exe` | SHA-256 `6d91b9927b98c4050afea4b965ed78cfbb558b30ad0e850b341e7beaa6da4f2d`; linked 2006-12-19 | SHA-256 `449334c6060749227a85ed03f93b3110c1a869b293b53803ca0f6a41e081a2ef`; linked 2014-02-05 | Different executable builds |
| `isactwin.dll` | SHA-256 `6905ee73e8d72c7ab901e99b24b8ca6d4672247285a18981f6687da4bc8361d6` | Same SHA-256 | Byte-for-byte identical |
| `Fivefold_Plains.icb` | SHA-256 `9e1d35d89daf8adf62c8a8eadcc0e77d47a64f0125fda09e879bd04103360cc0` | Same SHA-256 | Byte-for-byte identical |
| `fivefold_plains.isb` | SHA-256 `288a6f3c64bb25f2799b051532d64c7bcee05531b623b55fca6ea85d9b7ab803` | Same SHA-256 | Byte-for-byte identical |

Both Vanguard DLL copies have a November 13, 2006 PE timestamp and contain the
CodeView path:

```text
C:\ISACTSource\ISACTWin\Release\ISACTWin.pdb
```

That path establishes build provenance but does not provide the PDB. Searches
of the common public symbol servers and the exact PDB GUID found no matching
symbols. No Vanguard ISACT source, PDB, or map file has been recovered.

A later March 2007 `VGClient.exe`/`VGClient.dll` pair exists in the local source
collection without a complete matching client tree or ISACT DLL. It was not
used as a runnable binary pairing.

### Public ISACT material

The strongest public primary sources recovered so far are:

- [Creative ISACT SDK 1.6.3](https://archive.org/details/isact-sdk-163),
  including the 195-page programmer's reference, public headers, import
  libraries, DLLs, and sample integration code.
- [Creative Production Studio and SDK installer](https://archive.org/details/CreativeLabs-ISACT-163),
  including the 93-page IPS tutorial and editable sample authoring projects.
- Creative's original public header structures `STransition`, `SSyncStart`,
  `SPlayerStatus`, and `SPlayerMetricTime`.
- Creative's `Interactive3.sac` tutorial project, which supplies controlled
  serialized examples for transitions and synchronization.
- A community-preserved public 1.64 SDK, used only where its public declarations
  are compatible and corroborated by Vanguard's binary behavior.

The Vanguard DLL is not byte-for-byte the public 1.6.3 or public 1.64 build,
and its interface layout is not identical to either public SDK. Public headers
are therefore vocabulary and structure evidence, not permission to assume
every Vanguard vtable offset. The reference harness uses the offsets verified
in Vanguard's DLL.

The public 1.64 `isactwin.dll` inspected during this work has SHA-256
`12b4494e92b9f5e3178a7ac1930fa45cd2897e8dd262886a53ced2d922965399`,
which differs from Vanguard's DLL.

### What source code is and is not available

No original ISACT runtime implementation source has been found. The public SDK
contains headers and a sample integration `.cpp`, not the engine's internal
implementation. No runtime `.cpp`, `.pdb`, or `.map` is present in the public
archives.

[Legendary Explorer's ISACT helper](https://github.com/ME3Tweaks/LegendaryExplorer/blob/master/LegendaryExplorer/LegendaryExplorerCore/Sound/ISACT/ISACTHelper.cs)
and [vgmstream](https://vgmstream.org/) are valuable independent parser
references. They are later community work, not original Vanguard runtime
source.

## The four data layers

### 1. ISB: recorded sample banks

`.isb` files are RIFF containers with form type `isbf`. They hold the actual
audio payloads and sample-level metadata. Vanguard's current corpus contains
534 banks and 17,035 samples, totaling about 1.1 GB of extracted audio.

The corpus is divided into approximately:

- 222 banks under `Assets/Music/`;
- 312 banks under `Assets/Sounds/`.

The extractor has observed all 17,035 samples as Ogg Vorbis streams. It writes
the embedded stream beginning at its `OggS` header without decoding and
re-encoding it, so raw extraction is lossless with respect to the compressed
payload.

Important ISB structures include:

| Chunk/list | Recovered role |
| --- | --- |
| `LIST(samp)` | One named audio sample and its metadata |
| `titl` | Bank or sample title |
| `chnk` | Channel count |
| `sinf` | Buffer offset, time length, sample rate, PCM length, and bit depth |
| `cmpi` | Current/target codec IDs and compression information |
| `soff` | External sample offset when present |
| `data` | Compressed audio payload |
| `dtmp` | Tempo in microseconds per quarter note |
| `dtsg` | Time-signature-related metadata |
| `sync` | Synchronous start type and multiple |
| `loop` | Loop setting |
| `LIST(bfob)` | Bank-default 3D sound profile |
| `sdst` | Minimum/maximum distance, rolloff, and flags |
| `cone` | Directional cone settings; Vanguard's observed defaults are omnidirectional |
| `LIST(path)` | Segment/metric path information including knots and timestamps |
| `sbtp` | A beat/segment-pattern record observed on limited content |

The local codec labels are based on vgmstream and Legendary Explorer
conventions:

| ID | Label |
| ---: | --- |
| 0 | PCM |
| 1 | XBOX_IMA |
| 2 | OGG_VORBIS |
| 3 | WMA |
| 4 | XMA |
| 5 | MSF |
| 6 | MS_ADPCM |
| 7 | MS_PCM_BIG_ENDIAN |

These names describe the serialized IDs. The successful presence of an `OggS`
payload remains the extraction authority. If another codec appears, the tool
must preserve the metadata and bytes rather than write a misleading `.ogg`.

The extraction implementation is
[`scripts/extractors/extract_isb.py`](../scripts/extractors/extract_isb.py).
The full structural inspector is
[`scripts/extractors/inspect_isb.py`](../scripts/extractors/inspect_isb.py).

### 2. ICB: cue and behavior bundles

`.icb` files are RIFF containers with form type `icbf`. They describe how
sample-bank material is selected, combined, timed, and transitioned. The
current corpus contains:

- 535 ICB files;
- 534 case-insensitive ISB pairings;
- 218 cues classified as music;
- 317 cues classified as sound;
- 361,927 non-empty parsed nodes covering 37,035,768 payload bytes.

The parser reports 100% structural decode coverage across this corpus: every
observed chunk payload is represented by a typed decoder rather than an opaque
hex-only remainder. Structural coverage does **not** mean every field has a
final Creative-authored name or that every runtime behavior has been proved.
It means no serialized bytes are silently discarded.

Important ICB object/list types include:

| List type | Current interpretation |
| --- | --- |
| `ento` | Public entry object, commonly `Ambient`, `Combat`, or another callable entry |
| `tran` | Authored transition object, commonly `Trans1` |
| `sdri` | Selector/routing state such as `Idle`, `Walk`, `Run`, or `Adventure` |
| `sqob` | A playable combination of one or more sample stems |
| `snde` | Named sound entry, heavily used by non-music cues |
| `sdtl` | Detail/track routing, used by systems such as footsteps |
| `path` | Metric path, marker, and segment data |
| `gbef` | Global effect/control data |

Important chunks include:

| Chunk | Current interpretation |
| --- | --- |
| `ctdx` | Name/type/index directory for the cue graph |
| `info` | Object mode and related parameters; `info[0]` controls SDRI sequential/random selection |
| `rcnt` | Route target, authored order, and percentage weight |
| `data` under `sqob` | Packed references to concrete ISB sample slots |
| `sync` | Start boundary type and multiple |
| `tnst` | Six-field transition structure matching Creative's `STransition` |
| `sndt` | Fixed 728-byte sound/control records |
| `trck` | Detail-list target routes |
| `seai`, `ecac`, `selv`, `sepl`, `secl` | Entry/action/control tables with several corpus-specific layouts |

The `ctdx` directory is particularly useful. Every current ICB contains one;
its 264-byte records hold a 256-byte UTF-16LE name, a four-character object
tag, and an object index. Aggregate object counts include 13,548 `sqob`, 6,169
`snde`, 1,811 `sdri`, 338 `path`, 329 `sdtl`, 256 `tran`, 249 `ento`, and 26
`gbef` records.

SQOB titles are descriptive labels, not an authoritative way to find stems.
Consumers must use the decoded packed sample references. The extractor exposes
these as `sqob_sample_refs`, and the engine manifest carries them as
`target_sample_refs` and `target_sample_titles`.

Creative's documentation also warns against flattening different object types
into one generic playlist. ISACT queues are monophonic and may contain one
contiguous loop region; a normal stop can leave that loop and play its tail.
Timelines schedule events at absolute positions and may overlap them. Random
Sound Events use percentage chance, may deliberately leave part of the 100%
range as silence, and can be configured for ordered/random choice and
reselection on looping. Object type and serialized mode must therefore remain
part of the runtime contract.

The implementation is
[`scripts/extractors/dump_icb.py`](../scripts/extractors/dump_icb.py), with
corpus aggregation in
[`scripts/extractors/summarize_icb_semantics.py`](../scripts/extractors/summarize_icb_semantics.py).

### 3. UAX: Unreal-facing names and occasional embedded audio

`.uax` packages are the Unreal-side sound package layer. They connect names
used by game code, such as creature or combat sound sets, to the ISACT naming
system. They are important for the complete audio project but are not the main
storage or sequencing layer for adaptive world music.

The recovered UAX corpus contains 286 packages:

- 278 are primarily small string-wrapper `Sound` exports;
- 8 contain embedded WAV payloads;
- 6,190 wrapper exports and 809 embedded WAV exports were cataloged.

The embedded WAVs are extracted separately. Most UAX packages therefore act as
a lookup/naming bridge rather than a second copy of the music stems.

See [`scripts/extractors/inspect_uax.py`](../scripts/extractors/inspect_uax.py)
and [`scripts/extractors/extract_uax_wav.py`](../scripts/extractors/extract_uax_wav.py).

### 4. World music placement and region data

The emulator's `vgo_world` database preserves migrated world-audio rows from
`unreal_music` and `unreal_sound`. These records include chunk relationships,
locations, bounding boxes or radius/z-range geometry, and fields such as:

- `primaryIsactFile`;
- `secondaryIsactFile`;
- `excitementIsactFile`;
- `entryOgg`;
- `entryIntensity`;
- ambience, one-shot, emitter, and reverb fields.

The current generated snapshot contains:

- 1,394 music-volume rows, of which 1,389 join to recovered music bundles;
- 2,342 sound-volume rows, of which 2,311 join to recovered ambience bundles;
- 173 chunks with normalized engine-facing audio triggers.

The join preserves its confidence. Exact bundle or explicitly recovered
runtime-region matches can become activation rules. Name-overlap candidates
remain candidates and must not be presented as authoritative region switches.

Current `entry_intensity` values in the 1,394 music rows are:

| Value | Rows | Runtime family |
| ---: | ---: | --- |
| 0 | 720 | Ordinary land selector chooses the initial intensity |
| 3 | 1 | `Run` family |
| 4 | 1 | `Adventure` family |
| 9 | 41 | Default city |
| 10 | 199 | Alley |
| 11 | 213 | Religion |
| 12 | 65 | Pub |
| 13 | 154 | Regal |

The only current value-4 row is a Lost Canyon volume using the `Deadlands`
primary bank. The only value-3 row is in Stiirhad and lacks a primary bank in
the database snapshot. These are source-data observations, not claims that no
other runtime path can ever request the corresponding state.

The world-data pipeline is implemented by:

- [`scripts/extractors/export_world_audio_db_volumes.py`](../scripts/extractors/export_world_audio_db_volumes.py)
- [`scripts/extractors/export_world_audio_activation_manifest.py`](../scripts/extractors/export_world_audio_activation_manifest.py)
- [`scripts/extractors/export_world_audio_engine_manifest.py`](../scripts/extractors/export_world_audio_engine_manifest.py)

`m_strEntryOgg`/`entry_ogg` is a genuine reflected world-music field, but the
recovered normal `PlayIsactMusic` path resolves the primary and excitement
ISACT filenames instead of using that Ogg field to assemble adaptive sessions.
It should be preserved as authored metadata, not mistaken for the source of the
layered world-music performance.

## The generated music and ambience model

[`scripts/extractors/export_music_ambience_manifest.py`](../scripts/extractors/export_music_ambience_manifest.py)
turns the parsed cue catalog into an engine-oriented manifest. The current
snapshot contains:

- 218 music bundles;
- 45 ambience bundles;
- 215 music bundles with recognized runtime titles;
- 20 music bundles with direct chunk activation;
- 7 with explicit sea-region activation;
- 74 with review-level chunk candidates;
- 218 music bundles with an extracted audio profile.

For each music bundle it retains the paired bank, entry objects, transition
objects, known runtime state titles, SDRI metadata, weighted variants, and
resolved sample references. For ambience it organizes day/night ambience,
one-shot, special, storm, cricket, rear-ambience, and silence lanes where they
are present.

Generated files under `output/` are deliberately ignored by Git. They are
snapshots, not substitutes for this document or the parser source.

### Timing fields and loop-boundary authority

The serialized `tmcd` value commonly exposed as `0x00190028` (`1638440`) is a
generic content timebase initialized by Vanguard's ISACT runtime. The DLL
decodes its high and low halves as 25 and 40 and uses them in timebase
conversion. It is not a duration and, by itself, does not specify a 24-second
or 40-beat loop.

`dtmp` supplies tempo in microseconds per quarter note. `sync`, metric path
records, section/marker data, decoded sample length, and the player's live
metric status supply the other timing evidence. A reliable loop boundary must
come from those authored/runtime relationships rather than from `tmcd` alone.

Consumers should retain `tmcd` losslessly, use exact decoded sample duration
where appropriate, and honor authored metric and marker behavior. Any derived
human-readable duration is a convenience value, not source authority.

## What the music states mean

The original client maps numeric states to named ISACT content as follows:

| State | Day | Night | Use |
| ---: | --- | --- | --- |
| 1 | `Idle` | `Idle_Night` | Lowest ordinary world-music intensity |
| 2 | `Walk` | `Walk_Night` | Middle ordinary world-music intensity |
| 3 | `Run` | `Run_Night` | Higher ordinary world-music intensity |
| 4 | `Adventure` | `Adventure_Night` | Authored high-intensity world state |
| 5 | `1` | `2` | Special/excitement family |
| 6 | `1a` | `2a` | Special/excitement family |
| 7 | `1b` | `2b` | Special/excitement family |
| 8 | `1c` | `2c` | Special/excitement family |
| 9 | `Default_City` | `Default_City_Night` | City family |
| 10 | `Alley_Day` | `Alley_Night` | City family |
| 11 | `Religion_Day` | `Religion_Night` | City family |
| 12 | `Pub_Day` | `Pub_Night` | City family |
| 13 | `Regal_Day` | `Regal_Night` | City family |

The client uses the region's primary ISACT filename for ordinary and city
states and its excitement/secondary lane for states 5 through 8, with fallback
region data where necessary. `AFK1` is the recovered quiet fallback. Sea music
uses the named families `Bay_of_Verael`, `Cobalt_Deep`, `Emerald_Depths`,
`Jade_Sea`, `Mordeb_Sea`, `Ocean_of_Sorrow`, and `Straits_of_Thestra`.

### Walk and Run do not mean avatar movement

No recovered call path directly maps character walking speed to `Walk` or
running speed to `Run`. The names are intensity labels authored inside the
music banks.

The normal land controller is checked by the broader world-music update about
once per second. When it starts an ordinary session without a nonzero region
override, it makes an inclusive random roll from 1 to 100:

| Roll | Initial state | Probability | Initial interval |
| --- | --- | ---: | --- |
| 1-9 | Idle | 9% | 30 or 60 seconds |
| 10-59 | Walk | 50% | 60 or 90 seconds |
| 60-100 | Run | 41% | 60 or 90 seconds |

When the higher-level interval expires, the ordinary zero-override session
steps downward one intensity and uses a 30-second interval for the next step.
In plain language, this creates a timed musical comedown such as
`Run -> Walk -> Idle -> quiet`, not a soundtrack mirroring the player's gait.

A nonzero `m_EntryIntensity`/`entryIntensity` supplied by a region overrides
the randomized start. Values 1 through 4 select the corresponding ordinary
music state, and the region path schedules 30- or 60-second high-level
intervals. In the current DB snapshot, `Adventure` is therefore an explicitly
authored area intensity rather than an ordinary random choice.

These 30/60/90-second values are **VGClient session/request timers**. They are
not sample-loop lengths and do not tell the audio renderer where to cut. After
the client requests another state, ISACT may wait for the next authored metric
or marker boundary.

Other recovered high-level timing constants include:

| Purpose | Value |
| --- | ---: |
| Base scheduling unit | 30 seconds |
| Initial delay used by the timed city-family lane | 180 seconds |
| Maximum ordinary land-session accumulator | 420 seconds |
| Long state-5 wait/outro lane | 300 seconds |
| Cooldown gate | 60 seconds |
| Special-encounter threshold | 23 seconds |

These are session-control values, not generic fade durations.

### Other client music lanes

The top-level music controller also selects specialized paths rather than
forcing every situation through Idle/Walk/Run/Adventure:

- City categories 9 through 13 use a timed lane and the authored city content
  names. Without a forced refresh or region change, that lane can wait 180
  seconds before starting and later uses a 120- or 150-second threshold.
- Sea music chooses and retains one of seven hard-coded ocean bank names for a
  session, uses randomized repeat counts, and has its own cooldown behavior.
- Special encounter/combat music uses the region's excitement ISACT bank and
  states 5 through 8. A recovered 0-to-100 proximity/intensity byte biases a
  random tier choice; lower values represent the more urgent/nearer path, and
  the selected tier normally uses the 23-second encounter threshold.
- Bumper/introduction and AFK paths are separate again. An intro may be tried
  on eligible region entry, while AFK provides the deliberate quiet/outro
  behavior between sessions.

This branching is another reason not to interpret state numbers as player
locomotion. Region category, day/night state, sea state, encounter state,
session timers, and randomness all participate before ISACT receives a content
request.

The original settings layer exposes distinct `AmbientMusic` and `CombatMusic`
categories in addition to environment, creature, speech, and other effects
groups. Their existence and configuration path are verified, but the embedded
renderer's exact final category gain law remains part of the renderer unknowns.

## Weighted stem combinations

An SDRI state normally routes to one SQOB combination. An SQOB can reference
one or more synchronized ISB stems. For example, Fivefold Plains `Walk`
contains combinations such as `Pad1_Perc1`, `Pulse1_Perc2`, and larger layered
sets.

The serialized `rcnt` record contains at least:

- a target object index;
- an authored order field;
- a percentage weight.

Vanguard's DLL confirms two selector modes:

- `info[0] == 0`: sequential selection using the order field;
- `info[0] != 0`: cumulative random selection using a 1-to-100 roll and the
  percentage weights.

The runtime also supports a no-immediate-repeat flag and an `srtn`
route-to-route transition matrix. However:

- all 1,789 SDRIs found in the 222 current Music ICBs have `info[0] == 1`, so
  Vanguard's music corpus uses the weighted-random branch;
- every decoded current music route table sums to 100;
- no `srtn` chunk was found in any of the 222 Music ICBs or elsewhere in the
  current Assets ICB corpus.

The generic DLL capability must not be invented into assets that do not carry
it. Conversely, a player must not treat Vanguard's weighted music routes as a
numbered list merely because an `order` field exists.

### Runtime proof from Fivefold Plains

A 76-second trace of the shipped runtime playing Fivefold Plains `Walk`
selected one route at start and reselected at every exact 24,000 ms completed
loop:

| Elapsed | Selected route | Authored weight |
| ---: | --- | ---: |
| 0 ms | `Pad1_Perc1_Inst1` | 4 |
| 24,000 ms | `Pad1_Perc1` | 4 |
| 48,000 ms | `Pad1_Perc2_Inst1_Inst2` | 3 |
| 72,000 ms | `Perc1_Perc2_Inst1_Inst2` | 3 |

This proves loop-boundary reselection for this representative bank. It should
not be generalized into “all Vanguard cues loop every 24 seconds.” Other banks
have different sample lengths, metric data, object types, and behaviors.

## Synchronization and metric time

Creative's public `SSyncStart` structure is:

```cpp
struct SSyncStart {
    SYNC_START StartType;
    long       lMultiple;
};
```

The public enum and controlled Creative sample serialization establish:

| Value | Name | Meaning |
| ---: | --- | --- |
| 0 | `SYNC_START_IMMEDIATE` | Start without waiting for a metric boundary |
| 1 | `SYNC_START_CLOCK` | Start on a clock boundary |
| 2 | `SYNC_START_BEAT` | Start on a beat boundary |
| 3 | `SYNC_START_BAR` | Start on a bar/measure boundary |
| 4 | `SYNC_START_MARKER` | Start on an authored marker |

The second field is a multiple or resolution. Creative's tutorial sample
serializes `[2, 2]` for every two beats and `[3, 1]` for every bar.

Vanguard's DLL copies incoming content's `sync` pair into the player and waits
on live boundary flags and counters from a reference/master player. It does not
implement synchronization by sleeping for the decoded audio-file duration.

The player exposes measures, beats, clocks, tempo, time signature, total
clocks, and boundary flags. This is why a faithful replacement needs a musical
transport, not independent JavaScript or scene timers for each stem.

## Transitions

### Creative's transition model

The public `STransition` structure is exactly six 32-bit fields:

```cpp
struct STransition {
    TRANSITION_START StartType;
    float fTransitionLength;
    float fPrimaryEndTime;
    float fPrimaryEndLevel;
    float fTransitionStartTime;
    float fTransitionStartLevel;
};
```

The public start modes are:

| Value | Name | Meaning |
| ---: | --- | --- |
| 0 | `START_IMMEDIATE` | Begin the transition immediately |
| 1 | `START_MARKER` | Wait for the current content's next legal marker |
| 2 | `START_AFTER` | Begin after the primary content finishes its finite playback |
| 3 | `START_AFTER_LOOP` | Begin after the next loop/end boundary |

During the authored transition duration, the old primary gain is linearly
interpolated to `fPrimaryEndLevel` by `fPrimaryEndTime`. Incoming content
begins at `fTransitionStartTime` and `fTransitionStartLevel`, then linearly
reaches full level. The independent timings mean an author can deliberately
create overlap, a gap, or a hard handoff.

The serialized Vanguard/Creative `tnst` chunk matches this structure. A
controlled Creative tutorial transition named `TransitionOnNextBar` serializes
as `[1, 0, 0, 0, 0, 1.0]`: wait for a marker, use zero envelope time, stop the
old material, and start the new material at full level.

Creative's tutorial stresses that the Transition operation remains necessary
even when fade time is zero because it determines **when** the replacement is
allowed to start. Its interactive music example waits for a four-measure
section's next legal exit, plays a two-measure direction-specific changeover,
and then enters the destination loop. Section exits may be restricted to the
section end or exposed at bar, beat, or clock resolution.

ISACT does not time-stretch samples during playback and does not implement
tempo ramps. Source material and authored metric timing must agree.

### Vanguard's five client transition profiles

`VGClient.exe` initializes five exact six-field transition descriptors. The
first value uses the public start-mode numbering, followed by the five floats
from `STransition`:

| Profile | Start | Total | Old ends | Old target | New starts | New initial |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 0 | Marker | 2 s | 2 s | 0% | 0 s | 0% |
| 1 | After next loop/end | 0 s | 0 s | 0% | 0 s | 100% |
| 2 | Marker | 5 s | 5 s | 0% | 0 s | 0% |
| 3 | Immediate | 5 s | 5 s | 0% | 0 s | 0% |
| 4 | Immediate | 2 s | 2 s | 0% | 0 s | 0% |

The DLL converts the time fields to millisecond deadlines and the gain fields
to percentages. It executes linear ramps. These are real two- and five-second
Vanguard fades; the much longer session constants described earlier are not
fade lengths.

The exact gameplay meaning of every numeric profile at every caller is not yet
fully named. Consumer code should preserve the profile number and descriptor
rather than attach an unproved label such as “outdoor” to it.

### Pending and queued transitions

ISACT distinguishes:

- `PLAYER_PLAYING`;
- `PLAYER_PENDING_TRANSITION`, meaning the request was accepted but is waiting
  for a legal boundary;
- `PLAYER_TRANSITIONING`, meaning the envelope is active.

Vanguard's DLL has a one-deep pending transition queue. A new request arriving
during the late transition/crossfade state is retained rather than cancelling
the active transition. When the current transition completes, the queued
request is promoted and waits for its own synchronization boundary.

The Fivefold reference trace demonstrated this sequence:

| Time | Event |
| ---: | --- |
| 5.001 s | Client requests `Run` with profile 0 |
| 5.021 s | Player reports pending transition |
| 6.000 s | Runtime selector starts `Run` at the marker |
| 6.007 s | Two-second transition reports active |
| 6.510 s | `Adventure` profile 2 is queued without cancelling `Run` |
| 8.006 s | `Run` becomes primary; queued request is promoted and waits |
| 9.000 s | Runtime selector starts `Adventure` at the next marker |
| 9.002 s | Five-second transition reports active |
| 14.004 s | Player returns to ordinary playing state |

The captured metric was 100 BPM, 96 clocks per beat, and 5/4. A bar was three
seconds, matching the 6- and 9-second selector events. The request timestamp
was not the audible start timestamp.

## Renderer boundary

The music behavior and final audio rendering have different evidence levels.

Both the launch client's standalone `AudioDrv.dll` and Creative's public audio
driver reject the codec IDs queried for Vanguard's bank data in the focused
harness. The working OpenAL/ISACT renderer is embedded inside `VGClient.exe`;
it does not expose a standalone renderer entry point that can simply be linked
into a small test program.

Therefore the reference harness can authoritatively observe the shipped
`isactwin.dll` deciding:

- which route is selected;
- metric time and boundary flags;
- player status;
- when a transition becomes eligible;
- which linear gain commands are issued;
- how the one-deep queue behaves.

For waveform comparison, the selected losslessly extracted Ogg stems are mixed
from that exact command trace. The stems are averaged only to prevent clipping.
That output is an authoritative **continuity and timing reference**, not proof
of Vanguard's final gain law, EQ, reverb, limiter, or other embedded-renderer
effects.

In nontechnical terms: we know which original musical parts Vanguard chose and
when it told them to start, stop, and fade. We do not yet claim to have copied
every final tonal adjustment applied by the full game client.

## Fivefold Plains as the gold-standard fixture

Fivefold Plains is the current representative fixture because its source
assets are identical across the launch and Sunset clients, its stems have
clean 24-second lengths, and it exercises weighted routing plus state
transitions.

Its day and night world states include `Idle`, `Walk`, `Run`, and `Adventure`.
The current manifest shows:

- `Idle`: one route with weight 100;
- `Walk`: 29 weighted combinations, with weights 4 or 3 summing to 100;
- `Run`: 34 weighted combinations summing to 100;
- `Adventure`: 13 weighted combinations summing to 100;
- `info = [1, 50, 1, 0]`, selecting the runtime's weighted branch;
- `sync = [4, 1]`, meaning marker synchronization with multiple 1;
- entry object `Ambient` and transition object `Trans1`.

The fixture settles several general design requirements but does not prove that
every Vanguard bank has the same cadence or metric layout.

## Reverse-engineering methods

### Corpus-wide RIFF parsing

Both ISB and ICB are walked as aligned RIFF trees. Every node retains its chunk
ID, offset, size, container relationship, and decoded representation. Coverage
is measured by both node count and byte count. Variant record layouts are
decoded structurally rather than hidden behind a successful top-level parse.

Corpus-wide aggregation is important because a layout that works for one zone
may fail on ambience, combat, crafting, footsteps, or weather content. The
parser was extended only after grouping repeated byte patterns across all
files.

### Cross-layer name and reference correlation

Names recovered from client code, UAX packages, ICB directory records, and ISB
sample titles are normalized and compared. Packed numeric references are
preferred over guessed title splitting. Case-insensitive output stems include
the source directory (`Music__...` or `Sounds__...`) to avoid collisions on
case-insensitive filesystems.

### Controlled serialization with Creative projects

Creative's editable tutorial projects provide known authoring inputs. Parsing
their output lets us prove that `tnst` is `STransition`, that `sync` uses the
public enum/multiple layout, and which tags identify entry and transition
objects. This is stronger than assigning names from Vanguard patterns alone.

### Static analysis of `VGClient.exe`

Targeted Ghidra analysis recovered:

- the state-to-content-name resolver;
- day/night selection;
- region and `m_EntryIntensity` use;
- land, city, sea, encounter, AFK, and bumper music lanes;
- exact session timing constants;
- calls into the ISACT manager;
- the five transition descriptors;
- source-path and property-name evidence for Vanguard's world music objects.

Raw decompiler names are replaced with friendly names only after callers,
fields, and behavior support the interpretation. The underlying addresses and
raw decompilation remain available in the sibling `vanguard-research`
repository.

### Static analysis of Vanguard's `isactwin.dll`

The shipped DLL was imported into a disposable Ghidra project. Its only public
export is `RetrieveISACTInterface`, so useful behavior was recovered through
interface/vtable tracing. This established:

- the actual player interface slots used by Vanguard;
- transition descriptor arithmetic and linear ramps;
- pending and active player states;
- the one-deep transition queue;
- SDRI random versus sequential selection;
- percentage weights and optional no-repeat handling;
- live metric-boundary synchronization;
- `tmcd`'s generic default timebase initialization.

### Focused 32-bit runtime harness

The gold-standard harness in the sibling research repository stages exact
hashed inputs, builds a 32-bit test executable, runs it under Proton, and logs
player status, metric time, selectors, requests, and transitions. The SDRI
selector hook has a signature guard so it fails closed if used with an
unexpected DLL build.

The harness is intentionally narrow. It calls the real content/player runtime
without pretending the public SDK DLL is interchangeable with Vanguard's
custom build.

### Reference waveform validation

The exact runtime selector trace drives a lossless-stem continuity render.
Candidate playback engines can be inspected for silence, discontinuity, and
envelope timing around the same logical boundaries. Comparison should use
boundary behavior rather than sample subtraction because weighted selection
may choose different valid stem combinations.

### World-data joins with explicit confidence

Database music/sound volumes are normalized into boxes or radius/z-range
shapes, then joined to recovered bundles. Exact names, explicit aliases, sea
families, token subsets, and weak candidates remain distinguishable. A weak
name resemblance must never become a silent production activation rule.

## Required behavior for a faithful replacement

A replacement runtime consuming this extractor's output should:

1. Resolve SQOB stems from packed sample references, not title parsing.
2. Select one SDRI route according to `info[0]`, order, weights, and flags.
3. Reselect at the authored object's completed loop boundary, not a global
   guessed interval.
4. Start all stems in one combination on a shared sample-accurate clock.
5. Preserve shared stems where the authored transition permits continuity.
6. Maintain musical metric time and honor `sync` boundary type/multiple.
7. Keep a requested state pending until the legal marker or completion event.
8. Apply the exact transition descriptor with linear gain envelopes.
9. Retain one queued transition request instead of cancelling an active fade.
10. Keep client session timers separate from musical loop and marker timing.
11. Preserve raw/unknown fields and report unsupported semantics visibly.
12. Treat final gain, EQ, spatial effects, and limiting as separate renderer
    questions until verified.

## Extraction and regeneration

The ordinary audio command extracts embedded UAX WAVs, ISB Ogg samples, and
dumps ICB cue metadata:

```bash
python3 vanguard.py extract-audio
```

Equivalent focused commands are:

```bash
python3 scripts/extractors/extract_uax_wav.py "$VANGUARD_ASSETS_PATH" --glob '*.uax'
python3 scripts/extractors/extract_isb.py "$VANGUARD_ASSETS_PATH" --glob '*.isb'
python3 scripts/extractors/dump_icb.py "$VANGUARD_ASSETS_PATH" --glob '*.icb'
```

The full ISB structural inspection and semantic aggregation are separate
diagnostic stages:

```bash
python3 scripts/extractors/inspect_isb.py "$VANGUARD_ASSETS_PATH" --out output/audio/inspect_isb
python3 scripts/extractors/summarize_icb_semantics.py \
  --catalog output/audio/icb/cues.json \
  --out output/audio/icb_semantics_summary
```

Output folder names may be overridden. Directory-input outputs include the
source path in their generated stem so identically named Music and Sounds banks
remain distinct on case-insensitive filesystems.

After the cue catalog and ISB inspection exist, generate the engine-oriented
bundle manifest:

```bash
python3 scripts/extractors/export_music_ambience_manifest.py \
  --catalog output/audio/icb/cues.json \
  --isb-inspect output/audio/inspect_isb \
  --chunk-reference /path/to/vgo-server-emulator-wiki/Reference/Chunks.md \
  --out output/audio/music_ambience_manifest
```

The chunk reference is an external provenance dependency and is not committed
in this extractor checkout, so standalone use must supply that argument
explicitly.

The world-volume stages require access to the populated `vgo_world` database:

```bash
python3 scripts/extractors/export_world_audio_db_volumes.py
python3 scripts/extractors/export_world_audio_activation_manifest.py
python3 scripts/extractors/export_world_audio_engine_manifest.py
```

Generated Markdown summaries currently include:

- `output/audio/music_ambience_manifest/manifest.md`;
- `output/audio/world_audio_db_volumes/manifest.md`;
- `output/audio/world_audio_activation_manifest/manifest.md`;
- `output/audio/world_audio_engine_manifest/manifest.md`.

They are concise views of generated JSON, not hand-maintained documentation.

## Known unknowns and limitations

The following questions remain open or only partially closed:

- The complete semantic names for every field in `seai`, `ecac`, `selv`,
  `sepl`, `secl`, and related action tables.
- Exact behavior for every ICB object type and every non-world-music cue, even
  though their bytes are structurally decoded.
- The authored loop/reselection rule for every bank. Fivefold proves one
  representative case, not a universal 24-second rule.
- Exact caller-context names for every Vanguard transition profile.
- Complete `dsec` bitfield and section-exit semantics in Vanguard's build.
- The final embedded renderer's gain law, category-volume application, EQ,
  effects, reverb, limiting, and codec-specific behavior.
- Exact client-derived region geometry when the emulator DB row is missing or
  ambiguous.
- Whether all launch-era and Sunset-era music assets outside the verified
  fixture are identical; the shared runtime and Fivefold fixture are confirmed.
- The remaining behavioral difference between Vanguard's custom DLL interface
  and the public 1.64 ABI outside the methods exercised by the harness.

Absence of a recovered field or runtime path is not permission to invent one.
Where exact historical behavior is unavailable, consumers should use a small,
documented approximation and keep it separable from extracted truth.

## Interpretation constraints

The recovered evidence imposes the following constraints on implementations
and further analysis:

- `tmcd = 1638440` does **not** directly encode a 24-second phrase duration.
- The 30/60/90-second client intervals are not musical phrase lengths.
- The 300- and 420-second constants are not crossfade durations.
- `Idle`, `Walk`, and `Run` are not proved locomotion-speed bindings.
- Music SDRI variants are not sequential “song sections” merely because they
  have order fields.
- A generic crossfade beginning at a decoded sample boundary is not equivalent
  to ISACT's marker wait and transition profile.
- Public ISACT 1.6.3 or 1.64 binaries are not drop-in replacements for
  Vanguard's shipped runtime.
- The lossless-stem reference mix is not a claim about the full client's final
  EQ or effects.

## Primary and supporting references

### In this repository

- [`scripts/extractors/extract_isb.py`](../scripts/extractors/extract_isb.py)
- [`scripts/extractors/inspect_isb.py`](../scripts/extractors/inspect_isb.py)
- [`scripts/extractors/dump_icb.py`](../scripts/extractors/dump_icb.py)
- [`scripts/extractors/summarize_icb_semantics.py`](../scripts/extractors/summarize_icb_semantics.py)
- [`scripts/extractors/export_music_ambience_manifest.py`](../scripts/extractors/export_music_ambience_manifest.py)
- [`scripts/extractors/export_world_audio_db_volumes.py`](../scripts/extractors/export_world_audio_db_volumes.py)
- [`scripts/extractors/export_world_audio_activation_manifest.py`](../scripts/extractors/export_world_audio_activation_manifest.py)
- [`scripts/extractors/export_world_audio_engine_manifest.py`](../scripts/extractors/export_world_audio_engine_manifest.py)

### Sibling research repository

The detailed decompilation artifacts and reference harness live in
`/home/brynn/Code/vanguard-research`, especially:

- `docs/audio/2026-08-27_fivefold_isact_reference_harness.md`;
- `tools/isact_reference_harness/`;
- `ghidra/output/named_decomp_readable.md`;
- `ghidra/output/celestial_music_runtime_probe.md`.

### External primary and independent sources

- [Creative ISACT SDK 1.6.3 archive](https://archive.org/details/isact-sdk-163)
- [Creative Production Studio and SDK installer](https://archive.org/details/CreativeLabs-ISACT-163)
- [Creative's 2004 ISACT announcement](https://sg.creative.com/corporate/pressroom?id=12009)
- [Legendary Explorer ISACT helper](https://github.com/ME3Tweaks/LegendaryExplorer/blob/master/LegendaryExplorer/LegendaryExplorerCore/Sound/ISACT/ISACTHelper.cs)
- [vgmstream](https://vgmstream.org/)

## Recommended next step

Define the shared playback contract around the recovered source behavior:
weighted SDRI route sets, authoritative loop completion, an authored musical
transport, synchronized transition requests, exact linear envelopes, and the
one-deep queue. Renderer gain, EQ, and effects can remain a separate fidelity
layer.
