#!/usr/bin/env python3
"""Inspect printable spans inside an embedded SpeedTree .spt payload."""

from __future__ import annotations

import argparse
import os
import struct
import sys
from collections import Counter
from typing import Iterator, Sequence, Tuple


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(SCRIPT_DIR)
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)


from ue2 import UE2Package

from vanguard_assets import config
PROJECT_ROOT = config.PROJECT_ROOT


PrintableSpan = Tuple[int, str]
TokenizedStringRecord = Tuple[int, int, int, str]
TokenHit = Tuple[int, int]
SptHeader = Tuple[int, str]


def find_spt_header(data: bytes) -> SptHeader:
    header_prefix = b"__IdvSpt_"
    search_offset = 0

    while True:
        header_offset = data.find(header_prefix, search_offset)
        if header_offset < 0:
            break

        if header_offset >= 8:
            token, text_length = struct.unpack_from("<II", data, header_offset - 8)
            if token == 1000 and 4 <= text_length <= 64:
                header_end = header_offset + text_length
                if header_end <= len(data):
                    version_text = data[header_offset:header_end].decode("ascii", errors="replace")
                    return header_offset - 8, version_text

        search_offset = header_offset + len(header_prefix)

    raise SystemExit("No canonical SPT header (__IdvSpt_*) found in export data")


def build_default_dump_path(export_name: str, package_path: str) -> str:
    package_stem = os.path.splitext(os.path.basename(package_path))[0]
    dump_dir = os.path.join(config.DATA_DIR, "speedtree_spt")
    return os.path.join(dump_dir, f"{package_stem}__{export_name}.spt")


def dump_spt_payload(data: bytes, start_offset: int, dump_path: str) -> None:
    os.makedirs(os.path.dirname(dump_path), exist_ok=True)
    with open(dump_path, "wb") as handle:
        handle.write(data[start_offset:])


def iter_printable_spans(
    data: bytes, start_offset: int, min_length: int = 12
) -> Iterator[PrintableSpan]:
    span_start = None
    span_bytes = bytearray()

    for rel_offset, value in enumerate(data[start_offset:], start=start_offset):
        is_printable = value in (0x09, 0x0A, 0x0D) or 0x20 <= value < 0x7F
        if is_printable:
            if span_start is None:
                span_start = rel_offset
            span_bytes.append(value)
            continue

        if span_start is not None and len(span_bytes) >= min_length:
            yield span_start, span_bytes.decode("ascii", errors="replace")
        span_start = None
        span_bytes = bytearray()

    if span_start is not None and len(span_bytes) >= min_length:
        yield span_start, span_bytes.decode("ascii", errors="replace")


def summarize_span(text: str, max_len: int = 200) -> str:
    compact = " ".join(part for part in text.splitlines() if part.strip())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def dump_word_context(data: bytes, center_offset: int, window_words: int) -> None:
    start = max(0, center_offset - window_words * 4)
    end = min(len(data), center_offset + window_words * 4)
    start -= start % 4

    print(f"    word_context center={center_offset} window_words={window_words}")
    for offset in range(start, end, 4):
        chunk = data[offset : offset + 4]
        if len(chunk) < 4:
            break
        as_int = struct.unpack_from("<i", chunk)[0]
        as_uint = struct.unpack_from("<I", chunk)[0]
        as_float = struct.unpack_from("<f", chunk)[0]
        ascii_repr = "".join(chr(value) if 32 <= value < 127 else "." for value in chunk)
        marker = "<--" if offset <= center_offset < offset + 4 else "   "
        print(
            f"    {marker} @{offset:6d} hex={chunk.hex()} int={as_int:12d} "
            f"uint={as_uint:12d} float={as_float:14.6g} ascii='{ascii_repr}'"
        )


def is_printable_ascii_block(data: bytes) -> bool:
    return all(value in (0x09, 0x0A, 0x0D) or 0x20 <= value < 0x7F for value in data)


def iter_tokenized_strings(
    data: bytes,
    start_offset: int,
    min_token: int = 70000,
    max_token: int = 79999,
    min_length: int = 4,
    max_length: int = 260,
) -> Iterator[TokenizedStringRecord]:
    offset = start_offset
    limit = len(data) - 8

    while offset <= limit:
        token, text_length = struct.unpack_from("<II", data, offset)
        if not (min_token <= token <= max_token and min_length <= text_length <= max_length):
            offset += 1
            continue

        text_end = offset + 8 + text_length
        if text_end > len(data):
            offset += 1
            continue

        raw_text = data[offset + 8 : text_end]
        if not is_printable_ascii_block(raw_text):
            offset += 1
            continue

        yield offset, token, text_length, raw_text.decode("ascii")
        offset = text_end


def iter_token_hits(
    data: bytes,
    start_offset: int,
    target_tokens: Sequence[int],
) -> Iterator[TokenHit]:
    token_set = set(target_tokens)
    for offset in range(start_offset, len(data) - 4):
        token = struct.unpack_from("<I", data, offset)[0]
        if token in token_set:
            yield offset, token


def find_export(pkg: UE2Package, export_name: str):
    exact_match = next((exp for exp in pkg.exports if exp["object_name"] == export_name), None)
    if exact_match is not None:
        return exact_match

    lowered = export_name.lower()
    partial_matches = [
        exp for exp in pkg.exports if lowered in exp["object_name"].lower()
    ]
    if len(partial_matches) == 1:
        return partial_matches[0]
    if len(partial_matches) > 1:
        raise SystemExit(
            "Multiple exports matched: "
            + ", ".join(exp["object_name"] for exp in partial_matches[:10])
        )
    raise SystemExit(f"No export matched '{export_name}'")


