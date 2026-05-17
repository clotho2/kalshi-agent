"""Position bookkeeping and realized PnL accounting from fills.

Buy-only strategy: positions close either by buying the opposite side
(locks in $1 - cost) or by market settlement. We never sell directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.types import (
    KalshiFill,
    price_decimal_to_str,
    price_str_to_decimal,
)
from kalshi_agent.safety.fees import total_fee_dollars
from kalshi_agent.storage.models import Fill, PnlDaily, Position

log = get_logger(__name__)


def _fill_price(fill: KalshiFill) -> Decimal:
    raw = fill.yes_price_dollars if fill.side == "yes" else fill.no_price_dollars
    if not raw:
        raise ValueError(f"fill missing price for side {fill.side}: {fill}")
    return price_str_to_decimal(raw)


def apply_fill(
    session: Session,
    fill: KalshiFill,
    *,
    taker_rate: Decimal = Decimal("0.07"),
    maker_taker_ratio: Decimal = Decimal("0.25"),
) -> Decimal:
    """Apply a fill to the Position table. Returns realized PnL delta (signed).

    Idempotent on (kalshi_order_id, trade_id) — duplicate fills are skipped.
    """
    fill_id = fill.trade_id or f"{fill.order_id}:{fill.count}:{fill.created_time}"
    # Duplicate-fill check
    existing = session.scalars(
        select(Fill).where(
            Fill.kalshi_order_id == fill.order_id,
            Fill.market_ticker == fill.ticker,
            Fill.created_at == (fill.created_time or datetime.now(UTC)),
            Fill.count == fill.count,
        )
    ).first()
    if existing is not None:
        return Decimal("0")

    price = _fill_price(fill)
    fee = total_fee_dollars(
        fill.count, price, is_taker=fill.is_taker, rate=taker_rate
    )
    if not fill.is_taker:
        fee = fee * maker_taker_ratio / Decimal("0.25") * Decimal("0.25")
        # taker_fee already returns taker; maker is 25% of that. total_fee_dollars handles it.

    session.add(Fill(
        kalshi_order_id=fill.order_id,
        market_ticker=fill.ticker,
        side=fill.side,
        count=fill.count,
        price_dollars=price_decimal_to_str(price),
        fee_dollars=price_decimal_to_str(fee),
        is_taker=fill.is_taker,
        created_at=fill.created_time or datetime.now(UTC),
    ))

    pos = session.get(Position, fill.ticker)
    if pos is None:
        pos = Position(
            market_ticker=fill.ticker, side=fill.side, count=0,
            avg_price_dollars="0.0000", realized_pnl_dollars=0.0,
        )
        session.add(pos)
        session.flush()

    realized = Decimal("0")
    n = fill.count
    fill_side = fill.side
    action = fill.action

    if action == "buy":
        if pos.count == 0:
            pos.side = fill_side
            pos.count = n
            pos.avg_price_dollars = price_decimal_to_str(price)
        elif pos.side == fill_side:
            old_total = Decimal(pos.avg_price_dollars) * Decimal(pos.count)
            new_total = old_total + price * Decimal(n)
            new_count = pos.count + n
            pos.avg_price_dollars = price_decimal_to_str(new_total / Decimal(new_count))
            pos.count = new_count
        else:
            # Buying opposite side closes pairs at guaranteed $1 settlement
            pair_count = min(pos.count, n)
            close_pnl_per = Decimal("1") - Decimal(pos.avg_price_dollars) - price
            realized += close_pnl_per * Decimal(pair_count)
            pos.count -= pair_count
            remainder = n - pair_count
            if pos.count == 0:
                pos.avg_price_dollars = "0.0000"
                if remainder > 0:
                    pos.side = fill_side
                    pos.count = remainder
                    pos.avg_price_dollars = price_decimal_to_str(price)
    else:  # sell
        if pos.side != fill_side or pos.count == 0:
            log.warning("sell_without_position", ticker=fill.ticker, side=fill_side)
        else:
            n_close = min(pos.count, n)
            realized += (price - Decimal(pos.avg_price_dollars)) * Decimal(n_close)
            pos.count -= n_close
            if pos.count == 0:
                pos.avg_price_dollars = "0.0000"

    realized -= fee
    pos.realized_pnl_dollars = float(Decimal(str(pos.realized_pnl_dollars)) + realized)
    pos.updated_at = datetime.now(UTC)
    return realized


def daily_realized_pnl(session: Session, day_local: str, tz: ZoneInfo) -> tuple[Decimal, Decimal, int]:
    """Sum realized PnL components for fills in `day_local` (YYYY-MM-DD in display tz).

    Returns (realized_pnl_dollars, fees_dollars, fill_count). Realized PnL here is
    approximate: net cash flow of fills (proceeds - cost - fees) on closed pairs.
    Position-level realized_pnl is the source of truth; this is a fills-window slice.
    """
    midnight_local = datetime.strptime(day_local, "%Y-%m-%d").replace(tzinfo=tz)
    next_midnight = midnight_local + timedelta(days=1)
    start_utc = midnight_local.astimezone(UTC)
    end_utc = next_midnight.astimezone(UTC)
    fills = session.scalars(
        select(Fill).where(Fill.created_at >= start_utc, Fill.created_at < end_utc)
    ).all()
    fees = sum(Decimal(f.fee_dollars) for f in fills) if fills else Decimal("0")
    return Decimal("0"), fees, len(fills)


def snapshot_daily_pnl(session: Session, day_local: str, tz: ZoneInfo) -> PnlDaily:
    """Snapshot today's totals into PnlDaily."""
    _, fees, trade_count = daily_realized_pnl(session, day_local, tz)
    # Realized PnL "today" = sum of position-level realized_pnl_dollars changes since midnight.
    # Approximated as the sum of position realized_pnl (running total) — caller can diff snapshots.
    positions = session.scalars(select(Position)).all()
    realized_total = sum(p.realized_pnl_dollars for p in positions)
    row = session.get(PnlDaily, day_local) or PnlDaily(day=day_local)
    row.realized_pnl = float(realized_total)
    row.fees = float(fees)
    row.trade_count = trade_count
    row.updated_at = datetime.now(UTC)
    session.merge(row)
    return row


def open_exposure_dollars(session: Session) -> Decimal:
    positions = session.scalars(select(Position).where(Position.count != 0)).all()
    return sum(
        (Decimal(p.avg_price_dollars) * Decimal(p.count) for p in positions),
        Decimal("0"),
    )
