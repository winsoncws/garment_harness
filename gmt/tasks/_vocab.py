"""
_vocab.py -- the shared, closed vocabularies. Import these, never retype them.
=======================================================================

WHY A CLOSED VOCABULARY IS THE HIGHEST-LEVERAGE THING IN THIS REPO
------------------------------------------------------------------
An open-ended label ("describe the defect") gives you 400 spellings of
"oil stain" and a dataset nobody can group by. A closed list gives you:

    * a small model that can actually succeed (pick 1 of 13, not invent text)
    * contract validation that MEANS something
    * SQL that works                (GROUP BY defect_label)
    * a knowledge graph with real nodes, not string soup
    * an eval set you can score with ==

Every one of your seven initiatives -- CV, chatbot, supply chain, business
analytics, market, knowledge graph, physics -- reads these same constants.
That is what makes the "one database, one ground truth" promise real.

CHANGING THIS LIST IS A SCHEMA CHANGE. Bump `project.schema_version` in
config/settings.yaml and the affected tasks' `version`, so old rows stay
interpretable.

    text report ---+
                   +--> classify_defect --> DEFECT_LABELS -----+
    camera frame --+                                            +--> one
                                                                |    defect_label
    CNN logits ------> defect_cnn -------> DEFECT_LABELS -------+    column
"""

from __future__ import annotations

from typing import Dict, List

# --- the defect taxonomy ---------------------------------------------------
# Grouped by where they come from, because that is how you triage them.
DEFECT_LABELS: List[str] = [
    # weaving / knitting faults
    "hole",
    "slub",              # thick lump in the yarn
    "broken_end",        # a warp thread snapped
    "broken_pick",       # a weft thread snapped
    "drop_stitch",       # knitting
    "float",             # yarn passing over wrong number of threads
    # contamination / handling
    "stain",
    "oil_mark",          # machine lubricant - points at maintenance
    "contamination",     # foreign fibre, dirt
    # finishing / dyeing
    "shade_variation",   # colour differs across the roll
    "crease",
    # physical damage
    "snag",
    # the two escape hatches -- ALWAYS include these
    "none",              # the text/image shows no defect
    "other",             # a real defect that is not on this list
]

# Which department owns the fix. Drives routing of alerts, and later the KG.
DEFECT_OWNER: Dict[str, str] = {
    "hole": "weaving", "slub": "spinning", "broken_end": "weaving",
    "broken_pick": "weaving", "drop_stitch": "knitting", "float": "weaving",
    "stain": "handling", "oil_mark": "maintenance",
    "contamination": "raw_material", "shade_variation": "dyeing",
    "crease": "finishing", "snag": "handling",
    "none": "-", "other": "quality",
}

SEVERITY_SCALE = {
    1: "cosmetic, passes as first quality",
    2: "minor, allowed within tolerance",
    3: "noticeable, downgrade the roll",
    4: "major, cut out the affected section",
    5: "critical, reject the roll / stop the machine",
}

SHIFTS: List[str] = ["A", "B", "C", "unknown"]

FABRIC_TYPES: List[str] = [
    "cotton_woven", "cotton_knit", "polyester_woven", "polyester_knit",
    "poly_cotton_blend", "viscose", "denim", "linen", "nylon", "wool",
    "unknown",
]

# --- knowledge-graph vocabulary -------------------------------------------
NODE_TYPES: List[str] = [
    "machine", "operator", "shift", "defect", "fabric", "roll",
    "supplier", "yarn_lot", "order", "customer", "plant", "line",
]

# A relation is only meaningful between certain KINDS of node. Listing the
# legal shapes is what turns a bag of edges into a graph you can trust.
#
# MEASURED 2026-08-21: qwen2.5-coder:14b, given
#   "shade diff between left n right side, dye jigger 2, viscose lot VL-3301"
# emitted  yarn_lot:VL-3301 --supplied_by--> machine:DYE-02.
# Every part of that passed the old validation: `yarn_lot` is a node type,
# `machine` is a node type, `supplied_by` is a relation. Only the COMBINATION
# is nonsense -- a yarn lot is not supplied by a loom, and the note names no
# supplier at all. Type-checking the endpoints is not enough; the signature
# has to be checked too.
REL_SIGNATURES: Dict[str, List[tuple]] = {
    "produced":    [("machine", "defect")],
    "observed_on": [("defect", "roll")],
    "ran_on":      [("roll", "machine")],
    "supplied_by": [("yarn_lot", "supplier"), ("fabric", "supplier")],
    "made_of":     [("roll", "fabric"), ("roll", "yarn_lot")],
    "operated_by": [("machine", "operator")],
    "part_of":     [("line", "plant"), ("machine", "line"), ("defect", "line")],
    "ordered_by":  [("order", "customer")],
    "caused_by":   [("defect", "yarn_lot"), ("defect", "machine"),
                    ("defect", "operator"), ("defect", "shift")],
}

REL_TYPES: List[str] = list(REL_SIGNATURES)


def signature_ok(src_type: str, rel: str, dst_type: str) -> bool:
    return (src_type, dst_type) in REL_SIGNATURES.get(rel, [])


def legal_shapes(rel: str) -> str:
    """Human-readable list of what this relation may connect."""
    pairs = REL_SIGNATURES.get(rel, [])
    return " or ".join(f"{a} -> {b}" for a, b in pairs) or "(unknown relation)"

# --- unit conversion, used by the tier-0 task ------------------------------
# Factory floors mix metric and imperial in the same sentence, constantly.
TO_METRES: Dict[str, float] = {
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
    "cm": 0.01, "mm": 0.001,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144, "yds": 0.9144,
    "ft": 0.3048, "feet": 0.3048, "foot": 0.3048,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254,
}


def owner_of(label: str) -> str:
    return DEFECT_OWNER.get(label, "quality")


def severity_help() -> str:
    """Rendered into prompts so the model grades consistently."""
    return "\n".join(f"  {k} = {v}" for k, v in SEVERITY_SCALE.items())
