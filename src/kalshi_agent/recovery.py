"""Startup recovery: reconcile any orders that were in-flight when the process died.

For each local order with status='pending', query Kalshi to learn the true state.
If the server has the order, take its status; if not, mark as 'lost' for human review.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from kalshi_agent.journal.discord import DiscordNotifier
from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_agent.storage.models import Event, Order

log = get_logger(__name__)


async def recover_in_flight_orders(
    client: KalshiClient,
    session_maker: sessionmaker,
    discord: DiscordNotifier | None,
) -> dict:
    """Returns summary dict: {pending: N, recovered: N, lost: N}."""
    recovered = 0
    lost = 0
    with session_maker() as s:
        pending = s.scalars(
            select(Order).where(Order.status == "pending")
        ).all()
        pending_ids = [(o.id, o.client_order_id, o.kalshi_order_id) for o in pending]

    log.info("recovery_start", pending_count=len(pending_ids))

    # Query all orders from Kalshi and match by client_order_id (most reliable)
    try:
        all_open = await client.list_orders(status="resting")
        all_closed = await client.list_orders(status="executed")
        all_cancelled = await client.list_orders(status="canceled")
    except KalshiAPIError as e:
        log.error("recovery_list_orders_failed", error=str(e))
        return {"pending": len(pending_ids), "recovered": 0, "lost": 0, "error": str(e)}

    remote_by_client_id = {
        o.client_order_id: o
        for o in (all_open + all_closed + all_cancelled)
        if o.client_order_id
    }
    remote_by_order_id = {o.order_id: o for o in (all_open + all_closed + all_cancelled)}

    for local_id, coid, kid in pending_ids:
        match = None
        if coid and coid in remote_by_client_id:
            match = remote_by_client_id[coid]
        elif kid and kid in remote_by_order_id:
            match = remote_by_order_id[kid]

        with session_maker() as s:
            row = s.get(Order, local_id)
            if row is None:
                continue
            if match is not None:
                row.kalshi_order_id = match.order_id
                row.status = match.status
                row.raw_response = match.raw or {}
                recovered += 1
                s.add(Event(
                    kind="recovery",
                    level="info",
                    message=f"recovered in-flight order {coid} -> {match.order_id}/{match.status}",
                ))
            else:
                row.status = "lost"
                lost += 1
                s.add(Event(
                    kind="recovery",
                    level="warning",
                    message=f"lost in-flight order {coid}: not present on Kalshi after restart",
                ))
            s.commit()

    if lost > 0 and discord:
        await discord.post(
            f":warning: Startup recovery: {recovered} recovered, **{lost} lost** "
            f"(see events table)"
        )
    log.info("recovery_done", recovered=recovered, lost=lost)
    return {"pending": len(pending_ids), "recovered": recovered, "lost": lost}
