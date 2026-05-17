"""Fill ingestion: polling, idempotency, fill catch-up."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_agent.fills import FillIngestor
from kalshi_agent.kalshi.types import KalshiFill
from kalshi_agent.storage.models import Fill, Position


@pytest.mark.asyncio
async def test_catch_up_applies_fills(config, db_session_maker) -> None:
    mock_client = MagicMock()
    fill = KalshiFill(
        order_id="o1", trade_id="t1", ticker="FOO", side="yes",
        action="buy", count=10, yes_price_dollars="0.5000",
        is_taker=True, created_time=datetime.now(UTC),
    )
    mock_client.list_fills = AsyncMock(return_value=([fill], None))
    ingestor = FillIngestor(config, mock_client, db_session_maker, discord=None)
    applied = await ingestor.catch_up()
    assert applied == 1
    with db_session_maker() as s:
        pos = s.get(Position, "FOO")
        fills = s.query(Fill).all()
    assert pos is not None
    assert pos.count == 10
    assert len(fills) == 1


@pytest.mark.asyncio
async def test_catch_up_is_idempotent(config, db_session_maker) -> None:
    mock_client = MagicMock()
    fill = KalshiFill(
        order_id="o1", trade_id="t1", ticker="FOO", side="yes",
        action="buy", count=10, yes_price_dollars="0.5000",
        is_taker=True, created_time=datetime.now(UTC),
    )
    mock_client.list_fills = AsyncMock(return_value=([fill], None))
    ingestor = FillIngestor(config, mock_client, db_session_maker, discord=None)
    await ingestor.catch_up()
    # Reset high_water so it re-pulls
    ingestor._high_water_ts = 0
    applied2 = await ingestor.catch_up()
    assert applied2 == 0  # duplicate fill skipped
    with db_session_maker() as s:
        fills = s.query(Fill).all()
    assert len(fills) == 1


@pytest.mark.asyncio
async def test_catch_up_handles_empty(config, db_session_maker) -> None:
    mock_client = MagicMock()
    mock_client.list_fills = AsyncMock(return_value=([], None))
    ingestor = FillIngestor(config, mock_client, db_session_maker, discord=None)
    applied = await ingestor.catch_up()
    assert applied == 0
