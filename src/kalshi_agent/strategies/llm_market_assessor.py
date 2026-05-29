"""LLM-driven Kalshi market mispricing strategy.

Two modes:

* **Auto-discovery (default)**: with `tickers=[]`, the strategy queries Kalshi's
  open markets, filters by category whitelist, minimum volume, and minimum
  time-to-close, then asks the LLM to assess each candidate. The set of markets
  is rediscovered every tick — new markets get picked up automatically.

* **Manual (legacy)**: with `tickers=[...]`, the strategy only assesses those
  exact tickers. Useful for testing or pinning specific markets.

Per-ticker throttling (`min_seconds_between_signals_per_ticker`) prevents
re-running the LLM on the same ticker every minute regardless of mode.

Only markets where:
  - LLM probability differs from market price by > min_edge
  - LLM confidence > min_confidence
  - market is active and has an ask price
produce signals. The downstream risk monitor still re-checks edge after fees.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_agent.kalshi.types import Market, price_str_to_decimal
from kalshi_agent.llm.openrouter import OpenRouterClient, OpenRouterError
from kalshi_agent.storage.models import Decision
from kalshi_agent.strategies.base import Signal, Strategy

UTC = timezone.utc

log = get_logger(__name__)


_SYSTEM_PROMPT = """You are a careful probabilistic forecaster for prediction markets on Kalshi.
You receive a market description and current prices. You return a structured JSON
estimate of the true probability that the market resolves YES.

Be conservative. If you don't have strong evidence that the market is mispriced,
return a probability close to the market's mid price and a low confidence value.
False signals cost money; only express high confidence when you can articulate
a concrete, falsifiable reason the market is mispriced.

Output strictly this JSON schema (no other keys, no commentary):
{
  "probability": 0.0..1.0,
  "confidence": 0.0..1.0,
  "rationale": "string"
}
"""


def _user_prompt(market: Market) -> str:
    mid = None
    if market.yes_bid_dollars and market.yes_ask_dollars:
        mid = (price_str_to_decimal(market.yes_bid_dollars)
               + price_str_to_decimal(market.yes_ask_dollars)) / 2
    time_to_close = ""
    if market.close_time:
        delta = market.close_time - datetime.now(UTC)
        if delta.total_seconds() > 0:
            hours = delta.total_seconds() / 3600
            time_to_close = f"\nTime until market close: {hours:.1f} hours"
    return f"""Market ticker: {market.ticker}
Title: {market.title or market.ticker}
Subtitle: {market.subtitle or '(none)'}
Description: {market.description or '(none)'}
Category: {market.category or '(none)'}{time_to_close}

Current prices (decimal dollars, 0.0000 to 1.0000):
  YES bid={market.yes_bid_dollars}  YES ask={market.yes_ask_dollars}
  NO  bid={market.no_bid_dollars}   NO  ask={market.no_ask_dollars}
  YES mid={mid if mid is not None else 'unknown'}

