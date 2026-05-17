"""LLM market assessor strategy: signal generation, filters, throttling."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_agent.kalshi.types import Market
from kalshi_agent.storage.models import Decision
from kalshi_agent.strategies.llm_market_assessor import LLMMarketAssessor


def _market(ticker: str = "FOO", yes_ask: str = "0.50", no_ask: str = "0.50") -> Market:
    return Market(
        ticker=ticker, category="economics", status="active",
        yes_bid_dollars="0.49", yes_ask_dollars=yes_ask,
        no_bid_dollars="0.49", no_ask_dollars=no_ask,
        title="Test market", description="for the test",
    )


@pytest.mark.asyncio
async def test_emits_yes_signal_on_good_edge(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value=_market(yes_ask="0.50"))
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.8, "rationale": "strong edge",
    })
    s = LLMMarketAssessor(
        kalshi_client=kalshi, llm_client=llm, session_maker=db_session_maker,
        tickers=["FOO"], min_edge=Decimal("0.04"), min_confidence=Decimal("0.6"),
        signal_ttl_minutes=5,
    )
    signals = await s.generate_signals()
    assert len(signals) == 1
    assert signals[0].side == "yes"
    assert signals[0].model_probability == 0.65


@pytest.mark.asyncio
async def test_filters_low_confidence(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value=_market())
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.3, "rationale": "weak",
    })
    s = LLMMarketAssessor(
        kalshi_client=kalshi, llm_client=llm, session_maker=db_session_maker,
        tickers=["FOO"], min_edge=Decimal("0.04"), min_confidence=Decimal("0.6"),
        signal_ttl_minutes=5,
    )
    signals = await s.generate_signals()
    assert signals == []


@pytest.mark.asyncio
async def test_filters_insufficient_edge(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value=_market(yes_ask="0.50"))
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.51, "confidence": 0.9, "rationale": "barely",
    })
    s = LLMMarketAssessor(
        kalshi_client=kalshi, llm_client=llm, session_maker=db_session_maker,
        tickers=["FOO"], min_edge=Decimal("0.04"), min_confidence=Decimal("0.6"),
        signal_ttl_minutes=5,
    )
    signals = await s.generate_signals()
    assert signals == []


@pytest.mark.asyncio
async def test_throttles_recent_signals(db_session_maker) -> None:
    with db_session_maker() as sess:
        sess.add(Decision(
            strategy="llm_market_assessor",
            market_ticker="FOO", side="yes",
            model_probability=0.65, confidence=0.8, rationale="recent",
            valid_until=datetime.now(UTC), accepted=True,
            created_at=datetime.now(UTC),
        ))
        sess.commit()
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value=_market())
    llm = MagicMock()
    llm.chat_json = AsyncMock()
    s = LLMMarketAssessor(
        kalshi_client=kalshi, llm_client=llm, session_maker=db_session_maker,
        tickers=["FOO"], min_edge=Decimal("0.04"), min_confidence=Decimal("0.6"),
        signal_ttl_minutes=5, min_seconds_between_signals_per_ticker=1800,
    )
    signals = await s.generate_signals()
    assert signals == []
    llm.chat_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_closed_market(db_session_maker) -> None:
    kalshi = MagicMock()
    closed = _market()
    closed.status = "closed"
    kalshi.get_market = AsyncMock(return_value=closed)
    llm = MagicMock()
    llm.chat_json = AsyncMock()
    s = LLMMarketAssessor(
        kalshi_client=kalshi, llm_client=llm, session_maker=db_session_maker,
        tickers=["FOO"], min_edge=Decimal("0.04"), min_confidence=Decimal("0.6"),
        signal_ttl_minutes=5,
    )
    signals = await s.generate_signals()
    assert signals == []
    llm.chat_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_emits_no_signal_when_no_side_has_edge(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value=_market(yes_ask="0.50", no_ask="0.50"))
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.20, "confidence": 0.8, "rationale": "NO side mispriced",
    })
    s = LLMMarketAssessor(
        kalshi_client=kalshi, llm_client=llm, session_maker=db_session_maker,
        tickers=["FOO"], min_edge=Decimal("0.04"), min_confidence=Decimal("0.6"),
        signal_ttl_minutes=5,
    )
    signals = await s.generate_signals()
    assert len(signals) == 1
    assert signals[0].side == "no"  # NO at 0.50 with true P(NO)=0.80 → edge 0.30
