from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.safety.pnl import cumulative_realized_pnl, realized_pnl_since
from kalshi_agent.storage.models import Decision, Fill, Order, PnlDaily, Position

UTC = timezone.utc


def make_router(session_maker: sessionmaker, observer_auth, display_tz: str = "America/New_York") -> APIRouter:
    router = APIRouter(prefix="/api/observer", dependencies=[Depends(observer_auth)])
    tz = ZoneInfo(display_tz)

    @router.get("/positions")
    async def positions() -> list[dict]:
        with session_maker() as s:
            rows = s.scalars(select(Position)).all()
            return [
                {
                    "ticker": p.market_ticker,
                    "side": p.side,
                    "count": p.count,
                    "avg_price_dollars": p.avg_price_dollars,
                    "realized_pnl_dollars": p.realized_pnl_dollars,
                    "updated_at": p.updated_at.isoformat() if p.updated_at else None,
                }
                for p in rows
            ]

    @router.get("/trades")
    async def trades(limit: int = Query(20, le=200)) -> list[dict]:
        with session_maker() as s:
            rows = s.scalars(
                select(Order).order_by(Order.created_at.desc()).limit(limit)
            ).all()
            out: list[dict] = []
            for o in rows:
                decision = None
                if o.decision_id:
                    d = s.get(Decision, o.decision_id)
                    decision = {
                        "model_probability": d.model_probability if d else None,
                        "rationale": d.rationale if d else None,
                        "strategy": d.strategy if d else None,
                    }
                out.append({
                    "created_at": o.created_at.isoformat(),
                    "client_order_id": o.client_order_id,
                    "kalshi_order_id": o.kalshi_order_id,
                    "ticker": o.market_ticker,
                    "side": o.side,
                    "action": o.action,
                    "count": o.count,
                    "price_dollars": o.price_dollars,
                    "status": o.status,
                    "decision": decision,
                })
            return out

    @router.get("/pnl")
    async def pnl(period: str = Query("today", pattern="^(today|week|month|all)$")) -> dict:
        now = datetime.now(UTC)
        if period == "today":
            now_local = now.astimezone(tz)
            cutoff = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        else:
            cutoff = {
                "week": now - timedelta(days=7),
                "month": now - timedelta(days=30),
                "all": datetime(1970, 1, 1, tzinfo=UTC),
            }[period]
        with session_maker() as s:
            fills = s.scalars(select(Fill).where(Fill.created_at >= cutoff)).all()
            fees = sum((Decimal(f.fee_dollars) for f in fills), Decimal("0"))
            trade_count = len(fills)
            if period == "all":
                realized = cumulative_realized_pnl(s)
            else:
                realized = realized_pnl_since(s, cutoff, tz)
        return {
            "period": period,
            "cutoff_utc": cutoff.isoformat(),
            "fees_dollars": float(fees),
            "trade_count": trade_count,
            "realized_pnl_dollars": float(realized),
        }

    @router.get("/equity_curve")
    async def equity_curve(days: int = Query(30, le=365)) -> dict:
        cutoff_date = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        with session_maker() as s:
            rows = s.scalars(
                select(PnlDaily).where(PnlDaily.day >= cutoff_date)
                .order_by(PnlDaily.day.asc())
            ).all()
        labels = [r.day for r in rows]
        realized = [r.realized_pnl for r in rows]
        fees = [r.fees for r in rows]
        return {"days": labels, "realized_pnl": realized, "fees": fees}

    @router.get("/market/{ticker}")
    async def market(ticker: str) -> dict:
        with session_maker() as s:
            pos = s.get(Position, ticker)
            last_decision = s.scalars(
                select(Decision).where(Decision.market_ticker == ticker)
                .order_by(Decision.created_at.desc()).limit(1)
            ).first()
        return {
            "ticker": ticker,
            "position": {
                "side": pos.side, "count": pos.count,
                "avg_price_dollars": pos.avg_price_dollars,
            } if pos else None,
            "last_model_probability": last_decision.model_probability if last_decision else None,
            "last_decision_at": last_decision.created_at.isoformat() if last_decision else None,
        }

    return router
