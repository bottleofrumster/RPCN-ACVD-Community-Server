#!/usr/bin/env python3
"""Compatibility entry point for tools.migration.extract_ac_snapshot."""

import sys
from pathlib import Path

# Keep direct script invocation working from outside the repository directory.
_repository = str(Path(__file__).resolve().parents[1])
if _repository not in sys.path:
    sys.path.insert(0, _repository)

from tools.migration import extract_ac_snapshot as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    # One module object preserves function globals and monkeypatch behavior.
    sys.modules[__name__] = _implementation
