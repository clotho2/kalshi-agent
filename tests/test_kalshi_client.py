"""Kalshi client: auth signature, retries, fixed-point price parsing."""

from __future__ import annotations

import base64
from decimal import Decimal

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_agent.kalshi.client import KalshiClient, _api_path, sign_request
from kalshi_agent.kalshi.types import (
    OrderRequest,
    price_decimal_to_str,
    price_str_to_decimal,
)


def test_signature_verifies(rsa_private_key_path) -> None:
    priv = serialization.load_pem_private_key(rsa_private_key_path.read_bytes(), password=None)
    pub = priv.public_key()
    ts = "1700000000000"
    sig_b64 = sign_request(priv, ts, "GET", "/trade-api/v2/portfolio/positions")
    sig = base64.b64decode(sig_b64)
    # Should verify against the same message
    pub.verify(
        sig,
        (ts + "GET" + "/trade-api/v2/portfolio/positions").encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_signing_path_strips_query_string() -> None:
    assert _api_path("/portfolio/orders?status=open") == "/trade-api/v2/portfolio/orders"
    assert _api_path("/trade-api/v2/markets?limit=5") == "/trade-api/v2/markets"


def test_price_round_trip() -> None:
    assert price_str_to_decimal("0.6500") == Decimal("0.6500")
    assert price_decimal_to_str(Decimal("0.65")) == "0.6500"
    assert price_decimal_to_str(Decimal("0.999")) == "0.9990"


@pytest.mark.asyncio
async def test_get_market_parses_decimal_strings(rsa_private_key_path) -> None:
    base = "https://demo-api.kalshi.co/trade-api/v2"
    with respx.mock(base_url=base) as mock:
        mock.get("/markets/FOO").mock(return_value=httpx.Response(200, json={
            "market": {
                "ticker": "FOO",
                "category": "economics",
                "status": "active",
                "yes_ask_dollars": "0.6500",
                "no_ask_dollars": "0.3500",
            }
        }))
        async with KalshiClient(base, "key-id", rsa_private_key_path) as c:
            m = await c.get_market("FOO")
        assert m.ticker == "FOO"
        assert m.yes_ask_dollars == "0.6500"


@pytest.mark.asyncio
async def test_retries_on_5xx(rsa_private_key_path) -> None:
    base = "https://demo-api.kalshi.co/trade-api/v2"
    with respx.mock(base_url=base) as mock:
        route = mock.get("/markets/FOO").mock(side_effect=[
            httpx.Response(503, text="boom"),
            httpx.Response(503, text="boom"),
            httpx.Response(200, json={"market": {"ticker": "FOO"}}),
        ])
        async with KalshiClient(base, "key-id", rsa_private_key_path) as c:
            m = await c.get_market("FOO")
        assert m.ticker == "FOO"
        assert route.call_count == 3


@pytest.mark.asyncio
async def test_rate_limit_429_then_success(rsa_private_key_path) -> None:
    base = "https://demo-api.kalshi.co/trade-api/v2"
    with respx.mock(base_url=base) as mock:
        mock.get("/markets/FOO").mock(side_effect=[
            httpx.Response(429, headers={"retry-after": "0.01"}, text=""),
            httpx.Response(200, json={"market": {"ticker": "FOO"}}),
        ])
        async with KalshiClient(base, "key-id", rsa_private_key_path) as c:
            m = await c.get_market("FOO")
        assert m.ticker == "FOO"


@pytest.mark.asyncio
async def test_list_markets_returns_paginated(rsa_private_key_path) -> None:
    base = "https://demo-api.kalshi.co/trade-api/v2"
    with respx.mock(base_url=base) as mock:
        mock.get("/markets").mock(return_value=httpx.Response(200, json={
            "markets": [
                {"ticker": "A", "category": "economics", "status": "open",
                 "yes_ask_dollars": "0.6500", "volume": 1000},
                {"ticker": "B", "category": "kpi", "status": "open",
                 "yes_ask_dollars": "0.4000", "volume": 500},
            ],
            "cursor": "next-page-token",
        }))
        async with KalshiClient(base, "key-id", rsa_private_key_path) as c:
            markets, cursor = await c.list_markets(status="open", limit=200)
    assert len(markets) == 2
    assert markets[0].ticker == "A"
    assert markets[0].volume == 1000
    assert cursor == "next-page-token"


@pytest.mark.asyncio
async def test_create_order_sends_client_order_id(rsa_private_key_path) -> None:
    base = "https://demo-api.kalshi.co/trade-api/v2"
    captured: dict = {}

    def handler(request):
        nonlocal captured
        import json as _json
        captured = _json.loads(request.content)
        return httpx.Response(200, json={"order": {
            "order_id": "srv-1", "client_order_id": captured["client_order_id"],
            "status": "resting", "ticker": "FOO", "side": "yes", "action": "buy",
        }})

    with respx.mock(base_url=base) as mock:
        mock.post("/portfolio/orders").mock(side_effect=handler)
        async with KalshiClient(base, "key-id", rsa_private_key_path) as c:
            resp = await c.create_order(OrderRequest(
                ticker="FOO", side="yes", action="buy", count_fp="10.00",
                yes_price_dollars="0.5500", client_order_id="abc-123",
            ))
        assert captured["client_order_id"] == "abc-123"
        assert resp.order_id == "srv-1"
