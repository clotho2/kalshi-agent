"""Pre-trade and post-trade risk checks. Pure Python, deterministic, no LLM."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.config import Config
from kalshi_agent.journal.logger import get_logger
from kalshi_agent.safety.fees import edge_after_fees_dollars, total_fee_dollars
from kalshi_agent.safety.kill_switch import KillSwitch
from kalshi_agent.safety.pnl import realized_pnl_since
from kalshi_agent.storage.models import Position

UTC = timezone.utc

log = get_logger(__name__)


@dataclass
class RiskDecision:
    allowed: bool
    reason: str | None = None
    sized_contracts: int = 0
    expected_fees_dollars: Decimal = Decimal("0")
    expected_edge_dollars: Decimal = Decimal("0")


class RiskMonitor:
    def __init__(
        self,
        config: Config,
        kill_switch: KillSwitch,
        session_maker: sessionmaker,
        test_mode: bool = False,
    ) -> None:
        self._cfg = config
        self._kill = kill_switch
        self._sm = session_maker
        self._test_mode = test_mode
        self._order_timestamps: deque[float] = deque(maxlen=200)
        self._error_timestamps: deque[float] = deque(maxlen=200)
        self._tz = ZoneInfo(config.schedule.display_timezone)

    def record_order_attempt(self) -> None:
        self._order_timestamps.append(time.monotonic())

    def record_error(self) -> None:
        self._error_timestamps.append(time.monotonic())
        self._maybe_trip_on_error_spike()

    def _orders_in_last_minute(self) -> int:
        cutoff = time.monotonic() - 60.0
        return sum(1 for t in self._order_timestamps if t >= cutoff)

    def _maybe_trip_on_error_spike(self) -> None:
        window = self._cfg.risk.error_spike_window_seconds
        threshold = self._cfg.risk.error_spike_threshold
        cutoff = time.monotonic() - window
        recent = sum(1 for t in self._error_timestamps if t >= cutoff)
        if recent >= threshold:
            self._kill.engage(
                f"error_spike: {recent} errors in {window}s",
                source="risk_monitor",
                payload={"errors": recent, "window_seconds": window},
            )

    def current_total_exposure_dollars(self) -> Decimal:
        with self._sm() as s:
            positions = s.scalars(select(Position)).all()
            total = Decimal("0")
            for p in positions:
                total += Decimal(p.avg_price_dollars) * Decimal(p.count)
            return total

    def current_position_exposure_dollars(self, ticker: str) -> Decimal:
        with self._sm() as s:
            p = s.get(Position, ticker)
            if p is None:
                return Decimal("0")
            return Decimal(p.avg_price_dollars) * Decimal(p.count)

    def _existing_position(self, ticker: str) -> Position | None:
        with self._sm() as s:
            p = s.get(Position, ticker)
            if p is None:
                return None
            return Position(
                market_ticker=p.market_ticker,
                side=p.side,
                count=p.count,
                avg_price_dollars=p.avg_price_dollars,
                realized_pnl_dollars=p.realized_pnl_dollars,
                updated_at=p.updated_at,
            )

    def daily_realized_pnl_dollars(self) -> Decimal:
        """Realized PnL since local midnight using cumulative PnL snapshots."""
        now_local = datetime.now(self._tz)
        midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._sm() as s:
            return realized_pnl_since(s, midnight_local.astimezone(UTC), self._tz)

    def check_trade(
        self,
        *,
        market_ticker: str,
        side: str,
        model_probability: Decimal,
        current_price: Decimal,
        confidence: Decimal,
        bankroll_dollars: Decimal,
        market_category: str | None,
    ) -> RiskDecision:
        if self._kill.is_engaged():
            return RiskDecision(False, "kill_switch_engaged")

        whitelist_cats = set(self._cfg.markets.whitelist_categories)
        whitelist_tix = set(self._cfg.markets.whitelist_tickers)
        if whitelist_tix and market_ticker not in whitelist_tix:
            return RiskDecision(False, "ticker_not_whitelisted")
        if whitelist_cats and (market_category or "").lower() not in {c.lower() for c in whitelist_cats}:
            return RiskDecision(False, f"category_not_whitelisted:{market_category}")

        if self._orders_in_last_minute() >= self._cfg.risk.max_orders_per_minute:
            self._kill.engage(
                "runaway_order_rate", source="risk_monitor",
                payload={"orders_last_minute": self._orders_in_last_minute()},
            )
            return RiskDecision(False, "max_orders_per_minute_exceeded")

        daily_pnl = self.daily_realized_pnl_dollars()
        if daily_pnl <= -Decimal(self._cfg.risk.max_daily_loss_usd):
            self._kill.engage(
                f"daily_loss_breach: {daily_pnl}", source="risk_monitor",
                payload={"daily_pnl": str(daily_pnl)},
            )
            return RiskDecision(False, "max_daily_loss_exceeded")

        price = current_price
        if price <= 0 or price >= 1:
            return RiskDecision(False, "price_at_extremum")

        existing = self._existing_position(market_ticker)
        is_closing_buy = existing is not None and existing.count > 0 and existing.side != side

        if is_closing_buy:
            contracts = min(existing.count, self._cfg.risk.per_order_max_contracts)
            fees = total_fee_dollars(
                contracts, price, is_taker=self._cfg.fees.assume_taker,
                rate=Decimal(str(self._cfg.fees.taker_rate)),
            )
            close_edge = (Decimal("1") - Decimal(existing.avg_price_dollars) - price) * Decimal(contracts) - fees
            return RiskDecision(
                True,
                None,
                sized_contracts=contracts,
                expected_fees_dollars=fees,
                expected_edge_dollars=close_edge,
            )

        total_exp = self.current_total_exposure_dollars()
        if total_exp >= Decimal(self._cfg.risk.max_total_exposure_usd):
            return RiskDecision(False, "max_total_exposure_exceeded")

        per_market_cap = Decimal(self._cfg.risk.max_position_per_market_usd)
        used_for_market = self.current_position_exposure_dollars(market_ticker)
        if per_market_cap - used_for_market <= 0:
            return RiskDecision(False, "per_market_cap_full")

        if self._test_mode:
            # Pipeline validation: force a minimal real order instead of the
            # Kelly-sized amount (which rounds to zero for the low-confidence,
            # tiny-edge estimates a conservative LLM produces). All the
            # structural safety gates above still apply.
            contracts = min(1, self._cfg.risk.per_order_max_contracts)
            fees = total_fee_dollars(
                contracts, price, is_taker=self._cfg.fees.assume_taker,
                rate=Decimal(str(self._cfg.fees.taker_rate)),
            )
            edge = edge_after_fees_dollars(
                contracts, model_probability, price, side,
                is_taker=self._cfg.fees.assume_taker,
                rate=Decimal(str(self._cfg.fees.taker_rate)),
            )
            return RiskDecision(
                True, None,
                sized_contracts=contracts,
                expected_fees_dollars=fees,
                expected_edge_dollars=edge,
            )

        p = model_probability if side == "yes" else (Decimal("1") - model_probability)
        denom = Decimal("1") - price
        edge_per_dollar = p - price
        if edge_per_dollar <= 0:
            return RiskDecision(False, "no_edge")

        kelly = edge_per_dollar / denom
        kelly_capped = min(
            kelly * Decimal(self._cfg.risk.kelly_fraction) * confidence,
            Decimal(self._cfg.risk.max_kelly_size_pct_bankroll),
        )
        if kelly_capped <= 0:
            return RiskDecision(False, "kelly_zero_or_negative")

        size_dollars = kelly_capped * bankroll_dollars

        size_dollars = min(size_dollars, per_market_cap - used_for_market)

        remaining_global = Decimal(self._cfg.risk.max_total_exposure_usd) - total_exp
        size_dollars = min(size_dollars, remaining_global)

        contracts = int(size_dollars / price) if price > 0 else 0
        contracts = min(contracts, self._cfg.risk.per_order_max_contracts)
        if contracts <= 0:
            return RiskDecision(False, "size_below_one_contract")

        fees = total_fee_dollars(
            contracts, price, is_taker=self._cfg.fees.assume_taker,
            rate=Decimal(str(self._cfg.fees.taker_rate)),
        )
        edge = edge_after_fees_dollars(
            contracts, model_probability, price, side,
            is_taker=self._cfg.fees.assume_taker,
            rate=Decimal(str(self._cfg.fees.taker_rate)),
        )
        capital_at_risk = price * Decimal(contracts)
        if capital_at_risk <= 0:
            return RiskDecision(False, "zero_capital_at_risk")
        edge_bps = (edge / capital_at_risk) * Decimal("10000")
        if edge_bps < Decimal(self._cfg.risk.min_edge_after_fees_bps):
            return RiskDecision(
                False,
                f"edge_below_min:{edge_bps:.0f}bps<{self._cfg.risk.min_edge_after_fees_bps}bps",
                sized_contracts=contracts, expected_fees_dollars=fees, expected_edge_dollars=edge,
            )

        return RiskDecision(
            True, None,
            sized_contracts=contracts,
            expected_fees_dollars=fees,
            expected_edge_dollars=edge,
        )

    async def run_background(self, stop_event: asyncio.Event) -> None:
        """Periodically re-check global limits (post-hoc drift, error spikes)."""
        interval = self._cfg.schedule.risk_monitor_tick_seconds
        while not stop_event.is_set():
            try:
                if not self._kill.is_engaged():
                    total_exp = self.current_total_exposure_dollars()
                    if total_exp > Decimal(self._cfg.risk.max_total_exposure_usd):
                        self._kill.engage(
                            f"post_hoc_exposure_breach: {total_exp}",
                            source="risk_monitor",
                            payload={"total_exposure": str(total_exp)},
                        )
                    daily = self.daily_realized_pnl_dollars()
                    if daily <= -Decimal(self._cfg.risk.max_daily_loss_usd):
                        self._kill.engage(
                            f"post_hoc_daily_loss: {daily}",
                            source="risk_monitor",
                            payload={"daily_pnl": str(daily)},
                        )
            except Exception as e:
                log.error("risk_monitor_loop_error", error=str(e))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass
