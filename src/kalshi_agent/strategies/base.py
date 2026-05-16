"""Strategy ABC. Concrete strategies emit Signal objects on a schedule."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Signal(BaseModel):
    market_ticker: str
    side: Literal["yes", "no"]
    model_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    valid_until: datetime
    strategy_name: str = "unknown"

    @field_validator("valid_until")
    @classmethod
    def _must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("valid_until must be timezone-aware (UTC)")
        return v.astimezone(UTC)

    def is_expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return now >= self.valid_until


class Strategy(ABC):
    name: str = "unnamed"

    @abstractmethod
    async def generate_signals(self) -> list[Signal]:
        ...
