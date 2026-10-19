"""Claude Sonnet via NanoGPT's Anthropic-compatible Messages endpoint.

The model is called without tools: it can only return text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    model: str


class NanoGPTClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        max_tokens: int = 2000,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api_key = api_key
        self._model = model
        self._url = f"{base_url.rstrip('/')}/messages"
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._transport = transport

    async def complete(self, system: str, user: str) -> Completion:
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "anthropic-version": "2023-06-01"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"NanoGPT nicht erreichbar: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise LLMError(f"NanoGPT antwortete mit HTTP {response.status_code}: {_error_message(response)}")
        data = response.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if not text:
            raise LLMError("NanoGPT lieferte keinen Text")
        model = data.get("model") or self._model
        log.info("llm ok model=%s in_chars=%d out_chars=%d", model, len(system) + len(user), len(text))
        return Completion(text=text, model=model)


def _error_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except ValueError:
        return "(keine Details)"
    if isinstance(error, dict):
        return str(error.get("message", ""))[:300]
    return str(error)[:300] if error else "(keine Details)"
