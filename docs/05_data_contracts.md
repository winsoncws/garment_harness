# 05 — Data, contracts, and the single ground truth

You asked for one database that every initiative reads and writes. This is
how that is achieved, and — more importantly — how it stays true after two
years of changes.

---

## The three layers

```
BRONZE   source_document, source_blob
         raw, immutable, append-only. Never edited, never deleted.
              │
              ▼   a Task runs
SILVER   artifact, task_run
         every task output + full provenance: which model, which prompt
         version, how long it took, whether it escalated.
              │
              ▼   a projection (plain python, no models)
GOLD     defect_observation, kg_node, kg_edge, metric_daily
         curated, joinable, business-facing. What dashboards read.
```

**Data flows down only.** Gold is always rebuildable from bronze plus the
current task versions.

### Why not just write straight to `defects`?

Because you *will* improve a prompt. When you do, you need to answer:

- what did the old version say about these same 40,000 notes?
- how many rows changed?
- did they change for the better?

Bronze makes re-running possible. Silver makes comparing possible. Gold
keeps the business view stable while you churn underneath. Skip the layers
and every model improvement becomes a migration with no rollback.

---

## The `artifact` table is the clever bit

```sql
CREATE TABLE artifact (
    task           TEXT,      -- 'classify_defect'
    task_version   INTEGER,   -- 1
    input_hash     TEXT,      -- sha256(task + version + payload)[:16]
    value_json     TEXT,      -- the validated output
    tier           INTEGER,   -- which model produced it
    model          TEXT,
    doc_id         TEXT,      -- provenance back to bronze
    created_at     TEXT,
    UNIQUE(task, task_version, input_hash)
);
```

One table serves **every task, present and future**. Three consequences:

1. **Adding a task never requires a migration.** New task, same table.
2. **The unique key is the cache.** `router.run()` looks up
   `(task, version, input_hash)` and skips the model entirely on a hit.
3. **Bumping `version` invalidates exactly the right rows** — because the
   version is inside the hash. Change a prompt, bump the version, re-run;
   old rows are untouched and still attributable to the old prompt.

---

## Closed vocabularies are the actual integration mechanism

This is the part that makes "one ground truth" real rather than aspirational.

```
        text notes ──▶ classify_defect ──┐
                                          ├──▶ DEFECT_LABELS ──▶ one column
        camera     ──▶ defect_cnn      ──┘                        that everything
                                                                  can group by
```

Both paths produce a value from the same list in `gmt/tasks/_vocab.py`.
That is why a dashboard can show human-reported and camera-detected defects
on one chart, and why the knowledge graph has one `defect:hole` node instead
of nine spellings of it.

**Changing a vocabulary is a schema change.** Bump `project.schema_version`
in `config/settings.yaml` and the `version` of every task that emits it.

---

## Provenance: every row can be traced back

```
defect_observation.doc_id  ──▶ source_document.doc_id  ──▶ the exact raw text
artifact.doc_id            ──▶ same
task_run.raw_text          ──▶ exactly what the model said, verbatim
task_run.model + .tier     ──▶ which weights produced it
defect_cnn.model_version   ──▶ v1:a3f9c2e1  (hash of the weights file)
kg_edge.doc_id             ──▶ which note asserted this edge
```

When someone asks "why does the dashboard say LOOM-07 has 23 oil marks",
you can walk from the number to the note that caused it. That is not a nice
to have — it is the difference between a system people trust and a system
people work around.

---

## Adding a new data source

Say the ERP has work orders. Three steps, no schema change:

```python
store.put_document(
    source_system="erp",
    doc_type="work_order",
    content=json.dumps(row),
    source_uri=f"erp://work_order/{row['id']}",
    captured_at=row["created_at"],
)
```

1. Land it in bronze exactly as it arrived.
2. Write a task that extracts what you need from it.
3. Project the useful fields into gold — either a new table, or by joining
   through `doc_id` to what is already there.

The `source_system` column is what keeps sources distinguishable forever.

---

## Swapping the storage backend

Every SQL statement against the warehouse lives in `gmt/core/store.py`.
Nothing else in the repo issues SQL. To move backend you reimplement that
one file.

| Signal | Move to | Why |
|--------|---------|-----|
| more than one machine writing | PostgreSQL | SQLite has one writer |
| analytics scans over 10M+ rows are slow | DuckDB | columnar, vectorised |
| you need users, roles, row-level security | PostgreSQL | SQLite has none |
| single box, batch analytics, large files | DuckDB | reads Parquet directly |
| none of the above | **stay on SQLite** | it will hold tens of millions of rows |

A realistic end state is both: PostgreSQL for the live ground truth, and a
nightly Parquet export that DuckDB queries for analytics. `store.py` gets a
`backend:` switch; nothing above it changes.

---

## Swapping the contract layer for pydantic

`gmt/core/contracts.py` exists because your machines had zero packages
installed. Pydantic is better. When you are ready:

| Ours | Pydantic |
|------|----------|
| `Contract(name, {...})` | `class Out(BaseModel):` |
| `Field("int", minimum=1, maximum=5)` | `severity: int = Field(ge=1, le=5)` |
| `Field("str", enum=[...])` | `label: Literal["hole", "stain", ...]` |
| `contract.validate(d)` | `Out.model_validate(d)` |
| `contract.json_schema()` | `Out.model_json_schema()` |
| `task.check()` | `@model_validator(mode="after")` |

Keep the `Result(ok, value, errors)` return shape and `router.py` will not
need to change. The migration is per-task and can be done one file at a
time.

**Do not migrate for its own sake.** Migrate when you want something ours
lacks: nested models, union types, custom validators, or the ecosystem
(FastAPI request models sharing the same class).
