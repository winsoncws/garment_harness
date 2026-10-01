"""
registry.py -- a name -> Task phone book.
=======================================================================

WHY A REGISTRY INSTEAD OF `import`
----------------------------------
So that a pipeline can say  run("classify_defect", ...)  instead of
importing a class. That one indirection buys three things:

  1. SWAPPABILITY. Point the name at a different implementation and every
     caller changes at once. You asked for reusable/swappable modules --
     this is the mechanism.
  2. CONFIG BY NAME. routing.yaml, the MCP server, the CLI and the trace
     table all refer to tasks by string. They stay in sync automatically.
  3. DISCOVERY. `registry.all()` gives you the full catalogue, so the MCP
     server and `--list` generate themselves.

    gmt/tasks/*.py  --(import)-->  @register  --> _TASKS{name: instance}
                                                       ^
        pipelines / MCP / CLI / bench --(get by name)---+

ADDING A TASK = create the file, decorate the class. Nothing else to edit.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Dict, Iterator, List, Type

from .task import Task

_TASKS: Dict[str, Task] = {}
_LOADED = False


def register(cls: Type[Task]) -> Type[Task]:
    """Class decorator. Instantiates once and files it under cls.name."""
    if not cls.name:
        raise ValueError(f"{cls.__name__} must set a `name`")
    if cls.name in _TASKS and type(_TASKS[cls.name]) is not cls:
        raise ValueError(f"duplicate task name {cls.name!r}")
    _TASKS[cls.name] = cls()
    return cls


def load_all(package: str = "gmt.tasks") -> None:
    """Import every module under gmt/tasks/ so the decorators fire."""
    global _LOADED
    if _LOADED:
        return
    pkg = importlib.import_module(package)
    for mod in pkgutil.iter_modules(pkg.__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{package}.{mod.name}")
    _LOADED = True


def get(name: str) -> Task:
    load_all()
    if name not in _TASKS:
        raise KeyError(f"unknown task {name!r}. Known: {', '.join(sorted(_TASKS))}")
    return _TASKS[name]


def all() -> Dict[str, Task]:                      # noqa: A001 - reads well
    load_all()
    return dict(_TASKS)


def names() -> List[str]:
    return sorted(all())


def __iter__() -> Iterator[Task]:                  # pragma: no cover
    return iter(all().values())
