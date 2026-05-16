from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.api.routes.health import START_TIME
from kalshi_agent.storage.models import Decision, Event, Order, Position

_TEMPLATE = """<!doctype html>
<html><head>
<meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>kalshi-agent</title>
<style>
body {{ font-family: ui-monospace, monospace; max-width: 980px; margin: 1em auto; padding: 0 1em; }}
h1 {{ font-size: 1.2em; }} h2 {{ font-size: 1em; border-bottom: 1px solid #ccc; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85em; }}
td, th {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; }}
.halted {{ color: #b00; font-weight: bold; }}
.live {{ color: #b80; }} .paper {{ color: #080; }}
details {{ margin-top: 0.5em; }}
</style>
</head><body>
<h1>kalshi-agent — <span class="{mode_class}">{mode}</span></h1>
<p>uptime: {uptime}s &middot; kill switch:
{kill_status} &middot;
now (ET): {now_et}</p>

<h2>open positions</h2>
{positions_table}

<h2>recent orders</h2>
{orders_table}

<h2>recent decisions</h2>
{decisions_table}

<h2>recent events</h2>
{events_table}
</body></html>
"""


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "<p><em>(empty)</em></p>"
    h = "".join(f"<th>{c}</th>" for c in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"


def make_router(
    session_maker: sessionmaker,
    *,
    mode_fn,
    kill_switch_fn,
    display_tz: str,
) -> APIRouter:
    router = APIRouter()
    tz = ZoneInfo(display_tz)

    @router.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        import time as _t
        with session_maker() as s:
            positions = s.scalars(select(Position).where(Position.count != 0)).all()
            orders = s.scalars(
                select(Order).order_by(Order.created_at.desc()).limit(15)
            ).all()
            decisions = s.scalars(
                select(Decision).order_by(Decision.created_at.desc()).limit(15)
            ).all()
            events = s.scalars(
                select(Event).order_by(Event.created_at.desc()).limit(15)
            ).all()

        engaged = kill_switch_fn()
        return _TEMPLATE.format(
            mode=mode_fn(),
            mode_class="paper" if mode_fn() == "paper" else "live",
            uptime=int(_t.monotonic() - START_TIME),
            kill_status='<span class="halted">ENGAGED</span>' if engaged else "ok",
            now_et=datetime.now(UTC).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z"),
            positions_table=_render_table(
                ["ticker", "side", "count", "avg price", "realized pnl"],
                [
                    [p.market_ticker, p.side, str(p.count), p.avg_price_dollars,
                     f"{p.realized_pnl_dollars:.2f}"]
                    for p in positions
                ],
            ),
            orders_table=_render_table(
                ["time (ET)", "ticker", "side", "count", "price", "status"],
                [
                    [
                        o.created_at.astimezone(tz).strftime("%H:%M:%S"),
                        o.market_ticker, o.side, str(o.count),
                        o.price_dollars, o.status,
                    ]
                    for o in orders
                ],
            ),
            decisions_table=_render_table(
                ["time (ET)", "strategy", "ticker", "side", "p", "accepted", "reason / rationale"],
                [
                    [
                        d.created_at.astimezone(tz).strftime("%H:%M:%S"),
                        d.strategy, d.market_ticker, d.side,
                        f"{d.model_probability:.3f}",
                        "y" if d.accepted else "n",
                        (d.rejection_reason or d.rationale)[:80],
                    ]
                    for d in decisions
                ],
            ),
            events_table=_render_table(
                ["time (ET)", "kind", "level", "message"],
                [
                    [
                        e.created_at.astimezone(tz).strftime("%H:%M:%S"),
                        e.kind, e.level, e.message[:120],
                    ]
                    for e in events
                ],
            ),
        )

    return router
