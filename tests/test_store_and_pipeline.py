"""Store guarantees and one full offline pipeline run."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from gmt.core.store import Store


class TestStoreGuards(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.st = Store(str(Path(self.tmp) / "t.db"))
        self.st.init_schema()

    def test_documents_deduplicate_on_content(self) -> None:
        a = self.st.put_document(source_system="s", doc_type="qa_report", content="x")
        b = self.st.put_document(source_system="s", doc_type="qa_report", content="x")
        self.assertEqual(a, b)
        self.assertEqual(self.st.counts()["source_document"], 1)

    def test_query_refuses_writes(self) -> None:
        for bad in ("DROP TABLE artifact",
                    "DELETE FROM artifact",
                    "SELECT 1; DROP TABLE artifact",
                    "INSERT INTO artifact VALUES (1)"):
            with self.subTest(sql=bad):
                rows, err = self.st.query(bad)
                self.assertTrue(err, f"{bad!r} should have been refused")
                self.assertEqual(rows, [])

    def test_query_allows_reads(self) -> None:
        rows, err = self.st.query("SELECT COUNT(*) c FROM source_document")
        self.assertEqual(err, "")
        self.assertEqual(rows[0]["c"], 0)

    def test_query_adds_a_limit(self) -> None:
        for i in range(5):
            self.st.put_document(source_system="s", doc_type="qa_report",
                                 content=f"note {i}")
        rows, err = self.st.query("SELECT doc_id FROM source_document", limit=2)
        self.assertEqual(err, "")
        self.assertEqual(len(rows), 2)

    def test_artifact_cache_is_keyed_by_task_version(self) -> None:
        self.st.put_artifact(task="t", task_version=1, input_hash="h",
                             value={"a": 1})
        self.assertEqual(self.st.get_artifact("t", 1, "h"), {"a": 1})
        self.assertIsNone(self.st.get_artifact("t", 2, "h"))   # version bump busts it

    def test_schema_text_mentions_the_gold_tables(self) -> None:
        s = self.st.schema_text()
        for table in ("defect_observation", "kg_edge", "v_defect_daily"):
            self.assertIn(table, s)


class TestOfflinePipeline(unittest.TestCase):
    """The whole thing, end to end, with the fake model."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        # SAVE the previous values -- run_tests.py sets GMT_OFFLINE=1 for the
        # whole suite, and popping it here would silently switch every LATER
        # test class back to hitting a real Ollama. Restore, never delete.
        self._saved = {k: os.environ.get(k) for k in ("GMT_OFFLINE", "GMT_DB")}
        os.environ["GMT_OFFLINE"] = "1"
        os.environ["GMT_DB"] = str(Path(self.tmp) / "p.db")
        from gmt.core import config
        config.load(reload=True)
        self.st = Store()
        self.st.init_schema()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from gmt.core import config, llm
        llm._CLIENTS.clear()
        config.load(reload=True)

    def test_ingest_produces_gold_rows_and_a_trace(self) -> None:
        from gmt.pipelines import p_qa_ingest
        notes = [
            {"text": "Loom 7 shift A big oil smear 2m, roll DN-0912",
             "captured_at": "2026-08-14T06:20:00Z"},
            {"text": "roll checked all clean no issue",
             "captured_at": "2026-08-14T07:00:00Z"},
            {"text": "warp break at 88m loom 3, end down",
             "captured_at": "2026-08-14T08:00:00Z"},
        ]
        summary = p_qa_ingest.run(notes, store=self.st, concurrency=2, verbose=False)

        self.assertEqual(summary["notes"], 3)
        self.assertEqual(summary["clean_notes"], 1)
        self.assertEqual(summary["observations"], 2)
        self.assertEqual(summary["failed_chains"], 0)
        self.assertGreater(summary["kg_edges"], 0)

        counts = self.st.counts()
        self.assertEqual(counts["source_document"], 3)
        self.assertGreater(counts["task_run"], 0, "the trace must be written")
        self.assertGreater(counts["artifact"], 0, "silver artifacts must be written")

        rows, err = self.st.query(
            "SELECT defect_label, machine FROM defect_observation ORDER BY machine")
        self.assertEqual(err, "")
        self.assertEqual([r["machine"] for r in rows], ["LOOM-03", "LOOM-07"])

    def test_rerunning_is_idempotent(self) -> None:
        from gmt.pipelines import p_qa_ingest
        notes = [{"text": "Loom 7 oil smear 2m", "captured_at": "2026-08-14T06:20:00Z"}]
        p_qa_ingest.run(notes, store=self.st, concurrency=1, verbose=False)
        docs_after_first = self.st.counts()["source_document"]
        p_qa_ingest.run(notes, store=self.st, concurrency=1, verbose=False)
        self.assertEqual(self.st.counts()["source_document"], docs_after_first,
                         "bronze must dedupe on content")

    def test_analytics_round_trip(self) -> None:
        from gmt.pipelines import p_analytics_ask, p_qa_ingest
        p_qa_ingest.run(
            [{"text": "Loom 7 oil smear 2m", "captured_at": "2026-08-14T06:20:00Z"},
             {"text": "loom 7 hole at 45 m", "captured_at": "2026-08-14T07:20:00Z"}],
            store=self.st, concurrency=1, verbose=False)

        res = p_analytics_ask.ask("which machine had the most defects?", store=self.st)
        self.assertTrue(res["sql"].lower().startswith("select"))
        self.assertGreater(res["row_count"], 0)
        self.assertTrue(res["answer"])


class TestRunnerLoops(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        # SAVE the previous values -- run_tests.py sets GMT_OFFLINE=1 for the
        # whole suite, and popping it here would silently switch every LATER
        # test class back to hitting a real Ollama. Restore, never delete.
        self._saved = {k: os.environ.get(k) for k in ("GMT_OFFLINE", "GMT_DB")}
        os.environ["GMT_OFFLINE"] = "1"
        os.environ["GMT_DB"] = str(Path(self.tmp) / "r.db")
        from gmt.core import config
        config.load(reload=True)
        self.st = Store()
        self.st.init_schema()

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from gmt.core import config, llm
        llm._CLIENTS.clear()
        config.load(reload=True)

    def test_sync_and_async_agree(self) -> None:
        from gmt.core import runner
        payloads = [{"text": f"loom {i} oil smear at {i}0 m"} for i in range(1, 7)]
        a = runner.map_sync("classify_defect", payloads, store=self.st)
        b = runner.map_parallel("classify_defect", payloads, store=self.st,
                                concurrency=3)
        self.assertEqual(len(a.runs), 6)
        self.assertEqual(len(b.runs), 6)
        self.assertEqual([r.value["label"] for r in a.runs],
                         [r.value["label"] for r in b.runs])

    def test_chain_skips_when_the_gate_says_no(self) -> None:
        from gmt.core.runner import Step, chain
        steps = [
            Step("classify_defect", prep=lambda s: {"text": s["text"]}, key="cls"),
            Step("extract_defect_fields", prep=lambda s: {"text": s["text"]},
                 key="fields",
                 when=lambda s: s["cls"]["label"] != "none"),
        ]
        clean = chain(steps, {"text": "roll checked all clean no issue"}, store=self.st)
        self.assertIn("extract_defect_fields", clean.skipped)

        dirty = chain(steps, {"text": "loom 7 oil smear 2m"}, store=self.st)
        self.assertEqual(dirty.skipped, [])
        self.assertIn("fields", dirty.state)


if __name__ == "__main__":
    unittest.main()
