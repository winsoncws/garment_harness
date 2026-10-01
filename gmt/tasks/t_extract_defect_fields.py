"""
extract_defect_fields -- TIER 2. Pull the structured facts out of a note.
=======================================================================

Classification picks a label. THIS task pulls out the things you will
later JOIN on: which machine, which shift, which roll, how far along.
Extraction is strictly harder than classification -- there are many slots,
each can be absent, and "absent" must not become a hallucinated value.

    "L7 3rd shift, roll RB-4412, 60/40 pc, oil at 88.5m, 2 pcs, op Rahim"
                          |
                          v  qwen2.5:14b, temperature 0
    {"machine": "LOOM-07", "shift": "C", "roll_id": "RB-4412",
     "fabric_type": "poly_cotton_blend", "position_m": 88.5,
     "quantity": 2, "operator": "Rahim"}

THE ABSENT-FIELD PROBLEM
------------------------
Ask a model for `roll_id` when the note has none and it will cheerfully
produce "RB-0001". The three defences, all used below:

  1. every slot is `required=False` with an explicit default
  2. the system prompt states the null rule in the strongest terms
  3. check() rejects values that cannot be traced back to the input digits

Defence 3 is the real one. Prompts are advice; checks are enforcement.

NOTE ON MACHINE IDS
-------------------
`postprocess` canonicalises "L7", "loom 7", "LOOM7" -> "LOOM-07". Do this
normalisation in PYTHON, never in the prompt. It is deterministic, it is
free, and a model asked to do formatting will eventually do it differently.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ..core.contracts import Contract, Field
from ..core.registry import register
from ..core.task import Task
from ._vocab import FABRIC_TYPES, SHIFTS


@register
class ExtractDefectFields(Task):
    name = "extract_defect_fields"
    version = 2          # v2: split yarn_lot out of roll_id
    desc = ("Extract the machine, shift, roll, fabric and position mentioned "
            "in a fabric inspection note. Leave a field null if it is absent.")

    input = Contract("extract_defect_fields.in", {
        "text": Field("str", desc="the inspector's note, verbatim", max_len=4000),
    })

    output = Contract("extract_defect_fields.out", {
        "machine": Field("str", required=False, default="",
                         desc="machine or loom identifier exactly as written"),
        "line": Field("str", required=False, default="",
                      desc="production line, if stated"),
        "shift": Field("str", enum=SHIFTS, required=False, default="unknown",
                       desc="A=morning, B=afternoon, C=night"),
        "operator": Field("str", required=False, default="",
                          desc="operator name, if stated"),
        # MEASURED (2026-08-21): the old description said "roll / batch / lot
        # identifier", so the model correctly put yarn lot YL-88 here. The
        # model was right and the CONTRACT was wrong. Fixed by narrowing this
        # field and giving yarn lots their own -- which the supply-chain work
        # needs anyway. Do not tune a prompt to satisfy a bad contract.
        "roll_id": Field("str", required=False, default="",
                         desc="the FABRIC ROLL id (e.g. RB-4412, DN-0912, CT-2201). "
                              "NOT a yarn lot and NOT a dye batch."),
        "yarn_lot": Field("str", required=False, default="",
                          desc="the YARN LOT / raw-material lot id (e.g. YL-88, "
                               "VL-3301). This is what links a defect to a supplier."),
        "fabric_type": Field("str", enum=FABRIC_TYPES, required=False,
                             default="unknown", desc="fabric construction"),
        "position_m": Field("float", required=False, default=0.0, minimum=0.0,
                            desc="distance along the roll in METRES where the "
                                 "defect sits; 0 if not stated"),
        "quantity": Field("int", required=False, default=1, minimum=0,
                          desc="how many separate occurrences"),
    })

    # Persona lives in gmt/skills/extractor.md; this string is the fallback.
    skill = "extractor"
    system = (
        "You extract structured fields from short shop-floor textile notes.\n"
        "THE ONE RULE THAT MATTERS: if a field is not stated in the note, "
        "return its empty value (\"\" for text, 0 for numbers, \"unknown\" for "
        "shift and fabric). NEVER guess, NEVER invent an id, NEVER carry a "
        "value over from an example.\n"
        "Shift naming: 1st/morning/day = A, 2nd/afternoon/evening = B, "
        "3rd/night/graveyard = C.\n"
        "Copy identifiers exactly as written; normalisation happens later.\n"
        "Reply with JSON only."
    )

    def build_user(self, payload: Dict[str, Any]) -> str:
        return (
            f"{self.output.prompt_block()}\n\n"
            "Worked example -- note the empty fields:\n"
            "note: \"knitting m/c 12 night shift, drop stitch, 3 places\"\n"
            "json: {\"machine\": \"knitting m/c 12\", \"line\": \"\", \"shift\": \"C\", "
            "\"operator\": \"\", \"roll_id\": \"\", \"yarn_lot\": \"\", "
            "\"fabric_type\": \"unknown\", \"position_m\": 0, \"quantity\": 3}\n\n"
            f"Now this note:\n\"\"\"\n{payload['text']}\n\"\"\"\n\nJSON:"
        )

    # ---- enforcement ------------------------------------------------------
    def check(self, value: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        errs: List[str] = []
        text = (payload.get("text") or "").lower()
        digits_in_text = set(re.findall(r"\d+", text))

        # A number the note never mentions is a hallucination -- but a
        # CORRECT unit conversion is not.
        #
        # MEASURED 2026-08-21: this guard rejected position_m=2.7432 for a
        # note reading "3 yards from start". 2.7432 is exactly right and
        # 2.7432 appears nowhere in the text, so a naive digit check calls
        # the model a liar for doing the conversion we asked for.
        #
        # The fix reuses the tier-0 `normalize_units` task, which already
        # converts every measurement in a string to metres. Free, exact,
        # and it means the two tasks can never disagree about what "3 yards"
        # is worth.
        pos = value.get("position_m") or 0
        if pos and not _number_supported(pos, digits_in_text) \
                and not _matches_a_conversion(pos, payload.get("text", "")):
            errs.append(f"position_m={pos} is not supported by any number "
                        f"in the note, nor by any unit conversion of one")

        for key in ("roll_id", "yarn_lot"):
            val = (value.get(key) or "").strip()
            if val and not _token_supported(val, text):
                errs.append(f"{key}={val!r} does not appear in the note")

        mach = (value.get("machine") or "").strip()
        if mach and not _token_supported(mach, text):
            errs.append(f"machine={mach!r} does not appear in the note")

        if pos and pos > 5000:
            errs.append(f"position_m={pos} exceeds any plausible roll length")

        # A finished-fabric roll and an incoming yarn lot are different
        # things and can never share an id. When a note names only one, the
        # model (and the offline stub) will happily put it in both slots.
        rid = (value.get("roll_id") or "").strip().upper()
        lot = (value.get("yarn_lot") or "").strip().upper()
        if rid and rid == lot:
            errs.append(f"{rid!r} cannot be both roll_id and yarn_lot")
        return errs

    def postprocess(self, value: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        value["machine"] = canonical_machine(value.get("machine", ""))
        value["roll_id"] = (value.get("roll_id") or "").strip().upper()
        value["yarn_lot"] = (value.get("yarn_lot") or "").strip().upper()
        value["operator"] = (value.get("operator") or "").strip().title()
        return value

    def stub(self, payload: Dict[str, Any]) -> str:
        import json
        text = payload.get("text", "")
        low = text.lower()
        shift = "unknown"
        for pat, s in ((r"\b(1st|first|morning|day)\s*shift|shift\s*a\b", "A"),
                       (r"\b(2nd|second|afternoon|evening)\s*shift|shift\s*b\b", "B"),
                       (r"\b(3rd|third|night|graveyard)\s*shift|shift\s*c\b", "C")):
            if re.search(pat, low):
                shift = s
                break
        mach = re.search(
            r"\b((?:sewing|knitting|weaving|circular|dye)?\s*"
            r"(?:loom|l|m/c|machine|km|jigger|stenter|cutter)\s*[-#]?\s*\d+)\b", low)
        lot = re.search(r"(?:yarn|viscose|cotton|poly\w*)?\s*lot\s*[:#]?\s*"
                        r"([a-z]{1,3}[-_ ]?\d{2,6})\b", low)
        lot_id = lot.group(1) if lot else ""
        # mask the yarn lot out before hunting for a roll id, or the generic
        # "XX-9999" pattern below happily matches the lot a second time
        roll_hay = low.replace(lot_id, " ") if lot_id else low
        roll = re.search(r"\broll\s*[:#]?\s*([a-z]{1,3}[-_ ]?\d{2,6})\b"
                         r"|\b([a-z]{2,3}-\d{3,6})\b", roll_hay)
        pos = re.search(r"(?<![\d/])(\d+(?:\.\d+)?)\s*m\b", low)
        qty = re.search(r"(?<![\d/])(\d{1,3})\s*(?:pcs?|places|nos)\b", low)
        fabric = "unknown"
        for key, val in (("denim", "denim"), ("60/40", "poly_cotton_blend"),
                         ("pc", "poly_cotton_blend"), ("cotton", "cotton_woven"),
                         ("polyester", "polyester_woven"), ("viscose", "viscose")):
            if key in low:
                fabric = val
                break
        return json.dumps({
            "machine": mach.group(1) if mach else "",
            "line": "", "shift": shift, "operator": "",
            "roll_id": ((roll.group(1) or roll.group(2)) if roll else ""),
            "yarn_lot": lot_id,
            "fabric_type": fabric,
            "position_m": float(pos.group(1)) if pos else 0.0,
            "quantity": int(qty.group(1)) if qty else 1,
        })


# ---------------------------------------------------------------------------
# helpers -- deterministic normalisation, kept out of the prompt on purpose
# ---------------------------------------------------------------------------

_MACHINE_KIND = [
    (r"\b(?:loom|l)\b", "LOOM"),
    (r"knit|k/?m|circular", "KNIT"),
    (r"dye|jigger|jet", "DYE"),
    (r"stenter|finish", "FIN"),
    (r"cut(?:ter|ting)?", "CUT"),
    (r"sew|stitch(?:ing)? m", "SEW"),
]


def canonical_machine(raw: str) -> str:
    """'L7' / 'loom 7' / 'LOOM#07' -> 'LOOM-07'. Unknown kinds pass through."""
    s = (raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    num = re.search(r"(\d+)", low)
    # Blank out digits first so "l7" exposes a bare "l" for the \b(loom|l)\b rule.
    alpha = re.sub(r"\d+", " ", low)
    for pattern, kind in _MACHINE_KIND:
        if re.search(pattern, alpha):
            return f"{kind}-{int(num.group(1)):02d}" if num else kind
    return s.upper().replace(" ", "-")


def _number_supported(value: float, digits: set) -> bool:
    """Does this number's digit-string actually occur in the note?"""
    for form in (str(value), str(int(value)) if float(value).is_integer() else "",
                 f"{value:.1f}"):
        if not form:
            continue
        if form.replace(".", "") in {d for d in digits} or form.split(".")[0] in digits:
            return True
    return False


def _matches_a_conversion(value: float, text: str) -> bool:
    """Is `value` the metre-equivalent of some measurement in the note?

    Reuses gmt.tasks.t_normalize_units rather than re-implementing the
    conversion table. Module reuse is the point of the registry -- there is
    exactly one place in this repo that knows a yard is 0.9144 m.
    """
    from .t_normalize_units import NormalizeUnits
    got = NormalizeUnits().run_local({"text": text}) or {}
    return any(abs(float(value) - m) < 0.01 for m in got.get("metres", []))


def _token_supported(token: str, text: str) -> bool:
    """Loose containment: every alphanumeric chunk of the id must be in the note."""
    chunks = [c for c in re.findall(r"[a-z0-9]+", token.lower()) if c]
    if not chunks:
        return False
    return all(c in text for c in chunks)
