"""Gemini access through the google-genai SDK in Vertex AI mode (ADC = the Cloud Run service account).

Adapted from DANA's GeminiClient (structured JSON + token usage) and the dataplex/v22 token-bucket limiter with
exponential backoff + jitter. Adds Pydantic ``response_schema`` so responses are validated, not regex-parsed.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..settings import Settings

log = logging.getLogger("dahub.gemini")
T = TypeVar("T", bound=BaseModel)

_RETRYABLE_MARKERS = ("429", "quota", "rate", "503", "500", "unavailable", "deadline", "internal", "resource exhausted")


class GeminiError(RuntimeError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"prompt": self.prompt_tokens, "cached": self.cached_tokens, "output": self.output_tokens}


class RateLimiter:
    """Thread-safe token bucket (requests per minute)."""

    def __init__(self, rpm: int):
        self.capacity = float(max(1, rpm))
        self.tokens = self.capacity
        self.refill_per_sec = self.capacity / 60.0
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.refill_per_sec)
                self._last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.refill_per_sec
            time.sleep(min(wait, 1.0))


def _is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


class GeminiService:
    """The only place the app talks to an LLM. Tests inject a fake exposing the same two methods."""

    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self._client = client
        self._limiter = RateLimiter(settings.gemini_rpm)

    @property
    def available(self) -> bool:
        return bool(self.settings.gemini_enabled and (self._client is not None or self.settings.gcp_project))

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True, project=self.settings.gcp_project, location=self.settings.vertex_location
            )
        return self._client

    def _call(self, model: str, contents: str, config: Any) -> Any:
        last: Exception | None = None
        for attempt in range(self.settings.gemini_max_retries + 1):
            self._limiter.acquire()
            try:
                return self._get_client().models.generate_content(model=model, contents=contents, config=config)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt >= self.settings.gemini_max_retries or not _is_retryable(exc):
                    break
                delay = min(30.0, 2.0 * (2**attempt)) * (0.5 + random.random() * 0.5)
                log.warning("Gemini call failed (%s); retry %d in %.1fs", exc, attempt + 1, delay)
                time.sleep(delay)
        raise GeminiError(f"Gemini call failed (model={model}): {last}") from last

    def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        *,
        system_instruction: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> tuple[T, Usage]:
        from google.genai import types

        model = model or self.settings.gemini_model
        config = types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
            system_instruction=system_instruction,
        )
        started = time.perf_counter()
        response = self._call(model, prompt, config)
        usage = _usage(response)
        log.info(
            "gemini structured call",
            extra={"fields": {"model": model, "schema": schema.__name__, "latency_ms": int((time.perf_counter() - started) * 1000), **usage.as_dict()}},
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed, usage
        text = getattr(response, "text", None) or ""
        try:
            return schema.model_validate(json.loads(_strip_fences(text))), usage
        except (json.JSONDecodeError, ValidationError) as exc:
            raise GeminiError(f"Gemini returned an unexpected response: {exc}. Raw: {text[:300]}") from exc

    def generate_text(self, prompt: str, *, model: str | None = None, temperature: float = 0.2,
                      system_instruction: str | None = None) -> tuple[str, Usage]:
        from google.genai import types

        model = model or self.settings.gemini_fast_model
        config = types.GenerateContentConfig(temperature=temperature, system_instruction=system_instruction)
        response = self._call(model, prompt, config)
        text = getattr(response, "text", None)
        if not text:
            raise GeminiError("Gemini returned an empty response (possibly blocked)")
        return text.strip(), _usage(response)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


def _usage(response: Any) -> Usage:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return Usage()
    return Usage(
        prompt_tokens=getattr(meta, "prompt_token_count", 0) or 0,
        cached_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
        output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
    )
