# 06 — MCP: letting an agent call your tasks

Every task you write becomes an MCP tool with no extra code. That means
Claude Code (or any agent) can call your real tasks against your real
database instead of guessing about your data.

---

## What MCP actually is

A JSON-RPC 2.0 conversation over stdin/stdout. That is the whole protocol.
`gmt/mcp/server.py` implements it in ~200 lines of stdlib so you can read it
and see there is nothing hidden.

```
Claude Code  ──(spawns)──▶  python3 -m gmt.mcp.server
     │                              │
     │  {"method":"tools/list"}     │
     │─────────────────────────────▶│
     │  {"tools":[ ... 10 tools ]}  │  ◀── generated from registry.all()
     │◀─────────────────────────────│
     │  {"method":"tools/call",     │
     │   "params":{"name":          │
     │    "classify_defect", ...}}  │
     │─────────────────────────────▶│──▶ router.run() ──▶ Ollama ──▶ store
     │  {"content":[{"text": ...}]} │
     │◀─────────────────────────────│
```

**One rule that breaks everyone once:** stdout carries protocol only. A
stray `print()` corrupts the stream. Log to stderr. `server.py` never calls
`print()` for exactly this reason.

---

## Register it with Claude Code

```bash
cd garment
claude mcp add garment -- python3 -m gmt.mcp.server
```

Then in a Claude Code session:

> *"Use the garment tools. Classify this note and tell me which machine it
> came from: 'L7 3rd shift, roll RB-4412, oil at 88.5m, 2 pcs'"*

> *"Query the defect database — which shift has the worst average severity
> on denim, and how confident are those classifications?"*

---

## What is exposed

**Tools** — 8 generated from the registry, 2 hand-written:

| Tool | Source |
|------|--------|
| `classify_defect`, `extract_defect_fields`, `extract_triples`, `text_to_sql`, `answer_from_rows`, `normalize_units`, `frame_gate`, `defect_cnn` | auto, from `registry.all()` |
| `ask_analytics` | question → SQL → rows → grounded answer |
| `run_sql` | read-only SELECT against the ground truth |

Add a task file and it appears in this list on the next server start. That
is the payoff of the registry.

**Resources** — read-only context an agent can pull in:

| URI | Contents |
|-----|----------|
| `garment://schema` | live `CREATE TABLE` text |
| `garment://vocab` | the closed vocabularies |
| `garment://stats` | current row counts |

---

## Test it without an agent

The protocol is newline-delimited JSON, so `echo` and a pipe are a complete
test client:

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{}}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"classify_defect","arguments":{"text":"loom 7 big oil smear 2m"}}}' \
 | GMT_OFFLINE=1 python3 -m gmt.mcp.server
```

---

## Safety

`run_sql` executes SQL that may have come from a model. Three independent
guards, in `store.py`:

1. the connection is opened `mode=ro` — **the database engine itself**
   refuses writes, not a regex we hope is complete
2. exactly one statement — no `SELECT 1; DROP TABLE artifact`
3. a keyword blocklist — for a clear error message rather than a crash

Guard 1 is the real one. Guards 2 and 3 exist so failures are legible.

---

## Which model should drive the agent?

| Job | Driver | Why |
|-----|--------|-----|
| designing a new task, debugging a bad prompt | Claude (tier 3) | judgement, and it happens rarely |
| a fixed nightly pipeline | **no agent at all** | `python3 -m gmt ingest` in cron. An agent adds cost and non-determinism to a job that has neither |
| the internal chatbot | tier 1 router + tier 2 SQL | `p_analytics_ask` already is the agent, and it is deterministic enough to test |

**The most common mistake in agentic engineering is using an agent where a
`for` loop would do.** Agents are for open-ended decisions. A pipeline with
a known shape should be a pipeline.

---

## Two-machine setup

Nothing forces the agent and the GPU onto the same box:

```
your laptop                          GPU server
-----------                          ----------
Claude Code                          ollama serve (0.0.0.0:11434)
  └─ MCP client                      qwen2.5:3b, qwen2.5:14b
       │
       └─ python3 -m gmt.mcp.server ──HTTP──▶ Ollama
              │
              └─ data/garment.db
```

Or put the MCP server on the GPU box over SSH and keep the data there —
which is the right shape once the database holds real production data.
