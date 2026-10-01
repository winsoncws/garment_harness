"""
gmt -- the GarMenT agentic harness.

    from gmt import run, run_many, get_store

Everything else is one import deeper (gmt.core.*, gmt.tasks.*, gmt.pipelines.*).
Start with README.md, then docs/01_quickstart.md.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .core.router import run                                   # noqa: F401
from .core.runner import (Batch, ChainResult, Step, chain,     # noqa: F401
                          map_parallel, map_sync, progress)
from .core.store import get_store                              # noqa: F401
from .core import registry                                     # noqa: F401

run_many = map_sync          # friendly alias for the common case

__all__ = ["run", "run_many", "map_sync", "map_parallel", "chain", "Step",
           "Batch", "ChainResult", "progress", "get_store", "registry"]
