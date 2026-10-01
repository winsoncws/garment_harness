---
name: harness-check
description: Health check the garment harness — environment, tests, offline demo, and model performance from the trace. Use when the user asks "is everything working", after pulling changes, before a demo, or when something behaves oddly.
---

# Harness health check

Run these in order and report the output. Stop at the first failure and
diagnose it rather than continuing.

## 1. Environment

```bash
python3 -m gmt doctor
```

Checks python, optional packages, the database, Ollama reachability, and
whether the models named in `config/models.yaml` actually exist. It prints
the exact fix command for anything missing.

## 2. Tests — must pass, must be fast

```bash
python3 tests/run_tests.py
```

44 tests, offline, under two seconds. If this takes longer than a few
seconds something is reaching the network that should not be.

Two failures are especially informative:

- `test_every_task_has_a_routing_entry` → someone added a task and forgot
  `config/routing.yaml`
- `test_every_stub_satisfies_its_own_contract` → a stub is broken, so
  offline mode is silently lying

## 3. Offline demo — proves the whole path

```bash
make demo
```

Expect roughly: 24 notes → ~19 observations, ~5 clean, ~84 KG edges,
0 failed chains. Large deviations mean a task or a projection changed
behaviour.

## 4. Real models, if Ollama is up

```bash
python3 scripts/ollama_smoke.py
```

Checks connectivity, plain chat, **schema-constrained output**, cold vs warm
latency, every task against a real example, and accuracy per tier.

## 5. Read the trace — the part that is actually informative

```bash
python3 -m gmt stats
```

| Signal | Meaning | Action |
|--------|---------|--------|
| escalation rate > 50% on a task | the start tier is wrong | pin it higher in `routing.yaml` |
| escalation rate < 5% | the small model is coping | consider pushing it down a tier |
| tier 3 being reached at all | genuinely hard, or a contract bug | read `task_run.raw_text` |
| `needs_review` growing | either low confidence or a vocabulary gap | sample 10 and look |
| latency climbing over time | Ollama unloaded the weights | raise `keep_alive` |

## Report

A short table of what passed, what failed, and the one most likely cause of
each failure. Do not list everything that is fine.
