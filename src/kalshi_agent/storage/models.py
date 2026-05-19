"""SQLAlchemy models. All timestamps stored as UTC ISO-8601 strings."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Decision(Base):
    """Every signal — whether accepted or rejected — is persisted here."""

    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    strategy: Mapped[str] = mapped_column(String(64))
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(4))  # yes | no
    model_probability: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted: Mapped[bool] = mapped_column(default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kalshi_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(4))
    action: Mapped[str] = mapped_column(String(8))  # buy | sell
    count: Mapped[int] = mapped_column(Integer)
    price_dollars: Mapped[str] = mapped_column(String(16))  # decimal string from Kalshi
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    raw_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Fill(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kalshi_order_id: Mapped[str] = mapped_column(String(64), index=True)
    market_ticker: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(4))
    count: Mapped[int] = mapped_column(Integer)
    price_dollars: Mapped[str] = mapped_column(String(16))
    fee_dollars: Mapped[str] = mapped_column(String(16), default="0.0000")
    is_taker: Mapped[bool] = mapped_column(default=True)


class Position(Base):
    __tablename__ = "positions"
    market_ticker: Mapped[str] = mapped_column(String(64), primary_key=True)
    side: Mapped[str] = mapped_column(String(4))
    count: Mapped[int] = mapped_column(Integer, default=0)
    avg_price_dollars: Mapped[str] = mapped_column(String(16), default="0.0000")
    realized_pnl_dollars: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Generic event log — kill switch, reconciliation, errors, etc."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PnlDaily(Base):
    __tablename__ = "pnl_daily"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD in display TZ
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
