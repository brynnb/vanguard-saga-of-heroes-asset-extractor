#!/usr/bin/env python3
"""
generate_customization_data.py

Parses the authoritative Vanguard customization text files and produces the
two data files consumed by the character viewer:

  Inputs:
    <EMU>/bin/Resources/Texts/customization_data.txt   (slider → bones → 3 transform rows)
    <EMU>/bin/Resources/Texts/cust_race_mods_v2.txt    (per-race slider min/max clamps)

  Outputs written / patched:
    output/data/customization_sliders.json      — filtered slider tree used by the viewer
                                                   (identity-only bones dropped; bones with
                                                    at least one non-identity transform row
                                                    are kept)
    output/data/customization_sliders_raw.json  — complete authoritative slider tree with
                                                   ALL bones preserved verbatim from the
                                                   source .txt (archival; not read by the
                                                   viewer)
    output/data/playable_races.json             — slider_defaults[] and slider_ranges[]
                                                   updated per race

  Preservation goal:
    Before this script existed, both output files were hand-maintained with no
    provenance. This script makes the pipeline reproducible so parsing is not
    lost on future re-extraction. The _raw.json preserves all bones even
    though the current viewer filters them out, so future viewer fixes can
    reference the full authoritative data. See
    notes/2026-04-16_customization_investigation.md.

  Key file format notes:
    customization_data.txt
      - Slider block opens with "<Name>\\t<pageIdx>".
      - pageIdx in [0..7] → visible pages:
          0 Proportions, 1 Body Mass, 2 Head/Mood, 3 Brow,
          4 Eye,         5 Ear,        6 Nose,      7 Mouth
      - pageIdx == -1 (Breast Gravity) is a hidden auto-slider; not shown in UI.
      - Under each header, the body is: a bone-name line followed by three tab-
        separated rows of 6 floats. The historical JSON keys call these
        rowMin / rowMid / rowMax, but the source shape and native loader match
        X / Y / Z axis rows. Each axis row is interpreted downstream as:
          [position_min, position_max, rotation_min, rotation_max, scale_min, scale_max]
        We keep the legacy key names to avoid a broad schema migration, but
        they should not be read as slider 0 / 50 / 100 rows.

    cust_race_mods_v2.txt
      - 63 rows × 76 whitespace-separated integers each (= 38 min/max pairs).
      - Each pair is a clamp on ONE slider, but the file is not sequential.
        VGClient's native loader reads the first 19 pairs into even slider
        slots and the second 19 pairs into odd slider slots. Each recovered
        pair is preserved as that race/sex slider's allowed range. Midpoint of
        each deinterleaved pair is the reconstructed default slider value.
        Sliders 38..48 use range 0..100 and default 50 because no race-mod
        clamp data is present.
      - Rows 0..41 correspond to 21 races × 2 genders (M, F interleaved).
        Rows 42..62 are non-playable races cut from the shipping game.
      - Row→race mapping is fixed by source order. The current playable list
        omits HalfOrc, so rows 18/19 are skipped.

Usage:
    python scripts/generators/generate_customization_data.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
try:
    from vanguard_assets import config
    REPO = config.PROJECT_ROOT

    DEFAULT_EMU = Path(
        os.environ.get("VANGUARD_EMU_PATH", str(config.VANGUARD_EMU_ROOT))
    ).expanduser()
except ImportError:
    DEFAULT_EMU = Path(
        os.environ.get("VANGUARD_EMU_PATH", "~/Downloads/Vanguard EMU")
    ).expanduser()

# ── Page name mapping (pageIdx → display name) ─────────────────────────────
# These names come from the existing customization_sliders.json which in turn
# matches the in-game customization UI captions.
PAGE_NAMES = {
    0: "Proportions",
    1: "Body Mass",
    2: "Head/Mood",
    3: "Brow",
    4: "Eye",
    5: "Ear",
    6: "Nose",
    7: "Mouth",
}


# ─────────────────────────────────────────────────────────────────────────────
# customization_data.txt parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_customization_data(txt_path: Path) -> list[dict[str, Any]]:
    """Return a flat list of slider dicts in source order.

    Each slider dict:
        {
          "name":  str,
          "page":  int,  # -1 means hidden/auto-slider
          "bones": [
              {
                 "name":   str,
                 "rowMin": [6 floats],  # legacy key: source X-axis row
                 "rowMid": [6 floats],  # legacy key: source Y-axis row
                 "rowMax": [6 floats],  # legacy key: source Z-axis row
              }, ...
          ]
        }
    """
    sliders: list[dict[str, Any]] = []
    current_slider: dict[str, Any] | None = None
    current_bone_name: str | None = None
    current_rows: list[list[float]] = []

    def _flush_bone():
        """Append the collected bone rows to the current slider."""
        nonlocal current_bone_name, current_rows
        if current_bone_name is None:
            return
        if len(current_rows) != 3:
            raise ValueError(
                f"Slider '{current_slider['name'] if current_slider else '?'}' "
                f"bone '{current_bone_name}' expected 3 rows, got {len(current_rows)}"
            )
        current_slider["bones"].append({
            "name": current_bone_name,
            "rowMin": list(current_rows[0]),
            "rowMid": list(current_rows[1]),
            "rowMax": list(current_rows[2]),
        })
        current_bone_name = None
        current_rows = []

    for raw in txt_path.read_text().splitlines():
        line = raw.strip()
        if not line:
            # Blank line → flush whichever bone we were building, but keep the slider.
            if current_slider is not None and current_bone_name is not None:
                _flush_bone()
            continue

        parts = line.split("\t")
        is_slider_header = (
            len(parts) == 2
            and parts[1].strip().lstrip("-").isdigit()
        )

        if is_slider_header:
            # Flush any in-progress bone, close previous slider.
            if current_slider is not None:
                if current_bone_name is not None:
                    _flush_bone()
                sliders.append(current_slider)
            current_slider = {
                "name": parts[0],
                "page": int(parts[1]),
                "bones": [],
            }
            current_bone_name = None
            current_rows = []
            continue

        # Else: either a bone-name line or a numeric transform row.
        if any(ch.isalpha() for ch in line):
            # Bone-name line: close previous bone first.
            if current_bone_name is not None:
                _flush_bone()
            current_bone_name = line.split("\t")[0]
            current_rows = []
        else:
            # Numeric row. Expect exactly 6 floats per the engine convention.
            values = [float(tok) for tok in line.split()]
            if len(values) != 6:
                raise ValueError(
                    f"Expected 6 floats per row, got {len(values)}: '{raw}'"
                )
            current_rows.append(values)

    # EOF flush
    if current_slider is not None:
        if current_bone_name is not None:
            _flush_bone()
        sliders.append(current_slider)

    return sliders


IDENTITY_ROW = [0.0, 0.0, 0.0, 0.0, 1.0, 1.0]


def _is_identity_bone(bone: dict[str, Any]) -> bool:
    """Return True if all three rows of this bone are the identity transform.

    The current viewer overwrites per-bone TRS for every slider it evaluates,
    so listing an identity-only bone under a slider would wipe contributions
    that earlier sliders made to the same bone. We therefore drop those bones
    from the viewer-facing JSON. The raw JSON preserves them.
    """
    return (
        bone["rowMin"] == IDENTITY_ROW
        and bone["rowMid"] == IDENTITY_ROW
        and bone["rowMax"] == IDENTITY_ROW
    )


def build_sliders_json(
    all_sliders: list[dict[str, Any]],
    *,
    drop_identity_bones: bool = False,
) -> list[dict[str, Any]]:
    """Group visible sliders (page >= 0) into the page tree consumed by the viewer.

    If ``drop_identity_bones`` is True, bones whose three rows are all the
    identity transform are omitted — this matches the legacy viewer data.
    """
    pages_by_idx: dict[int, list[dict[str, Any]]] = {}
    for s in all_sliders:
        if s["page"] < 0:
            continue
        bones = s["bones"]
        if drop_identity_bones:
            bones = [b for b in bones if not _is_identity_bone(b)]
        pages_by_idx.setdefault(s["page"], []).append({
            "name": s["name"],
            "bones": bones,
        })

    return [
        {"id": idx, "name": PAGE_NAMES.get(idx, f"Page{idx}"), "sliders": sliders_list}
        for idx, sliders_list in sorted(pages_by_idx.items())
    ]


def visible_slider_order(all_sliders: list[dict[str, Any]]) -> list[str]:
    """Return names of visible sliders in source-file order.

    This order MUST match how the server indexes the uint8_t appearances[]
    array (indices 12..60 = these 49 sliders). It also matches the layout of
    slider_defaults[] in playable_races.json.
    """
    return [s["name"] for s in all_sliders if s["page"] >= 0]


# ─────────────────────────────────────────────────────────────────────────────
# cust_race_mods_v2.txt parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_race_mods(txt_path: Path) -> list[list[tuple[int, int]]]:
    """Return rows of deinterleaved (min, max) integer tuples.

    Expect 63 rows × 38 pairs. Rows 0..41 are 21 playable races × 2 genders.
    VGClient lays the 38 source pairs out as all even slider slots first, then
    all odd slider slots; normalize that here so every row is in slider order.
    """
    rows: list[list[tuple[int, int]]] = []
    for raw in txt_path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        nums = [int(tok) for tok in line.split()]
        if len(nums) % 2 != 0:
            raise ValueError(f"Odd number of tokens in row: '{raw}'")
        rows.append(_deinterleave_race_mod_pairs(
            [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
        ))
    return rows


def _deinterleave_race_mod_pairs(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return pairs in engine slider-slot order.

    The native loader copies source pairs 0..18 into output slots 0,2,4..36
    and source pairs 19..37 into output slots 1,3,5..37. Keeping this in the
    generator fixes the source provenance instead of compensating per slider.
    """
    out: list[tuple[int, int] | None] = [None] * len(pairs)
    even_count = (len(pairs) + 1) // 2
    for source_index, pair in enumerate(pairs):
        if source_index < even_count:
            slot = source_index * 2
        else:
            slot = (source_index - even_count) * 2 + 1
        if slot >= len(out):
            raise ValueError(f"Race-mod pair {source_index} maps outside row length")
        out[slot] = pair
    if any(pair is None for pair in out):
        raise ValueError("Race-mod deinterleave left one or more slots empty")
    return [pair for pair in out if pair is not None]


