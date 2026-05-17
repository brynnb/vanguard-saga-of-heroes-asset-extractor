# Vanguard: Saga of Heroes Asset Extractor

[Vanguard: Saga of Heroes](https://en.wikipedia.org/wiki/Vanguard:_Saga_of_Heroes) was a 2007 fantasy MMORPG built on a heavily customized Unreal Engine 2.5 and many other tools. This asset extractor project extracts, converts, and exports all of the 3D models, animations, audio, and many other files from the original game client that users would install to play. The goal of this process is to recreate a modern, cross-platform, performant, customizable client for Vanguard and have it run with an emulated server. Vanguard was officially shut down in 2014 by its current publisher and is now unplayable outside of a (very awesome but unaffiliated) dedicated server emulation project. Its users can currently only play by running a very dated, Windows-only original game client.

This project is the extraction side of my other project, [Vanguard: Eternal Sagas](https://www.eternalsagas.com/), which is the actual implementation of these assets into a modern engine. Both sides of this work are the result of hundreds of hours of reverse engineering, parser writing, binary inspection, format reconstruction, and reimplementation into a new engine. The Eternal Sagas website also has multiple pages showing the assets that this project extracts, including the character models, music, 3D world, and world objects.

This is a fan-made preservation and recreation project and is not affiliated with, endorsed by, or connected to Daybreak Game Company LLC or any of its subsidiaries. Vanguard: Saga of Heroes is a registered trademark of Daybreak Game Company LLC. This repository does not distribute Vanguard client files, extracted game assets, generated output, or any other copyrighted Vanguard asset content.

## What It Extracts

The original Vanguard client data is spread across UE2 package files, Vanguard-specific mesh and animation formats, terrain chunks, audio archives, prefab/object archives, text resources, and several one-off binary formats. This project currently extracts:

- Terrain from `.vgr` chunk files, including heightmaps, layer masks, terrain textures, and chunk transforms.
- Static meshes from UE2 package data, exported to glTF.
- Character meshes, playable race metadata, customization data, and item attachment references.
- EMotion FX and UE2 skeletal animations.
- UAX, ISB, and ICB audio data.
- SGO prefab and world object placement data.
- Shader-to-texture mappings and diffuse texture PNGs when the optional Unreal-Library CLI is available.

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
python vanguard.py extract-all
```

Run a small smoke test:

```bash
python vanguard.py extract-all --sections core --limit-meshes 3 --skip-unreal-library
```

Run individual extraction stages:

```bash
python vanguard.py setup --reset
python vanguard.py fetch-unreal-library
python vanguard.py build-shaders
python vanguard.py extract-terrain
python vanguard.py export-meshes
python vanguard.py export-characters
python vanguard.py export-animations
python vanguard.py extract-audio
python vanguard.py extract-world
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
python vanguard.py fetch-unreal-library
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

## Areas Remaining

- Exact runtime rendering logic for SpeedTree leaf billboards, mostly around the exact size and placement. Trees look very good as-is, though.
- Playable race assembly details around mixing heads with bodies, body sizing/proportions, and body texture selection.
- Skeleton customization logic for playable races, especially how character creation sliders drive the extra facial/body deformer bones.
- Hand-specific animation handling, including weapon/attachment sockets and left-hand/right-hand action variants.
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
- [Spt2Fbx](https://github.com/nickvdyck/Spt2Fbx) by the SpeedTree community: referenced for understanding SpeedTree RT 4.x leaf card and branch geometry as used in the original engine.
- [vgmstream](https://vgmstream.org/) by vgmstream contributors: library for streamed video game audio formats; used as a reference for ISACT ICB/ISB codec identifiers and field naming conventions.
- [Legendary Explorer](https://github.com/ME3Tweaks/LegendaryExplorer) by ME3Tweaks: Mass Effect package editor and toolkit; referenced for ISACT codec conventions and Unreal package parsing approaches.
- [VGO Emulator Wiki](https://wiki.vgoemulator.net/Docs/Main_Page) by the VGO Emulator community: community wiki for the Vanguard emulator project; used as a reference for game mechanics, zone data, and server-side systems.

And of course, credit and thanks to the original creators, developers, and others associated with the original Vanguard project. Vanguard was a really special game with a lot of vision, and it's been a lot of fun to explore it in such a deep, digital-archeological sort of way.
