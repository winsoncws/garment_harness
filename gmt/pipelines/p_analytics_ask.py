"""
p_analytics_ask -- a question in, a grounded answer out.
=======================================================================

This one pipeline is your business analytics, your supply-chain analytics,
AND the useful half of the internal chatbot. Same code, different questions.

    "which shift has the worst severity on denim?"
                    |
                    v
    +---------------------------+
    | text_to_sql   (tier 2)    |  schema pulled LIVE from the database
    +---------------------------+
                    |  sql
                    v
    +---------------------------+
    | store.query()  READ-ONLY  |  <- executed by python, never by the model
    +---------------------------+
                    |  rows           |
                    |                 +-- SQL error or 0 rows
                    v                 v
    +---------------------------+   retry text_to_sql at the next tier up,
    | answer_from_rows (tier 1) |   with the error text fed back in
    +---------------------------+
                    |
                    v
        {answer, sql, rows, tier_used}

WHY EXECUTION IS THE REAL VALIDATOR
------------------------------------
A contract can prove the SQL is well-formed. Only running it proves the
columns exist and the enum spellings match. So the database itself becomes
the final validator, and its error message becomes the repair prompt. This
is much stronger than any amount of prompt engineering:

    "no such column: defect_type"  ->  fed straight back to the model
                                       which then uses defect_label

RAG NOTE
--------
You mentioned the team already tried RAG. Note what is happening here: no
embeddings, no vector store, no chunking. For structured questions over
structured data, text-to-SQL beats retrieval every time, because it can
COUNT and JOIN and retrieval cannot. Keep RAG for the genuinely unstructured
corpus -- SOPs, machine manuals, customer emails -- and route between the
two with a cheap tier-1 classifier. See docs/07_roadmap.md.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core import router, trace
from ..core.store import Store, get_store

MAX_SQL_ATTEMPTS = 3


def ask(question: str, *, store: Optional[Store] = None,
        max_rows: int = 200, narrate: bool = True) -> Dict[str, Any]:
    """Answer one question against the ground truth."""
    st = store or get_store()
    schema = st.schema_text()

    sql = ""
    rows: List[Dict[str, Any]] = []
    err = ""
    tier_used = -1
    attempts = 0

    # ---- 1. write SQL, execute it, and let failures drive escalation ------
    for attempt in range(MAX_SQL_ATTEMPTS):
        attempts += 1
        payload: Dict[str, Any] = {"question": question, "schema": schema}
        if err:
            # Feed the database's own complaint back into the prompt. Turning
            # off the cache matters here -- the payload changed, but we also
            # do not want a previously-cached bad answer.
            payload["question"] = (
                f"{question}\n\n"
                f"Your previous query FAILED. Fix it.\n"
                f"Previous SQL: {sql}\n"
                f"Database said: {err}"
            )

        run = router.run("text_to_sql", payload, store=st,
                         tier=2 + attempt if attempt else None,
                         use_cache=(attempt == 0))
        tier_used = run.tier
        if not run.ok:
            err = "; ".join(run.errors)
            trace.warn("text_to_sql failed", attempt=attempt + 1, err=err[:120])
            continue

        sql = run.value["sql"]
        if not run.value.get("answerable", True):
            return _out(question, sql, [], tier_used, attempts,
                        "The current schema cannot answer that question.", False)

        rows, err = st.query(sql, limit=max_rows)
        if not err:
            break
        trace.warn("sql rejected by db", attempt=attempt + 1, err=err[:120])

    if err:
        return _out(question, sql, [], tier_used, attempts,
                    f"I could not build a working query. Last error: {err}", False)

    # ---- 2. restate the rows in plain language ----------------------------
    if not narrate:
        return _out(question, sql, rows, tier_used, attempts, "", True)

    nrun = router.run("answer_from_rows",
                      {"question": question, "rows": rows, "sql": sql}, store=st)
    if nrun.ok:
        return _out(question, sql, rows, tier_used, attempts,
                    nrun.value["answer"], nrun.value["grounded"])

    # Narration failed the grounding check -> show the data, not a guess.
    return _out(question, sql, rows, tier_used, attempts,
                f"{len(rows)} row(s) returned; see the table.", True)


def _out(question: str, sql: str, rows: List[Dict[str, Any]], tier: int,
         attempts: int, answer: str, grounded: bool) -> Dict[str, Any]:
    return {"question": question, "answer": answer, "grounded": grounded,
            "sql": sql, "rows": rows, "row_count": len(rows),
            "tier_used": tier, "sql_attempts": attempts}


def render(result: Dict[str, Any], max_rows: int = 12) -> str:
    """Pretty-print for a terminal. The chatbot UI would use the dict instead."""
    lines = [f"Q: {result['question']}", f"A: {result['answer']}", ""]
    lines.append(f"SQL (tier {result['tier_used']}, "
                 f"{result['sql_attempts']} attempt(s)):")
    lines.append(f"  {result['sql']}")
    rows = result["rows"]
    if rows:
        cols = list(rows[0].keys())
        widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows[:max_rows]))
                  for c in cols]
        lines.append("")
        lines.append("  " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)))
        lines.append("  " + "-+-".join("-" * w for w in widths))
        for r in rows[:max_rows]:
            lines.append("  " + " | ".join(str(r.get(c, "")).ljust(w)
                                           for c, w in zip(cols, widths)))
        if len(rows) > max_rows:
            lines.append(f"  ... {len(rows) - max_rows} more row(s)")
    return "\n".join(lines)
