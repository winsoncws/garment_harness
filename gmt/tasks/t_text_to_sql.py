"""
text_to_sql -- TIER 2/3. Turn a business question into ONE SELECT.
=======================================================================

This is the engine behind both "business analytics" and the analytics half
of the chatbot. One task, two products.

    "which loom had the most oil marks last week?"
                    |
                    v  qwen2.5:14b + the REAL schema in the prompt
    SELECT machine, COUNT(*) n FROM defect_observation
    WHERE defect_label='oil_mark' AND observed_at >= date('now','-7 day')
    GROUP BY machine ORDER BY n DESC

THREE THINGS THAT MAKE THIS WORK, AND ONE THAT DOES NOT
-------------------------------------------------------
WORKS  1. Put the real `CREATE TABLE` text in the prompt, pulled live from
          the database (store.schema_text()). A hand-written summary of the
          schema WILL drift out of date and the model will write SQL against
          columns you deleted last month.
WORKS  2. Give it the closed vocabularies. Without them it writes
          `defect_label = 'oil stain'` and returns zero rows -- the worst
          possible failure, because it looks like an answer.
WORKS  3. Execute read-only (store.query) so a bad generation cannot hurt
          anything. Then the *result* is the validation: zero rows or a SQL
          error is a signal to escalate, not something to show the user.
FAILS     Asking the model to "be careful". It is not a matter of care.

SAFETY
------
This task only WRITES sql text. Executing it is the pipeline's job, via
Store.query(), which opens the database read-only. Never wire a task's
output straight into a writable cursor.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ..core.contracts import Contract, Field
from ..core.registry import register
from ..core.store import get_store
from ..core.task import Task
from ._vocab import DEFECT_LABELS, FABRIC_TYPES, SHIFTS

_ALLOWED_TABLES = {
    "source_document", "source_blob", "artifact", "task_run",
    "defect_observation", "kg_node", "kg_edge", "metric_daily",
    "v_defect_daily",
}


@register
class TextToSQL(Task):
    name = "text_to_sql"
    version = 1
    desc = "Write one read-only SQLite SELECT that answers a business question."

    input = Contract("text_to_sql.in", {
        "question": Field("str", desc="the question in plain English", max_len=1000),
        "schema": Field("str", required=False, default="",
                        desc="CREATE TABLE text; filled in automatically if omitted"),
    })

    output = Contract("text_to_sql.out", {
        "sql": Field("str", desc="a single SELECT or WITH statement, no semicolon",
                     max_len=4000),
        "tables": Field("list", item_type="str", desc="tables the query reads"),
        "rationale": Field("str", max_len=400,
                           desc="one sentence on how the query answers the question"),
        "answerable": Field("bool", desc="false if the schema cannot answer this at all"),
    })

    # Persona lives in gmt/skills/analyst.md; this string is the fallback.
    skill = "analyst"
    system = (
        "You are a careful analytics engineer writing SQLite.\n"
        "  - Return exactly ONE statement. No semicolon. No comments.\n"
        "  - SELECT or WITH only. Never write to the database.\n"
        "  - Use ONLY tables and columns that appear in the provided schema.\n"
        "  - Use the exact enum spellings given; the database stores those.\n"
        "  - SQLite dates are ISO strings: use date('now','-7 day') style.\n"
        "  - If the schema genuinely cannot answer the question, set "
        "answerable=false and put your best partial query in sql.\n"
        "Reply with JSON only."
    )

    def build_user(self, payload: Dict[str, Any]) -> str:
        schema = payload.get("schema") or get_store().schema_text()
        return (
            "Database schema (authoritative -- nothing outside this exists):\n"
            f"```sql\n{schema}\n```\n\n"
            "Exact allowed values in the key columns:\n"
            f"  defect_observation.defect_label : {', '.join(DEFECT_LABELS)}\n"
            f"  defect_observation.shift        : {', '.join(SHIFTS)}\n"
            f"  defect_observation.fabric_type  : {', '.join(FABRIC_TYPES)}\n"
            f"  defect_observation.severity     : 1..5 (5 = reject)\n\n"
            f"{self.output.prompt_block()}\n\n"
            f"Question: {payload['question']}\n\nJSON:"
        )

    def check(self, value: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        errs: List[str] = []
        sql = (value.get("sql") or "").strip().rstrip(";")
        if not sql:
            return ["sql is empty"]
        if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
            errs.append("sql must start with SELECT or WITH")
        if ";" in sql:
            errs.append("only one statement is allowed (found a semicolon)")
        if re.search(r"\b(insert|update|delete|drop|alter|create|attach|pragma)\b",
                     sql, re.IGNORECASE):
            errs.append("sql contains a write keyword")

        # Every table referenced must actually exist. This catches the
        # classic small-model move of inventing a `defects` table.
        referenced = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)",
                                    sql, re.IGNORECASE))
        unknown = {t for t in referenced if t.lower() not in _ALLOWED_TABLES}
        if unknown:
            errs.append(f"unknown table(s): {', '.join(sorted(unknown))}. "
                        f"Allowed: {', '.join(sorted(_ALLOWED_TABLES))}")
        return errs

    def postprocess(self, value: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        value["sql"] = value["sql"].strip().rstrip(";").strip()
        return value

    def stub(self, payload: Dict[str, Any]) -> str:
        import json
        q = payload.get("question", "").lower()
        if "machine" in q or "loom" in q:
            sql = ("SELECT machine, COUNT(*) AS n FROM defect_observation "
                   "GROUP BY machine ORDER BY n DESC")
        elif "shift" in q:
            sql = ("SELECT shift, COUNT(*) AS n, AVG(severity) AS avg_sev "
                   "FROM defect_observation GROUP BY shift ORDER BY n DESC")
        else:
            sql = ("SELECT defect_label, COUNT(*) AS n FROM defect_observation "
                   "GROUP BY defect_label ORDER BY n DESC")
        return json.dumps({"sql": sql, "tables": ["defect_observation"],
                           "rationale": "offline stub: grouped count",
                           "answerable": True})
