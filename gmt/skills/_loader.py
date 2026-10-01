"""
_loader.py -- load a role/prompt pack from a markdown file.
=======================================================================

WHY PROMPTS LIVE IN .md FILES AND NOT IN PYTHON
------------------------------------------------
  * you will edit prompts 50x more often than you edit code
  * `git diff` on a prompt is readable; on a python triple-quoted string
    it is a wall of escaped quotes
  * the QA manager who actually knows what "slub" means can edit a .md
    file. They will not edit t_classify_defect.py.
  * the same role is reused by several tasks -- write it once

TRADE-OFF, STATED HONESTLY
--------------------------
Indirection costs you "where is this text coming from?". Two things keep
that manageable: a task names its skill explicitly (`skill = "inspector"`),
and if the file is missing the task silently falls back to its own
`system` string, so a typo degrades instead of crashing.

FILE FORMAT
-----------
    ---
    name: inspector
    tier_hint: 1
    tags: [quality, vision]
    ---

    ## System
    ...becomes the system prompt...

    ## Guidelines
    ...appended to the system prompt...

    ## Examples
    ...available as skill.examples, for few-shot prompting...

Only `## System` is required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_DIR = Path(__file__).resolve().parent


@dataclass
class Skill:
    name: str
    system: str = ""
    guidelines: str = ""
    examples: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def prompt(self) -> str:
        """System + guidelines, which is what a task actually sends."""
        parts = [self.system.strip()]
        if self.guidelines.strip():
            parts.append(self.guidelines.strip())
        return "\n\n".join(p for p in parts if p)


_CACHE: Dict[str, Skill] = {}


def load(name: str, *, reload: bool = False) -> Optional[Skill]:
    """Read gmt/skills/<name>.md. Returns None when it does not exist."""
    if name in _CACHE and not reload:
        return _CACHE[name]
    path = SKILL_DIR / f"{name}.md"
    if not path.exists():
        return None

    text = path.read_text(encoding="utf-8")
    meta: Dict[str, Any] = {}

    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if m:
        from ..core.config import _mini_yaml
        meta = _mini_yaml(m.group(1)) or {}
        text = text[m.end():]

    sections = _split_sections(text)
    skill = Skill(
        name=name,
        system=sections.get("system", ""),
        guidelines=sections.get("guidelines", ""),
        examples=sections.get("examples", ""),
        meta=meta,
    )
    _CACHE[name] = skill
    return skill


def _split_sections(text: str) -> Dict[str, str]:
    """Split on `## Heading` into {lowercased heading: body}."""
    out: Dict[str, str] = {}
    current = ""
    buf: List[str] = []
    for line in text.splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h:
            if current:
                out[current] = "\n".join(buf).strip()
            current = h.group(1).strip().lower()
            buf = []
        else:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf).strip()
    return out


def names() -> List[str]:
    return sorted(p.stem for p in SKILL_DIR.glob("*.md")
                  if not p.stem.startswith("_") and p.stem != "README")
