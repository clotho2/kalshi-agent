"""Execution: take a Signal, validate, size, place an idempotent order."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from kalshi_agent.config import Config
from kalshi_agent.journal.discord import DiscordNotifier
from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.client import KalshiAPIError, KalshiAuthError, KalshiClient
from kalshi_agent.kalshi.types import OrderRequest, price_decimal_to_str, price_str_to_decimal
from kalshi_agent.safety.reconciliation import Reconciler
from kalshi_agent.safety.risk_monitor import RiskMonitor
from kalshi_agent.storage.models import Decision, Order
from kalshi_agent.strategies.base import Signal

log = get_logger(__name__)


class Executor:
    def __init__(
        self,
        config: Config,
        client: KalshiClient,
        risk: RiskMonitor,
        reconciler: Reconciler,
        session_maker: sessionmaker,
        discord: DiscordNotifier | None,
        bankroll_provider,  # callable returning current bankroll Decimal
    ) -> None:
        self._cfg = config
        self._client = client
        self._risk = risk
        self._reconciler = reconciler
        self._sm = session_maker
        self._discord = discord
        self._bankroll = bankroll_provider

    async def handle_signal(self, signal: Signal) -> None:
        # Don't race reconciliation
        async with self._reconciler.lock:
            await self._handle_signal_inner(signal)

    async def _handle_signal_inner(self, signal: Signal) -> None:
        if signal.is_expired():
            self._persist_decision(signal, accepted=False, reason="signal_expired")
            return

        try:
            market = await self._client.get_market(signal.market_ticker)
        except (KalshiAPIError, KalshiAuthError) as e:
            log.error("market_fetch_failed", ticker=signal.market_ticker, error=str(e))
            self._risk.record_error()
            self._persist_decision(signal, accepted=False, reason=f"market_fetch_failed:{e}")
            return

        # Current price for the side we want to buy
        if signal.side == "yes":
            ask = market.yes_ask_dollars
        else:
            ask = market.no_ask_dollars
        if not ask:
            self._persist_decision(signal, accepted=False, reason="no_ask_price")
            return
        current_price = price_str_to_decimal(ask)

        bankroll = self._bankroll()
        decision = self._risk.check_trade(
            market_ticker=signal.market_ticker,
            side=signal.side,
            model_probability=Decimal(str(signal.model_probability)),
            current_price=current_price,
            confidence=Decimal(str(signal.confidence)),
            bankroll_dollars=bankroll,
            market_category=market.category,
        )

        if not decision.allowed:
            self._persist_decision(signal, accepted=False, reason=decision.reason or "risk_rejected")
            log.info("trade_rejected", ticker=signal.market_ticker, reason=decision.reason)
            return

        client_order_id = str(uuid.uuid4())
        decision_row_id = self._persist_decision(
            signal, accepted=True, reason=None, order_id=client_order_id,
        )

        # Persist Order row BEFORE sending so we can recover on restart
        with self._sm() as s:
            order_row = Order(
                client_order_id=client_order_id,
                market_ticker=signal.market_ticker,
                side=signal.side,
                action="buy",
                count=decision.sized_contracts,
                price_dollars=price_decimal_to_str(current_price),
                status="pending",
                decision_id=decision_row_id,
            )
            s.add(order_row)
            s.commit()

        self._risk.record_order_attempt()

        # Build & send
        req = OrderRequest(
            ticker=signal.market_ticker,
            side=signal.side,
            action="buy",
            count_fp=f"{decision.sized_contracts}.00",
            yes_price_dollars=price_decimal_to_str(current_price) if signal.side == "yes" else None,
            no_price_dollars=price_decimal_to_str(current_price) if signal.side == "no" else None,
            type="limit",
            time_in_force="IOC",
            client_order_id=client_order_id,
        )

        try:
            resp = await self._client.create_order(req)
        except (KalshiAPIError, KalshiAuthError) as e:
            log.error("order_send_failed", error=str(e), client_order_id=client_order_id)
            self._risk.record_error()
            with self._sm() as s:
                row = s.query(Order).filter_by(client_order_id=client_order_id).one()
                row.status = "send_failed"
                s.commit()
            if self._discord:
                await self._discord.post(f":x: Order send failed: {signal.market_ticker} — {e}")
            return

        with self._sm() as s:
            row = s.query(Order).filter_by(client_order_id=client_order_id).one()
            row.kalshi_order_id = resp.order_id
            row.status = resp.status
            row.raw_response = resp.raw
            s.commit()

        log.info(
            "order_placed",
            ticker=signal.market_ticker, side=signal.side,
            contracts=decision.sized_contracts, price=price_decimal_to_str(current_price),
            order_id=resp.order_id, client_order_id=client_order_id,
        )

        if self._discord:
            await self._discord.post(
                f":chart_with_upwards_trend: **{signal.market_ticker}** {signal.side.upper()} "
                f"{decision.sized_contracts} @ {price_decimal_to_str(current_price)} | "
                f"p={signal.model_probability:.3f} | {signal.rationale[:120]}"
            )

    def _persist_decision(
        self,
        signal: Signal,
        *,
        accepted: bool,
        reason: str | None,
        order_id: str | None = None,
    ) -> int:
        with self._sm() as s:
            row = Decision(
                strategy=signal.strategy_name,
                market_ticker=signal.market_ticker,
                side=signal.side,
                model_probability=signal.model_probability,
                confidence=signal.confidence,
                rationale=signal.rationale,
                valid_until=signal.valid_until,
                accepted=accepted,
                rejection_reason=reason,
                order_id=order_id,
                created_at=datetime.now(UTC),
            )
            s.add(row)
            s.commit()
            return row.id