def row_midpoints(pairs: list[tuple[int, int]], full_slider_count: int) -> list[int]:
    """Convert a row of (min, max) pairs to integer slider defaults.

    For sliders past the end of the clamp data, default to 50 (neutral).
    Midpoints are rounded to the nearest integer using banker's rounding
    (python round()), which matches the existing playable_races.json values.
    """
    out = [round((mn + mx) / 2) for mn, mx in pairs]
    while len(out) < full_slider_count:
        out.append(50)
    return out


def row_ranges(pairs: list[tuple[int, int]], full_slider_count: int) -> list[list[int]]:
    """Preserve recovered inclusive slider clamps, padding unknown slots."""
    out = [[min(mn, mx), max(mn, mx)] for mn, mx in pairs]
    while len(out) < full_slider_count:
        out.append([0, 100])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Row-to-race mapping
# ─────────────────────────────────────────────────────────────────────────────

RACE_MOD_RACE_ORDER = [
    "Dwarf",
    "DarkElf",
    "HighElf",
    "WoodElf",
    "Gnome",
    "Goblin",
    "HalfElf",
    "LesserGiant",
    "Halfling",
    "HalfOrc",
    "Kojani",
    "Qaliathari",
    "Thestran",
    "Mordebi",
    "Orc",
    "Raki",
    "Vulmane",
    "KojanBarbarian",
    "Varanthari",
    "Varanjar",
    "Kurashasa",
]


