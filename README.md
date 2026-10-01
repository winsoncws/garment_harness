# Garment AI Harness

An agentic harness for AI in a textile garment plant. Built so that **most of
the work is done by small local models on your own GPU**, and only the hard
parts reach a frontier model.

> **Runs on a bare Python 3.9 with zero packages installed.** No pip, no venv,
> no GPU needed to try it. Copy the folder to your GPU server and it works.

```bash
make demo          # the whole thing, offline, ~2 seconds
```

---

## Three minutes to understanding

There are five nouns. That is the entire mental model.

| Noun         | One line                                        | Where            |
|--------------|-------------------------------------------------|------------------|
| **Contract** | The shape of data going in and coming out.       | `gmt/core/contracts.py` |
| **Task**     | ONE atomic job. Contract in → contract out.      | `gmt/tasks/`     |
| **Skill**    | The persona/prompt a task uses. A markdown file. | `gmt/skills/`    |
| **Pipeline** | Tasks wired into a workflow.                     | `gmt/pipelines/` |
| **Store**    | The single ground truth. One SQLite file.        | `gmt/core/store.py` |

And three pieces of machinery that you configure but rarely edit:

| Machinery  | Job                                                    |
|------------|--------------------------------------------------------|
| **Router** | Picks which model tier runs a task. Escalates on failure.|
| **Runner** | The for-loops: sync, async, and task chaining.          |
| **Trace**  | Records every run. This becomes your evaluation dataset.|

```
             +-----------+
   payload   |   TASK    |   declares its input shape, its output shape,
   (a dict)  |           |   and the prompt. ~40 lines. That is all.
             +-----------+
                   |
                   v
             +-----------+   tier 0 python?  cached?  tier 1?  tier 2?  tier 3?
             |  ROUTER   |   validate the answer. reject it. retry. escalate.
             +-----------+
                   |
                   v
             +-----------+
             |   STORE   |   bronze (raw) -> silver (every output) -> gold
             +-----------+                                   (business tables)
```

---

## The idea the whole repo is built on

**Split work into jobs so small that a 3B model can do them, then check every
answer against a contract.** A rejected answer is retried with the error fed
back; if it still fails, it climbs to a bigger model automatically.

| Tier | Where          | Model            | Good at                        | Cost |
|------|----------------|------------------|--------------------------------|------|
| 0    | pure python    | —                | regex, OpenCV, SQL, lookups    | free |
| 1    | your GPU       | `qwen2.5:3b`     | classify, tag, route           | free |
| 2    | your GPU       | `qwen2.5:14b`    | extract, summarize, write SQL  | free |
| 3    | cloud          | `claude-sonnet-5`| plan, design, ambiguity        | $$   |

You do not guess which tier a task needs. You measure it:

```bash
python3 -m gmt bench --tiers 1,2
```

Measured on `hanalytics` with `qwen2.5-coder:14b`, 16 golden rows, 2026-08-21:
`classify_defect` **16/16**, `extract_defect_fields` 0.94 machine / 1.00
shift / 1.00 roll / 1.00 yarn lot, **zero contract failures in 32 calls**.
See `docs/04_model_routing.md` for how three of those misses were diagnosed
— only one was actually a prompt problem.

---

## Getting started

```bash
# 1. Does everything work? (checks python, ollama, models, db)
python3 -m gmt doctor

# 2. Try it with no GPU at all -- a deterministic fake model stands in
make demo

# 3. Point it at your GPU box and do it for real
export GMT_OLLAMA_HOST=http://<gpu-ip>:11434
python3 -m gmt init
python3 -m gmt ingest
python3 -m gmt ask "which loom has the most oil marks?"
python3 -m gmt stats
```

**`make demo` is the fastest way to understand this repo.** It ingests 24
deliberately messy shop-floor notes, classifies and extracts from each one,
builds a knowledge graph, answers a question in SQL, and prints how every
model call performed.

---

## What is in the box

```
garment/
├── README.md          you are here
├── main.md            the working charter -- how we work on this
├── system.md          the architecture map -- what talks to what
├── task.md            the progress board -- what is next
├── chat_history01.md  session handoff -- goal, measured facts, bugs found
│
├── docs/              read these in order, 01 -> 08
├── config/            settings.yaml, models.yaml, routing.yaml
├── data/              schema.sql + seed notes; the .db lands here
│
├── gmt/               the package
│   ├── core/          contracts, task, registry, llm, router, runner, store
│   ├── tasks/         8 atomic tasks -- the units of work
│   ├── skills/        6 prompt packs as editable markdown
│   ├── pipelines/     3 workflows
│   ├── mcp/           MCP server -- exposes every task to any agent
│   ├── cli.py         python -m gmt <command>
│   └── bench.py       tier comparison against labelled data
│
├── scripts/           helper scripts (frame capture, ollama warmup)
└── tests/             44 tests, stdlib unittest, ~1 second, no GPU
```

### The 8 tasks that ship

| Task                     | Tier | What it does                              |
|--------------------------|------|-------------------------------------------|
| `normalize_units`        | 0    | "3 yards at 120m" → metres. No LLM.       |
| `frame_gate`             | 0    | OpenCV: is this frame worth the GPU?      |
| `defect_cnn`             | 0    | GPU CNN shell — plug your weights in      |
| `classify_defect`        | 1    | note → 1 of 13 defect labels + severity   |
| `answer_from_rows`       | 1    | query rows → one grounded sentence        |
| `extract_defect_fields`  | 2    | note → machine, shift, roll, position     |
| `extract_triples`        | 2    | note → knowledge-graph edges              |
| `text_to_sql`            | 2    | question → one read-only SELECT           |

### The 3 pipelines

| Pipeline              | Flow                                          | Covers                       |
|-----------------------|-----------------------------------------------|------------------------------|
| `p_qa_ingest`         | notes → classify → extract → gold + graph      | chatbot, KG, analytics       |
| `p_defect_inspection` | frames → CPU gate → GPU CNN → gold             | fabric defect vision         |
| `p_analytics_ask`     | question → SQL → execute → grounded answer     | business + supply chain      |

---

## Where to read next

| If you want to…                            | Read                          |
|--------------------------------------------|-------------------------------|
| get it running in 10 minutes                | `docs/01_quickstart.md`       |
| understand the design properly              | `docs/02_concepts.md`         |
| **add your own task** (the common case)     | `docs/03_add_a_task.md`       |
| decide small model vs big model             | `docs/04_model_routing.md`    |
| understand the database + single-truth idea | `docs/05_data_contracts.md`   |
| let an agent call your tasks                | `docs/06_mcp_and_agents.md`   |
| see how your 7 initiatives map onto this    | `docs/07_roadmap.md`          |
| know when Rust is worth it (and learn it)   | `docs/08_rust_later.md`       |

---

## Design rules this repo follows

1. **Never trust an LLM. Validate every output.** A contract violation is a
   normal event to be handled, not an exception to be logged.
2. **Tier 0 first.** If a regex, a lookup table or SQL can do it, that is the
   correct implementation. Reach for a model for the part that genuinely
   needs language.
3. **Closed vocabularies everywhere.** Free-text labels destroy your ability
   to group, join, evaluate, and build a graph.
4. **Raw data is immutable.** Every derived thing can be rebuilt from bronze.
5. **Every run is traced.** The trace table is your eval set and your
   fine-tuning corpus. It costs nothing and cannot be recovered later.
6. **Deterministic work stays in python.** Formatting, unit conversion and ID
   normalisation must never be delegated to a model.
7. **Fail open on the safety-critical path, loud everywhere else.** A vision
   gate that cannot see sends the frame to the GPU. It never drops it quietly.
