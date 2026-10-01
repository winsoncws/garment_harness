"""
p_defect_inspection -- the camera path. CPU gate -> GPU model -> ground truth.
=======================================================================

    frames/*.jpg  (30 fps from the loom camera)
          |
          v
    +--------------+  tier 0, CPU, ~2 ms
    |  frame_gate  |
    +--------------+
       |        |
   drop|        |send_to_gpu = True   (typically <1% of frames)
       v        v
     (log)  +--------------+  tier 0, GPU, ~50 ms
            |  defect_cnn  |
            +--------------+
                   |
                   v  confidence >= report_above
            +--------------------+
            | defect_observation |  source = 'cv_model'
            +--------------------+
                   |
                   v  needs_review = 1
            a human confirms  ->  becomes labelled training data
                                  for the NEXT version of the CNN

THE LOOP THAT MAKES THIS GET BETTER
------------------------------------
Low-confidence predictions are the most informative examples you own. They
are, almost by definition, the frames near the decision boundary. Route
them to a human, store the correction, and you have an active-learning loop
that costs nothing extra:

    predict -> flag the unsure ones -> human labels -> retrain -> repeat

That is why `needs_review` exists in the schema and why every prediction
carries `model_version`: you must be able to ask "which frames did v1 get
wrong that v2 gets right".

THROUGHPUT
----------
The gate is CPU and embarrassingly parallel; the CNN is GPU and should be
batched. This module runs them sequentially per frame for clarity. When you
need real throughput, see the note at the bottom of the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..core import trace
from ..core.runner import Step, map_chain
from ..core.store import Store, get_store


def _passed_gate(state: Dict[str, Any]) -> bool:
    return bool((state.get("gate") or {}).get("send_to_gpu"))


STEPS: List[Step] = [
    Step(task="frame_gate",
         prep=lambda s: {"image_path": s["image_path"],
                         "prev_path": s.get("prev_path", ""),
                         "camera_id": s.get("camera_id", "cam0")},
         key="gate"),

    Step(task="defect_cnn",
         prep=lambda s: {"image_path": s["image_path"],
                         "roi": (s.get("gate") or {}).get("roi", []),
                         "camera_id": s.get("camera_id", "cam0")},
         key="cnn",
         when=_passed_gate),          # <- the whole delegation, in one line
]


def run(frames: Sequence[str], *, camera_id: str = "cam0",
        store: Optional[Store] = None, concurrency: int = 4,
        observed_at: str = "") -> Dict[str, Any]:
    """Process an ordered list of frame paths from one camera."""
    st = store or get_store()

    # prev_path gives the gate its motion signal; frames must stay in order
    payloads: List[Dict[str, Any]] = []
    for i, path in enumerate(frames):
        payloads.append({
            "image_path": str(path),
            "prev_path": str(frames[i - 1]) if i else "",
            "camera_id": camera_id,
        })

    results = map_chain(STEPS, payloads, concurrency=concurrency, store=st,
                        stop_on_error=False)

    n_gated = n_inferred = n_defects = n_review = 0
    reasons: Dict[str, int] = {}

    for path, res in zip(frames, results):
        gate = res.state.get("gate") or {}
        reasons[gate.get("reason", "?")] = reasons.get(gate.get("reason", "?"), 0) + 1
        if not gate.get("send_to_gpu"):
            n_gated += 1
            continue

        n_inferred += 1
        cnn = res.state.get("cnn")
        if not cnn:
            continue

        # Register the image itself in bronze so the prediction is traceable
        # back to the exact pixels that produced it.
        blob_id = st.put_blob(kind="frame", path=str(path),
                              meta={"camera_id": camera_id,
                                    "roi": gate.get("roi", [])})

        if cnn["label"] == "none":
            continue

        n_defects += 1
        n_review += 1 if cnn.get("needs_review") else 0
        st.put_defect({
            "doc_id": None,
            "observed_at": observed_at or _mtime(path),
            "plant": "", "line": "", "machine": camera_id, "shift": "unknown",
            "operator": "", "roll_id": "", "fabric_type": "unknown",
            "defect_label": cnn["label"],
            "severity": cnn["severity"],
            "confidence": cnn["confidence"],
            "position_m": None, "width_cm": None, "quantity": 1,
            "source": "cv_model",
            "needs_review": 1 if cnn.get("needs_review") else 0,
        })
        trace.debug("cv defect", frame=Path(path).name, label=cnn["label"],
                    conf=cnn["confidence"], blob=blob_id)

    summary = {
        "frames": len(frames),
        "dropped_by_gate": n_gated,
        "sent_to_gpu": n_inferred,
        "gpu_saved_pct": round(100 * n_gated / max(len(frames), 1), 1),
        "defects": n_defects,
        "needs_review": n_review,
        "gate_reasons": reasons,
    }
    trace.info("inspection done", **{k: v for k, v in summary.items()
                                     if k != "gate_reasons"})
    return summary


def _mtime(path: str) -> str:
    from datetime import datetime, timezone
    p = Path(path)
    if not p.exists():
        return ""
    return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# WHEN YOU NEED REAL THROUGHPUT (notes for later, not needed today)
# ---------------------------------------------------------------------------
# 1. Run the gate in a multiprocessing.Pool -- it is pure CPU and the GIL
#    will otherwise cap you at one core.
# 2. Do NOT call the CNN one frame at a time. Collect the frames that pass
#    the gate into a queue and run the model on batches of 16-32; GPU
#    utilisation on single images is typically under 15%.
# 3. Read frames with cv2.VideoCapture directly off the RTSP stream rather
#    than writing every frame to disk. Only persist frames that pass the
#    gate -- that is the difference between 400 GB/day and 400 MB/day.
# 4. The natural shape for that is: capture thread -> gate process pool ->
#    batch queue -> GPU thread -> store writer. Build it when the simple
#    version is genuinely too slow, and not one day before.
