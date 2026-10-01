"""
runner.py -- the for-loops. Sync and async, plus task chaining.
=======================================================================

SYNC vs ASYNC, drawn to scale
-----------------------------
Six documents, each taking ~2s on the GPU.

  map_sync (concurrency 1)
    doc1 [====]
    doc2       [====]
    doc3             [====]
    doc4                   [====]
    doc5                         [====]
    doc6                               [====]      total ~12s

  map_async (concurrency 3)
    doc1 [====]        doc4 [====]
    doc2 [====]        doc5 [====]
    doc3 [====]        doc6 [====]                 total ~4s

WHY NOT concurrency=50
----------------------
Because it is SLOWER. Ollama serves a limited number of parallel sequences
(OLLAMA_NUM_PARALLEL, default 1-4 depending on version and VRAM). Requests
beyond that just queue *inside* Ollama, where you cannot see them, and every
queued request still holds a socket and a timeout. You get the same
throughput plus mysterious timeouts.

    | concurrency | what happens                                      |
    |-------------|---------------------------------------------------|
    | 1           | safe, slow, easiest to debug. Start here.         |
    | 2-4         | the sweet spot for one Ollama on one GPU.         |
    | 8+          | only if OLLAMA_NUM_PARALLEL is raised to match    |
    |             | AND you have the VRAM for the extra KV caches.    |

WHICH LOOP DO I USE
-------------------
    | Situation                              | Use            |
    |----------------------------------------|----------------|
    | learning / debugging / <20 items       | map_sync       |
    | nightly batch of 10k reports           | map_parallel   |
    | inside an async web service            | map_async      |
    | steps that depend on each other        | chain          |

`map_parallel` is the sync-looking wrapper around the async one -- use it
from ordinary scripts so you never have to think about event loops.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from . import config, router, trace
from .store import Store, get_store
from .task import Task, TaskRun

TaskLike = Union[str, Task]
Payloads = Sequence[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Batch result
# ---------------------------------------------------------------------------

@dataclass
class Batch:
    """The outcome of running one task over many payloads."""
    task: str = ""
    runs: List[TaskRun] = field(default_factory=list)
    wall_ms: int = 0

    @property
    def ok(self) -> List[TaskRun]:
        return [r for r in self.runs if r.ok]

    @property
    def failed(self) -> List[TaskRun]:
        return [r for r in self.runs if not r.ok]

    @property
    def values(self) -> List[Dict[str, Any]]:
        return [r.value for r in self.runs if r.ok]

    def report(self) -> str:
        n = len(self.runs)
        if not n:
            return f"{self.task}: nothing to do"
        good = len(self.ok)
        esc = sum(1 for r in self.runs if r.escalated)
        tiers: Dict[int, int] = {}
        for r in self.runs:
            tiers[r.tier] = tiers.get(r.tier, 0) + 1
        tier_s = " ".join(f"t{k}={v}" for k, v in sorted(tiers.items()))
        toks = sum(r.output_tokens for r in self.runs)
        return (f"{self.task}: {good}/{n} ok  ({100 * good // n}%)  "
                f"escalated={esc}  {tier_s}  "
                f"wall={self.wall_ms}ms  out_tokens={toks}")

    def failures_table(self, limit: int = 10) -> str:
        rows = ["  " + r.summary() for r in self.failed[:limit]]
        if not rows:
            return "  (no failures)"
        more = len(self.failed) - len(rows)
        return "\n".join(rows) + (f"\n  ... and {more} more" if more > 0 else "")


# ---------------------------------------------------------------------------
# 1. the simple loop
# ---------------------------------------------------------------------------

def map_sync(task: TaskLike, payloads: Payloads, *,
             store: Optional[Store] = None,
             doc_ids: Optional[Sequence[Optional[str]]] = None,
             on_result: Optional[Callable[[int, TaskRun], None]] = None,
             stop_on_error: bool = False,
             **run_kw: Any) -> Batch:
    """One at a time. Boring, predictable, the right default while learning."""
    t0 = time.time()
    name = task if isinstance(task, str) else task.name
    batch = Batch(task=name)
    st = store if store is not None else get_store()

    for i, payload in enumerate(payloads):
        doc_id = doc_ids[i] if doc_ids else None
        r = router.run(task, payload, store=st, doc_id=doc_id, **run_kw)
        batch.runs.append(r)
        if on_result:
            on_result(i, r)
        if stop_on_error and not r.ok:
            trace.warn("stopping early", task=name, at=i)
            break

    batch.wall_ms = int((time.time() - t0) * 1000)
    return batch


# ---------------------------------------------------------------------------
# 2. the concurrent loop
# ---------------------------------------------------------------------------

async def map_async(task: TaskLike, payloads: Payloads, *,
                    concurrency: Optional[int] = None,
                    store: Optional[Store] = None,
                    doc_ids: Optional[Sequence[Optional[str]]] = None,
                    on_result: Optional[Callable[[int, TaskRun], None]] = None,
                    **run_kw: Any) -> Batch:
    """N in flight at once, capped by a semaphore.

    router.run() is synchronous, so each item runs on a worker thread via
    asyncio.to_thread. That is fine and correct here: the work is an HTTP
    request that spends ~all its time waiting on the GPU, and threads
    release the GIL while waiting on a socket.
    """
    t0 = time.time()
    name = task if isinstance(task, str) else task.name
    cfg = config.load()
    limit = concurrency or int(cfg.get("runtime.concurrency", 4))
    sem = asyncio.Semaphore(limit)
    st = store if store is not None else get_store()

    results: List[Optional[TaskRun]] = [None] * len(payloads)

    async def one(i: int, payload: Dict[str, Any]) -> None:
        async with sem:                       # <- the whole concurrency story
            doc_id = doc_ids[i] if doc_ids else None
            r = await asyncio.to_thread(
                router.run, task, payload, store=st, doc_id=doc_id, **run_kw)
            results[i] = r
            if on_result:
                on_result(i, r)

    await asyncio.gather(*(one(i, p) for i, p in enumerate(payloads)))

    batch = Batch(task=name, runs=[r for r in results if r is not None])
    batch.wall_ms = int((time.time() - t0) * 1000)
    return batch


def map_parallel(task: TaskLike, payloads: Payloads, **kw: Any) -> Batch:
    """Sync wrapper around map_async. Use this from normal scripts."""
    return asyncio.run(map_async(task, payloads, **kw))


# ---------------------------------------------------------------------------
# 3. chaining -- when step 2 needs step 1's answer
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """One link in a chain.

    task  -- the task name
    prep  -- (accumulated_state) -> payload for this task.
             Default: pass the whole accumulated state through.
    key   -- where to file this step's output in the state dict.
             Default: the task name.
    when  -- (state) -> bool. Skip the step when it returns False.
             This is how you branch: cheap gate first, expensive step only
             when the gate says yes.
    """
    task: str
    prep: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    key: Optional[str] = None
    when: Optional[Callable[[Dict[str, Any]], bool]] = None


@dataclass
class ChainResult:
    ok: bool = True
    state: Dict[str, Any] = field(default_factory=dict)
    runs: List[TaskRun] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    stopped_at: str = ""

    def report(self) -> str:
        bits = [f"{'OK' if self.ok else 'STOPPED at ' + self.stopped_at}"]
        bits += [f"{r.task}(t{r.tier}{'' if r.ok else ' FAIL'})" for r in self.runs]
        if self.skipped:
            bits.append("skipped:" + ",".join(self.skipped))
        return " -> ".join(bits)


def chain(steps: Sequence[Step], payload: Dict[str, Any], *,
          store: Optional[Store] = None,
          doc_id: Optional[str] = None,
          stop_on_error: bool = True,
          **run_kw: Any) -> ChainResult:
    """Run tasks in order, threading a shared `state` dict through them.

        state = {**payload}
        for step in steps:
            if step.when(state) is False: skip
            out = run(step.task, step.prep(state))
            state[step.key] = out

    Everything a later step needs must be in `state`, which keeps each task
    independently testable -- you can always reconstruct a task's exact
    input from the state dict without replaying the chain.
    """
    st = store if store is not None else get_store()
    res = ChainResult(state=dict(payload))

    for step in steps:
        if step.when is not None and not step.when(res.state):
            res.skipped.append(step.task)
            trace.debug("skipped", task=step.task)
            continue

        sub = step.prep(res.state) if step.prep else dict(res.state)
        r = router.run(step.task, sub, store=st, doc_id=doc_id, **run_kw)
        res.runs.append(r)

        if not r.ok:
            res.ok = False
            res.stopped_at = step.task
            if stop_on_error:
                return res
            continue

        res.state[step.key or step.task] = r.value

    return res


async def chain_async(steps: Sequence[Step], payload: Dict[str, Any],
                      **kw: Any) -> ChainResult:
    """Chain on a worker thread, so many chains can overlap."""
    return await asyncio.to_thread(chain, steps, payload, **kw)


async def map_chain_async(steps: Sequence[Step], payloads: Payloads, *,
                          concurrency: Optional[int] = None,
                          **kw: Any) -> List[ChainResult]:
    """The workhorse for nightly batches: N whole pipelines in flight at once."""
    limit = concurrency or int(config.load().get("runtime.concurrency", 4))
    sem = asyncio.Semaphore(limit)

    async def one(p: Dict[str, Any]) -> ChainResult:
        async with sem:
            return await chain_async(steps, p, **kw)

    return list(await asyncio.gather(*(one(p) for p in payloads)))


def map_chain(steps: Sequence[Step], payloads: Payloads, **kw: Any) -> List[ChainResult]:
    return asyncio.run(map_chain_async(steps, payloads, **kw))


# ---------------------------------------------------------------------------
# a tiny progress printer you can pass as on_result
# ---------------------------------------------------------------------------

def progress(total: int, every: int = 1) -> Callable[[int, TaskRun], None]:
    state = {"done": 0, "ok": 0}

    def cb(_i: int, r: TaskRun) -> None:
        state["done"] += 1
        state["ok"] += 1 if r.ok else 0
        if state["done"] % every == 0 or state["done"] == total:
            pct = 100 * state["done"] // max(total, 1)
            trace.info(f"[{pct:3d}%] {state['done']}/{total}  ok={state['ok']}",
                       task=r.task)
    return cb
