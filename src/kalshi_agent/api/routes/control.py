from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from kalshi_agent.safety.kill_switch import KillSwitch


def make_router(
    kill_switch: KillSwitch,
    control_auth,
    trigger_reconcile,  # async callable
) -> APIRouter:
    router = APIRouter(prefix="/api/control", dependencies=[Depends(control_auth)])

    @router.post("/halt")
    async def halt(payload: dict = Body(default_factory=dict)) -> dict:
        reason = payload.get("reason", "manual_http")
        kill_switch.engage(reason, source="http", payload=payload)
        return {"engaged": True, "reason": reason}

    @router.post("/resume")
    async def resume() -> dict:
        ok = kill_switch.disengage()
        return {"disengaged": ok}

    @router.post("/reconcile")
    async def reconcile() -> dict:
        result = await trigger_reconcile()
        return result

    return router
