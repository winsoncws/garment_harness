"""
contracts.py -- THE BACKBONE. Read this file first.
=======================================================================

WHY THIS FILE EXISTS
--------------------
A 14B quantized model is perfectly capable of doing small jobs. What it is
*not* capable of is reliably remembering to answer in the exact shape you
need. So we never "trust" an LLM. We declare the shape we want, we ask for
it, and then we *check*. If the check fails we retry or escalate.

    prompt  ->  LLM  ->  raw text  ->  parse JSON  ->  VALIDATE  -> accept
                                                         |
                                                         +-- fail -> retry
                                                                     or escalate
                                                                     to a bigger
                                                                     model

That validate step is the single most important line of defence in this
whole repo. It is what makes small local models usable in production.

WHY NOT PYDANTIC?
-----------------
Pydantic is better than this, and you should probably switch to it once the
project is real. It is not used here for exactly one reason: your machine has
zero third-party packages installed, and a beginner-facing kickstart that
fails on `pip install` at step 1 is a bad kickstart. This module is ~200
lines of stdlib and does the 5 things we actually need:

    1. declare a shape            Contract("x", {"a": Field("int")})
    2. validate a dict            c.validate({"a": 1})
    3. COERCE sloppy LLM output   "1" -> 1,  "Yes" -> True,  "  A " -> "A"
    4. emit JSON Schema           for Ollama's structured-output `format=`
    5. emit an example            to paste into the prompt as a template

See docs/05_data_contracts.md for the swap-to-pydantic instructions.

THE TYPE TABLE
--------------
| Field type | Python      | Coerces from                  | JSON Schema  |
|------------|-------------|-------------------------------|--------------|
| "str"      | str         | int/float/bool -> str         | string       |
| "int"      | int         | "12", 12.0, True              | integer      |
| "float"    | float       | "1.5", 2                      | number       |
| "bool"     | bool        | "yes"/"no"/"true"/1/0         | boolean      |
| "list"     | list        | single scalar -> [scalar]     | array        |
| "dict"     | dict        | (no coercion)                 | object       |
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as _dc_field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 1. Field -- one column of the shape
# ---------------------------------------------------------------------------

@dataclass
class Field:
    """One key in a contract.

    Only `type` is mandatory. Everything else narrows what is acceptable,
    and -- just as importantly -- everything else becomes part of the prompt
    we send to the model. A tight Field is a tight prompt.
    """

    type: str = "str"
    desc: str = ""                        # goes into the prompt AND the schema
    required: bool = True
    default: Any = None
    enum: Optional[List[Any]] = None      # closed vocabulary. USE THIS A LOT.
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    item_type: str = "str"                # element type when type == "list"
    max_len: Optional[int] = None         # for str and list
    pattern: Optional[str] = None         # regex, for str

    # -- validation of a single value ---------------------------------------
    def check(self, value: Any) -> Tuple[bool, Any, str]:
        """Return (ok, coerced_value, error_message)."""
        v, err = _coerce(value, self.type, self.item_type)
        if err:
            return False, value, err

        if self.enum is not None:
            match = _enum_match(v, self.enum)
            if match is _MISSING:
                return False, v, f"{v!r} not in {self.enum}"
            v = match

        if self.minimum is not None and isinstance(v, (int, float)) and v < self.minimum:
            return False, v, f"{v} < minimum {self.minimum}"
        if self.maximum is not None and isinstance(v, (int, float)) and v > self.maximum:
            return False, v, f"{v} > maximum {self.maximum}"
        if self.max_len is not None and hasattr(v, "__len__") and len(v) > self.max_len:
            return False, v, f"length {len(v)} > max_len {self.max_len}"
        if self.pattern is not None and isinstance(v, str) and not re.search(self.pattern, v):
            return False, v, f"{v!r} does not match /{self.pattern}/"

        return True, v, ""

    # -- how this field appears in a JSON Schema ----------------------------
    def json_schema(self) -> Dict[str, Any]:
        jtype = {
            "str": "string", "int": "integer", "float": "number",
            "bool": "boolean", "list": "array", "dict": "object",
        }[self.type]
        s: Dict[str, Any] = {"type": jtype}
        if self.desc:
            s["description"] = self.desc
        if self.enum is not None:
            s["enum"] = list(self.enum)
        if self.type == "list":
            s["items"] = {"type": {"str": "string", "int": "integer",
                                   "float": "number", "bool": "boolean",
                                   "dict": "object"}[self.item_type]}
        if self.minimum is not None:
            s["minimum"] = self.minimum
        if self.maximum is not None:
            s["maximum"] = self.maximum
        return s

    # -- a plausible value, used to build the "answer like this" example ----
    def example_value(self) -> Any:
        """A value that SATISFIES this field's own constraints.

        This matters more than it looks: example_json() goes into every
        prompt via prompt_block(). An example that violates its own
        minimum teaches the model to produce output we will then reject.
        """
        if self.default is not None:
            return self.default
        if self.enum:
            return self.enum[0]
        if self.type in ("int", "float"):
            lo = self.minimum if self.minimum is not None else 0
            hi = self.maximum if self.maximum is not None else lo
            v = lo if self.maximum is None else (lo + hi) / 2
            return int(v) if self.type == "int" else round(float(v), 2)
        return {"str": "...", "bool": False, "list": [], "dict": {}}[self.type]


_MISSING = object()


def _enum_match(v: Any, options: List[Any]) -> Any:
    """Case/space-insensitive enum match. Small models love wrong casing."""
    if v in options:
        return v
    if isinstance(v, str):
        norm = v.strip().lower().replace(" ", "_").replace("-", "_")
        for o in options:
            if isinstance(o, str) and o.strip().lower().replace(" ", "_").replace("-", "_") == norm:
                return o                      # snap it back to canonical form
    return _MISSING


def _coerce(value: Any, want: str, item_type: str = "str") -> Tuple[Any, str]:
    """Best-effort type repair. Returns (value, error)."""
    if value is None:
        return None, "value is null"

    if want == "str":
        if isinstance(value, (dict, list)):
            return value, f"expected str, got {type(value).__name__}"
        return str(value).strip(), ""

    if want == "int":
        if isinstance(value, bool):
            return int(value), ""
        try:
            return int(float(str(value).strip())), ""
        except (TypeError, ValueError):
            return value, f"expected int, got {value!r}"

    if want == "float":
        try:
            return float(str(value).strip()), ""
        except (TypeError, ValueError):
            return value, f"expected float, got {value!r}"

    if want == "bool":
        if isinstance(value, bool):
            return value, ""
        s = str(value).strip().lower()
        if s in ("true", "yes", "y", "1"):
            return True, ""
        if s in ("false", "no", "n", "0"):
            return False, ""
        return value, f"expected bool, got {value!r}"

    if want == "list":
        items = value if isinstance(value, list) else [value]   # scalar -> [scalar]
        out = []
        for i, it in enumerate(items):
            cv, err = _coerce(it, item_type)
            if err:
                return value, f"item[{i}]: {err}"
            out.append(cv)
        return out, ""

    if want == "dict":
        if isinstance(value, dict):
            return value, ""
        return value, f"expected dict, got {type(value).__name__}"

    return value, f"unknown field type {want!r}"


# ---------------------------------------------------------------------------
# 2. Contract -- a named set of Fields
# ---------------------------------------------------------------------------

@dataclass
class Result:
    """What validate() gives back. Truthy when ok."""
    ok: bool
    value: Dict[str, Any] = _dc_field(default_factory=dict)
    errors: List[str] = _dc_field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    def why(self) -> str:
        return "; ".join(self.errors)


@dataclass
class Contract:
    name: str
    fields: Dict[str, Field]
    desc: str = ""

    # -- the whole point of the module --------------------------------------
    def validate(self, data: Any, *, strict: bool = False) -> Result:
        """Check + repair a dict against this contract.

        strict=False (default) drops unknown keys silently -- small models
        love to invent extra keys, and that is usually harmless.
        strict=True turns unknown keys into errors instead.
        """
        if not isinstance(data, dict):
            return Result(False, {}, [f"expected an object, got {type(data).__name__}"])

        out: Dict[str, Any] = {}
        errors: List[str] = []

        for key, f in self.fields.items():
            if key not in data or data[key] is None:
                if f.required and f.default is None:
                    errors.append(f"{key}: missing (required)")
                else:
                    out[key] = f.default
                continue
            ok, coerced, err = f.check(data[key])
            if ok:
                out[key] = coerced
            else:
                errors.append(f"{key}: {err}")

        if strict:
            for key in data:
                if key not in self.fields:
                    errors.append(f"{key}: unexpected key")

        return Result(not errors, out, errors)

    # -- for Ollama's `format=` structured output ---------------------------
    def json_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {k: f.json_schema() for k, f in self.fields.items()},
            "required": [k for k, f in self.fields.items() if f.required],
        }

    # -- for pasting into the prompt ----------------------------------------
    def example_json(self) -> str:
        return json.dumps({k: f.example_value() for k, f in self.fields.items()},
                          indent=2, ensure_ascii=False)

    def prompt_block(self) -> str:
        """A human+LLM readable description of the shape. Goes in the prompt.

        Small models follow a compact bullet list far better than they follow
        a raw JSON Schema, so we give them both: bullets, then an example.
        """
        lines = [f"Return ONLY a JSON object with these keys ({self.name}):"]
        for k, f in self.fields.items():
            bits = [f"{f.type}"]
            if f.enum:
                bits.append("one of: " + " | ".join(str(e) for e in f.enum))
            if not f.required:
                bits.append("optional")
            if f.desc:
                bits.append(f.desc)
            lines.append(f'  - "{k}" ({"; ".join(bits)})')
        lines.append("")
        lines.append("Example of the exact shape (values are placeholders):")
        lines.append(self.example_json())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Extracting JSON out of whatever the model actually said
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Tuple[Optional[Any], str]:
    """Pull the first JSON value out of a model reply. (value, error).

    Handles, in order:
        1. the reply is already clean JSON
        2. the reply is wrapped in a ```json ... ``` fence
        3. the reply is prose with a {...} or [...] buried inside it
        4. the reply has trailing commas  (very common in small models)

    This exists because a 7B model will happily say
        "Sure! Here is the JSON you asked for: {...} Hope this helps!"
    and that is not a reason to throw the answer away.
    """
    if text is None:
        return None, "empty reply"
    s = text.strip()
    if not s:
        return None, "empty reply"

    candidates: List[str] = [s]
    m = _FENCE.search(s)
    if m:
        candidates.insert(0, m.group(1).strip())
    span = _balanced_span(s)
    if span:
        candidates.append(span)

    for cand in candidates:
        for attempt in (cand, _strip_trailing_commas(cand)):
            try:
                return json.loads(attempt), ""
            except json.JSONDecodeError:
                continue
    return None, f"no valid JSON found in {s[:160]!r}"


def _balanced_span(s: str) -> Optional[str]:
    """Find the first balanced {...} or [...] block, ignoring braces in strings."""
    start = None
    opener = closer = ""
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if start is None:
            if ch in "{[":
                start, opener = i, ch
                closer = "}" if ch == "{" else "]"
                depth = 1
            continue
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def _strip_trailing_commas(s: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", s)
