from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from kalshi_agent.config import Config
from kalshi_agent.kalshi.types import KalshiFill
from kalshi_agent.positions import apply_fill
from kalshi_agent.safety.pnl import realized_pnl_since
from kalshi_agent.safety.risk_monitor import RiskMonitor
from kalshi_agent.storage.models import Fill, PnlDaily, Position


def test_live_mode_refuses_placeholder_strategy_without_explicit_override(config) -> None:
    raw = config.model_dump()
    raw["mode"] = "live"
    raw["strategy"]["active"] = "placeholder"
    raw["strategy"]["allow_placeholder_live"] = False

    with pytest.raises(ValidationError, match="Refusing live mode with placeholder strategy"):
        Config.model_validate(raw)


def test_risk_allows_opposite_side_buy_to_close_when_exposure_cap_is_full(
    config, kill_switch, db_session_maker
) -> None:
    config.risk.max_total_exposure_usd = 1.0
    with db_session_maker() as s:
        s.add(Position(
            market_ticker="FOO",
            side="yes",
            count=10,
            avg_price_dollars="0.6000",
            realized_pnl_dollars=0.0,
        ))
        s.commit()

    risk = RiskMonitor(config, kill_switch, db_session_maker)
    decision = risk.check_trade(
        market_ticker="FOO",
        side="no",
        model_probability=Decimal("0.50"),
        current_price=Decimal("0.3500"),
        confidence=Decimal("0.1"),
        bankroll_dollars=Decimal("1000"),
        market_category="economics",
    )

    assert decision.allowed is True
    assert decision.sized_contracts == 10


def test_opposite_side_close_is_capped_to_existing_position_count(
    config, kill_switch, db_session_maker
) -> None:
    with db_session_maker() as s:
        s.add(Position(
            market_ticker="FOO",
            side="yes",
            count=7,
            avg_price_dollars="0.6000",
            realized_pnl_dollars=0.0,
        ))
        s.commit()

    risk = RiskMonitor(config, kill_switch, db_session_maker)
    decision = risk.check_trade(
        market_ticker="FOO",
        side="no",
        model_probability=Decimal("0.99"),
        current_price=Decimal("0.0100"),
        confidence=Decimal("1"),
        bankroll_dollars=Decimal("100000"),
        market_category="economics",
    )

    assert decision.allowed is True
    assert decision.sized_contracts == 7


def test_maker_fee_is_not_discounted_twice(db_session_maker) -> None:
    fill = KalshiFill(
        trade_id="trade-1",
        order_id="order-1",
        ticker="FOO",
        side="yes",
        action="buy",
        count=1,
        yes_price_dollars="0.5000",
        is_taker=False,
        created_time=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
    )

    with db_session_maker() as s:
        realized = apply_fill(s, fill)
        stored_position = s.query(Position).filter_by(market_ticker="FOO").one()
        stored_fill = s.query(Fill).one()

    assert Decimal(stored_fill.fee_dollars) == Decimal("0.0050")
    assert realized == Decimal("-0.0050")
    assert stored_position.realized_pnl_dollars == -0.005


def test_realized_pnl_since_uses_snapshot_baseline(db_session_maker) -> None:
    tz = ZoneInfo("America/New_York")
    cutoff = datetime(2026, 5, 19, 0, 0, tzinfo=tz).astimezone(UTC)
    with db_session_maker() as s:
        s.add(PnlDaily(day="2026-05-18", realized_pnl=12.5, fees=0.0, trade_count=0))
        s.add(Position(
            market_ticker="FOO",
            side="yes",
            count=0,
            avg_price_dollars="0.0000",
            realized_pnl_dollars=15.25,
        ))
        s.commit()
        assert realized_pnl_since(s, cutoff, tz) == Decimal("2.75")
