# 03 — Add a task

The thing you will do most often. Ten minutes end to end.

We will build `classify_urgency`: given a defect note, decide how fast
someone must react. A tier-1 job — closed vocabulary, one decision.

---

## Step 1 — add the vocabulary (30 seconds)

Closed lists live in **one** place. `gmt/tasks/_vocab.py`:

```python
URGENCY_LEVELS: List[str] = [
    "stop_machine",   # halt production now
    "next_shift",     # fix at the changeover
    "scheduled",      # routine maintenance window
    "monitor",        # watch it, no action yet
]
```

Why here and not in the task file: the chatbot, the dashboards and the
knowledge graph will all need this same list. One definition, one spelling.

---

## Step 2 — write the task file

`gmt/tasks/t_classify_urgency.py`:

```python
"""
classify_urgency -- TIER 1. How fast must someone react?
=========================================================
    "hole in denim, loom stopped, 3 rolls affected"
                  |
                  v  qwen2.5:3b, temperature 0
    {"urgency": "stop_machine", "confidence": 0.9,
     "reason": "loom stopped, 3 rolls affected"}
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.contracts import Contract, Field
from ..core.registry import register
from ..core.task import Task
from ._vocab import DEFECT_LABELS, URGENCY_LEVELS


@register                                   # <- this line registers the task
class ClassifyUrgency(Task):
    name = "classify_urgency"
    version = 1
    desc = "Decide how urgently a fabric defect must be acted on."

    input = Contract("classify_urgency.in", {
        "text":     Field("str", desc="the inspection note", max_len=2000),
        "label":    Field("str", enum=DEFECT_LABELS, required=False, default="other",
                          desc="defect label from classify_defect, if known"),
        "severity": Field("int", minimum=1, maximum=5, required=False, default=3),
    })

    output = Contract("classify_urgency.out", {
        "urgency":    Field("str", enum=URGENCY_LEVELS,
                            desc="how fast someone must react"),
        "confidence": Field("float", minimum=0.0, maximum=1.0),
        "reason":     Field("str", max_len=160,
                            desc="the words in the note that justify this. Quote them."),
    })

    skill = "inspector"          # reuse the persona we already wrote

    def build_user(self, payload: Dict[str, Any]) -> str:
        return (
            "Urgency levels:\n"
            "  stop_machine = production must halt now\n"
            "  next_shift   = fix at the changeover\n"
            "  scheduled    = routine maintenance window\n"
            "  monitor      = watch it, no action yet\n\n"
            f"{self.output.prompt_block()}\n\n"
            f"Defect: {payload.get('label', 'unknown')} "
            f"(severity {payload.get('severity', 3)})\n"
            f"Note:\n\"\"\"\n{payload['text']}\n\"\"\"\n\nJSON:"
        )

    # ---- the business rules. Write these BEFORE you tune the prompt. -----
    def check(self, value: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        errs: List[str] = []
        sev = payload.get("severity", 3)

        # a severity-5 defect cannot be "monitor". That is not a judgement
        # call, it is a policy -- so it belongs in code, not in a prompt.
        if sev >= 5 and value["urgency"] in ("monitor", "scheduled"):
            errs.append(f"severity {sev} cannot be urgency {value['urgency']!r}")

        # grounding: the reason must be quotable from the note
        note = (payload.get("text") or "").lower()
        words = [w for w in value.get("reason", "").lower().split() if len(w) > 3]
        if words and sum(1 for w in words if w in note) < max(2, len(words) // 2):
            errs.append("reason is not supported by the note")
        return errs

    # ---- so `make demo` works with no GPU --------------------------------
    def stub(self, payload: Dict[str, Any]) -> str:
        import json
        sev = payload.get("severity", 3)
        text = payload.get("text", "")
        urgency = ("stop_machine" if sev >= 5 else
                   "next_shift" if sev == 4 else
                   "scheduled" if sev == 3 else "monitor")
        return json.dumps({"urgency": urgency, "confidence": 0.7,
                           "reason": text[:80]})
```

---

## Step 3 — tell the router where to start

`config/routing.yaml`:

```yaml
    classify_urgency:       { start: 1, max: 2 }
```

Forget this and the task falls back to `default_start_tier: 2` — it still
works, it just costs more than it needs to. There is a test that catches
this (`test_every_task_has_a_routing_entry`).

---

## Step 4 — run it

No imports to add, no registry to edit. The `@register` decorator plus the
file's presence in `gmt/tasks/` is the whole wiring.

```bash
python3 -m gmt tasks | grep urgency
python3 tests/run_tests.py                  # the parametric tests cover it already
```

```python
import gmt
r = gmt.run("classify_urgency", {
    "text": "hole in denim, loom stopped, 3 rolls affected",
    "label": "hole", "severity": 5,
})
print(r.summary())   # [OK ] classify_urgency tier=1 210ms attempts=1
print(r.value)
```

---

## Step 5 — put it in a pipeline

`gmt/pipelines/p_qa_ingest.py`:

```python
STEPS = [
    Step("classify_defect", prep=lambda s: {"text": s["text"]}, key="classify"),

    Step("classify_urgency",                       # <-- new
         prep=lambda s: {"text": s["text"],
                         "label": s["classify"]["label"],
                         "severity": s["classify"]["severity"]},
         key="urgency",
         when=_has_defect),
    ...
]
```

`prep` maps the accumulated state to this task's payload. Because everything
a step needs comes out of `state`, you can always reconstruct a task's exact
input without replaying the chain — which is what makes them independently
testable.

---

## Step 6 — measure it

Add labelled rows to `tests/golden/qa_golden.jsonl`, extend `CHECKS` in
`gmt/bench.py`:

```python
CHECKS = {
    ...
    "urgency": ("classify_urgency", "urgency"),
}
```

```bash
python3 -m gmt bench --tiers 1,2
```

If tier 1 matches tier 2, pin it at 1 and you have a free task forever.

---

## The checklist

| # | Step | File |
|---|------|------|
| 1 | closed vocabulary | `gmt/tasks/_vocab.py` |
| 2 | the task | `gmt/tasks/t_<name>.py` |
| 3 | routing | `config/routing.yaml` |
| 4 | persona (optional) | `gmt/skills/<name>.md` |
| 5 | pipeline step (optional) | `gmt/pipelines/p_<name>.py` |
| 6 | golden rows + `CHECKS` | `tests/golden/`, `gmt/bench.py` |

Nothing else. No registry edit, no import, no MCP change — the task appears
in `python -m gmt tasks`, in the MCP tool list, and in the tests automatically.

---

## Mistakes worth avoiding

| Mistake | What happens | Do instead |
|---------|--------------|------------|
| Validating against a vocabulary you never showed the model | it writes reasonable English that fails your enum check | put the allowed values IN the prompt, not just in `check()` |
| Putting your field NAMES in front of the model | it emits `"roll_id"` as a value | pass values only, never a raw `json.dumps(fields)` |
| Rejecting what you could repair | a wasted model call and a lost row | `repair()` when there is exactly one legal reading |
| Free-text output field | you cannot group, join or score it | `enum=[...]` |
| No `check()` | confident invented answers get stored | write two rules minimum |
| Task does two things | small models fail, and you cannot tell which half | split it |
| Formatting in the prompt | inconsistent output forever | `postprocess()` in Python |
| Tuning the prompt first | you optimise against an unmeasured baseline | write `check()` and golden rows first |
| Editing a prompt, results unchanged | the artifact cache is serving old answers | bump `version` |
| `temperature > 0` for extraction | irreproducible output, unscorable | keep it at `0.0` |
