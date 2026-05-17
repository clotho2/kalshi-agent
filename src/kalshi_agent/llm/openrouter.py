"""OpenRouter chat completion client. Model is configurable per call.

Returns parsed JSON when the prompt requests structured output; falls back to
raw text otherwise. Retries on 429 with the server's `retry-after`, and on
transient 5xx with exponential backoff.
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
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._app_name = app_name
        self._client: httpx.AsyncClient | None = None

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

    async def chat_json(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        max_retries: int = 3,
    ) -> dict:
        """Send a chat request, parse the assistant message as JSON.

        Returns the parsed dict. Raises OpenRouterError on persistent failure
        or unparseable response.
        """
        assert self._client is not None, "use as async context manager"
        body = {
            "model": model or self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        last_err: str = ""
        for attempt in range(max_retries):
            start = time.monotonic()
            try:
                resp = await self._client.post(
                    f"{self._base}/chat/completions",
                    headers=self._headers(),
                    json=body,
                )
            except httpx.HTTPError as e:
                last_err = str(e)
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                wait = float(resp.headers.get("retry-after", 2.0))
                await asyncio.sleep(wait + 0.5)
                continue
            if 500 <= resp.status_code < 600:
                last_err = f"{resp.status_code}: {resp.text[:200]}"
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise OpenRouterError(f"{resp.status_code}: {resp.text[:500]}")
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                # Try to extract a JSON object from the content
                start_idx = content.find("{")
                end_idx = content.rfind("}")
                if start_idx >= 0 and end_idx > start_idx:
                    try:
                        parsed = json.loads(content[start_idx:end_idx + 1])
                    except json.JSONDecodeError:
                        raise OpenRouterError(f"unparseable JSON content: {content[:300]}")
                else:
                    raise OpenRouterError(f"unparseable JSON content: {content[:300]}")
            log.info(
                "llm_call",
                model=model or self._model,
                latency_seconds=round(time.monotonic() - start, 2),
                usage=data.get("usage"),
            )
            return parsed
        raise OpenRouterError(f"exhausted retries: {last_err}")
