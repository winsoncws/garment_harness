# 02 — Concepts

The whole design, in the order it makes sense to learn it.

---

## 1. Contract — the shape of data

```python
Contract("classify_defect.out", {
    "label":      Field("str", enum=DEFECT_LABELS),
    "severity":   Field("int", minimum=1, maximum=5),
    "confidence": Field("float", minimum=0.0, maximum=1.0),
    "evidence":   Field("str", max_len=200),
})
```

A contract does four jobs, and each one earns its keep:

| Job | Where it shows up |
|-----|-------------------|
| **Validate** the model's answer | `router.run()` rejects and retries |
| **Coerce** sloppy output | `"3"` → `3`, `" Hole "` → `"hole"`, `"yes"` → `True` |
| **Generate the prompt** | `contract.prompt_block()` writes the "return this shape" text |
| **Constrain decoding** | `contract.json_schema()` goes to Ollama's `format=` |

That last one is worth pausing on. Ollama ≥ 0.5 accepts a JSON Schema and
constrains token sampling so the model *cannot* emit invalid JSON. You write
the contract once and get both prevention and detection.

**Coercion is not sloppiness.** A 3B model returning `"3"` instead of `3` is
not wrong about the fabric — it is wrong about JSON. Rejecting that answer
would throw away a correct classification over a formatting detail.

---

## 2. Task — one atomic job

```python
@register
class ClassifyDefect(Task):
    name = "classify_defect"
    version = 1
    desc = "Classify the single most significant defect in an inspection note."

    input  = Contract(...)      # what goes in
    output = Contract(...)      # what must come out
    skill  = "inspector"        # which persona (gmt/skills/inspector.md)

    def check(self, value, payload):   # business rules
        ...
```

**How small is atomic?**

| Verdict | Task |
|---------|------|
| good | `classify_defect` — text → one of 13 labels |
| good | `extract_defect_fields` — text → 8 named slots |
| good | `frame_gate` — frame → send to GPU, yes or no |
| bad  | `process_report` — "everything" |
| bad  | `analyse_and_recommend` — the "and" gives it away |

Three reasons to keep them tiny:

1. A 3B model nails a one-decision job and fumbles a five-decision job.
2. "Was this correct?" is answerable for a small task. It is not answerable
   for a big one — so you cannot measure, so you cannot improve.
3. When quality drops you know *which* task lost it, and you move that one
   task up a tier instead of moving everything.

**The six hooks:**

| Hook | Runs when | Typical use |
|------|-----------|-------------|
| `build_user()` | building the prompt | few-shot examples, custom layout |
| `run_local()` | **before any LLM** | regex/OpenCV/SQL answer — free |
| `stub()` | offline mode | keyword rules so the demo is readable |
| `repair()` | before `check()` | fix what is **unambiguously** fixable |
| `check()` | during validation | **business rules — the valuable one** |
| `postprocess()` | after validation passes | normalise IDs, derive flags |

**`repair()` vs `check()`** is a distinction worth getting right. Repair when
there is exactly one thing the model could have meant; reject when there is
more than one.

```python
# REPAIR: roll --observed_on--> defect
#   the relation only connects defect -> roll, so swap it. Safe.
# REJECT: yarn_lot --made_of--> fabric
#   illegal in BOTH directions -- the model invented a relationship,
#   it did not reverse one. Guessing here would corrupt the graph.
```

Rejecting something you could have fixed deterministically wastes a model
call. "Fixing" something ambiguous corrupts your data. Repair the first kind
only.

---

## 3. `check()` — where the real engineering lives

The type contract asks *"is this the right shape?"*. `check()` asks
*"is this consistent with the input?"* — and that is the question that
catches confident, well-formed, entirely invented answers.

Three patterns you should copy into every task you write:

**Grounding** — the model must be able to quote its evidence:

```python
if ev not in text and not _mostly_present(ev, text):
    errs.append(f"evidence {ev!r} does not appear in the note")
```

**Number provenance** — a figure that is not in the input is a hallucination:

```python
if pos and not _number_supported(pos, digits_in_text):
    errs.append(f"position_m={pos} is not supported by any number in the note")
```

**Referential sanity** — the model invented a table that does not exist:

