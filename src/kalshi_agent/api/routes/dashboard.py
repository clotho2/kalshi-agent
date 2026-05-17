from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.api.routes.health import START_TIME
from kalshi_agent.storage.models import Decision, Event, Order, PnlDaily, Position

_TEMPLATE = """<!doctype html>
<html><head>
<meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>kalshi-agent</title>
<style>
body {{ font-family: ui-monospace, monospace; max-width: 1080px; margin: 1em auto; padding: 0 1em; }}
h1 {{ font-size: 1.2em; }} h2 {{ font-size: 1em; border-bottom: 1px solid #ccc; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.85em; }}
td, th {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left; }}
.halted {{ color: #b00; font-weight: bold; }}
.live {{ color: #b80; }} .paper {{ color: #080; }}
.kpi {{ display: inline-block; padding: 4px 12px; margin-right: 12px; background: #f4f4f4; border-radius: 4px; }}
#chart-container {{ width: 100%; height: 220px; margin: 0.5em 0; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
</head><body>
<h1>kalshi-agent — <span class="{mode_class}">{mode}</span></h1>
<p>uptime: {uptime}s &middot; kill switch:
{kill_status} &middot;
now (ET): {now_et}</p>

<p>
  <span class="kpi">realized pnl: ${realized_pnl_total:.4f}</span>
  <span class="kpi">today: ${today_pnl:.4f}</span>
  <span class="kpi">open positions: {open_count}</span>
  <span class="kpi">open exposure: ${open_exposure:.2f}</span>
</p>

<h2>equity curve (realized cumulative pnl by day)</h2>
<div id="chart-container"><canvas id="equity"></canvas></div>
<script>
const data = {chart_data_json};
new Chart(document.getElementById('equity'), {{
  type: 'line',
  data: {{
    labels: data.days,
    datasets: [{{
      label: 'realized pnl ($)',
      data: data.realized,
      borderColor: '#080',
      backgroundColor: 'rgba(0,128,0,0.1)',
      fill: true,
      tension: 0.1,
    }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
}});
</script>

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
            daily = s.scalars(
                select(PnlDaily).order_by(PnlDaily.day.desc()).limit(30)
            ).all()
            all_positions = s.scalars(select(Position)).all()

        daily = list(reversed(daily))
        realized_total = sum(p.realized_pnl_dollars for p in all_positions)
        today_local = datetime.now(UTC).astimezone(tz).strftime("%Y-%m-%d")
        today_pnl = next(
            (p.realized_pnl for p in daily if p.day == today_local), realized_total
        ) if daily else realized_total
        open_exposure = sum(
            float(p.avg_price_dollars) * p.count for p in positions
        )

        engaged = kill_switch_fn()
        chart_data = {
            "days": [p.day for p in daily],
            "realized": [p.realized_pnl for p in daily],
            "fees": [p.fees for p in daily],
        }
        return _TEMPLATE.format(
            mode=mode_fn(),
            mode_class="paper" if mode_fn() == "paper" else "live",
            uptime=int(_t.monotonic() - START_TIME),
            kill_status='<span class="halted">ENGAGED</span>' if engaged else "ok",
            now_et=datetime.now(UTC).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z"),
            realized_pnl_total=realized_total,
            today_pnl=today_pnl,
            open_count=len(positions),
            open_exposure=open_exposure,
            chart_data_json=json.dumps(chart_data),
            positions_table=_render_table(
                ["ticker", "side", "count", "avg price", "realized pnl"],
                [
                    [p.market_ticker, p.side, str(p.count), p.avg_price_dollars,
                     f"{p.realized_pnl_dollars:.4f}"]
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
