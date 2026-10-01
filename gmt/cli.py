"""
cli.py -- one entry point for everything.  `python -m gmt <command>`
=======================================================================

| command   | what it does                                              |
|-----------|-----------------------------------------------------------|
| doctor    | check python, ollama, models, database. RUN THIS FIRST.   |
| init      | create data/garment.db from data/schema.sql               |
| tasks     | list every registered task and how it will be routed      |
| ingest    | run the QA-note pipeline over data/seed/qa_reports.jsonl  |
| ask       | natural-language question -> SQL -> grounded answer       |
| inspect   | run the camera pipeline over a folder of frames           |
| bench     | score tasks against tests/golden/ at each tier            |
| stats     | what is in the database, and how the models are doing     |

Everything accepts --offline to run with the fake model (no GPU needed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    from .core import config, llm
    from .core.store import Store

    cfg = config.load()
    ok = True
    print("=" * 68)
    print("GARMENT HARNESS -- DOCTOR")
    print("=" * 68)

    print(f"\n[python]  {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 9):
        print("  !! python 3.9+ required")
        ok = False

    print("\n[optional packages]  (none of these are required to run)")
    for mod, why in (("yaml", "nicer YAML parsing"),
                     ("cv2", "the frame_gate task"),
                     ("numpy", "frame_gate + defect_cnn"),
                     ("torch", "defect_cnn inference"),
                     ("pandas", "analysis notebooks")):
        try:
            __import__(mod)
            print(f"  {'OK':<4} {mod:<8} {why}")
        except ImportError:
            print(f"  {'--':<4} {mod:<8} {why}")

    db = cfg.get("store.path")
    print(f"\n[database]  {db}")
    if Path(db).exists():
        st = Store()
        counts = st.counts()
        print("  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    else:
        print("  !! does not exist yet -- run:  python -m gmt init")
        ok = False

    host = cfg.get("ollama.host")
    print(f"\n[ollama]  {host}")
    probe = llm.OllamaClient(cfg.get("models.tier2.model", ""))
    available = probe.list_models()
    if not available:
        print("  !! not reachable.")
        print("     - on the GPU box:   ollama serve")
        print("     - from elsewhere:   export GMT_OLLAMA_HOST=http://<gpu-ip>:11434")
        print("       (and on the GPU box: OLLAMA_HOST=0.0.0.0 ollama serve)")
        print("     - no GPU right now: add --offline to any command")
        ok = False
    else:
        print(f"  reachable, {len(available)} model(s):")
        for m in available[:12]:
            print(f"     {m}")
        for tier in (1, 2):
            want = cfg.get(f"models.tier{tier}.model")
            # Match on the BASE name (before the colon), not a prefix.
            # A prefix test says "qwen2.5-coder:14b" satisfies "qwen2.5:14b",
            # which is a different model -- a false READY is worse than a
            # false MISSING, because you only find out from bad output.
            base = want.split(":")[0]
            exact = want in available
            near = [m for m in available if m.split(":")[0] == base and m != want]
            if exact:
                status = "FOUND"
            elif near:
                status = f"MISSING (same family: {', '.join(near)}) -> ollama pull {want}"
            else:
                status = f"MISSING -> ollama pull {want}"
            print(f"  tier{tier} wants {want:<22} {status}")
            ok = ok and exact

    print(f"\n[tier 3]  anthropic  "
          f"{'key present' if os.environ.get('ANTHROPIC_API_KEY') else 'no ANTHROPIC_API_KEY (fine - tier 3 disabled)'}")

    print("\n" + "=" * 68)
    print("READY" if ok else "NOT READY -- see the !! lines above")
    print("=" * 68)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    from .core import config
    from .core.store import Store
    st = Store()
    st.init_schema()
    print(f"schema applied to {config.load().get('store.path')}")
    for k, v in st.counts().items():
        print(f"  {k:<20} {v}")
    return 0


# ---------------------------------------------------------------------------
def cmd_tasks(args: argparse.Namespace) -> int:
    from .core import registry, router
    tasks = registry.all()
    print(f"{'task':<24} {'ver':<4} {'tiers':<8} route")
    print("-" * 78)
    for name in sorted(tasks):
        t = tasks[name]
        p = router.tiers_for(name)
        from .core import config
        cfg = config.load()
        chain = []
        for tr in range(p["start"], p["max"] + 1):
            spec = cfg.get(f"models.tier{tr}") or {}
            chain.append("python" if tr == 0 else str(spec.get("model", f"tier{tr}")))
        print(f"{name:<24} v{t.version:<3} {p['start']}->{p['max']:<5} "
              f"{' -> '.join(chain)}")
        if args.verbose:
            print(f"{'':26} in : {', '.join(t.input.fields)}")
            print(f"{'':26} out: {', '.join(t.output.fields)}")
            print(f"{'':26} {t.desc}")
    return 0


# ---------------------------------------------------------------------------
def cmd_ingest(args: argparse.Namespace) -> int:
    from .core.store import Store
    from .pipelines import p_qa_ingest

    path = Path(args.file) if args.file else ROOT / "data" / "seed" / "qa_reports.jsonl"
    notes: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                notes.append(json.loads(line))
    if args.limit:
        notes = notes[: args.limit]

    from .core import config
    conc = args.concurrency or int(config.load().get("runtime.concurrency", 1))
    print(f"ingesting {len(notes)} note(s) from {path.name} "
          f"(concurrency={conc})\n")
    st = Store()
    summary = p_qa_ingest.run(notes, store=st, concurrency=conc)
    print()
    for k, v in summary.items():
        print(f"  {k:<16} {v}")
    n = p_qa_ingest.rebuild_metrics(st)
    print(f"  {'metrics_rows':<16} {n}")
    return 0


# ---------------------------------------------------------------------------
def cmd_ask(args: argparse.Namespace) -> int:
    from .pipelines import p_analytics_ask
    question = " ".join(args.question)
    res = p_analytics_ask.ask(question, narrate=not args.no_narrate)
    print()
    print(p_analytics_ask.render(res))
    return 0 if res["grounded"] else 1


# ---------------------------------------------------------------------------
def cmd_inspect(args: argparse.Namespace) -> int:
    from .pipelines import p_defect_inspection
    folder = Path(args.folder)
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
    frames = sorted(str(p) for p in folder.iterdir() if p.suffix.lower() in exts)
    if not frames:
        print(f"no images found in {folder}")
        return 1
    from .core import config
    conc = args.concurrency or int(config.load().get("runtime.concurrency", 1))
    print(f"inspecting {len(frames)} frame(s) from {folder}\n")
    summary = p_defect_inspection.run(frames, camera_id=args.camera,
                                      concurrency=conc)
    print()
    for k, v in summary.items():
        print(f"  {k:<18} {v}")
    return 0


# ---------------------------------------------------------------------------
def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import run_bench
    tiers = [int(t) for t in args.tiers.split(",")]
    return run_bench(tiers, limit=args.limit)


# ---------------------------------------------------------------------------
def cmd_stats(args: argparse.Namespace) -> int:
    from .core.store import Store
    st = Store()
    print("[row counts]")
    for k, v in st.counts().items():
        print(f"  {k:<22} {v}")

    print("\n[defects by label]")
    rows, _ = st.query("SELECT defect_label, COUNT(*) n, ROUND(AVG(severity),2) sev "
                       "FROM defect_observation GROUP BY 1 ORDER BY n DESC")
    for r in rows:
        print(f"  {r['defect_label']:<18} n={r['n']:<4} avg_sev={r['sev']}")

    print("\n[model performance -- from the task_run trace]")
    rows, _ = st.query(
        "SELECT task, tier, COUNT(*) n, SUM(ok) ok, SUM(escalated) esc, "
        "ROUND(AVG(latency_ms)) ms FROM task_run GROUP BY task, tier "
        "ORDER BY task, tier")
    print(f"  {'task':<24} {'tier':<5} {'runs':<6} {'ok':<6} {'esc':<5} {'avg ms'}")
    for r in rows:
        print(f"  {r['task']:<24} {r['tier']:<5} {r['n']:<6} {r['ok']:<6} "
              f"{r['esc']:<5} {r['ms']}")

    print("\n[most recent failures]")
    rows, _ = st.query("SELECT task, tier, errors FROM task_run WHERE ok=0 "
                       "ORDER BY run_id DESC LIMIT 5")
    for r in rows or []:
        print(f"  {r['task']} (t{r['tier']}): {(r['errors'] or '')[:110]}")
    if not rows:
        print("  (none)")
    return 0


# ---------------------------------------------------------------------------
def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m gmt",
        description="Garment AI harness. Start with:  python -m gmt doctor")
    p.add_argument("--offline", action="store_true",
                   help="use the fake model -- no GPU, no network, instant")
    p.add_argument("--debug", action="store_true", help="verbose logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check the environment").set_defaults(fn=cmd_doctor)
    sub.add_parser("init", help="create the database").set_defaults(fn=cmd_init)

    sp = sub.add_parser("tasks", help="list tasks and routing")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(fn=cmd_tasks)

    sp = sub.add_parser("ingest", help="run the QA-note pipeline")
    sp.add_argument("--file", help="a .jsonl file of notes")
    sp.add_argument("--limit", type=int, default=0)
    # default=None -> fall back to runtime.concurrency in config.
    # It used to be a hardcoded 4, which silently overrode the config on
    # every run. MEASURED cost of that on hanalytics: 4 concurrent requests
    # overflow an 11 GB card's KV cache, Ollama spills to system RAM, and
    # the same 24 notes take 12-22 s per call instead of 3-4 s. A default
    # that contradicts the config file is worse than no default.
    sp.add_argument("--concurrency", type=int, default=None)
    sp.set_defaults(fn=cmd_ingest)

    sp = sub.add_parser("ask", help="ask a question of the ground truth")
    sp.add_argument("question", nargs="+")
    sp.add_argument("--no-narrate", action="store_true", help="show rows only")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("inspect", help="run the camera pipeline over a folder")
    sp.add_argument("folder")
    sp.add_argument("--camera", default="cam0")
    sp.add_argument("--concurrency", type=int, default=None)
    sp.set_defaults(fn=cmd_inspect)

    sp = sub.add_parser("bench", help="score tasks against the golden set")
    sp.add_argument("--tiers", default="1,2", help="comma separated, e.g. 0,1,2")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(fn=cmd_bench)

    sub.add_parser("stats", help="what is in the store").set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    if args.offline:
        os.environ["GMT_OFFLINE"] = "1"
    if args.debug:
        os.environ["GMT_LOG_LEVEL"] = "DEBUG"
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
