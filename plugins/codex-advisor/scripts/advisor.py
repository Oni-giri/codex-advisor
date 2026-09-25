#!/usr/bin/env python3
"""Run directly from a plugin checkout; installation needs no pip dependencies."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from advisor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
