---
name: tune-prompt
description: Improve a task's accuracy in the garment harness by measuring first, changing one thing, and measuring again. Use when a task is misclassifying, hallucinating fields, escalating too often, or the user says a prompt "isn't working".
---

# Tune a prompt — measure, change one thing, measure

The rule: **no prompt change is accepted without a before and after number.**
A change that feels better and measures the same is how prompt files rot.

## 1. Measure the baseline first

```bash
python3 -m gmt bench --tiers 1,2
python3 -m gmt stats
```

Record the numbers before touching anything. State them explicitly.

## 2. Read what the model actually said

This is the step people skip, and it is where the answer usually is.

```sql
SELECT tier, errors, raw_text
FROM task_run
WHERE task = '<name>' AND ok = 0
ORDER BY run_id DESC LIMIT 10;
```

```bash
python3 -m gmt ask --no-narrate "..."   # or query the db directly
```

Nine times out of ten the model was reasonable and something else is wrong.
Diagnose before you edit:

| What `raw_text` shows | Real cause | Fix |
|-----------------------|-----------|-----|
| valid JSON, wrong enum spelling | vocabulary gap | add the mill's wording to `## Examples` in the skill |
| valid JSON, invented a quote | grounding failure | strengthen the "copy word-for-word" line; the `check()` is already doing its job |
| prose wrapped around JSON | schema `format` not applied | check Ollama ≥ 0.5; `python3 scripts/ollama_smoke.py` |
| a field filled that should be empty | absent-field problem | strengthen the null rule; add an example WITH empty fields |
| right answer, contract rejected it | **the contract is wrong** | fix the contract, not the prompt |

That last row matters. Do not tune a prompt to satisfy a bad contract.

## 3. Change exactly one thing

In order of expected value:

1. **Add 2–3 worked examples** to `## Examples` in `gmt/skills/<role>.md`,
   drawn from actual failures. Highest yield by a wide margin.
2. **Tighten a `Field(desc=...)`** — it goes into the prompt and the schema.
3. **Narrow an enum** or add the missing member.
4. **Rewrite a `## Guidelines` rule** to be concrete and negative
   ("never invent an id") rather than aspirational ("be accurate").
5. **Only then** restructure `build_user()`.

## 4. Bust the cache, then re-measure

```bash
python3 -m gmt bench --tiers 1,2
```

`bench` already passes `use_cache=False`. But if you are testing through
`ingest` or `ask`, **bump the task's `version`** or you will be comparing
against cached answers from the old prompt and see no change at all.

## 5. Decide

| Result | Do |
|--------|-----|
| accuracy up | keep it; note the before/after in `task.md` |
| accuracy flat | **revert.** Unmeasured changes accumulate into noise |
| accuracy up at tier 1 only | consider pushing the task down a tier — free speed |
| still failing at tier 2 | the task may not be atomic. Re-read `docs/03` |

## Report back with

- the before and after `bench` table
- the single change made
- the specific failures it fixed, quoted from `raw_text`
