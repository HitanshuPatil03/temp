"""The local model client — the one module that talks to a model runtime.

Everything generative in MRIP goes through here, and **nothing on the figure
path may import it**: ``tests/test_architecture.py`` bans the name ``mrip.llm``
from every file under the figure path (ARCHITECTURE §7). Swapping the model is a
change confined to this file, "because the figure path has no model client to
swap" (§13.3).

The transport is the standard library on purpose: no runtime HTTP dependency to
provision on a locked-down host, and the only endpoint is the operator-configured
local Ollama (`127.0.0.1:11434` by default). There is no setting for a hosted
provider — MRIP performs no inference off the deployment host (§3).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from mrip.config import Settings

__all__ = ["LLMClient", "LLMUnavailableError", "client_from_settings"]


class LLMUnavailableError(RuntimeError):
    """The model runtime is disabled or unreachable.

    Raised rather than returned so a caller cannot mistake "no model" for "the
    model said nothing". The query service catches it and falls back to the
    deterministic, cited-passage answer — never to silence.
    """


@dataclass(frozen=True, slots=True)
class LLMClient:
    base_url: str
    model: str
    timeout: float
    thinking: bool
    enabled: bool

    @property
    def available(self) -> bool:
        return self.enabled

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        """One non-streaming completion, or :class:`LLMUnavailableError`."""
        if not self.enabled:
            raise LLMUnavailableError(
                "the local model runtime is disabled (MRIP_LLM_ENABLED=false)"
            )
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": self.thinking,
        }
        if system is not None:
            payload["system"] = system
        # Localhost, operator-configured endpoint — not a user-supplied URL.
        request = urllib.request.Request(  # noqa: S310
            f"{self.base_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise LLMUnavailableError(str(exc)) from exc
        text = body.get("response") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise LLMUnavailableError("model returned an unexpected response shape")
        return text.strip()


def client_from_settings(settings: Settings) -> LLMClient:
    return LLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        thinking=settings.llm_thinking,
        enabled=settings.llm_enabled,
    )
