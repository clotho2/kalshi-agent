"""OpenRouter chat completion client. Model is configurable per call.

Returns parsed JSON when the prompt requests structured output; falls back to
raw text otherwise.

Resilience:
  * 429 (rate limit) has its OWN retry budget, separate from transport/5xx
    errors, so a burst of rate-limit responses can't silently exhaust the
    error budget. Waits honor `retry-after` / `x-ratelimit-reset`, falling
    back to capped exponential backoff.
  * Transient transport errors and 5xx use exponential backoff.
  * Empty completions (0 content tokens) are retried within the error budget
    and logged with `finish_reason`/`usage` so they're diagnosable.
  * An optional minimum interval between requests spaces out back-to-back
    calls (a tick may assess many markets in a row).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from kalshi_agent.journal.logger import get_logger

log = get_logger(__name__)

_DEFAULT_BASE = "https://openrouter.ai/api/v1"
_MAX_BACKOFF_SECONDS = 60.0


class OpenRouterError(Exception):
    pass


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = _DEFAULT_BASE,
        timeout: float = 60.0,
        app_name: str = "kalshi-agent",
        min_request_interval: float = 0.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._app_name = app_name
        self._min_interval = max(0.0, min_request_interval)
        self._client: httpx.AsyncClient | None = None
        self._throttle_lock = asyncio.Lock()
        self._last_request_ts = 0.0

    async def __aenter__(self) -> OpenRouterClient:
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/clotho2/kalshi-agent",
            "X-Title": self._app_name,
        }

    async def _throttle(self) -> None:
        """Enforce a minimum gap between consecutive requests."""
        if self._min_interval <= 0:
            return
        async with self._throttle_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = time.monotonic()

    def _rate_limit_wait(self, resp: httpx.Response, attempt: int) -> float:
        """Seconds to wait after a 429, honoring server hints when present."""
        retry_after = resp.headers.get("retry-after")
        if retry_after is not None:
            try:
                return min(float(retry_after) + 0.5, _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        # OpenRouter exposes the reset point as a unix-epoch milliseconds value.
        reset = resp.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                secs = float(reset) / 1000.0 - time.time()
                if secs > 0:
                    return min(secs + 0.5, _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(2.0 * (2 ** (attempt - 1)), _MAX_BACKOFF_SECONDS)

    async def chat_json(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        max_retries: int = 3,
        max_rate_limit_retries: int = 6,
    ) -> dict:
        """Send a chat request, parse the assistant message as JSON.

        Returns the parsed dict. Raises OpenRouterError on persistent failure
        or unparseable response. Transport/5xx errors and empty completions
        share `max_retries`; 429 responses use the separate
        `max_rate_limit_retries` budget.
        """
        assert self._client is not None, "use as async context manager"
        used_model = model or self._model
        body = {
            "model": used_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        last_err = "no attempts made"
        error_attempts = 0
        rate_limit_attempts = 0
        while True:
            await self._throttle()
            start = time.monotonic()
            try:
                resp = await self._client.post(
                    f"{self._base}/chat/completions",
                    headers=self._headers(),
                    json=body,
                )
            except httpx.HTTPError as e:
                error_attempts += 1
                last_err = f"transport error: {e}"
                if error_attempts >= max_retries:
                    break
                await asyncio.sleep(2 ** (error_attempts - 1))
                continue

            if resp.status_code == 429:
                rate_limit_attempts += 1
                wait = self._rate_limit_wait(resp, rate_limit_attempts)
                last_err = f"429 rate limited (attempt {rate_limit_attempts})"
                log.warning(
                    "openrouter_rate_limited",
                    model=used_model,
                    attempt=rate_limit_attempts,
                    wait_seconds=round(wait, 2),
                )
                if rate_limit_attempts >= max_rate_limit_retries:
                    break
                await asyncio.sleep(wait)
                continue

            if 500 <= resp.status_code < 600:
                error_attempts += 1
                last_err = f"{resp.status_code}: {resp.text[:200]}"
                if error_attempts >= max_retries:
                    break
                await asyncio.sleep(2 ** (error_attempts - 1))
                continue

            if resp.status_code >= 400:
                raise OpenRouterError(f"{resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            choices = data.get("choices") or []
            usage = data.get("usage")
            finish_reason = choices[0].get("finish_reason") if choices else None
            content = ""
            if choices:
                content = (choices[0].get("message") or {}).get("content") or ""

            if not content.strip():
                # A 200 with no usable content (the failure mode seen with some
                # slow/free providers). Retry within the error budget.
                error_attempts += 1
                last_err = (
                    f"empty completion (finish_reason={finish_reason}, usage={usage})"
                )
                log.warning(
                    "openrouter_empty_completion",
                    model=used_model,
                    finish_reason=finish_reason,
                    usage=usage,
                )
                if error_attempts >= max_retries:
                    break
                await asyncio.sleep(2 ** (error_attempts - 1))
                continue

            parsed = _parse_json(content)
            log.info(
                "llm_call",
                model=used_model,
                latency_seconds=round(time.monotonic() - start, 2),
                finish_reason=finish_reason,
                usage=usage,
            )
            return parsed

        raise OpenRouterError(f"exhausted retries: {last_err}")


def _parse_json(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # Try to extract a JSON object embedded in prose.
    start_idx = content.find("{")
    end_idx = content.rfind("}")
    if start_idx >= 0 and end_idx > start_idx:
        try:
            return json.loads(content[start_idx:end_idx + 1])
        except json.JSONDecodeError:
            pass
    raise OpenRouterError(f"unparseable JSON content: {content[:300]}")
