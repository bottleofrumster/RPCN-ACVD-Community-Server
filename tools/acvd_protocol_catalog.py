#!/usr/bin/env python3
"""Compatibility entry point for tools.analysis.protocol_catalog."""

import sys
from pathlib import Path

_repository = str(Path(__file__).resolve().parents[1])
if _repository not in sys.path:
    sys.path.insert(0, _repository)

from tools.analysis import protocol_catalog as _implementation


def __getattr__(name):
    # Read-only importlib callers may retain the wrapper module object.
    return getattr(_implementation, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_implementation)))


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
