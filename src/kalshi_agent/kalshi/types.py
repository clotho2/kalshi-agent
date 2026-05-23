"""Kalshi wire types. Prices are decimal strings post the March 2026 migration."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def price_str_to_decimal(s: str) -> Decimal:
    """`"0.6500"` -> Decimal('0.6500'). Accepts ints/floats for back-compat tests."""
    return Decimal(str(s))


def price_decimal_to_str(d: Decimal) -> str:
    """Decimal('0.65') -> `"0.6500"` (4dp, matches Kalshi conventions)."""
    return f"{d.quantize(Decimal('0.0001'))}"


Side = Literal["yes", "no"]
Action = Literal["buy", "sell"]
TimeInForce = Literal["GTC", "IOC", "FOK"]


class Event(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_ticker: str
    series_ticker: str | None = None
    title: str | None = None
    sub_title: str | None = None
    category: str | None = None
    mutually_exclusive: bool | None = None


class Market(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ticker: str
    event_ticker: str | None = None
    title: str | None = None
    subtitle: str | None = None
    description: str | None = None
    category: str | None = None
    status: str | None = None
    yes_bid_dollars: str | None = None
    yes_ask_dollars: str | None = None
    no_bid_dollars: str | None = None
    no_ask_dollars: str | None = None
    last_price_dollars: str | None = None
    close_time: datetime | None = None
    volume: int | None = None
    open_interest: int | None = None
    liquidity_dollars: str | None = None


class OrderRequest(BaseModel):
    ticker: str
    side: Side
    action: Action
    count_fp: str  # decimal string e.g. "10.00"
    yes_price_dollars: str | None = None
    no_price_dollars: str | None = None
    type: Literal["limit", "market"] = "limit"
    time_in_force: TimeInForce = "GTC"
    client_order_id: str


class OrderResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    order_id: str
    client_order_id: str | None = None
    status: str
    ticker: str
    side: Side
    action: Action
    count_fp: str | None = None
    yes_price_dollars: str | None = None
    no_price_dollars: str | None = None
    filled_count_fp: str | None = None
    raw: dict | None = Field(default=None, exclude=True)


class KalshiPosition(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ticker: str
    position: int  # signed: + = YES holdings, - = NO holdings
    market_exposure_dollars: str | None = None
    realized_pnl_dollars: str | None = None


class KalshiFill(BaseModel):
    model_config = ConfigDict(extra="ignore")
    trade_id: str | None = None
    order_id: str
    ticker: str
    side: Side
    action: Action
    count: int
    yes_price_dollars: str | None = None
    no_price_dollars: str | None = None
    is_taker: bool = True
    created_time: datetime | None = None


class KalshiBalance(BaseModel):
    model_config = ConfigDict(extra="ignore")
    balance_dollars: str  # spendable cash, decimal string
    payout_dollars: str | None = None  # value if all open YES positions resolve true
