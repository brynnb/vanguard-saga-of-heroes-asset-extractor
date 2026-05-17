#!/usr/bin/env python3
"""Extract embedded WAV payloads from Vanguard `.uax` packages.

This walks Unreal `Sound` exports, reuses the UAX payload classifier, and
writes only exports that contain embedded RIFF/WAVE data. Wrapper-only `Sound`
exports are cataloged but not extracted.

Usage:
    python3 scripts/extractors/extract_uax_wav.py /path/to/file.uax
    python3 scripts/extractors/extract_uax_wav.py /path/to/Assets --glob '*.uax'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.extractors.inspect_uax import (  # noqa: E402
    classify_sound_export_payload,
    collect_inputs,
    output_stem_for_path,
)
from ue2.package import UE2Package  # noqa: E402


DEFAULT_GLOB = "*.uax"
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str) -> str:
    cleaned = SAFE_NAME_RE.sub("_", name).strip("._")
    return cleaned or "sound"


def build_output_name(export_index: int, object_name: str) -> str:
    return f"{export_index:04d}_{sanitize_filename(object_name)}.wav"


def extract_package(path: Path, out_root: Path, input_root: Path, dry_run: bool) -> dict[str, object]:
    pkg = UE2Package(str(path))
    package_dir = out_root / output_stem_for_path(path, input_root)
    package_dir.mkdir(parents=True, exist_ok=True)

    extracted_exports: list[dict[str, object]] = []
    skipped_exports: list[dict[str, object]] = []

    for export in pkg.exports:
        if export["class_name"] != "Sound":
            continue

        data = pkg.get_export_data(export)
        payload_info = classify_sound_export_payload(export, data)
        payload_kind = payload_info.get("payload_kind")

        if payload_kind != "embedded-wav":
            skipped_exports.append(
                {
                    "index": export["index"],
                    "object_name": export["object_name"],
                    "payload_kind": payload_kind,
                }
            )
            continue

        riff_offset = payload_info.get("riff_offset")
        if not isinstance(riff_offset, int):
            skipped_exports.append(
                {
                    "index": export["index"],
                    "object_name": export["object_name"],
                    "payload_kind": payload_kind,
                    "reason": "missing-riff-offset",
                }
            )
            continue

        wav_bytes = data[riff_offset:]
        output_name = build_output_name(export["index"], export["object_name"])
        output_path = package_dir / output_name
        if not dry_run:
            output_path.write_bytes(wav_bytes)

        extracted_exports.append(
            {
                "index": export["index"],
                "object_name": export["object_name"],
                "serial_offset": export["serial_offset"],
                "serial_size": export["serial_size"],
                "riff_offset": riff_offset,
                "header_prefix_size": payload_info.get("header_prefix_size"),
                "header_prefix_layout": payload_info.get("header_prefix_layout"),
                "prefix_end_offset": payload_info.get("prefix_end_offset"),
                "prefix_end_offset_matches_export_end": payload_info.get(
                    "prefix_end_offset_matches_export_end"
                ),
                "wav_size": len(wav_bytes),
                "output_name": output_name,
                "output_path": str(output_path),
            }
        )

    report = {
        "file": str(path),
        "version": pkg.version,
        "licensee": pkg.licensee,
        "export_count": len(pkg.exports),
        "embedded_wav_count": len(extracted_exports),
        "skipped_sound_exports": len(skipped_exports),
        "embedded_wav_exports": extracted_exports,
        "skipped_exports": skipped_exports,
    }

    report_path = package_dir / "manifest.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return {
        "file": str(path),
        "report": str(report_path),
        "embedded_wav_count": len(extracted_exports),
        "skipped_sound_exports": len(skipped_exports),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Path to a .uax file or a directory containing .uax files")
    parser.add_argument(
        "--glob",
        default=DEFAULT_GLOB,
        help="Glob pattern used when input is a directory",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output/audio/full_uax_wav"),
        help="Output directory for extracted WAV payloads and manifests",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write manifests without extracting WAV files",
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
    summary: dict[str, object] = {"packages": [], "failed": []}

    for path in files:
        try:
            package_summary = extract_package(path, out_root, input_path, args.dry_run)
        except Exception as exc:
            print(f"[FAIL] {path}: {exc}", file=sys.stderr)
            summary["failed"].append({"file": str(path), "error": str(exc)})
            continue

        summary["packages"].append(package_summary)
        if package_summary["embedded_wav_count"]:
            print(
                f"[OK] {path.name}: extracted {package_summary['embedded_wav_count']} embedded WAV exports"
            )

    summary_path = out_root / "packages.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Wrote summary to {summary_path}")
    return 0 if not summary["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())