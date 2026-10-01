"""
defect_cnn -- TIER 0 (GPU). The expensive half of the delegated-CV pair.
=======================================================================

STATUS: this is a WORKING SHELL, not a trained model. Everything around
the model -- contract, ROI cropping, thresholding, review flagging,
provenance, storage -- is real and finished. The weights are the part you
supply once you have labelled data.

    +----------------+        +---------------+       +------------------+
    |  frame_gate    |  ROI   |   defect_cnn  | label |  defect_observ.  |
    |  (CPU, ~2ms)   | -----> |  (GPU, ~50ms) | ----> |  (ground truth)  |
    +----------------+        +---------------+       +------------------+
         0.1% pass                                     source='cv_model'

WHY IT IS A TASK AND NOT JUST A FUNCTION
-----------------------------------------
Because then it gets, for free and identically to every text task: a
declared output contract, caching by image hash, a trace row recording
which model version produced which prediction, a place in a chain, and an
MCP tool so an agent can call it. When you later swap ResNet for a ViT,
you bump `version` and every stored prediction stays attributable to the
model that made it.

HOW TO PLUG IN YOUR REAL MODEL
------------------------------
1. Train something that maps a crop -> one of _vocab.DEFECT_LABELS.
2. Save it as TorchScript:   torch.jit.script(model).save("defect_v1.pt")
   TorchScript is worth it: no python class definition needed at load time,
   so serving does not have to import your training code.
3. Point config at it:       export GMT_CNN_MODEL=/models/defect_v1.pt
4. Bump `version` here.      Old predictions remain tagged v1.

The `_load_model` function is deliberately the ONLY place torch is
mentioned, so the rest of the repo installs and runs without it.

CALIBRATION -- THE PART EVERYONE SKIPS
---------------------------------------
A raw softmax score is not a probability; a network trained on a balanced
set will report 0.97 on a defect class that occurs 1 in 10,000 times. Until
you have run temperature scaling on a held-out set, treat `confidence` as a
ranking signal only, and route anything under `review_below` to a human.
That is what `needs_review` is for.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.contracts import Contract, Field
from ..core.registry import register
from ..core.task import Task
from ._vocab import DEFECT_LABELS

_MODEL: Any = None
_MODEL_ERR = ""

CONFIG = {
    "input_size": 224,
    "review_below": 0.75,      # below this confidence a human must confirm
    "report_above": 0.30,      # below this we simply say "none"
}


def _load_model() -> Any:
    """Load TorchScript weights once. Returns None when unavailable.

    The ONLY torch import in the repo. Everything else stays importable on
    a laptop with no CUDA and no torch.
    """
    global _MODEL, _MODEL_ERR
    if _MODEL is not None or _MODEL_ERR:
        return _MODEL
    path = os.environ.get("GMT_CNN_MODEL", "")
    if not path:
        _MODEL_ERR = "GMT_CNN_MODEL is not set"
        return None
    if not Path(path).exists():
        _MODEL_ERR = f"model file not found: {path}"
        return None
    try:
        import torch                                    # type: ignore
        _MODEL = torch.jit.load(path, map_location="cuda" if torch.cuda.is_available() else "cpu")
        _MODEL.eval()
        return _MODEL
    except Exception as e:                              # noqa: BLE001
        _MODEL_ERR = f"could not load model: {e}"
        return None


@register
class DefectCNN(Task):
    name = "defect_cnn"
    version = 1
    desc = "Classify a fabric image crop into a defect label with a GPU CNN."

    input = Contract("defect_cnn.in", {
        "image_path": Field("str", desc="frame on disk"),
        "roi": Field("list", item_type="int", required=False, default=[],
                     desc="[x, y, w, h] from frame_gate; empty = whole frame"),
        "camera_id": Field("str", required=False, default="cam0"),
    })

    output = Contract("defect_cnn.out", {
        "label": Field("str", enum=DEFECT_LABELS, desc="predicted defect class"),
        "confidence": Field("float", minimum=0.0, maximum=1.0),
        "severity": Field("int", minimum=1, maximum=5,
                          desc="derived from confidence and class, not predicted"),
        "bbox": Field("list", item_type="int", required=False, default=[],
                      desc="[x, y, w, h] in the ORIGINAL frame's coordinates"),
        "model_version": Field("str", desc="which weights produced this"),
        "needs_review": Field("bool", desc="true when a human must confirm"),
    })

    def run_local(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        model = _load_model()
        if model is None:
            # No weights yet. Fail LOUD but do not crash the pipeline: mark
            # the frame for human review and record why.
            return {
                "label": "other", "confidence": 0.0, "severity": 3,
                "bbox": payload.get("roi") or [],
                "model_version": f"unavailable ({_MODEL_ERR})",
                "needs_review": True,
            }
        return _infer(model, payload, self.version)

    def check(self, value: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        if value["label"] != "none" and value["confidence"] <= 0 and \
                "unavailable" not in value.get("model_version", ""):
            return ["a defect was reported with zero confidence"]
        return []


def _infer(model: Any, payload: Dict[str, Any], version: int) -> Dict[str, Any]:
    """The real forward pass. Reached only when weights are present."""
    import cv2                                          # type: ignore
    import numpy as np                                  # type: ignore
    import torch                                        # type: ignore

    img = cv2.imread(payload["image_path"], cv2.IMREAD_COLOR)
    if img is None:
        return {"label": "other", "confidence": 0.0, "severity": 3, "bbox": [],
                "model_version": f"v{version}", "needs_review": True}

    roi = payload.get("roi") or []
    if len(roi) == 4:
        x, y, w, h = roi
        crop = img[max(0, y):y + h, max(0, x):x + w]
        if crop.size == 0:
            crop = img
    else:
        crop = img

    size = CONFIG["input_size"]
    crop = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
    crop = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype("float32") / 255.0
    # ImageNet normalisation -- match whatever your training used.
    mean = np.array([0.485, 0.456, 0.406], dtype="float32")
    std = np.array([0.229, 0.224, 0.225], dtype="float32")
    crop = (crop - mean) / std
    tensor = torch.from_numpy(crop.transpose(2, 0, 1)).unsqueeze(0)
    if torch.cuda.is_available():
        tensor = tensor.cuda()

    with torch.no_grad():
        logits = model(tensor)
        probs = torch.softmax(logits, dim=-1)[0].cpu().numpy()

    idx = int(probs.argmax())
    label = DEFECT_LABELS[idx] if idx < len(DEFECT_LABELS) else "other"
    conf = float(probs[idx])

    if conf < CONFIG["report_above"]:
        label, severity = "none", 1
    else:
        severity = 5 if conf > 0.95 and label in ("hole", "broken_end") else \
                   4 if conf > 0.85 else 3

    return {
        "label": label,
        "confidence": round(conf, 4),
        "severity": severity,
        "bbox": roi if len(roi) == 4 else [],
        "model_version": f"v{version}:{_weights_fingerprint()}",
        "needs_review": bool(conf < CONFIG["review_below"]),
    }


def _weights_fingerprint() -> str:
    """Short hash of the weights file, so a prediction is traceable to bytes."""
    path = os.environ.get("GMT_CNN_MODEL", "")
    p = Path(path)
    if not p.exists():
        return "none"
    h = hashlib.sha256()
    with p.open("rb") as fh:
        h.update(fh.read(1 << 20))          # first 1 MB is plenty to identify
    return h.hexdigest()[:8]
