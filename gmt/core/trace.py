"""
trace.py -- console logging. Structured, quiet by default, no dependencies.
=======================================================================
The *durable* trace lives in the `task_run` table (see store.py). This
module is only the human-facing stream while a job runs.

Levels:  DEBUG < INFO < WARN < ERROR.  Set with GMT_LOG_LEVEL=DEBUG.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from . import config

_ORDER = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_COLOR = {"DEBUG": "\033[2m", "INFO": "", "WARN": "\033[33m", "ERROR": "\033[31m"}
_RESET = "\033[0m"
_USE_COLOR = sys.stderr.isatty()


def _threshold() -> int:
    return _ORDER.get(str(config.load().get("logging.level", "INFO")).upper(), 20)


def log(level: str, msg: str, **kv: Any) -> None:
    if _ORDER.get(level, 20) < _threshold():
        return
    extra = " ".join(f"{k}={v}" for k, v in kv.items())
    line = f"{time.strftime('%H:%M:%S')} {level:<5} {msg}" + (f"  {extra}" if extra else "")
    if _USE_COLOR and _COLOR.get(level):
        line = f"{_COLOR[level]}{line}{_RESET}"
    print(line, file=sys.stderr)


def debug(msg: str, **kv: Any) -> None: log("DEBUG", msg, **kv)
def info(msg: str, **kv: Any) -> None:  log("INFO", msg, **kv)
def warn(msg: str, **kv: Any) -> None:  log("WARN", msg, **kv)
def error(msg: str, **kv: Any) -> None: log("ERROR", msg, **kv)
