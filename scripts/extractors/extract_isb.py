#!/usr/bin/env python3
"""Extract Ogg samples from Vanguard ISACT .isb sound banks.

This is a first-pass extractor for RIFF `isbf` banks. It walks `LIST(samp)`
entries, extracts the embedded Ogg Vorbis `data` payloads, and writes a JSON
manifest describing the recovered samples.

Usage:
    python3 scripts/extractors/extract_isb.py /path/to/file.isb
    python3 scripts/extractors/extract_isb.py /path/to/Assets/Music --glob '*.isb'
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


OGGS_MAGIC = b"OggS"
DEFAULT_GLOB = "*.isb"
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


def align2(value: int) -> int:
    return value + (value & 1)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip()
    cleaned = cleaned.rstrip(".")
    return cleaned or "sample"


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


def read_u32(payload: bytes, offset: int) -> int | None:
    if offset + 4 > len(payload):
        return None
    return struct.unpack_from("<I", payload, offset)[0]


def read_u16(payload: bytes, offset: int) -> int | None:
    if offset + 2 > len(payload):
        return None
    return struct.unpack_from("<H", payload, offset)[0]


def read_f32(payload: bytes, offset: int) -> float | None:
    if offset + 4 > len(payload):
        return None
    return struct.unpack_from("<f", payload, offset)[0]


@dataclass
class SampleEntry:
    index: int
    bank_name: str
    bank_path: str
    folder_name: str = ""
    title: str = ""
    ogg_offset: int = -1
    ogg_size: int = 0
    data_offset: int = -1
    data_size: int = 0
    soff_offset: int | None = None
    channels: int | None = None
    sample_rate: int | None = None
    buffer_offset: int | None = None
    time_length: int | None = None
    pcm_byte_length: int | None = None
    bits_per_sample: int | None = None
    codec_id: int | None = None
    codec_name: str = "unknown"
    target_codec_id: int | None = None
    target_codec_name: str = "unknown"
    compression_total_size: int | None = None
    packet_size: int | None = None
    compression_ratio: float | None = None
    compression_quality: float | None = None
    duration_seconds: float | None = None
    output_name: str = ""
    output_path: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        if self.folder_name and self.title:
            return f"{self.folder_name}/{self.title}"
        if self.title:
            return self.title
        if self.folder_name:
            return self.folder_name
        return f"sample_{self.index:03d}"


class IsbParseError(ValueError):
    pass


def compute_duration_seconds(sample: SampleEntry) -> float | None:
    if not sample.sample_rate or not sample.channels or not sample.bits_per_sample or not sample.pcm_byte_length:
        return None
    bytes_per_sample = sample.bits_per_sample / 8
    if bytes_per_sample <= 0:
        return None
    duration = sample.pcm_byte_length / sample.channels / bytes_per_sample / sample.sample_rate
    return round(duration, 6) if duration > 0 else None


def parse_chnk_payload(payload: bytes, sample: SampleEntry) -> None:
    sample.channels = read_u32(payload, 0)


def parse_sinf_payload(payload: bytes, sample: SampleEntry) -> None:
    sample.buffer_offset = read_u32(payload, 0)
    sample.time_length = read_u32(payload, 4)
    sample.sample_rate = read_u32(payload, 8)
    sample.pcm_byte_length = read_u32(payload, 12)
    sample.bits_per_sample = read_u16(payload, 16)


def parse_cmpi_payload(payload: bytes, sample: SampleEntry) -> None:
    sample.codec_id = read_u32(payload, 0)
    sample.codec_name = codec_name(sample.codec_id)
    sample.target_codec_id = read_u32(payload, 4)
    sample.target_codec_name = codec_name(sample.target_codec_id)
    sample.compression_total_size = read_u32(payload, 8)
    sample.packet_size = read_u32(payload, 12)
    sample.compression_ratio = read_f32(payload, 16)
    sample.compression_quality = read_f32(payload, 20)


def parse_soff_payload(payload: bytes, sample: SampleEntry) -> None:
    sample.soff_offset = read_u32(payload, 0)


def iter_riff_chunks(data: bytes, start: int, end: int) -> Iterable[tuple[str, int, int, bytes]]:
    offset = start
    while offset + 8 <= end:
        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        if payload_end > len(data) or payload_end > end:
            raise IsbParseError(
                f"Chunk {chunk_id!r} at 0x{offset:x} overruns container: {chunk_size} bytes"
            )
        yield chunk_id.decode("latin-1", errors="replace"), offset, chunk_size, data[payload_start:payload_end]
        offset = payload_end + (chunk_size & 1)


def parse_sample_payload(payload: bytes, sample: SampleEntry, payload_base_offset: int) -> SampleEntry:
    for chunk_id, offset, chunk_size, chunk_payload in iter_riff_chunks(payload, 4, len(payload)):
        if chunk_id == "titl":
            sample.title = decode_text(chunk_payload)
        elif chunk_id == "chnk":
            parse_chnk_payload(chunk_payload, sample)
        elif chunk_id == "sinf":
            parse_sinf_payload(chunk_payload, sample)
        elif chunk_id == "cmpi":
            parse_cmpi_payload(chunk_payload, sample)
        elif chunk_id == "soff":
            parse_soff_payload(chunk_payload, sample)
        elif chunk_id == "data":
            sample.data_offset = payload_base_offset + offset + 8
            sample.data_size = chunk_size
            ogg_rel = chunk_payload.find(OGGS_MAGIC)
            if ogg_rel < 0:
                if sample.codec_id in (None, 2):
                    sample.warnings.append("data chunk missing OggS header")
                else:
                    sample.warnings.append(f"{sample.codec_name} data is metadata-only; extractor writes Ogg payloads")
            else:
                sample.ogg_offset = payload_base_offset + offset + 8 + ogg_rel
                sample.ogg_size = chunk_size - ogg_rel
    if sample.codec_id is not None:
        sample.codec_name = codec_name(sample.codec_id)
    if sample.target_codec_id is not None:
        sample.target_codec_name = codec_name(sample.target_codec_id)
    sample.duration_seconds = compute_duration_seconds(sample)
    if sample.data_offset < 0:
        sample.warnings.append("sample missing data chunk")
    return sample


def parse_list_payload(
    payload: bytes,
    bank_path: Path,
    bank_name: str,
    sample_index: int = 0,
    folder_name: str = "",
    payload_base_offset: int = 0,
) -> list[SampleEntry]:
    if len(payload) < 4:
        raise IsbParseError("LIST payload is too short")

    list_type = payload[:4].decode("latin-1", errors="replace")
    entries: list[SampleEntry] = []

    if list_type == "samp":
        sample = SampleEntry(index=sample_index, bank_name=bank_name, bank_path=str(bank_path), folder_name=folder_name)
        entries.append(parse_sample_payload(payload, sample, payload_base_offset=payload_base_offset))
        return entries

    local_folder_name = folder_name
    if list_type == "fldr":
        for chunk_id, chunk_offset, _, chunk_payload in iter_riff_chunks(payload, 4, len(payload)):
            if chunk_id == "titl" and not local_folder_name:
                local_folder_name = decode_text(chunk_payload)
            elif chunk_id == "LIST":
                nested_entries = parse_list_payload(
                    chunk_payload,
                    bank_path=bank_path,
                    bank_name=bank_name,
                    sample_index=sample_index + len(entries),
                    folder_name=local_folder_name,
                    payload_base_offset=payload_base_offset + chunk_offset + 8,
                )
                entries.extend(nested_entries)
        return entries

    return entries


def parse_isb_file(path: Path) -> list[SampleEntry]:
    data = path.read_bytes()
    if len(data) < 12:
        raise IsbParseError("File too small for RIFF header")
    if data[:4] != b"RIFF":
        raise IsbParseError(f"Expected RIFF header, found {data[:4]!r}")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size + 8 > len(data):
        raise IsbParseError(f"RIFF size {riff_size} exceeds file length {len(data)}")
    if data[8:12] != b"isbf":
        raise IsbParseError(f"Expected isbf form type, found {data[8:12]!r}")

    bank_name = path.stem
    samples: list[SampleEntry] = []
    for chunk_id, chunk_offset, _, chunk_payload in iter_riff_chunks(data, 12, min(len(data), riff_size + 8)):
        if chunk_id != "LIST":
            continue
        nested = parse_list_payload(
            chunk_payload,
            bank_path=path,
            bank_name=bank_name,
            sample_index=len(samples),
            payload_base_offset=chunk_offset + 8,
        )
        samples.extend(nested)

    for index, sample in enumerate(samples, start=1):
        sample.index = index
    return samples


def build_output_name(sample: SampleEntry) -> str:
    base = sanitize_filename(sample.display_name)
    return f"{sample.index:03d}_{base}.ogg"


def write_samples(data: bytes, out_dir: Path, samples: list[SampleEntry], dry_run: bool) -> None:
    for sample in samples:
        sample.output_name = build_output_name(sample)
        sample_path = out_dir / sample.output_name
        sample.output_path = str(sample_path)
        if sample.ogg_offset < 0 or sample.ogg_size <= 0 or dry_run:
            continue
        sample_path.write_bytes(data[sample.ogg_offset : sample.ogg_offset + sample.ogg_size])


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


def manifest_for_bank(bank_path: Path, output_dir: Path, samples: list[SampleEntry]) -> dict:
    warnings = [warning for sample in samples for warning in sample.warnings]
    return {
        "bank": bank_path.name,
        "bank_path": str(bank_path),
        "sample_count": len(samples),
        "output_dir": str(output_dir),
        "warnings": warnings,
        "samples": [
            {
                "index": sample.index,
                "display_name": sample.display_name,
                "folder_name": sample.folder_name,
                "title": sample.title,
                "ogg_offset": sample.ogg_offset,
                "ogg_size": sample.ogg_size,
                "data_offset": sample.data_offset,
                "data_size": sample.data_size,
                "soff_offset": sample.soff_offset,
                "channels": sample.channels,
                "sample_rate": sample.sample_rate,
                "buffer_offset": sample.buffer_offset,
                "time_length": sample.time_length,
                "pcm_byte_length": sample.pcm_byte_length,
                "bits_per_sample": sample.bits_per_sample,
                "codec_id": sample.codec_id,
                "codec_name": sample.codec_name,
                "target_codec_id": sample.target_codec_id,
                "target_codec_name": sample.target_codec_name,
                "compression_total_size": sample.compression_total_size,
                "packet_size": sample.packet_size,
                "compression_ratio": sample.compression_ratio,
                "compression_quality": sample.compression_quality,
                "duration_seconds": sample.duration_seconds,
                "output_name": sample.output_name,
                "output_path": sample.output_path,
                "warnings": sample.warnings,
            }
            for sample in samples
        ],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to an .isb file or a directory containing .isb files")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output/audio/isb"),
        help="Output directory for extracted samples and manifests",
    )
    parser.add_argument(
        "--glob",
        default=DEFAULT_GLOB,
        help="Glob pattern used when input is a directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse files and write manifests without extracting .ogg payloads",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    input_path = Path(args.input).expanduser().resolve()
    out_root = args.out.expanduser().resolve()

    try:
        banks = collect_inputs(input_path, args.glob)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    if not banks:
        print(f"No files matched {args.glob!r} under {input_path}", file=sys.stderr)
        return 1

    out_root.mkdir(parents=True, exist_ok=True)
    summaries: list[dict] = []
    failed = 0

    for bank_path in banks:
        try:
            data = bank_path.read_bytes()
            samples = parse_isb_file(bank_path)
        except (OSError, IsbParseError) as exc:
            print(f"[FAIL] {bank_path}: {exc}", file=sys.stderr)
            failed += 1
            continue

        bank_out_dir = out_root / output_stem_for_path(bank_path, input_path)
        bank_out_dir.mkdir(parents=True, exist_ok=True)
        write_samples(data, bank_out_dir, samples, dry_run=args.dry_run)

        manifest = manifest_for_bank(bank_path, bank_out_dir, samples)
        manifest_path = bank_out_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        summaries.append(
            {
                "bank": bank_path.name,
                "sample_count": len(samples),
                "manifest": str(manifest_path),
            }
        )
        print(f"[OK] {bank_path.name}: {len(samples)} samples")

    summary_path = out_root / "banks.json"
    summary_path.write_text(json.dumps({"banks": summaries, "failed": failed}, indent=2), encoding="utf-8")
    print(f"Wrote summary to {summary_path}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())