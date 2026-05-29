"""Risk monitor: every limit check. Pure functions, deterministic."""

from __future__ import annotations

from decimal import Decimal

from kalshi_agent.safety.risk_monitor import RiskMonitor
from kalshi_agent.storage.models import Position


def _rm(config, kill_switch, db_session_maker) -> RiskMonitor:
    return RiskMonitor(config, kill_switch, db_session_maker)


def _check(rm, **overrides):
    base = dict(
        market_ticker="TEST-MARKET",
        side="yes",
        model_probability=Decimal("0.7"),
        current_price=Decimal("0.50"),
        confidence=Decimal("1.0"),
        bankroll_dollars=Decimal("1000"),
        market_category="economics",
    )
    base.update(overrides)
    return rm.check_trade(**base)


def test_passes_with_good_edge(config, kill_switch, db_session_maker) -> None:
    rm = _rm(config, kill_switch, db_session_maker)
    decision = _check(rm)
    assert decision.allowed, decision.reason
    assert decision.sized_contracts > 0


def test_test_mode_forces_one_contract_on_thin_edge(config, kill_switch, db_session_maker) -> None:
    # Low confidence + tiny edge would normally round to zero contracts.
    rm = RiskMonitor(config, kill_switch, db_session_maker, test_mode=True)
    decision = _check(
        rm,
        model_probability=Decimal("0.5001"),
        current_price=Decimal("0.50"),
        confidence=Decimal("0.1"),
    )
    assert decision.allowed, decision.reason
    assert decision.sized_contracts == 1


def test_test_mode_still_rejects_price_extremum(config, kill_switch, db_session_maker) -> None:
    rm = RiskMonitor(config, kill_switch, db_session_maker, test_mode=True)
    decision = _check(rm, current_price=Decimal("0.0"))
    assert not decision.allowed
    assert decision.reason == "price_at_extremum"


def test_test_mode_still_honors_kill_switch(config, kill_switch, db_session_maker) -> None:
    rm = RiskMonitor(config, kill_switch, db_session_maker, test_mode=True)
    kill_switch.engage("manual", source="test")
    decision = _check(rm)
    assert not decision.allowed
    assert decision.reason == "kill_switch_engaged"


def test_rejects_below_min_edge(config, kill_switch, db_session_maker) -> None:
    rm = _rm(config, kill_switch, db_session_maker)
    decision = _check(
        rm,
        model_probability=Decimal("0.505"),  # tiny edge over fair price 0.50
        current_price=Decimal("0.50"),
    )
    assert not decision.allowed
    assert "edge_below_min" in (decision.reason or "")


def test_rejects_when_no_edge(config, kill_switch, db_session_maker) -> None:
    rm = _rm(config, kill_switch, db_session_maker)
    decision = _check(rm, model_probability=Decimal("0.40"), current_price=Decimal("0.50"))
    assert not decision.allowed
    assert decision.reason == "no_edge"


def test_rejects_off_whitelist_category(config, kill_switch, db_session_maker) -> None:
    rm = _rm(config, kill_switch, db_session_maker)
    decision = _check(rm, market_category="sports")
    assert not decision.allowed
    assert "category_not_whitelisted" in decision.reason


def test_rejects_when_per_market_cap_full(config, kill_switch, db_session_maker) -> None:
    with db_session_maker() as s:
        s.add(Position(
            market_ticker="TEST-MARKET", side="yes", count=100,
            avg_price_dollars="0.50",  # 100 * 0.50 = $50 used, cap is $25
        ))
        s.commit()
    rm = _rm(config, kill_switch, db_session_maker)
    decision = _check(rm)
    assert not decision.allowed
    assert decision.reason == "per_market_cap_full"


def test_total_exposure_cap(config, kill_switch, db_session_maker) -> None:
    with db_session_maker() as s:
        for i in range(10):
            s.add(Position(
                market_ticker=f"T-{i}", side="yes", count=100,
                avg_price_dollars="0.50",  # $50 each * 10 = $500 > $200 cap
            ))
        s.commit()
    rm = _rm(config, kill_switch, db_session_maker)
    decision = _check(rm)
    assert not decision.allowed
    assert decision.reason == "max_total_exposure_exceeded"


def test_per_order_contract_cap_clamps(config, kill_switch, db_session_maker) -> None:
    rm = _rm(config, kill_switch, db_session_maker)
    # Strong edge with high bankroll would otherwise size > 50 contracts
    decision = _check(
        rm,
        model_probability=Decimal("0.90"),
        current_price=Decimal("0.50"),
        bankroll_dollars=Decimal("100000"),
    )
    assert decision.allowed
    assert decision.sized_contracts <= config.risk.per_order_max_contracts


def test_runaway_order_rate_trips_kill_switch(config, kill_switch, db_session_maker) -> None:
    rm = _rm(config, kill_switch, db_session_maker)
    for _ in range(config.risk.max_orders_per_minute):
        rm.record_order_attempt()
    decision = _check(rm)
    assert not decision.allowed
    assert decision.reason == "max_orders_per_minute_exceeded"
    assert kill_switch.is_engaged()


def test_price_at_extremum(config, kill_switch, db_session_maker) -> None:
    rm = _rm(config, kill_switch, db_session_maker)
    decision = _check(rm, current_price=Decimal("1.0"))
    assert not decision.allowed
    assert decision.reason == "price_at_extremum"


def test_fee_math_parabolic_symmetric() -> None:
    from kalshi_agent.safety.fees import taker_fee_per_contract_dollars
    fee_05 = taker_fee_per_contract_dollars(Decimal("0.50"))
    fee_80 = taker_fee_per_contract_dollars(Decimal("0.80"))
    fee_20 = taker_fee_per_contract_dollars(Decimal("0.20"))
    assert fee_05 == Decimal("0.02")  # ceil(0.07 * 0.5 * 0.5 * 100)/100 = ceil(1.75)/100 = 0.02
    assert fee_80 == fee_20  # symmetric


def test_fee_at_extremes_near_zero() -> None:
    from kalshi_agent.safety.fees import taker_fee_per_contract_dollars
    assert taker_fee_per_contract_dollars(Decimal("0.99")) == Decimal("0.01")
    assert taker_fee_per_contract_dollars(Decimal("0.01")) == Decimal("0.01")
