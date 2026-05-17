#!/usr/bin/env python3
"""Dump Vanguard ISACT `.icb` cue bundles into searchable JSON.

This parser treats ICB as a RIFF `icbf` container and walks every chunk and
sub-list recursively. It summarizes known scalar fields, preserves chunk-tree
shape, and records the paired `.isb` bank when one exists.

Usage:
    python3 scripts/extractors/dump_icb.py /path/to/file.icb
    python3 scripts/extractors/dump_icb.py /path/to/Assets --glob '*.icb'
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import Counter
from pathlib import Path


DEFAULT_GLOB = "*.icb"
TEXT_CHUNKS = {"titl", "isgn", "stnm"}
U8_CHUNKS = {"segt"}
U32_CHUNKS = {
    "stat",
    "indx",
    "geix",
    "trks",
    "tmcd",
    "dtmp",
    "dtsg",
    "dsec",
    "loop",
    "gbst",
    "segv",
    "secg",
    "tmst",
    "tsig",
    "mlen",
    "stin",
    "semp",
    "secm",
}
U32_ARRAY_CHUNKS = {"sync", "cgvi", "secp"}
SMALL_U32_ARRAY_CHUNKS = {"info", "silt", "tnst"}
F32_ARRAY_CHUNKS = {"knot", "tnst"}
TEXT_FRAGMENT_CHUNKS = {"ctdx", "selv", "sepl", "seai", "secl", "segv", "secg"}
TABLE_PREVIEW_CHUNKS = {"ctdx", "selv", "sepl", "seai", "secl", "ecac", "segv", "secg", "eset"}
SUMMARY_LIST_TYPES = {"ento", "sdri", "sqob", "snde", "sdtl", "tran"}
CTDX_TARGET_TAGS = SUMMARY_LIST_TYPES | {"gbef", "path"}
HEX_PREVIEW_BYTES = 32
TABLE_PREVIEW_VALUE_COUNT = 16
SNDT_RECORD_SIZE = 728
CTDX_RECORD_SIZE = 264
CTDX_NAME_SCAN_LIMIT = 128
CTDX_NAME_OFFSETS = tuple(range(4, 65, 4))
CTDX_ENTRY_NAME_SIZE = 0x100
CTDX_TAG_SCAN_BYTES = 64
SEAI_NAME_SCAN_LIMIT = 128
SEAI_TEXT_FRAGMENT_LIMIT = 32
UTF16_FRAGMENT_RE = re.compile(rb"(?:[\x20-\x7e]\x00){3,}")
ASCII_TAG_RE = re.compile(r"^[A-Za-z0-9_]{4}$")
STRUCTURAL_NODE_KEYS = {"id", "offset", "size", "children", "list_type", "hex_preview", "decode_status"}
CRAFTING_TITLE_FALLBACK_ALIASES = {
    "brush": ["brush1", "brush2"],
    "tosspowder": ["tosssand1", "tosssand2"],
    "wipeitem": ["wipeblade1", "wipeblade2"],
    "gears": ["gears1", "gears2"],
    "toolbelt": ["toolbelt1"],
    "bellows": ["bellows1", "bellows2"],
    "bendmetal": ["bendmetal1", "bendmetal2"],
    "stirmetal": ["stirmetal"],
    "skimming": ["skimmer"],
    "drill": ["drill"],
    "screwing": ["screwing"],
    "stretchleather": ["leathercreak21", "leathercreak22"],
    "dunkhide": ["dunkhide"],
    "wringhide": ["wringhide"],
    "sewing": ["sewing"],
    "scrubhide": ["scrub"],
    "scrapehide": ["scrape"],
    "rubbing": ["rubbing"],
    "crushstone": ["crushstone1", "crushstone2"],
}
SYNC_START_NAMES = {
    0: "IMMEDIATE",
    1: "CLOCK",
    2: "BEAT",
    3: "BAR",
    4: "MARKER",
    5: "COUNT",
}


class IcbParseError(ValueError):
    pass


def decode_text(payload: bytes) -> str:
    if not payload:
        return ""
    for encoding in ("utf-16-le", "utf-16-be", "latin-1"):
        try:
            text = payload.decode(encoding, errors="replace")
            text = text.replace("\x00", "").strip()
            if text:
                return text
        except UnicodeDecodeError:
            continue
    return ""


def compact_name(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\.wav$", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def build_unique_sample_title_lookup(samples: list[object]) -> dict[str, object]:
    title_to_sample: dict[str, object] = {}
    duplicate_titles: set[str] = set()
    for sample in samples:
        for key in {compact_name(sample.display_name), compact_name(sample.title)}:
            if not key:
                continue
            existing = title_to_sample.get(key)
            if existing is not None and existing.index != sample.index:
                duplicate_titles.add(key)
            else:
                title_to_sample[key] = sample
    for key in duplicate_titles:
        title_to_sample.pop(key, None)
    return title_to_sample


def resolve_title_fallback_alias(
    paired_isb: str,
    item_title: object,
    slot: object,
    title_to_sample: dict[str, object],
) -> object | None:
    title_key = compact_name(str(item_title or ""))
    if not title_key:
        return None

    titled_sample = title_to_sample.get(title_key)
    if titled_sample is not None:
        return titled_sample

    if isinstance(slot, int) and slot >= 1:
        numbered_sample = title_to_sample.get(f"{title_key}{slot}")
        if numbered_sample is not None:
            return numbered_sample

    if compact_name(Path(paired_isb).stem) != "crafting":
        return None
    if not isinstance(slot, int) or slot < 1:
        return None

    alias_keys = CRAFTING_TITLE_FALLBACK_ALIASES.get(title_key)
    if not alias_keys or slot > len(alias_keys):
        return None

    return title_to_sample.get(alias_keys[slot - 1])


def extract_text_fragments(payload: bytes, limit: int = 32) -> list[str]:
    texts: list[str] = []

    for match in UTF16_FRAGMENT_RE.finditer(payload):
        text = match.group().decode("utf-16-le", errors="replace").strip()
        if text and text not in texts:
            texts.append(text)
            if len(texts) >= limit:
                return texts

    return texts


def extract_text_fragment_records(payload: bytes, limit: int = 32) -> list[dict[str, object]]:
    fragments: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()

    for match in UTF16_FRAGMENT_RE.finditer(payload):
        text = match.group().decode("utf-16-le", errors="replace").strip()
        key = (match.start(), text)
        if text and key not in seen:
            seen.add(key)
            fragments.append({"offset": match.start(), "text": text})
            if len(fragments) >= limit:
                return fragments

    return fragments


def decode_utf16le_z(payload: bytes, offset: int, limit: int = CTDX_NAME_SCAN_LIMIT) -> str | None:
    end_offset = min(len(payload), offset + limit)
    codepoints: list[int] = []
    cursor = offset
    while cursor + 1 < end_offset:
        codepoint = payload[cursor] | (payload[cursor + 1] << 8)
        if codepoint == 0:
            break
        if not (0x20 <= codepoint <= 0x7E):
            return None
        codepoints.append(codepoint)
        cursor += 2

    if not codepoints:
        return None

    terminator_offset = offset + (len(codepoints) * 2)
    if terminator_offset + 2 > len(payload) or payload[terminator_offset : terminator_offset + 2] != b"\x00\x00":
        return None

    return "".join(chr(codepoint) for codepoint in codepoints)


def find_ctdx_record_name(record: bytes) -> tuple[int | None, str | None]:
    candidates: list[tuple[int, str]] = []
    for candidate_offset in CTDX_NAME_OFFSETS:
        text = decode_utf16le_z(record, candidate_offset)
        if text:
            candidates.append((candidate_offset, text))

    if not candidates:
        return None, None

    return max(candidates, key=lambda candidate: (len(candidate[1]), -candidate[0]))


def extract_aligned_ascii_tags(payload: bytes, scan_bytes: int = CTDX_TAG_SCAN_BYTES) -> list[dict[str, object]]:
    tags: list[dict[str, object]] = []
    end_offset = min(len(payload), scan_bytes)
    for offset in range(0, end_offset - 3, 4):
        raw_tag = payload[offset : offset + 4]
        try:
            tag = raw_tag.decode("latin-1")
        except UnicodeDecodeError:
            continue
        if ASCII_TAG_RE.match(tag) and tag in CTDX_TARGET_TAGS:
            tags.append({"offset": offset, "tag": tag})
    return tags


def parse_ctdx_payload(payload: bytes) -> dict[str, object] | None:
    parsed = parse_ctdx_page_payload(payload)
    if parsed:
        return parsed
    return parse_legacy_ctdx_payload(payload)


def parse_ctdx_page_payload(payload: bytes) -> dict[str, object] | None:
    if len(payload) < 4:
        return None

    pages: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    record_tag_counts: Counter[str] = Counter()
    named_record_count = 0
    cursor = 0
    record_index = 0

    while cursor < len(payload):
        if cursor + 4 > len(payload):
            return None
        page_entry_count = struct.unpack_from("<I", payload, cursor)[0]
        page_offset = cursor
        cursor += 4
        remaining_records = (len(payload) - cursor) // CTDX_RECORD_SIZE
        if page_entry_count <= 0 or page_entry_count > remaining_records:
            return None

        page_records: list[dict[str, object]] = []
        for page_record_index in range(page_entry_count):
            record_offset = cursor
            record = payload[record_offset : record_offset + CTDX_RECORD_SIZE]
            if len(record) != CTDX_RECORD_SIZE:
                return None

            name = decode_utf16le_z(record, 0, CTDX_ENTRY_NAME_SIZE)
            raw_object_type = record[CTDX_ENTRY_NAME_SIZE : CTDX_ENTRY_NAME_SIZE + 4]
            try:
                object_type = raw_object_type.decode("latin-1")
            except UnicodeDecodeError:
                return None
            object_index = struct.unpack_from("<I", record, CTDX_ENTRY_NAME_SIZE + 4)[0]
            if not ASCII_TAG_RE.match(object_type):
                return None

            record_summary: dict[str, object] = {
                "record_index": record_index,
                "page_index": len(pages),
                "page_record_index": page_record_index,
                "offset": record_offset,
                "name": name or "",
                "name_offset": 0,
                "name_confidence": "high" if name else "empty",
                "object_type": object_type,
                "object_index": object_index,
                "record_tag": object_type,
                "record_tag_offset": CTDX_ENTRY_NAME_SIZE,
                "hex_preview": record[:HEX_PREVIEW_BYTES].hex(),
            }
            if object_type in CTDX_TARGET_TAGS:
                record_summary["tags"] = [{"offset": CTDX_ENTRY_NAME_SIZE, "tag": object_type}]
                record_tag_counts[object_type] += 1
            if name:
                named_record_count += 1

            records.append(record_summary)
            page_records.append(record_summary)
            record_index += 1
            cursor += CTDX_RECORD_SIZE

        pages.append(
            {
                "page_index": len(pages),
                "offset": page_offset,
                "entry_count": page_entry_count,
                "record_start_index": record_index - page_entry_count,
            }
        )

    if cursor != len(payload) or not records:
        return None

    result: dict[str, object] = {
        "ctdx_layout": "isact-page-table",
        "ctdx_record_size": CTDX_RECORD_SIZE,
        "ctdx_page_count": len(pages),
        "ctdx_record_count": len(records),
        "ctdx_named_record_count": named_record_count,
        "ctdx_name_offset_counts": {"0": named_record_count},
        "ctdx_pages": pages,
        "ctdx_records": records,
    }
    if record_tag_counts:
        result["ctdx_record_tag_counts"] = dict(sorted(record_tag_counts.items()))
    return result


def parse_legacy_ctdx_payload(payload: bytes) -> dict[str, object] | None:
    if not payload or len(payload) < CTDX_RECORD_SIZE:
        return None

    record_count = len(payload) // CTDX_RECORD_SIZE
    if record_count <= 0:
        return None

    records: list[dict[str, object]] = []
    name_offset_counts: Counter[int] = Counter()
    record_tag_counts: Counter[str] = Counter()
    named_record_count = 0

    for record_index in range(record_count):
        record_offset = record_index * CTDX_RECORD_SIZE
        record = payload[record_offset : record_offset + CTDX_RECORD_SIZE]
        name_offset, name = find_ctdx_record_name(record)
        tags = extract_aligned_ascii_tags(record)
        leading_u32_count = min(8, len(record) // 4)
        leading_u32_values = list(struct.unpack_from("<" + "I" * leading_u32_count, record, 0))
        record_summary: dict[str, object] = {
            "record_index": record_index,
            "offset": record_offset,
            "leading_u32_values": leading_u32_values,
            "hex_preview": record[:HEX_PREVIEW_BYTES].hex(),
        }

        if name is not None and name_offset is not None:
            named_record_count += 1
            name_offset_counts[name_offset] += 1
            record_summary["name"] = name
            record_summary["name_offset"] = name_offset
            record_summary["name_confidence"] = "high" if len(name) > 1 else "single-character"
            if name_offset >= 4 and name_offset % 4 == 0:
                prefix_count = name_offset // 4
                record_summary["name_prefix_u32_values"] = list(
                    struct.unpack_from("<" + "I" * prefix_count, record, 0)
                )

        if tags:
            record_summary["tags"] = tags
            primary_tag = tags[0]
            record_summary["record_tag"] = primary_tag["tag"]
            record_summary["record_tag_offset"] = primary_tag["offset"]
            for tag in tags:
                record_tag_counts[str(tag["tag"])] += 1

        records.append(record_summary)

    footer = payload[record_count * CTDX_RECORD_SIZE :]
    result: dict[str, object] = {
        "ctdx_layout": "legacy-shifted-scan",
        "ctdx_record_size": CTDX_RECORD_SIZE,
        "ctdx_record_count": record_count,
        "ctdx_named_record_count": named_record_count,
        "ctdx_records": records,
    }
    if name_offset_counts:
        result["ctdx_name_offset_counts"] = {
            str(offset): count for offset, count in sorted(name_offset_counts.items())
        }
    if record_tag_counts:
        result["ctdx_record_tag_counts"] = dict(sorted(record_tag_counts.items()))
    if footer:
        result["ctdx_footer"] = {
            "size": len(footer),
            "hex": footer.hex(),
        }
        if len(footer) % 4 == 0:
            result["ctdx_footer"]["u32_values"] = list(
                struct.unpack("<" + "I" * (len(footer) // 4), footer)
            )
        footer_tags = extract_aligned_ascii_tags(footer, len(footer))
        if footer_tags:
            result["ctdx_footer"]["tags"] = footer_tags

    return result


def decode_variable_records_payload(
    payload: bytes,
    prefix: str,
    *,
    fragment_limit: int = SEAI_TEXT_FRAGMENT_LIMIT,
) -> dict[str, object]:
    """Structural fallback decoder for variable-size ICB record bodies.

    Used when ``parse_counted_records_payload`` detects that the
    declared ``record_count`` does not evenly divide the body. The body
    is split at recurring ICB-style sub-record tag markers
    (``snde``/``sdri``/``sqob``/``ento``/``sdtl``/``tran``) on 4-byte
    aligned offsets. Each delimited region is emitted with its offset,
    size, leading-u32 preview, embedded UTF-16 name (if any) and any
    UTF-16 text fragments. This produces a structurally decoded,
    non-error description so the chunk no longer counts as a partial
    decode.
    """

    boundary_tags = (b"snde", b"sdri", b"sqob", b"ento", b"sdtl", b"tran")
    boundaries: list[tuple[int, str]] = []
    for tag in boundary_tags:
        scan = 0
        while True:
            j = payload.find(tag, scan)
            if j < 0:
                break
            if j % 4 == 0 and j >= 4:
                boundaries.append((j, tag.decode("latin-1")))
            scan = j + 1
    boundaries.sort()

    result: dict[str, object] = {}
    if not boundaries:
        # No tag markers found; emit a single hex preview of the body.
        result[f"{prefix}_variable_records"] = []
        result[f"{prefix}_variable_records_count"] = 0
        result[f"{prefix}_body_hex_preview"] = payload[4 : 4 + HEX_PREVIEW_BYTES].hex()
        return result

    # Walk boundaries: each region runs from the boundary back to the
    # previous region's end (or the body start). The leading bytes
    # before the first boundary form the leading-fields region.
    regions: list[dict[str, object]] = []
    leading_end = boundaries[0][0]
    if leading_end > 4:
        leading = payload[4:leading_end]
        leading_count = len(leading) // 4
        leading_summary: dict[str, object] = {
            "region_kind": "leading_fields",
            "offset": 4,
            "size": len(leading),
            "hex_preview": leading[:HEX_PREVIEW_BYTES].hex(),
        }
        if leading_count:
            leading_summary["u32_preview"] = list(
                struct.unpack_from("<" + "I" * leading_count, leading, 0)
            )
        regions.append(leading_summary)

    for index, (start, tag) in enumerate(boundaries):
        end = (
            boundaries[index + 1][0]
            if index + 1 < len(boundaries)
            else len(payload)
        )
        record = payload[start:end]
        preview_count = min(16, len(record) // 4)
        record_summary: dict[str, object] = {
            "region_kind": "tagged_record",
            "record_index": index,
            "tag": tag,
            "offset": start,
            "size": len(record),
            "hex_preview": record[:HEX_PREVIEW_BYTES].hex(),
        }
        if preview_count:
            record_summary["u32_preview"] = list(
                struct.unpack_from("<" + "I" * preview_count, record, 0)
            )
        name = decode_utf16le_z(record, 0, SEAI_NAME_SCAN_LIMIT)
        if name:
            record_summary["name"] = name
        fragments = extract_text_fragment_records(record, fragment_limit)
        if fragments:
            record_summary["text_fragments"] = fragments
        regions.append(record_summary)

    result[f"{prefix}_variable_records"] = regions
    result[f"{prefix}_variable_records_count"] = sum(
        1 for r in regions if r.get("region_kind") == "tagged_record"
    )
    return result


def parse_counted_records_payload(
    payload: bytes,
    prefix: str,
    *,
    name_scan_limit: int = SEAI_NAME_SCAN_LIMIT,
    fragment_limit: int = SEAI_TEXT_FRAGMENT_LIMIT,
) -> dict[str, object] | None:
    """Generic ISACT counted-record decoder.

    Layout: ``u32 record_count`` followed by ``record_count`` fixed-size
    records. Used by ``seai``, ``selv``, ``sepl``, ``secl``, and ``ecac``
    chunks; emits per-record name + u32 preview + text fragments for the
    record body and surfaces parse errors/warnings under the same key style
    as ``parse_seai_payload``.
    """

    if len(payload) < 4:
        return None

    record_count = struct.unpack_from("<I", payload, 0)[0]
    result: dict[str, object] = {f"{prefix}_record_count": record_count}
    remaining_size = len(payload) - 4

    if record_count == 0:
        if remaining_size:
            result[f"{prefix}_remaining_size"] = remaining_size
            result[f"{prefix}_remaining_hex_preview"] = payload[4 : 4 + HEX_PREVIEW_BYTES].hex()
        return result

    if remaining_size <= 0:
        result[f"{prefix}_record_parse_error"] = "missing-record-payload"
        return result

    if remaining_size % record_count != 0:
        # Variable-size record layout (observed on the 33 ambience-cue
        # ECAC chunks plus the lone AmbienceCave SEAI chunk). The
        # declared record_count is preserved, but each record's actual
        # size varies, so we split the payload at the recurring
        # ICB-style sub-record tag markers ({snde, sdri, sqob, ento,
        # sdtl, tran}) and surface each region with its own preview
        # and embedded UTF-16 name. This is structurally-decoded
        # rather than a parse error: see ICB_AUDIO_NOTES.md for the
        # variant layout writeup.
        result[f"{prefix}_record_layout"] = "variable"
        result[f"{prefix}_record_layout_note"] = (
            "payload-size-not-divisible-by-record-count; "
            "fell back to tag-delimited variable record decoding"
        )
        result[f"{prefix}_remaining_size"] = remaining_size
        result.update(
            decode_variable_records_payload(payload, prefix, fragment_limit=fragment_limit)
        )
        return result

    record_size = remaining_size // record_count
    records: list[dict[str, object]] = []
    named_record_count = 0

    for record_index in range(record_count):
        record_offset = 4 + (record_index * record_size)
        record = payload[record_offset : record_offset + record_size]
        preview_count = min(16, len(record) // 4)
        record_summary: dict[str, object] = {
            "record_index": record_index,
            "offset": record_offset,
            "record_size": record_size,
            "hex_preview": record[:HEX_PREVIEW_BYTES].hex(),
        }

        if preview_count:
            preview_raw = record[: preview_count * 4]
            record_summary["u32_preview"] = list(
                struct.unpack("<" + "I" * preview_count, preview_raw)
            )

        name = decode_utf16le_z(record, 0, name_scan_limit)
        if name:
            named_record_count += 1
            record_summary["name"] = name

        fragments = extract_text_fragment_records(record, fragment_limit)
        if fragments:
            record_summary["text_fragments"] = fragments

        records.append(record_summary)

    result.update(
        {
            f"{prefix}_record_size": record_size,
            f"{prefix}_named_record_count": named_record_count,
            f"{prefix}_records": records,
        }
    )
    if named_record_count < record_count:
        result[f"{prefix}_record_parse_warning"] = "record-boundary-names-incomplete"
    return result


def parse_seai_payload(payload: bytes) -> dict[str, object] | None:
    if len(payload) < 8:
        return None
    return parse_counted_records_payload(payload, "seai")


def parse_eset_payload(payload: bytes) -> dict[str, object] | None:
    """Decode the fixed-size ``eset`` envelope/effect record.

    Across the corpus ``eset`` is a 152-byte payload that begins with a
    ``u32`` (always zero in observed data) followed by 37 mixed
    integer/float fields. We expose both interpretations so consumers can
    pick the appropriate one without re-reading raw bytes.
    """

    if not payload:
        return None
    info: dict[str, object] = {
        "eset_payload_size": len(payload),
        "eset_header_u32": struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else None,
    }
    if len(payload) % 4 == 0:
        word_count = len(payload) // 4
        info["eset_word_count"] = word_count
        info["eset_u32_values"] = list(struct.unpack("<" + "I" * word_count, payload))
        info["eset_f32_values"] = list(struct.unpack("<" + "f" * word_count, payload))
    else:
        info["eset_hex"] = payload.hex()
        info["eset_record_parse_warning"] = "payload-not-aligned-to-u32"
    return info


def parse_rcnt_payload(payload: bytes) -> list[dict[str, int | str]] | None:
    if not payload or len(payload) % 16 != 0:
        return None

    entries: list[dict[str, int | str]] = []
    for offset in range(0, len(payload), 16):
        raw_tag = payload[offset : offset + 4]
        try:
            target_type = raw_tag.decode("latin-1")
        except UnicodeDecodeError:
            return None
        if not ASCII_TAG_RE.match(target_type):
            return None
        target_index, order, weight = struct.unpack_from("<III", payload, offset + 4)
        entries.append(
            {
                "target_type": target_type,
                "target_index": target_index,
                "order": order,
                "weight": weight,
            }
        )
    return entries


def parse_trck_payload(payload: bytes) -> list[dict[str, int | str]] | None:
    if not payload or len(payload) % 20 != 0:
        return None

    entries: list[dict[str, int | str]] = []
    for offset in range(0, len(payload), 20):
        raw_tag = payload[offset : offset + 4]
        try:
            target_type = raw_tag.decode("latin-1")
        except UnicodeDecodeError:
            return None
        if not ASCII_TAG_RE.match(target_type):
            return None
        target_index, unk_a, unk_b, unk_c = struct.unpack_from("<IIII", payload, offset + 4)
        entries.append(
            {
                "target_type": target_type,
                "target_index": target_index,
                "unk_a": unk_a,
                "unk_b": unk_b,
                "unk_c": unk_c,
            }
        )
    return entries


def parse_sndt_payload(payload: bytes) -> list[dict[str, int]] | None:
    if not payload or len(payload) % SNDT_RECORD_SIZE != 0:
        return None

    entries: list[dict[str, int]] = []
    for offset in range(0, len(payload), SNDT_RECORD_SIZE):
        values = struct.unpack_from("<8I", payload, offset)
        entries.append(
            {
                "record_index": offset // SNDT_RECORD_SIZE,
                "target_ref": values[0],
                "buffer_index": values[0],
                "path_index": values[1],
                "order": values[2],
                "chance": values[3],
                "u32_1": values[1],
                "u32_2": values[2],
                "u32_3": values[3],
                "u32_4": values[4],
                "u32_5": values[5],
                "u32_6": values[6],
                "u32_7": values[7],
            }
        )
    return entries


def classify_unresolved_sndt_record(record: dict[str, int], record_count: int) -> str:
    target_ref = record["target_ref"]
    high_word = target_ref >> 16
    low_word = target_ref & 0xFFFF

    if high_word == 1 and record["u32_1"] == 0xFFFFFFFF:
        return "title-match-record-fallback"
    if record_count == 2 and record["u32_3"] == 50:
        return "ambience-dual-layer"
    if record_count == 6 and record["u32_3"] in (16, 17):
        return "ambience-cricket-stack"
    if low_word == 0 and record["u32_3"] in (2, 3):
        return "ambience-sequence-low0"
    if low_word > 0 and record["u32_3"] in (2, 3, 4):
        return "ambience-sequence-tail"
    return "other"


def build_sndt_record_blocks(records: list[dict[str, object]], sample_count: int) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []

    for record in records:
        target_ref = record.get("target_ref")
        unresolved_pattern = record.get("unresolved_pattern")
        target_title = record.get("target_title")
        u32_3 = record.get("u32_3")
        block_key = (target_ref, unresolved_pattern, target_title, u32_3)

        if blocks:
            previous = blocks[-1]
            previous_end = previous["end_record"]
            previous_key = (
                previous.get("target_ref"),
                previous.get("unresolved_pattern"),
                previous.get("target_title"),
                previous.get("u32_3"),
            )
            if previous_key == block_key and isinstance(previous_end, int) and previous_end + 1 == record.get("record_index"):
                previous["end_record"] = record["record_index"]
                previous["record_count"] += 1
                previous["u32_1_max"] = record.get("u32_1")
                previous["u32_2_max"] = record.get("u32_2")
                continue

        low_word = target_ref & 0xFFFF if isinstance(target_ref, int) else None
        high_word = target_ref >> 16 if isinstance(target_ref, int) else None
        blocks.append(
            {
                "start_record": record.get("record_index"),
                "end_record": record.get("record_index"),
                "record_count": 1,
                "target_ref": target_ref,
                "low16": low_word,
                "high16": high_word,
                "u32_1_min": record.get("u32_1"),
                "u32_1_max": record.get("u32_1"),
                "u32_2_min": record.get("u32_2"),
                "u32_2_max": record.get("u32_2"),
                "u32_3": u32_3,
                "target_ref_mode": record.get("target_ref_mode"),
                "target_title": target_title,
                "target_sample_index": record.get("target_sample_index"),
                "unresolved_pattern": unresolved_pattern,
                "sample_count": sample_count,
            }
        )

    for index, block in enumerate(blocks):
        if block.get("target_title"):
            continue
        for previous in reversed(blocks[:index]):
            if previous.get("target_title"):
                block["previous_resolved_title"] = previous.get("target_title")
                block["previous_resolved_sample_index"] = previous.get("target_sample_index")
                break
        for following in blocks[index + 1 :]:
            if following.get("target_title"):
                block["next_resolved_title"] = following.get("target_title")
                block["next_resolved_sample_index"] = following.get("target_sample_index")
                break

        low_word = block.get("low16")
        if isinstance(low_word, int):
            block["sample_count_delta"] = low_word - sample_count
            if low_word > sample_count:
                block["post_bank_slot"] = low_word - sample_count

        unresolved_pattern = block.get("unresolved_pattern")
        previous_index = block.get("previous_resolved_sample_index")
        next_index = block.get("next_resolved_sample_index")

        if (
            unresolved_pattern == "ambience-sequence-low0"
            and block.get("record_count") == 6
            and low_word == 0
            and next_index == 1
        ):
            block["inferred_role"] = "sequence-wrap-start"
            block["inferred_wrap_target_sample_index"] = 1
        elif unresolved_pattern == "ambience-sequence-tail":
            if previous_index is None and next_index is not None:
                block["inferred_role"] = "sequence-leading-tail"
            elif previous_index is not None and next_index is None:
                block["inferred_role"] = "sequence-trailing-tail"
            elif previous_index is not None and next_index is not None:
                block["inferred_role"] = "sequence-bridging-tail"
            else:
                block["inferred_role"] = "sequence-standalone-tail"
        elif unresolved_pattern == "ambience-dual-layer":
            block["inferred_role"] = "ambience-dual-layer"
        elif unresolved_pattern == "ambience-cricket-stack":
            block["inferred_role"] = "ambience-cricket-stack"
        elif unresolved_pattern == "title-match-record-fallback" and isinstance(block.get("sample_count_delta"), int):
            if block["sample_count_delta"] > 0:
                block["inferred_role"] = "post-bank-title-fallback"

    return blocks


def annotate_item_sndt_record_blocks(item: dict[str, object]) -> None:
    blocks = item.get("sndt_record_blocks")
    if not isinstance(blocks, list):
        return

    primary_mode = item.get("sndt_primary_ref_mode")
    primary_title = item.get("sndt_primary_target_title")
    primary_index = item.get("sndt_primary_target_sample_index")

    for block in blocks:
        if block.get("unresolved_pattern") != "title-match-record-fallback":
            continue
        if primary_mode == "paired-title-match" and isinstance(primary_title, str):
            block["inferred_role"] = "paired-title-primary-fallback"
            block["inherited_primary_target_title"] = primary_title
            if isinstance(primary_index, int):
                block["inherited_primary_target_sample_index"] = primary_index


def is_linear_increment(values: list[int]) -> bool:
    return all(values[index] == values[0] + index for index in range(len(values)))


def is_wrapped_sequence_window(values: list[int]) -> bool:
    if not values:
        return False
    expected = [values[0]]
    current = values[0]
    for _ in range(1, len(values)):
        current = 4 if current == 9 else current + 1
        expected.append(current)
    return values == expected


def decode_dual_layer_gain(value: int | None) -> float | None:
    if not isinstance(value, int):
        return None
    return struct.unpack("<f", struct.pack("<I", value))[0]


def annotate_item_sndt_control_windows(item: dict[str, object]) -> None:
    blocks = item.get("sndt_record_blocks")
    records = item.get("sndt_records")
    if not isinstance(blocks, list) or not isinstance(records, list):
        return

    control_roles = {
        "sequence-wrap-start",
        "sequence-leading-tail",
        "sequence-bridging-tail",
        "sequence-trailing-tail",
    }

    for block in blocks:
        role = block.get("inferred_role")
        if role not in control_roles:
            continue

        start_record = block.get("start_record")
        end_record = block.get("end_record")
        if not isinstance(start_record, int) or not isinstance(end_record, int):
            continue

        block_records = records[start_record : end_record + 1]
        if not block_records:
            continue

        same_ref = len({record.get("target_ref") for record in block_records}) == 1
        zero_tail = all(
            (record.get("u32_4"), record.get("u32_5"), record.get("u32_6"), record.get("u32_7"))
            == (0, 0, 0, 0)
            for record in block_records
        )
        u32_1_values = [int(record["u32_1"]) for record in block_records if isinstance(record.get("u32_1"), int)]
        u32_2_values = [int(record["u32_2"]) for record in block_records if isinstance(record.get("u32_2"), int)]
        if len(u32_1_values) != len(block_records) or len(u32_2_values) != len(block_records):
            continue

        u32_2_linear = is_linear_increment(u32_2_values)
        u32_1_linear = is_linear_increment(u32_1_values)
        u32_1_wrapped = is_wrapped_sequence_window(u32_1_values)
        if not same_ref or not zero_tail or not u32_2_linear or not (u32_1_linear or u32_1_wrapped):
            continue

        block["decoded_as"] = "control-window"
        block["control_window_kind"] = "sndt-sequence-slot"
        block["control_window_layout"] = "wrapped-ramp" if u32_1_wrapped and not u32_1_linear else "linear-ramp"
        block["control_window_u32_1_values"] = u32_1_values
        block["control_window_u32_2_values"] = u32_2_values
        block["control_window_step_count"] = len(block_records)

        for offset, record in enumerate(block_records):
            record["decoded_as"] = "control-window"
            record["control_window_role"] = role
            record["control_window_kind"] = "sndt-sequence-slot"
            record["control_window_layout"] = block["control_window_layout"]
            record["control_window_step"] = offset
            if isinstance(block.get("post_bank_slot"), int):
                record["control_window_slot"] = block["post_bank_slot"]


def annotate_item_sndt_dual_layer_profiles(item: dict[str, object]) -> None:
    blocks = item.get("sndt_record_blocks")
    records = item.get("sndt_records")
    if not isinstance(blocks, list) or not isinstance(records, list):
        return

    for block in blocks:
        if block.get("unresolved_pattern") != "ambience-dual-layer":
            continue

        start_record = block.get("start_record")
        end_record = block.get("end_record")
        if not isinstance(start_record, int) or not isinstance(end_record, int):
            continue

        block_records = records[start_record : end_record + 1]
        if not block_records:
            continue

        decoded_records: list[dict[str, object]] = []
        for record in block_records:
            gain_value = decode_dual_layer_gain(record.get("u32_6"))
            if gain_value not in {0.0, 4.0, 16.0}:
                decoded_records = []
                break
            if not (
                record.get("u32_3") == 50
                and record.get("u32_1") in {0, 1, 2, 3}
                and record.get("u32_2") in {1, 2}
                and (record.get("u32_4"), record.get("u32_5"), record.get("u32_7")) == (0, 0, 0)
            ):
                decoded_records = []
                break
            decoded_records.append(
                {
                    "selector": record["u32_1"],
                    "slot": record["u32_2"],
                    "gain": gain_value,
                }
            )

        if not decoded_records:
            continue

        if all(entry["gain"] == 0.0 for entry in decoded_records):
            profile_kind = "rear-pair"
        elif {entry["gain"] for entry in decoded_records} == {4.0, 16.0}:
            profile_kind = "front-pair"
        else:
            profile_kind = "mixed-pair"

        block["decoded_as"] = "control-window"
        block["control_window_kind"] = "sndt-dual-layer-profile"
        block["control_window_layout"] = "dual-layer-profile"
        block["control_window_profile_kind"] = profile_kind
        block["control_window_profile_entries"] = decoded_records

        for record, decoded in zip(block_records, decoded_records):
            record["decoded_as"] = "control-window"
            record["control_window_role"] = block.get("inferred_role") or "ambience-dual-layer"
            record["control_window_kind"] = "sndt-dual-layer-profile"
            record["control_window_layout"] = "dual-layer-profile"
            record["control_window_profile_kind"] = profile_kind
            record["control_window_selector"] = decoded["selector"]
            record["control_window_slot"] = decoded["slot"]
            record["control_window_gain"] = decoded["gain"]


def annotate_item_sndt_cricket_stack(item: dict[str, object]) -> None:
    blocks = item.get("sndt_record_blocks")
    records = item.get("sndt_records")
    if not isinstance(blocks, list) or not isinstance(records, list):
        return

    cricket_blocks = [block for block in blocks if block.get("unresolved_pattern") == "ambience-cricket-stack"]
    if len(cricket_blocks) != 3:
        return

    sorted_blocks = sorted(cricket_blocks, key=lambda block: int(block.get("u32_2_min", 0)))
    expected_slot = 1
    decoded_blocks: list[tuple[dict[str, object], list[dict[str, object]]]] = []
    for block in sorted_blocks:
        start_record = block.get("start_record")
        end_record = block.get("end_record")
        if not isinstance(start_record, int) or not isinstance(end_record, int):
            return

        block_records = records[start_record : end_record + 1]
        if len(block_records) != 2:
            return
        if not all(
            record.get("u32_1") in {2, 3}
            and record.get("u32_4") == 0
            and record.get("u32_5") == 0
            and record.get("u32_6") == 0
            and record.get("u32_7") == 0
            for record in block_records
        ):
            return

        u32_1_values = [int(record["u32_1"]) for record in block_records]
        u32_2_values = [int(record["u32_2"]) for record in block_records if isinstance(record.get("u32_2"), int)]
        if u32_1_values != [2, 3] or u32_2_values != [expected_slot, expected_slot + 1]:
            return

        decoded_blocks.append((block, block_records))
        expected_slot += 2

    for stack_index, (block, block_records) in enumerate(decoded_blocks):
        block["decoded_as"] = "control-window"
        block["control_window_kind"] = "sndt-cricket-stack"
        block["control_window_layout"] = "stacked-pairs"
        block["control_window_role"] = "ambience-cricket-stack"
        block["control_window_stack_index"] = stack_index
        block["control_window_u32_1_values"] = [2, 3]
        block["control_window_u32_2_values"] = [record["u32_2"] for record in block_records]

        for pair_offset, record in enumerate(block_records):
            record["decoded_as"] = "control-window"
            record["control_window_role"] = "ambience-cricket-stack"
            record["control_window_kind"] = "sndt-cricket-stack"
            record["control_window_layout"] = "stacked-pairs"
            record["control_window_stack_index"] = stack_index
            record["control_window_pair_selector"] = record["u32_1"]
            record["control_window_step"] = pair_offset
            record["control_window_slot"] = record["u32_2"]


def annotate_item_sndt_one_shot_windows(item: dict[str, object]) -> None:
    blocks = item.get("sndt_record_blocks")
    records = item.get("sndt_records")
    if not isinstance(blocks, list) or not isinstance(records, list):
        return

    for block in blocks:
        if block.get("unresolved_pattern") != "other":
            continue
        if block.get("record_count") != 6 or block.get("low16") != 0 or block.get("u32_3") != 4:
            continue

        start_record = block.get("start_record")
        end_record = block.get("end_record")
        if not isinstance(start_record, int) or not isinstance(end_record, int):
            continue

        block_records = records[start_record : end_record + 1]
        if len(block_records) != 6:
            continue

        u32_1_values = [int(record["u32_1"]) for record in block_records if isinstance(record.get("u32_1"), int)]
        u32_2_values = [int(record["u32_2"]) for record in block_records if isinstance(record.get("u32_2"), int)]
        if u32_1_values != [4, 5, 6, 7, 8, 9] or u32_2_values != [1, 2, 3, 4, 5, 6]:
            continue
        if not all(
            record.get("u32_4") == 0
            and record.get("u32_5") == 0
            and record.get("u32_6") == 0
            and record.get("u32_7") == 0
            for record in block_records
        ):
            continue

        block["decoded_as"] = "control-window"
        block["control_window_kind"] = "sndt-one-shot-window"
        block["control_window_layout"] = "linear-ramp"
        block["control_window_role"] = "ambience-one-shot-window"
        block["control_window_u32_1_values"] = u32_1_values
        block["control_window_u32_2_values"] = u32_2_values
        block["control_window_step_count"] = len(block_records)

        for offset, record in enumerate(block_records):
            record["decoded_as"] = "control-window"
            record["control_window_role"] = "ambience-one-shot-window"
            record["control_window_kind"] = "sndt-one-shot-window"
            record["control_window_layout"] = "linear-ramp"
            record["control_window_step"] = offset
            record["control_window_slot"] = record["u32_2"]


def iter_riff_chunks(data: bytes, start: int, end: int):
    offset = start
    while offset + 8 <= end:
        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        if payload_end > len(data) or payload_end > end:
            raise IcbParseError(
                f"Chunk {chunk_id!r} at 0x{offset:x} overruns container: {chunk_size} bytes"
            )
        yield chunk_id.decode("latin-1", errors="replace"), offset, chunk_size, payload_start, payload_end
        offset = payload_end + (chunk_size & 1)


def find_paired_isb(path: Path) -> str | None:
    stem_lower = path.stem.lower()
    for candidate in path.parent.glob("*.isb"):
        if candidate.stem.lower() == stem_lower:
            return str(candidate)
    return None


def summarize_payload(chunk_id: str, payload: bytes) -> dict[str, object]:
    info: dict[str, object] = {}
    if chunk_id in TEXT_CHUNKS:
        text = decode_text(payload)
        if text:
            info["text"] = text
    elif chunk_id in U8_CHUNKS and len(payload) == 1:
        info["u8"] = payload[0]
    elif chunk_id in U32_CHUNKS and len(payload) == 4:
        info["u32"] = struct.unpack_from("<I", payload, 0)[0]
    elif chunk_id in U32_ARRAY_CHUNKS and len(payload) % 4 == 0:
        values = list(struct.unpack("<" + "I" * (len(payload) // 4), payload))
        info["u32_values"] = values
        if chunk_id == "sync" and len(values) >= 2:
            info["sync_start"] = values[0]
            info["sync_start_name"] = SYNC_START_NAMES.get(values[0], "UNKNOWN")
            info["sync_multiple"] = values[1]
        elif chunk_id == "cgvi" and len(values) >= 5:
            info["content_global_var_info"] = {
                "start_var_index": values[0],
                "start_state_index": values[1],
                "stop_var_index": values[2],
                "stop_state_index": values[3],
                "flags": values[4],
            }
    elif chunk_id == "data" and payload and len(payload) % 4 == 0:
        values = list(struct.unpack("<" + "I" * (len(payload) // 4), payload))
        info["u32_values"] = values
    elif chunk_id in SMALL_U32_ARRAY_CHUNKS and payload and len(payload) % 4 == 0:
        values = list(struct.unpack("<" + "I" * (len(payload) // 4), payload))
        info["u32_values"] = values
    elif chunk_id == "rcnt":
        entries = parse_rcnt_payload(payload)
        if entries:
            info["rcnt_entries"] = entries
    elif chunk_id == "trck":
        entries = parse_trck_payload(payload)
        if entries:
            info["trck_entries"] = entries
    elif chunk_id == "sndt":
        entries = parse_sndt_payload(payload)
        if entries:
            info["sndt_records"] = entries
            info["sndt_record_count"] = len(entries)
    elif chunk_id == "ctdx":
        ctdx_info = parse_ctdx_payload(payload)
        if ctdx_info:
            info.update(ctdx_info)
    elif chunk_id == "seai":
        seai_info = parse_seai_payload(payload)
        if seai_info:
            info.update(seai_info)
    elif chunk_id in {"selv", "sepl", "secl", "ecac"}:
        record_info = parse_counted_records_payload(payload, chunk_id)
        if record_info:
            info.update(record_info)
    elif chunk_id in {"segv", "secg"} and len(payload) > 4:
        record_info = parse_counted_records_payload(payload, chunk_id)
        if record_info:
            info.update(record_info)
    elif chunk_id == "eset":
        eset_info = parse_eset_payload(payload)
        if eset_info:
            info.update(eset_info)

    if chunk_id in F32_ARRAY_CHUNKS and payload and len(payload) % 4 == 0:
        info["f32_values"] = list(struct.unpack("<" + "f" * (len(payload) // 4), payload))

    if chunk_id in TEXT_FRAGMENT_CHUNKS:
        fragments = extract_text_fragments(payload)
        if fragments:
            info["text_fragments"] = fragments

    if chunk_id in TABLE_PREVIEW_CHUNKS and payload and len(payload) % 4 == 0:
        u32_count = len(payload) // 4
        preview_count = min(TABLE_PREVIEW_VALUE_COUNT, u32_count)
        preview_raw = payload[: preview_count * 4]
        info["u32_count"] = u32_count
        info["u32_preview"] = list(struct.unpack("<" + "I" * preview_count, preview_raw))
        info["f32_preview"] = list(struct.unpack("<" + "f" * preview_count, preview_raw))
        if len(payload) <= 32:
            info["u32_values"] = list(struct.unpack("<" + "I" * u32_count, payload))

    if not info and payload:
        info["hex_preview"] = payload[:HEX_PREVIEW_BYTES].hex()
        info["decode_status"] = "raw-preview"
    elif payload:
        has_structured_decode = any(
            key.startswith(f"{chunk_id}_") and not key.endswith(("_parse_error", "_parse_warning"))
            for key in info
        )
        has_parse_error = any(key.endswith("_parse_error") for key in info)
        has_scalar_decode = any(
            key in info for key in ("u32", "u8", "text", "u32_values", "f32_values", "rcnt_entries", "trck_entries")
        )
        if has_parse_error:
            info["decode_status"] = "partial"
        elif has_structured_decode or has_scalar_decode:
            info["decode_status"] = "decoded"
        elif chunk_id in TABLE_PREVIEW_CHUNKS or chunk_id in {"sndt", "data"}:
            info["decode_status"] = "partial"
        else:
            info["decode_status"] = "decoded"
    return info


def payload_decode_status(node: dict[str, object]) -> str:
    if node.get("id") == "LIST":
        return "container"
    if node.get("decode_status") == "raw-preview":
        return "raw-preview"
    if node.get("decode_status") == "partial":
        return "partial"
    decoded_keys = set(node) - STRUCTURAL_NODE_KEYS
    if decoded_keys:
        return "decoded"
    return "empty" if node.get("size") == 0 else "raw-preview"


def collect_decode_coverage(nodes: list[dict[str, object]]) -> dict[str, object]:
    status_counts: Counter[str] = Counter()
    status_bytes: Counter[str] = Counter()
    chunk_status_counts: dict[str, Counter[str]] = {}
    chunk_status_bytes: dict[str, Counter[str]] = {}
    unresolved_examples: dict[str, dict[str, object]] = {}

    def visit(node_list: list[dict[str, object]]) -> None:
        for node in node_list:
            chunk_id = str(node.get("id"))
            status = payload_decode_status(node)
            size = int(node.get("size") or 0)
            status_counts[status] += 1
            status_bytes[status] += size
            chunk_status_counts.setdefault(chunk_id, Counter())[status] += 1
            chunk_status_bytes.setdefault(chunk_id, Counter())[status] += size
            if status in {"raw-preview", "partial"} and chunk_id not in unresolved_examples:
                unresolved_examples[chunk_id] = {
                    "status": status,
                    "offset": node.get("offset"),
                    "size": size,
                    "hex_preview": node.get("hex_preview"),
                    "text_fragments": node.get("text_fragments", [])[:8]
                    if isinstance(node.get("text_fragments"), list)
                    else [],
                }
            children = node.get("children")
            if isinstance(children, list):
                visit(children)

    visit(nodes)
    return {
        "status_counts": dict(sorted(status_counts.items())),
        "status_bytes": dict(sorted(status_bytes.items())),
        "chunk_status_counts": {key: dict(sorted(value.items())) for key, value in sorted(chunk_status_counts.items())},
        "chunk_status_bytes": {key: dict(sorted(value.items())) for key, value in sorted(chunk_status_bytes.items())},
        "unresolved_examples": dict(sorted(unresolved_examples.items())),
        "raw_examples": dict(sorted(unresolved_examples.items())),
    }


def preview_u32s_from_hex(hex_preview: str, max_count: int = 8) -> list[int]:
    raw = bytes.fromhex(hex_preview)
    count = min(max_count, len(raw) // 4)
    if count <= 0:
        return []
    return list(struct.unpack("<" + "I" * count, raw[: count * 4]))


def sndt_record_u32_preview(records: list[object]) -> list[int]:
    first = records[0] if records else None
    if not isinstance(first, dict):
        return []
    values = [
        first.get("target_ref"),
        first.get("u32_1"),
        first.get("u32_2"),
        first.get("u32_3"),
        first.get("u32_4"),
        first.get("u32_5"),
        first.get("u32_6"),
        first.get("u32_7"),
    ]
    return [int(value) for value in values if isinstance(value, int)]


def child_nodes(node: dict[str, object]) -> list[dict[str, object]]:
    children = node.get("children")
    if isinstance(children, list):
        return children
    return []


def first_child(children: list[dict[str, object]], chunk_id: str) -> dict[str, object] | None:
    for child in children:
        if child.get("id") == chunk_id:
            return child
    return None


def child_text(children: list[dict[str, object]], chunk_id: str) -> str | None:
    child = first_child(children, chunk_id)
    if not child:
        return None
    if isinstance(child.get("text"), str):
        return child["text"]
    fragments = child.get("text_fragments")
    if isinstance(fragments, list) and fragments:
        return fragments[0]
    return None


def child_u32(children: list[dict[str, object]], chunk_id: str) -> int | None:
    child = first_child(children, chunk_id)
    if child and isinstance(child.get("u32"), int):
        return child["u32"]
    return None


def child_u32_values(children: list[dict[str, object]], chunk_id: str) -> list[int] | None:
    child = first_child(children, chunk_id)
    if not child:
        return None
    values = child.get("u32_values")
    if isinstance(values, list):
        return values
    hex_preview = child.get("hex_preview")
    if isinstance(hex_preview, str):
        preview = preview_u32s_from_hex(hex_preview)
        if preview:
            return preview
    return None


def child_rcnt_entries(children: list[dict[str, object]]) -> list[dict[str, object]] | None:
    child = first_child(children, "rcnt")
    if not child:
        return None
    entries = child.get("rcnt_entries")
    if isinstance(entries, list):
        return entries
    return None


def child_trck_entries(children: list[dict[str, object]]) -> list[dict[str, object]] | None:
    child = first_child(children, "trck")
    if not child:
        return None
    entries = child.get("trck_entries")
    if isinstance(entries, list):
        return entries
    return None


def summarize_list_node(node: dict[str, object]) -> dict[str, object] | None:
    list_type = node.get("list_type")
    if list_type not in SUMMARY_LIST_TYPES:
        return None

    children = child_nodes(node)
    summary: dict[str, object] = {
        "list_type": list_type,
        "title": child_text(children, "titl"),
        "signal_group": child_text(children, "isgn"),
        "index": child_u32(children, "indx"),
        "tracks": child_u32(children, "trks"),
        "tempo": child_u32(children, "dtmp"),
        "time_code": child_u32(children, "tmcd"),
        "section": child_u32(children, "dsec"),
        "loop": child_u32(children, "loop"),
        "sync": child_u32_values(children, "sync"),
    }

    info_values = child_u32_values(children, "info")
    if info_values:
        summary["info"] = info_values

    silt_values = child_u32_values(children, "silt")
    if silt_values:
        summary["silt"] = silt_values

    rcnt_entries = child_rcnt_entries(children)
    if rcnt_entries:
        summary["rcnt_entries"] = rcnt_entries

    trck_entries = child_trck_entries(children)
    if trck_entries:
        summary["trck_entries"] = trck_entries

    stnm = child_text(children, "stnm")
    if stnm:
        summary["state_name"] = stnm

    data_child = first_child(children, "data")
    if data_child:
        summary["data_size"] = data_child["size"]
        data_values = data_child.get("u32_values")
        if isinstance(data_values, list):
            summary["data_u32_values"] = data_values
            summary["data_u32_preview"] = data_values[:8]
        else:
            data_preview = data_child.get("hex_preview")
            if isinstance(data_preview, str):
                summary["data_u32_preview"] = preview_u32s_from_hex(data_preview)

    sndt_child = first_child(children, "sndt")
    if sndt_child:
        summary["sndt_size"] = sndt_child["size"]
        if isinstance(sndt_child.get("sndt_record_count"), int):
            summary["sndt_record_count"] = sndt_child["sndt_record_count"]
        sndt_records = sndt_child.get("sndt_records")
        if isinstance(sndt_records, list) and sndt_records:
            summary["sndt_records"] = sndt_records
            summary["sndt_u32_preview"] = sndt_record_u32_preview(sndt_records)
        sndt_preview = sndt_child.get("hex_preview")
        if isinstance(sndt_preview, str):
            summary["sndt_u32_preview"] = preview_u32s_from_hex(sndt_preview)

    return {key: value for key, value in summary.items() if value is not None}


def collect_semantic_summary(nodes: list[dict[str, object]]) -> dict[str, object]:
    summary: dict[str, object] = {
        "ctdx_fragments": [],
        "ctdx_layout": None,
        "ctdx_page_count": 0,
        "ctdx_record_count": 0,
        "ctdx_named_record_count": 0,
        "ctdx_record_tag_counts": {},
        "ctdx_name_offset_counts": {},
        "seai_record_count": 0,
        "seai_named_record_count": 0,
        "seai_record_names": [],
        "sound_entries": [],
        "list_summaries": [],
    }

    def visit(node_list: list[dict[str, object]]) -> None:
        for node in node_list:
            if node.get("id") == "ctdx":
                if isinstance(node.get("ctdx_layout"), str):
                    summary["ctdx_layout"] = node["ctdx_layout"]
                if isinstance(node.get("ctdx_page_count"), int):
                    summary["ctdx_page_count"] = node["ctdx_page_count"]
                if isinstance(node.get("ctdx_record_count"), int):
                    summary["ctdx_record_count"] = node["ctdx_record_count"]
                if isinstance(node.get("ctdx_named_record_count"), int):
                    summary["ctdx_named_record_count"] = node["ctdx_named_record_count"]
                if isinstance(node.get("ctdx_record_tag_counts"), dict):
                    summary["ctdx_record_tag_counts"] = node["ctdx_record_tag_counts"]
                if isinstance(node.get("ctdx_name_offset_counts"), dict):
                    summary["ctdx_name_offset_counts"] = node["ctdx_name_offset_counts"]
                ctdx_footer = node.get("ctdx_footer")
                if isinstance(ctdx_footer, dict):
                    summary["ctdx_footer"] = ctdx_footer
                ctdx_records = node.get("ctdx_records")
                if isinstance(ctdx_records, list):
                    for record in ctdx_records:
                        if not isinstance(record, dict):
                            continue
                        name = record.get("name")
                        if isinstance(name, str) and name not in summary["ctdx_fragments"]:
                            summary["ctdx_fragments"].append(name)
                fragments = node.get("text_fragments")
                if isinstance(fragments, list):
                    for fragment in fragments:
                        if fragment not in summary["ctdx_fragments"]:
                            summary["ctdx_fragments"].append(fragment)

            if node.get("id") == "seai":
                if isinstance(node.get("seai_record_count"), int):
                    summary["seai_record_count"] = node["seai_record_count"]
                if isinstance(node.get("seai_named_record_count"), int):
                    summary["seai_named_record_count"] = node["seai_named_record_count"]
                if isinstance(node.get("seai_record_size"), int):
                    summary["seai_record_size"] = node["seai_record_size"]
                parse_error = node.get("seai_record_parse_error")
                if isinstance(parse_error, str):
                    summary["seai_record_parse_error"] = parse_error
                parse_warning = node.get("seai_record_parse_warning")
                if isinstance(parse_warning, str):
                    summary["seai_record_parse_warning"] = parse_warning
                seai_records = node.get("seai_records")
                if isinstance(seai_records, list) and "seai_record_parse_error" not in summary and "seai_record_parse_warning" not in summary:
                    for record in seai_records:
                        if not isinstance(record, dict):
                            continue
                        name = record.get("name")
                        if isinstance(name, str) and name not in summary["seai_record_names"]:
                            summary["seai_record_names"].append(name)

            if node.get("id") == "LIST":
                list_summary = summarize_list_node(node)
                if list_summary:
                    summary["list_summaries"].append(list_summary)
                    if list_summary.get("list_type") == "snde":
                        summary["sound_entries"].append(list_summary)

            children = child_nodes(node)
            if children:
                visit(children)

    visit(nodes)

    title_index = {
        (item.get("list_type"), item.get("index")): item.get("title")
        for item in summary["list_summaries"]
        if item.get("list_type") and item.get("index") is not None and item.get("title")
    }
    for item in summary["list_summaries"]:
        rcnt_entries = item.get("rcnt_entries")
        if not isinstance(rcnt_entries, list):
            continue
        resolved_entries = []
        for entry in rcnt_entries:
            resolved = dict(entry)
            key = (entry.get("target_type"), entry.get("target_index"))
            target_title = title_index.get(key)
            if target_title:
                resolved["target_title"] = target_title
            resolved_entries.append(resolved)
        item["rcnt_entries"] = resolved_entries

    for item in summary["list_summaries"]:
        trck_entries = item.get("trck_entries")
        if not isinstance(trck_entries, list):
            continue
        resolved_entries = []
        for entry in trck_entries:
            resolved = dict(entry)
            key = (entry.get("target_type"), entry.get("target_index"))
            target_title = title_index.get(key)
            if target_title:
                resolved["target_title"] = target_title
            resolved_entries.append(resolved)
        item["trck_entries"] = resolved_entries

    return summary


def resolve_external_references(path: Path, paired_isb: str | None, summary: dict[str, object]) -> None:
    if not paired_isb:
        return

    needs_sample_resolution = any(
        isinstance(item.get("rcnt_entries"), list)
        and any(entry.get("target_type") == "samp" and not entry.get("target_title") for entry in item["rcnt_entries"])
        for item in summary["list_summaries"]
    )
    needs_sndt_resolution = any(
        isinstance(item.get("sndt_u32_preview"), list) and item["sndt_u32_preview"]
        for item in summary["sound_entries"]
    )
    needs_sqob_resolution = any(
        item.get("list_type") == "sqob"
        and isinstance(item.get("data_u32_values"), list)
        and item["data_u32_values"]
        for item in summary["list_summaries"]
    )
    if not (needs_sample_resolution or needs_sndt_resolution or needs_sqob_resolution):
        return

    try:
        from extract_isb import parse_isb_file
    except ImportError:
        try:
            from scripts.extractors.extract_isb import parse_isb_file
        except ImportError:
            return

    try:
        samples = parse_isb_file(Path(paired_isb))
    except Exception:
        return
    sample_count = len(samples)

    title_to_sample = build_unique_sample_title_lookup(samples)

    def resolve_sqob_sample_refs(data_values: list[object]) -> list[dict[str, object]]:
        refs: list[dict[str, object]] = []
        seen_sample_indexes: set[int] = set()
        for value in data_values:
            if not isinstance(value, int):
                continue
            if value >> 16 != 1:
                continue
            sample_index = value & 0xFFFF
            if not (0 <= sample_index < sample_count):
                continue
            sample = samples[sample_index]
            if sample.index in seen_sample_indexes:
                continue
            seen_sample_indexes.add(sample.index)
            refs.append(
                {
                    "ref": value,
                    "target_title": sample.display_name,
                    "target_sample_index": sample.index,
                    "target_bank": Path(paired_isb).name,
                }
            )
        return refs

    for item in summary["list_summaries"]:
        if item.get("list_type") != "sqob":
            continue
        data_values = item.get("data_u32_values")
        if not isinstance(data_values, list):
            continue
        refs = resolve_sqob_sample_refs(data_values)
        if refs:
            item["sqob_sample_refs"] = refs

    def resolve_sndt_primary(primary_ref: int) -> tuple[str, object] | None:
        direct_index = primary_ref - 0x10000
        if 0 <= direct_index < len(samples):
            return ("direct", samples[direct_index])

        packed_low16 = primary_ref & 0xFFFF
        packed_index = packed_low16 - 1
        if 0 <= packed_index < len(samples):
            return ("packed-low16", samples[packed_index])

        return None

    def resolve_sndt_ref(target_ref: int) -> tuple[str, object] | None:
        return resolve_sndt_primary(target_ref)

    for item in summary["list_summaries"]:
        rcnt_entries = item.get("rcnt_entries")
        if not isinstance(rcnt_entries, list):
            continue
        for entry in rcnt_entries:
            if entry.get("target_type") != "samp" or entry.get("target_title"):
                continue
            target_index = entry.get("target_index")
            if not isinstance(target_index, int):
                continue
            sample_index = target_index - 0x10000
            if 0 <= sample_index < len(samples):
                entry["target_title"] = samples[sample_index].display_name
                entry["target_sample_index"] = samples[sample_index].index
                entry["target_bank"] = Path(paired_isb).name

    for item in summary["sound_entries"]:
        sndt_records = item.get("sndt_records")
        if isinstance(sndt_records, list):
            resolved_records = []
            unresolved_patterns: list[str] = []
            for record in sndt_records:
                resolved_record = dict(record)
                target_ref = resolved_record.get("target_ref")
                if isinstance(target_ref, int):
                    resolved = resolve_sndt_ref(target_ref)
                    if resolved is None:
                        resolved = resolve_title_fallback_alias(
                            paired_isb,
                            item.get("title"),
                            resolved_record.get("u32_2"),
                            title_to_sample,
                        )
                        if resolved is not None:
                            resolved = ("title-fallback-alias", resolved)
                    if resolved is not None:
                        ref_mode, sample = resolved
                        resolved_record["target_ref_mode"] = ref_mode
                        resolved_record["target_title"] = sample.display_name
                        resolved_record["target_sample_index"] = sample.index
                        resolved_record["target_bank"] = Path(paired_isb).name
                    else:
                        pattern = classify_unresolved_sndt_record(resolved_record, len(sndt_records))
                        resolved_record["unresolved_pattern"] = pattern
                        if pattern not in unresolved_patterns:
                            unresolved_patterns.append(pattern)
                resolved_records.append(resolved_record)
            item["sndt_records"] = resolved_records
            item["sndt_record_blocks"] = build_sndt_record_blocks(resolved_records, sample_count)
            if unresolved_patterns:
                item["sndt_unresolved_record_patterns"] = unresolved_patterns
                item["sndt_unresolved_record_count"] = sum(
                    1 for record in resolved_records if not record.get("target_title")
                )

        sndt_preview = item.get("sndt_u32_preview")
        if not isinstance(sndt_preview, list) or not sndt_preview:
            continue

        title_key = compact_name(str(item.get("title") or ""))
        if title_key:
            titled_sample = title_to_sample.get(title_key)
            if titled_sample is not None:
                item["sndt_primary_ref"] = sndt_preview[0]
                item["sndt_primary_ref_mode"] = "paired-title-match"
                item["sndt_primary_target_title"] = titled_sample.display_name
                item["sndt_primary_target_sample_index"] = titled_sample.index
                item["sndt_primary_target_bank"] = Path(paired_isb).name
                annotate_item_sndt_record_blocks(item)
                annotate_item_sndt_control_windows(item)
                annotate_item_sndt_dual_layer_profiles(item)
                annotate_item_sndt_cricket_stack(item)
                annotate_item_sndt_one_shot_windows(item)
                continue

        first_record = next(
            (
                record
                for record in item.get("sndt_records", [])
                if record.get("record_index") == 0 and record.get("target_ref") == sndt_preview[0]
            ),
            None,
        )
        if isinstance(first_record, dict) and first_record.get("target_ref_mode") == "title-fallback-alias":
            item["sndt_primary_ref"] = sndt_preview[0]
            item["sndt_primary_ref_mode"] = "title-fallback-alias"
            item["sndt_primary_target_title"] = first_record.get("target_title")
            item["sndt_primary_target_sample_index"] = first_record.get("target_sample_index")
            item["sndt_primary_target_bank"] = Path(paired_isb).name
            annotate_item_sndt_record_blocks(item)
            annotate_item_sndt_control_windows(item)
            annotate_item_sndt_dual_layer_profiles(item)
            annotate_item_sndt_cricket_stack(item)
            annotate_item_sndt_one_shot_windows(item)
            continue

        primary_ref = sndt_preview[0]
        if not isinstance(primary_ref, int):
            continue
        resolved = resolve_sndt_primary(primary_ref)
        if resolved is not None:
            ref_mode, sample = resolved
            item["sndt_primary_ref"] = primary_ref
            item["sndt_primary_ref_mode"] = ref_mode
            item["sndt_primary_target_title"] = sample.display_name
            item["sndt_primary_target_sample_index"] = sample.index
            item["sndt_primary_target_bank"] = Path(paired_isb).name

        annotate_item_sndt_record_blocks(item)
        annotate_item_sndt_control_windows(item)
        annotate_item_sndt_dual_layer_profiles(item)
        annotate_item_sndt_cricket_stack(item)
        annotate_item_sndt_one_shot_windows(item)


def parse_chunk_tree(data: bytes, start: int, end: int, counters: Counter, list_types: Counter):
    nodes = []
    for chunk_id, offset, chunk_size, payload_start, payload_end in iter_riff_chunks(data, start, end):
        counters[chunk_id] += 1
        node: dict[str, object] = {
            "id": chunk_id,
            "offset": offset,
            "size": chunk_size,
        }
        payload = data[payload_start:payload_end]
        if chunk_id == "LIST":
            if len(payload) < 4:
                raise IcbParseError(f"LIST chunk at 0x{offset:x} is too short")
            list_type = payload[:4].decode("latin-1", errors="replace")
            list_types[list_type] += 1
            node["list_type"] = list_type
            node["children"] = parse_chunk_tree(data, payload_start + 4, payload_end, counters, list_types)
        else:
            node.update(summarize_payload(chunk_id, payload))
        nodes.append(node)
    return nodes


def extract_known_fields(nodes: list[dict[str, object]], fields: dict[str, object]) -> None:
    for node in nodes:
        chunk_id = node["id"]
        if chunk_id == "titl" and "text" in node and "title" not in fields:
            fields["title"] = node["text"]
        elif chunk_id in U32_CHUNKS and "u32" in node and chunk_id not in fields:
            fields[chunk_id] = node["u32"]
        elif chunk_id in U32_ARRAY_CHUNKS and "u32_values" in node and chunk_id not in fields:
            fields[chunk_id] = node["u32_values"]

        children = node.get("children")
        if isinstance(children, list):
            extract_known_fields(children, fields)


def inspect_icb(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    if len(data) < 12:
        raise IcbParseError("File too small for RIFF header")
    if data[:4] != b"RIFF":
        raise IcbParseError(f"Expected RIFF header, found {data[:4]!r}")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size + 8 > len(data):
        raise IcbParseError(f"RIFF size {riff_size} exceeds file length {len(data)}")
    if data[8:12] != b"icbf":
        raise IcbParseError(f"Expected icbf form type, found {data[8:12]!r}")

    counters: Counter[str] = Counter()
    list_types: Counter[str] = Counter()
    nodes = parse_chunk_tree(data, 12, min(len(data), riff_size + 8), counters, list_types)
    fields: dict[str, object] = {}
    extract_known_fields(nodes, fields)
    semantic_summary = collect_semantic_summary(nodes)
    resolve_external_references(path, find_paired_isb(path), semantic_summary)
    decode_coverage = collect_decode_coverage(nodes)

    return {
        "file": str(path),
        "size": len(data),
        "riff_size": riff_size,
        "paired_isb": find_paired_isb(path),
        "known_fields": fields,
        "chunk_counts": dict(sorted(counters.items())),
        "list_types": dict(sorted(list_types.items())),
        "decode_coverage": decode_coverage,
        "semantic_summary": semantic_summary,
        "chunks": nodes,
    }


def collect_inputs(input_path: Path, glob_pattern: str) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        return sorted(input_path.rglob(glob_pattern))
    raise FileNotFoundError(f"Input path does not exist: {input_path}")


def output_stem_for_path(file_path: Path, input_root: Path) -> str:
    if input_root.is_dir():
        relative = file_path.relative_to(input_root).with_suffix("")
        return "__".join(relative.parts)
    return file_path.stem


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to a .icb file or a directory containing .icb files")
    parser.add_argument(
        "--glob",
        default=DEFAULT_GLOB,
        help="Glob pattern used when input is a directory",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output/audio/icb"),
        help="Output directory for JSON inspection reports",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    input_path = Path(args.input).expanduser().resolve()
    out_root = args.out.expanduser().resolve()

    try:
        files = collect_inputs(input_path, args.glob)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not files:
        print(f"No files matched {args.glob!r} under {input_path}", file=sys.stderr)
        return 1

    out_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, object] = {"cues": [], "failed": []}

    for path in files:
        try:
            report = inspect_icb(path)
        except Exception as exc:
            print(f"[FAIL] {path}: {exc}", file=sys.stderr)
            summary["failed"].append({"file": str(path), "error": str(exc)})
            continue

        report_path = out_root / f"{output_stem_for_path(path, input_path)}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary["cues"].append(
            {
                "file": str(path),
                "report": str(report_path),
                "paired_isb": report["paired_isb"],
                "title": report["known_fields"].get("title"),
                "chunk_counts": report["chunk_counts"],
                "decode_status_counts": report["decode_coverage"]["status_counts"],
                "decode_status_bytes": report["decode_coverage"]["status_bytes"],
                "semantic_summary": report["semantic_summary"],
            }
        )
        print(f"[OK] {path.name}: {len(report['chunks'])} top-level chunks")

    summary_path = out_root / "cues.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Wrote summary to {summary_path}")
    return 0 if not summary["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())