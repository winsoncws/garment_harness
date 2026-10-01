"""Task layer.

The two tests that matter most here are the parametric ones: they run over
EVERY registered task, so a new task file is automatically covered the
moment you write it. That is the point of the registry.
"""
from __future__ import annotations

import json
import unittest

from gmt.core import config, registry, router
from gmt.core.contracts import extract_json

SAMPLES = {
    "classify_defect": {"text": "Loom 7 big oil smear down the selvedge, 2m"},
    "extract_defect_fields": {"text": "L7 3rd shift, roll RB-4412, oil at 88.5m, 2 pcs"},
    "extract_triples": {"text": "L7 oil mark, roll RB-4412"},
    "text_to_sql": {"question": "defects per machine"},
    "answer_from_rows": {"question": "how many?", "rows": [{"machine": "LOOM-07", "n": 3}]},
    "normalize_units": {"text": "3 yards and 120 m"},
    "frame_gate": {"image_path": "/nonexistent.png"},
    "defect_cnn": {"image_path": "/nonexistent.png"},
}


class TestEveryTask(unittest.TestCase):
    def test_every_task_has_a_routing_entry(self) -> None:
        """Forgetting routing.yaml is the easiest mistake to make."""
        cfg = config.load()
        for name in registry.names():
            with self.subTest(task=name):
                self.assertIsNotNone(
                    cfg.get(f"routing.tasks.{name}"),
                    f"add `{name}: {{ start: N, max: M }}` to config/routing.yaml")

    def test_every_stub_satisfies_its_own_contract(self) -> None:
        """A stub that does not validate makes offline mode a lie."""
        for name, payload in SAMPLES.items():
            with self.subTest(task=name):
                task = registry.get(name)
                data, err = extract_json(task.stub(payload))
                self.assertFalse(err, f"{name}: stub is not JSON: {err}")
                res = task.validate_output(data, payload)
                self.assertTrue(res.ok, f"{name}: {res.why()}")

    def test_every_task_declares_identity(self) -> None:
        for name, task in registry.all().items():
            with self.subTest(task=name):
                self.assertTrue(task.desc, f"{name} needs a desc")
                self.assertGreaterEqual(task.version, 1)
                self.assertTrue(task.output.fields, f"{name} needs an output contract")

    def test_input_hash_is_stable_and_version_sensitive(self) -> None:
        t = registry.get("classify_defect")
        a = t.input_hash({"text": "x"})
        self.assertEqual(a, t.input_hash({"text": "x"}))
        self.assertNotEqual(a, t.input_hash({"text": "y"}))
        old = t.version
        try:
            t.version = old + 1
            self.assertNotEqual(a, t.input_hash({"text": "x"}))
        finally:
            t.version = old


