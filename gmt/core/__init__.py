"""Core machinery. Read in this order:

    contracts.py  the shape of data          (start here)
    task.py       the atomic unit of work
    registry.py   name -> task lookup
    llm.py        ollama / anthropic / fake
    router.py     which tier runs it, and escalation
    runner.py     the sync + async for-loops
    store.py      the single ground truth
    config.py     settings + env overrides
    trace.py      console logging
"""
