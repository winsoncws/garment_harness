"""
p_qa_ingest -- messy QA notes  ->  the single ground truth.
=======================================================================

THE FLOW
--------
                     raw note (a string)
                             |
                             v
                 [ BRONZE ] store.put_document()      <- immutable, always
                             |
        +--------------------+-------------------+
        |                    |                   |
        v                    v                   v
  classify_defect   extract_defect_fields   extract_triples
     (tier 1)             (tier 2)             (tier 2)
        |                    |                   |
        |                    |                   |   [both skipped when
        +--------------------+-------------------+    label == 'none']
                             |
                             v
                 [ SILVER ] artifact rows            <- automatic, in router
                             |
                             v
                 [ GOLD ] defect_observation + kg_edge
                             |
                             v
              dashboards, chatbot, supply-chain analytics

WHERE THE MONEY IS SAVED
------------------------
classify_defect runs on EVERY note at tier 1. The two tier-2 tasks run
only on notes that actually describe a defect -- in a real mill that is
roughly 30-40% of notes, because most are "roll checked, clean". So the
expensive step runs on a third of the volume, decided by the cheap step.

That branch is the `when=` argument on the Step. It is three words of code
and it is the single highest-leverage line in the pipeline.

RE-RUNNABILITY
--------------
Running this twice over the same notes is safe: documents dedupe on a
content hash, and task results come from the artifact cache. Bump a task's
`version` and only that task re-runs. This is what lets you improve one
prompt and reprocess two years of history overnight without touching
anything else.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from ..core import trace
from ..core.runner import ChainResult, Step, map_chain, progress
from ..core.store import Store, get_store
from ..tasks._vocab import owner_of

# ---------------------------------------------------------------------------
# the chain definition -- read this as the spec of the pipeline
# ---------------------------------------------------------------------------

def _hint(state: Dict[str, Any]) -> str:
    """Already-extracted ids, as `key=value` pairs. Empty fields omitted.

    MEASURED 2026-08-21: this used to be `json.dumps(fields)`, so the model
    was handed a blob containing the literal keys "roll_id", "yarn_lot",
    "machine" -- and it duly emitted a graph node named `roll_id`. Never put
    your schema's field NAMES in front of a model that is being asked for
    VALUES; it cannot always tell them apart. Send only the values that
    actually exist.
    """
    f = state.get("fields") or {}
    keep = ("machine", "roll_id", "yarn_lot", "operator", "fabric_type")
    parts = [f"{k}={f[k]}" for k in keep
             if f.get(k) and str(f[k]).strip() not in ("", "unknown")]
    return "; ".join(parts)


def _has_defect(state: Dict[str, Any]) -> bool:
    """The gate. Only notes describing a real defect go on to tier 2."""
    return (state.get("classify") or {}).get("label") not in (None, "none")


STEPS: List[Step] = [
    Step(task="classify_defect",
         prep=lambda s: {"text": s["text"]},
         key="classify"),

    Step(task="extract_defect_fields",
         prep=lambda s: {"text": s["text"]},
         key="fields",
         when=_has_defect),

    Step(task="extract_triples",
         prep=lambda s: {"text": s["text"], "hint": _hint(s)},
         key="triples",
         when=_has_defect),
]


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def run(notes: Sequence[Dict[str, Any]], *,
        store: Optional[Store] = None,
        concurrency: int = 4,
        source_system: str = "qa_tablet",
        verbose: bool = True) -> Dict[str, Any]:
    """Ingest a batch of notes.

    Each note: {"text": ..., "captured_at": ..., "source_uri": ...}
    Returns a small summary dict; the real output is in the database.
    """
    st = store or get_store()

    # ---- BRONZE: land the raw text before doing anything clever -----------
    docs: List[str] = []
    payloads: List[Dict[str, Any]] = []
    for n in notes:
        doc_id = st.put_document(
            source_system=source_system, doc_type="qa_report",
            content=n["text"], source_uri=n.get("source_uri", ""),
            captured_at=n.get("captured_at"),
            meta={k: v for k, v in n.items() if k not in ("text",)})
        docs.append(doc_id)
        payloads.append({"text": n["text"], "captured_at": n.get("captured_at")})

    # ---- SILVER: run the chain (concurrently) -----------------------------
    results = map_chain(STEPS, payloads, concurrency=concurrency, store=st,
                        stop_on_error=False)

    # ---- GOLD: project into the business tables ---------------------------
    n_obs = n_edges = n_clean = n_review = 0
    for doc_id, note, res in zip(docs, notes, results):
        made = _project(st, doc_id, note, res)
        n_obs += made["obs"]
        n_edges += made["edges"]
        n_clean += made["clean"]
        n_review += made["review"]

    summary = {
        "notes": len(notes),
        "documents": len(set(docs)),
        "observations": n_obs,
        "clean_notes": n_clean,
        "kg_edges": n_edges,
        "needs_review": n_review,
        "failed_chains": sum(1 for r in results if not r.ok),
    }
    if verbose:
        trace.info("qa_ingest done", **summary)
    return summary


def _project(st: Store, doc_id: str, note: Dict[str, Any],
             res: ChainResult) -> Dict[str, int]:
    """Turn one chain result into gold rows. Pure plumbing, no model calls."""
    made = {"obs": 0, "edges": 0, "clean": 0, "review": 0}
    cls = res.state.get("classify")
    if not cls:
        return made                              # classification itself failed

    if cls["label"] == "none":
        made["clean"] = 1
        return made

    f = res.state.get("fields", {})
    needs_review = bool(cls.get("needs_review")) or not res.ok

    st.put_defect({
        "doc_id": doc_id,
        "observed_at": note.get("captured_at") or "",
        "plant": note.get("plant", ""),
        "line": f.get("line", ""),
        "machine": f.get("machine", ""),
        "shift": f.get("shift", "unknown"),
        "operator": f.get("operator", ""),
        "roll_id": f.get("roll_id", ""),
        "yarn_lot": f.get("yarn_lot", ""),
        "fabric_type": f.get("fabric_type", "unknown"),
        "defect_label": cls["label"],
        "severity": cls["severity"],
        "confidence": cls["confidence"],
        "position_m": f.get("position_m", 0.0),
        "width_cm": None,
        "quantity": f.get("quantity", 1),
        "source": "human_report",
        "needs_review": 1 if needs_review else 0,
    })
    made["obs"] = 1
    made["review"] = 1 if needs_review else 0

    # ---- edges we can DERIVE, so we never ask a model for them ----------
    #
    # MEASURED (qwen2.5-coder:14b, 2026-08-21): asked to extract triples from
    # "L7 oil mark, roll RB-4412, yarn lot YL-88 from Anhui Textile", the
    # model correctly produced the yarn-lot and supplier edges but silently
    # DROPPED machine --produced--> defect, even though that exact edge is a
    # worked example in gmt/skills/graph_builder.md.
    #
    # The fix is not a better prompt. We already know the machine (from
    # extract_defect_fields) and the label (from classify_defect), so the
    # edge is a join, not an inference. Deriving it here is deterministic,
    # free, and cannot be forgotten -- and it narrows the model's job to the
    # edges only IT can find (suppliers, yarn lots, orders).
    derived = []
    machine, roll = f.get("machine", ""), f.get("roll_id", "")
    lot = f.get("yarn_lot", "")
    fabric, operator = f.get("fabric_type", "unknown"), f.get("operator", "")
    label = cls["label"]

    if machine:
        derived.append({"src": machine, "src_type": "machine",
                        "rel": "produced", "dst": label, "dst_type": "defect"})
    if roll:
        derived.append({"src": label, "src_type": "defect",
                        "rel": "observed_on", "dst": roll, "dst_type": "roll"})
    if roll and machine:
        derived.append({"src": roll, "src_type": "roll",
                        "rel": "ran_on", "dst": machine, "dst_type": "machine"})
    if roll and fabric != "unknown":
        derived.append({"src": roll, "src_type": "roll",
                        "rel": "made_of", "dst": fabric, "dst_type": "fabric"})
    if machine and operator:
        derived.append({"src": machine, "src_type": "machine",
                        "rel": "operated_by", "dst": operator, "dst_type": "operator"})

    # the supply-chain link: this is the edge the whole ERP story hangs on
    if lot:
        derived.append({"src": label, "src_type": "defect",
                        "rel": "caused_by", "dst": lot, "dst_type": "yarn_lot"})
    if lot and roll:
        derived.append({"src": roll, "src_type": "roll",
                        "rel": "made_of", "dst": lot, "dst_type": "yarn_lot"})

    # who owns fixing this defect -- a lookup table, never a prompt
    owner = owner_of(label)
    if owner != "-":
        derived.append({"src": label, "src_type": "defect",
                        "rel": "part_of", "dst": owner, "dst_type": "line"})

    # ---- plus whatever the model found that we could not derive ---------
    extracted = (res.state.get("triples") or {}).get("triples") or []

    made["edges"] = st.put_triples(derived + extracted, doc_id=doc_id)
    return made


def rebuild_metrics(store: Optional[Store] = None) -> int:
    """Refresh metric_daily from the gold table. Run nightly.

    Deliberately plain SQL: aggregates must be reproducible and auditable,
    so no model is anywhere near this step.
    """
    st = store or get_store()
    rows, err = st.query(
        "SELECT substr(observed_at,1,10) AS day, machine, COUNT(*) AS n, "
        "AVG(severity) AS avg_sev FROM defect_observation "
        "WHERE observed_at != '' GROUP BY day, machine", limit=100000)
    if err:
        trace.warn("rebuild_metrics failed", err=err)
        return 0
    for r in rows:
        st.put_metric(r["day"], "defect_count", float(r["n"]),
                      dimension=f"machine={r['machine']}", n=int(r["n"]))
        st.put_metric(r["day"], "avg_severity", float(r["avg_sev"] or 0),
                      dimension=f"machine={r['machine']}", n=int(r["n"]))
    return len(rows)
