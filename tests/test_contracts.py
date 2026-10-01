"""Contract layer: coercion, enums, guards, and JSON rescue."""
from __future__ import annotations

import unittest

from gmt.core.contracts import Contract, Field, extract_json


class TestCoercion(unittest.TestCase):
    def setUp(self) -> None:
        self.c = Contract("t", {
            "label": Field("str", enum=["hole", "stain", "none"]),
            "sev": Field("int", minimum=1, maximum=5),
            "conf": Field("float", minimum=0.0, maximum=1.0),
            "flag": Field("bool", required=False, default=False),
            "tags": Field("list", item_type="str", required=False, default=[]),
        })

    def test_repairs_sloppy_types(self) -> None:
        """A small model returns strings for everything. That must be fine."""
        r = self.c.validate({"label": " Hole ", "sev": "3", "conf": "0.8",
                             "flag": "yes", "tags": "urgent"})
        self.assertTrue(r.ok, r.why())
        self.assertEqual(r.value["label"], "hole")     # snapped to canonical
        self.assertEqual(r.value["sev"], 3)
        self.assertEqual(r.value["conf"], 0.8)
        self.assertIs(r.value["flag"], True)
        self.assertEqual(r.value["tags"], ["urgent"])  # scalar -> list

    def test_enum_is_enforced(self) -> None:
        r = self.c.validate({"label": "banana", "sev": 1, "conf": 0.5})
        self.assertFalse(r.ok)
        self.assertIn("not in", r.why())

    def test_range_is_enforced(self) -> None:
        r = self.c.validate({"label": "hole", "sev": 9, "conf": 0.5})
        self.assertFalse(r.ok)
        self.assertIn("maximum", r.why())

    def test_missing_required_is_reported(self) -> None:
        r = self.c.validate({"label": "hole"})
        self.assertFalse(r.ok)
        self.assertIn("sev", r.why())

    def test_optional_gets_its_default(self) -> None:
        r = self.c.validate({"label": "none", "sev": 1, "conf": 0.9})
        self.assertTrue(r.ok, r.why())
        self.assertIs(r.value["flag"], False)

    def test_unknown_keys_dropped_unless_strict(self) -> None:
        data = {"label": "hole", "sev": 1, "conf": 0.5, "extra": "chatter"}
        self.assertTrue(self.c.validate(data).ok)
        self.assertNotIn("extra", self.c.validate(data).value)
        self.assertFalse(self.c.validate(data, strict=True).ok)

    def test_json_schema_shape(self) -> None:
        s = self.c.json_schema()
        self.assertEqual(s["type"], "object")
        self.assertEqual(s["properties"]["sev"]["type"], "integer")
        self.assertEqual(s["properties"]["label"]["enum"],
                         ["hole", "stain", "none"])
        self.assertIn("label", s["required"])
        self.assertNotIn("flag", s["required"])


class TestJSONRescue(unittest.TestCase):
    """Small models wrap, chatter, and leave trailing commas. Survive it."""

    def test_clean(self) -> None:
        self.assertEqual(extract_json('{"a": 1}')[0], {"a": 1})

    def test_fenced(self) -> None:
        self.assertEqual(extract_json('```json\n{"a": 1}\n```')[0], {"a": 1})

    def test_chatty(self) -> None:
        got, _ = extract_json('Sure! Here you go: {"a": 1} Hope this helps!')
        self.assertEqual(got, {"a": 1})

    def test_trailing_comma(self) -> None:
        self.assertEqual(extract_json('{"a": 1,}')[0], {"a": 1})

    def test_braces_inside_strings_dont_confuse_it(self) -> None:
        got, _ = extract_json('blah {"a": "not } the end", "b": 2} tail')
        self.assertEqual(got, {"a": "not } the end", "b": 2})

    def test_garbage_reports_an_error(self) -> None:
        got, err = extract_json("I refuse to answer.")
        self.assertIsNone(got)
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
