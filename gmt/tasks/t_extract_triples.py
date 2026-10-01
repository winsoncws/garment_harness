"""
extract_triples -- TIER 2. Text -> knowledge-graph edges.
=======================================================================

The same inspection note that fed the defect table also feeds the graph.
One source of truth, two shapes: rows for counting, edges for reasoning
about how things connect.

    "L7 oil mark, yarn lot YL-88 from Anhui Textile, roll RB-4412"
                              |
                              v
    machine:LOOM-07 --produced--> defect:oil_mark
    defect:oil_mark --observed_on--> roll:RB-4412
    roll:RB-4412 --ran_on--> machine:LOOM-07
    yarn_lot:YL-88 --supplied_by--> supplier:Anhui Textile

WHY BOTHER, WHEN YOU ALREADY HAVE SQL
-------------------------------------
SQL answers "how many oil marks on LOOM-07". The graph answers the
question you actually care about:

    "which SUPPLIER's yarn lots show up upstream of slub defects,
     across every machine, over any number of hops?"

That is a path query. In SQL it is a pile of self-joins whose depth you
must know in advance. In a graph it is one traversal. Keep both -- they
are cheap, they come from the same extraction pass, and they answer
different classes of question.

CLOSED VOCABULARY AGAIN
-----------------------
`src_type`, `rel`, `dst_type` are enums from _vocab.py. Free-text relation
names produce a graph with `supplies`, `supplied by`, `supplier_of` and
`SUPPLIES` as four distinct edges -- which is not a graph, it is a mess.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ..core.contracts import Contract, Field
from ..core.registry import register
from ..core.task import Task
from ._vocab import (DEFECT_LABELS, FABRIC_TYPES, NODE_TYPES, REL_TYPES,
                     SHIFTS, legal_shapes, signature_ok)

# Node types whose value is a real-world IDENTIFIER taken VERBATIM from the
# note. These must be quotable from the text -- inventing one poisons the
# graph with a node that refers to nothing and will never join to anything.
#
# `machine` is deliberately NOT in this set. Machine ids are canonicalised
# in python ("loom 7" / "L7" -> "LOOM-07"), so by design they no longer
# appear verbatim in the note and grounding them would reject every correct
# answer. The machine edges are derived in p_qa_ingest anyway, so the model
# should not be emitting them at all. Ground what we take verbatim; do not
# ground what we normalise.
IDENTIFIER_TYPES = {"roll", "yarn_lot", "supplier",
                    "operator", "order", "customer"}

# Node types whose value is a CONTROLLED TERM, not a quote. "oil_mark" is
# the right answer for a note that says "oil smear", so grounding these
# against the raw text would be wrong.
CONTROLLED_TYPES = {"defect": DEFECT_LABELS,
                    "fabric": FABRIC_TYPES,
                    "shift": SHIFTS}


@register
class ExtractTriples(Task):
    name = "extract_triples"
    version = 4          # v4: reversed edges are repaired instead of rejected
    desc = ("Extract knowledge-graph triples (source, relation, target) from a "
            "manufacturing note. Only facts stated in the text.")

    input = Contract("extract_triples.in", {
        "text": Field("str", desc="the note", max_len=4000),
        "hint": Field("str", required=False, default="",
                      desc="already-extracted fields, as a hint"),
    })

    # A list-of-dicts output. The item shape is enforced in check(), because
    # the mini contract layer validates the top level; see the note there.
    output = Contract("extract_triples.out", {
        "triples": Field("list", item_type="dict", max_len=20,
                         desc="each item: {src, src_type, rel, dst, dst_type}"),
        "n": Field("int", minimum=0, desc="how many triples you produced"),
    })

    # Persona lives in gmt/skills/graph_builder.md; this string is the fallback.
    skill = "graph_builder"
    system = (
        "You build a manufacturing knowledge graph from shop-floor notes.\n"
        "Emit only relationships STATED in the text. No inference, no "
        "background knowledge, no filler edges.\n"
        "Use only the allowed node types and relation names.\n"
        "If the note states nothing relational, return an empty list.\n"
        "Reply with JSON only."
    )

    def build_user(self, payload: Dict[str, Any]) -> str:
        hint = f"\nAlready extracted (use these exact ids): {payload['hint']}\n" if payload.get("hint") else ""
        return (
            f"Allowed node types : {', '.join(NODE_TYPES)}\n"
            f"Allowed relations  : {', '.join(REL_TYPES)}\n\n"
            # MEASURED 2026-08-21: without this block the model wrote
            # "float defect", "ladder fault", "foreign fibre contamination"
            # -- all reasonable English, none of them the enum. We had added
            # the controlled-vocabulary CHECK without ever telling the model
            # the vocabulary. Validate against a list, and you must also
            # SHOW that list.
            "A node of type 'defect' MUST use one of these exact values:\n"
            f"  {', '.join(DEFECT_LABELS)}\n"
            "A node of type 'fabric' MUST use one of these exact values:\n"
            f"  {', '.join(FABRIC_TYPES)}\n\n"
            "The ONLY valid edge shapes (direction matters):\n"
            "  machine  --produced-->     defect\n"
            "  defect   --observed_on-->  roll        (NOT roll -> defect)\n"
            "  roll     --ran_on-->       machine\n"
            "  roll     --made_of-->      fabric | yarn_lot\n"
            "  yarn_lot --supplied_by-->  supplier\n"
            "  machine  --operated_by-->  operator\n"
            "  defect   --caused_by-->    yarn_lot | machine | operator | shift\n"
            "  order    --ordered_by-->   customer\n\n"
            "Shape:\n"
            '{"triples": [{"src": "LOOM-07", "src_type": "machine", '
            '"rel": "produced", "dst": "oil_mark", "dst_type": "defect"}], "n": 1}\n'
            f"{hint}\n"
            f"Note:\n\"\"\"\n{payload['text']}\n\"\"\"\n\nJSON:"
        )

    def repair(self, value: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        """Flip edges the model wrote backwards.

        MEASURED 2026-08-21: the 14B produced `roll --observed_on--> defect`.
        The relation only connects defect -> roll, so there is exactly one
        thing it could have meant and swapping is safe.

        Note what is NOT repaired: `yarn_lot --made_of--> fabric`. Its
        reverse (fabric -> yarn_lot) is not legal either, so the model did
        not reverse an edge -- it invented a relationship. That one is
        correctly rejected.
        """
        for t in value.get("triples") or []:
            if not isinstance(t, dict):
                continue
            st, rel, dt = t.get("src_type"), t.get("rel"), t.get("dst_type")
            if signature_ok(st, rel, dt):
                continue
            if signature_ok(dt, rel, st):          # the reverse is the only legal form
                t["src"], t["dst"] = t.get("dst"), t.get("src")
                t["src_type"], t["dst_type"] = dt, st
        return value

    def check(self, value: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        """Validate each triple by hand -- this is the per-item contract."""
        errs: List[str] = []
        triples = value.get("triples") or []
        for i, t in enumerate(triples):
            if not isinstance(t, dict):
                errs.append(f"triples[{i}] is not an object")
                continue
            for key in ("src", "src_type", "rel", "dst", "dst_type"):
                if not str(t.get(key, "")).strip():
                    errs.append(f"triples[{i}].{key} is missing")
            for role in ("src", "dst"):
                # NB: `node` -- do NOT call this `value`; that is the name of
                # this method's parameter and shadowing it silently breaks
                # every check below this loop.
                ntype = t.get(f"{role}_type")
                node = str(t.get(role, "")).strip()
                if not node:
                    continue
                if ntype in IDENTIFIER_TYPES and not _grounded(node, payload.get("text", "")):
                    # OBSERVED 2026-08-21: the model emitted
                    #   {"src": "roll_id", "src_type": "roll", ...}
                    # -- it used the FIELD NAME as the value. Every type was
                    # legal and the signature was legal; only the identity
                    # was fictional. An ungrounded identifier is a graph node
                    # that refers to nothing, and it will never join to
                    # anything, so it is pure noise forever.
                    errs.append(f"triples[{i}].{role}={node!r} does not appear "
                                f"in the note (a {ntype} id must be quoted from it)")
                allowed = CONTROLLED_TYPES.get(ntype or "")
                if allowed is not None and node not in allowed:
                    errs.append(f"triples[{i}].{role}={node!r} is not an allowed "
                                f"{ntype} (use one of the listed values)")

            if t.get("src_type") not in NODE_TYPES:
                errs.append(f"triples[{i}].src_type={t.get('src_type')!r} not allowed")
            if t.get("dst_type") not in NODE_TYPES:
                errs.append(f"triples[{i}].dst_type={t.get('dst_type')!r} not allowed")
            rel = t.get("rel")
            if rel not in REL_TYPES:
                errs.append(f"triples[{i}].rel={rel!r} not allowed")
            elif not signature_ok(t.get("src_type"), rel, t.get("dst_type")):
                # the endpoints are both legal node types, but not for THIS
                # relation -- e.g. yarn_lot --supplied_by--> machine
                errs.append(
                    f"triples[{i}]: {t.get('src_type')} --{rel}--> "
                    f"{t.get('dst_type')} is not a valid shape. "
                    f"{rel} connects {legal_shapes(rel)}")
        if value.get("n") != len(triples):
            errs.append(f"n={value.get('n')} but {len(triples)} triples were given")
        return errs

    def stub(self, payload: Dict[str, Any]) -> str:
        import json
        import re
        text = payload.get("text", "")
        from .t_extract_defect_fields import canonical_machine
        from .t_classify_defect import _RULES
        low = text.lower()
        machine = ""
        m = re.search(
            r"\b((?:sewing|knitting|weaving|circular|dye)?\s*"
            r"(?:loom|l|m/c|machine|km|jigger|stenter|cutter)\s*[-#]?\s*\d+)\b", low)
        if m:
            machine = canonical_machine(m.group(1))
        label = ""
        for pattern, lab in _RULES:
            if re.search(pattern, low) and lab != "none":
                label = lab
                break
        roll = re.search(r"\b([a-z]{2,3}-\d{3,6})\b", low)
        triples = []
        if machine and label:
            triples.append({"src": machine, "src_type": "machine",
                            "rel": "produced", "dst": label, "dst_type": "defect"})
        if roll and label:
            triples.append({"src": label, "src_type": "defect", "rel": "observed_on",
                            "dst": roll.group(1).upper(), "dst_type": "roll"})
        if roll and machine:
            triples.append({"src": roll.group(1).upper(), "src_type": "roll",
                            "rel": "ran_on", "dst": machine, "dst_type": "machine"})
        return json.dumps({"triples": triples, "n": len(triples)})


def _grounded(value: str, text: str) -> bool:
    """Does this identifier actually occur in the note?

    Loose containment: every alphanumeric chunk of the id must appear
    somewhere in the text. That tolerates "LOOM-07" for "loom 7" and
    "L7" -> "l7", while still rejecting a wholly invented id.
    """
    low = (text or "").lower()
    chunks = [c for c in re.findall(r"[a-z0-9]+", value.lower()) if c]
    if not chunks:
        return False
    for c in chunks:
        # tolerate zero padding: "07" should match a note that says "7"
        stripped = c.lstrip("0") or "0"
        if c not in low and stripped not in low:
            return False
    return True