class TestGuards(unittest.TestCase):
    """The business rules. These are what stop confident nonsense."""

    def test_classify_rejects_invented_evidence(self) -> None:
        t = registry.get("classify_defect")
        errs = t.check({"label": "hole", "severity": 3, "confidence": 0.9,
                        "evidence": "a gaping tear near the hem"},
                       {"text": "Loom 7 oil smear on selvedge"})
        self.assertTrue(errs)

    def test_classify_accepts_quoted_evidence(self) -> None:
        t = registry.get("classify_defect")
        errs = t.check({"label": "oil_mark", "severity": 3, "confidence": 0.9,
                        "evidence": "oil smear on selvedge"},
                       {"text": "Loom 7 oil smear on selvedge"})
        self.assertEqual(errs, [])

    def test_extract_rejects_numbers_not_in_the_note(self) -> None:
        t = registry.get("extract_defect_fields")
        errs = t.check({"machine": "", "line": "", "shift": "A", "operator": "",
                        "roll_id": "", "fabric_type": "unknown",
                        "position_m": 42.0, "quantity": 1},
                       {"text": "loom 7 oil smear at 88m"})
        self.assertTrue(any("position_m" in e for e in errs))

    def test_number_guard_accepts_a_correct_unit_conversion(self) -> None:
        """3 yards -> 2.7432 m is right, even though 2.7432 is not in the note."""
        t = registry.get("extract_defect_fields")
        base = {"machine": "", "line": "", "shift": "unknown", "operator": "",
                "roll_id": "", "yarn_lot": "", "quantity": 1,
                "fabric_type": "unknown"}
        note = {"text": "loom 7 slub in weft, 3 yards from start"}
        self.assertEqual(t.check({**base, "position_m": 2.7432}, note), [])
        self.assertTrue(t.check({**base, "position_m": 88.0}, note))

    def test_roll_and_yarn_lot_cannot_be_the_same_id(self) -> None:
        """A finished roll and an incoming yarn lot are different things."""
        t = registry.get("extract_defect_fields")
        base = {"machine": "", "line": "", "shift": "unknown", "operator": "",
                "fabric_type": "unknown", "position_m": 0.0, "quantity": 1}
        errs = t.check({**base, "roll_id": "VL-3301", "yarn_lot": "VL-3301"},
                       {"text": "viscose lot VL-3301"})
        self.assertTrue(any("cannot be both" in e for e in errs))
        self.assertEqual(
            t.check({**base, "roll_id": "", "yarn_lot": "VL-3301"},
                    {"text": "viscose lot VL-3301"}), [])

    def test_sql_rejects_unknown_tables_and_writes(self) -> None:
        t = registry.get("text_to_sql")
        self.assertTrue(t.check({"sql": "SELECT * FROM defects", "tables": [],
                                 "rationale": "", "answerable": True}, {}))
        self.assertTrue(t.check({"sql": "DROP TABLE artifact", "tables": [],
                                 "rationale": "", "answerable": True}, {}))
        self.assertEqual(t.check({"sql": "SELECT machine FROM defect_observation",
                                  "tables": ["defect_observation"],
                                  "rationale": "", "answerable": True}, {}), [])

    def test_narration_rejects_invented_numbers(self) -> None:
        t = registry.get("answer_from_rows")
        rows = [{"machine": "LOOM-07", "n": 23}]
        self.assertTrue(t.check({"answer": "LOOM-07 leads with 23, about 30% of all.",
                                 "grounded": True}, {"rows": rows}))
        self.assertEqual(t.check({"answer": "LOOM-07 leads with 23.",
                                  "grounded": True}, {"rows": rows}), [])

    def test_triples_reject_unknown_relations(self) -> None:
        t = registry.get("extract_triples")
        self.assertTrue(t.check({"triples": [{"src": "a", "src_type": "machine",
                                              "rel": "invented", "dst": "b",
                                              "dst_type": "defect"}], "n": 1}, {}))

    def test_triples_reject_valid_parts_in_an_invalid_shape(self) -> None:
        """Every field legal, the combination nonsense. Seen in real output."""
        t = registry.get("extract_triples")
        note = {"text": "shade diff, dye jigger 2, viscose lot VL-3301 "
                        "from Anhui Textile"}
        bad = {"src": "VL-3301", "src_type": "yarn_lot", "rel": "supplied_by",
               "dst": "DYE-02", "dst_type": "machine"}
        errs = t.check({"triples": [bad], "n": 1}, note)
        self.assertTrue(any("not a valid shape" in e for e in errs), errs)

        good = {**bad, "dst": "Anhui Textile", "dst_type": "supplier"}
        self.assertEqual(t.check({"triples": [good], "n": 1}, note), [])

    def test_reversed_edge_is_repaired_not_rejected(self) -> None:
        """roll --observed_on--> defect has exactly one legal reading."""
        t = registry.get("extract_triples")
        note = {"text": "loom 7 oil smear, roll RB-4412"}
        backwards = {"triples": [{"src": "RB-4412", "src_type": "roll",
                                  "rel": "observed_on", "dst": "oil_mark",
                                  "dst_type": "defect"}], "n": 1}
        fixed = t.repair(backwards, note)
        edge = fixed["triples"][0]
        self.assertEqual(edge["src_type"], "defect")
        self.assertEqual(edge["src"], "oil_mark")
        self.assertEqual(edge["dst_type"], "roll")
        self.assertEqual(t.check(fixed, note), [])

    def test_invented_relation_is_not_repaired(self) -> None:
        """yarn_lot --made_of--> fabric is illegal BOTH ways. Reject it."""
        t = registry.get("extract_triples")
        note = {"text": "viscose lot VL-3301"}
        bogus = {"triples": [{"src": "VL-3301", "src_type": "yarn_lot",
                              "rel": "made_of", "dst": "viscose",
                              "dst_type": "fabric"}], "n": 1}
        self.assertTrue(t.check(t.repair(bogus, note), note))

    def test_triples_reject_ungrounded_identifiers(self) -> None:
        """The model echoed the FIELD NAME as a roll id. Seen in real output."""
        t = registry.get("extract_triples")
        note = {"text": "shade diff, dye jigger 2, viscose lot VL-3301"}
        bad = {"src": "roll_id", "src_type": "roll", "rel": "made_of",
               "dst": "VL-3301", "dst_type": "yarn_lot"}
        errs = t.check({"triples": [bad], "n": 1}, note)
        self.assertTrue(any("does not appear in the note" in e for e in errs), errs)

    def test_triples_allow_controlled_terms_not_in_the_note(self) -> None:
        """'oil_mark' is correct for a note saying 'oil smear'. Do not ground it."""
        t = registry.get("extract_triples")
        note = {"text": "loom 7 oil smear on the selvedge"}
        ok = {"src": "loom 7", "src_type": "machine", "rel": "produced",
              "dst": "oil_mark", "dst_type": "defect"}
        self.assertEqual(t.check({"triples": [ok], "n": 1}, note), [])

        invented = {**ok, "dst": "gremlins"}
        self.assertTrue(t.check({"triples": [invented], "n": 1}, note))


