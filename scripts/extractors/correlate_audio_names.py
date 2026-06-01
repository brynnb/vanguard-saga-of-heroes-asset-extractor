#!/usr/bin/env python3
"""Correlate Ghidra-discovered sound-set names with cataloged audio assets.

The main use case is matching client-code identifiers such as
`Human_Male_T_ASounds` or `RatmanSounds` against:

- cataloged `.uax` packages
- cataloged `.isb` banks
- cataloged `.icb` cue bundles
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*(?:ASounds|SSounds|Sounds))\b")


def compact_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def alias_variants(name: str) -> set[str]:
    compact = compact_name(name)
    aliases = {compact}
    for suffix in ("sounds", "sound"):
        if compact.endswith(suffix):
            trimmed = compact[: -len(suffix)]
            if trimmed:
                aliases.add(trimmed)

    # Ghidra/client identifiers occasionally drop the terminal "e" from the
    # underlying asset stem (for example StirgSounds vs StirgeSounds).
    expanded = set(aliases)
    for alias in aliases:
        if alias.endswith("e") and len(alias) > 1:
            expanded.add(alias[:-1])
        else:
            expanded.add(alias + "e")
    return expanded


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def load_catalog_entries(path: Path, kind: str) -> list[dict[str, object]]:
    payload = load_json(path)
    if kind == "uax":
        entries = payload["packages"]
        def name_of(entry: dict[str, object]) -> str:
            return Path(entry["file"]).stem
    elif kind == "isb":
        entries = payload["banks"]
        def name_of(entry: dict[str, object]) -> str:
            return Path(entry["bank"]).stem
    elif kind == "icb":
        entries = payload["cues"]
        def name_of(entry: dict[str, object]) -> str:
            return Path(entry["file"]).stem
    else:
        raise ValueError(f"Unsupported catalog kind: {kind}")

    result = []
    for entry in entries:
        name = name_of(entry)
        result.append({
            "name": name,
            "aliases": sorted(alias_variants(name)),
            "entry": entry,
        })
    return result


def build_index(entries: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    index: dict[str, list[dict[str, object]]] = {}
    for item in entries:
        for alias in item["aliases"]:
            index.setdefault(alias, []).append(item)
    return index


def scrape_ghidra_tokens(ghidra_dir: Path) -> dict[str, list[str]]:
    token_sources: dict[str, set[str]] = {}
    for path in ghidra_dir.rglob("*.json"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in TOKEN_RE.findall(text):
            token_sources.setdefault(match, set()).add(str(path))
    return {token: sorted(paths) for token, paths in sorted(token_sources.items())}


def match_token(token: str, source_files: list[str], indices: dict[str, dict[str, list[dict[str, object]]]]) -> dict[str, object]:
    aliases = sorted(alias_variants(token))
    result = {
        "token": token,
        "aliases": aliases,
        "ghidra_sources": source_files,
        "matches": {},
    }
    for kind, index in indices.items():
        matches: dict[str, dict[str, object]] = {}
        for alias in aliases:
            for item in index.get(alias, []):
                matches[item["name"]] = {
                    "name": item["name"],
                    "matched_alias": alias,
                    "entry": item["entry"],
                }
        result["matches"][kind] = sorted(matches.values(), key=lambda item: item["name"].lower())
    return result


def summarize(matches: list[dict[str, object]]) -> dict[str, object]:
    summary = {
        "token_count": len(matches),
        "matched_uax": 0,
        "matched_isb": 0,
        "matched_icb": 0,
        "matched_all_three": 0,
        "unmatched": 0,
    }
    for item in matches:
        has_uax = bool(item["matches"]["uax"])
        has_isb = bool(item["matches"]["isb"])
        has_icb = bool(item["matches"]["icb"])
        summary["matched_uax"] += int(has_uax)
        summary["matched_isb"] += int(has_isb)
        summary["matched_icb"] += int(has_icb)
        summary["matched_all_three"] += int(has_uax and has_isb and has_icb)
        summary["unmatched"] += int(not (has_uax or has_isb or has_icb))
    return summary


def build_markdown(summary: dict[str, object], matches: list[dict[str, object]]) -> str:
    lines = [
        "# Audio Name Correlation",
        "",
        f"- Ghidra tokens: {summary['token_count']}",
        f"- Tokens matching UAX: {summary['matched_uax']}",
        f"- Tokens matching ISB: {summary['matched_isb']}",
        f"- Tokens matching ICB: {summary['matched_icb']}",
        f"- Tokens matching all three: {summary['matched_all_three']}",
        f"- Unmatched tokens: {summary['unmatched']}",
        "",
        "## Sample matches",
        "",
    ]

    sample_rows = [item for item in matches if item["matches"]["uax"] or item["matches"]["isb"] or item["matches"]["icb"]][:40]
    for item in sample_rows:
        uax_names = ", ".join(match["name"] for match in item["matches"]["uax"][:3]) or "-"
        isb_names = ", ".join(match["name"] for match in item["matches"]["isb"][:3]) or "-"
        icb_names = ", ".join(match["name"] for match in item["matches"]["icb"][:3]) or "-"
        lines.append(f"- {item['token']}: UAX={uax_names}; ISB={isb_names}; ICB={icb_names}")

    unmatched = [item["token"] for item in matches if not (item["matches"]["uax"] or item["matches"]["isb"] or item["matches"]["icb"])]
    if unmatched:
        lines.extend(["", "## Unmatched tokens", ""])
        lines.extend(f"- {token}" for token in unmatched[:40])

    return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ghidra-dir", type=Path, default=Path("ghidra"), help="Directory containing Ghidra JSON artifacts")
    parser.add_argument("--uax-catalog", type=Path, default=Path("output/audio/catalog_uax/packages.json"))
    parser.add_argument("--isb-catalog", type=Path, default=Path("output/audio/catalog_isb/banks.json"))
    parser.add_argument("--icb-catalog", type=Path, default=Path("output/audio/catalog_icb/cues.json"))
    parser.add_argument("--out", type=Path, default=Path("output/audio/correlation"), help="Output directory for JSON and markdown reports")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    out_root = args.out.expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    catalogs = {
        "uax": load_catalog_entries(args.uax_catalog.expanduser().resolve(), "uax"),
        "isb": load_catalog_entries(args.isb_catalog.expanduser().resolve(), "isb"),
        "icb": load_catalog_entries(args.icb_catalog.expanduser().resolve(), "icb"),
    }
    indices = {kind: build_index(entries) for kind, entries in catalogs.items()}
    token_sources = scrape_ghidra_tokens(args.ghidra_dir.expanduser().resolve())
    matches = [match_token(token, sources, indices) for token, sources in token_sources.items()]
    summary = summarize(matches)

    json_path = out_root / "audio_name_matches.json"
    md_path = out_root / "audio_name_matches.md"
    json_path.write_text(json.dumps({"summary": summary, "matches": matches}, indent=2), encoding="utf-8")
    md_path.write_text(build_markdown(summary, matches), encoding="utf-8")

    print(f"Matched {summary['token_count']} Ghidra tokens")
    print(f"  UAX: {summary['matched_uax']}")
    print(f"  ISB: {summary['matched_isb']}")
    print(f"  ICB: {summary['matched_icb']}")
    print(f"  All three: {summary['matched_all_three']}")
    print(f"  Unmatched: {summary['unmatched']}")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())