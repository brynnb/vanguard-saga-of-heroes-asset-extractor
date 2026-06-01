#!/usr/bin/env python3
"""Export engine-facing world-music and ambience manifests from the ICB catalog."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


RUNTIME_MUSIC_STATES = [
    {"state": 1, "day": "Idle", "night": "Idle_Night", "lane": "world"},
    {"state": 2, "day": "Walk", "night": "Walk_Night", "lane": "world"},
    {"state": 3, "day": "Run", "night": "Run_Night", "lane": "world"},
    {"state": 4, "day": "Adventure", "night": "Adventure_Night", "lane": "world"},
    {"state": 5, "day": "1", "night": "2", "lane": "special-region"},
    {"state": 6, "day": "1a", "night": "2a", "lane": "special-region"},
    {"state": 7, "day": "1b", "night": "2b", "lane": "special-region"},
    {"state": 8, "day": "1c", "night": "2c", "lane": "special-region"},
    {"state": 9, "day": "Default_City", "night": "Default_City_Night", "lane": "city"},
    {"state": 10, "day": "Alley_Day", "night": "Alley_Night", "lane": "city"},
    {"state": 11, "day": "Religion_Day", "night": "Religion_Night", "lane": "city"},
    {"state": 12, "day": "Pub_Day", "night": "Pub_Night", "lane": "city"},
    {"state": 13, "day": "Regal_Day", "night": "Regal_Night", "lane": "city"},
]

SEA_REGION_OVERRIDES = [
    "Bay_of_Verael",
    "Cobalt_Deep",
    "Emerald_Depths",
    "Jade_Sea",
    "Mordeb_Sea",
    "Ocean_of_Sorrow",
    "Straits_of_Thestra",
]

RUNTIME_AMBIENCE_SELECTORS = ["TimeOfDay", "OneShots", "SpecialAmbience", "Storms"]

CHUNK_LINE_RE = re.compile(r"^\s*(\d+)\s+(\S+)\s{2,}(.+?)\s{2,}(-?\d+)\s+(-?\d+)\s*$")
GENERIC_CHUNK_ONLY_TOKENS = {"of", "the", "village", "ruins", "ruin", "ancient"}


def compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def tokenize_name(value: str) -> set[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return {part for part in re.findall(r"[A-Za-z0-9]+", value.lower()) if part}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=REPO_ROOT / "output/audio/catalog_icb_casefix/cues.json",
        help="Enriched ICB cue catalog produced by dump_icb.py",
    )
    parser.add_argument(
        "--chunk-reference",
        type=Path,
        default=REPO_ROOT / "docs/external/vgo-server-emulator-wiki/Reference/Chunks.md",
        help="Chunk reference document used for location hints",
    )
    parser.add_argument(
        "--isb-inspect",
        type=Path,
        default=REPO_ROOT / "output/audio/inspect_isb",
        help="Directory of inspect_isb.py per-bank reports plus banks.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "output/audio/music_ambience_manifest",
        help="Output directory for the generated manifest",
    )
    return parser.parse_args(argv)


# --- ISB audio-profile extraction (bpm + spatial attenuation) ----------------

def _walk_isb_chunks(node: dict) -> object:
    """Yield every node in an inspect_isb tree (root + nested children/chunks)."""
    yield node
    for child in node.get("children", []) or node.get("chunks", []) or []:
        yield from _walk_isb_chunks(child)


def _iter_samples(report: dict) -> object:
    for top in report.get("chunks", []):
        if top.get("list_type") == "samp":
            yield top


def _sample_title(samp: dict) -> str | None:
    for sub in samp.get("children", []):
        if sub.get("id") == "titl":
            text = sub.get("text")
            if isinstance(text, str):
                return text
    return None


def _find_chunk_field(node: dict, chunk_id: str) -> dict | None:
    for sub in node.get("children", []) or node.get("chunks", []) or []:
        if sub.get("id") == chunk_id:
            return sub
    return None


def _bpm_from_dtmp(dtmp_us: object) -> float | None:
    try:
        v = int(dtmp_us)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # MIDI microseconds-per-quarter-note -> BPM
    return round(60_000_000.0 / v, 4)


def _normalize_sdst(node: dict) -> dict | None:
    sdst = node.get("sdst")
    if not isinstance(sdst, dict):
        return None
    min_d = sdst.get("min_distance")
    max_d = sdst.get("max_distance")
    rolloff_db = sdst.get("rolloff_or_attenuation")
    if not isinstance(min_d, (int, float)) or not isinstance(max_d, (int, float)):
        return None
    return {
        "min_distance": float(min_d),
        "max_distance": float(max_d),
        "rolloff_db": float(rolloff_db) if isinstance(rolloff_db, (int, float)) else None,
        "flags": sdst.get("flags"),
    }


def _normalize_cone(node: dict) -> dict | None:
    cone = node.get("cone")
    if not isinstance(cone, dict):
        return None
    return {
        "inner_angle": cone.get("inner_angle"),
        "outer_angle": cone.get("outer_angle"),
        "outside_volume": cone.get("outside_volume"),
        "mode_flags": cone.get("mode_flags"),
    }


def build_isb_audio_profile(report: dict) -> dict:
    """Distill an inspect_isb report into a compact runtime audio profile.

    Returns:
      {
        bpm: float | null,                 # from top-level dtmp
        phrase_beats: int | null,          # from top-level tmcd[0] (40 in this corpus)
        phrase_seconds: float | null,      # 60 / bpm * phrase_beats
        time_signature: [num, den] | null, # from top-level dtsg
        sync_default: { sync_start_name, sync_multiple } | null,
        loop_flag: int | null,             # 0 across this corpus -> caller should loop via routing
        sample_overrides: {                # only samples that differ from defaults
          <title>: { bpm?, phrase_seconds?, sync? }
        },
        spatial: {                         # present only when sdst/cone are emitted
          distance_attenuation: { min_distance, max_distance, rolloff_db, flags } | null,
          cone: { inner_angle, outer_angle, outside_volume, mode_flags } | null,
          per_sample: { <title>: { distance_attenuation?, cone? } }   # only deviations
        } | null
      }
    """

    top_dtmp = _find_chunk_field(report, "dtmp")
    top_dtsg = _find_chunk_field(report, "dtsg")
    top_tmcd = _find_chunk_field(report, "tmcd")
    top_loop = _find_chunk_field(report, "loop")
    top_sync = _find_chunk_field(report, "sync")

    bpm_default = _bpm_from_dtmp(top_dtmp.get("u32") if top_dtmp else None)
    phrase_beats = None
    if top_tmcd and isinstance(top_tmcd.get("u16_pair"), list) and top_tmcd["u16_pair"]:
        phrase_beats = int(top_tmcd["u16_pair"][0])
    time_signature = None
    if top_dtsg and isinstance(top_dtsg.get("u16_pair"), list) and len(top_dtsg["u16_pair"]) >= 2:
        time_signature = [int(top_dtsg["u16_pair"][0]), int(top_dtsg["u16_pair"][1])]
    phrase_seconds = None
    if bpm_default and phrase_beats:
        phrase_seconds = round(60.0 / bpm_default * phrase_beats, 4)
    sync_default = None
    if top_sync and "sync_start_name" in top_sync:
        sync_default = {
            "sync_start_name": top_sync.get("sync_start_name"),
            "sync_multiple": top_sync.get("sync_multiple"),
        }

    sample_overrides: dict[str, dict] = {}
    spatial_per_sample: dict[str, dict] = {}
    spatial_default_sdst: dict | None = None
    spatial_default_cone: dict | None = None
    sdst_counts: dict = {}
    cone_counts: dict = {}

    # Bank-default sdst/cone live in a sibling LIST(bfob) at the top level.
    for top in report.get("chunks", []):
        if top.get("list_type") != "bfob":
            continue
        for sub in top.get("children", []):
            if sub.get("id") == "sdst" and spatial_default_sdst is None:
                spatial_default_sdst = _normalize_sdst(sub)
            elif sub.get("id") == "cone" and spatial_default_cone is None:
                spatial_default_cone = _normalize_cone(sub)

    # First pass: tally per-sample sdst/cone (rare, but handle it if a bank
    # ever overrides on a sample). If bfob did not provide a default, fall
    # back to the most common per-sample value.
    for samp in _iter_samples(report):
        for sub in samp.get("children", []):
            if sub.get("id") == "sdst":
                norm = _normalize_sdst(sub)
                if norm:
                    key = (norm["min_distance"], norm["max_distance"], norm["rolloff_db"], norm["flags"])
                    sdst_counts[key] = sdst_counts.get(key, 0) + 1
            elif sub.get("id") == "cone":
                norm = _normalize_cone(sub)
                if norm:
                    key = (norm["inner_angle"], norm["outer_angle"], norm["outside_volume"], norm["mode_flags"])
                    cone_counts[key] = cone_counts.get(key, 0) + 1

    if spatial_default_sdst is None and sdst_counts:
        best = max(sdst_counts.items(), key=lambda kv: kv[1])[0]
        spatial_default_sdst = {
            "min_distance": best[0],
            "max_distance": best[1],
            "rolloff_db": best[2],
            "flags": best[3],
        }
    if spatial_default_cone is None and cone_counts:
        best = max(cone_counts.items(), key=lambda kv: kv[1])[0]
        spatial_default_cone = {
            "inner_angle": best[0],
            "outer_angle": best[1],
            "outside_volume": best[2],
            "mode_flags": best[3],
        }

    # Second pass: per-sample tempo + spatial overrides
    for samp in _iter_samples(report):
        title = _sample_title(samp)
        if not title:
            continue
        sample_dtmp = _find_chunk_field(samp, "dtmp")
        sample_sync = _find_chunk_field(samp, "sync")
        sample_sdst = _find_chunk_field(samp, "sdst")
        sample_cone = _find_chunk_field(samp, "cone")

        sample_bpm = _bpm_from_dtmp(sample_dtmp.get("u32") if sample_dtmp else None)
        if sample_bpm is not None and sample_bpm != bpm_default:
            override: dict = {"bpm": sample_bpm}
            if phrase_beats:
                override["phrase_seconds"] = round(60.0 / sample_bpm * phrase_beats, 4)
            sample_overrides.setdefault(title, {}).update(override)

        if sample_sync and "sync_start_name" in sample_sync and sync_default:
            sync_kv = {
                "sync_start_name": sample_sync.get("sync_start_name"),
                "sync_multiple": sample_sync.get("sync_multiple"),
            }
            if sync_kv != sync_default:
                sample_overrides.setdefault(title, {})["sync"] = sync_kv
        elif sample_sync and "sync_start_name" in sample_sync and not sync_default:
            sample_overrides.setdefault(title, {})["sync"] = {
                "sync_start_name": sample_sync.get("sync_start_name"),
                "sync_multiple": sample_sync.get("sync_multiple"),
            }

        per_sample_spatial: dict = {}
        if sample_sdst:
            norm = _normalize_sdst(sample_sdst)
            if norm and norm != spatial_default_sdst:
                per_sample_spatial["distance_attenuation"] = norm
        if sample_cone:
            norm = _normalize_cone(sample_cone)
            if norm and norm != spatial_default_cone:
                per_sample_spatial["cone"] = norm
        if per_sample_spatial:
            spatial_per_sample[title] = per_sample_spatial

    spatial = None
    if spatial_default_sdst or spatial_default_cone or spatial_per_sample:
        spatial = {
            "distance_attenuation": spatial_default_sdst,
            "cone": spatial_default_cone,
            "per_sample": spatial_per_sample,
        }

    profile: dict = {
        "bpm": bpm_default,
        "phrase_beats": phrase_beats,
        "phrase_seconds": phrase_seconds,
        "time_signature": time_signature,
        "sync_default": sync_default,
        "loop_flag": (top_loop or {}).get("u32"),
        "sample_overrides": sample_overrides,
        "spatial": spatial,
    }
    return profile


def build_isb_audio_profile_index(inspect_dir: Path) -> dict[str, dict]:
    """Map paired_isb_bank basename (e.g. 'Ahgramun_Coast.isb') to audio profile."""
    banks_json = inspect_dir / "banks.json"
    if not banks_json.exists():
        return {}
    payload = json.loads(banks_json.read_text())
    index: dict[str, dict] = {}
    for entry in payload.get("banks", []):
        report_path = entry.get("report")
        original_file = entry.get("file")
        if not report_path or not Path(report_path).exists():
            continue
        try:
            report = json.loads(Path(report_path).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        profile = build_isb_audio_profile(report)
        # Key by both the original basename (e.g. 'Ahgramun_Coast.isb') and the
        # stem (e.g. 'Ahgramun_Coast') so both ICB.paired_isb_bank styles match.
        if original_file:
            basename = Path(original_file).name
            stem = Path(original_file).stem
            index[basename] = profile
            index[stem] = profile
        # Also key by the report stem (e.g. 'Music__Ahgramun_Coast' -> 'Ahgramun_Coast')
        report_stem = Path(report_path).stem
        if "__" in report_stem:
            short = report_stem.split("__", 1)[1]
            index.setdefault(short, profile)
            index.setdefault(short + ".isb", profile)
    return index


def lookup_audio_profile(
    profile_index: dict[str, dict],
    paired_isb_bank: str | None,
) -> dict | None:
    if not paired_isb_bank:
        return None
    name = str(paired_isb_bank)
    if name in profile_index:
        return profile_index[name]
    stem = Path(name).stem
    return profile_index.get(stem)


def load_catalog(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text())
    return payload["cues"]


def load_chunks(path: Path) -> list[dict[str, object]]:
    chunks: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = CHUNK_LINE_RE.match(line)
        if not match:
            continue
        chunk_id, shortname, displayname, coord_x, coord_y = match.groups()
        chunks.append(
            {
                "chunk_id": int(chunk_id),
                "shortname": shortname,
                "displayname": displayname.strip(),
                "coord_x": int(coord_x),
                "coord_y": int(coord_y),
                "shortname_key": compact_name(shortname),
                "displayname_key": compact_name(displayname),
                "tokens": tokenize_name(shortname) | tokenize_name(displayname),
            }
        )
    return chunks


def is_world_music_cue(cue: dict[str, object]) -> bool:
    semantic = cue.get("semantic_summary", {})
    sound_entries = semantic.get("sound_entries", [])
    if sound_entries:
        return False
    list_summaries = semantic.get("list_summaries", [])
    return any(item.get("signal_group") in {"AmbientMusic", "CombatMusic"} for item in list_summaries)


def is_ambience_cue(cue: dict[str, object]) -> bool:
    semantic = cue.get("semantic_summary", {})
    sound_entries = semantic.get("sound_entries", [])
    if not sound_entries:
        return False
    return any(item.get("signal_group") == "Environment" for item in sound_entries)


def summarize_chunk_match(
    chunk: dict[str, object],
    *,
    match_type: str,
    confidence: str,
    matched_tokens: set[str] | None = None,
) -> dict[str, object]:
    summary = {
        "match_type": match_type,
        "confidence": confidence,
        "chunk_id": chunk["chunk_id"],
        "shortname": chunk["shortname"],
        "displayname": chunk["displayname"],
        "coord_x": chunk["coord_x"],
        "coord_y": chunk["coord_y"],
    }
    if matched_tokens:
        summary["matched_tokens"] = sorted(matched_tokens)
    return summary


def build_chunk_activation(bundle_name: str, chunks: list[dict[str, object]]) -> dict[str, object]:
    if bundle_name in SEA_REGION_OVERRIDES:
        return {
            "resolution": "runtime-sea-region",
            "confidence": "high",
            "activation_rules": [
                {
                    "type": "runtime-sea-region-family",
                    "region_name": bundle_name,
                }
            ],
            "candidate_chunks": [],
        }

    bundle_key = compact_name(bundle_name)
    bundle_tokens = tokenize_name(bundle_name)
    exact_matches: list[dict[str, object]] = []
    token_subset_matches: list[dict[str, object]] = []
    candidate_chunks: list[dict[str, object]] = []
    seen_candidate_chunk_ids: set[int] = set()

    for chunk in chunks:
        if bundle_key == chunk["shortname_key"]:
            exact_matches.append(
                summarize_chunk_match(
                    chunk,
                    match_type="exact-shortname",
                    confidence="high",
                )
            )
            continue
        if bundle_key == chunk["displayname_key"]:
            exact_matches.append(
                summarize_chunk_match(
                    chunk,
                    match_type="exact-displayname",
                    confidence="high",
                )
            )
            continue

        if bundle_tokens and bundle_tokens <= chunk["tokens"]:
            extra_tokens = chunk["tokens"] - bundle_tokens
            if extra_tokens <= GENERIC_CHUNK_ONLY_TOKENS:
                token_subset_matches.append(
                    summarize_chunk_match(
                        chunk,
                        match_type="token-subset",
                        confidence="medium",
                        matched_tokens=bundle_tokens,
                    )
                )
                continue

        overlap = bundle_tokens & chunk["tokens"]
        overlap_ratio = len(overlap) / len(bundle_tokens) if bundle_tokens else 0.0
        if bundle_key and (
            chunk["shortname_key"].startswith(bundle_key)
            or chunk["displayname_key"].startswith(bundle_key)
        ):
            overlap = overlap or bundle_tokens
            overlap_ratio = max(overlap_ratio, 0.5)
        if overlap_ratio < 0.5:
            continue
        if chunk["chunk_id"] in seen_candidate_chunk_ids:
            continue
        seen_candidate_chunk_ids.add(chunk["chunk_id"])
        candidate_chunks.append(
            summarize_chunk_match(
                chunk,
                match_type="token-overlap",
                confidence="low",
                matched_tokens=overlap,
            )
        )

    exact_matches.sort(key=lambda item: (item["match_type"], item["chunk_id"]))
    token_subset_matches.sort(key=lambda item: (item["match_type"], item["chunk_id"]))
    candidate_chunks.sort(key=lambda item: (-len(item.get("matched_tokens", [])), item["chunk_id"]))

    if exact_matches:
        return {
            "resolution": "chunk-exact",
            "confidence": "high",
            "activation_rules": [
                {
                    "type": "chunk-id",
                    **item,
                }
                for item in exact_matches
            ],
            "candidate_chunks": candidate_chunks,
        }
    if token_subset_matches:
        return {
            "resolution": "chunk-token-subset",
            "confidence": "medium",
            "activation_rules": [
                {
                    "type": "chunk-id",
                    **item,
                }
                for item in token_subset_matches
            ],
            "candidate_chunks": candidate_chunks,
        }
    if candidate_chunks:
        return {
            "resolution": "chunk-candidate",
            "confidence": "low",
            "activation_rules": [],
            "candidate_chunks": candidate_chunks,
        }
    return {
        "resolution": "unresolved",
        "confidence": "none",
        "activation_rules": [],
        "candidate_chunks": [],
    }


def build_runtime_title_index() -> dict[str, dict[str, object]]:
    runtime_titles: dict[str, dict[str, object]] = {}
    for entry in RUNTIME_MUSIC_STATES:
        for phase_key in ("day", "night"):
            runtime_titles[entry[phase_key]] = {
                "state": entry["state"],
                "phase": phase_key,
                "lane": entry["lane"],
            }
    runtime_titles["AFK1"] = {"state": None, "phase": "always", "lane": "afk"}
    for sea_name in SEA_REGION_OVERRIDES:
        runtime_titles[sea_name] = {"state": None, "phase": "always", "lane": "sea"}
    return runtime_titles


def build_sqob_sample_ref_index(list_summaries: list[dict[str, object]]) -> dict[int, list[dict[str, object]]]:
    refs_by_index: dict[int, list[dict[str, object]]] = {}
    for item in list_summaries:
        if item.get("list_type") != "sqob":
            continue
        index = item.get("index")
        refs = item.get("sqob_sample_refs")
        if isinstance(index, int) and isinstance(refs, list) and refs:
            refs_by_index[index] = refs
    return refs_by_index


def simplify_music_variant(variant: dict[str, object], sqob_sample_refs_by_index: dict[int, list[dict[str, object]]]) -> dict[str, object]:
    simplified = {
        "order": variant.get("order"),
        "weight": variant.get("weight"),
        "target_type": variant.get("target_type"),
        "target_index": variant.get("target_index"),
        "target_title": variant.get("target_title"),
    }
    target_index = variant.get("target_index")
    if variant.get("target_type") == "sqob" and isinstance(target_index, int):
        sample_refs = sqob_sample_refs_by_index.get(target_index)
        if sample_refs:
            simplified["target_sample_refs"] = sample_refs
            simplified["target_sample_titles"] = [
                ref.get("target_title")
                for ref in sample_refs
                if isinstance(ref.get("target_title"), str)
            ]
    return simplified


def simplify_music_entry(
    entry: dict[str, object],
    runtime_titles: dict[str, dict[str, object]],
    sqob_sample_refs_by_index: dict[int, list[dict[str, object]]],
) -> dict[str, object]:
    title = str(entry.get("title") or "")
    runtime_binding = runtime_titles.get(title)
    return {
        "title": title,
        "signal_group": entry.get("signal_group"),
        "tracks": entry.get("tracks"),
        "tempo": entry.get("tempo"),
        "time_code": entry.get("time_code"),
        "section": entry.get("section"),
        "loop": entry.get("loop"),
        "sync": entry.get("sync"),
        "info": entry.get("info"),
        "runtime_binding": runtime_binding,
        "variants": [
            simplify_music_variant(variant, sqob_sample_refs_by_index)
            for variant in entry.get("rcnt_entries", [])
        ],
    }


def simplify_music_control_entry(
    entry: dict[str, object],
    sqob_sample_refs_by_index: dict[int, list[dict[str, object]]],
) -> dict[str, object]:
    payload = {
        "title": entry.get("title"),
        "list_type": entry.get("list_type"),
        "signal_group": entry.get("signal_group"),
        "tracks": entry.get("tracks"),
        "tempo": entry.get("tempo"),
        "loop": entry.get("loop"),
        "sync": entry.get("sync"),
        "info": entry.get("info"),
        "silt": entry.get("silt"),
        "time_code": entry.get("time_code"),
        "section": entry.get("section"),
        "variants": [
            simplify_music_variant(variant, sqob_sample_refs_by_index)
            for variant in entry.get("rcnt_entries", [])
        ],
        "trck_entries": entry.get("trck_entries", []),
        "data_u32_values": entry.get("data_u32_values", []),
    }
    return {
        key: value
        for key, value in payload.items()
        if value is not None and value != [] and value != {}
    }


def unique_targets_from_sound_entry(entry: dict[str, object]) -> list[dict[str, object]]:
    seen: set[tuple[object, object, object]] = set()
    targets: list[dict[str, object]] = []

    def record_target(title: object, sample_index: object, bank: object, source: str) -> None:
        key = (title, sample_index, bank)
        if key in seen:
            return
        seen.add(key)
        targets.append(
            {
                "title": title,
                "sample_index": sample_index,
                "bank": bank,
                "source": source,
            }
        )

    record_target(
        entry.get("sndt_primary_target_title"),
        entry.get("sndt_primary_target_sample_index"),
        entry.get("sndt_primary_target_bank"),
        "primary",
    )
    for record in entry.get("sndt_records", []):
        record_target(
            record.get("target_title"),
            record.get("target_sample_index"),
            record.get("target_bank"),
            "record",
        )
    for block in entry.get("sndt_record_blocks", []):
        record_target(
            block.get("target_title"),
            block.get("target_sample_index"),
            block.get("target_bank"),
            "record-block",
        )
    return [item for item in targets if item["title"] or item["sample_index"] is not None]


def simplify_record_block(block: dict[str, object]) -> dict[str, object]:
    return {
        "start_record": block.get("start_record"),
        "end_record": block.get("end_record"),
        "record_count": block.get("record_count"),
        "target_ref_mode": block.get("target_ref_mode"),
        "target_title": block.get("target_title"),
        "target_sample_index": block.get("target_sample_index"),
        "target_bank": block.get("target_bank"),
        "unresolved_pattern": block.get("unresolved_pattern"),
        "decoded_as": block.get("decoded_as"),
        "control_window_kind": block.get("control_window_kind"),
        "control_window_layout": block.get("control_window_layout"),
        "inferred_role": block.get("inferred_role"),
        "sample_count": block.get("sample_count"),
    }


def classify_ambience_lane(title: str) -> tuple[str | None, str | None]:
    lower = title.lower()
    time_of_day: str | None = None
    lane: str | None = None

    if lower.startswith("day"):
        time_of_day = "day"
        lower = lower[3:]
    elif lower.startswith("night"):
        time_of_day = "night"
        lower = lower[5:]

    key = compact_name(lower)
    if key == "ambience":
        lane = "ambience"
    elif key in {"oneshots", "oneshot"}:
        lane = "one_shots"
    elif key == "special":
        lane = "special"

    return time_of_day, lane


def simplify_sound_entry(entry: dict[str, object]) -> dict[str, object]:
    title = str(entry.get("title") or "")
    time_of_day, lane = classify_ambience_lane(title)
    return {
        "title": title,
        "signal_group": entry.get("signal_group"),
        "time_of_day": time_of_day,
        "lane": lane,
        "tracks": entry.get("tracks"),
        "tempo": entry.get("tempo"),
        "loop": entry.get("loop"),
        "sync": entry.get("sync"),
        "info": entry.get("info"),
        "primary_target": {
            "title": entry.get("sndt_primary_target_title"),
            "sample_index": entry.get("sndt_primary_target_sample_index"),
            "bank": entry.get("sndt_primary_target_bank"),
            "ref_mode": entry.get("sndt_primary_ref_mode"),
        },
        "sndt_record_count": entry.get("sndt_record_count"),
        "resolved_targets": unique_targets_from_sound_entry(entry),
        "record_blocks": [simplify_record_block(block) for block in entry.get("sndt_record_blocks", [])],
    }


def build_music_bundle(cue: dict[str, object], chunks: list[dict[str, object]], runtime_titles: dict[str, dict[str, object]], audio_profile_index: dict[str, dict] | None = None) -> dict[str, object]:
    cue_file = Path(str(cue["file"]))
    bundle_name = cue_file.stem
    semantic = cue.get("semantic_summary", {})
    list_summaries = semantic.get("list_summaries", [])
    sqob_sample_refs_by_index = build_sqob_sample_ref_index(list_summaries)
    sdri_entries = [item for item in list_summaries if item.get("list_type") == "sdri"]
    sqob_titles = sorted(
        str(item.get("title"))
        for item in list_summaries
        if item.get("list_type") == "sqob" and item.get("title")
    )
    ento_titles = sorted(
        str(item.get("title"))
        for item in list_summaries
        if item.get("list_type") == "ento" and item.get("title")
    )
    tran_titles = sorted(
        str(item.get("title"))
        for item in list_summaries
        if item.get("list_type") == "tran" and item.get("title")
    )
    matched_runtime_titles = sorted(
        title for title in (item.get("title") for item in sdri_entries) if title in runtime_titles
    )
    chunk_activation = build_chunk_activation(bundle_name, chunks)
    location_hints = [
        item
        for item in chunk_activation["activation_rules"]
        if item.get("type") == "chunk-id"
    ] or chunk_activation["candidate_chunks"]
    paired_isb = Path(str(cue.get("paired_isb") or "")).name or None
    audio_profile = lookup_audio_profile(audio_profile_index or {}, paired_isb)
    return {
        "bundle_name": bundle_name,
        "cue_file": str(cue_file),
        "paired_isb_bank": paired_isb,
        "location_hints": location_hints,
        "chunk_activation": chunk_activation,
        "audio_profile": audio_profile,
        "entry_points": ento_titles,
        "entry_point_details": [
            simplify_music_control_entry(item, sqob_sample_refs_by_index)
            for item in list_summaries
            if item.get("list_type") == "ento"
        ],
        "transitions": tran_titles,
        "transition_details": [
            simplify_music_control_entry(item, sqob_sample_refs_by_index)
            for item in list_summaries
            if item.get("list_type") == "tran"
        ],
        "matched_runtime_titles": matched_runtime_titles,
        "ctdx_fragments": semantic.get("ctdx_fragments", []),
        "sqob_titles": sqob_titles,
        "states": [
            simplify_music_entry(entry, runtime_titles, sqob_sample_refs_by_index)
            for entry in sdri_entries
        ],
    }


def build_ambience_bundle(cue: dict[str, object], audio_profile_index: dict[str, dict] | None = None) -> dict[str, object]:
    cue_file = Path(str(cue["file"]))
    bundle_name = cue_file.stem
    semantic = cue.get("semantic_summary", {})
    list_summaries = semantic.get("list_summaries", [])
    sound_entries = [simplify_sound_entry(entry) for entry in semantic.get("sound_entries", [])]
    channels: dict[str, dict[str, dict[str, object]]] = {"day": {}, "night": {}}
    auxiliary: list[dict[str, object]] = []
    for entry in sound_entries:
        time_of_day = entry.get("time_of_day")
        lane = entry.get("lane")
        if time_of_day in channels and lane:
            channels[time_of_day][lane] = entry
        else:
            auxiliary.append(entry)

    profile_names = [
        str(item.get("title"))
        for item in list_summaries
        if item.get("list_type") == "ento" and item.get("title")
    ]
    paired_isb = Path(str(cue.get("paired_isb") or "")).name or None
    audio_profile = lookup_audio_profile(audio_profile_index or {}, paired_isb)
    return {
        "bundle_name": bundle_name,
        "cue_file": str(cue_file),
        "paired_isb_bank": paired_isb,
        "profile_names": profile_names,
        "channels": channels,
        "auxiliary": auxiliary,
        "audio_profile": audio_profile,
    }


def build_manifest(
    cues: list[dict[str, object]],
    chunks: list[dict[str, object]],
    catalog_path: Path,
    chunk_reference_path: Path,
    audio_profile_index: dict[str, dict] | None = None,
) -> dict[str, object]:
    runtime_titles = build_runtime_title_index()
    music_bundles = [
        build_music_bundle(cue, chunks, runtime_titles, audio_profile_index)
        for cue in cues
        if is_world_music_cue(cue)
    ]
    ambience_bundles = [
        build_ambience_bundle(cue, audio_profile_index) for cue in cues if is_ambience_cue(cue)
    ]
    music_bundles.sort(key=lambda item: item["bundle_name"].lower())
    ambience_bundles.sort(key=lambda item: item["bundle_name"].lower())

    return {
        "summary": {
            "music_bundle_count": len(music_bundles),
            "ambience_bundle_count": len(ambience_bundles),
            "music_bundles_with_location_hints": sum(bool(item["location_hints"]) for item in music_bundles),
            "music_bundles_with_runtime_titles": sum(bool(item["matched_runtime_titles"]) for item in music_bundles),
            "music_bundles_with_direct_chunk_activation": sum(
                item["chunk_activation"]["resolution"] in {"chunk-exact", "chunk-token-subset"}
                for item in music_bundles
            ),
            "music_bundles_with_runtime_region_activation": sum(
                item["chunk_activation"]["resolution"] == "runtime-sea-region"
                for item in music_bundles
            ),
            "music_bundles_with_chunk_candidates": sum(
                bool(item["chunk_activation"]["candidate_chunks"])
                for item in music_bundles
            ),
            "music_bundles_with_audio_profile": sum(
                bool(item.get("audio_profile")) for item in music_bundles
            ),
            "ambience_bundles_with_audio_profile": sum(
                bool(item.get("audio_profile")) for item in ambience_bundles
            ),
            "ambience_bundles_with_distance_attenuation": sum(
                bool((item.get("audio_profile") or {}).get("spatial", {}) and (item["audio_profile"]["spatial"] or {}).get("distance_attenuation"))
                for item in ambience_bundles
            ),
        },
        "provenance": {
            "catalog": str(catalog_path),
            "chunk_reference": str(chunk_reference_path),
            "runtime_findings": "docs/audio/2026-05-04_ghidra_audio_runtime_findings.md",
        },
        "runtime": {
            "music_states": RUNTIME_MUSIC_STATES,
            "sea_region_overrides": SEA_REGION_OVERRIDES,
            "afk_fallback": "AFK1",
            "ambience_selectors": RUNTIME_AMBIENCE_SELECTORS,
        },
        "music_bundles": music_bundles,
        "ambience_bundles": ambience_bundles,
    }


def build_markdown(manifest: dict[str, object]) -> str:
    lines = [
        "# Music And Ambience Manifest",
        "",
        f"- Music bundles: {manifest['summary']['music_bundle_count']}",
        f"- Ambience bundles: {manifest['summary']['ambience_bundle_count']}",
        f"- Music bundles with chunk hints: {manifest['summary']['music_bundles_with_location_hints']}",
        f"- Music bundles with runtime title coverage: {manifest['summary']['music_bundles_with_runtime_titles']}",
        f"- Music bundles with direct chunk activation: {manifest['summary']['music_bundles_with_direct_chunk_activation']}",
        f"- Music bundles with runtime sea-region activation: {manifest['summary']['music_bundles_with_runtime_region_activation']}",
        f"- Music bundles with chunk candidates: {manifest['summary']['music_bundles_with_chunk_candidates']}",
        "",
        "## Runtime selectors",
        "",
    ]

    for item in manifest["runtime"]["music_states"]:
        lines.append(
            f"- State {item['state']}: {item['day']} / {item['night']} ({item['lane']})"
        )

    lines.extend([
        "",
        f"- AFK fallback: {manifest['runtime']['afk_fallback']}",
        f"- Sea overrides: {', '.join(manifest['runtime']['sea_region_overrides'])}",
        f"- Ambience selectors: {', '.join(manifest['runtime']['ambience_selectors'])}",
        "",
        "## Sample music bundles",
        "",
    ])

    for bundle in manifest["music_bundles"][:12]:
        runtime_titles = ", ".join(bundle["matched_runtime_titles"][:8]) or "-"
        activation = bundle["chunk_activation"]
        if activation["activation_rules"]:
            activation_targets = ", ".join(
                item.get("displayname", item.get("region_name", "-"))
                for item in activation["activation_rules"][:3]
            )
        else:
            activation_targets = ", ".join(
                item["displayname"] for item in activation["candidate_chunks"][:3]
            ) or "-"
        lines.append(
            f"- {bundle['bundle_name']}: runtime titles={runtime_titles}; activation={activation['resolution']} -> {activation_targets}"
        )

    lines.extend(["", "## Sample ambience bundles", ""])
    for bundle in manifest["ambience_bundles"][:12]:
        day_lanes = ", ".join(sorted(bundle["channels"]["day"].keys())) or "-"
        night_lanes = ", ".join(sorted(bundle["channels"]["night"].keys())) or "-"
        auxiliary = ", ".join(item["title"] for item in bundle["auxiliary"][:5]) or "-"
        lines.append(
            f"- {bundle['bundle_name']}: day={day_lanes}; night={night_lanes}; auxiliary={auxiliary}"
        )

    lines.extend([
        "",
        "## Notes",
        "",
        "- Direct music activation rules are emitted only for strong chunk-name joins or explicit runtime sea-region families. Lower-confidence name overlap stays in candidate_chunks for review instead of being treated as authoritative.",
        "- Exact region or polygon triggers inside a chunk are still unresolved; city or special-region state changes such as Alley, Pub, or Regal still need a stronger region-object join.",
        "- Ambience entries already carry resolved sample targets where sndt decoding succeeded, so the manifest is directly useful for first-pass playback wiring.",
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    catalog_path = args.catalog.expanduser().resolve()
    chunk_reference_path = args.chunk_reference.expanduser().resolve()
    out_root = args.out.expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    cues = load_catalog(catalog_path)
    chunks = load_chunks(chunk_reference_path)
    isb_inspect_dir = args.isb_inspect.expanduser().resolve()
    audio_profile_index = build_isb_audio_profile_index(isb_inspect_dir)
    manifest = build_manifest(cues, chunks, catalog_path, chunk_reference_path, audio_profile_index)

    json_path = out_root / "manifest.json"
    md_path = out_root / "manifest.md"
    json_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    md_path.write_text(build_markdown(manifest), encoding="utf-8")

    print(f"Music bundles: {manifest['summary']['music_bundle_count']}")
    print(f"Ambience bundles: {manifest['summary']['ambience_bundle_count']}")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())