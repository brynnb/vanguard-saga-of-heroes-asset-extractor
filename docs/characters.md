# Character and Animation Extraction

The character pipeline exports character and creature meshes, playable race
metadata, customization controls, item appearance rules, attachment groups,
facial-control sidecars, and EMotion FX/UE2 animations.

Run the public stages with:

```bash
vanguard-assets export-characters
vanguard-assets export-animations --workers 4
vanguard-assets export-facial-controls
```

## Appearance and attachment catalogs

`export-characters` rebuilds the authoritative appearance sidecars:

- `item_appearance_catalog.json` is the package-level index;
- `item_appearances/*.json` stores package-qualified item identities and their
  skin, layer, tint, and hiding rules;
- `attachment_group_catalog.json` decodes the 17 original `.sag` template
  packages;
- `playable_races.json` stores playable identity and exact race/style skin
  `TintAlpha` and `TintPalette` assets from the canonical material manifest.

Consumers must not resolve appearances from `attachment_index` without its
`package_index` and actor visual profile. Modular playable bodies can share
`npcHuman` geometry while retaining their own race material palette.

## Playable skeleton fidelity boundary

Live sunset-client capture confirms that male High Elf character creation uses
the 184-node `UEM_elf_M_char:elf_M_char_ALL_0_SKELETON` modular master. Its
45-node head and 82-node hand components are subsets and do not contribute
missing detailed facial or middle/pinky bones. Complete raw-package audits
found no skipped second hierarchy in the modular FXA data.

The exporter preserves this recovered limitation rather than silently
augmenting the playable master from optimized or creature rigs.

The separately captured 234-node detailed hierarchy,
`mouseman_M_char_body_0_C_0`, came from an incorrect emulator character
preview. It shares all 202 visible optimized High Elf skeleton bone names and
parent relationships, but it does not provide authored weights for the modular
High Elf head. The optimized mesh has its own detailed weights; transferring
them to modular geometry would be a reconstructed approximation.

Full evidence and methodology are maintained in the sibling
`vanguard-research` repository's
`docs/playable_facial_skeletons_and_animation.md`.

## Remaining extraction work

- Complete playable-race assembly mapping for head/body combinations, body
  proportions, and body texture selection.
- Audit remaining playable races against live sunset actor counts and record
  slider availability and no-op behavior per recovered master.
- Finish mapping animation usage for hand poses, weapon and attachment sockets,
  action variants, and other context-specific clips.
- Resolve remaining known character mesh and texture defects with exact source
  evidence rather than percentage estimates or cross-race fallbacks.