class TestDeadTierMemo(unittest.TestCase):
    """A missing model must be learned once, not rediscovered per item."""

    def tearDown(self) -> None:
        router.reset_dead_tiers()

    def test_permanent_error_detection(self) -> None:
        self.assertTrue(router._is_permanent(
            'HTTP 404 from http://localhost:11434/api/chat: {"error":"model \'x\' not found"}'))
        self.assertTrue(router._is_permanent("no such model"))
        self.assertFalse(router._is_permanent("timeout after 120s"))
        self.assertFalse(router._is_permanent("connection reset by peer"))

    def test_dead_tier_is_skipped_afterwards(self) -> None:
        router._DEAD_TIERS[1] = "model 'qwen2.5:3b' is not installed"
        run = router.run("classify_defect", {"text": "loom 7 oil smear 2m"},
                         tier=1, max_tier=2, use_cache=False, persist=False)
        self.assertTrue(any("skipped" in e for e in run.errors))
        self.assertTrue(run.ok, "must still escalate to tier 2 and succeed")
        self.assertEqual(run.tier, 2)


class TestTierZero(unittest.TestCase):
    def test_normalize_units_converts_mixed_units(self) -> None:
        run = router.run("normalize_units",
                         {"text": "3 yards at 120 m, 4.5 inch wide, 30cm deep"},
                         persist=False, use_cache=False)
        self.assertTrue(run.ok, run.errors)
        self.assertEqual(run.tier, 0)
        self.assertEqual(run.value["found"], 4)
        self.assertAlmostEqual(run.value["metres"][0], 2.7432, places=3)
        self.assertEqual(run.value["metres"][1], 120.0)

    def test_frame_gate_fails_open(self) -> None:
        """A gate that cannot see must never silently drop frames."""
        run = router.run("frame_gate", {"image_path": "/definitely/missing.png"},
                         persist=False, use_cache=False)
        self.assertTrue(run.ok, run.errors)
        self.assertTrue(run.value["send_to_gpu"])
        self.assertIn("fail_open", run.value["reason"])


class TestMachineNormalisation(unittest.TestCase):
    def test_canonical_machine(self) -> None:
        from gmt.tasks.t_extract_defect_fields import canonical_machine
        cases = {"L7": "LOOM-07", "loom 7": "LOOM-07", "LOOM#07": "LOOM-07",
                 "knitting m/c 12": "KNIT-12", "dye jigger 2": "DYE-02",
                 "stenter 2": "FIN-02", "": ""}
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(canonical_machine(raw), want)


if __name__ == "__main__":
    unittest.main()
