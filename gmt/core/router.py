"""
router.py -- decides WHICH model runs a task, and what to do when it fails.
=======================================================================

This is the file that turns "I have a 14B model" into "I have a system".

THE LADDER
----------
                        +------------------------+
   payload  ---------->  | validate INPUT         |-- bad --> fail fast
                        +------------------------+
                                   | ok
                                   v
                        +------------------------+
                        | tier 0: run_local()    |-- answered --> DONE (free)
                        | plain python / opencv  |
                        +------------------------+
                                   | no answer
                                   v
                        +------------------------+
                        | cache: artifact table  |-- hit ------> DONE (free)
                        +------------------------+
                                   | miss
                                   v
          +--------------------------------------------------+
          |  for tier in start..max:                          |
          |      for attempt in 1..retries+1:                 |
          |          call model                               |
          |          extract JSON                             |
          |          validate against OUTPUT contract   ------+--> DONE
          |          + run task.check() business rules        |
          |      (all attempts failed -> next tier up)        |
          +--------------------------------------------------+
                                   | ran out of tiers
                                   v
                        needs_review = 1   (a human looks at it)

WHY RETRY BEFORE ESCALATING
---------------------------
At temperature 0 a retry is nearly deterministic, so a bare retry is
usually pointless -- EXCEPT that we feed the validation errors back into
the second prompt ("your last answer was rejected because: severity: 'high'
is not an int"). That repair turn fixes a surprising share of small-model
failures for a fraction of the cost of jumping to a bigger model.

WHAT ESCALATION GIVES YOU FOR FREE
----------------------------------
Every escalation is logged. After a week you can ask the store:

    SELECT task, started_tier, tier, COUNT(*)
    FROM task_run GROUP BY 1,2,3;

...and you will know precisely which tasks your 14B handles and which ones
it does not. Then you either (a) improve that one prompt, (b) pin that one
task higher, or (c) collect its tier-3 answers as fine-tuning data. That
feedback loop is the actual product of this repo.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Union

from . import config, llm, registry, trace
from .contracts import extract_json
from .store import Store, get_store
from .task import Task, TaskRun

TaskLike = Union[str, Task]

# Tiers whose model is permanently unavailable (not installed, bad name).
# OBSERVED 2026-08-21: with tier 1 pointed at an un-pulled `qwen2.5:3b`, a
# 24-note ingest made 24 pointless HTTP round trips and printed 24 identical
# 404 warnings before escalating each time. Escalation was correct; the
# repetition was not. A missing model is a PERMANENT condition, so we learn
# it once and skip that tier for the rest of the process.
#
# Deliberately NOT persisted: `ollama pull` during a long-running service
# should start working on the next restart, and a process-lifetime memo is
# the right scope for that.
_DEAD_TIERS: Dict[int, str] = {}

_PERMANENT = ("not found", "404", "no such model", "unknown model")


def _is_permanent(err: str) -> bool:
    low = err.lower()
    return any(marker in low for marker in _PERMANENT)


def reset_dead_tiers() -> None:
    """Forget the blacklist -- call after an `ollama pull`, or in tests."""
    _DEAD_TIERS.clear()


# ---------------------------------------------------------------------------
# routing policy
# ---------------------------------------------------------------------------

def tiers_for(task_name: str) -> Dict[str, int]:
    """Look up (start, max) for a task, honouring the global ceiling."""
    cfg = config.load()
    spec = cfg.get(f"routing.tasks.{task_name}") or {}
    ceiling = int(cfg.get("routing.max_tier", 3))
    start = int(spec.get("start", cfg.get("routing.default_start_tier", 2)))
    top = min(int(spec.get("max", ceiling)), ceiling)
    return {"start": min(start, top), "max": top}


# ---------------------------------------------------------------------------
# the one function everything else calls
# ---------------------------------------------------------------------------

def run(task: TaskLike, payload: Dict[str, Any], *,
        store: Optional[Store] = None,
        doc_id: Optional[str] = None,
        tier: Optional[int] = None,
        max_tier: Optional[int] = None,
        use_cache: bool = True,
        persist: bool = True) -> TaskRun:
    """Execute one task on one payload, escalating as needed.

    tier      -- force a starting tier (bench.py uses this to compare tiers)
    use_cache -- reuse an identical previous answer from the artifact table
    persist   -- write the artifact + the task_run trace row
    """
    t: Task = registry.get(task) if isinstance(task, str) else task
    st = store if store is not None else (get_store() if persist else None)
    cfg = config.load()

    run_rec = TaskRun(task=t.name)
    run_rec.input_hash = t.input_hash(payload)

    # ---- 1. input contract ------------------------------------------------
    vin = t.validate_input(payload)
    if not vin.ok:
        run_rec.errors = [f"input invalid: {vin.why()}"]
        trace.warn("input rejected", task=t.name, err=vin.why())
        _finish(run_rec, t, st, doc_id, persist)
        return run_rec
    payload = vin.value

    # ---- 2. tier 0: no model at all ---------------------------------------
    policy = tiers_for(t.name)
    start = tier if tier is not None else policy["start"]
    top = max_tier if max_tier is not None else policy["max"]
    run_rec.started_tier = start

    if start == 0 or policy["start"] == 0:
        local = t.run_local(payload)
        if local is not None:
            res = t.validate_output(local, payload)
            run_rec.tier, run_rec.model, run_rec.provider = 0, "python", "local"
            if res.ok:
                run_rec.ok, run_rec.value = True, t.postprocess(res.value, payload)
            else:
                run_rec.errors = res.errors
            trace.debug("tier0 answered", task=t.name, ok=run_rec.ok)
            _finish(run_rec, t, st, doc_id, persist)
            return run_rec
        if top == 0:
            run_rec.errors = ["tier 0 declined and max tier is 0"]
            _finish(run_rec, t, st, doc_id, persist)
            return run_rec

    # ---- 3. cache ---------------------------------------------------------
    if use_cache and st is not None:
        cached = st.get_artifact(t.name, t.version, run_rec.input_hash)
        if cached is not None:
            run_rec.ok, run_rec.value, run_rec.tier = True, cached, -1
            run_rec.model = "cache"
            trace.debug("cache hit", task=t.name, hash=run_rec.input_hash)
            return run_rec                     # no artifact rewrite, no trace row

    # ---- 4. climb the ladder ----------------------------------------------
    retries = int(cfg.get("routing.retries_per_tier", 1))
    system = t.system_prompt()
    user = t.build_user(payload)
    schema = t.schema()
    repair_note = ""

    for cur in range(max(start, 1), top + 1):
        if cur in _DEAD_TIERS:
            run_rec.errors.append(f"tier{cur}: skipped ({_DEAD_TIERS[cur]})")
            continue
        try:
            client = llm.get_client(cur)
        except llm.LLMError as e:
            run_rec.errors.append(str(e))
            continue

        for attempt in range(retries + 1):
            run_rec.attempts += 1
            reply = client.chat(system, user + repair_note, schema=schema,
                                stub=(lambda: t.stub(payload)))
            run_rec.latency_ms += reply.latency_ms
            run_rec.prompt_tokens += reply.prompt_tokens
            run_rec.output_tokens += reply.output_tokens
            run_rec.tier, run_rec.model, run_rec.provider = cur, reply.model, reply.provider
            run_rec.raw_text = reply.text

            if not reply.ok:
                run_rec.errors.append(f"tier{cur}: {reply.error}")
                if _is_permanent(reply.error):
                    # Warn ONCE, then stop asking. Anything else buries the
                    # real output under identical 404s.
                    _DEAD_TIERS[cur] = f"model {reply.model!r} is not installed"
                    trace.warn("tier disabled for this run", tier=cur,
                               model=reply.model, hint=f"ollama pull {reply.model}")
                else:
                    trace.warn("model error", task=t.name, tier=cur, err=reply.error)
                break                                    # transport failure: escalate

            data, jerr = extract_json(reply.text)
            if jerr:
                errs = [f"reply was not JSON: {jerr}"]
            else:
                res = t.validate_output(data, payload)
                if res.ok:
                    run_rec.ok = True
                    run_rec.value = t.postprocess(res.value, payload)
                    run_rec.escalated = cur > run_rec.started_tier
                    trace.debug("ok", task=t.name, tier=cur, attempt=attempt + 1)
                    _finish(run_rec, t, st, doc_id, persist)
                    return run_rec
                errs = res.errors

            run_rec.errors.extend(f"tier{cur}a{attempt + 1}: {e}" for e in errs)
            trace.debug("rejected", task=t.name, tier=cur, err="; ".join(errs)[:120])
            # feed the failure back in -- this is the "repair turn"
            repair_note = (
                "\n\nYour previous answer was REJECTED. Fix exactly these problems "
                "and return only the corrected JSON object:\n- "
                + "\n- ".join(errs)
                + f"\n\nYour previous answer was:\n{reply.text[:800]}"
            )

        repair_note = ""            # a fresh tier gets a clean prompt

    trace.warn("all tiers failed", task=t.name, tried=f"{start}..{top}")
    _finish(run_rec, t, st, doc_id, persist)
    return run_rec


def _finish(run_rec: TaskRun, t: Task, st: Optional[Store],
            doc_id: Optional[str], persist: bool) -> None:
    """Persist the artifact (on success) and the trace row (always)."""
    run_rec.task_version = t.version                      # type: ignore[attr-defined]
    if not persist or st is None:
        return
    if run_rec.ok:
        st.put_artifact(task=t.name, task_version=t.version,
                        input_hash=run_rec.input_hash, value=run_rec.value,
                        doc_id=doc_id, tier=run_rec.tier, model=run_rec.model)
    if config.load().get("logging.trace", True):
        st.log_run(run_rec)


def explain(task_name: str) -> str:
    """Human-readable routing decision for one task. Used by the CLI."""
    p = tiers_for(task_name)
    cfg = config.load()
    lines = [f"task: {task_name}",
             f"  start tier : {p['start']}",
             f"  max tier   : {p['max']}  (global ceiling {cfg.get('routing.max_tier')})"]
    for tr in range(p["start"], p["max"] + 1):
        spec = cfg.get(f"models.tier{tr}") or {}
        if spec:
            lines.append(f"  tier {tr}     : {spec.get('provider')} / {spec.get('model')}")
        elif tr == 0:
            lines.append("  tier 0     : pure python (run_local)")
    return "\n".join(lines)
