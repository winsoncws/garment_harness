# System / Architecture

Reference map of the garment harness. Durable — update this when a module
moves or the dependency direction changes.

## In one line

**Small atomic tasks, each with a validated contract, routed to the cheapest
model that can pass it, all writing into one SQLite ground truth.**

## Dependency direction

Arrows point *from* the importer *to* the imported. Nothing points back up.

```
                     config.py        contracts.py
                    (settings)        (the shapes)
                         |                  |
                         +--------+---------+
                                  |
                    +-------------+-------------+
                    |             |             |
                 llm.py        task.py       store.py
              (ollama/         (the ABC)     (sqlite)
               anthropic)          |             |
                    |              |             |
                    |         registry.py        |
                    |         (name -> task)     |
                    |              |             |
                    +------+-------+------+------+
                           |              |
                       router.py      trace.py
                    (tier + retry)   (logging)
                           |
                       runner.py
                   (sync/async loops)
                           |
              +------------+------------+
              |            |            |
         gmt/tasks/   gmt/pipelines/  gmt/mcp/
              |            |            |
         gmt/skills/   gmt/cli.py   gmt/bench.py
```

**The rule:** `gmt/core/` must never import from `gmt/tasks/` or
`gmt/pipelines/`. Core is the foundation; everything else is a plugin.
(`router.py` imports `registry`, which imports task *modules* lazily at call
time — that is the one deliberate late binding, and it is what keeps core
free of any knowledge of the domain.)

## Module responsibilities

| Module               | Owns                                        | Does NOT own                |
|----------------------|---------------------------------------------|-----------------------------|
| `core/contracts.py`  | shapes, coercion, JSON rescue, JSON Schema  | anything domain-specific    |
| `core/task.py`       | the Task ABC and its 5 hooks                | model choice, retries, I/O  |
| `core/registry.py`   | name → task instance                        | execution                   |
| `core/llm.py`        | HTTP to Ollama / Anthropic; the fake client | prompts, validation         |
| `core/router.py`     | tier choice, retry, escalation, caching     | looping over many items     |
| `core/runner.py`     | sync/async loops, chaining, concurrency     | what a single call does     |
| `core/store.py`      | ALL SQL against the warehouse               | business logic              |
| `core/config.py`     | settings + env overrides + mini-YAML        | validation of task output   |
| `tasks/*`            | one job each: contract + prompt + checks    | which model, where it runs  |
| `skills/*.md`        | personas, rules, few-shot examples          | code                        |
| `pipelines/*`        | ordering, branching, gold projection        | prompts, retries            |
| `mcp/server.py`      | JSON-RPC transport                          | any business logic          |

## Data layers (see `data/schema.sql`)

```
BRONZE   source_document, source_blob      raw, immutable, append-only
   |                                       "what actually arrived"
   v     (a Task runs)
SILVER   artifact, task_run                every task output + full provenance
   |                                       "what the models said, and how"
   v     (a projection)
GOLD     defect_observation, kg_node,      curated, joinable, business-facing
         kg_edge, metric_daily             "what the dashboards read"
```

Data flows **down only**. Gold is always rebuildable from bronze + the current
task versions. That is what makes it safe to improve a prompt.

## The escalation ladder

```
   task starts at routing.yaml:start
        |
        v
   +---------+  ok   +---------------------+  pass
   | tier N  |-----> | validate contract   |------> store + trace  DONE
   +---------+       | + task.check()      |
        ^            +---------------------+
        |                      | fail
        |   retry once with    v
        +--- the error fed ----+
             back in           | still failing
                               v
                        tier N+1 (up to routing.yaml:max,
                                  capped by GMT_MAX_TIER)
                               |
                               v  out of tiers
                        needs_review = 1  ->  a human looks
```

## External surfaces

| Surface        | Entry point                | Used by                      |
|----------------|----------------------------|------------------------------|
| CLI            | `python -m gmt <cmd>`      | you, cron, CI                |
| Python API     | `import gmt; gmt.run(...)` | notebooks, other services    |
| MCP (stdio)    | `python -m gmt.mcp.server` | Claude Code, any agent       |
| Database       | `data/garment.db`          | BI tools, dashboards, pandas |

## Configuration precedence

```
config/*.yaml  ->  environment variables  ->  function arguments
(committed,        (per-machine:            (per-call override,
 same for all)      GPU box vs laptop)       used by bench + tests)
```

## Deliberate non-choices

| We did not use | Why not (yet)                                                 |
|----------------|---------------------------------------------------------------|
| pydantic       | zero-install requirement. `contracts.py` covers our 5 needs.   |
| LangChain      | it would hide exactly the routing/validation logic we teach.   |
| a vector DB    | structured questions need SQL, not retrieval. RAG comes later. |
| Postgres       | one writer, one box. See `store.py` for the move-off signals.  |
| Celery / Ray   | `asyncio.Semaphore` covers a single GPU box comfortably.       |
| Rust           | nothing is CPU-bound yet. See `docs/08_rust_later.md`.         |
