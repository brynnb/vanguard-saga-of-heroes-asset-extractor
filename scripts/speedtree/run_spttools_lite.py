#!/usr/bin/env python3
"""Build and run the minimal macOS spttools workflow against Vanguard .spt payloads."""

from __future__ import annotations

import argparse
import filecmp
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPTS_DIR.parent


from vanguard_assets import config
PROJECT_ROOT = config.PROJECT_ROOT
from scripts.speedtree.inspect_speedtree_spt import (
    build_default_dump_path,
    dump_spt_payload,
    find_export,
    find_spt_header,
)
from ue2 import UE2Package


DEFAULT_SPTTOOLS_DIR = PROJECT_ROOT.parent / "spttools"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--export-name",
        help="StaticMesh export name or unique substring inside the package",
    )
    source_group.add_argument(
        "--spt-file",
        help="Path to a standalone .spt file",
    )
    parser.add_argument(
        "--package",
        default=os.path.join(config.ASSETS_PATH, "Meshes", "Ra5000_P1_C1_SpeedTrees_mesh.usx"),
        help="Package containing the SpeedTree export when using --export-name",
    )
    parser.add_argument(
        "--spttools-dir",
        default=str(DEFAULT_SPTTOOLS_DIR),
        help="Path to the local spttools checkout",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force rebuild of sptparser-lite and sptcompiler-lite",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(config.DATA_DIR, "speedtree_spt", "parsed"),
        help="Directory for parsed text and round-trip artifacts",
    )
    parser.add_argument(
        "--roundtrip-check",
        action="store_true",
        help="Recompile the parsed output and compare it to the source .spt",
    )
    return parser.parse_args(argv)


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )


def ensure_spttools_lite_binaries(spttools_dir: Path, rebuild: bool) -> tuple[Path, Path]:
    parser_bin = spttools_dir / "sptparser-lite"
    compiler_bin = spttools_dir / "sptcompiler-lite"

    if rebuild or not parser_bin.exists():
        result = run_command(
            ["cc", "-O2", "-Wall", "-o", str(parser_bin), "sptparser.c", "mylib/buffer.c", "mylib/filesdirs.c", "-lm"],
            spttools_dir,
        )
        if result.returncode != 0:
            raise SystemExit(result.stderr or result.stdout or "Failed to build sptparser-lite")

    if rebuild or not compiler_bin.exists():
        result = run_command(
            ["cc", "-O2", "-Wall", "-o", str(compiler_bin), "sptcompiler.c", "-lm"],
            spttools_dir,
        )
        if result.returncode != 0:
            raise SystemExit(result.stderr or result.stdout or "Failed to build sptcompiler-lite")

    return parser_bin, compiler_bin


def extract_embedded_spt(package_path: Path, export_name: str) -> Path:
    pkg = UE2Package(str(package_path))
    export = find_export(pkg, export_name)
    data = pkg.get_export_data(export)
    start_offset, _version = find_spt_header(data)
    dump_path = Path(build_default_dump_path(export["object_name"], str(package_path)))
    dump_spt_payload(data, start_offset, str(dump_path))
    return dump_path


def parse_with_spttools(parser_bin: Path, spt_path: Path, parsed_path: Path) -> subprocess.CompletedProcess[str]:
    result = run_command([str(parser_bin), str(spt_path)], parser_bin.parent)
    parsed_path.parent.mkdir(parents=True, exist_ok=True)
    parsed_path.write_text(result.stdout, encoding="utf-8")
    return result


def roundtrip_with_spttools(compiler_bin: Path, parsed_path: Path, source_spt_path: Path, roundtrip_path: Path) -> bool:
    result = subprocess.run(
        [str(compiler_bin), str(parsed_path)],
        cwd=str(compiler_bin.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.decode("utf-8", errors="replace") or "Failed to run sptcompiler-lite")
    roundtrip_path.write_bytes(result.stdout)
    return filecmp.cmp(source_spt_path, roundtrip_path, shallow=False)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    spttools_dir = Path(args.spttools_dir).resolve()
    if not spttools_dir.exists():
        raise SystemExit(f"spttools checkout not found: {spttools_dir}")

    parser_bin, compiler_bin = ensure_spttools_lite_binaries(spttools_dir, args.rebuild)

    if args.spt_file:
        spt_path = Path(args.spt_file).resolve()
    else:
        spt_path = extract_embedded_spt(Path(args.package).resolve(), args.export_name)

    if not spt_path.exists():
        raise SystemExit(f"SPT file not found: {spt_path}")

    output_dir = Path(args.output_dir).resolve()
    parsed_path = output_dir / (spt_path.stem + ".parsed")
    parse_result = parse_with_spttools(parser_bin, spt_path, parsed_path)
    vanguard_tail_detected = "Vanguard raw tail detected" in parse_result.stdout

    print(f"source_spt={spt_path}")
    print(f"parsed_output={parsed_path}")
    print(f"parser_exit_code={parse_result.returncode}")
    print(f"vanguard_tail_detected={str(vanguard_tail_detected).lower()}")
    if parse_result.stderr:
        print("parser_stderr=")
        print(parse_result.stderr.rstrip())

    if args.roundtrip_check:
        if parse_result.returncode == 0 and not vanguard_tail_detected:
            roundtrip_path = output_dir / (spt_path.stem + ".roundtrip.spt")
            matches = roundtrip_with_spttools(compiler_bin, parsed_path, spt_path, roundtrip_path)
            print(f"roundtrip_output={roundtrip_path}")
            print(f"roundtrip_matches={str(matches).lower()}")
        elif vanguard_tail_detected:
            print("roundtrip_skipped=vanguard_tail_unparsed")
        else:
            print("roundtrip_skipped=parser_exit_nonzero")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))