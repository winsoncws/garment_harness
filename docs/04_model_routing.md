# 04 — Model routing: small model vs big model

The question this whole harness exists to answer: **what can my 14B actually
be trusted with?**

---

## The ladder

| Tier | Where | Model | Latency | Cost/1k calls | Good at |
|------|-------|-------|---------|---------------|---------|
| 0 | pure python | — | ~1 ms | £0 | regex, OpenCV, SQL, lookups, arithmetic |
| 1 | your GPU | `qwen2.5:3b` | ~0.2 s | £0 | classify, tag, route, yes/no |
| 2 | your GPU | `qwen2.5:14b` | ~2 s | £0 | extract, summarize, write SQL, rewrite |
| 3 | cloud | `claude-sonnet-5` | ~5 s | ~£3 | plan, design, ambiguity, write new tasks |

Tiers 1 and 2 cost nothing per call, but they are not free: they cost GPU
time, and GPU time is the bottleneck once you are processing a shift's worth
of notes. Tier 0 is the only genuinely free tier.

---

## How to choose a tier — the honest method

**Do not reason about it. Measure it.**

```bash
python3 -m gmt bench --tiers 1,2
```

### A real run — measured on `hanalytics`, 2026-08-21

`qwen2.5-coder:14b` (Q4_K_M) on a GTX 1080 Ti, 16 golden rows:

```
task                     tier  n    valid  field      acc     p50ms   esc
----------------------------------------------------------------------------
classify_defect          2     16   16     label      1.00    2687    0
extract_defect_fields    2     16   16     machine    0.94    3795    0
                                          shift      1.00
                                          roll_id    1.00
                                          yarn_lot   1.00
```

Zero contract failures across 32 calls. Every reply validated first time.

### The same golden set, both tiers

With `qwen2.5:3b` pulled, the comparison the whole harness exists to make:

```
task                     tier  n    valid  field      acc     p50ms
----------------------------------------------------------------------
classify_defect          1     16   16     label      0.88     861
classify_defect          2     16   16     label      1.00    2618
extract_defect_fields    1     16   15     machine    0.56     987
                                          shift      0.69
                                          roll_id    0.94
                                          yarn_lot   0.94
extract_defect_fields    2     16   16     machine    0.94    3780
                                          shift      1.00
                                          roll_id    1.00
                                          yarn_lot   1.00
```

**Extraction is settled**: 0.56 vs 0.94 on `machine`, 0.69 vs 1.00 on
`shift`, and one reply that did not validate at all. Tier 2, no argument.

**Classification is NOT settled, and this is the more useful lesson.**
0.88 vs 1.00 on 16 rows is a difference of *two examples*. That is noise
with a decimal point on it. Do not make a routing decision from it — get to
100+ golden rows first.

### What tuning actually looked like, run by run

Three full ingests of the same 24 notes, same model, on `hanalytics`:

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| failed chains | 5 | 2 | **1** |
| KG edges | 63 | 77 | **79** |
| `extract_triples` ok at t2 | 14/19 | 17/19 | **18/19** |
| `classify_defect` avg | 12407 ms | 2904 ms | 2916 ms |

- **run 1 → 2**: showed the model the vocabularies it was being validated
  against, and stopped passing field *names* in the hint. Also fixed a CLI
  default that overrode `runtime.concurrency` — that alone is the 4x latency
  drop, not a model change.
- **run 2 → 3**: added `repair()` to flip unambiguously reversed edges.

The last failure is `yarn_lot --made_of--> fabric`, which is illegal in both
directions — the model invented a relationship rather than reversing one, so
it is correctly rejected and not repaired. **One rejected chain out of 19 is
the right end state.** The goal was never zero failures; it was zero wrong
data, with the failures visible in the trace.

### The limit of escalation that you must understand

It is tempting to think "run classification at tier 1; the 12% that fail
will escalate to tier 2 anyway". **They will not.**

    escalation triggers on output that fails its CONTRACT
    escalation does NOT trigger on output that is merely WRONG

