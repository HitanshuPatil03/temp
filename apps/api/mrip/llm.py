"""The local model client — the one module that talks to a model runtime.

Everything generative in MRIP goes through here, and **nothing on the figure
path may import it**: ``tests/test_architecture.py`` bans the name ``mrip.llm``
from every file under the figure path (ARCHITECTURE 7). Swapping the model is a
change confined to this file, "because the figure path has no model client to
swap" (13.3).

Transport is stdlib urllib (no extra dep). Two modes:

* generate — blocking, for tests and POST /api/query.
* generate_stream — yields tokens as Ollama emits them (stream:true),
  for the prototype POST /api/query/stream SSE endpoint.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field

from mrip.config import Settings

__all__ = ["LLMClient", "LLMUnavailableError", "client_from_settings"]


class LLMUnavailableError(RuntimeError):
    """Raised so the caller can fall back to cited passages, never silence."""


@dataclass(frozen=True, slots=True)
class LLMClient:
    base_url: str
    model: str
    timeout: float
    thinking: bool
    enabled: bool
    keep_alive: str = "5m"
    num_predict: int = 512
    num_ctx: int = 2048
    temperature: float = 0.2
    _cache: dict[str, str] = field(
        default_factory=dict, compare=False, hash=False, repr=False
    )
    _cache_order: list[str] = field(
        default_factory=list, compare=False, hash=False, repr=False
    )
    _CACHE_CAP: int = field(default=64, compare=False, hash=False, repr=False)

    @property
    def available(self) -> bool:
        return self.enabled

    def _payload(
        self, prompt: str, system: str | None, *, stream: bool
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "stream": stream,
            "think": self.thinking,
            "keep_alive": self.keep_alive,
            "options": {
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
                "temperature": self.temperature,
            },
        }
        if system is not None:
            payload["system"] = system
        return payload

    def _request(self, payload: dict[str, object]) -> urllib.request.Request:
        return urllib.request.Request(  # noqa: S310
            f"{self.base_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def _cache_key(self, prompt: str, system: str | None) -> str:
        return f"{system or ''}\x1f{prompt}"

    def _cache_put(self, key: str, value: str) -> None:
        if key in self._cache:
            self._cache_order.remove(key)
        elif len(self._cache_order) >= self._CACHE_CAP:
            oldest = self._cache_order.pop(0)
            self._cache.pop(oldest, None)
        self._cache[key] = value
        self._cache_order.append(key)

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        if not self.enabled:
            raise LLMUnavailableError(
                "local model runtime is disabled (MRIP_LLM_ENABLED=false)"
            )
        key = self._cache_key(prompt, system)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        payload = self._payload(prompt, system, stream=False)
        request = self._request(payload)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise LLMUnavailableError(str(exc)) from exc
        text = body.get("response") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise LLMUnavailableError("model returned an unexpected response shape")
        text = text.strip()
        self._cache_put(key, text)
        return text

    def generate_stream(self, prompt: str, *, system: str | None = None) -> Iterator[str]:
        """Yield tokens as Ollama emits them."""
        if not self.enabled:
            raise LLMUnavailableError(
                "local model runtime is disabled (MRIP_LLM_ENABLED=false)"
            )
        key = self._cache_key(prompt, system)
        hit = self._cache.get(key)
        if hit is not None:
            yield hit
            return
        payload = self._payload(prompt, system, stream=True)
        request = self._request(payload)
        chunks: list[str] = []
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                for raw_line in response:
                    if not raw_line or not raw_line.strip():
                        continue
                    try:
                        obj = json.loads(raw_line.decode("utf-8"))
                    except ValueError:
                        continue
                    if obj.get("error"):
                        raise LLMUnavailableError(str(obj["error"]))
                    token = obj.get("response")
                    if isinstance(token, str) and token:
                        chunks.append(token)
                        yield token
                    if obj.get("done"):
                        break
        except LLMUnavailableError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMUnavailableError(str(exc)) from exc
        full = "".join(chunks).strip()
        if full:
            self._cache_put(key, full)

    def warm(self) -> None:
        """Best-effort warm: load model with 1-token generation. Silent on failure."""
        if not self.enabled:
            return
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": "ok",
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"num_predict": 1, "num_ctx": 64, "temperature": 0},
        }
        req = urllib.request.Request(  # noqa: S310
            f"{self.base_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                resp.read()
        except Exception:  # noqa: S110
            pass


def client_from_settings(settings: Settings) -> LLMClient:
    return LLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        thinking=settings.llm_thinking,
        enabled=settings.llm_enabled,
        keep_alive=settings.llm_keep_alive,
        num_predict=settings.llm_num_predict,
        num_ctx=settings.llm_num_ctx,
        temperature=settings.llm_temperature,
    )
