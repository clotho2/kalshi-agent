"""APScheduler wiring. All cron in display TZ (ET); intervals in seconds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.config import Config
from kalshi_agent.execution import Executor
from kalshi_agent.fills import FillIngestor
from kalshi_agent.journal.discord import DiscordNotifier
from kalshi_agent.journal.logger import get_logger
from kalshi_agent.positions import snapshot_daily_pnl
from kalshi_agent.safety.pnl import realized_pnl_since
from kalshi_agent.safety.reconciliation import Reconciler
from kalshi_agent.storage.models import Fill, Order, Position
from kalshi_agent.strategies.base import Strategy

UTC = timezone.utc

log = get_logger(__name__)


def build_scheduler(
    config: Config,
    strategy: Strategy,
    executor: Executor,
    reconciler: Reconciler,
    fill_ingestor: FillIngestor,
    session_maker: sessionmaker,
    discord: DiscordNotifier | None,
) -> AsyncIOScheduler:
    tz = ZoneInfo(config.schedule.display_timezone)
    sched = AsyncIOScheduler(timezone=tz)

    async def _tick_strategy() -> None:
        try:
            signals = await strategy.generate_signals()
        except Exception as e:
            log.error("strategy_tick_failed", error=str(e))
            return
        for sig in signals:
            try:
                await executor.handle_signal(sig)
            except Exception as e:
                log.error("execution_failed", error=str(e))

    async def _tick_reconcile() -> None:
        try:
            await reconciler.reconcile(source="scheduled")
            await fill_ingestor.catch_up()
        except Exception as e:
            log.error("reconciliation_failed", error=str(e))

    async def _eod_summary() -> None:
        await _post_summary("eod", discord, session_maker, tz)

    async def _weekly_summary() -> None:
        await _post_summary("weekly", discord, session_maker, tz)

    async def _midnight_snapshot() -> None:
        now_local = datetime.now(tz)
        day = now_local.strftime("%Y-%m-%d")
        with session_maker() as s:
            snapshot_daily_pnl(s, day, tz)
            s.commit()
        log.info("daily_pnl_snapshot", day=day)

    sched.add_job(_tick_strategy,
                  IntervalTrigger(seconds=config.schedule.strategy_tick_seconds),
                  id="strategy_tick", max_instances=1, coalesce=True)
    sched.add_job(_tick_reconcile,
                  IntervalTrigger(seconds=config.schedule.reconciliation_interval_seconds),
                  id="reconcile_tick", max_instances=1, coalesce=True)
    sched.add_job(_eod_summary,
                  CronTrigger.from_crontab(config.schedule.eod_summary_cron, timezone=tz),
                  id="eod")
    sched.add_job(_weekly_summary,
                  CronTrigger.from_crontab(config.schedule.weekly_summary_cron, timezone=tz),
                  id="weekly")
    sched.add_job(_midnight_snapshot,
                  CronTrigger(hour=0, minute=0, timezone=tz),
                  id="midnight_snapshot")

    return sched


async def _post_summary(kind: str, discord, session_maker, tz) -> None:
    now_local = datetime.now(tz)
    if kind == "eod":
        cutoff = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(UTC)
        label = now_local.strftime("%Y-%m-%d")
        pnl_label = "realized pnl today"
    else:
        cutoff = (now_local - timedelta(days=7)).astimezone(UTC)
        label = f"week ending {now_local.strftime('%Y-%m-%d')}"
        pnl_label = "realized pnl this week"

    with session_maker() as s:
        orders = s.scalars(select(Order).where(Order.created_at >= cutoff)).all()
        fills = s.scalars(select(Fill).where(Fill.created_at >= cutoff)).all()
        open_positions = s.scalars(
            select(Position).where(Position.count != 0)
        ).all()
        fees = sum((Decimal(f.fee_dollars) for f in fills), Decimal("0"))
        net_pnl = realized_pnl_since(s, cutoff, tz)

    open_pos_str = ", ".join(
        f"{p.market_ticker}({p.side}×{p.count}@{p.avg_price_dollars})"
        for p in open_positions
    ) or "(none)"

    msg = (
        f":calendar_spiral: **{kind.upper()} summary** ({label})\n"
        f"  orders: {len(orders)}  fills: {len(fills)}\n"
        f"  fees: ${fees:.4f}\n"
        f"  {pnl_label}: ${net_pnl:.4f}\n"
        f"  open positions: {open_pos_str[:1500]}"
    )
    log.info(
        "summary", kind=kind,
        orders=len(orders), fills=len(fills),
        fees=str(fees), realized=str(net_pnl),
        open_positions=len(open_positions),
    )
    if discord:
        await discord.post(msg)
