#!/usr/bin/env python3
"""Convenient CLI entrypoint from the repository checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "plugins/codex-advisor"))
from advisor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
