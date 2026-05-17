"""OpenRouter client: JSON parsing, retries, error handling."""

from __future__ import annotations

import httpx
import pytest
import respx

from kalshi_agent.llm.openrouter import OpenRouterClient, OpenRouterError


def _ok_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "test", "model": "test",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"total_tokens": 100},
    })


@pytest.mark.asyncio
async def test_parses_json_content() -> None:
    base = "https://openrouter.ai/api/v1"
    with respx.mock(base_url=base) as mock:
        mock.post("/chat/completions").mock(return_value=_ok_response(
            '{"probability": 0.7, "confidence": 0.8, "rationale": "test"}'
        ))
        async with OpenRouterClient("sk-test", "model-x", base_url=base) as c:
            res = await c.chat_json("sys", "user")
    assert res == {"probability": 0.7, "confidence": 0.8, "rationale": "test"}


@pytest.mark.asyncio
async def test_extracts_json_from_prose() -> None:
    base = "https://openrouter.ai/api/v1"
    with respx.mock(base_url=base) as mock:
        mock.post("/chat/completions").mock(return_value=_ok_response(
            'Here is the JSON: {"probability": 0.5, "confidence": 0.1, "rationale": "x"} that\'s it'
        ))
        async with OpenRouterClient("sk-test", "model-x", base_url=base) as c:
            res = await c.chat_json("sys", "user")
    assert res["probability"] == 0.5


@pytest.mark.asyncio
async def test_retries_on_5xx() -> None:
    base = "https://openrouter.ai/api/v1"
    with respx.mock(base_url=base) as mock:
        mock.post("/chat/completions").mock(side_effect=[
            httpx.Response(503, text="busy"),
            _ok_response('{"probability": 0.6, "confidence": 0.5, "rationale": "r"}'),
        ])
        async with OpenRouterClient("sk-test", "model-x", base_url=base) as c:
            res = await c.chat_json("sys", "user")
    assert res["probability"] == 0.6


@pytest.mark.asyncio
async def test_429_with_retry_after() -> None:
    base = "https://openrouter.ai/api/v1"
    with respx.mock(base_url=base) as mock:
        mock.post("/chat/completions").mock(side_effect=[
            httpx.Response(429, headers={"retry-after": "0.01"}, text=""),
            _ok_response('{"probability": 0.6, "confidence": 0.5, "rationale": "r"}'),
        ])
        async with OpenRouterClient("sk-test", "model-x", base_url=base) as c:
            res = await c.chat_json("sys", "user")
    assert res["probability"] == 0.6


@pytest.mark.asyncio
async def test_unparseable_raises() -> None:
    base = "https://openrouter.ai/api/v1"
    with respx.mock(base_url=base) as mock:
        mock.post("/chat/completions").mock(return_value=_ok_response("not json"))
        async with OpenRouterClient("sk-test", "model-x", base_url=base) as c:
            with pytest.raises(OpenRouterError):
                await c.chat_json("sys", "user")


@pytest.mark.asyncio
async def test_4xx_raises_immediately() -> None:
    base = "https://openrouter.ai/api/v1"
    with respx.mock(base_url=base) as mock:
        mock.post("/chat/completions").mock(return_value=httpx.Response(401, text="bad key"))
        async with OpenRouterClient("sk-test", "model-x", base_url=base) as c:
            with pytest.raises(OpenRouterError):
                await c.chat_json("sys", "user")
