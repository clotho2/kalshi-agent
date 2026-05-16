"""Reconciliation: startup empty, mismatch detection, fill catch-up."""

from __future__ import annotations

import httpx
import pytest
import respx

from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.safety.reconciliation import Reconciler
from kalshi_agent.storage.models import Position


@pytest.mark.asyncio
async def test_reconcile_empty_db_with_remote_positions(
    rsa_private_key_path, db_session_maker
) -> None:
    base = "https://demo-api.kalshi.co/trade-api/v2"
    with respx.mock(base_url=base) as mock:
        mock.get("/portfolio/positions").mock(return_value=httpx.Response(200, json={
            "market_positions": [
                {"ticker": "FOO", "position": 10},
                {"ticker": "BAR", "position": -5},
            ]
        }))
        async with KalshiClient(base, "key-id", rsa_private_key_path) as c:
            r = Reconciler(c, db_session_maker, discord=None)
            result = await r.reconcile(source="startup")

    assert result["ok"]
    assert len(result["discrepancies"]) == 2
    with db_session_maker() as s:
        foo = s.get(Position, "FOO")
        bar = s.get(Position, "BAR")
        assert foo is not None and foo.count == 10 and foo.side == "yes"
        assert bar is not None and bar.count == 5 and bar.side == "no"


@pytest.mark.asyncio
async def test_reconcile_no_discrepancies(rsa_private_key_path, db_session_maker) -> None:
    with db_session_maker() as s:
        s.add(Position(market_ticker="FOO", side="yes", count=10, avg_price_dollars="0.5000"))
        s.commit()

    base = "https://demo-api.kalshi.co/trade-api/v2"
    with respx.mock(base_url=base) as mock:
        mock.get("/portfolio/positions").mock(return_value=httpx.Response(200, json={
            "market_positions": [{"ticker": "FOO", "position": 10}]
        }))
        async with KalshiClient(base, "key-id", rsa_private_key_path) as c:
            r = Reconciler(c, db_session_maker, discord=None)
            result = await r.reconcile()
    assert result["ok"]
    assert result["discrepancies"] == []


@pytest.mark.asyncio
async def test_reconcile_local_position_closed_externally(
    rsa_private_key_path, db_session_maker
) -> None:
    with db_session_maker() as s:
        s.add(Position(market_ticker="GHOST", side="yes", count=7, avg_price_dollars="0.50"))
        s.commit()

    base = "https://demo-api.kalshi.co/trade-api/v2"
    with respx.mock(base_url=base) as mock:
        mock.get("/portfolio/positions").mock(return_value=httpx.Response(200, json={
            "market_positions": []
        }))
        async with KalshiClient(base, "key-id", rsa_private_key_path) as c:
            r = Reconciler(c, db_session_maker, discord=None)
            result = await r.reconcile()

    assert any(d["kind"] == "missing_remote" for d in result["discrepancies"])
    with db_session_maker() as s:
        assert s.get(Position, "GHOST").count == 0


@pytest.mark.asyncio
async def test_reconcile_handles_remote_failure(rsa_private_key_path, db_session_maker) -> None:
    base = "https://demo-api.kalshi.co/trade-api/v2"
    with respx.mock(base_url=base) as mock:
        mock.get("/portfolio/positions").mock(
            return_value=httpx.Response(500, text="oops")
        )
        async with KalshiClient(base, "key-id", rsa_private_key_path) as c:
            r = Reconciler(c, db_session_maker, discord=None)
            result = await r.reconcile()
    assert not result["ok"]
