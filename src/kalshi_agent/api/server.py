"""FastAPI app factory. Wires routes against the live service state."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from kalshi_agent.api.auth import require_control_auth, require_observer_auth
from kalshi_agent.api.routes import control, dashboard, health, observer
from kalshi_agent.config import Config
from kalshi_agent.safety.kill_switch import KillSwitch


def create_app(
    config: Config,
    session_maker: sessionmaker,
    kill_switch: KillSwitch,
    trigger_reconcile: Callable,
    db_health_fn: Callable[[], bool],
) -> FastAPI:
    app = FastAPI(title="kalshi-agent", version="0.1.0", docs_url=None, redoc_url=None)

    control_token = config.secrets.control_bearer_token.get_secret_value()
    control_auth = require_control_auth(control_token)
    observer_auth = require_observer_auth(control_token)

    app.include_router(health.make_router(
        mode_fn=lambda: config.mode,
        kill_switch_fn=kill_switch.is_engaged,
        db_health_fn=db_health_fn,
    ))
    app.include_router(observer.make_router(
        session_maker,
        observer_auth,
        display_tz=config.schedule.display_timezone,
    ))
    app.include_router(control.make_router(kill_switch, control_auth, trigger_reconcile))
    app.include_router(dashboard.make_router(
        session_maker,
        mode_fn=lambda: config.mode,
        kill_switch_fn=kill_switch.is_engaged,
        display_tz=config.schedule.display_timezone,
    ))

    return app
