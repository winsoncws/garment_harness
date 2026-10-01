"""
bench.py -- measure, do not guess.
=======================================================================

THE QUESTION THIS ANSWERS
-------------------------
"Can qwen2.5:3b do this job, or do I need the 14b?"

You cannot answer that by reading prompts. You answer it by running both
against labelled examples and reading a table:

    task                  tier  n   acc    p50 ms   escalations
    classify_defect       1     16  0.81      190   0
    classify_defect       2     16  1.00     2140   0
    extract_defect_fields 1     16  0.44      210   0
    extract_defect_fields 2     16  0.94     2600   0

Read that and the decision makes itself: classification can live at tier 1
if 81% is good enough for a first pass with review, extraction cannot.

THE MISTAKE TO AVOID
--------------------
Caching. If tier 1 runs first and writes an artifact, tier 2 will read that
cached answer and report tier 1's accuracy. Every run here passes
`use_cache=False` and `persist=False`. Benchmarks must never share state
with production data -- and they must never pollute it either.

GROWING THE GOLDEN SET
----------------------
tests/golden/qa_golden.jsonl has 16 rows. That is enough to catch gross
regressions and nowhere near enough to trust a percentage point. Get to
100-200 rows before you make a tier decision you would defend in a meeting.
The cheapest source of new rows: the `needs_review` queue -- those are
exactly the examples the model found hard, already picked out for you.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden" / "qa_golden.jsonl"

# field in the golden file -> (task, key in that task's output)
CHECKS = {
    "label":   ("classify_defect", "label"),
    "machine": ("extract_defect_fields", "machine"),
    "shift":   ("extract_defect_fields", "shift"),
    "roll_id": ("extract_defect_fields", "roll_id"),
    "yarn_lot": ("extract_defect_fields", "yarn_lot"),
}


def load_golden(limit: int = 0) -> List[Dict[str, Any]]:
    rows = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[:limit] if limit else rows


def run_bench(tiers: List[int], limit: int = 0) -> int:
    from .core import config, router

    rows = load_golden(limit)
    if not rows:
        print("no golden rows found")
        return 1

    offline = bool(config.load().get("runtime.offline"))
    print(f"golden set: {len(rows)} rows   tiers: {tiers}"
          f"{'   [OFFLINE - every tier uses the same fake model]' if offline else ''}\n")

    results: Dict[str, Dict[int, Dict[str, Any]]] = {}

    for tier in tiers:
        # group the checks by the task that produces them
        by_task: Dict[str, List[str]] = {}
        for gold_key, (task, _out_key) in CHECKS.items():
            by_task.setdefault(task, []).append(gold_key)

        for task, gold_keys in by_task.items():
            hits = {k: 0 for k in gold_keys}
            n_valid = 0
            lat: List[int] = []
            escalated = 0
            failures: List[str] = []

            for row in rows:
                t0 = time.time()
                run = router.run(task, {"text": row["text"]},
                                 tier=tier, max_tier=tier,
                                 use_cache=False,      # <- never share state
                                 persist=False)        # <- never pollute prod
                lat.append(int((time.time() - t0) * 1000))
                escalated += 1 if run.escalated else 0

                if not run.ok:
                    failures.append(f"{row['text'][:44]!r}: {'; '.join(run.errors)[:70]}")
                    continue
                n_valid += 1
                for gk in gold_keys:
                    out_key = CHECKS[gk][1]
                    if _same(run.value.get(out_key), row.get(gk)):
                        hits[gk] += 1

            results.setdefault(task, {})[tier] = {
                "n": len(rows), "valid": n_valid, "hits": hits,
                "lat": lat, "escalated": escalated, "failures": failures,
            }

    _print_table(results, tiers)
    return 0


def _print_table(results: Dict[str, Dict[int, Dict[str, Any]]],
                 tiers: List[int]) -> None:
    print(f"{'task':<24} {'tier':<5} {'n':<4} {'valid':<6} "
          f"{'field':<10} {'acc':<7} {'p50ms':<7} {'esc'}")
    print("-" * 82)
    for task in sorted(results):
        for tier in tiers:
            r = results[task].get(tier)
            if not r:
                continue
            p50 = sorted(r["lat"])[len(r["lat"]) // 2] if r["lat"] else 0
            first = True
            for gk, hit in r["hits"].items():
                acc = hit / max(r["n"], 1)
                head = f"{task:<24} {tier:<5} {r['n']:<4} {r['valid']:<6} " if first else " " * 42
                print(f"{head}{gk:<10} {acc:<7.2f} "
                      f"{p50 if first else '':<7} {r['escalated'] if first else ''}")
                first = False

    print("\n[contract failures]  (output did not validate at all)")
    any_fail = False
    for task in sorted(results):
        for tier in tiers:
            r = results[task].get(tier)
            for f in (r or {}).get("failures", [])[:4]:
                any_fail = True
                print(f"  {task} t{tier}: {f}")
    if not any_fail:
        print("  (none -- every reply passed its contract)")

    print("\nHOW TO READ THIS")
    print("  acc  = exact match against the golden label")
    print("  valid= replies that passed the contract; acc is over ALL n, so a")
    print("         model that fails the contract is correctly penalised")
    print("  esc  = escalations (should be 0 here: bench pins each tier)")


def _same(got: Any, want: Any) -> bool:
    """Compare leniently: '' and None and 'unknown' all mean 'absent'."""
    def norm(x: Any) -> str:
        s = str(x if x is not None else "").strip().lower()
        return "" if s in ("unknown", "none", "null", "0", "0.0") else s
    return norm(got) == norm(want)
