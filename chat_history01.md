# Chat History 01 — building the garment AI harness (2026-08-21)

Handoff for a future session. Not a transcript — only what you need to be
useful immediately. See `main.md` (working agreement), `system.md`
(architecture), `task.md` (live status + next steps).

---

## The goal

Winson is joining a **textile garment manufacturer's ML department** to build
AI capability from scratch. The seven initiatives are **his plan, not the
company's**: fabric-defect camera vision, delegated CV (cheap OpenCV gate →
GPU CNN), an internal chatbot (the team already tried RAG), supply-chain
analytics, business analytics, market analysis, a knowledge graph, and
eventually fabric physics.

He asked for an **agentic workflow / harness** as a kickstart, with these
constraints, all of which shaped the design:

- **Beginner-legible.** He is strong in Python, basic HTML/JS, new to this.
  Files open with comment blocks containing tables and ASCII diagrams.
- **Modules reusable and swappable**, so all company data lands in one
  database as a single source of truth.
- **Atomic tasks**, because ongoing work should be delegable to small
  quantized models on his Ollama box.
- **Rust allowed, but only with step-by-step teaching.**
- Not everything needs a local model; escalating to Claude is fine.

## What was built

`ARS/claude/garment/` — 73 files, ~9,600 lines, **46 tests**.

Organising idea: *atomic tasks with validated contracts, routed to the
cheapest model tier that passes, all writing into one SQLite ground truth
(bronze → silver → gold).*

| | |
|---|---|
| 8 tasks | `gmt/tasks/` — tiers 0–3, each with contracts + `check()` guards |
| 6 prompt packs | `gmt/skills/*.md`, python `system` string as fallback |
| 3 pipelines | QA ingest · camera inspection · analytics ask |
| MCP server | `gmt/mcp/server.py`, 10 tools generated from the registry |
| CLI | `python3 -m gmt {doctor,init,tasks,ingest,ask,inspect,bench,stats}` |
| Docs | `docs/01`–`08`, plus 3 Claude Code skills in `.claude/skills/` |
| Rust | `rust/gmt_fast/` — written, deliberately unused (see below) |

Entry point for a human: `make demo` (2 s, no GPU), then `docs/01` → `docs/03`.

---

## Design rules — do not quietly drop these

1. **Zero mandatory dependencies in core.** Winson's Mac had *nothing*
   installed. Hence the hand-rolled `contracts.py`, the mini-YAML parser in
   `config.py`, sqlite3, and urllib. This is why it can be scp'd to any box
   and run immediately. Pydantic/PyYAML/DuckDB are optional upgrades with
   documented migration paths. **Do not "improve" this by adding pip deps.**
2. **Closed vocabularies live only in `gmt/tasks/_vocab.py`.**
3. **Deterministic work stays in Python** — unit conversion, ID
   normalisation, aggregation, derivable graph edges. Never in a prompt.
4. **Prompts live in `gmt/skills/*.md`.** Editing a prompt is not a code change.
5. **Never benchmark with the artifact cache on.** `bench.py` passes
   `use_cache=False, persist=False`. A tier-2 run reading a tier-1 cached
   answer makes the whole comparison a lie.
6. **Bump `task.version` on any contract change** — the version is inside
   `input_hash`, so it correctly invalidates cached artifacts.

## The hardware (measured, not assumed)

`ssh biomind@27.54.63.214` — host `hanalytics`, key auth already set up.
Ollama runs in tmux session `o`. Harness lives at `~/garment/`.

- **GPUs**: GTX 1080 Ti 11 GB (compute 6.1) + RTX 2080 Ti 11 GB (7.5).
- **Ollama 0.32.1 picks a backend per card**: 2080 Ti → CUDA, 1080 Ti →
  **Vulkan** (Pascal is not CUDA-eligible in this build).
- **Placement is by free VRAM, not load order.** The 1080 Ti reports more
  free, so the big model always lands there, on the slow path. Verified both
  load orders. Loading big-first only prevents the small model being evicted.
- **`OLLAMA_MAX_LOADED_MODELS=2 OLLAMA_NUM_PARALLEL=1`** is now set and is
  required to keep both tiers resident.
- 14B Q4_K_M at `num_ctx 8192` = **10.27 GB on an 11.26 GB card (91%)**.
  A second parallel slot does not fit; Ollama spills to system RAM silently
  and gets ~4x slower with no error.
- Models: `qwen2.5:3b`, `qwen2.5-coder:14b` (a **code** model — there is no
  plain `qwen2.5:14b`). `qwen-fixed` is a dead-end copy Winson made trying to
  fix tool-calling; he plans to delete it. Also `nomic-embed-text` and
  `embeddinggemma`, ready for the RAG phase.
- **Schema-constrained output (`format` = JSON Schema) works reliably.**
  Ollama's `tools` API on the same model does not — it returns tool calls as
  prompt text. The harness uses `format` exclusively; do not switch to `tools`.

## Measured results

Golden set (16 rows), `python3 -m gmt bench`:

| task | field | tier 1 (3b) | tier 2 (14b coder) |
|---|---|---|---|
| `classify_defect` | label | 0.88 @ 861 ms | **1.00** @ 2618 ms |
| `extract_defect_fields` | machine | 0.56 | **0.94** |
| | shift | 0.69 | **1.00** |
| | roll_id / yarn_lot | 0.94 / 0.94 | **1.00 / 1.00** |

Full 24-note ingest, final state: 19 observations, 5 clean, **79 KG edges,
1 failed chain** (a genuine model error, correctly rejected). Analytics round
trip verified — the model even found and used the `v_defect_daily` view.

