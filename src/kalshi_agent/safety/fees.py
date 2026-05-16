"""Kalshi fee math. Verified May 2026: parabolic taker fee, maker = 25% of taker."""

from __future__ import annotations

import math
from decimal import Decimal


def taker_fee_per_contract_dollars(price: Decimal, rate: Decimal = Decimal("0.07")) -> Decimal:
    """Kalshi taker fee per contract: ceil(rate * P * (1-P) * 100) / 100 in dollars.

    Peaks at $0.0175 when P=0.50; ~0 at the extremes. Symmetric: fee(P) == fee(1-P).
    """
    raw_cents = rate * price * (Decimal("1.0") - price) * Decimal("100")
    cents = Decimal(math.ceil(raw_cents))
    return cents / Decimal("100")


def maker_fee_per_contract_dollars(price: Decimal, rate: Decimal = Decimal("0.07")) -> Decimal:
    return taker_fee_per_contract_dollars(price, rate) * Decimal("0.25")


def total_fee_dollars(
    contracts: int, price: Decimal, *, is_taker: bool = True, rate: Decimal = Decimal("0.07")
) -> Decimal:
    fn = taker_fee_per_contract_dollars if is_taker else maker_fee_per_contract_dollars
    return fn(price, rate) * Decimal(contracts)


def edge_after_fees_dollars(
    contracts: int,
    model_probability: Decimal,
    price: Decimal,
    side: str,
    *,
    is_taker: bool = True,
    rate: Decimal = Decimal("0.07"),
) -> Decimal:
    """Expected profit in dollars after fees for `contracts` contracts.

    Buying YES at `price` pays out $1 if YES resolves true (prob = model_probability),
    so expected payoff per contract = model_probability * (1 - price) - (1 - model_probability) * price
    Buying NO is the symmetric case.
    """
    p = model_probability if side == "yes" else (Decimal("1.0") - model_probability)
    expected_per_contract = p * (Decimal("1.0") - price) - (Decimal("1.0") - p) * price
    expected = expected_per_contract * Decimal(contracts)
    fees = total_fee_dollars(contracts, price, is_taker=is_taker, rate=rate)
    return expected - fees
