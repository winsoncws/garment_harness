"""
server.py -- an MCP server that exposes every registered task as a tool.
=======================================================================

WHAT MCP IS, IN ONE PARAGRAPH
-----------------------------
Model Context Protocol is a tiny JSON-RPC 2.0 conversation over stdin/stdout.
A client (Claude Code, Claude Desktop, your own agent) launches this script
as a subprocess, asks "what tools do you have?", and then calls them. That
is the entire protocol. There is no magic and no SDK needed -- this file
implements it in ~200 lines of stdlib, partly so you can read it and see
that there is nothing to be afraid of.

    Claude Code  --(spawn)-->  python -m gmt.mcp.server
         |                              |
         |  {"method":"tools/list"}     |
         |----------------------------->|
         |  {"tools":[{...},{...}]}     |  <- generated from registry.all()
         |<-----------------------------|
         |  {"method":"tools/call",     |
         |   "params":{"name":          |
         |     "classify_defect", ...}} |
         |----------------------------->|
         |                              |--> router.run(...) --> Ollama/store
         |  {"content":[{"text":...}]}  |
         |<-----------------------------|

WHY THIS MATTERS FOR YOUR PROJECT
----------------------------------
Every task you write becomes, for free and with no extra code:
  * a python function             registry.get("x")
  * a CLI command                 python -m gmt ...
  * a pipeline step               Step(task="x")
  * AND an MCP tool               callable by any agent, including me

So when you ask a big model to help debug a defect trend, it can call your
real tasks against your real database instead of guessing. The registry is
what makes this automatic -- add a task file, restart the server, the tool
appears.

FRAMING
-------
The stdio transport is newline-delimited JSON: exactly one JSON object per
line, both directions. stdout carries protocol ONLY -- every log line must
go to stderr or you corrupt the stream. That is the single most common way
to break an MCP server, so `print()` is deliberately never used below.

TO REGISTER IT WITH CLAUDE CODE
-------------------------------
    claude mcp add garment -- python3 -m gmt.mcp.server

...run from this repo's root, or use an absolute path plus a `cwd`. See
docs/06_mcp_and_agents.md.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "garment-harness", "version": "0.1.0"}


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _send(msg: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(*parts: Any) -> None:
    """stderr only. Never stdout -- see FRAMING above."""
    print(*parts, file=sys.stderr, flush=True)


def _result(mid: Any, result: Dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": mid, "result": result})


def _error(mid: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


# ---------------------------------------------------------------------------
# tool definitions, generated from the task registry
# ---------------------------------------------------------------------------

def _tool_defs() -> List[Dict[str, Any]]:
    from ..core import registry, router

    tools: List[Dict[str, Any]] = []
    for name, task in sorted(registry.all().items()):
        p = router.tiers_for(name)
        tools.append({
            "name": name,
            "description": (f"{task.desc}\n"
                            f"(runs at tier {p['start']}-{p['max']}; "
                            f"output is contract-validated before it is returned)"),
            "inputSchema": task.input.json_schema(),
        })

    # Two hand-written tools that are not tasks: they read the warehouse.
    tools.append({
        "name": "ask_analytics",
        "description": ("Answer a business question about defects/production by "
                        "generating read-only SQL, running it, and returning both "
                        "the rows and a grounded one-line answer."),
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string",
                                        "description": "the question in plain English"}},
            "required": ["question"],
        },
    })
    tools.append({
        "name": "run_sql",
        "description": ("Run a read-only SELECT against the ground-truth database. "
                        "Writes are refused by the engine."),
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string"},
                           "limit": {"type": "integer", "default": 200}},
            "required": ["sql"],
        },
    })
    return tools


# ---------------------------------------------------------------------------
# tool execution
# ---------------------------------------------------------------------------

def _call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    from ..core import registry, router
    from ..core.store import get_store

    if name == "run_sql":
        rows, err = get_store().query(arguments.get("sql", ""),
                                      limit=int(arguments.get("limit", 200)))
        if err:
            return _text(f"SQL rejected: {err}", is_error=True)
        return _text(json.dumps({"row_count": len(rows), "rows": rows},
                                indent=2, default=str))

    if name == "ask_analytics":
        from ..pipelines import p_analytics_ask
        res = p_analytics_ask.ask(arguments.get("question", ""))
        return _text(json.dumps(res, indent=2, default=str))

    if name in registry.all():
        run = router.run(name, arguments)
        payload = {
            "ok": run.ok,
            "value": run.value,
            "errors": run.errors,
            "tier": run.tier,
            "model": run.model,
            "escalated": run.escalated,
            "latency_ms": run.latency_ms,
        }
        return _text(json.dumps(payload, indent=2, default=str), is_error=not run.ok)

    return _text(f"unknown tool: {name}", is_error=True)


def _text(s: str, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


# ---------------------------------------------------------------------------
# resources -- read-only context the client can pull in
# ---------------------------------------------------------------------------

_RESOURCES = [
    {"uri": "garment://schema",
     "name": "Database schema",
     "description": "CREATE TABLE text for the ground-truth database",
     "mimeType": "text/plain"},
    {"uri": "garment://vocab",
     "name": "Closed vocabularies",
     "description": "Allowed defect labels, node types and relations",
     "mimeType": "application/json"},
    {"uri": "garment://stats",
     "name": "Row counts",
     "description": "How much data is currently in each table",
     "mimeType": "application/json"},
]


def _read_resource(uri: str) -> Dict[str, Any]:
    from ..core.store import get_store
    from ..tasks import _vocab

    if uri == "garment://schema":
        body, mime = get_store().schema_text(), "text/plain"
    elif uri == "garment://vocab":
        body = json.dumps({"defect_labels": _vocab.DEFECT_LABELS,
                           "node_types": _vocab.NODE_TYPES,
                           "relations": _vocab.REL_TYPES,
                           "shifts": _vocab.SHIFTS,
                           "fabric_types": _vocab.FABRIC_TYPES}, indent=2)
        mime = "application/json"
    elif uri == "garment://stats":
        body = json.dumps(get_store().counts(), indent=2)
        mime = "application/json"
    else:
        raise KeyError(uri)
    return {"contents": [{"uri": uri, "mimeType": mime, "text": body}]}


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

def handle(msg: Dict[str, Any]) -> None:
    method = msg.get("method", "")
    mid = msg.get("id")
    params = msg.get("params") or {}

    # notifications carry no id and get no reply
    if mid is None and method.startswith("notifications/"):
        return

    if method == "initialize":
        _result(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": SERVER_INFO,
        })
    elif method == "ping":
        _result(mid, {})
    elif method == "tools/list":
        _result(mid, {"tools": _tool_defs()})
    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            _result(mid, _call_tool(name, args))
        except Exception as e:                                 # noqa: BLE001
            _log("tool error:", traceback.format_exc())
            _result(mid, _text(f"{type(e).__name__}: {e}", is_error=True))
    elif method == "resources/list":
        _result(mid, {"resources": _RESOURCES})
    elif method == "resources/read":
        try:
            _result(mid, _read_resource(params.get("uri", "")))
        except KeyError:
            _error(mid, -32602, f"unknown resource: {params.get('uri')}")
    elif method in ("prompts/list",):
        _result(mid, {"prompts": []})
    elif mid is not None:
        _error(mid, -32601, f"method not found: {method}")


def main() -> int:
    _log(f"garment MCP server ready (protocol {PROTOCOL_VERSION})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _log("bad JSON on stdin:", e)
            continue
        try:
            handle(msg)
        except Exception:                                      # noqa: BLE001
            _log("handler crashed:", traceback.format_exc())
            if msg.get("id") is not None:
                _error(msg["id"], -32603, "internal error")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
