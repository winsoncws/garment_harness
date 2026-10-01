"""
config.py -- load config/*.yaml, apply environment overrides.
=======================================================================

WHERE SETTINGS COME FROM (later wins)
-------------------------------------

    config/*.yaml   -->   environment variables   -->   code arguments
    (the defaults)        (per-machine: your GPU        (per-call override)
                           server vs your laptop)

Why that order: the YAML is committed to git and is the same for everyone.
The env vars are what differs between your Mac and the GPU box (Ollama host,
GPU device, DB path). Code arguments win last so a test can force anything.

ENV VARS WE READ
----------------
| Variable          | Overrides            | Example                     |
|-------------------|----------------------|-----------------------------|
| GMT_OLLAMA_HOST   | ollama.host          | http://192.168.1.50:11434   |
| GMT_DB            | store.path           | /data/garment.db            |
| GMT_MAX_TIER      | routing.max_tier     | 2   (never call the cloud)  |
| GMT_OFFLINE       | forces the fake LLM  | 1                           |
| GMT_LOG_LEVEL     | logging verbosity    | DEBUG                       |
| ANTHROPIC_API_KEY | tier-3 credentials   | sk-ant-...                  |

A NOTE ON THE YAML PARSER
-------------------------
If PyYAML is installed we use it. If not, we fall back to `_mini_yaml`
below, which understands the small subset this repo's config files use:
nested maps, lists of scalars, lists of maps, `#` comments, and
str/int/float/bool/null scalars. It does NOT understand anchors, multi-line
blocks, or flow style. If you start writing fancy YAML, just:

    pip install pyyaml

and this file will silently start using the real thing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# The repo root == the folder that contains `config/`, `data/`, `gmt/`.
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


# ---------------------------------------------------------------------------
# YAML loading (real PyYAML if present, tiny fallback if not)
# ---------------------------------------------------------------------------

try:                                   # pragma: no cover - depends on machine
    import yaml as _pyyaml
except ImportError:
    _pyyaml = None


def load_yaml(path: Path) -> Dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    if _pyyaml is not None:
        return _pyyaml.safe_load(text) or {}
    return _mini_yaml(text)


_DASH = object()          # marker for a "- " sequence entry


def _scalar(tok: str) -> Any:
    """Turn one YAML scalar token into a Python value."""
    t = tok.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]                      # quoted string, keep as-is
    if t[:1] in "{[" and t[-1:] in "}]":     # inline flow: {} [] {start: 1, max: 2}
        try:
            return json.loads(t)            # strict JSON first
        except json.JSONDecodeError:
            return _flow(t)                 # then relaxed YAML flow
    low = t.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    for cast in (int, float):
        try:
            return cast(t)
        except ValueError:
            pass
    return t                                # plain string ("30m", a URL, ...)


def _split_top(s: str) -> List[str]:
    """Split on commas that are not inside quotes or nested brackets."""
    parts, depth, quote, buf = [], 0, "", []
    for ch in s:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return [p.strip() for p in parts]


def _flow(t: str) -> Any:
    """Relaxed one-line flow collection, e.g. `{ start: 1, max: 2 }`.

    Real YAML allows unquoted keys here; JSON does not. This is the only
    place the two disagree in the configs this repo ships.
    """
    inner = t[1:-1].strip()
    if t[0] == "[":
        return [_scalar(x) for x in _split_top(inner)]
    out: Dict[str, Any] = {}
    for part in _split_top(inner):
        k, _, v = part.partition(":")
        out[k.strip().strip("\"'")] = _scalar(v)
    return out


def _tokenize(text: str) -> List[Any]:
    """Lines -> [(indent, body)] with comments and blanks removed.

    A "- foo: bar" line is rewritten into TWO rows: a _DASH marker at the
    dash's indent, plus "foo: bar" indented two further. That single trick
    is what lets the parser below treat lists-of-maps as ordinary maps.
    """
    rows: List[Any] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = raw.split(" #", 1)[0].rstrip() if " #" in raw else raw.rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if body == "-":
            rows.append((indent, _DASH))
        elif body.startswith("- "):
            rest = body[2:].strip()
            rows.append((indent, _DASH))
            if ":" in rest and rest[:1] not in "\"'{[":
                rows.append((indent + 2, rest))            # a map entry
            else:
                rows.append((indent + 2, ("=", _scalar(rest))))   # a scalar
        else:
            rows.append((indent, body))
    return rows


def _parse(rows: List[Any], i: int, indent: int) -> Any:
    """Recursive descent. Returns (value, next_row_index)."""
    # ---- a sequence -------------------------------------------------------
    if i < len(rows) and rows[i][1] is _DASH:
        seq: List[Any] = []
        while i < len(rows) and rows[i][0] == indent and rows[i][1] is _DASH:
            i += 1
            if i < len(rows) and rows[i][0] > indent:
                child, i = _parse(rows, i, rows[i][0])
                seq.append(child)
            else:
                seq.append(None)
        return seq, i

    # ---- a mapping --------------------------------------------------------
    out: Dict[str, Any] = {}
    while i < len(rows) and rows[i][0] == indent:
        ind, body = rows[i]
        if isinstance(body, tuple) and body[0] == "=":
            return body[1], i + 1          # bare scalar list item
        key, _, val = str(body).partition(":")
        key, val = key.strip(), val.strip()
        i += 1
        if val:
            out[key] = _scalar(val)
        elif i < len(rows) and rows[i][0] > ind:
            out[key], i = _parse(rows, i, rows[i][0])
        else:
            out[key] = None
    return out, i


def _mini_yaml(text: str) -> Dict[str, Any]:
    rows = _tokenize(text)
    if not rows:
        return {}
    value, _ = _parse(rows, 0, rows[0][0])
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Config object
# ---------------------------------------------------------------------------

class Config:
    """Dotted read-only access over the merged config: cfg.get("ollama.host")."""

    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> Dict[str, Any]:
        return dict(self.get(name, {}) or {})

    def as_dict(self) -> Dict[str, Any]:
        return self._data

    def __repr__(self) -> str:
        return f"Config(sections={sorted(self._data)})"


_CACHE: Optional[Config] = None


def load(reload: bool = False) -> Config:
    """Load and merge config/settings.yaml + models.yaml + routing.yaml."""
    global _CACHE
    if _CACHE is not None and not reload:
        return _CACHE

    data: Dict[str, Any] = {}
    for name in ("settings.yaml", "models.yaml", "routing.yaml"):
        p = CONFIG_DIR / name
        if p.exists():
            data.update(load_yaml(p))

    # ---- environment overrides -------------------------------------------
    env = os.environ
    if env.get("GMT_OLLAMA_HOST"):
        data.setdefault("ollama", {})["host"] = env["GMT_OLLAMA_HOST"]
    if env.get("GMT_DB"):
        data.setdefault("store", {})["path"] = env["GMT_DB"]
    if env.get("GMT_MAX_TIER"):
        data.setdefault("routing", {})["max_tier"] = int(env["GMT_MAX_TIER"])
    if env.get("GMT_LOG_LEVEL"):
        data.setdefault("logging", {})["level"] = env["GMT_LOG_LEVEL"]
    data.setdefault("runtime", {})["offline"] = env.get("GMT_OFFLINE", "") == "1"

    # ---- resolve relative paths against ROOT ------------------------------
    store = data.setdefault("store", {})
    store["path"] = str((ROOT / store.get("path", "data/garment.db")).resolve()
                        if not os.path.isabs(store.get("path", "data/garment.db"))
                        else store["path"])

    _CACHE = Config(data)
    return _CACHE
