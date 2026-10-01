"""
classify_defect -- TIER 1. Pick one label from a closed list.
=======================================================================

The archetypal small-model job: no generation, no reasoning chain, just
one choice out of thirteen plus two numbers. A 3B model does this well;
paying a frontier model for it is waste.

    "Loom 7, big oil smear down the selvedge, about 2m of it, shift B"
                          |
                          v  qwen2.5:3b, temperature 0, ~200ms
    {"label": "oil_mark", "severity": 4, "confidence": 0.9,
     "evidence": "big oil smear down the selvedge"}

THE GROUNDING CHECK (the part worth copying into every task you write)
----------------------------------------------------------------------
`check()` below rejects an answer whose `evidence` string does not actually
appear in the input text. A small model that invents a quote is a small
model that invented the label too. This one rule catches the single most
dangerous failure mode -- confident, well-formed, completely made-up
output that sails through type validation.

    type contract  ->  "is it the right SHAPE?"      (contracts.py)
    check()        ->  "is it CONSISTENT with the input?"  (here)

Both must pass before the answer is stored.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ..core.contracts import Contract, Field
from ..core.registry import register
from ..core.task import Task
from ._vocab import DEFECT_LABELS, severity_help

# Cheap keyword rules. Used for the offline stub, and worth keeping as a
# tier-0 shortcut once you have measured which keywords are unambiguous.
_RULES = [
    (r"\boil\b|\blubricant\b|\bgrease\b", "oil_mark"),
    (r"\bhole\b|\btorn?\b|\brip\b|\bpunctur", "hole"),
    (r"\bslub\b|\bthick (?:yarn|place)\b|\blump\b", "slub"),
    (r"\bwarp break|broken end\b|\bend down\b", "broken_end"),
    (r"\bweft break|broken pick\b|\bpick out\b", "broken_pick"),
    (r"\bdrop ?stitch\b|\bladder\b", "drop_stitch"),
    (r"\bfloat\b", "float"),
    (r"\bshade\b|\bcolou?r (?:variation|diff)|\bbarr[ée]\b|\btonal\b", "shade_variation"),
    (r"\bcrease\b|\bfold mark\b|\bwrinkle\b", "crease"),
    (r"\bsnag\b|\bpull(?:ed)? thread\b", "snag"),
    (r"\bcontaminat|\bforeign fib|\bdirt\b|\bfluff\b", "contamination"),
    (r"\bstain\b|\bspot\b|\bmark\b|\bsoil", "stain"),
    (r"\bno (?:defect|issue|fault|problem)s?\b|\bclean\b|\bpass(?:ed)?\b"
     r"|\bok\b|\bnil\b|\ball good\b|\bnothing to report\b"
     r"|\brunning fine\b|\bno probs?\b|\binspected ok\b", "none"),
]


@register
class ClassifyDefect(Task):
    name = "classify_defect"
    version = 2          # v2: needs_review no longer trusts self-reported confidence
    desc = ("Read a shop-floor fabric inspection note and classify the single "
            "most significant defect it describes.")

    input = Contract("classify_defect.in", {
        "text": Field("str", desc="the inspector's note, verbatim", max_len=4000),
    })

    output = Contract("classify_defect.out", {
        "label": Field("str", enum=DEFECT_LABELS,
                       desc="the single most significant defect"),
        "severity": Field("int", minimum=1, maximum=5,
                          desc="1=cosmetic .. 5=reject the roll"),
        "confidence": Field("float", minimum=0.0, maximum=1.0,
                            desc="how sure you are, 0.0 to 1.0"),
        "evidence": Field("str", max_len=200,
                          desc="the EXACT words from the note that show this defect. "
                               "Copy them; do not paraphrase."),
    })
    # `needs_review` and `confidence_informative` are added by postprocess(),
    # not asked of the model -- they are our judgement, not its claim.

    # The persona. Short, concrete, and it names the failure mode we fear.
    # Persona lives in gmt/skills/inspector.md; this string is the fallback.
    skill = "inspector"
    system = (
        "You are a textile quality inspector's assistant in a garment mill.\n"
        "You classify fabric defects from short, misspelled, jargon-heavy "
        "shop-floor notes.\n"
        "Rules:\n"
        "  - Choose exactly ONE label from the allowed list.\n"
        "  - If the note describes no defect at all, use 'none'.\n"
        "  - If it describes a real defect that is not on the list, use 'other'.\n"
        "  - `evidence` MUST be copied word-for-word from the note. Never "
        "invent a quote. If you cannot quote it, you cannot claim it.\n"
        "  - Reply with JSON only. No explanation, no markdown fence."
    )

    def build_user(self, payload: Dict[str, Any]) -> str:
        return (
            f"Severity scale:\n{severity_help()}\n\n"
            f"{self.output.prompt_block()}\n\n"
            f"Inspection note:\n\"\"\"\n{payload['text']}\n\"\"\"\n\n"
            f"JSON:"
        )

    # ---- business rules the type system cannot express --------------------
    def check(self, value: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        errs: List[str] = []
        text = _norm(payload.get("text", ""))
        label, ev = value["label"], _norm(value.get("evidence", ""))

        if label == "none":
            if value["severity"] > 1:
                errs.append("label 'none' cannot have severity > 1")
        else:
            if not ev:
                errs.append("evidence is required when a defect is reported")
            elif ev not in text and not _mostly_present(ev, text):
                # the anti-hallucination guard
                errs.append(f"evidence {value['evidence']!r} does not appear in the note")

        if value["confidence"] < 0.35 and value["severity"] >= 4:
            errs.append("severity >= 4 requires confidence >= 0.35")
        return errs

    # Above this, a self-reported confidence carries no information.
    # MEASURED on qwen2.5-coder:14b (2026-08-21): it returned exactly 1.0 on
    # every single note, correct or not. Small models are not calibrated and
    # asking them to be does not work -- so we must not build the human-review
    # safety net on that number alone, or the net silently never catches
    # anything.
    SATURATED = 0.99
    REVIEW_BELOW = 0.6

    def postprocess(self, value: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        """Decide whether a human must look. Never blocks the pipeline.

        Structural signals, in order of how much we trust them:
          1. the model said it was unsure              (rare, but believe it)
          2. it fell back to 'other'                   (taxonomy gap)
          3. severity 5 -- rejecting a roll costs real money, always confirm
          4. severity 4 with a saturated confidence    (high stakes, no signal)
          5. a defect claimed with almost no evidence quoted
        """
        conf = value["confidence"]
        sev = value["severity"]
        saturated = conf >= self.SATURATED
        thin_evidence = (value["label"] != "none"
                         and len(value.get("evidence", "").strip()) < 8)

        value["needs_review"] = bool(
            conf < self.REVIEW_BELOW
            or value["label"] == "other"
            or sev >= 5
            or (saturated and sev >= 4)
            or thin_evidence
        )
        # Keep the raw number, but record that it told us nothing. Without
        # this flag you cannot later tell "the model was sure" apart from
        # "the model always says 1.0".
        value["confidence_informative"] = not saturated
        return value

    # ---- offline / test behaviour -----------------------------------------
    def stub(self, payload: Dict[str, Any]) -> str:
        import json
        text = payload.get("text", "")
        low = text.lower()
        label, ev = "other", text[:60]
        for pattern, lab in _RULES:
            m = re.search(pattern, low)
            if m:
                label = lab
                ev = text[max(0, m.start() - 12): m.end() + 24].strip()
                break
        sev = 1 if label == "none" else (4 if re.search(r"\bbig|large|severe|major|reject", low) else 3)
        return json.dumps({"label": label, "severity": sev,
                           "confidence": 0.72 if label != "other" else 0.4,
                           "evidence": "" if label == "none" else ev})


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _mostly_present(evidence: str, text: str) -> bool:
    """Tolerate small quoting slips: 3+ of the evidence words must be in the note.

    Strict substring matching is too brittle -- models normalise whitespace
    and drop a trailing comma. This keeps the guard useful without making it
    fire on cosmetic differences.
    """
    words = [w for w in re.findall(r"[a-z0-9]+", evidence) if len(w) > 2]
    if not words:
        return False
    hits = sum(1 for w in words if w in text)
    return hits >= max(3, int(0.7 * len(words)))
