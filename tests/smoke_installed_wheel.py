#!/usr/bin/env python3
"""Install a built wheel in isolation and exercise its public CLI boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_installed_wheel.py /path/to/package.whl")

    wheel = Path(sys.argv[1]).resolve()
    if not wheel.is_file():
        raise SystemExit(f"wheel not found: {wheel}")

    temporary_root = Path(os.environ.get("RUNNER_TEMP", "/var/tmp"))
    with tempfile.TemporaryDirectory(
        prefix="vanguard-assets-wheel-", dir=temporary_root
    ) as temporary:
        root = Path(temporary)
        environment = root / "venv"
        workspace = root / "workspace"
        assets = workspace / "emu" / "Assets"
        assets.mkdir(parents=True)
        venv.EnvBuilder(with_pip=True).create(environment)

        python = environment / "bin" / "python"
        executable = environment / "bin" / "vanguard-assets"
        run(
            [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
            cwd=workspace,
        )
        run([str(executable), "--help"], cwd=workspace)
        run(
            [
                str(executable),
                "extract-all",
                "--assets",
                str(assets),
                "--emu-root",
                str(assets.parent),
                "--sections",
                "audio",
                "--skip-unreal-library",
                "--dry-run",
            ],
            cwd=workspace,
        )
        run(
            [
                str(python),
                "-c",
                "from importlib.resources import files; "
                "assert files('client_tables').joinpath('vgo_world_npc_snapshot.json').is_file()",
            ],
            cwd=workspace,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