```python
unknown = referenced_tables - _ALLOWED_TABLES
```

None of these need a model to run. They are ordinary Python, they are fast,
and they catch the failure mode that actually hurts you in production.

---

## 4. Skill — the persona, as an editable file

`gmt/skills/inspector.md`:

```markdown
---
name: inspector
tier_hint: 1
---

## System
You are a textile quality inspector's assistant in a garment mill.

## Guidelines
  - Choose exactly ONE label from the allowed list.
  - `evidence` MUST be copied word-for-word from the note.

## Examples
note: "L7 3rd shift oil at 88.5m"
json: {"label": "oil_mark", ...}
```

A task points at one with `skill = "inspector"`. If the file is missing, the
task's own `system = "..."` string is used instead — a prompt typo degrades,
it does not take the line down.

Why files: you will edit prompts fifty times more often than code, `git diff`
on markdown is readable, and the QA manager who actually knows what "slub"
means can edit a `.md` and never will edit a `.py`.

---

## 5. Router — which model, and what to do on failure

```
 payload
    |
    v  input contract       -- bad input fails fast, no model call
 tier 0: run_local()        -- free, instant. Answered? Done.
    |
    v  artifact cache       -- same task+version+input? Reuse. Done.
    |
    v  tier N ... max
        call model
        extract JSON        -- survives fences, chatter, trailing commas
        validate contract
        run check()
        pass -> store + trace -> DONE
        fail -> retry ONCE with the errors fed back in
             -> still failing? tier N+1
    |
    v  out of tiers -> needs_review = 1, a human looks
```

**The repair turn** is the part people miss. At temperature 0 a bare retry is
nearly deterministic and therefore pointless — but we do not do a bare
retry. We append:

```
Your previous answer was REJECTED. Fix exactly these problems:
- severity: 'high' is not an int
- evidence 'a gaping tear' does not appear in the note
```

That fixes a surprising share of small-model failures for a fraction of the
cost of escalating.

---

## 6. Runner — the for-loops

| Function | Shape | Use when |
|----------|-------|----------|
| `map_sync` | one at a time | learning, debugging, < 20 items |
| `map_parallel` | N at once, sync-looking | nightly batches |
| `map_async` | N at once, inside an event loop | a web service |
| `chain` | steps that feed each other | a pipeline for one item |
| `map_chain` | N whole pipelines at once | the real workhorse |

```
 map_sync (1 at a time)          map_async (concurrency 3)
   doc1 [====]                     doc1 [====]   doc4 [====]
   doc2       [====]               doc2 [====]   doc5 [====]
   doc3             [====]         doc3 [====]   doc6 [====]
   ... total ~12s                  ... total ~4s
```

**Do not set concurrency to 50.** Ollama serves a limited number of parallel
sequences; the rest queue *inside* Ollama where you cannot see them. 2–4 is
the sweet spot for one GPU.

---

## 7. Store — the single ground truth

```
BRONZE  source_document      raw, immutable, append-only
   |    source_blob          "what actually arrived"
   v
SILVER  artifact             every task output + provenance
   |    task_run             "what the models said, and how they did"
   v
GOLD    defect_observation   curated, joinable, business-facing
        kg_node / kg_edge    "what the dashboards and the chatbot read"
        metric_daily
```

The rule: **data flows down only**, and gold is always rebuildable from
bronze. That is what makes it safe to improve a prompt — you re-run and
compare, instead of hoping.

`artifact` is generic: one row per task output, keyed by
`(task, version, input_hash)`. Adding a new task **never requires a schema
migration**, and the unique key doubles as the cache.

---

## How it all fits

```
   raw note ──▶ Store.put_document()  ─────────────────────────▶ BRONZE
                        │
                        ▼
                   Pipeline (chain of Steps)
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
        classify_defect     extract_defect_fields
         (Task + Skill)        (Task + Skill)
              │                   │
              ▼                   ▼
            Router  ──▶ tier 1/2/3, validate, retry, escalate
              │
              ├──▶ artifact + task_run ────────────────────────▶ SILVER
              │
              ▼
        pipeline projection ──▶ defect_observation, kg_edge ───▶ GOLD
                                          │
                                          ▼
                          dashboards · chatbot · MCP · bench
```
