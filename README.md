# Vanguard: Saga of Heroes Asset Extractor

[![CI](https://github.com/brynnb/vanguard-saga-of-heroes-asset-extractor/actions/workflows/ci.yml/badge.svg)](https://github.com/brynnb/vanguard-saga-of-heroes-asset-extractor/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[Vanguard: Saga of Heroes](https://en.wikipedia.org/wiki/Vanguard:_Saga_of_Heroes) was a 2007 fantasy MMORPG built on a heavily customized Unreal Engine 2.5 and many other tools. This asset extractor project extracts, converts, and exports all of the 3D models, animations, audio, and many other files from the original game client that users would install to play. The goal of this process is to recreate a modern, cross-platform, performant, customizable client for Vanguard and have it run with an emulated server. Vanguard was officially shut down in 2014 by its current publisher and is now unplayable outside of a (very awesome but unaffiliated) dedicated server emulation project. Its users can currently only play by running a very dated, Windows-only original game client.

This project is the extraction side of my other project, [Vanguard: Eternal Sagas](https://www.eternalsagas.com/), which implements these assets in a modern engine. Both sides of this work are the result of hundreds of hours of reverse engineering, parser writing, binary inspection, format reconstruction, and reimplementation into a new engine. The Eternal Sagas website also has multiple pages showing the assets that this project extracts, including the character models, music, 3D world, and world objects.

<table>
  <tr>
    <td width="50%">
      <img src="https://github.com/user-attachments/assets/c422ea53-8a68-4c16-a5d6-6d81cba06ffd" alt="Eternal Sagas client rendering a tree-lined Vanguard city and temple">
    </td>
    <td width="50%">
      <img src="https://github.com/user-attachments/assets/4094dd37-fba7-47cf-81fd-7e5cad36872e" alt="Extracted black dragon model displayed in the character viewer">
    </td>
  </tr>
  <tr>
    <td width="50%">
      <img src="https://github.com/user-attachments/assets/7722fce1-310f-4bf4-9613-4340e4510017" alt="Extracted two-handed hammer displayed in the object viewer">
    </td>
    <td width="50%">
      <img src="https://github.com/user-attachments/assets/a42734de-c826-48ba-9018-c59ae05c3efd" alt="Extracted Vanguard terrain displayed in the world viewer">
    </td>
  </tr>
</table>

This is a fan-made preservation and recreation project and is not affiliated with, endorsed by, or connected to Daybreak Game Company LLC or any of its subsidiaries. Vanguard: Saga of Heroes is a registered trademark of Daybreak Game Company LLC. This repository does not distribute Vanguard client files, extracted game assets, generated output, or any other copyrighted Vanguard asset content.

## What It Extracts

The original Vanguard client data spans UE2 packages, Vanguard-specific mesh
and animation formats, terrain chunks, audio archives, prefab/object archives,
text resources, and one-off binary formats. This project currently extracts:

- terrain geometry, textures, layer masks, vegetation-shadow maps, and chunk
  transforms from `.vgr` files;
- static meshes and UE2 material graphs to glTF with package-qualified identity,
  collision metadata, and surface classification;
- character and creature meshes, playable race data, customization controls,
  item appearances, and attachment groups;
- EMotion FX and UE2 skeletal animations, including hand poses and playable
  facial-control sidecars;
- UAX, ISB, and ICB audio samples, cues, behavior, and world-music metadata;
- SGO prefabs, rooms, portals, particles, and world-object placement data;
- shader-to-texture relationships and diffuse PNGs when the optional
  Unreal-Library helper is available.

## Documentation

- [Static mesh extraction contract](docs/static_meshes.md)
- [Character and animation extraction](docs/characters.md)
- [Music data and recovered ISACT behavior](docs/music.md)
- [Ambience data, world volumes, and recovered runtime behavior](docs/ambience.md)
- [SpeedTree extraction and runtime contract](docs/speedtree.md)
- [Interior room and portal authority](docs/interior_rooms_and_portals.md)
- [Advanced extraction workflows](docs/workflows.md)
- [Generated output and database](docs/output_and_database.md)

## Setup

Python 3.10 or newer is required. Create a virtual environment and install the
package in editable mode:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

For development and distribution-building tools, install the `dev` extra:

```bash
python -m pip install -e ".[dev]"
```

Download the original game client files from the
[VGO Emulator project](https://vgoplayers.com/downloads.php?tab=client). Any of
the “VG Full Client” downloads work. Extract the `Vanguard EMU` directory to one
of the default locations:

```text
./Vanguard EMU
~/Downloads/Vanguard EMU
```

The repository-local path is checked first. For another location, configure it
explicitly:

```bash
export VANGUARD_EMU_PATH="/path/to/Vanguard EMU"
export VANGUARD_ASSETS_PATH="$VANGUARD_EMU_PATH/Assets"
```

Generated data is written to the current workspace by default. Set
`VANGUARD_WORKSPACE_ROOT` when output should be rooted elsewhere.

## Quick Start

Preview the full extraction graph without running any child stages:

```bash
vanguard-assets extract-all --dry-run
```

Initialize the database and client-file index as a small smoke test:

```bash
vanguard-assets setup --db --files
```

Run the complete extraction pipeline:

```bash
vanguard-assets extract-all
```

Run individual public stages:

```bash
vanguard-assets setup --reset
vanguard-assets build-shaders
vanguard-assets extract-terrain
vanguard-assets export-meshes
vanguard-assets export-characters
vanguard-assets export-animations --workers 4
vanguard-assets export-facial-controls
vanguard-assets export-npc-assembly
vanguard-assets extract-audio
vanguard-assets extract-world
```

Use `vanguard-assets <command> --help` for stage-specific paths, worker counts,
filters, and optional outputs. The root `vanguard.py` entry point remains a
repository-checkout compatibility wrapper; new documentation and integrations
should use the installed `vanguard-assets` command.

Lower-level world, Godot, particle, NPC, and cache-management commands are
documented in [advanced extraction workflows](docs/workflows.md).

## Optional Unreal-Library Support

Most parsing is native Python. Unreal-Library is an optional helper for map
reference text decompilation and material/shader enumeration:

```bash
vanguard-assets fetch-unreal-library
```

The full pipeline detects the built helper automatically. Pass
`--skip-unreal-library` when it is intentionally unavailable. See
[advanced extraction workflows](docs/workflows.md#optional-unreal-library-support)
for the exact boundary.

## Output

Generated terrain, meshes, characters, animations, audio, catalogs, indexes,
and runtime assets are written under `output/`, which is ignored by Git. The
pipeline also maintains `output/data/vanguard_data.db` as a generated working
index.

See [generated output and database](docs/output_and_database.md) for directory
ownership, database tables, terrain shadow semantics, and runtime outputs.

## Areas Remaining

- Finish mapping music cue selection, transition requests, and zone-specific
  activation against the recovered ISACT behavior documented in
  [the music record](docs/music.md#known-unknowns-and-limitations).
- Expand exact extraction coverage for water shader/material graphs without
  replacing missing source behavior with filename heuristics.
- Complete particle and VFX extraction for unresolved glow, sparkle, emissive,
  spell, and other specialized emitter behavior.
- Finish source-backed character assembly, animation-use mapping, and remaining
  mesh/texture corrections described in
  [character extraction](docs/characters.md#remaining-extraction-work).

## License

Original project code is licensed under the MIT License; see `LICENSE` and `NOTICE`.

Some UE2 package/property/static mesh parsing code is adapted from or informed by UE Viewer, which is MIT licensed. Those portions retain their original MIT permissions and notices. See `THIRD_PARTY_NOTICES.md`.

This repository does not grant rights to Vanguard: Saga of Heroes, its trademarks, client files, or game assets.

## Credits

The format and structure used by the Vanguard client files was unique, proprietary, and from what I can tell never before reverse-engineered. However, I was helped in places by lots of wonderful tools for similar versions of Unreal Engine, and there's generally a great community around Unreal Engine tools.

- [UE Viewer (umodel)](https://www.gildor.org/en/projects/umodel) by Gildor: viewer and extractor for Unreal Engine assets across engine versions; used to help with inspecting meshes, textures, and packages.
- [UE Explorer](https://eliotvu.com/portfolio/view/21/ue-explorer) by Eliot Van Uytfanghe: GUI browser and decompiler for Unreal Engine packages; used to inspect UnrealScript bytecode and serialized object properties.
- [Unreal Library (UELib)](https://github.com/EliotVU/Unreal-Library) by Eliot Van Uytfanghe: .NET library for reading and deserializing Unreal Engine package files; referenced for package format conventions and used optionally for text/object dumps.
- [UTPackage.js](https://github.com/fserb/UTPackage.js) by Fernando Serboncini: JavaScript Unreal Tournament package reader; referenced for JS-side binary parsing of UE2 package structures.
- [Ghidra](https://ghidra-sre.org/) by NSA Research Directorate: open-source software reverse engineering framework; used for decompiling VGClient.exe and analyzing engine functions, audio dispatch, animation, and locomotion systems.
- [Spt2Fbx](https://github.com/VenoMKO/Spt2Fbx) by the SpeedTree community: used as a bridge to the SpeedTree RT 4.x leaf-card geometry employed by the original engine.
- [vgmstream](https://vgmstream.org/) by vgmstream contributors: library for streamed video game audio formats; used as a reference for ISACT ICB/ISB codec identifiers and field naming conventions.
- [Legendary Explorer](https://github.com/ME3Tweaks/LegendaryExplorer) by ME3Tweaks: Mass Effect package editor and toolkit; referenced for ISACT codec conventions and Unreal package parsing approaches.
- [VGO Emulator Wiki](https://wiki.vgoemulator.net/Docs/Main_Page) by the VGO Emulator community: community wiki for the Vanguard emulator project; used as a reference for game mechanics, zone data, and server-side systems.

And of course, credit and thanks to the original creators, developers, and others associated with the original Vanguard project. Vanguard was a really special game with a lot of vision, and it's been a lot of fun to explore it through such deep digital archaeology.
