"""Position accounting and PnL math from fills."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from kalshi_agent.kalshi.types import KalshiFill
from kalshi_agent.positions import apply_fill, open_exposure_dollars
from kalshi_agent.storage.models import Fill, Position


def _fill(**kw) -> KalshiFill:
    base = dict(
        order_id="o1", trade_id="t1", ticker="FOO", side="yes",
        action="buy", count=10, yes_price_dollars="0.5000",
        no_price_dollars=None, is_taker=True,
        created_time=datetime.now(UTC),
    )
    base.update(kw)
    return KalshiFill(**base)


def test_open_yes_position(db_session_maker) -> None:
    with db_session_maker() as s:
        realized = apply_fill(s, _fill())
        s.commit()
    with db_session_maker() as s:
        pos = s.get(Position, "FOO")
    assert pos is not None
    assert pos.side == "yes"
    assert pos.count == 10
    assert pos.avg_price_dollars == "0.5000"
    # Realized PnL should equal -fees: 10 * ceil(0.07*0.5*0.5*100)/100 = 10 * 0.02 = $0.20 loss
    assert realized < 0
    assert abs(realized + Decimal("0.20")) < Decimal("0.001")


def test_add_to_existing_yes_position(db_session_maker) -> None:
    with db_session_maker() as s:
        apply_fill(s, _fill(count=10, yes_price_dollars="0.5000"))
        apply_fill(s, _fill(trade_id="t2", count=10, yes_price_dollars="0.6000"))
        s.commit()
    with db_session_maker() as s:
        pos = s.get(Position, "FOO")
    assert pos.count == 20
    # avg = (10*0.50 + 10*0.60) / 20 = 0.55
    assert pos.avg_price_dollars == "0.5500"


def test_buy_opposite_closes_pairs_and_realizes_pnl(db_session_maker) -> None:
    """Buy 10 YES @ 0.40, then buy 10 NO @ 0.50.
    Each pair locks in $1; we paid 0.40+0.50=0.90; profit per pair = $0.10.
    Total realized before fees: $1.00 (10 pairs). Minus fees on both fills.
    """
    with db_session_maker() as s:
        apply_fill(s, _fill(count=10, yes_price_dollars="0.4000"))
        apply_fill(s, _fill(
            trade_id="t2", count=10, side="no",
            yes_price_dollars=None, no_price_dollars="0.5000",
        ))
        s.commit()
    with db_session_maker() as s:
        pos = s.get(Position, "FOO")
    assert pos.count == 0
    # Position should have accumulated realized_pnl from the close pairs minus fees
    # Pair realized = (1 - 0.40 - 0.50) * 10 = 1.00
    # Fee on first fill (10 contracts @ 0.40): ceil(0.07*0.4*0.6*100)/100 = ceil(1.68)/100 = 0.02 per contract * 10 = 0.20
    # Fee on second fill (10 contracts @ 0.50): ceil(0.07*0.5*0.5*100)/100 = 0.02 per * 10 = 0.20
    # Net: 1.00 - 0.40 = 0.60
    assert abs(pos.realized_pnl_dollars - 0.60) < 0.01


def test_buy_opposite_with_remainder_flips_side(db_session_maker) -> None:
    """Long 10 YES @ 0.50, buy 15 NO @ 0.50.
    Closes 10 pairs; opens 5 NO @ 0.50.
    """
    with db_session_maker() as s:
        apply_fill(s, _fill(count=10, yes_price_dollars="0.5000"))
        apply_fill(s, _fill(
            trade_id="t2", count=15, side="no",
            yes_price_dollars=None, no_price_dollars="0.5000",
        ))
        s.commit()
    with db_session_maker() as s:
        pos = s.get(Position, "FOO")
    assert pos.side == "no"
    assert pos.count == 5
    assert pos.avg_price_dollars == "0.5000"


def test_fill_idempotency(db_session_maker) -> None:
    f = _fill()
    with db_session_maker() as s:
        apply_fill(s, f)
        s.commit()
    with db_session_maker() as s:
        # Re-apply the identical fill — should be skipped
        realized = apply_fill(s, f)
        s.commit()
    assert realized == 0
    with db_session_maker() as s:
        fills = s.query(Fill).all()
    assert len(fills) == 1


def test_open_exposure_dollars(db_session_maker) -> None:
    with db_session_maker() as s:
        s.add(Position(market_ticker="A", side="yes", count=10, avg_price_dollars="0.5000"))
        s.add(Position(market_ticker="B", side="no", count=20, avg_price_dollars="0.3000"))
        s.add(Position(market_ticker="C", side="yes", count=0, avg_price_dollars="0.0000"))
        s.commit()
    with db_session_maker() as s:
        exp = open_exposure_dollars(s)
    assert exp == Decimal("11.0000")  # 5 + 6
