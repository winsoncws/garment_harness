#!/usr/bin/env python3
"""
capture_frames.py -- collect training data from a camera, gated.
=======================================================================

START THIS EARLY. Data collection has a lead time that code does not: you
can write the CNN in a day, but you cannot label 2,000 crops you never
captured. Point this at a loom camera on day one, even before you have
decided on a model architecture.

WHAT IT DOES
------------
Reads a stream, runs the CPU gate on every frame, and saves ONLY the frames
the gate finds interesting -- plus a small random sample of boring frames,
which you need as negative examples.

    RTSP 30fps --> frame_gate --> interesting? --> frames/YYYYMMDD/keep/
                                       |
                                       +-- 1-in-N boring --> frames/.../bg/

    400 GB/day of raw video   -->   ~400 MB/day of useful frames

USAGE
-----
    python3 scripts/capture_frames.py --source 0                  # webcam
    python3 scripts/capture_frames.py --source rtsp://user:pw@ip/stream
    python3 scripts/capture_frames.py --source video.mp4 --fps 2

Needs opencv:  pip install opencv-python-headless numpy
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="0", help="camera index, file, or rtsp:// url")
    ap.add_argument("--out", default=str(ROOT / "data" / "frames"))
    ap.add_argument("--camera", default="cam0")
    ap.add_argument("--fps", type=float, default=2.0,
                    help="how many frames per second to actually examine")
    ap.add_argument("--bg-every", type=int, default=200,
                    help="also keep 1 in N boring frames as negatives")
    ap.add_argument("--max", type=int, default=0, help="stop after N kept frames")
    args = ap.parse_args()

    try:
        import cv2
    except ImportError:
        print("opencv is required:  pip install opencv-python-headless numpy")
        return 1

    from gmt.tasks.t_frame_gate import FrameGate

    src = int(args.source) if args.source.isdigit() else args.source
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        print(f"cannot open source: {args.source}")
        return 1

    day = datetime.now().strftime("%Y%m%d")
    keep_dir = Path(args.out) / day / args.camera / "keep"
    bg_dir = Path(args.out) / day / args.camera / "bg"
    keep_dir.mkdir(parents=True, exist_ok=True)
    bg_dir.mkdir(parents=True, exist_ok=True)

    gate = FrameGate()
    tmp = Path(args.out) / f".probe_{args.camera}.png"
    prev = ""
    interval = 1.0 / max(args.fps, 0.1)

    seen = kept = bg = 0
    reasons: dict = {}
    t_next = 0.0
    print(f"capturing from {args.source} -> {keep_dir}\nCtrl-C to stop\n")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            now = time.time()
            if now < t_next:
                continue                 # throttle: do not gate all 30 fps
            t_next = now + interval
            seen += 1

            # The gate reads from disk, so write a probe copy first. For a
            # production capture loop you would refactor the gate to accept
            # an array -- see the throughput notes in p_defect_inspection.py.
            cv2.imwrite(str(tmp), frame)
            verdict = gate.run_local({"image_path": str(tmp), "prev_path": prev,
                                      "camera_id": args.camera})
            reasons[verdict["reason"]] = reasons.get(verdict["reason"], 0) + 1

            stamp = datetime.now().strftime("%H%M%S_%f")[:-3]
            if verdict["send_to_gpu"]:
                out = keep_dir / f"{stamp}_a{verdict['anomaly']:.1f}.jpg"
                cv2.imwrite(str(out), frame)
                kept += 1
            elif random.randint(1, args.bg_every) == 1:
                cv2.imwrite(str(bg_dir / f"{stamp}.jpg"), frame)
                bg += 1

            prev = str(tmp)
            if seen % 50 == 0:
                print(f"  seen={seen} kept={kept} bg={bg}  "
                      f"keep_rate={100*kept/seen:.1f}%")
            if args.max and kept >= args.max:
                break
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        cap.release()
        tmp.unlink(missing_ok=True)

    print(f"\nexamined {seen}  kept {kept}  background {bg}")
    print("gate reasons:")
    for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {r:<32} {n}")
    print(f"\n-> {keep_dir}")
    print("\nNEXT: eyeball the kept frames. If they are all boring, raise")
    print("      anomaly_k in gmt/tasks/t_frame_gate.py. If real defects are")
    print("      landing in bg/, lower it. Tune the GATE before the model.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
