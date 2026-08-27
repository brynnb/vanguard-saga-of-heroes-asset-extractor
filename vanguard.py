#!/usr/bin/env python3
"""Compatibility wrapper for the packaged Vanguard asset CLI."""

from vanguard_assets.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
