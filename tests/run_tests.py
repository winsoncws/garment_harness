#!/usr/bin/env python3
"""
Run every test. No pytest required.

    python3 tests/run_tests.py           all
    python3 tests/run_tests.py -v        verbose
    python3 tests/run_tests.py contracts just that module

Tests always run OFFLINE (the fake model), so they need no GPU, no network
and finish in under a second. That is deliberate: a test suite you can run
on every save is worth ten times one you run at the end of the day.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GMT_OFFLINE", "1")
os.environ.setdefault("GMT_LOG_LEVEL", "ERROR")
os.environ.setdefault("GMT_DB", "/tmp/gmt_test.db")

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbosity = 2 if "-v" in sys.argv else 1

    loader = unittest.TestLoader()
    if args:
        suite = unittest.TestSuite(
            loader.loadTestsFromName(f"tests.test_{a}") for a in args)
    else:
        suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py",
                                top_level_dir=str(ROOT))

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
