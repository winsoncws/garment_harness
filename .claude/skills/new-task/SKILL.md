---
name: new-task
description: Scaffold a new atomic task in the garment harness — contract, guards, stub, routing, and golden rows. Use when the user wants to add a capability like "classify urgency", "extract PO fields", "detect shade drift", or says "add a task".
---

# Add a task to the garment harness

Follow `docs/03_add_a_task.md`. Do not invent a different structure.

## Before writing any code, decide these five things and state them

1. **Is it atomic?** If the description needs the word "and", it is two
   tasks. Split it and say so.
2. **Is it tier 0?** Could a regex, a lookup table, or SQL answer it? If so,
   implement `run_local()` and write no prompt at all.
3. **What is the closed vocabulary?** Any output field that is conceptually
   a category MUST be an `enum`. Add it to `gmt/tasks/_vocab.py`, never
   inline in the task file.
4. **What are the two `check()` rules?** Minimum two. At least one must be a
   *grounding* rule that verifies the output against the input — the
   evidence appears in the text, the number appears in the source, the table
   exists. This is the rule that catches confident hallucination.
5. **What tier does it start at?** Guess conservatively (higher), and say
   that it must be verified with `bench` rather than trusted.

## Then produce, in this order

| # | File | Content |
|---|------|---------|
| 1 | `gmt/tasks/_vocab.py` | any new closed list |
| 2 | `gmt/tasks/t_<name>.py` | the task |
| 3 | `config/routing.yaml` | `<name>: { start: N, max: M }` |
| 4 | `gmt/skills/<role>.md` | only if no existing role fits |
| 5 | `tests/golden/*.jsonl` + `gmt/bench.py` `CHECKS` | so it is measurable |

## The task file must have

- a module docstring with a worked `input -> output` example and an ASCII
  arrow diagram, matching the style of the existing task files
- `name`, `version = 1`, `desc`, `input`, `output`, `skill`
- `check()` with the rules from step 4, each with a comment saying what
  failure mode it catches
- `stub()` using cheap keyword rules, so `make demo` stays meaningful
  offline. A stub returning the bare contract example is not acceptable.
- `postprocess()` for any deterministic normalisation — never ask the model
  to format anything
- `repair()` if some invalid outputs have exactly one legal reading (a
  reversed edge, a swapped pair). Do NOT use it to guess at ambiguous ones —
  that is invention, not repair

## Then verify, and show the output

```bash
python3 -m gmt tasks | grep <name>
python3 tests/run_tests.py
python3 -m gmt --offline ingest      # if you added it to a pipeline
```

The parametric tests in `tests/test_tasks.py` cover every registered task
automatically — if `test_every_stub_satisfies_its_own_contract` or
`test_every_task_has_a_routing_entry` fails, the task is not finished.

## Do not

- add an import anywhere; `@register` plus the file location is the wiring
- put a free-text field where an enum belongs
- put formatting or unit conversion in the prompt
- claim a tier without running `bench`
