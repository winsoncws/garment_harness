"""
store.py -- the only module that knows what the database is.
=======================================================================

Backend today: SQLite (stdlib, one file, zero servers).
Backend tomorrow: Postgres or DuckDB -- reimplement THIS FILE ONLY. No
other module in the repo issues SQL against the warehouse, so the swap is
contained. That is the whole point of routing every write through here.

    pipelines / tasks / MCP  ->  Store  ->  sqlite3  ->  data/garment.db
                                   ^
                            swap this layer  ->  psycopg / duckdb

WHEN TO MOVE OFF SQLITE
-----------------------
| Signal                                   | Move to    |
|------------------------------------------|------------|
| >1 machine writing concurrently          | Postgres   |
| analytics scans over 10M+ rows are slow  | DuckDB     |
| you need row-level security / users      | Postgres   |
| single box, batch analytics, big files   | DuckDB     |
Until one of those is true, SQLite is genuinely the right answer and will
happily hold tens of millions of rows.

THE READ-ONLY GUARANTEE
-----------------------
`Store.query()` runs SQL that a language model wrote. It opens a SEPARATE
connection with SQLite's `mode=ro` URI flag, so a DROP TABLE is refused by
the database engine itself, not by a regex we hope is complete. The regex
check on top of that is only there to give a friendlier error message.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "data" / "schema.sql"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_id(*parts: Any) -> str:
    blob = "|".join(str(p) for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, path: Optional[str] = None):
        cfg = config.load()
        self.path = str(path or cfg.get("store.path"))
        self.schema_version = int(cfg.get("project.schema_version", 1))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # ONE CONNECTION PER THREAD. This is not optional.
        #
        # sqlite3 refuses to let a connection be used from a thread other than
        # the one that created it, and runner.map_async runs every item on a
        # worker thread. A single shared connection raises
        #   "SQLite objects created in a thread can only be used in that
        #    same thread"
        # the moment you turn on concurrency. threading.local() gives each
        # worker its own handle; WAL mode (set in schema.sql, re-asserted
        # below) lets those handles read while another writes.
        self._local = threading.local()

    # -- connection ---------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA journal_mode = WAL")
            # Wait instead of failing when another thread holds the write lock.
            c.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = c
        return c

    def close(self) -> None:
        """Close this thread's connection. Other threads keep theirs."""
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    # =======================================================================
    # BRONZE -- raw, immutable
    # =======================================================================

    def put_document(self, *, source_system: str, doc_type: str, content: str,
                     source_uri: str = "", captured_at: Optional[str] = None,
                     lang: str = "en", meta: Optional[Dict[str, Any]] = None) -> str:
        """Insert raw text. Idempotent: same content -> same doc_id, no dupe."""
        doc_id = make_id(source_system, source_uri, content)
        self.conn.execute(
            """INSERT OR IGNORE INTO source_document
               (doc_id, source_system, source_uri, doc_type, content, lang,
                captured_at, ingested_at, meta_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (doc_id, source_system, source_uri, doc_type, content, lang,
             captured_at or now_iso(), now_iso(),
             json.dumps(meta or {}, ensure_ascii=False)))
        self.conn.commit()
        return doc_id

    def put_blob(self, *, kind: str, path: str, doc_id: Optional[str] = None,
                 width: int = 0, height: int = 0,
                 meta: Optional[Dict[str, Any]] = None) -> str:
        blob_id = make_id(kind, path)
        sha = ""
        p = Path(path)
        if p.exists():
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
        self.conn.execute(
            """INSERT OR IGNORE INTO source_blob
               (blob_id, doc_id, kind, path, sha256, width, height,
                captured_at, meta_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (blob_id, doc_id, kind, str(path), sha, width, height, now_iso(),
             json.dumps(meta or {}, ensure_ascii=False)))
        self.conn.commit()
        return blob_id

    def documents(self, doc_type: Optional[str] = None,
                  limit: int = 1000) -> List[sqlite3.Row]:
        if doc_type:
            cur = self.conn.execute(
                "SELECT * FROM source_document WHERE doc_type=? "
                "ORDER BY captured_at LIMIT ?", (doc_type, limit))
        else:
            cur = self.conn.execute(
                "SELECT * FROM source_document ORDER BY captured_at LIMIT ?", (limit,))
        return cur.fetchall()

    # =======================================================================
    # SILVER -- every task output + full provenance
    # =======================================================================

    def get_artifact(self, task: str, task_version: int,
                     input_hash: str) -> Optional[Dict[str, Any]]:
        """The cache lookup. Same task + version + input -> reuse the answer.

        This is why `input_hash` includes the task version: bump the version
        and every cached answer is correctly ignored.
        """
        row = self.conn.execute(
            "SELECT value_json FROM artifact WHERE task=? AND task_version=? "
            "AND input_hash=?", (task, task_version, input_hash)).fetchone()
        return json.loads(row["value_json"]) if row else None

    def put_artifact(self, *, task: str, task_version: int, input_hash: str,
                     value: Dict[str, Any], doc_id: Optional[str] = None,
                     tier: int = -1, model: str = "") -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO artifact
               (task, task_version, input_hash, doc_id, value_json, tier,
                model, schema_version, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (task, task_version, input_hash, doc_id,
             json.dumps(value, ensure_ascii=False), tier, model,
             self.schema_version, now_iso()))
        self.conn.commit()

    def log_run(self, run: Any) -> None:
        """Append a TaskRun. Never raises -- logging must not break a pipeline."""
        try:
            self.conn.execute(
                """INSERT INTO task_run
                   (task, task_version, input_hash, ok, tier, started_tier,
                    escalated, model, provider, attempts, latency_ms,
                    prompt_tokens, output_tokens, errors, raw_text, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run.task, getattr(run, "task_version", 1), run.input_hash,
                 1 if run.ok else 0, run.tier, run.started_tier,
                 1 if run.escalated else 0, run.model, run.provider,
                 run.attempts, run.latency_ms, run.prompt_tokens,
                 run.output_tokens, "; ".join(run.errors),
                 (run.raw_text or "")[:4000], now_iso()))
            self.conn.commit()
        except sqlite3.Error as e:                            # pragma: no cover
            print(f"[store] WARN could not log run: {e}")

    # =======================================================================
    # GOLD -- curated business tables
    # =======================================================================

    def put_defect(self, row: Dict[str, Any]) -> int:
        cols = ("doc_id", "observed_at", "plant", "line", "machine", "shift",
                "operator", "roll_id", "yarn_lot", "fabric_type", "defect_label",
                "severity", "confidence", "position_m", "width_cm",
                "quantity", "source", "needs_review")
        vals = [row.get(c) for c in cols]
        cur = self.conn.execute(
            f"INSERT INTO defect_observation ({','.join(cols)}, created_at) "
            f"VALUES ({','.join('?' * len(cols))}, ?)", (*vals, now_iso()))
        self.conn.commit()
        return int(cur.lastrowid)

    def put_triples(self, triples: Iterable[Dict[str, Any]],
                    doc_id: Optional[str] = None) -> int:
        """Upsert kg nodes + edges. Each triple: {src, src_type, rel, dst, dst_type}."""
        n = 0
        for t in triples:
            src = f"{t['src_type']}:{t['src']}"
            dst = f"{t['dst_type']}:{t['dst']}"
            for nid, ntype, label in ((src, t["src_type"], t["src"]),
                                      (dst, t["dst_type"], t["dst"])):
                self.conn.execute(
                    "INSERT OR IGNORE INTO kg_node (node_id, node_type, label, created_at)"
                    " VALUES (?,?,?,?)", (nid, ntype, label, now_iso()))
            self.conn.execute(
                "INSERT OR IGNORE INTO kg_edge (src_id, rel, dst_id, weight, doc_id, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (src, t["rel"], dst, float(t.get("weight", 1.0)), doc_id, now_iso()))
            n += 1
        self.conn.commit()
        return n

    def put_metric(self, day: str, metric: str, value: float,
                   dimension: str = "", n: int = 0) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO metric_daily (day, metric, dimension, value, n, created_at)"
            " VALUES (?,?,?,?,?,?)", (day, metric, dimension, value, n, now_iso()))
        self.conn.commit()

    # =======================================================================
    # READ PATH -- including SQL written by a language model
    # =======================================================================

    _FORBIDDEN = re.compile(
        r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
        r"pragma|vacuum|reindex)\b", re.IGNORECASE)

    def query(self, sql: str, params: Sequence[Any] = (),
              limit: int = 500) -> Tuple[List[Dict[str, Any]], str]:
        """Run UNTRUSTED read-only SQL. Returns (rows, error).

        Three independent guards, in order of strength:
          1. the connection is opened mode=ro  -> the engine refuses writes
          2. only one statement is allowed     -> no `SELECT 1; DROP TABLE x`
          3. a keyword blocklist               -> a clear message, not a crash
        """
        s = sql.strip().rstrip(";").strip()
        if not s:
            return [], "empty SQL"
        if ";" in s:
            return [], "only a single statement is allowed"
        if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
            return [], "only SELECT / WITH queries are allowed"
        if self._FORBIDDEN.search(s):
            return [], "query contains a write keyword"
        if not re.search(r"\blimit\b", s, re.IGNORECASE):
            s = f"{s} LIMIT {limit}"

        try:
            ro = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=15)
            ro.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in ro.execute(s, tuple(params)).fetchall()]
            finally:
                ro.close()
            return rows, ""
        except sqlite3.Error as e:
            return [], f"SQL error: {e}"

    def schema_text(self) -> str:
        """A compact CREATE-TABLE dump, to paste into a text-to-SQL prompt.

        Feed the model the real schema, never a hand-written summary -- a
        summary drifts, and then the model writes SQL against columns that
        no longer exist.
        """
        rows = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        out = []
        for r in rows:
            if r["sql"]:
                # strip comments and squeeze blank lines to save prompt tokens
                clean = re.sub(r"--.*$", "", r["sql"], flags=re.MULTILINE)
                out.append(re.sub(r"\n\s*\n", "\n", clean).strip())
        return ";\n\n".join(out) + ";"

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for t in ("source_document", "source_blob", "artifact", "task_run",
                  "defect_observation", "kg_node", "kg_edge", "metric_daily"):
            try:
                out[t] = self.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            except sqlite3.Error:
                out[t] = -1
        return out


_DEFAULT: Optional[Store] = None


def get_store() -> Store:
    """Process-wide default Store. Use this everywhere instead of Store()."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Store()
    return _DEFAULT
