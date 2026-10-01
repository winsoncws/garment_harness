# Project Overview & Working Agreement

The charter for the garment AI harness.
Read `system.md` for the architecture map, `task.md` for current progress,
and `chat_history01.md` for how the harness got here (measured hardware
facts, and the ten bugs that only appeared against a real model).

## Goal

Stand up the AI/ML capability for a textile garment manufacturer from
scratch, as a **single integrated system** rather than seven disconnected
proofs-of-concept. One database, one task abstraction, one routing policy —
so that the defect-vision work, the chatbot, and the supply-chain analytics
all read and write the same ground truth.

## Standing preferences (how to work)

1. **Beginner-legible over clever.** Every non-obvious file opens with a
   comment block containing a table or an ASCII diagram. If a design needs
   explaining, explain it in the file, not in a wiki nobody opens.
2. **Zero mandatory dependencies in core.** The harness must run on a bare
   `python3` so it can be copied to any machine and tried immediately.
   Optional extras degrade gracefully, never crash.
3. **Measure before deciding.** No tier assignment, prompt change, or model
   swap is accepted on vibes. `python3 -m gmt bench` or it did not happen.
4. **Atomic tasks.** If a task description needs the word "and", it is two
   tasks. This is what makes small quantized models viable.
5. **Validate everything a model says.** Type contract first, then business
   rules in `check()`, then — for SQL — execution against the real database.
6. **Deterministic work belongs in Python.** Unit conversion, ID
   normalisation, aggregation, and formatting never go in a prompt.
7. **Flag, don't silently change.** If a change alters stored output shape,
   bump `task.version` (and `project.schema_version` for schema changes) so
   old rows stay interpretable.
8. **Trace everything.** `task_run` is the eval set and the fine-tuning
   corpus. It is cheap to write and impossible to reconstruct later.

## Division of labour: which model does what

| Work                                              | Who        |
|---------------------------------------------------|------------|
| designing a new task, contract and guards          | Claude (T3)|
| writing the first prompt for a task                | Claude (T3)|
| deciding a tier                                    | `bench.py` |
| classify / tag / route, high volume                | qwen 3b (T1)|
| extract / summarize / write SQL                    | qwen 14b (T2)|
| unit conversion, ID normalisation, aggregation     | Python (T0)|
| looking at ambiguous cases flagged `needs_review`  | a human    |

The point of the harness is to keep pushing work *down* this table over time,
using the trace data to prove it is safe.

## Conventions adopted

- **Type hints** on every function; `from __future__ import annotations` at
  the top of every module (Python 3.9 compatibility).
- **Naming**: tasks are `snake_case` verbs (`classify_defect`); task files
  are `t_<name>.py`; pipeline files are `p_<name>.py`.
- **Closed vocabularies** live in `gmt/tasks/_vocab.py` and nowhere else.
- **Prompts live in `gmt/skills/*.md`**, not in Python strings. The Python
  `system` attribute is the fallback if the file is missing.
- **Tests are stdlib `unittest`** and always run offline. `python3
  tests/run_tests.py` must stay under two seconds.
- **Never `print()` in `gmt/mcp/server.py`** — stdout is the protocol.

## The trap to keep watching

**Cache poisoning during evaluation.** `router.run()` caches by
`(task, version, input_hash)`. If a benchmark forgets `use_cache=False`, a
tier-2 run silently returns a tier-1 answer and the comparison is
meaningless. `bench.py` passes `use_cache=False, persist=False` — keep it
that way, and do the same in any new evaluation code.
