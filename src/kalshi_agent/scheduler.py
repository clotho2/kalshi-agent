"""APScheduler wiring. All cron in display TZ (ET); intervals in seconds."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.config import Config
from kalshi_agent.execution import Executor
from kalshi_agent.journal.discord import DiscordNotifier
from kalshi_agent.journal.logger import get_logger
from kalshi_agent.safety.reconciliation import Reconciler
from kalshi_agent.storage.models import Fill, Order, PnlDaily
from kalshi_agent.strategies.base import Strategy

log = get_logger(__name__)


def build_scheduler(
    config: Config,
    strategy: Strategy,
    executor: Executor,
    reconciler: Reconciler,
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
        except Exception as e:
            log.error("reconciliation_failed", error=str(e))

    async def _eod_summary() -> None:
        await _post_summary("eod", discord, session_maker, tz)

    async def _weekly_summary() -> None:
        await _post_summary("weekly", discord, session_maker, tz)

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

    return sched


async def _post_summary(kind: str, discord, session_maker, tz) -> None:
    from datetime import timedelta
    now_local = datetime.now(tz)
    if kind == "eod":
        cutoff = now_local.replace(hour=0, minute=0, second=0).astimezone(UTC)
        label = now_local.strftime("%Y-%m-%d")
    else:
        cutoff = (now_local - timedelta(days=7)).astimezone(UTC)
        label = f"week ending {now_local.strftime('%Y-%m-%d')}"

    with session_maker() as s:
        orders = s.scalars(select(Order).where(Order.created_at >= cutoff)).all()
        fills = s.scalars(select(Fill).where(Fill.created_at >= cutoff)).all()
        fees = sum(float(f.fee_dollars) for f in fills)

    msg = (f":calendar_spiral: **{kind.upper()} summary** ({label}): "
           f"orders={len(orders)} fills={len(fills)} fees=${fees:.4f}")
    log.info("summary", kind=kind, orders=len(orders), fills=len(fills), fees=fees)
    if discord:
        await discord.post(msg)

    # Persist daily aggregate
    if kind == "eod":
        with session_maker() as s:
            row = s.get(PnlDaily, label) or PnlDaily(day=label)
            row.fees = fees
            row.trade_count = len(fills)
            s.merge(row)
            s.commit()
