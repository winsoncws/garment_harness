"""
answer_from_rows -- TIER 1. Turn query results into one plain sentence.
=======================================================================

The last step of the analytics chatbot, and the easiest place in the whole
system to accidentally introduce a lie.

    question: "which loom had the most oil marks?"
    rows:     [{"machine": "LOOM-07", "n": 23}, {"machine": "LOOM-03", "n": 9}]
                              |
                              v
    "LOOM-07 had the most oil marks with 23, ahead of LOOM-03 with 9."

THE RULE: THE MODEL MAY ONLY RESTATE THE ROWS
----------------------------------------------
No context, no averages it computed itself, no "which suggests the shuttle
may need servicing". Arithmetic belongs in SQL, where it is checkable.
Advice belongs to a human who is accountable for it.

`check()` enforces this numerically: every number appearing in the answer
must also appear in the rows. It is a crude rule and it is remarkably
effective -- a model that invents a statistic is caught immediately,
because the invented figure is not in the data.

    rows -> [23, 9]        answer mentions 23, 9        -> PASS
    rows -> [23, 9]        answer mentions "about 30%"  -> REJECTED
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from ..core.contracts import Contract, Field
from ..core.registry import register
from ..core.task import Task


@register
class AnswerFromRows(Task):
    name = "answer_from_rows"
    version = 1
    desc = "State what the query result shows, in one or two sentences, adding nothing."

    input = Contract("answer_from_rows.in", {
        "question": Field("str", max_len=1000),
        "rows": Field("list", item_type="dict", desc="the query result"),
        "sql": Field("str", required=False, default=""),
    })

    output = Contract("answer_from_rows.out", {
        "answer": Field("str", max_len=600,
                        desc="plain-language restatement of the rows"),
        "grounded": Field("bool",
                          desc="false if the rows do not actually answer the question"),
    })

    # Persona lives in gmt/skills/narrator.md; this string is the fallback.
    skill = "narrator"
    system = (
        "You restate database query results in plain English for a factory "
        "manager.\n"
        "  - Use ONLY numbers and names that appear in the rows.\n"
        "  - Do not compute anything new. Do not add context or advice.\n"
        "  - If the rows are empty, say so plainly and set grounded=false.\n"
        "  - Two sentences maximum.\n"
        "Reply with JSON only."
    )

    def build_user(self, payload: Dict[str, Any]) -> str:
        rows = payload["rows"][:25]          # long results do not improve the answer
        more = len(payload["rows"]) - len(rows)
        return (
            f"Question: {payload['question']}\n\n"
            f"Rows ({len(payload['rows'])} total"
            f"{f', showing first {len(rows)}' if more > 0 else ''}):\n"
            f"{json.dumps(rows, indent=2, default=str)}\n\n"
            f"{self.output.prompt_block()}\n\nJSON:"
        )

    def check(self, value: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        rows = payload.get("rows") or []
        answer = value.get("answer", "")

        if not rows:
            if value.get("grounded"):
                return ["there are no rows, so grounded must be false"]
            return []

        # every number in the answer must exist in the data.
        # The row count itself is legitimate: "3 machines had defects" is a
        # grounded statement even though 3 appears in no cell.
        row_nums = {_key(len(rows))}
        for r in rows:
            for v in r.values():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    row_nums.add(_key(v))
                elif isinstance(v, str):
                    for m in re.findall(r"\d+(?:\.\d+)?", v):
                        row_nums.add(_key(float(m)))

        invented = [n for n in re.findall(r"\d+(?:\.\d+)?", answer)
                    if _key(float(n)) not in row_nums]
        if invented:
            return [f"answer contains number(s) not present in the rows: "
                    f"{', '.join(invented[:5])}"]
        return []

    def stub(self, payload: Dict[str, Any]) -> str:
        rows = payload.get("rows") or []
        if not rows:
            return json.dumps({"answer": "The query returned no rows.",
                               "grounded": False})
        first = rows[0]
        parts = ", ".join(f"{k}={v}" for k, v in list(first.items())[:3])
        return json.dumps({
            "answer": f"{len(rows)} row(s). Top result: {parts}.",
            "grounded": True})


def _key(x: float) -> str:
    """Compare numbers loosely: 23, 23.0 and 23.00 are the same figure."""
    return f"{round(float(x), 2):g}"
