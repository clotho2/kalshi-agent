"""Entry point: parse args, wire components, run forever."""

from __future__ import annotations

import argparse
import asyncio
import signal
from decimal import Decimal
from pathlib import Path

import uvicorn

from kalshi_agent.config import load_config
from kalshi_agent.execution import Executor
from kalshi_agent.journal.discord import DiscordNotifier
from kalshi_agent.journal.logger import configure_logging, get_logger
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.safety.kill_switch import KillSwitch
from kalshi_agent.safety.reconciliation import Reconciler
from kalshi_agent.safety.risk_monitor import RiskMonitor
from kalshi_agent.scheduler import build_scheduler
from kalshi_agent.storage.db import healthcheck, init_db, make_engine, session_factory
from kalshi_agent.strategies.placeholder import PlaceholderStrategy


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="kalshi-agent")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--mode", choices=["paper", "live"], default=None,
                   help="override config mode")
    return p.parse_args()


async def amain(args: argparse.Namespace) -> int:
    config = load_config(args.config, mode_override=args.mode)
    configure_logging(config.journal.log_dir, config.journal.retention_days)
    log = get_logger("kalshi_agent.main")

    log.info("startup", mode=config.mode, base_url=config.kalshi_base_url)

    engine = make_engine(config.storage.db_path)
    init_db(engine)
    sm = session_factory(engine)

    kill = KillSwitch(config.kill_switch.halt_file_path)
    discord = DiscordNotifier(
        config.secrets.discord_webhook_url.get_secret_value()
        if config.secrets.discord_webhook_url else None
    )

    client_cm = KalshiClient(
        base_url=config.kalshi_base_url,
        api_key_id=config.secrets.kalshi_api_key_id.get_secret_value(),
        private_key_path=config.secrets.kalshi_private_key_path,
        rate_limit_reads_per_second=config.kalshi.rate_limit_reads_per_second,
        rate_limit_writes_per_second=config.kalshi.rate_limit_writes_per_second,
    )
    client = await client_cm.__aenter__()

    reconciler = Reconciler(client, sm, discord)
    risk = RiskMonitor(config, kill, sm)

    # bankroll provider — for now, a static placeholder; live wiring fetches /portfolio/balance
    def bankroll() -> Decimal:
        return Decimal("1000")  # paper mode starting bankroll

    executor = Executor(config, client, risk, reconciler, sm, discord, bankroll)
    strategy = PlaceholderStrategy(
        config.strategy.placeholder.test_ticker,
        config.strategy.placeholder.emit_interval_seconds,
    )

    sched = build_scheduler(config, strategy, executor, reconciler, sm, discord)
    sched.start()

    stop_event = asyncio.Event()
    risk_task = asyncio.create_task(risk.run_background(stop_event))

    # Startup reconciliation (best-effort — don't block startup on failure)
    asyncio.create_task(reconciler.reconcile(source="startup"))

    # Build FastAPI app & uvicorn server in same loop
    from kalshi_agent.api.server import create_app

    async def trigger_reconcile() -> dict:
        return await reconciler.reconcile(source="manual_http")

    app = create_app(config, sm, kill, trigger_reconcile, lambda: healthcheck(engine))
    uconfig = uvicorn.Config(app, host=config.api.host, port=config.api.port,
                             log_level="info", lifespan="off")
    server = uvicorn.Server(uconfig)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: stop_event.set())

    server_task = asyncio.create_task(server.serve())
    log.info("server_started", host=config.api.host, port=config.api.port)
    if discord.enabled:
        await discord.post(f":rocket: kalshi-agent started in **{config.mode}** mode")

    await stop_event.wait()
    log.info("shutdown_signal_received")
    server.should_exit = True
    sched.shutdown(wait=False)
    risk_task.cancel()
    await asyncio.gather(server_task, return_exceptions=True)
    await client_cm.__aexit__(None, None, None)
    log.info("shutdown_complete")
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