A 3B that confidently returns `{"label": "stain", "severity": 3,
"confidence": 1.0, "evidence": "oil smear"}` for an oil mark has produced
perfectly valid output. Every type checks, the enum is legal, the evidence
is genuinely quoted. It passes, it is stored, and nothing anywhere notices.

The ladder protects you from *malformed* output, not *incorrect* output.
Correctness is what `bench` and the golden set are for, and there is no
runtime substitute for them. This is the single most important thing to
understand about the routing design.

### The same run, before a fix — and what it teaches

The first pass looked like this:

```
extract_defect_fields    2     16   16     machine    0.81
                                          roll_id    0.81
```

Six misses. Diagnosing them individually mattered, because they had three
*different* causes and only one was the model's fault:

| Miss | Cause | Fix |
|------|-------|-----|
| "dye jigger 2", "stenter 2" → empty machine | the prompt never said these are machines | added a machine-vocabulary block to `gmt/skills/extractor.md` |
| "yarn lot YL-88" → `roll_id` | **the contract was wrong** — its description said "roll / batch / lot identifier" | split `yarn_lot` into its own field |
| "loom 7 running clean" → LOOM-07 | **the golden label was wrong** — the note does name loom 7 | fixed the label |

Two of the three fixes were not prompt changes at all. If you reach
straight for the prompt every time accuracy is low, you will "fix" a
correct model into agreeing with a broken contract. Read the failures
individually before you touch anything.

**Then re-measure after every change.** A prompt edit that feels better and
measures the same is how prompt files rot.

---

## Rules of thumb, before you have measurements

| The task is… | Start at | Why |
|--------------|----------|-----|
| deterministic (regex, unit conversion, lookup) | 0 | a model can only make it worse |
| picking 1 of N (N < 20), N is a closed list | 1 | this is what small models are for |
| picking 1 of N where the classes are subtle | 2 | 3B models confuse near-neighbours |
| filling 3+ slots from free text | 2 | each slot is another chance to fail |
| writing SQL / code | 2 | syntax needs the extra capacity |
| summarising with judgement about what matters | 2–3 | "important" is not in the text |
| designing a schema, a task, or a prompt | 3 | done once; correctness dominates cost |
| anything where being wrong is expensive and rare | 3 | volume is low, so cost is low |

**Start one tier higher than you think, then push down with evidence.**
Guessing tiers up front is how you ship silent quality loss.

---

## Escalation, and why it is not just a retry

```
tier 1  ──▶ invalid ──▶ retry tier 1 WITH THE ERRORS ──▶ invalid ──▶ tier 2 ──▶ ok
                              ▲
                              └── this is the part that matters
```

At temperature 0 a bare retry returns nearly the same tokens, so a bare
retry is pointless. What `router.py` actually sends on retry is:

```
Your previous answer was REJECTED. Fix exactly these problems and return
only the corrected JSON object:
- severity: 'high' is not an int
- evidence 'a gaping tear near the hem' does not appear in the note

Your previous answer was:
{"label": "hole", "severity": "high", ...}
```

A repair turn at tier 1 costs 0.2 s. Escalating to tier 2 costs 2 s. Repair
first, always.

### A real escalation, measured on hanalytics

Note: `"l3 float defect 12 inch from edge, roll CT-2210, second shift"`

```
pinned tier 1 only
  attempt 1  qwen2.5:3b   position_m = 0.1666666666666666   REJECTED
             -> "not supported by any number in the note, nor by any
                 unit conversion of one"
  attempt 2  qwen2.5:3b   position_m = 0.167                REJECTED
             (the repair turn fed the error back; the 3B rounded and
              resubmitted the same wrong answer -- it genuinely cannot
              do this conversion)
  RESULT: failed after 2 attempts, 8.1 s

tier 1, allowed to escalate
  attempts 1-2  as above, both rejected at tier 1
  attempt 3     qwen2.5-coder:14b  position_m = 0.3048      ACCEPTED
  RESULT: ok, ended at tier 2, escalated=True, 20.6 s
          {"shift": "B", "roll_id": "CT-2210", "position_m": 0.3048}
```

