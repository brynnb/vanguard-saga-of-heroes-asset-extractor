#!/usr/bin/env python3
"""Build likely server-animation-id to extracted clip-name candidates.

Inputs:
  - output/research/acn_aby_mappings/server_animation_crosswalk.jsonl
  - output/meshes/emfx_animations/manifest.json
  - output/meshes/animations/manifest.json

The output is a candidate bridge, not a final runtime resolver. Server
animation IDs resolve to ACN/ABY descriptors, while the final skeletal clip is
actor-specific and must be selected from the actor mesh's AnimSet packages.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MAPPING = ROOT / "output" / "research" / "acn_aby_mappings" / "server_animation_crosswalk.jsonl"
DEFAULT_EMFX_MANIFEST = ROOT / "output" / "meshes" / "emfx_animations" / "manifest.json"
DEFAULT_UKX_MANIFEST = ROOT / "output" / "meshes" / "animations" / "manifest.json"
DEFAULT_OUT = ROOT / "output" / "research" / "animation_clip_crosswalk"

ACTION_CAST_MAP = {
    "ALT": "altCast",
    "CON": "conCast",
    "EVO": "evoCast",
}
ACTION_PHASE_MAP = {
    "INTRO": "Intro",
    "LOOP": "Loop",
    "EXIT": "Exit",
}
COMMON_ACTION_TOKENS = {
    "ATTACK": "attack",
    "BOW": "bow",
    "CASTINFLUENCE": "castInfluence",
    "DISMISS": "dismiss",
    "HEADNOD": "headNod",
    "HEADSHAKE": "headShake",
    "HUG": "hug",
    "IMPACT": "impact",
    "KNEEL": "kneel",
    "MERCHANTSELL": "merchantSell",
    "PARRY": "parry",
    "PLEAD": "plead",
    "SALUTE": "salute",
    "SHAKEHAND": "shakeHand",
    "THROW": "throw",
    "WAVE": "wave",
    "WORSHIP": "worship",
}


@dataclass(frozen=True)
class ClipQuery:
    pattern: str
    reason: str
    score: int
    source: str
    exact: bool = False


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text)


def unique_queries(queries: list[ClipQuery]) -> list[ClipQuery]:
    seen: set[tuple[str, str, bool]] = set()
    result: list[ClipQuery] = []
    for query in sorted(queries, key=lambda item: (-item.score, item.source, item.pattern)):
        key = (normalize(query.pattern), query.source, query.exact)
        if key in seen:
            continue
        seen.add(key)
        result.append(query)
    return result


def add_query(
    queries: list[ClipQuery],
    pattern: str,
    reason: str,
    score: int,
    source: str,
    exact: bool = False,
) -> None:
    if not pattern or len(normalize(pattern)) < 3:
        return
    queries.append(ClipQuery(pattern=pattern, reason=reason, score=score, source=source, exact=exact))


def camel_from_tokens(tokens: list[str]) -> str:
    if not tokens:
        return ""
    lower = [token.lower() for token in tokens]
    return lower[0] + "".join(token[:1].upper() + token[1:] for token in lower[1:])


def cast_action_queries(action_prefix: str) -> list[ClipQuery]:
    queries: list[ClipQuery] = []
    upper = action_prefix.upper()

    match = re.search(r"(?:^|_)ANIM_(ALT|CON|EVO)_(INTRO|LOOP|EXIT)_?(\d+)?", upper)
    if match:
        cast_kind, phase, number = match.groups()
        pattern = ACTION_CAST_MAP[cast_kind] + ACTION_PHASE_MAP[phase] + (number or "")
        add_query(
            queries,
            pattern,
            f"action_prefix:{action_prefix}",
            92 if number is not None else 84,
            "action_cast_phase",
        )

    npc_match = re.search(r"(?:^|_)NPC_ANIM_(ALT|CON|EVO)_(INTRO|LOOP|EXIT)_?(\d+)?", upper)
    if npc_match:
        cast_kind, phase, number = npc_match.groups()
        pattern = ACTION_CAST_MAP[cast_kind] + ACTION_PHASE_MAP[phase] + (number or "")
        add_query(
            queries,
            pattern,
            f"npc_action_prefix:{action_prefix}",
            88 if number is not None else 80,
            "action_cast_phase",
        )

    if re.search(r"(?:^|_)ANIM_IMPACT(?:_|$)", upper):
        add_query(queries, "impact", f"action_prefix:{action_prefix}", 72, "action_generic")

    return queries


def social_action_queries(action_prefix: str) -> list[ClipQuery]:
    queries: list[ClipQuery] = []
    upper = action_prefix.upper()
    match = re.match(r"SOC(?:IAL)?_ANIM_(.+)", upper)
    if not match:
        return queries

    tokens = [token for token in match.group(1).split("_") if token and not token.isdigit()]
    if not tokens:
        return queries

    compact = "".join(tokens)
    camel = camel_from_tokens(tokens)
    add_query(queries, camel, f"social_action_prefix:{action_prefix}", 78, "social_action")
    add_query(queries, tokens[0].lower(), f"social_action_token:{action_prefix}", 62, "social_action")
    if compact in COMMON_ACTION_TOKENS:
        add_query(queries, COMMON_ACTION_TOKENS[compact], f"social_action_known:{action_prefix}", 82, "social_action")
    return queries


def generic_action_queries(action_prefix: str) -> list[ClipQuery]:
    queries: list[ClipQuery] = []
    upper = action_prefix.upper()
    compact_words = words(upper)

    if upper.startswith(("PARTICLE_", "NPC_PARTICLE_", "FX_", "BLADE_", "SPELL_PARTICLE_")):
        return queries

    for token, pattern in COMMON_ACTION_TOKENS.items():
        if token in compact_words or token in upper:
            score = 58
            if token in {"IMPACT", "THROW", "BOW", "WAVE", "SALUTE"}:
                score = 66
            add_query(queries, pattern, f"action_token:{action_prefix}", score, "action_token")
    return queries


def ability_symbol_queries(symbol: str, roles: dict[str, int]) -> list[ClipQuery]:
    queries: list[ClipQuery] = []
    upper = symbol.upper()
    compact_words = words(upper)

    if "THROW" in compact_words or "THROW" in upper:
        add_query(queries, "throw", f"ability_symbol:{symbol}", 58, "ability_token")
    if "BOW" in compact_words or "RANGED_ATTACK_BOW" in upper:
        add_query(queries, "bow", f"ability_symbol:{symbol}", 54, "ability_token")
    if ("IMPACT" in compact_words or "RESOLVE" in compact_words) and (
        roles.get("impact", 0) or roles.get("secondary", 0)
    ):
        add_query(queries, "impact", f"ability_symbol:{symbol}", 48, "ability_token")

    compact = "".join(compact_words)
    for token, pattern in COMMON_ACTION_TOKENS.items():
        if token in compact:
            add_query(queries, pattern, f"ability_known_token:{symbol}", 50, "ability_token")
    return queries


def role_queries(roles: dict[str, int]) -> list[ClipQuery]:
    queries: list[ClipQuery] = []
    if roles.get("impact", 0) or roles.get("secondary", 0):
        add_query(queries, "impact", "server_role:impact_or_secondary", 32, "server_role")
    return queries


def build_queries(row: dict[str, Any]) -> list[ClipQuery]:
    queries: list[ClipQuery] = []
    roles = row.get("roles", {})

    for action in row.get("action_matches", []):
        prefix = action.get("object_prefix") or action.get("object_name") or ""
        queries.extend(cast_action_queries(prefix))
        queries.extend(social_action_queries(prefix))
        queries.extend(generic_action_queries(prefix))

    for ability in row.get("ability_matches", []):
        for symbol in ability.get("ascii_symbols", []):
            queries.extend(ability_symbol_queries(symbol, roles))

    queries.extend(role_queries(roles))
    return unique_queries(queries)


def load_emfx_clips(manifest_path: Path, max_meshes: int) -> list[dict[str, Any]]:
    if not manifest_path.exists():
        return []

    manifest = load_json(manifest_path)
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for mesh_name, mesh_info in manifest.items():
        for package_entry in mesh_info.get("uea_packages", []):
            package = package_entry.get("package", "")
            for clip in package_entry.get("clips", []):
                clip_name = clip.get("name", "")
                path = clip.get("path", "")
                key = ("emfx", package, path)
                record = grouped.setdefault(
                    key,
                    {
                        "source": "emfx",
                        "package": package,
                        "clip_name": clip_name,
                        "path": path,
                        "bones": clip.get("bones"),
                        "duration": clip.get("duration"),
                        "mesh_count": 0,
                        "meshes": [],
                        "_name_norm": normalize(clip_name),
                        "_name_lower": clip_name.lower(),
                    },
                )
                record["mesh_count"] += 1
                if len(record["meshes"]) < max_meshes and mesh_name not in record["meshes"]:
                    record["meshes"].append(mesh_name)

    return sorted(grouped.values(), key=lambda item: (item["source"], item["package"], item["clip_name"]))


def load_ukx_clips(manifest_path: Path, max_meshes: int) -> list[dict[str, Any]]:
    if not manifest_path.exists():
        return []

    manifest = load_json(manifest_path)
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for package_name, package_info in manifest.items():
        for anim in package_info.get("anims", []):
            anim_name = anim.get("name", "")
            path = anim.get("path", "")
            for clip in anim.get("clips", []):
                clip_name = clip.get("name", "")
                key = ("ukx", package_name, anim_name, clip_name)
                record = grouped.setdefault(
                    key,
                    {
                        "source": "ukx",
                        "package": package_name,
                        "anim_name": anim_name,
                        "clip_name": clip_name,
                        "path": path,
                        "channels": clip.get("channels"),
                        "mesh_count": 0,
                        "meshes": [],
                        "_name_norm": normalize(clip_name),
                        "_name_lower": clip_name.lower(),
                    },
                )
                record["mesh_count"] += 1
                if len(record["meshes"]) < max_meshes and package_name not in record["meshes"]:
                    record["meshes"].append(package_name)

    return sorted(grouped.values(), key=lambda item: (item["source"], item["package"], item["clip_name"]))


def public_clip(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def query_matches_clip(query: ClipQuery, clip: dict[str, Any]) -> bool:
    pattern_norm = normalize(query.pattern)
    if query.exact:
        return clip["_name_norm"] == pattern_norm
    if pattern_norm == "throw":
        return re.search(r"throw(?!n)", clip["_name_lower"]) is not None
    if pattern_norm == "bow":
        return "bow" in clip["_name_lower"] and "elbow" not in clip["_name_lower"]
    return pattern_norm in clip["_name_norm"] or query.pattern.lower() in clip["_name_lower"]


def build_candidates(
    queries: list[ClipQuery],
    clips: list[dict[str, Any]],
    max_candidates: int,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    by_clip: dict[tuple[str, str, str], dict[str, Any]] = {}
    query_hits: Counter[str] = Counter()

    for query in queries:
        for clip in clips:
            if not query_matches_clip(query, clip):
                continue

            key = (clip["source"], clip["package"], clip["path"])
            entry = by_clip.get(key)
            if entry is None:
                entry = public_clip(clip)
                entry["score"] = query.score
                entry["match_reasons"] = []
                by_clip[key] = entry
            else:
                entry["score"] = max(entry["score"], query.score)

            reason = {
                "pattern": query.pattern,
                "source": query.source,
                "reason": query.reason,
                "score": query.score,
            }
            if reason not in entry["match_reasons"]:
                entry["match_reasons"].append(reason)
            query_hits[query.source] += 1

    candidates = sorted(
        by_clip.values(),
        key=lambda item: (
            -int(item["score"]),
            item["source"] != "emfx",
            -int(item.get("mesh_count", 0)),
            str(item["package"]),
            str(item["clip_name"]),
        ),
    )
    return candidates[:max_candidates], query_hits


def read_mapping_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def summarize(rows: list[dict[str, Any]], clips: list[dict[str, Any]]) -> dict[str, Any]:
    score_buckets = Counter()
    query_sources = Counter()
    action_prefixes_without_candidates = Counter()
    ability_symbols_without_candidates = Counter()
    candidate_sources = Counter()

    for row in rows:
        best_score = row.get("best_clip_score") or 0
        if best_score >= 85:
            score_buckets["strong"] += 1
        elif best_score >= 60:
            score_buckets["medium"] += 1
        elif best_score > 0:
            score_buckets["weak"] += 1
        else:
            score_buckets["none"] += 1

        for query in row.get("clip_queries", []):
            query_sources[query["source"]] += 1
        for candidate in row.get("clip_candidates", []):
            candidate_sources[candidate["source"]] += 1

        if not row.get("clip_candidates"):
            for action in row.get("action_matches", []):
                action_prefixes_without_candidates[action.get("object_prefix") or action.get("object_name") or ""] += 1
            for ability in row.get("ability_matches", []):
                for symbol in ability.get("ascii_symbols", []):
                    ability_symbols_without_candidates[symbol] += 1

    return {
        "server_animation_ids": len(rows),
        "clip_inventory_count": len(clips),
        "clip_inventory_by_source": dict(Counter(clip["source"] for clip in clips)),
        "candidate_score_buckets": dict(score_buckets),
        "query_sources": dict(query_sources),
        "candidate_sources": dict(candidate_sources),
        "no_candidate_count": score_buckets["none"],
        "top_action_prefixes_without_candidates": dict(action_prefixes_without_candidates.most_common(30)),
        "top_ability_symbols_without_candidates": dict(ability_symbols_without_candidates.most_common(30)),
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--emfx-manifest", type=Path, default=DEFAULT_EMFX_MANIFEST)
    parser.add_argument("--ukx-manifest", type=Path, default=DEFAULT_UKX_MANIFEST)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-candidates", type=int, default=24)
    parser.add_argument("--max-meshes", type=int, default=12)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    out_root = args.out.expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    rows = read_mapping_rows(args.mapping.expanduser().resolve())
    clips = load_emfx_clips(args.emfx_manifest.expanduser().resolve(), args.max_meshes)
    clips.extend(load_ukx_clips(args.ukx_manifest.expanduser().resolve(), args.max_meshes))

    output_rows: list[dict[str, Any]] = []
    for row in rows:
        queries = build_queries(row)
        candidates, query_hits = build_candidates(queries, clips, args.max_candidates)
        output_row = dict(row)
        output_row["clip_queries"] = [
            {
                "pattern": query.pattern,
                "reason": query.reason,
                "score": query.score,
                "source": query.source,
                "exact": query.exact,
            }
            for query in queries
        ]
        output_row["clip_candidate_count"] = len(candidates)
        output_row["best_clip_score"] = candidates[0]["score"] if candidates else 0
        output_row["clip_candidates"] = candidates
        output_row["query_hit_counts"] = dict(query_hits)
        output_rows.append(output_row)

    summary = summarize(output_rows, clips)
    write_jsonl(out_root / "server_animation_clip_crosswalk.jsonl", output_rows)
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Server animation IDs: {len(output_rows)}")
    print(f"Clip inventory: {len(clips)}")
    print(f"Score buckets: {summary['candidate_score_buckets']}")
    print(f"Wrote clip crosswalk to {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