Estimate P(YES) and your confidence."""


class LLMMarketAssessor(Strategy):
    """Discovers candidate markets, asks an LLM to assess each, emits filtered signals."""

    name = "llm_market_assessor"

    def __init__(
        self,
        *,
        kalshi_client: KalshiClient,
        llm_client: OpenRouterClient,
        session_maker: sessionmaker,
        tickers: list[str],
        categories: list[str],
        max_markets_per_tick: int,
        min_volume_contracts: int,
        min_hours_to_close: float,
        discovery_max_pages: int,
        min_edge: Decimal,
        min_confidence: Decimal,
        signal_ttl_minutes: int,
        min_seconds_between_signals_per_ticker: int = 1800,
    ) -> None:
        self._kalshi = kalshi_client
        self._llm = llm_client
        self._sm = session_maker
        self._tickers = tickers
        self._categories = [c.lower() for c in categories]
        self._max_per_tick = max_markets_per_tick
        self._min_volume = min_volume_contracts
        self._min_hours_to_close = min_hours_to_close
        self._discovery_max_pages = discovery_max_pages
        self._min_edge = min_edge
        self._min_confidence = min_confidence
        self._ttl = timedelta(minutes=signal_ttl_minutes)
        self._min_between = timedelta(seconds=min_seconds_between_signals_per_ticker)

    def _was_recently_signalled(self, ticker: str) -> bool:
        cutoff = datetime.now(UTC) - self._min_between
        with self._sm() as s:
            row = s.scalars(
                select(Decision)
                .where(Decision.market_ticker == ticker)
                .where(Decision.strategy == self.name)
                .where(Decision.created_at >= cutoff)
                .order_by(Decision.created_at.desc())
                .limit(1)
            ).first()
        return row is not None

    def _reject_reason(self, m: Market, close_cutoff: datetime) -> str | None:
        """Returns a short reason string when the market fails filters, else None."""
        if (m.status or "").lower() not in {"active", "open"}:
            return f"status:{m.status or 'none'}"
        if not m.yes_ask_dollars or not m.no_ask_dollars:
            return "no_ask_price"
        if self._categories and (m.category or "").lower() not in self._categories:
            return f"category:{m.category or 'none'}"
        if m.volume is not None and m.volume < self._min_volume:
            return "low_volume"
        if m.close_time and m.close_time < close_cutoff:
            return "imminent_close"
        return None

    async def _discover(self) -> list[Market]:
        """Scan open markets, return up to max_per_tick that pass filters and aren't throttled."""
        close_cutoff = datetime.now(UTC) + timedelta(hours=self._min_hours_to_close)
        candidates: list[Market] = []
        cursor: str | None = None
        scanned = 0
        rejected: dict[str, int] = {}
        for _ in range(self._discovery_max_pages):
            try:
                markets, cursor = await self._kalshi.list_markets(
                    status="open", limit=200, cursor=cursor,
                )
            except KalshiAPIError as e:
                log.warning("discovery_list_failed", error=str(e))
                break
            for m in markets:
                scanned += 1
                reason = self._reject_reason(m, close_cutoff)
                if reason is not None:
                    rejected[reason] = rejected.get(reason, 0) + 1
                    continue
                if self._was_recently_signalled(m.ticker):
                    rejected["recently_signalled"] = rejected.get("recently_signalled", 0) + 1
                    continue
                candidates.append(m)
                if len(candidates) >= self._max_per_tick:
                    log.info("discovery_done", scanned=scanned, candidates=len(candidates),
                             rejected=rejected, scanned_pages_capped=True)
                    return candidates
            if not cursor:
                break
        log.info("discovery_done", scanned=scanned, candidates=len(candidates),
                 rejected=rejected, scanned_pages_capped=False)
        return candidates

    async def _resolve_markets(self) -> list[Market]:
        """If manual tickers given, fetch each; else discover."""
        if self._tickers:
            out: list[Market] = []
            for t in self._tickers:
                if self._was_recently_signalled(t):
                    continue
                try:
                    m = await self._kalshi.get_market(t)
                except KalshiAPIError as e:
                    log.warning("market_fetch_failed", ticker=t, error=str(e))
                    continue
                out.append(m)
            return out
        return await self._discover()

    async def generate_signals(self) -> list[Signal]:
        markets = await self._resolve_markets()
        if not markets:
            return []
        log.info("assessing_markets", count=len(markets))

        signals: list[Signal] = []
        outcomes: dict[str, int] = {}
        for market in markets:
            if (market.status or "").lower() not in {"active", "open"}:
                outcomes["not_active"] = outcomes.get("not_active", 0) + 1
                continue
            if not market.yes_ask_dollars or not market.no_ask_dollars:
                outcomes["no_ask_price"] = outcomes.get("no_ask_price", 0) + 1
                continue

            yes_ask = price_str_to_decimal(market.yes_ask_dollars)
            no_ask = price_str_to_decimal(market.no_ask_dollars)

            try:
                resp = await self._llm.chat_json(_SYSTEM_PROMPT, _user_prompt(market))
            except OpenRouterError as e:
                log.warning("llm_call_failed", ticker=market.ticker, error=str(e))
                outcomes["llm_call_failed"] = outcomes.get("llm_call_failed", 0) + 1
                continue

            try:
                prob = float(resp["probability"])
                conf = float(resp["confidence"])
                rationale = str(resp.get("rationale", ""))[:500]
            except (KeyError, TypeError, ValueError) as e:
                log.warning("llm_bad_schema", ticker=market.ticker,
                            error=str(e), resp=resp)
                outcomes["bad_schema"] = outcomes.get("bad_schema", 0) + 1
                continue

            yes_edge = Decimal(str(prob)) - yes_ask
            no_edge = (Decimal("1") - Decimal(str(prob))) - no_ask

            # Decide the outcome, then log every assessment so it's visible how
            # close each market got and which gate (if any) stopped it.
            side: str | None = None
            decision: str
            if not 0.0 <= prob <= 1.0 or not 0.0 <= conf <= 1.0:
                decision = "out_of_range"
            elif Decimal(str(conf)) < self._min_confidence:
                decision = "below_confidence"
            elif yes_edge >= self._min_edge and yes_edge >= no_edge:
                decision, side = "signal_yes", "yes"
            elif no_edge >= self._min_edge:
                decision, side = "signal_no", "no"
            else:
                decision = "below_edge"

            outcomes[decision] = outcomes.get(decision, 0) + 1
            log.info(
                "market_assessed",
                ticker=market.ticker,
                decision=decision,
                probability=round(prob, 4),
                confidence=round(conf, 4),
                yes_ask=str(yes_ask),
                no_ask=str(no_ask),
                yes_edge=str(round(yes_edge, 4)),
                no_edge=str(round(no_edge, 4)),
                min_edge=str(self._min_edge),
                min_confidence=str(self._min_confidence),
                rationale=rationale[:200],
            )

            if side is not None:
                signals.append(Signal(
                    market_ticker=market.ticker, side=side,
                    model_probability=prob, confidence=conf,
                    rationale=rationale,
                    valid_until=datetime.now(UTC) + self._ttl,
                    strategy_name=self.name,
                ))

        log.info("assessment_done", signals_emitted=len(signals), outcomes=outcomes)
        return signals