def inspect_export(
    package_path: str,
    export_name: str,
    keywords: Sequence[str],
    min_span_length: int,
    max_matches: int,
    context_words: int,
    show_tokenized_strings: bool,
    show_low_tokens: bool,
    dump_file: str | None,
) -> int:
    pkg = UE2Package(package_path)
    export = find_export(pkg, export_name)
    data = pkg.get_export_data(export)

    spt_offset, spt_version = find_spt_header(data)
    marker_offset = data.find(b"SpeedTree")
    if marker_offset < 0:
        raise SystemExit("No embedded SpeedTree marker found in export data")

    spans = list(iter_printable_spans(data, marker_offset, min_span_length))
    tokenized_strings = list(iter_tokenized_strings(data, marker_offset))
    low_token_hits = list(iter_token_hits(data, max(0, marker_offset - 256), [1000, 1001, 1002, 1004, 1011, 7000]))
    lowered_keywords = [keyword.lower() for keyword in keywords if keyword]
    keyword_hits = []
    token_keyword_hits = []

    for offset, text in spans:
        lowered = text.lower()
        matched = [keyword for keyword in lowered_keywords if keyword in lowered]
        if matched:
            keyword_hits.append((offset, matched, text))

    for offset, token, text_length, text in tokenized_strings:
        lowered = text.lower()
        matched = [keyword for keyword in lowered_keywords if keyword in lowered]
        if matched:
            token_keyword_hits.append((offset, token, text_length, matched, text))

    bezier_hits = sum(text.count("BezierSpline") for _, text in spans)
    token_counts = Counter(token for _, token, _, _ in tokenized_strings)

    print(f"export={export['object_name']}")
    print(f"bytes_total={len(data)}")
    print(f"spt_offset={spt_offset}")
    print(f"spt_version={spt_version}")
    print(f"spt_bytes_total={len(data) - spt_offset}")
    print(f"speedtree_offset={marker_offset}")
    print(f"printable_span_count={len(spans)}")
    print(f"bezier_spline_mentions={bezier_hits}")
    print(f"keyword_hit_count={len(keyword_hits)}")
    print(f"tokenized_string_count={len(tokenized_strings)}")
    print(f"low_token_hit_count={len(low_token_hits)}")

    if dump_file:
        dump_spt_payload(data, spt_offset, dump_file)
        print(f"dumped_spt={dump_file}")

    if token_counts:
        print("top_token_ids:")
        for token, count in token_counts.most_common(10):
            print(f"  token={token} count={count}")

    if show_low_tokens and low_token_hits:
        print("low_token_hits:")
        for offset, token in low_token_hits[:40]:
            print(f"  @{offset} token={token} delta_from_speedtree={offset - marker_offset}")

    if show_tokenized_strings and tokenized_strings:
        print("tokenized_strings:")
        for offset, token, text_length, text in tokenized_strings:
            print(f"  @{offset} token={token} length={text_length}")
            print(f"    {summarize_span(text, max_len=300)}")

    if spans:
        print("top_printable_spans:")
        for offset, text in sorted(spans, key=lambda item: len(item[1]), reverse=True)[:5]:
            print(f"  @{offset}: {summarize_span(text)}")

    if keyword_hits:
        print("keyword_hits:")
        for offset, matched, text in keyword_hits[:max_matches]:
            print(f"  @{offset} keywords={','.join(matched)}")
            print(f"    {summarize_span(text, max_len=300)}")
            if context_words > 0:
                dump_word_context(data, offset, context_words)

    if token_keyword_hits:
        print("tokenized_string_hits:")
        for offset, token, text_length, matched, text in token_keyword_hits[:max_matches]:
            print(
                f"  @{offset} token={token} length={text_length} "
                f"keywords={','.join(matched)}"
            )
            print(f"    {summarize_span(text, max_len=300)}")
            if context_words > 0:
                dump_word_context(data, offset, context_words)

    return 0 if keyword_hits else 1


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "export_name",
        help="Exact export name or unique substring, e.g. Ra5000_P1_C1_SpeedTrees_Deadlands002",
    )
    parser.add_argument(
        "--package",
        default=os.path.join(config.ASSETS_PATH, "Meshes", "Ra5000_P1_C1_SpeedTrees_mesh.usx"),
        help="Path to the .usx package containing the export",
    )
    parser.add_argument(
        "--keywords",
        default="billboard,card,leaf,map,size,frond,branch",
        help="Comma-separated case-insensitive keywords to search within printable spans",
    )
    parser.add_argument(
        "--min-span-length",
        type=int,
        default=12,
        help="Minimum printable span length to keep",
    )
    parser.add_argument(
        "--max-matches",
        type=int,
        default=20,
        help="Maximum number of matching spans to print",
    )
    parser.add_argument(
        "--context-words",
        type=int,
        default=0,
        help="Number of 4-byte words of binary context to print on each side of a keyword hit",
    )
    parser.add_argument(
        "--show-tokenized-strings",
        action="store_true",
        help="Print all parsed tokenized string records after the SpeedTree marker",
    )
    parser.add_argument(
        "--show-low-tokens",
        action="store_true",
        help="Print byte-aligned low-number section tokens near and after the SpeedTree marker",
    )
    parser.add_argument(
        "--dump-file",
        help="Write the embedded SPT payload as a standalone .spt file",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    keywords = [part.strip() for part in args.keywords.split(",") if part.strip()]
    dump_file = args.dump_file or None
    if args.dump_file == "AUTO":
        dump_file = build_default_dump_path(args.export_name, args.package)
    return inspect_export(
        package_path=args.package,
        export_name=args.export_name,
        keywords=keywords,
        min_span_length=args.min_span_length,
        max_matches=args.max_matches,
        context_words=args.context_words,
        show_tokenized_strings=args.show_tokenized_strings,
        show_low_tokens=args.show_low_tokens,
        dump_file=dump_file,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))