`classify_defect` is pinned to **tier 2** despite the 3× speed gap, because
n=16 makes 0.88 vs 1.00 a two-example difference, and because classification
gates the whole pipeline. The reasoning is written into `config/routing.yaml`.

---

## Bugs found — the most valuable section

Everything passed offline. **Ten bugs surfaced only against the real model**,
and every one is a mistake a future session could repeat.

| # | Bug | Lesson |
|---|-----|--------|
| 1 | `doctor` said missing models were FOUND — `"qwen2.5-coder:14b".startswith("qwen2.5")` | a false READY is worse than a false MISSING |
| 2 | `needs_review` permanently dead — the 14B returns `confidence: 1.0` on everything | never build a safety net on self-reported confidence |
| 3 | Model put `YL-88` in `roll_id` — **my contract** said "roll / batch / **lot**" | when the model disagrees with the contract, check the contract first |
| 4 | A golden label was simply wrong | your eval set is not automatically right |
| 5 | 24 identical 404s — a missing model retried per item | permanent failures should be learned once |
| 6 | A test's `tearDown` deleted the suite-wide `GMT_OFFLINE` | save-and-restore env, never delete |
| 7 | Guard rejected a **correct** unit conversion (3 yd → 2.7432 m) | a guard too strict fails *closed* and eats good answers |
| 8 | KG relation *shapes* unvalidated — `yarn_lot --supplied_by--> machine` | validate combinations, not just field types |
| 9 | Validated against a vocabulary never shown to the model | if you check against a list, put the list in the prompt |
| 10 | Passed `json.dumps(fields)` as a hint → model emitted a node named `roll_id` | never show field NAMES to a model you are asking for VALUES |

Plus a performance bug worth its own line: **the CLI hardcoded
`--concurrency 4`, overriding `runtime.concurrency: 1`.** That alone was a 4×
slowdown (12.4 s → 2.9 s per call) from KV-cache spill. A default that
contradicts the config file is worse than no default.

### Two principles that came out of this

**Escalation catches malformed output, never merely wrong output.** A
confident, well-typed, incorrect label passes validation and is stored, and
nothing notices. Only the golden set catches that. This is the single most
important limitation of the routing design and it is documented in `docs/04`.

**Repair vs reject** (`Task.repair()`, the 6th hook): repair when there is
exactly one legal reading — `roll --observed_on--> defect` can only have
meant the reverse. Reject when there is more than one — `yarn_lot --made_of-->
fabric` is illegal both ways, so the model invented a relationship rather than
reversing one. Rejecting what you could fix wastes a call; "fixing" the
ambiguous corrupts data.

---

## Current state

- Local and `~/garment/` on the server are **in sync**. 46 tests pass on both.
- `make demo` works offline in ~2 s. `python3 -m gmt doctor` is the first
  thing to run on any machine.
- Nothing is half-finished. Two things are deliberately *not* done:
  - **GPU pinning.** Per-tier `host:` is wired into `config/models.yaml` with
    a two-server recipe. Winson said "nevermind this first, as long as it
    works." Revisit only if GPU time becomes the bottleneck.
  - **Rust.** `rust/gmt_fast/` is a complete, buildable PyO3 crate for the
    frame-gate hot loop, with line-by-line teaching comments. Nothing is
    CPU-bound yet, so it is unused on purpose. `docs/08` says when to reach
    for it (4+ cameras) and what to try first (`multiprocessing`, `numba`).

## What is NOT real yet

- **`defect_cnn` has no weights.** The pipeline, ROI cropping, review
  flagging and provenance are real; the model is a shell.
- **`frame_gate` has never run on a real image** — neither machine has
  OpenCV. The logic (median+MAD profile scan, fail-open) is sound but its
  thresholds are untuned guesses.
- **The golden set is 16 rows.** Too small for any real decision.
- The 24 seed notes in `data/seed/` are synthetic, written by Claude.

## Next steps (see `task.md` for the full list)

1. **Grow `tests/golden/qa_golden.jsonl` past 100 rows.** Everything else is
   blocked on being able to measure. Cheapest source: the `needs_review`
   queue — already the hard cases, pre-selected.
2. Replace the synthetic seed notes with real QA notes.
3. **Start capturing camera frames now** (`scripts/capture_frames.py`) — the
   CNN is months away but data collection has a lead time code does not.
4. ERP feed → `yarn_lot` is already a first-class column and
   `defect --caused_by--> yarn_lot` edges already populate, so "which
   supplier correlates with slub defects" is one join away.
5. `route_question` (tier 1) to split structured questions (text-to-SQL) from
   document questions (RAG) — this is what makes the team's earlier RAG work
   useful instead of replacing it.

## Open questions for the company

- Where do QA notes live today — paper, tablet, ERP, WhatsApp?
- Is there an existing defect taxonomy? `_vocab.py` must match it if so.
- Which ERP, and can we get read access to a replica?
- Does data leaving the building need approval? (If yes: `GMT_MAX_TIER=2`.)
- What did the earlier RAG experiment use, and what went wrong?
- Camera hardware — resolution, frame rate, lighting, mounting?

## Working style notes

- Winson gave SSH access and is comfortable with Claude driving the server.
  He did ask to pause testing once while restarting Ollama — ask before
  restarting long-running processes or pulling multi-GB models.
- He mirrors a `main.md` / `system.md` / `task.md` convention across projects
  and likes markdown tables and ASCII diagrams. Match it.
- He notices and cares about *why*, not just what. Explain the mechanism.