0.3048 m is exactly 12 inches. The 14B did the conversion the 3B could not,
and also got `shift: B` from "second shift".

**Three things in this trace are worth internalising:**

1. **The repair turn is not magic.** It re-asked and got the same wrong
   answer back, because the 3B lacked the capability, not the instructions.
   Repair fixes format mistakes; it does not fix incapability. That is what
   escalation is for, and why you want both.
2. **A guard fired on a real, confident, well-formed wrong answer.** Nothing
   about `0.1666666666666666` looks broken. Only checking it against the
   input caught it.
3. **The guard had to be right in BOTH directions.** An earlier version of
   this check rejected any number not literally in the note — which would
   have rejected the 14B's correct `0.3048` too, and the escalation would
   have failed at every tier. A guard that is too strict does not fail
   safe; it fails *closed*, and takes your good answers with it.

---

## Reading the trace to find the truth

After a week of production, the trace table tells you what actually happened:

```sql
SELECT task,
       started_tier,
       tier          AS finished_tier,
       COUNT(*)      AS n,
       SUM(ok)       AS passed,
       ROUND(AVG(latency_ms)) AS ms
FROM task_run
GROUP BY task, started_tier, tier
ORDER BY task, started_tier;
```

```bash
python3 -m gmt stats     # runs a version of this for you
```

| What you see | What it means | What to do |
|--------------|---------------|------------|
| escalation rate < 5% | the small model is handling it | consider pushing down a tier |
| escalation rate 20–50% | borderline | improve the prompt, or pin higher |
| escalation rate > 50% | the small model cannot do this | pin at the higher tier; stop paying the failed attempt |
| high escalation on ONE input type | a prompt gap, not a capability gap | add that case to the skill's `## Examples` |
| tier 3 hit at all | either genuinely hard, or a contract bug | read `raw_text` in `task_run` |

That last row matters. `task_run.raw_text` stores exactly what the model
said. When something fails mysteriously, read it — nine times out of ten the
model was right and the contract was wrong.

---

## The compounding win: your trace is a training set

```
     tier 3 answers          ─┐
     human `needs_review` fixes ├──▶ few-shot examples in the skill .md
     tier 2 answers that passed ─┘        │
                                          ▼
                                  tier 1 now handles it
                                          │
                                          ▼
                                  GPU freed for the next task
```

Two concrete uses, in increasing order of effort:

1. **Few-shot** — take 5 hard cases the small model got wrong and the big
   model got right, paste them into `gmt/skills/<role>.md` under
   `## Examples`. Costs 20 minutes. Often closes half the gap.
2. **Fine-tune** — export a few thousand `(prompt, validated_output)` pairs
   from `artifact` joined to `task_run`, LoRA-tune a 3B, and serve it from
   the same Ollama. Costs a weekend. Do this only after (1) stops helping.

```sql
-- the export query for a fine-tune set
SELECT a.input_hash, a.value_json, r.raw_text, r.tier
FROM artifact a JOIN task_run r
  ON a.task = r.task AND a.input_hash = r.input_hash
WHERE a.task = 'extract_defect_fields' AND r.ok = 1 AND r.tier >= 2;
```

---

## Controlling cost and data residency

```bash
export GMT_MAX_TIER=2     # tier 3 becomes physically unreachable
```

Set this on any machine that must not send data outside the building. Tasks
that would have escalated to the cloud now fail loudly and set
`needs_review` instead of quietly exfiltrating a production note.

Per-task ceilings live in `config/routing.yaml`:

```yaml
    extract_defect_fields:  { start: 2, max: 3 }   # may reach the cloud
    classify_defect:        { start: 1, max: 2 }   # never leaves the GPU
```

---

## Performance notes for one GPU

