#!/usr/bin/env python3
"""Aggregate semantic summaries from the enriched ICB catalog."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load_catalog(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text())
    return payload["cues"]


def classify_cue(cue: dict[str, object]) -> str:
    semantic = cue.get("semantic_summary", {})
    sound_entries = semantic.get("sound_entries", [])
    list_summaries = semantic.get("list_summaries", [])
    list_types = {item.get("list_type") for item in list_summaries}
    if sound_entries:
        return "sound"
    if list_types & {"sdri", "sqob", "ento", "tran"}:
        return "music"
    return "unknown"


def build_summary(cues: list[dict[str, object]]) -> dict[str, object]:
    cue_kinds = Counter()
    list_types = Counter()
    signal_groups = Counter()
    sound_entry_histogram = Counter()
    ctdx_histogram = Counter()
    ctdx_layout_counts = Counter()
    ctdx_page_histogram = Counter()
    ctdx_record_histogram = Counter()
    ctdx_named_record_histogram = Counter()
    ctdx_record_tag_counts = Counter()
    ctdx_name_offset_counts = Counter()
    ctdx_footer_tag_counts = Counter()
    seai_record_histogram = Counter()
    seai_named_record_histogram = Counter()
    seai_record_size_histogram = Counter()
    seai_parse_errors = Counter()
    seai_parse_warnings = Counter()
    seai_record_names = Counter()
    sndt_resolution_modes = Counter()
    sndt_unresolved_signal_groups = Counter()
    sndt_total = 0
    sndt_resolved = 0
    sndt_record_count_histogram = Counter()
    sndt_record_resolution_modes = Counter()
    sndt_unresolved_record_patterns = Counter()
    sndt_control_window_record_kinds = Counter()
    sndt_control_window_record_roles = Counter()
    sndt_control_window_record_layouts = Counter()
    sndt_unresolved_block_patterns = Counter()
    sndt_unresolved_block_roles = Counter()
    sndt_unresolved_block_role_slots: dict[str, Counter[int]] = {}
    sndt_control_window_block_kinds = Counter()
    sndt_control_window_block_roles = Counter()
    sndt_control_window_block_layouts = Counter()
    sndt_record_block_histogram = Counter()
    sndt_unresolved_block_total = 0
    sndt_record_total = 0
    sndt_record_resolved = 0
    sndt_control_window_record_total = 0
    sndt_control_window_block_total = 0
    rcnt_target_types = Counter()
    rcnt_entry_histogram = Counter()
    rcnt_weight_sum_histogram = Counter()
    sdri_total = 0
    sdri_with_rcnt = 0
    sdri_fully_resolved = 0
    largest_sound_cues: list[dict[str, object]] = []
    largest_music_cues: list[dict[str, object]] = []

    for cue in cues:
        semantic = cue.get("semantic_summary", {})
        list_summaries = semantic.get("list_summaries", [])
        sound_entries = semantic.get("sound_entries", [])
        ctdx_fragments = semantic.get("ctdx_fragments", [])
        ctdx_layout = semantic.get("ctdx_layout")
        ctdx_page_count = semantic.get("ctdx_page_count")
        ctdx_record_count = semantic.get("ctdx_record_count")
        ctdx_named_record_count = semantic.get("ctdx_named_record_count")
        seai_record_count = semantic.get("seai_record_count")
        seai_named_record_count = semantic.get("seai_named_record_count")
        kind = classify_cue(cue)
        cue_kinds[kind] += 1
        sound_entry_histogram[len(sound_entries)] += 1
        ctdx_histogram[len(ctdx_fragments)] += 1
        if isinstance(ctdx_layout, str):
            ctdx_layout_counts[ctdx_layout] += 1
        if isinstance(ctdx_page_count, int):
            ctdx_page_histogram[ctdx_page_count] += 1
        if isinstance(ctdx_record_count, int):
            ctdx_record_histogram[ctdx_record_count] += 1
        if isinstance(ctdx_named_record_count, int):
            ctdx_named_record_histogram[ctdx_named_record_count] += 1
        record_tag_counts = semantic.get("ctdx_record_tag_counts")
        if isinstance(record_tag_counts, dict):
            ctdx_record_tag_counts.update(
                {str(tag): int(count) for tag, count in record_tag_counts.items() if isinstance(count, int)}
            )
        name_offset_counts = semantic.get("ctdx_name_offset_counts")
        if isinstance(name_offset_counts, dict):
            ctdx_name_offset_counts.update(
                {str(offset): int(count) for offset, count in name_offset_counts.items() if isinstance(count, int)}
            )
        ctdx_footer = semantic.get("ctdx_footer")
        if isinstance(ctdx_footer, dict):
            footer_tags = ctdx_footer.get("tags")
            if isinstance(footer_tags, list):
                for tag in footer_tags:
                    if isinstance(tag, dict) and isinstance(tag.get("tag"), str):
                        ctdx_footer_tag_counts[tag["tag"]] += 1
        if isinstance(seai_record_count, int) and seai_record_count > 0:
            seai_record_histogram[seai_record_count] += 1
        if isinstance(seai_named_record_count, int) and seai_record_count:
            seai_named_record_histogram[seai_named_record_count] += 1
        seai_record_size = semantic.get("seai_record_size")
        if isinstance(seai_record_size, int):
            seai_record_size_histogram[seai_record_size] += 1
        seai_parse_error = semantic.get("seai_record_parse_error")
        if isinstance(seai_parse_error, str):
            seai_parse_errors[seai_parse_error] += 1
        seai_parse_warning = semantic.get("seai_record_parse_warning")
        if isinstance(seai_parse_warning, str):
            seai_parse_warnings[seai_parse_warning] += 1
        names = semantic.get("seai_record_names")
        if isinstance(names, list):
            seai_record_names.update(str(name) for name in names if isinstance(name, str))

        for item in sound_entries:
            if item.get("sndt_u32_preview"):
                sndt_total += 1
                record_count = item.get("sndt_record_count")
                if isinstance(record_count, int):
                    sndt_record_count_histogram[record_count] += 1
                ref_mode = item.get("sndt_primary_ref_mode")
                if isinstance(ref_mode, str):
                    sndt_resolved += 1
                    sndt_resolution_modes[ref_mode] += 1
                else:
                    group = item.get("signal_group") or "<unknown>"
                    sndt_unresolved_signal_groups[group] += 1

            sndt_records = item.get("sndt_records")
            if isinstance(sndt_records, list):
                sndt_record_total += len(sndt_records)
                for record in sndt_records:
                    if record.get("decoded_as") == "control-window":
                        sndt_control_window_record_total += 1
                        kind = record.get("control_window_kind")
                        if isinstance(kind, str):
                            sndt_control_window_record_kinds[kind] += 1
                        role = record.get("control_window_role")
                        if isinstance(role, str):
                            sndt_control_window_record_roles[role] += 1
                        layout = record.get("control_window_layout")
                        if isinstance(layout, str):
                            sndt_control_window_record_layouts[layout] += 1
                        continue
                    ref_mode = record.get("target_ref_mode")
                    if isinstance(ref_mode, str):
                        sndt_record_resolved += 1
                        sndt_record_resolution_modes[ref_mode] += 1
                    else:
                        pattern = record.get("unresolved_pattern")
                        if isinstance(pattern, str):
                            sndt_unresolved_record_patterns[pattern] += 1

            sndt_record_blocks = item.get("sndt_record_blocks")
            if isinstance(sndt_record_blocks, list):
                sndt_record_block_histogram[len(sndt_record_blocks)] += 1
                for block in sndt_record_blocks:
                    if block.get("decoded_as") == "control-window":
                        sndt_control_window_block_total += 1
                        kind = block.get("control_window_kind")
                        if isinstance(kind, str):
                            sndt_control_window_block_kinds[kind] += 1
                        role = block.get("inferred_role")
                        if isinstance(role, str):
                            sndt_control_window_block_roles[role] += 1
                        layout = block.get("control_window_layout")
                        if isinstance(layout, str):
                            sndt_control_window_block_layouts[layout] += 1
                    pattern = block.get("unresolved_pattern")
                    if isinstance(pattern, str):
                        sndt_unresolved_block_total += 1
                        sndt_unresolved_block_patterns[pattern] += 1
                        role = block.get("inferred_role")
                        if isinstance(role, str):
                            sndt_unresolved_block_roles[role] += 1
                            post_bank_slot = block.get("post_bank_slot")
                            if isinstance(post_bank_slot, int):
                                sndt_unresolved_block_role_slots.setdefault(role, Counter())[post_bank_slot] += 1

        for item in list_summaries:
            list_type = item.get("list_type")
            if list_type:
                list_types[list_type] += 1
            group = item.get("signal_group")
            if group:
                signal_groups[group] += 1
            if list_type == "sdri":
                sdri_total += 1
                rcnt_entries = item.get("rcnt_entries", [])
                rcnt_entry_histogram[len(rcnt_entries)] += 1
                if rcnt_entries:
                    sdri_with_rcnt += 1
                    rcnt_weight_sum_histogram[
                        sum(int(entry.get("weight", 0)) for entry in rcnt_entries)
                    ] += 1
                    if all(entry.get("target_title") for entry in rcnt_entries):
                        sdri_fully_resolved += 1
                for entry in rcnt_entries:
                    target_type = entry.get("target_type")
                    if target_type:
                        rcnt_target_types[target_type] += 1

        candidate = {
            "file": cue["file"],
            "title": cue.get("title"),
            "count": len(sound_entries) if kind == "sound" else len(list_summaries),
            "ctdx_fragments": len(ctdx_fragments),
        }
        if kind == "sound":
            largest_sound_cues.append(candidate)
        elif kind == "music":
            largest_music_cues.append(candidate)

    largest_sound_cues.sort(key=lambda item: (-item["count"], item["file"]))
    largest_music_cues.sort(key=lambda item: (-item["count"], item["file"]))

    return {
        "total_cues": len(cues),
        "cue_kinds": dict(cue_kinds),
        "list_types": dict(list_types.most_common()),
        "signal_groups": dict(signal_groups.most_common()),
        "sound_entry_histogram": dict(sorted(sound_entry_histogram.items())),
        "ctdx_fragment_histogram": dict(sorted(ctdx_histogram.items())),
        "ctdx": {
            "layout_counts": dict(ctdx_layout_counts.most_common()),
            "page_count_histogram": dict(sorted(ctdx_page_histogram.items())),
            "record_count_histogram": dict(sorted(ctdx_record_histogram.items())),
            "named_record_count_histogram": dict(sorted(ctdx_named_record_histogram.items())),
            "record_tag_counts": dict(ctdx_record_tag_counts.most_common()),
            "name_offset_counts": dict(sorted(ctdx_name_offset_counts.items(), key=lambda item: int(item[0]))),
            "footer_tag_counts": dict(ctdx_footer_tag_counts.most_common()),
        },
        "seai": {
            "record_count_histogram": dict(sorted(seai_record_histogram.items())),
            "named_record_count_histogram": dict(sorted(seai_named_record_histogram.items())),
            "record_size_histogram": dict(sorted(seai_record_size_histogram.items())),
            "parse_errors": dict(seai_parse_errors.most_common()),
            "parse_warnings": dict(seai_parse_warnings.most_common()),
            "top_record_names": dict(seai_record_names.most_common(40)),
        },
        "sndt": {
            "total": sndt_total,
            "resolved": sndt_resolved,
            "unresolved": sndt_total - sndt_resolved,
            "resolution_modes": dict(sndt_resolution_modes.most_common()),
            "unresolved_signal_groups": dict(sndt_unresolved_signal_groups.most_common()),
            "record_total": sndt_record_total,
            "record_resolved": sndt_record_resolved,
            "record_unresolved": sndt_record_total - sndt_record_resolved,
            "record_control_window": sndt_control_window_record_total,
            "record_unresolved_after_control_window": sndt_record_total - sndt_record_resolved - sndt_control_window_record_total,
            "record_resolution_modes": dict(sndt_record_resolution_modes.most_common()),
            "record_count_histogram": dict(sorted(sndt_record_count_histogram.items())),
            "unresolved_record_patterns": dict(sndt_unresolved_record_patterns.most_common()),
            "control_window_record_kinds": dict(sndt_control_window_record_kinds.most_common()),
            "control_window_record_roles": dict(sndt_control_window_record_roles.most_common()),
            "control_window_record_layouts": dict(sndt_control_window_record_layouts.most_common()),
            "record_block_histogram": dict(sorted(sndt_record_block_histogram.items())),
            "unresolved_block_total": sndt_unresolved_block_total,
            "control_window_block_total": sndt_control_window_block_total,
            "unresolved_block_after_control_window": sndt_unresolved_block_total - sndt_control_window_block_total,
            "unresolved_block_patterns": dict(sndt_unresolved_block_patterns.most_common()),
            "unresolved_block_roles": dict(sndt_unresolved_block_roles.most_common()),
            "control_window_block_kinds": dict(sndt_control_window_block_kinds.most_common()),
            "control_window_block_roles": dict(sndt_control_window_block_roles.most_common()),
            "control_window_block_layouts": dict(sndt_control_window_block_layouts.most_common()),
            "unresolved_block_role_slots": {
                role: dict(sorted(counter.items()))
                for role, counter in sorted(sndt_unresolved_block_role_slots.items())
            },
        },
        "rcnt": {
            "sdri_total": sdri_total,
            "sdri_with_rcnt": sdri_with_rcnt,
            "sdri_fully_resolved": sdri_fully_resolved,
            "target_types": dict(rcnt_target_types.most_common()),
            "entry_histogram": dict(sorted(rcnt_entry_histogram.items())),
            "weight_sum_histogram": dict(sorted(rcnt_weight_sum_histogram.items())),
        },
        "largest_sound_cues": largest_sound_cues[:20],
        "largest_music_cues": largest_music_cues[:20],
    }


def build_markdown(summary: dict[str, object]) -> str:
    lines = [
        "# ICB Semantic Summary",
        "",
        f"- Total cues: {summary['total_cues']}",
        f"- Classified as sound: {summary['cue_kinds'].get('sound', 0)}",
        f"- Classified as music: {summary['cue_kinds'].get('music', 0)}",
        f"- Classified as unknown: {summary['cue_kinds'].get('unknown', 0)}",
        "",
        "## List type totals",
        "",
    ]

    for list_type, count in summary["list_types"].items():
        lines.append(f"- {list_type}: {count}")

    lines.extend(["", "## Top signal groups", ""])
    for group, count in list(summary["signal_groups"].items())[:15]:
        lines.append(f"- {group}: {count}")

    lines.extend(["", "## Sound entry histogram", ""])
    for count, cues in summary["sound_entry_histogram"].items():
        lines.append(f"- {count} entries: {cues} cues")

    lines.extend(["", "## Ctdx Directory", ""])
    lines.append("- Ctdx is now parsed as one or more ISACT directory pages.")
    lines.append("- Layout counts:")
    for layout, cues in summary["ctdx"]["layout_counts"].items():
        lines.append(f"- {layout}: {cues} cues")
    lines.append("- Page count histogram:")
    for count, cues in summary["ctdx"]["page_count_histogram"].items():
        lines.append(f"- {count} pages: {cues} cues")
    lines.append("- Record count histogram:")
    for count, cues in summary["ctdx"]["record_count_histogram"].items():
        lines.append(f"- {count} records: {cues} cues")
    lines.append("- Named record count histogram:")
    for count, cues in summary["ctdx"]["named_record_count_histogram"].items():
        lines.append(f"- {count} named records: {cues} cues")
    lines.append("- Record tags:")
    for tag, count in summary["ctdx"]["record_tag_counts"].items():
        lines.append(f"- {tag}: {count}")
    lines.append("- Name offsets:")
    for offset, count in summary["ctdx"]["name_offset_counts"].items():
        lines.append(f"- +{offset}: {count}")
    lines.append("- Footer tags:")
    for tag, count in summary["ctdx"]["footer_tag_counts"].items():
        lines.append(f"- {tag}: {count}")

    lines.extend(["", "## Seai Entry Action Records", ""])
    lines.append("- Seai is now split when it matches a counted fixed-record layout.")
    lines.append("- Record count histogram:")
    for count, cues in summary["seai"]["record_count_histogram"].items():
        lines.append(f"- {count} records: {cues} cues")
    lines.append("- Named record count histogram:")
    for count, cues in summary["seai"]["named_record_count_histogram"].items():
        lines.append(f"- {count} named records: {cues} cues")
    lines.append("- Record sizes:")
    for size, cues in summary["seai"]["record_size_histogram"].items():
        lines.append(f"- {size} bytes: {cues} cues")
    lines.append("- Parse errors:")
    if summary["seai"]["parse_errors"]:
        for error, cues in summary["seai"]["parse_errors"].items():
            lines.append(f"- {error}: {cues} cues")
    else:
        lines.append("- none")
    lines.append("- Parse cautions:")
    if summary["seai"]["parse_warnings"]:
        for warning, cues in summary["seai"]["parse_warnings"].items():
            lines.append(f"- {warning}: {cues} cues")
    else:
        lines.append("- none")
    lines.append("- Top record names:")
    for name, count in summary["seai"]["top_record_names"].items():
        lines.append(f"- {name}: {count}")

    lines.extend(["", "## Sndt Resolution", ""])
    lines.append(f"- Sound entries with sndt preview: {summary['sndt']['total']}")
    lines.append(f"- Resolved primary sndt target: {summary['sndt']['resolved']}")
    lines.append(f"- Unresolved primary sndt target: {summary['sndt']['unresolved']}")
    lines.append(f"- Total sndt records: {summary['sndt']['record_total']}")
    lines.append(f"- Resolved sndt records: {summary['sndt']['record_resolved']}")
    lines.append(f"- Unresolved sndt records: {summary['sndt']['record_unresolved']}")
    lines.append(f"- Decoded sndt control-window records: {summary['sndt']['record_control_window']}")
    lines.append(f"- Remaining unresolved sndt records after control-window decode: {summary['sndt']['record_unresolved_after_control_window']}")
    lines.append("- Resolution modes:")
    for mode, count in summary["sndt"]["resolution_modes"].items():
        lines.append(f"- {mode}: {count}")
    lines.append("- Record resolution modes:")
    for mode, count in summary["sndt"]["record_resolution_modes"].items():
        lines.append(f"- {mode}: {count}")
    lines.append("- Sndt record count histogram:")
    for count, cues in summary["sndt"]["record_count_histogram"].items():
        lines.append(f"- {count} sndt records: {cues} sound entries")
    lines.append("- Unresolved sndt record patterns:")
    for pattern, count in summary["sndt"]["unresolved_record_patterns"].items():
        lines.append(f"- {pattern}: {count}")
    lines.append("- Decoded sndt control-window record roles:")
    for role, count in summary["sndt"]["control_window_record_roles"].items():
        lines.append(f"- {role}: {count}")
    lines.append("- Decoded sndt control-window kinds:")
    for kind, count in summary["sndt"]["control_window_record_kinds"].items():
        lines.append(f"- {kind}: {count}")
    lines.append("- Decoded sndt control-window layouts:")
    for layout, count in summary["sndt"]["control_window_record_layouts"].items():
        lines.append(f"- {layout}: {count}")
    lines.append("- Sndt record block histogram:")
    for count, cues in summary["sndt"]["record_block_histogram"].items():
        lines.append(f"- {count} sndt record blocks: {cues} sound entries")
    lines.append(f"- Unresolved sndt record blocks: {summary['sndt']['unresolved_block_total']}")
    lines.append(f"- Decoded sndt control-window blocks: {summary['sndt']['control_window_block_total']}")
    lines.append(f"- Remaining unresolved sndt blocks after control-window decode: {summary['sndt']['unresolved_block_after_control_window']}")
    lines.append("- Unresolved sndt block patterns:")
    for pattern, count in summary["sndt"]["unresolved_block_patterns"].items():
        lines.append(f"- {pattern}: {count}")
    lines.append("- Unresolved sndt block inferred roles:")
    for role, count in summary["sndt"]["unresolved_block_roles"].items():
        lines.append(f"- {role}: {count}")
    lines.append("- Decoded sndt control-window block roles:")
    for role, count in summary["sndt"]["control_window_block_roles"].items():
        lines.append(f"- {role}: {count}")
    lines.append("- Decoded sndt control-window block kinds:")
    for kind, count in summary["sndt"]["control_window_block_kinds"].items():
        lines.append(f"- {kind}: {count}")
    lines.append("- Decoded sndt control-window block layouts:")
    for layout, count in summary["sndt"]["control_window_block_layouts"].items():
        lines.append(f"- {layout}: {count}")
    lines.append("- Unresolved sndt block role slot histograms:")
    for role, histogram in summary["sndt"]["unresolved_block_role_slots"].items():
        slots = ", ".join(f"{slot}={count}" for slot, count in histogram.items())
        lines.append(f"- {role}: {slots}")
    lines.append("- Unresolved signal groups:")
    for group, count in summary["sndt"]["unresolved_signal_groups"].items():
        lines.append(f"- {group}: {count}")

    lines.extend(["", "## Rcnt Routing", ""])
    lines.append(f"- SDRI lists: {summary['rcnt']['sdri_total']}")
    lines.append(f"- SDRI lists with rcnt entries: {summary['rcnt']['sdri_with_rcnt']}")
    lines.append(f"- SDRI lists with fully resolved targets: {summary['rcnt']['sdri_fully_resolved']}")
    lines.append("- Target types:")
    for target_type, count in summary["rcnt"]["target_types"].items():
        lines.append(f"- {target_type}: {count}")
    lines.append("- Entry count histogram:")
    for count, cues in summary["rcnt"]["entry_histogram"].items():
        lines.append(f"- {count} rcnt entries: {cues} sdri lists")
    lines.append("- Weight sum histogram:")
    for total, cues in summary["rcnt"]["weight_sum_histogram"].items():
        lines.append(f"- {total}: {cues} sdri lists")

    lines.extend(["", "## Largest sound cues", ""])
    for item in summary["largest_sound_cues"]:
        lines.append(f"- {Path(item['file']).name}: {item['count']} sound entries, {item['ctdx_fragments']} ctdx fragments")

    lines.extend(["", "## Largest music cues", ""])
    for item in summary["largest_music_cues"]:
        lines.append(f"- {Path(item['file']).name}: {item['count']} list summaries, {item['ctdx_fragments']} ctdx fragments")

    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("output/audio/catalog_icb_casefix/cues.json"),
        help="Path to the enriched ICB catalog summary",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output/audio/icb_semantics_summary"),
        help="Output directory for aggregated JSON and Markdown reports",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog_path = args.catalog.expanduser().resolve()
    out_root = args.out.expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    cues = load_catalog(catalog_path)
    summary = build_summary(cues)
    json_path = out_root / "summary.json"
    md_path = out_root / "summary.md"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md_path.write_text(build_markdown(summary), encoding="utf-8")

    print(f"Summarized {summary['total_cues']} cues")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())