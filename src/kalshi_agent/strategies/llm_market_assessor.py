"""LLM-driven Kalshi market mispricing strategy.

For each whitelisted market, we ask an LLM to estimate the true probability of
YES given the market description, current price, and time-to-close. The LLM
returns a structured response with `probability`, `confidence`, and `rationale`.

Only markets where:
  - LLM probability differs from market mid by > min_edge_threshold
  - LLM confidence > min_confidence
  - market is active and within trading hours
produce signals. The downstream risk monitor still re-checks edge after fees.

This is a real, conservative strategy: it will produce few signals (most
markets will fail the edge/confidence filter), but the ones it produces are
informed by structured LLM reasoning rather than randomness or placeholders.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_agent.kalshi.types import price_str_to_decimal
from kalshi_agent.llm.openrouter import OpenRouterClient, OpenRouterError
from kalshi_agent.storage.models import Decision
from kalshi_agent.strategies.base import Signal, Strategy

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
  "probability": 0.0..1.0,    // your point estimate of P(YES)
  "confidence": 0.0..1.0,     // 0=no view, 1=very high conviction
  "rationale": "string"        // 1-3 sentences explaining the reasoning
}
"""


def _user_prompt(
    *,
    ticker: str,
    title: str,
    subtitle: str | None,
    description: str | None,
    yes_bid: str | None,
    yes_ask: str | None,
    no_bid: str | None,
    no_ask: str | None,
    close_time: datetime | None,
) -> str:
    mid = None
    if yes_bid and yes_ask:
        mid = (price_str_to_decimal(yes_bid) + price_str_to_decimal(yes_ask)) / 2
    time_to_close = ""
    if close_time:
        delta = close_time - datetime.now(UTC)
        if delta.total_seconds() > 0:
            hours = delta.total_seconds() / 3600
            time_to_close = f"\nTime until market close: {hours:.1f} hours"
    return f"""Market ticker: {ticker}
Title: {title}
Subtitle: {subtitle or '(none)'}
Description: {description or '(none)'}{time_to_close}

Current prices (decimal dollars, 0.0000 to 1.0000):
  YES bid={yes_bid}  YES ask={yes_ask}
  NO  bid={no_bid}   NO  ask={no_ask}
  YES mid={mid if mid is not None else 'unknown'}

Estimate P(YES) and your confidence."""


class LLMMarketAssessor(Strategy):
    """Polls a list of tickers, asks the LLM to assess each, emits filtered signals."""

    name = "llm_market_assessor"

    def __init__(
        self,
        *,
        kalshi_client: KalshiClient,
        llm_client: OpenRouterClient,
        session_maker: sessionmaker,
        tickers: list[str],
        min_edge: Decimal,
        min_confidence: Decimal,
        signal_ttl_minutes: int,
        min_seconds_between_signals_per_ticker: int = 1800,
    ) -> None:
        self._kalshi = kalshi_client
        self._llm = llm_client
        self._sm = session_maker
        self._tickers = tickers
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

    async def generate_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        for ticker in self._tickers:
            if self._was_recently_signalled(ticker):
                continue
            try:
                market = await self._kalshi.get_market(ticker)
            except KalshiAPIError as e:
                log.warning("market_fetch_failed", ticker=ticker, error=str(e))
                continue

            if (market.status or "").lower() not in {"active", "open"}:
                continue
            if not market.yes_ask_dollars or not market.no_ask_dollars:
                continue

            yes_ask = price_str_to_decimal(market.yes_ask_dollars)
            no_ask = price_str_to_decimal(market.no_ask_dollars)
            yes_mid = None
            if market.yes_bid_dollars and market.yes_ask_dollars:
                yes_mid = (price_str_to_decimal(market.yes_bid_dollars)
                           + price_str_to_decimal(market.yes_ask_dollars)) / 2

            prompt = _user_prompt(
                ticker=ticker,
                title=getattr(market, "title", None) or ticker,
                subtitle=getattr(market, "subtitle", None),
                description=getattr(market, "description", None),
                yes_bid=market.yes_bid_dollars,
                yes_ask=market.yes_ask_dollars,
                no_bid=market.no_bid_dollars,
                no_ask=market.no_ask_dollars,
                close_time=market.close_time,
            )

            try:
                resp = await self._llm.chat_json(_SYSTEM_PROMPT, prompt)
            except OpenRouterError as e:
                log.warning("llm_call_failed", ticker=ticker, error=str(e))
                continue

            try:
                prob = float(resp["probability"])
                conf = float(resp["confidence"])
                rationale = str(resp.get("rationale", ""))[:500]
            except (KeyError, TypeError, ValueError) as e:
                log.warning("llm_bad_schema", ticker=ticker, error=str(e), resp=resp)
                continue
            if not 0.0 <= prob <= 1.0 or not 0.0 <= conf <= 1.0:
                continue
            if Decimal(str(conf)) < self._min_confidence:
                continue

            # Edge per side
            yes_edge = Decimal(str(prob)) - yes_ask
            no_edge = (Decimal("1") - Decimal(str(prob))) - no_ask

            if yes_edge >= self._min_edge and yes_edge >= no_edge:
                signals.append(Signal(
                    market_ticker=ticker,
                    side="yes",
                    model_probability=prob,
                    confidence=conf,
                    rationale=rationale,
                    valid_until=datetime.now(UTC) + self._ttl,
                    strategy_name=self.name,
                ))
            elif no_edge >= self._min_edge:
                signals.append(Signal(
                    market_ticker=ticker,
                    side="no",
                    model_probability=prob,
                    confidence=conf,
                    rationale=rationale,
                    valid_until=datetime.now(UTC) + self._ttl,
                    strategy_name=self.name,
                ))

        return signals
