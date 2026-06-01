#!/usr/bin/env python3
"""Structural inspector for Vanguard ISACT `.isb` sample banks.

This walks the entire RIFF `isbf` tree, decodes the known scalar/text
chunks at both the top level and inside ``LIST(samp)`` entries, and emits
a per-bank JSON report alongside a corpus-wide ``banks.json`` summary
that includes a decode coverage breakdown (mirroring the ICB inspector).

This is a structural sibling of ``extract_isb.py`` -- it does not write
``.ogg`` payloads. Use it to audit how much of every ISB byte is
explained by the parser and to surface unparsed chunks for further
reverse engineering.

Usage:
    python3 scripts/extractors/inspect_isb.py /path/to/file.isb
    python3 scripts/extractors/inspect_isb.py /path/to/Assets --glob '*.isb'
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import Counter
from pathlib import Path


DEFAULT_GLOB = "*.isb"

# Chunks whose entire payload is a UTF-16 (or latin-1) text string.
TEXT_CHUNKS = {"titl", "isgn"}

# Single-u32 scalar chunks (4-byte payload).
U32_CHUNKS = {
    "indx",
    "geix",
    "trks",
    "loop",
    "prel",
    "s3di",
    "stri",
    "msti",
    "stat",
    "chfl",
    "dtmp",
    "dsec",
    "chnk",
    "gbst",   # f32 in practice but we emit both interpretations
}

# u16 / u16 packed pair chunks.
U16_PAIR_CHUNKS = {
    "tmcd",   # observed (0x28, 0x19) ~ tempo metadata
    "dtsg",   # observed (0x04, 0x04) ~ default time signature
}

OGGS_MAGIC = b"OggS"

CODEC_NAMES = {
    0: "PCM",
    1: "XBOX_IMA",
    2: "OGG_VORBIS",
    3: "WMA",
    4: "XMA",
    5: "MSF",
    6: "MS_ADPCM",
    7: "MS_PCM_BIG_ENDIAN",
}

SYNC_START_NAMES = {
    0: "IMMEDIATE",
    1: "CLOCK",
    2: "BEAT",
    3: "BAR",
    4: "MARKER",
    5: "COUNT",
}

HEX_PREVIEW_BYTES = 32

STRUCTURAL_NODE_KEYS = {
    "id",
    "list_type",
    "offset",
    "size",
    "children",
    "decode_status",
    "hex_preview",
}


class IsbParseError(ValueError):
    pass


def decode_utf16le_z(payload: bytes) -> str:
    text = payload.decode("utf-16-le", errors="replace")
    return text.replace("\x00", "").strip()


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


def codec_name(codec_id: int | None) -> str:
    if codec_id is None:
        return "unknown"
    return CODEC_NAMES.get(codec_id, f"unknown_{codec_id}")


def iter_riff_chunks(data: bytes, start: int, end: int):
    offset = start
    while offset + 8 <= end:
        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        if payload_end > len(data) or payload_end > end:
            raise IsbParseError(
                f"Chunk {chunk_id!r} at 0x{offset:x} overruns container: "
                f"{chunk_size} bytes"
            )
        yield (
            chunk_id.decode("latin-1", errors="replace"),
            offset,
            chunk_size,
            payload_start,
            payload_end,
        )
        offset = payload_end + (chunk_size & 1)


def parse_sinf_payload(payload: bytes) -> dict[str, object]:
    info: dict[str, object] = {}
    if len(payload) >= 4:
        info["buffer_offset"] = struct.unpack_from("<I", payload, 0)[0]
    if len(payload) >= 8:
        info["time_length"] = struct.unpack_from("<I", payload, 4)[0]
    if len(payload) >= 12:
        info["sample_rate"] = struct.unpack_from("<I", payload, 8)[0]
    if len(payload) >= 16:
        info["pcm_byte_length"] = struct.unpack_from("<I", payload, 12)[0]
    if len(payload) >= 18:
        info["bits_per_sample"] = struct.unpack_from("<H", payload, 16)[0]
    if len(payload) >= 20:
        info["sinf_trailing_u16"] = struct.unpack_from("<H", payload, 18)[0]
    return info


def parse_cmpi_payload(payload: bytes) -> dict[str, object]:
    info: dict[str, object] = {}
    if len(payload) >= 4:
        cid = struct.unpack_from("<I", payload, 0)[0]
        info["codec_id"] = cid
        info["codec_name"] = codec_name(cid)
    if len(payload) >= 8:
        tcid = struct.unpack_from("<I", payload, 4)[0]
        info["target_codec_id"] = tcid
        info["target_codec_name"] = codec_name(tcid)
    if len(payload) >= 12:
        info["compression_total_size"] = struct.unpack_from("<I", payload, 8)[0]
    if len(payload) >= 16:
        info["packet_size"] = struct.unpack_from("<I", payload, 12)[0]
    if len(payload) >= 20:
        info["compression_ratio"] = struct.unpack_from("<f", payload, 16)[0]
    if len(payload) >= 24:
        info["compression_quality"] = struct.unpack_from("<f", payload, 20)[0]
    return info


def parse_cgvi_payload(payload: bytes) -> dict[str, object] | None:
    if len(payload) < 20:
        return None
    values = struct.unpack_from("<5I", payload, 0)
    return {
        "content_global_var_info": {
            "start_var_index": values[0],
            "start_state_index": values[1],
            "stop_var_index": values[2],
            "stop_state_index": values[3],
            "flags": values[4],
        }
    }


def parse_sync_payload(payload: bytes) -> dict[str, object] | None:
    if len(payload) < 8:
        return None
    start, multiple = struct.unpack_from("<II", payload, 0)
    return {
        "sync_start": start,
        "sync_start_name": SYNC_START_NAMES.get(start, "UNKNOWN"),
        "sync_multiple": multiple,
    }


def parse_data_payload(payload: bytes, payload_offset: int) -> dict[str, object]:
    info: dict[str, object] = {"data_size": len(payload)}
    ogg_rel = payload.find(OGGS_MAGIC)
    if ogg_rel >= 0:
        info["ogg_offset"] = payload_offset + ogg_rel
        info["ogg_size"] = len(payload) - ogg_rel
        info["ogg_header_offset_within_data"] = ogg_rel
    return info


def parse_sbtp_payload(payload: bytes) -> dict[str, object]:
    """Decode the rare top-level ``sbtp`` (sample-bank type) record.

    Observed only on ``GlobalFalls.isb`` so far. Layout looks like a
    single ICB-style 264-byte sub-record: ``u32 record_count`` followed
    by an embedded UTF-16 name and trailing flags. We surface the
    leading u32, the embedded name, and a hex preview.
    """

    info: dict[str, object] = {"sbtp_size": len(payload)}
    if len(payload) >= 4:
        info["sbtp_record_count"] = struct.unpack_from("<I", payload, 0)[0]
    name_match = re.search(
        rb"(?:[\x20-\x7e\-\.]\x00){2,}\x00\x00", payload
    )
    if name_match:
        info["sbtp_name"] = decode_utf16le_z(name_match.group(0))
        info["sbtp_name_offset"] = name_match.start()
    info["sbtp_hex_preview"] = payload[:HEX_PREVIEW_BYTES].hex()
    return info


def summarize_payload(
    chunk_id: str,
    payload: bytes,
    payload_offset: int,
    *,
    is_sample: bool,
) -> dict[str, object]:
    info: dict[str, object] = {}

    if chunk_id in TEXT_CHUNKS:
        text = decode_text(payload)
        if text:
            info["text"] = text
    elif chunk_id == "gbst" and len(payload) == 4:
        info["u32"] = struct.unpack_from("<I", payload, 0)[0]
        info["f32"] = struct.unpack_from("<f", payload, 0)[0]
    elif chunk_id in U32_CHUNKS and len(payload) == 4:
        info["u32"] = struct.unpack_from("<I", payload, 0)[0]
    elif chunk_id in U16_PAIR_CHUNKS and len(payload) >= 4:
        info["u16_pair"] = list(struct.unpack_from("<2H", payload, 0))
    elif chunk_id == "path" and len(payload) == 2:
        info["u16"] = struct.unpack_from("<H", payload, 0)[0]
    elif chunk_id == "cone" and len(payload) == 20:
        inner, outer, outside_vol, reserved, mode = struct.unpack_from("<5I", payload, 0)
        info["cone"] = {
            "inner_angle": inner,
            "outer_angle": outer,
            "outside_volume": outside_vol,
            "reserved": reserved,
            "mode_flags": mode,
        }
    elif chunk_id == "sdst" and len(payload) == 16:
        f_values = struct.unpack_from("<4f", payload, 0)
        u_values = struct.unpack_from("<4I", payload, 0)
        info["sdst"] = {
            "min_distance": f_values[0],
            "max_distance": f_values[1],
            "rolloff_or_attenuation": f_values[2],
            "flags": u_values[3],
        }
    elif chunk_id == "sync":
        sync_info = parse_sync_payload(payload)
        if sync_info:
            info.update(sync_info)
    elif chunk_id == "sinf":
        info.update(parse_sinf_payload(payload))
    elif chunk_id == "cmpi":
        info.update(parse_cmpi_payload(payload))
    elif chunk_id == "cgvi":
        cgvi = parse_cgvi_payload(payload)
        if cgvi:
            info.update(cgvi)
    elif chunk_id == "data":
        info.update(parse_data_payload(payload, payload_offset))
    elif chunk_id == "sbtp":
        info.update(parse_sbtp_payload(payload))
    elif chunk_id == "soff" and len(payload) >= 4:
        info["soff_offset"] = struct.unpack_from("<I", payload, 0)[0]

    if not info and payload:
        info["hex_preview"] = payload[:HEX_PREVIEW_BYTES].hex()
        info["decode_status"] = "raw-preview"
    elif payload:
        info["decode_status"] = "decoded"
    return info


def build_chunk_tree(
    data: bytes,
    start: int,
    end: int,
    *,
    parent_list_type: str | None = None,
) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = []
    for chunk_id, offset, size, payload_start, payload_end in iter_riff_chunks(
        data, start, end
    ):
        node: dict[str, object] = {
            "id": chunk_id,
            "offset": offset,
            "size": size,
        }
        if chunk_id == "LIST":
            list_type = data[payload_start : payload_start + 4].decode(
                "latin-1", errors="replace"
            )
            node["list_type"] = list_type
            node["children"] = build_chunk_tree(
                data,
                payload_start + 4,
                payload_end,
                parent_list_type=list_type,
            )
        else:
            payload = data[payload_start:payload_end]
            info = summarize_payload(
                chunk_id,
                payload,
                payload_start,
                is_sample=parent_list_type == "samp",
            )
            node.update(info)
        nodes.append(node)
    return nodes


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
                }
            children = node.get("children")
            if isinstance(children, list):
                visit(children)

    visit(nodes)
    return {
        "status_counts": dict(sorted(status_counts.items())),
        "status_bytes": dict(sorted(status_bytes.items())),
        "chunk_status_counts": {
            key: dict(sorted(value.items()))
            for key, value in sorted(chunk_status_counts.items())
        },
        "chunk_status_bytes": {
            key: dict(sorted(value.items()))
            for key, value in sorted(chunk_status_bytes.items())
        },
        "unresolved_examples": dict(sorted(unresolved_examples.items())),
    }


def collect_chunk_counts(nodes: list[dict[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()

    def visit(node_list: list[dict[str, object]]) -> None:
        for node in node_list:
            cid = str(node.get("id"))
            if cid == "LIST":
                counts[f"LIST({node.get('list_type')})"] += 1
            else:
                counts[cid] += 1
            children = node.get("children")
            if isinstance(children, list):
                visit(children)

    visit(nodes)
    return dict(sorted(counts.items()))


def collect_sample_summaries(
    nodes: list[dict[str, object]],
) -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []

    def visit(node_list: list[dict[str, object]]) -> None:
        for node in node_list:
            if node.get("id") == "LIST" and node.get("list_type") == "samp":
                summary: dict[str, object] = {
                    "offset": node.get("offset"),
                    "size": node.get("size"),
                }
                for child in node.get("children", []) or []:
                    cid = child.get("id")
                    if cid == "titl" and child.get("text"):
                        summary["title"] = child["text"]
                    elif cid == "isgn" and child.get("text"):
                        summary["signal_group"] = child["text"]
                    elif cid == "sinf":
                        for k in (
                            "buffer_offset",
                            "time_length",
                            "sample_rate",
                            "pcm_byte_length",
                            "bits_per_sample",
                        ):
                            if k in child:
                                summary[k] = child[k]
                    elif cid == "cmpi":
                        for k in (
                            "codec_id",
                            "codec_name",
                            "target_codec_id",
                            "target_codec_name",
                            "packet_size",
                            "compression_total_size",
                        ):
                            if k in child:
                                summary[k] = child[k]
                    elif cid == "data":
                        summary["data_offset"] = child.get("offset", 0) + 8
                        summary["data_size"] = child.get("size")
                        if child.get("ogg_offset") is not None:
                            summary["ogg_offset"] = child["ogg_offset"]
                            summary["ogg_size"] = child["ogg_size"]
                samples.append(summary)
            children = node.get("children")
            if isinstance(children, list):
                visit(children)

    visit(nodes)
    return samples


def inspect_isb(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF":
        raise IsbParseError("not a RIFF file")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if data[8:12] != b"isbf":
        raise IsbParseError(f"unexpected form type {data[8:12]!r}")
    end = min(len(data), riff_size + 8)
    chunks = build_chunk_tree(data, 12, end)
    coverage = collect_decode_coverage(chunks)
    chunk_counts = collect_chunk_counts(chunks)
    samples = collect_sample_summaries(chunks)
    return {
        "file": str(path),
        "size": len(data),
        "riff_size": riff_size,
        "chunk_counts": chunk_counts,
        "sample_count": len(samples),
        "samples": samples,
        "decode_coverage": coverage,
        "chunks": chunks,
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
    parser.add_argument(
        "input",
        help="Path to a .isb file or a directory containing .isb files",
    )
    parser.add_argument(
        "--glob",
        default=DEFAULT_GLOB,
        help="Glob pattern used when input is a directory",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output/audio/inspect_isb"),
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
    summary: dict[str, object] = {"banks": [], "failed": []}

    for path in files:
        try:
            report = inspect_isb(path)
        except Exception as exc:
            print(f"[FAIL] {path}: {exc}", file=sys.stderr)
            summary["failed"].append({"file": str(path), "error": str(exc)})
            continue

        report_path = out_root / f"{output_stem_for_path(path, input_path)}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary["banks"].append(
            {
                "file": str(path),
                "report": str(report_path),
                "sample_count": report["sample_count"],
                "decode_status_counts": report["decode_coverage"]["status_counts"],
                "decode_status_bytes": report["decode_coverage"]["status_bytes"],
            }
        )
        print(f"[OK] {path.name}: {report['sample_count']} samples")

    summary_path = out_root / "banks.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Wrote summary to {summary_path}")
    return 0 if not summary["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
