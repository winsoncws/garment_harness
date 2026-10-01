#!/usr/bin/env python3
"""
ollama_smoke.py -- THE script to run first on the GPU server.
=======================================================================

`python3 -m gmt doctor` tells you whether Ollama is reachable.
This tells you whether it actually WORKS for what we need, which is a
different and much more useful question. It checks, in order:

    1. can we reach the server, and which models exist
    2. does it do plain chat
    3. does it support SCHEMA-CONSTRAINED output (`format` = JSON Schema)
       -- this is the single most important capability for the harness
    4. how fast is each model, cold and warm
    5. does every real task pass its own contract on a real example
    6. what is the actual escalation rate on the golden set

Run it, paste the output back, and there is enough information to tune
config/models.yaml and config/routing.yaml without guessing.

    python3 scripts/ollama_smoke.py
    python3 scripts/ollama_smoke.py --model qwen2.5:14b
    python3 scripts/ollama_smoke.py --quick        # skip the task tests
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gmt.core import config, llm, registry, router          # noqa: E402
from gmt.core.contracts import extract_json                 # noqa: E402

SAMPLES = {
    "classify_defect": {"text": "L7 3rd shift, big oil smear down the selvedge, about 2m"},
    "extract_defect_fields": {"text": "L7 3rd shift, roll RB-4412, 60/40 pc, oil at 88.5m, 2 pcs"},
    "extract_triples": {"text": "L7 oil mark, roll RB-4412, yarn lot YL-88 from Anhui Textile"},
    "text_to_sql": {"question": "which machine had the most oil marks last week?"},
    "answer_from_rows": {"question": "which machine is worst?",
                         "rows": [{"machine": "LOOM-07", "n": 23},
                                  {"machine": "LOOM-03", "n": 9}]},
}


def hr(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="override the model to probe")
    ap.add_argument("--quick", action="store_true", help="skip the per-task tests")
    args = ap.parse_args()

    cfg = config.load()
    host = cfg.get("ollama.host")

    # ---- 1. connectivity --------------------------------------------------
    hr("1. CONNECTIVITY")
    print(f"host: {host}")
    probe = llm.OllamaClient(args.model or cfg.get("models.tier2.model"))
    models = probe.list_models()
    if not models:
        print("FAIL: cannot reach Ollama.")
        print("  on the GPU box:  OLLAMA_HOST=0.0.0.0 ollama serve")
        print("  from elsewhere:  export GMT_OLLAMA_HOST=http://<gpu-ip>:11434")
        return 1
    print(f"OK: {len(models)} model(s) installed")
    for m in models:
        print(f"   {m}")

    tiers = {}
    for t in (1, 2):
        want = cfg.get(f"models.tier{t}.model")
        found = want in models          # exact tag only -- see cli.py doctor
        tiers[t] = want
        print(f"tier{t}: {want:<24} {'OK' if found else 'MISSING -> ollama pull ' + want}")

    targets = [args.model] if args.model else [tiers[1], tiers[2]]
    targets = [t for t in dict.fromkeys(targets) if t]

    # ---- 2 + 3. chat and structured output --------------------------------
    hr("2. PLAIN CHAT  +  3. SCHEMA-CONSTRAINED OUTPUT")
    schema = {
        "type": "object",
        "properties": {"label": {"type": "string", "enum": ["hole", "stain", "oil_mark"]},
                       "severity": {"type": "integer"}},
        "required": ["label", "severity"],
    }
    capable = {}
    for model in targets:
        c = llm.OllamaClient(model)
        print(f"\n--- {model} ---")

        r = c.chat("You answer in one word.", "What colour is cotton, typically?")
        if not r.ok:
            print(f"  chat        FAIL  {r.error}")
            capable[model] = False
            continue
        print(f"  chat        OK    {r.latency_ms}ms  -> {r.text.strip()[:60]!r}")

        r = c.chat("You classify fabric defects. JSON only.",
                   "Note: 'big oil smear on the selvedge, 2m'", schema=schema)
        if not r.ok:
            print(f"  schema      FAIL  {r.error}")
            capable[model] = False
            continue
        data, err = extract_json(r.text)
        good = not err and isinstance(data, dict) and "label" in data
        print(f"  schema      {'OK  ' if good else 'WEAK'}  {r.latency_ms}ms  -> {r.text.strip()[:70]!r}")
        if not good:
            print("     ^ your Ollama may predate JSON-Schema `format` (needs >= 0.5).")
            print("       The harness still works -- it falls back to prompt-based JSON")
            print("       plus contract validation -- but upgrading Ollama is worth it.")
        capable[model] = True

    # ---- 4. speed ---------------------------------------------------------
    hr("4. SPEED  (cold = first call, warm = weights already in VRAM)")
    print(f"{'model':<24} {'cold ms':<10} {'warm ms':<10} {'tok/s':<8}")
    for model in targets:
        if not capable.get(model):
            continue
        c = llm.OllamaClient(model)
        t0 = time.time()
        c.chat("Reply with one word.", "Say: ready")
        cold = int((time.time() - t0) * 1000)
        warm_ms, toks = [], 0
        for _ in range(3):
            r = c.chat("Reply with one short sentence.",
                       "Describe an oil stain on fabric in one sentence.")
            warm_ms.append(r.latency_ms)
            toks += r.output_tokens
        avg = sum(warm_ms) // len(warm_ms)
        tps = round(toks / (sum(warm_ms) / 1000), 1) if sum(warm_ms) else 0
        print(f"{model:<24} {cold:<10} {avg:<10} {tps:<8}")
    print("\nIf cold >> warm, set a longer `keep_alive` in config/settings.yaml.")

    if args.quick:
        return 0

    # ---- 5. every real task, at its configured tier -----------------------
    hr("5. REAL TASKS  (does each one pass its own contract?)")
    print(f"{'task':<24} {'tier':<5} {'ok':<4} {'ms':<7} {'att':<4} detail")
    print("-" * 78)
    failures = []
    for name, payload in SAMPLES.items():
        if name not in registry.all():
            continue
        run = router.run(name, payload, use_cache=False, persist=False)
        detail = json.dumps(run.value, default=str)[:44] if run.ok else \
            "; ".join(run.errors)[:44]
        print(f"{name:<24} {run.tier:<5} {'yes' if run.ok else 'NO':<4} "
              f"{run.latency_ms:<7} {run.attempts:<4} {detail}")
        if not run.ok:
            failures.append((name, run))

    if failures:
        hr("FAILURES IN DETAIL  (this is the useful part)")
        for name, run in failures:
            print(f"\n--- {name} ---")
            print("errors  :", "; ".join(run.errors)[:400])
            print("model said:", (run.raw_text or "")[:400])
            print("\nWhat to do:")
            print("  * output not JSON      -> Ollama may not support schema `format`;")
            print("                            upgrade Ollama, or set use_schema=False")
            print("                            on the task and lean on the contract.")
            print("  * enum mismatch        -> add the mill's wording to the")
            print("                            `## Examples` block in gmt/skills/*.md")
            print("  * evidence not in note -> the model is paraphrasing; strengthen")
            print("                            the 'copy word-for-word' line in the skill")

    # ---- 6. escalation on the golden set ----------------------------------
    hr("6. GOLDEN SET  (accuracy per tier -- the number that decides routing)")
    from gmt.bench import run_bench
    run_bench([1, 2])

    print("\nDone. Paste this whole output back for tuning advice.")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
