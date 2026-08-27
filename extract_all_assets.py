#!/usr/bin/env python3
"""Compatibility wrapper for the packaged extraction pipeline."""

from vanguard_assets.pipeline import main


if __name__ == "__main__":
    raise SystemExit(main())
