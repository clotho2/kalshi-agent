"""Realized PnL helpers for cumulative position accounting.

Position.realized_pnl_dollars is stored as a cumulative running total.  Any UI
or notification that reports a period must subtract the latest daily snapshot
at or before that period boundary instead of pretending the cumulative value is
period-local.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_agent.storage.models import PnlDaily, Position


def cumulative_realized_pnl(session: Session) -> Decimal:
    """Return the current cumulative realized PnL across all positions."""
    positions = session.scalars(select(Position)).all()
    return sum(
        (Decimal(str(p.realized_pnl_dollars)) for p in positions),
        Decimal("0"),
    )


def snapshot_baseline_at_or_before(
    session: Session,
    cutoff: datetime,
    tz: ZoneInfo,
) -> Decimal:
    """Return cumulative realized PnL from the latest snapshot <= cutoff date.

    Daily snapshots are keyed by local YYYY-MM-DD.  When there is no prior
    snapshot, zero is the safest baseline because the bot may be freshly
    deployed or tests may not have seeded historical snapshots.
    """
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    cutoff_day = cutoff.astimezone(tz).strftime("%Y-%m-%d")
    row = session.scalars(
        select(PnlDaily)
        .where(PnlDaily.day <= cutoff_day)
        .order_by(PnlDaily.day.desc())
        .limit(1)
    ).first()
    if row is None:
        return Decimal("0")
    return Decimal(str(row.realized_pnl))


def realized_pnl_since(session: Session, cutoff: datetime, tz: ZoneInfo) -> Decimal:
    """Return realized PnL since cutoff using cumulative snapshots."""
    return cumulative_realized_pnl(session) - snapshot_baseline_at_or_before(session, cutoff, tz)
