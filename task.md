# Task / Progress

Living progress board. Update as things move.
See `main.md` for the working agreement, `system.md` for the architecture,
`chat_history01.md` for the session handoff and the bug catalogue.

## Done — the harness itself

| # | Item | Where |
|---|------|-------|
| 1 | Contract layer: validate, coerce, JSON rescue, JSON Schema | `gmt/core/contracts.py` |
| 2 | Task ABC with 5 hooks + registry auto-discovery | `gmt/core/task.py`, `registry.py` |
| 3 | LLM clients: Ollama, Anthropic, deterministic Fake | `gmt/core/llm.py` |
| 4 | Router: tier choice, repair turn, escalation, caching | `gmt/core/router.py` |
| 5 | Runner: sync, async, chain, map_chain, concurrency cap | `gmt/core/runner.py` |
| 6 | Store: bronze/silver/gold, read-only guard, thread-local conns | `gmt/core/store.py` |
| 7 | 8 atomic tasks across text, SQL, graph and vision | `gmt/tasks/` |
| 8 | 6 prompt packs as editable markdown | `gmt/skills/` |
| 9 | 3 pipelines | `gmt/pipelines/` |
| 10 | MCP server, stdlib JSON-RPC, tools generated from the registry | `gmt/mcp/server.py` |
| 11 | CLI: doctor, init, tasks, ingest, ask, inspect, bench, stats | `gmt/cli.py` |
| 12 | Benchmark harness with cache isolation | `gmt/bench.py` |
| 13 | 44 tests, stdlib unittest, offline, ~0.6s | `tests/` |
| 14 | Rust crate for the frame-gate hot loop (optional) | `rust/gmt_fast/` |
| 15 | Docs 01–08 | `docs/` |

## Next — in order

### Phase 1 · get real data flowing (this is the only thing that matters now)

- [x] ~~Run `scripts/ollama_smoke.py` on the GPU server~~ — done 2026-08-21
- [x] ~~Tune `config/models.yaml` to the models actually installed~~ — tier 2
      now points at `qwen2.5-coder:14b`
- [x] ~~`ollama pull qwen2.5:3b`~~ — done 2026-08-21
- [x] ~~Restart Ollama with `OLLAMA_MAX_LOADED_MODELS=2
      OLLAMA_NUM_PARALLEL=1`~~ — done 2026-08-21. Both tiers now stay
      resident (3B 2.16 GB + 14B 9.46 GB), no more evict-and-reload.
- [ ] OPTIONAL, and only if GPU time becomes the bottleneck: pin the 14B to
      the 2080 Ti (CUDA) with the two-server recipe in `config/models.yaml`.
      Load order does NOT control placement — Ollama picks the card with the
      most free VRAM, and the 1080 Ti reports more. Unmeasured whether the
      Vulkan/1080 Ti path actually costs enough to care.
- [ ] **`ollama pull qwen2.5:14b`** — the installed 14B is `-coder`. It does
      well on these tasks, but the instruct variant is the right tool for
      messy natural-language notes. Worth a head-to-head `bench`.
- [ ] Clear the corrupt manifest Ollama warns about at startup:
      `registry.ollama.ai/library/fixing-qwen2.5-coder:Modelfile`
- [ ] Replace `data/seed/qa_reports.jsonl` with 200+ real QA notes
- [ ] **Grow `tests/golden/qa_golden.jsonl` to 100+ labelled rows.** This is
      now the single highest-value task. At n=16, the tier-1 vs tier-2 gap on
      `classify_defect` (0.88 vs 1.00) is two examples — not enough to decide
      anything. Everything else is blocked on being able to measure.
- [x] ~~`bench --tiers 1,2` → set real tiers in `routing.yaml`~~ — done,
      tiers now carry their measured justification in the config comments
- [ ] Nightly cron: `python3 -m gmt ingest`
- [ ] **Start capturing camera frames** (`scripts/capture_frames.py`) — longest
      lead time of anything on this list, so start it in phase 1

### Phase 2 · make it useful to the business

- [ ] ERP feed → `source_document(source_system='erp')`
- [ ] New task: `extract_po_fields` (tier 2)
- [ ] New task: `link_lot_to_roll` (tier 0, pure SQL)
- [ ] Static HTML dashboard off `metric_daily`, or point Metabase at the db
- [ ] New task: `route_question` (tier 1) — structured vs document

### Phase 3 · vision

