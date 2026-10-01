# 01 — Quickstart

Ten minutes, two paths. Do path A even if you have a GPU sitting right there:
it takes 30 seconds and it teaches you what the output should look like.

---

## Path A — no GPU, no network, no installs (30 seconds)

```bash
cd garment
make demo
```

That runs four commands. Here is what each one is doing.

### 1. `python3 -m gmt --offline init`

Creates `data/garment.db` from `data/schema.sql`. One SQLite file holds
everything: raw notes, every model output, and the curated business tables.

### 2. `python3 -m gmt --offline ingest`

Reads the 24 deliberately messy notes in `data/seed/qa_reports.jsonl` and
runs the QA pipeline over them.

```
  notes            24     <- what went in
  documents        24     <- landed in bronze (immutable, deduped)
  observations     19     <- defects found and written to gold
  clean_notes      5      <- "roll checked, all clean" -- correctly not a defect
  kg_edges         84     <- knowledge graph edges built from the same pass
  needs_review     1      <- low confidence, flagged for a human
  failed_chains    0      <- every note made it through
```

The interesting number is `clean_notes`. Five notes said nothing was wrong,
and the pipeline **skipped the two expensive tier-2 tasks for those five**.
That branch is one line in `gmt/pipelines/p_qa_ingest.py`:

```python
Step(task="extract_defect_fields", ..., when=_has_defect)
```

### 3. `python3 -m gmt --offline ask "which machine had the most defects?"`

```
Q: which machine had the most defects?
A: 7 row(s). Top result: machine=LOOM-07, n=6.

SQL (tier 2, 1 attempt(s)):
  SELECT machine, COUNT(*) AS n FROM defect_observation GROUP BY machine ORDER BY n DESC

  machine | n
  --------+--
  LOOM-07 | 6
  LOOM-03 | 5
  ...
```

Note that the SQL is shown. Always show the SQL. A number without its query
is a number nobody can check.

### 4. `python3 -m gmt --offline stats`

The part people skip and should not. This is the trace table:

```
[model performance -- from the task_run trace]
  task                     tier  runs   ok     esc   avg ms
  classify_defect          1     24     24     0     1.0
  extract_defect_fields    2     19     19     0     1.0
```

Every model call, ever, with its tier, whether it passed its contract, and
whether it had to escalate. This table is how you will decide what your 14B
can be trusted with.

> **What is "offline" doing?** `--offline` swaps in `FakeClient`, which
> returns canned answers built from simple keyword rules (each task's
> `stub()` method). The *entire rest of the system* is real: real prompts
> get built, real JSON extraction runs, real contracts validate, real rows
> are written. Only the model is fake. That is why the offline demo is
> genuinely useful and not a mock-up.

---

## Path B — against your real Ollama

### If you are running ON the GPU server

```bash
ollama serve &                  # if it is not already running
ollama pull qwen2.5:14b
ollama pull qwen2.5:3b          # optional but recommended -- tier 1

cd garment
python3 -m gmt doctor           # <- always start here
```

`doctor` checks python, optional packages, the database, whether Ollama
answers, and whether the models named in `config/models.yaml` actually
exist. It tells you the exact command to fix anything missing.

### If you are running from your laptop against a remote GPU box

On the **GPU box**, Ollama must listen on more than localhost:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

On your **laptop**:

```bash
export GMT_OLLAMA_HOST=http://<gpu-ip>:11434
python3 -m gmt doctor
```

### Then run it for real

```bash
python3 -m gmt init
python3 -m gmt ingest                 # 24 notes through qwen2.5
python3 -m gmt ask "which loom has the most oil marks?"
python3 -m gmt stats
python3 -m gmt bench --tiers 1,2      # the important one
```

`bench` is the payoff. It runs both models against the 16 labelled examples
in `tests/golden/qa_golden.jsonl` and prints accuracy per field per tier.
That table tells you whether your 3B can handle classification, or whether
everything needs the 14B.

---

## Only got one model?

Edit `config/models.yaml` and point tier 1 at the same model as tier 2:

```yaml
  tier1:
    provider: ollama
    model: qwen2.5:14b        # same as tier2 until you pull a small one
```

Everything still works — you just do not get the cost saving yet.

---

## Turning off the cloud entirely

For a factory-floor machine that must never send data outside:

```bash
export GMT_MAX_TIER=2
```

Tier 3 becomes unreachable. Tasks that would have escalated there now fail
loudly and set `needs_review` instead. Data residency, enforced in one
environment variable rather than in a policy document.

---

## Common problems

| Symptom                                    | Fix                                                        |
|--------------------------------------------|------------------------------------------------------------|
| `cannot reach http://localhost:11434`       | `ollama serve`, or set `GMT_OLLAMA_HOST`                   |
| `MISSING -> ollama pull qwen2.5:3b`         | run that exact command, or edit `config/models.yaml`       |
| First call takes 30 seconds                 | model loading into VRAM. `keep_alive: 30m` prevents repeats|
| Everything is slow at concurrency 8         | Ollama queues internally. Use 2–4. See `core/runner.py`    |
| `no such table: defect_observation`         | `python3 -m gmt init`                                      |
| Changed a prompt, results identical         | the artifact cache. Bump `version` on the task.            |
