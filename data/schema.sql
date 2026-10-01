-- =====================================================================
-- schema.sql -- THE SINGLE GROUND TRUTH
--
-- Three layers. The rule that makes this work: data only ever flows DOWN.
--
--   BRONZE  source_document      raw, immutable, append-only. Never edited,
--           source_blob          never deleted. If a model was wrong you can
--                                always re-derive from here.
--             |
--             v  (a Task runs)
--   SILVER  artifact             EVERY task output, generic JSON + full
--           task_run             provenance (which model, which prompt
--                                version, how long, did it escalate).
--             |
--             v  (a projection)
--   GOLD    defect_observation   curated, business-facing, joinable tables.
--           kg_node / kg_edge    These are what dashboards and the chatbot
--           metric_daily         read. Small, clean, well-typed.
--
-- WHY THIS SHAPE
-- --------------
-- The temptation is to write straight into a `defects` table. Do not. When
-- (not if) you improve a prompt or swap qwen2.5:14b for something better,
-- you need to re-run history and compare. Bronze makes re-running possible;
-- silver makes comparing possible; gold keeps the business view stable while
-- you churn underneath.
--
-- Everything carries `schema_version` and `task_version` so 2025 rows and
-- 2026 rows stay distinguishable forever.
-- =====================================================================

PRAGMA journal_mode = WAL;     -- readers never block the writer
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- BRONZE
CREATE TABLE IF NOT EXISTS source_document (
    doc_id        TEXT PRIMARY KEY,        -- sha256(source_uri + content)[:16]
    source_system TEXT NOT NULL,           -- 'qa_tablet' | 'erp' | 'email' | 'loom_plc'
    source_uri    TEXT,                    -- file path, URL, table row id
    doc_type      TEXT NOT NULL,           -- 'qa_report' | 'work_order' | 'spec_sheet'
    content       TEXT,                    -- the raw text, verbatim
    lang          TEXT DEFAULT 'en',
    captured_at   TEXT NOT NULL,           -- ISO8601, when it happened
    ingested_at   TEXT NOT NULL,           -- ISO8601, when we saw it
    meta_json     TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_doc_type ON source_document(doc_type, captured_at);

-- Binary things (fabric images, video frames) live on disk; we store the
-- pointer + a hash so the DB stays small but the link stays verifiable.
CREATE TABLE IF NOT EXISTS source_blob (
    blob_id     TEXT PRIMARY KEY,
    doc_id      TEXT REFERENCES source_document(doc_id),
    kind        TEXT NOT NULL,             -- 'frame' | 'still' | 'video' | 'pdf'
    path        TEXT NOT NULL,
    sha256      TEXT,
    width       INTEGER,
    height      INTEGER,
    captured_at TEXT,
    meta_json   TEXT DEFAULT '{}'
);

-- ---------------------------------------------------------------- SILVER
-- One row per task output. This single table serves EVERY task, present and
-- future -- adding a task never requires a migration.
CREATE TABLE IF NOT EXISTS artifact (
    artifact_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    task           TEXT NOT NULL,
    task_version   INTEGER NOT NULL,
    input_hash     TEXT NOT NULL,          -- sha256(task+version+payload)
    doc_id         TEXT REFERENCES source_document(doc_id),
    value_json     TEXT NOT NULL,          -- the validated output
    tier           INTEGER,
    model          TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    UNIQUE(task, task_version, input_hash)  -- <- makes the cache work
);
CREATE INDEX IF NOT EXISTS ix_artifact_doc ON artifact(doc_id, task);

-- Every attempt, including failures. This IS your evaluation dataset --
-- do not switch it off. Cheap to write, impossible to recover later.
CREATE TABLE IF NOT EXISTS task_run (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task          TEXT NOT NULL,
    task_version  INTEGER NOT NULL,
    input_hash    TEXT,
    ok            INTEGER NOT NULL,
    tier          INTEGER,
    started_tier  INTEGER,
    escalated     INTEGER DEFAULT 0,
    model         TEXT,
    provider      TEXT,
    attempts      INTEGER,
    latency_ms    INTEGER,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    errors        TEXT,
    raw_text      TEXT,                    -- what the model literally said
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_task ON task_run(task, created_at);

-- ------------------------------------------------------------------ GOLD
CREATE TABLE IF NOT EXISTS defect_observation (
    obs_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        TEXT REFERENCES source_document(doc_id),
    observed_at   TEXT NOT NULL,
    plant         TEXT,
    line          TEXT,
    machine       TEXT,
    shift         TEXT,                    -- 'A' | 'B' | 'C'
    operator      TEXT,
    roll_id       TEXT,
    yarn_lot      TEXT,                    -- the supply-chain join key
    fabric_type   TEXT,
    defect_label  TEXT NOT NULL,           -- closed vocabulary, see tasks
    severity      INTEGER,                 -- 1..5
    confidence    REAL,                    -- 0..1, from the model
    position_m    REAL,                    -- metres along the roll
    width_cm      REAL,
    quantity      INTEGER DEFAULT 1,
    source        TEXT,                    -- 'human_report' | 'cv_model'
    needs_review  INTEGER DEFAULT 0,       -- 1 -> a human must look
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_obs_time  ON defect_observation(observed_at);
CREATE INDEX IF NOT EXISTS ix_obs_label ON defect_observation(defect_label, machine);
CREATE INDEX IF NOT EXISTS ix_obs_lot   ON defect_observation(yarn_lot);

-- Knowledge graph. Deliberately plain: nodes + typed edges. Enough for
-- "which supplier's yarn correlates with slub defects on loom 7", and it
-- exports to Neo4j/RDF in one query when you outgrow SQLite.
CREATE TABLE IF NOT EXISTS kg_node (
    node_id    TEXT PRIMARY KEY,           -- 'machine:LOOM-07'
    node_type  TEXT NOT NULL,              -- machine|supplier|fabric|defect|order|operator
    label      TEXT NOT NULL,
    props_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kg_edge (
    edge_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id     TEXT NOT NULL REFERENCES kg_node(node_id),
    rel        TEXT NOT NULL,              -- 'supplies'|'ran_on'|'caused'|'part_of'
    dst_id     TEXT NOT NULL REFERENCES kg_node(node_id),
    weight     REAL DEFAULT 1.0,
    doc_id     TEXT REFERENCES source_document(doc_id),   -- provenance!
    created_at TEXT NOT NULL,
    UNIQUE(src_id, rel, dst_id, doc_id)
);
CREATE INDEX IF NOT EXISTS ix_edge_src ON kg_edge(src_id, rel);
CREATE INDEX IF NOT EXISTS ix_edge_dst ON kg_edge(dst_id, rel);

-- Pre-aggregated numbers for dashboards + the analytics chatbot. Rebuilt by
-- a nightly pipeline; never hand-edited.
CREATE TABLE IF NOT EXISTS metric_daily (
    day          TEXT NOT NULL,
    metric       TEXT NOT NULL,            -- 'defects_per_100m' | 'first_pass_yield'
    dimension    TEXT NOT NULL DEFAULT '', -- 'machine=LOOM-07'
    value        REAL NOT NULL,
    n            INTEGER,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (day, metric, dimension)
);

-- A convenience view: what a business user actually wants to see.
CREATE VIEW IF NOT EXISTS v_defect_daily AS
SELECT substr(observed_at, 1, 10) AS day,
       machine, shift, defect_label,
       COUNT(*)      AS n,
       AVG(severity) AS avg_severity,
       SUM(CASE WHEN needs_review = 1 THEN 1 ELSE 0 END) AS n_needs_review
FROM defect_observation
GROUP BY day, machine, shift, defect_label;
