from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter

START_TIME = time.monotonic()


def make_router(*, mode_fn, kill_switch_fn, db_health_fn) -> APIRouter:
    router = APIRouter()

    @router.get("/api/observer/health")
    async def health() -> dict:
        return {
            "ok": True,
            "mode": mode_fn(),
            "uptime_seconds": int(time.monotonic() - START_TIME),
            "kill_switch_engaged": kill_switch_fn(),
            "db_ok": db_health_fn(),
            "now_utc": datetime.now(UTC).isoformat(),
        }

    return router
