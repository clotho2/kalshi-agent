"""LLM market assessor strategy: discovery, filters, throttling, signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_agent.kalshi.types import Market
from kalshi_agent.storage.models import Decision
from kalshi_agent.strategies.llm_market_assessor import LLMMarketAssessor


def _market(
    ticker: str = "FOO",
    yes_ask: str = "0.50",
    no_ask: str = "0.50",
    category: str = "economics",
    status: str = "active",
    volume: int = 500,
    close_in_hours: float = 24.0,
) -> Market:
    return Market(
        ticker=ticker, category=category, status=status,
        yes_bid_dollars="0.49", yes_ask_dollars=yes_ask,
        no_bid_dollars="0.49", no_ask_dollars=no_ask,
        title=f"Test {ticker}", description="for the test",
        volume=volume,
        close_time=datetime.now(UTC) + timedelta(hours=close_in_hours),
    )


def _make_strategy(db_session_maker, kalshi, llm, *, tickers=None, categories=None, **kw):
    defaults = dict(
        tickers=tickers if tickers is not None else [],
        categories=categories if categories is not None else ["economics", "kpi"],
        max_markets_per_tick=20,
        min_volume_contracts=100,
        min_hours_to_close=2.0,
        discovery_max_pages=5,
        min_edge=Decimal("0.04"),
        min_confidence=Decimal("0.6"),
        signal_ttl_minutes=5,
        min_seconds_between_signals_per_ticker=1800,
    )
    defaults.update(kw)
    return LLMMarketAssessor(
        kalshi_client=kalshi, llm_client=llm, session_maker=db_session_maker,
        **defaults,
    )


# ---------- manual mode (explicit tickers) -------------------------------


@pytest.mark.asyncio
async def test_manual_mode_emits_yes_signal(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value=_market(yes_ask="0.50"))
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.8, "rationale": "strong edge",
    })
    s = _make_strategy(db_session_maker, kalshi, llm, tickers=["FOO"])
    signals = await s.generate_signals()
    assert len(signals) == 1
    assert signals[0].side == "yes"


@pytest.mark.asyncio
async def test_manual_mode_filters_low_confidence(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value=_market())
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.3, "rationale": "weak",
    })
    s = _make_strategy(db_session_maker, kalshi, llm, tickers=["FOO"])
    assert await s.generate_signals() == []


@pytest.mark.asyncio
async def test_manual_mode_filters_insufficient_edge(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value=_market(yes_ask="0.50"))
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.51, "confidence": 0.9, "rationale": "barely",
    })
    s = _make_strategy(db_session_maker, kalshi, llm, tickers=["FOO"])
    assert await s.generate_signals() == []


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
    s = _make_strategy(db_session_maker, kalshi, llm, tickers=["FOO"])
    assert await s.generate_signals() == []
    llm.chat_json.assert_not_awaited()
    kalshi.get_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_emits_no_signal_when_no_side_has_edge(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.get_market = AsyncMock(return_value=_market())
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.20, "confidence": 0.8, "rationale": "NO side mispriced",
    })
    s = _make_strategy(db_session_maker, kalshi, llm, tickers=["FOO"])
    signals = await s.generate_signals()
    assert len(signals) == 1
    assert signals[0].side == "no"


# ---------- discovery mode -----------------------------------------------


@pytest.mark.asyncio
async def test_discovery_filters_by_category(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.list_markets = AsyncMock(return_value=(
        [
            _market(ticker="ECON1", category="economics"),
            _market(ticker="SPORT1", category="sports"),
            _market(ticker="KPI1", category="kpi"),
        ],
        None,
    ))
    kalshi.get_market = AsyncMock()
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.8, "rationale": "edge",
    })
    s = _make_strategy(db_session_maker, kalshi, llm,
                       tickers=[], categories=["economics", "kpi"])
    signals = await s.generate_signals()
    tickers_signalled = {sig.market_ticker for sig in signals}
    assert tickers_signalled == {"ECON1", "KPI1"}
    kalshi.get_market.assert_not_awaited()  # discovery doesn't call get_market per ticker


@pytest.mark.asyncio
async def test_discovery_filters_low_volume(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.list_markets = AsyncMock(return_value=(
        [
            _market(ticker="LIQUID", volume=1000),
            _market(ticker="ILLIQUID", volume=10),
        ],
        None,
    ))
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.8, "rationale": "edge",
    })
    s = _make_strategy(db_session_maker, kalshi, llm,
                       tickers=[], min_volume_contracts=100)
    signals = await s.generate_signals()
    assert {sig.market_ticker for sig in signals} == {"LIQUID"}


@pytest.mark.asyncio
async def test_discovery_filters_imminent_close(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.list_markets = AsyncMock(return_value=(
        [
            _market(ticker="FAR", close_in_hours=24.0),
            _market(ticker="NEAR", close_in_hours=0.5),  # under 2h cutoff
        ],
        None,
    ))
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.8, "rationale": "edge",
    })
    s = _make_strategy(db_session_maker, kalshi, llm, tickers=[])
    signals = await s.generate_signals()
    assert {sig.market_ticker for sig in signals} == {"FAR"}


@pytest.mark.asyncio
async def test_discovery_caps_at_max_per_tick(db_session_maker) -> None:
    many = [_market(ticker=f"T{i}") for i in range(50)]
    kalshi = MagicMock()
    kalshi.list_markets = AsyncMock(return_value=(many, None))
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.8, "rationale": "edge",
    })
    s = _make_strategy(db_session_maker, kalshi, llm,
                       tickers=[], max_markets_per_tick=5)
    signals = await s.generate_signals()
    assert len(signals) == 5
    assert llm.chat_json.await_count == 5


@pytest.mark.asyncio
async def test_discovery_paginates(db_session_maker) -> None:
    page1 = [_market(ticker=f"P1-{i}", category="sports") for i in range(20)]  # all filtered out
    page2 = [_market(ticker="P2-HIT", category="economics")]
    kalshi = MagicMock()
    kalshi.list_markets = AsyncMock(side_effect=[
        (page1, "cursor-1"),
        (page2, None),
    ])
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.8, "rationale": "edge",
    })
    s = _make_strategy(db_session_maker, kalshi, llm,
                       tickers=[], categories=["economics"])
    signals = await s.generate_signals()
    assert {sig.market_ticker for sig in signals} == {"P2-HIT"}
    assert kalshi.list_markets.await_count == 2


@pytest.mark.asyncio
async def test_discovery_respects_throttle(db_session_maker) -> None:
    with db_session_maker() as sess:
        sess.add(Decision(
            strategy="llm_market_assessor",
            market_ticker="RECENT", side="yes",
            model_probability=0.65, confidence=0.8, rationale="x",
            valid_until=datetime.now(UTC), accepted=True,
            created_at=datetime.now(UTC),
        ))
        sess.commit()
    kalshi = MagicMock()
    kalshi.list_markets = AsyncMock(return_value=(
        [_market(ticker="RECENT"), _market(ticker="FRESH")], None,
    ))
    llm = MagicMock()
    llm.chat_json = AsyncMock(return_value={
        "probability": 0.65, "confidence": 0.8, "rationale": "edge",
    })
    s = _make_strategy(db_session_maker, kalshi, llm, tickers=[])
    signals = await s.generate_signals()
    assert {sig.market_ticker for sig in signals} == {"FRESH"}


@pytest.mark.asyncio
async def test_discovery_empty_returns_no_signals(db_session_maker) -> None:
    kalshi = MagicMock()
    kalshi.list_markets = AsyncMock(return_value=([], None))
    llm = MagicMock()
    llm.chat_json = AsyncMock()
    s = _make_strategy(db_session_maker, kalshi, llm, tickers=[])
    assert await s.generate_signals() == []
    llm.chat_json.assert_not_awaited()
