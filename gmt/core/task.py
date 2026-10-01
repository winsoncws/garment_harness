"""
task.py -- the atomic unit of work. THE most important concept here.
=======================================================================

WHAT IS A TASK
--------------
A Task is ONE small job with a declared input shape and a declared output
shape. Not "analyse the factory". Not "handle QA". One job:

    GOOD  classify_defect        text -> one of 8 labels + confidence
    GOOD  extract_defect_fields  text -> {machine, shift, metres, ...}
    GOOD  frame_gate             frame -> is this frame worth the GPU?
    BAD   process_report         text -> "everything"        <-- too big
    BAD   be_helpful             ...                         <-- not a task

WHY SO SMALL
------------
Three reasons, and the third is the one that matters most to you:

  1. A 3B model can nail a 1-decision job and will fumble a 5-decision job.
     Atomicity is what buys you the right to use cheap local models.
  2. Small jobs have small contracts, so validation is meaningful. "Was the
     output correct?" is answerable for `classify_defect`. It is not
     answerable for `process_report`.
  3. You can measure each one separately, so you know exactly WHERE quality
     is lost -- and you can move that ONE task up a tier instead of moving
     the whole pipeline to an expensive model.

WHAT A TASK IS *NOT* RESPONSIBLE FOR
------------------------------------
Deliberately, a Task does not know about: which model runs it, retries,
escalation, concurrency, the database, or logging. Those live in
router.py / runner.py / store.py. A Task is close to pure declaration --
which is why one is ~40 lines and you can write a new one in 10 minutes.

    +-----------+       +---------+       +--------+       +--------+
    |  payload  | ----> |  Task   | ----> | Router | ----> | Runner |
    | (a dict)  |       | declares|       | picks  |       | loops  |
    +-----------+       | shapes  |       | a tier |       | + saves|
                        | + prompt|       | + retry|       +--------+
                        +---------+       +--------+            |
                                                                v
                                                            +-------+
                                                            | Store |
                                                            +-------+

THE FIVE HOOKS YOU MAY OVERRIDE
-------------------------------
| Hook            | When it runs              | Must you?  |
|-----------------|---------------------------|------------|
| build_user()    | to make the prompt        | yes (LLM)  |
| system_prompt() | the persona (from skills/)| no         |
| run_local()     | BEFORE any LLM (tier 0)   | no         |
| stub()          | offline / test mode       | recommended|
| repair()        | before check(), if fixable| no         |
| postprocess()   | after validation passes   | no         |
| check()         | business rules validation | no         |
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .contracts import Contract, Result


# ---------------------------------------------------------------------------
# What comes back from running a task
# ---------------------------------------------------------------------------

@dataclass
class TaskRun:
    """One execution of one task on one payload. Also the row we log."""
    task: str = ""
    ok: bool = False
    value: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    tier: int = -1                 # tier that finally produced the answer
    started_tier: int = -1
    model: str = ""
    provider: str = ""
    attempts: int = 0              # total model calls, including failures
    escalated: bool = False

    latency_ms: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0

    input_hash: str = ""
    raw_text: str = ""             # exactly what the model said, kept for debugging

    def __bool__(self) -> bool:
        return self.ok

    def summary(self) -> str:
        flag = "OK " if self.ok else "FAIL"
        esc = " (escalated)" if self.escalated else ""
        return (f"[{flag}] {self.task} tier={self.tier}{esc} "
                f"{self.latency_ms}ms attempts={self.attempts} "
                f"{'' if self.ok else '| ' + '; '.join(self.errors)}")


# ---------------------------------------------------------------------------
# The base class
# ---------------------------------------------------------------------------

class Task:
    """Subclass this. Set the class attributes. Override the hooks you need."""

    # -- identity -----------------------------------------------------------
    name: str = ""                # unique, snake_case. Used in routing.yaml.
    version: int = 1              # bump on any behaviour change; it is stored
    desc: str = ""                # one line; shown in the MCP tool list

    # -- shapes -------------------------------------------------------------
    input: Contract = Contract("empty", {})
    output: Contract = Contract("empty", {})

    # -- the persona --------------------------------------------------------
    # `skill` names a file in gmt/skills/ (without .md). When it loads, its
    # "## System" + "## Guidelines" become the system prompt. When it does
    # not -- missing file, typo, renamed -- `system` below is used instead,
    # so a prompt-file mistake degrades rather than crashes.
    skill: str = ""
    system: str = "You are a precise data extraction system."

    def system_prompt(self) -> str:
        """The system message actually sent. Skill file wins; `system` backs it up."""
        if self.skill:
            from ..skills._loader import load
            sk = load(self.skill)
            if sk and sk.prompt():
                return sk.prompt()
        return self.system

    def examples(self) -> str:
        """Few-shot block from the skill file, or "" when there is none."""
        if self.skill:
            from ..skills._loader import load
            sk = load(self.skill)
            if sk:
                return sk.examples
        return ""

    # -- knobs --------------------------------------------------------------
    strict_output: bool = False   # True -> unknown keys from the LLM are errors
    use_schema: bool = True       # send JSON Schema to Ollama for constrained decoding

    # =======================================================================
    # HOOKS
    # =======================================================================

    def build_user(self, payload: Dict[str, Any]) -> str:
        """Build the user message. Default: the task description + shape + data.

        Override when you want few-shot examples or a custom layout. Keep the
        `self.output.prompt_block()` line -- it is what teaches the small
        model the exact JSON keys.
        """
        return (
            f"{self.desc}\n\n"
            f"{self.output.prompt_block()}\n\n"
            f"Input:\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n\n"
            f"JSON:"
        )

    def run_local(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Tier 0. Return a dict to answer WITHOUT any LLM, or None to fall through.

        Always try to answer here first. Every call you serve from plain
        Python is one you do not pay for in latency, GPU, or correctness risk.
        A regex that handles 60% of your inputs is worth more than a prompt
        that handles 95% -- because you can compose the two.
        """
        return None

    def stub(self, payload: Dict[str, Any]) -> str:
        """Canned model reply for offline mode. Return a JSON *string*.

        Default returns the contract's example, which is schema-valid but
        semantically meaningless. Override with cheap keyword rules so
        `make demo-offline` produces output you can actually read.
        """
        return self.output.example_json()

    def repair(self, value: Dict[str, Any],
               payload: Dict[str, Any]) -> Dict[str, Any]:
        """Fix what is UNAMBIGUOUSLY fixable, before check() judges it.

        Runs after the type contract passes and before the business rules.
        The bar is high: only repair when there is exactly one thing the
        model could have meant. A reversed graph edge whose reverse is the
        only legal shape qualifies. A wrong enum value does not -- guessing
        which label was intended is not repair, it is invention.

        Rejecting an answer you could have fixed deterministically wastes a
        model call; "fixing" an ambiguous one corrupts your data. Repair the
        first kind only.
        """
        return value

    def postprocess(self, value: Dict[str, Any],
                    payload: Dict[str, Any]) -> Dict[str, Any]:
        """Tidy a validated result. Runs only on success."""
        return value

    def check(self, value: Dict[str, Any],
              payload: Dict[str, Any]) -> List[str]:
        """Business rules the type system cannot express. Return error strings.

        This is where domain knowledge goes, and it is the highest-value code
        in the repo. Example: a defect cannot be "hole" with severity 1, and
        a roll cannot have more metres inspected than it contains. Rules like
        that catch confidently-wrong small-model output that passes the type
        contract perfectly.
        """
        return []

    # =======================================================================
    # PLUMBING -- you rarely touch anything below
    # =======================================================================

    def validate_input(self, payload: Dict[str, Any]) -> Result:
        return self.input.validate(payload)

    def validate_output(self, data: Any, payload: Dict[str, Any]) -> Result:
        res = self.output.validate(data, strict=self.strict_output)
        if res.ok:
            res.value = self.repair(res.value, payload)
            extra = self.check(res.value, payload)
            if extra:
                return Result(False, res.value, extra)
        return res

    def schema(self) -> Optional[Dict[str, Any]]:
        return self.output.json_schema() if self.use_schema else None

    def input_hash(self, payload: Dict[str, Any]) -> str:
        """Stable fingerprint of (task, version, payload) -- used for caching
        and for spotting when the same input silently produced a new answer."""
        blob = json.dumps({"t": self.name, "v": self.version, "p": payload},
                          sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def __repr__(self) -> str:
        return f"<Task {self.name} v{self.version}>"