def source_rows_to_races(
    rows: list[list[tuple[int, int]]],
    playable_entries: list[dict[str, Any]],
) -> dict[str, int]:
    """Return {race_gender_key: row_index} from the source race order."""
    source_map: dict[str, int] = {}
    for race_index, race in enumerate(RACE_MOD_RACE_ORDER):
        for gender_offset, gender in enumerate(("M", "F")):
            row_index = race_index * 2 + gender_offset
            if row_index < len(rows):
                source_map[f"{race}_{gender}"] = row_index

    mapping: dict[str, int] = {}
    for entry in playable_entries:
        key = f"{entry['race']}_{entry['gender']}"
        if key in source_map:
            mapping[key] = source_map[key]
    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--emu-root",
        type=Path,
        default=DEFAULT_EMU,
        help=f"Path to Vanguard EMU root (default: {DEFAULT_EMU})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO / "output" / "data",
        help="Destination directory for generated JSON (default: output/data)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Parse and compare to existing JSON files, but do NOT overwrite them.",
    )
    args = parser.parse_args()

    cdata_path = args.emu_root / "bin" / "Resources" / "Texts" / "customization_data.txt"
    cmods_path = args.emu_root / "bin" / "Resources" / "Texts" / "cust_race_mods_v2.txt"
    for p in (cdata_path, cmods_path):
        if not p.exists():
            print(f"ERROR: missing input file {p}", file=sys.stderr)
            return 1

    sliders = parse_customization_data(cdata_path)
    print(f"Parsed {len(sliders)} sliders from {cdata_path.name}")
    pages_tree_filtered = build_sliders_json(sliders, drop_identity_bones=True)
    pages_tree_raw      = build_sliders_json(sliders, drop_identity_bones=False)
    visible_names = visible_slider_order(sliders)
    visible_count = len(visible_names)
    dropped_bones = sum(
        sum(1 for b in s["bones"] if _is_identity_bone(b))
        for s in sliders if s["page"] >= 0
    )
    print(
        f"  visible sliders: {visible_count} across {len(pages_tree_filtered)} pages "
        f"(hidden/auto: {len(sliders) - visible_count}, identity-only bones dropped from viewer JSON: {dropped_bones})"
    )

    rows = parse_race_mods(cmods_path)
    print(f"Parsed {len(rows)} race-mod rows × {len(rows[0])} pairs from {cmods_path.name}")

    playable_path = args.output_dir / "playable_races.json"
    if not playable_path.exists():
        print(f"ERROR: {playable_path} not found; run generate_playable_races.py first", file=sys.stderr)
        return 1
    playable = json.loads(playable_path.read_text())

    row_map = source_rows_to_races(rows, playable)
    print(f"Mapped {len(row_map)}/{len(playable)} race entries to race-mod source rows")
    missing = [
        f"{e['race']}_{e['gender']}"
        for e in playable
        if f"{e['race']}_{e['gender']}" not in row_map
    ]
    if missing:
        print(f"  unmatched (will use all-50 defaults): {missing}")

    # Apply regenerated slider defaults and authoritative clamps to every entry.
    for entry in playable:
        key = f"{entry['race']}_{entry['gender']}"
        ri = row_map.get(key)
        if ri is None:
            entry["slider_defaults"] = [50] * visible_count
            entry["slider_ranges"] = [[0, 100] for _ in range(visible_count)]
        else:
            entry["slider_defaults"] = row_midpoints(rows[ri], visible_count)
            entry["slider_ranges"] = row_ranges(rows[ri], visible_count)

    sliders_out     = args.output_dir / "customization_sliders.json"
    sliders_raw_out = args.output_dir / "customization_sliders_raw.json"

    if args.check:
        # Diff against existing files, exit non-zero if they would change.
        existing_sliders = json.loads(sliders_out.read_text()) if sliders_out.exists() else None
        existing_playable = json.loads(playable_path.read_text())
        changed_slider_defaults = 0
        changed_slider_ranges = 0
        for new_e, old_e in zip(playable, existing_playable):
            if new_e.get("slider_defaults") != old_e.get("slider_defaults"):
                changed_slider_defaults += 1
                print(
                    f"  slider_defaults differ for {new_e['race']}_{new_e['gender']}:\n"
                    f"    new: {new_e['slider_defaults']}\n"
                    f"    old: {old_e.get('slider_defaults')}"
                )
            if new_e.get("slider_ranges") != old_e.get("slider_ranges"):
                changed_slider_ranges += 1
                print(
                    f"  slider_ranges differ for {new_e['race']}_{new_e['gender']}:\n"
                    f"    new: {new_e['slider_ranges']}\n"
                    f"    old: {old_e.get('slider_ranges')}"
                )
        slider_tree_matches = existing_sliders == pages_tree_filtered
        print(
            f"\nCHECK RESULT: "
            f"sliders_json(filtered) match={slider_tree_matches}, "
            f"slider_defaults diffs={changed_slider_defaults}/{len(playable)}, "
            f"slider_ranges diffs={changed_slider_ranges}/{len(playable)}"
        )
        return (
            0
            if slider_tree_matches
            and changed_slider_defaults == 0
            and changed_slider_ranges == 0
            else 2
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    sliders_out.write_text(json.dumps(pages_tree_filtered, indent=2) + "\n")
    print(f"Wrote {sliders_out}")
    sliders_raw_out.write_text(json.dumps(pages_tree_raw, indent=2) + "\n")
    print(f"Wrote {sliders_raw_out}")
    playable_path.write_text(json.dumps(playable, indent=2) + "\n")
    print(f"Updated {playable_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
