#!/usr/bin/env python3
"""
Dump binaryprefabs.sgo into a single JSONL file preserving every byte.

Every mini-package becomes one JSON line with:
  * file_offset, byte_size
  * header (signature, version, etc.)
  * guid (hex), generations
  * names[], imports[], exports[]
  * each export's decoded properties (with raw_hex fallback)
  * matched trailer entry (file_offset, timestamp, hashes, name)

Output: output/data/sgo_raw.jsonl (~150-200 MB expected)
"""
import argparse
import json
import os
import struct
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJ)

from scripts.lib.sgo_parser import (  # noqa: E402
    SGO_MAGIC,
    SGO_VERSION,
    find_mini_package_boundaries,
    parse_mini_package,
    parse_trailer,
)


def _default_sgo() -> str:
    try:
        import config
        p = getattr(config, "SGO_PATH", None)
        if p:
            return p
    except ImportError:
        pass
    return os.path.expanduser("~/Downloads/Vanguard EMU/Assets/Archives/binaryprefabs.sgo")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sgo", default=_default_sgo())
    ap.add_argument("--out", default=os.path.join(PROJ, "output/data/sgo_raw.jsonl"))
    ap.add_argument("--limit", type=int, default=0,
                    help="If >0, only dump the first N mini-packages (debug)")
    args = ap.parse_args()

    if not os.path.isfile(args.sgo):
        print(f"ERROR: SGO not found: {args.sgo}", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    print(f"Loading {args.sgo} ...")
    with open(args.sgo, "rb") as fh:
        data = fh.read()

    if data[:4] != SGO_MAGIC:
        print(f"ERROR: bad magic {data[:4]!r}", file=sys.stderr)
        return 1
    version = struct.unpack_from("<I", data, 4)[0]
    if version != SGO_VERSION:
        print(f"WARNING: unexpected version {version}", file=sys.stderr)

    boundaries = find_mini_package_boundaries(data)
    print(f"  {len(data):,} bytes, {len(boundaries):,} mini-packages")

    # The final 4 bytes of the file are a u32 = trailer directory size, so
    # the trailer starts at (total - 4 - directory_size).
    directory_size = struct.unpack_from("<I", data, len(data) - 4)[0]
    trailer_start = len(data) - 4 - directory_size
    trailer_entries, trailer_footer = parse_trailer(data, trailer_start)
    print(f"  trailer: {len(trailer_entries):,} entries @ {trailer_start:,}")

    # Index trailer entries by file_offset for fast lookup.
    trailer_by_offset = {te.file_offset: te for te in trailer_entries}

    total = len(boundaries)
    if args.limit and args.limit > 0:
        total = min(total, args.limit)

    t0 = time.time()
    n_pkgs = 0
    n_exports = 0
    n_props = 0
    n_bytes_consumed = 0
    n_bytes_serial = 0

    with open(args.out, "w", encoding="utf-8") as out:
        for i in range(total):
            pkg_start = boundaries[i]
            pkg_end = boundaries[i + 1] if (i + 1) < len(boundaries) else trailer_start
            try:
                mp = parse_mini_package(data, pkg_start, pkg_end)
            except Exception as exc:
                print(f"  pkg {i} @ {pkg_start}: parse error: {exc}", file=sys.stderr)
                continue

            te = trailer_by_offset.get(pkg_start)
            record = {
                "index": i,
                "file_offset": pkg_start,
                "byte_size": pkg_end - pkg_start,
                "header": {
                    "version": mp.header.version,
                    "licensee": mp.header.licensee,
                    "name_count": mp.header.name_count,
                    "name_offset": mp.header.name_offset,
                    "export_count": mp.header.export_count,
                    "export_offset": mp.header.export_offset,
                    "import_count": mp.header.import_count,
                    "import_offset": mp.header.import_offset,
                    "guid": mp.header.guid.hex(),
                    "generations": [
                        {"export_count": ec, "name_count": nc}
                        for ec, nc in mp.header.generations
                    ],
                },
                "names": mp.names,
                "imports": [
                    {"class_package": imp.class_package,
                     "class_name": imp.class_name,
                     "package_idx": imp.package_idx,
                     "name": imp.object_name}
                    for imp in mp.imports
                ],
                "exports": [],
                "trailer_entry": None if te is None else {
                    "hash_a": te.hash_a,
                    "hash_b": te.hash_b,
                    "timestamp": te.timestamp,
                    "size_a": te.size_a,
                    "cumulative": te.cumulative,
                    "name": te.name,
                },
            }

            for exp, props, consumed in mp.export_props:
                rec_exp = {
                    "class": exp.class_ref,
                    "name": exp.object_name,
                    "flags": exp.flags,
                    "serial_size": exp.serial_size,
                    "serial_offset": exp.serial_offset,
                    "bytes_consumed": consumed,
                    "props": [p.to_json() for p in props],
                }
                record["exports"].append(rec_exp)
                n_exports += 1
                n_props += len(props)
                n_bytes_consumed += consumed
                n_bytes_serial += max(exp.serial_size, 0)

            out.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            out.write("\n")
            n_pkgs += 1

            if (i + 1) % 2000 == 0:
                print(f"  wrote {i+1:,}/{total:,} packages "
                      f"({time.time()-t0:.1f}s)")

    dt = time.time() - t0
    size_mb = os.path.getsize(args.out) / (1024 * 1024)
    print(f"\nDone in {dt:.1f}s")
    print(f"  packages: {n_pkgs:,}")
    print(f"  exports:  {n_exports:,}")
    print(f"  props:    {n_props:,}")
    print(f"  serial bytes parsed: {n_bytes_consumed:,} / {n_bytes_serial:,} "
          f"({100.0*n_bytes_consumed/max(n_bytes_serial,1):.2f}%)")
    print(f"  output:   {args.out} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
