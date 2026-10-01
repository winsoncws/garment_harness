"""
llm.py -- one small interface, three backends.
=======================================================================

    +-------------------------------------------------------------+
    |                        LLMClient                            |
    |   .chat(system, user, schema=None, options=None) -> Reply   |
    +-------------------------------------------------------------+
        |                     |                        |
        v                     v                        v
    OllamaClient        AnthropicClient           FakeClient
    (tier 1 & 2)          (tier 3)             (tests / no GPU)
    your GPU box        api.anthropic.com      canned, instant

Nothing else in the repo imports `urllib` or knows a vendor's API shape.
Swap in vLLM, llama.cpp, OpenAI, or a colleague's endpoint by writing one
more class with a `.chat()` method and adding it to `_build()`.

Deliberately stdlib-only (urllib). No `requests`, no `ollama` package, no
`anthropic` package -- so this file runs on a bare Python 3.9.

STRUCTURED OUTPUT
-----------------
Ollama >= 0.5 accepts a JSON Schema in `format`, which constrains decoding
so the model *cannot* emit invalid JSON. We pass the contract's schema
whenever we have one. On older Ollama we fall back to `format: "json"`,
and if even that is unsupported the contract validation still catches it.
That is defence in depth:

    schema-constrained decoding  ->  robust JSON extraction  ->  contract
         (best case, no bugs)          (handles chatty models)     (final say)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from . import config


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------

@dataclass
class Reply:
    """One model response, plus everything we want to log about it."""
    text: str = ""
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

class OllamaClient:
    """Talks to /api/chat on your GPU server."""

    provider = "ollama"

    def __init__(self, model: str, host: Optional[str] = None,
                 options: Optional[Dict[str, Any]] = None,
                 timeout_s: Optional[int] = None,
                 keep_alive: Optional[str] = None):
        cfg = config.load()
        self.model = model
        self.host = (host or cfg.get("ollama.host", "http://localhost:11434")).rstrip("/")
        self.options = dict(options or {})
        self.timeout_s = timeout_s or int(cfg.get("ollama.timeout_s", 120))
        self.keep_alive = keep_alive or cfg.get("ollama.keep_alive", "30m")

    def chat(self, system: str, user: str,
             schema: Optional[Dict[str, Any]] = None,
             options: Optional[Dict[str, Any]] = None,
             stub: Optional[Callable[[], str]] = None) -> Reply:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {**self.options, **(options or {})},
        }
        if schema:
            body["format"] = schema          # constrained decoding

        t0 = time.time()
        try:
            data = _post_json(f"{self.host}/api/chat", body, self.timeout_s)
        except LLMError as e:
            # Older Ollama rejects a schema object in `format`. Retry with the
            # loose "json" mode before giving up -- costs one round trip and
            # saves you from having to upgrade the server today.
            if schema and "format" in str(e).lower():
                body["format"] = "json"
                try:
                    data = _post_json(f"{self.host}/api/chat", body, self.timeout_s)
                except LLMError as e2:
                    return Reply(model=self.model, provider=self.provider,
                                 error=str(e2), latency_ms=_ms(t0))
            else:
                return Reply(model=self.model, provider=self.provider,
                             error=str(e), latency_ms=_ms(t0))

        return Reply(
            text=(data.get("message") or {}).get("content", ""),
            model=self.model,
            provider=self.provider,
            latency_ms=_ms(t0),
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            raw=data,
        )

    # -- housekeeping helpers used by scripts/doctor.py ---------------------
    def list_models(self) -> list:
        try:
            data = _get_json(f"{self.host}/api/tags", self.timeout_s)
        except LLMError:
            return []
        return [m.get("name", "") for m in data.get("models", [])]

    def warm(self) -> bool:
        """Load the weights onto the GPU now, so the first real call is fast."""
        try:
            _post_json(f"{self.host}/api/chat",
                       {"model": self.model, "messages": [], "keep_alive": self.keep_alive},
                       self.timeout_s)
            return True
        except LLMError:
            return False


# ---------------------------------------------------------------------------
# Anthropic (tier 3)
# ---------------------------------------------------------------------------

class AnthropicClient:
    """The escape hatch. Reached only when a smaller model could not do it."""

    provider = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: str, options: Optional[Dict[str, Any]] = None):
        cfg = config.load()
        self.model = model
        self.options = dict(options or {})
        self.max_tokens = int(cfg.get("anthropic.max_tokens", 4096))
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    def chat(self, system: str, user: str,
             schema: Optional[Dict[str, Any]] = None,
             options: Optional[Dict[str, Any]] = None,
             stub: Optional[Callable[[], str]] = None) -> Reply:
        if not self.api_key:
            return Reply(model=self.model, provider=self.provider,
                         error="ANTHROPIC_API_KEY is not set")
        if schema:
            user = user + "\n\nRespond with JSON matching this schema:\n" + \
                   json.dumps(schema, indent=2)

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        merged = {**self.options, **(options or {})}
        if "temperature" in merged:
            body["temperature"] = merged["temperature"]

        t0 = time.time()
        try:
            data = _post_json(self.URL, body, 120, headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            })
        except LLMError as e:
            return Reply(model=self.model, provider=self.provider,
                         error=str(e), latency_ms=_ms(t0))

        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        usage = data.get("usage", {})
        return Reply(text=text, model=self.model, provider=self.provider,
                     latency_ms=_ms(t0),
                     prompt_tokens=int(usage.get("input_tokens") or 0),
                     output_tokens=int(usage.get("output_tokens") or 0),
                     raw=data)


# ---------------------------------------------------------------------------
# Fake (offline)
# ---------------------------------------------------------------------------

class FakeClient:
    """Returns whatever `stub` produces. No network, no GPU, instant.

    This is not a toy: it lets the ENTIRE pipeline -- prompt building,
    JSON extraction, contract validation, routing, storage, tracing -- run
    in CI and on your laptop. When something breaks you find out here, in
    5 milliseconds, instead of after a 40-minute GPU batch.
    """

    provider = "fake"

    def __init__(self, model: str = "fake", options: Optional[Dict[str, Any]] = None):
        self.model = model
        self.options = dict(options or {})

    def chat(self, system: str, user: str,
             schema: Optional[Dict[str, Any]] = None,
             options: Optional[Dict[str, Any]] = None,
             stub: Optional[Callable[[], str]] = None) -> Reply:
        text = stub() if stub else "{}"
        return Reply(text=text, model=self.model, provider=self.provider,
                     latency_ms=1, prompt_tokens=len(user) // 4,
                     output_tokens=len(text) // 4)


# ---------------------------------------------------------------------------
# Factory + async helper
# ---------------------------------------------------------------------------

_CLIENTS: Dict[int, Any] = {}


def get_client(tier: int, *, fresh: bool = False):
    """tier -> a client, built from config/models.yaml. Cached per tier."""
    cfg = config.load()
    if cfg.get("runtime.offline"):
        return FakeClient()
    if tier in _CLIENTS and not fresh:
        return _CLIENTS[tier]

    spec = cfg.get(f"models.tier{tier}")
    if not spec:
        raise LLMError(f"no model configured for tier {tier} (see config/models.yaml)")
    client = _build(spec)
    _CLIENTS[tier] = client
    return client


def _build(spec: Dict[str, Any]):
    provider = spec.get("provider", "ollama")
    model = spec.get("model", "")
    options = spec.get("options") or {}
    if provider == "ollama":
        # A tier may name its OWN host. This is how you pin a model to a
        # specific GPU: Ollama has no per-model GPU setting, but it does
        # honour CUDA_VISIBLE_DEVICES per server process. Run one Ollama
        # per card on different ports and point each tier at the right one.
        # See config/models.yaml for the recipe.
        return OllamaClient(model, host=spec.get("host"), options=options)
    if provider == "anthropic":
        return AnthropicClient(model, options=options)
    if provider == "fake":
        return FakeClient(model, options=options)
    raise LLMError(f"unknown provider {provider!r}")


async def achat(client, *args, **kwargs) -> Reply:
    """Run a blocking .chat() on a worker thread so asyncio can overlap calls.

    An HTTP request spends ~all its time waiting, so threads give real
    concurrency here without pulling in aiohttp. Concurrency is capped by a
    semaphore in runner.py -- do not let it run unbounded or you will simply
    queue requests inside Ollama instead of inside Python.
    """
    return await asyncio.to_thread(client.chat, *args, **kwargs)


# ---------------------------------------------------------------------------
# tiny HTTP helpers
# ---------------------------------------------------------------------------

def _post_json(url: str, body: Dict[str, Any], timeout: int,
               headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise LLMError(f"HTTP {e.code} from {url}: {detail}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"cannot reach {url}: {e.reason}") from None
    except TimeoutError:
        raise LLMError(f"timeout after {timeout}s calling {url}") from None


def _get_json(url: str, timeout: int) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:                                   # noqa: BLE001
        raise LLMError(f"cannot reach {url}: {e}") from None


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)