| Setting | Where | Why |
|---------|-------|-----|
| `keep_alive: 30m` | `config/settings.yaml` | otherwise Ollama unloads after 5 min and your next batch pays a 10–30 s reload |
| `concurrency` | `config/settings.yaml` | beyond `OLLAMA_NUM_PARALLEL` requests queue invisibly inside Ollama. On 11 GB cards a 14B Q4 leaves no room for a second KV cache — measured at 91% full, so this is set to **1** |
| `temperature: 0.0` | `config/models.yaml` | extraction and classification are not creative tasks; nonzero temperature makes results unreproducible and therefore unscorable |
| `num_ctx` as small as fits | `config/models.yaml` | KV cache is VRAM; a 32k context you do not use still costs you |
| `OLLAMA_MAX_LOADED_MODELS=2` | Ollama's environment | **not the default.** Without it Ollama evicts tier 1 to load tier 2 and back again, and you pay a full model load on every switch |

---

## Multi-GPU: check which BACKEND each card gets

This one cost real throughput on `hanalytics` and is invisible unless you
look for it. Ollama chooses a compute backend **per GPU**, not per server:

```bash
# in the terminal running `ollama serve`, or its log
grep -oE 'library=(CUDA|Vulkan)|(CUDA|Vulkan)[0-9] : NVIDIA [^(]*' server.log | sort -u
```

Measured on `hanalytics`, Ollama 0.32.1, driver 550.163.01:

| GPU | Compute capability | Backend Ollama chose |
|-----|--------------------|----------------------|
| RTX 2080 Ti | 7.5 (Turing) | **CUDA** |
| GTX 1080 Ti | 6.1 (Pascal) | **Vulkan** |

Vulkan works, but it is the fallback path and it is slower than CUDA on the
same silicon. So on a mixed box you must care *which model lands where*.

**What went wrong here, and it is easy to reproduce:** the small 3B loaded
first and took the 2080 Ti (CUDA). The 14B then did not fit beside it, so it
went to the 1080 Ti — on Vulkan. The result was exactly backwards: the model
doing the heavy lifting was on the slow backend and the slow card, while the
fast card ran the tiny model.

**Load order does NOT fix it.** This was tested both ways on `hanalytics`:

| Load order | GTX 1080 Ti | RTX 2080 Ti |
|------------|-------------|-------------|
| 14B first, then 3B | 9057 MiB (**14B**) | 2247 MiB (3B) |
| 3B first, then 14B | 9057 MiB (**14B**) | 10 MiB (3B evicted) |

The big model lands on the 1080 Ti either way. The likely reason is that
Ollama ranks candidate GPUs by FREE VRAM, and the 1080 Ti reports more of
it (11379 MiB vs 10838 MiB) — so it wins regardless of order.

Note the second row: loading the small model first is actively worse, because
the 14B then evicts it. **Always start the big model first**, not to control
which card it gets, but so both models stay resident.

The one setting that genuinely helped:

```bash
OLLAMA_MAX_LOADED_MODELS=2 OLLAMA_NUM_PARALLEL=1 ollama serve
```

Before: loading tier 2 evicted tier 1 and vice versa, so every tier switch
paid a full model load. After: 3B (2.16 GB) and 14B (9.46 GB) both stay
resident.

**To actually pin a model to a card**, Ollama gives you nothing per-model —
you run one server per GPU and point each tier at its own port. Each tier in
`config/models.yaml` accepts a `host:`; the recipe is in the comments there.
Do it only if GPU time is measurably your bottleneck. Two servers is two
things to restart, monitor, and get wrong.

`OLLAMA_NUM_PARALLEL=1` matters on 11 GB cards: each parallel slot gets its
own KV cache, and a 14B Q4 at `num_ctx 8192` already sits at ~91% of the
card. See the VRAM arithmetic in `config/settings.yaml`.

Keeping both tiers resident beats swapping between them — but only if you
raise `OLLAMA_MAX_LOADED_MODELS`, because it is not the default.
