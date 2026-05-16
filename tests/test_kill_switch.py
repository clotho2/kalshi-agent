"""Kill switch: file-based, HTTP-based, automatic."""

from __future__ import annotations

import json
from decimal import Decimal

from kalshi_agent.safety.kill_switch import KillSwitch
from kalshi_agent.safety.risk_monitor import RiskMonitor


def test_kill_switch_starts_disengaged(kill_switch: KillSwitch) -> None:
    assert not kill_switch.is_engaged()


def test_kill_switch_engage_creates_file(kill_switch: KillSwitch) -> None:
    kill_switch.engage("test reason", source="unit_test")
    assert kill_switch.is_engaged()
    payload = json.loads(kill_switch.path.read_text())
    assert payload["reason"] == "test reason"
    assert payload["source"] == "unit_test"
    assert payload["timestamp"]


def test_kill_switch_engage_is_idempotent(kill_switch: KillSwitch) -> None:
    kill_switch.engage("first")
    first = kill_switch.path.read_text()
    kill_switch.engage("second")
    assert kill_switch.path.read_text() == first  # not overwritten


def test_kill_switch_disengage_removes_file(kill_switch: KillSwitch) -> None:
    kill_switch.engage("test")
    assert kill_switch.disengage()
    assert not kill_switch.is_engaged()


def test_kill_switch_disengage_when_not_engaged(kill_switch: KillSwitch) -> None:
    assert not kill_switch.disengage()


def test_external_touch_engages(kill_switch: KillSwitch) -> None:
    # Simulate `touch /var/lib/kalshi-agent/HALT`
    kill_switch.path.write_text("")
    assert kill_switch.is_engaged()
    # malformed JSON still treated as engaged
    assert kill_switch.reason() is not None


def test_automatic_engagement_on_error_spike(config, kill_switch, db_session_maker) -> None:
    rm = RiskMonitor(config, kill_switch, db_session_maker)
    for _ in range(config.risk.error_spike_threshold):
        rm.record_error()
    assert kill_switch.is_engaged()
    reason = kill_switch.reason()
    assert reason is not None
    assert "error_spike" in reason["reason"]


def test_check_trade_rejects_when_engaged(config, kill_switch, db_session_maker) -> None:
    kill_switch.engage("manual halt")
    rm = RiskMonitor(config, kill_switch, db_session_maker)
    decision = rm.check_trade(
        market_ticker="TEST-MARKET",
        side="yes",
        model_probability=Decimal("0.7"),
        current_price=Decimal("0.50"),
        confidence=Decimal("0.5"),
        bankroll_dollars=Decimal("1000"),
        market_category="economics",
    )
    assert not decision.allowed
    assert decision.reason == "kill_switch_engaged"
