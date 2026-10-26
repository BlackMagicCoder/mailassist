"""LLM adapter: Amazon Bedrock Converse API (default model: OpenAI gpt-oss-120b in Frankfurt).

Converse has one request/response shape for every Bedrock model, so changing
MAILASSIST_MODEL needs no code change. Authenticates with a Bedrock API key
(bearer token), so no AWS SDK is needed.

The model is called without tools: it can only return text. Transient failures
(timeouts, connection errors, 429, 5xx) are retried; model calls have no side
effects, so a retry cannot duplicate anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import quote
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .errors import AssistantError

log = logging.getLogger(__name__)


class LLMError(AssistantError):
    pass


_STATUS_HINTS = {
    400: "Modell-ID und Region prüfen",
    401: "API-Schlüssel prüfen",
    403: "API-Schlüssel oder Modellzugang im AWS-Konto prüfen",
    404: "Modell-ID prüfen (gibt es das Modell in dieser Region?)",
    429: "zu viele Anfragen, später erneut versuchen",
}
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 30.0


@dataclass(frozen=True)
class Completion:
    text: str
    model: str


class LLMAdapter(Protocol):
    async def complete(self, system: str, user: str) -> Completion: ...


class BedrockClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        region: str,
        base_url: str | None = None,
        max_tokens: int = 4000,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 2,
        backoff: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._api_key = api_key
        self._model = model
        base = (base_url or f"https://bedrock-runtime.{region}.amazonaws.com").rstrip("/")
        self._url = f"{base}/model/{quote(model, safe='')}/converse"
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._transport = transport
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep

    async def _post(self, payload: dict) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                    response = await client.post(self._url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise LLMError(f"Bedrock nicht erreichbar: {type(exc).__name__}") from None
                delay = self._backoff * 2**attempt
            else:
                if response.status_code not in RETRY_STATUS or attempt >= self._max_retries:
                    return response
                delay = _retry_after(response) or self._backoff * 2**attempt
            attempt += 1
            log.warning("llm retry %d/%d in %.1fs", attempt, self._max_retries, delay)
            await self._sleep(delay)

    async def complete(self, system: str, user: str) -> Completion:
        # The model is part of the URL; no toolConfig is ever sent.
        payload = {
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"maxTokens": self._max_tokens},
        }
        response = await self._post(payload)
        if response.status_code != 200:
            code = response.status_code
            hint = _STATUS_HINTS.get(code, "Störung bei Bedrock" if code >= 500 else "Anfrage abgelehnt")
            raise LLMError(f"Bedrock antwortete mit HTTP {code} ({hint}).", detail=_error_message(response))
        try:
            data = response.json()
            blocks = data["output"]["message"]["content"] or []
            # Reasoning models (gpt-oss) also return reasoningContent blocks; only the answer text counts.
            text = "".join(b.get("text", "") for b in blocks if "text" in b).strip()
        except (ValueError, AttributeError, KeyError, TypeError):
            raise LLMError("Bedrock lieferte eine unlesbare Antwort.") from None
        if data.get("stopReason") == "max_tokens":
            raise LLMError(
                "Die Modellantwort wurde abgeschnitten (Längenlimit erreicht). "
                "MAILASSIST_MAX_TOKENS erhöhen oder die Anweisung kürzen."
            )
        if not text:
            raise LLMError("Bedrock lieferte keinen Text.")
        log.info("llm ok model=%s in_chars=%d out_chars=%d", self._model, len(system) + len(user), len(text))
        return Completion(text=text, model=self._model)


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers.get("retry-after", "")), MAX_RETRY_AFTER)
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
        error = data.get("error") or data.get("message")  # {"error": ...} or Bedrock's {"message": ...}
    except (ValueError, AttributeError):
        return "(keine Details)"
    if isinstance(error, dict):
        return str(error.get("message", ""))[:300]
    return str(error)[:300] if error else "(keine Details)"


# --- structured output ------------------------------------------------------------

M = TypeVar("M", bound=BaseModel)

JSON_REPAIR = (
    "\n\n<format_error>\nYour previous answer was not valid JSON for the required schema. "
    "Problems: {problems}\nAnswer again with ONLY the JSON object, no prose, no code fences.\n</format_error>"
)


def parse_json(text: str, model: type[M]) -> M:
    """Validate the first JSON object in `text` (tolerates code fences and stray prose)."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    return model.model_validate(json.loads(cleaned[start : end + 1]))


def _problems(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        # Locations and error types only; values could be mail content.
        return "; ".join(".".join(map(str, e["loc"])) + f": {e['type']}" for e in exc.errors())[:800]
    return type(exc).__name__ + (f": {exc}" if isinstance(exc, json.JSONDecodeError) else "")


async def complete_json(llm: LLMAdapter, system: str, user: str, model: type[M]) -> tuple[M, str]:
    """Ask for JSON matching `model`; one repair round on invalid output, then LLMError."""
    completion = await llm.complete(system, user)
    try:
        return parse_json(completion.text, model), completion.model
    except (ValueError, ValidationError) as first:
        log.warning("llm json invalid, repairing: %s", _problems(first))
        completion = await llm.complete(system, user + JSON_REPAIR.format(problems=_problems(first)))
    try:
        return parse_json(completion.text, model), completion.model
    except (ValueError, ValidationError) as second:
        raise LLMError("Das Modell lieferte keine gültige strukturierte Antwort.", detail=_problems(second)) from None
