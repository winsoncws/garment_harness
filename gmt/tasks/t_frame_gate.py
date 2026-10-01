"""
frame_gate -- TIER 0. The cheap OpenCV gate in front of the GPU.
=======================================================================

THE DELEGATED-CV PATTERN YOU DESCRIBED
--------------------------------------
A loom camera at 30 fps produces 2.6 million frames a day. A CNN cannot
see all of them and does not need to: the overwhelming majority are
in-focus, unremarkable, and identical to the frame before.

    camera 30fps
         |
         v
    +----------------------+   ~1-3 ms per frame, CPU, no model
    |     frame_gate       |
    |  1. sharp enough?    |--no--> DROP  (blurred: the CNN would guess)
    |  2. exposed OK?      |--no--> DROP  (blown/black: no information)
    |  3. new fabric?      |--no--> DROP  (loom stopped: same 2cm of cloth)
    |  4. texture anomaly? |--no--> DROP  (perfectly periodic weave = fine)
    +----------------------+
         | yes to all
         v
    +----------------------+   ~30-80 ms per frame, GPU
    |     defect_cnn       |   runs on maybe 0.1% of frames
    +----------------------+

Typical effect: 30 fps into the gate, 0.05-0.5 fps into the GPU. One GPU
then covers dozens of cameras instead of one.

THE TEXTURE TRICK (step 4)
--------------------------
Woven fabric is close to perfectly periodic. Average the greyscale down to
a row profile and a column profile, and a clean roll gives two almost-flat
lines. A defect is a *break in periodicity*, so it shows up as a spike in
one or both profiles:

    column profile of clean fabric   column profile with a broken end
    ~~~~~~~~~~~~~~~~~~~~~~~~~        ~~~~~~~~~~~~~^~~~~~~~~~~~~
                                                  |
                                            spike -> ROI to crop for the CNN

We score with a MEDIAN + MAD threshold, not mean + std, because a single
big defect drags the mean toward itself and hides the very thing you are
looking for. Median absolute deviation does not care about outliers, which
is exactly the property you want in an outlier detector.

FAIL-OPEN, ALWAYS
-----------------
If OpenCV is missing or the file will not decode, this gate returns
send_to_gpu=True. A gate that fails CLOSED silently drops defects and you
find out three months later from a customer. A gate that fails OPEN only
costs GPU time. Make that trade every single time.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.contracts import Contract, Field
from ..core.registry import register
from ..core.task import Task

# Optional imports. The whole module degrades gracefully without them.
try:
    import cv2                     # type: ignore
    import numpy as np             # type: ignore
    _CV = True
except ImportError:                # pragma: no cover - depends on machine
    cv2 = None                     # type: ignore
    np = None                      # type: ignore
    _CV = False


# Tune these on YOUR camera and YOUR fabric. The numbers below are sane
# starting points for a 1080p top-lit loom camera; they are not universal.
DEFAULTS = {
    "min_sharpness": 60.0,     # Laplacian variance. Below this = out of focus.
    "min_brightness": 35.0,    # 0-255 mean. Below = underexposed.
    "max_brightness": 220.0,   # above = blown out
    "min_motion": 1.5,         # mean abs frame diff. Below = loom stopped.
    "anomaly_k": 4.0,          # spike must exceed median + k*MAD
    "roi_pad": 24,             # pixels of context around a detected spike
}


@register
class FrameGate(Task):
    name = "frame_gate"
    version = 1
    desc = ("Decide whether a camera frame is worth sending to the GPU defect "
            "model, using cheap CPU image statistics only.")

    input = Contract("frame_gate.in", {
        "image_path": Field("str", desc="path to the frame on disk"),
        "prev_path": Field("str", required=False, default="",
                           desc="the previous frame, for motion detection"),
        "camera_id": Field("str", required=False, default="cam0"),
    })

    output = Contract("frame_gate.out", {
        "send_to_gpu": Field("bool", desc="true if the CNN should look at this frame"),
        "reason": Field("str", desc="why it was kept or dropped"),
        "sharpness": Field("float", desc="Laplacian variance"),
        "brightness": Field("float", desc="mean grey level 0-255"),
        "motion": Field("float", desc="mean abs difference vs previous frame"),
        "anomaly": Field("float", desc="peak profile deviation in MAD units"),
        "roi": Field("list", item_type="int", required=False, default=[],
                     desc="[x, y, w, h] region to crop for the CNN; empty = whole frame"),
    })

    def run_local(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        path = payload["image_path"]

        if not _CV:
            return _verdict(True, "opencv_unavailable_fail_open")

        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return _verdict(True, "unreadable_fail_open")

        # ---- 1. focus ------------------------------------------------------
        sharpness = float(cv2.Laplacian(img, cv2.CV_64F).var())
        if sharpness < DEFAULTS["min_sharpness"]:
            return _verdict(False, "too_blurry", sharpness=sharpness)

        # ---- 2. exposure ---------------------------------------------------
        brightness = float(img.mean())
        if brightness < DEFAULTS["min_brightness"]:
            return _verdict(False, "underexposed", sharpness, brightness)
        if brightness > DEFAULTS["max_brightness"]:
            return _verdict(False, "overexposed", sharpness, brightness)

        # ---- 3. motion (is this new fabric, or a stopped loom?) -------------
        motion = -1.0
        prev_path = payload.get("prev_path") or ""
        if prev_path:
            prev = cv2.imread(prev_path, cv2.IMREAD_GRAYSCALE)
            if prev is not None and prev.shape == img.shape:
                motion = float(cv2.absdiff(img, prev).mean())
                if motion < DEFAULTS["min_motion"]:
                    return _verdict(False, "no_motion_duplicate_frame",
                                    sharpness, brightness, motion)

        # ---- 4. texture anomaly --------------------------------------------
        anomaly, roi = _profile_anomaly(img)
        if anomaly < DEFAULTS["anomaly_k"]:
            return _verdict(False, "texture_uniform", sharpness, brightness,
                            motion, anomaly)

        return _verdict(True, "anomaly_detected", sharpness, brightness,
                        motion, anomaly, roi)

    def stub(self, payload: Dict[str, Any]) -> str:      # never reached (tier 0)
        import json
        return json.dumps(_verdict(True, "stub"))


# ---------------------------------------------------------------------------
# the actual image maths
# ---------------------------------------------------------------------------

def _profile_anomaly(img: Any) -> Any:
    """Return (peak deviation in MAD units, [x, y, w, h] or []).

    Rows and columns are averaged into two 1-D profiles. A clean weave gives
    flat profiles; a defect gives a spike. Scoring in MAD units makes the
    threshold comparable across fabrics, lighting and camera gain -- which
    is what lets you keep ONE threshold instead of one per product.
    """
    small = cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (5, 5), 0).astype("float32")

    col = small.mean(axis=0)          # 256 values, one per column
    row = small.mean(axis=1)          # 256 values, one per row

    cs, ci = _mad_peak(col)
    rs, ri = _mad_peak(row)
    score = float(max(cs, rs))
    if score < DEFAULTS["anomaly_k"]:
        return score, []

    h, w = img.shape[:2]
    pad = DEFAULTS["roi_pad"]
    cx = int(ci * w / 256)
    cy = int(ri * h / 256)
    # Widen along the weaker axis: a broken end is a full-height stripe, so
    # a tight square box would cut the evidence in half.
    if cs >= rs:
        x0, y0 = max(0, cx - pad), 0
        bw, bh = min(w - x0, 2 * pad), h
    else:
        x0, y0 = 0, max(0, cy - pad)
        bw, bh = w, min(h - y0, 2 * pad)
    return score, [int(x0), int(y0), int(bw), int(bh)]


def _mad_peak(profile: Any) -> Any:
    """(largest deviation in MAD units, index of it)."""
    med = float(np.median(profile))
    mad = float(np.median(np.abs(profile - med))) or 1e-6
    dev = np.abs(profile - med) / (1.4826 * mad)     # 1.4826 -> sigma-equivalent
    idx = int(np.argmax(dev))
    return float(dev[idx]), idx


def _verdict(send: bool, reason: str, sharpness: float = -1.0,
             brightness: float = -1.0, motion: float = -1.0,
             anomaly: float = -1.0, roi: Optional[List[int]] = None) -> Dict[str, Any]:
    return {"send_to_gpu": send, "reason": reason,
            "sharpness": round(sharpness, 2), "brightness": round(brightness, 2),
            "motion": round(motion, 3), "anomaly": round(anomaly, 2),
            "roi": roi or []}