- [ ] Label 200–500 crops per class from `data/frames/*/keep/`
- [ ] Train ResNet-18 / EfficientNet-B0 → TorchScript
- [ ] `export GMT_CNN_MODEL=...`, bump `DefectCNN.version`
- [ ] Tune `DEFAULTS` in `t_frame_gate.py` against a day of real footage
- [ ] Close the active-learning loop on the `needs_review` queue

### Phase 4 · later

- [ ] RAG over SOPs and manuals, behind `route_question`
- [ ] Knowledge-graph traversal queries (recursive CTEs, tier 0)
- [ ] Market signals into bronze — never into gold
- [ ] Fabric physics: property predictor first, simulation later

## Measured on hanalytics (2026-08-21)

| Fact | Value | Consequence |
|------|-------|-------------|
| GPUs | GTX 1080 Ti 11 GB + RTX 2080 Ti 11 GB | `concurrency: 2`, `num_ctx: 8192` max |
| Ollama | 0.32.1 | schema-constrained `format` **works** |
| Installed LLM | `qwen2.5-coder:14b` Q4_K_M (9.0 GB) only | no tier 1 yet |
| `qwen-fixed:latest` | same model, re-tagged copy | not a second model |
| Throughput | ~15.3 tok/s | `text_to_sql` takes 7–15 s per call |
| Cold start | ~6.7 s first call | `keep_alive: 30m` is doing real work |
| Contract pass rate | 7/7 tasks, first attempt, 0 escalations | the harness works end to end |
| `classify_defect` | t1 (3b) **0.88** @861ms · t2 (14b) **1.00** @2618ms | pinned t2 — see routing.yaml |
| `extract_defect_fields` | t1 **0.56/0.69/0.94/0.94** · t2 **0.94/1.00/1.00/1.00** | t1 not viable for extraction |
| Full ingest, 24 notes | 19 observations, 5 clean, 77 KG edges, **0 failures** | real model, end to end |
| Analytics round trip | NL question -> SQL -> rows -> grounded answer | verified with the real model |
| Escalation, end to end | t1 fails guard twice -> t2 returns 0.3048 (=12 in) | ladder verified on real models |
| Final ingest, 24 notes | 19 obs, 5 clean, **79 KG edges, 1 failed chain** | the 1 failure is a genuine model error, correctly rejected |
| Guard tuning, 3 runs | failed chains 5 -> 2 -> 1; edges 63 -> 77 -> 79 | each drop traced to a specific fix |
| Self-reported confidence | **always exactly 1.0** | `needs_review` was dead; now derived structurally |
| Embeddings available | `nomic-embed-text`, `embeddinggemma` | RAG phase already has what it needs |
| GPU backends | 2080 Ti → **CUDA**, 1080 Ti → **Vulkan** | Ollama picks per-GPU; Pascal falls back to Vulkan |
| Placement observed | 3B took the CUDA card, 14B pushed onto Vulkan | **backwards** — load the big model first |
| `OLLAMA_MAX_LOADED_MODELS` | unset (default) | Ollama evicts tier 1 to load tier 2; set it to 2 |

## Open questions for the company

- [ ] Where do QA notes live today — paper, tablet app, ERP, WhatsApp?
- [ ] Is there an existing defect taxonomy? If so, `_vocab.py` must match it.
- [ ] Which ERP, and can we get read access to a replica?
- [ ] Does data leaving the building need approval? (If yes: `GMT_MAX_TIER=2`.)
- [ ] What did the earlier RAG experiment use, and what did it get wrong?
- [ ] Existing camera hardware — resolution, frame rate, lighting, mounting?

## Decisions log

| Date | Decision | Why |
|------|----------|-----|
| 2026-08-21 | SQLite, not Postgres | one writer, one box. Swap point isolated in `store.py`. |
| 2026-08-21 | Own contract layer, not pydantic | zero-install requirement; migration path in `docs/05`. |
| 2026-08-21 | Text-to-SQL, not RAG, for structured questions | retrieval cannot COUNT or JOIN. |
| 2026-08-21 | No Rust yet | nothing is CPU-bound. Crate is written and waiting. |
| 2026-08-21 | Prompts in markdown, not Python | edited 50× more often than code; non-engineers can own them. |
| 2026-08-21 | `needs_review` no longer trusts model confidence | measured: the 14B returns 1.0 on everything. Now derived from severity, `other`, and thin evidence. |
| 2026-08-21 | `yarn_lot` split out of `roll_id` | the model put YL-88 in `roll_id` and was RIGHT — the contract said "roll / batch / lot". Fixed the contract, not the prompt. |
| 2026-08-21 | Structural KG edges derived in Python | the model dropped `machine --produced--> defect` despite a worked example. We already know both ends; it is a join, not an inference. |
