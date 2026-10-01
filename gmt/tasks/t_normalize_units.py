"""
normalize_units -- TIER 0. A task with no LLM at all.
=======================================================================

Read this one first. It exists to prove a point:

    A Task does not have to involve a model.

Everything a Task gives you -- a declared contract, validation, caching,
tracing, retry, a place in a pipeline, an MCP tool, a row in the ground
truth -- applies just the same to twenty lines of regex. And the regex is
free, instant, and 100% reproducible.

    "cut out 3 yards at the 120 m mark, 4.5 inch wide"
                          |
                          v  (pure python, ~0.1 ms)
    {"metres": [2.74, 120.0, 0.114], "found": 3}

RULE OF THUMB
-------------
Before you write a prompt, ask: "could a regex, a lookup table, or a SQL
query do this?" If yes, do that. Reach for the model only for the part
that genuinely needs language understanding. Most real pipelines are 80%
tier 0 by call count -- and that is what makes the other 20% affordable.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..core.contracts import Contract, Field
from ..core.registry import register
from ..core.task import Task
from ._vocab import TO_METRES

# number + unit, e.g. "3 yards", "120m", "4.5 inch", '12"'
_MEASURE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(" + "|".join(
        sorted((re.escape(u) for u in TO_METRES), key=len, reverse=True)
    ) + r")\b", re.IGNORECASE)


@register
class NormalizeUnits(Task):
    name = "normalize_units"
    version = 1
    desc = "Find every length measurement in a piece of text and convert it to metres."

    input = Contract("normalize_units.in", {
        "text": Field("str", desc="free text that may contain measurements"),
    })
    output = Contract("normalize_units.out", {
        "metres": Field("list", item_type="float", desc="every length found, in metres"),
        "found": Field("int", desc="how many measurements were found", minimum=0),
        "spans": Field("list", item_type="str", required=False, default=[],
                       desc="the original substrings, for auditing"),
    })

    def run_local(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        text = payload.get("text") or ""
        metres: List[float] = []
        spans: List[str] = []
        for m in _MEASURE.finditer(text):
            value = float(m.group(1).replace(",", "."))
            unit = m.group(2).lower()
            metres.append(round(value * TO_METRES[unit], 4))
            spans.append(m.group(0))
        # Returning a dict (not None) means "I answered; do not call a model".
        return {"metres": metres, "found": len(metres), "spans": spans}
